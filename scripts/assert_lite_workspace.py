from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PRIMARY_BRANCH = "main"
RETIRED_BRANCHES = frozenset({"feature/auto-cut-lite"})
PLUGIN_MANIFEST = Path("plugins/auto-cut-lite/.codex-plugin/plugin.json")
CAPABILITY_MANIFEST = Path("plugins/auto-cut-lite/PORTABLE-CAPABILITIES.json")


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError("lite workspace Git identity is unavailable")
    return completed.stdout.strip()


def _json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _has_lite_repository_identity(repo_root: Path) -> bool:
    plugin_path = repo_root / PLUGIN_MANIFEST
    capability_path = repo_root / CAPABILITY_MANIFEST
    if (
        not plugin_path.is_file()
        or plugin_path.is_symlink()
        or not capability_path.is_file()
        or capability_path.is_symlink()
    ):
        return False
    plugin = _json_object(plugin_path)
    capabilities = _json_object(capability_path)
    return (
        plugin.get("name") == "auto-cut-lite"
        and capabilities.get("plugin_name") == "auto-cut-lite"
        and capabilities.get("plugin_version") == plugin.get("version")
    )


def inspect_workspace(script_path: Path | None = None) -> dict[str, object]:
    source = (script_path or Path(__file__)).resolve()
    repo_root = source.parents[1]
    working_directory = Path.cwd().resolve()
    git_root = Path(_git(repo_root, "rev-parse", "--show-toplevel")).resolve()
    branch = _git(repo_root, "branch", "--show-current")
    status = _git(repo_root, "status", "--short", "--branch")
    problems: list[str] = []

    if git_root != repo_root:
        problems.append("script_not_in_git_root")
    if working_directory != repo_root:
        problems.append("unexpected_working_directory")
    if not _has_lite_repository_identity(repo_root):
        problems.append("unexpected_repository_identity")
    if not branch:
        problems.append("detached_head")
    elif branch in RETIRED_BRANCHES:
        problems.append("retired_legacy_branch")

    return {
        "ok": not problems,
        "repository": "auto-cut-lite",
        "repository_root": str(repo_root),
        "working_directory": str(working_directory),
        "branch": branch,
        "primary_branch": PRIMARY_BRANCH,
        "branch_role": "primary" if branch == PRIMARY_BRANCH else "development",
        "workflow_mode": "lite",
        "git_status": status,
        "problems": problems,
    }


def main() -> int:
    try:
        result = inspect_workspace()
    except (OSError, RuntimeError) as exc:
        result = {
            "ok": False,
            "repository": "auto-cut-lite",
            "workflow_mode": "lite",
            "problems": [str(exc)],
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
