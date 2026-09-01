import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from utils.execution_input import (
    ExecutionInputError,
    MAX_ARTIFACT_NAME_CHARS,
    load_execution_input,
    resolve_artifact_name,
)


class ExecutionInputTests(unittest.TestCase):
    def _write_json(self, root: Path, payload: object, name: str = "execution-input.json") -> Path:
        path = root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_load_accepts_only_the_versioned_artifact_name_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self._write_json(
                root,
                {"artifact_name": "第15课 从二月革命到十月革命", "schema_version": 1},
            )

            payload, digest = load_execution_input(path)

            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["artifact_name"], "第15课 从二月革命到十月革命")
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            self.assertEqual(digest, hashlib.sha256(canonical).hexdigest())

    def test_load_rejects_missing_extra_or_invalid_fields(self) -> None:
        invalid_payloads = (
            {"schema_version": 1},
            {"schema_version": 1, "artifact_name": "name", "command": "ignored"},
            {"schema_version": 2, "artifact_name": "name"},
            {"schema_version": True, "artifact_name": "name"},
            {"schema_version": 1, "artifact_name": "   "},
            {"schema_version": 1, "artifact_name": 123},
            [1, "name"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, payload in enumerate(invalid_payloads):
                with self.subTest(index=index, payload=payload):
                    path = self._write_json(root, payload, f"invalid-{index}.json")
                    with self.assertRaises(ExecutionInputError):
                        load_execution_input(path)

    def test_name_priority_is_external_then_document_then_project_then_fallback(self) -> None:
        external = resolve_artifact_name(
            external_name="外部名称",
            document_title="文档标题",
            project_name="兼容项目名",
            fallback_name="AutoCutLite-123456789abc",
        )
        document = resolve_artifact_name(
            document_title="文档标题",
            project_name="兼容项目名",
            fallback_name="AutoCutLite-123456789abc",
        )
        project = resolve_artifact_name(
            project_name="兼容项目名",
            fallback_name="AutoCutLite-123456789abc",
        )
        fallback = resolve_artifact_name(fallback_name="AutoCutLite-123456789abc")

        self.assertEqual((external.final_name, external.source), ("外部名称", "external_input"))
        self.assertEqual((document.final_name, document.source), ("文档标题", "document_title"))
        self.assertEqual((project.final_name, project.source), ("兼容项目名", "project_json"))
        self.assertEqual(
            (fallback.final_name, fallback.source),
            ("AutoCutLite-123456789abc", "identity_fallback"),
        )

    def test_name_is_one_safe_windows_component(self) -> None:
        traversal = resolve_artifact_name(
            external_name=' ..\\folder/CON:<bad>|?*\n"title".. ',
            fallback_name="AutoCutLite-123456789abc",
        )
        reserved = resolve_artifact_name(
            external_name="CON.txt",
            fallback_name="AutoCutLite-123456789abc",
        )
        numbered_reserved = resolve_artifact_name(
            external_name="COM1.mp4",
            fallback_name="AutoCutLite-123456789abc",
        )
        truncated = resolve_artifact_name(
            external_name="标" * (MAX_ARTIFACT_NAME_CHARS + 40),
            fallback_name="AutoCutLite-123456789abc",
        )
        punctuation_only = resolve_artifact_name(
            external_name=" ... ",
            document_title="must-not-win",
            fallback_name="AutoCutLite-123456789abc",
        )
        zip_suffix = resolve_artifact_name(
            external_name="课程名.ZIP",
            fallback_name="AutoCutLite-123456789abc",
        )

        self.assertTrue(traversal.sanitized)
        self.assertNotIn("..", traversal.final_name)
        self.assertFalse(any(character in traversal.final_name for character in '<>:"/\\|?*\n'))
        self.assertEqual(Path(traversal.final_name).name, traversal.final_name)
        self.assertEqual(reserved.final_name, "_CON.txt")
        self.assertTrue(reserved.sanitized)
        self.assertEqual(numbered_reserved.final_name, "_COM1.mp4")
        self.assertTrue(numbered_reserved.sanitized)
        self.assertEqual(len(truncated.final_name), MAX_ARTIFACT_NAME_CHARS)
        self.assertTrue(truncated.sanitized)
        self.assertEqual(punctuation_only.final_name, "_")
        self.assertEqual(punctuation_only.source, "external_input")
        self.assertTrue(punctuation_only.sanitized)
        self.assertEqual(zip_suffix.final_name, "课程名")
        self.assertTrue(zip_suffix.sanitized)


if __name__ == "__main__":
    unittest.main()
