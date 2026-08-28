"""Maintained, resumable end-to-end runner for Lite review documents."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from utils.jianying_native_delivery import capture_draft_tree_receipt
from utils.lite_package import package_lite_delivery
from utils.review_audio_precision import (
    CANDIDATE_RENDERER_VERSION,
    REVERSE_ASR_DIAGNOSTIC_PURPOSE,
    alignment_cache_identity,
    apply_audio_plan_to_compiled_payloads,
    apply_reverse_report_to_payloads,
    atomic_copy_file,
    atomic_write_json,
    build_full_candidate_reverse_report,
    build_lite_split_gap_audio_plan,
    cache_identity_lock,
    candidate_cache_identity,
    canonical_json_sha256,
    extract_alignment_wav,
    ffmpeg_identity,
    render_source_aligned_candidate,
    resolve_lite_audio_items,
    reverse_asr_cache_identity,
    run_resumable_volc_asr,
    sha256_file,
    source_asr_cache_identity,
)
from utils.review_document_intake import (
    LARK_ADAPTER_VERSION,
    ReviewDocumentIntakeError,
    compile_url_inputs,
    document_url_digest,
    download_lark_assets,
    evaluate_runtime_readiness,
    fetch_lark_document,
    invalidate_lark_readiness,
    lark_cli_version,
    lark_whoami,
    mark_asr_verified,
    mark_lark_verified,
    parse_lark_document,
    sanitize_document_snapshot,
    validate_document_url,
)
from utils.review_job_compiler import compile_review_job
from utils.review_job_pipeline import (
    ArtifactCache,
    CacheIdentity,
    JobStateStore,
    PhaseDefinition,
    PhaseOutcome,
    ReviewJobExecutor,
    safe_error_text,
    sanitize_public_value,
)
from utils.revision_evidence import audio_delivery_plan_sha256
from utils.revision_models import (
    lite_duration_change_is_label_only,
    lite_execution_required,
    lite_timing_source,
    resolve_execution_status,
)
from utils.revision_runner import (
    execute_revision_request,
    load_review_items_json,
    load_revision_request,
)

from audio_sound.segment_removal import probe_media
from audio_sound.volc_asr import VOLC_ASR_ADAPTER_VERSION, load_volc_asr_config

RUNNER_VERSION = "auto-cut-lite-review-document-run-v2"
_SCHEMA_VERSION = 2
_ASR_CACHE_SCHEMA_VERSION = 1
_NORMALIZER_VERSION = "lite-source-video-normalizer-v1"
_VIDEO_NORMALIZE_PARAMS = {
    "video_codec": "libx264",
    "pixel_format": "yuv420p",
    "preset": "veryfast",
    "crf": 18,
    "audio_codec": "aac",
    "audio_bitrate": "192k",
    "movflags": "+faststart",
}


class ReviewDocumentRunError(RuntimeError):
    def __init__(self, message: str, result: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.result = dict(result)


class LiteVisualAssetError(ValueError):
    """A privacy-safe, machine-readable visual material failure."""

    def __init__(self, code: str, item_id: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.item_id = str(item_id)
        self.details = {
            "status": "user_action_required",
            "code": self.code,
            "item_ids": [self.item_id] if self.item_id else [],
            "retryable": True,
        }

    def public_data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": sanitize_public_value(str(self)),
            "details": {"item_ids": list(self.details["item_ids"])},
        }
        if self.code == "visual_asset_ambiguous":
            payload["user_action_required"] = {
                "action_code": "high_risk_confirmation",
                "reason_code": self.code,
                "item_ids": list(self.details["item_ids"]),
            }
        return payload


def _read_json_object(path: str | os.PathLike[str], label: str) -> dict[str, Any]:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return payload


def _job_input_digest(
    snapshot_path: Path,
    project_path: Path,
    project: Mapping[str, Any],
    *,
    options: Mapping[str, Any],
) -> str:
    materials: dict[str, Any] = {}
    for field in ("source_video", "source_audio", "replacement_audio"):
        value = str(project.get(field) or "").strip()
        if not value:
            continue
        path = Path(value).expanduser().resolve(strict=False)
        materials[field] = {
            "path": os.path.normcase(str(path)),
            "sha256": sha256_file(path) if path.is_file() else "missing",
        }
    return canonical_json_sha256(
        {
            "snapshot_sha256": sha256_file(snapshot_path),
            "project_sha256": sha256_file(project_path),
            "materials": materials,
            "options": dict(options),
            "runner_version": RUNNER_VERSION,
        }
    )


def _artifact_map(paths: Sequence[Path], root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((value.resolve(strict=True) for value in paths), key=str):
        try:
            display = path.relative_to(root).as_posix()
        except ValueError:
            display = str(path)
        rows.append({"path": str(path), "display_path": display, "sha256": sha256_file(path)})
    return rows


def _draft_tree_digest(path: str | os.PathLike[str]) -> str:
    root = Path(path).expanduser().resolve(strict=True)
    if root.is_file():
        root = root.parent
    if not (root / "draft_content.json").is_file() or not (
        root / "draft_meta_info.json"
    ).is_file():
        raise FileNotFoundError(f"Draft has no saved content JSON: {root}")
    return str(capture_draft_tree_receipt(root)["tree_sha256"])


def _publish_phase_receipt(
    job_root: Path,
    phase: str,
    *,
    artifacts: Sequence[Path],
    trees: Sequence[tuple[str | os.PathLike[str], str]] = (),
    data: Mapping[str, Any] | None = None,
) -> tuple[Path, str]:
    artifact_rows = _artifact_map(artifacts, job_root)
    tree_rows = [
        {
            "path": str(Path(path).expanduser().resolve(strict=False)),
            "digest": digest,
        }
        for path, digest in trees
    ]
    safe_data = sanitize_public_value(dict(data or {}))
    output_digest = canonical_json_sha256(
        {
            "artifacts": artifact_rows,
            "data": safe_data,
            "trees": tree_rows,
            "phase": phase,
        }
    )
    receipt = {
        "schema_version": _SCHEMA_VERSION,
        "phase": phase,
        "runner_version": RUNNER_VERSION,
        "output_digest": output_digest,
        "artifacts": artifact_rows,
        "trees": tree_rows,
        "data": safe_data,
    }
    receipt_path = job_root / f"{phase}.receipt.json"
    atomic_write_json(receipt_path, receipt)
    return receipt_path, output_digest


def _phase_receipt_valid(
    store: JobStateStore,
    phase: str,
    receipt_path: Path,
) -> bool:
    record = store.get_phase(phase)
    if record is None or not receipt_path.is_file():
        return False
    try:
        receipt = _read_json_object(receipt_path, f"{phase} receipt")
        if (
            receipt.get("schema_version") != _SCHEMA_VERSION
            or receipt.get("phase") != phase
            or receipt.get("runner_version") != RUNNER_VERSION
            or receipt.get("output_digest") != record.get("output_digest")
        ):
            return False
        for artifact in receipt.get("artifacts") or []:
            path = Path(str(artifact["path"])).resolve(strict=True)
            if not path.is_file() or sha256_file(path) != artifact.get("sha256"):
                return False
        for tree in receipt.get("trees") or []:
            if _draft_tree_digest(str(tree["path"])) != tree.get("digest"):
                return False
        projected = {
            "artifacts": receipt.get("artifacts") or [],
            "data": receipt.get("data") or {},
            "trees": receipt.get("trees") or [],
            "phase": phase,
        }
        return canonical_json_sha256(projected) == record.get("output_digest")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _copy_cached_file(cache_path: Path, destination: Path) -> None:
    if destination.is_file() and sha256_file(destination) == sha256_file(cache_path):
        return
    atomic_copy_file(cache_path, destination)


def _cached_file(
    cache: ArtifactCache,
    identity: CacheIdentity,
    *,
    build: Callable[[Path], None],
    suffix: str,
) -> tuple[Path, bool]:
    cached = cache.get_file(identity)
    if cached is not None:
        return cached, True
    with cache_identity_lock(cache.root, identity.namespace, identity.digest()):
        cached = cache.get_file(identity)
        if cached is not None:
            return cached, True
        cache.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="auto-cut-cache-build-", dir=cache.root) as temp:
            output = Path(temp) / f"artifact{suffix}"
            build(output)
            cached = cache.store_file(identity, output)
    return cached, False


def _cached_asr_json(
    cache: ArtifactCache,
    identity: CacheIdentity,
    *,
    audio_path: Path,
    config: Any,
    inflight_root: Path,
    timeout_seconds: float,
    poll_interval_seconds: float,
    max_wait_seconds: float,
) -> tuple[dict[str, Any], bool]:
    # ASR payload schema is owned by the adapter. Runner receipt/schema bumps
    # must not discard a cache entry whose media hash and adapter identity
    # still match exactly.
    cached = cache.get_json(identity, _ASR_CACHE_SCHEMA_VERSION)
    if cached is not None:
        return cached, True
    with cache_identity_lock(cache.root, identity.namespace, identity.digest()):
        cached = cache.get_json(identity, _ASR_CACHE_SCHEMA_VERSION)
        if cached is not None:
            return cached, True
        ticket = inflight_root / identity.namespace / f"{identity.digest()}.json"
        payload = run_resumable_volc_asr(
            audio_path,
            config=config,
            identity_digest=identity.digest(),
            ticket_path=ticket,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            max_wait_seconds=max_wait_seconds,
        )
        cache.store_json(identity, payload)
        cached = cache.get_json(identity, _ASR_CACHE_SCHEMA_VERSION)
        if cached is None:
            raise RuntimeError("ASR cache publish could not be verified")
    return cached, False


def _normalize_webm(
    source: Path,
    output: Path,
    *,
    ffmpeg_bin: str,
) -> None:
    command = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not output.is_file():
        detail = (completed.stderr or completed.stdout or "normalization failed").strip()
        raise RuntimeError(f"Source video normalization failed: {detail[-1000:]}")


_LITE_EXECUTABLE_VISUAL_KINDS = frozenset(
    {
        "add_arrow",
        "add_hand",
        "add_pointer",
        "arrow_overlay",
        "circle_overlay",
        "hand_overlay",
        "hand_pointer",
        "image_overlay",
        "magnifier_overlay",
        "overlay",
        "pointer_overlay",
        "underline_overlay",
        "visual_delete",
        "visual_insert",
        "visual_overlay",
        "visual_replace",
    }
)
_VISUAL_PATH_FIELDS = (
    "asset_path",
    "asset_paths",
    "assets",
    "attachment_path",
    "attachment_paths",
    "downloaded_path",
    "local_path",
    "path",
)
_VISUAL_REFERENCE_FIELDS = frozenset(
    {
        *_VISUAL_PATH_FIELDS,
        "asset_ref",
        "asset_refs",
        "asset_token",
        "asset_url",
        "attachment_token",
        "download_url",
        "file_token",
        "media_token",
    }
)


def _visual_plan(item: Mapping[str, Any]) -> dict[str, Any]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    raw = item.get("visual_plan")
    if not isinstance(raw, Mapping):
        raw = evidence.get("visual_plan")
    return deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}


def _append_visual_paths(raw: Any, paths: list[str]) -> None:
    if isinstance(raw, (str, os.PathLike)):
        value = str(raw).strip()
        if value:
            paths.append(value)
        return
    if isinstance(raw, Mapping):
        for field in _VISUAL_PATH_FIELDS:
            if field in raw:
                _append_visual_paths(raw.get(field), paths)
        return
    if isinstance(raw, (list, tuple)):
        for value in raw:
            _append_visual_paths(value, paths)


def _visual_asset_paths(item: Mapping[str, Any]) -> list[str]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    plan = _visual_plan(item)
    paths: list[str] = []
    for container in (item, evidence):
        for field in _VISUAL_PATH_FIELDS:
            if field in container:
                _append_visual_paths(container.get(field), paths)
    for segment in plan.get("segments") or []:
        if isinstance(segment, Mapping):
            _append_visual_paths(segment, paths)
    return list(dict.fromkeys(paths))


def _has_visual_asset_reference(item: Mapping[str, Any]) -> bool:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    plan = _visual_plan(item)
    for container in (item, evidence, plan):
        if any(
            field in container and bool(container.get(field)) for field in _VISUAL_REFERENCE_FIELDS
        ):
            return True
    segments = plan.get("segments")
    return isinstance(segments, list) and bool(segments)


def _is_explicit_lite_visual(item: Mapping[str, Any]) -> bool:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    kinds = {
        str(value or "").strip().casefold()
        for value in (
            item.get("kind"),
            item.get("type"),
            item.get("source_kind"),
            evidence.get("operation"),
        )
        if str(value or "").strip()
    }
    # Lite executes only the maintained, explicit visual vocabulary.  A
    # generic prefix is intentionally not enough: a newly introduced or
    # misspelled kind must remain label-only even when it carries a local
    # visual_plan, until it has a dedicated execution contract and tests.
    return any(kind in _LITE_EXECUTABLE_VISUAL_KINDS for kind in kinds)


def _visual_plan_has_local_asset(item: Mapping[str, Any]) -> bool:
    plan = _visual_plan(item)
    segments = plan.get("segments")
    if not isinstance(segments, list) or not segments:
        return False
    for segment in segments:
        if not isinstance(segment, Mapping):
            return False
        segment_paths: list[str] = []
        _append_visual_paths(segment, segment_paths)
        if not segment_paths or not all(
            Path(path).expanduser().resolve(strict=False).is_file() for path in segment_paths
        ):
            return False
    return True


def _normalized_local_visual_assets(item: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    item_id = str(item.get("id") or item.get("item_id") or "")
    paths = _visual_asset_paths(item)
    has_reference = _has_visual_asset_reference(item)
    if not has_reference:
        return [], _visual_plan(item)
    if not paths:
        raise LiteVisualAssetError(
            "visual_asset_download_failed",
            item_id,
            f"Visual asset download did not produce a local file for item {item_id}",
        )

    normalized: list[str] = []
    replacements: dict[str, str] = {}
    for raw_path in paths:
        if "://" in raw_path:
            raise LiteVisualAssetError(
                "visual_asset_download_failed",
                item_id,
                f"Visual asset download did not produce a local file for item {item_id}",
            )
        path = Path(raw_path).expanduser().resolve(strict=False)
        if not path.is_file():
            raise LiteVisualAssetError(
                "visual_asset_download_failed",
                item_id,
                f"Visual asset download did not produce a local file for item {item_id}",
            )
        resolved = str(path)
        normalized.append(resolved)
        replacements[raw_path] = resolved

    plan = _visual_plan(item)
    raw_segments = plan.get("segments")
    if not raw_segments and len(normalized) > 1:
        raise LiteVisualAssetError(
            "visual_asset_ambiguous",
            item_id,
            f"Visual item {item_id} references multiple assets without an explicit segment plan",
        )
    if isinstance(raw_segments, list) and raw_segments:
        normalized_segments: list[dict[str, Any]] = []
        for segment in raw_segments:
            if not isinstance(segment, Mapping):
                raise LiteVisualAssetError(
                    "visual_asset_download_failed",
                    item_id,
                    f"Visual asset plan is incomplete for item {item_id}",
                )
            segment_paths: list[str] = []
            _append_visual_paths(segment, segment_paths)
            segment_paths = list(dict.fromkeys(segment_paths))
            if not segment_paths:
                raise LiteVisualAssetError(
                    "visual_asset_download_failed",
                    item_id,
                    f"Visual asset plan is incomplete for item {item_id}",
                )
            if len(segment_paths) > 1:
                raise LiteVisualAssetError(
                    "visual_asset_ambiguous",
                    item_id,
                    f"Visual asset plan is ambiguous for item {item_id}",
                )
            normalized_segment = deepcopy(dict(segment))
            normalized_segment["asset_path"] = replacements[segment_paths[0]]
            normalized_segments.append(normalized_segment)
        plan["segments"] = normalized_segments
    return list(dict.fromkeys(normalized)), plan


def _has_authoritative_visual_start(item: Mapping[str, Any]) -> bool:
    start = item.get("start")
    if isinstance(start, bool) or not isinstance(start, (int, float)) or float(start) < 0:
        return False
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    if str(evidence.get("timing_source") or "").strip().casefold() != "asr":
        return True
    alignment = evidence.get("asr_alignment")
    return bool(
        isinstance(alignment, Mapping)
        and alignment.get("status") == "pass"
        and alignment.get("authoritative_timing") is True
        and isinstance(alignment.get("resolved_time"), (int, float))
    )


def _is_lite_audio_or_asr_timing_item(item: Mapping[str, Any]) -> bool:
    """Keep audio/ASR-timed review rows out of the visual compiler.

    Some intake payloads carry a local ``visual_plan`` on an audio row (for
    example, as an attachment used for diagnosis).  The presence of that plan
    must never make the row eligible for an overlay or alter its ASR cut
    state.  Explicit visual rows remain eligible unless their instruction is
    itself a duration-changing request, which Lite always labels only.
    """

    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    kind = str(item.get("kind") or item.get("type") or item.get("source_kind") or "")
    source_text = str(item.get("source_text") or "")
    explicit_visual = _is_explicit_lite_visual(item)
    status = resolve_execution_status(
        item.get("execution_status"),
        item.get("evidence"),
        item.get("validation"),
    )
    if status.casefold().startswith("label_only_"):
        return True
    if lite_duration_change_is_label_only(kind, source_text):
        return True
    if explicit_visual and not lite_execution_required(kind, source_text, True):
        return True
    if explicit_visual:
        return False
    if str(evidence.get("timing_source") or "").strip().casefold() == "asr":
        return True
    return lite_timing_source(kind, source_text).casefold() == "asr"


def _compile_explicit_lite_visuals(request: dict[str, Any], ledger: dict[str, Any]) -> None:
    request_items = {
        str(item.get("id") or item.get("item_id") or "").casefold(): item
        for item in request.get("review_items") or []
        if isinstance(item, dict)
    }
    ledger_items = {
        str(item.get("id") or item.get("item_id") or "").casefold(): item
        for item in ledger.get("review_items") or []
        if isinstance(item, dict)
    }
    for item_id, item in request_items.items():
        if not item.get("execution_required"):
            continue
        if _is_lite_audio_or_asr_timing_item(item):
            # A stale pre-v2 pointer classification can still carry
            # execution_required=true.  Cleanup/removal requests are
            # label-only in Lite; normalize both request and ledger rows so
            # later phases cannot mistake the stale flag for executable work.
            kind = str(item.get("kind") or item.get("type") or item.get("source_kind") or "")
            source_text = str(item.get("source_text") or "")
            if kind.strip().casefold() in _LITE_EXECUTABLE_VISUAL_KINDS:
                if not lite_execution_required(kind, source_text, True):
                    for target in (item, ledger_items.get(item_id)):
                        if not isinstance(target, dict):
                            continue
                        target["execution_required"] = False
                        target["execution_status"] = "label_only_unresolved"
                        target_evidence = dict(target.get("evidence") or {})
                        target_evidence.update(
                            {
                                "execution_status": "label_only_unresolved",
                                "reason": "lite_pointer_cleanup_label_only",
                            }
                        )
                        target["evidence"] = target_evidence
            continue
        is_explicit_visual = _is_explicit_lite_visual(item)
        # A local plan is evidence for an already-maintained visual kind, not
        # an execution permission by itself.  Unknown/new review kinds stay
        # label-only under the Lite contract even when they happen to carry a
        # usable asset path.
        if not is_explicit_visual:
            # A stale or hand-authored request can mark an unknown visual
            # kind executable, with or without a local asset.  Downgrade it
            # before later validation so the marker is retained without
            # attempting a generic edit.  Audio/ASR rows have already been
            # returned above and are therefore unaffected.
            for target in (item, ledger_items.get(item_id)):
                if not isinstance(target, dict):
                    continue
                target["execution_required"] = False
                target["execution_status"] = "label_only_unresolved"
                target_evidence = dict(target.get("evidence") or {})
                target_evidence.update(
                    {
                        "execution_status": "label_only_unresolved",
                        "reason": "unknown_lite_visual_kind",
                    }
                )
                target["evidence"] = target_evidence
            continue
        paths, visual_plan = _normalized_local_visual_assets(item)
        evidence = dict(item.get("evidence") or {})
        if not _has_authoritative_visual_start(item):
            raise LiteVisualAssetError(
                "visual_timing_unresolved",
                str(item.get("id") or item.get("item_id") or ""),
                "Visual item has no authoritative timeline start",
            )
        if not paths:
            for target in (item, ledger_items.get(item_id)):
                if not isinstance(target, dict):
                    continue
                target["execution_required"] = False
                target["execution_status"] = "label_only_unresolved"
                target_evidence = dict(target.get("evidence") or {})
                target_evidence.update(
                    {
                        "execution_status": "label_only_unresolved",
                        "reason": "explicit_lite_visual_asset_missing",
                    }
                )
                target["evidence"] = target_evidence
            continue
        start = float(item.get("start") or 0.0)
        end = float(item.get("end") or start + 2.0)
        if end <= start:
            end = start + 2.0
        request.setdefault("edits", []).append(
            {
                "type": "add_overlay",
                "source_kind": str(item.get("kind") or "visual_overlay"),
                "doc_item_id": str(item.get("id") or item.get("item_id") or ""),
                "label": str(item.get("source_text") or ""),
                "detail": str(item.get("source_text") or ""),
                "start": start,
                "end": end,
                "asset_paths": paths,
                "visual_plan": visual_plan,
                "evidence": evidence,
            }
        )


_RUN_PHASES = (
    "preflight",
    "document_fetch",
    "asset_download",
    "input_compile",
    "source_hash",
    "source_asr",
    "classification",
    "reverse_asr",
    "draft_write_validate",
    "package_publish",
)
_PATH_KEYS = frozenset(
    {
        "asset_path",
        "asset_paths",
        "assets",
        "attachment_path",
        "attachment_paths",
        "downloaded_path",
        "local_path",
        "media_path",
        "path",
    }
)


def _json_safe(value: Any) -> Any:
    return sanitize_public_value(value)


def _json_compatible(value: Any) -> Any:
    """Normalize internal evidence without changing stable IDs or source text."""

    if isinstance(value, Mapping):
        return {str(key): _json_compatible(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(child) for child in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _path_rows_from_snapshot(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_paths: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key).strip().casefold())
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if key not in _PATH_KEYS or not isinstance(value, (str, os.PathLike)):
            return
        text = str(value).strip()
        if text and "://" not in text:
            raw_paths.append(text)

    visit(snapshot)
    rows: list[dict[str, Any]] = []
    for raw in dict.fromkeys(raw_paths):
        path = Path(raw).expanduser().resolve(strict=False)
        rows.append(
            {
                "path": os.path.normcase(str(path)),
                "sha256": sha256_file(path) if path.is_file() else "missing",
            }
        )
    return sorted(rows, key=lambda row: row["path"])


def _raw_item_ids(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    for key in ("review_items", "doc_items", "items"):
        rows = snapshot.get(key)
        if not isinstance(rows, list):
            continue
        result = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            item_id = str(row.get("id") or row.get("item_id") or "").strip()
            if item_id:
                result.append(item_id)
        return tuple(dict.fromkeys(result))
    return ()


def _phase_paths(job_root: Path) -> dict[str, Path]:
    workspace = job_root / "workspace"
    return {
        "workspace": workspace,
        "input_dir": workspace / "inputs",
        "asset_dir": workspace / "inputs" / "downloaded_assets",
        "asset_manifest": workspace / "inputs" / "asset_manifest.json",
        "snapshot": workspace / "inputs" / "document_snapshot.json",
        "project_original": workspace / "inputs" / "project_original.json",
        "project_lite": workspace / "inputs" / "project_lite.json",
        "compiled_base": workspace / "compiled_base",
        "materials_dir": workspace / "materials",
        "materials_ledger": workspace / "materials" / "source_materials.json",
        "effective_project": workspace / "materials" / "effective_project.json",
        "evidence_dir": workspace / "evidence",
        "visual_index": workspace / "evidence" / "visual_asset_index.json",
        "source_index": workspace / "evidence" / "source_asr_index.json",
        "source_asr": workspace / "evidence" / "source_asr.json",
        "alignment_wav": workspace / "materials" / "source_alignment.wav",
        "candidate_wav": workspace / "materials" / "candidate_source_aligned.wav",
        "classified_dir": workspace / "classified",
        "cut_plan": workspace / "classified" / "audio_cut_plan.json",
        "acceptance_plan": workspace / "classified" / "acceptance_plan.json",
        "processed_dir": workspace / "processed",
        "processed_request": workspace / "processed" / "revision_request.json",
        "processed_items": workspace / "processed" / "doc_items.json",
        "audio_plan": workspace / "processed" / "audio_delivery_plan.json",
        "reverse_report": workspace / "processed" / "reverse_asr_report.json",
        "processed_summary": workspace / "processed" / "processed_media_evidence.json",
        "execution_dir": workspace / "execution",
        "execution_result": workspace / "execution" / "revision_result.json",
        "final_result": workspace / "execution" / "final_acceptance.json",
    }


def _compiled_paths(root: Path) -> tuple[Path, Path, Path]:
    return (
        root / "revision_request.json",
        root / "doc_items.json",
        root / "job_manifest.json",
    )


def _receipt_data(path: Path) -> dict[str, Any] | None:
    try:
        payload = _read_json_object(path, "phase receipt")
    except (OSError, TypeError, ValueError):
        return None
    data = payload.get("data")
    return dict(data) if isinstance(data, Mapping) else None


def _source_text_index(payload: Mapping[str, Any], label: str) -> dict[str, str]:
    items = payload.get("review_items")
    if not isinstance(items, list):
        raise ValueError(f"{label}.review_items must be a list")
    result: dict[str, str] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"{label}.review_items[{index}] must be an object")
        item_id = str(item.get("id") or item.get("item_id") or "").strip()
        if not item_id:
            raise ValueError(f"{label}.review_items[{index}] is missing a stable ID")
        key = item_id.casefold()
        if key in result:
            raise ValueError(f"{label} has duplicate review item ID: {item_id}")
        result[key] = str(item.get("source_text") or "")
    return result


def _assert_source_text_fidelity(
    expected_payload: Mapping[str, Any],
    request: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> None:
    expected = _source_text_index(expected_payload, "source ledger")
    if _source_text_index(request, "revision request") != expected:
        raise ValueError("Revision request changed source_text or stable source item IDs")
    if _source_text_index(ledger, "compiled ledger") != expected:
        raise ValueError("Compiled ledger changed source_text or stable source item IDs")
    for edit in request.get("edits") or []:
        if not isinstance(edit, Mapping):
            continue
        item_id = str(edit.get("doc_item_id") or "").strip().casefold()
        if item_id and item_id in expected and str(edit.get("label") or "") != expected[item_id]:
            raise ValueError("Executable edit label does not equal source_text verbatim")


def _assert_authoritative_starts(ledger: Mapping[str, Any]) -> None:
    for item in ledger.get("review_items") or []:
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("id") or item.get("item_id") or "")
        start = item.get("start")
        if isinstance(start, bool) or not isinstance(start, (int, float)) or float(start) < 0:
            raise ValueError(
                f"Review item {item_id} has no authoritative non-negative start; refusing draft write"
            )


def _restore_non_asr_items(
    before_request: Mapping[str, Any],
    before_ledger: Mapping[str, Any],
    request: dict[str, Any],
    ledger: dict[str, Any],
    audio_item_ids: set[str],
) -> None:
    def restore(before: Mapping[str, Any], after: dict[str, Any]) -> None:
        originals = {
            str(item.get("id") or item.get("item_id") or "").casefold(): item
            for item in before.get("review_items") or []
            if isinstance(item, Mapping)
        }
        for item in after.get("review_items") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or item.get("item_id") or "").casefold()
            original = originals.get(key)
            if original is None or key in audio_item_ids:
                continue
            for field in ("execution_required", "execution_status", "evidence", "start", "end"):
                if field in original:
                    item[field] = deepcopy(original[field])
                else:
                    item.pop(field, None)

    restore(before_request, request)
    restore(before_ledger, ledger)


def _asr_required(doc_items: Mapping[str, Any]) -> bool:
    for item in doc_items.get("review_items") or []:
        if not isinstance(item, Mapping):
            continue
        evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
        if str(evidence.get("timing_source") or "").casefold() == "asr":
            return True
    return False


def _review_comment_time(item: Mapping[str, Any]) -> float | None:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    text_match = re.match(
        r"^\s*(?P<minutes>\d{1,3})\s*[:：]\s*(?P<seconds>\d{1,2}(?:\.\d+)?)",
        str(item.get("source_text") or ""),
    )
    text_time = (
        float(text_match.group("minutes")) * 60.0 + float(text_match.group("seconds"))
        if text_match is not None
        else None
    )
    for candidate in (
        evidence.get("review_search_hint_seconds"),
        evidence.get("resolved_review_timestamp_seconds"),
        text_time,
        item.get("start"),
    ):
        try:
            value = float(candidate)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value >= 0.0:
            return value
    return None


def _source_asr_unavailable_cut_plan(
    review_items: Sequence[Mapping[str, Any]],
    *,
    source_duration_seconds: float,
) -> dict[str, Any]:
    """Downgrade every ASR-timed item without inventing a nearby ASR boundary."""

    rows: list[dict[str, Any]] = []
    for index, raw_item in enumerate(review_items):
        item = dict(raw_item)
        evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
        if str(evidence.get("timing_source") or "").strip().casefold() != "asr":
            continue
        item_id = str(item.get("id") or item.get("item_id") or f"item_{index + 1:03d}")
        review_time = _review_comment_time(item)
        if review_time is None:
            raise ValueError(
                f"Lite audio item {item_id} has no review timestamp for safe ASR fallback"
            )
        if review_time > float(source_duration_seconds) + 1e-6:
            raise ValueError(
                f"Lite audio item {item_id} review timestamp exceeds the source duration"
            )
        status = str(item.get("execution_status") or "").strip()
        if not status.casefold().startswith("label_only_"):
            status = "label_only_unresolved"
        explicit_must_keep = evidence.get("must_keep")
        must_keep = (
            [str(value).strip() for value in explicit_must_keep if str(value).strip()]
            if isinstance(explicit_must_keep, list)
            else []
        )
        delete_phrase = ""
        for field in ("delete", "delete_phrase", "spoken_text", "target_phrase"):
            candidate = str(evidence.get(field) or item.get(field) or "").strip()
            if candidate:
                delete_phrase = candidate
                break
        rows.append(
            {
                "item_id": item_id,
                "kind": str(item.get("kind") or "review_only").strip().casefold(),
                "source_text": str(item.get("source_text") or ""),
                "status": "label_only",
                "execution_required": False,
                "execution_status": status,
                "strategy": str(evidence.get("strategy") or "precision_first"),
                "delete": delete_phrase,
                "must_keep": must_keep,
                "resolved_time": round(review_time, 6),
                "reason": "source_asr_unavailable",
                "match_method": "",
                "matches": [],
                "timing_source": "review_timestamp_fallback",
                "asr_alignment": None,
            }
        )
    return {
        "schema_version": _SCHEMA_VERSION,
        "planner_version": "source-asr-unavailable-label-fallback-v1",
        "source_duration_seconds": round(float(source_duration_seconds), 6),
        "source_asr_identity": {"status": "unavailable"},
        "source_asr_input_sha256": "",
        "rows": rows,
        "executable_cuts": [],
        "unresolved_item_ids": [str(row["item_id"]) for row in rows],
    }


def _media_tool_identity(binary: str, *, mock_media: bool) -> dict[str, Any]:
    if mock_media:
        return {"path": str(binary), "version": "mock-media", "sha256": ""}
    return ffmpeg_identity(binary)


def _phase_outcome(
    job_root: Path,
    phase: str,
    *,
    artifacts: Sequence[Path],
    trees: Sequence[tuple[str | os.PathLike[str], str]] = (),
    data: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
    cache_hit: bool | None = False,
) -> PhaseOutcome:
    receipt_path, digest = _publish_phase_receipt(
        job_root,
        phase,
        artifacts=artifacts,
        trees=trees,
        data=data,
    )
    payload = sanitize_public_value(dict(result or {}))
    payload.setdefault("receipt_path", str(receipt_path))
    return PhaseOutcome(payload, output_digest=digest, cache_hit=cache_hit)


def _package_receipt_path(package_zip: Path) -> Path:
    return package_zip.with_name(f"{package_zip.name}.receipt.json")


def _validate_existing_package(
    package_zip: Path,
    draft_path: Path,
    *,
    relink_tool: Path,
) -> dict[str, Any] | None:
    receipt_path = _package_receipt_path(package_zip)
    if not package_zip.is_file() or not receipt_path.is_file():
        return None
    try:
        receipt = _read_json_object(receipt_path, "Lite package receipt")
        draft_tree = capture_draft_tree_receipt(draft_path)
        if (
            receipt.get("status") != "pass"
            or receipt.get("workflow_mode") != "lite"
            or Path(str(receipt.get("archive_path") or "")).resolve(strict=False)
            != package_zip.resolve(strict=False)
            or str(receipt.get("archive_sha256") or "") != sha256_file(package_zip)
            or Path(str(receipt.get("source_draft_path") or "")).resolve(strict=False)
            != draft_path.resolve(strict=False)
            or str(receipt.get("source_tree_sha256") or "") != draft_tree["tree_sha256"]
            or receipt.get("package_tree_sha256") != receipt.get("extracted_tree_sha256")
            or receipt.get("zip_crc_pass") is not True
            or receipt.get("zip_tree_identity_pass") is not True
            or receipt.get("relink_tool_included") is not True
            or str(receipt.get("relink_tool_sha256") or "") != sha256_file(relink_tool)
            or receipt.get("json_rewritten") is not False
            or receipt.get("ui_invoked") is not False
            or receipt.get("opened_jianying") is not False
            or receipt.get("portable_package_invoked") is not False
        ):
            return None
        with zipfile.ZipFile(package_zip, "r") as archive:
            if archive.testzip() is not None:
                return None
        payload = dict(receipt)
        payload["receipt_path"] = str(receipt_path)
        payload["receipt_sha256"] = sha256_file(receipt_path)
        return payload
    except (OSError, TypeError, ValueError, zipfile.BadZipFile):
        return None


def _validate_marker_receipts(
    execution: Mapping[str, Any], ledger: Mapping[str, Any]
) -> None:
    if not isinstance(execution.get("validation"), Mapping) or execution["validation"].get(
        "ok"
    ) is not True:
        raise ValueError("Lite draft structural validation did not pass")
    if not isinstance(execution.get("acceptance_validation"), Mapping) or execution[
        "acceptance_validation"
    ].get("ok") is not True:
        raise ValueError("Lite draft strict acceptance did not pass")
    if execution["acceptance_validation"].get("skipped") is True:
        raise ValueError("Lite draft strict acceptance was skipped")
    expected = _source_text_index(ledger, "saved marker ledger")
    receipts = execution.get("review_marker_receipts")
    if not isinstance(receipts, list) or len(receipts) != len(expected):
        raise ValueError("Saved marker receipt count does not equal source item count")
    actual: dict[str, str] = {}
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise ValueError("Saved marker receipt must be an object")
        item_id = str(receipt.get("item_id") or "").strip()
        key = item_id.casefold()
        if not key or key in actual:
            raise ValueError("Saved marker receipts have a missing or duplicate source item ID")
        actual[key] = str(receipt.get("source_text") or "")
    if actual != expected:
        raise ValueError("Saved marker text is not code-point identical to source_text")


def _result_artifact(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "byte_size": path.stat().st_size,
    }


def run_review_document(
    snapshot_json: str | os.PathLike[str] | None = None,
    project_json: str | os.PathLike[str] | None = None,
    *,
    doc_url: str | None = None,
    job_root: str | os.PathLike[str],
    drafts_root: str | os.PathLike[str],
    package_zip: str | os.PathLike[str],
    relink_tool: str | os.PathLike[str] | None = None,
    mock_media: bool = False,
    asr_timeout_seconds: float = 60.0,
    asr_poll_interval_seconds: float = 2.0,
    asr_max_wait_seconds: float = 120.0,
    context_before: float = 5.0,
    context_after: float = 5.0,
    workflow_mode: str = "lite",
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    cache_root: str | os.PathLike[str] | None = None,
    max_workers: int = 1,
    progress: Callable[[Mapping[str, Any]], None] | None = None,
    lark_cli: str | os.PathLike[str] | None = None,
    lark_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    readiness_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Run the fixed Lite source-document DAG and publish a validated ZIP.

    Every phase communicates through hashed artifacts under ``job_root``.  A
    completed phase is resumed only when its receipt, files, and draft tree
    still match the persisted state.  ASR and media caches are addressed by
    source bytes plus the complete processing/service identity.
    """

    root = Path(job_root).expanduser().resolve(strict=False)
    state_path = root / "job_state.json"
    timing_path = root / "job_timing.json"
    phase_records: dict[str, dict[str, Any]] = {}
    failure_details: dict[str, Any] = {}
    store: JobStateStore | None = None
    paths = _phase_paths(root)
    package_path = Path(package_zip).expanduser().resolve(strict=False)
    snapshot_path = (
        Path(snapshot_json).expanduser().resolve(strict=False)
        if snapshot_json is not None and str(snapshot_json).strip()
        else paths["snapshot"]
    )
    project_path = (
        Path(project_json).expanduser().resolve(strict=False)
        if project_json is not None and str(project_json).strip()
        else paths["project_original"]
    )
    draft_path_text = ""
    runtime_integrity_receipt: dict[str, Any] | None = None

    def public_result(*, ok: bool, error: str = "") -> dict[str, Any]:
        state = store.snapshot() if store is not None else {}
        timing = store.timing_snapshot() if store is not None else {}
        persisted = state.get("phases") if isinstance(state.get("phases"), Mapping) else {}
        phases: dict[str, Any] = {}
        for name in _RUN_PHASES:
            run_record = dict(phase_records.get(name) or {"status": "pending", "result": None, "error": None})
            persisted_record = dict(persisted.get(name) or {})
            persisted_status = persisted_record.pop("status", None)
            phases[name] = {
                **persisted_record,
                "persisted_status": persisted_status,
                **run_record,
            }

        artifact_candidates = {
            "document_snapshot": paths["snapshot"],
            "project_lite": paths["project_lite"],
            "source_materials": paths["materials_ledger"],
            "visual_asset_index": paths["visual_index"],
            "source_asr_index": paths["source_index"],
            "source_asr": paths["source_asr"],
            "audio_cut_plan": paths["cut_plan"],
            "revision_request": paths["processed_request"],
            "doc_items": paths["processed_items"],
            "audio_delivery_plan": paths["audio_plan"],
            "reverse_asr_report": paths["reverse_report"],
            "processed_media_evidence": paths["processed_summary"],
            "revision_result": paths["execution_result"],
            "final_acceptance": paths["final_result"],
            "package_zip": package_path,
            "package_receipt": _package_receipt_path(package_path),
        }
        artifacts = {
            name: row
            for name, candidate in artifact_candidates.items()
            if (row := _result_artifact(candidate)) is not None
        }
        execution: dict[str, Any] = {}
        final: dict[str, Any] = {}
        try:
            if paths["execution_result"].is_file():
                execution = _read_json_object(paths["execution_result"], "revision result")
            if paths["final_result"].is_file():
                final = _read_json_object(paths["final_result"], "final acceptance")
        except (OSError, TypeError, ValueError):
            pass
        draft_path = str(execution.get("draft_path") or draft_path_text)
        unresolved: set[str] = set()
        for value in execution.get("label_only_unresolved_item_ids") or []:
            if str(value).strip():
                unresolved.add(str(value).strip())
        try:
            if paths["cut_plan"].is_file():
                cut_plan = _read_json_object(paths["cut_plan"], "audio cut plan")
                unresolved.update(
                    str(value).strip()
                    for value in cut_plan.get("unresolved_item_ids") or []
                    if str(value).strip()
                )
        except (OSError, TypeError, ValueError):
            pass
        phase_timing = timing.get("phases") if isinstance(timing.get("phases"), Mapping) else {}
        active_seconds = sum(float(row.get("active_seconds") or 0.0) for row in phase_timing.values())
        wait_seconds = sum(float(row.get("wait_seconds") or 0.0) for row in phase_timing.values())
        result = {
            "ok": ok,
            "runner_version": RUNNER_VERSION,
            "workflow_mode": "lite",
            "completion_boundary": "lite_zip_delivery",
            "job_root": str(root),
            "job_state_json": str(state_path),
            "job_timing_json": str(timing_path),
            "workspace_root": str(paths["workspace"]),
            "workspace": {
                "root": str(paths["workspace"]),
                "inputs": str(paths["input_dir"]),
                "materials": str(paths["materials_dir"]),
                "evidence": str(paths["evidence_dir"]),
                "classified": str(paths["classified_dir"]),
                "processed": str(paths["processed_dir"]),
                "execution": str(paths["execution_dir"]),
            },
            "output_artifacts": artifacts,
            "draft_path": draft_path,
            "package_zip": str(package_path),
            "delivery": dict(final.get("delivery") or {}),
            "phases": phases,
            "phase_execution": dict(phase_records),
            "unresolved_item_ids": sorted(unresolved),
            "timing": {
                "active_seconds": round(active_seconds, 6),
                "external_wait_seconds": round(wait_seconds, 6),
                "application_or_user_blocking_seconds": 0.0,
            },
            "failure_details": _json_safe(failure_details),
        }
        if error:
            result["error"] = safe_error_text(error)
        return result

    try:
        has_doc_url = bool(str(doc_url or "").strip())
        has_snapshot = snapshot_json is not None and bool(str(snapshot_json).strip())
        has_project = project_json is not None and bool(str(project_json).strip())
        if has_doc_url:
            if has_snapshot or has_project:
                raise ValueError(
                    "doc_url is mutually exclusive with snapshot_json and project_json"
                )
            validated_doc_url = validate_document_url(str(doc_url))
        else:
            if not (has_snapshot and has_project):
                raise ValueError("JSON input mode requires snapshot_json and project_json")
            validated_doc_url = ""
        if str(workflow_mode).strip().casefold() != "lite":
            raise ValueError("review-document-run supports workflow_mode=lite only")
        if not isinstance(mock_media, bool):
            raise TypeError("mock_media must be a boolean")
        for value, label, allow_zero in (
            (asr_timeout_seconds, "asr_timeout_seconds", False),
            (asr_poll_interval_seconds, "asr_poll_interval_seconds", True),
            (asr_max_wait_seconds, "asr_max_wait_seconds", False),
            (context_before, "context_before", True),
            (context_after, "context_after", True),
        ):
            number = float(value)
            if not (number >= 0 if allow_zero else number > 0):
                raise ValueError(f"{label} must be {'non-negative' if allow_zero else 'positive'}")
        if package_path.suffix.casefold() != ".zip":
            raise ValueError("package_zip must end with .zip")

        root.mkdir(parents=True, exist_ok=True)
        for key in (
            "input_dir",
            "asset_dir",
            "compiled_base",
            "materials_dir",
            "evidence_dir",
            "classified_dir",
            "processed_dir",
            "execution_dir",
        ):
            paths[key].mkdir(parents=True, exist_ok=True)
        drafts_path = Path(drafts_root).expanduser().resolve(strict=False)
        drafts_path.mkdir(parents=True, exist_ok=True)
        package_path.parent.mkdir(parents=True, exist_ok=True)
        relink_path = (
            Path(relink_tool).expanduser().resolve(strict=True)
            if relink_tool is not None
            else Path(__file__).resolve().parents[2]
            / "tools"
            / "relink_tool"
            / "Auto-Cut剪映素材重链工具.exe"
        )
        relink_path = relink_path.resolve(strict=True)
        if not relink_path.is_file():
            raise FileNotFoundError(f"Lite relink tool is missing: {relink_path}")

        if has_doc_url:
            job_identity = {
                "input_mode": "url",
                "document_url_sha256": document_url_digest(validated_doc_url),
                "workflow_mode": "lite",
            }
        else:
            snapshot_path = snapshot_path.resolve(strict=True)
            project_path = project_path.resolve(strict=True)
            identity_snapshot = sanitize_document_snapshot(
                _read_json_object(snapshot_path, "document snapshot")
            )
            identity_document = identity_snapshot.get("document")
            identity_digest = (
                str(identity_document.get("document_identity_sha256") or "")
                if isinstance(identity_document, Mapping)
                else ""
            )
            job_identity = {
                "input_mode": "json",
                "document_identity_sha256": identity_digest
                or canonical_json_sha256(identity_snapshot),
                "snapshot_path_sha256": canonical_json_sha256(
                    os.path.normcase(str(snapshot_path))
                ),
                "project_path_sha256": canonical_json_sha256(
                    os.path.normcase(str(project_path))
                ),
                "workflow_mode": "lite",
            }
        input_digest = canonical_json_sha256(job_identity)
        store = JobStateStore(state_path, input_digest, RUNNER_VERSION)

        snapshot: dict[str, Any] = {}
        raw_project: dict[str, Any] = {}
        lite_project: dict[str, Any] = {}
        snapshot_sha256 = ""
        project_sha256 = ""
        snapshot_assets: list[dict[str, Any]] = []
        expected_project_materials: dict[str, dict[str, str]] = {}
        input_options = {
            "workflow_mode": "lite",
            "lite_cut_layout": "split_gap",
            "mock_media": mock_media,
            "ffmpeg_bin": ffmpeg_bin,
            "ffprobe_bin": ffprobe_bin,
            "context_before": float(context_before),
            "context_after": float(context_after),
            "package_zip": os.path.normcase(str(package_path)),
            "drafts_root": os.path.normcase(str(drafts_path)),
            "relink_tool_sha256": sha256_file(relink_path),
        }
        cache_path = (
            Path(cache_root).expanduser().resolve(strict=False)
            if cache_root is not None
            else root.parent / ".auto-cut-review-cache"
        )
        cache = ArtifactCache(cache_path)
        inflight_root = cache_path / "inflight"
        item_ids: tuple[str, ...] = ()
        intake: dict[str, Any] = {}

        def run_preflight() -> PhaseOutcome:
            nonlocal runtime_integrity_receipt
            if not mock_media:
                from utils.runtime_integrity import validate_current_lite_runtime

                runtime_integrity_receipt = validate_current_lite_runtime()
            else:
                runtime_integrity_receipt = None
            lark_version = ""
            if has_doc_url:
                try:
                    lark_version = lark_cli_version(lark_cli=lark_cli, runner=lark_runner)
                    intake["lark_version"] = lark_version
                    intake["whoami"] = lark_whoami(lark_cli=lark_cli, runner=lark_runner)
                    evaluate_runtime_readiness(
                        path=readiness_path,
                        runtime_version=RUNNER_VERSION,
                        lark_version=lark_version,
                        asr_adapter_version=VOLC_ASR_ADAPTER_VERSION,
                    )
                except ReviewDocumentIntakeError as exc:
                    invalidate_lark_readiness(exc.code, path=readiness_path)
                    failure_details["preflight"] = _json_safe(exc.public_data())
                    raise
                except Exception as exc:
                    invalidate_lark_readiness(
                        "lark_user_identity_unavailable", path=readiness_path
                    )
                    failure_details["preflight"] = {
                        "code": "lark_user_identity_unavailable",
                        "message": "Feishu/Lark user identity validation failed",
                        "details": {"error": safe_error_text(exc)},
                    }
                    raise RuntimeError(
                        "Feishu/Lark user identity validation failed"
                    ) from exc
            return _phase_outcome(
                root,
                "preflight",
                artifacts=[],
                data={
                    "input_mode": "url" if has_doc_url else "json",
                    "runtime_integrity": _json_safe(
                        runtime_integrity_receipt or {"status": "skipped_mock_media"}
                    ),
                    "lark_adapter_version": LARK_ADAPTER_VERSION if has_doc_url else "",
                    "lark_cli_version": lark_version,
                },
                result={"status": "pass"},
                cache_hit=False,
            )

        def run_document_fetch() -> PhaseOutcome:
            nonlocal snapshot, raw_project
            if has_doc_url:
                try:
                    fetched = fetch_lark_document(
                        validated_doc_url,
                        lark_cli=lark_cli,
                        runner=lark_runner,
                    )
                    parsed = parse_lark_document(fetched)
                except ReviewDocumentIntakeError as exc:
                    if exc.code in {
                        "document_fetch_failed",
                        "lark_user_identity_unavailable",
                    }:
                        invalidate_lark_readiness(exc.code, path=readiness_path)
                    failure_details["document_fetch"] = _json_safe(exc.public_data())
                    raise
                intake["parsed"] = parsed
                mark_lark_verified(
                    intake["whoami"],
                    path=readiness_path,
                    runtime_version=RUNNER_VERSION,
                    lark_version=str(intake["lark_version"]),
                    asr_adapter_version=VOLC_ASR_ADAPTER_VERSION,
                )
                data = {
                    "document_identity_sha256": parsed["document_identity_sha256"],
                    "revision": parsed["revision_id"],
                    "content_sha256": parsed["content_sha256"],
                    "asset_identity_sha256": parsed["asset_identity_sha256"],
                }
            else:
                snapshot = sanitize_document_snapshot(
                    _read_json_object(snapshot_path, "document snapshot")
                )
                raw_project = _read_json_object(project_path, "project")
                document = snapshot.get("document")
                data = {
                    "document_identity_sha256": (
                        str(document.get("document_identity_sha256") or "")
                        if isinstance(document, Mapping)
                        else ""
                    ),
                    "snapshot_sha256": canonical_json_sha256(snapshot),
                    "project_sha256": canonical_json_sha256(raw_project),
                }
            return _phase_outcome(
                root,
                "document_fetch",
                artifacts=[],
                data=data,
                result=data,
                cache_hit=False,
            )

        def run_asset_download() -> PhaseOutcome:
            if has_doc_url:
                try:
                    downloaded = download_lark_assets(
                        intake["parsed"],
                        paths["asset_dir"],
                        lark_cli=lark_cli,
                        runner=lark_runner,
                        progress=progress,
                    )
                except ReviewDocumentIntakeError as exc:
                    failure_details["asset_download"] = _json_safe(exc.public_data())
                    raise
                failure_details.pop("asset_download", None)
                intake["downloaded_assets"] = downloaded
                rows = [
                    {
                        "asset_id": row["asset_id"],
                        "sha256": row["sha256"],
                        "byte_size": row["byte_size"],
                        "extension": row["extension"],
                    }
                    for row in downloaded
                ]
                artifacts = [Path(str(row["path"])) for row in downloaded]
                cache_hit = bool(downloaded) and all(
                    bool(row.get("cache_hit")) for row in downloaded
                )
            else:
                rows = _path_rows_from_snapshot(snapshot)
                intake["downloaded_assets"] = []
                artifacts = []
                cache_hit = None
            return _phase_outcome(
                root,
                "asset_download",
                artifacts=artifacts,
                data={"assets": rows},
                result={"asset_count": len(rows)},
                cache_hit=cache_hit,
            )

        def run_input_compile() -> PhaseOutcome:
            nonlocal snapshot, raw_project, lite_project
            nonlocal snapshot_sha256, project_sha256, snapshot_assets
            nonlocal expected_project_materials, item_ids
            if has_doc_url:
                try:
                    compiled_inputs = compile_url_inputs(
                        intake["parsed"], intake["downloaded_assets"]
                    )
                except ReviewDocumentIntakeError as exc:
                    failure_details["input_compile"] = _json_safe(exc.public_data())
                    raise
                failure_details.pop("input_compile", None)
                snapshot = sanitize_document_snapshot(compiled_inputs["snapshot"])
                raw_project = dict(compiled_inputs["project"])
                atomic_write_json(paths["asset_manifest"], compiled_inputs["asset_manifest"])
            atomic_write_json(paths["snapshot"], snapshot)
            atomic_write_json(paths["project_original"], raw_project)
            snapshot_sha256 = sha256_file(paths["snapshot"])
            project_sha256 = sha256_file(paths["project_original"])
            snapshot_assets = _path_rows_from_snapshot(snapshot)
            explicit_mode = str(raw_project.get("workflow_mode") or "").strip().casefold()
            if explicit_mode and explicit_mode != "lite":
                raise ValueError("Project explicitly requests a non-Lite workflow")
            explicit_layout = str(raw_project.get("lite_cut_layout") or "").strip().casefold()
            if explicit_layout and explicit_layout != "split_gap":
                raise ValueError("New Lite review-document jobs require lite_cut_layout=split_gap")
            lite_project = deepcopy(raw_project)
            lite_project["workflow_mode"] = "lite"
            lite_project["lite_cut_layout"] = "split_gap"
            atomic_write_json(paths["project_lite"], lite_project)
            expected_project_materials = {}
            for field in ("source_video", "source_audio", "replacement_audio"):
                raw_value = str(lite_project.get(field) or "").strip()
                if not raw_value:
                    continue
                material_path = Path(raw_value).expanduser().resolve(strict=False)
                expected_project_materials[field] = {
                    "path": os.path.normcase(str(material_path)),
                    "sha256": (
                        sha256_file(material_path) if material_path.is_file() else "missing"
                    ),
                }
            compiled = compile_review_job(
                _read_json_object(paths["snapshot"], "job document snapshot"),
                _read_json_object(paths["project_lite"], "Lite project"),
                paths["compiled_base"],
            )
            revision_path, ledger_path, manifest_path = _compiled_paths(paths["compiled_base"])
            request = _read_json_object(revision_path, "base revision request")
            ledger = _read_json_object(ledger_path, "base source ledger")
            if request.get("workflow_mode") != "lite" or request.get("lite_cut_layout") != "split_gap":
                raise ValueError("Compiler did not preserve the required Lite split-gap identity")
            _assert_source_text_fidelity(ledger, request, ledger)
            compiled_ids = list(_source_text_index(ledger, "base source ledger"))
            item_ids = tuple(compiled_ids)
            artifacts = [
                paths["snapshot"],
                paths["project_original"],
                paths["project_lite"],
                revision_path,
                ledger_path,
                manifest_path,
            ]
            if has_doc_url:
                artifacts.append(paths["asset_manifest"])
            return _phase_outcome(
                root,
                "input_compile",
                artifacts=artifacts,
                data={"item_ids": compiled_ids, "compiled": _json_safe(compiled)},
                result={
                    "revision_request": str(revision_path),
                    "doc_items": str(ledger_path),
                    "job_manifest": str(manifest_path),
                },
                cache_hit=None,
            )

        def run_source_materials() -> PhaseOutcome:
            project = _read_json_object(paths["project_lite"], "Lite project")
            source_video = Path(str(project.get("source_video") or "")).expanduser().resolve(strict=True)
            if not source_video.is_file():
                raise FileNotFoundError(f"Source video is missing: {source_video}")
            expected_source = expected_project_materials.get("source_video") or {}
            if (
                os.path.normcase(str(source_video)) != expected_source.get("path")
                or sha256_file(source_video) != expected_source.get("sha256")
            ):
                raise RuntimeError("Source video changed after the job input identity was captured")
            optional_materials: dict[str, Path] = {}
            for field in ("source_audio", "replacement_audio"):
                value = str(project.get(field) or "").strip()
                if not value:
                    continue
                candidate = Path(value).expanduser().resolve(strict=True)
                if not candidate.is_file():
                    raise FileNotFoundError(f"{field} is missing: {candidate}")
                expected = expected_project_materials.get(field) or {}
                if (
                    os.path.normcase(str(candidate)) != expected.get("path")
                    or sha256_file(candidate) != expected.get("sha256")
                ):
                    raise RuntimeError(
                        f"{field} changed after the job input identity was captured"
                    )
                optional_materials[field] = candidate

            ffmpeg_info = _media_tool_identity(ffmpeg_bin, mock_media=mock_media)
            ffprobe_info = _media_tool_identity(ffprobe_bin, mock_media=mock_media)
            normalized = source_video.suffix.casefold() == ".webm"
            normalization_hit: bool | None = None
            normalization_identity_digest = ""
            effective_video = source_video
            if normalized:
                identity = CacheIdentity(
                    "normalized_source_video",
                    inputs={
                        "source_sha256": sha256_file(source_video),
                        "parameters": _VIDEO_NORMALIZE_PARAMS,
                    },
                    versions={
                        "normalizer": _NORMALIZER_VERSION,
                        "ffmpeg": ffmpeg_info,
                        "mock_media": mock_media,
                    },
                )
                normalization_identity_digest = identity.digest()

                def normalize(output: Path) -> None:
                    if mock_media:
                        atomic_copy_file(source_video, output)
                    else:
                        _normalize_webm(source_video, output, ffmpeg_bin=ffmpeg_bin)

                cached_video, normalization_hit = _cached_file(
                    cache,
                    identity,
                    build=normalize,
                    suffix=".mp4",
                )
                effective_video = paths["materials_dir"] / "source_video.normalized.mp4"
                _copy_cached_file(cached_video, effective_video)

            if mock_media:
                duration = float(project.get("media_duration_seconds") or 0.0)
                if duration <= 0:
                    raise ValueError(
                        "mock_media requires project.media_duration_seconds for deterministic timing"
                    )
                has_audio = True
                has_video = True
            else:
                media_probe = probe_media(effective_video, ffprobe_bin=ffprobe_bin)
                duration = float(media_probe.duration_seconds)
                has_audio = bool(media_probe.has_audio)
                has_video = bool(media_probe.has_video)
                if not has_video:
                    raise ValueError("Source media has no video stream")
                if not has_audio and "source_audio" not in optional_materials:
                    raise ValueError("Source media has no audio stream for authoritative ASR")

            effective_project = deepcopy(project)
            effective_project["source_video"] = str(effective_video)
            effective_project["source_audio"] = str(optional_materials.get("source_audio") or "")
            effective_project["replacement_audio"] = str(
                optional_materials.get("replacement_audio") or ""
            )
            effective_project["media_duration_seconds"] = duration
            alignment_source = optional_materials.get("source_audio", effective_video)
            material_rows = {
                "source_video_original": {
                    "path": str(source_video),
                    "sha256": sha256_file(source_video),
                },
                "source_video_effective": {
                    "path": str(effective_video),
                    "sha256": sha256_file(effective_video),
                },
                "alignment_source": {
                    "path": str(alignment_source),
                    "sha256": sha256_file(alignment_source),
                },
            }
            for field, candidate in optional_materials.items():
                material_rows[field] = {"path": str(candidate), "sha256": sha256_file(candidate)}
            materials = {
                "schema_version": _SCHEMA_VERSION,
                "source_duration_seconds": round(duration, 6),
                "has_audio": has_audio,
                "has_video": has_video,
                "normalized_webm": normalized,
                "normalization_identity_digest": normalization_identity_digest,
                "ffmpeg_identity": ffmpeg_info,
                "ffprobe_identity": ffprobe_info,
                "materials": material_rows,
            }
            atomic_write_json(paths["materials_ledger"], materials)
            atomic_write_json(paths["effective_project"], effective_project)
            artifacts = [
                paths["materials_ledger"],
                paths["effective_project"],
                source_video,
                effective_video,
                *optional_materials.values(),
            ]
            return _phase_outcome(
                root,
                "source_hash",
                artifacts=list(dict.fromkeys(artifacts)),
                data={
                    "normalized_webm": normalized,
                    "normalization_identity_digest": normalization_identity_digest,
                    "ffmpeg_identity": ffmpeg_info,
                    "ffprobe_identity": ffprobe_info,
                },
                result={
                    "source_materials": str(paths["materials_ledger"]),
                    "effective_project": str(paths["effective_project"]),
                },
                cache_hit=normalization_hit,
            )

        def source_materials_resume() -> bool:
            phase = "source_hash"
            receipt_path = root / f"{phase}.receipt.json"
            if not _phase_receipt_valid(store, phase, receipt_path):
                return False
            data = _receipt_data(receipt_path)
            if data is None:
                return False
            return data.get("ffmpeg_identity") == _media_tool_identity(
                ffmpeg_bin, mock_media=mock_media
            ) and data.get("ffprobe_identity") == _media_tool_identity(
                ffprobe_bin, mock_media=mock_media
            )

        def run_source_asr_visual_index() -> PhaseOutcome:
            _revision_path, ledger_path, _manifest_path = _compiled_paths(paths["compiled_base"])
            ledger = _read_json_object(ledger_path, "base source ledger")
            materials = _read_json_object(paths["materials_ledger"], "source materials")
            visual_rows: list[dict[str, Any]] = []
            visual_files: list[Path] = []
            expected_asset_hashes = {
                str(row["path"]): str(row["sha256"]) for row in snapshot_assets
            }
            for item in ledger.get("review_items") or []:
                if not isinstance(item, Mapping):
                    continue
                item_id = str(item.get("id") or item.get("item_id") or "")
                if not item.get("execution_required"):
                    continue
                if _is_lite_audio_or_asr_timing_item(item):
                    continue
                if not _is_explicit_lite_visual(item):
                    continue
                try:
                    local_assets, _visual_plan_payload = _normalized_local_visual_assets(item)
                except LiteVisualAssetError as exc:
                    failure_details["source_asr"] = exc.public_data()
                    raise
                for raw_path in local_assets:
                    asset = Path(raw_path).resolve(strict=True)
                    visual_files.append(asset)
                    asset_sha256 = sha256_file(asset)
                    expected_sha256 = expected_asset_hashes.get(os.path.normcase(str(asset)))
                    if expected_sha256 is not None and expected_sha256 != asset_sha256:
                        raise RuntimeError(
                            f"Visual asset for {item_id} changed after job identity capture"
                        )
                    visual_rows.append(
                        {"item_id": item_id, "path": str(asset), "sha256": asset_sha256}
                    )
            atomic_write_json(
                paths["visual_index"],
                {"schema_version": _SCHEMA_VERSION, "assets": visual_rows},
            )

            needs_asr = _asr_required(ledger)
            source_index: dict[str, Any] = {
                "schema_version": _SCHEMA_VERSION,
                "asr_required": needs_asr,
                "visual_asset_count": len(visual_rows),
            }
            cache_hits: list[bool] = []
            artifacts = [paths["visual_index"], *visual_files]
            if needs_asr:
                try:
                    material_rows = materials.get("materials")
                    if not isinstance(material_rows, Mapping):
                        raise ValueError("Source material ledger is missing material identities")
                    alignment_row = material_rows.get("alignment_source")
                    if not isinstance(alignment_row, Mapping):
                        raise ValueError("Source material ledger is missing alignment_source")
                    alignment_source = Path(
                        str(alignment_row.get("path") or "")
                    ).resolve(strict=True)
                    ffmpeg_info = materials.get("ffmpeg_identity")
                    if not isinstance(ffmpeg_info, Mapping):
                        raise ValueError("Source material ledger is missing FFmpeg identity")
                    alignment_identity_payload = alignment_cache_identity(
                        source_sha256=sha256_file(alignment_source), ffmpeg=ffmpeg_info
                    )
                    alignment_identity = CacheIdentity(
                        "source_alignment_wav",
                        inputs=alignment_identity_payload["inputs"],
                        versions=alignment_identity_payload["versions"],
                    )
                    cached_alignment, alignment_hit = _cached_file(
                        cache,
                        alignment_identity,
                        build=lambda output: extract_alignment_wav(
                            alignment_source, output, ffmpeg_bin=ffmpeg_bin
                        ),
                        suffix=".wav",
                    )
                    cache_hits.append(alignment_hit)
                    _copy_cached_file(cached_alignment, paths["alignment_wav"])
                    config = load_volc_asr_config()
                    source_identity_payload = source_asr_cache_identity(
                        alignment_audio_sha256=sha256_file(paths["alignment_wav"]), config=config
                    )
                    source_identity = CacheIdentity(
                        "source_asr_words",
                        inputs=source_identity_payload["inputs"],
                        versions=source_identity_payload["versions"],
                    )
                    wait_started = time.monotonic()
                    source_hit = False
                    try:
                        source_asr, source_hit = _cached_asr_json(
                            cache,
                            source_identity,
                            audio_path=paths["alignment_wav"],
                            config=config,
                            inflight_root=inflight_root,
                            timeout_seconds=float(asr_timeout_seconds),
                            poll_interval_seconds=float(asr_poll_interval_seconds),
                            max_wait_seconds=float(asr_max_wait_seconds),
                        )
                    finally:
                        if not source_hit:
                            store.add_wait_seconds(
                                "source_asr",
                                max(0.0, time.monotonic() - wait_started),
                            )
                    cache_hits.append(source_hit)
                    input_sha256 = str(source_asr.get("input_sha256") or "")
                    if input_sha256 and input_sha256 != sha256_file(paths["alignment_wav"]):
                        raise ValueError(
                            "Source ASR input identity does not match alignment WAV bytes"
                        )
                    words = source_asr.get("words")
                    if not isinstance(words, list) or not words:
                        raise ValueError("Source ASR did not return real word-level timing rows")
                    atomic_write_json(paths["source_asr"], source_asr)
                    if not mock_media:
                        mark_asr_verified(
                            provider=str(source_asr.get("provider") or ""),
                            model_or_resource=str(
                                source_asr.get("model")
                                or source_asr.get("resource_id")
                                or ""
                            ),
                            adapter_version=str(source_asr.get("adapter_version") or ""),
                            path=readiness_path,
                        )
                    source_index.update(
                        {
                            "asr_available": True,
                            "asr_status": "verified",
                            "alignment_audio_path": str(paths["alignment_wav"]),
                            "alignment_audio_sha256": sha256_file(paths["alignment_wav"]),
                            "alignment_cache_identity_digest": alignment_identity.digest(),
                            "source_asr_path": str(paths["source_asr"]),
                            "source_asr_sha256": sha256_file(paths["source_asr"]),
                            "source_asr_cache_identity_digest": source_identity.digest(),
                        }
                    )
                    failure_details.pop("source_asr", None)
                    artifacts.extend([paths["alignment_wav"], paths["source_asr"]])
                except Exception as exc:
                    public_failure = {
                        "code": "source_asr_unavailable",
                        "message": (
                            "Source ASR was unavailable; ASR-timed review items will use "
                            "their review-comment timestamps as label-only fallbacks"
                        ),
                        "details": {"error": safe_error_text(exc)},
                    }
                    failure_details["source_asr"] = _json_safe(public_failure)
                    source_index.update(
                        {
                            "asr_available": False,
                            "asr_status": "unavailable",
                            "fallback_policy": "review_comment_time_label_only",
                            "reason_code": "source_asr_unavailable",
                        }
                    )
                    atomic_write_json(
                        paths["source_asr"],
                        {
                            "schema_version": _SCHEMA_VERSION,
                            "status": "unavailable",
                            "reason_code": "source_asr_unavailable",
                            "words": [],
                        },
                    )
                    if paths["alignment_wav"].is_file():
                        artifacts.append(paths["alignment_wav"])
                    artifacts.append(paths["source_asr"])
            else:
                atomic_write_json(
                    paths["source_asr"],
                    {"schema_version": _SCHEMA_VERSION, "status": "not_required", "words": []},
                )
                source_index.update({"asr_available": False, "asr_status": "not_required"})
                artifacts.append(paths["source_asr"])
            atomic_write_json(paths["source_index"], source_index)
            artifacts.append(paths["source_index"])
            return _phase_outcome(
                root,
                "source_asr",
                artifacts=list(dict.fromkeys(artifacts)),
                data={
                    "asr_required": needs_asr,
                    "asr_available": source_index.get("asr_available", False),
                    "asr_status": source_index.get("asr_status", ""),
                    "alignment_cache_identity_digest": source_index.get(
                        "alignment_cache_identity_digest", ""
                    ),
                    "source_asr_cache_identity_digest": source_index.get(
                        "source_asr_cache_identity_digest", ""
                    ),
                },
                result={
                    "source_asr_index": str(paths["source_index"]),
                    "visual_asset_index": str(paths["visual_index"]),
                },
                cache_hit=(all(cache_hits) if cache_hits else None),
            )

        def source_asr_resume() -> bool:
            phase = "source_asr"
            receipt_path = root / f"{phase}.receipt.json"
            if not _phase_receipt_valid(store, phase, receipt_path):
                return False
            data = _receipt_data(receipt_path)
            if data is None:
                return False
            if not data.get("asr_required"):
                return True
            if data.get("asr_available") is not True:
                # A later invocation must retry the service instead of making a
                # temporary label-only fallback permanently resumable.
                return False
            if not paths["alignment_wav"].is_file():
                return False
            config = load_volc_asr_config()
            identity_payload = source_asr_cache_identity(
                alignment_audio_sha256=sha256_file(paths["alignment_wav"]), config=config
            )
            identity = CacheIdentity(
                "source_asr_words",
                inputs=identity_payload["inputs"],
                versions=identity_payload["versions"],
            )
            valid = data.get("source_asr_cache_identity_digest") == identity.digest()
            if valid and not mock_media:
                source_asr = _read_json_object(paths["source_asr"], "source ASR")
                if not isinstance(source_asr.get("words"), list) or not source_asr["words"]:
                    return False
                mark_asr_verified(
                    provider=str(source_asr.get("provider") or ""),
                    model_or_resource=str(
                        source_asr.get("model") or source_asr.get("resource_id") or ""
                    ),
                    adapter_version=str(source_asr.get("adapter_version") or ""),
                    path=readiness_path,
                )
            return valid

        def run_classified_plans() -> PhaseOutcome:
            snapshot_payload = _read_json_object(paths["snapshot"], "job document snapshot")
            effective_project = _read_json_object(paths["effective_project"], "effective project")
            compile_review_job(snapshot_payload, effective_project, paths["classified_dir"])
            revision_path, ledger_path, manifest_path = _compiled_paths(paths["classified_dir"])
            request = _read_json_object(revision_path, "classified revision request")
            ledger = _read_json_object(ledger_path, "classified source ledger")
            source_index = _read_json_object(paths["source_index"], "source ASR index")
            materials = _read_json_object(paths["materials_ledger"], "source materials")
            if source_index.get("asr_required") and source_index.get("asr_available") is True:
                cut_plan = resolve_lite_audio_items(
                    ledger.get("review_items") or [],
                    _read_json_object(paths["source_asr"], "source ASR"),
                    source_duration_seconds=float(materials["source_duration_seconds"]),
                )
            elif source_index.get("asr_required"):
                cut_plan = _source_asr_unavailable_cut_plan(
                    ledger.get("review_items") or [],
                    source_duration_seconds=float(materials["source_duration_seconds"]),
                )
            else:
                cut_plan = {
                    "schema_version": _SCHEMA_VERSION,
                    "planner_version": "not-required",
                    "source_duration_seconds": float(materials["source_duration_seconds"]),
                    "rows": [],
                    "executable_cuts": [],
                    "unresolved_item_ids": [],
                }
            atomic_write_json(paths["cut_plan"], cut_plan)
            acceptance_plan = {
                "schema_version": _SCHEMA_VERSION,
                "workflow_mode": request.get("workflow_mode"),
                "lite_cut_layout": request.get("lite_cut_layout"),
                "acceptance": request.get("acceptance") or {},
                "acceptance_profile": request.get("acceptance_profile") or {},
                "item_ids": list(_source_text_index(ledger, "classified source ledger")),
                "audio_item_ids": [str(row.get("item_id") or "") for row in cut_plan["rows"]],
                "unresolved_item_ids": list(cut_plan.get("unresolved_item_ids") or []),
                "context_window_seconds": {
                    "before": float(context_before),
                    "after": float(context_after),
                },
            }
            atomic_write_json(paths["acceptance_plan"], acceptance_plan)
            base_ledger = _read_json_object(
                _compiled_paths(paths["compiled_base"])[1], "base source ledger"
            )
            _assert_source_text_fidelity(base_ledger, request, ledger)
            return _phase_outcome(
                root,
                "classification",
                artifacts=[
                    revision_path,
                    ledger_path,
                    manifest_path,
                    paths["cut_plan"],
                    paths["acceptance_plan"],
                ],
                data={"unresolved_item_ids": cut_plan.get("unresolved_item_ids") or []},
                result={
                    "revision_request": str(revision_path),
                    "doc_items": str(ledger_path),
                    "audio_cut_plan": str(paths["cut_plan"]),
                    "acceptance_plan": str(paths["acceptance_plan"]),
                },
                cache_hit=None,
            )

        def run_processed_media() -> PhaseOutcome:
            classified_request_path, classified_items_path, _manifest = _compiled_paths(
                paths["classified_dir"]
            )
            before_request = _read_json_object(
                classified_request_path, "classified revision request"
            )
            before_ledger = _read_json_object(classified_items_path, "classified source ledger")
            request = deepcopy(before_request)
            ledger = deepcopy(before_ledger)
            cut_plan = _read_json_object(paths["cut_plan"], "audio cut plan")
            materials = _read_json_object(paths["materials_ledger"], "source materials")
            material_rows = materials.get("materials")
            if not isinstance(material_rows, Mapping):
                raise ValueError("Source material ledger is missing material identities")
            source_audio_row = material_rows.get("source_audio") or material_rows.get(
                "source_video_effective"
            )
            if not isinstance(source_audio_row, Mapping):
                raise ValueError("Source material ledger is missing editable source audio")
            source_audio = Path(str(source_audio_row.get("path") or "")).resolve(strict=True)
            executable_cuts = list(cut_plan.get("executable_cuts") or [])
            audio_rows = [
                row for row in cut_plan.get("rows") or [] if isinstance(row, Mapping)
            ]
            audio_item_ids = {
                str(row.get("item_id") or "").casefold() for row in audio_rows
            }
            cache_hits: list[bool] = []
            artifacts: list[Path] = []
            candidate: Path | None = None
            reverse_report: dict[str, Any] = {
                "schema_version": _SCHEMA_VERSION,
                "status": "not_required",
                "unresolved_ids": [],
                "rows": [],
            }
            if executable_cuts:
                if not paths["alignment_wav"].is_file():
                    raise FileNotFoundError("Executable audio cuts require the cached alignment WAV")
                candidate_identity_payload = candidate_cache_identity(
                    alignment_audio_sha256=sha256_file(paths["alignment_wav"]),
                    executable_cuts=executable_cuts,
                )
                candidate_identity = CacheIdentity(
                    "source_aligned_candidate",
                    inputs=candidate_identity_payload["inputs"],
                    versions=candidate_identity_payload["versions"],
                )
                cached_candidate, candidate_hit = _cached_file(
                    cache,
                    candidate_identity,
                    build=lambda output: render_source_aligned_candidate(
                        paths["alignment_wav"], output, delete_windows=executable_cuts
                    ),
                    suffix=".wav",
                )
                cache_hits.append(candidate_hit)
                _copy_cached_file(cached_candidate, paths["candidate_wav"])
                candidate = paths["candidate_wav"]
                audio_plan = build_lite_split_gap_audio_plan(
                    cut_plan,
                    source_audio_path=source_audio,
                    candidate_audio_path=candidate,
                )
            else:
                audio_plan = {"mode": "legacy"}

            if audio_rows:
                request, ledger = apply_audio_plan_to_compiled_payloads(
                    request,
                    ledger,
                    cut_plan,
                    audio_delivery_plan=audio_plan,
                    source_audio_path=source_audio,
                    candidate_audio_path=candidate,
                )
                _restore_non_asr_items(
                    before_request,
                    before_ledger,
                    request,
                    ledger,
                    audio_item_ids,
                )
            else:
                request["audio_delivery_plan"] = {"mode": "legacy"}
            try:
                _compile_explicit_lite_visuals(request, ledger)
            except LiteVisualAssetError as exc:
                failure_details["reverse_asr"] = exc.public_data()
                raise
            if request.get("pause_adjustments"):
                raise ValueError("Lite review-document runner refuses executable pause adjustments")
            atomic_write_json(paths["processed_request"], request)
            plan_digest = audio_delivery_plan_sha256(
                load_revision_request(str(paths["processed_request"]))
            )

            if candidate is not None:
                config = load_volc_asr_config()
                cut_plan_digest = canonical_json_sha256(cut_plan)
                reverse_identity_payload = reverse_asr_cache_identity(
                    candidate_audio_sha256=sha256_file(candidate),
                    cut_plan_sha256=cut_plan_digest,
                    config=config,
                )
                reverse_identity = CacheIdentity(
                    "candidate_reverse_asr",
                    inputs=reverse_identity_payload["inputs"],
                    versions=reverse_identity_payload["versions"],
                )
                wait_started = time.monotonic()
                reverse_hit = False
                try:
                    candidate_asr, reverse_hit = _cached_asr_json(
                        cache,
                        reverse_identity,
                        audio_path=candidate,
                        config=config,
                        inflight_root=inflight_root,
                        timeout_seconds=float(asr_timeout_seconds),
                        poll_interval_seconds=float(asr_poll_interval_seconds),
                        max_wait_seconds=float(asr_max_wait_seconds),
                    )
                finally:
                    if not reverse_hit:
                        store.add_wait_seconds(
                            "reverse_asr",
                            max(0.0, time.monotonic() - wait_started),
                        )
                cache_hits.append(reverse_hit)
                input_sha256 = str(candidate_asr.get("input_sha256") or "")
                if input_sha256 and input_sha256 != sha256_file(candidate):
                    raise ValueError("Reverse ASR input identity does not match candidate bytes")
                reverse_report = build_full_candidate_reverse_report(
                    request,
                    cut_plan,
                    candidate_asr,
                    candidate_audio_path=candidate,
                    audio_delivery_plan_sha256=plan_digest,
                )
                atomic_write_json(paths["reverse_report"], reverse_report)
                request, ledger = apply_reverse_report_to_payloads(
                    request,
                    ledger,
                    reverse_report,
                    report_path=paths["reverse_report"],
                )
                artifacts.extend([candidate, paths["reverse_report"]])
            else:
                atomic_write_json(paths["reverse_report"], reverse_report)
                artifacts.append(paths["reverse_report"])

            base_ledger = _read_json_object(
                _compiled_paths(paths["compiled_base"])[1], "base source ledger"
            )
            _assert_source_text_fidelity(base_ledger, request, ledger)
            _assert_authoritative_starts(ledger)
            atomic_write_json(paths["processed_request"], request)
            atomic_write_json(paths["processed_items"], ledger)
            atomic_write_json(paths["audio_plan"], audio_plan)
            # Parse the exact files that the low-level writer will consume.
            load_revision_request(str(paths["processed_request"]))
            load_review_items_json(str(paths["processed_items"]))
            processed_audio = request.get("processed_audio")
            if not isinstance(processed_audio, Mapping):
                processed_audio = {}
            candidate_present = bool(candidate)
            summary = {
                "schema_version": _SCHEMA_VERSION,
                "candidate_audio_path": str(candidate or ""),
                "candidate_audio_sha256": sha256_file(candidate) if candidate else "",
                # Keep the candidate identity explicit in the phase evidence:
                # this source-time-preserving WAV is only a reverse-ASR probe,
                # never an A1/A2 delivery or replacement asset.
                "candidate_audio_purpose": (
                    REVERSE_ASR_DIAGNOSTIC_PURPOSE if candidate_present else ""
                ),
                "candidate_audio_role": (
                    REVERSE_ASR_DIAGNOSTIC_PURPOSE if candidate_present else ""
                ),
                "candidate_audio_delivery_eligible": False,
                "candidate_audio_source_aligned": bool(
                    processed_audio.get("candidate_audio_source_aligned", candidate_present)
                ),
                "candidate_audio_source_duration_seconds": processed_audio.get(
                    "candidate_audio_source_duration_seconds"
                ),
                "candidate_audio_duration_seconds": processed_audio.get(
                    "candidate_audio_duration_seconds"
                ),
                "candidate_audio_duration_matches_source": bool(
                    processed_audio.get("candidate_audio_duration_matches_source", False)
                ),
                "candidate_audio_renderer_version": (
                    CANDIDATE_RENDERER_VERSION if candidate_present else ""
                ),
                "audio_delivery_plan_sha256": plan_digest,
                "source_asr_reused": True,
                "reverse_asr_status": (
                    "pass" if not reverse_report.get("unresolved_ids") else "review"
                ),
                "unresolved_item_ids": list(cut_plan.get("unresolved_item_ids") or []),
            }
            atomic_write_json(paths["processed_summary"], summary)
            artifacts.extend(
                [
                    paths["processed_request"],
                    paths["processed_items"],
                    paths["audio_plan"],
                    paths["processed_summary"],
                ]
            )
            return _phase_outcome(
                root,
                "reverse_asr",
                artifacts=list(dict.fromkeys(artifacts)),
                data={
                    "audio_delivery_plan_sha256": plan_digest,
                    "unresolved_item_ids": cut_plan.get("unresolved_item_ids") or [],
                },
                result={
                    "revision_request": str(paths["processed_request"]),
                    "doc_items": str(paths["processed_items"]),
                    "processed_media_evidence": str(paths["processed_summary"]),
                },
                cache_hit=(all(cache_hits) if cache_hits else None),
            )

        def run_saved_draft() -> PhaseOutcome:
            nonlocal draft_path_text
            request = load_revision_request(str(paths["processed_request"]))
            doc_items = load_review_items_json(str(paths["processed_items"]))
            try:
                execution = execute_revision_request(
                    request,
                    drafts_root=str(drafts_path),
                    mock_media=mock_media,
                    strict=True,
                    doc_items=doc_items,
                    localize_materials=True,
                    runtime_integrity_receipt=runtime_integrity_receipt,
                )
            except Exception as exc:
                detail = getattr(exc, "result", None)
                if isinstance(detail, Mapping):
                    failure_details["draft_write_validate"] = dict(detail)
                raise
            if not isinstance(execution, Mapping):
                raise TypeError("Low-level revision execution must return an object")
            # This artifact is internal acceptance evidence.  Preserve stable
            # source-item IDs and verbatim marker text until both validation
            # passes have compared them.  Public phase/state projections are
            # sanitized separately by _phase_outcome and public_result.
            execution_payload = _json_compatible(execution)
            if not isinstance(execution_payload, dict):
                raise TypeError("Low-level revision execution result is not JSON-compatible")
            ledger = _read_json_object(paths["processed_items"], "processed source ledger")
            _validate_marker_receipts(execution_payload, ledger)
            draft_path_text = str(execution_payload.get("draft_path") or "")
            draft_path = Path(draft_path_text).expanduser().resolve(strict=True)
            if not draft_path.is_dir():
                raise FileNotFoundError(f"Low-level revision did not save a draft directory: {draft_path}")
            draft_digest = _draft_tree_digest(draft_path)
            atomic_write_json(paths["execution_result"], execution_payload)
            return _phase_outcome(
                root,
                "draft_write_validate",
                artifacts=[paths["execution_result"]],
                trees=[(draft_path, draft_digest)],
                data={
                    "draft_path": str(draft_path),
                    "draft_tree_sha256": draft_digest,
                    "review_marker_count": execution_payload.get("review_marker_count"),
                    "unresolved_item_ids": execution_payload.get(
                        "label_only_unresolved_item_ids"
                    )
                    or [],
                },
                result={"draft_path": str(draft_path), "draft_tree_sha256": draft_digest},
                cache_hit=False,
            )

        def run_final_acceptance() -> PhaseOutcome:
            execution = _read_json_object(paths["execution_result"], "revision result")
            ledger = _read_json_object(paths["processed_items"], "processed source ledger")
            _validate_marker_receipts(execution, ledger)
            draft_path = Path(str(execution.get("draft_path") or "")).expanduser().resolve(
                strict=True
            )
            draft_digest = _draft_tree_digest(draft_path)
            package_result = _validate_existing_package(
                package_path, draft_path, relink_tool=relink_path
            )
            package_cache_hit = package_result is not None
            if package_result is None:
                receipt_path = _package_receipt_path(package_path)
                if package_path.exists() or receipt_path.exists():
                    raise ValueError(
                        "Existing Lite ZIP or receipt is incomplete, corrupt, or belongs to another draft"
                    )
                package_lite_delivery(
                    draft_path,
                    package_path,
                    relink_tool=relink_path,
                )
                package_result = _validate_existing_package(
                    package_path, draft_path, relink_tool=relink_path
                )
                if package_result is None:
                    raise ValueError("Lite ZIP failed post-package hash, tree, or CRC validation")
            final = {
                "schema_version": _SCHEMA_VERSION,
                "status": "pass",
                "workflow_mode": "lite",
                "completion_boundary": "lite_zip_delivery",
                "draft_path": str(draft_path),
                "draft_tree_sha256": draft_digest,
                "strict_draft_validation": True,
                "marker_source_text_exact": True,
                "delivery": package_result,
                "unresolved_item_ids": execution.get("label_only_unresolved_item_ids") or [],
            }
            atomic_write_json(paths["final_result"], final)
            receipt_path = Path(str(package_result["receipt_path"])).resolve(strict=True)
            return _phase_outcome(
                root,
                "package_publish",
                artifacts=[package_path, receipt_path, paths["final_result"]],
                trees=[(draft_path, draft_digest)],
                data={
                    "draft_path": str(draft_path),
                    "archive_sha256": package_result["archive_sha256"],
                    "completion_boundary": "lite_zip_delivery",
                },
                result={
                    "draft_path": str(draft_path),
                    "package_zip": str(package_path),
                    "archive_sha256": package_result["archive_sha256"],
                },
                cache_hit=package_cache_hit,
            )

        def phase_input(name: str) -> str:
            return canonical_json_sha256(
                {
                    "phase": name,
                    "runner_version": RUNNER_VERSION,
                    "job_input_digest": input_digest,
                    "context_before": float(context_before),
                    "context_after": float(context_after),
                    "options": input_options,
                }
            )

        definitions = (
            PhaseDefinition(
                "preflight",
                run_preflight,
                item_ids=item_ids,
                input_digest=phase_input("preflight"),
                resume_check=lambda: False,
            ),
            PhaseDefinition(
                "document_fetch",
                run_document_fetch,
                depends_on=("preflight",),
                item_ids=item_ids,
                input_digest=phase_input("document_fetch"),
                resume_check=lambda: False,
            ),
            PhaseDefinition(
                "asset_download",
                run_asset_download,
                depends_on=("document_fetch",),
                item_ids=item_ids,
                input_digest=phase_input("asset_download"),
                retry_count=1,
                resume_check=lambda: False,
            ),
            PhaseDefinition(
                "input_compile",
                run_input_compile,
                depends_on=("document_fetch", "asset_download"),
                item_ids=item_ids,
                input_digest=phase_input("input_compile"),
                resume_check=lambda: False,
            ),
            PhaseDefinition(
                "source_hash",
                run_source_materials,
                depends_on=("input_compile",),
                item_ids=item_ids,
                input_digest=phase_input("source_hash"),
                resume_check=source_materials_resume,
            ),
            PhaseDefinition(
                "source_asr",
                run_source_asr_visual_index,
                depends_on=("source_hash", "input_compile"),
                item_ids=item_ids,
                input_digest=phase_input("source_asr"),
                retry_count=1,
                resume_check=source_asr_resume,
            ),
            PhaseDefinition(
                "classification",
                run_classified_plans,
                depends_on=("source_asr", "input_compile"),
                item_ids=item_ids,
                input_digest=phase_input("classification"),
                resume_check=lambda: _phase_receipt_valid(
                    store,
                    "classification",
                    root / "classification.receipt.json",
                ),
            ),
            PhaseDefinition(
                "reverse_asr",
                run_processed_media,
                depends_on=("classification",),
                item_ids=item_ids,
                input_digest=phase_input("reverse_asr"),
                retry_count=1,
                resume_check=lambda: _phase_receipt_valid(
                    store,
                    "reverse_asr",
                    root / "reverse_asr.receipt.json",
                ),
            ),
            PhaseDefinition(
                "draft_write_validate",
                run_saved_draft,
                depends_on=("reverse_asr",),
                resource="jianying_write",
                item_ids=item_ids,
                input_digest=phase_input("draft_write_validate"),
                resume_check=lambda: _phase_receipt_valid(
                    store,
                    "draft_write_validate",
                    root / "draft_write_validate.receipt.json",
                ),
            ),
            PhaseDefinition(
                "package_publish",
                run_final_acceptance,
                depends_on=("draft_write_validate",),
                item_ids=item_ids,
                input_digest=phase_input("package_publish"),
                resume_check=lambda: _phase_receipt_valid(
                    store,
                    "package_publish",
                    root / "package_publish.receipt.json",
                ),
            ),
        )
        phase_records = ReviewJobExecutor(
            max_workers=max_workers,
            state_store=store,
            progress=progress,
        ).run(definitions)
        failed = [
            name
            for name, record in phase_records.items()
            if record.get("status") not in {"complete", "resumed"}
        ]
        if failed:
            first = failed[0]
            error = safe_error_text(
                str(phase_records[first].get("error") or f"phase did not complete: {first}")
            )
            result = public_result(ok=False, error=error)
            raise ReviewDocumentRunError(error, result)
        result = public_result(ok=True)
        delivery = result.get("delivery")
        if not isinstance(delivery, Mapping) or delivery.get("status") != "pass":
            error = "Final Lite delivery receipt is missing or invalid"
            raise ReviewDocumentRunError(error, public_result(ok=False, error=error))
        return result
    except ReviewDocumentRunError:
        raise
    except Exception as exc:
        error = safe_error_text(exc)
        raise ReviewDocumentRunError(error, public_result(ok=False, error=error)) from exc


__all__ = [
    "RUNNER_VERSION",
    "LiteVisualAssetError",
    "ReviewDocumentRunError",
    "run_review_document",
]
