from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .errors import PortableProjectError
from .hashing import canonical_json_sha256, sha256_file
from .topology import resolve_timeline_topology, validate_timeline_directories

SNAPSHOT_RECEIPT_FILENAME = "autocut_snapshot_receipt.json"
SNAPSHOT_SCHEMA_VERSION = 2
_REQUIRED_SNAPSHOT_FILES = ("draft_content.json", "draft_meta_info.json")
_STABLE_IDENTITY_FILES = (
    "draft_virtual_store.json",
    "timeline_layout.json",
    "Timelines/project.json",
)
_REPARSE_POINT_ATTRIBUTE = 0x400


@dataclass(frozen=True)
class SnapshotCapture:
    receipt: dict[str, Any]
    seal_token: str


@dataclass(frozen=True)
class VerifiedSnapshot:
    receipt: dict[str, Any]
    documents: dict[str, dict[str, Any]]
    active_timeline_id: str
    timeline_ids: tuple[str, ...]


def _reject_untrusted(reason: str, data: Mapping[str, Any] | None = None) -> None:
    raise PortableProjectError("untrusted_snapshot", reason, dict(data or {}))


def _read_regular_file_bytes(path: Path, *, label: str) -> bytes:
    if not _is_regular_file(path):
        _reject_untrusted(f"{label} is missing or is not a regular file", {"file": path.name})
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            attributes = int(getattr(before, "st_file_attributes", 0))
            if not stat.S_ISREG(before.st_mode) or attributes & _REPARSE_POINT_ATTRIBUTE:
                _reject_untrusted(f"{label} is not a regular file", {"file": path.name})
            payload = handle.read()
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise PortableProjectError(
            "untrusted_snapshot", f"{label} could not be read", {"file": path.name}
        ) from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or len(payload) != before.st_size:
        _reject_untrusted(f"{label} changed while it was read", {"file": path.name})
    return payload


def _decode_json_object(payload_bytes: bytes, *, label: str, file_name: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableProjectError(
            "untrusted_snapshot", f"{label} is not readable JSON", {"file": file_name}
        ) from exc
    if not isinstance(payload, dict):
        raise PortableProjectError(
            "untrusted_snapshot", f"{label} is not a JSON object", {"file": file_name}
        )
    return payload


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    return _decode_json_object(
        _read_regular_file_bytes(path, label=label),
        label=label,
        file_name=path.name,
    )


def _is_regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0))
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not (attributes & _REPARSE_POINT_ATTRIBUTE)
    )


def _safe_relative_path(value: str) -> Path:
    text = str(value or "").replace("\\", "/")
    parts = text.split("/")
    if (
        not text
        or text.startswith("/")
        or text.startswith("//")
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
    ):
        _reject_untrusted("Snapshot receipt contains an unsafe file path", {"path": text})
    return Path(*parts)


def _contained_path(root: Path, relative: Path) -> Path:
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        _reject_untrusted("Snapshot receipt path escapes its root", {"path": relative.as_posix()})
    return candidate


def _regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.name == SNAPSHOT_RECEIPT_FILENAME:
            continue
        try:
            info = path.lstat()
        except OSError as exc:
            _reject_untrusted("Could not inspect a snapshot file", {"path": path.name})
            raise AssertionError("unreachable") from exc
        attributes = int(getattr(info, "st_file_attributes", 0))
        if stat.S_ISLNK(info.st_mode) or attributes & _REPARSE_POINT_ATTRIBUTE:
            _reject_untrusted("Snapshot cannot contain reparse points", {"path": path.name})
        if stat.S_ISREG(info.st_mode):
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _normalized_job_digest(value: str) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise PortableProjectError(
            "untrusted_snapshot", "Snapshot capture requires a 64-character job digest"
        )
    return digest


def _normalized_receipt_digest(value: str) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        _reject_untrusted("Snapshot verification requires a trusted receipt SHA-256")
    return digest


def _source_path_digest(source_draft_dir: Path) -> str:
    normalized = os.path.normcase(str(source_draft_dir.resolve())).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _read_optional_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return _read_json_object(path, label=path.name)


def _declared_timeline_ids(
    layout: Mapping[str, Any], project: Mapping[str, Any], root_content: Mapping[str, Any]
) -> tuple[str, tuple[str, ...]]:
    topology = resolve_timeline_topology(
        layout,
        project,
        root_content,
        error_code="untrusted_snapshot",
    )
    return topology.active_timeline_id, topology.declared_timeline_ids


def _snapshot_documents(payloads: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for relative, payload in payloads.items():
        if not relative.casefold().endswith(".json"):
            continue
        documents[relative] = _decode_json_object(
            payload,
            label=f"snapshot {relative}",
            file_name=relative,
        )
    return documents


def _snapshot_identity_from_documents(
    documents: Mapping[str, Mapping[str, Any]], source_draft_dir: Path
) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    root_content = documents.get("draft_content.json")
    meta = documents.get("draft_meta_info.json")
    if not isinstance(root_content, Mapping) or not isinstance(meta, Mapping):
        _reject_untrusted("Snapshot is missing required readable documents")
    layout = documents.get("timeline_layout.json", {})
    project = documents.get("Timelines/project.json", {})
    if not isinstance(layout, Mapping) or not isinstance(project, Mapping):
        _reject_untrusted("Snapshot timeline metadata is not a JSON object")
    active_id, timeline_ids = _declared_timeline_ids(layout, project, root_content)
    actual_timeline_ids = {
        parts[1]
        for relative in documents
        if (parts := relative.split("/"))[:1] == ["Timelines"]
        and len(parts) == 3
        and parts[2] == "draft_content.json"
    }
    undeclared = sorted(actual_timeline_ids - set(timeline_ids))
    if undeclared:
        _reject_untrusted(
            "Snapshot contains timeline content not declared by its project metadata",
            {"timeline_ids": undeclared},
        )
    root_id = str(root_content.get("id") or "").strip()
    if active_id and root_id != active_id:
        _reject_untrusted("Snapshot root timeline ID does not match the active timeline")
    for timeline_id in timeline_ids:
        relative = f"Timelines/{timeline_id}/draft_content.json"
        content = documents.get(relative)
        if not isinstance(content, Mapping):
            _reject_untrusted(
                "Snapshot is missing a declared timeline", {"timeline_id": timeline_id}
            )
        if str(content.get("id") or "").strip() != timeline_id:
            _reject_untrusted(
                "Snapshot timeline content ID does not match its directory",
                {"timeline_id": timeline_id},
            )
    return (
        {
            "source_draft_path_sha256": _source_path_digest(source_draft_dir),
            "draft_directory_name": source_draft_dir.name,
            "draft_name": str(meta.get("draft_name") or "").strip(),
            "draft_id": str(meta.get("draft_id") or "").strip(),
            "root_timeline_id": root_id,
            "active_timeline_id": active_id,
            "timeline_ids": list(timeline_ids),
        },
        active_id,
        timeline_ids,
    )


def _snapshot_identity(snapshot_dir: Path, source_draft_dir: Path) -> dict[str, Any]:
    payloads = {
        path.relative_to(snapshot_dir).as_posix(): _read_regular_file_bytes(
            path, label=f"snapshot file {path.name}"
        )
        for path in _regular_files(snapshot_dir)
    }
    identity, _active_id, _timeline_ids = _snapshot_identity_from_documents(
        _snapshot_documents(payloads), source_draft_dir
    )
    return identity


def _file_rows(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in _regular_files(root)
    ]


def _source_observations(
    source_draft_dir: Path, relative_paths: tuple[str, ...]
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    rows: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    for relative_text in relative_paths:
        relative = _safe_relative_path(relative_text)
        path = _contained_path(source_draft_dir, relative)
        payload = _read_regular_file_bytes(path, label="draft runtime identity file")
        digest = hashlib.sha256(payload).hexdigest()
        rows.append({"path": relative.as_posix(), "size": len(payload), "sha256": digest})
        payloads[relative.as_posix()] = payload
    return rows, payloads


def _source_rows(source_draft_dir: Path, relative_paths: tuple[str, ...]) -> list[dict[str, Any]]:
    rows, _payloads = _source_observations(source_draft_dir, relative_paths)
    return rows


def _write_receipt(snapshot_dir: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("receipt_sha256", None)
    result["receipt_sha256"] = canonical_json_sha256(result)
    receipt_path = snapshot_dir / SNAPSHOT_RECEIPT_FILENAME
    temporary = receipt_path.with_suffix(receipt_path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, receipt_path)
    return result


def _load_receipt(snapshot_dir: Path) -> dict[str, Any]:
    receipt = _read_json_object(snapshot_dir / SNAPSHOT_RECEIPT_FILENAME, label="snapshot receipt")
    supplied_digest = str(receipt.pop("receipt_sha256", ""))
    if supplied_digest != canonical_json_sha256(receipt):
        _reject_untrusted("Snapshot receipt hash does not match")
    if (
        receipt.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or receipt.get("kind") != "autocut_readable_draft_snapshot"
    ):
        _reject_untrusted("Snapshot receipt has an unsupported schema")
    receipt["receipt_sha256"] = supplied_digest
    return receipt


def _row_map(rows: Any, *, label: str) -> dict[str, tuple[int, str]]:
    if not isinstance(rows, list):
        _reject_untrusted(f"Snapshot receipt is missing {label} rows")
    result: dict[str, tuple[int, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            _reject_untrusted(f"Snapshot receipt contains an invalid {label} row")
        relative = _safe_relative_path(str(row.get("path") or ""))
        relative_text = relative.as_posix()
        size = row.get("size")
        digest = str(row.get("sha256") or "")
        if (
            relative_text in result
            or not isinstance(size, int)
            or size < 0
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest.casefold())
        ):
            _reject_untrusted(
                f"Snapshot receipt contains an invalid {label} row", {"path": relative_text}
            )
        result[relative_text] = (size, digest.casefold())
    return result


def _validate_snapshot_files(snapshot_dir: Path, rows: Any) -> dict[str, bytes]:
    expected = _row_map(rows, label="snapshot file")
    actual = {
        path.relative_to(snapshot_dir).as_posix(): path for path in _regular_files(snapshot_dir)
    }
    if set(actual) != set(expected):
        _reject_untrusted("Snapshot files do not match the receipt")
    validated: dict[str, bytes] = {}
    for relative, path in actual.items():
        size, digest = expected[relative]
        payload = _read_regular_file_bytes(path, label=f"snapshot file {relative}")
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            _reject_untrusted("Snapshot file hash does not match its receipt", {"path": relative})
        validated[relative] = payload
    if not all(name in actual for name in _REQUIRED_SNAPSHOT_FILES):
        _reject_untrusted("Snapshot is missing required readable files")
    return validated


def _validate_source_rows(source_draft_dir: Path, rows: Any, *, label: str) -> None:
    expected = _row_map(rows, label=label)
    if not expected:
        _reject_untrusted(f"Snapshot receipt has no {label} rows")
    actual_rows, _payloads = _source_observations(source_draft_dir, tuple(expected))
    actual = {row["path"]: (row["size"], row["sha256"]) for row in actual_rows}
    for relative_text, expected_identity in expected.items():
        if actual.get(relative_text) != expected_identity:
            _reject_untrusted(
                "Draft runtime identity no longer matches the snapshot", {"path": relative_text}
            )


def _is_readable_json_object(path: Path) -> bool:
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8-sig")), dict)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _copy_stable(source: Path, target: Path) -> None:
    if not _is_regular_file(source):
        _reject_untrusted(
            "Snapshot source contains a missing or unsafe file", {"file": source.name}
        )
    before = sha256_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    after = sha256_file(source)
    copied = sha256_file(target)
    if before != after or before != copied:
        raise PortableProjectError(
            "snapshot_source_changed", "Draft changed while its readable snapshot was captured"
        )


def _capture_relative_paths(source_draft_dir: Path) -> tuple[str, ...]:
    root_content = _read_json_object(
        source_draft_dir / "draft_content.json", label="draft_content.json"
    )
    _read_json_object(source_draft_dir / "draft_meta_info.json", label="draft_meta_info.json")
    layout = _read_optional_object(source_draft_dir / "timeline_layout.json")
    project = _read_optional_object(source_draft_dir / "Timelines" / "project.json")
    topology = resolve_timeline_topology(
        layout, project, root_content, error_code="untrusted_snapshot"
    )
    validate_timeline_directories(source_draft_dir, topology, error_code="untrusted_snapshot")
    timeline_ids = topology.declared_timeline_ids
    paths = ["draft_content.json", "draft_meta_info.json"]
    for optional in (
        "draft_virtual_store.json",
        "timeline_layout.json",
        "Timelines/project.json",
    ):
        if (source_draft_dir / _safe_relative_path(optional)).is_file():
            paths.append(optional)
    for timeline_id in timeline_ids:
        paths.append(f"Timelines/{timeline_id}/draft_content.json")
    return tuple(dict.fromkeys(paths))


def capture_readable_snapshot(
    source_draft_dir: Path,
    snapshot_dir: Path,
    *,
    job_digest: str,
) -> SnapshotCapture:
    """Capture one readable save transaction before JianYing runtime encoding."""

    source_draft = Path(source_draft_dir).resolve()
    snapshot = Path(snapshot_dir).resolve()
    digest = _normalized_job_digest(job_digest)
    if not source_draft.is_dir():
        raise PortableProjectError("draft_not_found", "Draft directory does not exist")
    if snapshot.exists():
        raise PortableProjectError(
            "snapshot_destination_exists", "Snapshot destination already exists"
        )
    try:
        relative_paths = _capture_relative_paths(source_draft)
    except PortableProjectError as exc:
        if exc.code == "untrusted_snapshot":
            raise PortableProjectError(
                "unpatchable_draft_content", "Draft is not readable at snapshot capture time"
            ) from exc
        raise
    snapshot.mkdir(parents=True)
    try:
        for relative_text in relative_paths:
            relative = _safe_relative_path(relative_text)
            _copy_stable(source_draft / relative, snapshot / relative)
        identity = _snapshot_identity(snapshot, source_draft)
        snapshot_rows = _file_rows(snapshot)
        runtime_rows = _source_rows(source_draft, relative_paths)
        snapshot_state = {row["path"]: (row["size"], row["sha256"]) for row in snapshot_rows}
        runtime_state = {row["path"]: (row["size"], row["sha256"]) for row in runtime_rows}
        if snapshot_state != runtime_state:
            raise PortableProjectError(
                "snapshot_source_changed",
                "Draft changed while its readable snapshot was captured",
            )
        seal_token = secrets.token_urlsafe(32)
        payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "kind": "autocut_readable_draft_snapshot",
            "state": "captured_readable",
            "snapshot_id": str(uuid.uuid4()),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "job_digest": digest,
            "seal_token_sha256": hashlib.sha256(seal_token.encode("utf-8")).hexdigest(),
            "identity": identity,
            "snapshot_files": snapshot_rows,
            "captured_runtime_files": runtime_rows,
            "runtime_identity_files": runtime_rows,
        }
        receipt = _write_receipt(snapshot, payload)
        return SnapshotCapture(receipt=receipt, seal_token=seal_token)
    except Exception:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise


def seal_runtime_snapshot(
    snapshot_dir: Path,
    *,
    source_draft_dir: Path,
    job_digest: str,
    seal_token: str,
) -> dict[str, Any]:
    """Bind a captured readable save to its immediate JianYing encoded runtime state."""

    snapshot = Path(snapshot_dir).resolve()
    source_draft = Path(source_draft_dir).resolve()
    receipt = _load_receipt(snapshot)
    if receipt.get("state") != "captured_readable":
        _reject_untrusted("Only a pending readable capture can be runtime sealed")
    if receipt.get("job_digest") != _normalized_job_digest(job_digest):
        _reject_untrusted("Snapshot job digest does not match")
    supplied_token_hash = hashlib.sha256(str(seal_token).encode("utf-8")).hexdigest()
    if not secrets.compare_digest(supplied_token_hash, str(receipt.get("seal_token_sha256") or "")):
        _reject_untrusted("Snapshot seal token does not match")
    snapshot_payloads = _validate_snapshot_files(snapshot, receipt.get("snapshot_files"))
    snapshot_documents = _snapshot_documents(snapshot_payloads)
    identity = receipt.get("identity")
    current_identity, _active_id, _timeline_ids = _snapshot_identity_from_documents(
        snapshot_documents, source_draft
    )
    if not isinstance(identity, dict) or identity != current_identity:
        _reject_untrusted("Snapshot identity does not match its capture receipt")
    captured_rows = _row_map(receipt.get("captured_runtime_files"), label="captured runtime")
    relative_paths = tuple(captured_rows)
    runtime_rows, runtime_payloads = _source_observations(source_draft, relative_paths)
    runtime_row_map = {row["path"]: (row["size"], row["sha256"]) for row in runtime_rows}
    for relative_text in _STABLE_IDENTITY_FILES:
        if relative_text not in captured_rows:
            continue
        size, digest = captured_rows[relative_text]
        if runtime_row_map.get(relative_text) != (size, digest):
            _reject_untrusted(
                "Draft timeline identity changed before the runtime snapshot was sealed",
                {"path": relative_text},
            )
    changed_encoded = 0
    for relative_text, (size, digest) in captured_rows.items():
        if relative_text in _STABLE_IDENTITY_FILES:
            continue
        if runtime_row_map.get(relative_text) == (size, digest):
            continue
        try:
            is_readable_object = isinstance(
                json.loads(runtime_payloads[relative_text].decode("utf-8-sig")), dict
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            is_readable_object = False
        if is_readable_object:
            _reject_untrusted(
                "Readable draft content changed after snapshot capture", {"path": relative_text}
            )
        changed_encoded += 1
    if changed_encoded == 0:
        _reject_untrusted("No JianYing runtime encoding transition was detected")
    previous_digest = str(receipt.get("receipt_sha256") or "")
    receipt.update(
        {
            "state": "runtime_sealed",
            "sealed_at": datetime.now(timezone.utc).isoformat(),
            "capture_receipt_sha256": previous_digest,
            "runtime_identity_files": runtime_rows,
        }
    )
    receipt.pop("seal_token_sha256", None)
    return _write_receipt(snapshot, receipt)


def verify_snapshot_receipt(
    snapshot_dir: Path,
    *,
    source_draft_dir: Path,
    expected_job_digest: str,
    expected_receipt_sha256: str,
) -> VerifiedSnapshot:
    """Verify a snapshot and its bound source state before encoded-draft fallback."""

    snapshot = Path(snapshot_dir).resolve()
    source_draft = Path(source_draft_dir).resolve()
    trusted_job_digest = _normalized_job_digest(expected_job_digest)
    trusted_receipt_digest = _normalized_receipt_digest(expected_receipt_sha256)
    receipt = _load_receipt(snapshot)
    receipt_digest = _normalized_receipt_digest(str(receipt.get("receipt_sha256") or ""))
    if not secrets.compare_digest(receipt_digest, trusted_receipt_digest):
        _reject_untrusted("Snapshot receipt does not match the trusted task receipt")
    if receipt.get("state") != "runtime_sealed":
        _reject_untrusted("Snapshot is not bound to a JianYing runtime save")
    receipt_job_digest = _normalized_job_digest(str(receipt.get("job_digest") or ""))
    if not secrets.compare_digest(receipt_job_digest, trusted_job_digest):
        _reject_untrusted("Snapshot job digest does not match the trusted task")
    snapshot_payloads = _validate_snapshot_files(snapshot, receipt.get("snapshot_files"))
    documents = _snapshot_documents(snapshot_payloads)
    _validate_source_rows(
        source_draft,
        receipt.get("runtime_identity_files"),
        label="runtime identity",
    )
    identity = receipt.get("identity")
    current_identity, active_id, timeline_ids = _snapshot_identity_from_documents(
        documents, source_draft
    )
    if not isinstance(identity, dict) or identity != current_identity:
        _reject_untrusted("Snapshot draft identity does not match its receipt")
    return VerifiedSnapshot(
        receipt=receipt,
        documents=documents,
        active_timeline_id=active_id,
        timeline_ids=timeline_ids,
    )
