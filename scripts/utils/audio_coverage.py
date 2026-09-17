"""Prove an unpadded alignment WAV contains the complete authoritative audio.

Timeline tail tolerance belongs to the caller. This proof never pads, trims,
stretches, or skips silent samples, and never uses video duration as audio length.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import wave
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO

POLICY_VERSION = "authoritative_audio_pcm_coverage_v1"
ALIGNMENT_SAMPLE_RATE = 16000
NATIVE_TAIL_TOLERANCE_SECONDS = 0.05
_CHUNK_BYTES = 1024 * 1024


class AudioCoverageError(ValueError):
    """Audio integrity or original timestamp coverage could not be proved."""


def _hash_stream(stream: BinaryIO) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return _hash_stream(stream)[0]


def _alignment_pcm(path: Path) -> tuple[str, int]:
    try:
        with wave.open(str(path), "rb") as stream:
            if (
                stream.getnchannels() != 1
                or stream.getframerate() != ALIGNMENT_SAMPLE_RATE
                or stream.getsampwidth() != 2
                or stream.getcomptype() != "NONE"
            ):
                raise AudioCoverageError("Alignment audio must be 16 kHz mono PCM16 WAV")
            expected = stream.getnframes()
            digest, read_bytes = hashlib.sha256(), 0
            while chunk := stream.readframes(_CHUNK_BYTES // 2):
                digest.update(chunk)
                read_bytes += len(chunk)
            if not expected or read_bytes != expected * 2:
                raise AudioCoverageError("Alignment audio PCM is empty or truncated")
            return digest.hexdigest(), expected
    except (wave.Error, EOFError) as exc:
        raise AudioCoverageError("Alignment audio has an invalid WAV header") from exc


def _source_frames(path: Path, ffprobe_bin: str) -> dict[str, Any]:
    """Inspect decoded frames, after codec priming/skip metadata is applied.

    Compare each integer PTS to the cumulative native sample position with exact
    rational arithmetic. At most one native sample is allowed for timestamp
    quantization; this allowance never applies to lost decoded PCM samples.
    """
    try:
        result = subprocess.run(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate,time_base",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode or result.stderr.strip():
            raise AudioCoverageError("Unable to inspect authoritative audio stream")
        rows = json.loads(result.stdout).get("streams", [])
        if len(rows) != 1:
            raise AudioCoverageError("Authoritative source needs one selected audio stream")
        sample_rate = int(rows[0]["sample_rate"])
        time_base = Fraction(rows[0]["time_base"])
        if sample_rate <= 0 or time_base <= 0:
            raise ValueError("invalid stream timing")
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        if isinstance(exc, AudioCoverageError):
            raise
        raise AudioCoverageError("Authoritative audio lacks valid native sample timing") from exc

    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_frames",
        "-show_entries",
        "frame=pts,best_effort_timestamp,nb_samples,sample_rate",
        "-of",
        "compact=p=1:nk=0",
        str(path),
    ]
    count = frame_count = 0
    first_pts: Fraction | None = None
    max_error = Fraction(0)
    tolerance = Fraction(1, sample_rate)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                parts = line.strip().split("|")
                if not parts or parts[0] != "frame":
                    continue
                row = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
                try:
                    samples = int(row["nb_samples"])
                    stamp = row.get("pts", row.get("best_effort_timestamp"))
                    pts = int(stamp) * time_base
                    rate = int(row.get("sample_rate", sample_rate))
                except (KeyError, TypeError, ValueError) as exc:
                    raise AudioCoverageError("A decoded audio frame lacks sample timing") from exc
                if samples <= 0 or rate != sample_rate:
                    raise AudioCoverageError("Decoded audio has invalid or changing sample rate")
                if first_pts is None:
                    first_pts = pts
                    if abs(first_pts) > tolerance:
                        raise AudioCoverageError(
                            "Authoritative audio has nonzero start; preserve its timestamp offset"
                        )
                error = abs(pts - (first_pts + Fraction(count, sample_rate)))
                max_error = max(max_error, error)
                if error > tolerance:
                    raise AudioCoverageError(
                        "Authoritative audio timestamps contain a gap, overlap, or drift"
                    )
                count += samples
                frame_count += 1
            returncode = process.wait()
            errors.seek(0, os.SEEK_END)
            if returncode or errors.tell():
                raise AudioCoverageError("Unable to verify authoritative decoded audio frames")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
    if first_pts is None or not count:
        raise AudioCoverageError("Authoritative source has no decoded audio samples")
    duration = Fraction(count, sample_rate)
    return {
        "source_native_rate": sample_rate,
        "source_native_frames": count,
        "source_decoded_frame_count": frame_count,
        "source_time_base": str(time_base),
        "start_seconds": float(first_pts),
        "end_seconds": float(first_pts + duration),
        "source_effective_duration_seconds": float(duration),
        "source_start_rational": str(first_pts),
        "source_end_rational": str(first_pts + duration),
        "timestamps_contiguous": True,
        "timestamp_tolerance_native_samples": 1,
        "max_timestamp_error_native_samples": float(max_error * sample_rate),
        "native_expected_16k_frames": round(duration * ALIGNMENT_SAMPLE_RATE),
    }


def _canonical_pcm(path: Path, ffmpeg_bin: str) -> tuple[str, int]:
    # Raw PCM uses exactly the maintained alignment recipe, without WAV headers.
    # Disk-backed streams keep both the decode and error output bounded in RAM.
    with tempfile.TemporaryFile() as decoded, tempfile.TemporaryFile() as errors:
        result = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(ALIGNMENT_SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "pipe:1",
            ],
            stdout=decoded,
            stderr=errors,
            check=False,
        )
        errors.seek(0, os.SEEK_END)
        if result.returncode or errors.tell():
            raise AudioCoverageError("Authoritative audio could not be decoded without errors")
        decoded.seek(0)
        digest, size = _hash_stream(decoded)
    if not size or size % 2:
        raise AudioCoverageError("Authoritative decode has empty or invalid PCM data")
    return digest, size // 2


def probe_source_audio_coverage(
    source_path: str | os.PathLike[str], *, ffprobe_bin: str = "ffprobe"
) -> dict[str, Any]:
    """Return stable native decoded coverage; fail on offset or discontinuity.

    This proves source timestamp coverage only. Processing integrity additionally
    requires ``verify_alignment_source`` against the actual unpadded alignment.
    """
    try:
        source = Path(source_path).expanduser().resolve(strict=True)
        source_hash = _sha256(source)
        timing = _source_frames(source, ffprobe_bin)
        if _sha256(source) != source_hash:
            raise AudioCoverageError("Source changed during native audio coverage verification")
        return {
            "status": "pass",
            "policy_version": POLICY_VERSION,
            "source_sha256": source_hash,
            **timing,
        }
    except OSError as exc:
        raise AudioCoverageError("Audio coverage input or media tool is unavailable") from exc


# Short form retained for callers that do not need to distinguish source probes
# from alignment integrity verification by name.
probe_source_audio = probe_source_audio_coverage


def verify_alignment_source(
    alignment_path: str | os.PathLike[str],
    source_path: str | os.PathLike[str],
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
) -> dict[str, Any]:
    """Compare an unpadded alignment to a fresh original-source decode exactly.

    The caller must supply the original video (video_original) or the selected
    replacement asset (replace_original), never an intermediate extracted WAV.
    No duration tolerance can turn a PCM count/hash mismatch into a pass.
    """
    try:
        alignment = Path(alignment_path).expanduser().resolve(strict=True)
        source = Path(source_path).expanduser().resolve(strict=True)
        source_hash, alignment_hash = _sha256(source), _sha256(alignment)
        alignment_pcm_hash, alignment_frames = _alignment_pcm(alignment)
        timing = _source_frames(source, ffprobe_bin)
        canonical_hash, canonical_frames = _canonical_pcm(source, ffmpeg_bin)
        if alignment_frames != canonical_frames or alignment_pcm_hash != canonical_hash:
            raise AudioCoverageError(
                "Alignment audio differs from complete authoritative decode: "
                f"expected_frames={canonical_frames}, actual_frames={alignment_frames}; "
                "processing loss or changed PCM is not a native tail difference"
            )
        if abs(canonical_frames - timing["native_expected_16k_frames"]) > 1:
            raise AudioCoverageError(
                "Authoritative resampling count disagrees with decoded native audio samples"
            )
        if _sha256(source) != source_hash or _sha256(alignment) != alignment_hash:
            raise AudioCoverageError("Audio changed during independent integrity verification")
        return {
            "status": "pass",
            "policy_version": POLICY_VERSION,
            "source_sha256": source_hash,
            "alignment_sha256": alignment_hash,
            "alignment_pcm_sha256": alignment_pcm_hash,
            "alignment_frames": alignment_frames,
            "authoritative_pcm_sha256": canonical_hash,
            "authoritative_16k_frames": canonical_frames,
            "sample_rate": ALIGNMENT_SAMPLE_RATE,
            "pcm_exact_match": True,
            "resample_rounding_tolerance_frames": 1,
            **timing,
        }
    except OSError as exc:
        raise AudioCoverageError("Audio integrity input or media tool is unavailable") from exc
