# ruff: noqa: E402,I001
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from cli.jy_wrapper_parser import build_parser
import jy_wrapper
import review_job


class ReviewDocumentCliTests(unittest.TestCase):
    def test_revision_run_forwards_structured_execution_input(self) -> None:
        args = build_parser().parse_args(
            [
                "revision-run",
                "--request-json",
                "request.json",
                "--workflow-mode",
                "lite",
                "--execution-input",
                "execution-input.json",
            ]
        )
        expected = {"ok": True, "code": "ok", "reason": "", "data": {}}

        self.assertEqual(args.execution_input_json, "execution-input.json")
        with patch.object(jy_wrapper, "cmd_revision_run", return_value=expected) as command:
            result = jy_wrapper.COMMAND_HANDLERS["revision-run"](args)

        self.assertEqual(result, expected)
        command.assert_called_once_with(
            "request.json",
            None,
            False,
            doc_items_json=None,
            strict=False,
            workflow_mode="lite",
            package_zip=None,
            relink_tool=None,
            package_root_name=None,
            package_receipt=None,
            execution_input_json="execution-input.json",
        )

    def test_parser_exposes_lite_review_document_controls_and_output_dir_alias(self) -> None:
        args = build_parser().parse_args(
            [
                "review-document-run",
                "--snapshot-json",
                "snapshot.json",
                "--project-json",
                "project.json",
                "--output-dir",
                "tmp/job-42",
                "--drafts-root",
                "drafts",
                "--package-zip",
                "delivery.zip",
                "--relink-tool",
                "relink.exe",
                "--execution-input",
                "execution-input.json",
                "--mock-media",
                "--asr-timeout-seconds",
                "30",
                "--asr-poll-interval-seconds",
                "0.5",
                "--asr-max-wait-seconds",
                "900",
                "--context-before",
                "7",
                "--context-after",
                "9",
                "--json",
            ]
        )

        self.assertEqual(args.cmd, "review-document-run")
        self.assertIsNone(args.doc_url)
        self.assertEqual(args.snapshot_json, "snapshot.json")
        self.assertEqual(args.project_json, "project.json")
        self.assertEqual(args.job_root, "tmp/job-42")
        self.assertEqual(args.drafts_root, "drafts")
        self.assertEqual(args.package_zip, "delivery.zip")
        self.assertEqual(args.relink_tool, "relink.exe")
        self.assertEqual(args.execution_input_json, "execution-input.json")
        self.assertTrue(args.mock_media)
        self.assertEqual(args.asr_timeout_seconds, 30.0)
        self.assertEqual(args.asr_poll_interval_seconds, 0.5)
        self.assertEqual(args.asr_max_wait_seconds, 900.0)
        self.assertEqual(args.context_before, 7.0)
        self.assertEqual(args.context_after, 9.0)
        self.assertTrue(args.json)
        self.assertFalse(hasattr(args, "workflow_mode"))

    def test_parser_defaults_use_job_root_and_maintained_asr_controls(self) -> None:
        args = build_parser().parse_args(
            [
                "review-document-run",
                "--snapshot-json",
                "snapshot.json",
                "--project-json",
                "project.json",
                "--job-root",
                "tmp/job",
                "--drafts-root",
                "drafts",
                "--package-zip",
                "delivery.zip",
            ]
        )

        self.assertEqual(args.job_root, "tmp/job")
        self.assertEqual(args.asr_timeout_seconds, 60.0)
        self.assertEqual(args.asr_poll_interval_seconds, 2.0)
        self.assertEqual(args.asr_max_wait_seconds, 120.0)
        self.assertEqual(args.context_before, 5.0)
        self.assertEqual(args.context_after, 5.0)
        self.assertIsNone(args.execution_input_json)

    def test_parser_exposes_mutually_exclusive_document_url_mode(self) -> None:
        args = build_parser().parse_args(
            [
                "review-document-run",
                "--doc-url",
                "https://example.feishu.cn/wiki/wiki_token",
                "--job-root",
                "tmp/job",
                "--drafts-root",
                "drafts",
                "--package-zip",
                "delivery.zip",
            ]
        )

        self.assertEqual(args.doc_url, "https://example.feishu.cn/wiki/wiki_token")
        self.assertIsNone(args.snapshot_json)
        self.assertIsNone(args.project_json)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "review-document-run",
                    "--doc-url",
                    "https://example.feishu.cn/wiki/wiki_token",
                    "--snapshot-json",
                    "snapshot.json",
                    "--job-root",
                    "tmp/job",
                    "--drafts-root",
                    "drafts",
                    "--package-zip",
                    "delivery.zip",
                ]
            )

    def test_command_wrapper_forces_lite_and_returns_protocol_result(self) -> None:
        calls = []

        def fake_run_review_document(**kwargs):
            calls.append(kwargs)
            return {"workflow_mode": "lite", "completion_boundary": "lite_zip_delivery"}

        fake_module = SimpleNamespace(
            ReviewDocumentRunError=RuntimeError,
            run_review_document=fake_run_review_document,
        )
        with patch.dict(sys.modules, {"utils.review_document_runner": fake_module}):
            result = review_job.cmd_review_document_run(
                "snapshot.json",
                "project.json",
                "tmp/job",
                drafts_root="drafts",
                package_zip="delivery.zip",
                relink_tool="relink.exe",
                execution_input_json="execution-input.json",
                mock_media=True,
                asr_timeout_seconds=45.0,
                asr_poll_interval_seconds=1.5,
                asr_max_wait_seconds=600.0,
                context_before=8.0,
                context_after=6.0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["completion_boundary"], "lite_zip_delivery")
        self.assertEqual(
            calls,
            [
                {
                    "snapshot_json": "snapshot.json",
                    "project_json": "project.json",
                    "doc_url": None,
                    "job_root": "tmp/job",
                    "drafts_root": "drafts",
                    "package_zip": "delivery.zip",
                    "relink_tool": "relink.exe",
                    "execution_input_json": "execution-input.json",
                    "mock_media": True,
                    "asr_timeout_seconds": 45.0,
                    "asr_poll_interval_seconds": 1.5,
                    "asr_max_wait_seconds": 600.0,
                    "context_before": 8.0,
                    "context_after": 6.0,
                    "workflow_mode": "lite",
                    "progress": None,
                }
            ],
        )

    def test_dispatch_forwards_every_public_control(self) -> None:
        args = build_parser().parse_args(
            [
                "review-document-run",
                "--snapshot-json",
                "snapshot.json",
                "--project-json",
                "project.json",
                "--job-root",
                "tmp/job",
                "--drafts-root",
                "drafts",
                "--package-zip",
                "delivery.zip",
                "--execution-input",
                "execution-input.json",
                "--mock-media",
            ]
        )
        expected = {"ok": True, "code": "ok", "reason": "", "data": {}}

        with patch.object(
            jy_wrapper, "cmd_review_document_run", return_value=expected
        ) as command:
            result = jy_wrapper.COMMAND_HANDLERS["review-document-run"](args)

        self.assertEqual(result, expected)
        command.assert_called_once_with(
            "snapshot.json",
            "project.json",
            "tmp/job",
            doc_url=None,
            drafts_root="drafts",
            package_zip="delivery.zip",
            relink_tool=None,
            execution_input_json="execution-input.json",
            mock_media=True,
            asr_timeout_seconds=60.0,
            asr_poll_interval_seconds=2.0,
            asr_max_wait_seconds=120.0,
            context_before=5.0,
            context_after=5.0,
            progress=None,
        )

    def test_command_wrapper_rejects_incomplete_or_mixed_input_modes(self) -> None:
        with self.assertRaisesRegex(Exception, "requires both"):
            review_job.cmd_review_document_run(
                "snapshot.json",
                None,
                "tmp/job",
                drafts_root="drafts",
                package_zip="delivery.zip",
            )
        with self.assertRaisesRegex(Exception, "mutually exclusive"):
            review_job.cmd_review_document_run(
                "snapshot.json",
                "project.json",
                "tmp/job",
                doc_url="https://example.feishu.cn/wiki/wiki_token",
                drafts_root="drafts",
                package_zip="delivery.zip",
            )

    def test_runner_failure_keeps_structured_diagnostics(self) -> None:
        class FakeRunError(RuntimeError):
            def __init__(self) -> None:
                super().__init__("final acceptance failed")
                self.result = {"workflow_mode": "lite", "failed_phase": "final_acceptance"}

        def fail_run_review_document(**_kwargs):
            raise FakeRunError()

        fake_module = SimpleNamespace(
            ReviewDocumentRunError=FakeRunError,
            run_review_document=fail_run_review_document,
        )
        with patch.dict(sys.modules, {"utils.review_document_runner": fake_module}):
            result = review_job.cmd_review_document_run(
                "snapshot.json",
                "project.json",
                "tmp/job",
                drafts_root="drafts",
                package_zip="delivery.zip",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "review_document_failed")
        self.assertEqual(result["reason"], "final acceptance failed")
        self.assertEqual(result["data"]["workflow_mode"], "lite")
        self.assertEqual(result["data"]["failed_phase"], "final_acceptance")


if __name__ == "__main__":
    unittest.main()
