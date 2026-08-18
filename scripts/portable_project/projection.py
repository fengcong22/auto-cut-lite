from __future__ import annotations

from typing import Any, Mapping

from .errors import PortableProjectError
from .manifest import validate_relative_path


def _target_path(relative: str) -> str | None:
    if relative == "Draft":
        return None
    if relative.startswith("Draft/"):
        return relative.removeprefix("Draft/")
    if relative == "Media" or relative.startswith("Media/"):
        return relative
    return None


def installation_projection(manifest: Mapping[str, Any]) -> dict[str, Any]:
    file_rows = manifest.get("files")
    directory_rows = manifest.get("directories")
    if not isinstance(file_rows, list) or not isinstance(directory_rows, list):
        raise PortableProjectError("invalid_manifest", "Package tree inventory is missing")

    entries: dict[str, dict[str, Any]] = {}

    def add(relative_value: object, entry_type: str, row: Mapping[str, Any] | None) -> None:
        relative = validate_relative_path(relative_value)
        target = _target_path(relative)
        if target is None:
            return
        key = target.casefold()
        previous = entries.get(key)
        if previous is not None:
            exact_directory_merge = (
                previous["type"] == "directory"
                and entry_type == "directory"
                and previous["target_path"] == target
            )
            if exact_directory_merge:
                previous["sources"].append(relative)
                return
            raise PortableProjectError(
                "installed_path_collision",
                "Package entries would collide during installation",
                {"target_path": target},
            )
        entries[key] = {
            "target_path": target,
            "type": entry_type,
            "row": row,
            "sources": [relative],
        }

    for relative in directory_rows:
        add(relative, "directory", None)
    for raw_row in file_rows:
        if not isinstance(raw_row, Mapping):
            raise PortableProjectError("invalid_manifest", "Package file row is invalid")
        add(raw_row.get("path"), "file", raw_row)

    files = {
        entry["target_path"]: entry["row"] for entry in entries.values() if entry["type"] == "file"
    }
    directories = {
        entry["target_path"] for entry in entries.values() if entry["type"] == "directory"
    }
    return {"files": files, "directories": directories}
