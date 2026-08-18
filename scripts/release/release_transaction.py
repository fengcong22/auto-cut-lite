from __future__ import annotations

import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

_SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class ReleaseSource:
    version: str
    source_commit: str


def _git_output(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _git_bytes(repo_root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def capture_clean_release_source(repo_root: str | Path) -> ReleaseSource:
    root = Path(repo_root).resolve()
    before = _git_output(root, "rev-parse", "HEAD^{commit}")
    if _SOURCE_COMMIT.fullmatch(before) is None:
        raise RuntimeError("release source commit is invalid")
    status = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    after = _git_output(root, "rev-parse", "HEAD^{commit}")
    if before != after:
        raise RuntimeError("release source HEAD changed during identity capture")
    if status:
        raise RuntimeError("release build requires a clean worktree")
    try:
        version = _git_bytes(root, "show", f"{before}:VERSION").decode("utf-8-sig").strip()
    except UnicodeError as exc:
        raise RuntimeError("committed release version is unreadable") from exc
    if _SEMVER.fullmatch(version) is None:
        raise RuntimeError("committed release version is invalid")
    return ReleaseSource(version=version, source_commit=before)


def assert_release_source_unchanged(
    repo_root: str | Path,
    expected: ReleaseSource,
) -> None:
    current = capture_clean_release_source(repo_root)
    if current != expected:
        raise RuntimeError("release source identity changed before publication")


@contextmanager
def unique_sibling_temp(destination: str | Path) -> Iterator[Path]:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def publish_files_no_replace(
    pairs: Iterable[tuple[str | Path, str | Path]],
) -> None:
    normalized = tuple((Path(source), Path(destination)) for source, destination in pairs)
    if not normalized:
        raise ValueError("release publication set is empty")
    destinations = [str(destination.resolve(strict=False)).casefold() for _, destination in normalized]
    if len(destinations) != len(set(destinations)):
        raise ValueError("release publication destinations are not unique")
    for source, destination in normalized:
        if source.is_symlink() or not source.is_file():
            raise ValueError("release publication source is not a regular file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(destination):
            raise FileExistsError(f"release output already exists: {destination.name}")

    published: list[tuple[Path, Path]] = []
    try:
        for source, destination in normalized:
            os.link(source, destination)
            published.append((source, destination))
    except Exception:
        rollback_failed = False
        for source, destination in reversed(published):
            try:
                if os.path.lexists(destination) and os.path.samefile(source, destination):
                    destination.unlink()
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise RuntimeError("release publication rollback failed")
        raise


def publish_file_no_replace(source: str | Path, destination: str | Path) -> None:
    publish_files_no_replace(((source, destination),))


__all__ = [
    "ReleaseSource",
    "assert_release_source_unchanged",
    "capture_clean_release_source",
    "publish_file_no_replace",
    "publish_files_no_replace",
    "unique_sibling_temp",
]
