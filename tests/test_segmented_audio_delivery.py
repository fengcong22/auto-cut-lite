import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CURRENT_DIR)
SCRIPTS_PATH = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, SCRIPTS_PATH)

from utils.revision_runner import (
    _request_with_pause_results,
    _request_with_visual_results,
    _write_segmented_audio_delivery,
    audio_delivery_plan_sha256,
    build_revision_summary,
    execute_revision_request,
    load_revision_request,
)
from utils.revision_validation import validate_saved_revision_draft


def _test_wav_bytes(duration_seconds: float = 0.1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\0\0" * max(1, round(16_000 * duration_seconds)))
    return buffer.getvalue()


class TestSegmentedAudioDelivery(unittest.TestCase):
    def _load_request(self, payload):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "request.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return load_revision_request(path)

    def _segmented_payload(self):
        return {
            "project": {
                "draft_name": "SegmentedDraft",
                "source_video": "C:/media/source.mp4",
                "source_audio": "C:/media/source.wav",
            },
            "processed_audio": {
                "output_wav": "C:/qa/final_narration.wav",
            },
            "audio_delivery_plan": {
                "mode": "segmented",
                "forbid_full_length_segments": True,
                "max_single_segment_ratio": 0.9,
                "validation_only_audio_paths": ["C:/qa/final_narration.wav"],
                "segments": [
                    {
                        "id": "source-001",
                        "role": "source",
                        "asset_path": "C:/media/source-001.wav",
                        "track_name": "Narration - Source Segments",
                        "source_start": 0.0,
                        "timeline_start": 0.0,
                        "duration": 10.0,
                        "volume": 1.0,
                        "fade_in": 0.001,
                        "fade_out": 0.001,
                        "doc_item_id": "修改01",
                        "reason": "kept source narration",
                    }
                ],
            },
            "edits": [
                {
                    "type": "delete",
                    "start": 10.0,
                    "end": 11.0,
                    "label": "修改01 删除",
                }
            ],
            "markers": [],
            "preserve": {
                "source_video_material": True,
                "separated_audio_material": True,
                "replacement_audio_material": False,
                "keep_cut_points": True,
                "keep_review_markers_separate": True,
            },
        }

    def _write_with_material_duration(self, request, material_duration_us):
        class FakeMaterial:
            material_id = "parsed-audio-material"
            duration = material_duration_us

        class FakeTimerange:
            def __init__(self, start, duration):
                self.start = start
                self.duration = duration

            @property
            def end(self):
                return self.start + self.duration

        class FakeAudioSegment:
            def __init__(
                self,
                material,
                target_timerange,
                *,
                source_timerange,
                volume,
            ):
                self.material = material
                self.target_timerange = target_timerange
                self.source_timerange = source_timerange
                self.volume = volume
                self.segment_id = "parsed-audio-segment"
                if source_timerange.end > material.duration:
                    raise ValueError("source timerange exceeds material duration")

        class FakeDraft:
            class TrackType:
                audio = "audio"

            Timerange = FakeTimerange
            AudioSegment = FakeAudioSegment

            @staticmethod
            def AudioMaterial(_path):
                return FakeMaterial()

        class FakeScript:
            def __init__(self):
                self.segments = []

            def add_track(self, _track_type, _track_name):
                return None

            def add_segment(self, segment, track_name):
                self.segments.append((track_name, segment))

        class FakeProject:
            def __init__(self):
                self.script = FakeScript()

            def add_audio_fade_to_segment(self, *_args, **_kwargs):
                return None

        project = FakeProject()
        _track_names, results = _write_segmented_audio_delivery(
            project,
            request,
            draft=FakeDraft,
            MockAudioMaterial=None,
            mock_media=False,
            fallback_duration=0.0,
        )
        return project.script.segments, results

    def test_segmented_writer_preserves_confirmed_two_microsecond_material_rounding_tail(self):
        payload = self._segmented_payload()
        segment = payload["audio_delivery_plan"]["segments"][0]
        segment["source_start"] = 0.0
        segment["timeline_start"] = 0.0
        segment["duration"] = 627.589002
        request = self._load_request(payload)

        with patch(
            "utils.revision_runner.get_duration_ffprobe_cached",
            return_value=627.589002,
        ):
            segments, results = self._write_with_material_duration(request, 627_589_000)

        written_segment = segments[0][1]
        self.assertEqual(written_segment.source_timerange.duration, 627_589_002)
        self.assertEqual(written_segment.target_timerange.duration, 627_589_002)
        self.assertEqual(results[0]["duration"], 627.589002)

    def test_segmented_writer_rejects_unconfirmed_sub_millisecond_material_rounding(self):
        payload = self._segmented_payload()
        segment = payload["audio_delivery_plan"]["segments"][0]
        segment["source_start"] = 0.0
        segment["timeline_start"] = 0.0
        segment["duration"] = 627.589002
        request = self._load_request(payload)

        with patch(
            "utils.revision_runner.get_duration_ffprobe_cached",
            return_value=627.589,
        ), self.assertRaisesRegex(ValueError, "cannot confirm parsed audio duration rounding"):
            self._write_with_material_duration(request, 627_589_000)

    def test_segmented_writer_rejects_material_overrun_beyond_rounding_tolerance(self):
        payload = self._segmented_payload()
        segment = payload["audio_delivery_plan"]["segments"][0]
        segment["source_start"] = 0.0
        segment["timeline_start"] = 0.0
        segment["duration"] = 627.591
        request = self._load_request(payload)

        with self.assertRaisesRegex(ValueError, "exceeds parsed audio material duration"):
            self._write_with_material_duration(request, 627_589_000)

    def _validation_request(
        self,
        segments,
        *,
        validation_only_audio_paths=None,
        forbid_full_length_segments=True,
        max_single_segment_ratio=0.9,
        processed_audio=None,
    ):
        payload = {
            "project": {
                "draft_name": "SegmentedValidationDraft",
                "source_video": "C:/media/source.mp4",
            },
            "audio_delivery_plan": {
                "mode": "segmented",
                "forbid_full_length_segments": forbid_full_length_segments,
                "max_single_segment_ratio": max_single_segment_ratio,
                "validation_only_audio_paths": validation_only_audio_paths or [],
                "segments": segments,
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
        if processed_audio is not None:
            payload["processed_audio"] = processed_audio
        return self._load_request(payload)

    def _saved_content_for_request(self, request, *, duration_us):
        audio_materials = []
        material_id_by_path = {}
        audio_fades = []
        track_by_name = {}
        tracks = [
            {
                "name": "Original Video",
                "type": "video",
                "segments": [
                    {
                        "id": "video-segment",
                        "material_id": "video-material",
                        "target_timerange": {"start": 0, "duration": duration_us},
                    }
                ],
            }
        ]

        for index, plan_segment in enumerate(request.audio_delivery_plan.segments, start=1):
            material_id = material_id_by_path.get(plan_segment.asset_path)
            if material_id is None:
                material_id = f"audio-material-{len(audio_materials) + 1}"
                material_id_by_path[plan_segment.asset_path] = material_id
                audio_materials.append(
                    {
                        "id": material_id,
                        "path": plan_segment.asset_path,
                    }
                )

            track = track_by_name.get(plan_segment.track_name)
            if track is None:
                track = {
                    "name": plan_segment.track_name,
                    "type": "audio",
                    "segments": [],
                }
                track_by_name[plan_segment.track_name] = track
                tracks.append(track)

            extra_material_refs = []
            if plan_segment.fade_in > 0 or plan_segment.fade_out > 0:
                fade_id = f"fade-{index}"
                audio_fades.append(
                    {
                        "id": fade_id,
                        "fade_in_duration": round(plan_segment.fade_in * 1_000_000),
                        "fade_out_duration": round(plan_segment.fade_out * 1_000_000),
                        "type": "audio_fade",
                    }
                )
                extra_material_refs.append(fade_id)

            track["segments"].append(
                {
                    "id": f"actual-{plan_segment.segment_id}",
                    "material_id": material_id,
                    "source_timerange": {
                        "start": round(plan_segment.source_start * 1_000_000),
                        "duration": round(plan_segment.duration * 1_000_000),
                    },
                    "target_timerange": {
                        "start": round(plan_segment.timeline_start * 1_000_000),
                        "duration": round(plan_segment.duration * 1_000_000),
                    },
                    "volume": plan_segment.volume,
                    "extra_material_refs": extra_material_refs,
                }
            )

        return {
            "duration": duration_us,
            "tracks": tracks,
            "materials": {
                "videos": [],
                "audios": audio_materials,
                "audio_fades": audio_fades,
                "texts": [],
            },
        }

    def test_segmented_plan_keeps_processed_audio_validation_only(self):
        request = self._load_request(self._segmented_payload())

        self.assertEqual(request.project.replacement_audio, "")
        self.assertEqual(request.audio_delivery_plan.mode, "segmented")
        self.assertEqual(len(request.audio_delivery_plan.segments), 1)
        self.assertEqual(request.audio_delivery_plan.segments[0].role, "source")

        summary = build_revision_summary(request)
        self.assertFalse(summary["full_track_replacement_audio"])
        self.assertEqual(summary["audio_delivery"]["mode"], "segmented")
        self.assertEqual(summary["audio_delivery"]["segment_count"], 1)

    def test_segmented_plan_rejects_validation_only_path_as_segment_asset(self):
        payload = self._segmented_payload()
        payload["audio_delivery_plan"]["validation_only_audio_paths"] = ["C:/media/source-001.wav"]

        with self.assertRaisesRegex(ValueError, "validation-only"):
            self._load_request(payload)

    def test_saved_runtime_omitted_zero_starts_and_default_volume_are_equivalent(self):
        request = self._validation_request(
            [
                {
                    "id": "source-001",
                    "role": "source",
                    "asset_path": "C:/media/source-part.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 2.0,
                    "volume": 1.0,
                }
            ]
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)
        segment = content["tracks"][1]["segments"][0]
        del segment["source_timerange"]["start"]
        del segment["target_timerange"]["start"]
        del segment["volume"]

        validation = validate_saved_revision_draft(request, content)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(validation["metrics"]["audio_delivery"]["matched"], 1)

    def test_segmented_plan_rejects_duplicate_overlap_and_non_finite_values(self):
        cases = []

        duplicate = self._segmented_payload()
        duplicate["audio_delivery_plan"]["segments"].append(
            copy.deepcopy(duplicate["audio_delivery_plan"]["segments"][0])
        )
        cases.append(duplicate)

        overlap = self._segmented_payload()
        overlapping_segment = copy.deepcopy(overlap["audio_delivery_plan"]["segments"][0])
        overlapping_segment["id"] = "source-002"
        overlapping_segment["timeline_start"] = 5.0
        overlap["audio_delivery_plan"]["segments"].append(overlapping_segment)
        cases.append(overlap)

        non_finite = self._segmented_payload()
        non_finite["audio_delivery_plan"]["segments"][0]["duration"] = "NaN"
        cases.append(non_finite)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self._load_request(payload)

    def test_segmented_plan_rejects_audible_reference_segment(self):
        payload = self._segmented_payload()
        segment = payload["audio_delivery_plan"]["segments"][0]
        segment["role"] = "reference"
        segment["track_name"] = "Separated Source Audio (Reference)"
        segment["volume"] = 1.0

        with self.assertRaisesRegex(ValueError, "reference.*volume"):
            self._load_request(payload)

    def test_request_rebuild_helpers_preserve_audio_delivery_plan(self):
        request = self._load_request(self._segmented_payload())

        with_visual_results = _request_with_visual_results(
            request,
            [{"item_id": "visual-item", "evidence": {}, "validation": {}}],
        )
        with_pause_results = _request_with_pause_results(
            request,
            [{"item_id": "pause-item"}],
        )

        self.assertEqual(with_visual_results.audio_delivery_plan, request.audio_delivery_plan)
        self.assertEqual(with_pause_results.audio_delivery_plan, request.audio_delivery_plan)

    def test_segmented_plan_writes_only_explicit_audio_tracks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assets = {}
            for name in ("reference.wav", "source.wav", "replacement.wav", "repair.wav"):
                path = os.path.join(tmpdir, name)
                with open(path, "wb") as f:
                    f.write(b"fixture")
                assets[name] = path
            qa_path = os.path.join(tmpdir, "final_narration_qa.wav")
            qa_bytes = _test_wav_bytes(8.0)
            with open(qa_path, "wb") as f:
                f.write(qa_bytes)
            summary_path = os.path.join(tmpdir, "final_narration_qa.summary.json")
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "candidate_audio_sha256": hashlib.sha256(qa_bytes).hexdigest(),
                        "asr_identity": {
                            "provider": "test-provider",
                            "model": "test-model",
                            "adapter_version": "1",
                        },
                        "status_counts": {"pass": 1},
                        "rows": [
                            {
                                "id": "修改01",
                                "status": "pass",
                                "strategy": "hybrid",
                                "source_cut_windows": [[10.0, 11.0]],
                                "mapped_join_times": [8.0],
                                "local_joined_text": "kept context",
                                "delete_hits": [],
                                "keep_hits": {},
                                "semantic_join_validation": {"status": "pass"},
                            }
                        ],
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

            payload = {
                "project": {
                    "draft_name": "SegmentedDraft",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": assets["reference.wav"],
                },
                "processed_audio": {
                    "output_wav": qa_path,
                    "validation_summary": summary_path,
                },
                "audio_delivery_plan": {
                    "mode": "segmented",
                    "validation_only_audio_paths": [qa_path],
                    "segments": [
                        {
                            "id": "reference-001",
                            "role": "reference",
                            "asset_path": assets["reference.wav"],
                            "track_name": "Separated Source Audio (Reference)",
                            "source_start": 0.0,
                            "timeline_start": 0.0,
                            "duration": 10.0,
                            "volume": 0.0,
                        },
                        {
                            "id": "source-001",
                            "role": "source",
                            "asset_path": assets["source.wav"],
                            "track_name": "Narration - Source Segments",
                            "source_start": 0.0,
                            "timeline_start": 0.0,
                            "duration": 5.0,
                            "volume": 1.0,
                            "fade_in": 0.001,
                            "fade_out": 0.001,
                        },
                        {
                            "id": "replacement-001",
                            "role": "replacement_video",
                            "asset_path": assets["replacement.wav"],
                            "track_name": "Narration - Replacement Clip",
                            "source_start": 2.0,
                            "timeline_start": 5.0,
                            "duration": 2.0,
                            "volume": 1.0,
                        },
                        {
                            "id": "repair-001",
                            "role": "repair",
                            "asset_path": assets["source.wav"],
                            "track_name": "Local Audio Repairs",
                            "source_start": 0.0,
                            "timeline_start": 7.0,
                            "duration": 1.0,
                            "volume": 1.0,
                            "doc_item_id": "修改01",
                        },
                    ],
                },
                "edits": [
                    {
                        "type": "delete",
                        "start": 10.0,
                        "end": 11.0,
                        "label": "修改01 删除",
                    }
                ],
                "markers": [],
                "preserve": {
                    "source_video_material": True,
                    "separated_audio_material": True,
                    "replacement_audio_material": False,
                    "keep_cut_points": True,
                    "keep_review_markers_separate": True,
                },
            }
            request_path = os.path.join(tmpdir, "request.json")
            with open(request_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            request = load_revision_request(request_path)
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
            summary["audio_delivery_plan_sha256"] = audio_delivery_plan_sha256(request)
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)

            result = execute_revision_request(request, drafts_root=tmpdir, mock_media=True)
            with open(
                os.path.join(tmpdir, "SegmentedDraft", "draft_content.json"),
                "r",
                encoding="utf-8",
            ) as f:
                content = json.load(f)

        track_names = [track["name"] for track in content["tracks"]]
        self.assertIn("Separated Source Audio (Reference)", track_names)
        self.assertIn("Narration - Source Segments", track_names)
        self.assertIn("Narration - Replacement Clip", track_names)
        self.assertIn("Local Audio Repairs", track_names)
        self.assertNotIn("Separated Source Audio", track_names)
        self.assertNotIn("Replacement Audio", track_names)
        self.assertEqual(len(result["audio_delivery_results"]), 4)

        audio_paths = {item["path"] for item in content["materials"]["audios"]}
        self.assertNotIn(os.path.abspath(qa_path), audio_paths)
        self.assertEqual(len(audio_paths), 3)
        self.assertEqual(len(content["materials"]["audios"]), 3)
        results_by_plan_id = {
            item["plan_segment_id"]: item for item in result["audio_delivery_results"]
        }
        self.assertEqual(
            results_by_plan_id["source-001"]["material_id"],
            results_by_plan_id["repair-001"]["material_id"],
        )
        self.assertEqual(results_by_plan_id["repair-001"]["role"], "repair")
        self.assertEqual(
            results_by_plan_id["replacement-001"]["track_name"],
            "Narration - Replacement Clip",
        )
        self.assertTrue(results_by_plan_id["replacement-001"]["segment_id"])
        reference_track = next(
            track
            for track in content["tracks"]
            if track["name"] == "Separated Source Audio (Reference)"
        )
        source_track = next(
            track for track in content["tracks"] if track["name"] == "Narration - Source Segments"
        )
        replacement_track = next(
            track for track in content["tracks"] if track["name"] == "Narration - Replacement Clip"
        )
        repair_track = next(
            track for track in content["tracks"] if track["name"] == "Local Audio Repairs"
        )
        self.assertEqual(reference_track["segments"][0]["volume"], 0.0)
        self.assertEqual(source_track["segments"][0]["volume"], 1.0)
        self.assertEqual(source_track["segments"][0]["source_timerange"]["start"], 0)
        self.assertEqual(source_track["segments"][0]["target_timerange"]["duration"], 5000000)
        self.assertEqual(
            replacement_track["segments"][0]["source_timerange"],
            {"start": 2000000, "duration": 2000000},
        )
        self.assertEqual(
            replacement_track["segments"][0]["target_timerange"],
            {"start": 5000000, "duration": 2000000},
        )
        self.assertEqual(repair_track["segments"][0]["target_timerange"]["start"], 7000000)

        fade_materials = {item["id"]: item for item in content["materials"]["audio_fades"]}
        source_fades = [
            fade_materials[material_id]
            for material_id in source_track["segments"][0]["extra_material_refs"]
            if material_id in fade_materials
        ]
        self.assertEqual(len(source_fades), 1)
        self.assertEqual(source_fades[0]["fade_in_duration"], 1000)
        self.assertEqual(source_fades[0]["fade_out_duration"], 1000)

        validation = validate_saved_revision_draft(
            request,
            content,
            draft_name="SegmentedDraft",
        )
        self.assertTrue(validation["ok"], validation["errors"])
        self.assertIn("audio_delivery", validation["metrics"])
        audio_delivery = validation["metrics"]["audio_delivery"]
        self.assertTrue(audio_delivery["enabled"])
        self.assertEqual(audio_delivery["planned"], 4)
        self.assertEqual(audio_delivery["matched"], 4)
        self.assertEqual(audio_delivery["unmatched"], [])
        self.assertEqual(audio_delivery["mismatched"], [])
        self.assertEqual(len(audio_delivery["matched_segments"]), 4)

    def test_segmented_validation_rejects_validation_only_material_not_on_track(self):
        qa_path = "C:/qa/final_narration.wav"
        request = self._validation_request(
            [
                {
                    "id": "source-001",
                    "role": "source",
                    "asset_path": "C:/media/source-part.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 2.0,
                    "volume": 1.0,
                }
            ],
            validation_only_audio_paths=[qa_path],
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)
        content["materials"]["audios"].append({"id": "qa-material", "path": qa_path})

        validation = validate_saved_revision_draft(request, content)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("validation-only" in message.lower() for message in validation["errors"])
        )
        self.assertEqual(
            validation["metrics"]["audio_delivery"]["validation_only_material_paths"],
            ["c:/qa/final_narration.wav"],
        )

    def test_segmented_validation_implicitly_excludes_full_candidate_audio(self):
        candidate_path = "C:/qa/final_candidate.wav"
        request = self._validation_request(
            [
                {
                    "id": "candidate-part-1",
                    "role": "source",
                    "asset_path": candidate_path,
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 5.0,
                },
                {
                    "id": "candidate-part-2",
                    "role": "source",
                    "asset_path": candidate_path,
                    "track_name": "Narration - Source Segments",
                    "source_start": 5.0,
                    "timeline_start": 5.0,
                    "duration": 5.0,
                },
            ],
            processed_audio={"output_wav": candidate_path},
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)

        validation = validate_saved_revision_draft(request, content)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("validation-only" in message for message in validation["errors"]),
            validation["errors"],
        )
        self.assertEqual(
            validation["metrics"]["audio_delivery"]["validation_only_material_paths"],
            ["c:/qa/final_candidate.wav"],
        )

    def test_segmented_validation_excludes_renamed_candidate_copy_by_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "final_candidate.wav")
            copied_path = os.path.join(tmpdir, "candidate_copy.wav")
            for path in (candidate_path, copied_path):
                with open(path, "wb") as f:
                    f.write(b"identical-full-candidate-bytes")

            request = self._validation_request(
                [
                    {
                        "id": "candidate-copy-part-1",
                        "role": "source",
                        "asset_path": copied_path,
                        "track_name": "Narration - Source Segments",
                        "source_start": 0.0,
                        "timeline_start": 0.0,
                        "duration": 5.0,
                    },
                    {
                        "id": "candidate-copy-part-2",
                        "role": "source",
                        "asset_path": copied_path,
                        "track_name": "Narration - Source Segments",
                        "source_start": 5.0,
                        "timeline_start": 5.0,
                        "duration": 5.0,
                    },
                ],
                processed_audio={"output_wav": candidate_path},
            )
            content = self._saved_content_for_request(request, duration_us=10_000_000)

            validation = validate_saved_revision_draft(request, content)

        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("validation-only" in message for message in validation["errors"]),
            validation["errors"],
        )
        self.assertTrue(
            any(
                os.path.basename(path).casefold() == "candidate_copy.wav"
                for path in validation["metrics"]["audio_delivery"][
                    "validation_only_material_paths"
                ]
            )
        )

    def test_segmented_validation_rejects_target_misalignment(self):
        request = self._validation_request(
            [
                {
                    "id": "source-001",
                    "role": "source",
                    "asset_path": "C:/media/source-part.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 1.0,
                    "timeline_start": 3.0,
                    "duration": 2.0,
                    "volume": 1.0,
                }
            ]
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)
        content["tracks"][1]["segments"][0]["target_timerange"]["start"] += 60_000

        validation = validate_saved_revision_draft(request, content)

        self.assertFalse(validation["ok"])
        mismatch = validation["metrics"]["audio_delivery"]["mismatched"][0]
        self.assertEqual(mismatch["plan_segment_id"], "source-001")
        self.assertIn("target_start", mismatch["fields"])

    def test_segmented_validation_rejects_missing_one_millisecond_fade(self):
        request = self._validation_request(
            [
                {
                    "id": "source-001",
                    "role": "source",
                    "asset_path": "C:/media/source-part.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 2.0,
                    "volume": 1.0,
                    "fade_in": 0.001,
                    "fade_out": 0.001,
                }
            ]
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)
        content["tracks"][1]["segments"][0]["extra_material_refs"] = []
        content["materials"]["audio_fades"] = []

        validation = validate_saved_revision_draft(request, content)

        self.assertFalse(validation["ok"])
        mismatch = validation["metrics"]["audio_delivery"]["mismatched"][0]
        self.assertEqual(mismatch["plan_segment_id"], "source-001")
        self.assertIn("fade_in", mismatch["fields"])
        self.assertIn("fade_out", mismatch["fields"])

    def test_segmented_validation_rejects_fifty_millisecond_fade_for_one_millisecond_plan(self):
        request = self._validation_request(
            [
                {
                    "id": "source-001",
                    "role": "source",
                    "asset_path": "C:/media/source-part.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 2.0,
                    "volume": 1.0,
                    "fade_in": 0.001,
                    "fade_out": 0.001,
                }
            ]
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)
        content["materials"]["audio_fades"][0]["fade_in_duration"] = 50_000
        content["materials"]["audio_fades"][0]["fade_out_duration"] = 50_000

        validation = validate_saved_revision_draft(request, content)

        self.assertFalse(validation["ok"])
        mismatch = validation["metrics"]["audio_delivery"]["mismatched"][0]
        self.assertIn("fade_in", mismatch["fields"])
        self.assertIn("fade_out", mismatch["fields"])

    def test_segmented_validation_rejects_single_narration_segment_at_ratio_limit(self):
        request = self._validation_request(
            [
                {
                    "id": "source-long",
                    "role": "source",
                    "asset_path": "C:/media/source-long.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 9.0,
                    "volume": 1.0,
                }
            ],
            max_single_segment_ratio=0.9,
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)

        validation = validate_saved_revision_draft(request, content)

        self.assertFalse(validation["ok"])
        audio_delivery = validation["metrics"]["audio_delivery"]
        self.assertEqual(audio_delivery["oversized_segment_ids"], ["source-long"])
        self.assertEqual(audio_delivery["max_planned_narration_ratio"], 0.9)

    def test_segmented_validation_allows_full_reference_and_short_narration_segments(self):
        request = self._validation_request(
            [
                {
                    "id": "reference-full",
                    "role": "reference",
                    "asset_path": "C:/media/source.wav",
                    "track_name": "Separated Source Audio (Reference)",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 10.0,
                    "volume": 0.0,
                },
                {
                    "id": "source-001",
                    "role": "source",
                    "asset_path": "C:/media/source-001.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 4.0,
                    "volume": 1.0,
                },
                {
                    "id": "source-002",
                    "role": "source",
                    "asset_path": "C:/media/source-002.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 4.0,
                    "duration": 4.0,
                    "volume": 1.0,
                },
                {
                    "id": "repair-001",
                    "role": "repair",
                    "asset_path": "C:/media/repair.wav",
                    "track_name": "Local Audio Repairs",
                    "source_start": 0.0,
                    "timeline_start": 8.0,
                    "duration": 2.0,
                    "volume": 1.0,
                },
            ],
            max_single_segment_ratio=0.9,
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)
        for track in content["tracks"]:
            if track["type"] == "audio":
                track["name"] = f"  {track['name'].swapcase()}  "

        validation = validate_saved_revision_draft(request, content)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertIn("audio_delivery", validation["metrics"])
        audio_delivery = validation["metrics"]["audio_delivery"]
        self.assertEqual(audio_delivery["matched"], 4)
        self.assertEqual(audio_delivery["max_planned_narration_ratio"], 0.4)
        self.assertEqual(audio_delivery["oversized_segment_ids"], [])

    def test_segmented_validation_does_not_reuse_actual_segment(self):
        request = self._validation_request(
            [
                {
                    "id": "source-001",
                    "role": "source",
                    "asset_path": "C:/media/source.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 2.0,
                    "volume": 1.0,
                },
                {
                    "id": "source-002",
                    "role": "source",
                    "asset_path": "C:/media/source.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 2.0,
                    "timeline_start": 2.0,
                    "duration": 2.0,
                    "volume": 1.0,
                },
            ]
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)
        content["tracks"][1]["segments"] = content["tracks"][1]["segments"][:1]

        validation = validate_saved_revision_draft(request, content)

        self.assertFalse(validation["ok"])
        audio_delivery = validation["metrics"]["audio_delivery"]
        self.assertEqual(audio_delivery["matched"], 1)
        self.assertEqual(audio_delivery["unmatched"], ["source-002"])

    def test_segmented_validation_matches_runner_absolute_material_paths(self):
        relative_asset_path = os.path.join("media", "source.wav")
        request = self._validation_request(
            [
                {
                    "id": "source-001",
                    "role": "source",
                    "asset_path": relative_asset_path,
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 4.0,
                    "volume": 1.0,
                }
            ]
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)
        content["materials"]["audios"][0]["path"] = os.path.abspath(relative_asset_path)

        validation = validate_saved_revision_draft(request, content)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertEqual(validation["metrics"]["audio_delivery"]["matched"], 1)

    def test_segmented_validation_rejects_unplanned_audio_segment(self):
        request = self._validation_request(
            [
                {
                    "id": "source-001",
                    "role": "source",
                    "asset_path": "C:/media/source.wav",
                    "track_name": "Narration - Source Segments",
                    "source_start": 0.0,
                    "timeline_start": 0.0,
                    "duration": 4.0,
                    "volume": 1.0,
                }
            ]
        )
        content = self._saved_content_for_request(request, duration_us=10_000_000)
        content["materials"]["audios"].append(
            {"id": "merged-material", "path": "C:/media/merged.wav"}
        )
        content["tracks"].append(
            {
                "name": "Merged Narration",
                "type": "audio",
                "segments": [
                    {
                        "id": "merged-segment",
                        "material_id": "merged-material",
                        "source_timerange": {"start": 0, "duration": 9_500_000},
                        "target_timerange": {"start": 0, "duration": 9_500_000},
                        "volume": 1.0,
                        "extra_material_refs": [],
                    }
                ],
            }
        )

        validation = validate_saved_revision_draft(request, content)

        self.assertFalse(validation["ok"])
        audio_delivery = validation["metrics"]["audio_delivery"]
        self.assertEqual(audio_delivery["unexpected_segment_ids"], ["merged-segment"])
        self.assertTrue(any("unplanned audio segment" in error for error in validation["errors"]))

    def test_legacy_validation_reports_audio_delivery_disabled(self):
        request = self._load_request(
            {
                "project": {
                    "draft_name": "LegacyDraft",
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
        content = {
            "duration": 10_000_000,
            "tracks": [
                {
                    "name": "Original Video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}],
                }
            ],
            "materials": {"videos": [], "audios": [], "texts": []},
        }

        validation = validate_saved_revision_draft(request, content)

        self.assertTrue(validation["ok"], validation["errors"])
        self.assertIn("audio_delivery", validation["metrics"])
        audio_delivery = validation["metrics"]["audio_delivery"]
        self.assertEqual(audio_delivery["mode"], "legacy")
        self.assertFalse(audio_delivery["enabled"])
        self.assertEqual(audio_delivery["planned"], 0)
        self.assertEqual(audio_delivery["matched"], 0)


if __name__ == "__main__":
    unittest.main()
