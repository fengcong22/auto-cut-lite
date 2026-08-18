import math
from collections.abc import Mapping, Sequence
from typing import Any

_TARGET_SCOPES = {"dependency_group", "roi_target"}
_COMPLETION_POLICIES = {"group_complete", "target_only"}
_TARGET_ANCHOR_STATES = {"first_visible", "stable_complete"}
_CONFIDENCE_LEVELS = {"high", "medium", "low"}
_PREVIEW_LOCATOR_FIELDS = ("preview_path", "artifact_path", "segment_id")


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_string_sequence(value: Any, *, allow_empty: bool) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return False
    if not allow_empty and not value:
        return False
    return all(isinstance(item, str) and item.strip() for item in value)


def _frame_event_problems(value: Any) -> tuple[list[str], list[Mapping[str, Any]]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        return ["scope_basis_frame_events_missing"], []

    problems: list[str] = []
    if len(value) < 2:
        problems.append("scope_basis_frame_events_insufficient")
    events: list[Mapping[str, Any]] = []
    for raw_event in value:
        if not isinstance(raw_event, Mapping):
            problems.append("scope_basis_frame_event_invalid")
            continue
        events.append(raw_event)
        if not any(
            isinstance(raw_event.get(field), str) and raw_event[field].strip()
            for field in ("event", "name")
        ):
            problems.append("scope_basis_frame_event_label_missing")
        if not any(_is_finite_number(raw_event.get(field)) for field in ("time", "source_time")):
            problems.append("scope_basis_frame_event_time_invalid")
    return problems, events


def _is_positive_in_canvas_roi(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    fields = ("x", "y", "width", "height", "canvas_width", "canvas_height")
    if not all(_is_finite_number(value.get(field)) for field in fields):
        return False
    x, y, width, height, canvas_width, canvas_height = (float(value[field]) for field in fields)
    return (
        x >= 0
        and y >= 0
        and width > 0
        and height > 0
        and canvas_width > 0
        and canvas_height > 0
        and x + width <= canvas_width
        and y + height <= canvas_height
    )


def _low_confidence_preview_problems(evidence: Mapping[str, Any]) -> list[str]:
    if evidence.get("confidence") != "low":
        return []

    raw_receipts = evidence.get("preview_receipts")
    if not isinstance(raw_receipts, Sequence) or isinstance(raw_receipts, (str, bytes)):
        return [
            "low_confidence_preview_receipts_insufficient",
            "low_confidence_preview_scopes_incomplete",
        ]

    problems: list[str] = []
    preview_ids: list[str] = []
    preview_scopes: set[str] = set()
    for receipt in raw_receipts:
        if not isinstance(receipt, Mapping):
            problems.append("low_confidence_preview_receipt_invalid")
            continue
        preview_id = receipt.get("preview_id")
        target_scope = receipt.get("target_scope")
        if not isinstance(preview_id, str) or not preview_id.strip():
            problems.append("low_confidence_preview_receipt_invalid")
        else:
            preview_ids.append(preview_id.strip())
        if target_scope not in _TARGET_SCOPES:
            problems.append("low_confidence_preview_receipt_invalid")
        else:
            preview_scopes.add(target_scope)
        if receipt.get("status") != "pass":
            problems.append("low_confidence_preview_receipt_status_not_pass")
        if not any(
            isinstance(receipt.get(field), str) and receipt[field].strip()
            for field in _PREVIEW_LOCATOR_FIELDS
        ):
            problems.append("low_confidence_preview_receipt_locator_missing")

    distinct_preview_ids = set(preview_ids)
    if len(distinct_preview_ids) < 2:
        problems.append("low_confidence_preview_receipts_insufficient")
    if len(preview_ids) != len(distinct_preview_ids):
        problems.append("low_confidence_preview_receipts_not_distinct")
    if preview_scopes != _TARGET_SCOPES:
        problems.append("low_confidence_preview_scopes_incomplete")
    return problems


def animation_evidence_problems(evidence: Mapping[str, Any]) -> list[str]:
    """Return stable problem codes for one animation timing evidence object."""

    if not isinstance(evidence, Mapping):
        return ["evidence_mapping_required"]

    problems: list[str] = []
    target_scope = evidence.get("target_scope")
    completion_policy = evidence.get("completion_policy")
    target_anchor_state = evidence.get("target_anchor_state")
    confidence = evidence.get("confidence")

    if target_scope not in _TARGET_SCOPES:
        problems.append("target_scope_invalid")
    if completion_policy not in _COMPLETION_POLICIES:
        problems.append("completion_policy_invalid")
    if target_anchor_state not in _TARGET_ANCHOR_STATES:
        problems.append("target_anchor_state_invalid")
    if confidence not in _CONFIDENCE_LEVELS:
        problems.append("confidence_invalid")

    scope_basis = evidence.get("scope_basis")
    frame_events: list[Mapping[str, Any]] = []
    if not isinstance(scope_basis, Mapping) or not scope_basis:
        problems.append("scope_basis_missing")
    else:
        source_text = scope_basis.get("source_text")
        if not isinstance(source_text, str) or not source_text.strip():
            problems.append("scope_basis_source_text_missing")
        if "screenshot_roi" not in scope_basis:
            problems.append("scope_basis_screenshot_roi_missing")
        event_problems, frame_events = _frame_event_problems(scope_basis.get("frame_events"))
        problems.extend(event_problems)

    required_elements = evidence.get("required_elements")
    forbidden_future_elements = evidence.get("forbidden_future_elements")
    required_elements_valid = _is_string_sequence(required_elements, allow_empty=False)
    if not required_elements_valid:
        problems.append("required_elements_invalid")
    if not _is_string_sequence(forbidden_future_elements, allow_empty=True):
        problems.append("forbidden_future_elements_invalid")

    if target_scope == "roi_target":
        if completion_policy != "target_only":
            problems.append("roi_target_completion_policy_mismatch")
        roi = scope_basis.get("screenshot_roi") if isinstance(scope_basis, Mapping) else None
        if not _is_positive_in_canvas_roi(roi):
            problems.append("roi_target_screenshot_roi_invalid")
    elif target_scope == "dependency_group":
        if completion_policy != "group_complete":
            problems.append("dependency_group_completion_policy_mismatch")
        if target_anchor_state != "stable_complete":
            problems.append("dependency_group_target_anchor_state_mismatch")
        normalized_required = (
            {item.strip() for item in required_elements} if required_elements_valid else set()
        )
        if len(normalized_required) < 2:
            problems.append("dependency_group_required_elements_insufficient")
        event_elements = {
            str(event.get("element")).strip()
            for event in frame_events
            if isinstance(event.get("element"), str) and str(event.get("element")).strip()
        }
        if any(
            not isinstance(event.get("element"), str) or not str(event.get("element")).strip()
            for event in frame_events
        ):
            problems.append("dependency_group_frame_event_element_missing")
        if not normalized_required.issubset(event_elements):
            problems.append("dependency_group_frame_event_elements_incomplete")

    numeric_boundaries: dict[str, float] = {}
    for field in ("first_visible", "stable_frame", "release"):
        value = evidence.get(field)
        if _is_finite_number(value):
            numeric_boundaries[field] = float(value)
        else:
            problems.append(f"{field}_invalid")

    next_animation_start = evidence.get("next_animation_start")
    if next_animation_start is not None:
        if _is_finite_number(next_animation_start):
            numeric_boundaries["next_animation_start"] = float(next_animation_start)
        else:
            problems.append("next_animation_start_invalid")

    if all(field in numeric_boundaries for field in ("first_visible", "stable_frame", "release")):
        first_visible = numeric_boundaries["first_visible"]
        stable_frame = numeric_boundaries["stable_frame"]
        release = numeric_boundaries["release"]
        ordered = first_visible <= stable_frame <= release
        if "next_animation_start" in numeric_boundaries:
            ordered = ordered and release <= numeric_boundaries["next_animation_start"]
        if not ordered:
            problems.append("timing_order_invalid")

    completion_receipt = evidence.get("completion_receipt")
    if not isinstance(completion_receipt, Mapping):
        problems.append("completion_receipt_missing")
    else:
        if completion_receipt.get("status") != "pass":
            problems.append("completion_receipt_status_not_pass")
        stable_sample_count = completion_receipt.get("stable_sample_count")
        if not _is_finite_number(stable_sample_count) or float(stable_sample_count) < 2:
            problems.append("completion_receipt_stable_sample_count_insufficient")

        present_required = completion_receipt.get("required_elements_present")
        if (
            not required_elements_valid
            or not _is_string_sequence(present_required, allow_empty=True)
            or not set(required_elements).issubset(set(present_required))
        ):
            problems.append("completion_receipt_required_elements_not_covered")

        present_forbidden = completion_receipt.get("forbidden_future_elements_present")
        if not _is_string_sequence(present_forbidden, allow_empty=True):
            problems.append("completion_receipt_forbidden_elements_unverified")
        elif present_forbidden:
            problems.append("completion_receipt_forbidden_elements_present")

    problems.extend(_low_confidence_preview_problems(evidence))
    return list(dict.fromkeys(problems))
