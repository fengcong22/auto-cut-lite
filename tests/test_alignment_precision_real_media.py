"""Offline FFmpeg evidence for diagnostic-only sample rounding at MP4 boundaries."""

import array
import hashlib
import json
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from utils import review_document_runner as runner


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pcm(path):
    with wave.open(str(path), "rb") as stream:
        return stream.getparams(), stream.readframes(stream.getnframes())


def _working_wav(path, samples):
    # Changing nonzero samples prove that rounding keeps the decoded signal.
    data = array.array(
        "h",
        (int(10000 * math.sin(i * 0.071) * (0.4 + (i % 701) / 1400)) for i in range(samples)),
    )
    if sys.byteorder != "little":
        data.byteswap()
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        stream.writeframes(data.tobytes())
    return path


@pytest.fixture
def ffmpeg_tools():
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are needed for real-media integration")
    return ffmpeg, ffprobe


def test_real_resampling_rounds_only_the_diagnostic_copy(tmp_path, ffmpeg_tools):
    working = _working_wav(tmp_path / "working.wav", 48000 * 3)
    original_hash = _sha(working)
    alignment = tmp_path / "alignment.wav"
    extraction = runner.extract_alignment_wav(working, alignment, ffmpeg_bin=ffmpeg_tools[0])
    before_params, before_pcm = _pcm(alignment)
    assert before_params.nframes == 48000
    report = runner._bound_alignment_wav_to_timeline(alignment, 3.0000645)
    after_params, after_pcm = _pcm(alignment)
    assert after_params.nframes == 48001
    assert after_pcm == before_pcm + b"\x00\x00"
    assert report["padded_frames"] == 1
    assert report["shortfall_seconds"] == pytest.approx(0.0000645)
    assert extraction["source_sha256"] == original_hash == _sha(working)


@pytest.mark.parametrize("missing_samples", [1, 2, 160])
def test_real_mp4_sample_gap_accepts_one_and_rejects_missing_audio(
    tmp_path, ffmpeg_tools, missing_samples
):
    ffmpeg, ffprobe = ffmpeg_tools
    # Ninety NTSC frames are 3.003 s. Lossless ALAC stores the exact 48 kHz
    # sample count, yielding one/two/160 absent samples after 16 kHz extraction.
    expected_frames = 48048
    working = _working_wav(tmp_path / "working.wav", (expected_frames - missing_samples) * 3)
    video = tmp_path / "fractional.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=160x90:r=30000/1001:d=3.003",
            "-i",
            str(working),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "alac",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    probe = json.loads(
        subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,duration",
                "-of",
                "json",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    duration = float(
        next(row["duration"] for row in probe["streams"] if row["codec_type"] == "video")
    )
    assert duration == 3.003
    source_hashes = (_sha(video), _sha(working))
    alignment = tmp_path / "alignment.wav"
    runner.extract_alignment_wav(video, alignment, ffmpeg_bin=ffmpeg)
    before_params, before_pcm = _pcm(alignment)
    assert before_params.nframes == expected_frames - missing_samples
    assert any(before_pcm)
    if missing_samples == 1:
        report = runner._bound_alignment_wav_to_timeline(alignment, duration)
        after_params, after_pcm = _pcm(alignment)
        assert after_params.nframes == expected_frames
        assert after_pcm == before_pcm + b"\x00\x00"
        assert report["padded_frames"] == 1
        assert report["timeline_modified"] is False
    else:
        diagnostic_hash = _sha(alignment)
        with pytest.raises(runner.OrderedSourceAsrIntegrityError):
            runner._bound_alignment_wav_to_timeline(alignment, duration)
        assert _sha(alignment) == diagnostic_hash
    assert (_sha(video), _sha(working)) == source_hashes
