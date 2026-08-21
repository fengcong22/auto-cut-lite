# ruff: noqa: E402,I001
import json
import os
import sys
import tempfile
import unittest


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CURRENT_DIR)
SCRIPTS_PATH = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, SCRIPTS_PATH)

from utils.lite_revision import (
    LITE_TRACKS,
    _asset_specs,
    _lite_visual_results,
    _spoken_cut_alignment_problems,
)
from utils.review_job_compiler import compile_review_job
from utils.revision_models import _classify_review_text
from utils.revision_runner import (
    RevisionAcceptanceError,
    execute_revision_request,
    load_revision_request,
)
from core.review_marker_ops import ReviewMarkerOpsMixin


def _load_request(payload):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "request.json")
        with open(path, "w", encoding="utf-8") as request_file:
            json.dump(payload, request_file, ensure_ascii=False)
        return load_revision_request(path)


def _track(content, name):
    return next(track for track in content["tracks"] if track["name"] == name)


class LiteRevisionTests(unittest.TestCase):
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
        self.assertEqual(
            request_payload["review_items"][0]["review_timestamp_role"], "search_hint"
        )

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
            request_payload["review_items"][0]["review_timestamp_role"], "search_hint"
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
                            "words": [
                                {"text": "summary", "start": 2.0, "end": 4.0}
                            ],
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
            [(s["target_timerange"]["start"], s["target_timerange"]["duration"]) for s in original["segments"]],
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
        self.assertTrue(all(segment["target_timerange"]["duration"] == 2_000_000 for segment in marker_segments))
        text_materials = {item["id"]: item for item in content["materials"]["texts"]}
        marker_colors = {
            text_materials[segment["material_id"]]["background_color"]
            for segment in marker_segments
        }
        self.assertGreaterEqual(len(marker_colors), 3)

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
                                "words": [
                                    {"text": "ending", "start": 9.5, "end": 10.0}
                                ],
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
        problems = _spoken_cut_alignment_problems(
            request.edits[0], request.review_items[0]
        )
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

        self.assertEqual(item["kind"], "colored_span_delete")
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

    def test_review_marker_semantic_backgrounds_are_distinct(self):
        colors = ReviewMarkerOpsMixin.REVIEW_MARKER_BACKGROUND_COLORS
        self.assertNotEqual(colors["ellipsis_range_delete"], colors["colored_span_delete"])
        self.assertNotEqual(colors["colored_span_delete"], colors["gap_delete"])


if __name__ == "__main__":
    unittest.main()
