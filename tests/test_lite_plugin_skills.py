from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.release import build_lite_plugin


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "auto-cut-lite"
SKILLS_ROOT = PLUGIN_ROOT / "skills"

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


def test_plugin_exposes_the_complete_generic_skill_surface() -> None:
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
        if not path.is_file() or path.suffix.lower() not in {".md", ".py", ".json", ".yaml", ".yml"}:
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
    payload = json.loads(
        (PLUGIN_ROOT / "PORTABLE-CAPABILITIES.json").read_text(encoding="utf-8")
    )

    assert payload["plugin_name"] == "auto-cut-lite"
    assert payload["plugin_version"] == "1.2.1"
    assert payload["marketplace"] == {
        "name": "auto-cut-lite-marketplace",
        "display_name": "Auto-Cut Lite",
    }
    assert payload["runtime_environments"]["main"]["default"] == "installed"
    audio = payload["runtime_environments"]["audio"]
    assert audio["default"] == "installed"
    assert audio["isolation"] == "separate"
    capability_ids = {row["id"] for row in payload["capabilities"]}
    assert capability_ids == {
        "skill_surface",
        "review_document_and_replacement_timebase",
        "editable_jianying_revision",
        "asr_audio_precision",
        "audio_restoration",
        "animation_text_bgm_and_favorites",
        "pointer_profile_onboarding",
        "portable_delivery_and_relink",
        "named_marketplace_deployment",
    }
