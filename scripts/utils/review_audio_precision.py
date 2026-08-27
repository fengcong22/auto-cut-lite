"""ASR-bound, source-aligned audio planning for the Lite review workflow."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
import wave
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from utils.revision_models import (
    lite_pause_change_is_label_only,
    lite_timing_source,
    resolve_execution_status,
)

from audio_sound.volc_asr import (
    PROCESSING_STATUS_CODES,
    SUCCESS_STATUS,
    VOLC_ASR_ADAPTER_VERSION,
    VolcAsrConfig,
    VolcAsrError,
    find_phrase_matches,
    normalize_result,
    query_result,
    submit_audio,
)

ALIGNMENT_RECIPE_VERSION = "lite-alignment-pcm16-v1"
CUT_PLANNER_VERSION = "lite-asr-cut-planner-v1"
CANDIDATE_RENDERER_VERSION = "lite-source-aligned-silence-v1"
REVERSE_REPORT_VERSION = "lite-reverse-report-v1"
ASR_REQUEST_OPTIONS = {
    "enable_ddc": True,
    "enable_itn": True,
    "enable_punc": True,
    "model_name": "bigmodel",
}
ALIGNMENT_SAMPLE_RATE = 16_000
ALIGNMENT_CHANNELS = 1
_SCHEMA_VERSION = 1
_AUDIO_KINDS = {
    "audio_delete",
    "colored_span_delete",
    "ellipsis_range_delete",
    "gap_delete",
    "phrase_delete",
    "range_delete",
    "speech_delete",
    "speech_tail_cleanup",
    "spoken_delete",
    "tail_cleanup",
    "tail_particle_delete",
}
_ELLIPSIS = re.compile(r"(?:\.{2,}|…+|\.\s*\.\s*\.)")
_TIMECODE_PREFIX = re.compile(
    r"^\s*(?:\d{1,2}\s*[:：]\s*\d{1,2}(?:\.\d+)?\s*"
    r"(?:[-–—~至]\s*\d{1,2}\s*[:：]\s*\d{1,2}(?:\.\d+)?)?\s*)+"
)
_DELETE_PREFIX = re.compile(
    r"^\s*(?:请|需要|把|将|这里|此处|这一段|这段|这句|这个)?\s*"
    r"(?:删除|删掉|删去|去掉|移除|剪掉|cut|delete|remove)\s*[:：,，]?\s*",
    re.IGNORECASE,
)
_QUOTE_PATTERN = re.compile(r"[“「『\"](?P<value>.+?)[”」』\"]")
_PUNCTUATION_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)
_FORBIDDEN_JOIN_PATTERNS = (
    "阶段性成就",
    "发发明",
    "禅让制于",
    "它说明",
    "夏朝立的",
)


def canonical_json_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> str:
    destination = Path(path).expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def atomic_copy_file(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> str:
    source_path = Path(source).expanduser().resolve(strict=True)
    target = Path(destination).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source_path, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256_file(target)


def ffmpeg_identity(ffmpeg_bin: str) -> dict[str, Any]:
    resolved = shutil.which(ffmpeg_bin) or ffmpeg_bin
    path = Path(resolved).expanduser().resolve(strict=False)
    try:
        completed = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Unable to inspect FFmpeg: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError("FFmpeg identity probe failed")
    first_line = (completed.stdout or completed.stderr or "").splitlines()
    binary_sha256 = sha256_file(path) if path.is_file() else ""
    return {
        "path": str(path),
        "version": first_line[0].strip() if first_line else "unknown",
        "sha256": binary_sha256,
    }


def extract_alignment_wav(
    source_media: str | os.PathLike[str],
    output_wav: str | os.PathLike[str],
    *,
    ffmpeg_bin: str,
) -> dict[str, Any]:
    source = Path(source_media).expanduser().resolve(strict=True)
    output = Path(output_wav).expanduser().resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        str(ALIGNMENT_CHANNELS),
        "-ar",
        str(ALIGNMENT_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(output),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 44:
        detail = (completed.stderr or completed.stdout or "FFmpeg extraction failed").strip()
        raise RuntimeError(f"Unable to prepare ASR alignment audio: {detail[-1000:]}")
    with wave.open(str(output), "rb") as candidate:
        if (
            candidate.getnchannels() != ALIGNMENT_CHANNELS
            or candidate.getframerate() != ALIGNMENT_SAMPLE_RATE
            or candidate.getsampwidth() != 2
            or candidate.getcomptype() != "NONE"
        ):
            raise RuntimeError("Alignment WAV does not match the fixed mono PCM16 recipe")
        duration = candidate.getnframes() / float(candidate.getframerate())
    return {
        "schema_version": _SCHEMA_VERSION,
        "path": str(output),
        "sha256": sha256_file(output),
        "source_path": str(source),
        "source_sha256": sha256_file(source),
        "duration_seconds": round(duration, 6),
        "recipe": {
            "version": ALIGNMENT_RECIPE_VERSION,
            "audio_stream": "0:a:0",
            "channels": ALIGNMENT_CHANNELS,
            "sample_rate": ALIGNMENT_SAMPLE_RATE,
            "sample_format": "pcm_s16le",
        },
    }


def wav_duration_seconds(path: str | os.PathLike[str]) -> float:
    with wave.open(str(path), "rb") as source:
        return source.getnframes() / float(source.getframerate())


def _normalize_windows(
    windows: Sequence[Mapping[str, Any]], duration_seconds: float
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in windows:
        start = max(0.0, min(float(row["start"]), duration_seconds))
        end = max(0.0, min(float(row["end"]), duration_seconds))
        if end <= start:
            continue
        result.append({**dict(row), "start": start, "end": end})
    result.sort(key=lambda item: (item["start"], item["end"], str(item.get("item_id") or "")))
    for previous, current in zip(result, result[1:]):
        if current["start"] < previous["end"] - 1e-6:
            raise ValueError(
                "Executable Lite delete windows overlap and require manual resolution: "
                f"{previous.get('item_id')} and {current.get('item_id')}"
            )
    return result


def render_source_aligned_candidate(
    source_wav: str | os.PathLike[str],
    output_wav: str | os.PathLike[str],
    *,
    delete_windows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source_path = Path(source_wav).expanduser().resolve(strict=True)
    output_path = Path(output_wav).expanduser().resolve(strict=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with wave.open(str(source_path), "rb") as source:
            params = source.getparams()
            if (
                params.nchannels != ALIGNMENT_CHANNELS
                or params.framerate != ALIGNMENT_SAMPLE_RATE
                or params.sampwidth != 2
                or params.comptype != "NONE"
            ):
                raise ValueError("Candidate renderer requires the fixed mono PCM16 alignment WAV")
            duration = params.nframes / float(params.framerate)
            windows = _normalize_windows(delete_windows, duration)
            frame_windows = [
                (
                    max(0, min(params.nframes, round(row["start"] * params.framerate))),
                    max(0, min(params.nframes, round(row["end"] * params.framerate))),
                )
                for row in windows
            ]
            with wave.open(str(temporary), "wb") as target:
                target.setparams(params._replace(nframes=0))
                cursor = 0
                window_index = 0
                while cursor < params.nframes:
                    frame_count = min(8192, params.nframes - cursor)
                    payload = bytearray(source.readframes(frame_count))
                    chunk_end = cursor + frame_count
                    while (
                        window_index < len(frame_windows)
                        and frame_windows[window_index][1] <= cursor
                    ):
                        window_index += 1
                    scan_index = window_index
                    while scan_index < len(frame_windows):
                        start_frame, end_frame = frame_windows[scan_index]
                        if start_frame >= chunk_end:
                            break
                        zero_start = max(cursor, start_frame) - cursor
                        zero_end = min(chunk_end, end_frame) - cursor
                        if zero_end > zero_start:
                            payload[zero_start * 2 : zero_end * 2] = b"\0" * (
                                (zero_end - zero_start) * 2
                            )
                        scan_index += 1
                    target.writeframesraw(payload)
                    cursor = chunk_end
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "schema_version": _SCHEMA_VERSION,
        "path": str(output_path),
        "sha256": sha256_file(output_path),
        "source_sha256": sha256_file(source_path),
        "duration_seconds": round(wav_duration_seconds(output_path), 6),
        "delete_windows": [
            {
                "item_id": str(row.get("item_id") or ""),
                "start": round(float(row["start"]), 6),
                "end": round(float(row["end"]), 6),
            }
            for row in windows
        ],
        "renderer_version": CANDIDATE_RENDERER_VERSION,
    }


def _normalized_text(value: Any) -> str:
    return "".join(character.casefold() for character in str(value or "") if character.isalnum())


def _phrase_supported(transcript: str, phrase: str) -> bool:
    target = _normalized_text(phrase)
    if not target:
        return False
    minimum = len(target) if len(target) <= 2 else max(2, len(target) - 2)
    for leading in range(3):
        for trailing in range(3):
            end = len(target) - trailing if trailing else None
            core = target[leading:end]
            if len(core) >= minimum and core in transcript:
                return True
    return False


def _rough_window(item: Mapping[str, Any]) -> tuple[float | None, float | None]:
    start = item.get("start")
    end = item.get("end")
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    if start is None:
        start = evidence.get("review_search_hint_seconds")
    try:
        start_value = float(start) if start is not None else None
        end_value = float(end) if end is not None else None
    except (TypeError, ValueError):
        return None, None
    if start_value is None:
        return None, None
    if end_value is None or end_value <= start_value:
        return max(0.0, start_value - 3.0), start_value + 3.0
    return max(0.0, start_value), end_value


def _review_timestamp(item: Mapping[str, Any]) -> float | None:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    text_match = re.match(
        r"^\s*(?P<minutes>\d{1,3})\s*[:：]\s*(?P<seconds>\d{1,2}(?:\.\d+)?)",
        str(item.get("source_text") or ""),
    )
    text_time = (
        float(text_match.group("minutes")) * 60.0 + float(text_match.group("seconds"))
        if text_match is not None
        else None
    )
    for candidate in (
        evidence.get("review_search_hint_seconds"),
        evidence.get("resolved_review_timestamp_seconds"),
        text_time,
        item.get("start"),
    ):
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0:
            return value
    return None


def _extract_delete_phrase(item: Mapping[str, Any]) -> str:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    for field in ("delete", "delete_phrase", "spoken_text", "target_phrase"):
        candidate = str(evidence.get(field) or item.get(field) or "").strip()
        if candidate:
            return candidate
    text = str(item.get("source_text") or "")
    quotes = [match.group("value").strip() for match in _QUOTE_PATTERN.finditer(text)]
    if quotes:
        return "...".join(value for value in quotes if value)
    text = _TIMECODE_PREFIX.sub("", text)
    text = _DELETE_PREFIX.sub("", text)
    text = text.strip(" \t\r\n:：,，。；;!?！？'\"“”「」『』（）()")
    return "" if not text or _PUNCTUATION_ONLY.fullmatch(text) else text


def _match_distance(match: Mapping[str, Any], anchor: float | None) -> float:
    if anchor is None:
        return 0.0
    center = (float(match["start"]) + float(match["end"])) / 2.0
    return abs(center - anchor)


def _unique_nearest_match(
    matches: Sequence[Mapping[str, Any]], anchor: float | None
) -> tuple[dict[str, Any] | None, str]:
    if not matches:
        return None, "phrase_not_found"
    ordered = sorted((dict(row) for row in matches), key=lambda row: _match_distance(row, anchor))
    if len(ordered) == 1 or anchor is None:
        return ordered[0] if len(ordered) == 1 else None, (
            "matched" if len(ordered) == 1 else "ambiguous_without_anchor"
        )
    first_distance = _match_distance(ordered[0], anchor)
    second_distance = _match_distance(ordered[1], anchor)
    if second_distance - first_distance < 0.35:
        return None, "ambiguous_near_anchor"
    return ordered[0], "matched_nearest_anchor"


def _nearest_word(words: Sequence[Mapping[str, Any]], anchor: float | None) -> dict[str, Any] | None:
    candidates = [
        dict(word)
        for word in words
        if str(word.get("text") or "").strip()
        and isinstance(word.get("start"), (int, float))
        and isinstance(word.get("end"), (int, float))
    ]
    if not candidates:
        return None
    if anchor is None:
        return None
    return min(
        candidates,
        key=lambda word: abs((float(word["start"]) + float(word["end"])) / 2.0 - anchor),
    )


def _context_phrases(
    words: Sequence[Mapping[str, Any]], start_index: int, end_index: int
) -> list[str]:
    phrases: list[str] = []
    for selected in (
        words[max(0, start_index - 2) : start_index],
        words[end_index + 1 : end_index + 3],
    ):
        phrase = "".join(str(word.get("text") or "") for word in selected).strip()
        if _normalized_text(phrase):
            phrases.append(phrase)
    return phrases


def _asr_identity(source_asr: Mapping[str, Any]) -> dict[str, str]:
    return {
        "provider": str(source_asr.get("provider") or ""),
        "resource_id": str(source_asr.get("resource_id") or ""),
        "adapter_version": str(source_asr.get("adapter_version") or ""),
    }


def _alignment_receipt(
    source_asr: Mapping[str, Any],
    *,
    matches: Sequence[Mapping[str, Any]],
    resolved_window: Sequence[float] | None = None,
    resolved_time: float | None = None,
    authoritative_cut_boundary: bool,
) -> dict[str, Any]:
    identity = _asr_identity(source_asr)
    receipt: dict[str, Any] = {
        "status": "pass",
        "granularity": "word",
        **identity,
        "input_sha256": str(source_asr.get("input_sha256") or ""),
        "authoritative_timing": True,
        "authoritative_cut_boundary": authoritative_cut_boundary,
        "matches": [
            {
                "text": str(row.get("text") or ""),
                "start": round(float(row["start"]), 6),
                "end": round(float(row["end"]), 6),
            }
            for row in matches
        ],
    }
    if resolved_window is not None:
        receipt["resolved_cut_window"] = [
            round(float(resolved_window[0]), 6),
            round(float(resolved_window[1]), 6),
        ]
    if resolved_time is not None:
        receipt["resolved_time"] = round(float(resolved_time), 6)
    return receipt


def resolve_lite_audio_items(
    review_items: Sequence[Mapping[str, Any]],
    source_asr: Mapping[str, Any],
    *,
    source_duration_seconds: float,
) -> dict[str, Any]:
    """Resolve executable speech cuts and fail closed to ASR-positioned labels."""

    words = source_asr.get("words")
    if not isinstance(words, list) or not words:
        raise ValueError("Source ASR must contain word-level timing rows")
    rows: list[dict[str, Any]] = []
    for index, raw_item in enumerate(review_items):
        item = dict(raw_item)
        item_id = str(item.get("id") or item.get("item_id") or f"item_{index + 1:03d}")
        kind = str(item.get("kind") or "review_only").strip().casefold()
        source_text = str(item.get("source_text") or "")
        if lite_timing_source(kind, source_text) != "asr":
            continue
        rough_start, rough_end = _rough_window(item)
        anchor = (
            (rough_start + rough_end) / 2.0
            if rough_start is not None and rough_end is not None
            else rough_start
        )
        delete_phrase = _extract_delete_phrase(item)
        pause_label_only = lite_pause_change_is_label_only(kind, source_text)
        routed_status = resolve_execution_status(
            item.get("execution_status"),
            item.get("evidence"),
            item.get("validation"),
        )
        routed_label_only = routed_status.casefold().startswith("label_only_")
        requested_execute = (
            bool(item.get("execution_required")) and not pause_label_only and not routed_label_only
        )
        executable_kind = kind in _AUDIO_KINDS
        selected: dict[str, Any] | None = None
        match_method = ""

        if delete_phrase and executable_kind:
            anchor_parts = [part.strip() for part in _ELLIPSIS.split(delete_phrase) if part.strip()]
            if len(anchor_parts) >= 2:
                first_matches = find_phrase_matches(
                    words,
                    anchor_parts[0],
                    anchor_start=rough_start,
                    anchor_end=rough_end,
                )
                last_matches = find_phrase_matches(
                    words,
                    anchor_parts[-1],
                    anchor_start=rough_start,
                    anchor_end=rough_end,
                )
                ranges: list[dict[str, Any]] = []
                for first in first_matches:
                    for last in last_matches:
                        if float(last["end"]) < float(first["start"]):
                            continue
                        ranges.append(
                            {
                                "text": f"{first['text']}...{last['text']}",
                                "start": float(first["start"]),
                                "end": float(last["end"]),
                                "word_start_index": int(first["word_start_index"]),
                                "word_end_index": int(last["word_end_index"]),
                            }
                        )
                selected, match_method = _unique_nearest_match(ranges, anchor)
                if selected is not None:
                    match_method = "ellipsis_anchor_range"
            else:
                matches = find_phrase_matches(
                    words,
                    delete_phrase,
                    anchor_start=rough_start,
                    anchor_end=rough_end,
                )
                selected, match_method = _unique_nearest_match(matches, anchor)

        explicit_evidence = (
            dict(item.get("evidence") or {})
            if isinstance(item.get("evidence"), Mapping)
            else {}
        )
        explicit_must_keep = explicit_evidence.get("must_keep")
        must_keep = (
            [str(value).strip() for value in explicit_must_keep if str(value).strip()]
            if isinstance(explicit_must_keep, list)
            else []
        )
        if selected is not None and requested_execute and executable_kind:
            start = max(0.0, min(float(selected["start"]), source_duration_seconds))
            end = max(0.0, min(float(selected["end"]), source_duration_seconds))
            if end <= start:
                selected = None
                match_method = "matched_window_out_of_range"
            else:
                start_index = int(selected.get("word_start_index", 0))
                end_index = int(selected.get("word_end_index", start_index))
                matched_words = [dict(row) for row in words[start_index : end_index + 1]]
                if not must_keep:
                    must_keep = _context_phrases(words, start_index, end_index)
                rows.append(
                    {
                        "item_id": item_id,
                        "kind": kind,
                        "source_text": source_text,
                        "status": "matched",
                        "execution_required": True,
                        "execution_status": "asr_resolved",
                        "strategy": str(explicit_evidence.get("strategy") or "precision_first"),
                        "delete": delete_phrase,
                        "must_keep": must_keep,
                        "start": round(start, 6),
                        "end": round(end, 6),
                        "resolved_time": round(start, 6),
                        "match_method": match_method or "exact_phrase",
                        "matches": matched_words,
                        "asr_alignment": _alignment_receipt(
                            source_asr,
                            matches=matched_words,
                            resolved_window=(start, end),
                            resolved_time=start,
                            authoritative_cut_boundary=True,
                        ),
                    }
                )
                continue

        if selected is not None:
            start_index = int(selected.get("word_start_index", 0))
            end_index = int(selected.get("word_end_index", start_index))
            label_matches = [dict(row) for row in words[start_index : end_index + 1]]
            resolved_time = float(selected["start"])
            timing_source = "asr"
            alignment = _alignment_receipt(
                source_asr,
                matches=label_matches,
                resolved_time=resolved_time,
                authoritative_cut_boundary=False,
            )
        else:
            review_time = _review_timestamp(item)
            if review_time is None:
                raise ValueError(
                    f"Lite audio item {item_id} could not be ASR-located and has no review timestamp"
                )
            label_matches = []
            resolved_time = review_time
            timing_source = "review_timestamp_fallback"
            alignment = None
        reason = (
            "pause_duration_change_is_label_only"
            if pause_label_only
            else match_method or "no_unique_executable_phrase_match"
        )
        if routed_label_only:
            label_only_status = routed_status
        elif pause_label_only:
            label_only_status = "label_only_lite_policy"
        else:
            label_only_status = "label_only_unresolved"
        rows.append(
            {
                "item_id": item_id,
                "kind": kind,
                "source_text": source_text,
                "status": "label_only",
                "execution_required": False,
                "execution_status": label_only_status,
                "strategy": str(explicit_evidence.get("strategy") or "precision_first"),
                "delete": delete_phrase,
                "must_keep": must_keep,
                "resolved_time": round(resolved_time, 6),
                "reason": reason,
                "match_method": match_method,
                "matches": label_matches,
                "timing_source": timing_source,
                "asr_alignment": alignment,
            }
        )

    executable = [row for row in rows if row["execution_required"]]
    collisions: set[str] = set()
    for previous, current in zip(
        sorted(executable, key=lambda row: (row["start"], row["end"])),
        sorted(executable, key=lambda row: (row["start"], row["end"]))[1:],
    ):
        if float(current["start"]) < float(previous["end"]) - 1e-6:
            collisions.update((str(previous["item_id"]), str(current["item_id"])))
    if collisions:
        for row in rows:
            if str(row["item_id"]) not in collisions:
                continue
            row["status"] = "label_only"
            row["execution_required"] = False
            row["execution_status"] = "label_only_unresolved"
            row["reason"] = "overlapping_audio_items_require_manual_resolution"
            row["resolved_time"] = row.get("start", row.get("resolved_time"))
            row["asr_alignment"]["authoritative_cut_boundary"] = False

    executable = [row for row in rows if row["execution_required"]]
    return {
        "schema_version": _SCHEMA_VERSION,
        "planner_version": CUT_PLANNER_VERSION,
        "source_duration_seconds": round(float(source_duration_seconds), 6),
        "source_asr_identity": _asr_identity(source_asr),
        "source_asr_input_sha256": str(source_asr.get("input_sha256") or ""),
        "rows": rows,
        "executable_cuts": [
            {
                "item_id": row["item_id"],
                "start": row["start"],
                "end": row["end"],
            }
            for row in executable
        ],
        "unresolved_item_ids": [
            str(row["item_id"]) for row in rows if not row["execution_required"]
        ],
    }


def _complement_windows(
    cuts: Sequence[Mapping[str, Any]], duration_seconds: float
) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    cursor = 0.0
    for row in _normalize_windows(cuts, duration_seconds):
        start, end = float(row["start"]), float(row["end"])
        if start > cursor + 1e-6:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_seconds - 1e-6:
        result.append((cursor, duration_seconds))
    return result


def build_lite_split_gap_audio_plan(
    cut_plan: Mapping[str, Any],
    *,
    source_audio_path: str | os.PathLike[str],
    candidate_audio_path: str | os.PathLike[str],
) -> dict[str, Any]:
    duration = float(cut_plan["source_duration_seconds"])
    cuts = list(cut_plan.get("executable_cuts") or [])
    if not cuts:
        return {"mode": "legacy"}
    source = str(Path(source_audio_path).expanduser().resolve(strict=True))
    candidate = str(Path(candidate_audio_path).expanduser().resolve(strict=True))
    segments: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(_complement_windows(cuts, duration), start=1):
        segments.append(
            {
                "id": f"a1-kept-{index:03d}",
                "role": "source",
                "asset_path": source,
                "track_name": "Lite Source Audio",
                "source_start": round(start, 6),
                "timeline_start": round(start, 6),
                "duration": round(end - start, 6),
                "volume": 1.0,
                "fade_in": 0.0,
                "fade_out": 0.0,
                "reason": "ASR-proved kept source interval",
            }
        )
    for index, row in enumerate(_normalize_windows(cuts, duration), start=1):
        segments.append(
            {
                "id": f"a2-delete-{index:03d}",
                "role": "reference",
                "asset_path": source,
                "track_name": "Lite Reused Audio",
                "source_start": round(float(row["start"]), 6),
                "timeline_start": round(float(row["start"]), 6),
                "duration": round(float(row["end"]) - float(row["start"]), 6),
                "volume": 0.0,
                "fade_in": 0.0,
                "fade_out": 0.0,
                "doc_item_id": str(row.get("item_id") or ""),
                "reason": "Independent audible A2 review clip restored by Lite writer",
            }
        )
    return {
        "mode": "segmented",
        "pending": False,
        "forbid_full_length_segments": True,
        "max_single_segment_ratio": 1.0,
        "validation_only_audio_paths": [candidate],
        "segments": segments,
    }


def _row_by_id(cut_plan: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("item_id") or "").casefold(): dict(row)
        for row in cut_plan.get("rows") or []
        if isinstance(row, Mapping) and str(row.get("item_id") or "")
    }


def apply_audio_plan_to_compiled_payloads(
    revision_request: Mapping[str, Any],
    doc_items: Mapping[str, Any],
    cut_plan: Mapping[str, Any],
    *,
    audio_delivery_plan: Mapping[str, Any],
    source_audio_path: str | os.PathLike[str],
    candidate_audio_path: str | os.PathLike[str] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = deepcopy(dict(revision_request))
    ledger = deepcopy(dict(doc_items))
    rows = _row_by_id(cut_plan)
    source_audio = str(Path(source_audio_path).expanduser().resolve(strict=True))
    request.setdefault("project", {})["source_audio"] = source_audio
    request["project"]["media_duration_seconds"] = float(
        cut_plan["source_duration_seconds"]
    )
    request["project"]["replacement_audio"] = ""
    request["audio_delivery_plan"] = deepcopy(dict(audio_delivery_plan))
    request.setdefault("preserve", {})["replacement_audio_material"] = False

    def update_items(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or item.get("item_id") or "").casefold()
            row = rows.get(item_id)
            if row is None:
                # This pass owns only items selected by the ASR timing router.
                # Visual and other maintained Lite executors run in later phases.
                continue
            evidence = dict(item.get("evidence") or {})
            evidence.update(
                {
                    "strategy": row["strategy"],
                    "delete": row["delete"],
                    "must_keep": list(row.get("must_keep") or []),
                    "match_method": row.get("match_method"),
                    "resolved_time": row.get("resolved_time"),
                }
            )
            if row.get("timing_source") == "review_timestamp_fallback":
                evidence["timing_source"] = "review_timestamp_fallback"
                evidence["review_timestamp_role"] = "authoritative_fallback"
                evidence.pop("asr_alignment", None)
            else:
                evidence["review_timestamp_role"] = "search_hint"
                evidence["asr_alignment"] = deepcopy(row["asr_alignment"])
            if row["execution_required"]:
                evidence["cut_windows"] = [[row["start"], row["end"]]]
                evidence["resolved_cut_window"] = [row["start"], row["end"]]
            else:
                evidence["execution_status"] = row["execution_status"]
                evidence["reason"] = row.get("reason")
            item["start"] = row["resolved_time"]
            item["end"] = round(float(row["resolved_time"]) + 0.8, 6)
            item["execution_required"] = bool(row["execution_required"])
            item["execution_status"] = row["execution_status"]
            item["evidence"] = evidence

    update_items(request.get("review_items"))
    update_items(ledger.get("review_items"))

    asr_item_ids = set(rows)
    request["edits"] = [
        edit
        for edit in request.get("edits") or []
        if str(edit.get("doc_item_id") or "").strip().casefold() not in asr_item_ids
    ]
    request["pause_adjustments"] = [
        pause
        for pause in request.get("pause_adjustments") or []
        if str(pause.get("item_id") or "").strip().casefold() not in asr_item_ids
    ]
    for row in cut_plan.get("rows") or []:
        if not isinstance(row, Mapping) or not row.get("execution_required"):
            continue
        evidence = {
            "review_timestamp_role": "search_hint",
            "strategy": row["strategy"],
            "delete": row["delete"],
            "must_keep": list(row.get("must_keep") or []),
            "asr_alignment": deepcopy(row["asr_alignment"]),
            "source_cut_windows": [[row["start"], row["end"]]],
            "resolved_cut_window": [row["start"], row["end"]],
            "boundary_refinement": {
                "status": "asr_character_edge",
                "resolved_cut_window": [row["start"], row["end"]],
                "crossed_must_keep": False,
            },
        }
        request["edits"].append(
            {
                "type": "delete",
                "start": row["start"],
                "end": row["end"],
                "label": row["source_text"],
                "detail": row["delete"],
                "doc_item_id": row["item_id"],
                "source_kind": row["kind"],
                "evidence": evidence,
            }
        )
    request["edits"].sort(
        key=lambda edit: (float(edit.get("start") or 0.0), float(edit.get("end") or 0.0))
    )

    if candidate_audio_path is not None:
        candidate = str(Path(candidate_audio_path).expanduser().resolve(strict=True))
        request["processed_audio"] = {
            "output_wav": candidate,
            "candidate_audio_sha256": sha256_file(candidate),
            "status": "pending_reverse_asr",
        }
    else:
        request.pop("processed_audio", None)
    return request, ledger


def _local_reverse_words(
    words: Sequence[Mapping[str, Any]], start: float, end: float, *, context: float = 4.0
) -> list[dict[str, Any]]:
    window_start = max(0.0, start - context)
    window_end = end + context
    return [
        dict(word)
        for word in words
        if float(word.get("end", -1.0)) >= window_start
        and float(word.get("start", math.inf)) <= window_end
    ]


def _candidate_join_times_from_payload(
    revision_request: Mapping[str, Any], source_window: Sequence[float]
) -> list[float]:
    """Map one source cut window to the segmented candidate timebase.

    This mirrors the strict acceptance mapping in ``revision_validation``.  A
    source-aligned Lite candidate can expose two different boundary times
    around a silent window, while a compressed candidate normally maps both
    boundaries to the same join time.
    """

    delivery = revision_request.get("audio_delivery_plan")
    if not isinstance(delivery, Mapping) or str(delivery.get("mode") or "") != "segmented":
        return []
    raw_segments = delivery.get("segments")
    if not isinstance(raw_segments, list):
        return []

    audible: list[dict[str, Any]] = []
    for raw in raw_segments:
        if not isinstance(raw, Mapping):
            continue
        try:
            volume = float(raw.get("volume", 1.0))
            source_start = float(raw.get("source_start", 0.0))
            timeline_start = float(raw.get("timeline_start", 0.0))
            duration = float(raw.get("duration", 0.0))
            fade_in = float(raw.get("fade_in", 0.0))
            fade_out = float(raw.get("fade_out", 0.0))
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            str(raw.get("role") or "").strip().casefold() == "reference"
            or volume <= 1e-6
            or duration <= 0.0
            or not all(
                math.isfinite(value)
                for value in (
                    source_start,
                    timeline_start,
                    duration,
                    fade_in,
                    fade_out,
                )
            )
        ):
            continue
        audible.append(
            {
                "id": str(raw.get("id") or raw.get("segment_id") or ""),
                "source_start": source_start,
                "source_end": source_start + duration,
                "timeline_start": timeline_start,
                "duration": duration,
                "fade_in": max(0.0, fade_in),
                "fade_out": max(0.0, fade_out),
            }
        )
    audible.sort(key=lambda row: (row["timeline_start"], row["id"]))

    boundaries: list[dict[str, float]] = []
    overlap_before = 0.0
    previous: dict[str, Any] | None = None
    for segment in audible:
        if previous is not None:
            previous_end = previous["timeline_start"] + previous["duration"]
            if abs(segment["timeline_start"] - previous_end) <= 1e-3:
                overlap_before += min(previous["fade_out"], segment["fade_in"])
        candidate_start = segment["timeline_start"] - overlap_before
        boundaries.append(
            {
                "source_start": segment["source_start"],
                "source_end": segment["source_end"],
                "candidate_start": candidate_start,
                "candidate_end": candidate_start + segment["duration"],
            }
        )
        previous = segment

    start, end = float(source_window[0]), float(source_window[1])
    candidates: list[float] = []
    for segment in boundaries:
        if abs(segment["source_end"] - start) <= 1e-3:
            candidates.append(segment["candidate_end"])
        if abs(segment["source_start"] - end) <= 1e-3:
            candidates.append(segment["candidate_start"])
    return list(dict.fromkeys(round(candidate, 9) for candidate in candidates))


def _phrase_hits(words: Sequence[Mapping[str, Any]], phrase: str) -> list[dict[str, Any]]:
    return [
        {
            "phrase": phrase,
            "text": str(row.get("text") or phrase),
            "start": round(float(row["start"]), 6),
            "end": round(float(row["end"]), 6),
        }
        for row in find_phrase_matches(words, phrase)
    ]


def build_full_candidate_reverse_report(
    revision_request: Mapping[str, Any],
    cut_plan: Mapping[str, Any],
    candidate_asr: Mapping[str, Any],
    *,
    candidate_audio_path: str | os.PathLike[str],
    audio_delivery_plan_sha256: str,
) -> dict[str, Any]:
    candidate = Path(candidate_audio_path).expanduser().resolve(strict=True)
    words = candidate_asr.get("words")
    if not isinstance(words, list) or not words:
        raise ValueError("Candidate reverse ASR must contain word timing rows")
    report_rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for row in cut_plan.get("rows") or []:
        if not isinstance(row, Mapping) or not row.get("execution_required"):
            continue
        start, end = float(row["start"]), float(row["end"])
        join_candidates = _candidate_join_times_from_payload(
            revision_request,
            (start, end),
        )
        if not join_candidates:
            raise ValueError(
                "Segmented audio plan cannot map the reverse-ASR join for "
                f"{row.get('item_id') or '<unknown>'}"
            )
        mapped_join_time = min(join_candidates)
        local_words = _local_reverse_words(words, start, end)
        local_text = "".join(str(word.get("text") or "") for word in local_words)
        normalized = _normalized_text(local_text)
        delete_hits = _phrase_hits(local_words, str(row["delete"]))
        keep_hits = {
            phrase: _phrase_supported(normalized, phrase)
            for phrase in row.get("must_keep") or []
        }
        forbidden = [
            phrase for phrase in _FORBIDDEN_JOIN_PATTERNS if _normalized_text(phrase) in normalized
        ]
        status = (
            "pass"
            if not delete_hits and all(keep_hits.values()) and not forbidden
            else "review"
        )
        if status != "pass":
            unresolved.append(str(row["item_id"]))
        report_rows.append(
            {
                "id": row["item_id"],
                "doc_item_id": row["item_id"],
                "kind": row["kind"],
                "status": status,
                "strategy": row["strategy"],
                "delete": row["delete"],
                "must_keep": list(row.get("must_keep") or []),
                "source_cut_windows": [[row["start"], row["end"]]],
                "mapped_join_times": [mapped_join_time],
                "local_joined_text": local_text,
                "delete_hits": delete_hits,
                "keep_hits": keep_hits,
                "semantic_join_validation": {
                    "status": "pass" if not forbidden and all(keep_hits.values()) else "review",
                    "method": REVERSE_REPORT_VERSION,
                    "forbidden_patterns": forbidden,
                },
                "candidate_audio_sha256": sha256_file(candidate),
                "asr_identity": {
                    "provider": str(candidate_asr.get("provider") or ""),
                    "model": str(candidate_asr.get("resource_id") or ""),
                    "adapter_version": str(candidate_asr.get("adapter_version") or ""),
                },
            }
        )
    return {
        "schema_version": _SCHEMA_VERSION,
        "report_builder_version": REVERSE_REPORT_VERSION,
        "candidate_audio_path": str(candidate),
        "candidate_audio_sha256": sha256_file(candidate),
        "candidate_audio_duration_seconds": round(wav_duration_seconds(candidate), 6),
        "audio_delivery_plan_sha256": audio_delivery_plan_sha256,
        "asr_identity": {
            "provider": str(candidate_asr.get("provider") or ""),
            "model": str(candidate_asr.get("resource_id") or ""),
            "adapter_version": str(candidate_asr.get("adapter_version") or ""),
        },
        "service_job_id": str(candidate_asr.get("service_job_id") or ""),
        "service_result_sha256": str(candidate_asr.get("service_result_sha256") or ""),
        "status_counts": {
            "pass": sum(row["status"] == "pass" for row in report_rows),
            "review": sum(row["status"] != "pass" for row in report_rows),
        },
        "unresolved_ids": unresolved,
        "semantic_join_anomalies": [],
        "rows": report_rows,
    }


def apply_reverse_report_to_payloads(
    revision_request: Mapping[str, Any],
    doc_items: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    report_path: str | os.PathLike[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    unresolved = [str(value) for value in report.get("unresolved_ids") or []]
    if unresolved:
        raise ValueError(
            "Full-candidate reverse ASR did not pass for: " + ", ".join(unresolved)
        )
    request = deepcopy(dict(revision_request))
    ledger = deepcopy(dict(doc_items))
    rows = {
        str(row.get("id") or row.get("item_id") or "").casefold(): dict(row)
        for row in report.get("rows") or []
        if isinstance(row, Mapping)
    }

    def update(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            row = rows.get(str(item.get("id") or item.get("item_id") or "").casefold())
            if row is None:
                continue
            evidence = dict(item.get("evidence") or {})
            evidence["validation"] = {"status": row["status"], "reverse_row": row}
            item["evidence"] = evidence
            item["validation"] = {"status": row["status"]}

    update(request.get("review_items"))
    update(ledger.get("review_items"))
    processed = dict(request.get("processed_audio") or {})
    processed.update(
        {
            "candidate_audio_sha256": report["candidate_audio_sha256"],
            "validation_summary": str(Path(report_path).expanduser().resolve(strict=False)),
            "audio_delivery_plan_sha256": report["audio_delivery_plan_sha256"],
            "status": "pass",
        }
    )
    request["processed_audio"] = processed
    return request, ledger


def asr_service_identity(config: VolcAsrConfig) -> dict[str, Any]:
    return {
        "provider": "volc_asr",
        "resource_id": config.resource_id,
        "adapter_version": VOLC_ASR_ADAPTER_VERSION,
        "submit_url": config.submit_url,
        "query_url": config.query_url,
        "request_options": ASR_REQUEST_OPTIONS,
    }


def alignment_cache_identity(
    *, source_sha256: str, ffmpeg: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "inputs": {
            "source_media_sha256": source_sha256,
            "recipe": {
                "audio_stream": "0:a:0",
                "channels": ALIGNMENT_CHANNELS,
                "sample_rate": ALIGNMENT_SAMPLE_RATE,
                "sample_format": "pcm_s16le",
            },
        },
        "versions": {
            "extractor": ALIGNMENT_RECIPE_VERSION,
            "ffmpeg": dict(ffmpeg),
        },
    }


def source_asr_cache_identity(
    *, alignment_audio_sha256: str, config: VolcAsrConfig
) -> dict[str, Any]:
    return {
        "inputs": {
            "alignment_audio_sha256": alignment_audio_sha256,
            "preprocessing": {
                "version": ALIGNMENT_RECIPE_VERSION,
                "channels": ALIGNMENT_CHANNELS,
                "sample_rate": ALIGNMENT_SAMPLE_RATE,
                "sample_format": "pcm_s16le",
            },
            "service": asr_service_identity(config),
        },
        "versions": {"adapter": VOLC_ASR_ADAPTER_VERSION},
    }


def candidate_cache_identity(
    *, alignment_audio_sha256: str, executable_cuts: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    normalized_cuts = [
        {
            "item_id": str(row.get("item_id") or ""),
            "start": round(float(row["start"]), 6),
            "end": round(float(row["end"]), 6),
        }
        for row in executable_cuts
    ]
    return {
        "inputs": {
            "alignment_audio_sha256": alignment_audio_sha256,
            "delete_plan_sha256": canonical_json_sha256(normalized_cuts),
        },
        "versions": {
            "renderer": CANDIDATE_RENDERER_VERSION,
            "sample_rate": ALIGNMENT_SAMPLE_RATE,
            "channels": ALIGNMENT_CHANNELS,
            "sample_format": "pcm_s16le",
        },
    }


def reverse_asr_cache_identity(
    *,
    candidate_audio_sha256: str,
    cut_plan_sha256: str,
    config: VolcAsrConfig,
) -> dict[str, Any]:
    return {
        "inputs": {
            "candidate_audio_sha256": candidate_audio_sha256,
            "cut_plan_sha256": cut_plan_sha256,
            "service": asr_service_identity(config),
        },
        "versions": {"adapter": VOLC_ASR_ADAPTER_VERSION},
    }


@contextmanager
def cache_identity_lock(
    cache_root: str | os.PathLike[str],
    namespace: str,
    digest: str,
    *,
    timeout_seconds: float = 300.0,
) -> Iterator[None]:
    root = Path(cache_root).expanduser().resolve(strict=False) / ".locks"
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f"{namespace}-{digest}.lock"
    handle = open(lock_path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        acquired = False
        while not acquired:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for cache identity lock: {namespace}")
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _read_ticket(path: Path, identity_digest: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _SCHEMA_VERSION
        or payload.get("identity_digest") != identity_digest
        or not str(payload.get("request_id") or "")
    ):
        return None
    return payload


def run_resumable_volc_asr(
    audio_path: str | os.PathLike[str],
    *,
    config: VolcAsrConfig,
    identity_digest: str,
    ticket_path: str | os.PathLike[str],
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 2.0,
    max_wait_seconds: float = 120.0,
) -> dict[str, Any]:
    """Run or resume one provider job and return normalized word-level ASR."""

    audio = Path(audio_path).expanduser().resolve(strict=True)
    ticket_file = Path(ticket_path).expanduser().resolve(strict=False)
    ticket_file.parent.mkdir(parents=True, exist_ok=True)
    ticket = _read_ticket(ticket_file, identity_digest)
    if ticket is None:
        request_id = str(uuid.uuid4())
        ticket = {
            "schema_version": _SCHEMA_VERSION,
            "identity_digest": identity_digest,
            "request_id": request_id,
            "audio_sha256": sha256_file(audio),
            "status": "submitting",
        }
        atomic_write_json(ticket_file, ticket)
        submit_audio(
            audio,
            config=config,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        ticket["status"] = "submitted"
        atomic_write_json(ticket_file, ticket)
    else:
        request_id = str(ticket["request_id"])
        if str(ticket.get("audio_sha256") or "") != sha256_file(audio):
            raise ValueError("ASR inflight ticket audio identity does not match current bytes")

    deadline = time.monotonic() + max_wait_seconds
    first_poll = True
    last_status = ""
    last_message = ""
    while time.monotonic() <= deadline:
        if not first_poll and poll_interval_seconds:
            time.sleep(poll_interval_seconds)
        first_poll = False
        response, status, message = query_result(
            config=config,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        last_status, last_message = status, message
        if status == SUCCESS_STATUS and response:
            raw = dict(response)
            raw["_auto_cut_evidence"] = {
                "input_sha256": sha256_file(audio),
                "service_job_id": request_id,
                "resource_id": config.resource_id,
                "adapter_version": VOLC_ASR_ADAPTER_VERSION,
            }
            normalized = normalize_result(raw)
            normalized["schema_version"] = _SCHEMA_VERSION
            ticket_file.unlink(missing_ok=True)
            return normalized
        if status and status not in PROCESSING_STATUS_CODES:
            raise VolcAsrError(
                f"Volc ASR query failed for resumable job: code={status} message={message}"
            )
    raise TimeoutError(
        "Timed out waiting for resumable Volc ASR job: "
        f"code={last_status} message={last_message}"
    )


__all__ = [
    "ALIGNMENT_CHANNELS",
    "ALIGNMENT_RECIPE_VERSION",
    "ALIGNMENT_SAMPLE_RATE",
    "ASR_REQUEST_OPTIONS",
    "CANDIDATE_RENDERER_VERSION",
    "CUT_PLANNER_VERSION",
    "REVERSE_REPORT_VERSION",
    "alignment_cache_identity",
    "apply_audio_plan_to_compiled_payloads",
    "apply_reverse_report_to_payloads",
    "asr_service_identity",
    "atomic_copy_file",
    "atomic_write_json",
    "build_full_candidate_reverse_report",
    "build_lite_split_gap_audio_plan",
    "cache_identity_lock",
    "candidate_cache_identity",
    "canonical_json_sha256",
    "extract_alignment_wav",
    "ffmpeg_identity",
    "render_source_aligned_candidate",
    "resolve_lite_audio_items",
    "reverse_asr_cache_identity",
    "run_resumable_volc_asr",
    "sha256_file",
    "source_asr_cache_identity",
    "wav_duration_seconds",
]
