from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import PortableProjectError

_REPARSE_POINT_ATTRIBUTE = 0x400


@dataclass(frozen=True)
class TimelineTopology:
    active_timeline_id: str
    declared_timeline_ids: tuple[str, ...]


def _raise_topology_error(code: str, reason: str, data: dict[str, Any] | None = None) -> None:
    raise PortableProjectError(code, reason, data)


def _is_reparse_stat(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _validate_timeline_id(timeline_id: str, *, error_code: str) -> None:
    if timeline_id in {".", ".."} or any(character in timeline_id for character in "\\/:\0"):
        _raise_topology_error(
            error_code,
            "Timeline declaration contains an unsafe ID",
            {"timeline_id": timeline_id},
        )


def resolve_timeline_topology(
    layout: Mapping[str, Any],
    timeline_project: Mapping[str, Any],
    root_content: Mapping[str, Any] | None = None,
    *,
    error_code: str = "timeline_declaration_mismatch",
) -> TimelineTopology:
    layout_active = str(layout.get("activeTimeline") or "").strip()
    project_active = str(timeline_project.get("main_timeline_id") or "").strip()
    if layout_active and project_active and layout_active != project_active:
        _raise_topology_error(
            error_code,
            "timeline_layout and Timelines/project declare different active timelines",
        )

    root_id = str((root_content or {}).get("id") or "").strip()
    active_id = layout_active or project_active or root_id
    declared: set[str] = set()
    has_explicit_declaration = bool(layout_active or project_active)

    rows = timeline_project.get("timelines")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            timeline_id = str(row.get("id") or "").strip()
            if timeline_id:
                declared.add(timeline_id)
                has_explicit_declaration = True

    dock_items = layout.get("dockItems")
    if isinstance(dock_items, list):
        for item in dock_items:
            if not isinstance(item, dict) or not isinstance(item.get("timelineIds"), list):
                continue
            for value in item["timelineIds"]:
                timeline_id = str(value or "").strip()
                if timeline_id:
                    declared.add(timeline_id)
                    has_explicit_declaration = True

    if has_explicit_declaration and active_id:
        declared.add(active_id)
    for timeline_id in declared:
        _validate_timeline_id(timeline_id, error_code=error_code)
    if active_id:
        _validate_timeline_id(active_id, error_code=error_code)

    return TimelineTopology(active_id, tuple(sorted(declared)))


def discover_timeline_directories(
    source_dir: Path,
    *,
    error_code: str = "timeline_declaration_mismatch",
) -> tuple[str, ...]:
    timeline_root = source_dir / "Timelines"
    if not os.path.lexists(timeline_root):
        return ()
    try:
        root_info = timeline_root.lstat()
    except OSError as exc:
        raise PortableProjectError(error_code, "Could not inspect the Timelines directory") from exc
    if _is_reparse_stat(root_info):
        _raise_topology_error(error_code, "Timelines directory cannot be a reparse point")
    if not stat.S_ISDIR(root_info.st_mode):
        _raise_topology_error(error_code, "Timelines must be a directory")

    actual: set[str] = set()
    for child in timeline_root.iterdir():
        try:
            child_info = child.lstat()
        except OSError as exc:
            raise PortableProjectError(
                error_code, "Could not inspect a timeline directory"
            ) from exc
        if _is_reparse_stat(child_info):
            _raise_topology_error(error_code, "Timeline directory cannot be a reparse point")
        if not stat.S_ISDIR(child_info.st_mode):
            continue
        content_path = child / "draft_content.json"
        if not os.path.lexists(content_path):
            continue
        try:
            content_info = content_path.lstat()
        except OSError as exc:
            raise PortableProjectError(error_code, "Could not inspect timeline content") from exc
        if _is_reparse_stat(content_info):
            _raise_topology_error(error_code, "Timeline content cannot be a reparse point")
        if not stat.S_ISREG(content_info.st_mode):
            continue
        _validate_timeline_id(child.name, error_code=error_code)
        actual.add(child.name)
    return tuple(sorted(actual))


def validate_timeline_directories(
    source_dir: Path,
    topology: TimelineTopology,
    *,
    error_code: str = "timeline_declaration_mismatch",
) -> tuple[str, ...]:
    actual = discover_timeline_directories(source_dir, error_code=error_code)
    undeclared = sorted(set(actual) - set(topology.declared_timeline_ids))
    if undeclared:
        _raise_topology_error(
            error_code,
            "Draft contains timeline content not declared by its project metadata",
            {"timeline_ids": undeclared},
        )
    return actual
