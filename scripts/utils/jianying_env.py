import json
import os
import re
import subprocess
import uuid
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional

from utils.formatters import get_default_drafts_root

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None


APP_NAMES = ("JianyingPro", "CapCut")
WINDOWS_UNINSTALL_ROOTS = (
    r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


def _version_key(raw_value: Optional[str]) -> tuple:
    text = str(raw_value or "")
    parts = re.findall(r"\d+", text)
    if not parts:
        return tuple()
    return tuple(int(part) for part in parts)


def _canonical_app_name(raw_value: Optional[str]) -> str:
    value = str(raw_value or "").strip().casefold()
    return next((name for name in APP_NAMES if name.casefold() == value), "")


def _looks_like_install_root(path: Optional[str]) -> bool:
    if not path:
        return False
    root = os.path.abspath(path)
    return any(
        os.path.exists(os.path.join(root, f"{app_name}.exe")) or os.path.basename(root) == app_name
        for app_name in APP_NAMES
    )


def _candidate_from_install_root(
    install_root: str,
    *,
    app_name: Optional[str] = None,
    source: str,
    process_running: bool = False,
    registry_version: Optional[str] = None,
) -> Dict[str, Any]:
    root = os.path.abspath(install_root)
    normalized_app = app_name or _guess_app_name(root)
    version = _extract_version_from_path(root) or registry_version or ""
    executable = _resolve_executable(root, normalized_app)
    resources_root = os.path.join(root, "Resources")
    combine_adjust = os.path.join(resources_root, "DefaultAdjustBundle", "combine_adjust")
    return {
        "app_name": normalized_app,
        "app_version": version,
        "install_root": root,
        "executable_path": executable if executable and os.path.exists(executable) else "",
        "resources_root": resources_root if os.path.isdir(resources_root) else "",
        "combine_adjust_path": combine_adjust if os.path.isdir(combine_adjust) else "",
        "detection_source": source,
        "process_running": bool(process_running),
    }


def _guess_app_name(path: str) -> str:
    raw_path = os.path.abspath(path)
    basename = os.path.basename(raw_path).casefold()
    for app_name in APP_NAMES:
        if basename == f"{app_name}.exe".casefold():
            return app_name
    executable_root = os.path.dirname(raw_path) if basename.endswith(".exe") else raw_path
    executable_matches = [
        app_name
        for app_name in APP_NAMES
        if os.path.isfile(os.path.join(executable_root, f"{app_name}.exe"))
    ]
    if len(executable_matches) == 1:
        return executable_matches[0]
    lowered = str(path).lower()
    if "capcut" in lowered:
        return "CapCut"
    if "jianying" in lowered or "剪映" in str(path):
        return "JianyingPro"
    return "JianyingPro"


def _resolve_executable(root: str, app_name: str) -> str:
    direct = os.path.join(root, f"{app_name}.exe")
    if os.path.exists(direct):
        return direct
    if os.path.isfile(root) and root.lower().endswith(".exe"):
        return root
    return direct


def _extract_version_from_path(path: str) -> str:
    current = os.path.abspath(path)
    for part in reversed(current.split(os.sep)):
        if re.fullmatch(r"\d+(?:\.\d+){1,4}", part):
            return part
    return ""


def _choose_best_candidate(
    candidates: Iterable[Dict[str, Any]],
    preferred_app: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    rows = [
        row
        for row in candidates
        if row.get("selectable", True) and _canonical_app_name(str(row.get("app_name") or ""))
    ]
    if not rows:
        return None
    preferred = _canonical_app_name(preferred_app)
    preferred_rows = [row for row in rows if row.get("app_name") == preferred]
    if preferred:
        if not preferred_rows:
            return None
        rows = preferred_rows

    def capability_rank(item: Dict[str, Any]) -> tuple:
        return (
            1 if item.get("process_running") else 0,
            1 if item.get("combine_adjust_path") else 0,
            _version_key(item.get("app_version")),
        )

    best_rank = max(capability_rank(item) for item in rows)
    tied = [item for item in rows if capability_rank(item) == best_rank]

    def deterministic_key(item: Dict[str, Any]) -> tuple[str | int, ...]:
        app_name = str(item.get("app_name") or "")
        return (
            0 if app_name == "JianyingPro" else 1,
            os.path.normcase(os.path.abspath(str(item.get("install_root") or ""))),
            str(item.get("detection_source") or "").casefold(),
            os.path.normcase(str(item.get("executable_path") or "")),
        )

    return dict(min(tied, key=deterministic_key))


def _drafts_root_owner(path: str) -> str:
    components = [
        part.casefold() for part in os.path.abspath(path).replace("/", "\\").split("\\") if part
    ]
    owner_suffix = ("user data", "projects", "com.lveditor.draft")
    app_names = {"jianyingpro": "JianyingPro", "capcut": "CapCut"}
    owners = {
        app_names[component]
        for index, component in enumerate(components[:-3])
        if component in app_names and tuple(components[index + 1 : index + 4]) == owner_suffix
    }
    return next(iter(owners)) if len(owners) == 1 else ""


def infer_app_name_from_path(path: os.PathLike[str] | str) -> str:
    return _drafts_root_owner(os.fspath(path))


def _drafts_root_for_selected_app(app_name: str, default_root: str) -> str:
    fallback = os.path.abspath(default_root)
    owner = _drafts_root_owner(fallback)
    if app_name not in APP_NAMES or not owner or owner == app_name:
        return fallback
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        user_profile = os.environ.get("USERPROFILE")
        if user_profile:
            local_app_data = os.path.join(user_profile, "AppData", "Local")
    if not local_app_data:
        return fallback
    return os.path.abspath(
        os.path.join(
            local_app_data,
            app_name,
            "User Data",
            "Projects",
            "com.lveditor.draft",
        )
    )


def _powershell_json_result(command: str) -> Dict[str, Any]:
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [Console]::OutputEncoding; "
        "$ErrorActionPreference='SilentlyContinue'; "
        f"{command} | ConvertTo-Json -Depth 4 -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "unknown", "reason": "execution_failed", "rows": []}
    if completed.returncode != 0:
        return {"status": "unknown", "reason": "command_failed", "rows": []}
    if (completed.stderr or "").strip():
        return {"status": "unknown", "reason": "command_error_output", "rows": []}
    stdout = (completed.stdout or "").strip()
    if not stdout:
        return {"status": "known", "reason": "", "rows": []}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {"status": "unknown", "reason": "invalid_output", "rows": []}
    if isinstance(parsed, list):
        rows = [item for item in parsed if isinstance(item, dict)]
        if len(rows) != len(parsed):
            return {"status": "unknown", "reason": "invalid_output", "rows": []}
        return {"status": "known", "reason": "", "rows": rows}
    if isinstance(parsed, dict):
        return {"status": "known", "reason": "", "rows": [parsed]}
    return {"status": "unknown", "reason": "invalid_output", "rows": []}


def _powershell_json(command: str) -> List[Dict[str, Any]]:
    result = _powershell_json_result(command)
    return list(result["rows"]) if result["status"] == "known" else []


def _process_app_name(process_name: object) -> str:
    name = str(process_name or "").strip().casefold()
    if "capcut" in name:
        return "CapCut"
    if "jianying" in name or "剪映" in name:
        return "JianyingPro"
    return ""


def _read_running_processes() -> Dict[str, Any]:
    command = (
        "Get-Process | Where-Object { $_.ProcessName -match 'Jianying|CapCut' } | "
        "Select-Object @{Name='process_name';Expression={$_.ProcessName}},"
        "@{Name='path';Expression={$_.Path}},"
        "@{Name='main_window_title';Expression={$_.MainWindowTitle}}"
    )
    result = _powershell_json_result(command)
    matches = []
    for row in result["rows"]:
        process_name = str(row.get("process_name") or "").strip()
        if not _process_app_name(process_name):
            continue
        matches.append(
            {
                "process_name": process_name,
                "path": str(row.get("path") or "").strip(),
                "main_window_title": str(row.get("main_window_title") or ""),
            }
        )
    return {
        "status": result["status"],
        "reason": result["reason"],
        "matches": matches,
    }


def _read_registry_installs() -> List[Dict[str, Any]]:
    if winreg is not None:
        return _read_registry_installs_winreg()
    return _read_registry_installs_powershell()


def _read_registry_installs_winreg() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    root_specs = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            winreg.KEY_WOW64_64KEY,
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            winreg.KEY_WOW64_32KEY,
        ),
    ]
    access_base = getattr(winreg, "KEY_READ", 0)
    for hive, subkey, wow_flag in root_specs:
        try:
            with winreg.OpenKey(hive, subkey, 0, access_base | wow_flag) as key:
                count, _, _ = winreg.QueryInfoKey(key)
                for index in range(count):
                    try:
                        child_name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, child_name, 0, access_base | wow_flag) as child:
                            display_name = _query_reg_value(child, "DisplayName")
                            if not re.search(
                                r"Jianying|CapCut|剪映", str(display_name or ""), re.IGNORECASE
                            ):
                                continue
                            entries.append(
                                {
                                    "display_name": display_name or "",
                                    "install_location": _query_reg_value(child, "InstallLocation")
                                    or "",
                                    "display_version": _query_reg_value(child, "DisplayVersion")
                                    or "",
                                    "uninstall_string": _query_reg_value(child, "UninstallString")
                                    or "",
                                }
                            )
                    except OSError:
                        continue
        except OSError:
            continue
    return entries


def _query_reg_value(key, name: str) -> Optional[str]:
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return str(value)
    except OSError:
        return None


def _read_registry_installs_powershell() -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for root in WINDOWS_UNINSTALL_ROOTS:
        command = (
            f"Get-ChildItem '{root}' | Get-ItemProperty | "
            "Where-Object { $_.DisplayName -match 'Jianying|CapCut|剪映' } | "
            "Select-Object @{Name='display_name';Expression={$_.DisplayName}},"
            "@{Name='install_location';Expression={$_.InstallLocation}},"
            "@{Name='display_version';Expression={$_.DisplayVersion}},"
            "@{Name='uninstall_string';Expression={$_.UninstallString}}"
        )
        results.extend(_powershell_json(command))
    return results


def _read_local_app_installs() -> List[str]:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return []
    roots = []
    for app_name in APP_NAMES:
        for candidate in (
            os.path.join(local_app_data, app_name),
            os.path.join(local_app_data, app_name, "Apps"),
        ):
            if os.path.isdir(candidate):
                roots.append(candidate)
    return roots


def _read_standard_installs() -> List[str]:
    candidates: List[str] = []
    for base in filter(
        None,
        [
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            "C:\\Program Files",
            "C:\\Program Files (x86)",
        ],
    ):
        for app_name in APP_NAMES:
            path = os.path.join(base, app_name)
            if os.path.isdir(path):
                candidates.append(path)
    return candidates


def _expand_install_roots(root: str) -> List[str]:
    expanded: List[str] = []
    if _looks_like_install_root(root):
        expanded.append(os.path.abspath(root))
    if not os.path.isdir(root):
        return expanded
    for child in os.listdir(root):
        child_path = os.path.join(root, child)
        if not os.path.isdir(child_path):
            continue
        if _looks_like_install_root(child_path):
            expanded.append(os.path.abspath(child_path))
            continue
        if re.fullmatch(r"\d+(?:\.\d+){1,4}", child):
            if any(
                os.path.exists(os.path.join(child_path, f"{app_name}.exe"))
                for app_name in APP_NAMES
            ):
                expanded.append(os.path.abspath(child_path))
    return expanded


def _collect_candidates(include_process: bool) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    seen_roots = set()

    if include_process:
        raw_process_probe = _read_running_processes()
        if isinstance(raw_process_probe, dict):
            process_probe_status = str(raw_process_probe.get("status") or "unknown")
            process_probe_reason = str(raw_process_probe.get("reason") or "")
            raw_processes = raw_process_probe.get("matches")
            if process_probe_status not in {"known", "unknown"} or not isinstance(
                raw_processes, list
            ):
                process_probe_status = "unknown"
                process_probe_reason = "invalid_probe_evidence"
                raw_processes = []
        elif isinstance(raw_process_probe, list):
            process_probe_status = "known"
            process_probe_reason = ""
            raw_processes = raw_process_probe
        else:
            process_probe_status = "unknown"
            process_probe_reason = "invalid_probe_evidence"
            raw_processes = []

        process_matches = []
        for proc in raw_processes:
            if not isinstance(proc, dict):
                process_probe_status = "unknown"
                process_probe_reason = "invalid_probe_evidence"
                process_matches = []
                break
            process_name = str(proc.get("process_name") or "").strip()
            app_name = _process_app_name(process_name)
            if not app_name:
                continue
            exe_path = proc.get("path") or ""
            process_matches.append(
                {
                    "app_name": app_name,
                    "path_available": bool(exe_path),
                    "process_name": process_name,
                }
            )
            if not exe_path:
                candidates.append(
                    {
                        "app_name": app_name,
                        "app_version": "",
                        "install_root": "",
                        "executable_path": "",
                        "resources_root": "",
                        "combine_adjust_path": "",
                        "detection_source": "running_process",
                        "process_name": process_name,
                        "process_running": True,
                        "selectable": False,
                    }
                )
                continue
            install_root = os.path.dirname(exe_path)
            key = os.path.abspath(install_root)
            if key in seen_roots:
                continue
            seen_roots.add(key)
            candidates.append(
                _candidate_from_install_root(
                    install_root,
                    app_name=app_name,
                    source="running_process",
                    process_running=True,
                )
            )
            candidates[-1]["process_name"] = process_name

        candidates.append(
            {
                "evidence_type": "editor_process_probe",
                "process_probe_status": process_probe_status,
                "process_probe_reason": process_probe_reason,
                "matching_editor_processes": process_matches,
                "process_running": bool(process_matches),
                "selectable": False,
            }
        )

    for item in _read_registry_installs():
        install_location = item.get("install_location") or ""
        uninstall_string = str(item.get("uninstall_string") or "").strip().strip('"')
        if not install_location and uninstall_string:
            install_location = os.path.dirname(uninstall_string)
        if not install_location:
            continue
        for root in _expand_install_roots(install_location):
            key = os.path.abspath(root)
            if key in seen_roots:
                continue
            seen_roots.add(key)
            candidates.append(
                _candidate_from_install_root(
                    root,
                    app_name=_guess_app_name(root),
                    source="registry",
                    registry_version=item.get("display_version") or "",
                )
            )

    for source_name, roots in (
        ("local_appdata", _read_local_app_installs()),
        ("standard_scan", _read_standard_installs()),
    ):
        for root in roots:
            for expanded_root in _expand_install_roots(root):
                key = os.path.abspath(expanded_root)
                if key in seen_roots:
                    continue
                seen_roots.add(key)
                candidates.append(
                    _candidate_from_install_root(
                        expanded_root,
                        app_name=_guess_app_name(expanded_root),
                        source=source_name,
                    )
                )
    return candidates


def resolve_adjust_bundle_path(default_path: Optional[str] = None) -> str:
    detected = detect_jianying_environment(include_process=False)
    if detected.get("combine_adjust_path"):
        return detected["combine_adjust_path"]
    fallback = default_path or ""
    return fallback


@lru_cache(maxsize=8)
def _detect_jianying_environment_cached(
    include_process: bool,
    preferred_app: str,
) -> Dict[str, Any]:
    default_drafts_root = os.path.abspath(get_default_drafts_root())
    collected = _collect_candidates(include_process)
    process_probe_rows = [
        candidate
        for candidate in collected
        if candidate.get("evidence_type") == "editor_process_probe"
    ]
    candidates = [
        candidate
        for candidate in collected
        if candidate.get("evidence_type") != "editor_process_probe"
    ]
    if include_process and process_probe_rows:
        process_probe = process_probe_rows[-1]
        process_probe_status = str(process_probe.get("process_probe_status") or "unknown")
        process_probe_reason = str(process_probe.get("process_probe_reason") or "")
        matching_editor_processes = list(process_probe.get("matching_editor_processes") or [])
    elif include_process:
        process_probe_status = "known"
        process_probe_reason = ""
        matching_editor_processes = [
            {
                "app_name": str(candidate.get("app_name") or ""),
                "path_available": bool(candidate.get("executable_path")),
                "process_name": str(
                    candidate.get("process_name") or candidate.get("app_name") or ""
                ),
            }
            for candidate in candidates
            if candidate.get("process_running")
        ]
    else:
        process_probe_status = "not_requested"
        process_probe_reason = ""
        matching_editor_processes = []
    matching_editor_processes = sorted(
        matching_editor_processes,
        key=lambda row: (
            str(row.get("app_name") or "").casefold(),
            str(row.get("process_name") or "").casefold(),
            bool(row.get("path_available")),
        ),
    )
    detected_app_identities = sorted(
        {
            str(candidate.get("app_name") or "")
            for candidate in candidates
            if str(candidate.get("app_name") or "") in APP_NAMES
        }
    )
    any_editor_process_running = bool(matching_editor_processes) or any(
        bool(candidate.get("process_running")) for candidate in candidates
    )
    editor_process_state_unknown = process_probe_status == "unknown"
    editor_process_gate_clear = process_probe_status == "known" and not any_editor_process_running
    selected = _choose_best_candidate(candidates, preferred_app=preferred_app)
    if not selected:
        return {
            "found": False,
            "app_name": "",
            "app_version": "",
            "install_root": "",
            "executable_path": "",
            "resources_root": "",
            "combine_adjust_path": "",
            "drafts_root": default_drafts_root,
            "process_running": False,
            "ui_automation_available": False,
            "detection_source": "none",
            "warnings": ["No JianyingPro/CapCut install detected"],
            "candidates": candidates,
            "detected_app_identities": detected_app_identities,
            "app_identity_ambiguous": len(detected_app_identities) > 1,
            "any_editor_process_running": any_editor_process_running,
            "editor_process_probe_status": process_probe_status,
            "editor_process_probe_reason": process_probe_reason,
            "editor_process_state_unknown": editor_process_state_unknown,
            "editor_process_gate_clear": editor_process_gate_clear,
            "matching_editor_processes": matching_editor_processes,
        }

    selected["found"] = True
    selected["drafts_root"] = _drafts_root_for_selected_app(
        str(selected.get("app_name") or ""), default_drafts_root
    )
    selected["ui_automation_available"] = selected.get("app_name") == "JianyingPro"
    selected["warnings"] = (
        ["Editor process state could not be determined"] if editor_process_state_unknown else []
    )
    selected["candidates"] = candidates
    selected["detected_app_identities"] = detected_app_identities
    selected["app_identity_ambiguous"] = len(detected_app_identities) > 1
    selected["any_editor_process_running"] = any_editor_process_running
    selected["editor_process_probe_status"] = process_probe_status
    selected["editor_process_probe_reason"] = process_probe_reason
    selected["editor_process_state_unknown"] = editor_process_state_unknown
    selected["editor_process_gate_clear"] = editor_process_gate_clear
    selected["matching_editor_processes"] = matching_editor_processes
    return selected


def clear_environment_cache() -> None:
    _detect_jianying_environment_cached.cache_clear()


def detect_jianying_environment(
    force_refresh: bool = False,
    include_process: bool = True,
    preferred_app: Optional[str] = None,
) -> Dict[str, Any]:
    if force_refresh:
        clear_environment_cache()
    payload = _detect_jianying_environment_cached(
        include_process,
        _canonical_app_name(preferred_app),
    )
    return json.loads(json.dumps(payload, ensure_ascii=False))


def stamp_draft_platform(
    payload: Dict[str, Any],
    app_version: Optional[str],
    *,
    initialize_new: bool = False,
) -> Dict[str, Any]:
    detected_version = str(app_version or "").strip()
    saved_versions = []
    for field in ("platform", "last_modified_platform"):
        block = payload.get(field)
        if isinstance(block, dict):
            saved_version = str(block.get("app_version") or "").strip()
            if saved_version:
                saved_versions.append(saved_version)
    fallback_version = (
        detected_version
        if initialize_new and detected_version
        else saved_versions[0] if saved_versions else ""
    )

    for field in ("platform", "last_modified_platform"):
        block = payload.get(field)
        if block is None:
            block = {}
            payload[field] = block
        if isinstance(block, dict):
            if fallback_version and (
                initialize_new or not str(block.get("app_version") or "").strip()
            ):
                block["app_version"] = fallback_version
            block.setdefault("os", "windows")
    return payload


def stamp_draft_meta(
    meta: Dict[str, Any], *, project_name: str, drafts_root: str
) -> Dict[str, Any]:
    root = os.path.abspath(drafts_root)
    draft_dir = os.path.join(root, project_name)
    meta["draft_name"] = project_name
    meta["draft_root_path"] = root
    meta["draft_fold_path"] = draft_dir
    return meta


def _normalize_material_path(path: Optional[str]) -> str:
    if not path:
        return ""
    return os.path.abspath(path).replace("\\", "/")


def _build_local_material_entry(item: Dict[str, Any], *, metetype: str) -> Optional[Dict[str, Any]]:
    path = _normalize_material_path(item.get("path"))
    if not path:
        return None

    raw_duration = item.get("duration")
    try:
        duration = int(raw_duration or 0)
    except (TypeError, ValueError):
        duration = 0

    width = 0
    height = 0
    if metetype == "video":
        try:
            width = int(item.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        try:
            height = int(item.get("height") or 0)
        except (TypeError, ValueError):
            height = 0

    timestamp = int(os.path.getmtime(path)) if os.path.exists(path) else 0
    import_time_ms = int(timestamp * 1_000_000)
    material_id = str(item.get("id") or uuid.uuid4())
    roughcut_duration = duration if duration > 0 else -1
    file_name = os.path.basename(path)

    return {
        "create_time": timestamp,
        "duration": duration,
        "extra_info": file_name,
        "file_Path": path,
        "height": height,
        "id": material_id,
        "import_time": timestamp,
        "import_time_ms": import_time_ms,
        "item_source": 1,
        "md5": "",
        "metetype": metetype,
        "roughcut_time_range": {
            "duration": roughcut_duration,
            "start": 0 if duration > 0 else -1,
        },
        "sub_time_range": {"duration": -1, "start": -1},
        "type": 0,
        "width": width,
    }


def _normalize_content_media_materials(content: Dict[str, Any]) -> bool:
    materials = content.get("materials", {}) or {}
    changed = False

    for item in materials.get("videos", []) or []:
        if not isinstance(item, dict):
            continue
        material_id = str(item.get("id") or item.get("material_id") or "").strip()
        if not material_id:
            continue
        if not str(item.get("local_material_id") or "").strip():
            item["local_material_id"] = material_id
            changed = True
        if not str(item.get("material_id") or "").strip():
            item["material_id"] = material_id
            changed = True

    for item in materials.get("audios", []) or []:
        if not isinstance(item, dict):
            continue
        material_id = str(
            item.get("id") or item.get("material_id") or item.get("music_id") or ""
        ).strip()
        if not material_id:
            continue
        if not str(item.get("local_material_id") or "").strip():
            item["local_material_id"] = material_id
            changed = True
        if not str(item.get("music_id") or "").strip():
            item["music_id"] = material_id
            changed = True

    return changed


def _sync_meta_materials_from_content(
    meta: Dict[str, Any], content: Dict[str, Any]
) -> Dict[str, Any]:
    materials = content.get("materials", {}) or {}
    local_materials: List[Dict[str, Any]] = []

    for item in materials.get("videos", []) or []:
        if not isinstance(item, dict):
            continue
        entry = _build_local_material_entry(item, metetype="video")
        if entry:
            local_materials.append(entry)

    for item in materials.get("audios", []) or []:
        if not isinstance(item, dict):
            continue
        entry = _build_local_material_entry(item, metetype="music")
        if entry:
            local_materials.append(entry)

    draft_materials = meta.get("draft_materials")
    if not isinstance(draft_materials, list):
        draft_materials = []
    bucket_zero = None
    for bucket in draft_materials:
        if isinstance(bucket, dict) and bucket.get("type") == 0:
            bucket_zero = bucket
            break
    if bucket_zero is None:
        bucket_zero = {"type": 0, "value": []}
        draft_materials.insert(0, bucket_zero)
    bucket_zero["value"] = local_materials
    meta["draft_materials"] = draft_materials

    material_size = 0
    for item in local_materials:
        file_path = item.get("file_Path") or ""
        normalized = file_path.replace("/", os.sep)
        if os.path.exists(normalized):
            material_size += os.path.getsize(normalized)

    try:
        meta["draft_timeline_materials_size_"] = int(material_size)
    except (TypeError, ValueError):
        meta["draft_timeline_materials_size_"] = 0
    try:
        meta["tm_duration"] = int(content.get("duration", 0) or 0)
    except (TypeError, ValueError):
        meta["tm_duration"] = 0

    return meta


def _material_ids_for_virtual_store(content: Dict[str, Any]) -> List[str]:
    materials = content.get("materials", {}) or {}
    material_ids: List[str] = []
    seen = set()

    for bucket_name in ("videos", "audios"):
        for item in materials.get(bucket_name, []) or []:
            if not isinstance(item, dict):
                continue
            material_id = str(
                item.get("id") or item.get("material_id") or item.get("music_id") or ""
            ).strip()
            if not material_id or material_id in seen:
                continue
            seen.add(material_id)
            material_ids.append(material_id)

    return material_ids


def _default_virtual_store_payload() -> Dict[str, Any]:
    return {
        "draft_materials": [],
        "draft_virtual_store": [
            {
                "type": 0,
                "value": [
                    {
                        "creation_time": 0,
                        "display_name": "",
                        "filter_type": 0,
                        "id": "",
                        "import_time": 0,
                        "import_time_us": 0,
                        "sort_sub_type": 0,
                        "sort_type": 0,
                        "subdraft_filter_type": 0,
                    }
                ],
            },
            {"type": 1, "value": []},
            {"type": 2, "value": []},
        ],
    }


def _ensure_virtual_store_bucket(
    payload: Dict[str, Any], bucket_type: int, default_value: Any
) -> Dict[str, Any]:
    buckets = payload.setdefault("draft_virtual_store", [])
    if not isinstance(buckets, list):
        buckets = []
        payload["draft_virtual_store"] = buckets

    for bucket in buckets:
        if isinstance(bucket, dict) and bucket.get("type") == bucket_type:
            if not isinstance(bucket.get("value"), list):
                bucket["value"] = default_value
            return bucket

    bucket = {"type": bucket_type, "value": default_value}
    buckets.append(bucket)
    return bucket


def _sync_virtual_store_materials_from_content(
    draft_dir: str, content: Dict[str, Any]
) -> Dict[str, Any]:
    virtual_store_path = os.path.join(draft_dir, "draft_virtual_store.json")
    payload: Dict[str, Any]

    if os.path.exists(virtual_store_path):
        try:
            with open(virtual_store_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            payload = loaded if isinstance(loaded, dict) else _default_virtual_store_payload()
        except Exception:
            payload = _default_virtual_store_payload()
    else:
        payload = _default_virtual_store_payload()

    if not isinstance(payload.get("draft_materials"), list):
        payload["draft_materials"] = []

    _ensure_virtual_store_bucket(
        payload,
        0,
        _default_virtual_store_payload()["draft_virtual_store"][0]["value"],
    )
    bucket_one = _ensure_virtual_store_bucket(payload, 1, [])
    _ensure_virtual_store_bucket(payload, 2, [])

    value = bucket_one.setdefault("value", [])
    if not isinstance(value, list):
        value = []
        bucket_one["value"] = value

    existing = {str(item.get("child_id") or "") for item in value if isinstance(item, dict)}
    for material_id in _material_ids_for_virtual_store(content):
        if material_id not in existing:
            value.append({"child_id": material_id, "parent_id": ""})
            existing.add(material_id)

    with open(virtual_store_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    return payload


def sync_draft_runtime_metadata(
    draft_dir: str, project_name: str, drafts_root: Optional[str] = None
) -> Dict[str, Any]:
    root = os.path.abspath(drafts_root or os.path.dirname(draft_dir))
    preferred_app = infer_app_name_from_path(root)
    env = detect_jianying_environment(
        include_process=False,
        preferred_app=preferred_app,
    )
    content: Dict[str, Any] = {}

    content_path = os.path.join(draft_dir, "draft_content.json")
    content_paths = [content_path]
    timelines_root = os.path.join(draft_dir, "Timelines")
    if os.path.isdir(timelines_root):
        for entry in sorted(os.scandir(timelines_root), key=lambda row: row.name.casefold()):
            if entry.is_dir(follow_symlinks=False):
                timeline_content = os.path.join(entry.path, "draft_content.json")
                if os.path.isfile(timeline_content):
                    content_paths.append(timeline_content)
    stamp_platform = bool(preferred_app) or not bool(env.get("app_identity_ambiguous"))
    for candidate_path in content_paths:
        if not os.path.exists(candidate_path):
            continue
        try:
            with open(candidate_path, "r", encoding="utf-8") as f:
                candidate_content = json.load(f)
            _normalize_content_media_materials(candidate_content)
            stamp_draft_platform(
                candidate_content,
                env.get("app_version") if stamp_platform else None,
            )
            with open(candidate_path, "w", encoding="utf-8") as f:
                json.dump(candidate_content, f, ensure_ascii=False, indent=4)
            if candidate_path == content_path:
                content = candidate_content
        except Exception:
            pass

    meta_path = os.path.join(draft_dir, "draft_meta_info.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            stamp_draft_meta(meta, project_name=project_name, drafts_root=root)
            if content:
                _sync_meta_materials_from_content(meta, content)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    if content:
        try:
            _sync_virtual_store_materials_from_content(draft_dir, content)
        except Exception:
            pass

    return env
