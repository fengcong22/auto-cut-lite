"""Build the distributable generic auto-cut-lite Codex plugin.

The plugin is assembled from an explicit runtime allowlist.  The source checkout
contains private development material, so the repository root is never archived
as a whole.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


PLUGIN_NAME = "auto-cut-lite"
PLUGIN_VERSION = "1.0.2"
ARCHIVE_NAME = f"{PLUGIN_NAME}-{PLUGIN_VERSION}-windows-x64.zip"
FORBIDDEN_MARKERS = (
    "高中历史",
    "high-school-history",
    "subject-pointer",
    "subject_pointer",
    "senior-high-history",
    "private-assets-high-school-history",
)
ROOT_FILES = (
    "LICENSE",
    "pyproject.toml",
    "requirements.txt",
    "requirements-audio.lock",
    "requirements-audio-build.lock",
    "requirements-offline-main.lock",
    "VERSION",
)
RUNTIME_DIRS = ("audio_sound", "presets")
RUNTIME_SCRIPT_DIRS = ("scripts/audio", "scripts/cli", "scripts/core", "scripts/portable", "scripts/portable_project", "scripts/utils", "scripts/vendor")
RUNTIME_TOP_LEVEL_SCRIPTS = (
    "api_validator.py",
    "asset_search.py",
    "auto_cut_notifications.py",
    "auto_exporter.py",
    "build_cloud_music_library.py",
    "build_cloud_text_styles_library.py",
    "cloud_manager.py",
    "draft_inspector.py",
    "draft_retention.py",
    "jy_adapter_core.py",
    "jy_http_server.py",
    "jy_mcp_server.py",
    "jy_wrapper.py",
    "movie_commentary_builder.py",
    "portable_project_tool.py",
    "render_revision_preview.py",
    "review_job.py",
    "smart_zoomer.py",
    "sync_jy_assets.py",
    "sync_jy_favorite_text_assets.py",
    "text_template_adapter_example.py",
    "universal_tts.py",
    "web_recorder.py",
)
PUBLIC_DATA_FILES = (
    "cloud_music_library.csv",
    "cloud_sound_effects.csv",
    "cloud_text_styles.csv",
    "cloud_video_assets.csv",
    "favorite_flower_texts.csv",
    "favorite_text_templates.csv",
    "filters.csv",
    "jy_cached_audio.csv",
    "text_animations.csv",
    "text_templates.csv",
    "transitions.csv",
    "tts_speakers.csv",
    "video_intro_animations.csv",
    "video_outro_animations.csv",
    "video_scene_effects.csv",
)
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".yaml", ".yml", ".csv", ".ps1"}
SANITIZE = {
    "高中历史": "通用精简版",
    "High School History": "Generic Lite",
    "high-school-history": "auto-cut-lite",
    "subject-pointer": "optional-profile",
    "subject_pointer": "optional_profile",
    "SUBJECT_POINTER": "OPTIONAL_PROFILE",
    "SUBJECT-POINTER": "OPTIONAL-PROFILE",
    "senior-high-history": "generic-profile",
    "private-assets-high-school-history": "private-assets",
}


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: str) -> str:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"unsafe archive path: {path}")
    if any("\\" in part or any(char in '<>:"|?*' for char in part) for part in candidate.parts):
        raise ValueError(f"unsafe archive path: {path}")
    return candidate.as_posix()


def _copy_file(source: Path, target: Path, *, sanitize_text: bool = True) -> None:
    if _is_reparse(source) or not source.is_file():
        raise ValueError(f"runtime source is not a regular file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if sanitize_text and source.suffix.casefold() in TEXT_SUFFIXES:
        text = source.read_text(encoding="utf-8-sig")
        for old, new in SANITIZE.items():
            text = text.replace(old, new)
        target.write_text(text, encoding="utf-8", newline="\n")
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def _copy_tree(source: Path, target: Path, *, exclude_names: set[str] | None = None) -> None:
    if _is_reparse(source) or not source.is_dir():
        raise ValueError(f"runtime source is not a regular directory: {source}")
    exclude_names = exclude_names or set()
    for entry in sorted(source.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
        if entry.name in exclude_names or entry.name in {"__pycache__", ".pytest_cache", ".ruff_cache"}:
            continue
        if _is_reparse(entry):
            raise ValueError(f"runtime source contains a reparse point: {entry}")
        destination = target / entry.name
        if entry.is_dir():
            _copy_tree(entry, destination, exclude_names=exclude_names)
        elif entry.is_file():
            _copy_file(entry, destination)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_target_setup(stage: Path) -> None:
    _write_text(
        stage / "install.ps1",
        """[CmdletBinding()]\nparam([switch]$WithAudio)\n$ErrorActionPreference = 'Stop'\n& (Join-Path $PSScriptRoot 'deploy-to-codex.ps1') -WithAudio:$WithAudio\nexit $LASTEXITCODE\n""",
    )
    _write_text(
        stage / "TARGET_SETUP.md",
        """# Target setup\n\n1. Install 64-bit Python 3.10-3.12, Codex CLI, JianYing/CapCut desktop and FFmpeg/FFprobe.\n2. In PowerShell run `powershell -ExecutionPolicy Bypass -File .\\deploy-to-codex.ps1`; add `-WithAudio` when audio restoration dependencies are needed.\n3. Start a new Codex thread after deployment.\n4. Authorize Feishu as the current operator when first prompted. Deployment enforces `default-as user` and `strict-mode user` when `lark-cli` exists, but never copies or creates a token.\n5. Configure ASR credentials only on the target computer and verify them with a real alignment request.\n6. Set the JianYing draft root on the target computer. The package never copies a source computer's account state, caches or absolute paths.\n\nRead `%LOCALAPPDATA%\\Auto-Cut\\auto-cut-lite\\deployment-report.json` for machine readiness. `deployment_status=installed` can coexist with `readiness=pending_user_configuration`.\n\nThe generic workflow does not ship a subject-specific pointer library. Add local image or pointer assets explicitly per project when needed.\n""",
    )
    _write_text(
        stage / "runtime" / "README.md",
        """# Auto-Cut runtime\n\nRun `python scripts/jy_wrapper.py ...` from this directory, or use the equivalent path from the plugin root.\n\nThe runtime is source-inspectable and uses relative paths. Runtime caches, draft outputs, login state and credentials are intentionally target-local.\n""",
    )


def _copy_runtime(repo: Path, stage: Path) -> None:
    runtime = stage / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ROOT_FILES:
        source = repo / name
        if source.is_file():
            _copy_file(source, runtime / name)
    for dirname in RUNTIME_DIRS:
        _copy_tree(repo / dirname, runtime / dirname)
    for dirname in RUNTIME_SCRIPT_DIRS:
        _copy_tree(repo / dirname, runtime / dirname)
    for name in RUNTIME_TOP_LEVEL_SCRIPTS:
        _copy_file(repo / "scripts" / name, runtime / "scripts" / name)
    for name in PUBLIC_DATA_FILES:
        _copy_file(repo / "data" / name, runtime / "data" / name)
    _copy_tree(repo / "schemas", runtime / "schemas", exclude_names={"private-subject-assets-manifest.schema.json"})
    relink = repo / "tools" / "relink_tool" / "Auto-Cut剪映素材重链工具.exe"
    if relink.is_file():
        _copy_file(relink, stage / "runtime" / "tools" / "relink_tool" / relink.name, sanitize_text=False)


def _tree_inventory(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        relative = _safe_relative(path.relative_to(root).as_posix())
        rows.append({"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)})
    return rows


def _privacy_scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in root.rglob("*"):
        if _is_reparse(path):
            findings.append(f"reparse:{path.relative_to(root).as_posix()}")
            continue
        relative = path.relative_to(root).as_posix()
        parts = {part.casefold() for part in PurePosixPath(relative).parts}
        if parts.intersection({".git", ".codex", ".venv", ".venv-audio", "tmp", "output", "logs"}):
            findings.append(f"forbidden_path:{relative}")
        if any(marker.casefold() in relative.casefold() for marker in FORBIDDEN_MARKERS):
            findings.append(f"forbidden_marker_path:{relative}")
        if path.is_file() and path.suffix.casefold() in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            for marker in FORBIDDEN_MARKERS:
                if marker.casefold() in text.casefold():
                    findings.append(f"forbidden_marker_text:{relative}:{marker}")
            if "C:\\Users\\" in text or "E:\\codex\\" in text:
                findings.append(f"absolute_local_path:{relative}")
    return findings


def _zip_name(path: str) -> str:
    return _safe_relative(path.replace("\\", "/"))


def _make_zip(stage_parent: Path, output: Path) -> tuple[str, int]:
    entries: list[tuple[Path, str]] = []
    for path in sorted((item for item in stage_parent.rglob("*") if item.is_file()), key=lambda item: item.relative_to(stage_parent).as_posix()):
        entries.append((path, _zip_name(path.relative_to(stage_parent).as_posix())))
    names = [name.casefold() for _, name in entries]
    if len(names) != len(set(names)):
        raise ValueError("ZIP contains duplicate case-insensitive paths")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, name in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return _sha256(output), len(entries)


def build(repo_root: Path, output: Path) -> dict[str, object]:
    repo = repo_root.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="auto-cut-lite-plugin-", dir=output.parent) as temporary:
        parent = Path(temporary)
        stage = parent / PLUGIN_NAME
        source_plugin = repo / "plugins" / PLUGIN_NAME
        if not source_plugin.is_dir():
            raise FileNotFoundError(source_plugin)
        _copy_tree(source_plugin, stage)
        _copy_runtime(repo, stage)
        _write_target_setup(stage)
        findings = _privacy_scan(stage)
        if findings:
            raise ValueError("plugin privacy scan failed: " + "; ".join(findings[:20]))
        inventory = _tree_inventory(stage)
        _write_text(stage / "PACKAGE-MANIFEST.json", json.dumps({"name": PLUGIN_NAME, "version": PLUGIN_VERSION, "files": inventory}, ensure_ascii=False, indent=2) + "\n")
        # Recompute after the manifest itself is part of the package.
        inventory = _tree_inventory(stage)
        zip_sha256, entry_count = _make_zip(parent, output)
        with zipfile.ZipFile(output) as archive:
            if archive.testzip() is not None:
                raise ValueError("ZIP CRC validation failed")
            names = archive.namelist()
            if any(name.startswith("/") or ".." in PurePosixPath(name).parts for name in names):
                raise ValueError("ZIP contains an unsafe path")
        receipt = {
            "schema_version": 1,
            "status": "pass",
            "plugin_name": PLUGIN_NAME,
            "plugin_version": PLUGIN_VERSION,
            "archive_file": output.name,
            "archive_sha256": zip_sha256,
            "archive_byte_size": output.stat().st_size,
            "archive_entry_count": entry_count,
            "staged_tree_sha256": hashlib.sha256(json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "runtime_allowlist": True,
            "privacy_scan": "pass",
            "zip_crc": "pass",
            "zip_path_safety": "pass",
            "feishu_identity": "target_user_only",
            "credentials_bundled": False,
            "private_subject_assets_bundled": False,
            "one_command_deployer": "deploy-to-codex.ps1",
            "personal_marketplace_registration": "atomic_structured_helper",
            "runtime_dependency_installation": "isolated_venv_automatic",
            "deployment_report": "%LOCALAPPDATA%\\Auto-Cut\\auto-cut-lite\\deployment-report.json",
            "readiness_model": "installed_can_require_user_configuration",
        }
        receipt_path = output.with_name(output.name + ".receipt.json")
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        receipt["receipt_path"] = str(receipt_path)
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the generic auto-cut-lite plugin ZIP.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--output", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    output = Path(args.output) if args.output else Path(args.repo_root) / "dist" / ARCHIVE_NAME
    try:
        result = build(Path(args.repo_root), output)
    except (OSError, ValueError) as exc:
        result = {"status": "failed", "code": "plugin_build_failed", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
