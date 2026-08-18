from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "docs" / "audio-sound" / "source-migration-manifest.json"
_CLEAN_SOURCE_ROOT_VALUE = os.environ.get("AUDIO_SOUND_CLEAN_SOURCE_ROOT")
CLEAN_SOURCE_ROOT = Path(_CLEAN_SOURCE_ROOT_VALUE) if _CLEAN_SOURCE_ROOT_VALUE else None

EXPECTED_SOURCE_PATHS = {
    ".codex/skills/audio-sound/SKILL.md",
    ".codex/skills/audio-sound/agents/openai.yaml",
    ".codex/skills/audio-sound/references/acceptance.md",
    ".codex/skills/audio-sound/references/commands.md",
    ".codex/skills/audio-sound/references/spoken-segment-boundaries.md",
    ".codex/skills/audio-sound/references/workflow.md",
    ".env.example",
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "RELEASE_QUICK_START.md",
    "audio_sound/__init__.py",
    "audio_sound/bootstrap.py",
    "audio_sound/cli.py",
    "audio_sound/config.py",
    "audio_sound/pipeline.py",
    "audio_sound/segment_removal.py",
    "audio_sound/skill_workflow.py",
    "check_runtime.cmd",
    "doctor.cmd",
    "docs/architecture.md",
    "docs/assets/before-after-waveform.svg",
    "docs/assets/spectrum-before-after.jpg",
    "docs/reference-sop.md",
    "docs/superpowers/plans/2026-05-27-audio-sound-standalone.md",
    "docs/tuning-guide.md",
    "presets/fast.json",
    "presets/final.json",
    "presets/repair-soft.json",
    "presets/repair.json",
    "presets/review.json",
    "presets/safe.json",
    "presets/voice-isolate.json",
    "pyproject.toml",
    "requirements.txt",
    "run_audio_workflow.cmd",
    "scripts/audio_cleanup.py",
    "scripts/audio_skill_workflow.py",
    "scripts/exact_window_cleanup.py",
    "scripts/narrow_onset_cleanup.py",
    "scripts/remove_spoken_segments.py",
    "setup.cmd",
    "tests/__init__.py",
    "tests/fixture_respiro.wav",
    "tests/test_bootstrap.py",
    "tests/test_cli.py",
    "tests/test_config.py",
    "tests/test_pipeline.py",
    "tests/test_segment_removal.py",
    "tests/test_skill_workflow.py",
}

EXPECTED_MANIFEST_ITEM_KEYS = {
    "source_path",
    "source_size",
    "source_sha256",
    "normalization",
    "normalized_sha256",
    "source_git_blob_sha1",
    "treatment",
    "destinations",
    "normalized_content_destination",
}


def _load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _normalize(data: bytes, mode: str) -> bytes:
    if mode == "crlf_to_lf":
        return data.replace(b"\r\n", b"\n")
    if mode == "none":
        return data
    raise AssertionError(f"Unknown normalization mode: {mode}")


def _tree_fingerprint(items: list[dict[str, object]]) -> str:
    rows = sorted(
        f"{item['source_path']}\t{item['source_size']}\t{item['source_sha256']}" for item in items
    )
    return _sha256_bytes("\n".join(rows).encode("utf-8"))


def _repository_mode(package_root: Path) -> str:
    return "git" if (package_root / ".git").exists() else "archive"


def _reachable_git_objects() -> set[str]:
    result = subprocess.run(
        ["git", "rev-list", "--objects", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return {line.split(" ", 1)[0] for line in result.stdout.splitlines()}


def _tracked_paths() -> set[str]:
    if _repository_mode(REPO_ROOT) == "archive":
        inventory_path = REPO_ROOT / "release-inventory.json"
        if inventory_path.is_file():
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            return {item["path"] for item in inventory["files"]}
        return {
            path.relative_to(REPO_ROOT).as_posix()
            for path in REPO_ROOT.rglob("*")
            if path.is_file()
        }

    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return {line.replace("\\", "/") for line in result.stdout.splitlines()}


def _read_reachable_git_blob(object_id: str, reachable: set[str]) -> bytes:
    assert object_id in reachable, object_id
    object_type = subprocess.run(
        ["git", "cat-file", "-t", object_id],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert object_type.stdout.strip() == "blob", object_id
    blob = subprocess.run(
        ["git", "cat-file", "blob", object_id],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return blob.stdout


def _assert_manifest_item_schema(item: dict[str, object], tracked: set[str]) -> None:
    assert set(item) == EXPECTED_MANIFEST_ITEM_KEYS
    assert item["treatment"] in {"copied", "adapted", "merged"}

    destinations = item["destinations"]
    assert isinstance(destinations, list)
    assert destinations
    assert all(
        isinstance(destination, str) and destination in tracked for destination in destinations
    )

    normalized_destination = item["normalized_content_destination"]
    assert normalized_destination is None or isinstance(normalized_destination, str)
    if normalized_destination:
        assert normalized_destination in destinations
        assert normalized_destination in tracked


def test_source_manifest_has_complete_unique_inventory() -> None:
    manifest = _load_manifest()
    items = manifest["items"]
    tracked = _tracked_paths()

    assert manifest["schema_version"] == 2
    assert manifest["source_root_label"] == "Audio-sound-release-clean-v0.1.0"
    assert manifest["source_directory_version_hint"] == "0.1.0"
    assert manifest["source_declared_package_version"] == "0.2.0"
    assert (
        manifest["source_version_status"] == "directory_hint_differs_from_declared_package_version"
    )
    assert manifest["source_file_count"] == 50
    assert manifest["source_total_bytes"] == 1_255_475
    assert (
        manifest["source_tree_sha256"]
        == "493d4f7cfedf1206179c87c8e8bc2007032e2c5f7ef1799e2984790961758cc5"
    )
    assert (
        manifest["source_tree_fingerprint_format"]
        == "sorted by POSIX source_path using Unicode code point order; path<TAB>size<TAB>lowercase-sha256 records joined with LF as UTF-8 without BOM"
    )
    assert isinstance(items, list)
    assert len(items) == 50
    for item in items:
        _assert_manifest_item_schema(item, tracked)
    assert sum(bool(item["normalized_content_destination"]) for item in items) == 21
    assert {item["source_path"] for item in items} == EXPECTED_SOURCE_PATHS
    assert {item["source_path"] for item in items if item["normalization"] == "none"} == {
        "docs/assets/spectrum-before-after.jpg",
        "tests/fixture_respiro.wav",
    }
    assert sum(item["normalization"] == "crlf_to_lf" for item in items) == 48
    assert _tree_fingerprint(items) == manifest["source_tree_sha256"]


def test_manifest_item_schema_rejects_a_missing_required_key() -> None:
    tracked = _tracked_paths()
    item = dict(_load_manifest()["items"][0])
    item.pop("normalized_content_destination")

    with pytest.raises(AssertionError):
        _assert_manifest_item_schema(item, tracked)


def test_manifest_item_schema_rejects_an_unexpected_key() -> None:
    tracked = _tracked_paths()
    item = dict(_load_manifest()["items"][0])
    item["unexpected"] = "not part of schema v2"

    with pytest.raises(AssertionError):
        _assert_manifest_item_schema(item, tracked)


def test_manifest_item_schema_rejects_an_unknown_treatment() -> None:
    tracked = _tracked_paths()
    item = dict(_load_manifest()["items"][0])
    item["treatment"] = "repackaged"

    with pytest.raises(AssertionError):
        _assert_manifest_item_schema(item, tracked)


def test_manifest_item_schema_rejects_mapping_outside_destinations() -> None:
    tracked = _tracked_paths()
    item = dict(_load_manifest()["items"][0])
    item["normalized_content_destination"] = "README.md"
    assert "README.md" in tracked
    assert "README.md" not in item["destinations"]

    with pytest.raises(AssertionError):
        _assert_manifest_item_schema(item, tracked)


def test_manifest_item_schema_rejects_an_untracked_mapping() -> None:
    tracked = _tracked_paths()
    item = dict(_load_manifest()["items"][0])
    untracked = "tmp/not-tracked-normalized-content.txt"
    item["destinations"] = [*item["destinations"], untracked]
    item["normalized_content_destination"] = untracked
    assert untracked not in tracked

    with pytest.raises(AssertionError):
        _assert_manifest_item_schema(item, tracked)


def test_manifest_matches_clean_source_tree_when_available() -> None:
    if CLEAN_SOURCE_ROOT is None or not CLEAN_SOURCE_ROOT.is_dir():
        pytest.skip(f"Clean source is unavailable: {CLEAN_SOURCE_ROOT}")

    manifest = _load_manifest()
    items = manifest["items"]
    actual_paths = {
        path.relative_to(CLEAN_SOURCE_ROOT).as_posix()
        for path in CLEAN_SOURCE_ROOT.rglob("*")
        if path.is_file()
    }
    assert actual_paths == EXPECTED_SOURCE_PATHS

    total_bytes = 0
    for item in items:
        source_path = CLEAN_SOURCE_ROOT / item["source_path"]
        raw = source_path.read_bytes()
        total_bytes += len(raw)
        assert len(raw) == item["source_size"]
        assert _sha256_bytes(raw) == item["source_sha256"]
        normalized = _normalize(raw, item["normalization"])
        assert _sha256_bytes(normalized) == item["normalized_sha256"]
        assert _git_blob_sha1(normalized) == item["source_git_blob_sha1"]

    assert total_bytes == manifest["source_total_bytes"]


def test_every_source_item_has_a_tracked_destination() -> None:
    tracked = _tracked_paths()
    for item in _load_manifest()["items"]:
        assert item["destinations"], item["source_path"]
        for destination in item["destinations"]:
            assert (REPO_ROOT / destination).is_file(), destination
            assert destination in tracked, destination


def test_normalized_content_destinations_match_recorded_hash() -> None:
    for item in _load_manifest()["items"]:
        destination = item.get("normalized_content_destination")
        if not destination:
            continue
        data = (REPO_ROOT / destination).read_bytes()
        normalized = _normalize(data, item["normalization"])
        assert _sha256_bytes(normalized) == item["normalized_sha256"], item["source_path"]


@pytest.mark.skipif(
    _repository_mode(REPO_ROOT) == "archive",
    reason="Git history provenance requires package-local .git metadata",
)
def test_every_normalized_source_blob_is_reachable_in_git_history() -> None:
    reachable = _reachable_git_objects()
    for item in _load_manifest()["items"]:
        data = _read_reachable_git_blob(item["source_git_blob_sha1"], reachable)
        assert _git_blob_sha1(data) == item["source_git_blob_sha1"], item["source_path"]
        assert _sha256_bytes(data) == item["normalized_sha256"], item["source_path"]


@pytest.mark.skipif(
    _repository_mode(REPO_ROOT) == "archive",
    reason="Git history provenance requires package-local .git metadata",
)
def test_read_reachable_git_blob_rejects_a_reachable_non_blob() -> None:
    reachable = _reachable_git_objects()
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    head_commit = result.stdout.strip()
    assert head_commit in reachable

    with pytest.raises(AssertionError):
        _read_reachable_git_blob(head_commit, reachable)


def test_retained_legacy_license_is_separate_and_traceable() -> None:
    manifest = _load_manifest()
    artifacts = manifest["retained_legacy_artifacts"]
    assert len(artifacts) == 1
    license_item = artifacts[0]
    assert license_item == {
        "source_root_label": "Audio-sound-release-main",
        "source_path": "LICENSE",
        "source_size": 1064,
        "source_sha256": "6cd18efdd78725b67e1623278d111f69a11e01aee50d5957785bf2695e940d89",
        "source_git_blob_sha1": "0caf2237e493041ac89ed22b3a2ec4a75668f79e",
        "destination": "docs/audio-sound/LICENSE.audio-sound",
        "reason": "MIT license retained from prior source package for legal provenance",
    }
    destination = REPO_ROOT / license_item["destination"]
    normalized = destination.read_bytes().replace(b"\r\n", b"\n")
    assert license_item["destination"] in _tracked_paths()
    assert _sha256_bytes(normalized) == license_item["source_sha256"]
    assert "LICENSE" not in {item["source_path"] for item in manifest["items"]}


@pytest.mark.skipif(
    _repository_mode(REPO_ROOT) == "archive",
    reason="Git history provenance requires package-local .git metadata",
)
def test_retained_legacy_license_blob_is_reachable_in_git_history() -> None:
    manifest = _load_manifest()
    license_item = manifest["retained_legacy_artifacts"][0]
    blob = _read_reachable_git_blob(license_item["source_git_blob_sha1"], _reachable_git_objects())
    assert _git_blob_sha1(blob) == license_item["source_git_blob_sha1"]
    assert _sha256_bytes(blob) == license_item["source_sha256"]


def test_current_docs_describe_the_clean_source_receipt() -> None:
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "docs" / "audio-sound" / "README.md").read_text(encoding="utf-8")
    architecture = (REPO_ROOT / "docs" / "audio-sound" / "architecture.md").read_text(
        encoding="utf-8"
    )
    provenance = (REPO_ROOT / "docs" / "audio-sound" / "PROVENANCE.md").read_text(encoding="utf-8")

    assert "canonical 51-item" not in agents
    assert "51 项来源清单" not in root_readme
    assert "all 51 files" not in readme
    assert "all 51 upstream files" not in architecture
    assert "50 raw source files" in agents
    assert "50 项来源清单" in root_readme
    assert "all 50 files" in readme
    assert "all 50 files" in architecture
    assert "Audio-sound-release-clean-v0.1.0" in provenance
    assert "CRLF" in provenance
    assert "0.1.0" in provenance and "0.2.0" in provenance
    assert "LICENSE.audio-sound" in provenance
