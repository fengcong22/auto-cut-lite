from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from scripts.cloud_manager import cloud_material_status
    from scripts.full_setup import sanitize_payload
    from scripts.universal_tts import get_tts_status
    from scripts.utils.formatters import (
        ConfiguredDraftRootError,
        get_configured_jianying_draft_root,
    )
    from scripts.utils.user_action_notifications import resolve_lark_command
    from scripts.utils.volc_asr_onboarding import (
        build_volc_guide,
        configure_volc,
        inspect_volc_config,
    )
    from tools.recording.install_carnac import carnac_status
except ModuleNotFoundError:  # Direct execution from scripts/.
    from cloud_manager import cloud_material_status
    from full_setup import sanitize_payload
    from universal_tts import get_tts_status
    from utils.formatters import ConfiguredDraftRootError, get_configured_jianying_draft_root
    from utils.user_action_notifications import resolve_lark_command
    from utils.volc_asr_onboarding import build_volc_guide, configure_volc, inspect_volc_config

    from tools.recording.install_carnac import carnac_status

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
LarkCommandResolver = Callable[[], Sequence[str]]
FIRST_RUN_PYTHON = r".\.venv\Scripts\python.exe"

POINTER_REFERENCE_IMAGES = (
    "skills/auto-cut-subject-pointer-onboarding/assets/pointer-material-reference.png",
    "skills/auto-cut-subject-pointer-onboarding/assets/scale-reference-screenshot.png",
)
POINTER_INTAKE_FIELDS = (
    "stage_name",
    "subject_name",
    "pointer_png",
    "asset_id",
    "role",
    "anchor",
    "scale_references",
    "lesson",
    "time",
    "layout",
    "full_frame",
    "confirmed",
    "canvas_size",
    "visible_bbox",
    "approved_previews",
    "approved",
    "optional_motion_reference",
)
POINTER_INTAKE_SPECIFICATION = {
    "identity": [
        "stage_id",
        "subject_id",
        "stage_name",
        "subject_name",
        "display_name",
        "aliases",
        "detection_evidence",
    ],
    "pointer_assets": ["path", "sha256", "asset_id", "role", "anchor"],
    "placement_policies_optional": [
        "policy_id",
        "asset_role",
        "target_kind",
        "layout",
        "target_anchor",
        "gap_px",
    ],
    "scale_references": [
        "path",
        "sha256",
        "reference_id",
        "lesson",
        "time",
        "layout",
        "full_frame",
        "confirmed",
        "canvas_size",
        "visible_bbox",
    ],
    "approved_previews": ["path", "sha256", "approved", "note"],
    "incremental_optional": ["remove_aliases", "optional_motion_reference"],
}


def build_first_run_guide(repo_root: Path) -> dict[str, object]:
    del repo_root
    return {
        "schema_version": 1,
        "jianying_draft_delivery": {
            "status": "requires_target_path_confirmation",
            "first_use_requires_path": True,
            "setting_key": "currentCustomDraftPath",
            "setting_file": "JianyingPro/User Data/Config/globalSetting",
            "command": (
                f"{FIRST_RUN_PYTHON} scripts/jy_wrapper.py draft-root-check "
                "--expected-root <source_currentCustomDraftPath> --json"
            ),
            "missing_path_message": (
                "请在对方电脑创建与源电脑相同的剪映草稿路径：<source_currentCustomDraftPath>"
            ),
            "boundary": (
                "The plugin never silently chooses another default root and never edits "
                "globalSetting. Confirm the same absolute path before the first draft."
            ),
        },
        "volc_asr": build_volc_guide(),
        "cloud_materials": {
            "status": "degraded",
            "cache_root": "tmp/cloud_cache",
            "capture_root": "tmp/capture",
            "remote_urls_verified_on_first_run": False,
            "command": f"{FIRST_RUN_PYTHON} scripts/cloud_manager.py <asset_id>",
            "boundary": (
                "The package contains a static index. Each target machine downloads only "
                "validated remote assets into its own temporary cache."
            ),
        },
        "tts": {
            "status": "pending",
            "command": f"{FIRST_RUN_PYTHON} scripts/auto_cut_first_run.py tts-status --json",
            "sami": {
                "requires_local_jianying": True,
                "target_identity_copied": False,
                "tls_verification": "required",
                "network_probe": "not_run",
            },
            "edge": {
                "installed_on_first_run": True,
                "requires_network": True,
                "network_probe": "not_run",
            },
        },
        "subtitle_material_matching": {
            "status": "degraded",
            "provider": "deterministic_round_robin",
            "external_provider_bundled": False,
            "boundary": (
                "Deterministic matching is bundled. No attributable external AI matching "
                "provider is currently installed."
            ),
        },
        "carnac": {
            "status": "pending",
            "optional": True,
            "automatic_download": False,
            "command": f"{FIRST_RUN_PYTHON} tools/recording/install_carnac.py",
            "boundary": (
                "Install Carnac independently or set CARNAC_EXE. Recording remains available "
                "without the optional key overlay."
            ),
        },
        "favorites": {
            "status": "pending",
            "requires_target_resync": True,
            "requires_local_jianying": True,
            "command": (
                f"{FIRST_RUN_PYTHON} scripts/jy_wrapper.py " "sync-favorite-text-assets --json"
            ),
            "boundary": "Only assets found on this computer are indexed.",
        },
        "subject_pointer": {
            "status": "pending",
            "identity_source": "explicit_user_input_only",
            "default_profile": None,
            "reference_images": list(POINTER_REFERENCE_IMAGES),
            "intake_fields": list(POINTER_INTAKE_FIELDS),
            "intake_specification": POINTER_INTAKE_SPECIFICATION,
            "minimum_scale_reference_count": 2,
            "requires_binding_confirmation": True,
            "bind_command_requires_affirmative_confirmation": True,
            "registry_root": "data/subject-pointer-profiles.local",
            "commands": [
                f"{FIRST_RUN_PYTHON} skills/auto-cut-subject-pointer-onboarding/scripts/"
                "profile_registry.py identity --stage-name <stage_name> "
                "--subject-name <subject_name> --json",
                f"{FIRST_RUN_PYTHON} skills/auto-cut-subject-pointer-onboarding/scripts/"
                "profile_registry.py register --input <intake.json> "
                "--root data/subject-pointer-profiles.local --json",
                f"{FIRST_RUN_PYTHON} skills/auto-cut-subject-pointer-onboarding/scripts/"
                "profile_registry.py check --stage-id <stage_id> --subject-id <subject_id> "
                "--root data/subject-pointer-profiles.local --json",
                f"{FIRST_RUN_PYTHON} skills/auto-cut-subject-pointer-onboarding/scripts/"
                "profile_registry.py list --root data/subject-pointer-profiles.local --json",
                f"{FIRST_RUN_PYTHON} skills/auto-cut-subject-pointer-onboarding/scripts/"
                "profile_registry.py validate --root data/subject-pointer-profiles.local --json",
            ],
            "bind_command_after_confirmation": (
                f"{FIRST_RUN_PYTHON} skills/auto-cut-subject-pointer-onboarding/scripts/"
                "project_bindings.py bind --project-key <project_key> "
                "--project-path <project_path> --project-root <project_root> "
                "--stage-id <stage_id> --subject-id <subject_id> "
                "--root data/subject-pointer-profiles.local --json"
            ),
            "binding_boundary": (
                "Register and validate first. Bind or rebind a ready profile only after the "
                "user explicitly confirms the exact current project and profile."
            ),
        },
        "feishu": {
            "status": "pending",
            "optional": True,
            "notification_only": True,
            "reply_can_resolve": False,
            "authorization_is_target_local": True,
            "commands": [
                "lark-cli config init --new",
                "lark-cli auth login --scope <required_scope> --no-wait --json",
                f"{FIRST_RUN_PYTHON} scripts/auto_cut_notifications.py "
                "setup-preview --as <user|bot> "
                "--chat-id <approved_chat_id> --json",
                f"{FIRST_RUN_PYTHON} scripts/auto_cut_notifications.py "
                "setup-enable --as <user|bot> "
                "--chat-id <approved_chat_id> --preview-digest <preview_digest> "
                "--confirm-template auto_cut_user_action_required_v1 "
                "--confirm-privacy auto_cut_notification_privacy_v1 --json",
                f"{FIRST_RUN_PYTHON} scripts/auto_cut_notifications.py status --json",
            ],
            "authorization_boundary": (
                "Configure and authorize lark-cli on this computer. Bot identity does not use "
                "auth login; user identity must request only the required scopes."
            ),
        },
    }


def _default_runner(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _run_json_command(
    command: Sequence[str], *, repo_root: Path, runner: CommandRunner
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    try:
        completed = runner([str(part) for part in command], cwd=repo_root)
    except OSError as exc:
        completed = subprocess.CompletedProcess(
            [str(part) for part in command], 127, stdout="", stderr=str(exc)
        )
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        payload = None
    return completed, payload


def _feishu_status(
    repo_root: Path,
    *,
    runner: CommandRunner,
    python_executable: str | Path | None = None,
    lark_command_resolver: LarkCommandResolver | None = None,
) -> dict[str, object]:
    command = [
        str(python_executable or sys.executable),
        str(repo_root / "scripts" / "auto_cut_notifications.py"),
        "status",
        "--json",
    ]
    completed, payload = _run_json_command(command, repo_root=repo_root, runner=runner)
    data = payload.get("data", {}) if payload else {}
    allowed = {
        key: data[key]
        for key in ("configured", "enabled", "command_available", "provider", "identity")
        if key in data
    }
    command_available = data.get("command_available") is True
    probe_ready = completed.returncode == 0 and payload is not None and payload.get("ok") is True
    configured = data.get("configured") is True
    locally_enabled = (
        probe_ready and command_available and configured and data.get("enabled") is True
    )
    authorization_verified = False
    scope_verified = False
    if locally_enabled:
        resolver = lark_command_resolver or resolve_lark_command
        try:
            command_prefix = list(resolver())
        except OSError:
            command_prefix = []
        if command_prefix:
            identity = data.get("identity")
            auth_completed, auth_payload = _run_json_command(
                [*command_prefix, "auth", "status", "--verify", "--json"],
                repo_root=repo_root,
                runner=runner,
            )
            identities = auth_payload.get("identities") if auth_payload else None
            identity_status = identities.get(identity) if isinstance(identities, dict) else None
            authorization_verified = bool(
                auth_completed.returncode == 0
                and isinstance(identity, str)
                and identity in {"bot", "user"}
                and isinstance(identity_status, dict)
                and identity_status.get("available") is True
                and identity_status.get("status") == "ready"
            )
            required_scope = "im:message.send_as_user" if identity == "user" else "im:message"
            scope_completed, scope_payload = _run_json_command(
                [
                    *command_prefix,
                    "auth",
                    "check",
                    "--scope",
                    required_scope,
                    "--json",
                ],
                repo_root=repo_root,
                runner=runner,
            )
            granted = scope_payload.get("granted") if scope_payload else None
            missing = scope_payload.get("missing") if scope_payload else None
            scope_verified = bool(
                scope_completed.returncode == 0
                and scope_payload is not None
                and scope_payload.get("ok") is True
                and isinstance(granted, list)
                and required_scope in granted
                and missing in (None, [])
            )
    enabled = locally_enabled and authorization_verified and scope_verified
    status = (
        "ready" if enabled else "pending" if probe_ready and command_available else "unavailable"
    )
    code = (
        "notifications_enabled"
        if enabled
        else (
            "authorization_required"
            if locally_enabled
            else (
                "notifications_not_configured"
                if probe_ready and command_available
                else (
                    "notification_command_unavailable"
                    if probe_ready
                    else "notification_status_failed"
                )
            )
        )
    )
    allowed.update(
        {
            "authorization_verified": authorization_verified,
            "scope_verified": scope_verified,
        }
    )
    return {
        "status": status,
        "code": code,
        "details": sanitize_payload(allowed, repo_root=repo_root),
    }


def collect_first_run_status(
    repo_root: Path,
    *,
    runner: CommandRunner | None = None,
    python_executable: str | Path | None = None,
    lark_command_resolver: LarkCommandResolver | None = None,
) -> dict[str, object]:
    root = Path(repo_root).resolve()
    data_root = root / "data"
    favorite_indexes = list(data_root.glob("*favorite*.local.csv")) if data_root.is_dir() else []
    pointer_root = data_root / "subject-pointer-profiles.local"
    profile_files: set[Path] = set()
    if pointer_root.is_dir():
        profile_files.update(pointer_root.glob("*.json"))
        profile_files.update(pointer_root.rglob("profile.json"))
        profile_files = {
            path
            for path in profile_files
            if path.name not in {"catalog.json", "project-bindings.json"}
        }
    try:
        configured_root = get_configured_jianying_draft_root(require_exists=True)
        jianying_delivery = {
            "status": "ready",
            "configured_root": str(configured_root),
            "setting_key": "currentCustomDraftPath",
            "first_use_requires_path": True,
            "message": f"剪映草稿路径已读取：{configured_root}",
        }
    except ConfiguredDraftRootError as exc:
        jianying_delivery = {
            "status": "needs_user_action",
            "configured_root": None,
            "setting_key": "currentCustomDraftPath",
            "first_use_requires_path": True,
            "message": (
                "请先确认对方电脑的剪映草稿路径；请在对方电脑创建与源电脑相同的剪映草稿路径："
                f"<source_currentCustomDraftPath>（{exc}）"
            ),
        }
    return {
        "schema_version": 1,
        "jianying_draft_delivery": jianying_delivery,
        "favorites": {
            "status": "pending",
            "local_index_count": len(favorite_indexes),
        },
        "subject_pointer": {
            "status": "pending",
            "profile_count": len(profile_files),
        },
        "feishu": _feishu_status(
            root,
            runner=runner or _default_runner,
            python_executable=python_executable,
            lark_command_resolver=lark_command_resolver,
        ),
        "volc_asr": inspect_volc_config(root),
        "cloud_materials": cloud_material_status(root),
        "tts": get_tts_status(),
        "subtitle_material_matching": {
            "status": "degraded",
            "code": "deterministic_fallback",
            "provider": "deterministic_round_robin",
        },
        "carnac": carnac_status(),
    }


def _favorites_sync(
    repo_root: Path,
    *,
    runner: CommandRunner,
    python_executable: str | Path | None = None,
) -> tuple[int, dict[str, object]]:
    command = [
        str(python_executable or sys.executable),
        str(repo_root / "scripts" / "jy_wrapper.py"),
        "sync-favorite-text-assets",
        "--json",
    ]
    completed, payload = _run_json_command(command, repo_root=repo_root, runner=runner)
    ok = completed.returncode == 0 and payload is not None and payload.get("ok") is True
    result = {
        "status": "ready" if ok else "failed",
        "code": "favorites_synchronized" if ok else "favorites_sync_failed",
        "result": sanitize_payload(payload or {}, repo_root=repo_root),
    }
    return (0 if ok else 1), result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize target-owned Auto-Cut capabilities.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in (
        "status",
        "guide",
        "favorites-sync",
        "pointer-guide",
        "feishu-status",
        "volc-guide",
        "volc-status",
        "volc-config",
        "tts-status",
    ):
        child = commands.add_parser(command)
        child.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _emit(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path | None = None,
    runner: CommandRunner | None = None,
    python_executable: str | Path | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    effective_runner = runner or _default_runner
    effective_python = str(python_executable or sys.executable)
    if args.command == "guide":
        payload = build_first_run_guide(root)
        exit_code = 0
    elif args.command == "pointer-guide":
        payload = build_first_run_guide(root)["subject_pointer"]
        exit_code = 0
    elif args.command == "volc-guide":
        payload = build_volc_guide()
        exit_code = 0
    elif args.command == "volc-status":
        payload = inspect_volc_config(root)
        exit_code = 0
    elif args.command == "volc-config":
        payload = configure_volc(root)
        exit_code = 0
    elif args.command == "tts-status":
        payload = get_tts_status()
        exit_code = 0
    elif args.command == "status":
        payload = collect_first_run_status(
            root,
            runner=effective_runner,
            python_executable=effective_python,
        )
        exit_code = 0
    elif args.command == "feishu-status":
        payload = _feishu_status(
            root,
            runner=effective_runner,
            python_executable=effective_python,
        )
        exit_code = 0 if payload["status"] != "unavailable" else 1
    else:
        exit_code, payload = _favorites_sync(
            root,
            runner=effective_runner,
            python_executable=effective_python,
        )
    _emit(payload, as_json=args.as_json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
