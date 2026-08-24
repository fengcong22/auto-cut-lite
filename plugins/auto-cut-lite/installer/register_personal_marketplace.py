"""Atomically register auto-cut-lite in the Codex personal marketplace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any


PLUGIN_NAME = "auto-cut-lite"
PLUGIN_SOURCE = "./plugins/auto-cut-lite"
IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} root must be an object: {path}")
    return payload


def _validate_plugin(plugin_dir: Path) -> Path:
    resolved = plugin_dir.expanduser().resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError(f"plugin directory must be a regular directory: {resolved}")
    manifest = resolved / ".codex-plugin" / "plugin.json"
    payload = _read_json_object(manifest, label="plugin manifest")
    if payload.get("name") != PLUGIN_NAME:
        raise ValueError(f"plugin manifest name must be {PLUGIN_NAME!r}: {manifest}")
    return resolved


def _new_marketplace() -> dict[str, Any]:
    return {
        "name": "personal",
        "interface": {"displayName": "Personal"},
        "plugins": [],
    }


def _validate_marketplace(payload: dict[str, Any], path: Path) -> str:
    name = payload.get("name")
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise ValueError(f"{path} field 'name' must be a valid marketplace identifier")
    interface = payload.get("interface")
    if interface is not None and not isinstance(interface, dict):
        raise ValueError(f"{path} field 'interface' must be an object when present")
    plugins = payload.get("plugins")
    if not isinstance(plugins, list):
        raise ValueError(f"{path} field 'plugins' must be an array")
    if any(not isinstance(entry, dict) for entry in plugins):
        raise ValueError(f"{path} field 'plugins' must contain only objects")
    return name


def _plugin_entry() -> dict[str, Any]:
    return {
        "name": PLUGIN_NAME,
        "source": {"source": "local", "path": PLUGIN_SOURCE},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }


def _replace_plugin_entry(payload: dict[str, Any]) -> str:
    plugins = payload["plugins"]
    replacement = _plugin_entry()
    matching = [index for index, entry in enumerate(plugins) if entry.get("name") == PLUGIN_NAME]
    if matching:
        first = matching[0]
        payload["plugins"] = [
            replacement if index == first else entry
            for index, entry in enumerate(plugins)
            if index == first or entry.get("name") != PLUGIN_NAME
        ]
        return "replaced"
    plugins.append(replacement)
    return "appended"


def _write_exclusive(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.auto-cut-lite.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def register(plugin_dir: Path, marketplace_path: Path) -> dict[str, Any]:
    resolved_plugin = _validate_plugin(plugin_dir)
    marketplace = marketplace_path.expanduser().resolve(strict=False)
    existed = marketplace.exists()
    if existed:
        if not marketplace.is_file() or marketplace.is_symlink():
            raise ValueError(f"marketplace path must be a regular file: {marketplace}")
        original = marketplace.read_bytes()
        payload = _read_json_object(marketplace, label="marketplace")
    else:
        original = b""
        payload = _new_marketplace()
    marketplace_name = _validate_marketplace(payload, marketplace)
    action = _replace_plugin_entry(payload)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    backup_path: Path | None = None
    if existed:
        backup_path = marketplace.with_name(
            f"{marketplace.name}.auto-cut-lite.{uuid.uuid4().hex}.bak"
        )
        _write_exclusive(backup_path, original)
    try:
        _atomic_write(marketplace, encoded)
    except BaseException:
        if backup_path is not None:
            backup_path.unlink(missing_ok=True)
        raise

    return {
        "status": "registered",
        "plugin_name": PLUGIN_NAME,
        "plugin_dir": str(resolved_plugin),
        "marketplace_path": str(marketplace),
        "marketplace_name": marketplace_name,
        "marketplace_created": not existed,
        "marketplace_backup_path": str(backup_path) if backup_path else None,
        "marketplace_sha256": _sha256_bytes(encoded),
        "entry_action": action,
    }


def rollback(
    marketplace_path: Path,
    *,
    backup_path: Path | None,
    created_new: bool,
    expected_current_sha256: str,
) -> dict[str, Any]:
    marketplace = marketplace_path.expanduser().resolve(strict=False)
    if not marketplace.is_file() or marketplace.is_symlink():
        raise ValueError(f"marketplace file is unavailable for rollback: {marketplace}")
    current = marketplace.read_bytes()
    if _sha256_bytes(current) != expected_current_sha256:
        raise ValueError("marketplace changed after registration; refusing to overwrite it")

    if created_new:
        if backup_path is not None:
            raise ValueError("a newly created marketplace cannot also have a backup")
        marketplace.unlink()
        return {"status": "rolled_back", "action": "removed_created_marketplace"}

    if backup_path is None:
        raise ValueError("backup path is required to restore an existing marketplace")
    backup = backup_path.expanduser().resolve(strict=True)
    if backup.parent != marketplace.parent or not backup.name.startswith(
        f"{marketplace.name}.auto-cut-lite."
    ) or not backup.name.endswith(".bak"):
        raise ValueError("backup path is not an auto-cut-lite marketplace backup")
    if not backup.is_file() or backup.is_symlink():
        raise ValueError(f"backup path must be a regular file: {backup}")
    restored = backup.read_bytes()
    _read_json_object(backup, label="marketplace backup")
    _atomic_write(marketplace, restored)
    return {
        "status": "rolled_back",
        "action": "restored_marketplace_backup",
        "restored_sha256": _sha256_bytes(restored),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_parser = subparsers.add_parser("register")
    register_parser.add_argument("--plugin-dir", type=Path, required=True)
    register_parser.add_argument("--marketplace-path", type=Path, required=True)
    register_parser.add_argument("--json", action="store_true")

    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--marketplace-path", type=Path, required=True)
    rollback_parser.add_argument("--backup-path", type=Path)
    rollback_parser.add_argument("--created-new", action="store_true")
    rollback_parser.add_argument("--expected-current-sha256", required=True)
    rollback_parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "register":
            result = register(args.plugin_dir, args.marketplace_path)
        else:
            result = rollback(
                args.marketplace_path,
                backup_path=args.backup_path,
                created_new=args.created_new,
                expected_current_sha256=args.expected_current_sha256,
            )
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
