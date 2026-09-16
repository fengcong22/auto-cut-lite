"""Saved draft acceptance must bind audible lanes to the selected working file."""

import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from utils.lite_revision import _validate_lite_content


def _segment(mid, start, duration, volume=1.0):
    return {
        "id": f"{mid}-{start}",
        "material_id": mid,
        "volume": volume,
        "speed": 1.0,
        "source_timerange": {"start": start, "duration": duration},
        "target_timerange": {"start": start, "duration": duration},
    }


def _fixture():
    tracks = [
        {"name": name, "type": kind, "attribute": 0, "segments": segments}
        for name, kind, segments in [
            ("Original Video", "video", [_segment("video", 0, 3_000_000, 0)]),
            ("Lite Cut Segments", "video", []),
            ("Lite Visual Assets", "video", []),
            ("Lite Timing Adjusted", "video", []),
            ("Separated Source Audio", "audio", [_segment("work", 0, 3_000_000)]),
        ]
    ]
    content = {
        "duration": 3_000_000,
        "config": {"maintrack_adsorb": False},
        "tracks": tracks,
        "materials": {
            "videos": [{"id": "video", "path": "C:/fixture/video.mp4"}],
            "audios": [
                {"id": "work", "path": "C:/fixture/restored.wav"},
                {"id": "original", "path": "C:/fixture/original.wav"},
            ],
        },
    }
    return content


def _validate(content):
    return _validate_lite_content(
        content,
        total_duration=3,
        marker_plan=[],
        marker_receipts=[],
        reused_audio_expected=False,
        source_video_material_id="video",
        source_video_path="C:/fixture/video.mp4",
        working_audio_bindings=[
            {
                "pair_index": 0,
                "offset": 0,
                "duration": 3,
                "working_path": "C:/fixture/restored.wav",
                "original_path": "C:/fixture/original.wav",
            }
        ],
    )


def test_clean_saved_working_audio_is_accepted():
    assert _validate(_fixture())["ok"]


@pytest.mark.parametrize(
    "mutation",
    [
        "original",
        "wrong_path",
        "missing_backup",
        "mute",
        "track_mute",
        "video_sound",
        "duplicate",
        "reference_sound",
        "speed",
        "source_duration",
        "material_duration",
        "volume_keyframe",
        "fade",
        "direct_fade",
    ],
)
def test_saved_corruption_is_rejected(mutation):
    content = deepcopy(_fixture())
    audio = content["tracks"][-1]
    segment = audio["segments"][0]
    if mutation == "original":
        segment["material_id"] = "original"
    elif mutation == "wrong_path":
        content["materials"]["audios"][0]["path"] = "C:/fixture/wrong.wav"
    elif mutation == "missing_backup":
        content["materials"]["audios"].pop()
    elif mutation == "mute":
        segment["volume"] = 0
    elif mutation == "track_mute":
        audio["attribute"] = 1
    elif mutation == "video_sound":
        content["tracks"][0]["segments"][0]["volume"] = 1
    elif mutation in {"duplicate", "reference_sound"}:
        content["tracks"].append(
            {
                "name": "Replacement Audio" if mutation == "duplicate" else "Original Reference",
                "type": "audio",
                "segments": [
                    _segment("work" if mutation == "duplicate" else "original", 0, 3_000_000)
                ],
            }
        )
    elif mutation == "speed":
        segment["speed"] = 1.1
    elif mutation == "source_duration":
        segment["source_timerange"]["duration"] -= 100_000
    elif mutation == "material_duration":
        content["materials"]["audios"][0]["duration"] = 2_000_000
    elif mutation == "volume_keyframe":
        segment["common_keyframes"] = [{"property_type": "KFTypeVolume"}]
    elif mutation in {"fade", "direct_fade"}:
        fade = {"id": "unwanted-fade", "fade_in_duration": 3_000_000, "fade_out_duration": 0}
        if mutation == "fade":
            content["materials"]["audio_fades"] = [fade]
            segment["extra_material_refs"] = ["unwanted-fade"]
        else:
            segment["audio_fade"] = fade
    result = _validate(content)
    assert not result["ok"], mutation
    assert any("working audio" in error.lower() for error in result["errors"]), result


@pytest.mark.parametrize("variant", ["root", "active_timeline"])
def test_writer_rejects_corruption_in_each_saved_variant(tmp_path, monkeypatch, variant):
    import json

    from utils import lite_revision
    from utils.revision_runner import execute_revision_request, load_revision_request

    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "SavedVariantRegression",
                    "source_video": "C:/fixture/video.mp4",
                    "source_audio": "C:/fixture/original.wav",
                    "replacement_audio": "C:/fixture/restored.wav",
                    "audio_mode": "replace_original",
                    "media_duration_seconds": 3,
                },
            }
        ),
        encoding="utf-8",
    )
    load = lite_revision._load_content_variants

    def damaged(result):
        content = deepcopy(load(result)[0][1])
        alternate = deepcopy(content)
        target = content if variant == "root" else alternate
        track = next(row for row in target["tracks"] if row["name"] == "Separated Source Audio")
        track["segments"][0]["volume"] = 0
        return [("root", content), ("active_timeline", alternate)]

    monkeypatch.setattr(lite_revision, "_load_content_variants", damaged)
    with pytest.raises((ValueError, RuntimeError), match="working audio"):
        execute_revision_request(
            load_revision_request(str(request_path)),
            drafts_root=str(tmp_path / "drafts"),
            mock_media=True,
            strict=True,
        )
