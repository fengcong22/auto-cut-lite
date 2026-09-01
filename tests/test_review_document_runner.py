# ruff: noqa: E402
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
import zipfile
from contextlib import ExitStack, contextmanager
from copy import deepcopy
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
                "document": {"id": "doc-1", "revision": "r1", "title": "RunnerDraft"},
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
                "document": {
                    "id": "doc-visual",
                    "revision": "r1",
                    "title": "VisualDraft",
                },
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
                "document": {
                    "id": "doc-pause",
                    "revision": "r1",
                    "title": "PauseRunnerDraft",
                },
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
    def _fake_package(
        draft_dir,
        output_zip,
        *,
        relink_tool=None,
        name_resolution=None,
        execution_input_digest="",
        **_kwargs,
    ):
        draft = Path(draft_dir).resolve()
        output = Path(output_zip).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(f"{draft.name}/receipt.txt", "validated")
        archive_sha256 = runner.sha256_file(output)
        tree = runner.capture_draft_tree_receipt(draft)
        receipt = output.with_name(f"{output.name}.receipt.json")
        payload = {
            "schema_version": runner.PACKAGE_SCHEMA_VERSION,
            "status": "pass",
            "workflow_mode": "lite",
            "delivery_mode": "lite_zip",
            "archive_path": str(output),
            "archive_sha256": archive_sha256,
            "source_draft_path": str(draft),
            "source_tree_sha256": tree["tree_sha256"],
            "package_root_name": draft.name,
            "draft_name": draft.name,
            "name_resolution": dict(name_resolution or {}),
            "execution_input_digest": execution_input_digest,
            "package_layout": "draft_root_bundle_v2",
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

        def extract_alignment(source, output, **_kwargs):
            try:
                with wave.open(str(source), "rb"):
                    pass
            except (EOFError, wave.Error):
                _write_wav(Path(output))
            else:
                runner.atomic_copy_file(source, output)

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "utils.runtime_integrity.validate_current_lite_runtime",
                    return_value={"status": "pass", "plugin_version": "test"},
                )
            )
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
                    side_effect=extract_alignment,
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
        execution_input_json: Path | None = None,
    ):
        return runner.run_review_document(
            snapshot,
            project,
            job_root=job_root,
            drafts_root=drafts_root,
            package_zip=package_zip,
            cache_root=cache_root,
            execution_input_json=execution_input_json,
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
            self.assertEqual(
                {name: row["status"] for name, row in second["phases"].items()},
                {
                    "preflight": "complete",
                    "document_fetch": "complete",
                    "asset_download": "complete",
                    "input_compile": "complete",
                    "source_hash": "resumed",
                    "source_asr": "resumed",
                    "classification": "resumed",
                    "reverse_asr": "resumed",
                    "draft_write_validate": "resumed",
                    "package_publish": "resumed",
                },
            )
            self.assertEqual(mocks["asr"].call_count, 2)
            self.assertEqual(mocks["extract"].call_count, 1)
            self.assertEqual(mocks["render"].call_count, 1)
            self.assertEqual(mocks["execute"].call_count, 1)
            self.assertEqual(mocks["package"].call_count, 1)
            self.assertEqual(first["package_zip"], str((root / "RunnerDraft.zip").resolve()))
            self.assertIn("revision_request", first["output_artifacts"])
            self.assertEqual(list(job.rglob("*.py")), [])
            processed = json.loads(
                Path(first["output_artifacts"]["doc_items"]["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(processed["review_items"][0]["source_text"], "00:01 删除“测试”")

    def test_execution_input_is_invocation_scoped_and_does_not_leak_from_sidecar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            execution_input = root / "execution-input.json"
            _write_json(
                execution_input,
                {"schema_version": 1, "artifact_name": "External:Name"},
            )
            job = root / "job"
            with self._patched_runtime():
                first = self._run(
                    snapshot,
                    project,
                    job_root=job,
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery" / "placeholder.zip",
                    cache_root=root / "cache",
                    execution_input_json=execution_input,
                )
                second = self._run(
                    snapshot,
                    project,
                    job_root=job,
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery" / "placeholder.zip",
                    cache_root=root / "cache",
                )

            self.assertEqual(first["name_resolution"]["final_name"], "External_Name")
            self.assertEqual(first["name_resolution"]["source"], "external_input")
            self.assertTrue(first["execution_input_digest"])
            self.assertEqual(second["name_resolution"]["final_name"], "RunnerDraft")
            self.assertEqual(second["name_resolution"]["source"], "document_title")
            self.assertEqual(second["execution_input_digest"], "")
            self.assertFalse((job / "workspace" / "inputs" / "execution-input.json").exists())

            replacement_input = root / "replacement-input.json"
            _write_json(
                replacement_input,
                {"schema_version": 1, "artifact_name": "A New Name"},
            )
            with (
                self._patched_runtime(),
                patch(
                    "utils.runtime_integrity.validate_current_lite_runtime",
                    side_effect=RuntimeError("forced preflight failure"),
                ),
            ):
                with self.assertRaises(runner.ReviewDocumentRunError) as raised:
                    self._run(
                        snapshot,
                        project,
                        job_root=job,
                        drafts_root=root / "drafts",
                        package_zip=root / "delivery" / "placeholder.zip",
                        cache_root=root / "cache",
                        execution_input_json=replacement_input,
                    )
            failure = raised.exception.result
            self.assertEqual(failure["delivery"], {})
            self.assertEqual(failure["name_resolution"], {})
            self.assertNotIn("package_zip", failure["output_artifacts"])
            self.assertNotIn("execution_input", failure["output_artifacts"])

    def test_top_level_document_title_overrides_compat_project_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
            snapshot_payload["title"] = "Top Level Title"
            snapshot_payload["document"].pop("title")
            _write_json(snapshot, snapshot_payload)
            with self._patched_runtime():
                result = self._run(
                    snapshot,
                    project,
                    job_root=root / "job",
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery" / "placeholder.zip",
                    cache_root=root / "cache",
                )

            self.assertEqual(result["name_resolution"]["final_name"], "Top Level Title")
            self.assertEqual(result["name_resolution"]["source"], "document_title")
            self.assertEqual(Path(result["draft_path"]).name, "Top Level Title")
            self.assertEqual(Path(result["package_zip"]).name, "Top Level Title.zip")

    def test_embedded_source_audio_uses_cached_editable_audio_for_a1_a2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            project_payload = json.loads(project.read_text(encoding="utf-8"))
            project_payload["source_audio"] = ""
            _write_json(project, project_payload)
            cache = root / "cache"

            def extract_editable_audio(_source, output, **_kwargs):
                _write_wav(Path(output))

            with (
                self._patched_runtime() as mocks,
                patch.object(
                    runner,
                    "_extract_editable_source_audio",
                    side_effect=extract_editable_audio,
                ) as editable_extractor,
            ):
                first = self._run(
                    snapshot,
                    project,
                    job_root=root / "job-1",
                    drafts_root=root / "drafts-1",
                    package_zip=root / "delivery-1" / "placeholder.zip",
                    cache_root=cache,
                )
                second = self._run(
                    snapshot,
                    project,
                    job_root=root / "job-2",
                    drafts_root=root / "drafts-2",
                    package_zip=root / "delivery-2" / "placeholder.zip",
                    cache_root=cache,
                )

            self.assertTrue(first["ok"] and second["ok"], (first, second))
            editable_audio = (
                root / "job-1" / "workspace" / "materials" / "source_audio.m4a"
            ).resolve()
            alignment_wav = (
                root / "job-1" / "workspace" / "materials" / "source_alignment.wav"
            ).resolve()
            processed_request = json.loads(
                Path(first["output_artifacts"]["revision_request"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                Path(processed_request["project"]["source_audio"]).resolve(),
                editable_audio,
            )
            self.assertEqual(
                {
                    Path(segment["asset_path"]).resolve()
                    for segment in processed_request["audio_delivery_plan"]["segments"]
                },
                {editable_audio},
            )
            self.assertNotEqual(editable_audio, alignment_wav)
            with wave.open(str(editable_audio), "rb") as source:
                self.assertGreater(source.getnframes(), 0)
            with wave.open(str(alignment_wav), "rb") as source:
                self.assertGreater(source.getnframes(), 0)
            self.assertEqual(editable_extractor.call_count, 1)
            self.assertEqual(mocks["extract"].call_count, 1)

    def test_editable_source_audio_prefers_stream_copy_then_lossless_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.mp4"
            output = root / "source.m4a"
            source.write_bytes(b"source")

            for fail_copy, expected_codecs in (
                (False, ["copy"]),
                (True, ["copy", "alac"]),
            ):
                with self.subTest(fail_copy=fail_copy):
                    commands = []

                    def run(command, **_kwargs):
                        commands.append(list(command))
                        codec = command[command.index("-c:a") + 1]
                        if codec == "copy" and fail_copy:
                            return subprocess.CompletedProcess(
                                command,
                                1,
                                "",
                                "copy is not supported by the target container",
                            )
                        Path(command[-1]).write_bytes(b"audio-only")
                        return subprocess.CompletedProcess(command, 0, "", "")

                    with patch.object(runner.subprocess, "run", side_effect=run):
                        runner._extract_editable_source_audio(
                            source,
                            output,
                            ffmpeg_bin="ffmpeg",
                        )

                    self.assertEqual(
                        [command[command.index("-c:a") + 1] for command in commands],
                        expected_codecs,
                    )
                    self.assertTrue(output.is_file())
                    output.unlink()

    def test_reverse_asr_retry_downgrades_only_failed_item_and_revalidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
            snapshot_payload["items"].append(
                {
                    "id": "spoken-2",
                    "kind": "spoken_delete",
                    "source_text": "00:02 删除“第二”",
                    "start": 1.7,
                    "end": 2.5,
                    "execution_required": True,
                    "evidence": {"delete": "第二", "strategy": "precision_first"},
                }
            )
            _write_json(snapshot, snapshot_payload)

            def two_item_asr(audio_path, **_kwargs):
                path = Path(audio_path)
                is_candidate = path.name == "candidate_source_aligned.wav"
                words = (
                    [
                        {"text": "前", "start": 0.2, "end": 0.5},
                        {"text": "后", "start": 2.5, "end": 2.8},
                    ]
                    if is_candidate
                    else [
                        {"text": "前", "start": 0.2, "end": 0.5},
                        {"text": "测试", "start": 1.0, "end": 1.3},
                        {"text": "第二", "start": 2.0, "end": 2.3},
                        {"text": "后", "start": 2.5, "end": 2.8},
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

            original_builder = runner.build_full_candidate_reverse_report
            report_calls = 0

            def controlled_report(*args, **kwargs):
                nonlocal report_calls
                report_calls += 1
                report = original_builder(*args, **kwargs)
                for row in report["rows"]:
                    row["status"] = "pass"
                    row["delete_hits"] = []
                    row["keep_hits"] = {phrase: True for phrase in row.get("must_keep") or []}
                    row["semantic_join_validation"] = {
                        "status": "pass",
                        "method": "test",
                        "forbidden_patterns": [],
                    }
                if report_calls == 1:
                    report["unresolved_ids"] = ["spoken-1"]
                    report["rows"][0]["status"] = "review"
                else:
                    report["unresolved_ids"] = []
                report["status_counts"] = {
                    "pass": sum(row["status"] == "pass" for row in report["rows"]),
                    "review": sum(row["status"] != "pass" for row in report["rows"]),
                }
                return report

            with self._patched_runtime() as mocks:
                with (
                    patch.object(
                        runner,
                        "run_resumable_volc_asr",
                        side_effect=two_item_asr,
                    ),
                    patch.object(
                        runner,
                        "build_full_candidate_reverse_report",
                        side_effect=controlled_report,
                    ),
                ):
                    result = self._run(
                        snapshot,
                        project,
                        job_root=root / "job",
                        drafts_root=root / "drafts",
                        package_zip=root / "delivery.zip",
                        cache_root=root / "cache",
                    )

            self.assertTrue(result["ok"], result)
            self.assertEqual(report_calls, 2)
            self.assertEqual(mocks["render"].call_count, 2)
            cut_plan = json.loads(
                Path(result["output_artifacts"]["audio_cut_plan"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            rows = {row["item_id"]: row for row in cut_plan["rows"]}
            self.assertFalse(rows["spoken-1"]["execution_required"])
            self.assertEqual(rows["spoken-1"]["execution_status"], "label_only_unresolved")
            self.assertFalse(rows["spoken-1"]["asr_alignment"]["authoritative_cut_boundary"])
            self.assertTrue(rows["spoken-2"]["execution_required"])
            self.assertEqual([row["item_id"] for row in cut_plan["executable_cuts"]], ["spoken-2"])
            processed_request = json.loads(
                Path(result["output_artifacts"]["revision_request"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                [edit["doc_item_id"] for edit in processed_request["edits"]], ["spoken-2"]
            )
            summary = json.loads(
                Path(result["output_artifacts"]["processed_media_evidence"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["reverse_asr_status"], "pass")
            self.assertEqual(summary["reverse_asr_attempt_count"], 2)
            self.assertEqual(summary["reverse_asr_downgraded_item_ids"], ["spoken-1"])
            self.assertIn("spoken-1", result["unresolved_item_ids"])
            self.assertEqual(
                summary["execution_summary"]["actual_audio_cut_item_ids"],
                ["spoken-2"],
            )
            self.assertEqual(
                summary["execution_summary"]["label_only_unresolved_item_ids"],
                ["spoken-1"],
            )
            self.assertFalse(summary["execution_summary"]["all_requested_audio_deletions_executed"])
            self.assertEqual(
                result["execution_summary"]["status"],
                "complete_with_label_only_unresolved",
            )
            self.assertEqual(
                result["acceptance_scope"],
                "draft_structure_and_package_delivery",
            )
            final_acceptance = json.loads(
                Path(result["output_artifacts"]["final_acceptance"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(final_acceptance["status"], "pass")
            self.assertEqual(
                final_acceptance["execution_summary"]["status"],
                "complete_with_label_only_unresolved",
            )
            self.assertEqual(
                final_acceptance["execution_summary"]["unexecuted_audio_deletion_item_ids"],
                ["spoken-1"],
            )
            self.assertTrue(
                (
                    root / "job" / "workspace" / "processed" / "reverse_asr_initial_report.json"
                ).is_file()
            )

    def test_lark_block_ids_remain_unique_in_internal_marker_receipts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            block_ids = [
                "doxcnMGTlYvs75YGJ1Lm8iuo09c",
                "doxcnTJeXRP93bxcYBBCgaemAkh",
            ]
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            payload["items"] = [
                {
                    "id": block_ids[0],
                    "kind": "animation_timing",
                    "source_text": "00:01 第一条仅标签",
                    "start": 1.0,
                    "end": 1.2,
                    "execution_required": False,
                },
                {
                    "id": block_ids[1],
                    "kind": "animation_timing",
                    "source_text": "00:02 第二条仅标签",
                    "start": 2.0,
                    "end": 2.2,
                    "execution_required": False,
                },
            ]
            _write_json(snapshot, payload)

            with self._patched_runtime():
                result = self._run(
                    snapshot,
                    project,
                    job_root=root / "job",
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                )

            self.assertTrue(result["ok"])
            self.assertEqual(
                {name: row["status"] for name, row in result["phases"].items()},
                {name: "complete" for name in runner._RUN_PHASES},
            )
            execution = json.loads(
                Path(result["output_artifacts"]["revision_result"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            receipt_ids = [row["item_id"] for row in execution["review_marker_receipts"]]
            self.assertEqual(receipt_ids, block_ids)
            self.assertEqual(len(set(receipt_ids)), len(block_ids))
            runner._validate_marker_receipts(
                execution,
                json.loads(
                    Path(result["output_artifacts"]["doc_items"]["path"]).read_text(
                        encoding="utf-8"
                    )
                ),
            )

    def test_visual_compiler_only_touches_visual_items_in_large_mixed_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asset = root / "overlay.png"
            asset.write_bytes(b"png")
            audio_items = [
                {
                    "id": f"audio-{index:02d}",
                    "kind": "spoken_delete",
                    "source_text": f"00:01 删除词语 {index}",
                    "start": 1.0 + index / 100.0,
                    "end": 1.2 + index / 100.0,
                    "execution_required": True,
                    "execution_status": "asr_resolved",
                    "evidence": {
                        "delete": f"词语 {index}",
                        "asr_alignment": {
                            "status": "pass",
                            "authoritative_timing": True,
                            "authoritative_cut_boundary": True,
                            "resolved_time": 1.0 + index / 100.0,
                        },
                    },
                }
                for index in range(42)
            ]
            # An audio row may carry an attachment/visual plan for diagnosis;
            # it must remain owned by the ASR/audio compiler.
            audio_items[0]["visual_plan"] = {
                "segments": [{"asset_path": str(asset), "duration": 0.2}]
            }
            visual_items = [
                {
                    "id": "visual-with-asset",
                    "kind": "visual_overlay",
                    "source_text": "00:02 添加给定图片",
                    "start": 2.0,
                    "end": 3.0,
                    "execution_required": True,
                    "evidence": {"asset_path": str(asset)},
                },
                {
                    "id": "visual-with-empty-plan",
                    "kind": "visual_overlay",
                    "source_text": "00:03 缺少图片时仅保留标签",
                    "start": 3.0,
                    "end": 4.0,
                    "execution_required": True,
                    "evidence": {"visual_plan": {"reuse_audio": False}},
                },
            ]
            request = {
                "review_items": deepcopy(audio_items + visual_items),
                "edits": [
                    {
                        "type": "delete",
                        "doc_item_id": item["id"],
                        "start": item["start"],
                        "end": item["end"],
                    }
                    for item in audio_items
                ],
                "audio_delivery_plan": {
                    "mode": "segmented",
                    "segments": [{"id": f"segment-{index:02d}"} for index in range(42)],
                },
            }
            ledger = {"review_items": deepcopy(audio_items + visual_items)}
            original_request_audio = deepcopy(request["review_items"][:42])
            original_ledger_audio = deepcopy(ledger["review_items"][:42])
            original_audio_plan = deepcopy(request["audio_delivery_plan"])

            runner._compile_explicit_lite_visuals(request, ledger)

            self.assertEqual(request["review_items"][:42], original_request_audio)
            self.assertEqual(ledger["review_items"][:42], original_ledger_audio)
            self.assertEqual(request["audio_delivery_plan"], original_audio_plan)
            self.assertEqual(
                [edit["doc_item_id"] for edit in request["edits"] if edit["type"] == "add_overlay"],
                ["visual-with-asset"],
            )
            request_empty = request["review_items"][43]
            ledger_empty = ledger["review_items"][43]
            self.assertFalse(request_empty["execution_required"])
            self.assertEqual(request_empty["execution_status"], "label_only_unresolved")
            self.assertEqual(ledger_empty["execution_status"], "label_only_unresolved")

    def test_stale_pointer_cleanup_receipt_with_asset_remains_label_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asset = root / "clean-cover.png"
            asset.write_bytes(b"png")
            item = {
                "id": "pointer-cleanup",
                "kind": "pointer_overlay",
                "source_text": "00:02 清理原小手遮挡",
                "start": 2.0,
                "end": 3.0,
                # Simulate a stale v1 classification that incorrectly marked
                # a cleanup request executable.
                "execution_required": True,
                "evidence": {"asset_path": str(asset)},
            }
            request = {"review_items": [deepcopy(item)], "edits": []}
            ledger = {"review_items": [deepcopy(item)]}

            runner._compile_explicit_lite_visuals(request, ledger)

            self.assertEqual(request["edits"], [])
            self.assertFalse(request["review_items"][0]["execution_required"])
            self.assertEqual(
                request["review_items"][0].get("execution_status"),
                "label_only_unresolved",
            )
            self.assertFalse(ledger["review_items"][0]["execution_required"])
            self.assertEqual(
                ledger["review_items"][0].get("execution_status"),
                "label_only_unresolved",
            )

    def test_pointer_removal_with_asset_does_not_compile_overlay_but_readd_does(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            asset = Path(tmpdir) / "hand.png"
            asset.write_bytes(b"png")

            for index, source_text in enumerate(
                (
                    "00:02 删除屏幕上的小手",
                    "00:03 删除小手",
                    "00:04 去除画面中的小手",
                    "00:05 把小手拿掉",
                    "00:06 这里不要小手",
                    "00:07 删除小手添加的动画",
                    "00:07 删除小手，再添加动画",
                ),
                start=1,
            ):
                with self.subTest(source_text=source_text):
                    item = {
                        "id": f"pointer-remove-{index}",
                        "kind": "pointer_overlay",
                        "source_text": source_text,
                        "start": float(index + 1),
                        "end": float(index + 2),
                        "execution_required": True,
                        "evidence": {"asset_path": str(asset)},
                    }
                    request = {"review_items": [deepcopy(item)], "edits": []}
                    ledger = {"review_items": [deepcopy(item)]}

                    runner._compile_explicit_lite_visuals(request, ledger)

                    self.assertEqual(request["edits"], [])
                    self.assertFalse(request["review_items"][0]["execution_required"])
                    self.assertEqual(
                        request["review_items"][0]["execution_status"],
                        "label_only_unresolved",
                    )
                    self.assertFalse(ledger["review_items"][0]["execution_required"])

            for index, source_text in enumerate(
                (
                    "00:08 删除原小手并重新添加",
                    "00:09 小手移除后再加一个",
                    "00:10 移除后再加一个",
                ),
                start=1,
            ):
                with self.subTest(source_text=source_text):
                    readd_item = {
                        "id": f"pointer-readd-{index}",
                        "kind": "pointer_overlay",
                        "source_text": source_text,
                        "start": float(index + 7),
                        "end": float(index + 8),
                        "execution_required": True,
                        "evidence": {"asset_path": str(asset)},
                    }
                    request = {"review_items": [deepcopy(readd_item)], "edits": []}
                    ledger = {"review_items": [deepcopy(readd_item)]}

                    runner._compile_explicit_lite_visuals(request, ledger)

                    self.assertEqual(
                        [edit["type"] for edit in request["edits"]],
                        ["add_overlay"],
                    )

    def test_unknown_visual_kind_with_local_plan_is_label_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            asset = root / "unknown.png"
            asset.write_bytes(b"png")
            item = {
                "id": "unknown-visual-kind",
                "kind": "visual_new_effect",
                "source_text": "00:04 新增未维护视觉效果",
                "start": 4.0,
                "end": 5.0,
                "execution_required": True,
                "visual_plan": {
                    "segments": [{"asset_path": str(asset), "duration": 1.0}],
                },
            }
            request = {"review_items": [deepcopy(item)], "edits": []}
            ledger = {"review_items": [deepcopy(item)]}

            runner._compile_explicit_lite_visuals(request, ledger)

            self.assertEqual(request["edits"], [])
            self.assertFalse(request["review_items"][0]["execution_required"])
            self.assertEqual(
                request["review_items"][0]["execution_status"],
                "label_only_unresolved",
            )
            self.assertFalse(ledger["review_items"][0]["execution_required"])
            self.assertEqual(
                ledger["review_items"][0]["execution_status"],
                "label_only_unresolved",
            )

    def test_unknown_visual_kind_without_asset_is_label_only(self):
        item = {
            "id": "unknown-visual-no-asset",
            "kind": "visual_new_effect",
            "source_text": "00:05 新增未维护视觉效果",
            "start": 5.0,
            "end": 6.0,
            "execution_required": True,
        }
        request = {"review_items": [deepcopy(item)], "edits": []}
        ledger = {"review_items": [deepcopy(item)]}

        runner._compile_explicit_lite_visuals(request, ledger)

        self.assertEqual(request["edits"], [])
        self.assertFalse(request["review_items"][0]["execution_required"])
        self.assertEqual(
            request["review_items"][0]["execution_status"],
            "label_only_unresolved",
        )
        self.assertFalse(ledger["review_items"][0]["execution_required"])
        self.assertEqual(
            ledger["review_items"][0]["execution_status"],
            "label_only_unresolved",
        )

    def test_visual_asset_failures_are_structured_without_leaking_references(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first.png"
            second = root / "second.png"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            cases = (
                (
                    {
                        "id": "download-failed",
                        "kind": "visual_overlay",
                        "source_text": "00:01 添加附件",
                        "start": 1.0,
                        "execution_required": True,
                        "evidence": {
                            "asset_url": "https://secret.example/path?token=private-token"
                        },
                    },
                    "visual_asset_download_failed",
                ),
                (
                    {
                        "id": "asset-ambiguous",
                        "kind": "visual_overlay",
                        "source_text": "00:02 添加附件",
                        "start": 2.0,
                        "execution_required": True,
                        "evidence": {"asset_paths": [str(first), str(second)]},
                    },
                    "visual_asset_ambiguous",
                ),
            )
            for item, expected_code in cases:
                with self.subTest(expected_code=expected_code):
                    request = {"review_items": [deepcopy(item)], "edits": []}
                    ledger = {"review_items": [deepcopy(item)]}
                    with self.assertRaises(runner.LiteVisualAssetError) as raised:
                        runner._compile_explicit_lite_visuals(request, ledger)
                    self.assertEqual(raised.exception.details["code"], expected_code)
                    self.assertEqual(raised.exception.details["status"], "user_action_required")
                    self.assertEqual(raised.exception.details["item_ids"], [item["id"]])
                    serialized = json.dumps(raised.exception.details, ensure_ascii=False)
                    self.assertNotIn("secret.example", serialized)
                    self.assertNotIn("private-token", serialized)

    def test_v1_runner_receipts_rerun_downstream_while_source_asr_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            job = root / "job"
            original_reverse_apply = runner.apply_reverse_report_to_payloads
            with (
                self._patched_runtime() as mocks,
                patch.object(
                    runner,
                    "apply_reverse_report_to_payloads",
                    wraps=original_reverse_apply,
                ) as reverse_apply,
            ):
                with (
                    patch.object(runner, "RUNNER_VERSION", "auto-cut-lite-review-document-run-v1"),
                    patch.object(runner, "_SCHEMA_VERSION", 1),
                ):
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

            self.assertTrue(first["ok"] and second["ok"])
            self.assertEqual(first["runner_version"], "auto-cut-lite-review-document-run-v1")
            self.assertEqual(second["runner_version"], runner.RUNNER_VERSION)
            self.assertTrue(all(row["status"] == "complete" for row in second["phases"].values()))
            timing = json.loads((job / "job_timing.json").read_text(encoding="utf-8"))
            self.assertTrue(timing["phases"]["source_asr"]["cache_hit"])
            self.assertEqual(mocks["asr"].call_count, 2)
            self.assertEqual(mocks["extract"].call_count, 1)
            self.assertEqual(mocks["render"].call_count, 1)
            self.assertEqual(mocks["execute"].call_count, 2)
            self.assertEqual(reverse_apply.call_count, 2)
            receipt = json.loads((job / "reverse_asr.receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], 2)

    def test_lite_pause_language_and_nested_status_compile_strictly_label_only(self):
        pause_rows = [
            ("pause-add", "00:01 停顿增加 1 秒"),
            ("pause-extend", "00:02 把停顿延长到 2 秒"),
            ("pause-shorten", "00:03 把停顿缩短 1 秒"),
            ("pause-delete", "00:04 删除这段停顿"),
            ("pause-delete-after", "00:04 这段停顿删除"),
            ("pause-plus", "00:05 停顿 +1s"),
            ("pause-minus", "00:06 停顿 -1s"),
            ("pause-duration", "00:07 这里停顿 1 秒"),
        ]
        rows = [
            {
                "id": item_id,
                "source_text": source_text,
                "execution_required": True,
            }
            for item_id, source_text in pause_rows
        ]
        rows.extend(
            [
                {
                    "id": "explicit-gap-delete",
                    "kind": "gap_delete",
                    "source_text": "00:08 删除中间空白",
                    "execution_required": True,
                },
                {
                    "id": "explicit-semantic",
                    "kind": "semantic_pause_adjustment",
                    "source_text": "00:09 调整语义停顿",
                    "execution_required": True,
                },
                {
                    "id": "nested-label-only",
                    "kind": "spoken_delete",
                    "source_text": "00:10 删除“测试”",
                    "execution_required": True,
                    "execution_status": "pending",
                    "validation": {"layers": [{"status": "LABEL-ONLY-UNRESOLVED"}]},
                },
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "compiled"
            compiled = runner.compile_review_job(
                {"items": rows},
                {
                    "draft_name": "PauseMatrix",
                    "source_video": "C:/media/source.mp4",
                    "workflow_mode": "lite",
                    "lite_cut_layout": "split_gap",
                },
                output,
            )
            ledger = json.loads(Path(compiled["doc_items"]).read_text(encoding="utf-8"))
            request = json.loads(Path(compiled["revision_request"]).read_text(encoding="utf-8"))

        by_id = {item["id"]: item for item in ledger["review_items"]}
        for item_id in by_id:
            with self.subTest(item_id=item_id):
                self.assertFalse(by_id[item_id]["execution_required"])
                self.assertTrue(by_id[item_id]["execution_status"].startswith("label_only_"))
                self.assertEqual(by_id[item_id]["evidence"]["timing_source"], "asr")
        self.assertEqual(request.get("pause_adjustments") or [], [])
        self.assertEqual(request.get("edits") or [], [])

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
            self.assertEqual(item["execution_status"], "label_only_lite_policy")
            self.assertEqual(item["start"], 1.0)
            self.assertEqual(item["evidence"]["resolved_time"], 1.0)
            self.assertEqual(item["evidence"]["timing_source"], "review_timestamp_fallback")
            self.assertNotIn("asr_alignment", item["evidence"])
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
            self.assertEqual(second["phases"]["source_asr"]["status"], "complete")
            self.assertEqual(second["phases"]["classification"]["status"], "resumed")
            self.assertEqual(mocks["asr"].call_count, 2)
            self.assertEqual(
                json.loads(source_asr.read_text(encoding="utf-8"))["provider"], "volc_asr"
            )

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
            self.assertEqual(result["phases"]["package_publish"]["status"], "failed")
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
                    package_zip=root / "delivery-1" / "placeholder.zip",
                    cache_root=cache,
                )
                project_payload = json.loads(project.read_text(encoding="utf-8"))
                _write_wav(Path(project_payload["source_audio"]), fill=2)
                second = self._run(
                    snapshot,
                    project,
                    job_root=root / "job-2",
                    drafts_root=root / "drafts-2",
                    package_zip=root / "delivery-2" / "placeholder.zip",
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

            with (
                self._patched_runtime() as mocks,
                patch.object(runner, "_normalize_webm", side_effect=normalize) as normalizer,
            ):
                first = self._run(
                    snapshot,
                    project,
                    job_root=root / "job-1",
                    drafts_root=root / "drafts-1",
                    package_zip=root / "delivery-1" / "placeholder.zip",
                    cache_root=cache,
                )
                second = self._run(
                    snapshot,
                    project,
                    job_root=root / "job-2",
                    drafts_root=root / "drafts-2",
                    package_zip=root / "delivery-2" / "placeholder.zip",
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

    def test_url_mode_runs_ten_phases_resumes_and_invalidates_by_revision(self):
        class LarkRunner:
            def __init__(self) -> None:
                self.revision = 1
                self.unknown_text = "00:02 新增从未支持过的镜头旋转效果"
                self.commands: list[list[str]] = []

            def __call__(self, command):
                row = [str(value) for value in command]
                self.commands.append(row)
                if row[-1:] == ["--version"]:
                    return subprocess.CompletedProcess(row, 0, "lark-cli version 1.2.3\n", "")
                if row[1:] == ["whoami"]:
                    payload = {
                        "available": True,
                        "defaultAs": "user",
                        "identity": "user",
                        "profile": "operator",
                        "tokenStatus": "valid",
                        "onBehalfOf": {"openId": "ou_private", "userName": "reviewer"},
                    }
                    return subprocess.CompletedProcess(row, 0, json.dumps(payload), "")
                if row[1:3] == ["docs", "+fetch"]:
                    content = "".join(
                        (
                            '<source token="source_private_token" name="课程源视频.mp4" '
                            'mime="video/mp4"/>',
                            '<checkbox id="delete">00:01 删除“测试”</checkbox>',
                            f'<checkbox id="unknown">{self.unknown_text}</checkbox>',
                        )
                    )
                    payload = {
                        "ok": True,
                        "identity": "user",
                        "data": {
                            "document": {
                                "document_id": "document_private_token",
                                "revision_id": self.revision,
                                "content": content,
                            }
                        },
                    }
                    return subprocess.CompletedProcess(row, 0, json.dumps(payload), "")
                if row[1:3] == ["docs", "+media-download"]:
                    target = Path(row[row.index("--output") + 1])
                    _write_wav(target)
                    return subprocess.CompletedProcess(row, 0, json.dumps({"ok": True}), "")
                return subprocess.CompletedProcess(row, 1, "", "unsupported")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            lark_exe = root / "lark-cli.exe"
            lark_exe.write_bytes(b"not executed")
            lark = LarkRunner()
            job = root / "job"
            url = "https://example.feishu.cn/docx/url_private_token"
            progress: list[dict] = []
            kwargs = {
                "doc_url": url,
                "job_root": job,
                "drafts_root": root / "drafts",
                "package_zip": root / "delivery.zip",
                "cache_root": root / "cache",
                "lark_cli": lark_exe,
                "lark_runner": lark,
                "readiness_path": root / "runtime-readiness.json",
                "progress": lambda event: progress.append(dict(event)),
            }
            with self._patched_runtime() as mocks:
                first = runner.run_review_document(**kwargs)
                second = runner.run_review_document(**kwargs)
                lark.revision = 2
                lark.unknown_text = "00:02 新增另一种从未支持过的镜头翻转效果"
                third = runner.run_review_document(**kwargs)

            self.assertTrue(first["ok"] and second["ok"] and third["ok"])
            self.assertEqual(list(first["phases"]), list(runner._RUN_PHASES))
            self.assertEqual(
                set(json.loads((job / "job_timing.json").read_text(encoding="utf-8"))["phases"]),
                set(runner._RUN_PHASES),
            )
            self.assertEqual(second["phases"]["source_hash"]["status"], "resumed")
            self.assertEqual(second["phases"]["classification"]["status"], "resumed")
            self.assertEqual(third["phases"]["source_hash"]["status"], "complete")
            self.assertEqual(third["phases"]["classification"]["status"], "complete")
            self.assertEqual(mocks["asr"].call_count, 2)
            self.assertTrue(any(event.get("phase") == "document_fetch" for event in progress))

            processed = json.loads(
                Path(third["output_artifacts"]["revision_request"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            unknown = next(
                item for item in processed["review_items"] if "翻转效果" in item["source_text"]
            )
            self.assertEqual(unknown["kind"], "review_only")
            self.assertFalse(unknown["execution_required"])

            serialized = ""
            for path in job.rglob("*"):
                if path.is_file():
                    serialized += path.read_text(encoding="utf-8", errors="ignore")
            for secret in (
                url,
                "url_private_token",
                "source_private_token",
                "document_private_token",
                "ou_private",
                "reviewer",
            ):
                self.assertNotIn(secret, serialized)

    def test_url_mode_creates_state_and_timing_before_preflight_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            lark_exe = root / "lark-cli.exe"
            lark_exe.write_bytes(b"not executed")

            def failing(command):
                return subprocess.CompletedProcess(command, 9, "", "private failure")

            job = root / "job"
            with self._patched_runtime(), self.assertRaises(runner.ReviewDocumentRunError):
                runner.run_review_document(
                    doc_url="https://example.feishu.cn/wiki/failure_private_token",
                    job_root=job,
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                    lark_cli=lark_exe,
                    lark_runner=failing,
                    readiness_path=root / "runtime-readiness.json",
                )
            self.assertTrue((job / "job_state.json").is_file())
            self.assertTrue((job / "job_timing.json").is_file())
            state = (job / "job_state.json").read_text(encoding="utf-8")
            self.assertNotIn("failure_private_token", state)

    def test_phase_receipt_and_result_redact_provider_secrets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secret_url = "https://provider.example/private?token=receipt-private-token"
            outcome = runner._phase_outcome(
                root,
                "preflight",
                artifacts=[],
                data={
                    "authorization_url": secret_url,
                    "nested": {"message": f"token=receipt_private_token at {secret_url}"},
                },
                result={"provider_message": f"request failed at {secret_url}"},
            )

            receipt_text = (root / "preflight.receipt.json").read_text(encoding="utf-8")
            result_text = json.dumps(outcome.result, ensure_ascii=False)
            for secret in (secret_url, "receipt-private-token", "receipt_private_token"):
                self.assertNotIn(secret, receipt_text)
                self.assertNotIn(secret, result_text)

    def test_whoami_failure_invalidates_old_lark_readiness_and_redacts_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            readiness = root / "runtime-readiness.json"
            runner.evaluate_runtime_readiness(
                path=readiness,
                runtime_version=runner.RUNNER_VERSION,
                lark_version="1.2.3",
                asr_adapter_version=runner.VOLC_ASR_ADAPTER_VERSION,
            )
            runner.mark_lark_verified(
                {"identity": "user", "open_id": "ou_previous_private"},
                path=readiness,
                runtime_version=runner.RUNNER_VERSION,
                lark_version="1.2.3",
                asr_adapter_version=runner.VOLC_ASR_ADAPTER_VERSION,
            )
            self.assertEqual(
                json.loads(readiness.read_text(encoding="utf-8"))["lark"]["status"],
                "verified",
            )
            job = root / "job"
            provider_url = "https://provider.example/whoami/private_token"
            with (
                self._patched_runtime(),
                patch.object(runner, "lark_cli_version", return_value="1.2.3"),
                patch.object(
                    runner,
                    "lark_whoami",
                    side_effect=RuntimeError(
                        f"identity failed at {provider_url}; token=whoami_private_token"
                    ),
                ),
                self.assertRaises(runner.ReviewDocumentRunError) as raised,
            ):
                runner.run_review_document(
                    doc_url="https://example.feishu.cn/docx/document_private_token",
                    job_root=job,
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                    readiness_path=readiness,
                )

            readiness_payload = json.loads(readiness.read_text(encoding="utf-8"))
            self.assertEqual(readiness_payload["lark"]["status"], "pending_validation")
            self.assertEqual(
                readiness_payload["lark"]["reason_code"],
                "lark_user_identity_unavailable",
            )
            self.assertEqual(
                raised.exception.result["failure_details"]["preflight"]["code"],
                "lark_user_identity_unavailable",
            )
            serialized = json.dumps(raised.exception.result, ensure_ascii=False)
            serialized += (job / "job_state.json").read_text(encoding="utf-8")
            serialized += (job / "job_timing.json").read_text(encoding="utf-8")
            for secret in (provider_url, "whoami_private_token", "document_private_token"):
                self.assertNotIn(secret, serialized)

    def test_source_asr_failure_downgrades_precise_delete_to_verbatim_comment_time_label(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot, project = self._audio_inputs(root)
            job = root / "job"
            provider_url = "https://asr.example/jobs/asr_private_token"
            with self._patched_runtime() as mocks:
                mocks["asr"].side_effect = RuntimeError(
                    f"provider unavailable at {provider_url}; token=asr_private_token"
                )
                result = self._run(
                    snapshot,
                    project,
                    job_root=job,
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["phases"]["source_asr"]["status"], "complete")
            self.assertEqual(mocks["asr"].call_count, 1)
            self.assertEqual(mocks["render"].call_count, 0)
            processed = json.loads(
                Path(result["output_artifacts"]["revision_request"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            item = processed["review_items"][0]
            self.assertEqual(item["source_text"], "00:01 删除“测试”")
            self.assertFalse(item["execution_required"])
            self.assertEqual(item["execution_status"], "label_only_unresolved")
            self.assertEqual(item["start"], 1.0)
            self.assertEqual(item["evidence"]["timing_source"], "review_timestamp_fallback")
            self.assertEqual(processed.get("edits") or [], [])
            expected_source_audio = Path(
                json.loads(project.read_text(encoding="utf-8"))["source_audio"]
            ).resolve()
            self.assertEqual(
                Path(processed["project"]["source_audio"]).resolve(),
                expected_source_audio,
            )
            self.assertEqual(processed["audio_delivery_plan"]["mode"], "legacy")
            source_index = json.loads(
                Path(result["output_artifacts"]["source_asr_index"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(source_index["asr_available"])
            self.assertEqual(source_index["fallback_policy"], "review_comment_time_label_only")
            serialized = json.dumps(result, ensure_ascii=False)
            for path in job.rglob("*"):
                if path.is_file():
                    serialized += path.read_text(encoding="utf-8", errors="ignore")
            for secret in (provider_url, "asr_private_token"):
                self.assertNotIn(secret, serialized)

    def test_url_asset_and_compile_errors_keep_sanitized_public_contract(self):
        parsed = {
            "document_identity_sha256": "d" * 64,
            "revision_id": "1",
            "content_sha256": "c" * 64,
            "asset_identity_sha256": "a" * 64,
        }
        cases = ("asset_download", "input_compile")
        for failed_phase in cases:
            with self.subTest(failed_phase=failed_phase), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                job = root / "job"
                intake_error = runner.ReviewDocumentIntakeError(
                    "visual_asset_ambiguous",
                    "A review item has multiple possible local visual materials",
                    user_action={
                        "action_code": "high_risk_confirmation",
                        "reason_code": "visual_asset_ambiguous",
                        "item_ids": ["visual-1"],
                    },
                    details={
                        "asset_token": "asset_private_token",
                        "provider_message": (
                            "download failed at https://provider.example/private-token"
                        ),
                    },
                )
                with ExitStack() as stack:
                    stack.enter_context(self._patched_runtime())
                    stack.enter_context(
                        patch.object(runner, "lark_cli_version", return_value="1.2.3")
                    )
                    stack.enter_context(
                        patch.object(
                            runner,
                            "lark_whoami",
                            return_value={"available": True, "identity": "user"},
                        )
                    )
                    stack.enter_context(
                        patch.object(runner, "fetch_lark_document", return_value={})
                    )
                    stack.enter_context(
                        patch.object(runner, "parse_lark_document", return_value=parsed)
                    )
                    if failed_phase == "asset_download":
                        stack.enter_context(
                            patch.object(
                                runner,
                                "download_lark_assets",
                                side_effect=intake_error,
                            )
                        )
                    else:
                        stack.enter_context(
                            patch.object(runner, "download_lark_assets", return_value=[])
                        )
                        stack.enter_context(
                            patch.object(runner, "compile_url_inputs", side_effect=intake_error)
                        )
                    with self.assertRaises(runner.ReviewDocumentRunError) as raised:
                        runner.run_review_document(
                            doc_url="https://example.feishu.cn/wiki/document_private_token",
                            job_root=job,
                            drafts_root=root / "drafts",
                            package_zip=root / "delivery.zip",
                            cache_root=root / "cache",
                            readiness_path=root / "runtime-readiness.json",
                        )

                detail = raised.exception.result["failure_details"][failed_phase]
                self.assertEqual(detail["code"], "visual_asset_ambiguous")
                self.assertEqual(
                    detail["user_action_required"]["action_code"],
                    "high_risk_confirmation",
                )
                serialized = json.dumps(raised.exception.result, ensure_ascii=False)
                for path in job.rglob("*"):
                    if path.is_file():
                        serialized += path.read_text(encoding="utf-8", errors="ignore")
                for secret in (
                    "asset_private_token",
                    "provider.example",
                    "document_private_token",
                ):
                    self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
