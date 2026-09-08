"""Strict, provenance-preserving intake for phased Feishu Auto-Cut runs.

The Taskboard creates a small JSON manifest for one task/run/stage.  This module
is intentionally independent from the editing engine: it validates the
manifest, reads only the configured Feishu fields, and returns ordered local
materials with byte receipts.  In particular, it never scans a directory to
infer a source or a result.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

SOURCE_MANIFEST_SCHEMA_VERSION = 1
_STAGE_IDS = {"initial", "first_review", "final_review"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_VIDEO_SUFFIXES = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})
_AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"})
_LARK_HOST_SUFFIXES = ("feishu.cn", "larksuite.com", "larkoffice.com")
_ECMASCRIPT_TRIM_CHARS = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


class SourceManifestError(ValueError):
    """A sanitized manifest or source-material failure.

    ``code`` is stable and suitable for Taskboard blocking records.  Messages
    deliberately contain no provider tokens, local source paths, or document
    text beyond a configured anchor (which is useful for operator diagnosis).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(f"{self.code}: {message}")

    def public_data(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "details": _sanitize_public(self.details),
        }


@dataclass(frozen=True)
class LoadedSourceManifest:
    data: dict[str, Any]
    canonical_sha256: str
    path: str = ""


@dataclass(frozen=True)
class DocxSectionSelection:
    anchor_text: str
    blocks: tuple[dict[str, Any], ...]
    text_blocks: tuple[dict[str, Any], ...]
    attachments: tuple[dict[str, Any], ...]


def _sanitize_public(value: Any) -> Any:
    """Remove accidental sensitive values from diagnostic details."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            name = str(key).casefold()
            if any(token in name for token in ("token", "secret", "credential", "authorization")):
                continue
            if name in {"path", "video_path", "audio_path", "replacement_audio_path"}:
                # Paths are useful internally but are not safe in public errors.
                continue
            result[str(key)] = _sanitize_public(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_public(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _ecmascript_trim(value: str) -> str:
    """Match String.prototype.trim for manifest normalization."""

    return value.strip(_ECMASCRIPT_TRIM_CHARS)


def _ecmascript_number(value: float) -> str:
    """Render a finite float the same way JSON.stringify renders a number."""

    if not math.isfinite(value):
        raise ValueError("manifest contains invalid JSON values")
    if value == 0:
        return "0"
    text = repr(value)
    if "e" not in text:
        return text[:-2] if text.endswith(".0") else text
    mantissa, exponent_text = text.split("e", 1)
    exponent = int(exponent_text)
    if -6 <= exponent < 21:
        sign = ""
        if mantissa.startswith("-"):
            sign, mantissa = "-", mantissa[1:]
        decimal = mantissa.find(".")
        digits = mantissa.replace(".", "")
        point = (decimal if decimal >= 0 else len(mantissa)) + exponent
        if point <= 0:
            return f"{sign}0.{('0' * -point)}{digits}"
        if point >= len(digits):
            return f"{sign}{digits}{('0' * (point - len(digits)))}"
        return f"{sign}{digits[:point]}.{digits[point:]}"
    normalized_mantissa = mantissa[:-2] if mantissa.endswith(".0") else mantissa
    return f"{normalized_mantissa}e{'+' if exponent > 0 else ''}{exponent}"


def _ecmascript_string(value: str) -> str:
    """Render strings with JSON.stringify-compatible surrogate escaping."""

    text = json.dumps(value, ensure_ascii=False)
    result: list[str] = []
    index = 0
    while index < len(text):
        codepoint = ord(text[index])
        if 0xD800 <= codepoint <= 0xDBFF and index + 1 < len(text):
            next_codepoint = ord(text[index + 1])
            if 0xDC00 <= next_codepoint <= 0xDFFF:
                result.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + next_codepoint - 0xDC00))
                index += 2
                continue
        if 0xD800 <= codepoint <= 0xDFFF:
            result.append(f"\\u{codepoint:04x}")
        else:
            result.append(text[index])
        index += 1
    return "".join(result)


def _canonical_json_text(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _ecmascript_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _ecmascript_number(value)
    if isinstance(value, (list, tuple)):
        return f"[{','.join(_canonical_json_text(item) for item in value)}]"
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("manifest object keys must be strings")
        items = (
            f"{_ecmascript_string(key)}:{_canonical_json_text(value[key])}"
            for key in sorted(value)
        )
        return f"{{{','.join(items)}}}"
    raise TypeError("manifest contains unsupported JSON values")


def _canonical_json(value: Any) -> bytes:
    try:
        return _canonical_json_text(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SourceManifestError("source_manifest_invalid", "manifest contains invalid JSON values") from exc


def canonical_sha256(value: Any) -> str:
    """Hash canonical UTF-8 JSON (sorted object keys, original array order)."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or isinstance(value, (list, tuple)):
        raise SourceManifestError("source_manifest_invalid", f"{name} must be an object")
    return dict(value)


def _allowed(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = next((key for key in value if str(key) not in allowed), None)
    if unknown is not None:
        raise SourceManifestError(
            "source_manifest_field_unsupported",
            f"{name}.{unknown} is unsupported",
        )


def _text(value: Any, name: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str):
        raise SourceManifestError("source_manifest_invalid", f"{name} is invalid")
    result = _ecmascript_trim(value)
    if not result or "\x00" in result or len(result) > max_length:
        raise SourceManifestError("source_manifest_invalid", f"{name} is invalid")
    return result


def _identifier(value: Any, name: str) -> str:
    result = _text(value, name, max_length=256)
    if not _ID_RE.fullmatch(result):
        raise SourceManifestError("source_manifest_invalid", f"{name} is invalid")
    return result


def _url(value: Any) -> str:
    result = _text(value, "document.url", max_length=2048)
    parsed = urlsplit(result)
    host = str(parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.netloc
        or not any(host == suffix or host.endswith(f".{suffix}") for suffix in _LARK_HOST_SUFFIXES)
        or re.fullmatch(r"/(?:docx|wiki)/[A-Za-z0-9][A-Za-z0-9_-]*", parsed.path) is None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise SourceManifestError(
            "source_manifest_invalid",
            "document.url must be an HTTPS Feishu Docx or Wiki URL",
        )
    return result


def _reject_executable_keys(value: Any, name: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).casefold()
            if any(term in lowered for term in ("path", "command", "prompt", "shell", "executable")):
                raise SourceManifestError(
                    "source_manifest_field_unsupported",
                    f"{name}.{key} is unsupported",
                )
            _reject_executable_keys(child, f"{name}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_executable_keys(child, f"{name}[{index}]")


def _normalize_source(
    value: Any,
    name: str,
    *,
    allow_base: bool = True,
    record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = _object(value, name)
    _allowed(source, {"kind", "anchor_text", "field_id", "base_token", "table_id", "record_id"}, name)
    kind = _text(source.get("kind"), f"{name}.kind", max_length=64)
    if kind == "docx_section":
        return {"kind": kind, "anchor_text": _text(source.get("anchor_text"), f"{name}.anchor_text", max_length=512)}
    if kind != "base_attachment" or not allow_base:
        raise SourceManifestError("source_manifest_invalid", f"{name}.kind is invalid")
    identity = dict(record or {})
    values = {
        "base_token": source.get("base_token", identity.get("base_token")),
        "table_id": source.get("table_id", identity.get("table_id")),
        "record_id": source.get("record_id", identity.get("record_id")),
        "field_id": source.get("field_id"),
    }
    normalized = {
        "kind": kind,
        "base_token": _identifier(values["base_token"], f"{name}.base_token"),
        "table_id": _identifier(values["table_id"], f"{name}.table_id"),
        "record_id": _identifier(values["record_id"], f"{name}.record_id"),
        "field_id": _identifier(values["field_id"], f"{name}.field_id"),
    }
    return normalized


def _normalize_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    input_value = _object(value, "source manifest")
    # Check this before normalizing so a path/command cannot be hidden in an
    # unknown nested object.
    _reject_executable_keys(input_value)
    _allowed(input_value, {"schema_version", "binding", "record", "document", "sources"}, "manifest")
    if input_value.get("schema_version") != SOURCE_MANIFEST_SCHEMA_VERSION:
        raise SourceManifestError("source_manifest_schema_unsupported", "schema_version must be 1")

    binding = _object(input_value.get("binding"), "binding")
    _allowed(binding, {"task_id", "run_id", "subject_key", "config_version", "stage_id", "event_id"}, "binding")
    config_version = binding.get("config_version")
    if isinstance(config_version, bool) or not isinstance(config_version, int) or config_version < 1:
        raise SourceManifestError("source_manifest_invalid", "binding.config_version is invalid")
    stage_id = _text(binding.get("stage_id"), "binding.stage_id", max_length=64)
    if stage_id not in _STAGE_IDS:
        raise SourceManifestError("source_manifest_invalid", "binding.stage_id is invalid")
    subject_key = _text(binding.get("subject_key"), "binding.subject_key", max_length=513)
    if ":" not in subject_key:
        raise SourceManifestError("source_manifest_invalid", "binding.subject_key is invalid")
    normalized_binding = {
        "task_id": _identifier(binding.get("task_id"), "binding.task_id"),
        "run_id": _identifier(binding.get("run_id"), "binding.run_id"),
        "subject_key": subject_key,
        "config_version": config_version,
        "stage_id": stage_id,
        "event_id": _identifier(binding.get("event_id"), "binding.event_id"),
    }

    record_value: dict[str, Any] | None = None
    if "record" in input_value:
        record = _object(input_value["record"], "record")
        _allowed(record, {"base_token", "table_id", "record_id"}, "record")
        record_value = {
            "base_token": _identifier(record.get("base_token"), "record.base_token"),
            "table_id": _identifier(record.get("table_id"), "record.table_id"),
            "record_id": _identifier(record.get("record_id"), "record.record_id"),
        }

    document = _object(input_value.get("document"), "document")
    _allowed(document, {"field_id", "url"}, "document")
    normalized_document = {
        "field_id": _identifier(document.get("field_id"), "document.field_id"),
        "url": _url(document.get("url")),
    }

    sources = _object(input_value.get("sources"), "sources")
    _allowed(sources, {"video", "review", "audio"}, "sources")
    normalized_sources: dict[str, Any] = {
        "video": _normalize_source(sources.get("video"), "sources.video", record=record_value),
        "review": _normalize_source(sources.get("review"), "sources.review", allow_base=False),
    }
    audio = _object(sources.get("audio"), "sources.audio")
    _allowed(audio, {"mode", "duration_tolerance_seconds", "source"}, "sources.audio")
    mode = _text(audio.get("mode"), "sources.audio.mode", max_length=64)
    if mode == "video_original":
        if "source" in audio or "duration_tolerance_seconds" in audio:
            raise SourceManifestError("source_manifest_invalid", "video_original must not include an audio source")
        normalized_sources["audio"] = {"mode": mode}
    elif mode == "replace_original":
        if "source" not in audio:
            raise SourceManifestError("source_manifest_invalid", "replace_original requires an audio source")
        tolerance = audio.get("duration_tolerance_seconds", 3)
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, (int, float))
            or not math.isfinite(float(tolerance))
            or not float(tolerance) > 0
        ):
            raise SourceManifestError("source_manifest_invalid", "sources.audio.duration_tolerance_seconds must be positive")
        normalized_sources["audio"] = {
            "mode": mode,
            "duration_tolerance_seconds": float(tolerance) if isinstance(tolerance, float) else tolerance,
            "source": _normalize_source(audio.get("source"), "sources.audio.source", record=record_value),
        }
    else:
        raise SourceManifestError("source_manifest_invalid", "sources.audio.mode is invalid")

    result: dict[str, Any] = {
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "binding": normalized_binding,
        "document": normalized_document,
        "sources": normalized_sources,
    }
    if record_value is not None:
        result["record"] = record_value
    return result


def load_source_manifest(path: str | os.PathLike[str]) -> LoadedSourceManifest:
    """Load, normalize, and bind one manifest to the injected run context."""

    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
        raise SourceManifestError("source_manifest_path_invalid", "source manifest must be an absolute regular file")
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceManifestError("source_manifest_invalid", "source manifest is not valid UTF-8 JSON") from exc
    normalized = _normalize_manifest(raw)
    digest = canonical_sha256(normalized)
    binding = normalized["binding"]
    expected_bindings = {
        "task_id": os.environ.get("CODEX_AUTOCUT_TASK_ID"),
        "run_id": os.environ.get("CODEX_AUTOCUT_RUN_ID"),
        "subject_key": os.environ.get("CODEX_AUTOCUT_SUBJECT_KEY"),
        "config_version": os.environ.get("CODEX_AUTOCUT_CONFIG_VERSION"),
        "stage_id": os.environ.get("CODEX_AUTOCUT_STAGE_ID"),
        "event_id": os.environ.get("CODEX_AUTOCUT_EVENT_ID"),
    }
    for key, expected in expected_bindings.items():
        if expected is None or str(expected).strip() == "":
            raise SourceManifestError("source_manifest_binding_mismatch", f"missing {key} execution binding")
        expected_text = str(expected).strip()
        if key == "config_version":
            try:
                expected_text = str(int(expected_text))
            except ValueError as exc:
                raise SourceManifestError("source_manifest_binding_mismatch", "invalid config version execution binding") from exc
        if not hmac.compare_digest(str(binding[key]), expected_text):
            raise SourceManifestError("source_manifest_binding_mismatch", f"binding {key} does not match this run")
    expected_digest = os.environ.get("CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256")
    if expected_digest is None or not _SHA256_RE.fullmatch(str(expected_digest).strip()):
        raise SourceManifestError("source_manifest_digest_mismatch", "missing or invalid manifest digest execution binding")
    if not hmac.compare_digest(digest, str(expected_digest).strip().lower()):
        raise SourceManifestError("source_manifest_digest_mismatch", "manifest digest does not match this run")
    return LoadedSourceManifest(normalized, digest, str(candidate.resolve(strict=True)))


def _block_text(block: Mapping[str, Any]) -> str:
    for key in ("text", "source_text", "content", "label", "title"):
        value = block.get(key)
        if isinstance(value, str):
            return value
    return ""


def _block_kind(block: Mapping[str, Any]) -> str:
    return str(block.get("kind") or block.get("type") or "text").strip().casefold()


def _block_level(block: Mapping[str, Any]) -> int | None:
    raw = block.get("level", block.get("heading_level"))
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        kind = _block_kind(block)
        match = re.fullmatch(r"h([1-6])", kind)
        return int(match.group(1)) if match else None
    return value if value > 0 else None


def _is_heading(block: Mapping[str, Any]) -> bool:
    kind = _block_kind(block)
    return kind in {"heading", "title", "header"} or bool(re.fullmatch(r"h[1-6]", kind)) or _block_level(block) is not None and kind.startswith("heading")


def _is_attachment(block: Mapping[str, Any]) -> bool:
    kind = _block_kind(block)
    return kind in {"attachment", "asset", "image", "video", "audio", "source", "img"} or bool(block.get("filename") or block.get("file_name")) and bool(block.get("mime") or block.get("content_type") or block.get("token"))


def _normalized_blocks(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_blocks = document.get("blocks")
    if not isinstance(raw_blocks, list):
        raw_blocks = document.get("block_metadata")
    if not isinstance(raw_blocks, list):
        return []
    blocks: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, Mapping):
            continue
        block = dict(raw)
        block["_position"] = index
        # Some serializers put attachments inside a paragraph/heading block.
        nested = block.get("attachments")
        if isinstance(nested, list):
            for nested_index, attachment in enumerate(nested):
                if not isinstance(attachment, Mapping):
                    continue
                row = dict(attachment)
                row.setdefault("kind", "attachment")
                row["_position"] = (index, nested_index)
                blocks.append(row)
            block.pop("attachments", None)
        blocks.append(block)
    # Keep source order.  Tuple positions ensure nested attachments remain
    # adjacent to their containing block while a plain integer block follows.
    blocks.sort(key=lambda row: (row.get("_position") if isinstance(row.get("_position"), tuple) else (row.get("_position", 0), -1)))
    return blocks


def select_docx_section(
    document: Mapping[str, Any],
    anchor_text: str,
    configured_anchors: Sequence[str] | set[str],
) -> DocxSectionSelection:
    """Select one exact anchor range and preserve its document order."""

    anchor = str(anchor_text or "").strip()
    if not anchor:
        raise SourceManifestError("docx_anchor_missing", "configured Docx anchor is empty")
    anchors = {str(value).strip() for value in configured_anchors if str(value).strip()}
    anchors.add(anchor)
    blocks = _normalized_blocks(document)
    if not blocks:
        raise SourceManifestError("docx_anchor_missing", "the fetched document has no selectable blocks")
    matches = [index for index, block in enumerate(blocks) if _block_text(block).strip() == anchor and not _is_attachment(block)]
    if not matches:
        raise SourceManifestError("docx_anchor_missing", f"Docx anchor {anchor!r} was not found")
    if len(matches) > 1:
        raise SourceManifestError("docx_anchor_ambiguous", f"Docx anchor {anchor!r} matched more than once")
    start = matches[0]
    start_block = blocks[start]
    start_level = _block_level(start_block) if _is_heading(start_block) else None
    # For a plain label, discover its containing heading.  A subsequent heading
    # at that level or above closes the range.
    containing_level: int | None = None
    if start_level is None:
        for previous in reversed(blocks[:start]):
            if _is_heading(previous):
                containing_level = _block_level(previous)
                break
    boundary = len(blocks)
    for index in range(start + 1, len(blocks)):
        candidate = blocks[index]
        text = _block_text(candidate).strip()
        if text and text in anchors and not _is_attachment(candidate):
            boundary = index
            break
        if _is_heading(candidate):
            level = _block_level(candidate)
            if start_level is not None and level is not None and level <= start_level:
                boundary = index
                break
            if start_level is None and containing_level is not None and level is not None and level <= containing_level:
                boundary = index
                break
            if start_level is None and containing_level is None:
                boundary = index
                break
    selected = blocks[start + 1 : boundary]
    text_rows: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    for row in selected:
        if _is_attachment(row):
            attachments.append(dict(row))
        else:
            text = _block_text(row)
            if text.strip():
                copied = dict(row)
                copied.setdefault("source_text", text)
                text_rows.append(copied)
    return DocxSectionSelection(
        anchor_text=anchor,
        blocks=tuple(dict(row) for row in selected),
        text_blocks=tuple(text_rows),
        attachments=tuple(attachments),
    )


def _review_items_from_section(selection: DocxSectionSelection) -> list[dict[str, Any]]:
    rows = [dict(row) for row in selection.text_blocks]
    checkbox_rows = [row for row in rows if _block_kind(row) == "checkbox"]
    selected = checkbox_rows or [row for row in rows if not _is_heading(row)]
    for row in selected:
        if _block_kind(row) in {"checkbox", "text"}:
            row.pop("kind", None)
            row.pop("type", None)
    return selected


def _safe_filename(name: Any, fallback: str) -> str:
    raw = str(name or "").strip().replace("\\", "/").split("/")[-1]
    raw = re.sub(r"[\x00-\x1f\x7f]", "_", raw).strip(" .")
    return raw or fallback


def _extension(name: str, mime: str) -> str:
    suffix = Path(name).suffix.casefold()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return suffix
    guessed = mimetypes.guess_extension(str(mime).split(";", 1)[0].strip()) or ".bin"
    return guessed.casefold()


def _classify(mime: Any, name: Any) -> str | None:
    mime_text = str(mime or "").split(";", 1)[0].strip().casefold()
    if mime_text.startswith("video/"):
        return "video"
    if mime_text.startswith("audio/"):
        return "audio"
    suffix = Path(str(name or "")).suffix.casefold()
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    if suffix in _AUDIO_SUFFIXES:
        return "audio"
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_runner(
    command: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _lark_prefix(lark_cli: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    try:
        # Reuse the maintained shim/Node resolution used by URL intake so a
        # Windows .cmd installation is never invoked as an opaque executable.
        from utils.review_document_intake import _lark_executable

        return tuple(_lark_executable(lark_cli))
    except Exception as exc:
        if isinstance(exc, SourceManifestError):
            raise
        raise SourceManifestError("lark_cli_unavailable", "lark-cli is unavailable") from exc


def _json_command(
    command: Sequence[str],
    *,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None,
    code: str,
    message: str,
) -> dict[str, Any]:
    completed = (runner or _command_runner)(command)
    if completed.returncode != 0:
        raise SourceManifestError(code, message, details={"provider_exit_code": int(completed.returncode)})
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SourceManifestError(code, message) from exc
    if not isinstance(payload, Mapping):
        raise SourceManifestError(code, message)
    return dict(payload)


def _require_user(
    executable: Sequence[str],
    *,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None,
) -> dict[str, Any]:
    payload = _json_command(
        [*executable, "whoami"],
        runner=runner,
        code="lark_user_identity_unavailable",
        message="the current Feishu/Lark user identity is unavailable",
    )
    if payload.get("available") is not True or str(payload.get("identity") or "").casefold() != "user" or str(payload.get("defaultAs") or payload.get("default_as") or "").casefold() != "user":
        raise SourceManifestError("lark_user_identity_unavailable", "Feishu/Lark must use the current user identity")
    return payload


def _extract_field_attachments(payload: Mapping[str, Any], field_id: str) -> list[dict[str, Any]]:
    candidates: Any = None
    data = payload.get("data")
    if isinstance(data, Mapping):
        record = data.get("record")
        if isinstance(record, Mapping):
            fields = record.get("fields")
            if isinstance(fields, Mapping):
                candidates = fields.get(field_id)
        if candidates is None:
            fields = data.get("fields")
            if isinstance(fields, Mapping):
                candidates = fields.get(field_id)
    if candidates is None:
        record = payload.get("record")
        if isinstance(record, Mapping) and isinstance(record.get("fields"), Mapping):
            candidates = record["fields"].get(field_id)
    if isinstance(candidates, Mapping):
        candidates = [candidates]
    if not isinstance(candidates, list):
        return []
    rows: list[dict[str, Any]] = []
    for raw in candidates:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        token = str(row.get("file_token") or row.get("fileToken") or row.get("token") or row.get("id") or "").strip()
        if not token:
            continue
        row["file_token"] = token
        row["name"] = str(row.get("name") or row.get("file_name") or row.get("fileName") or "").strip()
        row["mime"] = str(row.get("mime") or row.get("mime_type") or row.get("mimeType") or row.get("content_type") or "").strip().casefold()
        rows.append(row)
    return rows


def _download_lark_file(
    command: Sequence[str],
    target: Path,
    *,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None,
    code: str,
    message: str,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    destination_temporary = target.parent / f".{target.name}.{os.getpid()}.part"
    destination_temporary.unlink(missing_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="auto-cut-lark-download-") as staging_name:
            staging_root = Path(staging_name)
            staged = staging_root / target.name
            if runner is None:
                completed = _command_runner(
                    [*command, "--output", f"./{target.name}"],
                    cwd=staging_root,
                )
            else:
                completed = runner([*command, "--output", str(staged)])
            if completed.returncode != 0 or not staged.is_file() or staged.stat().st_size <= 0:
                raise SourceManifestError(code, message)
            shutil.copyfile(staged, destination_temporary)
        os.replace(destination_temporary, target)
    except Exception:
        destination_temporary.unlink(missing_ok=True)
        raise


def fetch_base_attachment_source(
    manifest: LoadedSourceManifest | Mapping[str, Any],
    source: Mapping[str, Any],
    destination: str | os.PathLike[str],
    command_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    *,
    lark_cli: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Read one configured Base attachment field and download exactly one file."""

    data = manifest.data if isinstance(manifest, LoadedSourceManifest) else _normalize_manifest(manifest)
    binding = data.get("record") or {}
    descriptor = dict(source)
    for key in ("base_token", "table_id", "record_id"):
        descriptor.setdefault(key, binding.get(key))
    base_token = _identifier(descriptor.get("base_token"), "source.base_token")
    table_id = _identifier(descriptor.get("table_id"), "source.table_id")
    record_id = _identifier(descriptor.get("record_id"), "source.record_id")
    field_id = _identifier(descriptor.get("field_id"), "source.field_id")
    executable = _lark_prefix(lark_cli)
    _require_user(executable, runner=command_runner)
    payload = _json_command(
        [*executable, "base", "+record-get", "--base-token", base_token, "--table-id", table_id, "--record-id", record_id, "--field-id", field_id, "--format", "json"],
        runner=command_runner,
        code="base_attachment_read_failed",
        message="the configured Base attachment field could not be read",
    )
    rows = _extract_field_attachments(payload, field_id)
    if len(rows) != 1:
        raise SourceManifestError("base_attachment_count_mismatch", "the configured Base field must contain exactly one attachment")
    row = rows[0]
    target_root = Path(destination).expanduser().resolve(strict=False)
    target_root.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(row.get("name"), f"{field_id}.bin")
    target = target_root / filename
    stem, suffix = target.stem, target.suffix
    counter = 2
    while target.exists():
        target = target_root / f"{stem}_{counter}{suffix}"
        counter += 1
    _require_user(executable, runner=command_runner)
    _download_lark_file(
        [*executable, "base", "+record-download-attachment", "--base-token", base_token, "--table-id", table_id, "--record-id", record_id, "--file-token", row["file_token"], "--format", "json"],
        target,
        runner=command_runner,
        code="base_attachment_download_failed",
        message="the configured Base attachment could not be downloaded",
    )
    receipt = {
        "path": str(target.resolve()),
        "filename": target.name,
        "mime": row.get("mime") or mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        "extension": _extension(target.name, str(row.get("mime") or "")),
        "byte_size": target.stat().st_size,
        "sha256": _sha256_file(target),
        "field_id": field_id,
        "token_sha256": hashlib.sha256(row["file_token"].encode("utf-8")).hexdigest(),
    }
    return [receipt]


def _download_docx_attachment(
    attachment: Mapping[str, Any],
    destination: Path,
    *,
    executable: Sequence[str],
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None,
) -> dict[str, Any]:
    token = str(attachment.get("token") or attachment.get("file_token") or attachment.get("fileToken") or "").strip()
    if not token:
        raise SourceManifestError("docx_attachment_invalid", "a selected Docx attachment has no downloadable token")
    filename = _safe_filename(attachment.get("filename") or attachment.get("name") or attachment.get("file_name"), "attachment.bin")
    target = destination / filename
    stem, suffix = target.stem, target.suffix
    counter = 2
    while target.exists():
        target = destination / f"{stem}_{counter}{suffix}"
        counter += 1
    _require_user(executable, runner=runner)
    _download_lark_file(
        [*executable, "docs", "+media-download", "--token", token, "--as", "user"],
        target,
        runner=runner,
        code="docx_attachment_download_failed",
        message="a selected Docx attachment could not be downloaded",
    )
    mime = str(attachment.get("mime") or attachment.get("content_type") or "").casefold()
    return {
        "path": str(target.resolve()),
        "filename": target.name,
        "mime": mime or mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        "extension": _extension(target.name, mime),
        "byte_size": target.stat().st_size,
        "sha256": _sha256_file(target),
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    }


def _fetch_and_parse_document(
    url: str,
    *,
    executable: Sequence[str],
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None,
) -> dict[str, Any]:
    _require_user(executable, runner=runner)
    payload = _json_command(
        [*executable, "docs", "+fetch", "--doc", url, "--scope", "full", "--detail", "full", "--doc-format", "xml", "--format", "json", "--as", "user"],
        runner=runner,
        code="document_fetch_failed",
        message="the configured Feishu Docx could not be read",
    )
    if payload.get("ok") is not True or str(payload.get("identity") or "").casefold() != "user":
        raise SourceManifestError("document_fetch_failed", "the Feishu Docx read did not use the current user identity")
    document = payload.get("data", {}).get("document") if isinstance(payload.get("data"), Mapping) else None
    if not isinstance(document, Mapping):
        raise SourceManifestError("document_fetch_failed", "the Feishu Docx response was incomplete")
    try:
        from utils.review_document_intake import parse_lark_document

        return parse_lark_document({
            "content": document.get("content"),
            "document_id": document.get("document_id"),
            "revision_id": document.get("revision_id"),
            "title": document.get("title") or document.get("document_title") or document.get("name") or "",
        }, require_review_items=False)
    except SourceManifestError:
        raise
    except Exception as exc:
        raise SourceManifestError("document_fetch_failed", "the Feishu Docx could not be parsed") from exc


def materialize_manifest_sources(
    manifest: LoadedSourceManifest | Mapping[str, Any],
    job_root: str | os.PathLike[str],
    command_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    *,
    lark_cli: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Fetch only configured ranges/fields and return ordered local receipts."""

    loaded = manifest if isinstance(manifest, LoadedSourceManifest) else LoadedSourceManifest(_normalize_manifest(manifest), canonical_sha256(_normalize_manifest(manifest)))
    data = loaded.data
    root = Path(job_root).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    materials = root / "manifest-materials"
    materials.mkdir(parents=True, exist_ok=True)
    executable = _lark_prefix(lark_cli)
    parsed = _fetch_and_parse_document(data["document"]["url"], executable=executable, runner=command_runner)
    source_descriptors = data["sources"]
    configured_anchors: set[str] = set()
    for descriptor in source_descriptors.values():
        if isinstance(descriptor, Mapping) and descriptor.get("kind") == "docx_section":
            configured_anchors.add(str(descriptor.get("anchor_text") or "").strip())
        if isinstance(descriptor, Mapping) and isinstance(descriptor.get("source"), Mapping):
            nested = descriptor["source"]
            if nested.get("kind") == "docx_section":
                configured_anchors.add(str(nested.get("anchor_text") or "").strip())
    configured_anchors = {str(value).strip() for value in configured_anchors if str(value).strip()}

    def materialize_descriptor(descriptor: Mapping[str, Any], label: str) -> list[dict[str, Any]]:
        if descriptor.get("kind") == "base_attachment":
            return fetch_base_attachment_source(loaded, descriptor, materials / label, command_runner, lark_cli=lark_cli)
        selection = select_docx_section(parsed, str(descriptor.get("anchor_text") or ""), configured_anchors)
        rows: list[dict[str, Any]] = []
        for attachment in selection.attachments:
            rows.append(_download_docx_attachment(attachment, materials / label, executable=executable, runner=command_runner))
        return rows

    video_rows = materialize_descriptor(source_descriptors["video"], "video")
    video_paths: list[str] = []
    videos: list[dict[str, Any]] = []
    for row in video_rows:
        kind = _classify(row.get("mime"), row.get("filename"))
        if kind != "video":
            continue
        videos.append(row)
        video_paths.append(str(row["path"]))
    if not videos:
        raise SourceManifestError("video_source_invalid", "configured video source contains no video attachment")

    review_descriptor = source_descriptors["review"]
    review_selection = select_docx_section(parsed, str(review_descriptor.get("anchor_text") or ""), configured_anchors)
    review_items = _review_items_from_section(review_selection)
    if not review_items:
        raise SourceManifestError("review_source_empty", "configured review source contains no meaningful text")

    audio_rows: list[dict[str, Any]] = []
    audio_paths: list[str] = []
    audio_config = source_descriptors["audio"]
    if audio_config.get("mode") == "replace_original":
        audio_rows = materialize_descriptor(audio_config["source"], "audio")
        classified_audio_rows: list[dict[str, Any]] = []
        for row in audio_rows:
            kind = _classify(row.get("mime"), row.get("filename"))
            if kind != "audio":
                continue
            classified_audio_rows.append(row)
            audio_paths.append(str(row["path"]))
        audio_rows = classified_audio_rows
        if not audio_rows:
            raise SourceManifestError("audio_source_invalid", "configured audio source contains no audio attachment")

    return {
        "document": {
            "field_id": data["document"]["field_id"],
            "url": data["document"]["url"],
            "document_identity_sha256": parsed.get("document_identity_sha256", ""),
            "revision_id": parsed.get("revision_id"),
            "content_sha256": parsed.get("content_sha256", ""),
        },
        "review_items": review_items,
        "videos": videos,
        "audios": audio_rows,
        "video_paths": video_paths,
        "audio_paths": audio_paths,
        "receipts": [*videos, *audio_rows],
        "manifest_sha256": loaded.canonical_sha256,
    }


def _duration_from_probe(value: Any) -> float | None:
    if isinstance(value, Mapping):
        for key in ("duration_seconds", "duration", "format_duration"):
            if key in value:
                value = value[key]
                break
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def compile_manifest_project(
    videos: Sequence[Mapping[str, Any]],
    audios: Sequence[Mapping[str, Any]],
    mode: str,
    tolerance_seconds: float = 3.0,
) -> dict[str, Any]:
    """Pair media strictly by supplied/document order."""

    normalized_mode = str(mode or "").strip().casefold()
    if normalized_mode not in {"video_original", "replace_original"}:
        raise SourceManifestError("audio_mode_invalid", "audio mode is invalid")
    try:
        tolerance = float(tolerance_seconds)
    except (TypeError, ValueError) as exc:
        raise SourceManifestError("media_duration_mismatch", "duration tolerance is invalid") from exc
    if not math.isfinite(tolerance) or tolerance < 0:
        raise SourceManifestError("media_duration_mismatch", "duration tolerance is invalid")
    video_rows = [dict(row) for row in videos]
    audio_rows = [dict(row) for row in audios]
    if not video_rows:
        raise SourceManifestError("video_source_invalid", "no video sources were supplied")
    if normalized_mode == "replace_original":
        if not audio_rows or len(video_rows) != len(audio_rows):
            raise SourceManifestError("media_count_mismatch", "video and audio source counts must match")
    pairs: list[dict[str, Any]] = []
    for index, video in enumerate(video_rows):
        video_path = str(video.get("path") or "").strip()
        video_sha = str(video.get("sha256") or "").strip().lower()
        if not video_path or not _SHA256_RE.fullmatch(video_sha):
            raise SourceManifestError("video_source_invalid", f"video pair {index} is invalid")
        row: dict[str, Any] = {
            "pair_index": index,
            "video_path": video_path,
            "video_sha256": video_sha,
        }
        if normalized_mode == "video_original":
            row["audio_mode"] = "video_original"
        else:
            audio = audio_rows[index]
            audio_path = str(audio.get("path") or "").strip()
            audio_sha = str(audio.get("sha256") or "").strip().lower()
            if not audio_path or not _SHA256_RE.fullmatch(audio_sha):
                raise SourceManifestError("audio_source_invalid", f"audio pair {index} is invalid")
            row.update({"audio_mode": "replace_original", "replacement_audio_path": audio_path, "replacement_audio_sha256": audio_sha})
        # Retain measured/declared values for the first validation pass; no
        # sorting or inference is performed.
        if video.get("duration_seconds") is not None:
            row["video_duration_seconds"] = float(video["duration_seconds"])
        if normalized_mode == "replace_original" and audio_rows[index].get("duration_seconds") is not None:
            row["audio_duration_seconds"] = float(audio_rows[index]["duration_seconds"])
        pairs.append(row)
    return {
        "workflow_mode": "lite",
        "source_pairs": pairs,
        "audio_mode": normalized_mode,
        "duration_tolerance_seconds": tolerance,
    }


def validate_source_pairs(
    project: Mapping[str, Any],
    tolerance_seconds: float,
    ffprobe: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Validate pair cardinality and duration without changing pair order."""

    pairs = project.get("source_pairs")
    if not isinstance(pairs, list) or not pairs:
        raise SourceManifestError("media_count_mismatch", "project has no source pairs")
    mode = str(project.get("audio_mode") or "").strip().casefold()
    if mode not in {"video_original", "replace_original"}:
        mode = "replace_original" if any(isinstance(row, Mapping) and row.get("replacement_audio_path") for row in pairs) else "video_original"
    if mode == "replace_original" and any(not isinstance(row, Mapping) or not row.get("replacement_audio_path") for row in pairs):
        raise SourceManifestError("media_count_mismatch", "every video pair requires replacement audio")
    try:
        tolerance = float(tolerance_seconds)
    except (TypeError, ValueError) as exc:
        raise SourceManifestError("media_duration_mismatch", "duration tolerance is invalid") from exc
    if tolerance < 0:
        raise SourceManifestError("media_duration_mismatch", "duration tolerance is invalid")
    result = json.loads(json.dumps(dict(project), ensure_ascii=False))
    result_pairs = result["source_pairs"]
    for index, pair in enumerate(result_pairs):
        if not isinstance(pair, Mapping):
            raise SourceManifestError("media_count_mismatch", f"source pair {index} is invalid")
        video_duration = _duration_from_probe(ffprobe(str(pair.get("video_path") or ""))) if ffprobe is not None else _duration_from_probe(pair.get("video_duration_seconds"))
        if video_duration is not None:
            pair["video_duration_seconds"] = video_duration
        if mode == "replace_original":
            audio_path = str(pair.get("replacement_audio_path") or "")
            audio_duration = _duration_from_probe(ffprobe(audio_path)) if ffprobe is not None else _duration_from_probe(pair.get("audio_duration_seconds"))
            if audio_duration is not None:
                pair["audio_duration_seconds"] = audio_duration
            if video_duration is not None and audio_duration is not None and abs(video_duration - audio_duration) > tolerance:
                raise SourceManifestError(
                    "media_duration_mismatch",
                    f"source pair {index} duration differs beyond tolerance",
                    details={"pair_index": index, "video_duration_seconds": round(video_duration, 6), "audio_duration_seconds": round(audio_duration, 6)},
                )
    result["duration_tolerance_seconds"] = tolerance
    return result


__all__ = [
    "SOURCE_MANIFEST_SCHEMA_VERSION",
    "SourceManifestError",
    "LoadedSourceManifest",
    "DocxSectionSelection",
    "canonical_sha256",
    "load_source_manifest",
    "select_docx_section",
    "fetch_base_attachment_source",
    "materialize_manifest_sources",
    "compile_manifest_project",
    "validate_source_pairs",
]
