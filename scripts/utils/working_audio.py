"""Working narration identity and conservative preservation-of-timeline checks.

The check measures local energy envelopes; it never moves or resamples a draft
segment. Uncorrelated/silent inputs fail closed instead of implying sync from
container duration. PCM decoding here is diagnostic only.
"""

from __future__ import annotations

import hashlib
import math
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

SYNC_VERSION = "preserve_timeline_envelope_v1"
SAMPLE_RATE = 8000
HOP_SECONDS = 0.01
MAX_OFFSET_SECONDS = 0.04
MAX_DRIFT_SECONDS = 0.04
MIN_CORRELATION = 0.85
MIN_PEAK_MARGIN = 0.05
MAX_DURATION_EXCESS_SECONDS = 0.05


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode(path: Path, ffmpeg_bin: str):
    import numpy as np

    result = subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode or not result.stdout:
        raise ValueError("Working audio synchronization cannot decode the selected media")
    data = np.frombuffer(result.stdout, dtype="<f4")
    if not np.isfinite(data).all():
        raise ValueError("Working audio contains non-finite decoded samples")
    return data


def validate_working_audio_sync(
    original_path: str | os.PathLike[str],
    working_path: str | os.PathLike[str],
    *,
    duration_seconds: float,
    ffmpeg_bin: str = "ffmpeg",
) -> dict[str, Any]:
    """Require coverage, <=40 ms offset/drift, and unique local correlation.

    Each consecutive <=10 s window must have correlation >=.85 and beat any
    candidate >40 ms away by >=.05. Search spans +/-500 ms. Equality by byte hash
    is an exact identity proof; transformed files must provide measurable energy.
    """
    import numpy as np

    original = Path(original_path).resolve(strict=True)
    working = Path(working_path).resolve(strict=True)
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Working audio requires positive finite timeline duration")
    original_hash, working_hash = _sha256(original), _sha256(working)
    source = _decode(original, ffmpeg_bin)
    candidate = _decode(working, ffmpeg_bin)
    target_samples = int(round(duration * SAMPLE_RATE))
    # One diagnostic sample is only float-rounding tolerance, never gap padding.
    if len(candidate) < target_samples - 1 or len(source) < target_samples - 1:
        raise ValueError("Working audio does not cover the unchanged source timeline")
    if len(candidate) / SAMPLE_RATE - duration > MAX_DURATION_EXCESS_SECONDS:
        raise ValueError("Working audio duration exceeds the 50 ms preservation tolerance")
    report: dict[str, Any] = {
        "status": "pass",
        "strategy": SYNC_VERSION,
        "original_path": str(original),
        "working_path": str(working),
        "original_sha256": original_hash,
        "working_sha256": working_hash,
        "duration_seconds": duration,
        "working_duration_seconds": len(candidate) / SAMPLE_RATE,
        "max_offset_seconds": MAX_OFFSET_SECONDS,
        "max_drift_seconds": MAX_DRIFT_SECONDS,
        "min_correlation": MIN_CORRELATION,
        "min_peak_margin": MIN_PEAK_MARGIN,
        "windows": [],
        "timeline_modified": False,
    }
    if original_hash == working_hash:
        if _sha256(original) != original_hash or _sha256(working) != working_hash:
            raise ValueError("Working audio changed during synchronization validation")
        report["identity_proof"] = "identical_bytes"
        return report
    hop = round(SAMPLE_RATE * HOP_SECONDS)
    count = min(target_samples, len(source), len(candidate)) // hop
    if count < 100:
        raise ValueError("Working audio synchronization needs at least one second of evidence")

    def envelope(data):
        blocks = data[: count * hop].reshape(count, hop).astype(np.float64)
        return np.log1p(1000 * np.sqrt(np.mean(blocks * blocks, axis=1)))

    left, right = envelope(source), envelope(candidate)
    # At least three windows expose local drift even on short material.
    width = max(100, min(1000, count // 3))
    starts = list(range(0, count - width + 1, width))
    if starts[-1] + width < count:
        starts.append(count - width)
    offsets = []
    for start in starts:
        end = start + width
        scores = []
        for lag in range(-50, 51):
            low, high = max(start, -lag), min(end, count - lag)
            a, b = left[low:high], right[low + lag : high + lag]
            if len(a) < 50 or np.std(a) < 0.02 or np.std(b) < 0.02:
                score = -1.0
            else:
                score = float(np.corrcoef(a, b)[0, 1])
            scores.append((score, lag))
        best, lag = max(scores, key=lambda row: (row[0], -abs(row[1])))
        alternative = max(score for score, other in scores if abs(other - lag) > 4)
        if best < MIN_CORRELATION or best - alternative < MIN_PEAK_MARGIN:
            raise ValueError(
                "Working audio synchronization is ambiguous or lacks correlated evidence"
            )
        offset = round(lag * HOP_SECONDS, 6)
        if abs(offset) > MAX_OFFSET_SECONDS:
            raise ValueError("Working audio offset exceeds 40 ms; restore the original timeline")
        offsets.append(offset)
        report["windows"].append(
            {
                "start": start * HOP_SECONDS,
                "end": end * HOP_SECONDS,
                "offset_seconds": offset,
                "correlation": best,
                "peak_margin": best - alternative,
            }
        )
    if max(offsets) - min(offsets) > MAX_DRIFT_SECONDS:
        raise ValueError("Working audio local drift exceeds 40 ms")
    if _sha256(original) != original_hash or _sha256(working) != working_hash:
        raise ValueError("Working audio changed during synchronization validation")
    return report


def saved_working_audio_errors(
    content: Mapping[str, Any], bindings: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Independently inspect saved material identity and audible lane ownership."""
    errors: list[str] = []
    if not bindings:
        return errors

    def fail(message):
        errors.append("Lite working audio: " + message)

    def path_key(value):
        return os.path.normcase(os.path.abspath(str(value or "")))

    materials: dict[str, Mapping[str, Any]] = {}
    fades = {
        str(row.get("id") or ""): row
        for row in (content.get("materials") or {}).get("audio_fades", [])
    }
    paths: set[str] = set()
    for bucket in ("videos", "audios"):
        for row in (content.get("materials") or {}).get(bucket, []):
            mid = str(row.get("id") or row.get("material_id") or "")
            if mid in materials:
                fail("duplicate material identity " + mid)
            materials[mid] = row
            paths.add(path_key(row.get("path") or row.get("media_path")))
    for binding in bindings:
        for field in ("working_path", "original_path"):
            if not binding.get(field) or path_key(binding[field]) not in paths:
                fail(field + " is missing from the material library")
        for field, hash_field in (
            ("working_path", "working_sha256"),
            ("original_path", "original_sha256"),
        ):
            expected_hash = binding.get(hash_field)
            if expected_hash:
                media = Path(str(binding[field]))
                if not media.is_file() or _sha256(media) != expected_hash:
                    fail(field + " bytes do not match their declared SHA-256")
    for track in content.get("tracks") or []:
        name, kind = str(track.get("name") or ""), track.get("type")
        is_work = kind == "audio" and name in {"Separated Source Audio", "Lite Reused Audio"}
        muted = bool(int(track.get("attribute", 0)) & 1) or bool(track.get("mute", False))
        if is_work and muted:
            fail(name + " track is muted")
        for segment in track.get("segments") or []:
            volume = float(segment.get("volume", 1.0))
            if not math.isfinite(volume):
                fail(name + " has invalid volume")
                continue
            if kind == "video" and abs(volume) > 1e-6:
                fail(name + " embedded sound must be muted")
            if kind == "audio" and not is_work and abs(volume) > 1e-6:
                fail(name + " introduces audible reference or duplicate narration")
            if not is_work:
                continue
            if abs(volume - 1.0) > 1e-6:
                fail(name + " must retain normal audible volume")
            fade_payloads = [
                fades[str(ref)]
                for ref in segment.get("extra_material_refs") or []
                if str(ref) in fades
            ]
            if segment.get("audio_fade"):
                direct = segment["audio_fade"]
                fade_payloads.append(
                    fades.get(str(direct), direct) if not isinstance(direct, dict) else direct
                )
            for fade in fade_payloads:
                try:
                    values = [
                        float(fade.get(key, 0))
                        for key in (
                            "fade_in_duration",
                            "fade_out_duration",
                            "in_duration",
                            "out_duration",
                        )
                    ]
                    valid = all(math.isfinite(value) and value == 0 for value in values)
                except (TypeError, ValueError, AttributeError):
                    valid = False
                if not valid:
                    fail(name + " has nonzero or invalid narration fades")
            target, source = (
                segment.get("target_timerange") or {},
                segment.get("source_timerange") or {},
            )
            start, length = int(target.get("start", -1)), int(target.get("duration", 0))
            source_start, source_length = int(source.get("start", -1)), int(
                source.get("duration", 0)
            )
            owners = [
                row
                for row in bindings
                if start >= round(float(row["offset"]) * 1e6) - 1
                and start + length
                <= round((float(row["offset"]) + float(row["duration"])) * 1e6) + 1
            ]
            if length <= 0 or len(owners) != 1:
                fail(name + " range has no unique source pair")
                continue
            owner = owners[0]
            material = materials.get(str(segment.get("material_id") or ""), {})
            if path_key(material.get("path") or material.get("media_path")) != path_key(
                owner["working_path"]
            ):
                fail(name + " references the wrong working source")
            if (
                material.get("duration") is not None
                and source_start + source_length > int(material["duration"]) + 1
            ):
                fail(name + " exceeds the working material duration")
            if (
                source_length != length
                or abs(source_start - (start - round(float(owner["offset"]) * 1e6))) > 1
            ):
                fail(name + " source and target ranges are not identical")
            if abs(float(segment.get("speed", 1.0)) - 1.0) > 1e-6:
                fail(name + " changes playback speed")
            if any(
                str(row.get("property_type") or "").casefold()
                in {"volume", "kfvolume", "kftypevolume"}
                for row in segment.get("common_keyframes") or []
            ):
                fail(name + " has an unsupported volume keyframe")
    return errors
