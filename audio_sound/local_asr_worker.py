"""Isolated, offline local transcription worker; never grants edit permission.

Run this file in the selected audio interpreter. The probe uses distribution
metadata only, so the coordinator does not need to import any model runtime.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.abc
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
import sysconfig
import time
import wave
from collections.abc import Mapping
from contextlib import redirect_stdout
from pathlib import Path, PurePosixPath
from typing import Any

WORKER_VERSION = "local-asr-worker-v1"
_HASH = re.compile(r"^[a-fA-F0-9]{64}$")
_DLL_HANDLES: list[Any] = []
_PACKAGES = (
    "faster-whisper", "ctranslate2", "funasr", "torch", "torchaudio", "numpy",
    "transformers", "tokenizers", "huggingface-hub", "modelscope", "soundfile",
    "librosa", "scipy", "onnxruntime", "av", "sentencepiece",
)
_PARAMETERS = {
    "language", "device", "device_index", "compute_type", "cpu_threads", "chunk_seconds"
}
_REQUEST_KEYS = {
    "schema_version", "backend", "audio_path", "model_path", "parameters", "input", "model"
}


def json_safe(value: Any) -> Any:
    """Preserve nonfinite/unknown native values as explicit diagnostic records."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_float": repr(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_safe(dataclasses.asdict(value))
    if hasattr(value, "_asdict"):
        return json_safe(value._asdict())
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return json_safe(value.tolist())
    return {"non_json_type": type(value).__name__, "representation": repr(value)}


def runtime_identity() -> dict[str, Any]:
    versions: dict[str, str | None] = {}
    for name in _PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "worker_version": WORKER_VERSION,
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "package_versions": versions,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class OfflineGuard:
    """Python socket audit guard, not a host or native-library firewall."""

    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []
        self.allowed_local_binds: list[dict[str, Any]] = []

    @staticmethod
    def _is_ephemeral_loopback_bind(arguments: tuple[Any, ...]) -> bool:
        """Allow libraries to reserve a local-only ephemeral port.

        Some CPU inference runtimes create a loopback listener for internal
        coordination.  Binding ``::1``/``127.0.0.1`` to port zero does not
        contact an external host, so it remains compatible with the worker's
        offline contract.  Any other bind is still blocked and recorded.
        """
        if len(arguments) < 2 or not isinstance(arguments[1], tuple) or len(arguments[1]) < 2:
            return False
        host, port = arguments[1][0], arguments[1][1]
        return host in {"::1", "127.0.0.1", "localhost"} and port == 0

    def audit(self, event: str, arguments: tuple[Any, ...]) -> None:
        if event == "socket.bind" and self._is_ephemeral_loopback_bind(arguments):
            self.allowed_local_binds.append({"event": event, "arguments": repr(arguments),
                                             "blocked": False, "reason": "ephemeral_loopback"})
            return
        if event in {"socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
                     "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                     "socket.getnameinfo"}:
            self.attempts.append({"event": event, "arguments": repr(arguments), "blocked": True})
            raise PermissionError("Local ASR worker blocks Python network operations")

    def install(self) -> None:
        os.environ.update({
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1", "FUNASR_DISABLE_UPDATE": "1",
        })
        sys.addaudithook(self.audit)


def validate_request(request: Any) -> None:
    _require(isinstance(request, dict), "Request must be an object")
    _require(set(request) == _REQUEST_KEYS, "Request contains unknown fields")
    _require(request.get("schema_version") == "local-asr-worker-request-v1", "Unknown request schema")
    _require(request.get("backend") in {"faster-whisper", "funasr"}, "Unsupported backend")
    parameters = request.get("parameters")
    _require(isinstance(parameters, dict) and set(parameters) == _PARAMETERS,
             "Parameters must be exactly language/device/device_index/compute_type/cpu_threads/chunk_seconds; prompts are forbidden")
    _require(parameters["language"] in {"zh", "en", None}, "Unsupported language")
    _require(parameters["device"] in {"cuda", "cpu"}, "Explicit cuda or cpu required")
    _require(type(parameters["device_index"]) is int and parameters["device_index"] >= 0,
             "device_index must be a nonnegative integer")
    _require(type(parameters["cpu_threads"]) is int and parameters["cpu_threads"] > 0,
             "cpu_threads must be a positive integer")
    seconds = parameters["chunk_seconds"]
    _require(type(seconds) in {float, int} and math.isfinite(seconds) and 0 < seconds <= 18,
             "chunk_seconds must be finite and within (0, 18]")
    _require(seconds * 16000 >= 1, "chunk_seconds is shorter than one audio frame")
    _require(parameters["compute_type"] in {"int8_float16", "float32"}, "Unsupported compute type")
    if request["backend"] == "funasr":
        _require(parameters["compute_type"] == "float32", "Paraformer requires float32")
        _require(parameters["language"] == "zh", "This Paraformer worker requires language zh")
    for name in ("audio_path", "model_path"):
        _require(isinstance(request.get(name), str) and Path(request[name]).is_absolute(),
                 f"{name} must be an absolute local path")
    identity = request.get("input")
    _require(isinstance(identity, dict), "Input identity required")
    _require(set(identity) == {"sha256", "pcm_sha256", "frame_count", "sample_rate"},
             "Input identity contains unknown fields")
    for name in ("sha256", "pcm_sha256"):
        _require(isinstance(identity.get(name), str) and _HASH.fullmatch(identity[name]) is not None,
                 f"Invalid input {name}")
    _require(type(identity.get("sample_rate")) is int and identity["sample_rate"] == 16000,
             "Input sample rate must be 16000")
    _require(type(identity.get("frame_count")) is int and identity["frame_count"] > 0,
             "Positive frame_count required")
    model = request.get("model")
    _require(isinstance(model, dict) and isinstance(model.get("files"), dict) and bool(model["files"]),
             "Nonempty model file manifest required")
    _require(set(model) == {"files"}, "Model identity contains unknown fields")


def verify_input(request: dict[str, Any]) -> bytes:
    path, expected = Path(request["audio_path"]), request["input"]
    _require(_sha256(path) == expected["sha256"].lower(), "Input file SHA-256 mismatch")
    with wave.open(str(path), "rb") as stream:
        _require((stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype())
                 == (1, 2, 16000, "NONE"), "Input must be mono PCM16 16 kHz WAV")
        _require(stream.getnframes() == expected["frame_count"], "Input frame count mismatch")
        pcm = stream.readframes(stream.getnframes())
    _require(len(pcm) == expected["frame_count"] * 2, "Truncated PCM input")
    _require(hashlib.sha256(pcm).hexdigest() == expected["pcm_sha256"].lower(), "Input PCM SHA-256 mismatch")
    return pcm


def verify_model(request: dict[str, Any]) -> None:
    root = Path(request["model_path"]).resolve(strict=True)
    _require(root.is_dir(), "Model path must be a local directory")
    expected = request["model"]["files"]
    names: set[str] = set()
    for name, binding in expected.items():
        _require(isinstance(name, str) and "\\" not in name and ":" not in name,
                 "Model manifest paths must use relative POSIX paths")
        relative = PurePosixPath(name)
        _require(not relative.is_absolute() and name == relative.as_posix()
                 and ".." not in relative.parts and name not in {"", "."}, "Unsafe model path")
        path = (root / name).resolve(strict=True)
        _require(path.is_relative_to(root) and path.is_file(), "Model file escapes local model directory")
        _require(isinstance(binding, dict) and isinstance(binding.get("sha256"), str)
                 and _HASH.fullmatch(binding["sha256"]) is not None, "Invalid model SHA-256")
        _require(type(binding.get("bytes")) is int and binding["bytes"] >= 0, "Invalid model byte count")
        _require(path.stat().st_size == binding["bytes"] and _sha256(path) == binding["sha256"].lower(),
                 f"Model identity mismatch: {name}")
        names.add(name)
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    _require(actual == names, "Model directory has extra or missing files")


class _NoTorchConversion(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname == "torch" or fullname.startswith("torch."):
            raise ImportError("Torch conversion is disabled in this CTranslate2-only worker")
        return None


def initialize_ct2_only(runtime: dict[str, Any], device: str) -> None:
    _require("torch" not in sys.modules, "Torch is already loaded in the CTranslate2 worker")
    _require(not os.environ.get("KMP_DUPLICATE_LIB_OK"), "KMP_DUPLICATE_LIB_OK is forbidden")
    sys.meta_path.insert(0, _NoTorchConversion())
    runtime["torch_conversion_imports_disabled"] = True
    if os.name == "nt" and device == "cuda":
        dll_path = Path(sysconfig.get_path("purelib")) / "torch" / "lib"
        _require(dll_path.is_dir(), "Isolated CUDA DLL directory is missing")
        _DLL_HANDLES.append(os.add_dll_directory(str(dll_path)))
        os.environ["PATH"] = str(dll_path) + os.pathsep + os.environ.get("PATH", "")
        runtime["process_local_cuda_dll_directory"] = str(dll_path)


def _mapping(value: Any) -> dict[str, Any]:
    safe = json_safe(value)
    return safe if isinstance(safe, dict) else {"malformed_native_row": safe}


def _trusted_request_echo(request: Any) -> Any:
    """Echo only the schema-shaped request; never reflect unvalidated flags."""
    if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
        return {"omitted": "request_failed_schema_shape_validation"}
    parameters = request.get("parameters")
    identity = request.get("input")
    model = request.get("model")
    if (not isinstance(parameters, dict) or set(parameters) != _PARAMETERS
            or not isinstance(identity, dict)
            or set(identity) != {"sha256", "pcm_sha256", "frame_count", "sample_rate"}
            or not isinstance(model, dict) or set(model) != {"files"}):
        return {"omitted": "request_failed_identity_shape_validation"}
    return json_safe(request)


def run_whisper(request: dict[str, Any], pcm: bytes, report: dict[str, Any]) -> None:
    parameters, runtime = request["parameters"], report["runtime"]
    initialize_ct2_only(runtime, parameters["device"])
    import ctranslate2
    import numpy as np
    from faster_whisper import WhisperModel

    supported = sorted(ctranslate2.get_supported_compute_types(parameters["device"], parameters["device_index"]))
    runtime["supported_compute_types"] = supported
    _require(parameters["compute_type"] in supported, "Requested compute type unavailable; fallback forbidden")
    if parameters["device"] == "cuda":
        _require(ctranslate2.get_cuda_device_count() > parameters["device_index"], "Requested CUDA device unavailable")
    _progress("Loading verified local faster-whisper model")
    model = WhisperModel(request["model_path"], device=parameters["device"],
                         device_index=parameters["device_index"], compute_type=parameters["compute_type"],
                         cpu_threads=parameters["cpu_threads"], num_workers=1, local_files_only=True)
    actual_indices = json_safe(model.model.device_index)
    runtime.update(actual_device=str(model.model.device), actual_compute_type=str(model.model.compute_type),
                   actual_device_index=actual_indices)
    _require(runtime["actual_device"] == parameters["device"], "Model device fallback forbidden")
    _require(runtime["actual_compute_type"] == parameters["compute_type"], "Model compute fallback forbidden")
    if parameters["device"] == "cuda":
        _require(actual_indices in (parameters["device_index"], [parameters["device_index"]]), "GPU index mismatch")
    options = {"language": parameters["language"], "task": "transcribe", "word_timestamps": True,
               "vad_filter": False, "beam_size": 5, "temperature": 0.0,
               "condition_on_previous_text": False, "initial_prompt": None, "prefix": None,
               "hotwords": None}
    report["effective_decoding_options"] = options
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    segments, info = model.transcribe(audio, **options)
    report["transcription_info"] = json_safe(info)
    for segment_index, segment in enumerate(segments):
        row = _mapping(segment)
        report["native_segments"].append(row)
        if isinstance(row.get("text"), str):
            report["text"] += row["text"]
        words = row.get("words")
        words = [] if words is None else words if isinstance(words, list) else [words]
        for word_index, word in enumerate(words):
            native = _mapping(word)
            report["native_units"].append({
                "native_index": len(report["native_units"]), "text": native.get("word"),
                "start": native.get("start"), "end": native.get("end"),
                "probability": native.get("probability"), "segment_index": segment_index,
                "word_index": word_index, "native_row": native,
            })
        _progress(f"Whisper segment {segment_index}: {row.get('start')}–{row.get('end')} seconds")
    report["timestamp_kind"] = "native_unit_seconds" if report["native_units"] else "unavailable"


def _native_seconds(value: Any, offset: float) -> Any:
    if type(value) not in {int, float} or not math.isfinite(value):
        return {"invalid_native_milliseconds": json_safe(value)}
    return json_safe(offset + value / 1000.0)


def run_funasr(request: dict[str, Any], pcm: bytes, report: dict[str, Any]) -> None:
    import numpy as np
    import torch
    from funasr import AutoModel

    parameters, runtime = request["parameters"], report["runtime"]
    device = f"cuda:{parameters['device_index']}" if parameters["device"] == "cuda" else "cpu"
    if parameters["device"] == "cuda":
        _require(torch.cuda.is_available() and torch.cuda.device_count() > parameters["device_index"],
                 "Requested CUDA device unavailable; CPU fallback forbidden")
    _progress("Loading verified local Paraformer model; no VAD or punctuation model")
    model = AutoModel(model=request["model_path"], device=device, disable_update=True,
                      trust_remote_code=False, disable_pbar=True, local_files_only=True,
                      ncpu=parameters["cpu_threads"], vad_model=None, punc_model=None, spk_model=None)
    parameter = next(model.model.parameters())
    runtime.update(actual_device=str(parameter.device), actual_compute_type=str(parameter.dtype))
    _require(runtime["actual_device"] == device, "Model device fallback forbidden")
    _require(runtime["actual_compute_type"] == "torch.float32", "Model compute fallback forbidden")
    report["effective_decoding_options"] = {
        "language": "zh", "batch_size": 1, "sentence_timestamp": False,
        "vad_model": None, "punc_model": None, "spk_model": None,
        "chunk_policy": "fixed_nonoverlapping_pcm_frame_ranges",
        "initial_prompt": None, "hotwords": None,
    }
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    chunk_frames = int(parameters["chunk_seconds"] * 16000)
    for chunk_index, first in enumerate(range(0, len(audio), chunk_frames)):
        end = min(len(audio), first + chunk_frames)
        raw = model.generate(input=audio[first:end].copy(), fs=16000, language="zh", batch_size=1,
                             cache={}, disable_pbar=True, sentence_timestamp=False)
        if parameters["device"] == "cuda":
            torch.cuda.synchronize(parameters["device_index"])
        safe = json_safe(raw)
        results = safe if isinstance(safe, list) else [safe]
        text = "".join(row.get("text", "") for row in results
                       if isinstance(row, dict) and isinstance(row.get("text"), str))
        report["native_segments"].append({
            "chunk_index": chunk_index, "source_start_frame": first, "source_end_frame": end,
            "source_start_seconds": first / 16000, "source_end_seconds": end / 16000,
            "range_basis": "input_chunk_not_speech_boundary", "text": text, "native_result": safe,
        })
        report["text"] += text
        for result_index, result in enumerate(results):
            if not isinstance(result, dict) or result.get("timestamp") is None:
                continue
            timestamps = result["timestamp"]
            timestamps = timestamps if isinstance(timestamps, list) else [timestamps]
            tokens = result.get("tokens", result.get("token"))
            mapped = isinstance(tokens, list) and len(tokens) == len(timestamps)
            for token_index, pair in enumerate(timestamps):
                valid_pair = isinstance(pair, list) and len(pair) == 2
                report["native_units"].append({
                    "native_index": len(report["native_units"]),
                    "text": tokens[token_index] if mapped else None,
                    "start": _native_seconds(pair[0], first / 16000) if valid_pair else None,
                    "end": _native_seconds(pair[1], first / 16000) if valid_pair else None,
                    "chunk_index": chunk_index, "result_index": result_index, "token_index": token_index,
                    "native_pair_ms": pair,
                    "token_mapping": "native_tokens" if mapped else "unavailable_no_synthetic_mapping",
                })
        _progress(f"Paraformer chunk {chunk_index}: {first / 16000:.3f}–{end / 16000:.3f} seconds")
    # Base Paraformer/FunASR has unverified timestamp semantics; preserve any
    # unexpected timestamp rows but do not advertise usable word timing.
    report["timestamp_kind"] = "unavailable"
    report["limitations"].append("Paraformer chunk ranges are input context, not word boundaries; native timestamp semantics remain unverified.")


def execute_request(request: Any, guard: OfflineGuard) -> dict[str, Any]:
    started = time.perf_counter()
    report: dict[str, Any] = {
        "schema_version": "local-asr-worker-result-v1", "status": "failed",
        "backend": request.get("backend") if isinstance(request, dict) else None,
        "request": _trusted_request_echo(request),
        "parameters": json_safe(request.get("parameters")) if isinstance(request, dict) and isinstance(request.get("parameters"), dict) and set(request["parameters"]) == _PARAMETERS else None,
        "input": json_safe(request.get("input")) if isinstance(request, dict) and isinstance(request.get("input"), dict) and set(request["input"]) == {"sha256", "pcm_sha256", "frame_count", "sample_rate"} else None,
        "model": json_safe(request.get("model")) if isinstance(request, dict) and isinstance(request.get("model"), dict) and set(request["model"]) == {"files"} else None,
        "text": "", "native_units": [], "native_segments": [], "timestamp_kind": "unavailable",
        "runtime": runtime_identity(), "network": {"guard": "python_socket_audit_not_host_firewall",
                                                     "blocked_attempts": guard.attempts,
                                                     "allowed_local_binds": guard.allowed_local_binds},
        "production_eligible": False, "acoustic_accepted": False, "neighbor_protection_verified": False,
        "source_media_modified": False, "production_integrated": False, "limitations": [],
        "identity_checks": {"before": False, "after": False},
    }
    validated = False
    try:
        validate_request(request)
        validated = True
        pcm = verify_input(request)
        verify_model(request)
        report["identity_checks"]["before"] = True
        with redirect_stdout(sys.stderr):
            runner = run_whisper if request["backend"] == "faster-whisper" else run_funasr
            runner(request, pcm, report)
        _require(not guard.attempts, "A network attempt was blocked; execution cannot be accepted")
        report["status"] = "completed"
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if validated:
            try:
                verify_input(request)
                verify_model(request)
                report["identity_checks"]["after"] = True
            except Exception as exc:
                report["status"] = "failed"
                report["post_verification_error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["elapsed_seconds"] = time.perf_counter() - started
    return json_safe(report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--request")
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.probe:
        _require(not args.request and not args.output, "--probe cannot run inference")
        print(json.dumps(runtime_identity(), ensure_ascii=False, allow_nan=False))
        return 0
    if not args.request or not args.output:
        parser.error("--request and --output are required for inference")
    output = Path(args.output).resolve()
    _require(not output.exists(), "Worker output already exists; refusing overwrite")
    request_error = None
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        request = None
        request_error = {"type": type(exc).__name__, "message": str(exc)}
    _require(output != Path(args.request).resolve(), "Output cannot overwrite request")
    if isinstance(request, dict):
        if isinstance(request.get("audio_path"), str):
            _require(output != Path(request["audio_path"]).resolve(), "Output cannot overwrite input")
        if isinstance(request.get("model_path"), str):
            _require(not output.is_relative_to(Path(request["model_path"]).resolve()), "Output cannot modify model")
    guard = OfflineGuard()
    guard.install()
    report = execute_request(request, guard)
    if request_error is not None:
        report["error"] = request_error
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"status": report["status"], "output": str(output), "production_eligible": False}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
