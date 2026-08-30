# ruff: noqa: E402
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CURRENT_DIR)
SCRIPTS_PATH = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, SCRIPTS_PATH)

from utils import review_document_intake as intake


class FakeLarkRunner:
    def __init__(
        self,
        fetch_payload: dict | None = None,
        *,
        asset_bytes: bytes | None = None,
    ) -> None:
        self.fetch_payload = fetch_payload or {}
        self.asset_bytes = asset_bytes
        self.commands: list[list[str]] = []

    def __call__(self, command):
        row = [str(value) for value in command]
        self.commands.append(row)
        if row[-1:] == ["--version"]:
            return subprocess.CompletedProcess(row, 0, "lark-cli version 1.2.3\n", "")
        if row[1:] == ["whoami"]:
            return subprocess.CompletedProcess(
                row,
                0,
                json.dumps(
                    {
                        "available": True,
                        "defaultAs": "user",
                        "identity": "user",
                        "profile": "operator",
                        "tokenStatus": "valid",
                        "onBehalfOf": {"openId": "ou_private", "userName": "reviewer"},
                    }
                ),
                "",
            )
        if row[1:3] == ["docs", "+fetch"]:
            return subprocess.CompletedProcess(row, 0, json.dumps(self.fetch_payload), "")
        if row[1:3] == ["docs", "+media-download"]:
            target = Path(row[row.index("--output") + 1])
            token = row[row.index("--token") + 1]
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = (
                self.asset_bytes
                if self.asset_bytes is not None
                else f"asset:{token}".encode("utf-8")
            )
            target.write_bytes(payload)
            return subprocess.CompletedProcess(row, 0, json.dumps({"ok": True}), "")
        return subprocess.CompletedProcess(row, 1, "", "unsupported")


def _fetch_payload(document_id: str = "doxcn_private_token", revision: int = 7) -> dict:
    content = "".join(
        [
            '<title id="title_private">Lesson title</title>',
            '<figure id="reference"><source token="video_reference_token" '
            'name="reference.mp4" mime="video/mp4"/></figure>',
            '<figure id="source"><source token="video_source_token" '
            'name="lesson-录屏.mp4" mime="video/mp4"/></figure>',
            '<checkbox id="block_delete">00:01 删除“<span text-color="#245BDB">测试</span>”</checkbox>',
            '<checkbox id="block_pointer">00:02 添加小手指向此处</checkbox>',
            '<img id="pointer" src="pointer_private_token" name="小手.png" mime="image/png"/>',
        ]
    )
    return {
        "ok": True,
        "identity": "user",
        "data": {
            "document": {
                "document_id": document_id,
                "revision_id": revision,
                "content": content,
            }
        },
    }


def _single_asset_parsed(
    *,
    token: str = "raw_private_asset_token",
    expected_size: int | None = None,
) -> dict:
    return {
        "document_identity_sha256": "a" * 64,
        "revision_id": 1,
        "content_sha256": "b" * 64,
        "asset_identity_sha256": "c" * 64,
        "assets": [
            {
                "asset_id": "asset_safe",
                "token": token,
                "extension": ".bin",
                "mime": "application/octet-stream",
                "name": "material.bin",
                "expected_size": expected_size,
                "associated_item_index": None,
            }
        ],
    }


class ReviewDocumentIntakeTests(unittest.TestCase):
    maxDiff = None

    def test_url_mode_uses_fixed_user_identity_commands(self) -> None:
        runner = FakeLarkRunner(_fetch_payload())
        url = "https://example.feishu.cn/wiki/wiki_private_token"

        self.assertEqual(
            intake.lark_cli_version(lark_cli=sys.executable, runner=runner),
            "1.2.3",
        )
        whoami = intake.lark_whoami(lark_cli=sys.executable, runner=runner)
        fetched = intake.fetch_lark_document(url, lark_cli=sys.executable, runner=runner)

        self.assertEqual(whoami["identity"], "user")
        self.assertEqual(fetched["revision_id"], 7)
        fetch_command = runner.commands[2]
        self.assertEqual(
            fetch_command[1:],
            [
                "docs",
                "+fetch",
                "--doc",
                url,
                "--scope",
                "full",
                "--detail",
                "full",
                "--doc-format",
                "xml",
                "--format",
                "json",
                "--as",
                "user",
            ],
        )

    def test_parse_download_and_compile_preserve_text_without_provider_secrets(self) -> None:
        runner = FakeLarkRunner(_fetch_payload())
        fetched = intake.fetch_lark_document(
            "https://example.feishu.cn/docx/doc_private_token",
            lark_cli=sys.executable,
            runner=runner,
        )
        parsed = intake.parse_lark_document(fetched)

        self.assertEqual(parsed["review_items"][0]["source_text"], '00:01 删除“测试”')
        self.assertEqual(
            parsed["review_items"][0]["colored_spans"],
            [{"text": "测试", "color": "#245BDB"}],
        )
        serialized_identity = json.dumps(parsed["safe_asset_identity"], ensure_ascii=False)
        self.assertNotIn("private_token", serialized_identity)

        with tempfile.TemporaryDirectory() as temporary:
            downloaded = intake.download_lark_assets(
                parsed,
                Path(temporary) / "assets",
                lark_cli=sys.executable,
                runner=runner,
            )
            compiled = intake.compile_url_inputs(parsed, downloaded)
            output = Path(temporary) / "compiled.json"
            output.write_text(json.dumps(compiled, ensure_ascii=False), encoding="utf-8")
            serialized = output.read_text(encoding="utf-8")

        self.assertEqual(compiled["project"]["workflow_mode"], "lite")
        self.assertTrue(compiled["project"]["source_video"].endswith(".mp4"))
        self.assertEqual(
            compiled["snapshot"]["review_items"][1]["asset_paths"],
            [next(row["path"] for row in downloaded if row["extension"] == ".png")],
        )
        self.assertNotIn("doc_private_token", serialized)
        self.assertNotIn("doxcn_private_token", serialized)
        self.assertNotIn("video_source_token", serialized)
        self.assertNotIn("pointer_private_token", serialized)
        media_commands = [row for row in runner.commands if row[1:3] == ["docs", "+media-download"]]
        self.assertEqual(len(media_commands), 3)
        self.assertTrue(all(row[-2:] == ["--as", "user"] for row in media_commands))

    def test_multiple_unlabelled_videos_require_structured_selection(self) -> None:
        parsed = {
            "document_identity_sha256": "a" * 64,
            "revision_id": 1,
            "content_sha256": "b" * 64,
            "asset_identity_sha256": "c" * 64,
            "review_items": [{"block_id": "block_1", "source_text": "00:01 校对"}],
        }
        assets = [
            {
                "asset_id": "asset_one",
                "path": "one.mp4",
                "relative_path": "asset_one.mp4",
                "sha256": "1" * 64,
                "byte_size": 10,
                "mime": "video/mp4",
                "extension": ".mp4",
                "name": "first.mp4",
            },
            {
                "asset_id": "asset_two",
                "path": "two.mp4",
                "relative_path": "asset_two.mp4",
                "sha256": "2" * 64,
                "byte_size": 20,
                "mime": "video/mp4",
                "extension": ".mp4",
                "name": "second.mp4",
            },
        ]

        with self.assertRaises(intake.ReviewDocumentIntakeError) as raised:
            intake.compile_url_inputs(parsed, assets)

        error = raised.exception
        self.assertEqual(error.code, "source_video_ambiguous")
        self.assertEqual(error.user_action["action_code"], "high_risk_confirmation")
        self.assertEqual(error.user_action["candidate_ids"], ["asset_one", "asset_two"])
        serialized = json.dumps(error.public_data(), ensure_ascii=False)
        self.assertNotIn("first.mp4", serialized)
        self.assertNotIn("second.mp4", serialized)

    def test_document_url_digest_isolated_and_never_returns_the_url(self) -> None:
        first = "https://example.feishu.cn/docx/doc_alpha"
        second = "https://example.feishu.cn/docx/doc_beta"
        first_digest = intake.document_url_digest(first)
        second_digest = intake.document_url_digest(second)

        self.assertEqual(len(first_digest), 64)
        self.assertNotEqual(first_digest, second_digest)
        self.assertNotIn("doc_alpha", first_digest)
        with self.assertRaises(intake.ReviewDocumentIntakeError):
            intake.document_url_digest("https://example.feishu.cn/sheets/sheet_token")

    def test_document_url_rejects_lookalike_hosts_userinfo_controls_and_ports(self) -> None:
        invalid = [
            "https://feishu.cn.evil.example/docx/token",
            "https://operator@example.feishu.cn/docx/token",
            "https://example.feishu.cn:444/docx/token",
            "http://example.feishu.cn/docx/token",
            "https://example.feishu.cn/docx/token\nignored",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(intake.ReviewDocumentIntakeError):
                    intake.validate_document_url(value)

    def test_xml_preserves_exact_checkbox_whitespace_and_enforces_depth_limit(self) -> None:
        exact = "  00:01  删除这一句\n第二行保留  "
        parsed = intake.parse_lark_document(
            {
                "document_id": "doc-exact",
                "revision_id": 1,
                "content": f"<checkbox>{exact}</checkbox>",
            }
        )
        self.assertEqual(parsed["review_items"][0]["source_text"], exact)

        too_deep = "<node>" * 70 + "<checkbox>00:01 校对</checkbox>" + "</node>" * 70
        with self.assertRaises(intake.ReviewDocumentIntakeError) as raised:
            intake.parse_lark_document(
                {"document_id": "doc-deep", "revision_id": 1, "content": too_deep}
            )
        self.assertEqual(raised.exception.code, "document_xml_too_complex")

    def test_corrupt_download_cache_is_redownloaded_before_publish(self) -> None:
        runner = FakeLarkRunner(_fetch_payload())
        parsed = intake.parse_lark_document(
            intake.fetch_lark_document(
                "https://example.feishu.cn/docx/cache_test",
                lark_cli=sys.executable,
                runner=runner,
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "assets"
            first = intake.download_lark_assets(
                parsed, output, lark_cli=sys.executable, runner=runner
            )
            target = Path(first[0]["path"])
            target.write_bytes(b"corrupt")
            second = intake.download_lark_assets(
                parsed, output, lark_cli=sys.executable, runner=runner
            )

        downloads = [
            command for command in runner.commands if command[1:3] == ["docs", "+media-download"]
        ]
        self.assertEqual(len(downloads), 4)
        self.assertFalse(second[0]["cache_hit"])
        self.assertEqual(second[0]["sha256"], first[0]["sha256"])

    def test_cache_digest_binds_document_revision_content_and_asset_identity(self) -> None:
        runner = FakeLarkRunner(asset_bytes=b"revision-one")
        parsed = _single_asset_parsed()

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "assets"
            intake.download_lark_assets(
                parsed, output, lark_cli=sys.executable, runner=runner
            )
            first_receipt = json.loads(
                (output / ".receipts" / "asset_safe.json").read_text(encoding="utf-8")
            )
            runner.asset_bytes = b"revision-two"
            revised = dict(parsed)
            revised.update(
                {
                    "revision_id": 2,
                    "content_sha256": "d" * 64,
                    "asset_identity_sha256": "e" * 64,
                }
            )
            second = intake.download_lark_assets(
                revised, output, lark_cli=sys.executable, runner=runner
            )
            second_receipt = json.loads(
                (output / ".receipts" / "asset_safe.json").read_text(encoding="utf-8")
            )
            downloaded_bytes = Path(second[0]["path"]).read_bytes()

        downloads = [
            command for command in runner.commands if command[1:3] == ["docs", "+media-download"]
        ]
        self.assertEqual(len(downloads), 2)
        self.assertFalse(second[0]["cache_hit"])
        self.assertEqual(downloaded_bytes, b"revision-two")
        self.assertNotEqual(first_receipt["input_digest"], second_receipt["input_digest"])

    def test_expected_size_change_rejects_cache_hit_and_receipt_stays_sanitized(self) -> None:
        token = "raw_private_asset_token"
        runner = FakeLarkRunner(asset_bytes=b"first")
        parsed = _single_asset_parsed(token=token, expected_size=5)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "assets"
            intake.download_lark_assets(
                parsed, output, lark_cli=sys.executable, runner=runner
            )
            first_receipt = json.loads(
                (output / ".receipts" / "asset_safe.json").read_text(encoding="utf-8")
            )
            runner.asset_bytes = b"second"
            resized = dict(parsed)
            resized["assets"] = [dict(parsed["assets"][0], expected_size=6)]
            second = intake.download_lark_assets(
                resized, output, lark_cli=sys.executable, runner=runner
            )
            receipt_text = (output / ".receipts" / "asset_safe.json").read_text(
                encoding="utf-8"
            )
            second_receipt = json.loads(receipt_text)
            downloaded_bytes = Path(second[0]["path"]).read_bytes()

        downloads = [
            command for command in runner.commands if command[1:3] == ["docs", "+media-download"]
        ]
        self.assertEqual(len(downloads), 2)
        self.assertFalse(second[0]["cache_hit"])
        self.assertEqual(downloaded_bytes, b"second")
        self.assertNotEqual(first_receipt["input_digest"], second_receipt["input_digest"])
        self.assertNotIn(token, receipt_text)

    def test_actual_asset_size_limit_is_structured_and_removes_partial_file(self) -> None:
        token = "oversized_private_token"
        runner = FakeLarkRunner(asset_bytes=b"12345")
        parsed = _single_asset_parsed(token=token)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "assets"
            with mock.patch.object(intake, "_MAX_ASSET_DOWNLOAD_BYTES", 4):
                with self.assertRaises(intake.ReviewDocumentIntakeError) as raised:
                    intake.download_lark_assets(
                        parsed, output, lark_cli=sys.executable, runner=runner
                    )
            self.assertFalse((output / "asset_safe.bin").exists())
            self.assertEqual(list(output.glob("*.part*")), [])

        error = raised.exception
        self.assertEqual(error.code, "asset_download_size_limit_exceeded")
        self.assertEqual(error.details["byte_size"], 5)
        self.assertNotIn(token, json.dumps(error.public_data(), ensure_ascii=False))

    def test_actual_aggregate_size_limit_is_structured_and_removes_partial_file(self) -> None:
        first_token = "first_private_token"
        second_token = "second_private_token"
        parsed = _single_asset_parsed(token=first_token)
        parsed["assets"].append(
            {
                **parsed["assets"][0],
                "asset_id": "asset_second",
                "token": second_token,
            }
        )
        runner = FakeLarkRunner(asset_bytes=b"1234")

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "assets"
            with mock.patch.object(intake, "_MAX_TOTAL_ASSET_DOWNLOAD_BYTES", 7):
                with self.assertRaises(intake.ReviewDocumentIntakeError) as raised:
                    intake.download_lark_assets(
                        parsed, output, lark_cli=sys.executable, runner=runner
                    )
            self.assertTrue((output / "asset_safe.bin").is_file())
            self.assertFalse((output / "asset_second.bin").exists())
            self.assertEqual(list(output.glob("*.part*")), [])

        error = raised.exception
        serialized = json.dumps(error.public_data(), ensure_ascii=False)
        self.assertEqual(error.code, "asset_download_total_limit_exceeded")
        self.assertEqual(error.details["aggregate_byte_size"], 8)
        self.assertNotIn(first_token, serialized)
        self.assertNotIn(second_token, serialized)

    def test_pointer_material_reuses_unique_same_name_attachment(self) -> None:
        parsed = {
            "document_identity_sha256": "a" * 64,
            "revision_id": 1,
            "content_sha256": "b" * 64,
            "asset_identity_sha256": "c" * 64,
            "review_items": [
                {"block_id": "one", "source_text": "00:01 添加小手指向标题"},
                {"block_id": "two", "source_text": "00:02 添加小手指向标题"},
            ],
        }
        assets = [
            {
                "asset_id": "source",
                "path": "source.mp4",
                "relative_path": "source.mp4",
                "sha256": "1" * 64,
                "byte_size": 10,
                "mime": "video/mp4",
                "extension": ".mp4",
                "name": "源视频.mp4",
            },
            {
                "asset_id": "hand",
                "path": "hand.png",
                "relative_path": "hand.png",
                "sha256": "2" * 64,
                "byte_size": 20,
                "mime": "image/png",
                "extension": ".png",
                "name": "小手.png",
                "associated_item_index": 0,
            },
        ]

        compiled = intake.compile_url_inputs(parsed, assets)
        rows = compiled["snapshot"]["review_items"]
        self.assertEqual(rows[0]["asset_paths"], ["hand.png"])
        self.assertEqual(rows[1]["asset_paths"], ["hand.png"])

    def test_recommended_pointer_candidate_is_selected_without_user_action(self) -> None:
        parsed = {
            "document_identity_sha256": "a" * 64,
            "revision_id": 1,
            "content_sha256": "b" * 64,
            "asset_identity_sha256": "c" * 64,
            "review_items": [
                {"block_id": "pointer", "source_text": "00:01 添加小手指向丹麦"}
            ],
        }
        assets = [
            {
                "asset_id": "source",
                "path": "source.mp4",
                "relative_path": "source.mp4",
                "sha256": "1" * 64,
                "byte_size": 10,
                "mime": "video/mp4",
                "extension": ".mp4",
                "name": "源视频.mp4",
            },
            {
                "asset_id": "screenshot",
                "path": "screenshot.png",
                "relative_path": "screenshot.png",
                "sha256": "2" * 64,
                "byte_size": 20,
                "mime": "image/png",
                "extension": ".png",
                "name": "attachment.png",
                "context_text": "候选 1：完整画面截图",
                "associated_item_index": 0,
            },
            {
                "asset_id": "hand",
                "path": "hand.png",
                "relative_path": "hand.png",
                "sha256": "3" * 64,
                "byte_size": 20,
                "mime": "image/png",
                "extension": ".png",
                "name": "attachment-2.png",
                "context_text": "候选 2：小手素材，建议选择此项",
                "associated_item_index": 0,
            },
        ]

        compiled = intake.compile_url_inputs(parsed, assets)
        row = compiled["snapshot"]["review_items"][0]
        self.assertEqual(row["asset_paths"], ["hand.png"])
        self.assertEqual(row["kind"], "pointer_overlay")

    def test_byte_identical_pointer_candidates_are_not_ambiguous(self) -> None:
        parsed = {
            "document_identity_sha256": "a" * 64,
            "revision_id": 1,
            "content_sha256": "b" * 64,
            "asset_identity_sha256": "c" * 64,
            "review_items": [
                {"block_id": "pointer", "source_text": "00:01 添加小手指向丹麦"}
            ],
        }
        assets = [
            {
                "asset_id": "source",
                "path": "source.mp4",
                "relative_path": "source.mp4",
                "sha256": "1" * 64,
                "byte_size": 10,
                "mime": "video/mp4",
                "extension": ".mp4",
                "name": "源视频.mp4",
            },
            *[
                {
                    "asset_id": f"hand-{index}",
                    "path": f"hand-{index}.png",
                    "relative_path": f"hand-{index}.png",
                    "sha256": "2" * 64,
                    "byte_size": 20,
                    "mime": "image/png",
                    "extension": ".png",
                    "name": f"attachment-{index}.png",
                    "associated_item_index": 0,
                }
                for index in (1, 2)
            ],
        ]

        compiled = intake.compile_url_inputs(parsed, assets)
        self.assertEqual(
            compiled["snapshot"]["review_items"][0]["asset_paths"], ["hand-1.png"]
        )

    def test_recommended_candidate_number_selects_ordered_pointer_material(self) -> None:
        parsed = {
            "document_identity_sha256": "a" * 64,
            "revision_id": 1,
            "content_sha256": "b" * 64,
            "asset_identity_sha256": "c" * 64,
            "review_items": [
                {"block_id": "pointer", "source_text": "00:01 添加小手指向丹麦"}
            ],
        }
        assets = [
            {
                "asset_id": "source",
                "path": "source.mp4",
                "relative_path": "source.mp4",
                "sha256": "1" * 64,
                "byte_size": 10,
                "mime": "video/mp4",
                "extension": ".mp4",
                "name": "源视频.mp4",
            },
            *[
                {
                    "asset_id": f"candidate-{index}",
                    "path": f"candidate-{index}.png",
                    "relative_path": f"candidate-{index}.png",
                    "sha256": str(index + 1) * 64,
                    "byte_size": 20,
                    "mime": "image/png",
                    "extension": ".png",
                    "name": "attachment.png",
                    "context_text": (
                        "候选 1：完整画面截图；候选 2：小手素材，建议选择此项"
                    ),
                    "associated_item_index": 0,
                }
                for index in (1, 2)
            ],
        ]

        compiled = intake.compile_url_inputs(parsed, assets)
        self.assertEqual(
            compiled["snapshot"]["review_items"][0]["asset_paths"], ["candidate-2.png"]
        )

    def test_pointer_recommendation_context_associates_caption_group(self) -> None:
        parsed = intake.parse_lark_document(
            {
                "document_id": "doc-pointer-caption",
                "revision_id": 1,
                "content": (
                    '<checkbox id="pointer">00:01 添加小手指向丹麦</checkbox>'
                    "<p>候选 1：完整画面截图</p>"
                    '<p><img src="screenshot-token" name="attachment.png" mime="image/png"/></p>'
                    "<p>候选 2：小手素材，建议选择此项</p>"
                    '<p><img src="hand-token" name="attachment-2.png" mime="image/png"/></p>'
                ),
            }
        )
        self.assertEqual(len(parsed["assets"]), 2)
        self.assertIsNone(parsed["assets"][0]["associated_item_index"])
        self.assertEqual(parsed["assets"][1]["associated_item_index"], 0)
        self.assertTrue(parsed["assets"][1]["recommended"])

    def test_structural_visual_ambiguity_requires_user_action(self) -> None:
        parsed = {
            "document_identity_sha256": "a" * 64,
            "revision_id": 1,
            "content_sha256": "b" * 64,
            "asset_identity_sha256": "c" * 64,
            "review_items": [
                {"block_id": "formula", "source_text": "00:01 用图片替换公式"}
            ],
        }
        assets = [
            {
                "asset_id": "source",
                "path": "source.mp4",
                "relative_path": "source.mp4",
                "sha256": "1" * 64,
                "byte_size": 10,
                "mime": "video/mp4",
                "extension": ".mp4",
                "name": "源视频.mp4",
            },
            *[
                {
                    "asset_id": f"image-{index}",
                    "path": f"image-{index}.png",
                    "relative_path": f"image-{index}.png",
                    "sha256": str(index) * 64,
                    "byte_size": 20,
                    "mime": "image/png",
                    "extension": ".png",
                    "name": f"formula-{index}.png",
                    "associated_item_index": 0,
                }
                for index in (2, 3)
            ],
        ]
        with self.assertRaises(intake.ReviewDocumentIntakeError) as raised:
            intake.compile_url_inputs(parsed, assets)
        self.assertEqual(raised.exception.code, "visual_asset_ambiguous")
        self.assertEqual(
            raised.exception.user_action["candidate_ids"], ["image-2", "image-3"]
        )

    def test_readiness_persists_hashes_and_invalidates_version_changes(self) -> None:
        whoami = {
            "available": True,
            "defaultAs": "user",
            "identity": "user",
            "profile": "operator",
            "tokenStatus": "private_refresh_token",
            "onBehalfOf": {"openId": "ou_private", "userName": "reviewer"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            readiness = Path(temporary) / "runtime-readiness.json"
            first = intake.mark_lark_verified(
                whoami,
                path=readiness,
                runtime_version="1.6.0",
                lark_version="1.2.3",
                asr_adapter_version="asr-v1",
            )
            intake.mark_asr_verified(
                provider="volc",
                model_or_resource="big-asr",
                adapter_version="asr-v1",
                path=readiness,
            )
            verified = json.loads(readiness.read_text(encoding="utf-8"))
            changed = intake.evaluate_runtime_readiness(
                path=readiness,
                runtime_version="1.6.1",
                lark_version="1.2.3",
                asr_adapter_version="asr-v1",
            )
            serialized = readiness.read_text(encoding="utf-8")

        self.assertEqual(first["lark"]["status"], "verified")
        self.assertEqual(verified["asr"]["status"], "verified")
        self.assertEqual(changed["lark"]["status"], "pending_validation")
        self.assertEqual(changed["asr"]["status"], "pending_validation")
        self.assertNotIn("private_refresh_token", serialized)
        self.assertNotIn("ou_private", serialized)
        self.assertNotIn("reviewer", serialized)

    def test_old_asr_adapter_cannot_overwrite_current_pending_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            readiness = Path(temporary) / "runtime-readiness.json"
            intake.evaluate_runtime_readiness(
                path=readiness,
                runtime_version="1.6.0",
                lark_version="1.2.3",
                asr_adapter_version="asr-v2",
            )
            result = intake.mark_asr_verified(
                provider="volc",
                model_or_resource="big-asr",
                adapter_version="asr-v1",
                path=readiness,
            )
        self.assertEqual(result["asr"]["status"], "pending_validation")
        self.assertEqual(result["asr"]["reason_code"], "asr_adapter_identity_mismatch")

    def test_provider_failures_do_not_echo_urls_tokens_or_stderr(self) -> None:
        sentinel_url = "https://example.feishu.cn/docx/private_url_token"

        def failing(command):
            return subprocess.CompletedProcess(command, 7, "", f"denied {sentinel_url}")

        with self.assertRaises(intake.ReviewDocumentIntakeError) as raised:
            intake.fetch_lark_document(sentinel_url, lark_cli=sys.executable, runner=failing)

        serialized = json.dumps(raised.exception.public_data(), ensure_ascii=False)
        self.assertNotIn(sentinel_url, serialized)
        self.assertNotIn("private_url_token", serialized)
        self.assertEqual(raised.exception.details["provider_exit_code"], 7)

    def test_windows_command_shim_is_replaced_by_node_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = root / "lark-cli.cmd"
            node = root / ("node.exe" if os.name == "nt" else "node")
            script = root / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
            shim.write_text("@echo off\nnode run.js %*\n", encoding="ascii")
            node.write_bytes(b"not-executed")
            script.parent.mkdir(parents=True)
            script.write_text("// not executed\n", encoding="ascii")

            prefix = intake._lark_command_prefix(shim)

        self.assertEqual(prefix, (str(node.resolve()), str(script.resolve())))
        self.assertFalse(any(value.casefold().endswith((".cmd", ".bat", ".ps1")) for value in prefix))


if __name__ == "__main__":
    unittest.main()
