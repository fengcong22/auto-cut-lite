"""Resolve main-video versus replacement-video clocks in review documents.

Document timestamps are only search hints.  This module gives each row an
explicit source clock and, when the mapping is provable, a global timeline
range.  It deliberately fails closed for an unanchored or ambiguous
replacement section instead of treating a local timestamp as a main-video
timestamp.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


_CLOCK = re.compile(r"(?<!\d)(\d{1,3})\s*[:：]\s*(\d{1,2}(?:\.\d+)?)(?!\d)")
_RANGE = re.compile(
    r"\s*(?:-|–|—|~|至|到|鈥搢鈥攟|\bto\b)\s*", re.IGNORECASE
)
_ANCHOR_HINTS = (
    "替换为以下视频",
    "替换为下面视频",
    "替换成以下视频",
    "以下补录视频",
)
_HANDOFF_HINTS = ("原视频", "主视频", "全片")
_HANDOFF_ACTIONS = ("直到", "延长至", "延长到", "回到", "切回", "恢复", "接回")
_ROLE_VALUES = {"main_video", "replacement_video"}


def _clock(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        result = math.nan
    if math.isfinite(result) and result >= 0:
        return result
    match = _CLOCK.fullmatch(text)
    if not match:
        return None
    minutes, seconds = float(match.group(1)), float(match.group(2))
    if seconds >= 60:
        return None
    return minutes * 60 + seconds


def _row_text(row: Mapping[str, Any]) -> str:
    for key in ("source_text", "text", "detail", "comment", "label"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _row_range(row: Mapping[str, Any]) -> tuple[float | None, float | None]:
    if isinstance(row.get("source_time_range"), Sequence) and not isinstance(
        row.get("source_time_range"), (str, bytes)
    ):
        values = list(row["source_time_range"])
        if len(values) == 2:
            return _clock(values[0]), _clock(values[1])
    start = _clock(row.get("start"))
    end = _clock(row.get("end"))
    if start is not None or end is not None:
        return start, end
    rough = row.get("rough_time")
    if isinstance(rough, Mapping):
        return _clock(rough.get("start")), _clock(rough.get("end"))
    if isinstance(rough, Sequence) and not isinstance(rough, (str, bytes)) and len(rough) == 2:
        return _clock(rough[0]), _clock(rough[1])
    if isinstance(rough, str):
        parts = _RANGE.split(rough.strip(), maxsplit=1)
        if len(parts) == 2:
            return _clock(parts[0]), _clock(parts[1])
    return None, None


def _first_clock(text: str, *, after: int = 0) -> float | None:
    match = _CLOCK.search(text, after)
    return _clock(match.group(0)) if match else None


def _handoff_time(text: str) -> float | None:
    """Find a main-video time in phrases such as '延长至原视频 09:42'."""
    folded = text.casefold()
    positions = [folded.find(hint.casefold()) for hint in _HANDOFF_HINTS]
    positions = [position for position in positions if position >= 0]
    for position in positions:
        context = text[max(0, position - 40) : position + 80]
        if not any(action in context for action in _HANDOFF_ACTIONS):
            continue
        matches = list(_CLOCK.finditer(context))
        if matches:
            # The first clock is normally the replacement-local range; the
            # main-video handoff is the final clock after "原视频".
            value = _clock(matches[-1].group(0))
            if value is not None:
                return value
    return None


def _is_anchor(text: str) -> bool:
    return any(hint in text for hint in _ANCHOR_HINTS)


def _anchor_id(row: Mapping[str, Any], index: int) -> str:
    for key in ("replacement_anchor_id", "anchor_id", "id", "item_id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return f"replacement_anchor_{index + 1:03d}"


def _number(value: Any) -> float | None:
    result = _clock(value)
    return result if result is not None else None


def _duration_from(value: Mapping[str, Any]) -> float | None:
    for key in (
        "replacement_duration_seconds",
        "replacement_video_duration_seconds",
        "replacement_duration",
        "duration_seconds",
        "duration",
    ):
        if key in value:
            result = _number(value.get(key))
            if result is not None:
                return result
    return None


def _declared_anchors(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = snapshot.get("replacement_anchors")
    if raw is None:
        raw = snapshot.get("replacement_sections")
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    result: list[dict[str, Any]] = []
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            continue
        row = copy.deepcopy(dict(value))
        row["id"] = str(row.get("id") or row.get("anchor_id") or f"replacement_anchor_{index + 1:03d}")
        result.append(row)
    return result


def resolve_review_timebases(
    rows: Sequence[Mapping[str, Any]],
    *,
    snapshot: Mapping[str, Any] | None = None,
    project: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[str], list[dict[str, Any]]]:
    """Return rows annotated with a closed, auditable timebase mapping.

    ``start``/``end`` are rewritten to global timeline seconds only when the
    mapping is resolved.  Replacement-local values remain in
    ``source_time_range``.  The returned unresolved IDs must not be executed.
    """
    snapshot = snapshot or {}
    project = project or {}
    media_duration = _number(project.get("media_duration_seconds"))
    anchors: list[dict[str, Any]] = []
    declared_rows = _declared_anchors(snapshot)
    declared_rows.extend(_declared_anchors(project))
    for declared in declared_rows:
        start = _number(declared.get("timeline_start", declared.get("start")))
        if start is None:
            continue
        declared["timeline_start"] = start
        declared["duration_seconds"] = _duration_from(declared)
        anchors.append(declared)

    anchor_by_id = {str(anchor["id"]): anchor for anchor in anchors}
    output: list[dict[str, Any]] = []
    warnings: list[str] = []
    unresolved: list[str] = []
    active: dict[str, Any] | None = None
    active_row_indexes: list[int] = []
    for index, original in enumerate(rows):
        row = copy.deepcopy(dict(original))
        item_id = str(row.get("id") or row.get("item_id") or f"index_{index + 1:03d}")
        text = _row_text(row)
        anchor_declared = _is_anchor(text)
        start, end = _row_range(row)

        if anchor_declared:
            explicit_anchor = row.get("replacement_anchor")
            data = dict(explicit_anchor) if isinstance(explicit_anchor, Mapping) else {}
            data.update({key: row[key] for key in ("replacement_duration_seconds", "replacement_video_duration_seconds", "replacement_duration") if key in row})
            anchor = {
                "id": _anchor_id(row, index),
                "timeline_start": _number(row.get("timeline_start", start)),
                "duration_seconds": _duration_from(data),
            }
            if anchor["timeline_start"] is None:
                anchor["timeline_start"] = start
            if anchor["timeline_start"] is None:
                warnings.append(f"{item_id}: replacement declaration has no main-video anchor time")
            else:
                anchors.append(anchor)
                active = anchor
                active_row_indexes = []
            row["source_role"] = "main_video"
            row["source_time_range"] = [start, end] if start is not None and end is not None else None
            row["timeline_time_range"] = [start, end] if start is not None and end is not None else None
            row["timebase"] = {"kind": "main_global", "offset_seconds": 0.0, "status": "resolved"}
            output.append(row)
            continue

        explicit_role = str(row.get("source_role") or "").strip().casefold()
        if explicit_role and explicit_role not in _ROLE_VALUES:
            warnings.append(f"{item_id}: unsupported source_role {explicit_role!r}")
            explicit_role = ""
        role = explicit_role or ("replacement_video" if active else "main_video")
        handoff = _handoff_time(text) if role == "replacement_video" else None
        if handoff is not None:
            row["handoff_to_main_time"] = handoff

        anchor = active if role == "replacement_video" else None
        if anchor is None and role == "replacement_video":
            requested_anchor = str(row.get("replacement_anchor_id") or "")
            anchor = anchor_by_id.get(requested_anchor)
        resolved = start is not None and end is not None
        timeline_range: list[float] | None = None
        status = "resolved"
        if start is not None and end is not None:
            row["source_time_range"] = [start, end]
        if role == "replacement_video":
            if anchor is None or anchor.get("timeline_start") is None:
                resolved = False
                status = "unresolved_no_anchor"
            elif handoff is not None and start is not None:
                # The local replacement clip runs until an explicitly stated
                # main-video handoff.  The handoff timestamp is global.
                timeline_range = [anchor["timeline_start"] + start, handoff]
                end = anchor.get("duration_seconds") or end or start
                resolved = True
            elif resolved:
                timeline_range = [anchor["timeline_start"] + start, anchor["timeline_start"] + end]
            else:
                status = "unresolved_missing_local_range"
            if anchor and anchor.get("duration_seconds") is not None and end is not None:
                if end > float(anchor["duration_seconds"]) + 1e-6:
                    resolved = False
                    status = "unresolved_out_of_replacement_range"
            if handoff is not None and media_duration is not None and handoff > media_duration + 1e-6:
                resolved = False
                status = "unresolved_handoff_out_of_main_range"
            if resolved and timeline_range is None:
                resolved = False
        else:
            if resolved:
                timeline_range = [start, end]
            elif start is not None and end is None:
                status = "unresolved_missing_end"
            else:
                status = "unresolved_missing_main_range"

        if resolved and timeline_range:
            row["source_time_range"] = [start, end]
            row["timeline_time_range"] = [round(timeline_range[0], 6), round(timeline_range[1], 6)]
            row["start"], row["end"] = row["timeline_time_range"]
            row["timebase"] = {
                "kind": "replacement_local" if role == "replacement_video" else "main_global",
                "offset_seconds": round((anchor or {}).get("timeline_start", 0.0) if role == "replacement_video" else 0.0, 6),
                "status": "resolved",
            }
            evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
            evidence = copy.deepcopy(dict(evidence))
            evidence["timebase"] = copy.deepcopy(row["timebase"])
            evidence["source_role"] = role
            evidence["source_time_range"] = list(row["source_time_range"])
            evidence["timeline_time_range"] = list(row["timeline_time_range"])
            evidence["review_timestamp_role"] = "search_hint"
            row["evidence"] = evidence
            row["source_role"] = role
            if anchor:
                row["replacement_anchor_id"] = str(anchor["id"])
        else:
            row["source_role"] = role
            row["timebase"] = {
                "kind": "replacement_local" if role == "replacement_video" else "main_global",
                "offset_seconds": round((anchor or {}).get("timeline_start", 0.0) if role == "replacement_video" else 0.0, 6),
                "status": status,
            }
            evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
            evidence = copy.deepcopy(dict(evidence))
            evidence["timebase"] = copy.deepcopy(row["timebase"])
            evidence["source_role"] = role
            if start is not None and end is not None:
                evidence["source_time_range"] = [start, end]
            evidence["timebase_status"] = status
            row["evidence"] = evidence
            # Never leave a replacement-local start/end in the canonical row:
            # downstream consumers must not be able to mistake it for the
            # main-video clock.  The local range remains as evidence.
            row.pop("start", None)
            row.pop("end", None)
            row.pop("timeline_time_range", None)
            unresolved.append(item_id)
            warnings.append(f"{item_id}: timebase mapping is {status}; no executable global range was inferred")
            if anchor:
                row["replacement_anchor_id"] = str(anchor["id"])
        output.append(row)
        if role == "replacement_video" and active is not None:
            active_row_indexes.append(len(output) - 1)

        if role == "replacement_video" and handoff is not None:
            active = None
            active_row_indexes = []
        elif explicit_role == "main_video":
            active = None
            active_row_indexes = []

    if active is not None and active.get("duration_seconds") is None and active_row_indexes:
        # Without either a replacement duration or an explicit return to the
        # main clock, the section boundary is unknowable.  Downgrade every
        # affected row to an auditable review item rather than guessing.
        for row_index in active_row_indexes:
            row = output[row_index]
            item_id = str(row.get("id") or f"index_{row_index + 1:03d}")
            status = "unresolved_missing_replacement_boundary"
            row["timebase"] = {
                "kind": "replacement_local",
                "offset_seconds": row.get("timebase", {}).get("offset_seconds", 0.0),
                "status": status,
            }
            evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
            evidence = copy.deepcopy(dict(evidence))
            evidence["timebase"] = copy.deepcopy(row["timebase"])
            evidence["timebase_status"] = status
            row["evidence"] = evidence
            row.pop("start", None)
            row.pop("end", None)
            row.pop("timeline_time_range", None)
            unresolved.append(item_id)
            warnings.append(
                f"{item_id}: replacement section has no duration or explicit main-video handoff"
            )

    return output, warnings, list(dict.fromkeys(unresolved)), anchors


__all__ = ["resolve_review_timebases"]
