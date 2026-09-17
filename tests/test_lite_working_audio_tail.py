"""A proven native tail must not move edits or overrun working audio material."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from utils.review_audio_precision import build_lite_split_gap_audio_plan
from utils.revision_runner import execute_revision_request, load_revision_request

from tests.test_lite_working_audio import _delete
from tests.test_working_audio_sync import signal, wav


def _tail_sources(tmp_path, tail_samples):
    data = signal(rate=16000)[:-tail_samples]
    original = wav(tmp_path / "original.wav", data, rate=16000)
    restored = wav(tmp_path / "restored.wav", data * 0.7, rate=16000)
    video = tmp_path / "source.mov"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=160x90:r=25:d=8",
            "-i",
            str(original),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    return video, original, restored


@pytest.mark.parametrize("paired", [False, True])
def test_native_tail_preserves_editable_project_duration_and_pair_offsets(tmp_path, paired):
    video, original, restored = _tail_sources(tmp_path, 148)

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    project = {
        "draft_name": "NativeTailPairs" if paired else "NativeTailSingle",
        "source_video": str(video),
        "source_audio": str(original),
        "replacement_audio": str(restored),
        "audio_mode": "replace_original",
        "media_duration_seconds": 16 if paired else 8,
    }
    if paired:
        project["source_pairs"] = [
            {
                "pair_index": index,
                "video_path": str(video),
                "video_sha256": sha(video),
                "source_audio_path": str(original),
                "source_audio_sha256": sha(original),
                "replacement_audio_path": str(restored),
                "replacement_audio_sha256": sha(restored),
                "video_duration_seconds": 8,
                "audio_duration_seconds": 7.99075,
                "audio_mode": "replace_original",
            }
            for index in range(2)
        ]
    edits = [_delete("first", 1, 2)]
    if paired:
        edits.append(_delete("second", 9, 10))
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps({"workflow_mode": "lite", "project": project, "edits": edits}), encoding="utf-8"
    )
    result = execute_revision_request(
        load_revision_request(str(request_path)),
        drafts_root=str(tmp_path / "drafts"),
        mock_media=False,
        localize_materials=True,
        runtime_integrity_receipt={"status": "pass", "source": "offline-source-test"},
    )
    assert result["validation"]["ok"], result["validation"]
    draft = Path(result["draft_path"])
    content = json.loads((draft / "draft_content.json").read_text(encoding="utf-8"))
    assert content["duration"] == (16 if paired else 8) * 1_000_000
    materials = {row["id"]: row for row in content["materials"]["audios"]}
    audible = [
        segment
        for track in content["tracks"]
        if track["type"] == "audio"
        for segment in track["segments"]
        if segment["volume"] > 0
    ]
    for segment in audible:
        source, target = segment["source_timerange"], segment["target_timerange"]
        material = materials[segment["material_id"]]
        assert sha(Path(material["path"])) == sha(restored)
        assert source["start"] + source["duration"] <= material["duration"]
        assert source["start"] + source["duration"] <= 7_990_750
        assert target["start"] == source["start"] + (
            8_000_000 if target["start"] >= 8_000_000 else 0
        )
    assert any(segment["target_timerange"]["start"] == 1_000_000 for segment in audible)
    if paired:
        assert any(segment["target_timerange"]["start"] == 9_000_000 for segment in audible)


def _segmented_tail_payload(tmp_path, tail_samples, audio_mode="replace_original"):
    video, original, restored = _tail_sources(tmp_path, tail_samples)
    working = restored if audio_mode == "replace_original" else original
    candidate = wav(tmp_path / "candidate.wav", signal(rate=16000), rate=16000)
    plan = build_lite_split_gap_audio_plan(
        {
            "source_duration_seconds": 8,
            "executable_cuts": [{"item_id": "first", "start": 1, "end": 2}],
        },
        source_audio_path=working,
        candidate_audio_path=candidate,
        source_audio_duration_seconds=8 - tail_samples / 16000,
    )
    return {
        "workflow_mode": "lite",
        "project": {
            "draft_name": "SegmentedNativeTail",
            "source_video": str(video),
            "source_audio": str(original),
            "replacement_audio": str(restored),
            "audio_mode": audio_mode,
            "media_duration_seconds": 8,
        },
        "edits": [_delete("first", 1, 2)],
        "audio_delivery_plan": plan,
    }


def _write_segmented(tmp_path, payload, *, mock_media=False):
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return execute_revision_request(
        load_revision_request(str(request_path)),
        drafts_root=str(tmp_path / "drafts"),
        mock_media=mock_media,
        localize_materials=True,
        runtime_integrity_receipt={"status": "pass", "source": "offline-source-test"},
    )


@pytest.mark.parametrize("tail_samples", [500, 800])
@pytest.mark.parametrize("audio_mode", ["video_original", "replace_original"])
def test_real_segmented_tail_preserves_complete_audio_coverage(tmp_path, tail_samples, audio_mode):
    payload = _segmented_tail_payload(tmp_path, tail_samples, audio_mode)
    result = _write_segmented(tmp_path, payload)
    assert result["validation"]["ok"], result["validation"]
    assert result["segmented_audio_native_coverage"]["status"] == "pass"
    assert (
        result["segmented_audio_native_coverage"]["source_native_frames"] == 128000 - tail_samples
    )
    draft = Path(result["draft_path"])
    content = json.loads((draft / "draft_content.json").read_text(encoding="utf-8"))
    assert content["duration"] == 8_000_000
    audible = [
        (track["name"], segment)
        for track in content["tracks"]
        if track["type"] == "audio"
        for segment in track["segments"]
        if segment["volume"] > 0
    ]
    assert len(audible) == 3
    assert sum(name == "Lite Reused Audio" for name, _ in audible) == 1
    cursor = 0
    for _, segment in sorted(audible, key=lambda row: row[1]["source_timerange"]["start"]):
        source, target = segment["source_timerange"], segment["target_timerange"]
        assert source == target
        assert source["start"] == cursor
        cursor += source["duration"]
    assert cursor == round((8 - tail_samples / 16000) * 1_000_000)
    assert sum(segment["target_timerange"]["duration"] for _, segment in audible) == cursor


@pytest.mark.parametrize("lost_samples", [1, 148, 500])
def test_segmented_plan_cannot_discard_real_audio_inside_tail_allowance(tmp_path, lost_samples):
    payload = _segmented_tail_payload(tmp_path, 500)
    final_a1 = max(
        payload["audio_delivery_plan"]["segments"],
        key=lambda row: row["source_start"],
    )
    final_a1["duration"] -= lost_samples / 16000
    with pytest.raises(ValueError, match="complete verified source audio coverage"):
        _write_segmented(tmp_path, payload)
    assert not list((tmp_path / "drafts").rglob("draft_content.json"))


def test_mock_segmented_tail_has_no_native_coverage_exception(tmp_path):
    payload = _segmented_tail_payload(tmp_path, 500)
    with pytest.raises(ValueError, match="independent source-aligned"):
        _write_segmented(tmp_path, payload, mock_media=True)
