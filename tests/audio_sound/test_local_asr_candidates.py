from __future__ import annotations

import json
import math
from copy import deepcopy

import pytest

from audio_sound.local_asr_candidates import locate_candidates


def _transcript() -> dict:
    return {
        "schema_version": "local-asr-transcript-v1",
        "identity": {
            "input": {"sha256": "a" * 64},
            "result": {"sha256": "b" * 64},
            "model": {"id": "whisper-medium", "revision": "fixed", "sha256": "c" * 64},
            "provider": {"name": "faster-whisper", "version": "1.1.1"},
            "adapter": {"name": "native-adapter", "version": "1", "sha256": "d" * 64},
        },
        "input": {"duration_seconds": 2.0, "sample_rate": 16000, "frame_count": 32000, "sha256": "a" * 64},
        "text": "before Childhood after",
        "native_units": [
            {"native_index": 0, "text": "before ", "start": 0.0, "end": 0.5},
            {"native_index": 1, "text": "Child", "start": 0.5, "end": 0.8},
            {"native_index": 2, "text": "hood", "start": 0.8, "end": 1.1},
            {"native_index": 3, "text": " after", "start": 1.4, "end": 1.8},
        ],
        "timestamp_kind": "native_unit_seconds",
    }


def _codes(candidate: dict) -> set[str]:
    return {issue["code"] for issue in candidate["issues"]}


def _no_execution(report: dict) -> None:
    flags = (
        "production_eligible", "acoustic_accepted", "neighbor_protection_verified",
        "authoritative_cut_boundary", "execution_required",
    )
    for key in flags:
        assert report[key] is False
    for candidate in report["candidates"]:
        for key in flags:
            assert candidate[key] is False
    assert report["executable_cuts"] == []


def test_unique_exact_hit_groups_native_subwords_without_authorizing_cuts() -> None:
    transcript = _transcript()
    original = deepcopy(transcript)
    report = locate_candidates(transcript, "Childhood", must_keep=["before", "after"])
    assert transcript == original
    assert report["status"] == "unique_text_support"
    candidate = report["candidates"][0]
    assert candidate["native_mapping"]["native_indices"] == [1, 2]
    assert candidate["native_window"] == {"start": 0.5, "end": 1.1}
    assert candidate["word_boundary_status"] == "structurally_valid_only"
    structure = candidate["boundary_validation"]
    assert structure["raw_native_units"] == original["native_units"]
    assert structure["neighbor_units_before_pruning"] == {
        "before": original["native_units"][0], "after": original["native_units"][3],
    }
    assert structure["identity_verification"] == "assumed_caller_contract_not_verified_here"
    assert structure["supplied_identity"] == transcript["identity"]
    assert report["caller_verified_artifact_required"] is True
    _no_execution(report)


def test_repeated_hits_remain_ambiguous_and_are_all_returned() -> None:
    transcript = _transcript()
    transcript.update(text="Childhood Childhood", timestamp_kind="unavailable", native_units=[])
    report = locate_candidates(transcript, "Childhood")
    assert report["status"] == "ambiguous"
    assert report["count"] == 2
    assert [row["raw_range"] for row in report["candidates"]] == [[0, 9], [10, 19]]
    assert all(row["native_window"] is None for row in report["candidates"])
    _no_execution(report)


@pytest.mark.parametrize("text", ["Childhoods", "preChildhood", "Childhood2", "2Childhood", "Childhood\u0301", "éChildhood"])
def test_english_query_cannot_match_inside_latin_marks_or_digits(text: str) -> None:
    transcript = _transcript()
    transcript["text"] = text
    assert locate_candidates(transcript, "Childhood")["count"] == 0


@pytest.mark.parametrize("text", ["CHILDHOOD，", "这是Childhood。", "Child hood"])
def test_case_punctuation_and_whitespace_match_preserves_original_span(text: str) -> None:
    transcript = _transcript()
    transcript.update(text=text, timestamp_kind="unavailable", native_units=[])
    report = locate_candidates(transcript, "Childhood")
    assert report["count"] == 1
    candidate = report["candidates"][0]
    start, end = candidate["raw_range"]
    assert candidate["raw_text"] == text[start:end]
    assert candidate["native_window"] is None


def test_finite_traditional_mapping_does_not_replace_homophones() -> None:
    transcript = _transcript()
    transcript.update(text="幂的運算規則", native_units=[], timestamp_kind="unavailable")
    assert locate_candidates(transcript, "幂的运算规则")["count"] == 1
    transcript["text"] = "密的運算規則"
    assert locate_candidates(transcript, "幂的运算规则")["count"] == 0


def test_casefold_expansion_cannot_match_a_partial_original_character() -> None:
    transcript = _transcript()
    transcript.update(text="ß", native_units=[], timestamp_kind="unavailable")
    assert locate_candidates(transcript, "s")["count"] == 0
    assert locate_candidates(transcript, "ss")["count"] == 1


@pytest.mark.parametrize("start,end,code", [
    (0.5, 0.5, "native_interval_nonpositive"),
    (0.8, 0.5, "native_interval_nonpositive"),
    (math.nan, 0.8, "native_time_invalid"),
    (0.5, math.inf, "native_time_invalid"),
])
def test_invalid_target_times_remain_diagnostic_and_are_never_repaired(start, end, code: str) -> None:
    transcript = _transcript()
    transcript["native_units"][1].update(start=start, end=end)
    report = locate_candidates(transcript, "Childhood")
    candidate = report["candidates"][0]
    assert candidate["native_window"] is None
    assert code in _codes(candidate)
    assert len(candidate["boundary_validation"]["raw_native_units"]) == 4
    json.dumps(report, allow_nan=False)


def test_reversed_units_and_overlap_are_preserved_and_blocked() -> None:
    transcript = _transcript()
    transcript["native_units"][2].update(start=0.4, end=0.9)
    candidate = locate_candidates(transcript, "Childhood")["candidates"][0]
    assert {"native_order_reversed", "native_intervals_overlap"} <= _codes(candidate)
    assert candidate["native_window"] is None


@pytest.mark.parametrize("gap,allowed", [(1.5, True), (1.50001, False)])
def test_native_continuity_is_enforced_at_candidate_retrieval(gap: float, allowed: bool) -> None:
    transcript = _transcript()
    transcript["input"]["duration_seconds"] = 4.0
    for row in transcript["native_units"][2:]:
        row["start"] += gap
        row["end"] += gap
    candidate = locate_candidates(transcript, "Childhood")["candidates"][0]
    assert (candidate["native_window"] is not None) is allowed
    if not allowed:
        assert "native_gap_exceeds_limit" in _codes(candidate)


def test_target_cannot_skip_an_intervening_chinese_unit() -> None:
    transcript = _transcript()
    transcript["text"] = "before Child的hood after"
    transcript["native_units"].insert(2, {"text": "的", "start": 0.8, "end": 0.9})
    for index, row in enumerate(transcript["native_units"]):
        row["native_index"] = index
    assert locate_candidates(transcript, "Childhood")["count"] == 0


def test_punctuation_only_intervening_native_row_is_not_filtered() -> None:
    transcript = _transcript()
    transcript["native_units"].insert(2, {"text": ",", "start": 0.8, "end": 0.8})
    for index, row in enumerate(transcript["native_units"]):
        row["native_index"] = index
    candidate = locate_candidates(transcript, "Childhood")["candidates"][0]
    assert candidate["native_mapping"]["native_indices"] == [1, 2, 3]
    assert "native_interval_nonpositive" in _codes(candidate)


def test_target_inside_larger_native_unit_has_no_synthetic_word_edges() -> None:
    transcript = _transcript()
    transcript["native_units"] = [{"native_index": 0, "text": transcript["text"], "start": 0.0, "end": 1.8}]
    candidate = locate_candidates(transcript, "Childhood")["candidates"][0]
    assert candidate["native_mapping"]["status"] == "native_unit_too_large"
    assert candidate["native_window"] is None
    assert "target_edges_inside_native_unit" in _codes(candidate)


def test_native_text_mismatch_does_not_guess_an_occurrence_mapping() -> None:
    transcript = _transcript()
    transcript["native_units"][1]["text"] = "different"
    candidate = locate_candidates(transcript, "Childhood")["candidates"][0]
    assert "native_text_does_not_match_transcript" in _codes(candidate)
    assert candidate["native_window"] is None


@pytest.mark.parametrize("index", [0, 3])
def test_invalid_immediate_neighbor_withholds_target(index: int) -> None:
    transcript = _transcript()
    transcript["native_units"][index]["end"] = transcript["native_units"][index]["start"]
    candidate = locate_candidates(transcript, "Childhood")["candidates"][0]
    assert "native_interval_nonpositive" in _codes(candidate)
    assert candidate["native_window"] is None


def test_must_keep_overlap_is_checked_before_any_pruning() -> None:
    report = locate_candidates(_transcript(), "Childhood", must_keep=["Childhood"])
    candidate = report["candidates"][0]
    assert "protected_window_overlap" in _codes(candidate)
    assert candidate["native_window"] is None
    assert candidate["boundary_validation"]["protected_windows_before_pruning"] == [
        {"start": 0.5, "end": 1.1, "phrase": "Childhood"}
    ]


def test_touching_must_keep_edge_is_structurally_acceptable_without_padding() -> None:
    report = locate_candidates(_transcript(), "Childhood", must_keep=["before"])
    assert report["candidates"][0]["native_window"] == {"start": 0.5, "end": 1.1}
    assert report["neighbor_protection_verified"] is False


@pytest.mark.parametrize("phrase,code", [("missing", "must_keep_not_found"), ("before", "must_keep_ambiguous")])
def test_missing_or_multiple_must_keep_is_unverified(phrase: str, code: str) -> None:
    transcript = _transcript()
    if phrase == "before":
        transcript["text"] += " before"
        transcript["native_units"].append({"native_index": 4, "text": " before", "start": 1.8, "end": 1.9})
    report = locate_candidates(transcript, "Childhood", must_keep=[phrase])
    assert code in _codes(report["candidates"][0])
    assert report["must_keep"][0]["status"] == "unverified"
    assert report["candidates"][0]["native_window"] is None


def test_unavailable_timestamps_do_not_use_segment_or_chunk_times() -> None:
    transcript = _transcript()
    transcript["timestamp_kind"] = "unavailable"
    transcript["chunks"] = [{"text": transcript["text"], "start": 0.0, "end": 1.8}]
    report = locate_candidates(transcript, "Childhood")
    candidate = report["candidates"][0]
    assert report["count"] == 1
    assert candidate["native_window"] is None
    assert candidate["word_boundary_status"] == "unavailable"


def test_candidate_function_does_not_invent_missing_result_identity() -> None:
    transcript = _transcript()
    del transcript["identity"]["result"]
    candidate = locate_candidates(transcript, "Childhood")["candidates"][0]
    assert "identity_component_missing" in _codes(candidate)
    assert candidate["native_window"] is None


def test_input_flags_cannot_authorize_candidate_execution() -> None:
    transcript = _transcript()
    transcript.update(production_eligible=True, acoustic_accepted=True, execution_required=True)
    report = locate_candidates(transcript, "Childhood")
    _no_execution(report)


def test_wrong_text_control_remains_unsupported_despite_any_other_timings() -> None:
    transcript = _transcript()
    transcript["forced_alignment"] = [{"text": "Tomorrow", "start": 0.5, "end": 1.1}]
    report = locate_candidates(transcript, "Tomorrow")
    assert report["status"] == "no_text_support"
    assert report["candidates"] == []
    _no_execution(report)


@pytest.mark.parametrize("query", ["", "...", None])
def test_empty_or_invalid_queries_are_rejected(query) -> None:
    with pytest.raises(ValueError):
        locate_candidates(_transcript(), query)
