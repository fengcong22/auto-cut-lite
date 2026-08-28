"""Build the distributable generic auto-cut-lite Codex plugin.

The plugin is assembled from an explicit runtime allowlist.  The source checkout
contains private development material, so the repository root is never archived
as a whole.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

PLUGIN_NAME = "auto-cut-lite"
WORKSPACE_NAME = "Auto-cut-lite"
PLUGIN_VERSION = "1.6.0+codex.20260828170002"
ARCHIVE_NAME = f"{PLUGIN_NAME}-{PLUGIN_VERSION}-windows-x64.zip"
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
    "C2597_v37_bgm_cloud_funk",
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
RUNTIME_SCRIPT_DIRS = (
    "scripts/audio",
    "scripts/cli",
    "scripts/core",
    "scripts/portable",
    "scripts/portable_project",
    "scripts/utils",
    "scripts/vendor",
)
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
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
    ".csv",
    ".ps1",
    ".cmd",
    ".bat",
}
SANITIZE = {
    "高中历史": "通用精简版",
    "High School History": "Generic Lite",
    "high-school-history": "auto-cut-lite",
    "subject-pointer": "pointer-profile",
    "subject_pointer": "pointer_profile",
    "SUBJECT_POINTER": "POINTER_PROFILE",
    "SUBJECT-POINTER": "POINTER-PROFILE",
    "senior-high-history": "generic-course-profile",
    "private-assets-high-school-history": "private-assets",
    "C2597_v37_bgm_cloud_funk": "generic_oral_video_baseline",
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


def _git_release_identity(repo: Path) -> dict[str, object]:
    commit_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if commit_result.returncode != 0:
        raise ValueError("release source must be a committed Git worktree")
    commit = commit_result.stdout.strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("release source Git commit is invalid")

    status_result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if status_result.returncode != 0:
        raise ValueError("release source Git status could not be read")
    return {
        "commit": commit,
        "clean": not bool(status_result.stdout.strip()),
    }


def _safe_relative(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
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
        if entry.name in exclude_names or entry.name in {
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
        }:
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
        """[CmdletBinding()]\nparam([switch]$WithAudio, [switch]$SkipAudio, [switch]$UseChinaMirrors, [string]$WorkspaceRoot, [string]$LocalAppDataRoot, [string]$UserProfileRoot)\n$ErrorActionPreference = 'Stop'\n& (Join-Path $PSScriptRoot 'deploy-to-codex.ps1') -WithAudio:$WithAudio -SkipAudio:$SkipAudio -UseChinaMirrors:$UseChinaMirrors -WorkspaceRoot $WorkspaceRoot -LocalAppDataRoot $LocalAppDataRoot -UserProfileRoot $UserProfileRoot\nexit $LASTEXITCODE\n""",
    )
    _write_text(
        stage / "TARGET_SETUP.md",
        """# Target setup\n\nRead `Auto-Cut-Lite新手部署说明.md` before installation.\n\nFirst install: use **Extract All**, open the extracted `Auto-cut-lite` directory, and double-click `一键安装或升级-Auto-Cut-Lite.cmd`. The launcher asks where the stable workspace should live, validates the extracted package, and uses China mirrors by default. The selected workspace receives the complete managed package, `AGENTS.md`, and `.codex/skills`.\n\nUpgrade: extract the new ZIP into a temporary directory and double-click its `一键安装或升级-Auto-Cut-Lite.cmd`. Choose the existing workspace when prompted. The installer synchronizes the new package into it with rollback protection. Delete only the temporary upgrade extraction after success. Do not extract a new ZIP directly over the stable workspace.\n\nUninstall: double-click `一键卸载-Auto-Cut-Lite.cmd`. It validates deployment receipts and removes only Auto-Cut Lite managed files, environments, and registrations.\n\nAdvanced users can run `powershell -ExecutionPolicy Bypass -File .\\deploy-to-codex.ps1` directly. Use `-UseChinaMirrors` when direct pip/npm access is unreliable. Use `-WorkspaceRoot \"<absolute-path>\\Auto-cut-lite\"` only when selecting or relocating the stable workspace. The runtime remains under `%LOCALAPPDATA%\\Auto-Cut\\auto-cut-lite`; open the reported `workspace_root` in Codex, start a new thread, and follow `Auto-Cut-Lite部署成功后操作说明.md`.\n""",
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
    _copy_tree(
        repo / "schemas",
        runtime / "schemas",
        exclude_names={"private-subject-assets-manifest.schema.json"},
    )
    relink = repo / "tools" / "relink_tool" / "Auto-Cut剪映素材重链工具.exe"
    if relink.is_file():
        _copy_file(
            relink, stage / "runtime" / "tools" / "relink_tool" / relink.name, sanitize_text=False
        )


def _tree_inventory(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = _safe_relative(path.relative_to(root).as_posix())
        rows.append({"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)})
    return rows


def _validate_portable_capabilities(stage: Path) -> dict[str, int | str]:
    contract_path = stage / "PORTABLE-CAPABILITIES.json"
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"portable capability contract is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("portable capability contract root must be an object")
    if payload.get("plugin_name") != PLUGIN_NAME or payload.get("plugin_version") != PLUGIN_VERSION:
        raise ValueError("portable capability contract identity does not match the plugin")
    plugin_manifest = json.loads(
        (stage / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8-sig")
    )
    if (
        plugin_manifest.get("name") != PLUGIN_NAME
        or plugin_manifest.get("version") != PLUGIN_VERSION
    ):
        raise ValueError("plugin manifest identity does not match the portable contract")
    if "skills" in plugin_manifest:
        raise ValueError("plugin manifest must not expose user-scoped skills")
    if (stage / "skills").exists():
        raise ValueError("plugin package must not contain a top-level skills directory")
    marketplace = payload.get("marketplace")
    if (
        not isinstance(marketplace, dict)
        or marketplace.get("name") != "auto-cut-lite-marketplace"
        or marketplace.get("display_name") != "Auto-Cut Lite"
    ):
        raise ValueError("portable capability contract marketplace identity is invalid")
    environments = payload.get("runtime_environments")
    if not isinstance(environments, dict):
        raise ValueError("portable capability contract has no runtime environments")
    main = environments.get("main")
    audio = environments.get("audio")
    if not isinstance(main, dict) or main.get("default") != "installed":
        raise ValueError("main runtime must be installed by default")
    if (
        not isinstance(audio, dict)
        or audio.get("default") != "installed"
        or audio.get("isolation") != "separate"
    ):
        raise ValueError("audio runtime must be installed by default in a separate environment")
    workspace = payload.get("workspace_installation")
    if not isinstance(workspace, dict) or workspace != {
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
    }:
        raise ValueError("workspace installation contract is invalid")

    skills_root = stage / "workspace-payload" / "skills"
    skill_names = {path.name for path in skills_root.iterdir() if path.is_dir()}
    if skill_names != EXPECTED_SKILLS:
        missing = sorted(EXPECTED_SKILLS - skill_names)
        extra = sorted(skill_names - EXPECTED_SKILLS)
        raise ValueError(f"portable skill surface mismatch: missing={missing}, extra={extra}")
    for name in sorted(skill_names):
        for relative in ("SKILL.md", "agents/openai.yaml"):
            path = skills_root / name / relative
            if not path.is_file() or _is_reparse(path):
                raise ValueError(f"portable skill metadata is missing or unsafe: {name}/{relative}")

    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("portable capability contract has no capabilities")
    capability_ids: set[str] = set()
    required_count = 0
    for capability in capabilities:
        if not isinstance(capability, dict) or not isinstance(capability.get("id"), str):
            raise ValueError("portable capability contract contains an invalid capability")
        capability_id = capability["id"]
        if capability_id in capability_ids:
            raise ValueError(f"duplicate portable capability id: {capability_id}")
        capability_ids.add(capability_id)
        required_paths = capability.get("required_paths")
        if not isinstance(required_paths, list) or not required_paths:
            raise ValueError(f"portable capability has no required paths: {capability_id}")
        for raw_path in required_paths:
            if not isinstance(raw_path, str):
                raise ValueError(f"portable capability path is invalid: {capability_id}")
            relative = _safe_relative(raw_path)
            path = stage.joinpath(*PurePosixPath(relative).parts)
            if not path.is_file() or _is_reparse(path):
                raise ValueError(
                    f"portable capability path is missing or unsafe: {capability_id}:{relative}"
                )
            required_count += 1
    return {
        "status": "pass",
        "capability_count": len(capability_ids),
        "required_path_count": required_count,
        "skill_count": len(skill_names),
        "workspace_scope": workspace["scope"],
        "workspace_label": workspace["label"],
    }


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
    for path in sorted(
        (item for item in stage_parent.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(stage_parent).as_posix(),
    ):
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


def build(
    repo_root: Path,
    output: Path,
    *,
    require_clean: bool = True,
) -> dict[str, object]:
    repo = repo_root.resolve()
    output = output.resolve()
    source_identity = _git_release_identity(repo)
    if require_clean and not source_identity["clean"]:
        raise ValueError("release source worktree is dirty; commit and retest before packaging")
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="auto-cut-lite-plugin-", dir=output.parent
    ) as temporary:
        parent = Path(temporary)
        stage = parent / WORKSPACE_NAME
        source_plugin = repo / "plugins" / PLUGIN_NAME
        if not source_plugin.is_dir():
            raise FileNotFoundError(source_plugin)
        _copy_tree(source_plugin, stage)
        _copy_runtime(repo, stage)
        _write_target_setup(stage)
        capability_evidence = _validate_portable_capabilities(stage)
        findings = _privacy_scan(stage)
        if findings:
            raise ValueError("plugin privacy scan failed: " + "; ".join(findings[:20]))
        inventory = _tree_inventory(stage)
        _write_text(
            stage / "PACKAGE-MANIFEST.json",
            json.dumps(
                {"name": PLUGIN_NAME, "version": PLUGIN_VERSION, "files": inventory},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
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
            "staged_tree_sha256": hashlib.sha256(
                json.dumps(
                    inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
            "runtime_allowlist": True,
            "portable_capability_closure": capability_evidence["status"],
            "portable_capability_count": capability_evidence["capability_count"],
            "portable_required_path_count": capability_evidence["required_path_count"],
            "packaged_skill_count": capability_evidence["skill_count"],
            "archive_root": WORKSPACE_NAME,
            "workspace_mode": "combined_package_workspace",
            "workspace_root_default": "extracted_package_root",
            "workspace_root_customizable": True,
            "workspace_root_parameter": "WorkspaceRoot",
            "workspace_root_precedence": "parameter_then_existing_receipt_then_package_root",
            "workspace_explicit_path_upgrade": "verified_relocation_with_rollback",
            "workspace_package_sync": "manifest_verified_transactional",
            "workspace_package_rollback": True,
            "workspace_skill_payload": "workspace-payload/skills",
            "workspace_skill_scope": capability_evidence["workspace_scope"],
            "workspace_skill_label": capability_evidence["workspace_label"],
            "plugin_manifest_exposes_skills": False,
            "plugin_top_level_skills_present": False,
            "privacy_scan": "pass",
            "zip_crc": "pass",
            "zip_path_safety": "pass",
            "feishu_identity": "target_user_only",
            "credentials_bundled": False,
            "private_subject_assets_bundled": False,
            "source_git_commit": source_identity["commit"],
            "source_git_clean": source_identity["clean"],
            "one_command_deployer": "deploy-to-codex.ps1",
            "one_click_launcher": "一键安装或升级-Auto-Cut-Lite.cmd",
            "one_click_uninstaller": "一键卸载-Auto-Cut-Lite.cmd",
            "one_click_default_network": "china_mirrors",
            "one_click_internal_manifest_validation": True,
            "post_install_codex_guide": "Auto-Cut-Lite部署成功后操作说明.md",
            "marketplace_name": "auto-cut-lite-marketplace",
            "marketplace_display_name": "Auto-Cut Lite",
            "named_marketplace_registration": "atomic_structured_helper",
            "legacy_personal_migration": "automatic",
            "runtime_dependency_installation": "separate_main_and_audio_transactional_reuse_upgrade_rebuild",
            "runtime_dependency_rollback": True,
            "audio_runtime_default": "installed",
            "deployment_report": "%LOCALAPPDATA%\\Auto-Cut\\auto-cut-lite\\deployment-report.json",
            "readiness_model": "installed_can_require_user_configuration",
            "codex_cli_fallback": "@openai/codex@0.149.1_via_npx",
        }
        receipt_path = output.with_name(output.name + ".receipt.json")
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
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
