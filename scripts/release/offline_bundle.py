from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .release_policy import normalize_archive_path, scan_text
from .release_transaction import publish_file_no_replace, unique_sibling_temp

OFFLINE_MANIFEST_NAME = "offline-deps-manifest.json"
OFFLINE_SCHEMA_VERSION = 1
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
MAX_OFFLINE_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
_SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}")
_REQUIRED_FILE_METADATA = (
    "component",
    "version",
    "platform",
    "license",
    "source",
)
_MANIFEST_FIELDS = {
    "schema_version",
    "release_version",
    "source_commit",
    "target",
    "components",
    "files",
    "manifest_sha256",
}
_FILE_FIELDS = {"path", "size", "sha256", *_REQUIRED_FILE_METADATA}
_EXPECTED_TARGET = {
    "os": "windows",
    "arch": "x64",
    "python_implementation": "cpython",
    "python_version": "3.11",
    "abi": "cp311",
}


def canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _canonical_path(raw_path: str) -> str:
    if not isinstance(raw_path, str):
        raise ValueError("unsafe offline bundle path")
    try:
        return normalize_archive_path(raw_path)
    except ValueError as exc:
        raise ValueError("unsafe offline bundle path") from exc


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return info.create_system == 3 and stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def _unsigned_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    return payload


def _validate_manifest(
    manifest: object,
    *,
    expected_version: str | None,
    expected_source_commit: str | None,
) -> dict[str, object]:
    if not isinstance(manifest, dict):
        raise ValueError("offline bundle manifest must be an object")
    if set(manifest) != _MANIFEST_FIELDS:
        raise ValueError("offline bundle manifest fields are invalid")
    if manifest.get("schema_version") != OFFLINE_SCHEMA_VERSION:
        raise ValueError("offline bundle manifest schema is unsupported")
    version = manifest.get("release_version")
    if not isinstance(version, str) or _SEMVER.fullmatch(version) is None:
        raise ValueError("offline bundle release version is invalid")
    if expected_version is not None and version != expected_version:
        raise ValueError("offline bundle release version mismatch")
    source_commit = manifest.get("source_commit")
    if not isinstance(source_commit, str) or _SOURCE_COMMIT.fullmatch(source_commit) is None:
        raise ValueError("offline bundle source commit is invalid")
    if expected_source_commit is not None and source_commit != expected_source_commit:
        raise ValueError("offline bundle source commit mismatch")
    if manifest.get("target") != _EXPECTED_TARGET:
        raise ValueError("offline bundle target identity mismatch")
    if not isinstance(manifest.get("components"), dict):
        raise ValueError("offline bundle components are invalid")
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("offline bundle file rows are invalid")
    paths: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FILE_FIELDS:
            raise ValueError("offline bundle file metadata is invalid")
        try:
            path = _canonical_path(row["path"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("offline bundle file metadata is invalid") from exc
        size = row.get("size")
        digest = row.get("sha256")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise ValueError("offline bundle file metadata is invalid")
        for field in _REQUIRED_FILE_METADATA:
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("offline bundle file metadata is incomplete")
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("offline bundle file paths must be sorted and unique")
    if len({path.casefold() for path in paths}) != len(paths):
        raise ValueError("offline bundle file paths collide on Windows")
    declared_digest = manifest.get("manifest_sha256")
    actual_digest = hashlib.sha256(canonical_json(_unsigned_manifest(manifest))).hexdigest()
    if not isinstance(declared_digest, str) or declared_digest != actual_digest:
        raise ValueError("offline bundle manifest hash mismatch")
    return manifest


def _verify_payload_rows(
    manifest: Mapping[str, object],
    *,
    sizes_and_hashes: Mapping[str, tuple[int, str]],
) -> None:
    expected = {
        str(row["path"]): (int(row["size"]), str(row["sha256"]))
        for row in manifest["files"]
        if isinstance(row, dict)
    }
    if set(expected) != set(sizes_and_hashes):
        raise ValueError("offline bundle payload inventory mismatch")
    for path, expected_value in expected.items():
        actual_value = sizes_and_hashes[path]
        if actual_value[0] != expected_value[0]:
            raise ValueError("offline bundle payload size mismatch")
        if actual_value[1] != expected_value[1]:
            raise ValueError("offline bundle payload hash mismatch")


def verify_offline_bundle(
    bundle_path: str | Path,
    *,
    expected_version: str | None = None,
    expected_source_commit: str | None = None,
) -> dict[str, object]:
    path = Path(bundle_path)
    if path.is_dir():
        manifest_path = path / OFFLINE_MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("offline bundle manifest is unreadable") from exc
        validated = _validate_manifest(
            manifest,
            expected_version=expected_version,
            expected_source_commit=expected_source_commit,
        )
        actual: dict[str, tuple[int, str]] = {}
        for candidate in sorted(path.rglob("*")):
            if not candidate.is_file() or candidate == manifest_path:
                continue
            relative = candidate.relative_to(path).as_posix()
            _canonical_path(relative)
            if candidate.is_symlink():
                raise ValueError("unsafe offline bundle symlink")
            actual[relative] = (candidate.stat().st_size, sha256_file(candidate))
        _verify_payload_rows(validated, sizes_and_hashes=actual)
    elif path.is_file():
        try:
            archive = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError("offline bundle ZIP is unreadable") from exc
        with archive:
            infos: dict[str, zipfile.ZipInfo] = {}
            casefold: set[str] = set()
            total_size = 0
            for info in archive.infolist():
                name = _canonical_path(
                    info.filename.rstrip("/") if info.is_dir() else info.filename
                )
                if info.is_dir():
                    continue
                if _is_symlink(info):
                    raise ValueError("unsafe offline bundle symlink")
                if name in infos or name.casefold() in casefold:
                    raise ValueError("offline bundle contains duplicate paths")
                infos[name] = info
                casefold.add(name.casefold())
                total_size += info.file_size
                if total_size > MAX_OFFLINE_ARCHIVE_BYTES:
                    raise ValueError("offline bundle exceeds extraction limit")
            manifest_info = infos.pop(OFFLINE_MANIFEST_NAME, None)
            if manifest_info is None or manifest_info.file_size > 32 * 1024 * 1024:
                raise ValueError("offline bundle manifest is missing or oversized")
            try:
                manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
                raise ValueError("offline bundle manifest is unreadable") from exc
            validated = _validate_manifest(
                manifest,
                expected_version=expected_version,
                expected_source_commit=expected_source_commit,
            )
            actual = {}
            for name, info in sorted(infos.items()):
                with archive.open(info) as stream:
                    actual[name] = (info.file_size, _sha256_stream(stream))
            _verify_payload_rows(validated, sizes_and_hashes=actual)
    else:
        raise FileNotFoundError("offline bundle does not exist")
    return {
        "status": "ready",
        "manifest": validated,
        "manifest_sha256": validated["manifest_sha256"],
        "file_count": len(validated["files"]),
    }


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, FIXED_ZIP_TIME)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def _validate_manifest_privacy(manifest_bytes: bytes) -> None:
    findings = scan_text(OFFLINE_MANIFEST_NAME, manifest_bytes.decode("utf-8"))
    if findings:
        codes = ", ".join(sorted({finding.code for finding in findings}))
        raise ValueError(f"offline dependency manifest privacy validation failed: {codes}")


def write_offline_bundle(
    staging_root: str | Path,
    output_zip: str | Path,
    *,
    release_version: str,
    source_commit: str,
    components: Mapping[str, object],
    file_metadata: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    root = Path(staging_root)
    output = Path(output_zip)
    if output.exists():
        raise FileExistsError(f"offline bundle output already exists: {output.name}")
    if not root.is_dir():
        raise FileNotFoundError("offline bundle staging root does not exist")
    payload_paths = sorted(
        candidate.relative_to(root).as_posix()
        for candidate in root.rglob("*")
        if candidate.is_file()
    )
    if OFFLINE_MANIFEST_NAME in payload_paths:
        raise ValueError("staging root must not contain the generated manifest")
    if set(payload_paths) != set(file_metadata):
        raise ValueError("offline bundle file metadata does not match staging payload")
    rows: list[dict[str, object]] = []
    for relative in payload_paths:
        _canonical_path(relative)
        path = root / Path(*PurePosixPath(relative).parts)
        if path.is_symlink():
            raise ValueError("unsafe offline bundle symlink")
        metadata = file_metadata[relative]
        row: dict[str, object] = {
            "path": relative,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for field in _REQUIRED_FILE_METADATA:
            value = metadata.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("offline bundle file metadata is incomplete")
            row[field] = value
        rows.append(row)
    manifest: dict[str, object] = {
        "schema_version": OFFLINE_SCHEMA_VERSION,
        "release_version": release_version,
        "source_commit": source_commit,
        "target": dict(_EXPECTED_TARGET),
        "components": dict(components),
        "files": rows,
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    _validate_manifest(
        manifest,
        expected_version=release_version,
        expected_source_commit=source_commit,
    )
    manifest_bytes = canonical_json(manifest)
    _validate_manifest_privacy(manifest_bytes)
    with unique_sibling_temp(output) as temporary:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            for relative in payload_paths:
                path = root / Path(*PurePosixPath(relative).parts)
                info = _zip_info(relative)
                info.file_size = path.stat().st_size
                with (
                    path.open("rb") as source_stream,
                    archive.open(info, "w") as target_stream,
                ):
                    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
            archive.writestr(_zip_info(OFFLINE_MANIFEST_NAME), manifest_bytes)
        verified = verify_offline_bundle(
            temporary,
            expected_version=release_version,
            expected_source_commit=source_commit,
        )
        result = {
            **verified,
            "zip_sha256": sha256_file(temporary),
            "zip_size": temporary.stat().st_size,
        }
        publish_file_no_replace(temporary, output)
        return result


def extract_offline_bundle(bundle_zip: str | Path, destination: str | Path) -> dict[str, object]:
    source = Path(bundle_zip)
    target = Path(destination)
    if target.exists():
        raise FileExistsError("offline bundle extraction destination already exists")
    with zipfile.ZipFile(source) as archive:
        entries: list[tuple[zipfile.ZipInfo, str]] = []
        seen: set[str] = set()
        seen_casefold: set[str] = set()
        total_size = 0
        for info in archive.infolist():
            normalized = _canonical_path(
                info.filename.rstrip("/") if info.is_dir() else info.filename
            )
            if _is_symlink(info):
                raise ValueError("unsafe offline bundle symlink")
            if normalized in seen or normalized.casefold() in seen_casefold:
                raise ValueError("offline bundle contains duplicate paths")
            seen.add(normalized)
            seen_casefold.add(normalized.casefold())
            total_size += info.file_size
            if total_size > MAX_OFFLINE_ARCHIVE_BYTES:
                raise ValueError("offline bundle exceeds extraction limit")
            entries.append((info, normalized))
        target.mkdir(parents=True)
        try:
            for info, normalized in entries:
                output = target / Path(*PurePosixPath(normalized).parts)
                if info.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source_stream, output.open("xb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
            return verify_offline_bundle(target)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise


__all__ = [
    "OFFLINE_MANIFEST_NAME",
    "canonical_json",
    "extract_offline_bundle",
    "sha256_file",
    "verify_offline_bundle",
    "write_offline_bundle",
]
