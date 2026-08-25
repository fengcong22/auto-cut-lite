"""Install Auto-Cut Lite skills into its stable repository-scoped workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE_NAME = "Auto-cut-lite"
PLUGIN_NAME = "auto-cut-lite"
WORKSPACE_SKILL_PAYLOAD = Path("workspace-payload") / "skills"
RECEIPT_SCHEMA_VERSION = 1
REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _lexical(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & REPARSE_ATTRIBUTE
    )


def _assert_descendant(path: Path, root: Path, *, allow_root: bool = False) -> None:
    candidate = _lexical(path)
    boundary = _lexical(root)
    try:
        relative = candidate.relative_to(boundary)
    except ValueError:
        raise ValueError(f"path escapes expected root: {candidate}") from None
    if not allow_root and not relative.parts:
        raise ValueError(f"path must be below expected root: {candidate}")


def _assert_no_reparse_components(path: Path, boundary: Path) -> None:
    candidate = _lexical(path)
    stop = _lexical(boundary)
    _assert_descendant(candidate, stop, allow_root=True)
    current = candidate
    while True:
        if os.path.lexists(current) and _is_reparse(current):
            raise ValueError(f"deployment path contains a reparse point: {current}")
        if current == stop:
            return
        current = current.parent


def _assert_regular_tree(root: Path) -> None:
    if not root.is_dir() or _is_reparse(root):
        raise ValueError(f"source is not a regular directory: {root}")
    for path in root.rglob("*"):
        if _is_reparse(path):
            raise ValueError(f"source contains a reparse point: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    _assert_regular_tree(root)
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any], state_root: Path) -> None:
    _assert_descendant(path, state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _safe_remove_tree(path: Path, allowed_parent: Path) -> None:
    candidate = _lexical(path)
    boundary = _lexical(allowed_parent)
    _assert_descendant(candidate, boundary)
    if os.path.lexists(candidate):
        _assert_no_reparse_components(candidate, boundary)
        shutil.rmtree(candidate)


def _expected_state_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ValueError("LOCALAPPDATA must be set")
    return _lexical(Path(local_app_data) / "Auto-Cut" / PLUGIN_NAME)


def _is_same_or_descendant(path: Path, root: Path) -> bool:
    try:
        _lexical(path).relative_to(_lexical(root))
    except ValueError:
        return False
    return True


def _validate_workspace_root(workspace_root: Path, state_root: Path) -> Path:
    if not workspace_root.is_absolute():
        raise ValueError("workspace root must be an absolute path")
    candidate = _lexical(workspace_root)
    if candidate.name != WORKSPACE_NAME:
        raise ValueError(f"workspace root folder name must be exactly: {WORKSPACE_NAME}")
    if not candidate.anchor:
        raise ValueError("workspace root has no filesystem anchor")
    anchor = _lexical(Path(candidate.anchor))
    if candidate == anchor:
        raise ValueError("workspace root cannot be a filesystem root")
    if _is_same_or_descendant(candidate, state_root) or _is_same_or_descendant(
        state_root, candidate
    ):
        raise ValueError("workspace root cannot overlap the Auto-Cut runtime state root")
    _assert_no_reparse_components(candidate, anchor)
    return candidate


def _validate_state_destinations(state_root: Path, receipt_path: Path) -> Path:
    expected_state = _expected_state_root()
    if _lexical(state_root) != expected_state:
        raise ValueError(f"state root must be exactly: {expected_state}")
    expected_receipt = expected_state / "workspace-install-receipt.json"
    if _lexical(receipt_path) != expected_receipt:
        raise ValueError(f"receipt path must be exactly: {expected_receipt}")
    _assert_no_reparse_components(expected_state, _lexical(Path(os.environ["LOCALAPPDATA"])))
    return expected_state


def _validate_destinations(workspace_root: Path, state_root: Path, receipt_path: Path) -> None:
    expected_state = _validate_state_destinations(state_root, receipt_path)
    _validate_workspace_root(workspace_root, expected_state)


def _read_active_receipt(receipt_path: Path) -> dict[str, Any] | None:
    if not receipt_path.is_file():
        return None
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"existing workspace receipt is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValueError("existing workspace receipt is invalid")
    if payload.get("status") != "installed":
        return None
    if payload.get("plugin_name") != PLUGIN_NAME:
        raise ValueError("existing workspace receipt has an invalid plugin identity")
    return payload


def _managed_skill_name(name: Any) -> bool:
    return isinstance(name, str) and (name == "auto-cut" or name.startswith("auto-cut-"))


def _receipt_details(
    payload: dict[str, Any], state_root: Path, *, verify_installed_files: bool
) -> dict[str, Any]:
    if (
        payload.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or payload.get("status") != "installed"
        or payload.get("plugin_name") != PLUGIN_NAME
    ):
        raise ValueError("workspace receipt is invalid")
    workspace_value = payload.get("workspace_root")
    if not isinstance(workspace_value, str) or not Path(workspace_value).is_absolute():
        raise ValueError("workspace receipt target is invalid")
    workspace_root = _validate_workspace_root(Path(workspace_value), state_root)
    workspace_skills = workspace_root / ".codex" / "skills"
    workspace_agents = workspace_root / "AGENTS.md"

    installed_names = payload.get("installed_skill_names")
    if (
        not isinstance(installed_names, list)
        or not installed_names
        or any(not _managed_skill_name(name) for name in installed_names)
        or len(set(installed_names)) != len(installed_names)
    ):
        raise ValueError("workspace receipt contains invalid managed skill names")
    expected_hashes = payload.get("installed_skill_sha256")
    if not isinstance(expected_hashes, dict):
        raise ValueError("workspace receipt contains invalid skill hashes")
    expected_agents_hash = payload.get("installed_agents_sha256")
    if not isinstance(expected_agents_hash, str):
        raise ValueError("workspace receipt contains an invalid AGENTS.md hash")
    backup_value = payload.get("backup_root")
    if not isinstance(backup_value, str) or not Path(backup_value).is_absolute():
        raise ValueError("workspace receipt contains an invalid backup root")
    backup_root = _lexical(backup_value)
    _assert_descendant(backup_root, state_root / "workspace-backups")

    if verify_installed_files:
        for name in installed_names:
            target = workspace_skills / name
            expected_hash = expected_hashes.get(name)
            if (
                not isinstance(expected_hash, str)
                or not target.is_dir()
                or _tree_sha256(target) != expected_hash
            ):
                raise ValueError(
                    f"workspace skill changed after deployment; refusing operation: {name}"
                )
        if not workspace_agents.is_file() or _sha256(workspace_agents) != expected_agents_hash:
            raise ValueError("workspace AGENTS.md changed after deployment; refusing operation")

    return {
        "workspace_root": workspace_root,
        "workspace_skills": workspace_skills,
        "workspace_agents": workspace_agents,
        "installed_names": installed_names,
        "expected_hashes": expected_hashes,
        "expected_agents_hash": expected_agents_hash,
        "backup_root": backup_root,
    }


def _read_plugin_manifest(plugin_root: Path) -> dict[str, Any]:
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"plugin manifest is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("name") != PLUGIN_NAME:
        raise ValueError("plugin manifest identity is invalid")
    if "skills" in payload:
        raise ValueError("plugin manifest must not expose user-scoped skills")
    if (plugin_root / "skills").exists():
        raise ValueError("plugin root must not contain a top-level skills directory")
    return payload


def _discover_skills(plugin_root: Path) -> list[Path]:
    skills_root = plugin_root / WORKSPACE_SKILL_PAYLOAD
    _assert_regular_tree(skills_root)
    skills: list[Path] = []
    for child in sorted(skills_root.iterdir(), key=lambda path: path.name.casefold()):
        if not child.is_dir() or not (child / "SKILL.md").is_file():
            continue
        if child.name != "auto-cut" and not child.name.startswith("auto-cut-"):
            raise ValueError(f"unexpected workspace skill name: {child.name}")
        if not (child / "agents" / "openai.yaml").is_file():
            raise ValueError(f"workspace skill metadata is missing: {child.name}")
        _assert_regular_tree(child)
        skills.append(child)
    if not skills:
        raise ValueError("plugin contains no Auto-Cut workspace skills")
    return skills


def _managed_skill_entries(skills_root: Path) -> list[Path]:
    if not skills_root.exists():
        return []
    if not skills_root.is_dir() or _is_reparse(skills_root):
        raise ValueError(f"workspace skills root is unsafe: {skills_root}")
    managed: list[Path] = []
    for child in sorted(skills_root.iterdir(), key=lambda path: path.name.casefold()):
        if child.name == "auto-cut" or child.name.startswith("auto-cut-"):
            if not child.is_dir() or _is_reparse(child):
                raise ValueError(f"managed workspace skill is unsafe: {child}")
            _assert_regular_tree(child)
            managed.append(child)
    return managed


def _restore_install(
    *,
    workspace_agents: Path,
    workspace_skills: Path,
    installed_names: list[str],
    backup_root: Path,
    agents_installed: bool,
    agents_backed_up: bool,
    backed_up_names: list[str],
) -> None:
    failed_root = backup_root / "failed-new"
    failed_skills = failed_root / "skills"
    for name in installed_names:
        target = workspace_skills / name
        if target.exists():
            failed_skills.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(failed_skills / name))
    if agents_installed and workspace_agents.exists():
        failed_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(workspace_agents), str(failed_root / "AGENTS.md"))
    backup_skills = backup_root / "skills"
    workspace_skills.mkdir(parents=True, exist_ok=True)
    for name in backed_up_names:
        source = backup_skills / name
        if source.exists():
            shutil.move(str(source), str(workspace_skills / name))
    backup_agents = backup_root / "AGENTS.md"
    if agents_backed_up and backup_agents.exists():
        workspace_agents.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup_agents), str(workspace_agents))


def _restore_relocated_workspace(
    *,
    previous_details: dict[str, Any],
    relocation_root: Path,
    moved_names: list[str],
    agents_moved: bool,
) -> None:
    previous_skills = previous_details["workspace_skills"]
    previous_agents = previous_details["workspace_agents"]
    relocated_skills = relocation_root / "skills"
    previous_skills.mkdir(parents=True, exist_ok=True)
    for name in moved_names:
        source = relocated_skills / name
        destination = previous_skills / name
        if destination.exists():
            raise ValueError(f"previous workspace target is no longer empty: {destination}")
        if source.exists():
            shutil.move(str(source), str(destination))
    relocated_agents = relocation_root / "AGENTS.md"
    if agents_moved and relocated_agents.exists():
        if previous_agents.exists():
            raise ValueError(f"previous workspace target is no longer empty: {previous_agents}")
        previous_agents.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(relocated_agents), str(previous_agents))


def install_workspace(
    *,
    plugin_root: Path,
    workspace_root: Path,
    state_root: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    plugin_root = _lexical(plugin_root)
    if not workspace_root.is_absolute():
        raise ValueError("workspace root must be an absolute path")
    workspace_root = _lexical(workspace_root)
    state_root = _lexical(state_root)
    receipt_path = _lexical(receipt_path)
    _validate_destinations(workspace_root, state_root, receipt_path)
    previous_receipt = _read_active_receipt(receipt_path)
    previous_details: dict[str, Any] | None = None
    if previous_receipt is not None:
        active_details = _receipt_details(previous_receipt, state_root, verify_installed_files=True)
        previous_root = active_details["workspace_root"]
        if previous_root != workspace_root:
            if _is_same_or_descendant(previous_root, workspace_root) or _is_same_or_descendant(
                workspace_root, previous_root
            ):
                raise ValueError("old and new workspace roots cannot overlap")
            previous_details = active_details
    _assert_regular_tree(plugin_root)
    manifest = _read_plugin_manifest(plugin_root)
    source_agents = plugin_root / "AGENTS.md"
    if not source_agents.is_file() or _is_reparse(source_agents):
        raise ValueError("portable AGENTS.md is missing or unsafe")
    source_skills = _discover_skills(plugin_root)

    operation_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "." + uuid.uuid4().hex
    staging_parent = state_root / "workspace-staging"
    staging_root = staging_parent / operation_id
    backup_root = state_root / "workspace-backups" / operation_id
    staged_skills = staging_root / "skills"
    staged_agents = staging_root / "AGENTS.md"
    workspace_skills = workspace_root / ".codex" / "skills"
    workspace_agents = workspace_root / "AGENTS.md"

    staging_root.mkdir(parents=True, exist_ok=False)
    staged_skills.mkdir(parents=True)
    try:
        shutil.copy2(source_agents, staged_agents)
        for source in source_skills:
            shutil.copytree(source, staged_skills / source.name)
        _assert_regular_tree(staging_root)

        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace_skills.mkdir(parents=True, exist_ok=True)
        _assert_no_reparse_components(workspace_skills, workspace_root)
        _assert_no_reparse_components(workspace_agents, workspace_root)

        existing_skills = _managed_skill_entries(workspace_skills)
        if workspace_agents.exists() and (
            not workspace_agents.is_file() or _is_reparse(workspace_agents)
        ):
            raise ValueError(f"existing workspace AGENTS.md is unsafe: {workspace_agents}")
        backed_up_names: list[str] = []
        agents_backed_up = workspace_agents.exists()
        backup_root.mkdir(parents=True, exist_ok=False)
        backup_skills = backup_root / "skills"
        installed_names: list[str] = []
        agents_moved_to_backup = False
        agents_installed = False
        relocated_names: list[str] = []
        relocated_agents = False
        relocation_root = backup_root / "relocated-from"
        try:
            if existing_skills:
                backup_skills.mkdir(parents=True)
                for existing in existing_skills:
                    shutil.move(str(existing), str(backup_skills / existing.name))
                    backed_up_names.append(existing.name)
            if agents_backed_up:
                shutil.move(str(workspace_agents), str(backup_root / "AGENTS.md"))
                agents_moved_to_backup = True
            if previous_details is not None:
                relocated_skills = relocation_root / "skills"
                relocated_skills.mkdir(parents=True)
                for name in previous_details["installed_names"]:
                    shutil.move(
                        str(previous_details["workspace_skills"] / name),
                        str(relocated_skills / name),
                    )
                    relocated_names.append(name)
                relocation_root.mkdir(parents=True, exist_ok=True)
                shutil.move(
                    str(previous_details["workspace_agents"]),
                    str(relocation_root / "AGENTS.md"),
                )
                relocated_agents = True
            for source in sorted(staged_skills.iterdir(), key=lambda path: path.name.casefold()):
                shutil.move(str(source), str(workspace_skills / source.name))
                installed_names.append(source.name)
            shutil.move(str(staged_agents), str(workspace_agents))
            agents_installed = True

            skill_hashes = {name: _tree_sha256(workspace_skills / name) for name in installed_names}
            receipt: dict[str, Any] = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "status": "installed",
                "plugin_name": PLUGIN_NAME,
                "plugin_version": manifest.get("version"),
                "workspace_root": str(workspace_root),
                "workspace_label": WORKSPACE_NAME,
                "workspace_scope": "repo",
                "workspace_skills_root": str(workspace_skills),
                "workspace_agents_path": str(workspace_agents),
                "deployment_report_path": str(state_root / "deployment-report.json"),
                "plugin_root": str(plugin_root),
                "runtime_root": str(plugin_root / "runtime"),
                "workspace_skill_count": len(installed_names),
                "installed_skill_names": installed_names,
                "installed_skill_sha256": skill_hashes,
                "installed_agents_sha256": _sha256(workspace_agents),
                "plugin_manifest_exposes_skills": False,
                "plugin_top_level_skills_present": False,
                "workspace_skill_payload": WORKSPACE_SKILL_PAYLOAD.as_posix(),
                "backup_root": str(backup_root),
                "backed_up_skill_names": backed_up_names,
                "agents_backed_up": agents_backed_up,
                "installed_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            if previous_details is not None:
                receipt.update(
                    {
                        "workspace_action": "relocated",
                        "relocated_from_workspace_root": str(previous_details["workspace_root"]),
                        "relocated_skill_names": relocated_names,
                        "previous_install_receipt": previous_receipt,
                    }
                )
            else:
                receipt["workspace_action"] = (
                    "refreshed" if previous_receipt is not None else "installed"
                )
            _atomic_write_json(receipt_path, receipt, state_root)
        except Exception as exc:
            restore_errors: list[str] = []
            try:
                _restore_install(
                    workspace_agents=workspace_agents,
                    workspace_skills=workspace_skills,
                    installed_names=installed_names,
                    backup_root=backup_root,
                    agents_installed=agents_installed,
                    agents_backed_up=agents_moved_to_backup,
                    backed_up_names=backed_up_names,
                )
            except Exception as restore_exc:  # pragma: no cover - catastrophic filesystem failure
                restore_errors.append(f"new workspace restore failed: {restore_exc}")
            if previous_details is not None:
                try:
                    _restore_relocated_workspace(
                        previous_details=previous_details,
                        relocation_root=relocation_root,
                        moved_names=relocated_names,
                        agents_moved=relocated_agents,
                    )
                except (
                    Exception
                ) as restore_exc:  # pragma: no cover - catastrophic filesystem failure
                    restore_errors.append(f"previous workspace restore failed: {restore_exc}")
            if restore_errors:
                raise RuntimeError(f"{exc} | {' | '.join(restore_errors)}") from exc
            raise
        return receipt
    except Exception:
        if staging_root.exists():
            _safe_remove_tree(staging_root, staging_parent)
        raise
    finally:
        if staging_root.exists():
            _safe_remove_tree(staging_root, staging_parent)


def rollback_workspace(*, receipt_path: Path) -> dict[str, Any]:
    receipt_path = _lexical(receipt_path)
    state_root = _expected_state_root()
    _validate_state_destinations(state_root, receipt_path)
    payload = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValueError("workspace receipt is invalid")
    if payload.get("status") != "installed":
        return {"status": payload.get("status"), "action": "unchanged"}

    details = _receipt_details(payload, state_root, verify_installed_files=True)
    workspace_root = details["workspace_root"]
    workspace_skills = details["workspace_skills"]
    workspace_agents = details["workspace_agents"]
    backup_root = details["backup_root"]
    installed_names = details["installed_names"]

    previous_receipt = payload.get("previous_install_receipt")
    previous_details: dict[str, Any] | None = None
    relocation_root = backup_root / "relocated-from"
    if previous_receipt is not None:
        if not isinstance(previous_receipt, dict):
            raise ValueError("previous workspace receipt is invalid")
        previous_details = _receipt_details(
            previous_receipt, state_root, verify_installed_files=False
        )
        if payload.get("relocated_skill_names") != previous_details["installed_names"]:
            raise ValueError("relocated workspace skill inventory is invalid")
        if _managed_skill_entries(previous_details["workspace_skills"]):
            raise ValueError("previous workspace gained managed skills after relocation")
        if previous_details["workspace_agents"].exists():
            raise ValueError("previous workspace gained AGENTS.md after relocation")
        for name in previous_details["installed_names"]:
            relocated = relocation_root / "skills" / name
            if not relocated.is_dir() or _tree_sha256(relocated) != previous_details[
                "expected_hashes"
            ].get(name):
                raise ValueError(f"relocated workspace backup is invalid: {name}")
        relocated_agents = relocation_root / "AGENTS.md"
        if (
            not relocated_agents.is_file()
            or _sha256(relocated_agents) != previous_details["expected_agents_hash"]
        ):
            raise ValueError("relocated AGENTS.md backup is invalid")

        _restore_relocated_workspace(
            previous_details=previous_details,
            relocation_root=relocation_root,
            moved_names=previous_details["installed_names"],
            agents_moved=True,
        )

    _restore_install(
        workspace_agents=workspace_agents,
        workspace_skills=workspace_skills,
        installed_names=installed_names,
        backup_root=backup_root,
        agents_installed=True,
        agents_backed_up=bool(payload.get("agents_backed_up")),
        backed_up_names=list(payload.get("backed_up_skill_names", [])),
    )
    if previous_receipt is not None:
        _atomic_write_json(receipt_path, previous_receipt, state_root)
    else:
        payload["status"] = "rolled_back"
        payload["rolled_back_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(receipt_path, payload, state_root)
    return {
        "status": "rolled_back",
        "action": (
            "restored_relocated_workspace"
            if previous_details is not None
            else "restored_previous_workspace"
        ),
        "workspace_root": str(workspace_root),
        "restored_workspace_root": (
            str(previous_details["workspace_root"])
            if previous_details is not None
            else str(workspace_root)
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--plugin-root", type=Path, required=True)
    install.add_argument("--workspace-root", type=Path, required=True)
    install.add_argument("--state-root", type=Path, required=True)
    install.add_argument("--receipt-path", type=Path, required=True)
    install.add_argument("--json", action="store_true")
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--receipt-path", type=Path, required=True)
    rollback.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "install":
            result = install_workspace(
                plugin_root=args.plugin_root,
                workspace_root=args.workspace_root,
                state_root=args.state_root,
                receipt_path=args.receipt_path,
            )
        else:
            result = rollback_workspace(receipt_path=args.receipt_path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for key, value in result.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
