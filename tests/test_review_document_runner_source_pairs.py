import hashlib
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from utils import review_document_runner as runner
from utils.review_job_pipeline import ArtifactCache
from utils.source_manifest import canonical_sha256, load_source_manifest

from audio_sound.volc_asr import VolcAsrConfig
from tests import test_review_document_runner as runner_test_support


def _write_wav(path: Path, *, fill: int, duration: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        sample = int(fill).to_bytes(2, "little", signed=True)
        target.writeframes(sample * int(16000 * duration))


class _WaitStore:
    def __init__(self) -> None:
        self.waits: list[tuple[str, float]] = []

    def add_wait_seconds(self, phase: str, seconds: float) -> None:
        self.waits.append((phase, seconds))


class ReviewDocumentRunnerSourcePairTests(unittest.TestCase):
    def _manifest_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "binding": {
                "task_id": "task-1",
                "run_id": "run-1",
                "subject_key": "bas_demo:tbl_math",
                "config_version": 7,
                "stage_id": "initial",
                "event_id": "evt-1",
            },
            "record": {
                "base_token": "bas_demo",
                "table_id": "tbl_math",
                "record_id": "rec_1",
            },
            "document": {
                "field_id": "fld_document",
                "url": "https://guanghe.feishu.cn/docx/opaque-token",
            },
            "sources": {
                "video": {"kind": "docx_section", "anchor_text": "video"},
                "review": {"kind": "docx_section", "anchor_text": "review"},
                "audio": {"mode": "video_original"},
            },
        }

    def _manifest_environment(self, digest: str) -> dict[str, str]:
        return {
            "CODEX_AUTOCUT_TASK_ID": "task-1",
            "CODEX_AUTOCUT_RUN_ID": "run-1",
            "CODEX_AUTOCUT_SUBJECT_KEY": "bas_demo:tbl_math",
            "CODEX_AUTOCUT_CONFIG_VERSION": "7",
            "CODEX_AUTOCUT_STAGE_ID": "initial",
            "CODEX_AUTOCUT_EVENT_ID": "evt-1",
            "CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256": digest,
        }

    def test_manifest_package_path_must_match_normalized_artifact_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            expected = root / "Expected Draft.zip"
            runner._validate_manifest_package_path(expected, "Expected Draft")
            with self.assertRaises(runner.SourceManifestError) as context:
                runner._validate_manifest_package_path(root / "requested.zip", "Expected Draft")

        self.assertEqual(context.exception.code, "package_path_mismatch")

    def test_manifest_validation_failure_writes_bound_blocked_receipt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = root / "source-manifest.json"
            manifest.write_text("{", encoding="utf-8")
            result_path = root / "driver-result.json"
            digest = "a" * 64
            with patch.dict(os.environ, self._manifest_environment(digest), clear=False):
                with self.assertRaises(runner.ReviewDocumentRunError):
                    runner.run_review_document(
                        source_manifest_json=manifest,
                        result_path=result_path,
                        job_root=root / "job",
                        drafts_root=root / "drafts",
                        package_zip=root / "Expected Draft.zip",
                        workflow_mode="lite",
                        mock_media=True,
                    )

            receipt = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "blocked")
            self.assertEqual(receipt["manifest_sha256"], digest)
            self.assertEqual(receipt["binding"]["task_id"], "task-1")
            self.assertEqual(receipt["error"]["code"], "source_manifest_invalid")

    def test_manifest_blocked_receipt_error_message_does_not_expose_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload = self._manifest_payload()
            manifest = root / "source-manifest.json"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            digest = canonical_sha256(payload)
            result_path = root / "driver-result.json"
            missing_relink_tool = root / "private" / "missing-relink-tool.exe"
            with patch.dict(os.environ, self._manifest_environment(digest), clear=False):
                with self.assertRaises(runner.ReviewDocumentRunError):
                    runner.run_review_document(
                        source_manifest_json=manifest,
                        result_path=result_path,
                        job_root=root / "job",
                        drafts_root=root / "drafts",
                        package_zip=root / "Expected Draft.zip",
                        relink_tool=missing_relink_tool,
                        workflow_mode="lite",
                        mock_media=True,
                    )

            receipt = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "blocked")
            self.assertNotIn(str(missing_relink_tool), receipt["error"]["message"])
            self.assertEqual(
                receipt["error"]["message"],
                "Auto-Cut run blocked: autocut_failed",
            )

    def test_base_attachment_manifest_has_shared_literal_canonical_sha256(self):
        manifest_payload = {
            "schema_version": 1,
            "binding": {
                "task_id": "task-1",
                "run_id": "run-1",
                "subject_key": "bas_demo:tbl_math",
                "config_version": 7,
                "stage_id": "initial",
                "event_id": "evt-1",
            },
            "record": {
                "base_token": "bas_demo",
                "table_id": "tbl_math",
                "record_id": "rec_1",
            },
            "document": {
                "field_id": "fld_document",
                "url": "https://guanghe.feishu.cn/docx/opaque-token",
            },
            "sources": {
                "video": {"kind": "base_attachment", "field_id": "fld_video"},
                "review": {"kind": "docx_section", "anchor_text": "review"},
                "audio": {
                    "mode": "replace_original",
                    "duration_tolerance_seconds": 3,
                    "source": {"kind": "base_attachment", "field_id": "fld_audio"},
                },
            },
        }
        expected_digest = "f77a0143746714042bfd0373710ce9141131f483d9672f7f35b923b7b1350b5d"
        environment = {
            "CODEX_AUTOCUT_TASK_ID": "task-1",
            "CODEX_AUTOCUT_RUN_ID": "run-1",
            "CODEX_AUTOCUT_SUBJECT_KEY": "bas_demo:tbl_math",
            "CODEX_AUTOCUT_CONFIG_VERSION": "7",
            "CODEX_AUTOCUT_STAGE_ID": "initial",
            "CODEX_AUTOCUT_EVENT_ID": "evt-1",
            "CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256": expected_digest,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "source-manifest.json"
            manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
            with patch.dict(os.environ, environment, clear=False):
                loaded = load_source_manifest(manifest.resolve())

        self.assertEqual(loaded.canonical_sha256, expected_digest)
        self.assertEqual(
            loaded.data["sources"]["video"],
            {
                "kind": "base_attachment",
                "base_token": "bas_demo",
                "table_id": "tbl_math",
                "record_id": "rec_1",
                "field_id": "fld_video",
            },
        )
        self.assertEqual(
            loaded.data["sources"]["audio"]["source"]["record_id"],
            "rec_1",
        )

    def test_job_input_digest_includes_every_ordered_source_pair_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = root / "snapshot.json"
            snapshot.write_text("{}", encoding="utf-8")
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            project = {
                "source_video": str(first),
                "source_pairs": [
                    {
                        "pair_index": 0,
                        "video_path": str(first),
                        "video_sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
                        "audio_mode": "video_original",
                    },
                    {
                        "pair_index": 1,
                        "video_path": str(second),
                        "video_sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
                        "audio_mode": "video_original",
                    },
                ],
            }
            project_path = root / "project.json"
            project_path.write_text(json.dumps(project), encoding="utf-8")
            before = runner._job_input_digest(
                snapshot,
                project_path,
                project,
                options={"input_mode": "source_manifest"},
            )
            second.write_bytes(b"second changed")
            after = runner._job_input_digest(
                snapshot,
                project_path,
                project,
                options={"input_mode": "source_manifest"},
            )

            self.assertNotEqual(before, after)

    def test_merge_source_asr_words_applies_cumulative_pair_offsets(self):
        merged = runner._merge_source_asr_words(
            [
                {
                    "pair_index": 0,
                    "offset": 0.0,
                    "duration": 4.0,
                    "words": [{"text": "第一段", "start": 1.0, "end": 1.5}],
                },
                {
                    "pair_index": 1,
                    "offset": 4.0,
                    "duration": 6.0,
                    "words": [{"text": "第二段", "start": 0.5, "end": 1.0}],
                },
            ]
        )

        self.assertEqual(
            [(row["text"], row["start"], row["end"], row["pair_index"]) for row in merged],
            [("第一段", 1.0, 1.5, 0), ("第二段", 4.5, 5.0, 1)],
        )

    def test_merge_source_asr_words_rejects_pair_local_timing_outside_duration(self):
        with self.assertRaisesRegex(
            runner.OrderedSourceAsrIntegrityError,
            "pair 0.*outside",
        ):
            runner._merge_source_asr_words(
                [
                    {
                        "pair_index": 0,
                        "offset": 0.0,
                        "duration": 2.0,
                        "words": [{"text": "越界", "start": 1.0, "end": 2.5}],
                    }
                ]
            )

    def test_ordered_source_asr_recognizes_each_pair_and_preserves_provider_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first.wav"
            second = root / "second.wav"
            _write_wav(first, fill=1, duration=2.0)
            _write_wav(second, fill=2, duration=3.0)
            asr_calls: list[str] = []

            def fake_asr(_cache, _identity, *, audio_path, **_kwargs):
                pair_index = 0 if Path(audio_path).name.endswith("000.wav") else 1
                asr_calls.append(Path(audio_path).name)
                word = (
                    {"text": "first", "start": 0.5, "end": 0.8}
                    if pair_index == 0
                    else {"text": "second", "start": 0.25, "end": 0.75}
                )
                return (
                    {
                        "schema_version": 1,
                        "provider": "test-provider",
                        "resource_id": "test-resource",
                        "adapter_version": "test-adapter",
                        "input_sha256": runner.sha256_file(audio_path),
                        "service_result_sha256": str(pair_index + 1) * 64,
                        "words": [word],
                    },
                    False,
                )

            def copy_alignment(source, output, **_kwargs):
                runner.atomic_copy_file(source, output)

            with (
                patch.object(runner, "extract_alignment_wav", side_effect=copy_alignment),
                patch.object(runner, "_cached_asr_json", side_effect=fake_asr),
            ):
                source_asr, source_index, _artifacts, _cache_hits = runner._run_ordered_source_asr(
                    [
                        {
                            "pair_index": 0,
                            "offset": 0.0,
                            "duration": 2.0,
                            "path": str(first),
                            "sha256": runner.sha256_file(first),
                        },
                        {
                            "pair_index": 1,
                            "offset": 2.0,
                            "duration": 3.0,
                            "path": str(second),
                            "sha256": runner.sha256_file(second),
                        },
                    ],
                    materials_dir=root / "materials",
                    alignment_output=root / "combined.wav",
                    source_asr_output=root / "source-asr.json",
                    cache=ArtifactCache(root / "cache"),
                    inflight_root=root / "inflight",
                    ffmpeg_bin="ffmpeg",
                    ffmpeg_info={"version": "test"},
                    config=VolcAsrConfig(api_key="test-key"),
                    asr_timeout_seconds=1.0,
                    asr_poll_interval_seconds=0.01,
                    asr_max_wait_seconds=1.0,
                    store=_WaitStore(),
                )

            self.assertEqual(
                asr_calls, ["source_alignment_pair_000.wav", "source_alignment_pair_001.wav"]
            )
            self.assertEqual(source_asr["provider"], "test-provider")
            self.assertEqual(source_asr["resource_id"], "test-resource")
            self.assertEqual(source_asr["adapter_version"], "test-adapter")
            self.assertEqual(
                [
                    (row["text"], row["start"], row["end"], row["pair_index"])
                    for row in source_asr["words"]
                ],
                [("first", 0.5, 0.8, 0), ("second", 2.25, 2.75, 1)],
            )
            self.assertEqual(source_index["source_pair_count"], 2)
            self.assertEqual(len(source_index["source_asr_cache_identity_digests"]), 2)

    def test_ordered_source_asr_rejects_provider_input_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.wav"
            _write_wav(source, fill=1, duration=1.0)

            def fake_asr(_cache, _identity, **_kwargs):
                return (
                    {
                        "schema_version": 1,
                        "provider": "test-provider",
                        "resource_id": "test-resource",
                        "adapter_version": "test-adapter",
                        "input_sha256": "f" * 64,
                        "words": [{"text": "bad", "start": 0.1, "end": 0.2}],
                    },
                    False,
                )

            with (
                patch.object(
                    runner,
                    "extract_alignment_wav",
                    side_effect=lambda source_path, output, **_kwargs: runner.atomic_copy_file(
                        source_path, output
                    ),
                ),
                patch.object(runner, "_cached_asr_json", side_effect=fake_asr),
            ):
                with self.assertRaisesRegex(
                    runner.OrderedSourceAsrIntegrityError,
                    "input identity",
                ):
                    runner._run_ordered_source_asr(
                        [
                            {
                                "pair_index": 0,
                                "offset": 0.0,
                                "duration": 1.0,
                                "path": str(source),
                                "sha256": runner.sha256_file(source),
                            }
                        ],
                        materials_dir=root / "materials",
                        alignment_output=root / "combined.wav",
                        source_asr_output=root / "source-asr.json",
                        cache=ArtifactCache(root / "cache"),
                        inflight_root=root / "inflight",
                        ffmpeg_bin="ffmpeg",
                        ffmpeg_info={"version": "test"},
                        config=VolcAsrConfig(api_key="test-key"),
                        asr_timeout_seconds=1.0,
                        asr_poll_interval_seconds=0.01,
                        asr_max_wait_seconds=1.0,
                        store=_WaitStore(),
                    )

    def test_runner_recognizes_all_pairs_and_keeps_writer_audio_plan_legacy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"first-video")
            second.write_bytes(b"second-video")
            snapshot = root / "snapshot.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "document": {
                            "id": "doc-pairs",
                            "revision": "r1",
                            "title": "PairDraft",
                        },
                        "items": [
                            {
                                "id": "spoken-second",
                                "kind": "spoken_delete",
                                "source_text": "00:04 delete second",
                                "start": 3.2,
                                "end": 4.2,
                                "execution_required": True,
                                "evidence": {
                                    "delete": "second",
                                    "strategy": "precision_first",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            project = root / "project.json"
            project.write_text(
                json.dumps(
                    {
                        "draft_name": "PairDraft",
                        "source_video": str(first),
                        "audio_mode": "video_original",
                        "media_duration_seconds": 6.0,
                        "source_pairs": [
                            {
                                "pair_index": 0,
                                "video_path": str(first),
                                "video_sha256": runner.sha256_file(first),
                                "video_duration_seconds": 3.0,
                                "audio_mode": "video_original",
                            },
                            {
                                "pair_index": 1,
                                "video_path": str(second),
                                "video_sha256": runner.sha256_file(second),
                                "video_duration_seconds": 3.0,
                                "audio_mode": "video_original",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            support = runner_test_support.ReviewDocumentRunnerTests(
                methodName="test_fixed_dag_caches_source_and_reverse_asr_and_resumes_every_phase"
            )

            def extract_pair_alignment(source, output, **_kwargs):
                fill = 2 if Path(source).name == "second.mp4" else 1
                _write_wav(Path(output), fill=fill, duration=3.0)

            asr_inputs: list[str] = []

            def fake_pair_asr(audio_path, **_kwargs):
                path = Path(audio_path)
                asr_inputs.append(path.name)
                if path.name == "source_alignment_pair_000.wav":
                    words = [{"text": "first", "start": 1.0, "end": 1.4}]
                elif path.name == "source_alignment_pair_001.wav":
                    words = [
                        {"text": "second", "start": 0.5, "end": 0.9},
                        {"text": "after", "start": 1.3, "end": 1.7},
                    ]
                elif path.name == "candidate_source_aligned.wav":
                    words = [
                        {"text": "first", "start": 1.0, "end": 1.4},
                        {"text": "after", "start": 4.3, "end": 4.7},
                    ]
                else:
                    raise AssertionError(f"unexpected single-source ASR input: {path.name}")
                return {
                    "schema_version": 1,
                    "provider": "volc_asr",
                    "resource_id": "volc.bigasr.auc",
                    "adapter_version": "test-adapter-v1",
                    "input_sha256": runner.sha256_file(path),
                    "service_job_id": path.stem,
                    "service_result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "words": words,
                }

            with support._patched_runtime() as mocks:
                mocks["extract"].side_effect = extract_pair_alignment
                mocks["asr"].side_effect = fake_pair_asr
                result = runner.run_review_document(
                    snapshot,
                    project,
                    job_root=root / "job",
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                    workflow_mode="lite",
                    mock_media=True,
                )
                first_asr_call_count = len(asr_inputs)
                resumed = runner.run_review_document(
                    snapshot,
                    project,
                    job_root=root / "job",
                    drafts_root=root / "drafts",
                    package_zip=root / "delivery.zip",
                    cache_root=root / "cache",
                    workflow_mode="lite",
                    mock_media=True,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(
                asr_inputs[:2],
                ["source_alignment_pair_000.wav", "source_alignment_pair_001.wav"],
            )
            source_asr = json.loads(
                Path(result["output_artifacts"]["source_asr"]["path"]).read_text(encoding="utf-8")
            )
            second_word = next(row for row in source_asr["words"] if row["text"] == "second")
            self.assertEqual(
                (second_word["start"], second_word["end"], second_word["pair_index"]),
                (3.5, 3.9, 1),
            )
            processed_request = json.loads(
                Path(result["output_artifacts"]["revision_request"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(processed_request["audio_delivery_plan"]["mode"], "legacy")
            self.assertEqual(
                [
                    row["source_audio_path"]
                    for row in processed_request["project"]["source_pairs"]
                ],
                [str(first), str(second)],
            )
            self.assertEqual(
                [
                    row["source_audio_sha256"]
                    for row in processed_request["project"]["source_pairs"]
                ],
                [runner.sha256_file(first), runner.sha256_file(second)],
            )
            self.assertEqual(mocks["execute"].call_args.args[0].audio_delivery_plan.mode, "legacy")
            self.assertEqual(resumed["phases"]["source_asr"]["status"], "resumed")
            self.assertEqual(len(asr_inputs), first_asr_call_count)

            actual_execution = runner.execute_revision_request(
                runner.load_revision_request(
                    result["output_artifacts"]["revision_request"]["path"]
                ),
                drafts_root=str(root / "actual-drafts"),
                mock_media=True,
                strict=True,
                doc_items=runner.load_review_items_json(
                    result["output_artifacts"]["doc_items"]["path"]
                ),
            )
            self.assertEqual(len(actual_execution["source_pairs"]), 2)
            self.assertTrue(actual_execution["validation"]["ok"])

    def test_manifest_success_terminal_receipt_contains_exact_delivery_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "source.mp4"
            video.write_bytes(b"manifest-video")
            manifest_payload = {
                "schema_version": 1,
                "binding": {
                    "task_id": "task-1",
                    "run_id": "run-1",
                    "subject_key": "bas_demo:tbl_math",
                    "config_version": 7,
                    "stage_id": "initial",
                    "event_id": "evt-1",
                },
                "record": {
                    "base_token": "bas_demo",
                    "table_id": "tbl_math",
                    "record_id": "rec_1",
                },
                "document": {
                    "field_id": "fld_document",
                    "url": "https://guanghe.feishu.cn/docx/opaque-token",
                },
                "sources": {
                    "video": {"kind": "docx_section", "anchor_text": "video"},
                    "review": {"kind": "docx_section", "anchor_text": "review"},
                    "audio": {"mode": "video_original"},
                },
            }
            manifest = root / "source-manifest.json"
            manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
            execution_input = root / "execution-input.json"
            execution_input.write_text(
                json.dumps({"schema_version": 1, "artifact_name": "Expected Draft"}),
                encoding="utf-8",
            )
            result_path = root / "driver-result.json"
            material = {
                "asset_id": "video-1",
                "filename": "source.mp4",
                "path": str(video),
                "sha256": runner.sha256_file(video),
                "byte_size": video.stat().st_size,
                "extension": ".mp4",
                "mime": "video/mp4",
                "duration_seconds": 3.0,
            }
            materialized = {
                "document": {
                    "document_identity_sha256": "d" * 64,
                    "revision_id": "r1",
                    "content_sha256": "c" * 64,
                },
                "videos": [material],
                "audios": [],
                "review_items": [
                    {
                        "id": "review-1",
                        "kind": "review_only",
                        "source_text": "check this frame",
                        "start": 1.0,
                        "end": 1.8,
                        "execution_required": False,
                    }
                ],
                "receipts": [material],
            }
            manifest_digest = canonical_sha256(manifest_payload)
            environment = {
                "CODEX_AUTOCUT_TASK_ID": "task-1",
                "CODEX_AUTOCUT_RUN_ID": "run-1",
                "CODEX_AUTOCUT_SUBJECT_KEY": "bas_demo:tbl_math",
                "CODEX_AUTOCUT_CONFIG_VERSION": "7",
                "CODEX_AUTOCUT_STAGE_ID": "initial",
                "CODEX_AUTOCUT_EVENT_ID": "evt-1",
                "CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256": manifest_digest,
            }
            support = runner_test_support.ReviewDocumentRunnerTests(
                methodName="test_fixed_dag_caches_source_and_reverse_asr_and_resumes_every_phase"
            )

            with (
                patch.dict(os.environ, environment, clear=False),
                support._patched_runtime(),
                patch.object(runner, "lark_cli_version", return_value="test-lark"),
                patch.object(runner, "lark_whoami", return_value={"open_id": "test-user"}),
                patch.object(runner, "evaluate_runtime_readiness"),
                patch.object(
                    runner,
                    "materialize_manifest_sources",
                    return_value=materialized,
                ),
            ):
                result = runner.run_review_document(
                    source_manifest_json=manifest,
                    execution_input_json=execution_input,
                    result_path=result_path,
                    job_root=root / "job",
                    drafts_root=root / "drafts",
                    package_zip=root / "Expected Draft.zip",
                    cache_root=root / "cache",
                    workflow_mode="lite",
                    mock_media=True,
                )

            self.assertTrue(result["ok"])
            receipt = json.loads(result_path.read_text(encoding="utf-8"))
            expected_package = (root / "Expected Draft.zip").resolve()
            self.assertEqual(receipt["status"], "pass")
            self.assertEqual(receipt["binding"], manifest_payload["binding"])
            self.assertEqual(receipt["manifest_sha256"], manifest_digest)
            self.assertEqual(receipt["package_zip"], str(expected_package))
            self.assertEqual(receipt["archive_sha256"], runner.sha256_file(expected_package))
            self.assertEqual(receipt["draft_name"], "Expected Draft")

    def test_ordered_source_asr_integrity_failure_replaces_stale_pass_receipt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "source.mp4"
            video.write_bytes(b"manifest-video")
            manifest_payload = {
                "schema_version": 1,
                "binding": {
                    "task_id": "task-1",
                    "run_id": "run-1",
                    "subject_key": "bas_demo:tbl_math",
                    "config_version": 7,
                    "stage_id": "initial",
                    "event_id": "evt-1",
                },
                "record": {
                    "base_token": "bas_demo",
                    "table_id": "tbl_math",
                    "record_id": "rec_1",
                },
                "document": {
                    "field_id": "fld_document",
                    "url": "https://guanghe.feishu.cn/docx/opaque-token",
                },
                "sources": {
                    "video": {"kind": "docx_section", "anchor_text": "video"},
                    "review": {"kind": "docx_section", "anchor_text": "review"},
                    "audio": {"mode": "video_original"},
                },
            }
            manifest = root / "source-manifest.json"
            manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
            execution_input = root / "execution-input.json"
            execution_input.write_text(
                json.dumps({"schema_version": 1, "artifact_name": "Expected Draft"}),
                encoding="utf-8",
            )
            result_path = root / "driver-result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "package_zip": str(root / "stale.zip"),
                        "archive_sha256": "f" * 64,
                    }
                ),
                encoding="utf-8",
            )
            material = {
                "asset_id": "video-1",
                "filename": "source.mp4",
                "path": str(video),
                "sha256": runner.sha256_file(video),
                "byte_size": video.stat().st_size,
                "extension": ".mp4",
                "mime": "video/mp4",
                "duration_seconds": 3.0,
            }
            materialized = {
                "document": {
                    "document_identity_sha256": "d" * 64,
                    "revision_id": "r1",
                    "content_sha256": "c" * 64,
                },
                "videos": [material],
                "audios": [],
                "review_items": [
                    {
                        "id": "spoken-1",
                        "kind": "spoken_delete",
                        "source_text": "delete repeated word",
                        "start": 1.0,
                        "end": 1.8,
                        "execution_required": True,
                        "evidence": {"delete": "word", "strategy": "precision_first"},
                    }
                ],
                "receipts": [material],
            }
            manifest_digest = canonical_sha256(manifest_payload)
            environment = {
                "CODEX_AUTOCUT_TASK_ID": "task-1",
                "CODEX_AUTOCUT_RUN_ID": "run-1",
                "CODEX_AUTOCUT_SUBJECT_KEY": "bas_demo:tbl_math",
                "CODEX_AUTOCUT_CONFIG_VERSION": "7",
                "CODEX_AUTOCUT_STAGE_ID": "initial",
                "CODEX_AUTOCUT_EVENT_ID": "evt-1",
                "CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256": manifest_digest,
            }
            support = runner_test_support.ReviewDocumentRunnerTests(
                methodName="test_fixed_dag_caches_source_and_reverse_asr_and_resumes_every_phase"
            )

            with (
                patch.dict(os.environ, environment, clear=False),
                support._patched_runtime(),
                patch.object(runner, "lark_cli_version", return_value="test-lark"),
                patch.object(runner, "lark_whoami", return_value={"open_id": "test-user"}),
                patch.object(runner, "evaluate_runtime_readiness"),
                patch.object(
                    runner,
                    "materialize_manifest_sources",
                    return_value=materialized,
                ),
                patch.object(
                    runner,
                    "_run_ordered_source_asr",
                    side_effect=runner.OrderedSourceAsrIntegrityError(
                        "source ASR pair identity mismatch"
                    ),
                ),
            ):
                with self.assertRaises(runner.ReviewDocumentRunError):
                    runner.run_review_document(
                        source_manifest_json=manifest,
                        execution_input_json=execution_input,
                        result_path=result_path,
                        job_root=root / "job",
                        drafts_root=root / "drafts",
                        package_zip=root / "Expected Draft.zip",
                        cache_root=root / "cache",
                        workflow_mode="lite",
                        mock_media=True,
                    )

            receipt = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "blocked")
            self.assertEqual(receipt["binding"], manifest_payload["binding"])
            self.assertEqual(receipt["manifest_sha256"], manifest_digest)
            self.assertEqual(receipt["error"]["code"], "source_pair_asr_integrity")
            self.assertNotIn("package_zip", receipt)
            self.assertNotIn("archive_sha256", receipt)
            self.assertNotIn("draft_name", receipt)


if __name__ == "__main__":
    unittest.main()
