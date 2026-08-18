"""Non-destructive revision writer for the compact Auto-Cut workflow.

The lite workflow keeps the source timeline intact and writes reviewable copies
of requested segments on dedicated tracks. It intentionally does not reuse the
full workflow's destructive delete/splice mapping.
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


def _runtime_components():
    # Imported lazily to avoid a revision_runner <-> lite_revision import cycle.
    from utils.revision_runner import _import_runtime_components

    return _import_runtime_components()


def _open_project(request: RevisionRequest, drafts_root: Optional[str]):
    from utils.revision_runner import _open_revision_project

    _draft, _marker, _mock_audio, _mock_video, jy_project = _runtime_components()
    return _open_revision_project(jy_project, request.project.draft_name, drafts_root=drafts_root)


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


def _edit_text(edit: RevisionEdit) -> str:
    return " ".join(
        str(value or "")
        for value in (edit.op_type, edit.source_kind, edit.label, edit.detail)
    ).strip().lower()


def _edit_kind(edit: RevisionEdit) -> str:
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
) -> Any:
    segment = draft.AudioSegment(
        material,
        draft.Timerange(round(timeline_start * 1_000_000), round(duration * 1_000_000)),
        source_timerange=draft.Timerange(round(source_start * 1_000_000), round(duration * 1_000_000)),
        volume=1.0,
    )
    project.script.add_segment(segment, track_name)
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
) -> Any:
    extension = os.path.splitext(path)[1].lower()
    if mock_media:
        material = _make_video_material(draft, mock_video, path, max(duration, 0.2), True)
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
    if extension in _IMAGE_EXTENSIONS:
        return project.add_image_simple(
            path,
            start_time=f"{timeline_start:.6f}s",
            duration=f"{duration:.6f}s",
            track_name=LITE_TRACKS["visual_assets"],
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

    original = by_name.get(LITE_TRACKS["original_video"])
    if original is not None:
        segments = original.get("segments") or []
        if len(segments) != 1:
            errors.append("Original Video must contain exactly one full source segment in lite mode.")
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

    draft, marker_type, mock_audio, mock_video, _jy_project = _runtime_components()
    project, write_info = _open_project(request, drafts_root)
    validated = False
    try:
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

        for track_name, track_type in (
            (LITE_TRACKS["original_video"], draft.TrackType.video),
            (LITE_TRACKS["cut_segments"], draft.TrackType.video),
            (LITE_TRACKS["visual_assets"], draft.TrackType.video),
            (LITE_TRACKS["timing_adjusted"], draft.TrackType.video),
            (LITE_TRACKS["source_audio"], draft.TrackType.audio),
        ):
            project.script.add_track(track_type, track_name)

        video_material = _make_video_material(
            draft,
            mock_video,
            request.project.source_video,
            total_duration,
            mock_media,
        )
        original_segment = _add_video_segment(
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
        audio_material = _make_audio_material(
            draft,
            mock_audio,
            audio_path,
            total_duration,
            mock_media,
        )
        audio_duration = _material_duration_seconds(audio_material, total_duration)
        source_audio_duration = min(total_duration, audio_duration)
        _add_audio_segment(
            project,
            draft,
            audio_material,
            track_name=LITE_TRACKS["source_audio"],
            timeline_start=0.0,
            source_start=0.0,
            duration=source_audio_duration,
        )

        segment_receipts: List[Dict[str, Any]] = []
        audio_requests: List[Tuple[float, float, float, str]] = []
        reused_audio_expected = False
        for idx, edit in enumerate(request.edits):
            kind = _edit_kind(edit)
            source_start, source_end = _bounded_window(edit.start, edit.end, total_duration)
            duration = source_end - source_start
            if duration <= 0 and kind in {"cut", "timing"}:
                continue

            if kind == "cut":
                target_start = source_start
                target_track = LITE_TRACKS["cut_segments"]
                source_path = request.project.source_video
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
                if _reuse_audio(edit, kind):
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
                if _reuse_audio(edit, kind) and duration > 0:
                    reusable_duration = min(duration, max(0.0, audio_duration - source_start))
                    if reusable_duration > 0:
                        reused_audio_expected = True
                        audio_requests.append(
                            (target_start, source_start, reusable_duration, edit.doc_item_id)
                        )
            else:
                segment = None
                target_start = source_start

            if segment is not None:
                segment_receipts.append(
                    {
                        "item_id": edit.doc_item_id or f"edit_{idx + 1:03d}",
                        "kind": kind,
                        "track_name": target_track,
                        "segment_id": str(getattr(segment, "segment_id", "")),
                        "material_id": str(getattr(segment, "material_id", "")),
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
                )
                if asset_segment is None:
                    raise RuntimeError(f"Lite visual asset failed to import: {asset_path}")
                segment_receipts.append(
                    {
                        "item_id": edit.doc_item_id or f"edit_{idx + 1:03d}",
                        "kind": "visual",
                        "track_name": LITE_TRACKS["visual_assets"],
                        "segment_id": str(getattr(asset_segment, "segment_id", "")),
                        "material_id": str(getattr(asset_segment, "material_id", "")),
                        "asset_path": asset_path,
                        "source_start": _spec_float(spec, "source_start", default=0.0),
                        "timeline_start": visual_start,
                        "duration": visual_duration,
                        "audio_preserved": False,
                    }
                )

        if reused_audio_expected:
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
        validations = [
            _validate_lite_content(
                content,
                total_duration=total_duration,
                marker_plan=marker_plan,
                marker_receipts=marker_receipt_dicts,
                reused_audio_expected=reused_audio_expected,
            )
            for _name, content in variants
        ]
        validation = validations[0]
        validation["metrics"]["validated_variants"] = [name for name, _content in variants]
        validation["warnings"].extend(marker_warnings)
        if not validation["ok"]:
            raise RuntimeError("Lite editable draft validation failed: " + "; ".join(validation["errors"]))
        validated = True

        from utils.revision_runner import _run_project_retention

        retention = _run_project_retention(project)
        save_result["retention"] = retention
        return {
            "draft_name": write_info["draft_name"],
            "requested_draft_name": write_info["requested_draft_name"],
            "write_mode": write_info.get("write_mode", "requested_draft"),
            "workflow_mode": "lite",
            "draft_path": save_result.get("draft_path", ""),
            "source_duration_seconds": total_duration,
            "tracks": list(LITE_TRACKS.values()),
            "segment_receipts": segment_receipts,
            "review_marker_count": len(marker_receipt_dicts),
            "review_marker_receipts": marker_receipt_dicts,
            "validation": validation,
            "acceptance_validation": validation,
            "retention": retention,
            "non_destructive": True,
            "delete_operations": "cut_boundaries_only",
            "visual_transform_policy": "none",
        }
    except Exception:
        if not validated and write_info.get("cleanup_on_failure", True):
            from utils.revision_runner import _cleanup_incomplete_draft

            _cleanup_incomplete_draft(project)
        raise
