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
        if target.exists():
            _safe_remove_tree(target, state_root)
        backup_value = record.get("backup_environment")
        if backup_value:
            backup = _lexical(Path(backup_value))
            _assert_descendant(backup, state_root / "dependency-backups")
            if not backup.is_dir():
                raise ValueError(f"dependency rollback backup is missing: {backup}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(target))


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

    transaction_id = uuid.uuid4().hex
    backup_root = state / "dependency-backups" / transaction_id
    receipt_path = state / "dependency-transaction.json"
    if receipt_path.exists():
        active = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        if active.get("status") == "active":
            raise ValueError("an unfinished dependency transaction already exists")
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
                "changed": False,
            }
            records.append(record)
            _atomic_write_json(receipt_path, receipt)

            source_is_target = previous_environment == target_environment
            if plan["action"] == "reuse" and source_is_target:
                pass
            else:
                if target_environment.exists():
                    backup_environment = backup_root / spec.name
                    backup_environment.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target_environment), str(backup_environment))
                    record["backup_environment"] = str(backup_environment)
                    record["changed"] = True
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
    return {
        "status": "prepared",
        "transaction_receipt_path": str(receipt_path),
        "environments": results,
    }


def rollback_transaction(*, receipt_path: Path, state_root: Path) -> dict[str, Any]:
    receipt_file = _lexical(receipt_path)
    state = _lexical(state_root)
    if receipt_file != state / "dependency-transaction.json":
        raise ValueError("dependency transaction receipt path is invalid")
    payload = json.loads(receipt_file.read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise ValueError("dependency transaction receipt is invalid")
    if payload.get("status") != "active":
        return {"status": payload.get("status"), "action": "unchanged"}
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("dependency transaction records are invalid")
    _rollback_records(records, state)
    payload["status"] = "rolled_back"
    payload["rolled_back_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(receipt_file, payload)
    backup_root = _lexical(Path(payload["backup_root"]))
    if backup_root.exists():
        _safe_remove_tree(backup_root, state / "dependency-backups")
    return {"status": "rolled_back", "action": "restored_previous_environments"}


def commit_transaction(*, receipt_path: Path, state_root: Path) -> dict[str, Any]:
    receipt_file = _lexical(receipt_path)
    state = _lexical(state_root)
    if receipt_file != state / "dependency-transaction.json":
        raise ValueError("dependency transaction receipt path is invalid")
    payload = json.loads(receipt_file.read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise ValueError("dependency transaction receipt is invalid")
    if payload.get("status") != "active":
        return {"status": payload.get("status"), "action": "unchanged"}
    payload["status"] = "committed"
    payload["committed_at_utc"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(receipt_file, payload)
    backup_root = _lexical(Path(payload["backup_root"]))
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
