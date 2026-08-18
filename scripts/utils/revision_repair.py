import json
import os
import uuid
from copy import deepcopy
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from utils.revision_models import (
    RevisionEdit,
    RevisionRequest,
    _edit_review_id,
    _normalize_review_id,
)


class RevisionAcceptanceError(RuntimeError):
    def __init__(self, message: str, result_data: Dict[str, Any]):
        super().__init__(message)
        self.result_data = result_data


def _saved_draft_directory(save_result: Dict[str, Any]) -> str:
    raw_saved_path = str(save_result.get("draft_path") or "").strip()
    if not raw_saved_path:
        raise RuntimeError("Saved draft result is missing draft_path.")
    saved_path = os.path.abspath(raw_saved_path)
    return (
        os.path.dirname(saved_path)
        if os.path.basename(saved_path).casefold() == "draft_content.json"
        else saved_path
    )


def _read_json_object(path: str, label: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Saved draft {label} is not readable JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Saved draft {label} is not a JSON object: {path}.")
    return payload


def snapshot_saved_draft_files(
    save_result: Dict[str, Any],
) -> Dict[str, Optional[bytes]]:
    draft_path = _saved_draft_directory(save_result)
    paths = [
        os.path.join(draft_path, "draft_content.json"),
        os.path.join(draft_path, "draft_meta_info.json"),
        os.path.join(draft_path, "draft_virtual_store.json"),
        os.path.join(draft_path, "timeline_layout.json"),
    ]
    layout_path = paths[-1]
    if os.path.isfile(layout_path):
        layout = _read_json_object(layout_path, "timeline_layout")
        active_timeline = str(layout.get("activeTimeline") or "").strip()
        if active_timeline:
            paths.append(
                os.path.join(
                    draft_path,
                    "Timelines",
                    active_timeline,
                    "draft_content.json",
                )
            )

    snapshot: Dict[str, Optional[bytes]] = {}
    for path in dict.fromkeys(paths):
        if not os.path.isfile(path):
            snapshot[path] = None
            continue
        with open(path, "rb") as handle:
            snapshot[path] = handle.read()
    return snapshot


def restore_saved_draft_files(snapshot: Dict[str, Optional[bytes]]) -> None:
    for path, payload in snapshot.items():
        if payload is None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary_path = f"{path}.acceptance-restore-{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary_path, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass


def _edit_groups_by_item(request: RevisionRequest) -> Dict[str, List[RevisionEdit]]:
    grouped: Dict[str, List[RevisionEdit]] = {}
    for index, edit in enumerate(request.edits):
        item_id = _normalize_review_id(_edit_review_id(edit, index))
        grouped.setdefault(item_id, []).append(edit)
    return grouped


def _validate_targeted_repair_scope(
    original: RevisionRequest,
    repaired: RevisionRequest,
    *,
    affected_item_ids: List[str],
    gates: List[str],
) -> None:
    affected = {_normalize_review_id(item_id) for item_id in affected_item_ids}
    if original.project != repaired.project:
        raise ValueError("Targeted repair cannot change project identity or source materials.")
    if original.preserve != repaired.preserve or original.acceptance != repaired.acceptance:
        raise ValueError("Targeted repair cannot weaken preservation or acceptance rules.")
    if original.markers != repaired.markers:
        raise ValueError("Targeted repair cannot change review markers.")
    if original.pause_alignment != repaired.pause_alignment:
        raise ValueError("Targeted repair cannot change hash-bound pause alignment evidence.")

    original_groups = _edit_groups_by_item(original)
    repaired_groups = _edit_groups_by_item(repaired)

    for item_id in sorted(set(original_groups).union(repaired_groups) - affected):
        if original_groups.get(item_id, []) != repaired_groups.get(item_id, []):
            raise ValueError(
                f"Targeted repair changed unrelated review item {item_id or '<unknown>'}."
            )

    for item_id in affected:
        before_edits = original_groups.get(item_id, [])
        after_edits = repaired_groups.get(item_id, [])
        if len(before_edits) != len(after_edits):
            raise ValueError(f"Targeted repair cannot add or remove edit operations for {item_id}.")
        for before_edit, after_edit in zip(before_edits, after_edits):
            before_contract = replace(
                before_edit,
                start=0.0,
                end=0.0,
                evidence={},
                validation={},
            )
            after_contract = replace(
                after_edit,
                start=0.0,
                end=0.0,
                evidence={},
                validation={},
            )
            if before_contract != after_contract:
                raise ValueError(f"Targeted repair changed the operation contract for {item_id}.")

    original_review_items = {
        _normalize_review_id(item.item_id): item for item in original.review_items
    }
    repaired_review_items = {
        _normalize_review_id(item.item_id): item for item in repaired.review_items
    }
    for item_id in sorted(set(original_review_items).union(repaired_review_items) - affected):
        if original_review_items.get(item_id) != repaired_review_items.get(item_id):
            raise ValueError(
                f"Targeted repair changed unrelated review item {item_id or '<unknown>'}."
            )
    for item_id in affected:
        before_item = original_review_items.get(item_id)
        after_item = repaired_review_items.get(item_id)
        if before_item is None or after_item is None:
            raise ValueError(f"Targeted repair cannot add or remove review item {item_id}.")
        if replace(before_item, evidence={}, validation={}) != replace(
            after_item, evidence={}, validation={}
        ):
            raise ValueError(
                f"Targeted repair cannot change canonical review item fields for {item_id}."
            )

    def pause_groups(items):
        grouped = {}
        for item in items:
            grouped.setdefault(_normalize_review_id(item.item_id), []).append(item)
        return grouped

    original_pauses = pause_groups(original.pause_adjustments)
    repaired_pauses = pause_groups(repaired.pause_adjustments)
    for item_id in sorted(set(original_pauses).union(repaired_pauses) - affected):
        if original_pauses.get(item_id, []) != repaired_pauses.get(item_id, []):
            raise ValueError(
                f"Targeted repair changed unrelated pause adjustment {item_id or '<unknown>'}."
            )

    before_plan = original.audio_delivery_plan
    after_plan = repaired.audio_delivery_plan
    if (
        before_plan.mode != after_plan.mode
        or before_plan.pending != after_plan.pending
        or before_plan.forbid_full_length_segments != after_plan.forbid_full_length_segments
        or before_plan.max_single_segment_ratio != after_plan.max_single_segment_ratio
    ):
        raise ValueError("Targeted repair cannot change audio-delivery safety settings.")

    def delivery_groups(segments):
        grouped = {}
        for segment in segments:
            item_id = _normalize_review_id(segment.doc_item_id) or "<global>"
            grouped.setdefault(item_id, []).append(segment)
        return grouped

    original_delivery = delivery_groups(before_plan.segments)
    repaired_delivery = delivery_groups(after_plan.segments)
    for item_id in sorted(set(original_delivery).union(repaired_delivery) - affected):
        if original_delivery.get(item_id, []) != repaired_delivery.get(item_id, []):
            raise ValueError(f"Targeted repair changed unrelated audio-delivery segment {item_id}.")

    if not {"audio_precision", "audio_join"}.intersection(gates):
        return
    for item_id in affected:
        before = [edit for edit in original_groups.get(item_id, []) if edit.op_type == "delete"]
        after = [edit for edit in repaired_groups.get(item_id, []) if edit.op_type == "delete"]
        if len(before) != len(after):
            raise ValueError(
                f"Automatic audio repair cannot add or remove physical delete windows for {item_id}."
            )
        for original_edit, repaired_edit in zip(before, after):
            if (
                repaired_edit.start < original_edit.start - 0.000001
                or repaired_edit.end > original_edit.end + 0.000001
            ):
                raise ValueError(
                    f"Automatic audio repair cannot widen the delete window for {item_id}; "
                    "protected must_keep boundaries require a non-expanding repair."
                )


def run_targeted_acceptance_repair(
    request: RevisionRequest,
    initial_validation: Dict[str, Any],
    *,
    repair_callback: Callable[[RevisionRequest, Dict[str, Any]], RevisionRequest],
    prepare_callback: Optional[Callable[[RevisionRequest, Dict[str, Any]], RevisionRequest]] = None,
    validation_callback: Callable[[RevisionRequest, Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    """Run at most one scoped repair and revalidate its gates plus all global gates."""

    failures = [
        dict(failure)
        for failure in (initial_validation.get("failures") or [])
        if isinstance(failure, dict)
    ]
    previous_attempts = int(initial_validation.get("attempt_count") or 0)
    if previous_attempts >= 1 or bool(initial_validation.get("repair_attempted")):
        return {
            **initial_validation,
            "repair_attempted": False,
            "attempt_count": max(1, previous_attempts),
            "unresolved_item_ids": sorted(
                {
                    str(failure.get("item_id") or "")
                    for failure in failures
                    if str(failure.get("item_id") or "")
                }
            ),
        }
    if initial_validation.get("ok") or not failures:
        return {
            **initial_validation,
            "repair_attempted": False,
            "attempt_count": 0,
            "unresolved_item_ids": [],
        }
    if any(not bool(failure.get("repairable")) for failure in failures):
        return {
            **initial_validation,
            "repair_attempted": False,
            "attempt_count": 0,
            "unresolved_item_ids": sorted(
                {
                    str(failure.get("item_id") or "")
                    for failure in failures
                    if str(failure.get("item_id") or "")
                }
            ),
        }

    item_ids = list(
        dict.fromkeys(
            str(failure.get("item_id") or "").strip()
            for failure in failures
            if str(failure.get("item_id") or "").strip()
        )
    )
    if not item_ids:
        raise ValueError("Automatic targeted repair requires attributable review item IDs.")
    gates = list(
        dict.fromkeys(
            str(failure.get("gate") or "").strip()
            for failure in failures
            if str(failure.get("gate") or "").strip()
        )
    )
    plan = {
        "attempt": 1,
        "max_attempts": 1,
        "item_ids": item_ids,
        "gates": gates,
        "failures": failures,
        "physical_delete_window_policy": "non_expanding",
    }
    original_request = deepcopy(request)
    repaired_request = repair_callback(deepcopy(request), plan)
    if not isinstance(repaired_request, RevisionRequest):
        raise TypeError("repair_callback must return a RevisionRequest.")
    _validate_targeted_repair_scope(
        original_request,
        repaired_request,
        affected_item_ids=item_ids,
        gates=gates,
    )
    if prepare_callback is not None:
        repaired_request = prepare_callback(deepcopy(repaired_request), plan)
        if not isinstance(repaired_request, RevisionRequest):
            raise TypeError("prepare_callback must return a RevisionRequest.")
        _validate_targeted_repair_scope(
            original_request,
            repaired_request,
            affected_item_ids=item_ids,
            gates=gates,
        )

    final_validation = validation_callback(repaired_request, plan)
    if not isinstance(final_validation, dict):
        raise TypeError("validation_callback must return a validation result object.")
    required_gates = {
        "source_coverage",
        "execution_evidence",
        "draft_exists",
        "editable_structure",
        "verbatim_markers",
        "audio_delivery",
        *gates,
    }
    enabled_gates = set((final_validation.get("metrics") or {}).get("enabled_gates") or [])
    missing_gates = sorted(required_gates - enabled_gates)
    if missing_gates:
        final_validation = {
            **final_validation,
            "ok": False,
            "failures": [
                *(final_validation.get("failures") or []),
                {
                    "gate": "repair_validation",
                    "item_id": "",
                    "status": "fail",
                    "repairable": False,
                    "reason": "Repair validation skipped required gates: "
                    + ", ".join(missing_gates),
                },
            ],
        }
    unresolved_item_ids = sorted(
        {
            str(failure.get("item_id") or "")
            for failure in (final_validation.get("failures") or [])
            if isinstance(failure, dict) and str(failure.get("item_id") or "")
        }
    )
    return {
        **final_validation,
        "repair_attempted": True,
        "attempt_count": 1,
        "repair_plan": plan,
        "unresolved_item_ids": unresolved_item_ids,
    }


__all__ = [
    "RevisionAcceptanceError",
    "restore_saved_draft_files",
    "run_targeted_acceptance_repair",
    "snapshot_saved_draft_files",
]
