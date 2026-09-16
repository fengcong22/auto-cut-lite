"""Offline real-media draft + ZIP evidence; ASR boundaries are test fixtures."""

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.test_lite_working_audio import _delete
from tests.test_working_audio_sync import signal, wav

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from utils.lite_package import package_lite_delivery
from utils.revision_runner import execute_revision_request, load_revision_request


@pytest.mark.parametrize("mixed", [False, True])
def test_real_media_editable_draft_and_zip_keep_working_source(tmp_path, mixed):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is needed for real-media integration")
    original = wav(tmp_path / "original.wav", signal())
    restored = wav(tmp_path / "restored.wav", signal() * 0.7)
    video = tmp_path / "source.mp4"
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
            "aac",
            "-t",
            "8",
            str(video),
        ],
        check=True,
        capture_output=True,
    )

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    project = {
        "draft_name": "RealWorkingAudioMixed" if mixed else "RealWorkingAudio",
        "source_video": str(video),
        "source_audio": str(original),
        "replacement_audio": str(restored),
        "audio_mode": "replace_original",
        "media_duration_seconds": 16 if mixed else 8,
    }
    if mixed:
        project["source_pairs"] = [
            {
                "pair_index": 0,
                "video_path": str(video),
                "video_sha256": sha(video),
                "source_audio_path": str(original),
                "source_audio_sha256": sha(original),
                "replacement_audio_path": str(restored),
                "replacement_audio_sha256": sha(restored),
                "video_duration_seconds": 8,
                "audio_duration_seconds": 8,
                "audio_mode": "replace_original",
            },
            {
                "pair_index": 1,
                "video_path": str(video),
                "video_sha256": sha(video),
                "source_audio_path": str(original),
                "source_audio_sha256": sha(original),
                "video_duration_seconds": 8,
                "audio_mode": "video_original",
            },
        ]
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "workflow_mode": "lite",
                "project": project,
                "edits": [_delete("one", 1, 2), _delete("two", 2, 3)],
            }
        ),
        encoding="utf-8",
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
    assert content["duration"] == (16 if mixed else 8) * 1_000_000
    materials = {row["id"]: Path(row["path"]) for row in content["materials"]["audios"]}
    for track in content["tracks"]:
        if track["type"] != "audio":
            continue
        for segment in track["segments"]:
            expected = original if segment["target_timerange"]["start"] >= 8_000_000 else restored
            assert sha(materials[segment["material_id"]]) == sha(expected)
            assert segment["volume"] == 1
    assert sha(original) in {sha(path) for path in materials.values()}
    receipt = package_lite_delivery(draft, tmp_path / f"{draft.name}.zip")
    assert receipt["schema_version"] == 2
    assert receipt["zip_crc_pass"] and receipt["zip_tree_identity_pass"]
    assert receipt["package_tree_sha256"] == receipt["extracted_tree_sha256"]
    with zipfile.ZipFile(receipt["archive_path"]) as archive:
        assert archive.testzip() is None
        packaged = [entry for entry in archive.namelist() if "/Resources/local/" in entry]
        assert any(
            hashlib.sha256(archive.read(entry)).hexdigest() == sha(restored) for entry in packaged
        )
        assert any(
            hashlib.sha256(archive.read(entry)).hexdigest() == sha(original) for entry in packaged
        )
