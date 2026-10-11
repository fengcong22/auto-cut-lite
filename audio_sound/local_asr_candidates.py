"""Retrieve diagnostic local-ASR text candidates without authorizing cuts.

The caller must verify the transcript artifact and all of its file bindings.
This pure function does not open files, run ASR, infer missing word times, or
prove that a recognized word was actually spoken. Native timing validation is
structural only, including when every check succeeds.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from audio_sound.local_asr_boundaries import validate_local_asr_boundaries

CANDIDATE_SCHEMA_VERSION = "local-asr-candidates-v1"
_TRADITIONAL = str.maketrans("運規則陣驟結數學時經歷", "运规则阵骤结数学时经历")
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
