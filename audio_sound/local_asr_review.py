"""Import saved human listening references without authorizing ASR cuts.

This verifies bytes and frame arithmetic, not what a human heard. Confirmation
must originate in the user's chat; a model or browser QA run cannot supply it.
Historical model evidence is checked against its saved worker output, without
claiming that the currently installed runtime/model has been validated.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import wave
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from audio_sound.local_asr_candidates import (
    _canonical_json_bytes, _require_frame, _require_number, _require_sha256,
    _verified_identity, locate_candidates,
)

SCHEMA_VERSION = "local-asr-human-review-receipt-v1"
FALSE_FLAGS = {key: False for key in (
    "production_eligible", "acoustic_accepted", "neighbor_protection_verified",
    "source_media_modified", "production_integrated", "authoritative_cut_boundary",
    "execution_required",
)}
_PRODUCTION_FLAGS = tuple(FALSE_FLAGS)[:5]
_ASSESSMENTS = ("target_onset_F", "target_final_s", "preceding_word", "following_word_or_quote")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _mapping(value: Any, label: str) -> Mapping:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    return value


def _json(data: bytes) -> dict:
    value = json.loads(data)
    _mapping(value, "JSON artifact")
    _canonical_json_bytes(value)  # Reject nonfinite numbers before comparing.
    return value


def _closed(value: Mapping, *, required: bool = False) -> None:
    for key in FALSE_FLAGS:
        if key in value or (required and key in _PRODUCTION_FLAGS):
            _require(value.get(key) is False, f"{key} must be false")


class _Files:
    def __init__(self) -> None:
        self.bindings: dict[str, dict] = {}

    def read(self, path: Path, expected: Mapping | None = None) -> tuple[bytes, dict]:
        path = path.resolve(strict=True)
        data = path.read_bytes()
        binding = {"path": str(path), "file_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        if expected is not None:
            digest = _require_sha256(expected.get("file_sha256"), "binding.file_sha256").lower()
            _require(digest == binding["file_sha256"], "bound file hash mismatch")
            if "bytes" in expected:
                _require(_require_frame(expected["bytes"], "binding.bytes") == len(data), "bound file size mismatch")
        key = str(path)
        _require(key not in self.bindings or self.bindings[key] == binding, "file changed during import")
        self.bindings[key] = binding
        return data, binding

    def bound(self, value: Any, base: Path) -> tuple[bytes, dict]:
        value = _mapping(value, "file binding")
        _require(isinstance(value.get("path"), str) and bool(value["path"]), "binding path missing")
        path = Path(value["path"])
        return self.read(path if path.is_absolute() else base / path, value)

    def check_unchanged(self) -> None:
        for name, binding in self.bindings.items():
            data = Path(name).read_bytes()
            _require(len(data) == binding["bytes"] and hashlib.sha256(data).hexdigest() == binding["file_sha256"],
                     "bound file changed during import")


def _wav(data: bytes, binding: Mapping) -> tuple[dict, bytes]:
    try:
        with wave.open(io.BytesIO(data), "rb") as stream:
            _require((stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype())
                     == (1, 2, 16000, "NONE"), "audio must be mono PCM16 16 kHz")
            frames = stream.getnframes()
            pcm = stream.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise ValueError("invalid WAV") from exc
    _require(frames > 0 and len(pcm) == frames * 2, "audio is empty or truncated")
    metadata = {"sample_rate": 16000, "channels": 1, "sample_width_bytes": 2,
                "frame_count": frames, "pcm16_sha256": hashlib.sha256(pcm).hexdigest()}
    for key, actual in metadata.items():
        if isinstance(actual, int):
            _require(_require_frame(binding.get(key), key) == actual, f"WAV {key} mismatch")
        else:
            _require(binding.get(key) == actual, f"WAV {key} mismatch")
    return metadata, pcm


def _frames(value: Any, label: str) -> list[int]:
    _require(isinstance(value, list) and len(value) == 2, f"{label} requires two frames")
    start, end = (_require_frame(v, label) for v in value)
    _require(start < end, f"{label} must be increasing")
    return [start, end]


def _range(frames: list[int], context_start: int) -> dict:
    return {"source_frames": frames, "context_relative_frames": [v - context_start for v in frames],
            "seconds": dict(zip(("start", "end"), (v / 16000 for v in frames))), "sample_rate": 16000}


def _model_evidence(manifest: Mapping, base: Path, files: _Files, source: Mapping, candidate: Mapping) -> dict:
    if "model_evidence" not in manifest:
        return {"status": "not_supplied", "identity_verified_against_installed_runtime": False}
    evidence = _mapping(manifest["model_evidence"], "model_evidence")
    data, binding = files.bound({"path": evidence.get("result_path"),
                                "file_sha256": evidence.get("result_sha256")}, base)
    artifact = _json(data)
    transcript = _mapping(artifact.get("transcript", artifact), "transcript")
    identity = _verified_identity(transcript)
    _closed(transcript, required=True)
    worker = _mapping(transcript.get("worker_report"), "worker_report")
    _closed(worker, required=True)
    _require(worker.get("schema_version") == "local-asr-worker-result-v1", "unsupported worker schema")
    checks = _mapping(worker.get("identity_checks"), "worker identity checks")
    _require(checks.get("before") is True and checks.get("after") is True, "worker identity checks failed")
    _require(transcript.get("status") == worker.get("status") == "completed", "incomplete model evidence")
    canonical = _canonical_json_bytes(worker)
    _require(identity["result"]["sha256"] == hashlib.sha256(canonical).hexdigest()
             and identity["result"].get("bytes") == len(canonical), "worker result identity mismatch")
    for key in ("text", "native_units", "native_segments", "timestamp_kind", "backend", "parameters"):
        _require(transcript.get(key) == worker.get(key), f"transcript/worker {key} mismatch")
    _require(identity["input"]["sha256"] == source["file_sha256"] == evidence.get("input_sha256"),
             "model input does not match reviewed source")
    _require(identity["provider"]["name"] == evidence.get("backend")
             and identity["model"]["id"] == evidence.get("model_id"), "manifest model identity mismatch")
    request = _mapping(worker.get("request"), "worker request")
    for key in ("backend", "parameters", "input", "model"):
        _require(worker.get(key) == request.get(key), f"worker request {key} mismatch")
    runtime = _mapping(worker.get("runtime"), "worker runtime")
    _require(transcript.get("runtime") == runtime, "transcript/worker runtime mismatch")
    versions = _mapping(runtime.get("package_versions"), "runtime package versions")
    _require(versions.get(worker["backend"]) == identity["provider"]["version"], "provider version mismatch")
    model_files = _mapping(_mapping(worker.get("model"), "worker model").get("files"), "model files")
    _require(bool(model_files), "model file inventory is empty")
    for row in model_files.values():
        row = _mapping(row, "model file")
        _require_sha256(row.get("sha256"), "model file hash")
        _require_frame(row.get("bytes"), "model file bytes")
    model_hash = hashlib.sha256(_canonical_json_bytes(model_files)).hexdigest()
    _require(identity["model"]["sha256"] == identity["model"]["revision"] == model_hash,
             "model inventory identity mismatch")
    model = _mapping(transcript.get("model"), "transcript model")
    _require(model.get("files") == model_files and model.get("sha256") == model_hash
             and model.get("revision") == model_hash and model.get("id") == identity["model"]["id"],
             "transcript model identity mismatch")
    for key, expected in (("sha256", source["file_sha256"]), ("pcm_sha256", source["pcm16_sha256"]),
                          ("frame_count", source["frame_count"]), ("sample_rate", 16000)):
        _require(transcript["input"].get(key) == expected and request.get("input", {}).get(key) == expected,
                 f"model input {key} mismatch")
    report = locate_candidates(transcript, candidate["target"])
    _require(report["status"] == "unique_text_support", "model candidate is no longer unique")
    row = report["candidates"][0]
    window = row.get("native_window")
    _require(isinstance(window, Mapping) and not row["issues"], "model native boundary is structurally invalid")
    _require([round(window[key] * 16000) for key in ("start", "end")]
             == [candidate["source_start_frame"], candidate["source_end_frame"]], "model candidate frame mismatch")
    return {"status": "saved_worker_evidence_verified", "result_binding": binding, "identity": identity,
            "native_indices": row["native_indices"], "identity_verified_against_installed_runtime": False}


def import_refined_feedback(feedback_path: str | Path) -> dict:
    """Verify a saved, chat-confirmed reference. Does not write any files."""
    files = _Files()
    data, feedback_binding = files.read(Path(feedback_path))
    feedback = _json(data)
    base = Path(feedback_binding["path"]).parent
    _require(feedback.get("schema_version") == "local-asr-refined-boundary-feedback-v1", "unsupported feedback schema")
    _require(feedback.get("evidence_kind") == "human_refined_boundary_feedback"
             and feedback.get("source") == "user_reply_in_originating_codex_chat", "feedback must originate in user chat")
    _require(feedback.get("human_confirmation_in_chat") is True
             and feedback.get("user_judgment") == "refined_boundary_suitable", "refined human confirmation missing")
    _closed(feedback, required=True)
    for key in ("manifest_binding", "source_audio_binding", "context_audio_binding"):
        _require_frame(_mapping(feedback.get(key), key).get("bytes"), f"{key}.bytes")
    assessments = _mapping(feedback.get("assessments"), "assessments")
    for key in _ASSESSMENTS:
        _require(_mapping(assessments.get(key), key).get("judgment") == "complete", f"{key} not confirmed complete")
    manifest_data, manifest_binding = files.bound(feedback.get("manifest_binding"), base)
    manifest = _json(manifest_data)
    _require(manifest.get("schema_version") == "local-asr-candidate-review-v1", "unsupported manifest schema")
    _require(feedback["manifest_binding"].get("schema_version") == manifest["schema_version"], "manifest schema binding mismatch")
    _closed(manifest)
    candidate = _mapping(manifest.get("candidate"), "candidate")
    _closed(candidate)
    for key, candidate_key in (("item_id", "id"), ("target", "target")):
        _require(isinstance(feedback.get(key), str) and bool(feedback[key])
                 and feedback[key] == candidate.get(candidate_key), f"feedback {key} mismatch")
    _require(feedback["manifest_binding"].get("candidate_id") == candidate["id"], "candidate binding mismatch")
    context = _mapping(manifest.get("context"), "manifest context")
    source_data, source_binding = files.bound(feedback.get("source_audio_binding"), base)
    clip_data, clip_binding = files.bound(feedback.get("context_audio_binding"), base)
    source_meta, source_pcm = _wav(source_data, feedback["source_audio_binding"])
    clip_meta, clip_pcm = _wav(clip_data, feedback["context_audio_binding"])
    context_start, context_end = _frames([context.get("source_start_frame"), context.get("source_end_frame")], "context range")
    _require(context_end <= source_meta["frame_count"] and context_end - context_start == clip_meta["frame_count"],
             "context exceeds source or has incorrect length")
    for key, expected in (("source_start_frame", context_start), ("source_end_frame", context_end)):
        _require(feedback["context_audio_binding"].get(key) == expected, "context range binding mismatch")
    for key, expected in (("source_audio_sha256", source_binding["file_sha256"]),
                          ("clip_sha256", clip_binding["file_sha256"]), ("clip_pcm16_sha256", clip_meta["pcm16_sha256"]),
                          ("source_sample_rate", 16000), ("clip_frame_count", clip_meta["frame_count"])):
        _require(context.get(key) == expected, f"manifest context {key} mismatch")
    _require(clip_pcm == source_pcm[context_start * 2:context_end * 2], "context is not an exact source PCM slice")
    model_frames = _frames([candidate.get("source_start_frame"), candidate.get("source_end_frame")], "model candidate range")
    review = _mapping(feedback.get("refined_review_range"), "refined_review_range")
    human_frames = _frames(review.get("source_frames"), "review source range")
    relative_frames = _frames(review.get("context_relative_frames"), "review relative range")
    for frames in (model_frames, human_frames):
        _require(context_start <= frames[0] < frames[1] <= context_end, "review/candidate outside context")
    _require(relative_frames == [v - context_start for v in human_frames], "review relative/source frames differ")
    _require(_require_frame(review.get("sample_rate"), "review sample rate") == 16000, "review sample rate mismatch")
    seconds = _mapping(review.get("seconds"), "review seconds")
    for key, frame in zip(("start", "end"), human_frames):
        _require(abs(_require_number(seconds.get(key), key) - frame / 16000) <= 1e-5, "review seconds/frame mismatch")
    # Preserve and verify prior evidence without letting an old judgment replace
    # the explicit final feedback supplied to this invocation.
    for key, binding in feedback.items():
        if key.startswith("prior_") and key.endswith("_binding"):
            prior_data, _ = files.bound(binding, base)
            prior = _json(prior_data)
            _require(prior.get("item_id") == candidate["id"] and prior.get("target") == candidate["target"],
                     "prior feedback belongs to a different candidate")
            _require(prior.get("schema_version") == binding.get("schema_version"), "prior feedback schema mismatch")
    model_evidence = _model_evidence(manifest, Path(manifest_binding["path"]).parent, files,
                                     {**source_binding, **source_meta}, candidate)
    files.check_unchanged()
    return {
        "schema_version": SCHEMA_VERSION, "evidence_kind": "human_boundary_reference_only",
        "status": "human_reference_recorded", "item_id": candidate["id"], "target": candidate["target"],
        "feedback_binding": feedback_binding, "bindings": list(files.bindings.values()),
        "human_confirmation_in_chat": True, "user_judgment": feedback["user_judgment"], "assessments": dict(assessments),
        "source_audio_binding": {**source_binding, **source_meta},
        "context_audio_binding": {**clip_binding, **clip_meta, "source_start_frame": context_start, "source_end_frame": context_end},
        "model_candidate_range": _range(model_frames, context_start), "human_reference_range": _range(human_frames, context_start),
        "difference_ms": dict(zip(("start", "end"), ((h - m) / 16 for h, m in zip(human_frames, model_frames)))),
        "model_evidence": model_evidence, "verification": {"file_bindings": True, "exact_pcm_slice": True,
            "frame_arithmetic": True, "independent_listening_performed": False},
        "executable_cuts": [], **FALSE_FLAGS,
    }


def write_review_receipt(feedback_path: str | Path, output_path: str | Path) -> dict:
    """Atomically write a new receipt; preserve input files and older outputs."""
    receipt = import_refined_feedback(feedback_path)
    output = Path(output_path).resolve()
    for binding in receipt["bindings"]:
        source = Path(binding["path"])
        _require(output != source and not (output.exists() and output.samefile(source)), "output would overwrite evidence")
    encoded = (json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if output.exists():
        _require(output.read_bytes() == encoded, "output already exists with different content; use a new receipt path")
        return receipt
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".asr-review-", dir=output.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded)
        # Atomic publish without replacing a concurrently created destination.
        os.link(name, output)
    finally:
        Path(name).unlink(missing_ok=True)
    return receipt


def verify_review_receipt(receipt_path: str | Path) -> dict:
    """Recompute the receipt from its bound feedback and current evidence bytes."""
    saved = _json(Path(receipt_path).read_bytes())
    binding = _mapping(saved.get("feedback_binding"), "feedback_binding")
    _Files().bound(binding, Path(receipt_path).resolve().parent)
    expected = import_refined_feedback(binding["path"])
    _require(_canonical_json_bytes(saved) == _canonical_json_bytes(expected), "receipt does not match current evidence")
    return expected
