from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

from audio_sound import local_asr


def _audio_and_model(tmp_path: Path) -> tuple[Path, Path]:
    audio = tmp_path / "source.wav"
    pcm = b"\x00\x00" * 32000
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(pcm)
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.bin").write_bytes(b"model")
    return audio, model


def _fake_runtime() -> dict:
    return {"worker_version": "local-asr-worker-v1", "package_versions": {"faster-whisper": "test"}}


def _fake_worker(request: dict) -> dict:
    return {
        "schema_version": "local-asr-worker-result-v1", "status": "completed", "backend": request["backend"],
        "request": request, "parameters": request["parameters"], "input": request["input"], "model": request["model"],
        "text": "before Childhood after", "native_units": [
            {"native_index": 0, "text": "before ", "start": 0.0, "end": 0.5},
            {"native_index": 1, "text": "Childhood", "start": 0.5, "end": 1.1},
            {"native_index": 2, "text": " after", "start": 1.1, "end": 1.5},
        ], "native_segments": [], "timestamp_kind": "native_unit_seconds", "runtime": _fake_runtime(),
        "production_eligible": False, "acoustic_accepted": False, "neighbor_protection_verified": False,
        "source_media_modified": False, "production_integrated": False,
        "identity_checks": {"before": True, "after": True},
    }


def test_transcribe_runs_worker_then_returns_candidates_and_cache_hit(tmp_path, monkeypatch) -> None:
    audio, model = _audio_and_model(tmp_path)
    calls = []

    monkeypatch.setattr(local_asr, "_runtime_probe", lambda *args: _fake_runtime())

    def fake_run(command, **kwargs):
        calls.append(command)
        request_path = Path(command[command.index("--request") + 1])
        output_path = Path(command[command.index("--output") + 1])
        report = _fake_worker(json.loads(request_path.read_text(encoding="utf-8")))
        output_path.write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
        return type("Completed", (), {"returncode": 0, "stdout": "done", "stderr": "progress"})()

    monkeypatch.setattr(local_asr.subprocess, "run", fake_run)
    first = local_asr.transcribe_local(audio, model, cache_dir=tmp_path / "cache", query="Childhood", must_keep=("before",))
    assert first["status"] == "completed" and first["cache_hit"] is False
    assert first["candidates"]["count"] == 1
    assert first["candidates"]["candidates"][0]["native_window"] == {"start": 0.5, "end": 1.1}
    second = local_asr.transcribe_local(audio, model, cache_dir=tmp_path / "cache", query="Childhood")
    assert second["status"] == "cache_hit" and second["cache_hit"] is True
    assert len(calls) == 1


def test_source_change_invalidates_cache_and_model_change_changes_identity(tmp_path, monkeypatch) -> None:
    audio, model = _audio_and_model(tmp_path)
    monkeypatch.setattr(local_asr, "_runtime_probe", lambda *args: _fake_runtime())
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        request_path = Path(command[command.index("--request") + 1])
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_bytes(json.dumps(_fake_worker(json.loads(request_path.read_text(encoding="utf-8")))).encode())
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(local_asr.subprocess, "run", fake_run)
    local_asr.transcribe_local(audio, model, cache_dir=tmp_path / "cache")
    audio.write_bytes(audio.read_bytes() + b"\x00\x00")
    local_asr.transcribe_local(audio, model, cache_dir=tmp_path / "cache")
    (model / "config.json").write_text("{}", encoding="utf-8")
    local_asr.transcribe_local(audio, model, cache_dir=tmp_path / "cache")
    assert len(calls) == 3


def test_cpu_default_uses_supported_ct2_compute_type() -> None:
    assert local_asr._parameters(None, "faster-whisper")["compute_type"] == "float32"
    assert local_asr._parameters({"device": "cuda"}, "faster-whisper")["compute_type"] == "int8_float16"


def test_cache_hit_revalidates_payload_context_after_manifest_rewrite(tmp_path, monkeypatch) -> None:
    audio, model = _audio_and_model(tmp_path)
    monkeypatch.setattr(local_asr, "_runtime_probe", lambda *args: _fake_runtime())
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        request_path = Path(command[command.index("--request") + 1])
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_bytes(json.dumps(_fake_worker(json.loads(request_path.read_text(encoding="utf-8")))).encode())
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(local_asr.subprocess, "run", fake_run)
    first = local_asr.transcribe_local(audio, model, cache_dir=tmp_path / "cache")
    assert first["status"] == "completed"
    artifact = next((tmp_path / "cache").rglob("artifact.json"))
    manifest = artifact.with_name("manifest.json")
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["backend"] = "funasr"
    artifact_bytes = local_asr._json_bytes(payload)
    artifact.write_bytes(artifact_bytes)
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_payload.update(size=len(artifact_bytes), sha256=hashlib.sha256(artifact_bytes).hexdigest())
    manifest.write_bytes(local_asr._json_bytes(manifest_payload))
    second = local_asr.transcribe_local(audio, model, cache_dir=tmp_path / "cache")
    assert second["status"] == "completed" and second["cache_hit"] is False
    assert len(calls) == 2
