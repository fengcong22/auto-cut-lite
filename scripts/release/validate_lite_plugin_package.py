"""Validate and safely extract a generic auto-cut-lite plugin release ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.release.build_lite_plugin import PLUGIN_NAME, WORKSPACE_NAME, _privacy_scan


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} root must be an object: {path}")
    return payload


def _validate_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or "\\" in name
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] != WORKSPACE_NAME
    ):
        raise ValueError(f"unsafe ZIP member: {name}")
    file_type = (info.external_attr >> 16) & 0o170000
    if info.is_dir() or file_type not in {0, stat.S_IFREG}:
        raise ValueError(f"ZIP member is not a regular file: {name}")


def validate(archive_path: Path, receipt_path: Path, extract_to: Path) -> dict[str, Any]:
    archive = archive_path.expanduser().resolve(strict=True)
    receipt_file = receipt_path.expanduser().resolve(strict=True)
    destination = extract_to.expanduser().resolve(strict=False)
    if destination.exists():
        raise ValueError(f"extraction destination already exists: {destination}")
    destination.mkdir(parents=True)

    receipt = _json_object(receipt_file, "release receipt")
    archive_sha256 = _sha256(archive)
    if receipt.get("archive_sha256") != archive_sha256:
        raise ValueError("release receipt SHA-256 does not match the archive")

    with zipfile.ZipFile(archive) as package:
        infos = package.infolist()
        if package.testzip() is not None:
            raise ValueError("ZIP CRC validation failed")
        names = [info.filename for info in infos]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("ZIP contains duplicate case-insensitive paths")
        for info in infos:
            _validate_member(info)
        package.extractall(destination)

    if receipt.get("archive_root") != WORKSPACE_NAME:
        raise ValueError("release receipt archive root is invalid")
    root = destination / WORKSPACE_NAME
    manifest = _json_object(root / "PACKAGE-MANIFEST.json", "package manifest")
    plugin_manifest = _json_object(root / ".codex-plugin" / "plugin.json", "plugin manifest")
    if manifest.get("name") != PLUGIN_NAME or plugin_manifest.get("name") != PLUGIN_NAME:
        raise ValueError("package or plugin name does not match auto-cut-lite")
    if manifest.get("version") != plugin_manifest.get("version"):
        raise ValueError("package and plugin versions do not match")
    if receipt.get("plugin_version") != manifest.get("version"):
        raise ValueError("receipt and package versions do not match")

    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("package manifest files must be a non-empty array")
    inventory: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ValueError("package manifest contains an invalid file row")
        relative = PurePosixPath(row["path"])
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"unsafe package manifest path: {row['path']}")
        key = relative.as_posix().casefold()
        if key in inventory:
            raise ValueError(f"duplicate package manifest path: {row['path']}")
        inventory[key] = row
        file_path = root.joinpath(*relative.parts)
        if not file_path.is_file() or file_path.is_symlink():
            raise ValueError(f"inventoried file is missing or unsafe: {row['path']}")
        if file_path.stat().st_size != row.get("size") or _sha256(file_path) != row.get("sha256"):
            raise ValueError(f"inventoried file hash or size mismatch: {row['path']}")

    actual = {
        path.relative_to(root).as_posix().casefold()
        for path in root.rglob("*")
        if path.is_file()
    }
    expected = set(inventory) | {"package-manifest.json"}
    if actual != expected:
        raise ValueError("extracted package tree does not exactly match its manifest")
    privacy_findings = _privacy_scan(root)
    if privacy_findings:
        raise ValueError("package privacy scan failed: " + "; ".join(privacy_findings[:20]))

    return {
        "status": "pass",
        "plugin_name": PLUGIN_NAME,
        "plugin_version": manifest["version"],
        "archive_sha256": archive_sha256,
        "archive_entry_count": len(names),
        "manifest_file_count": len(inventory),
        "extracted_root": str(root),
        "zip_crc": "pass",
        "zip_path_safety": "pass",
        "tree_manifest": "pass",
        "privacy_scan": "pass",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--extract-to", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = validate(args.archive, args.receipt, args.extract_to)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
