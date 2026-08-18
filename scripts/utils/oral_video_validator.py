import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ValidationConfig = Dict[str, Any]


def _read_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _seconds_to_us(value: float) -> int:
    return int(round(float(value) * 1_000_000))


def _range_us(segment: dict) -> tuple[int, int]:
    timerange = segment.get("target_timerange", {}) or {}
    start = int(timerange.get("start", 0) or 0)
    duration = int(timerange.get("duration", 0) or 0)
    return start, start + duration


def _material_path(item: dict) -> str:
    for key in ("path", "file_path", "material_url", "media_path"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _find_track(content: Dict[str, Any], name: str) -> Optional[dict]:
    for track in content.get("tracks", []) or []:
        if str(track.get("name", "") or "") == name:
            return track
    return None


def _collect_font_names(materials: dict) -> set[str]:
    names: set[str] = set()
    for item in materials.get("texts", []) or []:
        if not isinstance(item, dict):
            continue
        for key in ("font_name", "font_title"):
            value = str(item.get(key) or "").strip()
            if value:
                names.add(value)
        try:
            payload = json.loads(item.get("content") or "{}")
        except Exception:
            payload = {}
        for style in payload.get("styles", []) or []:
            font = style.get("font") or {}
            for key in ("id", "name", "title"):
                value = str(font.get(key) or "").strip()
                if value:
                    names.add(value)
    return names


def _measure_audio_file_peak_dbfs(path: str, duration_s: Optional[float] = None) -> Optional[float]:
    if not path or not os.path.exists(path):
        return None
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        path,
    ]
    if duration_s is not None and duration_s > 0:
        command.extend(["-t", f"{duration_s:.3f}"])
    command.extend(["-af", "volumedetect", "-f", "null", "-"])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    stderr = result.stderr or ""
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr)
    if not match:
        return None
    return float(match.group(1))


def _default_mojibake_detector(text: str) -> bool:
    markers = (
        "閿",
        "锟",
        "婢",
        "鈺",
        "绗",
        "鐏",
        "鐐",
        "濮",
        "鏉",
        "鍛",
        "顕",
        "\ufffd",
    )
    return any(marker in str(text or "") for marker in markers)


def _validate_no_mojibake_texts(
    label: str,
    texts: List[str],
    detector: Optional[Callable[[str], bool]] = None,
) -> None:
    checker = detector or _default_mojibake_detector
    bad = [str(text) for text in texts if checker(str(text or ""))]
    if bad:
        sample = ", ".join(bad[:3])
        raise RuntimeError(f"Suspected mojibake in {label}: {sample}")


def _validate_local_media_paths(materials: dict) -> list[dict]:
    rows: list[dict] = []
    missing: list[str] = []
    for bucket in ("videos", "audios"):
        for item in materials.get(bucket, []) or []:
            if not isinstance(item, dict):
                continue
            path = _material_path(item)
            name = str(item.get("name") or item.get("material_name") or item.get("id") or "")
            if not path:
                missing.append(f"{bucket}:{name}: empty path")
                continue
            if path.startswith("cloud_music_"):
                missing.append(f"{bucket}:{name}: placeholder path {path}")
                continue
            if not os.path.exists(path):
                missing.append(f"{bucket}:{name}: missing {path}")
                continue
            rows.append({"bucket": bucket, "name": name, "path": path})
    if missing:
        raise RuntimeError("Missing local media files in saved draft: " + " | ".join(missing[:8]))
    return rows


def _validate_bgm_library_source(materials: dict, bgm_library_ids: set[str]) -> dict:
    audios = materials.get("audios", []) or []
    bgm_items = []
    for item in audios:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("material_name") or item.get("id") or "")
        path = _material_path(item)
        music_id = str(item.get("music_id") or item.get("id") or "")
        if (
            name == "BGM"
            or music_id in bgm_library_ids
            or any(str(item.get(key) or "") in bgm_library_ids for key in ("music_id", "id"))
        ):
            bgm_items.append({"name": name, "music_id": music_id, "path": path})
    if not bgm_items:
        raise RuntimeError("Missing BGM library material reference")

    accepted = []
    for item in bgm_items:
        music_id = item["music_id"]
        if music_id not in bgm_library_ids:
            raise RuntimeError(
                f"BGM is not sourced from JianYing music library: {music_id or item['path']}"
            )
        accepted.append(item)
    return {"count": len(accepted), "items": accepted}


def _validate_bgm_peak_target(
    tracks: list[dict], materials: dict, target_dbfs: float, timeline_duration: Optional[float]
) -> dict:
    bgm_track = _find_track({"tracks": tracks}, "BGM")
    if not bgm_track:
        raise RuntimeError("Missing BGM track for peak validation")
    segments = bgm_track.get("segments", []) or []
    if len(segments) != 1:
        raise RuntimeError(f"Expected one BGM segment for peak validation, got {len(segments)}")
    segment = segments[0]
    material_id = str(segment.get("material_id") or "")
    audio = next(
        (
            item
            for item in materials.get("audios", []) or []
            if isinstance(item, dict) and str(item.get("id") or "") == material_id
        ),
        None,
    )
    if audio is None:
        raise RuntimeError(f"Missing BGM audio material for peak validation: {material_id}")
    path = _material_path(audio)
    source_peak = _measure_audio_file_peak_dbfs(path, timeline_duration)
    if source_peak is None:
        raise RuntimeError(f"Unable to measure BGM peak: {path}")
    volume = float(segment.get("volume", 1.0) or 1.0)
    adjusted_peak = source_peak + 20.0 * math.log10(max(volume, 1e-9))
    if abs(adjusted_peak - target_dbfs) > 0.25:
        raise RuntimeError(
            f"BGM peak target mismatch: source={source_peak:.2f}dBFS volume={volume:.6f} "
            f"adjusted={adjusted_peak:.2f}dBFS target={target_dbfs:.2f}dBFS"
        )
    return {
        "source_peak_dbfs": round(source_peak, 2),
        "volume": volume,
        "adjusted_peak_dbfs": round(adjusted_peak, 2),
        "target_peak_dbfs": target_dbfs,
    }


def _validate_voice_track_layout(tracks: list[dict]) -> dict:
    main_video = _find_track({"tracks": tracks}, "Main Video")
    source_audio = _find_track({"tracks": tracks}, "Source Audio")
    main_segments = main_video.get("segments", []) if main_video else []
    if not main_segments:
        raise RuntimeError("Missing Main Video track for voice-layout validation")

    audible_main = [
        float(seg.get("volume", 1.0) or 0.0)
        for seg in main_segments
        if float(seg.get("volume", 1.0) or 0.0) > 0.0001
    ]
    if not audible_main:
        raise RuntimeError("Main Video embedded voice is unexpectedly muted")
    if source_audio and (source_audio.get("segments", []) or []):
        raise RuntimeError(
            "Unexpected duplicate Source Audio track when Main Video already carries voice"
        )
    return {"embedded_voice_segments": len(audible_main), "source_audio_segments": 0}


def validate_oral_video_draft(draft_path: str, config: ValidationConfig) -> dict:
    content = _read_json(Path(draft_path) / "draft_content.json")
    tracks = content.get("tracks", []) or []
    materials = content.get("materials", {}) or {}
    track_summary = {
        track.get("name", ""): len(track.get("segments", []) or []) for track in tracks
    }

    text_bucket = [item for item in materials.get("texts", []) or [] if isinstance(item, dict)]
    text_values = [str(item.get("content") or item.get("text") or "") for item in text_bucket]
    _validate_no_mojibake_texts(
        "draft text materials", text_values, config.get("mojibake_detector")
    )

    required_texts = [
        str(item) for item in config.get("required_flower_texts", []) or [] if str(item).strip()
    ]
    missing_required = [text for text in required_texts if text not in text_values]
    if missing_required:
        raise RuntimeError("Missing required flower texts: " + ", ".join(missing_required[:8]))

    final_tracks = [name for name in track_summary if name in {"Final Video", "Final Audio"}]
    if final_tracks:
        raise RuntimeError(f"Draft contains flattened preview tracks: {final_tracks}")
    if track_summary.get("Main Video", 0) <= int(config.get("min_main_video_segments", 1)):
        raise RuntimeError("Main Video collapsed into one segment")
    if track_summary.get("BGM", 0) < int(config.get("min_bgm_segments", 1)):
        raise RuntimeError("Missing BGM track")

    min_sfx_segments = int(config.get("min_sfx_segments", 1))
    sfx_segment_count = int(track_summary.get("SFX", 0))
    if sfx_segment_count < min_sfx_segments:
        if min_sfx_segments > 1:
            raise RuntimeError(
                f"Missing SFX track segments: expected {min_sfx_segments}, got {sfx_segment_count}"
            )
        raise RuntimeError("Missing SFX track")

    if track_summary.get("Subtitles", 0) < int(config.get("min_subtitle_segments", 1)):
        raise RuntimeError("Missing subtitles track")

    flower_track_prefixes = tuple(config.get("flower_track_prefixes", ("Flower",)))
    flower_count = sum(
        count
        for name, count in track_summary.items()
        if any(str(name).startswith(prefix) for prefix in flower_track_prefixes)
    )
    if flower_count < int(config.get("expected_flower_segments", 1)):
        raise RuntimeError("Missing flower text/template segments")

    forbidden_track_names = set(config.get("forbidden_track_names", []) or [])
    present_forbidden = sorted(name for name in track_summary if name in forbidden_track_names)
    if present_forbidden:
        raise RuntimeError(f"Draft contains forbidden tracks: {present_forbidden}")

    font_names = _collect_font_names(materials)
    expected_font_name = str(config.get("flower_font_name") or "").strip()
    expected_font_resource_id = str(config.get("flower_font_resource_id") or "").strip()
    if (
        expected_font_name
        and expected_font_name not in font_names
        and (not expected_font_resource_id or expected_font_resource_id not in font_names)
    ):
        raise RuntimeError(f"Flower font was not locked to {expected_font_name}")

    flower_material_reader = config.get("flower_text_materials_reader")
    effect_style_rows: list[dict] = []
    allowed_effect_style_ids = {
        str(item).strip()
        for item in config.get("allowed_flower_effect_style_ids", []) or []
        if str(item).strip()
    }
    if callable(flower_material_reader):
        for material in flower_material_reader(content):
            if not isinstance(material, dict):
                continue
            font_name = str(material.get("font_name") or "").strip()
            font_title = str(material.get("font_title") or "").strip()
            font_id = str((material.get("style_font", {}) or {}).get("id") or "").strip()
            if (
                expected_font_name
                and expected_font_name not in {font_name, font_title}
                and font_id != expected_font_resource_id
            ):
                raise RuntimeError(
                    f"FlowerText font mismatch: expected {expected_font_name}/{expected_font_resource_id}, "
                    f"got {font_name or font_title or font_id}"
                )
            effect_style = material.get("effect_style")
            if effect_style:
                effect_id = str((effect_style or {}).get("id") or "").strip()
                if allowed_effect_style_ids and effect_id not in allowed_effect_style_ids:
                    raise RuntimeError(
                        f"FlowerText effectStyle is not in the allowed favorite-flower set: {effect_style}"
                    )
                effect_style_rows.append(effect_style)
    if allowed_effect_style_ids and not effect_style_rows:
        raise RuntimeError("FlowerText is missing required favorite-flower effectStyle")
    if effect_style_rows and bool(config.get("fail_on_flower_effect_style", False)):
        raise RuntimeError(
            f"FlowerText still carries template effectStyle: {effect_style_rows[:3]}"
        )

    local_media_paths = _validate_local_media_paths(materials)

    bgm_source = None
    bgm_library_ids = set(config.get("bgm_library_ids", []) or [])
    if bgm_library_ids:
        bgm_source = _validate_bgm_library_source(materials, bgm_library_ids)

    bgm_peak = None
    if config.get("bgm_target_dbfs") is not None:
        bgm_peak = _validate_bgm_peak_target(
            tracks,
            materials,
            float(config["bgm_target_dbfs"]),
            config.get("timeline_duration"),
        )

    voice_layout = None
    if bool(config.get("validate_voice_layout", True)):
        voice_layout = _validate_voice_track_layout(tracks)

    extra_validator = config.get("extra_validator")
    extra = extra_validator(content) if callable(extra_validator) else None

    return {
        "tracks": track_summary,
        "font_names": sorted(font_names),
        "bgm_source": bgm_source,
        "bgm_peak": bgm_peak,
        "voice_layout": voice_layout,
        "local_media_paths": len(local_media_paths),
        "material_counts": {
            key: len(value) for key, value in materials.items() if isinstance(value, list)
        },
        "extra": extra,
    }
