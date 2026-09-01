# ruff: noqa: E402,I001
import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from cli.jy_wrapper_parser import build_parser
from utils.lite_package import LitePackageError, package_lite_delivery
import jy_wrapper


@dataclass(frozen=True)
class _FakeProject:
    draft_name: str
    project_key: str = ""


@dataclass(frozen=True)
class _FakeRequest:
    workflow_mode: str
    project: _FakeProject


class LitePackageTests(unittest.TestCase):
    def _draft(self, root: Path) -> Path:
        draft = root / "必修下-第15课-2_从二月革命到十月革命_精简版_R41"
        (draft / "Resources" / "local").mkdir(parents=True)
        source_video = draft / "Resources" / "local" / "source-video.mp4"
        source_video.write_bytes(b"video-bytes")
        (draft / "draft_content.json").write_text(
            json.dumps(
                {
                    "materials": {
                        "videos": [{"id": "video-1", "path": str(source_video)}]
                    },
                    "tracks": [],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (draft / "draft_meta_info.json").write_text(
            json.dumps({"draft_name": draft.name}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return draft

    def test_package_is_byte_preserving_and_self_validating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = self._draft(root)
            tool = root / "relink-tool.exe"
            tool.write_bytes(b"relink-tool-bytes")
            output = root / "Desktop" / f"{draft.name}.zip"
            receipt = root / "receipts" / "final.json"
            source_content = (draft / "draft_content.json").read_bytes()

            result = package_lite_delivery(
                draft,
                output,
                relink_tool=tool,
                package_root_name=draft.name,
                receipt_json=receipt,
                name_resolution={
                    "requested_name": "外部名称",
                    "final_name": draft.name,
                    "source": "external_input",
                    "sanitized": True,
                },
                execution_input_digest="a" * 64,
            )

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["workflow_mode"], "lite")
            self.assertEqual(result["delivery_mode"], "lite_zip")
            self.assertEqual(result["localized_material_reference_count"], 1)
            self.assertTrue(result["relink_tool_included"])
            self.assertFalse(result["json_rewritten"])
            self.assertFalse(result["ui_invoked"])
            self.assertFalse(result["opened_jianying"])
            self.assertEqual(result["name_resolution"]["requested_name"], "外部名称")
            self.assertEqual(result["name_resolution"]["final_name"], draft.name)
            self.assertEqual(result["execution_input_digest"], "a" * 64)
            self.assertEqual(source_content, (draft / "draft_content.json").read_bytes())
            self.assertEqual(result["archive_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())

            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                root_name = draft.name
                self.assertIn(f"{root_name}/Auto-Cut剪映素材重链工具.exe", names)
                self.assertIn(f"{root_name}/使用说明.txt", names)
                self.assertIn(f"{root_name}/draft_content.json", names)
                self.assertIn(
                    f"{root_name}/Resources/local/source-video.mp4",
                    names,
                )
                self.assertIn("同目录", archive.read(f"{root_name}/使用说明.txt").decode("utf-8"))

            stored_receipt = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(stored_receipt["archive_sha256"], result["archive_sha256"])
            self.assertEqual(stored_receipt["package_tree_sha256"], result["package_tree_sha256"])
            self.assertEqual(stored_receipt["extracted_tree_sha256"], result["package_tree_sha256"])

    def test_package_refuses_overwrite_and_source_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = self._draft(root)
            output = root / f"{draft.name}.zip"
            package_lite_delivery(draft, output)
            with self.assertRaises(LitePackageError):
                package_lite_delivery(draft, output)
            with self.assertRaises(LitePackageError):
                package_lite_delivery(draft, draft / "nested.zip")
            with self.assertRaises(LitePackageError):
                package_lite_delivery(
                    draft,
                    root / f"{draft.name}.zip",
                    receipt_json=root / f"{draft.name}.zip",
                )

    def test_package_rejects_any_name_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = self._draft(root)
            with self.assertRaisesRegex(LitePackageError, "ZIP 文件名"):
                package_lite_delivery(draft, root / "different.zip")
            with self.assertRaisesRegex(LitePackageError, "ZIP 根目录名"):
                package_lite_delivery(
                    draft,
                    root / f"{draft.name}.zip",
                    package_root_name="different",
                )
            with self.assertRaisesRegex(LitePackageError, "name_resolution.final_name"):
                package_lite_delivery(
                    draft,
                    root / f"{draft.name}.zip",
                    name_resolution={
                        "requested_name": "different",
                        "final_name": "different",
                        "source": "external_input",
                        "sanitized": False,
                    },
                )
            with self.assertRaisesRegex(LitePackageError, "execution_input_digest"):
                package_lite_delivery(
                    draft,
                    root / f"{draft.name}.zip",
                    execution_input_digest="not-a-sha256",
                )

    def test_package_rejects_external_material_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = self._draft(root)
            external = root / "external.mp4"
            external.write_bytes(b"external-video")
            (draft / "draft_content.json").write_text(
                json.dumps(
                    {
                        "materials": {
                            "videos": [{"id": "external", "path": str(external)}]
                        },
                        "tracks": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(LitePackageError, "external material path"):
                package_lite_delivery(draft, root / f"{draft.name}.zip")

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
        fake_request = _FakeRequest("lite", _FakeProject("LiteDraft"))
        fake_execution = {
            "draft_name": "LiteDraft",
            "draft_path": "C:/drafts/LiteDraft",
            "validation": {"ok": True},
        }
        fake_package = {
            "status": "pass",
            "delivery_mode": "lite_zip",
            "archive_path": "C:/Desktop/LiteDraft.zip",
        }
        with (
            patch.object(jy_wrapper, "load_revision_request", return_value=fake_request),
            patch.object(
                jy_wrapper, "execute_revision_request", return_value=fake_execution
            ) as execute,
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
        self.assertTrue(execute.call_args.kwargs["localize_materials"])
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
            fake_request = _FakeRequest("lite", _FakeProject(draft.name))
            fake_execution = {
                "draft_name": draft.name,
                "draft_path": str(draft),
                "validation": {"ok": True},
            }
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
            actual_output = output.with_name(f"{draft.name}.zip")
            self.assertTrue(actual_output.is_file())
            with zipfile.ZipFile(actual_output) as archive:
                names = set(archive.namelist())
                self.assertTrue(
                    any(name.endswith("/Auto-Cut剪映素材重链工具.exe") for name in names)
                )

    def test_revision_run_uses_actual_fallback_name_for_every_package_boundary(self) -> None:
        fake_request = _FakeRequest("lite", _FakeProject("Requested Name"))
        actual_name = "Requested Name__fallback_20260901_abcdef"
        fake_execution = {
            "draft_name": actual_name,
            "draft_path": f"C:/drafts/{actual_name}",
            "validation": {"ok": True},
        }
        fake_package = {"status": "pass", "archive_path": f"C:/Desktop/{actual_name}.zip"}
        with (
            patch.object(jy_wrapper, "load_revision_request", return_value=fake_request),
            patch.object(jy_wrapper, "execute_revision_request", return_value=fake_execution),
            patch.object(jy_wrapper, "package_lite_delivery", return_value=fake_package) as package,
        ):
            result = jy_wrapper.cmd_revision_run(
                "request.json",
                package_zip="C:/Desktop/placeholder.zip",
                relink_tool="C:/tools/relink.exe",
            )

        self.assertTrue(result["ok"])
        resolution = result["data"]["name_resolution"]
        self.assertEqual(resolution["requested_name"], "Requested Name")
        self.assertEqual(resolution["pre_fallback_name"], "Requested Name")
        self.assertEqual(resolution["final_name"], actual_name)
        self.assertTrue(resolution["draft_fallback_applied"])
        self.assertEqual(Path(package.call_args.args[1]).name, f"{actual_name}.zip")
        self.assertEqual(package.call_args.kwargs["package_root_name"], actual_name)

    def test_revision_run_applies_mode_override_before_external_name_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            execution_input = Path(temporary) / "execution-input.json"
            execution_input.write_text(
                json.dumps({"schema_version": 1, "artifact_name": "External:Name"}),
                encoding="utf-8",
            )
            fake_request = _FakeRequest("full", _FakeProject("Original Name"))
            fake_execution = {
                "draft_name": "External_Name",
                "draft_path": "C:/drafts/External_Name",
                "validation": {"ok": True},
            }
            with (
                patch.object(jy_wrapper, "load_revision_request", return_value=fake_request),
                patch.object(
                    jy_wrapper,
                    "execute_revision_request",
                    return_value=fake_execution,
                ) as execute,
            ):
                result = jy_wrapper.cmd_revision_run(
                    "request.json",
                    workflow_mode="lite",
                    execution_input_json=str(execution_input),
                )

            self.assertTrue(result["ok"])
            self.assertEqual(
                execute.call_args.args[0].project.draft_name,
                "External_Name",
            )
            self.assertEqual(result["data"]["name_resolution"]["final_name"], "External_Name")

            with patch.object(jy_wrapper, "load_revision_request", return_value=fake_request):
                with self.assertRaisesRegex(Exception, "only available for workflow_mode=lite"):
                    jy_wrapper.cmd_revision_run(
                        "request.json",
                        execution_input_json=str(execution_input),
                    )


if __name__ == "__main__":
    unittest.main()
