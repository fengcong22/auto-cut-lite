# ruff: noqa: E402

import os
from unittest.mock import patch

from _bootstrap import ensure_skill_scripts_on_path

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT, _ = ensure_skill_scripts_on_path(CURRENT_DIR)

from core.mocking_ops import MockAudioMaterial, MockVideoMaterial
from jy_wrapper import JyProject


def main() -> None:
    project = JyProject("Draft_Writing_Primitives_Demo", overwrite=True)
    assets_dir = os.path.join(SKILL_ROOT, "assets")
    video_path = os.path.join(assets_dir, "video.mp4")

    if not os.path.exists(video_path):
        print(f"Demo video not found: {video_path}")
        return

    print("Adding base video...")
    video_segment = None
    try:
        video_segment = project.add_media_safe(
            video_path, start_time="0s", duration="4s", track_name="VideoTrack"
        )
    except Exception:
        video_segment = None

    if video_segment is None:
        print("Falling back to mocked video material for the demo.")

        def fake_video_material(path, duration=None):
            return MockVideoMaterial("demo_video_mat", 5_000_000, "DemoVideo", path)

        with patch("core.media_ops.draft.VideoMaterial", side_effect=fake_video_material):
            video_segment = project.add_media_safe(
                video_path,
                start_time="0s",
                duration="4s",
                track_name="VideoTrack",
            )

    print("Adding rich text...")
    project.add_rich_text(
        "Draft writing primitives demo",
        start_time="0.3s",
        duration="3s",
        keyword="primitives|demo",
        keyword_color="#ff7100",
        border_color="#111111",
        shadow={"color": "#111111", "alpha": 0.85, "distance": 6.0},
        font_query="文轩体",
        anim_in_query="复古打字机",
        anim_loop_query="VHS",
    )

    print("Adding sticker...")
    project.add_sticker_simple(
        "7405878923323641129",
        start_time="0.8s",
        duration="2s",
        scale=1.1,
        transform_x=120,
        transform_y=-80,
    )

    print("Adding mask and keyframes to the video segment...")
    project.add_mask_to_segment(
        video_segment,
        "circle",
        center_x=10,
        center_y=-20,
        size=0.3,
        feather=20,
        invert=True,
    )
    project.add_segment_keyframes(
        video_segment,
        [
            {"property": "position_x", "offset": "0.5s", "value": 0.2},
            {"property": "uniform_scale", "offset": "1.5s", "value": 1.1},
        ],
    )

    print("Adding complex text with query-driven bubble/effect/animation...")
    project.add_complex_text(
        "Complex title",
        start_time="2.1s",
        duration="1.5s",
        bubble_query="6838834573413978631",
        text_effect_query="1998",
        anim_loop_query="VHS",
    )

    print("Saving project...")
    project.save()

    print("Reopening the saved draft in patch mode compatible path...")
    reopened = JyProject("Draft_Writing_Primitives_Demo", overwrite=False)
    target_segment_id = None
    if getattr(reopened, "is_patch_mode", False):
        segments = reopened.patch_session.list_segments()
        video_segments = [item for item in segments if item["track_type"] == "video"]
        if video_segments:
            target_segment_id = video_segments[0]["segment_id"]
    elif video_segment is not None:
        target_segment_id = video_segment.segment_id

    if target_segment_id:
        seg_ref = reopened.find_segment_by_id(target_segment_id)
        reopened.add_rich_text(
            "Patched existing draft",
            start_time="3.2s",
            duration="1.5s",
            keyword="Patched",
            keyword_color="#00c2ff",
            font_query="文轩体",
        )
        reopened.add_segment_keyframes(
            seg_ref,
            [{"property": "position_x", "offset": "0.5s", "value": 0.15}],
        )
        reopened.save()

    print("Adding audio primitives on a small voice track...")
    audio_project = JyProject("Draft_Writing_Audio_Primitives_Demo", overwrite=True)

    def fake_audio_material(path):
        return MockAudioMaterial("demo_audio_mat", 3_000_000, "DemoAudio", path)

    with patch("core.media_ops.draft.AudioMaterial", side_effect=fake_audio_material):
        audio_segment = audio_project.add_audio_safe(
            video_path, start_time="0s", duration="2s", track_name="AudioTrack"
        )

    audio_project.add_audio_effect_to_segment(
        audio_segment,
        effect_query="8bit",
        effect_category="scene",
    )
    audio_project.add_audio_fade_to_segment(
        audio_segment,
        fade_in="0.2s",
        fade_out="0.4s",
    )
    audio_project.save()

    print("Done. Open JianYing and find draft: Draft_Writing_Primitives_Demo")


if __name__ == "__main__":
    main()
