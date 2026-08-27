from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.release import build_lite_plugin

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "auto-cut-lite"
SKILLS_ROOT = PLUGIN_ROOT / "workspace-payload" / "skills"

EXPECTED_SKILLS = {
    "auto-cut",
    "auto-cut-animation-timing-revision",
    "auto-cut-audio-peak-target",
    "auto-cut-audio-restoration",
    "auto-cut-basic-oral-video",
    "auto-cut-draft-retention",
    "auto-cut-editable-ad-revision",
    "auto-cut-favorite-text-assets",
    "auto-cut-final-acceptance",
    "auto-cut-lite",
    "auto-cut-local-image-overlay-revision",
    "auto-cut-music-library-bgm",
    "auto-cut-pointer-targeting",
    "auto-cut-profile-onboarding",
    "auto-cut-review-audio-precision",
    "auto-cut-revision-draft",
    "auto-cut-text-safezone-animation-revision",
}

FORBIDDEN_MARKERS = (
    "高中历史",
    "high-school-history",
    "subject-pointer",
    "subject_pointer",
    "senior-high-history",
    "private-assets-high-school-history",
    "c2597_v37_bgm_cloud_funk",
)


def _skill_directories() -> dict[str, Path]:
    return {path.name: path for path in SKILLS_ROOT.iterdir() if path.is_dir()}


def test_package_contains_the_complete_workspace_skill_surface() -> None:
    skills = _skill_directories()
    assert set(skills) == EXPECTED_SKILLS
    assert build_lite_plugin.EXPECTED_SKILLS == EXPECTED_SKILLS

    for name, directory in skills.items():
        skill_text = (directory / "SKILL.md").read_text(encoding="utf-8-sig")
        frontmatter = re.match(r"\A---\s*\n(.*?)\n---\s*\n", skill_text, re.DOTALL)
        assert frontmatter is not None, name
        declared_name = re.search(r"(?m)^name:\s*['\"]?([^'\"\s]+)", frontmatter.group(1))
        assert declared_name is not None and declared_name.group(1) == name

        metadata_path = directory / "agents" / "openai.yaml"
        metadata = metadata_path.read_text(encoding="utf-8-sig")
        assert re.search(r'(?m)^\s*display_name:\s*"[^"\r\n]+"\s*$', metadata)
        assert re.search(r'(?m)^\s*short_description:\s*"[^"\r\n]+"\s*$', metadata)
        prompt = re.search(r'(?m)^\s*default_prompt:\s*"([^"\r\n]+)"\s*$', metadata)
        assert prompt is not None and f"${name}" in prompt.group(1)

    manifest = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8-sig")
    )
    assert "skills" not in manifest
    assert not (PLUGIN_ROOT / "skills").exists()


def test_all_packaged_skill_relative_links_resolve() -> None:
    missing: list[str] = []
    for markdown in SKILLS_ROOT.rglob("*.md"):
        text = markdown.read_text(encoding="utf-8-sig")
        for target in re.findall(r"\]\(([^)]+)\)", text):
            clean = target.split("#", 1)[0].strip()
            if not clean or "://" in clean or clean.startswith("mailto:"):
                continue
            if not (markdown.parent / clean).resolve().exists():
                missing.append(f"{markdown.relative_to(PLUGIN_ROOT)} -> {target}")
    assert missing == []


def test_visual_skills_share_one_authoritative_lite_execution_contract() -> None:
    contract = SKILLS_ROOT / "auto-cut-lite" / "references" / "lite-execution-contract.md"
    contract_text = contract.read_text(encoding="utf-8-sig")
    assert "single authoritative visual execution contract" in contract_text
    assert "Lite Timing Adjusted" in contract_text
    assert "execution_required" in contract_text
    assert "word/character ASR-resolved cut start" in contract_text
    assert "otherwise at the review-comment time" in contract_text
    assert "New or unrecognized issue" in contract_text
    assert "same normalized modification name" in contract_text
    assert "user_action_required" in contract_text

    agents_text = (PLUGIN_ROOT / "AGENTS.md").read_text(encoding="utf-8-sig")
    assert "Lite Execution Precedence" in agents_text
    assert ".codex/skills/auto-cut-lite/references/lite-execution-contract.md" in agents_text
    assert "overrides conflicting text in every router" in agents_text

    contract_users = {
        "auto-cut",
        "auto-cut-animation-timing-revision",
        "auto-cut-basic-oral-video",
        "auto-cut-editable-ad-revision",
        "auto-cut-favorite-text-assets",
        "auto-cut-final-acceptance",
        "auto-cut-lite",
        "auto-cut-local-image-overlay-revision",
        "auto-cut-pointer-targeting",
        "auto-cut-profile-onboarding",
        "auto-cut-revision-draft",
        "auto-cut-text-safezone-animation-revision",
    }
    for skill_name in contract_users:
        skill_text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8-sig")
        assert "lite-execution-contract.md" in skill_text, skill_name


def test_lite_audio_timing_and_a2_acceptance_contract_is_packaged() -> None:
    contract = (
        SKILLS_ROOT / "auto-cut-lite" / "references" / "lite-execution-contract.md"
    ).read_text(encoding="utf-8-sig")
    router = (SKILLS_ROOT / "auto-cut" / "SKILL.md").read_text(encoding="utf-8-sig")
    audio = (
        SKILLS_ROOT / "auto-cut-review-audio-precision" / "SKILL.md"
    ).read_text(encoding="utf-8-sig")
    acceptance = (
        SKILLS_ROOT / "auto-cut-final-acceptance" / "references" / "checklist.md"
    ).read_text(encoding="utf-8-sig")
    agents = (PLUGIN_ROOT / "AGENTS.md").read_text(encoding="utf-8-sig")

    combined = "\n".join((contract, router, audio, acceptance, agents))
    assert "Only a uniquely ASR-located spoken deletion" in combined
    assert "review-comment time" in combined
    assert "ASR fails or is non-unique" in combined
    assert "0:00" in combined
    assert "one independent" in combined
    assert "merged ASR" in combined
    assert "full-length" in combined
    assert "pending" in combined


def test_lite_packaged_rules_do_not_restore_old_asr_or_visual_execution_contracts() -> None:
    markdown = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in sorted(SKILLS_ROOT.rglob("*.md"))
    )
    stale_rules = (
        "Only completely non-speech items use the review timestamp",
        "Missing authoritative ASR fails before",
        "a review timestamp cannot substitute for missing ASR",
        "whole-section advance or full-screen video advance",
        "strict validation always enforces the canonical pointer-profile receipt",
    )
    for rule in stale_rules:
        assert rule not in markdown

    assert "every duration-changing request is label-only" in markdown
    assert "Every newly encountered or unrecognized issue is label-only" in markdown
    assert "review_timestamp_fallback" in markdown
    assert "same normalized modification name" in markdown
    assert "user_action_required" in markdown


def test_active_lite_visual_prompts_do_not_reenable_full_visual_execution() -> None:
    active_files = [
        SKILLS_ROOT / "auto-cut" / "SKILL.md",
        SKILLS_ROOT / "auto-cut-animation-timing-revision" / "SKILL.md",
        SKILLS_ROOT / "auto-cut-animation-timing-revision" / "agents" / "openai.yaml",
        SKILLS_ROOT / "auto-cut-local-image-overlay-revision" / "SKILL.md",
        SKILLS_ROOT / "auto-cut-pointer-targeting" / "SKILL.md",
        SKILLS_ROOT / "auto-cut-pointer-targeting" / "agents" / "openai.yaml",
        SKILLS_ROOT / "auto-cut-text-safezone-animation-revision" / "SKILL.md",
        SKILLS_ROOT / "auto-cut-final-acceptance" / "SKILL.md",
    ]
    active_text = "\n".join(path.read_text(encoding="utf-8-sig") for path in active_files)
    forbidden = (
        "fresh-check its exact stage+subject",
        "frame-level boundary checks",
        "精准覆盖局部错误内容",
        "pointer source-reference sizing",
        "markers are not execution evidence",
    )
    for phrase in forbidden:
        assert phrase not in active_text

    animation_text = (SKILLS_ROOT / "auto-cut-animation-timing-revision" / "SKILL.md").read_text(
        encoding="utf-8-sig"
    )
    pointer_text = (SKILLS_ROOT / "auto-cut-pointer-targeting" / "SKILL.md").read_text(
        encoding="utf-8-sig"
    )
    assert "execution_required=false" in animation_text
    assert "leave `Lite Timing Adjusted` empty" in animation_text
    assert "default geometry" in pointer_text
    assert "label-only" in pointer_text


def test_documented_python_entrypoints_are_bundled() -> None:
    missing: list[str] = []
    command_pattern = re.compile(r"\bpython(?:\.exe)?\s+([A-Za-z0-9_./\\-]+\.py)\b")
    for markdown in SKILLS_ROOT.rglob("*.md"):
        text = markdown.read_text(encoding="utf-8-sig")
        for raw_path in command_pattern.findall(text):
            relative = Path(raw_path.replace("\\", "/"))
            if relative.parts[0] == "scripts":
                source_target = REPO_ROOT / relative
                relative_posix = relative.as_posix()
                copied_by_directory = any(
                    relative_posix.startswith(f"{directory}/")
                    for directory in build_lite_plugin.RUNTIME_SCRIPT_DIRS
                )
                copied_as_top_level = (
                    len(relative.parts) == 2
                    and relative.name in build_lite_plugin.RUNTIME_TOP_LEVEL_SCRIPTS
                )
                target_exists = source_target.exists() and (
                    copied_by_directory or copied_as_top_level
                )
            elif relative.parts[0] == "skills":
                source_target = PLUGIN_ROOT / relative
                target_exists = source_target.exists()
            else:
                continue
            if not target_exists:
                missing.append(f"{markdown.relative_to(PLUGIN_ROOT)} -> {raw_path}")
    assert missing == []


def test_plugin_skill_text_has_no_private_or_source_checkout_markers() -> None:
    findings: list[str] = []
    for path in SKILLS_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {
            ".md",
            ".py",
            ".json",
            ".yaml",
            ".yml",
        }:
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace").casefold()
        for marker in FORBIDDEN_MARKERS:
            if marker.casefold() in text:
                findings.append(f"{path.relative_to(PLUGIN_ROOT)}:{marker}")
        assert "e:\\codex" not in text
        assert "c:\\users\\yc" not in text
        assert "../../docs/" not in text
        assert "../../examples/" not in text
        assert "tmp/c2597" not in text
    assert findings == []


def test_portable_capability_contract_requires_named_marketplace_and_split_runtimes() -> None:
    payload = json.loads((PLUGIN_ROOT / "PORTABLE-CAPABILITIES.json").read_text(encoding="utf-8"))

    assert payload["plugin_name"] == "auto-cut-lite"
    manifest = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8-sig")
    )
    assert payload["plugin_version"] == manifest["version"]
    assert payload["marketplace"] == {
        "name": "auto-cut-lite-marketplace",
        "display_name": "Auto-Cut Lite",
    }
    assert payload["runtime_environments"]["main"]["default"] == "installed"
    audio = payload["runtime_environments"]["audio"]
    assert audio["default"] == "installed"
    assert audio["isolation"] == "separate"
    assert payload["workspace_installation"] == {
        "mode": "combined_package_workspace",
        "default_root": "extracted_package_root",
        "custom_root_supported": True,
        "custom_root_parameter": "WorkspaceRoot",
        "beginner_guide": "Auto-Cut-Lite新手部署说明.md",
        "one_click_launcher": "一键安装或升级-Auto-Cut-Lite.cmd",
        "one_click_uninstaller": "一键卸载-Auto-Cut-Lite.cmd",
        "one_click_default_network": "china_mirrors",
        "post_install_guide": "Auto-Cut-Lite部署成功后操作说明.md",
        "required_leaf_name": "Auto-cut-lite",
        "upgrade_root_precedence": "parameter_then_existing_receipt_then_package_root",
        "explicit_path_upgrade": "verified_relocation_with_rollback",
        "managed_package_sync": "manifest_verified_transactional",
        "managed_package_rollback": True,
        "payload_skills_path": "workspace-payload/skills",
        "skills_path": ".codex/skills",
        "agents_path": "AGENTS.md",
        "scope": "repo",
        "label": "Auto-cut-lite",
        "plugin_manifest_exposes_skills": False,
        "plugin_top_level_skills_present": False,
    }
    capability_ids = {row["id"] for row in payload["capabilities"]}
    assert capability_ids == {
        "workspace_skill_surface",
        "review_document_and_replacement_timebase",
        "editable_jianying_revision",
        "asr_audio_precision",
        "audio_restoration",
        "animation_text_bgm_and_favorites",
        "pointer_profile_onboarding",
        "portable_delivery_and_relink",
        "named_marketplace_deployment",
    }
