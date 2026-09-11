import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from utils.source_manifest import (
    SourceManifestError,
    canonical_sha256,
    load_source_manifest,
    materialize_manifest_sources,
    select_docx_section,
)


def _document_with_attachment(heading: str, filename: str = "inside.mp4") -> dict:
    return {
        "blocks": [
            {"kind": "heading", "level": 2, "text": heading},
            {
                "kind": "attachment",
                "filename": filename,
                "mime": "video/mp4",
            },
            {"kind": "heading", "level": 2, "text": "下一节"},
        ]
    }


def _manifest(*, stage_id: str = "initial") -> dict:
    return {
        "schema_version": 1,
        "binding": {
            "task_id": "task-1",
            "run_id": "run-1",
            "subject_key": "bas_demo:tbl_history",
            "config_version": 7,
            "stage_id": stage_id,
            "event_id": "event-1",
        },
        "record": {
            "base_token": "bas_demo",
            "table_id": "tbl_history",
            "record_id": "record-1",
        },
        "document": {
            "field_id": "fld_document",
            "url": "https://example.feishu.cn/docx/doc_opaque",
        },
        "sources": {
            "video": {"kind": "docx_section", "anchor_text": "二、视频"},
            "review": {"kind": "docx_section", "anchor_text": "(2) 剪辑意见"},
            "audio": {
                "mode": "replace_original",
                "duration_tolerance_seconds": 3,
                "source": {"kind": "docx_section", "anchor_text": "3.1 配音"},
            },
        },
    }


def test_exact_match_wins_before_number_stripped_fallback():
    document = {
        "blocks": [
            {"kind": "heading", "level": 2, "text": "PPT草稿+翻录"},
            {
                "kind": "attachment",
                "filename": "fallback.mp4",
                "mime": "video/mp4",
            },
            {"kind": "heading", "level": 2, "text": "二、PPT草稿+翻录"},
            {
                "kind": "attachment",
                "filename": "exact.mp4",
                "mime": "video/mp4",
            },
            {"kind": "heading", "level": 2, "text": "下一节"},
        ]
    }

    selected = select_docx_section(
        document,
        "二、PPT草稿+翻录",
        {"二、PPT草稿+翻录"},
    )

    assert [row["filename"] for row in selected.attachments] == ["exact.mp4"]


@pytest.mark.parametrize(
    ("anchor", "document_heading"),
    [
        ("二、PPT草稿+翻录", "PPT草稿+翻录"),
        ("2. PPT草稿+翻录", "PPT草稿+翻录"),
        ("（二）PPT草稿+翻录", "PPT草稿+翻录"),
        ("(2) PPT草稿+翻录", "PPT草稿+翻录"),
        ("3.1 PPT草稿+翻录", "PPT草稿+翻录"),
        ("PPT草稿+翻录", "2. PPT草稿+翻录"),
        ("二、PPT草稿+翻录", "2. PPT草稿+翻录"),
    ],
)
def test_supported_number_prefixes_fall_back_to_the_same_complete_body(
    anchor,
    document_heading,
):
    selected = select_docx_section(
        _document_with_attachment(document_heading),
        anchor,
        {anchor},
    )

    assert [row["filename"] for row in selected.attachments] == ["inside.mp4"]
    assert selected.anchor_text == anchor


@pytest.mark.parametrize(
    ("anchor", "document_heading"),
    [
        ("PPT草稿+翻录", "PPT定稿+翻录"),
        ("PPT草稿+翻录", "PPT草稿+翻录（教师版）"),
        ("PPT草稿+翻录", "ppt草稿+翻录"),
        ("PPT草稿+翻录", "2. 3.1 PPT草稿+翻录"),
        ("时代", "2.0时代"),
    ],
)
def test_number_fallback_rejects_fuzzy_or_repeated_prefix_matches(
    anchor,
    document_heading,
):
    with pytest.raises(SourceManifestError) as raised:
        select_docx_section(
            _document_with_attachment(document_heading),
            anchor,
            {anchor},
        )

    assert raised.value.code == "docx_anchor_missing"


def test_number_fallback_reports_multiple_equivalent_candidates_as_ambiguous():
    document = {
        "blocks": [
            {"kind": "heading", "level": 2, "text": "PPT草稿+翻录"},
            {"kind": "heading", "level": 2, "text": "2. PPT草稿+翻录"},
        ]
    }

    with pytest.raises(SourceManifestError) as raised:
        select_docx_section(
            document,
            "二、PPT草稿+翻录",
            {"二、PPT草稿+翻录"},
        )

    assert raised.value.code == "docx_anchor_ambiguous"


def test_manifest_and_selection_preserve_the_original_numbered_anchor(tmp_path, monkeypatch):
    payload = _manifest()
    manifest_path = tmp_path / "source-manifest.json"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    bindings = {
        "CODEX_AUTOCUT_TASK_ID": "task-1",
        "CODEX_AUTOCUT_RUN_ID": "run-1",
        "CODEX_AUTOCUT_SUBJECT_KEY": "bas_demo:tbl_history",
        "CODEX_AUTOCUT_CONFIG_VERSION": "7",
        "CODEX_AUTOCUT_STAGE_ID": "initial",
        "CODEX_AUTOCUT_EVENT_ID": "event-1",
        "CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256": canonical_sha256(payload),
    }
    for name, value in bindings.items():
        monkeypatch.setenv(name, value)

    loaded = load_source_manifest(manifest_path)
    original_anchor = payload["sources"]["video"]["anchor_text"]
    selected = select_docx_section(
        _document_with_attachment("视频"),
        loaded.data["sources"]["video"]["anchor_text"],
        {loaded.data["sources"]["video"]["anchor_text"]},
    )

    assert loaded.data["sources"]["video"]["anchor_text"] == original_anchor
    assert selected.anchor_text == original_anchor


@pytest.mark.parametrize(
    ("role", "anchor", "next_anchor", "start_text", "next_text"),
    [
        ("video", "二、视频", "(2) 剪辑意见", "视频", "剪辑意见"),
        ("review", "(2) 剪辑意见", "3.1 配音", "剪辑意见", "配音"),
        ("docx_audio", "3.1 配音", "二、视频", "配音", "视频"),
    ],
)
def test_number_equivalent_configured_anchor_closes_each_plain_label_section(
    role,
    anchor,
    next_anchor,
    start_text,
    next_text,
):
    document = {
        "blocks": [
            {"kind": "text", "text": start_text},
            {"kind": "text", "text": f"inside-{role}"},
            {"kind": "text", "text": next_text},
            {"kind": "text", "text": f"outside-{role}"},
        ]
    }

    selected = select_docx_section(document, anchor, {anchor, next_anchor})

    assert [row["text"] for row in selected.text_blocks] == [f"inside-{role}"]


@pytest.mark.parametrize("stage_id", ["initial", "first_review", "final_review"])
def test_materialization_applies_numbered_ranges_to_video_review_and_docx_audio(
    tmp_path,
    stage_id,
):
    content = "".join(
        [
            '<h1 id="materials">课程素材</h1>',
            '<h2 seq-marker="二、">视频</h2>',
            '<figure><source token="video-good" name="video-good.mp4" '
            'mime="video/mp4"/></figure>',
            '<h2 seq-marker="(2)">剪辑意见</h2>',
            '<checkbox id="review-good">00:01 校对视频</checkbox>',
            '<figure><source token="video-decoy" name="video-decoy.mp4" '
            'mime="video/mp4"/></figure>',
            '<h2 seq-marker="3.1">配音</h2>',
            '<checkbox id="review-decoy">00:02 不应进入剪辑意见</checkbox>',
            '<figure><source token="audio-good" name="audio-good.wav" '
            'mime="audio/wav"/></figure>',
            '<h1 id="next">下一章</h1>',
            '<figure><source token="audio-decoy" name="audio-decoy.wav" '
            'mime="audio/wav"/></figure>',
        ]
    )
    downloaded_tokens = []

    def runner(command):
        row = [str(value) for value in command]
        if row[1:] == ["whoami"]:
            return subprocess.CompletedProcess(
                row,
                0,
                json.dumps({"available": True, "identity": "user", "defaultAs": "user"}),
                "",
            )
        if row[1:3] == ["docs", "+fetch"]:
            response = {
                "ok": True,
                "identity": "user",
                "data": {
                    "document": {
                        "document_id": "doc-opaque",
                        "revision_id": 1,
                        "content": content,
                    }
                },
            }
            return subprocess.CompletedProcess(row, 0, json.dumps(response), "")
        if row[1:3] == ["docs", "+media-download"]:
            token = row[row.index("--token") + 1]
            downloaded_tokens.append(token)
            target = Path(row[row.index("--output") + 1])
            target.write_bytes(f"download:{token}".encode())
            return subprocess.CompletedProcess(row, 0, json.dumps({"ok": True}), "")
        return subprocess.CompletedProcess(row, 3, "", "unsupported")

    result = materialize_manifest_sources(
        _manifest(stage_id=stage_id),
        tmp_path / "job",
        runner,
        lark_cli=sys.executable,
    )

    assert downloaded_tokens == ["video-good", "audio-good"]
    assert [row["filename"] for row in result["videos"]] == ["video-good.mp4"]
    assert [row["source_text"] for row in result["review_items"]] == ["00:01 校对视频"]
    assert [row["filename"] for row in result["audios"]] == ["audio-good.wav"]
