from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

import cv2
import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.release.offline_bundle import canonical_json, sha256_file
from scripts.release.release_policy import normalize_archive_path, scan_text
from scripts.release.release_transaction import (
    ReleaseSource,
    assert_release_source_unchanged,
    capture_clean_release_source,
    publish_file_no_replace,
    unique_sibling_temp,
)

PROFILE_KEY = "senior-high-history"
STAGE_ID = "senior-high"
SUBJECT_ID = "history"
PRIVATE_MANIFEST_NAME = "private-assets-manifest.json"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
_SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}")
_SAFE_PROFILE_SYMBOL = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
_MAX_PRIVATE_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_ASSET_FIELDS = {"asset_id", "role", "direction", "anchor"}
_SCALE_FIELDS = {
    "reference_id",
    "lesson",
    "time",
    "layout",
    "full_frame",
    "confirmed",
    "canvas_size",
    "visible_bbox",
}
_PREVIEW_FIELDS = {"approved"}
_PRIVATE_INTAKE_FIELDS = {
    "stage_id",
    "subject_id",
    "stage_name",
    "subject_name",
    "display_name",
    "aliases",
    "detection_evidence",
    "placement_policies",
    "assets",
    "scale_references",
    "approved_previews",
}
_PLACEMENT_POLICY_FIELDS = {
    "policy_id",
    "asset_role",
    "target_kind",
    "layout",
    "target_anchor",
    "gap_px",
}
_PRIVATE_MANIFEST_FIELDS = {
    "schema_version",
    "release_version",
    "source_commit",
    "profile_key",
    "stage_id",
    "subject_id",
    "target_requires_explicit_binding",
    "files",
    "manifest_sha256",
}
_PRIVATE_FILE_FIELDS = {
    "path",
    "size",
    "sha256",
    "source_sha256",
    "normalization",
    "role",
    "rights",
}
_PRIVATE_ROLES = {
    "hand",
    "scale-references",
    "approved-previews",
    "registration",
    "restore_guide",
}
_PRIVATE_IMAGE_ROLES = {"hand", "scale-references", "approved-previews"}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Kept as a public compatibility symbol for callers/tests that inject a
# captured release identity while exercising the builder in isolation.
__all__ = [
    "ReleaseSource",
    "build_private_subject_assets",
    "build_parser",
    "main",
    "verify_private_subject_assets_bundle",
    "write_private_profile_bundle",
]


def _safe_relative_path(raw: object) -> PurePosixPath:
    if not isinstance(raw, str):
        raise ValueError("unsafe profile evidence path")
    try:
        return PurePosixPath(normalize_archive_path(raw))
    except ValueError as exc:
        raise ValueError("unsafe profile evidence path") from exc


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_regular_profile_path(root: Path, relative: PurePosixPath) -> Path:
    if _is_reparse_point(root):
        raise ValueError("profile evidence file is missing or unsafe")
    current = root
    for part in relative.parts:
        current /= part
        if _is_reparse_point(current):
            raise ValueError("profile evidence file is missing or unsafe")
    if not current.is_file():
        raise ValueError("profile evidence file is missing or unsafe")
    return current


def _decode_image(data: bytes) -> np.ndarray:
    decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None or decoded.size == 0 or decoded.ndim not in {2, 3}:
        raise ValueError("profile evidence is not a decodable image")
    if decoded.ndim == 3 and decoded.shape[2] not in {1, 3, 4}:
        raise ValueError("profile evidence is not a decodable image")
    return decoded


def _image_contract(image: np.ndarray) -> dict[str, object]:
    height, width = image.shape[:2]
    channels = 1 if image.ndim == 2 else image.shape[2]
    return {
        "format": "png",
        "has_alpha": channels == 4,
        "width": int(width),
        "height": int(height),
    }


def _png_contract(data: bytes) -> dict[str, object]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("hand asset is not a decodable alpha PNG")
    contract = _image_contract(_decode_image(data))
    if contract["has_alpha"] is not True:
        raise ValueError("hand asset is not a decodable alpha PNG")
    return contract


def _normalize_image(
    source_data: bytes, destination: Path, *, require_alpha: bool
) -> dict[str, object]:
    source_pixels = _decode_image(source_data)
    source_contract = _image_contract(source_pixels)
    if require_alpha and source_contract["has_alpha"] is not True:
        raise ValueError("hand asset is not a decodable alpha PNG")
    encoded, output = cv2.imencode(
        ".png",
        source_pixels,
        [cv2.IMWRITE_PNG_COMPRESSION, 9],
    )
    if not encoded:
        raise ValueError("profile evidence PNG normalization failed")
    output_data = output.tobytes()
    output_pixels = _decode_image(output_data)
    if not np.array_equal(source_pixels, output_pixels):
        raise ValueError("profile evidence PNG normalization changed pixels")
    output_contract = _image_contract(output_pixels)
    if require_alpha and output_contract["has_alpha"] is not True:
        raise ValueError("hand asset PNG normalization lost alpha")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(output_data)
    return output_contract


def _exact_ready_profile(check_result: Mapping[str, object]) -> dict[str, object]:
    profile = check_result.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("profile_registry check did not return a profile")
    identity = (
        check_result.get("key"),
        check_result.get("stage_id"),
        check_result.get("subject_id"),
        profile.get("key"),
        profile.get("stage_id"),
        profile.get("subject_id"),
    )
    if identity != (PROFILE_KEY, STAGE_ID, SUBJECT_ID, PROFILE_KEY, STAGE_ID, SUBJECT_ID):
        raise ValueError("private release requires the exact senior-high-history profile")
    if (
        check_result.get("status") != "ready"
        or profile.get("status") != "ready"
        or check_result.get("missing_items") not in (None, [])
        or check_result.get("problems") not in (None, [])
        or profile.get("missing_items") != []
        or profile.get("problems") != []
    ):
        raise ValueError("exact subject profile is not ready")
    return profile


def _evidence_rows(profile: Mapping[str, object]) -> list[tuple[str, dict[str, object]]]:
    rows: list[tuple[str, dict[str, object]]] = []
    categories = (
        ("assets", "assets"),
        ("scale_references", "scale-references"),
        ("approved_previews", "approved-previews"),
    )
    for field, destination in categories:
        values = profile.get(field)
        if not isinstance(values, list) or not values:
            raise ValueError(f"ready profile is missing {field}")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("profile evidence row is invalid")
            rows.append((destination, value))
    if len(profile.get("scale_references", [])) < 2 or not all(
        row.get("confirmed") is True and row.get("full_frame") is True
        for row in profile["scale_references"]
    ):
        raise ValueError("profile scale references are not confirmed full frames")
    if not all(row.get("approved") is True for row in profile["approved_previews"]):
        raise ValueError("profile previews are not approved")
    hand_rows = [row for row in profile["assets"] if row.get("role") == "hand"]
    if not hand_rows:
        raise ValueError("ready profile is missing an owned hand PNG")
    return rows


def _sanitize_geometry(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("profile geometry is invalid")
    return {field: value[field] for field in fields if field in value}


def _safe_profile_symbol(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SAFE_PROFILE_SYMBOL.fullmatch(value) is None:
        raise ValueError(f"private profile {field} is invalid")
    return value


def _safe_anchor(value: object) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise ValueError("private profile anchor is invalid")
    anchor = [float(component) for component in value]
    if any(not math.isfinite(component) or component < 0 or component > 1 for component in anchor):
        raise ValueError("private profile anchor is invalid")
    return anchor


def _intake(
    profile: Mapping[str, object],
    path_map: Mapping[tuple[str, str], tuple[str, str]],
) -> dict[str, object]:
    intake: dict[str, object] = {
        "stage_id": STAGE_ID,
        "subject_id": SUBJECT_ID,
        "stage_name": "高中",
        "subject_name": "历史",
        "display_name": "高中历史",
        "aliases": ["高中历史"],
        "detection_evidence": [
            {
                "source": "explicit_user_command",
                "value": "迁移高中历史指向物素材库",
                "confirmed": True,
            }
        ],
    }
    policies = profile.get("placement_policies")
    if not isinstance(policies, list) or any(not isinstance(row, Mapping) for row in policies):
        raise ValueError("profile placement policies are invalid")
    sanitized_policies = []
    for index, row in enumerate(policies, start=1):
        asset_role = _safe_profile_symbol(row.get("asset_role"), field="asset role")
        target_kind = _safe_profile_symbol(row.get("target_kind"), field="target kind")
        layout = _safe_profile_symbol(row.get("layout"), field="layout")
        target_anchor = _safe_profile_symbol(row.get("target_anchor"), field="target anchor")
        gap_px = row.get("gap_px")
        if (
            asset_role != "hand"
            or not isinstance(gap_px, (int, float))
            or isinstance(gap_px, bool)
            or not math.isfinite(float(gap_px))
            or float(gap_px) < 0
        ):
            raise ValueError("profile placement policies are invalid")
        sanitized_policies.append(
            {
                "policy_id": f"history-placement-policy-{index:03d}",
                "asset_role": asset_role,
                "target_kind": target_kind,
                "layout": layout,
                "target_anchor": target_anchor,
                "gap_px": gap_px,
            }
        )
    intake["placement_policies"] = sanitized_policies

    assets = profile.get("assets")
    if not isinstance(assets, list):
        raise ValueError("profile evidence rows are invalid")
    sanitized_assets = []
    for index, row in enumerate(assets, start=1):
        if not isinstance(row, Mapping) or row.get("role") != "hand":
            raise ValueError("private profile contains a non-hand asset")
        destination, digest = path_map[("assets", str(row["path"]))]
        sanitized_assets.append(
            {
                "asset_id": f"history-hand-{index:03d}",
                "role": "hand",
                "anchor": _safe_anchor(row.get("anchor")),
                "path": destination,
                "sha256": digest,
            }
        )
    intake["assets"] = sanitized_assets

    scale_references = profile.get("scale_references")
    if not isinstance(scale_references, list):
        raise ValueError("profile evidence rows are invalid")
    sanitized_references = []
    for index, row in enumerate(scale_references, start=1):
        if not isinstance(row, Mapping):
            raise ValueError("profile evidence row is invalid")
        destination, digest = path_map[("scale-references", str(row["path"]))]
        sanitized_references.append(
            {
                "reference_id": f"history-scale-reference-{index:03d}",
                "lesson": f"迁移比例参考 {index}",
                "time": f"00:00:{index:02d}",
                "layout": _safe_profile_symbol(row.get("layout"), field="layout"),
                "full_frame": True,
                "confirmed": True,
                "canvas_size": _sanitize_geometry(row.get("canvas_size"), ("width", "height")),
                "visible_bbox": _sanitize_geometry(
                    row.get("visible_bbox"), ("x", "y", "width", "height")
                ),
                "path": destination,
                "sha256": digest,
            }
        )
    intake["scale_references"] = sanitized_references

    previews = profile.get("approved_previews")
    if not isinstance(previews, list):
        raise ValueError("profile evidence rows are invalid")
    sanitized_previews = []
    for row in previews:
        if not isinstance(row, Mapping):
            raise ValueError("profile evidence row is invalid")
        destination, digest = path_map[("approved-previews", str(row["path"]))]
        sanitized_previews.append({"approved": True, "path": destination, "sha256": digest})
    intake["approved_previews"] = sanitized_previews
    return intake


def _restore_guide(release_version: str) -> str:
    return f"""# Private High-School History Pointer Assets

This private archive is paired with Auto-Cut v{release_version}. Verify the ZIP
SHA-256 before extraction. Run these commands from the extracted Auto-Cut
program directory after extracting this private archive to a separate folder.
Use a new empty registry. Do not merge this intake into an existing registry or
reuse any existing project bindings.

```powershell
$privateRegistry = Join-Path "data" "subject-pointer-profiles.restored-history"
if (Test-Path -LiteralPath $privateRegistry) {{ throw "new empty registry required" }}
New-Item -ItemType Directory -Path $privateRegistry | Out-Null
.\\.venv\\Scripts\\python.exe skills/auto-cut-subject-pointer-onboarding/scripts/profile_registry.py register --input <private-assets>\\intake.json --root $privateRegistry --json
.\\.venv\\Scripts\\python.exe skills/auto-cut-subject-pointer-onboarding/scripts/profile_registry.py check --stage-id senior-high --subject-id history --root $privateRegistry --json
.\\.venv\\Scripts\\python.exe skills/auto-cut-subject-pointer-onboarding/scripts/profile_registry.py validate --root $privateRegistry --json
```

Continue only when the exact check returns `status=ready` and validation has no
problems. This archive contains no project binding. Ask the target user for an
explicit confirmation before binding or rebinding any target project.
"""


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, FIXED_ZIP_TIME)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def _unsigned_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return unsigned


def _validate_private_manifest(manifest: object) -> dict[str, object]:
    if not isinstance(manifest, dict) or set(manifest) != _PRIVATE_MANIFEST_FIELDS:
        raise ValueError("private manifest fields are invalid")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(manifest.get("release_version"), str)
        or _SEMVER.fullmatch(str(manifest["release_version"])) is None
        or not isinstance(manifest.get("source_commit"), str)
        or _SOURCE_COMMIT.fullmatch(str(manifest["source_commit"])) is None
        or manifest.get("profile_key") != PROFILE_KEY
        or manifest.get("stage_id") != STAGE_ID
        or manifest.get("subject_id") != SUBJECT_ID
        or manifest.get("target_requires_explicit_binding") is not True
    ):
        raise ValueError("private manifest identity is invalid")
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("private manifest file inventory is empty")
    paths: list[str] = []
    role_counts = {role: 0 for role in _PRIVATE_ROLES}
    for row in rows:
        if not isinstance(row, dict) or set(row) != _PRIVATE_FILE_FIELDS:
            raise ValueError("private manifest file metadata is invalid")
        path = _safe_relative_path(row.get("path")).as_posix()
        size = row.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError("private manifest file metadata is invalid")
        for field in ("sha256", "source_sha256"):
            value = row.get(field)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError("private manifest file metadata is invalid")
        if row.get("normalization") not in {"decoded_png_v1", "generated_v1"}:
            raise ValueError("private manifest file metadata is invalid")
        if row.get("role") not in _PRIVATE_ROLES:
            raise ValueError("private manifest file metadata is invalid")
        if row.get("rights") != "user_owned_migration_approved":
            raise ValueError("private manifest file metadata is invalid")
        expected_role: str
        expected_normalization: str
        if path == "intake.json":
            expected_role, expected_normalization = "registration", "generated_v1"
        elif path == "RESTORE.md":
            expected_role, expected_normalization = "restore_guide", "generated_v1"
        elif re.fullmatch(r"evidence/assets/hand-[0-9]{3}\.png", path):
            expected_role, expected_normalization = "hand", "decoded_png_v1"
        elif re.fullmatch(r"evidence/scale-references/scale-reference-[0-9]{3}\.png", path):
            expected_role, expected_normalization = "scale-references", "decoded_png_v1"
        elif re.fullmatch(r"evidence/approved-previews/preview-[0-9]{3}\.png", path):
            expected_role, expected_normalization = "approved-previews", "decoded_png_v1"
        else:
            raise ValueError("private manifest path prefix is invalid")
        if row.get("role") != expected_role or row.get("normalization") != expected_normalization:
            raise ValueError("private manifest path role mapping is invalid")
        if expected_normalization == "generated_v1" and row.get("source_sha256") != row.get(
            "sha256"
        ):
            raise ValueError("private generated payload source SHA-256 is invalid")
        role_counts[expected_role] += 1
        paths.append(path)
    if paths != sorted(paths, key=lambda value: (value.casefold(), value)) or len(paths) != len(
        set(paths)
    ):
        raise ValueError("private manifest paths must be sorted and unique")
    if len(paths) != len({path.casefold() for path in paths}):
        raise ValueError("private manifest paths collide on Windows")
    if not (
        role_counts["registration"] == 1
        and role_counts["restore_guide"] == 1
        and role_counts["hand"] >= 1
        and role_counts["scale-references"] >= 2
        and role_counts["approved-previews"] >= 1
    ):
        raise ValueError("private manifest role inventory is incomplete")
    declared = manifest.get("manifest_sha256")
    measured = hashlib.sha256(canonical_json(_unsigned_manifest(manifest))).hexdigest()
    if declared != measured:
        raise ValueError("private manifest hash mismatch")
    return manifest


def _private_intake_rows(
    intake: object,
) -> dict[str, tuple[str, list[dict[str, object]]]]:
    if not isinstance(intake, dict) or set(intake) != _PRIVATE_INTAKE_FIELDS:
        raise ValueError("private intake fields are invalid")
    if intake.get("stage_id") != STAGE_ID or intake.get("subject_id") != SUBJECT_ID:
        raise ValueError("private intake identity is invalid")
    if (
        intake.get("stage_name") != "高中"
        or intake.get("subject_name") != "历史"
        or intake.get("display_name") != "高中历史"
    ):
        raise ValueError("private intake identity is invalid")
    if intake.get("aliases") != ["高中历史"]:
        raise ValueError("private intake aliases are invalid")
    if intake.get("detection_evidence") != [
        {
            "source": "explicit_user_command",
            "value": "迁移高中历史指向物素材库",
            "confirmed": True,
        }
    ]:
        raise ValueError("private intake detection evidence is invalid")
    policies = intake.get("placement_policies")
    if not isinstance(policies, list) or any(
        not isinstance(row, dict) or set(row) != _PLACEMENT_POLICY_FIELDS for row in policies
    ):
        raise ValueError("private intake placement policies are invalid")
    for index, row in enumerate(policies, start=1):
        gap_px = row.get("gap_px")
        if (
            row.get("policy_id") != f"history-placement-policy-{index:03d}"
            or row.get("asset_role") != "hand"
            or any(
                _SAFE_PROFILE_SYMBOL.fullmatch(str(row.get(field, ""))) is None
                for field in ("target_kind", "layout", "target_anchor")
            )
            or not isinstance(gap_px, (int, float))
            or isinstance(gap_px, bool)
            or not math.isfinite(float(gap_px))
            or float(gap_px) < 0
        ):
            raise ValueError("private intake placement policies are invalid")

    groups: dict[str, tuple[str, list[dict[str, object]]]] = {}
    specifications = (
        (
            "assets",
            "hand",
            _ASSET_FIELDS | {"path", "sha256"},
            {"asset_id", "role", "path", "sha256"},
            1,
        ),
        (
            "scale_references",
            "scale-references",
            _SCALE_FIELDS | {"path", "sha256"},
            {
                "reference_id",
                "lesson",
                "time",
                "layout",
                "full_frame",
                "confirmed",
                "canvas_size",
                "visible_bbox",
                "path",
                "sha256",
            },
            2,
        ),
        (
            "approved_previews",
            "approved-previews",
            _PREVIEW_FIELDS | {"path", "sha256"},
            {"approved", "path", "sha256"},
            1,
        ),
    )
    for field, role, allowed, required, minimum in specifications:
        raw_rows = intake.get(field)
        if not isinstance(raw_rows, list) or len(raw_rows) < minimum:
            raise ValueError("private intake evidence inventory is incomplete")
        normalized_rows: list[dict[str, object]] = []
        for raw_row in raw_rows:
            if (
                not isinstance(raw_row, dict)
                or set(raw_row) - allowed
                or not required.issubset(raw_row)
            ):
                raise ValueError("private intake evidence row is invalid")
            path = _safe_relative_path(raw_row.get("path")).as_posix()
            digest = raw_row.get("sha256")
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise ValueError("private intake evidence SHA-256 is invalid")
            if field == "assets" and raw_row.get("role") != "hand":
                raise ValueError("private intake asset role is invalid")
            if field == "assets" and raw_row.get("asset_id") != (
                f"history-hand-{len(normalized_rows) + 1:03d}"
            ):
                raise ValueError("private intake asset identity is invalid")
            if field == "scale_references" and not (
                raw_row.get("full_frame") is True and raw_row.get("confirmed") is True
            ):
                raise ValueError("private intake scale reference is not confirmed")
            if field == "scale_references":
                index = len(normalized_rows) + 1
                if (
                    raw_row.get("reference_id") != f"history-scale-reference-{index:03d}"
                    or raw_row.get("lesson") != f"迁移比例参考 {index}"
                    or raw_row.get("time") != f"00:00:{index:02d}"
                    or _SAFE_PROFILE_SYMBOL.fullmatch(str(raw_row.get("layout", ""))) is None
                ):
                    raise ValueError("private intake scale reference metadata is invalid")
            if field == "approved_previews" and raw_row.get("approved") is not True:
                raise ValueError("private intake preview is not approved")
            normalized = dict(raw_row)
            normalized["path"] = path
            normalized_rows.append(normalized)
        groups[field] = (role, normalized_rows)
    return groups


def _validate_private_intake_manifest_mapping(
    intake: object,
    manifest: Mapping[str, object],
) -> None:
    groups = _private_intake_rows(intake)
    manifest_images = {
        str(row["path"]): row for row in manifest["files"] if row["role"] in _PRIVATE_IMAGE_ROLES
    }
    intake_paths: set[str] = set()
    for role, rows in groups.values():
        for row in rows:
            path = str(row["path"])
            if path in intake_paths:
                raise ValueError("private intake contains duplicate evidence paths")
            intake_paths.add(path)
            manifest_row = manifest_images.get(path)
            if not isinstance(manifest_row, dict) or not (
                manifest_row.get("role") == role and manifest_row.get("sha256") == row.get("sha256")
            ):
                raise ValueError("private intake and manifest evidence mapping is invalid")
    if intake_paths != set(manifest_images):
        raise ValueError("private intake and manifest evidence inventory differs")


def _validate_private_image_payload(data: bytes, *, role: str) -> None:
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("private image payload is not a normalized PNG")
    decoded = _decode_image(data)
    if role == "hand" and (decoded.ndim != 3 or decoded.shape[2] != 4):
        raise ValueError("private hand image payload is missing alpha")


def _zip_entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    return info.create_system == 3 and stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def verify_private_subject_assets_bundle(path: str | Path) -> dict[str, object]:
    archive_path = Path(path)
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("private archive is unreadable") from exc
    with archive:
        infos: dict[str, zipfile.ZipInfo] = {}
        casefold_paths: set[str] = set()
        total_size = 0
        for info in archive.infolist():
            if info.is_dir() or _zip_entry_is_symlink(info):
                raise ValueError("private archive contains an unsafe entry")
            name = _safe_relative_path(info.filename).as_posix()
            if name in infos or name.casefold() in casefold_paths:
                raise ValueError("private archive contains duplicate paths")
            infos[name] = info
            casefold_paths.add(name.casefold())
            total_size += info.file_size
            if total_size > _MAX_PRIVATE_ARCHIVE_BYTES:
                raise ValueError("private archive exceeds the size limit")
        manifest_info = infos.pop(PRIVATE_MANIFEST_NAME, None)
        if manifest_info is None or manifest_info.file_size > 16 * 1024 * 1024:
            raise ValueError("private archive manifest is missing or oversized")
        try:
            manifest = _validate_private_manifest(
                json.loads(archive.read(manifest_info).decode("utf-8"))
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("private archive manifest is unreadable") from exc
        expected_rows = manifest["files"]
        expected = {str(row["path"]): row for row in expected_rows}
        if set(infos) != set(expected):
            raise ValueError("private archive payload inventory mismatch")
        intake: object | None = None
        for name, row in expected.items():
            data = archive.read(infos[name])
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("private archive payload SHA-256 mismatch")
            if name == "intake.json":
                try:
                    intake = json.loads(data.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("private intake is unreadable") from exc
            if row["role"] in _PRIVATE_IMAGE_ROLES:
                _validate_private_image_payload(data, role=str(row["role"]))
        _validate_private_intake_manifest_mapping(intake, manifest)
    return {
        "status": "ready",
        "manifest": manifest,
        "manifest_sha256": manifest["manifest_sha256"],
        "file_count": len(manifest["files"]),
        "zip_sha256": sha256_file(archive_path),
    }


def write_private_profile_bundle(
    profile_root: str | Path,
    output_zip: str | Path,
    *,
    release_version: str,
    source_commit: str,
    check_result: Mapping[str, object],
) -> dict[str, object]:
    root = Path(profile_root)
    output = Path(output_zip)
    if output.exists():
        raise FileExistsError("private asset output already exists")
    if (
        _SEMVER.fullmatch(release_version) is None
        or _SOURCE_COMMIT.fullmatch(source_commit) is None
    ):
        raise ValueError("private release identity is invalid")
    profile = _exact_ready_profile(check_result)
    profile_path = _assert_regular_profile_path(root, PurePosixPath("profile.json"))
    stored_profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if stored_profile != profile:
        raise ValueError("profile_registry result does not match the stored exact profile")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="auto-cut-private-", dir=output.parent) as temporary:
        staging = Path(temporary) / "payload"
        staging.mkdir()
        path_map: dict[tuple[str, str], tuple[str, str]] = {}
        used_destinations: set[str] = set()
        destination_counts = {"assets": 0, "scale-references": 0, "approved-previews": 0}
        payload_metadata: dict[str, dict[str, str]] = {}
        for category, row in _evidence_rows(profile):
            source_relative = _safe_relative_path(row.get("path"))
            if source_relative.suffix.casefold() not in _IMAGE_SUFFIXES:
                raise ValueError("profile evidence must be an approved image file")
            source = _assert_regular_profile_path(root, source_relative)
            declared_hash = row.get("sha256")
            if not isinstance(declared_hash, str) or _SHA256.fullmatch(declared_hash) is None:
                raise ValueError("profile evidence SHA-256 is invalid")
            source_data = source.read_bytes()
            if hashlib.sha256(source_data).hexdigest() != declared_hash:
                raise ValueError("profile evidence SHA-256 mismatch")
            is_hand = category == "assets" and row.get("role") == "hand"
            if category == "assets" and not is_hand:
                raise ValueError("private profile contains a non-hand asset")
            if is_hand:
                if source_relative.suffix.casefold() != ".png":
                    raise ValueError("hand asset must be an alpha PNG")
                contract = _png_contract(source_data)
                if row.get("media_contract") != contract:
                    raise ValueError("hand asset media contract mismatch")
            destination_counts[category] += 1
            if category == "assets":
                destination_name = f"hand-{destination_counts[category]:03d}.png"
            elif category == "scale-references":
                destination_name = f"scale-reference-{destination_counts[category]:03d}.png"
            else:
                destination_name = f"preview-{destination_counts[category]:03d}.png"
            destination_relative = f"evidence/{category}/{destination_name}"
            if destination_relative.casefold() in used_destinations:
                raise ValueError("profile evidence destination collision")
            destination = staging / Path(*PurePosixPath(destination_relative).parts)
            normalized_contract = _normalize_image(
                source_data,
                destination,
                require_alpha=is_hand,
            )
            if is_hand and normalized_contract != row.get("media_contract"):
                raise ValueError("normalized hand asset media contract mismatch")
            normalized_hash = sha256_file(destination)
            path_map[(category, source_relative.as_posix())] = (
                destination_relative,
                normalized_hash,
            )
            used_destinations.add(destination_relative.casefold())
            payload_metadata[destination_relative] = {
                "role": str(row.get("role")) if category == "assets" else category,
                "source_sha256": declared_hash,
                "normalization": "decoded_png_v1",
            }

        intake = _intake(profile, path_map)
        intake_path = staging / "intake.json"
        intake_path.write_bytes(canonical_json(intake))
        restore_path = staging / "RESTORE.md"
        restore_path.write_text(_restore_guide(release_version), encoding="utf-8", newline="\n")
        for text_path in (intake_path, restore_path):
            findings = scan_text(
                text_path.relative_to(staging).as_posix(),
                text_path.read_text(encoding="utf-8"),
            )
            if findings:
                raise ValueError("private archive text failed privacy validation")
            relative = text_path.relative_to(staging).as_posix()
            generated_hash = sha256_file(text_path)
            payload_metadata[relative] = {
                "role": "registration" if relative == "intake.json" else "restore_guide",
                "source_sha256": generated_hash,
                "normalization": "generated_v1",
            }

        payload_paths = sorted(
            (path for path in staging.rglob("*") if path.is_file()),
            key=lambda path: (
                path.relative_to(staging).as_posix().casefold(),
                path.relative_to(staging).as_posix(),
            ),
        )
        rows = []
        for path in payload_paths:
            relative = path.relative_to(staging).as_posix()
            metadata = payload_metadata[relative]
            rows.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "source_sha256": metadata["source_sha256"],
                    "normalization": metadata["normalization"],
                    "role": metadata["role"],
                    "rights": "user_owned_migration_approved",
                }
            )
        manifest: dict[str, object] = {
            "schema_version": 1,
            "release_version": release_version,
            "source_commit": source_commit,
            "profile_key": PROFILE_KEY,
            "stage_id": STAGE_ID,
            "subject_id": SUBJECT_ID,
            "target_requires_explicit_binding": True,
            "files": rows,
        }
        manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()

        with unique_sibling_temp(output) as temporary_zip:
            with zipfile.ZipFile(
                temporary_zip,
                "w",
                compression=zipfile.ZIP_STORED,
            ) as archive:
                for path in payload_paths:
                    relative = path.relative_to(staging).as_posix()
                    archive.writestr(_zip_info(relative), path.read_bytes())
                archive.writestr(_zip_info(PRIVATE_MANIFEST_NAME), canonical_json(manifest))
            verification = verify_private_subject_assets_bundle(temporary_zip)
            zip_size = temporary_zip.stat().st_size
            zip_sha256 = sha256_file(temporary_zip)
            publish_file_no_replace(temporary_zip, output)
    return {
        "schema_version": 1,
        "status": "ready",
        "version": release_version,
        "source_commit": source_commit,
        "zip_name": output.name,
        "zip_size": zip_size,
        "zip_sha256": zip_sha256,
        "manifest_sha256": verification["manifest_sha256"],
        "profile_key": PROFILE_KEY,
        "file_count": verification["file_count"],
    }


def _run_registry(repo_root: Path, registry_root: Path, command: str) -> object:
    script = (
        repo_root
        / "skills"
        / "auto-cut-subject-pointer-onboarding"
        / "scripts"
        / "profile_registry.py"
    )
    arguments = [sys.executable, str(script), command]
    if command == "check":
        arguments.extend(("--stage-id", STAGE_ID, "--subject-id", SUBJECT_ID))
    arguments.extend(("--root", str(registry_root), "--json"))
    completed = subprocess.run(
        arguments,
        cwd=str(repo_root),
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError("profile_registry verification failed")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("profile_registry output is invalid") from exc


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


def build_private_subject_assets(
    repo_root: Path,
    registry_root: Path,
    output_zip: Path,
) -> dict[str, object]:
    root = repo_root.resolve()
    if registry_root.is_absolute():
        resolved_registry_root = registry_root.resolve()
    else:
        resolved_registry_root = (root / registry_root).resolve()
        try:
            resolved_registry_root.relative_to(root)
        except ValueError as exc:
            raise ValueError("relative registry root escapes the repository") from exc
    source = capture_clean_release_source(root)
    version = source.version
    expected_name = f"Auto-Cut-v{version}-private-assets-high-school-history.zip"
    if output_zip.name != expected_name:
        raise ValueError("private asset output filename does not match VERSION")
    check_result = _run_registry(root, resolved_registry_root, "check")
    validation = _run_registry(root, resolved_registry_root, "validate")
    if not isinstance(validation, list) or not any(
        isinstance(row, dict)
        and row.get("key") == PROFILE_KEY
        and row.get("status") == "ready"
        and row.get("problems") == []
        for row in validation
    ):
        raise ValueError("registry-wide validation did not preserve exact profile readiness")
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="auto-cut-private-publish-", dir=output_zip.parent
    ) as publish_directory:
        staged_output = Path(publish_directory) / output_zip.name
        result = write_private_profile_bundle(
            resolved_registry_root / PROFILE_KEY,
            staged_output,
            release_version=version,
            source_commit=source.source_commit,
            check_result=check_result,
        )
        assert_release_source_unchanged(root, source)
        publish_file_no_replace(staged_output, output_zip)
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the private high-school history asset ZIP.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--registry-root", default="data/subject-pointer-profiles.local")
    parser.add_argument("--output", required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_private_subject_assets(
            Path(args.repo_root), Path(args.registry_root), Path(args.output)
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": "private_asset_build_failed",
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
