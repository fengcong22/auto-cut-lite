import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from jy_wrapper import (
    cmd_add_audio_effect,
    cmd_add_audio_fade,
    cmd_add_complex_text,
    cmd_add_effect,
    cmd_add_face_effect,
    cmd_add_filter,
    cmd_add_green_screen,
    cmd_add_image,
    cmd_add_keyframes,
    cmd_add_mask,
    cmd_add_rich_text,
    cmd_add_sticker,
    cmd_add_text_template,
    cmd_add_video_effect,
    cmd_apply_template,
    cmd_apply_zoom,
    cmd_attach_material,
    cmd_batch,
    cmd_cut,
    cmd_draft_info,
    cmd_export_srt,
    cmd_import_srt,
    cmd_list_asset_categories,
    cmd_list_materials,
    cmd_list_segments,
    cmd_list_texts,
    cmd_list_tracks,
    cmd_opacity,
    cmd_resolve_asset,
    cmd_save_template,
    cmd_search_assets,
    cmd_self_check,
    cmd_set_text,
    cmd_shift,
    cmd_shift_all,
    cmd_show_material,
    cmd_show_segment,
    cmd_smoke_test,
    cmd_speed,
    cmd_sync_favorite_text_assets,
    cmd_text_ranges,
    cmd_trim,
    cmd_validate_oral_video_draft,
    cmd_volume,
)
from utils.cli_protocol import make_result
from utils.errors import UserInputError
from utils.jianying_env import detect_jianying_environment


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]


def _namespace(
    arguments: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None
) -> SimpleNamespace:
    payload = dict(defaults or {})
    payload.update(arguments or {})
    return SimpleNamespace(**payload)


def _json_string(value: Any) -> Optional[str]:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _handle_search_assets(arguments: Dict[str, Any]) -> Dict[str, Any]:
    if arguments.get("list_categories"):
        return cmd_list_asset_categories()
    query = arguments.get("query")
    if not query:
        raise UserInputError("search_assets requires query unless list_categories=true")
    return cmd_search_assets(
        query,
        arguments.get("category"),
        int(arguments.get("limit", 20)),
    )


def _handle_resolve_asset(arguments: Dict[str, Any]) -> Dict[str, Any]:
    query = arguments.get("query")
    if not query:
        raise UserInputError("resolve_asset requires query")
    return cmd_resolve_asset(query, arguments.get("category"))


def _handle_list_segments(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = arguments.get("name")
    if not name:
        raise UserInputError("list_segments requires name")
    return cmd_list_segments(name, arguments.get("drafts_root"))


def _handle_draft_info(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = arguments.get("name")
    if not name:
        raise UserInputError("draft_info requires name")
    return cmd_draft_info(name, arguments.get("drafts_root"))


def _handle_list_materials(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = arguments.get("name")
    if not name:
        raise UserInputError("list_materials requires name")
    return cmd_list_materials(name, arguments.get("drafts_root"), arguments.get("bucket"))


def _handle_show_material(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = arguments.get("name")
    material_id = arguments.get("material_id")
    if not name:
        raise UserInputError("show_material requires name")
    if not material_id:
        raise UserInputError("show_material requires material_id")
    return cmd_show_material(name, arguments.get("drafts_root"), material_id)


def _handle_list_tracks(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = arguments.get("name")
    if not name:
        raise UserInputError("list_tracks requires name")
    return cmd_list_tracks(name, arguments.get("drafts_root"))


def _handle_list_texts(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = arguments.get("name")
    if not name:
        raise UserInputError("list_texts requires name")
    return cmd_list_texts(name, arguments.get("drafts_root"))


def _handle_show_segment(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = arguments.get("name")
    segment_id = arguments.get("segment_id")
    if not name:
        raise UserInputError("show_segment requires name")
    if not segment_id:
        raise UserInputError("show_segment requires segment_id")
    return cmd_show_segment(name, arguments.get("drafts_root"), segment_id)


def _handle_add_sticker(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "sticker_id": None,
            "sticker_query": None,
            "start_time": None,
            "duration": "3s",
            "track_name": "StickerTrack",
            "scale": 1.0,
            "transform_x": 0.0,
            "transform_y": 0.0,
            "rotation": 0.0,
            "alpha": 1.0,
        },
    )
    return cmd_add_sticker(args)


def _handle_add_mask(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "mask_type": None,
            "mask_query": None,
            "center_x": 0.0,
            "center_y": 0.0,
            "size": 0.5,
            "rotation": 0.0,
            "feather": 0.0,
            "invert": False,
            "rect_width": None,
            "round_corner": None,
        },
    )
    return cmd_add_mask(args)


def _handle_add_rich_text(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        {
            **arguments,
            "shadow_json": _json_string(arguments.get("shadow_json")),
        },
        {
            "drafts_root": None,
            "start_time": None,
            "duration": "3s",
            "track_name": "Subtitles",
            "text_color": "#ffffff",
            "border_color": None,
            "font": None,
            "font_query": None,
            "font_size": 5.0,
            "bold": False,
            "italic": False,
            "underline": False,
            "transform_x": 0.0,
            "transform_y": -0.8,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "keyword": None,
            "keyword_color": "#ff7100",
            "keyword_font_size": None,
            "keyword_border_color": None,
            "shadow_json": None,
            "text_effect": None,
            "text_effect_query": None,
            "anim_in": None,
            "anim_in_query": None,
            "anim_out": None,
            "anim_out_query": None,
            "anim_loop": None,
            "anim_loop_query": None,
        },
    )
    return cmd_add_rich_text(args)


def _handle_import_srt(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "track_name": "Subtitles",
            "style_ref_segment_id": None,
            "time_offset": None,
            "text_color": "#ffffff",
            "border_color": None,
            "font": None,
            "font_size": 5.0,
            "bold": False,
            "italic": False,
            "underline": False,
            "transform_x": 0.0,
            "transform_y": -0.8,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "keyword": None,
            "keyword_color": "#ff7100",
            "keyword_font_size": None,
            "keyword_border_color": None,
            "shadow_json": None,
            "text_effect": None,
            "anim_in": None,
            "anim_out": None,
            "anim_loop": None,
        },
    )
    return cmd_import_srt(args)


def _handle_export_srt(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = arguments.get("name")
    if not name:
        raise UserInputError("export_srt requires name")
    return cmd_export_srt(name, arguments.get("drafts_root"))


def _handle_text_ranges(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        {
            **arguments,
            "styles": _json_string(arguments.get("styles")),
        },
        {
            "drafts_root": None,
        },
    )
    return cmd_text_ranges(args)


def _handle_set_text(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        {
            **arguments,
            "texts_json": _json_string(arguments.get("texts_json")),
        },
        {
            "drafts_root": None,
            "text": None,
            "texts_json": None,
        },
    )
    return cmd_set_text(args)


def _handle_shift(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
        },
    )
    return cmd_shift(args)


def _handle_trim(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
        },
    )
    return cmd_trim(args)


def _handle_speed(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
        },
    )
    return cmd_speed(args)


def _handle_volume(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
        },
    )
    return cmd_volume(args)


def _handle_opacity(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
        },
    )
    return cmd_opacity(args)


def _handle_shift_all(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "track_type": None,
        },
    )
    return cmd_shift_all(args)


def _handle_batch(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        {
            **arguments,
            "ops_json": _json_string(arguments.get("ops_json")),
        },
        {
            "drafts_root": None,
        },
    )
    return cmd_batch(args)


def _handle_cut(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
        },
    )
    return cmd_cut(args)


def _handle_add_keyframes(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        {
            **arguments,
            "keyframes_json": _json_string(arguments.get("keyframes_json")),
        },
        {
            "drafts_root": None,
        },
    )
    return cmd_add_keyframes(args)


def _handle_add_filter(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "filter_name": None,
            "filter_query": None,
            "start_time": None,
            "duration": "3s",
            "track_name": "FilterTrack",
            "intensity": 100.0,
        },
    )
    return cmd_add_filter(args)


def _handle_add_image(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "start_time": None,
            "duration": "3s",
            "track_name": "ImageTrack",
            "scale_x": 1.0,
            "scale_y": 1.0,
            "transform_x": 0.0,
            "transform_y": 0.0,
            "rotation": 0.0,
            "alpha": 1.0,
            "background_blur": None,
        },
    )
    return cmd_add_image(args)


def _handle_add_effect(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "effect_name": None,
            "effect_query": None,
            "effect_category": "scene",
            "start_time": None,
            "duration": "3s",
            "track_name": "EffectTrack",
        },
    )
    return cmd_add_effect(args)


def _handle_add_video_effect(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "effect_name": None,
            "effect_query": None,
            "start_time": None,
            "duration": "3s",
            "track_name": "EffectTrack",
        },
    )
    return cmd_add_video_effect(args)


def _handle_add_face_effect(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "effect_name": None,
            "effect_query": None,
            "start_time": None,
            "duration": "3s",
            "track_name": "EffectTrack",
        },
    )
    return cmd_add_face_effect(args)


def _handle_add_audio_effect(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        {
            **arguments,
            "params_json": _json_string(arguments.get("params_json")),
        },
        {
            "drafts_root": None,
            "effect_name": None,
            "effect_query": None,
            "effect_category": "scene",
            "params_json": None,
        },
    )
    return cmd_add_audio_effect(args)


def _handle_add_audio_fade(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "fade_in": "0s",
            "fade_out": "0s",
        },
    )
    return cmd_add_audio_fade(args)


def _handle_attach_material(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "material_name": None,
            "query": None,
            "duration": None,
            "animation_role": "in",
        },
    )
    return cmd_attach_material(args)


def _handle_add_complex_text(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        {
            **arguments,
            "shadow_json": _json_string(arguments.get("shadow_json")),
        },
        {
            "drafts_root": None,
            "start_time": None,
            "duration": "3s",
            "track_name": "Subtitles",
            "text_color": "#ffffff",
            "border_color": None,
            "font": None,
            "font_query": None,
            "font_size": 5.0,
            "transform_x": 0.0,
            "transform_y": -0.8,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "shadow_json": None,
            "bubble_resource_id": None,
            "bubble_effect_id": None,
            "bubble_query": None,
            "flower_id": None,
            "flower_query": None,
            "text_effect": None,
            "text_effect_query": None,
            "anim_in": None,
            "anim_in_query": None,
            "anim_out": None,
            "anim_out_query": None,
            "anim_loop": None,
            "anim_loop_query": None,
        },
    )
    return cmd_add_complex_text(args)


def _handle_add_text_template(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        {
            **arguments,
            "texts_json": _json_string(arguments.get("texts_json")),
        },
        {
            "drafts_root": None,
            "template_id": None,
            "template_query": None,
            "text": None,
            "texts_json": None,
            "track_name": "TextTemplateTrack",
            "template_payload_path": None,
        },
    )
    return cmd_add_text_template(args)


def _handle_add_green_screen(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "chroma_color": "#00ff00",
            "chroma_strength": 20,
            "edge_feather": 10,
            "edge_cleanup": 10,
        },
    )
    return cmd_add_green_screen(args)


def _handle_apply_zoom(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "scale": 150,
        },
    )
    return cmd_apply_zoom(args)


def _handle_save_template(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
        },
    )
    return cmd_save_template(args)


def _handle_apply_template(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "drafts_root": None,
            "text": None,
        },
    )
    return cmd_apply_template(args)


def _handle_detect_env(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return make_result(True, "ok", "", {"environment": detect_jianying_environment()})


def _handle_smoke_test(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "name": None,
            "drafts_root": None,
            "cleanup": False,
            "allow_launch": False,
        },
    )
    return cmd_smoke_test(args)


def _handle_self_check(arguments: Dict[str, Any]) -> Dict[str, Any]:
    args = _namespace(
        arguments,
        {
            "name": None,
            "drafts_root": None,
            "cleanup": False,
            "allow_launch": False,
            "refresh": False,
        },
    )
    return cmd_self_check(args)


def _handle_sync_favorite_text_assets(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return cmd_sync_favorite_text_assets()


def _handle_validate_oral_video_draft(arguments: Dict[str, Any]) -> Dict[str, Any]:
    name = str(arguments.get("name") or "").strip()
    if not name:
        raise UserInputError("validate_oral_video_draft requires name")
    return cmd_validate_oral_video_draft(
        name,
        arguments.get("drafts_root"),
        str(arguments.get("style") or "basic-oral-video"),
    )


TOOL_SPECS: List[ToolSpec] = [
    ToolSpec(
        "detect_env",
        "Detect the local JianYing/CapCut install, version, resources, and drafts root",
        {
            "type": "object",
            "properties": {},
        },
        _handle_detect_env,
    ),
    ToolSpec(
        "smoke_test",
        "Create a smoke-test draft and probe the live JianYing window",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "cleanup": {"type": "boolean", "default": False},
                "allow_launch": {"type": "boolean", "default": False},
            },
        },
        _handle_smoke_test,
    ),
    ToolSpec(
        "self_check",
        "Run environment detection and smoke test, then summarize whether this machine is ready to use",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "cleanup": {"type": "boolean", "default": False},
                "allow_launch": {"type": "boolean", "default": False},
                "refresh": {"type": "boolean", "default": False},
            },
        },
        _handle_self_check,
    ),
    ToolSpec(
        "search_assets",
        "Search indexed JianYing/CapCut assets",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
                "list_categories": {"type": "boolean", "default": False},
            },
        },
        _handle_search_assets,
    ),
    ToolSpec(
        "sync_favorite_text_assets",
        "Sync local JianYing favorite/cached text templates and flower text assets into local CSV indexes",
        {
            "type": "object",
            "properties": {},
        },
        _handle_sync_favorite_text_assets,
    ),
    ToolSpec(
        "validate_oral_video_draft",
        "Run the repository oral-video post-save validation against a saved draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "style": {"type": "string", "default": "basic-oral-video"},
            },
            "required": ["name"],
        },
        _handle_validate_oral_video_draft,
    ),
    ToolSpec(
        "resolve_asset",
        "Resolve one best-match indexed asset for direct writing",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
            },
            "required": ["query"],
        },
        _handle_resolve_asset,
    ),
    ToolSpec(
        "list_segments",
        "List segment ids in a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
            },
            "required": ["name"],
        },
        _handle_list_segments,
    ),
    ToolSpec(
        "draft_info",
        "Show draft overview, counts, and platform metadata",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
            },
            "required": ["name"],
        },
        _handle_draft_info,
    ),
    ToolSpec(
        "list_materials",
        "List draft material buckets or the items in one bucket",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "bucket": {"type": "string"},
            },
            "required": ["name"],
        },
        _handle_list_materials,
    ),
    ToolSpec(
        "show_material",
        "Show one draft material by id",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "material_id": {"type": "string"},
            },
            "required": ["name", "material_id"],
        },
        _handle_show_material,
    ),
    ToolSpec(
        "list_tracks",
        "List tracks in a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
            },
            "required": ["name"],
        },
        _handle_list_tracks,
    ),
    ToolSpec(
        "list_texts",
        "List text segments and resolved caption text",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
            },
            "required": ["name"],
        },
        _handle_list_texts,
    ),
    ToolSpec(
        "show_segment",
        "Show one segment with track and material context",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
            },
            "required": ["name", "segment_id"],
        },
        _handle_show_segment,
    ),
    ToolSpec(
        "add_sticker",
        "Add a sticker segment to a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "sticker_id": {"type": "string"},
                "sticker_query": {"type": "string"},
                "start_time": {"type": "string"},
                "duration": {"type": "string", "default": "3s"},
            },
            "required": ["name"],
        },
        _handle_add_sticker,
    ),
    ToolSpec(
        "add_mask",
        "Attach a mask to an existing segment",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "mask_type": {"type": "string"},
                "mask_query": {"type": "string"},
            },
            "required": ["name", "segment_id"],
        },
        _handle_add_mask,
    ),
    ToolSpec(
        "add_rich_text",
        "Add rich text with keyword highlighting",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "text": {"type": "string"},
                "font_query": {"type": "string"},
                "anim_in_query": {"type": "string"},
                "anim_out_query": {"type": "string"},
                "anim_loop_query": {"type": "string"},
                "text_effect_query": {"type": "string"},
            },
            "required": ["name", "text"],
        },
        _handle_add_rich_text,
    ),
    ToolSpec(
        "import_srt",
        "Import an SRT subtitle file into a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "srt_path": {"type": "string"},
                "track_name": {"type": "string"},
                "style_ref_segment_id": {"type": "string"},
            },
            "required": ["name", "srt_path"],
        },
        _handle_import_srt,
    ),
    ToolSpec(
        "export_srt",
        "Export draft subtitles as SRT text",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
            },
            "required": ["name"],
        },
        _handle_export_srt,
    ),
    ToolSpec(
        "text_ranges",
        "Apply inline style ranges to an existing text segment",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "styles": {"type": ["array", "string"]},
            },
            "required": ["name", "segment_id", "styles"],
        },
        _handle_text_ranges,
    ),
    ToolSpec(
        "set_text",
        "Set the text content for one existing text segment",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["name", "segment_id", "text"],
        },
        _handle_set_text,
    ),
    ToolSpec(
        "shift_segment",
        "Shift one segment start time by an offset",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "offset": {"type": "string"},
            },
            "required": ["name", "segment_id", "offset"],
        },
        _handle_shift,
    ),
    ToolSpec(
        "trim_segment",
        "Adjust one segment source range and target duration",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "start_time": {"type": "string"},
                "duration": {"type": "string"},
            },
            "required": ["name", "segment_id", "start_time", "duration"],
        },
        _handle_trim,
    ),
    ToolSpec(
        "set_speed",
        "Set one segment playback speed",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "multiplier": {"type": "number"},
            },
            "required": ["name", "segment_id", "multiplier"],
        },
        _handle_speed,
    ),
    ToolSpec(
        "set_volume",
        "Set one segment volume",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "level": {"type": "number"},
            },
            "required": ["name", "segment_id", "level"],
        },
        _handle_volume,
    ),
    ToolSpec(
        "set_opacity",
        "Set one segment clip opacity",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "alpha": {"type": "number"},
            },
            "required": ["name", "segment_id", "alpha"],
        },
        _handle_opacity,
    ),
    ToolSpec(
        "shift_all",
        "Shift all segment start times, optionally filtered by track type",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "offset": {"type": "string"},
                "track_type": {"type": "string"},
            },
            "required": ["name", "offset"],
        },
        _handle_shift_all,
    ),
    ToolSpec(
        "batch_edit",
        "Apply a batch of saved-draft patch operations",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "ops_json": {"type": ["array", "string"]},
            },
            "required": ["name", "ops_json"],
        },
        _handle_batch,
    ),
    ToolSpec(
        "cut_timeline",
        "Keep only a timeline range and rebase the draft to zero",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
            },
            "required": ["name", "start_time", "end_time"],
        },
        _handle_cut,
    ),
    ToolSpec(
        "add_keyframes",
        "Apply high-level keyframes to a segment",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "keyframes_json": {"type": "array"},
            },
            "required": ["name", "segment_id", "keyframes_json"],
        },
        _handle_add_keyframes,
    ),
    ToolSpec(
        "add_filter",
        "Add a filter track segment to a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "filter_name": {"type": "string"},
                "filter_query": {"type": "string"},
            },
            "required": ["name"],
        },
        _handle_add_filter,
    ),
    ToolSpec(
        "add_image",
        "Add an image segment to a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "image_path": {"type": "string"},
                "background_blur": {"type": "integer"},
            },
            "required": ["name", "image_path"],
        },
        _handle_add_image,
    ),
    ToolSpec(
        "add_effect",
        "Add a scene or character effect segment to a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "effect_name": {"type": "string"},
                "effect_query": {"type": "string"},
                "effect_category": {"type": "string", "enum": ["scene", "character"]},
            },
            "required": ["name"],
        },
        _handle_add_effect,
    ),
    ToolSpec(
        "add_video_effect",
        "Add a scene video effect segment to a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "effect_name": {"type": "string"},
                "effect_query": {"type": "string"},
                "start_time": {"type": "string"},
                "duration": {"type": "string", "default": "3s"},
                "track_name": {"type": "string", "default": "EffectTrack"},
            },
            "required": ["name"],
        },
        _handle_add_video_effect,
    ),
    ToolSpec(
        "add_face_effect",
        "Add a face effect segment to a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "effect_name": {"type": "string"},
                "effect_query": {"type": "string"},
                "start_time": {"type": "string"},
                "duration": {"type": "string", "default": "3s"},
                "track_name": {"type": "string", "default": "EffectTrack"},
            },
            "required": ["name"],
        },
        _handle_add_face_effect,
    ),
    ToolSpec(
        "add_audio_effect",
        "Attach an audio effect, tone effect, or speech-to-song effect to an existing audio segment",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "effect_name": {"type": "string"},
                "effect_query": {"type": "string"},
                "effect_category": {"type": "string", "enum": ["scene", "tone", "speech_to_song"]},
                "params_json": {"type": ["array", "string"]},
            },
            "required": ["name", "segment_id"],
        },
        _handle_add_audio_effect,
    ),
    ToolSpec(
        "add_audio_fade",
        "Attach fade-in/out settings to an existing audio segment",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "fade_in": {"type": "string"},
                "fade_out": {"type": "string"},
            },
            "required": ["name", "segment_id"],
        },
        _handle_add_audio_fade,
    ),
    ToolSpec(
        "attach_material",
        "Attach transition or animation material to a segment",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["transition", "animation"]},
                "query": {"type": "string"},
                "material_name": {"type": "string"},
                "animation_role": {"type": "string", "enum": ["in", "out", "group", "loop"]},
            },
            "required": ["name", "segment_id", "kind"],
        },
        _handle_attach_material,
    ),
    ToolSpec(
        "add_complex_text",
        "Add complex text with bubble, flower, and animation materials",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "text": {"type": "string"},
                "font_query": {"type": "string"},
                "bubble_query": {"type": "string"},
                "flower_query": {"type": "string"},
                "text_effect_query": {"type": "string"},
                "anim_loop_query": {"type": "string"},
            },
            "required": ["name", "text"],
        },
        _handle_add_complex_text,
    ),
    ToolSpec(
        "add_green_screen",
        "Attach green screen chroma settings and add a background segment behind a video segment",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "background_path": {"type": "string"},
                "chroma_color": {"type": "string", "default": "#00ff00"},
                "chroma_strength": {"type": "integer", "default": 20},
                "edge_feather": {"type": "integer", "default": 10},
                "edge_cleanup": {"type": "integer", "default": 10},
            },
            "required": ["name", "segment_id", "background_path"],
        },
        _handle_add_green_screen,
    ),
    ToolSpec(
        "add_text_template",
        "Add an official text template segment to a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "template_id": {"type": "string"},
                "template_query": {"type": "string"},
                "text": {"type": "string"},
                "texts_json": {},
                "start_time": {"type": "string"},
                "duration": {"type": "string"},
                "track_name": {"type": "string"},
                "template_payload_path": {"type": "string"},
            },
            "required": ["name", "start_time", "duration"],
        },
        _handle_add_text_template,
    ),
    ToolSpec(
        "save_template",
        "Save one segment and its materials as a reusable template",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "segment_id": {"type": "string"},
                "template_name": {"type": "string"},
                "out": {"type": "string"},
            },
            "required": ["name", "segment_id", "template_name", "out"],
        },
        _handle_save_template,
    ),
    ToolSpec(
        "apply_template",
        "Apply a saved template into a draft",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "template_path": {"type": "string"},
                "start_time": {"type": "string"},
                "duration": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["name", "template_path", "start_time", "duration"],
        },
        _handle_apply_template,
    ),
    ToolSpec(
        "apply_zoom",
        "Create a draft from a recording and apply smart zoom",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "drafts_root": {"type": "string"},
                "video": {"type": "string"},
                "events_json": {"type": "string"},
                "scale": {"type": "integer", "default": 150},
            },
            "required": ["name", "video", "events_json"],
        },
        _handle_apply_zoom,
    ),
]


TOOL_MAP = {tool.name: tool for tool in TOOL_SPECS}


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }
        for tool in TOOL_SPECS
    ]


def invoke_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    tool = TOOL_MAP.get(str(name))
    if tool is None:
        return make_result(False, "invalid_input", f"Unknown tool: {name}")
    try:
        return tool.handler(arguments or {})
    except UserInputError as exc:
        return make_result(False, "invalid_input", str(exc))
    except FileNotFoundError as exc:
        return make_result(False, "not_found", str(exc))
    except Exception as exc:
        return make_result(False, "runtime_error", str(exc))
