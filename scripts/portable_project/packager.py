from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

from portable.build_importer import validate_build_receipt

from .draft_json import (
    DraftDocumentSet,
    discover_material_references,
    discover_non_file_dependencies,
    rewrite_draft_documents,
)
from .draft_policy import is_sensitive_draft_artifact
from .errors import PortableProjectError
from .hashing import sha256_file
from .manifest import (
    DRAFT_DIR_MARKER,
    DRAFT_ROOT_MARKER,
    IMPORTER_FILENAME,
    INSTRUCTIONS_FILENAME,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    REPORT_FILENAME,
    logical_media_path,
    material_relative_path,
    package_file_row,
    seal_manifest,
    validate_material_id,
)
from .promotion import withdraw_promoted_directory
from .provenance import source_provenance
from .validator import (
    draft_structure_receipt,
    normalize_structure_policy,
    timeline_structure_policy,
    validate_package,
)

TRANSIENT_DRAFT_POLICY_VERSION = 1
_TRANSIENT_DRAFT_DIRECTORIES = frozenset({".backup"})
_TRANSIENT_DRAFT_SUFFIXES = (".bak", ".tmp", ".backup")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _reparse_component(path: Path) -> Path | None:
    absolute = Path(path).absolute()
    parts = absolute.parts
    if not parts:
        return None
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            current.lstat()
        except OSError:
            return None
        if _is_reparse(current):
            return current
    return None


def _validate_regular_source(
    path: Path,
    *,
    missing_code: str,
    nonregular_code: str,
    reparse_code: str,
    safe_data: Mapping[str, Any] | None = None,
) -> None:
    data = dict(safe_data or {})
    if _reparse_component(path) is not None:
        raise PortableProjectError(reparse_code, "Source path contains a reparse point", data)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PortableProjectError(missing_code, "Required source file is missing", data) from exc
    if _is_reparse(path):
        raise PortableProjectError(reparse_code, "Source file is a reparse point", data)
    if not stat.S_ISREG(metadata.st_mode):
        raise PortableProjectError(nonregular_code, "Source path is not a regular file", data)


def _copy_regular_file_checked(source: Path, target: Path, *, safe_label: str) -> dict[str, Any]:
    if _reparse_component(source) is not None:
        raise PortableProjectError(
            "source_changed_during_copy",
            "A source path became a reparse point during package creation",
            {"file": safe_label},
        )
    before = source.stat()
    before_hash = sha256_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if _reparse_component(source) is not None:
        raise PortableProjectError(
            "source_changed_during_copy",
            "A source path became a reparse point while it was copied",
            {"file": safe_label},
        )
    after = source.stat()
    after_hash = sha256_file(source)
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before_hash != after_hash
    ):
        raise PortableProjectError(
            "source_changed_during_copy",
            "A source file changed while the portable project was being copied",
            {"file": safe_label},
        )
    target_hash = sha256_file(target)
    if target.stat().st_size != before.st_size or target_hash != before_hash:
        raise PortableProjectError(
            "source_changed_during_copy",
            "Copied bytes do not match the stable source file",
            {"file": safe_label},
        )
    return {"byte_size": before.st_size, "sha256": before_hash}


def _is_transient_draft_entry(name: str, *, is_directory: bool) -> bool:
    normalized = str(name).casefold()
    if is_directory and normalized in _TRANSIENT_DRAFT_DIRECTORIES:
        return True
    return not is_directory and normalized.endswith(_TRANSIENT_DRAFT_SUFFIXES)


def _stable_source_file_receipt(path: Path, *, safe_label: str) -> tuple[int, int, str]:
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
    ):
        raise PortableProjectError(
            "source_changed_during_copy",
            "Draft source changed while its package receipt was computed",
            {"file": safe_label},
        )
    return before.st_size, before.st_mtime_ns, digest


def _draft_tree_receipt(source: Path, *, relative_dir: Path = Path()) -> dict[str, tuple[Any, ...]]:
    receipt: dict[str, tuple[Any, ...]] = {}
    for entry in sorted(os.scandir(source), key=lambda row: row.name.casefold()):
        source_path = Path(entry.path)
        is_directory = entry.is_dir(follow_symlinks=False)
        relative = relative_dir / entry.name
        if is_sensitive_draft_artifact(relative.as_posix(), is_directory=is_directory):
            raise PortableProjectError(
                "unsafe_draft_artifact",
                "Draft tree contains a sensitive or source-control artifact",
                {"path": relative.as_posix()},
            )
        if _is_transient_draft_entry(entry.name, is_directory=is_directory):
            continue
        if _is_reparse(source_path):
            raise PortableProjectError(
                "unsafe_draft_source",
                "Draft tree contains a reparse point",
                {"file": entry.name},
            )
        relative_text = relative.as_posix()
        if is_directory:
            receipt[relative_text] = ("directory",)
            receipt.update(_draft_tree_receipt(source_path, relative_dir=relative))
        elif entry.is_file(follow_symlinks=False):
            receipt[relative_text] = (
                "file",
                *_stable_source_file_receipt(source_path, safe_label=entry.name),
            )
        else:
            raise PortableProjectError(
                "unsafe_draft_source",
                "Draft tree contains a non-regular entry",
                {"file": entry.name},
            )
    return receipt


def _copy_draft_tree(
    source: Path,
    target: Path,
    *,
    relative_dir: Path = Path(),
    excluded_paths: list[str] | None = None,
) -> list[str]:
    excluded = excluded_paths if excluded_paths is not None else []
    target.mkdir(parents=True)
    for entry in sorted(os.scandir(source), key=lambda row: row.name.casefold()):
        source_path = Path(entry.path)
        target_path = target / entry.name
        is_directory = entry.is_dir(follow_symlinks=False)
        relative = relative_dir / entry.name
        if is_sensitive_draft_artifact(relative.as_posix(), is_directory=is_directory):
            raise PortableProjectError(
                "unsafe_draft_artifact",
                "Draft tree contains a sensitive or source-control artifact",
                {"path": relative.as_posix()},
            )
        if _is_transient_draft_entry(entry.name, is_directory=is_directory):
            excluded.append(f"Draft/{relative.as_posix()}")
            continue
        if _is_reparse(source_path):
            raise PortableProjectError(
                "unsafe_draft_source", "Draft tree contains a reparse point", {"file": entry.name}
            )
        if is_directory:
            _copy_draft_tree(
                source_path,
                target_path,
                relative_dir=relative,
                excluded_paths=excluded,
            )
        elif entry.is_file(follow_symlinks=False):
            _copy_regular_file_checked(source_path, target_path, safe_label=entry.name)
        else:
            raise PortableProjectError(
                "unsafe_draft_source",
                "Draft tree contains a non-regular entry",
                {"file": entry.name},
            )
    return excluded


def _load_importer_receipt(
    importer: Path, receipt: Path | Mapping[str, Any] | None
) -> dict[str, Any]:
    if receipt is None:
        adjacent = importer.parent / "build-receipt.json"
        if not adjacent.is_file():
            raise PortableProjectError(
                "importer_receipt_missing", "Verified importer build receipt is required"
            )
        receipt = adjacent
    if isinstance(receipt, Mapping):
        payload = dict(receipt)
    else:
        try:
            payload = json.loads(Path(receipt).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PortableProjectError(
                "importer_receipt_invalid", "Importer build receipt is not readable JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise PortableProjectError(
                "importer_receipt_invalid", "Importer build receipt is invalid"
            )
    return validate_build_receipt(importer, payload)


def _source_environment(
    documents: DraftDocumentSet, supplied: Mapping[str, Any] | None
) -> dict[str, Any]:
    return source_provenance(
        documents.root_content,
        documents.timeline_contents,
        supplied,
    )


def _role(relative_path: str) -> str:
    if relative_path == IMPORTER_FILENAME:
        return "importer"
    if relative_path == INSTRUCTIONS_FILENAME:
        return "instructions"
    if relative_path.startswith("Media/"):
        return "material"
    if relative_path.startswith("Draft/"):
        return "draft"
    return "package"


def _inventory_files(staging: Path) -> list[dict[str, Any]]:
    candidates = (
        path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_file()
    )
    paths = sorted(path for path in candidates if path not in {MANIFEST_FILENAME, REPORT_FILENAME})
    return [package_file_row(staging, path, role=_role(path)) for path in paths]


def _inventory_directories(staging: Path) -> list[str]:
    return sorted(
        path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_dir()
    )


def _instructions(project_name: str) -> str:
    return (
        f"项目：{project_name}\n\n"
        "1. 只运行通过可信渠道收到的完整交付包。\n"
        "2. 包内哈希不等同于代码签名或发布者身份认证。\n"
        "3. 请先完整解压此文件夹。\n"
        "4. 关闭剪映专业版。\n"
        f"5. 双击 {IMPORTER_FILENAME} 并按提示导入。\n"
        "6. 导入后打开、预览并确认时间线仍可编辑。\n\n"
        "剪映原生链接素材仅用于单个素材的诊断兜底，不代表完整工程已通过验收。\n"
    )


def _check_output_location(draft: Path, output: Path) -> None:
    if output.exists():
        raise PortableProjectError("package_exists", "Destination package already exists")
    try:
        output.relative_to(draft)
    except ValueError:
        return
    raise PortableProjectError(
        "invalid_output_path", "Destination package cannot be created inside the source draft"
    )


def _check_snapshot_location(draft: Path, snapshot_dir: Path | None) -> None:
    if snapshot_dir is None:
        return
    snapshot = Path(snapshot_dir).resolve()
    try:
        snapshot.relative_to(draft)
    except ValueError:
        return
    raise PortableProjectError(
        "unsafe_snapshot_location",
        "Readable snapshot cannot be stored inside the source draft",
    )


def package_project(
    draft_dir: Path,
    output_dir: Path,
    importer_exe: Path,
    snapshot_dir: Path | None = None,
    expected_snapshot_job_digest: str | None = None,
    expected_snapshot_receipt_sha256: str | None = None,
    source_environment: Mapping[str, Any] | None = None,
    importer_receipt: Path | Mapping[str, Any] | None = None,
    structure_policy: str = "revision",
) -> dict[str, Any]:
    draft = Path(draft_dir).resolve()
    output = Path(output_dir).resolve()
    importer = Path(importer_exe)
    structure_policy = normalize_structure_policy(structure_policy)
    _check_output_location(draft, output)
    _check_snapshot_location(draft, snapshot_dir)
    draft_tree_before = _draft_tree_receipt(draft)
    documents = discover_material_references(
        draft,
        snapshot_dir=snapshot_dir,
        expected_snapshot_job_digest=expected_snapshot_job_digest,
        expected_snapshot_receipt_sha256=expected_snapshot_receipt_sha256,
    )

    _validate_regular_source(
        importer,
        missing_code="importer_missing",
        nonregular_code="importer_invalid",
        reparse_code="importer_invalid",
    )
    verified_receipt = _load_importer_receipt(importer, importer_receipt)
    for material in documents.materials:
        validate_material_id(material.material_id)
        _validate_regular_source(
            material.source_path,
            missing_code="missing_local_media",
            nonregular_code="non_regular_media",
            reparse_code="reparse_source_media",
            safe_data={"material_id": material.material_id, "basename": material.basename},
        )
        material_relative_path(material.bucket, material.material_id, material.basename)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.autocut-staging-", dir=output.parent))
    try:
        draft_target = staging / "Draft"
        excluded_draft_paths = _copy_draft_tree(draft, draft_target)
        if _draft_tree_receipt(draft) != draft_tree_before:
            raise PortableProjectError(
                "source_changed_during_copy",
                "Draft tree changed while the portable package was copied",
            )
        targets = {
            material.material_id: logical_media_path(
                material.bucket, material.material_id, material.basename
            )
            for material in documents.materials
        }
        rewritten = rewrite_draft_documents(
            documents,
            targets,
            target_draft_root=DRAFT_ROOT_MARKER,
            target_draft_dir=DRAFT_DIR_MARKER,
            project_name=str(documents.meta.get("draft_name") or draft.name),
        )
        for relative_path, payload in rewritten.items():
            _write_json(draft_target.joinpath(*relative_path.split("/")), payload)

        material_rows: list[dict[str, Any]] = []
        for material in documents.materials:
            relative = material_relative_path(
                material.bucket, material.material_id, material.basename
            )
            target = staging.joinpath(*relative.split("/"))
            copied = _copy_regular_file_checked(
                material.source_path, target, safe_label=material.basename
            )
            material_rows.append(
                {
                    "material_id": material.material_id,
                    "kind": material.bucket.title(),
                    "basename": material.basename,
                    "package_path": relative,
                    "byte_size": copied["byte_size"],
                    "sha256": copied["sha256"],
                    "json_locations": list(material.json_locations),
                }
            )

        importer_target = staging / IMPORTER_FILENAME
        _copy_regular_file_checked(importer, importer_target, safe_label=IMPORTER_FILENAME)
        if (
            importer_target.stat().st_size != verified_receipt["byte_size"]
            or sha256_file(importer_target) != verified_receipt["sha256"]
        ):
            raise PortableProjectError(
                "importer_hash_mismatch", "Packaged importer does not match its build receipt"
            )
        (staging / INSTRUCTIONS_FILENAME).write_text(
            _instructions(str(documents.meta.get("draft_name") or draft.name)),
            encoding="utf-8",
        )

        source = _source_environment(documents, source_environment)
        root = rewritten["draft_content.json"]
        active = rewritten.get(f"Timelines/{documents.active_timeline_id}/draft_content.json", root)
        timeline_structures = {
            timeline_id: draft_structure_receipt(
                rewritten[f"Timelines/{timeline_id}/draft_content.json"],
                rewritten[f"Timelines/{timeline_id}/draft_content.json"],
                structure_policy=timeline_structure_policy(
                    structure_policy,
                    timeline_id=timeline_id,
                    active_timeline_id=documents.active_timeline_id,
                ),
            )
            for timeline_id in sorted(documents.timeline_contents)
        }
        manifest = seal_manifest(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "project": {
                    "name": str(documents.meta.get("draft_name") or draft.name),
                    "source_app": source["app_name"],
                    "source_version": source["app_version"],
                    "detected_source_app": source["detected_source_app"],
                    "detected_source_version": source["detected_source_version"],
                    "source_app_selection_basis": source["source_app_selection_basis"],
                    "detected_app_identities": source["detected_app_identities"],
                    "compatibility_schema_version": source["compatibility_schema_version"],
                    "draft_platform_versions": source["draft_platform_versions"],
                    "draft_source_mode": documents.source_mode,
                    "active_timeline_id": documents.active_timeline_id,
                    "timeline_ids": sorted(documents.timeline_contents),
                    "structure_policy": structure_policy,
                },
                "tokens": {
                    "media": "@AUTOCUT_MEDIA@",
                    "draft_root": DRAFT_ROOT_MARKER,
                    "draft_dir": DRAFT_DIR_MARKER,
                },
                "documents": {
                    "rewritten_json": sorted(
                        f"Draft/{relative_path}" for relative_path in rewritten
                    )
                },
                "draft_copy": {
                    "transient_policy_version": TRANSIENT_DRAFT_POLICY_VERSION,
                    "excluded_paths": sorted(excluded_draft_paths),
                },
                "structure": draft_structure_receipt(
                    root, active, structure_policy=structure_policy
                ),
                "timeline_structures": timeline_structures,
                "materials": sorted(material_rows, key=lambda row: row["material_id"]),
                "directories": _inventory_directories(staging),
                "files": _inventory_files(staging),
                "importer": {
                    "path": IMPORTER_FILENAME,
                    **verified_receipt,
                },
                "non_file_dependencies": discover_non_file_dependencies(documents),
            }
        )
        _write_json(staging / MANIFEST_FILENAME, manifest)
        report = validate_package(staging, require_report=False)
        _write_json(staging / REPORT_FILENAME, report)
        report = validate_package(staging)
        if output.exists():
            raise PortableProjectError("package_exists", "Destination package already exists")
        staging.rename(output)
        try:
            promoted_report = validate_package(output)
        except PortableProjectError as exc:
            withdraw_promoted_directory(
                output,
                quarantine_prefix=output.name,
                failure_code="package_changed_after_promotion",
            )
            raise PortableProjectError(
                "package_changed_after_promotion",
                "Portable project changed while it was being promoted",
                {"cause_code": exc.code},
            ) from exc
        return promoted_report
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
