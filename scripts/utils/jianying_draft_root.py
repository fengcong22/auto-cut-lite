"""JianYing custom-draft-root discovery and first-use validation.

The desktop editor stores its custom project directory in the small
``globalSetting`` file.  Delivery relies on that path being the same on both
machines; silently selecting a different default makes an otherwise healthy
draft invisible to JianYing.  This module therefore exposes a fail-closed
check used by the delivery command and by first-run diagnostics.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

SETTING_KEY = "currentCustomDraftPath"
SETTING_RELATIVE_PATH = Path("JianyingPro") / "User Data" / "Config" / "globalSetting"
REMINDER_PREFIX = "请在对方电脑创建与源电脑相同的剪映草稿路径"


class DraftRootError(RuntimeError):
    """Raised when the recipient cannot use the agreed JianYing draft root."""


def _setting_path(local_app_data: str | os.PathLike[str] | None = None) -> Path:
    base = local_app_data or os.environ.get("LOCALAPPDATA")
    if not base:
        return Path(SETTING_RELATIVE_PATH)
    return Path(base).expanduser() / SETTING_RELATIVE_PATH


def _unescape_setting_value(value: str) -> str:
    # QSettings may double path separators in stored Windows path values.
    # Only collapse doubled backslashes; do not interpret arbitrary escape
    # sequences because they can be meaningful in a path component.
    return value.strip().strip('"').replace("\\\\", "\\").strip()


def read_configured_draft_root(
    local_app_data: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Read ``currentCustomDraftPath`` without inventing a fallback path.

    ``globalSetting`` is an INI-like text file rather than strict INI: values
    may contain ``=`` and sections are not important.  We intentionally read
    only the one key that controls project storage.
    """

    path = _setting_path(local_app_data)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    key_pattern = re.compile(r"^\s*" + re.escape(SETTING_KEY) + r"\s*=\s*(.*?)\s*$")
    for line in text.splitlines():
        match = key_pattern.match(line)
        if not match:
            continue
        raw = _unescape_setting_value(match.group(1))
        if not raw:
            return None
        try:
            return Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return Path(raw).expanduser()
    return None


def _normalise(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _display(path: Path) -> str:
    # Keep the user's Windows spelling where possible while making relative
    # test paths deterministic.
    return str(path.expanduser().resolve(strict=False))


def check_draft_root(
    expected_root: str | os.PathLike[str],
    *,
    configured_root: str | os.PathLike[str] | None = None,
    local_app_data: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Return a machine-readable first-use check for an agreed root.

    ``configured_root`` is an explicit probe override used by tests and by a
    caller that already read the editor setting.  When omitted, the actual
    JianYing ``globalSetting`` file is read.  No directory is created here.
    """

    raw_expected = os.fspath(expected_root) if expected_root is not None else ""
    if not str(raw_expected).strip():
        raise DraftRootError("必须提供源电脑的剪映草稿路径")
    expected = Path(raw_expected).expanduser().resolve(strict=False)
    configured_value = (
        Path(configured_root).expanduser().resolve(strict=False)
        if configured_root is not None
        else read_configured_draft_root(local_app_data)
    )
    expected_exists = expected.is_dir()
    configured_exists = bool(configured_value and configured_value.is_dir())
    same = bool(configured_value and _normalise(configured_value) == _normalise(expected))

    if same and expected_exists:
        status = "ready"
        needs_user_action = False
        message = f"剪映草稿路径已确认：{_display(expected)}"
    elif configured_value is None or not configured_exists:
        status = "missing"
        needs_user_action = True
        message = f"{REMINDER_PREFIX}：{_display(expected)}"
    else:
        status = "mismatch"
        needs_user_action = True
        message = (
            f"对方电脑的剪映草稿路径与源电脑不一致。{REMINDER_PREFIX}：" f"{_display(expected)}"
        )

    return {
        "status": status,
        "ready": status == "ready",
        "needs_user_action": needs_user_action,
        "expected_root": _display(expected),
        "configured_root": _display(configured_value) if configured_value else None,
        "expected_root_exists": expected_exists,
        "configured_root_exists": configured_exists,
        "message": message,
        "action": "create_matching_draft_root" if needs_user_action else None,
        "setting_path": _display(_setting_path(local_app_data)),
    }


def require_draft_root(
    expected_root: str | os.PathLike[str],
    *,
    configured_root: str | os.PathLike[str] | None = None,
    local_app_data: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the agreed root or raise with the user-facing reminder."""

    result = check_draft_root(
        expected_root,
        configured_root=configured_root,
        local_app_data=local_app_data,
    )
    if not result["ready"]:
        raise DraftRootError(str(result["message"]))
    return Path(str(result["expected_root"]))


def current_draft_root(*, local_app_data: str | os.PathLike[str] | None = None) -> Path:
    """Return the configured root, failing instead of silently falling back."""

    configured = read_configured_draft_root(local_app_data)
    if configured is None or not configured.is_dir():
        expected = (
            configured
            or Path(os.environ.get("USERPROFILE", Path.home()))
            / "AppData"
            / "Local"
            / "JianyingPro"
            / "User Data"
            / "Projects"
            / "com.lveditor.draft"
        )
        raise DraftRootError(f"{REMINDER_PREFIX}：{_display(expected)}")
    return configured


__all__ = [
    "DraftRootError",
    "REMINDER_PREFIX",
    "SETTING_KEY",
    "SETTING_RELATIVE_PATH",
    "check_draft_root",
    "current_draft_root",
    "read_configured_draft_root",
    "require_draft_root",
]
