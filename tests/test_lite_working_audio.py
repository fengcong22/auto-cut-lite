"""Regressions for source-preserving Lite working-audio delivery."""

import json
import os
import tempfile
import unittest

from utils.revision_runner import execute_revision_request

from tests.test_lite_source_pairs import _request


def _delete(item_id, start, end):
    return {
        "type": "delete", "source_kind": "phrase_delete",
        "start": start, "end": end, "doc_item_id": item_id,
        "label": "删除这句",
        "evidence": {
            "review_timestamp_role": "search_hint", "delete": "删除这句",
            "must_keep": ["前文", "后文"], "strategy": "precision_first",
            "boundary_refinement": {
                "status": "asr_character_edge", "crossed_must_keep": False,
                "resolved_cut_window": [start, end],
            },
            "asr_alignment": {
                "status": "pass", "provider": "test-provider", "model": "test-model",
                "adapter_version": "1", "granularity": "word", "input_sha256": "f" * 64,
                "authoritative_cut_boundary": True,
                "words": [{"text": "删除这句", "start": start, "end": end}],
                "resolved_cut_window": [start, end],
            },
        },
    }


def _payload(*, mixed=False, cuts=True, legacy=False):
    pairs = [{
        "pair_index": 0, "video_path": "C:/media/one.mp4", "video_sha256": "a" * 64,
        "source_audio_path": "C:/media/one-original.wav", "source_audio_sha256": "b" * 64,
        "video_duration_seconds": 4.0, "audio_mode": "replace_original",
        "replacement_audio_path": "C:/media/one-restored.wav", "replacement_audio_sha256": "c" * 64,
        "audio_duration_seconds": 4.0,
    }]
    if mixed:
        pairs.append({
            "pair_index": 1, "video_path": "C:/media/two.mp4", "video_sha256": "d" * 64,
            "source_audio_path": "C:/media/two-original.wav", "source_audio_sha256": "e" * 64,
            "video_duration_seconds": 4.0, "audio_mode": "video_original",
        })
    project = {
        "draft_name": "WorkingAudioRegression", "source_video": pairs[0]["video_path"],
        "source_audio": pairs[0]["source_audio_path"],
        "replacement_audio": pairs[0]["replacement_audio_path"],
        "media_duration_seconds": 8.0 if mixed else 4.0,
    }
    if not legacy:
        project.update(audio_mode="replace_original", source_pairs=pairs)
    edits = [_delete("first", 1.0, 2.0), _delete("adjacent", 2.0, 3.0)] if cuts else []
    if cuts and mixed:
        edits.append(_delete("second", 5.0, 6.0))
    return {"workflow_mode": "lite", "project": project, "edits": edits}


def _audio_rows(content):
    materials = {row["id"]: row["path"] for row in content["materials"]["audios"]}
    return [(track["name"], materials[segment["material_id"]], segment)
            for track in content["tracks"] if track["type"] == "audio"
            for segment in track["segments"]]


class LiteWorkingAudioTests(unittest.TestCase):
    def _write(self, payload):
        with tempfile.TemporaryDirectory() as root:
            result = execute_revision_request(_request(payload), drafts_root=root, mock_media=True)
            with open(os.path.join(result["draft_path"], "draft_content.json"), encoding="utf-8") as stream:
                return result, json.load(stream)

    def test_replacement_a1_a2_use_restored_source_and_preserve_original(self):
        for cuts in (False, True):
            with self.subTest(cuts=cuts):
                _, content = self._write(_payload(cuts=cuts))
                rows = _audio_rows(content)
                audible = [(track, path, segment) for track, path, segment in rows if segment["volume"] > 0]
                self.assertTrue(audible)
                self.assertEqual({path for _, path, _ in audible}, {"C:/media/one-restored.wav"})
                self.assertNotIn("Replacement Audio", {track for track, _, _ in audible})
                self.assertTrue(all(segment["volume"] == 1.0 for _, _, segment in audible))
                self.assertIn("C:/media/one-original.wav", {row["path"] for row in content["materials"]["audios"]})
                self.assertEqual(content["duration"], 4_000_000)
                self.assertEqual(len([row for row in audible if row[0] == "Lite Reused Audio"]), 2 if cuts else 0)
                self.assertEqual(sum(row[2]["target_timerange"]["duration"] for row in audible), 4_000_000)

    def test_mixed_modes_choose_working_audio_independently(self):
        _, content = self._write(_payload(mixed=True))
        for _, path, segment in _audio_rows(content):
            if not segment["volume"]:
                continue
            start = segment["target_timerange"]["start"]
            self.assertEqual(path, "C:/media/one-restored.wav" if start < 4_000_000 else "C:/media/two-original.wav")
        self.assertEqual(content["duration"], 8_000_000)

    def test_legacy_single_source_uses_replacement_in_a1_and_a2(self):
        _, content = self._write(_payload(legacy=True))
        self.assertEqual({path for _, path, segment in _audio_rows(content) if segment["volume"] > 0},
                         {"C:/media/one-restored.wav"})
        self.assertIn("C:/media/one-original.wav", {row["path"] for row in content["materials"]["audios"]})

    def test_short_replacement_cannot_use_duration_tolerance_to_hide_gap(self):
        payload = _payload(cuts=False)
        payload["project"]["duration_tolerance_seconds"] = 3.0
        payload["project"]["source_pairs"][0]["audio_duration_seconds"] = 3.9
        with self.assertRaisesRegex(ValueError, "cover|short"):
            self._write(payload)

    def test_explicit_video_original_ignores_replacement_as_working_source(self):
        payload = _payload(legacy=True)
        payload["project"]["audio_mode"] = "video_original"
        _, content = self._write(payload)
        self.assertEqual({path for _, path, segment in _audio_rows(content) if segment["volume"] > 0},
                         {"C:/media/one-original.wav"})

    def test_replacement_mode_without_audio_fails_instead_of_falling_back(self):
        payload = _payload(legacy=True)
        payload["project"].update(audio_mode="replace_original", replacement_audio="")
        with self.assertRaisesRegex(ValueError, "replacement"):
            self._write(payload)

    def test_legacy_segmented_plan_cannot_switch_back_to_original(self):
        payload = _payload(legacy=True)
        rows = [(0, 1, "Separated Source Audio", ""), (3, 4, "Separated Source Audio", ""),
                (1, 2, "Lite Reused Audio", "first"), (2, 3, "Lite Reused Audio", "adjacent")]
        payload["audio_delivery_plan"] = {
            "mode": "segmented", "lite_a2_audible": True,
            "segments": [{
                "id": f"row-{index}", "role": "reference" if item else "source",
                "asset_path": payload["project"]["replacement_audio"], "track_name": track,
                "source_start": start, "timeline_start": start, "duration": end - start,
                "volume": 1.0, "doc_item_id": item,
            } for index, (start, end, track, item) in enumerate(rows)],
        }
        _, content = self._write(payload)
        self.assertEqual({path.replace("\\", "/") for _, path, segment in _audio_rows(content) if segment["volume"] > 0},
                         {"C:/media/one-restored.wav"})
        payload["audio_delivery_plan"]["segments"][-1]["asset_path"] = payload["project"]["source_audio"]
        with self.assertRaisesRegex((ValueError, RuntimeError), "working source"):
            self._write(payload)
