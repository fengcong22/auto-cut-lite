"""Non-destructive revision writer for the compact Auto-Cut workflow.

The lite workflow preserves source time while isolating ASR-resolved delete
segments on dedicated tracks. Review-document timestamps are search hints only;
they are never accepted as final spoken-word cut boundaries by themselves.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from utils.revision_markers import build_marker_plan
from utils.review_marker_layout_validation import review_marker_top_layout_problems
from utils.revision_models import (
    RevisionEdit,
    RevisionRequest,
    RevisionReviewItem,
    _visual_plan_segments,
)


LITE_TRACKS = {
    "original_video": "Original Video",
    "cut_segments": "Lite Cut Segments",
    "visual_assets": "Lite Visual Assets",
    "timing_adjusted": "Lite Timing Adjusted",
    "source_audio": "Separated Source Audio",
    "reused_audio": "Lite Reused Audio",
}
_DELETE_TOKENS = ("delete", "删除", "删掉", "剪掉", "去掉", "移除")
_TIMING_TOKENS = (
    "timing",
    "animation",
    "提前",
    "推迟",
    "延后",
    "加快",
    "变速",
    "移到",
    "挪到",
    "调到",
    "时序",
)
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}
_ALIGNMENT_PASS_STATUSES = {"pass", "passed", "ok", "validated", "complete", "completed"}
_ASR_GRANULARITIES = {"word", "character", "word_character", "word+character"}
_SHA256_HEX_LENGTH = 64
_SPOKEN_CUT_KINDS = {
    "spoken_delete",
    "speech_delete",
    "audio_delete",
    "phrase_delete",
    "range_delete",
    "ellipsis_range_delete",
    "colored_span_delete",
    "gap_delete",
    "tail_cleanup",
    "tail_particle_delete",
    "pause_delete",
}
_PRECISE_BOUNDARY_KINDS = {
    "phrase_delete",
    "range_delete",
    "ellipsis_range_delete",
    "colored_span_delete",
    "gap_delete",
    "tail_particle_delete",
}


def _runtime_components():
    # Imported lazily to avoid a revision_runner <-> lite_revision import cycle.
    from utils.revision_runner import _import_runtime_components

    return _import_runtime_components()


def _open_project(request: RevisionRequest, drafts_root: Optional[str]):
    from utils.revision_runner import _open_revision_project

    _draft, _marker, _mock_audio, _mock_video, jy_project = _runtime_components()
    return _open_revision_project(jy_project, request.project.draft_name, drafts_root=drafts_root)


def _disable_maintrack_adsorb(project: Any) -> None:
    """Make split-gap placement independent of JianYing's magnetic main track.

    JianYing treats this as a saved draft setting.  The lite layout deliberately
    keeps source-time gaps on V1/A1 and places the removed windows on V2/A2, so
    allowing the editor to apply its default magnetic-main-track behavior can
    make an opened draft appear different from the saved structure.
    """

    script = getattr(project, "script", None)
    if script is None:
        return
    if hasattr(script, "maintrack_adsorb"):
        script.maintrack_adsorb = False
    content = getattr(script, "content", None)
    if isinstance(content, dict):
        content.setdefault("config", {})["maintrack_adsorb"] = False


def _restore_lite_reused_audio_volume(
    project: Any, receipts: List[Dict[str, Any]]
) -> None:
    """Keep deleted-source audio audible on A2 for manual review.

    A2 is a reference lane in the full segmented-audio contract and is often
    declared with volume 0.  In the lite split-gap contract the user explicitly
    needs to hear the isolated deleted audio, so only this fixed lane is
    restored to its normal volume.  A1 remains the audible kept-source lane.
    """

    script = getattr(project, "script", None)
    tracks = getattr(script, "tracks", {}) if script is not None else {}
    for track in tracks.values() if isinstance(tracks, dict) else []:
        if str(getattr(track, "name", "")) != LITE_TRACKS["reused_audio"]:
            continue
        for segment in getattr(track, "segments", []) or []:
            if hasattr(segment, "volume"):
                segment.volume = 1.0
    for receipt in receipts:
        if receipt.get("track_name") == LITE_TRACKS["reused_audio"]:
            receipt["volume"] = 1.0


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == _SHA256_HEX_LENGTH and all(char in "0123456789abcdef" for char in text)


def _edit_text(edit: RevisionEdit) -> str:
    return " ".join(
        str(value or "")
        for value in (edit.op_type, edit.source_kind, edit.label, edit.detail)
    ).strip().lower()


def _edit_kind(edit: RevisionEdit) -> str:
    explicit_kind = str(edit.source_kind or "").strip().lower()
    if explicit_kind in {"animation_timing", "timing", "pointer_overlay", "visual_overlay", "visual_delete", "review_only"}:
        return "timing" if explicit_kind in {"animation_timing", "timing"} else explicit_kind
    text = _edit_text(edit)
    if any(token in text for token in _DELETE_TOKENS):
        return "cut"
    if any(token in text for token in _TIMING_TOKENS):
        return "timing"
    return "visual"


def _bounded_window(start: Any, end: Any, total_duration: float) -> Tuple[float, float]:
    try:
        source_start = float(start)
        source_end = float(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("Lite edit start/end must be numeric.") from exc
    if not math.isfinite(source_start) or not math.isfinite(source_end):
        raise ValueError("Lite edit start/end must be finite.")
    source_start = max(0.0, min(source_start, total_duration))
    source_end = max(source_start, min(source_end, total_duration))
    return source_start, source_end


def _merge_windows(windows: Iterable[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Return sorted, non-overlapping source windows for the cut lane."""
    merged: List[Tuple[float, float]] = []
    for start, end in sorted((float(start), float(end)) for start, end in windows if end > start):
        if not merged or start > merged[-1][1] + 1e-6:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _complement_windows(
    windows: Iterable[Tuple[float, float]], total_duration: float
) -> List[Tuple[float, float]]:
    """Return the source intervals that remain on the primary track."""
    result: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in _merge_windows(windows):
        if start > cursor + 1e-6:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total_duration - 1e-6:
        result.append((cursor, total_duration))
    return result


def _lite_layout(request: RevisionRequest) -> str:
    layout = str(getattr(request, "lite_cut_layout", "split_gap") or "split_gap").strip().lower()
    if layout not in {"split_gap", "copy"}:
        raise ValueError("Lite cut layout must be either 'split_gap' or 'copy'.")
    return layout


def _spoken_cut_alignment_problems(
    edit: RevisionEdit,
    review_item: Optional[RevisionReviewItem],
) -> List[str]:
    """Reject review timestamps that lack word/character ASR resolution."""

    item_id = edit.doc_item_id or "unattributed_spoken_delete"
    evidence: Dict[str, Any] = {}
    if review_item is not None and isinstance(review_item.evidence, dict):
        evidence.update(review_item.evidence)
    if isinstance(edit.evidence, dict):
        evidence.update(edit.evidence)

    problems: List[str] = []
    timestamp_role = str(
        evidence.get("review_timestamp_role") or evidence.get("rough_time_role") or ""
    ).strip().casefold()
    if timestamp_role != "search_hint":
        problems.append("review_timestamp_role must be search_hint")

    alignment = evidence.get("asr_alignment")
    if not isinstance(alignment, dict):
        problems.append("asr_alignment receipt is missing")
        alignment = {}
    status = str(alignment.get("status") or "").strip().casefold()
    if status not in _ALIGNMENT_PASS_STATUSES:
        problems.append("asr_alignment.status is not pass")
    granularity = str(alignment.get("granularity") or "").strip().casefold()
    if granularity not in _ASR_GRANULARITIES:
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
    if _as_bool(alignment.get("authoritative_cut_boundary")) is not True:
        problems.append("asr_alignment.authoritative_cut_boundary must be true")

    matched_rows = alignment.get("matches") or alignment.get("words")
    if not isinstance(matched_rows, list) or not matched_rows:
        problems.append("asr_alignment word/character matches are missing")
    else:
        previous_start = -math.inf
        for index, row in enumerate(matched_rows):
            if not isinstance(row, dict) or not str(row.get("text") or "").strip():
                problems.append(f"asr_alignment match {index + 1} has no text")
                continue
            try:
                match_start = float(row.get("start"))
                match_end = float(row.get("end"))
            except (TypeError, ValueError):
                problems.append(f"asr_alignment match {index + 1} has invalid timing")
                continue
            if (
                not math.isfinite(match_start)
                or not math.isfinite(match_end)
                or match_end <= match_start
                or match_start < previous_start
            ):
                problems.append(f"asr_alignment match {index + 1} is not a positive ordered interval")
            previous_start = match_start

    resolved = (
        alignment.get("resolved_cut_window")
        or evidence.get("resolved_cut_window")
        or evidence.get("cut_window")
    )
    if not isinstance(resolved, (list, tuple)) or len(resolved) != 2:
        problems.append("resolved_cut_window is missing")
    else:
        try:
            resolved_start, resolved_end = float(resolved[0]), float(resolved[1])
        except (TypeError, ValueError):
            problems.append("resolved_cut_window must be numeric")
        else:
            if (
                not math.isfinite(resolved_start)
                or not math.isfinite(resolved_end)
                or resolved_end <= resolved_start
            ):
                problems.append("resolved_cut_window is invalid")
            elif abs(resolved_start - edit.start) > 0.01 or abs(resolved_end - edit.end) > 0.01:
                problems.append("edit start/end do not match the ASR-resolved cut window")

    if not str(evidence.get("delete") or "").strip():
        if str(edit.source_kind or "").strip().casefold() != "gap_delete":
            problems.append("delete phrase is missing")
    if "must_keep" not in evidence:
        problems.append("must_keep field is missing")
    strategy = str(evidence.get("strategy") or "").strip().casefold()
    if strategy not in {"precision_first", "hybrid", "listening_first"}:
        problems.append("strategy is missing or invalid")
    if str(edit.source_kind or "").strip().casefold() == "colored_span_delete":
        colored_spans = evidence.get("colored_spans")
        colored_status = str(evidence.get("colored_span_status") or "").strip().casefold()
        if colored_status != "resolved" or not isinstance(colored_spans, list) or not colored_spans:
            problems.append("colored_span_delete requires resolved review-colored rich-text spans")

    source_kind = str(edit.source_kind or "").strip().casefold()
    if source_kind in _PRECISE_BOUNDARY_KINDS:
        refinement = evidence.get("boundary_refinement")
        if not isinstance(refinement, dict):
            problems.append("boundary_refinement evidence is missing")
        else:
            refinement_status = str(refinement.get("status") or "").strip().casefold()
            if refinement_status not in {"asr_character_edge", "anchor_gap", "acoustic_gap_refined"}:
                problems.append("boundary_refinement.status is invalid")
            if _as_bool(refinement.get("crossed_must_keep")) is not False:
                problems.append("boundary_refinement must prove no must_keep word was crossed")
            refinement_window = refinement.get("resolved_cut_window")
            if not isinstance(refinement_window, (list, tuple)) or len(refinement_window) != 2:
                problems.append("boundary_refinement.resolved_cut_window is missing")
            else:
                try:
                    refinement_start = float(refinement_window[0])
                    refinement_end = float(refinement_window[1])
                except (TypeError, ValueError):
                    problems.append("boundary_refinement.resolved_cut_window must be numeric")
                else:
                    if (
                        abs(refinement_start - edit.start) > 0.01
                        or abs(refinement_end - edit.end) > 0.01
                    ):
                        problems.append(
                            "boundary_refinement window does not match the edit start/end"
                        )

    return [f"Lite spoken cut {item_id}: {problem}." for problem in problems]


def _validate_spoken_cut_alignment(
    request: RevisionRequest,
    doc_items: Optional[List[RevisionReviewItem]],
) -> None:
    authoritative = doc_items if doc_items is not None else request.review_items
    by_id = {item.item_id.casefold(): item for item in authoritative}
    problems: List[str] = []
    for edit in request.edits:
        if _edit_kind(edit) != "cut":
            continue
        source_kind = str(edit.source_kind or "").strip().casefold()
        review_item = by_id.get(edit.doc_item_id.casefold()) if edit.doc_item_id else None
        review_kind = str(review_item.kind if review_item is not None else "").strip().casefold()
        if source_kind in _SPOKEN_CUT_KINDS or review_kind in _SPOKEN_CUT_KINDS:
            problems.extend(_spoken_cut_alignment_problems(edit, review_item))
    if problems:
        raise ValueError(
            "Lite spoken-word cuts require ASR-resolved boundaries; review timestamps are "
            "search hints only. " + " ".join(problems)
        )


def _spec_float(spec: Dict[str, Any], *keys: str, default: float) -> float:
    for key in keys:
        if key not in spec or spec.get(key) in (None, ""):
            continue
        try:
            value = float(spec[key])
        except (TypeError, ValueError):
            break
        if math.isfinite(value):
            return value
    return default


def _target_start(edit: RevisionEdit, default: float) -> float:
    for payload in (edit.visual_plan, edit.evidence, edit.validation):
        if not isinstance(payload, dict):
            continue
        for key in ("timeline_start", "target_start", "new_start", "move_to"):
            value = payload.get(key)
            if value in (None, ""):
                continue
            try:
                result = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(result):
                return result
    return default


def _reuse_audio(edit: RevisionEdit, kind: str) -> bool:
    if kind not in {"cut", "timing"}:
        return False
    if edit.asset_paths and all(
        os.path.splitext(path)[1].lower() in _IMAGE_EXTENSIONS for path in edit.asset_paths
    ):
        return False
    for payload in (edit.visual_plan, edit.evidence, edit.validation):
        if not isinstance(payload, dict):
            continue
        if any(_as_bool(payload.get(key)) is True for key in ("still_frame", "visual_only")):
            return False
        for key in ("reuse_audio", "include_audio", "audio_reuse", "keep_audio"):
            if key in payload:
                parsed = _as_bool(payload.get(key))
                if parsed is not None:
                    return parsed
    return True


def _material_duration_seconds(material: Any, fallback: float) -> float:
    duration_us = int(getattr(material, "duration", 0) or 0)
    if duration_us > 0:
        return duration_us / 1_000_000.0
    return fallback


def _make_video_material(draft: Any, mock_video: Any, path: str, duration: float, mock: bool):
    if mock:
        return mock_video(
            f"mock-lite-video-{os.path.basename(path) or 'source'}",
            int(round(duration * 1_000_000)),
            os.path.basename(path) or "lite-video",
            path,
        )
    return draft.VideoMaterial(path)


def _make_audio_material(draft: Any, mock_audio: Any, path: str, duration: float, mock: bool):
    if mock:
        return mock_audio(
            f"mock-lite-audio-{os.path.basename(path) or 'source'}",
            int(round(duration * 1_000_000)),
            os.path.basename(path) or "lite-audio",
            path,
        )
    return draft.AudioMaterial(path)


def _add_video_segment(
    project: Any,
    draft: Any,
    material: Any,
    *,
    track_name: str,
    timeline_start: float,
    source_start: float,
    duration: float,
    volume: float,
) -> Any:
    segment = draft.VideoSegment(
        material,
        draft.Timerange(round(timeline_start * 1_000_000), round(duration * 1_000_000)),
        source_timerange=draft.Timerange(round(source_start * 1_000_000), round(duration * 1_000_000)),
        volume=volume,
    )
    project.script.add_segment(segment, track_name)
    return segment


def _add_audio_segment(
    project: Any,
    draft: Any,
    material: Any,
    *,
    track_name: str,
    timeline_start: float,
    source_start: float,
    duration: float,
    volume: float = 1.0,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
) -> Any:
    segment = draft.AudioSegment(
        material,
        draft.Timerange(round(timeline_start * 1_000_000), round(duration * 1_000_000)),
        source_timerange=draft.Timerange(round(source_start * 1_000_000), round(duration * 1_000_000)),
        volume=volume,
    )
    project.script.add_segment(segment, track_name)
    if fade_in > 0 or fade_out > 0:
        project.add_audio_fade_to_segment(
            segment,
            fade_in=round(fade_in * 1_000_000),
            fade_out=round(fade_out * 1_000_000),
        )
    return segment


def _asset_specs(edit: RevisionEdit) -> List[Dict[str, Any]]:
    specs = list(_visual_plan_segments(edit))
    if specs:
        return [dict(spec) for spec in specs]
    return [
        {
            "asset_path": path,
            "source_start": edit.start,
            "timeline_start": edit.start,
            "duration": max(0.0, edit.end - edit.start),
        }
        for path in edit.asset_paths
    ]


def _lite_visual_results(
    request: RevisionRequest,
    segment_receipts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Translate saved lite overlays into the canonical acceptance evidence shape.

    Lite keeps every overlay on one fixed editable lane, but pointer and visual
    acceptance must still use the same saved-segment/material attribution as the
    full workflow.  Preserve the planner's binding/lifecycle/landing evidence;
    this helper only adds identities that can be known after the draft is saved.
    """

    visual_by_item: Dict[str, List[Dict[str, Any]]] = {}
    for receipt in segment_receipts:
        if receipt.get("kind") != "visual":
            continue
        item_id = str(receipt.get("item_id") or "").strip()
        if item_id:
            visual_by_item.setdefault(item_id.casefold(), []).append(receipt)

    results: List[Dict[str, Any]] = []
    for index, edit in enumerate(request.edits):
        item_id = str(edit.doc_item_id or f"edit_{index + 1:03d}")
        receipts = visual_by_item.get(item_id.casefold(), [])
        if not receipts:
            continue
        segments = [
            {
                "role": str(receipt.get("role") or "visual_overlay"),
                "asset_path": str(receipt.get("asset_path") or ""),
                "asset_type": str(receipt.get("asset_type") or "image"),
                "track_name": str(receipt.get("track_name") or ""),
                "segment_id": str(receipt.get("segment_id") or ""),
                "material_id": str(receipt.get("material_id") or ""),
                "source_start": float(receipt.get("source_start") or 0.0),
                "timeline_start": float(receipt.get("timeline_start") or 0.0),
                "duration": float(receipt.get("duration") or 0.0),
                "scale_x": float(receipt.get("scale_x") or 1.0),
                "scale_y": float(receipt.get("scale_y") or 1.0),
                "transform_x": float(receipt.get("transform_x") or 0.0),
                "transform_y": float(receipt.get("transform_y") or 0.0),
                "keyframes": [],
            }
            for receipt in receipts
        ]
        first = segments[0]
        operation = str(edit.source_kind or edit.op_type or "visual_overlay")
        results.append(
            {
                "item_id": item_id,
                "kind": operation,
                "segments": segments,
                "evidence": {
                    "status": "pass",
                    "executed": True,
                    "operation": operation,
                    "edit_type": edit.op_type,
                    "asset_path": first["asset_path"],
                    "asset_paths": [segment["asset_path"] for segment in segments],
                    "track_name": first["track_name"],
                    "track_names": [segment["track_name"] for segment in segments],
                    "segment_id": first["segment_id"],
                    "segment_ids": [segment["segment_id"] for segment in segments],
                    "material_id": first["material_id"],
                    "material_ids": [segment["material_id"] for segment in segments],
                    "overlay_track": first["track_name"],
                    "overlay_segment": first["segment_id"],
                    "overlay_segments": segments,
                    "validation": {
                        "status": "pass",
                        "method": "editable_lite_overlay_segment_written",
                    },
                },
                "validation": {
                    "status": "pass",
                    "method": "editable_lite_overlay_segment_written",
                },
            }
        )
    return results


def _requires_full_lite_acceptance(request: RevisionRequest, strict: bool) -> bool:
    acceptance = request.acceptance
    return bool(
        strict
        or acceptance.require_final_acceptance
        or acceptance.require_audio_validation
        or acceptance.require_pause_validation
        or acceptance.require_subject_pointer_binding
        or acceptance.require_pointer_lifecycle_evidence
        or acceptance.require_review_items
        or acceptance.expected_review_item_count is not None
        or acceptance.expected_review_item_ids
        or acceptance._explicit_require_visual_evidence
    )


def _add_asset_segment(
    project: Any,
    draft: Any,
    mock_video: Any,
    *,
    path: str,
    timeline_start: float,
    duration: float,
    mock_media: bool,
    total_duration: float,
    spec: Optional[Dict[str, Any]] = None,
    material_cache: Optional[Dict[str, Any]] = None,
) -> Any:
    spec = spec or {}
    extension = os.path.splitext(path)[1].lower()
    cache_key = os.path.normcase(os.path.abspath(path))
    if mock_media:
        material = material_cache.get(cache_key) if material_cache is not None else None
        if material is None:
            material = _make_video_material(draft, mock_video, path, max(duration, 0.2), True)
            if material_cache is not None:
                material_cache[cache_key] = material
        return _add_video_segment(
            project,
            draft,
            material,
            track_name=LITE_TRACKS["visual_assets"],
            timeline_start=timeline_start,
            source_start=0.0,
            duration=duration,
            volume=0.0,
        )
    if extension in _IMAGE_EXTENSIONS and material_cache is not None:
        material = material_cache.get(cache_key)
        if material is None:
            try:
                material = draft.VideoMaterial(path)
            except Exception:
                from core.media_ops import _FallbackPhotoMaterial

                material = _FallbackPhotoMaterial(path)
            material_cache[cache_key] = material
        script = getattr(project, "script", None)
        canvas_width = float(getattr(script, "width", 1920) or 1920)
        canvas_height = float(getattr(script, "height", 1080) or 1080)
        clip_settings = draft.ClipSettings(
            alpha=_spec_float(spec, "alpha", default=1.0),
            rotation=_spec_float(spec, "rotation", default=0.0),
            scale_x=_spec_float(spec, "scale_x", default=1.0),
            scale_y=_spec_float(spec, "scale_y", default=1.0),
            transform_x=_spec_float(spec, "transform_x", default=0.0) / (canvas_width / 2.0),
            transform_y=_spec_float(spec, "transform_y", default=0.0) / (canvas_height / 2.0),
        )
        segment = draft.VideoSegment(
            material,
            draft.Timerange(round(timeline_start * 1_000_000), round(duration * 1_000_000)),
            source_timerange=draft.Timerange(0, round(duration * 1_000_000)),
            clip_settings=clip_settings,
            volume=0.0,
        )
        project.script.add_segment(segment, LITE_TRACKS["visual_assets"])
        return segment
    if extension in _IMAGE_EXTENSIONS:
        return project.add_image_simple(
            path,
            start_time=f"{timeline_start:.6f}s",
            duration=f"{duration:.6f}s",
            track_name=LITE_TRACKS["visual_assets"],
            scale_x=_spec_float(spec, "scale_x", default=1.0),
            scale_y=_spec_float(spec, "scale_y", default=1.0),
            transform_x=_spec_float(spec, "transform_x", default=0.0),
            transform_y=_spec_float(spec, "transform_y", default=0.0),
            rotation=_spec_float(spec, "rotation", default=0.0),
            alpha=_spec_float(spec, "alpha", default=1.0),
        )
    if extension in _VIDEO_EXTENSIONS:
        segment = project.add_media_safe(
            path,
            start_time=f"{timeline_start:.6f}s",
            duration=f"{duration:.6f}s",
            track_name=LITE_TRACKS["visual_assets"],
        )
        if segment is not None:
            segment.volume = 0.0
        return segment
    raise ValueError(f"Unsupported lite visual asset type: {path}")


def _marker_items(
    marker_type: Any,
    request: RevisionRequest,
    doc_items: Optional[List[RevisionReviewItem]],
    total_duration: float,
) -> Tuple[List[Any], List[Dict[str, Any]], List[str]]:
    plan = build_marker_plan(request, doc_items=doc_items)
    markers: List[Any] = []
    warnings: List[str] = []
    for item in plan:
        start = max(0.0, min(float(item.start), total_duration))
        if start >= total_duration and total_duration > 0:
            start = max(0.0, total_duration - 0.01)
            warnings.append(f"Marker {item.item_id} started at the timeline end and was clamped.")
        duration = min(2.0, max(0.01, total_duration - start)) if total_duration > 0 else 0.01
        markers.append(
            marker_type(
                label=item.source_text,
                start_time=f"{start:.6f}s",
                duration=f"{duration:.6f}s",
                detail=item.source,
                item_id=item.item_id,
                source_text=item.source_text,
                verbatim_status=item.verbatim_status,
                kind=item.kind,
            )
        )
    return markers, plan, warnings


def _load_content_variants(save_result: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    from utils.revision_runner import _load_saved_draft_content_variants

    return _load_saved_draft_content_variants(save_result)


def _validate_lite_content(
    content: Dict[str, Any],
    *,
    total_duration: float,
    marker_plan: Iterable[Any],
    marker_receipts: List[Dict[str, Any]],
    reused_audio_expected: bool,
    layout: str = "split_gap",
    delete_windows: Optional[List[Tuple[float, float]]] = None,
    audio_duration: Optional[float] = None,
) -> Dict[str, Any]:
    errors: List[str] = []
    tracks = [track for track in content.get("tracks", []) if isinstance(track, dict)]
    by_name = {str(track.get("name") or ""): track for track in tracks}
    required = [
        (LITE_TRACKS["original_video"], "video"),
        (LITE_TRACKS["cut_segments"], "video"),
        (LITE_TRACKS["visual_assets"], "video"),
        (LITE_TRACKS["timing_adjusted"], "video"),
        (LITE_TRACKS["source_audio"], "audio"),
    ]
    if reused_audio_expected:
        required.append((LITE_TRACKS["reused_audio"], "audio"))
    for name, track_type in required:
        track = by_name.get(name)
        if track is None:
            errors.append(f"Missing lite track: {name}")
        elif str(track.get("type") or "") != track_type:
            errors.append(f"Lite track {name} has type {track.get('type')!r}, expected {track_type!r}.")

    saved_duration = int(content.get("duration", 0) or 0) / 1_000_000.0
    if abs(saved_duration - total_duration) > 0.01:
        errors.append(
            f"Lite draft duration changed: expected {total_duration:.3f}s, found {saved_duration:.3f}s."
        )
    if (content.get("config") or {}).get("maintrack_adsorb") is not False:
        errors.append("Lite split-gap draft must save maintrack_adsorb=false.")

    def _track_windows(track_name: str) -> List[Tuple[float, float]]:
        track = by_name.get(track_name)
        if track is None:
            return []
        windows: List[Tuple[float, float]] = []
        for segment in track.get("segments") or []:
            target = segment.get("target_timerange") or {}
            source = segment.get("source_timerange") or {}
            target_start = int(target.get("start", 0) or 0) / 1_000_000.0
            duration = int(target.get("duration", 0) or 0) / 1_000_000.0
            source_start = int(source.get("start", 0) or 0) / 1_000_000.0
            windows.append((target_start, target_start + duration, source_start))
        return windows

    def _expect_windows(track_name: str, expected: List[Tuple[float, float]], label: str) -> None:
        actual = _track_windows(track_name)
        if len(actual) != len(expected):
            errors.append(
                f"{label} segment count mismatch: expected {len(expected)}, found {len(actual)}."
            )
            return
        for index, (saved_start, saved_end, source_start) in enumerate(actual):
            expected_start, expected_end = expected[index]
            if (
                abs(saved_start - expected_start) > 0.01
                or abs(saved_end - expected_end) > 0.01
                or abs(source_start - expected_start) > 0.01
            ):
                errors.append(
                    f"{label} segment {index + 1} is not source-aligned: "
                    f"expected {expected_start:.3f}-{expected_end:.3f}, "
                    f"found {saved_start:.3f}-{saved_end:.3f} (source {source_start:.3f})."
                )

    original = by_name.get(LITE_TRACKS["original_video"])
    if layout == "split_gap":
        merged_deletes = _merge_windows(delete_windows or [])
        keep_video = _complement_windows(merged_deletes, total_duration)
        if original is not None:
            _expect_windows(LITE_TRACKS["original_video"], keep_video, "V1")
        cut_track = by_name.get(LITE_TRACKS["cut_segments"])
        if cut_track is not None:
            _expect_windows(LITE_TRACKS["cut_segments"], merged_deletes, "V2")
        audio_total = min(total_duration, float(audio_duration or total_duration))
        audio_deletes = _merge_windows(
            (start, min(end, audio_total))
            for start, end in merged_deletes
            if start < audio_total
        )
        keep_audio = _complement_windows(audio_deletes, audio_total)
        _expect_windows(LITE_TRACKS["source_audio"], keep_audio, "A1")
        if audio_deletes:
            _expect_windows(LITE_TRACKS["reused_audio"], audio_deletes, "A2")
            a2 = by_name.get(LITE_TRACKS["reused_audio"])
            if a2 is not None:
                muted = [
                    str(segment.get("id") or index + 1)
                    for index, segment in enumerate(a2.get("segments") or [])
                    if abs(float(segment.get("volume", 1.0) or 0.0) - 1.0) > 1e-6
                ]
                if muted:
                    errors.append(
                        "A2 deleted-source audio must keep normal volume: "
                        + ", ".join(muted)
                    )
    elif original is not None:
        segments = original.get("segments") or []
        if len(segments) != 1:
            errors.append("Original Video must contain exactly one full source segment in lite copy layout.")
        elif (
            int((segments[0].get("target_timerange") or {}).get("start", -1)) != 0
            or abs(
                int((segments[0].get("target_timerange") or {}).get("duration", 0)) / 1_000_000.0
                - total_duration
            )
            > 0.01
        ):
            errors.append("Original Video does not cover the unchanged source duration.")

    saved_markers = []
    texts: Dict[str, str] = {}
    for material in (content.get("materials") or {}).get("texts", []) or []:
        if not isinstance(material, dict):
            continue
        raw_content = material.get("content")
        text = ""
        if isinstance(raw_content, dict):
            text = str(raw_content.get("text") or "")
        else:
            try:
                parsed_content = json.loads(str(raw_content or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_content = {}
            if isinstance(parsed_content, dict):
                text = str(parsed_content.get("text") or "")
        texts[str(material.get("id") or "")] = text
    for track in tracks:
        if not str(track.get("name") or "").startswith("Review Marker"):
            continue
        for segment in track.get("segments") or []:
            target = segment.get("target_timerange") or {}
            saved_markers.append(
                {
                    "start": int(target.get("start", 0) or 0) / 1_000_000.0,
                    "duration": int(target.get("duration", 0) or 0) / 1_000_000.0,
                    "text": texts.get(str(segment.get("material_id") or ""), ""),
                }
            )
    planned = sorted(marker_plan, key=lambda item: (float(item.start), item.item_id))
    if len(saved_markers) != len(planned):
        errors.append(
            f"Lite marker count mismatch: expected {len(planned)}, found {len(saved_markers)}."
        )
    for marker, saved in zip(planned, sorted(saved_markers, key=lambda item: item["start"])):
        if saved["text"] != marker.source_text:
            errors.append(f"Lite marker {marker.item_id} is not verbatim.")
        expected_start = max(0.0, min(float(marker.start), total_duration))
        if expected_start >= total_duration and total_duration > 0:
            expected_start = max(0.0, total_duration - 0.01)
        if abs(saved["start"] - expected_start) > 0.001:
            errors.append(f"Lite marker {marker.item_id} is not aligned to its edit start.")
        if saved["duration"] <= 0 or saved["duration"] > 2.001:
            errors.append(f"Lite marker {marker.item_id} duration is outside the 2s rule.")
    errors.extend(review_marker_top_layout_problems(content))

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": [],
        "metrics": {
            "saved_duration_seconds": saved_duration,
            "marker_count": len(saved_markers),
            "required_tracks": [name for name, _track_type in required],
            "reused_audio_expected": reused_audio_expected,
            "lite_cut_layout": layout,
        },
        "marker_receipts": marker_receipts,
    }


def execute_lite_revision_request(
    request: RevisionRequest,
    *,
    drafts_root: Optional[str] = None,
    mock_media: bool = False,
    strict: bool = False,
    doc_items: Optional[List[RevisionReviewItem]] = None,
    acceptance_repair_callback: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    if acceptance_repair_callback is not None:
        raise ValueError("Lite mode does not support destructive acceptance repair callbacks.")

    # Keep the lite timeline contract, but do not silently bypass the full
    # workflow's evidence/preflight gates when a compiled job asks for them.
    # This is deliberately before opening JianYing so stale ASR/audio plans or
    # invalid visual volume requests cannot produce a plausible-looking draft.
    if _requires_full_lite_acceptance(request, strict):
        from utils.revision_runner import (
            _validate_revision_execution_preflight,
            _validate_visual_overlay_volumes,
        )
        from utils.revision_evidence import normalize_pause_adjustments

        request = normalize_pause_adjustments(request)
        _validate_revision_execution_preflight(request, doc_items)
        _validate_visual_overlay_volumes(request)

    draft, marker_type, mock_audio, mock_video, _jy_project = _runtime_components()
    project, write_info = _open_project(request, drafts_root)
    _disable_maintrack_adsorb(project)
    validated = False
    try:
        layout = _lite_layout(request)
        _validate_spoken_cut_alignment(request, doc_items)
        declared_duration = float(request.project.media_duration_seconds or 0.0)
        total_duration = declared_duration
        if mock_media and total_duration <= 0:
            total_duration = 30.0
        if not mock_media:
            source_probe = draft.VideoMaterial(request.project.source_video)
            detected_duration = _material_duration_seconds(source_probe, 0.0)
            if (
                declared_duration > 0
                and detected_duration > 0
                and abs(declared_duration - detected_duration) > 0.1
            ):
                raise ValueError(
                    "Lite project.media_duration_seconds does not match the source video: "
                    f"declared {declared_duration:.3f}s, detected {detected_duration:.3f}s."
                )
            total_duration = detected_duration or declared_duration
        if total_duration <= 0:
            raise ValueError("Lite mode requires a positive source video duration.")

        delete_windows = _merge_windows(
            _bounded_window(edit.start, edit.end, total_duration)
            for edit in request.edits
            if _edit_kind(edit) == "cut"
        )
        segmented_audio_delivery = request.audio_delivery_plan.mode == "segmented"
        segment_receipts: List[Dict[str, Any]] = []
        visual_material_cache: Dict[str, Any] = {}

        fixed_tracks = [
            (LITE_TRACKS["original_video"], draft.TrackType.video),
            (LITE_TRACKS["cut_segments"], draft.TrackType.video),
            (LITE_TRACKS["visual_assets"], draft.TrackType.video),
            (LITE_TRACKS["timing_adjusted"], draft.TrackType.video),
        ]
        if not segmented_audio_delivery:
            fixed_tracks.append((LITE_TRACKS["source_audio"], draft.TrackType.audio))
        for track_name, track_type in fixed_tracks:
            project.script.add_track(track_type, track_name)

        video_material = _make_video_material(
            draft,
            mock_video,
            request.project.source_video,
            total_duration,
            mock_media,
        )
        if layout == "split_gap":
            for keep_start, keep_end in _complement_windows(delete_windows, total_duration):
                _add_video_segment(
                    project,
                    draft,
                    video_material,
                    track_name=LITE_TRACKS["original_video"],
                    timeline_start=keep_start,
                    source_start=keep_start,
                    duration=keep_end - keep_start,
                    volume=0.0,
                )
            for cut_start, cut_end in delete_windows:
                _add_video_segment(
                    project,
                    draft,
                    video_material,
                    track_name=LITE_TRACKS["cut_segments"],
                    timeline_start=cut_start,
                    source_start=cut_start,
                    duration=cut_end - cut_start,
                    volume=0.0,
                )
        else:
            _add_video_segment(
                project,
                draft,
                video_material,
                track_name=LITE_TRACKS["original_video"],
                timeline_start=0.0,
                source_start=0.0,
                duration=total_duration,
                volume=0.0,
            )

        audio_path = request.project.source_audio or request.project.source_video
        audio_material = None
        audio_duration = total_duration
        source_audio_duration = total_duration
        if segmented_audio_delivery:
            from utils.revision_runner import _write_segmented_audio_delivery

            _audio_track_names, audio_delivery_receipts = _write_segmented_audio_delivery(
                project,
                request,
                draft=draft,
                MockAudioMaterial=mock_audio,
                mock_media=mock_media,
                fallback_duration=total_duration,
            )
            segment_receipts.extend(
                {**receipt, "kind": "audio_delivery"}
                for receipt in audio_delivery_receipts
            )
            _restore_lite_reused_audio_volume(project, segment_receipts)
            reused_audio_expected = any(
                segment.track_name == LITE_TRACKS["reused_audio"]
                for segment in request.audio_delivery_plan.segments
            )
            planned_audio_end = max(
                (
                    segment.timeline_start + segment.duration
                    for segment in request.audio_delivery_plan.segments
                    if segment.track_name
                    in {LITE_TRACKS["source_audio"], LITE_TRACKS["reused_audio"]}
                ),
                default=total_duration,
            )
            audio_duration = min(total_duration, planned_audio_end)
            source_audio_duration = audio_duration
        else:
            audio_material = _make_audio_material(
                draft,
                mock_audio,
                audio_path,
                total_duration,
                mock_media,
            )
            audio_duration = _material_duration_seconds(audio_material, total_duration)
            source_audio_duration = min(total_duration, audio_duration)
            if layout == "split_gap":
                audio_delete_windows = _merge_windows(
                    (start, min(end, source_audio_duration))
                    for start, end in delete_windows
                    if start < source_audio_duration
                )
                for keep_start, keep_end in _complement_windows(
                    audio_delete_windows, source_audio_duration
                ):
                    _add_audio_segment(
                        project,
                        draft,
                        audio_material,
                        track_name=LITE_TRACKS["source_audio"],
                        timeline_start=keep_start,
                        source_start=keep_start,
                        duration=keep_end - keep_start,
                    )
                if audio_delete_windows:
                    project.script.add_track(draft.TrackType.audio, LITE_TRACKS["reused_audio"])
                    for cut_start, cut_end in audio_delete_windows:
                        _add_audio_segment(
                            project,
                            draft,
                            audio_material,
                            track_name=LITE_TRACKS["reused_audio"],
                            timeline_start=cut_start,
                            source_start=cut_start,
                            duration=cut_end - cut_start,
                        )
                    reused_audio_expected = True
                else:
                    reused_audio_expected = False
            else:
                _add_audio_segment(
                    project,
                    draft,
                    audio_material,
                    track_name=LITE_TRACKS["source_audio"],
                    timeline_start=0.0,
                    source_start=0.0,
                    duration=source_audio_duration,
                )

        audio_requests: List[Tuple[float, float, float, str]] = []
        if layout == "copy" and not segmented_audio_delivery:
            reused_audio_expected = False
        for idx, edit in enumerate(request.edits):
            kind = _edit_kind(edit)
            if kind == "timing":
                # Lite execution intentionally leaves picture timing requests as labels only.
                continue
            source_start, source_end = _bounded_window(edit.start, edit.end, total_duration)
            duration = source_end - source_start
            if duration <= 0 and kind in {"cut", "timing"}:
                continue

            if kind == "cut":
                target_start = source_start
                target_track = LITE_TRACKS["cut_segments"]
                source_path = request.project.source_video
                if layout == "split_gap":
                    # Merged V2 segments are written before this loop so overlapping
                    # review rows do not create duplicate timeline clips.
                    segment = None
                else:
                    segment = _add_video_segment(
                        project,
                        draft,
                        video_material,
                        track_name=target_track,
                        timeline_start=target_start,
                        source_start=source_start,
                        duration=duration,
                        volume=0.0,
                    )
                if (
                    layout == "copy"
                    and not segmented_audio_delivery
                    and _reuse_audio(edit, kind)
                ):
                    reusable_duration = min(duration, max(0.0, audio_duration - source_start))
                    if reusable_duration > 0:
                        reused_audio_expected = True
                        audio_requests.append(
                            (target_start, source_start, reusable_duration, edit.doc_item_id)
                        )
            elif kind == "timing":
                target_start = max(0.0, min(_target_start(edit, source_start), total_duration))
                duration = min(duration, max(0.0, total_duration - target_start))
                if duration <= 0:
                    raise ValueError(
                        f"Lite timing edit {edit.doc_item_id or idx + 1} has no in-range target duration."
                    )
                target_track = LITE_TRACKS["timing_adjusted"]
                segment = _add_video_segment(
                    project,
                    draft,
                    video_material,
                    track_name=target_track,
                    timeline_start=target_start,
                    source_start=source_start,
                    duration=duration,
                    volume=0.0,
                )
                if (
                    layout == "copy"
                    and not segmented_audio_delivery
                    and _reuse_audio(edit, kind)
                    and duration > 0
                ):
                    reusable_duration = min(duration, max(0.0, audio_duration - source_start))
                    if reusable_duration > 0:
                        reused_audio_expected = True
                        audio_requests.append(
                            (target_start, source_start, reusable_duration, edit.doc_item_id)
                        )
            else:
                segment = None
                target_start = source_start

            if segment is not None or (kind == "cut" and layout == "split_gap"):
                segment_receipts.append(
                    {
                        "item_id": edit.doc_item_id or f"edit_{idx + 1:03d}",
                        "kind": kind,
                        "track_name": target_track,
                        "segment_id": str(getattr(segment, "segment_id", "")) if segment is not None else "merged",
                        "material_id": str(getattr(segment, "material_id", "")) if segment is not None else str(getattr(video_material, "material_id", "")),
                        "source_start": source_start,
                        "timeline_start": target_start,
                        "duration": duration,
                        "reuse_audio": _reuse_audio(edit, kind),
                    }
                )

            for spec in _asset_specs(edit):
                asset_path = str(spec.get("asset_path") or "").strip()
                if not asset_path:
                    continue
                if kind in {"cut", "timing"} and os.path.normcase(os.path.abspath(asset_path)) == os.path.normcase(
                    os.path.abspath(request.project.source_video)
                ):
                    continue
                if not mock_media and not os.path.exists(asset_path):
                    raise FileNotFoundError(asset_path)
                visual_start = max(
                    0.0,
                    min(_spec_float(spec, "timeline_start", "start", default=source_start), total_duration),
                )
                available_visual_duration = max(0.0, total_duration - visual_start)
                if available_visual_duration <= 0:
                    raise ValueError(f"Lite visual asset starts outside the project: {asset_path}")
                visual_duration = min(
                    max(0.01, _spec_float(spec, "duration", default=max(0.01, duration))),
                    available_visual_duration,
                )
                asset_segment = _add_asset_segment(
                    project,
                    draft,
                    mock_video,
                    path=asset_path,
                    timeline_start=visual_start,
                    duration=visual_duration,
                    mock_media=mock_media,
                    total_duration=total_duration,
                    spec=spec,
                    material_cache=visual_material_cache,
                )
                if asset_segment is None:
                    raise RuntimeError(f"Lite visual asset failed to import: {asset_path}")
                segment_receipts.append(
                    {
                        "item_id": edit.doc_item_id or f"edit_{idx + 1:03d}",
                        "kind": "visual",
                        "role": str(spec.get("role") or "visual_overlay"),
                        "asset_type": (
                            "video"
                            if os.path.splitext(asset_path)[1].lower() in _VIDEO_EXTENSIONS
                            else "image"
                        ),
                        "track_name": LITE_TRACKS["visual_assets"],
                        "segment_id": str(getattr(asset_segment, "segment_id", "")),
                        "material_id": str(getattr(asset_segment, "material_id", "")),
                        "asset_path": asset_path,
                        "source_start": _spec_float(spec, "source_start", default=0.0),
                        "timeline_start": visual_start,
                        "duration": visual_duration,
                        "scale_x": _spec_float(spec, "scale_x", default=1.0),
                        "scale_y": _spec_float(spec, "scale_y", default=1.0),
                        "transform_x": _spec_float(spec, "transform_x", default=0.0),
                        "transform_y": _spec_float(spec, "transform_y", default=0.0),
                        "audio_preserved": False,
                    }
                )

        if reused_audio_expected and not segmented_audio_delivery:
            if layout == "copy":
                project.script.add_track(draft.TrackType.audio, LITE_TRACKS["reused_audio"])
            for timeline_start, source_start, duration, item_id in audio_requests:
                audio_segment = _add_audio_segment(
                    project,
                    draft,
                    audio_material,
                    track_name=LITE_TRACKS["reused_audio"],
                    timeline_start=timeline_start,
                    source_start=source_start,
                    duration=duration,
                )
                segment_receipts.append(
                    {
                        "item_id": item_id,
                        "kind": "reused_audio",
                        "track_name": LITE_TRACKS["reused_audio"],
                        "segment_id": str(getattr(audio_segment, "segment_id", "")),
                        "material_id": str(getattr(audio_segment, "material_id", "")),
                        "source_start": source_start,
                        "timeline_start": timeline_start,
                        "duration": duration,
                    }
                )

        markers, marker_plan, marker_warnings = _marker_items(
            marker_type,
            request,
            doc_items,
            total_duration,
        )
        marker_receipts = project.add_review_markers(markers)
        marker_receipt_dicts = [
            {
                "item_id": item.item_id,
                "source_text": item.source_text,
                "segment_id": item.segment_id,
                "material_id": item.material_id,
                "track_name": item.track_name,
                "start_time": item.start_time,
                "duration": item.duration,
            }
            for item in marker_receipts
        ]

        save_result = project.save(auto_retain=False)
        variants = _load_content_variants(save_result)
        validations = []
        for variant_name, content in variants:
            variant_validation = _validate_lite_content(
                content,
                total_duration=total_duration,
                marker_plan=marker_plan,
                marker_receipts=marker_receipt_dicts,
                reused_audio_expected=reused_audio_expected,
                layout=layout,
                delete_windows=delete_windows,
                audio_duration=source_audio_duration,
            )
            validations.append((variant_name, variant_validation))
        primary_name, primary_validation = validations[0]
        validation = {
            **primary_validation,
            "errors": list(primary_validation["errors"]),
            "warnings": list(primary_validation["warnings"]),
            "metrics": dict(primary_validation["metrics"]),
        }
        variant_metrics = {primary_name: dict(primary_validation["metrics"])}
        for variant_name, variant_validation in validations[1:]:
            variant_metrics[variant_name] = dict(variant_validation["metrics"])
            validation["errors"].extend(
                f"[{variant_name}] {message}" for message in variant_validation["errors"]
            )
            validation["warnings"].extend(
                f"[{variant_name}] {message}" for message in variant_validation["warnings"]
            )
        validation["ok"] = not validation["errors"]
        validation["metrics"]["validated_variants"] = [name for name, _content in variants]
        validation["metrics"]["lite_validation_variants"] = variant_metrics
        validation["warnings"].extend(marker_warnings)
        if not validation["ok"]:
            raise RuntimeError("Lite editable draft validation failed: " + "; ".join(validation["errors"]))
        validated = True

        visual_results = _lite_visual_results(request, segment_receipts)
        from utils.revision_runner import (
            RevisionAcceptanceError,
            _merge_visual_results_into_items,
            _request_with_visual_results,
        )
        from utils.revision_validation import validate_revision_acceptance_variants

        validation_request = _request_with_visual_results(request, visual_results)
        validation_doc_items = (
            _merge_visual_results_into_items(doc_items, visual_results)
            if doc_items is not None
            else None
        )
        if _requires_full_lite_acceptance(validation_request, strict):
            acceptance_validation = validate_revision_acceptance_variants(
                validation_request,
                variants,
                draft_name=write_info["draft_name"],
                doc_items=validation_doc_items,
                strict=strict,
            )
        else:
            acceptance_validation = {
                "ok": True,
                "strict": False,
                "skipped": True,
                "reason": "No explicit full-capability acceptance gate was requested.",
                "errors": [],
                "warnings": [],
                "failures": [],
                "metrics": {"acceptance_validation_variants": {}},
            }
        if not acceptance_validation["ok"]:
            failure_reasons = [
                str(reason)
                for reason in acceptance_validation.get("errors") or []
                if str(reason).strip()
            ]
            message = "; ".join(dict.fromkeys(failure_reasons)) or "Acceptance failed."
            unresolved_item_ids = sorted(
                {
                    str(item_id).strip()
                    for item_id in acceptance_validation.get("unresolved_item_ids") or []
                    if str(item_id).strip()
                }
                | {
                    str(failure.get("item_id") or "").strip()
                    for failure in acceptance_validation.get("failures") or []
                    if isinstance(failure, dict) and str(failure.get("item_id") or "").strip()
                }
            )
            draft_path = str(save_result.get("draft_path") or "")
            raise RevisionAcceptanceError(
                f"Lite revision acceptance validation failed: {message}",
                {
                    "draft_name": write_info["draft_name"],
                    "requested_draft_name": write_info["requested_draft_name"],
                    "write_mode": write_info.get("write_mode", "requested_draft"),
                    "workflow_mode": "lite",
                    "lite_cut_layout": layout,
                    "draft_path": draft_path,
                    "draft_dir": os.path.dirname(draft_path),
                    "review_marker_count": len(marker_receipt_dicts),
                    "review_marker_receipts": marker_receipt_dicts,
                    "validation": validation,
                    "acceptance_validation": acceptance_validation,
                    "unresolved_item_ids": unresolved_item_ids,
                },
            )

        from utils.revision_runner import _run_project_retention

        retention = _run_project_retention(project)
        save_result["retention"] = retention
        return {
            "draft_name": write_info["draft_name"],
            "requested_draft_name": write_info["requested_draft_name"],
            "write_mode": write_info.get("write_mode", "requested_draft"),
            "workflow_mode": "lite",
            "lite_cut_layout": layout,
            "draft_path": save_result.get("draft_path", ""),
            "source_duration_seconds": total_duration,
            "tracks": list(LITE_TRACKS.values()),
            "segment_receipts": segment_receipts,
            "visual_overlay_results": visual_results,
            "review_marker_count": len(marker_receipt_dicts),
            "review_marker_receipts": marker_receipt_dicts,
            "validation": validation,
            "acceptance_validation": acceptance_validation,
            "retention": retention,
            "non_destructive": True,
            "delete_operations": (
                "split_gap_cut_boundaries" if layout == "split_gap" else "cut_boundaries_only"
            ),
            "visual_transform_policy": "request_spec",
        }
    except Exception:
        if not validated and write_info.get("cleanup_on_failure", True):
            from utils.revision_runner import _cleanup_incomplete_draft

            _cleanup_incomplete_draft(project)
        raise
