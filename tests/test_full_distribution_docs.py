from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_release_version_is_consistent() -> None:
    assert _read("VERSION").strip() == "1.7.0"
    pyproject = _read("pyproject.toml")
    assert re.search(r'^version\s*=\s*"1\.7\.0"$', pyproject, flags=re.MULTILINE)
    manifest = json.loads(_read("capability-manifest.json"))
    contract = json.loads(_read("runtime-capability-contract.json"))
    assert manifest["release_version"] == "1.7.0"
    assert contract["release_version"] == "1.7.0"


def test_release_docs_record_the_safe_prior_release_identification() -> None:
    release_record = _read("CHANGELOG.md")

    for value in (
        "Auto-Cut-v1.6.1-windows-x64.zip",
        "7,372,704",
        "66a88f25476a206c0bf83162b9d4996b4868b830bbbe38edb1c6cecbdb5d0251",
        "6269f1f76ed6f153d44f3dacfb6d8722a5e6e0b5",
        "1a9e2e75f65f95ada32f3506c48bce29eb1f4baaec4c72e8a8f864f72348bcad",
        "formal acceptance is unproven",
    ):
        assert value in release_record


def test_readme_and_quickstart_lead_with_unified_windows_setup() -> None:
    for path in ("README.md", "docs/quickstart.md", "docs/release-full-install.md"):
        text = _read(path)
        assert "powershell -ExecutionPolicy Bypass -File .\\setup.ps1" in text
        assert "Python 3.10-3.12" in text
        assert "16" in text and "skill" in text.lower()
        assert ".venv-audio" in text
        assert "Playwright Chromium" in text


def test_user_start_docs_require_a_discoverable_python_3_11_for_audio() -> None:
    for path in ("README.md", "docs/quickstart.md", "docs/release-full-install.md"):
        assert "Python 3.11 must also be installed" in " ".join(_read(path).split())


def test_release_docs_explain_machine_owned_first_use_steps() -> None:
    combined = _read("README.md") + _read("docs/release-full-install.md")
    for command in (
        ".\\.venv\\Scripts\\python.exe scripts/auto_cut_first_run.py guide --json",
        ".\\.venv\\Scripts\\python.exe scripts/auto_cut_first_run.py favorites-sync --json",
        ".\\.venv\\Scripts\\python.exe scripts/auto_cut_first_run.py pointer-guide --json",
        ".\\.venv\\Scripts\\python.exe scripts/auto_cut_first_run.py feishu-status --json",
    ):
        assert command in combined
    for concept in (
        "target machine",
        "explicit binding confirmation",
        "authorization",
        "reference images",
    ):
        assert concept in combined.lower()


def test_release_documents_describe_every_manifest_capability() -> None:
    manifest = json.loads(_read("capability-manifest.json"))
    combined = _read("README.md") + _read("docs/release-full-install.md")

    for capability in manifest["capabilities"]:
        assert capability["id"] in combined


def test_carnac_verification_uses_machine_readable_first_run_status() -> None:
    manifest = json.loads(_read("capability-manifest.json"))
    contract = json.loads(_read("runtime-capability-contract.json"))
    expected = r".\.venv\Scripts\python.exe scripts/auto_cut_first_run.py status --json"

    manifest_row = next(row for row in manifest["capabilities"] if row["id"] == "carnac_overlay")
    contract_row = next(row for row in contract["capabilities"] if row["id"] == "carnac_overlay")

    assert manifest_row["verification_command"] == expected
    assert contract_row["verification_command"] == expected


def test_release_docs_expose_status_volc_and_tts_first_use_commands() -> None:
    combined = (
        _read("README.md")
        + _read("docs/quickstart.md")
        + _read("docs/release-full-install.md")
        + _read("docs/api.md")
    )
    for command in (
        ".\\.venv\\Scripts\\python.exe scripts/auto_cut_first_run.py status --json",
        ".\\.venv\\Scripts\\python.exe scripts/auto_cut_first_run.py volc-config",
        ".\\.venv\\Scripts\\python.exe scripts/auto_cut_first_run.py volc-status --json",
        ".\\.venv\\Scripts\\python.exe scripts/auto_cut_first_run.py tts-status --json",
        ".\\.venv\\Scripts\\python.exe scripts/audio/volc_word_align.py",
    ):
        assert command in combined
    for evidence_field in (
        "input_sha256",
        "service_job_id",
        "service_result_sha256",
        "resource_id",
        "adapter_version",
    ):
        assert evidence_field in combined


def test_user_start_docs_route_volc_setup_through_the_official_guide() -> None:
    command = ".\\.venv\\Scripts\\python.exe scripts/auto_cut_first_run.py volc-guide --json"

    for path in ("README.md", "docs/quickstart.md", "docs/release-full-install.md"):
        assert command in _read(path)


def test_first_use_docs_and_machine_commands_use_installed_venv() -> None:
    paths = (
        "README.md",
        "docs/quickstart.md",
        "docs/release-full-install.md",
        "docs/api.md",
        "docs/audio-sound/README.md",
        "skills/auto-cut-review-audio-precision/SKILL.md",
        "skills/auto-cut-review-audio-precision/references/workflow.md",
    )
    for path in paths:
        text = _read(path)
        assert "python scripts/auto_cut_first_run.py" not in text
        assert "python scripts/audio/volc_word_align.py" not in text

    for contract_path in (
        "capability-manifest.json",
        "runtime-capability-contract.json",
    ):
        contract = json.loads(_read(contract_path))
        for capability in contract["capabilities"]:
            command = capability["verification_command"]
            if "scripts/auto_cut_first_run.py" in command:
                assert command.startswith(".\\.venv\\Scripts\\python.exe ")


def test_first_use_status_is_not_documented_as_full_install_report() -> None:
    combined = (
        _read("README.md")
        + _read("docs/quickstart.md")
        + _read("docs/release-full-install.md")
        + _read("docs/api.md")
    ).lower()

    assert "tmp/install/install-report.json" in combined
    assert "does not replace" in combined
    assert "first-use status" in combined


def test_audio_precision_skill_uses_bundled_volc_adapter_without_weakening_gates() -> None:
    skill = _read("skills/auto-cut-review-audio-precision/SKILL.md")
    workflow = _read("skills/auto-cut-review-audio-precision/references/workflow.md")
    combined = skill + workflow

    assert "does not contain a built-in `volc_word_align.py`" not in combined
    assert "scripts/audio/volc_word_align.py" in combined
    for phrase in (
        "input_sha256",
        "service_job_id",
        "service_result_sha256",
        "resource_id",
        "adapter_version",
        "must_keep",
        "physical segment removal",
        "full-candidate reverse ASR",
        "semantic_pause_adjustment",
        "visual_hold_review",
        "editable",
    ):
        assert phrase in combined


def test_release_docs_report_truthful_audio_boundary() -> None:
    combined = _read("docs/release-full-install.md") + _read("docs/audio-sound/README.md")
    assert "SpectraMini-style" in combined
    assert "DeepFilterNet" in combined
    assert "Respiro" in combined
    assert "degraded" in combined
    assert "license" in combined.lower()
    assert "SHA-256" in combined
    assert "--skip-deepfilternet" in combined
    assert "unavailable" in combined


def test_release_docs_preserve_editable_draft_contract_and_no_git_acceptance() -> None:
    combined = _read("README.md") + _read("docs/release-full-install.md")
    for phrase in (
        "source video",
        "separated audio",
        "visible cut",
        "review marker",
        "clean extraction",
        "without .git",
        "SHA-256",
    ):
        assert phrase in combined


def test_target_install_documents_offline_and_private_restore_contract() -> None:
    target = _read("TARGET_INSTALL.md")
    for phrase in (
        "--offline-bundle",
        "--no-index",
        "CPython 3.11",
        "Chromium",
        "FFmpeg",
        "draft-root-check",
        "senior-high-history",
        "profile_registry.py register",
        "profile_registry.py check",
        "profile_registry.py validate",
        "explicitly confirms",
        "DeepFilterNet",
        "Respiro",
        "Volcengine",
        "Edge TTS",
        "SAMI TTS",
        "Codex",
    ):
        assert phrase in target
    assert "project-bindings.json" in target
    assert "new empty registry" in target
    assert "subject-pointer-profiles.restored-history" in target


def test_target_install_blocks_on_jianying_version_mismatch() -> None:
    target = _read("TARGET_INSTALL.md")

    assert "data.environment.app_version" in target
    assert "$expectedJianYingVersion" in target
    assert "JianYing version mismatch" in target
    assert "must equal" in target
    assert "Prefer the same JianYing version" not in target
    assert "--root data/subject-pointer-profiles.local" not in target
    assert "does not fall back to the network" in target


def test_release_docs_keep_online_and_offline_install_modes_explicit() -> None:
    combined = (
        _read("README.md")
        + _read("docs/quickstart.md")
        + _read("docs/release-full-install.md")
        + _read("TARGET_INSTALL.md")
    )
    assert "omit `--offline-bundle`" in combined
    assert "normal online" in combined.lower()
    assert "optional when the target" in combined.lower()
    assert "does not require windows appcontainer" in " ".join(combined.split()).lower()


def test_changelog_records_current_offline_distribution_release() -> None:
    changelog = _read("CHANGELOG.md")
    assert "1.7.0" in changelog
    assert "full Windows installer" in changelog
    assert "capability-manifest.json" in changelog
    assert "offline dependency" in changelog
    assert "record_video_dir" in changelog
    assert "revision 1011" in changelog
    assert "reproducibly built minimal FFmpeg/FFprobe 8.1.2" in changelog
    assert "source tag `n8.1.2`" in changelog
    assert "tmp/offline-runtime" in changelog
    assert "new empty isolated registry" in changelog


def test_release_docs_describe_persisted_runtime_discovery_and_recording_probe() -> None:
    for path in (
        "README.md",
        "docs/quickstart.md",
        "docs/release-full-install.md",
        "TARGET_INSTALL.md",
    ):
        text = _read(path)
        assert "record_video_dir" in text
        assert "revision 1011" in text
        assert "tmp/offline-runtime/browsers" in text
        assert "tmp/offline-runtime/tools/ffmpeg" in text

    combined = (
        _read("CHANGELOG.md")
        + _read("README.md")
        + _read("docs/release-full-install.md")
        + _read("TARGET_INSTALL.md")
    )
    assert "WinLDD revision 1007" in combined
    assert "MIT and Apache-2.0" in combined

    full_install = _read("docs/release-full-install.md")
    assert "committed, reproducibly built minimal FFmpeg/FFprobe 8.1.2" in full_install
    assert "source tag `n8.1.2`" in full_install
