# ruff: noqa: E402,I001
import base64
import hashlib
import inspect
import io
import json
import os
import sys
import tempfile
import unittest
import wave
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(CURRENT_DIR)
SCRIPTS_PATH = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, SCRIPTS_PATH)

import utils.revision_runner as revision_runner_api
import utils.revision_validation as revision_validation_api
import utils.formatters as formatters_api
from utils.revision_runner import (
    _add_visual_overlay_segments,
    _merge_pause_results_into_items,
    _merge_visual_results_into_items,
    _visual_segment_uses_video_asset,
    build_revision_summary,
    execute_revision_request,
    load_revision_request,
    validate_saved_revision_draft,
    validate_revision_acceptance,
)
from utils.revision_models import RevisionReviewItem
from utils.revision_validation import _merge_unique_review_items


_TEST_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _test_wav_bytes(duration_seconds: float = 0.1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\0\0" * max(1, round(16_000 * duration_seconds)))
    return buffer.getvalue()


class TestRevisionRunner(unittest.TestCase):
    def _load_request_payload(self, payload, *, bind_audio_report=True):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)
        summary_path = str(request.processed_audio.get("validation_summary") or "")
        if (
            bind_audio_report
            and request.audio_delivery_plan.mode == "segmented"
            and os.path.isfile(summary_path)
        ):
            with open(summary_path, "r", encoding="utf-8") as summary_file:
                summary = json.load(summary_file)
            summary["audio_delivery_plan_sha256"] = (
                revision_validation_api._audio_delivery_plan_digest(request)
            )
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump(summary, summary_file, ensure_ascii=False, indent=2)
        return request

    def _conditional_request(
        self,
        *,
        kind,
        op_type,
        extra_op_types=(),
        source_kind="",
        extra_source_kinds=(),
        execution_required=True,
        evidence=None,
        validation=None,
        acceptance=None,
        processed_audio=None,
    ):
        operation_types = (op_type, *extra_op_types)
        source_kinds = (source_kind, *extra_source_kinds)
        if len(source_kinds) < len(operation_types):
            source_kinds = (*source_kinds, *("" for _ in operation_types[len(source_kinds) :]))
        edits = [
            {
                "type": operation_type,
                "doc_item_id": "item01",
                "start": 1.0,
                "end": 2.0,
                "label": "item01 requested edit",
            }
            for operation_type in operation_types
        ]
        for edit, edit_source_kind in zip(edits, source_kinds):
            if edit_source_kind:
                edit["source_kind"] = edit_source_kind
        project = {
            "draft_name": "ConditionalAcceptance",
            "source_video": "C:/media/source.mp4",
            "source_audio": "C:/media/source.wav",
        }
        if "replace_audio" in operation_types:
            project["replacement_audio"] = "C:/media/replacement.wav"
        payload = {
            "project": project,
            "edits": edits,
            "markers": [],
            "review_items": [
                {
                    "id": "item01",
                    "kind": kind,
                    "source_text": "item01 requested edit",
                    "execution_required": execution_required,
                    "evidence": evidence if evidence is not None else {"executed": True},
                    "validation": validation if validation is not None else {"status": "pass"},
                }
            ],
        }
        if acceptance is not None:
            payload["acceptance"] = acceptance
        if processed_audio is not None:
            payload["processed_audio"] = processed_audio
        return self._load_request_payload(payload)

    def _write_full_candidate_reverse_asr(
        self,
        tmpdir,
        *,
        rows=None,
        summary_overrides=None,
        duration_seconds=1.0,
    ):
        candidate_path = os.path.join(tmpdir, "final-candidate.wav")
        summary_path = os.path.join(tmpdir, "reverse-asr.json")
        candidate_bytes = _test_wav_bytes(duration_seconds)
        with open(candidate_path, "wb") as f:
            f.write(candidate_bytes)
        raw_rows = rows if rows is not None else [{"id": "item01", "status": "pass"}]
        attributable_rows = []
        for row in raw_rows:
            attributable_rows.append(
                {
                    "strategy": "hybrid",
                    "source_cut_windows": [[1.0, 2.0]],
                    "mapped_join_times": [1.0],
                    "local_joined_text": "kept context",
                    "delete_hits": [],
                    "keep_hits": {},
                    "semantic_join_validation": {"status": "pass"},
                    **row,
                }
            )
        summary = {
            "candidate_audio_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            "asr_identity": {
                "provider": "test-provider",
                "model": "test-model",
                "adapter_version": "1",
            },
            "status_counts": {"pass": 1},
            "rows": attributable_rows,
        }
        if summary_overrides:
            summary.update(summary_overrides)
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        return {
            "output_wav": candidate_path,
            "validation_summary": summary_path,
        }

    def _segmented_audio_request(
        self,
        processed_audio,
        *,
        duration,
        bind_audio_report=True,
    ):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "FullCandidateCoverage",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                },
                "audio_delivery_plan": {
                    "mode": "segmented",
                    "segments": [
                        {
                            "id": "source-1",
                            "role": "source",
                            "asset_path": "C:/media/source.wav",
                            "track_name": "Narration",
                            "source_start": 0.0,
                            "timeline_start": 0.0,
                            "duration": duration,
                        }
                    ],
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "spoken_delete",
                        "doc_item_id": "item01",
                        "start": duration,
                        "end": duration + 0.1,
                        "label": "delete filler",
                    }
                ],
                "review_items": [
                    {
                        "id": "item01",
                        "kind": "spoken_delete",
                        "source_text": "delete filler",
                        "execution_required": True,
                        "evidence": {
                            "executed": True,
                            "cut_window": [duration, duration + 0.1],
                        },
                        "validation": {"status": "pass"},
                    }
                ],
                "processed_audio": processed_audio,
            },
            bind_audio_report=False,
        )
        summary_path = request.processed_audio["validation_summary"]
        with open(summary_path, "r", encoding="utf-8") as summary_file:
            summary = json.load(summary_file)
        for row in summary.get("rows") or []:
            row["source_cut_windows"] = [[duration, duration + 0.1]]
            row["mapped_join_times"] = [duration]
        if bind_audio_report:
            summary["audio_delivery_plan_sha256"] = (
                revision_validation_api._audio_delivery_plan_digest(request)
            )
        with open(summary_path, "w", encoding="utf-8") as summary_file:
            json.dump(summary, summary_file, ensure_ascii=False, indent=2)
        return request

    def test_saved_variant_loader_rejects_unreadable_root_even_with_active_timeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            active_dir = os.path.join(tmpdir, "Timelines", "active")
            os.makedirs(active_dir, exist_ok=True)
            with open(os.path.join(tmpdir, "draft_content.json"), "w", encoding="utf-8") as f:
                f.write("encoded root")
            with open(os.path.join(tmpdir, "timeline_layout.json"), "w", encoding="utf-8") as f:
                json.dump({"activeTimeline": "active"}, f)
            with open(os.path.join(active_dir, "draft_content.json"), "w", encoding="utf-8") as f:
                json.dump({"tracks": [], "materials": {}}, f)

            with self.assertRaisesRegex(RuntimeError, "root.*not readable JSON"):
                revision_runner_api._load_saved_draft_content_variants({"draft_path": tmpdir})

    def test_saved_variant_loader_rejects_missing_declared_active_timeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "draft_content.json"), "w", encoding="utf-8") as f:
                json.dump({"tracks": [], "materials": {}}, f)
            with open(os.path.join(tmpdir, "timeline_layout.json"), "w", encoding="utf-8") as f:
                json.dump({"activeTimeline": "missing-active"}, f)

            with self.assertRaisesRegex(RuntimeError, "active_timeline.*does not exist"):
                revision_runner_api._load_saved_draft_content_variants({"draft_path": tmpdir})

    def test_saved_variant_validation_rejects_flattened_active_timeline(self):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "ActiveTimelineStructure",
                    "source_video": "C:/media/source.mp4",
                },
                "edits": [],
                "markers": [],
                "preserve": {
                    "source_video_material": False,
                    "separated_audio_material": False,
                    "replacement_audio_material": False,
                    "keep_cut_points": False,
                    "keep_review_markers_separate": False,
                },
            }
        )
        root_content = {
            "duration": 10_000_000,
            "tracks": [
                {
                    "type": "video",
                    "name": "Original Video",
                    "segments": [
                        {
                            "id": "root-video",
                            "material_id": "root-material",
                            "source_timerange": {"start": 0, "duration": 10_000_000},
                            "target_timerange": {"start": 0, "duration": 10_000_000},
                        }
                    ],
                }
            ],
            "materials": {
                "videos": [{"id": "root-material", "path": "C:/media/source.mp4"}],
                "audios": [],
                "texts": [],
            },
        }
        active_content = {
            **root_content,
            "tracks": [
                {
                    "type": "video",
                    "name": "Final Video",
                    "segments": [
                        {
                            "id": "flattened-video",
                            "material_id": "root-material",
                            "source_timerange": {"start": 0, "duration": 10_000_000},
                            "target_timerange": {"start": 0, "duration": 10_000_000},
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            active_dir = os.path.join(tmpdir, "Timelines", "active")
            os.makedirs(active_dir, exist_ok=True)
            with open(os.path.join(tmpdir, "draft_content.json"), "w", encoding="utf-8") as f:
                json.dump(root_content, f)
            with open(os.path.join(tmpdir, "timeline_layout.json"), "w", encoding="utf-8") as f:
                json.dump({"activeTimeline": "active"}, f)
            with open(os.path.join(active_dir, "draft_content.json"), "w", encoding="utf-8") as f:
                json.dump(active_content, f)

            _variants, _content, validation = revision_runner_api._validate_saved_revision_variants(
                request,
                {"draft_path": tmpdir},
                draft_name="ActiveTimelineStructure",
                doc_items=None,
                marker_receipts=[],
                marker_plan=[],
            )

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                "active_timeline:active" in message and "Final Video" in message
                for message in validation["errors"]
            ),
            validation["errors"],
        )

    def test_strict_saved_variant_validation_rejects_centered_active_marker(self):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "ActiveTimelineMarkerLayout",
                    "source_video": "C:/media/source.mp4",
                },
                "edits": [],
                "markers": [],
                "preserve": {
                    "source_video_material": False,
                    "separated_audio_material": False,
                    "replacement_audio_material": False,
                    "keep_cut_points": False,
                    "keep_review_markers_separate": False,
                },
            }
        )
        root_content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Review Marker 1",
                    "type": "text",
                    "segments": [
                        {
                            "id": "marker-segment",
                            "clip": {"transform": {"y": 0.8}},
                            "target_timerange": {
                                "start": 1_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {"videos": [], "audios": [], "texts": []},
        }
        active_content = json.loads(json.dumps(root_content))
        active_content["tracks"][1]["segments"][0]["clip"]["transform"]["y"] = 0.0

        with tempfile.TemporaryDirectory() as tmpdir:
            active_dir = os.path.join(tmpdir, "Timelines", "active")
            os.makedirs(active_dir, exist_ok=True)
            with open(os.path.join(tmpdir, "draft_content.json"), "w", encoding="utf-8") as file:
                json.dump(root_content, file)
            with open(os.path.join(tmpdir, "timeline_layout.json"), "w", encoding="utf-8") as file:
                json.dump({"activeTimeline": "active"}, file)
            with open(
                os.path.join(active_dir, "draft_content.json"), "w", encoding="utf-8"
            ) as file:
                json.dump(active_content, file)

            _variants, _content, validation = revision_runner_api._validate_saved_revision_variants(
                request,
                {"draft_path": tmpdir},
                draft_name="ActiveTimelineMarkerLayout",
                doc_items=None,
                marker_receipts=[],
                marker_plan=[],
                strict=True,
            )

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                "active_timeline:active" in error
                and "review_marker_top_layout.y_out_of_band" in error
                for error in validation["errors"]
            ),
            validation["errors"],
        )

    def _derive_acceptance_profile(self, request, *, doc_items=None, supplied=False):
        self.assertTrue(
            hasattr(revision_runner_api, "derive_acceptance_profile"),
            "derive_acceptance_profile must be exported by utils.revision_runner",
        )
        derive = revision_runner_api.derive_acceptance_profile
        if supplied:
            return derive(request, doc_items=doc_items)
        return derive(request)

    def _validate_visual_delete_source_ranges(self, source_ranges, source_window=(2.0, 4.0)):
        request = self._conditional_request(
            kind="visual_delete",
            op_type="visual_delete",
            evidence={
                "operation": "visual_delete",
                "source_window": list(source_window),
            },
        )
        target_cursor = 0.0
        segments = []
        for idx, (source_start, source_end) in enumerate(source_ranges, start=1):
            duration = source_end - source_start
            segments.append(
                {
                    "id": f"main-{idx}",
                    "source_timerange": {
                        "start": round(source_start * 1_000_000),
                        "duration": round(duration * 1_000_000),
                    },
                    "target_timerange": {
                        "start": round(target_cursor * 1_000_000),
                        "duration": round(duration * 1_000_000),
                    },
                }
            )
            target_cursor += duration
        content = {
            "duration": round(target_cursor * 1_000_000),
            "tracks": [
                {
                    "type": "video",
                    "name": "Original Video",
                    "segments": segments,
                }
            ],
            "materials": {"videos": [{"path": "C:/media/source.mp4"}]},
        }
        return validate_revision_acceptance(request, content, strict=True)

    def _write_png_fixture(self, path):
        # 1x1 transparent PNG.
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
            b"\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05"
            b"\xfe\x02\xfeA\x89\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        with open(path, "wb") as f:
            f.write(png_bytes)

    def _write_video_fixture(self, path):
        # VideoMaterial accepts the requested fallback duration when media probing finds no track.
        with open(path, "wb") as f:
            f.write(b"fixture")

    def _visual_video_payload(self, overlay_path, *, include_volume, volume=None):
        segment = {
            "role": "replacement_video_keep_1",
            "asset_type": "video",
            "asset_path": overlay_path,
            "track_name": "Replacement Video",
            "timeline_start": 10.0,
            "source_start": 10.0,
            "duration": 2.0,
        }
        if include_volume:
            segment["volume"] = volume
        return {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "visual_overlay",
                    "doc_item_id": "修改01",
                    "start": 10.0,
                    "end": 12.0,
                    "label": "修改01 replacement video",
                    "visual_plan": {"segments": [segment]},
                }
            ],
        }

    def test_load_revision_request_normalizes_review_job(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
                "replacement_audio": "C:/media/replacement.mp3",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 0.0,
                    "end": 3.0,
                    "label": "Remove opener",
                    "detail": "Delete 0-3s title opener",
                },
                {
                    "type": "replace_audio",
                    "start": 197.0,
                    "end": 205.0,
                    "audio_path": "C:/media/replacement.mp3",
                    "label": "Replace narration",
                    "detail": "Use provided MP3 for the narration window",
                },
            ],
            "markers": [
                {
                    "label": "Animation check",
                    "start": 220.0,
                    "end": 221.0,
                    "detail": "Verify animation timing",
                }
            ],
            "preserve": {
                "source_video_material": True,
                "separated_audio_material": True,
                "replacement_audio_material": True,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            request = load_revision_request(path)

        self.assertEqual(request.project.draft_name, "ReviewDraft")
        self.assertEqual(request.project.source_video, "C:/media/source.mp4")
        self.assertEqual(len(request.edits), 2)
        self.assertEqual(request.edits[0].op_type, "delete")
        self.assertEqual(request.edits[1].op_type, "replace_audio")
        self.assertEqual(request.edits[1].audio_path, "C:/media/replacement.mp3")
        self.assertEqual(len(request.markers), 1)
        self.assertTrue(request.preserve.keep_cut_points)
        self.assertTrue(request.preserve.source_video_material)

    def test_load_revision_request_preserves_explicit_source_text_verbatim(self):
        source_text = "  00:51，删除“是吧”\r\n保留??  "
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
            },
            "review_items": [
                {
                    "id": "修改003",
                    "source_text": source_text,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        self.assertEqual(request.review_items[0].source_text, source_text)
        self.assertEqual(request.review_items[0].verbatim_status, "verified")

    def test_load_revision_request_preserves_fallback_source_text_as_unverified(self):
        detail = "  00:52，保留 detail??\r\n第二行  "
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
            },
            "review_items": [
                {
                    "id": "校对004",
                    "detail": detail,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        self.assertEqual(request.review_items[0].source_text, detail)
        self.assertEqual(
            request.review_items[0].verbatim_status,
            "unverified_source_unavailable",
        )

    def test_load_revision_request_preserves_marker_doc_item_aliases(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
            },
            "markers": [
                {
                    "doc_item_id": "修改003",
                    "label": "First marker",
                    "start": 1.0,
                    "end": 2.0,
                },
                {
                    "item_id": "校对004",
                    "label": "Second marker",
                    "start": 2.0,
                    "end": 3.0,
                },
                {
                    "id": "修改005",
                    "label": "Third marker",
                    "start": 3.0,
                    "end": 4.0,
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        self.assertEqual(
            [marker.doc_item_id for marker in request.markers],
            ["修改003", "校对004", "修改005"],
        )

    def test_visual_merge_preserves_review_item_verbatim_status(self):
        source_item = RevisionReviewItem(
            item_id="修改01",
            kind="pointer_overlay",
            source_text="修改01 添加指针",
            verbatim_status="unverified_source_unavailable",
            execution_status="label_only_unresolved",
        )

        merged = _merge_visual_results_into_items(
            [source_item],
            [
                {
                    "item_id": "修改01",
                    "evidence": {"status": "pass"},
                    "validation": {"status": "pass"},
                }
            ],
        )

        self.assertEqual(merged[0].verbatim_status, "unverified_source_unavailable")
        self.assertEqual(merged[0].execution_status, "label_only_unresolved")

    def test_pause_merge_preserves_review_item_verbatim_status(self):
        source_item = RevisionReviewItem(
            item_id="校对02",
            kind="pause_delete",
            source_text="校对02 调整停顿",
            verbatim_status="unverified_source_unavailable",
            execution_status="label_only_unresolved",
        )

        merged = _merge_pause_results_into_items(
            [source_item],
            [{"item_id": "校对02", "duration": 0.5}],
        )

        self.assertEqual(merged[0].verbatim_status, "unverified_source_unavailable")
        self.assertEqual(merged[0].execution_status, "label_only_unresolved")

    def test_doc_request_merge_uses_doc_text_and_status_together(self):
        request_item = RevisionReviewItem(
            item_id="修改03",
            kind="spoken_delete",
            source_text="fallback detail",
            verbatim_status="unverified_source_unavailable",
        )
        source_item = RevisionReviewItem(
            item_id="修改03",
            kind="spoken_delete",
            source_text="source text",
            execution_status="label_only_unresolved",
        )

        merged = _merge_unique_review_items([request_item], [source_item])

        self.assertEqual(merged[0].source_text, "source text")
        self.assertEqual(merged[0].verbatim_status, "verified")
        self.assertEqual(merged[0].execution_status, "label_only_unresolved")

    def test_doc_request_merge_uses_request_text_and_status_when_doc_text_is_empty(self):
        request_item = RevisionReviewItem(
            item_id="修改04",
            kind="spoken_delete",
            source_text="fallback detail",
            verbatim_status="unverified_source_unavailable",
        )
        source_item = RevisionReviewItem(
            item_id="修改04",
            kind="spoken_delete",
            source_text="",
        )

        merged = _merge_unique_review_items([request_item], [source_item])

        self.assertEqual(merged[0].source_text, "fallback detail")
        self.assertEqual(merged[0].verbatim_status, "unverified_source_unavailable")

    def test_load_revision_request_preserves_visual_edit_assets(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
            },
            "edits": [
                {
                    "type": "pointer_overlay",
                    "source_kind": "pointer_overlay",
                    "doc_item_id": "校对05",
                    "start": 317.0,
                    "end": 319.0,
                    "label": "校对05 小手重做",
                    "detail": "删除原小手并重新添加",
                    "asset_paths": ["C:/media/screenshot.png", "C:/media/hand.png"],
                    "visual_plan": {
                        "segments": [
                            {
                                "role": "pointer_asset",
                                "asset_path": "C:/media/hand.png",
                                "track_name": "Pointer Overlay 校对05",
                                "source_start": 317.0,
                                "duration": 2.0,
                            }
                        ]
                    },
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        self.assertEqual(request.edits[0].doc_item_id, "校对05")
        self.assertEqual(request.edits[0].source_kind, "pointer_overlay")
        self.assertEqual(request.edits[0].asset_paths[-1], "C:/media/hand.png")
        self.assertEqual(
            request.edits[0].visual_plan["segments"][0]["track_name"], "Pointer Overlay 校对05"
        )

    def test_visual_segment_uses_video_asset_from_extension_or_type(self):
        self.assertTrue(_visual_segment_uses_video_asset({}, "C:/media/overlay.mp4"))
        self.assertTrue(
            _visual_segment_uses_video_asset({"asset_type": "video"}, "C:/media/overlay.png")
        )
        self.assertFalse(_visual_segment_uses_video_asset({}, "C:/media/overlay.png"))

    def test_visual_video_overlay_honors_explicit_volume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = os.path.join(tmpdir, "overlay.mp4")
            with open(overlay_path, "wb") as f:
                f.write(b"fixture")
            payload = {
                "project": {
                    "draft_name": "ReviewDraft",
                    "source_video": "C:/media/source.mp4",
                },
                "edits": [
                    {
                        "type": "visual_overlay",
                        "doc_item_id": "修改01",
                        "start": 10.0,
                        "end": 12.0,
                        "label": "修改01 replacement video",
                        "visual_plan": {
                            "segments": [
                                {
                                    "role": "replacement_video_keep_1",
                                    "asset_type": "video",
                                    "asset_path": overlay_path,
                                    "timeline_start": 10.0,
                                    "source_start": 10.0,
                                    "duration": 2.0,
                                    "volume": 0.0,
                                }
                            ]
                        },
                    }
                ],
            }
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            class FakeProject:
                def __init__(self):
                    self.segment = type(
                        "FakeSegment",
                        (),
                        {"segment_id": "segment-1", "material_id": "material-1", "volume": 1.0},
                    )()

                def add_media_safe(self, *args, **kwargs):
                    return self.segment

            project = FakeProject()
            results = _add_visual_overlay_segments(project, request, [], mock_media=True)

        self.assertEqual(project.segment.volume, 0.0)
        self.assertEqual(results[0]["segments"][0]["volume"], 0.0)

    def test_visual_video_overlay_volume_is_saved_to_draft_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = os.path.join(tmpdir, "overlay.mp4")
            self._write_video_fixture(overlay_path)
            payload = self._visual_video_payload(
                overlay_path,
                include_volume=True,
                volume=0.0,
            )
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            result = execute_revision_request(request, drafts_root=tmpdir, mock_media=True)
            with open(
                os.path.join(tmpdir, "ReviewDraft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as f:
                content = json.load(f)

        overlay_track = next(
            track for track in content["tracks"] if track["name"] == "Replacement Video"
        )
        self.assertEqual(overlay_track["segments"][0]["volume"], 0.0)
        self.assertEqual(result["visual_overlay_results"][0]["segments"][0]["volume"], 0.0)

    def test_visual_video_overlay_omitted_volume_keeps_default_in_saved_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = os.path.join(tmpdir, "overlay.mp4")
            self._write_video_fixture(overlay_path)
            payload = self._visual_video_payload(overlay_path, include_volume=False)
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            execute_revision_request(request, drafts_root=tmpdir, mock_media=True)
            with open(
                os.path.join(tmpdir, "ReviewDraft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as f:
                content = json.load(f)

        overlay_track = next(
            track for track in content["tracks"] if track["name"] == "Replacement Video"
        )
        self.assertEqual(overlay_track["segments"][0]["volume"], 1.0)

    def test_visual_video_overlay_rejects_invalid_explicit_volume(self):
        invalid_values = [
            ("null", None),
            ("invalid string", "loud"),
            ("negative", -0.1),
            ("nan", float("nan")),
            ("positive infinity", float("inf")),
            ("negative infinity", float("-inf")),
        ]
        for label, volume in invalid_values:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                overlay_path = os.path.join(tmpdir, "overlay.mp4")
                self._write_video_fixture(overlay_path)
                payload = self._visual_video_payload(
                    overlay_path,
                    include_volume=True,
                    volume=volume,
                )
                path = os.path.join(tmpdir, "request.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                request = load_revision_request(path)

                with self.assertRaisesRegex(
                    ValueError,
                    "volume must be a finite non-negative number",
                ):
                    execute_revision_request(request, drafts_root=tmpdir, mock_media=True)

    def test_invalid_visual_video_volume_does_not_replace_existing_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = os.path.join(tmpdir, "overlay.mp4")
            self._write_video_fixture(overlay_path)
            payload = self._visual_video_payload(
                overlay_path,
                include_volume=True,
                volume=None,
            )
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            draft_dir = os.path.join(tmpdir, "ReviewDraft")
            os.makedirs(draft_dir)
            content_path = os.path.join(draft_dir, "draft_content.json")
            meta_path = os.path.join(draft_dir, "draft_meta_info.json")
            original_content = b'{"sentinel":"keep-content"}'
            original_meta = b'{"sentinel":"keep-meta"}'
            with open(content_path, "wb") as f:
                f.write(original_content)
            with open(meta_path, "wb") as f:
                f.write(original_meta)

            with patch(
                "core.project_base.JyProjectBase._ensure_safe_overwrite_context",
                return_value=None,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "volume must be a finite non-negative number",
                ):
                    execute_revision_request(request, drafts_root=tmpdir, mock_media=True)

            with open(content_path, "rb") as f:
                saved_content = f.read()
            with open(meta_path, "rb") as f:
                saved_meta = f.read()

        self.assertEqual(saved_content, original_content)
        self.assertEqual(saved_meta, original_meta)

    def test_load_revision_request_uses_processed_audio_path_as_replacement_audio(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "processed_audio": {
                "output_wav": "C:/media/processed.wav",
                "validation_summary": "C:/media/summary.json",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 10.0,
                    "end": 12.0,
                    "label": "Remove phrase",
                }
            ],
            "preserve": {"replacement_audio_material": True},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        self.assertEqual(request.project.replacement_audio, "C:/media/processed.wav")
        self.assertEqual(request.processed_audio["validation_summary"], "C:/media/summary.json")

    def test_load_revision_request_cleans_replacement_glyph_review_labels(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 1.0,
                    "end": 2.0,
                    "label": "修改02 ????",
                    "detail": "删除一段话",
                    "doc_item_id": "修改02",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        self.assertEqual(request.edits[0].label, "修改02")

    def test_build_revision_summary_reports_preservation_contract(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
                "replacement_audio": "C:/media/replacement.mp3",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 0.0,
                    "end": 3.0,
                    "label": "Remove opener",
                    "detail": "Delete 0-3s title opener",
                },
                {
                    "type": "replace_audio",
                    "start": 197.0,
                    "end": 205.0,
                    "audio_path": "C:/media/replacement.mp3",
                    "label": "Replace narration",
                    "detail": "Use provided MP3 for the narration window",
                },
            ],
            "markers": [
                {
                    "label": "Animation check",
                    "start": 220.0,
                    "end": 221.0,
                    "detail": "Verify animation timing",
                }
            ],
            "preserve": {
                "source_video_material": True,
                "separated_audio_material": True,
                "replacement_audio_material": True,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        summary = build_revision_summary(request)

        self.assertEqual(summary["draft_name"], "ReviewDraft")
        self.assertEqual(summary["edit_count"], 2)
        self.assertEqual(summary["review_marker_count"], 3)
        self.assertEqual(summary["replacement_windows"], [[197.0, 205.0]])
        self.assertEqual(summary["delete_windows"], [[0.0, 3.0]])
        self.assertEqual(
            summary["required_materials"], ["replacement_audio", "source_audio", "source_video"]
        )
        self.assertTrue(summary["preservation"]["keep_cut_points"])
        self.assertTrue(summary["preservation"]["keep_review_markers_separate"])
        self.assertIn("video_track", summary["required_tracks"])
        self.assertIn("source_audio_track", summary["required_tracks"])
        self.assertIn("replacement_audio_track", summary["required_tracks"])

    def test_build_revision_summary_requires_full_track_replacement_audio(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
                "replacement_audio": "C:/media/processed.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 10.0,
                    "end": 12.0,
                    "label": "Remove phrase",
                }
            ],
            "preserve": {
                "source_video_material": True,
                "separated_audio_material": True,
                "replacement_audio_material": True,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        summary = build_revision_summary(request)

        self.assertTrue(summary["full_track_replacement_audio"])
        self.assertEqual(
            summary["required_materials"], ["replacement_audio", "source_audio", "source_video"]
        )
        self.assertIn("replacement_audio_track", summary["required_tracks"])

    def test_build_revision_summary_uses_one_marker_for_each_review_item(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 118.95,
                    "end": 120.0,
                    "label": "修改007 first cut",
                    "doc_item_id": "修改007",
                },
                {
                    "type": "delete",
                    "start": 120.0,
                    "end": 123.3,
                    "label": "修改007 second cut",
                    "doc_item_id": "修改007",
                },
            ],
            "review_items": [
                {
                    "id": "修改007",
                    "source_text": "01:59-02:00，删除“无产阶级必须”",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        summary = build_revision_summary(request)

        self.assertEqual(summary["review_marker_count"], 1)
        self.assertIn("review_marker_tracks", summary["required_tracks"])

    def test_build_revision_summary_requires_marker_track_for_source_item_without_action(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
            },
            "review_items": [
                {
                    "id": "校对009",
                    "source_text": "只提供了原始审阅文字",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        summary = build_revision_summary(request)

        self.assertEqual(summary["review_marker_count"], 1)
        self.assertIn("review_marker_tracks", summary["required_tracks"])

    def test_build_revision_summary_uses_latest_doc_items_for_marker_contract(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 10.0,
                    "end": 11.0,
                    "label": "修改01 request action",
                    "doc_item_id": "修改01",
                }
            ],
            "review_items": [
                {
                    "id": "修改01",
                    "source_text": "request 中的旧台账",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        empty_summary = build_revision_summary(request, doc_items=[])
        latest_summary = build_revision_summary(
            request,
            doc_items=[
                RevisionReviewItem("修改01", "review_only", "latest row 1"),
                RevisionReviewItem("校对02", "review_only", "latest row 2"),
            ],
        )

        self.assertEqual(empty_summary["review_marker_count"], 0)
        self.assertNotIn("review_marker_tracks", empty_summary["required_tracks"])
        self.assertEqual(latest_summary["review_marker_count"], 2)
        self.assertIn("review_marker_tracks", latest_summary["required_tracks"])

    def test_execute_revision_request_renders_one_verbatim_marker_and_returns_saved_receipt(self):
        source_text = "Canonical source sentence, preserved verbatim without an ID prefix."
        payload = {
            "project": {
                "draft_name": "VerbatimReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 10.0,
                    "end": 11.0,
                    "label": "Summary label for first cut",
                    "doc_item_id": "item-007",
                },
                {
                    "type": "delete",
                    "start": 12.0,
                    "end": 13.0,
                    "label": "Different summary label for second cut",
                    "doc_item_id": "item-007",
                },
            ],
            "review_items": [
                {
                    "id": "item-007",
                    "source_text": source_text,
                    "start": 10.0,
                    "end": 13.0,
                    "verbatim_status": "verified",
                    "evidence": {"execution_status": "label_only_unresolved"},
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            payload["processed_audio"] = self._write_full_candidate_reverse_asr(
                tmpdir,
                duration_seconds=12.0,
                rows=[
                    {
                        "id": "item-007",
                        "status": "pass",
                        "source_cut_windows": [[10.0, 11.0], [12.0, 13.0]],
                        "mapped_join_times": [10.0, 11.0],
                    }
                ],
            )
            payload["audio_delivery_plan"] = {
                "mode": "segmented",
                "forbid_full_length_segments": True,
                "segments": [
                    {
                        "id": "source-item-007-before",
                        "role": "source",
                        "asset_path": "C:/media/source.wav",
                        "track_name": "Narration - Source Segments",
                        "source_start": 0.0,
                        "timeline_start": 0.0,
                        "duration": 10.0,
                        "doc_item_id": "item-007",
                    },
                    {
                        "id": "source-item-007-middle",
                        "role": "source",
                        "asset_path": "C:/media/source.wav",
                        "track_name": "Narration - Source Segments",
                        "source_start": 11.0,
                        "timeline_start": 10.0,
                        "duration": 1.0,
                        "doc_item_id": "item-007",
                    },
                    {
                        "id": "source-item-007-after",
                        "role": "source",
                        "asset_path": "C:/media/source.wav",
                        "track_name": "Narration - Source Segments",
                        "source_start": 13.0,
                        "timeline_start": 11.0,
                        "duration": 1.0,
                        "doc_item_id": "item-007",
                    },
                ],
            }
            request = self._load_request_payload(payload)

            result = execute_revision_request(request, drafts_root=tmpdir, mock_media=True)
            with open(
                os.path.join(tmpdir, "VerbatimReviewDraft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as f:
                content = json.load(f)

        rendered_texts = [
            json.loads(material["content"])["text"] for material in content["materials"]["texts"]
        ]
        marker_tracks = [
            track for track in content["tracks"] if track["name"].startswith("Review Marker ")
        ]
        marker_segments = [segment for track in marker_tracks for segment in track["segments"]]
        self.assertEqual(rendered_texts, [source_text])
        self.assertEqual(len(marker_segments), 1)
        self.assertEqual(result["review_marker_count"], 1)
        self.assertEqual(len(result["review_marker_receipts"]), 1)
        receipt = result["review_marker_receipts"][0]
        self.assertEqual(
            set(receipt),
            {
                "item_id",
                "source_text",
                "verbatim_status",
                "execution_status",
                "segment_id",
                "material_id",
                "track_name",
                "start_time",
                "duration",
            },
        )
        self.assertEqual(receipt["item_id"], "item-007")
        self.assertEqual(receipt["source_text"], source_text)
        self.assertEqual(receipt["verbatim_status"], "verified")
        self.assertEqual(receipt["execution_status"], "label_only_unresolved")
        self.assertEqual(receipt["segment_id"], marker_segments[0]["id"])
        self.assertEqual(receipt["material_id"], marker_segments[0]["material_id"])
        self.assertEqual(receipt["track_name"], marker_tracks[0]["name"])
        json.dumps(result["review_marker_receipts"])

    def test_execute_revision_request_rejects_mismatched_marker_in_active_timeline(self):
        source_text = "Canonical active-timeline marker text."
        payload = {
            "project": {
                "draft_name": "ActiveTimelineMarkerDraft",
                "source_video": "C:/media/source.mp4",
            },
            "review_items": [
                {
                    "id": "item-active",
                    "source_text": source_text,
                    "start": 5.0,
                    "end": 6.0,
                    "execution_required": False,
                    "verbatim_status": "verified",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)
            real_import = __import__(
                "utils.revision_runner", fromlist=["_import_runtime_components"]
            )
            _draft, _marker_item, _audio, _video, real_jy_project = (
                real_import._import_runtime_components()
            )
            real_save = real_jy_project.save

            def save_with_mismatched_active_timeline(project, *args, **kwargs):
                save_result = real_save(project, *args, **kwargs)
                draft_path = save_result["draft_path"]
                with open(
                    os.path.join(draft_path, "draft_content.json"),
                    "r",
                    encoding="utf-8",
                ) as f:
                    active_content = json.load(f)
                active_content["materials"]["texts"][0]["content"] = json.dumps(
                    {"text": "Summarized active marker"}, ensure_ascii=False
                )
                active_id = "active-marker-mismatch"
                active_dir = os.path.join(draft_path, "Timelines", active_id)
                os.makedirs(active_dir, exist_ok=True)
                with open(
                    os.path.join(active_dir, "draft_content.json"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    json.dump(active_content, f, ensure_ascii=False)
                with open(
                    os.path.join(draft_path, "timeline_layout.json"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    json.dump({"activeTimeline": active_id}, f)
                return save_result

            with patch.object(real_jy_project, "save", new=save_with_mismatched_active_timeline):
                with self.assertRaisesRegex(
                    RuntimeError, "active_timeline:active-marker-mismatch.*verbatim"
                ):
                    execute_revision_request(request, drafts_root=tmpdir, mock_media=True)

    def test_execute_revision_request_uses_external_doc_items_for_markers_and_summary(self):
        payload = {
            "project": {
                "draft_name": "ExternalLedgerDraft",
                "source_video": "C:/media/source.mp4",
            },
            "review_items": [
                {
                    "id": "old-item",
                    "source_text": "Stale request ledger row",
                    "start": 1.0,
                    "end": 2.0,
                }
            ],
        }
        latest_items = [
            RevisionReviewItem(
                "latest-1",
                "review_only",
                "Latest canonical row one",
                start=5.0,
                end=6.0,
                execution_required=False,
            ),
            RevisionReviewItem(
                "latest-2",
                "review_only",
                "Latest canonical row two",
                start=7.0,
                end=8.0,
                execution_required=False,
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            result = execute_revision_request(
                request,
                drafts_root=tmpdir,
                mock_media=True,
                doc_items=latest_items,
            )
            with open(
                os.path.join(tmpdir, "ExternalLedgerDraft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as f:
                content = json.load(f)

        self.assertEqual(result["review_marker_count"], 2)
        self.assertEqual(result["review_item_count"], 2)
        self.assertEqual(
            result["acceptance_validation"]["metrics"]["review_item_count"],
            2,
        )
        self.assertEqual(
            [receipt["item_id"] for receipt in result["review_marker_receipts"]],
            ["latest-1", "latest-2"],
        )
        self.assertEqual(
            [json.loads(material["content"])["text"] for material in content["materials"]["texts"]],
            ["Latest canonical row one", "Latest canonical row two"],
        )

    def test_execute_revision_request_preserves_request_audio_evidence_with_doc_items(self):
        payload = {
            "project": {
                "draft_name": "ExternalLedgerAudioEvidenceDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "doc_item_id": "item-audio",
                    "start": 1.0,
                    "end": 2.0,
                    "label": "item-audio delete phrase",
                }
            ],
            "review_items": [
                {
                    "id": "item-audio",
                    "source_text": "Request row with execution evidence",
                    "kind": "spoken_delete",
                    "execution_required": True,
                    "evidence": {
                        "status": "executed",
                        "executed": True,
                        "cut_window": [1.0, 2.0],
                        "strategy": "hybrid",
                        "delete": "removed phrase",
                        "must_keep": [],
                    },
                    "validation": {"status": "pass_adjudicated"},
                }
            ],
            "acceptance": {
                "expected_review_item_count": 1,
                "expected_review_item_ids": ["item-audio"],
                "require_audio_validation": True,
            },
            "audio_delivery_plan": {
                "mode": "segmented",
                "forbid_full_length_segments": True,
                "segments": [
                    {
                        "id": "source-item-audio",
                        "role": "source",
                        "asset_path": "C:/media/source.wav",
                        "track_name": "Narration - Source Segments",
                        "source_start": 0.0,
                        "timeline_start": 0.0,
                        "duration": 1.0,
                        "doc_item_id": "item-audio",
                    }
                ],
            },
        }
        latest_items = [
            RevisionReviewItem(
                "item-audio",
                "spoken_delete",
                "Canonical source ledger row",
                start=1.0,
                end=2.0,
                execution_required=True,
                evidence={"status": "executed", "cut_window": [1.0, 2.0]},
                validation={"status": "review"},
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            payload["processed_audio"] = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[
                    {
                        "id": "item-audio",
                        "status": "pass_adjudicated",
                        "delete": "removed phrase",
                        "must_keep": [],
                    }
                ],
            )
            request = self._load_request_payload(payload)

            result = execute_revision_request(
                request,
                drafts_root=tmpdir,
                mock_media=True,
                strict=True,
                doc_items=latest_items,
            )

        self.assertTrue(
            result["acceptance_validation"]["ok"],
            result["acceptance_validation"]["errors"],
        )
        self.assertEqual(
            result["acceptance_validation"]["metrics"]["audio_unresolved_validation"],
            [],
        )

    def test_execute_revision_request_external_empty_ledger_controls_saved_validation(self):
        payload = {
            "project": {
                "draft_name": "EmptyExternalLedgerDraft",
                "source_video": "C:/media/source.mp4",
            },
            "review_items": [
                {
                    "id": "stale-item",
                    "source_text": "Stale request ledger row",
                    "start": 1.0,
                    "end": 2.0,
                    "execution_required": False,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            result = execute_revision_request(
                request,
                drafts_root=tmpdir,
                mock_media=True,
                doc_items=[],
            )

        self.assertEqual(result["review_marker_count"], 0)
        self.assertEqual(result["review_item_count"], 0)
        self.assertEqual(result["review_marker_receipts"], [])
        self.assertTrue(result["validation"]["ok"], result["validation"]["errors"])
        self.assertEqual(
            result["validation"]["metrics"]["expected_review_marker_count"],
            0,
        )
        self.assertEqual(result["validation"]["metrics"]["review_marker_track_count"], 0)

    def test_execute_revision_request_explicit_empty_ledger_suppresses_legacy_markers(self):
        payload = {
            "project": {
                "draft_name": "EmptyLedgerLegacyMarkerDraft",
                "source_video": "C:/media/source.mp4",
            },
            "markers": [
                {
                    "label": "Legacy marker must be suppressed",
                    "start": 10.0,
                    "end": 11.0,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            result = execute_revision_request(
                request,
                drafts_root=tmpdir,
                mock_media=True,
                doc_items=[],
            )
            with open(
                os.path.join(tmpdir, "EmptyLedgerLegacyMarkerDraft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as f:
                content = json.load(f)

        marker_tracks = [
            track for track in content["tracks"] if track["name"].startswith("Review Marker ")
        ]
        self.assertEqual(result["review_marker_count"], 0)
        self.assertEqual(result["review_item_count"], 0)
        self.assertEqual(result["review_marker_receipts"], [])
        self.assertEqual(marker_tracks, [])
        self.assertEqual(
            result["validation"]["metrics"]["expected_review_marker_count"],
            0,
        )
        self.assertEqual(result["validation"]["metrics"]["review_marker_segment_count"], 0)

    def test_execute_revision_request_expands_seven_overlapping_markers_to_distinct_tracks(self):
        payload = {
            "project": {
                "draft_name": "OverlappingMarkerDraft",
                "source_video": "C:/media/source.mp4",
            },
            "markers": [
                {
                    "label": f"overlapping marker {index}",
                    "start": 10.0,
                    "end": 12.0,
                }
                for index in range(1, 8)
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            result = execute_revision_request(request, drafts_root=tmpdir, mock_media=True)
            with open(
                os.path.join(tmpdir, "OverlappingMarkerDraft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as f:
                content = json.load(f)

        marker_tracks = [
            track for track in content["tracks"] if track["name"].startswith("Review Marker ")
        ]
        self.assertEqual(result["review_marker_count"], 7)
        self.assertEqual(
            [track["name"] for track in marker_tracks],
            [f"Review Marker {index}" for index in range(1, 8)],
        )
        for track in marker_tracks:
            ordered = sorted(
                track["segments"], key=lambda segment: segment["target_timerange"]["start"]
            )
            for previous, current in zip(ordered, ordered[1:]):
                previous_end = (
                    previous["target_timerange"]["start"] + previous["target_timerange"]["duration"]
                )
                self.assertLessEqual(previous_end, current["target_timerange"]["start"])

    def test_execute_revision_request_saves_42_long_marker_texts_verbatim(self):
        source_text = "x" * 70
        payload = {
            "project": {
                "draft_name": "DenseMarkerDraft",
                "source_video": "C:/media/source.mp4",
            },
            "markers": [
                {
                    "label": source_text,
                    "start": 10.0,
                    "end": 12.0,
                }
                for _ in range(42)
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            result = execute_revision_request(request, drafts_root=tmpdir, mock_media=True)
            with open(
                os.path.join(tmpdir, "DenseMarkerDraft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as f:
                content = json.load(f)

        saved_texts = [
            json.loads(material["content"])["text"] for material in content["materials"]["texts"]
        ]
        self.assertEqual(result["review_marker_count"], 42)
        self.assertEqual(saved_texts, [source_text] * 42)

    def test_execute_revision_request_builds_reviewable_draft(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
                "replacement_audio": "C:/media/replacement.mp3",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 0.0,
                    "end": 3.0,
                    "label": "Remove opener",
                    "detail": "Delete 0-3s title opener",
                },
                {
                    "type": "replace_audio",
                    "start": 197.0,
                    "end": 205.0,
                    "audio_path": "C:/media/replacement.mp3",
                    "label": "Replace narration",
                    "detail": "Use provided MP3 for the narration window",
                },
            ],
            "markers": [
                {
                    "label": "Animation check",
                    "start": 220.0,
                    "end": 221.0,
                    "detail": "Verify animation timing",
                }
            ],
            "preserve": {
                "source_video_material": True,
                "separated_audio_material": True,
                "replacement_audio_material": True,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            payload["processed_audio"] = self._write_full_candidate_reverse_asr(tmpdir)
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            result = execute_revision_request(request, drafts_root=tmpdir, mock_media=True)
            with open(
                os.path.join(tmpdir, "ReviewDraft", "draft_content.json"), "r", encoding="utf-8"
            ) as f:
                content = json.load(f)
            with open(
                os.path.join(tmpdir, "ReviewDraft", "draft_meta_info.json"), "r", encoding="utf-8"
            ) as f:
                meta = json.load(f)

        self.assertEqual(result["draft_name"], "ReviewDraft")
        self.assertEqual(result["requested_draft_name"], "ReviewDraft")
        self.assertEqual(result["write_mode"], "requested_draft")
        self.assertEqual(result["edit_count"], 2)
        self.assertTrue(result["preservation"]["keep_cut_points"])
        self.assertEqual(
            result["tracks"],
            ["Original Video", "Separated Source Audio", "Replacement Audio"],
        )
        self.assertEqual(result["review_marker_count"], 3)
        self.assertEqual(len(result["review_marker_receipts"]), 3)
        self.assertEqual(
            [receipt["source_text"] for receipt in result["review_marker_receipts"]],
            ["Remove opener", "Replace narration", "Animation check"],
        )
        self.assertEqual(
            result["required_materials"],
            ["replacement_audio", "source_audio", "source_video"],
        )
        self.assertTrue(result["validation"]["ok"])
        self.assertGreaterEqual(
            result["validation"]["metrics"]["video_segment_count"],
            3,
        )
        self.assertEqual(len(content["materials"]["videos"]), 1)
        self.assertEqual(len(content["materials"]["audios"]), 2)
        local_materials = next(
            item["value"] for item in meta["draft_materials"] if item["type"] == 0
        )
        self.assertEqual(
            [item["metetype"] for item in local_materials], ["video", "music", "music"]
        )
        original_video_track = next(
            track for track in content["tracks"] if track["name"] == "Original Video"
        )
        self.assertEqual(len(original_video_track["segments"]), 3)
        first_segment = original_video_track["segments"][0]
        self.assertEqual(first_segment["target_timerange"]["start"], 0)
        self.assertEqual(first_segment["source_timerange"]["start"], 3000000)
        source_audio_track = next(
            track for track in content["tracks"] if track["name"] == "Separated Source Audio"
        )
        replacement_audio_track = next(
            track for track in content["tracks"] if track["name"] == "Replacement Audio"
        )
        self.assertEqual(len(source_audio_track["segments"]), 3)
        self.assertEqual(len(replacement_audio_track["segments"]), 1)
        replacement_segment = replacement_audio_track["segments"][0]
        self.assertEqual(replacement_segment["target_timerange"]["start"], 194000000)
        review_marker_track = next(
            track for track in content["tracks"] if track["name"] == "Review Marker 1"
        )
        self.assertEqual(
            [segment["target_timerange"]["start"] for segment in review_marker_track["segments"]],
            [0, 194000000, 217000000],
        )

    def test_execute_revision_request_adds_full_track_replacement_audio(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
                "replacement_audio": "C:/media/processed.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 10.0,
                    "end": 12.0,
                    "label": "Remove phrase",
                }
            ],
            "markers": [],
            "preserve": {
                "source_video_material": True,
                "separated_audio_material": True,
                "replacement_audio_material": True,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            payload["processed_audio"] = self._write_full_candidate_reverse_asr(tmpdir)
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            result = execute_revision_request(request, drafts_root=tmpdir, mock_media=True)
            with open(
                os.path.join(tmpdir, "ReviewDraft", "draft_content.json"), "r", encoding="utf-8"
            ) as f:
                content = json.load(f)

        self.assertTrue(result["full_track_replacement_audio"])
        self.assertTrue(result["validation"]["ok"], result["validation"]["errors"])
        self.assertEqual(len(content["materials"]["audios"]), 2)
        source_audio_track = next(
            track for track in content["tracks"] if track["name"] == "Separated Source Audio"
        )
        replacement_audio_track = next(
            track for track in content["tracks"] if track["name"] == "Replacement Audio"
        )
        self.assertEqual(len(replacement_audio_track["segments"]), 1)
        self.assertTrue(
            all(segment.get("volume", 1.0) == 0.0 for segment in source_audio_track["segments"])
        )

    def test_execute_revision_request_writes_visual_overlay_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hand_path = os.path.join(tmpdir, "hand.png")
            self._write_png_fixture(hand_path)
            payload = {
                "project": {
                    "draft_name": "ReviewDraft",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                },
                "edits": [
                    {
                        "type": "delete",
                        "start": 10.0,
                        "end": 12.0,
                        "label": "修改01 删除",
                    },
                    {
                        "type": "visual_overlay",
                        "source_kind": "visual_overlay",
                        "doc_item_id": "校对05",
                        "start": 317.0,
                        "end": 319.0,
                        "label": "校对05 小手重做",
                        "detail": "删除原小手并重新添加",
                        "asset_paths": [hand_path],
                        "visual_plan": {
                            "segments": [
                                {
                                    "role": "pointer_asset",
                                    "asset_path": hand_path,
                                    "track_name": "Pointer Overlay 校对05",
                                    "source_start": 317.0,
                                    "duration": 2.0,
                                    "scale_x": 0.2,
                                    "scale_y": 0.2,
                                }
                            ]
                        },
                    },
                ],
                "review_items": [
                    {
                        "id": "校对05",
                        "kind": "visual_overlay",
                        "source_text": "05:17-05:19 删除原小手并重新添加",
                        "execution_required": True,
                    }
                ],
                "acceptance": {
                    "expected_review_item_count": 1,
                    "expected_review_item_ids": ["校对05"],
                    "require_review_items": True,
                    "require_execution_evidence": True,
                    "require_visual_evidence": True,
                },
            }
            payload["review_items"][0]["evidence"] = {
                "review_timestamp_role": "search_hint",
                "boundary_control": "visual",
                "lifecycle_mode": "visual_only_pointer",
                "duration_rule": "visual frame scan around the review hint",
                "source_pointer_window": {"start": 317.0, "end": 319.0},
                "speech_anchor": {
                    "status": "visual_only",
                    "reason": "fixture pointer timing is visual-only",
                },
                "tail_scan": {
                    "status": "pass",
                    "scan_end": 319.2,
                    "last_pointer_visible": 319.0,
                    "pointer_absent_after": 319.04,
                },
            }
            payload["processed_audio"] = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[{"id": "legacy-delete", "status": "pass"}],
            )
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            result = execute_revision_request(
                request, drafts_root=tmpdir, mock_media=True, strict=True
            )
            with open(
                os.path.join(tmpdir, "ReviewDraft", "draft_content.json"), "r", encoding="utf-8"
            ) as f:
                content = json.load(f)

        self.assertTrue(
            result["acceptance_validation"]["ok"], result["acceptance_validation"]["errors"]
        )
        self.assertEqual(len(result["visual_overlay_results"]), 1)
        evidence = result["visual_overlay_results"][0]["evidence"]
        self.assertTrue(evidence["executed"])
        self.assertEqual(evidence["overlay_track"], "Pointer Overlay 校对05")
        self.assertTrue(evidence["overlay_segment"])
        overlay_track = next(
            track for track in content["tracks"] if track["name"] == "Pointer Overlay 校对05"
        )
        self.assertEqual(overlay_track["type"], "video")
        self.assertEqual(len(overlay_track["segments"]), 1)
        self.assertEqual(overlay_track["segments"][0]["target_timerange"]["start"], 315000000)

    def test_execute_revision_request_cleans_replacement_glyph_visual_track_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            asset_path = os.path.join(tmpdir, "overlay.png")
            self._write_png_fixture(asset_path)
            payload = {
                "project": {
                    "draft_name": "ReviewDraft",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                },
                "edits": [
                    {
                        "type": "visual_overlay",
                        "start": 10.0,
                        "end": 12.0,
                        "label": "校对05 ??/?????",
                        "doc_item_id": "校对05",
                        "source_kind": "visual_overlay",
                        "asset_paths": [asset_path],
                        "visual_plan": {
                            "segments": [
                                {
                                    "asset_path": asset_path,
                                    "track_name": "Visual Overlay ??05 hand",
                                    "source_start": 10.0,
                                    "duration": 1.0,
                                }
                            ]
                        },
                    }
                ],
                "preserve": {
                    "source_video_material": True,
                    "separated_audio_material": True,
                    "replacement_audio_material": False,
                    "keep_cut_points": True,
                    "keep_review_markers_separate": True,
                },
                "review_items": [
                    {
                        "id": "校对05",
                        "kind": "visual_overlay",
                        "source_text": "校对05 小手重做",
                        "execution_required": True,
                    }
                ],
                "acceptance": {
                    "expected_review_item_count": 1,
                    "expected_review_item_ids": ["校对05"],
                    "require_review_items": True,
                    "require_visual_evidence": True,
                },
            }
            payload["review_items"][0]["evidence"] = {
                "review_timestamp_role": "search_hint",
                "boundary_control": "visual",
                "lifecycle_mode": "visual_only_pointer",
                "duration_rule": "visual frame scan around the review hint",
                "source_pointer_window": {"start": 10.0, "end": 11.0},
                "speech_anchor": {
                    "status": "visual_only",
                    "reason": "fixture pointer timing is visual-only",
                },
                "tail_scan": {
                    "status": "pass",
                    "scan_end": 11.2,
                    "last_pointer_visible": 11.0,
                    "pointer_absent_after": 11.04,
                },
            }
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            result = execute_revision_request(
                request, drafts_root=tmpdir, mock_media=True, strict=True
            )
            with open(
                os.path.join(tmpdir, "ReviewDraft", "draft_content.json"), "r", encoding="utf-8"
            ) as f:
                content = json.load(f)

        self.assertTrue(result["validation"]["ok"], result["validation"]["errors"])
        self.assertTrue(
            any(track["name"].startswith("Visual Overlay check05") for track in content["tracks"])
        )
        self.assertFalse(any("??" in track["name"] for track in content["tracks"]))

    def test_validate_saved_revision_draft_rejects_flattened_preview_shell(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
                "replacement_audio": "C:/media/replacement.mp3",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 0.0,
                    "end": 3.0,
                    "label": "Remove opener",
                },
                {
                    "type": "replace_audio",
                    "start": 197.0,
                    "end": 205.0,
                    "audio_path": "C:/media/replacement.mp3",
                    "label": "Replace narration",
                },
            ],
            "markers": [{"label": "Animation check", "start": 220.0, "end": 221.0}],
            "preserve": {
                "source_video_material": True,
                "separated_audio_material": True,
                "replacement_audio_material": True,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        flattened_content = {
            "duration": 300000000,
            "tracks": [
                {
                    "name": "Final Video",
                    "type": "video",
                    "segments": [
                        {
                            "target_timerange": {"start": 0, "duration": 300000000},
                        }
                    ],
                },
                {
                    "name": "Final Audio",
                    "type": "audio",
                    "segments": [
                        {
                            "target_timerange": {"start": 0, "duration": 300000000},
                        }
                    ],
                },
            ],
            "materials": {
                "videos": [{"path": "C:/media/final_preview.mp4"}],
                "audios": [{"path": "C:/media/final_preview.wav"}],
                "texts": [],
            },
        }

        validation = validate_saved_revision_draft(
            request, flattened_content, draft_name="ReviewDraft"
        )

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("Final Video/Final Audio" in message for message in validation["errors"])
        )
        self.assertTrue(
            any(
                "one full-length segment" in message or "collapsed" in message
                for message in validation["errors"]
            )
        )

    def test_validate_saved_revision_draft_rejects_replacement_glyph_text(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 1.0,
                    "end": 2.0,
                    "label": "修改02",
                }
            ],
            "preserve": {
                "source_video_material": False,
                "separated_audio_material": False,
                "replacement_audio_material": False,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        content = {
            "duration": 5_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 1_000_000}}],
                },
                {
                    "name": "Review Marker 1",
                    "type": "text",
                    "segments": [{"target_timerange": {"start": 0, "duration": 800_000}}],
                },
            ],
            "materials": {
                "videos": [],
                "audios": [],
                "texts": [
                    {
                        "id": "text-1",
                        "content": json.dumps({"text": "修改02 ????"}, ensure_ascii=False),
                    }
                ],
            },
        }

        validation = validate_saved_revision_draft(request, content, draft_name="ReviewDraft")

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("replacement-glyph question marks" in msg for msg in validation["errors"])
        )
        self.assertEqual(validation["metrics"]["review_marker_track_count"], 1)

    def test_validate_saved_revision_draft_enforces_receipted_marker_text(self):
        source_text = "The full source-ledger sentence must be saved verbatim."
        payload = {
            "project": {"draft_name": "ReviewDraft", "source_video": "C:/media/source.mp4"},
            "review_items": [
                {
                    "id": "item-1",
                    "source_text": source_text,
                    "start": 1.0,
                    "end": 2.0,
                    "execution_required": False,
                }
            ],
            "preserve": {
                "source_video_material": False,
                "separated_audio_material": False,
                "replacement_audio_material": False,
                "keep_cut_points": False,
                "keep_review_markers_separate": True,
            },
        }
        content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Review Marker 1",
                    "type": "text",
                    "segments": [
                        {
                            "id": "segment-1",
                            "material_id": "material-1",
                            "target_timerange": {"start": 1_000_000, "duration": 1_000_000},
                        }
                    ],
                },
            ],
            "materials": {
                "texts": [
                    {
                        "id": "material-1",
                        "content": json.dumps({"text": "Short summary"}),
                    }
                ]
            },
        }
        receipts = [
            {
                "item_id": "item-1",
                "source_text": source_text,
                "verbatim_status": "verified",
                "segment_id": "segment-1",
                "material_id": "material-1",
                "track_name": "Review Marker 1",
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            request = load_revision_request(path)

        validation = validate_saved_revision_draft(request, content, marker_receipts=receipts)

        self.assertFalse(validation["ok"])
        self.assertTrue(any("verbatim" in error.lower() for error in validation["errors"]))

    def test_validate_saved_revision_draft_allows_exact_literal_question_marks(self):
        source_text = "Literal ???? belongs to the review source."
        payload = {
            "project": {"draft_name": "ReviewDraft", "source_video": "C:/media/source.mp4"},
            "review_items": [
                {
                    "id": "item-1",
                    "source_text": source_text,
                    "start": 1.0,
                    "end": 2.0,
                    "execution_required": False,
                }
            ],
            "preserve": {
                "source_video_material": False,
                "separated_audio_material": False,
                "replacement_audio_material": False,
                "keep_cut_points": False,
                "keep_review_markers_separate": True,
            },
        }
        content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Review Marker 1",
                    "type": "text",
                    "segments": [
                        {
                            "id": "segment-1",
                            "material_id": "material-1",
                            "target_timerange": {"start": 1_000_000, "duration": 1_000_000},
                        }
                    ],
                },
            ],
            "materials": {
                "texts": [
                    {
                        "id": "material-1",
                        "content": json.dumps({"text": source_text}),
                    }
                ]
            },
        }
        receipts = [
            {
                "item_id": "item-1",
                "source_text": source_text,
                "verbatim_status": "verified",
                "segment_id": "segment-1",
                "material_id": "material-1",
                "track_name": "Review Marker 1",
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            request = load_revision_request(path)

        validation = validate_saved_revision_draft(request, content, marker_receipts=receipts)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(
            validation["metrics"]["replacement_glyph_evidence"]["bad_text_material_count"],
            0,
        )

    def test_strict_saved_revision_draft_rejects_dynamic_marker_below_top_band(self):
        source_text = "Keep this canonical review text at the top."
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "ReviewDraft",
                    "source_video": "C:/media/source.mp4",
                },
                "review_items": [
                    {
                        "id": "item-1",
                        "source_text": source_text,
                        "start": 1.0,
                        "end": 2.0,
                        "execution_required": False,
                    }
                ],
                "preserve": {
                    "source_video_material": False,
                    "separated_audio_material": False,
                    "replacement_audio_material": False,
                    "keep_cut_points": False,
                    "keep_review_markers_separate": True,
                },
            }
        )
        content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Review Marker 1",
                    "type": "text",
                    "segments": [
                        {
                            "id": "segment-1",
                            "material_id": "material-1",
                            "clip": {"transform": {"y": 0.0}},
                            "target_timerange": {
                                "start": 1_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {
                "texts": [
                    {
                        "id": "material-1",
                        "content": json.dumps({"text": source_text}),
                    }
                ]
            },
        }
        receipts = [
            {
                "item_id": "item-1",
                "source_text": source_text,
                "verbatim_status": "verified",
                "segment_id": "segment-1",
                "material_id": "material-1",
                "track_name": "Review Marker 1",
            }
        ]

        validation = validate_saved_revision_draft(
            request,
            content,
            marker_receipts=receipts,
            strict=True,
        )

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                "review_marker_top_layout.y_out_of_band" in error for error in validation["errors"]
            ),
            validation["errors"],
        )

    def test_require_final_acceptance_promotes_saved_marker_layout_validation(self):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "PromotedStrictMarkerLayout",
                    "source_video": "C:/media/source.mp4",
                },
                "review_items": [],
                "acceptance": {"require_final_acceptance": True},
                "preserve": {
                    "source_video_material": False,
                    "separated_audio_material": False,
                    "replacement_audio_material": False,
                    "keep_cut_points": False,
                    "keep_review_markers_separate": False,
                },
            }
        )
        content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Review Marker 1",
                    "type": "text",
                    "segments": [
                        {
                            "id": "centered-marker",
                            "clip": {"transform": {"y": 0.0}},
                            "target_timerange": {
                                "start": 1_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {"videos": [], "audios": [], "texts": []},
        }

        validation = validate_saved_revision_draft(request, content, strict=False)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                "review_marker_top_layout.y_out_of_band" in error for error in validation["errors"]
            ),
            validation["errors"],
        )

    def test_unverified_literal_question_marks_with_timing_warning_remain_allowed(self):
        source_text = "Literal ???? is exact even when marker timing is unverified."
        payload = {
            "project": {"draft_name": "ReviewDraft", "source_video": "C:/media/source.mp4"},
            "review_items": [
                {
                    "id": "item-1",
                    "source_text": source_text,
                    "start": 1.0,
                    "end": 2.0,
                    "execution_required": False,
                    "verbatim_status": "unverified_timing_unavailable",
                }
            ],
            "preserve": {
                "source_video_material": False,
                "separated_audio_material": False,
                "replacement_audio_material": False,
                "keep_cut_points": False,
                "keep_review_markers_separate": True,
            },
        }
        content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Review Marker 1",
                    "type": "text",
                    "segments": [
                        {
                            "id": "segment-1",
                            "material_id": "material-1",
                            "target_timerange": {"start": 1_250_000, "duration": 1_000_000},
                        }
                    ],
                },
            ],
            "materials": {
                "texts": [
                    {
                        "id": "material-1",
                        "content": json.dumps({"text": source_text}),
                    }
                ]
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            request = load_revision_request(path)

        validation = validate_saved_revision_draft(request, content)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertTrue(any("timeline start" in warning for warning in validation["warnings"]))
        marker_metrics = validation["metrics"]["marker_validation"]
        self.assertEqual(marker_metrics["exact_match_count"], 0)
        self.assertEqual(marker_metrics["verbatim_match_count"], 1)
        self.assertEqual(marker_metrics["exact_marker_material_ids"], ["material-1"])
        self.assertEqual(marker_metrics["exact_marker_text_values"], [source_text])
        self.assertEqual(
            validation["metrics"]["replacement_glyph_evidence"]["bad_text_material_count"],
            0,
        )

    def test_validate_saved_revision_draft_rejects_extra_marker_for_empty_source_ledger(self):
        payload = {
            "project": {"draft_name": "ReviewDraft", "source_video": "C:/media/source.mp4"},
            "markers": [{"label": "legacy marker", "start": 1.0, "end": 2.0}],
            "preserve": {
                "source_video_material": False,
                "separated_audio_material": False,
                "replacement_audio_material": False,
                "keep_cut_points": False,
                "keep_review_markers_separate": True,
            },
        }
        content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Review Marker 1",
                    "type": "text",
                    "segments": [
                        {
                            "id": "segment-extra",
                            "material_id": "material-extra",
                            "target_timerange": {"start": 1_000_000, "duration": 1_000_000},
                        }
                    ],
                },
            ],
            "materials": {
                "texts": [
                    {
                        "id": "material-extra",
                        "content": json.dumps({"text": "legacy marker"}),
                    }
                ]
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            request = load_revision_request(path)

        validation = validate_saved_revision_draft(
            request, content, doc_items=[], marker_receipts=[]
        )

        self.assertFalse(validation["ok"])
        self.assertTrue(any("expected exactly 0" in error for error in validation["errors"]))

    def test_validate_saved_revision_draft_maps_marker_start_after_delete_and_pause(self):
        source_text = "Marker source text after an earlier delete and inserted pause."
        payload = {
            "project": {"draft_name": "ReviewDraft", "source_video": "C:/media/source.mp4"},
            "edits": [
                {
                    "type": "delete",
                    "start": 1.0,
                    "end": 3.0,
                    "label": "Delete two seconds before the marker",
                }
            ],
            "review_items": [
                {
                    "id": "item-marker",
                    "source_text": source_text,
                    "start": 10.0,
                    "end": 11.0,
                    "execution_required": False,
                    "verbatim_status": "verified",
                }
            ],
            "pause_adjustments": [
                {
                    "item_id": "pause-before-marker",
                    "source_time": 4.0,
                    "duration": 1.0,
                    "frame_path": "C:/media/pause-frame.png",
                }
            ],
            "preserve": {
                "source_video_material": False,
                "separated_audio_material": False,
                "replacement_audio_material": False,
                "keep_cut_points": False,
                "keep_review_markers_separate": True,
            },
        }
        content = {
            "duration": 10_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}],
                },
                {
                    "name": "Review Marker 1",
                    "type": "text",
                    "segments": [
                        {
                            "id": "segment-mapped",
                            "material_id": "material-mapped",
                            "target_timerange": {"start": 9_000_000, "duration": 1_000_000},
                        }
                    ],
                },
            ],
            "materials": {
                "texts": [
                    {
                        "id": "material-mapped",
                        "content": json.dumps({"text": source_text}),
                    }
                ]
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            request = load_revision_request(path)

        validation = validate_saved_revision_draft(request, content)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(
            validation["metrics"]["marker_validation"]["saved_markers"][0]["start"],
            9_000_000,
        )

    def test_acceptance_profile_always_enables_low_cost_gates(self):
        request = self._conditional_request(kind="bgm_replace", op_type="replace_audio")

        profile = self._derive_acceptance_profile(request)

        self.assertEqual(
            profile["enabled_gates"][:6],
            [
                "source_coverage",
                "execution_evidence",
                "draft_exists",
                "editable_structure",
                "verbatim_markers",
                "audio_delivery",
            ],
        )

    def test_pure_visual_acceptance_skips_nonexistent_processed_audio_summary(self):
        request = self._conditional_request(
            kind="visual_overlay",
            op_type="visual_overlay",
            acceptance={"require_visual_evidence": False},
            evidence={
                "operation": "visual_overlay",
                "track_name": "Visual Overlay item01",
                "segment_id": "visual-overlay-item01",
            },
            processed_audio={"validation_summary": "Z:/missing/reverse-summary.json"},
        )
        content = {
            "duration": 10_000_000,
            "tracks": [
                {
                    "type": "video",
                    "name": "Original Video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}],
                },
                {
                    "type": "video",
                    "name": "Visual Overlay item01",
                    "segments": [
                        {
                            "id": "visual-overlay-item01",
                            "target_timerange": {
                                "start": 1_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {},
        }

        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertIn("visual", validation["metrics"]["enabled_gates"])
        self.assertIn("audio_precision", validation["metrics"]["skipped_gates"])
        self.assertIn("audio_join", validation["metrics"]["skipped_gates"])
        self.assertEqual(validation["metrics"]["processed_audio_summary"]["path"], "")
        self.assertIsNone(
            validation["metrics"]["processed_audio_summary"]["candidate_audio_duration_seconds"]
        )

    def test_pure_bgm_kind_overrides_legacy_generic_replace_audio(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            processed_audio={"validation_summary": "Z:/missing/reverse-summary.json"},
        )

        validation = validate_revision_acceptance(request, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertIn("audio_precision", validation["metrics"]["skipped_gates"])
        self.assertIn("audio_join", validation["metrics"]["skipped_gates"])

    def test_noise_cleanup_kind_overrides_generic_replace_audio_operation(self):
        request = self._conditional_request(
            kind="noise_cleanup",
            op_type="replace_audio",
        )

        profile = self._derive_acceptance_profile(request)

        self.assertIn("audio_precision", profile["skipped_gates"])
        self.assertIn("audio_join", profile["skipped_gates"])
        self.assertIn("audio_delivery", profile["enabled_gates"])

    def test_visual_delete_kind_overrides_generic_delete_operation(self):
        request = self._conditional_request(
            kind="visual_delete",
            op_type="delete",
        )

        profile = self._derive_acceptance_profile(request)

        self.assertIn("visual", profile["enabled_gates"])
        self.assertIn("audio_precision", profile["skipped_gates"])
        self.assertIn("audio_join", profile["skipped_gates"])

    def test_noise_cleanup_only_suppresses_generic_replace_audio_in_mixed_item(self):
        request = self._conditional_request(
            kind="noise_cleanup",
            op_type="replace_audio",
            extra_op_types=("visual_overlay",),
        )

        profile = self._derive_acceptance_profile(request)

        self.assertIn("visual", profile["enabled_gates"])
        self.assertIn("audio_precision", profile["skipped_gates"])
        self.assertIn("audio_join", profile["skipped_gates"])

    def test_visual_delete_only_suppresses_generic_delete_in_mixed_item(self):
        request = self._conditional_request(
            kind="visual_delete",
            op_type="delete",
            extra_op_types=("pointer_overlay",),
        )

        profile = self._derive_acceptance_profile(request)

        self.assertIn("visual", profile["enabled_gates"])
        self.assertIn("pointer", profile["enabled_gates"])
        self.assertIn("audio_precision", profile["skipped_gates"])
        self.assertIn("audio_join", profile["skipped_gates"])

    def test_trim_visual_kind_overrides_generic_delete_operation(self):
        request = self._conditional_request(
            kind="trim_visual",
            op_type="delete",
        )

        profile = self._derive_acceptance_profile(request)

        self.assertIn("visual", profile["enabled_gates"])
        self.assertIn("audio_precision", profile["skipped_gates"])
        self.assertIn("audio_join", profile["skipped_gates"])

    def test_explicit_speech_replace_edit_cannot_inherit_noise_cleanup_suppression(self):
        request = self._conditional_request(
            kind="noise_cleanup",
            op_type="replace_audio",
            extra_op_types=("replace_audio",),
            source_kind="noise_cleanup",
            extra_source_kinds=("speech_replace",),
        )

        profile = self._derive_acceptance_profile(request)

        self.assertIn("audio_precision", profile["enabled_gates"])
        self.assertIn("audio_join", profile["enabled_gates"])

    def test_explicit_spoken_delete_edit_cannot_inherit_visual_delete_suppression(self):
        request = self._conditional_request(
            kind="visual_delete",
            op_type="delete",
            extra_op_types=("delete",),
            source_kind="visual_delete",
            extra_source_kinds=("spoken_delete",),
        )

        profile = self._derive_acceptance_profile(request)

        self.assertIn("visual", profile["enabled_gates"])
        self.assertIn("audio_precision", profile["enabled_gates"])
        self.assertIn("audio_join", profile["enabled_gates"])

    def test_audio_level_and_denoise_profiles_skip_expensive_audio_gates(self):
        for kind, op_type in (("audio_level", "gain"), ("denoise", "noise_cleanup")):
            with self.subTest(kind=kind):
                request = self._conditional_request(kind=kind, op_type=op_type)

                profile = self._derive_acceptance_profile(request)

                self.assertIn("audio_precision", profile["skipped_gates"])
                self.assertIn("audio_join", profile["skipped_gates"])
                self.assertIn("audio_delivery", profile["enabled_gates"])

    def test_spoken_delete_enables_precision_and_join_gates(self):
        request = self._conditional_request(
            kind="spoken_delete",
            op_type="delete",
            validation={},
        )

        validation = validate_revision_acceptance(request, strict=True)

        self.assertIn("audio_precision", validation["metrics"]["enabled_gates"])
        self.assertIn("audio_join", validation["metrics"]["enabled_gates"])
        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(failure["gate"] == "audio_precision" for failure in validation["failures"])
        )

    def test_spoken_acceptance_requires_full_candidate_reverse_asr_report(self):
        request = self._conditional_request(
            kind="spoken_delete",
            op_type="delete",
            evidence={"executed": True, "cut_window": [1.0, 2.0]},
            validation={"status": "pass"},
        )

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("full-candidate reverse ASR" in message for message in validation["errors"]),
            validation["errors"],
        )

    def test_spoken_acceptance_rejects_candidate_hash_mismatch_and_missing_asr_identity(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "final-candidate.wav")
            summary_path = os.path.join(tmpdir, "reverse-asr.json")
            with open(candidate_path, "wb") as f:
                f.write(b"final-candidate-audio")
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "candidate_audio_sha256": hashlib.sha256(b"different-audio").hexdigest(),
                        "status_counts": {"pass": 1},
                        "rows": [{"id": "item01", "status": "pass"}],
                    },
                    f,
                )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio={
                    "output_wav": candidate_path,
                    "validation_summary": summary_path,
                },
            )

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("candidate audio SHA-256" in message for message in validation["errors"]),
            validation["errors"],
        )
        self.assertTrue(
            any("ASR identity" in message for message in validation["errors"]),
            validation["errors"],
        )

    def test_spoken_acceptance_rejects_identity_only_reverse_asr_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[],
                summary_overrides={"status_counts": {}},
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio=processed_audio,
            )

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("no reverse ASR result rows" in message for message in validation["errors"]),
            validation["errors"],
        )

    def test_spoken_acceptance_rejects_review_count_in_summary_aggregate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[],
                summary_overrides={
                    "status_counts": {},
                    "summary": {"pass": 0, "review": 1, "fail": 0},
                },
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio=processed_audio,
            )

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertIn(
            "review:1",
            validation["metrics"]["processed_audio_summary"]["unresolved_statuses"],
        )

    def test_spoken_acceptance_rejects_statusless_reverse_asr_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[{"id": "item01"}],
                summary_overrides={"status_counts": {}},
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio=processed_audio,
            )

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertIn(
            "item01",
            validation["metrics"]["processed_audio_summary"]["unresolved_ids"],
        )

    def test_spoken_acceptance_rejects_aggregate_only_reverse_asr_pass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[],
                summary_overrides={"status_counts": {"pass": 1}},
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio=processed_audio,
            )

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("no reverse ASR result rows" in message for message in validation["errors"]),
            validation["errors"],
        )

    def test_spoken_acceptance_rejects_statusless_items_reverse_asr_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                summary_overrides={
                    "rows": None,
                    "items": [{"id": "item01"}],
                    "status_counts": {"pass": 1},
                },
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio=processed_audio,
            )

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertIn(
            "item01",
            validation["metrics"]["processed_audio_summary"]["unresolved_ids"],
        )

    def test_spoken_acceptance_rejects_statusless_semantic_join_scan_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                summary_overrides={
                    "semantic_join_scan": {"rows": [{"id": "item01"}]},
                },
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio=processed_audio,
            )

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertIn(
            "item01:semantic_join_scan:missing_status",
            validation["metrics"]["processed_audio_summary"]["semantic_join_anomalies"],
        )

    def test_bgm_kind_cannot_hide_concrete_spoken_delete_operation(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="delete",
            evidence={"executed": True, "cut_window": [1.0, 2.0]},
            validation={"status": "pass"},
        )

        profile = self._derive_acceptance_profile(request)
        validation = validate_revision_acceptance(request, strict=True)

        self.assertIn("audio_precision", profile["items"][0]["gates"])
        self.assertIn("audio_join", profile["items"][0]["gates"])
        self.assertFalse(validation["ok"])

    def test_non_strict_legacy_spoken_delete_still_requires_full_candidate_report(self):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "LegacySpokenDraft",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                },
                "edits": [
                    {
                        "type": "delete",
                        "start": 1.0,
                        "end": 2.0,
                        "label": "delete repeated word",
                    }
                ],
                "markers": [],
            }
        )

        validation = validate_revision_acceptance(request, strict=False)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("full-candidate reverse ASR" in message for message in validation["errors"]),
            validation["errors"],
        )

    def test_spoken_source_ledger_rejects_legacy_full_track_audio_delivery(self):
        request = self._conditional_request(
            kind="spoken_delete",
            op_type="delete",
            evidence={"executed": True, "cut_window": [1.0, 2.0]},
            validation={"status": "pass"},
        )
        content = {
            "duration": 10_000_000,
            "tracks": [
                {
                    "type": "video",
                    "name": "Original Video",
                    "segments": [
                        {
                            "id": "video-1",
                            "material_id": "video-material",
                            "target_timerange": {"start": 0, "duration": 10_000_000},
                        },
                        {
                            "id": "video-2",
                            "material_id": "video-material",
                            "target_timerange": {"start": 10_000_000, "duration": 1},
                        },
                    ],
                },
                {
                    "type": "audio",
                    "name": "Replacement Audio",
                    "segments": [
                        {
                            "id": "merged-audio",
                            "material_id": "qa-audio",
                            "source_timerange": {"start": 0, "duration": 10_000_000},
                            "target_timerange": {"start": 0, "duration": 10_000_000},
                        }
                    ],
                },
            ],
            "materials": {
                "videos": [{"id": "video-material", "path": "C:/media/source.mp4"}],
                "audios": [{"id": "qa-audio", "path": "C:/qa/final-narration.wav"}],
                "texts": [],
            },
        }

        validation = validate_saved_revision_draft(request, content)

        self.assertTrue(
            any("segmented audio delivery" in message.lower() for message in validation["errors"]),
            validation["errors"],
        )

    def test_spoken_legacy_delivery_is_rejected_before_opening_a_draft(self):
        request = self._conditional_request(
            kind="spoken_delete",
            op_type="delete",
            evidence={"executed": True, "cut_window": [1.0, 2.0]},
            validation={"status": "pass"},
        )

        with patch.object(
            revision_runner_api,
            "_open_revision_project",
            side_effect=AssertionError("draft must not be opened"),
        ) as open_project:
            with self.assertRaisesRegex(ValueError, "segmented audio delivery"):
                execute_revision_request(request, mock_media=True, strict=True)

        open_project.assert_not_called()

    def test_routed_audio_gates_reject_legacy_delivery_before_opening_a_draft(self):
        cases = (
            ("speech_replace", "replace_audio"),
            ("bgm_replace", "delete"),
        )
        for kind, op_type in cases:
            with self.subTest(kind=kind, op_type=op_type):
                request = self._conditional_request(
                    kind=kind,
                    op_type=op_type,
                    evidence={"executed": True, "cut_window": [1.0, 2.0]},
                    validation={"status": "pass"},
                )
                profile = revision_validation_api.derive_acceptance_profile(
                    request, doc_items=request.review_items
                )
                self.assertTrue(
                    {"audio_precision", "audio_join"}.intersection(
                        profile["items"][0]["gates"]
                    )
                )

                with patch.object(
                    revision_runner_api,
                    "_open_revision_project",
                    side_effect=AssertionError("draft must not be opened"),
                ) as open_project:
                    with self.assertRaisesRegex(
                        ValueError, "segmented audio delivery"
                    ):
                        execute_revision_request(request, mock_media=True, strict=True)

                open_project.assert_not_called()

    def test_lite_label_only_audio_does_not_attach_segmented_gate_to_pointer(self):
        request = self._load_request_payload(
            {
                "workflow_mode": "lite",
                "lite_cut_layout": "split_gap",
                "project": {
                    "draft_name": "LiteLabelOnlyAudio",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                    "media_duration_seconds": 10.0,
                },
                "review_items": [
                    {
                        "id": "audio-label",
                        "kind": "spoken_delete",
                        "source_text": "00:01 删除“无法确认”",
                        "start": 1.0,
                        "execution_required": False,
                        "execution_status": "label_only_unresolved",
                        "evidence": {
                            "asr_alignment": {
                                "status": "pass",
                                "authoritative_timing": True,
                                "authoritative_cut_boundary": False,
                                "resolved_time": 1.0,
                            }
                        },
                    },
                    {
                        "id": "pointer-add",
                        "kind": "pointer_overlay",
                        "source_text": "00:02 添加小手指向标题",
                        "start": 2.0,
                        "execution_required": True,
                    },
                ],
                "edits": [
                    {
                        "type": "add_overlay",
                        "source_kind": "pointer_overlay",
                        "doc_item_id": "pointer-add",
                        "start": 2.0,
                        "end": 3.0,
                        "label": "00:02 添加小手指向标题",
                        "asset_paths": ["C:/media/hand.png"],
                    }
                ],
                "audio_delivery_plan": {"mode": "legacy"},
                "acceptance": {"require_audio_validation": True},
            }
        )

        profile = revision_validation_api.derive_acceptance_profile(
            request, doc_items=request.review_items
        )
        self.assertNotIn("audio_precision", profile["enabled_gates"])
        self.assertNotIn("audio_join", profile["enabled_gates"])
        revision_runner_api._validate_revision_execution_preflight(
            request, request.review_items
        )

    def test_targeted_audio_repair_rejects_widened_delete_window(self):
        request = self._conditional_request(
            kind="spoken_delete",
            op_type="delete",
            evidence={"executed": True, "cut_window": [1.0, 2.0]},
            validation={"status": "review"},
        )
        initial = {
            "ok": False,
            "failures": [
                {
                    "gate": "audio_precision",
                    "item_id": "item01",
                    "status": "fail",
                    "repairable": True,
                    "reason": "residue",
                }
            ],
        }

        def widen(original, _plan):
            widened = replace(original.edits[0], start=0.5, end=2.5)
            return replace(original, edits=[widened])

        with self.assertRaisesRegex(ValueError, "widen"):
            revision_runner_api.run_targeted_acceptance_repair(
                request,
                initial,
                repair_callback=widen,
                validation_callback=lambda *_args: {"ok": True},
            )

    def test_targeted_repair_rejects_in_place_pause_alignment_mutation(self):
        request = self._conditional_request(
            kind="spoken_delete",
            op_type="delete",
            evidence={"executed": True, "cut_window": [1.0, 2.0]},
            validation={"status": "review"},
        )
        request = replace(
            request,
            pause_alignment={
                "source_asr_path": "A.json",
                "source_asr_sha256": "a" * 64,
            },
        )
        initial = {
            "ok": False,
            "failures": [
                {
                    "gate": "audio_precision",
                    "item_id": "item01",
                    "status": "fail",
                    "repairable": True,
                    "reason": "residue",
                }
            ],
        }

        def mutate_in_place(original, _plan):
            original.pause_alignment["source_asr_path"] = "B.json"
            return original

        with self.assertRaisesRegex(ValueError, "hash-bound pause alignment"):
            revision_runner_api.run_targeted_acceptance_repair(
                request,
                initial,
                repair_callback=mutate_in_place,
                validation_callback=lambda *_args: {
                    "ok": True,
                    "metrics": {
                        "enabled_gates": [
                            "source_coverage",
                            "execution_evidence",
                            "draft_exists",
                            "editable_structure",
                            "verbatim_markers",
                            "audio_delivery",
                            "audio_precision",
                        ]
                    },
                },
            )
        self.assertEqual(request.pause_alignment["source_asr_path"], "A.json")

    def test_targeted_repair_scopes_raw_callback_before_prepare(self):
        parameters = inspect.signature(
            revision_runner_api.run_targeted_acceptance_repair
        ).parameters
        self.assertIn("prepare_callback", parameters)

        request = self._conditional_request(
            kind="spoken_delete",
            op_type="delete",
            evidence={"executed": True, "cut_window": [1.0, 2.0]},
            validation={"status": "review"},
        )
        request = replace(request, pause_alignment={"source_asr_path": "A.json"})
        initial = {
            "ok": False,
            "failures": [
                {
                    "gate": "audio_precision",
                    "item_id": "item01",
                    "status": "fail",
                    "repairable": True,
                    "reason": "residue",
                }
            ],
        }
        prepare_calls = []

        def mutate_scope(original, _plan):
            return replace(original, pause_alignment={"source_asr_path": "B.json"})

        def prepare(repaired, _plan):
            prepare_calls.append(repaired)
            return repaired

        with self.assertRaisesRegex(ValueError, "hash-bound pause alignment"):
            revision_runner_api.run_targeted_acceptance_repair(
                request,
                initial,
                repair_callback=mutate_scope,
                prepare_callback=prepare,
                validation_callback=lambda *_args: {"ok": True},
            )

        self.assertEqual(prepare_calls, [])

    def test_targeted_repair_rejects_changes_to_unrelated_review_item(self):
        request = self._conditional_request(
            kind="spoken_delete",
            op_type="delete",
            evidence={"executed": True, "cut_window": [1.0, 2.0]},
            validation={"status": "review"},
        )
        unrelated = RevisionReviewItem(
            item_id="item02",
            kind="review_only",
            source_text="keep this exact source text",
            execution_required=False,
        )
        request = replace(request, review_items=[*request.review_items, unrelated])
        initial = {
            "ok": False,
            "failures": [
                {
                    "gate": "audio_precision",
                    "item_id": "item01",
                    "status": "fail",
                    "repairable": True,
                    "reason": "residue",
                }
            ],
        }

        def corrupt_unrelated(original, _plan):
            changed = replace(original.review_items[1], source_text="changed summary")
            return replace(original, review_items=[original.review_items[0], changed])

        with self.assertRaisesRegex(ValueError, "unrelated review item"):
            revision_runner_api.run_targeted_acceptance_repair(
                request,
                initial,
                repair_callback=corrupt_unrelated,
                validation_callback=lambda *_args: {"ok": True},
            )

    def test_targeted_repair_attempts_once_and_reports_second_failure(self):
        request = self._conditional_request(
            kind="spoken_delete",
            op_type="delete",
            evidence={"executed": True, "cut_window": [1.0, 2.0]},
            validation={"status": "review"},
        )
        initial = {
            "ok": False,
            "failures": [
                {
                    "gate": "audio_precision",
                    "item_id": "item01",
                    "status": "fail",
                    "repairable": True,
                    "reason": "residue",
                }
            ],
        }
        calls = {"repair": 0, "validate": 0}

        def repair_once(original, _plan):
            calls["repair"] += 1
            narrowed = replace(original.edits[0], start=1.1, end=1.9)
            return replace(original, edits=[narrowed])

        def still_fails(_repaired, plan):
            calls["validate"] += 1
            return {
                "ok": False,
                "failures": [
                    {
                        "gate": "audio_precision",
                        "item_id": "item01",
                        "status": "fail",
                        "repairable": False,
                        "reason": "still audible",
                    }
                ],
                "metrics": {
                    "enabled_gates": [
                        "source_coverage",
                        "execution_evidence",
                        "draft_exists",
                        "editable_structure",
                        "verbatim_markers",
                        "audio_delivery",
                        *plan["gates"],
                    ]
                },
            }

        result = revision_runner_api.run_targeted_acceptance_repair(
            request,
            initial,
            repair_callback=repair_once,
            validation_callback=still_fails,
        )

        self.assertEqual(calls, {"repair": 1, "validate": 1})
        self.assertEqual(result["attempt_count"], 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["unresolved_item_ids"], ["item01"])

        repeated = revision_runner_api.run_targeted_acceptance_repair(
            request,
            result,
            repair_callback=repair_once,
            validation_callback=still_fails,
        )
        self.assertEqual(calls, {"repair": 1, "validate": 1})
        self.assertEqual(repeated["attempt_count"], 1)
        self.assertFalse(repeated["repair_attempted"])

    def test_execute_revision_request_integrates_one_targeted_acceptance_repair(self):
        payload = {
            "project": {
                "draft_name": "IntegratedAcceptanceRepair",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "audio_delivery_plan": {
                "mode": "segmented",
                "forbid_full_length_segments": True,
                "segments": [
                    {
                        "id": "source-1",
                        "role": "source",
                        "asset_path": "C:/media/source.wav",
                        "track_name": "Narration - Source Segments",
                        "source_start": 0.0,
                        "timeline_start": 0.0,
                        "duration": 1.0,
                        "doc_item_id": "item01",
                    }
                ],
            },
            "edits": [
                {
                    "type": "delete",
                    "doc_item_id": "item01",
                    "start": 1.0,
                    "end": 2.0,
                    "label": "delete repeated word",
                }
            ],
            "review_items": [
                {
                    "id": "item01",
                    "kind": "spoken_delete",
                    "source_text": "00:01-00:02 delete repeated word",
                    "execution_required": True,
                    "evidence": {
                        "executed": True,
                        "cut_window": [1.0, 2.0],
                        "strategy": "hybrid",
                        "delete": "repeated word",
                        "must_keep": [],
                    },
                    "validation": {"status": "pass"},
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[
                    {
                        "id": "item01",
                        "status": "pass",
                        "delete": "repeated word",
                        "must_keep": [],
                    }
                ],
            )
            request = self._load_request_payload(payload)
            summary_path = processed_audio["validation_summary"]
            with open(summary_path, "r", encoding="utf-8") as summary_file:
                summary = json.load(summary_file)
            summary["audio_delivery_plan_sha256"] = (
                revision_validation_api._audio_delivery_plan_digest(request)
            )
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump(summary, summary_file, ensure_ascii=False, indent=2)
            calls = []

            def repair(_project, current, plan):
                calls.append(plan)
                return replace(current, processed_audio=processed_audio)

            with patch.object(
                revision_runner_api,
                "normalize_pause_adjustments",
                wraps=revision_runner_api.normalize_pause_adjustments,
            ) as normalize_pauses:
                result = execute_revision_request(
                    request,
                    drafts_root=tmpdir,
                    mock_media=True,
                    strict=True,
                    acceptance_repair_callback=repair,
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(normalize_pauses.call_count, 2)
        self.assertTrue(result["acceptance_validation"]["ok"])
        self.assertEqual(result["acceptance_repair"]["attempt_count"], 1)

    def test_integrated_repair_rejects_direct_live_project_mutation(self):
        payload = {
            "project": {
                "draft_name": "RejectLiveProjectRepairMutation",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "audio_delivery_plan": {
                "mode": "segmented",
                "forbid_full_length_segments": True,
                "segments": [
                    {
                        "id": "source-1",
                        "role": "source",
                        "asset_path": "C:/media/source.wav",
                        "track_name": "Narration - Source Segments",
                        "source_start": 0.0,
                        "timeline_start": 0.0,
                        "duration": 1.0,
                        "doc_item_id": "item01",
                    }
                ],
            },
            "edits": [
                {
                    "type": "delete",
                    "doc_item_id": "item01",
                    "start": 1.0,
                    "end": 2.0,
                    "label": "delete repeated word",
                }
            ],
            "review_items": [
                {
                    "id": "item01",
                    "kind": "spoken_delete",
                    "source_text": "00:01-00:02 delete repeated word",
                    "execution_required": True,
                    "evidence": {
                        "executed": True,
                        "cut_window": [1.0, 2.0],
                        "strategy": "hybrid",
                        "delete": "repeated word",
                        "must_keep": [],
                    },
                    "validation": {"status": "pass"},
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[
                    {
                        "id": "item01",
                        "status": "pass",
                        "delete": "repeated word",
                        "must_keep": [],
                    }
                ],
            )
            request = self._load_request_payload(payload)
            summary_path = processed_audio["validation_summary"]
            with open(summary_path, "r", encoding="utf-8") as summary_file:
                summary = json.load(summary_file)
            summary["audio_delivery_plan_sha256"] = (
                revision_validation_api._audio_delivery_plan_digest(request)
            )
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump(summary, summary_file, ensure_ascii=False, indent=2)

            def mutate_live_project(project, current, _plan):
                project.script.width = 1280
                return replace(current, processed_audio=processed_audio)

            with self.assertRaisesRegex(ValueError, "must not mutate the live project"):
                execute_revision_request(
                    request,
                    drafts_root=tmpdir,
                    mock_media=True,
                    strict=True,
                    acceptance_repair_callback=mutate_live_project,
                )

    def test_acceptance_failure_preserves_saved_editable_draft(self):
        payload = {
            "project": {
                "draft_name": "PreserveFailedAcceptance",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "audio_delivery_plan": {
                "mode": "segmented",
                "forbid_full_length_segments": True,
                "segments": [
                    {
                        "id": "source-1",
                        "role": "source",
                        "asset_path": "C:/media/source.wav",
                        "track_name": "Narration - Source Segments",
                        "source_start": 0.0,
                        "timeline_start": 0.0,
                        "duration": 1.0,
                        "doc_item_id": "item01",
                    }
                ],
            },
            "edits": [
                {
                    "type": "delete",
                    "doc_item_id": "item01",
                    "start": 1.0,
                    "end": 2.0,
                    "label": "delete repeated word",
                }
            ],
            "review_items": [
                {
                    "id": "item01",
                    "kind": "spoken_delete",
                    "source_text": "00:01-00:02 delete repeated word",
                    "execution_required": True,
                    "evidence": {"executed": True, "cut_window": [1.0, 2.0]},
                    "validation": {"status": "pass"},
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            request = self._load_request_payload(payload)
            with self.assertRaisesRegex(RuntimeError, "full-candidate reverse ASR"):
                execute_revision_request(
                    request,
                    drafts_root=tmpdir,
                    mock_media=True,
                    strict=True,
                )

            draft_dir = os.path.join(tmpdir, "PreserveFailedAcceptance")
            self.assertTrue(os.path.isdir(draft_dir))
            self.assertTrue(os.path.isfile(os.path.join(draft_dir, "draft_content.json")))

    def test_structurally_invalid_repair_restores_pre_repair_draft(self):
        payload = {
            "project": {
                "draft_name": "RestoreInvalidRepair",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "audio_delivery_plan": {
                "mode": "segmented",
                "forbid_full_length_segments": True,
                "segments": [
                    {
                        "id": "source-1",
                        "role": "source",
                        "asset_path": "C:/media/source.wav",
                        "track_name": "Narration - Source Segments",
                        "source_start": 0.0,
                        "timeline_start": 0.0,
                        "duration": 1.0,
                        "doc_item_id": "item01",
                    }
                ],
            },
            "edits": [
                {
                    "type": "delete",
                    "doc_item_id": "item01",
                    "start": 1.0,
                    "end": 2.0,
                    "label": "delete repeated word",
                }
            ],
            "review_items": [
                {
                    "id": "item01",
                    "kind": "spoken_delete",
                    "source_text": "00:01-00:02 delete repeated word",
                    "execution_required": True,
                    "evidence": {"executed": True, "cut_window": [1.0, 2.0]},
                    "validation": {"status": "pass"},
                }
            ],
        }
        request = self._load_request_payload(payload)

        def corrupt_saved_structure(project, current, _plan):
            project.script.tracks.clear()
            return current

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "must not mutate the live project"):
                execute_revision_request(
                    request,
                    drafts_root=tmpdir,
                    mock_media=True,
                    strict=True,
                    acceptance_repair_callback=corrupt_saved_structure,
                )

            content_path = os.path.join(
                tmpdir,
                "RestoreInvalidRepair",
                "draft_content.json",
            )
            with open(content_path, "r", encoding="utf-8") as f:
                restored_content = json.load(f)

        track_names = [track.get("name") for track in restored_content.get("tracks", [])]
        self.assertIn("Original Video", track_names)
        self.assertIn("Narration - Source Segments", track_names)

    def test_repair_callback_save_then_error_restores_pre_repair_draft(self):
        payload = {
            "project": {
                "draft_name": "RestoreCallbackError",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "audio_delivery_plan": {
                "mode": "segmented",
                "forbid_full_length_segments": True,
                "segments": [
                    {
                        "id": "source-1",
                        "role": "source",
                        "asset_path": "C:/media/source.wav",
                        "track_name": "Narration - Source Segments",
                        "source_start": 0.0,
                        "timeline_start": 0.0,
                        "duration": 1.0,
                        "doc_item_id": "item01",
                    }
                ],
            },
            "edits": [
                {
                    "type": "delete",
                    "doc_item_id": "item01",
                    "start": 1.0,
                    "end": 2.0,
                    "label": "delete repeated word",
                }
            ],
            "review_items": [
                {
                    "id": "item01",
                    "kind": "spoken_delete",
                    "source_text": "00:01-00:02 delete repeated word",
                    "execution_required": True,
                    "evidence": {"executed": True, "cut_window": [1.0, 2.0]},
                    "validation": {"status": "pass"},
                }
            ],
        }
        request = self._load_request_payload(payload)

        def save_corrupt_draft_then_fail(project, _current, _plan):
            project.script.tracks.clear()
            project.save(auto_retain=False)
            raise RuntimeError("repair callback failed after save")

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "repair callback failed after save"):
                execute_revision_request(
                    request,
                    drafts_root=tmpdir,
                    mock_media=True,
                    strict=True,
                    acceptance_repair_callback=save_corrupt_draft_then_fail,
                )

            content_path = os.path.join(
                tmpdir,
                "RestoreCallbackError",
                "draft_content.json",
            )
            with open(content_path, "r", encoding="utf-8") as f:
                restored_content = json.load(f)

        track_names = [track.get("name") for track in restored_content.get("tracks", [])]
        self.assertIn("Original Video", track_names)
        self.assertIn("Narration - Source Segments", track_names)

    def test_failing_item_status_emits_all_routed_gates_in_canonical_order(self):
        request = self._conditional_request(
            kind="spoken_delete",
            op_type="delete",
            evidence={"executed": True},
            validation={"status": "fail"},
        )

        validation = validate_revision_acceptance(request, strict=True)

        reason = "Review item item01 has failing evidence/validation status."
        generic_failures = [
            failure for failure in validation["failures"] if failure["reason"] == reason
        ]
        self.assertEqual(
            [failure["gate"] for failure in generic_failures],
            ["audio_precision", "audio_join"],
        )
        self.assertTrue(all(not failure["repairable"] for failure in generic_failures))
        self.assertEqual(validation["errors"].count(reason), 1)

    def test_pause_validation_rejects_generic_pass_without_pause_proof(self):
        request = self._conditional_request(
            kind="pause_delete",
            op_type="delete",
            validation={"status": "pass"},
        )

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertIn("pause_fit", validation["metrics"]["enabled_gates"])
        self.assertIn("audio_join", validation["metrics"]["enabled_gates"])
        self.assertIn("audio_precision", validation["metrics"]["skipped_gates"])
        self.assertTrue(any(failure["gate"] == "pause_fit" for failure in validation["failures"]))

    def test_pause_validation_accepts_explicit_pause_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            request = self._conditional_request(
                kind="pause_delete",
                op_type="delete",
                validation={"status": "pass", "pause_status": "pass"},
                processed_audio=self._write_full_candidate_reverse_asr(tmpdir),
            )

            validation = validate_revision_acceptance(request, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])

    def test_pause_validation_rejects_nonaccepted_pause_status(self):
        request = self._conditional_request(
            kind="pause_delete",
            op_type="delete",
            validation={"status": "pass", "pause_status": "done"},
        )

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["pause_without_validation"], ["item01"])

    def test_semantic_pause_reverse_asr_requires_source_bound_anchors(self):
        payload = {
            "candidate_audio_sha256": "a" * 64,
            "rows": [
                {
                    "item_id": "item01",
                    "status": "pass",
                    "reverse_asr_evidence": {
                        "candidate_audio_sha256": "a" * 64,
                        "full_candidate_reverse_asr_status": "success",
                        "previous_utterance_match": {"text": "unrelated tail"},
                        "next_utterance_match": {"text": "unrelated onset"},
                        "previous_utterance_preserved": True,
                        "next_utterance_preserved": True,
                        "surrounding_utterance_order_valid": True,
                        "no_asr_word_overlaps_hold": True,
                        "reverse_asr_word_overlaps_hold": [],
                        "previous_protected_trailing_anchor": "tail",
                        "previous_protected_trailing_anchor_present": True,
                        "next_protected_leading_anchor": "onset",
                        "next_protected_leading_anchor_present": True,
                    },
                }
            ],
        }

        problems = revision_validation_api._semantic_pause_reverse_asr_problems(
            payload,
            ["item01"],
            source_anchors_by_id={"item01": ("步枪", "由于")},
        )

        self.assertTrue(any("source-bound" in problem for problem in problems), problems)

    def test_processed_audio_summary_requires_top_level_row_for_each_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[{"id": "item01", "status": "pass"}],
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio=processed_audio,
            )

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01", "item02"),
            )

        self.assertTrue(
            any("item02" in error and "result row" in error for error in result["errors"]),
            result["errors"],
        )

    def test_processed_audio_item_error_is_not_fanned_out_to_passing_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[
                    {
                        "id": "item1",
                        "status": "pass",
                        "delete": "delete item1",
                        "must_keep": [],
                        "source_cut_windows": [[1.0, 1.2]],
                        "mapped_join_times": [1.0],
                    },
                    {
                        "id": "item10",
                        "status": "fail",
                        "delete": "delete item10",
                        "must_keep": [],
                        "source_cut_windows": [[2.0, 2.2]],
                        "mapped_join_times": [2.0],
                        "keep_hits": {"item1": False},
                    },
                ],
            )
            request = self._load_request_payload(
                {
                    "project": {
                        "draft_name": "ScopedAudioFailure",
                        "source_video": "C:/media/source.mp4",
                        "source_audio": "C:/media/source.wav",
                    },
                    "edits": [
                        {
                            "type": "delete",
                            "source_kind": "spoken_delete",
                            "doc_item_id": item_id,
                            "start": start,
                            "end": start + 0.2,
                            "label": item_id,
                        }
                        for item_id, start in (("item1", 1.0), ("item10", 2.0))
                    ],
                    "review_items": [
                        {
                            "id": item_id,
                            "kind": "spoken_delete",
                            "source_text": item_id,
                            "execution_required": True,
                            "evidence": {
                                "executed": True,
                                "cut_window": [start, start + 0.2],
                                "strategy": "hybrid",
                                "delete": f"delete {item_id}",
                                "must_keep": [],
                            },
                            "validation": {"status": "pass"},
                        }
                        for item_id, start in (("item1", 1.0), ("item10", 2.0))
                    ],
                    "processed_audio": processed_audio,
                }
            )

            validation = validate_revision_acceptance(request, strict=True)

        scoped_failures = [
            failure
            for failure in validation["failures"]
            if "reverse ASR result row is not pass" in failure["reason"]
        ]
        self.assertEqual(
            {failure["item_id"] for failure in scoped_failures},
            {"item10"},
            validation["failures"],
        )
        item_errors = validation["metrics"]["processed_audio_summary"].get("item_errors")
        self.assertEqual(set(item_errors or {}), {"item10"})

    def test_nested_reverse_asr_rows_do_not_replace_full_candidate_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "final-candidate.wav")
            candidate_bytes = _test_wav_bytes()
            with open(candidate_path, "wb") as candidate_file:
                candidate_file.write(candidate_bytes)
            nested_path = os.path.join(tmpdir, "nested.json")
            with open(nested_path, "w", encoding="utf-8") as nested_file:
                json.dump({"rows": [{"id": "item01", "status": "pass"}]}, nested_file)
            summary_path = os.path.join(tmpdir, "summary.json")
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump(
                    {
                        "candidate_audio_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                        "asr_identity": {
                            "provider": "test-provider",
                            "model": "test-model",
                            "adapter_version": "1",
                        },
                        "rows": [],
                        "reverse_asr_reports": [nested_path],
                    },
                    summary_file,
                )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio={
                    "output_wav": candidate_path,
                    "validation_summary": summary_path,
                },
            )

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertIn(
            "Full-candidate report contains no reverse ASR result rows.", result["errors"]
        )

    def test_processed_audio_summary_rejects_undecodable_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "final-candidate.wav")
            candidate_bytes = b"not-a-wave"
            with open(candidate_path, "wb") as candidate_file:
                candidate_file.write(candidate_bytes)
            summary_path = os.path.join(tmpdir, "summary.json")
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump(
                    {
                        "candidate_audio_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                        "asr_identity": {
                            "provider": "test-provider",
                            "model": "test-model",
                            "adapter_version": "1",
                        },
                        "rows": [{"id": "item01", "status": "pass"}],
                    },
                    summary_file,
                )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio={
                    "output_wav": candidate_path,
                    "validation_summary": summary_path,
                },
            )

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertTrue(any("decodable audio" in error for error in result["errors"]))

    def test_processed_audio_summary_rejects_truncated_wave_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "truncated-candidate.wav")
            candidate_bytes = _test_wav_bytes()[:-100]
            with open(candidate_path, "wb") as candidate_file:
                candidate_file.write(candidate_bytes)
            summary_path = os.path.join(tmpdir, "summary.json")
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump(
                    {
                        "candidate_audio_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                        "asr_identity": {
                            "provider": "test-provider",
                            "model": "test-model",
                            "adapter_version": "1",
                        },
                        "rows": [{"id": "item01", "status": "pass"}],
                    },
                    summary_file,
                )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio={
                    "output_wav": candidate_path,
                    "validation_summary": summary_path,
                },
            )

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertTrue(any("decodable audio" in error for error in result["errors"]))

    def test_processed_audio_summary_streams_wave_payload_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "candidate.wav")
            candidate_bytes = _test_wav_bytes()
            with open(candidate_path, "wb") as candidate_file:
                candidate_file.write(candidate_bytes)
            summary_path = os.path.join(tmpdir, "summary.json")
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump(
                    {
                        "candidate_audio_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                        "asr_identity": {
                            "provider": "test-provider",
                            "model": "test-model",
                            "adapter_version": "1",
                        },
                        "rows": [{"id": "item01", "status": "pass"}],
                    },
                    summary_file,
                )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio={
                    "output_wav": candidate_path,
                    "validation_summary": summary_path,
                },
            )

            class BoundedReadWave:
                def __init__(self):
                    self.remaining_frames = 131_073
                    self.read_sizes = []

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def getnchannels(self):
                    return 1

                def getsampwidth(self):
                    return 2

                def getframerate(self):
                    return 8_000

                def getnframes(self):
                    return 131_073

                def readframes(self, frame_count):
                    self.read_sizes.append(frame_count)
                    if frame_count > 65_536:
                        raise AssertionError("WAV validation attempted an unbounded read")
                    decoded_count = min(frame_count, self.remaining_frames)
                    self.remaining_frames -= decoded_count
                    return b"\0\0" * decoded_count

            candidate_wave = BoundedReadWave()
            with patch(
                "utils.revision_validation.wave.open",
                return_value=candidate_wave,
            ):
                result = revision_validation_api._validate_processed_audio_summary(
                    request,
                    required_item_ids=("item01",),
                )

        self.assertGreater(len(candidate_wave.read_sizes), 1)
        self.assertLessEqual(max(candidate_wave.read_sizes), 65_536)
        self.assertFalse(
            any("decodable audio" in error for error in result["errors"]),
            result["errors"],
        )

    def test_processed_audio_summary_rejects_candidate_shorter_than_segmented_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(tmpdir)
            request = self._segmented_audio_request(processed_audio, duration=10.0)

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertTrue(
            any(
                "does not cover the segmented delivery timeline" in error
                for error in result["errors"]
            ),
            result["errors"],
        )

    def test_candidate_duration_does_not_deduct_unpaired_segment_fades(self):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "UnpairedFadeCoverage",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                },
                "audio_delivery_plan": {
                    "mode": "segmented",
                    "segments": [
                        {
                            "id": "source-1",
                            "role": "source",
                            "asset_path": "C:/media/source.wav",
                            "track_name": "Narration",
                            "source_start": 0.0,
                            "timeline_start": 0.0,
                            "duration": 10.0,
                            "fade_out": 10.0,
                        }
                    ],
                },
            }
        )

        minimum, maximum = revision_validation_api._segmented_candidate_duration_bounds(request)

        self.assertAlmostEqual(minimum, 9.95)
        self.assertAlmostEqual(maximum, 10.05)

    def test_segmented_candidate_requires_audio_delivery_plan_digest_without_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                duration_seconds=0.1,
            )
            request = self._segmented_audio_request(
                processed_audio,
                duration=0.1,
                bind_audio_report=False,
            )

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertTrue(
            any("audio_delivery_plan_sha256" in error for error in result["errors"]),
            result["errors"],
        )

    def test_spoken_reverse_asr_row_must_match_item_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                duration_seconds=0.2,
                rows=[
                    {
                        "id": "item01",
                        "status": "pass",
                        "strategy": "hybrid",
                        "delete": "filler",
                        "must_keep": ["kept context"],
                        "source_cut_windows": [[9.0, 10.0]],
                        "mapped_join_times": [9.0],
                        "local_joined_text": "wrong item transcript",
                        "delete_hits": {"filler": True},
                        "keep_hits": {},
                    }
                ],
            )
            request = self._load_request_payload(
                {
                    "project": {
                        "draft_name": "AttributableAudioEvidence",
                        "source_video": "C:/media/source.mp4",
                        "source_audio": "C:/media/source.wav",
                    },
                    "audio_delivery_plan": {
                        "mode": "segmented",
                        "segments": [
                            {
                                "id": "source-before",
                                "role": "source",
                                "asset_path": "C:/media/source.wav",
                                "track_name": "Narration",
                                "source_start": 0.0,
                                "timeline_start": 0.0,
                                "duration": 0.1,
                            },
                            {
                                "id": "source-after",
                                "role": "source",
                                "asset_path": "C:/media/source.wav",
                                "track_name": "Narration",
                                "source_start": 0.2,
                                "timeline_start": 0.1,
                                "duration": 0.1,
                            },
                        ],
                    },
                    "edits": [
                        {
                            "type": "delete",
                            "source_kind": "spoken_delete",
                            "doc_item_id": "item01",
                            "start": 0.1,
                            "end": 0.2,
                            "label": "delete filler",
                        }
                    ],
                    "review_items": [
                        {
                            "id": "item01",
                            "kind": "spoken_delete",
                            "source_text": "delete filler",
                            "execution_required": True,
                            "evidence": {
                                "executed": True,
                                "cut_window": [0.1, 0.2],
                                "delete": "filler",
                                "must_keep": ["kept context"],
                                "strategy": "hybrid",
                            },
                            "validation": {"status": "pass"},
                        }
                    ],
                    "processed_audio": processed_audio,
                }
            )
            summary_path = request.processed_audio["validation_summary"]
            with open(summary_path, "r", encoding="utf-8") as summary_file:
                summary = json.load(summary_file)
            summary["audio_delivery_plan_sha256"] = (
                revision_validation_api._audio_delivery_plan_digest(request)
            )
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump(summary, summary_file, ensure_ascii=False, indent=2)

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertTrue(
            any("item contract" in error for error in result["errors"]),
            result["errors"],
        )

    def test_spoken_reverse_asr_row_rejects_delete_phrase_hidden_by_empty_hits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[
                    {
                        "id": "item01",
                        "status": "pass",
                        "delete": "bad",
                        "must_keep": ["keep one"],
                        "local_joined_text": "before bad keep one after",
                        "delete_hits": [],
                        "keep_hits": {"keep one": True},
                    }
                ],
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "bad",
                    "must_keep": ["keep one"],
                    "strategy": "hybrid",
                },
                validation={"status": "pass"},
                processed_audio=processed_audio,
            )

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertTrue(
            any(
                "local transcript contains the item delete phrase" in error
                for error in result["errors"]
            ),
            result["errors"],
        )

    def test_spoken_reverse_asr_row_requires_transcript_support_for_every_must_keep(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[
                    {
                        "id": "item01",
                        "status": "pass",
                        "delete": "bad",
                        "must_keep": ["keep one", "keep two"],
                        "local_joined_text": "keep one only",
                        "delete_hits": [],
                        "keep_hits": {"keep one": True, "keep two": True},
                    }
                ],
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "bad",
                    "must_keep": ["keep one", "keep two"],
                    "strategy": "hybrid",
                },
                validation={"status": "pass"},
                processed_audio=processed_audio,
            )

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertTrue(
            any(
                "local transcript does not contain every item must_keep phrase" in error
                for error in result["errors"]
            ),
            result["errors"],
        )

    def test_spoken_reverse_asr_row_rejects_unstructured_delete_hit_adjudication(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[
                    {
                        "id": "item01",
                        "status": "pass_adjudicated",
                        "delete": "bad",
                        "must_keep": ["keep one"],
                        "local_joined_text": "later bad keep one",
                        "delete_hits": ["bad"],
                        "delete_hit_adjudication": "it is a later occurrence",
                        "keep_hits": {"keep one": True},
                    }
                ],
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "bad",
                    "must_keep": ["keep one"],
                    "strategy": "hybrid",
                },
                validation={"status": "pass_adjudicated"},
                processed_audio=processed_audio,
            )

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertTrue(
            any("structured kept-recurrence adjudication" in error for error in result["errors"]),
            result["errors"],
        )

    def test_spoken_reverse_asr_row_accepts_attributable_kept_recurrence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[
                    {
                        "id": "item01",
                        "status": "pass_adjudicated",
                        "delete": "bad",
                        "must_keep": ["keep one"],
                        "local_joined_text": "later bad keep one",
                        "delete_hits": ["bad"],
                        "delete_hit_adjudication": {
                            "classification": "kept_recurrence",
                            "occurrence_role": "later_kept_occurrence",
                            "phrase": "bad",
                            "local_context": "later bad keep one",
                            "context_anchor": "later",
                            "reason": "The hit belongs to a later retained sentence.",
                        },
                        "keep_hits": {"keep one": True},
                    }
                ],
            )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "bad",
                    "must_keep": ["keep one"],
                    "strategy": "hybrid",
                },
                validation={"status": "pass_adjudicated"},
                processed_audio=processed_audio,
            )

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertEqual(result["errors"], [])

    def test_spoken_reverse_asr_row_rejects_delete_substring_as_context_anchor(self):
        problems = revision_validation_api._spoken_reverse_asr_row_evidence_problems(
            {
                "id": "item01",
                "status": "pass_adjudicated",
                "strategy": "hybrid",
                "delete": "foobar",
                "must_keep": [],
                "source_cut_windows": [[1.0, 2.0]],
                "mapped_join_times": [1.0],
                "local_joined_text": "foobar",
                "delete_hits": ["foobar"],
                "delete_hit_adjudication": {
                    "classification": "kept_recurrence",
                    "occurrence_role": "later_kept_occurrence",
                    "phrase": "foobar",
                    "local_context": "foobar",
                    "context_anchor": "foo",
                    "reason": "Claimed later occurrence.",
                },
                "keep_hits": {},
                "semantic_join_validation": {"status": "pass"},
            },
            request=self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "foobar",
                    "strategy": "hybrid",
                },
                validation={"status": "pass_adjudicated"},
            ),
            item_id="item01",
        )

        self.assertTrue(
            any("structured kept-recurrence adjudication" in problem for problem in problems),
            problems,
        )

    def test_spoken_reverse_asr_row_rejects_one_adjudication_for_multiple_delete_hits(self):
        problems = revision_validation_api._spoken_reverse_asr_row_evidence_problems(
            {
                "id": "item01",
                "status": "pass_adjudicated",
                "strategy": "hybrid",
                "delete": "foo",
                "must_keep": [],
                "source_cut_windows": [[1.0, 2.0]],
                "mapped_join_times": [1.0],
                "local_joined_text": "foo target residue then later foo kept",
                "delete_hits": [
                    {"text": "foo", "candidate_time": 1.0},
                    {"text": "foo", "candidate_time": 3.0},
                ],
                "delete_hit_adjudication": {
                    "classification": "kept_recurrence",
                    "occurrence_role": "later_kept_occurrence",
                    "phrase": "foo",
                    "local_context": "later foo kept",
                    "context_anchor": "later",
                    "reason": "The second hit belongs to a retained sentence.",
                },
                "keep_hits": {},
                "semantic_join_validation": {"status": "pass"},
            },
            request=self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "foo",
                    "strategy": "hybrid",
                },
                validation={"status": "pass_adjudicated"},
            ),
            item_id="item01",
        )

        self.assertTrue(
            any("multiple positive delete_hits" in problem for problem in problems),
            problems,
        )

    def test_spoken_reverse_asr_row_requires_hit_for_kept_recurrence_adjudication(self):
        problems = revision_validation_api._spoken_reverse_asr_row_evidence_problems(
            {
                "id": "item01",
                "status": "pass_adjudicated",
                "strategy": "hybrid",
                "delete": "foo",
                "must_keep": [],
                "source_cut_windows": [[1.0, 2.0]],
                "mapped_join_times": [1.0],
                "local_joined_text": "later foo kept",
                "delete_hits": [],
                "delete_hit_adjudication": {
                    "classification": "kept_recurrence",
                    "occurrence_role": "later_kept_occurrence",
                    "phrase": "foo",
                    "local_context": "later foo kept",
                    "context_anchor": "later",
                    "reason": "The hit belongs to a retained sentence.",
                },
                "keep_hits": {},
                "semantic_join_validation": {"status": "pass"},
            },
            request=self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "foo",
                    "strategy": "hybrid",
                },
                validation={"status": "pass_adjudicated"},
            ),
            item_id="item01",
        )

        self.assertTrue(
            any("exactly one positive delete_hit" in problem for problem in problems),
            problems,
        )

    def test_spoken_reverse_asr_row_rejects_multiple_transcript_delete_occurrences(self):
        problems = revision_validation_api._spoken_reverse_asr_row_evidence_problems(
            {
                "id": "item01",
                "status": "pass_adjudicated",
                "strategy": "hybrid",
                "delete": "foo",
                "must_keep": [],
                "source_cut_windows": [[1.0, 2.0]],
                "mapped_join_times": [1.0],
                "local_joined_text": "foo target residue then later foo kept",
                "delete_hits": [{"text": "foo", "candidate_time": 3.0}],
                "delete_hit_adjudication": {
                    "classification": "kept_recurrence",
                    "occurrence_role": "later_kept_occurrence",
                    "phrase": "foo",
                    "local_context": "later foo kept",
                    "context_anchor": "later",
                    "reason": "The reported hit belongs to a retained sentence.",
                },
                "keep_hits": {},
                "semantic_join_validation": {"status": "pass"},
            },
            request=self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "foo",
                    "strategy": "hybrid",
                },
                validation={"status": "pass_adjudicated"},
            ),
            item_id="item01",
        )

        self.assertTrue(
            any("multiple local transcript delete occurrences" in problem for problem in problems),
            problems,
        )

    def test_spoken_reverse_asr_row_counts_overlapping_delete_occurrences(self):
        problems = revision_validation_api._spoken_reverse_asr_row_evidence_problems(
            {
                "id": "item01",
                "status": "pass_adjudicated",
                "strategy": "hybrid",
                "delete": "哈哈",
                "must_keep": [],
                "source_cut_windows": [[1.0, 2.0]],
                "mapped_join_times": [1.0],
                "local_joined_text": "哈哈哈后来",
                "delete_hits": [{"text": "哈哈", "candidate_time": 3.0}],
                "delete_hit_adjudication": {
                    "classification": "kept_recurrence",
                    "occurrence_role": "later_kept_occurrence",
                    "phrase": "哈哈",
                    "local_context": "哈哈后来",
                    "context_anchor": "后来",
                    "reason": "The reported hit belongs to retained context.",
                },
                "keep_hits": {},
                "semantic_join_validation": {"status": "pass"},
            },
            request=self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "哈哈",
                    "strategy": "hybrid",
                },
                validation={"status": "pass_adjudicated"},
            ),
            item_id="item01",
        )

        self.assertTrue(
            any("multiple local transcript delete occurrences" in problem for problem in problems),
            problems,
        )

    def test_spoken_reverse_asr_row_rejects_conflicting_transcript_aliases(self):
        problems = revision_validation_api._spoken_reverse_asr_row_evidence_problems(
            {
                "id": "item01",
                "status": "pass",
                "strategy": "hybrid",
                "delete": "bad",
                "must_keep": ["keep"],
                "source_cut_windows": [[1.0, 2.0]],
                "mapped_join_times": [1.0],
                "local_joined_text": "keep",
                "local_asr_text": "bad keep",
                "delete_hits": [],
                "keep_hits": {"keep": True},
                "semantic_join_validation": {"status": "pass"},
            },
            request=self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "bad",
                    "must_keep": ["keep"],
                    "strategy": "hybrid",
                },
                validation={"status": "pass"},
            ),
            item_id="item01",
        )

        self.assertIn("local transcript aliases disagree", problems)

    def test_spoken_reverse_asr_row_rejects_punctuation_only_transcript(self):
        problems = revision_validation_api._spoken_reverse_asr_row_evidence_problems(
            {
                "id": "item01",
                "status": "pass",
                "strategy": "hybrid",
                "delete": "bad",
                "must_keep": [],
                "source_cut_windows": [[1.0, 2.0]],
                "mapped_join_times": [1.0],
                "local_joined_text": "...",
                "delete_hits": [],
                "keep_hits": {},
                "semantic_join_validation": {"status": "pass"},
            },
            request=self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "bad",
                    "strategy": "hybrid",
                },
                validation={"status": "pass"},
            ),
            item_id="item01",
        )

        self.assertIn("local transcript has no alphanumeric content", problems)

    def test_spoken_reverse_asr_row_requires_nonempty_item_contract(self):
        problems = revision_validation_api._spoken_reverse_asr_row_evidence_problems(
            {
                "id": "item01",
                "status": "pass",
                "strategy": "hybrid",
                "source_cut_windows": [[1.0, 2.0]],
                "mapped_join_times": [1.0],
                "local_joined_text": "arbitrary transcript",
                "delete_hits": [],
                "keep_hits": {},
                "semantic_join_validation": {"status": "pass"},
            },
            request=self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
            ),
            item_id="item01",
        )

        self.assertIn("item contract: strategy is missing", problems)
        self.assertIn("item contract: delete phrase is missing", problems)
        self.assertIn("item contract: must_keep field is missing", problems)

    def test_spoken_reverse_asr_row_rejects_unrelated_positive_delete_hit(self):
        problems = revision_validation_api._spoken_reverse_asr_row_evidence_problems(
            {
                "id": "item01",
                "status": "pass_adjudicated",
                "strategy": "hybrid",
                "delete": "bad",
                "must_keep": ["keep"],
                "source_cut_windows": [[1.0, 2.0]],
                "mapped_join_times": [1.0],
                "local_joined_text": "later bad keep",
                "delete_hits": {"unrelated": True},
                "delete_hit_adjudication": {
                    "classification": "kept_recurrence",
                    "occurrence_role": "later_kept_occurrence",
                    "phrase": "bad",
                    "local_context": "later bad keep",
                    "context_anchor": "later",
                    "reason": "Claimed retained recurrence.",
                },
                "keep_hits": {"keep": True},
                "semantic_join_validation": {"status": "pass"},
            },
            request=self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={
                    "executed": True,
                    "cut_window": [1.0, 2.0],
                    "delete": "bad",
                    "must_keep": ["keep"],
                    "strategy": "hybrid",
                },
                validation={"status": "pass_adjudicated"},
            ),
            item_id="item01",
        )

        self.assertTrue(
            any(
                "positive delete_hit does not match the item delete phrase" in problem
                for problem in problems
            ),
            problems,
        )

    def test_processed_audio_summary_fully_decodes_non_wave_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "broken-candidate.mp3")
            candidate_bytes = b"ID3\x04\x00\x00\x00\x00\x00\x00broken-payload"
            with open(candidate_path, "wb") as candidate_file:
                candidate_file.write(candidate_bytes)
            summary_path = os.path.join(tmpdir, "summary.json")
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump(
                    {
                        "candidate_audio_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                        "asr_identity": {
                            "provider": "test-provider",
                            "model": "test-model",
                            "adapter_version": "1",
                        },
                        "rows": [
                            {
                                "id": "item01",
                                "status": "pass",
                                "strategy": "hybrid",
                                "source_cut_windows": [[1.0, 2.0]],
                                "mapped_join_times": [1.0],
                                "local_joined_text": "kept context",
                                "delete_hits": [],
                                "keep_hits": {},
                                "semantic_join_validation": {"status": "pass"},
                            }
                        ],
                    },
                    summary_file,
                )
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={"status": "pass"},
                processed_audio={
                    "output_wav": candidate_path,
                    "validation_summary": summary_path,
                },
            )

            with patch.object(
                revision_validation_api,
                "get_duration_ffprobe_cached",
                return_value=0.1,
            ) as duration_probe:
                result = revision_validation_api._validate_processed_audio_summary(
                    request,
                    required_item_ids=("item01",),
                )

        duration_probe.assert_called_once_with(
            candidate_path,
            hashlib.sha256(candidate_bytes).hexdigest(),
        )
        self.assertTrue(any("decodable audio" in error for error in result["errors"]))

    def test_ffprobe_duration_cache_keys_same_path_by_content_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "candidate.mp3")
            with open(candidate_path, "wb") as candidate_file:
                candidate_file.write(b"first candidate")
            formatters_api.get_duration_ffprobe_cached.cache_clear()
            with patch.object(
                formatters_api.subprocess,
                "run",
                side_effect=[
                    SimpleNamespace(stdout="2.0\n"),
                    SimpleNamespace(stdout="0.5\n"),
                ],
            ) as probe:
                first_duration = formatters_api.get_duration_ffprobe_cached(
                    candidate_path,
                    "sha-first",
                )
                with open(candidate_path, "wb") as candidate_file:
                    candidate_file.write(b"second candidate")
                second_duration = formatters_api.get_duration_ffprobe_cached(
                    candidate_path,
                    "sha-second",
                )
            formatters_api.get_duration_ffprobe_cached.cache_clear()

        self.assertEqual(first_duration, 2.0)
        self.assertEqual(second_duration, 0.5)
        self.assertEqual(probe.call_count, 2)

    def test_processed_audio_summary_rejects_generic_pass_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "candidate.wav")
            candidate_bytes = _test_wav_bytes()
            with open(candidate_path, "wb") as candidate_file:
                candidate_file.write(candidate_bytes)
            summary_path = os.path.join(tmpdir, "summary.json")
            with open(summary_path, "w", encoding="utf-8") as summary_file:
                json.dump(
                    {
                        "candidate_audio_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                        "asr_identity": {
                            "provider": "test-provider",
                            "model": "test-model",
                            "adapter_version": "1",
                        },
                        "rows": [{"id": "item01", "status": "pass"}],
                    },
                    summary_file,
                )
            request = self._segmented_audio_request(
                {
                    "output_wav": candidate_path,
                    "validation_summary": summary_path,
                },
                duration=0.1,
            )

            result = revision_validation_api._validate_processed_audio_summary(
                request,
                required_item_ids=("item01",),
            )

        self.assertTrue(
            any("attributable reverse ASR evidence" in error for error in result["errors"]),
            result["errors"],
        )

    def test_semantic_pause_boundary_rejects_gap_absent_from_bound_asr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            asr_path = os.path.join(tmpdir, "source_asr.json")
            with open(asr_path, "w", encoding="utf-8") as source:
                json.dump(
                    {
                        "utterances": [
                            {"text": "first", "start": 100.0, "end": 105.0},
                            {"text": "second", "start": 105.2, "end": 111.0},
                        ],
                        "words": [
                            {"text": "first", "start": 100.0, "end": 105.0},
                            {"text": "second", "start": 105.2, "end": 111.0},
                        ],
                    },
                    source,
                )
            with open(asr_path, "rb") as source:
                asr_sha256 = hashlib.sha256(source.read()).hexdigest()
            proof = {
                "duration": 1.0,
                "segment_id": "hold-1",
                "requested_source_time": 109.0,
                "source_time": 107.415,
                "frame_source_time": 107.415,
                "boundary_evidence": {
                    "status": "pass",
                    "requested_time": 109.0,
                    "resolved_time": 107.415,
                    "previous_word_end": 106.72,
                    "next_word_start": 108.11,
                    "gap_duration": 1.39,
                    "previous_guard_seconds": 0.695,
                    "next_guard_seconds": 0.695,
                    "minimum_edge_guard_seconds": 0.05,
                    "placement": "gap_midpoint",
                    "reason": "nearest_utterance_gap_midpoint",
                    "source_asr_path": asr_path,
                    "source_asr_sha256": asr_sha256,
                },
            }

            proven = revision_validation_api._semantic_pause_boundary_is_proven(proof)

        self.assertFalse(proven)

    def test_pause_validation_accepts_semantic_pause_adjustment_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import cv2
            import numpy as np

            asr_path = os.path.join(tmpdir, "source_asr.json")
            with open(asr_path, "w", encoding="utf-8") as source:
                json.dump(
                    {
                        "utterances": [
                            {
                                "text": "first ending rifle",
                                "start": 100.08,
                                "end": 106.72,
                            },
                            {"text": "because second", "start": 108.11, "end": 111.59},
                        ],
                        "words": [
                            {"text": "tail", "start": 106.2, "end": 106.72},
                            {"text": "由于", "start": 108.11, "end": 108.43},
                            {"text": "大", "start": 108.83, "end": 109.03},
                        ],
                    },
                    source,
                )
            with open(asr_path, "rb") as source:
                asr_sha256 = hashlib.sha256(source.read()).hexdigest()

            source_audio_path = os.path.join(tmpdir, "source.wav")
            with open(source_audio_path, "wb") as source_audio:
                source_audio.write(_test_wav_bytes())
            source_audio_sha256 = revision_validation_api._sha256_file(source_audio_path)

            source_video_path = os.path.join(tmpdir, "source.avi")
            writer = cv2.VideoWriter(
                source_video_path,
                cv2.VideoWriter_fourcc(*"MJPG"),
                2.0,
                (16, 16),
            )
            self.assertTrue(writer.isOpened())
            for index in range(236):
                writer.write(np.full((16, 16, 3), index % 251, dtype=np.uint8))
            writer.release()
            source_video_sha256 = revision_validation_api._sha256_file(source_video_path)

            frame_path = os.path.join(tmpdir, "stable_107.415.png")
            capture = cv2.VideoCapture(source_video_path)
            capture.set(cv2.CAP_PROP_POS_MSEC, 107.415 * 1_000)
            ok, source_frame = capture.read()
            capture.release()
            self.assertTrue(ok)
            self.assertTrue(cv2.imwrite(frame_path, source_frame))
            with open(frame_path, "rb") as frame_file:
                frame_sha256 = hashlib.sha256(frame_file.read()).hexdigest()
            boundary_evidence = {
                "status": "pass",
                "requested_time": 109.0,
                "resolved_time": 107.415,
                "previous_word_end": 106.72,
                "next_word_start": 108.11,
                "gap_duration": 1.39,
                "previous_guard_seconds": 0.695,
                "next_guard_seconds": 0.695,
                "minimum_edge_guard_seconds": 0.05,
                "placement": "gap_midpoint",
                "reason": "nearest_utterance_gap_midpoint",
                "source_asr_path": asr_path,
                "source_asr_sha256": asr_sha256,
            }
            candidate_sha256 = hashlib.sha256(_test_wav_bytes(118.6)).hexdigest()
            pause_reverse_row = {
                "item_id": "item01",
                "kind": "semantic_pause_adjustment",
                "status": "pass",
                "reverse_asr_evidence": {
                    "candidate_audio_sha256": candidate_sha256,
                    "full_candidate_reverse_asr_status": "success",
                    "previous_utterance_match": {"text": "first ending rifle"},
                    "next_utterance_match": {"text": "because second"},
                    "previous_utterance_preserved": True,
                    "next_utterance_preserved": True,
                    "surrounding_utterance_order_valid": True,
                    "no_asr_word_overlaps_hold": True,
                    "reverse_asr_word_overlaps_hold": [],
                    "previous_protected_trailing_anchor": "le",
                    "previous_protected_trailing_anchor_present": True,
                    "next_protected_leading_anchor": "be",
                    "next_protected_leading_anchor_present": True,
                },
            }
            processed_audio = self._write_full_candidate_reverse_asr(
                tmpdir,
                duration_seconds=118.6,
                summary_overrides={
                    "rows": None,
                    "result_rows": [pause_reverse_row],
                },
            )
            request = self._load_request_payload(
                {
                    "project": {
                        "draft_name": "SemanticPauseAcceptance",
                        "source_video": source_video_path,
                        "source_audio": source_audio_path,
                        "media_duration_seconds": 118.0,
                    },
                    "edits": [
                        {
                            "type": "semantic_pause_adjustment",
                            "source_kind": "semantic_pause_adjustment",
                            "doc_item_id": "item01",
                            "start": 109.0,
                            "end": 109.0,
                            "label": "semantic pause",
                        }
                    ],
                    "review_items": [
                        {
                            "id": "item01",
                            "kind": "semantic_pause_adjustment",
                            "source_text": "insert a sentence-boundary pause",
                            "execution_required": True,
                            "evidence": {
                                "executed": True,
                                "semantic_pause_adjustment": {
                                    "duration": 0.6,
                                    "segment_id": "hold-1",
                                    "timeline_start": 107.415,
                                    "timeline_end": 108.015,
                                    "frame_path": frame_path,
                                    "frame_sha256": frame_sha256,
                                    "track_name": "Original Video",
                                    "requested_source_time": 109.0,
                                    "source_time": 107.415,
                                    "frame_source_time": 107.415,
                                    "boundary_evidence": boundary_evidence,
                                },
                            },
                            "validation": {"status": "pass"},
                        }
                    ],
                    "pause_adjustments": [
                        {
                            "item_id": "item01",
                            "requested_source_time": 109.0,
                            "source_time": 109.0,
                            "frame_source_time": 107.415,
                            "duration": 0.6,
                            "frame_path": frame_path,
                            "frame_sha256": frame_sha256,
                            "reason": "sentence boundary",
                        }
                    ],
                    "pause_alignment": {
                        "source_asr_path": asr_path,
                        "source_asr_sha256": asr_sha256,
                        "source_video_sha256": source_video_sha256,
                        "source_audio_sha256": source_audio_sha256,
                        "alignment_audio_path": source_audio_path,
                        "alignment_audio_sha256": source_audio_sha256,
                        "source_asr_identity": {
                            "provider": "test-provider",
                            "model": "test-model",
                            "adapter_version": "test-adapter-v1",
                            "preprocessing": "none",
                        },
                        "semantic_gap_seconds": 0.8,
                        "search_window_seconds": 3.0,
                    },
                    "audio_delivery_plan": {
                        "mode": "segmented",
                        "max_single_segment_ratio": 1.0,
                        "segments": [
                            {
                                "id": "source-before",
                                "role": "source",
                                "asset_path": source_audio_path,
                                "track_name": "Narration",
                                "source_start": 0.0,
                                "timeline_start": 0.0,
                                "duration": 107.415,
                            },
                            {
                                "id": "source-after",
                                "role": "source",
                                "asset_path": source_audio_path,
                                "track_name": "Narration",
                                "source_start": 107.415,
                                "timeline_start": 108.015,
                                "duration": 10.585,
                            },
                            {
                                "id": "reference-before",
                                "role": "reference",
                                "asset_path": source_audio_path,
                                "track_name": "Reference",
                                "source_start": 0.0,
                                "timeline_start": 0.0,
                                "duration": 107.415,
                                "volume": 0.0,
                            },
                            {
                                "id": "reference-after",
                                "role": "reference",
                                "asset_path": source_audio_path,
                                "track_name": "Reference",
                                "source_start": 107.415,
                                "timeline_start": 108.015,
                                "duration": 10.585,
                                "volume": 0.0,
                            },
                        ],
                    },
                    "processed_audio": processed_audio,
                }
            )
            request = revision_runner_api.normalize_pause_adjustments(request)
            with open(processed_audio["validation_summary"], "r", encoding="utf-8") as report:
                bound_report = revision_runner_api.bind_audio_delivery_plan_to_report(
                    request,
                    json.load(report),
                )
            with open(processed_audio["validation_summary"], "w", encoding="utf-8") as report:
                json.dump(bound_report, report, ensure_ascii=False, indent=2)
            content = {
                "duration": 118_600_000,
                "tracks": [
                    {
                        "type": "video",
                        "name": "Original Video",
                        "segments": [
                            {
                                "id": "source-before-video",
                                "material_id": "source-video-material",
                                "target_timerange": {
                                    "start": 0,
                                    "duration": 107_415_000,
                                },
                                "source_timerange": {
                                    "start": 0,
                                    "duration": 107_415_000,
                                },
                            },
                            {
                                "id": "hold-1",
                                "material_id": "frame-material",
                                "target_timerange": {
                                    "start": 107_415_000,
                                    "duration": 600_000,
                                },
                                "source_timerange": {"start": 0, "duration": 600_000},
                            },
                            {
                                "id": "source-after-video",
                                "material_id": "source-video-material",
                                "target_timerange": {
                                    "start": 108_015_000,
                                    "duration": 10_585_000,
                                },
                                "source_timerange": {
                                    "start": 107_415_000,
                                    "duration": 10_585_000,
                                },
                            },
                        ],
                    },
                    {
                        "type": "audio",
                        "name": "Narration",
                        "segments": [
                            {
                                "id": "source-before-audio",
                                "material_id": "source-audio-material",
                                "volume": 1.0,
                                "target_timerange": {
                                    "start": 0,
                                    "duration": 107_415_000,
                                },
                                "source_timerange": {
                                    "start": 0,
                                    "duration": 107_415_000,
                                },
                            },
                            {
                                "id": "source-after-audio",
                                "material_id": "source-audio-material",
                                "volume": 1.0,
                                "target_timerange": {
                                    "start": 108_015_000,
                                    "duration": 10_585_000,
                                },
                                "source_timerange": {
                                    "start": 107_415_000,
                                    "duration": 10_585_000,
                                },
                            },
                        ],
                    },
                    {
                        "type": "audio",
                        "name": "Reference",
                        "segments": [
                            {
                                "id": "reference-before-audio",
                                "material_id": "source-audio-material",
                                "volume": 0.0,
                                "target_timerange": {
                                    "start": 0,
                                    "duration": 107_415_000,
                                },
                                "source_timerange": {
                                    "start": 0,
                                    "duration": 107_415_000,
                                },
                            },
                            {
                                "id": "reference-after-audio",
                                "material_id": "source-audio-material",
                                "volume": 0.0,
                                "target_timerange": {
                                    "start": 108_015_000,
                                    "duration": 10_585_000,
                                },
                                "source_timerange": {
                                    "start": 107_415_000,
                                    "duration": 10_585_000,
                                },
                            },
                        ],
                    },
                ],
                "materials": {
                    "videos": [
                        {
                            "id": "source-video-material",
                            "type": "video",
                            "path": source_video_path,
                        },
                        {
                            "id": "frame-material",
                            "type": "photo",
                            "path": frame_path,
                        },
                    ],
                    "audios": [
                        {
                            "id": "source-audio-material",
                            "type": "audio",
                            "path": source_audio_path,
                        }
                    ],
                    "texts": [],
                },
            }

            self.assertFalse(
                revision_validation_api._pause_fit_is_proven(request.review_items[0], None)
            )

            validation = validate_revision_acceptance(request, content, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertIn("pause_fit", validation["metrics"]["enabled_gates"])

    def test_pause_validation_rejects_reverse_asr_without_following_sentence_onset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            asr_path = os.path.join(tmpdir, "source_asr.json")
            with open(asr_path, "w", encoding="utf-8") as source:
                json.dump({"utterances": []}, source)
            with open(asr_path, "rb") as source:
                asr_sha256 = hashlib.sha256(source.read()).hexdigest()
            candidate_sha256 = hashlib.sha256(_test_wav_bytes()).hexdigest()
            request = self._conditional_request(
                kind="semantic_pause_adjustment",
                op_type="semantic_pause_adjustment",
                evidence={
                    "executed": True,
                    "semantic_pause_adjustment": {
                        "duration": 1.0,
                        "segment_id": "hold-1",
                        "requested_source_time": 109.0,
                        "source_time": 107.415,
                        "frame_source_time": 107.415,
                        "boundary_evidence": {
                            "status": "pass",
                            "requested_time": 109.0,
                            "resolved_time": 107.415,
                            "previous_word_end": 106.72,
                            "next_word_start": 108.11,
                            "gap_duration": 1.39,
                            "previous_guard_seconds": 0.695,
                            "next_guard_seconds": 0.695,
                            "minimum_edge_guard_seconds": 0.05,
                            "placement": "gap_midpoint",
                            "reason": "nearest_utterance_gap_midpoint",
                            "source_asr_path": asr_path,
                            "source_asr_sha256": asr_sha256,
                        },
                    },
                },
                validation={"status": "pass"},
                processed_audio=self._write_full_candidate_reverse_asr(
                    tmpdir,
                    rows=[
                        {
                            "item_id": "item01",
                            "kind": "semantic_pause_adjustment",
                            "status": "pass",
                            "reverse_asr_evidence": {
                                "candidate_audio_sha256": candidate_sha256,
                                "full_candidate_reverse_asr_status": "success",
                                "previous_utterance_match": {"text": "前句结尾步枪"},
                                "next_utterance_match": {"text": "于后句开始"},
                                "previous_utterance_preserved": True,
                                "next_utterance_preserved": False,
                                "surrounding_utterance_order_valid": True,
                                "no_asr_word_overlaps_hold": True,
                                "reverse_asr_word_overlaps_hold": [],
                                "previous_protected_trailing_anchor": "步枪",
                                "previous_protected_trailing_anchor_present": True,
                                "next_protected_leading_anchor": "由于",
                                "next_protected_leading_anchor_present": False,
                            },
                        }
                    ],
                ),
            )

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("following sentence onset" in error for error in validation["errors"]),
            validation["errors"],
        )

    def test_pause_validation_rejects_hold_on_reported_sentence_onset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            asr_path = os.path.join(tmpdir, "source_asr.json")
            with open(asr_path, "w", encoding="utf-8") as source:
                json.dump({"utterances": []}, source)
            with open(asr_path, "rb") as source:
                asr_sha256 = hashlib.sha256(source.read()).hexdigest()
            request = self._conditional_request(
                kind="semantic_pause_adjustment",
                op_type="semantic_pause_adjustment",
                evidence={
                    "executed": True,
                    "semantic_pause_adjustment": {
                        "duration": 0.6,
                        "segment_id": "hold-1",
                        "requested_source_time": 109.0,
                        "source_time": 108.11,
                        "frame_source_time": 108.11,
                        "boundary_evidence": {
                            "status": "pass",
                            "requested_time": 109.0,
                            "resolved_time": 108.11,
                            "previous_word_end": 106.72,
                            "next_word_start": 108.11,
                            "gap_duration": 1.39,
                            "previous_guard_seconds": 1.39,
                            "next_guard_seconds": 0.0,
                            "reason": "nearest_utterance_gap_boundary",
                            "source_asr_path": asr_path,
                            "source_asr_sha256": asr_sha256,
                        },
                    },
                },
                validation={"status": "pass"},
            )

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["pause_without_validation"], ["item01"])

    def test_pause_validation_rejects_semantic_pause_without_asr_boundary(self):
        request = self._conditional_request(
            kind="semantic_pause_adjustment",
            op_type="semantic_pause_adjustment",
            evidence={
                "executed": True,
                "semantic_pause_adjustment": {"duration": 0.6, "segment_id": "hold-1"},
            },
            validation={"status": "pass"},
        )

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["pause_without_validation"], ["item01"])

    def test_pause_validation_accepts_visual_hold_review_evidence(self):
        request = self._conditional_request(
            kind="visual_hold_review",
            op_type="visual_hold_review",
            evidence={
                "executed": True,
                "visual_hold_review": {
                    "status": "pass",
                    "duration": 0.6,
                    "frame_path": "C:/media/hold.png",
                    "segment_id": "hold-1",
                },
            },
            validation={"status": "pass"},
        )

        validation = validate_revision_acceptance(request, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertIn("pause_fit", validation["metrics"]["enabled_gates"])

    def test_pause_validation_rejects_nonaccepted_semantic_status(self):
        for status in ("review", "pending"):
            with self.subTest(status=status):
                request = self._conditional_request(
                    kind="semantic_pause_adjustment",
                    op_type="delete",
                    evidence={
                        "executed": True,
                        "semantic_pause_adjustment": {
                            "status": status,
                            "duration": 0.6,
                            "segment_id": "hold-1",
                        },
                    },
                    validation={"status": "pass"},
                )

                validation = validate_revision_acceptance(request, strict=True)

                self.assertFalse(validation["ok"])
                self.assertEqual(validation["metrics"]["pause_without_validation"], ["item01"])
                self.assertTrue(
                    any(failure["gate"] == "pause_fit" for failure in validation["failures"])
                )

    def test_pause_validation_rejects_arbitrary_nonempty_proof(self):
        proofs = (
            {"note": "reviewed"},
            {"status": "pass"},
            {"duration": 0.6},
            {"duration": 0.6, "source_time": 2.0},
        )
        for proof in proofs:
            with self.subTest(proof=proof):
                request = self._conditional_request(
                    kind="semantic_pause_adjustment",
                    op_type="delete",
                    evidence={
                        "executed": True,
                        "semantic_pause_adjustment": proof,
                    },
                    validation={"status": "pass"},
                )

                validation = validate_revision_acceptance(request, strict=True)

                self.assertFalse(validation["ok"])
                self.assertEqual(validation["metrics"]["pause_without_validation"], ["item01"])

    def test_unknown_execution_required_kind_is_conservative_review_failure(self):
        request = self._conditional_request(
            kind="custom_magic_edit",
            op_type="custom_magic_edit",
            evidence={"executed": True, "operation": "custom_magic_edit"},
            validation={"status": "pass"},
        )

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        failure = next(
            failure for failure in validation["failures"] if failure["item_id"] == "item01"
        )
        self.assertEqual(failure["gate"], "execution_evidence")
        self.assertEqual(failure["status"], "review")
        self.assertTrue(failure["repairable"])

    def test_review_only_is_known_only_when_execution_is_not_required(self):
        non_executing = self._conditional_request(
            kind="custom_review_note",
            op_type="custom_review_note",
            execution_required=False,
        )
        executing = self._conditional_request(
            kind="review_only",
            op_type="custom_review_note",
            execution_required=True,
        )

        non_executing_profile = self._derive_acceptance_profile(non_executing)
        executing_validation = validate_revision_acceptance(executing, strict=True)

        self.assertTrue(non_executing_profile["items"][0]["known"])
        self.assertEqual(non_executing_profile["items"][0]["kind"], "review_only")
        self.assertFalse(executing_validation["ok"])
        self.assertTrue(
            any(failure["status"] == "review" for failure in executing_validation["failures"])
        )

    def test_marker_only_audio_or_pause_kind_does_not_enable_execution_gates(self):
        for kind in ("spoken_delete", "semantic_pause_adjustment"):
            with self.subTest(kind=kind):
                request = self._load_request_payload(
                    {
                        "project": {
                            "draft_name": "MarkerOnly",
                            "source_video": "C:/media/source.mp4",
                            "source_audio": "C:/media/source.wav",
                        },
                        "edits": [],
                        "markers": [],
                        "review_items": [
                            {
                                "id": "item01",
                                "kind": kind,
                                "source_text": "manual review only",
                                "execution_required": False,
                            }
                        ],
                    }
                )

                profile = self._derive_acceptance_profile(request)
                validation = validate_revision_acceptance(request, strict=True)

                self.assertIn("audio_precision", profile["skipped_gates"])
                self.assertIn("audio_join", profile["skipped_gates"])
                self.assertIn("pause_fit", profile["skipped_gates"])
                self.assertTrue(validation["ok"], validation["errors"])
                self.assertEqual(validation["metrics"]["processed_audio_summary"]["path"], "")

    def test_false_acceptance_flags_cannot_disable_item_derived_gates(self):
        payload = {
            "project": {
                "draft_name": "ConditionalAcceptance",
                "source_video": "C:/media/source.mp4",
            },
            "edits": [],
            "markers": [{"label": "item01 marker", "start": 1.0, "end": 2.0}],
            "review_items": [
                {
                    "id": "item01",
                    "kind": "visual_overlay",
                    "source_text": "item01 marker",
                    "execution_required": True,
                }
            ],
            "acceptance": {
                "require_execution_evidence": False,
                "require_visual_evidence": False,
            },
        }
        request = self._load_request_payload(payload)

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertIn("execution_evidence", validation["metrics"]["enabled_gates"])
        self.assertIn("visual", validation["metrics"]["enabled_gates"])
        self.assertTrue(
            any(
                "cannot disable" in reason
                for reason in validation["metrics"]["gate_reasons"]["execution_evidence"]
            )
        )
        self.assertTrue(
            any(failure["gate"] == "execution_evidence" for failure in validation["failures"])
        )
        self.assertTrue(any(failure["gate"] == "visual" for failure in validation["failures"]))

    def test_latest_doc_kind_overrides_request_kind_and_empty_list_is_authoritative(self):
        request = self._conditional_request(kind="spoken_delete", op_type="replace_audio")
        latest_bgm = RevisionReviewItem(
            item_id="item01",
            kind="bgm_replace",
            source_text="item01 replace background music",
            execution_required=True,
        )

        request_profile = self._derive_acceptance_profile(request)
        latest_profile = self._derive_acceptance_profile(
            request,
            doc_items=[latest_bgm],
            supplied=True,
        )
        empty_profile = self._derive_acceptance_profile(request, doc_items=[], supplied=True)

        self.assertIn("audio_precision", request_profile["enabled_gates"])
        self.assertIn("audio_precision", latest_profile["skipped_gates"])
        self.assertIn("audio_join", latest_profile["skipped_gates"])
        self.assertIn("audio_precision", empty_profile["enabled_gates"])
        self.assertIn("audio_join", empty_profile["enabled_gates"])

    def test_explicit_true_acceptance_flags_add_conditional_gates(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            acceptance={
                "require_audio_validation": True,
                "require_visual_evidence": True,
                "require_pause_validation": True,
            },
        )

        profile = self._derive_acceptance_profile(request)

        for gate in ("audio_precision", "audio_join", "pause_fit", "visual"):
            self.assertIn(gate, profile["enabled_gates"])

    def test_explicit_pause_flag_requires_proof_for_cheap_item(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            acceptance={"require_pause_validation": True},
            validation={"status": "pass"},
        )

        profile = self._derive_acceptance_profile(request)
        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertIn("pause_fit", validation["metrics"]["enabled_gates"])
        self.assertIn("pause_fit", profile["items"][0]["gates"])
        self.assertEqual(validation["metrics"]["pause_without_validation"], ["item01"])
        self.assertTrue(
            any(
                failure["gate"] == "pause_fit" and failure["item_id"] == "item01"
                for failure in validation["failures"]
            )
        )

    def test_explicit_pause_flag_accepts_proof_for_cheap_item(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            acceptance={"require_pause_validation": True},
            validation={"status": "pass", "pause_status": "accepted"},
        )

        profile = self._derive_acceptance_profile(request)
        validation = validate_revision_acceptance(request, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertIn("pause_fit", profile["items"][0]["gates"])

    def test_explicit_pause_flag_does_not_add_pause_gate_to_unrelated_items(self):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "ConditionalAcceptance",
                    "source_video": "C:/media/source.mp4",
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "pause_delete",
                        "doc_item_id": "pause-item",
                        "start": 1.0,
                        "end": 1.5,
                        "label": "shorten pause",
                    },
                    {
                        "type": "delete",
                        "source_kind": "spoken_delete",
                        "doc_item_id": "speech-item",
                        "start": 2.0,
                        "end": 2.5,
                        "label": "delete word",
                    },
                ],
                "review_items": [
                    {
                        "id": "pause-item",
                        "kind": "pause_delete",
                        "source_text": "shorten pause",
                        "execution_required": True,
                    },
                    {
                        "id": "speech-item",
                        "kind": "spoken_delete",
                        "source_text": "delete word",
                        "execution_required": True,
                    },
                ],
                "acceptance": {"require_pause_validation": True},
            }
        )

        profile = self._derive_acceptance_profile(request)
        routes = {row["item_id"]: row for row in profile["items"]}

        self.assertIn("pause_fit", routes["pause-item"]["gates"])
        self.assertNotIn("pause_fit", routes["speech-item"]["gates"])

    def test_explicit_pause_flag_without_execution_item_is_global_failure(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            execution_required=False,
            acceptance={"require_pause_validation": True},
            evidence={},
            validation={},
        )

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["pause_without_validation"], [])
        self.assertTrue(
            any(
                failure["gate"] == "pause_fit" and not failure["item_id"]
                for failure in validation["failures"]
            )
        )

    def test_explicit_pause_flag_without_attributable_review_item_is_global_failure(self):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "ConditionalAcceptance",
                    "source_video": "C:/media/source.mp4",
                    "replacement_audio": "C:/media/replacement.wav",
                },
                "edits": [
                    {
                        "type": "replace_audio",
                        "doc_item_id": "item01",
                        "start": 1.0,
                        "end": 2.0,
                        "label": "item01 requested edit",
                    }
                ],
                "acceptance": {"require_pause_validation": True},
            }
        )

        validation = validate_revision_acceptance(request, strict=False)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                failure["gate"] == "pause_fit" and not failure["item_id"]
                for failure in validation["failures"]
            )
        )

    def test_explicit_pause_flag_checks_ledger_item_without_matching_action(self):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "ConditionalAcceptance",
                    "source_video": "C:/media/source.mp4",
                },
                "review_items": [
                    {
                        "id": "item01",
                        "kind": "bgm_replace",
                        "source_text": "item01 requested edit",
                        "execution_required": True,
                        "validation": {"status": "pass"},
                    }
                ],
                "acceptance": {"require_pause_validation": True},
            }
        )

        validation = validate_revision_acceptance(request, strict=False)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["pause_without_validation"], ["item01"])
        self.assertTrue(
            any(
                failure["gate"] == "pause_fit" and failure["item_id"] == "item01"
                for failure in validation["failures"]
            )
        )

    def test_explicit_audio_flag_requires_validation_for_cheap_bgm_item(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            acceptance={"require_audio_validation": True},
            validation={},
        )

        profile = self._derive_acceptance_profile(request)
        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        item_gates = profile["items"][0]["gates"]
        self.assertIn("audio_precision", item_gates)
        self.assertIn("audio_join", item_gates)
        self.assertEqual(validation["metrics"]["audio_without_validation"], ["item01"])
        self.assertTrue(
            any(
                failure["gate"] == "audio_precision" and failure["item_id"] == "item01"
                for failure in validation["failures"]
            )
        )

    def test_explicit_audio_flag_accepts_validation_for_cheap_bgm_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            request = self._conditional_request(
                kind="bgm_replace",
                op_type="replace_audio",
                acceptance={"require_audio_validation": True},
                validation={"status": "pass"},
                processed_audio=self._write_full_candidate_reverse_asr(tmpdir),
            )

            profile = self._derive_acceptance_profile(request)
            validation = validate_revision_acceptance(request, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])
        item_gates = profile["items"][0]["gates"]
        self.assertIn("audio_precision", item_gates)
        self.assertIn("audio_join", item_gates)

    def test_explicit_audio_flag_without_review_ledger_is_global_failure(self):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "ConditionalAcceptance",
                    "source_video": "C:/media/source.mp4",
                    "replacement_audio": "C:/media/replacement.wav",
                },
                "edits": [
                    {
                        "type": "replace_audio",
                        "doc_item_id": "item01",
                        "start": 1.0,
                        "end": 2.0,
                        "label": "item01 replace background music",
                    }
                ],
                "acceptance": {"require_audio_validation": True},
            }
        )

        validation = validate_revision_acceptance(request, strict=False)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                failure["gate"] == "audio_precision" and not failure["item_id"]
                for failure in validation["failures"]
            )
        )
        self.assertTrue(any("review item" in error for error in validation["errors"]))

    def test_explicit_visual_flag_requires_item_and_saved_content_evidence(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            acceptance={"require_visual_evidence": True},
            evidence={},
        )
        content = {
            "duration": 10_000_000,
            "tracks": [
                {
                    "type": "video",
                    "name": "Original Video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}],
                }
            ],
            "materials": {},
        }

        profile = self._derive_acceptance_profile(request)
        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertFalse(validation["ok"])
        self.assertIn("visual", profile["items"][0]["gates"])
        self.assertEqual(validation["metrics"]["visual_without_item_evidence"], ["item01"])
        self.assertTrue(any(failure["gate"] == "visual" for failure in validation["failures"]))

    def test_explicit_visual_flag_accepts_item_and_saved_content_evidence(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            acceptance={"require_visual_evidence": True},
            evidence={
                "operation": "visual_overlay",
                "track_name": "Explicit Visual Overlay",
                "segment_id": "overlay-1",
                "material_id": "overlay-material-1",
                "asset_path": "C:/media/overlay.png",
            },
        )
        content = {
            "duration": 10_000_000,
            "tracks": [
                {
                    "type": "video",
                    "name": "Original Video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}],
                },
                {
                    "type": "video",
                    "name": "Explicit Visual Overlay",
                    "segments": [
                        {
                            "id": "overlay-1",
                            "material_id": "overlay-material-1",
                            "target_timerange": {
                                "start": 1_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {
                "videos": [
                    {
                        "id": "overlay-material-1",
                        "path": "C:/media/overlay.png",
                    }
                ]
            },
        }

        profile = self._derive_acceptance_profile(request)
        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertIn("visual", profile["items"][0]["gates"])

    def test_explicit_visual_flag_rejects_generic_pass_with_unrelated_saved_overlay(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            acceptance={"require_visual_evidence": True},
            evidence={"status": "pass"},
        )
        content = {
            "duration": 10_000_000,
            "tracks": [
                {
                    "type": "video",
                    "name": "Original Video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}],
                },
                {
                    "type": "video",
                    "name": "Unrelated Overlay",
                    "segments": [
                        {
                            "id": "unrelated-overlay",
                            "target_timerange": {
                                "start": 8_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {},
        }

        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["visual_without_item_evidence"], ["item01"])
        self.assertTrue(
            any(
                failure["gate"] == "visual" and failure["item_id"] == "item01"
                for failure in validation["failures"]
            )
        )

    def test_explicit_visual_flag_rejects_executed_only_with_unrelated_saved_overlay(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            acceptance={"require_visual_evidence": True},
            evidence={"executed": True},
        )
        content = {
            "duration": 10_000_000,
            "tracks": [
                {
                    "type": "video",
                    "name": "Original Video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}],
                },
                {
                    "type": "video",
                    "name": "Unrelated Overlay",
                    "segments": [
                        {
                            "id": "unrelated-overlay",
                            "target_timerange": {
                                "start": 8_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {},
        }

        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["visual_without_item_evidence"], ["item01"])

    def test_explicit_visual_flag_rejects_locators_for_unrelated_saved_overlay(self):
        request = self._conditional_request(
            kind="bgm_replace",
            op_type="replace_audio",
            acceptance={"require_visual_evidence": True},
            evidence={
                "operation": "visual_overlay",
                "track_name": "Expected Overlay",
                "segment_id": "expected-segment",
                "asset_path": "C:/media/expected.png",
            },
        )
        content = {
            "duration": 10_000_000,
            "tracks": [
                {
                    "type": "video",
                    "name": "Original Video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}],
                },
                {
                    "type": "video",
                    "name": "Unrelated Overlay",
                    "segments": [
                        {
                            "id": "unrelated-segment",
                            "material_id": "unrelated-material",
                            "target_timerange": {
                                "start": 8_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {
                "videos": [
                    {
                        "id": "unrelated-material",
                        "path": "C:/media/unrelated.png",
                    }
                ]
            },
        }

        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["visual_without_item_evidence"], ["item01"])

    def test_visual_delete_rejects_source_window_still_present_in_saved_main_track(self):
        validation = self._validate_visual_delete_source_ranges(((0.0, 2.0), (2.0, 4.0)))

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["visual_without_item_evidence"], ["item01"])

    def test_visual_delete_accepts_removed_tail_without_surviving_end_boundary(self):
        validation = self._validate_visual_delete_source_ranges(((0.0, 2.0),))

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(validation["metrics"]["visual_without_item_evidence"], [])

    def test_visual_delete_accepts_absent_middle_source_interval_without_overlay(self):
        request = self._conditional_request(
            kind="visual_delete",
            op_type="visual_delete",
            evidence={
                "operation": "visual_delete",
                "source_window": [2.0, 4.0],
            },
        )
        content = {
            "duration": 4_000_000,
            "tracks": [
                {
                    "type": "video",
                    "name": "Original Video",
                    "segments": [
                        {
                            "id": "main-before",
                            "source_timerange": {
                                "start": 0,
                                "duration": 2_000_000,
                            },
                            "target_timerange": {
                                "start": 0,
                                "duration": 2_000_000,
                            },
                        },
                        {
                            "id": "main-after",
                            "source_timerange": {
                                "start": 4_000_000,
                                "duration": 2_000_000,
                            },
                            "target_timerange": {
                                "start": 2_000_000,
                                "duration": 2_000_000,
                            },
                        },
                    ],
                }
            ],
            "materials": {"videos": [{"path": "C:/media/source.mp4"}]},
        }

        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(validation["metrics"]["visual_without_item_evidence"], [])

    def test_visual_delete_rejects_partial_retained_overlap(self):
        validation = self._validate_visual_delete_source_ranges(
            ((0.0, 2.0), (3.0, 4.0), (4.0, 6.0))
        )

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["visual_without_item_evidence"], ["item01"])

    def test_explicit_visual_flag_without_review_ledger_is_global_failure(self):
        request = self._load_request_payload(
            {
                "project": {
                    "draft_name": "ConditionalAcceptance",
                    "source_video": "C:/media/source.mp4",
                    "replacement_audio": "C:/media/replacement.wav",
                },
                "edits": [
                    {
                        "type": "replace_audio",
                        "doc_item_id": "item01",
                        "start": 1.0,
                        "end": 2.0,
                        "label": "item01 replace background music",
                    }
                ],
                "acceptance": {"require_visual_evidence": True},
            }
        )

        validation = validate_revision_acceptance(request, strict=False)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                failure["gate"] == "visual" and not failure["item_id"]
                for failure in validation["failures"]
            )
        )
        self.assertTrue(any("review item" in error for error in validation["errors"]))

    def test_false_acceptance_flags_cannot_disable_routed_audio_or_pause_gates(self):
        cases = (
            ("spoken_delete", "delete", {"require_audio_validation": False}, "audio_precision"),
            ("pause_delete", "delete", {"require_pause_validation": False}, "pause_fit"),
        )
        for kind, op_type, acceptance, expected_gate in cases:
            with self.subTest(kind=kind):
                request = self._conditional_request(
                    kind=kind,
                    op_type=op_type,
                    acceptance=acceptance,
                    validation={},
                )

                profile = self._derive_acceptance_profile(request)
                validation = validate_revision_acceptance(request, strict=True)

                self.assertFalse(validation["ok"])
                self.assertIn(expected_gate, validation["metrics"]["enabled_gates"])
                self.assertIn(
                    expected_gate,
                    profile["items"][0]["gates"],
                )
                self.assertTrue(
                    any(failure["gate"] == expected_gate for failure in validation["failures"])
                )

    def test_speech_repair_replace_operation_requires_audio_join(self):
        request = self._conditional_request(kind="speech_repair", op_type="replace")

        profile = self._derive_acceptance_profile(request)

        self.assertIn("audio_precision", profile["items"][0]["gates"])
        self.assertIn("audio_join", profile["items"][0]["gates"])

    def test_timeline_plan_mapping_runs_in_non_strict_acceptance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            timeline_plan_path = os.path.join(tmpdir, "timeline-plan.json")
            with open(timeline_plan_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "replacement_source_window": [10.0, 20.0],
                        "replacement_timeline_window": [8.0, 18.0],
                    },
                    f,
                )
            request = self._conditional_request(
                kind="visual_overlay",
                op_type="visual_overlay",
                acceptance={"require_visual_evidence": False},
                evidence={
                    "executed": True,
                    "source_window": [10.0, 20.0],
                    "timeline_window": [9.0, 19.0],
                },
                processed_audio={"timeline_plan": timeline_plan_path},
            )

            validation = validate_revision_acceptance(request, strict=False)

        self.assertFalse(validation["ok"])
        self.assertTrue(validation["metrics"]["timeline_mapping_errors"])

    def test_acceptance_missing_source_text_remains_nonblocking(self):
        payload = {
            "project": {
                "draft_name": "ConditionalAcceptance",
                "source_video": "C:/media/source.mp4",
                "replacement_audio": "C:/media/bgm.wav",
            },
            "edits": [
                {
                    "type": "replace_audio",
                    "doc_item_id": "item01",
                    "start": 0.0,
                    "end": 5.0,
                    "label": "item01 replace music",
                }
            ],
            "review_items": [
                {
                    "id": "item01",
                    "kind": "bgm_replace",
                    "detail": "item01 replace music",
                    "execution_required": True,
                    "evidence": {"executed": True},
                    "validation": {"status": "pass"},
                }
            ],
        }
        request = self._load_request_payload(payload)

        validation = validate_revision_acceptance(request, strict=True)

        self.assertEqual(request.review_items[0].verbatim_status, "unverified_source_unavailable")
        self.assertTrue(validation["ok"], validation["errors"])

    def test_review_only_item_without_source_text_or_action_still_delivers_draft(self):
        payload = {
            "project": {
                "draft_name": "MissingSourceReviewOnly",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [],
            "markers": [],
            "review_items": [
                {
                    "id": "empty-row",
                    "kind": "review_only",
                    "source_text": "",
                    "verbatim_status": "unverified_source_unavailable",
                    "execution_required": False,
                }
            ],
            "acceptance": {
                "expected_review_item_count": 1,
                "expected_review_item_ids": ["empty-row"],
                "require_review_items": True,
                "require_execution_evidence": True,
                "require_final_acceptance": True,
            },
        }
        request = self._load_request_payload(payload)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = execute_revision_request(
                request,
                drafts_root=tmpdir,
                mock_media=True,
                strict=True,
            )
            draft_path = os.path.join(
                tmpdir,
                "MissingSourceReviewOnly",
                "draft_content.json",
            )
            self.assertTrue(os.path.isfile(draft_path))

        self.assertTrue(result["acceptance_validation"]["ok"])
        self.assertEqual(result["review_marker_count"], 1)

    def test_strict_acceptance_rejects_missing_doc_item(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 10.0,
                    "end": 12.0,
                    "label": "修改01 删除开头",
                }
            ],
            "markers": [],
            "review_items": [
                {"id": "修改01", "source_text": "修改01 删除开头", "kind": "spoken_delete"},
            ],
            "acceptance": {
                "expected_review_item_count": 2,
                "expected_review_item_ids": ["修改01", "修改02"],
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(any("count mismatch" in message for message in validation["errors"]))
        self.assertTrue(any("修改02".lower() in message for message in validation["errors"]))

    def test_strict_acceptance_rejects_marker_only_pointer_task_without_overlay(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [],
            "markers": [
                {
                    "label": "校对05 小手重做",
                    "start": 317.0,
                    "end": 319.0,
                    "detail": "05:17-05:19 删除原小手并重新添加，指向/下划线“记有5000余单字”",
                }
            ],
            "review_items": [
                {
                    "id": "校对05",
                    "source_text": "05:17-05:19 删除原小手并重新添加，指向/下划线“记有5000余单字”",
                    "kind": "pointer_overlay",
                    "execution_required": True,
                }
            ],
        }
        content = {
            "duration": 400000000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 400000000}}],
                },
                {
                    "name": "校对标记1",
                    "type": "text",
                    "segments": [{"target_timerange": {"start": 317000000, "duration": 2000000}}],
                },
            ],
            "materials": {"videos": [{"path": "C:/media/source.mp4"}], "audios": [], "texts": [{}]},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("markers are not execution evidence" in message for message in validation["errors"])
        )
        self.assertTrue(any("no non-marker overlay" in message for message in validation["errors"]))

    def test_strict_acceptance_rejects_pointer_overlay_without_subject_receipt(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [],
            "markers": [
                {
                    "label": "校对05 小手重做",
                    "start": 317.0,
                    "end": 319.0,
                    "detail": "05:17-05:19 删除原小手并重新添加，指向/下划线“记有5000余单字”",
                }
            ],
            "review_items": [
                {
                    "id": "校对05",
                    "source_text": "05:17-05:19 删除原小手并重新添加，指向/下划线“记有5000余单字”",
                    "kind": "pointer_overlay",
                    "execution_required": True,
                    "evidence": {
                        "executed": True,
                        "track_name": "Pointer Overlay",
                        "segment_id": "seg-pointer-05",
                        "asset_path": "C:/media/hand.png",
                    },
                }
            ],
        }
        content = {
            "duration": 400000000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 400000000}}],
                },
                {
                    "name": "Pointer Overlay",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 317000000, "duration": 2000000}}],
                },
                {
                    "name": "校对标记1",
                    "type": "text",
                    "segments": [{"target_timerange": {"start": 317000000, "duration": 2000000}}],
                },
            ],
            "materials": {
                "videos": [{"path": "C:/media/source.mp4"}, {"path": "C:/media/hand.png"}],
                "audios": [],
                "texts": [{}],
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("subject pointer binding receipt" in message for message in validation["errors"]),
            validation["errors"],
        )

    def test_strict_acceptance_rejects_pointer_rough_time_without_lifecycle_evidence(self):
        request = self._conditional_request(
            kind="pointer_overlay",
            op_type="pointer_overlay",
            evidence={
                "executed": True,
                "duration_rule": "review comment source window mapped after physical deletions",
                "source_window": [122.0, 124.0],
                "in_point": 113.2,
                "out_point": 115.2,
            },
            validation={"status": "pass"},
            acceptance={"require_pointer_lifecycle_evidence": False},
        )

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                "pointer lifecycle evidence failed" in message
                and "rough_time_used_as_exact_boundary" in message
                for message in validation["errors"]
            ),
            validation["errors"],
        )

    def test_strict_acceptance_rejects_course_pointer_without_binding_receipt(self):
        request = self._conditional_request(
            kind="pointer_overlay",
            op_type="pointer_overlay",
            evidence={
                "executed": True,
                "track_name": "Pointer Overlay",
                "segment_id": "seg-pointer-01",
                "asset_path": "C:/media/hand.png",
            },
            validation={"status": "pass"},
        )
        content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Pointer Overlay",
                    "type": "video",
                    "segments": [
                        {
                            "id": "seg-pointer-01",
                            "target_timerange": {
                                "start": 1_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {
                "videos": [
                    {"path": "C:/media/source.mp4"},
                    {"path": "C:/media/hand.png"},
                ],
                "audios": [],
                "texts": [],
            },
        }

        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("subject pointer binding receipt" in message for message in validation["errors"]),
            validation["errors"],
        )

    def test_strict_acceptance_rejects_unverified_subject_pointer_receipt(self):
        request = self._conditional_request(
            kind="pointer_overlay",
            op_type="pointer_overlay",
            evidence={
                "executed": True,
                "track_name": "Pointer Overlay",
                "segment_id": "seg-pointer-01",
                "asset_path": "C:/media/hand.png",
                "subject_profile_receipt": {
                    "project_key": "History_C1001",
                    "registry_root": 123,
                },
            },
            validation={"status": "pass"},
            acceptance={"require_subject_pointer_binding": True},
        )
        content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Pointer Overlay",
                    "type": "video",
                    "segments": [
                        {
                            "id": "seg-pointer-01",
                            "target_timerange": {
                                "start": 1_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {
                "videos": [
                    {"path": "C:/media/source.mp4"},
                    {"path": "C:/media/hand.png"},
                ],
                "audios": [],
                "texts": [],
            },
        }

        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("failed fresh validation" in message for message in validation["errors"]),
            validation["errors"],
        )

    def test_cleanup_pointer_still_requires_fresh_subject_receipt(self):
        request = self._conditional_request(
            kind="pointer_overlay",
            op_type="pointer_overlay",
            evidence={
                "executed": True,
                "lifecycle_mode": "cleanup_recorded_pointer",
            },
            validation={"status": "pass"},
        )

        with (
            patch(
                "utils.revision_validation.pointer_lifecycle_evidence_problems",
                return_value=[],
            ),
            patch(
                "utils.revision_validation._visual_evidence_attribution",
                return_value={"ok": True, "requires_overlay": False},
            ),
        ):
            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                "requires a fresh subject pointer binding receipt" in message
                for message in validation["errors"]
            ),
            validation["errors"],
        )

    def test_cleanup_pointer_receipt_skips_added_pointer_overlay_matching(self):
        request = self._conditional_request(
            kind="pointer_overlay",
            op_type="pointer_overlay",
            evidence={
                "executed": True,
                "lifecycle_mode": "cleanup_recorded_pointer",
                "subject_profile_receipt": {"project_key": "ConditionalAcceptance"},
            },
            validation={"status": "pass"},
        )

        with (
            patch(
                "utils.revision_validation.pointer_lifecycle_evidence_problems",
                return_value=[],
            ),
            patch(
                "utils.revision_validation._fresh_subject_pointer_receipt_validation",
                return_value={"ok": True, "problems": []},
            ) as fresh_receipt,
            patch(
                "utils.revision_validation._pointer_overlay_receipt_problems",
                return_value=["overlay_matching_must_not_run"],
            ) as overlay_matching,
            patch(
                "utils.revision_validation._visual_evidence_attribution",
                return_value={"ok": True, "requires_overlay": False},
            ),
        ):
            validation = validate_revision_acceptance(request, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])
        fresh_receipt.assert_called_once()
        overlay_matching.assert_not_called()

    def test_cleanup_pointer_saved_state_keeps_cleanup_checks_without_pointer_geometry(self):
        item = RevisionReviewItem(
            "item-cleanup",
            "pointer_overlay",
            "Remove the recorded hand",
            evidence={"lifecycle_mode": "remove_recorded_pointer_until_absent"},
        )
        routes = {
            "item-cleanup": {
                "item_id": "item-cleanup",
                "normalized_item_id": "item-cleanup",
                "gates": ["visual", "pointer"],
            }
        }

        with (
            patch(
                "utils.revision_validation.pointer_saved_motion_problems",
                return_value=["motion_check_ran"],
            ) as motion_check,
            patch(
                "utils.revision_validation.pointer_saved_residual_cover_problems",
                return_value=["cover_check_ran"],
            ) as cover_check,
            patch(
                "utils.revision_validation.pointer_saved_geometry_problems",
                return_value=["geometry_check_must_not_run"],
            ) as geometry_check,
        ):
            result = revision_validation_api._pointer_saved_state_validation(
                [item],
                routes,
                {"tracks": [], "materials": {}},
            )

        motion_check.assert_called_once()
        cover_check.assert_called_once()
        geometry_check.assert_not_called()
        self.assertEqual(
            result["item_problems"]["item-cleanup"],
            ["motion_check_ran", "cover_check_ran"],
        )
        self.assertEqual(result["pointer_item_count"], 0)

    def test_strict_pointer_receipt_gate_cannot_be_disabled_by_item_metadata(self):
        content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Pointer Overlay",
                    "type": "video",
                    "segments": [
                        {
                            "id": "seg-pointer-01",
                            "target_timerange": {
                                "start": 1_000_000,
                                "duration": 1_000_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {
                "videos": [
                    {"path": "C:/media/source.mp4"},
                    {"path": "C:/media/hand.png"},
                ],
                "audios": [],
                "texts": [],
            },
        }
        cases = (
            {"kind": "visual_overlay", "execution_required": True},
            {"kind": "pointer_overlay", "execution_required": False},
        )

        for case in cases:
            with self.subTest(**case):
                request = self._conditional_request(
                    kind=case["kind"],
                    op_type="pointer_overlay",
                    execution_required=case["execution_required"],
                    evidence={
                        "executed": True,
                        "track_name": "Pointer Overlay",
                        "segment_id": "seg-pointer-01",
                        "asset_path": "C:/media/hand.png",
                    },
                    validation={"status": "pass"},
                )

                validation = validate_revision_acceptance(request, content, strict=True)

                self.assertFalse(validation["ok"])
                self.assertTrue(
                    any(
                        "subject pointer binding receipt" in message
                        for message in validation["errors"]
                    ),
                    validation["errors"],
                )

    def test_strict_acceptance_rejects_saved_pointer_scale_and_extra_hand_layer(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as tmpdir:
            asset_path = os.path.join(tmpdir, "history-hand.png")
            old_asset_path = os.path.join(tmpdir, "old-hand.png")
            image = np.zeros((354, 354, 4), dtype=np.uint8)
            image[:, :, 3] = 255
            self.assertTrue(cv2.imwrite(asset_path, image))
            self.assertTrue(cv2.imwrite(old_asset_path, image))
            visible_height_ratio = 47 / 1080
            visible_width_ratio = 47 / 1920
            receipt = {
                "asset_path": asset_path,
                "asset_role": "hand",
                "anchor": [13 / 354, 12 / 354],
                "visible_height_ratio": visible_height_ratio,
                "visible_width_ratio": visible_width_ratio,
                "scale_reference_layout": "full-frame-history-image-text",
                "media_contract": {
                    "format": "png",
                    "has_alpha": True,
                    "width": 354,
                    "height": 354,
                },
            }
            evidence = {
                "executed": True,
                "operation": "pointer_overlay",
                "track_name": "Hand Pointer - Editable Single PNG",
                "segment_id": "pointer-segment-01",
                "asset_path": asset_path,
                "asset_role": "hand",
                "current_layout": "full-frame-history-image-text",
                "target_point": [812.0, 704.0],
                "anchor": [13 / 354, 12 / 354],
                "anchor_rule": "registered fingertip",
                "scale_rule": "bound current-layout reference",
                "placement_method": "registered_hotspot_to_frame_target",
                "in_point": 1.0,
                "out_point": 2.0,
                "duration_rule": "target explanation beat",
                "out_reason": "next visual change",
                "hotspot_landing": {
                    "status": "pass",
                    "method": "rendered_hotspot_landing",
                    "landing_error_px": 0.0,
                },
                "subject_profile_receipt": receipt,
            }
            request = self._conditional_request(
                kind="pointer_overlay",
                op_type="pointer_overlay",
                evidence=evidence,
                validation={"status": "pass"},
                acceptance={"require_subject_pointer_binding": True},
            )
            content = {
                "duration": 3_000_000,
                "canvas_config": {"width": 1920, "height": 1080},
                "tracks": [
                    {
                        "name": "Original Video",
                        "type": "video",
                        "segments": [
                            {
                                "id": "source-segment",
                                "material_id": "source-material",
                                "render_index": 0,
                                "target_timerange": {
                                    "start": 0,
                                    "duration": 3_000_000,
                                },
                            }
                        ],
                    },
                    {
                        "name": "Hand Pointer - Editable Single PNG",
                        "type": "video",
                        "segments": [
                            {
                                "id": "pointer-segment-01",
                                "material_id": "pointer-material",
                                "render_index": 30,
                                "target_timerange": {
                                    "start": 1_000_000,
                                    "duration": 1_000_000,
                                },
                                "clip": {
                                    "scale": {"x": 0.22, "y": 0.22},
                                    "transform": {"x": 0.0, "y": 0.0},
                                },
                            }
                        ],
                    },
                    {
                        "name": "Old Hand Pointer",
                        "type": "video",
                        "segments": [
                            {
                                "id": "old-hand-segment",
                                "material_id": "old-hand-material",
                                "render_index": 29,
                                "target_timerange": {
                                    "start": 1_000_000,
                                    "duration": 1_000_000,
                                },
                            }
                        ],
                    },
                ],
                "materials": {
                    "videos": [
                        {"id": "source-material", "path": "C:/media/source.mp4"},
                        {"id": "pointer-material", "path": asset_path},
                        {"id": "old-hand-material", "path": old_asset_path},
                    ],
                    "audios": [],
                    "texts": [],
                },
            }

            with patch(
                "utils.revision_validation._fresh_subject_pointer_receipt_validation",
                return_value={"ok": True, "problems": []},
            ):
                validation = validate_revision_acceptance(
                    request,
                    content,
                    strict=True,
                )

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("pointer_geometry.scale_x_mismatch" in error for error in validation["errors"]),
            validation["errors"],
        )
        self.assertTrue(
            any("pointer_layers.extra_pointer_track" in error for error in validation["errors"]),
            validation["errors"],
        )

    def test_strict_acceptance_rejects_marker_only_animation_task(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [],
            "markers": [
                {
                    "label": "校对01 橙色字动画提前",
                    "start": 78.0,
                    "end": 88.0,
                    "detail": "01:28 橙色字动画提前到01:18",
                }
            ],
            "review_items": [
                {
                    "id": "校对01",
                    "source_text": "01:28 橙色字动画提前到01:18",
                    "kind": "animation_timing",
                    "execution_required": True,
                }
            ],
        }
        content = {
            "duration": 400000000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 400000000}}],
                },
                {
                    "name": "校对标记1",
                    "type": "text",
                    "segments": [{"target_timerange": {"start": 78000000, "duration": 10000000}}],
                },
            ],
            "materials": {"videos": [{"path": "C:/media/source.mp4"}], "audios": [], "texts": [{}]},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("markers are not execution evidence" in message for message in validation["errors"])
        )

    def test_strict_acceptance_rejects_animation_without_scope_completion_contract(self):
        request = self._conditional_request(
            kind="animation_timing",
            op_type="visual_overlay",
            evidence={
                "executed": True,
                "operation": "animation_timing",
                "edit_mode": "full_screen_state_overlay",
                "track_name": "Animation Overlay",
                "segment_id": "animation-segment-1",
                "asset_path": "C:/media/animation-state.png",
                "first_visible": 1.0,
                "stable_frame": 1.2,
                "release": 1.5,
                "next_animation_start": 2.0,
            },
            validation={"status": "pass"},
        )
        content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Animation Overlay",
                    "type": "video",
                    "segments": [
                        {
                            "id": "animation-segment-1",
                            "material_id": "animation-material-1",
                            "target_timerange": {
                                "start": 1_000_000,
                                "duration": 500_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {
                "videos": [
                    {"id": "source-material", "path": "C:/media/source.mp4"},
                    {
                        "id": "animation-material-1",
                        "path": "C:/media/animation-state.png",
                    },
                ],
                "audios": [],
                "texts": [],
            },
        }

        non_strict_validation = validate_revision_acceptance(
            request,
            content,
            strict=False,
        )
        validation = validate_revision_acceptance(request, content, strict=True)

        self.assertTrue(non_strict_validation["ok"], non_strict_validation["errors"])
        self.assertFalse(validation["ok"])
        self.assertIn("animation", validation["metrics"]["enabled_gates"])
        self.assertIn("item01", validation["metrics"]["animation_evidence_errors"])
        self.assertTrue(
            any(
                failure["gate"] == "animation" and failure["item_id"] == "item01"
                for failure in validation["failures"]
            ),
            validation["failures"],
        )
        self.assertTrue(
            any(
                "animation evidence" in error and "target_scope_invalid" in error
                for error in validation["errors"]
            ),
            validation["errors"],
        )

    def test_acceptance_variants_reject_active_timeline_missing_animation_overlay(self):
        evidence = {
            "executed": True,
            "operation": "animation_timing",
            "edit_mode": "full_screen_state_overlay",
            "track_name": "Animation Overlay",
            "segment_id": "animation-segment-1",
            "asset_path": "C:/media/animation-state.png",
            "target_scope": "roi_target",
            "completion_policy": "target_only",
            "target_anchor_state": "stable_complete",
            "scope_basis": {
                "source_text": "item01 requested edit",
                "screenshot_roi": {
                    "x": 100,
                    "y": 100,
                    "width": 200,
                    "height": 100,
                    "canvas_width": 1920,
                    "canvas_height": 1080,
                },
                "frame_events": [
                    {"event": "line_visible", "source_time": 1.0},
                    {"event": "line_stable", "source_time": 1.1},
                ],
            },
            "required_elements": ["line"],
            "forbidden_future_elements": [],
            "confidence": "high",
            "first_visible": 1.0,
            "stable_frame": 1.1,
            "release": 1.5,
            "next_animation_start": 2.0,
            "completion_receipt": {
                "status": "pass",
                "stable_sample_count": 2,
                "required_elements_present": ["line"],
                "forbidden_future_elements_present": [],
            },
        }
        request = self._conditional_request(
            kind="animation_timing",
            op_type="visual_overlay",
            evidence=evidence,
            validation={"status": "pass"},
        )
        root_content = {
            "duration": 3_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 3_000_000}}],
                },
                {
                    "name": "Animation Overlay",
                    "type": "video",
                    "segments": [
                        {
                            "id": "animation-segment-1",
                            "material_id": "animation-material-1",
                            "target_timerange": {
                                "start": 1_000_000,
                                "duration": 500_000,
                            },
                        }
                    ],
                },
            ],
            "materials": {
                "videos": [
                    {"id": "source-material", "path": "C:/media/source.mp4"},
                    {
                        "id": "animation-material-1",
                        "path": "C:/media/animation-state.png",
                    },
                ],
                "audios": [],
                "texts": [],
            },
        }
        active_content = json.loads(json.dumps(root_content))
        active_content["tracks"] = [active_content["tracks"][0]]
        active_content["materials"]["videos"] = [active_content["materials"]["videos"][0]]

        validation = revision_runner_api.validate_revision_acceptance_variants(
            request,
            [
                ("root", root_content),
                ("active_timeline:active", active_content),
            ],
            strict=True,
        )

        json.dumps(validation)
        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                "[active_timeline:active]" in error and "no non-marker overlay" in error
                for error in validation["errors"]
            ),
            validation["errors"],
        )

    def test_strict_acceptance_requires_audio_validation_for_spoken_delete(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 27.0,
                    "end": 45.0,
                    "label": "修改02 删除解释段",
                    "detail": "00:27-00:45 删除“为什么呢……古史的文字记载，对吧？”",
                }
            ],
            "markers": [],
            "review_items": [
                {
                    "id": "修改02",
                    "source_text": "00:27-00:45 删除“为什么呢……古史的文字记载，对吧？”",
                    "kind": "spoken_delete",
                    "execution_required": True,
                    "evidence": {
                        "executed": True,
                        "cut_window": [27.0, 45.0],
                    },
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any(
                "delete/must-keep validation evidence" in message
                for message in validation["errors"]
            )
        )

    def test_strict_acceptance_rejects_unresolved_audio_validation_status(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 27.0,
                    "end": 45.0,
                    "label": "\u4fee\u653909",
                    "detail": "delete phrase",
                }
            ],
            "review_items": [
                {
                    "id": "\u4fee\u653909",
                    "source_text": "\u4fee\u653909 delete phrase",
                    "kind": "spoken_delete",
                    "execution_required": True,
                    "evidence": {
                        "status": "executed",
                        "executed": True,
                        "cut_window": [27.0, 45.0],
                    },
                    "validation": {"status": "review"},
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["audio_unresolved_validation"], ["\u4fee\u653909"])
        self.assertTrue(
            any("unresolved audio validation status" in message for message in validation["errors"])
        )

    def test_strict_acceptance_rejects_unresolved_processed_audio_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            summary_path = os.path.join(tmpdir, "summary.json")
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "status_counts": {"pass": 1, "review": 1},
                        "review_ids": ["audio09"],
                        "pending_ids": [],
                    },
                    f,
                    ensure_ascii=False,
                )
            payload = {
                "project": {
                    "draft_name": "ReviewDraft",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                    "replacement_audio": "C:/media/processed.wav",
                },
                "processed_audio": {"validation_summary": summary_path},
                "edits": [
                    {
                        "type": "delete",
                        "start": 27.0,
                        "end": 45.0,
                        "label": "audio01 delete phrase",
                    }
                ],
                "review_items": [
                    {
                        "id": "audio01",
                        "source_text": "audio01 delete phrase",
                        "kind": "spoken_delete",
                        "execution_required": True,
                        "evidence": {
                            "status": "executed",
                            "executed": True,
                            "cut_window": [27.0, 45.0],
                        },
                        "validation": {"status": "pass"},
                    }
                ],
                "preserve": {"replacement_audio_material": True},
            }
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)
            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertIn("audio09", validation["metrics"]["processed_audio_summary"]["unresolved_ids"])
        self.assertTrue(
            any(
                "Processed audio reverse validation has unresolved rows" in message
                for message in validation["errors"]
            )
        )

    def test_strict_acceptance_rejects_nested_reverse_asr_report_reviews(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested_path = os.path.join(tmpdir, "summary_selected.json")
            with open(nested_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "status_counts": {"review": 1},
                        "rows": [{"item": "audio09", "status": "review"}],
                    },
                    f,
                    ensure_ascii=False,
                )
            summary_path = os.path.join(tmpdir, "summary.json")
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "status_counts": {"pass": 2},
                        "fail_ids": [],
                        "review_ids": [],
                        "pending_ids": [],
                        "reverse_asr_reports": [nested_path],
                    },
                    f,
                    ensure_ascii=False,
                )
            payload = {
                "project": {
                    "draft_name": "ReviewDraft",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                    "replacement_audio": "C:/media/processed.wav",
                },
                "processed_audio": {"validation_summary": summary_path},
                "edits": [
                    {
                        "type": "delete",
                        "start": 27.0,
                        "end": 45.0,
                        "label": "audio01 delete phrase",
                    }
                ],
                "review_items": [
                    {
                        "id": "audio01",
                        "source_text": "audio01 delete phrase",
                        "kind": "spoken_delete",
                        "execution_required": True,
                        "evidence": {
                            "status": "executed",
                            "executed": True,
                            "cut_window": [27.0, 45.0],
                        },
                        "validation": {"status": "pass"},
                    }
                ],
                "preserve": {"replacement_audio_material": True},
            }
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)
            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        summary = validation["metrics"]["processed_audio_summary"]
        self.assertIn("summary_selected.json:review:1", summary["unresolved_statuses"])
        self.assertIn("summary_selected.json:audio09", summary["unresolved_ids"])

    def test_strict_acceptance_rejects_semantic_join_anomalies_from_reverse_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            summary_path = os.path.join(tmpdir, "summary.json")
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "status_counts": {"pass": 1},
                        "rows": [
                            {
                                "id": "\u4fee\u653917",
                                "status": "pass",
                                "local_asr_text": "\u5305\u62ec\u6587\u5b57\u7684\u53d1\u53d1\u660e\u3002",
                                "keep_hits": {"\u53d1\u660e": True},
                            }
                        ],
                    },
                    f,
                    ensure_ascii=False,
                )
            payload = {
                "project": {
                    "draft_name": "ReviewDraft",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                    "replacement_audio": "C:/media/processed.wav",
                },
                "processed_audio": {"validation_summary": summary_path},
                "edits": [
                    {
                        "type": "delete",
                        "start": 200.0,
                        "end": 201.0,
                        "label": "\u4fee\u653917",
                    }
                ],
                "review_items": [
                    {
                        "id": "\u4fee\u653917",
                        "source_text": "\u4fee\u653917 delete phrase",
                        "kind": "spoken_delete",
                        "execution_required": True,
                        "evidence": {"executed": True, "cut_window": [200.0, 201.0]},
                        "validation": {"status": "pass"},
                    }
                ],
                "acceptance": {"require_audio_validation": True},
            }
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        summary = validation["metrics"]["processed_audio_summary"]
        self.assertIn(
            "\u4fee\u653917:semantic_join:\u53d1\u53d1\u660e", summary["semantic_join_anomalies"]
        )
        self.assertTrue(
            any("semantic join anomalies" in message for message in validation["errors"])
        )

    def test_strict_acceptance_allows_adjudicated_asr_punctuation_join(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "final-candidate.wav")
            candidate_bytes = _test_wav_bytes()
            with open(candidate_path, "wb") as f:
                f.write(candidate_bytes)
            summary_path = os.path.join(tmpdir, "summary.json")
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "candidate_audio_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                        "asr_identity": {
                            "provider": "test-provider",
                            "model": "test-model",
                            "adapter_version": "1",
                        },
                        "summary": {"pass": 1, "review": 0, "fail": 0},
                        "items": [
                            {
                                "id": "\u4fee\u653911",
                                "status": "pass",
                                "strategy": "hybrid",
                                "delete": "removed phrase",
                                "must_keep": [
                                    "\u7684\u8fd9\u4e2a\u9636\u6bb5\u6027",
                                    "\u6210\u5c31\u5f53\u7136\u8fd9\u4e9b",
                                ],
                                "source_cut_windows": [[127.85, 139.54]],
                                "mapped_join_times": [127.85],
                                "local_asr_text": "\u8fd9\u4e2a\u9636\u6bb5\u6027\u3002\u6210\u5c31\u3002\u5f53\u7136\u8fd9\u4e9b",
                                "delete_hits": [],
                                "keep_hits": {
                                    "\u7684\u8fd9\u4e2a\u9636\u6bb5\u6027": True,
                                    "\u6210\u5c31\u5f53\u7136\u8fd9\u4e9b": True,
                                },
                                "semantic_join_validation": {
                                    "status": "pass_adjudicated",
                                    "adjudicated_patterns": [
                                        "\u9636\u6bb5\u6027\u3002\u6210\u5c31"
                                    ],
                                    "reason": "ASR punctuation only; final acoustic gap is 0.03s and no-extra-deletion contract passed.",
                                    "final_gap": 0.03,
                                    "no_extra_deletion_contract": "pass",
                                },
                            }
                        ],
                    },
                    f,
                    ensure_ascii=False,
                )
            payload = {
                "project": {
                    "draft_name": "ReviewDraft",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                    "replacement_audio": "C:/media/processed.wav",
                },
                "processed_audio": {
                    "output_wav": candidate_path,
                    "validation_summary": summary_path,
                },
                "edits": [
                    {
                        "type": "delete",
                        "start": 127.85,
                        "end": 139.54,
                        "label": "\u4fee\u653911",
                    }
                ],
                "review_items": [
                    {
                        "id": "\u4fee\u653911",
                        "source_text": "\u4fee\u653911 delete phrase",
                        "kind": "spoken_delete",
                        "execution_required": True,
                        "evidence": {
                            "executed": True,
                            "cut_window": [127.85, 139.54],
                            "strategy": "hybrid",
                            "delete": "removed phrase",
                            "must_keep": [
                                "\u7684\u8fd9\u4e2a\u9636\u6bb5\u6027",
                                "\u6210\u5c31\u5f53\u7136\u8fd9\u4e9b",
                            ],
                        },
                        "validation": {"status": "pass"},
                    }
                ],
                "acceptance": {"require_audio_validation": True},
            }
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            validation = validate_revision_acceptance(request, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])
        summary = validation["metrics"]["processed_audio_summary"]
        self.assertEqual([], summary["semantic_join_anomalies"])

    def test_semantic_join_forbidden_phrase_requires_attributable_adjudication(self):
        phrase = next(iter(revision_validation_api._SEMANTIC_JOIN_FORBIDDEN_PHRASES))
        invalid_adjudications = [
            {
                "status": "pass",
                "adjudicated_patterns": [phrase],
            },
            {
                "status": "pass_adjudicated",
                "adjudicated_patterns": [phrase],
                "final_gap": 0.03,
                "no_extra_deletion_contract": "pass",
            },
            {
                "status": "pass_adjudicated",
                "adjudicated_patterns": [phrase],
                "reason": "Claimed punctuation artifact.",
                "final_gap": 0.5,
                "no_extra_deletion_contract": "pass",
            },
            {
                "status": "pass_adjudicated",
                "adjudicated_patterns": [phrase],
                "reason": "Claimed punctuation artifact.",
                "final_gap": 0.03,
                "no_extra_deletion_contract": "fail",
            },
        ]

        for semantic_validation in invalid_adjudications:
            with self.subTest(semantic_validation=semantic_validation):
                anomalies = revision_validation_api._collect_semantic_join_anomalies(
                    {
                        "rows": [
                            {
                                "id": "item01",
                                "local_asr_text": f"before{phrase}after",
                                "semantic_join_validation": semantic_validation,
                            }
                        ]
                    }
                )

                self.assertIn(f"item01:semantic_join:{phrase}", anomalies)

    def test_semantic_join_scan_reads_local_joined_text_alias(self):
        phrase = next(iter(revision_validation_api._SEMANTIC_JOIN_FORBIDDEN_PHRASES))

        anomalies = revision_validation_api._collect_semantic_join_anomalies(
            {
                "rows": [
                    {
                        "id": "item01",
                        "local_joined_text": f"before{phrase}after",
                        "semantic_join_validation": {"status": "pass"},
                    }
                ]
            }
        )

        self.assertIn(f"item01:semantic_join:{phrase}", anomalies)

    def test_strict_acceptance_rejects_visual_timeline_without_mapping_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            timeline_plan_path = os.path.join(tmpdir, "timeline_plan.json")
            with open(timeline_plan_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "replacement_source_window": [453.0, 582.0],
                        "replacement_timeline_window": [404.9, 533.9],
                    },
                    f,
                    ensure_ascii=False,
                )
            payload = {
                "project": {
                    "draft_name": "ReviewDraft",
                    "source_video": "C:/media/source.mp4",
                },
                "processed_audio": {"timeline_plan": timeline_plan_path},
                "edits": [
                    {
                        "type": "visual_overlay",
                        "doc_item_id": "\u4fee\u653926",
                        "start": 453.0,
                        "end": 582.0,
                        "label": "\u4fee\u653926 replacement",
                        "visual_plan": {
                            "segments": [
                                {
                                    "role": "replacement_video_keep_1",
                                    "asset_path": "C:/media/replacement.mp4",
                                    "timeline_start": 408.4,
                                    "source_start": 453.0,
                                    "duration": 2.0,
                                }
                            ]
                        },
                    }
                ],
                "review_items": [
                    {
                        "id": "\u4fee\u653926",
                        "kind": "visual_overlay",
                        "source_text": "\u4fee\u653926 replacement",
                        "execution_required": True,
                        "evidence": {
                            "executed": True,
                            "operation": "replacement_video_overlay",
                            "source_window": [453.0, 582.0],
                            "timeline_window": [408.4, 537.4],
                        },
                        "validation": {"status": "pass"},
                    }
                ],
                "acceptance": {
                    "require_review_items": True,
                    "require_visual_evidence": False,
                },
            }
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertIn("\u4fee\u653926", validation["metrics"]["timeline_mapping_errors"][0])
        self.assertTrue(any("timeline-plan mapping" in message for message in validation["errors"]))

    def test_strict_acceptance_rejects_pointer_planned_anchor_without_landing_proof(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
            },
            "edits": [
                {
                    "type": "pointer_overlay",
                    "doc_item_id": "\u4fee\u653904",
                    "start": 81.0,
                    "end": 83.8,
                    "label": "\u4fee\u653904 pointer",
                }
            ],
            "review_items": [
                {
                    "id": "\u4fee\u653904",
                    "kind": "pointer_overlay",
                    "source_text": "\u4fee\u653904 pointer",
                    "execution_required": True,
                    "evidence": {
                        "executed": True,
                        "operation": "pointer_overlay",
                        "target_point": [765, 690],
                        "anchor": [0.07, 0.05],
                        "scale_rule": "same_lesson_source_video_pointer_reference",
                        "validation": {
                            "status": "pass",
                            "method": "planned_anchor_overlay",
                        },
                    },
                    "validation": {
                        "status": "pass",
                        "method": "planned_editable_overlay",
                    },
                }
            ],
            "acceptance": {
                "require_review_items": True,
                "require_visual_evidence": False,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

        validation = validate_revision_acceptance(request, strict=True)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["pointer_landing_errors"], ["\u4fee\u653904"])
        self.assertTrue(any("pointer landing" in message for message in validation["errors"]))

    def test_doc_items_do_not_override_request_audio_validation_evidence(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 27.0,
                    "end": 45.0,
                    "label": "\u4fee\u653909",
                    "detail": "delete phrase",
                }
            ],
            "review_items": [
                {
                    "id": "\u4fee\u653909",
                    "source_text": "\u4fee\u653909 delete phrase",
                    "kind": "spoken_delete",
                    "execution_required": True,
                    "evidence": {
                        "status": "executed",
                        "executed": True,
                        "cut_window": [27.0, 45.0],
                        "strategy": "hybrid",
                        "delete": "removed phrase",
                        "must_keep": [],
                    },
                    "validation": {"status": "pass_adjudicated"},
                }
            ],
            "acceptance": {
                "expected_review_item_count": 1,
                "expected_review_item_ids": ["\u4fee\u653909"],
                "require_audio_validation": True,
            },
        }
        doc_items_payload = [
            {
                "id": "\u4fee\u653909",
                "source_text": "source ledger row",
                "kind": "spoken_delete",
                "execution_required": True,
                "evidence": {"status": "executed", "cut_window": [27.0, 45.0]},
                "validation": {"status": "review"},
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            payload["processed_audio"] = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[
                    {
                        "id": "\u4fee\u653909",
                        "status": "pass_adjudicated",
                        "delete": "removed phrase",
                        "must_keep": [],
                        "source_cut_windows": [[27.0, 45.0]],
                        "mapped_join_times": [27.0],
                    }
                ],
            )
            request_path = os.path.join(tmpdir, "request.json")
            doc_items_path = os.path.join(tmpdir, "doc_items.json")
            with open(request_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            with open(doc_items_path, "w", encoding="utf-8") as f:
                json.dump({"items": doc_items_payload}, f, ensure_ascii=False, indent=2)
            request = load_revision_request(request_path)
            from utils.revision_runner import load_review_items_json

            doc_items = load_review_items_json(doc_items_path)

            validation = validate_revision_acceptance(request, strict=True, doc_items=doc_items)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(validation["metrics"]["audio_unresolved_validation"], [])

    def test_canonical_doc_item_routes_spoken_contract_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            request = self._conditional_request(
                kind="bgm_replace",
                op_type="replace_audio",
                source_kind="bgm_replace",
                evidence={"executed": True},
                validation={"status": "pass"},
                processed_audio=self._write_full_candidate_reverse_asr(tmpdir),
            )
            doc_items = [
                RevisionReviewItem(
                    "item01",
                    "spoken_delete",
                    "Canonical spoken delete",
                    execution_required=True,
                    evidence={"executed": True},
                    validation={"status": "pass"},
                )
            ]

            validation = validate_revision_acceptance(
                request,
                strict=True,
                doc_items=doc_items,
            )

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("item contract: strategy is missing" in error for error in validation["errors"]),
            validation["errors"],
        )

    def test_semantic_pause_does_not_skip_same_item_spoken_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            processed_audio = self._write_full_candidate_reverse_asr(tmpdir)
            request = self._conditional_request(
                kind="spoken_delete",
                op_type="delete",
                evidence={"executed": True, "cut_window": [1.0, 2.0]},
                validation={
                    "status": "pass",
                    "semantic_pause_adjustment": {"status": "pass"},
                },
                processed_audio=processed_audio,
            )

            with patch.object(
                revision_validation_api,
                "_semantic_pause_reverse_asr_problems",
                return_value=[],
            ):
                result = revision_validation_api._validate_processed_audio_summary(
                    request,
                    required_item_ids=("item01",),
                    semantic_pause_item_ids=("item01",),
                    spoken_contract_item_ids=("item01",),
                )

        self.assertTrue(
            any("item contract: strategy is missing" in error for error in result["errors"]),
            result["errors"],
        )

    def test_strict_acceptance_accepts_spoken_delete_with_reverse_validation(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 27.0,
                    "end": 45.0,
                    "label": "修改02 删除解释段",
                    "detail": "00:27-00:45 删除“为什么呢……古史的文字记载，对吧？”",
                }
            ],
            "markers": [],
            "review_items": [
                {
                    "id": "修改02",
                    "source_text": "00:27-00:45 删除“为什么呢……古史的文字记载，对吧？”",
                    "kind": "spoken_delete",
                    "execution_required": True,
                    "evidence": {
                        "executed": True,
                        "cut_window": [27.0, 45.0],
                        "strategy": "hybrid",
                        "delete": "removed phrase",
                        "must_keep": [],
                        "validation": {"status": "pass"},
                    },
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            payload["processed_audio"] = self._write_full_candidate_reverse_asr(
                tmpdir,
                rows=[
                    {
                        "id": "\u4fee\u653902",
                        "status": "pass",
                        "delete": "removed phrase",
                        "must_keep": [],
                        "source_cut_windows": [[27.0, 45.0]],
                        "mapped_join_times": [27.0],
                    }
                ],
            )
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            validation = validate_revision_acceptance(request, strict=True)

        self.assertTrue(validation["ok"], validation["errors"])

    def test_execute_revision_request_falls_back_to_new_draft_when_overwrite_is_blocked(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
                "replacement_audio": "C:/media/replacement.mp3",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 0.0,
                    "end": 3.0,
                    "label": "Remove opener",
                }
            ],
            "markers": [],
            "preserve": {
                "source_video_material": True,
                "separated_audio_material": True,
                "replacement_audio_material": True,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            payload["processed_audio"] = self._write_full_candidate_reverse_asr(tmpdir)
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            call_names = []
            ui_modes = []
            real_import = __import__(
                "utils.revision_runner", fromlist=["_import_runtime_components"]
            )
            draft_mod, review_marker_item, mock_audio, mock_video, real_jy_project = (
                real_import._import_runtime_components()
            )

            def fake_jy_project(name, drafts_root=None, overwrite=True, ui_mode=None):
                call_names.append(name)
                ui_modes.append(ui_mode)
                if name == "ReviewDraft":
                    raise RuntimeError(
                        "JianYing is editing another project or showing export UI. "
                        "To avoid corrupting the target draft, overwrite is blocked until "
                        "JianYing returns to the home/drafts page."
                    )
                return real_jy_project(
                    name,
                    drafts_root=drafts_root,
                    overwrite=overwrite,
                    ui_mode=ui_mode,
                )

            with patch("utils.revision_runner._import_runtime_components") as import_mock:
                import_mock.return_value = (
                    draft_mod,
                    review_marker_item,
                    mock_audio,
                    mock_video,
                    fake_jy_project,
                )
                result = execute_revision_request(request, drafts_root=tmpdir, mock_media=True)

        self.assertEqual(call_names[0], "ReviewDraft")
        self.assertTrue(all(mode == "offline" for mode in ui_modes), ui_modes)
        self.assertTrue(result["draft_name"].startswith("ReviewDraft__fallback_"))
        self.assertEqual(result["requested_draft_name"], "ReviewDraft")
        self.assertEqual(result["write_mode"], "fallback_new_draft")
        self.assertIn("overwrite is blocked", result["fallback_reason"])

    def test_open_revision_project_explicitly_uses_offline_mode(self):
        calls = []

        class FakeProject:
            def __init__(self, name, **kwargs):
                calls.append((name, kwargs))

        project, write_info = revision_runner_api._open_revision_project(
            FakeProject,
            "ReviewDraft",
            drafts_root="C:/drafts",
        )

        self.assertIsInstance(project, FakeProject)
        self.assertEqual(write_info["draft_name"], "ReviewDraft")
        self.assertEqual(calls[0][1]["ui_mode"], "offline")

    def test_execute_revision_request_cleans_incomplete_new_draft_on_failure(self):
        payload = {
            "project": {
                "draft_name": "BrokenDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 0.0,
                    "end": 3.0,
                    "label": "Remove opener",
                }
            ],
            "markers": [],
            "preserve": {
                "source_video_material": True,
                "separated_audio_material": True,
                "replacement_audio_material": True,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            real_import = __import__(
                "utils.revision_runner", fromlist=["_import_runtime_components"]
            )
            draft_mod, review_marker_item, mock_audio, mock_video, real_jy_project = (
                real_import._import_runtime_components()
            )

            with patch.object(
                real_jy_project, "add_review_markers", side_effect=RuntimeError("boom")
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    execute_revision_request(request, drafts_root=tmpdir, mock_media=True)

            self.assertFalse(os.path.exists(os.path.join(tmpdir, "BrokenDraft")))

    def test_execute_revision_request_cleans_failed_fallback_without_retaining_first(self):
        payload = {
            "project": {
                "draft_name": "ReviewDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 0.0,
                    "end": 3.0,
                    "label": "Remove opener",
                }
            ],
            "markers": [],
            "preserve": {
                "source_video_material": True,
                "separated_audio_material": True,
                "replacement_audio_material": True,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            old_fallback = os.path.join(tmpdir, "ReviewDraft__fallback_previous_ok")
            os.makedirs(old_fallback, exist_ok=True)
            with open(os.path.join(old_fallback, "draft_content.json"), "w", encoding="utf-8") as f:
                f.write("{}")

            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(path)

            real_import = __import__(
                "utils.revision_runner", fromlist=["_import_runtime_components"]
            )
            draft_mod, review_marker_item, mock_audio, mock_video, real_jy_project = (
                real_import._import_runtime_components()
            )

            ui_modes = []

            def fake_jy_project(name, drafts_root=None, overwrite=True, ui_mode=None):
                ui_modes.append(ui_mode)
                if name == "ReviewDraft":
                    raise RuntimeError(
                        "JianYing is editing another project or showing export UI. "
                        "To avoid corrupting the target draft, overwrite is blocked until "
                        "JianYing returns to the home/drafts page."
                    )
                return real_jy_project(
                    name,
                    drafts_root=drafts_root,
                    overwrite=overwrite,
                    ui_mode=ui_mode,
                )

            with patch("utils.revision_runner._import_runtime_components") as import_mock:
                import_mock.return_value = (
                    draft_mod,
                    review_marker_item,
                    mock_audio,
                    mock_video,
                    fake_jy_project,
                )
                with patch(
                    "utils.revision_runner.validate_saved_revision_draft",
                    return_value={"ok": False, "errors": ["forced validation failure"]},
                ):
                    with self.assertRaisesRegex(RuntimeError, "forced validation failure"):
                        execute_revision_request(request, drafts_root=tmpdir, mock_media=True)

            fallback_names = sorted(
                name for name in os.listdir(tmpdir) if name.startswith("ReviewDraft__fallback_")
            )
            self.assertEqual(fallback_names, ["ReviewDraft__fallback_previous_ok"])
            self.assertTrue(all(mode == "offline" for mode in ui_modes), ui_modes)


if __name__ == "__main__":
    unittest.main()
