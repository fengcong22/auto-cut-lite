from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


EXPECTED_ROOT = Path(r"E:\codex\Auto-cut-高中历史\worktrees\auto-cut-lite")
EXPECTED_BRANCH = "feature/auto-cut-lite"


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
    if repo_root != EXPECTED_ROOT.resolve():
        problems.append("unexpected_repository_root")
    if branch != EXPECTED_BRANCH:
        problems.append("unexpected_branch")

    return {
        "ok": not problems,
        "repository": "auto-cut-lite",
        "repository_root": str(repo_root),
        "working_directory": str(working_directory),
        "branch": branch,
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
