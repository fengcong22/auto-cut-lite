"""Coordinator for diagnostic, local-only ASR.

This module deliberately stops at a verified transcript and text candidates.  It
does not create an executable cut boundary or alter source media.  Heavy model
imports stay in :mod:`audio_sound.local_asr_worker`, which is run in a separate
Python process.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, Mapping

from audio_sound.local_asr_candidates import locate_candidates

SCHEMA_VERSION = "local-asr-transcript-v1"
REQUEST_SCHEMA_VERSION = "local-asr-worker-request-v1"
WORKER_RESULT_SCHEMA_VERSION = "local-asr-worker-result-v1"
COORDINATOR_VERSION = "local-asr-coordinator-v1"
_HASH = set("0123456789abcdef")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def audio_identity(audio_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return a complete identity for a mono PCM16 16 kHz WAV."""
    path = Path(audio_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise FileNotFoundError(path)
    with wave.open(str(path), "rb") as stream:
        params = (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype())
        if params != (1, 2, 16000, "NONE"):
            raise ValueError("local ASR requires mono PCM16 16 kHz WAV")
        frame_count = stream.getnframes()
        pcm = stream.readframes(frame_count)
    if len(pcm) != frame_count * 2 or frame_count <= 0:
        raise ValueError("audio PCM is truncated or empty")
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "pcm_sha256": _sha256_bytes(pcm),
        "sample_rate": 16000,
        "channels": 1,
        "sample_width": 2,
        "frame_count": frame_count,
        "duration_seconds": frame_count / 16000.0,
    }


def model_manifest(model_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Hash every model file; paths are relative POSIX names."""
    root = Path(model_path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    files: dict[str, dict[str, Any]] = {}
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        data = path.read_bytes()
        files[name] = {"sha256": _sha256_bytes(data), "bytes": len(data)}
    if not files:
        raise ValueError("model directory is empty")
    manifest_digest = _sha256_bytes(_json_bytes(files))
    return {"root": str(root), "files": files, "sha256": manifest_digest, "file_count": len(files)}


def _runtime_probe(python_executable: str | os.PathLike[str], project_root: Path) -> dict[str, Any]:
    command = [str(Path(python_executable).expanduser()), "-m", "audio_sound.local_asr_worker", "--probe"]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root) + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(command, cwd=str(project_root), env=environment, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"local ASR runtime probe failed: {completed.stderr.strip() or completed.stdout.strip()}")
    try:
        value = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("local ASR runtime probe returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("local ASR runtime probe returned a non-object")
    return value


def _provider_version(backend: str, runtime: Mapping[str, Any]) -> str:
    versions = runtime.get("package_versions")
    if isinstance(versions, Mapping):
        key = "faster-whisper" if backend == "faster-whisper" else "funasr"
        value = versions.get(key)
        if isinstance(value, str) and value:
            return value
    return "unavailable"


def _adapter_identity() -> dict[str, Any]:
    path = Path(__file__).with_name("local_asr_worker.py").resolve()
    return {"name": "audio_sound.local_asr_worker", "version": "local-asr-worker-v1", "sha256": _sha256_file(path)}


def _parameters(parameters: Mapping[str, Any] | None, backend: str) -> dict[str, Any]:
    incoming = dict(parameters or {})
    device = incoming.get("device", "cpu")
    if device not in {"cpu", "cuda"}:
        # Keep the error aligned with the worker request validator rather than
        # allowing an invalid device to influence a cache identity.
        raise ValueError("device must be cpu or cuda")
    result = {
        "language": incoming.get("language", "zh" if backend == "funasr" else None),
        "device": device,
        "device_index": incoming.get("device_index", 0),
        # CTranslate2's float16 compute type is a CUDA choice.  Keep the
        # default CPU request executable under the worker's explicit compute
        # type policy; callers can still request another supported type.
        "compute_type": incoming.get(
            "compute_type",
            "float32" if backend == "funasr" or device == "cpu" else "int8_float16",
        ),
        "cpu_threads": incoming.get("cpu_threads", max(1, os.cpu_count() or 1)),
        "chunk_seconds": incoming.get("chunk_seconds", 18),
    }
    if set(incoming) - set(result):
        raise ValueError("unsupported ASR parameters; prompts and hotwords are disabled")
    return result


def _cache_identity(audio: Mapping[str, Any], model: Mapping[str, Any], backend: str,
                    parameters: Mapping[str, Any], runtime: Mapping[str, Any], adapter: Mapping[str, Any]) -> Any:
    try:
        CacheIdentity = importlib.import_module("scripts.utils.review_job_pipeline").CacheIdentity
    except Exception as exc:  # pragma: no cover - source checkout supplies this import
        raise RuntimeError("ArtifactCache is unavailable in this source checkout") from exc
    input_key = {key: value for key, value in audio.items() if key != "path"}
    model_key = {key: value for key, value in model.items() if key != "root"}
    return CacheIdentity(
        namespace="local-asr-transcript",
        inputs={"audio": input_key, "model": model_key, "backend": backend, "parameters": dict(parameters)},
        versions={"schema": SCHEMA_VERSION, "coordinator": COORDINATOR_VERSION,
                  "worker": runtime.get("worker_version"), "runtime": dict(runtime), "adapter": dict(adapter)},
    )


def _fixed_false(payload: Mapping[str, Any]) -> list[str]:
    return [key for key in ("production_eligible", "acoustic_accepted", "neighbor_protection_verified",
                            "source_media_modified", "production_integrated") if payload.get(key) is not False]


def _runtime_contains(actual: Any, expected: Mapping[str, Any]) -> bool:
    """Allow inference to append device diagnostics while binding the probe."""
    return isinstance(actual, Mapping) and all(actual.get(key) == value for key, value in expected.items())


def _validate_transcript(
    payload: Any,
    expected_identity: Mapping[str, Any],
    *,
    expected_backend: str | None = None,
    expected_parameters: Mapping[str, Any] | None = None,
    expected_input: Mapping[str, Any] | None = None,
    expected_model_files: Mapping[str, Any] | None = None,
    expected_runtime: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != SCHEMA_VERSION:
        return False, "transcript_schema_invalid"
    if payload.get("status") != "completed":
        return False, "transcript_status_invalid"
    if _fixed_false(payload):
        return False, "production_gate_open"
    if expected_backend is not None and payload.get("backend") != expected_backend:
        return False, "transcript_backend_mismatch"
    if expected_parameters is not None and payload.get("parameters") != dict(expected_parameters):
        return False, "transcript_parameters_mismatch"
    if expected_runtime is not None and not _runtime_contains(payload.get("runtime"), expected_runtime):
        return False, "transcript_runtime_mismatch"
    identity = payload.get("identity")
    if not isinstance(identity, Mapping):
        return False, "transcript_identity_missing"
    for component in ("input", "result", "model", "provider", "adapter"):
        if not isinstance(identity.get(component), Mapping):
            return False, f"identity_{component}_missing"
    if identity["input"].get("sha256") != expected_identity["input_sha256"]:
        return False, "input_identity_mismatch"
    if identity["model"].get("sha256") != expected_identity["model_sha256"]:
        return False, "model_identity_mismatch"
    if identity["provider"].get("name") != expected_identity["provider_name"] or identity["provider"].get("version") != expected_identity["provider_version"]:
        return False, "provider_identity_mismatch"
    if identity["adapter"].get("sha256") != expected_identity["adapter_sha256"]:
        return False, "adapter_identity_mismatch"
    result = identity["result"]
    if (not isinstance(result.get("sha256"), str) or len(result["sha256"]) != 64
            or set(result["sha256"].lower()) - _HASH
            or type(result.get("bytes")) is not int or result["bytes"] < 0):
        return False, "result_identity_invalid"
    worker_report = payload.get("worker_report")
    if not isinstance(worker_report, Mapping):
        return False, "worker_report_missing"
    if worker_report.get("schema_version") != WORKER_RESULT_SCHEMA_VERSION or worker_report.get("status") != "completed":
        return False, "worker_report_status_invalid"
    if _fixed_false(worker_report):
        return False, "worker_report_production_gate_open"
    if expected_backend is not None and worker_report.get("backend") != expected_backend:
        return False, "worker_report_backend_mismatch"
    if expected_runtime is not None and not _runtime_contains(worker_report.get("runtime"), expected_runtime):
        return False, "worker_report_runtime_mismatch"
    request = worker_report.get("request")
    if not isinstance(request, Mapping):
        return False, "worker_report_request_missing"
    if expected_parameters is not None and request.get("parameters") != dict(expected_parameters):
        return False, "worker_report_request_parameters_mismatch"
    if expected_input is not None and request.get("input") != dict(expected_input):
        return False, "worker_report_request_input_mismatch"
    if expected_model_files is not None:
        request_model = request.get("model")
        if not isinstance(request_model, Mapping) or request_model.get("files") != dict(expected_model_files):
            return False, "worker_report_request_model_mismatch"
    canonical_report = _json_bytes(worker_report)
    if result.get("sha256") != _sha256_bytes(canonical_report) or result.get("bytes") != len(canonical_report):
        return False, "result_identity_mismatch"
    if not isinstance(payload.get("text"), str) or not isinstance(payload.get("native_units"), list):
        return False, "transcript_body_invalid"
    if expected_input is not None:
        input_metadata = payload.get("input")
        if not isinstance(input_metadata, Mapping) or input_metadata.get("sha256") != expected_input.get("sha256"):
            return False, "transcript_input_mismatch"
    if expected_model_files is not None:
        model_metadata = payload.get("model")
        if not isinstance(model_metadata, Mapping) or model_metadata.get("files") != dict(expected_model_files):
            return False, "transcript_model_mismatch"
    return True, "ok"


def _build_transcript(report: Mapping[str, Any], raw_result: bytes, audio: Mapping[str, Any], model: Mapping[str, Any],
                      backend: str, runtime: Mapping[str, Any], adapter: Mapping[str, Any]) -> dict[str, Any]:
    if report.get("schema_version") != WORKER_RESULT_SCHEMA_VERSION or report.get("status") != "completed":
        raise RuntimeError(str(report.get("error") or "local ASR worker failed"))
    if report.get("identity_checks") != {"before": True, "after": True}:
        raise RuntimeError("local ASR worker identity checks did not pass")
    expected_input = {key: audio[key] for key in ("sha256", "pcm_sha256", "frame_count", "sample_rate")}
    request = report.get("request")
    if not isinstance(request, Mapping) or request.get("backend") != backend:
        raise RuntimeError("worker backend echo mismatch")
    if request.get("input") != expected_input:
        raise RuntimeError("worker input echo mismatch")
    if request.get("parameters") != _parameters(report.get("parameters"), backend):
        raise RuntimeError("worker parameter echo mismatch")
    request_model = request.get("model")
    if not isinstance(request_model, Mapping) or request_model.get("files") != model["files"]:
        raise RuntimeError("worker model echo mismatch")
    if not _runtime_contains(report.get("runtime"), runtime):
        raise RuntimeError("worker runtime probe mismatch")
    input_identity = {key: audio[key] for key in ("sha256", "pcm_sha256", "sample_rate", "channels", "sample_width", "frame_count", "duration_seconds")}
    model_identity = {"id": Path(str(model["root"])).name, "revision": model["sha256"], "sha256": model["sha256"], "files": model["files"]}
    return {
        "schema_version": SCHEMA_VERSION, "status": "completed", "backend": backend,
        "text": report.get("text", ""), "native_units": report.get("native_units", []),
        "native_segments": report.get("native_segments", []), "timestamp_kind": report.get("timestamp_kind", "unavailable"),
        "input": input_identity, "model": model_identity, "runtime": report.get("runtime", runtime),
        "parameters": dict(report.get("parameters") or {}),
        "worker_report": dict(report),
        "identity": {"input": {"sha256": audio["sha256"], "verified": True},
                      "result": {"sha256": _sha256_bytes(_json_bytes(report)), "bytes": len(_json_bytes(report)), "verified": True},
                      "model": {"id": model_identity["id"], "revision": model_identity["revision"], "sha256": model_identity["sha256"], "verified": True},
                      "provider": {"name": backend, "version": _provider_version(backend, runtime), "verified": True},
                      "adapter": {**adapter, "verified": True}},
        "production_eligible": False, "acoustic_accepted": False, "neighbor_protection_verified": False,
        "source_media_modified": False, "production_integrated": False,
    }


def transcribe_local(audio_path: str | os.PathLike[str], model_path: str | os.PathLike[str], *,
                     backend: str = "faster-whisper", parameters: Mapping[str, Any] | None = None,
                     cache_dir: str | os.PathLike[str] | None = None, python_executable: str | os.PathLike[str] | None = None,
                     query: str | None = None, must_keep: tuple[str, ...] = (), timeout_seconds: float = 3600) -> dict[str, Any]:
    """Run or load one verified local transcript and optionally locate a query."""
    if backend not in {"faster-whisper", "funasr"}:
        raise ValueError("backend must be faster-whisper or funasr")
    root = Path(__file__).resolve().parents[1]
    executable = python_executable or sys.executable
    audio = audio_identity(audio_path)
    model = model_manifest(model_path)
    params = _parameters(parameters, backend)
    runtime = _runtime_probe(executable, root)
    adapter = _adapter_identity()
    identity = _cache_identity(audio, model, backend, params, runtime, adapter)
    try:
        ArtifactCache = importlib.import_module("scripts.utils.review_job_pipeline").ArtifactCache
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("ArtifactCache is unavailable in this source checkout") from exc
    cache = ArtifactCache(cache_dir or (root / "tmp" / "local-asr-cache"))
    expected = {"input_sha256": audio["sha256"], "model_sha256": model["sha256"],
                "provider_name": backend, "provider_version": _provider_version(backend, runtime),
                "adapter_sha256": adapter["sha256"]}
    cached = cache.get_json(identity, SCHEMA_VERSION)
    if cached is not None:
        valid, reason = _validate_transcript(
            cached,
            expected,
            expected_backend=backend,
            expected_parameters=params,
            expected_input={key: audio[key] for key in ("sha256", "pcm_sha256", "frame_count", "sample_rate")},
            expected_model_files=model["files"],
            expected_runtime=runtime,
        )
        if valid:
            result = {"status": "cache_hit", "cache_hit": True, "transcript": cached, "identity_digest": identity.digest()}
            if query is not None:
                result["candidates"] = locate_candidates(cached, query, must_keep=must_keep)
            return result
    request = {
        "schema_version": REQUEST_SCHEMA_VERSION, "backend": backend,
        "audio_path": audio["path"], "model_path": model["root"], "parameters": params,
        "input": {key: audio[key] for key in ("sha256", "pcm_sha256", "frame_count", "sample_rate")},
        "model": {"files": model["files"]},
    }
    with tempfile.TemporaryDirectory(prefix="local-asr-") as temporary:
        temporary_path = Path(temporary)
        request_path, output_path = temporary_path / "request.json", temporary_path / "result.json"
        request_path.write_bytes(_json_bytes(request))
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(root) + os.pathsep + environment.get("PYTHONPATH", "")
        command = [str(Path(executable).expanduser()), "-m", "audio_sound.local_asr_worker", "--request", str(request_path), "--output", str(output_path)]
        completed = subprocess.run(command, cwd=str(root), env=environment, capture_output=True, text=True,
                                   timeout=timeout_seconds, check=False)
        if not output_path.exists():
            return {"status": "failed", "cache_hit": False, "error": "worker produced no result", "stdout": completed.stdout, "stderr": completed.stderr}
        raw_result = output_path.read_bytes()
        try:
            report = json.loads(raw_result.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"status": "failed", "cache_hit": False, "error": f"invalid worker result: {exc}", "stdout": completed.stdout, "stderr": completed.stderr}
        if completed.returncode != 0 or report.get("status") != "completed":
            return {"status": "failed", "cache_hit": False, "worker": report, "stdout": completed.stdout, "stderr": completed.stderr}
        try:
            transcript = _build_transcript(report, raw_result, audio, model, backend, runtime, adapter)
        except Exception as exc:
            return {"status": "failed", "cache_hit": False, "error": str(exc), "worker": report, "stdout": completed.stdout, "stderr": completed.stderr}
    cache.store_json(identity, transcript)
    result = {"status": "completed", "cache_hit": False, "transcript": transcript, "identity_digest": identity.digest(),
              "stdout": completed.stdout, "stderr": completed.stderr}
    if query is not None:
        result["candidates"] = locate_candidates(transcript, query, must_keep=must_keep)
    return result


__all__ = ["audio_identity", "model_manifest", "transcribe_local", "SCHEMA_VERSION"]
