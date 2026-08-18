import difflib
import functools
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Union


# ----------------- 路径自动探测 -----------------
def _configured_jianying_draft_roots(local_app_data: str | None) -> List[str]:
    """Return existing JianYing custom draft folders configured by the desktop app."""

    if not local_app_data:
        return []
    setting_path = os.path.join(
        local_app_data,
        "JianyingPro",
        "User Data",
        "Config",
        "globalSetting",
    )
    try:
        with open(setting_path, "r", encoding="utf-8", errors="replace") as setting_file:
            lines = setting_file.readlines()
    except OSError:
        return []

    configured = []
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == "currentCustomDraftPath":
            candidate = value.strip().strip('"').replace("\\\\", "\\")
            if candidate and os.path.isdir(candidate):
                configured.append(candidate)
    return configured


class ConfiguredDraftRootError(ValueError):
    """A fail-closed configured JianYing draft-root validation error.

    ``get_default_drafts_root`` intentionally keeps its historical fallback
    behaviour for the editing commands that use it.  Native delivery must not
    silently fall back to another directory, so it uses the strict helper
    below and exposes a machine-readable prefix in the exception text.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: {detail}")


def _path_is_reparse_point(path: str) -> bool:
    """Return whether *path* is a symlink or Windows reparse point."""

    try:
        target_stat = os.lstat(path)
    except OSError as exc:
        raise ConfiguredDraftRootError(
            "configured_target_path_unreadable", f"cannot inspect {path}: {exc}"
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(target_stat.st_mode) or bool(
        getattr(target_stat, "st_file_attributes", 0) & reparse_flag
    )


def _path_has_reparse_component(path: str) -> bool:
    """Check every existing component without following symlinks."""

    current = os.path.abspath(path)
    while True:
        if os.path.lexists(current) and _path_is_reparse_point(current):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _read_configured_draft_path_values(local_app_data: str | os.PathLike[str] | None) -> List[str]:
    if local_app_data is None:
        local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return []

    setting_path = Path(local_app_data) / "JianyingPro" / "User Data" / "Config" / "globalSetting"
    try:
        raw = setting_path.read_bytes()
    except OSError:
        return []

    # JianYing has used a plain UTF-8 key/value file; accepting the common
    # UTF-16 variants keeps the strict check useful on machines with a legacy
    # locale while still treating the setting as opaque text.
    text = None
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "gb18030"):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        # A UTF-16 byte stream without a BOM can decode as UTF-8 containing
        # NULs.  Prefer a decoding that actually contains the setting key.
        text = decoded
        if "currentcustomdraftpath" in decoded.casefold():
            break
    if text is None:
        return []

    values: List[str] = []
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key.strip().casefold() != "currentcustomdraftpath":
            continue
        candidate = value.strip().lstrip("\ufeff")
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "\"'":
            candidate = candidate[1:-1].strip()
        # The setting sometimes serializes a Windows path with doubled
        # backslashes.  Collapse only that escape, never other path content.
        if candidate.startswith("\\\\"):
            candidate = "\\\\" + candidate[2:].replace("\\\\", "\\")
        else:
            candidate = candidate.replace("\\\\", "\\")
        candidate = os.path.expandvars(candidate).strip()
        if candidate:
            values.append(candidate)
    return values


def get_configured_jianying_draft_root(
    local_app_data: str | os.PathLike[str] | None = None,
    *,
    require_exists: bool = True,
) -> Path:
    """Resolve JianYing's explicitly configured custom drafts root.

    This is deliberately separate from :func:`get_default_drafts_root`.
    Native delivery mirrors into this path and therefore fails closed when
    the setting is absent, ambiguous, missing, or a reparse-point path.  It
    never substitutes the conventional ``Projects/com.lveditor.draft`` root.
    """

    values = _read_configured_draft_path_values(local_app_data)
    if not values:
        raise ConfiguredDraftRootError(
            "configured_target_path_missing", "JianYing currentCustomDraftPath is not configured"
        )

    normalized: dict[str, str] = {}
    for value in values:
        candidate = os.path.abspath(os.path.expanduser(value))
        key = os.path.normcase(candidate)
        normalized.setdefault(key, candidate)
    if len(normalized) != 1:
        joined = ", ".join(sorted(normalized.values()))
        raise ConfiguredDraftRootError("ambiguous_configured_target", joined)

    candidate = next(iter(normalized.values()))
    if _path_has_reparse_component(candidate):
        raise ConfiguredDraftRootError(
            "configured_target_path_invalid", "configured root contains a symlink or reparse point"
        )
    path = Path(candidate)
    if not path.exists():
        if require_exists:
            raise ConfiguredDraftRootError("configured_target_path_missing", candidate)
        return path
    if not path.is_dir():
        raise ConfiguredDraftRootError(
            "configured_target_path_invalid", "configured root is not a directory"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ConfiguredDraftRootError(
            "configured_target_path_unreadable", f"cannot resolve {candidate}: {exc}"
        ) from exc
    return resolved


def get_default_drafts_root() -> str:
    """自动探测剪映草稿目录 (Windows)"""
    local_app_data = os.environ.get("LOCALAPPDATA")
    user_profile = os.environ.get("USERPROFILE")

    candidates = _configured_jianying_draft_roots(local_app_data)
    if local_app_data:
        candidates.extend(
            [
                os.path.join(local_app_data, "JianyingPro/User Data/Projects/com.lveditor.draft"),
                os.path.join(local_app_data, "CapCut/User Data/Projects/com.lveditor.draft"),
            ]
        )

    if user_profile:
        candidates.append(
            os.path.join(
                user_profile, "AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft"
            )
        )

    for path in candidates:
        if os.path.exists(path):
            return path
    if candidates:
        return candidates[0]
    return os.path.join(
        os.path.expanduser("~"),
        "AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft",
    )


def get_all_drafts(root_path: str = None) -> List[Dict]:
    """获取所有草稿并按修改时间排序"""
    root = root_path or get_default_drafts_root()
    drafts = []
    if not os.path.exists(root):
        return []

    for item in os.listdir(root):
        path = os.path.join(root, item)
        if os.path.isdir(path):
            if os.path.exists(os.path.join(path, "draft_content.json")) or os.path.exists(
                os.path.join(path, "draft_meta_info.json")
            ):
                drafts.append({"name": item, "mtime": os.path.getmtime(path), "path": path})
    return sorted(drafts, key=lambda x: x["mtime"], reverse=True)


try:
    from pyJianYingDraft import tim
except ImportError:

    def tim(v):
        if isinstance(v, (int, float)):
            return int(v * 1000000)
        return 0


# ----------------- 时间与格式转换 -----------------
def safe_tim(inp: Union[str, int, float]) -> int:
    """
    增强版时间解析器，支持:
    1. 1h2m3s (底层库自带)
    2. 00:00:10 (冒号分隔格式)
    3. 10 (纯数字秒)
    """
    # 约定：
    # - int 统一视为“微秒”，避免 200000us 被误判为 200000s
    # - float 视为“秒”
    if isinstance(inp, int):
        return int(inp)
    if isinstance(inp, float):
        return int(inp * 1000000)

    if isinstance(inp, str) and ":" in inp:
        try:
            parts = inp.split(":")
            if len(parts) == 3:  # HH:MM:SS
                h, m, s = map(float, parts)
                return int((h * 3600 + m * 60 + s) * 1000000)
            elif len(parts) == 2:  # MM:SS
                m, s = map(float, parts)
                return int((m * 60 + s) * 1000000)
        except Exception:
            pass
    if isinstance(inp, str):
        s = inp.strip()
        # 支持显式单位组合: 1h2m3s500ms / 500ms / 200000us / 1m2.5s
        unit_pattern = re.compile(r"\s*(\d+(?:\.\d+)?)(ms|us|h|m|s)\s*", re.IGNORECASE)
        pos = 0
        total_us = 0.0
        unit_scale = {
            "h": 3600 * 1000000,
            "m": 60 * 1000000,
            "s": 1000000,
            "ms": 1000,
            "us": 1,
        }
        matches = list(unit_pattern.finditer(s))
        if matches:
            for match in matches:
                if match.start() != pos:
                    break
                value = float(match.group(1))
                unit = match.group(2).lower()
                total_us += value * unit_scale[unit]
                pos = match.end()
            if pos == len(s):
                return int(total_us)
        # 纯数字字符串按秒处理
        if s.replace(".", "", 1).isdigit():
            return int(float(s) * 1000000)
    return tim(inp)


def format_srt_time(us: int) -> str:
    """将微秒转换为 SRT 时间戳格式 (HH:MM:SS,mmm)"""
    ms = (us // 1000) % 1000
    s = (us // 1000000) % 60
    m = (us // 60000000) % 60
    h = us // 3600000000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ----------------- FFprobe 工具 -----------------


@functools.lru_cache(maxsize=128)
def _get_duration_ffprobe_cached(file_path: str, content_identity: str) -> float:
    """
    带缓存的 ffprobe 时长检测，防止重复开销。
    """
    if not os.path.exists(file_path):
        return 0.0
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
        return float(result.stdout.strip())
    except Exception as e:
        print(f"[warn] ffprobe failed for {os.path.basename(file_path)}: {e}")
        return 0.0


# ----------------- Enum 模糊匹配 -----------------
def get_duration_ffprobe_cached(file_path: str, content_identity: str = "") -> float:
    """Probe duration with a cache key tied to the current media bytes or file state."""

    absolute_path = os.path.abspath(file_path)
    if not os.path.exists(absolute_path):
        return 0.0
    if not content_identity:
        stat = os.stat(absolute_path)
        content_identity = f"stat:{stat.st_size}:{stat.st_mtime_ns}"
    return _get_duration_ffprobe_cached(absolute_path, str(content_identity))


get_duration_ffprobe_cached.cache_clear = _get_duration_ffprobe_cached.cache_clear
get_duration_ffprobe_cached.cache_info = _get_duration_ffprobe_cached.cache_info


def resolve_enum_with_synonyms(enum_cls, name: str, synonyms_dict: dict):
    """
    尝试从 Enum 类中找到匹配的属性。
    """
    if not name:
        return None

    if hasattr(enum_cls, name):
        return getattr(enum_cls, name)

    name_lower = name.lower()
    mapping = {k.lower(): k for k in enum_cls.__members__.keys()}

    if name_lower in mapping:
        real_key = mapping[name_lower]
        return getattr(enum_cls, real_key)

    for key, synonyms in synonyms_dict.items():
        if name_lower == key.lower():
            for candidate in synonyms:
                if candidate in mapping:
                    real_key = mapping[candidate]
                    print(f"[info] Map EN->CN: '{name}' -> '{real_key}'")
                    return getattr(enum_cls, real_key)

        if key.lower() in mapping:
            for syn in synonyms:
                if syn in name_lower or name_lower in syn:
                    real_key = mapping[key.lower()]
                    print(f"[info] Synonym Match: '{name}' -> '{real_key}'")
                    return getattr(enum_cls, real_key)

    matches = difflib.get_close_matches(name, enum_cls.__members__.keys(), n=1, cutoff=0.6)
    if matches:
        print(f"[info] Fuzzy Match: '{name}' -> '{matches[0]}'")
        return getattr(enum_cls, matches[0])
    return None
