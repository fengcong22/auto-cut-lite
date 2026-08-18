"""Compile document snapshots into canonical, resumable review-job inputs."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from utils.revision_models import (
    AcceptanceRules,
    PreservationRules,
    RevisionProject,
    RevisionRequest,
    RevisionReviewItem,
    _as_bool,
    _classify_review_text,
    _looks_execution_required,
    _normalize_review_id,
)
from utils.revision_validation import derive_acceptance_profile

_SCHEMA_VERSION = 1
_TOOL_NAME = "auto-cut-review-job-compiler"
_TOOL_VERSION = 1
_OUTPUT_NAMES = {
    "doc_items": "doc_items.json",
    "revision_request": "revision_request.json",
    "job_manifest": "job_manifest.json",
}
_ITEM_ALIASES = ("doc_items", "review_items", "items")
_ID_ALIASES = ("id", "item_id", "clip_id")
_BLOCK_ID_ALIASES = ("block_id", "source_block_id", "blockId")
_SOURCE_FALLBACKS = ("text", "detail", "comment", "label")
_DOCUMENT_FIELDS = (
    "token",
    "url",
    "revision",
    "version",
    "schema",
    "schema_version",
    "document_token",
    "document_url",
    "document_revision",
    "revision_id",
    "doc_token",
    "extraction_schema_version",
)
_REVIEW_ONLY_HINTS = ("校对", "核对", "检查", "确认", "review", "check", "verify")
_RANGE_SEPARATOR = re.compile(r"\s*(?:-|–|—|~|至|\bto\b)\s*", re.IGNORECASE)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"input must contain only finite JSON values: {error}") from error


def _json_file_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write_json(path: Path, value: Any) -> str:
    payload = _json_file_bytes(value)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping_copy(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    copied = copy.deepcopy(dict(value))
    _canonical_json_bytes(copied)
    return copied


def _extract_items(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    selected: Any = None
    selected_name = "review_items"
    for alias in _ITEM_ALIASES:
        if alias in snapshot:
            selected = snapshot[alias]
            selected_name = alias
            break
    if selected is None:
        raise ValueError("snapshot must contain review_items, doc_items, or items")
    if not isinstance(selected, list):
        raise ValueError(f"snapshot.{selected_name} must be a list")

    rows: list[dict[str, Any]] = []
    for index, row in enumerate(selected):
        if not isinstance(row, Mapping):
            raise ValueError(f"snapshot.{selected_name}[{index}] must be an object")
        rows.append(copy.deepcopy(dict(row)))
    return rows


def _document_identity(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    nested = snapshot.get("document")
    if nested is not None and not isinstance(nested, Mapping):
        raise ValueError("snapshot.document must be an object when provided")
    identity = (
        {field: copy.deepcopy(nested[field]) for field in _DOCUMENT_FIELDS if field in nested}
        if isinstance(nested, Mapping)
        else {}
    )
    for field in _DOCUMENT_FIELDS:
        if field in snapshot and field not in identity:
            identity[field] = copy.deepcopy(snapshot[field])
    return identity


def _first_text(row: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[str, str]:
    for key in keys:
        if key not in row or row[key] is None:
            continue
        value = row[key]
        if not isinstance(value, str):
            raise ValueError(f"review item {key} must be text when provided")
        if value.strip():
            return key, value
    return "", ""


def _explicit_id(row: Mapping[str, Any]) -> str:
    for key in _ID_ALIASES:
        if key not in row or row[key] is None:
            continue
        value = row[key]
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError(f"review item {key} must be scalar text")
        candidate = str(value).strip()
        if candidate:
            return candidate
    return ""


def _block_id(row: Mapping[str, Any]) -> str:
    for key in _BLOCK_ID_ALIASES:
        if key not in row or row[key] is None:
            continue
        value = row[key]
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError(f"review item {key} must be scalar text")
        candidate = str(value).strip()
        if candidate:
            return candidate
    return ""


def _clock_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        number = math.nan
    if math.isfinite(number) and number >= 0:
        return number

    parts = text.split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        numbers = [float(part.strip()) for part in parts]
    except ValueError:
        return None
    if any(not math.isfinite(number) or number < 0 for number in numbers):
        return None
    if len(numbers) == 2:
        minutes, seconds = numbers
        if seconds >= 60:
            return None
        return minutes * 60 + seconds
    hours, minutes, seconds = numbers
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _rough_time_range(value: Any) -> tuple[float | None, float | None]:
    if isinstance(value, Mapping):
        return _clock_seconds(value.get("start")), _clock_seconds(value.get("end"))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return _clock_seconds(value[0]), _clock_seconds(value[1])
    if not isinstance(value, str):
        return None, None
    parts = _RANGE_SEPARATOR.split(value.strip(), maxsplit=1)
    if len(parts) != 2:
        return None, None
    return _clock_seconds(parts[0]), _clock_seconds(parts[1])


def _normalized_times(
    row: Mapping[str, Any], item_id: str, warnings: list[str]
) -> tuple[float | None, float | None]:
    start_supplied = "start" in row and row.get("start") is not None and row.get("start") != ""
    end_supplied = "end" in row and row.get("end") is not None and row.get("end") != ""
    start = _clock_seconds(row.get("start")) if start_supplied else None
    end = _clock_seconds(row.get("end")) if end_supplied else None
    invalid = (start_supplied and start is None) or (end_supplied and end is None)

    if "rough_time" in row:
        rough_start, rough_end = _rough_time_range(row.get("rough_time"))
        if start is None and not start_supplied:
            start = rough_start
        if end is None and not end_supplied:
            end = rough_end
        if rough_start is None or rough_end is None:
            invalid = True

    if start is not None and end is not None and end <= start:
        start = None
        end = None
        invalid = True
    if invalid:
        warnings.append(f"Review item {item_id} contains an invalid time value; it was left unset.")
    return start, end


def _infer_execution_required(text: str, kind: str, *, explicit_kind: bool) -> bool:
    if explicit_kind:
        return kind != "review_only"
    inferred = _looks_execution_required(text, kind)
    if inferred or kind != "review_only":
        return inferred
    folded = text.casefold()
    if any(hint.casefold() in folded for hint in _REVIEW_ONLY_HINTS):
        return False
    return True


def _content_id(row: Mapping[str, Any], source_text: str, kind: str) -> str:
    identity = {
        "source": row.get("source"),
        "source_text": source_text,
        "kind": kind,
        "start": row.get("start"),
        "end": row.get("end"),
        "rough_time": row.get("rough_time"),
    }
    digest = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()[:16]
    return f"item_{digest}"


def _unique_fallback_id(preferred: str, safe_base: str, used_ids: set[str]) -> str:
    preferred_normalized = _normalize_review_id(preferred)
    if preferred_normalized not in used_ids:
        used_ids.add(preferred_normalized)
        return preferred

    safe_normalized = _normalize_review_id(safe_base)
    if safe_normalized not in used_ids:
        used_ids.add(safe_normalized)
        return safe_base

    # At most len(used_ids) candidates can already be occupied, so one of the
    # following safe names must be free within this finite bound.
    for occurrence in range(2, len(used_ids) + 3):
        candidate = f"{safe_base}_{occurrence:02d}"
        normalized = _normalize_review_id(candidate)
        if normalized not in used_ids:
            used_ids.add(normalized)
            return candidate
    raise ValueError("Unable to allocate a unique fallback review item id")


def _canonical_review_items(
    source_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    explicit_ids: list[str] = []
    normalized_explicit: dict[str, str] = {}
    for row in source_rows:
        item_id = _explicit_id(row)
        explicit_ids.append(item_id)
        if not item_id:
            continue
        normalized = _normalize_review_id(item_id)
        if normalized in normalized_explicit:
            raise ValueError(
                f"Duplicate explicit review item id: {normalized_explicit[normalized]} / {item_id}"
            )
        normalized_explicit[normalized] = item_id

    used_ids = set(normalized_explicit)
    warnings: list[str] = []
    canonical: list[dict[str, Any]] = []
    for index, (source_row, explicit_id) in enumerate(zip(source_rows, explicit_ids)):
        row = copy.deepcopy(source_row)
        block_id = _block_id(source_row)

        explicit_source_text = ""
        if "source_text" in source_row and source_row["source_text"] is not None:
            if not isinstance(source_row["source_text"], str):
                raise ValueError(f"review item source_text must be text at index {index}")
            explicit_source_text = source_row["source_text"]
        if explicit_source_text.strip():
            source_text = explicit_source_text
            has_source_text = True
        else:
            _, fallback_text = _first_text(source_row, _SOURCE_FALLBACKS)
            source_text = fallback_text or explicit_source_text
            has_source_text = False

        for metadata_field in ("evidence", "validation"):
            metadata = source_row.get(metadata_field)
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError(f"review item {metadata_field} must be an object at index {index}")

        explicit_kind_value = source_row.get("kind") or source_row.get("type")
        explicit_kind = bool(str(explicit_kind_value or "").strip())
        kind_text = str(explicit_kind_value or "").strip()
        inference_text = " ".join(
            value for value in (str(source_row.get("label") or ""), source_text) if value
        )
        kind = kind_text or _classify_review_text(inference_text)

        if explicit_id:
            item_id = explicit_id
        else:
            safe_base_id = _content_id(source_row, source_text, kind)
            preferred_id = block_id or safe_base_id
            item_id = _unique_fallback_id(preferred_id, safe_base_id, used_ids)

        if "execution_required" in source_row:
            execution_required = _as_bool(source_row.get("execution_required"), True)
        else:
            execution_required = _infer_execution_required(
                inference_text, kind, explicit_kind=explicit_kind
            )

        explicit_status = str(source_row.get("verbatim_status") or "").strip()
        verbatim_status = (
            explicit_status or "verified" if has_source_text else "unverified_source_unavailable"
        )
        if verbatim_status == "unverified_source_unavailable":
            warnings.append(f"Review item {item_id} source text is unverified or unavailable.")

        explicit_source = source_row.get("source")
        if explicit_source is not None and not isinstance(explicit_source, str):
            raise ValueError(f"review item source must be text at index {index}")
        source = (
            explicit_source if explicit_source else (f"feishu_block:{block_id}" if block_id else "")
        )
        start, end = _normalized_times(source_row, item_id, warnings)

        row["id"] = item_id
        row["source_text"] = source_text
        row["source"] = source
        if block_id:
            row["block_id"] = block_id
        row["kind"] = kind
        row["execution_required"] = execution_required
        row["verbatim_status"] = verbatim_status
        row.pop("start", None)
        row.pop("end", None)
        if start is not None:
            row["start"] = start
        if end is not None:
            row["end"] = end
        canonical.append(row)
    return canonical, warnings


def _normalize_project(project: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(project)
    for field in ("draft_name", "source_video", "source_audio", "replacement_audio", "project_key"):
        if field in normalized and normalized[field] is not None:
            if not isinstance(normalized[field], str):
                raise ValueError(f"project.{field} must be text")
            normalized[field] = normalized[field].strip()
    if not normalized.get("draft_name"):
        raise ValueError("project.draft_name is required")
    if not normalized.get("source_video"):
        raise ValueError("project.source_video is required")
    normalized.setdefault("source_audio", "")
    normalized.setdefault("replacement_audio", "")
    normalized.setdefault("project_key", "")
    workflow_mode = str(normalized.get("workflow_mode") or "full").strip().lower()
    if workflow_mode not in {"full", "lite"}:
        raise ValueError("project.workflow_mode must be either 'full' or 'lite'")
    normalized["workflow_mode"] = workflow_mode
    return normalized


def _request_model(
    project: Mapping[str, Any],
    review_items: list[dict[str, Any]],
    acceptance: Mapping[str, Any],
) -> tuple[RevisionRequest, list[RevisionReviewItem]]:
    model_items = [
        RevisionReviewItem(
            item_id=str(item["id"]),
            kind=str(item["kind"]),
            source_text=str(item["source_text"]),
            source=str(item.get("source") or ""),
            start=item.get("start"),
            end=item.get("end"),
            execution_required=bool(item["execution_required"]),
            evidence=(item.get("evidence") if isinstance(item.get("evidence"), dict) else {}),
            validation=(item.get("validation") if isinstance(item.get("validation"), dict) else {}),
            verbatim_status=str(item["verbatim_status"]),
        )
        for item in review_items
    ]
    rules = AcceptanceRules(
        expected_review_item_count=int(acceptance["expected_review_item_count"]),
        expected_review_item_ids=list(acceptance["expected_review_item_ids"]),
        require_review_items=True,
        require_execution_evidence=True,
        require_audio_validation=bool(acceptance["require_audio_validation"]),
        require_visual_evidence=bool(acceptance["require_visual_evidence"]),
        require_pause_validation=bool(acceptance["require_pause_validation"]),
        require_subject_pointer_binding=bool(acceptance["require_subject_pointer_binding"]),
        require_pointer_lifecycle_evidence=bool(acceptance["require_pointer_lifecycle_evidence"]),
        require_final_acceptance=bool(acceptance["require_final_acceptance"]),
        _explicit_require_execution_evidence=True,
        _explicit_require_audio_validation=True,
        _explicit_require_visual_evidence=True,
        _explicit_require_pause_validation=True,
    )
    request = RevisionRequest(
        project=RevisionProject(
            draft_name=str(project["draft_name"]),
            source_video=str(project["source_video"]),
            source_audio=str(project.get("source_audio") or ""),
            replacement_audio=str(project.get("replacement_audio") or ""),
            project_key=str(project.get("project_key") or ""),
        ),
        edits=[],
        markers=[],
        preserve=PreservationRules(),
        review_items=model_items,
        acceptance=rules,
        workflow_mode=str(project.get("workflow_mode") or "full"),
    )
    return request, model_items


def _acceptance_payload(review_items: list[dict[str, Any]]) -> dict[str, Any]:
    provisional = {
        "expected_review_item_count": len(review_items),
        "expected_review_item_ids": [str(item["id"]) for item in review_items],
        "require_review_items": True,
        "require_execution_evidence": True,
        "require_audio_validation": False,
        "require_visual_evidence": False,
        "require_pause_validation": False,
        "require_subject_pointer_binding": False,
        "require_pointer_lifecycle_evidence": False,
        "require_final_acceptance": True,
    }
    return provisional


def compile_review_job(snapshot: dict, project: dict, output_dir: str | Path) -> dict[str, Any]:
    """Compile a source-document snapshot into canonical editable-revision inputs."""

    snapshot_copy = _mapping_copy(snapshot, "snapshot")
    project_copy = _mapping_copy(project, "project")
    source_rows = _extract_items(snapshot_copy)
    document = _document_identity(snapshot_copy)
    review_items, warnings = _canonical_review_items(source_rows)
    normalized_project = _normalize_project(project_copy)
    workflow_mode = str(normalized_project.get("workflow_mode") or "full")

    acceptance = _acceptance_payload(review_items)
    provisional_request, model_items = _request_model(normalized_project, review_items, acceptance)
    provisional_profile = derive_acceptance_profile(provisional_request, doc_items=model_items)
    enabled_gates = set(provisional_profile["enabled_gates"])
    lite_mode = workflow_mode == "lite"
    requires_segmented_audio = (
        False if lite_mode else bool({"audio_precision", "audio_join"}.intersection(enabled_gates))
    )
    acceptance["require_audio_validation"] = (
        False if lite_mode else bool({"audio_precision", "audio_join"} & enabled_gates)
    )
    acceptance["require_visual_evidence"] = "visual" in enabled_gates
    acceptance["require_pause_validation"] = False if lite_mode else "pause_fit" in enabled_gates
    acceptance["require_subject_pointer_binding"] = False if lite_mode else "pointer" in enabled_gates
    acceptance["require_pointer_lifecycle_evidence"] = (
        False if lite_mode else "pointer" in enabled_gates
    )
    request_model, model_items = _request_model(normalized_project, review_items, acceptance)
    acceptance_profile = derive_acceptance_profile(request_model, doc_items=model_items)

    preserve = {
        "source_video_material": True,
        "separated_audio_material": True,
        "replacement_audio_material": True,
        "keep_cut_points": True,
        "keep_review_markers_separate": True,
    }
    doc_items_payload = {
        "schema_version": _SCHEMA_VERSION,
        "document": document,
        "review_items": review_items,
    }
    revision_request_payload = {
        "schema_version": _SCHEMA_VERSION,
        "workflow_mode": workflow_mode,
        "document": document,
        "project": normalized_project,
        "edits": [],
        "markers": [],
        "review_items": review_items,
        "acceptance": acceptance,
        "acceptance_profile": acceptance_profile,
        "preserve": preserve,
        "audio_delivery_plan": (
            {
                "mode": "segmented",
                "pending": True,
                "forbid_full_length_segments": True,
                "max_single_segment_ratio": 0.9,
                "validation_only_audio_paths": [],
                "segments": [],
            }
            if requires_segmented_audio
            else {"mode": "legacy"}
        ),
    }

    if not isinstance(output_dir, (str, os.PathLike)):
        raise TypeError("output_dir must be a path")
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths = {key: root / name for key, name in _OUTPUT_NAMES.items()}
    if any(path.parent.resolve() != root for path in paths.values()):
        raise ValueError("compiled output path escapes output_dir")

    manifest_path = paths["job_manifest"]
    try:
        manifest_path.unlink()
    except FileNotFoundError:
        pass

    digests: dict[str, str] = {}
    digests["doc_items"] = _atomic_write_json(paths["doc_items"], doc_items_payload)
    digests["revision_request"] = _atomic_write_json(
        paths["revision_request"], revision_request_payload
    )

    warning_ids = [
        str(item["id"])
        for item in review_items
        if any(f"Review item {item['id']} " in warning for warning in warnings)
    ]
    warning_ids = list(dict.fromkeys(warning_ids))
    unverified_ids = [
        str(item["id"])
        for item in review_items
        if item["verbatim_status"] == "unverified_source_unavailable"
    ]
    outputs = {
        key: {
            "path": str(paths[key]),
            "relative_path": paths[key].relative_to(root).as_posix(),
            "sha256": digests[key],
        }
        for key in ("doc_items", "revision_request")
    }
    outputs["job_manifest"] = {
        "path": str(manifest_path),
        "relative_path": manifest_path.relative_to(root).as_posix(),
    }
    source_materials: dict[str, dict[str, Any]] = {}
    for field in ("source_video", "source_audio", "replacement_audio"):
        raw_path = str(normalized_project.get(field) or "").strip()
        if not raw_path:
            continue
        material_path = Path(raw_path).expanduser()
        exists = material_path.is_file()
        source_materials[field] = {
            "path": str(material_path.resolve()) if exists else raw_path,
            "exists": exists,
            "sha256": _sha256_path(material_path) if exists else None,
        }

    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "tool": {"name": _TOOL_NAME, "version": _TOOL_VERSION},
        "tool_version": _TOOL_VERSION,
        "document": document,
        "document_revision": document.get("revision", document.get("document_revision")),
        "inputs": {
            "snapshot_sha256": hashlib.sha256(_canonical_json_bytes(snapshot_copy)).hexdigest(),
            "project_sha256": hashlib.sha256(_canonical_json_bytes(project_copy)).hexdigest(),
            "source_materials": source_materials,
        },
        "outputs": outputs,
        "item_count": len(review_items),
        "item_ids": [str(item["id"]) for item in review_items],
        "unverified_item_ids": unverified_ids,
        "warning_item_ids": warning_ids,
        "warnings": warnings,
        "acceptance_profile": acceptance_profile,
        "acceptance_gates": acceptance_profile["enabled_gates"],
    }
    digests["job_manifest"] = _atomic_write_json(manifest_path, manifest)

    return {
        "doc_items": str(paths["doc_items"]),
        "revision_request": str(paths["revision_request"]),
        "job_manifest": str(manifest_path),
        "digests": digests,
    }


__all__ = ["compile_review_job"]
