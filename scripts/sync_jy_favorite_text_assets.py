import csv
import json
import os
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Set

LOCAL_APP_DATA = os.getenv("LOCALAPPDATA", "")
JY_USER_DATA = os.path.join(LOCAL_APP_DATA, r"JianyingPro\User Data")
JY_PROJECTS_ROOT = os.path.join(JY_USER_DATA, r"Projects\com.lveditor.draft")
JY_RESSDK_ROOT = os.path.join(JY_USER_DATA, r"Cache\ressdk_db")
JY_AI_TEXT_TEMPLATE_ROOT = os.path.join(JY_USER_DATA, r"Cache\AITextTemplate\Resource")
JY_ARTIST_EFFECT_ROOT = os.path.join(JY_USER_DATA, r"Cache\artistEffect")

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(SKILL_ROOT, "data")
FAVORITE_TEXT_TEMPLATES_CSV = os.path.join(DATA_DIR, "favorite_text_templates.local.csv")
FAVORITE_FLOWER_TEXTS_CSV = os.path.join(DATA_DIR, "favorite_flower_texts.local.csv")


def _safe_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _iter_rp_db_paths(root: str) -> Iterable[str]:
    if not os.path.isdir(root):
        return []
    paths: List[str] = []
    for base, _, files in os.walk(root):
        if "rp.db" in files:
            paths.append(os.path.join(base, "rp.db"))
    return sorted(paths)


def _read_json(path: str) -> Optional[Dict[str, object]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_csv(
    path: str, fieldnames: Sequence[str], rows: Sequence[Dict[str, str]], comments: Sequence[str]
) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        for comment in comments:
            f.write(f"# {comment}\n")
        writer = csv.DictWriter(f, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def _load_existing_rows(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        lines = [line for line in f.readlines() if not line.startswith("#")]
    if not lines:
        return []
    return list(csv.DictReader(lines))


def _merge_row(existing: Optional[Dict[str, str]], update: Dict[str, str]) -> Dict[str, str]:
    merged = dict(existing or {})
    for key, value in update.items():
        value = _safe_text(value)
        if value:
            merged[key] = value
    return merged


def _collect_from_databases() -> tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    templates: Dict[str, Dict[str, str]] = {}
    flowers: Dict[str, Dict[str, str]] = {}

    for db_path in _iter_rp_db_paths(JY_RESSDK_ROOT):
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            rows = cur.execute(
                """
                SELECT
                    e.id,
                    e.title,
                    e.name,
                    e.effect_type,
                    e.effect_id,
                    e.third_resource_id,
                    e.has_favorite,
                    e.favorite_time,
                    t.resource_id,
                    t.type
                FROM effect e
                LEFT JOIN effect_txt_template_resource t
                    ON e.id = t.id
                """
            ).fetchall()
        except sqlite3.Error:
            conn.close()
            continue
        finally:
            conn.close()

        for row in rows:
            (
                effect_row_id,
                title,
                name,
                effect_type,
                effect_id,
                third_resource_id,
                has_favorite,
                favorite_time,
                template_resource_id,
                template_type,
            ) = row
            title = _safe_text(title) or _safe_text(name)
            effect_type = _safe_text(effect_type).lower()
            effect_id = _safe_text(effect_id)
            third_resource_id = _safe_text(third_resource_id)
            template_resource_id = _safe_text(template_resource_id)
            favorite_flag = "1" if str(has_favorite or 0) == "1" else ""
            source_db = os.path.basename(os.path.dirname(db_path))

            if template_resource_id:
                existing = templates.get(template_resource_id)
                templates[template_resource_id] = _merge_row(
                    existing,
                    {
                        "identifier": template_resource_id,
                        "name": title or template_resource_id,
                        "title": title or template_resource_id,
                        "description": "Favorite or cached JianYing text template",
                        "resource_id": template_resource_id,
                        "effect_id": effect_id or _safe_text(effect_row_id),
                        "source": "jy_db",
                        "source_detail": source_db,
                        "favorite_flag": favorite_flag,
                        "favorite_time": _safe_text(favorite_time),
                        "categories": _safe_text(template_type) or effect_type or "text_template",
                    },
                )

            candidate_resource_id = third_resource_id or effect_id
            if candidate_resource_id and any(
                token in effect_type for token in ("text", "sticker", "effect")
            ):
                existing = flowers.get(candidate_resource_id)
                flowers[candidate_resource_id] = _merge_row(
                    existing,
                    {
                        "identifier": candidate_resource_id,
                        "name": title or candidate_resource_id,
                        "title": title or candidate_resource_id,
                        "description": "Favorite or cached JianYing flower text effect",
                        "resource_id": candidate_resource_id,
                        "effect_id": effect_id or candidate_resource_id,
                        "source": "jy_db",
                        "source_detail": source_db,
                        "favorite_flag": favorite_flag,
                        "favorite_time": _safe_text(favorite_time),
                        "categories": effect_type or "flower_text",
                    },
                )

    return templates, flowers


def _collect_from_projects() -> tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    templates: Dict[str, Dict[str, str]] = {}
    flowers: Dict[str, Dict[str, str]] = {}
    if not os.path.isdir(JY_PROJECTS_ROOT):
        return templates, flowers

    for project_name in os.listdir(JY_PROJECTS_ROOT):
        project_path = os.path.join(JY_PROJECTS_ROOT, project_name)
        if not os.path.isdir(project_path):
            continue
        content_path = os.path.join(project_path, "draft_content.json")
        data = _read_json(content_path)
        if not data:
            continue
        materials = data.get("materials", {})

        for item in materials.get("text_templates", []) or []:
            if not isinstance(item, dict):
                continue
            resource_id = _safe_text(item.get("resource_id"))
            if not resource_id:
                continue
            templates[resource_id] = _merge_row(
                templates.get(resource_id),
                {
                    "identifier": resource_id,
                    "name": _safe_text(item.get("name")) or project_name,
                    "title": _safe_text(item.get("name")) or project_name,
                    "description": "Text template observed in local draft",
                    "resource_id": resource_id,
                    "effect_id": _safe_text(item.get("effect_id")) or resource_id,
                    "source": "draft_scan",
                    "source_detail": project_name,
                    "favorite_flag": "1" if "收藏" in project_name else "",
                    "favorite_time": "",
                    "categories": "draft|" + project_name,
                },
            )

        for item in materials.get("effects", []) or []:
            if not isinstance(item, dict):
                continue
            if _safe_text(item.get("type")) != "text_effect":
                continue
            resource_id = _safe_text(item.get("resource_id")) or _safe_text(item.get("effect_id"))
            if not resource_id:
                continue
            flowers[resource_id] = _merge_row(
                flowers.get(resource_id),
                {
                    "identifier": resource_id,
                    "name": _safe_text(item.get("name")) or project_name,
                    "title": _safe_text(item.get("name")) or project_name,
                    "description": "Flower text effect observed in local draft",
                    "resource_id": resource_id,
                    "effect_id": _safe_text(item.get("effect_id")) or resource_id,
                    "source": "draft_scan",
                    "source_detail": project_name,
                    "favorite_flag": "1" if "收藏" in project_name else "",
                    "favorite_time": "",
                    "categories": "draft|" + project_name,
                },
            )

    return templates, flowers


def _collect_from_cache_directories() -> (
    tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]
):
    templates: Dict[str, Dict[str, str]] = {}
    flowers: Dict[str, Dict[str, str]] = {}

    if os.path.isdir(JY_AI_TEXT_TEMPLATE_ROOT):
        for entry in os.listdir(JY_AI_TEXT_TEMPLATE_ROOT):
            config = _read_json(os.path.join(JY_AI_TEXT_TEMPLATE_ROOT, entry, "config.json")) or {}
            content_exists = os.path.exists(
                os.path.join(JY_AI_TEXT_TEMPLATE_ROOT, entry, "content.json")
            )
            templates[entry] = _merge_row(
                templates.get(entry),
                {
                    "identifier": entry,
                    "name": entry,
                    "title": entry,
                    "description": "AI text template cache entry",
                    "resource_id": entry,
                    "effect_id": entry,
                    "source": "ai_text_template_cache",
                    "source_detail": "content" if content_exists else "config",
                    "favorite_flag": "",
                    "favorite_time": "",
                    "categories": _safe_text(config.get("version")) or "ai_text_template",
                },
            )

    if os.path.isdir(JY_ARTIST_EFFECT_ROOT):
        for entry in os.listdir(JY_ARTIST_EFFECT_ROOT):
            effect_dir = os.path.join(JY_ARTIST_EFFECT_ROOT, entry)
            if not os.path.isdir(effect_dir):
                continue
            flowers[entry] = _merge_row(
                flowers.get(entry),
                {
                    "identifier": entry,
                    "name": entry,
                    "title": entry,
                    "description": "Local artistEffect cache entry",
                    "resource_id": entry,
                    "effect_id": entry,
                    "source": "artist_effect_cache",
                    "source_detail": "local_cache",
                    "favorite_flag": "",
                    "favorite_time": "",
                    "categories": "artistEffect",
                },
            )

    return templates, flowers


def _combine_maps(*maps: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    combined: Dict[str, Dict[str, str]] = {}
    for mapping in maps:
        for key, row in mapping.items():
            existing = combined.get(key)
            merged = _merge_row(existing, row)
            existing_categories = set(
                filter(None, _safe_text(existing.get("categories") if existing else "").split("|"))
            )
            new_categories = set(filter(None, _safe_text(row.get("categories")).split("|")))
            merged["categories"] = "|".join(sorted(existing_categories | new_categories))
            combined[key] = merged
    return combined


def _merge_with_existing_file(
    rows_by_id: Dict[str, Dict[str, str]], path: str
) -> List[Dict[str, str]]:
    existing_rows = {row.get("identifier", ""): row for row in _load_existing_rows(path)}
    all_ids: Set[str] = set(existing_rows) | set(rows_by_id)
    merged_rows: List[Dict[str, str]] = []
    for identifier in sorted(filter(None, all_ids)):
        merged = _merge_row(existing_rows.get(identifier), rows_by_id.get(identifier, {}))
        categories = set(
            filter(
                None, _safe_text(existing_rows.get(identifier, {}).get("categories", "")).split("|")
            )
        )
        categories.update(
            filter(
                None, _safe_text(rows_by_id.get(identifier, {}).get("categories", "")).split("|")
            )
        )
        merged["categories"] = "|".join(sorted(categories))
        merged_rows.append(merged)
    return merged_rows


def sync_favorite_text_assets() -> Dict[str, int]:
    db_templates, db_flowers = _collect_from_databases()
    project_templates, project_flowers = _collect_from_projects()
    cache_templates, cache_flowers = _collect_from_cache_directories()

    template_rows = _merge_with_existing_file(
        _combine_maps(db_templates, project_templates, cache_templates),
        FAVORITE_TEXT_TEMPLATES_CSV,
    )
    flower_rows = _merge_with_existing_file(
        _combine_maps(db_flowers, project_flowers, cache_flowers),
        FAVORITE_FLOWER_TEXTS_CSV,
    )

    _write_csv(
        FAVORITE_TEXT_TEMPLATES_CSV,
        [
            "identifier",
            "name",
            "title",
            "description",
            "resource_id",
            "effect_id",
            "source",
            "source_detail",
            "favorite_flag",
            "favorite_time",
            "categories",
        ],
        template_rows,
        [
            "JianYing favorite or cached text templates",
            "Built from rp.db, local drafts, and AITextTemplate cache",
            "Refresh with: python scripts/sync_jy_favorite_text_assets.py",
        ],
    )
    _write_csv(
        FAVORITE_FLOWER_TEXTS_CSV,
        [
            "identifier",
            "name",
            "title",
            "description",
            "resource_id",
            "effect_id",
            "source",
            "source_detail",
            "favorite_flag",
            "favorite_time",
            "categories",
        ],
        flower_rows,
        [
            "JianYing favorite or cached flower text effects",
            "Built from rp.db, local drafts, and artistEffect cache",
            "Refresh with: python scripts/sync_jy_favorite_text_assets.py",
        ],
    )
    return {
        "favorite_text_templates": len(template_rows),
        "favorite_flower_texts": len(flower_rows),
    }


if __name__ == "__main__":
    result = sync_favorite_text_assets()
    print(json.dumps(result, ensure_ascii=False, indent=2))
