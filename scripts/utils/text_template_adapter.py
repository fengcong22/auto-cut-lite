import copy
import json
import os
import subprocess
import sys
import uuid
from typing import Any, Dict, List, Optional


def detect_text_template_adapter() -> Optional[str]:
    candidate = os.getenv("JY_TEXT_TEMPLATE_ADAPTER", "").strip()
    if not candidate:
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def build_text_template_adapter_command(adapter_path: str, *args: str) -> list[str]:
    suffix = os.path.splitext(adapter_path)[1].lower()
    if suffix == ".py":
        return [sys.executable, adapter_path, *args]
    return [adapter_path, *args]


def load_text_template_payload(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_text_template_bundle(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("text template payload must be an object")

    if "text_template" in payload:
        bundle = {
            "version": int(payload.get("version", 1) or 1),
            "text_template": copy.deepcopy(payload.get("text_template") or {}),
            "texts": copy.deepcopy(payload.get("texts") or []),
            "effects": copy.deepcopy(payload.get("effects") or []),
        }
    else:
        bundle = {
            "version": int(payload.get("version", 1) or 1),
            "text_template": copy.deepcopy(payload.get("textTemplate") or {}),
            "texts": copy.deepcopy(payload.get("texts") or []),
            "effects": copy.deepcopy(payload.get("effects") or []),
        }

    text_template = bundle.get("text_template") or {}
    if not isinstance(text_template, dict) or not text_template.get("id"):
        raise ValueError("text template payload missing text_template.id")
    if not isinstance(bundle.get("texts"), list):
        raise ValueError("text template payload texts must be a list")
    if not isinstance(bundle.get("effects"), list):
        raise ValueError("text template payload effects must be a list")
    return bundle


def apply_text_template_texts(bundle: Dict[str, Any], texts: List[str]) -> Dict[str, Any]:
    updated = copy.deepcopy(bundle)
    slots = updated["text_template"].get("text_info_resources") or []
    if len(texts) > len(slots):
        raise ValueError(
            f"text template only has {len(slots)} text slot(s), but received {len(texts)} text value(s)"
        )

    text_materials = {
        item.get("id"): item
        for item in updated.get("texts", [])
        if isinstance(item, dict) and item.get("id")
    }

    for index, value in enumerate(texts):
        slot = slots[index]
        material_id = slot.get("text_material_id")
        material = text_materials.get(material_id)
        if not material:
            raise ValueError(f"text template slot material not found: {material_id}")
        content = material.get("content")
        try:
            parsed = json.loads(content)
        except Exception:
            material["content"] = str(value)
            continue
        if not isinstance(parsed, dict):
            material["content"] = str(value)
            continue
        old_text = str(parsed.get("text", ""))
        parsed["text"] = str(value)
        styles = parsed.get("styles", [])
        if styles:
            styles[0]["range"] = [0, len(str(value))]
        elif old_text:
            parsed["styles"] = [{"range": [0, len(str(value))]}]
        material["content"] = json.dumps(parsed, ensure_ascii=False)
    return updated


def remap_text_template_bundle_ids(bundle: Dict[str, Any]) -> Dict[str, Any]:
    updated = copy.deepcopy(bundle)
    text_template = updated.get("text_template") or {}

    id_map: Dict[str, str] = {}
    root_id = text_template.get("id")
    if isinstance(root_id, str) and root_id:
        id_map[root_id] = uuid.uuid4().hex

    for item in updated.get("texts", []) or []:
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            id_map[item_id] = uuid.uuid4().hex

    for item in updated.get("effects", []) or []:
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            id_map[item_id] = uuid.uuid4().hex

    if root_id in id_map:
        text_template["id"] = id_map[root_id]

    for slot in text_template.get("text_info_resources") or []:
        material_id = slot.get("text_material_id")
        if material_id in id_map:
            slot["text_material_id"] = id_map[material_id]
        refs = slot.get("extra_material_refs")
        if isinstance(refs, list):
            slot["extra_material_refs"] = [id_map.get(ref, ref) for ref in refs]

    for item in updated.get("texts", []) or []:
        item_id = item.get("id")
        if item_id in id_map:
            item["id"] = id_map[item_id]

    for item in updated.get("effects", []) or []:
        item_id = item.get("id")
        if item_id in id_map:
            item["id"] = id_map[item_id]

    return updated


def build_text_template_segment(
    bundle: Dict[str, Any], *, start_us: int, duration_us: int
) -> Dict[str, Any]:
    text_template = copy.deepcopy(bundle["text_template"])
    return {
        "id": uuid.uuid4().hex,
        "material_id": text_template["id"],
        "target_timerange": {
            "start": int(start_us),
            "duration": int(duration_us),
        },
        "extra_material_refs": [],
        "render_index": 0,
        "track_attribute": 0,
        "track_render_index": 0,
        "type": "text",
    }


def resolve_text_template_bundle(
    *,
    template_id: Optional[str],
    texts: List[str],
    template_payload: Optional[Dict[str, Any]] = None,
    template_payload_path: Optional[str] = None,
) -> Dict[str, Any]:
    adapter_path = detect_text_template_adapter()

    if template_payload is not None:
        return normalize_text_template_bundle(template_payload)
    if template_payload_path:
        return normalize_text_template_bundle(load_text_template_payload(template_payload_path))
    if not template_id:
        raise ValueError("template_id is required when no template payload is provided")
    if not adapter_path:
        raise ValueError(
            "text template adapter not available; provide template_payload or template_payload_path"
        )

    command = build_text_template_adapter_command(
        adapter_path,
        "-r",
        str(template_id),
        "-t",
        json.dumps(texts, ensure_ascii=False),
    )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise ValueError(f"text template adapter failed: {stderr or completed.returncode}")
    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise ValueError("text template adapter returned empty output")
    return normalize_text_template_bundle(json.loads(stdout))
