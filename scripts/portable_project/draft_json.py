from __future__ import annotations

import json
import ntpath
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import PortableProjectError
from .snapshot import VerifiedSnapshot, verify_snapshot_receipt
from .topology import resolve_timeline_topology, validate_timeline_directories

_NON_FILE_RESOURCE_BUCKETS = frozenset(
    {
        "audio_effects",
        "digital_humans",
        "effects",
        "filters",
        "flowers",
        "material_animations",
        "plugin_effects",
        "stickers",
        "text_templates",
        "transitions",
        "video_effects",
    }
)
_RESOURCE_ID_FIELDS = (
    "resource_id",
    "effect_id",
    "sticker_id",
    "template_id",
    "animation_id",
    "transition_id",
    "filter_id",
    "id",
)
_CONTENT_PATH_FIELDS = ("path", "file_path", "file_Path")
_CONTENT_DISPLAY_NAME_FIELDS = ("material_name", "name")


def content_material_path_fields(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(field for field in _CONTENT_PATH_FIELDS if str(row.get(field) or "").strip())


def content_material_path(row: Mapping[str, Any]) -> str:
    fields = content_material_path_fields(row)
    return str(row.get(fields[0]) or "").strip() if fields else ""


def content_material_name_fields(row: Mapping[str, Any], *, bucket: str) -> tuple[str, ...]:
    existing = tuple(field for field in _CONTENT_DISPLAY_NAME_FIELDS if field in row)
    if existing:
        return existing
    return ("name",) if bucket == "audios" else ("material_name",)


@dataclass(frozen=True)
class MaterialReference:
    material_id: str
    bucket: str
    source_path: Path
    display_name: str
    basename: str
    json_locations: tuple[str, ...]


@dataclass(frozen=True)
class DraftDocumentSet:
    draft_dir: Path
    source_mode: str
    active_timeline_id: str
    root_content: dict[str, Any]
    active_content: dict[str, Any]
    timeline_contents: dict[str, dict[str, Any]]
    meta: dict[str, Any]
    virtual_store: dict[str, Any]
    timeline_layout: dict[str, Any]
    timeline_project: dict[str, Any]
    materials: tuple[MaterialReference, ...]


def discover_non_file_dependencies(document_set: DraftDocumentSet) -> list[dict[str, str]]:
    indexed: dict[tuple[str, str], dict[str, str]] = {}
    contents = [
        document_set.root_content,
        *(document_set.timeline_contents[key] for key in sorted(document_set.timeline_contents)),
    ]
    for content in contents:
        materials = content.get("materials")
        if not isinstance(materials, dict):
            continue
        for bucket in sorted(_NON_FILE_RESOURCE_BUCKETS):
            rows = materials.get(bucket)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or content_material_path(row):
                    continue
                resource_id = next(
                    (
                        str(row.get(field) or "").strip()
                        for field in _RESOURCE_ID_FIELDS
                        if str(row.get(field) or "").strip()
                    ),
                    "",
                )
                if not resource_id:
                    continue
                name = str(
                    row.get("name")
                    or row.get("resource_name")
                    or row.get("effect_name")
                    or row.get("title")
                    or ""
                ).strip()
                indexed[(bucket, resource_id)] = {
                    "kind": bucket,
                    "name": name,
                    "resource_id": resource_id,
                }
        text_rows = materials.get("texts")
        if isinstance(text_rows, list):
            for row in text_rows:
                if not isinstance(row, dict):
                    continue
                font_id = str(row.get("font_id") or row.get("font_resource_id") or "").strip()
                if not font_id:
                    continue
                font_name = str(
                    row.get("font_name") or row.get("font_family") or row.get("font_title") or ""
                ).strip()
                indexed[("fonts", font_id)] = {
                    "kind": "fonts",
                    "name": font_name,
                    "resource_id": font_id,
                }
    return [indexed[key] for key in sorted(indexed)]


def _read_json_object(path: Path, *, code: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableProjectError(
            code, f"{label} is not readable JSON", {"file": path.name}
        ) from exc
    if not isinstance(payload, dict):
        raise PortableProjectError(code, f"{label} is not a JSON object", {"file": path.name})
    return payload


def _optional_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return _read_json_object(path, code="invalid_draft_metadata", label=path.name)


def _timeline_declarations(
    layout: Mapping[str, Any], timeline_project: Mapping[str, Any], root_content: Mapping[str, Any]
) -> tuple[str, tuple[str, ...]]:
    topology = resolve_timeline_topology(layout, timeline_project, root_content)
    return topology.active_timeline_id, topology.declared_timeline_ids


def _content_material_rows(
    content: Mapping[str, Any], *, prefix: str
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    found: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    materials = content.get("materials")
    if not isinstance(materials, dict):
        return found
    for bucket_name, rows in materials.items():
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            material_id = str(
                row.get("id") or row.get("material_id") or row.get("music_id") or ""
            ).strip()
            path = content_material_path(row)
            if not material_id or not path:
                continue
            found.setdefault(material_id, []).append(
                (f"{prefix}:materials.{bucket_name}[{index}]", row)
            )
    return found


def _meta_material_rows(meta: Mapping[str, Any]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    found: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    buckets = meta.get("draft_materials")
    if not isinstance(buckets, list):
        return found
    for bucket_index, bucket in enumerate(buckets):
        if not isinstance(bucket, dict):
            continue
        values = bucket.get("value")
        if not isinstance(values, list):
            continue
        for index, row in enumerate(values):
            if not isinstance(row, dict):
                continue
            material_id = str(row.get("id") or "").strip()
            path = str(row.get("file_Path") or "").strip()
            if not material_id or not path:
                continue
            found.setdefault(material_id, []).append(
                (f"meta:draft_materials[{bucket_index}].value[{index}]", row)
            )
    return found


def _is_fully_qualified_windows_path(value: str) -> bool:
    raw_value = str(value or "").strip().replace("/", "\\")
    if raw_value.startswith(("\\\\?\\", "\\\\.\\")):
        return False
    drive, tail = ntpath.splitdrive(raw_value)
    if len(drive) == 2 and drive[0].isalpha() and drive[1] == ":":
        return tail.startswith("\\")
    if drive.startswith("\\\\") and tail.startswith("\\"):
        share_parts = [part for part in drive[2:].split("\\") if part]
        return len(share_parts) == 2
    return False


def _normalized_path(value: str) -> Path:
    raw_value = str(value).strip()
    if not _is_fully_qualified_windows_path(raw_value):
        raise PortableProjectError(
            "invalid_local_media_path",
            "Local media references must use a fully qualified Windows path",
        )
    normalized = ntpath.normpath(raw_value)
    return Path(normalized)


def _validate_virtual_store_paths(value: Any, *, location: str = "virtual_store") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_virtual_store_paths(child, location=f"{location}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_virtual_store_paths(child, location=f"{location}[{index}]")
        return
    if not isinstance(value, str):
        return
    text = value.strip()
    if ntpath.isabs(text) or text.startswith(("\\\\?\\", "\\\\.\\")):
        raise PortableProjectError(
            "unsupported_virtual_store_path",
            "draft_virtual_store.json contains an unsupported local path field",
            {"location": location},
        )


def _classify_bucket(row: Mapping[str, Any], path: Path) -> str:
    if str(row.get("type") or "").casefold() == "photo":
        return "image"
    suffix = path.suffix.casefold()
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}:
        return "image"
    if "music_id" in row or suffix in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}:
        return "audio"
    return "video"


def _material_references(
    root_content: Mapping[str, Any],
    timeline_contents: Mapping[str, Mapping[str, Any]],
    meta: Mapping[str, Any],
    *,
    active_timeline_id: str,
) -> tuple[MaterialReference, ...]:
    root_rows = _content_material_rows(root_content, prefix="root")
    timeline_rows = {
        timeline_id: _content_material_rows(
            content,
            prefix=(
                f"active_timeline:{timeline_id}"
                if timeline_id == active_timeline_id
                else f"timeline:{timeline_id}"
            ),
        )
        for timeline_id, content in timeline_contents.items()
    }
    active_rows = timeline_rows.get(active_timeline_id, root_rows)
    meta_rows = _meta_material_rows(meta)
    if set(root_rows) != set(active_rows):
        raise PortableProjectError(
            "root_active_material_mismatch",
            "Root and active timeline local material IDs do not match",
            {
                "root_only": sorted(set(root_rows) - set(active_rows)),
                "active_only": sorted(set(active_rows) - set(root_rows)),
            },
        )

    all_ids = set(root_rows) | set(meta_rows)
    for rows in timeline_rows.values():
        all_ids.update(rows)
    result: list[MaterialReference] = []
    for material_id in sorted(all_ids):
        occurrences = [*root_rows.get(material_id, [])]
        for timeline_id in sorted(timeline_rows):
            occurrences.extend(timeline_rows[timeline_id].get(material_id, []))
        occurrences.extend(meta_rows.get(material_id, []))
        first_row = occurrences[0][1]
        first_path = _normalized_path(content_material_path(first_row))
        all_paths = {
            os.path.normcase(str(_normalized_path(str(row.get(field) or ""))))
            for _location, row in occurrences
            for field in content_material_path_fields(row)
        }
        if len(all_paths) != 1:
            raise PortableProjectError(
                "root_active_material_mismatch",
                "Material mirrors refer to different source paths",
                {"material_id": material_id},
            )
        display_name = next(
            (
                str(first_row.get(field) or "")
                for field in _CONTENT_DISPLAY_NAME_FIELDS
                if str(first_row.get(field) or "").strip()
            ),
            str(first_row.get("extra_info") or ""),
        )
        result.append(
            MaterialReference(
                material_id=material_id,
                bucket=_classify_bucket(first_row, first_path),
                source_path=first_path,
                display_name=display_name,
                basename=first_path.name,
                json_locations=tuple(location for location, _row in occurrences),
            )
        )
    return tuple(result)


def _validate_timeline_identity(
    root_content: Mapping[str, Any], active_content: Mapping[str, Any], active_timeline_id: str
) -> None:
    root_id = str(root_content.get("id") or "").strip()
    active_id = str(active_content.get("id") or "").strip()
    if active_timeline_id and root_id != active_timeline_id:
        raise PortableProjectError(
            "root_active_material_mismatch",
            "Readable snapshot timeline identity does not match the saved draft",
            {"root_timeline_id": root_id, "active_timeline_id": active_timeline_id},
        )
    if active_timeline_id and active_id != active_timeline_id:
        raise PortableProjectError(
            "root_active_material_mismatch",
            "Active timeline content ID does not match its declaration",
            {"active_timeline_id": active_timeline_id, "active_content_id": active_id},
        )


def _load_document_payloads(
    source_dir: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
    dict[str, dict[str, Any]],
]:
    layout = _optional_json_object(source_dir / "timeline_layout.json")
    timeline_project = _optional_json_object(source_dir / "Timelines" / "project.json")
    preflight_topology = resolve_timeline_topology(layout, timeline_project)
    validate_timeline_directories(source_dir, preflight_topology)
    root_content = _read_json_object(
        source_dir / "draft_content.json",
        code="unpatchable_draft_content",
        label="draft_content.json",
    )
    meta = _read_json_object(
        source_dir / "draft_meta_info.json",
        code="unpatchable_draft_content",
        label="draft_meta_info.json",
    )
    virtual_store = _optional_json_object(source_dir / "draft_virtual_store.json")
    _validate_virtual_store_paths(virtual_store)
    active_id, timeline_ids = _timeline_declarations(layout, timeline_project, root_content)
    timeline_contents: dict[str, dict[str, Any]] = {}
    for timeline_id in timeline_ids:
        content_path = source_dir / "Timelines" / timeline_id / "draft_content.json"
        if not content_path.is_file():
            raise PortableProjectError(
                "active_timeline_missing" if timeline_id == active_id else "timeline_missing",
                "A declared timeline draft_content.json is missing",
                {"timeline_id": timeline_id},
            )
        content = _read_json_object(
            content_path,
            code="unpatchable_draft_content",
            label=f"timeline {timeline_id}",
        )
        content_id = str(content.get("id") or "").strip()
        if content_id != timeline_id:
            raise PortableProjectError(
                "timeline_declaration_mismatch",
                "Timeline content ID does not match its declared directory",
                {"timeline_id": timeline_id, "content_id": content_id},
            )
        timeline_contents[timeline_id] = content
    active_content = timeline_contents.get(active_id, deepcopy(root_content))
    _validate_timeline_identity(root_content, active_content, active_id)
    return (
        root_content,
        meta,
        virtual_store,
        layout,
        timeline_project,
        active_id,
        timeline_contents,
    )


def _load_verified_snapshot_payloads(
    verified: VerifiedSnapshot,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
    dict[str, dict[str, Any]],
]:
    documents = verified.documents
    root_content = deepcopy(documents["draft_content.json"])
    meta = deepcopy(documents["draft_meta_info.json"])
    virtual_store = deepcopy(documents.get("draft_virtual_store.json", {}))
    layout = deepcopy(documents.get("timeline_layout.json", {}))
    timeline_project = deepcopy(documents.get("Timelines/project.json", {}))
    _validate_virtual_store_paths(virtual_store)
    timeline_contents = {
        timeline_id: deepcopy(documents[f"Timelines/{timeline_id}/draft_content.json"])
        for timeline_id in verified.timeline_ids
    }
    active_content = timeline_contents.get(verified.active_timeline_id, deepcopy(root_content))
    _validate_timeline_identity(root_content, active_content, verified.active_timeline_id)
    return (
        root_content,
        meta,
        virtual_store,
        layout,
        timeline_project,
        verified.active_timeline_id,
        timeline_contents,
    )


def discover_material_references(
    draft_dir: Path,
    snapshot_dir: Path | None = None,
    *,
    expected_snapshot_job_digest: str | None = None,
    expected_snapshot_receipt_sha256: str | None = None,
) -> DraftDocumentSet:
    draft = Path(draft_dir).resolve()
    if not draft.is_dir():
        raise PortableProjectError("draft_not_found", "Draft directory does not exist")
    source_mode = "saved_readable"
    try:
        (
            root_content,
            meta,
            virtual_store,
            layout,
            timeline_project,
            active_id,
            timeline_contents,
        ) = _load_document_payloads(draft)
    except PortableProjectError as exc:
        if snapshot_dir is None or exc.code != "unpatchable_draft_content":
            raise
        snapshot = Path(snapshot_dir).resolve()
        if not expected_snapshot_job_digest or not expected_snapshot_receipt_sha256:
            raise PortableProjectError(
                "untrusted_snapshot",
                "Snapshot fallback requires trusted job and receipt digests",
            ) from exc
        verified = verify_snapshot_receipt(
            snapshot,
            source_draft_dir=draft,
            expected_job_digest=expected_snapshot_job_digest,
            expected_receipt_sha256=expected_snapshot_receipt_sha256,
        )
        (
            root_content,
            meta,
            virtual_store,
            layout,
            timeline_project,
            active_id,
            timeline_contents,
        ) = _load_verified_snapshot_payloads(verified)
        source_mode = "verified_snapshot"
    active_content = timeline_contents.get(active_id, deepcopy(root_content))
    materials = _material_references(
        root_content,
        timeline_contents,
        meta,
        active_timeline_id=active_id,
    )
    return DraftDocumentSet(
        draft_dir=draft,
        source_mode=source_mode,
        active_timeline_id=active_id,
        root_content=root_content,
        active_content=active_content,
        timeline_contents=timeline_contents,
        meta=meta,
        virtual_store=virtual_store,
        timeline_layout=layout,
        timeline_project=timeline_project,
        materials=materials,
    )


def _target_basename(value: str) -> str:
    return ntpath.basename(str(value).replace("/", "\\"))


def _normalized_target(
    value: str | os.PathLike[str],
    *,
    logical_marker: str,
    allow_logical_suffix: bool,
) -> str:
    text = os.fspath(value).strip()
    if allow_logical_suffix and text.startswith(f"{logical_marker}/"):
        suffix_parts = text[len(logical_marker) + 1 :].split("/")
        if "\\" in text or any(part in {"", ".", ".."} or ":" in part for part in suffix_parts):
            raise PortableProjectError("invalid_material_target", "Logical target path is unsafe")
        return text
    if not allow_logical_suffix and text == logical_marker:
        return text
    if text.startswith("@AUTOCUT_") or not _is_fully_qualified_windows_path(text):
        raise PortableProjectError(
            "invalid_material_target", "Installed target path must be fully qualified"
        )
    return ntpath.normpath(text)


def _rewrite_content(content: Mapping[str, Any], targets: Mapping[str, str]) -> dict[str, Any]:
    result = deepcopy(content)
    materials = result.get("materials")
    if not isinstance(materials, dict):
        return result
    for bucket_name, rows in materials.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            material_id = str(
                row.get("id") or row.get("material_id") or row.get("music_id") or ""
            ).strip()
            target = targets.get(material_id)
            path_fields = content_material_path_fields(row)
            if target is None or not path_fields:
                continue
            for field in path_fields:
                row[field] = target
            basename = _target_basename(target)
            for field in content_material_name_fields(row, bucket=bucket_name):
                row[field] = basename
    return result


def _rewrite_meta(
    meta: Mapping[str, Any],
    targets: Mapping[str, str],
    target_draft_root: str,
    target_draft_dir: str,
    project_name: str,
) -> dict[str, Any]:
    result = deepcopy(meta)
    result["draft_name"] = project_name
    result["draft_root_path"] = target_draft_root
    result["draft_fold_path"] = target_draft_dir
    buckets = result.get("draft_materials")
    if not isinstance(buckets, list):
        return result
    for bucket in buckets:
        if not isinstance(bucket, dict) or not isinstance(bucket.get("value"), list):
            continue
        for row in bucket["value"]:
            if not isinstance(row, dict):
                continue
            target = targets.get(str(row.get("id") or "").strip())
            if target is None or not str(row.get("file_Path") or "").strip():
                continue
            row["file_Path"] = target.replace("\\", "/")
            row["extra_info"] = _target_basename(target)
    return result


def rewrite_draft_documents(
    document_set: DraftDocumentSet,
    material_targets: Mapping[str, str | os.PathLike[str]],
    *,
    target_draft_dir: str | os.PathLike[str],
    target_draft_root: str | os.PathLike[str] | None = None,
    project_name: str,
) -> dict[str, dict[str, Any]]:
    targets = {
        str(key): _normalized_target(
            value,
            logical_marker="@AUTOCUT_MEDIA@",
            allow_logical_suffix=True,
        )
        for key, value in material_targets.items()
    }
    missing = sorted({row.material_id for row in document_set.materials} - set(targets))
    if missing:
        raise PortableProjectError(
            "missing_material_target",
            "Not every local material has a target path",
            {"material_ids": missing},
        )
    draft_dir_value = _normalized_target(
        target_draft_dir,
        logical_marker="@AUTOCUT_DRAFT_DIR@",
        allow_logical_suffix=False,
    )
    if target_draft_root is None:
        if draft_dir_value.startswith("@"):
            raise PortableProjectError(
                "invalid_material_target", "Logical draft directory requires a logical root"
            )
        draft_root_value = ntpath.dirname(draft_dir_value)
    else:
        draft_root_value = _normalized_target(
            target_draft_root,
            logical_marker="@AUTOCUT_DRAFT_ROOT@",
            allow_logical_suffix=False,
        )
    result = {
        "draft_content.json": _rewrite_content(document_set.root_content, targets),
        "draft_meta_info.json": _rewrite_meta(
            document_set.meta,
            targets,
            draft_root_value,
            draft_dir_value,
            str(project_name),
        ),
        "draft_virtual_store.json": deepcopy(document_set.virtual_store),
    }
    for timeline_id, content in document_set.timeline_contents.items():
        result[f"Timelines/{timeline_id}/draft_content.json"] = _rewrite_content(content, targets)
    return result
