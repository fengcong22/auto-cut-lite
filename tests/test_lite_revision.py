# ruff: noqa: E402,I001
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CURRENT_DIR)
SCRIPTS_PATH = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, SCRIPTS_PATH)

from utils.lite_revision import (
    LITE_TRACKS,
    _asset_specs,
    _collect_lite_delete_windows,
    _localize_lite_request_materials,
    _lite_layout,
    _lite_timeline_duration,
    _lite_visual_results,
    _spoken_cut_alignment_problems,
    _source_video_duration_seconds,
    _validate_lite_content,
)
from utils.review_job_compiler import compile_review_job
from utils.revision_markers import build_marker_plan
from utils.revision_models import (
    _classify_review_text,
    lite_execution_required,
    resolve_execution_status,
)
from utils.revision_runner import (
    execute_revision_request,
    load_revision_request,
)
from utils.revision_evidence import (
    audio_delivery_plan_sha256,
)
from utils.revision_validation import derive_acceptance_profile
from core.review_marker_ops import ReviewMarkerOpsMixin


def _load_request(payload):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "request.json")
        with open(path, "w", encoding="utf-8") as request_file:
            json.dump(payload, request_file, ensure_ascii=False)
        return load_revision_request(path)


def _track(content, name):
    return next(track for track in content["tracks"] if track["name"] == name)


def _spoken_delete_edit(item_id, start, end, *, label=None):
    phrase = label or item_id
    return {
        "type": "delete",
        "source_kind": "spoken_delete",
        "start": start,
        "end": end,
        "label": phrase,
        "doc_item_id": item_id,
        "evidence": {
            "review_timestamp_role": "search_hint",
            "delete": phrase,
            "must_keep": [f"before-{item_id}", f"after-{item_id}"],
            "strategy": "precision_first",
            "asr_alignment": {
                "status": "pass",
                "provider": "test-asr",
                "model": "test-model",
                "adapter_version": "1",
                "granularity": "word",
                "input_sha256": "a" * 64,
                "authoritative_cut_boundary": True,
                "words": [{"text": phrase, "start": start, "end": end}],
                "resolved_cut_window": [start, end],
            },
        },
    }


class LiteRevisionTests(unittest.TestCase):
    def test_source_duration_keeps_longer_container_audio_tail(self):
        material = type("Material", (), {"duration": 627_480_000})()
        with patch(
            "utils.lite_revision.get_duration_ffprobe_cached",
            return_value=627.589002,
        ):
            detected = _source_video_duration_seconds(material, "source.mp4", 0.0)

        self.assertEqual(detected, 627.589002)

    def test_lite_video_track_stops_at_stream_end_while_audio_keeps_container_tail(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteContainerTail",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 627.589002,
                },
            }
        )

        def shorter_video(_draft, mock_video, path, _duration, _mock):
            return mock_video(
                "mock-lite-video-source",
                627_480_000,
                "source.mp4",
                path,
            )

        with (
            tempfile.TemporaryDirectory() as drafts_root,
            patch(
                "utils.lite_revision._make_video_material",
                side_effect=shorter_video,
            ),
        ):
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=True,
            )
            with open(
                os.path.join(result["draft_path"], "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        video_segment = _track(content, LITE_TRACKS["original_video"])["segments"][0]
        audio_segment = _track(content, LITE_TRACKS["source_audio"])["segments"][0]
        self.assertEqual(video_segment["source_timerange"]["duration"], 627_480_000)
        self.assertEqual(audio_segment["source_timerange"]["duration"], 627_589_002)
        self.assertEqual(content["duration"], 627_589_002)
        self.assertEqual(result["source_video_duration_seconds"], 627.48)
        self.assertEqual(result["source_duration_seconds"], 627.589002)
        self.assertTrue(result["validation"]["ok"], result["validation"])

    def test_lite_delete_windows_keep_adjacent_source_items_separate(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "AdjacentLiteCuts",
                    "source_video": "source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {"type": "delete", "start": 2.0, "end": 3.0, "doc_item_id": "a"},
                    {"type": "delete", "start": 3.0, "end": 4.0, "doc_item_id": "b"},
                ],
            }
        )

        windows = _collect_lite_delete_windows(request, 10.0)

        self.assertEqual(
            [(row.item_id, row.start, row.end) for row in windows],
            [("a", 2.0, 3.0), ("b", 3.0, 4.0)],
        )

    def test_lite_delete_windows_merge_only_within_same_source_item(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "SameItemLiteCuts",
                    "source_video": "source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {"type": "delete", "start": 2.0, "end": 3.0, "doc_item_id": "a"},
                    {"type": "delete", "start": 2.5, "end": 4.0, "doc_item_id": "a"},
                ],
            }
        )

        windows = _collect_lite_delete_windows(request, 10.0)

        self.assertEqual(
            [(row.item_id, row.start, row.end) for row in windows],
            [("a", 2.0, 4.0)],
        )

    def test_lite_delete_windows_reject_overlapping_different_source_items(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "OverlappingLiteCuts",
                    "source_video": "source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {"type": "delete", "start": 2.0, "end": 3.5, "doc_item_id": "a"},
                    {"type": "delete", "start": 3.0, "end": 4.0, "doc_item_id": "b"},
                ],
            }
        )

        with self.assertRaisesRegex(ValueError, "different review items overlap"):
            _collect_lite_delete_windows(request, 10.0)

    def test_lite_copy_layout_is_rejected_before_draft_execution(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "lite_cut_layout": "copy",
                "project": {
                    "draft_name": "CopyLiteCuts",
                    "source_video": "source.mp4",
                    "media_duration_seconds": 10.0,
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "overlaps V2/A2 delete clips"):
            _lite_layout(request)

    def test_lite_package_materials_are_localized_before_import(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_dir = os.path.join(tmpdir, "media")
            draft_dir = os.path.join(tmpdir, "draft")
            os.makedirs(media_dir)
            os.makedirs(draft_dir)
            video = os.path.join(media_dir, "source.mp4")
            audio = os.path.join(media_dir, "source.wav")
            pointer = os.path.join(media_dir, "pointer.png")
            report = os.path.join(media_dir, "reverse_asr_bound.json")
            for path, payload in (
                (video, b"video"),
                (audio, b"audio"),
                (pointer, b"pointer"),
            ):
                with open(path, "wb") as material_file:
                    material_file.write(payload)
            with open(report, "w", encoding="utf-8") as report_file:
                json.dump({"audio_delivery_plan_sha256": "old", "rows": []}, report_file)

            request = _load_request(
                {
                    "workflow_mode": "lite",
                    "project": {
                        "draft_name": "LocalizedLite",
                        "source_video": video,
                        "source_audio": audio,
                    },
                    "edits": [
                        {
                            "type": "visual_overlay",
                            "start": 1.0,
                            "end": 2.0,
                            "doc_item_id": "visual-1",
                            "asset_paths": [pointer],
                            "visual_plan": {"segments": [{"asset_path": pointer, "duration": 1.0}]},
                            "evidence": {"asset_path": pointer},
                        }
                    ],
                    "audio_delivery_plan": {
                        "mode": "segmented",
                        "segments": [
                            {
                                "segment_id": "a1",
                                "role": "source",
                                "asset_path": audio,
                                "track_name": "Separated Source Audio",
                                "source_start": 0.0,
                                "timeline_start": 0.0,
                                "duration": 1.0,
                            }
                        ],
                    },
                    "processed_audio": {"validation_summary": report},
                }
            )

            localized, receipts = _localize_lite_request_materials(request, draft_dir)

            self.assertEqual(len(receipts), 3)
            localized_root = os.path.join(draft_dir, "Resources", "local")
            localized_paths = {
                localized.project.source_video,
                localized.project.source_audio,
                localized.edits[0].asset_paths[0],
            }
            self.assertTrue(
                all(
                    os.path.commonpath([localized_root, path]) == localized_root
                    for path in localized_paths
                )
            )
            self.assertTrue(all(os.path.isfile(path) for path in localized_paths))
            self.assertEqual(
                localized.edits[0].visual_plan["segments"][0]["asset_path"],
                localized.edits[0].asset_paths[0],
            )
            self.assertEqual(
                localized.edits[0].evidence["asset_path"],
                localized.edits[0].asset_paths[0],
            )
            self.assertEqual(
                localized.audio_delivery_plan.segments[0].asset_path,
                localized.project.source_audio,
            )
            self.assertTrue(
                localized.processed_audio["validation_summary"].startswith(
                    os.path.join(draft_dir, "Evidence")
                )
            )
            with open(
                localized.processed_audio["validation_summary"], "r", encoding="utf-8"
            ) as report_file:
                bound_report = json.load(report_file)
            self.assertEqual(
                bound_report["audio_delivery_plan_sha256"],
                audio_delivery_plan_sha256(localized),
            )

    def test_lite_pointer_spec_does_not_calibrate_bound_anchor(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LitePointerAnchor",
                    "source_video": "C:/media/source.mp4",
                },
                "edits": [
                    {
                        "type": "visual_overlay",
                        "source_kind": "pointer_overlay",
                        "start": 2.0,
                        "end": 3.0,
                        "doc_item_id": "pointer-1",
                        "visual_plan": {
                            "segments": [
                                {
                                    "role": "pointer_asset",
                                    "asset_path": "C:/media/pointer.png",
                                    "scale_x": 0.04,
                                    "scale_y": 0.04,
                                }
                            ]
                        },
                        "evidence": {
                            "target_point": [507.5, 664.0],
                            "target_geometry": {
                                "canvas_width": 1920,
                                "canvas_height": 1080,
                            },
                            "subject_profile_receipt": {
                                "anchor": [0.04, 0.03],
                                "media_contract": {"width": 354, "height": 354},
                            },
                        },
                    }
                ],
            }
        )

        spec = _asset_specs(request.edits[0])[0]
        self.assertEqual(spec["scale_x"], 0.04)
        self.assertNotIn("transform_x", spec)
        self.assertNotIn("transform_y", spec)

    def test_lite_pointer_results_keep_only_simple_execution_evidence(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LitePointerEvidence",
                    "source_video": "C:/media/source.mp4",
                },
                "edits": [
                    {
                        "type": "visual_overlay",
                        "source_kind": "pointer_overlay",
                        "start": 2.0,
                        "end": 3.0,
                        "doc_item_id": "pointer-1",
                        "evidence": {
                            "lifecycle_mode": "replace_recorded_pointer_then_handoff",
                            "target_point": [500.0, 600.0],
                            "subject_profile_receipt": {
                                "asset_role": "hand",
                                "scale_reference_layout": "history-layout",
                            },
                            "residual_pointer_cover": {
                                "status": "pass",
                                "mode": "transparent_roi_still_cover",
                                "cover_sha256": "a" * 64,
                            },
                        },
                    }
                ],
            }
        )
        receipts = [
            {
                "item_id": "pointer-1",
                "kind": "visual",
                "role": "pointer_asset",
                "asset_path": "C:/media/pointer.png",
                "track_name": LITE_TRACKS["visual_assets"],
                "segment_id": "pointer-segment",
                "material_id": "pointer-material",
                "timeline_start": 2.0,
                "duration": 1.0,
            },
            {
                "item_id": "pointer-1",
                "kind": "visual",
                "role": "clean_cover",
                "asset_path": "C:/media/cover.png",
                "track_name": LITE_TRACKS["visual_assets"],
                "segment_id": "cover-segment",
                "material_id": "cover-material",
                "timeline_start": 2.0,
                "duration": 1.0,
            },
        ]

        evidence = _lite_visual_results(
            request,
            receipts,
            source_track_name=LITE_TRACKS["original_video"],
            source_material_id="source-material",
        )[0]["evidence"]

        self.assertEqual(evidence["segment_id"], "pointer-segment")
        self.assertEqual(evidence["track_name"], LITE_TRACKS["visual_assets"])
        self.assertTrue(evidence["executed"])
        self.assertNotIn("subject_profile_receipt", evidence)
        self.assertNotIn("residual_pointer_cover", evidence)

    def test_lite_execution_ignores_requested_visual_geometry(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteDefaultGeometry",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {
                        "type": "visual_overlay",
                        "source_kind": "pointer_overlay",
                        "start": 2.0,
                        "end": 3.0,
                        "doc_item_id": "pointer-geometry",
                        "visual_plan": {
                            "segments": [
                                {
                                    "role": "pointer_asset",
                                    "asset_path": "C:/media/pointer.png",
                                    "scale_x": 0.04,
                                    "scale_y": 0.05,
                                    "transform_x": -315.0,
                                    "transform_y": 120.0,
                                    "rotation": 3.0,
                                    "alpha": 0.9,
                                }
                            ]
                        },
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=True,
            )

        segment = result["visual_overlay_results"][0]["segments"][0]
        self.assertEqual(segment["timeline_start"], 2.0)
        self.assertEqual(segment["scale_x"], 1.0)
        self.assertEqual(segment["scale_y"], 1.0)
        self.assertEqual(segment["transform_x"], 0.0)
        self.assertEqual(segment["transform_y"], 0.0)

    def test_review_job_compiler_preserves_lite_mode_with_precision_audio_gate(self):
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {
                    "review_items": [
                        {
                            "id": "item-1",
                            "kind": "spoken_delete",
                            "source_text": "删除这一段",
                            "start": 1.0,
                            "end": 2.0,
                        }
                    ]
                },
                {
                    "draft_name": "LiteCompiledDraft",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["revision_request"], "r", encoding="utf-8") as request_file:
                request_payload = json.load(request_file)

        self.assertEqual(request_payload["workflow_mode"], "lite")
        self.assertEqual(request_payload["audio_delivery_plan"]["mode"], "segmented")
        self.assertTrue(request_payload["acceptance"]["require_audio_validation"])
        self.assertEqual(request_payload["review_items"][0]["review_timestamp_role"], "search_hint")

    def test_lite_compiler_uses_target_timestamp_only_for_non_speech_item(self):
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {
                    "review_items": [
                        {
                            "id": "pause-1",
                            "kind": "semantic_pause_adjustment",
                            "source_text": "01：48，音频需要停顿一秒，留出ppt切换的时间",
                        },
                        {
                            "id": "animation-1",
                            "kind": "animation_timing",
                            "source_text": "07：14，刷色动画提前到07：12",
                        },
                    ]
                },
                {
                    "draft_name": "LiteTimingSources",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                payload = json.load(source_file)

        items = {item["id"]: item for item in payload["review_items"]}
        pause = items["pause-1"]
        animation = items["animation-1"]
        self.assertNotIn("start", pause)
        self.assertEqual(pause["evidence"]["timing_source"], "asr")
        self.assertEqual(pause["evidence"]["review_search_hint_seconds"], 108.0)
        self.assertEqual(pause["timebase"]["status"], "pending_asr")
        self.assertEqual(animation["start"], 432.0)
        self.assertNotIn("end", animation)
        self.assertEqual(animation["evidence"]["original_time"], 434.0)
        self.assertEqual(animation["evidence"]["target_time"], 432.0)
        self.assertEqual(
            animation["evidence"]["review_timestamp_parse"],
            "target_after_cue",
        )
        self.assertEqual(animation["timebase"]["status"], "resolved_point")
        self.assertEqual(
            animation["evidence"]["review_timestamp_role"],
            "authoritative_non_speech",
        )
        self.assertNotIn("animation-1", payload["unresolved_timebase_item_ids"])

    def test_lite_visual_object_wording_never_routes_picture_labels_to_asr(self):
        rows = [
            {
                "id": "move-red-circle",
                "source_text": "02:23，‘蜀’上的红圈，提前到02:22",
            },
            {
                "id": "delete-red-circle",
                "source_text": "02:17，删除‘蜀’上的红圈",
            },
            {
                "id": "delete-circle-text",
                "source_text": "03:10，删除圈中文字",
            },
        ]
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {"review_items": rows},
                {
                    "draft_name": "VisualObjectLabels",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                items = {item["id"]: item for item in json.load(source_file)["review_items"]}

        move = items["move-red-circle"]
        self.assertEqual(move["kind"], "animation_timing")
        self.assertFalse(move["execution_required"])
        self.assertEqual(move["start"], 142.0)
        self.assertEqual(move["evidence"]["original_time"], 143.0)
        self.assertEqual(move["evidence"]["target_time"], 142.0)
        for item_id, expected_start in (
            ("delete-red-circle", 137.0),
            ("delete-circle-text", 190.0),
        ):
            item = items[item_id]
            self.assertEqual(item["kind"], "visual_delete")
            self.assertFalse(item["execution_required"])
            self.assertEqual(item["start"], expected_start)
            self.assertEqual(item["evidence"]["timing_source"], "review_timestamp")

    def test_lite_content_and_animation_changes_override_stale_delete_labels(self):
        rows = [
            {
                "id": "content-edit",
                "kind": "phrase_delete",
                "source_text": "04:23，绿圈中的文字，改为“公元229年”",
            },
            {
                "id": "animation-direction",
                "kind": "visual_overlay",
                "source_text": "05:00，红线动画的方向，改为从左到右",
            },
            {
                "id": "spoken-delete",
                "source_text": "05:10，删除“普通删词”",
            },
        ]
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {"review_items": rows},
                {
                    "draft_name": "ContentClassification",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                items = {item["id"]: item for item in json.load(source_file)["review_items"]}

        self.assertEqual(items["content-edit"]["kind"], "visual_content_edit")
        self.assertFalse(items["content-edit"]["execution_required"])
        self.assertEqual(items["animation-direction"]["kind"], "animation_timing")
        self.assertFalse(items["animation-direction"]["execution_required"])
        self.assertEqual(items["spoken-delete"]["kind"], "phrase_delete")
        self.assertTrue(items["spoken-delete"]["execution_required"])

    def test_lite_audio_marker_requires_asr_instead_of_zero_or_review_time(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteNoAsrPause",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 500.0,
                },
                "review_items": [
                    {
                        "id": "pause-1",
                        "kind": "semantic_pause_adjustment",
                        "source_text": "01：48，音频需要停顿一秒",
                        "start": 108.0,
                    }
                ],
            }
        )

        with self.assertRaisesRegex(ValueError, "ASR-resolved edit or pause boundary"):
            build_marker_plan(request)

    def test_lite_semantic_pause_marker_uses_asr_resolved_boundary(self):
        source_text = "01：48，音频需要停顿一秒"
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteAsrPauseMarker",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 500.0,
                },
                "edits": [
                    {
                        "type": "semantic_pause_adjustment",
                        "source_kind": "semantic_pause_adjustment",
                        "start": 109.375,
                        "end": 109.375,
                        "doc_item_id": "pause-1",
                        "evidence": {
                            "review_timestamp_role": "search_hint",
                            "asr_alignment": {
                                "status": "pass",
                                "provider": "test-asr",
                                "model": "test-model",
                                "adapter_version": "1",
                                "granularity": "word",
                                "input_sha256": "a" * 64,
                                "matches": [{"text": "停顿", "start": 109.375, "end": 109.625}],
                                "resolved_time": 109.375,
                            },
                        },
                    }
                ],
                "review_items": [
                    {
                        "id": "pause-1",
                        "kind": "semantic_pause_adjustment",
                        "source_text": source_text,
                        "start": 108.0,
                    }
                ],
            }
        )

        marker = build_marker_plan(request)[0]
        self.assertEqual(marker.start, 109.375)
        self.assertNotEqual(marker.start, 108.0)
        self.assertEqual(marker.source_text, source_text)
        self.assertEqual(marker.execution_status, "label_only_lite_policy")

    def test_lite_pending_segmented_audio_fails_before_draft_write(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LitePendingAudio",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "audio_delivery_plan": {
                    "mode": "segmented",
                    "pending": True,
                    "segments": [],
                },
            }
        )
        with tempfile.TemporaryDirectory() as drafts_root:
            with self.assertRaisesRegex(ValueError, "empty plan cannot write A1/A2"):
                execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            self.assertFalse(os.path.exists(os.path.join(drafts_root, "LitePendingAudio")))

    def test_lite_compiler_applies_label_only_animation_and_pointer_cleanup_contract(self):
        rows = [
            {
                "id": "animation-1",
                "kind": "animation_timing",
                "source_text": "00:01 动画提前并加快",
                "execution_required": True,
            },
            {
                "id": "cleanup-1",
                "kind": "pointer_overlay",
                "source_text": "00:02 小手遮挡，清除原小手",
                "execution_required": True,
            },
            {
                "id": "pointer-1",
                "kind": "pointer_overlay",
                "source_text": "00:03 添加小手素材",
                "execution_required": True,
            },
            {
                "id": "image-1",
                "kind": "visual_overlay",
                "source_text": "00:04 贴入提供的图片",
                "execution_required": True,
            },
        ]
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {"review_items": rows},
                {
                    "draft_name": "LiteBehaviorCompiler",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                doc_payload = json.load(source_file)
            with open(compiled["revision_request"], "r", encoding="utf-8") as source_file:
                request_payload = json.load(source_file)

        flags = {item["id"]: item["execution_required"] for item in doc_payload["review_items"]}
        self.assertEqual(
            flags,
            {
                "animation-1": False,
                "cleanup-1": False,
                "pointer-1": True,
                "image-1": True,
            },
        )
        enabled_gates = set(request_payload["acceptance_profile"]["enabled_gates"])
        self.assertFalse({"visual", "pointer", "animation"} & enabled_gates)

        with tempfile.TemporaryDirectory() as output_dir:
            full = compile_review_job(
                {"review_items": [rows[0]]},
                {
                    "draft_name": "FullBehaviorCompiler",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "full",
                },
                output_dir,
            )
            with open(full["doc_items"], "r", encoding="utf-8") as source_file:
                full_item = json.load(source_file)["review_items"][0]
        self.assertTrue(full_item["execution_required"])

    def test_lite_nested_label_only_status_wins_in_loader_and_compiler(self):
        nested_status = {
            "routing": {
                "attempts": [
                    {
                        "decision": {
                            "execution_status": "label_only_unresolved",
                        }
                    }
                ]
            }
        }
        self.assertEqual(
            resolve_execution_status("pending", nested_status),
            "label_only_unresolved",
        )
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteNestedLabelOnly",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "review_items": [
                    {
                        "id": "nested-evidence",
                        "kind": "visual_overlay",
                        "source_text": "02:00 add the supplied image",
                        "start": 2.0,
                        "execution_required": True,
                        "execution_status": "pending",
                        "evidence": nested_status,
                    },
                    {
                        "id": "nested-validation",
                        "kind": "visual_overlay",
                        "source_text": "03:00 add the supplied image",
                        "start": 3.0,
                        "execution_required": True,
                        "execution_status": "pending",
                        "validation": {
                            "layers": [
                                {"status": "label_only_lite_policy"},
                            ]
                        },
                    },
                ],
            }
        )
        self.assertEqual(
            [item.execution_status for item in request.review_items],
            ["label_only_unresolved", "label_only_lite_policy"],
        )
        self.assertEqual(
            [item.execution_required for item in request.review_items],
            [False, False],
        )

        rows = [
            {
                "id": "compiled-nested",
                "kind": "visual_overlay",
                "source_text": "02:00 add the supplied image",
                "start": 2.0,
                "execution_required": True,
                "execution_status": "pending",
                "evidence": nested_status,
            },
            {
                "id": "signed-before-cn",
                "kind": "review_only",
                "source_text": "05:00 +1s 停顿",
                "execution_required": True,
            },
            {
                "id": "signed-before-en",
                "kind": "review_only",
                "source_text": "06:00 -2.5s gap",
                "execution_required": True,
            },
        ]
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {"review_items": rows},
                {
                    "draft_name": "LiteNestedCompiler",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                items = {item["id"]: item for item in json.load(source_file)["review_items"]}

        self.assertEqual(
            items["compiled-nested"]["execution_status"],
            "label_only_unresolved",
        )
        self.assertFalse(items["compiled-nested"]["execution_required"])
        for item_id in ("signed-before-cn", "signed-before-en"):
            self.assertFalse(items[item_id]["execution_required"])
            self.assertEqual(
                items[item_id]["execution_status"],
                "label_only_lite_policy",
            )

    def test_lite_mixed_visual_behavior_passes_strict_acceptance(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteMixedVisualContract",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {
                        "type": "animation_timing",
                        "source_kind": "animation_timing",
                        "start": 1.0,
                        "end": 2.0,
                        "doc_item_id": "animation-1",
                    },
                    {
                        "type": "visual_overlay",
                        "source_kind": "pointer_overlay",
                        "start": 2.0,
                        "end": 3.0,
                        "doc_item_id": "pointer-1",
                        "visual_plan": {
                            "segments": [
                                {
                                    "role": "pointer_asset",
                                    "asset_path": "C:/media/pointer.png",
                                    "scale_x": 0.05,
                                    "scale_y": 0.06,
                                    "transform_x": 400.0,
                                    "transform_y": -250.0,
                                    "rotation": 15.0,
                                    "alpha": 0.4,
                                    "keyframes": [{"time": 0.0, "x": 1.0}],
                                }
                            ]
                        },
                    },
                    {
                        "type": "visual_overlay",
                        "source_kind": "pointer_overlay",
                        "start": 3.0,
                        "end": 4.0,
                        "doc_item_id": "cleanup-1",
                        "visual_plan": {
                            "segments": [
                                {
                                    "role": "clean_cover",
                                    "asset_path": "C:/media/clean-cover.png",
                                    "timeline_start": 3.0,
                                    "duration": 1.0,
                                }
                            ]
                        },
                    },
                    {
                        "type": "visual_overlay",
                        "source_kind": "visual_overlay",
                        "start": 4.0,
                        "end": 5.0,
                        "doc_item_id": "image-1",
                        "visual_plan": {
                            "segments": [
                                {
                                    "role": "visual_overlay",
                                    "asset_path": "C:/media/local-image.png",
                                    "scale_x": 0.2,
                                    "transform_x": -300.0,
                                    "keyframes": [{"time": 0.0, "scale": 0.2}],
                                }
                            ]
                        },
                    },
                ],
                "review_items": [
                    {
                        "id": "animation-1",
                        "kind": "animation_timing",
                        "source_text": "00:01 动画提前",
                        "start": 1.0,
                        "end": 2.0,
                        "execution_required": True,
                    },
                    {
                        "id": "pointer-1",
                        "kind": "pointer_overlay",
                        "source_text": "00:02 添加小手素材",
                        "start": 2.0,
                        "end": 3.0,
                        "execution_required": True,
                    },
                    {
                        "id": "cleanup-1",
                        "kind": "pointer_overlay",
                        "source_text": "00:03 小手遮挡，清除原小手",
                        "start": 3.0,
                        "end": 4.0,
                        "execution_required": True,
                    },
                    {
                        "id": "image-1",
                        "kind": "visual_overlay",
                        "source_text": "00:04 贴入提供的图片",
                        "start": 4.0,
                        "end": 5.0,
                        "execution_required": True,
                    },
                ],
                "acceptance": {
                    "require_review_items": True,
                    "expected_review_item_count": 4,
                    "expected_review_item_ids": [
                        "animation-1",
                        "pointer-1",
                        "cleanup-1",
                        "image-1",
                    ],
                    "require_visual_evidence": True,
                    "require_subject_pointer_binding": True,
                },
            }
        )
        item_flags = {item.item_id: item.execution_required for item in request.review_items}
        self.assertFalse(item_flags["animation-1"])
        self.assertFalse(item_flags["cleanup-1"])
        self.assertTrue(item_flags["pointer-1"])
        self.assertTrue(item_flags["image-1"])

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=True,
                strict=True,
            )
            with open(
                os.path.join(
                    drafts_root,
                    "LiteMixedVisualContract",
                    "draft_content.json",
                ),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        self.assertTrue(result["acceptance_validation"]["ok"])
        enabled_gates = set(result["acceptance_validation"]["metrics"]["enabled_gates"])
        self.assertFalse({"visual", "pointer", "animation"} & enabled_gates)
        self.assertEqual(len(_track(content, LITE_TRACKS["timing_adjusted"])["segments"]), 0)
        visual_segments = _track(content, LITE_TRACKS["visual_assets"])["segments"]
        self.assertEqual(len(visual_segments), 2)
        self.assertEqual(
            [row["segments"][0]["timeline_start"] for row in result["visual_overlay_results"]],
            [2.0, 4.0],
        )
        for overlay in result["visual_overlay_results"]:
            for segment in overlay["segments"]:
                self.assertEqual(segment["scale_x"], 1.0)
                self.assertEqual(segment["scale_y"], 1.0)
                self.assertEqual(segment["transform_x"], 0.0)
                self.assertEqual(segment["transform_y"], 0.0)
                self.assertEqual(segment["keyframes"], [])

        marker_tracks = [
            track for track in content["tracks"] if track["name"].startswith("Review Marker")
        ]
        self.assertEqual(sum(len(track["segments"]) for track in marker_tracks), 4)

    def test_review_job_compiler_normalizes_replacement_local_clock_once(self):
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {
                    "review_items": [
                        {
                            "id": "replace-anchor",
                            "source_text": "07:33-09:32 替换为以下视频",
                            "start": 453.0,
                            "end": 572.0,
                            "kind": "visual_replace",
                        },
                        {
                            "id": "replace-local",
                            "source_text": "00:05-00:13 删除补录口误",
                            "start": 5.0,
                            "end": 13.0,
                            "kind": "spoken_delete",
                        },
                        {
                            "id": "replace-handoff",
                            "source_text": "02:01-结尾，延长至原视频 09:42",
                            "start": 121.0,
                            "end": 125.0,
                            "kind": "spoken_delete",
                        },
                        {
                            "id": "main-after",
                            "source_text": "09:45 删除主片口误",
                            "start": 585.0,
                            "end": 586.0,
                            "kind": "spoken_delete",
                        },
                    ]
                },
                {
                    "draft_name": "ReplacementTimebaseDraft",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                    "media_duration_seconds": 627.48,
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                payload = json.load(source_file)

        items = {item["id"]: item for item in payload["review_items"]}
        local = items["replace-local"]
        self.assertEqual(local["source_time_range"], [5.0, 13.0])
        self.assertEqual(local["timeline_time_range"], [458.0, 466.0])
        self.assertEqual([local["start"], local["end"]], [458.0, 466.0])
        self.assertEqual(local["timebase"]["kind"], "replacement_local")
        self.assertEqual(items["replace-handoff"]["timeline_time_range"], [574.0, 582.0])
        self.assertEqual(items["main-after"]["timeline_time_range"], [585.0, 586.0])
        self.assertEqual(payload["unresolved_timebase_item_ids"], [])

    def test_review_job_compiler_does_not_guess_unanchored_replacement_time(self):
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {
                    "review_items": [
                        {
                            "id": "unanchored-local",
                            "source_role": "replacement_video",
                            "source_text": "00:05-00:13 删除补录口误",
                            "start": 5.0,
                            "end": 13.0,
                            "kind": "spoken_delete",
                        }
                    ]
                },
                {
                    "draft_name": "UnanchoredReplacementDraft",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                payload = json.load(source_file)
            with open(compiled["job_manifest"], "r", encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)

        item = payload["review_items"][0]
        self.assertNotIn("start", item)
        self.assertNotIn("end", item)
        self.assertNotIn("timeline_time_range", item)
        self.assertEqual(item["source_time_range"], [5.0, 13.0])
        self.assertEqual(item["timebase"]["status"], "unresolved_no_anchor")
        self.assertEqual(payload["unresolved_timebase_item_ids"], ["unanchored-local"])
        self.assertEqual(manifest["unresolved_timebase_item_ids"], ["unanchored-local"])

    def test_review_job_compiler_does_not_downgrade_lite_pointer_to_marker_only(self):
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {
                    "review_items": [
                        {
                            "id": "pointer-1",
                            "kind": "pointer_overlay",
                            "source_text": "05:10 add a hand pointing to Austria",
                            "start": 310.0,
                            "end": 310.5,
                            "execution_required": False,
                        }
                    ]
                },
                {
                    "draft_name": "LitePointerDraft",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["revision_request"], "r", encoding="utf-8") as request_file:
                request_payload = json.load(request_file)

        self.assertTrue(request_payload["review_items"][0]["execution_required"])
        self.assertEqual(
            request_payload["review_items"][0]["review_timestamp_role"],
            "authoritative_non_speech",
        )
        self.assertFalse(request_payload["acceptance"]["require_visual_evidence"])
        self.assertFalse(request_payload["acceptance"]["require_subject_pointer_binding"])
        self.assertFalse(request_payload["acceptance"]["require_pointer_lifecycle_evidence"])
        self.assertNotIn("visual", request_payload["acceptance_profile"]["enabled_gates"])
        self.assertNotIn("pointer", request_payload["acceptance_profile"]["enabled_gates"])

    def test_lite_explicit_visual_flag_still_uses_lite_start_alignment(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteVisualAcceptance",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {
                        "type": "visual_overlay",
                        "source_kind": "visual_overlay",
                        "start": 2.0,
                        "end": 3.0,
                        "doc_item_id": "visual-1",
                        "asset_paths": ["C:/media/overlay.png"],
                    }
                ],
                "review_items": [
                    {
                        "id": "visual-1",
                        "kind": "visual_overlay",
                        "source_text": "02:00 add the supplied overlay",
                        "start": 2.0,
                        "end": 3.0,
                        "execution_required": True,
                    }
                ],
                "acceptance": {"require_visual_evidence": True},
            }
        )
        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=True,
                strict=True,
            )

        self.assertTrue(result["acceptance_validation"]["ok"])
        self.assertFalse(result["acceptance_validation"].get("skipped", False))
        self.assertEqual(len(result["visual_overlay_results"]), 1)
        self.assertEqual(
            result["visual_overlay_results"][0]["evidence"]["track_name"],
            LITE_TRACKS["visual_assets"],
        )

    def test_lite_pointer_does_not_require_full_binding_evidence(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LitePointerAcceptance",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                    "project_key": "lite-pointer-test",
                },
                "edits": [
                    {
                        "type": "pointer_overlay",
                        "source_kind": "pointer_overlay",
                        "start": 2.0,
                        "end": 3.0,
                        "doc_item_id": "pointer-1",
                        "asset_paths": ["C:/media/pointer.png"],
                    }
                ],
                "review_items": [
                    {
                        "id": "pointer-1",
                        "kind": "pointer_overlay",
                        "source_text": "02:00 point to the target",
                        "start": 2.0,
                        "end": 3.0,
                        "execution_required": True,
                    }
                ],
                "acceptance": {
                    "require_visual_evidence": True,
                    "require_subject_pointer_binding": True,
                },
            }
        )
        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=True,
                strict=True,
            )

        self.assertTrue(result["acceptance_validation"]["ok"])
        self.assertEqual(
            result["acceptance_validation"]["metrics"]["subject_pointer_binding_errors"],
            [],
        )

    def test_lite_visual_roles_keep_clean_cover_on_v3_and_pointer_on_v4(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LitePointerLayering",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {
                        "type": "visual_overlay",
                        "source_kind": "visual_overlay",
                        "start": 2.0,
                        "end": 3.0,
                        "doc_item_id": "pointer-layering",
                        "visual_plan": {
                            "segments": [
                                {
                                    "role": "clean_cover",
                                    "asset_path": "C:/media/clean.png",
                                    "timeline_start": 2.0,
                                    "duration": 1.0,
                                },
                                {
                                    "role": "pointer_asset",
                                    "asset_path": "C:/media/pointer.png",
                                    "timeline_start": 2.0,
                                    "duration": 1.0,
                                    "scale_x": 0.05,
                                    "scale_y": 0.05,
                                },
                            ]
                        },
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as drafts_root:
            execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(drafts_root, "LitePointerLayering", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        self.assertEqual(len(_track(content, LITE_TRACKS["visual_assets"])["segments"]), 1)
        self.assertEqual(len(_track(content, LITE_TRACKS["timing_adjusted"])["segments"]), 0)

    def test_load_revision_request_defaults_to_full_and_accepts_lite(self):
        base = {
            "project": {
                "draft_name": "ModeDraft",
                "source_video": "C:/media/source.mp4",
            }
        }
        self.assertEqual(_load_request(base).workflow_mode, "full")
        self.assertEqual(_load_request({**base, "workflow_mode": "lite"}).workflow_mode, "lite")
        lite_flags = _load_request(
            {
                **base,
                "workflow_mode": "lite",
                "acceptance": {
                    "require_visual_evidence": True,
                    "require_subject_pointer_binding": True,
                    "require_pointer_lifecycle_evidence": True,
                },
            }
        )
        self.assertFalse(lite_flags.acceptance.require_visual_evidence)
        self.assertFalse(lite_flags.acceptance.require_subject_pointer_binding)
        self.assertFalse(lite_flags.acceptance.require_pointer_lifecycle_evidence)
        with self.assertRaisesRegex(ValueError, "workflow_mode"):
            _load_request({**base, "workflow_mode": "compact"})

    def test_lite_mode_writes_split_gap_tracks_without_changing_duration(self):
        payload = {
            "workflow_mode": "lite",
            "project": {
                "draft_name": "LiteStructureDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
                "media_duration_seconds": 10.0,
            },
            "edits": [
                {
                    "type": "delete",
                    "source_kind": "spoken_delete",
                    "start": 2.0,
                    "end": 4.0,
                    "label": "delete summary",
                    "doc_item_id": "item-1",
                    "visual_plan": {"reuse_audio": False},
                    "evidence": {
                        "review_timestamp_role": "search_hint",
                        "delete": "summary",
                        "must_keep": ["before", "after"],
                        "strategy": "hybrid",
                        "asr_alignment": {
                            "status": "pass",
                            "provider": "test-asr",
                            "model": "test-model",
                            "adapter_version": "1",
                            "granularity": "word",
                            "input_sha256": "a" * 64,
                            "authoritative_cut_boundary": True,
                            "words": [{"text": "summary", "start": 2.0, "end": 4.0}],
                            "resolved_cut_window": [2.0, 4.0],
                        },
                    },
                },
                {
                    "type": "animation_timing",
                    "start": 5.0,
                    "end": 7.0,
                    "label": "timing summary",
                    "doc_item_id": "item-2",
                    "visual_plan": {"timeline_start": 1.0, "reuse_audio": True},
                },
                {
                    "type": "visual_overlay",
                    "start": 8.0,
                    "end": 9.0,
                    "label": "pointer summary",
                    "doc_item_id": "item-3",
                    "asset_paths": ["C:/media/pointer.png"],
                },
            ],
            "review_items": [
                {
                    "id": "item-1",
                    "kind": "spoken_delete",
                    "source_text": "02:00-04:00 删除这一段",
                    "start": 2.0,
                    "end": 4.0,
                },
                {
                    "id": "item-2",
                    "kind": "animation_timing",
                    "source_text": "05:00-07:00 提前到 01:00",
                    "start": 5.0,
                    "end": 7.0,
                },
                {
                    "id": "item-3",
                    "kind": "pointer_overlay",
                    "source_text": "08:00 添加指向物",
                    "start": 8.0,
                    "end": 9.0,
                },
            ],
        }
        request = _load_request(payload)
        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=True,
            )
            with open(
                os.path.join(drafts_root, "LiteStructureDraft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        self.assertEqual(result["workflow_mode"], "lite")
        self.assertTrue(result["non_destructive"])
        self.assertTrue(result["validation"]["ok"])
        self.assertEqual(content["duration"], 10_000_000)

        original = _track(content, LITE_TRACKS["original_video"])
        cut_track = _track(content, LITE_TRACKS["cut_segments"])
        visual_track = _track(content, LITE_TRACKS["visual_assets"])
        timing_track = _track(content, LITE_TRACKS["timing_adjusted"])
        source_audio = _track(content, LITE_TRACKS["source_audio"])
        reused_audio = _track(content, LITE_TRACKS["reused_audio"])

        self.assertEqual(
            [
                (s["target_timerange"]["start"], s["target_timerange"]["duration"])
                for s in original["segments"]
            ],
            [(0, 2_000_000), (4_000_000, 6_000_000)],
        )
        self.assertEqual(len(cut_track["segments"]), 1)
        self.assertEqual(cut_track["segments"][0]["source_timerange"]["start"], 2_000_000)
        self.assertEqual(len(visual_track["segments"]), 1)
        self.assertEqual(len(timing_track["segments"]), 0)
        self.assertEqual(len(source_audio["segments"]), 2)
        self.assertEqual(len(reused_audio["segments"]), 1)
        self.assertEqual(reused_audio["segments"][0]["source_timerange"]["start"], 2_000_000)

        marker_tracks = [
            track for track in content["tracks"] if track["name"].startswith("Review Marker")
        ]
        marker_segments = [segment for track in marker_tracks for segment in track["segments"]]
        self.assertEqual(len(marker_segments), 3)
        self.assertTrue(
            all(segment["target_timerange"]["duration"] == 2_000_000 for segment in marker_segments)
        )
        text_materials = {item["id"]: item for item in content["materials"]["texts"]}
        marker_colors = {
            text_materials[segment["material_id"]]["background_color"]
            for segment in marker_segments
        }
        self.assertGreaterEqual(len(marker_colors), 3)

    def test_lite_keeps_adjacent_different_item_cuts_and_a2_clips_independent(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteAdjacentCuts",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    _spoken_delete_edit("item-a", 2.0, 3.0),
                    _spoken_delete_edit("item-b", 3.0, 4.0),
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(drafts_root, "LiteAdjacentCuts", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        v2 = _track(content, LITE_TRACKS["cut_segments"])["segments"]
        a2 = _track(content, LITE_TRACKS["reused_audio"])["segments"]
        self.assertEqual(len(v2), 2)
        self.assertEqual(len(a2), 2)
        self.assertEqual(
            [(row["target_timerange"]["start"], row["target_timerange"]["duration"]) for row in v2],
            [(2_000_000, 1_000_000), (3_000_000, 1_000_000)],
        )
        self.assertTrue(result["validation"]["ok"])

    def test_lite_merges_overlapping_windows_only_within_one_item(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteSameItemMerge",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    _spoken_delete_edit("item-a", 2.0, 3.0, label="first"),
                    _spoken_delete_edit("item-a", 2.5, 4.0, label="second"),
                ],
                "review_items": [
                    {
                        "id": "item-a",
                        "kind": "spoken_delete",
                        "source_text": "02:00-04:00 delete the resolved phrase",
                        "start": 2.0,
                        "end": 4.0,
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(drafts_root, "LiteSameItemMerge", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        self.assertEqual(len(_track(content, LITE_TRACKS["cut_segments"])["segments"]), 1)
        self.assertEqual(len(_track(content, LITE_TRACKS["reused_audio"])["segments"]), 1)
        self.assertTrue(result["validation"]["ok"])

    def test_lite_rejects_overlapping_windows_from_different_items_before_draft_open(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteCrossItemOverlap",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    _spoken_delete_edit("item-a", 2.0, 4.0),
                    _spoken_delete_edit("item-b", 3.0, 5.0),
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            with self.assertRaisesRegex(ValueError, "different review items overlap"):
                execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            self.assertFalse(os.path.exists(os.path.join(drafts_root, "LiteCrossItemOverlap")))

    def test_lite_zero_start_delete_uses_v2_as_the_zero_aligned_main_track(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteZeroStartCut",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [_spoken_delete_edit("item-zero", 0.0, 1.0)],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(drafts_root, "LiteZeroStartCut", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        v1 = _track(content, LITE_TRACKS["original_video"])["segments"]
        v2 = _track(content, LITE_TRACKS["cut_segments"])["segments"]
        self.assertEqual(v1[0]["target_timerange"]["start"], 1_000_000)
        self.assertGreater(v1[0]["render_index"], v2[0]["render_index"])
        self.assertEqual(v2[0]["target_timerange"]["start"], 0)
        self.assertTrue(result["validation"]["ok"])

    def test_lite_all_pause_wording_variants_are_asr_timed_labels_only(self):
        cases = [
            ("pause-add", "review_only", "05:00 在现有停顿基础上增加1秒", 2.25),
            ("pause-plus", "review_only", "05:00 停顿 +1s", 3.25),
            ("pause-extend", "visual_overlay", "05:00 延长停顿1秒", 4.25),
            ("pause-shorten", "visual_overlay", "05:00 缩短停顿1秒", 6.25),
            ("pause-minus", "review_only", "05:00 停顿 -1s", 7.25),
            ("pause-delete", "pause_delete", "05:00 删除这段停顿", 8.25),
            ("gap-delete", "gap_delete", "05:00 删除两句之间的停顿", 9.25),
        ]
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LitePauseWordingLabelOnly",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "review_items": [
                    {
                        "id": item_id,
                        "kind": kind,
                        "source_text": source_text,
                        "start": 1.0,
                        "execution_required": True,
                    }
                    for item_id, kind, source_text, _resolved_time in cases
                ],
                "edits": [
                    {
                        "type": "visual_overlay",
                        "source_kind": "visual_overlay",
                        "start": resolved_time,
                        "end": resolved_time,
                        "doc_item_id": item_id,
                        "asset_paths": [f"C:/media/{item_id}.png"],
                        "evidence": {
                            "review_timestamp_role": "search_hint",
                            "asr_alignment": {
                                "status": "pass",
                                "provider": "test-asr",
                                "model": "test-model",
                                "adapter_version": "1",
                                "granularity": "word",
                                "input_sha256": "e" * 64,
                                "matches": [
                                    {
                                        "text": "停顿",
                                        "start": resolved_time,
                                        "end": resolved_time + 0.2,
                                    }
                                ],
                                "resolved_time": resolved_time,
                            },
                        },
                    }
                    for item_id, kind, _source_text, resolved_time in cases
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(
                    drafts_root,
                    "LitePauseWordingLabelOnly",
                    "draft_content.json",
                ),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        item_ids = [item_id for item_id, _kind, _source_text, _time in cases]
        self.assertEqual(content["duration"], 10_000_000)
        self.assertEqual(_track(content, LITE_TRACKS["visual_assets"])["segments"], [])
        self.assertEqual(result["pause_results"], [])
        self.assertEqual(result["label_only_item_ids"], item_ids)
        self.assertEqual(
            [receipt["execution_status"] for receipt in result["review_marker_receipts"]],
            ["label_only_lite_policy"] * len(cases),
        )
        text_by_material = {
            material["id"]: json.loads(material["content"])["text"]
            for material in content["materials"]["texts"]
        }
        marker_rows = sorted(
            (
                segment["target_timerange"]["start"],
                text_by_material[segment["material_id"]],
            )
            for track in content["tracks"]
            if track["name"].startswith("Review Marker")
            for segment in track["segments"]
        )
        self.assertEqual(
            marker_rows,
            [
                (round(resolved_time * 1_000_000), source_text)
                for _item_id, _kind, source_text, resolved_time in cases
            ],
        )

    def test_lite_legacy_added_pause_is_label_only_without_shifting_later_content(self):
        source_asr_path = "C:/evidence/source-asr.json"
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteAddedPause",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.wav",
                    "media_duration_seconds": 10.0,
                },
                "pause_alignment": {
                    "source_asr_path": source_asr_path,
                    "source_asr_sha256": "b" * 64,
                    "source_asr_identity": {
                        "provider": "test-asr",
                        "model": "test-model",
                        "adapter_version": "1",
                    },
                },
                "pause_adjustments": [
                    {
                        "item_id": "pause-1",
                        "requested_source_time": 5.0,
                        "source_time": 5.0,
                        "duration": 1.0,
                        "frame_path": "C:/media/pause.png",
                        "frame_sha256": "c" * 64,
                        "boundary_evidence": {
                            "status": "pass",
                            "resolved_time": 5.0,
                            "source_asr_sha256": "b" * 64,
                        },
                    }
                ],
                "edits": [
                    {
                        "type": "visual_overlay",
                        "source_kind": "visual_overlay",
                        "start": 6.0,
                        "end": 7.0,
                        "doc_item_id": "visual-1",
                        "asset_paths": ["C:/media/pointer.png"],
                    }
                ],
                "review_items": [
                    {
                        "id": "pause-1",
                        "kind": "semantic_pause_adjustment",
                        "source_text": "05:00 add one more second to the existing pause",
                        "start": 5.0,
                    },
                    {
                        "id": "visual-1",
                        "kind": "visual_overlay",
                        "source_text": "06:00 place the supplied pointer",
                        "start": 6.0,
                        "end": 7.0,
                    },
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(drafts_root, "LiteAddedPause", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        self.assertEqual(content["duration"], 10_000_000)
        self.assertEqual(result["added_pause_duration_seconds"], 0.0)
        self.assertEqual(result["pause_results"], [])
        self.assertEqual(result["label_only_pause_item_ids"], ["pause-1"])
        v1 = _track(content, LITE_TRACKS["original_video"])["segments"]
        self.assertEqual(
            [(row["target_timerange"]["start"], row["target_timerange"]["duration"]) for row in v1],
            [(0, 10_000_000)],
        )
        self.assertNotIn(
            "semantic_pause_hold",
            [receipt.get("kind") for receipt in result["segment_receipts"]],
        )
        visual = _track(content, LITE_TRACKS["visual_assets"])["segments"][0]
        self.assertEqual(visual["target_timerange"]["start"], 6_000_000)
        marker_starts = sorted(
            segment["target_timerange"]["start"]
            for track in content["tracks"]
            if track["name"].startswith("Review Marker")
            for segment in track["segments"]
        )
        self.assertEqual(marker_starts, [5_000_000, 6_000_000])
        pause_receipt = next(
            receipt
            for receipt in result["review_marker_receipts"]
            if receipt["item_id"] == "pause-1"
        )
        self.assertEqual(pause_receipt["source_text"], request.review_items[0].source_text)
        self.assertEqual(pause_receipt["execution_status"], "label_only_lite_policy")
        self.assertTrue(result["validation"]["ok"])

    def test_lite_low_level_validation_rejects_pause_execution_artifacts(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LitePauseArtifactGuard",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
            }
        )
        with tempfile.TemporaryDirectory() as drafts_root:
            execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(
                    drafts_root,
                    "LitePauseArtifactGuard",
                    "draft_content.json",
                ),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        clean_content = json.loads(json.dumps(content))
        source_segment = _track(content, LITE_TRACKS["original_video"])["segments"][0]
        source_material_id = source_segment["material_id"]
        hold = json.loads(json.dumps(source_segment))
        hold["id"] = "forged-semantic-pause-hold"
        hold["target_timerange"] = {"start": 5_000_000, "duration": 1_000_000}
        hold["source_timerange"] = {"start": 0, "duration": 1_000_000}
        _track(content, LITE_TRACKS["original_video"])["segments"].append(hold)

        self.assertEqual(_lite_timeline_duration(10.0, [object()]), 10.0)
        validation = _validate_lite_content(
            content,
            total_duration=10.0,
            marker_plan=[],
            marker_receipts=[],
            reused_audio_expected=False,
            pause_adjustments=[object()],
            pause_receipts=[{"item_id": "pause-1", "segment_id": hold["id"]}],
            segment_receipts=[
                {
                    "item_id": "pause-1",
                    "kind": "Semantic_Pause_Hold",
                    "segment_id": hold["id"],
                }
            ],
            source_video_material_id=source_material_id,
            source_video_path=request.project.source_video,
        )

        self.assertFalse(validation["ok"])
        joined_errors = "\n".join(validation["errors"])
        self.assertIn("executable pause_adjustments", joined_errors)
        self.assertIn("pause_receipts", joined_errors)
        self.assertIn("semantic_pause_hold", joined_errors)
        self.assertIn("V1 segment count mismatch", joined_errors)

        forged_content = json.loads(json.dumps(clean_content))
        forged_material = json.loads(
            json.dumps(
                next(
                    material
                    for material in forged_content["materials"]["videos"]
                    if material["id"] == source_material_id
                )
            )
        )
        forged_material["id"] = "forged-unreceipted-hold-material"
        forged_material["material_id"] = "forged-unreceipted-hold-material"
        forged_material["path"] = "C:/media/pause-frame.png"
        forged_content["materials"]["videos"].append(forged_material)
        _track(forged_content, LITE_TRACKS["original_video"])["segments"][0]["material_id"] = (
            forged_material["id"]
        )
        forged_validation = _validate_lite_content(
            forged_content,
            total_duration=10.0,
            marker_plan=[],
            marker_receipts=[],
            reused_audio_expected=False,
            source_video_material_id=source_material_id,
            source_video_path=request.project.source_video,
        )
        self.assertFalse(forged_validation["ok"])
        self.assertTrue(
            any(
                "contains non-source video material" in error
                for error in forged_validation["errors"]
            ),
            forged_validation["errors"],
        )

        forged_v2_content = json.loads(json.dumps(clean_content))
        forged_v2_segment = json.loads(json.dumps(source_segment))
        forged_v2_segment["id"] = "forged-v2-segment"
        forged_v2_segment["material_id"] = "forged-v2-material"
        forged_v2_segment["target_timerange"] = {
            "start": 2_000_000,
            "duration": 1_000_000,
        }
        forged_v2_segment["source_timerange"] = {
            "start": 2_000_000,
            "duration": 1_000_000,
        }
        _track(forged_v2_content, LITE_TRACKS["cut_segments"])["segments"].append(forged_v2_segment)
        forged_v2_validation = _validate_lite_content(
            forged_v2_content,
            total_duration=10.0,
            marker_plan=[],
            marker_receipts=[],
            reused_audio_expected=False,
            source_video_material_id=source_material_id,
            source_video_path=request.project.source_video,
        )
        self.assertFalse(forged_v2_validation["ok"])
        self.assertTrue(
            any(
                LITE_TRACKS["cut_segments"] in error
                and "contains non-source video material" in error
                for error in forged_v2_validation["errors"]
            ),
            forged_v2_validation["errors"],
        )

        wrong_path_content = json.loads(json.dumps(clean_content))
        next(
            material
            for material in wrong_path_content["materials"]["videos"]
            if material["id"] == source_material_id
        )["path"] = "C:/media/not-the-source.mp4"
        wrong_path_validation = _validate_lite_content(
            wrong_path_content,
            total_duration=10.0,
            marker_plan=[],
            marker_receipts=[],
            reused_audio_expected=False,
            source_video_material_id=source_material_id,
            source_video_path=request.project.source_video,
        )
        self.assertFalse(wrong_path_validation["ok"])
        self.assertIn(
            "Lite source video material path does not match project.source_video.",
            wrong_path_validation["errors"],
        )

        extended_content = json.loads(json.dumps(clean_content))
        extended_content["duration"] = 11_000_000
        duration_validation = _validate_lite_content(
            extended_content,
            total_duration=10.0,
            marker_plan=[],
            marker_receipts=[],
            reused_audio_expected=False,
            source_video_material_id=source_material_id,
            source_video_path=request.project.source_video,
        )
        self.assertFalse(duration_validation["ok"])
        self.assertTrue(
            any(
                "expected unchanged source duration" in error
                for error in duration_validation["errors"]
            ),
            duration_validation["errors"],
        )

    def test_lite_audio_label_only_item_keeps_asr_marker_time_after_execution_filtering(
        self,
    ):
        source_text = "02:00 发音问题暂时只记录修改意见原文"
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteAudioLabelOnlyAsrTime",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "review_items": [
                    {
                        "id": "audio-new",
                        "kind": "pronunciation_repair",
                        "source_text": source_text,
                        "start": 2.0,
                        "execution_required": True,
                        "evidence": {
                            "execution_status": "label_only_unresolved",
                        },
                    }
                ],
                "edits": [
                    {
                        "type": "visual_overlay",
                        "source_kind": "pronunciation_repair",
                        "start": 6.25,
                        "end": 6.25,
                        "doc_item_id": "audio-new",
                        "asset_paths": ["C:/media/unresolved-audio-repair.png"],
                        "evidence": {
                            "review_timestamp_role": "search_hint",
                            "asr_alignment": {
                                "status": "pass",
                                "provider": "test-asr",
                                "model": "test-model",
                                "adapter_version": "1",
                                "granularity": "word",
                                "input_sha256": "d" * 64,
                                "matches": [{"text": "发音", "start": 6.25, "end": 6.45}],
                                "resolved_time": 6.25,
                            },
                        },
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(
                    drafts_root,
                    "LiteAudioLabelOnlyAsrTime",
                    "draft_content.json",
                ),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        self.assertEqual(_track(content, LITE_TRACKS["visual_assets"])["segments"], [])
        self.assertEqual(result["label_only_unresolved_item_ids"], ["audio-new"])
        self.assertEqual(result["label_only_item_ids"], ["audio-new"])
        marker_segment = next(
            segment
            for track in content["tracks"]
            if track["name"].startswith("Review Marker")
            for segment in track["segments"]
        )
        self.assertEqual(marker_segment["target_timerange"]["start"], 6_250_000)
        text_material = next(
            material
            for material in content["materials"]["texts"]
            if material["id"] == marker_segment["material_id"]
        )
        self.assertEqual(json.loads(text_material["content"])["text"], source_text)
        receipt = result["review_marker_receipts"][0]
        self.assertEqual(receipt["source_text"], source_text)
        self.assertEqual(receipt["execution_status"], "label_only_unresolved")

    def test_lite_copy_layout_is_rejected_before_draft_open(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "lite_cut_layout": "copy",
                "project": {
                    "draft_name": "LiteCopyRejected",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
            }
        )
        with tempfile.TemporaryDirectory() as drafts_root:
            with self.assertRaisesRegex(ValueError, "copy.*no longer executable"):
                execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            self.assertFalse(os.path.exists(os.path.join(drafts_root, "LiteCopyRejected")))

    def test_lite_label_only_unresolved_status_never_changes_visible_marker_text(self):
        source_text = "02:00 new issue; keep this exact review comment"
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteUnresolvedLabel",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "review_items": [
                    {
                        "id": "new-issue",
                        "kind": "review_only",
                        "source_text": source_text,
                        "start": 2.0,
                        "execution_required": True,
                        "evidence": {"execution_status": "label_only_unresolved"},
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(drafts_root, "LiteUnresolvedLabel", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        marker_segment = next(
            segment
            for track in content["tracks"]
            if track["name"].startswith("Review Marker")
            for segment in track["segments"]
        )
        text_material = next(
            material
            for material in content["materials"]["texts"]
            if material["id"] == marker_segment["material_id"]
        )
        self.assertEqual(json.loads(text_material["content"])["text"], source_text)
        self.assertNotIn("label_only_unresolved", json.loads(text_material["content"])["text"])
        self.assertEqual(
            result["review_marker_receipts"][0]["execution_status"],
            "label_only_unresolved",
        )

    def test_lite_label_only_unresolved_edit_is_not_executed(self):
        source_text = "02:00 新问题暂时只记录原文标签"
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteUnresolvedEditSkipped",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "review_items": [
                    {
                        "id": "new-issue",
                        "kind": "visual_overlay",
                        "source_text": source_text,
                        "start": 2.0,
                        "end": 4.0,
                        "execution_required": True,
                        "execution_status": "pending",
                        "validation": {"execution_status": "label_only_unresolved"},
                    }
                ],
                "edits": [
                    {
                        "type": "add",
                        "op_type": "add",
                        "source_kind": "visual_overlay",
                        "label": source_text,
                        "detail": source_text,
                        "start": 2.0,
                        "end": 4.0,
                        "doc_item_id": "new-issue",
                        "asset_paths": ["C:/media/unresolved-overlay.png"],
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(drafts_root, "LiteUnresolvedEditSkipped", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        visual_track = _track(content, LITE_TRACKS["visual_assets"])
        assert not visual_track["segments"]
        assert result["label_only_unresolved_item_ids"] == ["new-issue"]
        assert result["review_marker_count"] == 1
        marker_segment = next(
            segment
            for track in content["tracks"]
            if track["name"].startswith("Review Marker")
            for segment in track["segments"]
        )
        text_material = next(
            material
            for material in content["materials"]["texts"]
            if material["id"] == marker_segment["material_id"]
        )
        assert json.loads(text_material["content"])["text"] == source_text
        assert result["review_marker_receipts"][0]["execution_status"] == ("label_only_unresolved")

    def test_lite_acceptance_downgrades_stale_doc_item_execution_status(self):
        """A stale external doc item cannot promote a marker-only issue to execution."""

        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteStaleDocStatus",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "review_items": [
                    {
                        "id": "unresolved",
                        "kind": "visual_overlay",
                        "source_text": "02:00 新问题只记录原文",
                        "start": 2.0,
                        "execution_required": True,
                    }
                ],
            }
        )
        request_item = replace(
            request.review_items[0],
            execution_required=True,
            execution_status="label_only_unresolved",
        )
        external_doc_item = replace(request_item)
        stale_request = replace(request, review_items=[request_item])

        profile = derive_acceptance_profile(stale_request, doc_items=[external_doc_item])

        self.assertEqual(len(profile["items"]), 1)
        self.assertFalse(profile["items"][0]["execution_required"])
        self.assertEqual(profile["items"][0]["gates"], [])
        self.assertFalse(profile["routing_failures"])

    def test_lite_rejects_asr_window_that_is_wider_than_authoritative_matches(self):
        edit = _spoken_delete_edit("wide-window", 0.0, 4.0)
        edit["evidence"]["asr_alignment"]["words"] = [{"text": "actual", "start": 2.0, "end": 3.0}]
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteWideAsrWindow",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [edit],
            }
        )
        problems = _spoken_cut_alignment_problems(request.edits[0], None)
        self.assertTrue(any("first/last authoritative ASR match" in row for row in problems))

    def test_lite_mode_writes_a2_for_cut_even_when_audio_reuse_is_false(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteNoA2Draft",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "spoken_delete",
                        "start": 9.5,
                        "end": 10.0,
                        "label": "last cut",
                        "doc_item_id": "item-last",
                        "visual_plan": {"reuse_audio": False},
                        "evidence": {
                            "review_timestamp_role": "search_hint",
                            "delete": "ending",
                            "must_keep": ["previous word"],
                            "strategy": "precision_first",
                            "asr_alignment": {
                                "status": "pass",
                                "provider": "test-asr",
                                "model": "test-model",
                                "adapter_version": "1",
                                "granularity": "character",
                                "input_sha256": "b" * 64,
                                "authoritative_cut_boundary": True,
                                "words": [{"text": "ending", "start": 9.5, "end": 10.0}],
                                "resolved_cut_window": [9.5, 10.0],
                            },
                        },
                    }
                ],
                "review_items": [
                    {
                        "id": "item-last",
                        "kind": "spoken_delete",
                        "source_text": "09:50-10:00 删除结尾",
                        "start": 9.5,
                        "end": 10.0,
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(drafts_root, "LiteNoA2Draft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        track_names = {track["name"] for track in content["tracks"]}
        self.assertIn(LITE_TRACKS["reused_audio"], track_names)
        reused_audio = _track(content, LITE_TRACKS["reused_audio"])
        self.assertEqual(len(reused_audio["segments"]), 1)
        self.assertEqual(reused_audio["segments"][0]["target_timerange"]["start"], 9_500_000)
        marker_segment = next(
            segment
            for track in content["tracks"]
            if track["name"].startswith("Review Marker")
            for segment in track["segments"]
        )
        self.assertEqual(marker_segment["target_timerange"]["start"], 9_500_000)
        self.assertEqual(marker_segment["target_timerange"]["duration"], 500_000)
        self.assertEqual(content["duration"], 10_000_000)
        self.assertTrue(result["validation"]["ok"])

    def test_lite_spoken_delete_marker_uses_asr_cut_start_not_review_timestamp(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteAsrAlignedMarker",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 8.0,
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "spoken_delete",
                        "start": 2.25,
                        "end": 2.75,
                        "doc_item_id": "spoken-1",
                        "evidence": {
                            "review_timestamp_role": "search_hint",
                            "delete": "口误",
                            "must_keep": ["前词", "后词"],
                            "strategy": "precision_first",
                            "asr_alignment": {
                                "status": "pass",
                                "provider": "test-asr",
                                "model": "test-model",
                                "adapter_version": "1",
                                "granularity": "character",
                                "input_sha256": "d" * 64,
                                "authoritative_cut_boundary": True,
                                "words": [
                                    {"text": "口", "start": 2.25, "end": 2.5},
                                    {"text": "误", "start": 2.5, "end": 2.75},
                                ],
                                "resolved_cut_window": [2.25, 2.75],
                            },
                        },
                    }
                ],
                "review_items": [
                    {
                        "id": "spoken-1",
                        "kind": "spoken_delete",
                        "source_text": "00:01 删除口误",
                        "start": 1.0,
                        "end": 1.5,
                        "execution_required": True,
                        "evidence": {"review_timestamp_role": "search_hint"},
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=True,
            )
            with open(
                os.path.join(
                    drafts_root,
                    "LiteAsrAlignedMarker",
                    "draft_content.json",
                ),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        marker_segment = next(
            segment
            for track in content["tracks"]
            if track["name"].startswith("Review Marker")
            for segment in track["segments"]
        )
        self.assertEqual(marker_segment["target_timerange"]["start"], 2_250_000)
        self.assertNotEqual(marker_segment["target_timerange"]["start"], 1_000_000)
        self.assertTrue(result["validation"]["ok"])

    def test_lite_split_gap_disables_maintrack_adsorb_and_restores_a2_volume(self):
        source_audio = "C:/media/source.wav"
        request = _load_request(
            {
                "workflow_mode": "lite",
                "lite_cut_layout": "split_gap",
                "project": {
                    "draft_name": "LiteSegmentedAudioDraft",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": source_audio,
                    "media_duration_seconds": 10.0,
                },
                "audio_delivery_plan": {
                    "mode": "segmented",
                    "forbid_full_length_segments": True,
                    "validation_only_audio_paths": ["C:/qa/full-candidate.wav"],
                    "segments": [
                        {
                            "id": "a1-001",
                            "role": "source",
                            "asset_path": source_audio,
                            "track_name": LITE_TRACKS["source_audio"],
                            "source_start": 0.0,
                            "timeline_start": 0.0,
                            "duration": 2.0,
                            "volume": 1.0,
                        },
                        {
                            "id": "a1-002",
                            "role": "source",
                            "asset_path": source_audio,
                            "track_name": LITE_TRACKS["source_audio"],
                            "source_start": 4.0,
                            "timeline_start": 4.0,
                            "duration": 6.0,
                            "volume": 1.0,
                        },
                        {
                            "id": "a2-001",
                            "role": "reference",
                            "asset_path": source_audio,
                            "track_name": LITE_TRACKS["reused_audio"],
                            "source_start": 2.0,
                            "timeline_start": 2.0,
                            "duration": 2.0,
                            "volume": 0.0,
                        },
                    ],
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "spoken_delete",
                        "start": 2.0,
                        "end": 4.0,
                        "label": "delete summary",
                        "doc_item_id": "item-1",
                        "evidence": {
                            "review_timestamp_role": "search_hint",
                            "delete": "summary",
                            "must_keep": ["before", "after"],
                            "strategy": "precision_first",
                            "asr_alignment": {
                                "status": "pass",
                                "provider": "test-asr",
                                "model": "test-model",
                                "adapter_version": "1",
                                "granularity": "word",
                                "input_sha256": "c" * 64,
                                "authoritative_cut_boundary": True,
                                "words": [{"text": "summary", "start": 2.0, "end": 4.0}],
                                "resolved_cut_window": [2.0, 4.0],
                            },
                        },
                    }
                ],
                "review_items": [
                    {
                        "id": "item-1",
                        "kind": "spoken_delete",
                        "source_text": "02:00-04:00 delete summary",
                        "start": 2.0,
                        "end": 4.0,
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(drafts_root, "LiteSegmentedAudioDraft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        a1 = _track(content, LITE_TRACKS["source_audio"])
        a2 = _track(content, LITE_TRACKS["reused_audio"])
        self.assertIs(content["config"]["maintrack_adsorb"], False)
        self.assertEqual([segment["volume"] for segment in a1["segments"]], [1.0, 1.0])
        self.assertEqual([segment["volume"] for segment in a2["segments"]], [1.0])
        self.assertEqual(
            {segment["material_id"] for segment in a1["segments"] + a2["segments"]},
            {a1["segments"][0]["material_id"]},
        )
        self.assertTrue(result["validation"]["ok"])

    def test_lite_split_gap_rejects_full_length_a2_in_segmented_plan(self):
        source_audio = "C:/media/source.wav"
        request = _load_request(
            {
                "workflow_mode": "lite",
                "lite_cut_layout": "split_gap",
                "project": {
                    "draft_name": "LiteInvalidFullA2",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": source_audio,
                    "media_duration_seconds": 10.0,
                },
                "audio_delivery_plan": {
                    "mode": "segmented",
                    "lite_a2_audible": True,
                    "forbid_full_length_segments": True,
                    "segments": [
                        {
                            "id": "a2-full",
                            "role": "reference",
                            "asset_path": source_audio,
                            "track_name": LITE_TRACKS["reused_audio"],
                            "source_start": 0.0,
                            "timeline_start": 0.0,
                            "duration": 10.0,
                        },
                    ],
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "spoken_delete",
                        "start": 0.0,
                        "end": 10.0,
                        "doc_item_id": "item-1",
                        "evidence": {
                            "review_timestamp_role": "search_hint",
                            "delete": "summary",
                            "must_keep": ["before", "after"],
                            "strategy": "precision_first",
                            "asr_alignment": {
                                "status": "pass",
                                "provider": "test-asr",
                                "model": "test-model",
                                "adapter_version": "1",
                                "granularity": "word",
                                "input_sha256": "e" * 64,
                                "authoritative_cut_boundary": True,
                                "words": [{"text": "summary", "start": 0.0, "end": 10.0}],
                                "resolved_cut_window": [0.0, 10.0],
                            },
                        },
                    }
                ],
                "review_items": [
                    {
                        "id": "item-1",
                        "kind": "spoken_delete",
                        "source_text": "00:00-00:10 delete summary",
                        "start": 0.0,
                        "end": 10.0,
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            with self.assertRaisesRegex(ValueError, "independent clip per merged ASR delete"):
                execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            self.assertFalse(
                os.path.exists(os.path.join(drafts_root, "LiteInvalidFullA2", "draft_content.json"))
            )

    def test_lite_unresolved_timebase_rejects_stale_segmented_cut_plan(self):
        source_audio = "C:/media/source.wav"
        request = _load_request(
            {
                "workflow_mode": "lite",
                "lite_cut_layout": "split_gap",
                "project": {
                    "draft_name": "LiteStaleUnresolvedAudioPlan",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": source_audio,
                    "media_duration_seconds": 10.0,
                },
                "audio_delivery_plan": {
                    "mode": "segmented",
                    "lite_a2_audible": True,
                    "segments": [
                        {
                            "id": "a1-001",
                            "role": "source",
                            "asset_path": source_audio,
                            "track_name": LITE_TRACKS["source_audio"],
                            "source_start": 0.0,
                            "timeline_start": 0.0,
                            "duration": 2.0,
                        },
                        {
                            "id": "a1-002",
                            "role": "source",
                            "asset_path": source_audio,
                            "track_name": LITE_TRACKS["source_audio"],
                            "source_start": 4.0,
                            "timeline_start": 4.0,
                            "duration": 6.0,
                        },
                        {
                            "id": "a2-stale",
                            "role": "reference",
                            "asset_path": source_audio,
                            "track_name": LITE_TRACKS["reused_audio"],
                            "source_start": 2.0,
                            "timeline_start": 2.0,
                            "duration": 2.0,
                            "doc_item_id": "replacement-local",
                        },
                    ],
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "phrase_delete",
                        "start": 2.0,
                        "end": 4.0,
                        "doc_item_id": "replacement-local",
                    }
                ],
                "review_items": [
                    {
                        "id": "replacement-local",
                        "kind": "phrase_delete",
                        "source_text": "00：02-00：04，删除重复词",
                        "start": 2.0,
                        "end": 4.0,
                        "execution_required": True,
                        "execution_status": "asr_resolved",
                        "evidence": {
                            "review_search_hint_seconds": 2.0,
                            "review_timestamp_parse": "range",
                            "review_timestamp_role": "authoritative_fallback",
                            "timebase": {
                                "kind": "replacement_local",
                                "offset_seconds": 0.0,
                                "status": "unresolved_missing_local_range",
                            },
                        },
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            with self.assertRaisesRegex(ValueError, "segment count must equal"):
                execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        drafts_root,
                        "LiteStaleUnresolvedAudioPlan",
                        "draft_content.json",
                    )
                )
            )

    def test_lite_split_gap_rejects_legacy_a1_track_alongside_canonical_a1(self):
        source_audio = "C:/media/source.wav"
        request = _load_request(
            {
                "workflow_mode": "lite",
                "lite_cut_layout": "split_gap",
                "project": {
                    "draft_name": "LiteLegacyA1",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": source_audio,
                    "media_duration_seconds": 10.0,
                },
                "audio_delivery_plan": {
                    "mode": "segmented",
                    "lite_a2_audible": True,
                    "forbid_full_length_segments": True,
                    "segments": [
                        {
                            "id": "a1-001",
                            "role": "source",
                            "asset_path": source_audio,
                            "track_name": LITE_TRACKS["source_audio"],
                            "source_start": 0.0,
                            "timeline_start": 0.0,
                            "duration": 2.0,
                        },
                        {
                            "id": "a1-002",
                            "role": "source",
                            "asset_path": source_audio,
                            "track_name": LITE_TRACKS["source_audio"],
                            "source_start": 4.0,
                            "timeline_start": 4.0,
                            "duration": 6.0,
                        },
                        {
                            "id": "a2-001",
                            "role": "reference",
                            "asset_path": source_audio,
                            "track_name": LITE_TRACKS["reused_audio"],
                            "source_start": 2.0,
                            "timeline_start": 2.0,
                            "duration": 2.0,
                        },
                        {
                            "id": "legacy-a1",
                            "role": "source",
                            "asset_path": source_audio,
                            "track_name": "Lite Source Audio",
                            "source_start": 0.0,
                            "timeline_start": 0.0,
                            "duration": 10.0,
                        },
                    ],
                },
                "edits": [_spoken_delete_edit("item-1", 2.0, 4.0, label="summary")],
                "review_items": [
                    {
                        "id": "item-1",
                        "kind": "spoken_delete",
                        "source_text": "00:02 delete summary",
                        "start": 2.0,
                        "end": 4.0,
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            with self.assertRaisesRegex(ValueError, "unexpected track.*Lite Source Audio"):
                execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            self.assertFalse(
                os.path.exists(os.path.join(drafts_root, "LiteLegacyA1", "draft_content.json"))
            )

    def test_lite_reuses_one_material_for_repeated_pointer_png(self):
        pointer_path = "C:/media/shared-pointer.png"
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteSharedPointerMaterial",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {
                        "type": "pointer_overlay",
                        "source_kind": "pointer_overlay",
                        "start": 2.0,
                        "end": 3.0,
                        "doc_item_id": "pointer-1",
                        "asset_paths": [pointer_path],
                    },
                    {
                        "type": "pointer_overlay",
                        "source_kind": "pointer_overlay",
                        "start": 5.0,
                        "end": 6.0,
                        "doc_item_id": "pointer-2",
                        "asset_paths": [pointer_path],
                    },
                ],
                "review_items": [
                    {
                        "id": "pointer-1",
                        "kind": "pointer_overlay",
                        "source_text": "pointer one",
                        "start": 2.0,
                        "end": 3.0,
                    },
                    {
                        "id": "pointer-2",
                        "kind": "pointer_overlay",
                        "source_text": "pointer two",
                        "start": 5.0,
                        "end": 6.0,
                    },
                ],
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            execute_revision_request(request, drafts_root=drafts_root, mock_media=True)
            with open(
                os.path.join(drafts_root, "LiteSharedPointerMaterial", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as content_file:
                content = json.load(content_file)

        visual_segments = _track(content, LITE_TRACKS["visual_assets"])["segments"]
        self.assertEqual(len(visual_segments), 2)
        self.assertEqual(
            len({segment["material_id"] for segment in visual_segments}),
            1,
        )

    def test_lite_mode_rejects_spoken_cut_that_only_uses_review_timestamp(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteRoughTimestampRejected",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "spoken_delete",
                        "start": 2.0,
                        "end": 4.0,
                        "label": "rough document time",
                        "doc_item_id": "item-rough",
                    }
                ],
                "review_items": [
                    {
                        "id": "item-rough",
                        "kind": "spoken_delete",
                        "source_text": "00:02-00:04 delete words",
                        "start": 2.0,
                        "end": 4.0,
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as drafts_root:
            with self.assertRaisesRegex(ValueError, "search hints only"):
                execute_revision_request(request, drafts_root=drafts_root, mock_media=True)

    def test_lite_mode_rejects_non_authoritative_asr_candidate(self):
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteCandidateRejected",
                    "source_video": "C:/media/source.mp4",
                    "media_duration_seconds": 10.0,
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "spoken_delete",
                        "start": 2.0,
                        "end": 3.0,
                        "doc_item_id": "item-candidate",
                        "evidence": {
                            "review_timestamp_role": "search_hint",
                            "delete": "candidate",
                            "must_keep": ["before", "after"],
                            "strategy": "precision_first",
                            "asr_alignment": {
                                "status": "pass",
                                "provider": "fallback-asr",
                                "adapter_version": "candidate-v1",
                                "granularity": "word",
                                "resolved_cut_window": [2.0, 3.0],
                            },
                        },
                    }
                ],
                "review_items": [
                    {
                        "id": "item-candidate",
                        "kind": "spoken_delete",
                        "source_text": "00:02 delete candidate",
                        "start": 2.0,
                        "end": 3.0,
                    }
                ],
            }
        )
        problems = _spoken_cut_alignment_problems(request.edits[0], request.review_items[0])
        self.assertTrue(any("model/resource_id" in problem for problem in problems))
        self.assertTrue(any("source audio SHA-256" in problem for problem in problems))
        self.assertTrue(any("authoritative_cut_boundary" in problem for problem in problems))
        self.assertTrue(any("matches are missing" in problem for problem in problems))

    def test_implicit_quoted_source_and_ellipsis_are_audio_deletions(self):
        implicit = (
            "00\uff1a13-00\uff1a26\uff0c\u201c\u8fd9\u4e24\u4e2a\u56fd\u5bb6\u548c\u5730\u533a\u5462"
            "\u2026\u2026\u6240\u4ee5\u6211\u4eec\u628a\u5b83\u653e\u5230\u4e00\u8d77\u6765\u8bf4\u554a\u3002\u201d"
        )
        self.assertEqual(_classify_review_text(implicit), "ellipsis_range_delete")
        chinese_full_stop_ellipsis = (
            "02:20-02:56，删除\u201c那么它对各国的。。。指导的作用。对吧？\u201d"
        )
        self.assertEqual(
            _classify_review_text(chinese_full_stop_ellipsis),
            "ellipsis_range_delete",
        )
        gap = (
            "09\uff1a20\uff0c\u5220\u9664\u201c\u5bb0\u76f8\u201d\u548c"
            "\u201c\u4e3a\u6838\u5fc3\u201d\u4e2d\u95f4\u7684\u505c\u987f"
        )
        self.assertEqual(_classify_review_text(gap), "gap_delete")

        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {
                    "review_items": [
                        {
                            "id": "implicit-1",
                            "kind": "review_only",
                            "source_text": implicit,
                        }
                    ]
                },
                {
                    "draft_name": "ImplicitDeleteDraft",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                item = json.load(source_file)["review_items"][0]

        self.assertEqual(item["kind"], "ellipsis_range_delete")
        self.assertTrue(item["execution_required"])
        self.assertEqual(item["review_timestamp_role"], "search_hint")

    def test_blue_rich_text_fragments_stay_independent(self):
        source_text = (
            "06\uff1a14\uff0c\u201c\u5728\u8fd9\u5f20\u5443\u753b\u4e0a\u201d"
            "\u5220\u9664\u84dd\u8272\u5b57"
        )
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {
                    "review_items": [
                        {
                            "id": "blue-1",
                            "kind": "visual_delete",
                            "source_text": source_text,
                            "text_runs": [
                                {"text": "\u5728\u8fd9\u5f20", "color": "#111111"},
                                {"text": "\u5443", "color": "rgb(36,91,219)"},
                                {"text": "\u753b\u4e0a", "color": "#111111"},
                                {"text": "\u753b", "color": [36, 91, 219]},
                            ],
                        }
                    ]
                },
                {
                    "draft_name": "BlueSpanDraft",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                item = json.load(source_file)["review_items"][0]

        self.assertEqual(item["kind"], "colored_span_delete")
        self.assertTrue(item["execution_required"])
        self.assertEqual(item["evidence"]["colored_span_status"], "resolved")
        self.assertEqual(
            [span["text"] for span in item["evidence"]["colored_spans"]],
            ["\u5443", "\u753b"],
        )

    def test_blue_delete_without_markup_is_not_guessed_as_whole_sentence(self):
        source_text = (
            "08\uff1a51-08\uff1a56\uff0c\u201c\u4f60\u770b\u3002\u90a3\u4e2a\u8bae\u4f1a\u201d"
            "\u5220\u9664\u84dd\u8272\u5b57"
        )
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {"review_items": [{"id": "blue-missing", "source_text": source_text}]},
                {
                    "draft_name": "BlueMissingDraft",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                item = json.load(source_file)["review_items"][0]

        self.assertEqual(_classify_review_text(source_text), "review_only")
        self.assertEqual(item["kind"], "review_only")
        self.assertFalse(item["execution_required"])
        self.assertEqual(item["execution_status"], "label_only_unresolved")
        self.assertEqual(item["evidence"]["colored_spans"], [])
        self.assertEqual(item["evidence"]["colored_span_status"], "missing_markup")

    def test_red_rich_text_fragments_stay_independent(self):
        source_text = (
            "05:21、05:23、05:30，\u201c当时法国就是法兰西第二帝国嘛，"
            "他的皇帝拿破仑三世和 10 万官兵就成了俘虏，这法法军就惨败\u201d，删除红字"
        )
        with tempfile.TemporaryDirectory() as output_dir:
            compiled = compile_review_job(
                {
                    "review_items": [
                        {
                            "id": "red-1",
                            "source_text": source_text,
                            "markup": (
                                '当时<span text-color="rgb(216,57,49)">法国就是</span>'
                                '法兰西第二帝国<span text-color="#d83931">嘛</span>，'
                                '这<span text-color="rgb(216, 57, 49)">法</span>法军就惨败'
                            ),
                        }
                    ]
                },
                {
                    "draft_name": "RedSpanDraft",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                },
                output_dir,
            )
            with open(compiled["doc_items"], "r", encoding="utf-8") as source_file:
                item = json.load(source_file)["review_items"][0]

        self.assertEqual(item["kind"], "colored_span_delete")
        self.assertTrue(item["execution_required"])
        self.assertEqual(item["evidence"]["colored_span_status"], "resolved")
        self.assertEqual(
            [span["text"] for span in item["evidence"]["colored_spans"]],
            ["法国就是", "嘛", "法"],
        )

    def test_precise_semantic_requires_must_keep_safe_boundary_evidence(self):
        request = _load_request(
            {
                "project": {
                    "draft_name": "BoundaryEvidenceDraft",
                    "source_video": "C:/media/source.mp4",
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "ellipsis_range_delete",
                        "start": 2.0,
                        "end": 3.0,
                        "doc_item_id": "range-1",
                        "evidence": {
                            "review_timestamp_role": "search_hint",
                            "delete": "start...end",
                            "must_keep": ["before", "after"],
                            "strategy": "precision_first",
                            "asr_alignment": {
                                "status": "pass",
                                "provider": "test-asr",
                                "model": "test-model",
                                "adapter_version": "1",
                                "granularity": "character",
                                "input_sha256": "d" * 64,
                                "authoritative_cut_boundary": True,
                                "matches": [{"text": "range", "start": 2.0, "end": 3.0}],
                                "resolved_cut_window": [2.0, 3.0],
                            },
                        },
                    }
                ],
            }
        )
        problems = _spoken_cut_alignment_problems(request.edits[0], None)
        self.assertTrue(any("boundary_refinement evidence is missing" in row for row in problems))

        evidence = request.edits[0].evidence
        evidence["boundary_refinement"] = {
            "status": "asr_character_edge",
            "resolved_cut_window": [2.0, 3.0],
            "crossed_must_keep": False,
        }
        problems = _spoken_cut_alignment_problems(request.edits[0], None)
        self.assertFalse(any("boundary_refinement" in row for row in problems))

    def test_legacy_colored_span_multi_window_evidence_uses_window_index(self):
        windows = [[513.44, 513.64], [513.76, 514.0]]
        alignment = {
            "status": "pass",
            "provider": "volc_asr",
            "resource_id": "volc.bigasr.auc",
            "adapter_version": "auto-cut-volc-asr-v4",
            "granularity": "word",
            "input_sha256": "a" * 64,
            "authoritative_cut_boundary": True,
            "matches": [
                {"text": "我", "start": 513.44, "end": 513.56},
                {"text": "们", "start": 513.56, "end": 513.64},
                {"text": "看", "start": 513.76, "end": 514.0},
            ],
            "resolved_cut_window": [513.44, 514.0],
            "resolved_cut_windows": windows,
        }
        common_evidence = {
            "review_timestamp_role": "search_hint",
            "delete": "我们就看",
            "must_keep": ["北朝", "进入"],
            "strategy": "precision_first",
            "colored_span_status": "resolved",
            "colored_spans": [
                {"text": "我们", "color": "rgb(36,91,219)"},
                {"text": "看", "color": "rgb(36,91,219)"},
            ],
            "asr_alignment": alignment,
            "source_cut_windows": windows,
            "resolved_cut_windows": windows,
            "resolved_cut_window": [513.44, 514.0],
            "boundary_refinement": {
                "status": "asr_character_edge",
                "resolved_cut_window": [513.44, 514.0],
                "crossed_must_keep": False,
            },
        }
        request = _load_request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LegacyColoredMultiWindow",
                    "source_video": "C:/media/source.mp4",
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "colored_span_delete",
                        "start": start,
                        "end": end,
                        "doc_item_id": "colored-multi",
                        "evidence": {**common_evidence, "window_index": index},
                    }
                    for index, (start, end) in enumerate(windows, start=1)
                ],
            }
        )

        for edit in request.edits:
            self.assertEqual(_spoken_cut_alignment_problems(edit, None), [])

        request.edits[1].evidence["window_index"] = 3
        problems = _spoken_cut_alignment_problems(request.edits[1], None)
        self.assertTrue(any("window_index" in problem for problem in problems))

    def test_review_marker_semantic_backgrounds_are_distinct(self):
        colors = ReviewMarkerOpsMixin.REVIEW_MARKER_BACKGROUND_COLORS
        self.assertNotEqual(colors["ellipsis_range_delete"], colors["colored_span_delete"])
        self.assertNotEqual(colors["colored_span_delete"], colors["gap_delete"])

    def test_lite_unknown_and_duration_changing_instructions_are_label_only(self):
        self.assertEqual(
            _classify_review_text("00:01 新增从未支持过的镜头旋转效果"),
            "review_only",
        )
        for kind, source_text in (
            ("brand_new_effect", "00:01 执行新的效果"),
            ("speed_change", "00:02 画面加速到两倍"),
            ("visual_overlay", "00:03 把视频延长 2 秒"),
            ("freeze_frame", "00:04 插入 1 秒静帧"),
        ):
            with self.subTest(kind=kind):
                self.assertFalse(lite_execution_required(kind, source_text, True))
        self.assertTrue(lite_execution_required("spoken_delete", "00:05 删除“重复词”", True))

    def test_lite_pointer_removal_is_label_only_unless_readded(self):
        for source_text in (
            "00:06 删除屏幕上的小手",
            "00:07 删除小手",
            "00:08 把小手删掉",
            "00:09 去除画面中的小手",
            "00:10 把小手拿掉",
            "00:11 这里不要小手",
            "00:12 删除小手添加的动画",
            "00:12 删除小手，再添加动画",
        ):
            with self.subTest(source_text=source_text):
                self.assertFalse(lite_execution_required("pointer_overlay", source_text, True))

        for source_text in (
            "00:13 删除原小手并重新添加",
            "00:14 小手移除后再加一个",
            "00:15 删除原小手，然后添加一个新小手",
            "00:16 移除后再加一个",
        ):
            with self.subTest(source_text=source_text):
                self.assertTrue(lite_execution_required("pointer_overlay", source_text, True))


if __name__ == "__main__":
    unittest.main()
