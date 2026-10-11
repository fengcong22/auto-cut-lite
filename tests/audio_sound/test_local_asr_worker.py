from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import wave
from collections import namedtuple
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from audio_sound import local_asr_worker as worker


@pytest.fixture
def request_payload(tmp_path: Path) -> dict:
    audio = tmp_path / "audio.wav"
    pcm = b"\x00\x00" * 160
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(pcm)
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.bin").write_bytes(b"local-model")
    return {
        "schema_version": "local-asr-worker-request-v1", "backend": "faster-whisper",
        "audio_path": str(audio), "model_path": str(model),
        "parameters": {"language": "zh", "device": "cuda", "device_index": 0,
                       "compute_type": "int8_float16", "cpu_threads": 8, "chunk_seconds": 18},
        "input": {"sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                  "pcm_sha256": hashlib.sha256(pcm).hexdigest(), "frame_count": 160, "sample_rate": 16000},
        "model": {"files": {"model.bin": {"sha256": hashlib.sha256(b"local-model").hexdigest(), "bytes": 11}}},
    }


def test_probe_imports_no_model_libraries() -> None:
    program = (
        "import json,sys; from audio_sound.local_asr_worker import runtime_identity; "
        "r=runtime_identity(); assert not {'torch','numpy','funasr','faster_whisper','ctranslate2'} & set(sys.modules); "
        "print(json.dumps(r))"
    )
    result = subprocess.run([sys.executable, "-c", program], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    assert payload["python_executable"] == str(Path(sys.executable).resolve())
    assert {"torch", "faster-whisper", "ctranslate2", "funasr", "numpy"} <= payload["package_versions"].keys()


@pytest.mark.parametrize("key,value", [("initial_prompt", "Childhood"), ("hotwords", "Childhood"),
                                       ("vad_filter", True), ("prefix", "target")])
def test_prompt_or_extra_parameter_rejected(request_payload, key, value) -> None:
    request_payload["parameters"][key] = value
    with pytest.raises(ValueError, match="prompts are forbidden"):
        worker.validate_request(request_payload)


def test_request_execution_flags_or_unknown_top_level_fields_are_rejected(request_payload) -> None:
    request_payload["production_eligible"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        worker.validate_request(request_payload)


def test_invalid_request_flags_are_not_reflected_in_failure_echo(request_payload) -> None:
    request_payload["production_eligible"] = True
    report = worker.execute_request(request_payload, worker.OfflineGuard())
    assert report["status"] == "failed"
    assert report["request"] == {"omitted": "request_failed_schema_shape_validation"}
    assert "production_eligible" not in json.dumps(report["request"])


@pytest.mark.parametrize("section", ["input", "model"])
def test_execution_flags_cannot_hide_in_identity_sections(request_payload, section) -> None:
    request_payload[section]["production_eligible"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        worker.validate_request(request_payload)


@pytest.mark.parametrize("key,value", [("chunk_seconds", math.nan), ("chunk_seconds", 19),
                                       ("chunk_seconds", 0), ("device_index", True),
                                       ("cpu_threads", 0), ("compute_type", "auto"), ("device", "auto")])
def test_explicit_runtime_parameters_required(request_payload, key, value) -> None:
    request_payload["parameters"][key] = value
    with pytest.raises(ValueError):
        worker.validate_request(request_payload)


def test_file_pcm_and_manifest_identities_are_independent(request_payload) -> None:
    worker.validate_request(request_payload)
    assert worker.verify_input(request_payload) == b"\x00\x00" * 160
    worker.verify_model(request_payload)
    request_payload["input"]["pcm_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="PCM"):
        worker.verify_input(request_payload)
    (Path(request_payload["model_path"]) / "model.bin").write_bytes(b"changed____")
    with pytest.raises(ValueError, match="identity mismatch"):
        worker.verify_model(request_payload)


@pytest.mark.parametrize("name", ["../outside.bin", "/absolute.bin", "C:/absolute.bin", "nested\\file.bin", "a/../model.bin"])
def test_model_paths_must_stay_inside_directory(request_payload, name) -> None:
    request_payload["model"]["files"] = {name: {"sha256": "a" * 64, "bytes": 1}}
    with pytest.raises(ValueError):
        worker.verify_model(request_payload)


def test_extra_model_file_blocks_unbound_config_read(request_payload) -> None:
    (Path(request_payload["model_path"]) / "config.json").write_text("{}")
    with pytest.raises(ValueError, match="extra or missing"):
        worker.verify_model(request_payload)


@pytest.mark.parametrize("event", ["socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.bind"])
def test_offline_guard_records_and_rejects_network(event) -> None:
    guard = worker.OfflineGuard()
    with pytest.raises(PermissionError):
        guard.audit(event, ("example.invalid", 443))
    assert guard.attempts == [{"event": event, "arguments": "('example.invalid', 443)", "blocked": True}]
    guard.audit("socket.gethostname", ())


def test_ct2_only_blocks_torch_conversion_without_importing_it(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.delenv("KMP_DUPLICATE_LIB_OK", raising=False)
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    runtime = {}
    worker.initialize_ct2_only(runtime, "cpu")
    assert runtime["torch_conversion_imports_disabled"] is True
    with pytest.raises(ImportError):
        sys.meta_path[0].find_spec("torch")
    assert "torch" not in sys.modules


def test_json_safe_retains_invalid_and_zero_native_rows() -> None:
    payload = worker.json_safe([{"start": math.nan, "end": math.inf}, {"start": 1, "end": 1}, None])
    assert len(payload) == 3
    assert payload[0] == {"start": {"nonfinite_float": "nan"}, "end": {"nonfinite_float": "inf"}}
    assert payload[1]["start"] == payload[1]["end"]
    json.dumps(payload, allow_nan=False)


def test_result_echoes_identity_and_never_grants_execution(request_payload, monkeypatch) -> None:
    original = deepcopy(request_payload)

    def fake_run(request, pcm, report):
        report["text"] = "Childhood"
        report["native_units"] = [{"native_index": 0, "text": "Childhood", "start": 1, "end": 1}]

    monkeypatch.setattr(worker, "run_whisper", fake_run)
    report = worker.execute_request(request_payload, worker.OfflineGuard())
    assert report["status"] == "completed"
    assert report["request"] == original == request_payload
    assert all(report["identity_checks"].values())
    for flag in ("production_eligible", "acoustic_accepted", "neighbor_protection_verified", "production_integrated"):
        assert report[flag] is False


def test_model_change_during_inference_fails_post_verification(request_payload, monkeypatch) -> None:
    def change_model(request, pcm, report):
        report["text"] = "untrusted result"
        (Path(request["model_path"]) / "model.bin").write_bytes(b"different")

    monkeypatch.setattr(worker, "run_whisper", change_model)
    report = worker.execute_request(request_payload, worker.OfflineGuard())
    assert report["status"] == "failed"
    assert report["text"] == "untrusted result"
    assert report["identity_checks"] == {"before": True, "after": False}
    assert "post_verification_error" in report


def test_blocked_network_caught_by_backend_still_fails_run(request_payload, monkeypatch) -> None:
    guard = worker.OfflineGuard()

    def caught_network(request, pcm, report):
        try:
            guard.audit("socket.connect", ("example.invalid",))
        except PermissionError:
            pass

    monkeypatch.setattr(worker, "run_whisper", caught_network)
    report = worker.execute_request(request_payload, guard)
    assert report["status"] == "failed"
    assert report["network"]["blocked_attempts"]


class _Audio(list):
    def astype(self, dtype):
        return self

    def __truediv__(self, value):
        return self

    def __getitem__(self, key):
        value = super().__getitem__(key)
        return _Audio(value) if isinstance(key, slice) else value


def _mock_numpy(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(
        frombuffer=lambda pcm, dtype: _Audio([0] * (len(pcm) // 2)), float32="float32"))


def test_whisper_preserves_native_invalid_rows_and_uses_no_prompt(request_payload, monkeypatch) -> None:
    _mock_numpy(monkeypatch)
    monkeypatch.setattr(worker, "initialize_ct2_only", lambda *args: None)
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(
        get_supported_compute_types=lambda *args: {"int8_float16"}, get_cuda_device_count=lambda: 1))
    Word = namedtuple("Word", "word start end probability")
    Segment = namedtuple("Segment", "text start end words")
    seen = {}

    class Model:
        model = SimpleNamespace(device="cuda", compute_type="int8_float16", device_index=[0])

        def __init__(self, path, **kwargs):
            seen["init"] = kwargs

        def transcribe(self, audio, **kwargs):
            seen["decode"] = kwargs
            return iter([Segment("raw", 0, 1, [Word("zero", 0, 0, .1), Word("bad", math.nan, 1, .2), None])]), {}

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    report = worker.execute_request(request_payload, worker.OfflineGuard())
    assert report["status"] == "completed"
    assert len(report["native_units"]) == 3
    assert report["native_units"][0]["start"] == report["native_units"][0]["end"] == 0
    assert report["native_units"][1]["start"] == {"nonfinite_float": "nan"}
    assert report["native_units"][2]["native_row"] == {"malformed_native_row": None}
    assert report["timestamp_kind"] == "native_unit_seconds"
    assert seen["init"]["local_files_only"] is True
    assert seen["decode"]["initial_prompt"] is None
    assert seen["decode"]["hotwords"] is None
    assert seen["decode"]["condition_on_previous_text"] is False


def test_whisper_device_fallback_is_failure(request_payload, monkeypatch) -> None:
    _mock_numpy(monkeypatch)
    monkeypatch.setattr(worker, "initialize_ct2_only", lambda *args: None)
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(
        get_supported_compute_types=lambda *args: {"int8_float16"}, get_cuda_device_count=lambda: 1))
    model = SimpleNamespace(model=SimpleNamespace(device="cpu", compute_type="int8_float16", device_index=[0]))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=lambda *a, **k: model))
    report = worker.execute_request(request_payload, worker.OfflineGuard())
    assert report["status"] == "failed"
    assert "fallback forbidden" in report["error"]["message"]


def test_paraformer_chunks_have_no_fabricated_word_times(request_payload, monkeypatch) -> None:
    _mock_numpy(monkeypatch)
    request_payload["backend"] = "funasr"
    request_payload["parameters"].update(device="cpu", compute_type="float32", chunk_seconds=.005)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    seen = []

    class Model:
        model = SimpleNamespace(parameters=lambda: iter([SimpleNamespace(device="cpu", dtype="torch.float32")]))

        def __init__(self, **kwargs):
            seen.append(kwargs)

        def generate(self, **kwargs):
            seen.append(kwargs)
            return [{"text": "原文", "timestamp": [[0, 0], [math.nan, 10], "bad"]}]

    monkeypatch.setitem(sys.modules, "funasr", SimpleNamespace(AutoModel=Model))
    report = worker.execute_request(request_payload, worker.OfflineGuard())
    assert report["status"] == "completed"
    assert report["timestamp_kind"] == "unavailable"
    assert len(report["native_segments"]) == 2
    assert len(report["native_units"]) == 6
    assert all(unit["text"] is None for unit in report["native_units"])
    assert report["native_units"][0]["start"] == report["native_units"][0]["end"] == 0
    assert report["native_units"][2]["native_pair_ms"] == "bad"
    assert report["native_segments"][1]["source_start_frame"] == 80
    assert seen[0]["trust_remote_code"] is False
    assert seen[0]["vad_model"] is seen[0]["punc_model"] is seen[0]["spk_model"] is None
    assert "initial_prompt" not in seen[1] and "hotword" not in seen[1]


def test_cli_saves_failure_and_does_not_overwrite(tmp_path, monkeypatch, capsys) -> None:
    request = tmp_path / "request.json"
    output = tmp_path / "output.json"
    request.write_text("invalid json", encoding="utf-8")
    monkeypatch.setattr(worker.OfflineGuard, "install", lambda self: None)
    assert worker.main(["--request", str(request), "--output", str(output)]) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed" and report["error"]["type"] == "JSONDecodeError"
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    before = output.read_bytes()
    with pytest.raises(ValueError, match="refusing overwrite"):
        worker.main(["--request", str(request), "--output", str(output)])
    assert output.read_bytes() == before
