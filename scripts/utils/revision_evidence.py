"""Shared evidence binding for review-driven revision requests."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from copy import deepcopy
from dataclasses import replace
from typing import Any, Dict, List, Mapping

import cv2
from utils.pause_alignment import (
    PauseAlignmentError,
    extract_word_boundaries,
    protected_utterance_anchor,
    resolve_pause_boundary,
)
from utils.revision_models import (
    PauseAdjustment,
    RevisionEdit,
    RevisionRequest,
    _collect_delete_windows,
    _normalize_review_id,
)

_FRAME_MATCH_MAX_MAE = 3.0
_FRAME_TIME_EPSILON_SECONDS = 0.002


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_path(raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    if not value:
        return ""
    return os.path.normcase(os.path.abspath(value)).replace("\\", "/").lower()


def audio_delivery_plan_sha256(request: RevisionRequest) -> str:
    """Hash the normalized segmented plan and its resolved pause evidence."""

    asset_hashes: Dict[str, str] = {}
    segments = []
    for segment in request.audio_delivery_plan.segments:
        normalized_path = _normalized_path(segment.asset_path)
        if normalized_path not in asset_hashes:
            asset_hashes[normalized_path] = (
                sha256_file(segment.asset_path) if os.path.isfile(segment.asset_path) else ""
            )
        segments.append(
            {
                "segment_id": segment.segment_id,
                "role": segment.role,
                "asset_path": normalized_path,
                "asset_sha256": asset_hashes[normalized_path],
                "track_name": segment.track_name,
                "source_start": segment.source_start,
                "timeline_start": segment.timeline_start,
                "duration": segment.duration,
                "volume": segment.volume,
                "fade_in": segment.fade_in,
                "fade_out": segment.fade_out,
                "doc_item_id": segment.doc_item_id,
                "reason": segment.reason,
            }
        )
    pauses = [
        {
            "item_id": pause.item_id,
            "requested_source_time": pause.requested_source_time,
            "source_time": pause.source_time,
            "duration": pause.duration,
            "frame_sha256": pause.frame_sha256,
            "source_asr_sha256": str(
                (pause.boundary_evidence or {}).get("source_asr_sha256") or ""
            ).casefold(),
        }
        for pause in request.pause_adjustments
    ]
    canonical = {
        "schema": "auto-cut-audio-delivery-plan-v1",
        "mode": request.audio_delivery_plan.mode,
        "segments": segments,
        "pauses": pauses,
    }
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def bind_audio_delivery_plan_to_report(
    request: RevisionRequest,
    report: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return a report copy bound to the exact normalized delivery plan."""

    bound = deepcopy(dict(report))
    bound["audio_delivery_plan_sha256"] = audio_delivery_plan_sha256(request)
    return bound


def _validated_media_hash(
    path: str,
    expected_sha256: Any,
    *,
    field_name: str,
    label: str,
) -> tuple[str, str]:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        raise PauseAlignmentError(f"{label} path is required for semantic pause alignment.")
    if not os.path.isfile(normalized_path):
        raise PauseAlignmentError(f"{label} does not exist: {normalized_path}.")
    expected = str(expected_sha256 or "").strip().casefold()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise PauseAlignmentError(f"{field_name} must be 64 hex characters.")
    actual = sha256_file(normalized_path)
    if actual != expected:
        raise PauseAlignmentError(
            f"Pause alignment {label} SHA-256 mismatch: expected {expected}, got {actual}."
        )
    return os.path.abspath(normalized_path), actual


def _validate_preprocessing_binding(
    preprocessing: Any,
    *,
    source_audio_sha256: str,
    alignment_audio_sha256: str,
) -> None:
    if isinstance(preprocessing, str) and preprocessing.strip().casefold() == "none":
        if source_audio_sha256 != alignment_audio_sha256:
            raise PauseAlignmentError(
                "pause_alignment preprocessing 'none' requires source and alignment "
                "audio bytes to be identical."
            )
        return
    if not isinstance(preprocessing, Mapping):
        raise PauseAlignmentError(
            "pause_alignment requires a hash-bound preprocessing receipt for transformed "
            "alignment audio."
        )
    receipt = dict(preprocessing)
    parameters = receipt.get("parameters")
    if (
        str(receipt.get("source_audio_sha256") or "").strip().casefold() != source_audio_sha256
        or str(receipt.get("alignment_audio_sha256") or "").strip().casefold()
        != alignment_audio_sha256
        or not str(receipt.get("tool") or "").strip()
        or not str(receipt.get("tool_version") or "").strip()
        or not isinstance(parameters, Mapping)
        or not parameters
    ):
        raise PauseAlignmentError(
            "pause_alignment preprocessing receipt must bind source/alignment SHA-256, "
            "tool, tool_version, and parameters."
        )


def validate_pause_source_provenance(request: RevisionRequest) -> Dict[str, Any]:
    """Bind pause ASR to the current source video/audio and ASR adapter identity."""

    config = request.pause_alignment
    source_asr_path, source_asr_sha256 = _validated_media_hash(
        str(config.get("source_asr_path") or ""),
        config.get("source_asr_sha256"),
        field_name="pause_alignment.source_asr_sha256",
        label="source ASR",
    )
    source_video_path, source_video_sha256 = _validated_media_hash(
        request.project.source_video,
        config.get("source_video_sha256"),
        field_name="pause_alignment.source_video_sha256",
        label="source video",
    )
    source_audio_path, source_audio_sha256 = _validated_media_hash(
        request.project.source_audio,
        config.get("source_audio_sha256"),
        field_name="pause_alignment.source_audio_sha256",
        label="source audio",
    )
    alignment_audio_path, alignment_audio_sha256 = _validated_media_hash(
        str(config.get("alignment_audio_path") or ""),
        config.get("alignment_audio_sha256"),
        field_name="pause_alignment.alignment_audio_sha256",
        label="alignment audio",
    )

    raw_identity = config.get("source_asr_identity")
    identity = dict(raw_identity) if isinstance(raw_identity, Mapping) else {}
    provider = str(identity.get("provider") or "").strip()
    model = str(
        identity.get("model") or identity.get("model_id") or identity.get("resource_id") or ""
    ).strip()
    adapter_version = str(identity.get("adapter_version") or "").strip()
    preprocessing = identity.get("preprocessing")
    preprocessing_present = bool(
        str(preprocessing).strip() if not isinstance(preprocessing, (dict, list)) else preprocessing
    )
    if not provider or not model or not adapter_version or not preprocessing_present:
        raise PauseAlignmentError(
            "pause_alignment requires complete source_asr_identity "
            "(provider, model/resource ID, adapter_version, preprocessing)."
        )
    _validate_preprocessing_binding(
        preprocessing,
        source_audio_sha256=source_audio_sha256,
        alignment_audio_sha256=alignment_audio_sha256,
    )

    return {
        "source_asr_path": source_asr_path,
        "source_asr_sha256": source_asr_sha256,
        "source_video_path": source_video_path,
        "source_video_sha256": source_video_sha256,
        "source_audio_path": source_audio_path,
        "source_audio_sha256": source_audio_sha256,
        "alignment_audio_path": alignment_audio_path,
        "alignment_audio_sha256": alignment_audio_sha256,
        "source_asr_identity": identity,
    }


def validate_semantic_pause_pairing(request: RevisionRequest) -> bool:
    """Require one semantic-pause edit per adjustment before media work."""

    pause_adjustments_by_id: Dict[str, List[PauseAdjustment]] = {}
    for adjustment in request.pause_adjustments:
        normalized_id = _normalize_review_id(adjustment.item_id)
        if not normalized_id:
            raise PauseAlignmentError(
                "Every semantic pause adjustment requires a non-empty item_id."
            )
        pause_adjustments_by_id.setdefault(normalized_id, []).append(adjustment)
    semantic_edits_by_id: Dict[str, List[RevisionEdit]] = {}
    for edit in request.edits:
        if edit.op_type != "semantic_pause_adjustment":
            continue
        normalized_id = _normalize_review_id(edit.doc_item_id)
        semantic_edits_by_id.setdefault(normalized_id, []).append(edit)
    if not pause_adjustments_by_id and not semantic_edits_by_id:
        return False
    duplicate_ids = sorted(
        normalized_id
        for normalized_id in set(pause_adjustments_by_id).union(semantic_edits_by_id)
        if len(pause_adjustments_by_id.get(normalized_id, [])) > 1
        or len(semantic_edits_by_id.get(normalized_id, [])) > 1
    )
    if duplicate_ids:
        raise PauseAlignmentError(
            "One source item cannot carry duplicate semantic pauses until pause receipts "
            "support one-to-many evidence; duplicate semantic pauses: "
            + ", ".join(duplicate_ids)
            + "."
        )
    unmatched_ids = sorted(
        normalized_id
        for normalized_id in set(pause_adjustments_by_id).union(semantic_edits_by_id)
        if len(semantic_edits_by_id.get(normalized_id, []))
        != len(pause_adjustments_by_id.get(normalized_id, []))
    )
    if unmatched_ids:
        raise PauseAlignmentError(
            "Pause adjustments and matching semantic pause edits must correspond one-for-one; "
            "every semantic pause edit requires a matching pause adjustment; unmatched item "
            "ids: " + ", ".join(unmatched_ids) + "."
        )
    return True


def normalize_pause_adjustments(request: RevisionRequest) -> RevisionRequest:
    """Resolve semantic pause timestamps against bound source ASR evidence."""

    if not validate_semantic_pause_pairing(request):
        return request
    if not request.pause_alignment:
        raise PauseAlignmentError(
            "semantic pause adjustments require pause_alignment with hash-bound source ASR."
        )

    config = request.pause_alignment
    source_asr_path = str(config.get("source_asr_path") or "").strip()
    if not source_asr_path:
        raise PauseAlignmentError("pause_alignment.source_asr_path is required.")
    with open(source_asr_path, "rb") as source:
        source_asr_bytes = source.read()
    source_asr_sha256 = hashlib.sha256(source_asr_bytes).hexdigest()
    alignment_payload = json.loads(source_asr_bytes.decode("utf-8-sig"))
    if not isinstance(alignment_payload, (dict, list)):
        raise PauseAlignmentError("pause_alignment source ASR payload must be an object or list.")

    expected_source_asr_sha256 = str(config.get("source_asr_sha256") or "").strip().casefold()
    if not expected_source_asr_sha256:
        raise PauseAlignmentError("pause_alignment.source_asr_sha256 is required.")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_source_asr_sha256):
        raise PauseAlignmentError("pause_alignment.source_asr_sha256 must be 64 hex characters.")
    if source_asr_sha256 != expected_source_asr_sha256:
        raise PauseAlignmentError(
            "pause_alignment source ASR SHA-256 mismatch: "
            f"expected {expected_source_asr_sha256}, got {source_asr_sha256}."
        )
    source_provenance = validate_pause_source_provenance(request)

    extract_word_boundaries(alignment_payload)
    min_gap = float(config.get("min_gap_seconds", 0.35))
    search_window = float(config.get("search_window_seconds", 3.0))
    semantic_gap = float(config.get("semantic_gap_seconds", 0.8))
    edge_guard = float(config.get("edge_guard_seconds", 0.05))
    tolerance = float(config.get("tolerance_seconds", 0.005))
    delete_windows = _collect_delete_windows(request)
    normalized = []
    resolved_by_item: Dict[str, List[float]] = {}
    for adjustment in request.pause_adjustments:
        expected_frame_sha256 = adjustment.frame_sha256.strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_frame_sha256):
            raise PauseAlignmentError(
                f"Pause {adjustment.item_id or '<unidentified>'} requires frame_sha256 "
                "with 64 hex characters."
            )
        try:
            with open(adjustment.frame_path, "rb") as frame_file:
                actual_frame_sha256 = hashlib.sha256(frame_file.read()).hexdigest()
        except OSError as exc:
            raise PauseAlignmentError(
                f"Pause {adjustment.item_id or '<unidentified>'} still frame is unreadable: "
                f"{adjustment.frame_path}."
            ) from exc
        if actual_frame_sha256 != expected_frame_sha256:
            raise PauseAlignmentError(
                f"Pause {adjustment.item_id or '<unidentified>'} still-frame SHA-256 mismatch: "
                f"expected {expected_frame_sha256}, got {actual_frame_sha256}."
            )
        decoded_frame = cv2.imread(adjustment.frame_path, cv2.IMREAD_UNCHANGED)
        if decoded_frame is None or decoded_frame.size <= 0:
            raise PauseAlignmentError(
                f"Pause {adjustment.item_id or '<unidentified>'} still frame must be a "
                "decodable image."
            )
        boundary = resolve_pause_boundary(
            (
                adjustment.requested_source_time
                if adjustment.requested_source_time is not None
                else adjustment.source_time
            ),
            alignment_payload,
            min_gap_seconds=min_gap,
            search_window_seconds=search_window,
            semantic_gap_seconds=semantic_gap,
            edge_guard_seconds=edge_guard,
            tolerance_seconds=tolerance,
        )
        if boundary.reason != "nearest_utterance_gap_midpoint":
            raise PauseAlignmentError(
                f"Pause {adjustment.item_id or '<unidentified>'} requires a real adjacent "
                "utterance gap; word-level ASR gaps are not sufficient."
            )
        for delete_start, delete_end in delete_windows:
            if delete_start - 1e-6 <= boundary.resolved_time <= delete_end + 1e-6:
                raise PauseAlignmentError(
                    f"Pause {adjustment.item_id or '<unidentified>'} resolved boundary "
                    f"{boundary.resolved_time:.3f}s falls inside delete window or on its boundary "
                    f"{delete_start:.3f}-{delete_end:.3f}s."
                )
        if adjustment.frame_source_time is None:
            raise PauseAlignmentError(
                f"Pause {adjustment.item_id or '<unidentified>'} requires "
                f"frame_source_time at resolved boundary {boundary.resolved_time:.3f}s."
            )
        if abs(adjustment.frame_source_time - boundary.resolved_time) > 1e-3:
            raise PauseAlignmentError(
                f"Pause {adjustment.item_id or '<unidentified>'} still frame was sampled "
                f"at {adjustment.frame_source_time:.3f}s; re-extract still frame at "
                f"resolved boundary {boundary.resolved_time:.3f}s."
            )
        frame_match_receipt = validate_pause_frame_matches_source(
            source_provenance["source_video_path"],
            boundary.resolved_time,
            adjustment.frame_path,
        )
        evidence = {
            "status": "pass",
            "requested_time": boundary.requested_time,
            "resolved_time": boundary.resolved_time,
            "previous_word_end": boundary.previous_word_end,
            "next_word_start": boundary.next_word_start,
            "gap_duration": boundary.gap_duration,
            "previous_guard_seconds": boundary.previous_guard_seconds,
            "next_guard_seconds": boundary.next_guard_seconds,
            "minimum_edge_guard_seconds": boundary.minimum_edge_guard_seconds,
            "placement": boundary.placement,
            "snapped": boundary.snapped,
            "reason": boundary.reason,
            "previous_utterance_text": boundary.previous_utterance_text,
            "next_utterance_text": boundary.next_utterance_text,
            "previous_utterance_end": boundary.previous_utterance_end,
            "next_utterance_start": boundary.next_utterance_start,
            "previous_protected_trailing_anchor": protected_utterance_anchor(
                boundary.previous_utterance_text,
                leading=False,
            ),
            "next_protected_leading_anchor": protected_utterance_anchor(
                boundary.next_utterance_text,
                leading=True,
            ),
            "source_asr_path": source_asr_path,
            "source_asr_sha256": source_asr_sha256,
            "min_gap_seconds": min_gap,
            "semantic_gap_seconds": semantic_gap,
            "search_window_seconds": search_window,
            "edge_guard_seconds": edge_guard,
            "tolerance_seconds": tolerance,
            **source_provenance,
            "frame_match_receipt": frame_match_receipt,
        }
        normalized.append(
            replace(
                adjustment,
                source_time=boundary.resolved_time,
                requested_source_time=(
                    adjustment.requested_source_time
                    if adjustment.requested_source_time is not None
                    else boundary.requested_time
                ),
                boundary_evidence=evidence,
            )
        )
        resolved_by_item.setdefault(_normalize_review_id(adjustment.item_id), []).append(
            boundary.resolved_time
        )

    normalized_edits = []
    consumed_resolved_by_item: Dict[str, int] = {}
    for edit in request.edits:
        normalized_id = _normalize_review_id(edit.doc_item_id)
        resolved_times = resolved_by_item.get(normalized_id) or []
        resolved_index = consumed_resolved_by_item.get(normalized_id, 0)
        if edit.op_type == "semantic_pause_adjustment" and resolved_index < len(resolved_times):
            resolved_time = resolved_times[resolved_index]
            consumed_resolved_by_item[normalized_id] = resolved_index + 1
            normalized_edits.append(replace(edit, start=resolved_time, end=resolved_time))
        else:
            normalized_edits.append(edit)
    return replace(
        request,
        edits=normalized_edits,
        pause_adjustments=normalized,
    )


def validate_pause_frame_matches_source(
    source_video_path: str,
    source_time: float,
    frame_path: str,
) -> Dict[str, Any]:
    """Decode the current source frame and compare it with the supplied still."""

    if not math.isfinite(source_time) or source_time < 0:
        raise PauseAlignmentError("Pause frame source time must be finite and non-negative.")
    still = cv2.imread(frame_path, cv2.IMREAD_COLOR)
    if still is None or still.size <= 0:
        raise PauseAlignmentError("Pause still frame must be a decodable image.")

    capture = cv2.VideoCapture(source_video_path)
    try:
        if not capture.isOpened():
            raise PauseAlignmentError(f"Pause source video is not decodable: {source_video_path}.")
        frames_per_second = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(frames_per_second) or frames_per_second <= 0:
            raise PauseAlignmentError("Pause source video has no valid frame-rate evidence.")
        if not capture.set(cv2.CAP_PROP_POS_MSEC, source_time * 1_000.0):
            raise PauseAlignmentError(f"Pause source video could not seek to {source_time:.3f}s.")
        ok, source_frame = capture.read()
        decoded_time = capture.get(cv2.CAP_PROP_POS_MSEC) / 1_000.0
    finally:
        capture.release()
    if not ok or source_frame is None or source_frame.size <= 0:
        raise PauseAlignmentError(
            f"Pause source video has no decodable frame at {source_time:.3f}s."
        )
    frame_interval = 1.0 / frames_per_second
    maximum_time_error = frame_interval + _FRAME_TIME_EPSILON_SECONDS
    decoded_time_error = abs(decoded_time - source_time)
    if not math.isfinite(decoded_time) or decoded_time_error > maximum_time_error:
        raise PauseAlignmentError(
            f"Pause source video decoded source time {decoded_time:.3f}s does not match "
            f"requested {source_time:.3f}s within {maximum_time_error:.3f}s."
        )
    if source_frame.shape != still.shape:
        raise PauseAlignmentError(
            f"Pause still frame does not match source video at {source_time:.3f}s "
            f"(shape {still.shape} != {source_frame.shape})."
        )
    difference = cv2.absdiff(source_frame, still)
    mean_absolute_error = sum(cv2.mean(difference)[:3]) / 3.0
    if mean_absolute_error > _FRAME_MATCH_MAX_MAE:
        raise PauseAlignmentError(
            f"Pause still frame does not match source video at {source_time:.3f}s "
            f"(mean absolute error {mean_absolute_error:.3f} > {_FRAME_MATCH_MAX_MAE:.3f})."
        )
    return {
        "method": "opencv_source_frame_mae_v2",
        "requested_source_time": source_time,
        "decoded_source_time": decoded_time,
        "frames_per_second": frames_per_second,
        "frame_interval_seconds": frame_interval,
        "decoded_source_time_error_seconds": decoded_time_error,
        "maximum_decoded_source_time_error_seconds": maximum_time_error,
        "mean_absolute_error": mean_absolute_error,
        "maximum_mean_absolute_error": _FRAME_MATCH_MAX_MAE,
        "status": "pass",
    }


__all__ = [
    "audio_delivery_plan_sha256",
    "bind_audio_delivery_plan_to_report",
    "normalize_pause_adjustments",
    "sha256_file",
    "validate_semantic_pause_pairing",
    "validate_pause_frame_matches_source",
    "validate_pause_source_provenance",
]
