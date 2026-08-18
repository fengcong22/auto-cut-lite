import os
import re
import shutil
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from utils.formatters import get_default_drafts_root

VERSION_RE = re.compile(r"^(?P<family>.+?)_v(?P<version>\d+)(?:_|$)", re.IGNORECASE)
FALLBACK_MARKER = "__fallback_"


@dataclass(frozen=True)
class DraftRetentionEntry:
    name: str
    path: str
    mtime: float
    version: Optional[int]


def infer_project_family(draft_name: str) -> str:
    name = str(draft_name or "").strip()
    match = VERSION_RE.match(name)
    if match:
        return match.group("family")
    if FALLBACK_MARKER in name:
        return name.split(FALLBACK_MARKER, 1)[0]
    return name


def is_fallback_draft_name(name: str) -> bool:
    return FALLBACK_MARKER in str(name or "")


def _is_valid_draft_dir(path: str) -> bool:
    return os.path.exists(os.path.join(path, "draft_content.json")) or os.path.exists(
        os.path.join(path, "draft_meta_info.json")
    )


def _matches_family(name: str, family: str) -> bool:
    if name == family:
        return True
    return name.startswith(f"{family}_") or name.startswith(f"{family}__")


def _version_for_name(name: str, family: str) -> Optional[int]:
    match = re.match(rf"^{re.escape(family)}_v(?P<version>\d+)(?:_|$)", name, re.IGNORECASE)
    if not match:
        return None
    return int(match.group("version"))


def _resolve_root(drafts_root: Optional[str]) -> str:
    root = os.path.abspath(drafts_root or get_default_drafts_root())
    if not os.path.isdir(root):
        raise FileNotFoundError(f"JianYing drafts root not found: {root}")
    return root


def list_family_drafts(drafts_root: Optional[str], family: str) -> List[DraftRetentionEntry]:
    root = _resolve_root(drafts_root)
    entries: List[DraftRetentionEntry] = []
    for name in os.listdir(root):
        if not _matches_family(name, family):
            continue
        path = os.path.abspath(os.path.join(root, name))
        if not os.path.isdir(path) or not _is_valid_draft_dir(path):
            continue
        if os.path.commonpath([root, path]) != root:
            continue
        entries.append(
            DraftRetentionEntry(
                name=name,
                path=path,
                mtime=os.path.getmtime(path),
                version=_version_for_name(name, family),
            )
        )
    return entries


def _retention_sort_key(entry: DraftRetentionEntry) -> tuple[int, int, float, str]:
    version = entry.version if entry.version is not None else -1
    return (1 if entry.version is not None else 0, version, entry.mtime, entry.name)


def _safe_remove_draft(root: str, entry: DraftRetentionEntry) -> None:
    target = os.path.abspath(entry.path)
    if target == root:
        raise ValueError("Refuse to delete the JianYing drafts root.")
    if os.path.commonpath([root, target]) != root:
        raise ValueError(f"Refuse to delete outside the JianYing drafts root: {target}")
    if os.path.basename(target) != entry.name:
        raise ValueError(f"Draft path/name mismatch: {entry.name} -> {target}")
    shutil.rmtree(target)


def retain_latest_project_drafts(
    *,
    draft_name: Optional[str] = None,
    family: Optional[str] = None,
    drafts_root: Optional[str] = None,
    keep_count: int = 3,
    max_fallback_count: Optional[int] = 1,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if keep_count <= 0:
        raise ValueError("keep_count must be > 0")
    if max_fallback_count is not None and max_fallback_count < 0:
        raise ValueError("max_fallback_count must be >= 0 when set")
    project_family = str(family or infer_project_family(str(draft_name or ""))).strip()
    if not project_family:
        raise ValueError("draft_name or family is required")

    root = _resolve_root(drafts_root)
    entries = sorted(
        list_family_drafts(root, project_family), key=_retention_sort_key, reverse=True
    )
    if max_fallback_count is None:
        kept = entries[:keep_count]
        deleted = entries[keep_count:]
    else:
        fallback_entries = [entry for entry in entries if is_fallback_draft_name(entry.name)]
        normal_entries = [entry for entry in entries if not is_fallback_draft_name(entry.name)]
        fallback_kept = fallback_entries[: min(max_fallback_count, keep_count)]
        normal_slots = max(0, keep_count - len(fallback_kept))
        normal_kept = normal_entries[:normal_slots]
        kept = sorted(normal_kept + fallback_kept, key=_retention_sort_key, reverse=True)
        kept_paths = {entry.path for entry in kept}
        deleted = [entry for entry in entries if entry.path not in kept_paths]

    if not dry_run:
        for entry in deleted:
            _safe_remove_draft(root, entry)

    remaining = sorted(
        list_family_drafts(root, project_family), key=_retention_sort_key, reverse=True
    )
    return {
        "drafts_root": root,
        "family": project_family,
        "keep_count": keep_count,
        "max_fallback_count": max_fallback_count,
        "matched_count": len(entries),
        "dry_run": dry_run,
        "kept": [asdict(entry) for entry in kept],
        "deleted": [asdict(entry) for entry in deleted],
        "remaining": [asdict(entry) for entry in remaining],
    }
