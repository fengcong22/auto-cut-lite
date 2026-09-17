"""Fresh-source decoding must distinguish native tails from processing loss."""

import array
import hashlib
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from utils import audio_coverage as coverage
from utils.review_audio_precision import extract_alignment_wav


@pytest.fixture
def media_tools():
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("Real-media integrity tests need FFmpeg and FFprobe")
    return {"ffmpeg_bin": ffmpeg, "ffprobe_bin": ffprobe}


def _wav(path, *, frames=48000, rate=16000, silent_frames=0):
    data = array.array(
        "h", (0 if i < silent_frames else int(12000 * math.sin(i * 0.071)) for i in range(frames))
    )
    if sys.byteorder != "little":
        data.byteswap()
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(data.tobytes())
    return path


def _read(path):
    with wave.open(str(path), "rb") as stream:
        return stream.getparams(), stream.readframes(stream.getnframes())


def _rewrite(path, params, pcm):
    with wave.open(str(path), "wb") as stream:
        stream.setparams(params)
        stream.writeframes(pcm)


def _extract(source, alignment, media_tools):
    extract_alignment_wav(source, alignment, ffmpeg_bin=media_tools["ffmpeg_bin"])
    return alignment


def _mux(tmp_path, media_tools, *, codec="alac", offset=0, gap=False):
    # 9.25 ms audio/video tail at 16 kHz, or an independently encoded AAC tail.
    source = _wav(tmp_path / "original.wav", frames=(48000 - 148) * 3, rate=48000)
    video = tmp_path / "original.mp4"
    command = [
        media_tools["ffmpeg_bin"],
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=navy:s=160x90:r=30:d=3",
        "-itsoffset",
        str(offset),
        "-i",
        str(source),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        codec,
    ]
    if gap:
        command += ["-af", "asetpts=PTS+if(gte(T\\,1)\\,0.1/TB\\,0)"]
    subprocess.run(command + [str(video)], check=True, capture_output=True)
    return video


@pytest.mark.parametrize("codec", ["alac", "aac"])
def test_original_encoded_audio_is_complete_despite_native_video_tail(tmp_path, media_tools, codec):
    source = _mux(tmp_path, media_tools, codec=codec)
    alignment = _extract(source, tmp_path / "alignment.wav", media_tools)
    source_before = source.read_bytes()
    alignment_before = alignment.read_bytes()
    report = coverage.verify_alignment_source(alignment, source, **media_tools)
    assert report["status"] == "pass"
    assert report["pcm_exact_match"] is True
    assert report["timestamps_contiguous"] is True
    assert report["start_seconds"] == 0
    assert abs(3 - report["end_seconds"]) <= 0.05
    assert report["alignment_frames"] == report["authoritative_16k_frames"]
    if codec == "alac":
        assert report["authoritative_16k_frames"] == 48000 - 148
        assert 3 - report["end_seconds"] == pytest.approx(0.00925)
    assert source.read_bytes() == source_before
    assert alignment.read_bytes() == alignment_before


@pytest.mark.parametrize("lost_frames", [1, 148, 500])
def test_valid_wav_header_cannot_hide_processing_loss(tmp_path, media_tools, lost_frames):
    source = _wav(tmp_path / "source.wav")
    alignment = _extract(source, tmp_path / "alignment.wav", media_tools)
    params, pcm = _read(alignment)
    _rewrite(alignment, params, pcm[: -lost_frames * 2])
    with pytest.raises(coverage.AudioCoverageError, match="processing loss"):
        coverage.verify_alignment_source(alignment, source, **media_tools)


def test_same_duration_modified_pcm_is_rejected(tmp_path, media_tools):
    source = _wav(tmp_path / "source.wav")
    alignment = _extract(source, tmp_path / "alignment.wav", media_tools)
    params, pcm = _read(alignment)
    damaged = bytearray(pcm)
    damaged[3333] ^= 1
    _rewrite(alignment, params, damaged)
    with pytest.raises(coverage.AudioCoverageError, match="changed PCM"):
        coverage.verify_alignment_source(alignment, source, **media_tools)


def test_leading_silence_retains_its_samples_and_origin(tmp_path, media_tools):
    source = _wav(tmp_path / "source.wav", silent_frames=16000)
    alignment = _extract(source, tmp_path / "alignment.wav", media_tools)
    report = coverage.verify_alignment_source(alignment, source, **media_tools)
    assert report["start_seconds"] == 0
    assert report["alignment_frames"] == 48000
    assert _read(alignment)[1][:32000] == bytes(32000)
    assert any(_read(alignment)[1][32000:])


@pytest.mark.parametrize("offset,gap,message", [(0.25, False, "nonzero start"), (0, True, "gap")])
def test_original_timestamp_offset_and_internal_gap_are_not_reset(
    tmp_path, media_tools, offset, gap, message
):
    source = _mux(tmp_path, media_tools, offset=offset, gap=gap)
    alignment = _extract(source, tmp_path / "alignment.wav", media_tools)
    with pytest.raises(coverage.AudioCoverageError, match=message):
        coverage.verify_alignment_source(alignment, source, **media_tools)


def test_44100_resample_count_uses_native_samples(tmp_path, media_tools):
    source = _wav(tmp_path / "source.wav", frames=132299, rate=44100)
    alignment = _extract(source, tmp_path / "alignment.wav", media_tools)
    report = coverage.verify_alignment_source(alignment, source, **media_tools)
    assert report["source_native_frames"] == 132299
    assert report["source_native_rate"] == 44100
    assert abs(report["authoritative_16k_frames"] - round(132299 * 16000 / 44100)) <= 1


def test_malformed_truncated_wav_fails_before_decoder(tmp_path, media_tools, monkeypatch):
    source = _wav(tmp_path / "source.wav")
    alignment = _extract(source, tmp_path / "alignment.wav", media_tools)
    alignment.write_bytes(alignment.read_bytes()[:-2])
    monkeypatch.setattr(
        coverage, "_source_frames", lambda *args: pytest.fail("must fail before probe")
    )
    with pytest.raises(coverage.AudioCoverageError, match="truncated"):
        coverage.verify_alignment_source(alignment, source, **media_tools)


def test_source_mutation_during_decode_is_rejected(tmp_path, media_tools, monkeypatch):
    source = _wav(tmp_path / "source.wav")
    alignment = _extract(source, tmp_path / "alignment.wav", media_tools)
    original_decode = coverage._canonical_pcm

    def changing_source(path, ffmpeg):
        result = original_decode(path, ffmpeg)
        with path.open("ab") as stream:
            stream.write(b"changed")
        return result

    monkeypatch.setattr(coverage, "_canonical_pcm", changing_source)
    with pytest.raises(coverage.AudioCoverageError, match="changed during"):
        coverage.verify_alignment_source(alignment, source, **media_tools)


def test_native_probe_receipt_is_json_safe(tmp_path, media_tools):
    import json

    source = _wav(tmp_path / "source.wav")
    report = coverage.probe_source_audio_coverage(source, ffprobe_bin=media_tools["ffprobe_bin"])
    assert (
        json.loads(json.dumps(report))["source_sha256"]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert report["end_seconds"] == 3
