# ruff: noqa: E402
import hashlib
import json
import os
import sys
import tempfile
import unittest
import wave
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CURRENT_DIR)
SCRIPTS_PATH = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, SCRIPTS_PATH)

from utils import review_document_runner as runner

from audio_sound.volc_asr import VolcAsrConfig


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_wav(path: Path, *, fill: int = 1, duration: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        sample = int(fill).to_bytes(2, "little", signed=True)
        target.writeframes(sample * int(16000 * duration))


class ReviewDocumentRunnerTests(unittest.TestCase):
    maxDiff = None

    def _audio_inputs(self, root: Path, *, video_suffix: str = ".mp4") -> tuple[Path, Path]:
        video = root / f"source{video_suffix}"
        video.write_bytes(b"source-video")
        audio = root / "source.wav"
        _write_wav(audio)
        snapshot = root / "snapshot.json"
        project = root / "project.json"
        _write_json(
            snapshot,
            {
                "document": {"id": "doc-1", "revision": "r1"},
                "items": [
                    {
                        "id": "spoken-1",
                        "kind": "spoken_delete",
                        "source_text": "00:01 删除“测试”",
                        "start": 0.8,
                        "end": 1.6,
                        "execution_required": True,
                        "evidence": {"delete": "测试", "strategy": "precision_first"},
                    }
                ],
            },
        )
        _write_json(
            project,
            {
                "draft_name": "RunnerDraft",
                "source_video": str(video),
                "source_audio": str(audio),
                "media_duration_seconds": 3.0,
            },
        )
        return snapshot, project

    def _visual_inputs(self, root: Path) -> tuple[Path, Path]:
        video = root / "source.webm"
        video.write_bytes(b"webm-source")
        audio = root / "source.wav"
        _write_wav(audio)
        snapshot = root / "snapshot.json"
        project = root / "project.json"
        _write_json(
            snapshot,
            {
                "document": {"id": "doc-visual", "revision": "r1"},
                "items": [
                    {
                        "id": "animation-1",
                        "kind": "animation_timing",
                        "source_text": "00:01 时序动画仅标签",
                        "start": 1.0,
                        "end": 2.0,
                        "execution_required": False,
                    }
                ],
            },
        )
        _write_json(
            project,
            {
                "draft_name": "VisualDraft",
                "source_video": str(video),
                "source_audio": str(audio),
                "media_duration_seconds": 3.0,
            },
        )
        return snapshot, project

    def _pause_inputs(self, root: Path) -> tuple[Path, Path]:
        video = root / "source.mp4"
        video.write_bytes(b"source-video")
        audio = root / "source.wav"
        _write_wav(audio)
        snapshot = root / "snapshot.json"
        project = root / "project.json"
        _write_json(
            snapshot,
            {
                "document": {"id": "doc-pause", "revision": "r1"},
                "items": [
                    {
                        "id": "pause-1",
                        "kind": "pause_timing_review",
                        "source_text": "00:01 在现有停顿基础上增加 1s",
                        "start": 0.8,
                        "end": 1.6,
                        "execution_required": True,
                    }
                ],
            },
        )
        _write_json(
            project,
            {
                "draft_name": "PauseRunnerDraft",
                "source_video": str(video),
                "source_audio": str(audio),
                "media_duration_seconds": 3.0,
            },
        )
        return snapshot, project

    @staticmethod
    def _fake_execution(request, *, drafts_root, doc_items, **_kwargs):
        draft = Path(drafts_root) / request.project.draft_name
        draft.mkdir(parents=True, exist_ok=True)
        _write_json(draft / "draft_content.json", {"duration": 3_000_000, "tracks": []})
        _write_json(draft / "draft_meta_info.json", {"draft_name": request.project.draft_name})
        receipts = [
            {
                "item_id": item.item_id,
                "source_text": item.source_text,
                "execution_status": item.execution_status,
                "segment_id": f"segment-{index}",
                "material_id": f"material-{index}",
                "track_name": "Review Marker Delete 1",
                "start_time": int((item.start or 0.0) * 1_000_000),
                "duration": 2_000_000,
            }
            for index, item in enumerate(doc_items, start=1)
        ]
        return {
            "draft_name": request.project.draft_name,
            "workflow_mode": "lite",
            "draft_path": str(draft),
            "review_marker_count": len(receipts),
            "review_marker_receipts": receipts,
            "label_only_unresolved_item_ids": [
                item.item_id
                for item in doc_items
                if item.execution_status.casefold().startswith("label_only_unresolved")
            ],
            "validation": {"ok": True, "errors": []},
            "acceptance_validation": {"ok": True, "errors": []},
        }

    @staticmethod
    def _fake_package(draft_dir, output_zip, *, relink_tool=None, **_kwargs):
        draft = Path(draft_dir).resolve()
        output = Path(output_zip).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("RunnerDraft_ZIP/receipt.txt", "validated")
        archive_sha256 = runner.sha256_file(output)
        tree = runner.capture_draft_tree_receipt(draft)
        receipt = output.with_name(f"{output.name}.receipt.json")
        payload = {
            "schema_version": 1,
            "status": "pass",
            "workflow_mode": "lite",
            "delivery_mode": "lite_zip",
            "archive_path": str(output),
            "archive_sha256": archive_sha256,
            "source_draft_path": str(draft),
            "source_tree_sha256": tree["tree_sha256"],
            "package_tree_sha256": "a" * 64,
            "extracted_tree_sha256": "a" * 64,
            "zip_crc_pass": True,
            "zip_tree_identity_pass": True,
            "relink_tool_included": True,
            "relink_tool_sha256": runner.sha256_file(relink_tool),
            "json_rewritten": False,
            "ui_invoked": False,
            "opened_jianying": False,
            "portable_package_invoked": False,
        }
        _write_json(receipt, payload)
        return {**payload, "receipt_path": str(receipt)}

    @staticmethod
    def _fake_asr(audio_path, **_kwargs):
        path = Path(audio_path)
        is_candidate = path.name == "candidate_source_aligned.wav"
        words = (
            [
                {"text": "前", "start": 0.2, "end": 0.6},
                {"text": "后", "start": 1.8, "end": 2.2},
            ]
            if is_candidate
            else [
                {"text": "前", "start": 0.2, "end": 0.6},
                {"text": "测试", "start": 1.0, "end": 1.4},
                {"text": "后", "start": 1.8, "end": 2.2},
            ]
        )
        return {
            "schema_version": 1,
            "provider": "volc_asr",
            "resource_id": "volc.bigasr.auc",
            "adapter_version": "test-adapter-v1",
            "input_sha256": runner.sha256_file(path),
            "service_job_id": "candidate-job" if is_candidate else "source-job",
            "service_result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "words": words,
        }

    @contextmanager
    def _patched_runtime(self):
        config = VolcAsrConfig(api_key="test-key")
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    runner,
                    "ffmpeg_identity",
                    side_effect=lambda binary: {
                        "path": str(binary),
                        "version": "test-tools-v1",
                        "sha256": "f" * 64,
                    },
                )
            )
            stack.enter_context(
                patch.object(
                    runner,
                    "probe_media",
                    return_value=SimpleNamespace(
                        duration_seconds=3.0, has_audio=True, has_video=True
                    ),
                )
            )
            extract = stack.enter_context(
                patch.object(
                    runner,
                    "extract_alignment_wav",
                    side_effect=lambda source, output, **_kwargs: runner.atomic_copy_file(
                        source, output
                    ),
                )
            )
            asr = stack.enter_context(
                patch.object(runner, "run_resumable_volc_asr", side_effect=self._fake_asr)
            )
            stack.enter_context(patch.object(runner, "load_volc_asr_config", return_value=config))
            render = stack.enter_context(
                patch.object(
                    runner,
                    "render_source_aligned_candidate",
                    wraps=runner.render_source_aligned_candidate,
                )
            )
            execute = stack.enter_context(
                patch.object(runner, "execute_revision_request", side_effect=self._fake_execution)
            )
            package = stack.enter_context(
                patch.object(runner, "package_lite_delivery", side_effect=self._fake_package)
            )
            yield {
                "extract": extract,
                "asr": asr,
                "render": render,
                "execute": execute,
                "package": package,
            }

    @staticmethod
    def _run(
        snapshot: Path,
        project: Path,
        *,
        job_root: Path,
        drafts_root: Path,
        package_zip: Path,
        cache_root: Path,
    ):
        return runner.run_review_document(
            snapshot,
            project,
            job_root=job_root,
            drafts_root=drafts_root,
            package_zip=package_zip,
            cache_root=cache_root,
            workflow_mode="lite",
        )

    def test_fixed_dag_caches_source_and_reverse_asr_and_resumes_every_phase(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            job = root / "job"
            with self._patched_runtime() as mocks:
                first = self._run(
                    snapshot,
                    project,
                    job_root=job,
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                )
                second = self._run(
                    snapshot,
                    project,
                    job_root=job,
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                )

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(list(second["phases"]), list(runner._RUN_PHASES))
            self.assertTrue(
                all(row["status"] == "resumed" for row in second["phases"].values())
            )
            self.assertEqual(mocks["asr"].call_count, 2)
            self.assertEqual(mocks["extract"].call_count, 1)
            self.assertEqual(mocks["render"].call_count, 1)
            self.assertEqual(mocks["execute"].call_count, 1)
            self.assertEqual(mocks["package"].call_count, 1)
            self.assertEqual(first["package_zip"], str((root / "delivery.zip").resolve()))
            self.assertIn("revision_request", first["output_artifacts"])
            self.assertEqual(list(job.rglob("*.py")), [])
            processed = json.loads(
                Path(first["output_artifacts"]["doc_items"]["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(processed["review_items"][0]["source_text"], "00:01 删除“测试”")

    def test_pause_change_uses_source_asr_for_verbatim_label_without_media_edit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._pause_inputs(root)
            with self._patched_runtime() as mocks:
                result = self._run(
                    snapshot,
                    project,
                    job_root=root / "job",
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                )

            self.assertTrue(result["ok"])
            self.assertEqual(mocks["asr"].call_count, 1)
            self.assertEqual(mocks["render"].call_count, 0)
            processed = json.loads(
                Path(result["output_artifacts"]["revision_request"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            item = processed["review_items"][0]
            self.assertEqual(item["source_text"], "00:01 在现有停顿基础上增加 1s")
            self.assertFalse(item["execution_required"])
            self.assertTrue(item["execution_status"].startswith("label_only_"))
            self.assertEqual(item["start"], 1.0)
            self.assertEqual(item["evidence"]["asr_alignment"]["resolved_time"], 1.0)
            self.assertEqual(processed.get("pause_adjustments") or [], [])
            self.assertEqual(processed.get("edits") or [], [])
            request = mocks["execute"].call_args.args[0]
            self.assertEqual(request.pause_adjustments, [])
            self.assertEqual(request.edits, [])

    def test_corrupt_source_asr_is_rebuilt_from_cache_without_repeating_provider_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            job = root / "job"
            with self._patched_runtime() as mocks:
                first = self._run(
                    snapshot,
                    project,
                    job_root=job,
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                )
                source_asr = Path(first["output_artifacts"]["source_asr"]["path"])
                source_asr.write_text("corrupt", encoding="utf-8")
                second = self._run(
                    snapshot,
                    project,
                    job_root=job,
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                )

            self.assertTrue(second["ok"])
            self.assertEqual(second["phases"]["source_asr_visual_index"]["status"], "complete")
            self.assertEqual(
                second["phases"]["classified_edit_acceptance_plans"]["status"], "resumed"
            )
            self.assertEqual(mocks["asr"].call_count, 2)
            self.assertEqual(json.loads(source_asr.read_text(encoding="utf-8"))["provider"], "volc_asr")

    def test_corrupt_final_zip_is_not_treated_as_resumable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            job = root / "job"
            with self._patched_runtime() as mocks:
                first = self._run(
                    snapshot,
                    project,
                    job_root=job,
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                )
                Path(first["package_zip"]).write_bytes(b"corrupt zip")
                with self.assertRaises(runner.ReviewDocumentRunError) as raised:
                    self._run(
                        snapshot,
                        project,
                        job_root=job,
                        drafts_root=root / "drafts",
                        package_zip=root / "delivery.zip",
                        cache_root=root / "cache",
                    )

            result = raised.exception.result
            self.assertFalse(result["ok"])
            self.assertEqual(result["phases"]["final_acceptance"]["status"], "failed")
            self.assertIn("corrupt", result["error"].casefold())
            self.assertEqual(mocks["package"].call_count, 1)

    def test_source_audio_byte_change_invalidates_alignment_candidate_and_both_asr_caches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            cache = root / "cache"
            with self._patched_runtime() as mocks:
                first = self._run(
                    snapshot,
                    project,
                    job_root=root / "job-1",
                    drafts_root=root / "drafts-1",
                    package_zip=root / "delivery-1.zip",
                    cache_root=cache,
                )
                project_payload = json.loads(project.read_text(encoding="utf-8"))
                _write_wav(Path(project_payload["source_audio"]), fill=2)
                second = self._run(
                    snapshot,
                    project,
                    job_root=root / "job-2",
                    drafts_root=root / "drafts-2",
                    package_zip=root / "delivery-2.zip",
                    cache_root=cache,
                )

            self.assertTrue(first["ok"] and second["ok"])
            self.assertEqual(mocks["extract"].call_count, 2)
            self.assertEqual(mocks["render"].call_count, 2)
            self.assertEqual(mocks["asr"].call_count, 4)

    def test_webm_is_normalized_once_across_jobs_with_the_same_content_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._visual_inputs(root)
            cache = root / "cache"

            def normalize(source, output, **_kwargs):
                runner.atomic_copy_file(source, output)

            with self._patched_runtime() as mocks, patch.object(
                runner, "_normalize_webm", side_effect=normalize
            ) as normalizer:
                first = self._run(
                    snapshot,
                    project,
                    job_root=root / "job-1",
                    drafts_root=root / "drafts-1",
                    package_zip=root / "delivery-1.zip",
                    cache_root=cache,
                )
                second = self._run(
                    snapshot,
                    project,
                    job_root=root / "job-2",
                    drafts_root=root / "drafts-2",
                    package_zip=root / "delivery-2.zip",
                    cache_root=cache,
                )

            self.assertTrue(first["ok"] and second["ok"])
            self.assertEqual(normalizer.call_count, 1)
            self.assertEqual(mocks["asr"].call_count, 0)
            self.assertEqual(list((root / "job-1").rglob("*.py")), [])
            self.assertEqual(list((root / "job-2").rglob("*.py")), [])

    def test_non_lite_workflow_is_rejected_with_structured_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._visual_inputs(root)
            with self.assertRaises(runner.ReviewDocumentRunError) as raised:
                runner.run_review_document(
                    snapshot,
                    project,
                    job_root=root / "job",
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    workflow_mode="full",
                )
            self.assertFalse(raised.exception.result["ok"])
            self.assertEqual(raised.exception.result["workflow_mode"], "lite")


if __name__ == "__main__":
    unittest.main()
