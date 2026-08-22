# ruff: noqa: E402,I001
import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from cli.jy_wrapper_parser import build_parser
from utils.lite_package import LitePackageError, package_lite_delivery
import jy_wrapper


class LitePackageTests(unittest.TestCase):
    def _draft(self, root: Path) -> Path:
        draft = root / "必修下-第15课-2_从二月革命到十月革命_精简版_R41"
        (draft / "Resources" / "local").mkdir(parents=True)
        (draft / "draft_content.json").write_text(
            '{"materials":{"videos":[]},"tracks":[]}\n', encoding="utf-8"
        )
        (draft / "draft_meta_info.json").write_text(
            '{"draft_name":"lite"}\n', encoding="utf-8"
        )
        (draft / "Resources" / "local" / "source-video.mp4").write_bytes(b"video-bytes")
        return draft

    def test_package_is_byte_preserving_and_self_validating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = self._draft(root)
            tool = root / "relink-tool.exe"
            tool.write_bytes(b"relink-tool-bytes")
            output = root / "Desktop" / "final.zip"
            receipt = root / "receipts" / "final.json"
            source_content = (draft / "draft_content.json").read_bytes()

            result = package_lite_delivery(
                draft,
                output,
                relink_tool=tool,
                package_root_name="剪映草稿ZIP打包测试_R41",
                receipt_json=receipt,
            )

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["workflow_mode"], "lite")
            self.assertEqual(result["delivery_mode"], "lite_zip")
            self.assertTrue(result["relink_tool_included"])
            self.assertFalse(result["json_rewritten"])
            self.assertFalse(result["ui_invoked"])
            self.assertFalse(result["opened_jianying"])
            self.assertEqual(source_content, (draft / "draft_content.json").read_bytes())
            self.assertEqual(result["archive_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())

            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                root_name = "剪映草稿ZIP打包测试_R41"
                self.assertIn(f"{root_name}/Auto-Cut剪映素材重链工具.exe", names)
                self.assertIn(f"{root_name}/使用说明.txt", names)
                self.assertIn(
                    f"{root_name}/{draft.name}/draft_content.json",
                    names,
                )
                self.assertIn(
                    f"{root_name}/{draft.name}/Resources/local/source-video.mp4",
                    names,
                )
                self.assertIn("路径不同", archive.read(f"{root_name}/使用说明.txt").decode("utf-8"))

            stored_receipt = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(stored_receipt["archive_sha256"], result["archive_sha256"])
            self.assertEqual(stored_receipt["package_tree_sha256"], result["package_tree_sha256"])
            self.assertEqual(stored_receipt["extracted_tree_sha256"], result["package_tree_sha256"])

    def test_package_refuses_overwrite_and_source_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = self._draft(root)
            output = root / "final.zip"
            package_lite_delivery(draft, output)
            with self.assertRaises(LitePackageError):
                package_lite_delivery(draft, output)
            with self.assertRaises(LitePackageError):
                package_lite_delivery(draft, draft / "nested.zip")
            with self.assertRaises(LitePackageError):
                package_lite_delivery(draft, root / "same.zip", receipt_json=root / "same.zip")

    def test_revision_run_parser_exposes_unattended_lite_package_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "revision-run",
                "--request-json",
                "request.json",
                "--workflow-mode",
                "lite",
                "--strict",
                "--package-zip",
                "Desktop/final.zip",
                "--package-root-name",
                "delivery",
                "--package-receipt",
                "tmp/receipt.json",
            ]
        )
        self.assertEqual(args.package_zip, "Desktop/final.zip")
        self.assertEqual(args.package_root_name, "delivery")
        self.assertEqual(args.package_receipt, "tmp/receipt.json")
        self.assertIsNone(args.relink_tool)

    def test_revision_run_packages_only_after_lite_acceptance_result(self) -> None:
        fake_request = SimpleNamespace(workflow_mode="lite")
        fake_execution = {"draft_path": "C:/drafts/LiteDraft", "validation": {"ok": True}}
        fake_package = {
            "status": "pass",
            "delivery_mode": "lite_zip",
            "archive_path": "C:/Desktop/LiteDraft.zip",
        }
        with (
            patch.object(jy_wrapper, "load_revision_request", return_value=fake_request),
            patch.object(jy_wrapper, "execute_revision_request", return_value=fake_execution),
            patch.object(jy_wrapper, "package_lite_delivery", return_value=fake_package) as package,
        ):
            result = jy_wrapper.cmd_revision_run(
                "request.json",
                strict=True,
                package_zip="C:/Desktop/LiteDraft.zip",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["completion_boundary"], "lite_zip_delivery")
        self.assertEqual(result["data"]["delivery"], fake_package)
        package.assert_called_once()
        self.assertTrue(str(package.call_args.args[0]).endswith("LiteDraft"))
        self.assertTrue(
            str(package.call_args.kwargs["relink_tool"]).endswith(
                "tools\\relink_tool\\Auto-Cut剪映素材重链工具.exe"
            )
        )

    def test_revision_run_default_tool_produces_real_zip_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = self._draft(root)
            output = root / "Desktop" / "real-lite.zip"
            fake_request = SimpleNamespace(workflow_mode="lite")
            fake_execution = {"draft_path": str(draft), "validation": {"ok": True}}
            with (
                patch.object(jy_wrapper, "load_revision_request", return_value=fake_request),
                patch.object(jy_wrapper, "execute_revision_request", return_value=fake_execution),
            ):
                result = jy_wrapper.cmd_revision_run(
                    "request.json",
                    strict=True,
                    package_zip=str(output),
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["completion_boundary"], "lite_zip_delivery")
            self.assertTrue(output.is_file())
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                self.assertTrue(
                    any(name.endswith("/Auto-Cut剪映素材重链工具.exe") for name in names)
                )


if __name__ == "__main__":
    unittest.main()
