"""Mixed review intake and whole-source labels must not authorize timeline edits."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from utils.review_document_intake import parse_lark_document
from utils.review_document_runner import _asr_required, _assert_authoritative_starts
from utils.review_job_compiler import compile_review_job
from utils.revision_markers import build_marker_plan
from utils.revision_runner import execute_revision_request, load_revision_request
from utils.source_manifest import _review_items_from_section, select_docx_section


def parse(xml):
    return parse_lark_document({"document_id": "review-doc", "content": xml})


def compile_request(tmp_path, rows, *, pairs=None, workflow="lite"):
    project = {
        "draft_name": "ScopeLabels",
        "source_video": "source.mp4",
        "source_audio": "source.wav",
        "media_duration_seconds": 10,
        "workflow_mode": workflow,
    }
    if pairs:
        project["source_pairs"] = pairs
        project["media_duration_seconds"] = sum(row["video_duration_seconds"] for row in pairs)
    compile_review_job({"review_items": rows}, project, tmp_path / "compiled")
    path = tmp_path / "compiled" / "revision_request.json"
    return path, load_revision_request(str(path))


def test_mixed_region_keeps_plain_lists_and_checked_originals_without_body_text():
    text = "  使用“某音色”变声\n保持原话  "
    parsed = parse(
        "<h1>课程正文</h1><p>需要理解国家统一的重要性。</p>"
        "<h1>修改意见</h1>"
        f'<checkbox id="voice" checked="true"><span>{text}</span></checkbox>'
        '<p id="bar">左下角工具栏隐藏</p><ul><li id="rough">粗剪</li></ul>'
        "<p>7.05MB</p><p>lesson.mp4</p><p>示例：删除某句话</p>"
        "<h1>参考正文</h1><p>添加的知识点是例子。</p>"
    )
    rows = parsed["review_items"]
    assert [row["source_text"] for row in rows] == [text, "左下角工具栏隐藏", "粗剪"]
    assert rows[0]["evidence"]["review_intake"]["checked"] is True
    assert rows[1]["evidence"]["review_intake"]["checked"] is None
    assert len({row["block_id"] for row in rows}) == 3
    section = select_docx_section(parsed, "修改意见", {"修改意见"})
    assert [row["source_text"] for row in _review_items_from_section(section)] == [
        text,
        "左下角工具栏隐藏",
        "粗剪",
    ]


def test_recording_checklist_targets_the_attached_video():
    parsed = parse(
        '<h1>2.录屏</h1><figure><source token="v" name="lesson.mp4" mime="video/mp4"/></figure>'
        "<p>使用某音色变声</p><p>左下角工具栏隐藏</p><p>粗剪</p>"
    )
    assert len(parsed["review_items"]) == 3
    assert all(
        row["evidence"]["review_intake"]["target_asset_id"] == parsed["assets"][0]["asset_id"]
        for row in parsed["review_items"]
    )


def test_mixed_item_insert_does_not_move_existing_attachment_binding():
    parsed = parse(
        '<h1>修改意见</h1><p id="whole">粗剪</p>'
        '<checkbox id="pointer">00:02 添加小手指向此处</checkbox>'
        '<img token="hand" name="hand.png" mime="image/png"/>'
    )
    assert parsed["assets"][0]["associated_item_index"] == 1


@pytest.mark.parametrize("text", ["使用某音色变声", "左下角工具栏隐藏", "粗剪", "全片统一字幕样式"])
def test_global_comments_are_label_only_without_asr_or_fabricated_edit(tmp_path, text):
    path, request = compile_request(tmp_path, [{"id": "whole", "source_text": text}])
    item = request.review_items[0]
    assert item.kind == "global_review"
    assert item.execution_required is False
    assert item.execution_status == "label_only_global_review"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert not _asr_required(payload)
    _assert_authoritative_starts(payload)
    plan = build_marker_plan(request)
    assert plan[0].source_text == text
    assert plan[0].start == 0
    assert plan[0].review_scope["end"] == 10
    assert not request.edits and not request.pause_adjustments


@pytest.mark.parametrize("text", ["这里隐藏工具栏", "这句使用某音色变声", "调整一下"])
def test_local_missing_time_is_not_reclassified_as_global(tmp_path, text):
    _, request = compile_request(tmp_path, [{"id": "local", "source_text": text}])
    assert request.review_items[0].kind != "global_review"
    marker = build_marker_plan(request)[0]
    assert marker.start == 0
    assert marker.label_placement["basis"] == "first_local_label"
    assert marker.execution_status == "label_only_unresolved"
    assert not request.edits


def test_timed_unusual_item_preserves_its_requested_position(tmp_path):
    _, request = compile_request(
        tmp_path, [{"id": "local", "source_text": "00:04 左下角工具栏隐藏"}]
    )
    assert request.review_items[0].kind != "global_review"
    assert build_marker_plan(request)[0].start == 4


def pair(index, duration):
    return {
        "pair_index": index,
        "video_path": f"v{index}.mp4",
        "video_sha256": str(index + 1) * 64,
        "video_duration_seconds": duration,
        "audio_mode": "video_original",
    }


def test_multiple_sources_require_binding_and_scope_anchor_uses_offset(tmp_path):
    pairs = [pair(0, 6), pair(1, 4)]
    rows = [
        {
            "id": "whole",
            "source_text": "粗剪",
            "evidence": {"review_intake": {"target_pair_index": 1}},
        }
    ]
    _, request = compile_request(tmp_path, rows, pairs=pairs)
    marker = build_marker_plan(request)[0]
    assert marker.start == 6
    assert marker.review_scope["end"] == 10
    _, ambiguous = compile_request(tmp_path, [{"source_text": "粗剪"}], pairs=pairs)
    with pytest.raises(ValueError):
        build_marker_plan(ambiguous)


def test_whole_project_scope_is_explicit(tmp_path):
    _, request = compile_request(
        tmp_path, [{"source_text": "全部视频统一字幕样式"}], pairs=[pair(0, 6), pair(1, 4)]
    )
    scope = build_marker_plan(request)[0].review_scope
    assert scope["kind"] == "project" and scope["end"] == 10


def test_full_workflow_does_not_gain_lite_global_execution_exception(tmp_path):
    _, request = compile_request(tmp_path, [{"source_text": "全片统一字幕样式"}], workflow="full")
    assert request.review_items[0].kind != "global_review"


@pytest.mark.parametrize("paired", [False, True])
def test_saved_draft_keeps_each_verbatim_global_label_and_original_timeline(tmp_path, paired):
    rows = [
        {
            "id": f"whole-{i}",
            "source_text": text,
            "evidence": {"review_intake": {"target_pair_index": 1 if paired else 0}},
        }
        for i, text in enumerate(["使用某音色变声", "左下角工具栏隐藏", "粗剪"])
    ]
    _, request = compile_request(tmp_path, rows, pairs=[pair(0, 6), pair(1, 4)] if paired else None)
    result = execute_revision_request(
        request, drafts_root=str(tmp_path / "drafts"), mock_media=True
    )
    assert result["validation"]["ok"], result["validation"]
    assert len(result["global_review_labels"]) == 3
    assert all(row["executed"] is False for row in result["global_review_labels"])
    receipts = result["review_marker_receipts"]
    assert len(receipts) == 3
    assert len({row["track_name"] for row in receipts}) == 3
    assert all(row["track_name"].startswith("Review Marker Global ") for row in receipts)
    for content_path in Path(result["draft_path"]).rglob("draft_content.json"):
        content = json.loads(content_path.read_text(encoding="utf-8"))
        assert content["duration"] == 10_000_000
        tracks = [
            track
            for track in content["tracks"]
            if track["name"].startswith("Review Marker Global ")
        ]
        segments = [segment for track in tracks for segment in track["segments"]]
        assert len(segments) == 3
        assert all(
            s["target_timerange"] == {"start": 6_000_000 if paired else 0, "duration": 2_000_000}
            for s in segments
        )
        assert not [
            track
            for track in content["tracks"]
            if track["name"] in {"Lite Cut Segments", "Lite Reused Audio"} and track["segments"]
        ]


@pytest.mark.parametrize("mixed", [False, True])
def test_public_pipeline_keeps_global_labels_alongside_precise_audio_items(tmp_path, mixed):
    from utils import review_document_runner as runner

    from tests.test_review_document_runner import ReviewDocumentRunnerTests as RunnerHarness

    harness = RunnerHarness()
    snapshot, project = harness._audio_inputs(tmp_path)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    if not mixed:
        payload["items"] = []
    payload["items"].append({"id": "global", "source_text": "使用某音色变声"})
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def write_mock(request, **kwargs):
        kwargs.update(mock_media=True, localize_materials=False)
        return execute_revision_request(request, **kwargs)

    with (
        harness._patched_runtime() as mocks,
        patch.object(runner, "execute_revision_request", side_effect=write_mock),
    ):
        result = harness._run(
            snapshot,
            project,
            job_root=tmp_path / "job",
            drafts_root=tmp_path / "drafts",
            package_zip=tmp_path / "out.zip",
            cache_root=tmp_path / "cache",
        )
    assert result["ok"], result
    assert len(result["global_review_labels"]) == 1
    assert result["global_review_labels"][0]["executed"] is False
    assert mocks["asr"].call_count == (2 if mixed else 0)


def test_untimed_local_labels_follow_document_order_without_moving_timed_items(tmp_path):
    texts = ["00:02 这里隐藏工具栏", "调整一下", "这里替换画面", "00:07 调整字号", "这里隐藏工具栏"]
    _, request = compile_request(
        tmp_path, [{"id": str(i), "source_text": text} for i, text in enumerate(texts)]
    )
    plan = build_marker_plan(request)
    assert [item.start for item in plan] == [2, 4, 6, 7, 9]
    assert [item.source_text for item in plan] == texts
    assert plan[2].label_placement["previous_item_id"] == "1"
    result = execute_revision_request(
        request, drafts_root=str(tmp_path / "drafts"), mock_media=True
    )
    assert result["validation"]["ok"], result["validation"]
    assert len(result["review_marker_receipts"]) == len(texts)
    assert result["review_marker_receipts"][1]["label_placement"]["display_only"]
    for path in Path(result["draft_path"]).rglob("draft_content.json"):
        content = json.loads(path.read_text(encoding="utf-8"))
        assert content["duration"] == 10_000_000
        markers = [
            s
            for t in content["tracks"]
            if t["name"].startswith("Review Marker ")
            for s in t["segments"]
        ]
        assert sorted(s["target_timerange"]["start"] for s in markers) == [
            2_000_000,
            4_000_000,
            6_000_000,
            7_000_000,
            9_000_000,
        ]


def test_missing_labels_at_end_are_clamped_without_extending_media(tmp_path):
    _, request = compile_request(
        tmp_path,
        [
            {"id": "timed", "source_text": "00:09 这里隐藏工具栏"},
            {"id": "a", "source_text": "调整一下"},
            {"id": "b", "source_text": "这里替换画面"},
        ],
    )
    plan = build_marker_plan(request)
    assert [p.start for p in plan] == [9, 9.99, 9.99]
    assert all(p.label_placement["clamped"] for p in plan[1:])
    result = execute_revision_request(
        request, drafts_root=str(tmp_path / "drafts"), mock_media=True
    )
    assert result["validation"]["ok"], result["validation"]
    assert len(result["review_marker_receipts"]) == 3


@pytest.mark.parametrize("unavailable", [False, True])
def test_untimed_audio_fallback_is_label_only_after_asr_attempt(tmp_path, unavailable):
    from utils import review_document_runner as runner

    from tests.test_review_document_runner import ReviewDocumentRunnerTests as RunnerHarness

    harness = RunnerHarness()
    snapshot, project = harness._audio_inputs(tmp_path)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["items"] = [
        {"id": "first", "source_text": "删除“没有出现的句子甲”"},
        {"id": "timed", "source_text": "00:01 这里隐藏工具栏"},
        {"id": "speech", "source_text": "删除“没有出现的句子乙”"},
        {"id": "local", "source_text": "这里替换画面"},
    ]
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def write_mock(request, **kwargs):
        kwargs.update(mock_media=True, localize_materials=False)
        return execute_revision_request(request, **kwargs)

    with (
        harness._patched_runtime() as mocks,
        patch.object(runner, "execute_revision_request", side_effect=write_mock),
    ):
        if unavailable:
            mocks["asr"].side_effect = RuntimeError("test ASR unavailable")
        result = harness._run(
            snapshot,
            project,
            job_root=tmp_path / "job",
            drafts_root=tmp_path / "drafts",
            package_zip=tmp_path / "out.zip",
            cache_root=tmp_path / "cache",
        )
    assert result["ok"], result
    assert mocks["asr"].call_count == 1
    processed = json.loads(
        Path(result["output_artifacts"]["revision_request"]["path"]).read_text(encoding="utf-8")
    )
    assert not processed["edits"] and not processed["pause_adjustments"]
    items = processed["review_items"]
    assert [item["start"] for item in items] == [0, 1, 2.99, 2.99]
    for item in [items[0], items[2], items[3]]:
        assert item["evidence"]["timing_source"] == "document_order_fallback"
        assert not item["execution_required"]


def test_display_fallback_never_becomes_asr_search_or_cut_time():
    from utils.review_audio_precision import _review_timestamp, _rough_window
    from utils.review_scope import place_missing_review_labels

    rows = [
        {
            "id": "first",
            "source_text": "删除这句话",
            "evidence": {"timing_source": "document_order_fallback"},
        }
    ]
    place_missing_review_labels(rows, {"media_duration_seconds": 10})
    assert rows[0]["start"] == 0
    assert _rough_window(rows[0]) == (None, None)
    assert _review_timestamp(rows[0]) is None


@pytest.mark.parametrize("reverse_failure", [False, True])
def test_untimed_speech_requires_real_asr_and_reverse_failure_retains_label(
    tmp_path, reverse_failure
):
    from utils import review_document_runner as runner

    from tests.test_review_document_runner import ReviewDocumentRunnerTests as RunnerHarness

    harness = RunnerHarness()
    snapshot, project = harness._audio_inputs(tmp_path)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    item = payload["items"][0]
    item["source_text"] = "删除“测试”"
    item.pop("start")
    item.pop("end")
    payload["items"].append({"id": "after", "source_text": "这里替换画面"})
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    original_report = runner.build_full_candidate_reverse_report

    def reverse_report(*args, **kwargs):
        report = original_report(*args, **kwargs)
        if reverse_failure:
            report["unresolved_ids"] = ["spoken-1"]
            report["rows"][0]["status"] = "review"
            report["status_counts"] = {"pass": 0, "review": 1}
        return report

    def write_mock(request, **kwargs):
        kwargs.update(mock_media=True, localize_materials=False)
        return execute_revision_request(request, **kwargs)

    with (
        harness._patched_runtime(),
        patch.object(runner, "execute_revision_request", side_effect=write_mock),
        patch.object(runner, "build_full_candidate_reverse_report", side_effect=reverse_report),
    ):
        result = harness._run(
            snapshot,
            project,
            job_root=tmp_path / "job",
            drafts_root=tmp_path / "drafts",
            package_zip=tmp_path / "out.zip",
            cache_root=tmp_path / "cache",
        )
    assert result["ok"], result
    processed = json.loads(
        Path(result["output_artifacts"]["revision_request"]["path"]).read_text(encoding="utf-8")
    )
    if reverse_failure:
        assert processed["edits"] == []
        assert [i["start"] for i in processed["review_items"]] == [0, 2]
        assert (
            processed["review_items"][0]["evidence"]["timing_source"] == "document_order_fallback"
        )
    else:
        assert len(processed["edits"]) == 1
        assert processed["review_items"][0]["evidence"]["asr_alignment"][
            "authoritative_cut_boundary"
        ]
        assert [i["start"] for i in processed["review_items"]] == [1, 2.99]
