from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_NAME = "auto-cut-lite"
TRANSACTION_SCHEMA_VERSION = 1
ENVIRONMENT_RECEIPT = ".auto-cut-lite-environment.json"


@dataclass(frozen=True)
class EnvironmentSpec:
    name: str
    relative_environment: str
    relative_requirements: str


ENVIRONMENT_SPECS = (
    EnvironmentSpec("main", ".runtime-venv", "runtime/requirements.txt"),
    EnvironmentSpec("audio", "runtime/.venv-audio", "runtime/requirements-audio.lock"),
)


def _lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_descendant(path: Path, parent: Path) -> None:
    candidate = _lexical(path)
    boundary = _lexical(parent)
    try:
        candidate.relative_to(boundary)
    except ValueError as exc:
        raise ValueError(f"path escapes managed boundary: {candidate}") from exc
    if candidate == boundary:
        raise ValueError(f"managed target cannot equal its boundary: {candidate}")


def _assert_no_reparse_ancestors(path: Path, boundary: Path) -> None:
    candidate = _lexical(path)
    root = _lexical(boundary)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes managed boundary: {candidate}") from exc
    cursor = candidate
    while True:
        if os.path.lexists(cursor) and _is_reparse(cursor):
            raise ValueError(f"managed path has a reparse-point ancestor: {cursor}")
        if cursor == root:
            return
        cursor = cursor.parent


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _assert_regular_tree(root: Path) -> None:
    if not root.is_dir() or _is_reparse(root):
        raise ValueError(f"managed environment is missing or unsafe: {root}")
    for path in root.rglob("*"):
        if _is_reparse(path):
            raise ValueError(f"managed environment contains a reparse point: {path}")


def _safe_remove_tree(path: Path, parent: Path) -> None:
    candidate = _lexical(path)
    boundary = _lexical(parent)
    _assert_descendant(candidate, boundary)
    if os.path.lexists(candidate):
        _assert_regular_tree(candidate)
        shutil.rmtree(candidate)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _python_identity(python_path: Path) -> dict[str, Any]:
    probe = (
        "import json,platform,struct,sys;"
        "print(json.dumps({'implementation':platform.python_implementation(),"
        "'version':platform.python_version(),'bits':struct.calcsize('P')*8,"
        "'major':sys.version_info.major,'minor':sys.version_info.minor}))"
    )
    result = _run([str(python_path), "-c", probe])
    if result.returncode != 0:
        raise ValueError(f"Python identity probe failed: {python_path}")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError(f"Python identity probe returned invalid JSON: {python_path}") from exc
    required = {"implementation", "version", "bits", "major", "minor"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ValueError(f"Python identity probe is incomplete: {python_path}")
    return {key: payload[key] for key in sorted(required)}


def _normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _requirement_pins(path: Path) -> dict[str, str]:
    if not path.is_file() or _is_reparse(path):
        raise ValueError(f"requirements lock is missing or unsafe: {path}")
    pins: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if match is None:
            raise ValueError(f"requirements lock contains a non-exact entry: {line}")
        name = _normalize_distribution(match.group(1))
        if name in pins:
            raise ValueError(f"requirements lock contains a duplicate distribution: {name}")
        pins[name] = match.group(2)
    if not pins:
        raise ValueError(f"requirements lock has no pinned dependencies: {path}")
    return pins


def _installed_versions(python_path: Path) -> dict[str, str]:
    result = _run([str(python_path), "-m", "pip", "list", "--format=json"])
    if result.returncode != 0:
        raise ValueError(f"pip inventory failed: {python_path}")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"pip inventory returned invalid JSON: {python_path}") from exc
    if not isinstance(rows, list):
        raise ValueError(f"pip inventory is not an array: {python_path}")
    inventory: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError(f"pip inventory contains an invalid row: {python_path}")
        inventory[_normalize_distribution(row["name"])] = str(row.get("version") or "")
    return inventory


def _pip_check(python_path: Path) -> bool:
    return _run([str(python_path), "-m", "pip", "check"]).returncode == 0


def _numeric_version(value: str) -> tuple[int, ...] | None:
    if re.fullmatch(r"\d+(?:\.\d+)*", value) is None:
        return None
    return tuple(int(part) for part in value.split("."))


def choose_action(
    *,
    previous_environment_exists: bool,
    python_matches: bool,
    pip_healthy: bool,
    previous_lock_sha256: str | None,
    requested_lock_sha256: str,
    installed_versions: dict[str, str],
    requested_versions: dict[str, str],
) -> tuple[str, str]:
    if not previous_environment_exists:
        return "recreate", "environment_missing"
    if not python_matches:
        return "recreate", "python_identity_mismatch"
    if not pip_healthy:
        return "recreate", "environment_unhealthy"
    if previous_lock_sha256 is None:
        return "recreate", "previous_lock_missing"
    missing = sorted(set(requested_versions) - set(installed_versions))
    if missing:
        return "recreate", "dependency_missing"
    exact = all(installed_versions[name] == version for name, version in requested_versions.items())
    if previous_lock_sha256 == requested_lock_sha256 and exact:
        return "reuse", "lock_python_and_health_match"
    if previous_lock_sha256 == requested_lock_sha256:
        return "recreate", "installed_versions_drifted_from_unchanged_lock"

    lower_seen = False
    for name, requested in requested_versions.items():
        installed = installed_versions[name]
        installed_key = _numeric_version(installed)
        requested_key = _numeric_version(requested)
        if installed_key is None or requested_key is None or installed_key > requested_key:
            return "recreate", "dependency_version_incompatible"
        lower_seen = lower_seen or installed_key < requested_key
    if lower_seen:
        return "incremental_upgrade", "compatible_lower_versions"
    return "recreate", "lock_changed_without_compatible_upgrade"


def _environment_python(environment: Path) -> Path:
    return environment / "Scripts" / "python.exe"


def _same_python(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left == right


def _plan_environment(
    spec: EnvironmentSpec,
    *,
    plugin_root: Path,
    previous_plugin_root: Path | None,
    base_identity: dict[str, Any],
) -> dict[str, Any]:
    requested_lock = plugin_root / Path(spec.relative_requirements)
    requested_versions = _requirement_pins(requested_lock)
    requested_sha = _sha256(requested_lock)
    previous_environment = (
        previous_plugin_root / Path(spec.relative_environment)
        if previous_plugin_root is not None
        else None
    )
    previous_lock = (
        previous_plugin_root / Path(spec.relative_requirements)
        if previous_plugin_root is not None
        else None
    )
    result: dict[str, Any] = {
        "name": spec.name,
        "requested_lock_sha256": requested_sha,
        "requested_versions": requested_versions,
        "previous_environment": str(previous_environment) if previous_environment else None,
    }
    if previous_environment is None or not previous_environment.is_dir():
        action, reason = choose_action(
            previous_environment_exists=False,
            python_matches=False,
            pip_healthy=False,
            previous_lock_sha256=None,
            requested_lock_sha256=requested_sha,
            installed_versions={},
            requested_versions=requested_versions,
        )
    else:
        try:
            _assert_regular_tree(previous_environment)
            previous_python = _environment_python(previous_environment)
            previous_identity = _python_identity(previous_python)
            installed = _installed_versions(previous_python)
            healthy = _pip_check(previous_python)
        except (OSError, ValueError):
            previous_identity = {}
            installed = {}
            healthy = False
        previous_sha = (
            _sha256(previous_lock)
            if previous_lock is not None and previous_lock.is_file() and not _is_reparse(previous_lock)
            else None
        )
        action, reason = choose_action(
            previous_environment_exists=True,
            python_matches=_same_python(previous_identity, base_identity),
            pip_healthy=healthy,
            previous_lock_sha256=previous_sha,
            requested_lock_sha256=requested_sha,
            installed_versions=installed,
            requested_versions=requested_versions,
        )
        result.update(
            {
                "previous_lock_sha256": previous_sha,
                "previous_python_identity": previous_identity or None,
                "previous_pip_check": "pass" if healthy else "failed",
            }
        )
    result.update({"action": action, "reason": reason})
    return result


def _copy_environment(source: Path, destination: Path) -> None:
    _assert_regular_tree(source)
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=False)


def _install_lock(python_path: Path, requirements: Path) -> None:
    result = _run(
        [
            str(python_path),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            "-r",
            str(requirements),
        ]
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = detail[-1] if detail else "pip returned a failure code"
        raise RuntimeError(f"dependency installation failed: {suffix}")


def _verify_environment(
    environment: Path, requested_versions: dict[str, str], base_identity: dict[str, Any]
) -> dict[str, Any]:
    python_path = _environment_python(environment)
    identity = _python_identity(python_path)
    if not _same_python(identity, base_identity):
        raise ValueError(f"installed environment Python identity mismatch: {environment}")
    if not _pip_check(python_path):
        raise ValueError(f"installed environment failed pip check: {environment}")
    installed = _installed_versions(python_path)
    mismatches = {
        name: {"expected": version, "actual": installed.get(name)}
        for name, version in requested_versions.items()
        if installed.get(name) != version
    }
    if mismatches:
        raise ValueError(f"installed dependency versions do not match the lock: {mismatches}")
    return {"python_identity": identity, "pip_check": "pass", "dependency_check": "pass"}


def _rollback_records(records: list[dict[str, Any]], state_root: Path) -> None:
    for record in reversed(records):
        if not record.get("changed"):
            continue
        target = _lexical(Path(record["target_environment"]))
        _assert_descendant(target, state_root)
        backup_value = record.get("backup_environment")
        if backup_value:
            backup = _lexical(Path(backup_value))
            _assert_descendant(backup, state_root / "dependency-backups")
            if not os.path.lexists(backup):
                # The recovery intent is persisted before the move. A present
                # target with no backup means the move never started.
                if record.get("backup_move_status") == "pending" and os.path.lexists(target):
                    _assert_regular_tree(target)
                    continue
                raise ValueError(f"dependency rollback backup is missing: {backup}")
            _assert_regular_tree(backup)
            if os.path.lexists(target):
                _safe_remove_tree(target, state_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(target))
        elif os.path.lexists(target):
            _safe_remove_tree(target, state_root)


def _transaction_path(payload: dict[str, Any], key: str, label: str) -> Path:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise ValueError(f"dependency transaction {label} is invalid")
    return _lexical(Path(value))


def _validate_transaction_receipt(
    payload: Any,
    *,
    state_root: Path,
    plugin_root: Path,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("dependency transaction receipt is invalid")
    if payload.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise ValueError("dependency transaction receipt is invalid")
    status = payload.get("status")
    if status not in {"active", "committed", "rolled_back"}:
        raise ValueError("dependency transaction status is invalid")
    transaction_id = payload.get("transaction_id")
    if not isinstance(transaction_id, str) or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
        raise ValueError("dependency transaction ID is invalid")

    state = _lexical(state_root)
    plugin = _lexical(plugin_root)
    receipt_state = _transaction_path(payload, "state_root", "state root")
    receipt_plugin = _transaction_path(payload, "plugin_root", "plugin root")
    if receipt_state != state:
        raise ValueError("dependency transaction belongs to a different state root")
    if receipt_plugin != plugin:
        raise ValueError("dependency transaction belongs to a different plugin root")
    _assert_no_reparse_ancestors(receipt_plugin, state)

    backup_root = _transaction_path(payload, "backup_root", "backup root")
    expected_backup_root = state / "dependency-backups" / transaction_id
    if backup_root != expected_backup_root:
        raise ValueError("dependency transaction backup root is outside its exact boundary")
    _assert_no_reparse_ancestors(backup_root, state)
    if os.path.lexists(backup_root):
        _assert_regular_tree(backup_root)

    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("dependency transaction records are invalid")
    specs = {spec.name: spec for spec in ENVIRONMENT_SPECS}
    seen: set[str] = set()
    validated_records: list[dict[str, Any]] = []
    for raw_record in records:
        if not isinstance(raw_record, dict):
            raise ValueError("dependency transaction record is invalid")
        name = raw_record.get("name")
        if not isinstance(name, str) or name not in specs or name in seen:
            raise ValueError("dependency transaction environment identity is invalid")
        seen.add(name)
        if raw_record.get("action") not in {"reuse", "incremental_upgrade", "recreate"}:
            raise ValueError("dependency transaction environment action is invalid")
        if not isinstance(raw_record.get("changed"), bool):
            raise ValueError("dependency transaction changed flag is invalid")

        target = _transaction_path(raw_record, "target_environment", "target environment")
        expected_target = plugin / Path(specs[name].relative_environment)
        if target != expected_target:
            raise ValueError("dependency transaction target is outside its exact plugin boundary")
        _assert_no_reparse_ancestors(target, plugin)

        backup_value = raw_record.get("backup_environment")
        backup: Path | None = None
        if backup_value is not None:
            backup = _transaction_path(raw_record, "backup_environment", "backup environment")
            if backup != backup_root / name:
                raise ValueError("dependency transaction environment backup is invalid")
            _assert_no_reparse_ancestors(backup, state)
            if raw_record["changed"] is not True:
                raise ValueError("unchanged dependency transaction record cannot have a backup")
        move_status = raw_record.get("backup_move_status")
        if move_status is None:
            move_status = "complete" if backup is not None else "not_required"
        if move_status not in {"not_required", "pending", "complete"}:
            raise ValueError("dependency transaction backup move status is invalid")
        if backup is None and move_status != "not_required":
            raise ValueError("dependency transaction backup move status is inconsistent")
        if backup is not None and move_status == "not_required":
            raise ValueError("dependency transaction backup move status is inconsistent")
        validated = dict(raw_record)
        validated["target_environment"] = str(target)
        validated["backup_environment"] = str(backup) if backup is not None else None
        validated["backup_move_status"] = move_status
        validated_records.append(validated)
    if backup_root.exists():
        expected_names = {
            str(record["name"])
            for record in validated_records
            if record.get("backup_environment") is not None
        }
        actual_names = {path.name for path in backup_root.iterdir()}
        if not actual_names.issubset(expected_names):
            raise ValueError("dependency transaction backup inventory is invalid")
    return {
        "payload": payload,
        "status": status,
        "transaction_id": transaction_id,
        "backup_root": backup_root,
        "records": validated_records,
    }


def _read_transaction_receipt(
    receipt_path: Path,
    *,
    state_root: Path,
    plugin_root: Path,
) -> dict[str, Any]:
    if not receipt_path.is_file() or _is_reparse(receipt_path):
        raise ValueError("dependency transaction receipt is missing or unsafe")
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("dependency transaction receipt is invalid JSON") from exc
    return _validate_transaction_receipt(
        payload,
        state_root=state_root,
        plugin_root=plugin_root,
    )


def _remove_transaction_backup(backup_root: Path, state_root: Path) -> str:
    if not os.path.lexists(backup_root):
        return "not_needed"
    _safe_remove_tree(backup_root, state_root / "dependency-backups")
    return "removed"


def _recover_active_transaction(
    details: dict[str, Any],
    *,
    receipt_path: Path,
    state_root: Path,
) -> dict[str, Any]:
    payload = details["payload"]
    _rollback_records(details["records"], state_root)
    recovered_at = datetime.now(timezone.utc).isoformat()
    payload["status"] = "rolled_back"
    payload["recovered"] = True
    payload["recovery_reason"] = "stale_active_transaction"
    payload["recovered_at_utc"] = recovered_at
    payload["rolled_back_at_utc"] = recovered_at
    payload["backup_cleanup"] = "pending"
    _atomic_write_json(receipt_path, payload)
    cleanup = _remove_transaction_backup(details["backup_root"], state_root)
    payload["backup_cleanup"] = cleanup
    _atomic_write_json(receipt_path, payload)
    return {
        "status": "rolled_back",
        "recovered": True,
        "action": "recovered_stale_active_transaction",
        "transaction_id": details["transaction_id"],
        "backup_cleanup": cleanup,
        "recovered_at_utc": recovered_at,
    }


def install_dependencies(
    *,
    plugin_root: Path,
    previous_plugin_root: Path | None,
    base_python: Path,
    state_root: Path,
    include_audio: bool,
) -> dict[str, Any]:
    state = _lexical(state_root)
    plugin = _lexical(plugin_root)
    expected_plugin = state / "marketplace" / "plugins" / PLUGIN_NAME
    if plugin != expected_plugin:
        raise ValueError(f"plugin root must be exactly: {expected_plugin}")
    if not plugin.is_dir() or _is_reparse(plugin):
        raise ValueError("plugin root is missing or unsafe")
    _assert_no_reparse_ancestors(plugin, state)
    previous = _lexical(previous_plugin_root) if previous_plugin_root is not None else None
    if previous is not None:
        allowed_parent = expected_plugin.parent
        if previous != plugin:
            _assert_descendant(previous, allowed_parent)
            if not previous.name.startswith(f".{PLUGIN_NAME}.backup."):
                raise ValueError("previous plugin root is not a managed deployment backup")
    python = _lexical(base_python)
    if not python.is_file():
        raise ValueError(f"base Python is missing: {python}")
    base_identity = _python_identity(python)
    if base_identity["implementation"] != "CPython" or int(base_identity["bits"]) != 64:
        raise ValueError("base Python must be 64-bit CPython")

    receipt_path = state / "dependency-transaction.json"
    recovered_transaction: dict[str, Any] | None = None
    if receipt_path.exists():
        existing = _read_transaction_receipt(
            receipt_path,
            state_root=state,
            plugin_root=plugin,
        )
        if existing["status"] == "active":
            recovered_transaction = _recover_active_transaction(
                existing,
                receipt_path=receipt_path,
                state_root=state,
            )
        elif existing["payload"].get("recovered") is True:
            cleanup = _remove_transaction_backup(existing["backup_root"], state)
            if existing["payload"].get("backup_cleanup") != cleanup:
                existing["payload"]["backup_cleanup"] = cleanup
                _atomic_write_json(receipt_path, existing["payload"])

    transaction_id = uuid.uuid4().hex
    backup_root = state / "dependency-backups" / transaction_id
    receipt: dict[str, Any] = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "status": "active",
        "transaction_id": transaction_id,
        "state_root": str(state),
        "plugin_root": str(plugin),
        "backup_root": str(backup_root),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "records": [],
    }
    if recovered_transaction is not None:
        receipt["recovered_transaction"] = recovered_transaction
    _atomic_write_json(receipt_path, receipt)
    records: list[dict[str, Any]] = receipt["records"]
    results: dict[str, Any] = {}
    try:
        for spec in ENVIRONMENT_SPECS:
            if spec.name == "audio" and not include_audio:
                results[spec.name] = {"action": "skipped", "reason": "skipped_by_request"}
                continue
            plan = _plan_environment(
                spec, plugin_root=plugin, previous_plugin_root=previous, base_identity=base_identity
            )
            target_environment = plugin / Path(spec.relative_environment)
            previous_environment = (
                previous / Path(spec.relative_environment) if previous is not None else None
            )
            record: dict[str, Any] = {
                "name": spec.name,
                "action": plan["action"],
                "target_environment": str(target_environment),
                "backup_environment": None,
                "backup_move_status": "not_required",
                "changed": False,
            }
            records.append(record)
            _atomic_write_json(receipt_path, receipt)

            source_is_target = previous_environment == target_environment
            if plan["action"] == "reuse" and source_is_target:
                pass
            else:
                if os.path.lexists(target_environment):
                    _assert_regular_tree(target_environment)
                    backup_environment = backup_root / spec.name
                    backup_environment.parent.mkdir(parents=True, exist_ok=True)
                    record["backup_environment"] = str(backup_environment)
                    record["backup_move_status"] = "pending"
                    record["changed"] = True
                    _atomic_write_json(receipt_path, receipt)
                    shutil.move(str(target_environment), str(backup_environment))
                    record["backup_move_status"] = "complete"
                    _atomic_write_json(receipt_path, receipt)
                else:
                    record["changed"] = True
                    _atomic_write_json(receipt_path, receipt)
                if plan["action"] in {"reuse", "incremental_upgrade"}:
                    if previous_environment is None or not previous_environment.is_dir():
                        raise ValueError(f"planned environment source is missing: {spec.name}")
                    source = (
                        Path(record["backup_environment"])
                        if source_is_target and record["backup_environment"]
                        else previous_environment
                    )
                    _copy_environment(source, target_environment)
                else:
                    target_environment.parent.mkdir(parents=True, exist_ok=True)
                    result = _run([str(python), "-m", "venv", str(target_environment)])
                    if result.returncode != 0:
                        raise RuntimeError(f"failed to create the {spec.name} environment")
                _atomic_write_json(receipt_path, receipt)

            if plan["action"] in {"incremental_upgrade", "recreate"}:
                _install_lock(
                    _environment_python(target_environment),
                    plugin / Path(spec.relative_requirements),
                )
            verification = _verify_environment(
                target_environment, plan["requested_versions"], base_identity
            )
            environment_receipt = {
                "schema_version": 1,
                "environment": spec.name,
                "lock_sha256": plan["requested_lock_sha256"],
                "python_identity": base_identity,
                "verified_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            if record["changed"]:
                _atomic_write_json(target_environment / ENVIRONMENT_RECEIPT, environment_receipt)
            results[spec.name] = {
                "action": plan["action"],
                "reason": plan["reason"],
                "runtime_path": str(_environment_python(target_environment)),
                "lock_sha256": plan["requested_lock_sha256"],
                **verification,
            }
        receipt["environments"] = results
        receipt["prepared_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(receipt_path, receipt)
    except Exception:
        rollback_succeeded = False
        try:
            _rollback_records(records, state)
            receipt["status"] = "rolled_back"
            receipt["rolled_back_at_utc"] = datetime.now(timezone.utc).isoformat()
            _atomic_write_json(receipt_path, receipt)
            rollback_succeeded = True
        finally:
            if rollback_succeeded and backup_root.exists():
                _safe_remove_tree(backup_root, state / "dependency-backups")
        raise
    result = {
        "status": "prepared",
        "transaction_receipt_path": str(receipt_path),
        "environments": results,
    }
    if recovered_transaction is not None:
        result["recovery"] = recovered_transaction
    return result


def rollback_transaction(*, receipt_path: Path, state_root: Path) -> dict[str, Any]:
    receipt_file = _lexical(receipt_path)
    state = _lexical(state_root)
    if receipt_file != state / "dependency-transaction.json":
        raise ValueError("dependency transaction receipt path is invalid")
    plugin = state / "marketplace" / "plugins" / PLUGIN_NAME
    details = _read_transaction_receipt(
        receipt_file,
        state_root=state,
        plugin_root=plugin,
    )
    payload = details["payload"]
    if details["status"] != "active":
        return {"status": details["status"], "action": "unchanged"}
    _rollback_records(details["records"], state)
    payload["status"] = "rolled_back"
    payload["rolled_back_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(receipt_file, payload)
    _remove_transaction_backup(details["backup_root"], state)
    return {"status": "rolled_back", "action": "restored_previous_environments"}


def commit_transaction(*, receipt_path: Path, state_root: Path) -> dict[str, Any]:
    receipt_file = _lexical(receipt_path)
    state = _lexical(state_root)
    if receipt_file != state / "dependency-transaction.json":
        raise ValueError("dependency transaction receipt path is invalid")
    plugin = state / "marketplace" / "plugins" / PLUGIN_NAME
    details = _read_transaction_receipt(
        receipt_file,
        state_root=state,
        plugin_root=plugin,
    )
    payload = details["payload"]
    if details["status"] != "active":
        return {"status": details["status"], "action": "unchanged"}
    payload["status"] = "committed"
    payload["committed_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(receipt_file, payload)
    backup_root = details["backup_root"]
    cleanup = "not_needed"
    if backup_root.exists():
        try:
            _safe_remove_tree(backup_root, state / "dependency-backups")
            cleanup = "removed"
        except (OSError, ValueError):
            cleanup = "deferred"
    return {
        "status": "committed",
        "action": "kept_verified_environments",
        "backup_cleanup": cleanup,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--plugin-root", type=Path, required=True)
    install.add_argument("--previous-plugin-root", type=Path)
    install.add_argument("--base-python", type=Path, required=True)
    install.add_argument("--state-root", type=Path, required=True)
    install.add_argument("--skip-audio", action="store_true")
    install.add_argument("--json", action="store_true")
    for name in ("commit", "rollback"):
        command = subparsers.add_parser(name)
        command.add_argument("--receipt-path", type=Path, required=True)
        command.add_argument("--state-root", type=Path, required=True)
        command.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "install":
            result = install_dependencies(
                plugin_root=args.plugin_root,
                previous_plugin_root=args.previous_plugin_root,
                base_python=args.base_python,
                state_root=args.state_root,
                include_audio=not args.skip_audio,
            )
        elif args.command == "commit":
            result = commit_transaction(
                receipt_path=args.receipt_path, state_root=args.state_root
            )
        else:
            result = rollback_transaction(
                receipt_path=args.receipt_path, state_root=args.state_root
            )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
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
