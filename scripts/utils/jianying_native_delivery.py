"""Byte-preserving JianYing draft delivery helpers.

The native-save contract intentionally has no JianYing/UI dependency.  A
draft is assumed to have already been saved by JianYing; this module only
validates the configured destination and makes an exact filesystem mirror.
It never opens the editor, invokes an automation backend, parses draft JSON,
or calls the historical portable-project packager.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:  # Package import (``scripts.utils``).
    from .formatters import (
        ConfiguredDraftRootError,
        get_configured_jianying_draft_root,
    )
except ImportError:  # pragma: no cover - direct ``scripts`` execution fallback
    from utils.formatters import (  # type: ignore
        ConfiguredDraftRootError,
        get_configured_jianying_draft_root,
    )


NATIVE_DELIVERY_SCHEMA_VERSION = 1
NATIVE_DELIVERY_ADAPTER_VERSION = "jianying-native-draft-mirror-v1"
DELIVERY_MODE = "jianying_fixed_path_mirror"
DEFAULT_QUIET_WINDOW_SECONDS = 6.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_STABILITY_TIMEOUT_SECONDS = 120.0
DEFAULT_STABILITY_RETRY_COUNT = 2


class NativeDeliveryError(ValueError):
    """A fail-closed native draft delivery contract failure."""


# Name retained for callers that imported the prototype mirror module.
DraftMirrorError = NativeDeliveryError


def _error(code: str, detail: str) -> NativeDeliveryError:
    return NativeDeliveryError(f"{code}: {detail}")


def _is_reparse_point(path: str | os.PathLike[str]) -> bool:
    try:
        target_stat = os.lstat(os.fspath(path))
    except OSError as exc:
        raise _error("path_unreadable", f"cannot inspect {path}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(target_stat.st_mode) or bool(
        getattr(target_stat, "st_file_attributes", 0) & reparse_flag
    )


def _has_reparse_component(path: Path) -> bool:
    current = os.path.abspath(os.fspath(path))
    while True:
        if os.path.lexists(current) and _is_reparse_point(current):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _resolve_existing_directory(path: str | os.PathLike[str], *, code: str) -> Path:
    candidate = Path(path).expanduser()
    if not os.path.lexists(os.fspath(candidate)):
        raise _error(code, f"directory does not exist: {candidate}")
    if _has_reparse_component(candidate):
        raise _error(
            "reparse_path_rejected", f"path contains a symlink or reparse point: {candidate}"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(code, f"directory cannot be resolved: {candidate}") from exc
    if not resolved.is_dir():
        raise _error(code, f"path is not a directory: {candidate}")
    return resolved


def validate_native_target_root(
    target_root: str | os.PathLike[str], *, require_exists: bool = True
) -> Path:
    """Validate the fixed mirror root without creating or selecting a fallback."""

    candidate = Path(target_root).expanduser()
    if not os.path.lexists(os.fspath(candidate)):
        if require_exists:
            raise _error(
                "configured_target_path_missing",
                f"native delivery target root does not exist: {candidate}",
            )
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise _error("configured_target_path_invalid", str(candidate)) from exc
    try:
        return _resolve_existing_directory(candidate, code="configured_target_path_invalid")
    except NativeDeliveryError as exc:
        # Keep the public error vocabulary stable for a configured root while
        # retaining the more specific reparse/path detail in the message.
        if str(exc).startswith("reparse_path_rejected:"):
            raise
        raise


def resolve_configured_native_target_root(
    local_app_data: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the one configured JianYing root, failing closed on ambiguity."""

    try:
        return get_configured_jianying_draft_root(local_app_data, require_exists=True)
    except ConfiguredDraftRootError as exc:
        raise NativeDeliveryError(str(exc)) from exc


def check_native_target_path(
    target_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Return a non-mutating first-use path-check result."""

    try:
        resolved = validate_native_target_root(target_root, require_exists=True)
    except NativeDeliveryError as exc:
        return {
            "status": "fail",
            "path_check": "fail",
            "error": str(exc),
            "target_root": str(Path(target_root).expanduser()),
        }
    return {
        "status": "pass",
        "path_check": "pass",
        "target_root": str(resolved),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise _error("source_tree_unreadable", f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def capture_draft_tree_receipt(draft_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Hash every regular file and record empty directories in a draft tree.

    No file is parsed.  In particular, ``draft_content.json`` is treated as
    opaque bytes, so line endings, key ordering, and JianYing's encoding are
    preserved exactly by the mirror operation.
    """

    root = _resolve_existing_directory(draft_dir, code="source_draft_missing")
    files: list[dict[str, Any]] = []
    directories: list[str] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise _error("source_tree_unreadable", f"cannot enumerate {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            if _is_reparse_point(path):
                raise _error(
                    "reparse_path_rejected", f"draft tree contains a reparse point: {path}"
                )
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise _error("source_tree_unreadable", f"cannot inspect {path}: {exc}") from exc
            if stat.S_ISDIR(entry_stat.st_mode):
                directories.append(_relative_path(path, root))
                visit(path)
            elif stat.S_ISREG(entry_stat.st_mode):
                before = (entry_stat.st_size, entry_stat.st_mtime_ns)
                sha256 = _sha256_file(path)
                try:
                    after_stat = path.stat()
                except OSError as exc:
                    raise _error("source_tree_unreadable", f"cannot restat {path}: {exc}") from exc
                after = (after_stat.st_size, after_stat.st_mtime_ns)
                if before != after:
                    raise _error("source_tree_changed", f"file changed while hashing: {path}")
                files.append(
                    {
                        "path": _relative_path(path, root),
                        "byte_size": int(after_stat.st_size),
                        "sha256": sha256,
                    }
                )
            else:
                raise _error(
                    "unsupported_tree_entry", f"draft tree contains a non-file entry: {path}"
                )

    visit(root)
    files.sort(key=lambda row: (str(row["path"]).casefold(), str(row["path"])))
    directories.sort(key=lambda item: (item.casefold(), item))
    manifest = {"directories": directories, "files": files}
    tree_sha256 = hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()
    return {
        "schema_version": NATIVE_DELIVERY_SCHEMA_VERSION,
        "tree_sha256": tree_sha256,
        "file_count": len(files),
        "directory_count": len(directories),
        "byte_size": sum(int(row["byte_size"]) for row in files),
        "directories": directories,
        "files": files,
    }


def _validate_tree_receipt_shape(value: Mapping[str, Any]) -> None:
    files = value.get("files")
    directories = value.get("directories")
    if not isinstance(files, list) or not isinstance(directories, list):
        raise _error(
            "invalid_tree_receipt", "tree receipt must contain files and directories lists"
        )
    if any(not isinstance(item, str) or not item for item in directories):
        raise _error("invalid_tree_receipt", "directory receipt contains an invalid path")
    if len(set(directories)) != len(directories):
        raise _error("invalid_tree_receipt", "directory receipt contains duplicate paths")
    total_size = 0
    for row in files:
        if not isinstance(row, Mapping):
            raise _error("invalid_tree_receipt", "file receipt row is not an object")
        relative = row.get("path")
        byte_size = row.get("byte_size")
        digest = str(row.get("sha256") or "")
        if (
            not isinstance(relative, str)
            or not relative
            or isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or byte_size < 0
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise _error("invalid_tree_receipt", "file receipt row is invalid")
        total_size += byte_size
    if value.get("file_count") != len(files) or value.get("directory_count") != len(directories):
        raise _error("invalid_tree_receipt", "tree receipt counts are inconsistent")
    if value.get("byte_size") != total_size:
        raise _error("invalid_tree_receipt", "tree receipt byte size is inconsistent")
    manifest = {
        "directories": sorted(directories, key=lambda item: (item.casefold(), item)),
        "files": sorted(files, key=lambda item: (str(item["path"]).casefold(), str(item["path"]))),
    }
    expected_tree_sha = hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()
    if value.get("tree_sha256") != expected_tree_sha:
        raise _error("invalid_tree_receipt", "tree receipt digest is inconsistent")


def verify_draft_tree_receipt(
    draft_dir: str | os.PathLike[str], expected: Mapping[str, Any]
) -> dict[str, Any]:
    """Capture and compare a tree receipt, returning the fresh receipt."""

    if not isinstance(expected, Mapping):
        raise _error("invalid_tree_receipt", "expected receipt is not an object")
    _validate_tree_receipt_shape(expected)
    actual = capture_draft_tree_receipt(draft_dir)
    expected_sha = str(expected.get("tree_sha256") or "")
    if not expected_sha or actual["tree_sha256"] != expected_sha:
        raise _error("tree_receipt_mismatch", f"tree digest differs for {draft_dir}")
    return actual


# Compatibility alias used by the earlier mirror prototype.  Both names are
# intentionally read-only tree inventory operations.
capture_tree_receipt = capture_draft_tree_receipt


def _safe_draft_name(value: str | os.PathLike[str] | None) -> str:
    name = str(value or "").strip()
    if (
        not name
        or name in {".", ".."}
        or name.endswith((".", " "))
        or any(char in name for char in ("/", "\\", "\x00", ":", "<", ">", '"', "|", "?", "*"))
    ):
        raise _error("invalid_draft_name", "draft name must be one safe directory name")
    if Path(name).name != name:
        raise _error("invalid_draft_name", "draft name must be one safe directory name")
    # Windows rejects these names even when running under a POSIX test host.
    stem = name.rstrip(" .").split(".", 1)[0].casefold()
    if (
        stem in {"con", "prn", "aux", "nul"}
        or stem.startswith(("com", "lpt"))
        and stem[3:].isdigit()
    ):
        raise _error("invalid_draft_name", "draft name is reserved by Windows")
    return name


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        os.path.commonpath([os.fspath(first), os.fspath(second)])
    except ValueError:
        return False
    first_text = os.path.normcase(os.path.abspath(os.fspath(first)))
    second_text = os.path.normcase(os.path.abspath(os.fspath(second)))
    return (
        first_text == second_text
        or first_text.startswith(second_text + os.sep)
        or second_text.startswith(first_text + os.sep)
    )


def _assert_source_draft(source_draft: str | os.PathLike[str]) -> Path:
    source = _resolve_existing_directory(source_draft, code="source_draft_missing")
    if source.name in {".", ".."}:
        raise _error("invalid_source_draft", "source must be a named draft directory")
    content = source / "draft_content.json"
    metadata = source / "draft_meta_info.json"
    if not content.is_file() and not metadata.is_file():
        raise _error(
            "invalid_source_draft",
            "source directory has no draft_content.json or draft_meta_info.json",
        )
    # A top-level reparse-point file would otherwise be followed by copy2.
    for marker in (content, metadata):
        if os.path.lexists(os.fspath(marker)) and _is_reparse_point(marker):
            raise _error("reparse_path_rejected", f"draft marker is a reparse point: {marker}")
    return source


def _copy_tree_bytes(source: Path, destination: Path) -> None:
    """Copy a validated tree without interpreting any file contents."""

    try:
        destination.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise _error(
            "target_write_failed", f"cannot create temporary destination: {destination}"
        ) from exc
    stack: list[tuple[Path, Path]] = [(source, destination)]
    while stack:
        current_source, current_destination = stack.pop()
        try:
            entries = sorted(os.scandir(current_source), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise _error(
                "source_tree_unreadable", f"cannot enumerate {current_source}: {exc}"
            ) from exc
        for entry in entries:
            source_path = Path(entry.path)
            destination_path = current_destination / entry.name
            if _is_reparse_point(source_path):
                raise _error(
                    "reparse_path_rejected", f"draft tree contains a reparse point: {source_path}"
                )
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise _error(
                    "source_tree_unreadable", f"cannot inspect {source_path}: {exc}"
                ) from exc
            if stat.S_ISDIR(entry_stat.st_mode):
                try:
                    destination_path.mkdir()
                except OSError as exc:
                    raise _error(
                        "target_write_failed", f"cannot create {destination_path}"
                    ) from exc
                # Preserve the source directory mode/timestamps where the
                # platform permits; bytes remain the authoritative contract.
                stack.append((source_path, destination_path))
            elif stat.S_ISREG(entry_stat.st_mode):
                try:
                    shutil.copy2(source_path, destination_path, follow_symlinks=False)
                except OSError as exc:
                    raise _error(
                        "target_write_failed", f"cannot copy {source_path}: {exc}"
                    ) from exc
            else:
                raise _error(
                    "unsupported_tree_entry", f"draft tree contains a non-file entry: {source_path}"
                )


def _remove_tree(path: Path) -> None:
    if not os.path.lexists(os.fspath(path)):
        return
    if _is_reparse_point(path):
        try:
            path.unlink()
        except OSError:
            pass
        return
    shutil.rmtree(path, ignore_errors=True)


def _write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    if os.path.lexists(os.fspath(path)) and _is_reparse_point(path):
        raise _error("receipt_path_invalid", f"receipt path is a reparse point: {path}")
    if _has_reparse_component(path.parent):
        raise _error(
            "receipt_path_invalid", f"receipt parent contains a reparse point: {path.parent}"
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _error(
            "receipt_write_failed", f"cannot create receipt directory: {path.parent}"
        ) from exc
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise _error("receipt_write_failed", f"cannot write receipt: {path}") from exc


def _validate_stability_policy(
    quiet_window_seconds: float,
    poll_interval_seconds: float,
    timeout_seconds: float,
    retry_count: int,
) -> tuple[float, float, float, int]:
    values = {
        "quiet_window_seconds": quiet_window_seconds,
        "poll_interval_seconds": poll_interval_seconds,
        "timeout_seconds": timeout_seconds,
    }
    normalized: dict[str, float] = {}
    for name, value in values.items():
        if isinstance(value, bool):
            raise _error("invalid_stability_policy", f"{name} must be a finite positive number")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise _error(
                "invalid_stability_policy", f"{name} must be a finite positive number"
            ) from exc
        if not number > 0 or not math.isfinite(number):
            raise _error("invalid_stability_policy", f"{name} must be a finite positive number")
        normalized[name] = number
    if normalized["poll_interval_seconds"] > normalized["quiet_window_seconds"]:
        raise _error("invalid_stability_policy", "poll interval cannot exceed quiet window")
    if normalized["timeout_seconds"] < normalized["quiet_window_seconds"]:
        raise _error("invalid_stability_policy", "timeout cannot be shorter than quiet window")
    if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
        raise _error("invalid_stability_policy", "retry count must be a non-negative integer")
    return (
        normalized["quiet_window_seconds"],
        normalized["poll_interval_seconds"],
        normalized["timeout_seconds"],
        retry_count,
    )


def _wait_for_stable_tree(
    source: Path,
    *,
    quiet_window_seconds: float,
    poll_interval_seconds: float,
    timeout_seconds: float,
    clock: Any,
    sleep: Any,
    capture_receipt: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wait until the complete source receipt is unchanged for quiet_window."""

    started = float(clock())
    observations: list[dict[str, Any]] = []
    baseline: dict[str, Any] | None = None
    baseline_started = started
    while True:
        now = float(clock())
        if now - started > timeout_seconds:
            raise _error(
                "stability_timeout",
                f"source tree did not remain quiet for {quiet_window_seconds:g}s within {timeout_seconds:g}s",
            )
        try:
            receipt = capture_receipt(source)
        except NativeDeliveryError as exc:
            if not str(exc).startswith("source_tree_changed:"):
                raise
            receipt = None
        observed_at = float(clock())
        if receipt is not None:
            digest = str(receipt["tree_sha256"])
            if baseline is None or digest != str(baseline["tree_sha256"]):
                baseline = receipt
                baseline_started = observed_at
            quiet_elapsed = max(0.0, observed_at - baseline_started)
            observations.append(
                {
                    "timestamp": observed_at,
                    "tree_sha256": digest,
                    "quiet_elapsed_seconds": quiet_elapsed,
                }
            )
            if quiet_elapsed >= quiet_window_seconds:
                return receipt, {
                    "observation_count": len(observations),
                    "started_at": started,
                    "finished_at": observed_at,
                    "elapsed_seconds": max(0.0, observed_at - started),
                    "observations": observations,
                }
        else:
            baseline = None
            baseline_started = observed_at
            observations.append(
                {"timestamp": observed_at, "tree_sha256": None, "quiet_elapsed_seconds": 0.0}
            )
        remaining = timeout_seconds - (float(clock()) - started)
        if remaining <= 0:
            raise _error(
                "stability_timeout",
                f"source tree did not remain quiet for {quiet_window_seconds:g}s within {timeout_seconds:g}s",
            )
        sleep(min(poll_interval_seconds, remaining))


def mirror_draft_tree(
    *,
    source_draft: str | os.PathLike[str],
    target_root: str | os.PathLike[str],
    draft_name: str | None = None,
    receipt_path: str | os.PathLike[str] | None = None,
    require_target_root_exists: bool = True,
    receipt_metadata: Mapping[str, Any] | None = None,
    quiet_window_seconds: float = DEFAULT_QUIET_WINDOW_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_STABILITY_TIMEOUT_SECONDS,
    retry_count: int = DEFAULT_STABILITY_RETRY_COUNT,
    clock: Any | None = None,
    sleep: Any | None = None,
    capture_receipt: Any | None = None,
) -> dict[str, Any]:
    """Make and verify an exact tree mirror in a fixed existing target root."""

    source = _assert_source_draft(source_draft)
    # The first-use contract is intentionally fail-closed.  Keep the keyword
    # for API compatibility, but never create a missing root even when a
    # caller passes ``False``.
    target = validate_native_target_root(target_root, require_exists=True)
    name = _safe_draft_name(draft_name or source.name)
    destination = target / name
    # Keep the destination lexical until it has been checked.  Resolving it
    # first would follow an existing destination symlink and could turn an
    # apparently contained copy into an out-of-root write.
    if destination.parent != target:
        raise _error("invalid_target_path", "destination must be a direct child of target root")
    if _has_reparse_component(target) or _has_reparse_component(destination):
        raise _error("reparse_path_rejected", "target path contains a symlink or reparse point")
    if _paths_overlap(source, target) or _paths_overlap(source, destination):
        raise _error("source_target_overlap", "source draft overlaps the fixed target root")
    if os.path.lexists(os.fspath(destination)):
        raise _error("destination_already_exists", f"destination already exists: {destination}")

    receipt_target: Path | None = None
    if receipt_path is not None:
        # Do not resolve through a pre-existing symlink before validating the
        # receipt location; the receipt must never escape an explicitly chosen
        # parent unexpectedly.
        receipt_target = Path(os.path.abspath(os.fspath(Path(receipt_path).expanduser())))
        if _has_reparse_component(receipt_target):
            raise _error("receipt_path_invalid", "receipt path contains a symlink or reparse point")
        if _paths_overlap(receipt_target, source) or _paths_overlap(receipt_target, destination):
            raise _error(
                "receipt_path_invalid", "receipt must be outside source and destination drafts"
            )

    quiet_window_seconds, poll_interval_seconds, timeout_seconds, max_retries = (
        _validate_stability_policy(
            quiet_window_seconds, poll_interval_seconds, timeout_seconds, retry_count
        )
    )
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    capture_receipt = capture_receipt or capture_draft_tree_receipt
    stability_policy = {
        "quiet_window_seconds": quiet_window_seconds,
        "poll_interval_seconds": poll_interval_seconds,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
    }
    attempts: list[dict[str, Any]] = []
    completed_retries = 0
    source_before: dict[str, Any]
    final_target_receipt: dict[str, Any]
    pre_copy_stability: dict[str, Any]
    post_copy_stability: dict[str, Any]
    while True:
        temporary = target / f".{name}.native-mirror-{uuid.uuid4().hex}.tmp"
        if os.path.lexists(os.fspath(temporary)):
            raise _error(
                "target_write_failed", f"temporary destination already exists: {temporary}"
            )
        try:
            source_before, pre_copy_stability = _wait_for_stable_tree(
                source,
                quiet_window_seconds=quiet_window_seconds,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=timeout_seconds,
                clock=clock,
                sleep=sleep,
                capture_receipt=capture_receipt,
            )
            _copy_tree_bytes(source, temporary)
            target_receipt = capture_receipt(temporary)
            if target_receipt["tree_sha256"] != source_before["tree_sha256"]:
                raise _error("tree_receipt_mismatch", "copied draft tree differs from source")
            stable_after, post_copy_stability = _wait_for_stable_tree(
                source,
                quiet_window_seconds=quiet_window_seconds,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=timeout_seconds,
                clock=clock,
                sleep=sleep,
                capture_receipt=capture_receipt,
            )
            if stable_after["tree_sha256"] != source_before["tree_sha256"]:
                raise _error("source_tree_changed", "source draft changed after copying; retrying")
            if os.path.lexists(os.fspath(destination)):
                raise _error(
                    "destination_already_exists", f"destination already exists: {destination}"
                )
            try:
                os.replace(temporary, destination)
            except OSError as exc:
                raise _error(
                    "target_write_failed", f"cannot finalize destination: {destination}"
                ) from exc
            final_target_receipt = capture_receipt(destination)
            if final_target_receipt["tree_sha256"] != source_before["tree_sha256"]:
                _remove_tree(destination)
                raise _error("tree_receipt_mismatch", "final destination tree differs from source")
            promotion_source_receipt = capture_receipt(source)
            if promotion_source_receipt["tree_sha256"] != source_before["tree_sha256"]:
                _remove_tree(destination)
                raise _error(
                    "source_tree_changed", "source draft changed during promotion; retrying"
                )
            attempts.append({"attempt": completed_retries + 1, "status": "pass"})
            break
        except NativeDeliveryError as exc:
            _remove_tree(temporary)
            retryable = str(exc).startswith(("source_tree_changed:", "stability_timeout:"))
            attempts.append(
                {
                    "attempt": completed_retries + 1,
                    "status": "retry" if retryable and completed_retries < max_retries else "fail",
                    "error": str(exc),
                }
            )
            if not retryable or completed_retries >= max_retries:
                if str(exc).startswith("source_tree_changed:") and completed_retries >= max_retries:
                    raise _error("stability_retry_exhausted", str(exc)) from exc
                raise
            completed_retries += 1
            continue
        except Exception:
            _remove_tree(temporary)
            raise

    final_target_receipt = capture_receipt(destination)
    if final_target_receipt["tree_sha256"] != source_before["tree_sha256"]:
        _remove_tree(destination)
        raise _error("tree_receipt_mismatch", "final destination tree differs from source")

    result: dict[str, Any] = {
        "schema_version": NATIVE_DELIVERY_SCHEMA_VERSION,
        "adapter_version": NATIVE_DELIVERY_ADAPTER_VERSION,
        "status": "pass",
        "mode": "native_draft_mirror",
        "delivery_mode": DELIVERY_MODE,
        "source_draft_path": str(source),
        "native_draft_path": str(source),
        "target_root": str(target),
        "target_draft_path": str(destination),
        "desktop_draft_path": str(destination),
        "draft_name": name,
        "source_tree": source_before,
        "target_tree": final_target_receipt,
        "desktop_tree": final_target_receipt,
        "source_tree_sha256": source_before["tree_sha256"],
        "target_tree_sha256": final_target_receipt["tree_sha256"],
        "tree_sha256": final_target_receipt["tree_sha256"],
        "path_check": "pass",
        "path_check_result": {
            "status": "pass",
            "target_root": str(target),
            "exists": True,
        },
        "ui_invoked": False,
        "json_rewritten": False,
        "portable_package_invoked": False,
        "native_editor_invoked": False,
        "review_operations": [],
        "self_contained_verified": False,
        "external_material_localization_verified": False,
        "portability_claim": "tree_identity_only",
        "warning": (
            "镜像仅证明源草稿目录与目标目录字节一致；不会推断草稿引用的外部素材已经本地化。"
        ),
        "stability_policy": stability_policy,
        "stability": {
            "pre_copy": pre_copy_stability,
            "post_copy": post_copy_stability,
            "attempts": attempts,
        },
        "source_stable_before_copy": True,
        "source_stable_after_copy": True,
        "source_stable_at_promotion": True,
        "retry_count": completed_retries,
    }
    if receipt_metadata:
        # Metadata is receipt-only evidence.  It is never written into the
        # JianYing draft tree and cannot alter the byte comparison above.
        result.update(dict(receipt_metadata))
    if receipt_target is not None:
        try:
            _write_receipt(receipt_target, result)
        except Exception:
            # A mirror without its integrity receipt is not a deliverable;
            # remove the newly-created destination before surfacing the error.
            _remove_tree(destination)
            raise
        result["receipt_path"] = str(receipt_target)
        result["receipt_sha256"] = _sha256_file(receipt_target)
    return result


def copy_verified_draft_to_desktop(
    *,
    native_draft: str | os.PathLike[str],
    desktop_root: str | os.PathLike[str],
    receipt: Mapping[str, Any] | None = None,
    receipt_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Compatibility name for the neutral fixed-root mirror operation."""

    if receipt is not None:
        expected = receipt.get("source_tree") if isinstance(receipt, Mapping) else None
        if not isinstance(expected, Mapping):
            raise _error("invalid_tree_receipt", "source tree receipt is missing")
        actual = capture_draft_tree_receipt(native_draft)
        if actual["tree_sha256"] != expected.get("tree_sha256"):
            raise _error("tree_receipt_mismatch", "native draft does not match its receipt")
    return mirror_draft_tree(
        source_draft=native_draft,
        target_root=desktop_root,
        receipt_path=receipt_path,
    )


def run_native_import_save_delivery(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Reject the retired UI flow; optionally route an explicit mirror call.

    The old implementation accepted a UI executor and created/imported a
    draft.  That behavior is intentionally unavailable: callers must provide
    a JianYing-saved ``source_draft`` and invoke :func:`mirror_draft_tree`.
    Keeping this narrow guard makes accidental legacy calls fail loudly rather
    than opening or controlling JianYing.
    """

    if kwargs.get("ui_executor") is not None or kwargs.get("source_video") is not None:
        raise NativeDeliveryError(
            "native_ui_disabled: native delivery does not open JianYing or import media; "
            "mirror an already saved draft instead"
        )
    source = kwargs.pop("source_draft", kwargs.pop("native_draft", None))
    target = kwargs.pop("target_root", kwargs.pop("desktop_root", None))
    if args:
        raise NativeDeliveryError(
            "native_ui_disabled: positional legacy delivery arguments are unsupported"
        )
    if source is None or target is None:
        raise NativeDeliveryError("native_ui_disabled: source_draft and target_root are required")
    # These are the supported fixed-path mirror options.  Remove them before
    # checking for retired UI arguments so the compatibility entrypoint can
    # accept the same explicit delivery knobs as ``mirror_draft_tree``.
    draft_name = kwargs.pop("draft_name", None)
    receipt_path = kwargs.pop("receipt_path", None)
    require_target_root_exists = kwargs.pop("require_target_root_exists", True)
    quiet_window_seconds = kwargs.pop("quiet_window_seconds", DEFAULT_QUIET_WINDOW_SECONDS)
    poll_interval_seconds = kwargs.pop("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
    timeout_seconds = kwargs.pop("timeout_seconds", DEFAULT_STABILITY_TIMEOUT_SECONDS)
    retry_count = kwargs.pop("retry_count", DEFAULT_STABILITY_RETRY_COUNT)
    unsupported = ", ".join(sorted(str(key) for key in kwargs))
    if unsupported:
        raise NativeDeliveryError(
            f"native_ui_disabled: unsupported legacy arguments: {unsupported}"
        )
    return mirror_draft_tree(
        source_draft=source,
        target_root=target,
        draft_name=draft_name,
        receipt_path=receipt_path,
        require_target_root_exists=require_target_root_exists,
        quiet_window_seconds=quiet_window_seconds,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
        retry_count=retry_count,
    )


__all__ = [
    "ConfiguredDraftRootError",
    "DELIVERY_MODE",
    "DraftMirrorError",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_QUIET_WINDOW_SECONDS",
    "DEFAULT_STABILITY_RETRY_COUNT",
    "DEFAULT_STABILITY_TIMEOUT_SECONDS",
    "NATIVE_DELIVERY_ADAPTER_VERSION",
    "NATIVE_DELIVERY_SCHEMA_VERSION",
    "NativeDeliveryError",
    "capture_draft_tree_receipt",
    "capture_tree_receipt",
    "check_native_target_path",
    "copy_verified_draft_to_desktop",
    "get_configured_jianying_draft_root",
    "mirror_draft_tree",
    "resolve_configured_native_target_root",
    "run_native_import_save_delivery",
    "validate_native_target_root",
    "verify_draft_tree_receipt",
]
