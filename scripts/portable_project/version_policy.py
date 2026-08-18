from __future__ import annotations

import re
from typing import Any

SUPPORTED_APP_IDENTITIES = frozenset({"jianyingpro", "capcut"})


def _version_key(raw_value: str) -> tuple[int, ...]:
    text = str(raw_value or "").strip()
    if not text or re.fullmatch(r"\d+(?:\.\d+)*", text) is None:
        return ()
    version = tuple(int(part) for part in text.split("."))
    while len(version) > 1 and version[-1] == 0:
        version = version[:-1]
    return version


def _base_payload(
    source_app: str,
    source_version: str,
    target_app: str,
    target_version: str,
) -> dict[str, Any]:
    return {
        "source_app": str(source_app or "").strip(),
        "target_app": str(target_app or "").strip(),
        "source_version": str(source_version or "").strip(),
        "target_version": str(target_version or "").strip(),
    }


def evaluate_version_policy(
    source_app: str,
    source_version: str,
    target_app: str,
    target_version: str,
    *,
    diagnostic_override: bool = False,
) -> dict[str, Any]:
    payload = _base_payload(source_app, source_version, target_app, target_version)
    source_app_key = payload["source_app"].casefold()
    target_app_key = payload["target_app"].casefold()
    source_key = _version_key(payload["source_version"])
    target_key = _version_key(payload["target_version"])

    if (
        source_app_key not in SUPPORTED_APP_IDENTITIES
        or target_app_key not in SUPPORTED_APP_IDENTITIES
    ):
        decision, code = "block", "unknown_app_identity"
    elif source_app_key != target_app_key:
        decision, code = "block", "target_app_mismatch"
    elif not source_key or not target_key:
        decision, code = "block", "unknown_app_version"
    elif source_key == target_key:
        decision, code = "allow", "same_version"
    elif target_key > source_key:
        decision, code = "allow_with_warning", "target_version_newer"
    else:
        decision, code = "block", "unsupported_target_version"

    if (
        decision == "block"
        and diagnostic_override
        and code
        in {
            "target_app_mismatch",
            "unknown_app_version",
        }
    ):
        decision = "allow_with_warning"
        code = f"diagnostic_override_{code}"
        payload["diagnostic_override"] = True

    return {"decision": decision, "code": code, **payload}
