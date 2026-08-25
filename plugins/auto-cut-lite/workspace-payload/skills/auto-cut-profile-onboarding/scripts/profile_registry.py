from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import cv2
import numpy as np

SCHEMA_VERSION = 1
ASPECT_RATIO_16_9 = 16 / 9
ASPECT_RATIO_TOLERANCE = 1e-3
ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
REQUIRED_IDENTITY_FIELDS = ("stage_name", "subject_name", "aliases", "detection_evidence")
EVIDENCE_GROUPS = (
    ("assets", "assets"),
    ("scale_references", "scale-references"),
    ("approved_previews", "approved-previews"),
)
STATUS_LABELS = {
    "missing": "资料不完整",
    "incomplete": "资料不完整",
    "needs_confirmation": "待确认",
    "ready": "可使用",
    "stale": "素材已变化",
}
TRANSACTION_PHASES = {"prepared", "old_moved", "candidate_active", "committed"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PLACEMENT_POLICY_FIELDS = (
    "policy_id",
    "asset_role",
    "target_kind",
    "layout",
    "target_anchor",
    "gap_px",
)
MANUAL_STAGE_IDS = {
    "幼儿园": "kindergarten",
    "小学": "primary",
    "初中": "junior-high",
    "高中": "senior-high",
    "中职": "vocational",
    "大学": "university",
}
MANUAL_SUBJECT_IDS = {
    "语文": "chinese",
    "数学": "math",
    "英语": "english",
    "历史": "history",
    "地理": "geography",
    "物理": "physics",
    "化学": "chemistry",
    "生物": "biology",
    "政治": "politics",
    "道德与法治": "ethics-and-law",
    "科学": "science",
    "信息技术": "information-technology",
    "音乐": "music",
    "美术": "art",
    "体育": "physical-education",
}


def profile_key(stage_id: str, subject_id: str) -> str:
    """Return the exact registry key for one lowercase ASCII stage/subject pair."""

    for label, value in (("stage_id", stage_id), ("subject_id", subject_id)):
        if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{label} must be a lowercase ASCII hyphen ID, got {value!r}")
    return f"{stage_id}-{subject_id}"


def _explicit_name_id(prefix: str, value: str, known: Mapping[str, str]) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError(f"{prefix}_name is required")
    return known.get(name) or f"{prefix}-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]}"


def manual_identity(stage_name: str, subject_name: str) -> dict[str, str]:
    """Create stable internal IDs from names explicitly supplied by the user."""

    clean_stage = str(stage_name or "").strip()
    clean_subject = str(subject_name or "").strip()
    stage_id = _explicit_name_id("stage", clean_stage, MANUAL_STAGE_IDS)
    subject_id = _explicit_name_id("subject", clean_subject, MANUAL_SUBJECT_IDS)
    return {
        "stage_id": stage_id,
        "stage_name": clean_stage,
        "subject_id": subject_id,
        "subject_name": clean_subject,
        "profile_key": profile_key(stage_id, subject_id),
    }


def _looks_like_profile_key(value: str) -> bool:
    return "-" in value and ID_PATTERN.fullmatch(value) is not None


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Hash a file without loading it all into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _registry_root(registry_root: str | os.PathLike[str] | None) -> Path:
    if registry_root is None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is required when --root is omitted")
        return (Path(local_app_data) / "Auto-Cut" / "auto-cut-lite" / "pointer-profiles.local").resolve()
    return Path(registry_root).expanduser().resolve()


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink():
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_no_reparse_below(root: Path, target: Path) -> None:
    root = _absolute_lexical(root)
    target = _absolute_lexical(target)
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise ValueError(f"path escapes registry root: {target}") from error
    current = root
    for part in relative.parts:
        current /= part
        if (current.exists() or current.is_symlink()) and _is_reparse_point(current):
            component = current.relative_to(root).as_posix()
            raise ValueError(f"reparse_component:{component}")


def _resolve_stored_evidence_path(
    profile_directory: Path,
    expected_group: str,
    stored_path: Any,
) -> Path:
    if not isinstance(stored_path, str) or not stored_path:
        raise ValueError("path_required")
    if "\\" in stored_path:
        raise ValueError("path_not_normalized")
    raw_parts = stored_path.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError("path_not_normalized")
    pure_path = PurePosixPath(stored_path)
    native_path = Path(stored_path)
    if pure_path.is_absolute() or native_path.is_absolute() or native_path.drive:
        raise ValueError("absolute_path_forbidden")
    if len(raw_parts) < 2 or raw_parts[0] != expected_group:
        raise ValueError(f"expected_group:{expected_group}")

    target = profile_directory.joinpath(*raw_parts)
    expected_directory = profile_directory / expected_group
    _assert_no_reparse_below(profile_directory.parent, target)
    try:
        _absolute_lexical(target).relative_to(_absolute_lexical(expected_directory))
    except ValueError as error:
        raise ValueError(f"expected_group:{expected_group}") from error
    resolved_target = target.resolve(strict=False)
    resolved_group = expected_directory.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_group):
        raise ValueError("resolved_path_escapes_group")
    return target


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _positive_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be a positive number")
    return float(value)


def _nonnegative_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{label} must be a nonnegative number")
    return float(value)


def _dimensions(value: Any, label: str) -> tuple[float, float]:
    if isinstance(value, Mapping):
        width = value.get("width")
        height = value.get("height")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        width, height = value[0], value[1]
    else:
        raise ValueError(f"{label} must contain width and height")
    return _positive_number(width, f"{label}.width"), _positive_number(height, f"{label}.height")


def _bbox_values(value: Any) -> tuple[float, float, float, float]:
    if isinstance(value, Mapping):
        x = value.get("x")
        y = value.get("y")
        width = value.get("width")
        height = value.get("height")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 4:
        x, y, width, height = value
    else:
        raise ValueError("visible_bbox must contain x, y, width, and height")
    return (
        _nonnegative_number(x, "visible_bbox.x"),
        _nonnegative_number(y, "visible_bbox.y"),
        _positive_number(width, "visible_bbox.width"),
        _positive_number(height, "visible_bbox.height"),
    )


def _scale_reference_problems(raw_entry: Any, index: int) -> list[str]:
    prefix = f"scale_references[{index}]"
    if not isinstance(raw_entry, Mapping):
        return [f"{prefix}.object_required"]
    problems: list[str] = []
    if raw_entry.get("full_frame") is not True:
        problems.append(f"{prefix}.full_frame_required")
    for field in ("lesson", "time", "layout"):
        value = raw_entry.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{prefix}.{field}_required")

    canvas: tuple[float, float] | None = None
    bbox: tuple[float, float, float, float] | None = None
    try:
        canvas = _dimensions(raw_entry.get("canvas_size"), "canvas_size")
    except ValueError:
        problems.append(f"{prefix}.canvas_size_invalid")
    if (
        canvas is not None
        and abs(canvas[0] / canvas[1] - ASPECT_RATIO_16_9) > ASPECT_RATIO_TOLERANCE
    ):
        problems.append(f"{prefix}.canvas_not_16_9")
    try:
        bbox = _bbox_values(raw_entry.get("visible_bbox"))
    except ValueError:
        problems.append(f"{prefix}.visible_bbox_invalid")
    if canvas is not None and bbox is not None:
        x, y, width, height = bbox
        if x + width > canvas[0] or y + height > canvas[1]:
            problems.append(f"{prefix}.visible_bbox_outside_canvas")
    return problems


def _hand_media_contract(source_path: Path, label: str) -> dict[str, Any]:
    payload = source_path.read_bytes()
    if payload[: len(PNG_SIGNATURE)] != PNG_SIGNATURE:
        raise ValueError(f"{label}.hand_asset_png_invalid")
    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None or decoded.ndim != 3:
        raise ValueError(f"{label}.hand_asset_png_invalid")
    height, width = decoded.shape[:2]
    if decoded.shape[2] != 4:
        raise ValueError(f"{label}.hand_asset_alpha_required")
    return {
        "format": "png",
        "has_alpha": True,
        "width": int(width),
        "height": int(height),
    }


def _normalize_placement_policies(value: Any, label: str) -> list[dict[str, Any]]:
    entries = _require_list(value, label)
    normalized: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for index, raw_entry in enumerate(entries):
        prefix = f"{label}[{index}]"
        entry = _require_mapping(raw_entry, prefix)
        policy_id = entry.get("policy_id")
        if not isinstance(policy_id, str) or ID_PATTERN.fullmatch(policy_id) is None:
            raise ValueError(f"{prefix}.policy_id_invalid")
        policy: dict[str, Any] = {"policy_id": policy_id}
        for field in ("asset_role", "target_kind", "layout", "target_anchor"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{prefix}.{field}_required")
            policy[field] = value.strip()
        try:
            policy["gap_px"] = _nonnegative_number(entry.get("gap_px"), f"{prefix}.gap_px")
        except ValueError as error:
            raise ValueError(f"{prefix}.gap_px_invalid") from error
        if policy_id in positions:
            normalized[positions[policy_id]] = policy
        else:
            positions[policy_id] = len(normalized)
            normalized.append(policy)
    policy_ids_by_key: dict[tuple[str, str, str], str] = {}
    for policy in normalized:
        policy_key = (
            policy["asset_role"],
            policy["target_kind"],
            policy["layout"],
        )
        existing_policy_id = policy_ids_by_key.get(policy_key)
        if existing_policy_id is not None and existing_policy_id != policy["policy_id"]:
            raise ValueError(
                f"{label}.placement_policy_key_ambiguous:"
                f"{policy_key!r}:{existing_policy_id}:{policy['policy_id']}"
            )
        policy_ids_by_key[policy_key] = policy["policy_id"]
    return normalized


def _merge_placement_policies(existing: Any, incoming: Any) -> list[dict[str, Any]]:
    existing_policies = _normalize_placement_policies(
        existing if existing is not None else [], "existing.placement_policies"
    )
    if incoming is None:
        return existing_policies
    incoming_policies = _normalize_placement_policies(incoming, "intake.placement_policies")
    return _normalize_placement_policies(
        [*existing_policies, *incoming_policies], "merged.placement_policies"
    )


def _prepare_evidence(
    raw_entry: Any,
    *,
    label: str,
    intake_directory: Path,
    derive_ratios: bool = False,
) -> tuple[Path, dict[str, Any]]:
    entry = dict(_require_mapping(raw_entry, label))
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"{label}.path must be a non-empty string")
    source_path = Path(raw_path).expanduser()
    if not source_path.is_absolute():
        source_path = intake_directory / source_path
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"evidence file does not exist: {source_path}")

    actual_hash = sha256_file(source_path)
    expected_hash = entry.get("sha256")
    if expected_hash is not None and expected_hash != actual_hash:
        raise ValueError(
            f"SHA-256 mismatch for {source_path}: expected {expected_hash}, got {actual_hash}"
        )

    entry["sha256"] = actual_hash
    entry["source_name"] = source_path.name
    if str(entry.get("role") or "").strip().lower() == "hand":
        entry["media_contract"] = _hand_media_contract(source_path, label)
    if derive_ratios:
        try:
            canvas_width, canvas_height = _dimensions(entry.get("canvas_size"), "canvas_size")
            _, _, visible_width, visible_height = _bbox_values(entry.get("visible_bbox"))
        except ValueError:
            pass
        else:
            entry["visible_width_ratio"] = visible_width / canvas_width
            entry["visible_height_ratio"] = visible_height / canvas_height
    return source_path, entry


def _copy_file_verified(
    source: Path,
    destination: Path,
    expected_sha256: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".copying",
    )
    temporary_path = Path(temporary_name)
    try:
        with source.open("rb") as input_file, os.fdopen(file_descriptor, "wb") as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        copied_hash = sha256_file(temporary_path)
        if copied_hash != expected_sha256:
            raise OSError(
                "copied evidence SHA-256 mismatch: "
                f"expected {expected_sha256}, got {copied_hash}"
            )
        os.replace(temporary_path, destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _store_evidence(
    prepared_entries: list[tuple[Path, dict[str, Any]]],
    *,
    profile_directory: Path,
    destination_directory: str,
) -> list[dict[str, Any]]:
    stored_entries: list[dict[str, Any]] = []
    target_directory = profile_directory / destination_directory
    target_directory.mkdir(parents=True, exist_ok=True)
    for source_path, entry in prepared_entries:
        source_name = entry.get("source_name")
        if (
            not isinstance(source_name, str)
            or not source_name.strip()
            or Path(source_name).name != source_name
        ):
            source_name = source_path.name
        filename = f"{entry['sha256'][:12]}-{source_name}"
        destination_path = target_directory / filename
        _copy_file_verified(source_path, destination_path, entry["sha256"])
        stored_entry = dict(entry)
        stored_entry["path"] = (Path(destination_directory) / filename).as_posix()
        stored_entries.append(stored_entry)
    return stored_entries


def _identity_metadata_issues(profile: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    missing_items: list[str] = []
    problems: list[str] = []
    missing_fields = [field for field in REQUIRED_IDENTITY_FIELDS if field not in profile]
    if missing_fields:
        missing_items.extend(missing_fields)
        problems.append("missing_required_profile_fields")

    for field in ("stage_name", "subject_name"):
        if field in profile and (not isinstance(profile[field], str) or not profile[field].strip()):
            missing_items.append(field)
            problems.append(f"invalid_profile_field:{field}")

    aliases = profile.get("aliases")
    if "aliases" in profile and (
        not isinstance(aliases, list)
        or not aliases
        or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
    ):
        missing_items.append("aliases")
        problems.append("invalid_profile_field:aliases")

    detection_evidence = profile.get("detection_evidence")
    evidence_valid = isinstance(detection_evidence, list) and bool(detection_evidence)
    if evidence_valid:
        for evidence in detection_evidence:
            if not isinstance(evidence, Mapping):
                evidence_valid = False
                break
            if any(
                not isinstance(evidence.get(field), str) or not evidence[field].strip()
                for field in ("source", "value")
            ) or not isinstance(evidence.get("confirmed"), bool):
                evidence_valid = False
                break
        if evidence_valid and not any(
            evidence.get("confirmed") is True for evidence in detection_evidence
        ):
            evidence_valid = False
    if "detection_evidence" in profile and not evidence_valid:
        missing_items.append("detection_evidence")
        problems.append("invalid_profile_field:detection_evidence")
    if "placement_policies" in profile:
        try:
            _normalize_placement_policies(
                profile.get("placement_policies"), "profile.placement_policies"
            )
        except ValueError as error:
            problems.append(f"invalid_profile_field:placement_policies:{error}")
    return list(dict.fromkeys(missing_items)), problems


def _profile_group(profile: Mapping[str, Any], field: str) -> list[Any]:
    value = profile.get(field)
    return value if isinstance(value, list) else []


def _evidence_schema_issues(
    profile: Mapping[str, Any], profile_directory: Path
) -> tuple[list[str], list[str]]:
    missing_items: list[str] = []
    problems: list[str] = []
    for field, expected_group in EVIDENCE_GROUPS:
        entries = profile.get(field)
        if not isinstance(entries, list):
            missing_items.append(field)
            problems.append(f"profile_field_not_list:{field}")
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                problems.append(f"{field}[{index}].object_required")
                continue
            if not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
                problems.append(f"{field}[{index}].path_sha256_required")
                continue
            if SHA256_PATTERN.fullmatch(entry["sha256"]) is None:
                problems.append(f"{field}[{index}].sha256_invalid")
                continue
            try:
                evidence_path = _resolve_stored_evidence_path(
                    profile_directory,
                    expected_group,
                    entry["path"],
                )
            except ValueError as error:
                problems.append(f"unsafe_evidence_path:{field}[{index}]:{error}")
                continue
            if field == "assets" and str(entry.get("role") or "").strip().lower() == "hand":
                contract = entry.get("media_contract")
                if not isinstance(contract, Mapping):
                    problems.append(f"assets[{index}].hand_media_contract_required")
                    continue
                if evidence_path.is_file():
                    try:
                        decoded_contract = _hand_media_contract(evidence_path, f"assets[{index}]")
                    except ValueError as error:
                        problems.append(str(error))
                    else:
                        expected_contract = {
                            field_name: contract.get(field_name)
                            for field_name in ("format", "has_alpha", "width", "height")
                        }
                        if expected_contract != decoded_contract:
                            problems.append(f"assets[{index}].hand_media_contract_mismatch")
    return missing_items, problems


def _count_evidence(profile: Mapping[str, Any]) -> tuple[dict[str, int], list[str]]:
    assets = _profile_group(profile, "assets")
    scale_references = _profile_group(profile, "scale_references")
    approved_previews = _profile_group(profile, "approved_previews")
    scale_problems: list[str] = []
    full_frame_count = 0
    qualifying_count = 0
    for index, item in enumerate(scale_references):
        problems = _scale_reference_problems(item, index)
        scale_problems.extend(problems)
        if not problems:
            full_frame_count += 1
            if isinstance(item, Mapping) and item.get("confirmed") is True:
                qualifying_count += 1
    return (
        {
            "assets": len(assets),
            "scale_references": len(scale_references),
            "full_frame_scale_references": full_frame_count,
            "confirmed_scale_references": sum(
                1
                for item in scale_references
                if isinstance(item, Mapping) and item.get("confirmed") is True
            ),
            "qualifying_scale_references": qualifying_count,
            "approved_previews": sum(
                1
                for item in approved_previews
                if isinstance(item, Mapping) and item.get("approved") is True
            ),
        },
        scale_problems,
    )


def _missing_items(
    counts: Mapping[str, int], *, approved_preview_entries: int, schema_missing: list[str]
) -> tuple[str, list[str]]:
    missing = list(schema_missing)
    if counts["assets"] < 1:
        missing.append(f"assets:{counts['assets']}")
    if counts["scale_references"] < 2:
        missing.append(f"scale_references:{counts['scale_references']}")
    if counts["full_frame_scale_references"] < 2:
        missing.append(f"qualifying_scale_references:{counts['full_frame_scale_references']}")
    if approved_preview_entries < 1:
        missing.append("approved_previews:0")
    if missing:
        return "incomplete", list(dict.fromkeys(missing))

    if counts["qualifying_scale_references"] < 2:
        missing.append(f"confirmed_scale_references:{counts['qualifying_scale_references']}")
    if counts["approved_previews"] < 1:
        missing.append("approved_previews:0")
    if missing:
        return "needs_confirmation", missing
    return "ready", []


def _integrity_problems(profile: Mapping[str, Any], profile_directory: Path) -> list[str]:
    problems: list[str] = []
    for profile_key_name, expected_group in EVIDENCE_GROUPS:
        entries = profile.get(profile_key_name)
        if not isinstance(entries, list):
            continue
        for raw_entry in entries:
            if not isinstance(raw_entry, Mapping):
                continue
            relative_path = raw_entry.get("path")
            expected_hash = raw_entry.get("sha256")
            if not isinstance(relative_path, str) or not isinstance(expected_hash, str):
                continue
            if SHA256_PATTERN.fullmatch(expected_hash) is None:
                continue
            try:
                evidence_path = _resolve_stored_evidence_path(
                    profile_directory,
                    expected_group,
                    relative_path,
                )
            except ValueError:
                continue
            if not evidence_path.is_file():
                problems.append(f"missing_evidence:{relative_path}")
            elif sha256_file(evidence_path) != expected_hash:
                problems.append(f"sha256_mismatch:{relative_path}")
    return problems


def _base_result(
    *,
    key: str,
    stage_id: str | None,
    subject_id: str | None,
    status: str,
    profile_path: Path,
    profile: Mapping[str, Any] | None,
    counts: Mapping[str, int] | None = None,
    missing_items: list[str] | None = None,
    problems: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "stage_id": stage_id,
        "subject_id": subject_id,
        "status": status,
        "profile_path": str(profile_path.resolve()),
        "profile": dict(profile) if profile is not None else None,
        "counts": dict(counts or {}),
        "missing_items": list(missing_items or []),
        "problems": list(problems or []),
    }


def _evaluate_profile(
    profile: Mapping[str, Any],
    *,
    expected_stage_id: str,
    expected_subject_id: str,
    profile_path: Path,
) -> dict[str, Any]:
    key = profile_key(expected_stage_id, expected_subject_id)
    schema_missing, schema_problems = _identity_metadata_issues(profile)
    evidence_missing, evidence_problems = _evidence_schema_issues(profile, profile_path.parent)
    schema_missing.extend(evidence_missing)
    if profile.get("schema_version") != SCHEMA_VERSION:
        schema_missing.append("schema_version")
        if "missing_required_profile_fields" not in schema_problems:
            schema_problems.append("missing_required_profile_fields")
    identity_problems: list[str] = []
    if (
        profile.get("key") != key
        or profile.get("stage_id") != expected_stage_id
        or profile.get("subject_id") != expected_subject_id
    ):
        identity_problems.append("profile_identity_mismatch")
    counts, scale_problems = _count_evidence(profile)
    integrity_problems = _integrity_problems(profile, profile_path.parent)
    status, missing_items = _missing_items(
        counts,
        approved_preview_entries=len(_profile_group(profile, "approved_previews")),
        schema_missing=schema_missing,
    )
    profile_problems = [
        *identity_problems,
        *schema_problems,
        *evidence_problems,
        *scale_problems,
    ]
    if profile_problems:
        status = "incomplete"
    elif integrity_problems:
        status = "stale"
    return _base_result(
        key=key,
        stage_id=expected_stage_id,
        subject_id=expected_subject_id,
        status=status,
        profile_path=profile_path,
        profile=profile,
        counts=counts,
        missing_items=missing_items,
        problems=[*profile_problems, *integrity_problems],
    )


def _load_existing_profile(
    profile_path: Path,
    *,
    stage_id: str,
    subject_id: str,
) -> dict[str, Any] | None:
    if not profile_path.is_file():
        return None
    existing = _read_json(profile_path)
    if not isinstance(existing, Mapping):
        raise ValueError(f"existing profile root must be an object: {profile_path}")
    if (
        existing.get("key") != profile_key(stage_id, subject_id)
        or existing.get("stage_id") != stage_id
        or existing.get("subject_id") != subject_id
    ):
        raise ValueError(f"registry key collision at {profile_path}")
    return dict(existing)


def _prepare_existing_evidence(
    profile: Mapping[str, Any] | None,
    profile_directory: Path,
) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    prepared = {field: [] for field, _ in EVIDENCE_GROUPS}
    if profile is None:
        return prepared
    for field, expected_group in EVIDENCE_GROUPS:
        entries = profile.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if (
                not isinstance(entry, Mapping)
                or not isinstance(entry.get("sha256"), str)
                or SHA256_PATTERN.fullmatch(entry["sha256"]) is None
            ):
                continue
            source_path = _resolve_stored_evidence_path(
                profile_directory,
                expected_group,
                entry.get("path"),
            )
            if not source_path.is_file() or sha256_file(source_path) != entry["sha256"]:
                continue
            prepared[field].append((source_path, dict(entry)))
    return prepared


def _nonempty_text(value: Any, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _with_logical_evidence_id(field: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(entry)
    if field == "assets":
        asset_id = normalized.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id.strip():
            role = _nonempty_text(normalized.get("role"), "asset")
            direction = _nonempty_text(normalized.get("direction"), "default")
            normalized["asset_id"] = f"{role}:{direction}"
    elif field == "scale_references":
        reference_id = normalized.get("reference_id")
        if not isinstance(reference_id, str) or not reference_id.strip():
            layout = _nonempty_text(normalized.get("layout"), "layout")
            lesson = _nonempty_text(normalized.get("lesson"), "lesson")
            time = _nonempty_text(normalized.get("time"), "time")
            normalized["reference_id"] = f"{layout}:{lesson}:{time}"
    return normalized


def _evidence_identity(field: str, entry: Mapping[str, Any]) -> tuple[Any, ...]:
    if field == "assets":
        return (entry.get("asset_id"),)
    if field == "scale_references":
        return (entry.get("reference_id"),)
    return (entry.get("sha256"),)


def _merge_prepared_evidence(
    field: str,
    existing: list[tuple[Path, dict[str, Any]]],
    incoming: list[tuple[Path, dict[str, Any]]],
) -> list[tuple[Path, dict[str, Any]]]:
    merged: list[tuple[Path, dict[str, Any]]] = []
    positions: dict[tuple[Any, ...], int] = {}
    for source_path, raw_entry in [*existing, *incoming]:
        entry = _with_logical_evidence_id(field, raw_entry)
        item = (source_path, entry)
        identity = _evidence_identity(field, entry)
        if identity in positions:
            merged[positions[identity]] = item
        else:
            positions[identity] = len(merged)
            merged.append(item)
    return merged


def _merge_strings(existing: Any, incoming: Any, removed: Any = None) -> list[str]:
    removed_values = (
        {value for value in removed if isinstance(value, str)}
        if isinstance(removed, list)
        else set()
    )
    merged: list[str] = []
    if isinstance(existing, list):
        for value in existing:
            if isinstance(value, str) and value not in removed_values and value not in merged:
                merged.append(value)
    if isinstance(incoming, list):
        for value in incoming:
            if isinstance(value, str) and value not in merged:
                merged.append(value)
    return merged


def _detection_identity(value: Mapping[str, Any]) -> tuple[Any, Any]:
    return value.get("source"), value.get("value")


def _merge_detection_evidence(existing: Any, incoming: Any) -> list[Any]:
    merged: list[Any] = []
    positions: dict[tuple[Any, Any], int] = {}
    for collection in (existing, incoming):
        if not isinstance(collection, list):
            continue
        for value in collection:
            if not isinstance(value, Mapping):
                if value not in merged:
                    merged.append(value)
                continue
            copied = dict(value)
            identity = _detection_identity(copied)
            if identity in positions:
                merged[positions[identity]] = copied
            else:
                positions[identity] = len(merged)
                merged.append(copied)
    return merged


def _build_candidate_profile(
    intake: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
    *,
    key: str,
    stage_id: str,
    subject_id: str,
) -> dict[str, Any]:
    existing = existing or {}
    profile: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "key": key,
        "stage_id": stage_id,
        "subject_id": subject_id,
    }
    for field in ("stage_name", "subject_name", "display_name"):
        if field in intake:
            profile[field] = intake[field]
        elif field in existing:
            profile[field] = existing[field]
    profile["aliases"] = _merge_strings(
        existing.get("aliases"),
        intake.get("aliases"),
        intake.get("remove_aliases"),
    )
    profile["detection_evidence"] = _merge_detection_evidence(
        existing.get("detection_evidence"),
        intake.get("detection_evidence"),
    )
    profile["placement_policies"] = _merge_placement_policies(
        existing.get("placement_policies"),
        intake.get("placement_policies") if "placement_policies" in intake else None,
    )
    return profile


def _cleanup_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or _is_reparse_point(path):
        path.unlink()
    else:
        shutil.rmtree(path)


def _restore_catalog_snapshots(root: Path, snapshots: Mapping[str, bytes | None]) -> None:
    for name, content in snapshots.items():
        path = root / name
        if content is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write_bytes(path, content)


def _transaction_journal_path(root: Path, key: str) -> Path:
    return root / f".{key}.transaction.json"


def _transaction_artifact_path(root: Path, key: str, name: Any, kind: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError(f"invalid transaction {kind} name: {name!r}")
    if not name.startswith(f".{key}.{kind}-"):
        raise ValueError(f"transaction {kind} name does not match key {key}: {name}")
    path = root / name
    _assert_no_reparse_below(root, path)
    return path


def _encode_catalog_snapshots(
    snapshots: Mapping[str, bytes | None],
) -> dict[str, str | None]:
    return {
        name: None if content is None else base64.b64encode(content).decode("ascii")
        for name, content in snapshots.items()
    }


def _decode_catalog_snapshots(raw: Any) -> dict[str, bytes | None]:
    catalogs = _require_mapping(raw, "transaction.catalogs")
    snapshots: dict[str, bytes | None] = {}
    for name in ("catalog.json", "catalog.md"):
        encoded = catalogs.get(name)
        if encoded is None:
            snapshots[name] = None
            continue
        if not isinstance(encoded, str):
            raise ValueError(f"transaction.catalogs.{name} must be base64 or null")
        try:
            snapshots[name] = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError(f"transaction.catalogs.{name} is not valid base64") from error
    return snapshots


def _write_transaction_journal(path: Path, transaction: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(transaction, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _load_transaction_journal(
    root: Path,
    journal_path: Path,
) -> tuple[dict[str, Any], Path, Path, dict[str, bytes | None]]:
    _assert_no_reparse_below(root, journal_path)
    transaction = dict(
        _require_mapping(_read_json(journal_path), f"transaction journal {journal_path}")
    )
    key = transaction.get("key")
    if (
        transaction.get("schema_version") != SCHEMA_VERSION
        or not isinstance(key, str)
        or not _looks_like_profile_key(key)
        or journal_path.name != f".{key}.transaction.json"
    ):
        raise ValueError(f"invalid transaction journal identity: {journal_path}")
    phase = transaction.get("phase")
    if phase not in TRANSACTION_PHASES:
        raise ValueError(f"invalid transaction phase in {journal_path}: {phase!r}")
    if not isinstance(transaction.get("had_active"), bool):
        raise ValueError(f"transaction.had_active must be boolean: {journal_path}")
    staging = _transaction_artifact_path(root, key, transaction.get("staging"), "staging")
    backup = _transaction_artifact_path(root, key, transaction.get("backup"), "backup")
    snapshots = _decode_catalog_snapshots(transaction.get("catalogs"))
    return transaction, staging, backup, snapshots


def _recover_profile_transaction(root: Path, journal_path: Path) -> list[str]:
    transaction, staging, backup, snapshots = _load_transaction_journal(root, journal_path)
    key = transaction["key"]
    active = root / key
    _assert_no_reparse_below(root, active)

    if transaction["phase"] == "committed":
        warnings: list[str] = []
        if not active.is_dir():
            raise ValueError(f"committed transaction is missing active profile: {active}")
        for artifact in (staging, backup):
            try:
                _cleanup_path(artifact)
            except OSError as error:
                warnings.append(f"cleanup_failed:{artifact.name}:{error}")
        if warnings:
            return warnings
        try:
            journal_path.unlink(missing_ok=True)
        except OSError as error:
            return [f"cleanup_failed:{journal_path.name}:{error}"]
        return []

    had_active = transaction["had_active"]
    backup_exists = backup.exists() or backup.is_symlink()
    active_exists = active.exists() or active.is_symlink()
    if backup_exists:
        if active_exists:
            _cleanup_path(active)
        os.replace(backup, active)
    elif had_active:
        if not active_exists:
            raise ValueError(f"cannot recover missing active and backup for {key}")
    elif active_exists:
        _cleanup_path(active)

    _restore_catalog_snapshots(root, snapshots)
    _cleanup_path(staging)
    journal_path.unlink(missing_ok=True)
    return []


def _recover_transactions(root: Path, key: str | None = None) -> list[str]:
    if not root.exists():
        return []
    journal_paths = (
        [_transaction_journal_path(root, key)]
        if key is not None
        else sorted(
            path
            for path in root.iterdir()
            if path.name.startswith(".") and path.name.endswith(".transaction.json")
        )
    )
    warnings: list[str] = []
    for journal_path in journal_paths:
        _assert_no_reparse_below(root, journal_path)
        if not journal_path.exists() and not journal_path.is_symlink():
            continue
        warnings.extend(_recover_profile_transaction(root, journal_path))
    return warnings


def _commit_candidate_profile(
    root: Path,
    key: str,
    staging_directory: Path,
) -> list[str]:
    active_directory = root / key
    backup_directory = root / f".{key}.backup-{uuid.uuid4().hex}"
    catalog_snapshots: dict[str, bytes | None] = {}
    for name in ("catalog.json", "catalog.md"):
        catalog_path = root / name
        _assert_no_reparse_below(root, catalog_path)
        catalog_snapshots[name] = catalog_path.read_bytes() if catalog_path.is_file() else None
    journal_path = _transaction_journal_path(root, key)
    transaction: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "key": key,
        "phase": "prepared",
        "staging": staging_directory.name,
        "backup": backup_directory.name,
        "had_active": active_directory.exists() or active_directory.is_symlink(),
        "catalogs": _encode_catalog_snapshots(catalog_snapshots),
    }
    _write_transaction_journal(journal_path, transaction)
    try:
        if transaction["had_active"]:
            _assert_no_reparse_below(root, active_directory)
            os.replace(active_directory, backup_directory)
        transaction["phase"] = "old_moved"
        _write_transaction_journal(journal_path, transaction)

        os.replace(staging_directory, active_directory)
        transaction["phase"] = "candidate_active"
        _write_transaction_journal(journal_path, transaction)

        render_catalog(root, _recover=False)
        transaction["phase"] = "committed"
        _write_transaction_journal(journal_path, transaction)
    except BaseException as error:
        try:
            _recover_profile_transaction(root, journal_path)
        except BaseException as recovery_error:
            if hasattr(error, "add_note"):
                error.add_note(f"transaction recovery also failed: {recovery_error}")
        raise
    return _recover_profile_transaction(root, journal_path)


def _stage_hand_media_migration_evidence(
    profile: Mapping[str, Any],
    *,
    profile_directory: Path,
    staging_directory: Path,
) -> tuple[dict[str, Any], int]:
    candidate = dict(profile)
    migrated_count = 0
    for field, expected_group in EVIDENCE_GROUPS:
        entries = profile.get(field)
        if not isinstance(entries, list):
            raise ValueError(f"migration_existing_evidence_invalid:{field}:list_required")
        staged_entries: list[dict[str, Any]] = []
        for index, raw_entry in enumerate(entries):
            label = f"{field}[{index}]"
            if not isinstance(raw_entry, Mapping):
                raise ValueError(f"migration_existing_evidence_invalid:{label}:object_required")
            entry = dict(raw_entry)
            expected_hash = entry.get("sha256")
            if (
                not isinstance(expected_hash, str)
                or SHA256_PATTERN.fullmatch(expected_hash) is None
            ):
                raise ValueError(f"migration_existing_evidence_invalid:{label}:sha256_invalid")
            try:
                source_path = _resolve_stored_evidence_path(
                    profile_directory,
                    expected_group,
                    entry.get("path"),
                )
            except ValueError as error:
                raise ValueError(f"migration_existing_evidence_invalid:{label}:{error}") from error
            if not source_path.is_file():
                raise ValueError(f"migration_existing_evidence_invalid:{label}:missing_evidence")
            try:
                actual_hash = sha256_file(source_path)
            except OSError as error:
                raise ValueError(f"migration_existing_evidence_invalid:{label}:{error}") from error
            if actual_hash != expected_hash:
                raise ValueError(f"migration_existing_evidence_invalid:{label}:sha256_mismatch")

            if (
                field == "assets"
                and str(entry.get("role") or "").strip().lower() == "hand"
                and "media_contract" not in entry
            ):
                try:
                    entry["media_contract"] = _hand_media_contract(source_path, label)
                except (OSError, ValueError) as error:
                    raise ValueError(
                        f"migration_existing_evidence_invalid:{label}:{error}"
                    ) from error
                migrated_count += 1

            try:
                destination_path = _resolve_stored_evidence_path(
                    staging_directory,
                    expected_group,
                    entry.get("path"),
                )
                _copy_file_verified(source_path, destination_path, expected_hash)
            except OSError as error:
                if "SHA-256 mismatch" in str(error):
                    raise ValueError(
                        f"migration_existing_evidence_invalid:{label}:sha256_mismatch"
                    ) from error
                raise
            staged_entries.append(entry)
        candidate[field] = staged_entries
    return candidate, migrated_count


def migrate_hand_media_contracts(
    stage_id: str,
    subject_id: str,
    registry_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Add missing hand media contracts from verified stored profile evidence."""

    key = profile_key(stage_id, subject_id)
    root = _registry_root(registry_root)
    cleanup_warnings = _recover_transactions(root, key)
    pending_journal = _transaction_journal_path(root, key)
    if pending_journal.exists() or pending_journal.is_symlink():
        raise OSError(f"pending transaction cleanup for {key}: {pending_journal}")

    profile_directory = root / key
    profile_path = profile_directory / "profile.json"
    _assert_no_reparse_below(root, profile_directory)
    _assert_no_reparse_below(root, profile_path)
    for _, evidence_group in EVIDENCE_GROUPS:
        _assert_no_reparse_below(root, profile_directory / evidence_group)
    for catalog_name in ("catalog.json", "catalog.md"):
        _assert_no_reparse_below(root, root / catalog_name)
    existing = _load_existing_profile(
        profile_path,
        stage_id=stage_id,
        subject_id=subject_id,
    )
    if existing is None:
        raise FileNotFoundError(f"profile does not exist: {profile_path}")

    staging_directory = Path(tempfile.mkdtemp(prefix=f".{key}.staging-", dir=root))
    try:
        candidate, migrated_count = _stage_hand_media_migration_evidence(
            existing,
            profile_directory=profile_directory,
            staging_directory=staging_directory,
        )
        if migrated_count == 0:
            _cleanup_path(staging_directory)
            result = _check_profile_unrecovered(stage_id, subject_id, root)
            if cleanup_warnings:
                result["cleanup_warnings"] = cleanup_warnings
            return result

        staging_profile_path = staging_directory / "profile.json"
        evaluated = _evaluate_profile(
            candidate,
            expected_stage_id=stage_id,
            expected_subject_id=subject_id,
            profile_path=staging_profile_path,
        )
        candidate["status"] = evaluated["status"]
        candidate["missing_items"] = evaluated["missing_items"]
        candidate["problems"] = evaluated["problems"]
        _atomic_write_text(
            staging_profile_path,
            json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        cleanup_warnings.extend(_commit_candidate_profile(root, key, staging_directory))
    except BaseException:
        if staging_directory.exists() or staging_directory.is_symlink():
            _cleanup_path(staging_directory)
        raise

    result = _check_profile_unrecovered(stage_id, subject_id, root)
    if cleanup_warnings:
        result["cleanup_warnings"] = cleanup_warnings
    return result


def register_profile(
    intake_path: str | os.PathLike[str],
    registry_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Register one exact stage/subject profile and refresh both catalogs."""

    intake_file = Path(intake_path).expanduser().resolve()
    intake = _require_mapping(_read_json(intake_file), "intake")
    stage_id = intake.get("stage_id")
    subject_id = intake.get("subject_id")
    key = profile_key(stage_id, subject_id)
    root = _registry_root(registry_root)
    root.mkdir(parents=True, exist_ok=True)
    cleanup_warnings = _recover_transactions(root, key)
    pending_journal = _transaction_journal_path(root, key)
    if pending_journal.exists() or pending_journal.is_symlink():
        raise OSError(f"pending transaction cleanup for {key}: {pending_journal}")
    remove_aliases = _require_list(intake.get("remove_aliases", []), "intake.remove_aliases")
    if any(not isinstance(alias, str) or not alias.strip() for alias in remove_aliases):
        raise ValueError("intake.remove_aliases entries must be non-empty strings")
    profile_directory = root / key
    profile_path = profile_directory / "profile.json"
    _assert_no_reparse_below(root, profile_directory)
    _assert_no_reparse_below(root, profile_path)
    for _, evidence_group in EVIDENCE_GROUPS:
        _assert_no_reparse_below(root, profile_directory / evidence_group)
    for catalog_name in ("catalog.json", "catalog.md"):
        _assert_no_reparse_below(root, root / catalog_name)
    existing = _load_existing_profile(
        profile_path,
        stage_id=stage_id,
        subject_id=subject_id,
    )

    prepared: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for profile_group, destination_group in EVIDENCE_GROUPS:
        raw_entries = _require_list(intake.get(profile_group, []), f"intake.{profile_group}")
        prepared[profile_group] = [
            _prepare_evidence(
                raw_entry,
                label=f"intake.{profile_group}[{index}]",
                intake_directory=intake_file.parent,
                derive_ratios=profile_group == "scale_references",
            )
            for index, raw_entry in enumerate(raw_entries)
        ]

    existing_prepared = _prepare_existing_evidence(existing, profile_directory)
    staging_directory = Path(tempfile.mkdtemp(prefix=f".{key}.staging-", dir=root))
    try:
        profile = _build_candidate_profile(
            intake,
            existing,
            key=key,
            stage_id=stage_id,
            subject_id=subject_id,
        )
        for profile_group, destination_group in EVIDENCE_GROUPS:
            merged = _merge_prepared_evidence(
                profile_group,
                existing_prepared[profile_group],
                prepared[profile_group],
            )
            profile[profile_group] = _store_evidence(
                merged,
                profile_directory=staging_directory,
                destination_directory=destination_group,
            )

        staging_profile_path = staging_directory / "profile.json"
        evaluated = _evaluate_profile(
            profile,
            expected_stage_id=stage_id,
            expected_subject_id=subject_id,
            profile_path=staging_profile_path,
        )
        profile["status"] = evaluated["status"]
        profile["missing_items"] = evaluated["missing_items"]
        profile["problems"] = evaluated["problems"]
        _atomic_write_text(
            staging_profile_path,
            json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        cleanup_warnings.extend(_commit_candidate_profile(root, key, staging_directory))
    except BaseException:
        if staging_directory.exists() or staging_directory.is_symlink():
            _cleanup_path(staging_directory)
        raise
    result = _check_profile_unrecovered(stage_id, subject_id, root)
    if cleanup_warnings:
        result["cleanup_warnings"] = cleanup_warnings
    return result


def _check_profile_unrecovered(
    stage_id: str,
    subject_id: str,
    root: Path,
) -> dict[str, Any]:
    key = profile_key(stage_id, subject_id)
    profile_directory = root / key
    profile_path = profile_directory / "profile.json"
    try:
        _assert_no_reparse_below(root, profile_directory)
        _assert_no_reparse_below(root, profile_path)
    except ValueError as error:
        return _base_result(
            key=key,
            stage_id=stage_id,
            subject_id=subject_id,
            status="incomplete",
            profile_path=profile_path,
            profile=None,
            missing_items=["profile_path"],
            problems=[f"unsafe_profile_path:{error}"],
        )
    if not profile_path.is_file():
        return _base_result(
            key=key,
            stage_id=stage_id,
            subject_id=subject_id,
            status="missing",
            profile_path=profile_path,
            profile=None,
            missing_items=["profile"],
        )
    try:
        raw_profile = _read_json(profile_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return _base_result(
            key=key,
            stage_id=stage_id,
            subject_id=subject_id,
            status="incomplete",
            profile_path=profile_path,
            profile=None,
            missing_items=["profile_json"],
            problems=[f"malformed_profile:{error}"],
        )
    if not isinstance(raw_profile, Mapping):
        return _base_result(
            key=key,
            stage_id=stage_id,
            subject_id=subject_id,
            status="incomplete",
            profile_path=profile_path,
            profile=None,
            missing_items=["profile_object"],
            problems=["malformed_profile:profile root must be an object"],
        )
    return _evaluate_profile(
        raw_profile,
        expected_stage_id=stage_id,
        expected_subject_id=subject_id,
        profile_path=profile_path,
    )


def check_profile(
    stage_id: str,
    subject_id: str,
    registry_root: str | os.PathLike[str] | None = None,
    *,
    _recover: bool = True,
) -> dict[str, Any]:
    """Check only the requested exact stage/subject key; never fall back."""

    key = profile_key(stage_id, subject_id)
    root = _registry_root(registry_root)
    cleanup_warnings = _recover_transactions(root, key) if _recover else []
    result = _check_profile_unrecovered(stage_id, subject_id, root)
    if cleanup_warnings:
        result["cleanup_warnings"] = cleanup_warnings
    return result


def _incomplete_catalog_entry(
    directory: Path, problem: str, missing_item: str = "profile_json"
) -> dict[str, Any]:
    return _base_result(
        key=directory.name,
        stage_id=None,
        subject_id=None,
        status="incomplete",
        profile_path=directory / "profile.json",
        profile=None,
        missing_items=[missing_item],
        problems=[problem],
    )


def _list_profiles_unrecovered(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    results: list[dict[str, Any]] = []
    candidates = sorted(
        (item for item in root.iterdir() if _looks_like_profile_key(item.name)),
        key=lambda path: path.name,
    )
    for directory in candidates:
        profile_path = directory / "profile.json"
        try:
            _assert_no_reparse_below(root, profile_path)
        except ValueError as error:
            results.append(
                _incomplete_catalog_entry(
                    directory,
                    f"unsafe_profile_path:{error}",
                    "profile_path",
                )
            )
            continue
        if not directory.is_dir():
            continue
        if not profile_path.is_file():
            results.append(_incomplete_catalog_entry(directory, "missing_profile_json", "profile"))
            continue
        try:
            raw_profile = _read_json(profile_path)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            results.append(_incomplete_catalog_entry(directory, f"malformed_profile:{error}"))
            continue
        if not isinstance(raw_profile, Mapping):
            results.append(
                _incomplete_catalog_entry(
                    directory,
                    "malformed_profile:profile root must be an object",
                    "profile_object",
                )
            )
            continue
        stage_id = raw_profile.get("stage_id")
        subject_id = raw_profile.get("subject_id")
        try:
            expected_key = profile_key(stage_id, subject_id)
        except ValueError as error:
            results.append(_incomplete_catalog_entry(directory, f"malformed_identity:{error}"))
            continue
        if expected_key != directory.name:
            results.append(_incomplete_catalog_entry(directory, "profile_identity_mismatch"))
            continue
        results.append(_check_profile_unrecovered(stage_id, subject_id, root))
    return results


def list_profiles(
    registry_root: str | os.PathLike[str] | None = None,
    *,
    _recover: bool = True,
) -> list[dict[str, Any]]:
    """List every stored profile in stable key order, including incomplete entries."""

    root = _registry_root(registry_root)
    cleanup_warnings = _recover_transactions(root) if _recover else []
    results = _list_profiles_unrecovered(root)
    if cleanup_warnings:
        for result in results:
            result["cleanup_warnings"] = list(cleanup_warnings)
    return results


def _catalog_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    profile = result.get("profile")
    profile = profile if isinstance(profile, Mapping) else {}
    return {
        "key": result["key"],
        "stage_id": result["stage_id"],
        "subject_id": result["subject_id"],
        "stage_name": profile.get("stage_name"),
        "subject_name": profile.get("subject_name"),
        "aliases": profile.get("aliases") if isinstance(profile.get("aliases"), list) else [],
        "detection_evidence": (
            profile.get("detection_evidence")
            if isinstance(profile.get("detection_evidence"), list)
            else []
        ),
        "counts": result["counts"],
        "status": result["status"],
        "missing_items": result["missing_items"],
        "problems": result["problems"],
    }


def _markdown_cell(value: Any) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("|", "\\|")
    )


def render_catalog(
    registry_root: str | os.PathLike[str] | None = None,
    *,
    _recover: bool = True,
) -> dict[str, Any]:
    """Refresh machine-readable and human-readable registry catalogs."""

    root = _registry_root(registry_root)
    root.mkdir(parents=True, exist_ok=True)
    cleanup_warnings = _recover_transactions(root) if _recover else []
    summaries = [_catalog_summary(result) for result in _list_profiles_unrecovered(root)]
    catalog = {"schema_version": SCHEMA_VERSION, "profiles": summaries}
    _atomic_write_text(
        root / "catalog.json",
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    lines = [
        "# 学科指向物档案",
        "",
        "| 档案键 | 学段学科 | 已确认 aliases | 素材数 | 合格比例参考数 | 状态 | 缺少项 / 问题 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        "| {key} | {display_name} | {aliases} | {assets} | {scales} | {status} | {details} |".format(
            key=_markdown_cell(item["key"]),
            display_name=_markdown_cell(
                (
                    f"{item['stage_name']}{item['subject_name']}"
                    if item["stage_name"] and item["subject_name"]
                    else item["key"]
                )
            ),
            aliases=_markdown_cell("、".join(item["aliases"]) or "-"),
            assets=item["counts"].get("assets", 0),
            scales=item["counts"].get("qualifying_scale_references", 0),
            status=_markdown_cell(STATUS_LABELS[item["status"]]),
            details=_markdown_cell("；".join([*item["missing_items"], *item["problems"]]) or "-"),
        )
        for item in summaries
    )
    _atomic_write_text(root / "catalog.md", "\n".join(lines) + "\n")
    if cleanup_warnings:
        catalog["cleanup_warnings"] = cleanup_warnings
    return catalog


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage exact pointer-profile profiles.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_parser = subparsers.add_parser("register", help="Register an intake JSON file.")
    register_parser.add_argument("--input", required=True, dest="intake_path")

    identity_parser = subparsers.add_parser(
        "identity", help="Create stable IDs from an explicitly named stage and subject."
    )
    identity_parser.add_argument("--stage-name", required=True)
    identity_parser.add_argument("--subject-name", required=True)
    identity_parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON.")

    check_parser = subparsers.add_parser("check", help="Check one exact profile.")
    check_parser.add_argument("--stage-id", required=True)
    check_parser.add_argument("--subject-id", required=True)

    migrate_parser = subparsers.add_parser(
        "migrate-hand-media",
        help="Derive missing hand media contracts from stored profile evidence.",
    )
    migrate_parser.add_argument("--stage-id", required=True)
    migrate_parser.add_argument("--subject-id", required=True)

    list_parser = subparsers.add_parser("list", help="List stored profiles.")
    validate_parser = subparsers.add_parser("validate", help="Validate all stored profiles.")
    for command_parser in (
        register_parser,
        check_parser,
        migrate_parser,
        list_parser,
        validate_parser,
    ):
        command_parser.add_argument(
            "--root", help="Registry root; defaults to target-local app data."
        )
        command_parser.add_argument(
            "--json", action="store_true", dest="as_json", help="Emit JSON."
        )
    return parser


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(payload, list):
        if not payload:
            print("No profiles registered.")
        for result in payload:
            print(f"{result['key']}: {result['status']}")
            if result.get("cleanup_warnings"):
                print("Cleanup warnings: " + "; ".join(result["cleanup_warnings"]))
        return
    if isinstance(payload, Mapping) and "key" in payload:
        print(f"{payload['key']}: {payload['status']}")
        if payload.get("missing_items"):
            print("Missing: " + ", ".join(payload["missing_items"]))
        if payload.get("cleanup_warnings"):
            print("Cleanup warnings: " + "; ".join(payload["cleanup_warnings"]))
        return
    print(payload)


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "identity":
            payload = manual_identity(args.stage_name, args.subject_name)
            exit_code = 0
        elif args.command == "register":
            payload = register_profile(args.intake_path, args.root)
            exit_code = 0
        elif args.command == "migrate-hand-media":
            payload = migrate_hand_media_contracts(
                args.stage_id,
                args.subject_id,
                args.root,
            )
            exit_code = 0 if payload["status"] == "ready" else 1
        elif args.command == "check":
            payload = check_profile(args.stage_id, args.subject_id, args.root)
            exit_code = 0 if payload["status"] == "ready" else 1
        else:
            payload = list_profiles(args.root)
            exit_code = 0
            if args.command == "validate":
                render_catalog(args.root)
                exit_code = (
                    0 if payload and all(item["status"] == "ready" for item in payload) else 1
                )
        _emit(payload, as_json=args.as_json)
        return exit_code
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        payload = {"status": "error", "error": str(error)}
        _emit(payload, as_json=args.as_json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
