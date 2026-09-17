"""Only independently proved native tails receive the 50 ms allowance."""

import array
import math
import shutil
import subprocess
import sys
import wave
from unittest.mock import patch

import pytest

from audio_sound.volc_asr import VolcAsrConfig
from tests import test_review_document_runner as support
from tests.test_review_document_runner_source_pairs import _WaitStore

runner = support.runner
RATE = 16000
TIMELINE_SECONDS = 3.0


@pytest.fixture
def media_tools():
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("Tail compatibility integration tests require FFmpeg and FFprobe")
    return {"ffmpeg_bin": ffmpeg, "ffprobe_bin": ffprobe}


def _native_media(tmp_path, media_tools, *, missing=148, offset=0.0):
    # One second of actual silent samples precedes the sound. ALAC preserves the
    # native 48 kHz sample count, including an exact 148/500/800 point 16 kHz tail.
    raw = tmp_path / "native.wav"
    samples = array.array(
        "h",
        (
            0 if index < 48000 else int(10000 * math.sin(index * 0.043))
            for index in range((48000 - missing) * 3)
        ),
    )
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(raw), "wb") as stream:
        stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
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
            str(raw),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "alac",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    alignment = tmp_path / "alignment.wav"
    runner.extract_alignment_wav(source, alignment, ffmpeg_bin=media_tools["ffmpeg_bin"])
    return source, alignment


def _pcm(path):
    with wave.open(str(path), "rb") as stream:
        return stream.getparams(), stream.readframes(stream.getnframes())


def _lose_samples(path, count):
    params, data = _pcm(path)
    with wave.open(str(path), "wb") as stream:
        stream.setparams(params)
        stream.writeframes(data[: -count * 2])


@pytest.mark.parametrize("missing", [148, 500, 800])
def test_proved_native_tail_pads_diagnostic_without_moving_sound(tmp_path, media_tools, missing):
    source, alignment = _native_media(tmp_path, media_tools, missing=missing)
    source_before = source.read_bytes()
    params, input_pcm = _pcm(alignment)
    report = runner._bound_alignment_wav_to_timeline(
        alignment, TIMELINE_SECONDS, source_path=source, **media_tools
    )
    output_params, output_pcm = _pcm(alignment)
    assert params.nframes == 48000 - missing
    assert output_params.nframes == 48000
    assert output_pcm == input_pcm + bytes(missing * 2)
    # Resampling can ring immediately around the sound boundary, but leaves the
    # long leading silence and every existing sample at its original position.
    assert output_pcm[: RATE * 2 - 100] == bytes(RATE * 2 - 100)
    assert any(output_pcm[RATE * 2 :])
    assert source.read_bytes() == source_before
    assert report["padded_frames"] == missing
    assert report["allowed_missing_frames"] == 800
    assert report["tail_tolerance_seconds"] == 0.05
    assert report["timeline_modified"] is False
    assert report["source_integrity"]["pcm_exact_match"] is True
    assert report["source_integrity"]["source_sha256"] == runner.sha256_file(source)


def test_original_tail_beyond_50ms_fails_without_modifying_alignment(tmp_path, media_tools):
    source, alignment = _native_media(tmp_path, media_tools, missing=801)
    before = alignment.read_bytes()
    with pytest.raises(runner.OrderedSourceAsrIntegrityError, match="exceeds 50 ms"):
        runner._bound_alignment_wav_to_timeline(
            alignment, TIMELINE_SECONDS, source_path=source, **media_tools
        )
    assert alignment.read_bytes() == before


@pytest.mark.parametrize("lost", [1, 148, 500])
def test_processing_loss_is_rejected_even_within_tail_allowance(tmp_path, media_tools, lost):
    source, alignment = _native_media(tmp_path, media_tools, missing=148)
    _lose_samples(alignment, lost)
    before = alignment.read_bytes()
    assert 148 + lost < 800
    with pytest.raises(runner.OrderedSourceAsrIntegrityError, match="processing loss"):
        runner._bound_alignment_wav_to_timeline(
            alignment, TIMELINE_SECONDS, source_path=source, **media_tools
        )
    assert alignment.read_bytes() == before


def test_nonzero_audio_origin_cannot_be_hidden_by_tail_padding(tmp_path, media_tools):
    source, alignment = _native_media(tmp_path, media_tools, offset=0.25)
    before = alignment.read_bytes()
    with pytest.raises(runner.OrderedSourceAsrIntegrityError, match="nonzero start"):
        runner._bound_alignment_wav_to_timeline(
            alignment, TIMELINE_SECONDS, source_path=source, **media_tools
        )
    assert alignment.read_bytes() == before


def test_ordered_pair_verifies_original_instead_of_damaged_intermediate(tmp_path, media_tools):
    source, intermediate = _native_media(tmp_path, media_tools)
    _lose_samples(intermediate, 148)
    with patch.object(runner, "_cached_asr_json") as asr:
        with pytest.raises(runner.OrderedSourceAsrIntegrityError, match="processing loss"):
            runner._run_ordered_source_asr(
                [
                    {
                        "pair_index": 0,
                        "offset": 0.0,
                        "duration": TIMELINE_SECONDS,
                        "path": str(intermediate),
                        "sha256": runner.sha256_file(intermediate),
                        "integrity_source_path": str(source),
                        "integrity_source_sha256": runner.sha256_file(source),
                    }
                ],
                materials_dir=tmp_path / "materials",
                alignment_output=tmp_path / "combined.wav",
                source_asr_output=tmp_path / "asr.json",
                cache=runner.ArtifactCache(tmp_path / "cache"),
                inflight_root=tmp_path / "inflight",
                ffmpeg_info={"version": "real-test"},
                config=VolcAsrConfig(api_key="test"),
                asr_timeout_seconds=1,
                asr_poll_interval_seconds=0.01,
                asr_max_wait_seconds=1,
                store=_WaitStore(),
                **media_tools,
            )
    asr.assert_not_called()


def test_split_gap_plan_stops_at_native_audio_end_while_candidate_covers_video(
    tmp_path, media_tools
):
    source, alignment = _native_media(tmp_path, media_tools)
    native_duration = _pcm(alignment)[0].nframes / RATE
    runner._bound_alignment_wav_to_timeline(
        alignment, TIMELINE_SECONDS, source_path=source, **media_tools
    )
    cuts = [{"item_id": "spoken", "start": 0.5, "end": 0.75}]
    candidate = tmp_path / "candidate.wav"
    runner.render_source_aligned_candidate(alignment, candidate, delete_windows=cuts)
    plan = runner.build_lite_split_gap_audio_plan(
        {"source_duration_seconds": TIMELINE_SECONDS, "executable_cuts": cuts},
        source_audio_path=source,
        candidate_audio_path=candidate,
        source_audio_duration_seconds=native_duration,
    )
    segments = sorted(plan["segments"], key=lambda row: row["timeline_start"])
    assert [(row["timeline_start"], row["duration"]) for row in segments] == [
        (0.0, 0.5),
        (0.5, 0.25),
        (0.75, native_duration - 0.75),
    ]
    assert all(row["source_start"] == row["timeline_start"] for row in segments)
    assert all(row["asset_path"] == str(source.resolve()) for row in segments)
    assert segments[1]["track_name"] == "Lite Reused Audio"
    assert segments[1]["volume"] == 1.0
    assert _pcm(candidate)[0].nframes == 48000
    assert plan["validation_only_audio_metadata"]["delivery_eligible"] is False
    assert plan["validation_only_audio_metadata"]["duration_matches_source"] is True
