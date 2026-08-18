from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import av
import cv2
import numpy as np
from pymediainfo import MediaInfo

DEFAULT_CANVAS_WIDTH = 1920.0
DEFAULT_CANVAS_HEIGHT = 1080.0
NUMBER_TOLERANCE = 1e-6
CLEAN_MOTION_MIN_MEAN_DELTA = 0.01
CLEAN_MOTION_MIN_CHANGED_RATIO = 0.0001
CLEAN_MEDIA_DURATION_TOLERANCE_SECONDS = 0.05
CLEAN_SOURCE_MAX_MEAN_DELTA = 5.0
CLEAN_SOURCE_MAX_CHANGED_RATIO = 0.01
CLEAN_SOURCE_CHANGED_PIXEL_DELTA = 16
POINTER_COVER_BACKGROUND_MAX_MEAN_DELTA = 2.0
POINTER_COVER_BACKGROUND_MAX_CHANGED_RATIO = 0.002
POINTER_COVER_CLEAN_FRAME_MAX_CHANGED_RATIO = 0.012
POINTER_COVER_CHANGED_PIXEL_DELTA = 12
POINTER_COVER_OPENED_TIME_TOLERANCE_SECONDS = 0.08
POINTER_COVER_OPENED_CONTEXT_MIN_CORRELATION = 0.97
POINTER_COVER_OPENED_CONTEXT_MAX_MEAN_DELTA = 8.0
POINTER_COVER_OPENED_CONTEXT_MAX_P95_DELTA = 48.0
POINTER_COVER_OPENED_COVER_MIN_CORRELATION = 0.75
POINTER_COVER_OPENED_DISTINCT_CHANGED_RATIO = 0.0005
POINTER_COVER_OPENED_TEMPLATE_MAX_SQDIFF = 0.10
POINTER_COVER_OPENED_TEMPLATE_SIZE_TOLERANCE_PX = 2
POINTER_COVER_RECORDED_ALPHA_MAX = 0.09
POINTER_COVER_OPENED_COVERAGE_ROLES = {
    "recorded_first_visible",
    "recorded_midpoint",
    "recorded_last_visible",
}
PASS_STATUSES = {"pass", "passed", "ok", "success", "verified"}
POINTER_LIFECYCLE_MODES = {
    "editable_pointer",
    "visual_only_pointer",
    "handoff_to_recorded_pointer",
    "replace_recorded_pointer_then_handoff",
    "cleanup_recorded_pointer",
    "remove_recorded_pointer_until_absent",
    "remove_recorded_pointer_until_relevant",
}
CLEANUP_ONLY_POINTER_LIFECYCLE_MODES = {
    "cleanup_recorded_pointer",
    "remove_recorded_pointer_until_absent",
    "remove_recorded_pointer_until_relevant",
}
RECORDED_POINTER_EDIT_MODES = {
    "replace_recorded_pointer_then_handoff",
    *CLEANUP_ONLY_POINTER_LIFECYCLE_MODES,
}
HANDOFF_POINTER_MODES = {
    "handoff_to_recorded_pointer",
    "replace_recorded_pointer_then_handoff",
}
_POINTER_TRACK_HINT = re.compile(
    r"(?:pointer|hand|finger|arrow|display[ _-]*safe|calibrated|小手|手指|指针|箭头|校准)",
    re.IGNORECASE,
)
_UNDERLINE_TRACK_HINT = re.compile(
    r"(?:underline|line[ _-]*decoration|decoration[ _-]*line|下划线|划线)",
    re.IGNORECASE,
)
_CLEAN_TRACK_HINT = re.compile(
    r"(?:clean|restoration|restore|patch|cover|清理|修复|遮盖)",
    re.IGNORECASE,
)


def _status_passed(value: Any) -> bool:
    return str(value or "").strip().casefold() in PASS_STATUSES


def _positive_rectangle(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Mapping):
        return None
    x = _number(value.get("x"))
    y = _number(value.get("y"))
    width = _number(value.get("width"))
    height = _number(value.get("height"))
    if (
        x is None
        or y is None
        or width is None
        or height is None
        or x < 0
        or y < 0
        or width <= 0
        or height <= 0
    ):
        return None
    return x, y, width, height


def _opened_cover_visual_inspection_valid(sample: Mapping[str, Any]) -> bool:
    inspection = sample.get("visual_inspection")
    hand_count = _number(inspection.get("hand_count")) if isinstance(inspection, Mapping) else None
    hand_regions = inspection.get("hand_regions") if isinstance(inspection, Mapping) else None
    artifact_sha256 = str(sample.get("artifact_sha256") or "").strip().lower()
    return bool(
        isinstance(inspection, Mapping)
        and _status_passed(inspection.get("status"))
        and hand_count is not None
        and abs(hand_count - 1.0) <= NUMBER_TOLERANCE
        and inspection.get("editable_pointer_visible") is True
        and inspection.get("original_recorded_hand_visible") is False
        and inspection.get("method") == "opened_jianying_canvas_manual_inspection_v2"
        and inspection.get("inspector") == "codex_visual_review"
        and str(inspection.get("artifact_sha256") or "").strip().lower() == artifact_sha256
        and isinstance(hand_regions, list)
        and len(hand_regions) == 1
        and _positive_rectangle(hand_regions[0]) is not None
    )


def _opened_capture_receipt_valid(sample: Mapping[str, Any], context: Any) -> bool:
    receipt = sample.get("editor_capture_receipt")
    record_path = sample.get("editor_capture_record_path")
    record_sha256 = str(sample.get("editor_capture_record_sha256") or "").strip().lower()
    record: Any = None
    if isinstance(record_path, (str, os.PathLike)) and record_sha256:
        path = Path(os.fspath(record_path)).expanduser()
        if path.is_file() and _sha256_file(path) == record_sha256:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                record = None
    sample_canvas = _positive_rectangle(sample.get("editor_canvas_rect"))
    receipt_canvas = (
        _positive_rectangle(receipt.get("editor_canvas_rect"))
        if isinstance(receipt, Mapping)
        else None
    )
    window_rect = (
        _positive_rectangle(receipt.get("window_rect")) if isinstance(receipt, Mapping) else None
    )
    sample_time = _number(sample.get("timeline_time"))
    receipt_time = (
        _number(receipt.get("playhead_timeline_time")) if isinstance(receipt, Mapping) else None
    )
    captured_at = str(receipt.get("captured_at") or "") if isinstance(receipt, Mapping) else ""
    if (
        not isinstance(receipt, Mapping)
        or not isinstance(record, Mapping)
        or record != dict(receipt)
        or not _status_passed(receipt.get("status"))
        or receipt.get("method") != "windows_jianying_window_capture_v1"
        or receipt.get("capture_tool") != "codex_desktop_capture"
        or str(receipt.get("process_name") or "").strip().casefold() != "jianyingpro.exe"
        or not any(
            marker in str(receipt.get("window_title") or "").casefold()
            for marker in ("jianyingpro", "剪映")
        )
        or receipt.get("timeline_id") != sample.get("timeline_id")
        or sample_time is None
        or receipt_time is None
        or abs(sample_time - receipt_time) > 0.02
        or not _same_path(receipt.get("editor_context_path"), sample.get("editor_context_path"))
        or str(receipt.get("editor_context_sha256") or "").strip().lower()
        != str(sample.get("editor_context_sha256") or "").strip().lower()
        or sample_canvas is None
        or receipt_canvas is None
        or any(
            abs(left - right) > NUMBER_TOLERANCE
            for left, right in zip(sample_canvas, receipt_canvas)
        )
        or window_rect is None
        or context is None
        or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            captured_at,
        )
        is None
    ):
        return False
    window_x, window_y, window_width, window_height = window_rect
    canvas_x, canvas_y, canvas_width, canvas_height = sample_canvas
    return bool(
        window_x + window_width <= context.shape[1] + NUMBER_TOLERANCE
        and window_y + window_height <= context.shape[0] + NUMBER_TOLERANCE
        and window_x <= canvas_x + NUMBER_TOLERANCE
        and window_y <= canvas_y + NUMBER_TOLERANCE
        and window_x + window_width >= canvas_x + canvas_width - NUMBER_TOLERANCE
        and window_y + window_height >= canvas_y + canvas_height - NUMBER_TOLERANCE
    )


def _opened_cover_receipt_problems(
    opened_samples: Sequence[Mapping[str, Any]],
) -> list[str]:
    if len(opened_samples) < len(POINTER_COVER_OPENED_COVERAGE_ROLES):
        return ["pointer_cover.opened_jianying_window_coverage_incomplete"]
    roles = [sample.get("coverage_role") for sample in opened_samples]
    if not POINTER_COVER_OPENED_COVERAGE_ROLES.issubset(set(roles)) or len(set(roles)) != len(
        roles
    ):
        return ["pointer_cover.opened_jianying_window_coverage_incomplete"]
    paths = [
        (
            os.path.normcase(os.path.abspath(os.fspath(sample.get("artifact_path"))))
            if isinstance(sample.get("artifact_path"), (str, os.PathLike))
            else ""
        )
        for sample in opened_samples
    ]
    hashes = [str(sample.get("artifact_sha256") or "").strip().lower() for sample in opened_samples]
    if (
        any(not path for path in paths)
        or any(re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in hashes)
        or len(set(paths)) != len(paths)
        or len(set(hashes)) != len(hashes)
    ):
        return ["pointer_cover.opened_jianying_samples_not_distinct"]
    context_paths = [
        (
            os.path.normcase(os.path.abspath(os.fspath(sample.get("editor_context_path"))))
            if isinstance(sample.get("editor_context_path"), (str, os.PathLike))
            else ""
        )
        for sample in opened_samples
    ]
    context_hashes = [
        str(sample.get("editor_context_sha256") or "").strip().lower() for sample in opened_samples
    ]
    if (
        any(not path for path in context_paths)
        or any(re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in context_hashes)
        or len(set(context_paths)) != len(context_paths)
        or len(set(context_hashes)) != len(context_hashes)
        or any(
            _positive_rectangle(sample.get("editor_canvas_rect")) is None
            for sample in opened_samples
        )
    ):
        return ["pointer_cover.opened_jianying_editor_context_invalid"]
    if any(
        _number(sample.get("source_time")) is None
        or _number(sample.get("timeline_time")) is None
        or not isinstance(sample.get("timeline_id"), str)
        or not sample.get("timeline_id").strip()
        for sample in opened_samples
    ):
        return ["pointer_cover.opened_jianying_window_coverage_incomplete"]
    if any(not _opened_cover_visual_inspection_valid(sample) for sample in opened_samples):
        return ["pointer_cover.opened_jianying_visual_inspection_invalid"]
    return []


def _static_residual_cover_contract_valid(
    evidence: Mapping[str, Any], cover_start: float | None, cover_end: float | None
) -> bool:
    cover = evidence.get("residual_pointer_cover")
    if (
        not isinstance(cover, Mapping)
        or not _status_passed(cover.get("status"))
        or cover.get("mode") != "transparent_roi_still_cover"
        or cover.get("region_shape") != "hard_edge_rectangle"
    ):
        return False
    source_window = cover.get("source_window")
    source_start = (
        _number(source_window.get("start")) if isinstance(source_window, Mapping) else None
    )
    source_end = _number(source_window.get("end")) if isinstance(source_window, Mapping) else None
    regions = cover.get("opaque_regions")
    trajectory_bounds = cover.get("trajectory_bounds")
    safety_margin = _number(cover.get("safety_margin_px"))
    background_samples = cover.get("background_samples")
    composite_samples = cover.get("final_composite_samples")
    trajectory_receipt = cover.get("trajectory_receipt")
    source_media_path = cover.get("source_media_path")
    source_media_sha256 = str(cover.get("source_media_sha256") or "").strip().lower()
    opened_samples = [
        sample
        for sample in composite_samples or []
        if isinstance(sample, Mapping) and sample.get("artifact_kind") == "opened_jianying"
    ]
    preopen_sample_exists = any(
        isinstance(sample, Mapping) and sample.get("artifact_kind") != "opened_jianying"
        for sample in composite_samples or []
    )
    return (
        cover_start is not None
        and cover_end is not None
        and source_start is not None
        and source_end is not None
        and abs(source_start - cover_start) <= NUMBER_TOLERANCE
        and abs(source_end - cover_end) <= NUMBER_TOLERANCE
        and isinstance(regions, list)
        and len(regions) == 1
        and _positive_rectangle(regions[0]) is not None
        and _positive_rectangle(trajectory_bounds) is not None
        and safety_margin is not None
        and safety_margin > 0
        and isinstance(background_samples, list)
        and len(background_samples) >= 3
        and isinstance(source_media_path, (str, os.PathLike))
        and bool(os.fspath(source_media_path))
        and re.fullmatch(r"[0-9a-f]{64}", source_media_sha256) is not None
        and isinstance(trajectory_receipt, Mapping)
        and _status_passed(trajectory_receipt.get("status"))
        and trajectory_receipt.get("method") == "source_clean_frame_sequence_v1"
        and isinstance(composite_samples, list)
        and preopen_sample_exists
        and not _opened_cover_receipt_problems(opened_samples)
    )


def pointer_lifecycle_evidence_problems(evidence: Mapping[str, Any]) -> list[str]:
    """Validate speech/visual evidence used to choose pointer in/out boundaries.

    Review-document timestamps are search hints. They cannot be promoted directly to
    exact pointer boundaries without speech alignment, frame scanning, and tail proof.
    """

    if not isinstance(evidence, Mapping):
        return ["pointer_lifecycle.evidence_not_object"]

    problems: list[str] = []
    duration_rule = str(evidence.get("duration_rule") or "").strip().casefold()
    anchor_rule = str(evidence.get("anchor_rule") or "").strip().casefold()
    boundary_control = str(evidence.get("boundary_control") or "").strip().casefold()
    if boundary_control not in {"speech", "visual", "hybrid"}:
        problems.append("pointer_lifecycle.boundary_control_required")
    lifecycle_mode = str(evidence.get("lifecycle_mode") or "").strip().casefold()
    if not lifecycle_mode:
        problems.append("pointer_lifecycle.lifecycle_mode_required")
    elif lifecycle_mode not in POINTER_LIFECYCLE_MODES:
        problems.append("pointer_lifecycle.lifecycle_mode_invalid")
    if str(evidence.get("review_timestamp_role") or "").strip().casefold() != "search_hint":
        problems.append("pointer_lifecycle.review_timestamp_role_required")
    rough_window_markers = (
        "review comment source window",
        "exact review window",
        "rough review window",
        "teacher timestamp",
    )
    if "search hint" not in duration_rule and any(
        marker in duration_rule for marker in rough_window_markers
    ):
        problems.append("pointer_lifecycle.rough_time_used_as_exact_boundary")

    speech_anchor = evidence.get("speech_anchor")
    speech_led = boundary_control in {"speech", "hybrid"}
    speech_anchor_passed = False
    speech_anchor_visual_only = False
    if isinstance(speech_anchor, Mapping):
        status = str(speech_anchor.get("status") or "").strip().casefold()
        if _status_passed(status):
            phrase = str(speech_anchor.get("phrase") or "").strip()
            source_start = _number(speech_anchor.get("source_start"))
            source_end = _number(speech_anchor.get("source_end"))
            speech_anchor_passed = (
                bool(phrase)
                and source_start is not None
                and source_end is not None
                and source_end >= source_start
            )
        elif status == "visual_only":
            speech_anchor_visual_only = not speech_led and bool(
                str(speech_anchor.get("reason") or "").strip()
            )
    if not speech_anchor_passed and not speech_anchor_visual_only:
        problems.append("pointer_lifecycle.speech_anchor_required")

    pointer_window = evidence.get("source_pointer_window")
    pointer_start = None
    pointer_end = None
    if isinstance(pointer_window, Mapping):
        pointer_start = _number(pointer_window.get("start"))
        pointer_end = _number(pointer_window.get("end"))
        if pointer_start is None or pointer_end is None or pointer_end < pointer_start:
            problems.append("pointer_lifecycle.source_pointer_window_invalid")

    handoff = evidence.get("source_pointer_handoff")
    handoff_expected = isinstance(handoff, Mapping) or lifecycle_mode in HANDOFF_POINTER_MODES
    recorded_pointer_edit = lifecycle_mode in RECORDED_POINTER_EDIT_MODES
    if lifecycle_mode in POINTER_LIFECYCLE_MODES and not isinstance(pointer_window, Mapping):
        problems.append("pointer_lifecycle.source_pointer_window_required")
    elif isinstance(handoff, Mapping) and not isinstance(pointer_window, Mapping):
        problems.append("pointer_lifecycle.source_pointer_window_required")
    if handoff_expected and not isinstance(handoff, Mapping):
        problems.append("pointer_lifecycle.source_pointer_handoff_required")

    if not speech_anchor_visual_only:
        start_alignment = evidence.get("start_alignment")
        if not isinstance(start_alignment, Mapping) or not _status_passed(
            start_alignment.get("status")
        ):
            problems.append("pointer_lifecycle.start_alignment_required")
        else:
            speech_start = _number(start_alignment.get("speech_start"))
            pointer_first_visible = _number(start_alignment.get("pointer_first_visible"))
            alignment_error = _number(start_alignment.get("alignment_error_seconds"))
            max_early = _number(start_alignment.get("max_early_seconds"))
            max_late = _number(start_alignment.get("max_late_seconds"))
            anchor_start = (
                _number(speech_anchor.get("source_start"))
                if isinstance(speech_anchor, Mapping)
                else None
            )
            calculated_error = (
                pointer_first_visible - speech_start
                if speech_start is not None and pointer_first_visible is not None
                else None
            )
            if (
                speech_start is None
                or pointer_first_visible is None
                or alignment_error is None
                or max_early is None
                or max_late is None
                or max_early < 0
                or max_late < 0
                or anchor_start is None
                or abs(speech_start - anchor_start) > NUMBER_TOLERANCE
                or calculated_error is None
                or abs(alignment_error - calculated_error) > NUMBER_TOLERANCE
                or (
                    pointer_start is not None
                    and abs(pointer_first_visible - pointer_start) > NUMBER_TOLERANCE
                )
                or alignment_error < -max_early - NUMBER_TOLERANCE
                or alignment_error > max_late + NUMBER_TOLERANCE
            ):
                problems.append("pointer_lifecycle.start_alignment_invalid")

    if "first" in anchor_rule and "character" in anchor_rule:
        first_character = evidence.get("first_character_target")
        geometry = (
            _target_geometry(first_character.get("geometry"))
            if isinstance(first_character, Mapping)
            else None
        )
        character = (
            str(first_character.get("character") or "").strip()
            if isinstance(first_character, Mapping)
            else ""
        )
        phrase = (
            str(speech_anchor.get("phrase") or "").strip()
            if isinstance(speech_anchor, Mapping)
            else ""
        )
        expected_character = phrase[0] if phrase else ""
        target_geometry = _target_geometry(evidence.get("target_geometry"))
        geometry_in_canvas = False
        if geometry is not None:
            x, y, width, height, canvas_width, canvas_height = geometry
            geometry_in_canvas = (
                x >= 0 and y >= 0 and x + width <= canvas_width and y + height <= canvas_height
            )
        geometry_matches_target = (
            geometry is not None
            and target_geometry is not None
            and all(
                abs(actual - expected) <= NUMBER_TOLERANCE
                for actual, expected in zip(geometry, target_geometry)
            )
        )
        if (
            not isinstance(first_character, Mapping)
            or not _status_passed(first_character.get("status"))
            or len(character) != 1
            or character != expected_character
            or not geometry_in_canvas
            or not geometry_matches_target
        ):
            problems.append("pointer_lifecycle.first_character_target_required")

    tail_scan = evidence.get("tail_scan")
    if not isinstance(tail_scan, Mapping) or not _status_passed(tail_scan.get("status")):
        problems.append("pointer_lifecycle.tail_scan_required")
    else:
        scan_end = _number(tail_scan.get("scan_end"))
        last_visible = _number(tail_scan.get("last_pointer_visible"))
        absent_after = _number(tail_scan.get("pointer_absent_after"))
        relevant_after = _number(tail_scan.get("pointer_relevant_after"))
        relevance_reason = str(tail_scan.get("relevance_reason") or "").strip()
        release_outcome_count = int(absent_after is not None) + int(relevant_after is not None)
        release_boundary = absent_after if absent_after is not None else relevant_after
        recorded_visibility = evidence.get("recorded_pointer_visibility")
        recorded_last_visible = (
            _number(recorded_visibility.get("last_visible"))
            if isinstance(recorded_visibility, Mapping)
            else None
        )
        inspected_window_end = None
        clean_cover_for_tail = evidence.get("clean_cover_window")
        visibility_for_tail = evidence.get("recorded_pointer_visibility")
        if isinstance(clean_cover_for_tail, Mapping) and isinstance(visibility_for_tail, Mapping):
            cover_start = _number(clean_cover_for_tail.get("source_start"))
            cover_end = _number(clean_cover_for_tail.get("source_end"))
            first_visible = _number(visibility_for_tail.get("first_visible"))
            visibility_end = _number(visibility_for_tail.get("last_visible"))
            if (
                cover_start is not None
                and cover_end is not None
                and first_visible is not None
                and visibility_end is not None
                and cover_start <= first_visible
                and cover_end >= visibility_end
            ):
                inspected_window_end = cover_end
        elif pointer_end is not None:
            handoff_first_visible = (
                _number(handoff.get("first_visible")) if isinstance(handoff, Mapping) else None
            )
            if handoff_first_visible is None or pointer_end <= handoff_first_visible:
                inspected_window_end = pointer_end
        if (
            scan_end is None
            or last_visible is None
            or release_boundary is None
            or release_outcome_count != 1
            or (relevant_after is not None and not relevance_reason)
            or release_boundary < last_visible
            or scan_end < release_boundary
            or (
                recorded_last_visible is not None
                and (
                    abs(last_visible - recorded_last_visible) > NUMBER_TOLERANCE
                    or release_boundary < recorded_last_visible
                )
            )
            or (
                inspected_window_end is not None
                and (scan_end < inspected_window_end or last_visible > inspected_window_end)
            )
        ):
            problems.append("pointer_lifecycle.tail_scan_invalid")

    visibility = evidence.get("recorded_pointer_visibility")
    clean_cover = evidence.get("clean_cover_window")
    if recorded_pointer_edit and not isinstance(visibility, Mapping):
        problems.append("pointer_lifecycle.recorded_pointer_visibility_required")
    if isinstance(visibility, Mapping) != isinstance(clean_cover, Mapping):
        problems.append("pointer_lifecycle.clean_cover_required")
    if isinstance(visibility, Mapping) and isinstance(clean_cover, Mapping):
        first_visible = _number(visibility.get("first_visible"))
        last_visible = _number(visibility.get("last_visible"))
        cover_start = _number(clean_cover.get("source_start"))
        cover_end = _number(clean_cover.get("source_end"))
        if (
            not _status_passed(visibility.get("status"))
            or first_visible is None
            or last_visible is None
            or last_visible < first_visible
            or cover_start is None
            or cover_end is None
            or cover_start > first_visible
            or cover_end < last_visible
        ):
            problems.append("pointer_lifecycle.clean_cover_incomplete")

        motion_preservation = evidence.get("motion_preservation")
        motion_path = (
            motion_preservation.get("clean_media_path")
            if isinstance(motion_preservation, Mapping)
            else None
        )
        motion_sha256 = (
            str(motion_preservation.get("clean_media_sha256") or "").strip().lower()
            if isinstance(motion_preservation, Mapping)
            else ""
        )
        motion_source_window = (
            motion_preservation.get("source_window")
            if isinstance(motion_preservation, Mapping)
            else None
        )
        motion_source_start = (
            _number(motion_source_window.get("start"))
            if isinstance(motion_source_window, Mapping)
            else None
        )
        motion_source_end = (
            _number(motion_source_window.get("end"))
            if isinstance(motion_source_window, Mapping)
            else None
        )
        samples = (
            motion_preservation.get("samples") if isinstance(motion_preservation, Mapping) else None
        )
        parsed_samples: list[tuple[float, float]] = []
        if isinstance(samples, list):
            for sample in samples:
                if not isinstance(sample, Mapping):
                    parsed_samples = []
                    break
                source_time = _number(sample.get("source_time"))
                media_time = _number(sample.get("media_time"))
                if source_time is None or media_time is None:
                    parsed_samples = []
                    break
                parsed_samples.append((source_time, media_time))
        samples_valid = len(parsed_samples) >= 2
        if samples_valid and cover_start is not None and cover_end is not None:
            first_source, first_media = parsed_samples[0]
            previous_source, previous_media = first_source, first_media
            for source_time, media_time in parsed_samples[1:]:
                if (
                    source_time < previous_source
                    or media_time < previous_media
                    or abs((source_time - first_source) - (media_time - first_media))
                    > NUMBER_TOLERANCE
                ):
                    samples_valid = False
                    break
                previous_source, previous_media = source_time, media_time
            samples_valid = samples_valid and (
                abs(first_source - cover_start) <= NUMBER_TOLERANCE
                and abs(first_media) <= NUMBER_TOLERANCE
                and abs(parsed_samples[-1][0] - cover_end) <= NUMBER_TOLERANCE
                and abs(parsed_samples[-1][1] - (cover_end - cover_start)) <= NUMBER_TOLERANCE
            )
        else:
            samples_valid = False
        local_baked_path = evidence.get("local_baked_window_path")
        motion_preservation_invalid = (
            not isinstance(motion_preservation, Mapping)
            or not _status_passed(motion_preservation.get("status"))
            or str(motion_preservation.get("mode") or "").strip().casefold()
            != "source_synchronous_motion_preserving"
            or _sha256_file(motion_path) != motion_sha256
            or motion_source_start is None
            or motion_source_end is None
            or cover_start is None
            or cover_end is None
            or abs(motion_source_start - cover_start) > NUMBER_TOLERANCE
            or abs(motion_source_end - cover_end) > NUMBER_TOLERANCE
            or not samples_valid
            or (local_baked_path and not _same_path(local_baked_path, motion_path))
        )
        if (
            not _static_residual_cover_contract_valid(evidence, cover_start, cover_end)
            and motion_preservation_invalid
        ):
            problems.append("pointer_lifecycle.motion_preservation_required")

    if isinstance(handoff, Mapping) and isinstance(pointer_window, Mapping):
        first_visible = _number(handoff.get("first_visible"))
        overlay_end = pointer_end
        if (
            not _status_passed(handoff.get("status"))
            or first_visible is None
            or overlay_end is None
            or overlay_end > first_visible
        ):
            problems.append("pointer_lifecycle.source_pointer_handoff_collision")

    return sorted(set(problems))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _point(value: Any) -> tuple[float, float] | None:
    if isinstance(value, Mapping):
        pair = value.get("x"), value.get("y")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
        pair = value[0], value[1]
    else:
        return None
    x = _number(pair[0])
    y = _number(pair[1])
    return (x, y) if x is not None and y is not None else None


def _target_geometry(
    value: Any,
) -> tuple[float, float, float, float, float, float] | None:
    if not isinstance(value, Mapping):
        return None
    fields = ("x", "y", "width", "height", "canvas_width", "canvas_height")
    values = tuple(_number(value.get(field)) for field in fields)
    if any(item is None for item in values):
        return None
    x, y, width, height, canvas_width, canvas_height = values
    if width <= 0 or height <= 0 or canvas_width <= 0 or canvas_height <= 0:
        return None
    return x, y, width, height, canvas_width, canvas_height


def _canvas_size(content: Mapping[str, Any]) -> tuple[float, float] | None:
    raw = content.get("canvas_config")
    if raw is None:
        return DEFAULT_CANVAS_WIDTH, DEFAULT_CANVAS_HEIGHT
    if not isinstance(raw, Mapping):
        return None
    width = _number(raw.get("width"))
    height = _number(raw.get("height"))
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    return width, height


def _tracks(content: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = content.get("tracks")
    if not isinstance(raw, list):
        return []
    return [track for track in raw if isinstance(track, Mapping)]


def _segments(track: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = track.get("segments")
    if not isinstance(raw, list):
        return []
    return [segment for segment in raw if isinstance(segment, Mapping)]


def _find_track(content: Mapping[str, Any], track_name: str) -> list[Mapping[str, Any]]:
    return [track for track in _tracks(content) if track.get("name") == track_name]


def _find_segment(track: Mapping[str, Any], segment_id: str) -> list[Mapping[str, Any]]:
    return [segment for segment in _segments(track) if segment.get("id") == segment_id]


def _video_materials(content: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    materials = content.get("materials")
    if not isinstance(materials, Mapping):
        return []
    videos = materials.get("videos")
    if not isinstance(videos, list):
        return []
    return [material for material in videos if isinstance(material, Mapping)]


def _material_by_id(content: Mapping[str, Any], material_id: Any) -> Mapping[str, Any] | None:
    matches = [
        material for material in _video_materials(content) if material.get("id") == material_id
    ]
    if not matches:
        return None
    first = matches[0]
    if any(
        not _same_path(first.get("path"), material.get("path"))
        or str(first.get("type") or "").strip().casefold()
        != str(material.get("type") or "").strip().casefold()
        for material in matches[1:]
    ):
        return None
    return first


def _same_path(left: Any, right: Any) -> bool:
    if not isinstance(left, (str, os.PathLike)) or not isinstance(right, (str, os.PathLike)):
        return False
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def _decode_asset(path: Any) -> tuple[int, int, int] | None:
    if not isinstance(path, (str, os.PathLike)):
        return None
    decoded = cv2.imread(os.fspath(Path(path).expanduser()), cv2.IMREAD_UNCHANGED)
    if decoded is None or decoded.ndim not in (2, 3):
        return None
    height, width = decoded.shape[:2]
    channels = 1 if decoded.ndim == 2 else int(decoded.shape[2])
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height), channels


def _placement_policy_problems(
    evidence: Mapping[str, Any], receipt: Mapping[str, Any]
) -> list[str]:
    target_kind = evidence.get("target_kind")
    if not isinstance(target_kind, str) or not target_kind.strip():
        return ["pointer_geometry.target_kind_required"]
    target_kind = target_kind.strip()

    asset_role = receipt.get("asset_role")
    layout = receipt.get("scale_reference_layout")
    raw_policies = receipt.get("placement_policies")
    policies = raw_policies if isinstance(raw_policies, list) else []
    matching = [
        policy
        for policy in policies
        if isinstance(policy, Mapping)
        and policy.get("asset_role") == asset_role
        and policy.get("target_kind") == target_kind
        and policy.get("layout") == layout
    ]

    if len(matching) == 1:
        policy = matching[0]
        problems: list[str] = []
        if evidence.get("selected_placement_policy_id") != policy.get("policy_id"):
            problems.append("pointer_geometry.placement_policy_id_mismatch")
        if evidence.get("target_anchor") != policy.get("target_anchor"):
            problems.append("pointer_geometry.placement_policy_target_anchor_mismatch")
        actual_gap = _number(evidence.get("gap_px"))
        expected_gap = _number(policy.get("gap_px"))
        if (
            actual_gap is None
            or expected_gap is None
            or abs(actual_gap - expected_gap) > NUMBER_TOLERANCE
        ):
            problems.append("pointer_geometry.placement_policy_gap_mismatch")
        return problems

    required_target_kinds = receipt.get("placement_policy_target_kinds")
    required = (
        {value for value in required_target_kinds if isinstance(value, str) and value.strip()}
        if isinstance(required_target_kinds, list)
        else set()
    )
    not_required_reason = evidence.get("placement_policy_not_required_reason")
    if (
        not matching
        and evidence.get("placement_policy_status") == "not_required"
        and isinstance(not_required_reason, str)
        and bool(not_required_reason.strip())
        and target_kind not in required
    ):
        return []
    return ["pointer_geometry.placement_policy_not_required_unproven"]


def pointer_saved_geometry_problems(
    evidence: Mapping[str, Any],
    receipt: Mapping[str, Any],
    content: Mapping[str, Any],
) -> list[str]:
    """Return stable problem codes for one saved editable pointer segment."""

    if not isinstance(evidence, Mapping):
        return ["pointer_geometry.evidence_not_object"]
    if not isinstance(receipt, Mapping):
        return ["pointer_geometry.receipt_not_object"]
    if not isinstance(content, Mapping):
        return ["pointer_geometry.content_not_object"]

    problems: list[str] = []
    canvas = _canvas_size(content)
    if canvas is None:
        return ["pointer_geometry.canvas_invalid"]
    canvas_width, canvas_height = canvas
    problems.extend(_placement_policy_problems(evidence, receipt))

    track_name = evidence.get("track_name") or evidence.get("overlay_track")
    if not isinstance(track_name, str) or not track_name.strip():
        return ["pointer_geometry.track_name_required"]
    track_matches = _find_track(content, track_name)
    if not track_matches:
        return ["pointer_geometry.track_missing"]
    if len(track_matches) != 1:
        return ["pointer_geometry.track_ambiguous"]
    track = track_matches[0]

    segment_id = evidence.get("segment_id")
    if not isinstance(segment_id, str) or not segment_id.strip():
        return ["pointer_geometry.segment_id_required"]
    segment_matches = _find_segment(track, segment_id)
    if not segment_matches:
        return ["pointer_geometry.segment_missing"]
    if len(segment_matches) != 1:
        return ["pointer_geometry.segment_ambiguous"]
    segment = segment_matches[0]

    timeline_window = evidence.get("timeline_window")
    parsed_timeline_window: tuple[float, float] | None = None
    if (
        isinstance(timeline_window, Sequence)
        and not isinstance(timeline_window, (str, bytes))
        and len(timeline_window) == 2
    ):
        timeline_start = _number(timeline_window[0])
        timeline_end = _number(timeline_window[1])
        if (
            timeline_start is not None
            and timeline_end is not None
            and timeline_end > timeline_start
        ):
            parsed_timeline_window = timeline_start, timeline_end
    if parsed_timeline_window is None:
        if evidence.get("review_timestamp_role") is not None:
            problems.append("pointer_geometry.timeline_window_required")
    else:
        target_timerange = segment.get("target_timerange")
        saved_start = (
            _number(target_timerange.get("start", 0))
            if isinstance(target_timerange, Mapping)
            else None
        )
        saved_duration = (
            _number(target_timerange.get("duration"))
            if isinstance(target_timerange, Mapping)
            else None
        )
        expected_start = parsed_timeline_window[0] * 1_000_000
        expected_end = parsed_timeline_window[1] * 1_000_000
        if (
            saved_start is None
            or saved_duration is None
            or abs(saved_start - expected_start) > 500
            or abs(saved_start + saved_duration - expected_end) > 500
        ):
            problems.append("pointer_geometry.timeline_window_mismatch")

    if _number(segment.get("render_index")) is None:
        problems.append("pointer_geometry.render_index_missing")

    material = _material_by_id(content, segment.get("material_id"))
    if material is None:
        return sorted(set([*problems, "pointer_geometry.material_missing"]))
    asset_path = receipt.get("asset_path")
    if not _same_path(material.get("path"), asset_path):
        return sorted(set([*problems, "pointer_geometry.material_path_mismatch"]))
    decoded = _decode_asset(material.get("path"))
    if decoded is None:
        return sorted(set([*problems, "pointer_geometry.asset_decode_failed"]))
    asset_width, asset_height, channels = decoded
    if str(receipt.get("asset_role") or "").strip().lower() == "hand" and channels != 4:
        problems.append("pointer_geometry.asset_alpha_required")

    media_contract = receipt.get("media_contract")
    if isinstance(media_contract, Mapping):
        if (
            media_contract.get("format") != "png"
            or media_contract.get("has_alpha") is not True
            or media_contract.get("width") != asset_width
            or media_contract.get("height") != asset_height
        ):
            problems.append("pointer_geometry.asset_dimensions_mismatch")

    expected_scale = _number(receipt.get("visible_height_ratio"))
    receipt_width_ratio = _number(receipt.get("visible_width_ratio"))
    if expected_scale is None or expected_scale <= 0:
        return sorted(set([*problems, "pointer_geometry.visible_height_ratio_invalid"]))
    if receipt_width_ratio is None or receipt_width_ratio <= 0:
        problems.append("pointer_geometry.visible_width_ratio_invalid")
    else:
        expected_width_ratio = (
            expected_scale * (asset_width / asset_height) * (canvas_height / canvas_width)
        )
        if abs(receipt_width_ratio - expected_width_ratio) > NUMBER_TOLERANCE:
            problems.append("pointer_geometry.visible_width_ratio_mismatch")

    clip = segment.get("clip")
    if not isinstance(clip, Mapping):
        return sorted(set([*problems, "pointer_geometry.clip_missing"]))
    scale = clip.get("scale")
    if not isinstance(scale, Mapping):
        return sorted(set([*problems, "pointer_geometry.scale_missing"]))
    scale_x = _number(scale.get("x"))
    scale_y = _number(scale.get("y"))
    scale_problems: list[str] = []
    if scale_x is None:
        scale_problems.append("pointer_geometry.scale_x_missing")
    elif abs(scale_x - expected_scale) > NUMBER_TOLERANCE:
        scale_problems.append("pointer_geometry.scale_x_mismatch")
    if scale_y is None:
        scale_problems.append("pointer_geometry.scale_y_missing")
    elif abs(scale_y - expected_scale) > NUMBER_TOLERANCE:
        scale_problems.append("pointer_geometry.scale_y_mismatch")
    problems.extend(scale_problems)

    transform = clip.get("transform")
    if not isinstance(transform, Mapping):
        return sorted(set([*problems, "pointer_geometry.transform_missing"]))
    transform_x = _number(transform.get("x"))
    transform_y = _number(transform.get("y"))
    if transform_x is None or transform_y is None:
        return sorted(set([*problems, "pointer_geometry.transform_invalid"]))

    anchor = _point(receipt.get("anchor"))
    if anchor is None or not all(0 <= coordinate <= 1 for coordinate in anchor):
        return sorted(set([*problems, "pointer_geometry.anchor_invalid"]))
    target_point = _point(evidence.get("target_point"))
    if target_point is None:
        return sorted(set([*problems, "pointer_geometry.target_point_invalid"]))
    if not (0 <= target_point[0] <= canvas_width and 0 <= target_point[1] <= canvas_height):
        problems.append("pointer_geometry.target_point_out_of_canvas")
    max_error = _number(evidence.get("max_landing_error_px", 2.0))
    if max_error is None or max_error < 0:
        return sorted(set([*problems, "pointer_geometry.landing_tolerance_invalid"]))

    target_kind = str(evidence.get("target_kind") or "").strip()
    matching_policies = [
        policy
        for policy in receipt.get("placement_policies") or []
        if isinstance(policy, Mapping)
        and policy.get("asset_role") == receipt.get("asset_role")
        and policy.get("target_kind") == target_kind
        and policy.get("layout") == receipt.get("scale_reference_layout")
    ]
    if len(matching_policies) == 1 and matching_policies[0].get("target_anchor") == "text_bottom":
        geometry = _target_geometry(evidence.get("target_geometry"))
        if geometry is None:
            problems.append("pointer_geometry.target_geometry_required")
        else:
            x, y, width, height, geometry_canvas_width, geometry_canvas_height = geometry
            if (
                abs(geometry_canvas_width - canvas_width) > NUMBER_TOLERANCE
                or abs(geometry_canvas_height - canvas_height) > NUMBER_TOLERANCE
            ):
                problems.append("pointer_geometry.target_geometry_canvas_mismatch")
            if (
                x < 0
                or y < 0
                or x + width > geometry_canvas_width
                or y + height > geometry_canvas_height
            ):
                problems.append("pointer_geometry.target_geometry_out_of_canvas")
            policy_gap = _number(matching_policies[0].get("gap_px"))
            if policy_gap is not None:
                policy_target = (x + width / 2.0, y + height + policy_gap)
                if math.dist(policy_target, target_point) > NUMBER_TOLERANCE:
                    problems.append("pointer_geometry.placement_policy_target_point_mismatch")

    if not scale_problems and scale_x is not None and scale_y is not None:
        display_width = canvas_height * scale_x * (asset_width / asset_height)
        display_height = canvas_height * scale_y
        center_x = canvas_width / 2 + transform_x * (canvas_width / 2)
        center_y = canvas_height / 2 - transform_y * (canvas_height / 2)
        hotspot_x = center_x + (anchor[0] - 0.5) * display_width
        hotspot_y = center_y + (anchor[1] - 0.5) * display_height
        landing_error = math.hypot(hotspot_x - target_point[0], hotspot_y - target_point[1])
        if landing_error > max_error + NUMBER_TOLERANCE:
            problems.append("pointer_geometry.hotspot_miss")

    return sorted(set(problems))


def _timerange(segment: Mapping[str, Any]) -> tuple[float, float] | None:
    raw = segment.get("target_timerange")
    if not isinstance(raw, Mapping):
        return None
    start = _number(raw.get("start", 0))
    duration = _number(raw.get("duration"))
    if start is None or duration is None or duration <= 0:
        return None
    return start, start + duration


def _overlaps(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_range = _timerange(left)
    right_range = _timerange(right)
    if left_range is None or right_range is None:
        return False
    return left_range[0] < right_range[1] and right_range[0] < left_range[1]


def _saved_source_time_to_timeline(
    content: Mapping[str, Any],
    *,
    track_name: Any,
    material_id: Any,
    source_time: float,
) -> float | None:
    if (
        not isinstance(track_name, str)
        or not track_name.strip()
        or not isinstance(material_id, str)
        or not material_id.strip()
    ):
        return None
    tracks = _find_track(content, track_name)
    if len(tracks) != 1:
        return None
    source_time_us = source_time * 1_000_000.0
    candidates: list[float] = []
    for segment in _segments(tracks[0]):
        if segment.get("material_id") != material_id:
            continue
        source_timerange = segment.get("source_timerange")
        target_timerange = segment.get("target_timerange")
        if not isinstance(source_timerange, Mapping) or not isinstance(target_timerange, Mapping):
            continue
        source_start = _number(source_timerange.get("start", 0))
        source_duration = _number(source_timerange.get("duration"))
        target_start = _number(target_timerange.get("start", 0))
        target_duration = _number(target_timerange.get("duration"))
        if (
            source_start is None
            or source_duration is None
            or source_duration <= 0
            or target_start is None
            or target_duration is None
            or target_duration <= 0
            or source_time_us < source_start - 500
            or source_time_us > source_start + source_duration + 500
        ):
            continue
        source_offset = min(max(source_time_us - source_start, 0.0), source_duration)
        candidates.append(target_start + source_offset * target_duration / source_duration)
    if not candidates or max(candidates) - min(candidates) > 500:
        return None
    return candidates[0] / 1_000_000.0


def _sha256_file(path: Any) -> str | None:
    if not isinstance(path, (str, os.PathLike)):
        return None
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _file_size(path: Any) -> int | None:
    if not isinstance(path, (str, os.PathLike)):
        return None
    try:
        candidate = Path(path).expanduser()
        return candidate.stat().st_size if candidate.is_file() else None
    except OSError:
        return None


def _material_matches_pointer_identity(
    material: Mapping[str, Any],
    *,
    pointer_paths: Sequence[str],
    pointer_hashes: set[str],
    pointer_sizes: set[int],
) -> bool:
    path = material.get("path")
    if any(_same_path(path, pointer_path) for pointer_path in pointer_paths):
        return True
    size = _file_size(path)
    if size is None or (pointer_sizes and size not in pointer_sizes):
        return False
    digest = _sha256_file(path)
    return digest is not None and digest in pointer_hashes


def _explicit_layer_segments(
    evidence: Mapping[str, Any], field: str, content: Mapping[str, Any]
) -> tuple[list[Mapping[str, Any]], list[str]]:
    layer_kind = field.removesuffix("_layers")
    raw_references = evidence.get(field)
    if field not in evidence:
        return [], [f"pointer_layers.{field}_required"]
    if raw_references is None:
        return [], [f"pointer_layers.{layer_kind}_reference_invalid"]
    if not isinstance(raw_references, list):
        return [], [f"pointer_layers.{layer_kind}_reference_invalid"]
    resolved: list[Mapping[str, Any]] = []
    problems: list[str] = []
    for reference in raw_references:
        if not isinstance(reference, Mapping):
            problems.append(f"pointer_layers.{layer_kind}_reference_invalid")
            continue
        track_name = reference.get("track_name")
        segment_id = reference.get("segment_id")
        if (
            not isinstance(track_name, str)
            or not track_name.strip()
            or not isinstance(segment_id, str)
            or not segment_id.strip()
        ):
            problems.append(f"pointer_layers.{layer_kind}_reference_invalid")
            continue
        tracks = _find_track(content, track_name)
        if not tracks:
            problems.append(f"pointer_layers.{layer_kind}_track_missing")
            continue
        if len(tracks) != 1:
            problems.append(f"pointer_layers.{layer_kind}_track_ambiguous")
            continue
        segments = _find_segment(tracks[0], segment_id)
        if not segments:
            problems.append(f"pointer_layers.{layer_kind}_segment_missing")
            continue
        if len(segments) != 1:
            problems.append(f"pointer_layers.{layer_kind}_segment_ambiguous")
            continue
        resolved.append(segments[0])
    return resolved, problems


def _display_safe_contract_is_valid(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    status = str(value.get("status") or "").strip().lower()
    return (
        status in PASS_STATUSES
        and value.get("opened_draft_drift") is True
        and isinstance(value.get("track_name"), str)
        and bool(value["track_name"].strip())
        and isinstance(value.get("segment_id"), str)
        and bool(value["segment_id"].strip())
        and isinstance(value.get("asset_path"), str)
        and bool(value["asset_path"].strip())
        and isinstance(value.get("opened_draft_drift_artifact_sha256"), str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", value["opened_draft_drift_artifact_sha256"])
        is not None
        and isinstance(value.get("opened_draft_drift_timeline_id"), str)
        and bool(value["opened_draft_drift_timeline_id"].strip())
        and isinstance(value.get("opened_draft_drift_window"), Mapping)
    )


def _read_video_size(path: Path) -> tuple[int, int] | None:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return None
        ok, frame = capture.read()
        if not ok or frame is None or frame.ndim < 2:
            return None
        height, width = frame.shape[:2]
        return (int(width), int(height)) if width > 0 and height > 0 else None
    finally:
        capture.release()


def _video_has_audio(path: Path) -> bool | None:
    try:
        tracks = MediaInfo.parse(str(path)).tracks
    except (OSError, RuntimeError, ValueError):
        return None
    return any(str(getattr(track, "track_type", "")).casefold() == "audio" for track in tracks)


def _decodable_video_frame_count(path: Path) -> int | None:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return None
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        ok, first_frame = capture.read()
        if not ok or first_frame is None or frame_count < 2:
            return None
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_count - 1)
        ok, last_frame = capture.read()
        if not ok or last_frame is None:
            return None
        return frame_count
    finally:
        capture.release()


def _video_duration_seconds(path: Path) -> float | None:
    try:
        tracks = MediaInfo.parse(str(path)).tracks
    except (OSError, RuntimeError, ValueError):
        tracks = []
    for track in tracks:
        if str(getattr(track, "track_type", "")).casefold() != "video":
            continue
        try:
            duration_ms = float(getattr(track, "duration", 0.0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration_ms) and duration_ms > 0:
            return duration_ms / 1000.0

    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return None
        frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(frame_count) or not math.isfinite(fps) or frame_count <= 0 or fps <= 0:
            return None
        return frame_count / fps
    finally:
        capture.release()


def _frames_have_motion(left: Any, right: Any) -> bool:
    if (
        left is None
        or right is None
        or getattr(left, "shape", None) != getattr(right, "shape", None)
        or not getattr(left, "size", 0)
    ):
        return False
    difference = cv2.absdiff(left, right)
    mean_delta = float(difference.mean())
    changed = cv2.threshold(difference, 3, 255, cv2.THRESH_BINARY)[1]
    changed_ratio = cv2.countNonZero(changed) / float(changed.size)
    return (
        mean_delta >= CLEAN_MOTION_MIN_MEAN_DELTA or changed_ratio >= CLEAN_MOTION_MIN_CHANGED_RATIO
    )


def _sampled_video_frames(path: Path) -> list[tuple[float, Any]] | None:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return None
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if frame_count < 2 or not math.isfinite(fps) or fps <= 0:
            return None
        sample_indices = sorted({0, frame_count // 2, frame_count - 1})
        frames: list[tuple[float, Any]] = []
        for frame_index in sample_indices:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok or frame is None:
                return None
            frames.append((frame_index / fps, frame))
        return frames
    finally:
        capture.release()


def _video_has_sampled_motion(path: Path) -> bool | None:
    sampled = _sampled_video_frames(path)
    if sampled is None:
        return None
    frames = [
        cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        for _media_time, frame in sampled
    ]
    for left_index, left in enumerate(frames):
        for right in frames[left_index + 1 :]:
            if _frames_have_motion(left, right):
                return True
    return False


def _read_video_frame_at(path: Path, time_seconds: float) -> Any | None:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened() or time_seconds < 0:
            return None
        capture.set(cv2.CAP_PROP_POS_MSEC, time_seconds * 1000.0)
        ok, frame = capture.read()
        return frame if ok and frame is not None else None
    finally:
        capture.release()


def _decode_video_window(
    path: Path, start_seconds: float, end_seconds: float
) -> Iterator[tuple[float, Any]]:
    """Decode every source frame in a half-open time window in presentation order."""

    def frames() -> Iterator[tuple[float, Any]]:
        if start_seconds < 0 or end_seconds <= start_seconds:
            raise ValueError("invalid source window")
        try:
            container = av.open(str(path))
        except (av.error.FFmpegError, OSError, ValueError) as exc:
            raise ValueError("source video is not decodable") from exc
        with container:
            if not container.streams.video:
                raise ValueError("source video has no video stream")
            stream = container.streams.video[0]
            time_base = float(stream.time_base)
            if not math.isfinite(time_base) or time_base <= 0:
                raise ValueError("source video has no usable time base")
            stream_start = int(stream.start_time or 0)
            seek_seconds = max(0.0, start_seconds - 1.0)
            try:
                container.seek(
                    stream_start + int(seek_seconds / time_base),
                    stream=stream,
                    any_frame=False,
                    backward=True,
                )
                saw_frame_before_start = start_seconds <= NUMBER_TOLERANCE
                reached_end_boundary = False
                previous_time: float | None = None
                yielded = 0
                for frame in container.decode(stream):
                    if frame.pts is None:
                        continue
                    frame_time = float((frame.pts - stream_start) * stream.time_base)
                    if not math.isfinite(frame_time):
                        raise ValueError("source frame has invalid PTS")
                    if previous_time is not None and frame_time <= previous_time + NUMBER_TOLERANCE:
                        raise ValueError("source frame PTS is not strictly increasing")
                    previous_time = frame_time
                    if frame_time < start_seconds:
                        saw_frame_before_start = True
                        continue
                    if not saw_frame_before_start:
                        raise ValueError("source window start boundary is not proven")
                    if frame_time >= end_seconds:
                        reached_end_boundary = True
                        break
                    yielded += 1
                    yield frame_time, frame.to_ndarray(format="bgr24")
            except av.error.FFmpegError as exc:
                raise ValueError("source video decode failed") from exc
            if yielded == 0 or not reached_end_boundary:
                raise ValueError("source window end boundary is not proven")

    return frames()


def _comparison_exclusion_regions(
    value: Any, width: int, height: int
) -> list[tuple[int, int, int, int]] | None:
    if not isinstance(value, list) or not value:
        return None
    regions: list[tuple[int, int, int, int]] = []
    for region in value:
        if not isinstance(region, Mapping):
            return None
        x = _number(region.get("x"))
        y = _number(region.get("y"))
        region_width = _number(region.get("width"))
        region_height = _number(region.get("height"))
        if (
            x is None
            or y is None
            or region_width is None
            or region_height is None
            or x < 0
            or y < 0
            or region_width <= 0
            or region_height <= 0
            or x + region_width > width
            or y + region_height > height
        ):
            return None
        regions.append(
            (
                int(round(x)),
                int(round(y)),
                int(round(x + region_width)),
                int(round(y + region_height)),
            )
        )
    return regions


def _source_frame_corresponds(
    clean_frame: Any,
    source_frame: Any,
    exclusion_regions: Sequence[tuple[int, int, int, int]],
) -> bool:
    if (
        clean_frame is None
        or source_frame is None
        or getattr(clean_frame, "shape", None) != getattr(source_frame, "shape", None)
        or clean_frame.ndim != 3
    ):
        return False
    height, width = clean_frame.shape[:2]
    comparison_mask = np.ones((height, width), dtype=np.uint8)
    for x1, y1, x2, y2 in exclusion_regions:
        comparison_mask[y1:y2, x1:x2] = 0
    if cv2.countNonZero(comparison_mask) == 0:
        return False
    difference = cv2.absdiff(clean_frame, source_frame)
    compared_values = difference[comparison_mask.astype(bool)]
    mean_delta = float(compared_values.mean())
    changed_ratio = float(np.mean(compared_values > CLEAN_SOURCE_CHANGED_PIXEL_DELTA))
    return (
        mean_delta <= CLEAN_SOURCE_MAX_MEAN_DELTA
        and changed_ratio <= CLEAN_SOURCE_MAX_CHANGED_RATIO
    )


def _regions_mask(regions: Sequence[tuple[int, int, int, int]], width: int, height: int) -> Any:
    mask = np.zeros((height, width), dtype=np.uint8)
    for x1, y1, x2, y2 in regions:
        mask[y1:y2, x1:x2] = 1
    return mask


def _frames_match_in_mask(left: Any, right: Any, mask: Any) -> bool:
    if (
        left is None
        or right is None
        or getattr(left, "shape", None) != getattr(right, "shape", None)
        or left.ndim != 3
        or getattr(mask, "shape", None) != left.shape[:2]
        or cv2.countNonZero(mask) == 0
    ):
        return False
    difference = cv2.absdiff(left[:, :, :3], right[:, :, :3])
    compared = difference[mask.astype(bool)]
    mean_delta = float(compared.mean())
    changed_ratio = float(np.mean(compared > POINTER_COVER_CHANGED_PIXEL_DELTA))
    return (
        mean_delta <= POINTER_COVER_BACKGROUND_MAX_MEAN_DELTA
        and changed_ratio <= POINTER_COVER_BACKGROUND_MAX_CHANGED_RATIO
    )


def _read_hashed_color_image(record: Mapping[str, Any], path_key: str, hash_key: str) -> Any:
    path_value = record.get(path_key)
    expected_hash = str(record.get(hash_key) or "").strip().lower()
    if _sha256_file(path_value) != expected_hash:
        return None
    return cv2.imread(os.fspath(Path(os.fspath(path_value)).expanduser()), cv2.IMREAD_COLOR)


def _saved_residual_cover_layer_problems(
    evidence: Mapping[str, Any],
    cover: Mapping[str, Any],
    content: Mapping[str, Any],
) -> list[str]:
    cover_track_name = cover.get("track_name")
    cover_segment_id = cover.get("segment_id")
    if not isinstance(cover_track_name, str) or not isinstance(cover_segment_id, str):
        return ["pointer_cover.saved_material_mismatch"]
    cover_tracks = _find_track(content, cover_track_name)
    if len(cover_tracks) != 1:
        return ["pointer_cover.saved_material_mismatch"]
    cover_segments = _find_segment(cover_tracks[0], cover_segment_id)
    if len(cover_segments) != 1:
        return ["pointer_cover.saved_material_mismatch"]
    cover_segment = cover_segments[0]
    cover_material = _material_by_id(content, cover_segment.get("material_id"))
    if (
        cover_material is None
        or not _same_path(cover_material.get("path"), cover.get("asset_path"))
        or _sha256_file(cover_material.get("path"))
        != str(cover.get("asset_sha256") or "").strip().lower()
        or str(cover_material.get("type") or "").strip().casefold() not in {"photo", "image"}
    ):
        return ["pointer_cover.saved_material_mismatch"]

    problems: list[str] = []
    window = cover.get("timeline_window")
    expected_start = _number(window.get("start")) if isinstance(window, Mapping) else None
    expected_duration = _number(window.get("duration")) if isinstance(window, Mapping) else None
    saved_range = _timerange(cover_segment)
    if (
        expected_start is None
        or expected_duration is None
        or expected_duration <= 0
        or saved_range is None
        or abs(saved_range[0] - expected_start * 1_000_000) > 500
        or abs(saved_range[1] - (expected_start + expected_duration) * 1_000_000) > 500
    ):
        problems.append("pointer_cover.saved_window_mismatch")

    clip = cover_segment.get("clip")
    scale = clip.get("scale") if isinstance(clip, Mapping) else None
    transform = clip.get("transform") if isinstance(clip, Mapping) else None
    if (
        not isinstance(scale, Mapping)
        or not isinstance(transform, Mapping)
        or any(
            value is None or abs(value - expected) > NUMBER_TOLERANCE
            for value, expected in (
                (_number(scale.get("x")), 1.0),
                (_number(scale.get("y")), 1.0),
                (_number(transform.get("x")), 0.0),
                (_number(transform.get("y")), 0.0),
            )
        )
        or _number(cover_segment.get("volume")) != 0.0
    ):
        problems.append("pointer_cover.saved_material_mismatch")

    pointer_track_name = evidence.get("track_name") or evidence.get("overlay_track")
    pointer_segment_id = evidence.get("segment_id")
    pointer_tracks = (
        _find_track(content, pointer_track_name) if isinstance(pointer_track_name, str) else []
    )
    pointer_segments = (
        _find_segment(pointer_tracks[0], pointer_segment_id)
        if len(pointer_tracks) == 1 and isinstance(pointer_segment_id, str)
        else []
    )
    pointer_segment = pointer_segments[0] if len(pointer_segments) == 1 else None
    pointer_material = (
        _material_by_id(content, pointer_segment.get("material_id"))
        if pointer_segment is not None
        else None
    )
    pointer_decoded = (
        cv2.imread(str(pointer_material.get("path")), cv2.IMREAD_UNCHANGED)
        if pointer_material is not None
        else None
    )
    if (
        pointer_segment is None
        or pointer_material is None
        or pointer_segment.get("material_id") == cover_segment.get("material_id")
        or _same_path(pointer_material.get("path"), cover_material.get("path"))
        or pointer_decoded is None
        or pointer_decoded.ndim != 3
        or pointer_decoded.shape[2] != 4
        or not _overlaps(pointer_segment, cover_segment)
    ):
        problems.append("pointer_cover.pointer_not_independently_editable")

    source_track_name = cover.get("source_track_name")
    source_material_id = cover.get("source_material_id")
    source_tracks = (
        _find_track(content, source_track_name) if isinstance(source_track_name, str) else []
    )
    source_segments = (
        [
            segment
            for segment in _segments(source_tracks[0])
            if segment.get("material_id") == source_material_id
            and _overlaps(segment, cover_segment)
        ]
        if len(source_tracks) == 1
        else []
    )
    pointer_index = _number(pointer_segment.get("render_index")) if pointer_segment else None
    cover_index = _number(cover_segment.get("render_index"))
    source_indexes = [
        0.0 if "render_index" not in segment else _number(segment.get("render_index"))
        for segment in source_segments
    ]
    if (
        pointer_index is None
        or cover_index is None
        or not source_segments
        or any(index is None for index in source_indexes)
        or pointer_index <= cover_index
        or any(cover_index <= index for index in source_indexes if index is not None)
    ):
        problems.append("pointer_cover.render_order_invalid")
    return problems


def _residual_cover_source_material(
    cover: Mapping[str, Any], content: Mapping[str, Any]
) -> tuple[list[str], Path | None]:
    track_name = cover.get("source_track_name")
    material_id = cover.get("source_material_id")
    source_path_value = cover.get("source_media_path")
    tracks = _find_track(content, track_name) if isinstance(track_name, str) else []
    material = _material_by_id(content, material_id)
    material_is_used = len(tracks) == 1 and any(
        segment.get("material_id") == material_id for segment in _segments(tracks[0])
    )
    if (
        material is None
        or not material_is_used
        or not _same_path(source_path_value, material.get("path"))
    ):
        return ["pointer_cover.source_material_binding_mismatch"], None
    source_path = Path(os.fspath(source_path_value)).expanduser()
    expected_hash = str(cover.get("source_media_sha256") or "").strip().lower()
    if _sha256_file(source_path) != expected_hash:
        return ["pointer_cover.source_media_hash_mismatch"], None
    return [], source_path


def _saved_pointer_segment_and_rgba(
    evidence: Mapping[str, Any], content: Mapping[str, Any]
) -> tuple[Mapping[str, Any] | None, Any]:
    track_name = evidence.get("track_name") or evidence.get("overlay_track")
    segment_id = evidence.get("segment_id")
    tracks = _find_track(content, track_name) if isinstance(track_name, str) else []
    segments = (
        _find_segment(tracks[0], segment_id)
        if len(tracks) == 1 and isinstance(segment_id, str)
        else []
    )
    segment = segments[0] if len(segments) == 1 else None
    material = _material_by_id(content, segment.get("material_id")) if segment is not None else None
    rgba = (
        cv2.imread(str(material.get("path")), cv2.IMREAD_UNCHANGED)
        if material is not None
        else None
    )
    if rgba is None or rgba.ndim != 3 or rgba.shape[2] != 4:
        rgba = None
    return segment, rgba


def _rectangle_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[int, int, int, int],
) -> bool:
    outer_x, outer_y, outer_width, outer_height = outer
    inner_x, inner_y, inner_width, inner_height = inner
    return bool(
        inner_x >= outer_x - NUMBER_TOLERANCE
        and inner_y >= outer_y - NUMBER_TOLERANCE
        and inner_x + inner_width <= outer_x + outer_width + NUMBER_TOLERANCE
        and inner_y + inner_height <= outer_y + outer_height + NUMBER_TOLERANCE
    )


def _clean_frame_retains_source_pointer(
    source_frame: Any,
    clean_frame: Any,
    pointer_rgba: Any,
    region: tuple[int, int, int, int],
) -> bool:
    x, y, width, height = region
    if (
        source_frame is None
        or clean_frame is None
        or pointer_rgba is None
        or width <= 2
        or height <= 2
        or x < 0
        or y < 0
        or x + width > source_frame.shape[1]
        or y + height > source_frame.shape[0]
    ):
        return False
    template = cv2.resize(pointer_rgba, (width, height), interpolation=cv2.INTER_AREA)
    alpha = template[:, :, 3] > 32
    if cv2.countNonZero(alpha.astype(np.uint8)) < 4:
        return False
    source_crop = source_frame[y : y + height, x : x + width]
    clean_crop = clean_frame[y : y + height, x : x + width]
    source_template_delta = np.max(cv2.absdiff(source_crop, template[:, :, :3]), axis=2)
    source_clean_delta = np.max(cv2.absdiff(source_crop, clean_crop), axis=2)
    retained = (
        alpha
        & (source_template_delta <= 48)
        & (source_clean_delta <= POINTER_COVER_CHANGED_PIXEL_DELTA)
    )
    alpha_count = int(cv2.countNonZero(alpha.astype(np.uint8)))
    retained_count = int(cv2.countNonZero(retained.astype(np.uint8)))
    if retained_count < max(6, int(round(alpha_count * 0.01))):
        return False
    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        retained.astype(np.uint8)
    )
    largest = max(
        (int(stats[index, cv2.CC_STAT_AREA]) for index in range(1, component_count)),
        default=0,
    )
    return largest >= max(8, int(round(alpha_count * 0.008)))


def _receipt_pointer_regions(
    frame: Any,
    pointer_rgba: Any,
    pointer_segment: Mapping[str, Any],
) -> list[tuple[int, int, int, int]] | None:
    clip = pointer_segment.get("clip")
    scale = clip.get("scale") if isinstance(clip, Mapping) else None
    scale_y = _number(scale.get("y")) if isinstance(scale, Mapping) else None
    if (
        frame is None
        or pointer_rgba is None
        or pointer_rgba.ndim != 3
        or pointer_rgba.shape[2] != 4
        or scale_y is None
        or scale_y <= 0
    ):
        return None
    original_height, original_width = frame.shape[:2]
    scan_scale = 0.5 if original_width >= 1000 else 1.0
    scan = (
        cv2.resize(
            frame,
            (
                int(round(original_width * scan_scale)),
                int(round(original_height * scan_scale)),
            ),
            interpolation=cv2.INTER_AREA,
        )
        if scan_scale < 1.0
        else frame
    )
    expected_height = int(round(original_height * scale_y * scan_scale))
    aspect = pointer_rgba.shape[1] / pointer_rgba.shape[0]
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for height_delta in (-1, 0, 1):
        template_height = expected_height + height_delta
        template_width = int(round(template_height * aspect))
        if (
            template_height <= 2
            or template_width <= 2
            or template_height > scan.shape[0]
            or template_width > scan.shape[1]
        ):
            continue
        template = cv2.resize(
            pointer_rgba,
            (template_width, template_height),
            interpolation=cv2.INTER_AREA,
        )
        mask = (template[:, :, 3] > 32).astype(np.uint8) * 255
        if cv2.countNonZero(mask) < 4:
            continue
        result = cv2.matchTemplate(
            scan,
            template[:, :, :3],
            cv2.TM_SQDIFF_NORMED,
            mask=mask,
        )
        valid = np.isfinite(result) & (result <= POINTER_COVER_OPENED_TEMPLATE_MAX_SQDIFF)
        component_count, labels, _stats, _centroids = cv2.connectedComponentsWithStats(
            valid.astype(np.uint8)
        )
        for component in range(1, component_count):
            ys, xs = np.where(labels == component)
            if not len(xs):
                continue
            best_index = int(np.argmin(result[ys, xs]))
            left = int(xs[best_index])
            top = int(ys[best_index])
            region = (
                int(round(left / scan_scale)),
                int(round(top / scan_scale)),
                int(round(template_width / scan_scale)),
                int(round(template_height / scan_scale)),
            )
            candidates.append((float(result[top, left]), region))
    clustered: list[tuple[float, tuple[int, int, int, int]]] = []
    for score, region in sorted(candidates):
        center = (region[0] + region[2] / 2.0, region[1] + region[3] / 2.0)
        if any(
            math.dist(
                center,
                (
                    kept[1][0] + kept[1][2] / 2.0,
                    kept[1][1] + kept[1][3] / 2.0,
                ),
            )
            <= max(3.0, min(region[2], region[3]) * 0.4)
            for kept in clustered
        ):
            continue
        clustered.append((score, region))
    return [region for _score, region in clustered]


def _trajectory_pointer_analysis(
    cover: Mapping[str, Any],
    pointer_segment: Mapping[str, Any] | None,
    pointer_rgba: Any,
) -> tuple[list[str], list[tuple[float, tuple[int, int, int, int]]], float | None]:
    receipt = cover.get("trajectory_receipt")
    frames = receipt.get("frames") if isinstance(receipt, Mapping) else None
    interval = (
        _number(receipt.get("frame_interval_seconds")) if isinstance(receipt, Mapping) else None
    )
    if pointer_segment is None or pointer_rgba is None:
        return ["pointer_cover.trajectory_pointer_template_unavailable"], [], interval
    if not isinstance(frames, list):
        return [], [], interval
    trajectory = _positive_rectangle(cover.get("trajectory_bounds"))
    problems: list[str] = []
    detections: list[tuple[float, tuple[int, int, int, int]]] = []
    for row in frames:
        if not isinstance(row, Mapping):
            continue
        source_time = _number(row.get("source_time"))
        source_frame = _read_hashed_color_image(row, "source_frame_path", "source_frame_sha256")
        clean_frame = _read_hashed_color_image(row, "clean_frame_path", "clean_frame_sha256")
        if source_time is None or source_frame is None or clean_frame is None:
            continue
        source_regions = _receipt_pointer_regions(source_frame, pointer_rgba, pointer_segment)
        clean_regions = _receipt_pointer_regions(clean_frame, pointer_rgba, pointer_segment)
        if clean_regions:
            problems.append("pointer_cover.clean_frame_recorded_pointer_present")
        for region in source_regions or []:
            detections.append((source_time, region))
            if trajectory is None or not _rectangle_contains(trajectory, region):
                problems.append("pointer_cover.trajectory_bounds_miss_detected_pointer")
            if _clean_frame_retains_source_pointer(source_frame, clean_frame, pointer_rgba, region):
                problems.append("pointer_cover.clean_frame_recorded_pointer_present")
    if frames and not detections:
        problems.append("pointer_cover.source_pointer_trajectory_unverifiable")
    return sorted(set(problems)), detections, interval


def _trajectory_required_sample_times(
    detections: Sequence[tuple[float, tuple[int, int, int, int]]],
) -> list[float]:
    if not detections:
        return []
    ordered = sorted(detections, key=lambda row: row[0])
    required: set[float] = {ordered[0][0], ordered[-1][0]}
    extrema = (
        min(ordered, key=lambda row: row[1][0]),
        max(ordered, key=lambda row: row[1][0] + row[1][2]),
        min(ordered, key=lambda row: row[1][1]),
        max(ordered, key=lambda row: row[1][1] + row[1][3]),
    )
    required.update(row[0] for row in extrema)

    step = max(
        4.0,
        max(max(region[2], region[3]) for _source_time, region in ordered) * 0.15,
    )
    points = [
        (source_time, (region[0] + region[2] / 2.0, region[1] + region[3] / 2.0))
        for source_time, region in ordered
    ]

    def simplify(
        rows: list[tuple[float, tuple[float, float]]],
    ) -> list[tuple[float, tuple[float, float]]]:
        if len(rows) <= 2:
            return rows
        start = rows[0][1]
        end = rows[-1][1]
        line = (end[0] - start[0], end[1] - start[1])
        line_length = math.hypot(*line)
        distances = []
        for _source_time, point in rows[1:-1]:
            if line_length <= NUMBER_TOLERANCE:
                distance = math.dist(point, start)
            else:
                distance = (
                    abs(line[0] * (start[1] - point[1]) - (start[0] - point[0]) * line[1])
                    / line_length
                )
            distances.append(distance)
        maximum = max(distances, default=0.0)
        if maximum < step * 0.5:
            return [rows[0], rows[-1]]
        split = distances.index(maximum) + 1
        left = simplify(rows[: split + 1])
        right = simplify(rows[split:])
        return left[:-1] + right

    significant = simplify(points)
    for index in range(1, len(significant) - 1):
        previous = significant[index - 1]
        current = significant[index]
        following = significant[index + 1]
        incoming = (
            current[1][0] - previous[1][0],
            current[1][1] - previous[1][1],
        )
        outgoing = (
            following[1][0] - current[1][0],
            following[1][1] - current[1][1],
        )
        incoming_length = math.hypot(*incoming)
        outgoing_length = math.hypot(*outgoing)
        if incoming_length < step or outgoing_length < step:
            continue
        cosine = max(
            -1.0,
            min(
                1.0,
                (incoming[0] * outgoing[0] + incoming[1] * outgoing[1])
                / (incoming_length * outgoing_length),
            ),
        )
        if math.degrees(math.acos(cosine)) >= 45.0:
            required.add(current[0])
    return sorted(required)


def _opened_trajectory_sample_problems(
    detections: Sequence[tuple[float, tuple[int, int, int, int]]],
    opened_samples: Sequence[Mapping[str, Any]],
    frame_interval: float | None,
) -> list[str]:
    required_times = _trajectory_required_sample_times(detections)
    if not required_times:
        return []
    sample_times = [
        source_time
        for sample in opened_samples
        if (source_time := _number(sample.get("source_time"))) is not None
    ]
    tolerance = (
        frame_interval * 1.001
        if frame_interval is not None and frame_interval > 0
        else POINTER_COVER_OPENED_TIME_TOLERANCE_SECONDS
    )
    if any(
        not any(abs(sample_time - required_time) <= tolerance for sample_time in sample_times)
        for required_time in required_times
    ):
        return ["pointer_cover.opened_jianying_trajectory_samples_incomplete"]
    return []


def _trajectory_receipt_problems(
    cover: Mapping[str, Any],
    source_path: Path | None,
    width: int,
    height: int,
) -> list[str]:
    receipt = cover.get("trajectory_receipt")
    if (
        source_path is None
        or not isinstance(receipt, Mapping)
        or not _status_passed(receipt.get("status"))
        or receipt.get("method") != "source_clean_frame_sequence_v1"
        or not _same_path(receipt.get("source_media_path"), source_path)
        or str(receipt.get("source_media_sha256") or "").strip().lower()
        != str(cover.get("source_media_sha256") or "").strip().lower()
    ):
        return ["pointer_cover.trajectory_receipt_required"]
    source_window = cover.get("source_window")
    source_start = (
        _number(source_window.get("start")) if isinstance(source_window, Mapping) else None
    )
    source_end = _number(source_window.get("end")) if isinstance(source_window, Mapping) else None
    interval = _number(receipt.get("frame_interval_seconds"))
    declared_count = _number(receipt.get("frame_count"))
    frames = receipt.get("frames")
    if (
        source_start is None
        or source_end is None
        or source_end <= source_start
        or interval is None
        or interval <= 0
        or declared_count is None
        or abs(declared_count - round(declared_count)) > NUMBER_TOLERANCE
        or not isinstance(frames, list)
        or len(frames) < 3
        or len(frames) != int(round(declared_count))
    ):
        return ["pointer_cover.trajectory_receipt_not_frame_complete"]

    decoded_frames = iter(_decode_video_window(source_path, source_start, source_end))
    decoded_intervals: list[float] = []
    previous_decoded_time: float | None = None
    union = np.zeros((height, width), dtype=np.uint8)
    cover_rgba = cv2.imread(str(cover.get("asset_path")), cv2.IMREAD_UNCHANGED)
    cover_regions = _comparison_exclusion_regions(cover.get("opaque_regions"), width, height)
    cover_mask = _regions_mask(cover_regions, width, height) if cover_regions is not None else None
    for frame_receipt in frames:
        if not isinstance(frame_receipt, Mapping):
            return ["pointer_cover.trajectory_receipt_not_frame_complete"]
        try:
            decoded_time, decoded_source = next(decoded_frames)
        except (StopIteration, ValueError):
            return ["pointer_cover.trajectory_receipt_not_frame_complete"]
        source_time = _number(frame_receipt.get("source_time"))
        if source_time is None or abs(source_time - decoded_time) > max(0.002, interval * 0.05):
            return ["pointer_cover.trajectory_receipt_not_frame_complete"]
        source_frame = _read_hashed_color_image(
            frame_receipt, "source_frame_path", "source_frame_sha256"
        )
        clean_frame = _read_hashed_color_image(
            frame_receipt, "clean_frame_path", "clean_frame_sha256"
        )
        if (
            source_frame is None
            or clean_frame is None
            or source_frame.shape[:2] != (height, width)
            or clean_frame.shape != source_frame.shape
            or not _source_frame_corresponds(source_frame, decoded_source, [])
        ):
            return ["pointer_cover.trajectory_source_frame_mismatch"]
        clean_cover_values = (
            cv2.absdiff(clean_frame, cover_rgba[:, :, :3])[cover_mask.astype(bool)]
            if cover_rgba is not None
            and cover_rgba.ndim == 3
            and cover_rgba.shape[2] == 4
            and cover_mask is not None
            else None
        )
        if (
            cover_rgba is None
            or cover_rgba.ndim != 3
            or cover_rgba.shape[2] != 4
            or cover_mask is None
            or clean_cover_values is None
            or float(clean_cover_values.mean()) > POINTER_COVER_BACKGROUND_MAX_MEAN_DELTA
            or float(np.mean(clean_cover_values > POINTER_COVER_CHANGED_PIXEL_DELTA))
            > POINTER_COVER_CLEAN_FRAME_MAX_CHANGED_RATIO
        ):
            return ["pointer_cover.trajectory_clean_frame_cover_mismatch"]
        if previous_decoded_time is not None:
            decoded_intervals.append(decoded_time - previous_decoded_time)
        previous_decoded_time = decoded_time
        delta = cv2.cvtColor(cv2.absdiff(source_frame, clean_frame), cv2.COLOR_BGR2GRAY)
        union |= (delta > POINTER_COVER_CHANGED_PIXEL_DELTA).astype(np.uint8)
    try:
        next(decoded_frames)
    except StopIteration:
        pass
    except ValueError:
        return ["pointer_cover.trajectory_receipt_not_frame_complete"]
    else:
        return ["pointer_cover.trajectory_receipt_not_frame_complete"]
    decoded_interval = float(np.median(decoded_intervals)) if decoded_intervals else interval
    if decoded_interval is None or abs(interval - decoded_interval) > max(
        0.002, decoded_interval * 0.05
    ):
        return ["pointer_cover.trajectory_receipt_not_frame_complete"]
    union = cv2.dilate(union, np.ones((9, 9), np.uint8), iterations=1)
    if cv2.countNonZero(union) == 0:
        return ["pointer_cover.trajectory_receipt_incomplete"]

    mask_record = {
        "path": receipt.get("trajectory_mask_path"),
        "sha256": receipt.get("trajectory_mask_sha256"),
    }
    mask_path = mask_record["path"]
    if _sha256_file(mask_path) != str(mask_record["sha256"] or "").strip().lower():
        return ["pointer_cover.trajectory_mask_mismatch"]
    saved_mask = cv2.imread(
        os.fspath(Path(os.fspath(mask_path)).expanduser()), cv2.IMREAD_GRAYSCALE
    )
    if (
        saved_mask is None
        or saved_mask.shape != union.shape
        or not np.array_equal((saved_mask > 0).astype(np.uint8), union)
    ):
        return ["pointer_cover.trajectory_mask_mismatch"]
    x, y, region_width, region_height = cv2.boundingRect(union)
    trajectory = _positive_rectangle(cover.get("trajectory_bounds"))
    if trajectory is None or any(
        abs(actual - expected) > NUMBER_TOLERANCE
        for actual, expected in zip(trajectory, (x, y, region_width, region_height))
    ):
        return ["pointer_cover.trajectory_bounds_unverified"]
    return []


def _residual_cover_background_problems(
    cover: Mapping[str, Any],
    rgba: Any,
    regions: Sequence[tuple[int, int, int, int]],
    source_path: Path | None,
) -> list[str]:
    samples = cover.get("background_samples")
    if not isinstance(samples, list) or len(samples) < 3:
        return ["pointer_cover.background_samples_required"]
    receipt = cover.get("trajectory_receipt")
    receipt_frames = receipt.get("frames") if isinstance(receipt, Mapping) else None
    interval = (
        _number(receipt.get("frame_interval_seconds")) if isinstance(receipt, Mapping) else None
    )
    if not isinstance(receipt_frames, list) or not receipt_frames or interval is None:
        return ["pointer_cover.background_samples_not_trajectory_bound"]
    frames: list[Any] = []
    sample_times: list[float] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            return ["pointer_cover.background_samples_required"]
        frame = _read_hashed_color_image(sample, "path", "sha256")
        if frame is None or frame.shape[:2] != rgba.shape[:2]:
            return ["pointer_cover.background_samples_required"]
        source_time = _number(sample.get("source_time"))
        matching_rows = [
            row
            for row in receipt_frames
            if isinstance(row, Mapping)
            and source_time is not None
            and (row_time := _number(row.get("source_time"))) is not None
            and abs(source_time - row_time) <= max(0.002, interval * 0.05)
            and _same_path(sample.get("path"), row.get("clean_frame_path"))
            and str(sample.get("sha256") or "").strip().lower()
            == str(row.get("clean_frame_sha256") or "").strip().lower()
        ]
        if len(matching_rows) != 1:
            return ["pointer_cover.background_samples_not_trajectory_bound"]
        source_frame = _read_hashed_color_image(
            matching_rows[0], "source_frame_path", "source_frame_sha256"
        )
        if not _source_frame_corresponds(frame, source_frame, regions):
            return ["pointer_cover.background_source_frame_mismatch"]
        frames.append(frame)
        sample_times.append(source_time)
    receipt_times = [
        row_time
        for row in receipt_frames
        if isinstance(row, Mapping) and (row_time := _number(row.get("source_time"))) is not None
    ]
    if len({round(value, 9) for value in sample_times}) != len(sample_times):
        return ["pointer_cover.background_samples_not_trajectory_bound"]
    midpoint = (receipt_times[0] + receipt_times[-1]) / 2.0 if receipt_times else None
    coverage_tolerance = max(0.002, interval * 1.5)
    matched_roles = {
        "first": {
            index
            for index, sample_time in enumerate(sample_times)
            if abs(sample_time - receipt_times[0]) <= coverage_tolerance
        },
        "middle": {
            index
            for index, sample_time in enumerate(sample_times)
            if abs(sample_time - midpoint) <= coverage_tolerance
        },
        "last": {
            index
            for index, sample_time in enumerate(sample_times)
            if abs(sample_time - receipt_times[-1]) <= coverage_tolerance
        },
    }
    distinct_coverage = any(
        len({first, middle, last}) == 3
        for first in matched_roles["first"]
        for middle in matched_roles["middle"]
        for last in matched_roles["last"]
    )
    if len(receipt_times) != len(receipt_frames) or midpoint is None or not distinct_coverage:
        return ["pointer_cover.background_samples_not_trajectory_bound"]
    mask = _regions_mask(regions, rgba.shape[1], rgba.shape[0])
    for left_index, left in enumerate(frames):
        for right in frames[left_index + 1 :]:
            if not _frames_match_in_mask(left, right, mask):
                return ["pointer_cover.background_not_static"]
    cover_bgr = rgba[:, :, :3]
    if not _frames_match_in_mask(cover_bgr, frames[len(frames) // 2], mask):
        return ["pointer_cover.cover_background_mismatch"]
    return []


def _segment_has_position_motion(segment: Mapping[str, Any]) -> bool:
    values_by_property: dict[str, list[float]] = {}
    for group in segment.get("common_keyframes") or []:
        if not isinstance(group, Mapping):
            continue
        property_name = str(group.get("property_type") or group.get("property") or "").casefold()
        if "positionx" not in property_name and "positiony" not in property_name:
            continue
        raw_frames = group.get("keyframe_list")
        raw_frames = raw_frames if isinstance(raw_frames, list) else [group]
        for frame in raw_frames:
            if not isinstance(frame, Mapping):
                continue
            raw_value = frame.get("value")
            if raw_value is None:
                raw_values = frame.get("values")
                raw_value = raw_values[0] if isinstance(raw_values, list) and raw_values else None
            value = _number(raw_value)
            if value is not None:
                values_by_property.setdefault(property_name, []).append(value)
    return any(
        values and max(values) - min(values) > NUMBER_TOLERANCE
        for values in values_by_property.values()
    )


def _pointer_template_regions(
    artifact: Any,
    pointer_rgba: Any,
    pointer_segment: Mapping[str, Any],
    search_region: tuple[float, float, float, float] | None,
) -> list[tuple[int, int, int, int]] | None:
    clip = pointer_segment.get("clip")
    scale = clip.get("scale") if isinstance(clip, Mapping) else None
    scale_y = _number(scale.get("y")) if isinstance(scale, Mapping) else None
    if (
        artifact is None
        or pointer_rgba is None
        or pointer_rgba.ndim != 3
        or pointer_rgba.shape[2] != 4
        or scale_y is None
        or scale_y <= 0
    ):
        return None
    canvas_height, canvas_width = artifact.shape[:2]
    expected_height = int(round(canvas_height * scale_y))
    if expected_height <= 0:
        return None
    if search_region is None:
        search_left, search_top, search_right, search_bottom = (
            0,
            0,
            canvas_width,
            canvas_height,
        )
    else:
        region_x, region_y, region_width, region_height = search_region
        search_left = max(0, int(math.floor(region_x - expected_height)))
        search_top = max(0, int(math.floor(region_y - expected_height)))
        search_right = min(
            canvas_width,
            int(math.ceil(region_x + region_width + expected_height)),
        )
        search_bottom = min(
            canvas_height,
            int(math.ceil(region_y + region_height + expected_height)),
        )
    search_artifact = artifact[search_top:search_bottom, search_left:search_right]
    aspect = pointer_rgba.shape[1] / pointer_rgba.shape[0]
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for height_delta in range(
        -POINTER_COVER_OPENED_TEMPLATE_SIZE_TOLERANCE_PX,
        POINTER_COVER_OPENED_TEMPLATE_SIZE_TOLERANCE_PX + 1,
    ):
        template_height = expected_height + height_delta
        template_width = int(round(template_height * aspect))
        if (
            template_height <= 2
            or template_width <= 2
            or template_height > search_artifact.shape[0]
            or template_width > search_artifact.shape[1]
        ):
            continue
        resized = cv2.resize(
            pointer_rgba,
            (template_width, template_height),
            interpolation=cv2.INTER_AREA,
        )
        mask = (resized[:, :, 3] > 32).astype(np.uint8) * 255
        if cv2.countNonZero(mask) < 4:
            continue
        result = cv2.matchTemplate(
            search_artifact,
            resized[:, :, :3],
            cv2.TM_SQDIFF_NORMED,
            mask=mask,
        )
        valid = np.isfinite(result) & (result <= POINTER_COVER_OPENED_TEMPLATE_MAX_SQDIFF)
        component_count, labels, _stats, _centroids = cv2.connectedComponentsWithStats(
            valid.astype(np.uint8)
        )
        for component in range(1, component_count):
            ys, xs = np.where(labels == component)
            if not len(xs):
                continue
            best_index = int(np.argmin(result[ys, xs]))
            local_left = int(xs[best_index])
            local_top = int(ys[best_index])
            left = local_left + search_left
            top = local_top + search_top
            candidates.append(
                (
                    float(result[local_top, local_left]),
                    (left, top, template_width, template_height),
                )
            )
    clustered: list[tuple[float, tuple[int, int, int, int]]] = []
    for score, region in sorted(candidates):
        center = (region[0] + region[2] / 2, region[1] + region[3] / 2)
        if any(
            math.hypot(
                center[0] - (kept[1][0] + kept[1][2] / 2),
                center[1] - (kept[1][1] + kept[1][3] / 2),
            )
            <= max(3.0, min(region[2], region[3]) * 0.4)
            for kept in clustered
        ):
            continue
        clustered.append((score, region))
    return [region for _score, region in clustered]


def _opened_cover_registration(
    artifact: Any,
    cover_rgba: Any,
    region: tuple[float, float, float, float] | None,
) -> tuple[int, int, int, int, float] | None:
    if artifact is None or cover_rgba is None or region is None:
        return None
    region_x, region_y, region_width, region_height = [int(round(v)) for v in region]
    if (
        region_width < 2
        or region_height < 2
        or region_x < 0
        or region_y < 0
        or region_x + region_width > cover_rgba.shape[1]
        or region_y + region_height > cover_rgba.shape[0]
    ):
        return None
    template = cover_rgba[
        region_y : region_y + region_height,
        region_x : region_x + region_width,
        :3,
    ]
    padding = max(8, int(round(min(region_width, region_height) * 0.15)))
    left = max(0, region_x - padding)
    top = max(0, region_y - padding)
    right = min(artifact.shape[1], region_x + region_width + padding)
    bottom = min(artifact.shape[0], region_y + region_height + padding)
    search = artifact[top:bottom, left:right, :3]
    if template.size == 0 or search.shape[0] < region_height or search.shape[1] < region_width:
        return None
    template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    if float(template_gray.std()) < 3.0:
        return None
    result = cv2.matchTemplate(
        cv2.cvtColor(search, cv2.COLOR_BGR2GRAY),
        template_gray,
        cv2.TM_CCOEFF_NORMED,
    )
    _minimum, maximum, _minimum_location, maximum_location = cv2.minMaxLoc(result)
    if not math.isfinite(maximum) or maximum < POINTER_COVER_OPENED_COVER_MIN_CORRELATION:
        return None
    near_best = (result >= maximum - 0.002).astype(np.uint8)
    near_best = cv2.dilate(near_best, np.ones((3, 3), np.uint8), iterations=1)
    component_count, _labels = cv2.connectedComponents(near_best)
    if component_count != 2:
        return None
    return (
        left + int(maximum_location[0]),
        top + int(maximum_location[1]),
        region_width,
        region_height,
        float(maximum),
    )


def _opened_recorded_pointer_problems(
    cover: Mapping[str, Any],
    opened_samples: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Any],
    detected_regions: Sequence[tuple[int, int, int, int]],
    registrations: Sequence[tuple[int, int, int, int, float]],
) -> list[str]:
    if not (len(opened_samples) == len(artifacts) == len(detected_regions) == len(registrations)):
        return []
    aligned_crops: list[Any] = []
    comparison_mask: Any | None = None
    for artifact, detected, registration in zip(artifacts, detected_regions, registrations):
        left, top, width, height, _score = registration
        crop = artifact[top : top + height, left : left + width, :3]
        if crop.shape[:2] != (height, width):
            return ["pointer_cover.opened_jianying_cover_registration_invalid"]
        aligned_crops.append(crop)
        if comparison_mask is None:
            comparison_mask = np.ones((height, width), dtype=np.uint8)
            comparison_mask[:2, :] = 0
            comparison_mask[-2:, :] = 0
            comparison_mask[:, :2] = 0
            comparison_mask[:, -2:] = 0
        pointer_left, pointer_top, pointer_width, pointer_height = detected
        padding = max(4, int(round(max(pointer_width, pointer_height) * 0.2)))
        local_left = max(0, pointer_left - left - padding)
        local_top = max(0, pointer_top - top - padding)
        local_right = min(width, pointer_left + pointer_width - left + padding)
        local_bottom = min(height, pointer_top + pointer_height - top + padding)
        if local_left < local_right and local_top < local_bottom:
            comparison_mask[local_top:local_bottom, local_left:local_right] = 0
    problems: list[str] = []
    if comparison_mask is None or any(
        not _frames_match_in_mask(left, right, comparison_mask)
        for left_index, left in enumerate(aligned_crops)
        for right in aligned_crops[left_index + 1 :]
    ):
        problems.append("pointer_cover.opened_jianying_residual_motion_detected")
        problems.append("pointer_cover.opened_jianying_recorded_pointer_visible")

    receipt = cover.get("trajectory_receipt")
    receipt_frames = receipt.get("frames") if isinstance(receipt, Mapping) else None
    interval = (
        _number(receipt.get("frame_interval_seconds")) if isinstance(receipt, Mapping) else None
    )
    evaluated_source_masks = 0
    source_visible_votes = 0
    if isinstance(receipt_frames, list) and interval is not None:
        raw_region = _positive_rectangle(cover.get("opaque_regions")[0])
        if raw_region is not None:
            raw_x, raw_y, raw_width, raw_height = [int(round(v)) for v in raw_region]
            cover_rgba = cv2.imread(str(cover.get("asset_path")), cv2.IMREAD_UNCHANGED)
            cover_reference = (
                cover_rgba[
                    raw_y : raw_y + raw_height,
                    raw_x : raw_x + raw_width,
                    :3,
                ]
                if cover_rgba is not None and cover_rgba.ndim == 3 and cover_rgba.shape[2] == 4
                else None
            )
            for sample, artifact_crop, detected, registration in zip(
                opened_samples, aligned_crops, detected_regions, registrations
            ):
                sample_time = _number(sample.get("source_time"))
                candidates = [
                    row
                    for row in receipt_frames
                    if isinstance(row, Mapping)
                    and sample_time is not None
                    and (row_time := _number(row.get("source_time"))) is not None
                    and abs(row_time - sample_time) <= interval * 1.5 + 0.002
                ]
                if not candidates:
                    continue
                row = min(
                    candidates,
                    key=lambda value: abs(float(value["source_time"]) - sample_time),
                )
                source = _read_hashed_color_image(row, "source_frame_path", "source_frame_sha256")
                clean = _read_hashed_color_image(row, "clean_frame_path", "clean_frame_sha256")
                if source is None or clean is None or source.shape != clean.shape:
                    continue
                source_crop = source[raw_y : raw_y + raw_height, raw_x : raw_x + raw_width, :3]
                clean_crop = clean[raw_y : raw_y + raw_height, raw_x : raw_x + raw_width, :3]
                source_mask = (
                    cv2.cvtColor(cv2.absdiff(source_crop, clean_crop), cv2.COLOR_BGR2GRAY)
                    > POINTER_COVER_CHANGED_PIXEL_DELTA
                )
                registered_left, registered_top, _width, _height, _score = registration
                pointer_left, pointer_top, pointer_width, pointer_height = detected
                padding = max(4, int(round(max(pointer_width, pointer_height) * 0.15)))
                local_left = max(0, pointer_left - registered_left - padding)
                local_top = max(0, pointer_top - registered_top - padding)
                local_right = min(
                    raw_width,
                    pointer_left + pointer_width - registered_left + padding,
                )
                local_bottom = min(
                    raw_height,
                    pointer_top + pointer_height - registered_top + padding,
                )
                if cover_reference is not None:
                    warp = np.eye(2, 3, dtype=np.float32)
                    ecc_mask = np.where(source_mask, 0, 255).astype(np.uint8)
                    if local_left < local_right and local_top < local_bottom:
                        ecc_mask[local_top:local_bottom, local_left:local_right] = 0
                    try:
                        _correlation, warp = cv2.findTransformECC(
                            cv2.cvtColor(artifact_crop, cv2.COLOR_BGR2GRAY).astype(np.float32)
                            / 255.0,
                            cv2.cvtColor(cover_reference, cv2.COLOR_BGR2GRAY).astype(np.float32)
                            / 255.0,
                            warp,
                            cv2.MOTION_AFFINE,
                            (
                                cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                                100,
                                1e-6,
                            ),
                            ecc_mask,
                            1,
                        )
                    except cv2.error:
                        warp = np.eye(2, 3, dtype=np.float32)
                    source_crop = cv2.warpAffine(
                        source_crop,
                        warp,
                        (raw_width, raw_height),
                        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                        borderMode=cv2.BORDER_REPLICATE,
                    )
                    clean_crop = cv2.warpAffine(
                        clean_crop,
                        warp,
                        (raw_width, raw_height),
                        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                        borderMode=cv2.BORDER_REPLICATE,
                    )
                    source_mask = (
                        cv2.warpAffine(
                            source_mask.astype(np.uint8) * 255,
                            warp,
                            (raw_width, raw_height),
                            flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
                            borderMode=cv2.BORDER_CONSTANT,
                        )
                        > 0
                    )
                if local_left < local_right and local_top < local_bottom:
                    source_mask[local_top:local_bottom, local_left:local_right] = False
                minimum_mask_pixels = max(16, int(raw_width * raw_height * 0.001))
                if int(np.count_nonzero(source_mask)) < minimum_mask_pixels:
                    continue
                evaluated_source_masks += 1
                artifact_values = artifact_crop.astype(np.float32)
                source_vector = (source_crop.astype(np.float32) - clean_crop.astype(np.float32))[
                    source_mask
                ]
                artifact_vector = (artifact_values - clean_crop.astype(np.float32))[source_mask]
                vector_energy = float(np.sum(source_vector * source_vector))
                recorded_alpha = (
                    float(np.sum(artifact_vector * source_vector)) / vector_energy
                    if vector_energy > NUMBER_TOLERANCE
                    else 0.0
                )
                source_delta = np.mean(
                    np.abs(artifact_values - source_crop.astype(np.float32)), axis=2
                )[source_mask]
                clean_delta = np.mean(
                    np.abs(artifact_values - clean_crop.astype(np.float32)), axis=2
                )[source_mask]
                if (
                    float(source_delta.mean()) + 5.0 < float(clean_delta.mean())
                    or recorded_alpha >= POINTER_COVER_RECORDED_ALPHA_MAX
                ):
                    source_visible_votes += 1
    if not evaluated_source_masks:
        problems.append("pointer_cover.opened_jianying_recorded_pointer_unverifiable")
    elif source_visible_votes:
        problems.append("pointer_cover.opened_jianying_recorded_pointer_visible")
    return problems


def _saved_position_at_timeline_time(
    segment: Mapping[str, Any], timeline_time: float
) -> tuple[float, float] | None:
    target_range = segment.get("target_timerange")
    clip = segment.get("clip")
    transform = clip.get("transform") if isinstance(clip, Mapping) else None
    target_start = _number(target_range.get("start")) if isinstance(target_range, Mapping) else None
    target_duration = (
        _number(target_range.get("duration")) if isinstance(target_range, Mapping) else None
    )
    fallback_x = _number(transform.get("x")) if isinstance(transform, Mapping) else None
    fallback_y = _number(transform.get("y")) if isinstance(transform, Mapping) else None
    if (
        target_start is None
        or target_duration is None
        or target_duration <= 0
        or fallback_x is None
        or fallback_y is None
    ):
        return None
    relative_time = timeline_time * 1_000_000.0 - target_start
    if relative_time < -500.0 or relative_time > target_duration + 500.0:
        return None

    def axis_value(axis: str, fallback: float) -> float | None:
        matching_groups = [
            group
            for group in segment.get("common_keyframes") or []
            if isinstance(group, Mapping)
            and axis in str(group.get("property_type") or group.get("property") or "").casefold()
        ]
        if not matching_groups:
            return fallback
        if len(matching_groups) != 1:
            return None
        frames = matching_groups[0].get("keyframe_list")
        if not isinstance(frames, list) or not frames:
            return None
        points: list[tuple[float, float]] = []
        for frame_index, frame in enumerate(frames):
            if not isinstance(frame, Mapping):
                return None
            frame_time = _number(frame.get("time_offset", 0 if frame_index == 0 else None))
            raw_value = frame.get("value")
            if raw_value is None:
                values = frame.get("values")
                raw_value = values[0] if isinstance(values, list) and values else None
            frame_value = _number(raw_value)
            if frame_time is None or frame_value is None:
                return None
            points.append((frame_time, frame_value))
        points.sort()
        if any(right[0] <= left[0] for left, right in zip(points, points[1:])):
            return None
        if relative_time <= points[0][0]:
            return points[0][1]
        if relative_time >= points[-1][0]:
            return points[-1][1]
        for (left_time, left_value), (right_time, right_value) in zip(points, points[1:]):
            if left_time <= relative_time <= right_time:
                ratio = (relative_time - left_time) / (right_time - left_time)
                return left_value + (right_value - left_value) * ratio
        return None

    position_x = axis_value("positionx", fallback_x)
    position_y = axis_value("positiony", fallback_y)
    if position_x is None or position_y is None:
        return None
    return position_x, position_y


def _saved_keyframe_uses_linear_curve(keyframe: Mapping[str, Any]) -> bool:
    curve_type = str(keyframe.get("curveType") or "").strip().casefold()
    if curve_type:
        return curve_type == "line"
    for control_name in ("left_control", "right_control"):
        control = keyframe.get(control_name)
        if not isinstance(control, Mapping):
            return False
        control_x = _number(control.get("x"))
        control_y = _number(control.get("y"))
        if (
            control_x is None
            or control_y is None
            or abs(control_x) > NUMBER_TOLERANCE
            or abs(control_y) > NUMBER_TOLERANCE
        ):
            return False
    return True


def _opened_pointer_position_problems(
    cover: Mapping[str, Any],
    pointer_segment: Mapping[str, Any] | None,
    opened_samples: Sequence[Mapping[str, Any]],
    detected_regions: Sequence[tuple[int, int, int, int]],
    registrations: Sequence[tuple[int, int, int, int, float]],
    width: int,
    height: int,
) -> list[str]:
    raw_regions = cover.get("opaque_regions")
    raw_region = (
        _positive_rectangle(raw_regions[0])
        if isinstance(raw_regions, list) and len(raw_regions) == 1
        else None
    )
    if (
        pointer_segment is None
        or raw_region is None
        or not (len(opened_samples) == len(detected_regions) == len(registrations))
    ):
        return ["pointer_cover.opened_jianying_pointer_position_mismatch"]
    for group in pointer_segment.get("common_keyframes") or []:
        if not isinstance(group, Mapping):
            continue
        property_name = str(group.get("property_type") or group.get("property") or "").casefold()
        if "positionx" not in property_name and "positiony" not in property_name:
            continue
        for keyframe in group.get("keyframe_list") or []:
            if not isinstance(keyframe, Mapping):
                continue
            if not _saved_keyframe_uses_linear_curve(keyframe):
                return ["pointer_cover.opened_jianying_pointer_curve_unsupported"]
    raw_x, raw_y, _raw_width, _raw_height = raw_region
    for sample, detected, registration in zip(opened_samples, detected_regions, registrations):
        timeline_time = _number(sample.get("timeline_time"))
        position = (
            _saved_position_at_timeline_time(pointer_segment, timeline_time)
            if timeline_time is not None
            else None
        )
        if position is None:
            return ["pointer_cover.opened_jianying_pointer_position_mismatch"]
        registration_x, registration_y, _width, _height, _score = registration
        expected_center = (
            width / 2.0 + position[0] * (width / 2.0) + registration_x - raw_x,
            height / 2.0 - position[1] * (height / 2.0) + registration_y - raw_y,
        )
        detected_center = (
            detected[0] + detected[2] / 2.0,
            detected[1] + detected[3] / 2.0,
        )
        tolerance = max(6.0, max(detected[2], detected[3]) * 0.2)
        if math.dist(expected_center, detected_center) > tolerance:
            return ["pointer_cover.opened_jianying_pointer_position_mismatch"]
    return []


def _opened_artifact_problems(
    evidence: Mapping[str, Any],
    cover: Mapping[str, Any],
    opened_samples: Sequence[Mapping[str, Any]],
    content: Mapping[str, Any],
    width: int,
    height: int,
) -> list[str]:
    track_name = evidence.get("track_name") or evidence.get("overlay_track")
    segment_id = evidence.get("segment_id")
    tracks = _find_track(content, track_name) if isinstance(track_name, str) else []
    segments = (
        _find_segment(tracks[0], segment_id)
        if len(tracks) == 1 and isinstance(segment_id, str)
        else []
    )
    pointer_segment = segments[0] if len(segments) == 1 else None
    pointer_material = (
        _material_by_id(content, pointer_segment.get("material_id"))
        if pointer_segment is not None
        else None
    )
    pointer_rgba = (
        cv2.imread(str(pointer_material.get("path")), cv2.IMREAD_UNCHANGED)
        if pointer_material is not None
        else None
    )
    problems: list[str] = []
    artifacts: list[Any] = []
    contexts: list[Any] = []
    detected_regions: list[tuple[int, int, int, int]] = []
    registrations: list[tuple[int, int, int, int, float]] = []
    cover_rgba = cv2.imread(str(cover.get("asset_path")), cv2.IMREAD_UNCHANGED)
    raw_regions = cover.get("opaque_regions")
    pointer_search_region = (
        _positive_rectangle(raw_regions[0])
        if isinstance(raw_regions, list) and len(raw_regions) == 1
        else None
    )
    for sample in opened_samples:
        artifact = _read_hashed_color_image(sample, "artifact_path", "artifact_sha256")
        if artifact is None or artifact.shape[:2] != (height, width):
            problems.append("pointer_cover.opened_jianying_artifact_invalid")
            continue
        artifacts.append(artifact)
        registration = _opened_cover_registration(artifact, cover_rgba, pointer_search_region)
        if registration is None:
            problems.append("pointer_cover.opened_jianying_cover_registration_invalid")
            if pointer_search_region is not None:
                registrations.append((*[int(round(value)) for value in pointer_search_region], 0.0))
        else:
            registrations.append(registration)
        context = _read_hashed_color_image(sample, "editor_context_path", "editor_context_sha256")
        canvas_rect = _positive_rectangle(sample.get("editor_canvas_rect"))
        if not _opened_capture_receipt_valid(sample, context):
            problems.append("pointer_cover.opened_jianying_capture_receipt_invalid")
        if context is None or canvas_rect is None:
            problems.append("pointer_cover.opened_jianying_editor_context_invalid")
        else:
            x, y, context_width, context_height = [int(round(v)) for v in canvas_rect]
            if (
                x < 0
                or y < 0
                or context_width < 1
                or context_height < 1
                or x + context_width > context.shape[1]
                or y + context_height > context.shape[0]
            ):
                problems.append("pointer_cover.opened_jianying_editor_context_invalid")
            else:
                context_canvas = context[y : y + context_height, x : x + context_width]
                resized_context = cv2.resize(
                    context_canvas, (width, height), interpolation=cv2.INTER_LINEAR
                )
                correlation = float(
                    cv2.matchTemplate(
                        cv2.cvtColor(resized_context, cv2.COLOR_BGR2GRAY),
                        cv2.cvtColor(artifact, cv2.COLOR_BGR2GRAY),
                        cv2.TM_CCOEFF_NORMED,
                    )[0, 0]
                )
                context_delta = np.max(cv2.absdiff(resized_context, artifact), axis=2)
                if (
                    not math.isfinite(correlation)
                    or correlation < POINTER_COVER_OPENED_CONTEXT_MIN_CORRELATION
                    or float(context_delta.mean()) > POINTER_COVER_OPENED_CONTEXT_MAX_MEAN_DELTA
                    or float(np.percentile(context_delta, 95))
                    > POINTER_COVER_OPENED_CONTEXT_MAX_P95_DELTA
                ):
                    problems.append("pointer_cover.opened_jianying_editor_context_invalid")
                contexts.append(context)
        regions = (
            _pointer_template_regions(artifact, pointer_rgba, pointer_segment, None)
            if pointer_segment is not None
            else None
        )
        if regions is None or len(regions) != 1:
            problems.append("pointer_cover.opened_jianying_pointer_instance_mismatch")
            continue
        detected_region = regions[0]
        detected_regions.append(detected_region)
        inspection = sample.get("visual_inspection")
        declared = (
            _positive_rectangle(inspection.get("hand_regions")[0])
            if isinstance(inspection, Mapping)
            and isinstance(inspection.get("hand_regions"), list)
            and len(inspection["hand_regions"]) == 1
            else None
        )
        if declared is None:
            problems.append("pointer_cover.opened_jianying_visual_inspection_invalid")
        else:
            detected_center = (
                detected_region[0] + detected_region[2] / 2,
                detected_region[1] + detected_region[3] / 2,
            )
            declared_center = (
                declared[0] + declared[2] / 2,
                declared[1] + declared[3] / 2,
            )
            if math.hypot(
                detected_center[0] - declared_center[0],
                detected_center[1] - declared_center[1],
            ) > max(6.0, max(detected_region[2], detected_region[3]) * 0.2):
                problems.append("pointer_cover.opened_jianying_visual_inspection_invalid")

    for frames, threshold_error in (
        (artifacts, "pointer_cover.opened_jianying_samples_perceptually_duplicated"),
        (contexts, "pointer_cover.opened_jianying_editor_context_duplicated"),
    ):
        if len(frames) != len(opened_samples):
            continue
        for left_index, left in enumerate(frames):
            for right in frames[left_index + 1 :]:
                if left.shape != right.shape:
                    problems.append("pointer_cover.opened_jianying_editor_context_invalid")
                    continue
                changed_ratio = float(
                    np.mean(
                        np.max(cv2.absdiff(left, right), axis=2) > POINTER_COVER_CHANGED_PIXEL_DELTA
                    )
                )
                if changed_ratio <= POINTER_COVER_OPENED_DISTINCT_CHANGED_RATIO:
                    problems.append(threshold_error)
                    break
    if (
        pointer_segment is not None
        and _segment_has_position_motion(pointer_segment)
        and len(detected_regions) == len(opened_samples)
    ):
        centers = [
            (region[0] + region[2] / 2, region[1] + region[3] / 2) for region in detected_regions
        ]
        if any(
            math.hypot(left[0] - right[0], left[1] - right[1]) < 2.0
            for left_index, left in enumerate(centers)
            for right in centers[left_index + 1 :]
        ):
            problems.append("pointer_cover.opened_jianying_samples_perceptually_duplicated")
    problems.extend(
        _opened_recorded_pointer_problems(
            cover,
            opened_samples,
            artifacts,
            detected_regions,
            registrations,
        )
    )
    problems.extend(
        _opened_pointer_position_problems(
            cover,
            pointer_segment,
            opened_samples,
            detected_regions,
            registrations,
            width,
            height,
        )
    )
    return sorted(set(problems))


def _hard_edge_cover_geometry_problems(
    cover: Mapping[str, Any], width: int, height: int
) -> list[str]:
    raw_regions = cover.get("opaque_regions")
    if not isinstance(raw_regions, list) or len(raw_regions) != 1:
        return ["pointer_cover.opaque_regions_not_single_rectangle"]
    region = _positive_rectangle(raw_regions[0])
    if region is None:
        return ["pointer_cover.opaque_regions_invalid"]
    trajectory = _positive_rectangle(cover.get("trajectory_bounds"))
    if trajectory is None:
        return ["pointer_cover.trajectory_bounds_invalid"]
    margin = _number(cover.get("safety_margin_px"))
    if margin is None or margin <= 0:
        return ["pointer_cover.safety_margin_invalid"]
    region_x, region_y, region_width, region_height = region
    trajectory_x, trajectory_y, trajectory_width, trajectory_height = trajectory
    if (
        region_x + region_width > width + NUMBER_TOLERANCE
        or region_y + region_height > height + NUMBER_TOLERANCE
        or trajectory_x + trajectory_width > width + NUMBER_TOLERANCE
        or trajectory_y + trajectory_height > height + NUMBER_TOLERANCE
    ):
        return ["pointer_cover.trajectory_bounds_invalid"]
    expected_left = max(0.0, trajectory_x - margin)
    expected_top = max(0.0, trajectory_y - margin)
    expected_right = min(float(width), trajectory_x + trajectory_width + margin)
    expected_bottom = min(float(height), trajectory_y + trajectory_height + margin)
    if (
        region_x > expected_left + NUMBER_TOLERANCE
        or region_y > expected_top + NUMBER_TOLERANCE
        or region_x + region_width < expected_right - NUMBER_TOLERANCE
        or region_y + region_height < expected_bottom - NUMBER_TOLERANCE
    ):
        return ["pointer_cover.opaque_region_not_safety_expanded"]
    return []


def _residual_cover_composite_problems(
    evidence: Mapping[str, Any],
    cover: Mapping[str, Any],
    content: Mapping[str, Any],
    width: int,
    height: int,
) -> list[str]:
    samples = cover.get("final_composite_samples")
    if not isinstance(samples, list) or not samples:
        return ["pointer_cover.final_composite_samples_required"]
    opened_samples = [
        sample
        for sample in samples
        if isinstance(sample, Mapping) and sample.get("artifact_kind") == "opened_jianying"
    ]
    if not opened_samples:
        return ["pointer_cover.opened_jianying_sample_required"]
    problems = _opened_cover_receipt_problems(opened_samples)
    if not any(
        isinstance(sample, Mapping) and sample.get("artifact_kind") != "opened_jianying"
        for sample in samples
    ):
        problems.append("pointer_cover.final_composite_samples_required")
    for sample in samples:
        if not isinstance(sample, Mapping):
            problems.append("pointer_cover.final_composite_samples_required")
            continue
        artifact = _read_hashed_color_image(sample, "artifact_path", "artifact_sha256")
        if sample.get("artifact_kind") == "opened_jianying":
            if artifact is None or artifact.shape[:2] != (height, width):
                problems.append("pointer_cover.opened_jianying_artifact_invalid")
            continue
        expected = _read_hashed_color_image(
            sample, "expected_background_path", "expected_background_sha256"
        )
        if (
            artifact is None
            or expected is None
            or artifact.shape != expected.shape
            or artifact.shape[:2] != (height, width)
        ):
            problems.append("pointer_cover.final_composite_samples_required")
            continue
        residual_regions = _comparison_exclusion_regions(
            sample.get("residual_regions"), width, height
        )
        pointer_regions = _comparison_exclusion_regions(
            sample.get("editable_pointer_exclusion_regions"), width, height
        )
        if residual_regions is None or pointer_regions is None:
            problems.append("pointer_cover.final_composite_samples_required")
            continue
        residual_mask = _regions_mask(residual_regions, width, height)
        pointer_mask = _regions_mask(pointer_regions, width, height)
        residual_mask[pointer_mask.astype(bool)] = 0
        if not _frames_match_in_mask(artifact, expected, residual_mask):
            problems.append("pointer_cover.final_composite_residual_mismatch")
        if _frames_match_in_mask(artifact, expected, pointer_mask):
            problems.append("pointer_cover.final_composite_pointer_missing")
    problems.extend(
        _opened_artifact_problems(evidence, cover, opened_samples, content, width, height)
    )
    return sorted(set(problems))


def pointer_saved_residual_cover_problems(
    evidence: Mapping[str, Any], content: Mapping[str, Any]
) -> list[str]:
    """Validate an editable pointer over a transparent recorded-pointer cover."""

    if not isinstance(evidence, Mapping) or not isinstance(content, Mapping):
        return ["pointer_cover.invalid_input"]
    cover = evidence.get("residual_pointer_cover")
    if cover is None:
        return []
    if (
        not isinstance(cover, Mapping)
        or not _status_passed(cover.get("status"))
        or cover.get("mode") != "transparent_roi_still_cover"
        or cover.get("region_shape") != "hard_edge_rectangle"
    ):
        return ["pointer_cover.invalid_contract"]
    path_value = cover.get("asset_path")
    expected_hash = str(cover.get("asset_sha256") or "").strip().lower()
    if _sha256_file(path_value) != expected_hash:
        return ["pointer_cover.saved_material_mismatch"]
    rgba = cv2.imread(os.fspath(Path(os.fspath(path_value)).expanduser()), cv2.IMREAD_UNCHANGED)
    if rgba is None or rgba.ndim != 3 or rgba.shape[2] != 4:
        return ["pointer_cover.asset_not_rgba"]
    canvas = _canvas_size(content)
    if canvas is None or (rgba.shape[1], rgba.shape[0]) != (
        int(canvas[0]),
        int(canvas[1]),
    ):
        return ["pointer_cover.canvas_mismatch"]
    geometry_problems = _hard_edge_cover_geometry_problems(cover, rgba.shape[1], rgba.shape[0])
    if geometry_problems:
        return geometry_problems
    regions = _comparison_exclusion_regions(
        cover.get("opaque_regions"), rgba.shape[1], rgba.shape[0]
    )
    if regions is None:
        return ["pointer_cover.opaque_regions_invalid"]

    problems, source_path = _residual_cover_source_material(cover, content)
    pointer_segment, pointer_rgba = _saved_pointer_segment_and_rgba(evidence, content)
    allowed_alpha = _regions_mask(regions, rgba.shape[1], rgba.shape[0])
    if np.any(rgba[:, :, 3][allowed_alpha == 0] != 0):
        problems.append("pointer_cover.alpha_outside_regions_nonzero")
    if np.any(rgba[:, :, 3][allowed_alpha != 0] != 255):
        problems.append("pointer_cover.alpha_inside_regions_not_opaque")
    problems.extend(_trajectory_receipt_problems(cover, source_path, rgba.shape[1], rgba.shape[0]))
    (
        trajectory_pointer_problems,
        detected_pointer_trajectory,
        trajectory_frame_interval,
    ) = _trajectory_pointer_analysis(cover, pointer_segment, pointer_rgba)
    problems.extend(trajectory_pointer_problems)
    problems.extend(_saved_residual_cover_layer_problems(evidence, cover, content))
    problems.extend(_residual_cover_background_problems(cover, rgba, regions, source_path))
    problems.extend(
        _residual_cover_composite_problems(evidence, cover, content, rgba.shape[1], rgba.shape[0])
    )
    opened_samples = [
        sample
        for sample in cover.get("final_composite_samples") or []
        if isinstance(sample, Mapping) and sample.get("artifact_kind") == "opened_jianying"
    ]
    opened_timeline_mismatch = bool(opened_samples) and any(
        sample.get("timeline_id") != content.get("id") for sample in opened_samples
    )
    if opened_timeline_mismatch:
        problems.append("pointer_cover.opened_jianying_sample_timeline_mismatch")
    matching_opened_samples = [
        sample for sample in opened_samples if sample.get("timeline_id") == content.get("id")
    ]
    opened_window_mismatch = False
    if matching_opened_samples and not opened_timeline_mismatch:
        timeline_window = cover.get("timeline_window")
        timeline_start = (
            _number(timeline_window.get("start")) if isinstance(timeline_window, Mapping) else None
        )
        timeline_duration = (
            _number(timeline_window.get("duration"))
            if isinstance(timeline_window, Mapping)
            else None
        )
        if (
            timeline_start is None
            or timeline_duration is None
            or timeline_duration <= 0
            or any(
                (sample_time := _number(sample.get("timeline_time"))) is not None
                and not (
                    timeline_start - NUMBER_TOLERANCE
                    <= sample_time
                    <= timeline_start + timeline_duration + NUMBER_TOLERANCE
                )
                for sample in matching_opened_samples
            )
        ):
            opened_window_mismatch = True
            problems.append("pointer_cover.opened_jianying_sample_window_mismatch")
        if not opened_window_mismatch:
            source_window = cover.get("source_window")
            source_start = (
                _number(source_window.get("start")) if isinstance(source_window, Mapping) else None
            )
            source_end = (
                _number(source_window.get("end")) if isinstance(source_window, Mapping) else None
            )
            visibility = evidence.get("recorded_pointer_visibility")
            first_visible = (
                _number(visibility.get("first_visible"))
                if isinstance(visibility, Mapping)
                else None
            )
            last_visible = (
                _number(visibility.get("last_visible")) if isinstance(visibility, Mapping) else None
            )
            expected_source_times = (
                {
                    "recorded_first_visible": first_visible,
                    "recorded_midpoint": (
                        (first_visible + last_visible) / 2
                        if first_visible is not None and last_visible is not None
                        else None
                    ),
                    "recorded_last_visible": last_visible,
                }
                if source_start is not None
                and source_end is not None
                and first_visible is not None
                and last_visible is not None
                and source_start <= first_visible <= last_visible <= source_end
                else {}
            )
            coverage_complete = len(expected_source_times) == len(
                POINTER_COVER_OPENED_COVERAGE_ROLES
            )
            for sample in matching_opened_samples:
                sample_source_time = _number(sample.get("source_time"))
                sample_timeline_time = _number(sample.get("timeline_time"))
                mapped_sample_time = (
                    _saved_source_time_to_timeline(
                        content,
                        track_name=cover.get("source_track_name"),
                        material_id=cover.get("source_material_id"),
                        source_time=sample_source_time,
                    )
                    if sample_source_time is not None
                    else None
                )
                if (
                    sample_timeline_time is None
                    or mapped_sample_time is None
                    or abs(sample_timeline_time - mapped_sample_time)
                    > POINTER_COVER_OPENED_TIME_TOLERANCE_SECONDS
                ):
                    coverage_complete = False
                    problems.append("pointer_cover.opened_jianying_source_timeline_mismatch")
            for role, expected_source_time in expected_source_times.items():
                role_samples = [
                    sample
                    for sample in matching_opened_samples
                    if sample.get("coverage_role") == role
                ]
                expected_timeline_time = _saved_source_time_to_timeline(
                    content,
                    track_name=cover.get("source_track_name"),
                    material_id=cover.get("source_material_id"),
                    source_time=expected_source_time,
                )
                if not any(
                    (sample_source_time := _number(sample.get("source_time"))) is not None
                    and (sample_timeline_time := _number(sample.get("timeline_time"))) is not None
                    and expected_timeline_time is not None
                    and (
                        mapped_sample_timeline := _saved_source_time_to_timeline(
                            content,
                            track_name=cover.get("source_track_name"),
                            material_id=cover.get("source_material_id"),
                            source_time=sample_source_time,
                        )
                    )
                    is not None
                    and abs(sample_source_time - expected_source_time)
                    <= POINTER_COVER_OPENED_TIME_TOLERANCE_SECONDS
                    and abs(sample_timeline_time - expected_timeline_time)
                    <= POINTER_COVER_OPENED_TIME_TOLERANCE_SECONDS
                    and abs(sample_timeline_time - mapped_sample_timeline)
                    <= POINTER_COVER_OPENED_TIME_TOLERANCE_SECONDS
                    for sample in role_samples
                ):
                    coverage_complete = False
            if not coverage_complete:
                problems.append("pointer_cover.opened_jianying_window_coverage_incomplete")
    temporal_problems = {
        "pointer_cover.opened_jianying_sample_timeline_mismatch",
        "pointer_cover.opened_jianying_sample_window_mismatch",
        "pointer_cover.opened_jianying_source_timeline_mismatch",
        "pointer_cover.opened_jianying_window_coverage_incomplete",
    }
    if not temporal_problems.intersection(problems) and (
        "pointer_cover.opened_jianying_sample_required" not in problems
    ):
        problems.extend(
            _opened_trajectory_sample_problems(
                detected_pointer_trajectory,
                opened_samples,
                trajectory_frame_interval,
            )
        )
    if temporal_problems.intersection(problems):
        problems = [
            problem
            for problem in problems
            if problem != "pointer_cover.opened_jianying_pointer_position_mismatch"
        ]
    return sorted(set(problems))


def _saved_display_safe_problems(
    fallback: Mapping[str, Any],
    content: Mapping[str, Any],
    pointer_segment: Mapping[str, Any] | None,
) -> tuple[list[str], Mapping[str, Any] | None]:
    problems: list[str] = []
    tracks = _find_track(content, str(fallback["track_name"]))
    if len(tracks) != 1:
        return ["pointer_layers.invalid_display_safe_fallback"], None
    segments = _find_segment(tracks[0], str(fallback["segment_id"]))
    if len(segments) != 1:
        return ["pointer_layers.invalid_display_safe_fallback"], None
    segment = segments[0]
    clip = segment.get("clip")
    scale = clip.get("scale") if isinstance(clip, Mapping) else None
    transform = clip.get("transform") if isinstance(clip, Mapping) else None
    if not isinstance(scale, Mapping) or any(
        value is None or abs(value - 1.0) > NUMBER_TOLERANCE
        for value in (
            _number(scale.get("x")) if isinstance(scale, Mapping) else None,
            _number(scale.get("y")) if isinstance(scale, Mapping) else None,
        )
    ):
        problems.append("pointer_layers.display_safe_scale_mismatch")
    if not isinstance(transform, Mapping) or any(
        value is None or abs(value) > NUMBER_TOLERANCE
        for value in (
            _number(transform.get("x")) if isinstance(transform, Mapping) else None,
            _number(transform.get("y")) if isinstance(transform, Mapping) else None,
        )
    ):
        problems.append("pointer_layers.display_safe_transform_mismatch")

    material = _material_by_id(content, segment.get("material_id"))
    saved_path = material.get("path") if material is not None else None
    if material is None or not _same_path(saved_path, fallback.get("asset_path")):
        problems.append("pointer_layers.display_safe_asset_missing")
    else:
        media_path = Path(str(saved_path)).expanduser()
        dimensions = _read_video_size(media_path) if media_path.is_file() else None
        if dimensions is None:
            problems.append("pointer_layers.display_safe_asset_missing")
        else:
            canvas = _canvas_size(content)
            if canvas is None or dimensions != (int(canvas[0]), int(canvas[1])):
                problems.append("pointer_layers.display_safe_canvas_mismatch")
            has_audio = _video_has_audio(media_path)
            if has_audio is True:
                problems.append("pointer_layers.display_safe_audio_stream_present")
            elif has_audio is None:
                problems.append("pointer_layers.display_safe_audio_probe_failed")

    volume = _number(segment.get("volume"))
    if volume is None or abs(volume) > NUMBER_TOLERANCE:
        problems.append("pointer_layers.display_safe_segment_not_muted")

    fallback_range = _timerange(segment)
    pointer_range = _timerange(pointer_segment) if pointer_segment is not None else None
    if fallback_range is None or pointer_range is None or not _overlaps(segment, pointer_segment):
        problems.append("pointer_layers.display_safe_underlying_pointer_missing")
    elif (
        fallback_range[0] < pointer_range[0] - NUMBER_TOLERANCE
        or fallback_range[1] > pointer_range[1] + NUMBER_TOLERANCE
    ):
        problems.append("pointer_layers.display_safe_window_exceeds_pointer")

    render_index = _number(segment.get("render_index"))
    all_indexes = [
        index
        for track in _tracks(content)
        for saved_segment in _segments(track)
        if (index := _number(saved_segment.get("render_index"))) is not None
    ]
    if render_index is None or (all_indexes and render_index != max(all_indexes)):
        problems.append("pointer_layers.display_safe_not_topmost")

    artifact = fallback.get("opened_draft_drift_artifact_path")
    artifact_path = (
        Path(artifact).expanduser() if isinstance(artifact, (str, os.PathLike)) else None
    )
    if (
        fallback.get("opened_draft_drift") is not True
        or artifact_path is None
        or not artifact_path.is_file()
    ):
        problems.append("pointer_layers.display_safe_drift_artifact_missing")
    else:
        expected_artifact_hash = fallback.get("opened_draft_drift_artifact_sha256")
        if (
            not isinstance(expected_artifact_hash, str)
            or _sha256_file(artifact_path) != expected_artifact_hash
        ):
            problems.append("pointer_layers.display_safe_drift_artifact_hash_mismatch")
        if artifact_path.suffix.casefold() == ".json":
            try:
                artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                artifact_payload = None
            if not isinstance(artifact_payload, Mapping):
                problems.append("pointer_layers.display_safe_drift_artifact_invalid")
            else:
                artifact_status = str(artifact_payload.get("status") or "").strip().lower()
                artifact_start = _number(artifact_payload.get("start"))
                artifact_duration = _number(artifact_payload.get("duration"))
                if (
                    artifact_status not in {*PASS_STATUSES, "confirmed"}
                    or artifact_payload.get("timeline_id") != content.get("id")
                    or artifact_payload.get("segment_id") != segment.get("id")
                    or fallback_range is None
                    or artifact_start is None
                    or artifact_duration is None
                    or abs(artifact_start - fallback_range[0]) > NUMBER_TOLERANCE
                    or abs(artifact_start + artifact_duration - fallback_range[1])
                    > NUMBER_TOLERANCE
                ):
                    problems.append("pointer_layers.display_safe_drift_artifact_invalid")
        elif cv2.imread(str(artifact_path), cv2.IMREAD_UNCHANGED) is None:
            problems.append("pointer_layers.display_safe_drift_artifact_invalid")

    drift_timeline_id = fallback.get("opened_draft_drift_timeline_id")
    if not isinstance(drift_timeline_id, str) or drift_timeline_id != content.get("id"):
        problems.append("pointer_layers.display_safe_drift_timeline_mismatch")
    drift_window = fallback.get("opened_draft_drift_window")
    drift_start = _number(drift_window.get("start")) if isinstance(drift_window, Mapping) else None
    drift_duration = (
        _number(drift_window.get("duration")) if isinstance(drift_window, Mapping) else None
    )
    if (
        fallback_range is None
        or drift_start is None
        or drift_duration is None
        or drift_duration <= 0
        or abs(drift_start - fallback_range[0]) > NUMBER_TOLERANCE
        or abs(drift_start + drift_duration - fallback_range[1]) > NUMBER_TOLERANCE
    ):
        problems.append("pointer_layers.display_safe_drift_window_mismatch")
    return problems, segment


def pointer_saved_motion_problems(
    evidence: Mapping[str, Any], content: Mapping[str, Any]
) -> list[str]:
    """Bind one motion-preserving cleanup receipt to its saved clean segment."""

    if not isinstance(evidence, Mapping) or not isinstance(content, Mapping):
        return ["pointer_motion.invalid_input"]
    motion = evidence.get("motion_preservation")
    if not isinstance(motion, Mapping):
        return []
    clean_layers = evidence.get("clean_layers")
    if not isinstance(clean_layers, list) or len(clean_layers) != 1:
        return ["pointer_motion.clean_layer_required"]
    reference = clean_layers[0]
    if not isinstance(reference, Mapping):
        return ["pointer_motion.clean_layer_required"]
    track_name = reference.get("track_name")
    segment_id = reference.get("segment_id")
    if not isinstance(track_name, str) or not isinstance(segment_id, str):
        return ["pointer_motion.clean_layer_required"]
    tracks = _find_track(content, track_name)
    if len(tracks) != 1:
        return ["pointer_motion.clean_layer_missing"]
    segments = _find_segment(tracks[0], segment_id)
    if len(segments) != 1:
        return ["pointer_motion.clean_layer_missing"]
    segment = segments[0]
    material = _material_by_id(content, segment.get("material_id"))
    if material is None:
        return ["pointer_motion.clean_material_missing"]

    receipt_path = motion.get("clean_media_path")
    saved_path = material.get("path")
    if not _same_path(receipt_path, saved_path):
        return ["pointer_motion.clean_material_path_mismatch"]
    receipt_sha256 = str(motion.get("clean_media_sha256") or "").strip().lower()
    if _sha256_file(saved_path) != receipt_sha256:
        return ["pointer_motion.clean_material_hash_mismatch"]

    path = Path(os.fspath(saved_path)).expanduser()
    problems: list[str] = []
    frame_count = _decodable_video_frame_count(path)
    has_motion = _video_has_sampled_motion(path) if frame_count is not None else None
    if frame_count is None or has_motion is None or _video_has_audio(path) is not False:
        problems.append("pointer_motion.clean_media_invalid")
    elif not has_motion:
        problems.append("pointer_motion.clean_media_frozen")
    if _number(segment.get("volume")) != 0.0:
        problems.append("pointer_motion.clean_volume_mismatch")

    source_window = motion.get("source_window")
    source_start = (
        _number(source_window.get("start")) if isinstance(source_window, Mapping) else None
    )
    source_end = _number(source_window.get("end")) if isinstance(source_window, Mapping) else None
    expected_source_duration = (
        source_end - source_start
        if source_start is not None and source_end is not None and source_end > source_start
        else None
    )

    source_track_name = motion.get("source_track_name")
    source_material_id = motion.get("source_material_id")
    source_path_value = motion.get("source_media_path")
    source_material = (
        _material_by_id(content, source_material_id)
        if isinstance(source_material_id, str) and source_material_id
        else None
    )
    source_tracks = (
        _find_track(content, source_track_name)
        if isinstance(source_track_name, str) and source_track_name
        else []
    )
    source_material_is_used = len(source_tracks) == 1 and any(
        source_segment.get("material_id") == source_material_id
        for source_segment in _segments(source_tracks[0])
    )
    source_binding_valid = (
        source_material is not None
        and source_material_is_used
        and _same_path(source_path_value, source_material.get("path"))
    )
    if not source_binding_valid:
        problems.append("pointer_motion.source_material_binding_mismatch")

    source_path = (
        Path(os.fspath(source_path_value)).expanduser()
        if isinstance(source_path_value, (str, os.PathLike))
        else None
    )
    source_sha256 = str(motion.get("source_media_sha256") or "").strip().lower()
    source_hash_valid = (
        source_binding_valid
        and source_path is not None
        and _sha256_file(source_path) == source_sha256
    )
    if source_binding_valid and not source_hash_valid:
        problems.append("pointer_motion.source_media_hash_mismatch")

    sampled_clean_frames = _sampled_video_frames(path) if frame_count is not None else None
    exclusion_regions = None
    if sampled_clean_frames:
        clean_height, clean_width = sampled_clean_frames[0][1].shape[:2]
        exclusion_regions = _comparison_exclusion_regions(
            motion.get("comparison_exclusion_regions"), clean_width, clean_height
        )
    if exclusion_regions is None:
        problems.append("pointer_motion.source_comparison_regions_invalid")
    if (
        source_hash_valid
        and source_path is not None
        and source_start is not None
        and has_motion is True
        and sampled_clean_frames
        and exclusion_regions is not None
    ):
        for media_time, clean_frame in sampled_clean_frames:
            source_frame = _read_video_frame_at(source_path, source_start + media_time)
            if not _source_frame_corresponds(clean_frame, source_frame, exclusion_regions):
                problems.append("pointer_motion.clean_source_frame_mismatch")
                break

    media_duration = _video_duration_seconds(path) if frame_count is not None else None
    if expected_source_duration is not None and (
        media_duration is None
        or abs(media_duration - expected_source_duration) > CLEAN_MEDIA_DURATION_TOLERANCE_SECONDS
    ):
        problems.append("pointer_motion.clean_media_duration_mismatch")

    source_timerange = segment.get("source_timerange")
    saved_source_start = (
        _number(source_timerange.get("start", 0)) if isinstance(source_timerange, Mapping) else None
    )
    saved_source_duration = (
        _number(source_timerange.get("duration")) if isinstance(source_timerange, Mapping) else None
    )
    if (
        expected_source_duration is None
        or saved_source_start is None
        or saved_source_duration is None
        or abs(saved_source_start) > 500
        or abs(saved_source_duration - expected_source_duration * 1_000_000) > 500
    ):
        problems.append("pointer_motion.clean_source_timerange_mismatch")

    target_timerange = segment.get("target_timerange")
    saved_start = (
        _number(target_timerange.get("start", 0)) if isinstance(target_timerange, Mapping) else None
    )
    saved_duration = (
        _number(target_timerange.get("duration")) if isinstance(target_timerange, Mapping) else None
    )
    if (
        source_start is None
        or source_end is None
        or source_end <= source_start
        or saved_duration is None
        or abs(saved_duration - (source_end - source_start) * 1_000_000) > 500
    ):
        problems.append("pointer_motion.clean_duration_mismatch")

    timeline_window = evidence.get("timeline_window")
    timeline_start = None
    timeline_end = None
    if (
        isinstance(timeline_window, Sequence)
        and not isinstance(timeline_window, (str, bytes))
        and len(timeline_window) == 2
    ):
        timeline_start = _number(timeline_window[0])
        timeline_end = _number(timeline_window[1])
    if timeline_start is None or timeline_end is None or timeline_end <= timeline_start:
        problems.append("pointer_motion.clean_timeline_window_required")
    else:
        expected_start = timeline_start * 1_000_000
        expected_end = timeline_end * 1_000_000
        saved_end = (
            saved_start + saved_duration
            if saved_start is not None and saved_duration is not None
            else None
        )
        if (
            saved_start is None
            or saved_end is None
            or abs(saved_start - expected_start) > 500
            or abs(saved_end - expected_end) > 500
        ):
            problems.append("pointer_motion.clean_timeline_window_mismatch")
    return sorted(set(problems))


def pointer_saved_layer_problems(
    evidences: Sequence[Mapping[str, Any]], content: Mapping[str, Any]
) -> list[str]:
    """Reject undocumented pointer layers and invalid overlapping render order."""

    if (
        not isinstance(evidences, Sequence)
        or isinstance(evidences, (str, bytes))
        or not isinstance(content, Mapping)
    ):
        return ["pointer_layers.invalid_input"]

    problems: list[str] = []
    allowed_tracks: set[str] = set()
    allowed_segments: dict[str, set[str]] = {}
    allowed_material_ids: set[Any] = set()
    pointer_records: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    role_tracks: dict[str, set[str]] = {}
    role_materials: dict[str, set[Any]] = {}
    pointer_paths: list[str] = []
    pointer_hashes: set[str] = set()
    pointer_sizes: set[int] = set()

    for raw_evidence in evidences:
        if not isinstance(raw_evidence, Mapping):
            problems.append("pointer_layers.evidence_not_object")
            continue
        track_name = raw_evidence.get("track_name") or raw_evidence.get("overlay_track")
        segment_id = raw_evidence.get("segment_id")
        role = str(raw_evidence.get("asset_role") or "pointer").strip().lower()
        receipt = raw_evidence.get("subject_profile_receipt")
        if isinstance(receipt, Mapping):
            receipt_asset_path = receipt.get("asset_path")
            if isinstance(receipt_asset_path, str) and receipt_asset_path.strip():
                pointer_paths.append(receipt_asset_path)
                current_hash = _sha256_file(receipt_asset_path)
                if current_hash is not None:
                    pointer_hashes.add(current_hash)
                current_size = _file_size(receipt_asset_path)
                if current_size is not None:
                    pointer_sizes.add(current_size)
            receipt_asset_hash = receipt.get("asset_sha256")
            if isinstance(receipt_asset_hash, str) and receipt_asset_hash.strip():
                pointer_hashes.add(receipt_asset_hash)

        pointer_segment: Mapping[str, Any] | None = None
        if isinstance(track_name, str) and isinstance(segment_id, str):
            allowed_tracks.add(track_name)
            allowed_segments.setdefault(track_name, set()).add(segment_id)
            role_tracks.setdefault(role, set()).add(track_name)
            tracks = _find_track(content, track_name)
            if len(tracks) == 1:
                segments = _find_segment(tracks[0], segment_id)
                if len(segments) == 1:
                    pointer_segment = segments[0]
                    pointer_records.append((raw_evidence, pointer_segment))
                    material_id = pointer_segment.get("material_id")
                    allowed_material_ids.add(material_id)
                    role_materials.setdefault(role, set()).add(material_id)
                    material = _material_by_id(content, material_id)
                    if material is not None:
                        material_path = material.get("path")
                        if isinstance(material_path, str) and material_path.strip():
                            pointer_paths.append(material_path)
                        material_hash = _sha256_file(material_path)
                        if material_hash is not None:
                            pointer_hashes.add(material_hash)
                        material_size = _file_size(material_path)
                        if material_size is not None:
                            pointer_sizes.add(material_size)

        residual_cover = raw_evidence.get("residual_pointer_cover")
        if (
            isinstance(residual_cover, Mapping)
            and residual_cover.get("mode") == "transparent_roi_still_cover"
        ):
            cover_track_name = residual_cover.get("track_name")
            cover_segment_id = residual_cover.get("segment_id")
            if isinstance(cover_track_name, str) and isinstance(cover_segment_id, str):
                allowed_tracks.add(cover_track_name)
                allowed_segments.setdefault(cover_track_name, set()).add(cover_segment_id)
                cover_tracks = _find_track(content, cover_track_name)
                if len(cover_tracks) == 1:
                    cover_segments = _find_segment(cover_tracks[0], cover_segment_id)
                    if len(cover_segments) == 1:
                        allowed_material_ids.add(cover_segments[0].get("material_id"))

        fallback = raw_evidence.get("display_safe_baked_window")
        if fallback is not None:
            if not _display_safe_contract_is_valid(fallback):
                problems.append("pointer_layers.invalid_display_safe_fallback")
            else:
                fallback_problems, saved = _saved_display_safe_problems(
                    fallback, content, pointer_segment
                )
                problems.extend(fallback_problems)
                fallback_track = str(fallback["track_name"])
                fallback_segment = str(fallback["segment_id"])
                allowed_tracks.add(fallback_track)
                allowed_segments.setdefault(fallback_track, set()).add(fallback_segment)
                if saved is not None:
                    allowed_material_ids.add(saved.get("material_id"))

    if any(len(track_names) > 1 for track_names in role_tracks.values()):
        problems.append("pointer_layers.multiple_tracks_for_role")
    if any(len(material_ids) > 1 for material_ids in role_materials.values()):
        problems.append("pointer_layers.multiple_materials_for_role")

    pointer_material_ids = {
        material.get("id")
        for material in _video_materials(content)
        if _material_matches_pointer_identity(
            material,
            pointer_paths=pointer_paths,
            pointer_hashes=pointer_hashes,
            pointer_sizes=pointer_sizes,
        )
    }
    pointer_material_ids.update(
        segment.get("material_id")
        for track in _tracks(content)
        if _POINTER_TRACK_HINT.search(str(track.get("name") or ""))
        for segment in _segments(track)
    )
    for material in _video_materials(content):
        if (
            material.get("id") in pointer_material_ids
            and material.get("id") not in allowed_material_ids
        ):
            problems.append("pointer_layers.extra_pointer_material")

    for track in _tracks(content):
        track_name = str(track.get("name") or "")
        documented_ids = allowed_segments.get(track_name, set())
        pointer_identity_segments = [
            segment
            for segment in _segments(track)
            if segment.get("material_id") in pointer_material_ids
        ]
        for segment in pointer_identity_segments:
            if track_name not in allowed_tracks:
                problems.append("pointer_layers.extra_pointer_track")
                problems.append("pointer_layers.extra_pointer_segment")
            elif segment.get("id") not in documented_ids:
                problems.append("pointer_layers.extra_pointer_segment")

    for evidence, pointer in pointer_records:
        pointer_index = _number(pointer.get("render_index"))
        underline_segments, underline_problems = _explicit_layer_segments(
            evidence, "underline_layers", content
        )
        clean_segments, clean_problems = _explicit_layer_segments(evidence, "clean_layers", content)
        problems.extend(underline_problems)
        problems.extend(clean_problems)
        underline_reference_keys = {
            (reference.get("track_name"), reference.get("segment_id"))
            for reference in evidence.get("underline_layers") or []
            if isinstance(reference, Mapping)
        }
        clean_reference_keys = {
            (reference.get("track_name"), reference.get("segment_id"))
            for reference in evidence.get("clean_layers") or []
            if isinstance(reference, Mapping)
        }
        residual_cover = evidence.get("residual_pointer_cover")
        if (
            isinstance(residual_cover, Mapping)
            and residual_cover.get("mode") == "transparent_roi_still_cover"
        ):
            clean_reference_keys.add(
                (
                    residual_cover.get("track_name"),
                    residual_cover.get("segment_id"),
                )
            )
        for track in _tracks(content):
            track_name = str(track.get("name") or "")
            for segment in _segments(track):
                if not _overlaps(pointer, segment):
                    continue
                reference_key = (track_name, segment.get("id"))
                if (
                    _UNDERLINE_TRACK_HINT.search(track_name)
                    and reference_key not in underline_reference_keys
                ):
                    problems.append("pointer_layers.underline_layer_omitted")
                if (
                    _CLEAN_TRACK_HINT.search(track_name)
                    and reference_key not in clean_reference_keys
                ):
                    problems.append("pointer_layers.clean_layer_omitted")
        overlapping_underlines = [
            segment for segment in underline_segments if _overlaps(pointer, segment)
        ]
        overlapping_clean = [segment for segment in clean_segments if _overlaps(pointer, segment)]
        for underline in overlapping_underlines:
            underline_index = _number(underline.get("render_index"))
            if underline_index is None:
                problems.append("pointer_layers.underline_render_index_missing")
            elif pointer_index is not None and pointer_index <= underline_index:
                problems.append("pointer_layers.pointer_not_above_underline")
            for clean in overlapping_clean:
                clean_index = _number(clean.get("render_index"))
                if (
                    underline_index is not None
                    and clean_index is not None
                    and _overlaps(underline, clean)
                    and underline_index <= clean_index
                ):
                    problems.append("pointer_layers.underline_not_above_clean")
        for clean in overlapping_clean:
            clean_index = _number(clean.get("render_index"))
            if clean_index is None:
                problems.append("pointer_layers.clean_render_index_missing")
            elif pointer_index is not None and pointer_index <= clean_index:
                problems.append("pointer_layers.pointer_not_above_clean")

    return sorted(set(problems))
