# ruff: noqa: E402
"""Working-audio lineage regressions independent of production readiness."""

import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from utils import review_document_runner as runner
from utils.review_audio_precision import apply_audio_plan_to_compiled_payloads
from utils.revision_models import load_revision_request
from utils.source_manifest import validate_source_pairs

from tests import test_review_document_runner as support
from tests.readiness_support import IsolatedReadinessTestCase

_write_json = support._write_json
_write_wav = support._write_wav


class WorkingAudioPipelineTests(IsolatedReadinessTestCase):
    def test_legacy_working_file_changed_after_asr_is_rejected_before_writing(self):
        helper = support.ReviewDocumentRunnerTests()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot, project_path = helper._audio_inputs(root)
            repaired = root / "repaired.wav"
            _write_wav(repaired, fill=9)
            project = json.loads(project_path.read_text(encoding="utf-8"))
            project.update(replacement_audio=str(repaired), audio_mode="replace_original")
            _write_json(project_path, project)
            with helper._patched_runtime() as mocks, patch.object(runner, "validate_working_audio_sync", return_value={"status": "pass"}):
                fake_asr = helper._fake_asr
                def mutate_after_reverse(path, **kwargs):
                    result = fake_asr(path, **kwargs)
                    if Path(path).name == "candidate_source_aligned.wav":
                        _write_wav(repaired, fill=23)
                    return result
                mocks["asr"].side_effect = mutate_after_reverse
                with self.assertRaisesRegex(runner.ReviewDocumentRunError, "changed|hash|identity"):
                    helper._run(snapshot, project_path, job_root=root / "job", drafts_root=root / "drafts",
                                package_zip=root / "out.zip", cache_root=root / "cache")
                mocks["execute"].assert_not_called()

    def test_audio_plan_keeps_original_and_replacement_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, working = root / "original.wav", root / "repaired.wav"
            _write_wav(original, fill=1)
            _write_wav(working, fill=2)
            request, _ = apply_audio_plan_to_compiled_payloads(
                {"project": {"source_audio": str(original), "replacement_audio": str(working),
                             "audio_mode": "replace_original"},
                 "preserve": {"replacement_audio_material": True}},
                {}, {"source_duration_seconds": 3.0, "rows": []},
                audio_delivery_plan={"mode": "legacy"}, source_audio_path=working,
                candidate_audio_path=None,
            )
            self.assertEqual(request["project"]["source_audio"], str(original))
            self.assertEqual(request["project"]["replacement_audio"], str(working))
            self.assertTrue(request["preserve"]["replacement_audio_material"])

    @staticmethod
    def _mixed_project():
        return {"draft_name": "Mixed", "audio_mode": "replace_original", "source_pairs": [
            {"pair_index": 0, "video_path": "first.mp4", "video_sha256": "a" * 64,
             "audio_mode": "replace_original", "replacement_audio_path": "fixed.wav",
             "replacement_audio_sha256": "b" * 64, "video_duration_seconds": 3.0,
             "audio_duration_seconds": 3.0},
            {"pair_index": 1, "video_path": "second.mp4", "video_sha256": "c" * 64,
             "audio_mode": "video_original", "video_duration_seconds": 3.0},
        ]}

    def test_mixed_pairs_validate_per_pair_mode(self):
        project = self._mixed_project()
        result = validate_source_pairs(project, 3.0)
        self.assertEqual([row["audio_mode"] for row in result["source_pairs"]],
                         ["replace_original", "video_original"])

    def test_mixed_pairs_parse_without_global_mode_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "request.json"
            _write_json(path, {"workflow_mode": "lite", "lite_cut_layout": "split_gap",
                               "project": self._mixed_project()})
            request = load_revision_request(str(path))
            self.assertEqual(request.project.source_pairs[1]["audio_mode"], "video_original")

    def test_legacy_replacement_drives_asr_plan_candidate_and_content_cache(self):
        helper = support.ReviewDocumentRunnerTests()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot, project_path = helper._audio_inputs(root)
            repaired = root / "repaired.wav"
            _write_wav(repaired, fill=9)
            project = json.loads(project_path.read_text(encoding="utf-8"))
            project.update({"replacement_audio": str(repaired), "audio_mode": "replace_original"})
            _write_json(project_path, project)
            with helper._patched_runtime() as mocks, patch.object(
                runner, "validate_working_audio_sync", return_value={"status": "pass", "strategy": "test-only"}
            ):
                def run_job(name):
                    return runner.run_review_document(
                        snapshot, project_path, job_root=root / name,
                        drafts_root=root / (name + "-drafts"),
                        package_zip=root / name / "delivery.zip",
                        cache_root=root / "cache", readiness_path=root / name / "readiness.json",
                        workflow_mode="lite",
                    )
                first = run_job("job1")
                self.assertTrue(first["ok"])
                self.assertEqual(Path(mocks["extract"].call_args_list[0].args[0]), repaired)
                request = mocks["execute"].call_args.args[0]
                self.assertEqual(request.project.source_audio, project["source_audio"])
                self.assertEqual(request.project.replacement_audio, str(repaired))
                self.assertTrue(request.audio_delivery_plan.segments)
                self.assertEqual({segment.asset_path for segment in request.audio_delivery_plan.segments},
                                 {str(repaired)})
                materials = json.loads(Path(first["output_artifacts"]["source_materials"]["path"]).read_text(encoding="utf-8"))
                self.assertEqual(materials["materials"]["source_audio_effective"]["sha256"],
                                 runner.sha256_file(Path(project["source_audio"])))
                self.assertEqual(materials["materials"]["working_audio"]["sha256"], runner.sha256_file(repaired))
                self.assertEqual(materials["materials"]["replacement_audio"]["sha256"], runner.sha256_file(repaired))
                source_asr = json.loads(Path(first["output_artifacts"]["source_asr"]["path"]).read_text(encoding="utf-8"))
                candidate_source = Path(mocks["render"].call_args.args[0])
                self.assertEqual(source_asr["input_sha256"], runner.sha256_file(candidate_source))
                with wave.open(str(candidate_source), "rb") as media:
                    self.assertEqual(int.from_bytes(media.readframes(1), "little", signed=True), 9)
                original_calls = mocks["asr"].call_count
                _write_wav(repaired, fill=23)
                second = run_job("job2")
                self.assertTrue(second["ok"])
                self.assertEqual(mocks["asr"].call_count, original_calls + 2)
                self.assertEqual(Path(mocks["extract"].call_args_list[-1].args[0]), repaired)

    def test_pre_working_audio_runner_receipts_are_not_current(self):
        self.assertNotEqual(runner.RUNNER_VERSION, "auto-cut-lite-review-document-run-v9")

    def test_old_package_receipt_rejected_even_when_zip_and_draft_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            draft = root / "OldDraft"
            draft.mkdir()
            _write_json(draft / "draft_content.json", {"tracks": []})
            relink = root / "relink.exe"
            relink.write_bytes(b"test-relink")
            archive = root / "OldDraft.zip"
            support.ReviewDocumentRunnerTests._fake_package(
                draft, archive, relink_tool=relink, name_resolution={}, execution_input_digest=""
            )
            self.assertIsNone(runner._validate_existing_package(
                archive, draft, relink_tool=relink, name_resolution={}, execution_input_digest=""
            ))

    def test_upgrade_invalidates_saved_writer_and_package_phase_receipts(self):
        helper = support.ReviewDocumentRunnerTests()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot, project = helper._audio_inputs(root)
            kwargs = dict(job_root=root / "job", drafts_root=root / "drafts",
                          package_zip=root / "delivery.zip", cache_root=root / "cache")
            with helper._patched_runtime() as mocks:
                helper._run(snapshot, project, **kwargs)
                state_path = root / "job" / "job_state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["tool_version"] = "auto-cut-lite-review-document-run-v9"
                for phase in state["phases"].values():
                    phase["tool_version"] = "auto-cut-lite-review-document-run-v9"
                _write_json(state_path, state)
                resumed = helper._run(snapshot, project, **kwargs)
                self.assertEqual(mocks["execute"].call_count, 2)
                self.assertEqual(resumed["phases"]["draft_write_validate"]["status"], "complete")
                self.assertEqual(resumed["phases"]["package_publish"]["status"], "complete")

    def test_mixed_pairs_bind_alignment_and_candidate_to_each_working_source(self):
        helper = support.ReviewDocumentRunnerTests()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot, project_path = helper._audio_inputs(root)
            first, second, repaired = root / "source.mp4", root / "second.mp4", root / "repaired.wav"
            second.write_bytes(b"second-original-video")
            # Small permitted container tail must not shift later pair audio
            # in the reverse-ASR candidate's diagnostic timebase.
            _write_wav(repaired, fill=17, duration=3.03)
            project = self._mixed_project()
            project.update({"draft_name": "Mixed", "media_duration_seconds": 6.0})
            project["source_pairs"][0].update({"video_path": str(first), "video_sha256": runner.sha256_file(first),
                "replacement_audio_path": str(repaired), "replacement_audio_sha256": runner.sha256_file(repaired)})
            project["source_pairs"][1].update({"video_path": str(second), "video_sha256": runner.sha256_file(second)})
            _write_json(project_path, project)
            with helper._patched_runtime() as mocks:
                result = runner.run_review_document(
                    snapshot, project_path, job_root=root / "job", drafts_root=root / "drafts",
                    package_zip=root / "Mixed.zip", cache_root=root / "cache",
                    readiness_path=root / "readiness.json", mock_media=True,
                )
                self.assertTrue(result["ok"])
                self.assertEqual([Path(call.args[0]) for call in mocks["extract"].call_args_list],
                                 [repaired, second])
                request = mocks["execute"].call_args.args[0]
                self.assertEqual(request.project.source_pairs[0]["replacement_audio_path"], str(repaired))
                self.assertEqual(request.project.source_pairs[1]["audio_mode"], "video_original")
                materials = json.loads(Path(result["output_artifacts"]["source_materials"]["path"]).read_text(encoding="utf-8"))
                self.assertEqual([row["working_audio"]["path"] for row in materials["source_pairs"]],
                                 [str(repaired), str(second)])
                source_alignment = mocks["render"].call_args.args[0]
                with wave.open(str(source_alignment), "rb") as media:
                    self.assertEqual(media.getnframes(), 6 * media.getframerate())
                    self.assertEqual(int.from_bytes(media.readframes(1), "little", signed=True), 17)
                    media.setpos(4 * media.getframerate())
                    self.assertEqual(int.from_bytes(media.readframes(1), "little", signed=True), 1)


if __name__ == "__main__":
    unittest.main()
