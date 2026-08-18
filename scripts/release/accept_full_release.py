from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.full_setup import (
    _minimal_subprocess_environment,
    _validate_fixed_offline_ffmpeg,
    _validate_offline_browser_manifest_files,
)
from scripts.release.audit_runtime_capabilities import audit_runtime_capabilities
from scripts.release.build_offline_deps import (
    _assert_no_playwright_runtime_markers,
    _clear_playwright_runtime_markers,
    _has_reparse_component,
    _is_reparse_point,
    _normalized_project_name,
    read_wheel_identity,
    validate_direct_pin_parity,
    validate_wheelhouse_lock_closure,
)
from scripts.release.build_private_subject_assets import (
    verify_private_subject_assets_bundle,
)
from scripts.release.offline_bundle import extract_offline_bundle, verify_offline_bundle
from scripts.release.release_policy import (
    is_forbidden_path,
    normalize_archive_path,
    scan_release_tree,
)
from scripts.release.release_transaction import publish_file_no_replace, unique_sibling_temp
from scripts.utils.jianying_smoke import (
    blocked_jianying_checks_valid,
    smoke_editability_receipt_valid,
)

EXPECTED_SKILL_COUNT = 17
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
OFFLINE_PLAYWRIGHT_FFMPEG_REVISION = "1011"
OFFLINE_PLAYWRIGHT_WINLDD_REVISION = "1007"
OFFLINE_CHROMIUM_VERSION = "136.0.7103.25"
SPECTRAMINI_SMOKE_ALGORITHM = "auto_cut_spectramini_style_smoke_v1"
SPECTRAMINI_SMOKE_REQUIRED_CHECKS = (
    "int16_output",
    "shape_preserved",
    "finite_output",
    "breath_rms_reduced",
    "click_peak_reduced",
    "memory_roundtrip_ok",
    "feature_finite",
    "deterministic",
)
_PROGRAM_ZIP_NAME = re.compile(
    r"Auto-Cut-v(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))-windows-x64\.zip"
)
_ACTIVE_OFFLINE_ISOLATION: ContextVar[Any | None] = ContextVar(
    "auto_cut_active_offline_isolation", default=None
)
_OFFLINE_CONTRACT_REQUIREMENT_FILES = (
    "requirements.txt",
    "requirements-offline-main.lock",
    "requirements-audio.lock",
    "requirements-offline-audio.lock",
    "requirements-audio-build.lock",
    "requirements-offline-acceptance.lock",
    "requirements-offline-bootstrap.lock",
)
_FORMAL_OFFLINE_CONTRACT_MIN_VERSION = (1, 7, 0)


class _OfflineBundleExecution:
    """Run acceptance commands with the offline bundle contract applied.

    AppContainer is an optional Windows hardening primitive, not a release
    requirement.  The release contract is the bundle-local package source,
    sanitized subprocess environment, and command-level no-fallback checks.
    This backend deliberately records that physical network isolation was not
    claimed so the acceptance report cannot overstate what was measured.
    """

    def __init__(self) -> None:
        self.workspace_root: Path | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.evidence: dict[str, object] = {
            "status": "ready",
            "code": "offline_bundle_no_network_fallback_verified",
            "primitive": "offline_command_environment_gates",
            "fail_closed": True,
            "declared_capabilities": [],
            "network_fallback": "disabled",
            "physical_network_isolation": False,
            "network_probe": "not_run",
            "security_boundary": "trusted_release_commands_only",
            "direct_socket_blocking": False,
        }

    def __enter__(self) -> _OfflineBundleExecution:
        if self._temporary is not None or self.workspace_root is not None:
            raise RuntimeError("offline acceptance workspace is already active")
        temporary = tempfile.TemporaryDirectory(prefix="auto-cut-release-accept-")
        workspace = Path(temporary.name)
        if _has_reparse_component(workspace) or not workspace.is_dir():
            temporary.cleanup()
            raise RuntimeError("offline acceptance workspace is unavailable")
        self._temporary = temporary
        self.workspace_root = workspace
        return self

    def __exit__(self, *_args: object) -> None:
        temporary = self._temporary
        self._temporary = None
        self.workspace_root = None
        if temporary is not None:
            temporary.cleanup()

    def _workspace_path(self, path: Path) -> Path:
        workspace = self.workspace_root
        if workspace is None:
            raise RuntimeError("offline acceptance workspace is not active")
        raw = Path(path)
        if _has_reparse_component(raw):
            raise RuntimeError("offline acceptance workspace path is unsafe")
        target = raw.resolve()
        try:
            target.relative_to(workspace.resolve())
        except ValueError as exc:
            raise RuntimeError("offline acceptance path is outside the workspace") from exc
        return target

    def prepare_directory(self, path: Path) -> Path:
        target = self._workspace_path(path)
        if _has_reparse_component(target) or (target.exists() and not target.is_dir()):
            raise RuntimeError("offline acceptance directory is invalid")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def prepare_python_runtime(self, destination: Path) -> Path:
        """Copy a minimal base CPython runtime into the temp acceptance root."""

        if (
            sys.version_info[:2] != (3, 11)
            or sys.implementation.name.casefold() != "cpython"
            or ctypes.sizeof(ctypes.c_void_p) != 8
        ):
            raise RuntimeError("acceptance Python must be CPython 3.11 x64")
        source_raw = Path(sys.base_prefix)
        if _has_reparse_component(source_raw):
            raise RuntimeError("acceptance Python source is an unsafe reparse path")
        source = source_raw.resolve()
        if os.name != "nt":
            executable = Path(sys.executable).resolve()
            if not executable.is_file() or executable.is_symlink():
                raise RuntimeError("acceptance Python executable is unavailable")
            return executable

        required_files = (
            source / "python.exe",
            source / "python3.dll",
            source / "python311.dll",
            source / "vcruntime140.dll",
        )
        optional_files = (
            source / "pythonw.exe",
            source / "vcruntime140_1.dll",
            source / "LICENSE.txt",
        )
        if any(_has_reparse_component(path) or not path.is_file() for path in required_files):
            raise RuntimeError("acceptance Python runtime is incomplete")
        for tree in (source / "DLLs", source / "Lib"):
            if _has_reparse_component(tree) or not tree.is_dir():
                raise RuntimeError("acceptance Python runtime is incomplete")
            if any(_is_reparse_point(candidate) for candidate in tree.rglob("*")):
                raise RuntimeError("acceptance Python runtime contains an unsafe reparse path")

        target = self._workspace_path(destination)
        if _has_reparse_component(target) or target.exists() and not target.is_dir():
            raise RuntimeError("acceptance Python destination is invalid")
        target.mkdir(parents=True, exist_ok=True)
        try:
            for path in (*required_files, *optional_files):
                if path.is_file() and not path.is_symlink():
                    shutil.copy2(path, target / path.name)
            shutil.copytree(source / "DLLs", target / "DLLs", symlinks=False)

            def ignore_lib(directory: str, names: list[str]) -> set[str]:
                ignored = {name for name in names if name == "__pycache__" or name.endswith(".pyc")}
                if Path(directory).resolve() == (source / "Lib").resolve():
                    ignored.add("site-packages")
                return ignored

            shutil.copytree(source / "Lib", target / "Lib", ignore=ignore_lib, symlinks=False)
            executable = target / "python.exe"
            if not executable.is_file() or executable.is_symlink():
                raise RuntimeError("acceptance Python copy is incomplete")
            return executable
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        command_error = _offline_command_error(command)
        if command_error:
            raise RuntimeError(command_error)
        environment_error = _offline_environment_error(env)
        if environment_error:
            raise RuntimeError(environment_error)
        working_directory = self._workspace_path(cwd)
        if not working_directory.is_dir():
            raise RuntimeError("offline acceptance working directory is unavailable")
        arguments = [str(part) for part in command]
        executable = Path(arguments[0])
        if not executable.is_absolute():
            raise RuntimeError("offline acceptance executable must be workspace-local")
        resolved_executable = self._workspace_path(executable)
        if resolved_executable.is_symlink() or not resolved_executable.is_file():
            raise RuntimeError("offline acceptance executable is unavailable")
        arguments[0] = str(resolved_executable)
        process_temp = self.prepare_directory(self.workspace_root / "process-temp")
        environment = {str(key): str(value) for key, value in env.items()}
        environment["TEMP"] = str(process_temp)
        environment["TMP"] = str(process_temp)
        return subprocess.run(
            arguments,
            cwd=working_directory,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )


def _requires_formal_offline_contract(version: str) -> bool:
    try:
        parsed = tuple(int(part) for part in version.split("."))
    except (AttributeError, ValueError):
        return False
    return parsed >= _FORMAL_OFFLINE_CONTRACT_MIN_VERSION


def _runtime_capability_probe(
    package_root: Path, release_paths: Sequence[str]
) -> dict[str, object]:
    result = audit_runtime_capabilities(package_root, release_paths)
    findings = result.get("findings") if isinstance(result.get("findings"), list) else []
    if result.get("status") != "ready" or findings:
        codes = sorted(
            {
                str(finding.get("code") or "unknown")
                for finding in findings
                if isinstance(finding, dict)
            }
        )
        raise ValueError(
            "runtime capability audit failed" + (f": {', '.join(codes)}" if codes else "")
        )
    declared = result.get("declared_capability_ids")
    checked = result.get("checked_paths")
    return {
        "status": "ready",
        "code": "runtime_capability_contract_verified",
        "capability_count": len(declared) if isinstance(declared, list) else 0,
        "checked_path_count": len(checked) if isinstance(checked, list) else 0,
        "finding_count": 0,
    }


class ReleaseCommandError(RuntimeError):
    def __init__(self, stage: str, returncode: int, *, detail: str = "") -> None:
        self.stage = stage if re.fullmatch(r"[a-z0-9_]+", stage) else "unknown_command"
        self.returncode = int(returncode)
        self.detail = detail if re.fullmatch(r"[a-z0-9_.]+", detail) else ""
        super().__init__(f"{self.stage} failed with exit code {self.returncode}")


MAIN_IMPORT_MODULES = (
    "uiautomation",
    "playwright",
    "pynput",
    "edge_tts",
    "pymediainfo",
    "cv2",
    "av",
    "numpy",
    "imageio",
    "psutil",
    "requests",
    "websockets",
)
RELEASE_TESTS = (
    "tests/test_repository_contracts.py::TestRepositoryContracts::"
    "test_notification_setup_docs_and_release_boundary_are_explicit",
    "tests/test_repository_contracts.py::TestRepositoryContracts::"
    "test_subject_pointer_release_tracks_guides_and_handoff_but_ignores_local_state",
    "tests/audio_sound/test_source_migration_contract.py",
    "tests/audio_sound/test_volc_asr.py",
    "tests/test_no_git_release_contracts.py",
    "tests/test_capability_manifest.py",
    "tests/test_full_distribution_docs.py",
    "tests/test_private_subject_assets_release.py",
    "tests/test_runtime_capability_audit.py",
    "tests/test_revision_markers.py",
    "tests/test_segmented_audio_delivery.py",
    "tests/test_repository_contracts.py::TestRepositoryContracts::"
    "test_revision_markers_preserve_source_ledger_text_verbatim",
    "tests/test_repository_contracts.py::TestRepositoryContracts::"
    "test_source_text_recovery_is_canonical_warned_and_nonblocking",
    "tests/test_repository_contracts.py::TestRepositoryContracts::"
    "test_saved_draft_marker_receipts_cover_root_and_active_timeline",
    "tests/test_repository_contracts.py::TestRepositoryContracts::"
    "test_final_acceptance_routes_always_on_and_conditional_gates",
    "tests/test_revision_runner.py::TestRevisionRunner::"
    "test_execute_revision_request_renders_one_verbatim_marker_and_returns_saved_receipt",
    "tests/test_revision_runner.py::TestRevisionRunner::"
    "test_execute_revision_request_rejects_mismatched_marker_in_active_timeline",
    "tests/test_revision_runner.py::TestRevisionRunner::"
    "test_false_acceptance_flags_cannot_disable_item_derived_gates",
    "tests/test_revision_runner.py::TestRevisionRunner::"
    "test_acceptance_variants_reject_active_timeline_missing_animation_overlay",
    "tests/test_revision_runner.py::TestRevisionRunner::"
    "test_saved_variant_validation_rejects_flattened_active_timeline",
    "tests/test_revision_runner.py::TestRevisionRunner::"
    "test_validate_saved_revision_draft_rejects_flattened_preview_shell",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _snapshot_archive(path: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("rb") as source, destination.open("xb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def _verify_archive_path_digest(
    path: Path, expected_sha256: str, *, artifact: str = "release ZIP"
) -> None:
    try:
        actual_sha256 = _sha256_file(path)
    except OSError as exc:
        raise ValueError(f"{artifact} changed or disappeared during acceptance") from exc
    if actual_sha256 != expected_sha256:
        raise ValueError(f"{artifact} hash changed during acceptance")


def _verify_private_assets_archive(
    archive_path: Path,
    snapshot_path: Path,
    *,
    expected_version: str,
    expected_source_commit: str,
) -> dict[str, object]:
    private_archive = archive_path.resolve()
    expected_name = f"Auto-Cut-v{expected_version}-private-assets-high-school-history.zip"
    if private_archive.name != expected_name:
        raise ValueError("private asset filename does not match the release version")
    archive_sha256 = _snapshot_archive(private_archive, snapshot_path)
    _verify_archive_path_digest(
        private_archive,
        archive_sha256,
        artifact="private asset ZIP",
    )
    verification = verify_private_subject_assets_bundle(snapshot_path)
    manifest = verification.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("private asset manifest evidence is missing")
    if (
        manifest.get("release_version") != expected_version
        or manifest.get("source_commit") != expected_source_commit
        or manifest.get("profile_key") != "senior-high-history"
        or manifest.get("target_requires_explicit_binding") is not True
    ):
        raise ValueError("private asset release identity mismatch")
    return {
        "status": "ready",
        "zip_name": private_archive.name,
        "zip_sha256": archive_sha256,
        "manifest_sha256": verification["manifest_sha256"],
        "source_commit": expected_source_commit,
        "profile_key": "senior-high-history",
        "file_count": verification["file_count"],
        "target_requires_explicit_binding": True,
    }


def _exact_private_profile_result(payload: object, *, label: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError(f"private profile_registry {label} output is invalid")
    profile = payload.get("profile")
    if not isinstance(profile, dict) or not (
        payload.get("status") == "ready"
        and payload.get("key") == "senior-high-history"
        and payload.get("stage_id") == "senior-high"
        and payload.get("subject_id") == "history"
        and payload.get("missing_items") in (None, [])
        and payload.get("problems") in (None, [])
        and profile.get("status") == "ready"
        and profile.get("key") == "senior-high-history"
        and profile.get("stage_id") == "senior-high"
        and profile.get("subject_id") == "history"
        and profile.get("missing_items") == []
        and profile.get("problems") == []
    ):
        raise ValueError(f"private profile_registry {label} did not return the exact ready profile")
    return payload


def _verify_private_profile_round_trip(
    venv_python: Path,
    package_root: Path,
    private_archive: Path,
    working_root: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    if working_root.exists() or working_root.is_symlink():
        raise ValueError("private profile round-trip workspace is not fresh")
    verify_private_subject_assets_bundle(private_archive)
    private_root = working_root / "private-assets"
    registry_root = working_root / "registry"
    active_isolation = _ACTIVE_OFFLINE_ISOLATION.get()
    if active_isolation is not None:
        _prepare_isolated_directory(active_isolation, working_root)
        _prepare_isolated_directory(active_isolation, private_root)
        _prepare_isolated_directory(active_isolation, registry_root)
    with private_archive.open("rb") as archive_stream:
        safe_extract(archive_stream, private_root)
    script = (
        package_root
        / "skills"
        / "auto-cut-subject-pointer-onboarding"
        / "scripts"
        / "profile_registry.py"
    )
    if script.is_symlink() or not script.is_file():
        raise ValueError("private profile_registry script is missing from clean extraction")
    environment = env or offline_subprocess_environment()
    commands = (
        (
            "register",
            ["register", "--input", str(private_root / "intake.json")],
        ),
        (
            "check",
            ["check", "--stage-id", "senior-high", "--subject-id", "history"],
        ),
        ("validate", ["validate"]),
    )
    results: dict[str, object] = {}
    for label, arguments in commands:
        completed = _require_offline_command(
            [
                str(venv_python),
                "-I",
                str(script),
                *arguments,
                "--root",
                str(registry_root),
                "--json",
            ],
            cwd=package_root,
            timeout=180,
            code=f"private_profile_registry_{label}",
            env=environment,
        )
        try:
            results[label] = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"private profile_registry {label} output is unreadable") from exc
    _exact_private_profile_result(results["register"], label="register")
    _exact_private_profile_result(results["check"], label="check")
    validation = results["validate"]
    if not isinstance(validation, list) or len(validation) != 1:
        raise ValueError("private profile_registry validate output is not exact")
    _exact_private_profile_result(validation[0], label="validate")
    return {
        "status": "ready",
        "code": "private_profile_registry_round_trip_verified",
        "profile_key": "senior-high-history",
        "stage_id": "senior-high",
        "subject_id": "history",
    }


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _zip_entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    return info.create_system == 3 and stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def safe_extract(zip_path: Path | BinaryIO, destination: Path) -> list[str]:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    seen_casefold: set[str] = set()
    total_size = 0
    extracted: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            raw_name = info.filename.rstrip("/")
            if not raw_name:
                continue
            name = normalize_archive_path(raw_name)
            if name in seen or name.casefold() in seen_casefold:
                raise ValueError(f"unsafe duplicate archive entry: {name}")
            if is_forbidden_path(name):
                raise ValueError(f"forbidden archive entry: {name}")
            if _zip_entry_is_symlink(info):
                raise ValueError(f"unsafe symlink archive entry: {name}")
            total_size += info.file_size
            if total_size > MAX_ARCHIVE_BYTES:
                raise ValueError("unsafe archive exceeds extraction size limit")
            seen.add(name)
            seen_casefold.add(name.casefold())
            target = destination.joinpath(*PurePosixPath(name).parts)
            try:
                target.resolve(strict=False).relative_to(destination)
            except ValueError as exc:
                raise ValueError(f"unsafe archive extraction target: {name}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            with archive.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            extracted.append(name)
    return sorted(extracted)


def _load_inventory(extracted_root: Path) -> dict[str, Any]:
    inventory_path = extracted_root / "release-inventory.json"
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("release inventory is missing or invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ValueError("release inventory has an invalid schema")
    return payload


def verify_inventory(extracted_root: Path) -> dict[str, Any]:
    root = extracted_root.resolve()
    inventory = _load_inventory(root)
    if set(inventory) != {
        "schema_version",
        "version",
        "source_commit",
        "files",
        "inventory_sha256",
    }:
        raise ValueError("release inventory top-level schema is invalid")
    if inventory.get("schema_version") != 1 or isinstance(inventory.get("schema_version"), bool):
        raise ValueError("release inventory schema version is invalid")
    version = inventory.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("release inventory version is invalid")
    source_commit = inventory.get("source_commit")
    if not isinstance(source_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}(?:[0-9a-f]{24})?", source_commit
    ):
        raise ValueError("release inventory source commit is invalid")
    rows = inventory["files"]
    expected_paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "size", "sha256"}:
            raise ValueError("release inventory file row has an invalid schema")
        path_value = row["path"]
        size = row["size"]
        sha256 = row["sha256"]
        if not isinstance(path_value, str):
            raise ValueError("release inventory file row path is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("release inventory file row size is invalid")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("release inventory file row hash is invalid")
        path = normalize_archive_path(path_value)
        if path in expected_paths:
            raise ValueError(f"duplicate release inventory path: {path}")
        expected_paths.add(path)
        source = root.joinpath(*PurePosixPath(path).parts)
        if not source.is_file():
            raise ValueError(f"inventory file is missing: {path}")
        if source.stat().st_size != size:
            raise ValueError(f"inventory size mismatch: {path}")
        if _sha256_file(source) != sha256:
            raise ValueError(f"inventory hash mismatch: {path}")
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() != "release-inventory.json"
    }
    extras = sorted(actual_paths - expected_paths)
    if extras:
        raise ValueError(f"unlisted release file: {extras[0]}")
    missing = sorted(expected_paths - actual_paths)
    if missing:
        raise ValueError(f"inventory file is missing: {missing[0]}")
    version_path = root / "VERSION"
    if (
        not version_path.is_file()
        or version_path.read_text(encoding="utf-8-sig").strip() != version
    ):
        raise ValueError("release inventory version does not match VERSION")
    digest_payload = {
        "schema_version": inventory["schema_version"],
        "version": version,
        "source_commit": source_commit,
        "files": rows,
    }
    expected_digest = _sha256_bytes(_canonical_json(digest_payload))
    if inventory.get("inventory_sha256") != expected_digest:
        raise ValueError("release inventory digest mismatch")
    return inventory


def capture_inventory_state(extracted_root: Path, inventory: dict[str, Any]) -> dict[str, object]:
    root = extracted_root.resolve()
    rows = tuple(
        (str(row["path"]), int(row["size"]), str(row["sha256"])) for row in inventory["files"]
    )
    return {
        "inventory_bytes": (root / "release-inventory.json").read_bytes(),
        "files": rows,
    }


def _is_allowed_runtime_addition(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    if parts[0].casefold() in {".venv", ".venv-audio", ".codex", "tmp"}:
        return True
    lowered_parts = {part.casefold() for part in parts}
    if lowered_parts & {"__pycache__", ".pytest_cache", ".ruff_cache"}:
        return True
    return PurePosixPath(path).suffix.casefold() in {".pyc", ".pyo"}


def verify_post_acceptance_inventory(extracted_root: Path, snapshot: dict[str, object]) -> None:
    root = extracted_root.resolve()
    inventory_bytes = snapshot.get("inventory_bytes")
    rows = snapshot.get("files")
    if not isinstance(inventory_bytes, bytes) or not isinstance(rows, tuple):
        raise ValueError("release inventory snapshot is invalid")
    if (root / "release-inventory.json").read_bytes() != inventory_bytes:
        raise ValueError("release inventory file was modified during acceptance")

    expected_paths: set[str] = set()
    for row in rows:
        if not isinstance(row, tuple) or len(row) != 3:
            raise ValueError("release inventory snapshot row is invalid")
        path, size, sha256 = row
        expected_paths.add(path)
        source = root.joinpath(*PurePosixPath(path).parts)
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"inventory file changed during acceptance: {path}")
        if source.stat().st_size != size or _sha256_file(source) != sha256:
            raise ValueError(f"inventory file hash changed during acceptance: {path}")

    for source in root.rglob("*"):
        if not (source.is_file() or source.is_symlink()):
            continue
        path = source.relative_to(root).as_posix()
        if path == "release-inventory.json" or path in expected_paths:
            continue
        if source.is_symlink() or not _is_allowed_runtime_addition(path):
            raise ValueError(f"unlisted non-runtime file created during acceptance: {path}")


def _tree_receipt(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, _sha256_file(path))
        for path in sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(root).as_posix(),
        )
    }


def verify_skill_parity(source_root: Path, installed_root: Path) -> dict[str, object]:
    source = {
        child.name: child
        for child in source_root.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    }
    installed = {
        child.name: child
        for child in installed_root.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    }
    if len(source) != EXPECTED_SKILL_COUNT or set(source) != set(installed):
        raise ValueError("skill count or name mismatch")
    for name in sorted(source):
        if _tree_receipt(source[name]) != _tree_receipt(installed[name]):
            raise ValueError(f"skill tree hash mismatch: {name}")
    return {"status": "ready", "code": "skills_hash_verified", "count": len(source)}


def _component_status(component: object) -> str:
    return "ready" if isinstance(component, dict) and component.get("ok") is True else "unavailable"


def evaluate_audio_doctor(doctor: object) -> dict[str, object]:
    if not isinstance(doctor, dict):
        raise ValueError("audio doctor evidence is missing")
    if not (
        doctor.get("status") == "degraded"
        and doctor.get("full") is False
        and doctor.get("degraded") is True
        and doctor.get("unavailable") is False
        and doctor.get("execution_policy") == "external_models_fail_closed"
    ):
        raise ValueError("audio doctor status or execution policy is inconsistent")

    core: dict[str, dict[str, object]] = {}
    for component_id in ("python", "ffmpeg", "ffprobe", "spectramini"):
        component = doctor.get(component_id)
        if not isinstance(component, dict) or component.get("ok") is not True:
            raise ValueError(f"audio doctor core component is unavailable: {component_id}")
        identity = component.get("identity")
        if not isinstance(identity, str) or not identity:
            raise ValueError(f"audio doctor core component identity is missing: {component_id}")
        evidence: dict[str, object] = {"ok": True, "identity": identity}
        if component_id == "spectramini":
            smoke_checks = component.get("smoke_checks")
            if not (
                component.get("smoke_status") == "passed"
                and component.get("algorithm_identity") == SPECTRAMINI_SMOKE_ALGORITHM
                and isinstance(smoke_checks, dict)
                and all(
                    smoke_checks.get(check_id) is True
                    for check_id in SPECTRAMINI_SMOKE_REQUIRED_CHECKS
                )
            ):
                raise ValueError("SpectraMini smoke execution evidence is invalid")
            evidence.update(
                smoke_status="passed",
                algorithm_identity=SPECTRAMINI_SMOKE_ALGORITHM,
                smoke_checks={check_id: True for check_id in SPECTRAMINI_SMOKE_REQUIRED_CHECKS},
            )
        core[component_id] = evidence

    external: dict[str, dict[str, object]] = {}
    for component_id in ("deepfilternet", "respiro_en"):
        component = doctor.get(component_id)
        if not isinstance(component, dict) or not (
            component.get("ok") is False
            and component.get("execution_status") == "external_unavailable"
            and type(component.get("asset_verification_ok")) is bool
        ):
            raise ValueError(f"audio external model policy is invalid: {component_id}")
        identity = component.get("identity")
        if not isinstance(identity, str):
            raise ValueError(f"audio external model identity is invalid: {component_id}")
        external[component_id] = {
            "ok": False,
            "identity": identity,
            "asset_verification_ok": component["asset_verification_ok"],
            "execution_status": "external_unavailable",
        }
    return {"status": "degraded", "core": core, "external": external}


def verify_fresh_audio_doctor(
    expected_evidence: object,
    fresh_doctor: object,
) -> dict[str, object]:
    if not isinstance(expected_evidence, dict):
        raise ValueError("saved audio doctor evidence is invalid")
    fresh_evidence = evaluate_audio_doctor(fresh_doctor)
    if fresh_evidence != expected_evidence:
        raise ValueError("fresh audio doctor component truth or identity changed")
    return fresh_evidence


def evaluate_install_report(
    report: dict[str, Any],
    *,
    expected_version: str,
    expected_manifest_sha256: str,
    expected_offline: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected_version):
        raise ValueError("expected install report version is invalid")
    if report.get("schema_version") != 1 or isinstance(report.get("schema_version"), bool):
        raise ValueError("install report schema version is invalid")
    if report.get("installer_version") != expected_version:
        raise ValueError("install report installer version does not match release inventory")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256):
        raise ValueError("expected capability manifest digest is invalid")
    manifest = report.get("capability_manifest")
    if not isinstance(manifest, dict) or not (
        manifest.get("status") == "ready"
        and manifest.get("schema_version") == 1
        and manifest.get("release_version") == expected_version
        and isinstance(manifest.get("sha256"), str)
        and manifest.get("sha256") == expected_manifest_sha256
    ):
        raise ValueError("install report capability manifest version or digest is invalid")
    if report.get("status") not in {"ready", "degraded"}:
        raise ValueError("install report status is not acceptable")
    stage_rows = [stage for stage in report.get("stages", []) if isinstance(stage, dict)]
    stage_ids = [stage.get("id") for stage in stage_rows]
    if any(not isinstance(stage_id, str) or not stage_id for stage_id in stage_ids):
        raise ValueError("install report stage id is invalid")
    if len(stage_ids) != len(set(stage_ids)):
        raise ValueError("install report contains a duplicate stage id")
    stages = dict(zip(stage_ids, stage_rows, strict=True))
    required_ready = (
        "manifest",
        "platform",
        "python",
        "dependencies",
        "dependency_imports",
        "skills",
        "playwright_chromium",
    )
    if expected_offline is not None:
        required_ready += ("offline_bundle",)
    for stage_id in required_ready:
        stage = stages.get(stage_id, {})
        if stage.get("status") != "ready" or stage.get("mandatory") is not True:
            raise ValueError(f"mandatory install stage is not ready: {stage_id}")
    for stage in stages.values():
        if stage.get("mandatory") is True and stage.get("status") in {
            "failed",
            "skipped",
            "pending",
            "unavailable",
        }:
            raise ValueError(f"mandatory install stage failed: {stage.get('id')}")
    audio_stage = stages.get("audio_runtime", {})
    if audio_stage.get("mandatory") is not True:
        raise ValueError("mandatory install stage is missing: audio_runtime")
    doctor = report.get("audio_doctor")
    audio_evidence = evaluate_audio_doctor(doctor)
    doctor_status = str(audio_evidence["status"])
    if report.get("status") != "degraded":
        raise ValueError("audio truth was upgraded by the install report status")
    if audio_stage.get("status") != doctor_status:
        raise ValueError("audio truth was upgraded or changed by the installer")
    offline_status = "not_requested"
    if expected_offline is not None:
        expected_archive_sha256 = expected_offline.get("zip_sha256")
        if (
            not isinstance(expected_archive_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_archive_sha256) is None
        ):
            raise ValueError("offline companion archive digest is invalid")
        receipt = report.get("offline_bundle")
        expected_runtime = {
            "browser_root": "tmp/offline-runtime/browsers",
            "ffmpeg": "tmp/offline-runtime/tools/ffmpeg/bin/ffmpeg.exe",
            "ffprobe": "tmp/offline-runtime/tools/ffmpeg/bin/ffprobe.exe",
        }
        if not isinstance(receipt, dict) or set(receipt) != {
            "status",
            "manifest_sha256",
            "source_commit",
            "archive_sha256",
            "target",
            "runtime",
        }:
            raise ValueError("offline companion install receipt is invalid")
        if not (
            receipt.get("status") == "ready"
            and receipt.get("manifest_sha256") == expected_offline.get("manifest_sha256")
            and receipt.get("source_commit") == expected_offline.get("source_commit")
            and receipt.get("archive_sha256") == expected_archive_sha256
            and receipt.get("target") == expected_offline.get("target")
            and receipt.get("runtime") == expected_runtime
        ):
            raise ValueError("offline companion install receipt does not match acceptance input")
        offline_status = "ready"
    return {
        "status": "ready",
        "code": "install_report_verified",
        "audio_status": doctor_status,
        "spectramini": _component_status(doctor.get("spectramini")),
        "deepfilternet": _component_status(doctor.get("deepfilternet")),
        "respiro": _component_status(doctor.get("respiro_en", doctor.get("respiro"))),
        "jianying": stages.get("jianying", {}).get("status", "pending"),
        "offline_status": offline_status,
        "audio_evidence": audio_evidence,
    }


def _offline_isolation_evidence_valid(evidence: object) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    if (
        evidence.get("status") == "ready"
        and evidence.get("code") == "offline_bundle_no_network_fallback_verified"
    ):
        return bool(
            evidence.get("primitive") == "offline_command_environment_gates"
            and evidence.get("fail_closed") is True
            and evidence.get("declared_capabilities") == []
            and evidence.get("network_fallback") == "disabled"
            and evidence.get("physical_network_isolation") is False
            and evidence.get("network_probe") == "not_run"
            and evidence.get("security_boundary") == "trusted_release_commands_only"
            and evidence.get("direct_socket_blocking") is False
        )
    parent = evidence.get("parent_probe")
    child = evidence.get("child_probe")
    exact_probe = {
        "is_appcontainer": True,
        "capability_count": 0,
        "loopback_blocked": True,
    }
    return bool(
        evidence.get("status") == "ready"
        and evidence.get("code") == "windows_appcontainer_network_isolation_verified"
        and evidence.get("primitive") == "windows_appcontainer_zero_capabilities"
        and evidence.get("fail_closed") is True
        and evidence.get("declared_capabilities") == []
        and parent == exact_probe
        and child == exact_probe
        and evidence.get("listener_control_connection_observed") is True
        and evidence.get("listener_connection_observed") is False
        and isinstance(evidence.get("connection_attempt_count"), int)
        and not isinstance(evidence.get("connection_attempt_count"), bool)
        and int(evidence["connection_attempt_count"]) >= 12
    )


@contextmanager
def _activate_offline_isolation(isolation: Any) -> Iterator[Any]:
    if not _offline_isolation_evidence_valid(getattr(isolation, "evidence", None)):
        raise RuntimeError("offline execution evidence is invalid")
    if _ACTIVE_OFFLINE_ISOLATION.get() is not None:
        raise RuntimeError("offline execution is already active")
    token = _ACTIVE_OFFLINE_ISOLATION.set(isolation)
    try:
        yield isolation
    finally:
        _ACTIVE_OFFLINE_ISOLATION.reset(token)


def _open_offline_isolation() -> Any:
    # The formal release contract does not require AppContainer or a firewall
    # change.  It verifies the local bundle, strips package/browser indexes,
    # and rejects commands that could fall back to a network source.
    return _OfflineBundleExecution()


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    enforce_no_network: bool = False,
) -> subprocess.CompletedProcess[str]:
    arguments = [str(part) for part in command]
    environment = env or clean_subprocess_environment()
    if not enforce_no_network:
        return subprocess.run(
            arguments,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    isolation = _ACTIVE_OFFLINE_ISOLATION.get()
    if isolation is None:
        raise RuntimeError("offline execution is not active")
    if not _offline_isolation_evidence_valid(getattr(isolation, "evidence", None)):
        raise RuntimeError("offline execution evidence is invalid")
    command_error = _offline_command_error(arguments)
    if command_error:
        raise RuntimeError(command_error)
    environment_error = _offline_environment_error(environment)
    if environment_error:
        raise RuntimeError(environment_error)
    return isolation.run(arguments, cwd=cwd, timeout=timeout, env=environment)


def clean_subprocess_environment() -> dict[str, str]:
    environment = _minimal_subprocess_environment()
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def offline_subprocess_environment() -> dict[str, str]:
    environment = clean_subprocess_environment()
    for key in tuple(environment):
        upper = key.upper()
        if (
            upper.startswith("PIP_")
            or upper.endswith("_PROXY")
            or upper
            in {
                "PLAYWRIGHT_DOWNLOAD_HOST",
                "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
                "PLAYWRIGHT_BROWSERS_PATH",
                "NPM_CONFIG_REGISTRY",
                "UV_INDEX",
                "UV_INDEX_URL",
                "UV_EXTRA_INDEX_URL",
            }
        ):
            environment.pop(key, None)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_NO_INDEX": "1",
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
    )
    return environment


def _offline_command_error(command: Sequence[str]) -> str | None:
    arguments = [str(part) for part in command]
    if not arguments:
        return "offline command is empty"
    lowered = [part.casefold() for part in arguments]
    forbidden_flags = {
        "--index-url",
        "--extra-index-url",
        "--trusted-host",
        "--proxy",
    }
    def is_remote_reference(value: str) -> bool:
        return bool(
            re.match(r"^[a-z][a-z0-9+.-]*://", value, flags=re.IGNORECASE)
            or value.startswith(("\\\\", "//"))
        )

    if any(
        part in forbidden_flags or any(part.startswith(flag + "=") for flag in forbidden_flags)
        for part in lowered
    ) or any(
        is_remote_reference(part) for part in lowered
    ):
        return "offline command contains a network or package-index fallback"

    is_playwright_install = False
    for index, part in enumerate(lowered[:-1]):
        if part == "-m" and lowered[index + 1] == "playwright":
            is_playwright_install = "install" in lowered[index + 2 :]
            break
    executable_name = Path(arguments[0]).stem.casefold()
    if executable_name in {"playwright", "playwright.cmd"} and "install" in lowered[1:]:
        is_playwright_install = True
    if is_playwright_install:
        return "offline command cannot invoke a browser download installer"

    is_pip_install = "pip" in lowered and "install" in lowered
    if is_pip_install:
        if "--no-index" not in lowered or "--find-links" not in lowered:
            return "offline pip install must use --no-index and --find-links"
        find_links_index = lowered.index("--find-links")
        if find_links_index + 1 >= len(arguments):
            return "offline pip install is missing its local wheelhouse"
        wheelhouse = arguments[find_links_index + 1]
        if is_remote_reference(wheelhouse.casefold()):
            return "offline pip wheelhouse must be local"
    return None


def _offline_environment_error(env: Mapping[str, str]) -> str | None:
    required = {
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_NO_INDEX": "1",
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }
    if any(env.get(key) != value for key, value in required.items()):
        return "offline subprocess environment is incomplete"
    forbidden_exact = {
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PLAYWRIGHT_DOWNLOAD_HOST",
        "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
        "PLAYWRIGHT_BROWSERS_PATH",
        "NPM_CONFIG_REGISTRY",
        "UV_INDEX",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
    }
    required_keys = set(required)
    allowed_control = required_keys | forbidden_exact | {"NO_PROXY"}
    for key in env:
        upper = str(key).upper()
        if upper in allowed_control and key != upper:
            return "offline subprocess environment has a noncanonical control key"
        if upper in forbidden_exact:
            return "offline subprocess environment contains a package index"
        if upper.startswith("PIP_") and upper not in required_keys:
            return "offline subprocess environment contains a package index"
        if upper.startswith("PLAYWRIGHT_") and upper not in {
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"
        }:
            return "offline subprocess environment contains a browser fallback"
        if upper.startswith("UV_") or upper == "NPM_CONFIG_REGISTRY":
            return "offline subprocess environment contains a package index"
    allowed_proxy_keys = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    for key in env:
        upper = str(key).upper()
        if upper.endswith("_PROXY") and upper not in allowed_proxy_keys:
            return "offline subprocess environment contains a proxy fallback"
        if upper in allowed_proxy_keys and key != upper:
            return "offline subprocess environment has a noncanonical proxy key"
    return None


def _require_offline_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    code: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    command_error = _offline_command_error(command)
    if command_error:
        raise ValueError(command_error)
    environment_error = _offline_environment_error(env)
    if environment_error:
        raise ValueError(environment_error)
    return _require_command(
        command,
        cwd=cwd,
        timeout=timeout,
        code=code,
        env=env,
        enforce_no_network=True,
    )


def _require_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    code: str,
    env: dict[str, str] | None = None,
    enforce_no_network: bool = False,
) -> subprocess.CompletedProcess[str]:
    completed = _run(
        command,
        cwd=cwd,
        timeout=timeout,
        env=env,
        enforce_no_network=enforce_no_network,
    )
    if completed.returncode != 0:
        detail = ""
        if code == "unified_setup":
            report_path = cwd / "tmp" / "install" / "install-report.json"
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                report = {}
            stage_rows = report.get("stages") if isinstance(report, dict) else None
            for row in stage_rows if isinstance(stage_rows, list) else []:
                if not isinstance(row, dict) or row.get("status") != "failed":
                    continue
                stage_id = row.get("id")
                stage_code = row.get("code")
                if (
                    isinstance(stage_id, str)
                    and isinstance(stage_code, str)
                    and re.fullmatch(r"[a-z0-9_]+", stage_id)
                    and re.fullmatch(r"[a-z0-9_]+", stage_code)
                ):
                    detail = f"{stage_id}.{stage_code}"
                break
        raise ReleaseCommandError(code, completed.returncode, detail=detail)
    return completed


def _parse_json_output(output: str, label: str) -> dict[str, Any]:
    candidates = [line for line in output.splitlines() if line.lstrip().startswith("{")]
    for candidate in reversed(candidates):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} did not return JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} did not return a JSON object")
    return payload


def _main_import_probe(
    venv_python: Path,
    package_root: Path,
    *,
    env: dict[str, str] | None = None,
) -> None:
    statement = "; ".join(f"import {module}" for module in MAIN_IMPORT_MODULES)
    _require_offline_command(
        [str(venv_python), "-c", statement],
        cwd=package_root,
        timeout=120,
        code="main_dependency_import_probe",
        env=env or offline_subprocess_environment(),
    )


def _chromium_probe(
    venv_python: Path,
    package_root: Path,
    *,
    expected_version: str,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    if not isinstance(expected_version, str) or not expected_version:
        raise ValueError("expected Chromium version is invalid")
    temporary_parent = package_root / "tmp"
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="acceptance-recording-", dir=temporary_parent
    ) as recording_directory:
        script = "\n".join(
            (
                "import json",
                "from pathlib import Path",
                "from audio_sound.config import configure_playwright_browser_path",
                "from playwright.sync_api import sync_playwright",
                f"recording_root = Path({json.dumps(recording_directory)})",
                "browser_root = configure_playwright_browser_path(Path.cwd())",
                "if browser_root is None:",
                "    raise RuntimeError('persisted Playwright runtime was not discovered')",
                "with sync_playwright() as runtime:",
                "    browser = runtime.chromium.launch(headless=True)",
                "    context = browser.new_context(record_video_dir=str(recording_root), "
                "record_video_size={'width': 320, 'height': 180})",
                "    page = context.new_page()",
                "    page.set_content('<!doctype html><title>offline</title><p>recording</p>')",
                "    page.wait_for_timeout(150)",
                "    version = browser.version",
                "    context.close()",
                "    browser.close()",
                "videos = sorted(recording_root.glob('*.webm'))",
                "video_size = max((path.stat().st_size for path in videos), default=0)",
                "print(json.dumps({'ok': True, 'browser': 'chromium', 'version': version, "
                "'recorded': bool(videos) and video_size > 0, 'video_size': video_size}, "
                "sort_keys=True))",
            )
        )
        completed = _require_offline_command(
            [str(venv_python), "-c", script],
            cwd=package_root,
            timeout=120,
            code="chromium_recording_probe",
            env=env or offline_subprocess_environment(),
        )
    payload = _parse_json_output(completed.stdout, "Chromium launch probe")
    if not (
        set(payload) == {"ok", "browser", "version", "recorded", "video_size"}
        and payload.get("ok") is True
        and payload.get("browser") == "chromium"
        and payload.get("version") == expected_version
        and payload.get("recorded") is True
        and isinstance(payload.get("video_size"), int)
        and not isinstance(payload.get("video_size"), bool)
        and int(payload["video_size"]) > 0
    ):
        raise ValueError("Chromium recording identity does not match the offline bundle")
    return {
        "status": "ready",
        "code": "chromium_recording_verified",
        "browser_version": expected_version,
        "recording": "ready",
    }


def _verify_installed_runtime(
    runtime_root: Path,
    offline_manifest: Mapping[str, object],
) -> dict[str, object]:
    rows = offline_manifest.get("files")
    manifest_sha256 = offline_manifest.get("manifest_sha256")
    if not isinstance(rows, list) or not isinstance(manifest_sha256, str):
        raise ValueError("offline runtime manifest evidence is invalid")
    expected: dict[str, tuple[int, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        relative = str(row.get("path", ""))
        if not relative.startswith(("browsers/", "tools/ffmpeg/")):
            continue
        size = row.get("size")
        digest = row.get("sha256")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("offline runtime manifest row is invalid")
        expected[relative] = (size, digest)
    if not expected:
        raise ValueError("offline runtime manifest inventory is empty")

    actual: dict[str, tuple[int, str]] = {}
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise ValueError("installed offline runtime is missing")
    for candidate in sorted(runtime_root.rglob("*")):
        if candidate.is_symlink():
            raise ValueError("installed offline runtime contains an unsafe entry")
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(runtime_root).as_posix()
        actual[relative] = (candidate.stat().st_size, _sha256_file(candidate))
    if set(actual) != set(expected):
        raise ValueError("installed offline runtime inventory does not match manifest")
    if any(actual[path] != expected[path] for path in expected):
        raise ValueError("installed offline runtime hash does not match manifest")
    return {
        "status": "ready",
        "code": "installed_runtime_hashes_verified",
        "file_count": len(expected),
        "manifest_sha256": manifest_sha256,
    }


def _contract_regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"offline {label} is missing or unsafe")
    return path


def _contract_json_mapping(path: Path, label: str) -> dict[str, object]:
    _contract_regular_file(path, label)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"offline {label} is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"offline {label} is invalid")
    return payload


def _contract_wheelhouse(path: Path, label: str) -> list[Path]:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"offline {label} is missing or unsafe")
    entries = sorted(path.iterdir(), key=lambda candidate: candidate.name.casefold())
    if not entries:
        raise ValueError(f"offline {label} is empty")
    if any(
        candidate.is_symlink()
        or not candidate.is_file()
        or candidate.suffix != ".whl"
        for candidate in entries
    ):
        raise ValueError(f"offline {label} contains a non-wheel entry")
    return entries


def _contract_tree_receipt(root: Path, label: str) -> dict[str, object]:
    """Return the deterministic file-tree receipt used by the offline builder.

    The companion manifest authenticates its own rows, but those rows are not
    an independent trust anchor.  Formal acceptance therefore recomputes the
    tree receipt and compares it with the fixed values in the program's
    committed ``offline_sources.json`` declaration.
    """
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"offline {label} tree is missing or unsafe")
    rows: list[dict[str, object]] = []
    for candidate in sorted(
        root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()
    ):
        if candidate.is_symlink():
            raise ValueError(f"offline {label} tree contains an unsafe symlink")
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root).as_posix()
        data = candidate.read_bytes()
        rows.append(
            {
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    canonical = (
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return {
        "file_count": len(rows),
        "total_size": sum(int(row["size"]) for row in rows),
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _validate_offline_native_contract(
    offline_root: Path,
    offline_manifest: Mapping[str, object],
    program_sources: Mapping[str, object],
) -> dict[str, object]:
    """Bind Chromium/native runtime bytes to committed source identities.

    A companion can otherwise rewrite both its file rows and component
    receipt, yielding a perfectly self-consistent but unreviewed executable.
    The release program carries fixed archive, executable, and complete-tree
    identities in ``scripts/release/offline_sources.json``; this gate checks
    every executed browser helper against those identities before setup or a
    target-local probe starts.
    """
    browser_source = program_sources.get("playwright_chromium")
    if not isinstance(browser_source, Mapping):
        raise ValueError("program Playwright source declaration is missing")
    native_binding_schema = browser_source.get("native_binding_schema")
    if (
        not isinstance(native_binding_schema, int)
        or isinstance(native_binding_schema, bool)
        or native_binding_schema != 1
    ):
        raise ValueError("program browser native binding declaration is invalid")
    fixed = {
        "playwright_version": "1.52.0",
        "revision": "1169",
        "browser_version": OFFLINE_CHROMIUM_VERSION,
        "ffmpeg_revision": OFFLINE_PLAYWRIGHT_FFMPEG_REVISION,
        "winldd_revision": OFFLINE_PLAYWRIGHT_WINLDD_REVISION,
    }
    if (
        browser_source.get("browser_version") != fixed["browser_version"]
        or any(
            browser_source.get(key) != value
            for key, value in fixed.items()
            if key != "browser_version"
        )
    ):
        raise ValueError("program Playwright source identity is invalid")

    rows_payload = offline_manifest.get("files")
    if not isinstance(rows_payload, list):
        raise ValueError("offline native file inventory is missing")
    manifest_rows: dict[str, Mapping[str, object]] = {}
    for row in rows_payload:
        if not isinstance(row, Mapping) or not isinstance(row.get("path"), str):
            raise ValueError("offline native file inventory is invalid")
        path = str(row["path"])
        if path in manifest_rows:
            raise ValueError("offline native file inventory contains duplicates")
        manifest_rows[path] = row

    tree_specs = (
        (
            "chromium",
            f"browsers/chromium-{fixed['revision']}",
            "chromium",
            "chrome-win/chrome.exe",
        ),
        (
            "headless_shell",
            f"browsers/chromium_headless_shell-{fixed['revision']}",
            "headless_shell",
            "chrome-win/headless_shell.exe",
        ),
        (
            "recording_ffmpeg",
            f"browsers/ffmpeg-{fixed['ffmpeg_revision']}",
            "ffmpeg",
            "ffmpeg-win64.exe",
        ),
        (
            "winldd",
            f"browsers/winldd-{fixed['winldd_revision']}",
            "winldd",
            "PrintDeps.exe",
        ),
    )
    component = offline_manifest.get("components", {})
    browser_component = (
        component.get("playwright_chromium") if isinstance(component, Mapping) else None
    )
    if not isinstance(browser_component, Mapping) or browser_component.get("included") is not True:
        raise ValueError("offline Chromium component receipt is missing")
    receipts = browser_component.get("tree_receipts")
    if not isinstance(receipts, Mapping):
        raise ValueError("offline Chromium tree receipts are missing")
    _validate_offline_browser_manifest_files(offline_root, manifest_rows, browser_source)

    measured: dict[str, object] = {}
    for label, root_relative, source_prefix, executable_relative in tree_specs:
        archive_size = browser_source.get(f"{source_prefix}_archive_size")
        archive_sha256 = browser_source.get(f"{source_prefix}_archive_sha256")
        archive_source = browser_source.get(
            "ffmpeg_source" if source_prefix == "ffmpeg" else f"{source_prefix}_source"
        )
        if (
            not isinstance(archive_size, int)
            or isinstance(archive_size, bool)
            or archive_size <= 0
            or not isinstance(archive_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", archive_sha256) is None
            or archive_sha256 == "0" * 64
            or not isinstance(archive_source, str)
            or not archive_source.strip()
        ):
            raise ValueError(f"offline {label} archive identity is invalid")

        expected_tree = {
            "file_count": browser_source.get(f"{source_prefix}_tree_file_count"),
            "total_size": browser_source.get(f"{source_prefix}_tree_total_size"),
            "tree_sha256": browser_source.get(f"{source_prefix}_tree_sha256"),
        }
        if (
            not isinstance(expected_tree["file_count"], int)
            or isinstance(expected_tree["file_count"], bool)
            or expected_tree["file_count"] <= 0
            or not isinstance(expected_tree["total_size"], int)
            or isinstance(expected_tree["total_size"], bool)
            or expected_tree["total_size"] <= 0
            or not isinstance(expected_tree["tree_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_tree["tree_sha256"]) is None
            or expected_tree["tree_sha256"] == "0" * 64
        ):
            raise ValueError(f"offline {label} tree identity is invalid")
        actual_tree = _contract_tree_receipt(offline_root / Path(*root_relative.split("/")), label)
        if actual_tree != expected_tree:
            raise ValueError(f"offline {label} tree bytes do not match committed identity")
        if receipts.get(label) != expected_tree:
            raise ValueError(f"offline {label} tree receipt does not match committed identity")

        executable_path = PurePosixPath(root_relative, executable_relative).as_posix()
        expected_size = browser_source.get(f"{source_prefix}_executable_size")
        expected_sha256 = browser_source.get(f"{source_prefix}_executable_sha256")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size <= 0
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or expected_sha256 == "0" * 64
        ):
            raise ValueError(f"offline {label} executable identity is invalid")
        executable = _contract_regular_file(
            offline_root / Path(*executable_path.split("/")), f"{label} executable"
        )
        actual_identity = (executable.stat().st_size, _sha256_file(executable))
        if actual_identity != (expected_size, expected_sha256):
            raise ValueError(f"offline {label} executable bytes do not match committed identity")
        row = manifest_rows.get(executable_path)
        if (
            not isinstance(row, Mapping)
            or row.get("size") != expected_size
            or row.get("sha256") != expected_sha256
        ):
            raise ValueError(f"offline {label} executable metadata is not source-bound")
        measured[label] = {
            "archive": {
                "size": archive_size,
                "sha256": archive_sha256,
                "source": archive_source,
            },
            "tree": actual_tree,
            "executable": {
                "path": executable_path,
                "size": expected_size,
                "sha256": expected_sha256,
            },
        }

    # These fields are consumed by the installer and must describe the same
    # fixed browser package that was just hashed above.
    if (
        browser_component.get("playwright_version") != fixed["playwright_version"]
        or browser_component.get("revision") != fixed["revision"]
        or browser_component.get("ffmpeg_revision") != fixed["ffmpeg_revision"]
        or browser_component.get("winldd_revision") != fixed["winldd_revision"]
        or browser_component.get("browser_version") != fixed["browser_version"]
        or browser_component.get("recording_ffmpeg_relative_path")
        != measured["recording_ffmpeg"]["executable"]["path"]
    ):
        raise ValueError("offline Chromium component identity is not source-bound")
    for key in ("chromium", "headless_shell"):
        source_prefix = key
        if (
            browser_component.get(f"{source_prefix}_executable_size")
            != measured[key]["executable"]["size"]
            or browser_component.get(f"{source_prefix}_executable_sha256")
            != measured[key]["executable"]["sha256"]
        ):
            raise ValueError("offline Chromium executable receipt is not source-bound")
    return {
        "status": "ready",
        "code": "offline_native_runtime_identity_verified",
        "native_component_count": len(tree_specs),
        "native_file_count": sum(
            int(value["tree"]["file_count"]) for value in measured.values()
        ),
        "native_components": measured,
    }


def _validate_offline_wheel_manifest_metadata(
    offline_root: Path,
    offline_manifest: Mapping[str, object],
    program_sources: Mapping[str, object],
) -> None:
    """Bind wheel manifest provenance to the wheel bytes and source inputs.

    Wheel hashes and lock closure prove that the selected bytes are allowed,
    but they do not authenticate the descriptive fields in each manifest row.
    Re-read the wheel's own ``METADATA`` and derive the expected component,
    platform, license, and source from the committed release declaration so a
    companion cannot silently rewrite provenance while keeping wheel bytes
    unchanged.
    """

    rows_payload = offline_manifest.get("files")
    if not isinstance(rows_payload, list):
        raise ValueError("offline wheel manifest inventory is missing")
    rows: dict[str, Mapping[str, object]] = {}
    for row in rows_payload:
        if not isinstance(row, Mapping) or not isinstance(row.get("path"), str):
            raise ValueError("offline wheel manifest inventory is invalid")
        path = str(row["path"])
        if path in rows:
            raise ValueError("offline wheel manifest inventory contains duplicates")
        rows[path] = row

    raw_overrides = program_sources.get("python_wheel_license_overrides", {})
    if raw_overrides is None:
        raw_overrides = {}
    if not isinstance(raw_overrides, Mapping):
        raise ValueError("program wheel license overrides are invalid")
    license_overrides: dict[str, str] = {}
    for raw_key, raw_value in raw_overrides.items():
        if (
            not isinstance(raw_key, str)
            or raw_key.count("==") != 1
            or not isinstance(raw_value, str)
        ):
            raise ValueError("program wheel license overrides are invalid")
        package_name, package_version = raw_key.split("==", 1)
        normalized_name = _normalized_project_name(package_name)
        normalized_version = package_version.strip()
        normalized_license = " ".join(raw_value.split())
        normalized_key = f"{normalized_name}=={normalized_version}"
        if (
            not normalized_name
            or not normalized_version
            or not normalized_license
            or normalized_key in license_overrides
        ):
            raise ValueError("program wheel license overrides are invalid")
        license_overrides[normalized_key] = normalized_license

    intervaltree_source = program_sources.get("intervaltree")
    if intervaltree_source is not None and not isinstance(intervaltree_source, Mapping):
        raise ValueError("program intervaltree source declaration is invalid")
    intervaltree_version = (
        intervaltree_source.get("version")
        if isinstance(intervaltree_source, Mapping)
        else None
    )
    intervaltree_url = (
        intervaltree_source.get("source")
        if isinstance(intervaltree_source, Mapping)
        else None
    )

    expected_paths: set[str] = set()
    for kind in ("main", "audio"):
        wheelhouse = _contract_wheelhouse(
            offline_root / "wheelhouse" / kind,
            f"{kind} wheelhouse",
        )
        component = f"{kind}_wheelhouse"
        for wheel in wheelhouse:
            relative = wheel.relative_to(offline_root).as_posix()
            expected_paths.add(relative)
            try:
                identity = read_wheel_identity(
                    wheel,
                    license_overrides=license_overrides,
                )
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                raise ValueError(f"offline wheel metadata is invalid: {relative}") from exc
            if (
                _normalized_project_name(identity["name"]) == "intervaltree"
                and identity["version"] == intervaltree_version
            ):
                if not isinstance(intervaltree_url, str) or not intervaltree_url.strip():
                    raise ValueError("program intervaltree source declaration is incomplete")
                source = intervaltree_url
            else:
                source = "https://pypi.org/" + chr(47).join(
                    ("project", identity["name"], identity["version"], "")
                )
            expected = {
                "component": component,
                "version": identity["version"],
                "platform": (
                    "win_amd64" if "win_amd64" in wheel.name.casefold() else "any"
                ),
                "license": identity["license"],
                "source": source,
            }
            row = rows.get(relative)
            actual = (
                row.get("size") if isinstance(row, Mapping) else None,
                row.get("sha256") if isinstance(row, Mapping) else None,
            )
            expected_bytes = (wheel.stat().st_size, _sha256_file(wheel))
            if (
                not isinstance(row, Mapping)
                or actual != expected_bytes
                or any(row.get(key) != value for key, value in expected.items())
            ):
                raise ValueError(f"offline wheel metadata is not source-bound: {relative}")

    manifest_wheel_paths = {
        path
        for path in rows
        if path.startswith(("wheelhouse/main/", "wheelhouse/audio/"))
    }
    if manifest_wheel_paths != expected_paths:
        raise ValueError("offline wheel manifest inventory does not match wheelhouses")


def _validate_offline_program_contract(
    package_root: Path,
    offline_root: Path,
    offline_manifest: Mapping[str, object],
    *,
    expected_source_commit: str,
) -> dict[str, object]:
    """Bind a formal companion to the committed program payload before execution.

    ``verify_offline_bundle`` authenticates a companion only against its own
    manifest.  That is necessary for transport integrity but insufficient for
    release acceptance: a caller could otherwise replace both a lock and its
    wheels, then present a self-consistent manifest.  This preflight compares
    the companion with the already extracted program ZIP and performs the
    complete hash-lock closure check while no package subprocess has run.
    """
    if not isinstance(expected_source_commit, str) or not expected_source_commit:
        raise ValueError("offline program source commit is invalid")
    if offline_manifest.get("source_commit") != expected_source_commit:
        raise ValueError("offline program source commit does not match the program")

    program_requirements = package_root
    companion_requirements = offline_root / "requirements"
    for name in _OFFLINE_CONTRACT_REQUIREMENT_FILES:
        program_path = _contract_regular_file(program_requirements / name, f"program {name}")
        companion_path = _contract_regular_file(
            companion_requirements / name,
            f"companion {name}",
        )
        if program_path.read_bytes() != companion_path.read_bytes():
            raise ValueError(f"offline requirements do not match program: {name}")

    program_sources = _contract_json_mapping(
        package_root / "scripts" / "release" / "offline_sources.json",
        "program offline source declaration",
    )
    if "release_source_commit" in program_sources:
        raise ValueError("program offline source declaration contains a generated identity")
    companion_sources = _contract_json_mapping(
        offline_root / "provenance" / "offline-sources.json",
        "companion offline source declaration",
    )
    # ``offline_sources.json`` is a committed release input.  The companion
    # provenance copy must be byte-for-byte equivalent at the parsed-object
    # level; generated release identity belongs in the signed bundle manifest,
    # not in this source declaration.  Keeping the two objects exact prevents
    # a self-consistent companion from silently selecting different native
    # assets or dependency origins.
    if companion_sources != program_sources:
        raise ValueError("offline source declaration does not match the program")

    native_evidence = _validate_offline_native_contract(
        offline_root,
        offline_manifest,
        program_sources,
    )

    try:
        main_direct = validate_direct_pin_parity(
            package_root / "requirements.txt",
            companion_requirements / "requirements-offline-main.lock",
        )
        audio_direct = validate_direct_pin_parity(
            package_root / "requirements-audio.lock",
            companion_requirements / "requirements-offline-audio.lock",
        )
    except (OSError, ValueError) as exc:
        raise ValueError("offline requirements direct pin parity is invalid") from exc

    main_wheelhouse = offline_root / "wheelhouse" / "main"
    audio_wheelhouse = offline_root / "wheelhouse" / "audio"
    main_wheels = _contract_wheelhouse(main_wheelhouse, "main wheelhouse")
    audio_wheels = _contract_wheelhouse(audio_wheelhouse, "audio wheelhouse")
    try:
        main_closure = validate_wheelhouse_lock_closure(
            main_wheelhouse,
            [
                companion_requirements / "requirements-offline-bootstrap.lock",
                companion_requirements / "requirements-offline-main.lock",
                companion_requirements / "requirements-offline-acceptance.lock",
            ],
        )
        audio_closure = validate_wheelhouse_lock_closure(
            audio_wheelhouse,
            [
                companion_requirements / "requirements-audio-build.lock",
                companion_requirements / "requirements-offline-audio.lock",
            ],
        )
    except (OSError, ValueError) as exc:
        raise ValueError("offline wheelhouse lock closure is invalid") from exc
    _validate_offline_wheel_manifest_metadata(
        offline_root,
        offline_manifest,
        program_sources,
    )
    return {
        "status": "ready",
        "code": "offline_program_contract_verified",
        "requirement_file_count": len(_OFFLINE_CONTRACT_REQUIREMENT_FILES),
        "main_wheel_count": len(main_wheels),
        "audio_wheel_count": len(audio_wheels),
        "main_lock_package_count": main_closure["lock_package_count"],
        "audio_lock_package_count": audio_closure["lock_package_count"],
        "main_direct_pin_count": main_direct["direct_pin_count"],
        "audio_direct_pin_count": audio_direct["direct_pin_count"],
        "native_runtime": native_evidence,
    }


def _native_tools_probe(
    runtime_root: Path,
    package_root: Path,
    *,
    expected_version: str,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    if not isinstance(expected_version, str) or not expected_version:
        raise ValueError("expected FFmpeg version is invalid")
    tool_root = runtime_root / "tools" / "ffmpeg" / "bin"
    components: dict[str, dict[str, object]] = {}
    environment = env or offline_subprocess_environment()
    for name in ("ffmpeg", "ffprobe"):
        executable = tool_root / f"{name}.exe"
        if executable.is_symlink() or not executable.is_file():
            raise ValueError(f"installed offline {name} executable is missing")
        completed = _require_offline_command(
            [str(executable), "-version"],
            cwd=package_root,
            timeout=120,
            code=f"{name}_version_probe",
            env=environment,
        )
        output = "\n".join(
            value
            for value in (completed.stdout, completed.stderr)
            if isinstance(value, str) and value
        )
        first_line = output.splitlines()[0] if output.splitlines() else ""
        identity_match = re.match(
            rf"^{re.escape(name)}\s+version\s+(\S+)(?:\s|$)",
            first_line,
        )
        measured_version = identity_match.group(1) if identity_match else ""
        if measured_version != expected_version:
            raise ValueError(f"installed offline {name} identity is invalid")
        components[name] = {"status": "ready", "version": measured_version}
    return {
        "status": "ready",
        "code": "offline_native_tools_verified",
        "components": components,
    }


def _data_schema_probe(
    venv_python: Path,
    package_root: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    _require_offline_command(
        [str(venv_python), "-I", "tools/validate_data_schema.py"],
        cwd=package_root,
        timeout=120,
        code="data_schema",
        env=env or offline_subprocess_environment(),
    )
    return {"status": "ready", "code": "data_schema_verified"}


def _jianying_probe(
    venv_python: Path,
    package_root: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    completed = _run(
        [
            str(venv_python),
            "scripts/jy_wrapper.py",
            "self-check",
            "--cleanup",
            "--refresh",
            "--json",
        ],
        cwd=package_root,
        timeout=180,
        env=env or offline_subprocess_environment(),
        enforce_no_network=True,
    )
    payload = _parse_json_output(completed.stdout, "JianYing self-check")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    smoke = checks.get("smoke_test") if isinstance(checks.get("smoke_test"), dict) else {}
    draft_path = smoke.get("draft_path") if isinstance(smoke, dict) else None
    usable = payload.get("ok") is True and data.get("usable") is True
    if usable and (
        not isinstance(smoke, dict)
        or smoke.get("ok") is not True
        or not isinstance(draft_path, str)
        or not draft_path.strip()
    ):
        raise ValueError("JianYing smoke path cleanup evidence is missing")
    if isinstance(draft_path, str) and draft_path.strip():
        smoke_path = Path(draft_path)
        if not smoke_path.is_absolute():
            smoke_path = package_root / smoke_path
        if os.path.lexists(smoke_path):
            raise ValueError("JianYing smoke draft cleanup verification failed")
    if usable and not smoke_editability_receipt_valid(smoke.get("structure")):
        raise ValueError("JianYing editable smoke-draft structure evidence is missing")
    if usable and completed.returncode != 0:
        raise ValueError("JianYing self-check returned an inconsistent exit status")
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    blocked = (
        completed.returncode == 1
        and payload.get("ok") is False
        and payload.get("code") == "runtime_error"
        and data.get("usable") is False
        and summary.get("status") == "blocked"
        and blocked_jianying_checks_valid(checks)
    )
    if not usable and not blocked:
        raise ValueError("JianYing self-check failed without local-software evidence")
    return {
        "status": "ready" if usable else "pending",
        "code": "jianying_smoke_cleanup_verified" if usable else "requires_local_jianying",
    }


def _audio_probe(
    venv_python: Path,
    package_root: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    audio_python = package_root / ".venv-audio" / "Scripts" / "python.exe"
    completed = _require_offline_command(
        [
            str(venv_python),
            "scripts/audio/audio_cleanup.py",
            "doctor",
            "--python-executable",
            str(audio_python),
        ],
        cwd=package_root,
        timeout=180,
        code="audio_doctor",
        env=env or offline_subprocess_environment(),
    )
    return _parse_json_output(completed.stdout, "audio doctor")


def _install_acceptance_test_dependencies(
    venv_python: Path,
    package_root: Path,
    offline_root: Path,
    *,
    env: dict[str, str] | None = None,
) -> None:
    wheelhouse = (offline_root / "wheelhouse" / "main").resolve()
    if not wheelhouse.is_dir():
        raise ValueError("offline main wheelhouse is missing")
    acceptance_lock = offline_root / "requirements" / "requirements-offline-acceptance.lock"
    if acceptance_lock.is_symlink() or not acceptance_lock.is_file():
        raise ValueError("offline acceptance dependency lock is missing")
    _require_offline_command(
        [
            str(venv_python),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--requirement",
            str(acceptance_lock),
        ],
        cwd=package_root,
        timeout=900,
        code="acceptance_test_dependency_install",
        env=env or offline_subprocess_environment(),
    )


def _run_release_tests(
    venv_python: Path,
    package_root: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    command = [str(venv_python), "-I", "-m", "pytest", *RELEASE_TESTS, "-q"]
    completed = _require_offline_command(
        command,
        cwd=package_root,
        timeout=600,
        code="no_git_release_tests",
        env=env or offline_subprocess_environment(),
    )
    return {
        "status": "ready",
        "code": "no_git_release_tests_passed",
        "output_sha256": _sha256_bytes(completed.stdout.encode("utf-8")),
    }


def _capability_groups(
    package_root: Path,
    install_report: dict[str, Any],
) -> dict[str, list[str]]:
    manifest = json.loads((package_root / "capability-manifest.json").read_text(encoding="utf-8"))
    manifest_ids = [str(row["id"]) for row in manifest["capabilities"]]
    measured_rows = [
        row for row in install_report.get("capability_table", []) if isinstance(row, dict)
    ]
    measured_ids = [str(row.get("id")) for row in measured_rows if isinstance(row.get("id"), str)]
    if len(measured_ids) != len(set(measured_ids)):
        raise ValueError("install report capability set contains duplicate ids")
    missing = sorted(set(manifest_ids) - set(measured_ids))
    extra = sorted(set(measured_ids) - set(manifest_ids))
    if missing or extra or len(measured_ids) != len(manifest_ids):
        raise ValueError(
            "install report capability set mismatch: " f"missing={len(missing)} extra={len(extra)}"
        )
    measured = {str(row["id"]): str(row.get("status")) for row in measured_rows}
    allowed_statuses = {"ready", "degraded", "pending", "unavailable", "failed", "skipped"}
    invalid_statuses = sorted(
        capability_id
        for capability_id, status in measured.items()
        if status not in allowed_statuses
    )
    if invalid_statuses:
        raise ValueError("install report contains invalid capability status")
    groups = {
        "ready": [],
        "degraded": [],
        "pending": [],
        "failed": [],
        "skipped": [],
        "requires_authorization": [],
        "requires_user_assets": [],
        "requires_local_software": [],
        "unavailable": [],
    }
    for capability in manifest["capabilities"]:
        capability_id = capability["id"]
        measured_status = measured[capability_id]
        if measured_status == "ready":
            groups["ready"].append(capability_id)
        elif measured_status == "degraded":
            groups["degraded"].append(capability_id)
        elif measured_status == "failed":
            groups["failed"].append(capability_id)
        elif measured_status == "skipped":
            groups["skipped"].append(capability_id)
        elif measured_status == "unavailable" or capability["unavailable"]:
            groups["unavailable"].append(capability_id)
        elif capability["requires_user_authorization"]:
            groups["requires_authorization"].append(capability_id)
        elif capability["requires_user_assets"]:
            groups["requires_user_assets"].append(capability_id)
        elif capability["requires_local_jianying"] or capability["actual_result"]["code"] in {
            "requires_local_software",
            "requires_local_jianying",
        }:
            groups["requires_local_software"].append(capability_id)
        else:
            groups["pending"].append(capability_id)
    return {key: sorted(values) for key, values in groups.items()}


@contextmanager
def _acceptance_workspace(isolation: Any) -> Iterator[Path]:
    configured_root = getattr(isolation, "workspace_root", None)
    if configured_root is None:
        with tempfile.TemporaryDirectory(prefix="auto-cut-release-accept-") as temporary:
            yield Path(temporary)
        return
    root = Path(configured_root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("offline acceptance workspace is unavailable")
    yield root


def _prepare_isolated_directory(isolation: Any, path: Path) -> Path:
    prepare = getattr(isolation, "prepare_directory", None)
    if callable(prepare):
        return Path(prepare(path))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _accept_release_in_workspace(
    zip_path: Path,
    offline_bundle_path: Path,
    private_assets_path: Path | None = None,
    *,
    isolation: Any,
) -> dict[str, object]:
    archive = zip_path.resolve()
    offline_archive = offline_bundle_path.resolve()
    private_archive = private_assets_path.resolve() if private_assets_path is not None else None
    with _acceptance_workspace(isolation) as temporary_root:
        temporary = str(temporary_root)
        package_root = _prepare_isolated_directory(isolation, temporary_root / "Auto-Cut")
        offline_parent = _prepare_isolated_directory(
            isolation, temporary_root / "offline-deps-workspace"
        )
        offline_root = offline_parent / "payload"
        with archive.open("rb") as archive_stream:
            archive_sha256 = _sha256_stream(archive_stream)
            safe_extract(archive_stream, package_root)
        _verify_archive_path_digest(archive, archive_sha256)
        inventory = verify_inventory(package_root)
        inventory_snapshot = capture_inventory_state(package_root, inventory)
        expected_program_name = f"Auto-Cut-v{inventory['version']}-windows-x64.zip"
        if archive.name != expected_program_name:
            raise ValueError("program ZIP filename does not match the release version")
        expected_offline_name = f"Auto-Cut-v{inventory['version']}-windows-x64-offline-deps.zip"
        if offline_archive.name != expected_offline_name:
            raise ValueError("offline bundle filename does not match the release version")
        offline_snapshot = Path(temporary) / "offline-deps-snapshot.zip"
        offline_archive_sha256 = _snapshot_archive(offline_archive, offline_snapshot)
        extracted_offline = extract_offline_bundle(offline_snapshot, offline_root)
        _verify_archive_path_digest(
            offline_archive,
            offline_archive_sha256,
            artifact="offline bundle ZIP",
        )
        verified_offline = verify_offline_bundle(
            offline_root,
            expected_version=str(inventory["version"]),
            expected_source_commit=str(inventory["source_commit"]),
        )
        if extracted_offline.get("manifest_sha256") != verified_offline.get("manifest_sha256"):
            raise ValueError("offline bundle verification changed after extraction")
        offline_manifest = verified_offline["manifest"]
        offline_components = offline_manifest.get("components")
        if not isinstance(offline_components, dict):
            raise ValueError("offline bundle component evidence is missing")
        offline_contract_evidence = None
        if _requires_formal_offline_contract(str(inventory["version"])):
            # This is deliberately before any extracted package command,
            # installer invocation, or native/browser probe.  The companion's
            # self-hash authenticates transport only; this binds its locks,
            # source declaration, and wheel closure to the program ZIP.
            offline_contract_evidence = _validate_offline_program_contract(
                package_root,
                offline_root,
                offline_manifest,
                expected_source_commit=str(inventory["source_commit"]),
            )
        chromium_component = offline_components.get("playwright_chromium")
        ffmpeg_component = offline_components.get("ffmpeg")
        if not (
            isinstance(chromium_component, dict)
            and chromium_component.get("included") is True
            and chromium_component.get("launch_verified") is True
            and chromium_component.get("record_video_verified") is True
            and chromium_component.get("ffmpeg_revision") == OFFLINE_PLAYWRIGHT_FFMPEG_REVISION
            and chromium_component.get("winldd_revision") == OFFLINE_PLAYWRIGHT_WINLDD_REVISION
            and chromium_component.get("winldd_source_verified") is True
            and isinstance(chromium_component.get("browser_version"), str)
            and chromium_component.get("browser_version")
        ):
            raise ValueError("offline Chromium component evidence is invalid")
        if not (
            isinstance(ffmpeg_component, dict)
            and ffmpeg_component.get("included") is True
            and ffmpeg_component.get("ffmpeg_verified") is True
            and ffmpeg_component.get("ffprobe_verified") is True
            and isinstance(ffmpeg_component.get("version"), str)
            and ffmpeg_component.get("version")
        ):
            raise ValueError("offline FFmpeg component evidence is invalid")
        # Bind the acceptance result to the reviewed, reproducible minimal
        # FFmpeg bytes.  The installer performs the same check when a release
        # inventory source commit is present; acceptance must fail before any
        # target-local probes if the companion advertises a self-consistent but
        # unreviewed native executable.
        _validate_fixed_offline_ffmpeg(
            offline_root,
            offline_manifest,
            ffmpeg_component,
            strict=True,
        )
        offline_evidence = {
            "status": "ready",
            "mode": "offline_only",
            "zip_name": offline_archive.name,
            "zip_sha256": offline_archive_sha256,
            "manifest_sha256": verified_offline["manifest_sha256"],
            "source_commit": offline_manifest["source_commit"],
            "file_count": verified_offline["file_count"],
            "target": dict(offline_manifest["target"]),
        }
        if isinstance(offline_contract_evidence, dict):
            offline_evidence["program_contract"] = offline_contract_evidence
        private_evidence = None
        private_snapshot = None
        if private_archive is not None:
            private_snapshot = Path(temporary) / "private-assets-snapshot.zip"
            private_evidence = _verify_private_assets_archive(
                private_archive,
                private_snapshot,
                expected_version=str(inventory["version"]),
                expected_source_commit=str(inventory["source_commit"]),
            )
        if (package_root / ".git").exists():
            raise ValueError("clean extraction unexpectedly contains .git")
        release_paths = [str(row["path"]) for row in inventory["files"]]
        findings = scan_release_tree(package_root, release_paths)
        if findings:
            raise ValueError(f"release privacy scan failed: {findings[0].code}")
        capability_audit = _runtime_capability_probe(package_root, release_paths)

        acceptance_environment = offline_subprocess_environment()
        bootstrap_root = _prepare_isolated_directory(
            isolation, Path(temporary) / "acceptance-python"
        )
        bootstrap_python = Path(isolation.prepare_python_runtime(bootstrap_root))
        acceptance_environment["PATH"] = os.pathsep.join(
            (str(bootstrap_python.parent), acceptance_environment.get("PATH", ""))
        )
        acceptance_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        acceptance_environment["PYTHONUTF8"] = "1"
        runtime_root = package_root / "tmp" / "offline-runtime"
        setup = _require_offline_command(
            [
                str(bootstrap_python),
                "scripts/full_setup.py",
                "--json",
                "--onboarding-only",
                "--offline-bundle",
                str(offline_snapshot),
            ],
            cwd=package_root,
            timeout=10800,
            code="unified_setup",
            env=acceptance_environment,
        )
        _clear_playwright_runtime_markers(runtime_root / "browsers")
        post_setup_offline = verify_offline_bundle(
            offline_root,
            expected_version=str(inventory["version"]),
            expected_source_commit=str(inventory["source_commit"]),
        )
        if post_setup_offline.get("manifest_sha256") != verified_offline.get("manifest_sha256"):
            raise ValueError("offline bundle manifest changed during setup")
        setup_payload = _parse_json_output(setup.stdout, "unified setup")
        saved_report = json.loads(
            (package_root / "tmp" / "install" / "install-report.json").read_text(encoding="utf-8")
        )
        install_report_path = package_root / "tmp" / "install" / "install-report.json"
        install_report_sha256 = _sha256_file(install_report_path)
        if setup_payload != saved_report:
            raise ValueError("saved install report does not match setup output")
        install_evidence = evaluate_install_report(
            saved_report,
            expected_version=str(inventory["version"]),
            expected_manifest_sha256=_sha256_file(package_root / "capability-manifest.json"),
            expected_offline=offline_evidence,
        )

        skill_evidence = verify_skill_parity(
            package_root / "skills", package_root / ".codex" / "skills"
        )
        venv_python = package_root / ".venv" / "Scripts" / "python.exe"
        private_round_trip = None
        if isinstance(private_evidence, dict) and isinstance(private_snapshot, Path):
            private_round_trip = _verify_private_profile_round_trip(
                venv_python,
                package_root,
                private_snapshot,
                Path(temporary) / "private-profile-round-trip",
                env=acceptance_environment,
            )
            private_evidence["registry_round_trip"] = private_round_trip["status"]
        _main_import_probe(venv_python, package_root, env=acceptance_environment)
        chromium_evidence = _chromium_probe(
            venv_python,
            package_root,
            expected_version=str(chromium_component["browser_version"]),
            env=acceptance_environment,
        )
        _clear_playwright_runtime_markers(runtime_root / "browsers")
        native_tool_evidence = _native_tools_probe(
            runtime_root,
            package_root,
            expected_version=str(ffmpeg_component["version"]),
            env=acceptance_environment,
        )
        data_schema_evidence = _data_schema_probe(
            venv_python, package_root, env=acceptance_environment
        )
        jianying_evidence = _jianying_probe(venv_python, package_root, env=acceptance_environment)
        doctor = _audio_probe(
            venv_python,
            package_root,
            env=acceptance_environment,
        )
        verify_fresh_audio_doctor(install_evidence["audio_evidence"], doctor)
        _install_acceptance_test_dependencies(
            venv_python,
            package_root,
            offline_root,
            env=acceptance_environment,
        )
        no_git_evidence = _run_release_tests(venv_python, package_root, env=acceptance_environment)
        capability_groups = _capability_groups(package_root, saved_report)
        _assert_no_playwright_runtime_markers(runtime_root / "browsers")
        verify_post_acceptance_inventory(package_root, inventory_snapshot)
        runtime_integrity_evidence = _verify_installed_runtime(runtime_root, offline_manifest)
        isolation_evidence = dict(isolation.evidence)
        final_offline = verify_offline_bundle(
            offline_root,
            expected_version=str(inventory["version"]),
            expected_source_commit=str(inventory["source_commit"]),
        )
        if final_offline.get("manifest_sha256") != verified_offline.get("manifest_sha256"):
            raise ValueError("offline bundle manifest changed during acceptance")
        _verify_archive_path_digest(archive, archive_sha256)
        _verify_archive_path_digest(
            offline_archive,
            offline_archive_sha256,
            artifact="offline bundle ZIP",
        )
        if private_archive is not None and isinstance(private_evidence, dict):
            _verify_archive_path_digest(
                private_archive,
                str(private_evidence["zip_sha256"]),
                artifact="private asset ZIP",
            )

        checks = [
            isolation_evidence | {"id": "offline_execution"},
            {"id": "archive_inventory", "status": "ready", "code": "inventory_verified"},
            {
                "id": "offline_bundle",
                "status": "ready",
                "code": "offline_bundle_verified",
                "manifest_sha256": verified_offline["manifest_sha256"],
            },
            {
                "id": "install_report",
                "status": "ready",
                "code": "install_report_verified",
                "sha256": install_report_sha256,
            },
            {
                "id": "offline_install",
                "status": str(install_evidence["offline_status"]),
                "code": "offline_install_receipt_verified",
            },
            capability_audit | {"id": "runtime_capabilities"},
            skill_evidence | {"id": "skills"},
            {"id": "main_imports", "status": "ready", "code": "imports_verified"},
            chromium_evidence | {"id": "chromium"},
            native_tool_evidence | {"id": "native_tools"},
            runtime_integrity_evidence | {"id": "installed_runtime"},
            data_schema_evidence | {"id": "data_schema"},
            jianying_evidence | {"id": "jianying"},
            {
                "id": "audio_doctor",
                "status": str(doctor.get("status")),
                "code": "audio_doctor_truth_preserved",
            },
            no_git_evidence | {"id": "no_git_tests"},
            {"id": "privacy", "status": "ready", "code": "privacy_scan_passed"},
        ]
        if isinstance(offline_contract_evidence, dict):
            checks.insert(
                3,
                offline_contract_evidence | {"id": "offline_program_contract"},
            )
        if isinstance(private_evidence, dict):
            checks.insert(
                2,
                {
                    "id": "private_assets",
                    "status": "ready",
                    "code": str(private_round_trip["code"]),
                    "manifest_sha256": private_evidence["manifest_sha256"],
                },
            )
        physical_network_evidence = {
            "status": "not_required",
            "code": "physical_network_isolation_not_claimed",
            "required": False,
            "verified": False,
        }
        return {
            "schema_version": 1,
            "status": "ready",
            "version": inventory["version"],
            "source_commit": inventory["source_commit"],
            "zip_name": archive.name,
            "zip_sha256": archive_sha256,
            "offline_bundle": offline_evidence,
            "offline_execution": isolation_evidence,
            "physical_network_isolation": dict(physical_network_evidence),
            # Deprecated schema-v1 compatibility alias.  Historically this
            # field carried the execution-guard evidence now named above.
            "offline_network_isolation": dict(isolation_evidence),
            "private_assets": private_evidence,
            "inventory_sha256": inventory["inventory_sha256"],
            "install_report_sha256": install_report_sha256,
            "checks": checks,
            "audio_components": {
                "status": doctor.get("status"),
                "spectramini": _component_status(doctor.get("spectramini")),
                "deepfilternet": _component_status(doctor.get("deepfilternet")),
                "respiro": _component_status(doctor.get("respiro_en", doctor.get("respiro"))),
            },
            "capability_groups": capability_groups,
        }


def accept_release(
    zip_path: Path,
    offline_bundle_path: Path,
    private_assets_path: Path | None = None,
) -> dict[str, object]:
    with _open_offline_isolation() as isolation:
        with _activate_offline_isolation(isolation):
            return _accept_release_in_workspace(
                zip_path,
                offline_bundle_path,
                private_assets_path,
                isolation=isolation,
            )


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Auto-Cut v{report['version']} Release Acceptance",
        "",
        f"- Status: `{report['status']}`",
        f"- Source commit: `{report['source_commit']}`",
        f"- ZIP SHA-256: `{report['zip_sha256']}`",
    ]
    offline = report.get("offline_bundle")
    if isinstance(offline, dict):
        lines.extend(
            (
                f"- Offline mode: `{offline.get('mode', 'offline_only')}`",
                f"- Offline bundle: `{offline.get('zip_name', '')}`",
                f"- Offline bundle SHA-256: `{offline.get('zip_sha256', '')}`",
                f"- Offline manifest SHA-256: `{offline.get('manifest_sha256', '')}`",
            )
        )
    execution = report.get("offline_execution")
    if not isinstance(execution, dict):
        legacy_execution = report.get("offline_network_isolation")
        execution = legacy_execution if isinstance(legacy_execution, dict) else None
    if isinstance(execution, dict):
        physical = report.get("physical_network_isolation")
        physical_status = (
            physical.get("status", "not_claimed")
            if isinstance(physical, dict)
            else "not_claimed"
        )
        lines.extend(
            (
                f"- Offline acceptance guard: `{execution.get('code', '')}`",
                f"- Physical network isolation: `{physical_status}` (not required)",
                "- Network security sandbox: `not_claimed` (trusted release commands only)",
            )
        )
    private_assets = report.get("private_assets")
    if isinstance(private_assets, dict):
        lines.extend(
            (
                f"- Private assets: `{private_assets.get('zip_name', '')}`",
                f"- Private assets SHA-256: `{private_assets.get('zip_sha256', '')}`",
                "- Private manifest SHA-256: " f"`{private_assets.get('manifest_sha256', '')}`",
            )
        )
    lines.extend(
        (
            "",
            "## Checks",
            "",
            "| Check | Status | Result |",
            "| --- | --- | --- |",
        )
    )
    for check in report.get("checks", []):
        lines.append(f"| `{check['id']}` | `{check['status']}` | `{check['code']}` |")
    lines.extend(("", "## Capability Groups", ""))
    for group, capability_ids in report.get("capability_groups", {}).items():
        value = ", ".join(f"`{item}`" for item in capability_ids) or "None"
        lines.append(f"- {group}: {value}")
    return "\n".join(lines) + "\n"


def _write_report(path: Path, content: str) -> tuple[int, int, int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with unique_sibling_temp(path) as temporary:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        publish_file_no_replace(temporary, path)
        identity = _report_file_identity(path)
        if identity is None:
            raise OSError("published report identity is unavailable")
        return identity


def _report_file_identity(path: Path) -> tuple[int, int, int, str] | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        _sha256_file(path),
    )


def _paths_alias(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    if os.path.lexists(left) and os.path.lexists(right):
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _validate_cli_artifact_paths(
    program_zip: Path,
    offline_bundle: Path,
    private_assets: Path | None,
    report_paths: Sequence[Path],
) -> str:
    match = _PROGRAM_ZIP_NAME.fullmatch(program_zip.name)
    if match is None:
        raise ValueError("program ZIP filename is invalid")
    version = match.group("version")
    if offline_bundle.name != f"Auto-Cut-v{version}-windows-x64-offline-deps.zip":
        raise ValueError("offline bundle filename does not match the program ZIP")
    if _requires_formal_offline_contract(version) and private_assets is None:
        raise ValueError("private asset archive is required for formal release acceptance")
    if private_assets is not None and private_assets.name != (
        f"Auto-Cut-v{version}-private-assets-high-school-history.zip"
    ):
        raise ValueError("private asset filename does not match the program ZIP")
    expected_report_names = (
        f"Auto-Cut-v{version}-windows-x64-acceptance.json",
        f"Auto-Cut-v{version}-windows-x64-acceptance.md",
    )
    if tuple(path.name for path in report_paths) != expected_report_names:
        raise ValueError("acceptance report filenames do not match the program ZIP")
    input_paths = [program_zip, offline_bundle]
    if private_assets is not None:
        input_paths.append(private_assets)
    all_paths = [*input_paths, *report_paths]
    for index, left in enumerate(all_paths):
        for right in all_paths[index + 1 :]:
            if _paths_alias(left, right):
                raise ValueError("release artifact paths must be distinct and non-aliasing")
    for path in report_paths:
        if os.path.lexists(path):
            raise FileExistsError("acceptance report output already exists")
    return version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Accept an Auto-Cut ZIP from a clean extraction.")
    parser.add_argument("--zip", required=True, dest="zip_path")
    parser.add_argument("--offline-bundle", required=True)
    parser.add_argument("--private-assets")
    parser.add_argument("--json-report", required=True)
    parser.add_argument("--markdown-report", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    program_zip = Path(args.zip_path)
    offline_bundle = Path(args.offline_bundle)
    private_assets = Path(args.private_assets) if args.private_assets else None
    report_paths = (Path(args.json_report), Path(args.markdown_report))
    published_reports: list[tuple[Path, tuple[int, int, int, str]]] = []
    try:
        expected_version = _validate_cli_artifact_paths(
            program_zip,
            offline_bundle,
            private_assets,
            report_paths,
        )
        report = accept_release(
            program_zip,
            offline_bundle,
            private_assets,
        )
        if report.get("version") != expected_version:
            raise ValueError("acceptance report version does not match artifact filenames")
        publication = _write_report(
            report_paths[0],
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        if not isinstance(publication, tuple) or len(publication) != 4:
            fallback_identity = _report_file_identity(report_paths[0])
            if fallback_identity is None:
                raise OSError("published JSON report identity is unavailable")
            publication = fallback_identity
        published_reports.append((report_paths[0], publication))
        publication = _write_report(report_paths[1], render_markdown_report(report))
        if not isinstance(publication, tuple) or len(publication) != 4:
            fallback_identity = _report_file_identity(report_paths[1])
            if fallback_identity is None:
                raise OSError("published Markdown report identity is unavailable")
            publication = fallback_identity
        published_reports.append((report_paths[1], publication))
    except Exception as exc:
        cleanup_failed = False
        for path, identity in published_reports:
            try:
                if _report_file_identity(path) == identity:
                    path.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        failure: dict[str, object] = {
            "status": "failed",
            "code": (
                "release_report_cleanup_failed" if cleanup_failed else "release_acceptance_failed"
            ),
            "error_type": type(exc).__name__,
        }
        if isinstance(exc, ReleaseCommandError):
            failure.update(
                failure_stage=exc.stage,
                command_returncode=exc.returncode,
            )
            if exc.detail:
                failure["failure_detail"] = exc.detail
        print(json.dumps(failure))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
