from __future__ import annotations

import json
import os
import re
from typing import Any

SMOKE_TEXT = "Codex smoke"
SMOKE_DURATION_US = 1_000_000

_BOOLEAN_RECEIPT_FIELDS = (
    "editable",
    "track_verified",
    "segment_verified",
    "material_link_verified",
    "smoke_text_verified",
    "timerange_verified",
    "metadata_verified",
)
_COUNT_RECEIPT_FIELDS = (
    "text_track_count",
    "text_segment_count",
    "text_material_count",
    "linked_text_segment_count",
)
_REQUIRED_SELF_CHECKS = frozenset(
    {
        "environment_detected",
        "resources_resolved",
        "draft_root_available",
        "smoke_test",
        "live_app_attach",
    }
)
_PREREQUISITE_BLOCKED_CHECKS = frozenset(
    {"environment_detected", "resources_resolved", "draft_root_available"}
)
_DRAFT_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _material_text(material: dict[str, Any]) -> str:
    payload = material.get("content")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return ""
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def _same_absolute_path(value: object, expected: str) -> bool:
    return (
        isinstance(value, str)
        and os.path.isabs(value)
        and os.path.normcase(os.path.abspath(value)) == os.path.normcase(os.path.abspath(expected))
    )


def build_smoke_editability_receipt(
    content: object,
    meta_info: object,
    *,
    draft_name: str,
    expected_root_path: str,
    expected_draft_path: str,
) -> dict[str, object]:
    content_row = content if isinstance(content, dict) else {}
    meta_row = meta_info if isinstance(meta_info, dict) else {}
    text_tracks = [row for row in _rows(content_row.get("tracks")) if row.get("type") == "text"]
    text_segments = [segment for track in text_tracks for segment in _rows(track.get("segments"))]
    materials = content_row.get("materials")
    text_materials = _rows(materials.get("texts")) if isinstance(materials, dict) else []
    materials_by_id = {
        material["id"]: material
        for material in text_materials
        if _nonempty_string(material.get("id"))
    }
    linked_segments = [
        segment for segment in text_segments if segment.get("material_id") in materials_by_id
    ]

    track_verified = len(text_tracks) == 1 and _nonempty_string(text_tracks[0].get("id"))
    segment_verified = len(text_segments) == 1 and _nonempty_string(text_segments[0].get("id"))
    material_link_verified = len(linked_segments) == 1
    smoke_text_verified = material_link_verified and all(
        _material_text(materials_by_id[segment["material_id"]]) == SMOKE_TEXT
        for segment in linked_segments
    )
    timerange = text_segments[0].get("target_timerange") if len(text_segments) == 1 else None
    timerange_verified = (
        isinstance(timerange, dict)
        and set(timerange) == {"start", "duration"}
        and type(timerange.get("start")) is int
        and timerange.get("start") == 0
        and type(timerange.get("duration")) is int
        and timerange.get("duration") == SMOKE_DURATION_US
    )
    metadata_verified = (
        meta_row.get("draft_name") == draft_name
        and isinstance(meta_row.get("draft_id"), str)
        and _DRAFT_ID_PATTERN.fullmatch(meta_row["draft_id"]) is not None
        and _same_absolute_path(meta_row.get("draft_root_path"), expected_root_path)
        and _same_absolute_path(meta_row.get("draft_fold_path"), expected_draft_path)
    )
    editable = (
        len(text_tracks) == 1
        and len(text_segments) == 1
        and len(text_materials) == 1
        and len(linked_segments) == 1
        and track_verified
        and segment_verified
        and material_link_verified
        and smoke_text_verified
        and timerange_verified
        and metadata_verified
    )
    return {
        "schema_version": 1,
        "editable": editable,
        "track_verified": track_verified,
        "segment_verified": segment_verified,
        "material_link_verified": material_link_verified,
        "smoke_text_verified": smoke_text_verified,
        "timerange_verified": timerange_verified,
        "metadata_verified": metadata_verified,
        "text_track_count": len(text_tracks),
        "text_segment_count": len(text_segments),
        "text_material_count": len(text_materials),
        "linked_text_segment_count": len(linked_segments),
    }


def smoke_editability_receipt_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    schema_version = value.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        return False
    if schema_version != 1:
        return False
    if any(value.get(field) is not True for field in _BOOLEAN_RECEIPT_FIELDS):
        return False
    return all(
        isinstance(value.get(field), int)
        and not isinstance(value.get(field), bool)
        and value.get(field) == 1
        for field in _COUNT_RECEIPT_FIELDS
    )


def blocked_jianying_checks_valid(value: object) -> bool:
    if not isinstance(value, dict) or not _REQUIRED_SELF_CHECKS <= set(value):
        return False
    rows = {check_id: value.get(check_id) for check_id in _REQUIRED_SELF_CHECKS}
    if any(not isinstance(row, dict) or type(row.get("ok")) is not bool for row in rows.values()):
        return False
    return any(rows[check_id]["ok"] is False for check_id in _PREREQUISITE_BLOCKED_CHECKS)
