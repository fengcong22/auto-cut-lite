import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from utils.revision_runner import execute_revision_request, load_revision_request


def _request(payload):
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "request.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        return load_revision_request(path)


class LiteSourcePairsTests(unittest.TestCase):
    def test_manifest_source_pairs_write_each_video_in_document_order(self):
        request = _request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteOrderedPairs",
                    "source_video": "C:/media/first.mp4",
                    "audio_mode": "video_original",
                    "source_pairs": [
                        {
                            "pair_index": 0,
                            "video_path": "C:/media/first.mp4",
                            "video_sha256": "a" * 64,
                            "video_duration_seconds": 4.0,
                            "audio_mode": "video_original",
                        },
                        {
                            "pair_index": 1,
                            "video_path": "C:/media/second.mp4",
                            "video_sha256": "b" * 64,
                            "video_duration_seconds": 6.0,
                            "audio_mode": "video_original",
                        },
                    ],
                },
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=True,
            )
            with open(
                os.path.join(result["draft_path"], "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as stream:
                content = json.load(stream)

        videos = content["materials"]["videos"]
        self.assertEqual(
            [row["path"] for row in videos if row["path"].endswith(("first.mp4", "second.mp4"))],
            ["C:/media/first.mp4", "C:/media/second.mp4"],
        )
        original = next(track for track in content["tracks"] if track["name"] == "Original Video")
        self.assertEqual(
            [
                (
                    segment["target_timerange"]["start"] / 1_000_000,
                    segment["target_timerange"]["duration"] / 1_000_000,
                    next(row["path"] for row in videos if row["id"] == segment["material_id"]),
                )
                for segment in original["segments"]
            ],
            [(0.0, 4.0, "C:/media/first.mp4"), (4.0, 6.0, "C:/media/second.mp4")],
        )
        self.assertEqual(result["source_duration_seconds"], 10.0)
        self.assertEqual(
            [(row["pair_index"], row["video_path"]) for row in result["source_pairs"]],
            [(0, "C:/media/first.mp4"), (1, "C:/media/second.mp4")],
        )

    def test_manifest_source_pairs_use_extracted_source_audio(self):
        request = _request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteExtractedSourceAudio",
                    "source_video": "C:/media/first.mp4",
                    "source_audio": "C:/media/first.m4a",
                    "audio_mode": "video_original",
                    "source_pairs": [
                        {
                            "pair_index": 0,
                            "video_path": "C:/media/first.mp4",
                            "video_sha256": "a" * 64,
                            "source_audio_path": "C:/media/first.m4a",
                            "source_audio_sha256": "b" * 64,
                            "video_duration_seconds": 4.0,
                            "audio_mode": "video_original",
                        }
                    ],
                },
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=True,
            )
            with open(
                os.path.join(result["draft_path"], "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as stream:
                content = json.load(stream)

        audio_paths = [row["path"] for row in content["materials"]["audios"]]
        self.assertIn("C:/media/first.m4a", audio_paths)
        self.assertNotIn("C:/media/first.mp4", audio_paths)
        self.assertEqual(
            result["source_pairs"][0]["source_audio_path"],
            "C:/media/first.m4a",
        )

    def test_manifest_source_pair_video_stops_before_longer_audio_tail(self):
        request = _request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LitePairContainerTail",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": "C:/media/source.m4a",
                    "media_duration_seconds": 627.589002,
                    "audio_mode": "video_original",
                    "source_pairs": [
                        {
                            "pair_index": 0,
                            "video_path": "C:/media/source.mp4",
                            "video_sha256": "a" * 64,
                            "source_audio_path": "C:/media/source.m4a",
                            "source_audio_sha256": "b" * 64,
                            "video_duration_seconds": 627.589002,
                            "audio_mode": "video_original",
                        }
                    ],
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

        def longer_audio(_draft, mock_audio, path, _duration, _mock):
            return mock_audio(
                "mock-lite-audio-source",
                627_589_002,
                "source.m4a",
                path,
            )

        with (
            tempfile.TemporaryDirectory() as drafts_root,
            patch(
                "utils.lite_revision._make_video_material",
                side_effect=shorter_video,
            ),
            patch(
                "utils.lite_revision._make_audio_material",
                side_effect=longer_audio,
            ),
            patch(
                "utils.lite_revision.get_duration_ffprobe_cached",
                return_value=627.589002,
            ),
        ):
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=False,
                runtime_integrity_receipt={"status": "pass"},
            )
            with open(
                os.path.join(result["draft_path"], "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as stream:
                content = json.load(stream)

        original = next(
            track for track in content["tracks"] if track["name"] == "Original Video"
        )
        source_audio = next(
            track
            for track in content["tracks"]
            if track["name"] == "Separated Source Audio"
        )
        self.assertEqual(
            original["segments"][-1]["source_timerange"]["duration"],
            627_480_000,
        )
        self.assertEqual(
            source_audio["segments"][-1]["source_timerange"]["duration"],
            627_589_002,
        )
        self.assertEqual(content["duration"], 627_589_002)

    def test_manifest_source_pairs_replace_audio_per_pair(self):
        request = _request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteReplacementPairs",
                    "source_video": "C:/media/first.mp4",
                    "audio_mode": "replace_original",
                    "duration_tolerance_seconds": 0.1,
                    "source_pairs": [
                        {
                            "pair_index": 0,
                            "video_path": "C:/media/first.mp4",
                            "video_sha256": "a" * 64,
                            "video_duration_seconds": 4.0,
                            "audio_mode": "replace_original",
                            "replacement_audio_path": "C:/media/first.wav",
                            "replacement_audio_sha256": "c" * 64,
                            "audio_duration_seconds": 4.0,
                        },
                        {
                            "pair_index": 1,
                            "video_path": "C:/media/second.mp4",
                            "video_sha256": "b" * 64,
                            "video_duration_seconds": 6.0,
                            "audio_mode": "replace_original",
                            "replacement_audio_path": "C:/media/second.wav",
                            "replacement_audio_sha256": "d" * 64,
                            "audio_duration_seconds": 6.0,
                        },
                    ],
                },
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            result = execute_revision_request(
                request,
                drafts_root=drafts_root,
                mock_media=True,
            )
            with open(
                os.path.join(result["draft_path"], "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as stream:
                content = json.load(stream)

        audios = content["materials"]["audios"]
        self.assertEqual(
            [row["path"] for row in audios if row["path"].endswith(("first.wav", "second.wav"))],
            ["C:/media/first.wav", "C:/media/second.wav"],
        )
        replacement = next(track for track in content["tracks"] if track["name"] == "Replacement Audio")
        self.assertEqual(
            [
                (
                    segment["target_timerange"]["start"] / 1_000_000,
                    segment["target_timerange"]["duration"] / 1_000_000,
                    next(row["path"] for row in audios if row["id"] == segment["material_id"]),
                    segment.get("volume"),
                )
                for segment in replacement["segments"]
            ],
            [
                (0.0, 4.0, "C:/media/first.wav", 1.0),
                (4.0, 6.0, "C:/media/second.wav", 1.0),
            ],
        )
        self.assertEqual(result["source_pairs"][1]["replacement_audio_path"], "C:/media/second.wav")

    def test_manifest_source_pair_delete_crossing_boundary_is_split_per_material(self):
        request = _request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteCrossPairDelete",
                    "source_video": "C:/media/first.mp4",
                    "audio_mode": "video_original",
                    "source_pairs": [
                        {
                            "pair_index": 0,
                            "video_path": "C:/media/first.mp4",
                            "video_sha256": "a" * 64,
                            "video_duration_seconds": 4.0,
                            "audio_mode": "video_original",
                        },
                        {
                            "pair_index": 1,
                            "video_path": "C:/media/second.mp4",
                            "video_sha256": "b" * 64,
                            "video_duration_seconds": 6.0,
                            "audio_mode": "video_original",
                        },
                    ],
                },
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "phrase_delete",
                        "start": 3.0,
                        "end": 5.0,
                        "doc_item_id": "cross-pair",
                        "label": "跨段删除",
                        "evidence": {
                            "review_timestamp_role": "search_hint",
                            "delete": "跨段删除",
                            "must_keep": ["前文", "后文"],
                            "strategy": "precision_first",
                            "boundary_refinement": {
                                "status": "asr_character_edge",
                                "crossed_must_keep": False,
                                "resolved_cut_window": [3.0, 5.0],
                            },
                            "asr_alignment": {
                                "status": "pass",
                                "provider": "test-provider",
                                "model": "test-model",
                                "adapter_version": "1",
                                "granularity": "word",
                                "input_sha256": "e" * 64,
                                "authoritative_cut_boundary": True,
                                "words": [
                                    {"text": "跨段删除", "start": 3.0, "end": 5.0}
                                ],
                                "resolved_cut_window": [3.0, 5.0],
                            },
                        },
                    }
                ],
                "review_items": [
                    {
                        "id": "cross-pair",
                        "kind": "phrase_delete",
                        "source_text": "03:00 删除跨段画面",
                        "start": 3.0,
                        "end": 5.0,
                        "execution_required": True,
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
                os.path.join(result["draft_path"], "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as stream:
                content = json.load(stream)

        cut = next(track for track in content["tracks"] if track["name"] == "Lite Cut Segments")
        self.assertEqual(
            [
                (segment["target_timerange"]["start"] / 1_000_000, segment["target_timerange"]["duration"] / 1_000_000)
                for segment in cut["segments"]
            ],
            [(3.0, 1.0), (4.0, 1.0)],
        )
        reused = next(track for track in content["tracks"] if track["name"] == "Lite Reused Audio")
        self.assertEqual(len(reused["segments"]), 2)
        self.assertEqual(result["source_duration_seconds"], 10.0)

    def test_manifest_source_pair_replacement_duration_mismatch_blocks_before_write(self):
        request = _request(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "LiteMismatchedPair",
                    "source_video": "C:/media/first.mp4",
                    "audio_mode": "replace_original",
                    "duration_tolerance_seconds": 0.5,
                    "source_pairs": [
                        {
                            "pair_index": 0,
                            "video_path": "C:/media/first.mp4",
                            "video_sha256": "a" * 64,
                            "video_duration_seconds": 4.0,
                            "audio_mode": "replace_original",
                            "replacement_audio_path": "C:/media/first.wav",
                            "replacement_audio_sha256": "c" * 64,
                            "audio_duration_seconds": 6.0,
                        }
                    ],
                },
            }
        )

        with tempfile.TemporaryDirectory() as drafts_root:
            with self.assertRaisesRegex(ValueError, "video/audio duration"):
                execute_revision_request(
                    request,
                    drafts_root=drafts_root,
                    mock_media=True,
                )


if __name__ == "__main__":
    unittest.main()
