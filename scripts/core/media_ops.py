import os
from typing import Optional, Union

import pyJianYingDraft as draft
from pyJianYingDraft import trange
from pyJianYingDraft.exceptions import SegmentOverlap
from utils.formatters import get_duration_ffprobe_cached, safe_tim
from utils.media_normalizer import normalize_webm_for_jianying
from utils.vendor_enums import get_vendor_enum


def _half_canvas(value: float, fallback: float) -> float:
    return (float(value) if value else fallback) / 2.0


class _FallbackPhotoMaterial:
    def __init__(self, path: str):
        self.material_id = os.path.basename(path) + "_photo"
        self.local_material_id = self.material_id
        self.duration = 10_800_000_000
        self.material_name = os.path.basename(path)
        self.path = os.path.abspath(path)
        self.width = 1920
        self.height = 1080
        self.crop_settings = draft.CropSettings()
        self.material_type = "photo"

    def export_json(self):
        return {
            "audio_fade": None,
            "category_id": "",
            "category_name": "local",
            "check_flag": 63487,
            "crop": self.crop_settings.export_json(),
            "crop_ratio": "free",
            "crop_scale": 1.0,
            "duration": self.duration,
            "height": self.height,
            "id": self.material_id,
            "local_material_id": self.local_material_id,
            "material_id": self.material_id,
            "material_name": self.material_name,
            "media_path": "",
            "path": self.path,
            "type": self.material_type,
            "width": self.width,
        }


class MediaOpsMixin:
    def add_media_safe(
        self,
        media_path: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        track_name: str = None,
        source_start: Union[str, int] = 0,
        **kwargs,
    ):
        if not os.path.exists(media_path):
            print(f"Media Missing: {media_path}")
            return None

        ext = os.path.splitext(media_path)[1].lower()
        if ext == ".webm":
            normalized_path = normalize_webm_for_jianying(media_path)
            if not normalized_path:
                print(f"WEBM normalization failed: {media_path}")
                return None
            media_path = normalized_path
            ext = ".mp4"

        if ext in [".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg"]:
            return self.add_audio_safe(media_path, start_time, duration, track_name or "AudioTrack")

        return self._add_video_safe(
            media_path, start_time, duration, track_name or "VideoTrack", source_start=source_start
        )

    def add_audio_safe(
        self,
        media_path: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        track_name: str = "AudioTrack",
        **kwargs,
    ):
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.audio, track_name)

        try:
            mat = draft.AudioMaterial(media_path)
            phys_duration = mat.duration
        except Exception:
            return None

        start_us = safe_tim(start_time)
        actual_duration = self._calculate_duration(duration, phys_duration)
        seg = draft.AudioSegment(
            mat, trange(start_us, actual_duration), source_timerange=trange(0, actual_duration)
        )
        target_track = self._find_available_audio_track_name(track_name, seg)
        self.script.add_segment(seg, target_track)
        return seg

    def _add_video_safe(
        self,
        media_path: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        track_name: str = "VideoTrack",
        source_start: Union[str, int] = 0,
        **kwargs,
    ):
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.video, track_name)

        try:
            fallback_duration_us = safe_tim(duration) * 10 if duration else None
            mat = draft.VideoMaterial(media_path, duration=fallback_duration_us)
            phys_duration = mat.duration
        except Exception:
            return None

        if not phys_duration or phys_duration <= 0:
            ff_dur = get_duration_ffprobe_cached(media_path)
            if ff_dur > 0:
                phys_duration = int(ff_dur * 1000000)
                mat.duration = phys_duration

        if not self._explicit_res and not self._first_video_resolved:
            if hasattr(mat, "width") and mat.width > 0:
                self.script.width, self.script.height = mat.width, mat.height
            self._first_video_resolved = True

        start_us = safe_tim(start_time)
        src_start_us = safe_tim(source_start)
        actual_duration = self._calculate_duration(duration, phys_duration - src_start_us)

        seg = draft.VideoSegment(
            mat,
            trange(start_us, actual_duration),
            source_timerange=trange(src_start_us, actual_duration),
        )
        self.script.add_segment(seg, track_name)
        return seg

    def add_cloud_media(
        self,
        query: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        track_name: str = None,
    ):
        cm = self.cloud_manager
        local_path = cm.download_asset(query)
        if not local_path:
            return None

        asset_info = cm.find_asset(query)
        db_type = str(asset_info.get("type", "")).lower() if asset_info else ""
        source_db = str(asset_info.get("source_db", "")).lower() if asset_info else ""
        is_audio_db = source_db in {"cloud_music_library.csv", "cloud_sound_effects.csv"}
        is_audio_type = any(k in db_type for k in ["music", "audio", "sound", "bgm"])
        is_audio = is_audio_db or is_audio_type

        if is_audio:
            default_track = (
                "BGM"
                if source_db == "cloud_music_library.csv"
                or any(k in db_type for k in ["music", "bgm"])
                else "AudioTrack"
            )
            return self.add_audio_safe(
                local_path, start_time, duration, track_name or default_track
            )
        return self.add_media_safe(local_path, start_time, duration, track_name or "VideoTrack")

    def add_cloud_music(
        self,
        query: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = None,
        name: str = None,
        duration_s: float = None,
        track_name: str = "BGM",
    ):
        if start_time is None:
            start_time = self.get_track_duration(track_name)

        local_path = self.cloud_manager.download_asset(query)
        if local_path and os.path.exists(local_path):
            seg = self.add_audio_safe(
                local_path, start_time=start_time, duration=duration, track_name=track_name
            )
            if seg is not None:
                return seg

        actual_duration_s = duration_s or self.cloud_manager.get_asset_duration(query)
        if not actual_duration_s:
            print(f"Warning: Duration for cloud music '{query}' not found. Using fallback 3.0s")
            actual_duration_s = 3.0

        final_dur_us = safe_tim(duration) if duration else int(actual_duration_s * 1000000)
        dummy_path = (
            local_path
            if (local_path and os.path.exists(local_path))
            else f"cloud_music_{query}.mp3"
        )
        self._cloud_audio_patches[dummy_path] = {"id": query, "type": "music"}

        from core.mocking_ops import MockAudioMaterial

        mat = MockAudioMaterial(query, final_dur_us, name or f"CloudMusic_{query}", dummy_path)
        seg = draft.AudioSegment(
            mat,
            draft.Timerange(safe_tim(start_time), final_dur_us),
            source_timerange=draft.Timerange(0, final_dur_us),
        )
        self._ensure_track(draft.TrackType.audio, track_name)
        target_track = self._find_available_audio_track_name(track_name, seg)
        self.script.add_segment(seg, target_track)
        return seg

    def add_sticker_simple(
        self,
        sticker_id: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "StickerTrack",
        scale: float = 1.0,
        transform_x: float = 0.0,
        transform_y: float = 0.0,
        rotation: float = 0.0,
        alpha: float = 1.0,
    ):
        if getattr(self, "is_patch_mode", False):
            if start_time is None:
                start_time = self.get_track_duration(track_name)
            clip_settings = draft.ClipSettings(
                alpha=alpha,
                rotation=rotation,
                scale_x=scale,
                scale_y=scale,
                transform_x=transform_x / 960.0,
                transform_y=transform_y / 540.0,
            )
            seg = draft.StickerSegment(
                resource_id=str(sticker_id),
                target_timerange=trange(safe_tim(start_time), safe_tim(duration)),
                clip_settings=clip_settings,
            )
            self.patch_session.add_sticker_segment(seg, seg.export_material(), track_name)
            return seg
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.sticker, track_name)
        clip_settings = draft.ClipSettings(
            alpha=alpha,
            rotation=rotation,
            scale_x=scale,
            scale_y=scale,
            transform_x=transform_x / _half_canvas(getattr(self.script, "width", 1920), 1920),
            transform_y=transform_y / _half_canvas(getattr(self.script, "height", 1080), 1080),
        )
        seg = draft.StickerSegment(
            resource_id=str(sticker_id),
            target_timerange=trange(safe_tim(start_time), safe_tim(duration)),
            clip_settings=clip_settings,
        )
        self.script.add_segment(seg, track_name)
        return seg

    def add_image_simple(
        self,
        image_path: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "ImageTrack",
        *,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        transform_x: float = 0.0,
        transform_y: float = 0.0,
        rotation: float = 0.0,
        alpha: float = 1.0,
        background_blur: int = None,
    ):
        if not os.path.exists(image_path):
            print(f"Media Missing: {image_path}")
            return None

        if start_time is None:
            start_time = self.get_track_duration(track_name)

        blur_value = None
        if background_blur is not None:
            blur_levels = {
                1: 0.0625,
                2: 0.375,
                3: 0.75,
                4: 1.0,
            }
            blur_value = blur_levels.get(int(background_blur))
            if blur_value is None:
                raise ValueError(f"Invalid background_blur level: {background_blur}")

        self._ensure_track(draft.TrackType.video, track_name)
        start_us = safe_tim(start_time)
        duration_us = safe_tim(duration)

        try:
            mat = draft.VideoMaterial(image_path)
        except Exception:
            mat = _FallbackPhotoMaterial(image_path)

        clip_settings = draft.ClipSettings(
            alpha=alpha,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            transform_x=transform_x / _half_canvas(getattr(self.script, "width", 1920), 1920),
            transform_y=transform_y / _half_canvas(getattr(self.script, "height", 1080), 1080),
        )
        seg = draft.VideoSegment(
            mat,
            trange(start_us, duration_us),
            source_timerange=trange(0, duration_us),
            clip_settings=clip_settings,
        )
        if blur_value is not None:
            seg.add_background_filling("blur", blur=blur_value)
        self.script.add_segment(seg, track_name)
        return seg

    def add_mask_to_segment(
        self,
        segment,
        mask_type: str,
        *,
        center_x: float = 0.0,
        center_y: float = 0.0,
        size: float = 0.5,
        rotation: float = 0.0,
        feather: float = 0.0,
        invert: bool = False,
        rect_width: float = None,
        round_corner: float = None,
    ):
        if getattr(self, "is_patch_mode", False):
            if segment is None or not hasattr(segment, "data"):
                raise TypeError("Segment does not support patch-mode masks")
            self.patch_session.add_mask_to_segment(
                segment,
                mask_type=mask_type,
                center_x=center_x,
                center_y=center_y,
                size=size,
                rotation=rotation,
                feather=feather,
                invert=invert,
                rect_width=rect_width,
                round_corner=round_corner,
            )
            return segment
        if segment is None or not hasattr(segment, "add_mask"):
            raise TypeError("Segment does not support masks")
        mask_aliases = {
            "line": "线性",
            "linear": "线性",
            "mirror": "镜面",
            "circle": "圆形",
            "rectangle": "矩形",
            "rect": "矩形",
            "heart": "爱心",
            "star": "星形",
        }
        resolved_name = mask_aliases.get(str(mask_type).lower(), mask_type)
        mask_enum = self._resolve_enum(draft.MaskType, resolved_name)
        if not mask_enum:
            raise ValueError(f"Unknown mask type: {mask_type}")
        segment.add_mask(
            mask_enum,
            center_x=center_x,
            center_y=center_y,
            size=size,
            rotation=rotation,
            feather=feather,
            invert=invert,
            rect_width=rect_width,
            round_corner=round_corner,
        )
        return segment

    def add_segment_keyframes(self, segment, keyframes):
        if getattr(self, "is_patch_mode", False):
            if segment is None or not hasattr(segment, "data"):
                raise TypeError("Segment does not support patch-mode keyframes")
            self.patch_session.add_keyframes_to_segment(segment, keyframes)
            return segment
        if segment is None or not hasattr(segment, "add_keyframe"):
            raise TypeError("Segment does not support keyframes")
        property_map = {
            "position_x": draft.KeyframeProperty.position_x,
            "position_y": draft.KeyframeProperty.position_y,
            "rotation": draft.KeyframeProperty.rotation,
            "scale_x": draft.KeyframeProperty.scale_x,
            "scale_y": draft.KeyframeProperty.scale_y,
            "uniform_scale": draft.KeyframeProperty.uniform_scale,
            "alpha": draft.KeyframeProperty.alpha,
            "saturation": draft.KeyframeProperty.saturation,
            "contrast": draft.KeyframeProperty.contrast,
            "brightness": draft.KeyframeProperty.brightness,
            "volume": draft.KeyframeProperty.volume,
            "KFTypePositionX": draft.KeyframeProperty.position_x,
            "KFTypePositionY": draft.KeyframeProperty.position_y,
            "KFTypeRotation": draft.KeyframeProperty.rotation,
            "KFTypeScaleX": draft.KeyframeProperty.scale_x,
            "KFTypeScaleY": draft.KeyframeProperty.scale_y,
            "UNIFORM_SCALE": draft.KeyframeProperty.uniform_scale,
            "KFTypeAlpha": draft.KeyframeProperty.alpha,
            "KFTypeSaturation": draft.KeyframeProperty.saturation,
            "KFTypeContrast": draft.KeyframeProperty.contrast,
            "KFTypeBrightness": draft.KeyframeProperty.brightness,
            "KFTypeVolume": draft.KeyframeProperty.volume,
        }
        for item in keyframes:
            prop = property_map.get(str(item["property"]))
            if not prop:
                raise ValueError(f"Unsupported keyframe property: {item['property']}")
            segment.add_keyframe(prop, safe_tim(item["offset"]), float(item["value"]))
        return segment

    def find_segment_by_id(self, segment_id: str):
        if getattr(self, "is_patch_mode", False):
            return self.patch_session.find_segment(segment_id)
        for track in self.script.tracks.values():
            for segment in getattr(track, "segments", []):
                if getattr(segment, "segment_id", None) == segment_id:
                    return segment
        return None

    def add_audio_effect_to_segment(
        self,
        segment,
        *,
        effect_name: str = None,
        effect_category: str = "scene",
        effect_query: str = None,
        params: list[Optional[float]] = None,
        return_resolved_asset: bool = False,
    ):
        resolved_asset = None
        normalized_category = str(effect_category or "scene").strip().lower()
        if normalized_category == "scene":
            query_category = "audio_effect"
            enum_cls = get_vendor_enum("AudioSceneEffectType")
        elif normalized_category == "tone":
            query_category = "tone_effect"
            enum_cls = get_vendor_enum("ToneEffectType")
        elif normalized_category == "speech_to_song":
            query_category = "speech_to_song"
            enum_cls = get_vendor_enum("SpeechToSongType")
        else:
            raise ValueError(f"Unsupported audio effect_category: {effect_category}")

        if effect_query:
            resolved_value, resolved_asset = self._resolve_asset_write_value(
                effect_query,
                query_category,
                field_name="effect_query",
            )
            effect_name = effect_name or resolved_value

        if not effect_name:
            raise ValueError("add_audio_effect_to_segment requires effect_name or effect_query")

        if getattr(self, "is_patch_mode", False):
            if segment is None or not hasattr(segment, "segment_id"):
                raise TypeError("Segment does not support patch-mode audio effect edits")
            result = self.patch_session.add_audio_effect_to_segment(
                segment,
                effect_name=str(effect_name),
                effect_category=normalized_category,
                params=params,
            )
            if return_resolved_asset:
                return result, resolved_asset
            return result

        if segment is None or not hasattr(segment, "add_effect"):
            raise TypeError("Segment does not support audio effects")
        effect_enum = self._resolve_enum(enum_cls, str(effect_name))
        if not effect_enum:
            raise ValueError(f"Unknown {normalized_category} audio effect: {effect_name}")
        segment.add_effect(effect_enum, params=params)
        if return_resolved_asset:
            return segment, resolved_asset
        return segment

    def add_audio_fade_to_segment(
        self,
        segment,
        *,
        fade_in: Union[str, int] = 0,
        fade_out: Union[str, int] = 0,
    ):
        fade_in_us = safe_tim(fade_in)
        fade_out_us = safe_tim(fade_out)
        if fade_in_us < 0 or fade_out_us < 0:
            raise ValueError("Audio fade durations must be >= 0")

        if getattr(self, "is_patch_mode", False):
            if segment is None or not hasattr(segment, "segment_id"):
                raise TypeError("Segment does not support patch-mode audio fade edits")
            return self.patch_session.add_audio_fade_to_segment(
                segment,
                fade_in_duration=fade_in_us,
                fade_out_duration=fade_out_us,
            )

        if segment is None or not hasattr(segment, "add_fade"):
            raise TypeError("Segment does not support audio fade")
        segment.add_fade(fade_in_us, fade_out_us)
        return segment

    def set_text_for_segment(self, segment, text: str):
        if not getattr(self, "is_patch_mode", False):
            raise NotImplementedError("set_text_for_segment currently requires patch mode")
        if segment is None or not hasattr(segment, "segment_id"):
            raise TypeError("Segment does not support patch-mode text editing")
        self.patch_session.set_text_for_segment(segment.segment_id, text)
        return segment

    def shift_segment(self, segment, offset):
        if not getattr(self, "is_patch_mode", False):
            raise NotImplementedError("shift_segment currently requires patch mode")
        if segment is None or not hasattr(segment, "segment_id"):
            raise TypeError("Segment does not support patch-mode shifting")
        self.patch_session.shift_segment(segment.segment_id, offset)
        return segment

    def trim_segment(self, segment, start_time, duration):
        if not getattr(self, "is_patch_mode", False):
            raise NotImplementedError("trim_segment currently requires patch mode")
        if segment is None or not hasattr(segment, "segment_id"):
            raise TypeError("Segment does not support patch-mode trimming")
        self.patch_session.trim_segment(segment.segment_id, start_time, duration)
        return segment

    def set_segment_speed(self, segment, multiplier: float):
        if not getattr(self, "is_patch_mode", False):
            raise NotImplementedError("set_segment_speed currently requires patch mode")
        if segment is None or not hasattr(segment, "segment_id"):
            raise TypeError("Segment does not support patch-mode speed edits")
        self.patch_session.set_segment_speed(segment.segment_id, multiplier)
        return segment

    def set_segment_volume(self, segment, level: float):
        if not getattr(self, "is_patch_mode", False):
            raise NotImplementedError("set_segment_volume currently requires patch mode")
        if segment is None or not hasattr(segment, "segment_id"):
            raise TypeError("Segment does not support patch-mode volume edits")
        self.patch_session.set_segment_volume(segment.segment_id, level)
        return segment

    def set_segment_opacity(self, segment, alpha: float):
        if not getattr(self, "is_patch_mode", False):
            raise NotImplementedError("set_segment_opacity currently requires patch mode")
        if segment is None or not hasattr(segment, "segment_id"):
            raise TypeError("Segment does not support patch-mode opacity edits")
        self.patch_session.set_segment_opacity(segment.segment_id, alpha)
        return segment

    def _find_available_audio_track_name(self, base_track_name: str, segment) -> str:
        preferred = base_track_name or "AudioTrack"
        if self._track_accepts_segment(preferred, segment):
            return preferred
        index = 1
        while True:
            candidate = f"{preferred}_{index}"
            self._ensure_track(draft.TrackType.audio, candidate)
            if self._track_accepts_segment(candidate, segment):
                return candidate
            index += 1

    def _track_accepts_segment(self, track_name: str, segment) -> bool:
        track = self.script.tracks.get(track_name)
        if track is None:
            return False
        try:
            for existing in track.segments:
                if existing.overlaps(segment):
                    return False
            return True
        except SegmentOverlap:
            return False

    def _ensure_track(self, track_type, track_name):
        if getattr(self, "is_patch_mode", False):
            return
        if not track_name:
            return
        if track_name not in self.script.tracks:
            self.script.add_track(track_type, track_name)

    def _calculate_duration(self, req_dur, phys_dur_available):
        if req_dur:
            req_us = safe_tim(req_dur)
            return min(req_us, phys_dur_available) if phys_dur_available > 0 else req_us
        return phys_dur_available
