"""Privacy-safe Feishu/Lark URL intake for the maintained Lite runner."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from utils.execution_input import resolve_artifact_name

INTAKE_SCHEMA_VERSION = 1
READINESS_SCHEMA_VERSION = 1
LARK_ADAPTER_VERSION = "auto-cut-lite-lark-document-v2"

_MAX_DOCUMENT_XML_BYTES = 16 * 1024 * 1024
_MAX_DOCUMENT_XML_ELEMENTS = 50_000
_MAX_DOCUMENT_XML_DEPTH = 64
_MAX_DOCUMENT_XML_ATTRIBUTES = 64
_MAX_DOCUMENT_XML_ATTRIBUTE_CHARS = 64 * 1024
_MAX_DOCUMENT_XML_TEXT_CHARS = 8 * 1024 * 1024
_MAX_ASSET_DOWNLOAD_BYTES = 8 * 1024 * 1024 * 1024
_MAX_TOTAL_ASSET_DOWNLOAD_BYTES = 16 * 1024 * 1024 * 1024
_LARK_HOST_SUFFIXES = ("feishu.cn", "larksuite.com", "larkoffice.com")
_VIDEO_SUFFIXES = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})
_AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"})
_IMAGE_SUFFIXES = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})
_SOURCE_VIDEO_CUES = (
    "录屏",
    "源视频",
    "原视频",
    "原始视频",
    "待剪",
    "待修改",
    "source",
)
_VISUAL_NAME_CUES = (
    "小手",
    "手指",
    "箭头",
    "指针",
    "pointer",
    "hand",
    "arrow",
)
_IMAGE_REPLACEMENT_RELATION_RE = re.compile(
    r"(?:"
    r"(?:用|使用|采用|拿|将|把)?(?:下方|下图|此|这张|该|右侧|右边|左侧|左边)?"
    r"(?:图片|图像|图|公式|图标|标签).{0,16}?(?:替换|换成|替换为|改为|更换)"
    r"|(?:替换|换成|替换为|更换|改为).{0,16}?"
    r"(?:下方|下图|此|这张|该|右侧|右边|左侧|左边)?(?:图片|图像|图|公式|图标|标签)"
    r"|(?:图片|图像|图|公式|图标|标签)\s*[/／]\s*"
    r"(?:图片|图像|图|公式|图标|标签)\s*(?:替换|更换)"
    r"|(?:use).{0,16}?(?:image|picture).{0,16}?(?:replace|swap|substitute)"
    r"|(?:replace|swap|substitute).{0,24}?(?:image|picture|formula|icon|label)"
    r")",
    re.IGNORECASE,
)
_VISUAL_RECOMMENDATION_RE = re.compile(
    r"(?:建议|推荐|优先|请选|首选|preferred|recommend(?:ed)?)"
    r".{0,12}?"
    r"(?:选择|选用|采用|使用|选|use|select|choose)",
    re.IGNORECASE,
)
_VISUAL_RECOMMENDATION_VALUE_RE = re.compile(
    r"(?:建议|推荐|优先|首选|preferred|recommend(?:ed)?)"
    r".{0,12}?"
    r"(?:此项|这一项|该项|这个|此素材|该素材|this|it)",
    re.IGNORECASE,
)
_VISUAL_RECOMMENDED_CANDIDATE_RE = re.compile(
    r"候选\s*(\d+)\s*[:：]?[^\n。！？；]{0,80}?"
    r"(?:建议|推荐|优先|首选|preferred|recommend(?:ed)?)",
    re.IGNORECASE,
)
_SENSITIVE_IDENTITY_KEYS = (
    "token",
    "secret",
    "credential",
    "authorization",
    "url",
    "code",
)
_SENSITIVE_SNAPSHOT_FIELDS = frozenset(
    {
        "access_token",
        "app_secret",
        "asset_token",
        "asset_url",
        "authorization_url",
        "credential",
        "credentials",
        "doc_token",
        "document_id",
        "document_token",
        "document_url",
        "download_url",
        "file_token",
        "media_token",
        "refresh_token",
        "signed_url",
        "temporary_url",
        "token",
        "url",
    }
)
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)


class ReviewDocumentIntakeError(RuntimeError):
    """A sanitized intake failure suitable for a machine-readable response."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        user_action: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.user_action = dict(user_action or {})
        self.details = dict(details or {})

    def public_data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.user_action:
            payload["user_action_required"] = self.user_action
        if self.details:
            payload["details"] = self.details
        return payload


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_document_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ReviewDocumentIntakeError(
            "invalid_document_url", "doc_url contains unsupported control characters"
        )
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ReviewDocumentIntakeError(
            "invalid_document_url", "doc_url contains an invalid port"
        ) from exc
    host = str(parsed.hostname or "").rstrip(".").casefold()
    trusted_host = any(host == suffix or host.endswith(f".{suffix}") for suffix in _LARK_HOST_SUFFIXES)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not trusted_host
    ):
        raise ReviewDocumentIntakeError(
            "invalid_document_url", "doc_url must be an absolute HTTPS Feishu/Lark URL"
        )
    route_parts = {part.casefold() for part in parsed.path.split("/") if part}
    if not route_parts.intersection({"docx", "wiki"}):
        raise ReviewDocumentIntakeError(
            "invalid_document_url", "doc_url must identify a /docx/ or /wiki/ document"
        )
    return value


def document_url_digest(raw_url: str) -> str:
    value = validate_document_url(raw_url)
    parsed = urlsplit(value)
    canonical = urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/"),
            parsed.query,
            parsed.fragment,
        )
    )
    return _sha256_text(canonical)


def sanitize_document_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Remove provider secrets from a compatibility snapshot before job persistence."""

    identity_values: list[str] = []
    document = snapshot.get("document")
    if isinstance(document, Mapping):
        for key, value in document.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _SENSITIVE_SNAPSHOT_FIELDS and str(value or "").strip():
                identity_values.append(str(value).strip())

    def visit(value: Any) -> Any:
        if isinstance(value, Mapping):
            cleaned: dict[str, Any] = {}
            for key, child in value.items():
                normalized = str(key).strip().casefold().replace("-", "_")
                if normalized in _SENSITIVE_SNAPSHOT_FIELDS:
                    continue
                cleaned[str(key)] = visit(child)
            return cleaned
        if isinstance(value, list):
            return [visit(child) for child in value]
        if isinstance(value, tuple):
            return [visit(child) for child in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    sanitized = visit(snapshot)
    if not isinstance(sanitized, dict):
        raise ReviewDocumentIntakeError(
            "document_snapshot_invalid", "The compatibility document snapshot is invalid"
        )
    sanitized_document = sanitized.get("document")
    if not isinstance(sanitized_document, dict):
        sanitized_document = {}
        sanitized["document"] = sanitized_document
    sanitized_document.pop("id", None)
    existing_digest = str(sanitized_document.get("document_identity_sha256") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", existing_digest):
        identity_payload = identity_values or [
            str(document.get("id") or "") if isinstance(document, Mapping) else "",
            str(document.get("revision") or "") if isinstance(document, Mapping) else "",
        ]
        sanitized_document["document_identity_sha256"] = _sha256_bytes(
            _canonical_json(identity_payload)
        )
    return sanitized


def _node_executable(shim_directory: Path) -> Path:
    adjacent = shim_directory / ("node.exe" if os.name == "nt" else "node")
    if adjacent.is_file():
        return adjacent.resolve(strict=True)
    discovered = shutil.which("node.exe") or shutil.which("node")
    if not discovered:
        raise ReviewDocumentIntakeError(
            "lark_cli_unavailable", "The Node.js runtime required by lark-cli is unavailable"
        )
    return Path(discovered).resolve(strict=True)


def _lark_command_prefix(
    explicit: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    if explicit is not None:
        candidate = Path(explicit).expanduser().resolve(strict=True)
        if not candidate.is_file():
            raise ReviewDocumentIntakeError(
                "lark_cli_unavailable", "The configured lark-cli executable is unavailable"
            )
    else:
        executable = shutil.which("lark-cli.cmd") or shutil.which("lark-cli")
        if not executable:
            raise ReviewDocumentIntakeError(
                "lark_cli_unavailable",
                "lark-cli is not installed",
                user_action={
                    "action_code": "authorization",
                    "reason_code": "lark_cli_unavailable",
                },
            )
        candidate = Path(executable).resolve(strict=True)

    suffix = candidate.suffix.casefold()
    if suffix == ".exe":
        return (str(candidate),)
    if suffix == ".js":
        return (str(_node_executable(candidate.parent)), str(candidate))
    if suffix in {".cmd", ".bat", ".ps1"} or (os.name == "nt" and not suffix):
        script = candidate.parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
        if not script.is_file():
            raise ReviewDocumentIntakeError(
                "lark_cli_unavailable",
                "The lark-cli Windows shim does not have a verified JavaScript entrypoint",
            )
        return (str(_node_executable(candidate.parent)), str(script.resolve(strict=True)))
    if os.name != "nt" and os.access(candidate, os.X_OK):
        return (str(candidate),)
    raise ReviewDocumentIntakeError(
        "lark_cli_unavailable",
        "The configured lark-cli executable type is not supported safely",
    )


def _lark_executable(explicit: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    try:
        return _lark_command_prefix(explicit)
    except (FileNotFoundError, OSError) as exc:
        raise ReviewDocumentIntakeError(
            "lark_cli_unavailable",
            "The configured lark-cli executable is unavailable",
            user_action={
                "action_code": "authorization",
                "reason_code": "lark_cli_unavailable",
            },
        ) from exc


def _default_command_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _run_json_command(
    command: Sequence[str],
    *,
    runner: CommandRunner | None,
    failure_code: str,
    failure_message: str,
) -> dict[str, Any]:
    completed = (runner or _default_command_runner)(command)
    if completed.returncode != 0:
        raise ReviewDocumentIntakeError(
            failure_code,
            failure_message,
            user_action={
                "action_code": "authorization",
                "reason_code": failure_code,
            },
            details={"provider_exit_code": int(completed.returncode)},
        )
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReviewDocumentIntakeError(
            failure_code,
            f"{failure_message}; the provider response was not valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise ReviewDocumentIntakeError(
            failure_code, f"{failure_message}; the provider response was not an object"
        )
    return payload


def lark_cli_version(
    *,
    lark_cli: str | os.PathLike[str] | None = None,
    runner: CommandRunner | None = None,
) -> str:
    executable = _lark_executable(lark_cli)
    completed = (runner or _default_command_runner)([*executable, "--version"])
    if completed.returncode != 0:
        raise ReviewDocumentIntakeError(
            "lark_cli_unavailable", "lark-cli version detection failed"
        )
    match = re.search(r"\bversion\s+([^\s]+)", completed.stdout, flags=re.IGNORECASE)
    if not match:
        raise ReviewDocumentIntakeError(
            "lark_cli_unavailable", "lark-cli returned an unsupported version response"
        )
    return match.group(1)


def lark_whoami(
    *,
    lark_cli: str | os.PathLike[str] | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    executable = _lark_executable(lark_cli)
    payload = _run_json_command(
        [*executable, "whoami"],
        runner=runner,
        failure_code="lark_user_identity_unavailable",
        failure_message="The current Feishu/Lark user identity is unavailable",
    )
    identity = str(payload.get("identity") or "").strip().casefold()
    default_as = str(payload.get("defaultAs") or payload.get("default_as") or "").strip().casefold()
    if payload.get("available") is not True or identity != "user" or default_as != "user":
        raise ReviewDocumentIntakeError(
            "lark_user_identity_unavailable",
            "Feishu/Lark must be configured for the current user with strict user identity",
            user_action={
                "action_code": "authorization",
                "reason_code": "lark_user_identity_unavailable",
            },
        )
    return payload


def fetch_lark_document(
    doc_url: str,
    *,
    lark_cli: str | os.PathLike[str] | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    value = validate_document_url(doc_url)
    executable = _lark_executable(lark_cli)
    payload = _run_json_command(
        [
            *executable,
            "docs",
            "+fetch",
            "--doc",
            value,
            "--scope",
            "full",
            "--detail",
            "full",
            "--doc-format",
            "xml",
            "--format",
            "json",
            "--as",
            "user",
        ],
        runner=runner,
        failure_code="document_fetch_failed",
        failure_message="The Feishu/Lark document could not be read as the current user",
    )
    if payload.get("ok") is not True or str(payload.get("identity") or "").casefold() != "user":
        raise ReviewDocumentIntakeError(
            "document_fetch_failed",
            "The Feishu/Lark document read did not use the current user identity",
            user_action={
                "action_code": "authorization",
                "reason_code": "document_fetch_failed",
            },
        )
    data = payload.get("data")
    document = data.get("document") if isinstance(data, Mapping) else None
    if not isinstance(document, Mapping):
        raise ReviewDocumentIntakeError(
            "document_fetch_failed", "The Feishu/Lark response did not contain a document"
        )
    content = document.get("content")
    document_id = document.get("document_id")
    revision_id = document.get("revision_id")
    document_title = document.get("title")
    if not isinstance(document_title, str):
        document_title = document.get("document_title")
    if not isinstance(document_title, str):
        document_title = document.get("name")
    if not isinstance(document_title, str):
        document_title = ""
    if (
        not isinstance(content, str)
        or not content.strip()
        or not isinstance(document_id, str)
        or not document_id.strip()
        or isinstance(revision_id, bool)
        or not isinstance(revision_id, (int, str))
    ):
        raise ReviewDocumentIntakeError(
            "document_fetch_failed", "The Feishu/Lark document response was incomplete"
        )
    return {
        "content": content,
        "document_id": document_id,
        "revision_id": revision_id,
        "title": document_title.strip(),
        "identity": "user",
    }


def _safe_extension(name: str, mime: str) -> str:
    suffix = Path(str(name or "")).suffix.casefold()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = str(mimetypes.guess_extension(str(mime or "").split(";", 1)[0]) or "").casefold()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"


def _asset_token(element: ET.Element) -> str:
    direct = str(element.get("token") or "").strip()
    if direct:
        return direct
    source = str(element.get("src") or "").strip()
    return source if source and "://" not in source else ""


def _asset_context_text(element: ET.Element, top_nodes: Sequence[ET.Element], top_index: int) -> str:
    """Return bounded text adjacent to an asset for deterministic role hints.

    Lark exports candidate captions either in the same block as an image or in the
    immediately preceding block.  Keeping this context in the transient parsed
    asset row lets Lite recognize an explicit recommendation (for example,
    ``候选 2：小手素材，建议选择此项``) without asking the operator to repeat a
    choice.  The context is never written to the user-visible marker text.
    """

    indexes = [top_index]
    if top_index > 0:
        indexes.append(top_index - 1)
    chunks: list[str] = []
    for index in reversed(indexes):
        if not (0 <= index < len(top_nodes)):
            continue
        text = "".join(top_nodes[index].itertext()).strip()
        if text:
            chunks.append(text)
    return " ".join(chunks)[:4096]


def _asset_has_visual_cue(row: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(row.get(field) or "")
        for field in ("name", "relative_path", "context_text", "alt", "description")
    ).casefold()
    return any(cue.casefold() in text for cue in _VISUAL_NAME_CUES)


def _asset_is_recommended(row: Mapping[str, Any]) -> bool:
    for field in ("recommended", "is_recommended", "preferred"):
        value = row.get(field)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, str) and value.strip().casefold() in {
            "true",
            "yes",
            "1",
            "recommended",
            "preferred",
        }:
            return True
    text = " ".join(
        str(row.get(field) or "")
        for field in ("name", "relative_path", "context_text", "alt", "description", "recommendation")
    )
    return bool(_VISUAL_RECOMMENDATION_RE.search(text) or _VISUAL_RECOMMENDATION_VALUE_RE.search(text))


def _recommended_candidate_numbers(row: Mapping[str, Any]) -> set[int]:
    text = " ".join(
        str(row.get(field) or "")
        for field in ("name", "relative_path", "context_text", "alt", "description", "recommendation")
    )
    return {
        int(match.group(1))
        for match in _VISUAL_RECOMMENDED_CANDIDATE_RE.finditer(text)
        if match.group(1).isdigit()
    }


def _image_file_features(row: Mapping[str, Any]) -> dict[str, Any]:
    """Read a few safe, local image features for deterministic selection.

    The intake never OCRs or uploads candidate images.  PNG dimensions and
    alpha-channel presence are enough to distinguish a clean pointer/material
    asset from a screenshot in the common review-document layout; unsupported
    formats simply contribute no feature score.
    """

    path = Path(str(row.get("path") or ""))
    features: dict[str, Any] = {"width": 0, "height": 0, "has_alpha": False}
    try:
        with path.open("rb") as stream:
            header = stream.read(32)
    except OSError:
        return features
    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 26:
        features["width"] = int.from_bytes(header[16:20], "big")
        features["height"] = int.from_bytes(header[20:24], "big")
        # PNG colour types 4 and 6 carry an alpha channel.
        features["has_alpha"] = header[25] in {4, 6}
    elif header.startswith(b"GIF8") and len(header) >= 10:
        features["width"] = int.from_bytes(header[6:8], "little")
        features["height"] = int.from_bytes(header[8:10], "little")
    elif header.startswith(b"\xff\xd8"):
        # JPEG dimensions are optional for ranking; avoid a fragile parser.
        features["jpeg"] = True
    return features


def _asset_selection_score(row: Mapping[str, Any], *, pointer: bool = False) -> tuple[float, ...]:
    """Score an attachment without asking the operator to pick a candidate."""

    text = " ".join(
        str(row.get(field) or "")
        for field in (
            "name",
            "relative_path",
            "context_text",
            "alt",
            "description",
            "recommendation",
        )
    ).casefold()
    score = 0.0
    if _asset_is_recommended(row):
        score += 1000.0
    if _asset_has_visual_cue(row):
        score += 80.0
    positive_cues = (
        "干净",
        "透明",
        "无字",
        "纯色",
        "独立素材",
        "素材",
        "clean",
        "transparent",
        "isolated",
    )
    negative_cues = (
        "截图",
        "屏幕",
        "界面",
        "时间轴",
        "剪映",
        "预览",
        "示例",
        "带字",
        "screenshot",
        "screen",
        "timeline",
        "preview",
    )
    score += sum(24.0 for cue in positive_cues if cue in text)
    score -= sum(32.0 for cue in negative_cues if cue in text)
    features = _image_file_features(row) if _is_image_asset(row) else {}
    has_alpha = bool(features.get("has_alpha"))
    if pointer and has_alpha:
        score += 55.0
    if pointer and not has_alpha and str(row.get("extension") or "").casefold() in {".jpg", ".jpeg"}:
        score -= 8.0
    width = int(features.get("width") or 0)
    height = int(features.get("height") or 0)
    pixel_count = width * height
    # Screenshots are usually large rectangular canvases; pointer assets are
    # compact.  This is a small tie-breaker, never an absolute rejection.
    if pointer and pixel_count:
        if pixel_count <= 2_000_000:
            score += 12.0
        elif pixel_count >= 4_000_000:
            score -= 12.0
    try:
        byte_size = float(row.get("byte_size") or 0)
    except (TypeError, ValueError):
        byte_size = 0.0
    # Prefer a non-empty, higher-quality candidate only after semantic cues.
    quality = min(12.0, max(0.0, byte_size / 500_000.0))
    return (
        score,
        1.0 if has_alpha else 0.0,
        quality,
        float(width * height),
        -float(len(str(row.get("asset_id") or ""))),
    )


def _pointer_preferred_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return an explicit, deterministic pointer candidate when one is present."""

    if not candidates:
        return []
    recommended = [row for row in candidates if _asset_is_recommended(row)]
    if len(recommended) == 1:
        return recommended
    recommended_numbers = {
        number
        for row in candidates
        for number in _recommended_candidate_numbers(row)
    }
    if len(recommended_numbers) == 1:
        candidate_index = next(iter(recommended_numbers)) - 1
        if 0 <= candidate_index < len(candidates):
            return [candidates[candidate_index]]
    named = [row for row in candidates if _asset_has_visual_cue(row)]
    if len(named) == 1:
        return named
    # Distinct Lark attachment IDs can point to the exact same bytes.  They are
    # equivalent for Lite and should not create a needless operator prompt.
    hashes = {
        str(row.get("sha256") or "").casefold()
        for row in candidates
        if str(row.get("sha256") or "").strip()
    }
    if len(hashes) == 1 and hashes:
        return [candidates[0]]
    # No explicit recommendation: still choose deterministically from the
    # description and local image features.  Lite must not stop for a routine
    # candidate group and ask the operator to click one.
    ranked = sorted(
        candidates,
        key=lambda row: (
            -_asset_selection_score(row, pointer=True)[0],
            -_asset_selection_score(row, pointer=True)[1],
            -_asset_selection_score(row, pointer=True)[2],
            -_asset_selection_score(row, pointer=True)[3],
            str(row.get("asset_id") or "").casefold(),
        ),
    )
    return [ranked[0]] if ranked else []


def _wrap_document_xml(content: str) -> ET.Element:
    encoded_size = len(content.encode("utf-8"))
    if encoded_size > _MAX_DOCUMENT_XML_BYTES:
        raise ReviewDocumentIntakeError(
            "document_too_large", "The Feishu/Lark document exceeds the maintained intake limit"
        )
    if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
        raise ReviewDocumentIntakeError(
            "document_xml_unsafe", "The Feishu/Lark document contains unsupported XML declarations"
        )
    normalized = _XML_DECLARATION.sub("", content, count=1)
    parser = ET.XMLPullParser(events=("start", "end"))
    root: ET.Element | None = None
    depth = 0
    element_count = 0
    text_chars = 0
    try:
        document = f"<auto-cut-document>{normalized}</auto-cut-document>"
        for offset in range(0, len(document), 64 * 1024):
            parser.feed(document[offset : offset + 64 * 1024])
            for event, element in parser.read_events():
                if event == "start":
                    depth += 1
                    element_count += 1
                    if root is None:
                        root = element
                    if depth > _MAX_DOCUMENT_XML_DEPTH:
                        raise ReviewDocumentIntakeError(
                            "document_xml_too_complex",
                            "The Feishu/Lark document XML exceeds the maintained depth limit",
                        )
                    if element_count > _MAX_DOCUMENT_XML_ELEMENTS:
                        raise ReviewDocumentIntakeError(
                            "document_xml_too_complex",
                            "The Feishu/Lark document XML exceeds the maintained element limit",
                        )
                    if len(element.attrib) > _MAX_DOCUMENT_XML_ATTRIBUTES or any(
                        len(str(key)) + len(str(value)) > _MAX_DOCUMENT_XML_ATTRIBUTE_CHARS
                        for key, value in element.attrib.items()
                    ):
                        raise ReviewDocumentIntakeError(
                            "document_xml_too_complex",
                            "The Feishu/Lark document XML exceeds the maintained attribute limit",
                        )
                else:
                    text_chars += len(element.text or "") + len(element.tail or "")
                    if text_chars > _MAX_DOCUMENT_XML_TEXT_CHARS:
                        raise ReviewDocumentIntakeError(
                            "document_xml_too_complex",
                            "The Feishu/Lark document XML exceeds the maintained text limit",
                        )
                    depth -= 1
        parser.close()
    except ET.ParseError as exc:
        raise ReviewDocumentIntakeError(
            "document_xml_invalid", "The Feishu/Lark document XML could not be parsed"
        ) from exc
    if root is None or depth != 0:
        raise ReviewDocumentIntakeError(
            "document_xml_invalid", "The Feishu/Lark document XML could not be parsed"
        )
    return root


def _top_level_index(root: ET.Element) -> dict[int, int]:
    indexes: dict[int, int] = {}
    for index, top in enumerate(list(root)):
        for element in top.iter():
            indexes[id(element)] = index
    return indexes


def parse_lark_document(fetch: Mapping[str, Any]) -> dict[str, Any]:
    content = str(fetch.get("content") or "")
    raw_document_id = str(fetch.get("document_id") or "").strip()
    if not raw_document_id:
        raise ReviewDocumentIntakeError(
            "document_fetch_failed", "The Feishu/Lark document identity was missing"
        )
    document_identity_sha256 = _sha256_text(raw_document_id)
    root = _wrap_document_xml(content)
    document_title = str(fetch.get("title") or "").strip()
    if not document_title:
        for title_node in root.iter("title"):
            candidate = "".join(title_node.itertext()).strip()
            if candidate:
                document_title = candidate
                break
    top_indexes = _top_level_index(root)
    top_nodes = list(root)
    review_rows: list[dict[str, Any]] = []
    checkbox_positions: list[tuple[int, int]] = []
    for checkbox in root.iter("checkbox"):
        source_text = "".join(checkbox.itertext())
        if not source_text.strip():
            continue
        raw_block_id = str(checkbox.get("id") or "").strip()
        block_digest = _sha256_text(
            f"{document_identity_sha256}\0{raw_block_id or len(review_rows)}"
        )
        row: dict[str, Any] = {
            "block_id": f"block_{block_digest[:24]}",
            "source_text": source_text,
        }
        colored_spans: list[dict[str, str]] = []
        for span in checkbox.iter("span"):
            color = str(span.get("text-color") or "").strip()
            text = "".join(span.itertext())
            if color and text:
                colored_spans.append({"text": text, "color": color})
        if colored_spans:
            row["colored_spans"] = colored_spans
        review_rows.append(row)
        checkbox_positions.append((top_indexes.get(id(checkbox), -1), len(review_rows) - 1))

    if not review_rows:
        raise ReviewDocumentIntakeError(
            "review_items_missing", "The Feishu/Lark document contains no review checkbox items"
        )

    assets: list[dict[str, Any]] = []
    for asset_index, element in enumerate(
        candidate for candidate in root.iter() if candidate.tag in {"img", "source"}
    ):
        token = _asset_token(element)
        if not token:
            continue
        name = str(element.get("name") or element.get("alt") or "").strip()
        mime = str(element.get("mime") or "application/octet-stream").strip().casefold()
        extension = _safe_extension(name, mime)
        asset_digest = _sha256_text(
            "\0".join(
                (
                    document_identity_sha256,
                    element.tag,
                    str(asset_index),
                    token,
                    name,
                    mime,
                )
            )
        )
        top_index = top_indexes.get(id(element), -1)
        context_text = _asset_context_text(element, top_nodes, top_index)
        pointer_hint = _asset_has_visual_cue(
            {
                "name": name,
                "context_text": context_text,
                "alt": str(element.get("alt") or ""),
            }
        )
        recommended_hint = _asset_is_recommended(
            {
                "name": name,
                "context_text": context_text,
                "alt": str(element.get("alt") or ""),
            }
        )
        associated_item_index: int | None = None
        preceding = [row for row in checkbox_positions if row[0] < top_index]
        if preceding:
            nearest_top, nearest_item = preceding[-1]
            intervening = [row for row in checkbox_positions if nearest_top < row[0] < top_index]
            gap = top_index - nearest_top
            # Keep the original adjacent-block association, but also allow a
            # clearly identified pointer/recommended asset to follow a caption
            # block or a small candidate group before the next review checkbox.
            if not intervening and (
                gap <= 1 or (gap <= 8 and (pointer_hint or recommended_hint))
            ):
                associated_item_index = nearest_item
        expected_size: int | None = None
        raw_size = str(element.get("size") or "").strip()
        if raw_size.isdigit():
            expected_size = int(raw_size)
        assets.append(
            {
                "asset_id": f"asset_{asset_digest[:24]}",
                "tag": element.tag,
                "token": token,
                "name": name,
                "mime": mime,
                "extension": extension,
                "expected_size": expected_size,
                "context_text": context_text,
                "pointer_hint": pointer_hint,
                "recommended": recommended_hint,
                "associated_item_index": associated_item_index,
            }
        )

    safe_asset_identity = [
        {
            "asset_id": row["asset_id"],
            "tag": row["tag"],
            "mime": row["mime"],
            "extension": row["extension"],
            "expected_size": row["expected_size"],
            "pointer_hint": row["pointer_hint"],
            "recommended": row["recommended"],
            "associated_item_index": row["associated_item_index"],
        }
        for row in assets
    ]
    return {
        "document_identity_sha256": document_identity_sha256,
        "document_title": document_title,
        "revision_id": fetch.get("revision_id"),
        "content_sha256": _sha256_text(content),
        "asset_identity_sha256": _sha256_bytes(_canonical_json(safe_asset_identity)),
        "review_items": review_rows,
        "assets": assets,
        "safe_asset_identity": safe_asset_identity,
    }


def download_lark_assets(
    parsed: Mapping[str, Any],
    output_dir: str | os.PathLike[str],
    *,
    lark_cli: str | os.PathLike[str] | None = None,
    runner: CommandRunner | None = None,
    progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    root = Path(output_dir).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    receipt_root = root / ".receipts"
    receipt_root.mkdir(parents=True, exist_ok=True)
    executable = _lark_executable(lark_cli)
    rows: list[dict[str, Any]] = []
    raw_assets = parsed.get("assets")
    if not isinstance(raw_assets, list):
        raise ReviewDocumentIntakeError(
            "asset_index_invalid", "The document asset index is invalid"
        )
    normalized_assets: list[tuple[int, Mapping[str, Any], int | None]] = []
    declared_total_size = 0
    for index, raw in enumerate(raw_assets):
        if not isinstance(raw, Mapping):
            raise ReviewDocumentIntakeError(
                "asset_index_invalid", "The document asset index contains an invalid row"
            )
        asset_id = str(raw.get("asset_id") or "")
        raw_expected_size = raw.get("expected_size")
        if raw_expected_size is None:
            expected_size = None
        elif (
            isinstance(raw_expected_size, bool)
            or not isinstance(raw_expected_size, int)
            or raw_expected_size < 0
        ):
            raise ReviewDocumentIntakeError(
                "asset_index_invalid",
                "The document asset index contains an invalid expected size",
                details={"asset_id": asset_id},
            )
        else:
            expected_size = raw_expected_size
        if expected_size is not None and expected_size > _MAX_ASSET_DOWNLOAD_BYTES:
            raise ReviewDocumentIntakeError(
                "asset_download_size_limit_exceeded",
                "A Feishu/Lark document material exceeds the download size limit",
                details={
                    "asset_id": asset_id,
                    "byte_size": expected_size,
                    "maximum_byte_size": _MAX_ASSET_DOWNLOAD_BYTES,
                },
            )
        declared_total_size += expected_size or 0
        if declared_total_size > _MAX_TOTAL_ASSET_DOWNLOAD_BYTES:
            raise ReviewDocumentIntakeError(
                "asset_download_total_limit_exceeded",
                "The Feishu/Lark document materials exceed the total download size limit",
                details={
                    "declared_byte_size": declared_total_size,
                    "maximum_byte_size": _MAX_TOTAL_ASSET_DOWNLOAD_BYTES,
                },
            )
        normalized_assets.append((index, raw, expected_size))

    total_asset_size = 0
    revision_id = parsed.get("revision_id")
    normalized_revision_id = None if revision_id is None else str(revision_id)
    for index, raw, expected_size in normalized_assets:
        asset_id = str(raw.get("asset_id") or "")
        extension = str(raw.get("extension") or ".bin")
        token = str(raw.get("token") or "")
        target = root / f"{asset_id}{extension}"
        receipt_path = receipt_root / f"{asset_id}.json"
        input_digest = _sha256_bytes(
            _canonical_json(
                {
                    "document_identity_sha256": str(
                        parsed.get("document_identity_sha256") or ""
                    ),
                    "revision_id": normalized_revision_id,
                    "content_sha256": str(parsed.get("content_sha256") or ""),
                    "asset_identity_sha256": str(
                        parsed.get("asset_identity_sha256") or ""
                    ),
                    "asset_id": asset_id,
                    "token_sha256": _sha256_text(token),
                    "expected_size": expected_size,
                    "adapter_version": LARK_ADAPTER_VERSION,
                }
            )
        )
        receipt: dict[str, Any] = {}
        try:
            loaded = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                receipt = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            receipt = {}
        target_size = target.stat().st_size if target.is_file() else None
        cache_metadata_matches = bool(
            target_size is not None
            and target_size > 0
            and receipt.get("schema_version") == INTAKE_SCHEMA_VERSION
            and receipt.get("asset_id") == asset_id
            and receipt.get("input_digest") == input_digest
            and receipt.get("byte_size") == target_size
        )
        if (
            cache_metadata_matches
            and target_size is not None
            and target_size > _MAX_ASSET_DOWNLOAD_BYTES
        ):
            raise ReviewDocumentIntakeError(
                "asset_download_size_limit_exceeded",
                "A cached Feishu/Lark document material exceeds the download size limit",
                details={
                    "asset_id": asset_id,
                    "byte_size": target_size,
                    "maximum_byte_size": _MAX_ASSET_DOWNLOAD_BYTES,
                },
            )
        cache_hit = bool(
            cache_metadata_matches
            and target_size is not None
            and (expected_size is None or target_size == expected_size)
            and receipt.get("sha256") == _sha256_file(target)
        )
        if (
            cache_hit
            and target_size is not None
            and total_asset_size + target_size > _MAX_TOTAL_ASSET_DOWNLOAD_BYTES
        ):
            raise ReviewDocumentIntakeError(
                "asset_download_total_limit_exceeded",
                "The cached Feishu/Lark document materials exceed the total download size limit",
                details={
                    "aggregate_byte_size": total_asset_size + target_size,
                    "maximum_byte_size": _MAX_TOTAL_ASSET_DOWNLOAD_BYTES,
                },
            )
        if not cache_hit:
            if target.exists():
                target.unlink()
            if receipt_path.exists():
                receipt_path.unlink()
            if progress is not None:
                progress(
                    {
                        "event": "asset_download",
                        "status": "started",
                        "asset_id": asset_id,
                        "asset_index": index,
                    }
                )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{asset_id}.", suffix=f".part{extension}", dir=root
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            temporary.unlink()
            try:
                completed = (runner or _default_command_runner)(
                    [
                        *executable,
                        "docs",
                        "+media-download",
                        "--token",
                        token,
                        "--output",
                        str(temporary),
                        "--as",
                        "user",
                    ]
                )
            except Exception:
                if temporary.exists():
                    temporary.unlink()
                raise
            if (
                completed.returncode != 0
                or not temporary.is_file()
                or temporary.stat().st_size <= 0
            ):
                if temporary.exists():
                    temporary.unlink()
                raise ReviewDocumentIntakeError(
                    "asset_download_failed",
                    "A Feishu/Lark document material could not be downloaded",
                    user_action={
                        "action_code": "authorization",
                        "reason_code": "asset_download_failed",
                    },
                    details={"asset_id": asset_id, "provider_exit_code": completed.returncode},
                )
            downloaded_size = temporary.stat().st_size
            if downloaded_size > _MAX_ASSET_DOWNLOAD_BYTES:
                temporary.unlink()
                raise ReviewDocumentIntakeError(
                    "asset_download_size_limit_exceeded",
                    "A downloaded Feishu/Lark document material exceeds the download size limit",
                    details={
                        "asset_id": asset_id,
                        "byte_size": downloaded_size,
                        "maximum_byte_size": _MAX_ASSET_DOWNLOAD_BYTES,
                    },
                )
            if total_asset_size + downloaded_size > _MAX_TOTAL_ASSET_DOWNLOAD_BYTES:
                temporary.unlink()
                raise ReviewDocumentIntakeError(
                    "asset_download_total_limit_exceeded",
                    (
                        "The downloaded Feishu/Lark document materials exceed the total "
                        "download size limit"
                    ),
                    details={
                        "aggregate_byte_size": total_asset_size + downloaded_size,
                        "maximum_byte_size": _MAX_TOTAL_ASSET_DOWNLOAD_BYTES,
                    },
                )
            if expected_size is not None and downloaded_size != expected_size:
                temporary.unlink()
                raise ReviewDocumentIntakeError(
                    "asset_download_incomplete",
                    "A downloaded Feishu/Lark document material failed its size check",
                    details={"asset_id": asset_id},
                )
            os.replace(temporary, target)
            receipt = {
                "schema_version": INTAKE_SCHEMA_VERSION,
                "asset_id": asset_id,
                "input_digest": input_digest,
                "sha256": _sha256_file(target),
                "byte_size": target.stat().st_size,
            }
            _atomic_write_json(receipt_path, receipt)
            target_size = downloaded_size
        if target_size is None:
            raise ReviewDocumentIntakeError(
                "asset_download_failed",
                "A Feishu/Lark document material could not be downloaded",
                details={"asset_id": asset_id},
            )
        total_asset_size += target_size
        row = {
            "asset_id": asset_id,
            "path": str(target),
            "relative_path": target.name,
            "sha256": str(receipt.get("sha256") or _sha256_file(target)),
            "byte_size": target_size,
            "mime": str(raw.get("mime") or "application/octet-stream"),
            "extension": extension,
            "name": str(raw.get("name") or ""),
            "context_text": str(raw.get("context_text") or ""),
            "pointer_hint": bool(raw.get("pointer_hint")),
            "recommended": bool(raw.get("recommended")),
            "associated_item_index": raw.get("associated_item_index"),
            "cache_hit": cache_hit,
        }
        rows.append(row)
        if progress is not None:
            progress(
                {
                    "event": "asset_download",
                    "status": "resumed" if cache_hit else "complete",
                    "asset_id": asset_id,
                    "asset_index": index,
                }
            )
    return rows


def _name_has_source_cue(name: str) -> bool:
    folded = str(name or "").casefold()
    return any(cue.casefold() in folded for cue in _SOURCE_VIDEO_CUES)


def _explicit_visual_match(name: str, source_text: str) -> bool:
    folded_name = str(name or "").casefold()
    folded_text = str(source_text or "").casefold()
    return any(
        cue.casefold() in folded_name and cue.casefold() in folded_text
        for cue in _VISUAL_NAME_CUES
    )


def _is_image_asset(row: Mapping[str, Any]) -> bool:
    return str(row.get("mime") or "").casefold().startswith("image/") or str(
        row.get("extension") or ""
    ).casefold() in _IMAGE_SUFFIXES


def _pointer_review_item(item: Mapping[str, Any]) -> bool:
    text = str(item.get("source_text") or "").casefold()
    kind = str(item.get("kind") or "").casefold()
    return any(cue.casefold() in text or cue.casefold() in kind for cue in _VISUAL_NAME_CUES)


def _known_image_replacement_item(item: Mapping[str, Any]) -> bool:
    text = str(item.get("source_text") or "")
    kind = str(item.get("kind") or "").strip().casefold()
    explicit_replacement_kind = kind in {
        "image_replace",
        "image_replacement",
        "replace_picture",
        "replacement_picture",
        "visual_replace",
        "visual_replacement",
    }
    return (
        _pointer_review_item(item)
        or explicit_replacement_kind
        or bool(_IMAGE_REPLACEMENT_RELATION_RE.search(f"{kind} {text}"))
    )


def _same_name_key(item: Mapping[str, Any]) -> str:
    for field in ("material_name", "name", "title"):
        value = str(item.get(field) or "").strip()
        if value:
            return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()
    text = str(item.get("source_text") or "")
    text = re.sub(
        r"^\s*(?:\d{1,2}\s*[:：]\s*\d{1,2}(?:\.\d+)?)\s*",
        "",
        text,
    )
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()


def _visual_ambiguity(item: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> None:
    item_id = str(item.get("block_id") or item.get("id") or item.get("item_id") or "")
    safe_candidates = [
        {
            "asset_id": str(row.get("asset_id") or ""),
            "sha256": str(row.get("sha256") or ""),
            "byte_size": int(row.get("byte_size") or 0),
            "extension": str(row.get("extension") or ""),
        }
        for row in candidates
    ]
    raise ReviewDocumentIntakeError(
        "visual_asset_ambiguous",
        "A review item has multiple possible local visual materials",
        user_action={
            "action_code": "high_risk_confirmation",
            "reason_code": "visual_asset_ambiguous",
            "item_ids": [item_id] if item_id else [],
            "candidate_ids": [row["asset_id"] for row in safe_candidates],
        },
        details={"candidates": safe_candidates},
    )


def compile_url_inputs(
    parsed: Mapping[str, Any],
    downloaded_assets: Sequence[Mapping[str, Any]],
    *,
    external_name: str | None = None,
) -> dict[str, Any]:
    review_items = [dict(row) for row in parsed.get("review_items") or []]
    asset_rows = [dict(row) for row in downloaded_assets]
    videos = [
        row
        for row in asset_rows
        if str(row.get("mime") or "").casefold().startswith("video/")
        or str(row.get("extension") or "").casefold() in _VIDEO_SUFFIXES
    ]
    if len(videos) == 1:
        source_video = videos[0]
    elif videos:
        # Source videos can also be exported as a small candidate group.  Use
        # the same deterministic, description-led policy as image materials;
        # an explicit source cue wins, then byte size provides a stable final
        # quality tie-breaker.  This removes a needless interactive choice
        # while retaining a clear failure for the genuinely missing case.
        source_video = sorted(
            videos,
            key=lambda row: (
                -float(_name_has_source_cue(str(row.get("name") or ""))),
                -float(_name_has_source_cue(str(row.get("context_text") or ""))),
                -float(row.get("byte_size") or 0),
                str(row.get("asset_id") or "").casefold(),
            ),
        )[0]
    else:
        raise ReviewDocumentIntakeError(
            "source_video_missing",
            "The document contains no downloadable source video",
            user_action={
                "action_code": "high_risk_confirmation",
                "reason_code": "source_video_missing",
                "candidate_ids": [],
            },
            details={"candidates": []},
        )

    visual_asset_ids: set[str] = set()
    direct_by_item: dict[int, list[dict[str, Any]]] = {}
    for asset in asset_rows:
        if not _is_image_asset(asset):
            continue
        item_index = asset.get("associated_item_index")
        if isinstance(item_index, bool) or not isinstance(item_index, int):
            continue
        if 0 <= item_index < len(review_items):
            direct_by_item.setdefault(item_index, []).append(asset)

    pointer_by_name: dict[str, list[dict[str, Any]]] = {}
    for item_index, candidates in direct_by_item.items():
        item = review_items[item_index]
        if not _known_image_replacement_item(item):
            continue
        if len(candidates) > 1:
            preferred = _pointer_preferred_candidates(candidates)
            selected = preferred[0] if preferred else None
            if selected is None:
                # A candidate group with no readable metadata is still
                # handled deterministically by asset ID; it is safer to keep
                # the workflow moving with a traceable choice than to request
                # manual intervention for a routine image attachment.
                selected = sorted(
                    candidates,
                    key=lambda row: str(row.get("asset_id") or "").casefold(),
                )[0]
        else:
            selected = candidates[0]
        item["asset_paths"] = [str(selected["path"])]
        item["asset_selection"] = {
            "policy": "automatic_recommended_description_visual_features",
            "candidate_count": len(candidates),
            "selected_asset_id": str(selected.get("asset_id") or ""),
            "selected_score": list(_asset_selection_score(
                selected,
                pointer=_pointer_review_item(item),
            )),
        }
        item["kind"] = "pointer_overlay" if _pointer_review_item(item) else "visual_overlay"
        item["execution_required"] = True
        visual_asset_ids.add(str(selected.get("asset_id") or ""))
        if _pointer_review_item(item):
            pointer_by_name.setdefault(_same_name_key(item), []).append(selected)

    # A missing hand/pointer attachment may reuse a unique material already
    # attached to another review row with the same modification name.
    for item in review_items:
        if item.get("asset_paths") or not _pointer_review_item(item):
            continue
        candidates = pointer_by_name.get(_same_name_key(item), [])
        unique = {
            str(candidate.get("asset_id") or ""): candidate for candidate in candidates
        }
        if unique:
            if len(unique) == 1:
                selected = next(iter(unique.values()))
            else:
                selected = _pointer_preferred_candidates(list(unique.values()))[0]
            item["asset_paths"] = [str(selected["path"])]
            item["asset_selection"] = {
                "policy": "automatic_same_name_reuse_description_visual_features",
                "candidate_count": len(unique),
                "selected_asset_id": str(selected.get("asset_id") or ""),
                "selected_score": list(_asset_selection_score(selected, pointer=True)),
            }
            item["kind"] = "pointer_overlay"
            item["execution_required"] = True
            visual_asset_ids.add(str(selected.get("asset_id") or ""))

    document_identity = str(parsed.get("document_identity_sha256") or "")
    revision_id = parsed.get("revision_id")
    content_sha256 = str(parsed.get("content_sha256") or "")
    asset_identity_sha256 = str(parsed.get("asset_identity_sha256") or "")
    document_title = str(parsed.get("document_title") or "").strip()
    name_resolution = resolve_artifact_name(
        external_name=external_name,
        document_title=document_title,
        fallback_name=f"AutoCutLite-{document_identity[:12]}",
    )
    snapshot = {
        "document": {
            "document_identity_sha256": document_identity,
            "title": document_title,
            "revision": revision_id,
            "content_sha256": content_sha256,
            "asset_identity_sha256": asset_identity_sha256,
            "extraction_schema_version": INTAKE_SCHEMA_VERSION,
        },
        "review_items": review_items,
    }
    project = {
        "draft_name": name_resolution.final_name,
        "requested_name": name_resolution.requested_name,
        "final_name": name_resolution.final_name,
        "name_source": name_resolution.source,
        "name_sanitized": name_resolution.sanitized,
        "source_video": str(source_video["path"]),
        "source_audio": "",
        "replacement_audio": "",
        "project_key": document_identity[:32],
        "workflow_mode": "lite",
        "lite_cut_layout": "split_gap",
    }
    manifest_rows: list[dict[str, Any]] = []
    for row in asset_rows:
        asset_id = str(row.get("asset_id") or "")
        if asset_id == source_video.get("asset_id"):
            role = "source_video"
        elif asset_id in visual_asset_ids:
            role = "visual_asset"
        else:
            role = "document_attachment"
        manifest_rows.append(
            {
                "asset_id": asset_id,
                "relative_path": str(row.get("relative_path") or ""),
                "sha256": str(row.get("sha256") or ""),
                "byte_size": int(row.get("byte_size") or 0),
                "mime": str(row.get("mime") or ""),
                "extension": str(row.get("extension") or ""),
                "role": role,
            }
        )
    asset_manifest = {
        "schema_version": INTAKE_SCHEMA_VERSION,
        "document_identity_sha256": document_identity,
        "revision": revision_id,
        "content_sha256": content_sha256,
        "asset_identity_sha256": asset_identity_sha256,
        "assets": manifest_rows,
    }
    return {
        "snapshot": snapshot,
        "project": project,
        "asset_manifest": asset_manifest,
        "name_resolution": name_resolution.as_dict(),
    }


def default_readiness_path() -> Path:
    explicit = os.environ.get("AUTOCUT_LITE_READINESS_PATH", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise ReviewDocumentIntakeError(
                "readiness_path_invalid", "AUTOCUT_LITE_READINESS_PATH must be absolute"
            )
        return path.resolve(strict=False)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise ReviewDocumentIntakeError(
            "readiness_path_invalid", "LOCALAPPDATA is unavailable"
        )
    return (
        Path(local_app_data) / "Auto-Cut" / "auto-cut-lite" / "runtime-readiness.json"
    ).resolve(strict=False)


def _read_readiness(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": READINESS_SCHEMA_VERSION,
            "lark": {"status": "pending_validation"},
            "asr": {"status": "pending_validation"},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "schema_version": READINESS_SCHEMA_VERSION,
            "lark": {"status": "pending_validation"},
            "asr": {"status": "pending_validation"},
        }
    if not isinstance(payload, dict) or payload.get("schema_version") != READINESS_SCHEMA_VERSION:
        return {
            "schema_version": READINESS_SCHEMA_VERSION,
            "lark": {"status": "pending_validation"},
            "asr": {"status": "pending_validation"},
        }
    return payload


def _safe_identity_digest(payload: Mapping[str, Any]) -> str:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        folded = str(key).casefold()
        if any(fragment in folded for fragment in _SENSITIVE_IDENTITY_KEYS):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            safe[str(key)] = value
        elif isinstance(value, Mapping):
            nested = {
                str(child_key): child_value
                for child_key, child_value in value.items()
                if child_value is None
                or isinstance(child_value, (str, int, float, bool))
                if not any(
                    fragment in str(child_key).casefold()
                    for fragment in _SENSITIVE_IDENTITY_KEYS
                )
            }
            if nested:
                safe[str(key)] = nested
    return _sha256_bytes(_canonical_json(safe))


def evaluate_runtime_readiness(
    *,
    path: str | os.PathLike[str] | None = None,
    runtime_version: str,
    lark_version: str,
    asr_adapter_version: str,
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve(strict=False) if path else default_readiness_path()
    payload = _read_readiness(target)
    expected = {
        "runtime_version": str(runtime_version),
        "lark_adapter_version": LARK_ADAPTER_VERSION,
        "lark_cli_version": str(lark_version),
        "asr_adapter_version": str(asr_adapter_version),
    }
    changed = False
    if payload.get("versions") != expected:
        payload["versions"] = expected
        payload["lark"] = {
            "status": "pending_validation",
            "reason_code": "runtime_or_adapter_changed",
        }
        payload["asr"] = {
            "status": "pending_validation",
            "reason_code": "runtime_or_adapter_changed",
        }
        changed = True
    payload["schema_version"] = READINESS_SCHEMA_VERSION
    if changed or not target.is_file():
        _atomic_write_json(target, payload)
    return payload


def mark_lark_verified(
    whoami: Mapping[str, Any],
    *,
    path: str | os.PathLike[str] | None = None,
    runtime_version: str,
    lark_version: str,
    asr_adapter_version: str,
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve(strict=False) if path else default_readiness_path()
    payload = evaluate_runtime_readiness(
        path=target,
        runtime_version=runtime_version,
        lark_version=lark_version,
        asr_adapter_version=asr_adapter_version,
    )
    payload["lark"] = {
        "status": "verified",
        "verified_at": _utc_now(),
        "identity_sha256": _safe_identity_digest(whoami),
    }
    _atomic_write_json(target, payload)
    return payload


def invalidate_lark_readiness(
    reason_code: str,
    *,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve(strict=False) if path else default_readiness_path()
    payload = _read_readiness(target)
    payload["lark"] = {
        "status": "pending_validation",
        "invalidated_at": _utc_now(),
        "reason_code": str(reason_code),
    }
    _atomic_write_json(target, payload)
    return payload


def mark_asr_verified(
    *,
    provider: str,
    model_or_resource: str,
    adapter_version: str,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve(strict=False) if path else default_readiness_path()
    payload = _read_readiness(target)
    identity = {
        "provider": str(provider),
        "model_or_resource": str(model_or_resource),
        "adapter_version": str(adapter_version),
    }
    versions = payload.get("versions")
    expected_adapter = (
        str(versions.get("asr_adapter_version") or "")
        if isinstance(versions, Mapping)
        else ""
    )
    if (
        not expected_adapter
        or identity["adapter_version"] != expected_adapter
        or not identity["provider"].strip()
        or not identity["model_or_resource"].strip()
    ):
        payload["asr"] = {
            "status": "pending_validation",
            "reason_code": "asr_adapter_identity_mismatch",
        }
        _atomic_write_json(target, payload)
        return payload
    payload["asr"] = {
        "status": "verified",
        "verified_at": _utc_now(),
        "identity_sha256": _sha256_bytes(_canonical_json(identity)),
        "adapter_version": str(adapter_version),
    }
    _atomic_write_json(target, payload)
    return payload


__all__ = [
    "INTAKE_SCHEMA_VERSION",
    "LARK_ADAPTER_VERSION",
    "READINESS_SCHEMA_VERSION",
    "ReviewDocumentIntakeError",
    "compile_url_inputs",
    "default_readiness_path",
    "document_url_digest",
    "download_lark_assets",
    "evaluate_runtime_readiness",
    "fetch_lark_document",
    "invalidate_lark_readiness",
    "lark_cli_version",
    "lark_whoami",
    "mark_asr_verified",
    "mark_lark_verified",
    "parse_lark_document",
    "sanitize_document_snapshot",
    "validate_document_url",
]
