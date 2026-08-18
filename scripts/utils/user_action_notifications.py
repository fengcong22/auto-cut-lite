"""Closed, privacy-safe contracts for optional user-action notifications."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
TEMPLATE_VERSION = "auto_cut_user_action_required_v1"
PROVIDER = "lark_cli"
PRIVACY_CONFIRMATION = "auto_cut_notification_privacy_v1"
SETUP_PREVIEW_IDEMPOTENCY_KEY = "ac-uar-setup-preview-v1"
SETUP_PREVIEW_MESSAGE = "\n".join(
    (
        "Auto-Cut requires your confirmation",
        "Project: project-<fingerprint>",
        "Action: <action label> (<action code>)",
        "Waiting items: <count>",
        "Return to the current Codex task to continue.",
        "Event ID: uar-<event-id>",
    )
)
FORBIDDEN_PRIVACY_FIELDS = (
    "review_document_text",
    "source_ledger_text",
    "media_contents",
    "local_absolute_paths",
    "access_tokens",
    "app_credentials",
    "authorization_urls",
    "device_codes",
    "full_profile_evidence",
    "raw_destination_id",
    "raw_project_key",
    "raw_item_ids",
    "input_digest",
    "provider_stdout",
    "provider_stderr",
)
ACTION_LABELS = {
    "subject_identity": "Confirm stage and subject",
    "subject_evidence": "Provide subject pointer evidence",
    "preview_approval": "Approve the final preview",
    "project_binding": "Confirm project binding or rebind",
    "authorization": "Complete required authorization",
    "high_risk_confirmation": "Confirm a high-risk operation",
}

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "data" / "auto-cut-notifications.local.json"
DEFAULT_RECEIPTS_ROOT = REPOSITORY_ROOT / "data" / "auto-cut-notification-receipts.local"
ERROR_CODES = frozenset(
    {
        "missing_cli",
        "timeout",
        "authentication_required",
        "permission_denied",
        "network_error",
        "rate_limited",
        "provider_unavailable",
        "invalid_response",
        "cli_error",
    }
)

_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "enabled",
        "provider",
        "identity",
        "destination",
        "approved_template_version",
    }
)
_DESTINATION_FIELDS = frozenset({"type", "id"})
_IDENTITIES = frozenset({"user", "bot"})
_DESTINATION_PATTERNS = {
    "chat_id": re.compile(r"oc_[A-Za-z0-9_-]+"),
    "open_id": re.compile(r"ou_[A-Za-z0-9_-]+"),
}
_ACTION_CODES = frozenset(ACTION_LABELS)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_PROJECT_LABEL_PATTERN = re.compile(r"project-[0-9a-f]{12}")
_MESSAGE_ID_PATTERN = re.compile(r"om_[A-Za-z0-9_-]{1,128}")
_UTC_Z_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "idempotency_key_digest",
        "template_version",
        "destination_fingerprint",
        "delivery_state",
        "attempt_count",
        "sent_at",
        "message_id",
        "error_code",
    }
)
_UNSAFE_EXECUTABLE_SUFFIXES = frozenset({".bat", ".ps1", ".sh", ".js", ".py"})
_TRANSIENT_ERROR_CODES = frozenset(
    {"timeout", "network_error", "rate_limited", "provider_unavailable"}
)
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF
_EVENT_LOCKS: dict[str, threading.Lock] = {}
_EVENT_LOCKS_GUARD = threading.Lock()


class NotificationPersistenceError(OSError):
    """A privacy-safe receipt storage failure."""


class NotificationSetupError(RuntimeError):
    """A closed, privacy-safe setup dry-run failure."""

    def __init__(self, error_code: str) -> None:
        if error_code not in ERROR_CODES:
            error_code = "cli_error"
        self.error_code = error_code
        super().__init__(f"notification setup dry-run failed: {error_code}")


def _require_literal(value: Any, expected: frozenset[str], label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if value not in expected:
        raise ValueError(f"unsupported {label}: {value}")
    return value


def _require_destination(destination_type: Any, destination_id: Any) -> tuple[str, str]:
    destination_type = _require_literal(
        destination_type,
        frozenset(_DESTINATION_PATTERNS),
        "destination type",
    )
    if not isinstance(destination_id, str):
        raise TypeError("destination id must be text")
    if _DESTINATION_PATTERNS[destination_type].fullmatch(destination_id) is None:
        prefix = "oc_" if destination_type == "chat_id" else "ou_"
        raise ValueError(f"destination id for {destination_type} must use the {prefix} prefix")
    return destination_type, destination_id


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_item_ids(values: Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("item_ids must be a sequence of strings")
    item_ids = tuple(values)
    if not item_ids:
        raise ValueError("item_ids must contain at least one stable item ID")
    for item_id in item_ids:
        if not isinstance(item_id, str):
            raise TypeError("item_ids must contain only strings")
        if not item_id:
            raise ValueError("item_ids must contain only nonempty strings")
        try:
            item_id.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("item_ids must contain valid UTF-8 text") from error
    return tuple(sorted(set(item_ids)))


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _missing_cli() -> FileNotFoundError:
    return FileNotFoundError("missing_cli: no safe native lark-cli command is available")


def _regular_resolved_path(value: str | os.PathLike[str] | None) -> Path | None:
    if not value:
        return None
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return path if path.is_file() else None


def _is_native_executable(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            signature = handle.read(4)
    except OSError:
        return False
    return (
        signature.startswith(b"MZ")
        or signature == b"\x7fELF"
        or signature
        in {
            b"\xfe\xed\xfa\xce",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
        }
    )


def resolve_lark_command(executable: str | None = None) -> list[str]:
    """Resolve a safe native command prefix without executing any process."""

    candidate = _regular_resolved_path(shutil.which(executable or "lark-cli"))
    if candidate is None:
        raise _missing_cli()
    suffix = candidate.suffix.lower()
    if suffix == ".cmd":
        run_js = _regular_resolved_path(
            candidate.parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
        )
        node = _regular_resolved_path(shutil.which("node.exe") or shutil.which("node"))
        if run_js is None or node is None or not _is_native_executable(node):
            raise _missing_cli()
        return [str(node), str(run_js)]
    if suffix in _UNSAFE_EXECUTABLE_SUFFIXES or not _is_native_executable(candidate):
        raise _missing_cli()
    return [str(candidate)]


@dataclass(frozen=True)
class NotificationConfig:
    """One explicitly approved local notification destination."""

    enabled: bool
    provider: str
    identity: str
    destination_type: str
    destination_id: str
    approved_template_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a boolean")
        _require_literal(self.provider, frozenset({PROVIDER}), "provider")
        _require_literal(self.identity, _IDENTITIES, "identity")
        _require_destination(self.destination_type, self.destination_id)
        _require_literal(
            self.approved_template_version,
            frozenset({TEMPLATE_VERSION}),
            "approved template version",
        )


@dataclass(frozen=True)
class UserActionEvent:
    """Canonical identity for one unresolved, user-visible action request."""

    input_digest: str
    project_label: str
    action_code: str
    item_ids: tuple[str, ...]
    prompt_revision: int

    def __post_init__(self) -> None:
        _require_sha256(self.input_digest, "input_digest")
        if not isinstance(self.project_label, str):
            raise TypeError("project_label must be text")
        if _PROJECT_LABEL_PATTERN.fullmatch(self.project_label) is None:
            raise ValueError("project_label must be a sanitized project fingerprint")
        _require_literal(self.action_code, _ACTION_CODES, "action_code")
        object.__setattr__(self, "item_ids", _canonical_item_ids(self.item_ids))
        if not isinstance(self.prompt_revision, int) or isinstance(self.prompt_revision, bool):
            raise TypeError("prompt_revision must be an integer")
        if self.prompt_revision < 1:
            raise ValueError("prompt_revision must be at least 1")

    @property
    def digest(self) -> str:
        """Return the full SHA-256 digest of the canonical event payload."""

        payload = {
            "schema_version": SCHEMA_VERSION,
            "input_digest": self.input_digest,
            "project_label": self.project_label,
            "action_code": self.action_code,
            "item_ids": sorted(set(self.item_ids)),
            "prompt_revision": self.prompt_revision,
        }
        return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()

    @property
    def event_digest(self) -> str:
        """Return the named full event digest used for durable deduplication."""

        return self.digest

    @property
    def event_id(self) -> str:
        """Return a short, user-visible correlation identifier."""

        return f"uar-{self.digest[:12]}"

    @property
    def idempotency_key(self) -> str:
        """Return the provider idempotency key for this exact event."""

        return f"ac-uar-v1-{self.digest[:40]}"


def destination_fingerprint(config: NotificationConfig) -> str:
    """Return the only destination representation allowed in reports and receipts."""

    if not isinstance(config, NotificationConfig):
        raise TypeError("config must be a NotificationConfig")
    return hashlib.sha256(config.destination_id.encode("utf-8")).hexdigest()


def render_user_action_message(event: UserActionEvent) -> str:
    """Render the fixed, privacy-reviewed notification template."""

    if not isinstance(event, UserActionEvent):
        raise TypeError("event must be a UserActionEvent")
    return "\n".join(
        (
            "Auto-Cut requires your confirmation",
            f"Project: {event.project_label}",
            f"Action: {ACTION_LABELS[event.action_code]} ({event.action_code})",
            f"Waiting items: {len(event.item_ids)}",
            "Return to the current Codex task to continue.",
            f"Event ID: {event.event_id}",
        )
    )


def build_lark_argv(
    config: NotificationConfig,
    message: str,
    idempotency_key: str,
    *,
    dry_run: bool = False,
    command_prefix: Sequence[str] | None = None,
) -> list[str]:
    """Build the exact argument array for one approved Lark message send."""

    if not isinstance(config, NotificationConfig):
        raise TypeError("config must be a NotificationConfig")
    if not isinstance(message, str):
        raise TypeError("message must be text")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("idempotency_key must be nonempty text")
    if not isinstance(dry_run, bool):
        raise TypeError("dry_run must be a boolean")
    prefix = resolve_lark_command() if command_prefix is None else list(command_prefix)
    if not prefix or any(not isinstance(argument, str) or not argument for argument in prefix):
        raise ValueError("command_prefix must contain nonempty argument strings")
    destination_flag = "--chat-id" if config.destination_type == "chat_id" else "--user-id"
    argv = [
        *prefix,
        "im",
        "+messages-send",
        "--as",
        config.identity,
        destination_flag,
        config.destination_id,
        "--text",
        message,
        "--idempotency-key",
        idempotency_key,
    ]
    if dry_run:
        argv.append("--dry-run")
    argv.append("--json")
    return argv


def classify_lark_failure(returncode: int, stdout: str, stderr: str) -> tuple[str, bool]:
    """Map provider output to one closed error code without returning raw details."""

    if returncode == 0:
        return "invalid_response", False
    combined = "\n".join(value for value in (stdout, stderr) if isinstance(value, str)).lower()
    combined = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", combined)
    if re.search(
        r"authentication required|not logged in|login required|unauthorized|"
        r"invalid (?:access )?token|token (?:has )?expired|\b401\b",
        combined,
    ):
        return "authentication_required", False
    if re.search(
        r"permission denied|permission_violations|forbidden|"
        r"(?:missing|insufficient) (?:app )?scope|\b403\b",
        combined,
    ):
        return "permission_denied", False
    if re.search(r"rate.?limit|too many requests|\b429\b", combined):
        return "rate_limited", True
    if re.search(
        r"\b5\d\d\b|service unavailable|provider unavailable|internal server error|"
        r"bad gateway|gateway timeout",
        combined,
    ):
        return "provider_unavailable", True
    if re.search(
        r"econn|enet|ehost|eai_again|dns|network|socket|connection (?:reset|refused)|"
        r"fetcherror|connectex|wsarecv|i/o timeout|no such host|"
        r"context deadline exceeded|tls handshake timeout",
        combined,
    ):
        return "network_error", True
    return "cli_error", False


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"notification config contains non-finite JSON value: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"notification config contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_fields(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    if set(value) != expected:
        raise ValueError(f"{label} must contain exactly the approved fields")
    return value


def load_notification_config(path: Path = DEFAULT_CONFIG_PATH) -> NotificationConfig | None:
    """Load one strict local config, or return ``None`` when it does not exist."""

    try:
        content = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("notification config must be valid UTF-8 JSON") from error
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError("notification config must be valid JSON") from error

    config = _require_exact_fields(payload, _CONFIG_FIELDS, "notification config")
    schema_version = config["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise TypeError("notification config schema_version must be an integer")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"notification config schema_version must be {SCHEMA_VERSION}")
    destination = _require_exact_fields(
        config["destination"],
        _DESTINATION_FIELDS,
        "notification config destination",
    )
    return NotificationConfig(
        enabled=config["enabled"],
        provider=config["provider"],
        identity=config["identity"],
        destination_type=destination["type"],
        destination_id=destination["id"],
        approved_template_version=config["approved_template_version"],
    )


def _config_payload(config: NotificationConfig) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": config.enabled,
        "provider": config.provider,
        "identity": config.identity,
        "destination": {
            "type": config.destination_type,
            "id": config.destination_id,
        },
        "approved_template_version": config.approved_template_version,
    }


def _config_summary(config: NotificationConfig | None) -> dict[str, Any]:
    return {
        "configured": config is not None,
        "enabled": config.enabled if config is not None else False,
        "provider": config.provider if config is not None else PROVIDER,
        "identity": config.identity if config is not None else None,
        "destination_type": config.destination_type if config is not None else None,
        "approved_template_version": (
            config.approved_template_version if config is not None else TEMPLATE_VERSION
        ),
        "destination_fingerprint": (
            destination_fingerprint(config) if config is not None else None
        ),
    }


def notification_setup_preview(
    identity: str,
    destination_type: str,
    destination_id: str,
) -> dict[str, Any]:
    """Build the canonical, privacy-safe setup approval preview."""

    config = NotificationConfig(
        enabled=True,
        provider=PROVIDER,
        identity=identity,
        destination_type=destination_type,
        destination_id=destination_id,
        approved_template_version=TEMPLATE_VERSION,
    )
    preview = {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER,
        "identity": config.identity,
        "destination_type": config.destination_type,
        "destination_fingerprint": destination_fingerprint(config),
        "template_version": TEMPLATE_VERSION,
        "privacy_version": PRIVACY_CONFIRMATION,
        "message_template": SETUP_PREVIEW_MESSAGE,
        "forbidden_privacy_fields": list(FORBIDDEN_PRIVACY_FIELDS),
    }
    destination_bytes = config.destination_id.encode("utf-8")
    digest_input = b"auto-cut-notification-setup-preview-v1\0"
    digest_input += len(destination_bytes).to_bytes(8, "big") + destination_bytes
    digest_input += _canonical_json_bytes(preview)
    return {
        "preview_digest": hashlib.sha256(digest_input).hexdigest(),
        "preview": preview,
    }


def preview_notification_setup(
    *,
    identity: str,
    destination_type: str,
    destination_id: str,
    timeout_seconds: float = 15.0,
    command_resolver: Callable[[], Sequence[str]] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Run the exact approved send command in dry-run mode and return its safe preview."""

    result = notification_setup_preview(identity, destination_type, destination_id)
    config = NotificationConfig(
        enabled=True,
        provider=PROVIDER,
        identity=identity,
        destination_type=destination_type,
        destination_id=destination_id,
        approved_template_version=TEMPLATE_VERSION,
    )
    resolver = resolve_lark_command if command_resolver is None else command_resolver
    try:
        command_prefix = resolver()
    except OSError:
        raise NotificationSetupError("missing_cli") from None
    argv = build_lark_argv(
        config,
        SETUP_PREVIEW_MESSAGE,
        SETUP_PREVIEW_IDEMPOTENCY_KEY,
        dry_run=True,
        command_prefix=command_prefix,
    )
    effective_runner = subprocess.run if runner is None else runner
    try:
        completed = effective_runner(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout_seconds,
            cwd=REPOSITORY_ROOT,
        )
    except subprocess.TimeoutExpired:
        raise NotificationSetupError("timeout") from None
    except FileNotFoundError:
        raise NotificationSetupError("missing_cli") from None
    except OSError:
        raise NotificationSetupError("cli_error") from None
    returncode = getattr(completed, "returncode", None)
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise NotificationSetupError("invalid_response")
    if returncode != 0:
        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        error_code, _ = classify_lark_failure(returncode, stdout, stderr)
        raise NotificationSetupError(error_code)
    return result


def _write_notification_config_atomic(path: Path, config: NotificationConfig) -> None:
    path = Path(path)
    descriptor = -1
    temporary: Path | None = None
    operation_failed = False
    cleanup_failed = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            json.dump(
                _config_payload(config),
                handle,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        operation_failed = True
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                cleanup_failed = True
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                cleanup_failed = True
    if operation_failed or cleanup_failed:
        raise NotificationPersistenceError("notification config persistence failed") from None


def enable_notification_setup(
    *,
    identity: str,
    destination_type: str,
    destination_id: str,
    preview_digest: str,
    confirm_template: str,
    confirm_privacy: str,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Enable the exact destination choices covered by an approved setup preview."""

    preview_digest = _require_sha256(preview_digest, "preview_digest")
    _require_literal(
        confirm_template,
        frozenset({TEMPLATE_VERSION}),
        "template confirmation",
    )
    _require_literal(
        confirm_privacy,
        frozenset({PRIVACY_CONFIRMATION}),
        "privacy confirmation",
    )
    expected = notification_setup_preview(identity, destination_type, destination_id)
    if not hmac.compare_digest(preview_digest, expected["preview_digest"]):
        raise ValueError("preview_digest does not approve the requested notification setup")
    config = NotificationConfig(
        enabled=True,
        provider=PROVIDER,
        identity=identity,
        destination_type=destination_type,
        destination_id=destination_id,
        approved_template_version=TEMPLATE_VERSION,
    )
    _write_notification_config_atomic(config_path, config)
    return _config_summary(config)


def get_notification_status(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    command_resolver: Callable[[], Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Return sanitized local status without launching any process."""

    config = load_notification_config(config_path)
    resolver = resolve_lark_command if command_resolver is None else command_resolver
    try:
        command_prefix = resolver()
    except OSError:
        command_available = False
    else:
        command_available = (
            isinstance(command_prefix, Sequence)
            and not isinstance(command_prefix, (str, bytes))
            and bool(command_prefix)
            and all(isinstance(argument, str) and bool(argument) for argument in command_prefix)
        )
    return {**_config_summary(config), "command_available": command_available}


def disable_notification_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Disable a valid local config without deleting provider auth or receipts."""

    config = load_notification_config(config_path)
    if config is None:
        return _config_summary(None)
    if config.enabled:
        config = NotificationConfig(
            enabled=False,
            provider=config.provider,
            identity=config.identity,
            destination_type=config.destination_type,
            destination_id=config.destination_id,
            approved_template_version=config.approved_template_version,
        )
        _write_notification_config_atomic(config_path, config)
    return _config_summary(config)


def notification_receipt_summary(
    receipt: dict[str, Any],
    event: UserActionEvent,
) -> dict[str, Any]:
    """Validate and reduce one terminal receipt to privacy-safe fields."""

    if not isinstance(event, UserActionEvent):
        raise TypeError("event must be a UserActionEvent")
    validated = _validate_terminal_receipt(receipt, event)
    if validated is None:
        raise ValueError("notification receipt is not a valid terminal receipt")
    receipt = validated

    allowed = (
        "event_id",
        "template_version",
        "destination_fingerprint",
        "delivery_state",
        "attempt_count",
        "sent_at",
        "message_id",
        "error_code",
    )
    return {field: receipt.get(field) for field in allowed}


def build_user_action_event(
    *,
    input_digest: str,
    project_key: str,
    action_code: str,
    item_ids: Sequence[str],
    prompt_revision: int,
) -> UserActionEvent:
    """Build a canonical event without retaining the caller's raw project key."""

    if not isinstance(project_key, str):
        raise TypeError("project_key must be text")
    if not project_key:
        raise ValueError("project_key must be nonempty")
    try:
        project_key_bytes = project_key.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("project_key must be valid UTF-8 text") from error
    project_label = f"project-{hashlib.sha256(project_key_bytes).hexdigest()[:12]}"
    return UserActionEvent(
        input_digest=input_digest,
        project_label=project_label,
        action_code=action_code,
        item_ids=_canonical_item_ids(item_ids),
        prompt_revision=prompt_revision,
    )


def _receipt_path(receipts_root: Path, event: UserActionEvent) -> Path:
    return Path(receipts_root) / f"{event.digest}.json"


def _valid_utc_z(value: Any) -> bool:
    if not isinstance(value, str) or _UTC_Z_PATTERN.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _validate_terminal_receipt(
    payload: Any,
    event: UserActionEvent,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or set(payload) != _RECEIPT_FIELDS:
        return None
    schema_version = payload["schema_version"]
    attempt_count = payload["attempt_count"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
        or not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count not in {0, 1, 2}
    ):
        return None
    expected_idempotency_digest = hashlib.sha256(event.idempotency_key.encode("utf-8")).hexdigest()
    if (
        payload["event_id"] != event.event_id
        or payload["idempotency_key_digest"] != expected_idempotency_digest
        or payload["template_version"] != TEMPLATE_VERSION
    ):
        return None
    fingerprint = payload["destination_fingerprint"]
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or _SHA256_PATTERN.fullmatch(fingerprint) is None
    ):
        return None
    state = payload["delivery_state"]
    sent_at = payload["sent_at"]
    message_id = payload["message_id"]
    error_code = payload["error_code"]
    if state == "disabled":
        if (
            attempt_count != 0
            or sent_at is not None
            or message_id is not None
            or error_code is not None
        ):
            return None
    elif state == "sent":
        if (
            fingerprint is None
            or attempt_count not in {1, 2}
            or not _valid_utc_z(sent_at)
            or not isinstance(message_id, str)
            or _MESSAGE_ID_PATTERN.fullmatch(message_id) is None
            or error_code is not None
        ):
            return None
    elif state == "failed":
        if (
            fingerprint is None
            or sent_at is not None
            or message_id is not None
            or not isinstance(error_code, str)
            or error_code not in ERROR_CODES
            or (attempt_count == 0 and error_code != "missing_cli")
            or (attempt_count == 1 and error_code in _TRANSIENT_ERROR_CODES)
        ):
            return None
    else:
        return None
    return dict(payload)


def _load_terminal_receipt(
    path: Path,
    event: UserActionEvent,
) -> dict[str, Any] | None:
    try:
        content = path.read_bytes()
        text = content.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except FileNotFoundError:
        return None
    except (UnicodeError, TypeError, ValueError):
        return None
    return _validate_terminal_receipt(payload, event)


def _write_receipt_atomic(path: Path, receipt: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                descriptor = -1
                json.dump(
                    receipt,
                    handle,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        raise NotificationPersistenceError("notification receipt persistence failed") from None


def _event_mutex_name(receipts_root: Path, event_digest: str) -> str:
    canonical_root = os.path.normcase(str(Path(receipts_root).resolve()))
    root_digest = hashlib.sha256(canonical_root.encode("utf-8")).hexdigest()
    return f"Local\\AutoCutUserActionNotifications-{root_digest}-{event_digest}"


def _get_kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


@contextmanager
def _windows_named_mutex(name: str) -> Iterator[None]:
    kernel32 = _get_kernel32()
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise NotificationPersistenceError("notification delivery lock failed")
    acquired = False
    body_failed = False
    cleanup_failed = False
    try:
        wait_result = kernel32.WaitForSingleObject(handle, _INFINITE)
        if wait_result not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
            raise NotificationPersistenceError("notification delivery lock failed")
        acquired = True
        try:
            yield
        except BaseException:
            body_failed = True
            raise
    finally:
        if acquired:
            try:
                if not kernel32.ReleaseMutex(handle):
                    cleanup_failed = True
            except BaseException:
                cleanup_failed = True
        try:
            if not kernel32.CloseHandle(handle):
                cleanup_failed = True
        except BaseException:
            cleanup_failed = True
        if cleanup_failed and not body_failed:
            raise NotificationPersistenceError("notification delivery lock failed") from None


@contextmanager
def _posix_file_lock(root: Path, event_digest: str) -> Iterator[None]:
    import fcntl

    lock_path = root / f".{event_digest}.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _event_delivery_lock(receipts_root: Path, event_digest: str) -> Iterator[None]:
    with _EVENT_LOCKS_GUARD:
        thread_lock = _EVENT_LOCKS.setdefault(event_digest, threading.Lock())
    with thread_lock:
        root = Path(receipts_root)
        root.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            with _windows_named_mutex(_event_mutex_name(root, event_digest)):
                yield
        else:
            with _posix_file_lock(root, event_digest):
                yield


def _base_receipt(
    event: UserActionEvent,
    fingerprint: str | None,
    *,
    delivery_state: str,
    attempt_count: int,
    sent_at: str | None = None,
    message_id: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event.event_id,
        "idempotency_key_digest": hashlib.sha256(event.idempotency_key.encode("utf-8")).hexdigest(),
        "template_version": TEMPLATE_VERSION,
        "destination_fingerprint": fingerprint,
        "delivery_state": delivery_state,
        "attempt_count": attempt_count,
        "sent_at": sent_at,
        "message_id": message_id,
        "error_code": error_code,
    }


def _parse_message_id(stdout: Any) -> str | None:
    if not isinstance(stdout, str):
        return None
    try:
        payload = json.loads(
            stdout,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    message_id = payload.get("message_id")
    if not isinstance(message_id, str) or _MESSAGE_ID_PATTERN.fullmatch(message_id) is None:
        return None
    return message_id


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def deliver_user_action_required(
    event: UserActionEvent,
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    receipts_root: Path = DEFAULT_RECEIPTS_ROOT,
    timeout_seconds: float = 15.0,
    command_resolver: Callable[[], Sequence[str]] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Deliver one optional alert and return a privacy-safe terminal receipt."""

    if not isinstance(event, UserActionEvent):
        raise TypeError("event must be a UserActionEvent")
    receipt_path = _receipt_path(receipts_root, event)
    with _event_delivery_lock(receipts_root, event.digest):
        existing = _load_terminal_receipt(receipt_path, event)
        if existing is not None:
            return existing
        config = load_notification_config(config_path)
        fingerprint = destination_fingerprint(config) if config is not None else None
        if config is None or not config.enabled:
            receipt = _base_receipt(
                event,
                fingerprint,
                delivery_state="disabled",
                attempt_count=0,
            )
            _write_receipt_atomic(receipt_path, receipt)
            return receipt
        resolver = resolve_lark_command if command_resolver is None else command_resolver
        try:
            command_prefix = resolver()
        except OSError:
            receipt = _base_receipt(
                event,
                fingerprint,
                delivery_state="failed",
                attempt_count=0,
                error_code="missing_cli",
            )
            _write_receipt_atomic(receipt_path, receipt)
            return receipt

        message = render_user_action_message(event)
        argv = build_lark_argv(
            config,
            message,
            event.idempotency_key,
            command_prefix=command_prefix,
        )
        effective_runner = subprocess.run if runner is None else runner
        reservation_error = "cli_error"
        for attempt_count in (1, 2):
            reservation = _base_receipt(
                event,
                fingerprint,
                delivery_state="failed",
                attempt_count=attempt_count,
                error_code=reservation_error,
            )
            _write_receipt_atomic(receipt_path, reservation)
            try:
                completed = effective_runner(
                    argv,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=timeout_seconds,
                    cwd=REPOSITORY_ROOT,
                )
            except subprocess.TimeoutExpired:
                error_code, transient = "timeout", True
            except FileNotFoundError:
                error_code, transient = "missing_cli", False
            except OSError:
                error_code, transient = "cli_error", False
            else:
                message_id = (
                    _parse_message_id(completed.stdout) if completed.returncode == 0 else None
                )
                if message_id is not None:
                    receipt = _base_receipt(
                        event,
                        fingerprint,
                        delivery_state="sent",
                        attempt_count=attempt_count,
                        sent_at=_utc_now_z(),
                        message_id=message_id,
                    )
                    _write_receipt_atomic(receipt_path, receipt)
                    return receipt
                error_code, transient = classify_lark_failure(
                    completed.returncode,
                    completed.stdout,
                    completed.stderr,
                )
            if transient and attempt_count == 1:
                reservation_error = error_code
                continue
            receipt = _base_receipt(
                event,
                fingerprint,
                delivery_state="failed",
                attempt_count=attempt_count,
                error_code=error_code,
            )
            _write_receipt_atomic(receipt_path, receipt)
            return receipt
    raise AssertionError("unreachable delivery state")


__all__ = [
    "ACTION_LABELS",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_RECEIPTS_ROOT",
    "ERROR_CODES",
    "FORBIDDEN_PRIVACY_FIELDS",
    "NotificationConfig",
    "NotificationPersistenceError",
    "NotificationSetupError",
    "PRIVACY_CONFIRMATION",
    "PROVIDER",
    "REPOSITORY_ROOT",
    "SCHEMA_VERSION",
    "SETUP_PREVIEW_IDEMPOTENCY_KEY",
    "SETUP_PREVIEW_MESSAGE",
    "TEMPLATE_VERSION",
    "UserActionEvent",
    "build_lark_argv",
    "build_user_action_event",
    "classify_lark_failure",
    "deliver_user_action_required",
    "destination_fingerprint",
    "disable_notification_config",
    "enable_notification_setup",
    "get_notification_status",
    "load_notification_config",
    "notification_receipt_summary",
    "notification_setup_preview",
    "preview_notification_setup",
    "render_user_action_message",
    "resolve_lark_command",
]
