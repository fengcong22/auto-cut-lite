import json
import os
import uuid

import pyJianYingDraft as draft
from utils.jianying_env import resolve_adjust_bundle_path


class MockVideoMaterial(draft.VideoMaterial):
    def __init__(self, material_id, duration, name, path):
        self.material_id = material_id
        self.local_material_id = material_id
        self.duration = duration
        self.material_name = name
        self.path = path
        self.width = 1920
        self.height = 1080
        self.crop_settings = draft.CropSettings()
        self.material_type = "video"

    def serialize(self):
        return {
            "id": self.material_id,
            "type": "video",
            "name": self.material_name,
            "path": self.path,
            "duration": self.duration,
            "material_id": self.material_id,
        }


class MockAudioMaterial(draft.AudioMaterial):
    def __init__(self, material_id, duration, name, path):
        self.material_id = material_id
        self.duration = duration
        self.material_name = name
        self.path = path

    def serialize(self):
        return {
            "id": self.material_id,
            "type": "audio",
            "name": self.material_name,
            "path": self.path,
            "duration": self.duration,
            "material_id": self.material_id,
        }


class CompoundSegment:
    def __init__(self, material_id, target_timerange):
        self.material_id = material_id
        self.target_timerange = target_timerange

    def serialize(self):
        return {
            "id": str(uuid.uuid4()).upper(),
            "material_id": self.material_id,
            "target_timerange": self.target_timerange.serialize(),
            "render_index": 0,
            "type": "video",
        }


class MockingOpsMixin:
    def _force_activate_adjustments(self):
        content_path = os.path.join(self.root, self.name, "draft_content.json")
        if not os.path.exists(content_path):
            return

        try:
            with open(content_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            has_modified = False
            materials = data.setdefault("materials", {})
            all_effects = materials.setdefault("effects", [])
            prop_map = {
                "KFTypeBrightness": "brightness",
                "KFTypeContrast": "contrast",
                "KFTypeSaturation": "saturation",
            }
            jy_res_path = resolve_adjust_bundle_path(
                "C:/Program Files/JianyingPro/5.9.0.11632/Resources/DefaultAdjustBundle/combine_adjust"
            )

            for track in data.get("tracks", []):
                for seg in track.get("segments", []):
                    keyframes = seg.get("common_keyframes", [])
                    active_props = [
                        item.get("property_type")
                        for item in keyframes
                        if item.get("property_type") in prop_map
                    ]
                    if not active_props:
                        continue

                    seg["enable_adjust"] = True
                    seg["enable_color_correct_adjust"] = True
                    refs = seg.setdefault("extra_material_refs", [])
                    for prop in active_props:
                        mat_type = prop_map[prop]
                        if any(
                            effect.get("type") == mat_type and effect["id"] in refs
                            for effect in all_effects
                        ):
                            continue
                        new_id = str(uuid.uuid4()).upper()
                        all_effects.append(
                            {
                                "type": mat_type,
                                "value": 0.0,
                                "path": jy_res_path,
                                "id": new_id,
                                "apply_target_type": 0,
                                "platform": "all",
                                "source_platform": 0,
                                "version": "v2",
                            }
                        )
                        refs.append(new_id)
                        has_modified = True

            if has_modified:
                with open(content_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
        except Exception as exc:
            print(f"Force activation failed: {exc}")

    def _patch_cloud_material_ids(self):
        if not self._cloud_audio_patches and not self._cloud_text_patches:
            return

        content_path = os.path.join(self.root, self.name, "draft_content.json")
        if not os.path.exists(content_path):
            return

        try:
            with open(content_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            has_modified = False
            materials = data.get("materials", {})
            audios = materials.get("audios", [])
            for mat in audios:
                path = mat.get("path", "")
                for dummy_path, patch_info in self._cloud_audio_patches.items():
                    if dummy_path in path and patch_info["type"] == "music":
                        mat["music_id"] = patch_info["id"]
                        mat["type"] = "music"
                        has_modified = True

            if has_modified:
                with open(content_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def _sync_dynamic_material_refs(self):
        content_path = os.path.join(self.root, self.name, "draft_content.json")
        if not os.path.exists(content_path):
            return

        try:
            with open(content_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            materials = data.setdefault("materials", {})
            masks = materials.setdefault("masks", [])
            texts = materials.setdefault("texts", [])
            canvases = materials.setdefault("canvases", [])
            audio_effects = materials.setdefault("audio_effects", [])
            audio_fades = materials.setdefault("audio_fades", [])
            video_effects = materials.setdefault("video_effects", [])
            existing_mask_ids = {item.get("id") for item in masks}
            text_material_map = {item.get("id"): item for item in texts}
            existing_canvas_ids = {item.get("id") for item in canvases}
            existing_audio_effect_ids = {item.get("id") for item in audio_effects}
            existing_audio_fade_ids = {item.get("id") for item in audio_fades}
            existing_video_effect_ids = {item.get("id") for item in video_effects}

            for track in self.script.tracks.values():
                for segment in getattr(track, "segments", []):
                    mask = getattr(segment, "mask", None)
                    if mask is not None and mask.global_id not in existing_mask_ids:
                        masks.append(mask.export_json())
                        existing_mask_ids.add(mask.global_id)
                    background = getattr(segment, "background_filling", None)
                    if background is not None and background.global_id not in existing_canvas_ids:
                        canvases.append(background.export_json())
                        existing_canvas_ids.add(background.global_id)
                    for effect in getattr(segment, "effects", []):
                        effect_id = getattr(effect, "effect_id", None)
                        if effect_id is not None and effect_id not in existing_audio_effect_ids:
                            audio_effects.append(effect.export_json())
                            existing_audio_effect_ids.add(effect_id)
                    fade = getattr(segment, "fade", None)
                    fade_id = getattr(fade, "fade_id", None)
                    if fade_id is not None and fade_id not in existing_audio_fade_ids:
                        audio_fades.append(fade.export_json())
                        existing_audio_fade_ids.add(fade_id)
                    effect_inst = getattr(segment, "effect_inst", None)
                    if (
                        effect_inst is not None
                        and effect_inst.global_id not in existing_video_effect_ids
                    ):
                        video_effects.append(effect_inst.export_json())
                        existing_video_effect_ids.add(effect_inst.global_id)
                    rich_text_content = getattr(segment, "_rich_text_content", None)
                    material_id = getattr(segment, "material_id", None)
                    if rich_text_content is not None and material_id in text_material_map:
                        text_material_map[material_id]["content"] = json.dumps(
                            rich_text_content, ensure_ascii=False
                        )

            with open(content_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as exc:
            print(f"Dynamic material sync failed: {exc}")
