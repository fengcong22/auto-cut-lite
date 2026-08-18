from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_CLEANUP_LOCK = threading.RLock()
_AUDIO_SOURCE_BOOTSTRAP_REQUIREMENT = "intervaltree==3.1.0"
_AUDIO_PYTHON_PROBE_TIMEOUT_SECONDS = 60
_AUDIO_VENV_CREATE_TIMEOUT_SECONDS = 600
_AUDIO_PACKAGE_INSTALL_TIMEOUT_SECONDS = 7200
_SPECTRAMINI_SMOKE_ALGORITHM = "auto_cut_spectramini_style_smoke_v1"
_SPECTRAMINI_SMOKE_REQUIRED_CHECKS = (
    "int16_output",
    "shape_preserved",
    "finite_output",
    "breath_rms_reduced",
    "click_peak_reduced",
    "memory_roundtrip_ok",
    "feature_finite",
    "deterministic",
)


def _lexical_absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _same_lexical_path(left: str | Path, right: str | Path) -> bool:
    left_value = os.path.normcase(os.path.normpath(str(_lexical_absolute(left))))
    right_value = os.path.normcase(os.path.normpath(str(_lexical_absolute(right))))
    return left_value == right_value


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _is_reparse_point(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(file_attributes & reparse_flag)


def _has_reparse_ancestor(path: str | Path) -> bool:
    current = _lexical_absolute(path)
    while True:
        if os.path.lexists(current) and _is_reparse_point(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _has_reparse_component(path: Path, *, root: Path) -> bool:
    path = _lexical_absolute(path)
    root = _lexical_absolute(root)
    if _has_reparse_ancestor(root) or not _is_within(path, root):
        return True
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if os.path.lexists(current) and _is_reparse_point(current):
            return True
    return False


def _safe_tree_size(path: Path) -> int | None:
    """Return bytes for a tree only when no reparse point is encountered."""
    try:
        path_stat = path.lstat()
    except OSError:
        return None
    if _is_reparse_point(path):
        return None
    if stat.S_ISREG(path_stat.st_mode):
        return path_stat.st_size
    if not stat.S_ISDIR(path_stat.st_mode):
        return 0

    total = 0
    try:
        for current, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
            current_path = Path(current)
            if _is_reparse_point(current_path):
                return None
            safe_dirnames: list[str] = []
            for name in dirnames:
                child = current_path / name
                if _is_reparse_point(child):
                    return None
                safe_dirnames.append(name)
            dirnames[:] = safe_dirnames
            for name in filenames:
                child = current_path / name
                if _is_reparse_point(child):
                    return None
                child_stat = child.lstat()
                if stat.S_ISREG(child_stat.st_mode):
                    total += child_stat.st_size
    except OSError:
        return None
    return total


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _file_matches(path: Path, *, expected_size: int, expected_sha256: str) -> bool:
    try:
        return path.stat().st_size == expected_size and _sha256_file(path) == expected_sha256
    except OSError:
        return False


def _read_runtime_env(root: Path, env_path: str | Path | None = None) -> dict[str, str]:
    path = _lexical_absolute(env_path) if env_path else root / ".env"
    values: dict[str, str] = {}
    if path.exists():
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            raw_lines = []
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in (
        "AUDIO_SOUND_RESPIRO_REPO",
        "AUDIO_SOUND_RESPIRO_WEIGHTS",
    ):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def _resolve_runtime_path(value: object, *, root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    if path.is_absolute():
        return _lexical_absolute(path)
    if path.anchor or path.drive or path.root or ".." in path.parts:
        return None
    root = _lexical_absolute(root)
    resolved = _lexical_absolute(root / path)
    if not _is_within(resolved, root):
        return None
    return resolved


def apply_external_model_execution_policy(runtime_report: dict[str, Any]) -> dict[str, Any]:
    report = dict(runtime_report)
    policy_note = (
        "External model execution is disabled until immutable asset binding and "
        "per-run execution receipts are available."
    )
    for component_name in ("deepfilternet", "respiro_en"):
        raw_component = runtime_report.get(component_name)
        component = dict(raw_component) if isinstance(raw_component, dict) else {}
        component["asset_verification_ok"] = bool(
            component.get("asset_verification_ok") is True or component.get("ok") is True
        )
        component["ok"] = False
        component["execution_status"] = "external_unavailable"
        existing_notes = str(component.get("notes") or "").strip()
        if policy_note not in existing_notes:
            component["notes"] = " ".join(value for value in (existing_notes, policy_note) if value)
        report[component_name] = component

    required_ok = all(
        isinstance(report.get(name), dict) and report[name].get("ok") is True
        for name in ("python", "ffmpeg", "ffprobe")
    )
    status = "degraded" if required_ok else "unavailable"
    report.update(
        {
            "status": status,
            "full": False,
            "degraded": status == "degraded",
            "unavailable": status == "unavailable",
            "execution_policy": "external_models_fail_closed",
        }
    )
    return report


def _parse_expected_size(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _parse_sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        return None
    return normalized


def _validate_package_index_url(index_url: str | None) -> dict[str, Any]:
    if index_url is None:
        return {"ok": True, "code": "ok", "reason": "", "data": {}}
    invalid = (
        not isinstance(index_url, str)
        or not index_url
        or "\\" in index_url
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in index_url
        )
    )
    parsed = None
    if not invalid:
        try:
            parsed = urlsplit(index_url)
            parsed.port
        except ValueError:
            invalid = True
    if (
        invalid
        or parsed is None
        or parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        return {
            "ok": False,
            "code": "invalid_package_index_url",
            "reason": (
                "Package index URL must be HTTPS with a hostname and no "
                "credentials, query, fragment, whitespace, or control characters."
            ),
            "data": {},
        }
    return {
        "ok": True,
        "code": "ok",
        "reason": "",
        "data": {"package_index_url": index_url},
    }


def _validate_local_wheel(
    root: Path,
    local_wheel: str | Path | None,
    expected_sha256: str | None,
) -> dict[str, Any]:
    if bool(local_wheel) != bool(expected_sha256):
        return {
            "ok": False,
            "code": "local_wheel_arguments_incomplete",
            "reason": "--local-wheel and --local-wheel-sha256 must be supplied together.",
            "data": {},
        }
    if not local_wheel:
        return {"ok": True, "code": "ok", "reason": "", "data": {}}

    digest = _parse_sha256(expected_sha256)
    if digest is None:
        return {
            "ok": False,
            "code": "invalid_local_wheel_sha256",
            "reason": "Local wheel SHA-256 must contain exactly 64 hexadecimal characters.",
            "data": {},
        }

    root = _lexical_absolute(root)
    source = Path(local_wheel)
    source = _lexical_absolute(source if source.is_absolute() else root / source)
    if (
        not _is_within(source, root)
        or _has_reparse_component(source, root=root)
        or source.suffix.lower() != ".whl"
        or not source.is_file()
    ):
        return {
            "ok": False,
            "code": "unsafe_local_wheel",
            "reason": "Local wheel must be a regular .whl file inside the repository.",
            "data": {"path": str(source)},
        }

    try:
        size = source.stat().st_size
    except OSError:
        size = 0
    actual_sha256 = _sha256_file(source)
    if actual_sha256 != digest:
        return {
            "ok": False,
            "code": "local_wheel_sha256_mismatch",
            "reason": "Local wheel SHA-256 does not match the supplied digest.",
            "data": {
                "path": str(source),
                "size": size,
                "actual_sha256": actual_sha256,
            },
        }
    return {
        "ok": True,
        "code": "ok",
        "reason": "",
        "data": {
            "path": str(source),
            "filename": source.name,
            "size": size,
            "sha256": digest,
        },
    }


def _trusted_adapter_relative_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip())
    if path.is_absolute() or not path.parts or path.parts[0] != "audio_sound" or ".." in path.parts:
        return None
    return path


def _read_trusted_repo_file(root: Path, relative_path: Path) -> tuple[bytes | None, str]:
    if relative_path.is_absolute() or not relative_path.parts:
        return None, "trusted repository path must be relative"
    path = _lexical_absolute(root / relative_path)
    root = _lexical_absolute(root)
    if not _is_within(path, root) or _has_reparse_component(path, root=root):
        return None, "trusted repository path escapes through a link or reparse point"
    git_marker = root / ".git"
    if not os.path.lexists(git_marker) or _is_reparse_point(git_marker):
        return None, "trusted repository verification requires package-local .git metadata"
    try:
        current_bytes = path.read_bytes()
        head_copy = subprocess.run(
            ["git", "show", f"HEAD:{relative_path.as_posix()}"],
            cwd=str(root),
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"trusted repository file could not be verified: {exc}"
    if head_copy.returncode != 0:
        return None, "trusted repository file is not tracked in HEAD"
    if current_bytes != head_copy.stdout:
        return None, "trusted repository file differs from HEAD"
    return current_bytes, ""


def _run_trusted_adapter(
    *,
    python_executable: str,
    adapter_bytes: bytes,
    display_path: Path,
    arguments: list[str],
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    runner = (
        "import sys\n"
        "source = sys.stdin.buffer.read()\n"
        "display_path = sys.argv[1]\n"
        "sys.argv = sys.argv[1:]\n"
        "namespace = {'__name__': '__main__', '__file__': display_path, "
        "'__package__': None}\n"
        "exec(compile(source, display_path, 'exec'), namespace, namespace)\n"
    )
    return subprocess.run(
        [
            python_executable,
            "-I",
            "-c",
            runner,
            str(display_path),
            *arguments,
        ],
        cwd=str(cwd),
        input=adapter_bytes,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _completed_output_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _timeout_step(command: list[str], exc: subprocess.TimeoutExpired) -> dict[str, Any]:
    return {
        "command": command,
        "returncode": 124,
        "stdout": _completed_output_text(exc.stdout or exc.output).strip(),
        "stderr": _completed_output_text(exc.stderr).strip()
        or f"Command timed out after {exc.timeout} seconds.",
        "timed_out": True,
        "timeout_seconds": exc.timeout,
    }


def _load_runtime_asset_section(
    *,
    root: Path,
    env_path: str | Path | None,
    asset_name: str,
) -> tuple[dict[str, Any] | None, str]:
    values = _read_runtime_env(root, env_path)
    manifest_relative = Path("audio_sound") / "runtime-manifest.json"
    manifest_path = root / manifest_relative
    if not manifest_path.exists():
        return None, "verified runtime manifest is unavailable"
    manifest_bytes, trust_error = _read_trusted_repo_file(root, manifest_relative)
    if manifest_bytes is None:
        return None, trust_error
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"runtime manifest could not be read: {exc}"
    if not isinstance(manifest, dict):
        return None, "runtime manifest must be a JSON object"
    assets = manifest.get("assets")
    section = assets.get(asset_name) if isinstance(assets, dict) else manifest.get(asset_name)
    if not isinstance(section, dict):
        return None, f"runtime manifest has no {asset_name} asset section"
    section = dict(section)
    section["_manifest_path"] = str(manifest_path)
    section["_env"] = values
    return section, ""


def _verify_respiro_runtime(
    *,
    repo_root: str | Path | None,
    python_executable: str,
    env_path: str | Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "static_assets_ok": False,
        "identity": "",
        "notes": "No verified Respiro-en asset manifest, weights hash, and executable adapter are configured.",
    }
    if repo_root is None:
        return result
    root = _lexical_absolute(repo_root)
    if _has_reparse_ancestor(root):
        result["notes"] = "Repository root is not a safe lexical path."
        return result
    section, error = _load_runtime_asset_section(
        root=root, env_path=env_path, asset_name="respiro_en"
    )
    if section is None:
        result["notes"] = error
        return result

    env_values = section.pop("_env")
    section.pop("_manifest_path", None)
    repo_info = section.get("repo") if isinstance(section.get("repo"), dict) else {}
    weights_info = section.get("weights") if isinstance(section.get("weights"), dict) else {}
    adapter_info = section.get("adapter") if isinstance(section.get("adapter"), dict) else {}
    repo_path = _resolve_runtime_path(
        env_values.get("AUDIO_SOUND_RESPIRO_REPO"),
        root=root,
    )
    weights_path = _resolve_runtime_path(
        env_values.get("AUDIO_SOUND_RESPIRO_WEIGHTS"),
        root=root,
    )
    adapter_value = section.get("adapter_path") or adapter_info.get("path")
    adapter_relative = _trusted_adapter_relative_path(adapter_value)
    adapter_bytes: bytes | None = None
    adapter_trust_error = "Respiro adapter path is missing or not under audio_sound/."
    if adapter_relative is not None:
        adapter_bytes, adapter_trust_error = _read_trusted_repo_file(root, adapter_relative)
    adapter_path = (
        root / adapter_relative if adapter_relative and adapter_bytes is not None else None
    )
    revision = (
        section.get("revision") or section.get("source_revision") or repo_info.get("revision")
    )
    weights_sha256 = _parse_sha256(section.get("weights_sha256") or weights_info.get("sha256"))
    weights_size = _parse_expected_size(section.get("weights_size") or weights_info.get("size"))
    adapter_sha256 = _parse_sha256(section.get("adapter_sha256") or adapter_info.get("sha256"))
    adapter_version = section.get("adapter_version") or adapter_info.get("version")
    license_id = section.get("license") or section.get("license_id") or repo_info.get("license")
    if adapter_bytes is None:
        result["notes"] = adapter_trust_error
        return result
    if not all(
        (
            repo_path,
            weights_path,
            adapter_path,
            revision,
            weights_sha256,
            weights_size,
            adapter_sha256,
            adapter_version,
            license_id,
        )
    ):
        result["notes"] = (
            "Respiro manifest is missing a pinned revision, license, size/hash, or adapter identity."
        )
        return result
    if not repo_path.is_dir() or not weights_path.is_file() or not adapter_path.is_file():
        result["notes"] = "Configured Respiro repository, weights, or adapter path is missing."
        return result
    if not _file_matches(weights_path, expected_size=weights_size, expected_sha256=weights_sha256):
        result["notes"] = "Respiro weights failed the manifest size or SHA-256 check."
        return result
    if hashlib.sha256(adapter_bytes).hexdigest() != adapter_sha256:
        result["notes"] = "Respiro adapter failed the manifest SHA-256 check."
        return result

    result.update(
        {
            "static_assets_ok": True,
            "identity": f"respiro-en@{revision};weights={weights_sha256};adapter={adapter_version}",
            "notes": (
                "Respiro-en manifest, weights, license, and adapter bytes are present, but the "
                "repository revision and execution are not verified."
            ),
        }
    )
    return result


def _verify_deepfilternet_model(
    *,
    repo_root: str | Path | None,
    env_path: str | Path | None = None,
) -> dict[str, Any]:
    result = {
        "ok": False,
        "identity": "",
        "notes": "No verified DeepFilterNet model manifest is configured.",
    }
    if repo_root is None:
        return result
    root = _lexical_absolute(repo_root)
    section, error = _load_runtime_asset_section(
        root=root, env_path=env_path, asset_name="deepfilternet"
    )
    if section is None:
        result["notes"] = error
        return result
    model_info = section.get("model") if isinstance(section.get("model"), dict) else section
    adapter_info = section.get("adapter") if isinstance(section.get("adapter"), dict) else {}
    model_path = _resolve_runtime_path(model_info.get("path"), root=root)
    expected_hash = _parse_sha256(model_info.get("sha256"))
    expected_size = _parse_expected_size(model_info.get("size"))
    version = model_info.get("version")
    license_id = model_info.get("license") or model_info.get("license_id")
    adapter_value = section.get("adapter_path") or adapter_info.get("path")
    adapter_relative = _trusted_adapter_relative_path(adapter_value)
    adapter_bytes: bytes | None = None
    adapter_trust_error = "DeepFilterNet adapter path is missing or not under audio_sound/."
    if adapter_relative is not None:
        adapter_bytes, adapter_trust_error = _read_trusted_repo_file(root, adapter_relative)
    adapter_path = (
        root / adapter_relative if adapter_relative and adapter_bytes is not None else None
    )
    adapter_hash = _parse_sha256(section.get("adapter_sha256") or adapter_info.get("sha256"))
    adapter_version = section.get("adapter_version") or adapter_info.get("version")
    if adapter_bytes is None:
        result["notes"] = adapter_trust_error
        return result
    if not all(
        (
            model_path,
            expected_hash,
            expected_size,
            version,
            license_id,
            adapter_path,
            adapter_hash,
            adapter_version,
        )
    ):
        result["notes"] = "DeepFilterNet model manifest has invalid or incomplete identity fields."
        return result
    if not model_path.is_file():
        result["notes"] = "Configured DeepFilterNet model path is missing."
        return result
    if not _file_matches(model_path, expected_size=expected_size, expected_sha256=expected_hash):
        result["notes"] = "DeepFilterNet model failed the manifest size or SHA-256 check."
        return result
    if hashlib.sha256(adapter_bytes).hexdigest() != adapter_hash:
        result["notes"] = "DeepFilterNet adapter failed the manifest SHA-256 check."
        return result
    result.update(
        {
            "ok": True,
            "identity": f"{version};model={expected_hash};adapter={adapter_version}",
            "notes": "Verified DeepFilterNet model and trusted adapter identities.",
            "model_path": str(model_path),
            "model_sha256": expected_hash,
            "adapter_path": str(adapter_path),
            "adapter_version": str(adapter_version),
            "_adapter_bytes": adapter_bytes,
            "_repo_root": str(root),
        }
    )
    return result


def _cleanup_lock_path(root: Path) -> Path:
    normalized_root = os.path.normcase(os.path.normpath(str(_lexical_absolute(root))))
    lock_name = (
        "auto-cut-audio-cleanup-"
        f"{hashlib.sha256(normalized_root.encode('utf-8')).hexdigest()[:20]}.lock"
    )
    return Path(tempfile.gettempdir()) / lock_name


@contextmanager
def _cleanup_mutex(root: Path):
    """Serialize setup and cleanup calls for one repository across processes."""
    lock_path = _cleanup_lock_path(root)
    with _CLEANUP_LOCK:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _remove_tree_without_reparse(path: Path, *, root: Path) -> bool:
    if _has_reparse_component(path, root=root) or _is_reparse_point(path):
        return False
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                child = Path(entry.path)
                if _has_reparse_component(child, root=root) or _is_reparse_point(child):
                    return False
                child_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(child_stat.st_mode):
                    if not _remove_tree_without_reparse(child, root=root):
                        return False
                elif stat.S_ISREG(child_stat.st_mode):
                    child.unlink()
                else:
                    return False
        if _has_reparse_component(path, root=root) or _is_reparse_point(path):
            return False
        path.rmdir()
    except OSError:
        return False
    return True


def _quarantine_and_remove(path: Path, *, root: Path, original_size: int) -> dict[str, Any]:
    """Atomically detach a target before deleting its contents."""
    if not os.path.lexists(path):
        return {
            "removed": False,
            "quarantine_path": "",
            "error": "Target disappeared before it could be quarantined.",
            "bytes_reclaimed": 0,
        }
    if _has_reparse_component(path, root=root):
        return {
            "removed": False,
            "quarantine_path": "",
            "error": "Target became a symlink or Windows reparse point before quarantine.",
            "bytes_reclaimed": 0,
        }
    quarantine = path.with_name(f".{path.name}.codex-cleanup-{uuid.uuid4().hex}")
    try:
        path.rename(quarantine)
    except OSError as exc:
        return {
            "removed": False,
            "quarantine_path": "",
            "error": f"Target could not be quarantined: {exc}",
            "bytes_reclaimed": 0,
        }

    success = False
    try:
        if _is_reparse_point(quarantine):
            quarantine_stat = quarantine.lstat()
            if stat.S_ISDIR(quarantine_stat.st_mode):
                quarantine.rmdir()
            else:
                quarantine.unlink()
            success = True
        elif _safe_tree_size(quarantine) is not None:
            quarantine_stat = quarantine.lstat()
            if stat.S_ISDIR(quarantine_stat.st_mode):
                success = _remove_tree_without_reparse(quarantine, root=root)
            elif stat.S_ISREG(quarantine_stat.st_mode):
                quarantine.unlink()
                success = True
    except OSError:
        success = False

    if success:
        return {
            "removed": True,
            "quarantine_path": "",
            "error": "",
            "bytes_reclaimed": original_size,
        }

    if not os.path.lexists(quarantine):
        return {
            "removed": False,
            "quarantine_path": "",
            "error": "Partial cleanup: quarantined target disappeared before completion.",
            "bytes_reclaimed": 0,
        }

    if not os.path.lexists(path):
        try:
            quarantine.rename(path)
        except OSError as exc:
            remaining_size = _safe_tree_size(quarantine)
            return {
                "removed": False,
                "quarantine_path": str(quarantine),
                "error": f"Partial cleanup; quarantine rollback failed: {exc}",
                "bytes_reclaimed": (
                    max(0, original_size - remaining_size) if remaining_size is not None else 0
                ),
            }
        remaining_size = _safe_tree_size(path)
        return {
            "removed": False,
            "quarantine_path": "",
            "error": "Partial cleanup: remaining contents were restored, but deleted contents cannot be rolled back.",
            "bytes_reclaimed": (
                max(0, original_size - remaining_size) if remaining_size is not None else 0
            ),
        }

    remaining_size = _safe_tree_size(quarantine)
    return {
        "removed": False,
        "quarantine_path": str(quarantine),
        "error": "Partial cleanup; rollback could not replace the recreated target.",
        "bytes_reclaimed": (
            max(0, original_size - remaining_size) if remaining_size is not None else 0
        ),
    }


def _is_python_311(version: str) -> bool:
    parts = version.split(".")
    return (
        len(parts) >= 3
        and parts[0] == "3"
        and parts[1] == "11"
        and bool(parts[2])
        and parts[2][0].isdigit()
    )


def _find_unsafe_audio_runtime_path(
    root: Path, *, extra_paths: tuple[Path, ...] = ()
) -> Path | None:
    paths = (
        root / ".venv-audio",
        root / ".cargo-home" / "audio-sound",
        *extra_paths,
    )
    for path in paths:
        if _has_reparse_component(path, root=root):
            return path
        if os.path.lexists(path) and _safe_tree_size(path) is None:
            return path
    return None


def _install_safety_failure(
    root: Path,
    *,
    steps: list[dict[str, Any]],
    extra_paths: tuple[Path, ...] = (),
) -> dict[str, Any] | None:
    if _has_reparse_ancestor(root):
        return {
            "ok": False,
            "code": "unsafe_repo_root",
            "reason": "The repository root or one of its parents became a symlink or Windows reparse point.",
            "data": {"repo_root": str(root), "steps": steps},
        }
    unsafe_path = _find_unsafe_audio_runtime_path(root, extra_paths=extra_paths)
    if unsafe_path is not None:
        return {
            "ok": False,
            "code": "unsafe_runtime_path",
            "reason": "An audio runtime tree contains a symlink, Windows reparse point, or unreadable entry.",
            "data": {"path": str(unsafe_path), "steps": steps},
        }
    return None


def _isolated_install_environment(
    *,
    cargo_home: Path | None = None,
    install_temp: Path | None = None,
    offline: bool = False,
) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        upper = key.upper()
        if upper.startswith("PIP_") or (offline and upper.endswith("_PROXY")):
            env.pop(key, None)
    for key in ("PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_NO_CACHE_DIR"] = "1"
    if offline:
        env["PIP_NO_INDEX"] = "1"
        env["HTTP_PROXY"] = "http://127.0.0.1:9"
        env["HTTPS_PROXY"] = "http://127.0.0.1:9"
        env["ALL_PROXY"] = "http://127.0.0.1:9"
        env["NO_PROXY"] = ""
    if cargo_home is not None:
        env["CARGO_HOME"] = str(cargo_home)
    if install_temp is not None:
        for key in ("TEMP", "TMP", "TMPDIR"):
            env[key] = str(install_temp)
    return env


def _create_install_staging(root: Path) -> Path:
    for _attempt in range(10):
        staging = root / f".venv-audio.codex-staging-{uuid.uuid4().hex}"
        try:
            staging.mkdir(mode=0o700)
        except FileExistsError:
            continue
        if _has_reparse_component(staging, root=root) or _safe_tree_size(staging) is None:
            raise OSError(f"created staging path is unsafe: {staging}")
        return staging
    raise OSError("could not allocate a unique audio environment staging directory")


def _discard_install_staging(staging: Path, *, root: Path) -> dict[str, Any]:
    if not os.path.lexists(staging):
        return {"ok": True, "path": "", "error": ""}
    size = _safe_tree_size(staging)
    if size is None:
        return {
            "ok": False,
            "path": str(staging),
            "error": "Unsafe install staging was retained for manual inspection.",
        }
    outcome = _quarantine_and_remove(staging, root=root, original_size=size)
    return {
        "ok": bool(outcome["removed"]),
        "path": str(outcome["quarantine_path"] or staging),
        "error": str(outcome["error"]),
    }


def _promote_install_staging(staging: Path, *, root: Path) -> dict[str, Any]:
    target = root / ".venv-audio"
    backup: Path | None = None
    backup_size = 0

    if _has_reparse_ancestor(root):
        return {
            "ok": False,
            "error": "Repository root became unsafe before environment promotion.",
            "backup_path": "",
            "rejected_path": "",
        }

    if os.path.lexists(target):
        measured_size = _safe_tree_size(target)
        if measured_size is None or _has_reparse_component(target, root=root):
            return {
                "ok": False,
                "error": "Existing .venv-audio became unsafe before promotion.",
                "backup_path": "",
                "rejected_path": "",
            }
        backup_size = measured_size
        backup = root / f".venv-audio.codex-backup-{uuid.uuid4().hex}"
        try:
            target.rename(backup)
        except OSError as exc:
            if os.path.lexists(target):
                return {
                    "ok": False,
                    "error": f"Existing .venv-audio could not be detached: {exc}",
                    "backup_path": "",
                    "rejected_path": "",
                }
            backup = None

    try:
        staging.rename(target)
    except OSError as exc:
        restore_error = ""
        if backup is not None and os.path.lexists(backup) and not os.path.lexists(target):
            try:
                backup.rename(target)
            except OSError as restore_exc:
                restore_error = f"; previous environment restore failed: {restore_exc}"
        return {
            "ok": False,
            "error": f"Staged environment promotion failed: {exc}{restore_error}",
            "backup_path": str(backup) if backup and os.path.lexists(backup) else "",
            "rejected_path": "",
        }

    if _has_reparse_component(target, root=root) or _safe_tree_size(target) is None:
        rejected = root / f".venv-audio.codex-staging-rejected-{uuid.uuid4().hex}"
        rejection_error = ""
        try:
            target.rename(rejected)
        except OSError as exc:
            rejection_error = f"; unsafe environment detach failed: {exc}"
        restore_error = ""
        if backup is not None and os.path.lexists(backup) and not os.path.lexists(target):
            try:
                backup.rename(target)
            except OSError as exc:
                restore_error = f"; previous environment restore failed: {exc}"
        return {
            "ok": False,
            "error": (
                "Promoted .venv-audio failed the final no-reparse scan"
                f"{rejection_error}{restore_error}."
            ),
            "backup_path": str(backup) if backup and os.path.lexists(backup) else "",
            "rejected_path": str(rejected) if os.path.lexists(rejected) else "",
        }

    if backup is not None and os.path.lexists(backup):
        current_backup_size = _safe_tree_size(backup)
        if current_backup_size is None:
            return {
                "ok": False,
                "error": "Previous audio environment backup became unsafe and was retained.",
                "backup_path": str(backup),
                "rejected_path": "",
            }
        cleanup = _quarantine_and_remove(
            backup,
            root=root,
            original_size=max(backup_size, current_backup_size),
        )
        if not cleanup["removed"]:
            return {
                "ok": False,
                "error": str(cleanup["error"]),
                "backup_path": str(cleanup["quarantine_path"] or backup),
                "rejected_path": "",
            }

    return {"ok": True, "error": "", "backup_path": "", "rejected_path": ""}


def build_install_commands(
    *,
    repo_root: str | Path,
    python_executable: str | None = None,
    local_wheel: str | Path | None = None,
    local_wheel_sha256: str | None = None,
    index_url: str | None = None,
    offline_wheelhouse: str | Path | None = None,
) -> list[list[str]]:
    root = Path(repo_root)
    python_bin = python_executable or str(root / ".venv-audio" / "Scripts" / "python.exe")
    wheelhouse = _lexical_absolute(offline_wheelhouse) if offline_wheelhouse else None
    if wheelhouse is not None and (index_url or local_wheel or local_wheel_sha256):
        raise ValueError("Offline wheelhouse cannot be combined with network or local-wheel sources.")
    requirements_root = wheelhouse.parent.parent / "requirements" if wheelhouse else root
    build_lock_path = requirements_root / "requirements-audio-build.lock"
    lock_path = requirements_root / (
        "requirements-offline-audio.lock" if wheelhouse else "requirements-audio.lock"
    )
    if bool(local_wheel) != bool(local_wheel_sha256):
        raise ValueError("A local wheel and its SHA-256 must be supplied together.")
    local_requirement = ""
    if local_wheel:
        digest = _parse_sha256(local_wheel_sha256)
        if digest is None:
            raise ValueError("Local wheel SHA-256 must be 64 hexadecimal characters.")
        wheel_uri = _lexical_absolute(local_wheel).as_uri()
        local_requirement = f"{wheel_uri}#sha256={digest}"
    index_arguments = ["--index-url", index_url] if index_url else []
    if wheelhouse is not None:
        index_arguments = ["--no-index", "--find-links", str(wheelhouse)]
    pip_install_prefix = [
        python_bin,
        "-I",
        "-m",
        "pip",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--timeout",
        "60",
        "--retries",
        "10",
        "install",
        *index_arguments,
    ]
    if wheelhouse is not None:
        return [
            [
                *pip_install_prefix,
                "--only-binary=:all:",
                "--require-hashes",
                "--requirement",
                str(lock_path),
            ],
            [python_bin, "-I", "-m", "pip", "check"],
        ]
    commands = [
        [
            *pip_install_prefix,
            "--requirement",
            str(build_lock_path),
        ],
        [
            *pip_install_prefix,
            "--no-build-isolation",
            "--no-deps",
            _AUDIO_SOURCE_BOOTSTRAP_REQUIREMENT,
        ],
        [
            *pip_install_prefix,
            "--requirement",
            str(lock_path),
        ],
        [python_bin, "-I", "-m", "pip", "check"],
    ]
    if local_requirement:
        commands.insert(
            0,
            [
                python_bin,
                "-I",
                "-m",
                "pip",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "install",
                "--no-deps",
                "--require-hashes",
                local_requirement,
            ],
        )
    return commands


def build_respiro_setup_commands(
    *, repo_root: str | Path, tools_dir: str | Path
) -> list[list[str]]:
    raise RuntimeError(
        "Automatic Respiro download requires a verified runtime manifest with pinned "
        "revision, size, license, and SHA-256. Configure existing verified assets via .env."
    )


def _inspect_python_runtime(
    python_executable: str,
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    controlled_repo_root = _lexical_absolute(
        repo_root if repo_root is not None else Path(__file__).resolve().parents[1]
    )
    script = """
import importlib
import importlib.metadata
import json
import sys
from pathlib import Path


repo_root = Path(sys.argv[1])
sys.path.insert(0, str(repo_root))


def probe_module(name):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except Exception:
        return ""


deepfilter_version = package_version("deepfilternet")
deepfilter_ok = bool(deepfilter_version)
spectramini_modules = {
    name: probe_module(name)
    for name in ("librosa", "numpy", "scipy", "soundfile")
}
spectramini_required_checks = (
    "int16_output",
    "shape_preserved",
    "finite_output",
    "breath_rms_reduced",
    "click_peak_reduced",
    "memory_roundtrip_ok",
    "feature_finite",
    "deterministic",
)
spectramini_algorithm_identity = "auto_cut_spectramini_style_smoke_v1"
spectramini_smoke_status = "dependencies_unavailable"
spectramini_smoke_checks = {name: False for name in spectramini_required_checks}
spectramini_smoke_metrics = {}
spectramini_smoke_error = ""
spectramini_dependencies_ok = all(
    module is not None for module in spectramini_modules.values()
)
if spectramini_dependencies_ok:
    try:
        from audio_sound.pipeline import run_spectramini_style_smoke

        smoke_report = run_spectramini_style_smoke()
        spectramini_algorithm_identity = str(
            smoke_report.get("algorithm_identity") or ""
        )
        spectramini_smoke_status = str(smoke_report.get("smoke_status") or "failed")
        spectramini_smoke_checks = dict(smoke_report.get("checks") or {})
        spectramini_smoke_metrics = dict(smoke_report.get("metrics") or {})
        spectramini_smoke_error = str(smoke_report.get("error") or "")
    except Exception as exc:
        spectramini_smoke_status = "failed"
        spectramini_smoke_checks = {
            name: False for name in spectramini_required_checks
        }
        spectramini_smoke_error = f"{type(exc).__name__}: {exc}"

spectramini_ok = (
    spectramini_smoke_status == "passed"
    and spectramini_algorithm_identity == "auto_cut_spectramini_style_smoke_v1"
    and all(
        spectramini_smoke_checks.get(name) is True
        for name in spectramini_required_checks
    )
)
print(json.dumps({
    "ok": True,
    "path": sys.executable,
    "version": sys.version.split()[0],
    "deepfilternet_ok": deepfilter_ok,
    "deepfilternet_identity": f"deepfilternet@{deepfilter_version}" if deepfilter_ok else "",
    "spectramini_runtime_ok": spectramini_ok,
    "spectramini_identity": ";".join([
        f"algorithm@{spectramini_algorithm_identity}",
        *(
            f"{name}@{package_version(name)}"
            for name in spectramini_modules
            if spectramini_modules[name]
        ),
    ]) if spectramini_ok else "",
    "spectramini_smoke_status": spectramini_smoke_status,
    "spectramini_algorithm_identity": spectramini_algorithm_identity,
    "spectramini_smoke_checks": spectramini_smoke_checks,
    "spectramini_smoke_metrics": spectramini_smoke_metrics,
    "spectramini_smoke_error": spectramini_smoke_error,
}))
"""
    try:
        completed = subprocess.run(
            [python_executable, "-I", "-c", script, str(controlled_repo_root)],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "path": python_executable,
            "version": "",
            "deepfilternet_ok": False,
            "deepfilternet_identity": "",
            "spectramini_runtime_ok": False,
            "spectramini_identity": "",
            "spectramini_smoke_status": "not_run",
            "spectramini_algorithm_identity": "",
            "spectramini_smoke_checks": {},
            "spectramini_smoke_metrics": {},
            "error": str(exc),
        }
    if completed.returncode != 0:
        return {
            "ok": False,
            "path": python_executable,
            "version": "",
            "deepfilternet_ok": False,
            "deepfilternet_identity": "",
            "spectramini_runtime_ok": False,
            "spectramini_identity": "",
            "spectramini_smoke_status": "not_run",
            "spectramini_algorithm_identity": "",
            "spectramini_smoke_checks": {},
            "spectramini_smoke_metrics": {},
            "error": (completed.stderr or completed.stdout or "").strip(),
        }
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError:
        return {
            "ok": False,
            "path": python_executable,
            "version": "",
            "deepfilternet_ok": False,
            "deepfilternet_identity": "",
            "spectramini_runtime_ok": False,
            "spectramini_identity": "",
            "spectramini_smoke_status": "not_run",
            "spectramini_algorithm_identity": "",
            "spectramini_smoke_checks": {},
            "spectramini_smoke_metrics": {},
            "error": completed.stdout.strip(),
        }
    smoke_status = payload.get("spectramini_smoke_status")
    smoke_algorithm = payload.get("spectramini_algorithm_identity")
    smoke_checks = payload.get("spectramini_smoke_checks")
    smoke_evidence_ok = (
        payload.get("spectramini_runtime_ok") is True
        and smoke_status == "passed"
        and smoke_algorithm == _SPECTRAMINI_SMOKE_ALGORITHM
        and isinstance(smoke_checks, dict)
        and all(smoke_checks.get(name) is True for name in _SPECTRAMINI_SMOKE_REQUIRED_CHECKS)
    )
    payload["spectramini_runtime_ok"] = smoke_evidence_ok
    if not smoke_evidence_ok:
        payload["spectramini_identity"] = ""
        if not smoke_status:
            payload["spectramini_smoke_status"] = "evidence_missing"
        elif smoke_status == "passed":
            payload["spectramini_smoke_status"] = "evidence_invalid"
    payload.setdefault("spectramini_algorithm_identity", "")
    payload.setdefault("spectramini_smoke_checks", {})
    payload.setdefault("spectramini_smoke_metrics", {})
    return payload


def _inspect_binary(path: str | None, *, expected_program: str) -> dict[str, Any]:
    if not path:
        return {"ok": False, "path": "", "version": "", "identity": "", "error": "not found"}
    try:
        completed = subprocess.run(
            [path, "-version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "path": path, "version": "", "identity": path, "error": str(exc)}
    version = (completed.stdout or completed.stderr or "").splitlines()
    version_line = version[0].strip() if version else ""
    expected_identity = f"{expected_program.lower()} version "
    identity_ok = version_line.lower().startswith(expected_identity)
    ok = completed.returncode == 0 and identity_ok
    if completed.returncode != 0:
        error = f"binary probe exited {completed.returncode}"
    elif not identity_ok:
        error = f"binary identity is not {expected_program}"
    else:
        error = ""
    return {
        "ok": ok,
        "path": path,
        "version": version_line,
        "identity": (
            f"{expected_program.lower()}@{version_line}"
            if version_line
            else expected_program.lower()
        ),
        "error": error,
    }


def _probe_deepfilternet_adapter(python_executable: str, runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "command": [],
        "error": (
            "DeepFilterNet adapter execution is disabled until immutable asset binding and "
            "per-run execution receipts are available."
        ),
    }


def detect_runtime(
    *,
    python_executable: str | None = None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    repo_root: str | Path | None = None,
    env_path: str | Path | None = None,
) -> dict[str, Any]:
    requested_python = python_executable or sys.executable
    expected_repo_python: Path | None = None
    python_target_error = ""
    if repo_root is not None:
        root = _lexical_absolute(repo_root)
        expected_repo_python = _lexical_absolute(root / ".venv-audio" / "Scripts" / "python.exe")
        if _has_reparse_ancestor(root):
            python_target_error = "Repository root is not a safe lexical path."
        elif not _same_lexical_path(requested_python, expected_repo_python):
            python_target_error = (
                f"Runtime doctor requires the repository .venv-audio interpreter: "
                f"{expected_repo_python}"
            )
        elif _has_reparse_component(expected_repo_python, root=root):
            python_target_error = "Repository .venv-audio contains a link or reparse point."

    if python_target_error:
        python_info: dict[str, Any] = {
            "ok": False,
            "path": str(requested_python),
            "version": "",
            "deepfilternet_ok": False,
            "deepfilternet_identity": "",
            "spectramini_runtime_ok": False,
            "spectramini_identity": "",
            "error": python_target_error,
        }
    else:
        python_info = _inspect_python_runtime(
            str(requested_python),
            repo_root=repo_root,
        )

    python_path = str(python_info.get("path") or requested_python)
    if (
        expected_repo_python is not None
        and python_info.get("ok")
        and not _same_lexical_path(python_path, expected_repo_python)
    ):
        python_target_error = (
            f"Runtime probe escaped the repository .venv-audio interpreter: {python_path}"
        )
    python_version = str(python_info.get("version") or "")
    python_ok = (
        not python_target_error and bool(python_info.get("ok")) and _is_python_311(python_version)
    )
    ffmpeg_path = shutil.which(ffmpeg_bin)
    ffprobe_path = shutil.which(ffprobe_bin)
    ffmpeg_component = _inspect_binary(ffmpeg_path, expected_program="ffmpeg")
    ffprobe_component = _inspect_binary(ffprobe_path, expected_program="ffprobe")
    respiro_verified = (
        _verify_respiro_runtime(
            repo_root=repo_root,
            python_executable=str(requested_python),
            env_path=env_path,
        )
        if python_ok
        else {
            "ok": False,
            "identity": "",
            "notes": "Python 3.11 audio runtime is unavailable.",
        }
    )
    deepfilter_model = _verify_deepfilternet_model(repo_root=repo_root, env_path=env_path)
    deepfilter_module_ok = bool(python_info.get("deepfilternet_ok"))
    deepfilter_adapter = {
        "ok": bool(deepfilter_model["ok"]),
        "command": [],
        "error": (
            "adapter bytes were statically verified; the adapter and model were not executed"
            if deepfilter_model["ok"]
            else "module or verified model unavailable"
        ),
    }
    python_component = {
        "ok": python_ok,
        "path": python_path,
        "version": python_version,
        "identity": f"python@{python_version}" if python_version else "python",
        "error": str(
            python_target_error
            or python_info.get("error")
            or (
                "Expected Python 3.11.x."
                if python_info.get("ok") and not _is_python_311(python_version)
                else ""
            )
        ),
    }
    deepfilternet_component = {
        "ok": python_ok
        and deepfilter_module_ok
        and bool(deepfilter_model["ok"])
        and bool(deepfilter_adapter["ok"]),
        "module_ok": deepfilter_module_ok,
        "model_ok": bool(deepfilter_model["ok"]),
        "adapter_ok": bool(deepfilter_adapter["ok"]),
        "module": "df.enhance",
        "identity": ";".join(
            value
            for value in (
                str(python_info.get("deepfilternet_identity") or ""),
                str(deepfilter_model["identity"] or ""),
            )
            if value
        ),
        "notes": (
            deepfilter_model["notes"]
            if not deepfilter_model["ok"]
            else str(deepfilter_adapter["error"] or deepfilter_model["notes"])
        ),
    }
    respiro_component = {**respiro_verified}
    respiro_component["ok"] = python_ok and bool(respiro_verified["ok"])
    spectramini_component = {
        "ok": python_ok and bool(python_info.get("spectramini_runtime_ok")),
        "identity": str(python_info.get("spectramini_identity") or ""),
        "smoke_status": str(python_info.get("spectramini_smoke_status") or "not_run"),
        "algorithm_identity": str(python_info.get("spectramini_algorithm_identity") or ""),
        "smoke_checks": dict(python_info.get("spectramini_smoke_checks") or {}),
        "smoke_metrics": dict(python_info.get("spectramini_smoke_metrics") or {}),
        "notes": "SpectraMini-style breath and mouth-click stages are embedded locally via librosa/scipy helpers.",
    }
    required_ok = python_component["ok"] and ffmpeg_component["ok"] and ffprobe_component["ok"]
    optional_ok = (
        deepfilternet_component["ok"] and respiro_component["ok"] and spectramini_component["ok"]
    )
    status = "unavailable" if not required_ok else "full" if optional_ok else "degraded"
    raw_report = {
        "status": status,
        "full": status == "full",
        "degraded": status == "degraded",
        "unavailable": status == "unavailable",
        "python": python_component,
        "ffmpeg": ffmpeg_component,
        "ffprobe": ffprobe_component,
        "deepfilternet": deepfilternet_component,
        "respiro_en": respiro_component,
        "spectramini": spectramini_component,
    }
    return apply_external_model_execution_policy(raw_report)


def run_install(
    *,
    repo_root: str | Path,
    python_executable: str | None = None,
    local_wheel: str | Path | None = None,
    local_wheel_sha256: str | None = None,
    index_url: str | None = None,
    offline_wheelhouse: str | Path | None = None,
    ffmpeg_bin: str | Path = "ffmpeg",
    ffprobe_bin: str | Path = "ffprobe",
) -> dict[str, Any]:
    root = _lexical_absolute(repo_root)
    if _has_reparse_ancestor(root):
        return {
            "ok": False,
            "code": "unsafe_repo_root",
            "reason": "The repository root or one of its parents is a symlink or Windows reparse point.",
            "data": {"repo_root": str(root)},
        }
    if offline_wheelhouse is not None and (
        index_url is not None or local_wheel is not None or local_wheel_sha256 is not None
    ):
        return {
            "ok": False,
            "code": "offline_source_conflict",
            "reason": "Offline installation accepts only the verified companion wheelhouse.",
            "data": {},
        }
    normalized_wheelhouse: Path | None = None
    if offline_wheelhouse is not None:
        normalized_wheelhouse = _lexical_absolute(offline_wheelhouse)
        requirements_root = normalized_wheelhouse.parent.parent / "requirements"
        required_locks = (
            requirements_root / "requirements-offline-audio.lock",
        )
        if (
            not normalized_wheelhouse.is_dir()
            or not any(normalized_wheelhouse.glob("*.whl"))
            or any(_has_reparse_ancestor(path) for path in (normalized_wheelhouse, *required_locks))
            or not all(path.is_file() for path in required_locks)
        ):
            return {
                "ok": False,
                "code": "offline_wheelhouse_invalid",
                "reason": "The verified offline audio wheelhouse is missing or unsafe.",
                "data": {},
            }
    index_result = _validate_package_index_url(index_url)
    if not index_result["ok"]:
        return index_result
    package_index_url = index_result["data"].get("package_index_url")
    local_wheel_result = _validate_local_wheel(root, local_wheel, local_wheel_sha256)
    if not local_wheel_result["ok"]:
        if package_index_url is not None:
            local_wheel_result.setdefault("data", {}).setdefault(
                "package_index_url", package_index_url
            )
        return local_wheel_result
    local_wheel_data = local_wheel_result["data"] or None
    with _cleanup_mutex(root):
        if _has_reparse_ancestor(root):
            result = {
                "ok": False,
                "code": "unsafe_repo_root",
                "reason": "The repository root or one of its parents became a symlink or Windows reparse point.",
                "data": {"repo_root": str(root)},
            }
        else:
            result = _run_install_unlocked(
                root=root,
                python_executable=python_executable,
                local_wheel=local_wheel_data,
                index_url=package_index_url,
                offline_wheelhouse=normalized_wheelhouse,
                ffmpeg_bin=str(ffmpeg_bin),
                ffprobe_bin=str(ffprobe_bin),
            )
        result_data = result.get("data")
        if isinstance(result_data, dict) and isinstance(result_data.get("runtime"), dict):
            result_data["runtime"] = apply_external_model_execution_policy(result_data["runtime"])
        if local_wheel_data is not None:
            result.setdefault("data", {}).setdefault("local_wheel", local_wheel_data)
        if package_index_url is not None:
            result.setdefault("data", {}).setdefault("package_index_url", package_index_url)
        return result


def _run_install_unlocked(
    *,
    root: Path,
    python_executable: str | None = None,
    local_wheel: dict[str, Any] | None = None,
    index_url: str | None = None,
    offline_wheelhouse: Path | None = None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
) -> dict[str, Any]:
    final_python = _lexical_absolute(root / ".venv-audio" / "Scripts" / "python.exe")
    requested_python = _lexical_absolute(python_executable) if python_executable else final_python
    if requested_python != final_python:
        return {
            "ok": False,
            "code": "unsafe_python_target",
            "reason": "Audio dependencies may only be installed into the repository .venv-audio.",
            "data": {"requested_python": str(requested_python)},
        }

    steps: list[dict[str, Any]] = []
    safety_failure = _install_safety_failure(root, steps=steps)
    if safety_failure is not None:
        return safety_failure

    raw_candidates: list[list[str]] = []
    if sys.version_info[:2] == (3, 11):
        raw_candidates.append([sys.executable])
    py_launcher = shutil.which("py")
    python_311 = shutil.which("python3.11")
    if py_launcher:
        raw_candidates.append([py_launcher, "-3.11"])
    if python_311:
        raw_candidates.append([python_311])
    candidates: list[list[str]] = []
    seen_candidates: set[tuple[str, ...]] = set()
    for candidate in raw_candidates:
        key = tuple(os.path.normcase(os.path.normpath(part)) for part in candidate)
        if key not in seen_candidates:
            seen_candidates.add(key)
            candidates.append(candidate)

    probe_script = (
        "# auto_cut_audio_python_creator_probe_v1\n"
        "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) "
        "and sys.maxsize > 2**32 else 1)"
    )
    staging: Path | None = None
    staged_python: Path | None = None
    last_create_error = "A 64-bit Python 3.11 interpreter is required for .venv-audio."
    for candidate in candidates:
        probe_command = [*candidate, "-I", "-c", probe_script]
        try:
            probe = subprocess.run(
                probe_command,
                cwd=str(root),
                capture_output=True,
                check=False,
                env=_isolated_install_environment(),
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_AUDIO_PYTHON_PROBE_TIMEOUT_SECONDS,
            )
            probe_step = {
                "command": probe_command,
                "returncode": probe.returncode,
                "stdout": probe.stdout.strip(),
                "stderr": probe.stderr.strip(),
            }
        except subprocess.TimeoutExpired as exc:
            probe_step = _timeout_step(probe_command, exc)
        except OSError as exc:
            probe_step = {
                "command": probe_command,
                "returncode": None,
                "stdout": "",
                "stderr": str(exc),
            }
        steps.append(probe_step)
        if probe_step["returncode"] != 0:
            last_create_error = str(probe_step["stderr"] or last_create_error)
            continue

        try:
            candidate_staging = _create_install_staging(root)
        except OSError as exc:
            return {
                "ok": False,
                "code": "install_staging_failed",
                "reason": str(exc),
                "data": {"steps": steps},
            }
        candidate_python = candidate_staging / "Scripts" / "python.exe"
        create_command = [*candidate, "-I", "-m", "venv", str(candidate_staging)]
        try:
            completed = subprocess.run(
                create_command,
                cwd=str(root),
                capture_output=True,
                check=False,
                env=_isolated_install_environment(),
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_AUDIO_VENV_CREATE_TIMEOUT_SECONDS,
            )
            create_step = {
                "command": create_command,
                "returncode": completed.returncode,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
            }
        except subprocess.TimeoutExpired as exc:
            create_step = _timeout_step(create_command, exc)
        except OSError as exc:
            create_step = {
                "command": create_command,
                "returncode": None,
                "stdout": "",
                "stderr": str(exc),
            }
        steps.append(create_step)
        if create_step["returncode"] == 0 and candidate_python.is_file():
            staging = candidate_staging
            staged_python = candidate_python
            break
        last_create_error = str(
            create_step["stderr"] or create_step["stdout"] or "venv creation failed"
        )
        create_step["staging_cleanup"] = _discard_install_staging(candidate_staging, root=root)

    if staging is None or staged_python is None:
        return {
            "ok": False,
            "code": "python_3_11_unavailable",
            "reason": last_create_error,
            "data": {"steps": steps},
        }

    staged_paths = (staging,)

    def abort(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.setdefault("data", {})
        data["staging_cleanup"] = _discard_install_staging(staging, root=root)
        return payload

    safety_failure = _install_safety_failure(root, steps=steps, extra_paths=staged_paths)
    if safety_failure is not None:
        return abort(safety_failure)

    python_info = _inspect_python_runtime(str(staged_python))
    safety_failure = _install_safety_failure(root, steps=steps, extra_paths=staged_paths)
    if safety_failure is not None:
        return abort(safety_failure)
    detected_version = str(python_info.get("version") or "")
    reported_python = str(python_info.get("path") or "")
    if (
        not python_info.get("ok")
        or not _is_python_311(detected_version)
        or not _same_lexical_path(reported_python, staged_python)
    ):
        return abort(
            {
                "ok": False,
                "code": "python_3_11_required",
                "reason": str(
                    python_info.get("error")
                    or f"Expected the staged Python 3.11.x, found {detected_version or 'unknown'}."
                ),
                "data": {
                    "detected_version": detected_version,
                    "python": str(staged_python),
                    "steps": steps,
                },
            }
        )

    cargo_home = staging / ".cargo-home"
    install_temp = staging / ".install-tmp"
    try:
        cargo_home.mkdir()
        install_temp.mkdir()
    except OSError as exc:
        return abort(
            {
                "ok": False,
                "code": "install_staging_failed",
                "reason": f"Could not create staged install support directories: {exc}",
                "data": {"steps": steps},
            }
        )
    safety_failure = _install_safety_failure(root, steps=steps, extra_paths=staged_paths)
    if safety_failure is not None:
        return abort(safety_failure)

    staged_wheel: Path | None = None
    if local_wheel is not None:
        staged_wheel = install_temp / str(local_wheel["filename"])
        try:
            shutil.copyfile(str(local_wheel["path"]), staged_wheel)
        except OSError as exc:
            return abort(
                {
                    "ok": False,
                    "code": "local_wheel_staging_failed",
                    "reason": f"Verified local wheel could not be copied safely into staging: {exc}",
                    "data": {"steps": steps},
                }
            )
        if _has_reparse_component(staged_wheel, root=root) or not _file_matches(
            staged_wheel,
            expected_size=int(local_wheel["size"]),
            expected_sha256=str(local_wheel["sha256"]),
        ):
            return abort(
                {
                    "ok": False,
                    "code": "local_wheel_staging_failed",
                    "reason": "Verified local wheel could not be copied safely into staging.",
                    "data": {"steps": steps},
                }
            )

    env = _isolated_install_environment(
        cargo_home=cargo_home,
        install_temp=install_temp,
        offline=offline_wheelhouse is not None,
    )
    for command in build_install_commands(
        repo_root=root,
        python_executable=str(staged_python),
        local_wheel=staged_wheel,
        local_wheel_sha256=(str(local_wheel["sha256"]) if local_wheel is not None else None),
        index_url=index_url,
        offline_wheelhouse=offline_wheelhouse,
    ):
        safety_failure = _install_safety_failure(root, steps=steps, extra_paths=staged_paths)
        if safety_failure is not None:
            return abort(safety_failure)
        try:
            completed = subprocess.run(
                command,
                cwd=str(root),
                capture_output=True,
                check=False,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_AUDIO_PACKAGE_INSTALL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            step = _timeout_step(command, exc)
            return abort(
                {
                    "ok": False,
                    "code": "install_failed",
                    "reason": step["stderr"],
                    "data": {"steps": [*steps, step]},
                }
            )
        except OSError as exc:
            return abort(
                {
                    "ok": False,
                    "code": "install_failed",
                    "reason": str(exc),
                    "data": {"steps": [*steps, {"command": command}]},
                }
            )
        step = {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
        steps.append(step)
        safety_failure = _install_safety_failure(root, steps=steps, extra_paths=staged_paths)
        if safety_failure is not None:
            return abort(safety_failure)
        if completed.returncode != 0:
            return abort(
                {
                    "ok": False,
                    "code": "install_failed",
                    "reason": step["stderr"] or step["stdout"] or "install failed",
                    "data": {"steps": steps},
                }
            )

    for generated_path, label in (
        (cargo_home, "Cargo cache"),
        (install_temp, "temporary install directory"),
    ):
        generated_size = _safe_tree_size(generated_path)
        if generated_size is None:
            return abort(
                {
                    "ok": False,
                    "code": "unsafe_runtime_path",
                    "reason": f"Staged {label} became unsafe.",
                    "data": {"path": str(generated_path), "steps": steps},
                }
            )
        generated_cleanup = _quarantine_and_remove(
            generated_path, root=root, original_size=generated_size
        )
        if not generated_cleanup["removed"]:
            return abort(
                {
                    "ok": False,
                    "code": "install_staging_failed",
                    "reason": str(generated_cleanup["error"]),
                    "data": {"steps": steps},
                }
            )

    safety_failure = _install_safety_failure(root, steps=steps, extra_paths=staged_paths)
    if safety_failure is not None:
        return abort(safety_failure)

    promotion = _promote_install_staging(staging, root=root)
    if not promotion["ok"]:
        promotion_data = {
            "steps": steps,
            "staging_path": str(
                promotion.get("rejected_path") or (staging if os.path.lexists(staging) else "")
            ),
            "backup_path": str(promotion["backup_path"]),
        }
        return {
            "ok": False,
            "code": "install_promotion_failed",
            "reason": str(promotion["error"]),
            "data": promotion_data,
        }

    safety_failure = _install_safety_failure(root, steps=steps)
    if safety_failure is not None:
        return safety_failure
    runtime_arguments: dict[str, Any] = {
        "python_executable": str(final_python),
        "repo_root": root,
    }
    if ffmpeg_bin != "ffmpeg" or ffprobe_bin != "ffprobe":
        runtime_arguments.update(ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)
    result_data = {
        "steps": steps,
        "runtime": detect_runtime(**runtime_arguments),
    }
    return {
        "ok": True,
        "code": "ok",
        "data": result_data,
    }


def run_respiro_setup(*, repo_root: str | Path, tools_dir: str | Path) -> dict[str, Any]:
    root = Path(repo_root)
    tools_path = Path(tools_dir)
    try:
        commands = build_respiro_setup_commands(repo_root=root, tools_dir=tools_path)
    except RuntimeError as exc:
        return {
            "ok": False,
            "code": "unverified_asset_install_disabled",
            "reason": str(exc),
            "data": {},
        }

    tools_path.mkdir(parents=True, exist_ok=True)

    steps: list[dict[str, Any]] = []
    repo_path = tools_path / "Respiro-en"
    weights_path = tools_path / "respiro-en.pt"
    for index, command in enumerate(commands):
        if index == 0 and repo_path.exists():
            steps.append(
                {"command": command, "returncode": 0, "stdout": "repo exists", "stderr": ""}
            )
            continue
        if index == 1 and weights_path.exists():
            steps.append(
                {"command": command, "returncode": 0, "stdout": "weights exists", "stderr": ""}
            )
            continue

        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        step = {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
        steps.append(step)
        if completed.returncode != 0:
            reason = step["stderr"] or step["stdout"] or "respiro setup failed"
            return {
                "ok": False,
                "code": "respiro_setup_failed",
                "reason": reason,
                "data": {"steps": steps},
            }

    env_path = root / ".env"
    env_lines: list[str] = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()

    updated: dict[str, str] = {}
    for line in env_lines:
        if "=" in line:
            key, value = line.split("=", 1)
            updated[key.strip()] = value.strip()
    updated["AUDIO_SOUND_RESPIRO_REPO"] = str(repo_path)
    updated["AUDIO_SOUND_RESPIRO_WEIGHTS"] = str(weights_path)

    serialized = "\n".join(f"{key}={value}" for key, value in updated.items()) + "\n"
    env_path.write_text(serialized, encoding="utf-8")
    return {
        "ok": True,
        "code": "ok",
        "data": {
            "steps": steps,
            "repo_path": str(repo_path),
            "weights_path": str(weights_path),
            "env_path": str(env_path),
        },
    }


def format_runtime_report(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_install_report(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _unsafe_prune_result(root: Path, *, dry_run: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "failed",
        "code": "unsafe_repo_root",
        "reason": "The repository root or one of its parents is a symlink or Windows reparse point.",
        "dry_run": dry_run,
        "estimated": dry_run,
        "removed_count": 0,
        "bytes_reclaimed": 0,
        "targets": [],
        "top_level_targets": [],
        "skipped_targets": [str(root)],
        "quarantined_targets": [],
        "errors": [],
    }


def prune_workspace(*, repo_root: str | Path, dry_run: bool = False) -> dict[str, Any]:
    root = _lexical_absolute(repo_root)
    if _has_reparse_ancestor(root):
        return _unsafe_prune_result(root, dry_run=dry_run)
    with _cleanup_mutex(root):
        if _has_reparse_ancestor(root):
            return _unsafe_prune_result(root, dry_run=dry_run)
        return _prune_workspace_unlocked(root=root, dry_run=dry_run)


def _prune_workspace_unlocked(*, root: Path, dry_run: bool = False) -> dict[str, Any]:
    audio_owned_dirs = [
        root / ".venv-audio",
        root / "tools" / "audio_sound_runtime",
        root / "scratch" / "audio-sound",
        root / ".cargo-home" / "audio-sound",
    ]
    skipped_targets: set[Path] = set()
    top_level_targets: set[Path] = set()
    for path in audio_owned_dirs:
        if not os.path.lexists(path):
            continue
        if _has_reparse_component(path, root=root) or _safe_tree_size(path) is None:
            skipped_targets.add(path)
            continue
        top_level_targets.add(path)

    code_roots = [root / "audio_sound", root / "scripts" / "audio"]
    removable_dirs: set[Path] = set()
    removable_files: set[Path] = set()
    for code_root in code_roots:
        if not os.path.lexists(code_root):
            continue
        if _has_reparse_component(code_root, root=root):
            skipped_targets.add(code_root)
            continue
        for current, dirnames, filenames in os.walk(code_root, topdown=True, followlinks=False):
            current_path = Path(current)
            if _has_reparse_component(current_path, root=root):
                skipped_targets.add(current_path)
                dirnames[:] = []
                continue

            safe_dirnames: list[str] = []
            for name in dirnames:
                child = current_path / name
                if _has_reparse_component(child, root=root):
                    skipped_targets.add(child)
                    continue
                if name == "__pycache__":
                    removable_dirs.add(child)
                    continue
                safe_dirnames.append(name)
            dirnames[:] = safe_dirnames

            for name in filenames:
                if not name.endswith(".pyc"):
                    continue
                child = current_path / name
                if _has_reparse_component(child, root=root):
                    skipped_targets.add(child)
                else:
                    removable_files.add(child)

    directory_targets = removable_dirs | top_level_targets
    file_targets = {
        path
        for path in removable_files
        if not any(parent in directory_targets for parent in path.parents)
    }
    targets = sorted([*directory_targets, *file_targets], key=lambda item: str(item))

    bytes_reclaimed = 0
    removed_targets: list[Path] = []
    quarantined_targets: list[Path] = []
    errors: list[dict[str, str]] = []
    for path in targets:
        if not os.path.lexists(path) or _has_reparse_component(path, root=root):
            skipped_targets.add(path)
            continue

        size = _safe_tree_size(path)
        if size is None:
            skipped_targets.add(path)
            continue
        if dry_run:
            bytes_reclaimed += size
            removed_targets.append(path)
            continue

        outcome = _quarantine_and_remove(path, root=root, original_size=size)
        if not outcome["removed"]:
            skipped_targets.add(path)
            bytes_reclaimed += int(outcome["bytes_reclaimed"])
            quarantine_path = str(outcome["quarantine_path"] or "")
            if quarantine_path:
                quarantined_targets.append(Path(quarantine_path))
            errors.append(
                {
                    "target": str(path),
                    "quarantine": quarantine_path,
                    "error": str(outcome["error"]),
                }
            )
            continue
        bytes_reclaimed += int(outcome["bytes_reclaimed"])
        removed_targets.append(path)

    partial = bool(skipped_targets)
    return {
        "ok": not partial,
        "status": "partial" if partial else "ok",
        "code": "partial_cleanup" if partial else "ok",
        "dry_run": dry_run,
        "estimated": dry_run,
        "removed_count": len(removed_targets),
        "bytes_reclaimed": bytes_reclaimed,
        "targets": [str(path) for path in removed_targets],
        "top_level_targets": [
            str(path)
            for path in sorted(set(removed_targets) & top_level_targets, key=lambda item: str(item))
        ],
        "skipped_targets": [
            str(path) for path in sorted(skipped_targets, key=lambda item: str(item))
        ],
        "quarantined_targets": [
            str(path) for path in sorted(quarantined_targets, key=lambda item: str(item))
        ],
        "errors": errors,
    }
