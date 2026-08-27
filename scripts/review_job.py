# ruff: noqa: E402,I001
"""CLI helpers for compiling and inspecting resumable review jobs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from utils.cli_protocol import make_result
from utils.errors import InfraError, UserInputError
from utils.review_job_compiler import compile_review_job
from utils.review_job_pipeline import JobStateStore
from utils.user_action_notifications import (
    TEMPLATE_VERSION as USER_ACTION_TEMPLATE_VERSION,
    build_user_action_event,
    deliver_user_action_required,
    notification_receipt_summary,
)


_PLAN_SCHEMA_VERSION = 1
_JOB_SCHEMA_VERSION = 2
_LEGACY_JOB_SCHEMA_VERSION = 1
_CACHE_MANIFEST_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_EXTERNAL_WAIT_ARTIFACT = "subject_pointer_onboarding.json"
_EXTERNAL_WAIT_CODE = "awaiting_subject_profile"
_EXTERNAL_WAIT_PHASE = "subject_pointer_profile_gate"
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
_WAIT_REQUEST_FIELDS = frozenset(
    {"schema_version", "item_ids", "input_digest", "artifact_payload", "replaces"}
)
_WAIT_REPLACES_FIELDS = frozenset({"input_digest", "artifact_sha256"})
_WAIT_INPUT_ERRORS = (
    FileNotFoundError,
    IsADirectoryError,
    NotADirectoryError,
    TypeError,
    ValueError,
)
_PHASE_STATUSES = frozenset({"pending", "running", "complete", "failed", "skipped"})
_PHASE_FIELDS = frozenset(
    {
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
)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _finite_nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return result


def _item_time(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _review_items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("review_items")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("review_items must be a sequence of objects")
    result = list(value)
    if not all(isinstance(item, Mapping) for item in result):
        raise TypeError("review_items must contain only objects")
    return result


def _item_id(item: Mapping[str, Any], index: int) -> str:
    for field in ("id", "item_id", "clip_id"):
        candidate = item.get(field)
        if isinstance(candidate, str) and candidate:
            return candidate
    raise ValueError(f"review item at index {index} requires a non-empty id")


def build_local_window_plan(
    review_items: Any,
    *,
    context_before: float = 5.0,
    context_after: float = 5.0,
    media_duration: float | None = None,
    full_preview_required: bool = False,
    full_preview_source: str | None = None,
) -> dict[str, Any]:
    """Build deterministic merged evidence windows without fabricating item time."""

    before = _finite_nonnegative(context_before, "context_before")
    after = _finite_nonnegative(context_after, "context_after")
    duration = (
        None if media_duration is None else _finite_nonnegative(media_duration, "media_duration")
    )
    if not isinstance(full_preview_required, bool):
        raise TypeError("full_preview_required must be a boolean")
    if full_preview_source is not None and (
        not isinstance(full_preview_source, str) or not full_preview_source.strip()
    ):
        raise ValueError("full_preview_source must be non-empty text")

    affected: list[dict[str, Any]] = []
    untimed_item_ids: list[str] = []
    out_of_range_item_ids: list[str] = []
    timed_item_count = 0
    seen_ids: set[str] = set()
    for index, item in enumerate(_review_items(review_items)):
        item_id = _item_id(item, index)
        if item_id in seen_ids:
            raise ValueError(f"duplicate review item id: {item_id}")
        seen_ids.add(item_id)
        start = _item_time(item.get("start"))
        end = _item_time(item.get("end"))
        if start is None or end is None or end <= start:
            untimed_item_ids.append(item_id)
            continue
        timed_item_count += 1
        window_start = max(0.0, start - before)
        window_end = end + after
        if duration is not None:
            window_start = min(window_start, duration)
            window_end = min(window_end, duration)
        if window_end <= window_start:
            out_of_range_item_ids.append(item_id)
            continue
        affected.append({"start": window_start, "end": window_end, "item_ids": [item_id]})

    affected.sort(key=lambda row: (row["start"], row["end"], row["item_ids"][0]))
    windows: list[dict[str, Any]] = []
    for candidate in affected:
        if not windows or candidate["start"] > windows[-1]["end"]:
            windows.append(candidate)
            continue
        current = windows[-1]
        current["end"] = max(current["end"], candidate["end"])
        current["item_ids"].extend(candidate["item_ids"])

    preview_requests = (
        [{"source": (full_preview_source or "explicit").strip()}] if full_preview_required else []
    )
    return {
        "schema_version": _PLAN_SCHEMA_VERSION,
        "context_before": before,
        "context_after": after,
        "media_duration": duration,
        "windows": windows,
        "window_count": len(windows),
        "timed_item_count": timed_item_count,
        "untimed_item_ids": untimed_item_ids,
        "untimed_item_count": len(untimed_item_ids),
        "out_of_range_item_ids": out_of_range_item_ids,
        "out_of_range_item_count": len(out_of_range_item_ids),
        "full_preview_required": full_preview_required,
        "full_preview_requests": preview_requests,
    }


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _read_json_object(path: str | os.PathLike[str], label: str) -> dict[str, Any]:
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError(f"{label} path must be a path")
    source = Path(path).expanduser().resolve(strict=False)
    try:
        with open(source, "r", encoding="utf-8") as handle:
            payload = json.load(handle, parse_constant=_reject_json_constant)
    except FileNotFoundError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> str:
    content = _canonical_json_bytes(payload)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    try:
        with open(temporary, "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(content).hexdigest()


def _nested_value(mapping: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = mapping
    for field in path:
        if not isinstance(current, Mapping) or field not in current:
            return None
        current = current[field]
    return current


def _project_media_duration(project: Mapping[str, Any]) -> float | None:
    paths = (
        ("media_duration_seconds",),
        ("media_duration",),
        ("duration_seconds",),
        ("duration",),
        ("source_duration_seconds",),
        ("source_duration",),
        ("media", "duration_seconds"),
        ("media", "duration"),
    )
    for path in paths:
        value = _nested_value(project, path)
        if value is not None:
            return _finite_nonnegative(value, ".".join(path))
    return None


def _full_preview_request(
    project: Mapping[str, Any], cli_requested: bool
) -> tuple[bool, str | None]:
    if not isinstance(cli_requested, bool):
        raise TypeError("full_preview must be a boolean")
    if cli_requested:
        return True, "cli"
    paths = (
        ("full_preview_required",),
        ("full_preview",),
        ("acceptance", "full_preview_required"),
        ("acceptance", "require_full_preview"),
        ("request", "full_preview_required"),
        ("request", "full_preview"),
        ("request", "acceptance", "full_preview_required"),
        ("request", "acceptance", "require_full_preview"),
    )
    for path in paths:
        value = _nested_value(project, path)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise TypeError(f"{'.'.join(path)} must be a boolean")
        if value:
            return True, f"project.{'.'.join(path)}"
    return False, None


def cmd_review_job_compile(
    snapshot_json: str | os.PathLike[str],
    project_json: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    context_before: float = 5.0,
    context_after: float = 5.0,
    full_preview: bool = False,
) -> dict[str, Any]:
    """Compile canonical job inputs and atomically add a local-window plan."""

    try:
        before = _finite_nonnegative(context_before, "context_before")
        after = _finite_nonnegative(context_after, "context_after")
        snapshot = _read_json_object(snapshot_json, "snapshot_json")
        project = _read_json_object(project_json, "project_json")
        duration = _project_media_duration(project)
        preview_required, preview_source = _full_preview_request(project, full_preview)
        if not isinstance(output_dir, (str, os.PathLike)):
            raise TypeError("output_dir must be a path")
        root = Path(output_dir).expanduser().resolve(strict=False)
        compiled = compile_review_job(snapshot, project, root)
        if not isinstance(compiled, Mapping):
            raise ValueError("compiler result must be an object")
        doc_items_path = Path(compiled["doc_items"]).expanduser().resolve(strict=False)
        doc_items = _read_json_object(doc_items_path, "compiled doc_items")
        plan = build_local_window_plan(
            doc_items,
            context_before=before,
            context_after=after,
            media_duration=duration,
            full_preview_required=preview_required,
            full_preview_source=preview_source,
        )
    except FileNotFoundError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise UserInputError(str(exc)) from exc

    plan_path = root / "local_window_plan.json"
    if plan_path.parent.resolve(strict=False) != root:
        raise UserInputError("local window plan path escapes output_dir")
    plan_digest = _atomic_write_json(plan_path, plan)

    data = dict(compiled)
    for field in ("doc_items", "revision_request", "job_manifest"):
        try:
            data[field] = str(Path(data[field]).expanduser().resolve(strict=False))
        except (KeyError, TypeError) as exc:
            raise UserInputError(f"compiler result is missing {field}") from exc
    digests = data.get("digests")
    if not isinstance(digests, Mapping):
        raise UserInputError("compiler result is missing digests")
    data["digests"] = dict(digests)
    data["digests"]["local_window_plan"] = plan_digest
    data.update(
        {
            "local_window_plan": str(plan_path.resolve(strict=False)),
            "window_count": plan["window_count"],
            "timed_item_count": plan["timed_item_count"],
            "untimed_item_count": plan["untimed_item_count"],
            "untimed_item_ids": list(plan["untimed_item_ids"]),
            "out_of_range_item_count": plan["out_of_range_item_count"],
            "out_of_range_item_ids": list(plan["out_of_range_item_ids"]),
            "full_preview_required": plan["full_preview_required"],
        }
    )
    return make_result(True, "ok", "", data)


def cmd_review_document_run(
    snapshot_json: str | os.PathLike[str],
    project_json: str | os.PathLike[str],
    job_root: str | os.PathLike[str],
    *,
    drafts_root: str | os.PathLike[str],
    package_zip: str | os.PathLike[str],
    relink_tool: str | os.PathLike[str] | None = None,
    mock_media: bool = False,
    asr_timeout_seconds: float = 60.0,
    asr_poll_interval_seconds: float = 2.0,
    asr_max_wait_seconds: float = 120.0,
    context_before: float = 5.0,
    context_after: float = 5.0,
) -> dict[str, Any]:
    """Run the public, Lite-only source-document workflow."""

    # Keep the heavy media/ASR runner lazy so parser and status commands stay lightweight.
    from utils.review_document_runner import ReviewDocumentRunError, run_review_document

    try:
        data = run_review_document(
            snapshot_json=snapshot_json,
            project_json=project_json,
            job_root=job_root,
            drafts_root=drafts_root,
            package_zip=package_zip,
            relink_tool=relink_tool,
            mock_media=bool(mock_media),
            asr_timeout_seconds=asr_timeout_seconds,
            asr_poll_interval_seconds=asr_poll_interval_seconds,
            asr_max_wait_seconds=asr_max_wait_seconds,
            context_before=context_before,
            context_after=context_after,
            workflow_mode="lite",
        )
    except ReviewDocumentRunError as exc:
        return make_result(False, "review_document_failed", str(exc), exc.result)
    except (TypeError, ValueError) as exc:
        raise UserInputError(str(exc)) from exc
    if not isinstance(data, Mapping):
        raise RuntimeError("review-document runner returned a non-object result")
    return make_result(True, "ok", "", dict(data))


def _valid_utc_timestamp(value: str) -> bool:
    if not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _validate_phase_record(candidate: Any, label: str) -> dict[str, Any]:
    if not isinstance(candidate, dict) or set(candidate) != _PHASE_FIELDS:
        raise ValueError(f"{label} has an invalid phase schema")
    status = candidate["status"]
    if status not in _PHASE_STATUSES:
        raise ValueError(f"{label} has an invalid phase status")
    if candidate["cache_hit"] is not None and not isinstance(candidate["cache_hit"], bool):
        raise ValueError(f"{label} has an invalid cache_hit")
    retries = candidate["retry_count"]
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError(f"{label} has an invalid retry_count")
    item_ids = candidate["item_ids"]
    if not isinstance(item_ids, list) or not all(isinstance(value, str) for value in item_ids):
        raise ValueError(f"{label} has invalid item_ids")
    if not isinstance(candidate["tool_version"], str):
        raise ValueError(f"{label} has an invalid tool_version")
    for field in ("input_digest", "output_digest", "error", "started_at", "finished_at"):
        if candidate[field] is not None and not isinstance(candidate[field], str):
            raise ValueError(f"{label} has an invalid {field}")
    durations = {
        field: _finite_nonnegative(candidate[field], f"{label}.{field}")
        for field in ("elapsed_seconds", "active_seconds", "wait_seconds")
    }
    if not math.isclose(
        durations["active_seconds"] + durations["wait_seconds"],
        durations["elapsed_seconds"],
        abs_tol=1e-9,
    ):
        raise ValueError(f"{label} timing fields are inconsistent")
    started = candidate["started_at"]
    finished = candidate["finished_at"]
    if status == "pending" and (started is not None or finished is not None):
        raise ValueError(f"{label} pending timestamps are invalid")
    if status == "running" and (started is None or finished is not None):
        raise ValueError(f"{label} running timestamps are invalid")
    if status in {"complete", "failed"} and (started is None or finished is None):
        raise ValueError(f"{label} finished timestamps are invalid")
    if status == "skipped" and (started is not None or finished is None or not candidate["error"]):
        raise ValueError(f"{label} skipped timestamps are invalid")
    if not all(value is None or _valid_utc_timestamp(value) for value in (started, finished)):
        raise ValueError(f"{label} timestamps are invalid")
    result = {field: candidate[field] for field in _PHASE_FIELDS}
    result.update(durations)
    return result


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_external_wait(candidate: Any, label: str) -> dict[str, Any]:
    if not isinstance(candidate, dict) or set(candidate) != _EXTERNAL_WAIT_FIELDS:
        raise ValueError(f"{label} has an invalid external_wait schema")
    if candidate["code"] != _EXTERNAL_WAIT_CODE:
        raise ValueError(f"{label} has an invalid external_wait code")
    if candidate["phase"] != _EXTERNAL_WAIT_PHASE:
        raise ValueError(f"{label} has an invalid external_wait phase")
    item_ids = candidate["item_ids"]
    if (
        not isinstance(item_ids, list)
        or not item_ids
        or not all(isinstance(item_id, str) and item_id for item_id in item_ids)
        or len(set(item_ids)) != len(item_ids)
    ):
        raise ValueError(f"{label} has invalid external_wait item_ids")
    artifact = candidate["artifact"]
    if not isinstance(artifact, str) or not artifact:
        raise ValueError(f"{label} has an invalid external_wait artifact")
    input_digest = _require_sha256(candidate["input_digest"], f"{label} wait input digest")
    artifact_sha256 = _require_sha256(candidate["artifact_sha256"], f"{label} wait artifact digest")
    started_at = candidate["started_at"]
    if not isinstance(started_at, str) or not _valid_utc_timestamp(started_at):
        raise ValueError(f"{label} has an invalid external_wait started_at")
    return {
        "code": candidate["code"],
        "phase": candidate["phase"],
        "item_ids": list(item_ids),
        "input_digest": input_digest,
        "artifact": artifact,
        "artifact_sha256": artifact_sha256,
        "started_at": started_at,
    }


def _validate_job_payload(payload: Mapping[str, Any], label: str) -> dict[str, Any]:
    schema_version = payload.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in {_LEGACY_JOB_SCHEMA_VERSION, _JOB_SCHEMA_VERSION}
    ):
        raise ValueError(f"{label} has an unsupported schema_version")
    if schema_version == _LEGACY_JOB_SCHEMA_VERSION:
        if "external_wait" in payload:
            raise ValueError(f"{label} schema v1 cannot contain external_wait")
        external_wait = None
    else:
        if "external_wait" not in payload:
            raise ValueError(f"{label} schema v2 requires external_wait")
        wait_candidate = payload["external_wait"]
        external_wait = (
            None if wait_candidate is None else _validate_external_wait(wait_candidate, label)
        )
    input_digest = payload.get("input_digest")
    tool_version = payload.get("tool_version")
    phases = payload.get("phases")
    if not isinstance(input_digest, str) or not input_digest:
        raise ValueError(f"{label} has an invalid input_digest")
    if not isinstance(tool_version, str) or not tool_version:
        raise ValueError(f"{label} has an invalid tool_version")
    if not isinstance(phases, dict):
        raise ValueError(f"{label} has invalid phases")
    validated: dict[str, dict[str, Any]] = {}
    for name, candidate in phases.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{label} has an invalid phase name")
        validated[name] = _validate_phase_record(candidate, f"{label}.phases.{name}")
    return {
        "schema_version": schema_version,
        "input_digest": input_digest,
        "tool_version": tool_version,
        "phases": validated,
        "external_wait": external_wait,
    }


def _path_is_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(stat.S_ISLNK(metadata.st_mode) or file_attributes & reparse_flag)


def _external_wait_artifact_error(state_path: Path, wait: Mapping[str, Any]) -> str | None:
    artifact_text = wait["artifact"]
    relative = Path(artifact_text)
    if (
        artifact_text != _EXTERNAL_WAIT_ARTIFACT
        or relative.is_absolute()
        or relative.drive
        or relative.anchor
        or relative == Path(".")
        or any(part == ".." for part in relative.parts)
    ):
        return "unsafe"
    root = state_path.parent
    candidate = root / relative
    if candidate.parent != root or _path_is_reparse(candidate):
        return "unsafe"
    if not os.path.lexists(candidate):
        return "missing"
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unsafe"
    if not stat.S_ISREG(metadata.st_mode):
        return "unsafe"
    try:
        artifact_bytes = candidate.read_bytes()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unsafe"
    if hashlib.sha256(artifact_bytes).hexdigest() != wait["artifact_sha256"]:
        return "hash_mismatch"
    try:
        artifact_payload = json.loads(
            artifact_bytes.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
        JobStateStore._validate_external_wait_payload(
            artifact_payload,
            input_digest=wait["input_digest"],
            item_ids=wait["item_ids"],
        )
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return "invalid"
    return None


def _project_external_wait(
    wait: Mapping[str, Any],
    *,
    artifact_error_code: str | None,
) -> dict[str, Any]:
    return {
        "code": wait["code"],
        "phase": wait["phase"],
        "item_ids": list(wait["item_ids"]),
        "input_digest": wait["input_digest"],
        "artifact_sha256": wait["artifact_sha256"],
        "started_at": wait["started_at"],
        "artifact_valid": artifact_error_code is None,
        "artifact_error_code": artifact_error_code,
    }


def _unavailable_notification_summary(event_id: str | None) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "template_version": USER_ACTION_TEMPLATE_VERSION,
        "destination_fingerprint": None,
        "delivery_state": "unavailable",
        "attempt_count": None,
        "sent_at": None,
        "message_id": None,
        "error_code": "unavailable",
    }


def _notify_current_external_wait(
    store: JobStateStore,
) -> tuple[dict[str, Any], dict[str, Any]]:
    context = store.get_external_wait_user_action_context()
    current_wait = context["wait"]
    event = build_user_action_event(
        input_digest=context["job_input_digest"],
        project_key=context["project_key"],
        action_code=context["action_code"],
        item_ids=context["item_ids"],
        prompt_revision=context["prompt_revision"],
    )
    try:
        summary = notification_receipt_summary(deliver_user_action_required(event), event)
        if summary["delivery_state"] not in {"sent", "failed", "disabled"}:
            raise ValueError("notification delivery returned a nonterminal summary")
    except Exception:
        summary = _unavailable_notification_summary(event.event_id)
    return current_wait, summary


def _redact_error(value: str | None, status: str) -> str | None:
    if value is None:
        return None
    if status == "failed":
        return "phase failed"
    if status == "skipped":
        return "phase skipped"
    return "phase error"


def _optional_identity(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _read_consistent_job_files(
    state_path: Path, attempts: int = 3
) -> tuple[dict[str, Any], dict[str, Any]]:
    timing_path = state_path.with_name("job_timing.json")
    if not state_path.exists():
        raise FileNotFoundError(state_path)
    for _attempt in range(attempts):
        state = _validate_job_payload(_read_json_object(state_path, "job state"), "job state")
        try:
            timing = _validate_job_payload(
                _read_json_object(timing_path, "job timing"), "job timing"
            )
        except (FileNotFoundError, ValueError):
            continue
        if (
            timing["schema_version"] == state["schema_version"]
            and timing["input_digest"] == state["input_digest"]
            and timing["tool_version"] == state["tool_version"]
            and timing["phases"] == state["phases"]
            and timing["external_wait"] == state["external_wait"]
        ):
            return state, timing
    raise ValueError("job timing is missing, corrupt, or inconsistent with job state")


def read_job_status(
    state_json: str | os.PathLike[str],
    *,
    input_digest: str | None = None,
    tool_version: str | None = None,
) -> dict[str, Any]:
    """Read state and timing files without recovery, mutation, or store creation."""

    expected_input = _optional_identity(input_digest, "input_digest")
    expected_tool = _optional_identity(tool_version, "tool_version")
    state_path = Path(state_json).expanduser().resolve(strict=False)
    state, timing = _read_consistent_job_files(state_path)

    stale_input = expected_input is not None and expected_input != state["input_digest"]
    stale_tool = expected_tool is not None and expected_tool != state["tool_version"]
    external_wait = state["external_wait"]
    external_wait_status = None
    wait_phase = None
    if external_wait is not None:
        artifact_error = _external_wait_artifact_error(state_path, external_wait)
        external_wait_status = _project_external_wait(
            external_wait,
            artifact_error_code=artifact_error,
        )
        wait_phase = external_wait["phase"]
    phases: dict[str, dict[str, Any]] = {}
    timings: dict[str, dict[str, float]] = {}
    errors: dict[str, str] = {}
    unresolved: list[str] = []
    unresolved_seen: set[str] = set()
    next_phase: str | None = None
    for name, record in state["phases"].items():
        resumable = bool(
            record["status"] == "complete"
            and record["tool_version"] == state["tool_version"]
            and not stale_input
            and not stale_tool
            and name != wait_phase
        )
        safe_record = dict(record)
        safe_record["error"] = _redact_error(record["error"], record["status"])
        safe_record["resumable"] = resumable
        phases[name] = safe_record
        timing_record = timing["phases"][name]
        timings[name] = {
            "elapsed_seconds": timing_record["elapsed_seconds"],
            "active_seconds": timing_record["active_seconds"],
            "wait_seconds": timing_record["wait_seconds"],
        }
        if safe_record["error"]:
            errors[name] = safe_record["error"]
        if not resumable:
            if next_phase is None:
                next_phase = name
            for item_id in record["item_ids"]:
                if item_id not in unresolved_seen:
                    unresolved_seen.add(item_id)
                    unresolved.append(item_id)

    if external_wait is not None:
        next_phase = external_wait["phase"]
        for item_id in external_wait["item_ids"]:
            if item_id not in unresolved_seen:
                unresolved_seen.add(item_id)
                unresolved.append(item_id)

    return {
        "schema_version": state["schema_version"],
        "input_digest": state["input_digest"],
        "tool_version": state["tool_version"],
        "job_identity": {
            "input_digest": state["input_digest"],
            "tool_version": state["tool_version"],
        },
        "phase_order": list(phases),
        "phases": phases,
        "timings": timings,
        "errors": errors,
        "external_wait": external_wait_status,
        "unresolved_item_ids": unresolved,
        "next_resumable_phase": next_phase,
        "stale_input": stale_input,
        "stale_tool": stale_tool,
    }


def cmd_review_job_status(
    state_json: str | os.PathLike[str],
    *,
    input_digest: str | None = None,
    tool_version: str | None = None,
) -> dict[str, Any]:
    try:
        status = read_job_status(
            state_json,
            input_digest=input_digest,
            tool_version=tool_version,
        )
    except FileNotFoundError:
        raise
    except (TypeError, ValueError) as exc:
        raise UserInputError(str(exc)) from exc
    return make_result(True, "ok", "", status)


def _closed_wait_mapping(
    candidate: Any,
    fields: frozenset[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(candidate, dict) or set(candidate) != fields:
        raise ValueError(f"{label} must contain exactly the approved fields")
    return dict(candidate)


def _wait_request_item_ids(candidate: Any) -> list[str]:
    if (
        not isinstance(candidate, list)
        or not candidate
        or not all(isinstance(item_id, str) and item_id for item_id in candidate)
        or len(set(candidate)) != len(candidate)
    ):
        raise ValueError("wait request item_ids must contain unique non-empty strings")
    return list(candidate)


def _validate_wait_request(candidate: Mapping[str, Any]) -> dict[str, Any]:
    request = _closed_wait_mapping(candidate, _WAIT_REQUEST_FIELDS, "wait request")
    schema_version = request["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise ValueError("wait request schema_version must be 1")
    item_ids = _wait_request_item_ids(request["item_ids"])
    input_digest = _require_sha256(request["input_digest"], "wait request input_digest")
    artifact_payload = JobStateStore._validate_external_wait_payload(
        request["artifact_payload"],
        input_digest=input_digest,
        item_ids=item_ids,
    )
    replaces_candidate = request["replaces"]
    if replaces_candidate is None:
        replaces = None
    else:
        replaces = _closed_wait_mapping(
            replaces_candidate,
            _WAIT_REPLACES_FIELDS,
            "wait request replaces",
        )
        replaces["input_digest"] = _require_sha256(
            replaces["input_digest"], "replacement input_digest"
        )
        replaces["artifact_sha256"] = _require_sha256(
            replaces["artifact_sha256"], "replacement artifact_sha256"
        )
    return {
        "item_ids": item_ids,
        "input_digest": input_digest,
        "artifact_payload": artifact_payload,
        "replaces": replaces,
    }


def _existing_job_store(state_json: str | os.PathLike[str]) -> JobStateStore:
    if not isinstance(state_json, (str, os.PathLike)):
        raise TypeError("job state path must be a path")
    state_path = Path(state_json).expanduser().resolve(strict=False)
    state = _validate_job_payload(_read_json_object(state_path, "job state"), "job state")
    return JobStateStore(
        state_path,
        input_digest=state["input_digest"],
        tool_version=state["tool_version"],
    )


def cmd_review_job_wait_open(
    state_json: str | os.PathLike[str],
    wait_json: str | os.PathLike[str],
) -> dict[str, Any]:
    try:
        request = _validate_wait_request(_read_json_object(wait_json, "wait request"))
        store = _existing_job_store(state_json)
        replaces = request["replaces"] or {}
        store.begin_external_wait(
            code=_EXTERNAL_WAIT_CODE,
            phase=_EXTERNAL_WAIT_PHASE,
            item_ids=request["item_ids"],
            input_digest=request["input_digest"],
            artifact=_EXTERNAL_WAIT_ARTIFACT,
            artifact_payload=request["artifact_payload"],
            replaces_input_digest=replaces.get("input_digest"),
            replaces_artifact_sha256=replaces.get("artifact_sha256"),
        )
    except _WAIT_INPUT_ERRORS as exc:
        raise UserInputError("external wait request could not be opened") from exc
    except Exception as exc:
        raise InfraError("external wait request could not be opened") from exc
    try:
        current_wait, notification = _notify_current_external_wait(store)
    except Exception as exc:
        raise InfraError("external wait notification context could not be loaded") from exc
    projected_wait = _project_external_wait(current_wait, artifact_error_code=None)
    projected_wait["notification"] = notification
    return make_result(
        True,
        "ok",
        "",
        projected_wait,
    )


def cmd_review_job_wait_resolve(
    state_json: str | os.PathLike[str],
    *,
    input_digest: str,
    artifact_sha256: str,
    project_key: str,
    draft_path: str,
    draft_root: str,
) -> dict[str, Any]:
    try:
        expected_input = _require_sha256(input_digest, "wait input_digest")
        expected_artifact = _require_sha256(artifact_sha256, "wait artifact_sha256")
        store = _existing_job_store(state_json)
        phase = store.resolve_external_wait(
            input_digest=expected_input,
            artifact_sha256=expected_artifact,
            project_key=project_key,
            draft_path=draft_path,
            draft_root=draft_root,
        )
    except _WAIT_INPUT_ERRORS as exc:
        raise UserInputError("external wait could not be resolved") from exc
    except Exception as exc:
        raise InfraError("external wait could not be resolved") from exc
    return make_result(
        True,
        "ok",
        "",
        {
            "phase": _EXTERNAL_WAIT_PHASE,
            "status": phase["status"],
            "item_ids": list(phase["item_ids"]),
            "input_digest": phase["input_digest"],
            "wait_seconds": phase["wait_seconds"],
        },
    )


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_result(
    namespace: str,
    digest: str,
    *,
    artifact_type: str | None = None,
    schema_version: int | str | None = None,
    artifact_exists: bool = False,
    valid: bool = False,
    reason: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "digest": digest,
        "artifact_type": artifact_type,
        "schema_version": schema_version,
        "artifact_exists": artifact_exists,
        "valid": valid,
        "reason": reason,
        "metadata": dict(metadata or {}),
    }


def _valid_payload_schema(value: Any) -> bool:
    return bool(
        (isinstance(value, int) and not isinstance(value, bool))
        or (isinstance(value, str) and value)
    )


def inspect_cache_entry(
    cache_root: str | os.PathLike[str], namespace: str, digest: str
) -> dict[str, Any]:
    """Validate one existing cache entry without reading identity inputs or mutating it."""

    namespace = _validate_namespace(namespace)
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        raise ValueError("digest must be exactly 64 lowercase SHA-256 hex characters")
    if not isinstance(cache_root, (str, os.PathLike)):
        raise TypeError("cache_root must be a path")
    root = Path(cache_root).expanduser().resolve(strict=False)
    entry = root / namespace / digest
    resolved_entry = entry.resolve(strict=False)
    try:
        resolved_entry.relative_to(root)
    except ValueError as exc:
        raise ValueError("cache entry path escapes cache_root") from exc
    if not entry.exists():
        return _cache_result(namespace, digest, reason="entry_not_found")
    if entry.is_symlink() or not entry.is_dir():
        return _cache_result(namespace, digest, reason="entry_not_directory")

    manifest_path = entry / "manifest.json"
    if not manifest_path.exists() or manifest_path.is_symlink() or not manifest_path.is_file():
        return _cache_result(namespace, digest, reason="manifest_not_found")
    try:
        manifest = _read_json_object(manifest_path, "cache manifest")
    except (OSError, ValueError):
        return _cache_result(namespace, digest, reason="manifest_invalid")
    artifact_type = manifest.get("type") if manifest.get("type") in {"json", "file"} else None
    schema_version = (
        manifest.get("payload_schema_version")
        if artifact_type == "json"
        else manifest.get("schema_version")
    )
    base = {
        "artifact_type": artifact_type,
        "schema_version": schema_version,
    }
    if manifest.get("schema_version") != _CACHE_MANIFEST_SCHEMA_VERSION:
        return _cache_result(namespace, digest, reason="manifest_schema_unsupported", **base)
    if manifest.get("identity_digest") != digest:
        return _cache_result(namespace, digest, reason="identity_digest_mismatch", **base)
    if manifest.get("namespace") != namespace:
        return _cache_result(namespace, digest, reason="namespace_mismatch", **base)
    if artifact_type is None:
        return _cache_result(namespace, digest, reason="artifact_type_invalid", **base)
    if artifact_type == "json" and not _valid_payload_schema(schema_version):
        return _cache_result(namespace, digest, reason="json_schema_invalid", **base)
    size = manifest.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return _cache_result(namespace, digest, reason="artifact_size_invalid", **base)
    expected_hash = manifest.get("sha256")
    if not isinstance(expected_hash, str) or not _SHA256_PATTERN.fullmatch(expected_hash):
        return _cache_result(namespace, digest, reason="artifact_hash_invalid", **base)
    artifact_name = manifest.get("artifact")
    if (
        not isinstance(artifact_name, str)
        or not artifact_name
        or Path(artifact_name).name != artifact_name
    ):
        return _cache_result(namespace, digest, reason="artifact_path_invalid", **base)
    artifact_path = entry / artifact_name
    if artifact_path.resolve(strict=False).parent != resolved_entry:
        return _cache_result(namespace, digest, reason="artifact_path_escapes_entry", **base)
    artifact_exists = (
        artifact_path.exists() and artifact_path.is_file() and not artifact_path.is_symlink()
    )
    if not artifact_exists:
        return _cache_result(namespace, digest, reason="artifact_not_found", **base)
    metadata = {
        "manifest_schema_version": _CACHE_MANIFEST_SCHEMA_VERSION,
        "size": size,
        "sha256": expected_hash,
    }
    if artifact_path.stat().st_size != size:
        return _cache_result(
            namespace,
            digest,
            artifact_exists=True,
            reason="artifact_size_mismatch",
            metadata=metadata,
            **base,
        )
    if _sha256_file(artifact_path) != expected_hash:
        return _cache_result(
            namespace,
            digest,
            artifact_exists=True,
            reason="artifact_hash_mismatch",
            metadata=metadata,
            **base,
        )
    if artifact_type == "json":
        try:
            payload = _read_json_object(artifact_path, "cache artifact")
        except (OSError, ValueError):
            return _cache_result(
                namespace,
                digest,
                artifact_exists=True,
                reason="json_artifact_invalid",
                metadata=metadata,
                **base,
            )
        if payload.get("schema_version") != schema_version:
            return _cache_result(
                namespace,
                digest,
                artifact_exists=True,
                reason="json_schema_mismatch",
                metadata=metadata,
                **base,
            )
    return _cache_result(
        namespace,
        digest,
        artifact_exists=True,
        valid=True,
        reason="ok",
        metadata=metadata,
        **base,
    )


def cmd_review_job_cache_inspect(
    cache_root: str | os.PathLike[str], namespace: str, digest: str
) -> dict[str, Any]:
    try:
        result = inspect_cache_entry(cache_root, namespace, digest)
    except (TypeError, ValueError) as exc:
        raise UserInputError(str(exc)) from exc
    return make_result(True, "ok", "", result)


__all__ = [
    "build_local_window_plan",
    "cmd_review_job_cache_inspect",
    "cmd_review_job_compile",
    "cmd_review_job_status",
    "cmd_review_job_wait_open",
    "cmd_review_job_wait_resolve",
    "inspect_cache_entry",
    "read_job_status",
]
