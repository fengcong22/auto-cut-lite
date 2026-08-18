import csv
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pyJianYingDraft as draft
from utils.constants import SYNONYMS
from utils.vendor_enums import get_vendor_enum_optional

MASK_IDENTIFIERS = {
    "线性": "line",
    "镜面": "mirror",
    "圆形": "circle",
    "矩形": "rectangle",
    "爱心": "heart",
    "星形": "star",
}

REFERENCE_ENTRY_RE = re.compile(
    r"(?:Effect id:\s*(?P<effect_id>\d+)\s*,\s*)?Resource id:\s*(?P<resource_id>\d+)\s*'(?P<name>[^']+)'"
)


class AssetIndex:
    CATEGORY_ALIASES = {
        "mask": "mask",
        "masks": "mask",
        "font": "font",
        "fonts": "font",
        "filter": "filter",
        "filters": "filter",
        "transition": "transition",
        "transitions": "transition",
        "text_animation": "text_animation",
        "text_animations": "text_animation",
        "text_outro_animation": "text_outro_animation",
        "text_outro_animations": "text_outro_animation",
        "text_loop_animation": "text_loop_animation",
        "text_loop_animations": "text_loop_animation",
        "animation": "text_animation",
        "animations": "text_animation",
        "textintro": "text_animation",
        "textoutro": "text_outro_animation",
        "textloopanim": "text_loop_animation",
        "video_intro_animation": "video_intro_animation",
        "video_intro_animations": "video_intro_animation",
        "intro": "video_intro_animation",
        "intros": "video_intro_animation",
        "video_outro_animation": "video_outro_animation",
        "video_outro_animations": "video_outro_animation",
        "outro": "video_outro_animation",
        "outros": "video_outro_animation",
        "video_effect": "video_effect",
        "video_effects": "video_effect",
        "effect": "video_effect",
        "effects": "video_effect",
        "video_scene_effect": "video_effect",
        "video_scene_effects": "video_effect",
        "video_character_effect": "video_character_effect",
        "video_character_effects": "video_character_effect",
        "character_effect": "video_character_effect",
        "character_effects": "video_character_effect",
        "face_effect": "video_character_effect",
        "face_effects": "video_character_effect",
        "audio_effect": "audio_effect",
        "audio_effects": "audio_effect",
        "audio_scene_effect": "audio_effect",
        "audio_scene_effects": "audio_effect",
        "sound_fx": "audio_effect",
        "tone_effect": "tone_effect",
        "tone_effects": "tone_effect",
        "tone": "tone_effect",
        "tones": "tone_effect",
        "speech_to_song": "speech_to_song",
        "speech_song": "speech_to_song",
        "voice_to_song": "speech_to_song",
        "sticker": "sticker",
        "stickers": "sticker",
        "bubble_text": "bubble_text",
        "bubble": "bubble_text",
        "flower_text": "flower_text",
        "flower": "flower_text",
        "text_template": "text_template",
        "text_templates": "text_template",
        "template": "text_template",
        "templates": "text_template",
        "cloud_music": "cloud_music",
        "music": "cloud_music",
        "cloud_sound_effect": "cloud_sound_effect",
        "cloud_sound_effects": "cloud_sound_effect",
        "sound_effect": "cloud_sound_effect",
        "sound_effects": "cloud_sound_effect",
        "sfx": "cloud_sound_effect",
        "cloud_text_style": "cloud_text_style",
        "cloud_text_styles": "cloud_text_style",
        "text_style": "cloud_text_style",
        "text_styles": "cloud_text_style",
        "cloud_video_asset": "cloud_video_asset",
        "cloud_video_assets": "cloud_video_asset",
        "video_asset": "cloud_video_asset",
        "video_assets": "cloud_video_asset",
        "tts_speaker": "tts_speaker",
        "tts_speakers": "tts_speaker",
        "speaker": "tts_speaker",
        "speakers": "tts_speaker",
        "cached_audio": "cached_audio",
        "cached_audios": "cached_audio",
        "audio_cache": "cached_audio",
    }

    CSV_CATEGORY_MAP = {
        "cloud_music_library.csv": "cloud_music",
        "cloud_sound_effects.csv": "cloud_sound_effect",
        "cloud_text_styles.csv": "cloud_text_style",
        "cloud_video_assets.csv": "cloud_video_asset",
        "favorite_flower_texts.csv": "flower_text",
        "favorite_flower_texts.local.csv": "flower_text",
        "favorite_text_templates.csv": "text_template",
        "favorite_text_templates.local.csv": "text_template",
        "filters.csv": "filter",
        "jy_cached_audio.csv": "cached_audio",
        "text_animations.csv": "text_animation",
        "transitions.csv": "transition",
        "tts_speakers.csv": "tts_speaker",
        "text_templates.csv": "text_template",
        "video_intro_animations.csv": "video_intro_animation",
        "video_outro_animations.csv": "video_outro_animation",
        "video_scene_effects.csv": "video_effect",
    }

    WRITE_TARGETS = {
        "sticker": ("sticker_id", "resource_id"),
        "bubble_text": ("sticker_id", "resource_id"),
        "flower_text": ("sticker_id", "resource_id"),
        "text_template": ("template_id", "resource_id"),
        "mask": ("mask_type", "identifier"),
        "font": ("font", "identifier"),
        "text_animation": ("anim_in", "identifier"),
        "text_outro_animation": ("anim_out", "identifier"),
        "text_loop_animation": ("anim_loop", "identifier"),
        "video_intro_animation": ("anim_in", "identifier"),
        "video_outro_animation": ("anim_out", "identifier"),
        "video_effect": ("text_effect", "identifier"),
        "video_character_effect": ("effect_name", "identifier"),
        "audio_effect": ("effect_name", "identifier"),
        "tone_effect": ("effect_name", "identifier"),
        "speech_to_song": ("effect_name", "identifier"),
        "filter": ("effect_name", "identifier"),
        "transition": ("transition_name", "identifier"),
        "tts_speaker": ("speaker", "identifier"),
        "cloud_music": ("query", "identifier"),
        "cloud_sound_effect": ("query", "identifier"),
        "cloud_text_style": ("style_id", "identifier"),
        "cloud_video_asset": ("query", "identifier"),
        "cached_audio": ("query", "identifier"),
    }

    def __init__(self, skill_root: Optional[str] = None):
        self.skill_root = os.path.abspath(
            skill_root or os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        )
        self.data_dir = os.path.join(self.skill_root, "data")
        self.references_dir = os.path.join(self.skill_root, "references")
        self._records: Optional[List[Dict[str, Any]]] = None
        self._category_index: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self._categories_cache: Optional[List[Dict[str, Any]]] = None

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        limit: int = 20,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_category = self._normalize_category(category)
        records = self._get_records()
        if normalized_category:
            records = [record for record in records if record["category"] == normalized_category]
        if source:
            records = [record for record in records if record.get("source") == source]

        query = (query or "").strip()
        if not query:
            results = list(records)
            results.sort(key=self._sort_key)
            return results[: max(1, limit)]

        scored: List[Dict[str, Any]] = []
        for record in records:
            score = self._score_record(query, record)
            if score <= 0:
                continue
            item = dict(record)
            item["score"] = score
            scored.append(item)

        scored.sort(key=self._sort_key, reverse=True)
        return scored[: max(1, limit)]

    def list_categories(self) -> List[Dict[str, Any]]:
        if self._categories_cache is not None:
            return [dict(item) for item in self._categories_cache]

        category_index = self._get_category_index()
        items: List[Dict[str, Any]] = []
        for category in sorted(category_index):
            records = category_index[category]
            sources = sorted({record["source"] for record in records})
            items.append(
                {
                    "category": category,
                    "count": len(records),
                    "sources": sources,
                }
            )
        self._categories_cache = items
        return [dict(item) for item in items]

    def resolve(
        self,
        query: str,
        category: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        matches = self.search(query, category=category, limit=1, source=source)
        if not matches:
            normalized = self._normalize_category(category) or "any"
            raise LookupError(f"No asset match found for query={query!r} category={normalized!r}")
        return matches[0]

    def _get_category_index(self) -> Dict[str, List[Dict[str, Any]]]:
        if self._category_index is None:
            index: Dict[str, List[Dict[str, Any]]] = {}
            for record in self._get_records():
                index.setdefault(record["category"], []).append(record)
            self._category_index = index
        return self._category_index

    def _get_records(self) -> List[Dict[str, Any]]:
        if self._records is None:
            records: List[Dict[str, Any]] = []
            records.extend(self._records_from_vendor_enums())
            records.extend(self._records_from_csv())
            records.extend(self._records_from_reference_readme())
            self._records = records
        return self._records

    def _records_from_vendor_enums(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        enum_specs: List[Tuple[str, Any, str, Optional[Any]]] = [
            ("mask", draft.MaskType, "vendor_enum", self._make_mask_identifier),
            ("font", draft.FontType, "vendor_enum", None),
            ("filter", draft.FilterType, "vendor_enum", None),
            ("transition", draft.TransitionType, "vendor_enum", None),
            ("text_animation", draft.TextIntro, "vendor_enum", None),
            ("text_outro_animation", draft.TextOutro, "vendor_enum", None),
            ("text_loop_animation", draft.TextLoopAnim, "vendor_enum", None),
            ("video_intro_animation", draft.IntroType, "vendor_enum", None),
            ("video_outro_animation", draft.OutroType, "vendor_enum", None),
            ("video_effect", draft.VideoSceneEffectType, "vendor_enum", None),
            ("video_character_effect", draft.VideoCharacterEffectType, "vendor_enum", None),
            ("audio_effect", get_vendor_enum_optional("AudioSceneEffectType"), "vendor_enum", None),
            ("tone_effect", get_vendor_enum_optional("ToneEffectType"), "vendor_enum", None),
            ("speech_to_song", get_vendor_enum_optional("SpeechToSongType"), "vendor_enum", None),
        ]

        for category, enum_cls, source, identifier_factory in enum_specs:
            if enum_cls is None:
                continue
            for enum_item in enum_cls:
                meta = enum_item.value
                enum_name = self._safe_str(enum_item.name)
                display_name = self._safe_str(getattr(meta, "name", "")) or enum_name
                identifier = (
                    identifier_factory(display_name, enum_name, meta)
                    if identifier_factory
                    else enum_name
                )
                description = self._build_space_separated(
                    [
                        getattr(meta, "description", ""),
                        getattr(meta, "category", ""),
                    ]
                )
                records.append(
                    self._make_record(
                        category=category,
                        identifier=identifier,
                        name=display_name,
                        source=source,
                        source_file=f"{enum_cls.__name__}",
                        enum_name=enum_name,
                        enum_type=enum_cls.__name__,
                        resource_id=self._safe_str(getattr(meta, "resource_id", "")),
                        effect_id=self._safe_str(getattr(meta, "effect_id", "")),
                        description=description,
                        tags=[display_name, enum_name],
                    )
                )
        return records

    def _records_from_csv(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        if not os.path.isdir(self.data_dir):
            return records

        for filename, category in self.CSV_CATEGORY_MAP.items():
            path = os.path.join(self.data_dir, filename)
            if not os.path.exists(path):
                continue
            for row in self._iter_csv_rows(path):
                record = self._make_csv_record(category, filename, row)
                if record is not None:
                    records.append(record)
        return records

    def _records_from_reference_readme(self) -> List[Dict[str, Any]]:
        path = os.path.join(self.references_dir, "README.md")
        if not os.path.exists(path):
            return []

        text = self._read_text(path)
        if not text:
            return []

        section = None
        records: List[Dict[str, Any]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.endswith(":") or line.endswith("："):
                if "贴纸素材" in line:
                    section = "sticker"
                elif "文字气泡效果" in line:
                    section = "bubble_text"
                elif "花字效果" in line:
                    section = "flower_text"
                else:
                    section = None
                continue
            match = REFERENCE_ENTRY_RE.search(line)
            if not match or not section:
                continue
            name = match.group("name").strip()
            resource_id = match.group("resource_id").strip()
            effect_id = (match.group("effect_id") or "").strip()
            records.append(
                self._make_record(
                    category=section,
                    identifier=resource_id,
                    name=name,
                    source="reference_readme",
                    source_file=path,
                    resource_id=resource_id,
                    effect_id=effect_id,
                    tags=[name],
                )
            )
            if section in {"bubble_text", "flower_text"}:
                records.append(
                    self._make_record(
                        category="sticker",
                        identifier=resource_id,
                        name=name,
                        source="reference_readme",
                        source_file=path,
                        resource_id=resource_id,
                        effect_id=effect_id,
                        tags=[name, section],
                    )
                )
        return records

    def _make_csv_record(
        self, category: str, filename: str, row: Dict[str, str]
    ) -> Optional[Dict[str, Any]]:
        cleaned = {key: self._safe_str(value) for key, value in row.items()}

        if category == "cloud_music":
            identifier = cleaned.get("music_id", "")
            name = cleaned.get("title", "") or identifier
            tags = [cleaned.get("categories", ""), cleaned.get("url", "")]
        elif category == "cloud_sound_effect":
            identifier = cleaned.get("effect_id", "")
            name = cleaned.get("title", "") or identifier
            tags = [cleaned.get("categories", "")]
        elif category == "cloud_text_style":
            identifier = cleaned.get("style_id", "")
            name = cleaned.get("name_hint", "") or identifier
            tags = [cleaned.get("categories", "")]
        elif category == "cloud_video_asset":
            identifier = cleaned.get("id", "")
            name = cleaned.get("name", "") or identifier
            tags = [cleaned.get("type", ""), cleaned.get("url", "")]
        elif category == "tts_speaker":
            identifier = cleaned.get("speaker_id", "")
            name = cleaned.get("name_hint", "") or identifier
            tags = [cleaned.get("categories", "")]
        elif category == "cached_audio":
            identifier = cleaned.get("identifier", "")
            name = cleaned.get("identifier", "") or identifier
            tags = [cleaned.get("author", ""), cleaned.get("path", ""), cleaned.get("category", "")]
        else:
            identifier = cleaned.get("identifier", "")
            name = cleaned.get("name", "") or cleaned.get("title", "") or identifier
            tags = [
                cleaned.get("description", ""),
                cleaned.get("category", ""),
                cleaned.get("enum_type", ""),
            ]

        if not identifier:
            return None

        description = self._build_space_separated(
            [
                cleaned.get("description", ""),
                cleaned.get("category", ""),
                cleaned.get("categories", ""),
                cleaned.get("type", ""),
                cleaned.get("duration", ""),
                cleaned.get("duration_s", ""),
                cleaned.get("author", ""),
            ]
        )
        source_file = os.path.join(self.data_dir, filename)
        return self._make_record(
            category=category,
            identifier=identifier,
            name=name,
            source="csv",
            source_file=source_file,
            resource_id=cleaned.get("resource_id", ""),
            effect_id=cleaned.get("effect_id", ""),
            enum_type=cleaned.get("enum_type", ""),
            description=description,
            metadata=cleaned,
            tags=tags,
            title=cleaned.get("title", "")
            or cleaned.get("name", "")
            or cleaned.get("name_hint", ""),
        )

    def _normalize_category(self, category: Optional[str]) -> Optional[str]:
        if not category:
            return None
        lowered = category.lower().strip().replace("-", "_").replace(" ", "_")
        return self.CATEGORY_ALIASES.get(lowered, lowered)

    def _score_record(self, query: str, record: Dict[str, Any]) -> int:
        tokens = self._expand_terms(query)
        if not tokens:
            return 0

        fields = {
            "identifier": record.get("identifier", ""),
            "name": record.get("name", ""),
            "title": record.get("title", ""),
            "description": record.get("description", ""),
            "tags": " ".join(record.get("tags", [])),
            "resource_id": record.get("resource_id", ""),
            "effect_id": record.get("effect_id", ""),
            "enum_name": record.get("enum_name", ""),
            "enum_type": record.get("enum_type", ""),
        }
        haystack = self._normalize_text(" ".join(value for value in fields.values() if value))

        score = 0
        raw_query = self._normalize_text(query)
        if raw_query and raw_query in haystack:
            score += 120

        for field_name, value in fields.items():
            normalized_value = self._normalize_text(value)
            if not normalized_value:
                continue
            for token in tokens:
                if token == normalized_value:
                    score += 80
                elif token in normalized_value:
                    score += self._field_weight(field_name)

        if record.get("source") == "vendor_enum":
            score += 1
        metadata = record.get("metadata", {}) or {}
        if self._safe_str(metadata.get("favorite_flag")) in {"1", "true", "True"}:
            score += 35
        source_file = self._safe_str(record.get("source_file"))
        if source_file.endswith(".local.csv"):
            score += 20
        if self._safe_str(metadata.get("source")) in {
            "draft_scan",
            "artist_effect_cache",
            "ai_text_template_cache",
            "jy_db",
        }:
            score += 10
        return score

    def _expand_terms(self, query: str) -> List[str]:
        normalized = self._normalize_text(query)
        if not normalized:
            return []

        terms = set(part for part in re.split(r"[\s|,/]+", normalized) if part)
        terms.add(normalized)

        for token in list(terms):
            for key, values in SYNONYMS.items():
                normalized_key = self._normalize_text(key)
                normalized_values = [self._normalize_text(value) for value in values]
                all_terms = {normalized_key, *normalized_values}
                if token in all_terms:
                    terms.update(all_terms)
        return sorted(terms, key=len, reverse=True)

    def _field_weight(self, field_name: str) -> int:
        weights = {
            "identifier": 60,
            "name": 45,
            "title": 45,
            "enum_name": 40,
            "tags": 25,
            "description": 20,
            "resource_id": 20,
            "effect_id": 20,
            "enum_type": 10,
        }
        return weights.get(field_name, 10)

    def _sort_key(self, record: Dict[str, Any]) -> Tuple[Any, ...]:
        return (
            record.get("score", 0),
            record.get("name", ""),
            record.get("identifier", ""),
        )

    def _make_record(
        self,
        *,
        category: str,
        identifier: str,
        name: str,
        source: str,
        source_file: str,
        title: str = "",
        resource_id: str = "",
        effect_id: str = "",
        enum_name: str = "",
        enum_type: str = "",
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        normalized_tags = [self._safe_str(tag) for tag in (tags or []) if self._safe_str(tag)]
        return {
            "category": category,
            "identifier": self._safe_str(identifier),
            "name": self._safe_str(name) or self._safe_str(identifier),
            "title": self._safe_str(title),
            "resource_id": self._safe_str(resource_id),
            "effect_id": self._safe_str(effect_id),
            "enum_name": self._safe_str(enum_name),
            "enum_type": self._safe_str(enum_type),
            "description": self._safe_str(description),
            "source": source,
            "source_file": source_file,
            "tags": normalized_tags,
            "metadata": dict(metadata or {}),
            "write_field": self._write_field_for_category(category),
            "write_value": self._write_value_for_category(
                category=category,
                identifier=self._safe_str(identifier),
                resource_id=self._safe_str(resource_id),
                effect_id=self._safe_str(effect_id),
            ),
        }

    def _make_mask_identifier(self, display_name: str, enum_name: str, meta: Any) -> str:
        return (
            MASK_IDENTIFIERS.get(display_name)
            or MASK_IDENTIFIERS.get(enum_name)
            or enum_name.lower()
        )

    def _iter_csv_rows(self, filepath: str) -> Iterable[Dict[str, str]]:
        with open(filepath, "r", encoding="utf-8", newline="") as f:
            lines = [line for line in f if not line.startswith("#")]
        if not lines:
            return []
        return csv.DictReader(lines)

    def _read_text(self, path: str) -> str:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def _safe_str(self, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _build_space_separated(self, values: Iterable[str]) -> str:
        return " ".join(part for part in (self._safe_str(value) for value in values) if part)

    def _normalize_text(self, text: str) -> str:
        return self._safe_str(text).lower()

    def _write_field_for_category(self, category: str) -> str:
        return self.WRITE_TARGETS.get(category, ("identifier", "identifier"))[0]

    def _write_value_for_category(
        self,
        *,
        category: str,
        identifier: str,
        resource_id: str,
        effect_id: str,
    ) -> str:
        _, source_key = self.WRITE_TARGETS.get(category, ("identifier", "identifier"))
        if source_key == "resource_id":
            return resource_id or identifier
        if source_key == "effect_id":
            return effect_id or identifier
        return identifier
