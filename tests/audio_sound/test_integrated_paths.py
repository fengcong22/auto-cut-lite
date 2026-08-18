from __future__ import annotations

import argparse
import hashlib
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from audio_sound import bootstrap, cli
from audio_sound.config import PRESETS_DIR, PROJECT_ROOT, list_presets, load_env_file

EXPECTED_PRESETS = {
    "fast",
    "final",
    "repair",
    "repair-soft",
    "review",
    "safe",
    "voice-isolate",
}
SCRIPT_NAMES = {
    "audio_cleanup.py",
    "audio_skill_workflow.py",
    "exact_window_cleanup.py",
    "narrow_onset_cleanup.py",
    "remove_spoken_segments.py",
}


def test_presets_resolve_only_from_integrated_directory() -> None:
    assert PRESETS_DIR == PROJECT_ROOT / "presets" / "audio_sound"
    assert set(list_presets()) == EXPECTED_PRESETS
    assert list((PROJECT_ROOT / "presets").glob("*.json")) == []


@pytest.mark.parametrize("script_name", sorted(SCRIPT_NAMES))
def test_audio_script_help_runs_outside_repository(script_name: str, tmp_path: Path) -> None:
    script = PROJECT_ROOT / "scripts" / "audio" / script_name
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_audio_fixture_retains_upstream_bytes() -> None:
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "audio_sound" / "fixture_respiro.wav"
    assert fixture.stat().st_size == 192_044
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == (
        "77dfcb6ed774b81696811451c32873c1eb53f73db47823891dfe23459642fc9f"
    )


def test_audio_package_does_not_import_script_modules() -> None:
    offenders = []
    for module in (PROJECT_ROOT / "audio_sound").glob("*.py"):
        if "from scripts." in module.read_text(encoding="utf-8"):
            offenders.append(module.name)

    assert offenders == []


def test_clean_repo_returns_failure_when_cleanup_is_rejected() -> None:
    args = argparse.Namespace(dry_run=False)
    with mock.patch("audio_sound.cli.prune_workspace") as prune_workspace:
        prune_workspace.return_value = {
            "ok": False,
            "code": "unsafe_repo_root",
            "reason": "unsafe root",
        }
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = cli.command_clean_repo(args)

    assert exit_code == 1
    assert "unsafe_repo_root" in buffer.getvalue()


def test_doctor_returns_failure_when_required_runtime_is_unavailable() -> None:
    args = argparse.Namespace(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        python_executable="missing-python",
    )
    with mock.patch(
        "audio_sound.cli.detect_runtime",
        return_value={"status": "unavailable", "python": {"ok": False}},
    ):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = cli.command_doctor(args)

    assert exit_code == 1
    assert '"status": "unavailable"' in buffer.getvalue()


def test_runtime_env_loader_degrades_for_non_utf8_content(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"AUDIO_SOUND_RESPIRO_REPO=repo\xff")

    assert load_env_file(env_path) == {}


def _active_lock_requirements(path: Path) -> set[str]:
    return {
        line.strip().split(" --hash=", 1)[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def test_audio_build_helpers_are_pinned_and_match_the_runtime_lock() -> None:
    build_requirements = _active_lock_requirements(PROJECT_ROOT / "requirements-audio-build.lock")
    runtime_requirements = _active_lock_requirements(PROJECT_ROOT / "requirements-audio.lock")

    assert build_requirements == {"packaging==23.2", "wheel==0.45.1"}
    assert bootstrap._AUDIO_SOURCE_BOOTSTRAP_REQUIREMENT in runtime_requirements


def test_heavy_audio_runtime_is_locked_and_ignored() -> None:
    lock_lines = _active_lock_requirements(PROJECT_ROOT / "requirements-audio.lock")
    assert lock_lines == {
        "numpy==1.23.5",
        "librosa==0.10.0",
        "soundfile==0.12.1",
        "scipy==1.10.1",
        "intervaltree==3.1.0",
        "torch==2.3.1",
        "torchaudio==2.3.1",
        "deepfilternet==0.5.6",
        "pytest==8.4.2",
    }

    main_requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    for package in ("torch", "torchaudio", "deepfilternet", "librosa", "intervaltree"):
        assert package not in main_requirements.lower()

    ignore_lines = set((PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    assert {
        ".env",
        ".venv-audio/",
        "tools/audio_sound_runtime/",
        "output/",
        "scratch/audio-sound/",
        ".cargo-home/audio-sound/",
    }.issubset(ignore_lines)
