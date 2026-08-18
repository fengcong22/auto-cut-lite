from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from scripts.utils.jianying_smoke import (
        blocked_jianying_checks_valid,
        smoke_editability_receipt_valid,
    )
except ModuleNotFoundError:
    from utils.jianying_smoke import (
        blocked_jianying_checks_valid,
        smoke_editability_receipt_valid,
    )

try:
    from scripts.release.offline_bundle import (
        extract_offline_bundle,
        verify_offline_bundle,
    )
    from scripts.release.offline_bundle import (
        sha256_file as offline_sha256_file,
    )
except ModuleNotFoundError:
    from release.offline_bundle import (  # type: ignore[no-redef]
        extract_offline_bundle,
        verify_offline_bundle,
    )
    from release.offline_bundle import (
        sha256_file as offline_sha256_file,
    )

EXPECTED_SKILL_COUNT = 16
INSTALLER_VERSION = "1.7.0"
INSTALL_REPORT_RELATIVE_PATH = Path("tmp") / "install" / "install-report.json"
OFFLINE_RUNTIME_RELATIVE_PATH = Path("tmp") / "offline-runtime"
OFFLINE_PLAYWRIGHT_VERSION = "1.52.0"
OFFLINE_CHROMIUM_REVISION = "1169"
OFFLINE_CHROMIUM_VERSION = "136.0.7103.25"
OFFLINE_PLAYWRIGHT_FFMPEG_REVISION = "1011"
OFFLINE_PLAYWRIGHT_WINLDD_REVISION = "1007"
OFFLINE_PLAYWRIGHT_LICENSE_SIZE = 11399
OFFLINE_PLAYWRIGHT_LICENSE_SHA256 = (
    "7fab1461b41970ff376f1c9303a637076bfaaeb71cd12dd3a1c44aaf59a1a2b9"
)
# The general installer deliberately accepts only the FFmpeg bytes that were
OFFLINE_FFMPEG_RUNTIME_FILES = {
    "tools/ffmpeg/bin/ffmpeg.exe": (
        11714560,
        "ab285a50429cf7e0d4a0ede6f751242db6a410210447cd3f38af2d905a57ce43",
    ),
    "tools/ffmpeg/bin/ffprobe.exe": (
        11508224,
        "e0134a4c4e93ba8fb3b5cf336405e077e312e27f8bb557f7b942a4b3bbaa3372",
    ),
    "tools/ffmpeg/LICENSE.txt": (
        26517,
        "246041b6ecf9bc32d718a62c57877c78b5eb397b6467e74ed7ae2626ab189c30",
    ),
}
OFFLINE_FFMPEG_EXECUTABLES = {
    path: identity
    for path, identity in OFFLINE_FFMPEG_RUNTIME_FILES.items()
    if path.endswith(".exe")
}
OFFLINE_FFMPEG_LICENSE_FILES = {
    "tools/ffmpeg/licenses/COPYING.GPLv2": (
        18092,
        "8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643",
    ),
    "tools/ffmpeg/licenses/COPYING.GPLv3": (
        35147,
        "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
    ),
    "tools/ffmpeg/licenses/COPYING.LGPLv2.1": (
        26517,
        "246041b6ecf9bc32d718a62c57877c78b5eb397b6467e74ed7ae2626ab189c30",
    ),
    "tools/ffmpeg/licenses/COPYING.LGPLv3": (
        7651,
        "da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768",
    ),
    "tools/ffmpeg/licenses/gcc-COPYING.RUNTIME": (
        3324,
        "9d6b43ce4d8de0c878bf16b54d8e7a10d9bd42b75178153e3af6a815bdc90f74",
    ),
    "tools/ffmpeg/licenses/gcc-COPYING.LIB": (
        26527,
        "a9bdde5616ecdd1e980b44f360600ee8783b1f99b8cc83a2beb163a0a390e861",
    ),
    "tools/ffmpeg/licenses/gcc-COPYING3": (
        35147,
        "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
    ),
    "tools/ffmpeg/licenses/COPYING.MinGW-w64-runtime.txt.b64": (
        16208,
        "124605dcbaf5921326b18d21aa57b58ac63da393b3f321e919c627d7cf5934ce",
    ),
    "tools/ffmpeg/licenses/COPYING.MinGW-w64.txt": (
        5905,
        "f38e6194bd3bfa1b654f118e5acefe0aead437bbe669eee43957ccc65a7127f1",
    ),
}
OFFLINE_FFMPEG_LICENSE_METADATA = {
    "tools/ffmpeg/licenses/COPYING.GPLv2": (
        "GPL-2.0-only",
        "repository:scripts/release/ffmpeg_assets/licenses/ffmpeg/COPYING.GPLv2",
    ),
    "tools/ffmpeg/licenses/COPYING.GPLv3": (
        "GPL-3.0-only",
        "repository:scripts/release/ffmpeg_assets/licenses/ffmpeg/COPYING.GPLv3",
    ),
    "tools/ffmpeg/licenses/COPYING.LGPLv2.1": (
        "LGPL-2.1-only",
        "repository:scripts/release/ffmpeg_assets/licenses/ffmpeg/COPYING.LGPLv2.1",
    ),
    "tools/ffmpeg/licenses/COPYING.LGPLv3": (
        "LGPL-3.0-only",
        "repository:scripts/release/ffmpeg_assets/licenses/ffmpeg/COPYING.LGPLv3",
    ),
    "tools/ffmpeg/licenses/gcc-COPYING.RUNTIME": (
        "GCC Runtime Library Exception",
        "repository:scripts/release/ffmpeg_assets/licenses/gcc/COPYING.RUNTIME",
    ),
    "tools/ffmpeg/licenses/gcc-COPYING.LIB": (
        "LGPL-2.1-only",
        "repository:scripts/release/ffmpeg_assets/licenses/gcc/COPYING.LIB",
    ),
    "tools/ffmpeg/licenses/gcc-COPYING3": (
        "GPL-3.0-only",
        "repository:scripts/release/ffmpeg_assets/licenses/gcc/COPYING3",
    ),
    "tools/ffmpeg/licenses/COPYING.MinGW-w64-runtime.txt.b64": (
        "BSD-3-Clause and LGPL-3.0-or-later",
        "repository:scripts/release/ffmpeg_assets/licenses/mingw/"
        "COPYING.MinGW-w64-runtime.txt.b64",
    ),
    "tools/ffmpeg/licenses/COPYING.MinGW-w64.txt": (
        "Zlib",
        "repository:scripts/release/ffmpeg_assets/licenses/mingw/COPYING.MinGW-w64.txt",
    ),
}
OFFLINE_FFMPEG_ATTESTATION_FILES = {
    "provenance/ffmpeg/build/build_ffmpeg.sh": (
        3484,
        "bb502ed910d1247cad597b28233cc033be07223e230d8d047084ec776e6daf4d",
        "ffmpeg_build_evidence",
        "source",
        "MIT",
        "repository:scripts/release/ffmpeg_assets/build/build_ffmpeg.sh",
    ),
    "provenance/ffmpeg/build/build-receipt.json": (
        8026,
        "925315ddb376cf506bd57b916d4ce278ca8c3073b2d463c7f7394585bf2e83ab",
        "ffmpeg_build_evidence",
        "source",
        "MIT",
        "repository:scripts/release/ffmpeg_assets/build/build-receipt.json",
    ),
    "provenance/ffmpeg/build/pe-imports.json": (
        574,
        "9ffac372d69bfabde94c56a8ae77b98a405145cb398052a4f49c5b9281f6ca8c",
        "ffmpeg_build_evidence",
        "source",
        "MIT",
        "repository:scripts/release/ffmpeg_assets/build/pe-imports.json",
    ),
    "provenance/ffmpeg/build/media-probe.json": (
        905,
        "54a7114c25cc8cd15e879747cf153f2d228e3ef2110a3fce58cebe23f16ebf74",
        "ffmpeg_build_evidence",
        "source",
        "MIT",
        "repository:scripts/release/ffmpeg_assets/build/media-probe.json",
    ),
    "provenance/ffmpeg/manifest.json": (
        4472,
        "e5722d3595f6f55a34fce3ca96d42f55db7385d47209b4f587fed3ba58122cd0",
        "ffmpeg_asset_manifest",
        "any",
        "MIT",
        "repository:scripts/release/ffmpeg_assets/manifest.json",
    ),
    "provenance/ffmpeg/license-encoding.json": (
        684,
        "9036d46d6941dfa6cc2723510461d228a5bff5c087b6d6fd4cb105598eebaf84",
        "ffmpeg_license_index",
        "any",
        "MIT",
        "repository:scripts/release/offline_sources.json",
    ),
}
# audited for the 1.7.0 offline companion.  A self-consistent manifest is not
# sufficient evidence for a native executable: these identities tie the
# target-side install back to the committed, reproducible source/runtime
# assets used by the release builder.
OFFLINE_FFMPEG_VERSION = "8.1.2"
OFFLINE_FFMPEG_RELEASE_TAG = "n8.1.2"
OFFLINE_FFMPEG_SOURCE_KIND = "committed_repository_asset"
OFFLINE_FFMPEG_RUNTIME_ARCHIVE_SHA256 = (
    "40a5867e1b229b787b1886efdf9dfc1f80afc75d0ecd28af9c51d66f13ecd963"
)
OFFLINE_FFMPEG_SOURCE_ARCHIVE_SHA256 = (
    "9fd092511605bbebafe095ea6d38d9e40f34d12f7386e1258372df8be0576eb7"
)
OFFLINE_FFMPEG_LICENSE = "LGPL-2.1-or-later with GCC and MinGW runtime terms"
OFFLINE_FFMPEG_RUNTIME_SOURCE = (
    "repository:scripts/release/ffmpeg_assets/runtime/ffmpeg-minimal-runtime.zip"
)
OFFLINE_FFMPEG_BUILD_SOURCE_IDENTITY = (
    "sha256:bb502ed910d1247cad597b28233cc033be07223e230d8d047084ec776e6daf4d"
)
OFFLINE_FFMPEG_SOURCE_IDENTITY = (
    "sha256:9fd092511605bbebafe095ea6d38d9e40f34d12f7386e1258372df8be0576eb7"
)
OFFLINE_FFMPEG_ASSET_MANIFEST_SELF_SHA256 = (
    "ef6ecd14ab2e71558961ea56ca564701a237dd5ca9a1cb2aaf732f37a29242f3"
)
OFFLINE_FFMPEG_ENCODED_LICENSE_RECEIPT = {
    "path": "tools/ffmpeg/licenses/COPYING.MinGW-w64-runtime.txt.b64",
    "encoding": "base64",
    "encoded_size": 16208,
    "encoded_sha256": "124605dcbaf5921326b18d21aa57b58ac63da393b3f321e919c627d7cf5934ce",
    "content_size": 12155,
    "content_sha256": "1db8da07b436c68833c0673ffee3d9fcb2526047f3820b81661865dfedc79a1f",
    "source": "repository:scripts/release/ffmpeg_assets/licenses/mingw/"
    "COPYING.MinGW-w64-runtime.txt.b64",
}


def _expected_ffmpeg_license_source_rows() -> dict[str, dict[str, object]]:
    expected: dict[str, dict[str, object]] = {}
    for relative, (license_name, source) in OFFLINE_FFMPEG_LICENSE_METADATA.items():
        filename = PurePosixPath(relative).name
        source_path = source.removeprefix("repository:")
        row: dict[str, object] = {
            "filename": filename,
            "path": source_path,
            "url": source,
            "size": OFFLINE_FFMPEG_LICENSE_FILES[relative][0],
            "sha256": OFFLINE_FFMPEG_LICENSE_FILES[relative][1],
            "license": license_name,
        }
        if filename == OFFLINE_FFMPEG_ENCODED_LICENSE_RECEIPT["path"].split("/")[-1]:
            row.update(
                {
                    "encoding": "base64",
                    "content_size": OFFLINE_FFMPEG_ENCODED_LICENSE_RECEIPT[
                        "content_size"
                    ],
                    "content_sha256": OFFLINE_FFMPEG_ENCODED_LICENSE_RECEIPT[
                        "content_sha256"
                    ],
                }
            )
        expected[filename] = row
    return expected


MAX_REPORT_STRING_LENGTH = 2048
STAGE_STATUSES = frozenset({"ready", "degraded", "pending", "unavailable", "failed", "skipped"})
CAPABILITY_STATUSES = frozenset(
    {"ready", "degraded", "pending", "unavailable", "failed", "skipped"}
)
CAPABILITY_RESULT_CODES = frozenset(
    {
        "bundled_contract_verified",
        "bundled_source_verified",
        "install_on_first_run",
        "requires_local_jianying",
        "requires_local_software",
        "requires_user_assets",
        "requires_user_authorization",
        "requires_user_resync",
        "unverified_external_model",
        "verification_failed",
        "verification_skipped",
        "verified_degraded",
        "verified_ready",
        "verified_unavailable",
        "configuration_present_unverified",
        "external_service_unverified",
        "static_index_ready_remote_unverified",
        "cloud_index_empty",
        "deterministic_fallback",
        "optional_local_tool_required",
        "target_local_carnac_found",
        "windows_only_optional_tool",
    }
)
CAPABILITY_CLASSIFICATION_FIELDS = frozenset(
    {
        "bundled",
        "installed_on_first_run",
        "requires_local_jianying",
        "requires_user_authorization",
        "requires_user_assets",
        "unavailable",
    }
)
REQUIRED_CAPABILITY_IDS = frozenset(
    {
        "editable_draft_contract",
        "auto_cut_skills",
        "main_python_dependencies",
        "playwright_chromium",
        "jianying_smoke_draft",
        "favorite_text_assets",
        "subject_pointer_profiles",
        "feishu_notifications",
        "ffmpeg",
        "ffprobe",
        "audio_runtime",
        "spectramini_cleanup",
        "deepfilternet",
        "respiro",
        "volc_asr_alignment",
        "sami_tts",
        "edge_tts",
        "cloud_materials",
        "subtitle_material_matching",
        "carnac_overlay",
    }
)
FIRST_USE_CAPABILITY_PATHS = {
    "favorite_text_assets": ("favorites",),
    "subject_pointer_profiles": ("subject_pointer",),
    "feishu_notifications": ("feishu",),
    "volc_asr_alignment": ("volc_asr",),
    "sami_tts": ("tts", "sami"),
    "edge_tts": ("tts", "edge"),
    "cloud_materials": ("cloud_materials",),
    "subtitle_material_matching": ("subtitle_material_matching",),
    "carnac_overlay": ("carnac",),
}
MAIN_DEPENDENCY_IMPORTS = {
    "uiautomation": "uiautomation",
    "playwright": "playwright",
    "pynput": "pynput",
    "edge-tts": "edge_tts",
    "pymediainfo": "pymediainfo",
    "opencv-python": "cv2",
    "av": "av",
    "numpy": "numpy",
    "imageio": "imageio",
    "psutil": "psutil",
    "requests": "requests",
    "websockets": "websockets",
}
_SECRET_KEY_PARTS = (
    "access_token",
    "app_secret",
    "authorization_url",
    "chat_id",
    "credential",
    "destination_id",
    "device_code",
    "password",
    "private_key",
    "provider_stderr",
    "provider_stdout",
    "refresh_token",
    "secret",
    "user_code",
    "verification_uri",
    "verification_url",
)
_INLINE_SECRET_PATTERN = re.compile(
    r"(?ix)(?<![A-Za-z0-9_])[\"']?"
    r"(?:access[_-]?token|refresh[_-]?token|api[_-]?key|app[_-]?secret|"
    r"client[_-]?secret|authorization(?:[_-]?url)?|credential|destination[_-]?id|"
    r"chat[_-]?id|device[_-]?code|user[_-]?code|verification[_-]?(?:uri|url)|"
    r"password|private[_-]?key)"
    r"[\"']?\s*[:=]\s*(?:Bearer\s+)?"
    r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s,;]+)'
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URL_PATTERN = re.compile(r"(?i)\b(?:https?|wss?|file)://[^\s<>\"']+")
_UNC_PATH_PATTERN = re.compile(r"(?<!\\)\\{2,4}[^\\/\s]+(?:(?:\\{1,2}|/)[^\s<>\"'|]*)+")
_FEISHU_ID_PATTERN = re.compile(r"\b(?:oc|ou|on|ocg)_[A-Za-z0-9_-]{16,}\b")
_DEVICE_CODE_PATTERN = re.compile(
    r"(?i)\b(?:device|verification|user)\s+code\s*(?:is|:|=)?\s*"
    r"[A-Z0-9]{4}(?:-[A-Z0-9]{4}){1,2}\b"
)


@dataclass(frozen=True)
class InstallOptions:
    install_dependencies: bool = True
    install_playwright: bool = True
    install_audio: bool = True
    run_jianying_check: bool = True
    onboarding_only: bool = False
    offline_bundle: str | Path | None = None


@dataclass(frozen=True)
class OfflineBundleLayout:
    source_root: Path
    requirements_root: Path
    main_wheelhouse: Path
    audio_wheelhouse: Path
    browser_root: Path
    ffmpeg_bin: Path
    ffprobe_bin: Path
    manifest_sha256: str
    source_commit: str
    archive_sha256: str | None
    manifest: Mapping[str, object]


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
SETUP_COMMAND_TIMEOUT_SECONDS = 7200
SPECTRAMINI_SMOKE_ALGORITHM = "auto_cut_spectramini_style_smoke_v1"
SPECTRAMINI_SMOKE_REQUIRED_CHECKS = (
    "int16_output",
    "shape_preserved",
    "finite_output",
    "breath_rms_reduced",
    "click_peak_reduced",
    "memory_roundtrip_ok",
    "feature_finite",
    "deterministic",
)


def python_supported(version: tuple[int, int, int]) -> bool:
    return version[0] == 3 and 10 <= version[1] <= 12


def offline_python_supported(version: tuple[int, int, int], implementation: str, bits: int) -> bool:
    return version[:2] == (3, 11) and implementation.casefold() == "cpython" and bits == 64


def platform_supported(
    platform_name: str,
    machine: str,
    *,
    windows_version: tuple[int, int, int] = (10, 0, 0),
    windows_product_type: int = 1,
) -> bool:
    return (
        platform_name.lower() == "windows"
        and machine.lower() in {"amd64", "x86_64"}
        and windows_version[0] == 10
        and windows_product_type == 1
    )


def _platform_evidence(
    platform_name: str,
    machine: str,
    *,
    windows_version: tuple[int, int, int],
    windows_product_type: int,
) -> dict[str, object]:
    if platform_name.lower() != "windows":
        reason = "windows_required"
    elif machine.lower() not in {"amd64", "x86_64"}:
        reason = "windows_x64_required"
    elif windows_product_type != 1:
        reason = "windows_server_unsupported"
    elif windows_version[0] != 10:
        reason = "windows_version_unsupported"
    else:
        reason = "supported"
    return {
        "supported": reason == "supported",
        "reason": reason,
        "system": platform_name,
        "machine": machine,
        "windows_version": list(windows_version),
        "windows_product_type": windows_product_type,
    }


def _current_windows_evidence() -> tuple[tuple[int, int, int], int]:
    getwindowsversion = getattr(sys, "getwindowsversion", None)
    if getwindowsversion is None:
        return (0, 0, 0), 0
    version = getwindowsversion()
    return (
        (int(version.major), int(version.minor), int(version.build)),
        int(version.product_type),
    )


def stage_result(
    stage_id: str,
    *,
    status: str,
    code: str,
    mandatory: bool,
    summary: str,
    details: dict[str, Any] | None = None,
) -> dict[str, object]:
    if status not in STAGE_STATUSES:
        raise ValueError(f"unsupported stage status: {status}")
    if not stage_id or not code:
        raise ValueError("stage id and code must be nonempty")
    return {
        "id": stage_id,
        "status": status,
        "code": code,
        "mandatory": bool(mandatory),
        "summary": summary,
        "details": details or {},
    }


def install_status(stages: Iterable[dict[str, object]]) -> str:
    stage_list = list(stages)
    if any(stage.get("mandatory") and stage.get("status") == "failed" for stage in stage_list):
        return "failed"
    if any(stage.get("status") != "ready" for stage in stage_list):
        return "degraded"
    return "ready"


def _sanitize_string(value: str, repo_root: Path) -> str:
    resolved_root = str(_lexical_absolute(repo_root))
    sanitized = re.sub(
        re.escape(resolved_root),
        "<package-root>",
        value,
        flags=re.IGNORECASE,
    )
    sanitized = _URL_PATTERN.sub("<redacted-url>", sanitized)
    sanitized = _UNC_PATH_PATTERN.sub("<local-unc-path>", sanitized)
    sanitized = re.sub(
        r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/][^\r\n\"']+",
        "<local-path>",
        sanitized,
    )
    sanitized = _BEARER_PATTERN.sub("<redacted>", sanitized)
    sanitized = _INLINE_SECRET_PATTERN.sub("<redacted>", sanitized)
    sanitized = _DEVICE_CODE_PATTERN.sub("<redacted>", sanitized)
    sanitized = _FEISHU_ID_PATTERN.sub("<redacted-id>", sanitized)
    if len(sanitized) > MAX_REPORT_STRING_LENGTH:
        sanitized = sanitized[: MAX_REPORT_STRING_LENGTH - 15] + "...<truncated>"
    return sanitized


def sanitize_payload(value: Any, *, repo_root: Path) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            safe_key = _sanitize_string(key_text, repo_root)
            normalized = key_text.lower()
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                sanitized[safe_key] = "<redacted>"
            else:
                sanitized[safe_key] = sanitize_payload(child, repo_root=repo_root)
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload(child, repo_root=repo_root) for child in value]
    if isinstance(value, tuple):
        return [sanitize_payload(child, repo_root=repo_root) for child in value]
    if isinstance(value, Path):
        return _sanitize_string(str(value), repo_root)
    if isinstance(value, str):
        return _sanitize_string(value, repo_root)
    return value


_SUBPROCESS_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATHEXT",
        "PATH",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
)


def _minimal_subprocess_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SUBPROCESS_ENV_ALLOWLIST
    }
    return environment


def _runner_environment(*, offline: bool) -> dict[str, str]:
    environment = _minimal_subprocess_environment()
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_CONFIG_FILE"] = os.devnull
    if offline:
        environment["PIP_NO_INDEX"] = "1"
        environment["HTTP_PROXY"] = "http://127.0.0.1:9"
        environment["HTTPS_PROXY"] = "http://127.0.0.1:9"
        environment["ALL_PROXY"] = "http://127.0.0.1:9"
        environment["NO_PROXY"] = ""
        environment["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    return environment


def _subprocess_runner(
    command: Sequence[str], *, cwd: Path, offline: bool
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        env=_runner_environment(offline=offline),
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SETUP_COMMAND_TIMEOUT_SECONDS,
    )


def _default_runner(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _subprocess_runner(command, cwd=cwd, offline=False)


def _offline_default_runner(
    command: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return _subprocess_runner(command, cwd=cwd, offline=True)


def _lexical_absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _unsafe_repo_write_path(repo_root: Path, target: Path) -> dict[str, object] | None:
    root = _lexical_absolute(repo_root)
    candidate = _lexical_absolute(target)
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return {"reason": "target_outside_repo", "path": str(candidate)}
    if not relative.parts:
        return {"reason": "target_outside_repo", "path": str(candidate)}

    current = root
    while True:
        if os.path.lexists(current) and _is_reparse_point(current):
            return {"reason": "reparse_component", "path": str(current)}
        parent = current.parent
        if parent == current:
            break
        current = parent

    current = root
    for part in relative.parts:
        current /= part
        if os.path.lexists(current) and _is_reparse_point(current):
            return {"reason": "reparse_component", "path": str(current)}
    return None


def _run_command(
    command: Sequence[str], *, cwd: Path, runner: CommandRunner
) -> subprocess.CompletedProcess[str]:
    normalized_command = [str(part) for part in command]
    try:
        return runner(normalized_command, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if exc.stdout is not None else exc.output
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        timeout_message = f"Command timed out after {exc.timeout} seconds."
        completed = subprocess.CompletedProcess(
            normalized_command,
            124,
            stdout=stdout or "",
            stderr="\n".join(part for part in (stderr, timeout_message) if part),
        )
        completed.timeout_seconds = exc.timeout
        return completed
    except OSError as exc:
        return subprocess.CompletedProcess(
            normalized_command,
            127,
            stdout="",
            stderr=str(exc),
        )


def _command_details(
    completed: subprocess.CompletedProcess[str], *, repo_root: Path
) -> dict[str, object]:
    details: dict[str, object] = {
        "command": [str(part) for part in completed.args],
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }
    timeout_seconds = getattr(completed, "timeout_seconds", None)
    if completed.returncode == 124 and isinstance(timeout_seconds, (int, float)):
        details.update(timed_out=True, timeout_seconds=timeout_seconds)
    return sanitize_payload(details, repo_root=repo_root)


def _parse_json_output(completed: subprocess.CompletedProcess[str]) -> dict[str, Any] | None:
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _probe_python_runtime(
    python_executable: Path,
    *,
    repo_root: Path,
    runner: CommandRunner,
) -> dict[str, object]:
    probe_script = (
        "import json, platform, sys; "
        "print(json.dumps({'ok': True, 'version': list(sys.version_info[:3]), "
        "'implementation': platform.python_implementation(), "
        "'bits': platform.architecture()[0], 'executable': sys.executable, "
        "'prefix': sys.prefix, 'base_prefix': sys.base_prefix}))"
    )
    completed = _run_command(
        [str(python_executable), "-I", "-c", probe_script],
        cwd=repo_root,
        runner=runner,
    )
    payload = _parse_json_output(completed)
    raw_version = payload.get("version") if payload else None
    version: tuple[int, int, int] | None = None
    if (
        isinstance(raw_version, list)
        and len(raw_version) >= 3
        and all(type(part) is int for part in raw_version[:3])
    ):
        version = tuple(raw_version[:3])
    elif isinstance(raw_version, str):
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[^\d].*)?", raw_version)
        if match:
            version = tuple(int(part) for part in match.groups())
    bits = str(payload.get("bits") or "") if payload else ""
    implementation = str(payload.get("implementation") or "") if payload else ""
    venv_root = python_executable.parent.parent
    expected_venv_root = repo_root / ".venv"
    config_path = venv_root / "pyvenv.cfg"

    def same_path(left: str | Path, right: str | Path) -> bool:
        try:
            left_path = Path(left).resolve(strict=False)
            right_path = Path(right).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return False
        return os.path.normcase(str(left_path)) == os.path.normcase(str(right_path))

    def has_reparse_component(path: Path) -> bool:
        current = path
        while True:
            try:
                metadata = current.lstat()
            except OSError:
                return True
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if current.is_symlink() or attributes & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ):
                return True
            if same_path(current, venv_root):
                return False
            parent = current.parent
            if parent == current:
                return True
            current = parent

    config_values: dict[str, str] = {}
    try:
        if config_path.is_file() and not has_reparse_component(config_path):
            for line in config_path.read_text(encoding="utf-8-sig").splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    config_values[key.strip().casefold()] = value.strip().casefold()
    except (OSError, UnicodeError):
        config_values = {}
    reported_executable = str(payload.get("executable") or "") if payload else ""
    reported_prefix = str(payload.get("prefix") or "") if payload else ""
    reported_base_prefix = str(payload.get("base_prefix") or "") if payload else ""
    venv_bound = (
        same_path(venv_root, expected_venv_root)
        and same_path(reported_executable, python_executable)
        and same_path(reported_prefix, venv_root)
        and bool(reported_base_prefix)
        and not same_path(reported_base_prefix, reported_prefix)
        and config_values.get("include-system-site-packages") == "false"
        and not has_reparse_component(python_executable)
    )
    compatible = (
        completed.returncode == 0
        and payload is not None
        and payload.get("ok") is True
        and version is not None
        and python_supported(version)
        and bits == "64bit"
        and venv_bound
    )
    return {
        "compatible": compatible,
        "version": ".".join(str(part) for part in version) if version else "",
        "bits": bits,
        "implementation": implementation,
        "venv_bound": venv_bound,
        "command": _command_details(completed, repo_root=repo_root),
    }


def _write_report(report: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_and_validate_capability_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("capability manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("capability manifest must be an object")
    top_level_fields = {"$schema", "schema_version", "release_version", "capabilities"}
    missing_top_level = sorted(top_level_fields - set(payload))
    if missing_top_level:
        raise ValueError(f"capability manifest missing {missing_top_level[0]}")
    unexpected_top_level = sorted(set(payload) - top_level_fields)
    if unexpected_top_level:
        raise ValueError(f"capability manifest has unexpected field {unexpected_top_level[0]}")
    if payload["$schema"] != "schemas/capability-manifest.schema.json":
        raise ValueError("capability manifest $schema is invalid")
    if payload.get("schema_version") != 1 or isinstance(payload.get("schema_version"), bool):
        raise ValueError("capability manifest schema_version must be 1")
    if payload.get("release_version") != INSTALLER_VERSION:
        raise ValueError(f"capability manifest release_version must be {INSTALLER_VERSION}")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("capability manifest capabilities must be a nonempty list")

    required_fields = {
        "id",
        "label",
        "description",
        "verification_command",
        "actual_result",
        *CAPABILITY_CLASSIFICATION_FIELDS,
    }
    ids: list[str] = []
    for index, capability in enumerate(capabilities):
        if not isinstance(capability, dict):
            raise ValueError(f"capability {index} must be an object")
        missing = sorted(required_fields - set(capability))
        if missing:
            raise ValueError(f"capability {index} missing {missing[0]}")
        unexpected = sorted(set(capability) - required_fields)
        if unexpected:
            raise ValueError(f"capability {index} has unexpected field {unexpected[0]}")
        capability_id = capability["id"]
        if not isinstance(capability_id, str) or not re.fullmatch(
            r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", capability_id
        ):
            raise ValueError(f"capability {index} id is invalid")
        ids.append(capability_id)
        for field in CAPABILITY_CLASSIFICATION_FIELDS:
            if type(capability[field]) is not bool:
                raise ValueError(f"capability {capability_id} {field} must be boolean")
        for field in ("label", "description", "verification_command"):
            if not isinstance(capability[field], str) or not capability[field].strip():
                raise ValueError(f"capability {capability_id} {field} must be nonempty")
        result = capability["actual_result"]
        if not isinstance(result, dict) or set(result) != {"status", "code", "summary"}:
            raise ValueError(
                f"capability {capability_id} actual_result requires status, code, and summary"
            )
        if not isinstance(result["status"], str) or result["status"] not in CAPABILITY_STATUSES:
            raise ValueError(f"capability {capability_id} status is invalid")
        if not isinstance(result["code"], str) or result["code"] not in CAPABILITY_RESULT_CODES:
            raise ValueError(f"capability {capability_id} code is invalid")
        if not isinstance(result["summary"], str) or not result["summary"].strip():
            raise ValueError(f"capability {capability_id} summary must be nonempty")
        if capability["unavailable"] and result["status"] != "unavailable":
            raise ValueError(f"capability {capability_id} unavailable status is inconsistent")

    if len(ids) != len(set(ids)):
        raise ValueError("duplicate capability id")
    missing_ids = sorted(REQUIRED_CAPABILITY_IDS - set(ids))
    if missing_ids:
        raise ValueError(f"missing required capability id: {missing_ids[0]}")
    return payload


def probe_main_dependencies_in_process() -> dict[str, object]:
    modules: dict[str, dict[str, object]] = {}
    for distribution, module_name in MAIN_DEPENDENCY_IMPORTS.items():
        try:
            imported = importlib.import_module(module_name)
        except Exception as exc:
            modules[module_name] = {
                "ok": False,
                "distribution": distribution,
                "error_type": type(exc).__name__,
            }
        else:
            modules[module_name] = {
                "ok": True,
                "distribution": distribution,
                "version": str(getattr(imported, "__version__", "")),
            }
    ready = len(modules) == len(MAIN_DEPENDENCY_IMPORTS) and all(
        component["ok"] for component in modules.values()
    )
    return {
        "status": "ready" if ready else "failed",
        "code": ("main_dependencies_importable" if ready else "main_dependency_import_failed"),
        "modules": modules,
    }


def verify_main_dependency_imports(
    repo_root: Path,
    python_executable: Path,
    *,
    runner: CommandRunner | None = None,
) -> dict[str, object]:
    root = Path(repo_root).resolve()
    module_names = list(MAIN_DEPENDENCY_IMPORTS.values())
    probe_script = "\n".join(
        (
            "import importlib",
            "import json",
            f"modules = {module_names!r}",
            "results = {}",
            "for name in modules:",
            "    try:",
            "        module = importlib.import_module(name)",
            "    except Exception as error:",
            '        results[name] = {"ok": False, "error_type": type(error).__name__}',
            "    else:",
            '        results[name] = {"ok": True, "version": str(getattr(module, "__version__", ""))}',
            'ready = len(results) == len(modules) and all(row["ok"] for row in results.values())',
            'print(json.dumps({"status": "ready" if ready else "failed",',
            '                  "code": "main_dependencies_importable" if ready else "main_dependency_import_failed",',
            '                  "modules": results}, sort_keys=True))',
            "raise SystemExit(0 if ready else 1)",
        )
    )
    completed = _run_command(
        [str(python_executable), "-c", probe_script],
        cwd=root,
        runner=runner or _default_runner,
    )
    payload = _parse_json_output(completed)
    modules = payload.get("modules") if payload else None
    expected_modules = set(module_names)
    ready = (
        completed.returncode == 0
        and isinstance(modules, dict)
        and set(modules) == expected_modules
        and all(isinstance(row, dict) and row.get("ok") is True for row in modules.values())
    )
    return {
        "status": "ready" if ready else "failed",
        "code": ("main_dependencies_importable" if ready else "main_dependency_import_failed"),
        "summary": (
            "All 12 declared main dependency modules imported successfully."
            if ready
            else "One or more declared main dependency modules could not be imported."
        ),
        "probe": sanitize_payload(payload or {}, repo_root=root),
        "command": _command_details(completed, repo_root=root),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_receipt(root: Path) -> dict[str, dict[str, int | str]]:
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): {
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(root).as_posix(),
        )
    }


def _skill_directories(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {
        child.name: child
        for child in sorted(root.iterdir(), key=lambda path: path.name)
        if child.is_dir() and (child / "SKILL.md").is_file()
    }


def verify_installed_skills(source_root: Path, installed_root: Path) -> dict[str, object]:
    source_skills = _skill_directories(Path(source_root))
    installed_skills = _skill_directories(Path(installed_root))
    mismatches = sorted(
        name
        for name in set(source_skills) | set(installed_skills)
        if name not in source_skills
        or name not in installed_skills
        or tree_receipt(source_skills[name]) != tree_receipt(installed_skills[name])
    )
    source_count = len(source_skills)
    installed_count = len(installed_skills)
    if source_count != EXPECTED_SKILL_COUNT or installed_count != EXPECTED_SKILL_COUNT:
        status = "failed"
        code = "skill_count_mismatch"
    elif mismatches:
        status = "failed"
        code = "skill_tree_mismatch"
    else:
        status = "ready"
        code = "skills_verified"
    return {
        "status": status,
        "code": code,
        "expected_skill_count": EXPECTED_SKILL_COUNT,
        "skill_count": source_count,
        "installed_skill_count": installed_count,
        "skill_names": sorted(source_skills),
        "mismatches": mismatches,
    }


def _required_offline_component(
    manifest: Mapping[str, object], component_id: str
) -> Mapping[str, object]:
    components = manifest.get("components")
    component = components.get(component_id) if isinstance(components, Mapping) else None
    if not isinstance(component, Mapping) or component.get("included") is not True:
        raise ValueError(f"required offline component is missing: {component_id}")
    return component


def _offline_manifest_rows(manifest: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise ValueError("offline bundle file inventory is invalid")
    result: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("offline bundle file inventory is invalid")
        path = row.get("path")
        if not isinstance(path, str) or not path or path in result:
            raise ValueError("offline bundle file inventory is invalid")
        result[path] = row
    return result


def _offline_manifest_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("offline bundle manifest path is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or value.startswith("/"):
        raise ValueError("offline bundle manifest path is invalid")
    return Path(*parts)


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _validate_offline_browser_manifest_files(
    source_root: Path,
    rows: Mapping[str, Mapping[str, object]],
    browser_source: Mapping[str, object],
) -> None:
    """Bind standalone browser license/source files to the committed source.

    The complete browser tree receipts authenticate executable/runtime bytes,
    but the legal/source files live outside those trees.  Formal 1.7.0
    declarations opt into this exact file-and-metadata closure with
    ``native_binding_schema=1``.  Formal offline installation fails closed if
    the marker is absent or has any other type or value.
    """

    native_binding_schema = browser_source.get("native_binding_schema")
    if type(native_binding_schema) is not int or native_binding_schema != 1:
        raise ValueError("offline browser native binding schema is invalid")
    expected_specs = (
        (
            "licenses/chromium/LICENSE",
            browser_source.get("license_size"),
            browser_source.get("license_sha256"),
            "playwright_chromium_license",
            browser_source.get("browser_version"),
            "any",
            browser_source.get("license"),
            browser_source.get("license_url"),
        ),
        (
            "licenses/playwright/LICENSE",
            browser_source.get("playwright_license_size"),
            browser_source.get("playwright_license_sha256"),
            "playwright_license",
            OFFLINE_PLAYWRIGHT_VERSION,
            "any",
            "Apache-2.0",
            browser_source.get("playwright_license_source"),
        ),
        (
            "licenses/playwright-winldd/PrintDeps.cpp",
            browser_source.get("winldd_source_code_size"),
            browser_source.get("winldd_source_code_sha256"),
            "playwright_winldd_source",
            OFFLINE_PLAYWRIGHT_WINLDD_REVISION,
            "source",
            browser_source.get("winldd_license"),
            browser_source.get("winldd_source_code_url"),
        ),
    )
    recording_license_name = browser_source.get("ffmpeg_license_filename")
    if (
        not isinstance(recording_license_name, str)
        or PurePosixPath(recording_license_name).name != recording_license_name
    ):
        raise ValueError("offline recording FFmpeg license filename is invalid")
    expected_specs += (
        (
            PurePosixPath(
                "browsers",
                f"ffmpeg-{OFFLINE_PLAYWRIGHT_FFMPEG_REVISION}",
                recording_license_name,
            ).as_posix(),
            browser_source.get("ffmpeg_license_size"),
            browser_source.get("ffmpeg_license_sha256"),
            "playwright_recording_ffmpeg",
            OFFLINE_PLAYWRIGHT_FFMPEG_REVISION,
            "win_amd64",
            browser_source.get("ffmpeg_license"),
            browser_source.get("ffmpeg_license_source"),
        ),
    )
    for relative, expected_size, expected_sha256, component, version, platform_name, license_name, source in expected_specs:
        if (
            not isinstance(relative, str)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size <= 0
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or not isinstance(version, str)
            or not version
            or not isinstance(platform_name, str)
            or not platform_name
            or not isinstance(license_name, str)
            or not license_name
            or not isinstance(source, str)
            or not source
        ):
            raise ValueError("offline browser source declaration is incomplete")
        row = rows.get(relative)
        if not isinstance(row, Mapping) or (
            row.get("size") != expected_size
            or row.get("sha256") != expected_sha256
            or row.get("component") != component
            or row.get("version") != version
            or row.get("platform") != platform_name
            or row.get("license") != license_name
            or row.get("source") != source
        ):
            raise ValueError(f"offline browser source metadata is invalid: {relative}")
        path = source_root / _offline_manifest_relative_path(relative)
        if (
            _is_reparse_point(path)
            or not path.is_file()
            or path.stat().st_size != expected_size
            or _sha256_file(path) != expected_sha256
        ):
            raise ValueError(f"offline browser source bytes are invalid: {relative}")


def _validate_fixed_ffmpeg_file_row(
    source_root: Path,
    rows: Mapping[str, Mapping[str, object]],
    relative: str,
    expected: tuple[int, str, str, str, str, str],
) -> Path:
    expected_size, expected_sha256, component, platform, license_name, source = expected
    evidence_label = "build receipt" if relative.endswith("build-receipt.json") else "build evidence"
    row = rows.get(relative)
    if not isinstance(row, Mapping) or (
        row.get("size") != expected_size
        or row.get("sha256") != expected_sha256
        or row.get("component") != component
        or row.get("version") != OFFLINE_FFMPEG_VERSION
        or row.get("platform") != platform
        or row.get("license") != license_name
        or row.get("source") != source
    ):
        raise ValueError(f"offline FFmpeg {evidence_label} metadata is invalid: {relative}")
    path = source_root / _offline_manifest_relative_path(relative)
    if (
        not path.is_file()
        or path.stat().st_size != expected_size
        or offline_sha256_file(path) != expected_sha256
    ):
        raise ValueError(f"offline FFmpeg {evidence_label} bytes are invalid: {relative}")
    return path


def _offline_browser_tree_receipt(root: Path, label: str) -> dict[str, object]:
    """Recompute the fixed browser tree identity from extracted bytes."""
    if _is_reparse_point(root) or not root.is_dir():
        raise ValueError(f"offline {label} tree is missing or unsafe")
    rows: list[dict[str, object]] = []
    for candidate in sorted(
        root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()
    ):
        if _is_reparse_point(candidate):
            raise ValueError(f"offline {label} tree contains an unsafe reparse point")
        if not candidate.is_file():
            continue
        data = candidate.read_bytes()
        rows.append(
            {
                "path": candidate.relative_to(root).as_posix(),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    canonical = _canonical_json_bytes(rows)
    return {
        "file_count": len(rows),
        "total_size": sum(int(row["size"]) for row in rows),
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _validate_offline_browser_source_binding(
    source_root: Path,
    manifest: Mapping[str, object],
    program_sources: Mapping[str, object],
) -> None:
    """Reject self-rehashed browser/native bytes in a formal offline bundle.

    ``offline-deps-manifest.json`` is intentionally treated as an inventory,
    not as the trust anchor.  The program's committed source declaration pins
    the upstream archives, complete extracted trees, and executable bytes;
    every runtime helper is checked against those values before it is copied
    into ``tmp/offline-runtime`` or used by a probe.
    """
    browser_source = program_sources.get("playwright_chromium")
    if not isinstance(browser_source, Mapping) or "release_source_commit" in program_sources:
        raise ValueError("program Playwright source declaration is invalid")
    expected_identity = {
        "playwright_version": OFFLINE_PLAYWRIGHT_VERSION,
        "revision": OFFLINE_CHROMIUM_REVISION,
        "browser_version": OFFLINE_CHROMIUM_VERSION,
        "ffmpeg_revision": OFFLINE_PLAYWRIGHT_FFMPEG_REVISION,
        "winldd_revision": OFFLINE_PLAYWRIGHT_WINLDD_REVISION,
    }
    if (
        browser_source.get("browser_version") != expected_identity["browser_version"]
        or any(
            browser_source.get(key) != value
            for key, value in expected_identity.items()
            if key != "browser_version"
        )
    ):
        raise ValueError("program Playwright source identity is invalid")
    rows = _offline_manifest_rows(manifest)
    components = manifest.get("components")
    browser_component = (
        components.get("playwright_chromium") if isinstance(components, Mapping) else None
    )
    if not isinstance(browser_component, Mapping) or browser_component.get("included") is not True:
        raise ValueError("offline Chromium component receipt is missing")
    receipts = browser_component.get("tree_receipts")
    if not isinstance(receipts, Mapping):
        raise ValueError("offline Chromium tree receipts are missing")
    _validate_offline_browser_manifest_files(source_root, rows, browser_source)

    specs = (
        (
            "chromium",
            f"browsers/chromium-{OFFLINE_CHROMIUM_REVISION}",
            "chromium",
            "chrome-win/chrome.exe",
        ),
        (
            "headless_shell",
            f"browsers/chromium_headless_shell-{OFFLINE_CHROMIUM_REVISION}",
            "headless_shell",
            "chrome-win/headless_shell.exe",
        ),
        (
            "recording_ffmpeg",
            f"browsers/ffmpeg-{OFFLINE_PLAYWRIGHT_FFMPEG_REVISION}",
            "ffmpeg",
            "ffmpeg-win64.exe",
        ),
        (
            "winldd",
            f"browsers/winldd-{OFFLINE_PLAYWRIGHT_WINLDD_REVISION}",
            "winldd",
            "PrintDeps.exe",
        ),
    )
    measured_executables: dict[str, tuple[int, str]] = {}
    for label, root_relative, source_prefix, executable_relative in specs:
        archive_size = browser_source.get(f"{source_prefix}_archive_size")
        archive_sha256 = browser_source.get(f"{source_prefix}_archive_sha256")
        archive_source = browser_source.get(
            "ffmpeg_source" if source_prefix == "ffmpeg" else f"{source_prefix}_source"
        )
        if (
            not isinstance(archive_size, int)
            or isinstance(archive_size, bool)
            or archive_size <= 0
            or not isinstance(archive_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", archive_sha256) is None
            or archive_sha256 == "0" * 64
            or not isinstance(archive_source, str)
            or not archive_source.strip()
        ):
            raise ValueError(f"offline {label} archive identity is invalid")
        expected_tree = {
            "file_count": browser_source.get(f"{source_prefix}_tree_file_count"),
            "total_size": browser_source.get(f"{source_prefix}_tree_total_size"),
            "tree_sha256": browser_source.get(f"{source_prefix}_tree_sha256"),
        }
        if (
            not isinstance(expected_tree["file_count"], int)
            or isinstance(expected_tree["file_count"], bool)
            or expected_tree["file_count"] <= 0
            or not isinstance(expected_tree["total_size"], int)
            or isinstance(expected_tree["total_size"], bool)
            or expected_tree["total_size"] <= 0
            or not isinstance(expected_tree["tree_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_tree["tree_sha256"]) is None
            or expected_tree["tree_sha256"] == "0" * 64
        ):
            raise ValueError(f"offline {label} tree identity is invalid")
        root = source_root / Path(*root_relative.split("/"))
        actual_tree = _offline_browser_tree_receipt(root, label)
        if actual_tree != expected_tree or receipts.get(label) != expected_tree:
            raise ValueError(f"offline {label} tree bytes do not match committed identity")

        executable_relative_path = PurePosixPath(
            root_relative, executable_relative
        ).as_posix()
        expected_size = browser_source.get(f"{source_prefix}_executable_size")
        expected_sha256 = browser_source.get(f"{source_prefix}_executable_sha256")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size <= 0
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or expected_sha256 == "0" * 64
        ):
            raise ValueError(f"offline {label} executable identity is invalid")
        executable = source_root / Path(*executable_relative_path.split("/"))
        if (
            _is_reparse_point(executable)
            or not executable.is_file()
            or executable.stat().st_size != expected_size
            or _sha256_file(executable) != expected_sha256
        ):
            raise ValueError(f"offline {label} executable bytes do not match committed identity")
        row = rows.get(executable_relative_path)
        if not isinstance(row, Mapping) or (
            row.get("size") != expected_size or row.get("sha256") != expected_sha256
        ):
            raise ValueError(f"offline {label} executable metadata is not source-bound")
        measured_executables[source_prefix] = (expected_size, expected_sha256)

    for source_prefix in ("chromium", "headless_shell"):
        measured_size, measured_sha256 = measured_executables[source_prefix]
        if (
            browser_component.get(f"{source_prefix}_executable_size") != measured_size
            or browser_component.get(f"{source_prefix}_executable_sha256") != measured_sha256
        ):
            raise ValueError("offline Chromium executable receipt is not source-bound")

    if (
        browser_component.get("playwright_version") != expected_identity["playwright_version"]
        or browser_component.get("revision") != expected_identity["revision"]
        or browser_component.get("ffmpeg_revision") != expected_identity["ffmpeg_revision"]
        or browser_component.get("winldd_revision") != expected_identity["winldd_revision"]
        or browser_component.get("browser_version") != expected_identity["browser_version"]
        or browser_component.get("recording_ffmpeg_relative_path")
        != (
            f"browsers/ffmpeg-{OFFLINE_PLAYWRIGHT_FFMPEG_REVISION}/ffmpeg-win64.exe"
        )
    ):
        raise ValueError("offline Chromium component identity is not source-bound")


def _read_ffmpeg_attestation_json(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"offline FFmpeg {label} is invalid") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"offline FFmpeg {label} is invalid")
    return payload


def _validate_ffmpeg_build_receipt(payload: Mapping[str, object]) -> None:
    source = payload.get("source")
    runtime = payload.get("runtime")
    configuration = payload.get("configuration")
    reproducibility = payload.get("reproducibility")
    compiler_packages = payload.get("compiler_packages")
    if not (
        payload.get("schema_version") == 1
        and payload.get("status") == "ready"
        and payload.get("target")
        == {
            "os": "windows",
            "arch": "x64",
            "toolchain": "MSYS2 MinGW-w64",
            "compiler": "GCC 16.2.0 (Rev3, Built by MSYS2 project)",
        }
        and isinstance(source, Mapping)
        and source.get("tag") == OFFLINE_FFMPEG_RELEASE_TAG
        and source.get("version") == OFFLINE_FFMPEG_VERSION
        and source.get("archive_size") == 16872873
        and source.get("archive_sha256") == OFFLINE_FFMPEG_SOURCE_ARCHIVE_SHA256
        and source.get("immutable_identity") == OFFLINE_FFMPEG_SOURCE_IDENTITY
        and isinstance(runtime, Mapping)
        and runtime.get("archive_size") == 23249775
        and runtime.get("archive_sha256") == OFFLINE_FFMPEG_RUNTIME_ARCHIVE_SHA256
        and isinstance(configuration, Mapping)
        and configuration.get("source_date_epoch") == 1767225600
        and configuration.get("disable_network") is True
        and configuration.get("disable_autodetect") is True
        and configuration.get("disable_everything_then_allowlist") is True
        and configuration.get("shared_libraries") is False
        and configuration.get("external_codec_libraries") is False
        and configuration.get("flags_sha256")
        == OFFLINE_FFMPEG_BUILD_SOURCE_IDENTITY.removeprefix("sha256:")
        and isinstance(reproducibility, Mapping)
        and reproducibility.get("build_count") == 2
        and reproducibility.get("outputs_byte_identical") is True
        and reproducibility.get("runtime_archive_byte_identical") is True
        and reproducibility.get("runtime_archive_sha256")
        == OFFLINE_FFMPEG_RUNTIME_ARCHIVE_SHA256
        and reproducibility.get("run_roots") == ["run-1", "run-2"]
        and payload.get("external_codec_libraries") is False
        and payload.get("network_access_during_build") is False
    ):
        raise ValueError("offline FFmpeg build receipt contract is invalid")

    runtime_rows = runtime.get("files")
    if not isinstance(runtime_rows, list):
        raise ValueError("offline FFmpeg build receipt runtime inventory is invalid")
    expected_runtime = {
        "bin/ffmpeg.exe": OFFLINE_FFMPEG_RUNTIME_FILES["tools/ffmpeg/bin/ffmpeg.exe"],
        "bin/ffprobe.exe": OFFLINE_FFMPEG_RUNTIME_FILES["tools/ffmpeg/bin/ffprobe.exe"],
        "LICENSE.txt": OFFLINE_FFMPEG_RUNTIME_FILES["tools/ffmpeg/LICENSE.txt"],
    }
    measured_runtime: dict[str, tuple[object, object]] = {}
    for row in runtime_rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("path"), str):
            raise ValueError("offline FFmpeg build receipt runtime inventory is invalid")
        measured_runtime[str(row["path"])] = (row.get("size"), row.get("sha256"))
    if measured_runtime != expected_runtime:
        raise ValueError("offline FFmpeg build receipt runtime inventory is invalid")

    if not isinstance(compiler_packages, list) or not compiler_packages:
        raise ValueError("offline FFmpeg build receipt toolchain inventory is invalid")
    package_names: set[str] = set()
    gmp_sha256: str | None = None
    for row in compiler_packages:
        if not isinstance(row, Mapping):
            raise ValueError("offline FFmpeg build receipt toolchain inventory is invalid")
        name = row.get("name")
        digest = row.get("sha256")
        if (
            not isinstance(name, str)
            or not name
            or name in package_names
            or not isinstance(row.get("version"), str)
            or not isinstance(row.get("archive"), str)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("offline FFmpeg build receipt toolchain inventory is invalid")
        package_names.add(name)
        if name == "mingw-w64-x86_64-gmp":
            gmp_sha256 = digest
    if gmp_sha256 != "8924433974c4add46cb46ea4f6ef283b5c5139d3f552375115b5580f855015cc":
        raise ValueError("offline FFmpeg build receipt toolchain inventory is invalid")


def _validate_ffmpeg_pe_imports(payload: Mapping[str, object]) -> None:
    files = payload.get("files")
    if not (
        payload.get("schema_version") == 1
        and payload.get("status") == "ready"
        and payload.get("target") == "windows-x64"
        and payload.get("external_codec_libraries") is False
        and isinstance(files, Mapping)
    ):
        raise ValueError("offline FFmpeg PE import receipt is invalid")
    expected = {
        "bin/ffmpeg.exe": OFFLINE_FFMPEG_RUNTIME_FILES["tools/ffmpeg/bin/ffmpeg.exe"][1],
        "bin/ffprobe.exe": OFFLINE_FFMPEG_RUNTIME_FILES["tools/ffmpeg/bin/ffprobe.exe"][1],
    }
    allowed_imports = {"bcrypt.dll", "kernel32.dll", "msvcrt.dll", "shell32.dll"}
    if set(files) != set(expected):
        raise ValueError("offline FFmpeg PE import receipt is invalid")
    for name, digest in expected.items():
        row = files.get(name)
        imports = row.get("imports") if isinstance(row, Mapping) else None
        if not (
            isinstance(row, Mapping)
            and row.get("sha256") == digest
            and isinstance(imports, list)
            and {str(value).casefold() for value in imports} == allowed_imports
        ):
            raise ValueError("offline FFmpeg PE import receipt is invalid")


def _validate_ffmpeg_media_probe(payload: Mapping[str, object]) -> None:
    fixture = payload.get("fixture")
    transcode = payload.get("transcode")
    probe = payload.get("ffprobe")
    if not (
        payload.get("schema_version") == 1
        and payload.get("status") == "ready"
        and isinstance(fixture, Mapping)
        and fixture.get("kind") == "generated_pcm_sine"
        and fixture.get("sample_rate") == 16000
        and fixture.get("channels") == 1
        and fixture.get("duration_seconds") == 0.2
        and re.fullmatch(r"[0-9a-f]{64}", str(fixture.get("input_sha256", "")))
        and isinstance(transcode, Mapping)
        and transcode.get("return_code") == 0
        and transcode.get("filter") == "volume=0.5"
        and transcode.get("codec") == "pcm_s16le"
        and re.fullmatch(r"[0-9a-f]{64}", str(transcode.get("output_sha256", "")))
        and isinstance(probe, Mapping)
        and probe.get("return_code") == 0
        and probe.get("codec_name") == "pcm_s16le"
        and probe.get("sample_rate") == "16000"
        and probe.get("channels") == 1
        and probe.get("duration") == "0.200000"
    ):
        raise ValueError("offline FFmpeg media probe receipt is invalid")


def _validate_ffmpeg_asset_manifest(payload: Mapping[str, object]) -> None:
    unsigned = dict(payload)
    declared = unsigned.pop("manifest_sha256", None)
    source = payload.get("source")
    target = payload.get("target")
    rows = payload.get("files")
    if not (
        payload.get("schema_version") == 1
        and payload.get("status") == "ready"
        and payload.get("component") == "ffmpeg-minimal"
        and payload.get("version") == OFFLINE_FFMPEG_VERSION
        and payload.get("source_tag") == OFFLINE_FFMPEG_RELEASE_TAG
        and payload.get("external_codec_libraries") is False
        and target == {"os": "windows", "arch": "x64"}
        and isinstance(source, Mapping)
        and source.get("kind") == OFFLINE_FFMPEG_SOURCE_KIND
        and source.get("sha256") == OFFLINE_FFMPEG_SOURCE_ARCHIVE_SHA256
        and declared == OFFLINE_FFMPEG_ASSET_MANIFEST_SELF_SHA256
        and hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest() == declared
        and isinstance(rows, list)
        and rows
    ):
        raise ValueError("offline FFmpeg asset manifest is invalid")
    paths: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("offline FFmpeg asset manifest is invalid")
        path = row.get("path")
        if (
            not isinstance(path, str)
            or path in paths
            or not isinstance(row.get("size"), int)
            or isinstance(row.get("size"), bool)
            or int(row["size"]) <= 0
            or re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", ""))) is None
            or not all(isinstance(row.get(key), str) and row.get(key) for key in (
                "version",
                "platform",
                "license",
                "source",
            ))
        ):
            raise ValueError("offline FFmpeg asset manifest is invalid")
        paths.add(path)
    if not {
        "build/build-receipt.json",
        "build/build_ffmpeg.sh",
        "build/media-probe.json",
        "build/pe-imports.json",
        "runtime/ffmpeg-minimal-runtime.zip",
        "source/FFmpeg-n8.1.2.tar.gz",
    }.issubset(paths):
        raise ValueError("offline FFmpeg asset manifest is incomplete")


def _validate_fixed_offline_ffmpeg(
    source_root: Path,
    manifest: Mapping[str, object],
    component: Mapping[str, object],
    *,
    strict: bool,
) -> None:
    """Require the audited minimal FFmpeg component for the formal release.

    The offline manifest is authenticated internally, but its digest alone
    cannot establish that a native executable came from the reviewed build.
    Keep this check fail-closed and tied to the fixed 1.7.0 companion inputs.
    Older test/compatibility manifests without ``source_kind`` remain handled
    by the legacy layout checks; a current committed release must carry it.
    """
    if not strict or manifest.get("release_version") != INSTALLER_VERSION:
        return
    if component.get("source_kind") != OFFLINE_FFMPEG_SOURCE_KIND:
        raise ValueError("offline FFmpeg source identity is not committed and fixed")
    if component.get("version") != OFFLINE_FFMPEG_VERSION:
        raise ValueError("offline FFmpeg version is not the audited 8.1.2 build")
    if component.get("release_tag") != OFFLINE_FFMPEG_RELEASE_TAG:
        raise ValueError("offline FFmpeg source tag is not the audited n8.1.2 release")
    if component.get("build_source_commit") != OFFLINE_FFMPEG_BUILD_SOURCE_IDENTITY:
        raise ValueError("offline FFmpeg build source identity is invalid")
    if component.get("ffmpeg_source_commit") != OFFLINE_FFMPEG_SOURCE_IDENTITY:
        raise ValueError("offline FFmpeg upstream source identity is invalid")
    if (
        component.get("archive_sha256") != OFFLINE_FFMPEG_RUNTIME_ARCHIVE_SHA256
        or component.get("runtime_archive_sha256") != OFFLINE_FFMPEG_RUNTIME_ARCHIVE_SHA256
    ):
        raise ValueError("offline FFmpeg runtime archive identity is invalid")
    if component.get("source_archive_sha256") != OFFLINE_FFMPEG_SOURCE_ARCHIVE_SHA256:
        raise ValueError("offline FFmpeg source archive identity is invalid")
    if component.get("external_codec_libraries") is not False:
        raise ValueError("offline FFmpeg external codec closure is invalid")
    if component.get("license_closure_complete") is not True:
        raise ValueError("offline FFmpeg license closure is incomplete")
    if (
        component.get("reproducible_builds") != 2
        or component.get("reproducible_outputs_equal") is not True
    ):
        raise ValueError("offline FFmpeg reproducibility evidence is invalid")

    rows = _offline_manifest_rows(manifest)
    declared_paths = list(rows)
    if len({path.casefold() for path in declared_paths}) != len(declared_paths):
        raise ValueError("offline FFmpeg inventory contains colliding paths")
    expected_runtime_tree = {
        *OFFLINE_FFMPEG_RUNTIME_FILES,
        *OFFLINE_FFMPEG_LICENSE_FILES,
    }
    actual_runtime_tree = {
        path for path in declared_paths if path.startswith("tools/ffmpeg/")
    }
    if actual_runtime_tree != expected_runtime_tree:
        raise ValueError("offline FFmpeg runtime tree is not exact")
    evidence_paths: dict[str, Path] = {}
    for relative, expected in OFFLINE_FFMPEG_ATTESTATION_FILES.items():
        if relative == "provenance/ffmpeg/license-encoding.json":
            continue
        evidence_paths[relative] = _validate_fixed_ffmpeg_file_row(
            source_root,
            rows,
            relative,
            expected,
        )
    component_evidence = {
        "build_recipe_path": (
            "provenance/ffmpeg/build/build_ffmpeg.sh",
            "build_recipe_size",
            "build_recipe_sha256",
        ),
        "build_receipt_path": (
            "provenance/ffmpeg/build/build-receipt.json",
            "build_receipt_size",
            "build_receipt_sha256",
        ),
        "pe_imports_path": (
            "provenance/ffmpeg/build/pe-imports.json",
            None,
            "pe_imports_sha256",
        ),
        "media_probe_path": (
            "provenance/ffmpeg/build/media-probe.json",
            None,
            "media_probe_sha256",
        ),
        "asset_manifest_path": (
            "provenance/ffmpeg/manifest.json",
            "asset_manifest_size",
            "asset_manifest_sha256",
        ),
    }
    for path_key, (relative, size_key, digest_key) in component_evidence.items():
        expected_size, expected_sha256, *_metadata = OFFLINE_FFMPEG_ATTESTATION_FILES[relative]
        if (
            component.get(path_key) != relative
            or component.get(digest_key) != expected_sha256
            or size_key is not None
            and component.get(size_key) != expected_size
        ):
            raise ValueError("offline FFmpeg build evidence component identity is invalid")
    _validate_ffmpeg_build_receipt(
        _read_ffmpeg_attestation_json(
            evidence_paths["provenance/ffmpeg/build/build-receipt.json"],
            label="build receipt",
        )
    )
    _validate_ffmpeg_pe_imports(
        _read_ffmpeg_attestation_json(
            evidence_paths["provenance/ffmpeg/build/pe-imports.json"],
            label="PE import receipt",
        )
    )
    _validate_ffmpeg_media_probe(
        _read_ffmpeg_attestation_json(
            evidence_paths["provenance/ffmpeg/build/media-probe.json"],
            label="media probe receipt",
        )
    )
    _validate_ffmpeg_asset_manifest(
        _read_ffmpeg_attestation_json(
            evidence_paths["provenance/ffmpeg/manifest.json"],
            label="asset manifest",
        )
    )

    runtime_files = component.get("runtime_files")
    if not isinstance(runtime_files, Mapping) or set(runtime_files) != {
        "ffmpeg.exe",
        "ffprobe.exe",
        "LICENSE.txt",
    }:
        raise ValueError("offline FFmpeg runtime inventory is missing")
    for relative, (expected_size, expected_sha256) in OFFLINE_FFMPEG_EXECUTABLES.items():
        runtime_file = runtime_files.get(Path(relative).name)
        if not isinstance(runtime_file, Mapping) or (
            runtime_file.get("path") != f"bin/{Path(relative).name}"
            or runtime_file.get("size") != expected_size
            or runtime_file.get("sha256") != expected_sha256
        ):
            raise ValueError(f"offline FFmpeg runtime evidence is invalid: {relative}")
        row = rows.get(relative)
        if not isinstance(row, Mapping):
            raise ValueError(f"offline FFmpeg inventory is missing {relative}")
        if (
            row.get("size") != expected_size
            or row.get("sha256") != expected_sha256
            or row.get("component") != "ffmpeg"
            or row.get("version") != OFFLINE_FFMPEG_VERSION
            or row.get("platform") != "win_amd64"
            or row.get("license") != OFFLINE_FFMPEG_LICENSE
            or row.get("source") != OFFLINE_FFMPEG_RUNTIME_SOURCE
        ):
            raise ValueError(f"offline FFmpeg executable metadata is invalid: {relative}")
        candidate = source_root / Path(*relative.split("/"))
        if (
            not candidate.is_file()
            or candidate.stat().st_size != expected_size
            or offline_sha256_file(candidate) != expected_sha256
        ):
            raise ValueError(f"offline FFmpeg executable bytes are invalid: {relative}")

    license_runtime = "tools/ffmpeg/LICENSE.txt"
    runtime_license = rows.get(license_runtime)
    expected_runtime_license = OFFLINE_FFMPEG_RUNTIME_FILES[license_runtime]
    runtime_license_evidence = runtime_files.get("LICENSE.txt")
    runtime_license_path = source_root / Path(*license_runtime.split("/"))
    if (
        not isinstance(runtime_license_evidence, Mapping)
        or runtime_license_evidence.get("path") != "LICENSE.txt"
        or runtime_license_evidence.get("size") != expected_runtime_license[0]
        or runtime_license_evidence.get("sha256") != expected_runtime_license[1]
        or not isinstance(runtime_license, Mapping)
        or runtime_license.get("size") != expected_runtime_license[0]
        or runtime_license.get("sha256") != expected_runtime_license[1]
        or runtime_license.get("component") != "ffmpeg"
        or runtime_license.get("version") != OFFLINE_FFMPEG_VERSION
        or runtime_license.get("platform") != "win_amd64"
        or runtime_license.get("license") != OFFLINE_FFMPEG_LICENSE
        or runtime_license.get("source") != OFFLINE_FFMPEG_RUNTIME_SOURCE
        or not runtime_license_path.is_file()
        or runtime_license_path.stat().st_size != expected_runtime_license[0]
        or offline_sha256_file(runtime_license_path) != expected_runtime_license[1]
    ):
        raise ValueError("offline FFmpeg runtime license identity is invalid")

    source_relative = "provenance/ffmpeg/source/FFmpeg-n8.1.2.tar.gz"
    source_row = rows.get(source_relative)
    if not isinstance(source_row, Mapping) or (
        source_row.get("size") != 16872873
        or source_row.get("sha256") != OFFLINE_FFMPEG_SOURCE_ARCHIVE_SHA256
        or source_row.get("component") != "ffmpeg_source_archive"
        or source_row.get("version") != OFFLINE_FFMPEG_RELEASE_TAG
        or source_row.get("platform") != "source"
        or source_row.get("license") != OFFLINE_FFMPEG_LICENSE
        or source_row.get("source")
        != "repository:scripts/release/ffmpeg_assets/source/FFmpeg-n8.1.2.tar.gz"
    ):
        raise ValueError("offline FFmpeg source archive evidence is invalid")
    if (
        component.get("source_archive_path") != source_relative
        or component.get("source_archive_size") != 16872873
        or component.get("source_archive_sha256") != OFFLINE_FFMPEG_SOURCE_ARCHIVE_SHA256
    ):
        raise ValueError("offline FFmpeg source archive component identity is invalid")

    license_rows = [
        (path, row) for path, row in rows.items() if path.startswith("tools/ffmpeg/licenses/")
    ]
    if set(path for path, _row in license_rows) != set(OFFLINE_FFMPEG_LICENSE_FILES):
        raise ValueError("offline FFmpeg license closure is incomplete")
    for path, row in license_rows:
        expected_size, expected_sha256 = OFFLINE_FFMPEG_LICENSE_FILES[path]
        expected_license, expected_source = OFFLINE_FFMPEG_LICENSE_METADATA[path]
        if (
            row.get("size") != expected_size
            or row.get("sha256") != expected_sha256
            or row.get("component") != "ffmpeg_license"
            or row.get("version") != OFFLINE_FFMPEG_VERSION
            or row.get("platform") != "any"
            or row.get("license") != expected_license
            or row.get("source") != expected_source
        ):
            raise ValueError(f"offline FFmpeg license metadata is invalid: {path}")
        candidate = source_root / Path(*path.split("/"))
        if (
            not candidate.is_file()
            or candidate.stat().st_size != expected_size
            or offline_sha256_file(candidate) != expected_sha256
        ):
            raise ValueError(f"offline FFmpeg license bytes are invalid: {path}")

    expected_license_sources = _expected_ffmpeg_license_source_rows()
    component_license_sources = component.get("license_sources")
    if not isinstance(component_license_sources, list):
        raise ValueError("offline FFmpeg license source attestation is missing")
    actual_license_sources: dict[str, Mapping[str, object]] = {}
    for row in component_license_sources:
        if not isinstance(row, Mapping) or not isinstance(row.get("filename"), str):
            raise ValueError("offline FFmpeg license source attestation is invalid")
        filename = str(row["filename"])
        if filename.casefold() in {key.casefold() for key in actual_license_sources}:
            raise ValueError("offline FFmpeg license source attestation is duplicated")
        actual_license_sources[filename] = row
    if set(actual_license_sources) != set(expected_license_sources) or any(
        dict(actual_license_sources[name]) != expected_license_sources[name]
        for name in expected_license_sources
    ):
        raise ValueError("offline FFmpeg license source attestation is not exact")

    # If an upstream legal text is carried in an encoded envelope, require the
    # accompanying receipt to prove the decoded byte identity.  This avoids
    # treating an arbitrary base64 blob as a license merely because it hashes.
    index_relative = component.get("license_index_path")
    if not isinstance(index_relative, str) or index_relative not in rows:
        raise ValueError("offline FFmpeg license encoding receipt is missing")
    index_row = rows[index_relative]
    if (
        index_row.get("component") != "ffmpeg_license_index"
        or index_row.get("version") != OFFLINE_FFMPEG_VERSION
        or index_row.get("platform") != "any"
        or not isinstance(index_row.get("source"), str)
        or not str(index_row.get("source")).startswith("repository:")
    ):
        raise ValueError("offline FFmpeg license encoding receipt metadata is invalid")
    index_path = source_root / _offline_manifest_relative_path(index_relative)
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("offline FFmpeg license encoding receipt is invalid") from exc
    encoded_rows = index.get("rows") if isinstance(index, Mapping) else None
    if not isinstance(encoded_rows, list):
        raise ValueError("offline FFmpeg license encoding receipt is invalid")
    if encoded_rows != [OFFLINE_FFMPEG_ENCODED_LICENSE_RECEIPT]:
        raise ValueError("offline FFmpeg encoded license coverage is invalid")
    for encoded in encoded_rows:
        if not isinstance(encoded, Mapping) or encoded.get("encoding") != "base64":
            raise ValueError("offline FFmpeg encoded license evidence is invalid")
        encoded_path = encoded.get("path")
        if not isinstance(encoded_path, str) or encoded_path not in rows:
            raise ValueError("offline FFmpeg encoded license path is invalid")
        encoded_manifest_row = rows[encoded_path]
        if (
            encoded.get("encoded_size") != encoded_manifest_row.get("size")
            or encoded.get("encoded_sha256") != encoded_manifest_row.get("sha256")
            or not isinstance(encoded.get("source"), str)
            or not str(encoded.get("source")).startswith("repository:")
        ):
            raise ValueError("offline FFmpeg encoded license receipt is inconsistent")
        payload = source_root / _offline_manifest_relative_path(encoded_path)
        try:
            decoded = base64.b64decode(payload.read_bytes(), validate=True)
        except (OSError, ValueError) as exc:
            raise ValueError("offline FFmpeg encoded license is invalid") from exc
        if len(decoded) != encoded.get("content_size") or hashlib.sha256(
            decoded
        ).hexdigest() != encoded.get("content_sha256"):
            raise ValueError("offline FFmpeg decoded license identity is invalid")
    _validate_fixed_ffmpeg_file_row(
        source_root,
        rows,
        index_relative,
        OFFLINE_FFMPEG_ATTESTATION_FILES[index_relative],
    )


def _validate_offline_layout(
    source_root: Path,
    manifest: Mapping[str, object],
    *,
    archive_sha256: str | None,
    strict_ffmpeg: bool = False,
    program_sources: Mapping[str, object] | None = None,
) -> OfflineBundleLayout:
    main = _required_offline_component(manifest, "main_wheelhouse")
    audio = _required_offline_component(manifest, "audio_wheelhouse")
    browser = _required_offline_component(manifest, "playwright_chromium")
    ffmpeg = _required_offline_component(manifest, "ffmpeg")
    if main.get("path") != "wheelhouse/main" or audio.get("path") != "wheelhouse/audio":
        raise ValueError("required offline wheelhouse path is invalid")
    if (
        browser.get("playwright_version") != OFFLINE_PLAYWRIGHT_VERSION
        or browser.get("revision") != OFFLINE_CHROMIUM_REVISION
        or (
            strict_ffmpeg
            and program_sources is not None
            and browser.get("browser_version") != OFFLINE_CHROMIUM_VERSION
        )
        or browser.get("ffmpeg_revision") != OFFLINE_PLAYWRIGHT_FFMPEG_REVISION
        or browser.get("winldd_revision") != OFFLINE_PLAYWRIGHT_WINLDD_REVISION
        or browser.get("winldd_source_verified") is not True
        or browser.get("browser_root") != "browsers"
        or browser.get("launch_verified") is not True
        or browser.get("record_video_verified") is not True
    ):
        raise ValueError("required offline Chromium identity is invalid")
    if (
        ffmpeg.get("ffmpeg_relative_path") != "tools/ffmpeg/bin/ffmpeg.exe"
        or ffmpeg.get("ffprobe_relative_path") != "tools/ffmpeg/bin/ffprobe.exe"
        or ffmpeg.get("ffmpeg_verified") is not True
        or ffmpeg.get("ffprobe_verified") is not True
    ):
        raise ValueError("required offline FFmpeg identity is invalid")
    _validate_fixed_offline_ffmpeg(
        source_root,
        manifest,
        ffmpeg,
        strict=strict_ffmpeg,
    )
    if strict_ffmpeg and program_sources is not None:
        _validate_offline_browser_source_binding(source_root, manifest, program_sources)

    requirements_root = source_root / "requirements"
    main_wheelhouse = source_root / "wheelhouse" / "main"
    audio_wheelhouse = source_root / "wheelhouse" / "audio"
    required_files = (
        requirements_root / "requirements.txt",
        requirements_root / "requirements-offline-main.lock",
        requirements_root / "requirements-offline-bootstrap.lock",
        requirements_root / "requirements-audio.lock",
        requirements_root / "requirements-offline-audio.lock",
        requirements_root / "requirements-audio-build.lock",
        source_root
        / "browsers"
        / f"chromium-{OFFLINE_CHROMIUM_REVISION}"
        / "chrome-win"
        / "chrome.exe",
        source_root
        / "browsers"
        / f"chromium_headless_shell-{OFFLINE_CHROMIUM_REVISION}"
        / "chrome-win"
        / "headless_shell.exe",
        source_root
        / "browsers"
        / f"ffmpeg-{OFFLINE_PLAYWRIGHT_FFMPEG_REVISION}"
        / "ffmpeg-win64.exe",
        source_root / "browsers" / f"winldd-{OFFLINE_PLAYWRIGHT_WINLDD_REVISION}" / "PrintDeps.exe",
        source_root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe",
        source_root / "tools" / "ffmpeg" / "bin" / "ffprobe.exe",
    )
    if not all(path.is_file() for path in required_files):
        raise ValueError("required offline bundle file is missing")
    for wheelhouse in (main_wheelhouse, audio_wheelhouse):
        wheels = sorted(wheelhouse.glob("*.whl"))
        if not wheels or any(path.is_symlink() for path in wheels):
            raise ValueError("required offline wheelhouse is missing or unsafe")
    if not any(
        path.name.casefold().startswith("playwright-1.52.0-")
        for path in main_wheelhouse.glob("*.whl")
    ):
        raise ValueError("required Playwright 1.52.0 wheel is missing")
    return OfflineBundleLayout(
        source_root=source_root,
        requirements_root=requirements_root,
        main_wheelhouse=main_wheelhouse,
        audio_wheelhouse=audio_wheelhouse,
        browser_root=source_root / "browsers",
        ffmpeg_bin=source_root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe",
        ffprobe_bin=source_root / "tools" / "ffmpeg" / "bin" / "ffprobe.exe",
        manifest_sha256=str(manifest["manifest_sha256"]),
        source_commit=str(manifest["source_commit"]),
        archive_sha256=archive_sha256,
        manifest=manifest,
    )


def _verify_staged_offline_runtime(staging_root: Path, manifest: Mapping[str, object]) -> None:
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise ValueError("offline bundle file inventory is invalid")
    checked = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        relative = str(row.get("path", ""))
        if relative.startswith("browsers/"):
            runtime_relative = relative
        elif relative.startswith("tools/ffmpeg/"):
            runtime_relative = relative
        else:
            continue
        candidate = staging_root / Path(*runtime_relative.split("/"))
        if (
            not candidate.is_file()
            or candidate.stat().st_size != row.get("size")
            or offline_sha256_file(candidate) != row.get("sha256")
        ):
            raise ValueError("persisted offline runtime hash verification failed")
        checked += 1
    if checked == 0:
        raise ValueError("required offline runtime inventory is missing")


def _persist_offline_runtime(repo_root: Path, layout: OfflineBundleLayout) -> OfflineBundleLayout:
    target = repo_root / OFFLINE_RUNTIME_RELATIVE_PATH
    unsafe = _unsafe_repo_write_path(repo_root, target)
    if unsafe is not None:
        raise ValueError("offline runtime target is unsafe")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".offline-runtime-staging-", dir=target.parent))
    backup_container: Path | None = None
    backup: Path | None = None
    try:
        shutil.copytree(layout.source_root / "browsers", staging / "browsers")
        shutil.copytree(layout.source_root / "tools" / "ffmpeg", staging / "tools" / "ffmpeg")
        _verify_staged_offline_runtime(staging, layout.manifest)
        if target.exists():
            if _is_reparse_point(target):
                raise ValueError("offline runtime target is unsafe")
            backup_container = Path(
                tempfile.mkdtemp(prefix=".offline-runtime-backup-", dir=target.parent)
            )
            backup = backup_container / target.name
            target.replace(backup)
        try:
            staging.replace(target)
        except Exception:
            if backup is not None and backup.exists():
                backup.replace(target)
            raise
        if backup_container is not None:
            shutil.rmtree(backup_container)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup_container is not None and backup_container.exists():
            if backup is None or not backup.exists():
                shutil.rmtree(backup_container, ignore_errors=True)
        raise
    return OfflineBundleLayout(
        source_root=layout.source_root,
        requirements_root=layout.requirements_root,
        main_wheelhouse=layout.main_wheelhouse,
        audio_wheelhouse=layout.audio_wheelhouse,
        browser_root=target / "browsers",
        ffmpeg_bin=target / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe",
        ffprobe_bin=target / "tools" / "ffmpeg" / "bin" / "ffprobe.exe",
        manifest_sha256=layout.manifest_sha256,
        source_commit=layout.source_commit,
        archive_sha256=layout.archive_sha256,
        manifest=layout.manifest,
    )


@contextmanager
def _prepare_offline_bundle(
    bundle_path: str | Path,
    *,
    repo_root: Path,
    expected_version: str,
    expected_source_commit: str | None = None,
) -> Iterator[OfflineBundleLayout]:
    source = _lexical_absolute(bundle_path)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        temporary_parent = repo_root / "tmp" / "install"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="offline-bundle-", dir=temporary_parent)
        if source.is_dir():
            source_root = Path(temporary.name) / "payload"
            if _is_reparse_point(source):
                raise ValueError("offline bundle directory is unsafe")
            source_root.mkdir()
            for candidate in sorted(source.rglob("*")):
                if _is_reparse_point(candidate):
                    raise ValueError("offline bundle directory is unsafe")
                relative = candidate.relative_to(source)
                destination = source_root / relative
                if candidate.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif candidate.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        candidate.open("rb") as source_stream,
                        destination.open("xb") as target_stream,
                    ):
                        shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                        target_stream.flush()
                        os.fsync(target_stream.fileno())
                else:
                    raise ValueError("offline bundle directory contains an unsafe entry")
            archive_sha256 = None
            verification = verify_offline_bundle(
                source_root,
                expected_version=expected_version,
                expected_source_commit=expected_source_commit,
            )
        else:
            snapshot = Path(temporary.name) / "offline-deps-snapshot.zip"
            with source.open("rb") as source_stream, snapshot.open("xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            archive_sha256 = offline_sha256_file(snapshot)
            verification = verify_offline_bundle(
                snapshot,
                expected_version=expected_version,
                expected_source_commit=expected_source_commit,
            )
            source_root = Path(temporary.name) / "payload"
            extracted = extract_offline_bundle(snapshot, source_root)
            if extracted.get("manifest_sha256") != verification.get("manifest_sha256"):
                raise ValueError("offline bundle verification changed after extraction")
        manifest = verification["manifest"]
        if not isinstance(manifest, Mapping):
            raise ValueError("offline bundle manifest is invalid")
        program_sources: Mapping[str, object] | None = None
        if expected_source_commit is not None:
            source_declaration = repo_root / "scripts" / "release" / "offline_sources.json"
            if source_declaration.is_file() and not _is_reparse_point(source_declaration):
                try:
                    parsed_sources = json.loads(source_declaration.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("program offline source declaration is unreadable") from exc
                if not isinstance(parsed_sources, Mapping):
                    raise ValueError("program offline source declaration is invalid")
                program_sources = parsed_sources
        layout = _validate_offline_layout(
            source_root,
            manifest,
            archive_sha256=archive_sha256,
            strict_ffmpeg=expected_source_commit is not None,
            program_sources=program_sources,
        )
        yield _persist_offline_runtime(repo_root, layout)
    finally:
        if temporary is not None:
            temporary.cleanup()


def _offline_bundle_receipt(layout: OfflineBundleLayout | None) -> dict[str, object]:
    if layout is None:
        return {"status": "not_requested"}
    return {
        "status": "ready",
        "manifest_sha256": layout.manifest_sha256,
        "source_commit": layout.source_commit,
        "archive_sha256": layout.archive_sha256,
        "target": {
            "os": "windows",
            "arch": "x64",
            "python_implementation": "cpython",
            "python_version": "3.11",
            "abi": "cp311",
        },
        "runtime": {
            "browser_root": (OFFLINE_RUNTIME_RELATIVE_PATH / "browsers").as_posix(),
            "ffmpeg": (
                OFFLINE_RUNTIME_RELATIVE_PATH / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
            ).as_posix(),
            "ffprobe": (
                OFFLINE_RUNTIME_RELATIVE_PATH / "tools" / "ffmpeg" / "bin" / "ffprobe.exe"
            ).as_posix(),
        },
    }


def _offline_bundle_report(
    layout: OfflineBundleLayout | None,
    *,
    requested: bool,
    error: str | None,
) -> dict[str, object]:
    if error is not None:
        return {"status": "failed", "error": error}
    if layout is not None:
        return _offline_bundle_receipt(layout)
    if requested:
        return {"status": "not_checked", "reason": "installer_preflight_failed"}
    return {"status": "not_requested"}


def _release_inventory_source_commit(repo_root: Path, *, expected_version: str) -> str | None:
    path = repo_root / "release-inventory.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("release inventory is unreadable") from exc
    source_commit = payload.get("source_commit") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("version") != expected_version
        or not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
    ):
        raise ValueError("release inventory identity is invalid")
    return source_commit


def _dependency_stage(
    repo_root: Path,
    options: InstallOptions,
    *,
    python_executable: str,
    runner: CommandRunner,
    offline: OfflineBundleLayout | None = None,
) -> tuple[dict[str, object], Path]:
    venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
    unsafe_path = _unsafe_repo_write_path(repo_root, venv_python)
    if unsafe_path is not None:
        return (
            stage_result(
                "dependencies",
                status="failed",
                code="unsafe_main_venv_path",
                mandatory=True,
                summary="The main .venv path is not safe for repository-local writes.",
                details=unsafe_path,
            ),
            venv_python,
        )
    evidence: list[dict[str, object]] = []
    if not venv_python.is_file():
        if not options.install_dependencies:
            return (
                stage_result(
                    "dependencies",
                    status="skipped",
                    code="dependencies_skipped",
                    mandatory=True,
                    summary="Main environment installation was explicitly skipped.",
                ),
                venv_python,
            )
        created = _run_command(
            [python_executable, "-m", "venv", str(repo_root / ".venv")],
            cwd=repo_root,
            runner=runner,
        )
        evidence.append(_command_details(created, repo_root=repo_root))
        if created.returncode != 0 or not venv_python.is_file():
            return (
                stage_result(
                    "dependencies",
                    status="failed",
                    code="venv_creation_failed",
                    mandatory=True,
                    summary="Could not create the main isolated Python environment.",
                    details={"commands": evidence},
                ),
                venv_python,
            )

    python_runtime = _probe_python_runtime(
        venv_python,
        repo_root=repo_root,
        runner=runner,
    )
    offline_runtime_compatible = bool(
        offline is None
        or (
            str(python_runtime.get("version", "")).startswith("3.11.")
            and str(python_runtime.get("implementation", "")).casefold() == "cpython"
            and python_runtime.get("bits") == "64bit"
        )
    )
    if python_runtime["compatible"] is not True or not offline_runtime_compatible:
        return (
            stage_result(
                "dependencies",
                status="failed",
                code="venv_python_incompatible",
                mandatory=True,
                summary="The existing main .venv must use 64-bit Python 3.10-3.12.",
                details={"python_runtime": python_runtime, "commands": evidence},
            ),
            venv_python,
        )

    if not options.install_dependencies:
        return (
            stage_result(
                "dependencies",
                status="skipped",
                code="dependencies_skipped",
                mandatory=True,
                summary="Main environment installation was explicitly skipped.",
                details={"python_runtime": python_runtime},
            ),
            venv_python,
        )

    pip_install_prefix = [
        str(venv_python),
        "-m",
        "pip",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--timeout",
        "60",
        "--retries",
        "10",
        "install",
    ]
    if offline is not None:
        pip_install_prefix.extend(
            (
                "--no-index",
                "--find-links",
                str(offline.main_wheelhouse),
                "--only-binary=:all:",
                "--require-hashes",
            )
        )
        commands = (
            [
                *pip_install_prefix,
                "--requirement",
                str(offline.requirements_root / "requirements-offline-bootstrap.lock"),
            ],
            [
                *pip_install_prefix,
                "--requirement",
                str(offline.requirements_root / "requirements-offline-main.lock"),
            ],
        )
    else:
        commands = (
            [*pip_install_prefix, "--upgrade", "pip", "setuptools", "wheel"],
            [*pip_install_prefix, "--requirement", str(repo_root / "requirements.txt")],
        )
    failure_codes = ("packaging_tools_install_failed", "requirements_install_failed")
    for command, failure_code in zip(commands, failure_codes):
        completed = _run_command(command, cwd=repo_root, runner=runner)
        evidence.append(_command_details(completed, repo_root=repo_root))
        if completed.returncode != 0:
            return (
                stage_result(
                    "dependencies",
                    status="failed",
                    code=failure_code,
                    mandatory=True,
                    summary="Main Python dependency installation failed.",
                    details={"commands": evidence},
                ),
                venv_python,
            )

    return (
        stage_result(
            "dependencies",
            status="ready",
            code="requirements_installed",
            mandatory=True,
            summary="The main .venv and requirements.txt dependencies are installed.",
            details={"python_runtime": python_runtime, "commands": evidence},
        ),
        venv_python,
    )


def _dependency_import_stage(
    repo_root: Path,
    *,
    python_executable: Path,
    dependencies_ready: bool,
    runner: CommandRunner,
) -> dict[str, object]:
    if not dependencies_ready or not python_executable.is_file():
        return stage_result(
            "dependency_imports",
            status="failed",
            code="main_dependency_prerequisite_failed",
            mandatory=True,
            summary="The main dependency import probe requires a valid installed .venv.",
        )
    verification = verify_main_dependency_imports(repo_root, python_executable, runner=runner)
    return stage_result(
        "dependency_imports",
        status=str(verification["status"]),
        code=str(verification["code"]),
        mandatory=True,
        summary=str(verification["summary"]),
        details={
            "probe": verification["probe"],
            "command": verification["command"],
        },
    )


def _skills_stage(
    repo_root: Path, *, python_executable: str, runner: CommandRunner
) -> dict[str, object]:
    completed = _run_command(
        [python_executable, str(repo_root / "scripts" / "install_repo_skills.py")],
        cwd=repo_root,
        runner=runner,
    )
    details: dict[str, object] = {"command": _command_details(completed, repo_root=repo_root)}
    if completed.returncode != 0:
        return stage_result(
            "skills",
            status="failed",
            code="skill_install_failed",
            mandatory=True,
            summary="Repository-local skill installation failed.",
            details=details,
        )
    verification = verify_installed_skills(repo_root / "skills", repo_root / ".codex" / "skills")
    details["verification"] = verification
    return stage_result(
        "skills",
        status=str(verification["status"]),
        code=str(verification["code"]),
        mandatory=True,
        summary=(
            "All 16 repository-local Auto-Cut skills match their bundled sources."
            if verification["status"] == "ready"
            else "Repository-local Auto-Cut skills do not match their bundled sources."
        ),
        details=details,
    )


def _playwright_stage(
    repo_root: Path,
    options: InstallOptions,
    *,
    python_executable: str,
    dependencies_ready: bool,
    runner: CommandRunner,
    offline: OfflineBundleLayout | None = None,
) -> dict[str, object]:
    if not options.install_playwright:
        return stage_result(
            "playwright_chromium",
            status="skipped",
            code="playwright_skipped",
            mandatory=True,
            summary="Playwright Chromium installation was explicitly skipped.",
        )
    if not dependencies_ready:
        return stage_result(
            "playwright_chromium",
            status="skipped",
            code="dependency_prerequisite_failed",
            mandatory=True,
            summary="Playwright Chromium requires the main environment.",
        )

    evidence: list[dict[str, object]] = []
    if offline is None:
        install = _run_command(
            [python_executable, "-m", "playwright", "install", "chromium"],
            cwd=repo_root,
            runner=runner,
        )
        evidence.append(_command_details(install, repo_root=repo_root))
        if install.returncode != 0:
            return stage_result(
                "playwright_chromium",
                status="failed",
                code="chromium_install_failed",
                mandatory=True,
                summary="Playwright Chromium installation failed.",
                details={"commands": evidence},
            )

    launch_lines = ["import json"]
    if offline is not None:
        launch_lines.extend(
            (
                "import os",
                f"os.environ['PLAYWRIGHT_BROWSERS_PATH'] = {json.dumps(str(offline.browser_root))}",
            )
        )
    launch_lines.extend(
        (
            "from playwright.sync_api import sync_playwright",
            "with sync_playwright() as runtime:",
            "    browser = runtime.chromium.launch(headless=True)",
            '    print(json.dumps({"ok": True, "browser": "chromium"}))',
            "    browser.close()",
        )
    )
    launch_probe = "\n".join(launch_lines)
    launched = _run_command([python_executable, "-c", launch_probe], cwd=repo_root, runner=runner)
    evidence.append(_command_details(launched, repo_root=repo_root))
    payload = _parse_json_output(launched)
    if launched.returncode != 0 or payload is None or payload.get("ok") is not True:
        return stage_result(
            "playwright_chromium",
            status="failed",
            code="chromium_launch_failed",
            mandatory=True,
            summary="Chromium was installed but did not pass a headless launch probe.",
            details={"commands": evidence},
        )
    return stage_result(
        "playwright_chromium",
        status="ready",
        code="chromium_launch_verified",
        mandatory=True,
        summary="Playwright Chromium installed and launched headlessly.",
        details={"commands": evidence},
    )


def _external_tools_stage(
    repo_root: Path,
    *,
    runner: CommandRunner,
    which: Callable[[str], str | None],
    offline: OfflineBundleLayout | None = None,
) -> dict[str, object]:
    components: dict[str, dict[str, object]] = {}
    offline_tools = (
        {"ffmpeg": str(offline.ffmpeg_bin), "ffprobe": str(offline.ffprobe_bin)}
        if offline is not None
        else {}
    )
    for name in ("ffmpeg", "ffprobe"):
        executable = offline_tools.get(name) or which(name)
        if not executable:
            components[name] = {"status": "unavailable", "code": "not_found"}
            continue
        completed = _run_command([executable, "-version"], cwd=repo_root, runner=runner)
        version_output = "\n".join(
            part for part in (completed.stdout, completed.stderr) if isinstance(part, str)
        )
        identity_verified = bool(
            completed.returncode == 0
            and re.search(
                rf"(?im)^\s*{re.escape(name)}\s+version(?:\s|$)",
                version_output,
            )
        )
        components[name] = {
            "status": "ready" if identity_verified else "unavailable",
            "code": (
                "available"
                if identity_verified
                else "probe_failed" if completed.returncode != 0 else "identity_mismatch"
            ),
            "probe": _command_details(completed, repo_root=repo_root),
        }
    ready = all(component["status"] == "ready" for component in components.values())
    return stage_result(
        "ffmpeg_tools",
        status="ready" if ready else "unavailable",
        code="ffmpeg_tools_ready" if ready else "ffmpeg_tools_unavailable",
        mandatory=False,
        summary=(
            "FFmpeg and FFprobe are available."
            if ready
            else "FFmpeg and FFprobe must be installed on this computer."
        ),
        details={"components": components},
    )


def _audio_stage(
    repo_root: Path,
    options: InstallOptions,
    *,
    python_executable: str,
    runner: CommandRunner,
    offline: OfflineBundleLayout | None = None,
) -> tuple[dict[str, object], dict[str, Any]]:
    if not options.install_audio:
        return (
            stage_result(
                "audio_runtime",
                status="skipped",
                code="audio_install_skipped",
                mandatory=True,
                summary="The isolated audio runtime installation was explicitly skipped.",
            ),
            {},
        )
    entrypoint = repo_root / "scripts" / "audio" / "audio_cleanup.py"
    setup_command = [python_executable, str(entrypoint), "setup"]
    if offline is not None:
        setup_command.extend(
            (
                "--offline-wheelhouse",
                str(offline.audio_wheelhouse),
                "--ffmpeg-bin",
                str(offline.ffmpeg_bin),
                "--ffprobe-bin",
                str(offline.ffprobe_bin),
            )
        )
    setup = _run_command(setup_command, cwd=repo_root, runner=runner)
    setup_payload = _parse_json_output(setup)
    evidence = [_command_details(setup, repo_root=repo_root)]
    if setup.returncode != 0 or setup_payload is None or setup_payload.get("ok") is not True:
        return (
            stage_result(
                "audio_runtime",
                status="failed",
                code="audio_install_failed",
                mandatory=True,
                summary="The isolated .venv-audio installation failed.",
                details={"commands": evidence},
            ),
            {},
        )

    audio_python = repo_root / ".venv-audio" / "Scripts" / "python.exe"
    doctor_command = [
        python_executable,
        str(entrypoint),
        "doctor",
        "--python-executable",
        str(audio_python),
    ]
    if offline is not None:
        doctor_command.extend(
            (
                "--ffmpeg-bin",
                str(offline.ffmpeg_bin),
                "--ffprobe-bin",
                str(offline.ffprobe_bin),
            )
        )
    doctor = _run_command(
        doctor_command,
        cwd=repo_root,
        runner=runner,
    )
    evidence.append(_command_details(doctor, repo_root=repo_root))
    doctor_payload = _parse_json_output(doctor)
    spectramini = doctor_payload.get("spectramini") if isinstance(doctor_payload, dict) else None
    spectramini_checks = spectramini.get("smoke_checks") if isinstance(spectramini, dict) else None
    spectramini_smoke_ready = bool(
        isinstance(spectramini, dict)
        and spectramini.get("smoke_status") == "passed"
        and spectramini.get("algorithm_identity") == SPECTRAMINI_SMOKE_ALGORITHM
        and isinstance(spectramini_checks, dict)
        and all(
            spectramini_checks.get(check_id) is True
            for check_id in SPECTRAMINI_SMOKE_REQUIRED_CHECKS
        )
    )
    core_ready = bool(
        isinstance(doctor_payload, dict)
        and all(
            isinstance(doctor_payload.get(component_id), dict)
            and doctor_payload[component_id].get("ok") is True
            and isinstance(doctor_payload[component_id].get("identity"), str)
            and bool(doctor_payload[component_id]["identity"])
            for component_id in ("python", "ffmpeg", "ffprobe", "spectramini")
        )
        and spectramini_smoke_ready
    )
    external_models_blocked = bool(
        isinstance(doctor_payload, dict)
        and all(
            isinstance(doctor_payload.get(component_id), dict)
            and doctor_payload[component_id].get("ok") is False
            and doctor_payload[component_id].get("execution_status") == "external_unavailable"
            and type(doctor_payload[component_id].get("asset_verification_ok")) is bool
            for component_id in ("deepfilternet", "respiro_en")
        )
    )
    doctor_valid = bool(
        doctor.returncode == 0
        and isinstance(doctor_payload, dict)
        and doctor_payload.get("status") == "degraded"
        and doctor_payload.get("full") is False
        and doctor_payload.get("degraded") is True
        and doctor_payload.get("unavailable") is False
        and doctor_payload.get("execution_policy") == "external_models_fail_closed"
        and core_ready
        and external_models_blocked
    )
    if not doctor_valid:
        return (
            stage_result(
                "audio_runtime",
                status="failed",
                code="audio_doctor_failed",
                mandatory=True,
                summary="Audio doctor did not produce a valid runtime report.",
                details={"commands": evidence},
            ),
            {},
        )
    status, code = "degraded", "audio_runtime_degraded"
    return (
        stage_result(
            "audio_runtime",
            status=status,
            code=code,
            mandatory=True,
            summary=(
                "Audio doctor reports all verified stages ready."
                if status == "ready"
                else "Audio doctor reports optional or required components that are not ready."
            ),
            details={"commands": evidence},
        ),
        sanitize_payload(doctor_payload, repo_root=repo_root),
    )


def _resolve_audio_python_311(
    repo_root: Path,
    *,
    host_python: str,
    host_version: tuple[int, int, int],
    runner: CommandRunner,
    which: Callable[[str], str | None],
) -> tuple[str | None, dict[str, object]]:
    probe_script = (
        "# auto_cut_audio_python_probe_v1\n"
        "import json, platform, sys; "
        "print(json.dumps({'ok': True, 'version': list(sys.version_info[:3]), "
        "'bits': platform.architecture()[0], 'executable': sys.executable}))"
    )
    candidates: list[tuple[str, list[str]]] = []
    if host_version[:2] == (3, 11):
        candidates.append((host_python, []))
    py_launcher = which("py")
    if py_launcher:
        candidates.append((py_launcher, ["-3.11"]))
    python_311 = which("python3.11")
    if python_311:
        candidates.append((python_311, []))

    evidence: list[dict[str, object]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for executable, prefix in candidates:
        key = (os.path.normcase(str(executable)), tuple(prefix))
        if key in seen:
            continue
        seen.add(key)
        completed = _run_command(
            [executable, *prefix, "-I", "-c", probe_script],
            cwd=repo_root,
            runner=runner,
        )
        evidence.append(_command_details(completed, repo_root=repo_root))
        payload = _parse_json_output(completed)
        version = payload.get("version") if payload else None
        resolved = payload.get("executable") if payload else None
        if (
            completed.returncode == 0
            and payload is not None
            and payload.get("ok") is True
            and isinstance(version, list)
            and len(version) >= 2
            and version[:2] == [3, 11]
            and payload.get("bits") == "64bit"
            and isinstance(resolved, str)
            and bool(resolved.strip())
        ):
            return resolved, {
                "required_version": "3.11.x",
                "status": "ready",
                "code": "audio_python_311_resolved",
                "commands": evidence,
            }
    return None, {
        "required_version": "3.11.x",
        "status": "failed",
        "code": "audio_python_311_unavailable",
        "commands": evidence,
    }


def _jianying_stage(
    repo_root: Path,
    options: InstallOptions,
    *,
    python_executable: str,
    runner: CommandRunner,
) -> tuple[dict[str, object], dict[str, Any]]:
    if options.onboarding_only:
        return (
            stage_result(
                "jianying",
                status="skipped",
                code="onboarding_only",
                mandatory=False,
                summary=(
                    "Target-machine JianYing state was not inspected during clean-release "
                    "onboarding acceptance."
                ),
            ),
            {},
        )
    if not options.run_jianying_check:
        return (
            stage_result(
                "jianying",
                status="skipped",
                code="jianying_check_skipped",
                mandatory=False,
                summary="The JianYing environment self-check was explicitly skipped.",
            ),
            {},
        )
    completed = _run_command(
        [
            python_executable,
            str(repo_root / "scripts" / "jy_wrapper.py"),
            "self-check",
            "--cleanup",
            "--refresh",
            "--json",
        ],
        cwd=repo_root,
        runner=runner,
    )
    payload = _parse_json_output(completed)
    details = {"command": _command_details(completed, repo_root=repo_root)}
    sanitized_payload = sanitize_payload(payload or {}, repo_root=repo_root)
    if payload is None:
        return (
            stage_result(
                "jianying",
                status="failed",
                code="jianying_check_failed",
                mandatory=True,
                summary="JianYing self-check did not complete successfully.",
                details=details,
            ),
            sanitized_payload,
        )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    smoke_check = checks.get("smoke_test") if isinstance(checks.get("smoke_test"), dict) else {}
    draft_path_value = smoke_check.get("draft_path")
    if isinstance(draft_path_value, str) and draft_path_value.strip():
        draft_path = Path(draft_path_value)
        if not draft_path.is_absolute():
            draft_path = repo_root / draft_path
        cleanup_verified = not os.path.lexists(draft_path)
        details["cleanup"] = {
            "reported": True,
            "verified_removed": cleanup_verified,
        }
        if not cleanup_verified:
            return (
                stage_result(
                    "jianying",
                    status="failed",
                    code="jianying_cleanup_verification_failed",
                    mandatory=True,
                    summary="The JianYing smoke draft still exists after requested cleanup.",
                    details=details,
                ),
                sanitized_payload,
            )
    usable = payload.get("ok") is True and data.get("usable") is True
    if usable:
        if completed.returncode != 0:
            return (
                stage_result(
                    "jianying",
                    status="failed",
                    code="jianying_check_failed",
                    mandatory=True,
                    summary="JianYing self-check returned an inconsistent exit status.",
                    details=details,
                ),
                sanitized_payload,
            )
        if smoke_check.get("ok") is not True:
            return (
                stage_result(
                    "jianying",
                    status="failed",
                    code="jianying_check_failed",
                    mandatory=True,
                    summary="JianYing self-check reported inconsistent smoke-test status.",
                    details=details,
                ),
                sanitized_payload,
            )
        if not isinstance(draft_path_value, str) or not draft_path_value.strip():
            return (
                stage_result(
                    "jianying",
                    status="failed",
                    code="jianying_cleanup_evidence_missing",
                    mandatory=True,
                    summary="JianYing self-check did not report the smoke-draft cleanup path.",
                    details=details,
                ),
                sanitized_payload,
            )
        if not smoke_editability_receipt_valid(smoke_check.get("structure")):
            return (
                stage_result(
                    "jianying",
                    status="failed",
                    code="jianying_editability_evidence_missing",
                    mandatory=True,
                    summary="JianYing self-check did not prove the smoke draft remained editable.",
                    details=details,
                ),
                sanitized_payload,
            )
        return (
            stage_result(
                "jianying",
                status="ready",
                code="jianying_self_check_ready",
                mandatory=True,
                summary="JianYing editable-draft smoke test completed and cleanup was verified.",
                details=details,
            ),
            sanitized_payload,
        )
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    blocked = (
        completed.returncode == 1
        and payload.get("ok") is False
        and payload.get("code") == "runtime_error"
        and data.get("usable") is False
        and summary.get("status") == "blocked"
        and blocked_jianying_checks_valid(checks)
    )
    if blocked:
        return (
            stage_result(
                "jianying",
                status="pending",
                code="local_jianying_required",
                mandatory=False,
                summary="A compatible local JianYing environment is required on this computer.",
                details=details,
            ),
            sanitized_payload,
        )
    return (
        stage_result(
            "jianying",
            status="failed",
            code="jianying_check_failed",
            mandatory=True,
            summary="JianYing self-check did not complete successfully.",
            details=details,
        ),
        sanitized_payload,
    )


def _onboarding_only_first_use_status() -> dict[str, object]:
    """Return a deterministic first-use receipt without reading target state.

    Clean-release acceptance must not inspect JianYing, Lark, Volc, TTS, or
    cloud-material state from the machine running the build.  The target user
    completes those capabilities during onboarding after installation.
    """
    return {
        "schema_version": 1,
        "mode": "onboarding_only",
        "jianying_draft_delivery": {
            "status": "pending",
            "configured_root": None,
            "setting_key": "currentCustomDraftPath",
            "first_use_requires_path": True,
        },
        "favorites": {"status": "pending", "local_index_count": 0},
        "subject_pointer": {"status": "pending", "profile_count": 0},
        "feishu": {"status": "pending", "notification_only": True},
        "volc_asr": {"status": "pending", "configured": False},
        "cloud_materials": {"status": "pending", "remote_urls_verified": False},
        "tts": {
            "status": "pending",
            "sami": {"status": "pending"},
            "edge": {"status": "pending"},
        },
        "subtitle_material_matching": {
            "status": "degraded",
            "provider": "deterministic_round_robin",
        },
        "carnac": {"status": "pending", "optional": True},
    }


def _collect_first_use_status(
    repo_root: Path,
    *,
    python_executable: str,
    runner: CommandRunner,
) -> tuple[dict[str, object], dict[str, object]]:
    completed = _run_command(
        [
            python_executable,
            str(repo_root / "scripts" / "auto_cut_first_run.py"),
            "status",
            "--json",
        ],
        cwd=repo_root,
        runner=runner,
    )
    status = _parse_json_output(completed)

    def nested_status(path: tuple[str, ...]) -> str | None:
        value: object = status
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if not isinstance(value, dict):
            return None
        candidate = value.get("status")
        return str(candidate) if candidate in CAPABILITY_STATUSES else None

    measured_states = [nested_status(path) for path in FIRST_USE_CAPABILITY_PATHS.values()]
    if (
        completed.returncode != 0
        or status is None
        or status.get("schema_version") != 1
        or any(state is None for state in measured_states)
    ):
        return (
            stage_result(
                "first_use",
                status="failed",
                code="first_use_status_failed",
                mandatory=True,
                summary="Could not inspect target-owned first-use capability state.",
                details={"command": _command_details(completed, repo_root=repo_root)},
            ),
            {},
        )
    ready = all(state == "ready" for state in measured_states)
    return (
        stage_result(
            "first_use",
            status="ready" if ready else "pending",
            code="first_use_ready" if ready else "first_use_actions_pending",
            mandatory=False,
            summary=(
                "All target-owned first-use capabilities report ready."
                if ready
                else "One or more target-owned first-use capabilities remain pending."
            ),
        ),
        sanitize_payload(status, repo_root=repo_root),
    )


def _runtime_capability_code(capability: dict[str, object], status: str) -> str:
    if status == "ready":
        return "verified_ready"
    if status == "degraded":
        return "verified_degraded"
    if status == "failed":
        return "verification_failed"
    if status == "skipped":
        return "verification_skipped"
    if status == "unavailable":
        return (
            "unverified_external_model"
            if capability.get("unavailable") is True
            else "verified_unavailable"
        )
    capability_id = str(capability.get("id") or "")
    if capability_id == "favorite_text_assets":
        return "requires_user_resync"
    if capability.get("requires_user_authorization") is True:
        return "requires_user_authorization"
    if capability.get("requires_user_assets") is True:
        return "requires_user_assets"
    if capability.get("requires_local_jianying") is True:
        return "requires_local_jianying"
    if capability.get("installed_on_first_run") is True:
        return "install_on_first_run"
    return "requires_local_software"


def _capability_table(
    manifest: dict[str, object],
    *,
    stages: Sequence[dict[str, object]],
    audio_doctor: dict[str, Any],
    first_use: dict[str, object],
) -> list[dict[str, object]]:
    stage_by_id = {str(stage["id"]): stage for stage in stages}
    rows: dict[str, dict[str, object]] = {}
    for capability in manifest["capabilities"]:
        result = capability["actual_result"]
        rows[str(capability["id"])] = {
            "id": capability["id"],
            "label": capability["label"],
            **{field: capability[field] for field in sorted(CAPABILITY_CLASSIFICATION_FIELDS)},
            "verification_command": capability["verification_command"],
            "status": result["status"],
            "code": result["code"],
            "summary": result["summary"],
        }

    def apply_stage(capability_id: str, stage_id: str) -> None:
        stage = stage_by_id.get(stage_id)
        if stage is None or capability_id not in rows:
            return
        status = str(stage["status"])
        rows[capability_id].update(
            status=status,
            code=_runtime_capability_code(rows[capability_id], status),
            summary=stage["summary"],
        )

    apply_stage("auto_cut_skills", "skills")
    apply_stage("main_python_dependencies", "dependency_imports")
    apply_stage("playwright_chromium", "playwright_chromium")
    apply_stage("jianying_smoke_draft", "jianying")
    apply_stage("audio_runtime", "audio_runtime")

    first_use_failed = stage_by_id.get("first_use", {}).get("status") == "failed"
    for capability_id, status_path in FIRST_USE_CAPABILITY_PATHS.items():
        if first_use_failed and capability_id in rows:
            rows[capability_id].update(
                status="failed",
                code="verification_failed",
                summary="Target-owned first-use capability state could not be inspected.",
            )
            continue
        status: object = first_use
        for status_id in status_path:
            status = status.get(status_id) if isinstance(status, dict) else None
        if not isinstance(status, dict) or capability_id not in rows:
            continue
        runtime_status = str(status.get("status", "pending"))
        if runtime_status not in CAPABILITY_STATUSES:
            runtime_status = "failed"
        rows[capability_id]["status"] = runtime_status
        measured_code = str(status.get("code") or "")
        rows[capability_id]["code"] = (
            measured_code
            if measured_code in CAPABILITY_RESULT_CODES
            else _runtime_capability_code(rows[capability_id], runtime_status)
        )

    tools_stage = stage_by_id.get("ffmpeg_tools", {})
    components = tools_stage.get("details", {}).get("components", {})
    for tool_id in ("ffmpeg", "ffprobe"):
        component = components.get(tool_id)
        if isinstance(component, dict):
            component_status = component.get("status", "unavailable")
            rows[tool_id]["status"] = component_status
            rows[tool_id]["code"] = (
                "verified_ready" if component_status == "ready" else "verified_unavailable"
            )
            rows[tool_id]["summary"] = (
                f"{tool_id} was found and its version probe succeeded."
                if component_status == "ready"
                else f"{tool_id} is not available on this computer."
            )

    audio_mapping = {
        "spectramini_cleanup": "spectramini",
        "deepfilternet": "deepfilternet",
        "respiro": "respiro_en",
    }
    for capability_id, component_id in audio_mapping.items():
        component = audio_doctor.get(component_id)
        if not isinstance(component, dict):
            continue
        if rows[capability_id]["unavailable"]:
            rows[capability_id]["status"] = "unavailable"
            rows[capability_id]["code"] = "unverified_external_model"
        elif component.get("ok") is True:
            rows[capability_id]["status"] = "ready"
            rows[capability_id]["code"] = _runtime_capability_code(rows[capability_id], "ready")
        else:
            rows[capability_id]["status"] = "degraded"
            rows[capability_id]["code"] = _runtime_capability_code(rows[capability_id], "degraded")
    return [rows[capability_id] for capability_id in sorted(rows)]


def _run_install_core(
    repo_root: Path,
    options: InstallOptions,
    *,
    runner: CommandRunner | None = None,
    platform_name: str | None = None,
    machine: str | None = None,
    windows_version: tuple[int, int, int] | None = None,
    windows_product_type: int | None = None,
    python_version: tuple[int, int, int] | None = None,
    python_implementation: str | None = None,
    python_bits: int | None = None,
    python_executable: str | None = None,
    which: Callable[[str], str | None] | None = None,
    offline: OfflineBundleLayout | None = None,
    offline_requested: bool = False,
    offline_error: str | None = None,
) -> dict[str, object]:
    root = _lexical_absolute(repo_root)
    effective_runner = runner or (_offline_default_runner if offline_requested else _default_runner)
    effective_platform = platform_name or platform.system()
    effective_machine = machine or platform.machine()
    if windows_version is None or windows_product_type is None:
        if platform_name is None and effective_platform.lower() == "windows":
            detected_windows_version, detected_product_type = _current_windows_evidence()
        else:
            detected_windows_version, detected_product_type = (10, 0, 0), 1
        effective_windows_version = windows_version or detected_windows_version
        effective_windows_product_type = (
            windows_product_type if windows_product_type is not None else detected_product_type
        )
    else:
        effective_windows_version = windows_version
        effective_windows_product_type = windows_product_type
    effective_version = python_version or tuple(sys.version_info[:3])
    effective_implementation = python_implementation or platform.python_implementation()
    effective_bits = python_bits or (64 if sys.maxsize > 2**32 else 32)
    effective_python = python_executable or sys.executable
    effective_which = which or shutil.which
    stages: list[dict[str, object]] = []
    unsafe_path = _unsafe_repo_write_path(
        root,
        root / ".venv" / "Scripts" / "python.exe",
    )
    if unsafe_path is not None:
        stages.append(
            stage_result(
                "dependencies",
                status="failed",
                code="unsafe_main_venv_path",
                mandatory=True,
                summary="The main .venv path is not safe for repository-local writes.",
                details=unsafe_path,
            )
        )
        return sanitize_payload(
            {
                "schema_version": 1,
                "installer_version": INSTALLER_VERSION,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "failed",
                "stages": stages,
                "capability_manifest": {"status": "skipped"},
                "capability_table": [],
                "first_use": {},
                "audio_doctor": {},
                "jianying_self_check": {},
                "offline_bundle": _offline_bundle_report(
                    offline,
                    requested=offline_requested,
                    error=offline_error,
                ),
            },
            repo_root=root,
        )
    manifest_path = root / "capability-manifest.json"
    try:
        manifest = load_and_validate_capability_manifest(manifest_path)
    except ValueError as exc:
        stages.append(
            stage_result(
                "manifest",
                status="failed",
                code="capability_manifest_invalid",
                mandatory=True,
                summary="The bundled capability manifest failed validation.",
                details={"error": str(exc)},
            )
        )
        report: dict[str, object] = {
            "schema_version": 1,
            "installer_version": INSTALLER_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "stages": stages,
            "capability_manifest": {"status": "failed"},
            "capability_table": [],
            "first_use": {},
            "audio_doctor": {},
            "jianying_self_check": {},
            "offline_bundle": _offline_bundle_report(
                offline,
                requested=offline_requested,
                error=offline_error,
            ),
        }
        sanitized_report = sanitize_payload(report, repo_root=root)
        _write_report(sanitized_report, root / INSTALL_REPORT_RELATIVE_PATH)
        return sanitized_report

    manifest_summary = {
        "status": "ready",
        "schema_version": manifest["schema_version"],
        "release_version": manifest["release_version"],
        "sha256": _sha256_file(manifest_path),
    }
    stages.append(
        stage_result(
            "manifest",
            status="ready",
            code="capability_manifest_valid",
            mandatory=True,
            summary="The bundled capability manifest passed validation.",
            details=manifest_summary,
        )
    )

    platform_details = _platform_evidence(
        effective_platform,
        effective_machine,
        windows_version=effective_windows_version,
        windows_product_type=effective_windows_product_type,
    )
    platform_ok = platform_details["supported"] is True
    stages.append(
        stage_result(
            "platform",
            status="ready" if platform_ok else "failed",
            code="windows_x64" if platform_ok else "unsupported_platform",
            mandatory=True,
            summary=(
                "Windows x64 host detected."
                if platform_ok
                else "Auto-Cut full setup requires Windows 10/11 x64."
            ),
            details=platform_details,
        )
    )
    python_ok = (
        offline_python_supported(effective_version, effective_implementation, effective_bits)
        if offline_requested
        else python_supported(effective_version)
    )
    stages.append(
        stage_result(
            "python",
            status="ready" if python_ok else "failed",
            code=(
                "offline_python_311_ready"
                if python_ok and offline_requested
                else (
                    "python_supported"
                    if python_ok
                    else (
                        "offline_python_311_required" if offline_requested else "unsupported_python"
                    )
                )
            ),
            mandatory=True,
            summary=(
                "CPython 3.11 x64 detected for offline installation."
                if python_ok and offline_requested
                else (
                    "Python 3.10-3.12 detected."
                    if python_ok
                    else (
                        "Offline installation requires 64-bit CPython 3.11."
                        if offline_requested
                        else "Python 3.10, 3.11, or 3.12 is required."
                    )
                )
            ),
            details={
                "version": ".".join(str(part) for part in effective_version),
                "implementation": effective_implementation,
                "bits": effective_bits,
            },
        )
    )

    if offline_requested:
        if offline_error is not None:
            stages.append(
                stage_result(
                    "offline_bundle",
                    status="failed",
                    code="offline_bundle_invalid",
                    mandatory=True,
                    summary="The offline dependency companion failed verification.",
                    details={"error": offline_error},
                )
            )
        elif offline is not None:
            stages.append(
                stage_result(
                    "offline_bundle",
                    status="ready",
                    code="offline_bundle_verified",
                    mandatory=True,
                    summary="The offline dependency companion and persisted runtime passed verification.",
                    details=_offline_bundle_receipt(offline),
                )
            )

    audio_doctor: dict[str, Any] = {}
    jianying_self_check: dict[str, Any] = {}
    first_use: dict[str, object] = {}
    if platform_ok and python_ok and (not offline_requested or offline is not None):
        dependencies, venv_python = _dependency_stage(
            root,
            options,
            python_executable=effective_python,
            runner=effective_runner,
            offline=offline,
        )
        stages.append(dependencies)
        python_runtime = dependencies.get("details", {}).get("python_runtime", {})
        runtime_compatible = (
            isinstance(python_runtime, dict) and python_runtime.get("compatible") is True
        )
        command_python = str(venv_python) if runtime_compatible else effective_python
        dependency_environment_ready = (
            dependencies["status"] in {"ready", "skipped"} and runtime_compatible
        )
        dependency_imports = _dependency_import_stage(
            root,
            python_executable=venv_python,
            dependencies_ready=dependency_environment_ready,
            runner=effective_runner,
        )
        stages.append(dependency_imports)
        stages.append(
            _skills_stage(root, python_executable=command_python, runner=effective_runner)
        )
        stages.append(
            _playwright_stage(
                root,
                options,
                python_executable=command_python,
                dependencies_ready=dependency_imports["status"] == "ready",
                runner=effective_runner,
                offline=offline,
            )
        )
        stages.append(
            _external_tools_stage(
                root,
                runner=effective_runner,
                which=effective_which,
                offline=offline,
            )
        )
        if options.install_audio:
            audio_python, audio_python_details = _resolve_audio_python_311(
                root,
                host_python=effective_python,
                host_version=effective_version,
                runner=effective_runner,
                which=effective_which,
            )
        else:
            audio_python, audio_python_details = effective_python, {}
        if audio_python is None:
            audio = stage_result(
                "audio_runtime",
                status="failed",
                code="audio_python_311_unavailable",
                mandatory=True,
                summary="A separate 64-bit Python 3.11 runtime is required for audio installation.",
                details=audio_python_details,
            )
        else:
            audio, audio_doctor = _audio_stage(
                root,
                options,
                python_executable=audio_python,
                runner=effective_runner,
                offline=offline,
            )
            if audio_python_details:
                audio["details"]["python_resolution"] = audio_python_details
        stages.append(audio)
        jianying, jianying_self_check = _jianying_stage(
            root,
            options,
            python_executable=command_python,
            runner=effective_runner,
        )
        stages.append(jianying)
        if options.onboarding_only:
            first_use = _onboarding_only_first_use_status()
            first_use_stage = stage_result(
                "first_use",
                status="pending",
                code="onboarding_only",
                mandatory=False,
                summary=(
                    "Target-owned first-use capabilities were left pending for explicit "
                    "target-machine onboarding."
                ),
            )
        else:
            first_use_stage, first_use = _collect_first_use_status(
                root,
                python_executable=command_python,
                runner=effective_runner,
            )
        stages.append(first_use_stage)

    report: dict[str, object] = {
        "schema_version": 1,
        "installer_version": INSTALLER_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": install_status(stages),
        "stages": stages,
        "capability_manifest": manifest_summary,
        "capability_table": _capability_table(
            manifest,
            stages=stages,
            audio_doctor=audio_doctor,
            first_use=first_use,
        ),
        "first_use": first_use,
        "audio_doctor": audio_doctor,
        "jianying_self_check": jianying_self_check,
        "offline_bundle": _offline_bundle_report(
            offline,
            requested=offline_requested,
            error=offline_error,
        ),
    }
    sanitized_report = sanitize_payload(report, repo_root=root)
    report_path = root / INSTALL_REPORT_RELATIVE_PATH
    _write_report(sanitized_report, report_path)
    return sanitized_report


def run_install(
    repo_root: Path,
    options: InstallOptions,
    *,
    runner: CommandRunner | None = None,
    platform_name: str | None = None,
    machine: str | None = None,
    windows_version: tuple[int, int, int] | None = None,
    windows_product_type: int | None = None,
    python_version: tuple[int, int, int] | None = None,
    python_implementation: str | None = None,
    python_bits: int | None = None,
    python_executable: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, object]:
    common = {
        "runner": runner,
        "platform_name": platform_name,
        "machine": machine,
        "windows_version": windows_version,
        "windows_product_type": windows_product_type,
        "python_version": python_version,
        "python_implementation": python_implementation,
        "python_bits": python_bits,
        "python_executable": python_executable,
        "which": which,
    }
    if options.offline_bundle is None:
        return _run_install_core(repo_root, options, **common)

    effective_platform = platform_name or platform.system()
    effective_machine = machine or platform.machine()
    effective_version = python_version or tuple(sys.version_info[:3])
    effective_implementation = python_implementation or platform.python_implementation()
    effective_bits = python_bits or (64 if sys.maxsize > 2**32 else 32)
    if windows_version is None or windows_product_type is None:
        if platform_name is None and effective_platform.casefold() == "windows":
            detected_windows_version, detected_product_type = _current_windows_evidence()
        else:
            detected_windows_version, detected_product_type = (10, 0, 0), 1
        effective_windows_version = windows_version or detected_windows_version
        effective_product_type = (
            windows_product_type if windows_product_type is not None else detected_product_type
        )
    else:
        effective_windows_version = windows_version
        effective_product_type = windows_product_type
    preflight_ready = platform_supported(
        effective_platform,
        effective_machine,
        windows_version=effective_windows_version,
        windows_product_type=effective_product_type,
    ) and offline_python_supported(
        effective_version,
        effective_implementation,
        effective_bits,
    )
    if not preflight_ready:
        return _run_install_core(
            repo_root,
            options,
            offline_requested=True,
            **common,
        )

    root = _lexical_absolute(repo_root)
    try:
        manifest = load_and_validate_capability_manifest(root / "capability-manifest.json")
        expected_version = str(manifest["release_version"])
        expected_source_commit = _release_inventory_source_commit(
            root,
            expected_version=expected_version,
        )
        with _prepare_offline_bundle(
            options.offline_bundle,
            repo_root=root,
            expected_version=expected_version,
            expected_source_commit=expected_source_commit,
        ) as offline:
            return _run_install_core(
                root,
                options,
                offline=offline,
                offline_requested=True,
                **common,
            )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _run_install_core(
            root,
            options,
            offline_requested=True,
            offline_error=str(exc),
            **common,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install the complete Auto-Cut runtime.")
    parser.add_argument(
        "--probe-main-imports",
        action="store_true",
        help="Probe all declared main dependency imports in the current interpreter and exit.",
    )
    parser.add_argument("--skip-dependencies", action="store_true")
    parser.add_argument("--skip-playwright", action="store_true")
    parser.add_argument("--skip-audio", action="store_true")
    parser.add_argument("--skip-jianying-check", action="store_true")
    parser.add_argument(
        "--onboarding-only",
        action="store_true",
        help="Do not inspect target-owned JianYing or first-use authorization state.",
    )
    parser.add_argument(
        "--offline-bundle",
        help="Install from a verified offline-deps ZIP or extracted directory; requires CPython 3.11 x64.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _format_stage_table(report: dict[str, object]) -> str:
    rows = ["Capability stage                 Status       Result"]
    rows.append("-" * 72)
    for stage in report.get("stages", []):
        rows.append(f"{str(stage['id']):32} {str(stage['status']):12} {str(stage['code'])}")
    capabilities = report.get("capability_table", [])
    if capabilities:
        rows.extend(("", "Capability                       Status       Result", "-" * 72))
        for capability in capabilities:
            rows.append(
                f"{str(capability['id']):32} "
                f"{str(capability['status']):12} {str(capability['code'])}"
            )
    rows.extend(
        (
            "",
            "First-use commands",
            r".\.venv\Scripts\python.exe scripts\auto_cut_first_run.py guide",
            r".\.venv\Scripts\python.exe scripts\auto_cut_first_run.py favorites-sync",
            r".\.venv\Scripts\python.exe scripts\auto_cut_first_run.py pointer-guide",
            r".\.venv\Scripts\python.exe scripts\auto_cut_first_run.py feishu-status",
            "",
            "Supported Auto-Cut skill usage modes",
            "1. Natural language: describe the editing result; auto-cut routes it.",
            "2. Router skill: ask auto-cut to select one or more child skills.",
            "3. Targeted skill: name an exact auto-cut-* skill when already known.",
        )
    )
    return "\n".join(rows)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.probe_main_imports:
        probe = probe_main_dependencies_in_process()
        print(json.dumps(probe, ensure_ascii=False, sort_keys=True))
        return 0 if probe["status"] == "ready" else 1
    repo_root = _lexical_absolute(Path(__file__).parent.parent)
    options = InstallOptions(
        install_dependencies=not args.skip_dependencies,
        install_playwright=not args.skip_playwright,
        install_audio=not args.skip_audio,
        run_jianying_check=not args.skip_jianying_check,
        onboarding_only=args.onboarding_only,
        offline_bundle=args.offline_bundle,
    )
    report = run_install(repo_root, options)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    else:
        print(_format_stage_table(report))
        print(f"\nInstall report: {INSTALL_REPORT_RELATIVE_PATH.as_posix()}")
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
