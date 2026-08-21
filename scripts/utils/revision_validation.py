import hashlib
import importlib.util
import json
import math
import os
import re
import subprocess
import wave
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Sequence

import cv2
from utils.animation_validation import animation_evidence_problems
from utils.draft_retention import infer_project_family
from utils.formatters import get_duration_ffprobe_cached
from utils.pause_alignment import (
    PauseAlignmentError,
    protected_utterance_anchor,
    resolve_pause_boundary,
)
from utils.pointer_validation import (
    CLEANUP_ONLY_POINTER_LIFECYCLE_MODES,
    pointer_lifecycle_evidence_problems,
    pointer_saved_geometry_problems,
    pointer_saved_layer_problems,
    pointer_saved_motion_problems,
    pointer_saved_residual_cover_problems,
)
from utils.review_marker_layout_validation import review_marker_top_layout_problems
from utils.revision_evidence import (
    audio_delivery_plan_sha256,
    sha256_file,
    validate_pause_frame_matches_source,
    validate_pause_source_provenance,
)
from utils.revision_markers import (
    MarkerPlanItem,
    build_marker_plan,
    map_marker_plan_to_timeline,
    validate_saved_marker_plan,
)
from utils.revision_models import (
    _AUDIO_KINDS,
    _AUDIO_VALIDATION_PASS_STATUSES,
    _FAIL_STATUSES,
    _PASS_STATUSES,
    _REVIEW_ID_PATTERN,
    _SEMANTIC_JOIN_FORBIDDEN_PHRASES,
    _VISUAL_KINDS,
    RevisionEdit,
    RevisionRequest,
    RevisionReviewItem,
    _as_bool,
    _build_keep_windows,
    _classify_review_text,
    _clean_track_name,
    _collect_delete_windows,
    _edit_review_id,
    _extract_review_id,
    _fingerprint_text,
    _has_replacement_glyphs,
    _is_visual_edit,
    _load_json,
    _normalize_review_id,
    _optional_float,
    _replacement_audio_paths_for_request,
    _visual_plan_segments,
    build_revision_summary,
)

_SUBJECT_POINTER_BINDINGS_MODULE: Optional[ModuleType] = None
_LOCAL_TRANSCRIPT_FIELDS = (
    "local_joined_text",
    "local_asr_text",
    "text",
    "snippet",
    "joined_text",
)


def _load_subject_pointer_bindings_module() -> ModuleType:
    global _SUBJECT_POINTER_BINDINGS_MODULE
    if _SUBJECT_POINTER_BINDINGS_MODULE is not None:
        return _SUBJECT_POINTER_BINDINGS_MODULE
    module_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "auto-cut-subject-pointer-onboarding"
        / "scripts"
        / "project_bindings.py"
    )
    spec = importlib.util.spec_from_file_location(
        "revision_subject_pointer_project_bindings", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load subject pointer binding validator: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SUBJECT_POINTER_BINDINGS_MODULE = module
    return module


def _canonical_subject_pointer_registry_root() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "subject-pointer-profiles.local"


def _fresh_subject_pointer_receipt_validation(
    receipt: Dict[str, Any], request: RevisionRequest
) -> Dict[str, Any]:
    expected_project_key = request.project.project_key or infer_project_family(
        request.project.draft_name
    )
    try:
        result = _load_subject_pointer_bindings_module().validate_pointer_receipt(
            receipt, _canonical_subject_pointer_registry_root()
        )
    except (
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        RuntimeError,
    ) as error:
        return {
            "ok": False,
            "status": "error",
            "problems": [f"receipt_validation_error:{error}"],
        }
    problems = [str(item) for item in result.get("problems") or []]
    if expected_project_key and receipt.get("project_key") != expected_project_key:
        problems.append("receipt_current_project_key_mismatch")
    return {
        **result,
        "ok": bool(result.get("ok")) and not problems,
        "problems": sorted(set(problems)),
        "expected_project_key": expected_project_key,
    }


def _pointer_overlay_receipt_problems(
    evidence: Dict[str, Any],
    receipt: Dict[str, Any],
    content: Optional[Dict[str, Any]],
) -> List[str]:
    problems: List[str] = []
    if _normalize_material_path(evidence.get("asset_path")) != _normalize_material_path(
        receipt.get("asset_path")
    ):
        problems.append("overlay_asset_path_mismatch")
    if (
        str(evidence.get("asset_role") or "").strip()
        != str(receipt.get("asset_role") or "").strip()
    ):
        problems.append("overlay_asset_role_mismatch")
    current_layout = str(evidence.get("current_layout") or evidence.get("layout") or "").strip()
    if current_layout != str(receipt.get("scale_reference_layout") or "").strip():
        problems.append("overlay_current_layout_mismatch")

    if not isinstance(content, dict):
        problems.append("overlay_saved_draft_missing")
        return problems
    track_name = _normalize_track_name(evidence.get("track_name"))
    segment_id = str(evidence.get("segment_id") or "").strip()
    matching_segment: Optional[Dict[str, Any]] = None
    for track in content.get("tracks") or []:
        if not isinstance(track, dict) or _normalize_track_name(track.get("name")) != track_name:
            continue
        for segment in track.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            saved_segment_id = str(segment.get("id") or segment.get("segment_id") or "").strip()
            if saved_segment_id == segment_id:
                matching_segment = segment
                break
        if matching_segment is not None:
            break
    if matching_segment is None:
        problems.append("overlay_segment_missing")
        return problems

    material_id = str(matching_segment.get("material_id") or "").strip()
    if not material_id:
        problems.append("overlay_segment_material_id_missing")
        return problems
    material_path = ""
    materials = content.get("materials") or {}
    if isinstance(materials, dict):
        for group in materials.values():
            if not isinstance(group, list):
                continue
            for material in group:
                if not isinstance(material, dict):
                    continue
                saved_material_id = str(
                    material.get("id") or material.get("material_id") or ""
                ).strip()
                if saved_material_id == material_id:
                    material_path = str(material.get("path") or "").strip()
                    break
            if material_path:
                break
    if _normalize_material_path(material_path) != _normalize_material_path(
        receipt.get("asset_path")
    ):
        problems.append("overlay_material_path_mismatch")
    return problems


def _is_cleanup_only_pointer_evidence(evidence: Dict[str, Any]) -> bool:
    return str(evidence.get("lifecycle_mode") or "").strip().casefold() in (
        CLEANUP_ONLY_POINTER_LIFECYCLE_MODES
    )


def _pointer_saved_state_validation(
    review_items: Sequence[RevisionReviewItem],
    routes_by_id: Dict[str, Dict[str, Any]],
    content: Dict[str, Any],
) -> Dict[str, Any]:
    item_problems: Dict[str, List[str]] = {}
    pointer_evidences: List[Dict[str, Any]] = []
    for item in review_items:
        route = routes_by_id.get(_normalize_review_id(item.item_id), {})
        evidence = item.evidence if isinstance(item.evidence, dict) else {}
        motion_problems = pointer_saved_motion_problems(evidence, content)
        if motion_problems:
            item_problems.setdefault(item.item_id, []).extend(motion_problems)
        cover_problems = pointer_saved_residual_cover_problems(evidence, content)
        if cover_problems:
            item_problems.setdefault(item.item_id, []).extend(cover_problems)
        if "pointer" not in set(route.get("gates") or []):
            continue
        if _is_cleanup_only_pointer_evidence(evidence):
            continue
        receipt = evidence.get("subject_profile_receipt")
        pointer_evidences.append(evidence)
        problems = pointer_saved_geometry_problems(evidence, receipt, content)
        if problems:
            item_problems.setdefault(item.item_id, []).extend(problems)

    layer_problems = pointer_saved_layer_problems(pointer_evidences, content)
    return {
        "ok": not item_problems and not layer_problems,
        "item_problems": item_problems,
        "layer_problems": layer_problems,
        "pointer_item_count": len(pointer_evidences),
    }


def _normalize_track_type_name(raw_value: Any) -> str:
    return str(raw_value or "").strip().lower()


def _normalize_track_name(raw_value: Any) -> str:
    return str(raw_value or "").strip().lower()


def _normalize_material_path(raw_value: Any) -> str:
    return str(raw_value or "").strip().replace("\\", "/").lower()


_AUDIO_DELIVERY_TOLERANCE_US = 50_000
_AUDIO_DELIVERY_FADE_TOLERANCE_US = 1_000
_AUDIO_DELIVERY_VOLUME_TOLERANCE = 1e-6
_AUDIO_DELIVERY_NARRATION_ROLES = {"source", "replacement_video", "repair"}
_LITE_REUSED_AUDIO_TRACK_NAME = "Lite Reused Audio"


def _saved_audio_delivery_volume(request: RevisionRequest, segment: Any) -> float:
    """Return the volume that the editable saved draft must expose.

    The normalized segmented plan keeps reference rows at volume 0 so full-workflow
    reverse-ASR and candidate-duration semantics remain unchanged.  Lite split-gap
    drafts intentionally expose the deleted-source A2 lane at normal volume for
    secondary manual review.
    """

    if (
        request.workflow_mode == "lite"
        and request.lite_cut_layout == "split_gap"
        and segment.track_name == _LITE_REUSED_AUDIO_TRACK_NAME
    ):
        return 1.0
    return float(segment.volume)
_WAV_VALIDATION_CHUNK_FRAMES = 65_536


def _normalize_audio_delivery_path(raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    if not value:
        return ""
    return _normalize_material_path(os.path.normcase(os.path.abspath(value)))


def _normalize_audio_delivery_track_name(raw_value: Any) -> str:
    return " ".join(str(raw_value or "").split()).casefold()


def _safe_int(raw_value: Any) -> Optional[int]:
    try:
        return int(round(float(raw_value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_float(raw_value: Any) -> Optional[float]:
    try:
        value = float(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _seconds_to_us(raw_value: Any) -> int:
    return int(round(float(raw_value) * 1_000_000))


def _fade_payload_durations_us(
    payload: Any,
    fade_materials_by_id: Dict[str, Dict[str, Any]],
) -> Optional[tuple[int, int]]:
    if isinstance(payload, (str, int)):
        payload = fade_materials_by_id.get(str(payload))
    if isinstance(payload, list):
        for item in payload:
            durations = _fade_payload_durations_us(item, fade_materials_by_id)
            if durations is not None:
                return durations
        return None
    if not isinstance(payload, dict):
        return None

    payload_id = str(payload.get("id") or payload.get("material_id") or "")
    if payload_id and payload_id in fade_materials_by_id:
        payload = fade_materials_by_id[payload_id]
    if not any(
        key in payload
        for key in ("fade_in_duration", "fade_out_duration", "in_duration", "out_duration")
    ):
        return None

    fade_in = _safe_int(payload.get("fade_in_duration", payload.get("in_duration", 0)))
    fade_out = _safe_int(payload.get("fade_out_duration", payload.get("out_duration", 0)))
    if fade_in is None or fade_out is None:
        return None
    return fade_in, fade_out


def _segment_fade_durations_us(
    segment: Dict[str, Any],
    fade_materials_by_id: Dict[str, Dict[str, Any]],
) -> tuple[int, int]:
    refs = segment.get("extra_material_refs") or []
    if not isinstance(refs, list):
        refs = [refs]
    for ref in refs:
        fade_payload = fade_materials_by_id.get(str(ref))
        if fade_payload is None:
            continue
        durations = _fade_payload_durations_us(fade_payload, fade_materials_by_id)
        if durations is not None:
            return durations

    durations = _fade_payload_durations_us(
        segment.get("audio_fade"),
        fade_materials_by_id,
    )
    return durations if durations is not None else (0, 0)


def _audio_delivery_validation(
    request: RevisionRequest,
    *,
    audio_tracks: List[Dict[str, Any]],
    materials: Dict[str, Any],
    total_duration_us: int,
    doc_items: Optional[List[RevisionReviewItem]] = None,
) -> Dict[str, Any]:
    plan = request.audio_delivery_plan
    metrics: Dict[str, Any] = {
        "mode": plan.mode,
        "enabled": plan.mode == "segmented",
        "tolerance_us": _AUDIO_DELIVERY_TOLERANCE_US,
        "fade_tolerance_us": _AUDIO_DELIVERY_FADE_TOLERANCE_US,
        "volume_tolerance": _AUDIO_DELIVERY_VOLUME_TOLERANCE,
        "planned": len(plan.segments) if plan.mode == "segmented" else 0,
        "matched": 0,
        "unmatched": [],
        "mismatched": [],
        "unexpected_segments": [],
        "unexpected_segment_ids": [],
        "matched_segments": [],
        "validation_only_material_paths": [],
        "full_candidate_audio_path": "",
        "draft_duration": total_duration_us / 1_000_000.0,
        "draft_duration_us": total_duration_us,
        "max_ratio": plan.max_single_segment_ratio,
        "max_single_segment_ratio": plan.max_single_segment_ratio,
        "max_planned_narration_ratio": 0.0,
        "oversized_ids": [],
        "oversized_segment_ids": [],
        "unmatched_count": 0,
        "mismatched_count": 0,
        "unexpected_count": 0,
    }
    delivery_profile = derive_acceptance_profile(request, doc_items=doc_items)
    requires_segmented = any(
        record.get("has_review_item")
        and {"audio_precision", "audio_join"}.intersection(record.get("gates") or [])
        for record in delivery_profile["items"]
    ) or bool(request.pause_adjustments and request.pause_alignment)
    if requires_segmented and plan.mode != "segmented":
        return {
            "errors": [
                "Spoken source-ledger revisions require segmented audio delivery; "
                "a legacy/full-track narration is not an editable final delivery."
            ],
            "metrics": metrics,
        }
    if plan.mode != "segmented":
        return {"errors": [], "metrics": metrics}

    if plan.pending or not plan.segments:
        return {
            "errors": [
                "Segmented audio delivery is still pending; populate explicit source/repair "
                "segments before draft validation."
            ],
            "metrics": metrics,
        }

    errors: List[str] = []
    audio_materials = [
        item for item in (materials.get("audios", []) or []) if isinstance(item, dict)
    ]
    audio_path_by_material_id: Dict[str, str] = {}
    raw_audio_path_by_normalized_path: Dict[str, str] = {}
    actual_audio_paths: set[str] = set()
    for material in audio_materials:
        material_id = str(material.get("id") or material.get("material_id") or "")
        raw_material_path = str(
            material.get("path") or material.get("material_url") or material.get("file_path") or ""
        ).strip()
        material_path = _normalize_audio_delivery_path(raw_material_path)
        if material_id:
            audio_path_by_material_id[material_id] = material_path
        if material_path:
            actual_audio_paths.add(material_path)
            raw_audio_path_by_normalized_path.setdefault(material_path, raw_material_path)

    validation_only_paths = {
        _normalize_audio_delivery_path(path)
        for path in plan.validation_only_audio_paths
        if _normalize_audio_delivery_path(path)
    }
    raw_full_candidate_audio_path = _processed_audio_candidate_path(request)
    full_candidate_audio_path = _normalize_audio_delivery_path(raw_full_candidate_audio_path)
    metrics["full_candidate_audio_path"] = full_candidate_audio_path
    if full_candidate_audio_path:
        validation_only_paths.add(full_candidate_audio_path)
        candidate_hash_cache: Dict[str, str] = {}
        validation_only_paths.update(
            material_path
            for material_path, raw_material_path in raw_audio_path_by_normalized_path.items()
            if _audio_files_share_identity(
                raw_full_candidate_audio_path,
                raw_material_path,
                hash_cache=candidate_hash_cache,
            )
        )
    validation_only_material_paths = sorted(validation_only_paths.intersection(actual_audio_paths))
    metrics["validation_only_material_paths"] = validation_only_material_paths
    if validation_only_material_paths:
        errors.append(
            "Segmented audio delivery contains validation-only audio material(s): "
            + ", ".join(validation_only_material_paths)
        )

    fade_materials_by_id = {
        str(item.get("id")): item
        for item in (materials.get("audio_fades", []) or [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    actual_segments: List[Dict[str, Any]] = []
    for track_index, track in enumerate(audio_tracks):
        normalized_track_name = _normalize_audio_delivery_track_name(track.get("name"))
        for segment_index, segment in enumerate(track.get("segments") or []):
            if not isinstance(segment, dict):
                continue
            source_timerange = segment.get("source_timerange") or {}
            target_timerange = segment.get("target_timerange") or {}
            material_id = str(segment.get("material_id") or "")
            fade_in_us, fade_out_us = _segment_fade_durations_us(
                segment,
                fade_materials_by_id,
            )
            actual_segments.append(
                {
                    "key": (track_index, segment_index),
                    "segment_id": str(segment.get("id") or f"{track_index}:{segment_index}"),
                    "track_name": str(track.get("name") or ""),
                    "normalized_track_name": normalized_track_name,
                    "material_id": material_id,
                    "material_path": audio_path_by_material_id.get(material_id, ""),
                    "source_start": _safe_int(source_timerange.get("start", 0)),
                    "source_duration": _safe_int(source_timerange.get("duration")),
                    "target_start": _safe_int(target_timerange.get("start", 0)),
                    "target_duration": _safe_int(target_timerange.get("duration")),
                    "volume": _safe_float(segment.get("volume", 1.0)),
                    "fade_in": fade_in_us,
                    "fade_out": fade_out_us,
                }
            )

    consumed_actual_segments: set[tuple[int, int]] = set()
    unmatched_ids: List[str] = []
    mismatched_segments: List[Dict[str, Any]] = []
    matched_segments: List[Dict[str, Any]] = []

    for planned_segment in plan.segments:
        expected = {
            "source_start": _seconds_to_us(planned_segment.source_start),
            "source_duration": _seconds_to_us(planned_segment.duration),
            "target_start": _seconds_to_us(planned_segment.timeline_start),
            "target_duration": _seconds_to_us(planned_segment.duration),
            "volume": _saved_audio_delivery_volume(request, planned_segment),
            "fade_in": _seconds_to_us(planned_segment.fade_in),
            "fade_out": _seconds_to_us(planned_segment.fade_out),
        }
        normalized_track_name = _normalize_audio_delivery_track_name(planned_segment.track_name)
        normalized_asset_path = _normalize_audio_delivery_path(planned_segment.asset_path)
        candidates = [
            actual
            for actual in actual_segments
            if actual["key"] not in consumed_actual_segments
            and actual["normalized_track_name"] == normalized_track_name
            and actual["material_path"] == normalized_asset_path
        ]
        if not candidates:
            unmatched_ids.append(planned_segment.segment_id)
            continue

        def time_difference(actual: Dict[str, Any]) -> int:
            total = 0
            for field_name in (
                "source_start",
                "source_duration",
                "target_start",
                "target_duration",
            ):
                actual_value = actual[field_name]
                if actual_value is None:
                    return 2**63 - 1
                total += abs(actual_value - expected[field_name])
            return total

        selected = min(candidates, key=lambda item: (time_difference(item), item["key"]))
        consumed_actual_segments.add(selected["key"])
        mismatched_fields: List[str] = []
        for field_name in (
            "source_start",
            "source_duration",
            "target_start",
            "target_duration",
        ):
            actual_value = selected[field_name]
            if (
                actual_value is None
                or abs(actual_value - expected[field_name]) > _AUDIO_DELIVERY_TOLERANCE_US
            ):
                mismatched_fields.append(field_name)

        actual_volume = selected["volume"]
        if (
            actual_volume is None
            or abs(actual_volume - expected["volume"]) > _AUDIO_DELIVERY_VOLUME_TOLERANCE
        ):
            mismatched_fields.append("volume")

        for fade_field in ("fade_in", "fade_out"):
            planned_fade = expected[fade_field]
            actual_fade = selected[fade_field]
            if planned_fade > 0:
                fade_mismatch = actual_fade <= 0 or (
                    abs(actual_fade - planned_fade) > _AUDIO_DELIVERY_FADE_TOLERANCE_US
                )
            else:
                fade_mismatch = actual_fade != 0
            if fade_mismatch:
                mismatched_fields.append(fade_field)

        match_detail = {
            "plan_segment_id": planned_segment.segment_id,
            "role": planned_segment.role,
            "segment_id": selected["segment_id"],
            "track_name": selected["track_name"],
            "material_id": selected["material_id"],
            "material_path": selected["material_path"],
            "time_difference_us": time_difference(selected),
        }
        if mismatched_fields:
            mismatched_segments.append(
                {
                    **match_detail,
                    "fields": mismatched_fields,
                    "expected": expected,
                    "actual": {
                        key: selected[key]
                        for key in (
                            "source_start",
                            "source_duration",
                            "target_start",
                            "target_duration",
                            "volume",
                            "fade_in",
                            "fade_out",
                        )
                    },
                }
            )
        else:
            matched_segments.append(match_detail)

    metrics["matched"] = len(matched_segments)
    metrics["unmatched"] = unmatched_ids
    metrics["mismatched"] = mismatched_segments
    metrics["matched_segments"] = matched_segments
    metrics["unmatched_count"] = len(unmatched_ids)
    metrics["mismatched_count"] = len(mismatched_segments)
    if unmatched_ids:
        errors.append(
            "Segmented audio delivery is missing planned audio segment(s): "
            + ", ".join(unmatched_ids)
        )
    if mismatched_segments:
        mismatch_labels = [
            f"{item['plan_segment_id']} ({', '.join(item['fields'])})"
            for item in mismatched_segments
        ]
        errors.append(
            "Segmented audio delivery has mismatched audio segment(s): "
            + "; ".join(mismatch_labels)
        )

    unexpected_segments = [
        {
            "segment_id": actual["segment_id"],
            "track_name": actual["track_name"],
            "material_id": actual["material_id"],
            "material_path": actual["material_path"],
        }
        for actual in actual_segments
        if actual["key"] not in consumed_actual_segments
    ]
    unexpected_segment_ids = [item["segment_id"] for item in unexpected_segments]
    metrics["unexpected_segments"] = unexpected_segments
    metrics["unexpected_segment_ids"] = unexpected_segment_ids
    metrics["unexpected_count"] = len(unexpected_segments)
    if unexpected_segments:
        errors.append(
            "Segmented audio delivery contains unplanned audio segment(s): "
            + ", ".join(unexpected_segment_ids)
        )

    narration_ratios: List[tuple[str, float]] = []
    if plan.forbid_full_length_segments:
        if total_duration_us <= 0:
            errors.append(
                "Segmented audio delivery cannot enforce full-length limits because draft "
                "duration is not positive."
            )
        else:
            narration_ratios = [
                (
                    segment.segment_id,
                    _seconds_to_us(segment.duration) / total_duration_us,
                )
                for segment in plan.segments
                if segment.role in _AUDIO_DELIVERY_NARRATION_ROLES
            ]
    max_planned_narration_ratio = max(
        (ratio for _, ratio in narration_ratios),
        default=0.0,
    )
    pause_points = [pause.source_time for pause in request.pause_adjustments]
    narration_segment_by_id = {
        segment.segment_id: segment
        for segment in plan.segments
        if segment.role in _AUDIO_DELIVERY_NARRATION_ROLES
    }
    narration_role_counts: Dict[str, int] = {}
    for segment in narration_segment_by_id.values():
        narration_role_counts[segment.role] = narration_role_counts.get(segment.role, 0) + 1
    oversized_segment_ids = []
    for segment_id, ratio in narration_ratios:
        if ratio < plan.max_single_segment_ratio:
            continue
        segment = narration_segment_by_id[segment_id]
        source_end = segment.source_start + segment.duration
        is_required_pause_split = narration_role_counts.get(segment.role, 0) > 1 and any(
            abs(pause_point - segment.source_start) <= 1e-3 or abs(pause_point - source_end) <= 1e-3
            for pause_point in pause_points
        )
        if not is_required_pause_split:
            oversized_segment_ids.append(segment_id)
    metrics["max_planned_narration_ratio"] = max_planned_narration_ratio
    metrics["oversized_ids"] = oversized_segment_ids
    metrics["oversized_segment_ids"] = oversized_segment_ids
    if oversized_segment_ids:
        errors.append(
            "Segmented audio delivery contains near-full-length planned narration "
            "segment(s): " + ", ".join(oversized_segment_ids)
        )

    return {"errors": errors, "metrics": metrics}


def _segment_end_us(segment: Dict[str, Any]) -> int:
    timerange = segment.get("target_timerange") or {}
    start_us = int(timerange.get("start", 0) or 0)
    duration_us = int(timerange.get("duration", 0) or 0)
    return start_us + duration_us


def _looks_like_flattened_preview_track(track: Dict[str, Any]) -> bool:
    return _normalize_track_name(track.get("name")) in {"final video", "final audio"}


def _track_has_single_full_length_segment(track: Dict[str, Any], total_duration_us: int) -> bool:
    segments = track.get("segments") or []
    if len(segments) != 1:
        return False
    segment = segments[0]
    timerange = segment.get("target_timerange") or {}
    start_us = int(timerange.get("start", 0) or 0)
    duration_us = int(timerange.get("duration", 0) or 0)
    if total_duration_us <= 0:
        return False
    return start_us == 0 and duration_us >= total_duration_us


def _review_item_role(item_id: str, source_text: str) -> str:
    normalized = f"{item_id} {source_text}"
    if _REVIEW_ID_PATTERN.search(normalized):
        match = _REVIEW_ID_PATTERN.search(normalized)
        return str(match.group(1)) if match else ""
    return ""


def _status_from_mapping(payload: Dict[str, Any]) -> str:
    for key in ("status", "result", "state", "validation_status", "execution_status"):
        value = payload.get(key)
        if value is not None:
            return str(value).strip().lower()
    return ""


def _status_is_failure(payload: Dict[str, Any]) -> bool:
    return _status_from_mapping(payload) in _FAIL_STATUSES


def _status_is_pass(payload: Dict[str, Any]) -> bool:
    return _status_from_mapping(payload) in _PASS_STATUSES


def _audio_validation_status(validation: Dict[str, Any], evidence: Dict[str, Any]) -> str:
    if validation:
        status = _status_from_mapping(validation)
        if status:
            return status
    if evidence:
        nested = evidence.get("validation")
        if isinstance(nested, dict):
            status = _status_from_mapping(nested)
            if status:
                return status
        status = _status_from_mapping(evidence)
        if status and status != "executed":
            return status
    return ""


def _audio_validation_is_pass(validation: Dict[str, Any], evidence: Dict[str, Any]) -> bool:
    return _audio_validation_status(validation, evidence) in _AUDIO_VALIDATION_PASS_STATUSES


def _evidence_has_execution(evidence: Dict[str, Any]) -> bool:
    if not evidence or _status_is_failure(evidence):
        return False
    if _as_bool(evidence.get("executed"), False) or _as_bool(evidence.get("timeline_edit"), False):
        return True
    for key in (
        "operation",
        "edit_type",
        "cut_window",
        "cut_windows",
        "segment_id",
        "segment_ids",
        "track_name",
        "track_names",
        "material_id",
        "material_ids",
        "asset_path",
        "asset_paths",
        "overlay_track",
        "overlay_segment",
        "validation_report",
    ):
        value = evidence.get(key)
        if value:
            return True
    return _status_is_pass(evidence)


def _evidence_has_validation(validation: Dict[str, Any], evidence: Dict[str, Any]) -> bool:
    if validation and not _status_is_failure(validation) and _status_is_pass(validation):
        return True
    if evidence and not _status_is_failure(evidence):
        nested = evidence.get("validation")
        if isinstance(nested, dict) and _status_is_pass(nested):
            return True
        return _status_is_pass(evidence)
    return False


def _processed_audio_summary_path(request: RevisionRequest) -> str:
    processed_audio = request.processed_audio if isinstance(request.processed_audio, dict) else {}
    for key in ("validation_summary", "summary", "reverse_validation_summary"):
        value = str(processed_audio.get(key) or "").strip()
        if value:
            return value
    outputs = processed_audio.get("outputs")
    if isinstance(outputs, dict):
        for key in ("validation_summary", "summary", "reverse_validation_summary"):
            value = str(outputs.get(key) or "").strip()
            if value:
                return value
    return ""


def _processed_audio_candidate_path(request: RevisionRequest) -> str:
    processed_audio = request.processed_audio if isinstance(request.processed_audio, dict) else {}
    for key in (
        "output_wav",
        "output_audio",
        "audio_path",
        "path",
        "final_audio",
        "final_candidate_audio",
    ):
        value = str(processed_audio.get(key) or "").strip()
        if value:
            return value
    outputs = processed_audio.get("outputs")
    if isinstance(outputs, dict):
        for key in ("output_wav", "output_audio", "audio", "wav", "final_candidate_audio"):
            value = str(outputs.get(key) or "").strip()
            if value:
                return value
    return ""


def _sha256_file(path: str) -> str:
    return sha256_file(path)


def _audio_files_share_identity(
    left_path: str,
    right_path: str,
    *,
    hash_cache: Optional[Dict[str, str]] = None,
) -> bool:
    left = str(left_path or "").strip()
    right = str(right_path or "").strip()
    if not left or not right:
        return False
    if _normalize_audio_delivery_path(left) == _normalize_audio_delivery_path(right):
        return True
    if not os.path.isfile(left) or not os.path.isfile(right):
        return False
    try:
        if os.path.samefile(left, right):
            return True
    except OSError:
        pass
    try:
        if os.path.getsize(left) != os.path.getsize(right):
            return False
    except OSError:
        return False

    cache = hash_cache if hash_cache is not None else {}

    def digest(path: str) -> str:
        key = os.path.normcase(os.path.realpath(path))
        if key not in cache:
            cache[key] = _sha256_file(path)
        return cache[key]

    return digest(left) == digest(right)


def _reverse_asr_identity(payload: Dict[str, Any]) -> Dict[str, str]:
    nested = payload.get("asr_identity")
    if not isinstance(nested, dict):
        nested = payload.get("asr") if isinstance(payload.get("asr"), dict) else {}
    provider = str(nested.get("provider") or payload.get("asr_provider") or "").strip()
    model = str(
        nested.get("model")
        or nested.get("model_id")
        or nested.get("resource_id")
        or payload.get("asr_model")
        or payload.get("asr_model_id")
        or payload.get("asr_resource_id")
        or ""
    ).strip()
    adapter = str(
        nested.get("adapter_version")
        or payload.get("asr_adapter_version")
        or payload.get("adapter_version")
        or ""
    ).strip()
    return {"provider": provider, "model": model, "adapter_version": adapter}


def _audio_delivery_plan_digest(request: RevisionRequest) -> str:
    return audio_delivery_plan_sha256(request)


def _reverse_asr_result_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in ("rows", "items", "results", "result_rows"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _semantic_pause_reverse_asr_problems(
    payload: Dict[str, Any],
    item_ids: Sequence[str],
    *,
    source_anchors_by_id: Optional[Dict[str, tuple[str, str]]] = None,
) -> List[str]:
    if not item_ids:
        return []

    rows_by_id: Dict[str, Dict[str, Any]] = {}
    for row in _reverse_asr_result_rows(payload):
        row_id = _normalize_review_id(_row_item_label(row))
        if row_id and row_id not in rows_by_id:
            rows_by_id[row_id] = row

    report_candidate_sha256 = str(payload.get("candidate_audio_sha256") or "").strip().lower()
    problems: List[str] = []
    for raw_item_id in item_ids:
        item_id = str(raw_item_id or "").strip()
        row = rows_by_id.get(_normalize_review_id(item_id))
        prefix = f"Semantic pause {item_id or '<unidentified>'}"
        if row is None:
            problems.append(f"{prefix} is missing a full-candidate reverse ASR result row.")
            continue
        proof = row.get("reverse_asr_evidence")
        if not isinstance(proof, dict):
            problems.append(f"{prefix} is missing attributable reverse-ASR edge evidence.")
            continue

        proof_candidate_sha256 = str(proof.get("candidate_audio_sha256") or "").strip().lower()
        if not report_candidate_sha256 or proof_candidate_sha256 != report_candidate_sha256:
            problems.append(f"{prefix} reverse-ASR evidence is not bound to the candidate SHA-256.")

        reverse_status = str(proof.get("full_candidate_reverse_asr_status") or "").strip().lower()
        if reverse_status not in (_PASS_STATUSES | {"success"}):
            problems.append(f"{prefix} full-candidate reverse ASR did not succeed.")

        previous_anchor = str(proof.get("previous_protected_trailing_anchor") or "").strip()
        next_anchor = str(proof.get("next_protected_leading_anchor") or "").strip()
        previous_match = proof.get("previous_utterance_match")
        next_match = proof.get("next_utterance_match")
        previous_text = (
            str(previous_match.get("text") or "") if isinstance(previous_match, dict) else ""
        )
        next_text = str(next_match.get("text") or "") if isinstance(next_match, dict) else ""
        expected_anchors = (source_anchors_by_id or {}).get(_normalize_review_id(item_id))
        if (
            not expected_anchors
            or not all(expected_anchors)
            or previous_anchor != expected_anchors[0]
            or next_anchor != expected_anchors[1]
        ):
            problems.append(f"{prefix} reverse-ASR anchors are not source-bound.")

        if (
            proof.get("previous_utterance_preserved") is not True
            or proof.get("previous_protected_trailing_anchor_present") is not True
            or not previous_anchor
            or previous_anchor not in previous_text
        ):
            problems.append(f"{prefix} did not preserve the preceding sentence tail.")
        if (
            proof.get("next_utterance_preserved") is not True
            or proof.get("next_protected_leading_anchor_present") is not True
            or not next_anchor
            or next_anchor not in next_text
        ):
            problems.append(f"{prefix} did not preserve the following sentence onset.")
        if proof.get("surrounding_utterance_order_valid") is not True:
            problems.append(f"{prefix} surrounding utterance order is not proven.")
        overlaps = proof.get("reverse_asr_word_overlaps_hold")
        if proof.get("no_asr_word_overlaps_hold") is not True or overlaps != []:
            problems.append(f"{prefix} has reverse-ASR word overlap inside the silent hold.")

    return problems


def _semantic_pause_source_anchors(
    request: RevisionRequest,
    item_ids: Sequence[str],
) -> Dict[str, tuple[str, str]]:
    wanted = {_normalize_review_id(item_id) for item_id in item_ids}
    anchors: Dict[str, tuple[str, str]] = {}

    proof_candidates: List[tuple[str, Dict[str, Any]]] = []
    for adjustment in request.pause_adjustments:
        proof_candidates.append(
            (
                adjustment.item_id,
                {
                    "requested_source_time": adjustment.requested_source_time,
                    "source_time": adjustment.source_time,
                    "frame_source_time": adjustment.frame_source_time,
                    "boundary_evidence": adjustment.boundary_evidence,
                },
            )
        )
    for item in request.review_items:
        for payload in (item.evidence, item.validation):
            proof = payload.get("semantic_pause_adjustment") if isinstance(payload, dict) else None
            if isinstance(proof, dict):
                proof_candidates.append((item.item_id, proof))

    for item_id, proof in proof_candidates:
        normalized_id = _normalize_review_id(item_id)
        if normalized_id not in wanted or normalized_id in anchors:
            continue
        boundary = proof.get("boundary_evidence")
        if not isinstance(boundary, dict):
            continue
        source_asr_path = str(boundary.get("source_asr_path") or "").strip()
        expected_sha256 = str(boundary.get("source_asr_sha256") or "").strip().casefold()
        requested = _safe_float(boundary.get("requested_time"))
        if (
            not source_asr_path
            or requested is None
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        ):
            continue
        try:
            with open(source_asr_path, "rb") as source_file:
                source_bytes = source_file.read()
            if hashlib.sha256(source_bytes).hexdigest() != expected_sha256:
                continue
            source_payload = json.loads(source_bytes.decode("utf-8-sig"))
            recomputed = resolve_pause_boundary(
                requested,
                source_payload,
                min_gap_seconds=_safe_float(boundary.get("min_gap_seconds")) or 0.35,
                search_window_seconds=(_safe_float(boundary.get("search_window_seconds")) or 3.0),
                tolerance_seconds=_safe_float(boundary.get("tolerance_seconds")) or 0.005,
                semantic_gap_seconds=(_safe_float(boundary.get("semantic_gap_seconds")) or 0.8),
                edge_guard_seconds=(
                    _safe_float(boundary.get("minimum_edge_guard_seconds")) or 0.05
                ),
            )
            previous_text = recomputed.previous_utterance_text
            next_text = recomputed.next_utterance_text
        except (OSError, UnicodeError, json.JSONDecodeError, PauseAlignmentError):
            continue
        anchors[normalized_id] = (
            protected_utterance_anchor(previous_text, leading=False),
            protected_utterance_anchor(next_text, leading=True),
        )
    return anchors


def _reverse_asr_result_count(
    payload: Dict[str, Any], *, seen_paths: Optional[set[str]] = None
) -> int:
    return len(_reverse_asr_result_rows(payload))


def _collect_processed_audio_unresolved_rows(
    payload: Dict[str, Any],
    *,
    source: str = "",
    seen_paths: Optional[set[str]] = None,
) -> Dict[str, List[str]]:
    unresolved_statuses: List[str] = []
    for aggregate_key in ("status_counts", "summary"):
        status_counts = payload.get(aggregate_key)
        if not isinstance(status_counts, dict):
            continue
        for status, count in status_counts.items():
            try:
                numeric_count = int(count)
            except (TypeError, ValueError):
                numeric_count = 0
            normalized_status = str(status or "").strip().lower()
            if numeric_count > 0 and normalized_status not in _AUDIO_VALIDATION_PASS_STATUSES:
                label = f"{normalized_status}:{numeric_count}"
                unresolved_statuses.append(f"{source}:{label}" if source else label)

    unresolved_ids: List[str] = []
    for key in (
        "fail_ids",
        "review_ids",
        "pending_ids",
        "needs_review_ids",
        "unresolved_ids",
        "pending_time_parse_ids",
    ):
        ids = payload.get(key)
        if isinstance(ids, list):
            unresolved_ids.extend(str(item) for item in ids if str(item).strip())

    for row in _reverse_asr_result_rows(payload):
        status = str(row.get("status") or "").strip().lower()
        if not status or status not in _AUDIO_VALIDATION_PASS_STATUSES:
            row_id = str(
                row.get("item")
                or row.get("id")
                or row.get("doc_item_id")
                or status
                or "missing_status"
            ).strip()
            if row_id:
                unresolved_ids.append(f"{source}:{row_id}" if source else row_id)

    nested_reports = payload.get("reverse_asr_reports")
    if isinstance(nested_reports, list):
        if seen_paths is None:
            seen_paths = set()
        for nested in nested_reports:
            nested_path = str(nested or "").strip()
            if not nested_path or nested_path in seen_paths:
                continue
            seen_paths.add(nested_path)
            if not os.path.exists(nested_path):
                unresolved_ids.append(f"missing_report:{nested_path}")
                continue
            nested_payload = _load_json(nested_path)
            nested_source = os.path.basename(nested_path)
            nested_result = _collect_processed_audio_unresolved_rows(
                nested_payload,
                source=nested_source,
                seen_paths=seen_paths,
            )
            unresolved_statuses.extend(nested_result["unresolved_statuses"])
            unresolved_ids.extend(nested_result["unresolved_ids"])

    return {
        "unresolved_statuses": sorted(set(unresolved_statuses)),
        "unresolved_ids": sorted(set(unresolved_ids)),
    }


def _row_item_label(row: Dict[str, Any], fallback: str = "row") -> str:
    return str(
        row.get("id")
        or row.get("item")
        or row.get("doc_item_id")
        or row.get("item_id")
        or row.get("clip_id")
        or fallback
    ).strip()


def _semantic_join_adjudication_is_attributable(validation: Dict[str, Any]) -> bool:
    final_gap = _safe_float(validation.get("final_gap"))
    return bool(
        str(validation.get("status") or "").strip().casefold() == "pass_adjudicated"
        and str(validation.get("reason") or "").strip()
        and final_gap is not None
        and math.isfinite(final_gap)
        and 0.0 <= final_gap <= 0.2
        and str(validation.get("no_extra_deletion_contract") or "").strip().casefold() == "pass"
    )


def _collect_semantic_join_anomalies(
    payload: Dict[str, Any],
    *,
    source: str = "",
    seen_paths: Optional[set[str]] = None,
) -> List[str]:
    anomalies: List[str] = []
    for idx, row in enumerate(_reverse_asr_result_rows(payload)):
        row_id = _row_item_label(row, f"row{idx + 1}")
        prefix = f"{source}:{row_id}" if source else row_id
        text_fields = (row.get(field) for field in _LOCAL_TRANSCRIPT_FIELDS)
        local_text = "\n".join(str(value or "") for value in text_fields if value)
        semantic_validation = row.get("semantic_join_validation")
        if semantic_validation is None:
            semantic_validation = row.get("semantic_validation")
        semantic_status = ""
        adjudicated_patterns: set[str] = set()
        if isinstance(semantic_validation, dict):
            semantic_status = str(semantic_validation.get("status") or "").strip().lower()
            if _semantic_join_adjudication_is_attributable(semantic_validation):
                patterns_payload = (
                    semantic_validation.get("adjudicated_patterns")
                    or semantic_validation.get("adjudicated_phrases")
                    or semantic_validation.get("adjudicated_forbidden_phrases")
                    or []
                )
                if isinstance(patterns_payload, str):
                    patterns_payload = [patterns_payload]
                if isinstance(patterns_payload, list):
                    adjudicated_patterns = {
                        str(pattern or "").strip()
                        for pattern in patterns_payload
                        if str(pattern or "").strip()
                    }
        for phrase in _SEMANTIC_JOIN_FORBIDDEN_PHRASES:
            if phrase and phrase in local_text:
                if phrase not in adjudicated_patterns:
                    anomalies.append(f"{prefix}:semantic_join:{phrase}")

        keep_hits = row.get("keep_hits")
        if isinstance(keep_hits, dict):
            for keep, hit in keep_hits.items():
                if hit is False:
                    anomalies.append(f"{prefix}:must_keep_missing:{keep}")

        if isinstance(semantic_validation, dict):
            if not semantic_status or semantic_status not in _PASS_STATUSES:
                anomalies.append(
                    f"{prefix}:semantic_join_status:{semantic_status or 'missing_status'}"
                )

    scan_payload = payload.get("semantic_join_scan") or payload.get("semantic_join_validation")
    if isinstance(scan_payload, dict):
        scan_rows = scan_payload.get("rows") or scan_payload.get("items") or []
        if isinstance(scan_rows, list):
            for idx, row in enumerate(scan_rows):
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status") or "").strip().lower()
                if not status or status not in _PASS_STATUSES:
                    row_id = _row_item_label(row, f"semantic{idx + 1}")
                    reason = str(
                        row.get("reason") or row.get("pattern") or status or "missing_status"
                    ).strip()
                    prefix = f"{source}:{row_id}" if source else row_id
                    anomalies.append(f"{prefix}:semantic_join_scan:{reason}")

    nested_reports = payload.get("reverse_asr_reports")
    if isinstance(nested_reports, list):
        if seen_paths is None:
            seen_paths = set()
        for nested in nested_reports:
            nested_path = str(nested or "").strip()
            if not nested_path or nested_path in seen_paths:
                continue
            seen_paths.add(nested_path)
            if not os.path.exists(nested_path):
                continue
            nested_payload = _load_json(nested_path)
            nested_source = os.path.basename(nested_path)
            anomalies.extend(
                _collect_semantic_join_anomalies(
                    nested_payload,
                    source=nested_source,
                    seen_paths=seen_paths,
                )
            )

    return sorted(set(anomalies))


def _segmented_candidate_duration_bounds(request: RevisionRequest) -> Optional[tuple[float, float]]:
    plan = request.audio_delivery_plan
    if plan.mode != "segmented" or plan.pending:
        return None
    audible_segments = [
        segment
        for segment in plan.segments
        if segment.role != "reference" and segment.volume > _AUDIO_DELIVERY_VOLUME_TOLERANCE
    ]
    if not audible_segments:
        return None
    timeline_end = max(segment.timeline_start + segment.duration for segment in audible_segments)
    ordered_segments = sorted(
        audible_segments,
        key=lambda segment: (segment.timeline_start, segment.segment_id),
    )
    crossfade_allowance = 0.0
    for previous, current in zip(ordered_segments, ordered_segments[1:]):
        previous_end = previous.timeline_start + previous.duration
        if abs(current.timeline_start - previous_end) > 1e-3:
            continue
        crossfade_allowance += min(previous.fade_out, current.fade_in)
    duration_tolerance = _AUDIO_DELIVERY_TOLERANCE_US / 1_000_000
    return (
        max(0.0, timeline_end - crossfade_allowance - duration_tolerance),
        timeline_end + duration_tolerance,
    )


def _phrase_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _normalized_phrase_text(value: Any) -> str:
    return "".join(character.casefold() for character in str(value or "") if character.isalnum())


def _transcript_supports_phrase(transcript: str, phrase: str) -> bool:
    normalized_phrase = _normalized_phrase_text(phrase)
    if not normalized_phrase:
        return False
    minimum_core_length = (
        len(normalized_phrase)
        if len(normalized_phrase) <= 2
        else max(2, len(normalized_phrase) - 2)
    )
    for leading_trim in range(3):
        for trailing_trim in range(3):
            end = len(normalized_phrase) - trailing_trim if trailing_trim else None
            core = normalized_phrase[leading_trim:end]
            if len(core) >= minimum_core_length and core in transcript:
                return True
    return False


def _overlapping_phrase_occurrence_count(text: str, phrase: str) -> int:
    if not phrase or len(phrase) > len(text):
        return 0
    return sum(text.startswith(phrase, index) for index in range(len(text) - len(phrase) + 1))


def _positive_delete_hit_phrases(delete_hits: Any) -> List[str]:
    phrases: List[str] = []
    if isinstance(delete_hits, list):
        for value in delete_hits:
            if not bool(value):
                continue
            phrase = (
                value.get("text") or value.get("phrase") or value.get("delete") or ""
                if isinstance(value, dict)
                else value
            )
            phrases.append(_normalized_phrase_text(phrase))
    elif isinstance(delete_hits, dict):
        for key, value in delete_hits.items():
            if not bool(value):
                continue
            phrase = (
                value.get("text") or value.get("phrase") or value.get("delete") or key
                if isinstance(value, dict)
                else key
            )
            phrases.append(_normalized_phrase_text(phrase))
    return phrases


def _has_attributable_kept_recurrence_adjudication(
    adjudication: Any,
    *,
    expected_delete: str,
    normalized_transcript: str,
    row_status: str,
) -> bool:
    if row_status != "pass_adjudicated" or not isinstance(adjudication, dict):
        return False
    if str(adjudication.get("classification") or "").strip().casefold() != "kept_recurrence":
        return False
    if str(adjudication.get("occurrence_role") or "").strip().casefold() not in {
        "earlier_kept_occurrence",
        "later_kept_occurrence",
    }:
        return False
    expected_phrase = _normalized_phrase_text(expected_delete)
    adjudicated_phrase = _normalized_phrase_text(adjudication.get("phrase"))
    local_context = _normalized_phrase_text(adjudication.get("local_context"))
    context_anchor = _normalized_phrase_text(adjudication.get("context_anchor"))
    reason = str(adjudication.get("reason") or "").strip()
    context_without_delete = (
        local_context.replace(expected_phrase, "", 1) if expected_phrase else local_context
    )
    return bool(
        expected_phrase
        and adjudicated_phrase == expected_phrase
        and local_context
        and local_context in normalized_transcript
        and expected_phrase in local_context
        and context_anchor
        and context_anchor != expected_phrase
        and context_anchor not in expected_phrase
        and expected_phrase not in context_anchor
        and context_anchor in context_without_delete
        and reason
    )


def _spoken_item_contract(request: RevisionRequest, item_id: str) -> Dict[str, Any]:
    normalized_id = _normalize_review_id(item_id)
    item = next(
        (
            candidate
            for candidate in request.review_items
            if _normalize_review_id(candidate.item_id) == normalized_id
        ),
        None,
    )
    evidence = item.evidence if item is not None and isinstance(item.evidence, dict) else {}
    nested_validation = evidence.get("validation")
    reverse_row = (
        nested_validation.get("reverse_row")
        if isinstance(nested_validation, dict)
        and isinstance(nested_validation.get("reverse_row"), dict)
        else {}
    )
    strategy_present = "strategy" in evidence or "strategy" in reverse_row
    delete_present = "delete" in evidence or "delete" in reverse_row
    must_keep_present = "must_keep" in evidence or "must_keep" in reverse_row
    strategy_value = (
        evidence.get("strategy") if "strategy" in evidence else reverse_row.get("strategy")
    )
    delete_value = evidence.get("delete") if "delete" in evidence else reverse_row.get("delete")
    must_keep_value = (
        evidence.get("must_keep") if "must_keep" in evidence else reverse_row.get("must_keep")
    )
    windows = sorted(
        [
            [float(edit.start), float(edit.end)]
            for edit in request.edits
            if edit.op_type == "delete"
            and _normalize_review_id(edit.doc_item_id) == normalized_id
            and edit.end > edit.start
        ],
        key=lambda window: (window[0], window[1]),
    )
    if not windows:
        raw_windows = evidence.get("cut_windows")
        if raw_windows is None and isinstance(evidence.get("cut_window"), (list, tuple)):
            raw_windows = [evidence.get("cut_window")]
        if isinstance(raw_windows, list):
            for raw_window in raw_windows:
                if not isinstance(raw_window, (list, tuple)) or len(raw_window) < 2:
                    continue
                start = _safe_float(raw_window[0])
                end = _safe_float(raw_window[1])
                if start is not None and end is not None and end > start:
                    windows.append([start, end])
            windows.sort(key=lambda window: (window[0], window[1]))
    return {
        "strategy": str(strategy_value or "").strip(),
        "strategy_present": strategy_present,
        "delete": str(delete_value or "").strip(),
        "delete_present": delete_present,
        "must_keep": _phrase_list(must_keep_value),
        "must_keep_present": must_keep_present,
        "source_cut_windows": windows,
    }


_SPOKEN_DELETE_CONTRACT_KINDS = {
    "spoken_delete",
    "speech_delete",
    "audio_delete",
    "phrase_delete",
    "range_delete",
    "ellipsis_range_delete",
    "colored_span_delete",
    "gap_delete",
    "tail_cleanup",
    "speech_tail_cleanup",
    "tail_particle_delete",
}


def _item_requires_spoken_delete_contract(request: RevisionRequest, item_id: str) -> bool:
    normalized_id = _normalize_review_id(item_id)
    for item in request.review_items:
        if _normalize_review_id(item.item_id) != normalized_id:
            continue
        if str(item.kind or "").strip().casefold() in _SPOKEN_DELETE_CONTRACT_KINDS:
            return True
    return any(
        _normalize_review_id(edit.doc_item_id) == normalized_id
        and str(edit.source_kind or "").strip().casefold() in _SPOKEN_DELETE_CONTRACT_KINDS
        for edit in request.edits
    )


def _candidate_segment_boundaries(request: RevisionRequest) -> List[Dict[str, Any]]:
    audible_segments = sorted(
        (
            segment
            for segment in request.audio_delivery_plan.segments
            if segment.role != "reference" and segment.volume > _AUDIO_DELIVERY_VOLUME_TOLERANCE
        ),
        key=lambda segment: (segment.timeline_start, segment.segment_id),
    )
    overlap_before = 0.0
    boundaries: List[Dict[str, Any]] = []
    previous = None
    for segment in audible_segments:
        if previous is not None:
            previous_end = previous.timeline_start + previous.duration
            if abs(segment.timeline_start - previous_end) <= 1e-3:
                overlap_before += min(previous.fade_out, segment.fade_in)
        candidate_start = segment.timeline_start - overlap_before
        boundaries.append(
            {
                "source_start": segment.source_start,
                "source_end": segment.source_start + segment.duration,
                "candidate_start": candidate_start,
                "candidate_end": candidate_start + segment.duration,
            }
        )
        previous = segment
    return boundaries


def _candidate_join_times_for_window(
    request: RevisionRequest,
    source_window: Sequence[float],
) -> List[float]:
    start, end = float(source_window[0]), float(source_window[1])
    candidates: List[float] = []
    for segment in _candidate_segment_boundaries(request):
        if abs(segment["source_end"] - start) <= 1e-3:
            candidates.append(float(segment["candidate_end"]))
        if abs(segment["source_start"] - end) <= 1e-3:
            candidates.append(float(segment["candidate_start"]))
    return list(dict.fromkeys(round(candidate, 9) for candidate in candidates))


def _spoken_reverse_asr_row_evidence_problems(
    row: Dict[str, Any],
    *,
    request: Optional[RevisionRequest] = None,
    item_id: str = "",
) -> List[str]:
    missing: List[str] = []
    if not str(row.get("strategy") or "").strip():
        missing.append("strategy")
    source_windows = row.get("source_cut_windows")
    if not isinstance(source_windows, list) or not source_windows:
        missing.append("source_cut_windows")
    mapped_joins = row.get("mapped_join_times")
    if not isinstance(mapped_joins, list) or not mapped_joins:
        missing.append("mapped_join_times")
    transcript_aliases = [
        (field, str(row.get(field) or "").strip())
        for field in _LOCAL_TRANSCRIPT_FIELDS
        if str(row.get(field) or "").strip()
    ]
    transcript = transcript_aliases[0][1] if transcript_aliases else ""
    normalized_transcript_aliases = {
        _normalized_phrase_text(value) for _, value in transcript_aliases
    }
    if not transcript:
        missing.append("local transcript")
    elif not _normalized_phrase_text(transcript):
        missing.append("local transcript has no alphanumeric content")
    elif len(normalized_transcript_aliases) > 1:
        missing.append("local transcript aliases disagree")
    if not isinstance(row.get("delete_hits"), (list, dict)):
        missing.append("delete_hits")
    if not isinstance(row.get("keep_hits"), dict):
        missing.append("keep_hits")
    semantic_validation = row.get("semantic_join_validation")
    semantic_status = (
        str(semantic_validation.get("status") or "").strip().casefold()
        if isinstance(semantic_validation, dict)
        else ""
    )
    if semantic_status not in _PASS_STATUSES:
        missing.append("semantic_join_validation")
    if request is None or not item_id:
        return missing

    contract = _spoken_item_contract(request, item_id)
    contract_problems: List[str] = []
    expected_strategy = contract["strategy"]
    if not contract["strategy_present"] or not expected_strategy:
        contract_problems.append("strategy is missing")
    elif str(row.get("strategy") or "").strip() != expected_strategy:
        contract_problems.append("strategy does not match item contract")
    expected_delete = contract["delete"]
    if not contract["delete_present"] or not expected_delete:
        contract_problems.append("delete phrase is missing")
    elif str(row.get("delete") or "").strip() != expected_delete:
        contract_problems.append("delete phrase does not match item contract")
    expected_must_keep = contract["must_keep"]
    if not contract["must_keep_present"]:
        contract_problems.append("must_keep field is missing")
    elif "must_keep" not in row or set(_phrase_list(row.get("must_keep"))) != set(
        expected_must_keep
    ):
        contract_problems.append("must_keep phrases do not match item contract")

    raw_windows = row.get("source_cut_windows")
    normalized_windows: List[List[float]] = []
    if isinstance(raw_windows, list):
        for raw_window in raw_windows:
            if not isinstance(raw_window, (list, tuple)) or len(raw_window) < 2:
                continue
            start = _safe_float(raw_window[0])
            end = _safe_float(raw_window[1])
            if start is not None and end is not None and end > start:
                normalized_windows.append([start, end])
    normalized_windows.sort(key=lambda window: (window[0], window[1]))
    expected_windows = contract["source_cut_windows"]
    if expected_windows and (
        len(normalized_windows) != len(expected_windows)
        or any(
            abs(actual[0] - expected[0]) > 1e-3 or abs(actual[1] - expected[1]) > 1e-3
            for actual, expected in zip(normalized_windows, expected_windows)
        )
    ):
        contract_problems.append("source cut windows do not match item contract")

    mapped_joins = row.get("mapped_join_times")
    normalized_joins = (
        [_safe_float(value) for value in mapped_joins] if isinstance(mapped_joins, list) else []
    )
    if expected_windows and (
        len(normalized_joins) != len(expected_windows)
        or any(value is None or value < 0 for value in normalized_joins)
    ):
        contract_problems.append("mapped join times do not match item contract")
    elif expected_windows and request.audio_delivery_plan.mode == "segmented":
        join_tolerance = _AUDIO_DELIVERY_TOLERANCE_US / 1_000_000
        for source_window, mapped_join in zip(expected_windows, normalized_joins):
            assert mapped_join is not None
            candidates = _candidate_join_times_for_window(request, source_window)
            if (
                not candidates
                or min(abs(mapped_join - candidate) for candidate in candidates) > join_tolerance
            ):
                contract_problems.append("mapped join times do not match item contract")
                break

    normalized_transcript = _normalized_phrase_text(transcript)
    delete_hits = row.get("delete_hits")
    positive_delete_hit_phrases = _positive_delete_hit_phrases(delete_hits)
    positive_delete_hit_count = len(positive_delete_hit_phrases)
    positive_delete_hits = positive_delete_hit_count > 0
    row_status = str(row.get("status") or "").strip().casefold()
    adjudication = row.get("delete_hit_adjudication") or row.get("adjudication")
    has_structured_adjudication = _has_attributable_kept_recurrence_adjudication(
        adjudication,
        expected_delete=expected_delete,
        normalized_transcript=normalized_transcript,
        row_status=row_status,
    )
    normalized_delete = _normalized_phrase_text(expected_delete)
    if positive_delete_hits and any(
        phrase != normalized_delete for phrase in positive_delete_hit_phrases
    ):
        contract_problems.append("positive delete_hit does not match the item delete phrase")
    transcript_delete_occurrence_count = (
        _overlapping_phrase_occurrence_count(normalized_transcript, normalized_delete)
        if normalized_delete
        else 0
    )
    has_attributable_adjudication = (
        has_structured_adjudication
        and positive_delete_hit_count == 1
        and transcript_delete_occurrence_count == 1
    )
    if has_structured_adjudication and positive_delete_hit_count == 0:
        contract_problems.append(
            "structured kept-recurrence adjudication requires exactly one positive delete_hit"
        )
    elif positive_delete_hit_count > 1:
        contract_problems.append(
            "multiple positive delete_hits require per-hit adjudication evidence"
        )
    if transcript_delete_occurrence_count > 1:
        contract_problems.append(
            "multiple local transcript delete occurrences require per-hit adjudication evidence"
        )
    delete_phrase_in_transcript = bool(
        normalized_delete and normalized_delete in normalized_transcript
    )
    if positive_delete_hits and not has_attributable_adjudication:
        contract_problems.append(
            "delete_hits show the requested phrase remains without a structured "
            "kept-recurrence adjudication"
        )
    if delete_phrase_in_transcript and not has_attributable_adjudication:
        contract_problems.append(
            "local transcript contains the item delete phrase without a structured "
            "kept-recurrence adjudication"
        )

    keep_hits = row.get("keep_hits") if isinstance(row.get("keep_hits"), dict) else {}
    if any(keep_hits.get(phrase) is not True for phrase in expected_must_keep):
        contract_problems.append("keep_hits do not prove the item must_keep contract")
    if expected_must_keep and not all(
        _transcript_supports_phrase(normalized_transcript, phrase) for phrase in expected_must_keep
    ):
        contract_problems.append("local transcript does not contain every item must_keep phrase")
    missing.extend(f"item contract: {problem}" for problem in contract_problems)
    return missing


def _non_wave_candidate_decodes_fully(path: str) -> bool:
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                path,
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _validate_processed_audio_summary(
    request: RevisionRequest,
    *,
    required: bool = True,
    semantic_pause_item_ids: Sequence[str] = (),
    required_item_ids: Sequence[str] = (),
    spoken_contract_item_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    path = _processed_audio_summary_path(request)
    if not path:
        return {
            "path": "",
            "errors": (
                ["Spoken-audio acceptance requires a full-candidate reverse ASR report."]
                if required
                else []
            ),
            "unresolved_statuses": [],
            "unresolved_ids": [],
            "semantic_join_anomalies": [],
            "semantic_pause_reverse_asr_problems": [],
            "item_errors": {},
            "candidate_audio_path": "",
            "candidate_audio_sha256": "",
            "candidate_audio_duration_seconds": None,
            "asr_identity": {},
            "result_count": 0,
        }
    if not os.path.exists(path):
        return {
            "path": path,
            "errors": [f"Processed audio validation summary does not exist: {path}."],
            "unresolved_statuses": [],
            "unresolved_ids": [],
            "semantic_join_anomalies": [],
            "semantic_pause_reverse_asr_problems": [],
            "item_errors": {},
            "candidate_audio_path": "",
            "candidate_audio_sha256": "",
            "candidate_audio_duration_seconds": None,
            "asr_identity": {},
            "result_count": 0,
        }
    payload = _load_json(path)
    unresolved = _collect_processed_audio_unresolved_rows(payload)
    unresolved_statuses = unresolved["unresolved_statuses"]
    unresolved_ids = unresolved["unresolved_ids"]
    semantic_join_anomalies = _collect_semantic_join_anomalies(payload)
    item_errors: Dict[str, List[str]] = {}

    def add_item_error(item_id: str, reason: str) -> None:
        display_id = str(item_id or "").strip()
        if display_id and reason not in item_errors.setdefault(display_id, []):
            item_errors[display_id].append(reason)

    semantic_pause_reverse_asr_problems: List[str] = []
    source_anchors = _semantic_pause_source_anchors(request, semantic_pause_item_ids)
    for raw_item_id in semantic_pause_item_ids:
        item_id = str(raw_item_id or "").strip()
        item_problems = _semantic_pause_reverse_asr_problems(
            payload,
            [item_id],
            source_anchors_by_id=source_anchors,
        )
        semantic_pause_reverse_asr_problems.extend(item_problems)
        for reason in item_problems:
            add_item_error(item_id, reason)

    errors: List[str] = []
    candidate_path = _processed_audio_candidate_path(request)
    reported_candidate_hash = (
        str(payload.get("candidate_audio_sha256") or payload.get("final_candidate_sha256") or "")
        .strip()
        .lower()
    )
    actual_candidate_hash = ""
    candidate_duration_seconds: Optional[float] = None
    if not candidate_path:
        errors.append(
            "Processed audio metadata is missing the full-candidate audio path used for reverse ASR."
        )
    elif not os.path.isfile(candidate_path):
        errors.append(f"Full-candidate audio does not exist: {candidate_path}.")
    else:
        actual_candidate_hash = _sha256_file(candidate_path)
        if os.path.splitext(candidate_path)[1].casefold() == ".wav":
            try:
                with wave.open(candidate_path, "rb") as candidate_wave:
                    channels = candidate_wave.getnchannels()
                    sample_width = candidate_wave.getsampwidth()
                    frame_rate = candidate_wave.getframerate()
                    frame_count = candidate_wave.getnframes()
                    if channels <= 0 or sample_width <= 0 or frame_rate <= 0 or frame_count <= 0:
                        raise wave.Error("empty wave")
                    frame_size = channels * sample_width
                    decoded_frame_count = 0
                    while decoded_frame_count < frame_count:
                        requested_frames = min(
                            _WAV_VALIDATION_CHUNK_FRAMES,
                            frame_count - decoded_frame_count,
                        )
                        decoded_frames = candidate_wave.readframes(requested_frames)
                        if not decoded_frames or len(decoded_frames) % frame_size:
                            raise wave.Error("truncated wave payload")
                        chunk_frame_count = len(decoded_frames) // frame_size
                        if chunk_frame_count > requested_frames:
                            raise wave.Error("invalid wave payload")
                        decoded_frame_count += chunk_frame_count
                    if decoded_frame_count != frame_count:
                        raise wave.Error("truncated wave payload")
                    candidate_duration_seconds = frame_count / frame_rate
            except (OSError, EOFError, wave.Error):
                errors.append("Full-candidate reverse ASR input is not decodable audio.")
        else:
            candidate_duration_seconds = get_duration_ffprobe_cached(
                candidate_path,
                actual_candidate_hash,
            )
            if candidate_duration_seconds <= 0 or not _non_wave_candidate_decodes_fully(
                candidate_path
            ):
                errors.append("Full-candidate reverse ASR input is not decodable audio.")
    if not reported_candidate_hash:
        errors.append("Reverse ASR report is missing candidate audio SHA-256 identity.")
    elif actual_candidate_hash and reported_candidate_hash != actual_candidate_hash:
        errors.append(
            "Reverse ASR candidate audio SHA-256 does not match the full-candidate audio bytes."
        )

    duration_bounds = _segmented_candidate_duration_bounds(request)
    if candidate_duration_seconds is not None and duration_bounds is not None:
        minimum_duration, maximum_duration = duration_bounds
        if not minimum_duration <= candidate_duration_seconds <= maximum_duration:
            reason = (
                "Full-candidate reverse ASR input does not cover the segmented delivery "
                f"timeline: duration {candidate_duration_seconds:.3f}s is outside "
                f"{minimum_duration:.3f}-{maximum_duration:.3f}s."
            )
            errors.append(reason)
            for item_id in dict.fromkeys([*required_item_ids, *semantic_pause_item_ids]):
                add_item_error(str(item_id or ""), reason)

    asr_identity = _reverse_asr_identity(payload)
    if not all(asr_identity.values()):
        errors.append(
            "Reverse ASR report is missing complete ASR identity "
            "(provider, model/resource ID, adapter version)."
        )
    if request.audio_delivery_plan.mode == "segmented":
        expected_plan_digest = _audio_delivery_plan_digest(request)
        reported_plan_digest = (
            str(payload.get("audio_delivery_plan_sha256") or "").strip().casefold()
        )
        if reported_plan_digest != expected_plan_digest:
            errors.append(
                "Reverse ASR report audio_delivery_plan_sha256 does not match the normalized "
                "segmented delivery plan and pause evidence."
            )
    result_count = _reverse_asr_result_count(payload)
    if result_count <= 0:
        errors.append("Full-candidate report contains no reverse ASR result rows.")
    direct_rows_by_id: Dict[str, List[Dict[str, Any]]] = {}
    for row in _reverse_asr_result_rows(payload):
        normalized_id = _normalize_review_id(_row_item_label(row))
        if normalized_id:
            direct_rows_by_id.setdefault(normalized_id, []).append(row)
    semantic_pause_normalized_ids = {
        _normalize_review_id(item_id) for item_id in semantic_pause_item_ids
    }
    spoken_contract_normalized_ids = (
        {
            _normalize_review_id(item_id)
            for item_id in spoken_contract_item_ids
            if str(item_id or "").strip()
        }
        if spoken_contract_item_ids is not None
        else None
    )
    for raw_item_id in dict.fromkeys([*required_item_ids, *semantic_pause_item_ids]):
        item_id = str(raw_item_id or "").strip()
        matching_rows = direct_rows_by_id.get(_normalize_review_id(item_id), [])
        if len(matching_rows) != 1:
            reason = (
                f"Review item {item_id or '<unidentified>'} requires exactly one attributable "
                "full-candidate reverse ASR result row."
            )
            errors.append(reason)
            add_item_error(item_id, reason)
            continue
        matching_row = matching_rows[0]
        row_status = str(matching_row.get("status") or "").strip().casefold()
        if row_status not in _AUDIO_VALIDATION_PASS_STATUSES:
            reason = (
                f"Review item {item_id or '<unidentified>'} reverse ASR result row is not pass."
            )
            errors.append(reason)
            add_item_error(item_id, reason)
            continue
        normalized_item_id = _normalize_review_id(item_id)
        requires_spoken_contract = (
            normalized_item_id in spoken_contract_normalized_ids
            if spoken_contract_normalized_ids is not None
            else _item_requires_spoken_delete_contract(request, item_id)
        )
        if normalized_item_id not in semantic_pause_normalized_ids or requires_spoken_contract:
            missing_evidence = _spoken_reverse_asr_row_evidence_problems(
                matching_row,
                request=request if requires_spoken_contract else None,
                item_id=item_id if requires_spoken_contract else "",
            )
            if missing_evidence:
                reason = (
                    f"Review item {item_id or '<unidentified>'} requires attributable reverse "
                    "ASR evidence fields: " + ", ".join(missing_evidence) + "."
                )
                errors.append(reason)
                add_item_error(item_id, reason)
    known_item_ids = [
        str(item_id or "").strip()
        for item_id in dict.fromkeys([*required_item_ids, *semantic_pause_item_ids])
        if str(item_id or "").strip()
    ]

    def diagnostic_item_ids(diagnostic: str, *, semantic_join: bool = False) -> List[str]:
        tokens = [token.strip() for token in str(diagnostic or "").split(":") if token.strip()]
        if not tokens:
            return []
        candidate = tokens[-1]
        if semantic_join:
            markers = {
                "must_keep_missing",
                "semantic_join",
                "semantic_join_scan",
                "semantic_join_status",
            }
            marker_index = next(
                (idx for idx, token in enumerate(tokens) if token.casefold() in markers),
                -1,
            )
            if marker_index <= 0:
                return []
            candidate = tokens[marker_index - 1]
        normalized_candidate = _normalize_review_id(candidate)
        return [
            item_id
            for item_id in known_item_ids
            if _normalize_review_id(item_id) == normalized_candidate
        ]

    if unresolved_statuses or unresolved_ids:
        parts = []
        if unresolved_statuses:
            parts.append("statuses=" + ", ".join(unresolved_statuses))
        if unresolved_ids:
            parts.append("ids=" + ", ".join(unresolved_ids))
        reason = "Processed audio reverse validation has unresolved rows: " + "; ".join(parts) + "."
        errors.append(reason)
        for item_id in dict.fromkeys(
            matched for diagnostic in unresolved_ids for matched in diagnostic_item_ids(diagnostic)
        ):
            add_item_error(item_id, reason)
    if semantic_join_anomalies:
        reason = (
            "Processed audio reverse validation has semantic join anomalies: "
            + "; ".join(semantic_join_anomalies)
            + "."
        )
        errors.append(reason)
        for item_id in dict.fromkeys(
            matched
            for diagnostic in semantic_join_anomalies
            for matched in diagnostic_item_ids(diagnostic, semantic_join=True)
        ):
            add_item_error(item_id, reason)
    errors.extend(semantic_pause_reverse_asr_problems)
    return {
        "path": path,
        "errors": errors,
        "unresolved_statuses": unresolved_statuses,
        "unresolved_ids": unresolved_ids,
        "semantic_join_anomalies": semantic_join_anomalies,
        "semantic_pause_reverse_asr_problems": semantic_pause_reverse_asr_problems,
        "item_errors": item_errors,
        "candidate_audio_path": candidate_path,
        "candidate_audio_sha256": actual_candidate_hash,
        "candidate_audio_duration_seconds": candidate_duration_seconds,
        "asr_identity": asr_identity,
        "result_count": result_count,
    }


def _is_marker_track_name(name: Any) -> bool:
    normalized = _normalize_track_name(name)
    return normalized.startswith("校对标记") or normalized.startswith("review marker")


def _is_main_video_track_name(name: Any) -> bool:
    normalized = _normalize_track_name(name)
    return normalized in {"original video", "main video", "video track", "source video"}


def _count_segments(tracks: Iterable[Dict[str, Any]]) -> int:
    return sum(len(track.get("segments") or []) for track in tracks)


def _collect_draft_visual_evidence(content: Dict[str, Any]) -> Dict[str, Any]:
    tracks = content.get("tracks") or []
    materials = content.get("materials") or {}

    video_tracks = [
        track for track in tracks if _normalize_track_type_name(track.get("type")) == "video"
    ]
    text_tracks = [
        track for track in tracks if _normalize_track_type_name(track.get("type")) == "text"
    ]
    visual_extra_tracks = [
        track
        for track in tracks
        if _normalize_track_type_name(track.get("type"))
        in {"sticker", "effect", "filter", "video_effect", "image"}
    ]
    overlay_video_tracks = [
        track
        for track in video_tracks
        if (track.get("segments") or [])
        and not _is_main_video_track_name(track.get("name"))
        and not _looks_like_flattened_preview_track(track)
    ]
    non_marker_text_tracks = [
        track
        for track in text_tracks
        if (track.get("segments") or []) and not _is_marker_track_name(track.get("name"))
    ]
    material_counts = {
        "stickers": len(materials.get("stickers", []) or []),
        "effects": len(materials.get("effects", []) or []),
        "video_effects": len(materials.get("video_effects", []) or []),
        "text_templates": len(materials.get("text_templates", []) or []),
    }
    visual_segment_count = (
        _count_segments(overlay_video_tracks)
        + _count_segments(non_marker_text_tracks)
        + _count_segments(visual_extra_tracks)
    )
    material_visual_count = sum(material_counts.values())
    return {
        "overlay_video_track_count": len(overlay_video_tracks),
        "overlay_video_segment_count": _count_segments(overlay_video_tracks),
        "non_marker_text_track_count": len(non_marker_text_tracks),
        "non_marker_text_segment_count": _count_segments(non_marker_text_tracks),
        "extra_visual_track_count": len(visual_extra_tracks),
        "extra_visual_segment_count": _count_segments(visual_extra_tracks),
        "visual_segment_count": visual_segment_count,
        "visual_material_count": material_visual_count,
        "material_counts": material_counts,
        "has_visual_overlay_evidence": visual_segment_count > 0 or material_visual_count > 0,
    }


def _draft_track_by_name(content: Dict[str, Any], track_name: str) -> Optional[Dict[str, Any]]:
    target = _normalize_track_name(track_name)
    for track in content.get("tracks") or []:
        if _normalize_track_name(track.get("name")) == target:
            return track
    return None


def _normalize_keyframe_property_name(property_name: Any) -> str:
    property_map = {
        "position_x": "KFTypePositionX",
        "position_y": "KFTypePositionY",
        "kftypepositionx": "KFTypePositionX",
        "kftypepositiony": "KFTypePositionY",
        "KFTypePositionX": "KFTypePositionX",
        "KFTypePositionY": "KFTypePositionY",
    }
    raw = str(property_name or "").strip()
    return property_map.get(raw, property_map.get(raw.lower(), raw))


def _draft_segment_keyframe_counts(segment: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for keyframe_list in segment.get("common_keyframes") or []:
        if not isinstance(keyframe_list, dict):
            continue
        property_type = _normalize_keyframe_property_name(keyframe_list.get("property_type"))
        frames = keyframe_list.get("keyframe_list") or []
        if property_type:
            counts[property_type] = max(counts.get(property_type, 0), len(frames))
    return counts


def _visual_segment_requires_smooth_follow(spec: Dict[str, Any]) -> bool:
    motion_mode = str(spec.get("motion_mode") or "").strip().lower()
    role = str(spec.get("role") or "").strip().lower()
    track = str(spec.get("track_name") or "").strip().lower()
    if motion_mode == "smooth_keyframed_follow_speech":
        return True
    return "follow_speech" in role or "follow_speech" in track


def _validate_smooth_pointer_keyframes(
    request: RevisionRequest,
    content: Dict[str, Any],
) -> Dict[str, Any]:
    errors: List[str] = []
    details: List[Dict[str, Any]] = []
    required_count = 0

    for idx, edit in enumerate(request.edits):
        if not _is_visual_edit(edit):
            continue
        item_id = _edit_review_id(edit, idx)
        for spec_idx, spec in enumerate(_visual_plan_segments(edit), start=1):
            if not _visual_segment_requires_smooth_follow(spec):
                continue
            required_count += 1
            role = str(spec.get("role") or "visual_overlay")
            track_name = _clean_track_name(
                spec.get("track_name") or "",
                item_id=item_id,
                fallback_idx=idx,
                role=role,
            )
            requested_props = {
                _normalize_keyframe_property_name(item.get("property") or item.get("property_type"))
                for item in spec.get("keyframes") or []
                if isinstance(item, dict)
            }
            requested_props = {
                prop for prop in requested_props if prop in {"KFTypePositionX", "KFTypePositionY"}
            }
            if not requested_props:
                errors.append(
                    f"Visual item {item_id} segment {spec_idx} is marked as smooth speech-following "
                    "but has no planned position keyframes."
                )
                details.append(
                    {
                        "item_id": item_id,
                        "role": role,
                        "track_name": track_name,
                        "status": "missing_planned_keyframes",
                    }
                )
                continue

            track = _draft_track_by_name(content, track_name)
            if track is None:
                errors.append(
                    f"Visual item {item_id} smooth pointer track is missing in the saved draft: {track_name}."
                )
                details.append(
                    {
                        "item_id": item_id,
                        "role": role,
                        "track_name": track_name,
                        "status": "missing_track",
                    }
                )
                continue

            segments = track.get("segments") or []
            if len(segments) != 1:
                errors.append(
                    f"Visual item {item_id} smooth pointer track must be one keyframed segment, "
                    f"found {len(segments)} segments on {track_name}."
                )

            matching_segment = None
            matching_counts: Dict[str, int] = {}
            for segment in segments:
                counts = _draft_segment_keyframe_counts(segment)
                if all(counts.get(prop, 0) >= 2 for prop in requested_props):
                    matching_segment = segment
                    matching_counts = counts
                    break
                if not matching_counts:
                    matching_counts = counts

            if matching_segment is None:
                errors.append(
                    f"Visual item {item_id} smooth pointer segment is missing required position keyframes "
                    f"on {track_name}: expected {', '.join(sorted(requested_props))}."
                )
                status = "missing_saved_keyframes"
            else:
                status = "pass"

            details.append(
                {
                    "item_id": item_id,
                    "role": role,
                    "track_name": track_name,
                    "status": status,
                    "segment_count": len(segments),
                    "required_properties": sorted(requested_props),
                    "saved_keyframe_counts": matching_counts,
                }
            )

    return {
        "ok": not errors,
        "required_count": required_count,
        "errors": errors,
        "details": details,
    }


def _collect_replacement_glyph_evidence(
    content: Dict[str, Any],
    *,
    allowed_text_material_ids: Optional[Iterable[str]] = None,
    allowed_text_values: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    tracks = content.get("tracks") or []
    materials = content.get("materials") or {}
    allowed_material_ids = {str(value) for value in allowed_text_material_ids or []}
    allowed_values = {str(value) for value in allowed_text_values or []}
    bad_track_names = [
        str(track.get("name") or "")
        for track in tracks
        if _has_replacement_glyphs(track.get("name")) or "\ufffd" in str(track.get("name") or "")
    ]
    bad_text_materials: List[str] = []
    for material in materials.get("texts", []) or []:
        material_id = str(material.get("id") or material.get("material_id") or "")
        raw_content = str(material.get("content") or "")
        text_value = ""
        try:
            parsed = json.loads(raw_content)
            text_value = str(parsed.get("text") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            text_value = raw_content
        has_unicode_replacement = "\ufffd" in text_value
        exact_marker_literal = material_id in allowed_material_ids or (
            not material_id and text_value in allowed_values
        )
        if has_unicode_replacement or (
            _has_replacement_glyphs(text_value) and not exact_marker_literal
        ):
            bad_text_materials.append(material_id or text_value[:40])
    return {
        "bad_track_names": bad_track_names,
        "bad_text_materials": bad_text_materials,
        "bad_track_name_count": len(bad_track_names),
        "bad_text_material_count": len(bad_text_materials),
        "has_replacement_glyphs": bool(bad_track_names or bad_text_materials),
    }


def _build_request_action_entries(request: RevisionRequest) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for idx, edit in enumerate(request.edits):
        text = f"{edit.label} {edit.detail}"
        entries.append(
            {
                "id": edit.doc_item_id or _extract_review_id(text, f"edit_{idx + 1:03d}"),
                "source": "edit",
                "type": edit.op_type,
                "label": edit.label,
                "detail": edit.detail,
                "text": text,
                "start": edit.start,
                "end": edit.end,
                "execution_evidence": True,
            }
        )
    for idx, marker in enumerate(request.markers):
        text = f"{marker.label} {marker.detail}"
        entries.append(
            {
                "id": marker.doc_item_id or _extract_review_id(text, f"marker_{idx + 1:03d}"),
                "source": "marker",
                "type": "marker",
                "label": marker.label,
                "detail": marker.detail,
                "text": text,
                "start": marker.start,
                "end": marker.end,
                "execution_evidence": False,
            }
        )
    return entries


def _action_matches_item(action: Dict[str, Any], item: RevisionReviewItem) -> bool:
    if _normalize_review_id(action["id"]) == _normalize_review_id(item.item_id):
        return True
    action_text = str(action.get("text") or "")
    source_text = item.source_text
    if not action_text or not source_text:
        return False
    action_fp = _fingerprint_text(action_text)
    item_fp = _fingerprint_text(source_text)
    if not action_fp or not item_fp:
        return False
    if len(item_fp) >= 8 and item_fp in action_fp:
        return True
    if len(action_fp) >= 8 and action_fp in item_fp:
        return True
    return False


def _matching_actions_for_item(
    item: RevisionReviewItem,
    action_entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [action for action in action_entries if _action_matches_item(action, item)]


def _evidence_score(evidence: Dict[str, Any], validation: Dict[str, Any]) -> int:
    if _audio_validation_is_pass(validation, evidence):
        return 4
    if _evidence_has_validation(validation, evidence):
        return 3
    if _evidence_has_execution(evidence):
        return 2
    if evidence or validation:
        return 1
    return 0


def _merge_unique_review_items(
    request_items: List[RevisionReviewItem],
    doc_items: Optional[List[RevisionReviewItem]],
) -> List[RevisionReviewItem]:
    if doc_items is None:
        return list(request_items)
    merged: Dict[str, RevisionReviewItem] = {}
    for item in doc_items:
        merged[_normalize_review_id(item.item_id)] = item
    for item in request_items:
        item_key = _normalize_review_id(item.item_id)
        source_item = merged.get(item_key)
        if source_item is None:
            continue
        if _evidence_score(item.evidence, item.validation) >= _evidence_score(
            source_item.evidence,
            source_item.validation,
        ):
            evidence = item.evidence or source_item.evidence
            validation = item.validation or source_item.validation
        else:
            evidence = source_item.evidence or item.evidence
            validation = source_item.validation or item.validation
        if source_item.source_text:
            source_text = source_item.source_text
            verbatim_status = source_item.verbatim_status
        else:
            source_text = item.source_text
            verbatim_status = item.verbatim_status
        merged[item_key] = RevisionReviewItem(
            item_id=source_item.item_id or item.item_id,
            kind=source_item.kind or item.kind,
            source_text=source_text,
            source=source_item.source or item.source,
            start=source_item.start if source_item.start is not None else item.start,
            end=source_item.end if source_item.end is not None else item.end,
            execution_required=source_item.execution_required,
            evidence=evidence,
            validation=validation,
            verbatim_status=verbatim_status,
        )
    return list(merged.values())


def _visual_evidence_required(kind: str) -> bool:
    return kind in _VISUAL_KINDS


def _audio_validation_required(kind: str) -> bool:
    return kind in _AUDIO_KINDS


def _action_provides_execution_for_kind(action: Dict[str, Any], kind: str) -> bool:
    if action.get("source") != "edit":
        return False
    op_type = str(action.get("type") or "").strip().lower()
    if kind in _AUDIO_KINDS:
        return op_type in {
            "delete",
            "replace_audio",
            "audio_delete",
            "audio_repair",
            "pause_delete",
        }
    if kind in _VISUAL_KINDS:
        return op_type in {
            "add_overlay",
            "pointer_overlay",
            "add_pointer",
            "animation_timing",
            "visual_delete",
            "visual_insert",
            "visual_overlay",
            "overlay",
            "add_image",
            "add_sticker",
            "speed",
            "shift",
            "trim_visual",
        }
    return bool(action.get("execution_evidence"))


def _timeline_plan_path(request: RevisionRequest) -> str:
    processed_audio = request.processed_audio if isinstance(request.processed_audio, dict) else {}
    for key in ("timeline_plan", "timeline_map", "timing_plan"):
        value = str(processed_audio.get(key) or "").strip()
        if value:
            return value
    outputs = processed_audio.get("outputs")
    if isinstance(outputs, dict):
        for key in ("timeline_plan", "timeline_map", "timing_plan"):
            value = str(outputs.get(key) or "").strip()
            if value:
                return value
    return ""


def _number_pair(value: Any) -> Optional[List[float]]:
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        return [float(value[0]), float(value[1])]
    except (TypeError, ValueError):
        return None


def _pairs_close(left: Any, right: Any, tolerance: float = 0.03) -> bool:
    left_pair = _number_pair(left)
    right_pair = _number_pair(right)
    if left_pair is None or right_pair is None:
        return False
    return all(abs(left_pair[idx] - right_pair[idx]) <= tolerance for idx in range(2))


def _status_pass_from_mapping(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    status = str(value.get("status") or "").strip().lower()
    if status not in _PASS_STATUSES:
        return False
    source = str(value.get("source") or value.get("method") or "").strip().lower()
    return (
        "timeline" in source
        or "mapping" in source
        or bool(value.get("expected_timeline_start") is not None)
    )


def _has_timeline_mapping_evidence(value: Dict[str, Any]) -> bool:
    for key in ("timeline_mapping", "timeline_map_validation", "mapping_validation"):
        if _status_pass_from_mapping(value.get(key)):
            return True
    validation = value.get("validation")
    if isinstance(validation, dict) and _status_pass_from_mapping(validation):
        return True
    return False


def _edit_item_id(edit: RevisionEdit, idx: int) -> str:
    return _edit_review_id(edit, idx)


def _validate_timeline_plan_mapping(request: RevisionRequest) -> Dict[str, Any]:
    path = _timeline_plan_path(request)
    if not path:
        return {"path": "", "errors": [], "timeline_mapping_errors": []}
    if not os.path.exists(path):
        message = f"Timeline plan does not exist: {path}."
        return {"path": path, "errors": [message], "timeline_mapping_errors": [message]}

    plan = _load_json(path)
    replacement_source_window = plan.get("replacement_source_window")
    replacement_timeline_window = plan.get("replacement_timeline_window")
    mapping_errors: List[str] = []

    if replacement_source_window and replacement_timeline_window:
        for item in request.review_items:
            evidence = item.evidence or {}
            if not isinstance(evidence, dict):
                continue
            source_window = evidence.get("source_window")
            timeline_window = evidence.get("timeline_window")
            if source_window is None or timeline_window is None:
                continue
            if _pairs_close(source_window, replacement_source_window) and not _pairs_close(
                timeline_window,
                replacement_timeline_window,
            ):
                mapping_errors.append(
                    f"{item.item_id}: replacement timeline_window {timeline_window} does not match "
                    f"timeline_plan replacement_timeline_window {replacement_timeline_window}."
                )

    for idx, edit in enumerate(request.edits):
        if not _is_visual_edit(edit):
            continue
        item_id = _edit_item_id(edit, idx)
        for segment_idx, spec in enumerate(_visual_plan_segments(edit), start=1):
            if not isinstance(spec, dict) or "timeline_start" not in spec:
                continue
            if _has_timeline_mapping_evidence(spec):
                continue
            role = str(spec.get("role") or f"segment_{segment_idx}").strip()
            source_kind = (edit.source_kind or edit.op_type or "").lower()
            source_start = _optional_float(
                spec.get("source_start"), f"visual_plan[{idx}].source_start"
            )
            source_in_replacement = False
            replacement_pair = _number_pair(replacement_source_window)
            if replacement_pair and source_start is not None:
                source_in_replacement = replacement_pair[0] <= source_start <= replacement_pair[1]
            if (
                role.lower().startswith("replacement")
                or "replacement" in source_kind
                or source_in_replacement
            ):
                mapping_errors.append(
                    f"{item_id}:{role} has explicit timeline_start={spec.get('timeline_start')} "
                    "but lacks passing timeline-plan mapping evidence."
                )

    errors = []
    if mapping_errors:
        errors.append(
            "Visual overlay timing failed timeline-plan mapping validation: "
            + "; ".join(mapping_errors)
        )
    return {"path": path, "errors": errors, "timeline_mapping_errors": sorted(set(mapping_errors))}


def _validate_lite_visual_start_alignment(
    request: RevisionRequest,
    content: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate the only visual placement rule kept by lite mode.

    Lite overlays are required to begin at the review edit's start time and to
    reference a saved editable material.  No scale, transform, hotspot,
    lifecycle, cover, or occlusion evidence is inspected here.  A generated
    clean-cover helper asset is deliberately ignored because lite mode does not
    perform recorded-pointer replacement or obstruction cleanup.
    """

    result = {"ok": True, "errors": [], "checked_items": 0, "matched_segments": 0}
    if request.workflow_mode != "lite" or not isinstance(content, dict):
        return result

    materials = content.get("materials") or {}
    material_paths: Dict[str, str] = {}
    if isinstance(materials, dict):
        for group in materials.values():
            if not isinstance(group, list):
                continue
            for material in group:
                if not isinstance(material, dict):
                    continue
                material_id = str(material.get("id") or material.get("material_id") or "").strip()
                material_path = str(
                    material.get("path")
                    or material.get("media_path")
                    or material.get("file_path")
                    or ""
                ).strip()
                if material_id and material_path:
                    material_paths[material_id] = _normalize_material_path(material_path)

    saved_segments: List[Dict[str, Any]] = []
    for track in content.get("tracks") or []:
        if not isinstance(track, dict) or _normalize_track_type_name(track.get("type")) != "video":
            continue
        for segment in track.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            target = segment.get("target_timerange") or {}
            try:
                target_start = int(target.get("start", 0) or 0) / 1_000_000.0
            except (TypeError, ValueError, OverflowError):
                continue
            saved_segments.append(
                {
                    "path": material_paths.get(str(segment.get("material_id") or ""), ""),
                    "start": target_start,
                }
            )

    for index, edit in enumerate(request.edits):
        if not _is_visual_edit(edit):
            continue
        specs = list(_visual_plan_segments(edit))
        if not specs:
            specs = [{"asset_path": path} for path in edit.asset_paths]
        if not specs:
            continue
        result["checked_items"] += 1
        try:
            expected_start = float(edit.start)
        except (TypeError, ValueError, OverflowError):
            result["errors"].append(
                f"Lite visual edit {edit.doc_item_id or index + 1} has an invalid start time."
            )
            continue
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            role = str(spec.get("role") or "").strip().casefold()
            if role in {"clean_cover", "cleanup", "clean_layer", "residual_pointer_cover"}:
                continue
            asset_path = _normalize_material_path(spec.get("asset_path"))
            if not asset_path:
                continue
            candidates = [row for row in saved_segments if row["path"] == asset_path]
            if not candidates:
                result["errors"].append(
                    f"Lite visual edit {edit.doc_item_id or index + 1} asset is not saved as an editable segment: "
                    f"{spec.get('asset_path')}."
                )
                continue
            if not any(abs(float(row["start"]) - expected_start) <= 0.01 for row in candidates):
                result["errors"].append(
                    f"Lite visual edit {edit.doc_item_id or index + 1} asset start is not aligned: "
                    f"expected {expected_start:.3f}s."
                )
                continue
            result["matched_segments"] += 1

    result["errors"] = list(dict.fromkeys(result["errors"]))
    result["ok"] = not result["errors"]
    return result


def _pointer_landing_is_proven(evidence: Dict[str, Any], validation: Dict[str, Any]) -> bool:
    candidate_payloads = [
        evidence.get("hotspot_landing"),
        evidence.get("landing_validation"),
        evidence.get("target_validation"),
        validation.get("hotspot_landing") if isinstance(validation, dict) else None,
        validation.get("landing_validation") if isinstance(validation, dict) else None,
    ]
    for candidate in candidate_payloads:
        if isinstance(candidate, dict):
            status = str(candidate.get("status") or "").strip().lower()
            if status in _PASS_STATUSES:
                return True

    for payload in (evidence, validation):
        if not isinstance(payload, dict):
            continue
        method = (
            str(payload.get("method") or payload.get("validation_method") or "").strip().lower()
        )
        if method in {
            "rendered_hotspot_landing",
            "opened_draft_calibration",
            "display_safe_baked_window",
            "hotspot_landing_verified",
            "target_landing_verified",
        }:
            return True
        try:
            landing_error_px = float(payload.get("landing_error_px"))
        except (TypeError, ValueError):
            landing_error_px = None
        if landing_error_px is not None:
            max_error_px = float(payload.get("max_landing_error_px") or 8.0)
            if landing_error_px <= max_error_px:
                return True
    return False


def _pointer_landing_required(item: RevisionReviewItem) -> bool:
    evidence = item.evidence or {}
    validation = item.validation or {}
    if not isinstance(evidence, dict) or not isinstance(validation, dict):
        return False
    if "target_point" in evidence or "anchor" in evidence:
        return True
    method = str(
        evidence.get("validation", {}).get("method")
        if isinstance(evidence.get("validation"), dict)
        else ""
    )
    method += " " + str(validation.get("method") or "")
    return "planned" in method.lower()


_ALWAYS_ACCEPTANCE_GATES = (
    "source_coverage",
    "execution_evidence",
    "draft_exists",
    "editable_structure",
    "verbatim_markers",
    "audio_delivery",
)
_CONDITIONAL_ACCEPTANCE_GATES = (
    "audio_precision",
    "audio_join",
    "pause_fit",
    "visual",
    "pointer",
    "animation",
)
_LITE_VISUAL_ACCEPTANCE_GATES = {"visual", "pointer"}
_CHEAP_AUDIO_KINDS = {
    "bgm_replace",
    "replace_bgm",
    "music_replace",
    "audio_level",
    "gain",
    "volume",
    "loudness",
    "peak_target",
    "noise_cleanup",
    "noise_reduction",
    "denoise",
    "noise_only_cleanup",
}
_PRECISION_JOIN_KINDS = {
    "spoken_delete",
    "speech_delete",
    "delete",
    "audio_delete",
    "phrase_delete",
    "range_delete",
    "ellipsis_range_delete",
    "colored_span_delete",
    "gap_delete",
    "tail_particle_delete",
    "tail_cleanup",
    "speech_tail_cleanup",
    "speech_replace",
    "speech_replacement",
    "narration_replace",
    "replace_audio",
}
_PRECISION_ONLY_KINDS = {"speech_repair", "audio_repair"}
_JOIN_OPERATION_KINDS = {
    "cut",
    "splice",
    "replace",
    "delete",
    "audio_delete",
    "spoken_delete",
    "phrase_delete",
    "range_delete",
    "ellipsis_range_delete",
    "colored_span_delete",
    "gap_delete",
    "tail_particle_delete",
    "replace_audio",
    "speech_replace",
    "speech_replacement",
    "narration_replace",
}
_PAUSE_JOIN_KINDS = {"pause_delete"}
_PAUSE_ONLY_KINDS = {
    "pause_timing_review",
    "semantic_pause_adjustment",
    "visual_hold_review",
}
_POINTER_KINDS = {
    "pointer",
    "pointer_overlay",
    "hand",
    "hand_pointer",
    "hand_overlay",
    "arrow",
    "arrow_overlay",
    "underline",
    "underline_overlay",
    "circle",
    "circle_overlay",
    "magnifier",
    "magnifier_overlay",
    "add_pointer",
    "add_hand",
    "add_arrow",
}
_ANIMATION_KINDS = {
    "animation_timing",
    "page_turn",
    "state_reveal",
    "state_release",
    "release_boundary",
}
_VISUAL_ACCEPTANCE_KINDS = {
    "visual_delete",
    "visual_insert",
    "visual_overlay",
    "visual_replace",
    "visual_replacement",
    "replacement_picture",
    "replace_picture",
    "safe_zone",
    "safe_zone_text",
    "text_safe_zone",
    "overlay",
    "add_overlay",
    "add_image",
    "add_sticker",
    "replace_visual",
    "trim_visual",
}
_VISUAL_DELETE_EVIDENCE_KINDS = {"visual_delete", "trim_visual"}


def _normalize_acceptance_token(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    normalized = re.sub(r"[^\w]+", "_", normalized, flags=re.UNICODE)
    return re.sub(r"_+", "_", normalized).strip("_")


def _acceptance_text_tokens(value: Any) -> List[str]:
    normalized = _normalize_acceptance_token(value)
    if not normalized:
        return []
    parts = [part for part in normalized.split("_") if part]
    tokens = list(parts)
    for width in (2, 3):
        tokens.extend(
            "_".join(parts[idx : idx + width]) for idx in range(max(0, len(parts) - width + 1))
        )
    return tokens


def _classify_acceptance_token(
    token: str,
    *,
    execution_required: bool,
    operation_tokens: Sequence[str] = (),
) -> Dict[str, Any]:
    normalized = _normalize_acceptance_token(token)
    if normalized in _CHEAP_AUDIO_KINDS:
        return {"known": True, "kind": normalized, "gates": []}
    if normalized in _PRECISION_JOIN_KINDS:
        return {
            "known": True,
            "kind": normalized,
            "gates": ["audio_precision", "audio_join"],
        }
    if normalized in _PRECISION_ONLY_KINDS:
        gates = ["audio_precision"]
        if any(op in _JOIN_OPERATION_KINDS for op in operation_tokens):
            gates.append("audio_join")
        return {"known": True, "kind": normalized, "gates": gates}
    if normalized in _PAUSE_JOIN_KINDS:
        return {"known": True, "kind": normalized, "gates": ["audio_join", "pause_fit"]}
    if normalized in _PAUSE_ONLY_KINDS:
        return {"known": True, "kind": normalized, "gates": ["pause_fit"]}
    if normalized in _POINTER_KINDS:
        return {"known": True, "kind": normalized, "gates": ["visual", "pointer"]}
    if normalized in _ANIMATION_KINDS:
        return {"known": True, "kind": normalized, "gates": ["visual", "animation"]}
    if normalized in _VISUAL_ACCEPTANCE_KINDS:
        return {"known": True, "kind": normalized, "gates": ["visual"]}
    if normalized == "review_only" and not execution_required:
        return {"known": True, "kind": normalized, "gates": []}
    return {"known": False, "kind": normalized, "gates": []}


def _acceptance_route_records(
    request: RevisionRequest,
    doc_items: Optional[List[RevisionReviewItem]],
) -> List[Dict[str, Any]]:
    request_by_id = {_normalize_review_id(item.item_id): item for item in request.review_items}
    doc_by_id = (
        {_normalize_review_id(item.item_id): item for item in doc_items}
        if doc_items is not None
        else {}
    )
    edits_by_id: Dict[str, List[RevisionEdit]] = {}
    display_ids: Dict[str, str] = {}
    route_order: List[str] = []
    for idx, edit in enumerate(request.edits):
        item_id = _edit_review_id(edit, idx)
        normalized_id = _normalize_review_id(item_id)
        if normalized_id not in edits_by_id:
            route_order.append(normalized_id)
            edits_by_id[normalized_id] = []
            display_ids[normalized_id] = item_id
        edits_by_id[normalized_id].append(edit)

    ledger_items = doc_items if doc_items is not None else request.review_items
    for item in ledger_items:
        normalized_id = _normalize_review_id(item.item_id)
        if normalized_id not in route_order:
            route_order.append(normalized_id)
        display_ids[normalized_id] = item.item_id

    records: List[Dict[str, Any]] = []
    for normalized_id in route_order:
        edits = edits_by_id.get(normalized_id, [])
        request_item = request_by_id.get(normalized_id)
        doc_item = doc_by_id.get(normalized_id)
        authoritative_item = doc_item if doc_items is not None else request_item
        execution_item = authoritative_item or request_item
        execution_required = (
            execution_item.execution_required if execution_item is not None else bool(edits)
        )
        operation_tokens = [
            _normalize_acceptance_token(edit.op_type) for edit in edits if edit.op_type
        ]

        kind = ""
        kind_source = ""
        if doc_item is not None and doc_item.kind:
            kind = doc_item.kind
            kind_source = "latest_doc_item.kind"
        if not kind:
            source_kind = next((edit.source_kind for edit in edits if edit.source_kind), "")
            if source_kind:
                kind = source_kind
                kind_source = "edit.source_kind"
        if not kind and doc_items is None and request_item is not None and request_item.kind:
            kind = request_item.kind
            kind_source = "request_review_item.kind"

        if kind:
            classification = _classify_acceptance_token(
                kind,
                execution_required=execution_required,
                operation_tokens=operation_tokens,
            )
        else:
            classification = {"known": False, "kind": "", "gates": []}
            for operation_token in operation_tokens:
                candidate = _classify_acceptance_token(
                    operation_token,
                    execution_required=execution_required,
                    operation_tokens=operation_tokens,
                )
                if candidate["known"]:
                    classification = candidate
                    kind_source = "edit.op_type"
                    break
            if not classification["known"]:
                text_parts = []
                if authoritative_item is not None:
                    text_parts.append(authoritative_item.source_text)
                for edit in edits:
                    text_parts.extend((edit.label, edit.detail))
                for text_token in _acceptance_text_tokens(" ".join(text_parts)):
                    candidate = _classify_acceptance_token(
                        text_token,
                        execution_required=execution_required,
                        operation_tokens=operation_tokens,
                    )
                    if candidate["known"]:
                        classification = candidate
                        kind_source = "text_fallback"
                        break
        pointer_operation_tokens = [
            *operation_tokens,
            *[_normalize_acceptance_token(edit.source_kind) for edit in edits if edit.source_kind],
        ]
        if any(token in _POINTER_KINDS for token in pointer_operation_tokens):
            classification = {
                **classification,
                "known": True,
                "gates": list(
                    dict.fromkeys([*classification.get("gates", []), "visual", "pointer"])
                ),
            }
            kind_source = (kind_source + "+edit_pointer_action").strip("+")
        if not classification["known"] and not execution_required:
            classification = {"known": True, "kind": "review_only", "gates": []}
            kind_source = "execution_required=false"

        operation_gates: List[str] = []
        explicit_source_kind_gates = False
        for edit in edits:
            operation_token = _normalize_acceptance_token(edit.op_type)
            if not operation_token:
                continue
            explicit_source_kind = _normalize_acceptance_token(edit.source_kind)
            semantic_kind = explicit_source_kind or str(classification.get("kind") or "")
            if explicit_source_kind:
                source_classification = _classify_acceptance_token(
                    explicit_source_kind,
                    execution_required=execution_required,
                    operation_tokens=operation_tokens,
                )
                source_gates = source_classification.get("gates") or []
                operation_gates.extend(source_gates)
                explicit_source_kind_gates = bool(source_gates) or explicit_source_kind_gates

            generic_operation_is_disambiguated = (
                semantic_kind in _CHEAP_AUDIO_KINDS and operation_token == "replace_audio"
            ) or (
                semantic_kind in (_PAUSE_JOIN_KINDS | _VISUAL_DELETE_EVIDENCE_KINDS)
                and operation_token == "delete"
            )
            if generic_operation_is_disambiguated:
                continue
            operation_classification = _classify_acceptance_token(
                operation_token,
                execution_required=execution_required,
                operation_tokens=operation_tokens,
            )
            operation_gates.extend(operation_classification.get("gates") or [])
        if operation_gates:
            classification = {
                **classification,
                "known": True,
                "gates": list(dict.fromkeys([*classification.get("gates", []), *operation_gates])),
            }
            if explicit_source_kind_gates:
                kind_source = (kind_source + "+edit_source_kind").strip("+")
            kind_source = (kind_source + "+concrete_operation").strip("+")

        if not execution_required and not operation_tokens:
            classification = {
                **classification,
                "known": True,
                "gates": [],
            }
            kind_source = (kind_source + "+execution_required=false").strip("+")

        records.append(
            {
                "item_id": display_ids.get(normalized_id, normalized_id),
                "normalized_item_id": normalized_id,
                "kind": classification["kind"],
                "kind_source": kind_source or "unresolved",
                "operation_types": operation_tokens,
                "execution_required": execution_required,
                "has_review_item": authoritative_item is not None,
                "known": classification["known"],
                "gates": list(classification["gates"]),
            }
        )
    return records


def derive_acceptance_profile(
    request: RevisionRequest,
    doc_items: Optional[List[RevisionReviewItem]] = None,
) -> Dict[str, Any]:
    """Derive low-cost global and item-specific revision acceptance gates."""

    records = _acceptance_route_records(request, doc_items)
    # Lite revisions intentionally do not use the full visual/pointer evidence
    # contract.  A visual edit is still an execution item (the edit and marker
    # must exist), but its acceptance is limited to the saved asset and start
    # time.  Removing these gates here also prevents downstream full-workflow
    # checks from being reached through strict validation or compiled jobs.
    if request.workflow_mode == "lite":
        for record in records:
            record["gates"] = [
                gate
                for gate in record.get("gates") or []
                if gate not in _LITE_VISUAL_ACCEPTANCE_GATES
            ]
    acceptance = request.acceptance
    enabled = list(_ALWAYS_ACCEPTANCE_GATES)
    gate_reasons: Dict[str, List[str]] = {
        gate: ["Always required for revision acceptance."] for gate in _ALWAYS_ACCEPTANCE_GATES
    }
    if not request.acceptance.require_execution_evidence:
        gate_reasons["execution_evidence"] = [
            "Always required; require_execution_evidence=false cannot disable execution proof."
        ]

    for record in records:
        for gate in record["gates"]:
            if gate not in enabled:
                enabled.append(gate)
            gate_reasons.setdefault(gate, []).append(
                f"{record['item_id']} routes as {record['kind']} from {record['kind_source']}."
            )

    explicit_item_gates: List[tuple[str, str]] = []
    if acceptance.require_audio_validation:
        for gate in ("audio_precision", "audio_join"):
            explicit_item_gates.append(
                (gate, "Explicit require_audio_validation=true adds this gate.")
            )
    if (
        request.workflow_mode != "lite"
        and acceptance.require_visual_evidence
        and acceptance._explicit_require_visual_evidence
    ):
        explicit_item_gates.append(
            ("visual", "Explicit require_visual_evidence=true adds this gate.")
        )
    if acceptance.require_pause_validation:
        explicit_item_gates.append(
            ("pause_fit", "Explicit require_pause_validation=true adds this gate.")
        )

    execution_records = [record for record in records if record["execution_required"]]
    attributable_execution_records = [
        record for record in execution_records if record["has_review_item"]
    ]
    for gate, _reason in explicit_item_gates:
        routed_carriers = [record for record in execution_records if gate in record["gates"]]
        if routed_carriers or not attributable_execution_records:
            continue
        # An explicit gate needs one attributable carrier when routing found no
        # natural match. It must not turn every unrelated execution item into a
        # carrier for that gate.
        attributable_execution_records[0]["gates"].append(gate)
    for gate, reason in explicit_item_gates:
        if gate not in enabled:
            enabled.append(gate)
        gate_reasons.setdefault(gate, []).append(reason)

    skipped = [gate for gate in _CONDITIONAL_ACCEPTANCE_GATES if gate not in enabled]
    for gate in skipped:
        gate_reasons[gate] = [
            "No routed item or explicit acceptance rule requires this conditional gate."
        ]
    routing_failures = [
        {
            "gate": "execution_evidence",
            "item_id": record["item_id"],
            "status": "review",
            "repairable": True,
            "reason": (
                f"Execution-required item {record['item_id']} has unknown or ambiguous "
                f"acceptance kind/operation ({record['kind'] or 'unresolved'}); classify it explicitly."
            ),
        }
        for record in records
        if record["execution_required"] and not record["known"]
    ]
    explicit_carrier_requirements = (
        (
            acceptance.require_audio_validation,
            "audio_precision",
            "require_audio_validation",
        ),
        (
            request.workflow_mode != "lite"
            and acceptance.require_visual_evidence
            and acceptance._explicit_require_visual_evidence,
            "visual",
            "require_visual_evidence",
        ),
        (acceptance.require_pause_validation, "pause_fit", "require_pause_validation"),
    )
    for required, gate, flag_name in explicit_carrier_requirements:
        if required and not attributable_execution_records:
            routing_failures.append(
                {
                    "gate": gate,
                    "item_id": "",
                    "status": "fail",
                    "repairable": True,
                    "reason": (
                        f"Explicit {flag_name}=true has no execution-required review item "
                        "to carry attributable acceptance evidence."
                    ),
                }
            )
    return {
        "enabled_gates": enabled,
        "skipped_gates": skipped,
        "gate_reasons": gate_reasons,
        "items": records,
        "routing_failures": routing_failures,
        "doc_items_supplied": doc_items is not None,
    }


def _semantic_pause_boundary_is_proven(
    proof: Dict[str, Any],
    request: Optional[RevisionRequest] = None,
) -> bool:
    boundary = proof.get("boundary_evidence")
    if not isinstance(boundary, dict):
        return False
    if str(boundary.get("status") or "").strip().casefold() not in {
        "pass",
        "accepted",
        "approved",
    }:
        return False

    requested = _safe_float(boundary.get("requested_time"))
    resolved = _safe_float(boundary.get("resolved_time"))
    previous_end = _safe_float(boundary.get("previous_word_end"))
    next_start = _safe_float(boundary.get("next_word_start"))
    previous_utterance_end = _safe_float(boundary.get("previous_utterance_end"))
    next_utterance_start = _safe_float(boundary.get("next_utterance_start"))
    gap_duration = _safe_float(boundary.get("gap_duration"))
    previous_guard = _safe_float(boundary.get("previous_guard_seconds"))
    next_guard = _safe_float(boundary.get("next_guard_seconds"))
    minimum_guard = _safe_float(boundary.get("minimum_edge_guard_seconds"))
    if any(
        value is None or not math.isfinite(value)
        for value in (
            requested,
            resolved,
            previous_end,
            next_start,
            previous_utterance_end,
            next_utterance_start,
            gap_duration,
            previous_guard,
            next_guard,
            minimum_guard,
        )
    ):
        return False
    assert requested is not None
    assert resolved is not None
    assert previous_end is not None
    assert next_start is not None
    assert previous_utterance_end is not None
    assert next_utterance_start is not None
    assert gap_duration is not None
    assert previous_guard is not None
    assert next_guard is not None
    assert minimum_guard is not None
    if (
        next_start <= previous_end
        or not previous_end < resolved < next_start
        or not previous_utterance_end < resolved < next_utterance_start
        or minimum_guard <= 0
        or previous_guard < minimum_guard - 1e-3
        or next_guard < minimum_guard - 1e-3
    ):
        return False
    if abs(gap_duration - (next_start - previous_end)) > 1e-3:
        return False
    if (
        abs(previous_guard - (resolved - previous_end)) > 1e-3
        or abs(next_guard - (next_start - resolved)) > 1e-3
    ):
        return False
    if abs(resolved - ((previous_utterance_end + next_utterance_start) / 2.0)) > 1e-3:
        return False
    if str(boundary.get("placement") or "").strip().casefold() != "gap_midpoint":
        return False
    if not str(boundary.get("reason") or "").strip().endswith("gap_midpoint"):
        return False

    proof_requested = _safe_float(proof.get("requested_source_time"))
    proof_resolved = _safe_float(proof.get("source_time"))
    frame_source_time = _safe_float(proof.get("frame_source_time"))
    if (
        proof_requested is None
        or proof_resolved is None
        or frame_source_time is None
        or abs(proof_requested - requested) > 1e-3
        or abs(proof_resolved - resolved) > 1e-3
        or abs(frame_source_time - resolved) > 1e-3
    ):
        return False

    source_asr_path = str(boundary.get("source_asr_path") or "").strip()
    expected_sha256 = str(boundary.get("source_asr_sha256") or "").strip().casefold()
    if not source_asr_path or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return False
    try:
        with open(source_asr_path, "rb") as source:
            source_asr_bytes = source.read()
        actual_sha256 = hashlib.sha256(source_asr_bytes).hexdigest()
        source_asr_payload = json.loads(source_asr_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if actual_sha256 != expected_sha256:
        return False

    media_keys = (
        ("source_video_path", "source_video_sha256"),
        ("source_audio_path", "source_audio_sha256"),
        ("alignment_audio_path", "alignment_audio_sha256"),
    )
    for path_key, hash_key in media_keys:
        media_path = str(boundary.get(path_key) or "").strip()
        expected_media_hash = str(boundary.get(hash_key) or "").strip().casefold()
        if not media_path or not re.fullmatch(r"[0-9a-f]{64}", expected_media_hash):
            return False
        try:
            if _sha256_file(media_path) != expected_media_hash:
                return False
        except OSError:
            return False
    source_identity = boundary.get("source_asr_identity")
    if not isinstance(source_identity, dict):
        return False
    if not (
        str(source_identity.get("provider") or "").strip()
        and str(
            source_identity.get("model")
            or source_identity.get("model_id")
            or source_identity.get("resource_id")
            or ""
        ).strip()
        and str(source_identity.get("adapter_version") or "").strip()
        and source_identity.get("preprocessing") not in (None, "", {}, [])
    ):
        return False

    if request is not None:
        try:
            expected_provenance = validate_pause_source_provenance(request)
        except PauseAlignmentError:
            return False
        for path_key, hash_key in (
            ("source_asr_path", "source_asr_sha256"),
            *media_keys,
        ):
            if (
                _normalize_audio_delivery_path(boundary.get(path_key))
                != _normalize_audio_delivery_path(expected_provenance[path_key])
                or str(boundary.get(hash_key) or "").casefold()
                != str(expected_provenance[hash_key]).casefold()
            ):
                return False
        if boundary.get("source_asr_identity") != expected_provenance["source_asr_identity"]:
            return False
        resolution_settings = {
            "min_gap_seconds": 0.35,
            "semantic_gap_seconds": 0.8,
            "search_window_seconds": 3.0,
            "edge_guard_seconds": 0.05,
            "tolerance_seconds": 0.005,
        }
        for setting, default in resolution_settings.items():
            expected_setting = _safe_float(request.pause_alignment.get(setting))
            actual_setting = _safe_float(boundary.get(setting))
            if expected_setting is None:
                expected_setting = default
            if actual_setting is None or abs(actual_setting - expected_setting) > 1e-9:
                return False

    frame_path = str(proof.get("frame_path") or proof.get("still_frame_path") or "").strip()
    frame_receipt = boundary.get("frame_match_receipt")
    if not frame_path or not isinstance(frame_receipt, dict):
        return False
    try:
        recomputed_frame_receipt = validate_pause_frame_matches_source(
            str(boundary.get("source_video_path") or ""),
            resolved,
            frame_path,
        )
    except PauseAlignmentError:
        return False
    claimed_frame_mae = _safe_float(frame_receipt.get("mean_absolute_error"))
    frame_receipt_fields = (
        "decoded_source_time",
        "frames_per_second",
        "frame_interval_seconds",
        "decoded_source_time_error_seconds",
        "maximum_decoded_source_time_error_seconds",
    )
    if (
        str(frame_receipt.get("status") or "").casefold() != "pass"
        or frame_receipt.get("method") != recomputed_frame_receipt["method"]
        or claimed_frame_mae is None
        or abs(claimed_frame_mae - recomputed_frame_receipt["mean_absolute_error"]) > 1e-3
        or any(
            _safe_float(frame_receipt.get(field)) is None
            or abs(
                float(_safe_float(frame_receipt.get(field)))
                - float(recomputed_frame_receipt[field])
            )
            > 1e-6
            for field in frame_receipt_fields
        )
    ):
        return False

    def _boundary_setting(key: str, default: float) -> float:
        value = _safe_float(boundary.get(key))
        return default if value is None else value

    try:
        recomputed = resolve_pause_boundary(
            requested,
            source_asr_payload,
            min_gap_seconds=_boundary_setting("min_gap_seconds", 0.35),
            search_window_seconds=_boundary_setting("search_window_seconds", 3.0),
            tolerance_seconds=_boundary_setting("tolerance_seconds", 0.005),
            semantic_gap_seconds=_boundary_setting("semantic_gap_seconds", 0.8),
            edge_guard_seconds=minimum_guard,
        )
    except PauseAlignmentError:
        return False

    recomputed_values = (
        (recomputed.resolved_time, resolved),
        (recomputed.previous_word_end, previous_end),
        (recomputed.next_word_start, next_start),
        (recomputed.previous_utterance_end, previous_utterance_end),
        (recomputed.next_utterance_start, next_utterance_start),
        (recomputed.gap_duration, gap_duration),
        (recomputed.previous_guard_seconds, previous_guard),
        (recomputed.next_guard_seconds, next_guard),
    )
    if any(
        actual is None or abs(float(actual) - claimed) > 1e-3
        for actual, claimed in recomputed_values
    ):
        return False
    return (
        recomputed.placement == "gap_midpoint"
        and recomputed.reason == "nearest_utterance_gap_midpoint"
        and recomputed.reason == str(boundary.get("reason") or "").strip()
    )


def _saved_semantic_pause_hold_is_proven(
    proof: Dict[str, Any],
    content: Optional[Dict[str, Any]],
    request: Optional[RevisionRequest],
) -> bool:
    if not isinstance(content, dict):
        return False
    segment_id = str(proof.get("segment_id") or "").strip()
    frame_path = str(proof.get("frame_path") or proof.get("still_frame_path") or "").strip()
    frame_sha256 = str(proof.get("frame_sha256") or "").strip().casefold()
    timeline_start = _safe_float(proof.get("timeline_start"))
    timeline_end = _safe_float(proof.get("timeline_end"))
    duration = _safe_float(proof.get("duration"))
    source_time = _safe_float(proof.get("source_time"))
    expected_track_name = str(proof.get("track_name") or "Original Video").strip()
    if (
        not segment_id
        or not frame_path
        or not re.fullmatch(r"[0-9a-f]{64}", frame_sha256)
        or timeline_start is None
        or timeline_end is None
        or duration is None
        or source_time is None
        or duration <= 0
        or abs(timeline_end - timeline_start - duration) > 1e-3
    ):
        return False

    matches: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
    for track in content.get("tracks") or []:
        if (
            not isinstance(track, dict)
            or _normalize_track_type_name(track.get("type")) != "video"
            or _normalize_track_name(track.get("name"))
            != _normalize_track_name(expected_track_name)
        ):
            continue
        for segment in track.get("segments") or []:
            if isinstance(segment, dict) and str(segment.get("id") or "") == segment_id:
                matches.append((track, segment))
    if len(matches) != 1:
        return False
    _track, segment = matches[0]
    target = segment.get("target_timerange") or {}
    source = segment.get("source_timerange") or {}
    target_start = _safe_int(target.get("start"))
    target_duration = _safe_int(target.get("duration"))
    source_start = _safe_int(source.get("start"))
    source_duration = _safe_int(source.get("duration"))
    expected_start = _seconds_to_us(timeline_start)
    expected_duration = _seconds_to_us(duration)
    if (
        target_start is None
        or target_duration is None
        or source_start != 0
        or source_duration is None
        or abs(target_start - expected_start) > 1_000
        or abs(target_duration - expected_duration) > 1_000
        or abs(source_duration - expected_duration) > 1_000
    ):
        return False

    if request is not None:
        delete_windows = _collect_delete_windows(request)
        deleted_before = sum(
            max(0.0, min(source_time, delete_end) - delete_start)
            for delete_start, delete_end in delete_windows
            if source_time > delete_start
        )
        inserted_before = sum(
            pause.duration
            for pause in request.pause_adjustments
            if pause.source_time < source_time - 1e-6
        )
        same_boundary_pauses = [
            pause
            for pause in request.pause_adjustments
            if abs(pause.source_time - source_time) <= 1e-6
        ]
        allowed_starts = [source_time - deleted_before + inserted_before]
        cumulative = 0.0
        for pause in same_boundary_pauses:
            allowed_starts.append(allowed_starts[0] + cumulative)
            cumulative += pause.duration
        if not any(abs(timeline_start - allowed_start) <= 1e-3 for allowed_start in allowed_starts):
            return False

    material_id = str(segment.get("material_id") or "")
    video_materials = [
        material
        for material in ((content.get("materials") or {}).get("videos") or [])
        if isinstance(material, dict) and str(material.get("id") or "") == material_id
    ]
    if len(video_materials) != 1:
        return False
    material = video_materials[0]
    material_path = str(
        material.get("path") or material.get("media_path") or material.get("file_path") or ""
    ).strip()
    if _normalize_audio_delivery_path(material_path) != _normalize_audio_delivery_path(frame_path):
        return False
    if str(material.get("type") or "").strip().casefold() not in {"photo", "image"}:
        return False
    try:
        with open(frame_path, "rb") as frame_file:
            if hashlib.sha256(frame_file.read()).hexdigest() != frame_sha256:
                return False
    except OSError:
        return False
    decoded_frame = cv2.imread(frame_path, cv2.IMREAD_UNCHANGED)
    if decoded_frame is None or decoded_frame.size <= 0:
        return False

    hold_start_us = expected_start
    hold_end_us = expected_start + expected_duration
    for track in content.get("tracks") or []:
        if not isinstance(track, dict) or _normalize_track_type_name(track.get("type")) != "audio":
            continue
        for audio_segment in track.get("segments") or []:
            if not isinstance(audio_segment, dict):
                continue
            volume = _safe_float(audio_segment.get("volume", 1.0))
            audio_target = audio_segment.get("target_timerange") or {}
            audio_start = _safe_int(audio_target.get("start"))
            audio_duration = _safe_int(audio_target.get("duration"))
            if volume is None or audio_start is None or audio_duration is None or volume <= 1e-6:
                continue
            if (
                min(audio_start + audio_duration, hold_end_us) - max(audio_start, hold_start_us)
                > 1_000
            ):
                return False
    return True


def _pause_fit_is_proven(
    item: RevisionReviewItem,
    content: Optional[Dict[str, Any]] = None,
    request: Optional[RevisionRequest] = None,
) -> bool:
    accepted_statuses = {"pass", "accepted", "approved"}
    semantic_pause = _normalize_acceptance_token(item.kind) == "semantic_pause_adjustment"
    semantic_pause = semantic_pause or any(
        isinstance(payload, dict) and isinstance(payload.get("semantic_pause_adjustment"), dict)
        for payload in (item.validation, item.evidence)
    )
    for payload in (item.validation, item.evidence):
        if not isinstance(payload, dict):
            continue
        pause_status = str(payload.get("pause_status") or "").strip().casefold()
        if pause_status in accepted_statuses and not semantic_pause:
            return True
        for key in ("semantic_pause_adjustment", "visual_hold_review"):
            proof = payload.get(key)
            if not isinstance(proof, dict):
                continue
            status = str(proof.get("status") or "").strip().casefold()
            if status and status not in accepted_statuses:
                continue
            duration = _safe_float(proof.get("duration"))
            if duration is None or not math.isfinite(duration) or duration <= 0:
                continue
            if key == "semantic_pause_adjustment":
                if request is not None:
                    matches = [
                        adjustment
                        for adjustment in request.pause_adjustments
                        if _normalize_review_id(adjustment.item_id)
                        == _normalize_review_id(item.item_id)
                    ]
                    if len(matches) == 1:
                        adjustment = matches[0]
                        proof = {
                            **proof,
                            "requested_source_time": adjustment.requested_source_time,
                            "source_time": adjustment.source_time,
                            "frame_source_time": adjustment.frame_source_time,
                            "frame_path": adjustment.frame_path,
                            "frame_sha256": adjustment.frame_sha256,
                            "boundary_evidence": adjustment.boundary_evidence,
                        }
                if not _semantic_pause_boundary_is_proven(proof, request):
                    continue
                if not _saved_semantic_pause_hold_is_proven(proof, content, request):
                    continue
            locator_keys = (
                "segment_id",
                "segment_ids",
                "timeline_start",
                "timeline_end",
                "timeline_window",
                "frame_path",
                "still_frame_path",
                "saved_segment_receipt",
                "timeline_receipt",
                "still_frame_receipt",
            )
            if any(
                locator_key in proof and proof.get(locator_key) not in (None, "", [], {})
                for locator_key in locator_keys
            ):
                return True
    return False


_VISUAL_OVERLAY_EVIDENCE_KINDS = (
    _POINTER_KINDS | _ANIMATION_KINDS | _VISUAL_ACCEPTANCE_KINDS
) - _VISUAL_DELETE_EVIDENCE_KINDS
_VISUAL_DELETE_OVERLAP_TOLERANCE_SECONDS = 0.000001


def _evidence_string_values(evidence: Dict[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        raw_value = evidence.get(key)
        raw_values = raw_value if isinstance(raw_value, list) else [raw_value]
        for value in raw_values:
            normalized = str(value or "").strip()
            if normalized:
                values.add(normalized)
    return values


def _visual_operation_kind(
    item: RevisionReviewItem,
    route_kind: str,
    matches: Sequence[Dict[str, Any]],
) -> str:
    operation_tokens = {
        _normalize_acceptance_token(value)
        for value in (
            item.evidence.get("operation"),
            item.evidence.get("edit_type"),
            route_kind,
            *(action.get("type") for action in matches),
        )
        if value
    }
    if operation_tokens.intersection(_VISUAL_DELETE_EVIDENCE_KINDS):
        return "delete"
    if operation_tokens.intersection(_VISUAL_OVERLAY_EVIDENCE_KINDS):
        return "overlay"
    return ""


def _is_saved_visual_overlay_track(track: Dict[str, Any]) -> bool:
    track_type = _normalize_track_type_name(track.get("type"))
    if track_type == "video":
        return (
            bool(track.get("segments"))
            and not _is_main_video_track_name(track.get("name"))
            and not _looks_like_flattened_preview_track(track)
        )
    if track_type == "text":
        return bool(track.get("segments")) and not _is_marker_track_name(track.get("name"))
    return track_type in {"sticker", "effect", "filter", "video_effect", "image"} and bool(
        track.get("segments")
    )


def _saved_material_paths_by_id(content: Dict[str, Any]) -> Dict[str, str]:
    paths: Dict[str, str] = {}
    materials = content.get("materials") or {}
    if not isinstance(materials, dict):
        return paths
    for group in materials.values():
        if not isinstance(group, list):
            continue
        for material in group:
            if not isinstance(material, dict):
                continue
            material_id = str(material.get("id") or material.get("material_id") or "").strip()
            material_path = _normalize_material_path(
                material.get("path") or material.get("material_url") or material.get("file_path")
            )
            if material_id and material_path:
                paths[material_id] = material_path
    return paths


def _overlay_evidence_matches_saved_content(
    evidence: Dict[str, Any], content: Optional[Dict[str, Any]]
) -> bool:
    if not isinstance(content, dict):
        return False
    track_names = {
        _normalize_track_name(value)
        for value in _evidence_string_values(evidence, "track_name", "track_names", "overlay_track")
    }
    segment_ids = _evidence_string_values(evidence, "segment_id", "segment_ids", "overlay_segment")
    material_ids = _evidence_string_values(evidence, "material_id", "material_ids")
    asset_paths = {
        _normalize_material_path(value)
        for value in _evidence_string_values(evidence, "asset_path", "asset_paths")
    }
    if not any((track_names, segment_ids, material_ids, asset_paths)):
        return False

    material_paths = _saved_material_paths_by_id(content)
    for track in content.get("tracks") or []:
        if not isinstance(track, dict) or not _is_saved_visual_overlay_track(track):
            continue
        if track_names and _normalize_track_name(track.get("name")) not in track_names:
            continue
        for segment in track.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            saved_segment_id = str(segment.get("id") or segment.get("segment_id") or "").strip()
            if segment_ids and saved_segment_id not in segment_ids:
                continue
            saved_material_ids = {
                str(value).strip()
                for value in (
                    segment.get("material_id"),
                    segment.get("resource_id"),
                    *(segment.get("extra_material_refs") or []),
                )
                if str(value or "").strip()
            }
            if material_ids and not saved_material_ids.intersection(material_ids):
                continue
            if asset_paths and not any(
                material_paths.get(material_id) in asset_paths for material_id in saved_material_ids
            ):
                continue
            return True
    return False


def _saved_main_video_source_intervals(
    content: Optional[Dict[str, Any]],
) -> List[tuple[float, float]]:
    intervals: List[tuple[float, float]] = []
    if not isinstance(content, dict):
        return intervals
    for track in content.get("tracks") or []:
        if not isinstance(track, dict) or not _is_main_video_track_name(track.get("name")):
            continue
        for segment in track.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            timerange = segment.get("source_timerange") or {}
            start_us = _safe_float(timerange.get("start"))
            duration_us = _safe_float(timerange.get("duration"))
            if (
                start_us is None
                or duration_us is None
                or not math.isfinite(start_us)
                or not math.isfinite(duration_us)
                or duration_us <= 0
            ):
                continue
            intervals.append(
                (
                    start_us / 1_000_000.0,
                    (start_us + duration_us) / 1_000_000.0,
                )
            )
    return intervals


def _visual_delete_source_window(
    evidence: Dict[str, Any],
) -> Optional[tuple[float, float]]:
    for key in ("source_window", "cut_window"):
        raw_window = evidence.get(key)
        if not isinstance(raw_window, (list, tuple)) or len(raw_window) != 2:
            continue
        start = _safe_float(raw_window[0])
        end = _safe_float(raw_window[1])
        if (
            start is not None
            and end is not None
            and math.isfinite(start)
            and math.isfinite(end)
            and end > start
        ):
            return (start, end)
    return None


def _delete_evidence_matches_saved_splits(
    evidence: Dict[str, Any], content: Optional[Dict[str, Any]]
) -> bool:
    deleted_window = _visual_delete_source_window(evidence)
    saved_intervals = _saved_main_video_source_intervals(content)
    if deleted_window is None or not saved_intervals:
        return False
    deleted_start, deleted_end = deleted_window
    return all(
        min(deleted_end, saved_end) - max(deleted_start, saved_start)
        <= _VISUAL_DELETE_OVERLAP_TOLERANCE_SECONDS
        for saved_start, saved_end in saved_intervals
    )


def _visual_evidence_attribution(
    item: RevisionReviewItem,
    route_kind: str,
    matches: Sequence[Dict[str, Any]],
    content: Optional[Dict[str, Any]],
) -> Dict[str, bool]:
    operation_kind = _visual_operation_kind(item, route_kind, matches)
    if operation_kind == "delete":
        return {
            "ok": _delete_evidence_matches_saved_splits(item.evidence, content),
            "requires_overlay": False,
        }
    return {
        "ok": operation_kind == "overlay"
        and _overlay_evidence_matches_saved_content(item.evidence, content),
        "requires_overlay": True,
    }


def _acceptance_failure(
    gate: str,
    reason: str,
    *,
    item_id: str = "",
    status: str = "fail",
    repairable: bool = True,
) -> Dict[str, Any]:
    return {
        "gate": gate,
        "item_id": item_id,
        "status": status,
        "repairable": repairable,
        "reason": reason,
    }


def validate_revision_acceptance(
    request: RevisionRequest,
    content: Optional[Dict[str, Any]] = None,
    *,
    draft_name: str = "",
    doc_items: Optional[List[RevisionReviewItem]] = None,
    strict: bool = False,
) -> Dict[str, Any]:
    """Validate item-level delivery evidence for review-document revision jobs."""

    strict = bool(strict or request.acceptance.require_final_acceptance)
    errors: List[str] = []
    warnings: List[str] = []

    profile = derive_acceptance_profile(request, doc_items=doc_items)
    failures: List[Dict[str, Any]] = list(profile["routing_failures"])
    errors.extend(failure["reason"] for failure in failures)
    routes_by_id = {record["normalized_item_id"]: record for record in profile["items"]}
    action_entries = _build_request_action_entries(request)
    review_items = _merge_unique_review_items(request.review_items, doc_items)
    acceptance = request.acceptance
    draft_visual_evidence = (
        _collect_draft_visual_evidence(content or {}) if content is not None else {}
    )
    smooth_pointer_validation = (
        _validate_smooth_pointer_keyframes(request, content or {})
        if content is not None and "pointer" in profile["enabled_gates"]
        else {"ok": True, "required_count": 0, "errors": [], "details": []}
    )
    errors.extend(smooth_pointer_validation["errors"])
    failures.extend(
        _acceptance_failure("pointer", reason) for reason in smooth_pointer_validation["errors"]
    )
    pointer_saved_state_validation = {
        "ok": True,
        "item_problems": {},
        "layer_problems": [],
        "pointer_item_count": 0,
    }
    if (
        strict
        and content is not None
        and request.workflow_mode != "lite"
        and "pointer" in profile["enabled_gates"]
    ):
        pointer_saved_state_validation = _pointer_saved_state_validation(
            review_items,
            routes_by_id,
            content,
        )
        for item_id, problems in pointer_saved_state_validation["item_problems"].items():
            reason = (
                f"Review item {item_id} saved pointer geometry failed: " + ", ".join(problems) + "."
            )
            errors.append(reason)
            failures.append(_acceptance_failure("pointer", reason, item_id=item_id))
        if pointer_saved_state_validation["layer_problems"]:
            reason = (
                "Saved pointer layer state failed: "
                + ", ".join(pointer_saved_state_validation["layer_problems"])
                + "."
            )
            errors.append(reason)
            failures.append(_acceptance_failure("pointer", reason))

    action_ids = [_normalize_review_id(action["id"]) for action in action_entries]
    unique_action_ids = sorted(set(action_ids))
    duplicate_action_ids = sorted(
        {item_id for item_id in action_ids if action_ids.count(item_id) > 1}
    )

    if duplicate_action_ids:
        warnings.append(
            "Revision request contains duplicate review item ids: "
            + ", ".join(duplicate_action_ids)
            + "."
        )

    if (
        (strict or acceptance.require_review_items)
        and not review_items
        and not acceptance.expected_review_item_ids
        and (acceptance.expected_review_item_count is None)
    ):
        reason = (
            "Strict acceptance validation requires review_items/doc_items or explicit expected "
            "review item ids/count."
        )
        errors.append(reason)
        failures.append(_acceptance_failure("source_coverage", reason))

    expected_count = acceptance.expected_review_item_count
    if expected_count is not None:
        actual_count = len(review_items) if review_items else len(unique_action_ids)
        if actual_count != expected_count:
            reason = (
                "Review item coverage count mismatch: "
                f"expected {expected_count}, found {actual_count}."
            )
            errors.append(reason)
            failures.append(_acceptance_failure("source_coverage", reason))

    expected_ids = [
        _normalize_review_id(item_id) for item_id in acceptance.expected_review_item_ids
    ]
    if expected_ids:
        review_items_by_id = {_normalize_review_id(item.item_id): item for item in review_items}
        expected_execution_ids = (
            [
                item_id
                for item_id in expected_ids
                if item_id in review_items_by_id and review_items_by_id[item_id].execution_required
            ]
            if review_items
            else expected_ids
        )
        missing_expected_actions = [
            item_id for item_id in expected_execution_ids if item_id not in set(action_ids)
        ]
        if missing_expected_actions:
            reason = (
                "Expected review item ids are missing from edits/markers/evidence: "
                + ", ".join(missing_expected_actions)
                + "."
            )
            errors.append(reason)
            failures.append(_acceptance_failure("source_coverage", reason))
        if review_items:
            item_ids = {_normalize_review_id(item.item_id) for item in review_items}
            missing_expected_items = [
                item_id for item_id in expected_ids if item_id not in item_ids
            ]
            if missing_expected_items:
                reason = (
                    "Expected review item ids are missing from the review-item ledger: "
                    + ", ".join(missing_expected_items)
                    + "."
                )
                errors.append(reason)
                failures.append(_acceptance_failure("source_coverage", reason))

    missing_item_ids: List[str] = []
    marker_only_item_ids: List[str] = []
    visual_required_items: List[RevisionReviewItem] = []
    visual_overlay_required_items: List[RevisionReviewItem] = []
    visual_without_item_evidence: List[str] = []
    audio_without_validation: List[str] = []
    audio_unresolved_validation: List[str] = []
    pause_without_validation: List[str] = []
    pointer_landing_errors: List[str] = []
    subject_pointer_binding_errors: List[str] = []
    pointer_lifecycle_errors: Dict[str, List[str]] = {}
    animation_evidence_errors: Dict[str, List[str]] = {}

    enabled_gates = set(profile["enabled_gates"])
    semantic_pause_item_ids = [
        item.item_id
        for item in review_items
        if item.execution_required
        and (
            _normalize_acceptance_token(item.kind) == "semantic_pause_adjustment"
            or any(
                isinstance(payload, dict)
                and isinstance(payload.get("semantic_pause_adjustment"), dict)
                for payload in (item.validation, item.evidence)
            )
        )
    ]
    require_processed_audio_summary = bool(
        enabled_gates.intersection({"audio_precision", "audio_join"})
        or (strict and semantic_pause_item_ids)
    )
    required_reverse_asr_item_ids = list(
        dict.fromkeys(
            str(record.get("item_id") or "").strip()
            for record in profile["items"]
            if record.get("has_review_item")
            and record.get("execution_required")
            and {"audio_precision", "audio_join"}.intersection(record.get("gates") or [])
            and str(record.get("item_id") or "").strip()
        )
    )
    spoken_contract_item_ids = [
        str(record.get("item_id") or "").strip()
        for record in profile["items"]
        if record.get("has_review_item")
        and record.get("execution_required")
        and _normalize_acceptance_token(record.get("kind")) in _SPOKEN_DELETE_CONTRACT_KINDS
        and str(record.get("item_id") or "").strip()
    ]
    if require_processed_audio_summary:
        processed_audio_summary_validation = _validate_processed_audio_summary(
            request,
            semantic_pause_item_ids=(semantic_pause_item_ids if strict else ()),
            required_item_ids=required_reverse_asr_item_ids,
            spoken_contract_item_ids=spoken_contract_item_ids,
        )
        errors.extend(processed_audio_summary_validation["errors"])
        routed_audio_item_ids = list(
            dict.fromkeys(
                str(record.get("item_id") or "").strip()
                for record in profile["items"]
                if record.get("has_review_item")
                and record.get("execution_required")
                and {"audio_precision", "audio_join"}.intersection(record.get("gates") or [])
                and str(record.get("item_id") or "").strip()
            )
        )
        semantic_pause_normalized_ids = {
            _normalize_review_id(item_id) for item_id in semantic_pause_item_ids
        }
        scoped_reasons: set[str] = set()
        for item_id, item_reasons in processed_audio_summary_validation.get(
            "item_errors", {}
        ).items():
            for reason in item_reasons:
                scoped_reasons.add(reason)
                if _normalize_review_id(item_id) in semantic_pause_normalized_ids:
                    gate = "pause_fit"
                else:
                    gate = "audio_join" if "semantic join" in reason.lower() else "audio_precision"
                failures.append(_acceptance_failure(gate, reason, item_id=item_id))
        for reason in processed_audio_summary_validation["errors"]:
            if reason in scoped_reasons:
                continue
            gate = "audio_join" if "semantic join" in reason.lower() else "audio_precision"
            if gate not in enabled_gates:
                gate = "audio_join"
            if routed_audio_item_ids:
                failures.extend(
                    _acceptance_failure(gate, reason, item_id=item_id)
                    for item_id in routed_audio_item_ids
                )
            else:
                failures.append(_acceptance_failure(gate, reason))
    else:
        processed_audio_summary_validation = {
            "path": "",
            "errors": [],
            "unresolved_statuses": [],
            "unresolved_ids": [],
            "semantic_join_anomalies": [],
            "semantic_pause_reverse_asr_problems": [],
            "item_errors": {},
            "candidate_audio_path": "",
            "candidate_audio_sha256": "",
            "candidate_audio_duration_seconds": None,
            "asr_identity": {},
            "result_count": 0,
        }
    timeline_mapping_validation = _validate_timeline_plan_mapping(request)
    errors.extend(timeline_mapping_validation["errors"])
    failures.extend(
        _acceptance_failure("visual", reason) for reason in timeline_mapping_validation["errors"]
    )
    lite_visual_start_validation = _validate_lite_visual_start_alignment(request, content)
    errors.extend(lite_visual_start_validation["errors"])
    failures.extend(
        _acceptance_failure("execution_evidence", reason)
        for reason in lite_visual_start_validation["errors"]
    )

    for item in review_items:
        route = routes_by_id.get(_normalize_review_id(item.item_id), {})
        item_gates = set(route.get("gates") or [])
        kind = str(route.get("kind") or item.kind or _classify_review_text(item.source_text))
        matches = _matching_actions_for_item(item, action_entries)
        item_has_execution_evidence = _evidence_has_execution(item.evidence)
        item_has_validation = _evidence_has_validation(item.validation, item.evidence)
        item_audio_validation_passed = _audio_validation_is_pass(item.validation, item.evidence)

        lifecycle_required = request.workflow_mode != "lite" and strict and (
            "pointer" in item_gates
            or kind == "pointer_overlay"
            or _classify_review_text(item.source_text) == "pointer_overlay"
        )
        if lifecycle_required:
            lifecycle_problems = pointer_lifecycle_evidence_problems(item.evidence)
            if lifecycle_problems:
                pointer_lifecycle_errors[item.item_id] = lifecycle_problems
                reason = (
                    f"Review item {item.item_id} pointer lifecycle evidence failed: "
                    + ", ".join(lifecycle_problems)
                    + "."
                )
                errors.append(reason)
                failures.append(_acceptance_failure("pointer", reason, item_id=item.item_id))

        if _status_is_failure(item.evidence) or _status_is_failure(item.validation):
            reason = f"Review item {item.item_id} has failing evidence/validation status."
            errors.append(reason)
            failure_gates = [
                gate for gate in _CONDITIONAL_ACCEPTANCE_GATES if gate in item_gates
            ] or ["execution_evidence"]
            failures.extend(
                _acceptance_failure(
                    gate,
                    reason,
                    item_id=item.item_id,
                    repairable=False,
                )
                for gate in failure_gates
            )

        if strict and item.execution_required and "animation" in item_gates:
            animation_problems = animation_evidence_problems(item.evidence)
            if animation_problems:
                animation_evidence_errors[item.item_id] = animation_problems
                reason = (
                    f"Review item {item.item_id} animation evidence failed: "
                    + ", ".join(animation_problems)
                    + "."
                )
                errors.append(reason)
                failures.append(_acceptance_failure("animation", reason, item_id=item.item_id))

        if (
            item.execution_required
            and "pause_fit" in item_gates
            and not _pause_fit_is_proven(item, content, request)
        ):
            pause_without_validation.append(item.item_id)
            reason = (
                f"Review item {item.item_id} requires accepted pause-fit proof "
                "(pause_status, semantic_pause_adjustment, or visual_hold_review)."
            )
            errors.append(reason)
            failures.append(_acceptance_failure("pause_fit", reason, item_id=item.item_id))

        if not matches:
            if item.execution_required:
                missing_item_ids.append(item.item_id)
                reason = f"Review item {item.item_id} is missing from edits and markers."
                errors.append(reason)
                failures.append(
                    _acceptance_failure("execution_evidence", reason, item_id=item.item_id)
                )
            elif _review_item_role(item.item_id, item.source_text) == "修改":
                warnings.append(
                    f"Review item {item.item_id} is labeled 修改 but execution_required=false."
                )
            continue

        executed_by_action = any(
            _action_provides_execution_for_kind(action, kind) for action in matches
        )
        marker_only = bool(matches) and not executed_by_action

        if (
            request.workflow_mode != "lite"
            and "pointer" in item_gates
            and (strict or acceptance.require_subject_pointer_binding)
        ):
            receipt = item.evidence.get("subject_profile_receipt")
            if not isinstance(receipt, dict) or not receipt:
                subject_pointer_binding_errors.append(item.item_id)
                reason = (
                    f"Review item {item.item_id} requires a fresh subject pointer "
                    "binding receipt."
                )
                errors.append(reason)
                failures.append(_acceptance_failure("pointer", reason, item_id=item.item_id))
            else:
                receipt_validation = _fresh_subject_pointer_receipt_validation(receipt, request)
                if not receipt_validation["ok"]:
                    subject_pointer_binding_errors.append(item.item_id)
                    reason = (
                        f"Review item {item.item_id} subject pointer binding receipt "
                        "failed fresh validation: "
                        + ", ".join(receipt_validation["problems"])
                        + "."
                    )
                    errors.append(reason)
                    failures.append(_acceptance_failure("pointer", reason, item_id=item.item_id))
                else:
                    overlay_problems = (
                        []
                        if _is_cleanup_only_pointer_evidence(item.evidence)
                        else _pointer_overlay_receipt_problems(item.evidence, receipt, content)
                    )
                    if overlay_problems:
                        subject_pointer_binding_errors.append(item.item_id)
                        reason = (
                            f"Review item {item.item_id} subject pointer receipt "
                            "does not match the saved overlay: " + ", ".join(overlay_problems) + "."
                        )
                        errors.append(reason)
                        failures.append(
                            _acceptance_failure("pointer", reason, item_id=item.item_id)
                        )

        if item.execution_required:
            if marker_only and not item_has_execution_evidence:
                marker_only_item_ids.append(item.item_id)
                reason = (
                    f"Review item {item.item_id} is represented only by marker labels; "
                    "markers are not execution evidence."
                )
                errors.append(reason)
                failures.append(
                    _acceptance_failure("execution_evidence", reason, item_id=item.item_id)
                )
            elif not executed_by_action and not item_has_execution_evidence:
                reason = f"Review item {item.item_id} lacks execution evidence."
                errors.append(reason)
                failures.append(
                    _acceptance_failure("execution_evidence", reason, item_id=item.item_id)
                )

            if request.workflow_mode != "lite" and "visual" in item_gates:
                visual_required_items.append(item)
                visual_attribution = _visual_evidence_attribution(item, kind, matches, content)
                if visual_attribution["requires_overlay"]:
                    visual_overlay_required_items.append(item)
                if not visual_attribution["ok"]:
                    visual_without_item_evidence.append(item.item_id)
                    reason = (
                        f"Review item {item.item_id} requires attributable per-item "
                        "visual evidence in the saved draft."
                    )
                    errors.append(reason)
                    failures.append(_acceptance_failure("visual", reason, item_id=item.item_id))
                if "pointer" in item_gates and _pointer_landing_required(item):
                    if not _pointer_landing_is_proven(item.evidence, item.validation):
                        pointer_landing_errors.append(item.item_id)
                        reason = (
                            f"Review item {item.item_id} has pointer landing evidence based only on planned "
                            "coordinates; rendered/opened hotspot landing proof is required."
                        )
                        errors.append(reason)
                        failures.append(
                            _acceptance_failure("pointer", reason, item_id=item.item_id)
                        )
            if "audio_precision" in item_gates:
                if not item_audio_validation_passed:
                    status = _audio_validation_status(item.validation, item.evidence)
                    if item_has_validation:
                        audio_unresolved_validation.append(item.item_id)
                        reason = (
                            f"Review item {item.item_id} has unresolved audio validation status: "
                            f"{status or 'missing'}."
                        )
                        errors.append(reason)
                    else:
                        audio_without_validation.append(item.item_id)
                        reason = f"Review item {item.item_id} requires delete/must-keep validation evidence."
                        errors.append(reason)
                    failures.append(
                        _acceptance_failure("audio_precision", reason, item_id=item.item_id)
                    )

        elif _review_item_role(item.item_id, item.source_text) == "修改":
            warnings.append(
                f"Review item {item.item_id} is labeled 修改 but execution_required=false."
            )

    if visual_overlay_required_items:
        if content is None:
            reason = "Saved draft content is required to validate visual overlay evidence."
            errors.append(reason)
            failures.append(_acceptance_failure("visual", reason))
        elif not draft_visual_evidence.get("has_visual_overlay_evidence"):
            reason = (
                "Draft has visual review items but no non-marker overlay tracks/materials "
                "(hands/arrows/underline/animation overlays are missing)."
            )
            errors.append(reason)
            failures.append(_acceptance_failure("visual", reason))
        elif draft_visual_evidence.get("visual_segment_count", 0) and draft_visual_evidence.get(
            "visual_segment_count", 0
        ) < len(visual_overlay_required_items):
            warnings.append(
                "Draft has fewer visual overlay segments than visual review items; "
                "per-item evidence should prove any shared overlay windows."
            )

    return {
        "ok": not errors,
        "strict": strict,
        "draft_name": draft_name,
        "errors": errors,
        "warnings": warnings,
        "failures": failures,
        "metrics": {
            "enabled_gates": profile["enabled_gates"],
            "skipped_gates": profile["skipped_gates"],
            "gate_reasons": profile["gate_reasons"],
            "review_item_count": len(review_items),
            "action_entry_count": len(action_entries),
            "unique_action_id_count": len(unique_action_ids),
            "expected_review_item_count": expected_count,
            "expected_review_item_ids": acceptance.expected_review_item_ids,
            "missing_item_ids": missing_item_ids,
            "marker_only_item_ids": marker_only_item_ids,
            "visual_required_item_count": len(visual_required_items),
            "visual_without_item_evidence": visual_without_item_evidence,
            "audio_without_validation": audio_without_validation,
            "audio_unresolved_validation": audio_unresolved_validation,
            "pause_without_validation": pause_without_validation,
            "processed_audio_summary": processed_audio_summary_validation,
            "timeline_mapping": timeline_mapping_validation,
            "timeline_mapping_errors": timeline_mapping_validation["timeline_mapping_errors"],
            "lite_visual_start_alignment": lite_visual_start_validation,
            "pointer_landing_errors": pointer_landing_errors,
            "subject_pointer_binding_errors": subject_pointer_binding_errors,
            "pointer_lifecycle_errors": pointer_lifecycle_errors,
            "animation_evidence_errors": animation_evidence_errors,
            "draft_visual_evidence": draft_visual_evidence,
            "smooth_pointer_keyframes": smooth_pointer_validation,
            "pointer_saved_state": pointer_saved_state_validation,
        },
    }


def validate_revision_acceptance_variants(
    request: RevisionRequest,
    content_variants: Sequence[tuple[str, Optional[Dict[str, Any]]]],
    *,
    draft_name: str = "",
    doc_items: Optional[List[RevisionReviewItem]] = None,
    strict: bool = False,
) -> Dict[str, Any]:
    """Run full acceptance independently for root and every declared timeline."""

    variants = list(content_variants)
    if not variants:
        variants = [("root", None)]

    primary_name, primary_content = variants[0]
    combined = validate_revision_acceptance(
        request,
        primary_content,
        draft_name=draft_name,
        doc_items=doc_items,
        strict=strict,
    )
    variant_results: Dict[str, Dict[str, Any]] = {
        primary_name: {
            "ok": combined["ok"],
            "errors": list(combined.get("errors") or []),
            "metrics": deepcopy(combined.get("metrics") or {}),
        }
    }
    unresolved_item_ids = {
        str(item_id)
        for item_id in combined.get("unresolved_item_ids") or []
        if str(item_id).strip()
    }

    for variant_name, variant_content in variants[1:]:
        variant_validation = validate_revision_acceptance(
            request,
            variant_content,
            draft_name=draft_name,
            doc_items=doc_items,
            strict=strict,
        )
        variant_results[variant_name] = {
            "ok": variant_validation["ok"],
            "errors": list(variant_validation.get("errors") or []),
            "metrics": deepcopy(variant_validation.get("metrics") or {}),
        }
        combined.setdefault("errors", []).extend(
            f"[{variant_name}] {message}" for message in variant_validation.get("errors") or []
        )
        combined.setdefault("warnings", []).extend(
            f"[{variant_name}] {message}" for message in variant_validation.get("warnings") or []
        )
        for failure in variant_validation.get("failures") or []:
            copied_failure = dict(failure)
            copied_failure["variant"] = variant_name
            copied_failure["reason"] = f"[{variant_name}] {copied_failure.get('reason') or ''}"
            combined.setdefault("failures", []).append(copied_failure)
        unresolved_item_ids.update(
            str(item_id)
            for item_id in variant_validation.get("unresolved_item_ids") or []
            if str(item_id).strip()
        )

    combined["ok"] = all(result["ok"] for result in variant_results.values())
    combined["unresolved_item_ids"] = sorted(unresolved_item_ids)
    combined.setdefault("metrics", {})["acceptance_validation_variants"] = variant_results
    return combined


def _semantic_pause_main_video_problems(
    request: RevisionRequest,
    content: Dict[str, Any],
) -> List[str]:
    if not request.pause_adjustments or not request.pause_alignment:
        return []
    tracks = content.get("tracks") or []
    main_track = next(
        (
            track
            for track in tracks
            if isinstance(track, dict)
            and _normalize_track_type_name(track.get("type")) == "video"
            and _normalize_track_name(track.get("name")) == "original video"
        ),
        None,
    )
    if not isinstance(main_track, dict):
        return ["Semantic pause source-video split is missing the Original Video track."]

    saved_output_duration = max(
        (
            (
                (_safe_int((segment.get("target_timerange") or {}).get("start")) or 0)
                + (_safe_int((segment.get("target_timerange") or {}).get("duration")) or 0)
            )
            / 1_000_000.0
            for segment in (main_track.get("segments") or [])
            if isinstance(segment, dict)
        ),
        default=0.0,
    )
    delete_duration = sum(end - start for start, end in _collect_delete_windows(request))
    source_duration = (
        saved_output_duration
        - sum(pause.duration for pause in request.pause_adjustments)
        + delete_duration
    )
    if source_duration <= 0:
        return ["Semantic pause source-video split cannot resolve source media duration."]

    problems: List[str] = []
    declared_duration = request.project.media_duration_seconds
    if declared_duration > 0 and abs(declared_duration - source_duration) > 0.1:
        problems.append(
            "project.media_duration_seconds does not match the saved timeline-derived source "
            f"duration: declared {declared_duration:.3f}s, derived {source_duration:.3f}s."
        )

    expected_windows = _build_keep_windows(request, source_duration)
    materials = content.get("materials") or {}
    video_path_by_id = {
        str(material.get("id") or ""): _normalize_audio_delivery_path(
            material.get("path") or material.get("media_path") or material.get("file_path")
        )
        for material in (materials.get("videos") or [])
        if isinstance(material, dict)
    }
    expected_source_path = _normalize_audio_delivery_path(request.project.source_video)
    source_segments = sorted(
        (
            segment
            for segment in (main_track.get("segments") or [])
            if isinstance(segment, dict)
            and video_path_by_id.get(str(segment.get("material_id") or "")) == expected_source_path
        ),
        key=lambda segment: (
            _safe_int((segment.get("source_timerange") or {}).get("start")) or 0,
            str(segment.get("id") or ""),
        ),
    )
    if len(source_segments) != len(expected_windows):
        problems.append(
            "Semantic pause source-video split does not expose every kept source window: "
            f"expected {len(expected_windows)}, found {len(source_segments)}."
        )
        return problems

    delete_windows = _collect_delete_windows(request)
    for index, (segment, (window_start, window_end)) in enumerate(
        zip(source_segments, expected_windows),
        start=1,
    ):
        source_range = segment.get("source_timerange") or {}
        target_range = segment.get("target_timerange") or {}
        actual_source_start = _safe_int(source_range.get("start"))
        actual_source_duration = _safe_int(source_range.get("duration"))
        actual_target_start = _safe_int(target_range.get("start"))
        actual_target_duration = _safe_int(target_range.get("duration"))
        expected_duration = window_end - window_start
        deleted_before = sum(
            max(0.0, min(window_start, delete_end) - delete_start)
            for delete_start, delete_end in delete_windows
            if window_start > delete_start
        )
        inserted_at_or_before = sum(
            pause.duration
            for pause in request.pause_adjustments
            if pause.source_time <= window_start + 1e-6
        )
        expected_target_start = window_start - deleted_before + inserted_at_or_before
        expected_values = (
            (actual_source_start, _seconds_to_us(window_start)),
            (actual_source_duration, _seconds_to_us(expected_duration)),
            (actual_target_start, _seconds_to_us(expected_target_start)),
            (actual_target_duration, _seconds_to_us(expected_duration)),
        )
        if any(
            actual is None or abs(actual - expected) > 2_000 for actual, expected in expected_values
        ):
            problems.append(
                f"Semantic pause source-video split window {index} does not match the "
                "resolved source-to-timeline mapping."
            )
    return problems


def validate_saved_revision_draft(
    request: RevisionRequest,
    content: Dict[str, Any],
    *,
    draft_name: str = "",
    doc_items: Optional[List[RevisionReviewItem]] = None,
    marker_receipts: Optional[List[Any]] = None,
    marker_plan: Optional[Sequence[MarkerPlanItem]] = None,
    strict: bool = False,
) -> Dict[str, Any]:
    strict = bool(strict or request.acceptance.require_final_acceptance)
    summary = build_revision_summary(request, doc_items=doc_items)
    if marker_plan is None:
        marker_plan = map_marker_plan_to_timeline(
            build_marker_plan(request, doc_items=doc_items),
            request,
        )
    else:
        marker_plan = list(marker_plan)
    marker_validation = validate_saved_marker_plan(marker_plan, content, marker_receipts)
    total_duration_us = int(content.get("duration", 0) or 0)
    tracks = content.get("tracks") or []
    materials = content.get("materials") or {}

    errors: List[str] = []
    warnings: List[str] = []
    semantic_pause_main_video_problems = _semantic_pause_main_video_problems(request, content)
    errors.extend(semantic_pause_main_video_problems)

    marker_layout_problems = review_marker_top_layout_problems(content) if strict else []
    errors.extend(marker_layout_problems)

    pointer_saved_state_validation = {
        "ok": True,
        "item_problems": {},
        "layer_problems": [],
        "pointer_item_count": 0,
    }
    if strict:
        saved_profile = derive_acceptance_profile(request, doc_items=doc_items)
        if request.workflow_mode != "lite" and "pointer" in saved_profile["enabled_gates"]:
            saved_routes_by_id = {
                record["normalized_item_id"]: record for record in saved_profile["items"]
            }
            saved_review_items = _merge_unique_review_items(
                request.review_items,
                doc_items,
            )
            pointer_saved_state_validation = _pointer_saved_state_validation(
                saved_review_items,
                saved_routes_by_id,
                content,
            )
            for item_id, problems in pointer_saved_state_validation["item_problems"].items():
                errors.append(
                    f"Review item {item_id} saved pointer geometry failed: "
                    + ", ".join(problems)
                    + "."
                )
            if pointer_saved_state_validation["layer_problems"]:
                errors.append(
                    "Saved pointer layer state failed: "
                    + ", ".join(pointer_saved_state_validation["layer_problems"])
                    + "."
                )

    track_names = [str(track.get("name", "")) for track in tracks]

    video_tracks = [
        track for track in tracks if _normalize_track_type_name(track.get("type")) == "video"
    ]
    audio_tracks = [
        track for track in tracks if _normalize_track_type_name(track.get("type")) == "audio"
    ]
    text_tracks = [
        track for track in tracks if _normalize_track_type_name(track.get("type")) == "text"
    ]
    replacement_glyph_evidence = _collect_replacement_glyph_evidence(
        content,
        allowed_text_material_ids=marker_validation["metrics"]["exact_marker_material_ids"],
        allowed_text_values=marker_validation["metrics"]["exact_marker_text_values"],
    )

    video_segment_count = sum(len(track.get("segments") or []) for track in video_tracks)
    audio_segment_count = sum(len(track.get("segments") or []) for track in audio_tracks)
    review_marker_track_count = sum(
        1 for track in text_tracks if _is_marker_track_name(track.get("name"))
    )
    review_marker_segment_count = sum(
        len(track.get("segments") or [])
        for track in text_tracks
        if _is_marker_track_name(track.get("name"))
    )

    expected_review_marker_count = int(summary["review_marker_count"])
    edit_count = int(summary["edit_count"])
    keep_cut_points = bool(request.preserve.keep_cut_points)
    expected_video_segments = len(
        _build_keep_windows(request, max(0.0, total_duration_us / 1_000_000.0))
    )

    errors.extend(marker_validation["errors"])
    warnings.extend(marker_validation["warnings"])

    if "video_track" in summary["required_tracks"] and video_segment_count <= 0:
        errors.append("Draft does not contain any video segments.")
    if "source_audio_track" in summary["required_tracks"] and audio_segment_count <= 0:
        errors.append("Draft does not contain any audio segments.")
    if request.workflow_mode != "lite" and "replacement_audio_track" in summary["required_tracks"]:
        replacement_tracks = [
            track
            for track in audio_tracks
            if _normalize_track_name(track.get("name")) == "replacement audio"
        ]
        replacement_segment_count = sum(
            len(track.get("segments") or []) for track in replacement_tracks
        )
        if replacement_segment_count <= 0:
            errors.append("Replacement audio track is missing audible replacement segments.")
    if "review_marker_tracks" in summary["required_tracks"] and review_marker_track_count <= 0:
        errors.append("Draft is missing review-marker tracks.")
    if replacement_glyph_evidence["has_replacement_glyphs"]:
        errors.append(
            "Draft contains replacement-glyph question marks in track names or text materials; "
            "Chinese labels were corrupted before save."
        )
    source_ledger_markers = doc_items is not None or bool(request.review_items)
    marker_count_mismatch = (
        review_marker_segment_count != expected_review_marker_count
        if source_ledger_markers
        else review_marker_segment_count < expected_review_marker_count
    )
    if marker_count_mismatch and (source_ledger_markers or expected_review_marker_count > 0):
        count_rule = "exactly" if source_ledger_markers else "at least"
        errors.append(
            "Draft review markers are incomplete: "
            f"expected {count_rule} {expected_review_marker_count}, "
            f"found {review_marker_segment_count}."
        )

    if any(_looks_like_flattened_preview_track(track) for track in tracks):
        errors.append("Draft still uses preview-style Final Video/Final Audio tracks.")

    main_video_track = next(
        (
            track
            for track in video_tracks
            if _normalize_track_name(track.get("name")) == "original video"
        ),
        video_tracks[0] if video_tracks else None,
    )
    if main_video_track is not None:
        main_video_segment_count = len(main_video_track.get("segments") or [])
        if (
            keep_cut_points
            and edit_count > 1
            and _track_has_single_full_length_segment(main_video_track, total_duration_us)
        ):
            errors.append(
                "Draft collapsed the main video timeline into one full-length segment while keep_cut_points=true."
        )
        if (
            request.workflow_mode != "lite"
            and keep_cut_points
            and edit_count > 1
            and main_video_segment_count < max(2, expected_video_segments)
        ):
            errors.append(
                "Draft lost visible edit structure: "
                f"expected at least {expected_video_segments} main video segments, found {main_video_segment_count}."
            )
    elif keep_cut_points:
        errors.append("Draft is missing a main video track.")

    if keep_cut_points and edit_count > 1 and review_marker_track_count <= 0:
        errors.append(
            "Revision draft does not expose the editing process through review-marker tracks."
        )

    expected_material_paths: Dict[str, str] = {}
    if request.preserve.source_video_material:
        expected_material_paths["source_video"] = _normalize_audio_delivery_path(
            request.project.source_video
        )
    if request.preserve.separated_audio_material and (
        request.project.source_audio or request.project.source_video
    ):
        expected_material_paths["source_audio"] = _normalize_audio_delivery_path(
            request.project.source_audio or request.project.source_video
        )
    replacement_audio_paths: set[str] = set()
    if request.preserve.replacement_audio_material:
        replacement_audio_paths = {
            _normalize_audio_delivery_path(path)
            for path in _replacement_audio_paths_for_request(request)
            if path
        }

    actual_video_paths = {
        _normalize_audio_delivery_path(
            item.get("path") or item.get("material_url") or item.get("file_path")
        )
        for item in materials.get("videos", []) or []
    }
    actual_audio_paths = {
        _normalize_audio_delivery_path(
            item.get("path") or item.get("material_url") or item.get("file_path")
        )
        for item in materials.get("audios", []) or []
    }

    source_video_path = expected_material_paths.get("source_video")
    if source_video_path and source_video_path not in actual_video_paths:
        errors.append("Source video material is missing from draft metadata.")

    source_audio_path = expected_material_paths.get("source_audio")
    if source_audio_path and source_audio_path not in actual_audio_paths:
        errors.append("Separated/source audio material is missing from draft metadata.")

    if replacement_audio_paths and not replacement_audio_paths.issubset(actual_audio_paths):
        errors.append("Replacement audio material is missing from draft metadata.")

    audio_delivery_validation = _audio_delivery_validation(
        request,
        audio_tracks=audio_tracks,
        materials=materials,
        total_duration_us=total_duration_us,
        doc_items=doc_items,
    )
    errors.extend(audio_delivery_validation["errors"])

    if (
        keep_cut_points
        and edit_count > 1
        and review_marker_segment_count == expected_review_marker_count
    ):
        warnings.append(
            "Revision draft passed structure validation and kept independently traceable review markers."
        )

    return {
        "ok": not errors,
        "draft_name": draft_name,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "video_track_count": len(video_tracks),
            "audio_track_count": len(audio_tracks),
            "video_segment_count": video_segment_count,
            "audio_segment_count": audio_segment_count,
            "review_marker_track_count": review_marker_track_count,
            "review_marker_segment_count": review_marker_segment_count,
            "expected_review_marker_count": expected_review_marker_count,
            "expected_main_video_segment_count": expected_video_segments,
            "track_names": track_names,
            "replacement_glyph_evidence": replacement_glyph_evidence,
            "marker_validation": marker_validation["metrics"],
            "review_marker_top_layout_problems": marker_layout_problems,
            "audio_delivery": audio_delivery_validation["metrics"],
            "pointer_saved_state": pointer_saved_state_validation,
            "semantic_pause_main_video_problems": semantic_pause_main_video_problems,
        },
    }
