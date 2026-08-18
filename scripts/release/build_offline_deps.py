from __future__ import annotations

import argparse
import base64
import email.parser
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.release import release_policy
from scripts.release.offline_bundle import sha256_file, write_offline_bundle
from scripts.release.release_policy import normalize_archive_path
from scripts.release.release_transaction import (
    ReleaseSource,
    assert_release_source_unchanged,
    capture_clean_release_source,
    publish_file_no_replace,
)

PLAYWRIGHT_VERSION = "1.52.0"
CHROMIUM_REVISION = "1169"
CHROMIUM_VERSION = "136.0.7103.25"
PLAYWRIGHT_FFMPEG_REVISION = "1011"
PLAYWRIGHT_WINLDD_REVISION = "1007"
BUILD_PIP_VERSION = "26.1.2"
REQUIREMENT_FILES = (
    "requirements.txt",
    "requirements-offline-main.lock",
    "requirements-audio.lock",
    "requirements-offline-audio.lock",
    "requirements-audio-build.lock",
    "requirements-offline-acceptance.lock",
    "requirements-offline-bootstrap.lock",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
_IMMUTABLE_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}")
_FFMPEG_PROGRAMS = ("ffmpeg.exe", "ffprobe.exe")
_INTERVALTREE_REQUIREMENT = "intervaltree==3.1.0"
_NORMALIZED_WHEEL_TIME = (2026, 1, 1, 0, 0, 0)
_PLAYWRIGHT_RUNTIME_MARKER = "DEPENDENCIES_VALIDATED"

# Kept as a public compatibility symbol for callers/tests that inject a
# captured release identity while exercising the builder in isolation.
__all__ = ["ReleaseSource", "build_offline_deps", "build_parser", "main"]


def _target_host_identity() -> dict[str, object]:
    """Return the concrete host identity used for native/wheel collection."""

    return {
        "os": platform.system().casefold(),
        "implementation": str(sys.implementation.name).casefold(),
        "python_version": tuple(sys.version_info[:2]),
        "machine": platform.machine().casefold(),
        "pointer_bits": struct.calcsize("P") * 8,
    }


def _assert_target_build_host() -> None:
    identity = _target_host_identity()
    machine = str(identity.get("machine", "")).casefold()
    if not (
        identity.get("os") == "windows"
        and identity.get("implementation") == "cpython"
        and identity.get("python_version") == (3, 11)
        and machine in {"amd64", "x86_64"}
        and identity.get("pointer_bits") == 64
    ):
        raise RuntimeError("offline dependency build requires Windows x64 CPython 3.11")


def _https_url(host: str, path: str) -> str:
    return urllib.parse.urlunsplit(("https", host, "/" + path.lstrip("/"), "", ""))


def pip_download_command(
    python_executable: str,
    destination: Path,
    requirement_files: Sequence[Path],
    *,
    find_links: Sequence[Path] = (),
) -> list[str]:
    command = [
        python_executable,
        "-I",
        "-m",
        "pip",
        "download",
        "--isolated",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--only-binary=:all:",
        "--require-hashes",
        "--platform",
        "win_amd64",
        "--implementation",
        "cp",
        "--python-version",
        "3.11",
        "--abi",
        "cp311",
    ]
    for link in find_links:
        command.extend(("--find-links", str(link)))
    command.extend(("--dest", str(destination)))
    for requirement in requirement_files:
        command.extend(("--requirement", str(requirement)))
    return command


def _normalized_project_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _direct_pin_entries(requirement_path: Path) -> dict[str, str]:
    requirement_pattern = re.compile(
        r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?==(?P<version>[^\s]+)$"
    )
    entries: dict[str, str] = {}
    for raw_line in requirement_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = requirement_pattern.fullmatch(stripped)
        if match is None:
            raise ValueError(f"direct requirement is not an exact pin: {requirement_path.name}")
        name = _normalized_project_name(match.group("name"))
        version = match.group("version")
        if name in entries and entries[name] != version:
            raise ValueError(
                f"direct requirements contain conflicting pins: {requirement_path.name}"
            )
        entries[name] = version
    if not entries:
        raise ValueError(f"direct requirements are empty: {requirement_path.name}")
    return entries


def _compiled_lock_entries(lock_path: Path) -> dict[tuple[str, str], set[str]]:
    logical_lines: list[str] = []
    pending = ""
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continued else stripped
        pending = f"{pending} {fragment}".strip()
        if continued:
            continue
        logical_lines.append(pending)
        pending = ""
    if pending:
        raise ValueError(f"compiled lock has an unterminated continuation: {lock_path.name}")

    entries: dict[tuple[str, str], set[str]] = {}
    requirement_pattern = re.compile(
        r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?==(?P<version>[^\s]+)$"
    )
    hash_pattern = re.compile(r"--hash=sha256:([0-9a-f]{64})")
    for line in logical_lines:
        tokens = line.split()
        match = requirement_pattern.fullmatch(tokens[0]) if tokens else None
        hashes = {
            hash_match.group(1)
            for token in tokens[1:]
            if (hash_match := hash_pattern.fullmatch(token))
        }
        if match is None or not hashes or len(hashes) != len(tokens) - 1:
            raise ValueError(f"compiled lock entry is not an exact hashed pin: {lock_path.name}")
        key = (_normalized_project_name(match.group("name")), match.group("version"))
        for existing_name, existing_version in entries:
            if existing_name == key[0] and existing_version != key[1]:
                raise ValueError(f"compiled lock contains conflicting versions: {lock_path.name}")
        entries.setdefault(key, set()).update(hashes)
    if not entries:
        raise ValueError(f"compiled lock is empty: {lock_path.name}")
    return entries


def validate_direct_pin_parity(direct_requirements: Path, compiled_lock: Path) -> dict[str, int]:
    direct = _direct_pin_entries(direct_requirements)
    compiled = {name: version for name, version in _compiled_lock_entries(compiled_lock)}
    if any(compiled.get(name) != version for name, version in direct.items()):
        raise ValueError("direct pin parity does not match the compiled lock")
    return {"direct_pin_count": len(direct)}


def validate_wheelhouse_lock_closure(
    wheelhouse: Path, lock_files: Sequence[Path]
) -> dict[str, int]:
    expected: dict[tuple[str, str], set[str]] = {}
    for lock_path in lock_files:
        for key, hashes in _compiled_lock_entries(lock_path).items():
            for existing_name, existing_version in expected:
                if existing_name == key[0] and existing_version != key[1]:
                    raise ValueError("wheelhouse lock closure contains conflicting versions")
            expected.setdefault(key, set()).update(hashes)

    actual: dict[tuple[str, str], str] = {}
    wheels = sorted(wheelhouse.glob("*.whl"))
    if any(path.is_symlink() for path in wheels):
        raise ValueError("wheelhouse lock closure contains an unsafe wheel")
    for wheel in wheels:
        identity = read_wheel_package_identity(wheel)
        key = (_normalized_project_name(identity["name"]), identity["version"])
        if key in actual:
            raise ValueError("wheelhouse lock closure contains duplicate artifacts")
        actual[key] = sha256_file(wheel)

    if set(actual) != set(expected):
        raise ValueError("wheelhouse lock closure does not match the compiled locks")
    for key, digest in actual.items():
        if digest not in expected[key]:
            raise ValueError("wheelhouse artifact hash is not authorized by the compiled lock")
    return {"lock_package_count": len(expected), "wheel_count": len(actual)}


def _clean_build_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        upper = key.upper()
        if upper in {"VIRTUAL_ENV", "PLAYWRIGHT_BROWSERS_PATH"} or upper.startswith(
            ("PIP_", "PYTHON")
        ):
            environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


def _run(command: Sequence[str], *, cwd: Path, environment: Mapping[str, str] | None = None) -> str:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        env=dict(environment) if environment is not None else _clean_build_environment(),
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=7200,
    )
    if completed.returncode != 0:
        raise RuntimeError("offline dependency collection command failed")
    return completed.stdout


def _normalized_license(
    metadata: email.message.Message,
    *,
    license_override: str | None = None,
) -> str:
    if license_override is not None:
        normalized_override = " ".join(license_override.split())
        if not normalized_override:
            raise ValueError("wheel license override is empty")
        return normalized_override[:1024]

    expression = " ".join(str(metadata.get("License-Expression", "")).split())
    if expression and expression.casefold() not in {"unknown", "n/a", "none"}:
        return expression[:1024]

    legacy_license = str(metadata.get("License", ""))
    normalized_legacy = " ".join(legacy_license.split())
    legacy_is_concise = (
        normalized_legacy
        and len(normalized_legacy) <= 160
        and not normalized_legacy.casefold().startswith(
            (
                "copyright",
                "permission is hereby",
                "redistribution and use",
                "this software",
            )
        )
    )
    if legacy_is_concise and normalized_legacy.casefold() not in {"unknown", "n/a", "none"}:
        return normalized_legacy

    classifiers = [
        " ".join(classifier.removeprefix("License :: ").split())
        for classifier in metadata.get_all("Classifier", [])
        if classifier.startswith("License :: ")
    ]
    classifiers = [value for value in classifiers if value]
    if classifiers:
        return " AND ".join(dict.fromkeys(classifiers))[:1024]

    if normalized_legacy and normalized_legacy.casefold() not in {"unknown", "n/a", "none"}:
        return normalized_legacy[:1024]
    raise ValueError("wheel license metadata is missing")


def _read_wheel_metadata(wheel_path: str | Path) -> email.message.Message:
    path = Path(wheel_path)
    try:
        with zipfile.ZipFile(path) as archive:
            candidates = sorted(
                (
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and len(PurePosixPath(info.filename).parts) == 2
                    and PurePosixPath(info.filename).parts[0].endswith(".dist-info")
                    and PurePosixPath(info.filename).parts[1] == "METADATA"
                ),
                key=lambda info: info.filename,
            )
            if len(candidates) != 1:
                raise ValueError("wheel must contain exactly one METADATA record")
            if candidates[0].file_size > 4 * 1024 * 1024:
                raise ValueError("wheel METADATA is oversized")
            raw = archive.read(candidates[0])
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("wheel is unreadable") from exc
    return email.parser.BytesParser().parsebytes(raw)


def _wheel_package_identity(metadata: email.message.Message) -> dict[str, str]:
    name = " ".join(str(metadata.get("Name", "")).split())
    version = " ".join(str(metadata.get("Version", "")).split())
    if not name or not version:
        raise ValueError("wheel identity metadata is missing")
    return {"name": name, "version": version}


def read_wheel_package_identity(wheel_path: str | Path) -> dict[str, str]:
    return _wheel_package_identity(_read_wheel_metadata(wheel_path))


def read_wheel_identity(
    wheel_path: str | Path,
    *,
    license_override: str | None = None,
    license_overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    metadata = _read_wheel_metadata(wheel_path)
    identity = _wheel_package_identity(metadata)
    name = identity["name"]
    version = identity["version"]
    if license_override is None and license_overrides is not None:
        override_key = f"{_normalized_project_name(name)}=={version}"
        license_override = license_overrides.get(override_key)
    return {
        "name": name,
        "version": version,
        "license": _normalized_license(metadata, license_override=license_override),
    }


def validate_staged_text_privacy(staging_root: Path) -> None:
    findings = []
    for path in sorted(staging_root.rglob("*")):
        if not path.is_file():
            continue
        decoded = release_policy._decode_scannable_text(path.read_bytes())
        if decoded is None:
            continue
        relative = path.relative_to(staging_root).as_posix()
        findings.extend(release_policy.scan_text(relative, decoded))
    if findings:
        codes = ", ".join(sorted({finding.code for finding in findings}))
        raise ValueError(f"offline dependency staging privacy validation failed: {codes}")


def _zip_entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    return info.create_system == 3 and stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def _safe_zip_name(raw: str) -> PurePosixPath:
    try:
        return PurePosixPath(normalize_archive_path(raw))
    except ValueError as exc:
        raise ValueError("unsafe source archive path") from exc


def normalize_built_wheel(wheel_path: str | Path) -> str:
    path = Path(wheel_path)
    entries: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    seen_casefold: set[str] = set()
    try:
        with zipfile.ZipFile(path) as source:
            for info in source.infolist():
                if info.is_dir():
                    continue
                name = _safe_zip_name(info.filename).as_posix()
                if _zip_entry_is_symlink(info):
                    raise ValueError("built wheel contains an unsafe symlink")
                if name in seen or name.casefold() in seen_casefold:
                    raise ValueError("built wheel contains duplicate paths")
                seen.add(name)
                seen_casefold.add(name.casefold())
                entries.append((name, source.read(info)))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("built wheel is unreadable") from exc
    temporary = path.with_name(f".{path.name}.normalized")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in sorted(entries):
                info = zipfile.ZipInfo(name, _NORMALIZED_WHEEL_TIME)
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    read_wheel_identity(path)
    return sha256_file(path)


def stage_ffmpeg_archive(
    archive_path: str | Path,
    destination: str | Path,
    source: Mapping[str, object],
) -> dict[str, object]:
    archive_file = Path(archive_path)
    target = Path(destination)
    expected_hash = source.get("archive_sha256")
    if not isinstance(expected_hash, str) or _SHA256.fullmatch(expected_hash) is None:
        raise ValueError("FFmpeg source requires a valid archive SHA-256")
    if sha256_file(archive_file) != expected_hash:
        raise ValueError("FFmpeg source archive SHA-256 mismatch")
    root_name = source.get("root_directory")
    for key in ("version", "license", "source"):
        if not isinstance(source.get(key), str) or not str(source[key]).strip():
            raise ValueError("FFmpeg source metadata is incomplete")
    if not isinstance(root_name, str) or not root_name:
        raise ValueError("FFmpeg source root is invalid")
    if target.exists():
        raise FileExistsError("FFmpeg staging destination already exists")
    target.mkdir(parents=True)
    copied: set[str] = set()
    try:
        with zipfile.ZipFile(archive_file) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                path = _safe_zip_name(info.filename)
                if _zip_entry_is_symlink(info) or not path.parts or path.parts[0] != root_name:
                    raise ValueError("unsafe FFmpeg source archive")
                relative = PurePosixPath(*path.parts[1:])
                keep = (len(relative.parts) == 1 and relative.name == "LICENSE.txt") or (
                    len(relative.parts) == 2
                    and relative.parts[0] == "bin"
                    and (
                        relative.suffix.casefold() == ".dll"
                        or relative.name.casefold() in _FFMPEG_PROGRAMS
                    )
                )
                if not keep:
                    continue
                normalized = relative.as_posix()
                if normalized.casefold() in {value.casefold() for value in copied}:
                    raise ValueError("FFmpeg source archive contains duplicate runtime files")
                output = target / Path(*relative.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source_stream, output.open("xb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                copied.add(normalized)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    required = {f"bin/{name}" for name in _FFMPEG_PROGRAMS} | {"LICENSE.txt"}
    if not required.issubset(copied):
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError("FFmpeg source archive is missing required runtime or license files")
    result = {
        "included": True,
        "version": source["version"],
        "license": source["license"],
        "source": source["source"],
        "archive_sha256": expected_hash,
        "ffmpeg_relative_path": "tools/ffmpeg/bin/ffmpeg.exe",
        "ffprobe_relative_path": "tools/ffmpeg/bin/ffprobe.exe",
        "runtime_file_count": len(copied),
    }
    return result


def _download(url: str, destination: Path, *, decode_base64: bool = False) -> None:
    headers = {"User-Agent": "Auto-Cut-offline-builder/1"}
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme == "https"
        and parsed.netloc == "api.github.com"
        and "releases/assets/" in parsed.path
    ):
        headers.update(
            {
                "Accept": "application/octet-stream",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if decode_base64:
        try:
            data = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise ValueError("downloaded license encoding is invalid") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def _verify_fixed_file(path: Path, *, size: object, sha256: object, label: str) -> None:
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError(f"{label} size is invalid")
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise ValueError(f"{label} SHA-256 is invalid")
    if path.stat().st_size != size or sha256_file(path) != sha256:
        raise ValueError(f"{label} identity mismatch")


def _validate_ffmpeg_source_declaration(source: Mapping[str, object]) -> None:
    # The maintained release uses bytes committed under the repository's
    # excluded companion-asset prefix.  It must never silently fall back to a
    # network URL.  Keep the legacy fixed-release validation below for callers
    # that still exercise the old declaration shape in isolated tests.
    if source.get("source_kind") == "committed_repository_asset":
        required_text = (
            "release_tag",
            "source",
            "source_filename",
            "runtime_archive_path",
            "source_archive_path",
            "build_recipe_path",
            "build_receipt_path",
            "pe_imports_path",
            "media_probe_path",
            "asset_manifest_path",
        )
        if any(
            not isinstance(source.get(key), str) or not str(source[key]).strip()
            for key in required_text
        ):
            raise ValueError("committed FFmpeg source declaration is incomplete")
        if not str(source["source"]).startswith("repository:"):
            raise ValueError("committed FFmpeg source must use a repository identity")
        for key in (
            "archive_size",
            "source_archive_size",
            "build_recipe_size",
            "build_receipt_size",
            "asset_manifest_size",
        ):
            value = source.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"committed FFmpeg {key} is invalid")
        for key in (
            "archive_sha256",
            "source_archive_sha256",
            "build_recipe_sha256",
            "build_receipt_sha256",
            "pe_imports_sha256",
            "media_probe_sha256",
            "asset_manifest_sha256",
        ):
            value = source.get(key)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"committed FFmpeg {key} is invalid")
        runtime_files = source.get("runtime_files")
        if not isinstance(runtime_files, Mapping):
            raise ValueError("committed FFmpeg runtime file inventory is invalid")
        for required_name in ("ffmpeg.exe", "ffprobe.exe", "LICENSE.txt"):
            row = runtime_files.get(required_name)
            if not isinstance(row, Mapping):
                raise ValueError("committed FFmpeg runtime file inventory is incomplete")
            path_value = row.get("path")
            if not isinstance(path_value, str):
                raise ValueError("committed FFmpeg runtime file path is invalid")
            try:
                normalized_runtime_path = normalize_archive_path(path_value)
            except ValueError as exc:
                raise ValueError("committed FFmpeg runtime file path is unsafe") from exc
            expected_runtime_path = (
                f"bin/{required_name}" if required_name != "LICENSE.txt" else "LICENSE.txt"
            )
            if normalized_runtime_path != expected_runtime_path:
                raise ValueError("committed FFmpeg runtime file path is unexpected")
            if (
                not isinstance(row.get("size"), int)
                or isinstance(row["size"], bool)
                or row["size"] <= 0
                or not isinstance(row.get("sha256"), str)
                or _SHA256.fullmatch(row["sha256"]) is None
            ):
                raise ValueError("committed FFmpeg runtime file identity is invalid")
        for key in ("build_source_commit", "ffmpeg_source_commit"):
            value = source.get(key)
            if not isinstance(value, str) or _IMMUTABLE_IDENTITY.fullmatch(value) is None:
                raise ValueError("committed FFmpeg immutable identity is invalid")
        for key in (
            "runtime_archive_path",
            "source_archive_path",
            "build_recipe_path",
            "build_receipt_path",
            "pe_imports_path",
            "media_probe_path",
        ):
            value = str(source[key])
            try:
                normalized = normalize_archive_path(value)
            except ValueError as exc:
                raise ValueError("committed FFmpeg asset path is unsafe") from exc
            if not normalized.startswith("scripts/release/ffmpeg_assets/"):
                raise ValueError("committed FFmpeg asset path is outside the asset root")
        if source.get("asset_manifest_path") is not None:
            try:
                manifest_path = normalize_archive_path(str(source["asset_manifest_path"]))
            except ValueError as exc:
                raise ValueError("committed FFmpeg asset manifest path is unsafe") from exc
            if not manifest_path.startswith("scripts/release/ffmpeg_assets/"):
                raise ValueError("committed FFmpeg asset manifest is outside the asset root")
        licenses = source.get("license_sources")
        if not isinstance(licenses, list) or not licenses:
            raise ValueError("committed FFmpeg license closure is empty")
        seen_license_names: set[str] = set()
        seen_license_paths: set[str] = set()
        for row in licenses:
            if not isinstance(row, Mapping):
                raise ValueError("committed FFmpeg license metadata is invalid")
            for key in ("filename", "path", "url", "license"):
                if not isinstance(row.get(key), str) or not str(row[key]).strip():
                    raise ValueError("committed FFmpeg license metadata is invalid")
            filename = str(row["filename"])
            try:
                normalized_filename = normalize_archive_path(filename)
                normalized_path = normalize_archive_path(str(row["path"]))
            except ValueError as exc:
                raise ValueError("committed FFmpeg license path is unsafe") from exc
            source_path = PurePosixPath(normalized_path)
            source_basename = source_path.name
            source_namespace = source_path.parent.name
            expected_filename = (
                source_basename
                if source_namespace in {"ffmpeg", "mingw"}
                else f"gcc-{source_basename}"
                if source_namespace == "gcc"
                else ""
            )
            if (
                normalized_filename != filename
                or PurePosixPath(filename).name != filename
                or not normalized_path.startswith("scripts/release/ffmpeg_assets/licenses/")
                or filename != expected_filename
                or str(row["url"]) != f"repository:{normalized_path}"
                or filename.casefold() in seen_license_names
                or normalized_path.casefold() in seen_license_paths
            ):
                raise ValueError("committed FFmpeg license path is invalid")
            seen_license_names.add(filename.casefold())
            seen_license_paths.add(normalized_path.casefold())
            if (
                not isinstance(row.get("size"), int)
                or isinstance(row["size"], bool)
                or row["size"] <= 0
                or not isinstance(row.get("sha256"), str)
                or _SHA256.fullmatch(row["sha256"]) is None
            ):
                raise ValueError("committed FFmpeg license metadata is invalid")
            if row.get("encoding") is not None:
                if row.get("encoding") != "base64":
                    raise ValueError("committed FFmpeg license encoding is invalid")
                if (
                    not isinstance(row.get("content_size"), int)
                    or isinstance(row["content_size"], bool)
                    or row["content_size"] <= 0
                    or not isinstance(row.get("content_sha256"), str)
                    or _SHA256.fullmatch(row["content_sha256"]) is None
                ):
                    raise ValueError("committed FFmpeg encoded license identity is invalid")
        return

    required_text = (
        "release_tag",
        "source",
        "source_filename",
        "build_source_commit",
        "ffmpeg_source_commit",
    )
    if any(
        not isinstance(source.get(key), str) or not str(source[key]).strip()
        for key in required_text
    ):
        raise ValueError("FFmpeg source declaration is incomplete")
    asset_id = source.get("github_asset_id")
    archive_size = source.get("archive_size")
    archive_sha256 = source.get("archive_sha256")
    if not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id <= 0:
        raise ValueError("FFmpeg source asset identity is invalid")
    if not isinstance(archive_size, int) or isinstance(archive_size, bool) or archive_size <= 0:
        raise ValueError("FFmpeg source archive size is invalid")
    if not isinstance(archive_sha256, str) or _SHA256.fullmatch(archive_sha256) is None:
        raise ValueError("FFmpeg source archive SHA-256 is invalid")
    for key in ("build_source_commit", "ffmpeg_source_commit"):
        if _GIT_COMMIT.fullmatch(str(source[key])) is None:
            raise ValueError("FFmpeg source commit identity is invalid")

    release_tag = str(source["release_tag"])
    filename = str(source["source_filename"])
    parsed = urllib.parse.urlsplit(str(source["source"]))
    path_parts = tuple(part for part in parsed.path.split("/") if part)
    expected_tail = ("releases", "download", release_tag, filename)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or release_tag.casefold() == "latest"
        or "latest" in filename.casefold()
        or path_parts[-4:] != expected_tail
    ):
        raise ValueError("FFmpeg source must use a fixed release asset")


def _extract_browser_archive(archive_path: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError("browser staging destination already exists")
    destination.mkdir(parents=True)
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                path = _safe_zip_name(info.filename.rstrip("/") if info.is_dir() else info.filename)
                if _zip_entry_is_symlink(info):
                    raise ValueError("unsafe browser source archive")
                normalized = path.as_posix().casefold()
                if normalized in seen:
                    raise ValueError("browser source archive contains duplicate paths")
                seen.add(normalized)
                output = destination / Path(*path.parts)
                if info.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source_stream, output.open("xb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _tree_receipt(root: Path) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for candidate in sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    ):
        if candidate.is_symlink():
            raise ValueError("browser tree contains an unsafe symlink")
        data = candidate.read_bytes()
        rows.append(
            {
                "path": candidate.relative_to(root).as_posix(),
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


def _verify_tree_receipt(
    root: Path,
    source: Mapping[str, object],
    prefix: str,
) -> dict[str, object]:
    receipt = _tree_receipt(root)
    expected = {
        "file_count": source.get(f"{prefix}_tree_file_count"),
        "total_size": source.get(f"{prefix}_tree_total_size"),
        "tree_sha256": source.get(f"{prefix}_tree_sha256"),
    }
    if receipt != expected:
        raise ValueError(f"{prefix} browser tree identity mismatch")
    return receipt


def _read_committed_blob(repo_root: Path, source_commit: str, relative_path: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"{source_commit}:{relative_path}"],
        cwd=str(repo_root),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("committed release input is unavailable")
    return completed.stdout


def _materialize_committed_file(
    repo_root: Path,
    source_commit: str,
    relative_path: str,
    destination: Path,
    *,
    size: object,
    sha256: object,
    label: str,
) -> None:
    """Copy one release input from the committed tree and verify its identity.

    This deliberately has no URL/network fallback.  A missing or changed
    committed input is a hard release error, which keeps offline dependency
    builds reproducible and prevents a stale remote artifact from being used.
    """
    try:
        normalized = normalize_archive_path(relative_path)
    except ValueError as exc:
        raise ValueError(f"{label} path is unsafe") from exc
    if not normalized.startswith("scripts/release/ffmpeg_assets/"):
        raise ValueError(f"{label} is outside the committed FFmpeg asset root")
    data = _read_committed_blob(repo_root, source_commit, normalized)
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError(f"{label} size is invalid")
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise ValueError(f"{label} SHA-256 is invalid")
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"{label} committed identity mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def _stage_committed_ffmpeg_assets(
    repo_root: Path,
    staging_root: Path,
    source_commit: str,
    source: Mapping[str, object],
    *,
    temporary_root: Path,
) -> dict[str, object]:
    """Materialize and validate the hash-pinned local FFmpeg component."""
    _validate_ffmpeg_source_declaration(source)
    runtime_archive = temporary_root / str(source["source_filename"])
    _materialize_committed_file(
        repo_root,
        source_commit,
        str(source["runtime_archive_path"]),
        runtime_archive,
        size=source["archive_size"],
        sha256=source["archive_sha256"],
        label="FFmpeg runtime archive",
    )
    component = stage_ffmpeg_archive(
        runtime_archive,
        staging_root / "tools" / "ffmpeg",
        source,
    )
    runtime_root = staging_root / "tools" / "ffmpeg"
    runtime_files = source.get("runtime_files", {})
    if not isinstance(runtime_files, Mapping):
        raise ValueError("committed FFmpeg runtime file inventory is invalid")
    for name, row in runtime_files.items():
        if not isinstance(row, Mapping) or not isinstance(row.get("path"), str):
            raise ValueError("committed FFmpeg runtime file inventory is invalid")
        runtime_path = runtime_root / Path(*PurePosixPath(str(row["path"])).parts)
        _verify_fixed_file(
            runtime_path,
            size=row.get("size"),
            sha256=row.get("sha256"),
            label=f"FFmpeg runtime {name}",
        )

    # Keep the source and machine-neutral build evidence in the offline bundle
    # so a target can audit exactly which bytes produced the executables.
    source_name = PurePosixPath(str(source["source_archive_path"])).name
    _materialize_committed_file(
        repo_root,
        source_commit,
        str(source["source_archive_path"]),
        staging_root / "provenance" / "ffmpeg" / "source" / source_name,
        size=source["source_archive_size"],
        sha256=source["source_archive_sha256"],
        label="FFmpeg source archive",
    )
    evidence_specs = (
        ("build_recipe_path", "build_recipe_size", "build_recipe_sha256", "build_ffmpeg.sh"),
        ("build_receipt_path", "build_receipt_size", "build_receipt_sha256", "build-receipt.json"),
        ("pe_imports_path", "pe_imports_size", "pe_imports_sha256", "pe-imports.json"),
        ("media_probe_path", "media_probe_size", "media_probe_sha256", "media-probe.json"),
    )
    for path_key, size_key, hash_key, name in evidence_specs:
        # The two evidence sizes are intentionally read from the committed
        # bytes when not duplicated in the declaration, while hashes remain
        # mandatory and pinned.
        data = _read_committed_blob(repo_root, source_commit, str(source[path_key]))
        declared_size = source.get(size_key, len(data))
        _materialize_committed_file(
            repo_root,
            source_commit,
            str(source[path_key]),
            staging_root / "provenance" / "ffmpeg" / "build" / name,
            size=declared_size,
            sha256=source[hash_key],
            label=f"FFmpeg {name}",
        )

    manifest_path = source.get("asset_manifest_path")
    if isinstance(manifest_path, str):
        destination = staging_root / "provenance" / "ffmpeg" / "manifest.json"
        _materialize_committed_file(
            repo_root,
            source_commit,
            manifest_path,
            destination,
            size=source.get("asset_manifest_size"),
            sha256=source.get("asset_manifest_sha256"),
            label="FFmpeg asset manifest",
        )

    license_dir = staging_root / "tools" / "ffmpeg" / "licenses"
    encoded_license_rows: list[dict[str, object]] = []
    for row in source.get("license_sources", []):
        if not isinstance(row, Mapping):
            raise ValueError("committed FFmpeg license source metadata is invalid")
        filename = str(row["filename"])
        _materialize_committed_file(
            repo_root,
            source_commit,
            str(row["path"]),
            license_dir / filename,
            size=row["size"],
            sha256=row["sha256"],
            label=f"FFmpeg license {filename}",
        )
        if row.get("encoding") == "base64":
            encoded = (license_dir / filename).read_bytes()
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError(f"FFmpeg license {filename} base64 encoding is invalid") from exc
            if len(decoded) != row.get("content_size") or hashlib.sha256(
                decoded
            ).hexdigest() != row.get("content_sha256"):
                raise ValueError(f"FFmpeg license {filename} decoded identity mismatch")
            encoded_license_rows.append(
                {
                    "path": f"tools/ffmpeg/licenses/{filename}",
                    "encoding": "base64",
                    "encoded_size": row["size"],
                    "encoded_sha256": row["sha256"],
                    "content_size": row["content_size"],
                    "content_sha256": row["content_sha256"],
                    "source": str(row["url"]),
                }
            )
    if encoded_license_rows:
        encoding_receipt = staging_root / "provenance" / "ffmpeg" / "license-encoding.json"
        encoding_receipt.parent.mkdir(parents=True, exist_ok=True)
        encoding_receipt.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "ready",
                    "reason": "Upstream legal text is preserved byte-for-byte in a base64 envelope because a citation resembles a local path to the release privacy scanner.",
                    "rows": encoded_license_rows,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    component.update(
        {
            "source_kind": "committed_repository_asset",
            "version": source["version"],
            "release_tag": source["release_tag"],
            "archive_size": source["archive_size"],
            "archive_sha256": source["archive_sha256"],
            "runtime_archive_size": source["archive_size"],
            "runtime_archive_sha256": source["archive_sha256"],
            "runtime_files": dict(runtime_files),
            "source_archive_path": f"provenance/ffmpeg/source/{source_name}",
            "source_archive_size": source["source_archive_size"],
            "source_archive_sha256": source["source_archive_sha256"],
            "build_source_commit": source["build_source_commit"],
            "ffmpeg_source_commit": source["ffmpeg_source_commit"],
            "build_recipe_path": "provenance/ffmpeg/build/build_ffmpeg.sh",
            "build_recipe_size": source["build_recipe_size"],
            "build_recipe_sha256": source["build_recipe_sha256"],
            "build_receipt_path": "provenance/ffmpeg/build/build-receipt.json",
            "build_receipt_size": source["build_receipt_size"],
            "build_receipt_sha256": source["build_receipt_sha256"],
            "pe_imports_path": "provenance/ffmpeg/build/pe-imports.json",
            "pe_imports_sha256": source["pe_imports_sha256"],
            "media_probe_path": "provenance/ffmpeg/build/media-probe.json",
            "media_probe_sha256": source["media_probe_sha256"],
            "asset_manifest_path": "provenance/ffmpeg/manifest.json",
            "asset_manifest_size": source["asset_manifest_size"],
            "asset_manifest_sha256": source["asset_manifest_sha256"],
            "license_index_path": "provenance/ffmpeg/license-encoding.json",
            "external_codec_libraries": False,
            "reproducible_builds": 2,
            "reproducible_outputs_equal": True,
            "license_closure_complete": True,
            "license_sources": list(source.get("license_sources", [])),
        }
    )
    return component


def _copy_requirement_inputs(repo_root: Path, staging_root: Path, source_commit: str) -> None:
    destination = staging_root / "requirements"
    destination.mkdir(parents=True)
    for name in REQUIREMENT_FILES:
        (destination / name).write_bytes(_read_committed_blob(repo_root, source_commit, name))
    validate_direct_pin_parity(
        destination / "requirements.txt",
        destination / "requirements-offline-main.lock",
    )
    validate_direct_pin_parity(
        destination / "requirements-audio.lock",
        destination / "requirements-offline-audio.lock",
    )
    provenance = json.loads(
        _read_committed_blob(
            repo_root,
            source_commit,
            "scripts/release/offline_sources.json",
        ).decode("utf-8")
    )
    if not isinstance(provenance, dict):
        raise ValueError("committed offline source declaration is invalid")
    output = staging_root / "provenance" / "offline-sources.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _prepare_build_runtime(repo_root: Path, staging_root: Path) -> Path:
    build_root = staging_root.parent / "dependency-build-runtime"
    if build_root.exists():
        raise FileExistsError("offline dependency build runtime already exists")
    _run([sys.executable, "-I", "-m", "venv", str(build_root)], cwd=repo_root)
    build_python = build_root / "Scripts" / "python.exe"
    if not build_python.is_file():
        raise ValueError("offline dependency build runtime was not created")

    main_wheelhouse = staging_root / "wheelhouse" / "main"
    main_wheelhouse.mkdir(parents=True)
    bootstrap_lock = staging_root / "requirements" / "requirements-offline-bootstrap.lock"
    _run(
        pip_download_command(str(build_python), main_wheelhouse, [bootstrap_lock]),
        cwd=repo_root,
    )
    _run(
        [
            str(build_python),
            "-I",
            "-m",
            "pip",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "install",
            "--no-index",
            "--find-links",
            str(main_wheelhouse),
            "--only-binary=:all:",
            "--require-hashes",
            "--requirement",
            str(bootstrap_lock),
        ],
        cwd=repo_root,
    )
    pip_version = _run(
        [str(build_python), "-I", "-m", "pip", "--version"],
        cwd=repo_root,
    )
    if re.match(rf"^pip\s+{re.escape(BUILD_PIP_VERSION)}(?:\s|$)", pip_version) is None:
        raise ValueError("offline dependency build runtime pip identity mismatch")
    return build_python


def _install_playwright_probe_runtime(
    repo_root: Path, staging_root: Path, build_python: Path
) -> None:
    main_wheelhouse = staging_root / "wheelhouse" / "main"
    main_lock = staging_root / "requirements" / "requirements-offline-main.lock"
    _run(
        [
            str(build_python),
            "-I",
            "-m",
            "pip",
            "install",
            "--isolated",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--find-links",
            str(main_wheelhouse),
            "--only-binary=:all:",
            "--require-hashes",
            "--requirement",
            str(main_lock),
        ],
        cwd=repo_root,
    )
    version_probe = "import importlib.metadata; " "print(importlib.metadata.version('playwright'))"
    measured = _run(
        [str(build_python), "-I", "-c", version_probe],
        cwd=repo_root,
    ).strip()
    if measured != PLAYWRIGHT_VERSION:
        raise ValueError("isolated Playwright probe runtime identity mismatch")


def _build_intervaltree_wheel(
    repo_root: Path,
    staging_root: Path,
    source: Mapping[str, object],
    *,
    build_python: Path,
) -> dict[str, object]:
    for key in ("version", "filename", "source", "license"):
        if not isinstance(source.get(key), str) or not str(source[key]).strip():
            raise ValueError("intervaltree source metadata is incomplete")
    expected_hash = source.get("archive_sha256")
    expected_size = source.get("archive_size")
    if not isinstance(expected_hash, str) or _SHA256.fullmatch(expected_hash) is None:
        raise ValueError("intervaltree source SHA-256 is invalid")
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size <= 0:
        raise ValueError("intervaltree source size is invalid")

    source_path = staging_root / "provenance" / "sources" / str(source["filename"])
    _download(str(source["source"]), source_path)
    if source_path.stat().st_size != expected_size or sha256_file(source_path) != expected_hash:
        raise ValueError("intervaltree source archive identity mismatch")

    main_wheelhouse = staging_root / "wheelhouse" / "main"
    audio_wheelhouse = staging_root / "wheelhouse" / "audio"
    _run(
        [
            str(build_python),
            "-I",
            "-m",
            "pip",
            "install",
            "--isolated",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--find-links",
            str(main_wheelhouse),
            "--find-links",
            str(audio_wheelhouse),
            "--only-binary=:all:",
            "--require-hashes",
            "--requirement",
            str(staging_root / "requirements" / "requirements-audio-build.lock"),
        ],
        cwd=repo_root,
    )
    before = {path.name for path in audio_wheelhouse.glob("intervaltree-*.whl")}
    _run(
        [
            str(build_python),
            "-I",
            "-m",
            "pip",
            "wheel",
            "--isolated",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(audio_wheelhouse),
            str(source_path),
        ],
        cwd=repo_root,
    )
    candidates = sorted(
        path for path in audio_wheelhouse.glob("intervaltree-*.whl") if path.name not in before
    )
    if len(candidates) != 1:
        raise ValueError("intervaltree wheel build did not produce one wheel")
    normalized_wheel_sha256 = normalize_built_wheel(candidates[0])
    identity = read_wheel_identity(candidates[0])
    if identity["name"].casefold() != "intervaltree" or identity["version"] != source["version"]:
        raise ValueError("intervaltree wheel identity mismatch")
    receipt = staging_root / "receipts" / "intervaltree-wheel-build.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "requirement": _INTERVALTREE_REQUIREMENT,
                "source_filename": source_path.name,
                "source_size": expected_size,
                "source_sha256": expected_hash,
                "wheel_filename": candidates[0].name,
                "wheel_sha256": normalized_wheel_sha256,
                "build_tools": {
                    "pip": BUILD_PIP_VERSION,
                    "setuptools": "83.0.0",
                    "packaging": "23.2",
                    "wheel": "0.45.1",
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "included": True,
        "requirement": _INTERVALTREE_REQUIREMENT,
        "source_filename": source_path.name,
        "source_size": expected_size,
        "source_sha256": expected_hash,
        "wheel_filename": candidates[0].name,
        "wheel_sha256": normalized_wheel_sha256,
        "license": source["license"],
        "source": source["source"],
    }


def _download_wheelhouses(
    repo_root: Path,
    staging_root: Path,
    source_config: Mapping[str, object],
    *,
    build_python: Path,
) -> dict[str, object]:
    main = staging_root / "wheelhouse" / "main"
    audio = staging_root / "wheelhouse" / "audio"
    main.mkdir(parents=True, exist_ok=True)
    audio.mkdir(parents=True)
    requirements = staging_root / "requirements"
    _run(
        pip_download_command(
            str(build_python),
            main,
            [
                requirements / "requirements-offline-main.lock",
                requirements / "requirements-offline-acceptance.lock",
            ],
        ),
        cwd=repo_root,
    )
    _run(
        pip_download_command(
            str(build_python),
            audio,
            [requirements / "requirements-audio-build.lock"],
        ),
        cwd=repo_root,
    )
    intervaltree = _build_intervaltree_wheel(
        repo_root,
        staging_root,
        source_config["intervaltree"],
        build_python=build_python,
    )
    _run(
        pip_download_command(
            str(build_python),
            audio,
            [requirements / "requirements-offline-audio.lock"],
            find_links=[audio],
        ),
        cwd=repo_root,
    )
    validate_wheelhouse_lock_closure(
        main,
        [
            requirements / "requirements-offline-bootstrap.lock",
            requirements / "requirements-offline-main.lock",
            requirements / "requirements-offline-acceptance.lock",
        ],
    )
    validate_wheelhouse_lock_closure(
        audio,
        [
            requirements / "requirements-audio-build.lock",
            requirements / "requirements-offline-audio.lock",
        ],
    )
    return intervaltree


def _copy_playwright_license(main_wheelhouse: Path, destination: Path) -> None:
    candidates = sorted(main_wheelhouse.glob("playwright-1.52.0-*.whl"))
    if len(candidates) != 1:
        raise ValueError("Playwright 1.52.0 wheel is missing or ambiguous")
    with zipfile.ZipFile(candidates[0]) as archive:
        licenses = sorted(
            info
            for info in archive.infolist()
            if info.filename.casefold().endswith(".dist-info/licenses/license")
            and not info.is_dir()
        )
        if len(licenses) != 1:
            raise ValueError("Playwright wheel license is missing or ambiguous")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(licenses[0]))


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _has_reparse_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(str(path)))
    for candidate in (*reversed(absolute.parents), absolute):
        if _is_reparse_point(candidate):
            return True
    return False


def _playwright_runtime_marker_paths(browser_root: Path) -> tuple[Path, ...]:
    if _has_reparse_component(browser_root) or (
        browser_root.exists() and not browser_root.is_dir()
    ):
        raise ValueError("Playwright browser root is unsafe")
    component_roots = (
        browser_root / f"chromium-{CHROMIUM_REVISION}",
        browser_root / f"chromium_headless_shell-{CHROMIUM_REVISION}",
        browser_root / f"ffmpeg-{PLAYWRIGHT_FFMPEG_REVISION}",
        browser_root / f"winldd-{PLAYWRIGHT_WINLDD_REVISION}",
    )
    for component_root in component_roots:
        if _has_reparse_component(component_root) or (
            component_root.exists() and not component_root.is_dir()
        ):
            raise ValueError("Playwright browser component root is unsafe")
    return tuple(root / _PLAYWRIGHT_RUNTIME_MARKER for root in component_roots)


def _assert_no_playwright_runtime_markers(browser_root: Path) -> None:
    for marker in _playwright_runtime_marker_paths(browser_root):
        if _is_reparse_point(marker) or marker.exists():
            raise ValueError("Playwright runtime marker is present before probe")


def _clear_playwright_runtime_markers(browser_root: Path) -> None:
    """Remove the deterministic marker Playwright writes during launch probes.

    The marker is a probe side effect, not a redistributable browser payload.  Only
    the four fixed component roots are touched; unexpected entries or symlinks
    fail closed so this cleanup cannot hide a changed browser tree.
    """

    for marker in _playwright_runtime_marker_paths(browser_root):
        if _is_reparse_point(marker):
            raise ValueError("Playwright runtime marker is an unsafe reparse point")
        if marker.exists():
            if not marker.is_file() or marker.stat().st_size != 0:
                raise ValueError("Playwright runtime marker is not a regular file")
            marker.unlink()


def _stage_chromium(
    repo_root: Path,
    staging_root: Path,
    source: Mapping[str, object],
    *,
    python_executable: Path,
) -> dict[str, object]:
    expected = {
        "playwright_version": PLAYWRIGHT_VERSION,
        "revision": CHROMIUM_REVISION,
        "browser_version": CHROMIUM_VERSION,
        "ffmpeg_revision": PLAYWRIGHT_FFMPEG_REVISION,
        "winldd_revision": PLAYWRIGHT_WINLDD_REVISION,
    }
    if any(source.get(key) != value for key, value in expected.items()):
        raise ValueError("Playwright Chromium source declaration mismatch")
    if type(source.get("native_binding_schema")) is not int or source.get("native_binding_schema") != 1:
        raise ValueError("Playwright Chromium native binding schema must be 1")
    formal_tree_fields = (
        "chromium_executable_size",
        "chromium_executable_sha256",
        "headless_shell_executable_size",
        "headless_shell_executable_sha256",
        "chromium_tree_file_count",
        "chromium_tree_total_size",
        "chromium_tree_sha256",
        "headless_shell_tree_file_count",
        "headless_shell_tree_total_size",
        "headless_shell_tree_sha256",
        "ffmpeg_tree_file_count",
        "ffmpeg_tree_total_size",
        "ffmpeg_tree_sha256",
        "winldd_tree_file_count",
        "winldd_tree_total_size",
        "winldd_tree_sha256",
    )
    if any(key not in source for key in formal_tree_fields):
        raise ValueError("formal Chromium tree binding is incomplete")
    archives = (
        (
            "chromium",
            "chromium_source",
            "chromium_archive_size",
            "chromium_archive_sha256",
            staging_root / "browsers" / f"chromium-{CHROMIUM_REVISION}",
        ),
        (
            "Chromium headless shell",
            "headless_shell_source",
            "headless_shell_archive_size",
            "headless_shell_archive_sha256",
            staging_root / "browsers" / f"chromium_headless_shell-{CHROMIUM_REVISION}",
        ),
        (
            "Playwright recording FFmpeg",
            "ffmpeg_source",
            "ffmpeg_archive_size",
            "ffmpeg_archive_sha256",
            staging_root / "browsers" / f"ffmpeg-{PLAYWRIGHT_FFMPEG_REVISION}",
        ),
        (
            "Playwright Windows dependency probe",
            "winldd_source",
            "winldd_archive_size",
            "winldd_archive_sha256",
            staging_root / "browsers" / f"winldd-{PLAYWRIGHT_WINLDD_REVISION}",
        ),
    )
    archive_root = staging_root.parent / "browser-source-archives"
    archive_root.mkdir()
    for index, (label, url_key, size_key, sha_key, destination) in enumerate(archives, start=1):
        url = source.get(url_key)
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"{label} source URL is invalid")
        archive_path = archive_root / f"browser-{index}.zip"
        _download(url, archive_path)
        _verify_fixed_file(
            archive_path,
            size=source.get(size_key),
            sha256=source.get(sha_key),
            label=f"{label} source archive",
        )
        _extract_browser_archive(archive_path, destination)

    browser_root = staging_root / "browsers"
    environment = _clean_build_environment()
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
    chromium = browser_root / f"chromium-{CHROMIUM_REVISION}" / "chrome-win" / "chrome.exe"
    shell = (
        browser_root
        / f"chromium_headless_shell-{CHROMIUM_REVISION}"
        / "chrome-win"
        / "headless_shell.exe"
    )
    recording_ffmpeg_root = browser_root / f"ffmpeg-{PLAYWRIGHT_FFMPEG_REVISION}"
    recording_ffmpeg = recording_ffmpeg_root / "ffmpeg-win64.exe"
    license_filename = source.get("ffmpeg_license_filename")
    if (
        not isinstance(license_filename, str)
        or PurePosixPath(license_filename).name != license_filename
    ):
        raise ValueError("Playwright recording FFmpeg license filename is invalid")
    recording_license = recording_ffmpeg_root / license_filename
    winldd = browser_root / f"winldd-{PLAYWRIGHT_WINLDD_REVISION}" / "PrintDeps.exe"
    if (
        not chromium.is_file()
        or not shell.is_file()
        or not recording_ffmpeg.is_file()
        or not winldd.is_file()
    ):
        raise ValueError("Playwright Chromium installation is incomplete")
    _verify_fixed_file(
        chromium,
        size=source.get("chromium_executable_size"),
        sha256=source.get("chromium_executable_sha256"),
        label="Chromium executable",
    )
    _verify_fixed_file(
        shell,
        size=source.get("headless_shell_executable_size"),
        sha256=source.get("headless_shell_executable_sha256"),
        label="Chromium headless shell executable",
    )
    _verify_fixed_file(
        recording_ffmpeg,
        size=source.get("ffmpeg_executable_size"),
        sha256=source.get("ffmpeg_executable_sha256"),
        label="Playwright recording FFmpeg executable",
    )
    _verify_fixed_file(
        recording_license,
        size=source.get("ffmpeg_license_size"),
        sha256=source.get("ffmpeg_license_sha256"),
        label="Playwright recording FFmpeg license",
    )
    _verify_fixed_file(
        winldd,
        size=source.get("winldd_executable_size"),
        sha256=source.get("winldd_executable_sha256"),
        label="Playwright Windows dependency probe executable",
    )
    winldd_source_url = source.get("winldd_source_code_url")
    if not isinstance(winldd_source_url, str) or not winldd_source_url.strip():
        raise ValueError("Playwright Windows dependency probe source URL is invalid")
    winldd_source = staging_root / "licenses" / "playwright-winldd" / "PrintDeps.cpp"
    _download(winldd_source_url, winldd_source)
    _verify_fixed_file(
        winldd_source,
        size=source.get("winldd_source_code_size"),
        sha256=source.get("winldd_source_code_sha256"),
        label="Playwright Windows dependency probe source",
    )
    _assert_no_playwright_runtime_markers(browser_root)
    probe = "\n".join(
        (
            "import json",
            "import sys",
            "from pathlib import Path",
            "from playwright.sync_api import sync_playwright",
            "recording_root = Path(sys.argv[1])",
            "recording_root.mkdir(parents=True, exist_ok=True)",
            "with sync_playwright() as runtime:",
            "    browser = runtime.chromium.launch(headless=True)",
            "    context = browser.new_context(",
            "        record_video_dir=str(recording_root),",
            '        record_video_size={"width": 320, "height": 240},',
            "    )",
            "    page = context.new_page()",
            '    page.set_content("<html><body>offline recording probe</body></html>")',
            "    page.wait_for_timeout(250)",
            "    video = page.video",
            "    context.close()",
            "    video_path = Path(video.path())",
            "    browser_version = browser.version",
            "    browser.close()",
            "record_video_size = video_path.stat().st_size if video_path.is_file() else 0",
            'print(json.dumps({"ok": True, "browser": "chromium", "browser_version": browser_version, "record_video_verified": record_video_size > 0, "record_video_size": record_video_size}))',
        )
    )
    with tempfile.TemporaryDirectory(
        prefix="browser-recording-probe-", dir=staging_root.parent
    ) as recording_directory:
        output = _run(
            [str(python_executable), "-I", "-c", probe, recording_directory],
            cwd=repo_root,
            environment=environment,
        )
    try:
        launch = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError("Chromium launch receipt is invalid") from exc
    record_video_size = launch.get("record_video_size")
    if (
        launch.get("ok") is not True
        or launch.get("browser") != "chromium"
        or launch.get("browser_version") != source["browser_version"]
        or launch.get("record_video_verified") is not True
        or not isinstance(record_video_size, int)
        or isinstance(record_video_size, bool)
        or record_video_size <= 0
    ):
        raise ValueError("Chromium launch verification failed")
    _clear_playwright_runtime_markers(browser_root)
    tree_receipts = {
        "chromium": _verify_tree_receipt(
            browser_root / f"chromium-{CHROMIUM_REVISION}", source, "chromium"
        ),
        "headless_shell": _verify_tree_receipt(
            browser_root / f"chromium_headless_shell-{CHROMIUM_REVISION}",
            source,
            "headless_shell",
        ),
        "recording_ffmpeg": _verify_tree_receipt(
            recording_ffmpeg_root, source, "ffmpeg"
        ),
        "winldd": _verify_tree_receipt(
            browser_root / f"winldd-{PLAYWRIGHT_WINLDD_REVISION}", source, "winldd"
        ),
    }
    measured_version = str(launch["browser_version"])
    receipt = staging_root / "receipts" / "chromium-launch.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "playwright_version": PLAYWRIGHT_VERSION,
                "revision": CHROMIUM_REVISION,
                "ffmpeg_revision": PLAYWRIGHT_FFMPEG_REVISION,
                "winldd_revision": PLAYWRIGHT_WINLDD_REVISION,
                "browser_version": measured_version,
                "launch_verified": True,
                "record_video_verified": True,
                "winldd_source_verified": True,
                "source_archives_verified": True,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    result = {
        "included": True,
        "playwright_version": PLAYWRIGHT_VERSION,
        "revision": CHROMIUM_REVISION,
        "ffmpeg_revision": PLAYWRIGHT_FFMPEG_REVISION,
        "winldd_revision": PLAYWRIGHT_WINLDD_REVISION,
        "browser_version": measured_version,
        "browser_root": "browsers",
        "recording_ffmpeg_relative_path": PurePosixPath(
            "browsers",
            f"ffmpeg-{PLAYWRIGHT_FFMPEG_REVISION}",
            "ffmpeg-win64.exe",
        ).as_posix(),
        "launch_verified": True,
        "record_video_verified": True,
        "winldd_source_verified": True,
        "source_archives_verified": True,
        "tree_receipts": tree_receipts,
    }
    result.update(
        {
            "chromium_executable_size": int(source["chromium_executable_size"]),
            "chromium_executable_sha256": str(source["chromium_executable_sha256"]),
            "headless_shell_executable_size": int(source["headless_shell_executable_size"]),
            "headless_shell_executable_sha256": str(
                source["headless_shell_executable_sha256"]
            ),
        }
    )
    return result


def _validate_ffmpeg(staging_root: Path, component: Mapping[str, object]) -> dict[str, object]:
    bin_root = staging_root / "tools" / "ffmpeg" / "bin"
    identities: dict[str, str] = {}
    for name in ("ffmpeg", "ffprobe"):
        executable = bin_root / f"{name}.exe"
        completed = subprocess.run(
            [str(executable), "-version"],
            cwd=str(bin_root),
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        first_line = completed.stdout.splitlines()[0] if completed.stdout.splitlines() else ""
        identity_match = re.match(
            rf"^{re.escape(name)}\s+version\s+(\S+)(?:\s|$)",
            first_line,
        )
        measured_version = identity_match.group(1) if identity_match else ""
        if completed.returncode != 0 or measured_version != str(component["version"]):
            raise ValueError("bundled FFmpeg tool identity verification failed")
        identities[name] = measured_version
    receipt = staging_root / "receipts" / "ffmpeg-tools.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "version": component["version"],
                "ffmpeg_verified": True,
                "ffprobe_verified": True,
                "identities": identities,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"ffmpeg_verified": True, "ffprobe_verified": True}


def _wheel_metadata_rows(
    staging_root: Path,
    source_config: Mapping[str, object],
) -> tuple[dict[str, dict[str, str]], dict[str, object]]:
    metadata: dict[str, dict[str, str]] = {}
    components: dict[str, object] = {}
    raw_overrides = source_config.get("python_wheel_license_overrides", {})
    if not isinstance(raw_overrides, Mapping):
        raise ValueError("Python wheel license overrides must be an object")
    license_overrides: dict[str, str] = {}
    for raw_key, raw_value in raw_overrides.items():
        if not isinstance(raw_key, str) or raw_key.count("==") != 1:
            raise ValueError("Python wheel license override key is invalid")
        package_name, package_version = raw_key.split("==", 1)
        if (
            not package_name.strip()
            or not package_version.strip()
            or not isinstance(raw_value, str)
        ):
            raise ValueError("Python wheel license override is invalid")
        normalized_key = f"{_normalized_project_name(package_name)}=={package_version.strip()}"
        normalized_value = " ".join(raw_value.split())
        if normalized_key in license_overrides or not normalized_value:
            raise ValueError("Python wheel license override is invalid")
        license_overrides[normalized_key] = normalized_value
    for kind in ("main", "audio"):
        package_rows = []
        wheelhouse = staging_root / "wheelhouse" / kind
        for wheel in sorted(wheelhouse.glob("*.whl")):
            identity = read_wheel_identity(wheel, license_overrides=license_overrides)
            relative = wheel.relative_to(staging_root).as_posix()
            platform_value = "win_amd64" if "win_amd64" in wheel.name.casefold() else "any"
            intervaltree = source_config["intervaltree"]
            is_built_intervaltree = (
                identity["name"].casefold() == "intervaltree"
                and identity["version"] == intervaltree["version"]
            )
            metadata[relative] = {
                "component": f"{kind}_wheelhouse",
                "version": identity["version"],
                "platform": platform_value,
                "license": identity["license"],
                "source": (
                    str(intervaltree["source"])
                    if is_built_intervaltree
                    else _https_url(
                        "pypi.org",
                        f"project/{identity['name']}/{identity['version']}/",
                    )
                ),
            }
            package_rows.append(
                {
                    **identity,
                    "source_kind": "verified_sdist_build" if is_built_intervaltree else "wheel",
                }
            )
        components[f"{kind}_wheelhouse"] = {
            "included": True,
            "path": f"wheelhouse/{kind}",
            "wheel_count": len(package_rows),
            "packages": package_rows,
        }
    return metadata, components


def _file_metadata(
    staging_root: Path,
    *,
    source_commit: str,
    source_config: Mapping[str, object],
) -> tuple[dict[str, dict[str, str]], dict[str, object]]:
    metadata, components = _wheel_metadata_rows(staging_root, source_config)
    playwright = source_config["playwright_chromium"]
    ffmpeg = source_config["ffmpeg"]
    ffmpeg_license_sources = {
        f"tools/ffmpeg/licenses/{row['filename']}": row
        for row in ffmpeg.get("license_sources", [])
        if isinstance(row, Mapping) and isinstance(row.get("filename"), str)
    }
    repository_source = f"repository:{source_commit}"
    for path in sorted(staging_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(staging_root).as_posix()
        if relative in metadata:
            continue
        if relative.startswith("browsers/ffmpeg-"):
            is_license = relative.endswith(f"/{playwright['ffmpeg_license_filename']}")
            row = {
                "component": "playwright_recording_ffmpeg",
                "version": str(playwright["ffmpeg_revision"]),
                "platform": "win_amd64",
                "license": str(playwright["ffmpeg_license"]),
                "source": str(
                    playwright["ffmpeg_license_source" if is_license else "ffmpeg_source"]
                ),
            }
        elif relative.startswith("browsers/winldd-"):
            row = {
                "component": "playwright_winldd",
                "version": str(playwright["winldd_revision"]),
                "platform": "win_amd64",
                "license": str(playwright["winldd_license"]),
                "source": str(playwright["winldd_source"]),
            }
        elif relative.startswith("browsers/chromium_headless_shell-"):
            row = {
                "component": "playwright_chromium",
                "version": CHROMIUM_VERSION,
                "platform": "win_amd64",
                "license": str(playwright["license"]),
                "source": str(playwright["headless_shell_source"]),
            }
        elif relative.startswith("browsers/chromium-"):
            row = {
                "component": "playwright_chromium",
                "version": CHROMIUM_VERSION,
                "platform": "win_amd64",
                "license": str(playwright["license"]),
                "source": str(playwright["chromium_source"]),
            }
        elif relative in ffmpeg_license_sources:
            license_source = ffmpeg_license_sources[relative]
            row = {
                "component": "ffmpeg_license",
                "version": str(ffmpeg["version"]),
                "platform": "any",
                "license": str(license_source.get("license", ffmpeg["license"])),
                "source": str(license_source["url"]),
            }
        elif relative.startswith("provenance/ffmpeg/source/"):
            row = {
                "component": "ffmpeg_source_archive",
                "version": str(ffmpeg["release_tag"]),
                "platform": "source",
                "license": str(ffmpeg["license"]),
                "source": f"repository:{ffmpeg['source_archive_path']}",
            }
        elif relative.startswith("provenance/ffmpeg/build/"):
            evidence_sources = {
                "build_ffmpeg.sh": ffmpeg["build_recipe_path"],
                "build-receipt.json": ffmpeg["build_receipt_path"],
                "pe-imports.json": ffmpeg["pe_imports_path"],
                "media-probe.json": ffmpeg["media_probe_path"],
            }
            evidence_name = PurePosixPath(relative).name
            if evidence_name not in evidence_sources:
                raise ValueError("FFmpeg build evidence metadata is unmapped")
            row = {
                "component": "ffmpeg_build_evidence",
                "version": str(ffmpeg["version"]),
                "platform": "source",
                "license": "MIT",
                "source": f"repository:{evidence_sources[evidence_name]}",
            }
        elif relative == "provenance/ffmpeg/manifest.json":
            row = {
                "component": "ffmpeg_asset_manifest",
                "version": str(ffmpeg["version"]),
                "platform": "any",
                "license": "MIT",
                "source": f"repository:{ffmpeg.get('asset_manifest_path', 'scripts/release/ffmpeg_assets/manifest.json')}",
            }
        elif relative == "provenance/ffmpeg/license-encoding.json":
            row = {
                "component": "ffmpeg_license_index",
                "version": str(ffmpeg["version"]),
                "platform": "any",
                "license": "MIT",
                "source": "repository:scripts/release/offline_sources.json",
            }
        elif relative.startswith("tools/ffmpeg/"):
            row = {
                "component": "ffmpeg",
                "version": str(ffmpeg["version"]),
                "platform": "win_amd64",
                "license": str(ffmpeg["license"]),
                "source": str(ffmpeg["source"]),
            }
        elif relative == f"provenance/sources/{source_config['intervaltree']['filename']}":
            intervaltree = source_config["intervaltree"]
            row = {
                "component": "intervaltree_source",
                "version": str(intervaltree["version"]),
                "platform": "source",
                "license": str(intervaltree["license"]),
                "source": str(intervaltree["source"]),
            }
        elif relative.startswith("licenses/chromium/"):
            row = {
                "component": "playwright_chromium_license",
                "version": CHROMIUM_VERSION,
                "platform": "any",
                "license": str(playwright["license"]),
                "source": str(playwright["license_url"]),
            }
        elif relative.startswith("licenses/playwright-winldd/"):
            row = {
                "component": "playwright_winldd_source",
                "version": str(playwright["winldd_revision"]),
                "platform": "source",
                "license": str(playwright["winldd_license"]),
                "source": str(playwright["winldd_source_code_url"]),
            }
        elif relative.startswith("licenses/playwright/"):
            row = {
                "component": "playwright_license",
                "version": PLAYWRIGHT_VERSION,
                "platform": "any",
                "license": "Apache-2.0",
                "source": _https_url("pypi.org", f"project/playwright/{PLAYWRIGHT_VERSION}/"),
            }
        else:
            row = {
                "component": "release_metadata",
                "version": "1",
                "platform": "any",
                "license": "MIT",
                "source": repository_source,
            }
        metadata[relative] = row
    return metadata, components


def _git(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(repo_root),
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError("Git release identity command failed")
    return completed.stdout.strip()


def build_offline_deps(
    repo_root: Path,
    output_zip: Path,
    *,
    work_root: Path,
    ffmpeg_archive: Path | None = None,
) -> dict[str, object]:
    _assert_target_build_host()
    root = repo_root.resolve()
    source = capture_clean_release_source(root)
    version = source.version
    expected_name = f"Auto-Cut-v{version}-windows-x64-offline-deps.zip"
    if output_zip.name != expected_name:
        raise ValueError("offline dependency output filename does not match VERSION")
    if output_zip.exists():
        raise FileExistsError("offline dependency output already exists")
    source_commit = source.source_commit
    source_config = json.loads(
        _read_committed_blob(
            root,
            source_commit,
            "scripts/release/offline_sources.json",
        ).decode("utf-8")
    )
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="auto-cut-offline-", dir=work_root) as temporary:
        temporary_root = Path(temporary)
        staging = temporary_root / "payload"
        staging.mkdir()
        _copy_requirement_inputs(root, staging, source_commit)
        build_python = _prepare_build_runtime(root, staging)
        intervaltree_component = _download_wheelhouses(
            root,
            staging,
            source_config,
            build_python=build_python,
        )
        _install_playwright_probe_runtime(root, staging, build_python)

        playwright_source = source_config["playwright_chromium"]
        chromium_component = _stage_chromium(
            root,
            staging,
            playwright_source,
            python_executable=build_python,
        )
        _download(
            str(playwright_source["license_url"]),
            staging / "licenses" / "chromium" / "LICENSE",
            decode_base64=playwright_source.get("license_encoding") == "base64",
        )
        _verify_fixed_file(
            staging / "licenses" / "chromium" / "LICENSE",
            size=playwright_source.get("license_size"),
            sha256=playwright_source.get("license_sha256"),
            label="Chromium license",
        )
        _copy_playwright_license(
            staging / "wheelhouse" / "main",
            staging / "licenses" / "playwright" / "LICENSE",
        )
        _verify_fixed_file(
            staging / "licenses" / "playwright" / "LICENSE",
            size=playwright_source.get("playwright_license_size"),
            sha256=playwright_source.get("playwright_license_sha256"),
            label="Playwright license",
        )

        ffmpeg_source = source_config["ffmpeg"]
        _validate_ffmpeg_source_declaration(ffmpeg_source)
        if ffmpeg_source.get("source_kind") == "committed_repository_asset":
            # All FFmpeg bytes and evidence are read from the clean committed
            # tree.  In particular, do not honor a caller-provided archive or
            # invoke the network for this component.
            if ffmpeg_archive is not None:
                raise ValueError("committed FFmpeg release does not accept an external archive")
            ffmpeg_component = _stage_committed_ffmpeg_assets(
                root,
                staging,
                source_commit,
                ffmpeg_source,
                temporary_root=temporary_root,
            )
        else:
            source_archive = ffmpeg_archive or temporary_root / "ffmpeg-source.zip"
            if ffmpeg_archive is None:
                _download(str(ffmpeg_source["source"]), source_archive)
            if source_archive.stat().st_size != int(ffmpeg_source["archive_size"]):
                raise ValueError("FFmpeg source archive size mismatch")
            ffmpeg_component = stage_ffmpeg_archive(
                source_archive,
                staging / "tools" / "ffmpeg",
                ffmpeg_source,
            )
            license_dir = staging / "tools" / "ffmpeg" / "licenses"
            for license_source in ffmpeg_source.get("license_sources", []):
                if not isinstance(license_source, Mapping):
                    raise ValueError("FFmpeg license source metadata is invalid")
                filename = license_source.get("filename")
                url = license_source.get("url")
                if (
                    not isinstance(filename, str)
                    or PurePosixPath(filename).name != filename
                    or not isinstance(url, str)
                    or not url.strip()
                ):
                    raise ValueError("FFmpeg license source metadata is invalid")
                license_path = license_dir / filename
                _download(url, license_path)
                _verify_fixed_file(
                    license_path,
                    size=license_source.get("size"),
                    sha256=license_source.get("sha256"),
                    label=f"FFmpeg license {filename}",
                )
        ffmpeg_component.update(_validate_ffmpeg(staging, ffmpeg_component))

        file_metadata, wheel_components = _file_metadata(
            staging,
            source_commit=source_commit,
            source_config=source_config,
        )
        components = {
            **wheel_components,
            "playwright_chromium": chromium_component,
            "ffmpeg": ffmpeg_component,
            "intervaltree_source_build": intervaltree_component,
            "python_installer": source_config["python_installer"],
            "deepfilternet": {
                "wheel_cached": any(
                    path.name.casefold().startswith(("deepfilternet-", "deep_filter_lib-"))
                    for path in (staging / "wheelhouse" / "audio").glob("*.whl")
                ),
                "runtime_status": "unavailable",
                "reason": "No fixed verified model and adapter chain is bundled.",
            },
            "respiro": {
                "included": False,
                "runtime_status": "unavailable",
                "reason": "No compliant fixed model is available for redistribution.",
            },
        }
        validate_staged_text_privacy(staging)
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="auto-cut-offline-publish-", dir=output_zip.parent
        ) as publish_directory:
            staged_output = Path(publish_directory) / output_zip.name
            result = write_offline_bundle(
                staging,
                staged_output,
                release_version=version,
                source_commit=source_commit,
                components=components,
                file_metadata=file_metadata,
            )
            assert_release_source_unchanged(root, source)
            publish_file_no_replace(staged_output, output_zip)
    return {
        "schema_version": 1,
        "status": "ready",
        "version": version,
        "source_commit": source_commit,
        "zip_name": output_zip.name,
        "zip_size": result["zip_size"],
        "zip_sha256": result["zip_sha256"],
        "manifest_sha256": result["manifest_sha256"],
        "file_count": result["file_count"],
        "target": result["manifest"]["target"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the Auto-Cut offline dependency ZIP.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--work-root", default="tmp/offline-deps-build")
    parser.add_argument("--ffmpeg-archive")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_offline_deps(
            Path(args.repo_root),
            Path(args.output),
            work_root=Path(args.work_root),
            ffmpeg_archive=Path(args.ffmpeg_archive) if args.ffmpeg_archive else None,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": "offline_dependency_build_failed",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
