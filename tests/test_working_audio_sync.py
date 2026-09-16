import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def signal(seconds=8, rate=8000):
    rng = np.random.default_rng(42)
    envelope = np.repeat(rng.uniform(0.05, 0.8, int(seconds * 10)), rate // 10)
    return (rng.normal(0, 0.2, len(envelope)) * envelope).astype(np.float32)


def wav(path, data, rate=8000):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes((np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def test_same_timeline_restoration_passes_with_measured_evidence(tmp_path):
    from utils.working_audio import validate_working_audio_sync

    data = signal()
    original = wav(tmp_path / "original.wav", data)
    restored = wav(tmp_path / "restored.wav", data * 0.7)
    report = validate_working_audio_sync(original, restored, duration_seconds=8)
    assert report["status"] == "pass"
    assert report["strategy"] == "preserve_timeline_envelope_v1"
    assert report["windows"] and report["working_sha256"] != report["original_sha256"]


@pytest.mark.parametrize("problem", ["short", "shift", "wrong", "unverifiable", "drift"])
def test_duration_close_is_not_sync_evidence(tmp_path, problem):
    from utils.working_audio import validate_working_audio_sync

    data = signal()
    replacement = data.copy()
    if problem == "short":
        replacement = data[:-800]
    elif problem == "shift":
        replacement = np.concatenate([np.zeros(1200), data[:-1200]])
    elif problem == "wrong":
        replacement = np.random.default_rng(99).normal(0, 0.1, len(data))
    elif problem == "unverifiable":
        replacement[:] = 0
    elif problem == "drift":
        replacement[len(data) // 2 :] = np.roll(data[len(data) // 2 :], 1200)
    original = wav(tmp_path / "original.wav", data)
    restored = wav(tmp_path / "restored.wav", replacement)
    with pytest.raises(ValueError, match="[Ww]orking audio"):
        validate_working_audio_sync(original, restored, duration_seconds=8)
