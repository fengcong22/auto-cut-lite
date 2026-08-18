from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .errors import PortableProjectError
from .hashing import canonical_json_sha256, sha256_file

MANIFEST_FILENAME = "material_manifest.json"
REPORT_FILENAME = "validation_report.json"
MANIFEST_SCHEMA_VERSION = 2
IMPORTER_FILENAME = "AutoCut工程导入工具.exe"
INSTRUCTIONS_FILENAME = "使用说明.txt"
MEDIA_MARKER = "@AUTOCUT_MEDIA@"
DRAFT_ROOT_MARKER = "@AUTOCUT_DRAFT_ROOT@"
DRAFT_DIR_MARKER = "@AUTOCUT_DRAFT_DIR@"

_SAFE_MATERIAL_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def validate_material_id(material_id: object) -> str:
    value = str(material_id or "")
    stem = value.split(".", 1)[0].upper()
    if not _SAFE_MATERIAL_ID.fullmatch(value) or stem in _WINDOWS_RESERVED_NAMES:
        raise PortableProjectError(
            "unsafe_material_id",
            "Material ID cannot be represented as a safe package directory",
            {"material_id": value},
        )
    return value


def validate_relative_path(value: object) -> str:
    raw = str(value or "")
    if not raw or "\\" in raw or ":" in raw or raw.startswith("/"):
        raise PortableProjectError(
            "unsafe_package_path", "Package path must be a safe relative POSIX path"
        )
    path = PurePosixPath(raw)
    if path.as_posix() != raw or any(part in {"", ".", ".."} for part in path.parts):
        raise PortableProjectError(
            "unsafe_package_path", "Package path contains traversal or ambiguous components"
        )
    for part in path.parts:
        if part.endswith((" ", ".")) or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise PortableProjectError(
                "unsafe_package_path", "Package path is not portable to Windows"
            )
    return raw


def logical_media_path(kind: str, material_id: str, basename: str) -> str:
    relative = material_relative_path(kind, material_id, basename)
    return f"{MEDIA_MARKER}/{relative.removeprefix('Media/')}"


def material_relative_path(kind: str, material_id: str, basename: str) -> str:
    kind_value = str(kind).strip().title()
    if kind_value not in {"Video", "Audio", "Image", "Other"}:
        raise PortableProjectError("unsafe_package_path", "Unknown material package kind")
    safe_id = validate_material_id(material_id)
    basename_value = str(basename or "")
    if (
        not basename_value
        or basename_value in {".", ".."}
        or "/" in basename_value
        or "\\" in basename_value
    ):
        raise PortableProjectError(
            "unsafe_package_path", "Material basename is not a single safe path component"
        )
    return validate_relative_path(f"Media/{kind_value}/{safe_id}/{basename_value}")


def seal_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(payload))
    result.pop("manifest_sha256", None)
    result["manifest_sha256"] = canonical_json_sha256(result)
    return result


def verify_manifest_digest(payload: Mapping[str, Any]) -> None:
    expected = str(payload.get("manifest_sha256") or "")
    unsigned = deepcopy(dict(payload))
    unsigned.pop("manifest_sha256", None)
    if len(expected) != 64 or canonical_json_sha256(unsigned) != expected:
        raise PortableProjectError(
            "manifest_hash_mismatch", "Portable project manifest digest does not match"
        )


def read_manifest(package_dir: Path) -> dict[str, Any]:
    path = Path(package_dir) / MANIFEST_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableProjectError(
            "invalid_manifest", "Portable project manifest is not readable JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise PortableProjectError("invalid_manifest", "Portable project manifest is invalid")
    verify_manifest_digest(payload)
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != MANIFEST_SCHEMA_VERSION:
        raise PortableProjectError(
            "invalid_manifest", "Portable project manifest schema is unsupported"
        )
    return payload


def package_file_row(package_dir: Path, relative_path: str, *, role: str) -> dict[str, Any]:
    safe_path = validate_relative_path(relative_path)
    path = Path(package_dir).joinpath(*PurePosixPath(safe_path).parts)
    return {
        "path": safe_path,
        "role": str(role),
        "byte_size": path.stat().st_size,
        "sha256": sha256_file(path),
    }
