import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from cli.jy_wrapper_parser import build_parser
from utils.review_job_compiler import compile_review_job
from utils.source_manifest import (
    SourceManifestError,
    _download_docx_attachment,
    canonical_sha256,
    compile_manifest_project,
    load_source_manifest,
    materialize_manifest_sources,
    select_docx_section,
    validate_source_pairs,
)


def valid_manifest() -> dict:
    return {
        "schema_version": 1,
        "binding": {
            "task_id": "task-1",
            "run_id": "run-1",
            "subject_key": "bas_demo:tbl_math",
            "config_version": 7,
            "stage_id": "initial",
            "event_id": "evt-1",
        },
        "record": {
            "base_token": "bas_demo",
            "table_id": "tbl_math",
            "record_id": "rec_1",
        },
        "document": {
            "field_id": "fld_document",
            "url": "https://guanghe.feishu.cn/docx/opaque-token",
        },
        "sources": {
            "video": {"kind": "docx_section", "anchor_text": "录屏"},
            "review": {"kind": "docx_section", "anchor_text": "修改意见"},
            "audio": {"mode": "video_original"},
        },
    }


def write_manifest(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "source-manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def media(name: str, duration: float = 10.0) -> dict:
    return {"path": name, "sha256": "a" * 64, "duration_seconds": duration}


def test_parser_accepts_source_manifest_as_an_exclusive_input():
    args = build_parser().parse_args(
        [
            "review-document-run",
            "--source-manifest",
            "source-manifest.json",
            "--job-root",
            "job",
            "--drafts-root",
            "drafts",
            "--package-zip",
            "out.zip",
        ]
    )
    assert args.source_manifest_json == "source-manifest.json"


def test_source_manifest_rejects_binding_or_digest_changes(tmp_path, monkeypatch):
    manifest_path = write_manifest(tmp_path, valid_manifest())
    monkeypatch.setenv("CODEX_AUTOCUT_TASK_ID", "task-1")
    monkeypatch.setenv("CODEX_AUTOCUT_RUN_ID", "run-1")
    monkeypatch.setenv("CODEX_AUTOCUT_SUBJECT_KEY", "bas_demo:tbl_math")
    monkeypatch.setenv("CODEX_AUTOCUT_CONFIG_VERSION", "7")
    monkeypatch.setenv("CODEX_AUTOCUT_STAGE_ID", "initial")
    monkeypatch.setenv("CODEX_AUTOCUT_EVENT_ID", "evt-1")
    monkeypatch.setenv(
        "CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256",
        canonical_sha256(valid_manifest()),
    )
    loaded = load_source_manifest(manifest_path)
    assert loaded.data["binding"]["stage_id"] == "initial"
    assert loaded.canonical_sha256 == canonical_sha256(valid_manifest())

    changed = valid_manifest()
    changed["binding"]["run_id"] = "another-run"
    assert canonical_sha256(changed) != loaded.canonical_sha256


def test_source_manifest_matches_taskboard_hash_for_small_tolerance(tmp_path, monkeypatch):
    payload = valid_manifest()
    payload["sources"]["audio"] = {
        "mode": "replace_original",
        "duration_tolerance_seconds": 1e-7,
        "source": {"kind": "docx_section", "anchor_text": "配音"},
    }
    manifest_path = write_manifest(tmp_path, payload)
    monkeypatch.setenv("CODEX_AUTOCUT_TASK_ID", "task-1")
    monkeypatch.setenv("CODEX_AUTOCUT_RUN_ID", "run-1")
    monkeypatch.setenv("CODEX_AUTOCUT_SUBJECT_KEY", "bas_demo:tbl_math")
    monkeypatch.setenv("CODEX_AUTOCUT_CONFIG_VERSION", "7")
    monkeypatch.setenv("CODEX_AUTOCUT_STAGE_ID", "initial")
    monkeypatch.setenv("CODEX_AUTOCUT_EVENT_ID", "evt-1")
    # SHA-256 from Taskboard's canonicalSourceManifestJson for this payload.
    monkeypatch.setenv(
        "CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256",
        "bfd2e0f93e83d761dc6963e469bf4da23549f42ec4a81fcc3957ea083f2fc57d",
    )

    loaded = load_source_manifest(manifest_path)

    assert loaded.canonical_sha256 == "bfd2e0f93e83d761dc6963e469bf4da23549f42ec4a81fcc3957ea083f2fc57d"


def test_source_manifest_matches_taskboard_hash_for_lone_utf16_surrogate(tmp_path, monkeypatch):
    payload = valid_manifest()
    payload["sources"]["video"]["anchor_text"] = "\ud800"
    manifest_path = tmp_path / "source-manifest.json"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    monkeypatch.setenv("CODEX_AUTOCUT_TASK_ID", "task-1")
    monkeypatch.setenv("CODEX_AUTOCUT_RUN_ID", "run-1")
    monkeypatch.setenv("CODEX_AUTOCUT_SUBJECT_KEY", "bas_demo:tbl_math")
    monkeypatch.setenv("CODEX_AUTOCUT_CONFIG_VERSION", "7")
    monkeypatch.setenv("CODEX_AUTOCUT_STAGE_ID", "initial")
    monkeypatch.setenv("CODEX_AUTOCUT_EVENT_ID", "evt-1")
    # SHA-256 from Taskboard's JSON.stringify-compatible canonical output.
    monkeypatch.setenv(
        "CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256",
        "438332534498c096b428667688a5ef04b66abc6d3eab0561b10ec55e446c388b",
    )

    loaded = load_source_manifest(manifest_path)

    assert loaded.data["sources"]["video"]["anchor_text"] == "\ud800"
    assert loaded.canonical_sha256 == "438332534498c096b428667688a5ef04b66abc6d3eab0561b10ec55e446c388b"


def test_source_manifest_uses_taskboard_trim_semantics(tmp_path, monkeypatch):
    payload = valid_manifest()
    payload["sources"]["video"]["anchor_text"] = "录屏\x1f"
    manifest_path = write_manifest(tmp_path, payload)
    monkeypatch.setenv("CODEX_AUTOCUT_TASK_ID", "task-1")
    monkeypatch.setenv("CODEX_AUTOCUT_RUN_ID", "run-1")
    monkeypatch.setenv("CODEX_AUTOCUT_SUBJECT_KEY", "bas_demo:tbl_math")
    monkeypatch.setenv("CODEX_AUTOCUT_CONFIG_VERSION", "7")
    monkeypatch.setenv("CODEX_AUTOCUT_STAGE_ID", "initial")
    monkeypatch.setenv("CODEX_AUTOCUT_EVENT_ID", "evt-1")
    # SHA-256 from Taskboard, where String.prototype.trim() preserves U+001F.
    monkeypatch.setenv(
        "CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256",
        "8b413fc9a9346eda75eb04e4414f7b0783c6e98ca0044f2424398a294930281f",
    )

    loaded = load_source_manifest(manifest_path)

    assert loaded.data["sources"]["video"]["anchor_text"] == "录屏\x1f"
    assert loaded.canonical_sha256 == "8b413fc9a9346eda75eb04e4414f7b0783c6e98ca0044f2424398a294930281f"


def test_source_manifest_strips_taskboard_bom_whitespace(tmp_path, monkeypatch):
    payload = valid_manifest()
    payload["sources"]["video"]["anchor_text"] = "\ufeff录屏"
    manifest_path = write_manifest(tmp_path, payload)
    monkeypatch.setenv("CODEX_AUTOCUT_TASK_ID", "task-1")
    monkeypatch.setenv("CODEX_AUTOCUT_RUN_ID", "run-1")
    monkeypatch.setenv("CODEX_AUTOCUT_SUBJECT_KEY", "bas_demo:tbl_math")
    monkeypatch.setenv("CODEX_AUTOCUT_CONFIG_VERSION", "7")
    monkeypatch.setenv("CODEX_AUTOCUT_STAGE_ID", "initial")
    monkeypatch.setenv("CODEX_AUTOCUT_EVENT_ID", "evt-1")
    # Taskboard's String.prototype.trim() removes a leading U+FEFF.
    monkeypatch.setenv(
        "CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256",
        "e95e8c2fdd2ee9836751d246232260047458bbbdd04711868fbc30f2b40bba55",
    )

    loaded = load_source_manifest(manifest_path)

    assert loaded.data["sources"]["video"]["anchor_text"] == "录屏"
    assert loaded.canonical_sha256 == "e95e8c2fdd2ee9836751d246232260047458bbbdd04711868fbc30f2b40bba55"


def test_source_manifest_accepts_feishu_wiki_document_url(tmp_path, monkeypatch):
    payload = valid_manifest()
    payload["document"]["url"] = "https://guanghe.feishu.cn/wiki/opaque-wiki-token"
    manifest_path = write_manifest(tmp_path, payload)
    monkeypatch.setenv("CODEX_AUTOCUT_TASK_ID", "task-1")
    monkeypatch.setenv("CODEX_AUTOCUT_RUN_ID", "run-1")
    monkeypatch.setenv("CODEX_AUTOCUT_SUBJECT_KEY", "bas_demo:tbl_math")
    monkeypatch.setenv("CODEX_AUTOCUT_CONFIG_VERSION", "7")
    monkeypatch.setenv("CODEX_AUTOCUT_STAGE_ID", "initial")
    monkeypatch.setenv("CODEX_AUTOCUT_EVENT_ID", "evt-1")
    monkeypatch.setenv("CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256", canonical_sha256(payload))

    loaded = load_source_manifest(manifest_path)

    assert loaded.data["document"]["url"] == payload["document"]["url"]


@pytest.mark.parametrize(
    "url",
    [
        "https://guanghe.feishu.cn/evil/wiki/token",
        "https://guanghe.feishu.cn/wiki/token/extra",
        "https://guanghe.feishu.cn/docx/token/extra",
        "https://guanghe.feishu.cn/wiki/token?share=copy",
        "https://guanghe.feishu.cn/wiki/token#heading",
        "https://operator@guanghe.feishu.cn/wiki/token",
        "https://guanghe.feishu.cn/wiki/",
        "https://guanghe.feishu.cn/wiki//token",
    ],
)
def test_source_manifest_rejects_noncanonical_document_route(tmp_path, url):
    payload = valid_manifest()
    payload["document"]["url"] = url

    with pytest.raises(SourceManifestError, match="source_manifest_invalid"):
        load_source_manifest(write_manifest(tmp_path, payload))


def test_heading_anchor_stops_before_the_next_configured_label():
    document = {
        "blocks": [
            {"kind": "heading", "level": 2, "text": "录屏"},
            {"kind": "attachment", "filename": "video.mp4", "mime": "video/mp4"},
            {"kind": "text", "text": "录音"},
            {"kind": "attachment", "filename": "voice.wav", "mime": "audio/wav"},
        ]
    }
    selected = select_docx_section(document, "录屏", {"录屏", "录音"})
    assert [item["filename"] for item in selected.attachments] == ["video.mp4"]


def test_docx_download_stages_cli_output_before_publishing_to_job_root(tmp_path, monkeypatch):
    cli = tmp_path / "fake_lark_cli.py"
    log_path = tmp_path / "media-download.json"
    cli.write_text(
        """
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["whoami"]:
    print(json.dumps({"available": True, "identity": "user", "defaultAs": "user"}))
    raise SystemExit(0)
if args[:2] == ["docs", "+media-download"]:
    output = args[args.index("--output") + 1]
    Path(os.environ["FAKE_LARK_LOG"]).write_text(
        json.dumps({"cwd": str(Path.cwd()), "output": output}), encoding="utf-8"
    )
    if Path(output).is_absolute():
        print(json.dumps({"ok": False, "error": {"subtype": "invalid_argument"}}), file=sys.stderr)
        raise SystemExit(2)
    target = Path.cwd() / output
    target.write_bytes(b"history-video")
    print(json.dumps({"ok": True, "identity": "user"}))
    raise SystemExit(0)
raise SystemExit(3)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_LARK_LOG", str(log_path))
    destination = tmp_path / "run" / "manifest-materials" / "video"

    receipt = _download_docx_attachment(
        {"token": "opaque-token", "filename": "lesson.mp4", "mime": "video/mp4"},
        destination,
        executable=(sys.executable, str(cli)),
        runner=None,
    )

    target = destination / "lesson.mp4"
    invocation = json.loads(log_path.read_text(encoding="utf-8"))
    assert Path(receipt["path"]) == target.resolve()
    assert target.read_bytes() == b"history-video"
    assert not Path(invocation["output"]).is_absolute()
    assert Path(invocation["cwd"]) != destination
    assert not Path(invocation["cwd"]).exists()


def test_mixed_docx_section_normalizes_checkbox_reviews(tmp_path):
    payload = valid_manifest()
    payload["sources"]["video"]["anchor_text"] = "二、PPT定稿+翻录"
    payload["sources"]["review"]["anchor_text"] = "二、PPT定稿+翻录"
    content = "".join(
        [
            '<title id="title">History lesson</title>',
            '<h1 id="heading">二、PPT定稿+翻录</h1>',
            '<figure id="slides"><source token="slides-token" name="lesson.pptx" '
            'mime="application/vnd.openxmlformats-officedocument.presentationml.presentation"/></figure>',
            '<figure id="video"><source token="video-token" name="lesson.mp4" '
            'mime="video/mp4"/></figure>',
            '<p>剪辑需求：</p>',
            '<checkbox id="review">00:01 删除“是吧”</checkbox>',
        ]
    )
    downloaded = []

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
                        "document_id": "doc-token",
                        "revision_id": 1,
                        "content": content,
                    }
                },
            }
            return subprocess.CompletedProcess(row, 0, json.dumps(response), "")
        if row[1:3] == ["docs", "+media-download"]:
            token = row[row.index("--token") + 1]
            downloaded.append(token)
            target = Path(row[row.index("--output") + 1])
            target.write_bytes(f"download:{token}".encode())
            return subprocess.CompletedProcess(row, 0, json.dumps({"ok": True}), "")
        return subprocess.CompletedProcess(row, 3, "", "unsupported")

    result = materialize_manifest_sources(
        payload,
        tmp_path / "job",
        runner,
        lark_cli=sys.executable,
    )

    assert downloaded == ["slides-token", "video-token"]
    assert [row["filename"] for row in result["videos"]] == ["lesson.mp4"]
    assert result["audios"] == []
    assert [row["source_text"] for row in result["review_items"]] == [
        "00:01 删除“是吧”"
    ]
    assert "kind" not in result["review_items"][0]

    compiled = compile_review_job(
        {
            "document": {"id": "history-lesson", "revision": "r1"},
            "review_items": result["review_items"],
        },
        {
            "draft_name": "History lesson",
            "source_video": result["videos"][0]["path"],
            "workflow_mode": "lite",
            "lite_cut_layout": "split_gap",
        },
        tmp_path / "compiled",
    )
    review_items = json.loads(Path(compiled["doc_items"]).read_text(encoding="utf-8"))[
        "review_items"
    ]
    assert len(review_items) == 1
    assert review_items[0]["kind"] == "phrase_delete"
    assert review_items[0]["execution_required"] is True
    assert review_items[0]["evidence"]["timing_source"] == "asr"


def test_manifest_media_pairs_keep_document_order():
    project = compile_manifest_project(
        videos=[media("v2.mp4"), media("v1.mp4")],
        audios=[media("a2.wav"), media("a1.wav")],
        mode="replace_original",
        tolerance_seconds=3,
    )
    assert [
        (Path(row["video_path"]).name, Path(row["replacement_audio_path"]).name)
        for row in project["source_pairs"]
    ] == [("v2.mp4", "a2.wav"), ("v1.mp4", "a1.wav")]


def test_pair_count_and_duration_mismatch_block():
    with pytest.raises(SourceManifestError, match="media_count_mismatch"):
        compile_manifest_project(
            videos=[media("v1.mp4"), media("v2.mp4")],
            audios=[media("a1.wav")],
            mode="replace_original",
        )
    project = compile_manifest_project(
        videos=[media("v1.mp4", 10.0)],
        audios=[media("a1.wav", 14.1)],
        mode="replace_original",
        tolerance_seconds=3,
    )
    with pytest.raises(SourceManifestError, match="media_duration_mismatch"):
        validate_source_pairs(project, 3.0, lambda path: {"duration_seconds": 14.1 if "a1" in path else 10.0})
