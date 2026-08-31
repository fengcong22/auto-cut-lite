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
from typing import Any, Iterable

from utils.replacement_timebase import resolve_review_timebases
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
    lite_execution_required,
    lite_pause_change_is_label_only,
    lite_timing_source,
    resolve_execution_status,
)
from utils.revision_validation import derive_acceptance_profile

_SCHEMA_VERSION = 1
_TOOL_NAME = "auto-cut-review-job-compiler"
_TOOL_VERSION = 2
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
_REVIEW_CLOCK_PATTERN = re.compile(
    r"(?<!\d)(?P<first>\d{1,2})\s*[:\uff1a]\s*(?P<second>\d{1,2})"
    r"(?:\s*[:\uff1a]\s*(?P<third>\d{1,2}(?:\.\d+)?))?(?!\d)"
)
_TARGET_TIME_CUE_PATTERN = re.compile(
    r"(?:提前|推迟|延后|移到|挪到|调整到|改到|调到|放到|贴到|开始于|结束于)\s*"
)


_COLORED_NOTE_HINTS = (
    "蓝色字",
    "蓝字",
    "红色字",
    "红字",
    "标色字",
    "颜色字",
    "着色字",
)
_RICH_TEXT_KEYS = ("colored_spans", "text_runs", "rich_text_spans", "spans", "runs", "elements")
_TEXT_KEYS = ("text", "content", "value", "plain_text", "plainText")
_COLOR_KEYS = ("color", "text_color", "textColor", "foreground_color", "foregroundColor")


def _color_triplet(value: Any) -> tuple[float, float, float] | None:
    """Normalize Feishu color representations to 0..255 RGB."""
    if isinstance(value, Mapping):
        for key in _COLOR_KEYS:
            if key in value:
                return _color_triplet(value.get(key))
        if all(key in value for key in ("r", "g", "b")):
            return _color_triplet([value.get("r"), value.get("g"), value.get("b")])
        return None
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            numbers = [float(value[index]) for index in range(3)]
        except (TypeError, ValueError):
            return None
        if max(numbers) <= 1.0:
            numbers = [number * 255.0 for number in numbers]
        return tuple(numbers)  # type: ignore[return-value]
    text = str(value or "").strip().casefold()
    if not text:
        return None
    rgb_match = re.search(r"rgba?\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)", text)
    if rgb_match:
        return tuple(float(rgb_match.group(index)) for index in range(1, 4))  # type: ignore[return-value]
    hex_match = re.fullmatch(r"#?([0-9a-f]{6})(?:[0-9a-f]{2})?", text)
    if hex_match:
        raw = hex_match.group(1)
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    return None


def _is_blue_color(value: Any) -> bool:
    rgb = _color_triplet(value)
    if rgb is None:
        return False
    # Feishu's blue review color is rgb(36,91,219). Allow small theme rounding.
    return max(abs(rgb[index] - (36.0, 91.0, 219.0)[index]) for index in range(3)) <= 12.0


def _is_red_color(value: Any) -> bool:
    rgb = _color_triplet(value)
    if rgb is None:
        return False
    # Feishu's red review color is rgb(216,57,49). Allow small theme rounding.
    return max(abs(rgb[index] - (216.0, 57.0, 49.0)[index]) for index in range(3)) <= 12.0


def _is_review_delete_color(value: Any) -> bool:
    return _is_blue_color(value) or _is_red_color(value)


def _run_text(run: Mapping[str, Any]) -> str:
    for key in _TEXT_KEYS:
        value = run.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _run_color(run: Mapping[str, Any]) -> Any:
    for key in _COLOR_KEYS:
        if key in run:
            return run.get(key)
    for container_key in ("style", "attrs"):
        container = run.get(container_key)
        if isinstance(container, Mapping):
            for key in _COLOR_KEYS:
                if key in container:
                    return container.get(key)
    return None


def _iter_rich_runs(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if _run_text(value):
            yield value
        for key in _RICH_TEXT_KEYS:
            child = value.get(key)
            if isinstance(child, (Mapping, list, tuple)):
                yield from _iter_rich_runs(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_rich_runs(child)


def _extract_colored_spans(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract review-colored runs without collapsing uncolored text between them."""
    candidates: list[Any] = []
    for key in _RICH_TEXT_KEYS:
        if key in row:
            candidates.append(row.get(key))
    for key in ("markup", "html", "rich_text", "richText"):
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        for match in re.finditer(
            r"(?is)<(?:span|text|a)[^>]*(?:style|color)=[\"'][^\"']*(?:rgb\([^)]*\)|#[0-9a-f]{6,8})[^\"']*[\"'][^>]*>(.*?)</(?:span|text|a)>",
            value,
        ):
            attrs = value[match.start() : match.end()]
            color_match = re.search(r"(?:rgb\([^)]*\)|#[0-9a-f]{6,8})", attrs, re.IGNORECASE)
            text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if text and color_match and _is_review_delete_color(color_match.group(0)):
                candidates.append({"text": text, "color": color_match.group(0)})
    spans: list[dict[str, Any]] = []
    for run in _iter_rich_runs(candidates):
        text = _run_text(run)
        color = _run_color(run)
        explicitly_colored = any(
            run.get(key) is True for key in ("blue", "is_blue", "red", "is_red")
        )
        if not text or not (_is_review_delete_color(color) or explicitly_colored):
            continue
        default_color = (
            "rgb(216,57,49)"
            if run.get("red") is True or run.get("is_red") is True
            else "rgb(36,91,219)"
        )
        entry: dict[str, Any] = {"text": text, "color": color or default_color}
        for key in ("start", "end", "start_index", "end_index"):
            if key in run and run.get(key) not in (None, ""):
                entry[key] = run.get(key)
        spans.append(entry)
    return spans


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


def _review_text_times(text: str) -> list[tuple[float, int, int]]:
    values: list[tuple[float, int, int]] = []
    for match in _REVIEW_CLOCK_PATTERN.finditer(str(text or "")):
        third = match.group("third")
        if third is None:
            value = _clock_seconds(f"{match.group('first')}:{match.group('second')}")
        else:
            value = _clock_seconds(
                f"{match.group('first')}:{match.group('second')}:{third}"
            )
        if value is not None:
            values.append((value, match.start(), match.end()))
    return values


def _review_text_target_range(text: str) -> tuple[float | None, float | None, str]:
    values = _review_text_times(text)
    if not values:
        return None, None, "missing"

    for cue in _TARGET_TIME_CUE_PATTERN.finditer(str(text or "")):
        following = next((row for row in values if row[1] >= cue.end()), None)
        if following is not None:
            # A move/animation comment contains two clocks: the first is the
            # object's current (original) location and the second is the
            # requested destination.  Lite does not execute timing changes,
            # so its trace label belongs to the object at the original clock,
            # regardless of whether the move is earlier or later.  Keep the
            # target in evidence for audit instead of using it as the marker
            # start.
            preceding = [row for row in values if row[2] <= cue.start()]
            if preceding:
                return preceding[-1][0], None, "original_before_target"
            return following[0], None, "target_after_cue"

    if len(values) >= 2:
        first, second = values[0], values[1]
        between = str(text or "")[first[2] : second[1]]
        if _RANGE_SEPARATOR.fullmatch(between) and second[0] > first[0]:
            return first[0], second[0], "range"
    return values[0][0], None, "point"


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


def _execution_required_for_kind(kind: str, requested: bool) -> bool:
    """Never downgrade a real audio or visual edit into a marker-only row."""

    normalized = str(kind or "").strip().casefold()
    if normalized in {
        "spoken_delete",
        "speech_delete",
        "audio_delete",
        "phrase_delete",
        "range_delete",
        "ellipsis_range_delete",
        "colored_span_delete",
        "gap_delete",
        "tail_cleanup",
        "tail_particle_delete",
        "pause_delete",
        "audio_repair",
        "replace_audio",
        "pointer_overlay",
        "visual_delete",
        "visual_insert",
        "visual_overlay",
        "visual_replace",
    }:
        return True
    return requested


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
    *,
    workflow_mode: str = "full",
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
        inferred_kind = _classify_review_text(inference_text)
        kind = kind_text or inferred_kind
        # Clipboard/legacy extractors often pre-label every quoted deletion as
        # spoken_delete (or leave it as review_only/visual_delete).  Promote a
        # clearly more specific source-text semantic so stale extractor labels
        # cannot suppress ellipsis, gap, or colored-span execution.
        if inferred_kind in {
            "phrase_delete",
            "ellipsis_range_delete",
            "colored_span_delete",
            "gap_delete",
        } and kind_text.casefold() in {
            "",
            "spoken_delete",
            "speech_delete",
            "audio_delete",
            "review_only",
            "visual_delete",
        }:
            kind = inferred_kind

        # Colored text in a Feishu review note means delete the marked spoken
        # fragments. Keep the rich-text spans in the source ledger so ASR can
        # resolve one physical window per fragment; never guess that the whole
        # quoted sentence is colored when markup was not captured.
        if any(hint.casefold() in inference_text.casefold() for hint in _COLORED_NOTE_HINTS):
            kind = "colored_span_delete"
            colored_spans = _extract_colored_spans(source_row)
            row["colored_spans"] = copy.deepcopy(colored_spans)
            row_evidence = (
                source_row.get("evidence") if isinstance(source_row.get("evidence"), dict) else {}
            )
            row_evidence = copy.deepcopy(row_evidence)
            row_evidence["colored_spans"] = copy.deepcopy(colored_spans)
            row_evidence["colored_span_status"] = "resolved" if colored_spans else "missing_markup"
            row["evidence"] = row_evidence
            if not colored_spans:
                warnings.append(
                    f"Review item {explicit_id or f'index_{index + 1:03d}'} requests colored-span deletion "
                    "but rich-text markup was not captured."
                )

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
        execution_required = _execution_required_for_kind(kind, execution_required)
        execution_status = resolve_execution_status(
            source_row.get("execution_status"),
            source_row.get("evidence"),
            source_row.get("validation"),
        )
        if workflow_mode == "lite":
            if lite_pause_change_is_label_only(kind, source_text):
                if not execution_status.casefold().startswith("label_only_"):
                    execution_status = "label_only_lite_policy"
            execution_required = (
                False
                if execution_status.casefold().startswith("label_only_")
                else lite_execution_required(
                    kind,
                    source_text,
                    execution_required,
                )
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
        parsed_start, parsed_end, parsed_method = _review_text_target_range(source_text)
        timing_source = lite_timing_source(kind, source_text) if workflow_mode == "lite" else ""
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        evidence = copy.deepcopy(evidence)
        if workflow_mode == "lite":
            evidence["timing_source"] = timing_source
            if timing_source == "asr":
                evidence["review_timestamp_role"] = "search_hint"
                if parsed_start is not None:
                    evidence["review_search_hint_seconds"] = parsed_start
                    evidence["review_timestamp_parse"] = parsed_method
                # Text-extracted clocks are hints for audio issues, never final
                # edit or marker boundaries.
                if not ("start" in source_row or "end" in source_row or "rough_time" in source_row):
                    start = None
                    end = None
            else:
                evidence["review_timestamp_role"] = "authoritative_non_speech"
                if parsed_start is not None:
                    parsed_times = _review_text_times(source_text)
                    if parsed_method == "original_before_target":
                        evidence["original_time"] = parsed_start
                        if len(parsed_times) >= 2:
                            evidence["target_time"] = parsed_times[1][0]
                    if parsed_method == "original_before_target":
                        start = parsed_start
                        end = None
                    elif parsed_method == "target_after_cue" or start is None:
                        start = parsed_start
                        end = parsed_end
                    evidence["review_timestamp_parse"] = parsed_method
                    evidence["resolved_review_timestamp_seconds"] = start
        if evidence:
            row["evidence"] = evidence

        row["id"] = item_id
        row["source_text"] = source_text
        row["source"] = source
        if block_id:
            row["block_id"] = block_id
        row["kind"] = kind
        row["execution_required"] = execution_required
        if execution_status:
            row["execution_status"] = execution_status
            evidence["execution_status"] = execution_status
            row["evidence"] = evidence
        if kind in {
            "spoken_delete",
            "speech_delete",
            "audio_delete",
            "phrase_delete",
            "range_delete",
            "ellipsis_range_delete",
            "colored_span_delete",
            "gap_delete",
            "tail_cleanup",
            "tail_particle_delete",
            "pause_delete",
            "pointer_overlay",
        }:
            row["review_timestamp_role"] = "search_hint"
        if workflow_mode == "lite":
            row["review_timestamp_role"] = str(evidence.get("review_timestamp_role") or "")
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
    lite_cut_layout = str(normalized.get("lite_cut_layout") or "split_gap").strip().lower()
    if lite_cut_layout not in {"split_gap", "copy"}:
        raise ValueError("project.lite_cut_layout must be either 'split_gap' or 'copy'")
    normalized["lite_cut_layout"] = lite_cut_layout
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
            execution_status=str(item.get("execution_status") or ""),
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
        lite_cut_layout=str(project.get("lite_cut_layout") or "split_gap"),
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
    normalized_project = _normalize_project(project_copy)
    workflow_mode = str(normalized_project.get("workflow_mode") or "full")
    review_items, warnings = _canonical_review_items(
        source_rows,
        workflow_mode=workflow_mode,
    )
    review_items, timebase_warnings, unresolved_timebase_ids, replacement_anchors = (
        resolve_review_timebases(review_items, snapshot=snapshot_copy, project=normalized_project)
    )
    warnings.extend(timebase_warnings)
    acceptance = _acceptance_payload(review_items)
    provisional_request, model_items = _request_model(normalized_project, review_items, acceptance)
    provisional_profile = derive_acceptance_profile(provisional_request, doc_items=model_items)
    enabled_gates = set(provisional_profile["enabled_gates"])
    requires_segmented_audio = bool({"audio_precision", "audio_join"}.intersection(enabled_gates))
    acceptance["require_audio_validation"] = bool({"audio_precision", "audio_join"} & enabled_gates)
    acceptance["require_visual_evidence"] = workflow_mode != "lite" and "visual" in enabled_gates
    acceptance["require_pause_validation"] = "pause_fit" in enabled_gates
    acceptance["require_subject_pointer_binding"] = (
        workflow_mode != "lite" and "pointer" in enabled_gates
    )
    acceptance["require_pointer_lifecycle_evidence"] = (
        workflow_mode != "lite" and "pointer" in enabled_gates
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
        "timebase_schema": {
            "version": 1,
            "clock_policy": "main_global_or_replacement_local_with_explicit_mapping",
            "unresolved_item_policy": "review_only_until_resolved",
        },
        "replacement_anchors": replacement_anchors,
        "unresolved_timebase_item_ids": unresolved_timebase_ids,
        "review_items": review_items,
    }
    revision_request_payload = {
        "schema_version": _SCHEMA_VERSION,
        "workflow_mode": workflow_mode,
        "lite_cut_layout": str(normalized_project.get("lite_cut_layout") or "split_gap"),
        "document": document,
        "project": normalized_project,
        "edits": [],
        "markers": [],
        "review_items": review_items,
        "acceptance": acceptance,
        "acceptance_profile": acceptance_profile,
        "timebase": {
            "schema_version": 1,
            "replacement_anchors": replacement_anchors,
            "unresolved_item_ids": unresolved_timebase_ids,
            "warnings": timebase_warnings,
        },
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
        "unresolved_timebase_item_ids": unresolved_timebase_ids,
        "replacement_anchors": replacement_anchors,
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
