import os
import time
from typing import Optional, Union

import pyJianYingDraft as draft
from utils.formatters import safe_tim


class VfxOpsMixin:
    """
    JyProject 的特效与转场 Mixin。
    """

    def add_effect_simple(
        self,
        effect_name: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "EffectTrack",
    ):
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.effect, track_name)

        eff_type = self._resolve_enum(draft.VideoSceneEffectType, effect_name)
        if not eff_type:
            return None

        seg = draft.EffectSegment(
            draft.EffectMaterial(eff_type),
            draft.Timerange(safe_tim(start_time), safe_tim(duration)),
        )
        self.script.add_segment(seg, track_name)
        return seg

    def add_effect_segment(
        self,
        effect_name: str = None,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "EffectTrack",
        *,
        effect_category: str = "scene",
        params: Optional[list[Optional[float]]] = None,
        effect_query: str = None,
        return_resolved_asset: bool = False,
    ):
        resolved_asset = None
        normalized_category = str(effect_category or "scene").strip().lower()
        if normalized_category == "scene":
            query_category = "video_effect"
        elif normalized_category == "character":
            query_category = "video_character_effect"
        else:
            raise ValueError(f"Unsupported effect_category: {effect_category}")

        if effect_query:
            resolved_value, resolved_asset = self._resolve_asset_write_value(
                effect_query,
                query_category,
                field_name="effect_query",
            )
            effect_name = effect_name or resolved_value

        if not effect_name:
            raise ValueError("add_effect_segment requires effect_name or effect_query")

        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.effect, track_name)

        enum_cls = (
            draft.VideoSceneEffectType
            if normalized_category == "scene"
            else draft.VideoCharacterEffectType
        )
        effect_enum = self._resolve_enum(enum_cls, str(effect_name))
        if not effect_enum:
            raise ValueError(f"Unknown {normalized_category} effect: {effect_name}")

        seg = draft.EffectSegment(
            effect_enum, draft.Timerange(safe_tim(start_time), safe_tim(duration)), params=params
        )
        self.script.add_segment(seg, track_name)
        if return_resolved_asset:
            return seg, resolved_asset
        return seg

    def add_video_effect(
        self,
        effect_name: str = None,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "EffectTrack",
        *,
        params: Optional[list[Optional[float]]] = None,
        effect_query: str = None,
        return_resolved_asset: bool = False,
    ):
        return self.add_effect_segment(
            effect_name=effect_name,
            start_time=start_time,
            duration=duration,
            track_name=track_name,
            effect_category="scene",
            params=params,
            effect_query=effect_query,
            return_resolved_asset=return_resolved_asset,
        )

    def add_face_effect(
        self,
        effect_name: str = None,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "EffectTrack",
        *,
        params: Optional[list[Optional[float]]] = None,
        effect_query: str = None,
        return_resolved_asset: bool = False,
    ):
        return self.add_effect_segment(
            effect_name=effect_name,
            start_time=start_time,
            duration=duration,
            track_name=track_name,
            effect_category="character",
            params=params,
            effect_query=effect_query,
            return_resolved_asset=return_resolved_asset,
        )

    def add_transition_simple(
        self,
        transition_name: str,
        video_segment: Optional[draft.VideoSegment] = None,
        duration: Union[str, int] = "1s",
        track_name: Optional[str] = None,
    ):
        # 兼容调用：如果未直接给 segment，则尝试从指定轨道获取最后一个视频片段
        if video_segment is None and track_name:
            track = self.script.tracks.get(track_name)
            if not track or not getattr(track, "segments", None):
                return None
            video_segment = track.segments[-1]

        if video_segment is None:
            return None

        trans_type = self._resolve_enum(draft.TransitionType, transition_name)
        if not trans_type:
            return None

        trans = draft.Transition(trans_type, safe_tim(duration))
        video_segment.add_transition(trans)
        return trans

    def add_web_asset_safe(
        self,
        html_path: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "5s",
        track_name: str = "WebVfxTrack",
        output_dir: Optional[str] = None,
    ):
        from web_recorder import record_web_animation

        if start_time is None:
            start_time = self.get_track_duration(track_name)
        if output_dir is None:
            output_dir = os.path.join(self.root, self.name, "temp_assets")
        os.makedirs(output_dir, exist_ok=True)

        video_output = os.path.join(output_dir, f"web_vfx_{int(time.time())}.webm")
        if record_web_animation(html_path, video_output, max_duration=safe_tim(duration) / 1e6 + 5):
            return self.add_media_safe(video_output, start_time, duration, track_name=track_name)
        return None
