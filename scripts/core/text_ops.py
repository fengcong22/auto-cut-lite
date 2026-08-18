import asyncio
import os
import re
import threading
from typing import Dict, Optional, Union

import pyJianYingDraft as draft
from utils.asset_index import AssetIndex
from utils.formatters import safe_tim


def _hex_to_rgb(hex_color: str):
    value = (hex_color or "").strip().lstrip("#")
    if len(value) != 6:
        return (1.0, 1.0, 1.0)
    try:
        return (
            int(value[0:2], 16) / 255.0,
            int(value[2:4], 16) / 255.0,
            int(value[4:6], 16) / 255.0,
        )
    except ValueError:
        return (1.0, 1.0, 1.0)


def _half_canvas_width(value: float = 1920.0) -> float:
    return (float(value) if value else 1920.0) / 2.0


class TextOpsMixin:
    def _asset_index(self) -> AssetIndex:
        skill_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return AssetIndex(skill_root=skill_root)

    def _resolve_asset_write_value(
        self,
        query: Optional[str],
        category: str,
        *,
        field_name: str,
        allow_none: bool = True,
    ):
        if not query:
            return None, None
        match = self._asset_index().resolve(query, category=category)
        value = match.get("write_value")
        if not value and not allow_none:
            raise LookupError(f"Resolved asset missing write_value for {field_name}: {query!r}")
        return value, match

    def add_text_simple(
        self,
        text: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "Subtitles",
        **kwargs,
    ):
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.text, track_name)

        start_us = safe_tim(start_time)
        dur_us = safe_tim(duration)

        anim_in = kwargs.pop("anim_in", None)
        anim_out = kwargs.pop("anim_out", None)
        anim_loop = kwargs.pop("anim_loop", None)
        anim_in_duration = kwargs.pop("anim_in_duration", None)
        anim_out_duration = kwargs.pop("anim_out_duration", None)
        anim_loop_duration = kwargs.pop("anim_loop_duration", None)

        allowed_keys = {"font", "style", "clip_settings", "border", "background", "shadow"}
        text_kwargs = {k: v for k, v in kwargs.items() if k in allowed_keys}
        if "style" not in text_kwargs:
            text_kwargs["style"] = draft.TextStyle(size=5.0)
        if "border" not in text_kwargs:
            text_kwargs["border"] = draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=1.0, width=40.0)
        if "clip_settings" not in text_kwargs:
            text_kwargs["clip_settings"] = draft.ClipSettings(transform_y=-0.8)

        seg = draft.TextSegment(text, draft.Timerange(start_us, dur_us), **text_kwargs)

        if anim_in:
            intro_enum = self._resolve_enum(draft.TextIntro, str(anim_in))
            if intro_enum:
                seg.add_animation(intro_enum, duration=anim_in_duration)
        if anim_out:
            outro_enum = self._resolve_enum(draft.TextOutro, str(anim_out))
            if outro_enum:
                seg.add_animation(outro_enum, duration=anim_out_duration)
        if anim_loop:
            loop_enum = self._resolve_enum(draft.TextLoopAnim, str(anim_loop))
            if loop_enum:
                seg.add_animation(loop_enum, duration=anim_loop_duration)

        self.script.add_segment(seg, track_name)
        return seg

    def add_rich_text(
        self,
        text: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "Subtitles",
        *,
        text_color: str = "#ffffff",
        border_color: str = None,
        font: str = None,
        font_size: float = 5.0,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        transform_x: float = 0.0,
        transform_y: float = -0.8,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        keyword: str = None,
        keyword_color: str = "#ff7100",
        keyword_font_size: float = None,
        keyword_border_color: str = None,
        shadow: Optional[Dict[str, float]] = None,
        text_effect: str = None,
        anim_in: str = None,
        anim_out: str = None,
        anim_loop: str = None,
        font_query: str = None,
        anim_in_query: str = None,
        anim_out_query: str = None,
        anim_loop_query: str = None,
        text_effect_query: str = None,
        return_resolved_assets: bool = False,
    ):
        resolved_assets: Dict[str, Dict[str, object]] = {}
        resolved_value, resolved_asset = self._resolve_asset_write_value(
            font_query, "font", field_name="font_query"
        )
        if resolved_asset:
            resolved_assets["font"] = resolved_asset
            font = font or resolved_value
        resolved_value, resolved_asset = self._resolve_asset_write_value(
            anim_in_query,
            "text_animation",
            field_name="anim_in_query",
        )
        if resolved_asset:
            resolved_assets["anim_in"] = resolved_asset
            anim_in = anim_in or resolved_value
        resolved_value, resolved_asset = self._resolve_asset_write_value(
            anim_out_query,
            "text_outro_animation",
            field_name="anim_out_query",
        )
        if resolved_asset:
            resolved_assets["anim_out"] = resolved_asset
            anim_out = anim_out or resolved_value
        resolved_value, resolved_asset = self._resolve_asset_write_value(
            anim_loop_query,
            "text_loop_animation",
            field_name="anim_loop_query",
        )
        if resolved_asset:
            resolved_assets["anim_loop"] = resolved_asset
            anim_loop = anim_loop or resolved_value
        resolved_value, resolved_asset = self._resolve_asset_write_value(
            text_effect_query,
            "video_effect",
            field_name="text_effect_query",
        )
        if resolved_asset:
            resolved_assets["text_effect"] = resolved_asset
            text_effect = text_effect or resolved_value

        if getattr(self, "is_patch_mode", False):
            style = draft.TextStyle(
                size=font_size,
                color=_hex_to_rgb(text_color),
                bold=bold,
                italic=italic,
                underline=underline,
                align=1,
                auto_wrapping=True,
            )
            clip_settings = draft.ClipSettings(
                scale_x=scale_x,
                scale_y=scale_y,
                transform_x=transform_x / 960.0,
                transform_y=transform_y,
            )
            border = (
                draft.TextBorder(color=_hex_to_rgb(border_color), alpha=1.0, width=40.0)
                if border_color
                else None
            )
            font_enum = self._resolve_enum(draft.FontType, font) if font else None
            shadow_obj = None
            if shadow:
                shadow_obj = draft.TextShadow(
                    color=_hex_to_rgb(shadow.get("color", "#000000")),
                    alpha=float(shadow.get("alpha", 1.0)),
                    diffuse=float(shadow.get("diffuse", 15.0)),
                    distance=float(shadow.get("distance", 5.0)),
                    angle=float(shadow.get("angle", -45.0)),
                )
            segment = draft.TextSegment(
                text,
                draft.Timerange(
                    safe_tim(start_time or self.get_track_duration(track_name)), safe_tim(duration)
                ),
                font=font_enum,
                style=style,
                clip_settings=clip_settings,
                border=border,
                shadow=shadow_obj,
            )
            if text_effect:
                segment.add_effect(str(text_effect))
            if anim_in:
                intro_enum = self._resolve_enum(draft.TextIntro, str(anim_in))
                if intro_enum:
                    segment.add_animation(intro_enum)
            if anim_out:
                outro_enum = self._resolve_enum(draft.TextOutro, str(anim_out))
                if outro_enum:
                    segment.add_animation(outro_enum)
            if anim_loop:
                loop_enum = self._resolve_enum(draft.TextLoopAnim, str(anim_loop))
                if loop_enum:
                    segment.add_animation(loop_enum)
            if keyword:
                self._apply_keyword_highlight(
                    segment,
                    keyword,
                    keyword_color=keyword_color,
                    keyword_font_size=keyword_font_size or font_size,
                    keyword_border_color=keyword_border_color or border_color,
                )
            self.patch_session.add_text_segment(segment, segment.export_material(), track_name)
            if return_resolved_assets:
                return segment, resolved_assets
            return segment
        style = draft.TextStyle(
            size=font_size,
            color=_hex_to_rgb(text_color),
            bold=bold,
            italic=italic,
            underline=underline,
            align=1,
            auto_wrapping=True,
        )
        clip_settings = draft.ClipSettings(
            scale_x=scale_x,
            scale_y=scale_y,
            transform_x=transform_x / _half_canvas_width(getattr(self.script, "width", 1920)),
            transform_y=transform_y,
        )
        border = (
            draft.TextBorder(color=_hex_to_rgb(border_color), alpha=1.0, width=40.0)
            if border_color
            else None
        )

        font_enum = None
        if font:
            font_enum = self._resolve_enum(draft.FontType, font)

        shadow_obj = None
        if shadow:
            shadow_obj = draft.TextShadow(
                color=_hex_to_rgb(shadow.get("color", "#000000")),
                alpha=float(shadow.get("alpha", 1.0)),
                diffuse=float(shadow.get("diffuse", 15.0)),
                distance=float(shadow.get("distance", 5.0)),
                angle=float(shadow.get("angle", -45.0)),
            )

        seg = self.add_text_simple(
            text,
            start_time=start_time,
            duration=duration,
            track_name=track_name,
            style=style,
            border=border,
            clip_settings=clip_settings,
            shadow=shadow_obj,
            font=font_enum,
            anim_in=anim_in,
            anim_out=anim_out,
            anim_loop=anim_loop,
        )

        if text_effect:
            seg.add_effect(str(text_effect))

        if keyword:
            self._apply_keyword_highlight(
                seg,
                keyword,
                keyword_color=keyword_color,
                keyword_font_size=keyword_font_size or font_size,
                keyword_border_color=keyword_border_color or border_color,
            )
        if return_resolved_assets:
            return seg, resolved_assets
        return seg

    def _apply_keyword_highlight(
        self,
        segment: draft.TextSegment,
        keywords: str,
        *,
        keyword_color: str,
        keyword_font_size: float,
        keyword_border_color: str = None,
    ):
        material = segment.export_material()
        content = __import__("json").loads(material["content"])
        styles = content.setdefault("styles", [])
        base_style = styles[0] if styles else None
        if base_style is None:
            return

        text = content.get("text", "")
        color_rgb = list(_hex_to_rgb(keyword_color))
        border_rgb = list(_hex_to_rgb(keyword_border_color)) if keyword_border_color else None
        for keyword in [item.strip() for item in keywords.split("|") if item.strip()]:
            start = 0
            while start < len(text):
                index = text.find(keyword, start)
                if index < 0:
                    break
                end = index + len(keyword)
                style = {
                    "fill": {
                        "alpha": 1.0,
                        "content": {
                            "render_type": "solid",
                            "solid": {"alpha": 1.0, "color": color_rgb},
                        },
                    },
                    "range": [index, end],
                    "size": keyword_font_size,
                    "bold": base_style.get("bold", False),
                    "italic": base_style.get("italic", False),
                    "underline": base_style.get("underline", False),
                    "strokes": [],
                }
                if border_rgb:
                    style["strokes"].append(
                        {
                            "content": {"solid": {"alpha": 1.0, "color": border_rgb}},
                            "width": 0.08,
                        }
                    )
                styles.append(style)
                start = end

        original_export = segment.export_material

        def export_material_with_highlights():
            payload = original_export()
            payload["content"] = __import__("json").dumps(content, ensure_ascii=False)
            return payload

        segment.export_material = export_material_with_highlights
        segment._rich_text_content = content
        return segment

    def add_narrated_subtitles(
        self,
        text: str,
        speaker: str = "zh_female_xiaopengyou",
        start_time: Union[str, int] = None,
        track_name: str = "Subtitles",
    ):
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        curr_us = safe_tim(start_time)
        chosen_backend = None

        parts = [p for p in re.split(r"([，。！？\n\r]+)", text) if p.strip()]
        sentences = []
        for i in range(0, len(parts), 2):
            value = parts[i]
            if i + 1 < len(parts):
                value += parts[i + 1]
            sentences.append(value.strip())

        for sentence in sentences:
            clean_text = sentence.rstrip("，。！？\n\r ")
            if not clean_text:
                continue

            audio_seg, backend_used = self.add_tts_intelligent(
                clean_text,
                speaker=speaker,
                start_time=curr_us,
                tts_backend=chosen_backend,
                allow_fallback=(chosen_backend is None),
                return_backend=True,
            )
            if audio_seg:
                if chosen_backend is None:
                    chosen_backend = backend_used
                actual_dur_us = audio_seg.target_timerange.duration
                self.add_text_simple(
                    clean_text,
                    start_time=curr_us,
                    duration=actual_dur_us,
                    track_name=track_name,
                    clip_settings=draft.ClipSettings(transform_y=-0.8),
                    style=draft.TextStyle(size=5.0),
                    border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=1.0, width=40.0),
                )
                curr_us += actual_dur_us + 100000
            else:
                if chosen_backend is not None:
                    raise RuntimeError(
                        f"TTS segment failed under locked backend '{chosen_backend}'. "
                        "Stopped to avoid mixed voice providers."
                    )
        return curr_us

    def add_tts_intelligent(
        self,
        text: str,
        speaker: str = "zh_male_huoli",
        start_time: Union[str, int] = None,
        track_name: str = "VoiceOver",
        tts_backend: str = None,
        allow_fallback: bool = True,
        return_backend: bool = False,
    ):
        import uuid

        from universal_tts import generate_voice_with_meta

        if start_time is None:
            start_time = self.get_track_duration(track_name)

        temp_dir = os.path.join(self.root, self.name, "temp_assets")
        os.makedirs(temp_dir, exist_ok=True)
        output_file = os.path.join(temp_dir, f"tts_{uuid.uuid4().hex[:8]}.ogg")

        async def _generate():
            return await generate_voice_with_meta(
                text,
                output_file,
                speaker,
                backend=tts_backend,
                allow_fallback=allow_fallback,
                sami_retries=2,
            )

        try:
            asyncio.get_running_loop()
            loop_running = True
        except Exception:
            loop_running = False

        if loop_running:
            box = {"path": None, "err": None}

            def _worker():
                try:
                    box["path"] = asyncio.run(_generate())
                except Exception as exc:
                    box["err"] = exc

            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()
            thread.join()
            if box["err"] is not None:
                raise box["err"]
            actual_path, backend_used = box["path"] if box["path"] else (None, None)
        else:
            actual_path, backend_used = asyncio.run(_generate())

        if not actual_path:
            return (None, None) if return_backend else None
        seg = self.add_media_safe(actual_path, start_time, track_name=track_name)
        return (seg, backend_used) if return_backend else seg

    async def add_tts_intelligent_async(
        self,
        text: str,
        speaker: str = "zh_male_huoli",
        start_time: Union[str, int] = None,
        track_name: str = "VoiceOver",
    ):
        import uuid

        from universal_tts import generate_voice

        if start_time is None:
            start_time = self.get_track_duration(track_name)

        temp_dir = os.path.join(self.root, self.name, "temp_assets")
        os.makedirs(temp_dir, exist_ok=True)
        output_file = os.path.join(temp_dir, f"tts_{uuid.uuid4().hex[:8]}.ogg")
        actual_path = await generate_voice(text, output_file, speaker)
        if not actual_path:
            return None
        return self.add_media_safe(actual_path, start_time, track_name=track_name)
