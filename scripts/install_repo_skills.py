from __future__ import annotations

import argparse
import os
import shutil
import stat
from pathlib import Path
from typing import Iterable


class UnsafeInstallPathError(RuntimeError):
    code = "unsafe_skill_install_path"

    def __init__(self, *, reason: str, path: Path) -> None:
        self.reason = reason
        self.path = path
        super().__init__(f"{reason}: {path}")


def _lexical_absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_safe_repo_write(repo_root: Path, target: Path) -> None:
    root = _lexical_absolute(repo_root)
    candidate = _lexical_absolute(target)
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise UnsafeInstallPathError(reason="target_outside_repo", path=candidate) from None
    if not relative.parts:
        raise UnsafeInstallPathError(reason="target_outside_repo", path=candidate)

    current = root
    while True:
        if os.path.lexists(current) and _is_reparse_point(current):
            raise UnsafeInstallPathError(reason="reparse_component", path=current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    current = root
    for part in relative.parts:
        current /= part
        if os.path.lexists(current) and _is_reparse_point(current):
            raise UnsafeInstallPathError(reason="reparse_component", path=current)


REPO_ROOT = _lexical_absolute(Path(__file__).parent.parent)
REPO_SKILLS_ROOT = REPO_ROOT / "skills"
REPO_LOCAL_SKILLS_ROOT = REPO_ROOT / ".codex" / "skills"
FIRST_USE_PROMPT = """\

现在有三种使用方式：

1. 自然语言直接说
例如：“帮我把这个草稿换个更适合的 BGM，花字不要挡字幕。”
我会按 auto-cut 总入口去判断该走哪个子 skill。

2. 点名总 skill
例如：“用 auto-cut 分析一下这个需求该怎么做。”
总 skill 会负责路由：该用 BGM、修订、音量峰值、红框安全区，还是多个组合。

3. 点名单独 skill
例如：“用 auto-cut-music-library-bgm 换 BGM。”
这种适合你已经知道要用哪个能力时精准调用。

新增最终验收入口：
- 用 auto-cut-final-acceptance 检查最终草稿是否真正满足审片意见、音频删词、画面修改、停顿节奏、动画时序和可编辑结构要求。

关键信息：
- 当前安装目标是仓库本地 .codex/skills，不是全局 skills 目录。
- skills/ 是源文件；.codex/skills 是运行时副本，可以用本脚本刷新。
- 后续新增技能请使用 auto-cut-<purpose> 命名，并同步更新目录和路由表。
"""


def discover_repo_skills(skills_root: Path) -> list[Path]:
    discovered: list[Path] = []
    for child in sorted(skills_root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "SKILL.md").exists():
            discovered.append(child)
    return discovered


def resolve_codex_skills_root(explicit_dest: str | None = None) -> Path:
    if explicit_dest:
        return _lexical_absolute(Path(explicit_dest).expanduser())
    return _lexical_absolute(REPO_LOCAL_SKILLS_ROOT)


def install_skills(
    source_root: Path,
    dest_root: Path,
    selected_names: Iterable[str] | None = None,
    dry_run: bool = False,
) -> list[tuple[Path, Path]]:
    package_root = _lexical_absolute(source_root.parent)
    dest_root = _lexical_absolute(dest_root)
    _assert_safe_repo_write(package_root, dest_root)
    selected = set(selected_names or [])
    skills = discover_repo_skills(source_root)
    pairs: list[tuple[Path, Path]] = []
    for skill_dir in skills:
        if selected and skill_dir.name not in selected:
            continue
        pairs.append((skill_dir, dest_root / skill_dir.name))

    if dry_run:
        return pairs

    _assert_safe_repo_write(package_root, dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    _assert_safe_repo_write(package_root, dest_root)
    for source, target in pairs:
        _assert_safe_repo_write(package_root, target)
        if os.path.lexists(target):
            shutil.rmtree(target)
        _assert_safe_repo_write(package_root, target)
        shutil.copytree(source, target)
    return pairs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install repository-scoped Auto-Cut skills into this repo's .codex/skills by "
            "default. Pass --dest only when an explicit copy target is needed."
        )
    )
    parser.add_argument(
        "--dest",
        help="Destination skills root. Defaults to this repository's .codex/skills.",
    )
    parser.add_argument(
        "--skill",
        action="append",
        dest="skills",
        help="Install only the named skill folder. Repeatable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned copies without writing.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List repository skills without copying them.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    skills = discover_repo_skills(REPO_SKILLS_ROOT)
    selected = set(args.skills or [])
    listed_skills = [skill for skill in skills if not selected or skill.name in selected]

    print(f"repo_skills_root={REPO_SKILLS_ROOT}")
    if not listed_skills:
        print("skills=0")
        return 0

    if args.list:
        print("mode=list")
        print("dest_root=<not set>")
        for skill in listed_skills:
            print(skill.name)
        print(f"skills={len(listed_skills)}")
        return 0

    dest_root = resolve_codex_skills_root(args.dest)
    try:
        pairs = install_skills(
            REPO_SKILLS_ROOT,
            dest_root,
            args.skills,
            args.dry_run,
        )
    except UnsafeInstallPathError as exc:
        print("mode=failed")
        print("status=failed")
        print(f"code={exc.code}")
        print(f"reason={exc.reason}")
        return 1

    print(f"dest_root={dest_root}")
    if args.dry_run:
        print("mode=dry-run-copy")
    else:
        print("mode=copy")

    for source, target in pairs:
        print(f"{source.name} -> {target}")
    print(f"copied={0 if args.dry_run else len(pairs)}")
    print(f"planned={len(pairs)}")
    if not args.dry_run:
        print(FIRST_USE_PROMPT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
