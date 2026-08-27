from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

PLUGIN_NAME = "auto-cut-lite"
PACKAGE_MANIFEST_NAME = "PACKAGE-MANIFEST.json"
DEPLOYMENT_ATTEMPT_REPORT_NAME = "deployment-attempt-report.json"
_REQUIRED_RUNTIME_FILES = {
    "runtime/requirements.txt",
    "runtime/scripts/jy_wrapper.py",
    "runtime/scripts/utils/lite_revision.py",
    "runtime/scripts/utils/revision_runner.py",
    "runtime/scripts/utils/runtime_integrity.py",
}
_MUTABLE_RUNTIME_ROOTS = {".venv-audio", "output", "tmp"}
_MUTABLE_RUNTIME_FILE_NAMES = {
    "data/jy_cached_audio.csv",
    "data/cloud_music_library.csv",
    "data/cloud_sound_effects.csv",
    "data/cloud_text_styles.csv",
}
_MUTABLE_RUNTIME_PREFIXES = ("assets/jy_sync/",)


def _is_mutable_runtime_relative(relative: str) -> bool:
    normalized = str(relative).replace("\\", "/").casefold()
    return (
        normalized in {value.casefold() for value in _MUTABLE_RUNTIME_FILE_NAMES}
        or normalized.startswith(tuple(value.casefold() for value in _MUTABLE_RUNTIME_PREFIXES))
        or (
            normalized.startswith("data/")
            and normalized.endswith(".local.csv")
        )
    )


class RuntimeIntegrityError(RuntimeError):
    """Raised when the installed Lite runtime cannot be trusted for execution."""


def _fail(message: str) -> RuntimeIntegrityError:
    return RuntimeIntegrityError(
        "Installed Auto-Cut Lite runtime integrity check failed: "
        f"{message}. Redeploy the verified package before running another revision"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_no_reparse_components(path: Path, description: str) -> None:
    candidate = Path(os.path.abspath(os.fspath(path)))
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if os.path.lexists(current) and _is_reparse(current):
            raise _fail(f"{description} crosses a reparse point: {current}")


def _require_regular_file(path: Path, description: str) -> Path:
    candidate = Path(os.path.abspath(os.fspath(path)))
    _assert_no_reparse_components(candidate, description)
    if not candidate.is_file():
        raise _fail(f"{description} is missing or is not a regular file: {candidate}")
    return candidate


def _require_regular_directory(path: Path, description: str) -> Path:
    candidate = Path(os.path.abspath(os.fspath(path)))
    _assert_no_reparse_components(candidate, description)
    if not candidate.is_dir():
        raise _fail(f"{description} is missing or is not a regular directory: {candidate}")
    return candidate


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    source = _require_regular_file(path, description)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail(f"{description} is not valid JSON: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise _fail(f"{description} is not a JSON object: {source}")
    return payload


def _same_path(left: os.PathLike[str] | str, right: os.PathLike[str] | str) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def _absolute_reported_path(
    payload: dict[str, Any], field: str, description: str
) -> Path:
    raw_value = payload.get(field)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise _fail(f"deployment report is missing {field}")
    candidate = Path(raw_value.strip())
    if not candidate.is_absolute():
        raise _fail(f"deployment report {field} is not absolute: {raw_value}")
    return candidate


def _safe_manifest_relative(raw_value: Any) -> str:
    if not isinstance(raw_value, str) or not raw_value or "\\" in raw_value:
        raise _fail("package manifest contains an invalid file path")
    candidate = PurePosixPath(raw_value)
    if (
        candidate.is_absolute()
        or candidate.as_posix() != raw_value
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or any(":" in part or "\x00" in part for part in candidate.parts)
    ):
        raise _fail(f"package manifest contains an unsafe file path: {raw_value}")
    return candidate.as_posix()


def _manifest_runtime_inventory(
    payload: dict[str, Any], *, version: str
) -> tuple[dict[str, tuple[int, str]], dict[str, tuple[int, str]]]:
    if (
        payload.get("name") != PLUGIN_NAME
        or payload.get("version") != version
        or not isinstance(payload.get("files"), list)
        or not payload["files"]
    ):
        raise _fail("package manifest identity does not match the deployment report")

    package_inventory: dict[str, tuple[int, str]] = {}
    inventory: dict[str, tuple[int, str]] = {}
    seen_casefold: set[str] = set()
    for row in payload["files"]:
        if not isinstance(row, dict):
            raise _fail("package manifest contains an invalid file row")
        relative = _safe_manifest_relative(row.get("path"))
        key = relative.casefold()
        if key in seen_casefold:
            raise _fail(f"package manifest contains a duplicate path: {relative}")
        seen_casefold.add(key)
        expected_size = row.get("size")
        expected_hash = row.get("sha256")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise _fail(f"package manifest contains an invalid size for {relative}")
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in expected_hash)
        ):
            raise _fail(f"package manifest contains an invalid SHA-256 for {relative}")
        package_inventory[relative] = (expected_size, expected_hash.casefold())
        if key.startswith("runtime/"):
            inventory[relative] = (expected_size, expected_hash.casefold())

    missing_required = sorted(_REQUIRED_RUNTIME_FILES.difference(inventory))
    if missing_required:
        raise _fail(
            "package manifest is missing required runtime files: " + ", ".join(missing_required)
        )
    if ".codex-plugin/plugin.json" not in package_inventory:
        raise _fail("package manifest is missing .codex-plugin/plugin.json")
    return inventory, package_inventory


def _validate_inventoried_file(
    package_root: Path,
    relative: str,
    expected: tuple[int, str],
) -> None:
    target = package_root.joinpath(*PurePosixPath(relative).parts)
    target = _require_regular_file(target, f"package file {relative}")
    expected_size, expected_hash = expected
    if target.stat().st_size != expected_size or _sha256(target) != expected_hash:
        raise _fail(f"package file drifted from PACKAGE-MANIFEST.json: {relative}")


def _validate_manifest_anchor(
    report: dict[str, Any],
    *,
    report_path: Path,
    package_root: Path,
    runtime_root: Path,
    manifest_path: Path,
    version: str,
) -> None:
    receipt_path = _absolute_reported_path(
        report, "workspace_receipt_path", "workspace receipt"
    )
    expected_receipt_path = report_path.parent / "workspace-install-receipt.json"
    if not _same_path(receipt_path, expected_receipt_path):
        raise _fail("deployment report workspace_receipt_path is outside the state root")
    receipt = _read_json_object(receipt_path, "workspace install receipt")
    if receipt.get("status") != "installed" or receipt.get("plugin_name") != PLUGIN_NAME:
        raise _fail("workspace install receipt identity is invalid")
    if receipt.get("plugin_version") != version:
        raise _fail("workspace install receipt version does not match the deployment report")
    for field, expected in (
        ("deployment_report_path", report_path),
        ("plugin_root", package_root),
        ("runtime_root", runtime_root),
    ):
        raw_value = receipt.get(field)
        if not isinstance(raw_value, str) or not _same_path(raw_value, expected):
            raise _fail(f"workspace install receipt {field} does not match the selected runtime")
    installed_hashes = receipt.get("installed_package_sha256")
    expected_manifest_hash = (
        installed_hashes.get(PACKAGE_MANIFEST_NAME)
        if isinstance(installed_hashes, dict)
        else None
    )
    if (
        not isinstance(expected_manifest_hash, str)
        or len(expected_manifest_hash) != 64
        or _sha256(manifest_path) != expected_manifest_hash.casefold()
    ):
        raise _fail("PACKAGE-MANIFEST.json does not match the deployment receipt")


def _validate_runtime_files(
    runtime_root: Path, inventory: dict[str, tuple[int, str]]
) -> None:
    expected_runtime_paths: set[str] = set()
    drifted_files: list[str] = []
    for relative, (expected_size, expected_hash) in inventory.items():
        runtime_relative = PurePosixPath(relative).relative_to("runtime")
        expected_runtime_paths.add(runtime_relative.as_posix().casefold())
        if _is_mutable_runtime_relative(runtime_relative.as_posix()):
            continue
        target = Path(os.path.abspath(runtime_root.joinpath(*runtime_relative.parts)))
        _assert_no_reparse_components(target, f"runtime file {relative}")
        if not target.is_file():
            drifted_files.append(f"{relative} (missing)")
        elif target.stat().st_size != expected_size or _sha256(target) != expected_hash:
            drifted_files.append(f"{relative} (modified)")

    extra_files: list[str] = []
    for current_root, directory_names, file_names in os.walk(
        runtime_root, topdown=True, followlinks=False
    ):
        current = Path(current_root)
        relative_directory = current.relative_to(runtime_root)
        retained_directories: list[str] = []
        for name in directory_names:
            child = current / name
            if _is_reparse(child):
                raise _fail(f"runtime directory crosses a reparse point: {child}")
            is_root_mutable = relative_directory == Path(".") and name in _MUTABLE_RUNTIME_ROOTS
            if is_root_mutable:
                continue
            retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in file_names:
            child = current / name
            if _is_reparse(child):
                raise _fail(f"runtime contains a reparse file: {child}")
            relative = child.relative_to(runtime_root).as_posix()
            relative_parts = PurePosixPath(relative).parts
            if "__pycache__" in relative_parts and child.suffix.casefold() == ".pyc":
                continue
            if _is_mutable_runtime_relative(relative):
                continue
            if relative.casefold() not in expected_runtime_paths:
                extra_files.append(f"runtime/{relative}")

    problems: list[str] = []
    if drifted_files:
        preview = ", ".join(sorted(drifted_files, key=str.casefold)[:10])
        suffix = "" if len(drifted_files) <= 10 else f" (+{len(drifted_files) - 10} more)"
        problems.append(f"manifest drift: {preview}{suffix}")
    if extra_files:
        preview = ", ".join(sorted(extra_files, key=str.casefold)[:10])
        suffix = "" if len(extra_files) <= 10 else f" (+{len(extra_files) - 10} more)"
        problems.append(f"unlisted files: {preview}{suffix}")
    if problems:
        raise _fail("runtime differs from PACKAGE-MANIFEST.json; " + "; ".join(problems))


def resolve_deployment_report_path(
    deployment_report_path: os.PathLike[str] | str | None = None,
) -> Path:
    if deployment_report_path is not None:
        candidate = Path(deployment_report_path)
    else:
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            raise _fail("LOCALAPPDATA is unavailable and no deployment report path was supplied")
        candidate = (
            Path(local_app_data) / "Auto-Cut" / "auto-cut-lite" / "deployment-report.json"
        )
    if not candidate.is_absolute():
        raise _fail(f"deployment report path is not absolute: {candidate}")
    return _require_regular_file(candidate, "deployment report")


def _validate_deployment_attempt_state(report_path: Path) -> None:
    attempt_path = report_path.parent / DEPLOYMENT_ATTEMPT_REPORT_NAME
    if not os.path.lexists(attempt_path):
        return
    attempt = _read_json_object(attempt_path, "deployment attempt report")
    safely_rolled_back = (
        attempt.get("schema_version") == 2
        and attempt.get("plugin_name") == PLUGIN_NAME
        and attempt.get("deployment_status") == "failed"
        and attempt.get("previous_deployment_report_preserved") is True
    )
    if not safely_rolled_back:
        raise _fail(
            "deployment attempt report does not prove a complete rollback to the installed report"
        )


def validate_deployed_lite_runtime(
    *,
    deployment_report_path: os.PathLike[str] | str | None = None,
    active_runtime_root: os.PathLike[str] | str,
    active_python: os.PathLike[str] | str,
) -> dict[str, Any]:
    report_path = resolve_deployment_report_path(deployment_report_path)
    report = _read_json_object(report_path, "deployment report")
    if report.get("schema_version") != 2:
        raise _fail("deployment report schema_version is not supported")
    if report.get("deployment_status") != "installed":
        raise _fail("deployment report status is not installed")
    if report.get("plugin_name") != PLUGIN_NAME:
        raise _fail("deployment report plugin identity is invalid")
    _validate_deployment_attempt_state(report_path)
    version = report.get("plugin_version")
    if not isinstance(version, str) or not version.strip():
        raise _fail("deployment report plugin version is missing")

    expected_package_root = report_path.parent / "marketplace" / "plugins" / PLUGIN_NAME
    reported_target_root = _absolute_reported_path(
        report, "target_root", "installed plugin root"
    )
    if not _same_path(reported_target_root, expected_package_root):
        raise _fail("deployment report target_root does not match the fixed install location")

    plugin_manifest_path = _require_regular_file(
        _absolute_reported_path(report, "plugin_manifest_path", "plugin manifest"),
        "installed plugin manifest",
    )
    package_root = _require_regular_directory(reported_target_root, "installed plugin root")
    if not _same_path(plugin_manifest_path, package_root / ".codex-plugin" / "plugin.json"):
        raise _fail("deployment report plugin_manifest_path does not match the plugin root")
    runtime_root = _require_regular_directory(
        _absolute_reported_path(report, "runtime_root", "runtime root"),
        "installed runtime root",
    )
    if not _same_path(runtime_root, package_root / "runtime"):
        raise _fail("deployment report runtime_root is outside the installed plugin root")
    if not _same_path(active_runtime_root, runtime_root):
        raise _fail(
            f"active code root is not the deployed runtime: {Path(active_runtime_root)}"
        )

    components = report.get("components")
    python_component = components.get("python") if isinstance(components, dict) else None
    python_value = (
        python_component.get("runtime_path") if isinstance(python_component, dict) else None
    )
    if not isinstance(python_value, str) or not python_value.strip():
        raise _fail("deployment report is missing components.python.runtime_path")
    if (
        python_component.get("status") != "detected"
        or python_component.get("dependencies") != "installed"
    ):
        raise _fail("deployment report isolated Python is not installed and ready")
    runtime_python = _require_regular_file(Path(python_value), "isolated runtime Python")
    expected_runtime_python = package_root / ".runtime-venv" / "Scripts" / "python.exe"
    if not _same_path(runtime_python, expected_runtime_python):
        raise _fail("deployment report runtime Python does not match the isolated environment")
    if not _same_path(active_python, runtime_python):
        raise _fail(f"active Python is not the deployed isolated interpreter: {active_python}")

    plugin_manifest = _read_json_object(plugin_manifest_path, "installed plugin manifest")
    if plugin_manifest.get("name") != PLUGIN_NAME or plugin_manifest.get("version") != version:
        raise _fail("installed plugin manifest identity does not match the deployment report")
    if "skills" in plugin_manifest:
        raise _fail("installed plugin manifest unexpectedly exposes user-scoped skills")

    package_manifest_path = _require_regular_file(
        package_root / PACKAGE_MANIFEST_NAME, "package manifest"
    )
    package_manifest = _read_json_object(package_manifest_path, "package manifest")
    inventory, package_inventory = _manifest_runtime_inventory(package_manifest, version=version)
    _validate_manifest_anchor(
        report,
        report_path=report_path,
        package_root=package_root,
        runtime_root=runtime_root,
        manifest_path=package_manifest_path,
        version=version,
    )
    _validate_inventoried_file(
        package_root,
        ".codex-plugin/plugin.json",
        package_inventory[".codex-plugin/plugin.json"],
    )
    _validate_runtime_files(runtime_root, inventory)

    return {
        "status": "pass",
        "plugin_name": PLUGIN_NAME,
        "plugin_version": version,
        "deployment_report_path": str(report_path),
        "runtime_root": str(runtime_root),
        "runtime_python": str(runtime_python),
        "package_manifest_sha256": _sha256(package_manifest_path),
        "validated_runtime_file_count": len(inventory),
    }


def validate_current_lite_runtime(
    deployment_report_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    current_runtime_root = Path(__file__).absolute().parents[2]
    return validate_deployed_lite_runtime(
        deployment_report_path=deployment_report_path,
        active_runtime_root=current_runtime_root,
        active_python=sys.executable,
    )
