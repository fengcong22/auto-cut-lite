import math
import subprocess
import sys
import wave
from fractions import Fraction
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
    assert report["strategy"] == "preserve_timeline_envelope_v2_native_coverage"
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


@pytest.mark.parametrize("tail_samples", [148, 800])
def test_native_tail_up_to_50_ms_is_allowed_without_replacement_loss(tmp_path, tail_samples):
    from utils.working_audio import validate_working_audio_sync

    data = signal(rate=16000)[:-tail_samples]
    original = wav(tmp_path / "original.wav", data, rate=16000)
    restored = wav(tmp_path / "restored.wav", data * 0.7, rate=16000)
    report = validate_working_audio_sync(original, restored, duration_seconds=8)
    assert report["native_source_tail_seconds"] == pytest.approx(tail_samples / 16000)
    assert report["source_native_coverage"]["timestamps_contiguous"] is True
    assert report["working_native_duration_seconds"] == pytest.approx(8 - tail_samples / 16000)
    assert report["timeline_modified"] is False


@pytest.mark.parametrize("lost_samples", [1, 2, 500])
def test_replacement_loss_below_50_ms_is_still_rejected(tmp_path, lost_samples):
    from utils.working_audio import validate_working_audio_sync

    data = signal(rate=16000)[:-148]
    original = wav(tmp_path / "original.wav", data, rate=16000)
    restored = wav(tmp_path / "restored.wav", data[:-lost_samples] * 0.7, rate=16000)
    with pytest.raises(ValueError, match="lost samples"):
        validate_working_audio_sync(original, restored, duration_seconds=8)


@pytest.mark.parametrize("lost_samples", [0, 1])
@pytest.mark.parametrize("working_rate,tail_samples", [(22050, 148), (24000, 149)])
def test_different_native_rate_only_allows_nearest_grid_boundary(
    tmp_path, lost_samples, working_rate, tail_samples
):
    from utils.working_audio import validate_working_audio_sync

    data = signal(rate=16000)[:-tail_samples]
    original = wav(tmp_path / "original.wav", data, rate=16000)
    resampled = tmp_path / "resampled.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(original),
            "-ar",
            str(working_rate),
            str(resampled),
        ],
        check=True,
        capture_output=True,
    )
    nearest_count = math.floor(Fraction(len(data) * working_rate, 16000) + Fraction(1, 2))
    with wave.open(str(resampled), "rb") as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
    restored = wav(
        tmp_path / "restored.wav",
        samples[: nearest_count - lost_samples] / 32767,
        rate=working_rate,
    )
    if lost_samples:
        with pytest.raises(ValueError, match="lost samples"):
            validate_working_audio_sync(original, restored, duration_seconds=8)
    else:
        report = validate_working_audio_sync(original, restored, duration_seconds=8)
        assert report["replacement_required_native_frames"] == nearest_count
        assert report["replacement_native_rounding"] == "nearest_ties_up"
        if working_rate == 22050:
            assert 0 < report["replacement_native_quantization_seconds"] < 0.5 / working_rate
        else:
            assert report["replacement_native_quantization_seconds"] == 0


def test_diagnostic_one_sample_rounding_does_not_relax_native_coverage(tmp_path, monkeypatch):
    from utils import working_audio

    original = wav(tmp_path / "original.wav", signal(rate=16000), rate=16000)
    decode = working_audio._decode
    monkeypatch.setattr(working_audio, "_decode", lambda *args: decode(*args)[:-1])
    report = working_audio.validate_working_audio_sync(original, original, duration_seconds=8)
    assert report["diagnostic_rounding_tolerance_samples"] == 1
    assert report["replacement_native_rounding"] == "none"
    assert report["replacement_required_native_frames"] == 128000


def test_native_tail_above_50_ms_is_rejected(tmp_path):
    from utils.working_audio import validate_working_audio_sync

    original = wav(tmp_path / "original.wav", signal(rate=16000)[:-801], rate=16000)
    with pytest.raises(ValueError, match="50 ms"):
        validate_working_audio_sync(original, original, duration_seconds=8)


def test_diagnostic_decode_loss_cannot_be_hidden_by_tail_tolerance(tmp_path, monkeypatch):
    from utils import working_audio

    original = wav(tmp_path / "original.wav", signal())
    decode = working_audio._decode
    monkeypatch.setattr(working_audio, "_decode", lambda *args: decode(*args)[:-20])
    with pytest.raises(ValueError, match="diagnostic decode lost"):
        working_audio.validate_working_audio_sync(original, original, duration_seconds=8)


def test_leading_silence_is_counted_and_not_shifted(tmp_path):
    from utils.working_audio import validate_working_audio_sync

    data = signal()
    data[:4000] = 0
    original = wav(tmp_path / "original.wav", data)
    restored = wav(tmp_path / "restored.wav", data * 0.7)
    report = validate_working_audio_sync(original, restored, duration_seconds=8)
    assert report["source_native_coverage"]["source_native_frames"] == 64000
    assert report["source_native_coverage"]["start_seconds"] == 0
    assert all(row["offset_seconds"] == 0 for row in report["windows"])


def test_nonzero_source_timestamp_is_not_silently_rebased(tmp_path):
    from utils.working_audio import validate_working_audio_sync

    original = wav(tmp_path / "original.wav", signal())
    shifted = tmp_path / "offset.mka"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-itsoffset",
            "0.25",
            "-i",
            str(original),
            "-c:a",
            "copy",
            str(shifted),
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="nonzero start"):
        validate_working_audio_sync(shifted, original, duration_seconds=8.25)
