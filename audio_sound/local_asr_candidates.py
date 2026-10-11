"""Retrieve diagnostic local-ASR text candidates without authorizing cuts.

The caller must verify the transcript artifact and all of its file bindings.
This pure function does not open files, run ASR, infer missing word times, or
prove that a recognized word was actually spoken. Native timing validation is
structural only, including when every check succeeds.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from audio_sound.local_asr_boundaries import validate_local_asr_boundaries

CANDIDATE_SCHEMA_VERSION = "local-asr-candidates-v1"
MANUAL_BOUNDARY_PROPOSAL_SCHEMA_VERSION = "manual-boundary-proposals-v1"
_TRADITIONAL = str.maketrans("運規則陣驟結數學時經歷", "运规则阵骤结数学时经历")
_SHA256_HEX = set("0123456789abcdef")
_FLAGS = {
    "production_eligible": False,
    "acoustic_accepted": False,
    "neighbor_protection_verified": False,
    "authoritative_cut_boundary": False,
    "execution_required": False,
}


def _copy(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_float": repr(value)}
    if isinstance(value, Mapping):
        return {str(key): _copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"non_json_type": type(value).__name__, "representation": repr(value)}


def _normalize(text: str) -> tuple[str, list[int]]:
    chars, offsets = [], []
    for index, character in enumerate(text):
        for normalized in character.translate(_TRADITIONAL).casefold():
            if unicodedata.category(normalized)[0] in "LNM":
                chars.append(normalized)
                offsets.append(index)
    return "".join(chars), offsets


def _latin_boundary(character: str) -> bool:
    category = unicodedata.category(character)
    return category[0] in "NM" or (
        category[0] == "L" and "LATIN" in unicodedata.name(character, "")
    )


def _occurrences(text: str, query: str) -> list[dict[str, Any]]:
    normalized, offsets = _normalize(text)
    needle, _ = _normalize(query)
    found = []
    for position in range(len(normalized) - len(needle) + 1):
        if not normalized.startswith(needle, position):
            continue
        finish = position + len(needle)
        # Never accept one half of a casefold expansion such as German sharp S.
        if position and offsets[position - 1] == offsets[position]:
            continue
        if finish < len(offsets) and offsets[finish - 1] == offsets[finish]:
            continue
        start, end = offsets[position], offsets[finish - 1] + 1
        if _latin_boundary(needle[0]) and start and _latin_boundary(text[start - 1]):
            continue
        if _latin_boundary(needle[-1]) and end < len(text) and _latin_boundary(text[end]):
            continue
        found.append(
            {
                "normalized_range": [position, finish],
                "raw_range": [start, end],
                "raw_text": text[start:end],
            }
        )
    return found


def _native_ranges(units: Any, text: str) -> tuple[list[tuple[int, int]], list[dict]]:
    if not isinstance(units, list) or not units:
        return [], [{"code": "native_units_missing_or_malformed"}]
    ranges, normalized_parts, position = [], [], 0
    for index, unit in enumerate(units):
        if not isinstance(unit, Mapping) or not isinstance(unit.get("text"), str):
            return [], [{"code": "native_text_malformed", "native_indices": [index]}]
        part, _ = _normalize(unit["text"])
        ranges.append((position, position + len(part)))
        position += len(part)
        normalized_parts.append(part)
    if "".join(normalized_parts) != _normalize(text)[0]:
        return ranges, [{"code": "native_text_does_not_match_transcript"}]
    return ranges, []


def _map_hit(hit: dict, ranges: list[tuple[int, int]], mapping_issues: list[dict]) -> dict:
    if mapping_issues:
        return {"status": "unavailable", "native_indices": [], "issues": _copy(mapping_issues)}
    start, end = hit["normalized_range"]
    touched = [index for index, (a, b) in enumerate(ranges) if a < end and start < b]
    if not touched:
        return {
            "status": "unavailable",
            "native_indices": [],
            "issues": [{"code": "native_mapping_unavailable"}],
        }
    first, last = touched[0], touched[-1]
    indices = list(range(first, last + 1))
    # A punctuation-only row between selected units remains in this range.
    if ranges[first][0] != start or ranges[last][1] != end:
        return {
            "status": "native_unit_too_large",
            "native_indices": indices,
            "issues": [{"code": "target_edges_inside_native_unit"}],
        }
    return {"status": "exact_native_span", "native_indices": indices, "issues": []}


def _structure(transcript: Mapping, indices: list[int], protected: list[dict]) -> dict:
    # The Boolean fields satisfy the existing validator's caller contract only.
    # All identities and hashes themselves must come from the supplied artifact.
    supplied = transcript.get("identity")
    identity = _copy(supplied) if isinstance(supplied, Mapping) else {}
    for component in ("input", "result", "model", "provider", "adapter"):
        if isinstance(identity.get(component), dict):
            identity[component]["verified"] = True
    identity["timestamp_kind"] = transcript.get("timestamp_kind")
    input_metadata = transcript.get("input")
    duration = input_metadata.get("duration_seconds") if isinstance(input_metadata, Mapping) else None
    report = validate_local_asr_boundaries(
        transcript.get("native_units"),
        identity=identity,
        crop_offset_seconds=0.0,
        crop_duration_seconds=duration,
        source_duration_seconds=duration,
        selected_native_indices=indices,
        protected_windows=protected,
    )
    # Do not label this in-memory adapter as a file-identity verifier.
    report.pop("identity", None)
    report.update(
        _FLAGS,
        identity_verification="assumed_caller_contract_not_verified_here",
        caller_verified_artifact_required=True,
        supplied_identity=_copy(supplied),
    )
    return report


def locate_candidates(
    transcript: Mapping, query: str, *, must_keep: Sequence[str] = ()
) -> dict:
    """Return every exact normalized occurrence and its diagnostic native span.

    No nearest-hit selection or fuzzy substitution is performed. Text matches
    survive unavailable word timestamps, while ``native_window`` stays absent.
    Missing/ambiguous protected phrases never imply neighbor protection passed.
    """
    if not isinstance(transcript, Mapping):
        raise TypeError("transcript must be a mapping")
    if not isinstance(query, str) or not _normalize(query)[0]:
        raise ValueError("query must contain letters or numbers")
    if isinstance(must_keep, (str, bytes)) or not isinstance(must_keep, Sequence):
        raise TypeError("must_keep must be a sequence of phrases")
    if any(not isinstance(phrase, str) or not _normalize(phrase)[0] for phrase in must_keep):
        raise ValueError("must_keep phrases must contain letters or numbers")
    text = transcript.get("text")
    if not isinstance(text, str):
        raise ValueError("transcript text must be a string")

    contract_issues = []
    if transcript.get("schema_version") != "local-asr-transcript-v1":
        contract_issues.append({"code": "transcript_schema_unsupported"})
    timestamps = transcript.get("timestamp_kind") == "native_unit_seconds"
    ranges, mapping_issues = _native_ranges(transcript.get("native_units"), text)
    protections, protected_windows, protection_issues = [], [], []
    for phrase in must_keep:
        hits = _occurrences(text, phrase)
        protection = {"phrase": phrase, "occurrences": hits, "count": len(hits), **_FLAGS}
        if len(hits) != 1:
            issue = {"code": "must_keep_not_found" if not hits else "must_keep_ambiguous", "phrase": phrase}
            protection.update(status="unverified", issues=[issue])
            protection_issues.append(issue)
        else:
            mapping = _map_hit(hits[0], ranges, mapping_issues)
            protection["native_mapping"] = mapping
            if timestamps and mapping["status"] == "exact_native_span":
                structure = _structure(transcript, mapping["native_indices"], [])
                protection["boundary_validation"] = structure
                if structure["structural_pass"]:
                    protected_windows.append({**structure["local_window"], "phrase": phrase})
                    protection.update(status="structurally_located_only", issues=[])
                else:
                    issue = {"code": "must_keep_structure_blocked", "phrase": phrase}
                    protection.update(status="unverified", issues=[issue])
                    protection_issues.append(issue)
            else:
                issue = {"code": "must_keep_word_boundary_unavailable", "phrase": phrase}
                protection.update(status="unverified", issues=[issue])
                protection_issues.append(issue)
        protections.append(protection)

    candidates = []
    for hit in _occurrences(text, query):
        mapping = _map_hit(hit, ranges, mapping_issues)
        candidate = {
            **hit,
            **_FLAGS,
            "native_mapping": mapping,
            "native_indices": list(mapping.get("native_indices") or []),
            "native_window": None,
            "word_boundary_status": "unavailable",
            "issues": _copy(contract_issues + protection_issues + mapping["issues"]),
            "caller_verified_artifact_required": True,
        }
        if timestamps and mapping["status"] == "exact_native_span":
            structure = _structure(transcript, mapping["native_indices"], protected_windows)
            candidate["boundary_validation"] = structure
            candidate["issues"].extend(structure["issues"])
            candidate["word_boundary_status"] = "structurally_valid_only" if not candidate["issues"] else "blocked"
            if not candidate["issues"]:
                candidate["native_window"] = structure["local_window"]
        else:
            candidate["issues"].append({"code": "word_timestamps_unavailable" if not timestamps else "word_boundary_unavailable"})
        candidate["status"] = "candidate_only_precision_unaccepted" if candidate["native_window"] is not None else "text_match_only"
        candidates.append(candidate)

    count = len(candidates)
    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "query": query,
        "normalized_query": _normalize(query)[0],
        "match_method": "exact_normalized_text",
        "normalization": "finite_traditional_map_casefold_keep_unicode_letters_numbers_marks",
        "status": "no_text_support" if count == 0 else "ambiguous" if count > 1 else "unique_text_support",
        "count": count,
        "ambiguous": count > 1,
        "candidates": candidates,
        # ``occurrences`` is an intentionally redundant spelling for callers
        # that treat text support as a search result rather than a cut plan.
        "occurrences": candidates,
        "must_keep": protections,
        "issues": _copy(contract_issues + protection_issues),
        "timestamp_kind": transcript.get("timestamp_kind"),
        "caller_verified_artifact_required": True,
        "identity_verification": "not_performed_by_candidate_retrieval",
        "word_existence_acoustically_proven": False,
        "executable_cuts": [],
        **_FLAGS,
    }


def _canonical_json_bytes(value: Any) -> bytes:
    """Return the byte representation used to bind a diagnostic artifact."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value.casefold()) - _SHA256_HEX:
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return value


def _require_frame(value: Any, field: str) -> int:
    # bool is an int subclass, but accepting it would make a malformed context
    # appear to be a valid source range.
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer frame")
    return value


def _require_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _verified_identity(transcript: Mapping) -> dict[str, Any]:
    """Validate the identity emitted by ``transcribe_local`` before binding it."""
    identity = transcript.get("identity")
    if not isinstance(identity, Mapping):
        raise ValueError("transcript identity is missing")
    required = {
        "input": ("sha256",),
        "result": ("sha256",),
        "model": ("id", "revision", "sha256"),
        "provider": ("name", "version"),
        "adapter": ("name", "version", "sha256"),
    }
    for component, fields in required.items():
        evidence = identity.get(component)
        if not isinstance(evidence, Mapping) or evidence.get("verified") is not True:
            raise ValueError(f"transcript identity {component} is not verified")
        for field in fields:
            value = evidence.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"transcript identity {component}.{field} is missing")
            if field == "sha256":
                _require_sha256(value, f"transcript identity {component}.{field}")
    input_metadata = transcript.get("input")
    if not isinstance(input_metadata, Mapping):
        raise ValueError("transcript input metadata is missing")
    input_sha = _require_sha256(input_metadata.get("sha256"), "transcript input.sha256")
    if identity["input"].get("sha256") != input_sha:
        raise ValueError("transcript identity input hash does not match input metadata")
    return _copy(identity)


def build_manual_boundary_proposal(
    transcript: Mapping,
    query: str,
    *,
    item_id: str,
    context: Mapping,
    must_keep: Sequence[str] = (),
    manifest_sha256: str | None = None,
    manifest_schema_version: str | None = None,
    manifest_bindings: Mapping | None = None,
) -> dict[str, Any]:
    """Bind one unique local-ASR hint to verified context for human audition.

    The returned range is a model suggestion only. This function never writes
    audio, never chooses an acoustic boundary, and never enables a production
    gate. The context describes an exact PCM copy already made from the source
    audio, with ``source_start_frame`` and ``source_end_frame`` on the full
    source timeline.
    """
    if not isinstance(transcript, Mapping):
        raise TypeError("transcript must be a mapping")
    if transcript.get("schema_version") != "local-asr-transcript-v1":
        raise ValueError("unsupported transcript schema")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("item_id must be a non-empty string")
    if not isinstance(context, Mapping):
        raise TypeError("context must be a mapping")
    source_audio_sha256 = _require_sha256(context.get("source_audio_sha256"), "context.source_audio_sha256")
    identity = _verified_identity(transcript)
    input_metadata = transcript["input"]
    if input_metadata.get("sha256") != source_audio_sha256:
        raise ValueError("context source audio does not match transcript input")
    sample_rate = _require_frame(context.get("sample_rate"), "context.sample_rate")
    if sample_rate <= 0:
        raise ValueError("context.sample_rate must be positive")
    context_start = _require_frame(context.get("source_start_frame"), "context.source_start_frame")
    context_end = _require_frame(context.get("source_end_frame"), "context.source_end_frame")
    if context_end <= context_start:
        raise ValueError("context source frame range must be increasing")
    input_rate = _require_frame(input_metadata.get("sample_rate"), "transcript input.sample_rate")
    if input_rate != sample_rate:
        raise ValueError("context sample rate does not match transcript input")
    input_frames = _require_frame(input_metadata.get("frame_count"), "transcript input.frame_count")
    if context_end > input_frames:
        raise ValueError("context source frame range exceeds transcript input")
    for key in ("file_sha256", "pcm16_sha256"):
        if context.get(key) is not None:
            _require_sha256(context[key], f"context.{key}")
    report = locate_candidates(transcript, query, must_keep=must_keep)
    if report["status"] != "unique_text_support" or report["count"] != 1:
        raise ValueError("manual boundary proposals require one unique text candidate")
    candidate = report["candidates"][0]
    window = candidate.get("native_window")
    if candidate.get("word_boundary_status") != "structurally_valid_only" or not isinstance(window, Mapping):
        raise ValueError("candidate has no structurally valid native window")
    if candidate.get("issues"):
        raise ValueError("candidate contains structural or protection issues")
    start_seconds = _require_number(window.get("start"), "candidate.native_window.start")
    end_seconds = _require_number(window.get("end"), "candidate.native_window.end")
    if end_seconds <= start_seconds:
        raise ValueError("candidate native window must be increasing")
    source_start = round(start_seconds * sample_rate)
    source_end = round(end_seconds * sample_rate)
    if source_end <= source_start or source_start < context_start or source_end > context_end:
        raise ValueError("candidate native window is outside the supplied context")
    for key in (
        "production_eligible", "acoustic_accepted", "neighbor_protection_verified",
        "source_media_modified", "production_integrated",
    ):
        if transcript.get(key) is not False:
            raise ValueError("transcript production gates must all be false")
    candidate_report_sha256 = _sha256_json(report)
    context_copy = _copy(context)
    candidate_identity = {
        "candidate_report_schema_version": report["schema_version"],
        "candidate_report_sha256": candidate_report_sha256,
        "transcript_schema_version": transcript["schema_version"],
        "transcript_identity": identity,
        "query": query,
        "candidate_index": 0,
        "native_indices": _copy(candidate.get("native_indices")),
        "native_window_seconds": {"start": start_seconds, "end": end_seconds},
        "candidate_raw_range": _copy(candidate.get("raw_range")),
        "candidate_raw_text": candidate.get("raw_text"),
        "basis": "unique_exact_normalized_text_structural_window_only",
        "acoustic_accepted": False,
    }
    item = {
        "id": item_id,
        "target": query,
        "context": context_copy,
        "context_file_sha256": context_copy.get("file_sha256"),
        "context_pcm_sha256": context_copy.get("pcm16_sha256"),
        "source_audio_sha256": source_audio_sha256,
        "sample_rate": sample_rate,
        "context_source_start_frame": context_start,
        "context_source_end_frame": context_end,
        "relative_start_frame": source_start - context_start,
        "relative_end_frame": source_end - context_start,
        "source_start_frame": source_start,
        "source_end_frame": source_end,
        "candidate_identity": candidate_identity,
        "candidate_report": report,
        "status": "candidate_boundary_for_manual_review",
        "user_judgment": "pending",
        "human_confirmation_in_chat": False,
        "playback_completed": {"left": False, "target": False, "right": False},
        "production_eligible": False,
        "acoustic_accepted": False,
        "neighbor_protection_verified": False,
        "source_media_modified": False,
        "production_integrated": False,
    }
    return {
        "schema_version": MANUAL_BOUNDARY_PROPOSAL_SCHEMA_VERSION,
        "evidence_kind": "manual_boundary_proposal",
        "browser_qa": False,
        "manifest_sha256": _require_sha256(manifest_sha256, "manifest_sha256") if manifest_sha256 is not None else None,
        "manifest_schema_version": manifest_schema_version,
        "manifest_bindings": _copy(manifest_bindings),
        "browser_sample_rate": sample_rate,
        "playback_method": "same_pcm_audio_buffer_sample_ranges",
        "qa_muted": False,
        "human_confirmation_in_chat": False,
        "production_eligible": False,
        "acoustic_accepted": False,
        "neighbor_protection_verified": False,
        "source_media_modified": False,
        "production_integrated": False,
        "items": [item],
    }
