import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pyJianYingDraft as draft
from core.text_io_ops import extract_text_from_material
from pyJianYingDraft.animation import SegmentAnimations, Text_animation, VideoAnimation
from pyJianYingDraft.audio_segment import AudioEffect, AudioFade
from pyJianYingDraft.video_segment import Transition
from utils import jianying_env
from utils.constants import SYNONYMS
from utils.draft_patch_inspection import (
    draft_content_path,
    draft_dir_path,
    draft_root_path,
    find_material_global,
    find_segment_entry,
    list_segments,
    next_render_index,
    read_draft_content,
)
from utils.formatters import (
    resolve_enum_with_synonyms,
    safe_tim,
)
from utils.vendor_enums import get_vendor_enum


def _run_write_retention(drafts_root: Optional[str], project_name: str) -> None:
    if str(project_name or "").startswith("__cli_"):
        return
    try:
        from utils.draft_retention import retain_latest_project_drafts

        retain_latest_project_drafts(
            draft_name=project_name,
            drafts_root=drafts_root,
            keep_count=3,
            max_fallback_count=1,
        )
    except Exception as exc:
        print(f"[warn] Draft retention failed for '{project_name}': {exc}")


def write_draft_content(
    drafts_root: Optional[str],
    project_name: str,
    payload: Dict[str, Any],
    *,
    auto_retain: bool = True,
) -> str:
    path = draft_content_path(drafts_root, project_name)
    try:
        env = jianying_env.detect_jianying_environment()
        jianying_env.stamp_draft_platform(payload, env.get("app_version"))
    except Exception:
        pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=4)
    os.utime(path, None)
    draft_dir = draft_dir_path(drafts_root, project_name)
    if os.path.exists(draft_dir):
        os.utime(draft_dir, None)
    if auto_retain:
        _run_write_retention(drafts_root, project_name)
    return path


def upsert_track(content: Dict[str, Any], track_name: str, track_type: str) -> Dict[str, Any]:
    for track in content.get("tracks", []):
        if track.get("name") == track_name:
            return track
    new_track = {
        "attribute": 0,
        "flag": 0,
        "id": uuid.uuid4().hex,
        "is_default_name": False,
        "name": track_name,
        "segments": [],
        "type": track_type,
    }
    content.setdefault("tracks", []).append(new_track)
    return new_track


def append_text_material(
    content: Dict[str, Any], segment, material: Dict[str, Any], track_name: str
) -> Dict[str, Any]:
    target_track = upsert_track(content, track_name, "text")
    segment_json = segment.export_json()
    segment_json["render_index"] = next_render_index(content, 15000)
    target_track.setdefault("segments", []).append(segment_json)
    content.setdefault("materials", {}).setdefault("texts", []).append(material)
    content["duration"] = max(
        int(content.get("duration", 0)),
        int(segment_json["target_timerange"]["start"])
        + int(segment_json["target_timerange"]["duration"]),
    )
    return {
        "segment_id": segment_json["id"],
        "track_name": target_track["name"],
        "track_type": target_track["type"],
        "segment_type": "text",
        "start_us": segment_json["target_timerange"]["start"],
        "duration_us": segment_json["target_timerange"]["duration"],
        "material_id": segment_json["material_id"],
        "resource_id": None,
    }


def append_sticker_material(
    content: Dict[str, Any], segment, material: Dict[str, Any], track_name: str
) -> Dict[str, Any]:
    target_track = upsert_track(content, track_name, "sticker")
    segment_json = segment.export_json()
    segment_json["render_index"] = next_render_index(content, 14000)
    target_track.setdefault("segments", []).append(segment_json)
    content.setdefault("materials", {}).setdefault("stickers", []).append(material)
    content["duration"] = max(
        int(content.get("duration", 0)),
        int(segment_json["target_timerange"]["start"])
        + int(segment_json["target_timerange"]["duration"]),
    )
    return {
        "segment_id": segment_json["id"],
        "track_name": target_track["name"],
        "track_type": target_track["type"],
        "segment_type": "sticker",
        "start_us": segment_json["target_timerange"]["start"],
        "duration_us": segment_json["target_timerange"]["duration"],
        "material_id": segment_json["material_id"],
        "resource_id": material["resource_id"],
    }


def append_filter_material(
    content: Dict[str, Any], segment, material: Dict[str, Any], track_name: str
) -> Dict[str, Any]:
    target_track = upsert_track(content, track_name, "filter")
    segment_json = segment.export_json()
    segment_json["render_index"] = next_render_index(content, 11000)
    target_track.setdefault("segments", []).append(segment_json)
    content.setdefault("materials", {}).setdefault("effects", []).append(material)
    content["duration"] = max(
        int(content.get("duration", 0)),
        int(segment_json["target_timerange"]["start"])
        + int(segment_json["target_timerange"]["duration"]),
    )
    return {
        "segment_id": segment_json["id"],
        "track_name": target_track["name"],
        "track_type": target_track["type"],
        "segment_type": "filter",
        "start_us": segment_json["target_timerange"]["start"],
        "duration_us": segment_json["target_timerange"]["duration"],
        "material_id": segment_json["material_id"],
        "resource_id": material.get("resource_id"),
    }


def append_video_material(
    content: Dict[str, Any], segment, material: Dict[str, Any], track_name: str
) -> Dict[str, Any]:
    target_track = upsert_track(content, track_name, "video")
    segment_json = segment.export_json()
    segment_json["render_index"] = next_render_index(content, 0)
    target_track.setdefault("segments", []).append(segment_json)
    content.setdefault("materials", {}).setdefault("videos", []).append(material)
    background = getattr(segment, "background_filling", None)
    if background is not None:
        _upsert_material_by_id(content, "canvases", background.export_json())
    content["duration"] = max(
        int(content.get("duration", 0)),
        int(segment_json["target_timerange"]["start"])
        + int(segment_json["target_timerange"]["duration"]),
    )
    return {
        "segment_id": segment_json["id"],
        "track_name": target_track["name"],
        "track_type": target_track["type"],
        "segment_type": "video",
        "start_us": segment_json["target_timerange"]["start"],
        "duration_us": segment_json["target_timerange"]["duration"],
        "material_id": segment_json["material_id"],
        "resource_id": material.get("resource_id"),
    }


def build_chroma_payload(
    *,
    color: str,
    chroma_strength: int,
    edge_feather: int,
    edge_cleanup: int,
    resource_id: str,
) -> Dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "type": "chroma",
        "color": str(color),
        "path": "",
        "edge_smooth_value": float(edge_feather) / 100.0,
        "intensity_value": float(chroma_strength) / 100.0,
        "resource_id": str(resource_id),
        "shadow_value": 0.0,
        "should_transfer_color": False,
        "spill_value": float(edge_cleanup) / 100.0,
        "version": "",
    }


def append_effect_material(content: Dict[str, Any], segment, track_name: str) -> Dict[str, Any]:
    target_track = upsert_track(content, track_name, "effect")
    segment_json = segment.export_json()
    segment_json["render_index"] = next_render_index(content, 11000)
    target_track.setdefault("segments", []).append(segment_json)
    content.setdefault("materials", {}).setdefault("video_effects", []).append(
        segment.effect_inst.export_json()
    )
    content["duration"] = max(
        int(content.get("duration", 0)),
        int(segment_json["target_timerange"]["start"])
        + int(segment_json["target_timerange"]["duration"]),
    )
    return {
        "segment_id": segment_json["id"],
        "track_name": target_track["name"],
        "track_type": target_track["type"],
        "segment_type": "effect",
        "start_us": segment_json["target_timerange"]["start"],
        "duration_us": segment_json["target_timerange"]["duration"],
        "material_id": segment_json["material_id"],
        "resource_id": segment.effect_inst.resource_id,
    }


def append_complex_text_material(
    content: Dict[str, Any],
    segment,
    material: Dict[str, Any],
    track_name: str,
) -> Dict[str, Any]:
    result = append_text_material(content, segment, material, track_name)
    bubble = getattr(segment, "bubble", None)
    if bubble is not None:
        _upsert_material_by_id(content, "effects", bubble.export_json())
    effect = getattr(segment, "effect", None)
    if effect is not None:
        _upsert_material_by_id(content, "effects", effect.export_json())
    animations = getattr(segment, "animations_instance", None)
    if animations is not None:
        _upsert_material_by_id(content, "material_animations", animations.export_json())
    return result


def append_text_template_material(
    content: Dict[str, Any],
    segment_payload: Dict[str, Any],
    bundle: Dict[str, Any],
    track_name: str,
) -> Dict[str, Any]:
    target_track = upsert_track(content, track_name, "text")
    segment_json = json.loads(json.dumps(segment_payload))
    segment_json["render_index"] = next_render_index(content, 15000)
    target_track.setdefault("segments", []).append(segment_json)

    materials = content.setdefault("materials", {})
    materials.setdefault("text_templates", []).append(
        json.loads(json.dumps(bundle["text_template"]))
    )
    for item in bundle.get("texts", []) or []:
        materials.setdefault("texts", []).append(json.loads(json.dumps(item)))
    for item in bundle.get("effects", []) or []:
        materials.setdefault("effects", []).append(json.loads(json.dumps(item)))

    content["duration"] = max(
        int(content.get("duration", 0)),
        int(segment_json["target_timerange"]["start"])
        + int(segment_json["target_timerange"]["duration"]),
    )
    return {
        "segment_id": segment_json["id"],
        "track_name": target_track["name"],
        "track_type": target_track["type"],
        "segment_type": "text_template",
        "start_us": segment_json["target_timerange"]["start"],
        "duration_us": segment_json["target_timerange"]["duration"],
        "material_id": segment_json["material_id"],
        "resource_id": bundle["text_template"].get("resource_id"),
    }


def _upsert_material_by_id(content: Dict[str, Any], bucket: str, payload: Dict[str, Any]) -> None:
    materials = content.setdefault("materials", {}).setdefault(bucket, [])
    payload_id = payload.get("id")
    if payload_id:
        materials[:] = [item for item in materials if item.get("id") != payload_id]
    materials.append(payload)


def _remove_material_refs(content: Dict[str, Any], refs: list[str], buckets: list[str]) -> None:
    if not refs:
        return
    materials = content.setdefault("materials", {})
    ref_set = set(refs)
    for bucket in buckets:
        items = materials.setdefault(bucket, [])
        items[:] = [item for item in items if item.get("id") not in ref_set]


def _segment_timerange(segment_data: Dict[str, Any]) -> Dict[str, Any]:
    return segment_data.get("target_timerange", {}) or {}


def _segment_duration(segment_ref: "SegmentRef") -> int:
    timerange = _segment_timerange(segment_ref.data)
    return int(timerange.get("duration", 0))


def _append_ref_once(segment_ref: "SegmentRef", material_id: str) -> None:
    refs = segment_ref.data.setdefault("extra_material_refs", [])
    if material_id not in refs:
        refs.append(material_id)


def resolve_mask_meta(mask_type: str) -> Dict[str, Any]:
    mask_aliases = {
        "line": "线性",
        "linear": "线性",
        "mirror": "镜面",
        "circle": "圆形",
        "rectangle": "矩形",
        "rect": "矩形",
        "heart": "爱心",
        "star": "星形",
    }
    resolved_name = mask_aliases.get(str(mask_type).lower(), mask_type)
    mask_enum = resolve_enum_with_synonyms(draft.MaskType, resolved_name, SYNONYMS)
    if not mask_enum:
        raise ValueError(f"Unknown mask type: {mask_type}")
    meta = mask_enum.value
    identifier_map = {
        "线性": "line",
        "镜面": "mirror",
        "圆形": "circle",
        "矩形": "rectangle",
        "爱心": "heart",
        "星形": "star",
    }
    return {
        "identifier": identifier_map.get(meta.name, str(mask_type).lower()),
        "name": meta.name,
        "resource_id": meta.resource_id,
    }


def build_mask_payload(
    mask_type: str,
    *,
    center_x: float = 0.0,
    center_y: float = 0.0,
    size: float = 0.5,
    rotation: float = 0.0,
    feather: float = 0.0,
    invert: bool = False,
    rect_width: float = None,
    round_corner: float = None,
) -> Dict[str, Any]:
    meta = resolve_mask_meta(mask_type)
    width = float(rect_width if rect_width is not None else size * 0.5625)
    round_corner_value = float(round_corner if round_corner is not None else 0.0)
    return {
        "config": {
            "aspectRatio": 1.0,
            "centerX": float(center_x) / 960.0,
            "centerY": float(center_y) / 540.0,
            "feather": float(feather) / 100.0,
            "height": float(size),
            "invert": bool(invert),
            "rotation": float(rotation),
            "roundCorner": round_corner_value,
            "width": width,
        },
        "id": uuid.uuid4().hex,
        "name": meta["name"],
        "platform": "all",
        "position_info": "",
        "resource_type": meta["identifier"],
        "resource_id": meta["resource_id"],
        "type": "mask",
    }


def normalize_keyframe_property(property_name: str) -> str:
    property_map = {
        "position_x": "KFTypePositionX",
        "position_y": "KFTypePositionY",
        "rotation": "KFTypeRotation",
        "scale_x": "KFTypeScaleX",
        "scale_y": "KFTypeScaleY",
        "uniform_scale": "KFTypeScaleX",
        "alpha": "KFTypeAlpha",
        "saturation": "KFTypeSaturation",
        "contrast": "KFTypeContrast",
        "brightness": "KFTypeBrightness",
        "volume": "KFTypeVolume",
        "KFTypePositionX": "KFTypePositionX",
        "KFTypePositionY": "KFTypePositionY",
        "KFTypeRotation": "KFTypeRotation",
        "KFTypeScaleX": "KFTypeScaleX",
        "KFTypeScaleY": "KFTypeScaleY",
        "UNIFORM_SCALE": "KFTypeScaleX",
        "KFTypeAlpha": "KFTypeAlpha",
        "KFTypeSaturation": "KFTypeSaturation",
        "KFTypeContrast": "KFTypeContrast",
        "KFTypeBrightness": "KFTypeBrightness",
        "KFTypeVolume": "KFTypeVolume",
    }
    normalized = property_map.get(str(property_name))
    if not normalized:
        raise ValueError(f"Unsupported keyframe property: {property_name}")
    return normalized


def build_keyframe_entry(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "curveType": "Line",
        "graphID": "",
        "left_control": {"x": 0.0, "y": 0.0},
        "right_control": {"x": 0.0, "y": 0.0},
        "id": uuid.uuid4().hex,
        "time_offset": safe_tim(item["offset"]),
        "values": [float(item["value"])],
    }


def build_keyframe_list(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "keyframe_list": [build_keyframe_entry(item)],
        "material_id": "",
        "property_type": normalize_keyframe_property(item["property"]),
    }


@dataclass
class SegmentRef:
    segment_id: str
    track_name: str
    track_type: str
    data: Dict[str, Any]


class DraftPatchSession:
    def __init__(self, project_name: str, drafts_root: Optional[str] = None):
        self.project_name = project_name
        self.drafts_root = drafts_root
        self.content = read_draft_content(drafts_root, project_name)

    @property
    def root(self) -> str:
        return draft_root_path(self.drafts_root)

    @property
    def draft_dir(self) -> str:
        return draft_dir_path(self.drafts_root, self.project_name)

    def list_segments(self) -> list[Dict[str, Any]]:
        return list_segments(self.content)

    def list_materials(self, bucket: Optional[str] = None) -> list[Dict[str, Any]]:
        materials = self.content.get("materials", {}) or {}
        if bucket:
            values = materials.get(bucket, [])
            if not isinstance(values, list):
                values = []
            return [{"type": str(bucket), "count": len(values), "items": values}]

        rows = []
        for name, values in materials.items():
            if isinstance(values, list):
                rows.append({"type": str(name), "count": len(values)})
        rows.sort(key=lambda item: (-int(item["count"]), str(item["type"])))
        return rows

    def list_tracks(self) -> list[Dict[str, Any]]:
        rows: list[Dict[str, Any]] = []
        for index, track in enumerate(self.content.get("tracks", []) or []):
            segments = track.get("segments", []) or []
            max_end = 0
            for segment in segments:
                timerange = segment.get("target_timerange", {}) or {}
                max_end = max(
                    max_end, int(timerange.get("start", 0)) + int(timerange.get("duration", 0))
                )
            attribute = int(track.get("attribute", 0) or 0)
            rows.append(
                {
                    "index": index,
                    "id": track.get("id", ""),
                    "type": track.get("type", ""),
                    "name": track.get("name", ""),
                    "segment_count": len(segments),
                    "segments": len(segments),
                    "duration_us": max_end,
                    "muted": bool(attribute & 1),
                    "hidden": bool(attribute & 2),
                    "locked": bool(attribute & 4),
                }
            )
        return rows

    def list_texts(self) -> list[Dict[str, Any]]:
        materials = self.content.get("materials", {}) or {}
        text_materials = {
            item.get("id"): item
            for item in materials.get("texts", [])
            if isinstance(item, dict) and item.get("id")
        }
        rows: list[Dict[str, Any]] = []
        for track in self.content.get("tracks", []) or []:
            if track.get("type") != "text":
                continue
            for segment in track.get("segments", []) or []:
                timerange = segment.get("target_timerange", {}) or {}
                material = text_materials.get(segment.get("material_id"))
                rows.append(
                    {
                        "segment_id": segment.get("id", ""),
                        "id": segment.get("id", ""),
                        "track_name": track.get("name", ""),
                        "track_type": track.get("type", ""),
                        "material_id": segment.get("material_id"),
                        "start_us": int(timerange.get("start", 0) or 0),
                        "duration_us": int(timerange.get("duration", 0) or 0),
                        "text": extract_text_from_material(material or {}),
                    }
                )
        rows.sort(key=lambda item: (item["start_us"], item["duration_us"], str(item["segment_id"])))
        return rows

    def show_segment(self, segment_id: str) -> Optional[Dict[str, Any]]:
        track, segment = find_segment_entry(self.content, segment_id)
        if track is None or segment is None:
            return None
        detail = json.loads(json.dumps(segment))
        bucket, material = self.find_material(detail.get("material_id"))
        detail["_track_id"] = track.get("id", "")
        detail["_track_name"] = track.get("name", "")
        detail["_track_type"] = track.get("type", "")
        detail["_material_bucket"] = bucket
        detail["_material"] = None if material is None else {"_type": bucket, **material}
        return detail

    def find_material(self, material_id: str) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        return find_material_global(self.content, material_id)

    def find_segment(self, segment_id: str) -> Optional[SegmentRef]:
        track, segment = find_segment_entry(self.content, segment_id)
        if segment is None or track is None:
            return None
        return SegmentRef(
            segment_id=segment["id"],
            track_name=track.get("name", ""),
            track_type=track.get("type", ""),
            data=segment,
        )

    def get_track_duration(self, track_name: str) -> int:
        max_end = 0
        for track in self.content.get("tracks", []):
            if track.get("name") != track_name:
                continue
            for seg in track.get("segments", []):
                timerange = seg.get("target_timerange", {}) or {}
                end = int(timerange.get("start", 0)) + int(timerange.get("duration", 0))
                if end > max_end:
                    max_end = end
        return max_end

    def add_text_segment(
        self, segment, material: Dict[str, Any], track_name: str
    ) -> Dict[str, Any]:
        return append_text_material(self.content, segment, material, track_name)

    def add_complex_text_segment(
        self, segment, material: Dict[str, Any], track_name: str
    ) -> Dict[str, Any]:
        return append_complex_text_material(self.content, segment, material, track_name)

    def add_sticker_segment(
        self, segment, material: Dict[str, Any], track_name: str
    ) -> Dict[str, Any]:
        return append_sticker_material(self.content, segment, material, track_name)

    def add_video_segment(
        self, segment, material: Dict[str, Any], track_name: str
    ) -> Dict[str, Any]:
        return append_video_material(self.content, segment, material, track_name)

    def add_filter_segment(
        self, segment, material: Dict[str, Any], track_name: str
    ) -> Dict[str, Any]:
        return append_filter_material(self.content, segment, material, track_name)

    def add_effect_segment(self, segment, track_name: str) -> Dict[str, Any]:
        return append_effect_material(self.content, segment, track_name)

    def add_text_template_segment(
        self,
        segment_payload: Dict[str, Any],
        bundle: Dict[str, Any],
        track_name: str,
    ) -> Dict[str, Any]:
        return append_text_template_material(self.content, segment_payload, bundle, track_name)

    def add_green_screen_background(
        self,
        segment_ref: SegmentRef,
        *,
        background_path: str,
        chroma_color: str,
        chroma_strength: int = 20,
        edge_feather: int = 10,
        edge_cleanup: int = 10,
    ) -> Dict[str, Any]:
        if segment_ref.track_type != "video":
            raise ValueError(f"Green screen only supports video segments: {segment_ref.segment_id}")
        if not os.path.exists(background_path):
            raise FileNotFoundError(background_path)

        segment_timerange = segment_ref.data.get("target_timerange", {}) or {}
        start_us = int(segment_timerange.get("start", 0) or 0)
        duration_us = int(segment_timerange.get("duration", 0) or 0)
        if duration_us <= 0:
            raise ValueError(f"Video segment has invalid duration: {segment_ref.segment_id}")

        scratch_material = draft.VideoMaterial(background_path, duration=duration_us)
        background_segment = draft.VideoSegment(
            scratch_material,
            draft.Timerange(start_us, duration_us),
            source_timerange=draft.Timerange(0, duration_us),
        )
        background_summary = self.add_video_segment(
            background_segment, scratch_material.export_json(), "绿幕背景"
        )

        chroma_payload = build_chroma_payload(
            color=chroma_color,
            chroma_strength=chroma_strength,
            edge_feather=edge_feather,
            edge_cleanup=edge_cleanup,
            resource_id="267417011880001538",
        )
        _upsert_material_by_id(self.content, "chromas", chroma_payload)
        _append_ref_once(segment_ref, chroma_payload["id"])
        return {
            "segment_id": segment_ref.segment_id,
            "background_segment_id": background_summary["segment_id"],
            "background_track_name": background_summary["track_name"],
            "background_material_id": background_summary["material_id"],
            "background_resource_id": background_summary["resource_id"],
            "chroma_id": chroma_payload["id"],
            "chroma_color": chroma_payload["color"],
        }

    def add_audio_effect_to_segment(
        self,
        segment_ref: SegmentRef,
        *,
        effect_name: str,
        effect_category: str,
        params: Optional[list[Optional[float]]] = None,
    ) -> Dict[str, Any]:
        if segment_ref.track_type != "audio":
            raise ValueError(f"Audio effect only supports audio segments: {segment_ref.segment_id}")

        normalized_category = str(effect_category or "scene").strip().lower()
        if normalized_category == "scene":
            enum_cls = get_vendor_enum("AudioSceneEffectType")
        elif normalized_category == "tone":
            enum_cls = get_vendor_enum("ToneEffectType")
        elif normalized_category == "speech_to_song":
            enum_cls = get_vendor_enum("SpeechToSongType")
        else:
            raise ValueError(f"Unsupported audio effect_category: {effect_category}")

        effect_enum = resolve_enum_with_synonyms(enum_cls, str(effect_name), SYNONYMS)
        if not effect_enum:
            raise ValueError(f"Unknown {normalized_category} audio effect: {effect_name}")

        effect = AudioEffect(effect_enum, params)
        payload = effect.export_json()
        _upsert_material_by_id(self.content, "audio_effects", payload)
        _append_ref_once(segment_ref, payload["id"])
        return {
            "segment_id": segment_ref.segment_id,
            "material_id": payload["id"],
            "effect_name": payload.get("name", str(effect_name)),
            "effect_category": normalized_category,
            "category_id": payload.get("category_id", ""),
        }

    def add_audio_fade_to_segment(
        self,
        segment_ref: SegmentRef,
        *,
        fade_in_duration: int = 0,
        fade_out_duration: int = 0,
    ) -> Dict[str, Any]:
        if segment_ref.track_type != "audio":
            raise ValueError(f"Audio fade only supports audio segments: {segment_ref.segment_id}")
        if fade_in_duration < 0 or fade_out_duration < 0:
            raise ValueError("Audio fade durations must be >= 0")

        fade = AudioFade(int(fade_in_duration), int(fade_out_duration))
        payload = fade.export_json()
        _upsert_material_by_id(self.content, "audio_fades", payload)
        _append_ref_once(segment_ref, payload["id"])
        segment_ref.data["audio_fade"] = {
            "fade_in_duration": int(fade_in_duration),
            "fade_out_duration": int(fade_out_duration),
        }
        return {
            "segment_id": segment_ref.segment_id,
            "material_id": payload["id"],
            "fade_in_duration_us": int(fade_in_duration),
            "fade_out_duration_us": int(fade_out_duration),
        }

    def add_mask_to_segment(self, segment_ref: SegmentRef, **kwargs) -> Dict[str, Any]:
        mask_payload = build_mask_payload(**kwargs)
        materials = self.content.setdefault("materials", {})
        masks = materials.setdefault("masks", [])
        masks.append(mask_payload)
        refs = segment_ref.data.setdefault("extra_material_refs", [])
        if mask_payload["id"] not in refs:
            refs.append(mask_payload["id"])
        return mask_payload

    def add_keyframes_to_segment(
        self, segment_ref: SegmentRef, keyframes: list[Dict[str, Any]]
    ) -> int:
        keyframe_lists = segment_ref.data.setdefault("common_keyframes", [])
        by_property = {
            str(item.get("property_type") or ""): item
            for item in keyframe_lists
            if isinstance(item, dict) and item.get("property_type")
        }
        for item in keyframes:
            property_type = normalize_keyframe_property(item["property"])
            keyframe_list = by_property.get(property_type)
            if keyframe_list is None:
                keyframe_list = {
                    "id": uuid.uuid4().hex,
                    "keyframe_list": [],
                    "material_id": "",
                    "property_type": property_type,
                }
                keyframe_lists.append(keyframe_list)
                by_property[property_type] = keyframe_list
            keyframe_list.setdefault("keyframe_list", []).append(build_keyframe_entry(item))
            keyframe_list["keyframe_list"].sort(
                key=lambda frame: int(frame.get("time_offset", 0) or 0)
            )
        return len(keyframes)

    def attach_transition_to_segment(
        self,
        segment_ref: SegmentRef,
        *,
        transition_name: str,
        duration: Optional[object] = None,
    ) -> str:
        transition_enum = resolve_enum_with_synonyms(
            draft.TransitionType, str(transition_name), SYNONYMS
        )
        if not transition_enum:
            raise ValueError(f"Unknown transition: {transition_name}")
        transition_duration = safe_tim(duration) if duration is not None else None
        transition = Transition(transition_enum, transition_duration)
        payload = transition.export_json()
        _upsert_material_by_id(self.content, "transitions", payload)
        _append_ref_once(segment_ref, payload["id"])
        return payload["id"]

    def attach_animation_to_segment(
        self,
        segment_ref: SegmentRef,
        *,
        animation_name: str,
        animation_role: str,
        duration: Optional[object] = None,
    ) -> str:
        role = str(animation_role).strip().lower()
        if segment_ref.track_type == "text":
            if role == "in":
                enum_cls = draft.TextIntro
                animation_cls = Text_animation
            elif role == "out":
                enum_cls = draft.TextOutro
                animation_cls = Text_animation
            elif role == "loop":
                enum_cls = draft.TextLoopAnim
                animation_cls = Text_animation
            else:
                raise ValueError(f"Unsupported text animation role: {animation_role}")
        else:
            if role == "in":
                enum_cls = draft.IntroType
            elif role == "out":
                enum_cls = draft.OutroType
            elif role == "group":
                enum_cls = draft.GroupAnimationType
            else:
                raise ValueError(f"Unsupported video animation role: {animation_role}")
            animation_cls = VideoAnimation

        animation_enum = resolve_enum_with_synonyms(enum_cls, str(animation_name), SYNONYMS)
        if not animation_enum:
            raise ValueError(f"Unknown animation: {animation_name}")

        segment_duration = _segment_duration(segment_ref)
        duration_us = safe_tim(duration) if duration is not None else animation_enum.value.duration
        duration_us = min(duration_us, segment_duration) if segment_duration else duration_us

        if role == "in":
            start_us = 0
        elif role == "out":
            start_us = max(0, segment_duration - duration_us)
        else:
            start_us = 0
            if role == "loop" and segment_duration:
                duration_us = segment_duration
            if role == "group" and segment_duration:
                duration_us = segment_duration

        animation = animation_cls(animation_enum, start_us, duration_us)
        group = SegmentAnimations()
        group.add_animation(animation)
        payload = group.export_json()
        _upsert_material_by_id(self.content, "material_animations", payload)
        _append_ref_once(segment_ref, payload["id"])
        return payload["id"]

    def replace_text_segment_materials(
        self, segment, material: Dict[str, Any], track_name: str
    ) -> Dict[str, Any]:
        track, segment_data = find_segment_entry(self.content, segment.segment_id)
        if track is None or segment_data is None:
            raise ValueError(f"Text segment not found: {segment.segment_id}")

        old_refs = list(segment_data.get("extra_material_refs", []))
        _remove_material_refs(self.content, old_refs, ["effects", "material_animations"])

        segment_json = segment.export_json()
        segment_json["render_index"] = segment_data.get(
            "render_index", next_render_index(self.content, 15000)
        )
        segment_data.clear()
        segment_data.update(segment_json)
        if track.get("name") != track_name:
            track["name"] = track_name

        texts = self.content.setdefault("materials", {}).setdefault("texts", [])
        texts[:] = [item for item in texts if item.get("id") != material.get("id")]
        texts.append(material)

        bubble = getattr(segment, "bubble", None)
        if bubble is not None:
            _upsert_material_by_id(self.content, "effects", bubble.export_json())
        effect = getattr(segment, "effect", None)
        if effect is not None:
            _upsert_material_by_id(self.content, "effects", effect.export_json())
        animations = getattr(segment, "animations_instance", None)
        if animations is not None:
            _upsert_material_by_id(self.content, "material_animations", animations.export_json())

        self.content["duration"] = max(
            int(self.content.get("duration", 0)),
            int(segment_json["target_timerange"]["start"])
            + int(segment_json["target_timerange"]["duration"]),
        )
        return {
            "segment_id": segment_json["id"],
            "track_name": track.get("name", ""),
            "track_type": track.get("type", ""),
            "segment_type": "text",
            "start_us": segment_json["target_timerange"]["start"],
            "duration_us": segment_json["target_timerange"]["duration"],
            "material_id": segment_json["material_id"],
            "resource_id": None,
        }

    def replace_material(
        self, bucket: str, material_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        materials = self.content.setdefault("materials", {}).setdefault(bucket, [])
        replaced = False
        for index, item in enumerate(materials):
            if item.get("id") == material_id:
                materials[index] = payload
                replaced = True
                break
        if not replaced:
            raise ValueError(f"Material not found in bucket {bucket}: {material_id}")
        return payload

    def set_text_for_segment(
        self,
        segment_id: str,
        text: Optional[str] = None,
        texts: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        segment = self.find_segment(segment_id)
        if segment is None:
            raise ValueError(f"Segment not found: {segment_id}")
        if segment.track_type != "text":
            raise ValueError(f"set-text only supports text segments: {segment_id}")

        if text is not None and texts is not None:
            raise ValueError("set-text accepts either text or texts, not both")
        if text is None and texts is None:
            raise ValueError("set-text requires text or texts")

        requested_texts = [str(text)] if text is not None else [str(item) for item in (texts or [])]
        if not requested_texts:
            raise ValueError("set-text requires at least one text value")

        bucket, material = self.find_material(segment.data.get("material_id"))
        if bucket == "texts" and material is not None:
            if len(requested_texts) != 1:
                raise ValueError("plain text segments only support one text value")
            payload = dict(material)
            try:
                content = json.loads(payload.get("content", "{}"))
            except Exception as exc:
                raise ValueError(f"Invalid text material content: {exc}") from exc
            old_text = str(content.get("text", ""))
            content["text"] = requested_texts[0]
            payload["content"] = json.dumps(content, ensure_ascii=False)
            self.replace_material("texts", payload["id"], payload)
            return {
                "segment_id": segment.segment_id,
                "material_id": payload["id"],
                "material_ids": [payload["id"]],
                "old_text": old_text,
                "old_texts": [old_text],
                "text": requested_texts[0],
                "texts": requested_texts,
                "updated_count": 1,
            }
        if bucket == "text_templates" and material is not None:
            text_resources = material.get("text_info_resources") or []
            if not text_resources:
                raise ValueError(f"Text template has no text slots: {segment_id}")
            if len(requested_texts) > len(text_resources):
                raise ValueError(
                    f"text template only has {len(text_resources)} text slot(s), but received {len(requested_texts)} text value(s)"
                )
            old_texts: list[str] = []
            material_ids: list[str] = []
            for index, new_text in enumerate(requested_texts):
                resource = text_resources[index]
                target_material_id = resource.get("text_material_id")
                text_bucket, text_material = self.find_material(target_material_id)
                if text_bucket != "texts" or text_material is None:
                    raise ValueError(f"Template text material not found: {target_material_id}")
                payload = dict(text_material)
                try:
                    content = json.loads(payload.get("content", "{}"))
                except Exception as exc:
                    raise ValueError(f"Invalid text material content: {exc}") from exc
                old_text = str(content.get("text", ""))
                content["text"] = new_text
                styles = content.get("styles", [])
                if styles:
                    styles[0]["range"] = [0, len(new_text)]
                elif old_text:
                    content["styles"] = [{"range": [0, len(new_text)]}]
                payload["content"] = json.dumps(content, ensure_ascii=False)
                self.replace_material("texts", payload["id"], payload)
                old_texts.append(old_text)
                material_ids.append(payload["id"])
            return {
                "segment_id": segment.segment_id,
                "material_id": material_ids[0],
                "material_ids": material_ids,
                "old_text": old_texts[0],
                "old_texts": old_texts,
                "text": requested_texts[0],
                "texts": requested_texts,
                "updated_count": len(material_ids),
            }
        raise ValueError(f"Text material not found for segment: {segment_id}")

    def shift_segment(self, segment_id: str, offset: object) -> Dict[str, Any]:
        segment = self.find_segment(segment_id)
        if segment is None:
            raise ValueError(f"Segment not found: {segment_id}")
        offset_us = safe_tim(offset)
        timerange = segment.data.setdefault("target_timerange", {})
        old_start = int(timerange.get("start", 0) or 0)
        new_start = max(0, old_start + offset_us)
        timerange["start"] = new_start
        self._recompute_duration()
        return {
            "segment_id": segment.segment_id,
            "old_start_us": old_start,
            "new_start_us": new_start,
            "offset_us": offset_us,
        }

    def trim_segment(self, segment_id: str, start_time: object, duration: object) -> Dict[str, Any]:
        segment = self.find_segment(segment_id)
        if segment is None:
            raise ValueError(f"Segment not found: {segment_id}")
        start_us = safe_tim(start_time)
        duration_us = safe_tim(duration)
        if duration_us <= 0:
            raise ValueError("trim requires duration > 0")
        source = segment.data.setdefault("source_timerange", {})
        source["start"] = start_us
        source["duration"] = duration_us
        speed = float(segment.data.get("speed", 1.0) or 1.0)
        target_duration_us = round(duration_us / speed) if speed > 0 else duration_us
        segment.data.setdefault("target_timerange", {})
        segment.data["target_timerange"]["duration"] = target_duration_us
        self._recompute_duration()
        return {
            "segment_id": segment.segment_id,
            "source_start_us": start_us,
            "source_duration_us": duration_us,
            "target_duration_us": target_duration_us,
        }

    def set_segment_speed(self, segment_id: str, multiplier: float) -> Dict[str, Any]:
        segment = self.find_segment(segment_id)
        if segment is None:
            raise ValueError(f"Segment not found: {segment_id}")
        speed = float(multiplier)
        if speed <= 0:
            raise ValueError("Speed must be a positive number")
        old_speed = float(segment.data.get("speed", 1.0) or 1.0)
        target = segment.data.setdefault("target_timerange", {})
        target_duration = int(target.get("duration", 0) or 0)
        source = segment.data.setdefault("source_timerange", {})
        source_duration = int(source.get("duration", 0) or 0)
        if source_duration <= 0:
            source_duration = round(target_duration * old_speed) if target_duration > 0 else 0

        segment.data["speed"] = speed
        source["duration"] = (
            round(source_duration * speed / old_speed)
            if old_speed > 0
            else round(target_duration * speed)
        )
        if target_duration > 0:
            target["duration"] = round(target_duration * old_speed / speed)
        for ref_id in segment.data.get("extra_material_refs", []) or []:
            bucket, material = self.find_material(ref_id)
            if bucket == "speeds" and material is not None:
                updated = dict(material)
                updated["speed"] = speed
                self.replace_material(bucket, ref_id, updated)
        self._recompute_duration()
        return {
            "segment_id": segment.segment_id,
            "old_speed": old_speed,
            "new_speed": speed,
        }

    def set_segment_volume(self, segment_id: str, level: float) -> Dict[str, Any]:
        segment = self.find_segment(segment_id)
        if segment is None:
            raise ValueError(f"Segment not found: {segment_id}")
        volume = float(level)
        if volume < 0:
            raise ValueError("Volume must be >= 0")
        old_volume = float(segment.data.get("volume", 1.0) or 1.0)
        segment.data["volume"] = volume
        segment.data["last_nonzero_volume"] = volume
        return {
            "segment_id": segment.segment_id,
            "old_volume": old_volume,
            "new_volume": volume,
        }

    def set_segment_opacity(self, segment_id: str, alpha: float) -> Dict[str, Any]:
        segment = self.find_segment(segment_id)
        if segment is None:
            raise ValueError(f"Segment not found: {segment_id}")
        opacity = float(alpha)
        if opacity < 0 or opacity > 1:
            raise ValueError("Opacity must be 0.0-1.0")
        clip = segment.data.get("clip")
        if clip is None:
            raise ValueError(f"Segment {segment_id} has no clip (audio segment?)")
        old_opacity = float(clip.get("alpha", 1.0) or 1.0)
        clip["alpha"] = opacity
        return {
            "segment_id": segment.segment_id,
            "old_opacity": old_opacity,
            "new_opacity": opacity,
        }

    def shift_all(self, offset: object, track_type: Optional[str] = None) -> Dict[str, Any]:
        offset_us = safe_tim(offset)
        shifted = 0
        for track in self.content.get("tracks", []) or []:
            if track_type and track.get("type") != track_type:
                continue
            for segment in track.get("segments", []) or []:
                timerange = segment.setdefault("target_timerange", {})
                old_start = int(timerange.get("start", 0) or 0)
                timerange["start"] = max(0, old_start + offset_us)
                shifted += 1
        self._recompute_duration()
        return {
            "shifted": shifted,
            "offset_us": offset_us,
            "track_type": track_type,
        }

    def batch_edit(self, ops: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        results: list[Dict[str, Any]] = []
        for index, op in enumerate(ops):
            if not isinstance(op, dict):
                raise ValueError(f"Batch op at index {index} must be an object")
            cmd = str(op.get("cmd", "")).strip().lower()
            if cmd == "set-text":
                segment_id = op.get("segment_id")
                if not segment_id:
                    raise ValueError("set-text requires segment_id")
                text = op.get("text")
                texts = op.get("texts", op.get("texts_json"))
                if text is None and texts is None:
                    raise ValueError("set-text requires text or texts")
                if text is not None and texts is not None:
                    raise ValueError("set-text accepts either text or texts, not both")
                if texts is not None and not isinstance(texts, list):
                    raise ValueError("set-text texts must be a list")
                results.append(
                    {
                        "cmd": "set-text",
                        **self.set_text_for_segment(
                            str(segment_id),
                            text=str(text) if text is not None else None,
                            texts=[str(item) for item in texts] if texts is not None else None,
                        ),
                    }
                )
            elif cmd == "shift-all":
                offset = op.get("offset")
                if offset is None:
                    raise ValueError("shift-all requires offset")
                results.append(
                    {
                        "cmd": "shift-all",
                        **self.shift_all(offset, track_type=op.get("track_type")),
                    }
                )
            elif cmd == "shift":
                segment_id = op.get("segment_id")
                offset = op.get("offset")
                if not segment_id:
                    raise ValueError("shift requires segment_id")
                if offset is None:
                    raise ValueError("shift requires offset")
                results.append(
                    {
                        "cmd": "shift",
                        **self.shift_segment(str(segment_id), offset),
                    }
                )
            elif cmd == "trim":
                segment_id = op.get("segment_id")
                start_time = op.get("start_time", op.get("start"))
                duration = op.get("duration")
                if not segment_id:
                    raise ValueError("trim requires segment_id")
                if start_time is None:
                    raise ValueError("trim requires start_time")
                if duration is None:
                    raise ValueError("trim requires duration")
                results.append(
                    {
                        "cmd": "trim",
                        **self.trim_segment(str(segment_id), start_time, duration),
                    }
                )
            elif cmd == "speed":
                segment_id = op.get("segment_id")
                multiplier = op.get("multiplier", op.get("speed"))
                if not segment_id:
                    raise ValueError("speed requires segment_id")
                if multiplier is None:
                    raise ValueError("speed requires multiplier")
                results.append(
                    {
                        "cmd": "speed",
                        **self.set_segment_speed(str(segment_id), float(multiplier)),
                    }
                )
            elif cmd == "volume":
                segment_id = op.get("segment_id")
                level = op.get("level", op.get("volume"))
                if not segment_id:
                    raise ValueError("volume requires segment_id")
                if level is None:
                    raise ValueError("volume requires level")
                results.append(
                    {
                        "cmd": "volume",
                        **self.set_segment_volume(str(segment_id), float(level)),
                    }
                )
            elif cmd == "opacity":
                segment_id = op.get("segment_id")
                alpha = op.get("alpha", op.get("opacity"))
                if not segment_id:
                    raise ValueError("opacity requires segment_id")
                if alpha is None:
                    raise ValueError("opacity requires alpha")
                results.append(
                    {
                        "cmd": "opacity",
                        **self.set_segment_opacity(str(segment_id), float(alpha)),
                    }
                )
            else:
                raise ValueError(f"Unsupported batch command: {cmd}")
        return results

    def cut_timeline(self, start_time: object, end_time: object) -> Dict[str, Any]:
        start_us = safe_tim(start_time)
        end_us = safe_tim(end_time)
        if end_us <= start_us:
            raise ValueError("cut requires end_time > start_time")

        kept = 0
        removed = 0
        removed_primary_ids: set[str] = set()
        removed_extra_ids: set[str] = set()

        for track in self.content.get("tracks", []) or []:
            surviving: list[Dict[str, Any]] = []
            for segment in track.get("segments", []) or []:
                timerange = segment.get("target_timerange", {}) or {}
                seg_start = int(timerange.get("start", 0) or 0)
                seg_duration = int(timerange.get("duration", 0) or 0)
                seg_end = seg_start + seg_duration

                if seg_end <= start_us or seg_start >= end_us:
                    material_id = segment.get("material_id")
                    if isinstance(material_id, str):
                        removed_primary_ids.add(material_id)
                    for ref in segment.get("extra_material_refs", []) or []:
                        if isinstance(ref, str):
                            removed_extra_ids.add(ref)
                    removed += 1
                    continue

                clipped_start = max(seg_start, start_us)
                clipped_end = min(seg_end, end_us)
                trim_from_start = clipped_start - seg_start
                new_duration = clipped_end - clipped_start

                if segment.get("source_timerange"):
                    source = segment["source_timerange"]
                    speed = float(segment.get("speed", 1) or 1)
                    source["start"] = int(source.get("start", 0) or 0) + round(
                        trim_from_start * speed
                    )
                    source["duration"] = round(new_duration * speed)

                segment.setdefault("target_timerange", {})
                segment["target_timerange"]["start"] = clipped_start - start_us
                segment["target_timerange"]["duration"] = new_duration
                surviving.append(segment)
                kept += 1
            track["segments"] = surviving

        self.content["tracks"] = [
            track for track in (self.content.get("tracks", []) or []) if track.get("segments")
        ]

        surviving_primary_ids: set[str] = set()
        surviving_extra_ids: set[str] = set()
        for track in self.content.get("tracks", []) or []:
            for segment in track.get("segments", []) or []:
                material_id = segment.get("material_id")
                if isinstance(material_id, str):
                    surviving_primary_ids.add(material_id)
                for ref in segment.get("extra_material_refs", []) or []:
                    if isinstance(ref, str):
                        surviving_extra_ids.add(ref)

        for bucket, items in (self.content.get("materials", {}) or {}).items():
            if not isinstance(items, list):
                continue
            kept_items = []
            for item in items:
                item_id = item.get("id")
                if not isinstance(item_id, str):
                    kept_items.append(item)
                    continue
                if item_id in surviving_primary_ids or item_id in surviving_extra_ids:
                    kept_items.append(item)
                    continue
                if item_id in removed_primary_ids or item_id in removed_extra_ids:
                    continue
                kept_items.append(item)
            self.content["materials"][bucket] = kept_items

        self.content["duration"] = end_us - start_us
        return {
            "start_us": start_us,
            "end_us": end_us,
            "duration_us": end_us - start_us,
            "kept": kept,
            "removed": removed,
        }

    def append_template_materials(self, payload: Dict[str, list[Dict[str, Any]]]) -> None:
        materials = self.content.setdefault("materials", {})
        for bucket, items in payload.items():
            bucket_items = materials.setdefault(bucket, [])
            bucket_items.extend(items)

    def append_segment_json(
        self, track_name: str, track_type: str, segment_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        target_track = upsert_track(self.content, track_name, track_type)
        target_track["type"] = track_type
        target_track.setdefault("segments", []).append(segment_payload)
        timerange = segment_payload.get("target_timerange", {}) or {}
        self.content["duration"] = max(
            int(self.content.get("duration", 0)),
            int(timerange.get("start", 0)) + int(timerange.get("duration", 0)),
        )
        return {
            "segment_id": segment_payload.get("id", ""),
            "track_name": target_track.get("name", ""),
            "track_type": target_track.get("type", ""),
            "segment_type": target_track.get("type", ""),
            "start_us": timerange.get("start", 0),
            "duration_us": timerange.get("duration", 0),
            "material_id": segment_payload.get("material_id"),
            "resource_id": segment_payload.get("resource_id"),
        }

    def _recompute_duration(self) -> int:
        max_end = 0
        for track in self.content.get("tracks", []) or []:
            for segment in track.get("segments", []) or []:
                timerange = segment.get("target_timerange", {}) or {}
                max_end = max(
                    max_end,
                    int(timerange.get("start", 0) or 0) + int(timerange.get("duration", 0) or 0),
                )
        self.content["duration"] = max_end
        return max_end

    def save(self, *, auto_retain: bool = True) -> str:
        return write_draft_content(
            self.drafts_root,
            self.project_name,
            self.content,
            auto_retain=auto_retain,
        )
