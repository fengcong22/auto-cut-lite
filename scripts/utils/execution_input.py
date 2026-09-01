"""Safe, structured external naming input for Auto-Cut jobs.

The Taskboard/Bridge layer may provide one human-facing artifact name for a
job.  This module deliberately treats that value as data only: it is parsed
from a small JSON envelope, normalized to one safe Windows path component,
and never interpolated into a shell command or prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


EXECUTION_INPUT_SCHEMA_VERSION = 1
EXECUTION_INPUT_FIELDS = frozenset({"schema_version", "artifact_name"})
MAX_EXTERNAL_NAME_CHARS = 1024
MAX_ARTIFACT_NAME_CHARS = 180

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ExecutionInputError(ValueError):
    """A safe, user-actionable external execution-input failure."""


@dataclass(frozen=True)
class NameResolution:
    """The requested name and the safe name used by the job."""

    requested_name: str
    final_name: str
    source: str
    sanitized: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_json_file(path: Path) -> Mapping[str, Any]:
    if path.is_symlink():
        raise ExecutionInputError("execution input must not be a symlink")
    if not path.is_file():
        raise ExecutionInputError(f"execution input is not a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExecutionInputError("execution input is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ExecutionInputError("execution input root must be an object")
    return payload


def load_execution_input(path: str | Path) -> tuple[dict[str, Any], str]:
    """Load and validate a Taskboard execution-input envelope.

    The returned digest is over canonical JSON, so insignificant whitespace
    changes do not invalidate a resumable job while a name change does.
    """

    raw_path = Path(path).expanduser()
    if raw_path.exists() and raw_path.is_symlink():
        raise ExecutionInputError("execution input must not be a symlink")
    candidate = raw_path.resolve(strict=False)
    payload = dict(_read_json_file(candidate))
    if set(payload) != EXECUTION_INPUT_FIELDS:
        raise ExecutionInputError(
            "execution input must contain exactly schema_version and artifact_name"
        )
    schema_version = payload.get("schema_version")
    if schema_version != EXECUTION_INPUT_SCHEMA_VERSION or isinstance(schema_version, bool):
        raise ExecutionInputError("execution input schema_version must be 1")
    artifact_name = payload.get("artifact_name")
    if not isinstance(artifact_name, str):
        raise ExecutionInputError("execution input artifact_name must be text")
    if not artifact_name.strip():
        raise ExecutionInputError("execution input artifact_name must not be empty")
    if len(artifact_name) > MAX_EXTERNAL_NAME_CHARS:
        raise ExecutionInputError(
            f"execution input artifact_name exceeds {MAX_EXTERNAL_NAME_CHARS} characters"
        )
    normalized = {"schema_version": EXECUTION_INPUT_SCHEMA_VERSION, "artifact_name": artifact_name}
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return normalized, hashlib.sha256(canonical).hexdigest()


def _safe_name(value: str) -> str:
    requested = str(value).strip()
    text = requested
    text = _INVALID_NAME_CHARS.sub("_", text)
    text = _WHITESPACE.sub(" ", text).strip(" .")
    while ".." in text:
        text = text.replace("..", "_")
    if text.casefold().endswith(".zip"):
        text = text[:-4].rstrip(" .")
    if len(text) > MAX_ARTIFACT_NAME_CHARS:
        text = text[:MAX_ARTIFACT_NAME_CHARS].rstrip(" .")
    if not text:
        return "_" if requested else ""
    stem = text.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        text = f"_{text}"
    return text


def resolve_artifact_name(
    *,
    external_name: str | None = None,
    project_name: str | None = None,
    document_title: str | None = None,
    fallback_name: str,
) -> NameResolution:
    """Choose the first non-empty name and normalize it safely.

    Priority is external input, document title, explicit project name, then
    the caller-provided identity fallback.  ``fallback_name`` is required so
    a missing/empty title can never turn into an unnamed draft.
    """

    candidates = (
        ("external_input", external_name),
        ("document_title", document_title),
        ("project_json", project_name),
        ("identity_fallback", fallback_name),
    )
    for source, candidate in candidates:
        raw = str(candidate or "").strip()
        if not raw:
            continue
        final = _safe_name(raw)
        if final:
            return NameResolution(
                requested_name=raw,
                final_name=final,
                source=source,
                sanitized=final != raw,
            )
    # The fallback is generated internally, but retain a defensive error if a
    # caller accidentally supplies an unusable value.
    raise ExecutionInputError("no usable artifact name or fallback was provided")


__all__ = [
    "EXECUTION_INPUT_FIELDS",
    "EXECUTION_INPUT_SCHEMA_VERSION",
    "ExecutionInputError",
    "MAX_ARTIFACT_NAME_CHARS",
    "MAX_EXTERNAL_NAME_CHARS",
    "NameResolution",
    "load_execution_input",
    "resolve_artifact_name",
]
