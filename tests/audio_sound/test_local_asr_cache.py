from __future__ import annotations

import wave
from pathlib import Path

import pytest

from audio_sound.local_asr_cache import (
    TRANSCRIPT_SCHEMA_VERSION,
    build_cache_identity,
    build_input_identity,
    build_model_manifest,
    read_transcript_cache,
    validate_cached_transcript,
    write_transcript_cache,
)


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    audio = tmp_path / "source.wav"
    pcm = b"\x00\x00" * 160
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(pcm)
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.bin").write_bytes(b"model")
    identity = build_cache_identity(
        build_input_identity(audio), build_model_manifest(model),
        provider="faster-whisper", adapter={"name": "native", "version": "1"},
        parameters={"language": "zh", "device": "cpu", "chunk_seconds": 18.0},
    )
    return audio, model, identity


def _transcript(identity: dict) -> dict:
    return {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "identity": identity,
        "text": "",
        "native_units": [],
        "production_eligible": False,
        "acoustic_accepted": False,
        "neighbor_protection_verified": False,
        "production_integrated": False,
        "source_media_modified": False,
    }


def test_cache_hit_verifies_manifest_and_result_bytes(tmp_path: Path) -> None:
    audio, model, identity = _fixture(tmp_path)
    path = tmp_path / "cache" / "transcript.json"
    write_transcript_cache(path, _transcript(identity), identity)
    assert read_transcript_cache(path, identity)["text"] == ""
    assert validate_cached_transcript(path, identity)["valid"] is True

    path.write_bytes(path.read_bytes().replace(b'"text":""', b'"text":"tampered"'))
    report = validate_cached_transcript(path, identity)
    assert report["valid"] is False
    assert "cache_bytes_hash_mismatch" in report["issues"]


def test_cache_invalidates_when_source_or_model_changes(tmp_path: Path) -> None:
    audio, model, identity = _fixture(tmp_path)
    path = tmp_path / "cache.json"
    write_transcript_cache(path, _transcript(identity), identity)
    audio.write_bytes(audio.read_bytes() + b"\x00\x00")
    assert "source_identity_changed" in validate_cached_transcript(path, identity)["issues"]

    # Rebuild the source identity while preserving the old artifact, then
    # mutate a model file to exercise the separate model binding.
    new_identity = build_cache_identity(
        build_input_identity(audio), build_model_manifest(model),
        provider=identity["provider"], adapter=identity["adapter"], parameters=identity["parameters"],
    )
    model.joinpath("model.bin").write_bytes(b"changed")
    assert "model_identity_changed" in validate_cached_transcript(path, new_identity)["issues"]


def test_writer_rejects_self_reported_verified_flags(tmp_path: Path) -> None:
    _, _, identity = _fixture(tmp_path)
    payload = _transcript(identity)
    payload["production_eligible"] = True
    with pytest.raises(ValueError, match="unsafe flags"):
        write_transcript_cache(tmp_path / "cache.json", payload, identity)

    payload = _transcript(identity)
    payload["identity"] = {**identity, "verified": True}
    # Identity mismatch is checked before writing, so a forged nested marker
    # cannot be smuggled into a cache artifact.
    with pytest.raises(ValueError, match="identity"):
        write_transcript_cache(tmp_path / "cache.json", payload, identity)
