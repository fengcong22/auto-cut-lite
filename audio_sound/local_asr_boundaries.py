"""Validate native local-ASR timing structure without accepting acoustic accuracy.

Callers must hash and verify the input, saved result, model and adapter before
supplying identity evidence. Native rows use unfiltered, zero-based indices and
crop-local seconds. Grouping is strict raw-text aggregation, not a text cleaner
or forced aligner. No invalid row is removed or assigned a synthetic boundary.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

BOUNDARY_VALIDATOR_VERSION = "local-asr-boundaries-v1"
MAX_NATIVE_GAP_SECONDS = 1.5
_EPSILON = 1e-6
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _json_copy(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_float": repr(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"non_json_type": type(value).__name__, "representation": repr(value)}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rows(value: Any) -> list[Any] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    return list(value)


def _issue(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **_json_copy(details)}


def _identity_issues(identity: Any) -> list[dict[str, Any]]:
    if not isinstance(identity, Mapping):
        return [_issue("identity_missing_or_malformed")]
    issues: list[dict[str, Any]] = []
    fields = {
        "input": ("sha256",),
        "result": ("sha256",),
        "model": ("id", "revision", "sha256"),
        "provider": ("name", "version"),
        "adapter": ("name", "version", "sha256"),
    }
    for component, required in fields.items():
        evidence = identity.get(component)
        if not isinstance(evidence, Mapping):
            issues.append(_issue("identity_component_missing", component=component))
            continue
        if evidence.get("verified") is not True:
            issues.append(_issue("identity_not_verified", component=component))
        for field in required:
            value = evidence.get(field)
            if not isinstance(value, str) or not value.strip():
                issues.append(_issue("identity_field_missing", component=component, field=field))
            elif field == "sha256" and _SHA256.fullmatch(value) is None:
                issues.append(_issue("identity_sha256_malformed", component=component))
    if identity.get("timestamp_kind") != "native_unit_seconds":
        issues.append(_issue("native_timestamp_semantics_unverified"))
    provider = identity.get("provider")
    model = identity.get("model")
    if isinstance(provider, Mapping) and isinstance(model, Mapping):
        provider_name = str(provider.get("name") or "").casefold()
        model_id = str(model.get("id") or "").casefold()
        if (
            "funasr" in provider_name
            and provider.get("version") == "1.2.6"
            and "paraformer" in model_id
        ):
            issues.append(_issue("funasr_1_2_6_paraformer_timestamp_ambiguity"))
    return issues


def _native_issues(units: list[Any], duration: float | None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for index, unit in enumerate(units):
        if not isinstance(unit, Mapping):
            issues.append(_issue("native_row_malformed", native_indices=[index]))
            continue
        if type(unit.get("native_index")) is not int or unit["native_index"] != index:
            issues.append(_issue("native_index_mismatch", native_indices=[index]))
        if not isinstance(unit.get("text"), str):
            issues.append(_issue("native_text_malformed", native_indices=[index]))
        start, end = _number(unit.get("start")), _number(unit.get("end"))
        if start is None or end is None:
            issues.append(_issue("native_time_invalid", native_indices=[index]))
            continue
        if end <= start:
            issues.append(_issue("native_interval_nonpositive", native_indices=[index]))
        if start < -_EPSILON or (duration is not None and end > duration + _EPSILON):
            issues.append(_issue("native_interval_out_of_bounds", native_indices=[index]))
        if index == 0 or not isinstance(units[index - 1], Mapping):
            continue
        previous = units[index - 1]
        previous_start, previous_end = _number(previous.get("start")), _number(previous.get("end"))
        if previous_start is not None and start < previous_start - _EPSILON:
            issues.append(_issue("native_order_reversed", native_indices=[index - 1, index]))
        if previous_end is None:
            continue
        if start < previous_end - _EPSILON:
            issues.append(_issue("native_intervals_overlap", native_indices=[index - 1, index]))
        elif start - previous_end > MAX_NATIVE_GAP_SECONDS + _EPSILON:
            issues.append(_issue("native_gap_exceeds_limit", native_indices=[index - 1, index]))
    return issues


def _group_issues(units: list[Any], grouped_units: Any) -> list[dict[str, Any]]:
    groups = _rows(grouped_units)
    if not groups:
        return [_issue("group_mapping_missing_or_malformed")]
    issues: list[dict[str, Any]] = []
    flattened: list[int] = []
    for group_index, group in enumerate(groups):
        if not isinstance(group, Mapping):
            issues.append(_issue("group_row_malformed", group_index=group_index))
            continue
        indices = _rows(group.get("native_indices"))
        if not indices or any(type(index) is not int for index in indices):
            issues.append(_issue("group_indices_malformed", group_index=group_index))
            continue
        flattened.extend(indices)
        if any(index < 0 or index >= len(units) for index in indices):
            issues.append(_issue("group_index_out_of_bounds", group_index=group_index))
            continue
        if indices != list(range(indices[0], indices[-1] + 1)):
            issues.append(_issue("group_indices_not_consecutive", group_index=group_index))
        selected = [units[index] for index in indices]
        if any(not isinstance(unit, Mapping) for unit in selected):
            continue
        native_start = _number(selected[0].get("start"))
        native_end = _number(selected[-1].get("end"))
        start, end = _number(group.get("start")), _number(group.get("end"))
        if (
            start is None
            or end is None
            or native_start is None
            or native_end is None
            or abs(start - native_start) > _EPSILON
            or abs(end - native_end) > _EPSILON
        ):
            issues.append(_issue("group_edges_not_native", group_index=group_index))
        texts = [unit.get("text") for unit in selected]
        if any(not isinstance(text, str) for text in texts) or group.get("text") != "".join(texts):
            issues.append(_issue("group_raw_text_mismatch", group_index=group_index))
    if flattened != list(range(len(units))):
        issues.append(_issue("group_mapping_not_exact_partition", native_indices=flattened))
    return issues


def validate_local_asr_boundaries(
    native_units: Any,
    *,
    identity: Any,
    crop_offset_seconds: Any,
    crop_duration_seconds: Any,
    source_duration_seconds: Any,
    selected_native_indices: Any,
    selected_window: Any = None,
    protected_windows: Any = (),
    grouped_units: Any = None,
) -> dict[str, Any]:
    """Return a JSON-safe structural report; never authorize a production cut.

    Required identity evidence contains verified input/result SHA-256s, model
    id/revision/SHA-256, provider name/version, adapter name/version/SHA-256 and
    timestamp_kind="native_unit_seconds". Verification is the caller's duty.

    Explicit selected/protected/group windows all use crop-local seconds.
    A grouped result must partition the complete original list exactly once in
    order, concatenate raw text exactly, and retain each group's native edges.
    Unselected timing anomalies remain in native_unit_issues. Selected anomalies
    and anomalies in either immediate neighbor block the selected window. An
    intersecting valid interval from any unselected native row also blocks it.
    optional full-list grouping additionally blocks on every timing anomaly.
    Nonfinite raw floats are preserved as tagged values for strict JSON output.
    """
    issues = _identity_issues(identity)
    offset = _number(crop_offset_seconds)
    duration = _number(crop_duration_seconds)
    source_duration = _number(source_duration_seconds)
    if offset is None or offset < 0:
        issues.append(_issue("crop_offset_invalid"))
    if duration is None or duration <= 0:
        issues.append(_issue("crop_duration_invalid"))
    if source_duration is None or source_duration <= 0:
        issues.append(_issue("source_duration_invalid"))
    if (
        offset is not None
        and duration is not None
        and source_duration is not None
        and offset + duration > source_duration + _EPSILON
    ):
        issues.append(_issue("crop_exceeds_source"))
    units = _rows(native_units)
    if not units:
        issues.append(_issue("native_units_missing_or_malformed"))
        units = []
    native_issues = _native_issues(units, duration)
    indices = _rows(selected_native_indices)
    valid_indices = bool(indices) and all(type(index) is int for index in indices)
    if not valid_indices:
        issues.append(_issue("selected_indices_missing_or_malformed"))
    elif any(index < 0 or index >= len(units) for index in indices):
        valid_indices = False
        issues.append(_issue("selected_index_out_of_bounds"))
    elif indices != list(range(indices[0], indices[-1] + 1)):
        valid_indices = False
        issues.append(_issue("selected_indices_not_consecutive"))
    selected_set = set(indices) if valid_indices else set()
    neighbor_set = set()
    if valid_indices:
        neighbor_set = {
            index for index in (indices[0] - 1, indices[-1] + 1) if 0 <= index < len(units)
        }
    for issue in native_issues:
        affected = set(issue["native_indices"])
        global_schema_error = issue["code"] in {"native_index_mismatch", "native_row_malformed"}
        neighbor_anomaly = len(affected) == 1 and bool(affected & neighbor_set)
        neighbor_order_error = (
            issue["code"] in {"native_intervals_overlap", "native_order_reversed"}
            and affected & selected_set
        )
        if (
            global_schema_error
            or affected <= selected_set
            or neighbor_anomaly
            or neighbor_order_error
            or grouped_units is not None
        ):
            issues.append(dict(issue))

    local_window: dict[str, float] | None = None
    source_window: dict[str, float] | None = None
    neighbors: dict[str, Any] = {"before": None, "after": None}
    if valid_indices:
        first, last = indices[0], indices[-1]
        neighbors = {
            "before": _json_copy(units[first - 1]) if first > 0 else None,
            "after": _json_copy(units[last + 1]) if last + 1 < len(units) else None,
        }
        first_unit, last_unit = units[first], units[last]
        start = _number(first_unit.get("start")) if isinstance(first_unit, Mapping) else None
        end = _number(last_unit.get("end")) if isinstance(last_unit, Mapping) else None
        if start is not None and end is not None:
            local_window = {"start": start, "end": end}
            if offset is not None:
                source_start, source_end = _number(offset + start), _number(offset + end)
                if source_start is None or source_end is None:
                    issues.append(_issue("source_window_arithmetic_invalid"))
                else:
                    source_window = {"start": source_start, "end": source_end}
    if local_window is not None:
        for index, unit in enumerate(units):
            if index in selected_set or not isinstance(unit, Mapping):
                continue
            start, end = _number(unit.get("start")), _number(unit.get("end"))
            if start is None or end is None or end <= start:
                continue
            if start < local_window["end"] - _EPSILON and local_window["start"] < end - _EPSILON:
                issues.append(
                    _issue(
                        "unselected_native_interval_overlap",
                        native_indices=[index],
                        selected_native_indices=indices,
                        unselected_window={"start": start, "end": end},
                    )
                )
    if selected_window is not None:
        if not isinstance(selected_window, Mapping):
            issues.append(_issue("selected_window_malformed"))
        else:
            requested_start = _number(selected_window.get("start"))
            requested_end = _number(selected_window.get("end"))
            if (
                requested_start is None
                or requested_end is None
                or local_window is None
                or abs(requested_start - local_window["start"]) > _EPSILON
                or abs(requested_end - local_window["end"]) > _EPSILON
            ):
                issues.append(_issue("selected_edges_not_native"))

    protection = _rows(protected_windows)
    if protection is None:
        issues.append(_issue("protected_windows_malformed"))
        protection = []
    for index, window in enumerate(protection):
        if not isinstance(window, Mapping):
            issues.append(_issue("protected_window_malformed", protected_index=index))
            continue
        start, end = _number(window.get("start")), _number(window.get("end"))
        if start is None or end is None or end <= start:
            issues.append(_issue("protected_interval_invalid", protected_index=index))
        elif start < -_EPSILON or (duration is not None and end > duration + _EPSILON):
            issues.append(_issue("protected_interval_out_of_bounds", protected_index=index))
        elif (
            local_window
            and start < local_window["end"] - _EPSILON
            and local_window["start"] < end - _EPSILON
        ):
            issues.append(_issue("protected_window_overlap", protected_index=index))
    if grouped_units is not None:
        issues.extend(_group_issues(units, grouped_units))
    return {
        "schema_version": BOUNDARY_VALIDATOR_VERSION,
        "status": "blocked" if issues else "structural_pass",
        "structural_pass": not issues,
        "production_eligible": False,
        "acoustic_accepted": False,
        "identity_verification": "caller_supplied_verified_evidence",
        "identity": _json_copy(identity),
        "crop": {
            "offset_seconds": _json_copy(crop_offset_seconds),
            "duration_seconds": _json_copy(crop_duration_seconds),
            "source_duration_seconds": _json_copy(source_duration_seconds),
        },
        "raw_native_units": _json_copy(native_units),
        "selected_native_indices": _json_copy(selected_native_indices),
        "selected_window_requested": _json_copy(selected_window),
        "local_window": local_window,
        "source_window": source_window,
        "neighbor_units_before_pruning": neighbors,
        "protected_windows_before_pruning": _json_copy(protected_windows),
        "grouped_units": _json_copy(grouped_units),
        "native_unit_issues": native_issues,
        "issues": issues,
    }
