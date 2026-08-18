from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / "skills" / "auto-cut-audio-restoration"


def test_audio_restoration_skill_name_matches_its_directory() -> None:
    skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"^name:\s*(.+)$", skill_text, flags=re.MULTILINE)

    assert match is not None
    assert match.group(1).strip() == "auto-cut-audio-restoration"


def test_audio_restoration_is_registered_in_router_and_catalogs() -> None:
    registered_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            REPO_ROOT / "skills" / "auto-cut" / "SKILL.md",
            REPO_ROOT / "skills" / "auto-cut" / "references" / "skill-catalog.md",
            REPO_ROOT / "docs" / "skill-catalog.md",
            REPO_ROOT / "skills" / "README.md",
        )
    )

    assert registered_text.count("auto-cut-audio-restoration") >= 4


def test_audio_restoration_skill_uses_integrated_commands_only() -> None:
    skill_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*.md"))
    )

    assert ".codex/skills/audio-sound" not in skill_text
    assert "../../../scripts/" not in skill_text
    assert "scripts/audio_cleanup.py" not in skill_text
    assert "scripts/audio_skill_workflow.py" not in skill_text
    assert "scripts/remove_spoken_segments.py" not in skill_text
    assert "scripts/audio/audio_cleanup.py" in skill_text
    assert "scripts/audio/audio_skill_workflow.py" in skill_text
    assert "scripts/audio/remove_spoken_segments.py" in skill_text


def test_windows_audio_wrappers_use_the_integrated_runtime() -> None:
    wrappers = [
        REPO_ROOT / "scripts" / "audio" / name
        for name in ("setup.cmd", "doctor.cmd", "check_runtime.cmd", "run_audio_workflow.cmd")
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in wrappers)

    assert ".venv\\Scripts" not in combined
    assert "scripts\\audio_cleanup.py" not in combined
    assert "scripts\\audio_skill_workflow.py" not in combined
    assert ".venv-audio\\Scripts\\python.exe" in combined
    assert "scripts\\audio\\audio_cleanup.py" in combined
    assert "scripts\\audio\\audio_skill_workflow.py" in combined


def test_maintained_audio_docs_use_integrated_paths() -> None:
    docs_root = REPO_ROOT / "docs" / "audio-sound"
    maintained_docs = [
        docs_root / "README.md",
        docs_root / "RELEASE_QUICK_START.md",
        docs_root / "architecture.md",
        docs_root / "reference-sop.md",
        docs_root / "tuning-guide.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in maintained_docs)

    assert "scripts/audio_cleanup.py" not in combined
    assert "scripts/audio_skill_workflow.py" not in combined
    assert "scripts/remove_spoken_segments.py" not in combined
    assert "presets/review.json" not in combined
    assert "scripts/audio/audio_cleanup.py" in combined
    assert "scripts/audio/audio_skill_workflow.py" in combined
    assert "presets/audio_sound/" in combined
    assert "README.upstream.md" in combined


def test_restoration_workflow_commands_explicitly_skip_deepfilternet() -> None:
    command_docs = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "audio-sound" / "README.md",
        REPO_ROOT / "docs" / "audio-sound" / "RELEASE_QUICK_START.md",
        *sorted((SKILL_ROOT / "references").glob("*.md")),
    ]
    command_pattern = re.compile(
        r"^\s*python\s+scripts/audio/"
        r"(?:audio_skill_workflow\.py\s+run|audio_cleanup\.py\s+clean)\b[^\r\n]*",
        flags=re.MULTILINE,
    )
    commands = [
        (path.relative_to(REPO_ROOT).as_posix(), command.group(0).strip())
        for path in command_docs
        for command in command_pattern.finditer(path.read_text(encoding="utf-8"))
    ]

    assert commands, "maintained docs must contain at least one restoration workflow command"
    missing_skip = [
        f"{path}: {command}" for path, command in commands if "--skip-deepfilternet" not in command
    ]
    assert not missing_skip, "commands missing --skip-deepfilternet:\n" + "\n".join(missing_skip)


def test_audio_precision_skill_has_no_external_engine_dependency() -> None:
    precision_root = REPO_ROOT / "skills" / "auto-cut-review-audio-precision"
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(precision_root.rglob("*.md"))
    )

    assert "D:\\codex\\Audio-sound-release" not in combined
    assert "scripts/audio/audio_cleanup.py" in combined
    assert "scripts/audio/remove_spoken_segments.py" in combined
