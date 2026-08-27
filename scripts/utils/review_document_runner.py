"""Maintained, resumable end-to-end runner for Lite review documents."""

from __future__ import annotations

import json
import os
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
from utils.review_job_compiler import compile_review_job
from utils.review_job_pipeline import (
    ArtifactCache,
    CacheIdentity,
    JobStateStore,
    PhaseDefinition,
    PhaseOutcome,
    ReviewJobExecutor,
)
from utils.revision_evidence import audio_delivery_plan_sha256
from utils.revision_runner import (
    execute_revision_request,
    load_review_items_json,
    load_revision_request,
)

from audio_sound.segment_removal import probe_media
from audio_sound.volc_asr import load_volc_asr_config

RUNNER_VERSION = "auto-cut-lite-review-document-run-v1"
_SCHEMA_VERSION = 1
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
    output_digest = canonical_json_sha256(
        {"artifacts": artifact_rows, "trees": tree_rows, "phase": phase}
    )
    receipt = {
        "schema_version": _SCHEMA_VERSION,
        "phase": phase,
        "runner_version": RUNNER_VERSION,
        "output_digest": output_digest,
        "artifacts": artifact_rows,
        "trees": tree_rows,
        "data": dict(data or {}),
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
    cached = cache.get_json(identity, _SCHEMA_VERSION)
    if cached is not None:
        return cached, True
    with cache_identity_lock(cache.root, identity.namespace, identity.digest()):
        cached = cache.get_json(identity, _SCHEMA_VERSION)
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
        cached = cache.get_json(identity, _SCHEMA_VERSION)
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


def _visual_asset_paths(item: Mapping[str, Any]) -> list[str]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    raw = evidence.get("asset_paths") or item.get("asset_paths") or item.get("assets") or []
    paths = [str(value).strip() for value in raw if str(value).strip()] if isinstance(raw, list) else []
    single = str(evidence.get("asset_path") or item.get("asset_path") or "").strip()
    if single:
        paths.append(single)
    return list(dict.fromkeys(paths))


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
        paths = _visual_asset_paths(item)
        evidence = dict(item.get("evidence") or {})
        visual_plan = (
            deepcopy(evidence.get("visual_plan"))
            if isinstance(evidence.get("visual_plan"), dict)
            else {}
        )
        if not paths and not visual_plan:
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
    "document_snapshot_ledger",
    "source_materials_hashes",
    "source_asr_visual_index",
    "classified_edit_acceptance_plans",
    "processed_local_media_evidence",
    "saved_editable_draft_marker_receipts",
    "final_acceptance",
)
_PATH_KEYS = frozenset(
    {
        "asset_path",
        "asset_paths",
        "assets",
        "attachment_path",
        "attachment_paths",
        "local_path",
        "media_path",
    }
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
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
    payload = dict(result or {})
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
    snapshot_json: str | os.PathLike[str],
    project_json: str | os.PathLike[str],
    *,
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
    snapshot_path = Path(snapshot_json).expanduser().resolve(strict=False)
    project_path = Path(project_json).expanduser().resolve(strict=False)
    draft_path_text = ""

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
            result["error"] = error
        return result

    try:
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
        snapshot_path = snapshot_path.resolve(strict=True)
        project_path = project_path.resolve(strict=True)
        snapshot = _read_json_object(snapshot_path, "document snapshot")
        raw_project = _read_json_object(project_path, "project")
        explicit_mode = str(raw_project.get("workflow_mode") or "").strip().casefold()
        if explicit_mode and explicit_mode != "lite":
            raise ValueError("Project explicitly requests a non-Lite workflow")
        explicit_layout = str(raw_project.get("lite_cut_layout") or "").strip().casefold()
        if explicit_layout and explicit_layout != "split_gap":
            raise ValueError("New Lite review-document jobs require lite_cut_layout=split_gap")
        lite_project = deepcopy(raw_project)
        lite_project["workflow_mode"] = "lite"
        lite_project["lite_cut_layout"] = "split_gap"

        root.mkdir(parents=True, exist_ok=True)
        for key in (
            "input_dir",
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

        snapshot_sha256 = sha256_file(snapshot_path)
        project_sha256 = sha256_file(project_path)
        snapshot_assets = _path_rows_from_snapshot(snapshot)
        expected_project_materials: dict[str, dict[str, str]] = {}
        for field in ("source_video", "source_audio", "replacement_audio"):
            raw_value = str(lite_project.get(field) or "").strip()
            if not raw_value:
                continue
            material_path = Path(raw_value).expanduser().resolve(strict=False)
            expected_project_materials[field] = {
                "path": os.path.normcase(str(material_path)),
                "sha256": sha256_file(material_path) if material_path.is_file() else "missing",
            }
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
            "snapshot_assets": snapshot_assets,
        }
        input_digest = _job_input_digest(
            snapshot_path,
            project_path,
            lite_project,
            options=input_options,
        )
        store = JobStateStore(state_path, input_digest, RUNNER_VERSION)
        cache_path = (
            Path(cache_root).expanduser().resolve(strict=False)
            if cache_root is not None
            else root.parent / ".auto-cut-review-cache"
        )
        cache = ArtifactCache(cache_path)
        inflight_root = cache_path / "inflight"
        item_ids = _raw_item_ids(snapshot)

        def run_document_snapshot() -> PhaseOutcome:
            if paths["snapshot"].resolve(strict=False) != snapshot_path:
                atomic_copy_file(snapshot_path, paths["snapshot"])
            if paths["project_original"].resolve(strict=False) != project_path:
                atomic_copy_file(project_path, paths["project_original"])
            if sha256_file(paths["snapshot"]) != snapshot_sha256 or sha256_file(
                paths["project_original"]
            ) != project_sha256:
                raise RuntimeError("Source JSON changed while creating the immutable job snapshot")
            atomic_write_json(paths["project_lite"], lite_project)
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
            return _phase_outcome(
                root,
                "document_snapshot_ledger",
                artifacts=[
                    paths["snapshot"],
                    paths["project_original"],
                    paths["project_lite"],
                    revision_path,
                    ledger_path,
                    manifest_path,
                ],
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
                "source_materials_hashes",
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
            phase = "source_materials_hashes"
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
                for raw_path in _visual_asset_paths(item):
                    asset = Path(raw_path).expanduser().resolve(strict=False)
                    if not asset.is_file():
                        raise FileNotFoundError(f"Visual asset for {item_id} is missing: {asset}")
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
                material_rows = materials.get("materials")
                if not isinstance(material_rows, Mapping):
                    raise ValueError("Source material ledger is missing material identities")
                alignment_row = material_rows.get("alignment_source")
                if not isinstance(alignment_row, Mapping):
                    raise ValueError("Source material ledger is missing alignment_source")
                alignment_source = Path(str(alignment_row.get("path") or "")).resolve(strict=True)
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
                            "source_asr_visual_index",
                            max(0.0, time.monotonic() - wait_started),
                        )
                cache_hits.append(source_hit)
                input_sha256 = str(source_asr.get("input_sha256") or "")
                if input_sha256 and input_sha256 != sha256_file(paths["alignment_wav"]):
                    raise ValueError("Source ASR input identity does not match alignment WAV bytes")
                atomic_write_json(paths["source_asr"], source_asr)
                source_index.update(
                    {
                        "alignment_audio_path": str(paths["alignment_wav"]),
                        "alignment_audio_sha256": sha256_file(paths["alignment_wav"]),
                        "alignment_cache_identity_digest": alignment_identity.digest(),
                        "source_asr_path": str(paths["source_asr"]),
                        "source_asr_sha256": sha256_file(paths["source_asr"]),
                        "source_asr_cache_identity_digest": source_identity.digest(),
                    }
                )
                artifacts.extend([paths["alignment_wav"], paths["source_asr"]])
            else:
                atomic_write_json(
                    paths["source_asr"],
                    {"schema_version": _SCHEMA_VERSION, "status": "not_required", "words": []},
                )
                artifacts.append(paths["source_asr"])
            atomic_write_json(paths["source_index"], source_index)
            artifacts.append(paths["source_index"])
            return _phase_outcome(
                root,
                "source_asr_visual_index",
                artifacts=list(dict.fromkeys(artifacts)),
                data={
                    "asr_required": needs_asr,
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
            phase = "source_asr_visual_index"
            receipt_path = root / f"{phase}.receipt.json"
            if not _phase_receipt_valid(store, phase, receipt_path):
                return False
            data = _receipt_data(receipt_path)
            if data is None or not data.get("asr_required"):
                return data is not None
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
            return data.get("source_asr_cache_identity_digest") == identity.digest()

        def run_classified_plans() -> PhaseOutcome:
            snapshot_payload = _read_json_object(paths["snapshot"], "job document snapshot")
            effective_project = _read_json_object(paths["effective_project"], "effective project")
            compile_review_job(snapshot_payload, effective_project, paths["classified_dir"])
            revision_path, ledger_path, manifest_path = _compiled_paths(paths["classified_dir"])
            request = _read_json_object(revision_path, "classified revision request")
            ledger = _read_json_object(ledger_path, "classified source ledger")
            source_index = _read_json_object(paths["source_index"], "source ASR index")
            materials = _read_json_object(paths["materials_ledger"], "source materials")
            if source_index.get("asr_required"):
                cut_plan = resolve_lite_audio_items(
                    ledger.get("review_items") or [],
                    _read_json_object(paths["source_asr"], "source ASR"),
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
                "classified_edit_acceptance_plans",
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
            _compile_explicit_lite_visuals(request, ledger)
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
                            "processed_local_media_evidence",
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
            summary = {
                "schema_version": _SCHEMA_VERSION,
                "candidate_audio_path": str(candidate or ""),
                "candidate_audio_sha256": sha256_file(candidate) if candidate else "",
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
                "processed_local_media_evidence",
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
                )
            except Exception as exc:
                detail = getattr(exc, "result", None)
                if isinstance(detail, Mapping):
                    failure_details["saved_editable_draft_marker_receipts"] = dict(detail)
                raise
            if not isinstance(execution, Mapping):
                raise TypeError("Low-level revision execution must return an object")
            execution_payload = _json_safe(execution)
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
                "saved_editable_draft_marker_receipts",
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
                "final_acceptance",
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
                }
            )

        definitions = (
            PhaseDefinition(
                "document_snapshot_ledger",
                run_document_snapshot,
                item_ids=item_ids,
                input_digest=phase_input("document_snapshot_ledger"),
                resume_check=lambda: _phase_receipt_valid(
                    store,
                    "document_snapshot_ledger",
                    root / "document_snapshot_ledger.receipt.json",
                ),
            ),
            PhaseDefinition(
                "source_materials_hashes",
                run_source_materials,
                depends_on=("document_snapshot_ledger",),
                item_ids=item_ids,
                input_digest=phase_input("source_materials_hashes"),
                resume_check=source_materials_resume,
            ),
            PhaseDefinition(
                "source_asr_visual_index",
                run_source_asr_visual_index,
                depends_on=("source_materials_hashes",),
                item_ids=item_ids,
                input_digest=phase_input("source_asr_visual_index"),
                retry_count=1,
                resume_check=source_asr_resume,
            ),
            PhaseDefinition(
                "classified_edit_acceptance_plans",
                run_classified_plans,
                depends_on=("source_asr_visual_index",),
                item_ids=item_ids,
                input_digest=phase_input("classified_edit_acceptance_plans"),
                resume_check=lambda: _phase_receipt_valid(
                    store,
                    "classified_edit_acceptance_plans",
                    root / "classified_edit_acceptance_plans.receipt.json",
                ),
            ),
            PhaseDefinition(
                "processed_local_media_evidence",
                run_processed_media,
                depends_on=("classified_edit_acceptance_plans",),
                item_ids=item_ids,
                input_digest=phase_input("processed_local_media_evidence"),
                retry_count=1,
                resume_check=lambda: _phase_receipt_valid(
                    store,
                    "processed_local_media_evidence",
                    root / "processed_local_media_evidence.receipt.json",
                ),
            ),
            PhaseDefinition(
                "saved_editable_draft_marker_receipts",
                run_saved_draft,
                depends_on=("processed_local_media_evidence",),
                resource="jianying_write",
                item_ids=item_ids,
                input_digest=phase_input("saved_editable_draft_marker_receipts"),
                resume_check=lambda: _phase_receipt_valid(
                    store,
                    "saved_editable_draft_marker_receipts",
                    root / "saved_editable_draft_marker_receipts.receipt.json",
                ),
            ),
            PhaseDefinition(
                "final_acceptance",
                run_final_acceptance,
                depends_on=("saved_editable_draft_marker_receipts",),
                item_ids=item_ids,
                input_digest=phase_input("final_acceptance"),
                resume_check=lambda: _phase_receipt_valid(
                    store,
                    "final_acceptance",
                    root / "final_acceptance.receipt.json",
                ),
            ),
        )
        phase_records = ReviewJobExecutor(
            max_workers=max_workers,
            state_store=store,
        ).run(definitions)
        failed = [
            name
            for name, record in phase_records.items()
            if record.get("status") not in {"complete", "resumed"}
        ]
        if failed:
            first = failed[0]
            error = str(phase_records[first].get("error") or f"phase did not complete: {first}")
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
        error = f"{type(exc).__name__}: {' '.join(str(exc).splitlines()).strip() or 'review-document-run failed'}"
        raise ReviewDocumentRunError(error, public_result(ok=False, error=error)) from exc


__all__ = [
    "RUNNER_VERSION",
    "ReviewDocumentRunError",
    "run_review_document",
]
