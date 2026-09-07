"""Maintained, resumable end-to-end runner for Lite review documents."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
import wave
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from utils.execution_input import (
    ExecutionInputError,
    load_execution_input,
    resolve_artifact_name,
)
from utils.jianying_native_delivery import capture_draft_tree_receipt
from utils.lite_package import PACKAGE_SCHEMA_VERSION, package_lite_delivery
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
    downgrade_reverse_asr_failures,
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
from utils.source_manifest import (
    LoadedSourceManifest,
    SourceManifestError,
    compile_manifest_project,
    load_source_manifest,
    materialize_manifest_sources,
)

from audio_sound.segment_removal import probe_media
from audio_sound.volc_asr import VOLC_ASR_ADAPTER_VERSION, load_volc_asr_config

RUNNER_VERSION = "auto-cut-lite-review-document-run-v9"
_SCHEMA_VERSION = 2
_ASR_CACHE_SCHEMA_VERSION = 1
_NORMALIZER_VERSION = "lite-source-video-normalizer-v1"
_EDITABLE_AUDIO_EXTRACTOR_VERSION = "lite-editable-source-audio-v1"
_VIDEO_NORMALIZE_PARAMS = {
    "video_codec": "libx264",
    "pixel_format": "yuv420p",
    "preset": "veryfast",
    "crf": 18,
    "audio_codec": "aac",
    "audio_bitrate": "192k",
    "movflags": "+faststart",
}
_EDITABLE_AUDIO_EXTRACT_PARAMS = {
    "audio_stream": "0:a:0",
    "container": "ipod",
    "primary_codec": "copy",
    "fallback_codec": "alac",
    "movflags": "+faststart",
}
_AUDIO_DELETE_KINDS = {
    "audio_delete",
    "colored_span_delete",
    "ellipsis_range_delete",
    "phrase_delete",
    "range_delete",
    "speech_delete",
    "speech_tail_cleanup",
    "spoken_delete",
    "tail_cleanup",
    "tail_particle_delete",
}


class ReviewDocumentRunError(RuntimeError):
    def __init__(self, message: str, result: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.result = dict(result)


class OrderedSourceAsrIntegrityError(ValueError):
    """Ordered source ASR evidence no longer matches its manifest timebase."""

    code = "source_pair_asr_integrity"


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


def _sanitize_manifest_failure_details(value: Any) -> Any:
    """Keep manifest terminal errors bounded and free of local/provider data."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).casefold()
            if any(token in normalized for token in ("token", "secret", "credential", "url")):
                continue
            if normalized in {
                "path",
                "video_path",
                "audio_path",
                "replacement_audio_path",
                "filename",
            }:
                continue
            result[str(key)] = _sanitize_manifest_failure_details(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_manifest_failure_details(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _trusted_manifest_terminal_context() -> tuple[dict[str, Any], str] | None:
    """Read the server-injected binding used when manifest parsing fails."""

    values = {
        "task_id": os.environ.get("CODEX_AUTOCUT_TASK_ID"),
        "run_id": os.environ.get("CODEX_AUTOCUT_RUN_ID"),
        "subject_key": os.environ.get("CODEX_AUTOCUT_SUBJECT_KEY"),
        "config_version": os.environ.get("CODEX_AUTOCUT_CONFIG_VERSION"),
        "stage_id": os.environ.get("CODEX_AUTOCUT_STAGE_ID"),
        "event_id": os.environ.get("CODEX_AUTOCUT_EVENT_ID"),
    }
    if any(value is None or not str(value).strip() for value in values.values()):
        return None
    try:
        config_version = int(str(values["config_version"]).strip())
    except (TypeError, ValueError):
        return None
    manifest_sha256 = str(
        os.environ.get("CODEX_AUTOCUT_SOURCE_MANIFEST_SHA256") or ""
    ).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        return None
    binding = {
        "task_id": str(values["task_id"]).strip(),
        "run_id": str(values["run_id"]).strip(),
        "subject_key": str(values["subject_key"]).strip(),
        "config_version": config_version,
        "stage_id": str(values["stage_id"]).strip(),
        "event_id": str(values["event_id"]).strip(),
    }
    return binding, manifest_sha256


def _validate_manifest_package_path(requested_package_path: Path, draft_name: str) -> None:
    """Require the Taskboard-owned ZIP path to use the final draft name."""

    expected = requested_package_path.with_name(f"{str(draft_name).strip()}.zip")
    if requested_package_path.resolve(strict=False) != expected.resolve(strict=False):
        raise SourceManifestError(
            "package_path_mismatch",
            "manifest package path must match the final artifact name",
        )


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
    source_pairs = project.get("source_pairs")
    if isinstance(source_pairs, list) and source_pairs:
        pair_materials: list[dict[str, Any]] = []
        for index, raw_pair in enumerate(source_pairs):
            if not isinstance(raw_pair, Mapping):
                pair_materials.append({"pair_index": index, "invalid": True})
                continue
            row: dict[str, Any] = {
                "pair_index": raw_pair.get("pair_index", index),
            }
            for field in ("video_path", "replacement_audio_path"):
                value = str(raw_pair.get(field) or "").strip()
                if not value:
                    continue
                path = Path(value).expanduser().resolve(strict=False)
                row[field] = {
                    "path": os.path.normcase(str(path)),
                    "sha256": sha256_file(path) if path.is_file() else "missing",
                }
            pair_materials.append(row)
        materials["source_pairs"] = pair_materials
    else:
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
    if not (root / "draft_content.json").is_file() or not (root / "draft_meta_info.json").is_file():
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


def _extract_editable_source_audio(
    source: Path,
    output: Path,
    *,
    ffmpeg_bin: str,
) -> None:
    """Create an audio-only source without introducing another lossy encode."""

    common = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
    ]
    attempts = (
        ("copy", "Source audio stream copy failed"),
        ("alac", "Lossless source audio extraction failed"),
    )
    failures: list[str] = []
    for codec, label in attempts:
        output.unlink(missing_ok=True)
        command = [
            *common,
            "-c:a",
            codec,
            "-movflags",
            "+faststart",
            "-f",
            "ipod",
            str(output),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            return
        detail = (completed.stderr or completed.stdout or label).strip()
        failures.append(f"{label}: {detail[-500:]}")
    raise RuntimeError("; ".join(failures))


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
        "execution_input": workspace / "inputs" / "execution-input.json",
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
        "editable_audio": workspace / "materials" / "source_audio.m4a",
        "candidate_wav": workspace / "materials" / "candidate_source_aligned.wav",
        "initial_candidate_wav": workspace / "materials" / "candidate_source_aligned_initial.wav",
        "classified_dir": workspace / "classified",
        "cut_plan": workspace / "classified" / "audio_cut_plan.json",
        "acceptance_plan": workspace / "classified" / "acceptance_plan.json",
        "processed_dir": workspace / "processed",
        "processed_request": workspace / "processed" / "revision_request.json",
        "processed_items": workspace / "processed" / "doc_items.json",
        "processed_cut_plan": workspace / "processed" / "audio_cut_plan.json",
        "audio_plan": workspace / "processed" / "audio_delivery_plan.json",
        "initial_reverse_report": workspace / "processed" / "reverse_asr_initial_report.json",
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
        evidence.get("target_time"),
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


def _name_resolution_from_project(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        project = _read_json_object(path, "persisted Lite project")
    except (OSError, TypeError, ValueError):
        return None
    final_name = str(project.get("final_name") or project.get("draft_name") or "").strip()
    if not final_name:
        return None
    return {
        "requested_name": str(project.get("requested_name") or final_name),
        "final_name": final_name,
        "source": str(project.get("name_source") or "persisted_project"),
        "sanitized": bool(project.get("name_sanitized", False)),
    }


def _name_resolution_for_actual_draft(
    resolution: Mapping[str, Any] | None,
    actual_name: str,
) -> dict[str, Any]:
    """Close naming over the directory actually written by JianYing."""

    actual = str(actual_name or "").strip()
    if not actual:
        raise ValueError("Saved Lite draft has no usable directory name")
    base = dict(resolution or {})
    expected = str(base.get("final_name") or actual).strip()
    base.setdefault("requested_name", expected)
    base.setdefault("source", "persisted_project")
    base.setdefault("sanitized", False)
    base["final_name"] = actual
    if expected != actual:
        base["pre_fallback_name"] = expected
        base["draft_fallback_applied"] = True
    else:
        base["draft_fallback_applied"] = False
    return base


def _validate_existing_package(
    package_zip: Path,
    draft_path: Path,
    *,
    relink_tool: Path,
    name_resolution: Mapping[str, Any],
    execution_input_digest: str,
) -> dict[str, Any] | None:
    receipt_path = _package_receipt_path(package_zip)
    if not package_zip.is_file() or not receipt_path.is_file():
        return None
    try:
        receipt = _read_json_object(receipt_path, "Lite package receipt")
        draft_tree = capture_draft_tree_receipt(draft_path)
        if (
            receipt.get("schema_version") != PACKAGE_SCHEMA_VERSION
            or receipt.get("status") != "pass"
            or receipt.get("workflow_mode") != "lite"
            or Path(str(receipt.get("archive_path") or "")).resolve(strict=False)
            != package_zip.resolve(strict=False)
            or str(receipt.get("archive_sha256") or "") != sha256_file(package_zip)
            or package_zip.name != f"{draft_path.name}.zip"
            or Path(str(receipt.get("source_draft_path") or "")).resolve(strict=False)
            != draft_path.resolve(strict=False)
            or str(receipt.get("source_tree_sha256") or "") != draft_tree["tree_sha256"]
            or str(receipt.get("package_root_name") or "") != draft_path.name
            or str(receipt.get("draft_name") or "") != draft_path.name
            or receipt.get("name_resolution") != dict(name_resolution)
            or str(receipt.get("execution_input_digest") or "") != execution_input_digest
            or str(receipt.get("package_layout") or "") != "draft_root_bundle_v2"
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


def _validate_marker_receipts(execution: Mapping[str, Any], ledger: Mapping[str, Any]) -> None:
    if (
        not isinstance(execution.get("validation"), Mapping)
        or execution["validation"].get("ok") is not True
    ):
        raise ValueError("Lite draft structural validation did not pass")
    if (
        not isinstance(execution.get("acceptance_validation"), Mapping)
        or execution["acceptance_validation"].get("ok") is not True
    ):
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


def _audio_execution_summary(
    cut_plan: Mapping[str, Any],
    *,
    additional_label_only_unresolved_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Separate delivery acceptance from the edits that physically executed."""

    rows = [dict(row) for row in cut_plan.get("rows") or [] if isinstance(row, Mapping)]
    actual_cuts = [
        dict(row)
        for row in cut_plan.get("executable_cuts") or []
        if isinstance(row, Mapping) and str(row.get("item_id") or "").strip()
    ]
    actual_cut_item_ids = list(dict.fromkeys(str(row["item_id"]).strip() for row in actual_cuts))
    label_only_unresolved_ids = [
        str(row.get("item_id") or "").strip()
        for row in rows
        if str(row.get("item_id") or "").strip()
        and str(row.get("execution_status") or "")
        .strip()
        .casefold()
        .startswith("label_only_unresolved")
    ]
    label_only_unresolved_ids = list(
        dict.fromkeys(
            [
                *label_only_unresolved_ids,
                *[
                    str(value).strip()
                    for value in additional_label_only_unresolved_ids
                    if str(value).strip()
                ],
            ]
        )
    )
    label_only_id_set = {value.casefold() for value in label_only_unresolved_ids}
    unexecuted_audio_deletion_ids = list(
        dict.fromkeys(
            str(row.get("item_id") or "").strip()
            for row in rows
            if str(row.get("item_id") or "").strip().casefold() in label_only_id_set
            and str(row.get("kind") or "").strip().casefold() in _AUDIO_DELETE_KINDS
        )
    )
    return {
        "status": (
            "complete_with_label_only_unresolved" if label_only_unresolved_ids else "complete"
        ),
        "acceptance_scope": "draft_structure_and_package_delivery",
        "actual_audio_cut_count": len(actual_cuts),
        "actual_audio_cut_item_count": len(actual_cut_item_ids),
        "actual_audio_cut_item_ids": actual_cut_item_ids,
        "label_only_unresolved_count": len(label_only_unresolved_ids),
        "label_only_unresolved_item_ids": label_only_unresolved_ids,
        "unexecuted_audio_deletion_item_ids": unexecuted_audio_deletion_ids,
        "all_requested_audio_deletions_executed": not unexecuted_audio_deletion_ids,
    }


def _merge_source_asr_words(
    pair_rows: Sequence[Mapping[str, Any]],
    *,
    boundary_tolerance_seconds: float = 0.001,
) -> list[dict[str, Any]]:
    """Merge pair-local ASR words into one authoritative global timebase.

    The manifest order is the only ordering signal. Every local timing row is
    checked against its pair duration before the cumulative offset is applied;
    an out-of-range provider response is a hard failure instead of a silently
    shifted cut.
    """

    if not isinstance(pair_rows, Sequence) or isinstance(pair_rows, (str, bytes)):
        raise OrderedSourceAsrIntegrityError("source ASR pair rows must be a sequence")
    merged: list[dict[str, Any]] = []
    expected_pair_index = 0
    expected_offset = 0.0
    for row_index, raw_pair in enumerate(pair_rows):
        if not isinstance(raw_pair, Mapping):
            raise OrderedSourceAsrIntegrityError(f"source ASR pair {row_index} is invalid")
        pair_index = raw_pair.get("pair_index", row_index)
        if (
            isinstance(pair_index, bool)
            or not isinstance(pair_index, int)
            or pair_index != expected_pair_index
        ):
            raise OrderedSourceAsrIntegrityError(
                "source ASR pairs must preserve contiguous manifest order"
            )
        expected_pair_index += 1
        try:
            offset = float(raw_pair.get("offset", 0.0))
            duration = float(raw_pair.get("duration"))
            tolerance = float(boundary_tolerance_seconds)
        except (TypeError, ValueError) as exc:
            raise OrderedSourceAsrIntegrityError(
                f"source ASR pair {pair_index} has invalid timing metadata"
            ) from exc
        if (
            not math.isfinite(offset)
            or offset < 0.0
            or not math.isfinite(duration)
            or duration <= 0.0
        ):
            raise OrderedSourceAsrIntegrityError(
                f"source ASR pair {pair_index} has invalid timing metadata"
            )
        if not math.isfinite(tolerance) or tolerance < 0.0:
            raise OrderedSourceAsrIntegrityError("source ASR boundary tolerance is invalid")
        if abs(offset - expected_offset) > tolerance:
            raise OrderedSourceAsrIntegrityError(
                f"source ASR pair {pair_index} offset does not preserve manifest order"
            )
        expected_offset += duration
        words = raw_pair.get("words")
        if not isinstance(words, list):
            raise OrderedSourceAsrIntegrityError(f"source ASR pair {pair_index} has no word rows")
        previous_start = -math.inf
        for word_index, raw_word in enumerate(words):
            if not isinstance(raw_word, Mapping):
                raise OrderedSourceAsrIntegrityError(
                    f"source ASR pair {pair_index} word {word_index} is invalid"
                )
            try:
                local_start = float(raw_word.get("start"))
                local_end = float(raw_word.get("end"))
            except (TypeError, ValueError) as exc:
                raise OrderedSourceAsrIntegrityError(
                    f"source ASR pair {pair_index} word {word_index} has invalid timing"
                ) from exc
            if (
                not math.isfinite(local_start)
                or not math.isfinite(local_end)
                or local_start < -tolerance
                or local_end > duration + tolerance
                or local_end < local_start
            ):
                raise OrderedSourceAsrIntegrityError(
                    f"source ASR pair {pair_index} word {word_index} timing is outside pair duration"
                )
            if local_start + tolerance < previous_start:
                raise OrderedSourceAsrIntegrityError(
                    f"source ASR pair {pair_index} word timing does not preserve provider order"
                )
            previous_start = local_start
            # Provider rounding may put an edge a fraction beyond the media;
            # clamp only within the explicit boundary tolerance.
            local_start = max(0.0, min(local_start, duration))
            local_end = max(local_start, min(local_end, duration))
            merged.append(
                {
                    **dict(raw_word),
                    "pair_index": pair_index,
                    "local_start": round(local_start, 6),
                    "local_end": round(local_end, 6),
                    "start": round(offset + local_start, 6),
                    "end": round(offset + local_end, 6),
                }
            )
    return merged


def _concat_alignment_wavs(
    sources: Sequence[str | os.PathLike[str]],
    output: str | os.PathLike[str],
) -> dict[str, Any]:
    """Concatenate fixed mono PCM16 alignment WAVs without re-encoding."""

    if not sources:
        raise ValueError("at least one alignment WAV is required")
    output_path = Path(output).expanduser().resolve(strict=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{os.getpid()}.part")
    total_frames = 0
    params = None
    try:
        with wave.open(str(temporary), "wb") as target:
            for index, raw_source in enumerate(sources):
                source = Path(raw_source).expanduser().resolve(strict=True)
                with wave.open(str(source), "rb") as stream:
                    current = stream.getparams()
                    if (
                        current.nchannels != 1
                        or current.framerate != 16000
                        or current.sampwidth != 2
                        or current.comptype != "NONE"
                    ):
                        raise ValueError(
                            f"alignment WAV {index} does not match the fixed PCM16 recipe"
                        )
                    if params is None:
                        params = current
                        target.setnchannels(current.nchannels)
                        target.setsampwidth(current.sampwidth)
                        target.setframerate(current.framerate)
                        target.setcomptype(current.comptype, current.compname)
                    elif (
                        current.nchannels,
                        current.sampwidth,
                        current.framerate,
                        current.comptype,
                    ) != (
                        params.nchannels,
                        params.sampwidth,
                        params.framerate,
                        params.comptype,
                    ):
                        raise ValueError("alignment WAV recipes do not match")
                    while True:
                        frames = stream.readframes(8192)
                        if not frames:
                            break
                        target.writeframes(frames)
                        total_frames += len(frames) // (current.nchannels * current.sampwidth)
        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "path": str(output_path),
        "sha256": sha256_file(output_path),
        "duration_seconds": round(total_frames / 16000.0, 6),
    }


def _run_ordered_source_asr(
    alignment_sources: Sequence[Mapping[str, Any]],
    *,
    materials_dir: Path,
    alignment_output: Path,
    source_asr_output: Path,
    cache: ArtifactCache,
    inflight_root: Path,
    ffmpeg_bin: str,
    ffmpeg_info: Mapping[str, Any],
    config: Any,
    asr_timeout_seconds: float,
    asr_poll_interval_seconds: float,
    asr_max_wait_seconds: float,
    store: JobStateStore,
) -> tuple[dict[str, Any], dict[str, Any], list[Path], list[bool]]:
    """Extract, recognize, and merge every ordered source pair."""

    if not alignment_sources:
        raise OrderedSourceAsrIntegrityError(
            "ordered source ASR requires at least one alignment source"
        )
    pair_payloads: list[dict[str, Any]] = []
    asr_payloads: list[dict[str, Any]] = []
    pair_receipts: list[dict[str, Any]] = []
    artifacts: list[Path] = []
    cache_hits: list[bool] = []
    alignment_paths: list[Path] = []
    for row_index, raw_source in enumerate(alignment_sources):
        if not isinstance(raw_source, Mapping):
            raise OrderedSourceAsrIntegrityError(f"source alignment pair {row_index} is invalid")
        pair_index = raw_source.get("pair_index", row_index)
        if (
            isinstance(pair_index, bool)
            or not isinstance(pair_index, int)
            or pair_index != row_index
        ):
            raise OrderedSourceAsrIntegrityError(
                "source alignment pairs must preserve contiguous manifest order"
            )
        source = Path(str(raw_source.get("path") or "")).expanduser().resolve(strict=True)
        source_sha256 = sha256_file(source)
        declared_source_sha256 = str(raw_source.get("sha256") or "").strip().casefold()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", declared_source_sha256)
            or declared_source_sha256 != source_sha256
        ):
            raise OrderedSourceAsrIntegrityError(
                f"Source alignment pair {pair_index} identity does not match source bytes"
            )
        pair_alignment = materials_dir / f"source_alignment_pair_{pair_index:03d}.wav"
        alignment_identity_payload = alignment_cache_identity(
            source_sha256=source_sha256,
            ffmpeg=ffmpeg_info,
        )
        alignment_identity = CacheIdentity(
            "source_alignment_wav_pair",
            inputs=alignment_identity_payload["inputs"],
            versions=alignment_identity_payload["versions"],
        )
        cached_alignment, alignment_hit = _cached_file(
            cache,
            alignment_identity,
            build=lambda output, source_path=source: extract_alignment_wav(
                source_path,
                output,
                ffmpeg_bin=ffmpeg_bin,
            ),
            suffix=".wav",
        )
        _copy_cached_file(cached_alignment, pair_alignment)
        alignment_paths.append(pair_alignment)
        artifacts.extend([source, pair_alignment])
        cache_hits.append(alignment_hit)

        source_identity_payload = source_asr_cache_identity(
            alignment_audio_sha256=sha256_file(pair_alignment),
            config=config,
        )
        source_identity = CacheIdentity(
            "source_asr_words_pair",
            inputs=source_identity_payload["inputs"],
            versions=source_identity_payload["versions"],
        )
        wait_started = time.monotonic()
        source_hit = False
        try:
            source_asr, source_hit = _cached_asr_json(
                cache,
                source_identity,
                audio_path=pair_alignment,
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
        alignment_sha256 = sha256_file(pair_alignment)
        if input_sha256 != alignment_sha256:
            raise OrderedSourceAsrIntegrityError(
                f"Source ASR pair {pair_index} input identity does not match alignment WAV bytes"
            )
        words = source_asr.get("words")
        if not isinstance(words, list) or not words:
            raise ValueError(f"Source ASR pair {pair_index} did not return word-level timing rows")
        try:
            offset = float(raw_source.get("offset"))
            duration = float(raw_source.get("duration"))
        except (TypeError, ValueError) as exc:
            raise OrderedSourceAsrIntegrityError(
                f"Source ASR pair {pair_index} has invalid timing metadata"
            ) from exc
        service_identity = {
            field: str(source_asr.get(field) or "")
            for field in ("provider", "resource_id", "model", "adapter_version")
        }
        if (
            not service_identity["provider"]
            or not (service_identity["resource_id"] or service_identity["model"])
            or not service_identity["adapter_version"]
        ):
            raise OrderedSourceAsrIntegrityError(
                f"Source ASR pair {pair_index} has incomplete provider identity"
            )
        if asr_payloads:
            expected_identity = {
                field: str(asr_payloads[0].get(field) or "")
                for field in ("provider", "resource_id", "model", "adapter_version")
            }
            if service_identity != expected_identity:
                raise OrderedSourceAsrIntegrityError(
                    "Source ASR provider identity changed between ordered pairs"
                )
        asr_payloads.append(dict(source_asr))
        pair_payloads.append(
            {
                "pair_index": pair_index,
                "offset": offset,
                "duration": duration,
                "words": words,
            }
        )
        pair_receipts.append(
            {
                "pair_index": pair_index,
                "offset": offset,
                "duration": duration,
                "alignment_audio_path": str(pair_alignment),
                "alignment_audio_sha256": alignment_sha256,
                "alignment_cache_identity_digest": alignment_identity.digest(),
                "source_asr_input_sha256": input_sha256 or alignment_sha256,
                "source_asr_cache_identity_digest": source_identity.digest(),
                "provider": service_identity["provider"],
                "resource_id": service_identity["resource_id"],
                "model": service_identity["model"],
                "adapter_version": service_identity["adapter_version"],
                "service_job_id": str(source_asr.get("service_job_id") or ""),
                "service_result_sha256": str(source_asr.get("service_result_sha256") or ""),
                "word_count": len(words),
                "cache_hit": bool(alignment_hit and source_hit),
            }
        )

    combined = _concat_alignment_wavs(alignment_paths, alignment_output)
    merged_words = _merge_source_asr_words(pair_payloads)
    first_asr = asr_payloads[0]
    source_asr = {
        "schema_version": _SCHEMA_VERSION,
        "input_sha256": combined["sha256"],
        "words": merged_words,
        "pair_asr": pair_receipts,
        "source_pair_count": len(pair_payloads),
        "service_job_ids": [str(payload.get("service_job_id") or "") for payload in asr_payloads],
        "service_result_sha256s": [
            str(payload.get("service_result_sha256") or "") for payload in asr_payloads
        ],
    }
    for field in ("provider", "resource_id", "model", "adapter_version"):
        if field in first_asr:
            source_asr[field] = first_asr[field]
    source_asr["service_result_sha256"] = canonical_json_sha256(
        source_asr["service_result_sha256s"]
    )
    atomic_write_json(source_asr_output, source_asr)
    artifacts.extend([alignment_output, source_asr_output])
    source_index = {
        "asr_available": True,
        "asr_status": "verified",
        "source_pair_count": len(pair_payloads),
        "alignment_audio_path": str(alignment_output),
        "alignment_audio_sha256": combined["sha256"],
        "alignment_sources": pair_receipts,
        "alignment_cache_identity_digests": [
            str(row["alignment_cache_identity_digest"]) for row in pair_receipts
        ],
        "source_asr_path": str(source_asr_output),
        "source_asr_sha256": sha256_file(source_asr_output),
        "source_asr_cache_identity_digests": [
            str(row["source_asr_cache_identity_digest"]) for row in pair_receipts
        ],
        "source_asr_cache_identity_digest": canonical_json_sha256(
            [row["source_asr_cache_identity_digest"] for row in pair_receipts]
        ),
    }
    return source_asr, source_index, list(dict.fromkeys(artifacts)), cache_hits


def run_review_document(
    snapshot_json: str | os.PathLike[str] | None = None,
    project_json: str | os.PathLike[str] | None = None,
    *,
    doc_url: str | None = None,
    source_manifest_json: str | os.PathLike[str] | None = None,
    job_root: str | os.PathLike[str],
    drafts_root: str | os.PathLike[str],
    package_zip: str | os.PathLike[str],
    relink_tool: str | os.PathLike[str] | None = None,
    execution_input_json: str | os.PathLike[str] | None = None,
    result_path: str | os.PathLike[str] | None = None,
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
    requested_package_path = Path(package_zip).expanduser().resolve(strict=False)
    package_path = requested_package_path
    intake: dict[str, Any] = {}
    source_manifest: LoadedSourceManifest | None = None
    external_name = ""
    execution_input_digest = ""
    execution_input_payload: dict[str, Any] | None = None
    if execution_input_json is not None and str(execution_input_json).strip():
        try:
            execution_input, execution_input_digest = load_execution_input(execution_input_json)
        except ExecutionInputError as exc:
            raise ValueError(str(exc)) from exc
        execution_input_payload = execution_input
        external_name = str(execution_input.get("artifact_name") or "").strip()
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

    # The result path is injected by Taskboard for a trusted run.  Resolve it
    # once and never discover it by enumerating a directory.
    result_file_path = (
        Path(result_path).expanduser().resolve(strict=False)
        if result_path is not None and str(result_path).strip()
        else None
    )
    manifest_requested = bool(str(source_manifest_json or "").strip())
    trusted_manifest_context = _trusted_manifest_terminal_context()

    def public_result(*, ok: bool, error: str = "") -> dict[str, Any]:
        state = store.snapshot() if store is not None else {}
        timing = store.timing_snapshot() if store is not None else {}
        persisted = state.get("phases") if isinstance(state.get("phases"), Mapping) else {}
        phases: dict[str, Any] = {}
        for name in _RUN_PHASES:
            run_record = dict(
                phase_records.get(name) or {"status": "pending", "result": None, "error": None}
            )
            persisted_record = dict(persisted.get(name) or {})
            persisted_status = persisted_record.pop("status", None)
            phases[name] = {
                **persisted_record,
                "persisted_status": persisted_status,
                **run_record,
            }

        processed_plan_is_current = bool(
            store is not None
            and paths["processed_cut_plan"].is_file()
            and _phase_receipt_valid(
                store,
                "reverse_asr",
                root / "reverse_asr.receipt.json",
            )
        )
        effective_cut_plan_path = (
            paths["processed_cut_plan"] if processed_plan_is_current else paths["cut_plan"]
        )
        execution: dict[str, Any] = {}
        final: dict[str, Any] = {}
        try:
            if paths["execution_result"].is_file():
                execution = _read_json_object(paths["execution_result"], "revision result")
            if paths["final_result"].is_file():
                final = _read_json_object(paths["final_result"], "final acceptance")
        except (OSError, TypeError, ValueError):
            pass
        compile_status = str((phase_records.get("input_compile") or {}).get("status") or "")
        compile_is_current = compile_status in {"complete", "resumed"}
        draft_status = str((phase_records.get("draft_write_validate") or {}).get("status") or "")
        if draft_status not in {"complete", "resumed"}:
            execution = {}
        effective_package_path = package_path
        package_status = str((phase_records.get("package_publish") or {}).get("status") or "")
        package_is_current = package_status in {"complete", "resumed"}
        if not package_is_current:
            final = {}
        delivery = final.get("delivery") if isinstance(final.get("delivery"), Mapping) else {}
        delivered_archive = str(delivery.get("archive_path") or "").strip()
        if package_is_current and delivered_archive:
            effective_package_path = Path(delivered_archive).expanduser().resolve(strict=False)
        artifact_candidates = {
            "document_snapshot": paths["snapshot"],
            "execution_input": paths["execution_input"],
            "project_lite": paths["project_lite"],
            "source_materials": paths["materials_ledger"],
            "visual_asset_index": paths["visual_index"],
            "source_asr_index": paths["source_index"],
            "source_asr": paths["source_asr"],
            "audio_cut_plan": effective_cut_plan_path,
            "revision_request": paths["processed_request"],
            "doc_items": paths["processed_items"],
            "audio_delivery_plan": paths["audio_plan"],
            "reverse_asr_report": paths["reverse_report"],
            "processed_media_evidence": paths["processed_summary"],
            "revision_result": paths["execution_result"],
            "final_acceptance": paths["final_result"],
            "package_zip": effective_package_path,
            "package_receipt": _package_receipt_path(effective_package_path),
        }
        if execution_input_payload is None or not compile_is_current:
            artifact_candidates.pop("execution_input", None)
        if not package_is_current:
            for stale_name in ("final_acceptance", "package_zip", "package_receipt"):
                artifact_candidates.pop(stale_name, None)
        artifacts = {
            name: row
            for name, candidate in artifact_candidates.items()
            if (row := _result_artifact(candidate)) is not None
        }
        if source_manifest is not None and package_is_current and effective_package_path.is_file():
            # Manifest callers consume this exact path/digest pair.  Keep the
            # legacy artifact rows for all other input modes unchanged.
            artifacts["package_zip"] = str(effective_package_path.resolve())
            artifacts["archive_sha256"] = sha256_file(effective_package_path)
        draft_path = str(execution.get("draft_path") or draft_path_text)
        unresolved: set[str] = set()
        cut_plan_payload: dict[str, Any] = {}
        for value in execution.get("label_only_unresolved_item_ids") or []:
            if str(value).strip():
                unresolved.add(str(value).strip())
        try:
            if effective_cut_plan_path.is_file():
                cut_plan_payload = _read_json_object(
                    effective_cut_plan_path,
                    "audio cut plan",
                )
                unresolved.update(
                    str(value).strip()
                    for value in cut_plan_payload.get("unresolved_item_ids") or []
                    if str(value).strip()
                )
        except (OSError, TypeError, ValueError):
            pass
        phase_timing = timing.get("phases") if isinstance(timing.get("phases"), Mapping) else {}
        active_seconds = sum(
            float(row.get("active_seconds") or 0.0) for row in phase_timing.values()
        )
        wait_seconds = sum(float(row.get("wait_seconds") or 0.0) for row in phase_timing.values())
        execution_summary = _audio_execution_summary(
            cut_plan_payload,
            additional_label_only_unresolved_ids=[
                str(value) for value in execution.get("label_only_unresolved_item_ids") or []
            ],
        )
        result = {
            "ok": ok,
            "runner_version": RUNNER_VERSION,
            "workflow_mode": "lite",
            "completion_boundary": "lite_zip_delivery",
            "acceptance_scope": "draft_structure_and_package_delivery",
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
            "package_zip": str(effective_package_path),
            "delivery": dict(delivery),
            "name_resolution": dict(
                final.get("name_resolution")
                or intake.get("name_resolution")
                or (
                    _name_resolution_from_project(paths["project_lite"])
                    if compile_is_current
                    else None
                )
                or (
                    _name_resolution_from_project(paths["project_original"])
                    if compile_is_current
                    else None
                )
                or {}
            ),
            "requested_package_zip": str(requested_package_path),
            "execution_input_digest": execution_input_digest,
            "phases": phases,
            "phase_execution": dict(phase_records),
            "unresolved_item_ids": sorted(unresolved),
            "execution_summary": execution_summary,
            "timing": {
                "active_seconds": round(active_seconds, 6),
                "external_wait_seconds": round(wait_seconds, 6),
                "application_or_user_blocking_seconds": 0.0,
            },
            "failure_details": _json_safe(failure_details),
        }
        if source_manifest is not None:
            result["source_manifest_sha256"] = source_manifest.canonical_sha256
            result["binding"] = dict(source_manifest.data["binding"])
            result["output_artifacts"] = artifacts
        if error:
            result["error"] = safe_error_text(error)
        return result

    def write_terminal_result(
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
        error_code: str | None = None,
    ) -> None:
        """Publish only the server-owned, manifest-bound terminal receipt."""

        if result_file_path is None:
            return
        if source_manifest is not None:
            binding = dict(source_manifest.data["binding"])
            manifest_sha256 = source_manifest.canonical_sha256
        elif manifest_requested and trusted_manifest_context is not None:
            binding, manifest_sha256 = trusted_manifest_context
        else:
            return
        payload: dict[str, Any] = {
            "schema_version": 1,
            "binding": binding,
            "manifest_sha256": manifest_sha256,
            "status": str(status),
        }
        if status == "pass":
            source = dict(result or {})
            delivery = source.get("delivery")
            if not isinstance(delivery, Mapping):
                delivery = {}
            name_resolution = source.get("name_resolution")
            if not isinstance(name_resolution, Mapping):
                name_resolution = {}
            package_value = str(source.get("package_zip") or "").strip()
            archive_sha = (
                str(source.get("archive_sha256") or delivery.get("archive_sha256") or "")
                .strip()
                .lower()
            )
            draft_name = str(
                source.get("draft_name") or name_resolution.get("final_name") or ""
            ).strip()
            if package_value and archive_sha:
                payload.update(
                    {
                        "package_zip": str(Path(package_value).expanduser().resolve(strict=False)),
                        "archive_sha256": archive_sha,
                        "draft_name": draft_name,
                    }
                )
        else:
            stable_code = str(error_code or "autocut_failed").strip() or "autocut_failed"
            if isinstance(error, SourceManifestError):
                stable_code = error.code
            details: Mapping[str, Any] = {}
            if isinstance(error, SourceManifestError):
                details = error.details
            elif isinstance(failure_details.get("source_manifest"), Mapping):
                details = failure_details["source_manifest"].get("details") or {}
            payload["error"] = {
                "code": stable_code,
                "message": f"Auto-Cut run blocked: {stable_code}",
                "details": _json_safe(_sanitize_manifest_failure_details(details)),
            }
        try:
            atomic_write_json(result_file_path, payload)
        except Exception:
            # A result receipt must never mask the primary Auto-Cut outcome.
            pass

    try:
        has_doc_url = bool(str(doc_url or "").strip())
        has_manifest = bool(str(source_manifest_json or "").strip())
        has_snapshot = snapshot_json is not None and bool(str(snapshot_json).strip())
        has_project = project_json is not None and bool(str(project_json).strip())
        if sum(bool(value) for value in (has_doc_url, has_manifest, has_snapshot)) > 1:
            raise ValueError(
                "doc_url, snapshot_json, and source_manifest_json are mutually exclusive"
            )
        if has_doc_url or has_manifest:
            if has_project:
                raise ValueError(
                    "doc_url/source_manifest_json is mutually exclusive with project_json"
                )
            validated_doc_url = validate_document_url(str(doc_url)) if has_doc_url else ""
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

        if has_manifest:
            try:
                source_manifest = load_source_manifest(source_manifest_json)  # type: ignore[arg-type]
            except SourceManifestError as exc:
                failure_details["source_manifest"] = _json_safe(exc.public_data())
                raise

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
                "execution_input_digest": execution_input_digest,
                "workflow_mode": "lite",
            }
        elif has_manifest:
            if source_manifest is None:
                raise RuntimeError("source manifest was not loaded")
            binding = source_manifest.data["binding"]
            job_identity = {
                "input_mode": "source_manifest",
                "source_manifest_sha256": source_manifest.canonical_sha256,
                "task_id": binding["task_id"],
                "run_id": binding["run_id"],
                "subject_key": binding["subject_key"],
                "config_version": binding["config_version"],
                "stage_id": binding["stage_id"],
                "event_id": binding["event_id"],
                "execution_input_digest": execution_input_digest,
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
                "snapshot_path_sha256": canonical_json_sha256(os.path.normcase(str(snapshot_path))),
                "project_path_sha256": canonical_json_sha256(os.path.normcase(str(project_path))),
                "execution_input_digest": execution_input_digest,
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
            "input_mode": "source_manifest" if has_manifest else ("url" if has_doc_url else "json"),
            "lite_cut_layout": "split_gap",
            "mock_media": mock_media,
            "ffmpeg_bin": ffmpeg_bin,
            "ffprobe_bin": ffprobe_bin,
            "context_before": float(context_before),
            "context_after": float(context_after),
            "package_directory": os.path.normcase(str(requested_package_path.parent)),
            "execution_input_digest": execution_input_digest,
            "drafts_root": os.path.normcase(str(drafts_path)),
            "relink_tool_sha256": sha256_file(relink_path),
        }
        if source_manifest is not None:
            input_options.update(
                {
                    "source_manifest_sha256": source_manifest.canonical_sha256,
                    "task_id": source_manifest.data["binding"]["task_id"],
                    "run_id": source_manifest.data["binding"]["run_id"],
                    "stage_id": source_manifest.data["binding"]["stage_id"],
                    "config_version": source_manifest.data["binding"]["config_version"],
                }
            )
        cache_path = (
            Path(cache_root).expanduser().resolve(strict=False)
            if cache_root is not None
            else root.parent / ".auto-cut-review-cache"
        )
        cache = ArtifactCache(cache_path)
        inflight_root = cache_path / "inflight"
        item_ids: tuple[str, ...] = ()

        def validate_manifest_package_path(draft_name: str, phase: str) -> None:
            try:
                _validate_manifest_package_path(requested_package_path, draft_name)
            except SourceManifestError as exc:
                failure_details[phase] = _json_safe(exc.public_data())
                raise

        def run_preflight() -> PhaseOutcome:
            nonlocal runtime_integrity_receipt
            if not mock_media:
                from utils.runtime_integrity import validate_current_lite_runtime

                runtime_integrity_receipt = validate_current_lite_runtime()
            else:
                runtime_integrity_receipt = None
            lark_version = ""
            if has_doc_url or has_manifest:
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
                    invalidate_lark_readiness("lark_user_identity_unavailable", path=readiness_path)
                    failure_details["preflight"] = {
                        "code": "lark_user_identity_unavailable",
                        "message": "Feishu/Lark user identity validation failed",
                        "details": {"error": safe_error_text(exc)},
                    }
                    raise RuntimeError("Feishu/Lark user identity validation failed") from exc
            return _phase_outcome(
                root,
                "preflight",
                artifacts=[],
                data={
                    "input_mode": (
                        "source_manifest" if has_manifest else ("url" if has_doc_url else "json")
                    ),
                    "runtime_integrity": _json_safe(
                        runtime_integrity_receipt or {"status": "skipped_mock_media"}
                    ),
                    "lark_adapter_version": (
                        LARK_ADAPTER_VERSION if (has_doc_url or has_manifest) else ""
                    ),
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
            elif has_manifest:
                if source_manifest is None:
                    raise RuntimeError("source manifest was not loaded")
                try:
                    materialized = materialize_manifest_sources(
                        source_manifest,
                        root,
                        lark_runner,
                        lark_cli=lark_cli,
                    )
                except SourceManifestError as exc:
                    failure_details["source_manifest"] = _json_safe(exc.public_data())
                    raise
                intake["manifest_materialized"] = materialized
                document = materialized.get("document") if isinstance(materialized, Mapping) else {}
                data = {
                    "document_identity_sha256": (
                        str(document.get("document_identity_sha256") or "")
                        if isinstance(document, Mapping)
                        else ""
                    ),
                    "revision": (
                        document.get("revision_id") if isinstance(document, Mapping) else None
                    ),
                    "content_sha256": (
                        str(document.get("content_sha256") or "")
                        if isinstance(document, Mapping)
                        else ""
                    ),
                    "source_manifest_sha256": source_manifest.canonical_sha256,
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
            elif has_manifest:
                materialized = intake.get("manifest_materialized")
                if not isinstance(materialized, Mapping):
                    raise RuntimeError("manifest source materials were not materialized")
                downloaded = [
                    dict(row)
                    for row in materialized.get("receipts") or []
                    if isinstance(row, Mapping)
                ]
                intake["downloaded_assets"] = downloaded
                rows = [
                    {
                        "asset_id": str(row.get("asset_id") or row.get("filename") or ""),
                        "sha256": str(row.get("sha256") or ""),
                        "byte_size": int(row.get("byte_size") or 0),
                        "extension": str(row.get("extension") or ""),
                        "role": "manifest_source",
                    }
                    for row in downloaded
                ]
                artifacts = [
                    Path(str(row["path"])) for row in downloaded if str(row.get("path") or "")
                ]
                cache_hit = False
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
            nonlocal snapshot, raw_project, lite_project, package_path
            nonlocal snapshot_sha256, project_sha256, snapshot_assets
            nonlocal expected_project_materials, item_ids
            if has_doc_url:
                try:
                    compiled_inputs = compile_url_inputs(
                        intake["parsed"],
                        intake["downloaded_assets"],
                        external_name=external_name or None,
                    )
                except ReviewDocumentIntakeError as exc:
                    failure_details["input_compile"] = _json_safe(exc.public_data())
                    raise
                failure_details.pop("input_compile", None)
                snapshot = sanitize_document_snapshot(compiled_inputs["snapshot"])
                raw_project = dict(compiled_inputs["project"])
                intake["name_resolution"] = dict(compiled_inputs.get("name_resolution") or {})
                atomic_write_json(paths["asset_manifest"], compiled_inputs["asset_manifest"])
            elif has_manifest:
                if source_manifest is None:
                    raise RuntimeError("source manifest was not loaded")
                materialized = intake.get("manifest_materialized")
                if not isinstance(materialized, Mapping):
                    raise RuntimeError("manifest source materials were not materialized")
                videos = [
                    dict(row)
                    for row in materialized.get("videos") or []
                    if isinstance(row, Mapping)
                ]
                audios = [
                    dict(row)
                    for row in materialized.get("audios") or []
                    if isinstance(row, Mapping)
                ]
                audio_config = source_manifest.data["sources"]["audio"]
                mode = str(audio_config.get("mode") or "video_original")
                manifest_project = compile_manifest_project(
                    videos,
                    audios,
                    mode,
                    float(audio_config.get("duration_tolerance_seconds") or 3.0),
                )
                review_items = [
                    dict(row)
                    for row in materialized.get("review_items") or []
                    if isinstance(row, Mapping)
                ]
                if not review_items:
                    raise SourceManifestError(
                        "review_source_empty",
                        "configured review source contains no meaningful text",
                    )
                if not external_name:
                    raise SourceManifestError(
                        "artifact_name_missing",
                        "manifest execution input did not provide an artifact name",
                    )
                name_resolution = resolve_artifact_name(
                    external_name=external_name,
                    fallback_name=f"AutoCutLite-{source_manifest.data['binding']['task_id'][:12]}",
                )
                validate_manifest_package_path(name_resolution.final_name, "input_compile")
                intake["name_resolution"] = name_resolution.as_dict()
                document_meta = (
                    materialized.get("document")
                    if isinstance(materialized.get("document"), Mapping)
                    else {}
                )
                snapshot = {
                    "document": {
                        "document_identity_sha256": str(
                            document_meta.get("document_identity_sha256") or ""
                        ),
                        "revision": document_meta.get("revision_id"),
                        "content_sha256": str(document_meta.get("content_sha256") or ""),
                        "title": "",
                        "extraction_schema_version": 1,
                    },
                    "review_items": review_items,
                }
                first_video = videos[0]
                first_audio = audios[0] if audios else None
                raw_project = {
                    "draft_name": name_resolution.final_name,
                    "requested_name": name_resolution.requested_name,
                    "final_name": name_resolution.final_name,
                    "name_source": name_resolution.source,
                    "name_sanitized": name_resolution.sanitized,
                    "source_video": str(first_video.get("path") or ""),
                    "source_audio": "",
                    "replacement_audio": (
                        str(first_audio.get("path") or "")
                        if mode == "replace_original" and first_audio
                        else ""
                    ),
                    "source_pairs": manifest_project["source_pairs"],
                    "audio_mode": mode,
                    "duration_tolerance_seconds": manifest_project["duration_tolerance_seconds"],
                    "project_key": source_manifest.data["binding"]["task_id"],
                    "workflow_mode": "lite",
                    "lite_cut_layout": "split_gap",
                }
                asset_manifest = {
                    "schema_version": 1,
                    "source_manifest_sha256": source_manifest.canonical_sha256,
                    "assets": [
                        {
                            "relative_path": str(row.get("filename") or ""),
                            "sha256": str(row.get("sha256") or ""),
                            "byte_size": int(row.get("byte_size") or 0),
                            "mime": str(row.get("mime") or ""),
                            "role": "source_video" if row in videos else "source_audio",
                        }
                        for row in [*videos, *audios]
                    ],
                }
                atomic_write_json(paths["asset_manifest"], asset_manifest)
                failure_details.pop("input_compile", None)
            if execution_input_payload is not None:
                atomic_write_json(paths["execution_input"], execution_input_payload)
            else:
                # An execution input is invocation-scoped.  Never let a sidecar
                # left by an older run silently rename a call that omitted the
                # formal --execution-input entrypoint.
                paths["execution_input"].unlink(missing_ok=True)
            atomic_write_json(paths["snapshot"], snapshot)
            atomic_write_json(paths["project_original"], raw_project)
            snapshot_sha256 = sha256_file(paths["snapshot"])
            project_sha256 = sha256_file(paths["project_original"])
            snapshot_assets = _path_rows_from_snapshot(snapshot)
            document = snapshot.get("document")
            document_title = ""
            if isinstance(document, Mapping):
                document_title = str(
                    document.get("title") or document.get("document_title") or ""
                ).strip()
            if not document_title:
                document_title = str(
                    snapshot.get("title") or snapshot.get("document_title") or ""
                ).strip()
            if not has_doc_url and not has_manifest:
                fallback_identity = ""
                if isinstance(document, Mapping):
                    fallback_identity = str(document.get("document_identity_sha256") or "")[:12]
                if not fallback_identity:
                    fallback_identity = canonical_json_sha256(snapshot)[:12]
                name_resolution = resolve_artifact_name(
                    external_name=external_name or None,
                    document_title=document_title or None,
                    fallback_name=f"AutoCutLite-{fallback_identity}",
                )
                raw_project.update(
                    {
                        "draft_name": name_resolution.final_name,
                        "requested_name": name_resolution.requested_name,
                        "final_name": name_resolution.final_name,
                        "name_source": name_resolution.source,
                        "name_sanitized": name_resolution.sanitized,
                    }
                )
                intake["name_resolution"] = name_resolution.as_dict()
                atomic_write_json(paths["project_original"], raw_project)
                project_sha256 = sha256_file(paths["project_original"])
            resolution = dict(intake.get("name_resolution") or {})
            final_name = str(
                resolution.get("final_name") or raw_project.get("draft_name") or ""
            ).strip()
            if final_name:
                desired_package_path = requested_package_path.with_name(f"{final_name}.zip")
                if has_manifest:
                    validate_manifest_package_path(final_name, "input_compile")
                package_path = desired_package_path
                package_path.parent.mkdir(parents=True, exist_ok=True)
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
            source_pairs_payload = lite_project.get("source_pairs")
            if isinstance(source_pairs_payload, list) and source_pairs_payload:
                expected_pairs: list[dict[str, Any]] = []
                for pair_index, raw_pair in enumerate(source_pairs_payload):
                    if not isinstance(raw_pair, Mapping):
                        raise ValueError(f"source_pairs[{pair_index}] must be an object")
                    pair_materials: dict[str, Any] = {
                        "pair_index": raw_pair.get("pair_index", pair_index),
                    }
                    for field in ("video_path", "replacement_audio_path"):
                        raw_value = str(raw_pair.get(field) or "").strip()
                        if not raw_value:
                            continue
                        material_path = Path(raw_value).expanduser().resolve(strict=False)
                        pair_materials[field] = {
                            "path": os.path.normcase(str(material_path)),
                            "sha256": (
                                sha256_file(material_path) if material_path.is_file() else "missing"
                            ),
                        }
                    expected_pairs.append(pair_materials)
                expected_project_materials["source_pairs"] = expected_pairs
                # Keep the scalar compatibility entries populated from the
                # first ordered pair for legacy phase consumers.
                first_pair = expected_pairs[0]
                if "video_path" in first_pair:
                    expected_project_materials["source_video"] = first_pair["video_path"]
                if "replacement_audio_path" in first_pair:
                    expected_project_materials["replacement_audio"] = first_pair[
                        "replacement_audio_path"
                    ]
            else:
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
            if (
                request.get("workflow_mode") != "lite"
                or request.get("lite_cut_layout") != "split_gap"
            ):
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
            if execution_input_payload is not None:
                artifacts.append(paths["execution_input"])
            if has_doc_url or has_manifest:
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
            source_pairs_payload = project.get("source_pairs")
            if isinstance(source_pairs_payload, list) and source_pairs_payload:
                # Manifest source pairs are authoritative. Resolve and hash
                # every row before any ASR or draft work; retaining only the
                # first row here would allow a later pair to drift unnoticed.
                tolerance = float(project.get("duration_tolerance_seconds", 3.0) or 3.0)
                if not math.isfinite(tolerance) or tolerance < 0.0:
                    raise ValueError("Lite source-pair duration tolerance is invalid")
                ffmpeg_info = _media_tool_identity(ffmpeg_bin, mock_media=mock_media)
                ffprobe_info = _media_tool_identity(ffprobe_bin, mock_media=mock_media)
                expected_pairs = expected_project_materials.get("source_pairs") or []
                if not isinstance(expected_pairs, list) or len(expected_pairs) != len(
                    source_pairs_payload
                ):
                    raise RuntimeError("Source pair identity snapshot is incomplete")
                declared_total = float(project.get("media_duration_seconds") or 0.0)
                pair_rows: list[dict[str, Any]] = []
                artifacts: list[Path] = []
                cursor = 0.0
                for pair_index, raw_pair in enumerate(source_pairs_payload):
                    if not isinstance(raw_pair, Mapping):
                        raise ValueError(f"Source pair {pair_index} is invalid")
                    raw_index = raw_pair.get("pair_index", pair_index)
                    if (
                        isinstance(raw_index, bool)
                        or not isinstance(raw_index, int)
                        or raw_index != pair_index
                    ):
                        raise ValueError("Source pairs must preserve contiguous manifest order")
                    video_path = (
                        Path(str(raw_pair.get("video_path") or ""))
                        .expanduser()
                        .resolve(strict=True)
                    )
                    if not video_path.is_file():
                        raise FileNotFoundError(f"Source video pair {pair_index} is missing")
                    video_sha256 = sha256_file(video_path)
                    declared_video_sha256 = (
                        str(raw_pair.get("video_sha256") or "").strip().casefold()
                    )
                    expected_video = (
                        expected_pairs[pair_index].get("video_path")
                        if isinstance(expected_pairs[pair_index], Mapping)
                        else None
                    )
                    expected_video_sha256 = (
                        str(expected_video.get("sha256") or "")
                        if isinstance(expected_video, Mapping)
                        else ""
                    )
                    if (
                        not re.fullmatch(r"[0-9a-f]{64}", declared_video_sha256)
                        or declared_video_sha256 != video_sha256
                        or expected_video_sha256 != video_sha256
                    ):
                        raise RuntimeError(
                            f"Source video pair {pair_index} changed after the job input identity was captured"
                        )
                    if mock_media:
                        fallback = float(raw_pair.get("video_duration_seconds") or 0.0)
                        if fallback <= 0.0:
                            fallback = (
                                declared_total / len(source_pairs_payload)
                                if declared_total > 0
                                else 30.0
                            )
                        duration = fallback
                        has_audio = True
                        has_video = True
                    else:
                        probe = probe_media(video_path, ffprobe_bin=ffprobe_bin)
                        duration = float(probe.duration_seconds)
                        has_audio = bool(probe.has_audio)
                        has_video = bool(probe.has_video)
                        if not has_video:
                            raise ValueError(f"Source video pair {pair_index} has no video stream")
                    if not math.isfinite(duration) or duration <= 0.0:
                        raise ValueError(f"Source video pair {pair_index} has no positive duration")
                    declared_video_duration = raw_pair.get("video_duration_seconds")
                    if declared_video_duration is not None:
                        declared_value = float(declared_video_duration)
                        if abs(duration - declared_value) > tolerance:
                            raise ValueError(
                                f"Source video pair {pair_index} duration exceeds configured tolerance"
                            )

                    source_audio_path = ""
                    source_audio_row: dict[str, Any] | None = None
                    if has_audio:
                        if mock_media:
                            # Mock media has no real stream to extract; the
                            # source video remains a deterministic placeholder.
                            source_audio_path = str(video_path)
                        else:
                            editable_identity = CacheIdentity(
                                "editable_source_audio_pair",
                                inputs={
                                    "source_sha256": video_sha256,
                                    "parameters": _EDITABLE_AUDIO_EXTRACT_PARAMS,
                                },
                                versions={
                                    "extractor": _EDITABLE_AUDIO_EXTRACTOR_VERSION,
                                    "ffmpeg": ffmpeg_info,
                                    "mock_media": mock_media,
                                },
                            )
                            cached_audio, audio_hit = _cached_file(
                                cache,
                                editable_identity,
                                build=lambda output, source=video_path: _extract_editable_source_audio(
                                    source, output, ffmpeg_bin=ffmpeg_bin
                                ),
                                suffix=".m4a",
                            )
                            target_audio = (
                                paths["materials_dir"] / f"source_pair_{pair_index:03d}.m4a"
                            )
                            _copy_cached_file(cached_audio, target_audio)
                            source_audio_path = str(target_audio)
                            artifacts.append(target_audio)
                            _ = audio_hit
                        source_audio_row = {
                            "path": source_audio_path,
                            "sha256": (
                                sha256_file(Path(source_audio_path))
                                if Path(source_audio_path).is_file()
                                else video_sha256
                            ),
                            "role": "source_audio",
                        }
                    elif (
                        str(
                            raw_pair.get("audio_mode")
                            or project.get("audio_mode")
                            or "video_original"
                        ).casefold()
                        == "video_original"
                    ):
                        raise ValueError(
                            f"Source video pair {pair_index} has no source audio stream"
                        )

                    mode = (
                        str(
                            raw_pair.get("audio_mode")
                            or project.get("audio_mode")
                            or "video_original"
                        )
                        .strip()
                        .casefold()
                    )
                    replacement_row: dict[str, Any] | None = None
                    if mode == "replace_original":
                        replacement_path = (
                            Path(str(raw_pair.get("replacement_audio_path") or ""))
                            .expanduser()
                            .resolve(strict=True)
                        )
                        if not replacement_path.is_file():
                            raise FileNotFoundError(
                                f"Replacement audio pair {pair_index} is missing"
                            )
                        replacement_sha256 = sha256_file(replacement_path)
                        declared_replacement_sha256 = (
                            str(raw_pair.get("replacement_audio_sha256") or "").strip().casefold()
                        )
                        expected_replacement = (
                            expected_pairs[pair_index].get("replacement_audio_path")
                            if isinstance(expected_pairs[pair_index], Mapping)
                            else None
                        )
                        expected_replacement_sha256 = (
                            str(expected_replacement.get("sha256") or "")
                            if isinstance(expected_replacement, Mapping)
                            else ""
                        )
                        if (
                            not re.fullmatch(r"[0-9a-f]{64}", declared_replacement_sha256)
                            or declared_replacement_sha256 != replacement_sha256
                            or expected_replacement_sha256 != replacement_sha256
                        ):
                            raise RuntimeError(
                                f"Replacement audio pair {pair_index} changed after the job input identity was captured"
                            )
                        if mock_media:
                            replacement_duration = float(
                                raw_pair.get("audio_duration_seconds") or duration
                            )
                        else:
                            replacement_probe = probe_media(
                                replacement_path, ffprobe_bin=ffprobe_bin
                            )
                            replacement_duration = float(replacement_probe.duration_seconds)
                        if not math.isfinite(replacement_duration) or replacement_duration <= 0.0:
                            raise ValueError(
                                f"Replacement audio pair {pair_index} has no positive duration"
                            )
                        declared_audio_duration = raw_pair.get("audio_duration_seconds")
                        if (
                            declared_audio_duration is not None
                            and abs(replacement_duration - float(declared_audio_duration))
                            > tolerance
                        ):
                            raise ValueError(
                                f"Replacement audio pair {pair_index} duration exceeds configured tolerance"
                            )
                        if abs(replacement_duration - duration) > tolerance:
                            raise ValueError(
                                f"Source pair {pair_index} video/audio duration exceeds configured tolerance"
                            )
                        replacement_row = {
                            "path": str(replacement_path),
                            "sha256": replacement_sha256,
                            "duration_seconds": replacement_duration,
                            "role": "replacement_audio",
                        }
                        artifacts.append(replacement_path)

                    alignment_input = (
                        str(replacement_row["path"])
                        if replacement_row is not None
                        else (source_audio_path or str(video_path))
                    )
                    alignment_sha256 = sha256_file(Path(alignment_input))
                    video_row = {
                        "path": str(video_path),
                        "sha256": video_sha256,
                        "duration_seconds": duration,
                        "role": "source_video",
                    }
                    pair_row = {
                        "pair_index": pair_index,
                        "offset": cursor,
                        "duration": duration,
                        "audio_mode": mode,
                        "source_video_original": video_row,
                        "source_video_effective": dict(video_row),
                        "source_audio_effective": source_audio_row,
                        "replacement_audio": replacement_row,
                        "alignment_source": {
                            "path": alignment_input,
                            "sha256": alignment_sha256,
                            "role": (
                                "replacement_audio"
                                if replacement_row is not None
                                else "source_audio"
                            ),
                        },
                    }
                    pair_rows.append(pair_row)
                    artifacts.append(video_path)
                    cursor += duration

                if declared_total > 0.0 and abs(declared_total - cursor) > tolerance:
                    raise ValueError(
                        "Lite project.media_duration_seconds does not match ordered source-pair duration"
                    )
                first_pair = pair_rows[0]
                first_video = first_pair["source_video_effective"]
                first_source_audio = first_pair.get("source_audio_effective") or {}
                first_replacement = first_pair.get("replacement_audio") or {}
                effective_project = deepcopy(project)
                effective_project.update(
                    {
                        "source_video": str(first_video.get("path") or ""),
                        "source_audio": str(first_source_audio.get("path") or ""),
                        "replacement_audio": str(first_replacement.get("path") or ""),
                        "media_duration_seconds": cursor,
                    }
                )
                for raw_pair, pair_row in zip(effective_project["source_pairs"], pair_rows):
                    raw_pair["video_duration_seconds"] = pair_row["duration"]
                    source_audio = pair_row.get("source_audio_effective")
                    if source_audio:
                        raw_pair["source_audio_path"] = source_audio["path"]
                        raw_pair["source_audio_sha256"] = source_audio["sha256"]
                    if pair_row.get("replacement_audio"):
                        raw_pair["audio_duration_seconds"] = pair_row["replacement_audio"][
                            "duration_seconds"
                        ]
                alignment_sources = [
                    {
                        "pair_index": row["pair_index"],
                        "offset": row["offset"],
                        "duration": row["duration"],
                        **dict(row["alignment_source"]),
                    }
                    for row in pair_rows
                ]
                materials = {
                    "schema_version": _SCHEMA_VERSION,
                    "source_duration_seconds": round(cursor, 6),
                    "has_audio": all(bool(row.get("source_audio_effective")) for row in pair_rows),
                    "has_video": True,
                    "normalized_webm": False,
                    "normalization_identity_digest": "",
                    "editable_audio_identity_digest": "",
                    "ffmpeg_identity": ffmpeg_info,
                    "ffprobe_identity": ffprobe_info,
                    "source_pair_count": len(pair_rows),
                    "source_pairs": pair_rows,
                    "alignment_sources": alignment_sources,
                    "materials": {
                        "source_video_original": dict(first_pair["source_video_original"]),
                        "source_video_effective": dict(first_pair["source_video_effective"]),
                        "source_audio_effective": dict(first_source_audio),
                        "replacement_audio": dict(first_replacement),
                        "alignment_source": dict(alignment_sources[0]),
                    },
                }
                atomic_write_json(paths["materials_ledger"], materials)
                atomic_write_json(paths["effective_project"], effective_project)
                artifacts.extend([paths["materials_ledger"], paths["effective_project"]])
                return _phase_outcome(
                    root,
                    "source_hash",
                    artifacts=list(dict.fromkeys(artifacts)),
                    data={
                        "source_pair_count": len(pair_rows),
                        "source_duration_seconds": round(cursor, 6),
                        "ffmpeg_identity": ffmpeg_info,
                        "ffprobe_identity": ffprobe_info,
                        "alignment_source_count": len(alignment_sources),
                    },
                    result={
                        "source_materials": str(paths["materials_ledger"]),
                        "effective_project": str(paths["effective_project"]),
                    },
                    cache_hit=False,
                )
            source_video = (
                Path(str(project.get("source_video") or "")).expanduser().resolve(strict=True)
            )
            if not source_video.is_file():
                raise FileNotFoundError(f"Source video is missing: {source_video}")
            expected_source = expected_project_materials.get("source_video") or {}
            if os.path.normcase(str(source_video)) != expected_source.get("path") or sha256_file(
                source_video
            ) != expected_source.get("sha256"):
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
                if os.path.normcase(str(candidate)) != expected.get("path") or sha256_file(
                    candidate
                ) != expected.get("sha256"):
                    raise RuntimeError(f"{field} changed after the job input identity was captured")
                optional_materials[field] = candidate

            ffmpeg_info = _media_tool_identity(ffmpeg_bin, mock_media=mock_media)
            ffprobe_info = _media_tool_identity(ffprobe_bin, mock_media=mock_media)
            normalized = source_video.suffix.casefold() == ".webm"
            normalization_hit: bool | None = None
            normalization_identity_digest = ""
            editable_audio_hit: bool | None = None
            editable_audio_identity_digest = ""
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

            explicit_source_audio = optional_materials.get("source_audio")
            if explicit_source_audio is not None:
                effective_audio = explicit_source_audio
            else:
                editable_audio_identity = CacheIdentity(
                    "editable_source_audio",
                    inputs={
                        "source_sha256": sha256_file(effective_video),
                        "parameters": _EDITABLE_AUDIO_EXTRACT_PARAMS,
                    },
                    versions={
                        "extractor": _EDITABLE_AUDIO_EXTRACTOR_VERSION,
                        "ffmpeg": ffmpeg_info,
                        "mock_media": mock_media,
                    },
                )
                editable_audio_identity_digest = editable_audio_identity.digest()

                def extract_editable_audio(output: Path) -> None:
                    if mock_media:
                        atomic_copy_file(effective_video, output)
                    else:
                        _extract_editable_source_audio(
                            effective_video,
                            output,
                            ffmpeg_bin=ffmpeg_bin,
                        )

                cached_audio, editable_audio_hit = _cached_file(
                    cache,
                    editable_audio_identity,
                    build=extract_editable_audio,
                    suffix=".m4a",
                )
                effective_audio = paths["editable_audio"]
                _copy_cached_file(cached_audio, effective_audio)

            effective_project = deepcopy(project)
            effective_project["source_video"] = str(effective_video)
            effective_project["source_audio"] = str(effective_audio)
            effective_project["replacement_audio"] = str(
                optional_materials.get("replacement_audio") or ""
            )
            effective_project["media_duration_seconds"] = duration
            alignment_source = effective_audio
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
                "source_audio_effective": {
                    "path": str(effective_audio),
                    "sha256": sha256_file(effective_audio),
                    "extraction_identity_digest": editable_audio_identity_digest,
                    "extraction_policy": (
                        "provided_audio_file"
                        if explicit_source_audio is not None
                        else "stream_copy_preferred_lossless_alac_fallback"
                    ),
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
                "editable_audio_identity_digest": editable_audio_identity_digest,
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
                effective_audio,
                *optional_materials.values(),
            ]
            return _phase_outcome(
                root,
                "source_hash",
                artifacts=list(dict.fromkeys(artifacts)),
                data={
                    "normalized_webm": normalized,
                    "normalization_identity_digest": normalization_identity_digest,
                    "editable_audio_identity_digest": editable_audio_identity_digest,
                    "ffmpeg_identity": ffmpeg_info,
                    "ffprobe_identity": ffprobe_info,
                },
                result={
                    "source_materials": str(paths["materials_ledger"]),
                    "effective_project": str(paths["effective_project"]),
                },
                cache_hit=(
                    all(
                        value
                        for value in (normalization_hit, editable_audio_hit)
                        if value is not None
                    )
                    if any(value is not None for value in (normalization_hit, editable_audio_hit))
                    else None
                ),
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
                    ffmpeg_info = materials.get("ffmpeg_identity")
                    if not isinstance(ffmpeg_info, Mapping):
                        raise ValueError("Source material ledger is missing FFmpeg identity")
                    config = load_volc_asr_config()
                    alignment_sources = materials.get("alignment_sources")
                    if isinstance(alignment_sources, list) and alignment_sources:
                        (
                            source_asr,
                            ordered_source_index,
                            ordered_artifacts,
                            ordered_cache_hits,
                        ) = _run_ordered_source_asr(
                            alignment_sources,
                            materials_dir=paths["materials_dir"],
                            alignment_output=paths["alignment_wav"],
                            source_asr_output=paths["source_asr"],
                            cache=cache,
                            inflight_root=inflight_root,
                            ffmpeg_bin=ffmpeg_bin,
                            ffmpeg_info=ffmpeg_info,
                            config=config,
                            asr_timeout_seconds=asr_timeout_seconds,
                            asr_poll_interval_seconds=asr_poll_interval_seconds,
                            asr_max_wait_seconds=asr_max_wait_seconds,
                            store=store,
                        )
                        source_index.update(ordered_source_index)
                        artifacts.extend(ordered_artifacts)
                        cache_hits.extend(ordered_cache_hits)
                    else:
                        material_rows = materials.get("materials")
                        if not isinstance(material_rows, Mapping):
                            raise ValueError(
                                "Source material ledger is missing material identities"
                            )
                        alignment_row = material_rows.get("alignment_source")
                        if not isinstance(alignment_row, Mapping):
                            raise ValueError("Source material ledger is missing alignment_source")
                        alignment_source = Path(str(alignment_row.get("path") or "")).resolve(
                            strict=True
                        )
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
                        source_identity_payload = source_asr_cache_identity(
                            alignment_audio_sha256=sha256_file(paths["alignment_wav"]),
                            config=config,
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
                            raise ValueError(
                                "Source ASR did not return real word-level timing rows"
                            )
                        atomic_write_json(paths["source_asr"], source_asr)
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
                    if not mock_media:
                        mark_asr_verified(
                            provider=str(source_asr.get("provider") or ""),
                            model_or_resource=str(
                                source_asr.get("model") or source_asr.get("resource_id") or ""
                            ),
                            adapter_version=str(source_asr.get("adapter_version") or ""),
                            path=readiness_path,
                        )
                    failure_details.pop("source_asr", None)
                    artifacts.extend([paths["alignment_wav"], paths["source_asr"]])
                except OrderedSourceAsrIntegrityError as exc:
                    failure_details["source_asr"] = {
                        "code": exc.code,
                        "message": "Ordered source ASR evidence failed integrity validation",
                        "details": {"error": safe_error_text(exc)},
                    }
                    raise
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
                    "alignment_cache_identity_digests": source_index.get(
                        "alignment_cache_identity_digests", []
                    ),
                    "source_asr_cache_identity_digests": source_index.get(
                        "source_asr_cache_identity_digests", []
                    ),
                    "source_pair_count": source_index.get("source_pair_count", 0),
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
            materials = _read_json_object(paths["materials_ledger"], "source materials")
            alignment_sources = materials.get("alignment_sources")
            if isinstance(alignment_sources, list) and alignment_sources:
                source_index = _read_json_object(paths["source_index"], "source ASR index")
                pair_receipts = source_index.get("alignment_sources")
                expected_alignment_digests = data.get("alignment_cache_identity_digests")
                expected_source_digests = data.get("source_asr_cache_identity_digests")
                if (
                    not isinstance(pair_receipts, list)
                    or len(pair_receipts) != len(alignment_sources)
                    or not isinstance(expected_alignment_digests, list)
                    or not isinstance(expected_source_digests, list)
                    or len(expected_alignment_digests) != len(alignment_sources)
                    or len(expected_source_digests) != len(alignment_sources)
                    or data.get("source_pair_count") != len(alignment_sources)
                ):
                    return False
                ffmpeg_info = materials.get("ffmpeg_identity")
                if not isinstance(ffmpeg_info, Mapping):
                    return False
                actual_alignment_digests: list[str] = []
                actual_source_digests: list[str] = []
                try:
                    for pair_index, (raw_source, raw_receipt) in enumerate(
                        zip(alignment_sources, pair_receipts)
                    ):
                        if not isinstance(raw_source, Mapping) or not isinstance(
                            raw_receipt, Mapping
                        ):
                            return False
                        if (
                            raw_source.get("pair_index") != pair_index
                            or raw_receipt.get("pair_index") != pair_index
                        ):
                            return False
                        source = Path(str(raw_source.get("path") or "")).resolve(strict=True)
                        source_sha256 = sha256_file(source)
                        if source_sha256 != str(raw_source.get("sha256") or "").casefold():
                            return False
                        alignment_identity_payload = alignment_cache_identity(
                            source_sha256=source_sha256,
                            ffmpeg=ffmpeg_info,
                        )
                        alignment_identity = CacheIdentity(
                            "source_alignment_wav_pair",
                            inputs=alignment_identity_payload["inputs"],
                            versions=alignment_identity_payload["versions"],
                        )
                        alignment_digest = alignment_identity.digest()
                        pair_alignment = Path(
                            str(raw_receipt.get("alignment_audio_path") or "")
                        ).resolve(strict=True)
                        alignment_sha256 = sha256_file(pair_alignment)
                        if (
                            alignment_sha256
                            != str(raw_receipt.get("alignment_audio_sha256") or "").casefold()
                        ):
                            return False
                        source_identity_payload = source_asr_cache_identity(
                            alignment_audio_sha256=alignment_sha256,
                            config=config,
                        )
                        source_identity = CacheIdentity(
                            "source_asr_words_pair",
                            inputs=source_identity_payload["inputs"],
                            versions=source_identity_payload["versions"],
                        )
                        source_digest = source_identity.digest()
                        if alignment_digest != str(
                            raw_receipt.get("alignment_cache_identity_digest") or ""
                        ) or source_digest != str(
                            raw_receipt.get("source_asr_cache_identity_digest") or ""
                        ):
                            return False
                        actual_alignment_digests.append(alignment_digest)
                        actual_source_digests.append(source_digest)
                except (OSError, TypeError, ValueError):
                    return False
                valid = (
                    actual_alignment_digests == expected_alignment_digests
                    and actual_source_digests == expected_source_digests
                    and actual_alignment_digests
                    == source_index.get("alignment_cache_identity_digests")
                    and actual_source_digests
                    == source_index.get("source_asr_cache_identity_digests")
                    and data.get("source_asr_cache_identity_digest")
                    == canonical_json_sha256(actual_source_digests)
                )
            else:
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
            audio_rows = [row for row in cut_plan.get("rows") or [] if isinstance(row, Mapping)]
            source_audio: Path | None = None
            ordered_pair_mode = False
            if audio_rows:
                materials = _read_json_object(paths["materials_ledger"], "source materials")
                source_pairs = materials.get("source_pairs")
                ordered_pair_mode = isinstance(source_pairs, list) and bool(source_pairs)
                if ordered_pair_mode:
                    source_audio = paths["alignment_wav"].resolve(strict=True)
                else:
                    material_rows = materials.get("materials")
                    if not isinstance(material_rows, Mapping):
                        raise ValueError("Source material ledger is missing material identities")
                    source_audio_row = material_rows.get("source_audio_effective")
                    if not isinstance(source_audio_row, Mapping):
                        raise ValueError("Source material ledger is missing editable source audio")
                    source_audio = Path(str(source_audio_row.get("path") or "")).resolve(
                        strict=True
                    )
            audio_item_ids = {str(row.get("item_id") or "").casefold() for row in audio_rows}
            cache_hits: list[bool] = []
            artifacts: list[Path] = []
            candidate: Path | None = None
            audio_plan: dict[str, Any] = {"mode": "legacy"}
            plan_digest = ""
            reverse_attempt_count = 0
            downgraded_item_ids: list[str] = []
            reverse_report: dict[str, Any] = {
                "schema_version": _SCHEMA_VERSION,
                "status": "not_required",
                "unresolved_ids": [],
                "rows": [],
            }
            while True:
                request = deepcopy(before_request)
                ledger = deepcopy(before_ledger)
                candidate = None
                mapping_audio_plan: dict[str, Any] = {"mode": "legacy"}
                executable_cuts = list(cut_plan.get("executable_cuts") or [])
                if executable_cuts:
                    if not paths["alignment_wav"].is_file():
                        raise FileNotFoundError(
                            "Executable audio cuts require the cached alignment WAV"
                        )
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
                    mapping_audio_plan = build_lite_split_gap_audio_plan(
                        cut_plan,
                        source_audio_path=source_audio,
                        candidate_audio_path=candidate,
                    )
                    audio_plan = (
                        {"mode": "legacy"} if ordered_pair_mode else deepcopy(mapping_audio_plan)
                    )
                else:
                    audio_plan = {"mode": "legacy"}

                if audio_rows:
                    request, ledger = apply_audio_plan_to_compiled_payloads(
                        request,
                        ledger,
                        cut_plan,
                        audio_delivery_plan=mapping_audio_plan,
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
                atomic_write_json(paths["processed_request"], request)
                mapping_plan_digest = audio_delivery_plan_sha256(
                    load_revision_request(str(paths["processed_request"]))
                )

                if candidate is None:
                    reverse_report = {
                        "schema_version": _SCHEMA_VERSION,
                        "status": "not_required",
                        "attempt_count": reverse_attempt_count,
                        "downgraded_item_ids": list(downgraded_item_ids),
                        "unresolved_ids": [],
                        "rows": [],
                    }
                    atomic_write_json(paths["reverse_report"], reverse_report)
                    artifacts.append(paths["reverse_report"])
                    break

                reverse_attempt_count += 1
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
                    audio_delivery_plan_sha256=mapping_plan_digest,
                )
                unresolved_ids = [
                    str(value).strip()
                    for value in reverse_report.get("unresolved_ids") or []
                    if str(value).strip()
                ]
                reverse_report["status"] = "review" if unresolved_ids else "pass"
                reverse_report["attempt_count"] = reverse_attempt_count
                reverse_report["downgraded_item_ids"] = list(downgraded_item_ids)
                if unresolved_ids and reverse_attempt_count == 1:
                    atomic_copy_file(candidate, paths["initial_candidate_wav"])
                    initial_report = deepcopy(reverse_report)
                    initial_report["candidate_audio_path"] = str(paths["initial_candidate_wav"])
                    initial_report["fallback_action"] = (
                        "downgrade_attributable_failures_and_revalidate"
                    )
                    atomic_write_json(paths["initial_reverse_report"], initial_report)
                    artifacts.extend(
                        [paths["initial_candidate_wav"], paths["initial_reverse_report"]]
                    )
                    cut_plan = downgrade_reverse_asr_failures(cut_plan, unresolved_ids)
                    downgraded_item_ids = list(
                        (cut_plan.get("reverse_asr_fallback") or {}).get("downgraded_item_ids")
                        or []
                    )
                    continue

                reverse_report["downgraded_item_ids"] = list(downgraded_item_ids)
                atomic_write_json(paths["reverse_report"], reverse_report)
                request, ledger = apply_reverse_report_to_payloads(
                    request,
                    ledger,
                    reverse_report,
                    report_path=paths["reverse_report"],
                )
                artifacts.extend([candidate, paths["reverse_report"]])
                break

            if ordered_pair_mode:
                request["audio_delivery_plan"] = deepcopy(audio_plan)
                request_project = request.setdefault("project", {})
                before_project = before_request.get("project")
                if not isinstance(before_project, Mapping):
                    raise ValueError("Classified revision request is missing project data")
                for field in ("source_audio", "replacement_audio"):
                    if field in before_project:
                        request_project[field] = deepcopy(before_project[field])
                    else:
                        request_project.pop(field, None)
                before_preserve = before_request.get("preserve")
                request_preserve = request.get("preserve")
                if isinstance(before_preserve, Mapping) and isinstance(request_preserve, dict):
                    if "replacement_audio_material" in before_preserve:
                        request_preserve["replacement_audio_material"] = deepcopy(
                            before_preserve["replacement_audio_material"]
                        )
                    else:
                        request_preserve.pop("replacement_audio_material", None)

            try:
                _compile_explicit_lite_visuals(request, ledger)
            except LiteVisualAssetError as exc:
                failure_details["reverse_asr"] = exc.public_data()
                raise
            if request.get("pause_adjustments"):
                raise ValueError("Lite review-document runner refuses executable pause adjustments")

            base_ledger = _read_json_object(
                _compiled_paths(paths["compiled_base"])[1], "base source ledger"
            )
            _assert_source_text_fidelity(base_ledger, request, ledger)
            _assert_authoritative_starts(ledger)
            atomic_write_json(paths["processed_request"], request)
            plan_digest = audio_delivery_plan_sha256(
                load_revision_request(str(paths["processed_request"]))
            )
            atomic_write_json(paths["processed_items"], ledger)
            atomic_write_json(paths["processed_cut_plan"], cut_plan)
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
                "reverse_asr_status": str(reverse_report.get("status") or "not_required"),
                "reverse_asr_attempt_count": reverse_attempt_count,
                "reverse_asr_downgraded_item_ids": list(downgraded_item_ids),
                "unresolved_item_ids": list(cut_plan.get("unresolved_item_ids") or []),
                "execution_summary": _audio_execution_summary(cut_plan),
            }
            atomic_write_json(paths["processed_summary"], summary)
            artifacts.extend(
                [
                    paths["processed_request"],
                    paths["processed_items"],
                    paths["processed_cut_plan"],
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
                    "audio_cut_plan": str(paths["processed_cut_plan"]),
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
                raise FileNotFoundError(
                    f"Low-level revision did not save a draft directory: {draft_path}"
                )
            execution_draft_name = str(execution_payload.get("draft_name") or "").strip()
            if execution_draft_name != draft_path.name:
                raise ValueError(
                    "Low-level revision draft_name does not match the saved draft directory: "
                    f"result={execution_draft_name!r} directory={draft_path.name!r}"
                )
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
                    "unresolved_item_ids": execution_payload.get("label_only_unresolved_item_ids")
                    or [],
                },
                result={"draft_path": str(draft_path), "draft_tree_sha256": draft_digest},
                cache_hit=False,
            )

        def run_final_acceptance() -> PhaseOutcome:
            nonlocal package_path
            execution = _read_json_object(paths["execution_result"], "revision result")
            ledger = _read_json_object(paths["processed_items"], "processed source ledger")
            _validate_marker_receipts(execution, ledger)
            draft_path = (
                Path(str(execution.get("draft_path") or "")).expanduser().resolve(strict=True)
            )
            execution_draft_name = str(execution.get("draft_name") or "").strip()
            if execution_draft_name != draft_path.name:
                raise ValueError(
                    "Saved revision draft_name does not match its directory: "
                    f"result={execution_draft_name!r} directory={draft_path.name!r}"
                )
            if source_manifest is not None:
                validate_manifest_package_path(draft_path.name, "package_publish")
            package_path = requested_package_path.with_name(f"{draft_path.name}.zip")
            name_resolution = _name_resolution_for_actual_draft(
                intake.get("name_resolution")
                or _name_resolution_from_project(paths["project_lite"]),
                draft_path.name,
            )
            draft_digest = _draft_tree_digest(draft_path)
            package_result = _validate_existing_package(
                package_path,
                draft_path,
                relink_tool=relink_path,
                name_resolution=name_resolution,
                execution_input_digest=execution_input_digest,
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
                    package_root_name=draft_path.name,
                    name_resolution=name_resolution,
                    execution_input_digest=execution_input_digest,
                )
                package_result = _validate_existing_package(
                    package_path,
                    draft_path,
                    relink_tool=relink_path,
                    name_resolution=name_resolution,
                    execution_input_digest=execution_input_digest,
                )
                if package_result is None:
                    raise ValueError("Lite ZIP failed post-package hash, tree, or CRC validation")
            if source_manifest is not None:
                # Extend the package receipt with the immutable Taskboard
                # binding after the existing ZIP/tree validation succeeds.
                receipt_path = Path(str(package_result["receipt_path"])).resolve(strict=True)
                receipt_payload = _read_json_object(receipt_path, "Lite package receipt")
                receipt_payload.update(
                    {
                        "source_manifest_sha256": source_manifest.canonical_sha256,
                        "binding": dict(source_manifest.data["binding"]),
                        "source_pairs": list(
                            _read_json_object(paths["project_lite"], "Lite project").get(
                                "source_pairs"
                            )
                            or []
                        ),
                        "package_zip": str(package_path.resolve()),
                        "archive_sha256": str(package_result["archive_sha256"]),
                    }
                )
                atomic_write_json(receipt_path, receipt_payload)
                package_result = dict(package_result)
                package_result.update(
                    {
                        "source_manifest_sha256": source_manifest.canonical_sha256,
                        "binding": dict(source_manifest.data["binding"]),
                        "package_zip": str(package_path.resolve()),
                    }
                )
            final = {
                "schema_version": _SCHEMA_VERSION,
                "status": "pass",
                "workflow_mode": "lite",
                "completion_boundary": "lite_zip_delivery",
                "acceptance_scope": "draft_structure_and_package_delivery",
                "draft_path": str(draft_path),
                "draft_tree_sha256": draft_digest,
                "strict_draft_validation": True,
                "marker_source_text_exact": True,
                "name_resolution": name_resolution,
                "delivery": package_result,
                "unresolved_item_ids": execution.get("label_only_unresolved_item_ids") or [],
                "execution_summary": _audio_execution_summary(
                    _read_json_object(
                        paths["processed_cut_plan"],
                        "processed audio cut plan",
                    ),
                    additional_label_only_unresolved_ids=[
                        str(value)
                        for value in execution.get("label_only_unresolved_item_ids") or []
                    ],
                ),
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
                    "name_resolution": name_resolution,
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
            if manifest_requested:
                failed_code = None
                detail = failure_details.get(first)
                if isinstance(detail, Mapping):
                    failed_code = str(detail.get("code") or "").strip() or None
                write_terminal_result(
                    status="blocked",
                    result=result,
                    error_code=failed_code,
                )
            raise ReviewDocumentRunError(error, result)
        result = public_result(ok=True)
        delivery = result.get("delivery")
        if not isinstance(delivery, Mapping) or delivery.get("status") != "pass":
            error = "Final Lite delivery receipt is missing or invalid"
            if manifest_requested:
                write_terminal_result(status="blocked", result=result, error_code="package_invalid")
            raise ReviewDocumentRunError(error, public_result(ok=False, error=error))
        if manifest_requested:
            write_terminal_result(status="pass", result=result)
        return result
    except ReviewDocumentRunError as exc:
        if manifest_requested:
            # The failed-phase branch normally writes before raising; this is
            # also the recovery path for a malformed final delivery result.
            if result_file_path is not None:
                existing = None
                try:
                    existing = _read_json_object(result_file_path, "taskboard result")
                except Exception:
                    existing = None
                if not isinstance(existing, Mapping) or existing.get("status") != "blocked":
                    write_terminal_result(status="blocked", result=exc.result, error=exc)
        raise
    except Exception as exc:
        error = safe_error_text(exc)
        result = public_result(ok=False, error=error)
        if manifest_requested:
            code = getattr(exc, "code", None)
            write_terminal_result(status="blocked", result=result, error=exc, error_code=code)
        raise ReviewDocumentRunError(error, result) from exc


__all__ = [
    "RUNNER_VERSION",
    "LiteVisualAssetError",
    "ReviewDocumentRunError",
    "run_review_document",
]
