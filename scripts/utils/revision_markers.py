import json
import math
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from utils.revision_models import (
    RevisionRequest,
    RevisionReviewItem,
    _collect_delete_windows,
    lite_pause_change_is_label_only,
    lite_review_item_execution_status,
    lite_timing_source,
    lite_unresolved_timebase_status,
    review_item_execution_status,
)

_REVIEW_ID_PATTERN = re.compile(r"(修改|校对)\s*0*(\d+)", re.IGNORECASE)
_UNVERIFIED_SOURCE = "unverified_source_unavailable"
_UNVERIFIED_TIMING = "unverified_timing_unavailable"
_SAVED_START_TOLERANCE_US = 500
_INTERNAL_LEGACY_SOURCES = {"legacy_marker", "legacy_edit"}
_ASR_RECEIPT_PASS_STATUSES = {"pass", "passed", "ok", "validated", "complete", "completed"}
_ASR_RECEIPT_GRANULARITIES = {"word", "character", "word_character", "word+character"}
_SHA256_HEX_LENGTH = 64


@dataclass(frozen=True)
class MarkerPlanItem:
    item_id: str
    source_text: str
    start: float
    end: float
    verbatim_status: str
    source: str = ""
    kind: str = "review_only"
    execution_status: str = ""


def _execution_status_from_source_item(source_item: RevisionReviewItem) -> str:
    return review_item_execution_status(source_item)


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().casefold()
    return len(text) == _SHA256_HEX_LENGTH and all(
        character in "0123456789abcdef" for character in text
    )


def _label_only_asr_marker_time(source_item: RevisionReviewItem) -> Optional[float]:
    """Return a validated item-level ASR marker point when one is present.

    Older compiled requests may carry the receipt on a now-filtered edit.  This
    helper deliberately returns ``None`` when the item has no item-level receipt
    so that compatibility path remains available.  Once an item-level receipt
    is supplied, however, it is authoritative and must be complete.
    """

    execution_status = _execution_status_from_source_item(source_item).casefold()
    if not execution_status.startswith("label_only_") or (
        lite_timing_source(source_item.kind, source_item.source_text) != "asr"
    ):
        return None
    evidence = source_item.evidence if isinstance(source_item.evidence, Mapping) else {}
    if str(evidence.get("timing_source") or "").strip().casefold() == (
        "review_timestamp_fallback"
    ):
        if (
            str(evidence.get("review_timestamp_role") or "").strip().casefold()
            != "authoritative_fallback"
        ):
            raise ValueError(
                f"Lite label-only audio item {source_item.item_id} has invalid review "
                "timestamp fallback role."
            )
        try:
            fallback_time = float(evidence.get("resolved_time"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"Lite label-only audio item {source_item.item_id} has no valid review "
                "timestamp fallback."
            ) from exc
        if not math.isfinite(fallback_time) or fallback_time < 0.0:
            raise ValueError(
                f"Lite label-only audio item {source_item.item_id} has no valid review "
                "timestamp fallback."
            )
        return fallback_time
    alignment = evidence.get("asr_alignment")
    if alignment is None:
        return None

    problems: List[str] = []
    if not isinstance(alignment, Mapping):
        problems.append("asr_alignment receipt must be an object")
        alignment = {}
    if str(evidence.get("review_timestamp_role") or "").strip().casefold() != "search_hint":
        problems.append("review_timestamp_role must be search_hint")
    if str(alignment.get("status") or "").strip().casefold() not in _ASR_RECEIPT_PASS_STATUSES:
        problems.append("asr_alignment.status is not pass")
    if (
        str(alignment.get("granularity") or "").strip().casefold()
        not in _ASR_RECEIPT_GRANULARITIES
    ):
        problems.append("asr_alignment.granularity must be word or character")
    if not str(alignment.get("provider") or "").strip():
        problems.append("asr_alignment.provider is missing")
    if not str(alignment.get("model") or alignment.get("resource_id") or "").strip():
        problems.append("asr_alignment model/resource_id is missing")
    if not str(alignment.get("adapter_version") or "").strip():
        problems.append("asr_alignment.adapter_version is missing")
    if not _is_sha256(
        alignment.get("input_sha256") or alignment.get("source_audio_sha256")
    ):
        problems.append("asr_alignment source audio SHA-256 is missing or invalid")
    if alignment.get("authoritative_cut_boundary") is not False:
        problems.append("label-only asr_alignment.authoritative_cut_boundary must be false")

    matches = alignment.get("matches") or alignment.get("words")
    if not isinstance(matches, list) or not matches:
        problems.append("asr_alignment word/character matches are missing")
    else:
        previous_start = -math.inf
        for index, row in enumerate(matches):
            if not isinstance(row, Mapping) or not str(row.get("text") or "").strip():
                problems.append(f"asr_alignment match {index + 1} has no text")
                continue
            try:
                match_start = float(row.get("start"))
                match_end = float(row.get("end"))
            except (TypeError, ValueError, OverflowError):
                problems.append(f"asr_alignment match {index + 1} has invalid timing")
                continue
            if (
                not math.isfinite(match_start)
                or not math.isfinite(match_end)
                or match_start < 0.0
                or match_end <= match_start
                or match_start < previous_start
            ):
                problems.append(
                    f"asr_alignment match {index + 1} is not a positive ordered interval"
                )
            previous_start = match_start

    resolved_time: Optional[float] = None
    try:
        resolved_time = float(alignment.get("resolved_time"))
    except (TypeError, ValueError, OverflowError):
        problems.append("asr_alignment.resolved_time is missing or invalid")
    else:
        if not math.isfinite(resolved_time) or resolved_time < 0.0:
            problems.append("asr_alignment.resolved_time is missing or invalid")
        evidence_time = evidence.get("resolved_time")
        if evidence_time is not None:
            try:
                if abs(float(evidence_time) - resolved_time) > 0.001:
                    problems.append("evidence.resolved_time does not match asr_alignment")
            except (TypeError, ValueError, OverflowError):
                problems.append("evidence.resolved_time is invalid")

    if problems:
        raise ValueError(
            f"Lite label-only audio item {source_item.item_id} has invalid ASR receipt: "
            + "; ".join(problems)
            + "."
        )
    return resolved_time


def lite_unresolved_timebase_marker_time(source_item: RevisionReviewItem) -> Optional[float]:
    """Map an unresolved Lite item from its review timestamp, never its ASR match."""

    status = lite_unresolved_timebase_status(source_item)
    if not status:
        return None
    evidence = source_item.evidence if isinstance(source_item.evidence, Mapping) else {}
    timebase = evidence.get("timebase")
    if not isinstance(timebase, Mapping):
        raise ValueError(
            f"Lite unresolved review item {source_item.item_id} has no timebase evidence."
        )

    timestamp_role = str(evidence.get("review_timestamp_role") or "").strip().casefold()
    timestamp_parse = str(evidence.get("review_timestamp_parse") or "").strip().casefold()
    raw_hint = evidence.get("review_search_hint_seconds")
    if raw_hint is None:
        source_range = evidence.get("source_time_range")
        if isinstance(source_range, (list, tuple)) and source_range:
            raw_hint = source_range[0]
    if (
        timestamp_role not in {"search_hint", "authoritative_fallback"}
        or timestamp_parse not in {"point", "range"}
        or raw_hint is None
    ):
        raise ValueError(
            f"Lite unresolved review item {source_item.item_id} has no reliable review timestamp."
        )
    try:
        review_time = float(raw_hint)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"Lite unresolved review item {source_item.item_id} has no reliable review timestamp."
        ) from exc
    if not math.isfinite(review_time) or review_time < 0.0:
        raise ValueError(
            f"Lite unresolved review item {source_item.item_id} has no reliable review timestamp."
        )

    kind = str(timebase.get("kind") or "").strip().casefold()
    if kind == "main_global":
        return review_time
    if kind != "replacement_local" or status == "unresolved_no_anchor":
        raise ValueError(
            f"Lite unresolved review item {source_item.item_id} has no reliable replacement offset."
        )
    if "offset_seconds" not in timebase:
        raise ValueError(
            f"Lite unresolved review item {source_item.item_id} has no reliable replacement offset."
        )
    try:
        offset = float(timebase.get("offset_seconds"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"Lite unresolved review item {source_item.item_id} has no reliable replacement offset."
        ) from exc
    marker_time = review_time + offset
    if not math.isfinite(offset) or offset < 0.0 or not math.isfinite(marker_time):
        raise ValueError(
            f"Lite unresolved review item {source_item.item_id} has no reliable replacement offset."
        )
    return marker_time


def _deleted_duration_before(point: float, delete_windows: List[List[float]]) -> float:
    deleted_duration = 0.0
    for delete_start, delete_end in delete_windows:
        if point <= delete_start:
            break
        deleted_duration += min(point, delete_end) - delete_start
        if point <= delete_end:
            break
    return max(0.0, deleted_duration)


def _inserted_duration_before(point: float, request: RevisionRequest) -> float:
    return sum(
        item.duration
        for item in request.pause_adjustments
        if item.source_time < float(point) - 0.000001
    )


def map_marker_plan_to_timeline(
    plan: Sequence[MarkerPlanItem], request: RevisionRequest
) -> List[MarkerPlanItem]:
    # Lite never compresses deletion windows or executes pause-duration changes.
    # Marker starts therefore remain on their authoritative source-time boundary.
    if str(getattr(request, "workflow_mode", "") or "").strip().casefold() == "lite":
        mapped_plan: List[MarkerPlanItem] = []
        for item in plan:
            mapped_start = max(0.0, item.start)
            mapped_plan.append(
                replace(
                    item,
                    start=mapped_start,
                    end=mapped_start + max(0.0, item.end - item.start),
                )
            )
        return mapped_plan

    delete_windows = _collect_delete_windows(request)
    mapped_plan: List[MarkerPlanItem] = []
    for item in plan:
        mapped_start = max(
            0.0,
            item.start
            - _deleted_duration_before(item.start, delete_windows)
            + _inserted_duration_before(item.start, request),
        )
        mapped_end = max(
            0.0,
            item.end
            - _deleted_duration_before(item.end, delete_windows)
            + _inserted_duration_before(item.end, request),
        )
        mapped_plan.append(replace(item, start=mapped_start, end=mapped_end))
    return mapped_plan


def _is_review_marker_track(name: Any) -> bool:
    normalized = str(name or "").strip().casefold()
    return normalized.startswith("review marker") or normalized.startswith("校对标记")


def _text_material_value(material: Dict[str, Any]) -> Tuple[str, str]:
    raw_content = material.get("content")
    if isinstance(raw_content, dict):
        if "text" not in raw_content:
            return "", "text material content has no text field"
        return str(raw_content.get("text") or ""), ""
    try:
        parsed = json.loads(str(raw_content or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", "text material content is not readable JSON"
    if not isinstance(parsed, dict) or "text" not in parsed:
        return "", "text material content has no text field"
    return str(parsed.get("text") or ""), ""


def _collect_saved_markers(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    text_materials: Dict[str, Dict[str, Any]] = {}
    for material in (content.get("materials") or {}).get("texts", []) or []:
        if not isinstance(material, dict):
            continue
        material_id = str(material.get("id") or material.get("material_id") or "")
        if material_id:
            text_materials[material_id] = material

    saved_markers: List[Dict[str, Any]] = []
    for track in content.get("tracks") or []:
        if not isinstance(track, dict) or not _is_review_marker_track(track.get("name")):
            continue
        track_name = str(track.get("name") or "")
        for segment in track.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            segment_id = str(segment.get("id") or segment.get("segment_id") or "")
            material_id = str(segment.get("material_id") or "")
            material = text_materials.get(material_id)
            if material is None:
                text = ""
                text_error = f"text material {material_id or '<missing>'} is missing"
            else:
                text, text_error = _text_material_value(material)
            target_timerange = segment.get("target_timerange") or {}
            saved_markers.append(
                {
                    "segment_id": segment_id,
                    "material_id": material_id,
                    "track_name": track_name,
                    "start": target_timerange.get("start", 0),
                    "text": text,
                    "_text_error": text_error,
                }
            )
    return saved_markers


def _receipt_field(receipt: Any, field_name: str) -> str:
    if isinstance(receipt, dict):
        value = receipt.get(field_name)
    else:
        value = getattr(receipt, field_name, None)
    return "" if value is None else str(value)


def _verbatim_is_verified(item: MarkerPlanItem) -> bool:
    status = str(item.verbatim_status or "").strip().casefold()
    return status in {"verified", _UNVERIFIED_TIMING}


def _timing_is_verified(item: MarkerPlanItem) -> bool:
    status = str(item.verbatim_status or "").strip().casefold()
    return status == "verified"


def _is_source_ledger_plan(plan: Sequence[MarkerPlanItem]) -> bool:
    return bool(plan) and any(
        str(item.source or "").strip() not in _INTERNAL_LEGACY_SOURCES for item in plan
    )


def _plan_start_us(item: MarkerPlanItem) -> Optional[int]:
    try:
        start_seconds = float(item.start)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(start_seconds):
        return None
    return int(round(start_seconds * 1_000_000))


def _saved_start_us(saved: Dict[str, Any]) -> Optional[int]:
    try:
        start_us = float(saved.get("start"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(start_us):
        return None
    return int(round(start_us))


def _record_text_mismatch(
    item: MarkerPlanItem,
    actual_text: str,
    *,
    errors: List[str],
    warnings: List[str],
) -> None:
    message = (
        f"Review marker {item.item_id} is not verbatim: expected {item.source_text!r}, "
        f"saved {actual_text!r}."
    )
    if _verbatim_is_verified(item):
        errors.append(message)
    else:
        warnings.append(f"Unverified marker text mismatch: {message}")


def _record_timing_mismatch(
    item: MarkerPlanItem,
    expected_start_us: Optional[int],
    actual_start_us: Optional[int],
    *,
    errors: List[str],
    warnings: List[str],
) -> None:
    message = (
        f"Review marker {item.item_id} timeline start mismatch: "
        f"expected {expected_start_us!r}us, saved {actual_start_us!r}us."
    )
    if _timing_is_verified(item):
        errors.append(message)
    else:
        warnings.append(f"Unverified marker timing mismatch: {message}")


def validate_saved_marker_plan(
    plan: Sequence[MarkerPlanItem],
    content: Dict[str, Any],
    receipts: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Verify saved review-marker materials against the source-ledger marker plan.

    ``receipts=None`` preserves legacy ambiguity for an empty plan. An explicit
    empty receipt sequence declares that empty plan authoritative.
    """

    plan = list(plan)
    receipts_provided = receipts is not None
    receipt_items = list(receipts or [])
    saved_markers = _collect_saved_markers(content)
    source_ledger = _is_source_ledger_plan(plan) or (receipts_provided and not plan)
    errors: List[str] = []
    warnings: List[str] = []
    exact_material_ids: List[str] = []
    exact_text_values: List[str] = []
    exact_pair_match_count = 0
    mismatched_item_ids: List[str] = []
    timing_mismatched_item_ids: List[str] = []

    for item in plan:
        if _verbatim_is_verified(item):
            if not _timing_is_verified(item):
                status = str(item.verbatim_status or "").strip() or "<empty>"
                warnings.append(
                    f"Review marker {item.item_id} has unverified timing status {status!r}."
                )
            continue
        status = str(item.verbatim_status or "").strip() or "<empty>"
        warnings.append(f"Review marker {item.item_id} has unverified verbatim_status {status!r}.")

    if source_ledger and len(saved_markers) != len(plan):
        errors.append(
            "Review marker count mismatch for source ledger: "
            f"expected {len(plan)}, found {len(saved_markers)}."
        )

    public_saved_markers = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in saved_markers
    ]

    if receipts_provided:
        plan_by_id = {item.item_id: item for item in plan}
        if len(plan_by_id) != len(plan):
            errors.append("Marker plan contains duplicate item_id values.")

        receipt_by_id: Dict[str, Any] = {}
        for receipt in receipt_items:
            item_id = _receipt_field(receipt, "item_id")
            if not item_id:
                errors.append("Review marker receipt is missing item_id.")
                continue
            if item_id in receipt_by_id:
                errors.append(f"Review marker receipt has duplicate item_id: {item_id}.")
                continue
            receipt_by_id[item_id] = receipt
            if item_id not in plan_by_id:
                errors.append(f"Review marker receipt has unexpected item_id: {item_id}.")

        for item in plan:
            if item.item_id not in receipt_by_id:
                errors.append(f"Review marker receipt is missing item_id: {item.item_id}.")

        saved_by_segment: Dict[str, Dict[str, Any]] = {}
        for saved in saved_markers:
            segment_id = saved["segment_id"]
            if not segment_id:
                errors.append("Saved review marker is missing segment_id.")
            elif segment_id in saved_by_segment:
                errors.append(f"Saved review markers contain duplicate segment_id: {segment_id}.")
            else:
                saved_by_segment[segment_id] = saved

        referenced_segment_ids: set[str] = set()
        for item in plan:
            receipt = receipt_by_id.get(item.item_id)
            if receipt is None:
                continue
            segment_id = _receipt_field(receipt, "segment_id")
            material_id = _receipt_field(receipt, "material_id")
            track_name = _receipt_field(receipt, "track_name")
            for field_name, field_value in (
                ("segment_id", segment_id),
                ("material_id", material_id),
                ("track_name", track_name),
            ):
                if not field_value:
                    errors.append(f"Review marker receipt {item.item_id} is missing {field_name}.")
            if source_ledger and _receipt_field(receipt, "source_text") != item.source_text:
                errors.append(
                    f"Review marker receipt {item.item_id} source_text does not match the marker plan."
                )
            saved = saved_by_segment.get(segment_id)
            if saved is None:
                errors.append(
                    f"Review marker receipt {item.item_id} segment_id {segment_id!r} "
                    "does not exist on a marker track."
                )
                continue
            referenced_segment_ids.add(segment_id)
            if saved["material_id"] != material_id:
                errors.append(
                    f"Review marker receipt {item.item_id} material_id mismatch: "
                    f"receipt {material_id!r}, saved {saved['material_id']!r}."
                )
            if saved["track_name"] != track_name:
                errors.append(
                    f"Review marker receipt {item.item_id} track_name mismatch: "
                    f"receipt {track_name!r}, saved {saved['track_name']!r}."
                )
            if saved["_text_error"]:
                errors.append(
                    f"Review marker receipt {item.item_id} cannot resolve saved text: "
                    f"{saved['_text_error']}."
                )
                continue
            text_matches = saved["text"] == item.source_text
            if source_ledger and not text_matches:
                mismatched_item_ids.append(item.item_id)
                _record_text_mismatch(item, saved["text"], errors=errors, warnings=warnings)
            if text_matches:
                exact_material_ids.append(saved["material_id"])
                exact_text_values.append(saved["text"])

            expected_start_us = _plan_start_us(item)
            actual_start_us = _saved_start_us(saved)
            timing_matches = (
                expected_start_us is not None
                and actual_start_us is not None
                and abs(actual_start_us - expected_start_us) <= _SAVED_START_TOLERANCE_US
            )
            if not timing_matches:
                timing_mismatched_item_ids.append(item.item_id)
                _record_timing_mismatch(
                    item,
                    expected_start_us,
                    actual_start_us,
                    errors=errors,
                    warnings=warnings,
                )
            elif text_matches:
                exact_pair_match_count += 1

        if source_ledger:
            extra_segment_ids = [
                saved["segment_id"]
                for saved in saved_markers
                if saved["segment_id"] not in referenced_segment_ids
            ]
            if extra_segment_ids:
                errors.append(
                    "Saved review markers contain extra receipt-unbound segment_id values: "
                    + ", ".join(extra_segment_ids)
                    + "."
                )
    elif source_ledger:
        saved_by_text: Dict[str, List[Tuple[int, Dict[str, Any], Optional[int]]]] = {}
        valid_saved_indices: set[int] = set()
        for saved_index, saved in enumerate(saved_markers):
            if saved["_text_error"]:
                errors.append(
                    f"Saved review marker {saved['segment_id'] or '<missing>'} cannot resolve text: "
                    f"{saved['_text_error']}."
                )
                continue
            valid_saved_indices.add(saved_index)
            saved_by_text.setdefault(saved["text"], []).append(
                (saved_index, saved, _saved_start_us(saved))
            )

        plan_by_text: Dict[str, List[Tuple[int, MarkerPlanItem, Optional[int]]]] = {}
        for plan_index, item in enumerate(plan):
            plan_by_text.setdefault(item.source_text, []).append(
                (plan_index, item, _plan_start_us(item))
            )

        matched_plan_indices: set[int] = set()
        matched_saved_indices: set[int] = set()
        for source_text, planned_group in plan_by_text.items():
            saved_group = saved_by_text.get(source_text, [])
            ordered_plan = sorted(
                planned_group,
                key=lambda entry: (
                    entry[2] is None,
                    entry[2] if entry[2] is not None else 0,
                    entry[0],
                ),
            )
            ordered_saved = sorted(
                saved_group,
                key=lambda entry: (
                    entry[2] is None,
                    entry[2] if entry[2] is not None else 0,
                    entry[0],
                ),
            )
            plan_pos = 0
            saved_pos = 0
            while plan_pos < len(ordered_plan) and saved_pos < len(ordered_saved):
                plan_index, _item, expected_start_us = ordered_plan[plan_pos]
                saved_index, saved, actual_start_us = ordered_saved[saved_pos]
                if expected_start_us is None:
                    plan_pos += 1
                    continue
                if actual_start_us is None:
                    saved_pos += 1
                    continue
                delta_us = actual_start_us - expected_start_us
                if abs(delta_us) <= _SAVED_START_TOLERANCE_US:
                    matched_plan_indices.add(plan_index)
                    matched_saved_indices.add(saved_index)
                    exact_material_ids.append(saved["material_id"])
                    exact_text_values.append(saved["text"])
                    exact_pair_match_count += 1
                    plan_pos += 1
                    saved_pos += 1
                elif delta_us < -_SAVED_START_TOLERANCE_US:
                    saved_pos += 1
                else:
                    plan_pos += 1

        for source_text, planned_group in plan_by_text.items():
            remaining_plan = sorted(
                (entry for entry in planned_group if entry[0] not in matched_plan_indices),
                key=lambda entry: (
                    entry[2] is None,
                    entry[2] if entry[2] is not None else 0,
                    entry[0],
                ),
            )
            remaining_saved = sorted(
                (
                    entry
                    for entry in saved_by_text.get(source_text, [])
                    if entry[0] not in matched_saved_indices
                ),
                key=lambda entry: (
                    entry[2] is None,
                    entry[2] if entry[2] is not None else 0,
                    entry[0],
                ),
            )
            for planned_entry, saved_entry in zip(remaining_plan, remaining_saved):
                plan_index, item, expected_start_us = planned_entry
                saved_index, saved, actual_start_us = saved_entry
                matched_plan_indices.add(plan_index)
                matched_saved_indices.add(saved_index)
                exact_material_ids.append(saved["material_id"])
                exact_text_values.append(saved["text"])
                timing_mismatched_item_ids.append(item.item_id)
                _record_timing_mismatch(
                    item,
                    expected_start_us,
                    actual_start_us,
                    errors=errors,
                    warnings=warnings,
                )

        unmatched_plan = [
            item for index, item in enumerate(plan) if index not in matched_plan_indices
        ]
        unmatched_saved = [
            saved_markers[index] for index in sorted(valid_saved_indices - matched_saved_indices)
        ]
        for index, item in enumerate(unmatched_plan):
            actual_text = (
                unmatched_saved[index]["text"] if index < len(unmatched_saved) else "<missing>"
            )
            mismatched_item_ids.append(item.item_id)
            _record_text_mismatch(item, actual_text, errors=errors, warnings=warnings)

    metrics = {
        "source_ledger": source_ledger,
        "expected_count": len(plan),
        "actual_count": len(saved_markers),
        "receipt_count": len(receipt_items),
        "saved_markers": public_saved_markers,
        "exact_match_count": exact_pair_match_count,
        "verbatim_match_count": len(exact_material_ids),
        "exact_marker_material_ids": exact_material_ids,
        "exact_marker_text_values": exact_text_values,
        "mismatched_item_ids": mismatched_item_ids,
        "timing_mismatched_item_ids": timing_mismatched_item_ids,
    }
    return {"ok": not errors, "errors": errors, "warnings": warnings, "metrics": metrics}


def _normalize_window(start: float, end: float) -> Tuple[float, float]:
    normalized_start = float(start)
    if not math.isfinite(normalized_start):
        normalized_start = 0.0

    normalized_end = float(end)
    if not math.isfinite(normalized_end):
        normalized_end = normalized_start + 0.8

    normalized_start, normalized_end = sorted((normalized_start, normalized_end))
    normalized_start = max(0.0, normalized_start)
    normalized_end = max(0.0, normalized_end)
    if math.isclose(normalized_start, normalized_end, rel_tol=0.0, abs_tol=1e-9):
        normalized_end = normalized_start + 0.8
    return normalized_start, normalized_end


def _normalize_item_id(value: str) -> str:
    raw = str(value or "").strip()
    match = _REVIEW_ID_PATTERN.search(raw)
    if match:
        return f"{match.group(1)}{int(match.group(2))}"
    return raw.casefold()


def _extract_action_item_id(action: Any) -> str:
    explicit_id = str(getattr(action, "doc_item_id", "") or "").strip()
    if explicit_id:
        return explicit_id
    action_text = f"{getattr(action, 'label', '')} {getattr(action, 'detail', '')}"
    match = _REVIEW_ID_PATTERN.search(action_text)
    if not match:
        return ""
    return f"{match.group(1)}{int(match.group(2))}"


def _build_action_index(request: RevisionRequest) -> Dict[str, List[Any]]:
    action_index: Dict[str, List[Any]] = {}
    for actions in (request.edits, request.markers):
        for action in actions:
            item_id = _extract_action_item_id(action)
            if not item_id:
                continue
            normalized_item_id = _normalize_item_id(item_id)
            action_index.setdefault(normalized_item_id, []).append(action)
    return action_index


def _build_edit_action_index(request: RevisionRequest) -> Dict[str, List[Any]]:
    action_index: Dict[str, List[Any]] = {}
    for action in request.edits:
        item_id = _extract_action_item_id(action)
        if not item_id:
            continue
        normalized_item_id = _normalize_item_id(item_id)
        action_index.setdefault(normalized_item_id, []).append(action)
    return action_index


def _action_window(actions: List[Any]) -> Tuple[Optional[float], Optional[float]]:
    starts = [float(action.start) for action in actions if action.start is not None]
    ends = [float(action.end) for action in actions if action.end is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _resolve_marker_window(
    source_item: RevisionReviewItem,
    actions: List[Any],
    *,
    prefer_action_window: bool = False,
) -> Tuple[float, float, bool]:
    action_start, action_end = _action_window(actions)
    row_start = source_item.start
    row_end = source_item.end

    if prefer_action_window and action_start is not None:
        if action_end is not None and action_end > action_start:
            return action_start, action_end, False
        return action_start, action_start + 0.8, True

    if row_start is not None and row_end is not None:
        start, end = float(row_start), float(row_end)
        if end < start:
            return end, start, True
        if end == start:
            return start, start + 0.8, True
        return start, end, False

    if row_start is not None:
        start = float(row_start)
        if action_end is not None and action_end > start:
            return start, action_end, False
        return start, start + 0.8, True

    if row_end is not None:
        end = float(row_end)
        if action_start is not None and action_start < end:
            return action_start, end, False
        return max(0.0, end - 0.8), end, True

    if action_start is None and action_end is None:
        return 0.0, 0.8, True
    if action_start is None:
        return max(0.0, action_end - 0.8), action_end, True
    if action_end is None:
        return action_start, action_start + 0.8, True
    if action_end < action_start:
        return action_end, action_start, True
    if action_end == action_start:
        return action_start, action_start + 0.8, True
    return action_start, action_end, False


def _longest_action_text(actions: List[Any]) -> str:
    details: List[str] = []
    labels: List[str] = []
    for action in actions:
        for values, value in (
            (details, getattr(action, "detail", "")),
            (labels, getattr(action, "label", "")),
        ):
            candidate = "" if value is None else str(value)
            if candidate.strip():
                values.append(candidate)
    candidates = details or labels
    return max(candidates, key=lambda value: len(value.strip()), default="")


def _legacy_item_id(action: Any, prefix: str, index: int) -> str:
    return _extract_action_item_id(action) or f"{prefix}_{index + 1:03d}"


def _build_legacy_plan(request: RevisionRequest) -> List[MarkerPlanItem]:
    plan: List[MarkerPlanItem] = []
    for index, marker in enumerate(request.markers):
        start, end = _normalize_window(marker.start, marker.end)
        plan.append(
            MarkerPlanItem(
                item_id=_legacy_item_id(marker, "marker", index),
                source_text=marker.label,
                start=start,
                end=end,
                verbatim_status=_UNVERIFIED_SOURCE,
                source="legacy_marker",
                kind="review_only",
            )
        )
    for index, edit in enumerate(request.edits):
        start, end = _normalize_window(edit.start, edit.end)
        plan.append(
            MarkerPlanItem(
                item_id=_legacy_item_id(edit, "edit", index),
                source_text=edit.label or edit.op_type,
                start=start,
                end=end,
                verbatim_status=_UNVERIFIED_SOURCE,
                source="legacy_edit",
                kind=str(
                    getattr(edit, "source_kind", "")
                    or getattr(edit, "op_type", "")
                    or "review_only"
                ),
            )
        )
    return plan


def build_marker_plan(
    request: RevisionRequest,
    doc_items: Optional[List[RevisionReviewItem]] = None,
) -> List[MarkerPlanItem]:
    source_items = doc_items if doc_items is not None else request.review_items
    if not source_items:
        if doc_items is not None:
            return []
        return _build_legacy_plan(request)

    action_index = _build_action_index(request)
    edit_action_index = _build_edit_action_index(request)
    pause_index: Dict[str, List[Any]] = {}
    for pause in request.pause_adjustments:
        pause_index.setdefault(_normalize_item_id(pause.item_id), []).append(pause)
    plan: List[MarkerPlanItem] = []
    seen_item_ids: set[str] = set()
    for index, source_item in enumerate(source_items):
        explicit_item_id = str(source_item.item_id or "").strip()
        item_id = explicit_item_id or f"item_{index + 1:03d}"
        normalized_item_id = _normalize_item_id(item_id)
        if normalized_item_id in seen_item_ids:
            raise ValueError(f"Duplicate review item id: {item_id}")
        seen_item_ids.add(normalized_item_id)

        actions = action_index.get(normalized_item_id, [])
        source_kind = str(source_item.kind or "").strip().casefold()
        execution_status = _execution_status_from_source_item(source_item)
        unresolved_timebase_marker_time = None
        if request.workflow_mode == "lite":
            execution_status = lite_review_item_execution_status(source_item)
            unresolved_timebase_marker_time = lite_unresolved_timebase_marker_time(source_item)
        if (
            request.workflow_mode == "lite"
            and lite_pause_change_is_label_only(source_kind, source_item.source_text)
            and not execution_status.casefold().startswith("label_only_")
        ):
            execution_status = "label_only_lite_policy"
        asr_aligned_lite_marker = request.workflow_mode == "lite" and (
            lite_timing_source(source_kind, source_item.source_text) == "asr"
        )
        pauses = pause_index.get(normalized_item_id, [])
        label_only_asr_time = (
            _label_only_asr_marker_time(source_item)
            if asr_aligned_lite_marker and unresolved_timebase_marker_time is None
            else None
        )
        if unresolved_timebase_marker_time is not None:
            start = unresolved_timebase_marker_time
            end = unresolved_timebase_marker_time + 0.8
        elif label_only_asr_time is not None:
            start, end = label_only_asr_time, label_only_asr_time + 0.8
        elif asr_aligned_lite_marker and pauses:
            pause_start = min(float(pause.source_time) for pause in pauses)
            start, end = pause_start, pause_start + 0.8
        else:
            if asr_aligned_lite_marker and not edit_action_index.get(normalized_item_id):
                raise ValueError(
                    f"Lite audio-related review item {item_id} has no ASR-resolved "
                    "edit or pause boundary."
                )
            timing_actions = (
                edit_action_index[normalized_item_id] if asr_aligned_lite_marker else actions
            )
            action_start, action_end = _action_window(timing_actions)
            if (
                request.workflow_mode == "lite"
                and source_item.start is None
                and source_item.end is None
                and action_start is None
                and action_end is None
            ):
                raise ValueError(
                    f"Lite review item {item_id} has no resolved timing source; "
                    "refusing to place its label at 0:00."
                )
            start, end, _timing_unverified = _resolve_marker_window(
                source_item,
                timing_actions,
                prefer_action_window=asr_aligned_lite_marker,
            )
        normalized_start, normalized_end = _normalize_window(start, end)
        start, end = normalized_start, normalized_end

        verbatim_status = str(source_item.verbatim_status or "verified").strip() or "verified"
        source_text = "" if source_item.source_text is None else str(source_item.source_text)
        if not source_text.strip():
            source_text = _longest_action_text(actions)
            verbatim_status = _UNVERIFIED_SOURCE

        plan.append(
            MarkerPlanItem(
                item_id=item_id,
                source_text=source_text,
                start=float(start),
                end=float(end),
                verbatim_status=verbatim_status,
                source=str(source_item.source or ""),
                kind=str(source_item.kind or "review_only"),
                execution_status=execution_status,
            )
        )

    return plan
