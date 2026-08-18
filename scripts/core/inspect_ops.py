import copy
from typing import Any, Dict, List, Optional, Tuple

from core.text_io_ops import extract_text_from_material


def _iter_material_buckets(content: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    materials = content.get("materials", {}) or {}
    items: List[Tuple[str, List[Dict[str, Any]]]] = []
    for bucket, values in materials.items():
        if isinstance(values, list):
            items.append((str(bucket), values))
    return items


def material_counts(content: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for bucket, values in _iter_material_buckets(content):
        counts[bucket] = len(values)
    return counts


def list_materials(content: Dict[str, Any], bucket: Optional[str] = None) -> List[Dict[str, Any]]:
    if bucket:
        materials = content.get("materials", {}) or {}
        values = materials.get(bucket, [])
        if not isinstance(values, list):
            values = []
        return [{"type": str(bucket), "count": len(values), "items": copy.deepcopy(values)}]

    summaries = [
        {"type": name, "count": len(values)} for name, values in _iter_material_buckets(content)
    ]
    summaries.sort(key=lambda item: (-int(item["count"]), str(item["type"])))
    return summaries


def find_material_global(
    content: Dict[str, Any], material_id: str
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not material_id:
        return None, None
    short_id = str(material_id).lower()
    for bucket, values in _iter_material_buckets(content):
        for item in values:
            current_id = item.get("id")
            if not isinstance(current_id, str):
                continue
            if current_id == material_id or current_id.lower().startswith(short_id):
                return bucket, copy.deepcopy(item)
    return None, None


def draft_info(content: Dict[str, Any]) -> Dict[str, Any]:
    tracks = content.get("tracks", []) or []
    track_summaries: List[Dict[str, Any]] = []
    segment_count = 0
    for track in tracks:
        segments = track.get("segments", []) or []
        segment_count += len(segments)
        max_end = 0
        for segment in segments:
            timerange = segment.get("target_timerange", {}) or {}
            end_us = int(timerange.get("start", 0)) + int(timerange.get("duration", 0))
            max_end = max(max_end, end_us)
        track_summaries.append(
            {
                "name": track.get("name", ""),
                "type": track.get("type", ""),
                "segment_count": len(segments),
                "duration_us": max_end,
            }
        )
    return {
        "draft_name": content.get("name", ""),
        "duration_us": int(content.get("duration", 0) or 0),
        "fps": content.get("fps"),
        "canvas": copy.deepcopy(content.get("canvas_config", {}) or {}),
        "track_count": len(tracks),
        "segment_count": segment_count,
        "material_counts": material_counts(content),
        "tracks": track_summaries,
        "platform": copy.deepcopy(content.get("platform", {}) or {}),
        "last_modified_platform": copy.deepcopy(content.get("last_modified_platform", {}) or {}),
    }


def list_tracks(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    tracks = content.get("tracks", []) or []
    rows: List[Dict[str, Any]] = []
    for index, track in enumerate(tracks):
        segments = track.get("segments", []) or []
        max_end = 0
        for segment in segments:
            timerange = segment.get("target_timerange", {}) or {}
            end_us = int(timerange.get("start", 0)) + int(timerange.get("duration", 0))
            max_end = max(max_end, end_us)
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


def list_texts(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    materials = content.get("materials", {}) or {}
    text_materials = {
        item.get("id"): item
        for item in materials.get("texts", [])
        if isinstance(item, dict) and item.get("id")
    }
    rows: List[Dict[str, Any]] = []
    for track in content.get("tracks", []) or []:
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


def find_segment_detail(content: Dict[str, Any], segment_id: str) -> Optional[Dict[str, Any]]:
    if not segment_id:
        return None
    short_id = str(segment_id).lower()
    for index, track in enumerate(content.get("tracks", []) or []):
        for segment in track.get("segments", []) or []:
            current_id = segment.get("id")
            if not isinstance(current_id, str):
                continue
            if current_id != segment_id and not current_id.lower().startswith(short_id):
                continue
            detail = copy.deepcopy(segment)
            bucket, material = find_material_global(content, str(detail.get("material_id") or ""))
            detail["_track_index"] = index
            detail["_track_id"] = track.get("id", "")
            detail["_track_name"] = track.get("name", "")
            detail["_track_type"] = track.get("type", "")
            detail["_material_bucket"] = bucket
            detail["_material"] = None if material is None else {"_type": bucket, **material}
            return detail
    return None
