import copy
import json
import re
from typing import Any, Dict, List, Optional

from utils.formatters import format_srt_time, safe_tim

SRT_TS_RE = re.compile(
    r"^(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})$"
)


def _srt_ts_to_us(h: str, m: str, s: str, ms: str) -> int:
    padded_ms = (str(ms) + "000")[:3]
    total_ms = (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(padded_ms)
    return total_ms * 1000


def parse_srt_text(raw: str) -> List[Dict[str, Any]]:
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: List[Dict[str, Any]] = []
    idx = 0
    auto_index = 1
    while idx < len(lines):
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx >= len(lines):
            break

        cue_index = auto_index
        if lines[idx].strip().isdigit():
            cue_index = int(lines[idx].strip())
            idx += 1
        if idx >= len(lines):
            break

        match = SRT_TS_RE.match(lines[idx].strip())
        if not match:
            raise ValueError(f"Invalid SRT timestamp near line {idx + 1}: {lines[idx]}")
        start_us = _srt_ts_to_us(match.group(1), match.group(2), match.group(3), match.group(4))
        end_us = _srt_ts_to_us(match.group(5), match.group(6), match.group(7), match.group(8))
        if end_us <= start_us:
            raise ValueError(f"SRT cue {cue_index} has end <= start")
        idx += 1

        text_lines: List[str] = []
        while idx < len(lines) and lines[idx].strip():
            text_lines.append(lines[idx])
            idx += 1

        cues.append(
            {
                "index": cue_index,
                "start_us": start_us,
                "end_us": end_us,
                "duration_us": end_us - start_us,
                "text": "\n".join(text_lines),
            }
        )
        auto_index = cue_index + 1
    return cues


def parse_srt_file(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return parse_srt_text(f.read())


def extract_text_from_material(material: Dict[str, Any]) -> str:
    content = material.get("content")
    if not isinstance(content, str):
        return ""
    try:
        parsed = json.loads(content)
    except Exception:
        return content
    text = parsed.get("text")
    return str(text) if text is not None else ""


def export_srt_from_content(content: Dict[str, Any]) -> str:
    materials = content.get("materials", {}) or {}
    text_materials = {
        item.get("id"): item
        for item in materials.get("texts", [])
        if isinstance(item, dict) and item.get("id")
    }
    cues: List[Dict[str, Any]] = []
    for track in content.get("tracks", []) or []:
        if track.get("type") != "text":
            continue
        for segment in track.get("segments", []) or []:
            timerange = segment.get("target_timerange", {}) or {}
            material = text_materials.get(segment.get("material_id"))
            if not material:
                continue
            start_us = int(timerange.get("start", 0))
            duration_us = int(timerange.get("duration", 0))
            cues.append(
                {
                    "start_us": start_us,
                    "end_us": start_us + duration_us,
                    "text": extract_text_from_material(material),
                }
            )
    cues.sort(key=lambda item: (item["start_us"], item["end_us"], item["text"]))
    chunks: List[str] = []
    for index, cue in enumerate(cues, start=1):
        chunks.append(str(index))
        chunks.append(f"{format_srt_time(cue['start_us'])} --> {format_srt_time(cue['end_us'])}")
        chunks.append(cue["text"])
        chunks.append("")
    return "\n".join(chunks).rstrip() + ("\n" if chunks else "")


def clone_text_style_fields(material: Dict[str, Any]) -> Dict[str, Any]:
    cloned = copy.deepcopy(material)
    return cloned


def apply_text_ranges_to_content(
    material: Dict[str, Any],
    styles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    payload = copy.deepcopy(material)
    try:
        content = json.loads(payload.get("content", "{}"))
    except Exception as exc:
        raise ValueError(f"Invalid text material content: {exc}") from exc
    text = str(content.get("text", ""))
    base_style = copy.deepcopy(content.get("styles", [{}])[0] or {})
    content["styles"] = [base_style]

    for item in styles:
        if not isinstance(item, dict):
            raise ValueError("Each text-ranges style entry must be an object")
        value_range = item.get("range")
        if not isinstance(value_range, list) or len(value_range) != 2:
            raise ValueError("Each text-ranges style entry requires range=[start,end]")
        start, end = int(value_range[0]), int(value_range[1])
        if start < 0 or end <= start or end > len(text):
            raise ValueError(f"Invalid style range [{start}, {end}] for text length {len(text)}")

        style = {
            "fill": copy.deepcopy(base_style.get("fill", {})),
            "range": [start, end],
            "size": float(item.get("font_size", item.get("size", base_style.get("size", 5.0)))),
            "bold": bool(item.get("bold", base_style.get("bold", False))),
            "italic": bool(item.get("italic", base_style.get("italic", False))),
            "underline": bool(item.get("underline", base_style.get("underline", False))),
            "strokes": copy.deepcopy(base_style.get("strokes", [])),
        }
        if "color" in item and item.get("color"):
            hex_color = str(item["color"]).strip().lstrip("#")
            if len(hex_color) != 6:
                raise ValueError(f"Invalid color: {item['color']}")
            style["fill"] = {
                "alpha": 1.0,
                "content": {
                    "render_type": "solid",
                    "solid": {
                        "alpha": 1.0,
                        "color": [
                            int(hex_color[0:2], 16) / 255.0,
                            int(hex_color[2:4], 16) / 255.0,
                            int(hex_color[4:6], 16) / 255.0,
                        ],
                    },
                },
            }
        if item.get("text_effect"):
            style["effectStyle"] = {"id": str(item["text_effect"]), "path": "C:"}
        content["styles"].append(style)

    payload["content"] = json.dumps(content, ensure_ascii=False)
    return payload


def normalize_time_offset(raw: Optional[Any]) -> int:
    if raw is None:
        return 0
    return int(safe_tim(raw))
