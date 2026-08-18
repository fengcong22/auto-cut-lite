import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _seconds(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _delete_windows(edits: List[Dict[str, Any]]) -> List[List[float]]:
    windows = []
    for item in edits:
        if item.get("type") != "delete":
            continue
        start = _seconds(item.get("start"))
        end = _seconds(item.get("end"))
        if end > start:
            windows.append([start, end])
    return sorted(windows)


def _interval_is_deleted(start: float, end: float, delete_windows: List[List[float]]) -> bool:
    for delete_start, delete_end in delete_windows:
        if start >= delete_start - 0.000001 and end <= delete_end + 0.000001:
            return True
    return False


def _keep_windows(
    duration: float,
    delete_windows: List[List[float]],
    split_points: List[float] | None = None,
) -> List[List[float]]:
    points = {0.0, max(0.0, duration)}
    for start, end in delete_windows:
        points.add(max(0.0, min(duration, start)))
        points.add(max(0.0, min(duration, end)))
    for point in split_points or []:
        points.add(max(0.0, min(duration, point)))

    keeps = []
    ordered_points = sorted(points)
    for idx in range(len(ordered_points) - 1):
        start = ordered_points[idx]
        end = ordered_points[idx + 1]
        if end - start <= 0.000001:
            continue
        if _interval_is_deleted(start, end, delete_windows):
            continue
        keeps.append([start, end])
    return keeps


def _pause_adjustments(request: Dict[str, Any]) -> List[Dict[str, Any]]:
    pauses = []
    for item in request.get("pause_adjustments") or []:
        if not isinstance(item, dict):
            continue
        frame_path = str(item.get("frame_path") or item.get("still_frame_path") or "").strip()
        duration = _seconds(item.get("duration"), 0.0)
        source_time = _seconds(item.get("source_time", item.get("source_cut_end")), 0.0)
        if not frame_path or duration <= 0:
            continue
        pauses.append({"frame_path": frame_path, "duration": duration, "source_time": source_time})
    return sorted(pauses, key=lambda row: row["source_time"])


def _deleted_before(point: float, delete_windows: List[List[float]]) -> float:
    total = 0.0
    for start, end in delete_windows:
        if point <= start:
            break
        total += max(0.0, min(point, end) - start)
        if point <= end:
            break
    return total


def _inserted_before(point: float, pauses: List[Dict[str, Any]]) -> float:
    return sum(row["duration"] for row in pauses if row["source_time"] < point - 0.000001)


def _map_source_time(
    point: float, delete_windows: List[List[float]], pauses: List[Dict[str, Any]]
) -> float:
    return max(
        0.0, float(point) - _deleted_before(point, delete_windows) + _inserted_before(point, pauses)
    )


def _visual_segments(edits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    segments = []
    for edit in edits:
        plan = edit.get("visual_plan") if isinstance(edit.get("visual_plan"), dict) else {}
        for segment in plan.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            asset_path = str(segment.get("asset_path") or "").strip()
            if not asset_path:
                continue
            segments.append(
                {
                    "asset_path": asset_path,
                    "asset_type": str(segment.get("asset_type") or "").strip().lower(),
                    "timeline_start": _seconds(
                        segment.get("timeline_start"), _seconds(segment.get("source_start"))
                    ),
                    "duration": max(0.2, _seconds(segment.get("duration"), 1.0)),
                    "scale_x": _seconds(segment.get("scale_x"), 1.0),
                    "scale_y": _seconds(segment.get("scale_y"), 1.0),
                    "transform_x": _seconds(segment.get("transform_x"), 0.0),
                    "transform_y": _seconds(segment.get("transform_y"), 0.0),
                    "keyframes": (
                        segment.get("keyframes")
                        if isinstance(segment.get("keyframes"), list)
                        else []
                    ),
                    "role": str(segment.get("role") or "overlay"),
                }
            )
    return segments


def _is_video_asset(segment: Dict[str, Any]) -> bool:
    asset_type = str(segment.get("asset_type") or "").strip().lower()
    if asset_type in {"video", "source_video", "video_overlay"}:
        return True
    return Path(str(segment.get("asset_path") or "")).suffix.lower() in {
        ".mp4",
        ".mov",
        ".m4v",
        ".webm",
    }


def _linear_keyframe_expr(
    segment: Dict[str, Any],
    property_names: List[str],
    default_value: float,
) -> str:
    keyframes = []
    aliases = {name.lower() for name in property_names}
    for item in segment.get("keyframes") or []:
        if not isinstance(item, dict):
            continue
        prop = str(item.get("property") or item.get("property_type") or "").strip().lower()
        if prop not in aliases:
            continue
        try:
            offset = float(item.get("offset", item.get("time_offset", 0.0)))
            value = float(item["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if offset > 1000:
            offset = offset / 1000000.0
        keyframes.append((max(0.0, offset), value))
    if not keyframes:
        return f"{default_value:.3f}"
    keyframes.sort(key=lambda item: item[0])
    start = float(segment["timeline_start"])
    points = [(start + offset, value) for offset, value in keyframes]
    if len(points) == 1:
        return f"{points[0][1]:.3f}"

    def value_expr(value: float) -> str:
        return f"({value:.6f})"

    expr = value_expr(points[-1][1])
    for index in range(len(points) - 2, -1, -1):
        t0, v0 = points[index]
        t1, v1 = points[index + 1]
        if t1 <= t0:
            continue
        interp = (
            f"({value_expr(v0)}+({value_expr(v1)}-{value_expr(v0)})"
            f"*(t-{t0:.3f})/({t1:.3f}-{t0:.3f}))"
        )
        expr = f"if(lt(t,{t0:.3f}),{value_expr(v0)},if(lt(t,{t1:.3f}),{interp},{expr}))"
    return expr


def _escape_overlay_expression(expr: str) -> str:
    return expr.replace("\\", "\\\\").replace(",", "\\,")


def _processed_audio_path(request: Dict[str, Any]) -> str:
    project = request.get("project") or {}
    replacement_audio = str(project.get("replacement_audio") or "").strip()
    if replacement_audio:
        return replacement_audio
    processed_audio = request.get("processed_audio") or {}
    if not isinstance(processed_audio, dict):
        return ""
    for key in (
        "output_wav",
        "output_audio",
        "audio_path",
        "path",
        "processed_audio",
        "replacement_audio",
        "final_audio",
    ):
        value = str(processed_audio.get(key) or "").strip()
        if value:
            return value
    outputs = processed_audio.get("outputs")
    if isinstance(outputs, dict):
        for key in (
            "output_wav",
            "output_audio",
            "audio",
            "wav",
            "mp3",
            "processed_audio",
            "replacement_audio",
        ):
            value = str(outputs.get(key) or "").strip()
            if value:
                return value
    return ""


def _uses_full_track_replacement_audio(request: Dict[str, Any]) -> bool:
    preserve = request.get("preserve") or {}
    replacement_audio = _processed_audio_path(request)
    replacement_material_required = bool(
        preserve.get("replacement_audio_material", bool(replacement_audio))
    )
    has_replacement_windows = any(
        (item.get("type") == "replace_audio") for item in (request.get("edits") or [])
    )
    return bool(replacement_audio and replacement_material_required and not has_replacement_windows)


def _probe_duration(path: str) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return float((completed.stdout or "0").strip())


def _quote_filter_path(path: str) -> str:
    normalized = os.path.abspath(path).replace("\\", "/")
    return normalized.replace(":", "\\:").replace("'", "\\'")


def render_preview(request_json: str, output_path: str) -> Dict[str, Any]:
    request = _load_json(request_json)
    project = request.get("project") or {}
    source_video = str(project.get("source_video") or "").strip()
    source_audio = str(project.get("source_audio") or "").strip() or source_video
    replacement_audio = _processed_audio_path(request)
    use_replacement_audio = _uses_full_track_replacement_audio(request)
    if not source_video or not os.path.exists(source_video):
        raise FileNotFoundError(source_video)
    if use_replacement_audio:
        if not replacement_audio or not os.path.exists(replacement_audio):
            raise FileNotFoundError(replacement_audio)
    elif not source_audio or not os.path.exists(source_audio):
        raise FileNotFoundError(source_audio)

    duration = _probe_duration(source_video)
    pauses = _pause_adjustments(request)
    deletes = _delete_windows(request.get("edits") or [])
    keeps = _keep_windows(duration, deletes, [pause["source_time"] for pause in pauses])
    visuals = _visual_segments(request.get("edits") or [])

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    concat_inputs = []
    media_idx = 0
    for idx, (start, end) in enumerate(keeps):
        for pause_idx, pause in enumerate(
            item for item in pauses if abs(item["source_time"] - start) <= 0.000001
        ):
            asset = _quote_filter_path(pause["frame_path"])
            label = f"p{idx}_{pause_idx}"
            lines.append(
                f"movie='{asset}':loop=0,trim=duration={pause['duration']:.3f},"
                f"setpts=PTS-STARTPTS,scale=1920:1080:flags=lanczos,format=yuv420p[{label}]"
            )
            concat_inputs.append(f"[{label}]")
        if use_replacement_audio:
            lines.append(
                f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{media_idx}]"
            )
            concat_inputs.append(f"[v{media_idx}]")
        else:
            lines.append(
                f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{media_idx}];"
                f"[1:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{media_idx}]"
            )
            concat_inputs.append(f"[v{media_idx}][a{media_idx}]")
        media_idx += 1
    if use_replacement_audio:
        lines.append(f"{''.join(concat_inputs)}concat=n={len(concat_inputs)}:v=1:a=0[basev]")
    else:
        if pauses:
            raise RuntimeError(
                "pause_adjustments require full-track replacement audio in preview rendering"
            )
        lines.append(f"{''.join(concat_inputs)}concat=n={len(keeps)}:v=1:a=1[basev][outa]")

    current_video = "basev"
    for idx, segment in enumerate(visuals):
        asset = _quote_filter_path(segment["asset_path"])
        start = segment["timeline_start"]
        end = start + segment["duration"]
        scale_x = segment["scale_x"]
        scale_y = segment["scale_y"]
        transform_x = segment["transform_x"]
        transform_y = segment["transform_y"]
        setpts = f",setpts=PTS-STARTPTS+{start:.6f}/TB" if _is_video_asset(segment) else ""
        if abs(scale_x - 1.0) > 0.001 or abs(scale_y - 1.0) > 0.001:
            image_expr = f"movie='{asset}'{setpts},scale=iw*{scale_x:.6f}:ih*{scale_y:.6f},format=rgba[ov{idx}]"
        else:
            image_expr = f"movie='{asset}'{setpts},format=rgba[ov{idx}]"
        # JianYing stores transform/keyframe positions in half-canvas units:
        # x=1.0 means one half-canvas width (960 px on 1920x1080),
        # y=1.0 means one half-canvas height (540 px on 1920x1080).
        kf_x = _linear_keyframe_expr(
            segment,
            ["position_x", "kftypepositionx"],
            transform_x / 960.0,
        )
        kf_y = _linear_keyframe_expr(
            segment,
            ["position_y", "kftypepositiony"],
            transform_y / 540.0,
        )
        x_expr = f"(W-w)/2+({kf_x})*960"
        y_expr = f"(H-h)/2-({kf_y})*540"
        next_video = f"vout{idx}"
        lines.append(image_expr)
        lines.append(
            f"[{current_video}][ov{idx}]overlay="
            f"x='{_escape_overlay_expression(x_expr)}':"
            f"y='{_escape_overlay_expression(y_expr)}':"
            f"enable='between(t,{start:.3f},{end:.3f})'[{next_video}]"
        )
        current_video = next_video

    filter_complex = ";".join(lines)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        source_video,
        "-i",
        replacement_audio if use_replacement_audio else source_audio,
        "-filter_complex",
        filter_complex,
        "-map",
        f"[{current_video}]",
        "-map",
        "1:a:0" if use_replacement_audio else "[outa]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        "-shortest",
        str(output),
    ]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout)[-4000:])
    return {
        "ok": True,
        "output": str(output.resolve()),
        "delete_window_count": len(deletes),
        "keep_window_count": len(keeps),
        "pause_adjustment_count": len(pauses),
        "visual_overlay_count": len(visuals),
        "audio_source": "replacement_audio" if use_replacement_audio else "source_audio",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Render an MP4 preview from a revision request.")
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = render_preview(args.request_json, args.output)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result["output"])
        return 0
    except Exception as exc:
        payload = {"ok": False, "reason": str(exc)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
