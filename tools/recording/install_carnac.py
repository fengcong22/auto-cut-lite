#!/usr/bin/env python3
"""Detect an existing Carnac installation without downloading executables."""

from __future__ import annotations

import os
import shutil
import sys
import threading

_install_lock = threading.Lock()

# Kept for backward-compatible imports. A URL alone never authorizes a download.
CARNAC_NUPKG_URL = ""
CARNAC_ASSET_VERIFIED = False
BUNDLE_DIR_NAME = "carnac_bundle"


def _script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def bundled_carnac_exe() -> str:
    return os.path.join(_script_dir(), BUNDLE_DIR_NAME, "lib", "net45", "Carnac.exe")


def _is_valid_carnac_executable(path: str) -> bool:
    if os.path.basename(path).casefold() != "carnac.exe" or not os.path.isfile(path):
        return False
    try:
        file_size = os.path.getsize(path)
        with open(path, "rb") as stream:
            dos_header = stream.read(64)
            if len(dos_header) != 64 or dos_header[:2] != b"MZ":
                return False
            pe_offset = int.from_bytes(dos_header[0x3C:0x40], "little")
            if pe_offset < 64 or pe_offset > file_size - 4:
                return False
            stream.seek(pe_offset)
            return stream.read(4) == b"PE\0\0"
    except OSError:
        return False


def _find_target_carnac() -> tuple[str, str] | None:
    if sys.platform != "win32":
        return None

    configured = (os.environ.get("CARNAC_EXE") or "").strip()
    if configured and _is_valid_carnac_executable(configured):
        return "target_configured", configured

    bundled = bundled_carnac_exe()
    if _is_valid_carnac_executable(bundled):
        return "bundled", bundled

    system = shutil.which("Carnac.exe")
    if system and _is_valid_carnac_executable(system):
        return "system", system

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Carnac", "Carnac.exe"),
        os.path.join(local_app_data, "Programs", "Carnac", "Carnac.exe"),
    ]
    for candidate in candidates:
        if _is_valid_carnac_executable(candidate):
            return "target_local", candidate

    squirrel_root = os.path.join(local_app_data, "Carnac")
    if os.path.isdir(squirrel_root):
        try:
            for name in sorted(os.listdir(squirrel_root), reverse=True):
                candidate = os.path.join(squirrel_root, name, "Carnac.exe")
                if name.startswith("app-") and _is_valid_carnac_executable(candidate):
                    return "target_local", candidate
        except OSError:
            return None
    return None


def carnac_status() -> dict[str, object]:
    found = _find_target_carnac()
    if sys.platform != "win32":
        return {
            "status": "unavailable",
            "code": "windows_only_optional_tool",
            "target_local": False,
        }
    if found is not None:
        return {
            "status": "ready",
            "code": "target_local_carnac_found",
            "target_local": True,
            "source": found[0],
            "probe": "named_pe_executable_verified",
        }
    return {
        "status": "pending",
        "code": "optional_local_tool_required",
        "target_local": False,
        "automatic_download": False,
    }


def ensure_bundled_carnac(verbose: bool = False) -> bool:
    """Return whether a verified bundled executable already exists.

    Automatic download stays disabled until a future release records a fixed
    version, license, trusted URL, SHA-256, and executable probe.
    """

    if sys.platform != "win32":
        return False
    with _install_lock:
        if _is_valid_carnac_executable(bundled_carnac_exe()):
            if verbose:
                print("A bundled Carnac executable is available.")
            return True
        if verbose:
            print("Carnac automatic download is disabled; install it locally or set CARNAC_EXE.")
        return False


def main() -> int:
    status = carnac_status()
    if status["status"] == "ready":
        print("Carnac is available on this computer.")
        return 0
    if sys.platform != "win32":
        print("Carnac is supported only on Windows.", file=sys.stderr)
        return 2
    print("Carnac is not installed. Install it locally or set CARNAC_EXE.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
