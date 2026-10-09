from __future__ import annotations

import json
import math
from copy import deepcopy

import pytest

from audio_sound.local_asr_boundaries import validate_local_asr_boundaries


def _identity() -> dict:
    return {
        "input": {"sha256": "a" * 64, "verified": True},
        "result": {"sha256": "b" * 64, "verified": True},
        "model": {
            "id": "whisper-medium",
            "revision": "pinned-revision",
            "sha256": "c" * 64,
            "verified": True,
        },
        "provider": {"name": "faster-whisper", "version": "1.1.1", "verified": True},
        "adapter": {
            "name": "fixture-native-adapter",
            "version": "1",
            "sha256": "d" * 64,
            "verified": True,
        },
        "timestamp_kind": "native_unit_seconds",
    }


def _units() -> list[dict]:
    return [
        {"native_index": 0, "text": "before", "start": 0.0, "end": 0.5},
        {"native_index": 1, "text": "Child", "start": 0.5, "end": 0.8},
        {"native_index": 2, "text": "hood", "start": 0.8, "end": 1.1},
        {"native_index": 3, "text": "after", "start": 1.4, "end": 1.8},
    ]


_DEFAULT_UNITS = object()


def _validate(units=_DEFAULT_UNITS, **overrides) -> dict:
    args = {
        "identity": _identity(),
        "crop_offset_seconds": 40.0,
        "crop_duration_seconds": 2.0,
        "source_duration_seconds": 100.0,
        "selected_native_indices": [1, 2],
    }
    args.update(overrides)
    return validate_local_asr_boundaries(_units() if units is _DEFAULT_UNITS else units, **args)


def _codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report["issues"]}


def test_exact_native_subword_grouping_is_structural_only_and_preserves_raw_rows() -> None:
    units = _units()
    original = deepcopy(units)
    grouped = [
        {"native_indices": [0], "text": "before", "start": 0.0, "end": 0.5},
        {"native_indices": [1, 2], "text": "Childhood", "start": 0.5, "end": 1.1},
        {"native_indices": [3], "text": "after", "start": 1.4, "end": 1.8},
    ]

    report = _validate(units, selected_window={"start": 0.5, "end": 1.1}, grouped_units=grouped)

    assert report["status"] == "structural_pass"
    assert report["source_window"] == {"start": 40.5, "end": 41.1}
    assert report["raw_native_units"] == original == units
    assert report["neighbor_units_before_pruning"] == {"before": units[0], "after": units[3]}
    assert report["production_eligible"] is False
    assert report["acoustic_accepted"] is False
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("component", ["input", "result", "model", "provider", "adapter"])
def test_each_identity_component_must_be_present_and_verified(component: str) -> None:
    identity = _identity()
    identity.pop(component)
    assert "identity_component_missing" in _codes(_validate(identity=identity))
    identity = _identity()
    identity[component]["verified"] = "true"
    assert "identity_not_verified" in _codes(_validate(identity=identity))


@pytest.mark.parametrize(
    "field", ["crop_offset_seconds", "crop_duration_seconds", "source_duration_seconds"]
)
@pytest.mark.parametrize("value", [None, "1", True, math.nan, math.inf])
def test_crop_metadata_requires_explicit_finite_numeric_values(field: str, value) -> None:
    report = _validate(**{field: value})
    assert report["status"] == "blocked"
    json.dumps(report, allow_nan=False)


def test_malformed_identity_hashes_and_timestamp_semantics_are_blocked() -> None:
    assert "identity_missing_or_malformed" in _codes(_validate(identity=None))
    identity = _identity()
    identity["result"]["sha256"] = "not-a-hash"
    identity["model"].pop("revision")
    identity["timestamp_kind"] = "segment_seconds"
    codes = _codes(_validate(identity=identity))
    assert {
        "identity_sha256_malformed",
        "identity_field_missing",
        "native_timestamp_semantics_unverified",
    } <= codes


@pytest.mark.parametrize(
    "metadata",
    [
        {"crop_offset_seconds": -1},
        {"crop_duration_seconds": 0},
        {"source_duration_seconds": 0},
        {"crop_offset_seconds": 99},
    ],
)
def test_crop_extent_is_bounded_by_source(metadata: dict) -> None:
    assert _validate(**metadata)["status"] == "blocked"


def test_finite_input_overflow_cannot_make_nonfinite_json_source_edges() -> None:
    units = [{"native_index": 0, "text": "word", "start": 1e308, "end": 1.2e308}]
    report = _validate(
        units,
        selected_native_indices=[0],
        crop_offset_seconds=1e308,
        crop_duration_seconds=1.3e308,
        source_duration_seconds=1.7e308,
    )
    assert "source_window_arithmetic_invalid" in _codes(report)
    assert report["source_window"] is None
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize(
    "indices", [None, [], [-1, 0], [1, 4], [1, 1], [2, 1], [1, 3], [True, 2], ["1", 2]]
)
def test_selected_indices_cannot_drop_duplicate_or_reorder_native_units(indices) -> None:
    assert _validate(selected_native_indices=indices)["status"] == "blocked"


@pytest.mark.parametrize(
    "start,end,code",
    [
        (0.5, 0.5, "native_interval_nonpositive"),
        (0.8, 0.5, "native_interval_nonpositive"),
        (math.nan, 0.8, "native_time_invalid"),
        (0.5, math.inf, "native_time_invalid"),
        ("0.5", 0.8, "native_time_invalid"),
        (-0.1, 0.8, "native_interval_out_of_bounds"),
        (0.5, 2.1, "native_interval_out_of_bounds"),
    ],
)
def test_invalid_native_time_is_not_filtered_or_repaired(start, end, code: str) -> None:
    units = _units()
    units[1].update(start=start, end=end)
    report = _validate(units)
    assert code in _codes(report)
    assert len(report["raw_native_units"]) == 4
    assert report["raw_native_units"][1]["native_index"] == 1
    json.dumps(report, allow_nan=False)


def test_reversed_and_overlapping_units_block() -> None:
    units = _units()
    units[2].update(start=0.4, end=0.9)
    assert {"native_order_reversed", "native_intervals_overlap"} <= _codes(_validate(units))


@pytest.mark.parametrize("neighbor_index", [0, 3])
def test_immediate_neighbor_overlap_blocks_without_explicit_protection(neighbor_index: int) -> None:
    units = _units()
    if neighbor_index == 0:
        units[0]["end"] = 0.6
    else:
        units[3]["start"] = 1.0
    report = _validate(units)
    assert "native_intervals_overlap" in _codes(report)
    assert units[neighbor_index] in report["neighbor_units_before_pruning"].values()


@pytest.mark.parametrize("remote_index", [0, 4])
@pytest.mark.parametrize("selected_window", [None, {"start": 0.4, "end": 0.6}])
@pytest.mark.parametrize("intersects", [True, False])
def test_all_unselected_native_intervals_are_checked_beyond_immediate_neighbors(
    remote_index: int, selected_window: dict | None, intersects: bool
) -> None:
    units = [
        {"native_index": 0, "text": "older", "start": 0.0, "end": 0.2},
        {"native_index": 1, "text": "before", "start": 0.2, "end": 0.4},
        {"native_index": 2, "text": "target", "start": 0.4, "end": 0.6},
        {"native_index": 3, "text": "after", "start": 0.6, "end": 0.8},
        {"native_index": 4, "text": "newer", "start": 0.8, "end": 1.0},
    ]
    if remote_index == 0:
        units[0]["end"] = 1.0 if intersects else 0.4
    else:
        units[4]["start"] = 0.3 if intersects else 0.6

    report = _validate(units, selected_native_indices=[2], selected_window=selected_window)

    if intersects:
        assert report["status"] == "blocked"
        conflicts = [
            issue
            for issue in report["issues"]
            if issue["code"] == "unselected_native_interval_overlap"
        ]
        assert len(conflicts) == 1
        assert conflicts[0]["native_indices"] == [remote_index]
        assert conflicts[0]["selected_native_indices"] == [2]
    else:
        assert report["status"] == "structural_pass"
        assert report["native_unit_issues"]
    assert report["raw_native_units"] == units
    assert report["neighbor_units_before_pruning"] == {"before": units[1], "after": units[3]}


@pytest.mark.parametrize("remote_index", [0, 4])
def test_remote_native_edge_roundoff_is_not_real_overlap(remote_index: int) -> None:
    units = [
        {"native_index": 0, "text": "older", "start": 0.0, "end": 0.4},
        {"native_index": 1, "text": "before", "start": 0.2, "end": 0.4},
        {"native_index": 2, "text": "target", "start": 0.4, "end": 0.6},
        {"native_index": 3, "text": "after", "start": 0.6, "end": 0.8},
        {"native_index": 4, "text": "newer", "start": 0.6, "end": 1.0},
    ]
    if remote_index == 0:
        units[0]["end"] += 1e-13
    else:
        units[4]["start"] -= 1e-13
    assert _validate(units, selected_native_indices=[2])["status"] == "structural_pass"


@pytest.mark.parametrize("neighbor_index", [0, 3])
def test_invalid_immediate_neighbor_cannot_be_removed(neighbor_index: int) -> None:
    units = _units()
    units[neighbor_index]["end"] = units[neighbor_index]["start"]
    assert "native_interval_nonpositive" in _codes(_validate(units))


def test_arithmetic_roundoff_is_not_real_overlap() -> None:
    units = _units()
    units[0]["end"] += 1e-13
    units[2]["start"] -= 1e-13
    units[3]["start"] = 1.1 - 1e-13
    assert _validate(units)["status"] == "structural_pass"


@pytest.mark.parametrize("gap,expected", [(1.5, "structural_pass"), (1.50001, "blocked")])
def test_native_continuity_uses_maximum_one_point_five_seconds(gap: float, expected: str) -> None:
    units = _units()
    units[2].update(start=0.8 + gap, end=1.1 + gap)
    units[3].update(start=1.4 + gap, end=1.8 + gap)
    report = _validate(units, crop_duration_seconds=4.0)
    assert report["status"] == expected


@pytest.mark.parametrize(
    "middle",
    [
        {"text": "\u7684", "start": 0.8, "end": 0.9},
        {"text": "invalid", "start": 0.8, "end": 0.8},
    ],
)
def test_skipped_chinese_or_invalid_unit_cannot_be_aggregated(middle: dict) -> None:
    units = _units()
    units.insert(2, {"native_index": 2, **middle})
    for index, unit in enumerate(units):
        unit["native_index"] = index
    report = _validate(units, selected_native_indices=[1, 3])
    assert "selected_indices_not_consecutive" in _codes(report)
    assert report["raw_native_units"][2] == middle | {"native_index": 2}


def test_filtered_original_list_is_detected_by_native_indices() -> None:
    units = _units()
    units.pop(2)
    assert "native_index_mismatch" in _codes(_validate(units, selected_native_indices=[1]))


@pytest.mark.parametrize(
    "units", [None, {}, [], [None], [{"native_index": 0, "text": None, "start": 0, "end": 1}]]
)
def test_malformed_native_rows_remain_reported_instead_of_disappearing(units) -> None:
    report = _validate(units, selected_native_indices=[0])
    assert report["status"] == "blocked"
    assert report["raw_native_units"] == units


@pytest.mark.parametrize(
    "window", [{"start": 0.4, "end": 1.1}, {"start": 0.5, "end": 1.5}, {"start": 0.6, "end": 1.0}]
)
def test_requested_edges_cannot_widen_into_neighbors_or_slice_native_unit(window: dict) -> None:
    assert "selected_edges_not_native" in _codes(_validate(selected_window=window))


def test_explicit_protection_is_checked_before_pruning_and_preserved() -> None:
    windows = [{"start": 0.7, "end": 0.9, "phrase": "must keep", "origin": "explicit"}]
    report = _validate(protected_windows=windows)
    assert "protected_window_overlap" in _codes(report)
    assert report["protected_windows_before_pruning"] == windows
    assert _validate(protected_windows=[{"start": 0.0, "end": 0.5}])["status"] == "structural_pass"


@pytest.mark.parametrize(
    "windows", [None, [None], [{"start": 1, "end": 1}], [{"start": -1, "end": 0.1}]]
)
def test_invalid_protection_cannot_silently_disappear(windows) -> None:
    assert _validate(protected_windows=windows)["status"] == "blocked"


@pytest.mark.parametrize(
    "groups",
    [
        [],
        [{"native_indices": [0, 1, 3], "text": "beforeChildafter", "start": 0.0, "end": 1.8}],
        [
            {
                "native_indices": [0, 1, 2, 2, 3],
                "text": "beforeChildhoodhoodafter",
                "start": 0.0,
                "end": 1.8,
            }
        ],
        [
            {
                "native_indices": [0, 1, 2, 3],
                "text": "beforeChildhoodafter",
                "start": 0.1,
                "end": 1.8,
            }
        ],
        [
            {
                "native_indices": [0, 1, 2, 3],
                "text": "BeforeChildhoodafter",
                "start": 0.0,
                "end": 1.8,
            }
        ],
    ],
)
def test_many_to_one_mapping_requires_full_raw_text_partition_and_native_edges(groups) -> None:
    assert _validate(grouped_units=groups)["status"] == "blocked"


def test_unselected_anomaly_is_retained_and_full_grouping_cannot_hide_it() -> None:
    units = _units()
    units.append({"native_index": 4, "text": "later", "start": 1.9, "end": 1.9})
    report = _validate(units)
    assert report["status"] == "structural_pass"
    assert {"code": "native_interval_nonpositive", "native_indices": [4]} in report[
        "native_unit_issues"
    ]
    grouped = [
        {
            "native_indices": [0, 1, 2, 3, 4],
            "text": "beforeChildhoodafterlater",
            "start": 0.0,
            "end": 1.9,
        }
    ]
    assert "native_interval_nonpositive" in _codes(_validate(units, grouped_units=grouped))


def test_known_fun_asr_paraformer_timestamp_ambiguity_is_blocked() -> None:
    identity = _identity()
    identity["provider"].update(name="funasr", version="1.2.6")
    identity["model"]["id"] = "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
    assert "funasr_1_2_6_paraformer_timestamp_ambiguity" in _codes(_validate(identity=identity))
