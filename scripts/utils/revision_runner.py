# ruff: noqa: I001
import json
import math
import os
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional

from utils.env_setup import setup_env
from utils.formatters import get_duration_ffprobe_cached
from utils.revision_markers import (
    build_marker_plan,
    map_marker_plan_to_timeline,
)
from utils.revision_models import (
    AcceptanceRules,
    PauseAdjustment,
    PreservationRules,
    RevisionEdit,
    RevisionMarker,
    RevisionProject,
    RevisionRequest,
    RevisionReviewItem,
    _VISUAL_VIDEO_EXTENSIONS,
    _build_keep_windows,
    _clean_track_name,
    _collect_delete_windows,
    _collect_replacement_edits,
    _edit_review_id,
    _is_visual_edit,
    _normalize_review_id,
    _replacement_audio_paths_for_request,
    _request_uses_full_track_replacement_audio,
    _visual_kind_for_edit,
    _visual_plan_segments,
    build_revision_summary,
    lite_pause_change_is_label_only,
    load_review_items_json,
    load_revision_request,
    summarize_revision_request,
)
from utils.revision_repair import (
    RevisionAcceptanceError,
    restore_saved_draft_files as _restore_saved_draft_files,
    run_targeted_acceptance_repair,
    snapshot_saved_draft_files as _snapshot_saved_draft_files,
)
from utils.revision_evidence import (
    audio_delivery_plan_sha256,
    bind_audio_delivery_plan_to_report,
    normalize_pause_adjustments,
    validate_semantic_pause_pairing,
)
from utils.revision_validation import (
    _audio_files_share_identity,
    _processed_audio_candidate_path,
    derive_acceptance_profile,
    validate_revision_acceptance,
    validate_revision_acceptance_variants,
    validate_saved_revision_draft,
)

setup_env()

__all__ = [
    "AcceptanceRules",
    "PauseAdjustment",
    "PreservationRules",
    "RevisionEdit",
    "RevisionMarker",
    "RevisionProject",
    "RevisionRequest",
    "RevisionReviewItem",
    "RevisionAcceptanceError",
    "build_revision_summary",
    "derive_acceptance_profile",
    "execute_revision_request",
    "load_review_items_json",
    "load_revision_request",
    "run_targeted_acceptance_repair",
    "summarize_revision_request",
    "validate_revision_acceptance",
    "validate_revision_acceptance_variants",
    "validate_saved_revision_draft",
    "normalize_pause_adjustments",
    "audio_delivery_plan_sha256",
    "bind_audio_delivery_plan_to_report",
]


def _contains_evidence_token(value: Any, token: str) -> bool:
    normalized_token = token.casefold()
    if isinstance(value, dict):
        return any(
            str(key).strip().casefold() == normalized_token
            or _contains_evidence_token(child, token)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_evidence_token(child, token) for child in value)
    return isinstance(value, str) and value.strip().casefold() == normalized_token


def _derive_revision_ui_policy(
    request: RevisionRequest,
    *,
    doc_items: Optional[List[RevisionReviewItem]] = None,
) -> Dict[str, Any]:
    evidence_payloads: List[Any] = []
    review_items = doc_items if doc_items is not None else request.review_items
    for item in review_items:
        evidence_payloads.extend((item.evidence, item.validation))
    for edit in request.edits:
        evidence_payloads.extend((edit.evidence, edit.validation, edit.visual_plan))

    reason = ""
    if any(
        _contains_evidence_token(payload, "residual_pointer_cover") for payload in evidence_payloads
    ):
        reason = "residual_pointer_cover"
    elif any(
        _contains_evidence_token(payload, "opened_draft_display_drift")
        or _contains_evidence_token(payload, "opened_draft_drift_artifact_sha256")
        for payload in evidence_payloads
    ):
        reason = "opened_draft_display_drift"

    return {
        "ui_mode": "offline",
        "opened_state_required": bool(reason),
        "opened_state_reason": reason,
        "opened_state_status": "pending_opened_verify" if reason else "not_required",
        "controller_calls": [],
    }


def _import_runtime_components():
    from core.mocking_ops import MockAudioMaterial, MockVideoMaterial
    from core.review_marker_ops import ReviewMarkerItem
    from jy_wrapper import JyProject
    import pyJianYingDraft as draft

    return draft, ReviewMarkerItem, MockAudioMaterial, MockVideoMaterial, JyProject


def _read_saved_draft_json(path: str, variant_name: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            content = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Saved draft variant {variant_name} is not readable JSON: {path}: {exc}"
        ) from exc
    if not isinstance(content, dict):
        raise RuntimeError(f"Saved draft variant {variant_name} is not a JSON object: {path}.")
    return content


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


def _load_saved_draft_content_variants(
    save_result: Dict[str, Any],
) -> List[tuple[str, Dict[str, Any]]]:
    draft_path = _saved_draft_directory(save_result)

    variants: List[tuple[str, Dict[str, Any]]] = []
    root_path = os.path.join(draft_path, "draft_content.json")
    root_error: Optional[RuntimeError] = None
    try:
        variants.append(("root", _read_saved_draft_json(root_path, "root")))
    except RuntimeError as exc:
        root_error = exc

    layout_path = os.path.join(draft_path, "timeline_layout.json")
    if os.path.exists(layout_path):
        layout = _read_saved_draft_json(layout_path, "timeline_layout")
        active_timeline = str(layout.get("activeTimeline") or "").strip()
        if active_timeline:
            active_path = os.path.join(
                draft_path,
                "Timelines",
                active_timeline,
                "draft_content.json",
            )
            variant_name = f"active_timeline:{active_timeline}"
            if not os.path.isfile(active_path):
                raise RuntimeError(
                    f"Saved draft variant {variant_name} does not exist: {active_path}."
                )
            variants.append((variant_name, _read_saved_draft_json(active_path, variant_name)))

    if root_error is not None:
        raise root_error
    if not variants:
        raise RuntimeError(f"Saved draft has no readable content variants: {draft_path}.")
    return variants


def _seconds_to_us(value: float) -> int:
    return int(round(float(value) * 1_000_000))


def _mock_duration_for_request(request: RevisionRequest) -> float:
    return max(
        request.project.media_duration_seconds,
        _max_request_end(request) + 5.0,
        30.0,
    )


def _max_request_end(request: RevisionRequest) -> float:
    max_end = 0.0
    for item in request.edits:
        max_end = max(max_end, item.end)
    for item in request.markers:
        max_end = max(max_end, item.end)
    return max_end


def _deleted_duration_before(point: float, delete_windows: List[List[float]]) -> float:
    deleted_duration = 0.0
    for delete_start, delete_end in delete_windows:
        if point <= delete_start:
            break
        deleted_duration += min(point, delete_end) - delete_start
        if point <= delete_end:
            break
    return max(0.0, deleted_duration)


def _inserted_duration_before(
    point: float,
    pause_adjustments: List[PauseAdjustment],
    *,
    include_at_point: bool = False,
) -> float:
    point_value = float(point)
    if include_at_point:
        return sum(
            item.duration
            for item in pause_adjustments
            if item.source_time <= point_value + 0.000001
        )
    return sum(
        item.duration for item in pause_adjustments if item.source_time < point_value - 0.000001
    )


def _map_source_time_to_timeline(
    point: float,
    delete_windows: List[List[float]],
    pause_adjustments: Optional[List[PauseAdjustment]] = None,
    *,
    include_pauses_at_point: bool = False,
) -> float:
    inserted = _inserted_duration_before(
        point,
        pause_adjustments or [],
        include_at_point=include_pauses_at_point,
    )
    return max(0.0, float(point) - _deleted_duration_before(point, delete_windows) + inserted)


def _detect_total_duration(
    request: RevisionRequest,
    *,
    mock_media: bool,
    video_material: Any,
) -> float:
    if mock_media:
        return _mock_duration_for_request(request)

    detected_duration = 0.0
    duration_us = int(getattr(video_material, "duration", 0) or 0)
    if duration_us > 0:
        detected_duration = duration_us / 1_000_000.0
    if detected_duration <= 0:
        detected_duration = get_duration_ffprobe_cached(request.project.source_video)

    if detected_duration > 0:
        return max(detected_duration, _max_request_end(request))
    return _mock_duration_for_request(request)


def _detect_material_duration_seconds(
    material: Any,
    *,
    fallback_path: str = "",
    fallback_default: float = 0.0,
) -> float:
    duration_us = int(getattr(material, "duration", 0) or 0)
    if duration_us > 0:
        return max(0.0, duration_us / 1_000_000.0)
    if fallback_path:
        detected = get_duration_ffprobe_cached(fallback_path)
        if detected > 0:
            return detected
    return max(0.0, fallback_default)


def _clamp_segment_duration(
    start: float, requested_duration: float, material_duration: float
) -> float:
    available_duration = max(0.0, float(material_duration) - float(start))
    return max(0.0, min(float(requested_duration), available_duration))


_AUDIO_MATERIAL_ROUNDING_TOLERANCE_US = 1_000


def _write_segmented_audio_delivery(
    project: Any,
    request: RevisionRequest,
    *,
    draft: Any,
    MockAudioMaterial: Any,
    mock_media: bool,
    fallback_duration: float,
) -> tuple[List[str], List[Dict[str, Any]]]:
    track_names: List[str] = []
    seen_track_names: set[str] = set()
    for plan_segment in request.audio_delivery_plan.segments:
        if plan_segment.track_name in seen_track_names:
            continue
        project.script.add_track(draft.TrackType.audio, plan_segment.track_name)
        seen_track_names.add(plan_segment.track_name)
        track_names.append(plan_segment.track_name)

    required_duration_by_path: Dict[str, float] = {}
    normalized_path_by_key: Dict[str, str] = {}
    for plan_segment in request.audio_delivery_plan.segments:
        normalized_path = os.path.abspath(plan_segment.asset_path)
        cache_key = os.path.normcase(normalized_path)
        normalized_path_by_key[cache_key] = normalized_path
        required_duration_by_path[cache_key] = max(
            required_duration_by_path.get(cache_key, 0.0),
            plan_segment.source_start + plan_segment.duration,
        )

    audio_materials: Dict[str, Any] = {}
    results: List[Dict[str, Any]] = []
    for plan_segment in request.audio_delivery_plan.segments:
        normalized_path = os.path.abspath(plan_segment.asset_path)
        cache_key = os.path.normcase(normalized_path)
        material = audio_materials.get(cache_key)
        if material is None:
            if mock_media:
                material_index = len(audio_materials) + 1
                material_duration = max(
                    fallback_duration,
                    required_duration_by_path[cache_key],
                )
                material = MockAudioMaterial(
                    f"mock-audio-delivery-{material_index}",
                    _seconds_to_us(material_duration),
                    f"audio-delivery-{material_index}",
                    normalized_path_by_key[cache_key],
                )
            else:
                material = draft.AudioMaterial(normalized_path_by_key[cache_key])
            audio_materials[cache_key] = material

            required_end_us = _seconds_to_us(required_duration_by_path[cache_key])
            parsed_duration_us = int(getattr(material, "duration", 0) or 0)
            if parsed_duration_us <= 0:
                if mock_media:
                    parsed_duration_us = required_end_us
                else:
                    parsed_duration_us = _seconds_to_us(
                        get_duration_ffprobe_cached(normalized_path_by_key[cache_key])
                    )
                if parsed_duration_us <= 0:
                    raise ValueError(
                        "Segmented audio delivery cannot resolve audio material duration: "
                        f"{normalized_path}."
                    )
                material.duration = parsed_duration_us

            if parsed_duration_us < required_end_us:
                duration_shortfall_us = required_end_us - parsed_duration_us
                if duration_shortfall_us > _AUDIO_MATERIAL_ROUNDING_TOLERANCE_US:
                    raise ValueError(
                        "Segmented audio delivery exceeds parsed audio material duration by "
                        f"{duration_shortfall_us / 1_000_000.0:.6f}s: required source end "
                        f"{required_end_us / 1_000_000.0:.6f}s, material duration "
                        f"{parsed_duration_us / 1_000_000.0:.6f}s ({normalized_path})."
                    )
                probed_duration_us = _seconds_to_us(
                    get_duration_ffprobe_cached(normalized_path_by_key[cache_key])
                )
                if probed_duration_us < required_end_us:
                    raise ValueError(
                        "Segmented audio delivery cannot confirm parsed audio duration "
                        f"rounding: required source end "
                        f"{required_end_us / 1_000_000.0:.6f}s, ffprobe duration "
                        f"{probed_duration_us / 1_000_000.0:.6f}s ({normalized_path})."
                    )
                material.duration = required_end_us

        source_start_us = _seconds_to_us(plan_segment.source_start)
        requested_duration_us = _seconds_to_us(plan_segment.duration)

        audio_segment = draft.AudioSegment(
            material,
            draft.Timerange(
                _seconds_to_us(plan_segment.timeline_start),
                requested_duration_us,
            ),
            source_timerange=draft.Timerange(
                source_start_us,
                requested_duration_us,
            ),
            volume=plan_segment.volume,
        )
        project.script.add_segment(audio_segment, plan_segment.track_name)
        if plan_segment.fade_in > 0 or plan_segment.fade_out > 0:
            project.add_audio_fade_to_segment(
                audio_segment,
                fade_in=_seconds_to_us(plan_segment.fade_in),
                fade_out=_seconds_to_us(plan_segment.fade_out),
            )

        results.append(
            {
                "plan_segment_id": plan_segment.segment_id,
                "role": plan_segment.role,
                "track_name": plan_segment.track_name,
                "asset_path": normalized_path,
                "material_id": str(getattr(material, "material_id", "")),
                "segment_id": str(getattr(audio_segment, "segment_id", "")),
                "source_start": plan_segment.source_start,
                "timeline_start": plan_segment.timeline_start,
                "duration": requested_duration_us / 1_000_000.0,
                "volume": plan_segment.volume,
                "fade_in": plan_segment.fade_in,
                "fade_out": plan_segment.fade_out,
                "doc_item_id": plan_segment.doc_item_id,
            }
        )

    return track_names, results


def _find_replacement_for_window(
    keep_start: float,
    keep_end: float,
    replacement_edits: List[RevisionEdit],
) -> Optional[RevisionEdit]:
    for edit in replacement_edits:
        if keep_start >= edit.start - 0.000001 and keep_end <= edit.end + 0.000001:
            return edit
    return None


def _float_from_mapping(payload: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(payload.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _explicit_nonnegative_finite_float(payload: Dict[str, Any], key: str) -> float:
    raw_value = payload[key]
    if isinstance(raw_value, bool):
        raise ValueError(f"{key} must be a finite non-negative number")
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a finite non-negative number") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{key} must be a finite non-negative number")
    return value


def _visual_segment_uses_video_asset(spec: Dict[str, Any], asset_path: str) -> bool:
    asset_type = str(spec.get("asset_type") or spec.get("media_type") or "").strip().lower()
    if asset_type in {"video", "source_video", "video_overlay"}:
        return True
    return os.path.splitext(asset_path)[1].lower() in _VISUAL_VIDEO_EXTENSIONS


def _explicit_visual_segment_volume(
    spec: Dict[str, Any],
    asset_path: str,
    *,
    item_id: str,
    segment_idx: int,
) -> Optional[float]:
    if "volume" not in spec or not _visual_segment_uses_video_asset(spec, asset_path):
        return None
    try:
        return _explicit_nonnegative_finite_float(spec, "volume")
    except ValueError as exc:
        raise ValueError(f"Visual edit {item_id} segment {segment_idx}: {exc}") from exc


def _validate_visual_overlay_volumes(request: RevisionRequest) -> None:
    for idx, edit in enumerate(request.edits):
        if not _is_visual_edit(edit):
            continue
        item_id = _edit_review_id(edit, idx)
        for segment_idx, spec in enumerate(_visual_plan_segments(edit), start=1):
            asset_path = str(spec.get("asset_path") or "").strip()
            _explicit_visual_segment_volume(
                spec,
                asset_path,
                item_id=item_id,
                segment_idx=segment_idx,
            )


def _normalized_visual_keyframes(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_keyframes = spec.get("keyframes")
    if not isinstance(raw_keyframes, list):
        return []
    keyframes: List[Dict[str, Any]] = []
    for item in raw_keyframes:
        if not isinstance(item, dict):
            continue
        prop = str(item.get("property") or item.get("property_type") or "").strip()
        if not prop:
            continue
        if "value" not in item:
            continue
        offset = item.get("offset", item.get("time_offset", 0))
        keyframes.append(
            {
                "property": prop,
                "offset": offset,
                "value": float(item["value"]),
            }
        )
    return keyframes


def _add_visual_overlay_segments(
    project: Any,
    request: RevisionRequest,
    delete_windows: List[List[float]],
    *,
    mock_media: bool = False,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for idx, edit in enumerate(request.edits):
        if not _is_visual_edit(edit):
            continue

        item_id = _edit_review_id(edit, idx)
        segment_specs = _visual_plan_segments(edit)
        if not segment_specs:
            raise RuntimeError(
                f"Visual edit {item_id} has no executable asset_paths or visual_plan.segments."
            )

        segment_results: List[Dict[str, Any]] = []
        for segment_idx, spec in enumerate(segment_specs, start=1):
            asset_path = str(spec.get("asset_path") or "").strip()
            if not asset_path:
                raise RuntimeError(
                    f"Visual edit {item_id} segment {segment_idx} is missing asset_path."
                )
            if not mock_media and not os.path.exists(asset_path):
                raise FileNotFoundError(asset_path)
            if mock_media and not os.path.exists(asset_path):
                raise FileNotFoundError(asset_path)

            source_start = _float_from_mapping(spec, "source_start", edit.start)
            timeline_start = spec.get("timeline_start")
            if timeline_start is None:
                timeline_start = _map_source_time_to_timeline(
                    source_start,
                    delete_windows,
                    request.pause_adjustments,
                )
            timeline_start = float(timeline_start)
            duration = _float_from_mapping(spec, "duration", max(0.8, edit.end - edit.start))
            duration = max(0.2, duration)
            track_name = _clean_track_name(
                spec.get("track_name") or "",
                item_id=item_id,
                fallback_idx=idx,
                role=spec.get("role") or "visual_overlay",
            )
            uses_video_asset = _visual_segment_uses_video_asset(spec, asset_path)
            explicit_volume = _explicit_visual_segment_volume(
                spec,
                asset_path,
                item_id=item_id,
                segment_idx=segment_idx,
            )
            if uses_video_asset:
                segment = project.add_media_safe(
                    asset_path,
                    start_time=f"{timeline_start:.3f}s",
                    duration=f"{duration:.3f}s",
                    track_name=track_name,
                    source_start=f"{_float_from_mapping(spec, 'asset_source_start', 0.0):.3f}s",
                )
            else:
                segment = project.add_image_simple(
                    asset_path,
                    start_time=f"{timeline_start:.3f}s",
                    duration=f"{duration:.3f}s",
                    track_name=track_name,
                    scale_x=_float_from_mapping(spec, "scale_x", 1.0),
                    scale_y=_float_from_mapping(spec, "scale_y", 1.0),
                    transform_x=_float_from_mapping(spec, "transform_x", 0.0),
                    transform_y=_float_from_mapping(spec, "transform_y", 0.0),
                    rotation=_float_from_mapping(spec, "rotation", 0.0),
                    alpha=_float_from_mapping(spec, "alpha", 1.0),
                )
            if segment is None:
                raise RuntimeError(
                    f"Visual edit {item_id} failed to create overlay segment for {asset_path}."
                )
            if explicit_volume is not None:
                segment.volume = explicit_volume
            keyframes = _normalized_visual_keyframes(spec)
            if keyframes and not uses_video_asset:
                project.add_segment_keyframes(segment, keyframes)
            segment_results.append(
                {
                    "role": str(spec.get("role") or "visual_overlay"),
                    "asset_path": asset_path,
                    "asset_type": "video" if uses_video_asset else "image",
                    "track_name": track_name,
                    "segment_id": getattr(segment, "segment_id", ""),
                    "material_id": getattr(segment, "material_id", ""),
                    "source_start": source_start,
                    "timeline_start": timeline_start,
                    "duration": duration,
                    "scale_x": _float_from_mapping(spec, "scale_x", 1.0),
                    "scale_y": _float_from_mapping(spec, "scale_y", 1.0),
                    "transform_x": _float_from_mapping(spec, "transform_x", 0.0),
                    "transform_y": _float_from_mapping(spec, "transform_y", 0.0),
                    "volume": float(getattr(segment, "volume", 1.0)),
                    "keyframes": keyframes,
                }
            )

        first_segment = segment_results[0]
        results.append(
            {
                "item_id": item_id,
                "kind": _visual_kind_for_edit(edit),
                "segments": segment_results,
                "evidence": {
                    "status": "pass",
                    "executed": True,
                    "operation": _visual_kind_for_edit(edit),
                    "edit_type": edit.op_type,
                    "asset_paths": [segment["asset_path"] for segment in segment_results],
                    "track_names": [segment["track_name"] for segment in segment_results],
                    "segment_ids": [segment["segment_id"] for segment in segment_results],
                    "material_ids": [segment["material_id"] for segment in segment_results],
                    "overlay_track": first_segment["track_name"],
                    "overlay_segment": first_segment["segment_id"],
                    "overlay_segments": segment_results,
                    "validation": {
                        "status": "pass",
                        "method": "editable_overlay_segment_written",
                    },
                },
                "validation": {
                    "status": "pass",
                    "method": "editable_overlay_segment_written",
                },
            }
        )
    return results


def _merge_visual_results_into_items(
    items: List[RevisionReviewItem],
    visual_results: List[Dict[str, Any]],
) -> List[RevisionReviewItem]:
    if not visual_results:
        return list(items)
    by_id = {
        _normalize_review_id(str(result.get("item_id") or "")): result
        for result in visual_results
        if result.get("item_id")
    }
    updated: List[RevisionReviewItem] = []
    for item in items:
        result = by_id.get(_normalize_review_id(item.item_id))
        if not result:
            updated.append(item)
            continue
        evidence = dict(item.evidence or {})
        evidence.update(result.get("evidence") or {})
        validation = dict(item.validation or {})
        validation.update(result.get("validation") or {})
        updated.append(
            RevisionReviewItem(
                item_id=item.item_id,
                kind=item.kind,
                source_text=item.source_text,
                source=item.source,
                start=item.start,
                end=item.end,
                execution_required=item.execution_required,
                evidence=evidence,
                validation=validation,
                verbatim_status=item.verbatim_status,
                execution_status=item.execution_status,
            )
        )
    return updated


def _request_with_visual_results(
    request: RevisionRequest,
    visual_results: List[Dict[str, Any]],
) -> RevisionRequest:
    if not visual_results:
        return request
    return replace(
        request,
        review_items=_merge_visual_results_into_items(request.review_items, visual_results),
    )


def _merge_pause_results_into_items(
    items: List[RevisionReviewItem],
    pause_results: List[Dict[str, Any]],
) -> List[RevisionReviewItem]:
    if not pause_results:
        return list(items)
    by_id = {
        _normalize_review_id(str(result.get("item_id") or "")): result
        for result in pause_results
        if result.get("item_id")
    }
    updated: List[RevisionReviewItem] = []
    for item in items:
        result = by_id.get(_normalize_review_id(item.item_id))
        if not result:
            updated.append(item)
            continue
        evidence = dict(item.evidence or {})
        evidence["semantic_pause_adjustment"] = dict(result)
        validation = dict(item.validation or {})
        validation["pause_status"] = "pass"
        validation["pause_method"] = "editable_no_audio_still_frame_hold"
        updated.append(
            RevisionReviewItem(
                item_id=item.item_id,
                kind=item.kind,
                source_text=item.source_text,
                source=item.source,
                start=item.start,
                end=item.end,
                execution_required=item.execution_required,
                evidence=evidence,
                validation=validation,
                verbatim_status=item.verbatim_status,
                execution_status=item.execution_status,
            )
        )
    return updated


def _request_with_pause_results(
    request: RevisionRequest,
    pause_results: List[Dict[str, Any]],
) -> RevisionRequest:
    if not pause_results:
        return request
    return replace(
        request,
        review_items=_merge_pause_results_into_items(request.review_items, pause_results),
    )


def _build_review_marker_item(
    ReviewMarkerItem,
    *,
    label: str,
    start: float,
    end: float,
    detail: str,
    item_id: str = "",
    source_text: str = "",
    verbatim_status: str = "legacy",
    kind: str = "review_only",
    execution_status: str = "",
):
    return ReviewMarkerItem(
        label=label,
        start_time=f"{start:.3f}s",
        duration=f"{max(0.8, end - start):.3f}s",
        detail=detail,
        item_id=item_id,
        source_text=source_text,
        verbatim_status=verbatim_status,
        kind=kind,
        execution_status=execution_status,
    )


def _should_fallback_new_draft(exc: Exception) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, RuntimeError) and (
        "overwrite is blocked until JianYing returns to the home/drafts page" in str(exc)
    ):
        return True
    return False


def _build_fallback_draft_name(requested_name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    token = uuid.uuid4().hex[:6]
    return f"{requested_name}__fallback_{stamp}_{token}"


def _open_revision_project(
    JyProject,
    requested_name: str,
    *,
    drafts_root: str = None,
):
    try:
        project = JyProject(
            requested_name,
            drafts_root=drafts_root,
            overwrite=True,
            ui_mode="offline",
        )
        return project, {
            "draft_name": requested_name,
            "requested_draft_name": requested_name,
            "cleanup_on_failure": True,
        }
    except Exception as exc:
        if not _should_fallback_new_draft(exc):
            raise

        fallback_name = _build_fallback_draft_name(requested_name)
        try:
            project = JyProject(
                fallback_name,
                drafts_root=drafts_root,
                overwrite=True,
                ui_mode="offline",
            )
        except Exception as fallback_exc:
            raise RuntimeError(
                "Requested draft could not be safely overwritten, and fallback draft creation failed: "
                f"{fallback_exc}"
            ) from fallback_exc

        return project, {
            "draft_name": fallback_name,
            "requested_draft_name": requested_name,
            "write_mode": "fallback_new_draft",
            "fallback_reason": str(exc),
            "cleanup_on_failure": True,
        }


def _run_project_retention(project: Any) -> Dict[str, Any]:
    runner = getattr(project, "_run_draft_retention", None)
    if not callable(runner):
        return {"enabled": False, "reason": "retention_unavailable"}
    return runner()


def _cleanup_incomplete_draft(project: Any) -> None:
    draft_dir = getattr(project, "draft_dir", "") or ""
    if not draft_dir:
        return
    if os.path.isdir(draft_dir):
        try:
            project._safe_remove_dir(draft_dir)  # type: ignore[attr-defined]
        except Exception:
            pass


def _snapshot_live_project_state(project: Any) -> str:
    script = getattr(project, "script", None)
    dumps = getattr(script, "dumps", None)
    if not callable(dumps):
        raise ValueError("Automatic repair requires a snapshot-capable live project.")
    return str(dumps())


def _validate_revision_execution_preflight(
    request: RevisionRequest,
    doc_items: Optional[List[RevisionReviewItem]],
) -> None:
    if request.workflow_mode == "lite":
        request = replace(
            request,
            edits=[
                edit
                for edit in request.edits
                if not lite_pause_change_is_label_only(
                    edit.source_kind or edit.op_type,
                    " ".join(str(value or "") for value in (edit.label, edit.detail)),
                )
            ],
            pause_adjustments=[],
        )
    profile = derive_acceptance_profile(request, doc_items=doc_items)
    source_spoken_edit = any(
        record.get("has_review_item")
        and record.get("execution_required")
        and {"audio_precision", "audio_join"}.intersection(record.get("gates") or [])
        for record in profile["items"]
    )
    semantic_pause_edit = validate_semantic_pause_pairing(request)
    if semantic_pause_edit and not request.pause_alignment:
        raise ValueError(
            "Semantic pause adjustments require pause_alignment with hash-bound source ASR "
            "before opening or writing a JianYing draft."
        )
    if semantic_pause_edit and request.audio_delivery_plan.mode != "segmented":
        raise ValueError(
            "Semantic pause adjustments require segmented audio delivery compiled after "
            "pause alignment before opening or writing a JianYing draft."
        )
    if source_spoken_edit and request.audio_delivery_plan.mode != "segmented":
        raise ValueError(
            "Spoken source-ledger revisions require segmented audio delivery before "
            "opening or writing a JianYing draft."
        )
    if request.audio_delivery_plan.mode == "segmented" and (
        request.audio_delivery_plan.pending or not request.audio_delivery_plan.segments
    ):
        raise ValueError(
            "Segmented audio delivery is pending; populate explicit source/repair "
            "segments before opening or writing a JianYing draft."
        )
    if request.audio_delivery_plan.mode == "segmented" and request.pause_alignment:
        stale_segment_ids: set[str] = set()
        delete_windows = _collect_delete_windows(request)
        mapping_delete_windows = [] if request.workflow_mode == "lite" else delete_windows
        pause_offset_by_source: Dict[float, float] = {}
        for pause in request.pause_adjustments:
            evidence = pause.boundary_evidence or {}
            if evidence.get("status") != "pass":
                raise ValueError(
                    f"Pause {pause.item_id or '<unidentified>'} is missing resolved "
                    "ASR boundary evidence."
                )
            pause_timeline_start = _map_source_time_to_timeline(
                pause.source_time,
                mapping_delete_windows,
                request.pause_adjustments,
            )
            pause_key = round(pause.source_time, 6)
            pause_timeline_start += pause_offset_by_source.get(pause_key, 0.0)
            pause_timeline_end = pause_timeline_start + pause.duration
            pause_offset_by_source[pause_key] = pause_timeline_end - _map_source_time_to_timeline(
                pause.source_time,
                mapping_delete_windows,
                request.pause_adjustments,
            )
            for segment in request.audio_delivery_plan.segments:
                segment_source_end = segment.source_start + segment.duration
                if (
                    segment.role in {"source", "reference"}
                    and segment.source_start < pause.source_time - 1e-6
                    and segment_source_end > pause.source_time + 1e-6
                ):
                    stale_segment_ids.add(segment.segment_id)
                if segment.role in {"source", "reference"}:
                    expected_timeline_start = _map_source_time_to_timeline(
                        segment.source_start,
                        mapping_delete_windows,
                        request.pause_adjustments,
                        include_pauses_at_point=True,
                    )
                    if abs(segment.timeline_start - expected_timeline_start) > 1e-3:
                        stale_segment_ids.add(segment.segment_id)
                    if any(
                        min(segment_source_end, delete_end)
                        - max(segment.source_start, delete_start)
                        > 1e-6
                        for delete_start, delete_end in delete_windows
                    ) and not (
                        request.workflow_mode == "lite"
                        and segment.track_name == "Lite Reused Audio"
                    ):
                        stale_segment_ids.add(segment.segment_id)
                segment_timeline_end = segment.timeline_start + segment.duration
                if (
                    segment.volume > 0
                    and min(segment_timeline_end, pause_timeline_end)
                    - max(segment.timeline_start, pause_timeline_start)
                    > 1e-6
                ):
                    stale_segment_ids.add(segment.segment_id)
        if stale_segment_ids:
            raise ValueError(
                "Aligned semantic pauses changed source/timeline boundaries; recompile "
                "audio_delivery_plan after pause alignment. Stale segments: "
                + ", ".join(sorted(stale_segment_ids))
                + "."
            )
        if request.pause_adjustments:
            declared_media_duration = request.project.media_duration_seconds
            detected_media_duration = (
                get_duration_ffprobe_cached(request.project.source_video)
                if os.path.isfile(request.project.source_video)
                else 0.0
            )
            if (
                declared_media_duration > 0
                and detected_media_duration > 0
                and abs(declared_media_duration - detected_media_duration) > 0.1
            ):
                raise ValueError(
                    "project.media_duration_seconds does not match the current source video "
                    f"duration: declared {declared_media_duration:.3f}s, "
                    f"detected {detected_media_duration:.3f}s."
                )
            media_duration = detected_media_duration or declared_media_duration
            if media_duration <= 0:
                raise ValueError(
                    "Semantic pause segmented audio requires project.media_duration_seconds "
                    "to prove complete source/reference coverage."
                )
            expected_windows = _build_keep_windows(request, media_duration)
            coverage_errors: List[str] = []
            reference_segments = sorted(
                (
                    segment
                    for segment in request.audio_delivery_plan.segments
                    if segment.role == "reference"
                ),
                key=lambda segment: (segment.source_start, segment.segment_id),
            )
            if request.workflow_mode != "lite" and (
                len({segment.track_name for segment in reference_segments}) != 1
                or len(reference_segments) != len(expected_windows)
                or any(
                    abs(segment.source_start - window_start) > 1e-3
                    or abs(segment.duration - (window_end - window_start)) > 0.05
                    for segment, (window_start, window_end) in zip(
                        reference_segments,
                        expected_windows,
                    )
                )
            ):
                coverage_errors.append("reference")

            expected_audible_intervals = [
                (
                    _map_source_time_to_timeline(
                        window_start,
                        mapping_delete_windows,
                        request.pause_adjustments,
                        include_pauses_at_point=True,
                    ),
                    _map_source_time_to_timeline(
                        window_start,
                        mapping_delete_windows,
                        request.pause_adjustments,
                        include_pauses_at_point=True,
                    )
                    + (window_end - window_start),
                )
                for window_start, window_end in expected_windows
            ]
            audible_intervals = sorted(
                (
                    segment.timeline_start,
                    segment.timeline_start + segment.duration,
                )
                for segment in request.audio_delivery_plan.segments
                if segment.role in {"source", "replacement_video", "repair"} and segment.volume > 0
            )

            def merge_coverage_intervals(
                intervals: List[tuple[float, float]],
            ) -> tuple[List[tuple[float, float]], bool]:
                merged: List[tuple[float, float]] = []
                overlap = False
                for start, end in intervals:
                    if not merged or start > merged[-1][1] + 1e-3:
                        merged.append((start, end))
                        continue
                    if start < merged[-1][1] - 1e-3:
                        overlap = True
                    previous_start, previous_end = merged[-1]
                    merged[-1] = (previous_start, max(previous_end, end))
                return merged, overlap

            merged_expected, _expected_overlap = merge_coverage_intervals(
                expected_audible_intervals
            )
            merged_audible, audible_overlap = merge_coverage_intervals(audible_intervals)
            if (
                audible_overlap
                or len(merged_audible) != len(merged_expected)
                or any(
                    abs(actual_start - expected_start) > 0.05
                    or abs(actual_end - expected_end) > 0.05
                    for (actual_start, actual_end), (expected_start, expected_end) in zip(
                        merged_audible,
                        merged_expected,
                    )
                )
            ):
                coverage_errors.append("audible")
            if coverage_errors:
                raise ValueError(
                    "Semantic pause segmented audio lacks complete source/reference coverage "
                    "after pause alignment: " + ", ".join(sorted(set(coverage_errors))) + "."
                )
    if request.audio_delivery_plan.mode == "segmented":
        candidate_path = _processed_audio_candidate_path(request)
        if candidate_path:
            candidate_hash_cache: Dict[str, str] = {}
            conflicting_ids = [
                segment.segment_id
                for segment in request.audio_delivery_plan.segments
                if _audio_files_share_identity(
                    candidate_path,
                    segment.asset_path,
                    hash_cache=candidate_hash_cache,
                )
            ]
            if conflicting_ids:
                raise ValueError(
                    "The full processed-audio candidate is validation-only and cannot be "
                    "imported as segmented draft audio: " + ", ".join(conflicting_ids) + "."
                )


def _validate_saved_revision_variants(
    request: RevisionRequest,
    save_result: Dict[str, Any],
    *,
    draft_name: str,
    doc_items: Optional[List[RevisionReviewItem]],
    marker_receipts: List[Dict[str, Any]],
    marker_plan: List[Any],
    strict: bool = False,
) -> tuple[List[tuple[str, Dict[str, Any]]], Dict[str, Any], Dict[str, Any]]:
    content_variants = _load_saved_draft_content_variants(save_result)
    primary_variant_name, content = content_variants[0]
    validation = validate_saved_revision_draft(
        request,
        content,
        draft_name=draft_name,
        doc_items=doc_items,
        marker_receipts=marker_receipts,
        marker_plan=marker_plan,
        strict=strict,
    )
    marker_variant_metrics = {
        primary_variant_name: validation.get("metrics", {}).get("marker_validation", {})
    }
    saved_variant_metrics = {primary_variant_name: dict(validation.get("metrics", {}))}
    validation.setdefault("warnings", [])
    validation.setdefault("metrics", {})
    for variant_name, variant_content in content_variants[1:]:
        variant_validation = validate_saved_revision_draft(
            request,
            variant_content,
            draft_name=draft_name,
            doc_items=doc_items,
            marker_receipts=marker_receipts,
            marker_plan=marker_plan,
            strict=strict,
        )
        saved_variant_metrics[variant_name] = variant_validation.get("metrics", {})
        marker_variant_metrics[variant_name] = variant_validation.get("metrics", {}).get(
            "marker_validation", {}
        )
        validation["errors"].extend(
            f"[{variant_name}] {message}" for message in variant_validation["errors"]
        )
        validation["warnings"].extend(
            f"[{variant_name}] {message}" for message in variant_validation["warnings"]
        )
    validation["ok"] = not validation["errors"]
    validation["metrics"]["variant_name"] = primary_variant_name
    validation["metrics"]["validated_marker_variants"] = [
        variant_name for variant_name, _content in content_variants
    ]
    validation["metrics"]["marker_validation_variants"] = marker_variant_metrics
    validation["metrics"]["saved_draft_validation_variants"] = saved_variant_metrics
    return content_variants, content, validation


def execute_revision_request(
    request: RevisionRequest,
    *,
    drafts_root: str = None,
    mock_media: bool = False,
    strict: bool = False,
    doc_items: Optional[List[RevisionReviewItem]] = None,
    acceptance_repair_callback: Optional[
        Callable[[Any, RevisionRequest, Dict[str, Any]], RevisionRequest]
    ] = None,
    localize_materials: bool = False,
    runtime_integrity_receipt: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if request.workflow_mode == "lite":
        runtime_integrity = (
            dict(runtime_integrity_receipt)
            if isinstance(runtime_integrity_receipt, Mapping)
            else None
        )
        if not mock_media and runtime_integrity is None:
            from utils.runtime_integrity import validate_current_lite_runtime

            runtime_integrity = validate_current_lite_runtime()
        from utils.lite_revision import execute_lite_revision_request

        result = execute_lite_revision_request(
            request,
            drafts_root=drafts_root,
            mock_media=mock_media,
            strict=strict,
            doc_items=doc_items,
            acceptance_repair_callback=acceptance_repair_callback,
            localize_materials=localize_materials,
        )
        if runtime_integrity is not None:
            result["runtime_integrity"] = runtime_integrity
        return result

    if localize_materials:
        raise ValueError("Material localization is only available for workflow_mode=lite.")

    request = normalize_pause_adjustments(request)
    ui_policy = _derive_revision_ui_policy(request, doc_items=doc_items)
    _validate_revision_execution_preflight(request, doc_items)
    _validate_visual_overlay_volumes(request)
    draft, ReviewMarkerItem, MockAudioMaterial, MockVideoMaterial, JyProject = (
        _import_runtime_components()
    )

    project, write_info = _open_revision_project(
        JyProject,
        request.project.draft_name,
        drafts_root=drafts_root,
    )
    created_new_requested_draft = (
        write_info.get("write_mode", "requested_draft") == "requested_draft"
    )
    validated_editable_draft = False
    try:
        segmented_audio_delivery = request.audio_delivery_plan.mode == "segmented"
        video_track_name = "Original Video"
        source_audio_track_name = "Separated Source Audio"
        replacement_audio_track_name = "Replacement Audio"

        project.script.add_track(draft.TrackType.video, video_track_name)
        if not segmented_audio_delivery:
            project.script.add_track(draft.TrackType.audio, source_audio_track_name)
            project.script.add_track(draft.TrackType.audio, replacement_audio_track_name)

        if mock_media:
            duration_us = _seconds_to_us(_mock_duration_for_request(request))
            video_material = MockVideoMaterial(
                "mock-video",
                duration_us,
                "source-video",
                request.project.source_video,
            )
        else:
            video_material = draft.VideoMaterial(request.project.source_video)

        source_audio_material = None
        if not segmented_audio_delivery:
            if mock_media:
                source_audio_material = MockAudioMaterial(
                    "mock-source-audio",
                    duration_us,
                    "source-audio",
                    request.project.source_audio or request.project.source_video,
                )
            else:
                source_audio_material = draft.AudioMaterial(
                    request.project.source_audio or request.project.source_video
                )

        replacement_edits = [] if segmented_audio_delivery else _collect_replacement_edits(request)
        full_track_replacement_audio = (
            False
            if segmented_audio_delivery
            else _request_uses_full_track_replacement_audio(request)
        )
        total_duration = _detect_total_duration(
            request,
            mock_media=mock_media,
            video_material=video_material,
        )
        duration_us = _seconds_to_us(total_duration)
        source_audio_duration = 0.0
        if source_audio_material is not None:
            source_audio_duration = _detect_material_duration_seconds(
                source_audio_material,
                fallback_path=request.project.source_audio or request.project.source_video,
                fallback_default=total_duration,
            )

        replacement_audio_materials: Dict[str, Any] = {}
        replacement_audio_durations: Dict[str, float] = {}
        if not segmented_audio_delivery:
            replacement_paths = _replacement_audio_paths_for_request(request)
            for idx, replacement_path in enumerate(replacement_paths, start=1):
                if replacement_path in replacement_audio_materials:
                    continue
                if mock_media:
                    replacement_audio_materials[replacement_path] = MockAudioMaterial(
                        f"mock-replacement-audio-{idx}",
                        duration_us,
                        f"replacement-audio-{idx}",
                        replacement_path,
                    )
                else:
                    replacement_audio_materials[replacement_path] = draft.AudioMaterial(
                        replacement_path
                    )
                replacement_audio_durations[replacement_path] = _detect_material_duration_seconds(
                    replacement_audio_materials[replacement_path],
                    fallback_path=replacement_path,
                    fallback_default=total_duration,
                )

        delete_windows = _collect_delete_windows(request)
        keep_windows = _build_keep_windows(request, total_duration)
        pauses_by_source: Dict[float, List[PauseAdjustment]] = {}
        for adjustment in request.pause_adjustments:
            if not mock_media and not os.path.exists(adjustment.frame_path):
                raise FileNotFoundError(adjustment.frame_path)
            pauses_by_source.setdefault(round(adjustment.source_time, 6), []).append(adjustment)

        timeline_cursor = 0.0
        pause_results: List[Dict[str, Any]] = []
        for keep_start, keep_end in keep_windows:
            for adjustment in pauses_by_source.get(round(keep_start, 6), []):
                hold_segment = project.add_image_simple(
                    adjustment.frame_path,
                    start_time=f"{timeline_cursor:.6f}s",
                    duration=f"{adjustment.duration:.6f}s",
                    track_name=video_track_name,
                )
                pause_results.append(
                    {
                        "item_id": adjustment.item_id,
                        "requested_source_time": adjustment.requested_source_time,
                        "source_time": adjustment.source_time,
                        "frame_source_time": adjustment.frame_source_time,
                        "timeline_start": round(timeline_cursor, 6),
                        "timeline_end": round(timeline_cursor + adjustment.duration, 6),
                        "duration": adjustment.duration,
                        "frame_path": adjustment.frame_path,
                        "frame_sha256": adjustment.frame_sha256,
                        "reason": adjustment.reason,
                        "boundary_evidence": dict(adjustment.boundary_evidence),
                        "segment_id": str(
                            getattr(hold_segment, "segment_id", "")
                            or getattr(hold_segment, "id", "")
                        ),
                        "track_name": video_track_name,
                    }
                )
                timeline_cursor += adjustment.duration
            keep_duration = max(0.0, keep_end - keep_start)
            if keep_duration <= 0:
                continue
            video_segment = draft.VideoSegment(
                video_material,
                draft.Timerange(_seconds_to_us(timeline_cursor), _seconds_to_us(keep_duration)),
                source_timerange=draft.Timerange(
                    _seconds_to_us(keep_start), _seconds_to_us(keep_duration)
                ),
                volume=0.0,
            )
            project.script.add_segment(video_segment, video_track_name)

            if not segmented_audio_delivery:
                matched_replacement = _find_replacement_for_window(
                    keep_start,
                    keep_end,
                    replacement_edits,
                )

                source_audio_duration_window = _clamp_segment_duration(
                    keep_start,
                    keep_duration,
                    source_audio_duration,
                )
                if source_audio_duration_window > 0:
                    source_audio_segment = draft.AudioSegment(
                        source_audio_material,
                        draft.Timerange(
                            _seconds_to_us(timeline_cursor),
                            _seconds_to_us(source_audio_duration_window),
                        ),
                        source_timerange=draft.Timerange(
                            _seconds_to_us(keep_start),
                            _seconds_to_us(source_audio_duration_window),
                        ),
                        volume=(
                            0.0 if (matched_replacement or full_track_replacement_audio) else 1.0
                        ),
                    )
                    project.script.add_segment(source_audio_segment, source_audio_track_name)
                    project.add_audio_fade_to_segment(
                        source_audio_segment,
                        fade_in="0.012s",
                        fade_out="0.012s",
                    )

                if matched_replacement:
                    replacement_path = (
                        matched_replacement.audio_path or request.project.replacement_audio
                    )
                    replacement_audio_material = replacement_audio_materials[replacement_path]
                    replacement_source_start = max(0.0, keep_start - matched_replacement.start)
                    replacement_duration_window = _clamp_segment_duration(
                        replacement_source_start,
                        keep_duration,
                        replacement_audio_durations[replacement_path],
                    )
                    if replacement_duration_window <= 0:
                        timeline_cursor += keep_duration
                        continue
                    replacement_segment = draft.AudioSegment(
                        replacement_audio_material,
                        draft.Timerange(
                            _seconds_to_us(timeline_cursor),
                            _seconds_to_us(replacement_duration_window),
                        ),
                        source_timerange=draft.Timerange(
                            _seconds_to_us(replacement_source_start),
                            _seconds_to_us(replacement_duration_window),
                        ),
                        volume=1.0,
                    )
                    project.script.add_segment(
                        replacement_segment,
                        replacement_audio_track_name,
                    )
                    project.add_audio_fade_to_segment(
                        replacement_segment,
                        fade_in="0.012s",
                        fade_out="0.018s",
                    )

            timeline_cursor += keep_duration

        audio_delivery_track_names: List[str] = []
        audio_delivery_results: List[Dict[str, Any]] = []
        if segmented_audio_delivery:
            audio_delivery_track_names, audio_delivery_results = _write_segmented_audio_delivery(
                project,
                request,
                draft=draft,
                MockAudioMaterial=MockAudioMaterial,
                mock_media=mock_media,
                fallback_duration=total_duration,
            )

        if full_track_replacement_audio:
            replacement_path = request.project.replacement_audio
            replacement_audio_material = replacement_audio_materials[replacement_path]
            replacement_duration = replacement_audio_durations[replacement_path]
            timeline_duration = max(0.0, timeline_cursor)
            replacement_duration_window = min(
                replacement_duration,
                timeline_duration if timeline_duration > 0 else replacement_duration,
            )
            if replacement_duration_window <= 0:
                raise RuntimeError("Full-track replacement audio has zero usable duration.")
            replacement_segment = draft.AudioSegment(
                replacement_audio_material,
                draft.Timerange(0, _seconds_to_us(replacement_duration_window)),
                source_timerange=draft.Timerange(0, _seconds_to_us(replacement_duration_window)),
                volume=1.0,
            )
            project.script.add_segment(replacement_segment, replacement_audio_track_name)
            project.add_audio_fade_to_segment(
                replacement_segment,
                fade_in="0.012s",
                fade_out="0.018s",
            )

        visual_results = _add_visual_overlay_segments(
            project,
            request,
            delete_windows,
            mock_media=mock_media,
        )
        validation_request = _request_with_visual_results(request, visual_results)
        validation_request = _request_with_pause_results(validation_request, pause_results)
        validation_doc_items = (
            _merge_visual_results_into_items(doc_items, visual_results)
            if doc_items is not None
            else None
        )
        if validation_doc_items is not None:
            validation_doc_items = _merge_pause_results_into_items(
                validation_doc_items,
                pause_results,
            )
        latest_ledger_request = validation_request
        ui_policy = _derive_revision_ui_policy(
            latest_ledger_request,
            doc_items=validation_doc_items,
        )

        marker_plan = map_marker_plan_to_timeline(
            build_marker_plan(
                latest_ledger_request,
                doc_items=validation_doc_items,
            ),
            latest_ledger_request,
        )
        review_markers = [
            _build_review_marker_item(
                ReviewMarkerItem,
                label=item.source_text,
                start=item.start,
                end=item.end,
                detail=item.source,
                item_id=item.item_id,
                source_text=item.source_text,
                verbatim_status=item.verbatim_status,
                kind=item.kind,
                execution_status=item.execution_status,
            )
            for item in marker_plan
        ]
        assigned_markers = project.add_review_markers(review_markers)
        review_marker_receipts = [
            {
                "item_id": item.item_id,
                "source_text": item.source_text,
                "verbatim_status": item.verbatim_status,
                "execution_status": item.execution_status,
                "segment_id": item.segment_id,
                "material_id": item.material_id,
                "track_name": item.track_name,
                "start_time": item.start_time,
                "duration": item.duration,
            }
            for item in assigned_markers
        ]

        save_result = project.save(auto_retain=False)
        content_variants, content, validation = _validate_saved_revision_variants(
            latest_ledger_request,
            save_result,
            draft_name=write_info["draft_name"],
            doc_items=validation_doc_items,
            marker_receipts=review_marker_receipts,
            marker_plan=marker_plan,
            strict=strict,
        )
        if not validation["ok"]:
            message = "; ".join(validation["errors"])
            raise RuntimeError(f"Editable revision draft validation failed: {message}")
        validated_editable_draft = True
        acceptance_validation = validate_revision_acceptance_variants(
            latest_ledger_request,
            content_variants,
            draft_name=write_info["draft_name"],
            doc_items=validation_doc_items,
            strict=strict,
        )
        acceptance_repair: Optional[Dict[str, Any]] = None
        if not acceptance_validation["ok"] and acceptance_repair_callback is not None:
            pre_repair_snapshot = _snapshot_saved_draft_files(save_result)
            pre_repair_project_state = _snapshot_live_project_state(project)
            repaired_state: Dict[str, Any] = {}

            def apply_repair(
                current_request: RevisionRequest, plan: Dict[str, Any]
            ) -> RevisionRequest:
                repaired_request = acceptance_repair_callback(
                    project,
                    current_request,
                    plan,
                )
                if _snapshot_live_project_state(project) != pre_repair_project_state:
                    raise ValueError(
                        "acceptance_repair_callback must not mutate the live project; "
                        "return a scoped RevisionRequest instead."
                    )
                if _snapshot_saved_draft_files(save_result) != pre_repair_snapshot:
                    raise ValueError(
                        "acceptance_repair_callback must not write saved draft files directly."
                    )
                return repaired_request

            def prepare_repair(
                repaired_request: RevisionRequest, _plan: Dict[str, Any]
            ) -> RevisionRequest:
                prepared_request = normalize_pause_adjustments(repaired_request)
                _validate_revision_execution_preflight(
                    prepared_request,
                    validation_doc_items,
                )
                return prepared_request

            def revalidate_repair(
                repaired_request: RevisionRequest, _plan: Dict[str, Any]
            ) -> Dict[str, Any]:
                repaired_state["request"] = repaired_request
                try:
                    repaired_save_result = project.save(auto_retain=False)
                    (
                        repaired_variants,
                        repaired_content,
                        repaired_structure,
                    ) = _validate_saved_revision_variants(
                        repaired_request,
                        repaired_save_result,
                        draft_name=write_info["draft_name"],
                        doc_items=validation_doc_items,
                        marker_receipts=review_marker_receipts,
                        marker_plan=marker_plan,
                        strict=strict,
                    )
                except Exception:
                    _restore_saved_draft_files(pre_repair_snapshot)
                    raise
                if not repaired_structure["ok"]:
                    _restore_saved_draft_files(pre_repair_snapshot)
                    repaired_state["restored_pre_repair_draft"] = True
                    repair_profile = derive_acceptance_profile(
                        repaired_request, doc_items=validation_doc_items
                    )
                    return {
                        "ok": False,
                        "errors": list(repaired_structure["errors"]),
                        "failures": [
                            {
                                "gate": "editable_structure",
                                "item_id": "",
                                "status": "fail",
                                "repairable": False,
                                "reason": reason,
                            }
                            for reason in repaired_structure["errors"]
                        ],
                        "metrics": {
                            "enabled_gates": repair_profile["enabled_gates"],
                            "saved_draft_validation": repaired_structure,
                        },
                    }
                repaired_state.update(
                    {
                        "save_result": repaired_save_result,
                        "content_variants": repaired_variants,
                        "content": repaired_content,
                        "validation": repaired_structure,
                    }
                )
                return validate_revision_acceptance_variants(
                    repaired_request,
                    repaired_variants,
                    draft_name=write_info["draft_name"],
                    doc_items=validation_doc_items,
                    strict=strict,
                )

            try:
                acceptance_repair = run_targeted_acceptance_repair(
                    latest_ledger_request,
                    acceptance_validation,
                    repair_callback=apply_repair,
                    prepare_callback=prepare_repair,
                    validation_callback=revalidate_repair,
                )
            except Exception:
                _restore_saved_draft_files(pre_repair_snapshot)
                raise
            acceptance_validation = acceptance_repair
            if repaired_state:
                latest_ledger_request = repaired_state.get("request", latest_ledger_request)
                save_result = repaired_state.get("save_result", save_result)
                content_variants = repaired_state.get("content_variants", content_variants)
                content = repaired_state.get("content", content)
                validation = repaired_state.get("validation", validation)
        if not acceptance_validation["ok"]:
            failure_reasons = [
                str(reason)
                for reason in (acceptance_validation.get("errors") or [])
                if str(reason).strip()
            ]
            if not failure_reasons:
                failure_reasons = [
                    str(failure.get("reason") or "")
                    for failure in (acceptance_validation.get("failures") or [])
                    if isinstance(failure, dict) and str(failure.get("reason") or "").strip()
                ]
            message = "; ".join(dict.fromkeys(failure_reasons)) or "Acceptance failed."
            unresolved_item_ids = sorted(
                {
                    str(item_id).strip()
                    for item_id in (acceptance_validation.get("unresolved_item_ids") or [])
                    if str(item_id).strip()
                }
                | {
                    str(failure.get("item_id") or "").strip()
                    for failure in (acceptance_validation.get("failures") or [])
                    if isinstance(failure, dict) and str(failure.get("item_id") or "").strip()
                }
            )
            draft_dir = _saved_draft_directory(save_result)
            raise RevisionAcceptanceError(
                f"Revision acceptance validation failed: {message}",
                {
                    "draft_name": write_info["draft_name"],
                    "requested_draft_name": write_info["requested_draft_name"],
                    "write_mode": write_info.get("write_mode", "requested_draft"),
                    "draft_path": os.path.join(draft_dir, "draft_content.json"),
                    "draft_dir": draft_dir,
                    "review_marker_count": len(review_marker_receipts),
                    "review_marker_receipts": review_marker_receipts,
                    "validation": validation,
                    "acceptance_validation": acceptance_validation,
                    "acceptance_repair": acceptance_repair,
                    "unresolved_item_ids": unresolved_item_ids,
                    **ui_policy,
                },
            )
        retention = _run_project_retention(project)
        save_result["retention"] = retention

        summary = build_revision_summary(
            latest_ledger_request,
            doc_items=validation_doc_items,
        )
        summary.update(
            {
                "draft_name": write_info["draft_name"],
                "requested_draft_name": write_info["requested_draft_name"],
                "write_mode": write_info.get("write_mode", "requested_draft"),
                "draft_path": save_result.get("draft_path", ""),
                "tracks": (
                    [video_track_name, *audio_delivery_track_names]
                    if segmented_audio_delivery
                    else [
                        video_track_name,
                        source_audio_track_name,
                        replacement_audio_track_name,
                    ]
                ),
                "audio_delivery_results": audio_delivery_results,
                "audio_delivery_plan_sha256": (
                    audio_delivery_plan_sha256(latest_ledger_request)
                    if segmented_audio_delivery
                    else ""
                ),
                "visual_overlay_results": visual_results,
                "pause_adjustment_results": pause_results,
                "review_marker_count": len(review_marker_receipts),
                "review_marker_receipts": review_marker_receipts,
                "validation": validation,
                "acceptance_validation": acceptance_validation,
                "acceptance_repair": acceptance_repair,
                "retention": retention,
                **ui_policy,
            }
        )
        if "fallback_reason" in write_info:
            summary["fallback_reason"] = write_info["fallback_reason"]
        return summary
    except Exception:
        if not validated_editable_draft and write_info.get(
            "cleanup_on_failure", created_new_requested_draft
        ):
            _cleanup_incomplete_draft(project)
        raise
