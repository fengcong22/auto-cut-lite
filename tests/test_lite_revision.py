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

from utils.lite_revision import LITE_TRACKS
from utils.review_job_compiler import compile_review_job
from utils.revision_runner import execute_revision_request, load_revision_request


def _load_request(payload):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "request.json")
        with open(path, "w", encoding="utf-8") as request_file:
            json.dump(payload, request_file, ensure_ascii=False)
        return load_revision_request(path)


def _track(content, name):
    return next(track for track in content["tracks"] if track["name"] == name)


class LiteRevisionTests(unittest.TestCase):
    def test_review_job_compiler_preserves_lite_mode_without_segmented_audio_gate(self):
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
        self.assertEqual(request_payload["audio_delivery_plan"]["mode"], "legacy")

    def test_load_revision_request_defaults_to_full_and_accepts_lite(self):
        base = {
            "project": {
                "draft_name": "ModeDraft",
                "source_video": "C:/media/source.mp4",
            }
        }
        self.assertEqual(_load_request(base).workflow_mode, "full")
        self.assertEqual(_load_request({**base, "workflow_mode": "lite"}).workflow_mode, "lite")
        with self.assertRaisesRegex(ValueError, "workflow_mode"):
            _load_request({**base, "workflow_mode": "compact"})

    def test_lite_mode_writes_fixed_visible_tracks_without_changing_duration(self):
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
                    "start": 2.0,
                    "end": 4.0,
                    "label": "delete summary",
                    "doc_item_id": "item-1",
                    "visual_plan": {"reuse_audio": False},
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

        self.assertEqual(len(original["segments"]), 1)
        self.assertEqual(original["segments"][0]["target_timerange"]["duration"], 10_000_000)
        self.assertEqual(len(cut_track["segments"]), 1)
        self.assertEqual(cut_track["segments"][0]["source_timerange"]["start"], 2_000_000)
        self.assertEqual(len(visual_track["segments"]), 1)
        self.assertEqual(len(timing_track["segments"]), 1)
        self.assertEqual(timing_track["segments"][0]["target_timerange"]["start"], 1_000_000)
        self.assertEqual(timing_track["segments"][0]["source_timerange"]["start"], 5_000_000)
        self.assertEqual(len(source_audio["segments"]), 1)
        self.assertEqual(len(reused_audio["segments"]), 1)
        self.assertEqual(reused_audio["segments"][0]["source_timerange"]["start"], 5_000_000)

        marker_tracks = [
            track for track in content["tracks"] if track["name"].startswith("Review Marker")
        ]
        marker_segments = [segment for track in marker_tracks for segment in track["segments"]]
        self.assertEqual(len(marker_segments), 3)
        self.assertTrue(all(segment["target_timerange"]["duration"] == 2_000_000 for segment in marker_segments))

    def test_lite_mode_omits_a2_and_clamps_last_marker(self):
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
                        "start": 9.5,
                        "end": 10.0,
                        "label": "last cut",
                        "doc_item_id": "item-last",
                        "visual_plan": {"reuse_audio": False},
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
        self.assertNotIn(LITE_TRACKS["reused_audio"], track_names)
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


if __name__ == "__main__":
    unittest.main()
