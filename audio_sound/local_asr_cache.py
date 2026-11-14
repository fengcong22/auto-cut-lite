"""Content-addressed cache validation for diagnostic local ASR transcripts.

This module deliberately treats a cached transcript as untrusted input.  A
payload's ``verified`` fields (or any production flag copied from a worker)
never establish cache validity; the caller must provide a freshly computed
identity and this module independently checks the artifact bytes, sidecar
manifest, identity and fixed safety flags.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import wave
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

CACHE_SCHEMA_VERSION = "local-asr-cache-v1"
TRANSCRIPT_SCHEMA_VERSION = "local-asr-transcript-v1"
MANIFEST_SCHEMA_VERSION = "local-asr-cache-manifest-v1"

_HASH_LENGTH = 64
_FALSE_FLAGS = {
    "production_eligible": False,
    "acoustic_accepted": False,
    "neighbor_protection_verified": False,
    "production_integrated": False,
    "source_media_modified": False,
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _snapshot(value: Any) -> Any:
    """Return JSON-compatible immutable-by-value data or raise clearly."""
    try:
        encoded = _canonical(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("cache identity must contain finite JSON values") from exc
    return json.loads(encoded.decode("utf-8"))


def _require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != _HASH_LENGTH or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    return value.lower()


def build_input_identity(audio_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Hash and describe a local mono PCM16 16 kHz WAV input."""
    path = Path(audio_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise IsADirectoryError(f"audio input is not a file: {path}")
    try:
        with wave.open(str(path), "rb") as stream:
            channels, width, rate, compression = (
                stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()
            )
            frames = stream.getnframes()
            if (channels, width, rate, compression) != (1, 2, 16000, "NONE"):
                raise ValueError("input must be mono PCM16 16 kHz WAV")
            pcm = stream.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise ValueError("input is not a valid PCM WAV") from exc
    if frames <= 0 or len(pcm) != frames * 2:
        raise ValueError("input PCM is empty or truncated")
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "pcm_sha256": _sha256_bytes(pcm),
        "sample_rate": rate,
        "channels": channels,
        "sample_width": width,
        "frame_count": frames,
        "duration_seconds": frames / rate,
    }


def build_model_manifest(model_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Hash every regular model file, including ``.runtime-model.json``."""
    root = Path(model_path).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("model path must be a real local directory")
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"model manifest cannot contain symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        safe = PurePosixPath(relative)
        if not relative or safe.is_absolute() or ".." in safe.parts:
            raise ValueError("unsafe model-relative path")
        data_size = path.stat().st_size
        files[relative] = {"sha256": _sha256_file(path), "bytes": data_size}
    if not files:
        raise ValueError("model directory contains no files")
    digest = _sha256_bytes(_canonical(files))
    return {"path": str(root), "files": files, "files_sha256": digest}


def build_cache_identity(
    input_identity: Mapping[str, Any],
    model_manifest: Mapping[str, Any],
    *,
    provider: str,
    adapter: str | Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the complete content-address identity used for one transcript."""
    if not isinstance(input_identity, Mapping) or not isinstance(model_manifest, Mapping):
        raise TypeError("input_identity and model_manifest must be mappings")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be non-empty text")
    if not isinstance(parameters, Mapping):
        raise TypeError("parameters must be a mapping")
    # Ensure required identity fields are present and syntactically sound.
    _require_hash(input_identity.get("sha256"), "input sha256")
    _require_hash(input_identity.get("pcm_sha256"), "input pcm_sha256")
    files = model_manifest.get("files")
    if not isinstance(files, Mapping) or not files:
        raise ValueError("model manifest files are required")
    identity = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "input": _snapshot(input_identity),
        "model": _snapshot(model_manifest),
        "provider": provider,
        "adapter": _snapshot(adapter),
        "parameters": _snapshot(parameters),
    }
    return identity


def cache_identity_digest(identity: Mapping[str, Any]) -> str:
    if not isinstance(identity, Mapping):
        raise TypeError("identity must be a mapping")
    return _sha256_bytes(_canonical(identity))


def _manifest_path(cache_path: Path) -> Path:
    return cache_path.with_name(cache_path.name + ".manifest.json")


def _safe_flags(payload: Mapping[str, Any]) -> list[str]:
    return [key for key, expected in _FALSE_FLAGS.items() if payload.get(key) is not expected]


def _contains_self_verified(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("verified") is True:
            return True
        return any(_contains_self_verified(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_self_verified(item) for item in value)
    return False


def validate_cached_transcript(cache_path: str | os.PathLike[str], expected_identity: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one cache entry against freshly computed inputs.

    The result is a diagnostic report.  ``valid`` is true only after all
    independent checks pass; malformed or changed entries are never raised as
    provider errors and can safely be recomputed by the caller.
    """
    if not isinstance(expected_identity, Mapping):
        raise TypeError("expected_identity must be a mapping")
    path = Path(cache_path).expanduser().resolve(strict=False)
    issues: list[str] = []
    payload: Any = None
    manifest: Any = None
    if path.is_symlink() or not path.is_file():
        return {"valid": False, "status": "invalid", "issues": ["cache_artifact_not_regular_file"]}
    # Refresh filesystem-bound identities independently.  This catches a
    # changed source/model even when a caller accidentally reuses an old
    # identity object; synthetic identities without paths remain supported for
    # unit-level callers and are still checked against the sidecar.

    expected_input = expected_identity.get("input")
    if isinstance(expected_input, Mapping) and isinstance(expected_input.get("path"), str):
        try:
            current_input = build_input_identity(expected_input["path"])
            if dict(current_input) != dict(expected_input):
                issues.append("source_identity_changed")
        except (OSError, ValueError):
            issues.append("source_identity_unavailable")
    expected_model = expected_identity.get("model")
    if isinstance(expected_model, Mapping) and isinstance(expected_model.get("path"), str):
        try:
            current_model = build_model_manifest(expected_model["path"])
            if dict(current_model) != dict(expected_model):
                issues.append("model_identity_changed")
        except (OSError, ValueError):
            issues.append("model_identity_unavailable")
    try:
        payload_bytes = path.read_bytes()
    except OSError as exc:
        return {"valid": False, "status": "miss", "issues": ["cache_missing"], "error": str(exc)}
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        issues.append("cache_json_invalid")
    sidecar = _manifest_path(path)
    if sidecar.is_symlink():
        issues.append("cache_manifest_symlink")
    try:
        manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        issues.append("cache_manifest_invalid")
    if isinstance(manifest, Mapping):
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            issues.append("cache_manifest_schema_mismatch")
        if manifest.get("artifact") != path.name:
            issues.append("cache_manifest_artifact_mismatch")
        if manifest.get("identity_digest") != cache_identity_digest(expected_identity):
            issues.append("cache_identity_digest_mismatch")
        if manifest.get("size") != len(payload_bytes):
            issues.append("cache_size_mismatch")
        try:
            if _require_hash(manifest.get("sha256"), "cache manifest sha256") != _sha256_bytes(payload_bytes):
                issues.append("cache_bytes_hash_mismatch")
        except ValueError:
            issues.append("cache_manifest_hash_invalid")
    if not isinstance(payload, Mapping):
        issues.append("cache_payload_not_object")
    else:
        if payload.get("schema_version") != TRANSCRIPT_SCHEMA_VERSION:
            issues.append("transcript_schema_mismatch")
        if payload.get("identity") != dict(expected_identity):
            issues.append("transcript_identity_mismatch")
        if _contains_self_verified(payload):
            issues.append("self_reported_verified_flag")
        issues.extend(f"unsafe_flag:{key}" for key in _safe_flags(payload))
    valid = not issues
    return {
        "valid": valid,
        "status": "hit" if valid else "invalid",
        "issues": issues,
        "payload": dict(payload) if valid and isinstance(payload, Mapping) else None,
        "manifest": dict(manifest) if isinstance(manifest, Mapping) else None,
        "identity_digest": cache_identity_digest(expected_identity),
    }


def read_transcript_cache(cache_path: str | os.PathLike[str], expected_identity: Mapping[str, Any]) -> dict[str, Any] | None:
    report = validate_cached_transcript(cache_path, expected_identity)
    return report["payload"] if report["valid"] else None


def write_transcript_cache(
    cache_path: str | os.PathLike[str], transcript: Mapping[str, Any], identity: Mapping[str, Any]
) -> Path:
    """Atomically write a transcript and independently generated sidecar."""
    if not isinstance(transcript, Mapping):
        raise TypeError("transcript must be a mapping")
    if transcript.get("schema_version") != TRANSCRIPT_SCHEMA_VERSION:
        raise ValueError("transcript schema_version is unsupported")
    if transcript.get("identity") != dict(identity):
        raise ValueError("transcript identity does not match cache identity")
    unsafe = _safe_flags(transcript)
    if unsafe:
        raise ValueError(f"transcript contains unsafe flags: {', '.join(unsafe)}")
    if _contains_self_verified(transcript):
        raise ValueError("transcript contains self-reported verified flag")
    path = Path(cache_path).expanduser().resolve(strict=False)
    atomic_write_bytes = importlib.import_module("scripts.utils.atomic_io").atomic_write_bytes
    payload_bytes = _canonical(dict(transcript))
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact": path.name,
        "identity_digest": cache_identity_digest(identity),
        "size": len(payload_bytes),
        "sha256": _sha256_bytes(payload_bytes),
    }
    atomic_write_bytes(path, payload_bytes + b"\n")
    # Manifest size/hash describe exactly the bytes on disk, including newline.
    stored = path.read_bytes()
    manifest["size"] = len(stored)
    manifest["sha256"] = _sha256_bytes(stored)
    atomic_write_bytes(_manifest_path(path), _canonical(manifest) + b"\n")
    return path


# Short aliases for callers that use cache-oriented naming.
read_cache = read_transcript_cache
write_cache = write_transcript_cache


__all__ = [
    "CACHE_SCHEMA_VERSION", "TRANSCRIPT_SCHEMA_VERSION", "MANIFEST_SCHEMA_VERSION",
    "build_input_identity", "build_model_manifest", "build_cache_identity",
    "cache_identity_digest", "validate_cached_transcript", "read_transcript_cache",
    "write_transcript_cache", "read_cache", "write_cache",
]
