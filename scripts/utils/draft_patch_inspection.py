import json
import os
from typing import Any, Dict, Optional

from utils.formatters import get_default_drafts_root


def draft_root_path(drafts_root: Optional[str]) -> str:
    return os.path.abspath(drafts_root or get_default_drafts_root())


def draft_dir_path(drafts_root: Optional[str], project_name: str) -> str:
    return os.path.join(draft_root_path(drafts_root), project_name)


def draft_content_path(drafts_root: Optional[str], project_name: str) -> str:
    return os.path.join(draft_dir_path(drafts_root, project_name), "draft_content.json")


def read_draft_content(drafts_root: Optional[str], project_name: str) -> Dict[str, Any]:
    path = draft_content_path(drafts_root, project_name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Draft content not found: {path}")
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def find_segment_entry(
    content: Dict[str, Any], segment_id: str
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    for track in content.get("tracks", []):
        for segment in track.get("segments", []):
            if segment.get("id") == segment_id:
                return track, segment
    return None, None


def list_segments(content: Dict[str, Any]) -> list[Dict[str, Any]]:
    segments: list[Dict[str, Any]] = []
    for track in content.get("tracks", []):
        track_name = track.get("name", "")
        track_type = track.get("type", "")
        for segment in track.get("segments", []):
            timerange = segment.get("target_timerange", {}) or {}
            segments.append(
                {
                    "segment_id": segment.get("id", ""),
                    "track_name": track_name,
                    "track_type": track_type,
                    "segment_type": track_type,
                    "start_us": timerange.get("start", 0),
                    "duration_us": timerange.get("duration", 0),
                    "material_id": segment.get("material_id"),
                    "resource_id": segment.get("resource_id"),
                }
            )
    return segments


def find_material_global(
    content: Dict[str, Any], material_id: str
) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    short_id = str(material_id or "").lower()
    for bucket, items in (content.get("materials", {}) or {}).items():
        if not isinstance(items, list):
            continue
        for item in items:
            current_id = item.get("id")
            if not isinstance(current_id, str):
                continue
            if current_id == material_id or current_id.lower().startswith(short_id):
                return str(bucket), item
    return None, None


def next_render_index(content: Dict[str, Any], default_value: int = 0) -> int:
    max_value = default_value
    for track in content.get("tracks", []):
        for segment in track.get("segments", []):
            render_index = segment.get("render_index")
            if isinstance(render_index, int) and render_index > max_value:
                max_value = render_index
    return max_value + 1000 if max_value else default_value
