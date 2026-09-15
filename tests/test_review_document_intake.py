# ruff: noqa: E402
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CURRENT_DIR)
SCRIPTS_PATH = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, SCRIPTS_PATH)

from utils import atomic_io
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

    def test_document_title_is_used_for_default_lite_name(self) -> None:
        fetched = intake.fetch_lark_document(
            "https://example.feishu.cn/docx/doc_private_token",
            lark_cli=sys.executable,
            runner=FakeLarkRunner(_fetch_payload()),
        )
        parsed = intake.parse_lark_document(fetched)
        self.assertEqual(parsed["document_title"], "Lesson title")
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
            }
        ]
        compiled = intake.compile_url_inputs(parsed, assets)
        self.assertEqual(compiled["project"]["draft_name"], "Lesson title")
        self.assertEqual(compiled["project"]["name_source"], "document_title")

    def test_external_name_wins_and_is_safely_normalized(self) -> None:
        parsed = {
            "document_identity_sha256": "a" * 64,
            "document_title": "Document title",
            "review_items": [{"block_id": "one", "source_text": "00:01 校对"}],
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
            }
        ]
        compiled = intake.compile_url_inputs(parsed, assets, external_name="  Name:One  ")
        self.assertEqual(compiled["project"]["draft_name"], "Name_One")
        self.assertEqual(compiled["project"]["name_source"], "external_input")
        self.assertTrue(compiled["project"]["name_sanitized"])

    def test_missing_title_uses_document_identity_prefix(self) -> None:
        parsed = {
            "document_identity_sha256": "a" * 64,
            "document_title": "",
            "review_items": [{"block_id": "one", "source_text": "00:01 校对"}],
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
            }
        ]

        compiled = intake.compile_url_inputs(parsed, assets)

        self.assertEqual(compiled["project"]["draft_name"], "AutoCutLite-aaaaaaaaaaaa")
        self.assertEqual(compiled["project"]["name_source"], "identity_fallback")

    def test_multiple_unlabelled_videos_use_deterministic_recommendation(self) -> None:
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

        compiled = intake.compile_url_inputs(parsed, assets)

        self.assertEqual(compiled["project"]["source_video"], "two.mp4")
        roles = {
            row["asset_id"]: row["role"]
            for row in compiled["asset_manifest"]["assets"]
        }
        self.assertEqual(roles["asset_two"], "source_video")
        self.assertEqual(roles["asset_one"], "document_attachment")

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

    def test_structural_visual_candidates_use_automatic_recommendation(self) -> None:
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
        compiled = intake.compile_url_inputs(parsed, assets)
        row = compiled["snapshot"]["review_items"][0]
        self.assertEqual(row["asset_paths"], ["image-2.png"])
        self.assertEqual(row["kind"], "visual_overlay")
        self.assertEqual(
            row["asset_selection"]["policy"],
            "automatic_recommended_description_visual_features",
        )
        self.assertEqual(row["asset_selection"]["candidate_count"], 2)
        self.assertEqual(row["asset_selection"]["selected_asset_id"], "image-2")

    def test_image_animation_reference_is_not_imported_as_project_material(self) -> None:
        parsed = {
            "document_identity_sha256": "a" * 64,
            "revision_id": 1,
            "content_sha256": "b" * 64,
            "asset_identity_sha256": "c" * 64,
            "review_items": [
                {
                    "block_id": "animation",
                    "source_text": "02:06，下方图片的动画，推迟到02:07",
                }
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
                "asset_id": "reference",
                "path": "reference.png",
                "relative_path": "reference.png",
                "sha256": "2" * 64,
                "byte_size": 20,
                "mime": "image/png",
                "extension": ".png",
                "name": "参考图.png",
                "associated_item_index": 0,
            },
        ]

        compiled = intake.compile_url_inputs(parsed, assets)
        row = compiled["snapshot"]["review_items"][0]
        manifest = {item["asset_id"]: item for item in compiled["asset_manifest"]["assets"]}

        self.assertNotIn("asset_paths", row)
        self.assertEqual(manifest["reference"]["role"], "document_attachment")

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


class BoundedReadinessAndCliTests(unittest.TestCase):
    def test_inaccessible_cli_is_sanitized_without_attempting_execution(self):
        private_path = "C:/Users/private/npm/lark-cli.cmd"
        with (
            mock.patch.object(
                intake, "_lark_command_prefix", side_effect=PermissionError(private_path)
            ),
            mock.patch.object(intake, "_default_command_runner") as runner,
            self.assertRaises(intake.ReviewDocumentIntakeError) as raised,
        ):
            intake.lark_cli_version(lark_cli=private_path)
        self.assertEqual(raised.exception.code, "lark_cli_unavailable")
        self.assertNotIn(private_path, json.dumps(raised.exception.public_data()))
        runner.assert_not_called()

    def test_cli_spawn_permission_denial_retains_cause_and_safe_error(self):
        denial = PermissionError("C:/Users/private/npm/lark-cli.cmd")
        with self.assertRaises(intake.ReviewDocumentIntakeError) as raised:
            intake.lark_cli_version(lark_cli=sys.executable, runner=mock.Mock(side_effect=denial))
        self.assertIs(raised.exception.__cause__, denial)
        self.assertEqual(raised.exception.code, "lark_cli_unavailable")
        self.assertEqual(raised.exception.details["error_type"], "PermissionError")
        self.assertNotIn("private", json.dumps(raised.exception.public_data()))

    def test_cli_process_timeout_reaps_a_real_stalled_command(self):
        started = time.monotonic()
        with (
            mock.patch.object(
                intake,
                "_lark_executable",
                return_value=(sys.executable, "-c", "import time; time.sleep(30)"),
            ),
            self.assertRaises(intake.ReviewDocumentIntakeError) as raised,
        ):
            intake.lark_cli_version(timeout_seconds=0.15)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(raised.exception.code, "lark_cli_unavailable")
        self.assertEqual(raised.exception.details["error_type"], "TimeoutExpired")
        self.assertEqual(raised.exception.details["timeout_seconds"], 0.15)

    def test_version_and_identity_commands_receive_finite_default_deadlines(self):
        whoami = json.dumps({"available": True, "identity": "user", "defaultAs": "user"})
        with mock.patch.object(
            intake,
            "_default_command_runner",
            side_effect=[
                subprocess.CompletedProcess([], 0, "lark-cli version 1.2.3", ""),
                subprocess.CompletedProcess([], 0, whoami, ""),
            ],
        ) as runner:
            self.assertEqual(intake.lark_cli_version(lark_cli=sys.executable), "1.2.3")
            self.assertEqual(intake.lark_whoami(lark_cli=sys.executable)["identity"], "user")
        self.assertEqual(
            [call.kwargs["timeout_seconds"] for call in runner.call_args_list],
            [intake.LARK_PREFLIGHT_COMMAND_TIMEOUT_SECONDS] * 2,
        )

    def test_file_capture_preserves_normal_stdout_stderr_and_return_code(self):
        completed = intake._default_command_runner(
            [
                sys.executable,
                "-c",
                "import sys; print('lark-cli version 1.2.3'); print('diagnostic', file=sys.stderr); sys.exit(7)",
            ],
            timeout_seconds=2.0,
        )
        self.assertEqual(completed.returncode, 7)
        self.assertEqual(completed.stdout.strip(), "lark-cli version 1.2.3")
        self.assertEqual(completed.stderr.strip(), "diagnostic")

    def test_inherited_output_handles_do_not_extend_timeout_and_child_is_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child-pid"
            code = (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "print('inherited output', flush=True); time.sleep(30)"
            )
            started = time.monotonic()
            try:
                with self.assertRaises(subprocess.TimeoutExpired) as raised:
                    intake._default_command_runner(
                        [sys.executable, "-c", code, str(pid_file)], timeout_seconds=0.3
                    )
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertTrue(pid_file.exists(), "the child must inherit output before timeout")
                child_pid = int(pid_file.read_text())
                try:
                    child = intake.psutil.Process(child_pid)
                    self.assertEqual(child.status(), intake.psutil.STATUS_ZOMBIE)
                except intake.psutil.NoSuchProcess:
                    pass
                self.assertIn(raised.exception.cleanup_status, {"terminated", "incomplete"})
            finally:
                if pid_file.exists():
                    try:
                        intake.psutil.Process(int(pid_file.read_text())).kill()
                    except intake.psutil.NoSuchProcess:
                        pass

    def test_command_capture_permission_error_has_one_attempt_and_never_spawns(self):
        capture_directory = tempfile.gettempdir()
        with (
            mock.patch.object(intake.tempfile, "gettempdir", return_value=capture_directory),
            mock.patch.object(
                atomic_io.os, "open", side_effect=PermissionError("private-path")
            ) as opening,
            mock.patch.object(intake.subprocess, "Popen") as popen,
            self.assertRaises(intake.ReviewDocumentIntakeError) as raised,
        ):
            intake.lark_cli_version(lark_cli=sys.executable)
        self.assertEqual(opening.call_count, 1)
        popen.assert_not_called()
        self.assertNotIn("private", json.dumps(raised.exception.public_data()))

    def test_capture_cleanup_failure_cannot_mask_spawn_permission_error(self):
        primary = PermissionError("private-cli-path")
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(intake.tempfile, "gettempdir", return_value=directory),
                mock.patch.object(intake.subprocess, "Popen", side_effect=primary),
                mock.patch.object(Path, "unlink", side_effect=PermissionError("cleanup-denied")),
                self.assertRaises(PermissionError) as raised,
            ):
                intake._default_command_runner(["not-executed"], timeout_seconds=0.1)
            self.assertIs(raised.exception, primary)

    def test_process_cleanup_failure_preserves_primary_timeout(self):
        process = mock.Mock()
        timeout = subprocess.TimeoutExpired(["private-command"], 0.1)
        process.wait.side_effect = timeout
        with (
            mock.patch.object(intake.subprocess, "Popen", return_value=process),
            mock.patch.object(intake.psutil, "Process", side_effect=intake.psutil.NoSuchProcess(0)),
            mock.patch.object(
                intake, "_terminate_command_tree", side_effect=PermissionError("private")
            ),
            self.assertRaises(subprocess.TimeoutExpired) as raised,
        ):
            intake._default_command_runner(["not-executed"], timeout_seconds=0.1)
        self.assertIs(raised.exception, timeout)
        self.assertEqual(raised.exception.cleanup_status, "incomplete")

    def test_output_snapshot_ignores_bytes_appended_after_the_parent_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captured-output"
            path.write_bytes(b"complete-response")
            original_fstat = os.fstat

            def snapshot_then_append(descriptor):
                snapshot = original_fstat(descriptor)
                with path.open("ab") as writer:
                    writer.write(b"descendant-still-writing")
                return snapshot

            with (
                path.open("rb") as stream,
                mock.patch.object(intake.os, "fstat", side_effect=snapshot_then_append),
            ):
                self.assertEqual(intake._read_command_output(stream), "complete-response")

    def test_oversized_cli_output_returns_safe_error_without_reading_contents(self):
        with (
            mock.patch.object(intake, "MAX_LARK_COMMAND_OUTPUT_BYTES", 8),
            self.assertRaises(intake.ReviewDocumentIntakeError) as raised,
        ):
            intake._default_command_runner(
                [sys.executable, "-c", "print('private-provider-response')"], timeout_seconds=2.0
            )
        self.assertEqual(raised.exception.code, "lark_cli_output_limit_exceeded")
        self.assertNotIn("private", json.dumps(raised.exception.public_data()))

    def test_successful_parent_does_not_wait_for_descendant_output_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child-pid"
            code = (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; print(\\\"child-log\\\", flush=True); time.sleep(30)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "print('parent-result', flush=True); time.sleep(0.1)"
            )
            try:
                started = time.monotonic()
                with mock.patch.object(intake.tempfile, "gettempdir", return_value=directory):
                    completed = intake._default_command_runner(
                        [sys.executable, "-c", code, str(pid_file)], timeout_seconds=2.0
                    )
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(completed.returncode, 0)
                self.assertIn("parent-result", completed.stdout)
                self.assertTrue(pid_file.exists())
                self.assertTrue(intake.psutil.Process(int(pid_file.read_text())).is_running())
            finally:
                if pid_file.exists():
                    try:
                        child = intake.psutil.Process(int(pid_file.read_text()))
                        child.kill()
                        child.wait(timeout=1.0)
                    except intake.psutil.NoSuchProcess:
                        pass

    def test_identity_timeout_does_not_leak_provider_command_or_output(self):
        timeout = subprocess.TimeoutExpired(
            ["private-cli", "whoami"], 0.1, output="private-token", stderr="private-stderr"
        )
        with self.assertRaises(intake.ReviewDocumentIntakeError) as raised:
            intake.lark_whoami(
                lark_cli=sys.executable,
                runner=mock.Mock(side_effect=timeout),
                timeout_seconds=0.1,
            )
        self.assertEqual(raised.exception.code, "lark_user_identity_unavailable")
        self.assertNotIn("private", json.dumps(raised.exception.public_data()))

    def test_readiness_write_denial_is_bounded_and_recovers_without_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "readiness.json"
            intake.evaluate_runtime_readiness(
                path=target, runtime_version="1.6.8", lark_version="1.2.3", asr_adapter_version="v1"
            )
            previous = target.read_bytes()
            started = time.monotonic()
            with (
                mock.patch.object(
                    atomic_io.os, "open", side_effect=PermissionError("private-readiness-path")
                ) as opening,
                self.assertRaises(intake.ReviewDocumentIntakeError) as raised,
            ):
                intake.invalidate_lark_readiness("lark_cli_unavailable", path=target)
            self.assertEqual(opening.call_count, 1)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(raised.exception.code, "readiness_write_failed")
            self.assertNotIn("private", json.dumps(raised.exception.public_data()))
            self.assertEqual(target.read_bytes(), previous)
            result = intake.invalidate_lark_readiness("lark_cli_unavailable", path=target)
            self.assertEqual(result["lark"]["status"], "pending_validation")
            verified = intake.mark_lark_verified(
                {"identity": "user", "defaultAs": "user", "available": True},
                path=target,
                runtime_version="1.6.8",
                lark_version="1.2.3",
                asr_adapter_version="v1",
            )
            self.assertEqual(verified["lark"]["status"], "verified")
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), verified)

    def test_strict_user_identity_is_still_required(self):
        for identity in ("bot", "tenant"):
            with (
                self.subTest(identity=identity),
                self.assertRaises(intake.ReviewDocumentIntakeError),
            ):
                intake.lark_whoami(
                    lark_cli=sys.executable,
                    runner=lambda command: subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps({"available": True, "identity": identity, "defaultAs": "user"}),
                        "",
                    ),
                )


if __name__ == "__main__":
    unittest.main()
