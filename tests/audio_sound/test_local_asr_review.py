from __future__ import annotations

import hashlib
import json
import os
import struct
import wave
from copy import deepcopy
from pathlib import Path

import pytest

from audio_sound import local_asr_review
from audio_sound.local_asr_review import (
    import_refined_feedback,
    verify_review_receipt,
    write_review_receipt,
)


_GATES = (
    "production_eligible", "acoustic_accepted", "neighbor_protection_verified",
    "source_media_modified", "production_integrated",
)
_ASSESSMENTS = (
    "target_onset_F", "target_final_s", "preceding_word", "following_word_or_quote",
)


def _save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _binding(path: Path) -> dict:
    data = path.read_bytes()
    return {
        "path": str(path),
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _wav(path: Path, pcm: bytes, *, sample_rate: int = 16000) -> dict:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm)
    return {
        **_binding(path),
        "pcm16_sha256": hashlib.sha256(pcm).hexdigest(),
        "sample_rate": sample_rate,
        "channels": 1,
        "sample_width_bytes": 2,
        "frame_count": len(pcm) // 2,
    }


@pytest.fixture
def feedback(tmp_path: Path) -> Path:
    # Nonconstant source samples make shifted or substituted context slices detectable.
    pcm = b"".join(struct.pack("<h", frame - 16000) for frame in range(32000))
    source = _wav(tmp_path / "source.wav", pcm)
    context = _wav(tmp_path / "context.wav", pcm[8000 * 2:24000 * 2])
    context.update(source_start_frame=8000, source_end_frame=24000)
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "schema_version": "local-asr-candidate-review-v1",
        "candidate": {
            "id": "E2-FW-test", "target": "Famous Works",
            "source_start_frame": 12000, "source_end_frame": 20000,
        },
        "context": {
            "source_audio": source["path"],
            "source_audio_sha256": source["file_sha256"],
            "clip_path": context["path"],
            "clip_sha256": context["file_sha256"],
            "clip_pcm16_sha256": context["pcm16_sha256"],
            "source_start_frame": 8000, "source_end_frame": 24000,
            "source_sample_rate": 16000, "clip_frame_count": 16000,
        },
    }
    _save(manifest_path, manifest)
    feedback_path = tmp_path / "feedback.json"
    _save(feedback_path, {
        "schema_version": "local-asr-refined-boundary-feedback-v1",
        "evidence_kind": "human_refined_boundary_feedback",
        "source": "user_reply_in_originating_codex_chat",
        "human_confirmation_in_chat": True,
        "item_id": "E2-FW-test", "target": "Famous Works",
        "manifest_binding": {
            **_binding(manifest_path),
            "schema_version": "local-asr-candidate-review-v1",
            "candidate_id": "E2-FW-test",
        },
        "source_audio_binding": source,
        "context_audio_binding": context,
        "refined_review_range": {
            "source_frames": [11000, 19992],
            "context_relative_frames": [3000, 11992],
            "seconds": {"start": 0.6875, "end": 1.2495},
            "sample_rate": 16000,
        },
        "assessments": {key: {"judgment": "complete"} for key in _ASSESSMENTS},
        "user_judgment": "refined_boundary_suitable",
        **{key: False for key in _GATES},
    })
    return feedback_path


def _refresh_manifest_binding(feedback_path: Path, data: dict, manifest: dict) -> None:
    manifest_path = Path(data["manifest_binding"]["path"])
    _save(manifest_path, manifest)
    data["manifest_binding"].update(_binding(manifest_path))
    _save(feedback_path, data)


def _assert_reference_only(receipt: dict) -> None:
    assert receipt["schema_version"] == "local-asr-human-review-receipt-v1"
    assert receipt["status"] == "human_reference_recorded"
    for key in (*_GATES, "authoritative_cut_boundary", "execution_required"):
        assert receipt[key] is False
    assert receipt["executable_cuts"] == []


def test_import_preserves_model_boundary_and_records_manual_onset_correction(feedback: Path) -> None:
    before = {path: path.read_bytes() for path in feedback.parent.iterdir()}
    receipt = import_refined_feedback(feedback)
    _assert_reference_only(receipt)
    assert receipt["model_candidate_range"]["source_frames"] == [12000, 20000]
    assert receipt["human_reference_range"]["source_frames"] == [11000, 19992]
    assert receipt["difference_ms"] == pytest.approx({"start": -62.5, "end": -0.5})
    assert {path: path.read_bytes() for path in feedback.parent.iterdir()} == before


@pytest.mark.parametrize("field", _GATES)
@pytest.mark.parametrize("invalid", [True, 0, "false", None])
def test_nonfalse_production_gate_is_rejected(feedback: Path, field: str, invalid) -> None:
    data = _read(feedback)
    data[field] = invalid
    _save(feedback, data)
    with pytest.raises(ValueError):
        import_refined_feedback(feedback)


@pytest.mark.parametrize("field", _GATES)
def test_missing_production_gate_is_rejected(feedback: Path, field: str) -> None:
    data = _read(feedback)
    del data[field]
    _save(feedback, data)
    with pytest.raises(ValueError):
        import_refined_feedback(feedback)


@pytest.mark.parametrize("field,value", [
    ("human_confirmation_in_chat", False),
    ("human_confirmation_in_chat", 1),
    ("user_judgment", "suitable"),
    ("schema_version", "manual-boundary-proposals-v1"),
    ("source", "model_inferred_feedback"),
    ("evidence_kind", "automatic_boundary_feedback"),
    ("item_id", "different-item"),
    ("target", "Childhood"),
])
def test_unconfirmed_or_mismatched_feedback_is_rejected(feedback: Path, field: str, value) -> None:
    data = _read(feedback)
    data[field] = value
    _save(feedback, data)
    with pytest.raises(ValueError):
        import_refined_feedback(feedback)


@pytest.mark.parametrize("assessment", _ASSESSMENTS)
@pytest.mark.parametrize("value", ["clipped", None])
def test_all_four_human_assessments_are_required(feedback: Path, assessment: str, value) -> None:
    data = _read(feedback)
    if value is None:
        del data["assessments"][assessment]
    else:
        data["assessments"][assessment]["judgment"] = value
    _save(feedback, data)
    with pytest.raises(ValueError):
        import_refined_feedback(feedback)


@pytest.mark.parametrize("binding", ["manifest_binding", "source_audio_binding", "context_audio_binding"])
@pytest.mark.parametrize("field,value", [("file_sha256", "0" * 64), ("bytes", 1)])
def test_bound_file_identity_is_recomputed(feedback: Path, binding: str, field: str, value) -> None:
    data = _read(feedback)
    data[binding][field] = value
    _save(feedback, data)
    with pytest.raises(ValueError):
        import_refined_feedback(feedback)


@pytest.mark.parametrize("binding", ["manifest_binding", "source_audio_binding", "context_audio_binding"])
@pytest.mark.parametrize("value", [None, True, "100", 100.0])
def test_primary_binding_size_must_be_present_and_integer(feedback: Path, binding: str, value) -> None:
    data = _read(feedback)
    if value is None:
        del data[binding]["bytes"]
    else:
        data[binding]["bytes"] = value
    _save(feedback, data)
    with pytest.raises(ValueError):
        import_refined_feedback(feedback)


@pytest.mark.parametrize("binding", ["source_audio_binding", "context_audio_binding"])
def test_changed_wav_is_rejected(feedback: Path, binding: str) -> None:
    path = Path(_read(feedback)[binding]["path"])
    content = bytearray(path.read_bytes())
    content[-1] ^= 1
    path.write_bytes(content)
    with pytest.raises(ValueError):
        import_refined_feedback(feedback)


def test_context_must_be_exact_source_slice_even_when_all_hashes_are_updated(feedback: Path) -> None:
    data = _read(feedback)
    path = Path(data["context_audio_binding"]["path"])
    with wave.open(str(path), "rb") as stream:
        pcm = bytearray(stream.readframes(stream.getnframes()))
    pcm[0] ^= 1
    data["context_audio_binding"].update(_wav(path, bytes(pcm)))
    manifest = _read(Path(data["manifest_binding"]["path"]))
    manifest["context"]["clip_sha256"] = data["context_audio_binding"]["file_sha256"]
    manifest["context"]["clip_pcm16_sha256"] = data["context_audio_binding"]["pcm16_sha256"]
    _refresh_manifest_binding(feedback, data, manifest)
    with pytest.raises(ValueError):
        import_refined_feedback(feedback)


@pytest.mark.parametrize("field,value", [
    ("source_frames", [19992, 11000]),
    ("source_frames", [11000, 11000]),
    ("source_frames", [7999, 19992]),
    ("source_frames", [11000, 24001]),
    ("source_frames", [11000.5, 19992]),
    ("context_relative_frames", [3001, 11992]),
    ("seconds", {"start": 0.75, "end": 1.2495}),
    ("seconds", {"start": float("nan"), "end": 1.2495}),
    ("sample_rate", 48000),
])
def test_refined_frames_and_seconds_must_be_consistent(feedback: Path, field: str, value) -> None:
    data = _read(feedback)
    data["refined_review_range"][field] = value
    _save(feedback, data)
    with pytest.raises(ValueError):
        import_refined_feedback(feedback)


def test_manifest_context_cannot_shift_even_when_hash_binding_is_updated(feedback: Path) -> None:
    data = _read(feedback)
    manifest = _read(Path(data["manifest_binding"]["path"]))
    manifest["context"]["source_start_frame"] += 1
    manifest["context"]["source_end_frame"] += 1
    _refresh_manifest_binding(feedback, data, manifest)
    with pytest.raises(ValueError):
        import_refined_feedback(feedback)


def test_saved_receipt_can_be_reverified(feedback: Path) -> None:
    output = feedback.parent / "receipt.json"
    receipt = write_review_receipt(feedback, output)
    _assert_reference_only(receipt)
    assert _read(output) == receipt
    verified = verify_review_receipt(output)
    assert isinstance(verified, dict)


@pytest.mark.parametrize("change", ["range", "model_range", "difference", "production_gate"])
def test_receipt_tampering_is_rejected(feedback: Path, change: str) -> None:
    output = feedback.parent / "receipt.json"
    write_review_receipt(feedback, output)
    receipt = _read(output)
    if change == "range":
        receipt["human_reference_range"]["source_frames"][0] += 1
    elif change == "model_range":
        receipt["model_candidate_range"]["source_frames"][0] += 1
    elif change == "difference":
        receipt["difference_ms"]["start"] = 0
    else:
        receipt["production_eligible"] = True
    _save(output, receipt)
    with pytest.raises(ValueError):
        verify_review_receipt(output)


def test_receipt_reverification_detects_changed_original_feedback(feedback: Path) -> None:
    output = feedback.parent / "receipt.json"
    write_review_receipt(feedback, output)
    data = _read(feedback)
    data["assessments"]["target_onset_F"]["judgment"] = "clipped"
    _save(feedback, data)
    with pytest.raises(ValueError):
        verify_review_receipt(output)


@pytest.mark.parametrize("output_name", ["feedback", "manifest_binding", "source_audio_binding", "context_audio_binding"])
def test_output_must_not_overwrite_input_or_evidence(feedback: Path, output_name: str) -> None:
    data = _read(feedback)
    output = feedback if output_name == "feedback" else Path(data[output_name]["path"])
    before = output.read_bytes()
    with pytest.raises(ValueError):
        write_review_receipt(feedback, output)
    assert output.read_bytes() == before


def test_invalid_feedback_does_not_damage_existing_output(feedback: Path) -> None:
    output = feedback.parent / "receipt.json"
    output.write_text("previous valid artifact", encoding="utf-8")
    data = _read(feedback)
    data["human_confirmation_in_chat"] = False
    _save(feedback, data)
    with pytest.raises(ValueError):
        write_review_receipt(feedback, output)
    assert output.read_text(encoding="utf-8") == "previous valid artifact"


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@pytest.fixture
def model_feedback(feedback: Path) -> Path:
    data = _read(feedback)
    source = data["source_audio_binding"]
    model_input = {
        "sha256": source["file_sha256"], "pcm_sha256": source["pcm16_sha256"],
        "sample_rate": 16000, "frame_count": 32000, "duration_seconds": 2.0,
    }
    model_files = {"model.bin": {"bytes": 1024, "sha256": "a" * 64}}
    model_digest = hashlib.sha256(_canonical_bytes(model_files)).hexdigest()
    worker = {
        "schema_version": "local-asr-worker-result-v1",
        "status": "completed", "text": "before Famous Works after",
        "native_units": [
            {"native_index": 0, "text": "before ", "start": 0.0, "end": 0.5},
            {"native_index": 1, "text": "Famous", "start": 0.75, "end": 1.0},
            {"native_index": 2, "text": " Works", "start": 1.0, "end": 1.25},
            {"native_index": 3, "text": " after", "start": 1.4, "end": 1.8},
        ],
        "native_segments": [], "timestamp_kind": "native_unit_seconds",
        "backend": "faster-whisper", "parameters": {},
        "input": deepcopy(model_input),
        "model": {"files": deepcopy(model_files)},
        "runtime": {"package_versions": {"faster-whisper": "1.1.1"}},
        "identity_checks": {"before": True, "after": True},
        "request": {
            "input": deepcopy(model_input), "model": {"files": deepcopy(model_files)},
            "backend": "faster-whisper", "parameters": {},
        },
        **{key: False for key in _GATES},
    }
    worker_bytes = _canonical_bytes(worker)
    identity = {
        "input": {"sha256": source["file_sha256"]},
        "result": {"sha256": hashlib.sha256(worker_bytes).hexdigest(), "bytes": len(worker_bytes)},
        "model": {"id": "whisper-small", "revision": model_digest, "sha256": model_digest},
        "provider": {"name": "faster-whisper", "version": "1.1.1"},
        "adapter": {"name": "local-adapter", "version": "1", "sha256": "b" * 64},
    }
    for component in identity.values():
        component["verified"] = True
    transcript = {
        **deepcopy(worker), "schema_version": "local-asr-transcript-v1",
        "input": deepcopy(model_input), "identity": identity, "worker_report": worker,
        "model": {"id": "whisper-small", "revision": model_digest, "sha256": model_digest,
                  "files": deepcopy(model_files)},
    }
    model_path = feedback.parent / "result.json"
    _save(model_path, {"transcript": transcript})
    manifest = _read(Path(data["manifest_binding"]["path"]))
    manifest["model_evidence"] = {
        "result_path": str(model_path), "result_sha256": _binding(model_path)["file_sha256"],
        "input_sha256": source["file_sha256"], "backend": "faster-whisper", "model_id": "whisper-small",
    }
    _refresh_manifest_binding(feedback, data, manifest)
    return feedback


def _save_model(feedback: Path, artifact: dict) -> None:
    data = _read(feedback)
    manifest = _read(Path(data["manifest_binding"]["path"]))
    model_path = Path(manifest["model_evidence"]["result_path"])
    _save(model_path, artifact)
    manifest["model_evidence"]["result_sha256"] = _binding(model_path)["file_sha256"]
    _refresh_manifest_binding(feedback, data, manifest)


def _model(feedback: Path) -> dict:
    data = _read(feedback)
    manifest = _read(Path(data["manifest_binding"]["path"]))
    return _read(Path(manifest["model_evidence"]["result_path"]))


def _rebind_worker(transcript: dict) -> None:
    worker_bytes = _canonical_bytes(transcript["worker_report"])
    transcript["identity"]["result"].update(sha256=hashlib.sha256(worker_bytes).hexdigest(), bytes=len(worker_bytes))


def test_saved_model_evidence_verifies_native_candidate_without_cut_authority(model_feedback: Path) -> None:
    receipt = import_refined_feedback(model_feedback)
    _assert_reference_only(receipt)
    evidence = receipt["model_evidence"]
    assert evidence["status"] == "saved_worker_evidence_verified"
    assert evidence["native_indices"] == [1, 2]
    assert evidence["identity_verified_against_installed_runtime"] is False
    assert any(Path(binding["path"]).name == "result.json" for binding in receipt["bindings"])


@pytest.mark.parametrize("change", ["worker_hash", "transcript_units", "input", "model_id", "unverified_identity"])
def test_model_evidence_cannot_disagree_with_saved_worker_or_source(model_feedback: Path, change: str) -> None:
    artifact = _model(model_feedback)
    transcript = artifact["transcript"]
    if change == "worker_hash":
        transcript["worker_report"]["parameters"]["changed"] = True
    elif change == "transcript_units":
        transcript["native_units"][1]["start"] -= 0.01
    elif change == "input":
        transcript["input"]["pcm_sha256"] = "c" * 64
    elif change == "model_id":
        transcript["identity"]["model"]["id"] = "another-model"
    else:
        transcript["identity"]["adapter"]["verified"] = False
    _save_model(model_feedback, artifact)
    with pytest.raises(ValueError):
        import_refined_feedback(model_feedback)


@pytest.mark.parametrize("change", [
    "provider_version", "runtime_version", "worker_model_files", "request_model_files",
    "transcript_model_files", "model_digest", "model_revision", "worker_input",
    "request_backend", "request_parameters", "identity_checks", "worker_schema",
])
def test_resigned_worker_cannot_hide_model_or_input_identity_inconsistency(model_feedback: Path, change: str) -> None:
    artifact = _model(model_feedback)
    transcript = artifact["transcript"]
    worker = transcript["worker_report"]
    if change == "provider_version":
        transcript["identity"]["provider"]["version"] = "9.9.9"
    elif change == "runtime_version":
        worker["runtime"]["package_versions"]["faster-whisper"] = "9.9.9"
    elif change == "worker_model_files":
        worker["model"]["files"]["model.bin"]["sha256"] = "c" * 64
    elif change == "request_model_files":
        worker["request"]["model"]["files"]["model.bin"]["sha256"] = "c" * 64
    elif change == "transcript_model_files":
        transcript["model"]["files"]["model.bin"]["sha256"] = "c" * 64
    elif change == "model_digest":
        transcript["identity"]["model"]["sha256"] = "c" * 64
    elif change == "model_revision":
        transcript["identity"]["model"]["revision"] = "c" * 64
    elif change == "worker_input":
        worker["input"]["pcm_sha256"] = "c" * 64
    elif change == "request_backend":
        worker["request"]["backend"] = "another-backend"
    elif change == "request_parameters":
        worker["request"]["parameters"]["language"] = "zh"
    elif change == "identity_checks":
        worker["identity_checks"]["after"] = False
    else:
        worker["schema_version"] = "unrecognized-worker-result"
    _rebind_worker(transcript)
    _save_model(model_feedback, artifact)
    with pytest.raises(ValueError):
        import_refined_feedback(model_feedback)


@pytest.mark.parametrize("change", ["nonword_timestamps", "changed_boundary", "overlap", "repeated_target"])
def test_model_evidence_requires_one_structurally_valid_matching_native_window(model_feedback: Path, change: str) -> None:
    artifact = _model(model_feedback)
    transcript = artifact["transcript"]
    worker = transcript["worker_report"]
    if change == "nonword_timestamps":
        worker["timestamp_kind"] = "input_chunk_seconds"
    elif change == "changed_boundary":
        worker["native_units"][1]["start"] -= 0.01
    elif change == "overlap":
        worker["native_units"][2]["start"] = 0.9
    else:
        worker["text"] += " Famous Works"
        worker["native_units"].append({"native_index": 4, "text": " Famous Works", "start": 1.81, "end": 1.99})
    for key in ("text", "native_units", "timestamp_kind"):
        transcript[key] = deepcopy(worker[key])
    _rebind_worker(transcript)
    _save_model(model_feedback, artifact)
    with pytest.raises(ValueError):
        import_refined_feedback(model_feedback)


def test_model_result_file_is_protected_from_output_overwrite(model_feedback: Path) -> None:
    data = _read(model_feedback)
    manifest = _read(Path(data["manifest_binding"]["path"]))
    result_path = Path(manifest["model_evidence"]["result_path"])
    before = result_path.read_bytes()
    with pytest.raises(ValueError):
        write_review_receipt(model_feedback, result_path)
    assert result_path.read_bytes() == before


def test_output_hardlink_to_input_is_rejected(feedback: Path) -> None:
    output = feedback.parent / "alias.json"
    os.link(feedback, output)
    before = feedback.read_bytes()
    with pytest.raises(ValueError):
        write_review_receipt(feedback, output)
    assert feedback.read_bytes() == before == output.read_bytes()


def test_atomic_publish_failure_leaves_no_partial_receipt(feedback: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = feedback.parent / "receipt.json"
    before = set(feedback.parent.iterdir())

    def fail_publish(*args, **kwargs):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(local_asr_review.os, "link", fail_publish)
    with pytest.raises(OSError, match="simulated filesystem failure"):
        write_review_receipt(feedback, output)
    assert not output.exists()
    assert set(feedback.parent.iterdir()) == before


def test_atomic_publish_does_not_replace_concurrently_created_output(feedback: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = feedback.parent / "receipt.json"
    original_link = os.link

    def competing_publish(source, destination):
        Path(destination).write_text("other writer", encoding="utf-8")
        return original_link(source, destination)

    monkeypatch.setattr(local_asr_review.os, "link", competing_publish)
    with pytest.raises(FileExistsError):
        write_review_receipt(feedback, output)
    assert output.read_text(encoding="utf-8") == "other writer"
    assert not list(feedback.parent.glob(".asr-review-*"))


def test_source_changed_during_import_is_rejected(feedback: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(_read(feedback)["source_audio_binding"]["path"])
    original_read = Path.read_bytes
    source_reads = 0

    def read_with_change(path):
        nonlocal source_reads
        if path == source:
            source_reads += 1
            if source_reads == 2:
                content = bytearray(original_read(path))
                content[-1] ^= 1
                path.write_bytes(content)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read_with_change)
    with pytest.raises(ValueError, match="changed during import"):
        import_refined_feedback(feedback)
