from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
import wave
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any

from .bootstrap import _resolve_runtime_path
from .bootstrap import (
    apply_external_model_execution_policy as apply_external_model_execution_policy,
)
from .config import PROJECT_ROOT, resolve_repo_python

SUPPORTED_MEDIA_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".flac",
    ".aac",
    ".ogg",
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
}
SPECTRAMINI_STYLE_SMOKE_ALGORITHM = "auto_cut_spectramini_style_smoke_v1"
SPECTRAMINI_STYLE_SMOKE_REQUIRED_CHECKS = (
    "int16_output",
    "shape_preserved",
    "finite_output",
    "breath_rms_reduced",
    "click_peak_reduced",
    "memory_roundtrip_ok",
    "feature_finite",
    "deterministic",
)
JSON_BLOCK_PATTERN = re.compile(r"\{\s*\"input_i\".*?\}", re.DOTALL)
SILENCE_START_PATTERN = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_PATTERN = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")
DEFAULT_BREATH_FALLBACK_CONFIG = {
    "low_threshold_db": -44,
    "high_threshold_db": -32,
    "silence_min_duration": 0.08,
    "min_breath_ms": 70,
    "max_breath_ms": 320,
    "pre_roll_ms": 25,
    "fade_ms": 14,
    "floor_gain": 0.0,
    "analysis_hop_ms": 18,
    "analysis_scan_ms": 260,
    "noise_floor_ms": 120,
    "speech_start_ratio": 0.78,
    "breath_over_noise_ratio": 2.6,
    "speech_over_breath_ratio": 2.15,
    "speech_confirm_frames": 2,
}
DEEPFILTERNET_LOCKED_VERSION = "0.5.6"
EXECUTION_RECEIPT_FIELDS = {
    "schema_version",
    "kind",
    "asset_identity",
    "package_version",
    "revision",
    "license",
    "model_sha256",
    "weights_sha256",
    "adapter_version",
    "adapter_sha256",
    "input_sha256",
    "output_sha256",
    "model_loaded",
    "execution_id",
}
_VERIFIED_RUNTIME_RECEIPTS: dict[object, tuple[str, ...]] = {}


def _runtime_receipt_payload(
    runtime: dict[str, Any],
    *,
    kind: str,
) -> tuple[str, ...] | None:
    adapter_bytes = runtime.get("adapter_bytes")
    common_keys = (
        "identity",
        "asset_identity",
        "python_executable",
        "adapter_path",
        "repo_root",
    )
    if (
        not isinstance(adapter_bytes, bytes)
        or not adapter_bytes
        or not all(runtime.get(key) for key in common_keys)
    ):
        return None
    payload = [
        kind,
        str(runtime["identity"]),
        str(runtime["asset_identity"]),
        str(runtime["python_executable"]),
        str(runtime["adapter_path"]),
        sha256(adapter_bytes).hexdigest(),
        str(runtime["repo_root"]),
        str(runtime.get("verification_env_path") or ""),
    ]
    kind_keys = {
        "deepfilternet": (
            "model_path",
            "model_sha256",
            "adapter_version",
            "package_version",
        ),
        "respiro": ("repo_path", "weights_path"),
    }.get(kind)
    if kind_keys is None or not all(runtime.get(key) for key in kind_keys):
        return None
    payload.extend(str(runtime[key]) for key in kind_keys)
    payload.extend(
        str(runtime.get(key) or "")
        for key in (
            "revision",
            "license",
            "weights_sha256",
            "execution_id",
            "execution_started_ns",
        )
    )
    return tuple(payload)


def _mint_verified_runtime(
    runtime: dict[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    payload = _runtime_receipt_payload(runtime, kind=kind)
    if payload is None:
        raise RuntimeError("verified runtime receipt is incomplete")
    receipt = object()
    _VERIFIED_RUNTIME_RECEIPTS[receipt] = payload
    runtime["_verification_receipt"] = receipt
    return runtime


def _verified_runtime_receipt_matches(
    runtime: dict[str, Any] | None,
    *,
    kind: str,
    python_executable: str,
) -> bool:
    if not runtime:
        return False
    receipt = runtime.get("_verification_receipt")
    expected = _VERIFIED_RUNTIME_RECEIPTS.get(receipt)
    current = _runtime_receipt_payload(runtime, kind=kind)
    return bool(
        expected
        and current == expected
        and str(runtime.get("python_executable") or "") == python_executable
    )


def _raise_external_model_execution_unavailable(kind: str) -> None:
    hint = (
        " Rerun with --skip-deepfilternet to use only local deterministic stages."
        if kind == "deepfilternet"
        else " Remove the configured Respiro paths to use local fallback detection."
    )
    raise RuntimeError(
        f"external_model_execution_unavailable: {kind} cannot execute until its adapter "
        f"can consume validation-bound immutable model assets.{hint}"
    )


def _sha256_path(path: Path) -> str | None:
    try:
        digest = sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def validate_execution_receipt(
    receipt: dict[str, Any],
    *,
    kind: str,
    expected_runtime: dict[str, Any],
    input_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    python_executable = expected_runtime.get("python_executable")
    if not isinstance(python_executable, str) or not _verified_runtime_receipt_matches(
        expected_runtime,
        kind=kind,
        python_executable=python_executable,
    ):
        raise RuntimeError("invalid_execution_receipt: unverified_execution_runtime")
    if not isinstance(receipt, dict) or set(receipt) != EXECUTION_RECEIPT_FIELDS:
        raise RuntimeError("invalid_execution_receipt: receipt fields are incomplete or unknown")
    if receipt.get("schema_version") != 1 or receipt.get("kind") != kind:
        raise RuntimeError("invalid_execution_receipt: schema or model kind does not match")
    if receipt.get("model_loaded") is not True:
        raise RuntimeError("invalid_execution_receipt: model_loaded must be true")
    execution_id = expected_runtime.get("execution_id")
    execution_started_ns = expected_runtime.get("execution_started_ns")
    if (
        not isinstance(execution_id, str)
        or not execution_id
        or receipt.get("execution_id") != execution_id
        or not isinstance(execution_started_ns, int)
        or execution_started_ns < 0
    ):
        raise RuntimeError("invalid_execution_receipt: stale_output execution identity")

    identity_fields = (
        "asset_identity",
        "package_version",
        "revision",
        "license",
        "adapter_version",
    )
    if any(
        not isinstance(receipt.get(field), str)
        or not receipt[field]
        or receipt[field] != expected_runtime.get(field)
        for field in identity_fields
    ):
        raise RuntimeError("invalid_execution_receipt: runtime identity does not match")

    if kind == "deepfilternet":
        model_sha256 = receipt.get("model_sha256")
        if (
            not isinstance(model_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", model_sha256) is None
            or model_sha256 != expected_runtime.get("model_sha256")
        ):
            raise RuntimeError("invalid_execution_receipt: model_sha256 does not match")
        if (
            receipt.get("weights_sha256") is not None
            or expected_runtime.get("weights_sha256") is not None
        ):
            raise RuntimeError(
                "invalid_execution_receipt: weights_sha256 must be null for DeepFilterNet"
            )
    elif kind == "respiro":
        if (
            receipt.get("model_sha256") is not None
            or expected_runtime.get("model_sha256") is not None
        ):
            raise RuntimeError("invalid_execution_receipt: model_sha256 must be null for Respiro")
        weights_sha256 = receipt.get("weights_sha256")
        if (
            not isinstance(weights_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", weights_sha256) is None
            or weights_sha256 != expected_runtime.get("weights_sha256")
        ):
            raise RuntimeError("invalid_execution_receipt: weights_sha256 does not match")
    else:
        raise RuntimeError("invalid_execution_receipt: unsupported model kind")

    adapter_bytes = expected_runtime.get("adapter_bytes")
    if not isinstance(adapter_bytes, bytes) or not adapter_bytes:
        raise RuntimeError("invalid_execution_receipt: trusted adapter bytes are unavailable")
    expected_adapter_sha256 = sha256(adapter_bytes).hexdigest()
    if receipt.get("adapter_sha256") != expected_adapter_sha256:
        raise RuntimeError("invalid_execution_receipt: adapter SHA-256 does not match")

    input_sha256 = _sha256_path(input_path)
    output_sha256 = _sha256_path(output_path)
    if input_sha256 is None or receipt.get("input_sha256") != input_sha256:
        raise RuntimeError("invalid_execution_receipt: input SHA-256 does not match")
    if output_sha256 is None or receipt.get("output_sha256") != output_sha256:
        raise RuntimeError("invalid_execution_receipt: output SHA-256 does not match")
    try:
        output_mtime_ns = output_path.stat().st_mtime_ns
    except OSError as exc:
        raise RuntimeError("invalid_execution_receipt: stale_output is unavailable") from exc
    if output_mtime_ns < execution_started_ns:
        raise RuntimeError("invalid_execution_receipt: stale_output predates this execution")
    if input_sha256 == output_sha256:
        raise RuntimeError("invalid_execution_receipt: no_op_output is not accepted")
    return dict(receipt)


@dataclass
class RuntimeOptions:
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    python_executable: str | None = None
    overwrite: bool = True
    dry_run: bool = False
    env_path: Path | None = None


@dataclass(frozen=True)
class NoiseWindow:
    start_seconds: float
    end_seconds: float


@dataclass
class RespiroDetectionResult:
    windows: list[NoiseWindow]
    mode: str
    assets_present: bool
    attempted: bool
    succeeded: bool
    command: list[str] | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


@dataclass(frozen=True)
class OutputLayout:
    job_name: str
    job_dir: Path
    preprocess_dir: Path
    transcript_dir: Path
    raw_wav: Path
    denoised_wav: Path
    noise_sample_wav: Path
    clean_wav: Path
    transcript_mp3: Path
    report_json: Path
    report_md: Path
    deepfilternet_dir: Path | None


def build_breath_processing_plan(
    preset: dict[str, Any],
    *,
    attenuation_db: float,
    skip_spectramini: bool,
    skip_deepfilternet: bool,
) -> list[dict[str, Any]]:
    stages = preset.get("pipeline", {}).get("stages", [])
    plan: list[dict[str, Any]] = []
    for stage in stages:
        stage_type = stage.get("type")
        if not stage.get("enabled", True):
            continue
        if stage_type == "spectramini" and skip_spectramini:
            continue
        if stage_type == "deepfilternet" and skip_deepfilternet:
            continue
        resolved = dict(stage)
        if stage_type == "respiro":
            resolved["attenuation_db"] = float(attenuation_db)
        plan.append(resolved)
    return plan


def utc_timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def build_output_root(base_dir: str | Path, run_slug: str) -> Path:
    return Path(base_dir) / f"run-{run_slug}"


def _slugify_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    if cleaned and re.search(r"[A-Za-z0-9]", cleaned):
        return cleaned
    digest = sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"media_{digest}"


def get_pipeline_stage(preset: dict[str, Any], stage_type: str) -> dict[str, Any]:
    for stage in preset.get("pipeline", {}).get("stages", []):
        if stage.get("type") == stage_type:
            return stage
    raise KeyError(stage_type)


def attenuation_db_to_gain(attenuation_db: float) -> float:
    return 10 ** (-float(attenuation_db) / 20.0)


def build_respiro_detect_command(
    *,
    audio_path: Path,
    python_executable: str,
    repo_path: Path,
    weights_path: Path,
    threshold: float,
    min_length_ms: int,
    verified_runtime: dict[str, Any] | None,
) -> list[str]:
    if not _respiro_runtime_matches(
        verified_runtime,
        repo_path=repo_path,
        weights_path=weights_path,
        python_executable=python_executable,
    ):
        raise RuntimeError(
            "unverified_respiro_runtime: Respiro-en requires a verified repository revision, "
            "weights license and SHA-256, trusted adapter, and matching self-check identity."
        )
    assert verified_runtime is not None
    return [
        python_executable,
        str(verified_runtime["adapter_path"]),
        "--repo",
        str(repo_path),
        "--weights",
        str(weights_path),
        "--audio",
        str(audio_path),
        "--threshold",
        str(threshold),
        "--min-length-ms",
        str(min_length_ms),
        "--json",
    ]


def _respiro_runtime_matches(
    verified_runtime: dict[str, Any] | None,
    *,
    repo_path: Path,
    weights_path: Path,
    python_executable: str,
) -> bool:
    if not _verified_runtime_receipt_matches(
        verified_runtime,
        kind="respiro",
        python_executable=python_executable,
    ):
        return False
    assert verified_runtime is not None
    return (
        Path(str(verified_runtime["repo_path"])) == repo_path
        and Path(str(verified_runtime["weights_path"])) == weights_path
        and str(verified_runtime["python_executable"]) == python_executable
    )


def resolve_respiro_runtime(
    *,
    respiro_repo: str | None,
    respiro_weights: str | None,
    env_values: dict[str, str] | None = None,
    repo_root: str | Path = PROJECT_ROOT,
) -> dict[str, Path | None]:
    values = env_values or {}
    repo_value = respiro_repo or values.get("AUDIO_SOUND_RESPIRO_REPO")
    weights_value = respiro_weights or values.get("AUDIO_SOUND_RESPIRO_WEIGHTS")

    def resolve_path(value: str | None) -> Path | None:
        if not value:
            return None
        path = _resolve_runtime_path(value, root=Path(repo_root))
        if path is None:
            raise ValueError("relative Respiro runtime paths must remain under the repository root")
        return path

    return {
        "repo_path": resolve_path(repo_value),
        "weights_path": resolve_path(weights_value),
    }


def verified_respiro_runtime(
    runtime_report: dict[str, Any],
    *,
    python_executable: str,
    repo_path: Path,
    weights_path: Path,
    repo_root: str | Path = PROJECT_ROOT,
    env_path: str | Path | None = None,
) -> dict[str, Any] | None:
    component = runtime_report.get("respiro_en")
    if not isinstance(component, dict) or not (
        component.get("asset_verification_ok") is True or component.get("ok") is True
    ):
        return None
    doctor_identity = component.get("identity")
    if not isinstance(doctor_identity, str) or not doctor_identity:
        return None
    python_component = runtime_report.get("python")
    if not isinstance(python_component, dict) or not (
        python_component.get("ok") is True
        and str(python_component.get("path") or "") == python_executable
    ):
        return None

    from .bootstrap import (
        _load_runtime_asset_section,
        _read_trusted_repo_file,
        _resolve_runtime_path,
        _trusted_adapter_relative_path,
        _verify_respiro_runtime,
    )

    root = Path(repo_root)
    verified = _verify_respiro_runtime(
        repo_root=root,
        python_executable=python_executable,
        env_path=env_path,
    )
    if verified.get("ok") is not True or verified.get("identity") != doctor_identity:
        return None
    section, _error = _load_runtime_asset_section(
        root=root,
        env_path=env_path,
        asset_name="respiro_en",
    )
    if section is None:
        return None
    env_values = section.get("_env")
    if not isinstance(env_values, dict):
        return None
    configured_repo = _resolve_runtime_path(
        env_values.get("AUDIO_SOUND_RESPIRO_REPO"),
        root=root,
    )
    configured_weights = _resolve_runtime_path(
        env_values.get("AUDIO_SOUND_RESPIRO_WEIGHTS"),
        root=root,
    )
    if configured_repo != repo_path or configured_weights != weights_path:
        return None
    adapter_info = section.get("adapter") if isinstance(section.get("adapter"), dict) else {}
    adapter_relative = _trusted_adapter_relative_path(
        section.get("adapter_path") or adapter_info.get("path")
    )
    if adapter_relative is None:
        return None
    adapter_bytes, _trust_error = _read_trusted_repo_file(root, adapter_relative)
    if adapter_bytes is None:
        return None
    return _mint_verified_runtime(
        {
            "identity": doctor_identity,
            "asset_identity": str(verified["identity"]),
            "python_executable": python_executable,
            "repo_path": configured_repo,
            "weights_path": configured_weights,
            "adapter_path": root / adapter_relative,
            "adapter_bytes": adapter_bytes,
            "repo_root": root,
            "verification_env_path": str(env_path) if env_path is not None else "",
        },
        kind="respiro",
    )


def apply_spectramini_style_cleanup_to_samples(
    samples: array,
    *,
    breath_windows: list[NoiseWindow],
    sample_rate: int,
    attenuation_db: float,
    mouth_declick_sensitivity: float,
    fade_ms: float,
) -> array:
    cleaned = array("h", samples)
    duck_samples_for_windows(
        cleaned,
        sample_rate=sample_rate,
        windows=breath_windows,
        floor_gain=attenuation_db_to_gain(attenuation_db),
        fade_ms=fade_ms,
    )

    if len(cleaned) < 3:
        return cleaned

    threshold_scale = max(1.0, 6.0 - (float(mouth_declick_sensitivity) * 4.0))
    diffs = [
        abs(int(cleaned[index + 1]) - int(cleaned[index])) for index in range(len(cleaned) - 1)
    ]
    mean_diff = sum(diffs) / float(len(diffs))
    variance = sum((diff - mean_diff) ** 2 for diff in diffs) / float(len(diffs))
    std_diff = variance**0.5
    click_threshold = mean_diff + (threshold_scale * std_diff)

    for _ in range(2):
        for index in range(1, len(cleaned) - 1):
            left_delta = abs(int(cleaned[index]) - int(cleaned[index - 1]))
            right_delta = abs(int(cleaned[index + 1]) - int(cleaned[index]))
            sample_peak = abs(int(cleaned[index]))
            polarity_flip = (
                int(cleaned[index - 1]) * int(cleaned[index]) < 0
                or int(cleaned[index]) * int(cleaned[index + 1]) < 0
            )
            if (
                max(left_delta, right_delta) > click_threshold
                or sample_peak > click_threshold
                or polarity_flip
            ):
                cleaned[index] = int((int(cleaned[index - 1]) + int(cleaned[index + 1])) / 2)

    return cleaned


def run_spectramini_style_smoke() -> dict[str, Any]:
    checks = {name: False for name in SPECTRAMINI_STYLE_SMOKE_REQUIRED_CHECKS}
    metrics: dict[str, Any] = {}
    error = ""
    try:
        sample_rate = 16000
        sample_count = 1024
        breath_start = 192
        breath_end = 384
        click_index = 640
        source = array(
            "h",
            (
                int(1200.0 * math.sin(2.0 * math.pi * 220.0 * index / sample_rate))
                for index in range(sample_count)
            ),
        )
        for index in range(breath_start, breath_end):
            source[index] += int(600.0 * math.sin(2.0 * math.pi * 1700.0 * index / sample_rate))
        source[click_index] = 29000
        breath_windows = [
            NoiseWindow(
                start_seconds=breath_start / sample_rate,
                end_seconds=breath_end / sample_rate,
            )
        ]
        cleanup_options = {
            "breath_windows": breath_windows,
            "sample_rate": sample_rate,
            "attenuation_db": 12.0,
            "mouth_declick_sensitivity": 0.55,
            "fade_ms": 0.0,
        }
        cleaned = apply_spectramini_style_cleanup_to_samples(
            source,
            **cleanup_options,
        )
        repeated = apply_spectramini_style_cleanup_to_samples(
            source,
            **cleanup_options,
        )

        import io

        import librosa
        import numpy
        import scipy.signal
        import soundfile

        before_breath_rms = math.sqrt(
            sum(int(value) ** 2 for value in source[breath_start:breath_end])
            / float(breath_end - breath_start)
        )
        after_breath_rms = math.sqrt(
            sum(int(value) ** 2 for value in cleaned[breath_start:breath_end])
            / float(breath_end - breath_start)
        )
        before_click_excursion = abs(
            int(source[click_index])
            - (int(source[click_index - 1]) + int(source[click_index + 1])) / 2.0
        )
        after_click_excursion = abs(
            int(cleaned[click_index])
            - (int(cleaned[click_index - 1]) + int(cleaned[click_index + 1])) / 2.0
        )

        normalized = numpy.asarray(cleaned, dtype=numpy.float32) / numpy.float32(32768.0)
        feature_rms = librosa.feature.rms(
            y=normalized,
            frame_length=128,
            hop_length=64,
            center=False,
        )
        median_feature = scipy.signal.medfilt(normalized, kernel_size=3)
        wav_buffer = io.BytesIO()
        soundfile.write(
            wav_buffer,
            numpy.asarray(cleaned, dtype=numpy.int16),
            sample_rate,
            format="WAV",
            subtype="PCM_16",
        )
        wav_buffer.seek(0)
        roundtrip, roundtrip_rate = soundfile.read(
            wav_buffer,
            dtype="int16",
            always_2d=False,
        )

        evaluated_checks = {
            "int16_output": isinstance(cleaned, array) and cleaned.typecode == "h",
            "shape_preserved": len(cleaned) == len(source),
            "finite_output": all(math.isfinite(value) for value in cleaned),
            "breath_rms_reduced": (
                before_breath_rms > 0.0 and after_breath_rms < before_breath_rms * 0.5
            ),
            "click_peak_reduced": (
                before_click_excursion > 0.0
                and after_click_excursion < before_click_excursion * 0.5
            ),
            "memory_roundtrip_ok": (
                roundtrip_rate == sample_rate
                and roundtrip.shape == (len(cleaned),)
                and numpy.array_equal(
                    roundtrip,
                    numpy.asarray(cleaned, dtype=numpy.int16),
                )
            ),
            "feature_finite": (
                feature_rms.size > 0
                and numpy.isfinite(feature_rms).all()
                and numpy.isfinite(median_feature).all()
            ),
            "deterministic": cleaned == repeated,
        }
        checks.update({name: bool(value) for name, value in evaluated_checks.items()})
        metrics = {
            "sample_rate": sample_rate,
            "sample_count": sample_count,
            "breath_rms_ratio": round(after_breath_rms / before_breath_rms, 6),
            "click_excursion_ratio": round(
                after_click_excursion / before_click_excursion,
                6,
            ),
            "feature_frame_count": int(feature_rms.size),
        }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    smoke_status = "passed" if all(checks.values()) and not error else "failed"
    return {
        "runtime_ok": smoke_status == "passed",
        "smoke_status": smoke_status,
        "algorithm_identity": SPECTRAMINI_STYLE_SMOKE_ALGORITHM,
        "checks": checks,
        "metrics": metrics,
        "error": error,
    }


def run_respiro_or_fallback_detection(
    *,
    audio_path: Path,
    ffmpeg_bin: str,
    respiro_repo: Path | None,
    respiro_weights: Path | None,
    python_executable: str,
    threshold: float,
    min_length_ms: int,
    fallback_config: dict[str, Any],
    verified_runtime: dict[str, Any] | None = None,
) -> RespiroDetectionResult:
    def build_fallback_result(
        *,
        command: list[str] | None,
        returncode: int | None,
        stdout: str,
        stderr: str,
        error: str | None,
    ) -> RespiroDetectionResult:
        fallback_windows = (
            detect_breath_onset_windows(
                audio_path,
                ffmpeg_bin=ffmpeg_bin,
                config=fallback_config,
            )
            if audio_path.exists()
            else []
        )
        return RespiroDetectionResult(
            windows=fallback_windows,
            mode="fallback",
            assets_present=True,
            attempted=True,
            succeeded=False,
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            error=error,
        )

    assets_present = bool(
        respiro_repo and respiro_weights and respiro_repo.exists() and respiro_weights.exists()
    )
    if assets_present and respiro_repo and respiro_weights:
        if not _respiro_runtime_matches(
            verified_runtime,
            repo_path=respiro_repo,
            weights_path=respiro_weights,
            python_executable=python_executable,
        ):
            raise RuntimeError(
                "unverified_respiro_runtime: Respiro-en assets exist but are not bound to a "
                "verified revision, license, SHA-256, adapter, and self-check identity."
            )
        duration_seconds = 0.0
        if audio_path.exists():
            try:
                duration_seconds = _read_wave_duration_seconds(audio_path)
            except (wave.Error, EOFError, FileNotFoundError):
                duration_seconds = 0.0
        segment_length_seconds = 120.0
        segment_overlap_seconds = 1.0
        segment_mode = duration_seconds > segment_length_seconds

        if not segment_mode:
            command = build_respiro_detect_command(
                audio_path=audio_path,
                python_executable=python_executable,
                repo_path=respiro_repo,
                weights_path=respiro_weights,
                threshold=threshold,
                min_length_ms=min_length_ms,
                verified_runtime=verified_runtime,
            )
            assert verified_runtime is not None
            completed = _run_verified_adapter_command(
                command,
                verified_runtime=verified_runtime,
            )
            if completed.returncode == 0:
                try:
                    payload = json.loads(completed.stdout.strip() or "{}")
                    intervals = payload.get("intervals", [])
                    return RespiroDetectionResult(
                        windows=[
                            NoiseWindow(
                                start_seconds=float(item["start_seconds"]),
                                end_seconds=float(item["end_seconds"]),
                            )
                            for item in intervals
                        ],
                        mode="respiro",
                        assets_present=True,
                        attempted=True,
                        succeeded=True,
                        command=command,
                        returncode=completed.returncode,
                        stdout=completed.stdout.strip(),
                        stderr=completed.stderr.strip(),
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    error = f"Failed to parse Respiro-en output: {exc}"
            else:
                error = (
                    completed.stderr.strip()
                    or completed.stdout.strip()
                    or "Respiro-en command failed"
                )
            return build_fallback_result(
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout.strip(),
                stderr=completed.stderr.strip(),
                error=error,
            )

        ffmpeg = ensure_tool(ffmpeg_bin)
        segment_windows: list[NoiseWindow] = []
        segment_commands: list[list[str]] = []
        segment_stdout: list[str] = []
        segment_stderr: list[str] = []
        segment_count = 0

        with tempfile.TemporaryDirectory(prefix="respiro-segments-") as tmp_dir:
            temp_root = Path(tmp_dir)
            start_seconds = 0.0
            while start_seconds < duration_seconds:
                segment_start = start_seconds
                segment_duration = min(segment_length_seconds, duration_seconds - segment_start)
                segment_wav = temp_root / f"segment_{segment_count:04d}.wav"
                extract_command = [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-nostdin",
                    "-ss",
                    f"{segment_start:.3f}",
                    "-t",
                    f"{segment_duration:.3f}",
                    "-i",
                    str(audio_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(segment_wav),
                ]
                extract_completed = run_command(extract_command)
                segment_commands.append(extract_command)
                if extract_completed.returncode != 0:
                    error = (
                        extract_completed.stderr.strip()
                        or extract_completed.stdout.strip()
                        or "Failed to extract Respiro-en segment"
                    )
                    return build_fallback_result(
                        command=extract_command,
                        returncode=extract_completed.returncode,
                        stdout=extract_completed.stdout.strip(),
                        stderr=extract_completed.stderr.strip(),
                        error=error,
                    )

                detect_command = build_respiro_detect_command(
                    audio_path=segment_wav,
                    python_executable=python_executable,
                    repo_path=respiro_repo,
                    weights_path=respiro_weights,
                    threshold=threshold,
                    min_length_ms=min_length_ms,
                    verified_runtime=verified_runtime,
                )
                assert verified_runtime is not None
                detect_completed = _run_verified_adapter_command(
                    detect_command,
                    verified_runtime=verified_runtime,
                )
                segment_commands.append(detect_command)
                segment_stdout.append(detect_completed.stdout.strip())
                segment_stderr.append(detect_completed.stderr.strip())
                if detect_completed.returncode != 0:
                    error = (
                        detect_completed.stderr.strip()
                        or detect_completed.stdout.strip()
                        or "Respiro-en segment command failed"
                    )
                    return build_fallback_result(
                        command=detect_command,
                        returncode=detect_completed.returncode,
                        stdout=detect_completed.stdout.strip(),
                        stderr=detect_completed.stderr.strip(),
                        error=error,
                    )
                try:
                    payload = json.loads(detect_completed.stdout.strip() or "{}")
                    intervals = payload.get("intervals", [])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    return build_fallback_result(
                        command=detect_command,
                        returncode=detect_completed.returncode,
                        stdout=detect_completed.stdout.strip(),
                        stderr=detect_completed.stderr.strip(),
                        error=f"Failed to parse Respiro-en segment output: {exc}",
                    )

                for item in intervals:
                    adjusted_start = segment_start + float(item["start_seconds"])
                    adjusted_end = segment_start + float(item["end_seconds"])
                    segment_windows.append(
                        NoiseWindow(
                            start_seconds=adjusted_start,
                            end_seconds=adjusted_end,
                        )
                    )

                segment_count += 1
                if segment_start + segment_duration >= duration_seconds:
                    break
                start_seconds += max(1.0, segment_length_seconds - segment_overlap_seconds)

        merged_windows = merge_noise_windows(segment_windows, max_gap_seconds=0.05)
        return RespiroDetectionResult(
            windows=merged_windows,
            mode="respiro",
            assets_present=True,
            attempted=True,
            succeeded=True,
            command=segment_commands[-1] if segment_commands else None,
            returncode=0,
            stdout="\n".join(line for line in segment_stdout if line),
            stderr="\n".join(line for line in segment_stderr if line),
            error=None,
        )
    fallback_windows = (
        detect_breath_onset_windows(
            audio_path,
            ffmpeg_bin=ffmpeg_bin,
            config=fallback_config,
        )
        if audio_path.exists()
        else []
    )
    return RespiroDetectionResult(
        windows=fallback_windows,
        mode="fallback",
        assets_present=False,
        attempted=False,
        succeeded=False,
        error=(
            "Respiro-en assets not configured or missing"
            if (respiro_repo or respiro_weights)
            else None
        ),
    )


def discover_media_files(input_path: Path, recursive: bool = False) -> list[Path]:
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    if input_path.is_file():
        if input_path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
            raise ValueError(f"Unsupported media file: {input_path}")
        return [input_path]

    pattern = "**/*" if recursive else "*"
    files = [
        path
        for path in input_path.glob(pattern)
        if path.is_file() and path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS
    ]
    return sorted(files)


def ensure_tool(tool_name: str) -> str:
    if any(separator in tool_name for separator in ("/", "\\")):
        path = Path(tool_name)
        if not path.exists():
            raise RuntimeError(f"Required tool not found: {tool_name}")
        return str(path)

    resolved = shutil.which(tool_name)
    if resolved is None:
        raise RuntimeError(f"Required tool not found on PATH: {tool_name}")
    return resolved


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _run_verified_adapter_command(
    command: list[str],
    *,
    verified_runtime: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    adapter_path = verified_runtime.get("adapter_path")
    adapter_bytes = verified_runtime.get("adapter_bytes")
    repo_root = verified_runtime.get("repo_root")
    kind = None
    for candidate in ("deepfilternet", "respiro"):
        if _verified_runtime_receipt_matches(
            verified_runtime,
            kind=candidate,
            python_executable=command[0] if command else "",
        ):
            kind = candidate
            break
    if (
        len(command) < 2
        or kind is None
        or not adapter_path
        or not isinstance(adapter_bytes, bytes)
        or not adapter_bytes
        or not repo_root
        or Path(command[1]) != Path(str(adapter_path))
        or str(verified_runtime.get("python_executable") or "") != command[0]
    ):
        raise RuntimeError(
            "unverified_adapter_runtime: model execution requires the exact verified adapter bytes."
        )

    _raise_external_model_execution_unavailable(kind)


def _reverify_runtime_for_execution(
    runtime: dict[str, Any],
    *,
    kind: str,
) -> bool:
    root = Path(str(runtime["repo_root"]))
    env_value = str(runtime.get("verification_env_path") or "")
    env_path: str | Path | None = env_value or None
    python_executable = str(runtime["python_executable"])
    if kind == "deepfilternet":
        from .bootstrap import (
            _inspect_python_runtime,
            _probe_deepfilternet_adapter,
            _verify_deepfilternet_model,
        )

        if (
            str(runtime.get("package_version") or "") != DEEPFILTERNET_LOCKED_VERSION
            or _locked_deepfilternet_version(root) != DEEPFILTERNET_LOCKED_VERSION
        ):
            return False
        python_info = _inspect_python_runtime(python_executable)
        if not (
            python_info.get("ok") is True
            and python_info.get("deepfilternet_ok") is True
            and python_info.get("deepfilternet_identity")
            == f"deepfilternet@{DEEPFILTERNET_LOCKED_VERSION}"
        ):
            return False

        current = _verify_deepfilternet_model(repo_root=root, env_path=env_path)
        if current.get("ok") is not True or not (
            str(current.get("identity") or "") == str(runtime["asset_identity"])
            and Path(str(current.get("model_path") or "")) == Path(str(runtime["model_path"]))
            and str(current.get("model_sha256") or "") == str(runtime["model_sha256"])
            and Path(str(current.get("adapter_path") or "")) == Path(str(runtime["adapter_path"]))
            and str(current.get("adapter_version") or "") == str(runtime["adapter_version"])
            and current.get("_adapter_bytes") == runtime["adapter_bytes"]
        ):
            return False
        probe = _probe_deepfilternet_adapter(python_executable, current)
        return probe.get("ok") is True

    if kind == "respiro":
        from .bootstrap import (
            _load_runtime_asset_section,
            _read_trusted_repo_file,
            _resolve_runtime_path,
            _trusted_adapter_relative_path,
            _verify_respiro_runtime,
        )

        current = _verify_respiro_runtime(
            repo_root=root,
            python_executable=python_executable,
            env_path=env_path,
        )
        if current.get("ok") is not True or str(current.get("identity") or "") != str(
            runtime["asset_identity"]
        ):
            return False
        section, _error = _load_runtime_asset_section(
            root=root,
            env_path=env_path,
            asset_name="respiro_en",
        )
        if section is None or not isinstance(section.get("_env"), dict):
            return False
        env_values = section["_env"]
        configured_repo = _resolve_runtime_path(
            env_values.get("AUDIO_SOUND_RESPIRO_REPO"),
            root=root,
        )
        configured_weights = _resolve_runtime_path(
            env_values.get("AUDIO_SOUND_RESPIRO_WEIGHTS"),
            root=root,
        )
        adapter_info = section.get("adapter") if isinstance(section.get("adapter"), dict) else {}
        adapter_relative = _trusted_adapter_relative_path(
            section.get("adapter_path") or adapter_info.get("path")
        )
        if adapter_relative is None:
            return False
        adapter_bytes, _trust_error = _read_trusted_repo_file(root, adapter_relative)
        return bool(
            configured_repo == Path(str(runtime["repo_path"]))
            and configured_weights == Path(str(runtime["weights_path"]))
            and root / adapter_relative == Path(str(runtime["adapter_path"]))
            and adapter_bytes == runtime["adapter_bytes"]
        )
    return False


def ffprobe_media(path: Path, ffprobe_bin: str) -> dict[str, Any]:
    ffprobe = ensure_tool(ffprobe_bin)
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_name,codec_type,sample_rate,channels,bit_rate:format=duration,size,bit_rate",
        "-of",
        "json",
        str(path),
    ]
    completed = run_command(cmd)
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {completed.stderr.strip()}")

    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams", [])
    audio_stream = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        streams[0] if streams else {},
    )
    format_block = payload.get("format", {})

    def parse_number(value: Any) -> float | int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return value
        try:
            if "." in str(value):
                return float(value)
            return int(value)
        except ValueError:
            return None

    return {
        "path": str(path),
        "codec": audio_stream.get("codec_name"),
        "sample_rate": parse_number(audio_stream.get("sample_rate")),
        "channels": parse_number(audio_stream.get("channels")),
        "stream_bit_rate": parse_number(audio_stream.get("bit_rate")),
        "format_bit_rate": parse_number(format_block.get("bit_rate")),
        "duration_seconds": parse_number(format_block.get("duration")),
        "size_bytes": parse_number(format_block.get("size")),
    }


def build_output_layout(
    *,
    input_path: Path,
    output_root: Path,
    run_slug: str,
    skip_deepfilternet: bool = False,
) -> OutputLayout:
    file_base_name = input_path.stem
    job_name = f"{run_slug}_{file_base_name}"
    job_dir = output_root / job_name
    preprocess_dir = job_dir / "audio_preprocess"
    transcript_dir = job_dir / "transcript_ready"
    return OutputLayout(
        job_name=job_name,
        job_dir=job_dir,
        preprocess_dir=preprocess_dir,
        transcript_dir=transcript_dir,
        raw_wav=preprocess_dir / f"{file_base_name}_raw.wav",
        denoised_wav=preprocess_dir
        / f"{file_base_name}_{'local_pre_master' if skip_deepfilternet else 'df'}.wav",
        noise_sample_wav=preprocess_dir / f"{file_base_name}_noise_sample.wav",
        clean_wav=preprocess_dir / f"{file_base_name}_clean.wav",
        transcript_mp3=transcript_dir / f"{file_base_name}_transcript.mp3",
        report_json=preprocess_dir / "audio_process_report.json",
        report_md=preprocess_dir / "audio_process_report.md",
        deepfilternet_dir=(None if skip_deepfilternet else preprocess_dir / "deepfilternet_out"),
    )


def build_ffmpeg_extract_command(
    *, input_path: Path, raw_wav: Path, preset: dict[str, Any], ffmpeg_bin: str
) -> list[str]:
    extract = preset["extract"]
    return [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        str(extract["channels"]),
        "-ar",
        str(extract["sample_rate"]),
        "-c:a",
        extract["pcm_codec"],
        str(raw_wav),
    ]


def verified_deepfilternet_runtime(
    runtime_report: dict[str, Any],
    *,
    python_executable: str,
    repo_root: str | Path = PROJECT_ROOT,
    env_path: str | Path | None = None,
) -> dict[str, Any] | None:
    component = runtime_report.get("deepfilternet")
    if not isinstance(component, dict) or not (
        (component.get("asset_verification_ok") is True or component.get("ok") is True)
        and all(component.get(key) is True for key in ("module_ok", "model_ok", "adapter_ok"))
    ):
        return None
    doctor_identity = component.get("identity")
    if not isinstance(doctor_identity, str) or not doctor_identity:
        return None
    package_identity = doctor_identity.split(";", 1)[0]
    if package_identity != f"deepfilternet@{DEEPFILTERNET_LOCKED_VERSION}":
        return None
    if _locked_deepfilternet_version(Path(repo_root)) != DEEPFILTERNET_LOCKED_VERSION:
        return None
    python_component = runtime_report.get("python")
    if not isinstance(python_component, dict) or not (
        python_component.get("ok") is True
        and str(python_component.get("path") or "") == python_executable
    ):
        return None

    from .bootstrap import _verify_deepfilternet_model

    model_runtime = _verify_deepfilternet_model(repo_root=repo_root, env_path=env_path)
    required = (
        "identity",
        "model_path",
        "model_sha256",
        "adapter_path",
        "adapter_version",
        "_adapter_bytes",
        "_repo_root",
    )
    if model_runtime.get("ok") is not True or not all(model_runtime.get(key) for key in required):
        return None
    model_identity = str(model_runtime["identity"])
    if model_identity not in doctor_identity:
        return None
    return _mint_verified_runtime(
        {
            "identity": doctor_identity,
            "asset_identity": model_identity,
            "python_executable": python_executable,
            "model_path": Path(str(model_runtime["model_path"])),
            "model_sha256": str(model_runtime["model_sha256"]),
            "adapter_path": Path(str(model_runtime["adapter_path"])),
            "adapter_version": str(model_runtime["adapter_version"]),
            "package_version": DEEPFILTERNET_LOCKED_VERSION,
            "adapter_bytes": model_runtime["_adapter_bytes"],
            "repo_root": Path(str(model_runtime["_repo_root"])),
            "verification_env_path": str(env_path) if env_path is not None else "",
        },
        kind="deepfilternet",
    )


def build_deepfilternet_command(
    *,
    raw_wav: Path,
    output_dir: Path,
    preset: dict[str, Any],
    python_executable: str,
    verified_runtime: dict[str, Any] | None,
) -> list[str]:
    if not _deepfilternet_runtime_ready(
        verified_runtime,
        python_executable=python_executable,
    ):
        raise RuntimeError(
            "unverified_deepfilternet_runtime: DeepFilterNet requires a verified local "
            "model and adapter; rerun with --skip-deepfilternet to use only local "
            "deterministic stages."
        )
    config = get_pipeline_stage(preset, "deepfilternet")
    command = [
        python_executable,
        str(verified_runtime["adapter_path"]),
        "--model",
        str(verified_runtime["model_path"]),
        "--output-dir",
        str(output_dir),
    ]
    if config.get("post_filter", False):
        command.append("--post-filter")
    command.append(str(raw_wav))
    return command


def _deepfilternet_runtime_ready(
    verified_runtime: dict[str, Any] | None,
    *,
    python_executable: str,
) -> bool:
    return bool(
        _verified_runtime_receipt_matches(
            verified_runtime,
            kind="deepfilternet",
            python_executable=python_executable,
        )
    )


def _locked_deepfilternet_version(repo_root: Path) -> str | None:
    from .bootstrap import _read_trusted_repo_file

    lock_bytes, _error = _read_trusted_repo_file(repo_root, Path("requirements-audio.lock"))
    if lock_bytes is None:
        return None
    try:
        lock_text = lock_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    matches = [
        line.split("==", 1)[1]
        for line in (raw_line.strip() for raw_line in lock_text.splitlines())
        if re.fullmatch(r"deepfilternet==[A-Za-z0-9][A-Za-z0-9._+-]*", line)
    ]
    return matches[0] if len(matches) == 1 else None


def parse_noise_window(raw_value: str) -> NoiseWindow:
    parts = raw_value.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid noise window: {raw_value}")

    try:
        start_seconds = float(parts[0])
        end_seconds = float(parts[1])
    except ValueError as exc:
        raise ValueError(f"Invalid noise window: {raw_value}") from exc

    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError(f"Invalid noise window: {raw_value}")

    return NoiseWindow(start_seconds=start_seconds, end_seconds=end_seconds)


def _format_filter_float(value: float) -> str:
    return format(value, ".6f").rstrip("0").rstrip(".")


def build_ffmpeg_noise_sample_command(
    *,
    source_wav: Path,
    noise_sample_wav: Path,
    noise_windows: list[NoiseWindow],
    preset: dict[str, Any],
    ffmpeg_bin: str,
) -> list[str]:
    if not noise_windows:
        raise ValueError("At least one noise window is required to build a noise sample command")

    segments: list[str] = []
    concat_inputs: list[str] = []
    for index, window in enumerate(noise_windows):
        label = f"s{index}"
        concat_inputs.append(f"[{label}]")
        segments.append(
            "[0:a]atrim="
            f"start={_format_filter_float(window.start_seconds)}:"
            f"end={_format_filter_float(window.end_seconds)},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )

    segments.append(f"{''.join(concat_inputs)}concat=n={len(noise_windows)}:v=0:a=1[outa]")
    filter_complex = ";".join(segments)
    return [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-i",
        str(source_wav),
        "-filter_complex",
        filter_complex,
        "-map",
        "[outa]",
        "-ar",
        str(preset["extract"]["sample_rate"]),
        "-ac",
        str(preset["extract"]["channels"]),
        "-c:a",
        preset["extract"]["pcm_codec"],
        str(noise_sample_wav),
    ]


def build_mastering_filter_chain(
    preset: dict[str, Any],
    *,
    for_noise_print: bool = False,
    include_secondary_denoise: bool = True,
    include_loudnorm: bool = True,
    include_tonal_shaping: bool = True,
    include_declick: bool = True,
    include_nonlinear_denoise: bool = True,
    include_deesser: bool = True,
    include_gate: bool = True,
    include_compressor: bool = True,
    include_speech_norm: bool = True,
) -> str:
    filters = preset["filters"]
    parts: list[str] = []

    highpass_hz = filters.get("highpass_hz")
    if include_tonal_shaping and highpass_hz:
        parts.append(f"highpass=f={highpass_hz}")

    lowpass_hz = filters.get("lowpass_hz")
    if include_tonal_shaping and lowpass_hz:
        parts.append(f"lowpass=f={lowpass_hz}")

    declick = filters.get("declick", {})
    if include_declick and declick.get("enabled"):
        parts.append(
            "adeclick="
            f"w={declick['window']}:o={declick['overlap']}:a={declick['arorder']}:t={declick['threshold']}:b={declick['burst']}"
        )

    secondary_denoise = filters.get("secondary_denoise", {})
    if include_secondary_denoise and secondary_denoise.get("enabled"):
        part = f"afftdn=nr={secondary_denoise['nr']}:nf={secondary_denoise['nf']}"
        residual_floor = secondary_denoise.get("residual_floor")
        if residual_floor is not None:
            part += f":rf={residual_floor}"
        adaptivity = secondary_denoise.get("adaptivity")
        if adaptivity is not None:
            part += f":ad={adaptivity}"
        floor_offset = secondary_denoise.get("floor_offset")
        if floor_offset is not None:
            part += f":fo={floor_offset}"
        gain_smooth = secondary_denoise.get("gain_smooth")
        if gain_smooth is not None:
            part += f":gs={gain_smooth}"
        noise_link = secondary_denoise.get("noise_link")
        if noise_link:
            part += f":nl={noise_link}"
        if for_noise_print:
            part += ":sn=start"
        elif secondary_denoise.get("tracking"):
            part += ":tn=1"
        parts.append(part)

    nonlinear_denoise = filters.get("nonlinear_denoise", {})
    if include_nonlinear_denoise and nonlinear_denoise.get("enabled"):
        parts.append(
            "anlmdn="
            f"s={nonlinear_denoise['strength']}:p={nonlinear_denoise['patch']}:r={nonlinear_denoise['research']}:m={nonlinear_denoise['smooth']}"
        )

    deesser = filters.get("deesser", {})
    if include_deesser and deesser.get("enabled"):
        parts.append(
            "deesser="
            f"i={deesser['intensity']}:m={deesser['max_deessing']}:f={deesser['frequency']}"
        )

    gate = filters.get("gate", {})
    if include_gate and gate.get("enabled"):
        gate_parts = [
            "agate="
            f"threshold={gate['threshold']}:ratio={gate['ratio']}:"
            f"attack={gate['attack_ms']}:release={gate['release_ms']}"
        ]
        makeup_db = gate.get("makeup_db")
        if makeup_db is not None and float(makeup_db) >= 1:
            gate_parts.append(f":makeup={makeup_db}")
        parts.append("".join(gate_parts))

    compressor = filters.get("compressor", {})
    if include_compressor and compressor.get("enabled"):
        compressor_part = (
            "acompressor="
            f"threshold={compressor['threshold_db']}dB:ratio={compressor['ratio']}:"
            f"attack={compressor['attack_ms']}:release={compressor['release_ms']}"
        )
        makeup_db = compressor.get("makeup_db")
        if makeup_db is not None and float(makeup_db) >= 1:
            compressor_part += f":makeup={makeup_db}"
        parts.append(compressor_part)

    speech_norm = filters.get("speech_norm", {})
    if include_speech_norm and speech_norm.get("enabled"):
        parts.append(
            "speechnorm="
            f"e={speech_norm['expansion']}:r={speech_norm['raise']}:f={speech_norm['fall']}:t={speech_norm['threshold']}"
        )

    if include_loudnorm:
        loudnorm = filters["loudnorm"]
        parts.append(
            "loudnorm="
            f"I={loudnorm['target_i']}:LRA={loudnorm['target_lra']}:TP={loudnorm['target_tp']}:print_format=json"
        )
    return ",".join(parts)


def build_breath_ducking_filter_graph(
    *,
    input_label: str,
    output_label: str,
    preset: dict[str, Any],
    include_loudnorm: bool = True,
) -> str:
    filters = preset["filters"]
    breath_ducking = filters.get("breath_ducking", {})

    pre_duck_chain = build_mastering_filter_chain(
        preset,
        include_secondary_denoise=False,
        include_loudnorm=False,
        include_deesser=False,
        include_gate=False,
        include_compressor=False,
        include_speech_norm=False,
    )
    post_duck_chain = build_mastering_filter_chain(
        preset,
        include_secondary_denoise=False,
        include_tonal_shaping=False,
        include_declick=False,
        include_nonlinear_denoise=False,
        include_gate=False,
        include_loudnorm=include_loudnorm,
        include_speech_norm=False,
    )

    if not breath_ducking.get("enabled"):
        if pre_duck_chain and post_duck_chain:
            return f"[{input_label}]{pre_duck_chain},{post_duck_chain}[{output_label}]"
        if pre_duck_chain:
            return f"[{input_label}]{pre_duck_chain}[{output_label}]"
        if post_duck_chain:
            return f"[{input_label}]{post_duck_chain}[{output_label}]"
        return f"[{input_label}]anull[{output_label}]"

    detector_chain = ",".join(
        [
            f"highpass=f={breath_ducking['detector_highpass_hz']}",
            f"lowpass=f={breath_ducking['detector_lowpass_hz']}",
            "acompressor="
            f"threshold={breath_ducking['detector_threshold_db']}dB:"
            f"ratio={breath_ducking['detector_ratio']}:"
            f"attack={breath_ducking['detector_attack_ms']}:"
            f"release={breath_ducking['detector_release_ms']}:"
            f"makeup={breath_ducking['detector_makeup_db']}",
        ]
    )
    gate_filter = (
        "sidechaingate="
        f"threshold={breath_ducking['threshold']}:"
        f"ratio={breath_ducking['ratio']}:"
        f"attack={breath_ducking['attack_ms']}:"
        f"release={breath_ducking['release_ms']}:"
        f"range={breath_ducking['range']}:"
        f"detection={breath_ducking['detection']}:"
        f"link={breath_ducking['link']}:"
        f"level_sc={breath_ducking['level_sc']}"
    )

    source_label = input_label
    graph_parts: list[str] = []
    if pre_duck_chain:
        graph_parts.append(f"[{input_label}]{pre_duck_chain}[duck_pre]")
        source_label = "duck_pre"

    graph_parts.append(f"[{source_label}]asplit=2[duck_main][duck_sc]")
    graph_parts.append(f"[duck_sc]{detector_chain}[duck_ctl]")
    graph_parts.append(f"[duck_main][duck_ctl]{gate_filter}[ducked]")

    if post_duck_chain:
        graph_parts.append(f"[ducked]{post_duck_chain}[{output_label}]")
    else:
        graph_parts.append(f"[ducked]anull[{output_label}]")
    return ";".join(graph_parts)


def build_ffmpeg_finalize_commands(
    *,
    denoised_wav: Path,
    clean_wav: Path,
    transcript_mp3: Path,
    preset: dict[str, Any],
    ffmpeg_bin: str,
    noise_sample_wav: Path | None = None,
    noise_sample_duration: float | None = None,
) -> list[list[str]]:
    transcript = preset["transcript_export"]
    breath_ducking_enabled = (
        preset.get("filters", {}).get("breath_ducking", {}).get("enabled", False)
    )
    if noise_sample_wav is not None:
        if noise_sample_duration is None or noise_sample_duration <= 0:
            raise ValueError("noise_sample_duration must be provided when noise_sample_wav is used")
        sample_duration = _format_filter_float(noise_sample_duration)
        noise_print_chain = build_mastering_filter_chain(
            preset,
            for_noise_print=True,
            include_loudnorm=False,
            include_tonal_shaping=False,
            include_declick=False,
            include_nonlinear_denoise=False,
            include_deesser=False,
            include_gate=False,
            include_compressor=False,
            include_speech_norm=False,
        )
        if breath_ducking_enabled:
            voice_graph = build_breath_ducking_filter_graph(
                input_label="den",
                output_label="outa",
                preset=preset,
                include_loudnorm=True,
            )
            filter_complex = (
                "[0:a][1:a]concat=n=2:v=0:a=1[cat];"
                f"[cat]{noise_print_chain},atrim=start={sample_duration},asetpts=PTS-STARTPTS[den];"
                f"{voice_graph}"
            )
        else:
            voice_chain = build_mastering_filter_chain(
                preset,
                include_secondary_denoise=False,
            )
            filter_complex = (
                "[0:a][1:a]concat=n=2:v=0:a=1[cat];"
                f"[cat]{noise_print_chain},atrim=start={sample_duration},asetpts=PTS-STARTPTS[outa]"
            )
            if voice_chain:
                filter_complex = (
                    "[0:a][1:a]concat=n=2:v=0:a=1[cat];"
                    f"[cat]{noise_print_chain},atrim=start={sample_duration},asetpts=PTS-STARTPTS[den];"
                    f"[den]{voice_chain}[outa]"
                )
        clean_command = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-nostdin",
            "-i",
            str(denoised_wav),
            "-i",
            str(noise_sample_wav),
            "-filter_complex",
            filter_complex,
            "-map",
            "[outa]",
            "-ar",
            str(preset["extract"]["sample_rate"]),
            "-ac",
            str(preset["extract"]["channels"]),
            "-c:a",
            preset["extract"]["pcm_codec"],
            str(clean_wav),
        ]
    elif breath_ducking_enabled:
        filter_complex = build_breath_ducking_filter_graph(
            input_label="0:a",
            output_label="outa",
            preset=preset,
            include_loudnorm=True,
        )
        clean_command = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-nostdin",
            "-i",
            str(denoised_wav),
            "-filter_complex",
            filter_complex,
            "-map",
            "[outa]",
            "-ar",
            str(preset["extract"]["sample_rate"]),
            "-ac",
            str(preset["extract"]["channels"]),
            "-c:a",
            preset["extract"]["pcm_codec"],
            str(clean_wav),
        ]
    else:
        clean_command = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-nostdin",
            "-i",
            str(denoised_wav),
            "-af",
            build_mastering_filter_chain(preset),
            "-ar",
            str(preset["extract"]["sample_rate"]),
            "-ac",
            str(preset["extract"]["channels"]),
            "-c:a",
            preset["extract"]["pcm_codec"],
            str(clean_wav),
        ]

    return [
        clean_command,
        [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-nostdin",
            "-i",
            str(clean_wav),
            "-c:a",
            transcript["codec"],
            "-b:a",
            transcript["bitrate"],
            str(transcript_mp3),
        ],
    ]


def extract_loudnorm_summary(stderr: str) -> dict[str, Any] | None:
    matches = JSON_BLOCK_PATTERN.findall(stderr)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


def detect_silence_candidates(
    audio_path: Path,
    *,
    ffmpeg_bin: str,
    threshold_db: float,
    min_duration: float,
) -> list[dict[str, float]]:
    ffmpeg = ensure_tool(ffmpeg_bin)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-i",
        str(audio_path),
        "-af",
        f"silencedetect=n={threshold_db}dB:d={min_duration}",
        "-f",
        "null",
        "-",
    ]
    completed = run_command(cmd)
    stderr = completed.stderr

    starts = [float(match.group(1)) for match in SILENCE_START_PATTERN.finditer(stderr)]
    ends = [
        (float(match.group(1)), float(match.group(2)))
        for match in SILENCE_END_PATTERN.finditer(stderr)
    ]

    candidates: list[dict[str, float]] = []
    for index, (silence_end, silence_duration) in enumerate(ends):
        silence_start = (
            starts[index] if index < len(starts) else max(0.0, silence_end - silence_duration)
        )
        candidates.append(
            {
                "start_seconds": round(silence_start, 3),
                "end_seconds": round(silence_end, 3),
                "duration_seconds": round(silence_duration, 3),
            }
        )
    return candidates


def merge_noise_windows(
    windows: list[NoiseWindow], max_gap_seconds: float = 0.01
) -> list[NoiseWindow]:
    if not windows:
        return []

    merged: list[NoiseWindow] = []
    for window in sorted(windows, key=lambda item: (item.start_seconds, item.end_seconds)):
        if not merged:
            merged.append(window)
            continue
        previous = merged[-1]
        if window.start_seconds <= previous.end_seconds + max_gap_seconds:
            merged[-1] = NoiseWindow(
                start_seconds=previous.start_seconds,
                end_seconds=max(previous.end_seconds, window.end_seconds),
            )
        else:
            merged.append(window)
    return merged


def infer_breath_onset_windows(
    low_threshold_silences: list[dict[str, float]],
    high_threshold_silences: list[dict[str, float]],
    *,
    min_breath_seconds: float,
    max_breath_seconds: float,
    pre_roll_seconds: float,
) -> list[NoiseWindow]:
    candidates: list[NoiseWindow] = []
    for low_item in low_threshold_silences:
        low_end = float(low_item["end_seconds"])
        low_start = float(low_item["start_seconds"])
        for high_item in high_threshold_silences:
            high_start = float(high_item["start_seconds"])
            high_end = float(high_item["end_seconds"])
            if high_start - 0.01 <= low_end <= high_end and high_end > low_end:
                breath_duration = high_end - low_end
                if min_breath_seconds <= breath_duration <= max_breath_seconds:
                    candidates.append(
                        NoiseWindow(
                            start_seconds=max(low_start, low_end - pre_roll_seconds),
                            end_seconds=high_end,
                        )
                    )
                break
    return merge_noise_windows(candidates)


def _compute_rms(samples: array) -> float:
    if not samples:
        return 0.0
    total = 0.0
    for value in samples:
        total += float(value) * float(value)
    return (total / len(samples)) ** 0.5


def infer_breath_window_from_frame_rms(
    frame_rms: list[float],
    *,
    silence_floor_rms: float,
    hop_seconds: float,
    min_breath_seconds: float,
    max_breath_seconds: float,
    speech_start_ratio: float,
    breath_over_noise_ratio: float,
    speech_over_breath_ratio: float,
    speech_confirm_frames: int,
) -> tuple[float, float] | None:
    if not frame_rms:
        return None

    peak_rms = max(frame_rms)
    if peak_rms <= 0:
        return None

    threshold = max(
        peak_rms * speech_start_ratio, silence_floor_rms * breath_over_noise_ratio * 1.25
    )
    onset_index: int | None = None
    for index in range(len(frame_rms)):
        confirm_slice = frame_rms[index : index + speech_confirm_frames]
        if len(confirm_slice) < speech_confirm_frames:
            break
        if sum(confirm_slice) / len(confirm_slice) >= threshold:
            onset_index = index
            break

    if onset_index is None or onset_index == 0:
        return None

    breath_duration = onset_index * hop_seconds
    if breath_duration < min_breath_seconds or breath_duration > max_breath_seconds:
        return None

    breath_avg = sum(frame_rms[:onset_index]) / float(onset_index)
    speech_slice = frame_rms[onset_index : onset_index + speech_confirm_frames]
    speech_avg = sum(speech_slice) / float(len(speech_slice))
    if breath_avg <= silence_floor_rms * breath_over_noise_ratio:
        return None
    if speech_avg <= breath_avg * speech_over_breath_ratio:
        return None
    return (0.0, breath_duration)


def infer_breath_windows_from_silence_edges(
    samples: array,
    *,
    sample_rate: int,
    silences: list[dict[str, float]],
    config: dict[str, Any],
) -> list[NoiseWindow]:
    hop_samples = max(1, int(sample_rate * (float(config["analysis_hop_ms"]) / 1000.0)))
    scan_samples = max(hop_samples, int(sample_rate * (float(config["analysis_scan_ms"]) / 1000.0)))
    noise_floor_samples = max(
        hop_samples, int(sample_rate * (float(config["noise_floor_ms"]) / 1000.0))
    )

    candidates: list[NoiseWindow] = []
    for silence in silences:
        silence_end_seconds = float(silence["end_seconds"])
        silence_end_index = min(len(samples), max(0, int(silence_end_seconds * sample_rate)))
        if silence_end_index >= len(samples):
            continue

        noise_floor_start = max(0, silence_end_index - noise_floor_samples)
        silence_floor_rms = _compute_rms(samples[noise_floor_start:silence_end_index])

        frame_rms: list[float] = []
        scan_end = min(len(samples), silence_end_index + scan_samples)
        index = silence_end_index
        while index + hop_samples <= scan_end:
            frame_rms.append(_compute_rms(samples[index : index + hop_samples]))
            index += hop_samples

        inferred = infer_breath_window_from_frame_rms(
            frame_rms,
            silence_floor_rms=silence_floor_rms,
            hop_seconds=hop_samples / float(sample_rate),
            min_breath_seconds=float(config["min_breath_ms"]) / 1000.0,
            max_breath_seconds=float(config["max_breath_ms"]) / 1000.0,
            speech_start_ratio=float(config["speech_start_ratio"]),
            breath_over_noise_ratio=float(config["breath_over_noise_ratio"]),
            speech_over_breath_ratio=float(config["speech_over_breath_ratio"]),
            speech_confirm_frames=int(config["speech_confirm_frames"]),
        )
        if inferred is None:
            continue

        _, inferred_end_offset = inferred
        candidates.append(
            NoiseWindow(
                start_seconds=max(
                    0.0, silence_end_seconds - (float(config["pre_roll_ms"]) / 1000.0)
                ),
                end_seconds=silence_end_seconds + inferred_end_offset,
            )
        )
    return merge_noise_windows(candidates)


def detect_breath_onset_windows(
    audio_path: Path,
    *,
    ffmpeg_bin: str,
    config: dict[str, Any],
) -> list[NoiseWindow]:
    low_threshold_silences = detect_silence_candidates(
        audio_path,
        ffmpeg_bin=ffmpeg_bin,
        threshold_db=float(config["low_threshold_db"]),
        min_duration=float(config["silence_min_duration"]),
    )
    high_threshold_silences = detect_silence_candidates(
        audio_path,
        ffmpeg_bin=ffmpeg_bin,
        threshold_db=float(config["high_threshold_db"]),
        min_duration=float(config["silence_min_duration"]),
    )
    seed_windows = infer_breath_onset_windows(
        low_threshold_silences,
        high_threshold_silences,
        min_breath_seconds=float(config["min_breath_ms"]) / 1000.0,
        max_breath_seconds=float(config["max_breath_ms"]) / 1000.0,
        pre_roll_seconds=float(config["pre_roll_ms"]) / 1000.0,
    )

    with wave.open(str(audio_path), "rb") as reader:
        params = reader.getparams()
        if params.sampwidth != 2 or params.nchannels != 1:
            raise ValueError("Breath onset detection currently expects mono 16-bit WAV audio")
        raw_frames = reader.readframes(params.nframes)
    samples = array("h")
    samples.frombytes(raw_frames)

    inferred_windows = infer_breath_windows_from_silence_edges(
        samples,
        sample_rate=params.framerate,
        silences=low_threshold_silences,
        config=config,
    )
    return merge_noise_windows([*seed_windows, *inferred_windows], max_gap_seconds=0.02)


def duck_samples_for_windows(
    samples: array,
    *,
    sample_rate: int,
    windows: list[NoiseWindow],
    floor_gain: float,
    fade_ms: float,
) -> None:
    merged_windows = merge_noise_windows(windows)
    fade_samples = max(0, int(sample_rate * (fade_ms / 1000.0)))
    total_samples = len(samples)

    for window in merged_windows:
        start_index = max(0, min(total_samples, int(window.start_seconds * sample_rate)))
        end_index = max(start_index, min(total_samples, int(window.end_seconds * sample_rate)))
        if end_index <= start_index:
            continue

        local_fade = min(fade_samples, max(0, (end_index - start_index) // 2))
        for index in range(start_index, end_index):
            gain = floor_gain
            if local_fade > 0 and index < start_index + local_fade:
                progress = (index - start_index) / float(local_fade)
                gain = 1.0 - (1.0 - floor_gain) * progress
            elif local_fade > 0 and index >= end_index - local_fade:
                progress = (index - (end_index - local_fade)) / float(local_fade)
                gain = floor_gain + (1.0 - floor_gain) * progress
            samples[index] = int(samples[index] * gain)


def duck_audio_file_in_place(
    audio_path: Path,
    *,
    windows: list[NoiseWindow],
    floor_gain: float,
    fade_ms: float,
) -> None:
    merged_windows = merge_noise_windows(windows)
    if not merged_windows:
        return

    with wave.open(str(audio_path), "rb") as reader:
        params = reader.getparams()
        if params.sampwidth != 2 or params.nchannels != 1:
            raise ValueError("Breath ducking currently expects mono 16-bit WAV audio")
        raw_frames = reader.readframes(params.nframes)

    samples = array("h")
    samples.frombytes(raw_frames)
    duck_samples_for_windows(
        samples,
        sample_rate=params.framerate,
        windows=merged_windows,
        floor_gain=floor_gain,
        fade_ms=fade_ms,
    )

    temp_path = audio_path.with_name(f"{audio_path.stem}.breathduck.tmp.wav")
    with wave.open(str(temp_path), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(samples.tobytes())
    temp_path.replace(audio_path)


def _amplitude_to_dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return -120.0
    return 20.0 * math.log10(amplitude / 32767.0)


def measure_segment_levels(
    samples: array,
    *,
    start_index: int,
    end_index: int,
) -> tuple[float, float]:
    start = max(0, min(len(samples), start_index))
    end = max(start, min(len(samples), end_index))
    if end <= start:
        return -120.0, -120.0

    peak = 0
    energy = 0.0
    count = end - start
    for index in range(start, end):
        value = abs(int(samples[index]))
        if value > peak:
            peak = value
        energy += float(value * value)

    rms = math.sqrt(energy / float(count)) if count else 0.0
    return _amplitude_to_dbfs(float(peak)), _amplitude_to_dbfs(rms)


def infer_pause_residual_cleanup_windows(
    samples: array,
    *,
    sample_rate: int,
    silence_candidates: list[dict[str, float]],
    min_neighbor_silence_duration: float,
    bridge_max_duration: float,
    bridge_peak_db: float,
    bridge_rms_db: float,
    core_pad_seconds: float,
) -> list[NoiseWindow]:
    windows: list[NoiseWindow] = []
    silence_windows = [
        NoiseWindow(
            start_seconds=float(item["start_seconds"]),
            end_seconds=float(item["end_seconds"]),
        )
        for item in silence_candidates
    ]

    for window in silence_windows:
        duration = window.end_seconds - window.start_seconds
        if duration <= (core_pad_seconds * 2.0):
            continue
        core_start = window.start_seconds + core_pad_seconds
        core_end = window.end_seconds - core_pad_seconds
        if core_end > core_start:
            windows.append(NoiseWindow(start_seconds=core_start, end_seconds=core_end))

    for previous, current in zip(silence_windows, silence_windows[1:]):
        previous_duration = previous.end_seconds - previous.start_seconds
        current_duration = current.end_seconds - current.start_seconds
        if (
            previous_duration < min_neighbor_silence_duration
            or current_duration < min_neighbor_silence_duration
        ):
            continue

        bridge_start = previous.end_seconds
        bridge_end = current.start_seconds
        bridge_duration = bridge_end - bridge_start
        if bridge_duration <= 0.0 or bridge_duration > bridge_max_duration:
            continue

        peak_db, rms_db = measure_segment_levels(
            samples,
            start_index=int(bridge_start * sample_rate),
            end_index=int(bridge_end * sample_rate),
        )
        if peak_db <= bridge_peak_db and rms_db <= bridge_rms_db:
            windows.append(NoiseWindow(start_seconds=bridge_start, end_seconds=bridge_end))

    return merge_noise_windows(windows, max_gap_seconds=0.02)


def _ensure_directories(layout: OutputLayout) -> None:
    layout.preprocess_dir.mkdir(parents=True, exist_ok=True)
    layout.transcript_dir.mkdir(parents=True, exist_ok=True)
    if layout.deepfilternet_dir is not None:
        layout.deepfilternet_dir.mkdir(parents=True, exist_ok=True)


def _find_single_wav(directory: Path) -> Path:
    candidates = sorted(directory.glob("*.wav"))
    if len(candidates) != 1:
        raise FileNotFoundError(
            f"Expected exactly one WAV output from DeepFilterNet in {directory}, found {len(candidates)}"
        )
    return candidates[0]


def _processing_steps(
    preset: dict[str, Any],
    *,
    skip_spectramini: bool = False,
    skip_deepfilternet: bool = False,
) -> list[str]:
    steps = ["Extract source audio to WAV"]
    stage_types = [
        stage.get("type")
        for stage in preset.get("pipeline", {}).get("stages", [])
        if stage.get("enabled", True)
    ]
    if "respiro" in stage_types:
        steps.append("Local fallback breath detection")
    if "spectramini" in stage_types and not skip_spectramini:
        steps.append("SpectraMini-style breath control")
        steps.append("SpectraMini-style mouth de-click")
    if "deepfilternet" in stage_types and not skip_deepfilternet:
        steps.append("Primary denoise via DeepFilterNet")
    filters = preset["filters"]
    if filters.get("declick", {}).get("enabled"):
        steps.append("Mouth-click reduction via adeclick")
    if filters.get("breath_ducking", {}).get("enabled"):
        steps.append("Speech-presence breath ducking via sidechaingate")
    if filters.get("secondary_denoise", {}).get("enabled"):
        steps.append("Secondary FFmpeg denoise via afftdn")
    if filters.get("nonlinear_denoise", {}).get("enabled"):
        steps.append("Non-local denoise via anlmdn")
    if filters.get("deesser", {}).get("enabled"):
        steps.append("Breath and sibilance control via deesser")
    if filters.get("gate", {}).get("enabled"):
        steps.append("Dynamic gate via agate")
    if filters.get("compressor", {}).get("enabled"):
        steps.append("Voice compression via acompressor")
    if filters.get("speech_norm", {}).get("enabled"):
        steps.append("Speech leveling via speechnorm")
    if filters.get("breath_onset_cleanup", {}).get("enabled"):
        steps.append("Breath-onset cleanup before speech entries")
    if filters.get("pause_residual_cleanup", {}).get("enabled"):
        steps.append("Residual pause cleanup in long silences")
    steps.append("Loudness normalization via loudnorm")
    steps.append("Transcript-ready MP3 export")
    return steps


def _noise_print_processing_steps(
    preset: dict[str, Any],
    *,
    skip_spectramini: bool = False,
    skip_deepfilternet: bool = False,
) -> list[str]:
    steps = ["Extract source audio to WAV"]
    stage_types = [
        stage.get("type")
        for stage in preset.get("pipeline", {}).get("stages", [])
        if stage.get("enabled", True)
    ]
    if "respiro" in stage_types:
        steps.append("Local fallback breath detection")
    if "spectramini" in stage_types and not skip_spectramini:
        steps.append("SpectraMini-style breath control")
        steps.append("SpectraMini-style mouth de-click")
    if "deepfilternet" in stage_types and not skip_deepfilternet:
        steps.append("Primary denoise via DeepFilterNet")
    steps.extend(
        [
            "Capture noise sample from selected windows",
            "Noise-print denoise via afftdn sample capture",
        ]
    )
    filters = preset["filters"]
    if filters.get("declick", {}).get("enabled"):
        steps.append("Mouth-click reduction via adeclick")
    if filters.get("breath_ducking", {}).get("enabled"):
        steps.append("Speech-presence breath ducking via sidechaingate")
    if filters.get("nonlinear_denoise", {}).get("enabled"):
        steps.append("Non-local denoise via anlmdn")
    if filters.get("deesser", {}).get("enabled"):
        steps.append("Breath and sibilance control via deesser")
    if filters.get("gate", {}).get("enabled"):
        steps.append("Dynamic gate via agate")
    if filters.get("compressor", {}).get("enabled"):
        steps.append("Voice compression via acompressor")
    if filters.get("speech_norm", {}).get("enabled"):
        steps.append("Speech leveling via speechnorm")
    if filters.get("breath_onset_cleanup", {}).get("enabled"):
        steps.append("Breath-onset cleanup before speech entries")
    if filters.get("pause_residual_cleanup", {}).get("enabled"):
        steps.append("Residual pause cleanup in long silences")
    steps.append("Loudness normalization via loudnorm")
    steps.append("Transcript-ready MP3 export")
    return steps


def _noise_sample_duration(noise_windows: list[NoiseWindow]) -> float:
    return sum(window.end_seconds - window.start_seconds for window in noise_windows)


def _normalize_noise_windows(noise_windows: list[NoiseWindow] | None) -> list[NoiseWindow]:
    return list(noise_windows or [])


def _load_wave_samples(audio_path: Path) -> tuple[Any, array]:
    with wave.open(str(audio_path), "rb") as reader:
        params = reader.getparams()
        if params.sampwidth != 2 or params.nchannels != 1:
            raise ValueError("Expected mono 16-bit WAV audio")
        raw_frames = reader.readframes(params.nframes)
    samples = array("h")
    samples.frombytes(raw_frames)
    return params, samples


def _read_wave_duration_seconds(audio_path: Path) -> float:
    with wave.open(str(audio_path), "rb") as reader:
        frame_rate = reader.getframerate()
        if frame_rate <= 0:
            return 0.0
        return reader.getnframes() / float(frame_rate)


def _write_wave_samples(audio_path: Path, params: Any, samples: array) -> None:
    with wave.open(str(audio_path), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(samples.tobytes())


def repair_deepfilternet_speech_dropouts(
    reference_samples: array,
    processed_samples: array,
    *,
    sample_rate: int,
    window_seconds: float = 0.06,
    hop_seconds: float = 0.01,
    reference_peak_db_min: float = -24.0,
    reference_rms_db_min: float = -38.0,
    processed_peak_db_max: float = -34.0,
    processed_rms_db_max: float = -46.0,
    copy_padding_seconds: float = 0.008,
    max_repair_duration_seconds: float = 0.16,
    context_window_seconds: float = 0.22,
    context_gap_seconds: float = 0.02,
    context_peak_db_min: float = -20.0,
    context_rms_db_min: float = -32.0,
) -> tuple[array, list[NoiseWindow]]:
    if len(reference_samples) != len(processed_samples):
        raise ValueError("reference_samples and processed_samples must have the same length")
    if not reference_samples:
        return array("h", processed_samples), []

    window_size = max(1, int(window_seconds * sample_rate))
    hop_size = max(1, int(hop_seconds * sample_rate))
    copy_padding = max(0, int(copy_padding_seconds * sample_rate))
    max_repair_samples = max(window_size, int(max_repair_duration_seconds * sample_rate))
    context_window_size = max(1, int(context_window_seconds * sample_rate))
    context_gap_size = max(0, int(context_gap_seconds * sample_rate))
    total_samples = len(reference_samples)

    candidate_windows: list[NoiseWindow] = []

    def has_voiced_context(start_index: int, end_index: int) -> bool:
        left_end = max(0, start_index - context_gap_size)
        left_start = max(0, left_end - context_window_size)
        right_start = min(total_samples, end_index + context_gap_size)
        right_end = min(total_samples, right_start + context_window_size)
        if left_end <= left_start or right_end <= right_start:
            return False

        left_peak_db, left_rms_db = measure_segment_levels(
            reference_samples,
            start_index=left_start,
            end_index=left_end,
        )
        right_peak_db, right_rms_db = measure_segment_levels(
            reference_samples,
            start_index=right_start,
            end_index=right_end,
        )
        return (
            left_peak_db >= context_peak_db_min
            and left_rms_db >= context_rms_db_min
            and right_peak_db >= context_peak_db_min
            and right_rms_db >= context_rms_db_min
        )

    index = 0
    while index + window_size <= total_samples:
        ref_peak_db, ref_rms_db = measure_segment_levels(
            reference_samples,
            start_index=index,
            end_index=index + window_size,
        )
        proc_peak_db, proc_rms_db = measure_segment_levels(
            processed_samples,
            start_index=index,
            end_index=index + window_size,
        )
        if (
            ref_peak_db >= reference_peak_db_min
            and ref_rms_db >= reference_rms_db_min
            and proc_peak_db <= processed_peak_db_max
            and proc_rms_db <= processed_rms_db_max
        ):
            start = index
            end = index + window_size
            index += hop_size
            while index + window_size <= total_samples:
                ref_peak_db, ref_rms_db = measure_segment_levels(
                    reference_samples,
                    start_index=index,
                    end_index=index + window_size,
                )
                proc_peak_db, proc_rms_db = measure_segment_levels(
                    processed_samples,
                    start_index=index,
                    end_index=index + window_size,
                )
                if not (
                    ref_peak_db >= reference_peak_db_min
                    and ref_rms_db >= reference_rms_db_min
                    and proc_peak_db <= processed_peak_db_max
                    and proc_rms_db <= processed_rms_db_max
                ):
                    break
                end = index + window_size
                index += hop_size
            if (end - start) <= max_repair_samples and has_voiced_context(start, end):
                candidate_windows.append(
                    NoiseWindow(
                        start_seconds=max(0.0, (start - copy_padding) / float(sample_rate)),
                        end_seconds=min(
                            total_samples / float(sample_rate),
                            (end + copy_padding) / float(sample_rate),
                        ),
                    )
                )
            continue
        index += hop_size

    merged_windows = merge_noise_windows(
        candidate_windows, max_gap_seconds=max(0.01, hop_seconds * 2.0)
    )
    repaired = array("h", processed_samples)
    for window in merged_windows:
        start_index = max(0, min(total_samples, int(window.start_seconds * sample_rate)))
        end_index = max(start_index, min(total_samples, int(window.end_seconds * sample_rate)))
        repaired[start_index:end_index] = reference_samples[start_index:end_index]
    return repaired, merged_windows


def process_media_file(
    input_file: Path,
    *,
    preset_name: str,
    preset: dict[str, Any],
    output_root: Path,
    runtime: RuntimeOptions,
    run_slug: str,
    input_metadata: dict[str, Any] | None = None,
    noise_windows: list[NoiseWindow] | None = None,
    respiro_repo: Path | None = None,
    respiro_weights: Path | None = None,
    attenuation_db: float = 18.0,
    respiro_threshold: float | None = None,
    respiro_min_length_ms: int | None = None,
    skip_spectramini: bool = False,
    skip_deepfilternet: bool = False,
    deepfilternet_runtime: dict[str, Any] | None = None,
    respiro_verified_runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_python = runtime.python_executable or resolve_repo_python(PROJECT_ROOT)
    if not skip_deepfilternet and not _deepfilternet_runtime_ready(
        deepfilternet_runtime,
        python_executable=requested_python,
    ):
        raise RuntimeError(
            "unverified_deepfilternet_runtime: DeepFilterNet requires a verified local "
            "model and adapter; rerun with --skip-deepfilternet to use only local "
            "deterministic stages."
        )
    if not skip_deepfilternet:
        _raise_external_model_execution_unavailable("deepfilternet")
    if respiro_repo is not None or respiro_weights is not None:
        if (
            respiro_repo is None
            or respiro_weights is None
            or not _respiro_runtime_matches(
                respiro_verified_runtime,
                repo_path=respiro_repo,
                weights_path=respiro_weights,
                python_executable=requested_python,
            )
        ):
            raise RuntimeError(
                "unverified_respiro_runtime: configured Respiro-en paths are not bound to a "
                "verified revision, license, SHA-256, adapter, and self-check identity."
            )
        _raise_external_model_execution_unavailable("respiro")
    metadata = input_metadata or ffprobe_media(input_file, runtime.ffprobe_bin)
    layout = build_output_layout(
        input_path=input_file,
        output_root=output_root,
        run_slug=run_slug,
        skip_deepfilternet=skip_deepfilternet,
    )
    resolved_noise_windows = _normalize_noise_windows(noise_windows)
    noise_sample_duration = (
        _noise_sample_duration(resolved_noise_windows) if resolved_noise_windows else None
    )

    ffmpeg = runtime.ffmpeg_bin if runtime.dry_run else ensure_tool(runtime.ffmpeg_bin)
    python_bin = requested_python
    if not runtime.dry_run:
        python_bin = ensure_tool(python_bin)

    commands: list[list[str]] = [
        build_ffmpeg_extract_command(
            input_path=input_file, raw_wav=layout.raw_wav, preset=preset, ffmpeg_bin=ffmpeg
        ),
    ]
    if not skip_deepfilternet:
        assert layout.deepfilternet_dir is not None
        commands.append(
            build_deepfilternet_command(
                raw_wav=layout.raw_wav,
                output_dir=layout.deepfilternet_dir,
                preset=preset,
                python_executable=python_bin,
                verified_runtime=deepfilternet_runtime,
            )
        )
    if resolved_noise_windows:
        commands.append(
            build_ffmpeg_noise_sample_command(
                source_wav=layout.denoised_wav,
                noise_sample_wav=layout.noise_sample_wav,
                noise_windows=resolved_noise_windows,
                preset=preset,
                ffmpeg_bin=ffmpeg,
            )
        )
    commands.extend(
        build_ffmpeg_finalize_commands(
            denoised_wav=layout.denoised_wav,
            clean_wav=layout.clean_wav,
            transcript_mp3=layout.transcript_mp3,
            preset=preset,
            ffmpeg_bin=ffmpeg,
            noise_sample_wav=layout.noise_sample_wav if resolved_noise_windows else None,
            noise_sample_duration=noise_sample_duration,
        )
    )

    processing_steps = (
        _noise_print_processing_steps(
            preset,
            skip_spectramini=skip_spectramini,
            skip_deepfilternet=skip_deepfilternet,
        )
        if resolved_noise_windows
        else _processing_steps(
            preset,
            skip_spectramini=skip_spectramini,
            skip_deepfilternet=skip_deepfilternet,
        )
    )

    report: dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "preset_name": preset_name,
        "backend": "local",
        "execution_policy": "external_models_fail_closed",
        "input_file": str(input_file),
        "input_metadata": metadata,
        "processing_steps": processing_steps,
        "commands": commands,
        "dry_run": runtime.dry_run,
        "noise_windows": [
            {"start_seconds": window.start_seconds, "end_seconds": window.end_seconds}
            for window in resolved_noise_windows
        ],
        "noise_sample_duration_seconds": noise_sample_duration,
        "outputs": {
            "job_dir": str(layout.job_dir),
            "preprocess_dir": str(layout.preprocess_dir),
            "transcript_dir": str(layout.transcript_dir),
            "raw_wav": str(layout.raw_wav),
            "denoised_wav": str(layout.denoised_wav),
            "noise_sample_wav": str(layout.noise_sample_wav),
            "clean_wav": str(layout.clean_wav),
            "transcript_mp3": str(layout.transcript_mp3),
            "report_json": str(layout.report_json),
            "report_md": str(layout.report_md),
        },
        "silence_candidates": [],
        "loudnorm_summary": None,
        "notes": [
            note
            for note in preset.get("notes", [])
            if not (skip_deepfilternet and "deepfilternet" in str(note).casefold())
        ],
        "respiro_detection_mode": "disabled",
        "respiro_assets_present": bool(
            respiro_repo and respiro_weights and respiro_repo.exists() and respiro_weights.exists()
        ),
        "respiro_attempted": False,
        "respiro_succeeded": False,
        "respiro_returncode": None,
        "respiro_error": None,
        "respiro_repo_path": str(respiro_repo) if respiro_repo else None,
        "respiro_weights_path": str(respiro_weights) if respiro_weights else None,
        "respiro_command": None,
        "respiro_stdout": "",
        "respiro_stderr": "",
        "spectramini_applied": False,
        "pause_residual_cleanup_windows": [],
        "model_execution": {
            "deepfilternet": {
                "status": "skipped" if skip_deepfilternet else "external_unavailable",
                "attempted": False,
                "succeeded": False,
                "execution_receipt": None,
            },
            "respiro": {
                "status": "fallback",
                "attempted": False,
                "succeeded": False,
                "execution_receipt": None,
            },
        },
    }

    if runtime.dry_run:
        return report

    _ensure_directories(layout)
    executed: list[dict[str, Any]] = []

    extract_command = commands[0]
    completed = run_command(extract_command)
    executed.append(
        {
            "command": extract_command,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "command failed")

    respiro_stage = get_pipeline_stage(preset, "respiro")
    fallback_config = {
        **DEFAULT_BREATH_FALLBACK_CONFIG,
        **preset.get("filters", {}).get("breath_onset_cleanup", {}),
    }
    threshold = float(
        respiro_threshold
        if respiro_threshold is not None
        else respiro_stage.get("threshold", 0.064)
    )
    min_length = int(
        respiro_min_length_ms
        if respiro_min_length_ms is not None
        else respiro_stage.get("min_length_ms", 20)
    )
    respiro_result = run_respiro_or_fallback_detection(
        audio_path=layout.raw_wav,
        ffmpeg_bin=runtime.ffmpeg_bin,
        respiro_repo=respiro_repo,
        respiro_weights=respiro_weights,
        python_executable=python_bin,
        threshold=threshold,
        min_length_ms=min_length,
        fallback_config=fallback_config,
        verified_runtime=respiro_verified_runtime,
    )
    breath_windows = respiro_result.windows
    report["breath_onset_windows"] = [
        {"start_seconds": window.start_seconds, "end_seconds": window.end_seconds}
        for window in breath_windows
    ]
    report["respiro_detection_mode"] = respiro_result.mode
    report["respiro_assets_present"] = respiro_result.assets_present
    report["respiro_attempted"] = respiro_result.attempted
    report["respiro_succeeded"] = respiro_result.succeeded
    report["respiro_returncode"] = respiro_result.returncode
    report["respiro_error"] = respiro_result.error
    report["respiro_command"] = respiro_result.command
    report["respiro_stdout"] = respiro_result.stdout
    report["respiro_stderr"] = respiro_result.stderr
    report["model_execution"]["respiro"] = {
        "status": (
            "external_unavailable"
            if respiro_result.mode == "respiro" or respiro_result.succeeded
            else "fallback"
        ),
        "attempted": False,
        "succeeded": False,
        "execution_receipt": None,
    }
    if not respiro_result.succeeded or respiro_result.mode != "respiro":
        report["processing_steps"] = [
            "Local fallback breath detection" if step == "Breath detection via Respiro-en" else step
            for step in report["processing_steps"]
        ]

    if layout.raw_wav.exists() and not skip_spectramini:
        params, samples = _load_wave_samples(layout.raw_wav)
        spectramini_stage = get_pipeline_stage(preset, "spectramini")
        cleaned = apply_spectramini_style_cleanup_to_samples(
            samples,
            breath_windows=breath_windows,
            sample_rate=params.framerate,
            attenuation_db=attenuation_db,
            mouth_declick_sensitivity=float(
                spectramini_stage.get("mouth_declick_sensitivity", 0.55)
            ),
            fade_ms=float(fallback_config.get("fade_ms", 14.0)),
        )
        _write_wave_samples(layout.raw_wav, params, cleaned)
        report["spectramini_applied"] = not skip_spectramini

    if not skip_deepfilternet:
        deepfilter_command = commands[1]
        assert deepfilternet_runtime is not None
        assert layout.deepfilternet_dir is not None
        completed = _run_verified_adapter_command(
            deepfilter_command,
            verified_runtime=deepfilternet_runtime,
        )
        executed.append(
            {
                "command": deepfilter_command,
                "returncode": completed.returncode,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
            }
        )
        if completed.returncode != 0:
            raise RuntimeError(
                completed.stderr.strip() or completed.stdout.strip() or "command failed"
            )

        detected_df_wav = _find_single_wav(layout.deepfilternet_dir)
        if detected_df_wav.resolve() != layout.denoised_wav.resolve():
            detected_df_wav.replace(layout.denoised_wav)
        repair_windows: list[NoiseWindow] = []
        if layout.raw_wav.exists() and layout.denoised_wav.exists():
            raw_params, raw_samples = _load_wave_samples(layout.raw_wav)
            denoised_params, denoised_samples = _load_wave_samples(layout.denoised_wav)
            repaired_samples, repair_windows = repair_deepfilternet_speech_dropouts(
                raw_samples,
                denoised_samples,
                sample_rate=denoised_params.framerate,
            )
            if repair_windows:
                _write_wave_samples(layout.denoised_wav, denoised_params, repaired_samples)
        report["deepfilternet_dropout_repair_windows"] = [
            {"start_seconds": window.start_seconds, "end_seconds": window.end_seconds}
            for window in repair_windows
        ]
        remaining_commands = commands[2:]
    else:
        shutil.copyfile(layout.raw_wav, layout.denoised_wav)
        report["deepfilternet_dropout_repair_windows"] = []
        remaining_commands = commands[1:]
    for command in remaining_commands:
        completed = run_command(command)
        executed.append(
            {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
            }
        )
        if completed.returncode != 0:
            raise RuntimeError(
                completed.stderr.strip() or completed.stdout.strip() or "command failed"
            )

    breath_onset_cleanup = preset.get("filters", {}).get("breath_onset_cleanup", {})
    if breath_onset_cleanup.get("enabled"):
        breath_windows = detect_breath_onset_windows(
            layout.clean_wav,
            ffmpeg_bin=runtime.ffmpeg_bin,
            config=breath_onset_cleanup,
        )
        report["postprocess_breath_onset_windows"] = [
            {"start_seconds": window.start_seconds, "end_seconds": window.end_seconds}
            for window in breath_windows
        ]
        if breath_windows:
            duck_audio_file_in_place(
                layout.clean_wav,
                windows=breath_windows,
                floor_gain=float(breath_onset_cleanup["floor_gain"]),
                fade_ms=float(breath_onset_cleanup["fade_ms"]),
            )
    else:
        report["postprocess_breath_onset_windows"] = []

    pause_residual_cleanup = preset.get("filters", {}).get("pause_residual_cleanup", {})
    if pause_residual_cleanup.get("enabled"):
        silence_candidates = detect_silence_candidates(
            layout.clean_wav,
            ffmpeg_bin=runtime.ffmpeg_bin,
            threshold_db=float(pause_residual_cleanup["silence_threshold_db"]),
            min_duration=float(pause_residual_cleanup["silence_min_duration"]),
        )
        params, samples = _load_wave_samples(layout.clean_wav)
        pause_windows = infer_pause_residual_cleanup_windows(
            samples,
            sample_rate=params.framerate,
            silence_candidates=silence_candidates,
            min_neighbor_silence_duration=float(
                pause_residual_cleanup["min_neighbor_silence_duration"]
            ),
            bridge_max_duration=float(pause_residual_cleanup["bridge_max_duration"]),
            bridge_peak_db=float(pause_residual_cleanup["bridge_peak_db"]),
            bridge_rms_db=float(pause_residual_cleanup["bridge_rms_db"]),
            core_pad_seconds=float(pause_residual_cleanup["core_pad_ms"]) / 1000.0,
        )
        report["pause_residual_cleanup_windows"] = [
            {"start_seconds": window.start_seconds, "end_seconds": window.end_seconds}
            for window in pause_windows
        ]
        if pause_windows:
            duck_audio_file_in_place(
                layout.clean_wav,
                windows=pause_windows,
                floor_gain=float(pause_residual_cleanup["floor_gain"]),
                fade_ms=float(pause_residual_cleanup["fade_ms"]),
            )

    report["executed"] = executed
    report["loudnorm_summary"] = extract_loudnorm_summary(executed[-2]["stderr"])
    report["output_metadata"] = ffprobe_media(layout.clean_wav, runtime.ffprobe_bin)

    analysis = preset.get("analysis", {})
    if analysis.get("silence_candidates"):
        report["silence_candidates"] = detect_silence_candidates(
            layout.clean_wav,
            ffmpeg_bin=runtime.ffmpeg_bin,
            threshold_db=analysis["silence_threshold_db"],
            min_duration=analysis["silence_min_duration"],
        )

    layout.report_json.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    layout.report_md.write_text(render_markdown_report(report), encoding="utf-8")
    return report


def render_markdown_report(report: dict[str, Any]) -> str:
    silence_lines = (
        ["- none"]
        if not report.get("silence_candidates")
        else [
            f"- {item['start_seconds']}s -> {item['end_seconds']}s ({item['duration_seconds']}s)"
            for item in report["silence_candidates"]
        ]
    )
    noise_window_lines = (
        ["- none"]
        if not report.get("noise_windows")
        else [
            f"- {item['start_seconds']}s -> {item['end_seconds']}s"
            for item in report["noise_windows"]
        ]
    )
    breath_onset_lines = (
        ["- none"]
        if not report.get("breath_onset_windows")
        else [
            f"- {item['start_seconds']}s -> {item['end_seconds']}s"
            for item in report["breath_onset_windows"]
        ]
    )
    loudnorm_block = (
        json.dumps(report["loudnorm_summary"], indent=2) if report.get("loudnorm_summary") else "{}"
    )
    return "\n".join(
        [
            f"# Audio Process Report: {Path(report['input_file']).name}",
            "",
            "## Summary",
            f"- Preset: `{report['preset_name']}`",
            f"- Backend: `{report['backend']}`",
            f"- Input: `{report['input_file']}`",
            f"- Clean WAV: `{report['outputs']['clean_wav']}`",
            f"- Transcript MP3: `{report['outputs']['transcript_mp3']}`",
            f"- Noise sample WAV: `{report['outputs']['noise_sample_wav']}`",
            "",
            "## Processing Steps",
            *[f"- {step}" for step in report["processing_steps"]],
            "",
            "## Input Metadata",
            f"- Duration: `{report['input_metadata'].get('duration_seconds')}`",
            f"- Sample rate: `{report['input_metadata'].get('sample_rate')}`",
            f"- Channels: `{report['input_metadata'].get('channels')}`",
            "",
            "## Noise Windows",
            *noise_window_lines,
            "",
            "## Noise Sample",
            f"- Duration: `{report.get('noise_sample_duration_seconds')}`",
            "",
            "## Respiro Detection",
            f"- Mode: `{report.get('respiro_detection_mode')}`",
            f"- Assets present: `{report.get('respiro_assets_present')}`",
            f"- Attempted: `{report.get('respiro_attempted')}`",
            f"- Succeeded: `{report.get('respiro_succeeded')}`",
            f"- Return code: `{report.get('respiro_returncode')}`",
            f"- Error: `{report.get('respiro_error')}`",
            "",
            "## Model Execution",
            (
                "- DeepFilterNet status: "
                f"`{report.get('model_execution', {}).get('deepfilternet', {}).get('status')}`"
            ),
            (
                "- Respiro status: "
                f"`{report.get('model_execution', {}).get('respiro', {}).get('status')}`"
            ),
            "- DeepFilterNet execution receipt: `none`",
            "- Respiro execution receipt: `none`",
            "",
            "## Breath Onset Windows",
            *breath_onset_lines,
            "",
            "## Loudnorm Summary",
            "```json",
            loudnorm_block,
            "```",
            "",
            "## Silence Candidates",
            *silence_lines,
            "",
            "## Commands",
            "```json",
            json.dumps(report["commands"], indent=2, ensure_ascii=False),
            "```",
        ]
    )


def build_batch_summary(
    *,
    preset_name: str,
    preset_description: str,
    input_path: Path,
    output_root: Path,
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "timestamp_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "preset_name": preset_name,
        "preset_description": preset_description,
        "input_path": str(input_path),
        "output_root": str(output_root),
        "files_processed": len(reports),
        "backend": "local",
        "reports": [
            {
                "input_file": report["input_file"],
                "clean_wav": report["outputs"]["clean_wav"],
                "transcript_mp3": report["outputs"].get("transcript_mp3", ""),
                "report_json": report["outputs"]["report_json"],
                "report_md": report["outputs"]["report_md"],
            }
            for report in reports
        ],
    }


def render_batch_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Batch Summary",
        "",
        f"- Preset: `{summary['preset_name']}`",
        f"- Description: `{summary['preset_description']}`",
        f"- Input path: `{summary['input_path']}`",
        f"- Output root: `{summary['output_root']}`",
        f"- Files processed: `{summary['files_processed']}`",
        "",
        "## Files",
    ]
    for item in summary["reports"]:
        lines.extend(
            [
                f"- `{item['input_file']}`",
                f"  - clean wav: `{item['clean_wav']}`",
                f"  - transcript mp3: `{item['transcript_mp3']}`",
                f"  - report: `{item['report_md']}`",
            ]
        )
    return "\n".join(lines)
