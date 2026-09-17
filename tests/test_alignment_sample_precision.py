"""Diagnostic sample quantization must not conceal missing or corrupt audio."""

import json
import shutil
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from audio_sound.volc_asr import VolcAsrConfig
from tests import test_review_document_runner as support
from tests.test_review_document_runner_source_pairs import _WaitStore

runner = support.runner
RATE = 16000


def pcm(path, frames, *, value=17, rate=RATE, channels=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(value.to_bytes(2, "little", signed=True) * frames * channels)
    return path


def read_pcm(path):
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes(), stream.readframes(stream.getnframes())


def test_reported_one_sample_deficit_preserves_every_existing_sample(tmp_path):
    duration = 627.589002
    actual = 10041423
    source = pcm(tmp_path / "alignment.wav", actual)
    original_pcm = read_pcm(source)[1]
    report = runner._bound_alignment_wav_to_timeline(source, duration)
    frames, data = read_pcm(source)
    assert frames == 10041424
    assert data == original_pcm + b"\x00\x00"
    assert report["input_frames"] == actual
    assert report["padded_frames"] == 1
    assert report["input_duration_seconds"] == 627.5889375
    assert report["shortfall_seconds"] == pytest.approx(0.0000645)
    assert report["timeline_modified"] is False


@pytest.mark.parametrize("missing", [2, 160, 16000])
def test_real_shortfall_is_rejected_with_measured_diagnostics(tmp_path, missing):
    source = pcm(tmp_path / "short.wav", RATE * 3 - missing)
    before = source.read_bytes()
    with pytest.raises(runner.OrderedSourceAsrIntegrityError) as caught:
        runner._bound_alignment_wav_to_timeline(source, 3)
    message = str(caught.value)
    assert "expected_frames=48000" in message
    assert f"actual_frames={48000 - missing}" in message
    assert "shortfall_seconds=" in message
    assert source.read_bytes() == before


@pytest.mark.parametrize("tail", [0, 500])
def test_truncated_pcm_is_not_mistaken_for_rounding(tmp_path, tail):
    source = pcm(tmp_path / "truncated.wav", RATE * 3 + tail)
    source.write_bytes(source.read_bytes()[:-2])
    before = source.read_bytes()
    with pytest.raises(runner.OrderedSourceAsrIntegrityError, match="truncated"):
        runner._bound_alignment_wav_to_timeline(source, 3)
    assert source.read_bytes() == before


@pytest.mark.parametrize("rate,channels", [(8000, 1), (16000, 2)])
def test_wrong_pcm_recipe_rejected_before_asr(tmp_path, rate, channels):
    source = pcm(tmp_path / "wrong.wav", rate * 3, rate=rate, channels=channels)
    with pytest.raises(runner.OrderedSourceAsrIntegrityError, match="PCM16"):
        runner._bound_alignment_wav_to_timeline(source, 3)


def test_tail_trim_and_exact_length_preserve_samples(tmp_path):
    source = pcm(tmp_path / "tail.wav", RATE * 3 + 1)
    runner._bound_alignment_wav_to_timeline(source, 3)
    assert read_pcm(source) == (RATE * 3, b"\x11\x00" * RATE * 3)
    before = source.read_bytes()
    runner._bound_alignment_wav_to_timeline(source, 3)
    assert source.read_bytes() == before


def test_legacy_entry_uses_corrected_copy_and_keeps_working_media(tmp_path):
    helper = support.ReviewDocumentRunnerTests()
    snapshot, project = helper._audio_inputs(tmp_path)
    original = tmp_path / "source.wav"
    # The one-point shortfall belongs to the original, not to extraction loss.
    pcm(original, RATE * 3 - 1)
    before = original.read_bytes()
    with helper._patched_runtime():
        result = helper._run(
            snapshot,
            project,
            job_root=tmp_path / "job",
            drafts_root=tmp_path / "drafts",
            package_zip=tmp_path / "delivery.zip",
            cache_root=tmp_path / "cache",
        )
    assert result["ok"]
    index = json.loads((tmp_path / "job/workspace/evidence/source_asr_index.json").read_text())
    assert index["alignment_timeline_adjustment"]["padded_frames"] == 1
    assert original.read_bytes() == before


@pytest.mark.parametrize("duration", [1.00003, 1.00004])
def test_many_pairs_keep_cumulative_boundaries_and_cached_sources(tmp_path, duration):
    # Independent rounding accumulates nearly half a frame per pair in either direction.
    sources = []
    for i in range(20):
        source = pcm(tmp_path / f"source_{i}.wav", round(RATE * duration) - 1, value=i + 1)
        sources.append(
            {
                "pair_index": i,
                "offset": i * duration,
                "duration": duration,
                "path": str(source),
                "sha256": runner.sha256_file(source),
            }
        )
    hashes = [row["sha256"] for row in sources]
    asr_inputs = []

    def recognize(cache, identity, *, audio_path, **kw):
        asr_inputs.append(Path(audio_path))
        return {
            "provider": "test",
            "resource_id": "test",
            "adapter_version": "test",
            "input_sha256": runner.sha256_file(Path(audio_path)),
            "words": [{"text": "word", "start": 0.1, "end": 0.2}],
        }, False

    with (
        patch.object(
            runner,
            "extract_alignment_wav",
            side_effect=lambda source, output, **kw: shutil.copyfile(source, output),
        ),
        patch.object(runner, "_cached_asr_json", side_effect=recognize),
    ):
        asr, index, _, _ = runner._run_ordered_source_asr(
            sources,
            materials_dir=tmp_path / "materials",
            alignment_output=tmp_path / "combined.wav",
            source_asr_output=tmp_path / "asr.json",
            cache=runner.ArtifactCache(tmp_path / "cache"),
            inflight_root=tmp_path / "inflight",
            ffmpeg_bin="unused",
            ffmpeg_info={"version": "test"},
            config=VolcAsrConfig(api_key="test"),
            asr_timeout_seconds=1,
            asr_poll_interval_seconds=0.01,
            asr_max_wait_seconds=1,
            store=_WaitStore(),
            mock_media=True,
        )
    frames, data = read_pcm(tmp_path / "combined.wav")
    assert frames == round(20 * duration * RATE)
    for i, row in enumerate(sources):
        start = round(row["offset"] * RATE)
        assert int.from_bytes(data[start * 2 : start * 2 + 2], "little", signed=True) == i + 1
        assert asr["words"][i]["start"] == round(row["offset"] + 0.1, 6)
        assert runner.sha256_file(Path(row["path"])) == hashes[i]
        assert asr["pair_asr"][i]["source_asr_input_sha256"] == runner.sha256_file(asr_inputs[i])
    assert index["alignment_concatenation"]["output_frames"] == frames
    for row in index["alignment_concatenation"]["pairs"]:
        assert abs(row["boundary_error_seconds"]) <= 0.5 / RATE + 1e-12
        assert abs(row["grid_adjustment_frames"]) <= 1
    cached_wavs = list((tmp_path / "cache").rglob("*.wav"))
    assert len(cached_wavs) == len(sources)
    assert {runner.sha256_file(p) for p in cached_wavs} == set(hashes)
    candidate = tmp_path / "candidate.wav"
    runner.render_source_aligned_candidate(
        tmp_path / "combined.wav",
        candidate,
        delete_windows=[{"item_id": "spoken", "start": 0.1, "end": 0.2}],
    )
    assert read_pcm(candidate)[0] == frames


def test_sample_fix_invalidates_old_phase_receipts():
    assert runner.RUNNER_VERSION != "auto-cut-lite-review-document-run-v10-working-audio"


@pytest.mark.parametrize(
    "old_version",
    [
        "auto-cut-lite-review-document-run-v10-working-audio",
        "auto-cut-lite-review-document-run-v11-sample-precision",
    ],
)
def test_old_source_asr_phase_is_revalidated_after_upgrade(tmp_path, old_version):
    helper = support.ReviewDocumentRunnerTests()
    snapshot, project = helper._audio_inputs(tmp_path)
    args = dict(
        job_root=tmp_path / "job",
        drafts_root=tmp_path / "drafts",
        package_zip=tmp_path / "delivery.zip",
        cache_root=tmp_path / "cache",
    )
    with helper._patched_runtime() as mocks:
        helper._run(snapshot, project, **args)
        state_path = tmp_path / "job/job_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["tool_version"] = old_version
        for phase in state["phases"].values():
            phase["tool_version"] = state["tool_version"]
        support._write_json(state_path, state)
        resumed = helper._run(snapshot, project, **args)
        assert resumed["phases"]["source_asr"]["status"] == "complete"
        assert mocks["execute"].call_count == 2
        index = json.loads((tmp_path / "job/workspace/evidence/source_asr_index.json").read_text())
        assert index["alignment_timeline_adjustment"]["allowed_missing_frames"] == 800
        assert index["alignment_timeline_adjustment"]["source_integrity"]["pcm_exact_match"]
        assert mocks["integrity"].call_count == 2


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_timeline_fails_as_integrity_error(tmp_path, duration):
    source = pcm(tmp_path / "alignment.wav", RATE)
    with pytest.raises(runner.OrderedSourceAsrIntegrityError):
        runner._bound_alignment_wav_to_timeline(source, duration)


def test_empty_wav_cannot_be_replaced_with_one_silent_frame(tmp_path):
    source = pcm(tmp_path / "empty.wav", 0)
    with pytest.raises(runner.OrderedSourceAsrIntegrityError):
        runner._bound_alignment_wav_to_timeline(source, 1 / RATE)
