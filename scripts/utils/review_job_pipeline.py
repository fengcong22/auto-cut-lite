"""Verified, content-addressed artifacts for resumable review jobs."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
else:
    import fcntl

_CHUNK_SIZE = 1024 * 1024
_MANIFEST_SCHEMA_VERSION = 1
_JOB_SCHEMA_VERSION = 2
_LEGACY_JOB_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_EXTERNAL_WAIT_FIELDS = frozenset(
    {
        "code",
        "phase",
        "item_ids",
        "input_digest",
        "artifact",
        "artifact_sha256",
        "started_at",
    }
)
_EXTERNAL_WAIT_CODES = frozenset({"awaiting_subject_profile"})
_EXTERNAL_WAIT_PHASES = frozenset({"subject_pointer_profile_gate"})
_EXTERNAL_WAIT_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "job_input_digest",
        "project",
        "items",
        "explicit_identity",
        "profile_check",
        "user_action",
    }
)
_EXTERNAL_WAIT_PROJECT_FIELDS = frozenset({"project_key", "draft_path", "draft_root"})
_EXTERNAL_WAIT_ITEM_FIELDS = frozenset(
    {
        "item_id",
        "source_ledger_id",
        "requested_asset_roles",
        "current_layout_names",
    }
)
_EXTERNAL_WAIT_IDENTITY_FIELDS = frozenset({"stage_name", "subject_name", "stage_id", "subject_id"})
_EXTERNAL_WAIT_PROFILE_FIELDS = frozenset({"status", "missing_items", "problems"})
_EXTERNAL_WAIT_ACTION_FIELDS = frozenset({"action_code", "prompt_revision"})
_EXTERNAL_WAIT_PROFILE_STATUSES = frozenset(
    {"missing", "incomplete", "needs_confirmation", "stale", "ready"}
)
_EXTERNAL_WAIT_ACTIONS = frozenset(
    {"subject_identity", "subject_evidence", "preview_approval", "project_binding"}
)
_EXTERNAL_WAIT_ARTIFACT = "subject_pointer_onboarding.json"
_TRANSACTION_SCHEMA_VERSION = 1
_TRANSACTION_FIELDS = frozenset({"schema_version", "transaction_id", "entries"})
_TRANSACTION_ENTRY_FIELDS = frozenset({"path", "before_exists", "before_sha256", "before_base64"})
_PHASE_STATUSES = frozenset({"pending", "running", "complete", "failed", "skipped"})
_SERIALIZED_RESOURCES = frozenset(
    {"jianying_write", "draft_inspection", "feishu_write", "timeline_repair"}
)
_PHASE_RESOURCES = frozenset({"readonly", *_SERIALIZED_RESOURCES})
_SUCCESSFUL_EXECUTION_STATUSES = frozenset({"complete", "resumed"})
_BLOCKING_EXECUTION_STATUSES = frozenset({"failed", "skipped", "blocked"})
_STATE_LOCKS_GUARD = threading.Lock()
_STATE_LOCKS: dict[Path, threading.RLock] = {}
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Return the lowercase SHA-256 digest of *path* without loading it whole."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_namespace(namespace: str) -> str:
    if not isinstance(namespace, str):
        raise TypeError("cache namespace must be a string")
    if not namespace or namespace != namespace.strip() or namespace in {".", ".."}:
        raise ValueError("cache namespace must be a non-empty safe path component")
    if namespace.endswith(".") or any(character in '<>:"/\\|?*' for character in namespace):
        raise ValueError("cache namespace must be a safe path component")
    if any(ord(character) < 32 for character in namespace):
        raise ValueError("cache namespace cannot contain control characters")
    if namespace.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("cache namespace cannot use a reserved device name")
    return namespace


def _snapshot_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        snapshot: dict[str, Any] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise TypeError("cache identity and JSON payload keys must be strings")
            snapshot[key] = _snapshot_json(nested_value)
        return snapshot
    if isinstance(value, (list, tuple)):
        return [_snapshot_json(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("cache identity and JSON payload numbers must be finite")
        return value
    raise TypeError(f"unsupported cache JSON value: {type(value).__name__}")


def _freeze_json(value: Any) -> Any:
    snapshot = _snapshot_json(value)
    if isinstance(snapshot, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in snapshot.items()})
    if isinstance(snapshot, list):
        return tuple(_freeze_json(item) for item in snapshot)
    return snapshot


def _canonical_json_bytes(value: Any, *, ensure_ascii: bool) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=ensure_ascii,
        allow_nan=False,
    ).encode("utf-8")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value is not allowed: {value}")


def _valid_schema_version(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, str) and bool(value)
    )


@dataclass(frozen=True)
class PhaseDefinition:
    """One dependency-aware unit of review-job work."""

    name: str
    run: Callable[[], Any]
    depends_on: tuple[str, ...] = ()
    resource: str = "readonly"
    item_ids: tuple[str, ...] = ()
    input_digest: str = ""
    retry_count: int = 0
    timeline_interval: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("phase name must be text")
        if not self.name.strip():
            raise ValueError("phase name must not be empty")
        if not callable(self.run):
            raise TypeError("phase run must be callable")
        dependencies = self._string_tuple(self.depends_on, "depends_on")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("depends_on cannot contain duplicate phase names")
        if not isinstance(self.resource, str):
            raise TypeError("phase resource must be text")
        if self.resource not in _PHASE_RESOURCES:
            raise ValueError(f"unsupported phase resource: {self.resource}")
        item_ids = self._string_tuple(self.item_ids, "item_ids")
        if not isinstance(self.input_digest, str):
            raise TypeError("phase input_digest must be text")
        if not isinstance(self.retry_count, int) or isinstance(self.retry_count, bool):
            raise TypeError("phase retry_count must be an integer")
        if self.retry_count < 0:
            raise ValueError("phase retry_count must be non-negative")
        if self.retry_count > 1:
            raise ValueError("phase retry_count must be at most 1")

        interval = self.timeline_interval
        normalized_interval: tuple[float, float] | None = None
        if interval is not None:
            if isinstance(interval, (str, bytes)):
                raise TypeError("timeline interval must contain two numbers")
            try:
                values = tuple(interval)
            except TypeError as error:
                raise TypeError("timeline interval must contain two numbers") from error
            if len(values) != 2 or any(
                not isinstance(value, (int, float)) or isinstance(value, bool) for value in values
            ):
                raise TypeError("timeline interval must contain two numbers")
            start, end = (float(value) for value in values)
            if not math.isfinite(start) or not math.isfinite(end):
                raise ValueError("timeline interval values must be finite")
            if start < 0 or end <= start:
                raise ValueError("timeline interval must have a non-negative positive span")
            normalized_interval = (start, end)

        object.__setattr__(self, "depends_on", dependencies)
        object.__setattr__(self, "item_ids", item_ids)
        object.__setattr__(self, "timeline_interval", normalized_interval)

    @staticmethod
    def _string_tuple(values: Any, label: str) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)):
            raise TypeError(f"{label} must be an iterable of strings")
        try:
            result = tuple(values)
        except TypeError as error:
            raise TypeError(f"{label} must be an iterable of strings") from error
        if not all(isinstance(value, str) for value in result):
            raise TypeError(f"{label} must contain only strings")
        if label == "depends_on" and any(not value.strip() for value in result):
            raise ValueError("depends_on phase names must not be empty")
        return result


@dataclass(frozen=True)
class CacheIdentity:
    """Stable inputs and implementation versions that identify one artifact."""

    namespace: str
    inputs: Mapping[str, Any]
    versions: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespace", _validate_namespace(self.namespace))
        if not isinstance(self.inputs, Mapping) or not isinstance(self.versions, Mapping):
            raise TypeError("cache identity inputs and versions must be mappings")
        object.__setattr__(self, "inputs", _freeze_json(self.inputs))
        object.__setattr__(self, "versions", _freeze_json(self.versions))

    def digest(self) -> str:
        canonical = _canonical_json_bytes(
            _snapshot_json(
                {
                    "namespace": self.namespace,
                    "inputs": self.inputs,
                    "versions": self.versions,
                }
            ),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical).hexdigest()


class ArtifactCache:
    """Store and verify JSON or file artifacts under a caller-owned cache root.

    Store methods return the final cached artifact path. ``get_json`` returns the
    decoded mapping and ``get_file`` returns the verified artifact path; either
    getter returns ``None`` for a cache miss or invalid entry.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)

    def store_json(self, identity: CacheIdentity, payload: Mapping[str, Any]) -> Path:
        if not isinstance(payload, Mapping):
            raise TypeError("JSON cache payload must be a mapping")
        stored_payload = _snapshot_json(payload)
        payload_schema_version = stored_payload.get("schema_version")
        if not _valid_schema_version(payload_schema_version):
            raise ValueError("JSON cache payload requires a valid schema_version")

        artifact_name = "artifact.json"
        artifact_bytes = _canonical_json_bytes(stored_payload, ensure_ascii=False)
        digest = hashlib.sha256(artifact_bytes).hexdigest()
        staging, entry = self._new_staging_entry(identity)
        try:
            self._atomic_write_bytes(staging / artifact_name, artifact_bytes)
            manifest = self._manifest(
                identity=identity,
                artifact_name=artifact_name,
                artifact_type="json",
                size=len(artifact_bytes),
                sha256=digest,
                payload_schema_version=payload_schema_version,
            )
            self._atomic_write_bytes(
                staging / "manifest.json",
                _canonical_json_bytes(manifest, ensure_ascii=True),
            )
            self._publish(staging, entry)
        except Exception:
            self._remove_path(staging)
            raise
        return entry / artifact_name

    def get_json(self, identity: CacheIdentity, schema_version: int | str) -> dict[str, Any] | None:
        if not _valid_schema_version(schema_version):
            raise ValueError("expected schema_version must be a non-empty string or integer")
        loaded = self._verified_artifact(
            identity,
            expected_type="json",
            expected_payload_schema_version=schema_version,
        )
        if loaded is None:
            return None
        entry, artifact_path, _manifest = loaded
        try:
            with open(artifact_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict) or payload.get("schema_version") != schema_version:
                raise ValueError("JSON artifact schema does not match the request")
            _snapshot_json(payload)
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            self._evict(entry)
            return None
        return payload

    def store_file(self, identity: CacheIdentity, source_path: str | os.PathLike[str]) -> Path:
        source = Path(source_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise IsADirectoryError(f"cache source is not a file: {source}")
        artifact_name = self._file_artifact_name(source)
        staging, entry = self._new_staging_entry(identity)
        try:
            size, digest = self._atomic_copy_file(source, staging / artifact_name)
            manifest = self._manifest(
                identity=identity,
                artifact_name=artifact_name,
                artifact_type="file",
                size=size,
                sha256=digest,
            )
            self._atomic_write_bytes(
                staging / "manifest.json",
                _canonical_json_bytes(manifest, ensure_ascii=True),
            )
            self._publish(staging, entry)
        except Exception:
            self._remove_path(staging)
            raise
        return entry / artifact_name

    def get_file(self, identity: CacheIdentity) -> Path | None:
        loaded = self._verified_artifact(identity, expected_type="file")
        if loaded is None:
            return None
        return loaded[1]

    def _manifest(
        self,
        *,
        identity: CacheIdentity,
        artifact_name: str,
        artifact_type: str,
        size: int,
        sha256: str,
        payload_schema_version: int | str | None = None,
    ) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "artifact": artifact_name,
            "identity_digest": identity.digest(),
            "namespace": identity.namespace,
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "sha256": sha256,
            "size": size,
            "type": artifact_type,
        }
        if artifact_type == "json":
            manifest["payload_schema_version"] = payload_schema_version
        return manifest

    def _new_staging_entry(self, identity: CacheIdentity) -> tuple[Path, Path]:
        entry = self._entry_path(identity)
        namespace_directory = entry.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self._require_within_root(self.root)
        if namespace_directory.exists():
            self._require_within_root(namespace_directory)
            if namespace_directory.is_symlink() or not namespace_directory.is_dir():
                raise ValueError("cache namespace path must be a real directory")
        else:
            namespace_directory.mkdir()
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{identity.digest()}.tmp-",
                dir=str(namespace_directory),
            )
        )
        self._require_within_root(staging)
        return staging, entry

    def _entry_path(self, identity: CacheIdentity) -> Path:
        if not isinstance(identity, CacheIdentity):
            raise TypeError("identity must be a CacheIdentity")
        entry = self.root / identity.namespace / identity.digest()
        self._require_within_root(entry)
        return entry

    def _require_within_root(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"cache path escapes root: {path}") from error
        return resolved

    def _verified_artifact(
        self,
        identity: CacheIdentity,
        *,
        expected_type: str,
        expected_payload_schema_version: int | str | None = None,
    ) -> tuple[Path, Path, dict[str, Any]] | None:
        try:
            entry = self._entry_path(identity)
        except (TypeError, ValueError, OSError):
            return None
        if not entry.exists():
            return None
        if entry.is_symlink() or not entry.is_dir():
            self._evict(entry)
            return None

        manifest_path = entry / "manifest.json"
        try:
            self._require_entry_child(entry, manifest_path)
            if manifest_path.is_symlink():
                raise ValueError("cache manifest cannot be a symlink")
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            artifact_path = self._validate_manifest(
                identity,
                entry,
                manifest,
                expected_type=expected_type,
                expected_payload_schema_version=expected_payload_schema_version,
            )
            if artifact_path.is_symlink() or not artifact_path.is_file():
                raise ValueError("cache artifact is missing or not a regular file")
            if artifact_path.stat().st_size != manifest["size"]:
                raise ValueError("cache artifact size differs from manifest")
            if sha256_file(artifact_path) != manifest["sha256"]:
                raise ValueError("cache artifact hash differs from manifest")
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            self._evict(entry)
            return None
        return entry, artifact_path, manifest

    def _validate_manifest(
        self,
        identity: CacheIdentity,
        entry: Path,
        manifest: Any,
        *,
        expected_type: str,
        expected_payload_schema_version: int | str | None,
    ) -> Path:
        if not isinstance(manifest, dict):
            raise ValueError("cache manifest must be a JSON object")
        if manifest.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
            raise ValueError("cache manifest schema version is unsupported")
        if manifest.get("identity_digest") != identity.digest():
            raise ValueError("cache manifest identity digest differs")
        if manifest.get("namespace") != identity.namespace:
            raise ValueError("cache manifest namespace differs")
        if manifest.get("type") != expected_type:
            raise ValueError("cache artifact type differs")
        if expected_type == "json" and (
            manifest.get("payload_schema_version") != expected_payload_schema_version
        ):
            raise ValueError("cache JSON schema version differs")

        size = manifest.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("cache manifest size is invalid")
        expected_hash = manifest.get("sha256")
        if not isinstance(expected_hash, str) or not _SHA256_PATTERN.fullmatch(expected_hash):
            raise ValueError("cache manifest SHA-256 is invalid")
        artifact_name = manifest.get("artifact")
        if not isinstance(artifact_name, str) or not artifact_name:
            raise ValueError("cache manifest artifact path is invalid")
        artifact_path = entry / artifact_name
        self._require_entry_child(entry, artifact_path)
        return artifact_path

    def _require_entry_child(self, entry: Path, path: Path) -> Path:
        resolved_entry = self._require_within_root(entry)
        resolved_path = self._require_within_root(path)
        if resolved_path.parent != resolved_entry:
            raise ValueError("cache artifact path must remain directly inside its entry")
        return resolved_path

    def _publish(self, staging: Path, entry: Path) -> None:
        backup = entry.with_name(f".{entry.name}.bak-{uuid.uuid4().hex}")
        had_previous = entry.exists() or entry.is_symlink()
        moved_previous = False
        try:
            if had_previous:
                os.replace(entry, backup)
                moved_previous = True
            os.replace(staging, entry)
        except Exception:
            if moved_previous and not entry.exists():
                os.replace(backup, entry)
            raise
        else:
            if moved_previous:
                self._remove_path(backup)
        finally:
            self._remove_path(staging)

    def _atomic_write_bytes(self, destination: Path, content: bytes) -> None:
        temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
        try:
            with open(temporary, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            self._remove_path(temporary)

    def _atomic_copy_file(self, source: Path, destination: Path) -> tuple[int, str]:
        temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
        digest = hashlib.sha256()
        size = 0
        try:
            with open(source, "rb") as source_handle, open(temporary, "xb") as output_handle:
                while chunk := source_handle.read(_CHUNK_SIZE):
                    output_handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            os.replace(temporary, destination)
        finally:
            self._remove_path(temporary)
        return size, digest.hexdigest()

    @staticmethod
    def _file_artifact_name(source: Path) -> str:
        suffix = source.suffix
        if suffix and re.fullmatch(r"\.[A-Za-z0-9._-]{1,32}", suffix):
            return f"artifact{suffix}"
        return "artifact.bin"

    def _evict(self, entry: Path) -> None:
        try:
            if entry.is_symlink():
                if entry.parent.resolve(strict=False).is_relative_to(self.root):
                    entry.unlink(missing_ok=True)
                return
            self._require_within_root(entry)
        except (OSError, ValueError):
            return
        self._remove_path(entry)

    @staticmethod
    def _remove_path(path: Path) -> None:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _state_lock(path: Path) -> threading.RLock:
    with _STATE_LOCKS_GUARD:
        return _STATE_LOCKS.setdefault(path, threading.RLock())


class JobStateStore:
    """Persist resumable phase state at *path* and timing beside it.

    ``path`` names the state file itself (normally ``job_state.json``). Timing
    telemetry is written to ``job_timing.json`` in the same directory.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        input_digest: str,
        tool_version: str,
        *,
        monotonic: Callable[[], float] | None = None,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        if self.path.exists() and self.path.is_dir():
            raise IsADirectoryError(f"job state path is a directory: {self.path}")
        self.timing_path = self.path.with_name("job_timing.json")
        if self.path == self.timing_path:
            raise ValueError("job state path must not be named job_timing.json")
        self.transaction_path = self.path.with_name(f"{self.path.name}.transaction.json")
        state_identity = os.path.normcase(str(self.path)).encode("utf-8")
        self._mutex_name = (
            "Local\\AutoCut.JobState." f"{hashlib.sha256(state_identity).hexdigest()}"
        )
        self.input_digest = self._require_text(input_digest, "job input digest")
        self.tool_version = self._require_text(tool_version, "tool version")
        self._monotonic = monotonic or time.monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._lock = _state_lock(self.path)
        self._phase_started_monotonic: dict[str, float] = {}
        self._phase_carried_active_seconds: dict[str, float] = {}
        self._phase_carried_wait_seconds: dict[str, float] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            self._reload_locked()
            if self._requires_initial_persist():
                self._persist_locked()

    def start_phase(
        self,
        name: str,
        input_digest: str | None = None,
        item_ids: tuple[str, ...] | list[str] = (),
        retry_count: int = 0,
    ) -> dict[str, Any]:
        phase_name = self._require_text(name, "phase name")
        phase_input = (
            self.input_digest
            if input_digest is None
            else self._require_text(input_digest, "phase input digest")
        )
        phase_items = self._item_ids(item_ids)
        retries = self._retry_count(retry_count)
        with self._locked():
            self._reload_locked()
            previous = self._valid_phase(self._state["phases"].get(phase_name))
            carried_active = (
                previous["active_seconds"]
                if previous is not None and previous["status"] == "pending"
                else 0.0
            )
            carried_wait = (
                previous["wait_seconds"]
                if previous is not None and previous["status"] == "pending"
                else 0.0
            )
            started = self._monotonic_value()
            record = self._phase_record_template(
                status="running",
                input_digest=phase_input,
                item_ids=phase_items,
                retry_count=retries,
                started_at=self._utc_timestamp(),
            )
            record.update(
                {
                    "active_seconds": carried_active,
                    "elapsed_seconds": carried_active + carried_wait,
                    "wait_seconds": carried_wait,
                }
            )
            self._state["phases"][phase_name] = record
            self._timing["phases"][phase_name] = _snapshot_json(record)
            self._phase_started_monotonic[phase_name] = started
            self._phase_carried_active_seconds[phase_name] = carried_active
            self._phase_carried_wait_seconds[phase_name] = carried_wait
            self._persist_locked()
            return _snapshot_json(record)

    def complete_phase(
        self,
        name: str,
        output_digest: str | None,
        cache_hit: bool | None,
    ) -> dict[str, Any]:
        if output_digest is not None:
            output_digest = self._require_text(output_digest, "phase output digest")
        if cache_hit is not None and not isinstance(cache_hit, bool):
            raise TypeError("cache_hit must be a boolean or None")
        return self._finish_phase(
            name,
            status="complete",
            output_digest=output_digest,
            cache_hit=cache_hit,
            error=None,
        )

    def fail_phase(self, name: str, error: str | BaseException) -> dict[str, Any]:
        if isinstance(error, BaseException):
            error_text = f"{type(error).__name__}: {error}"
        elif isinstance(error, str):
            error_text = error
        else:
            raise TypeError("phase error must be text or an exception")
        if not error_text:
            error_text = "phase failed"
        return self._finish_phase(
            name,
            status="failed",
            output_digest=None,
            cache_hit=None,
            error=error_text,
        )

    def skip_phase(
        self,
        name: str,
        error: str,
        input_digest: str | None = None,
        item_ids: tuple[str, ...] | list[str] = (),
        retry_count: int = 0,
    ) -> dict[str, Any]:
        """Persist a dependency-blocked phase without marking it as executed."""

        phase_name = self._require_text(name, "phase name")
        reason = self._require_text(error, "phase skip reason")
        phase_input = (
            self.input_digest
            if input_digest is None
            else self._require_text(input_digest, "phase input digest")
        )
        phase_items = self._item_ids(item_ids)
        retries = self._retry_count(retry_count)
        with self._locked():
            self._reload_locked()
            record = self._phase_record_template(
                status="skipped",
                input_digest=phase_input,
                item_ids=phase_items,
                retry_count=retries,
            )
            record.update({"error": reason, "finished_at": self._utc_timestamp()})
            self._state["phases"][phase_name] = record
            self._timing["phases"][phase_name] = _snapshot_json(record)
            self._persist_locked()
            return _snapshot_json(record)

    def add_wait_seconds(self, name: str, seconds: float) -> dict[str, Any]:
        phase_name = self._require_text(name, "phase name")
        wait_increment = self._duration(seconds, "wait duration")
        with self._locked():
            self._reload_locked()
            record = self._running_phase_locked(phase_name)
            wait_seconds = record["wait_seconds"] + wait_increment
            elapsed_seconds = record["active_seconds"] + wait_seconds
            record.update(
                {
                    "elapsed_seconds": elapsed_seconds,
                    "wait_seconds": wait_seconds,
                }
            )
            self._timing["phases"][phase_name] = _snapshot_json(record)
            self._persist_locked()
            return _snapshot_json(record)

    def can_resume(
        self,
        name: str,
        input_digest: str | None = None,
        tool_version: str | None = None,
    ) -> bool:
        phase_name = self._require_text(name, "phase name")
        expected_input = (
            self.input_digest
            if input_digest is None
            else self._require_text(input_digest, "phase input digest")
        )
        expected_tool = (
            self.tool_version
            if tool_version is None
            else self._require_text(tool_version, "tool version")
        )
        with self._locked():
            self._reload_locked()
            record = self._valid_phase(self._state["phases"].get(phase_name))
            return bool(
                record is not None
                and record["status"] == "complete"
                and expected_tool == self.tool_version
                and record["tool_version"] == expected_tool
                and record["input_digest"] == expected_input
                and bool(record["output_digest"])
                and self._matches_job_identity(self._state)
            )

    def status(self, name: str) -> str:
        phase_name = self._require_text(name, "phase name")
        with self._locked():
            self._reload_locked()
            record = self._valid_phase(self._state["phases"].get(phase_name))
            if record is None or record["tool_version"] != self.tool_version:
                return "pending"
            return record["status"]

    def get_phase(self, name: str) -> dict[str, Any] | None:
        phase_name = self._require_text(name, "phase name")
        with self._locked():
            self._reload_locked()
            record = self._valid_phase(self._state["phases"].get(phase_name))
            if record is None:
                return None
            return _snapshot_json(record)

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            self._reload_locked()
            return _snapshot_json(self._state)

    def timing_snapshot(self) -> dict[str, Any]:
        with self._locked():
            self._reload_locked()
            return _snapshot_json(self._timing)

    @property
    def state(self) -> dict[str, Any]:
        return self.snapshot()

    @property
    def timing(self) -> dict[str, Any]:
        return self.timing_snapshot()

    def begin_external_wait(
        self,
        *,
        code: str,
        phase: str,
        item_ids: Sequence[str],
        input_digest: str,
        artifact: str,
        artifact_payload: Mapping[str, Any],
        replaces_input_digest: str | None = None,
        replaces_artifact_sha256: str | None = None,
    ) -> dict[str, Any]:
        wait_code = self._require_external_literal(code, _EXTERNAL_WAIT_CODES, "wait code")
        wait_phase = self._require_external_literal(phase, _EXTERNAL_WAIT_PHASES, "wait phase")
        wait_items = self._external_item_ids(item_ids)
        wait_input = self._require_sha256(input_digest, "wait input digest")
        artifact_path, artifact_name = self._external_artifact_path(artifact)
        payload = self._validate_external_wait_payload(
            artifact_payload,
            input_digest=wait_input,
            item_ids=wait_items,
        )
        artifact_bytes = self._json_document_bytes(payload)
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()

        with self._locked():
            self._reload_locked()
            artifact_path, artifact_name = self._external_artifact_path(artifact)
            current = self._state["external_wait"]
            if current is None:
                if replaces_input_digest is not None or replaces_artifact_sha256 is not None:
                    raise ValueError("replacement digests require an active external wait")
                if payload["user_action"]["prompt_revision"] != 1:
                    raise ValueError("the first external wait prompt revision must be 1")
                started_at = self._utc_timestamp()
            else:
                current_payload, current_bytes = self._read_external_artifact_locked(current)
                if (
                    current["input_digest"] == wait_input
                    and current["artifact"] == artifact_name
                    and current_bytes == artifact_bytes
                ):
                    return _snapshot_json(current)
                if replaces_input_digest is None or replaces_artifact_sha256 is None:
                    raise ValueError("replacement requires both active external wait digests")
                replacement_input = self._require_sha256(
                    replaces_input_digest, "replacement input digest"
                )
                replacement_artifact = self._require_sha256(
                    replaces_artifact_sha256, "replacement artifact digest"
                )
                if (
                    replacement_input != current["input_digest"]
                    or replacement_artifact != current["artifact_sha256"]
                ):
                    raise ValueError("replacement digests do not match the active external wait")
                current_revision = current_payload["user_action"]["prompt_revision"]
                next_revision = payload["user_action"]["prompt_revision"]
                same_question = self._material_wait_question(
                    current_payload
                ) == self._material_wait_question(payload)
                expected_revision = current_revision if same_question else current_revision + 1
                if next_revision != expected_revision:
                    raise ValueError(
                        "external wait prompt revision must be preserved for an unchanged "
                        "question or incremented exactly once for a changed question"
                    )
                started_at = current["started_at"]

            wait = {
                "code": wait_code,
                "phase": wait_phase,
                "item_ids": list(wait_items),
                "input_digest": wait_input,
                "artifact": artifact_name,
                "artifact_sha256": artifact_sha256,
                "started_at": started_at,
            }
            state = _snapshot_json(self._state)
            timing = _snapshot_json(self._timing)
            state["external_wait"] = _snapshot_json(wait)
            timing["external_wait"] = _snapshot_json(wait)
            self._transactional_publish_locked(
                (
                    (artifact_path, payload),
                    (self.timing_path, timing),
                    (self.path, state),
                )
            )
            self._state = state
            self._timing = timing
            return _snapshot_json(wait)

    def get_external_wait(
        self,
        *,
        verify_artifact: bool = True,
    ) -> dict[str, Any] | None:
        if not isinstance(verify_artifact, bool):
            raise TypeError("verify_artifact must be a boolean")
        with self._locked():
            self._reload_locked()
            wait = self._state["external_wait"]
            if wait is None:
                return None
            if verify_artifact:
                self._read_external_artifact_locked(wait)
            return _snapshot_json(wait)

    def get_external_wait_user_action_context(self) -> dict[str, Any]:
        """Return notification inputs from the current verified wait sidecar."""

        with self._locked():
            self._reload_locked()
            wait = self._state["external_wait"]
            if wait is None:
                raise ValueError("no active external wait")
            payload, _ = self._read_external_artifact_locked(wait)
            return {
                "wait": _snapshot_json(wait),
                "job_input_digest": payload["job_input_digest"],
                "project_key": payload["project"]["project_key"],
                "item_ids": [item["item_id"] for item in payload["items"]],
                "action_code": payload["user_action"]["action_code"],
                "prompt_revision": payload["user_action"]["prompt_revision"],
            }

    def resolve_external_wait(
        self,
        *,
        input_digest: str,
        artifact_sha256: str,
        project_key: str,
        draft_path: str,
        draft_root: str,
    ) -> dict[str, Any]:
        expected_input = self._require_sha256(input_digest, "wait input digest")
        expected_artifact = self._require_sha256(artifact_sha256, "wait artifact digest")
        expected_project_key = self._require_text(project_key, "project key")
        expected_draft_path = self._normalized_physical_path(draft_path, "project draft path")
        expected_draft_root = self._normalized_physical_path(draft_root, "project draft root")

        with self._locked():
            self._reload_locked()
            wait = self._state["external_wait"]
            if wait is None:
                raise ValueError("no active external wait to resolve")
            payload, _ = self._read_external_artifact_locked(wait)
            if (
                expected_input != wait["input_digest"]
                or expected_artifact != wait["artifact_sha256"]
            ):
                raise ValueError("wait digests do not match the active external wait")
            project = payload["project"]
            if (
                expected_project_key != project["project_key"]
                or expected_draft_path
                != self._normalized_physical_path(
                    project["draft_path"], "sidecar project draft path"
                )
                or expected_draft_root
                != self._normalized_physical_path(
                    project["draft_root"], "sidecar project draft root"
                )
            ):
                raise ValueError("current project identity does not match the external wait")

            resolved_at = self._utc_datetime()
            started_at = self._parse_utc_timestamp(wait["started_at"])
            wait_duration = max(0.0, (resolved_at - started_at).total_seconds())
            existing = self._state["phases"].get(wait["phase"])
            if existing is None:
                phase_record = self._phase_record_template(
                    status="pending",
                    input_digest=wait["input_digest"],
                    item_ids=wait["item_ids"],
                )
            else:
                phase_record = self._valid_phase(existing)
                if phase_record is not None and phase_record["status"] == "complete":
                    carried_active = phase_record["active_seconds"]
                    carried_wait = phase_record["wait_seconds"]
                    phase_record = self._phase_record_template(
                        status="pending",
                        input_digest=wait["input_digest"],
                        item_ids=wait["item_ids"],
                    )
                    phase_record.update(
                        {
                            "active_seconds": carried_active,
                            "elapsed_seconds": carried_active + carried_wait,
                            "wait_seconds": carried_wait,
                        }
                    )
                if phase_record is None or phase_record["status"] != "pending":
                    raise RuntimeError(
                        "external wait phase must be pending before it can be resolved"
                    )
            phase_record["wait_seconds"] += wait_duration
            phase_record["elapsed_seconds"] += wait_duration

            state = _snapshot_json(self._state)
            timing = _snapshot_json(self._timing)
            state["phases"][wait["phase"]] = _snapshot_json(phase_record)
            timing["phases"][wait["phase"]] = _snapshot_json(phase_record)
            state["external_wait"] = None
            timing["external_wait"] = None
            self._transactional_publish_locked(((self.timing_path, timing), (self.path, state)))
            self._state = state
            self._timing = timing
            return _snapshot_json(phase_record)

    def _finish_phase(
        self,
        name: str,
        *,
        status: str,
        output_digest: str | None,
        cache_hit: bool | None,
        error: str | None,
    ) -> dict[str, Any]:
        phase_name = self._require_text(name, "phase name")
        with self._locked():
            self._reload_locked()
            record = self._running_phase_locked(phase_name)
            finished = self._monotonic_value()
            started = self._phase_started_monotonic.get(phase_name)
            measured_elapsed = (
                max(0.0, finished - started) if started is not None else record["elapsed_seconds"]
            )
            wait_seconds = record["wait_seconds"]
            carried_active = self._phase_carried_active_seconds.get(phase_name, 0.0)
            carried_wait = self._phase_carried_wait_seconds.get(phase_name, 0.0)
            runtime_wait = max(0.0, wait_seconds - carried_wait)
            runtime_elapsed = max(measured_elapsed, runtime_wait)
            active_seconds = carried_active + max(0.0, runtime_elapsed - runtime_wait)
            elapsed_seconds = active_seconds + wait_seconds
            record.update(
                {
                    "active_seconds": active_seconds,
                    "cache_hit": cache_hit,
                    "elapsed_seconds": elapsed_seconds,
                    "error": error,
                    "finished_at": self._utc_timestamp(),
                    "output_digest": output_digest,
                    "status": status,
                }
            )
            self._timing["phases"][phase_name] = _snapshot_json(record)
            self._persist_locked()
            self._phase_started_monotonic.pop(phase_name, None)
            self._phase_carried_active_seconds.pop(phase_name, None)
            self._phase_carried_wait_seconds.pop(phase_name, None)
            return _snapshot_json(record)

    def _running_phase_locked(self, name: str) -> dict[str, Any]:
        candidate = self._state["phases"].get(name)
        record = self._valid_phase(candidate)
        if record is None or record["status"] != "running":
            raise RuntimeError(f"phase is not running: {name}")
        return candidate

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            with self._process_lock():
                self._recover_transaction_locked()
                yield

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = (
                ctypes.c_void_p,
                wintypes.BOOL,
                wintypes.LPCWSTR,
            )
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.CreateMutexW(None, False, self._mutex_name)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
                if result not in {0x00000000, 0x00000080}:
                    if result == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    raise RuntimeError(f"unexpected named-mutex wait result: {result}")
                try:
                    yield
                finally:
                    if not kernel32.ReleaseMutex(handle):
                        raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel32.CloseHandle(handle)
        else:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path.parent, flags)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError("job state parent must be a physical directory")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _requires_initial_persist(self) -> bool:
        state = self._read_payload(self.path)
        timing = self._read_payload(self.timing_path)
        return not (
            self._valid_top_level(state)
            and self._valid_top_level(timing)
            and state["schema_version"] == _JOB_SCHEMA_VERSION
            and timing["schema_version"] == _JOB_SCHEMA_VERSION
            and self._matches_job_identity(state)
            and self._matches_job_identity(timing)
            and state == self._state
            and timing == self._timing
        )

    def _reload_locked(self) -> None:
        state_payload = self._recover_payload(self.path)
        if state_payload is None or not self._matches_job_identity(state_payload):
            state_payload = self._new_payload()
        state = self._normalize_payload(state_payload)

        timing_payload = self._recover_payload(self.timing_path)
        if timing_payload is None or not self._matches_job_identity(timing_payload):
            timing_payload = self._new_payload()
        timing = self._normalize_payload(timing_payload)
        if state["external_wait"] != timing["external_wait"]:
            raise ValueError("job state and timing external_wait values do not match")
        timing["phases"] = _snapshot_json(state["phases"])
        self._state = state
        self._timing = timing

        running_names = {
            name for name, record in self._state["phases"].items() if record["status"] == "running"
        }
        for name in tuple(self._phase_started_monotonic):
            if name not in running_names:
                self._phase_started_monotonic.pop(name, None)
                self._phase_carried_active_seconds.pop(name, None)
                self._phase_carried_wait_seconds.pop(name, None)

    def _recover_payload(self, path: Path) -> dict[str, Any] | None:
        temporary = self._temporary_path(path)
        final_payload = self._read_payload(path)
        if self._valid_top_level(final_payload):
            self._remove_temporary(temporary)
            return final_payload

        temporary_payload = self._read_payload(temporary)
        if self._valid_top_level(temporary_payload) and self._matches_job_identity(
            temporary_payload
        ):
            os.replace(temporary, path)
            return temporary_payload
        self._remove_temporary(temporary)
        return None

    @staticmethod
    def _read_payload(path: Path) -> dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _normalize_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = self._new_payload()
        schema_version = payload.get("schema_version")
        if schema_version == _LEGACY_JOB_SCHEMA_VERSION:
            if "external_wait" in payload:
                raise ValueError("schema-v1 job state cannot contain external_wait")
        elif schema_version == _JOB_SCHEMA_VERSION:
            if "external_wait" not in payload:
                raise ValueError("schema-v2 job state requires external_wait")
            external_wait = payload.get("external_wait")
            normalized["external_wait"] = (
                None if external_wait is None else self._validate_external_wait(external_wait)
            )
        else:
            raise ValueError("unsupported job state schema version")
        phases = payload.get("phases", {})
        if not isinstance(phases, dict):
            return normalized
        for name, candidate in phases.items():
            if not isinstance(name, str) or not name:
                continue
            record = self._valid_phase(candidate)
            normalized["phases"][name] = (
                record if record is not None else self._phase_record_template(status="pending")
            )
        return normalized

    def _new_payload(self) -> dict[str, Any]:
        return {
            "external_wait": None,
            "input_digest": self.input_digest,
            "phases": {},
            "schema_version": _JOB_SCHEMA_VERSION,
            "tool_version": self.tool_version,
        }

    def _phase_record_template(
        self,
        *,
        status: str,
        input_digest: str | None = None,
        item_ids: list[str] | None = None,
        retry_count: int = 0,
        started_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "active_seconds": 0.0,
            "cache_hit": None,
            "elapsed_seconds": 0.0,
            "error": None,
            "finished_at": None,
            "input_digest": self.input_digest if input_digest is None else input_digest,
            "item_ids": [] if item_ids is None else list(item_ids),
            "output_digest": None,
            "retry_count": retry_count,
            "started_at": started_at,
            "status": status,
            "tool_version": self.tool_version,
            "wait_seconds": 0.0,
        }

    def _validate_external_wait(self, candidate: Any) -> dict[str, Any]:
        wait = self._closed_mapping(candidate, _EXTERNAL_WAIT_FIELDS, "external_wait")
        wait["code"] = self._require_external_literal(
            wait["code"], _EXTERNAL_WAIT_CODES, "wait code"
        )
        wait["phase"] = self._require_external_literal(
            wait["phase"], _EXTERNAL_WAIT_PHASES, "wait phase"
        )
        wait["item_ids"] = self._external_item_ids(wait["item_ids"])
        wait["input_digest"] = self._require_sha256(wait["input_digest"], "wait input digest")
        _, wait["artifact"] = self._external_artifact_path(wait["artifact"])
        wait["artifact_sha256"] = self._require_sha256(
            wait["artifact_sha256"], "wait artifact digest"
        )
        started_at = wait["started_at"]
        if not isinstance(started_at, str) or not self._valid_utc_timestamp(started_at):
            raise ValueError("external wait started_at must be a UTC timestamp")
        return wait

    @classmethod
    def _validate_external_wait_payload(
        cls,
        candidate: Mapping[str, Any],
        *,
        input_digest: str,
        item_ids: Sequence[str],
    ) -> dict[str, Any]:
        payload = cls._closed_mapping(
            candidate, _EXTERNAL_WAIT_PAYLOAD_FIELDS, "external wait sidecar"
        )
        if (
            not isinstance(payload["schema_version"], int)
            or isinstance(payload["schema_version"], bool)
            or payload["schema_version"] != 1
        ):
            raise ValueError("external wait sidecar schema_version must be 1")
        payload_input = cls._require_sha256(payload["job_input_digest"], "sidecar job input digest")
        if payload_input != input_digest:
            raise ValueError("sidecar job input digest does not match the external wait")

        project = cls._closed_mapping(
            payload["project"], _EXTERNAL_WAIT_PROJECT_FIELDS, "sidecar project"
        )
        project["project_key"] = cls._require_text(project["project_key"], "sidecar project key")
        normalized_draft_path = cls._normalized_physical_path(
            project["draft_path"], "sidecar project draft path"
        )
        normalized_draft_root = cls._normalized_physical_path(
            project["draft_root"], "sidecar project draft root"
        )
        try:
            Path(normalized_draft_path).relative_to(Path(normalized_draft_root))
        except ValueError as error:
            raise ValueError("sidecar project draft path must be within draft root") from error
        project["draft_path"] = normalized_draft_path
        project["draft_root"] = normalized_draft_root
        payload["project"] = project

        raw_items = payload["items"]
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("sidecar items must be a non-empty array")
        normalized_items: list[dict[str, Any]] = []
        sidecar_item_ids: list[str] = []
        for index, raw_item in enumerate(raw_items):
            item = cls._closed_mapping(
                raw_item, _EXTERNAL_WAIT_ITEM_FIELDS, f"sidecar item {index}"
            )
            item_id = cls._require_text(item["item_id"], "sidecar item_id")
            source_ledger_id = item["source_ledger_id"]
            if not isinstance(source_ledger_id, str):
                raise TypeError("sidecar source_ledger_id must be text or empty")
            item["item_id"] = item_id
            item["source_ledger_id"] = source_ledger_id
            item["requested_asset_roles"] = cls._string_list(
                item["requested_asset_roles"],
                "sidecar requested_asset_roles",
                unique=True,
            )
            item["current_layout_names"] = cls._string_list(
                item["current_layout_names"],
                "sidecar current_layout_names",
                unique=True,
            )
            sidecar_item_ids.append(item_id)
            normalized_items.append(item)
        if len(set(sidecar_item_ids)) != len(sidecar_item_ids):
            raise ValueError("sidecar item_ids must not contain duplicates")
        if sidecar_item_ids != list(item_ids):
            raise ValueError("sidecar item_ids must exactly match the external wait item_ids")
        payload["items"] = normalized_items

        identity = cls._closed_mapping(
            payload["explicit_identity"],
            _EXTERNAL_WAIT_IDENTITY_FIELDS,
            "sidecar explicit_identity",
        )
        for field in _EXTERNAL_WAIT_IDENTITY_FIELDS:
            value = identity[field]
            if value is not None:
                identity[field] = cls._require_text(value, f"sidecar identity {field}")
        identity_complete = all(identity[field] is not None for field in identity)
        payload["explicit_identity"] = identity

        profile_candidate = payload["profile_check"]
        if profile_candidate is None:
            if identity_complete:
                raise ValueError("a complete identity requires a profile_check")
            profile_check = None
            expected_action = "subject_identity"
        else:
            if not identity_complete:
                raise ValueError("profile_check requires a complete explicit identity")
            profile_check = cls._closed_mapping(
                profile_candidate,
                _EXTERNAL_WAIT_PROFILE_FIELDS,
                "sidecar profile_check",
            )
            status = cls._require_external_literal(
                profile_check["status"],
                _EXTERNAL_WAIT_PROFILE_STATUSES,
                "profile status",
            )
            missing_items = cls._string_list(
                profile_check["missing_items"], "profile missing_items"
            )
            problems = cls._string_list(profile_check["problems"], "profile problems")
            profile_check.update(
                {"status": status, "missing_items": missing_items, "problems": problems}
            )
            if status in {"missing", "incomplete", "stale"}:
                expected_action = "subject_evidence"
            elif status == "needs_confirmation" and any(
                item.startswith("confirmed_scale_references:") for item in missing_items
            ):
                expected_action = "subject_evidence"
            elif status == "needs_confirmation":
                if missing_items != ["approved_previews:0"]:
                    raise ValueError("preview approval action requires exactly approved_previews:0")
                expected_action = "preview_approval"
            else:
                if missing_items:
                    raise ValueError("a ready profile cannot have missing_items")
                expected_action = "project_binding"
        payload["profile_check"] = profile_check

        action = cls._closed_mapping(
            payload["user_action"], _EXTERNAL_WAIT_ACTION_FIELDS, "sidecar user_action"
        )
        action_code = cls._require_external_literal(
            action["action_code"], _EXTERNAL_WAIT_ACTIONS, "user action code"
        )
        if action_code != expected_action:
            raise ValueError("user action does not match the blocking profile state")
        prompt_revision = action["prompt_revision"]
        if not isinstance(prompt_revision, int) or isinstance(prompt_revision, bool):
            raise TypeError("prompt revision must be an integer")
        if prompt_revision < 1:
            raise ValueError("prompt revision must be positive")
        action.update({"action_code": action_code, "prompt_revision": prompt_revision})
        payload["user_action"] = action
        return payload

    @staticmethod
    def _material_wait_question(payload: Mapping[str, Any]) -> dict[str, Any]:
        material = _snapshot_json(payload)
        material["user_action"].pop("prompt_revision")
        return material

    def _read_external_artifact_locked(
        self, wait: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bytes]:
        artifact_path, _ = self._external_artifact_path(wait["artifact"], require_regular=True)
        try:
            artifact_bytes = artifact_path.read_bytes()
        except FileNotFoundError:
            raise FileNotFoundError(
                f"external wait artifact is missing: {wait['artifact']}"
            ) from None
        if hashlib.sha256(artifact_bytes).hexdigest() != wait["artifact_sha256"]:
            raise ValueError("external wait artifact hash mismatch")
        try:
            candidate = json.loads(
                artifact_bytes.decode("utf-8"), parse_constant=_reject_json_constant
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("external wait artifact is not valid JSON") from error
        try:
            payload = self._validate_external_wait_payload(
                candidate,
                input_digest=wait["input_digest"],
                item_ids=wait["item_ids"],
            )
        except (TypeError, ValueError) as error:
            raise ValueError("external wait artifact payload is invalid") from error
        return payload, artifact_bytes

    def _external_artifact_path(
        self,
        artifact: Any,
        *,
        require_regular: bool = False,
    ) -> tuple[Path, str]:
        artifact_text = self._require_text(artifact, "external wait artifact")
        relative = Path(artifact_text)
        if (
            relative.is_absolute()
            or relative.drive
            or relative.anchor
            or relative == Path(".")
            or any(part == ".." for part in relative.parts)
        ):
            raise ValueError("external wait artifact must be a safe relative path")
        root = self.path.parent
        candidate = root / relative
        for path in (candidate, *candidate.parents):
            if path == root:
                break
            self._reject_reparse_path(path, "external wait artifact")
        resolved = candidate.resolve(strict=False)
        try:
            normalized_relative = resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("external wait artifact resolves outside the job directory") from error
        if artifact_text != _EXTERNAL_WAIT_ARTIFACT:
            raise ValueError(f"external wait artifact must be {_EXTERNAL_WAIT_ARTIFACT}")
        forbidden = {
            self.path,
            self.timing_path,
            self._temporary_path(self.path),
            self._temporary_path(self.timing_path),
        }
        if resolved in forbidden:
            raise ValueError("external wait artifact conflicts with job state files")
        if require_regular:
            if not os.path.lexists(resolved):
                raise FileNotFoundError(
                    f"external wait artifact is missing: {normalized_relative.as_posix()}"
                )
            if resolved.is_symlink():
                raise ValueError("external wait artifact cannot be a symlink")
            if not resolved.is_file():
                raise IsADirectoryError(f"external wait artifact is not a regular file: {resolved}")
        elif os.path.lexists(resolved) and (resolved.is_symlink() or not resolved.is_file()):
            raise ValueError("external wait artifact must be a regular non-symlink file")
        return resolved, normalized_relative.as_posix()

    @classmethod
    def _normalized_physical_path(cls, value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{label} must be text")
        if not value:
            raise ValueError(f"{label} must not be empty")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{label} must be absolute")
        current = Path(path.anchor)
        for part in path.parts[1:]:
            if part == ".":
                continue
            if part == "..":
                current = current.parent
                continue
            current /= part
            cls._reject_reparse_path(current, label)
        resolved = path.resolve(strict=False)
        for candidate in (resolved, *resolved.parents):
            cls._reject_reparse_path(candidate, label)
        return os.path.normcase(str(resolved))

    @staticmethod
    def _reject_reparse_path(path: Path, label: str) -> None:
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            return
        file_attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(metadata.st_mode) or file_attributes & reparse_flag:
            raise ValueError(f"{label} cannot contain a symlink, junction, or reparse point")

    @staticmethod
    def _closed_mapping(value: Any, expected_fields: frozenset[str], label: str) -> dict[str, Any]:
        snapshot = _snapshot_json(value)
        if not isinstance(snapshot, dict):
            raise TypeError(f"{label} must be an object")
        if set(snapshot) != expected_fields:
            raise ValueError(f"{label} must contain exactly the approved fields")
        return snapshot

    @staticmethod
    def _string_list(value: Any, label: str, *, unique: bool = False) -> list[str]:
        if not isinstance(value, list):
            raise TypeError(f"{label} must be an array")
        if not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"{label} must contain only non-empty strings")
        if unique and len(set(value)) != len(value):
            raise ValueError(f"{label} must not contain duplicates")
        return list(value)

    @classmethod
    def _external_item_ids(cls, values: Sequence[str]) -> list[str]:
        if isinstance(values, (str, bytes)):
            raise TypeError("external wait item_ids must be a sequence of strings")
        try:
            result = list(values)
        except TypeError as error:
            raise TypeError("external wait item_ids must be a sequence of strings") from error
        if not result or not all(isinstance(item, str) and item for item in result):
            raise ValueError("external wait item_ids must contain non-empty strings")
        if len(set(result)) != len(result):
            raise ValueError("external wait item_ids must not contain duplicates")
        return result

    @staticmethod
    def _require_sha256(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{label} must be text")
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        return value

    @staticmethod
    def _require_external_literal(value: Any, allowed: frozenset[str], label: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{label} must be text")
        if value not in allowed:
            raise ValueError(f"unsupported {label}: {value}")
        return value

    def _valid_phase(self, candidate: Any) -> dict[str, Any] | None:
        if not isinstance(candidate, dict):
            return None
        required = {
            "active_seconds",
            "cache_hit",
            "elapsed_seconds",
            "error",
            "finished_at",
            "input_digest",
            "item_ids",
            "output_digest",
            "retry_count",
            "started_at",
            "status",
            "tool_version",
            "wait_seconds",
        }
        if not required.issubset(candidate):
            return None
        status = candidate["status"]
        if status not in _PHASE_STATUSES:
            return None
        if candidate["cache_hit"] is not None and not isinstance(candidate["cache_hit"], bool):
            return None
        if (
            not isinstance(candidate["retry_count"], int)
            or isinstance(candidate["retry_count"], bool)
            or not 0 <= candidate["retry_count"] <= 1
        ):
            return None
        if not isinstance(candidate["item_ids"], list) or not all(
            isinstance(item_id, str) for item_id in candidate["item_ids"]
        ):
            return None
        if not isinstance(candidate["tool_version"], str):
            return None
        for field in ("input_digest", "output_digest", "error", "started_at", "finished_at"):
            if candidate[field] is not None and not isinstance(candidate[field], str):
                return None
        durations: dict[str, float] = {}
        for field in ("elapsed_seconds", "active_seconds", "wait_seconds"):
            try:
                durations[field] = self._duration(candidate[field], field)
            except (TypeError, ValueError):
                return None
        if not math.isclose(
            durations["active_seconds"] + durations["wait_seconds"],
            durations["elapsed_seconds"],
            abs_tol=1e-9,
        ):
            return None
        if status == "pending" and (
            candidate["started_at"] is not None or candidate["finished_at"] is not None
        ):
            return None
        if status == "running" and (
            candidate["started_at"] is None or candidate["finished_at"] is not None
        ):
            return None
        if status in {"complete", "failed"} and (
            candidate["started_at"] is None or candidate["finished_at"] is None
        ):
            return None
        if status == "skipped" and (
            candidate["started_at"] is not None
            or candidate["finished_at"] is None
            or not candidate["error"]
        ):
            return None
        if not all(
            timestamp is None or self._valid_utc_timestamp(timestamp)
            for timestamp in (candidate["started_at"], candidate["finished_at"])
        ):
            return None
        try:
            _snapshot_json(candidate)
            normalized = {field: _snapshot_json(candidate[field]) for field in required}
        except (TypeError, ValueError):
            return None
        normalized.update(durations)
        return normalized

    @staticmethod
    def _valid_top_level(candidate: Any) -> bool:
        return bool(
            isinstance(candidate, dict)
            and isinstance(candidate.get("schema_version"), int)
            and not isinstance(candidate.get("schema_version"), bool)
            and candidate.get("schema_version") in {_LEGACY_JOB_SCHEMA_VERSION, _JOB_SCHEMA_VERSION}
            and isinstance(candidate.get("input_digest"), str)
            and isinstance(candidate.get("tool_version"), str)
            and isinstance(candidate.get("phases"), dict)
        )

    @staticmethod
    def _valid_utc_timestamp(value: str) -> bool:
        try:
            JobStateStore._parse_utc_timestamp(value)
        except (TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _parse_utc_timestamp(value: str) -> datetime:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError("timestamp must use the UTC Z suffix")
        try:
            parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
        except ValueError as error:
            raise ValueError("timestamp must be valid ISO-8601 UTC") from error
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError("timestamp must be UTC")
        return parsed

    def _matches_job_identity(self, candidate: Mapping[str, Any]) -> bool:
        return bool(
            candidate.get("input_digest") == self.input_digest
            and candidate.get("tool_version") == self.tool_version
        )

    def _persist_locked(self) -> None:
        self._transactional_publish_locked(
            ((self.timing_path, self._timing), (self.path, self._state))
        )

    def _transactional_publish_locked(
        self, writes: Sequence[tuple[Path, Mapping[str, Any]]]
    ) -> None:
        ordered_writes = tuple(writes)
        if not ordered_writes:
            return
        journal = self._transaction_journal_locked(ordered_writes)
        try:
            self._atomic_write_json(self.transaction_path, journal)
            for destination, payload in ordered_writes:
                self._atomic_write_json(destination, payload)
        except BaseException:
            try:
                self._recover_transaction_locked()
            except BaseException as rollback_error:
                raise RuntimeError(
                    "external wait publish failed and byte rollback was incomplete"
                ) from rollback_error
            raise
        self._remove_transaction_journal_locked()

    def _transaction_journal_locked(
        self, writes: Sequence[tuple[Path, Mapping[str, Any]]]
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for destination, _ in writes:
            target, relative = self._transaction_target_path(
                destination.relative_to(self.path.parent).as_posix()
            )
            if target in seen:
                raise ValueError(f"transaction target is duplicated: {relative}")
            seen.add(target)
            before = self._file_snapshot(target)
            entries.append(
                {
                    "path": relative,
                    "before_exists": before is not None,
                    "before_sha256": (
                        None if before is None else hashlib.sha256(before).hexdigest()
                    ),
                    "before_base64": (
                        None if before is None else base64.b64encode(before).decode("ascii")
                    ),
                }
            )
        self._validate_transaction_target_set(seen)
        return {
            "schema_version": _TRANSACTION_SCHEMA_VERSION,
            "transaction_id": uuid.uuid4().hex,
            "entries": entries,
        }

    def _recover_transaction_locked(self) -> None:
        if not os.path.lexists(self.transaction_path):
            self._cleanup_transaction_temps_locked()
            return
        self._reject_reparse_path(self.transaction_path, "job transaction journal")
        if not self.transaction_path.is_file():
            raise ValueError("job transaction journal must be a regular non-symlink file")
        try:
            candidate = json.loads(
                self.transaction_path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("job transaction journal is invalid JSON") from error
        journal = self._closed_mapping(candidate, _TRANSACTION_FIELDS, "job transaction journal")
        if journal["schema_version"] != _TRANSACTION_SCHEMA_VERSION:
            raise ValueError("job transaction journal has an unsupported schema_version")
        transaction_id = journal["transaction_id"]
        if (
            not isinstance(transaction_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None
        ):
            raise ValueError("job transaction journal has an invalid transaction_id")
        raw_entries = journal["entries"]
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError("job transaction journal entries must be a non-empty array")
        snapshots: list[tuple[Path, bytes | None]] = []
        seen: set[Path] = set()
        for raw_entry in raw_entries:
            entry = self._closed_mapping(
                raw_entry, _TRANSACTION_ENTRY_FIELDS, "job transaction entry"
            )
            target, _ = self._transaction_target_path(entry["path"])
            if target in seen:
                raise ValueError("job transaction journal contains duplicate targets")
            seen.add(target)
            before_exists = entry["before_exists"]
            if not isinstance(before_exists, bool):
                raise TypeError("job transaction before_exists must be a boolean")
            encoded = entry["before_base64"]
            expected_sha256 = entry["before_sha256"]
            if not before_exists:
                if encoded is not None or expected_sha256 is not None:
                    raise ValueError("absent transaction snapshots cannot contain bytes")
                before = None
            else:
                if not isinstance(encoded, str):
                    raise TypeError("transaction snapshot bytes must be base64 text")
                expected_sha256 = self._require_sha256(
                    expected_sha256, "transaction snapshot digest"
                )
                try:
                    before = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as error:
                    raise ValueError("transaction snapshot bytes are invalid base64") from error
                if hashlib.sha256(before).hexdigest() != expected_sha256:
                    raise ValueError("transaction snapshot digest mismatch")
            snapshots.append((target, before))
        self._validate_transaction_target_set(seen)
        self._cleanup_target_temps_locked(
            (*tuple(target for target, _ in snapshots), self.transaction_path)
        )
        for target, before in reversed(snapshots):
            self._restore_file_snapshot(target, before)
        self._remove_transaction_journal_locked()

    def _transaction_target_path(self, value: Any) -> tuple[Path, str]:
        if not isinstance(value, str) or not value:
            raise TypeError("job transaction target path must be non-empty text")
        relative = Path(value)
        if (
            relative.is_absolute()
            or relative.drive
            or relative.anchor
            or relative == Path(".")
            or any(part == ".." for part in relative.parts)
        ):
            raise ValueError("job transaction target must be a safe relative path")
        lexical_target = self.path.parent
        for part in relative.parts:
            lexical_target /= part
            self._reject_reparse_path(lexical_target, "job transaction target")
        target = lexical_target.resolve(strict=False)
        try:
            normalized_relative = target.relative_to(self.path.parent)
        except ValueError as error:
            raise ValueError("job transaction target resolves outside the job directory") from error
        approved = {
            self.path,
            self.timing_path,
            self.path.with_name(_EXTERNAL_WAIT_ARTIFACT),
        }
        if target not in approved:
            raise ValueError("job transaction target is not an approved job artifact")
        return target, normalized_relative.as_posix()

    def _validate_transaction_target_set(self, targets: set[Path]) -> None:
        required = {self.path, self.timing_path}
        with_sidecar = required | {self.path.with_name(_EXTERNAL_WAIT_ARTIFACT)}
        if targets != required and targets != with_sidecar:
            raise ValueError("job transaction target set is incomplete or unsupported")

    def _remove_transaction_journal_locked(self) -> None:
        try:
            self.transaction_path.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(self.transaction_path.parent)

    def _cleanup_transaction_temps_locked(self) -> None:
        self._cleanup_target_temps_locked((self.transaction_path,))

    @staticmethod
    def _cleanup_target_temps_locked(targets: Iterator[Path] | Sequence[Path]) -> None:
        temporary_files: list[Path] = []
        seen_targets: set[Path] = set()
        for target in targets:
            if target in seen_targets:
                continue
            seen_targets.add(target)
            prefix = f"{target.name}.tmp-"
            try:
                candidates = tuple(target.parent.iterdir())
            except FileNotFoundError:
                continue
            for candidate in candidates:
                if (
                    not candidate.name.startswith(prefix)
                    or re.fullmatch(r"[0-9a-f]{32}", candidate.name[len(prefix) :]) is None
                ):
                    continue
                try:
                    metadata = os.lstat(candidate)
                except FileNotFoundError:
                    continue
                file_attributes = getattr(metadata, "st_file_attributes", 0)
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or file_attributes & reparse_flag
                    or not stat.S_ISREG(metadata.st_mode)
                ):
                    raise ValueError(
                        "job transaction temporary path must be a regular non-reparse file"
                    )
                temporary_files.append(candidate)
        for temporary in temporary_files:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _file_snapshot(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def _restore_file_snapshot(self, path: Path, snapshot: bytes | None) -> None:
        if snapshot is None:
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                if not path.is_file():
                    raise IsADirectoryError(f"rollback target is not a file: {path}")
                path.unlink()
            self._fsync_directory(path.parent)
            return
        self._atomic_write_bytes(path, snapshot)

    @staticmethod
    def _json_document_bytes(payload: Mapping[str, Any]) -> bytes:
        return _canonical_json_bytes(payload, ensure_ascii=False) + b"\n"

    def _atomic_write_json(self, destination: Path, payload: Mapping[str, Any]) -> None:
        self._atomic_write_bytes(destination, self._json_document_bytes(payload))

    def _atomic_write_bytes(self, destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f"{destination.name}.tmp-{uuid.uuid4().hex}")
        try:
            with open(temporary, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self._remove_temporary(temporary)
            raise
        os.replace(temporary, destination)
        self._fsync_directory(destination.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _temporary_path(destination: Path) -> Path:
        return destination.with_name(f"{destination.name}.tmp")

    def _remove_temporary(self, path: Path) -> None:
        try:
            path.parent.resolve(strict=False).relative_to(self.path.parent)
        except ValueError:
            return
        try:
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                if path.is_dir():
                    raise IsADirectoryError(f"job temporary path is a directory: {path}")
                path.unlink()
        except FileNotFoundError:
            pass

    def _utc_timestamp(self) -> str:
        return self._utc_datetime().isoformat().replace("+00:00", "Z")

    def _utc_datetime(self) -> datetime:
        current = self._utcnow()
        if not isinstance(current, datetime):
            raise TypeError("utcnow must return a datetime")
        if current.tzinfo is None:
            raise ValueError("utcnow must return a timezone-aware datetime")
        return current.astimezone(timezone.utc)

    def _monotonic_value(self) -> float:
        value = self._monotonic()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("monotonic clock must return a number")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("monotonic clock must return a finite number")
        return result

    @staticmethod
    def _duration(value: Any, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{label} must be a number")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError(f"{label} must be finite and non-negative")
        return result

    @staticmethod
    def _require_text(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{label} must be text")
        if not value:
            raise ValueError(f"{label} must not be empty")
        return value

    @staticmethod
    def _item_ids(values: tuple[str, ...] | list[str]) -> list[str]:
        if isinstance(values, (str, bytes)):
            raise TypeError("item_ids must be an iterable of strings")
        try:
            result = list(values)
        except TypeError as error:
            raise TypeError("item_ids must be an iterable of strings") from error
        if not all(isinstance(item_id, str) for item_id in result):
            raise TypeError("item_ids must contain only strings")
        return result

    @staticmethod
    def _retry_count(value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("retry_count must be an integer")
        if value < 0:
            raise ValueError("retry_count must be non-negative")
        if value > 1:
            raise ValueError("retry_count must be at most 1")
        return value


class PhaseTimer:
    """Time one phase and persist its completion or failure on context exit."""

    def __init__(
        self,
        store: JobStateStore,
        phase: str,
        item_ids: tuple[str, ...] | list[str] = (),
        input_digest: str | None = None,
        retry_count: int = 0,
    ) -> None:
        if not isinstance(store, JobStateStore):
            raise TypeError("store must be a JobStateStore")
        self.store = store
        self.phase = phase
        self.item_ids = item_ids
        self.input_digest = input_digest
        self.retry_count = retry_count
        self._entered = False
        self._finished = False
        self._output_digest: str | None = None
        self._cache_hit: bool | None = None

    def __enter__(self) -> PhaseTimer:
        if self._entered:
            raise RuntimeError("phase timer cannot be entered more than once")
        self.store.start_phase(
            self.phase,
            input_digest=self.input_digest,
            item_ids=self.item_ids,
            retry_count=self.retry_count,
        )
        self._entered = True
        return self

    def complete(self, output_digest: str | None, cache_hit: bool | None) -> None:
        self._require_active()
        self._output_digest = output_digest
        self._cache_hit = cache_hit

    def set_result(self, output_digest: str | None, cache_hit: bool | None) -> None:
        self.complete(output_digest, cache_hit)

    def add_wait_seconds(self, seconds: float) -> None:
        self._require_active()
        self.store.add_wait_seconds(self.phase, seconds)

    @contextmanager
    def wait(self) -> Iterator[None]:
        self._require_active()
        started = self.store._monotonic_value()
        try:
            yield
        finally:
            finished = self.store._monotonic_value()
            self.add_wait_seconds(max(0.0, finished - started))

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if not self._entered or self._finished:
            return False
        self._finished = True
        if exc is not None:
            try:
                self.store.fail_phase(self.phase, exc)
            except Exception:
                pass
            return False
        self.store.complete_phase(
            self.phase,
            output_digest=self._output_digest,
            cache_hit=self._cache_hit,
        )
        return False

    def _require_active(self) -> None:
        if not self._entered or self._finished:
            raise RuntimeError("phase timer is not active")


class ReviewJobExecutor:
    """Run a validated phase DAG with bounded, resource-safe concurrency."""

    def __init__(
        self,
        max_workers: int = 3,
        state_store: JobStateStore | None = None,
    ) -> None:
        if not isinstance(max_workers, int) or isinstance(max_workers, bool):
            raise TypeError("max_workers must be an integer")
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.max_workers = max_workers
        self.state_store = state_store

    def run(self, phases: Any) -> dict[str, dict[str, Any]]:
        """Run *phases* and return one stable status record per input phase."""

        if isinstance(phases, (str, bytes)):
            raise TypeError("phases must be an iterable of PhaseDefinition values")
        try:
            definitions = tuple(phases)
        except TypeError as error:
            raise TypeError("phases must be an iterable of PhaseDefinition values") from error
        self._preflight(definitions)

        records = {phase.name: self._record("pending") for phase in definitions}
        pending = set(range(len(definitions)))
        wait_blocked: set[int] = set()
        if self.state_store is not None:
            external_wait = self.state_store.get_external_wait()
            if external_wait is not None:
                wait_blocked = self._external_wait_blocked_indices(
                    definitions,
                    external_wait["phase"],
                )
                for index in sorted(wait_blocked):
                    if definitions[index].name != external_wait["phase"]:
                        continue
                    records[definitions[index].name] = self._record("waiting")
                    pending.remove(index)
        self._mark_resumable(definitions, pending, records)

        running: dict[Future[Any], int] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            while pending or running:
                progressed = self._mark_resumable(definitions, pending, records)
                progressed = (
                    self._skip_failed_dependents(
                        definitions,
                        pending,
                        records,
                        excluded_indices=wait_blocked,
                    )
                    or progressed
                )
                ready = self._ready_phase_indices(definitions, pending, records)
                available_workers = self.max_workers - len(running)
                serialized_running = any(
                    definitions[index].resource in _SERIALIZED_RESOURCES
                    for index in running.values()
                )

                for index in ready:
                    if available_workers == 0:
                        break
                    phase = definitions[index]
                    is_serialized = phase.resource in _SERIALIZED_RESOURCES
                    if is_serialized and serialized_running:
                        continue
                    phase_input_digest = self._phase_input_digest(definitions, index)
                    future = pool.submit(self._run_phase, phase, phase_input_digest)
                    running[future] = index
                    pending.remove(index)
                    available_workers -= 1
                    serialized_running = serialized_running or is_serialized
                    progressed = True

                if running:
                    completed, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in sorted(completed, key=lambda item: running[item]):
                        index = running.pop(future)
                        try:
                            records[definitions[index].name] = future.result()
                        except Exception as error:
                            records[definitions[index].name] = self._record(
                                "failed",
                                error=self._safe_error(error),
                            )
                    continue

                if pending and not progressed:
                    if pending.issubset(wait_blocked):
                        break
                    for index in sorted(pending):
                        phase = definitions[index]
                        reason = "scheduler made no progress"
                        records[phase.name] = self._record("blocked", error=reason)
                        self._persist_skip(phase, reason)
                    pending.clear()

        return {phase.name: records[phase.name] for phase in definitions}

    @classmethod
    def _external_wait_blocked_indices(
        cls,
        definitions: tuple[PhaseDefinition, ...],
        wait_phase: str,
    ) -> set[int]:
        blocked_names = {wait_phase}
        blocked_indices: set[int] = set()
        changed = True
        while changed:
            changed = False
            for index, phase in enumerate(definitions):
                if phase.name in blocked_names:
                    blocked_indices.add(index)
                    continue
                if any(
                    prerequisite in blocked_names
                    for prerequisite in cls._prerequisite_names(definitions, index)
                ):
                    blocked_names.add(phase.name)
                    blocked_indices.add(index)
                    changed = True
        return blocked_indices

    @staticmethod
    def _preflight(definitions: tuple[Any, ...]) -> None:
        if not all(isinstance(phase, PhaseDefinition) for phase in definitions):
            raise TypeError("phases must contain only PhaseDefinition values")

        names = [phase.name for phase in definitions]
        seen: set[str] = set()
        duplicates: list[str] = []
        for name in names:
            if name in seen and name not in duplicates:
                duplicates.append(name)
            seen.add(name)
        if duplicates:
            raise ValueError(f"duplicate phase names: {', '.join(duplicates)}")

        known_names = set(names)
        for phase in definitions:
            if phase.name in phase.depends_on:
                raise ValueError(f"phase cannot depend on itself: {phase.name}")
            missing = [name for name in phase.depends_on if name not in known_names]
            if missing:
                raise ValueError(
                    f"phase {phase.name} has missing dependencies: {', '.join(missing)}"
                )

        remaining_dependencies: dict[str, set[str]] = {}
        previous_feishu_write: str | None = None
        for phase in definitions:
            dependencies = set(phase.depends_on)
            if phase.resource == "feishu_write":
                if previous_feishu_write is not None:
                    dependencies.add(previous_feishu_write)
                previous_feishu_write = phase.name
            remaining_dependencies[phase.name] = dependencies
        ready = [name for name in names if not remaining_dependencies[name]]
        visited: set[str] = set()
        while ready:
            name = ready.pop(0)
            if name in visited:
                continue
            visited.add(name)
            for candidate in names:
                dependencies = remaining_dependencies[candidate]
                if name in dependencies:
                    dependencies.remove(name)
                    if not dependencies:
                        ready.append(candidate)
        if len(visited) != len(definitions):
            cycle_names = [name for name in names if name not in visited]
            raise ValueError(f"dependency cycle detected: {', '.join(cycle_names)}")

        repairs: list[PhaseDefinition] = []
        for phase in definitions:
            if phase.resource != "timeline_repair":
                continue
            if phase.timeline_interval is None:
                raise ValueError(f"timeline_repair phase requires timeline_interval: {phase.name}")
            for previous in repairs:
                assert previous.timeline_interval is not None
                start, end = phase.timeline_interval
                previous_start, previous_end = previous.timeline_interval
                if start < previous_end and previous_start < end:
                    raise ValueError(
                        "timeline repair intervals overlap between "
                        f"{previous.name} and {phase.name}"
                    )
            repairs.append(phase)

    def _mark_resumable(
        self,
        definitions: tuple[PhaseDefinition, ...],
        pending: set[int],
        records: dict[str, dict[str, Any]],
    ) -> bool:
        if self.state_store is None:
            return False
        progressed = False
        for index, phase in enumerate(definitions):
            if index not in pending:
                continue
            prerequisites = self._prerequisite_names(definitions, index)
            if any(
                records[prerequisite]["status"] not in _SUCCESSFUL_EXECUTION_STATUSES
                for prerequisite in prerequisites
            ):
                continue
            try:
                phase_input_digest = self._phase_input_digest(definitions, index)
                can_resume = self.state_store.can_resume(
                    phase.name,
                    input_digest=phase_input_digest,
                )
            except Exception as error:
                records[phase.name] = self._record(
                    "failed",
                    error=f"state resume check failed: {self._safe_error(error)}",
                )
                pending.remove(index)
                progressed = True
                continue
            if can_resume:
                records[phase.name] = self._record("resumed")
                pending.remove(index)
                progressed = True
        return progressed

    def _skip_failed_dependents(
        self,
        definitions: tuple[PhaseDefinition, ...],
        pending: set[int],
        records: dict[str, dict[str, Any]],
        *,
        excluded_indices: set[int] | frozenset[int] = frozenset(),
    ) -> bool:
        progressed = False
        for index in sorted(tuple(pending)):
            if index in excluded_indices:
                continue
            phase = definitions[index]
            failed_dependencies = [
                dependency
                for dependency in phase.depends_on
                if records[dependency]["status"] in _BLOCKING_EXECUTION_STATUSES
            ]
            if failed_dependencies:
                reason = f"blocked by failed dependency: {', '.join(failed_dependencies)}"
            else:
                previous_feishu_write = self._previous_feishu_write(definitions, index)
                if (
                    previous_feishu_write is None
                    or records[previous_feishu_write]["status"] not in _BLOCKING_EXECUTION_STATUSES
                ):
                    continue
                reason = "blocked by earlier feishu_write: " f"{previous_feishu_write}"
            records[phase.name] = self._record("skipped", error=reason)
            self._persist_skip(phase, reason)
            pending.remove(index)
            progressed = True
        return progressed

    def _ready_phase_indices(
        self,
        definitions: tuple[PhaseDefinition, ...],
        pending: set[int],
        records: dict[str, dict[str, Any]],
    ) -> list[int]:
        return [
            index
            for index in sorted(pending)
            if all(
                records[dependency]["status"] in _SUCCESSFUL_EXECUTION_STATUSES
                for dependency in definitions[index].depends_on
            )
            and self._feishu_order_is_ready(definitions, index, records)
        ]

    @classmethod
    def _feishu_order_is_ready(
        cls,
        definitions: tuple[PhaseDefinition, ...],
        index: int,
        records: dict[str, dict[str, Any]],
    ) -> bool:
        previous = cls._previous_feishu_write(definitions, index)
        return bool(
            previous is None or records[previous]["status"] in _SUCCESSFUL_EXECUTION_STATUSES
        )

    @staticmethod
    def _previous_feishu_write(
        definitions: tuple[PhaseDefinition, ...],
        index: int,
    ) -> str | None:
        if definitions[index].resource != "feishu_write":
            return None
        for previous_index in range(index - 1, -1, -1):
            previous = definitions[previous_index]
            if previous.resource == "feishu_write":
                return previous.name
        return None

    @classmethod
    def _prerequisite_names(
        cls,
        definitions: tuple[PhaseDefinition, ...],
        index: int,
    ) -> tuple[str, ...]:
        prerequisites = list(definitions[index].depends_on)
        previous_feishu_write = cls._previous_feishu_write(definitions, index)
        if previous_feishu_write is not None and previous_feishu_write not in prerequisites:
            prerequisites.append(previous_feishu_write)
        return tuple(prerequisites)

    def _phase_input_digest(
        self,
        definitions: tuple[PhaseDefinition, ...],
        index: int,
    ) -> str | None:
        phase = definitions[index]
        if self.state_store is None:
            return phase.input_digest or None
        prerequisites = self._prerequisite_names(definitions, index)
        if not prerequisites:
            return phase.input_digest or None

        prerequisite_outputs: list[dict[str, str]] = []
        for prerequisite in prerequisites:
            record = self.state_store.get_phase(prerequisite)
            output_digest = record.get("output_digest") if record is not None else None
            if not isinstance(output_digest, str) or not output_digest:
                raise RuntimeError(f"prerequisite phase lacks an output digest: {prerequisite}")
            prerequisite_outputs.append({"name": prerequisite, "output_digest": output_digest})
        payload = {
            "phase_input_digest": phase.input_digest or self.state_store.input_digest,
            "prerequisite_outputs": prerequisite_outputs,
        }
        return hashlib.sha256(_canonical_json_bytes(payload, ensure_ascii=True)).hexdigest()

    def _run_phase(
        self,
        phase: PhaseDefinition,
        input_digest: str | None = None,
    ) -> dict[str, Any]:
        started = False
        if self.state_store is not None:
            try:
                self.state_store.start_phase(
                    phase.name,
                    input_digest=input_digest,
                    item_ids=phase.item_ids,
                    retry_count=phase.retry_count,
                )
                started = True
            except Exception as error:
                return self._record(
                    "failed",
                    error=f"state start failed: {self._safe_error(error)}",
                )

        try:
            result = phase.run()
        except Exception as error:
            error_text = self._safe_error(error)
            if self.state_store is not None and started:
                try:
                    self.state_store.fail_phase(phase.name, error_text)
                except Exception:
                    pass
            return self._record("failed", error=error_text)

        try:
            output_digest = self._result_digest(result)
        except Exception as error:
            error_text = self._safe_error(error)
            if self.state_store is not None and started:
                try:
                    self.state_store.fail_phase(phase.name, error_text)
                except Exception:
                    pass
            return self._record("failed", error=error_text)
        if self.state_store is not None:
            try:
                self.state_store.complete_phase(
                    phase.name,
                    output_digest=output_digest,
                    cache_hit=False,
                )
            except Exception as error:
                error_text = f"state completion failed: {self._safe_error(error)}"
                if started:
                    try:
                        self.state_store.fail_phase(phase.name, error_text)
                    except Exception:
                        pass
                return self._record("failed", result=result, error=error_text)
        return self._record("complete", result=result)

    def _persist_skip(self, phase: PhaseDefinition, reason: str) -> None:
        if self.state_store is None:
            return
        try:
            self.state_store.skip_phase(
                phase.name,
                error=reason,
                input_digest=phase.input_digest or None,
                item_ids=phase.item_ids,
                retry_count=phase.retry_count,
            )
        except Exception:
            pass

    @staticmethod
    def _result_digest(result: Any) -> str:
        try:
            payload = _canonical_json_bytes(_snapshot_json(result), ensure_ascii=True)
        except (TypeError, ValueError) as error:
            result_type = type(result)
            qualified_name = f"{result_type.__module__}.{result_type.__qualname__}"
            raise TypeError(
                f"unsupported phase result without a content identity: {qualified_name}"
            ) from error
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        message = " ".join(str(error).splitlines()).strip() or "phase failed"
        if len(message) > 300:
            message = f"{message[:297]}..."
        return f"{type(error).__name__}: {message}"

    @staticmethod
    def _record(
        status: str,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {"status": status, "result": result, "error": error}


__all__ = [
    "ArtifactCache",
    "CacheIdentity",
    "JobStateStore",
    "PhaseDefinition",
    "PhaseTimer",
    "ReviewJobExecutor",
    "sha256_file",
]
