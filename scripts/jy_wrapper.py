# ruff: noqa: E402,I001
"""
High-level JianYing wrapper built on top of pyJianYingDraft.
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from utils.env_setup import setup_env

setup_env()

from cli.jy_wrapper_parser import build_parser
from utils.asset_index import AssetIndex
from utils.cli_protocol import emit_result, make_result
from utils.console import configure_utf8_stdio
from utils.constants import SYNONYMS
from utils.draft_lock import DraftLockManager
from utils.draft_patch import (
    DraftPatchSession,
    draft_content_path,
    draft_dir_path,
    draft_root_path,
    read_draft_content,
)
from utils.draft_retention import retain_latest_project_drafts
from utils.errors import UserInputError
from utils.formatters import (
    format_srt_time,
    get_configured_jianying_draft_root,
    get_all_drafts,
    get_default_drafts_root,
    resolve_enum_with_synonyms,
    safe_tim,
)
from utils.jianying_native_delivery import (
    NativeDeliveryError,
    mirror_draft_tree,
    resolve_configured_native_target_root,
)
from utils.jianying_env import detect_jianying_environment, sync_draft_runtime_metadata
from utils.jianying_smoke import (
    SMOKE_TEXT,
    build_smoke_editability_receipt,
    smoke_editability_receipt_valid,
)
from utils.oral_video_validator import validate_oral_video_draft
from utils.revision_runner import (
    RevisionAcceptanceError,
    _derive_revision_ui_policy,
    _validate_revision_execution_preflight,
    bind_audio_delivery_plan_to_report,
    build_revision_summary,
    execute_revision_request,
    load_review_items_json,
    load_revision_request,
    normalize_pause_adjustments,
    summarize_revision_request,
    validate_revision_acceptance_variants,
    validate_saved_revision_draft,
)
from review_job import (
    cmd_review_job_cache_inspect,
    cmd_review_job_compile,
    cmd_review_job_status,
    cmd_review_job_wait_open,
    cmd_review_job_wait_resolve,
)

from core.media_ops import MediaOpsMixin
from core.mocking_ops import MockingOpsMixin
from core.inspect_ops import (
    draft_info as build_draft_info,
    find_material_global as inspect_find_material_global,
)
from core.inspect_ops import find_segment_detail as inspect_find_segment_detail
from core.inspect_ops import list_materials as inspect_list_materials
from core.inspect_ops import list_texts as inspect_list_texts
from core.inspect_ops import list_tracks as inspect_list_tracks
from core.project_base import JyProjectBase, sanitize_project_name
from core.protocol_ops import ProtocolOpsMixin
from core.review_marker_ops import ReviewMarkerOpsMixin
from core.template_ops import (
    apply_template_payload,
    build_template_payload,
    load_template_file,
    save_template_file,
)
from core.text_template_ops import TextTemplateOpsMixin
from core.text_ops import TextOpsMixin
from core.text_io_ops import (
    apply_text_ranges_to_content,
    clone_text_style_fields,
    export_srt_from_content,
    normalize_time_offset,
    parse_srt_file,
)
from core.vfx_ops import VfxOpsMixin
from sync_jy_favorite_text_assets import sync_favorite_text_assets
from utils.text_template_adapter import load_text_template_payload

try:
    import pyJianYingDraft as draft
except ImportError:
    draft = None


def _iter_tracks(tracks: Any) -> Iterable[Any]:
    if isinstance(tracks, dict):
        return tracks.values()
    if isinstance(tracks, list):
        return tracks
    return []


def _track_type_name(track: Any) -> str:
    raw_type = getattr(track, "track_type", None) or getattr(track, "type", None)
    if hasattr(raw_type, "name"):
        return str(raw_type.name)
    if hasattr(raw_type, "value"):
        return str(raw_type.value)
    return str(raw_type or "")


def _segment_type_name(segment: Any) -> str:
    raw_type = getattr(segment, "segment_type", None) or getattr(segment, "type", None)
    if hasattr(raw_type, "name"):
        return str(raw_type.name)
    if hasattr(raw_type, "value"):
        return str(raw_type.value)
    return str(raw_type or segment.__class__.__name__)


def _serialize_segment(segment: Any, track_name: str) -> Dict[str, Any]:
    timerange = getattr(segment, "target_timerange", None)
    start_us = getattr(timerange, "start", 0) if timerange else 0
    duration_us = getattr(timerange, "duration", 0) if timerange else 0
    return {
        "segment_id": getattr(segment, "segment_id", ""),
        "track_name": track_name,
        "track_type": "",
        "segment_type": _segment_type_name(segment),
        "start_us": start_us,
        "duration_us": duration_us,
        "material_id": getattr(segment, "material_id", None),
        "resource_id": getattr(segment, "resource_id", None),
    }


def _draft_path(drafts_root: Optional[str], name: str) -> str:
    return draft_dir_path(drafts_root, name)


def _content_path(drafts_root: Optional[str], name: str) -> str:
    return draft_content_path(drafts_root, name)


def _read_draft_content(drafts_root: Optional[str], name: str) -> Dict[str, Any]:
    return read_draft_content(drafts_root, name)


def _load_latest_readable_backup_content(draft_path: str) -> Optional[Dict[str, Any]]:
    backup_root = os.path.join(draft_path, ".backup")
    if not os.path.isdir(backup_root):
        return None
    candidates = []
    for root, _dirs, files in os.walk(backup_root):
        for filename in files:
            lower_name = filename.lower()
            if not (lower_name.endswith(".bak") and ".load." in lower_name):
                continue
            path = os.path.join(root, filename)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            candidates.append((stat.st_mtime, path))
    for _mtime, path in sorted(candidates, reverse=True):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("tracks"):
            return payload
    return None


def _read_draft_content_for_validation(drafts_root: Optional[str], name: str) -> Dict[str, Any]:
    try:
        return _read_draft_content(drafts_root, name)
    except json.JSONDecodeError as root_exc:
        draft_path = _draft_path(drafts_root, name)
        backup_content = _load_latest_readable_backup_content(draft_path)
        if backup_content is not None:
            return backup_content

        raise UserInputError(
            "Draft content is not readable JSON. The saved JianYing draft appears to be "
            "runtime-encoded, so structural and strict acceptance validation cannot inspect it. "
            "Rebuild from a verifiable editable draft generator or export a readable draft_content.json before delivery."
        ) from root_exc


def _read_draft_content_variants_for_validation(
    drafts_root: Optional[str], name: str
) -> List[tuple[str, Dict[str, Any]]]:
    variants = [("root", _read_draft_content_for_validation(drafts_root, name))]
    draft_path = _draft_path(drafts_root, name)
    layout_path = os.path.join(draft_path, "timeline_layout.json")
    if not os.path.exists(layout_path):
        return variants

    try:
        with open(layout_path, "r", encoding="utf-8-sig") as f:
            layout = json.load(f)
        active_timeline = str(layout.get("activeTimeline") or "").strip()
        if not active_timeline:
            return variants
        active_path = os.path.join(
            draft_path,
            "Timelines",
            active_timeline,
            "draft_content.json",
        )
        if not os.path.exists(active_path):
            raise UserInputError(
                f"Saved draft variant active_timeline:{active_timeline} does not exist: "
                f"{active_path}."
            )
        with open(active_path, "r", encoding="utf-8-sig") as f:
            active_content = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise UserInputError(
            f"Active timeline content is not readable JSON for {name}: {exc}"
        ) from exc
    if not isinstance(active_content, dict):
        raise UserInputError(f"Active timeline content is not a JSON object for {name}.")
    variants.append((f"active_timeline:{active_timeline}", active_content))
    return variants


def _ensure_saved_draft(name: str, drafts_root: Optional[str]) -> Dict[str, Any]:
    path = _content_path(drafts_root, name)
    if not os.path.exists(path):
        project = _open_project(name=name, drafts_root=drafts_root, overwrite=True)
        _save_project(project)
    return _read_draft_content(drafts_root, name)


def _parse_json_arg(raw_value: Optional[str], field_name: str, expected_type: Any = None) -> Any:
    if raw_value is None:
        return None
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise UserInputError(f"Invalid JSON for {field_name}: {exc}") from exc
    if expected_type is not None and not isinstance(parsed, expected_type):
        expected_name = getattr(expected_type, "__name__", str(expected_type))
        raise UserInputError(f"{field_name} must decode to {expected_name}")
    return parsed


def _run_quietly(func, *args, **kwargs):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        return func(*args, **kwargs)


def _open_project(name: str, drafts_root: Optional[str], overwrite: bool = False):
    return _run_quietly(
        JyProject,
        name,
        drafts_root=drafts_root,
        overwrite=overwrite,
        recover_on_load_failure=overwrite,
    )


def _save_project(project) -> Dict[str, Any]:
    return _run_quietly(project.save)


def _cleanup_scratch_project(project) -> None:
    name = str(getattr(project, "name", "") or "")
    if not name.startswith("__cli_"):
        return
    draft_path = _draft_path(getattr(project, "root", None), name)
    root_path = os.path.abspath(draft_root_path(getattr(project, "root", None)))
    target_path = os.path.abspath(draft_path)
    if os.path.isdir(target_path) and os.path.basename(target_path).startswith("__cli_"):
        if os.path.commonpath([root_path, target_path]) == root_path:
            shutil.rmtree(target_path, ignore_errors=True)


def _smoke_draft_location(drafts_root: Optional[str], draft_name: str) -> tuple[str, str]:
    if sanitize_project_name(draft_name) != draft_name:
        raise UserInputError("Smoke draft name contains unsupported path characters")
    root_path = os.path.abspath(draft_root_path(drafts_root))
    target_path = os.path.abspath(_draft_path(drafts_root, draft_name))
    try:
        contained = os.path.commonpath([root_path, target_path]) == root_path
    except ValueError:
        contained = False
    direct_child = os.path.dirname(target_path) == root_path and target_path != root_path
    if not contained or not direct_child:
        raise UserInputError("Smoke draft name must resolve to one direct child of the drafts root")
    if _path_has_reparse_component(root_path) or _path_has_reparse_component(target_path):
        raise UserInputError("Smoke draft path cannot contain a filesystem reparse point")
    return root_path, target_path


def _same_path(first: str, second: str) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _path_is_reparse(path: str) -> bool:
    target_stat = os.lstat(path)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(target_stat.st_mode) or bool(
        getattr(target_stat, "st_file_attributes", 0) & reparse_flag
    )


def _path_has_reparse_component(path: str) -> bool:
    current = os.path.abspath(path)
    while True:
        if os.path.lexists(current):
            try:
                if _path_is_reparse(current):
                    return True
            except OSError as exc:
                raise UserInputError("Smoke draft path could not be validated") from exc
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _cleanup_smoke_draft(root_path: str, target_path: str) -> bool:
    try:
        contained = os.path.commonpath([root_path, target_path]) == root_path
    except ValueError:
        return False
    if not contained or os.path.dirname(target_path) != root_path or target_path == root_path:
        return False
    if not os.path.lexists(target_path):
        return True
    try:
        if _path_is_reparse(target_path):
            return False
        shutil.rmtree(target_path)
    except OSError:
        return False
    return not os.path.lexists(target_path)


def _make_patch_session(name: str, drafts_root: Optional[str]) -> DraftPatchSession:
    return DraftPatchSession(name, drafts_root=drafts_root)


def _append_text_material_from_segment(content: Dict[str, Any], text: str, args) -> Dict[str, Any]:
    scratch = _open_project(
        name=f"__cli_text_{uuid.uuid4().hex[:8]}",
        drafts_root=args.drafts_root,
        overwrite=True,
    )
    try:
        segment = scratch.add_rich_text(
            text,
            start_time=args.start_time,
            duration=args.duration,
            track_name=args.track_name,
            text_color=args.text_color,
            border_color=args.border_color,
            font=args.font,
            font_size=args.font_size,
            bold=args.bold,
            italic=args.italic,
            underline=args.underline,
            transform_x=args.transform_x,
            transform_y=args.transform_y,
            scale_x=args.scale_x,
            scale_y=args.scale_y,
            keyword=args.keyword,
            keyword_color=args.keyword_color,
            keyword_font_size=args.keyword_font_size,
            keyword_border_color=args.keyword_border_color,
            shadow=(
                _parse_json_arg(args.shadow_json, "shadow_json", dict) if args.shadow_json else None
            ),
            text_effect=args.text_effect,
            anim_in=args.anim_in,
            anim_out=args.anim_out,
            anim_loop=args.anim_loop,
        )
        material = segment.export_material()
        session = DraftPatchSession.__new__(DraftPatchSession)
        session.project_name = args.name
        session.drafts_root = args.drafts_root
        session.content = content
        return session.add_text_segment(segment, material, args.track_name)
    finally:
        _cleanup_scratch_project(scratch)


def _append_sticker_segment(content: Dict[str, Any], args) -> Dict[str, Any]:
    scratch = _open_project(
        name=f"__cli_sticker_{uuid.uuid4().hex[:8]}",
        drafts_root=args.drafts_root,
        overwrite=True,
    )
    try:
        segment = scratch.add_sticker_simple(
            sticker_id=args.sticker_id,
            start_time=args.start_time or "0s",
            duration=args.duration,
            track_name=args.track_name,
            scale=args.scale,
            transform_x=args.transform_x,
            transform_y=args.transform_y,
            rotation=args.rotation,
            alpha=args.alpha,
        )
        material = segment.export_material()
        session = DraftPatchSession.__new__(DraftPatchSession)
        session.project_name = args.name
        session.drafts_root = args.drafts_root
        session.content = content
        return session.add_sticker_segment(segment, material, args.track_name)
    finally:
        _cleanup_scratch_project(scratch)


def _append_filter_segment(content: Dict[str, Any], args) -> Dict[str, Any]:
    scratch = _open_project(
        name=f"__cli_filter_{uuid.uuid4().hex[:8]}",
        drafts_root=args.drafts_root,
        overwrite=True,
    )
    try:
        segment = scratch.add_filter_segment(
            args.filter_name,
            start_time=args.start_time or "0s",
            duration=args.duration,
            track_name=args.track_name,
            intensity=args.intensity,
        )
        material = segment.material.export_json()
        session = DraftPatchSession.__new__(DraftPatchSession)
        session.project_name = args.name
        session.drafts_root = args.drafts_root
        session.content = content
        return session.add_filter_segment(segment, material, args.track_name)
    finally:
        _cleanup_scratch_project(scratch)


def _append_image_segment(content: Dict[str, Any], args) -> Dict[str, Any]:
    scratch = _open_project(
        name=f"__cli_image_{uuid.uuid4().hex[:8]}",
        drafts_root=args.drafts_root,
        overwrite=True,
    )
    try:
        segment = scratch.add_image_simple(
            args.image_path,
            start_time=args.start_time or "0s",
            duration=args.duration,
            track_name=args.track_name,
            scale_x=args.scale_x,
            scale_y=args.scale_y,
            transform_x=args.transform_x,
            transform_y=args.transform_y,
            rotation=args.rotation,
            alpha=args.alpha,
            background_blur=args.background_blur,
        )
        material = segment.material_instance.export_json()
        session = DraftPatchSession.__new__(DraftPatchSession)
        session.project_name = args.name
        session.drafts_root = args.drafts_root
        session.content = content
        return session.add_video_segment(segment, material, args.track_name)
    finally:
        _cleanup_scratch_project(scratch)


def _append_effect_segment(
    content: Dict[str, Any], args
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    scratch = _open_project(
        name=f"__cli_effect_{uuid.uuid4().hex[:8]}",
        drafts_root=args.drafts_root,
        overwrite=True,
    )
    try:
        segment, resolved_asset = scratch.add_effect_segment(
            effect_name=args.effect_name,
            start_time=args.start_time or "0s",
            duration=args.duration,
            track_name=args.track_name,
            effect_category=args.effect_category,
            effect_query=args.effect_query,
            return_resolved_asset=True,
        )
        session = DraftPatchSession.__new__(DraftPatchSession)
        session.project_name = args.name
        session.drafts_root = args.drafts_root
        session.content = content
        return session.add_effect_segment(segment, args.track_name), resolved_asset
    finally:
        _cleanup_scratch_project(scratch)


def _append_green_screen(content: Dict[str, Any], args) -> Dict[str, Any]:
    session = DraftPatchSession.__new__(DraftPatchSession)
    session.project_name = args.name
    session.drafts_root = args.drafts_root
    session.content = content
    segment = session.find_segment(args.segment_id)
    if segment is None:
        raise UserInputError(f"Segment not found: {args.segment_id}")
    return session.add_green_screen_background(
        segment,
        background_path=args.background_path,
        chroma_color=args.chroma_color,
        chroma_strength=int(args.chroma_strength),
        edge_feather=int(args.edge_feather),
        edge_cleanup=int(args.edge_cleanup),
    )


def _append_audio_effect(
    content: Dict[str, Any], args
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    session = DraftPatchSession.__new__(DraftPatchSession)
    session.project_name = args.name
    session.drafts_root = args.drafts_root
    session.content = content
    segment = session.find_segment(args.segment_id)
    if segment is None:
        raise UserInputError(f"Segment not found: {args.segment_id}")

    category_map = {
        "scene": "audio_effect",
        "tone": "tone_effect",
        "speech_to_song": "speech_to_song",
    }
    normalized_category = str(args.effect_category or "scene").strip().lower()
    query_category = category_map.get(normalized_category)
    if not query_category:
        raise UserInputError(f"Unsupported audio effect_category: {args.effect_category}")

    resolved_asset = _resolve_asset_or_none(getattr(args, "effect_query", None), query_category)
    effect_name = args.effect_name or (resolved_asset["write_value"] if resolved_asset else None)
    if not effect_name:
        raise UserInputError("add-audio-effect requires --effect-name or --effect-query")

    params = (
        _parse_json_arg(args.params_json, "params_json", list)
        if getattr(args, "params_json", None)
        else None
    )
    result = session.add_audio_effect_to_segment(
        segment,
        effect_name=str(effect_name),
        effect_category=normalized_category,
        params=params,
    )
    return result, resolved_asset


def _append_audio_fade(content: Dict[str, Any], args) -> Dict[str, Any]:
    session = DraftPatchSession.__new__(DraftPatchSession)
    session.project_name = args.name
    session.drafts_root = args.drafts_root
    session.content = content
    segment = session.find_segment(args.segment_id)
    if segment is None:
        raise UserInputError(f"Segment not found: {args.segment_id}")
    return session.add_audio_fade_to_segment(
        segment,
        fade_in_duration=safe_tim(args.fade_in),
        fade_out_duration=safe_tim(args.fade_out),
    )


def _append_complex_text_segment(
    content: Dict[str, Any], args
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    scratch = _open_project(
        name=f"__cli_complex_{uuid.uuid4().hex[:8]}",
        drafts_root=args.drafts_root,
        overwrite=True,
    )
    try:
        segment, resolved_assets = scratch.add_complex_text(
            args.text,
            start_time=args.start_time,
            duration=args.duration,
            track_name=args.track_name,
            bubble_resource_id=args.bubble_resource_id,
            bubble_effect_id=args.bubble_effect_id,
            flower_id=args.flower_id,
            font=args.font,
            font_size=args.font_size,
            text_color=args.text_color,
            border_color=args.border_color,
            shadow=(
                _parse_json_arg(args.shadow_json, "shadow_json", dict) if args.shadow_json else None
            ),
            transform_x=args.transform_x,
            transform_y=args.transform_y,
            scale_x=args.scale_x,
            scale_y=args.scale_y,
            text_effect=args.text_effect,
            anim_in=args.anim_in,
            anim_out=args.anim_out,
            anim_loop=args.anim_loop,
            font_query=getattr(args, "font_query", None),
            anim_in_query=getattr(args, "anim_in_query", None),
            anim_out_query=getattr(args, "anim_out_query", None),
            anim_loop_query=getattr(args, "anim_loop_query", None),
            text_effect_query=getattr(args, "text_effect_query", None),
            bubble_query=getattr(args, "bubble_query", None),
            flower_query=getattr(args, "flower_query", None),
            return_resolved_assets=True,
        )
        material = segment.export_material()
        session = DraftPatchSession.__new__(DraftPatchSession)
        session.project_name = args.name
        session.drafts_root = args.drafts_root
        session.content = content
        return session.add_complex_text_segment(segment, material, args.track_name), resolved_assets
    finally:
        _cleanup_scratch_project(scratch)


def _append_text_template_segment(
    content: Dict[str, Any], args
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    scratch = _open_project(
        name=f"__cli_text_template_{uuid.uuid4().hex[:8]}",
        drafts_root=args.drafts_root,
        overwrite=True,
    )
    try:
        payload = (
            load_text_template_payload(args.template_payload_path)
            if getattr(args, "template_payload_path", None)
            else None
        )
        texts = (
            _parse_json_arg(args.texts_json, "texts_json", list)
            if getattr(args, "texts_json", None)
            else None
        )
        try:
            segment, resolved_asset = scratch.add_text_template(
                template_id=getattr(args, "template_id", None),
                template_query=getattr(args, "template_query", None),
                text=getattr(args, "text", None),
                texts=texts,
                start_time=args.start_time,
                duration=args.duration,
                track_name=args.track_name,
                template_payload=payload,
                return_resolved_asset=True,
            )
        except ValueError as exc:
            raise UserInputError(str(exc)) from exc
        session = DraftPatchSession.__new__(DraftPatchSession)
        session.project_name = args.name
        session.drafts_root = args.drafts_root
        session.content = content
        bundle = scratch._pending_text_template_bundle
        segment_payload = scratch._pending_text_template_segment_payload
        return (
            session.add_text_template_segment(segment_payload, bundle, args.track_name),
            resolved_asset,
        )
    finally:
        _cleanup_scratch_project(scratch)


def _asset_index() -> AssetIndex:
    return AssetIndex(skill_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve_asset_or_none(query: Optional[str], category: str) -> Optional[Dict[str, Any]]:
    if not query:
        return None
    return _asset_index().resolve(query, category=category)


def _resolved_write_value(
    query: Optional[str], category: str
) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    resolved = _resolve_asset_or_none(query, category)
    if not resolved:
        return None, None
    return resolved["write_value"], resolved


def _probe_app_window(allow_launch: bool = False) -> Dict[str, Any]:
    try:
        from pyJianYingDraft.jianying_controller import JianyingController

        try:
            controller = JianyingController(keep_topmost=False)
        except TypeError:
            controller = JianyingController()
        status = getattr(controller, "app_status", "")
        release_topmost = getattr(controller, "release_topmost", None)
        if callable(release_topmost):
            release_topmost()
        return {
            "ok": True,
            "attached": True,
            "launched": False,
            "status": status,
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "attached": False,
            "launched": False,
            "status": "",
            "error": str(exc),
        }


def cmd_detect_env() -> Dict[str, Any]:
    detected = detect_jianying_environment()
    return make_result(True, "ok", "", {"environment": detected})


def _draft_root_reminder(expected_root: str, detail: str = "") -> str:
    suffix = f"（{detail}）" if detail else ""
    return f"请在对方电脑创建与源电脑相同的剪映草稿路径：{expected_root}{suffix}"


def cmd_draft_root_check(
    expected_root: str,
    *,
    configured_root: str | None = None,
    local_app_data: str | None = None,
) -> Dict[str, Any]:
    """Check the recipient's fixed JianYing path without creating it."""

    expected = Path(str(expected_root or "").strip()).expanduser().resolve(strict=False)
    if not str(expected_root or "").strip():
        raise UserInputError("draft-root-check requires --expected-root")
    try:
        configured = (
            Path(configured_root).expanduser().resolve(strict=False)
            if configured_root is not None
            else get_configured_jianying_draft_root(local_app_data, require_exists=False)
        )
    except Exception as exc:
        message = _draft_root_reminder(str(expected), str(exc))
        result = make_result(
            False,
            "draft_root_required",
            message,
            {
                "status": "missing",
                "expected_root": str(expected),
                "configured_root": None,
                "needs_user_action": True,
                "message": message,
            },
        )
        result["message"] = message
        return result

    same = os.path.normcase(os.path.abspath(str(configured))) == os.path.normcase(
        os.path.abspath(str(expected))
    )
    exists = expected.is_dir()
    if same and exists:
        data = {
            "status": "ready",
            "expected_root": str(expected),
            "configured_root": str(configured),
            "needs_user_action": False,
            "message": f"剪映草稿路径已确认：{expected}",
        }
        return make_result(True, "ok", "", data)

    status = "missing" if not configured or not Path(configured).is_dir() else "mismatch"
    message = _draft_root_reminder(
        str(expected), "路径不存在" if status == "missing" else "路径不一致"
    )
    result = make_result(
        False,
        "draft_root_required",
        message,
        {
            "status": status,
            "expected_root": str(expected),
            "configured_root": str(configured) if configured else None,
            "needs_user_action": True,
            "message": message,
        },
    )
    result["message"] = message
    return result


def cmd_draft_mirror_deliver(
    name: str,
    drafts_root: str | None,
    desktop_root: str,
    receipt_json: str,
    job_input_digest: str,
    *,
    recipient_drafts_root: str | None = None,
    configured_recipient_root: str | None = None,
    local_app_data: str | None = None,
    quiet_window_seconds: float = 6.0,
    poll_interval_seconds: float = 1.0,
    stability_timeout_seconds: float = 120.0,
    stability_retries: int = 2,
) -> Dict[str, Any]:
    """Deliver a saved draft by fixed-path byte-preserving mirroring."""

    digest = str(job_input_digest or "").strip().casefold()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise UserInputError("draft-mirror-deliver requires a lowercase SHA-256 job input digest")
    if not str(name or "").strip():
        raise UserInputError("draft-mirror-deliver requires --name")
    try:
        source_root = (
            Path(drafts_root).expanduser().resolve(strict=True)
            if drafts_root
            else resolve_configured_native_target_root(local_app_data)
        )
    except NativeDeliveryError as exc:
        raise UserInputError(str(exc)) from exc

    # A source-machine process cannot inspect JianYing's setting on the
    # recipient computer.  Only an explicitly supplied configured recipient
    # root is verifiable here; never use the source root as fake remote
    # evidence, and do not block an otherwise valid source-side mirror.
    expected_root = recipient_drafts_root or str(source_root)
    if configured_recipient_root is not None:
        root_result = cmd_draft_root_check(
            expected_root,
            configured_root=configured_recipient_root,
            local_app_data=local_app_data,
        )
        if not root_result.get("ok"):
            raise UserInputError(
                str(
                    root_result.get("reason")
                    or root_result.get("message")
                    or "draft root check failed"
                )
            )
        root_check_data = dict(root_result.get("data", {}))
    else:
        root_check_data = {
            "status": "unverified",
            "path_check": "not_run",
            "expected_root": str(Path(expected_root).expanduser().resolve(strict=False)),
            "configured_root": None,
            "needs_user_action": False,
            "message": (
                "接收方电脑的 currentCustomDraftPath 未在源电脑验证；"
                "请在接收方首次使用时创建并配置与源电脑相同的路径。"
            ),
        }

    try:
        mirrored = mirror_draft_tree(
            source_draft=source_root / str(name).strip(),
            target_root=desktop_root,
            draft_name=str(name).strip(),
            receipt_path=receipt_json,
            receipt_metadata={
                "job_input_digest": digest,
                "expected_recipient_root": str(
                    Path(expected_root).expanduser().resolve(strict=False)
                ),
                "recipient_root_check": root_check_data,
            },
            quiet_window_seconds=quiet_window_seconds,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=stability_timeout_seconds,
            retry_count=stability_retries,
        )
    except NativeDeliveryError as exc:
        raise UserInputError(str(exc)) from exc
    return make_result(True, "ok", "", mirrored)


def cmd_smoke_test(args) -> Dict[str, Any]:
    env = detect_jianying_environment()
    draft_name = args.name or f"CodexSmoke_{uuid.uuid4().hex[:8]}"
    cleanup_requested = bool(getattr(args, "cleanup", False))
    root_path, draft_path = _smoke_draft_location(args.drafts_root, draft_name)
    cleanup_verified = False
    try:
        project = _open_project(name=draft_name, drafts_root=args.drafts_root, overwrite=True)
        project.add_text_simple(SMOKE_TEXT, "0s", "1s")
        save_result = _save_project(project)
        reported_path = save_result.get("draft_path") if isinstance(save_result, dict) else None
        if reported_path is not None and (
            not isinstance(reported_path, str) or not _same_path(reported_path, draft_path)
        ):
            raise RuntimeError("Smoke draft save returned an unexpected path")
        content = _read_draft_content(args.drafts_root, draft_name)
        platform_app_version = (content.get("platform", {}) or {}).get("app_version", "")

        meta_path = os.path.join(draft_path, "draft_meta_info.json")
        meta_info: Dict[str, Any] = {}
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta_info = json.load(f)

        structure = build_smoke_editability_receipt(
            content,
            meta_info,
            draft_name=draft_name,
            expected_root_path=root_path,
            expected_draft_path=draft_path,
        )
        probe = _probe_app_window(allow_launch=getattr(args, "allow_launch", False))
        draft_write = {
            "ok": smoke_editability_receipt_valid(structure),
            "draft_path": draft_path,
            "platform_app_version": platform_app_version,
            "meta_draft_name": meta_info.get("draft_name", ""),
            "meta_draft_root_path": meta_info.get("draft_root_path", ""),
            "meta_draft_fold_path": meta_info.get("draft_fold_path", ""),
            "structure": structure,
        }
    finally:
        if cleanup_requested:
            cleanup_verified = _cleanup_smoke_draft(root_path, draft_path)

    draft_write["cleaned_up"] = cleanup_requested and cleanup_verified
    if cleanup_requested:
        draft_write["ok"] = bool(draft_write["ok"] and draft_write["cleaned_up"])

    return make_result(
        bool(draft_write["ok"]),
        "ok" if draft_write["ok"] else "invalid_smoke_draft",
        "" if draft_write["ok"] else "Saved smoke draft failed editable-structure validation",
        {
            "environment": env,
            "draft_write": draft_write,
            "app_probe": probe,
        },
    )


def cmd_self_check(args) -> Dict[str, Any]:
    env = detect_jianying_environment(force_refresh=getattr(args, "refresh", False))
    smoke_result = cmd_smoke_test(args)
    smoke_data = smoke_result.get("data", {})
    draft_write = smoke_data.get("draft_write", {})
    app_probe = smoke_data.get("app_probe", {})

    checks = {
        "environment_detected": {
            "ok": bool(env.get("found")),
            "app_name": env.get("app_name", ""),
            "app_version": env.get("app_version", ""),
            "install_root": env.get("install_root", ""),
        },
        "resources_resolved": {
            "ok": bool(env.get("combine_adjust_path")),
            "combine_adjust_path": env.get("combine_adjust_path", ""),
        },
        "draft_root_available": {
            "ok": bool(env.get("drafts_root")) and os.path.isdir(env.get("drafts_root", "")),
            "drafts_root": env.get("drafts_root", ""),
        },
        "smoke_test": {
            "ok": (
                bool(smoke_result.get("ok"))
                and bool(draft_write.get("ok"))
                and smoke_editability_receipt_valid(draft_write.get("structure"))
            ),
            "draft_path": draft_write.get("draft_path", ""),
            "platform_app_version": draft_write.get("platform_app_version", ""),
            "structure": draft_write.get("structure", {}),
        },
        "live_app_attach": {
            "ok": bool(app_probe.get("ok")),
            "attached": bool(app_probe.get("attached")),
            "status": app_probe.get("status", ""),
            "error": app_probe.get("error", ""),
        },
    }

    usable = (
        checks["environment_detected"]["ok"]
        and checks["resources_resolved"]["ok"]
        and checks["draft_root_available"]["ok"]
        and checks["smoke_test"]["ok"]
    )
    summary = {
        "status": "ready" if usable else "blocked",
        "app_name": env.get("app_name", ""),
        "app_version": env.get("app_version", ""),
        "notes": (
            "Draft writing is ready; live window attach failed."
            if usable and not checks["live_app_attach"]["ok"]
            else ""
        ),
    }
    return make_result(
        usable,
        "ok" if usable else "runtime_error",
        "" if usable else "Environment self-check failed",
        {
            "usable": usable,
            "summary": summary,
            "checks": checks,
            "environment": env,
        },
    )


class JyProject(
    JyProjectBase,
    MediaOpsMixin,
    TextOpsMixin,
    TextTemplateOpsMixin,
    VfxOpsMixin,
    ProtocolOpsMixin,
    MockingOpsMixin,
    ReviewMarkerOpsMixin,
):
    def _resolve_enum(self, enum_cls, name: str):
        return resolve_enum_with_synonyms(enum_cls, name, SYNONYMS)

    def add_clip(
        self,
        media_path: str,
        source_start: Union[str, int],
        duration: Union[str, int],
        target_start: Union[str, int] = None,
        track_name: str = "VideoTrack",
        **kwargs,
    ):
        if target_start is None:
            target_start = self.get_track_duration(track_name)
        return self.add_media_safe(
            media_path,
            target_start,
            duration,
            track_name,
            source_start=source_start,
            **kwargs,
        )

    def search_assets(self, query: str, category: str = None, limit: int = 20):
        return _asset_index().search(query, category=category, limit=limit)

    def resolve_asset(self, query: str, category: str = None):
        return _asset_index().resolve(query, category=category)

    def hold_lock(self, timeout: float = None, lock_root: Optional[str] = None):
        return DraftLockManager(lock_root=lock_root).hold(self.name, timeout=timeout)

    def _run_draft_retention(self) -> Dict[str, Any]:
        if str(self.name).startswith("__cli_"):
            return {
                "enabled": False,
                "reason": "scratch_project",
            }
        try:
            result = retain_latest_project_drafts(
                draft_name=self.name,
                drafts_root=self.root,
                keep_count=3,
                max_fallback_count=1,
            )
        except Exception as exc:
            return {
                "enabled": True,
                "ok": False,
                "error": str(exc),
            }
        result["enabled"] = True
        result["ok"] = True
        return result

    def save(self, *, auto_retain: bool = True):
        if getattr(self, "is_patch_mode", False):
            content_path = self.patch_session.save(auto_retain=False)
            draft_path = os.path.dirname(content_path)
            sync_draft_runtime_metadata(draft_path, self.name, self.root)
            retention = (
                self._run_draft_retention()
                if auto_retain
                else {"enabled": False, "reason": "auto_retain_disabled"}
            )
            if retention.get("ok") is False:
                print(
                    f"[warn] Draft retention failed for '{self.name}': {retention.get('error', '')}"
                )
            return {
                "status": "SUCCESS",
                "draft_path": content_path,
                "mode": "patch",
                "retention": retention,
            }

        self.script.save()
        self._sync_dynamic_material_refs()
        self._patch_cloud_material_ids()
        self._force_activate_adjustments()

        draft_path = os.path.join(self.root, self.name)
        if self._pending_post_save_patches:
            self._patch_session = DraftPatchSession(self.name, drafts_root=self.root)
            for callback in self._pending_post_save_patches:
                callback()
            self._pending_post_save_patches = []
            self._pending_text_template_bundle = None
            self._pending_text_template_segment_payload = None
        sync_draft_runtime_metadata(draft_path, self.name, self.root)
        if os.path.exists(draft_path):
            os.utime(draft_path, None)
        retention = (
            self._run_draft_retention()
            if auto_retain
            else {"enabled": False, "reason": "auto_retain_disabled"}
        )
        if retention.get("ok") is False:
            print(f"[warn] Draft retention failed for '{self.name}': {retention.get('error', '')}")
        print(f"Project '{self.name}' saved and patched.")
        return {"status": "SUCCESS", "draft_path": draft_path, "retention": retention}


def cmd_search_assets(query: str, category: Optional[str], limit: int) -> Dict[str, Any]:
    results = _asset_index().search(query, category=category, limit=limit)
    return make_result(
        True,
        "ok",
        "",
        {
            "query": query,
            "category": category,
            "count": len(results),
            "results": results,
        },
    )


def cmd_list_asset_categories() -> Dict[str, Any]:
    categories = _asset_index().list_categories()
    return make_result(
        True,
        "ok",
        "",
        {
            "count": len(categories),
            "categories": categories,
        },
    )


def cmd_sync_favorite_text_assets() -> Dict[str, Any]:
    summary = sync_favorite_text_assets()
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    return make_result(
        True,
        "ok",
        "",
        {
            **summary,
            "favorite_text_templates_path": os.path.join(
                data_dir, "favorite_text_templates.local.csv"
            ),
            "favorite_flower_texts_path": os.path.join(data_dir, "favorite_flower_texts.local.csv"),
        },
    )


def _oral_video_flower_text_materials(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    text_materials = {
        str(item.get("id") or ""): item
        for item in (content.get("materials", {}) or {}).get("texts", []) or []
        if isinstance(item, dict) and item.get("id")
    }
    rows: List[Dict[str, Any]] = []
    for track in content.get("tracks", []) or []:
        if str(track.get("name", "") or "") != "FlowerText":
            continue
        for segment in track.get("segments", []) or []:
            material_id = str(segment.get("material_id") or "")
            material = text_materials.get(material_id)
            if material is None:
                continue
            try:
                payload = json.loads(material.get("content") or "{}")
            except Exception:
                payload = {}
            styles = payload.get("styles") or []
            first_style = styles[0] if styles else {}
            rows.append(
                {
                    "segment_id": str(segment.get("id") or ""),
                    "material_id": material_id,
                    "text": payload.get("text") or "",
                    "font_name": str(material.get("font_name") or ""),
                    "font_title": str(material.get("font_title") or ""),
                    "style_font": first_style.get("font") or {},
                    "effect_style": first_style.get("effectStyle"),
                }
            )
    return rows


def _basic_oral_video_validation_config() -> Dict[str, Any]:
    return {
        "flower_track_prefixes": ("Flower",),
        "flower_font_name": "优设标题黑",
        "flower_font_resource_id": "7068207165277737502",
        "min_main_video_segments": 1,
        "min_bgm_segments": 1,
        "min_sfx_segments": 1,
        "min_subtitle_segments": 1,
        "expected_flower_segments": 1,
        "forbidden_track_names": ["Final Video", "Final Audio", "修改标记"],
        "fail_on_flower_effect_style": True,
        "validate_voice_layout": True,
        "flower_text_materials_reader": _oral_video_flower_text_materials,
    }


def _basic_oral_video_favorite_flower_validation_config() -> Dict[str, Any]:
    config = _basic_oral_video_validation_config()
    config["fail_on_flower_effect_style"] = False
    config["allowed_flower_effect_style_ids"] = [
        "7351316503771368713",
        "7127668616656506149",
        "7127817135568538910",
        "7127678346472869134",
        "6896137858998996237",
        "6896138122774498567",
        "6896138188792925447",
        "7070138057772503583",
        "7070138924114383374",
        "6896137990788091143",
    ]
    return config


def cmd_validate_oral_video_draft(
    name: str, drafts_root: Optional[str], style: str
) -> Dict[str, Any]:
    style_name = str(style or "").strip() or "basic-oral-video"
    configs = {
        "basic-oral-video": _basic_oral_video_validation_config(),
        "basic-oral-video-favorite-flower": _basic_oral_video_favorite_flower_validation_config(),
    }
    if style_name not in configs:
        raise UserInputError(f"Unsupported oral-video validation style: {style_name}")
    draft_path = _draft_path(drafts_root, name)
    if not os.path.exists(os.path.join(draft_path, "draft_content.json")):
        raise FileNotFoundError(os.path.join(draft_path, "draft_content.json"))
    validation = validate_oral_video_draft(draft_path, configs[style_name])
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": name,
            "draft_path": draft_path,
            "style": style_name,
            "validation": validation,
        },
    )


def cmd_resolve_asset(query: str, category: Optional[str]) -> Dict[str, Any]:
    match = _asset_index().resolve(query, category=category)
    return make_result(
        True,
        "ok",
        "",
        {
            "query": query,
            "category": category,
            "match": match,
        },
    )


def cmd_list_segments(name: str, drafts_root: Optional[str]) -> Dict[str, Any]:
    segments = _make_patch_session(name, drafts_root).list_segments()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": name,
            "drafts_root": draft_root_path(drafts_root),
            "count": len(segments),
            "segments": segments,
        },
    )


def cmd_draft_info(name: str, drafts_root: Optional[str]) -> Dict[str, Any]:
    content = _ensure_saved_draft(name, drafts_root)
    info = build_draft_info(content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": name,
            "drafts_root": draft_root_path(drafts_root),
            **info,
        },
    )


def cmd_list_materials(
    name: str, drafts_root: Optional[str], bucket: Optional[str] = None
) -> Dict[str, Any]:
    content = _ensure_saved_draft(name, drafts_root)
    materials = inspect_list_materials(content, bucket=bucket)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": name,
            "drafts_root": draft_root_path(drafts_root),
            "count": len(materials),
            "materials": materials,
        },
    )


def cmd_show_material(name: str, drafts_root: Optional[str], material_id: str) -> Dict[str, Any]:
    content = _ensure_saved_draft(name, drafts_root)
    bucket, material = inspect_find_material_global(content, material_id)
    if material is None or bucket is None:
        raise UserInputError(f"Material not found: {material_id}")
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": name,
            "drafts_root": draft_root_path(drafts_root),
            "bucket": bucket,
            "material": material,
        },
    )


def cmd_list_tracks(name: str, drafts_root: Optional[str]) -> Dict[str, Any]:
    content = _ensure_saved_draft(name, drafts_root)
    tracks = inspect_list_tracks(content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": name,
            "drafts_root": draft_root_path(drafts_root),
            "count": len(tracks),
            "tracks": tracks,
        },
    )


def cmd_list_texts(name: str, drafts_root: Optional[str]) -> Dict[str, Any]:
    content = _ensure_saved_draft(name, drafts_root)
    texts = inspect_list_texts(content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": name,
            "drafts_root": draft_root_path(drafts_root),
            "count": len(texts),
            "texts": texts,
        },
    )


def cmd_show_segment(name: str, drafts_root: Optional[str], segment_id: str) -> Dict[str, Any]:
    content = _ensure_saved_draft(name, drafts_root)
    detail = inspect_find_segment_detail(content, segment_id)
    if detail is None:
        raise UserInputError(f"Segment not found: {segment_id}")
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": name,
            "drafts_root": draft_root_path(drafts_root),
            "segment": detail,
        },
    )


def cmd_add_sticker(args) -> Dict[str, Any]:
    resolved_asset = _resolve_asset_or_none(getattr(args, "sticker_query", None), "sticker")
    sticker_id = args.sticker_id or (resolved_asset["write_value"] if resolved_asset else None)
    if not sticker_id:
        raise UserInputError("add-sticker requires --sticker-id or --sticker-query")
    content = _ensure_saved_draft(args.name, args.drafts_root)
    args.sticker_id = sticker_id
    segment = _append_sticker_segment(content, args)
    _make_patch_session(args.name, args.drafts_root).content = content
    DraftPatchSession.__new__(DraftPatchSession)  # no-op to keep class referenced for tests/tools
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment": segment,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "resolved_asset": resolved_asset,
        },
    )


def cmd_add_mask(args) -> Dict[str, Any]:
    resolved_asset = _resolve_asset_or_none(getattr(args, "mask_query", None), "mask")
    mask_type = args.mask_type or (resolved_asset["write_value"] if resolved_asset else None)
    if not mask_type:
        raise UserInputError("add-mask requires --mask-type or --mask-query")
    session = _make_patch_session(args.name, args.drafts_root)
    segment = session.find_segment(args.segment_id)
    if segment is None:
        raise UserInputError(f"Segment not found: {args.segment_id}")
    mask_payload = session.add_mask_to_segment(
        segment,
        mask_type=mask_type,
        center_x=args.center_x,
        center_y=args.center_y,
        size=args.size,
        rotation=args.rotation,
        feather=args.feather,
        invert=args.invert,
        rect_width=args.rect_width,
        round_corner=args.round_corner,
    )
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment_id": args.segment_id,
            "mask_type": mask_type,
            "mask_id": mask_payload["id"],
            "draft_path": _draft_path(args.drafts_root, args.name),
            "resolved_asset": resolved_asset,
        },
    )


def cmd_add_rich_text(args) -> Dict[str, Any]:
    resolved_assets: Dict[str, Dict[str, Any]] = {}
    font_value, font_asset = _resolved_write_value(getattr(args, "font_query", None), "font")
    anim_in_value, anim_in_asset = _resolved_write_value(
        getattr(args, "anim_in_query", None), "text_animation"
    )
    anim_out_value, anim_out_asset = _resolved_write_value(
        getattr(args, "anim_out_query", None), "text_outro_animation"
    )
    anim_loop_value, anim_loop_asset = _resolved_write_value(
        getattr(args, "anim_loop_query", None), "text_loop_animation"
    )
    text_effect_value, text_effect_asset = _resolved_write_value(
        getattr(args, "text_effect_query", None), "video_effect"
    )

    if font_asset:
        resolved_assets["font"] = font_asset
        args.font = args.font or font_value
    if anim_in_asset:
        resolved_assets["anim_in"] = anim_in_asset
        args.anim_in = args.anim_in or anim_in_value
    if anim_out_asset:
        resolved_assets["anim_out"] = anim_out_asset
        args.anim_out = args.anim_out or anim_out_value
    if anim_loop_asset:
        resolved_assets["anim_loop"] = anim_loop_asset
        args.anim_loop = args.anim_loop or anim_loop_value
    if text_effect_asset:
        resolved_assets["text_effect"] = text_effect_asset
        args.text_effect = args.text_effect or text_effect_value

    content = _ensure_saved_draft(args.name, args.drafts_root)
    segment = _append_text_material_from_segment(content, args.text, args)
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment": segment,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "resolved_assets": resolved_assets,
        },
    )


def cmd_import_srt(args) -> Dict[str, Any]:
    cues = parse_srt_file(args.srt_path)
    content = _ensure_saved_draft(args.name, args.drafts_root)
    ref_material = None
    if getattr(args, "style_ref_segment_id", None):
        session = _make_patch_session(args.name, args.drafts_root)
        segment = session.find_segment(args.style_ref_segment_id)
        if segment is None or segment.track_type != "text":
            raise UserInputError(
                f"Style reference segment not found or not text: {args.style_ref_segment_id}"
            )
        _, ref_material = session.find_material(segment.data.get("material_id"))
        if ref_material is None:
            raise UserInputError(
                f"Style reference material not found: {segment.data.get('material_id')}"
            )

    imported_segments = []
    time_offset_us = normalize_time_offset(getattr(args, "time_offset", None))
    for cue in cues:
        segment_args = argparse.Namespace(**vars(args))
        segment_args.start_time = cue["start_us"] + time_offset_us
        segment_args.duration = cue["duration_us"]
        segment_args.track_name = args.track_name
        if ref_material is not None:
            cloned = clone_text_style_fields(ref_material)
            content_payload = json.loads(cloned.get("content", "{}"))
            base_style = content_payload.get("styles", [{}])[0] or {}
            segment_args.text_color = "#{:02x}{:02x}{:02x}".format(
                int(
                    round(
                        base_style.get("fill", {})
                        .get("content", {})
                        .get("solid", {})
                        .get("color", [1, 1, 1])[0]
                        * 255
                    )
                ),
                int(
                    round(
                        base_style.get("fill", {})
                        .get("content", {})
                        .get("solid", {})
                        .get("color", [1, 1, 1])[1]
                        * 255
                    )
                ),
                int(
                    round(
                        base_style.get("fill", {})
                        .get("content", {})
                        .get("solid", {})
                        .get("color", [1, 1, 1])[2]
                        * 255
                    )
                ),
            )
            segment_args.font_size = float(base_style.get("size", 5.0))
            segment_args.bold = bool(base_style.get("bold", False))
            segment_args.italic = bool(base_style.get("italic", False))
            segment_args.underline = bool(base_style.get("underline", False))
        imported_segments.append(
            _append_text_material_from_segment(content, cue["text"], segment_args)
        )
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "imported_count": len(imported_segments),
            "segments": imported_segments,
        },
    )


def cmd_export_srt(name: str, drafts_root: Optional[str]) -> Dict[str, Any]:
    content = _ensure_saved_draft(name, drafts_root)
    srt = export_srt_from_content(content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": name,
            "drafts_root": draft_root_path(drafts_root),
            "srt": srt,
        },
    )


def cmd_text_ranges(args) -> Dict[str, Any]:
    session = _make_patch_session(args.name, args.drafts_root)
    segment = session.find_segment(args.segment_id)
    if segment is None:
        raise UserInputError(f"Segment not found: {args.segment_id}")
    if segment.track_type != "text":
        raise UserInputError(f"text-ranges only supports text segments: {args.segment_id}")
    bucket, material = session.find_material(segment.data.get("material_id"))
    if bucket != "texts" or material is None:
        raise UserInputError(f"Text material not found for segment: {args.segment_id}")
    styles = _parse_json_arg(args.styles, "styles", list)
    updated = apply_text_ranges_to_content(material, styles)
    session.replace_material("texts", material["id"], updated)
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment_id": args.segment_id,
            "material_id": material["id"],
            "style_count": len(styles),
            "draft_path": _draft_path(args.drafts_root, args.name),
        },
    )


def cmd_set_text(args) -> Dict[str, Any]:
    _ensure_saved_draft(args.name, args.drafts_root)
    session = _make_patch_session(args.name, args.drafts_root)
    text = getattr(args, "text", None)
    texts = (
        _parse_json_arg(args.texts_json, "texts_json", list)
        if getattr(args, "texts_json", None)
        else None
    )
    if text is not None and texts is not None:
        raise UserInputError("set-text accepts either --text or --texts-json, not both")
    if text is None and texts is None:
        raise UserInputError("set-text requires --text or --texts-json")
    result = session.set_text_for_segment(args.segment_id, text=text, texts=texts)
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            **result,
        },
    )


def cmd_shift(args) -> Dict[str, Any]:
    _ensure_saved_draft(args.name, args.drafts_root)
    session = _make_patch_session(args.name, args.drafts_root)
    result = session.shift_segment(args.segment_id, args.offset)
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            **result,
        },
    )


def cmd_trim(args) -> Dict[str, Any]:
    _ensure_saved_draft(args.name, args.drafts_root)
    session = _make_patch_session(args.name, args.drafts_root)
    result = session.trim_segment(args.segment_id, args.start_time, args.duration)
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            **result,
        },
    )


def cmd_speed(args) -> Dict[str, Any]:
    _ensure_saved_draft(args.name, args.drafts_root)
    session = _make_patch_session(args.name, args.drafts_root)
    result = session.set_segment_speed(args.segment_id, args.multiplier)
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            **result,
        },
    )


def cmd_volume(args) -> Dict[str, Any]:
    _ensure_saved_draft(args.name, args.drafts_root)
    session = _make_patch_session(args.name, args.drafts_root)
    result = session.set_segment_volume(args.segment_id, args.level)
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            **result,
        },
    )


def cmd_opacity(args) -> Dict[str, Any]:
    _ensure_saved_draft(args.name, args.drafts_root)
    session = _make_patch_session(args.name, args.drafts_root)
    result = session.set_segment_opacity(args.segment_id, args.alpha)
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            **result,
        },
    )


def cmd_shift_all(args) -> Dict[str, Any]:
    _ensure_saved_draft(args.name, args.drafts_root)
    session = _make_patch_session(args.name, args.drafts_root)
    result = session.shift_all(args.offset, track_type=getattr(args, "track_type", None))
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            **result,
        },
    )


def cmd_batch(args) -> Dict[str, Any]:
    _ensure_saved_draft(args.name, args.drafts_root)
    session = _make_patch_session(args.name, args.drafts_root)
    ops = _parse_json_arg(args.ops_json, "ops_json", list)
    results = session.batch_edit(ops)
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "count": len(results),
            "results": results,
        },
    )


def cmd_cut(args) -> Dict[str, Any]:
    _ensure_saved_draft(args.name, args.drafts_root)
    session = _make_patch_session(args.name, args.drafts_root)
    result = session.cut_timeline(args.start_time, args.end_time)
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            **result,
        },
    )


def cmd_save_template(args) -> Dict[str, Any]:
    session = _make_patch_session(args.name, args.drafts_root)
    track, segment = None, None
    for candidate_track in session.content.get("tracks", []):
        for candidate_segment in candidate_track.get("segments", []):
            if candidate_segment.get("id") == args.segment_id:
                track, segment = candidate_track, candidate_segment
                break
        if segment is not None:
            break
    if track is None or segment is None:
        raise UserInputError(f"Segment not found: {args.segment_id}")
    payload = build_template_payload(session.content, track, segment)
    payload["template_name"] = args.template_name
    path = save_template_file(args.out, payload)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment_id": args.segment_id,
            "template_name": args.template_name,
            "template_path": os.path.abspath(path),
        },
    )


def cmd_apply_template(args) -> Dict[str, Any]:
    payload = load_template_file(args.template_path)
    content = _ensure_saved_draft(args.name, args.drafts_root)
    start_us = safe_tim(args.start_time)
    duration_us = safe_tim(args.duration)
    segment_payload, materials_payload = apply_template_payload(
        payload,
        start_us=start_us,
        duration_us=duration_us,
        text_override=getattr(args, "text", None),
    )
    session = _make_patch_session(args.name, args.drafts_root)
    session.content = content
    session.append_template_materials(materials_payload)
    segment_result = session.append_segment_json(
        payload.get("track_name") or "TemplateTrack",
        payload.get("track_type") or "text",
        segment_payload,
    )
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "segment": segment_result,
            "template_path": os.path.abspath(args.template_path),
        },
    )


def cmd_add_keyframes(args) -> Dict[str, Any]:
    session = _make_patch_session(args.name, args.drafts_root)
    segment = session.find_segment(args.segment_id)
    if segment is None:
        raise UserInputError(f"Segment not found: {args.segment_id}")
    keyframes = _parse_json_arg(args.keyframes_json, "keyframes_json", list)
    session.add_keyframes_to_segment(segment, keyframes)
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment_id": args.segment_id,
            "keyframe_count": len(keyframes),
            "draft_path": _draft_path(args.drafts_root, args.name),
        },
    )


def cmd_add_filter(args) -> Dict[str, Any]:
    resolved_asset = _resolve_asset_or_none(getattr(args, "filter_query", None), "filter")
    filter_name = args.filter_name or (resolved_asset["write_value"] if resolved_asset else None)
    if not filter_name:
        raise UserInputError("add-filter requires --filter-name or --filter-query")
    content = _ensure_saved_draft(args.name, args.drafts_root)
    args.filter_name = filter_name
    segment = _append_filter_segment(content, args)
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment": segment,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "resolved_asset": resolved_asset,
        },
    )


def cmd_add_image(args) -> Dict[str, Any]:
    content = _ensure_saved_draft(args.name, args.drafts_root)
    segment = _append_image_segment(content, args)
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment": segment,
            "draft_path": _draft_path(args.drafts_root, args.name),
        },
    )


def cmd_add_effect(args) -> Dict[str, Any]:
    content = _ensure_saved_draft(args.name, args.drafts_root)
    segment, resolved_asset = _append_effect_segment(content, args)
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment": segment,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "resolved_asset": resolved_asset,
        },
    )


def cmd_add_video_effect(args) -> Dict[str, Any]:
    args.effect_category = "scene"
    return cmd_add_effect(args)


def cmd_add_face_effect(args) -> Dict[str, Any]:
    args.effect_category = "character"
    return cmd_add_effect(args)


def cmd_add_audio_effect(args) -> Dict[str, Any]:
    content = _ensure_saved_draft(args.name, args.drafts_root)
    result, resolved_asset = _append_audio_effect(content, args)
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment": result,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "resolved_asset": resolved_asset,
        },
    )


def cmd_add_audio_fade(args) -> Dict[str, Any]:
    content = _ensure_saved_draft(args.name, args.drafts_root)
    result = _append_audio_fade(content, args)
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment": result,
            "draft_path": _draft_path(args.drafts_root, args.name),
        },
    )


def cmd_attach_material(args) -> Dict[str, Any]:
    session = _make_patch_session(args.name, args.drafts_root)
    segment = session.find_segment(args.segment_id)
    if segment is None:
        raise UserInputError(f"Segment not found: {args.segment_id}")
    if args.kind == "transition":
        category = "transition"
    else:
        if segment.track_type == "text":
            if args.animation_role == "loop":
                category = "text_loop_animation"
            elif args.animation_role == "out":
                category = "text_outro_animation"
            else:
                category = "text_animation"
        else:
            if args.animation_role == "out":
                category = "video_outro_animation"
            else:
                category = "video_intro_animation"

    resolved_asset = _resolve_asset_or_none(getattr(args, "query", None), category)
    material_name = args.material_name or (
        resolved_asset["write_value"] if resolved_asset else None
    )
    if not material_name:
        raise UserInputError("attach-material requires --material-name or --query")

    if args.kind == "transition":
        material_id = session.attach_transition_to_segment(
            segment,
            transition_name=material_name,
            duration=args.duration,
        )
    else:
        material_id = session.attach_animation_to_segment(
            segment,
            animation_name=material_name,
            animation_role=args.animation_role,
            duration=args.duration,
        )
    session.save()
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment_id": args.segment_id,
            "kind": args.kind,
            "material_id": material_id,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "resolved_asset": resolved_asset,
        },
    )


def cmd_add_complex_text(args) -> Dict[str, Any]:
    content = _ensure_saved_draft(args.name, args.drafts_root)
    segment, resolved_assets = _append_complex_text_segment(content, args)
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment": segment,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "resolved_assets": resolved_assets,
        },
    )


def cmd_add_text_template(args) -> Dict[str, Any]:
    if getattr(args, "text", None) and getattr(args, "texts_json", None):
        raise UserInputError("add-text-template accepts either --text or --texts-json, not both")
    if not getattr(args, "text", None) and not getattr(args, "texts_json", None):
        raise UserInputError("add-text-template requires --text or --texts-json")
    if (
        not getattr(args, "template_id", None)
        and not getattr(args, "template_query", None)
        and not getattr(args, "template_payload_path", None)
    ):
        raise UserInputError(
            "add-text-template requires --template-id, --template-query, or --template-payload-path"
        )

    content = _ensure_saved_draft(args.name, args.drafts_root)
    segment, resolved_asset = _append_text_template_segment(content, args)
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment": segment,
            "draft_path": _draft_path(args.drafts_root, args.name),
            "resolved_asset": resolved_asset,
        },
    )


def cmd_add_green_screen(args) -> Dict[str, Any]:
    content = _ensure_saved_draft(args.name, args.drafts_root)
    result = _append_green_screen(content, args)
    from utils.draft_patch import write_draft_content

    write_draft_content(args.drafts_root, args.name, content)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "segment": result,
            "draft_path": _draft_path(args.drafts_root, args.name),
        },
    )


def cmd_apply_zoom(args) -> Dict[str, Any]:
    if not os.path.exists(args.video):
        raise FileNotFoundError(args.video)
    if not os.path.exists(args.events_json):
        raise FileNotFoundError(args.events_json)

    project = _open_project(name=args.name, drafts_root=args.drafts_root, overwrite=True)
    segment = project.add_media_safe(args.video, "0s")
    if segment is None:
        raise UserInputError(f"Unable to add recording video: {args.video}")

    from smart_zoomer import apply_smart_zoom

    apply_smart_zoom(project, segment, args.events_json, zoom_scale=args.scale)
    result = _save_project(project)
    draft_path = result.get("draft_path") or _draft_path(args.drafts_root, args.name)
    return make_result(
        True,
        "ok",
        "",
        {
            "project_name": args.name,
            "draft_path": draft_path,
            "video": os.path.abspath(args.video),
            "events_json": os.path.abspath(args.events_json),
        },
    )


def _print_human_result(result: Dict[str, Any]) -> None:
    if result["ok"]:
        print(json.dumps(result["data"], ensure_ascii=False, indent=2))
        draft_path = result.get("data", {}).get("draft_path")
        if draft_path:
            print(f"DRAFT_PATH={draft_path}")
    else:
        print(f"Error [{result['code']}]: {result['reason']}")


def _dispatch_search_assets(args) -> Dict[str, Any]:
    if args.list_categories:
        return cmd_list_asset_categories()
    if not args.query:
        raise UserInputError("search-assets requires a query unless --list-categories is used")
    return cmd_search_assets(args.query, args.category, args.limit)


def cmd_revision_summary(request_json: str) -> Dict[str, Any]:
    try:
        summary = summarize_revision_request(request_json)
    except ValueError as exc:
        raise UserInputError(str(exc)) from exc
    return make_result(True, "ok", "", summary)


def cmd_revision_bind_audio_report(
    request_json: str,
    report_json: str,
    output_json: str,
) -> Dict[str, Any]:
    try:
        request = normalize_pause_adjustments(load_revision_request(request_json))
        with open(report_json, "r", encoding="utf-8-sig") as report_file:
            report = json.load(report_file)
        if not isinstance(report, dict):
            raise ValueError("Reverse-ASR report root must be a JSON object.")
        bound_report = bind_audio_delivery_plan_to_report(request, report)
        output_path = os.path.abspath(output_json)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        temporary_path = output_path + f".tmp-{uuid.uuid4().hex}"
        try:
            with open(temporary_path, "w", encoding="utf-8") as output_file:
                json.dump(bound_report, output_file, ensure_ascii=False, indent=2)
                output_file.write("\n")
            os.replace(temporary_path, output_path)
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise UserInputError(str(exc)) from exc
    return make_result(
        True,
        "ok",
        "",
        {
            "output_json": output_path,
            "audio_delivery_plan_sha256": bound_report["audio_delivery_plan_sha256"],
        },
    )


def cmd_revision_validate(
    request_json: str,
    *,
    doc_items_json: str = None,
    strict: bool = False,
) -> Dict[str, Any]:
    try:
        request = load_revision_request(request_json)
        request = normalize_pause_adjustments(request)
        doc_items = load_review_items_json(doc_items_json) if doc_items_json else None
    except (OSError, ValueError) as exc:
        raise UserInputError(str(exc)) from exc
    preflight_error = ""
    try:
        _validate_revision_execution_preflight(request, doc_items)
    except ValueError as exc:
        preflight_error = str(exc)
    strict = bool(strict or request.acceptance.require_final_acceptance)
    summary = build_revision_summary(request, doc_items=doc_items)
    draft_name = str(summary.get("draft_name") or "").strip()
    validation = None
    content = None
    content_variants = []
    if draft_name:
        try:
            content_variants = _read_draft_content_variants_for_validation(None, draft_name)
            _primary_variant_name, content = content_variants[0]
        except FileNotFoundError as exc:
            if strict:
                reason = f"Draft not found for structural validation: {exc}"
                validation = {
                    "ok": False,
                    "draft_name": draft_name,
                    "errors": [reason],
                    "warnings": [],
                    "failures": [
                        {
                            "gate": "draft_exists",
                            "item_id": "",
                            "status": "fail",
                            "repairable": True,
                            "reason": reason,
                        }
                    ],
                    "metrics": {"draft_exists": False},
                }
            else:
                validation = {
                    "ok": True,
                    "draft_name": draft_name,
                    "errors": [],
                    "warnings": ["Draft not found for structural validation yet."],
                    "failures": [],
                    "metrics": {},
                }
        except UserInputError as exc:
            validation = {
                "ok": False,
                "draft_name": draft_name,
                "errors": [str(exc)],
                "warnings": [],
                "metrics": {"content_readable": False},
            }
        else:
            validation = validate_saved_revision_draft(
                request,
                content,
                draft_name=draft_name,
                doc_items=doc_items,
                strict=strict,
            )
            variant_metrics = {"root": validation.get("metrics", {}).get("marker_validation", {})}
            for variant_name, variant_content in content_variants[1:]:
                variant_validation = validate_saved_revision_draft(
                    request,
                    variant_content,
                    draft_name=draft_name,
                    doc_items=doc_items,
                    strict=strict,
                )
                variant_metrics[variant_name] = variant_validation.get("metrics", {}).get(
                    "marker_validation", {}
                )
                validation["errors"].extend(
                    f"[{variant_name}] {message}" for message in variant_validation["errors"]
                )
                validation["warnings"].extend(
                    f"[{variant_name}] {message}" for message in variant_validation["warnings"]
                )
            validation["ok"] = not validation["errors"]
            validation.setdefault("metrics", {})["validated_variants"] = [
                variant_name for variant_name, _variant_content in content_variants
            ]
            validation["metrics"]["marker_validation_variants"] = variant_metrics
    payload = dict(summary)
    payload.update(_derive_revision_ui_policy(request, doc_items=doc_items))
    if validation is not None:
        payload["validation"] = validation
    acceptance_validation = None
    if strict or doc_items is not None:
        acceptance_validation = validate_revision_acceptance_variants(
            request,
            content_variants or [("root", content)],
            draft_name=draft_name,
            doc_items=doc_items,
            strict=strict,
        )
        payload["acceptance_validation"] = acceptance_validation
        if preflight_error:
            acceptance_validation.setdefault("errors", []).append(preflight_error)
            acceptance_validation.setdefault("failures", []).append(
                {
                    "gate": "audio_delivery",
                    "item_id": "",
                    "status": "fail",
                    "repairable": True,
                    "reason": preflight_error,
                }
            )
            acceptance_validation["ok"] = False
    elif preflight_error and validation is not None:
        validation.setdefault("errors", []).append(preflight_error)
        validation["ok"] = False
    if validation is not None and not validation["ok"]:
        return make_result(
            False, "invalid_revision_draft", "; ".join(validation["errors"]), payload
        )
    if acceptance_validation is not None and not acceptance_validation["ok"]:
        return make_result(
            False,
            "invalid_revision_acceptance",
            "; ".join(acceptance_validation["errors"]),
            payload,
        )
    return make_result(True, "ok", "", payload)


def cmd_revision_run(
    request_json: str,
    drafts_root: str = None,
    mock_media: bool = False,
    *,
    doc_items_json: str = None,
    strict: bool = False,
) -> Dict[str, Any]:
    try:
        request = load_revision_request(request_json)
        doc_items = load_review_items_json(doc_items_json) if doc_items_json else None
        result = execute_revision_request(
            request,
            drafts_root=drafts_root,
            mock_media=mock_media,
            strict=strict,
            doc_items=doc_items,
        )
    except RevisionAcceptanceError as exc:
        return make_result(
            False,
            "invalid_revision_acceptance",
            str(exc),
            exc.result_data,
        )
    except ValueError as exc:
        raise UserInputError(str(exc)) from exc
    return make_result(True, "ok", "", result)


def _build_command_handlers():
    return {
        "detect-env": lambda args: cmd_detect_env(),
        "draft-root-check": lambda args: cmd_draft_root_check(
            args.expected_root,
            configured_root=args.configured_root,
            local_app_data=args.local_app_data,
        ),
        "draft-mirror-deliver": lambda args: cmd_draft_mirror_deliver(
            args.name,
            args.drafts_root,
            args.desktop_root,
            args.receipt_json,
            args.job_input_digest,
            recipient_drafts_root=args.recipient_drafts_root,
            configured_recipient_root=args.configured_recipient_root,
            local_app_data=args.local_app_data,
            quiet_window_seconds=args.quiet_window_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            stability_timeout_seconds=args.stability_timeout_seconds,
            stability_retries=args.stability_retries,
        ),
        "smoke-test": cmd_smoke_test,
        "self-check": cmd_self_check,
        "search-assets": _dispatch_search_assets,
        "sync-favorite-text-assets": lambda args: cmd_sync_favorite_text_assets(),
        "resolve-asset": lambda args: cmd_resolve_asset(args.query, args.category),
        "revision-summary": lambda args: cmd_revision_summary(args.request_json),
        "revision-bind-audio-report": lambda args: cmd_revision_bind_audio_report(
            args.request_json,
            args.report_json,
            args.output_json,
        ),
        "revision-validate": lambda args: cmd_revision_validate(
            args.request_json,
            doc_items_json=args.doc_items_json,
            strict=args.strict,
        ),
        "revision-run": lambda args: cmd_revision_run(
            args.request_json,
            args.drafts_root,
            args.mock_media,
            doc_items_json=args.doc_items_json,
            strict=args.strict,
        ),
        "review-job-compile": lambda args: cmd_review_job_compile(
            args.snapshot_json,
            args.project_json,
            args.output_dir,
            context_before=args.context_before,
            context_after=args.context_after,
            full_preview=args.full_preview,
        ),
        "review-job-status": lambda args: cmd_review_job_status(
            args.state_json,
            input_digest=args.input_digest,
            tool_version=args.tool_version,
        ),
        "review-job-wait-open": lambda args: cmd_review_job_wait_open(
            args.state_json,
            args.wait_json,
        ),
        "review-job-wait-resolve": lambda args: cmd_review_job_wait_resolve(
            args.state_json,
            input_digest=args.input_digest,
            artifact_sha256=args.artifact_sha256,
            project_key=args.project_key,
            draft_path=args.draft_path,
            draft_root=args.draft_root,
        ),
        "review-job-cache-inspect": lambda args: cmd_review_job_cache_inspect(
            args.cache_root, args.namespace, args.digest
        ),
        "validate-oral-video-draft": lambda args: cmd_validate_oral_video_draft(
            args.name, args.drafts_root, args.style
        ),
        "list-segments": lambda args: cmd_list_segments(args.name, args.drafts_root),
        "draft-info": lambda args: cmd_draft_info(args.name, args.drafts_root),
        "list-materials": lambda args: cmd_list_materials(
            args.name, args.drafts_root, getattr(args, "bucket", None)
        ),
        "show-material": lambda args: cmd_show_material(
            args.name, args.drafts_root, args.material_id
        ),
        "list-tracks": lambda args: cmd_list_tracks(args.name, args.drafts_root),
        "list-texts": lambda args: cmd_list_texts(args.name, args.drafts_root),
        "show-segment": lambda args: cmd_show_segment(args.name, args.drafts_root, args.segment_id),
        "add-sticker": cmd_add_sticker,
        "add-mask": cmd_add_mask,
        "add-rich-text": cmd_add_rich_text,
        "import-srt": cmd_import_srt,
        "export-srt": lambda args: cmd_export_srt(args.name, args.drafts_root),
        "text-ranges": cmd_text_ranges,
        "set-text": cmd_set_text,
        "shift": cmd_shift,
        "trim": cmd_trim,
        "speed": cmd_speed,
        "volume": cmd_volume,
        "opacity": cmd_opacity,
        "shift-all": cmd_shift_all,
        "batch": cmd_batch,
        "cut": cmd_cut,
        "save-template": cmd_save_template,
        "apply-template": cmd_apply_template,
        "add-text-template": cmd_add_text_template,
        "add-keyframes": cmd_add_keyframes,
        "add-filter": cmd_add_filter,
        "add-image": cmd_add_image,
        "add-effect": cmd_add_effect,
        "add-video-effect": cmd_add_video_effect,
        "add-face-effect": cmd_add_face_effect,
        "add-audio-effect": cmd_add_audio_effect,
        "add-audio-fade": cmd_add_audio_fade,
        "attach-material": cmd_attach_material,
        "add-complex-text": cmd_add_complex_text,
        "add-green-screen": cmd_add_green_screen,
        "apply-zoom": cmd_apply_zoom,
    }


COMMAND_HANDLERS = _build_command_handlers()


def main(argv: Optional[List[str]] = None) -> int:
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        handler = COMMAND_HANDLERS.get(args.cmd)
        if handler is None:
            raise UserInputError(f"Unsupported command: {args.cmd}")
        recipient_root = getattr(args, "recipient_drafts_root", None) or os.environ.get(
            "AUTOCUT_RECIPIENT_DRAFT_ROOT"
        )
        if recipient_root and args.cmd not in {"draft-root-check", "draft-mirror-deliver"}:
            guard = cmd_draft_root_check(str(recipient_root))
            if not guard.get("ok"):
                result = guard
            elif getattr(args, "json", False):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = handler(args)
            else:
                result = handler(args)
        elif getattr(args, "json", False):
            with contextlib.redirect_stdout(io.StringIO()):
                result = handler(args)
        else:
            result = handler(args)
    except UserInputError as exc:
        result = make_result(False, "invalid_input", str(exc))
    except FileNotFoundError as exc:
        result = make_result(False, "not_found", str(exc))
    except Exception as exc:
        result = make_result(False, "runtime_error", str(exc))

    emit_result(result, getattr(args, "json", False))
    if not getattr(args, "json", False):
        _print_human_result(result)

    if result["ok"]:
        return 0
    if result["code"] in {"invalid_input", "not_found"}:
        return 2
    return 1


__all__ = ["JyProject", "get_default_drafts_root", "get_all_drafts", "safe_tim", "format_srt_time"]


if __name__ == "__main__":
    raise SystemExit(main())
