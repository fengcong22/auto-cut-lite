from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "assert_lite_workspace.py"
SPEC = importlib.util.spec_from_file_location("assert_lite_workspace", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LiteWorkspaceIdentityTests(unittest.TestCase):
    def test_current_lite_checkout_passes(self) -> None:
        result = MODULE.inspect_workspace()

        self.assertTrue(result["ok"])
        self.assertEqual(result["repository"], "auto-cut-lite")
        self.assertEqual(result["branch"], "feature/auto-cut-lite")
        self.assertEqual(result["workflow_mode"], "lite")

    def test_wrong_branch_fails_closed(self) -> None:
        real_git = MODULE._git

        def fake_git(repo_root: Path, *args: str) -> str:
            if args == ("branch", "--show-current"):
                return "main"
            return real_git(repo_root, *args)

        with mock.patch.object(MODULE, "_git", side_effect=fake_git):
            result = MODULE.inspect_workspace()

        self.assertFalse(result["ok"])
        self.assertIn("unexpected_branch", result["problems"])

    def test_wrong_working_directory_fails_closed(self) -> None:
        with mock.patch.object(MODULE.Path, "cwd", return_value=ROOT.parent):
            result = MODULE.inspect_workspace()

        self.assertFalse(result["ok"])
        self.assertIn("unexpected_working_directory", result["problems"])


if __name__ == "__main__":
    unittest.main()
