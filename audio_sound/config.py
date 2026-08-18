from __future__ import annotations

import copy
import json
import os
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRESETS_DIR = PROJECT_ROOT / "presets" / "audio_sound"
OFFLINE_RUNTIME_RELATIVE_PATH = Path("tmp") / "offline-runtime"
PLAYWRIGHT_CHROMIUM_REVISION = "1169"
PLAYWRIGHT_FFMPEG_REVISION = "1011"
PLAYWRIGHT_WINLDD_REVISION = "1007"


def list_presets() -> list[str]:
    return sorted(path.stem for path in PRESETS_DIR.glob("*.json"))


def load_preset(name: str) -> dict[str, Any]:
    preset_path = PRESETS_DIR / f"{name}.json"
    if not preset_path.exists():
        raise FileNotFoundError(f"Preset not found: {preset_path}")
    payload = json.loads(preset_path.read_text(encoding="utf-8"))
    validate_preset(payload)
    return payload


def validate_preset(preset: dict[str, Any]) -> None:
    required_top_level = [
        "name",
        "description",
        "pipeline",
        "extract",
        "filters",
        "analysis",
        "transcript_export",
    ]
    for key in required_top_level:
        if key not in preset:
            raise ValueError(f"Preset is missing required section: {key}")


def apply_runtime_overrides(
    preset: dict[str, Any],
    *,
    target_lufs: float | None = None,
    denoise_strength: str | None = None,
    disable_gate: bool = False,
    enable_silence_report: bool = False,
    enable_legacy_breath_filters: bool = False,
) -> dict[str, Any]:
    resolved = copy.deepcopy(preset)

    if target_lufs is not None:
        resolved["filters"]["loudnorm"]["target_i"] = float(target_lufs)

    if denoise_strength is not None:
        mapping = {
            "light": {"nr": 6, "nf": -30},
            "medium": {"nr": 10, "nf": -26},
            "aggressive": {"nr": 16, "nf": -22},
        }
        if denoise_strength not in mapping:
            raise ValueError(f"Unknown denoise strength: {denoise_strength}")
        resolved["filters"]["secondary_denoise"].update(mapping[denoise_strength])

    if disable_gate:
        resolved["filters"]["gate"]["enabled"] = False

    if enable_silence_report:
        resolved["analysis"]["silence_candidates"] = True

    if enable_legacy_breath_filters:
        if "breath_ducking" in resolved["filters"]:
            resolved["filters"]["breath_ducking"]["enabled"] = True
        if "breath_onset_cleanup" in resolved["filters"]:
            resolved["filters"]["breath_onset_cleanup"]["enabled"] = True

    return resolved


def load_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    try:
        raw_lines = env_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return values
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_binary(name: str, default: str, env_values: dict[str, str] | None = None) -> str:
    values = env_values or {}
    return os.environ.get(name) or values.get(name) or default


def resolve_bundled_binary(repo_root: Path, name: str, fallback: str) -> str:
    if name not in {"ffmpeg", "ffprobe"}:
        raise ValueError("unsupported bundled binary")
    candidate = (
        Path(repo_root) / OFFLINE_RUNTIME_RELATIVE_PATH / "tools" / "ffmpeg" / "bin" / f"{name}.exe"
    )
    if candidate.is_file() and not candidate.is_symlink():
        return str(candidate)
    return fallback


def resolve_playwright_browser_root(repo_root: Path) -> Path | None:
    browser_root = Path(repo_root) / OFFLINE_RUNTIME_RELATIVE_PATH / "browsers"
    chromium = (
        browser_root / f"chromium-{PLAYWRIGHT_CHROMIUM_REVISION}" / "chrome-win" / "chrome.exe"
    )
    headless_shell = (
        browser_root
        / f"chromium_headless_shell-{PLAYWRIGHT_CHROMIUM_REVISION}"
        / "chrome-win"
        / "headless_shell.exe"
    )
    recorder = browser_root / f"ffmpeg-{PLAYWRIGHT_FFMPEG_REVISION}" / "ffmpeg-win64.exe"
    winldd = browser_root / f"winldd-{PLAYWRIGHT_WINLDD_REVISION}" / "PrintDeps.exe"
    if all(
        path.is_file() and not path.is_symlink()
        for path in (chromium, headless_shell, recorder, winldd)
    ):
        return browser_root
    return None


def configure_playwright_browser_path(
    repo_root: Path,
    environment: MutableMapping[str, str] | None = None,
) -> Path | None:
    target_environment = environment if environment is not None else os.environ
    configured = target_environment.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return Path(configured)
    browser_root = resolve_playwright_browser_root(repo_root)
    if browser_root is not None:
        target_environment["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
    return browser_root


def resolve_repo_python(repo_root: str | Path | None = None) -> str:
    root = Path(repo_root) if repo_root else PROJECT_ROOT
    return str(root / ".venv-audio" / "Scripts" / "python.exe")
