import copy
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple


def _remap_ids(payload: Any, id_map: Dict[str, str]) -> Any:
    if isinstance(payload, dict):
        return {key: _remap_ids(value, id_map) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_remap_ids(item, id_map) for item in payload]
    if isinstance(payload, str) and payload in id_map:
        return id_map[payload]
    return payload


def build_template_payload(
    content: Dict[str, Any],
    track: Dict[str, Any],
    segment: Dict[str, Any],
) -> Dict[str, Any]:
    materials = content.get("materials", {}) or {}
    material_id = segment.get("material_id")
    extra_refs = list(segment.get("extra_material_refs", []) or [])
    bundle: Dict[str, List[Dict[str, Any]]] = {}
    for bucket, values in materials.items():
        if not isinstance(values, list):
            continue
        captured = []
        for item in values:
            item_id = item.get("id")
            if item_id == material_id or item_id in extra_refs:
                captured.append(copy.deepcopy(item))
        if captured:
            bundle[str(bucket)] = captured

    return {
        "version": 1,
        "track_type": track.get("type", ""),
        "track_name": track.get("name", ""),
        "segment": copy.deepcopy(segment),
        "materials": bundle,
    }


def save_template_file(path: str, payload: Dict[str, Any]) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def load_template_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_template_payload(
    payload: Dict[str, Any],
    *,
    start_us: int,
    duration_us: int,
    text_override: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
    segment = copy.deepcopy(payload.get("segment", {}))
    materials = copy.deepcopy(payload.get("materials", {}))

    old_segment_id = str(segment.get("id"))
    old_material_id = str(segment.get("material_id"))
    old_extra_refs = list(segment.get("extra_material_refs", []) or [])
    all_ids = [old_segment_id, old_material_id, *old_extra_refs]
    id_map = {value: uuid.uuid4().hex for value in all_ids if value}

    segment = _remap_ids(segment, id_map)
    segment["id"] = id_map.get(old_segment_id, uuid.uuid4().hex)
    segment["material_id"] = id_map.get(old_material_id, segment.get("material_id"))
    segment["extra_material_refs"] = [id_map.get(item, item) for item in old_extra_refs]
    segment.setdefault("target_timerange", {})
    segment["target_timerange"]["start"] = int(start_us)
    segment["target_timerange"]["duration"] = int(duration_us)

    if segment.get("source_timerange") is not None:
        source_timerange = segment.get("source_timerange", {}) or {}
        source_timerange["duration"] = int(duration_us)
        segment["source_timerange"] = source_timerange

    remapped_materials = _remap_ids(materials, id_map)
    if text_override:
        for item in remapped_materials.get("texts", []) or []:
            content = item.get("content")
            if not isinstance(content, str):
                continue
            try:
                parsed = json.loads(content)
            except Exception:
                continue
            parsed["text"] = str(text_override)
            styles = parsed.get("styles", [])
            if styles:
                styles[0]["range"] = [0, len(str(text_override))]
            item["content"] = json.dumps(parsed, ensure_ascii=False)

    return segment, remapped_materials
