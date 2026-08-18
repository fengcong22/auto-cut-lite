import os
import uuid
from typing import Optional, Union

import pyJianYingDraft as draft
from utils.formatters import safe_tim


class ProtocolOpsMixin:
    GREEN_SCREEN_EFFECT_RESOURCE_ID = "267417011880001538"

    def add_filter_segment(
        self,
        filter_name: Optional[str] = None,
        start_time: Union[str, int, None] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "FilterTrack",
        intensity: float = 100.0,
        filter_query: str = None,
        return_resolved_asset: bool = False,
    ):
        resolved_asset = None
        resolved_value, resolved_asset = self._resolve_asset_write_value(
            filter_query,
            "filter",
            field_name="filter_query",
        )
        filter_name = filter_name or resolved_value
        if not filter_name:
            raise ValueError("add_filter_segment requires filter_name or filter_query")

        if start_time is None:
            start_time = self.get_track_duration(track_name)

        filter_enum = self._resolve_enum(draft.FilterType, str(filter_name))
        if not filter_enum:
            raise ValueError(f"Unknown filter: {filter_name}")

        timerange = draft.Timerange(safe_tim(start_time), safe_tim(duration))
        segment = draft.FilterSegment(filter_enum, timerange, float(intensity) / 100.0)

        if getattr(self, "is_patch_mode", False):
            self.patch_session.add_filter_segment(
                segment, segment.material.export_json(), track_name
            )
            if return_resolved_asset:
                return segment, resolved_asset
            return segment

        self._ensure_track(draft.TrackType.filter, track_name)
        self.script.add_segment(segment, track_name)
        if segment.material not in self.script.materials:
            self.script.materials.filters.append(segment.material)
        if return_resolved_asset:
            return segment, resolved_asset
        return segment

    def attach_internal_material(
        self,
        segment,
        *,
        kind: str,
        material_name: Optional[str] = None,
        duration: Union[str, int, None] = None,
        animation_role: str = "in",
        query: str = None,
        return_resolved_asset: bool = False,
    ) -> str:
        normalized_kind = str(kind).strip().lower()
        resolved_asset = None
        if query:
            resolved_asset = self._resolve_internal_material_asset(
                segment,
                normalized_kind,
                animation_role,
                query,
            )
            material_name = material_name or resolved_asset["write_value"]
        if not material_name:
            raise ValueError("attach_internal_material requires material_name or query")

        if normalized_kind == "transition":
            result = self._attach_transition(
                segment, material_name=material_name, duration=duration
            )
            if return_resolved_asset:
                return result, resolved_asset
            return result
        if normalized_kind == "animation":
            result = self._attach_animation(
                segment,
                material_name=material_name,
                duration=duration,
                animation_role=animation_role,
            )
            if return_resolved_asset:
                return result, resolved_asset
            return result
        raise ValueError(f"Unsupported material kind: {kind}")

    def add_complex_text(
        self,
        text: str,
        start_time: Union[str, int, None] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "Subtitles",
        *,
        bubble_resource_id: Optional[str] = None,
        bubble_effect_id: Optional[str] = None,
        flower_id: Optional[str] = None,
        font: str = None,
        font_size: float = 5.0,
        text_color: str = "#ffffff",
        border_color: str = None,
        shadow=None,
        transform_x: float = 0.0,
        transform_y: float = -0.8,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        text_effect: str = None,
        anim_in: str = None,
        anim_out: str = None,
        anim_loop: str = None,
        font_query: str = None,
        anim_in_query: str = None,
        anim_out_query: str = None,
        anim_loop_query: str = None,
        text_effect_query: str = None,
        bubble_query: str = None,
        flower_query: str = None,
        return_resolved_assets: bool = False,
    ):
        resolved_assets = {}
        if flower_id and text_effect:
            raise ValueError("flower_id and text_effect are mutually exclusive")
        if flower_query and text_effect_query:
            raise ValueError("flower_query and text_effect_query are mutually exclusive")

        resolved_value, resolved_asset = self._resolve_asset_write_value(
            bubble_query,
            "bubble_text",
            field_name="bubble_query",
        )
        if resolved_asset:
            resolved_assets["bubble"] = resolved_asset
            bubble_resource_id = (
                bubble_resource_id or resolved_asset.get("resource_id") or resolved_value
            )
            bubble_effect_id = bubble_effect_id or resolved_asset.get("effect_id")

        resolved_value, resolved_asset = self._resolve_asset_write_value(
            flower_query,
            "flower_text",
            field_name="flower_query",
        )
        if resolved_asset:
            resolved_assets["flower"] = resolved_asset
            flower_id = flower_id or resolved_asset.get("resource_id") or resolved_value

        rich_result = self.add_rich_text(
            text,
            start_time=start_time,
            duration=duration,
            track_name=track_name,
            text_color=text_color,
            border_color=border_color,
            font=font,
            font_size=font_size,
            transform_x=transform_x,
            transform_y=transform_y,
            scale_x=scale_x,
            scale_y=scale_y,
            shadow=shadow,
            text_effect=text_effect,
            anim_in=anim_in,
            anim_out=anim_out,
            anim_loop=anim_loop,
            font_query=font_query,
            anim_in_query=anim_in_query,
            anim_out_query=anim_out_query,
            anim_loop_query=anim_loop_query,
            text_effect_query=text_effect_query,
            return_resolved_assets=True,
        )
        segment, rich_resolved_assets = rich_result
        resolved_assets.update(rich_resolved_assets)

        if bubble_resource_id:
            segment.add_bubble(
                str(bubble_effect_id or bubble_resource_id),
                str(bubble_resource_id),
            )
        if flower_id:
            segment.add_effect(str(flower_id))

        if getattr(self, "is_patch_mode", False):
            self.patch_session.replace_text_segment_materials(
                segment,
                segment.export_material(),
                track_name,
            )
        else:
            self._refresh_text_segment_materials(segment)
        if return_resolved_assets:
            return segment, resolved_assets
        return segment

    def add_green_screen_background(
        self,
        segment,
        *,
        background_path: Optional[str] = None,
        background_query: Optional[str] = None,
        chroma_color: str = "#00ff00",
        chroma_strength: int = 20,
        edge_feather: int = 10,
        edge_cleanup: int = 10,
        return_resolved_asset: bool = False,
    ):
        if segment is None:
            raise TypeError("add_green_screen_background requires a video segment")
        if background_query:
            raise ValueError("background_query is not implemented yet")
        if not background_path:
            raise ValueError("add_green_screen_background requires background_path")
        if not os.path.exists(background_path):
            raise FileNotFoundError(background_path)

        if getattr(self, "is_patch_mode", False):
            if not hasattr(segment, "data"):
                raise TypeError("Segment does not support patch-mode green screen")
            return self.patch_session.add_green_screen_background(
                segment,
                background_path=background_path,
                chroma_color=chroma_color,
                chroma_strength=chroma_strength,
                edge_feather=edge_feather,
                edge_cleanup=edge_cleanup,
            )

        if not isinstance(segment, draft.VideoSegment):
            raise TypeError("add_green_screen_background only supports video segments")

        background_duration = getattr(segment.target_timerange, "duration", 0) or 0
        background_material = draft.VideoMaterial(background_path)
        background_segment = draft.VideoSegment(
            background_material,
            draft.Timerange(segment.target_timerange.start, background_duration),
            source_timerange=draft.Timerange(0, background_duration),
        )
        self._ensure_track(draft.TrackType.video, "绿幕背景")
        self.script.add_segment(background_segment, "绿幕背景")

        chroma_payload = {
            "id": uuid.uuid4().hex,
            "type": "chroma",
            "color": str(chroma_color),
            "path": "",
            "edge_smooth_value": float(edge_feather) / 100.0,
            "intensity_value": float(chroma_strength) / 100.0,
            "resource_id": self.GREEN_SCREEN_EFFECT_RESOURCE_ID,
            "shadow_value": 0.0,
            "should_transfer_color": False,
            "spill_value": float(edge_cleanup) / 100.0,
            "version": "",
        }
        if chroma_payload["id"] not in segment.extra_material_refs:
            segment.extra_material_refs.append(chroma_payload["id"])
        self.script.materials.chromas.append(chroma_payload)
        result = {
            "segment_id": segment.segment_id,
            "background_segment_id": background_segment.segment_id,
            "background_track_name": "绿幕背景",
            "background_material_id": background_material.material_id,
            "background_resource_id": getattr(background_material, "resource_id", None),
            "chroma_id": chroma_payload["id"],
            "chroma_color": chroma_payload["color"],
        }
        if return_resolved_asset:
            return result, None
        return result

    def _attach_transition(
        self, segment, *, material_name: str, duration: Union[str, int, None] = None
    ) -> str:
        if getattr(self, "is_patch_mode", False):
            if segment is None or not hasattr(segment, "data"):
                raise TypeError("Segment does not support patch-mode material attachment")
            return self.patch_session.attach_transition_to_segment(
                segment,
                transition_name=material_name,
                duration=duration,
            )

        if segment is None or not hasattr(segment, "add_transition"):
            raise TypeError("Segment does not support transition attachment")
        transition_enum = self._resolve_enum(draft.TransitionType, str(material_name))
        if not transition_enum:
            raise ValueError(f"Unknown transition: {material_name}")
        segment.add_transition(transition_enum, duration=duration)
        if segment.transition is None:
            raise ValueError(f"Failed to attach transition: {material_name}")
        if segment.transition not in self.script.materials:
            self.script.materials.transitions.append(segment.transition)
        return segment.transition.global_id

    def _resolve_internal_material_asset(self, segment, kind: str, animation_role: str, query: str):
        if kind == "transition":
            category = "transition"
        elif kind == "animation":
            if (
                getattr(segment, "track_type", None) == "text"
                or segment.__class__.__name__ == "TextSegment"
            ):
                if animation_role == "loop":
                    category = "text_loop_animation"
                elif animation_role == "out":
                    category = "text_outro_animation"
                else:
                    category = "text_animation"
            else:
                if animation_role == "out":
                    category = "video_outro_animation"
                else:
                    category = "video_intro_animation"
        else:
            raise ValueError(f"Unsupported material kind: {kind}")
        _, resolved_asset = self._resolve_asset_write_value(
            query,
            category,
            field_name="query",
            allow_none=False,
        )
        return resolved_asset

    def _attach_animation(
        self,
        segment,
        *,
        material_name: str,
        duration: Union[str, int, None] = None,
        animation_role: str = "in",
    ) -> str:
        role = str(animation_role).strip().lower()
        if role not in {"in", "out", "group", "loop"}:
            raise ValueError(f"Unsupported animation role: {animation_role}")

        if getattr(self, "is_patch_mode", False):
            if segment is None or not hasattr(segment, "data"):
                raise TypeError("Segment does not support patch-mode material attachment")
            return self.patch_session.attach_animation_to_segment(
                segment,
                animation_name=material_name,
                animation_role=role,
                duration=duration,
            )

        if segment is None or not hasattr(segment, "add_animation"):
            raise TypeError("Segment does not support animation attachment")

        enum_cls = self._animation_enum_for_segment(segment, role)
        animation_enum = self._resolve_enum(enum_cls, str(material_name))
        if not animation_enum:
            raise ValueError(f"Unknown animation: {material_name}")
        segment.add_animation(animation_enum, duration=duration)
        animation_inst = getattr(segment, "animations_instance", None)
        if animation_inst is None:
            raise ValueError(f"Failed to attach animation: {material_name}")
        if animation_inst not in self.script.materials:
            self.script.materials.animations.append(animation_inst)
        return animation_inst.animation_id

    def _animation_enum_for_segment(self, segment, role: str):
        if (
            getattr(segment, "track_type", None) == "text"
            or segment.__class__.__name__ == "TextSegment"
        ):
            if role == "in":
                return draft.TextIntro
            if role == "out":
                return draft.TextOutro
            if role == "loop":
                return draft.TextLoopAnim
            raise ValueError("Text segments do not support group animation")
        if role == "in":
            return draft.IntroType
        if role == "out":
            return draft.OutroType
        return draft.GroupAnimationType

    def _refresh_text_segment_materials(self, segment) -> None:
        texts = self.script.materials.texts
        effects = self.script.materials.filters
        animations = self.script.materials.animations

        texts[:] = [item for item in texts if item.get("id") != segment.material_id]
        effects[:] = [
            item
            for item in effects
            if getattr(item, "global_id", None)
            not in {
                getattr(segment, "bubble", None) and segment.bubble.global_id,
                getattr(segment, "effect", None) and segment.effect.global_id,
            }
        ]
        if getattr(segment, "animations_instance", None) is not None:
            animations[:] = [
                item
                for item in animations
                if getattr(item, "animation_id", None) != segment.animations_instance.animation_id
            ]
            animations.append(segment.animations_instance)
        if getattr(segment, "bubble", None) is not None:
            effects.append(segment.bubble)
        if getattr(segment, "effect", None) is not None:
            effects.append(segment.effect)
        texts.append(segment.export_material())
