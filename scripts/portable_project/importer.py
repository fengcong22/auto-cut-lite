from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .draft_json import (
    DraftDocumentSet,
    MaterialReference,
    rewrite_draft_documents,
)
from .errors import PortableProjectError
from .hashing import sha256_file
from .manifest import read_manifest, validate_relative_path
from .projection import installation_projection
from .promotion import withdraw_promoted_directory
from .validator import directory_tree_receipt, validate_installed_project, validate_package
from .version_policy import evaluate_version_policy

_REPARSE_POINT_ATTRIBUTE = 0x400


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )


def _load_json_object(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.is_file() and not required:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableProjectError(
            "installed_draft_validation_failed", "Package draft JSON is not readable"
        ) from exc
    if not isinstance(payload, dict):
        raise PortableProjectError(
            "installed_draft_validation_failed", "Package draft JSON is not an object"
        )
    return payload


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        int(getattr(info, "st_file_attributes", 0)) & _REPARSE_POINT_ATTRIBUTE
    )


def _safe_project_name(value: object) -> str:
    name = str(value or "").strip()
    if (
        not name
        or len(name) > 128
        or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in name)
    ):
        raise PortableProjectError(
            "invalid_project_name", "Target project name is not a safe Windows directory name"
        )
    try:
        normalized = validate_relative_path(name)
    except PortableProjectError as exc:
        raise PortableProjectError(
            "invalid_project_name", "Target project name is not a safe Windows directory name"
        ) from exc
    if normalized in {".", ".."} or len(PurePosixPath(normalized).parts) != 1:
        raise PortableProjectError(
            "invalid_project_name", "Target project name must be one directory name"
        )
    return normalized


def _existing_reparse_component(path: Path) -> Path | None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PortableProjectError(
                "invalid_target_root", "Target drafts root could not be inspected"
            ) from exc
        if _is_reparse(current):
            return current
    return None


def _target_root_identity(target_root: Path) -> tuple[int, int]:
    try:
        metadata = target_root.lstat()
    except OSError as exc:
        raise PortableProjectError(
            "invalid_target_root", "Target drafts root could not be inspected"
        ) from exc
    if _is_reparse(target_root) or not stat.S_ISDIR(metadata.st_mode):
        raise PortableProjectError(
            "invalid_target_root", "Target drafts root is not a safe directory"
        )
    return metadata.st_dev, metadata.st_ino


def _ensure_target_root(package: Path, target_root: Path) -> tuple[Path, tuple[int, int]]:
    target_root = Path(os.path.abspath(target_root))
    if _existing_reparse_component(target_root) is not None:
        raise PortableProjectError(
            "invalid_target_root", "Target drafts root contains a reparse point"
        )
    resolved_target = target_root.resolve(strict=False)
    try:
        resolved_target.relative_to(package.resolve())
    except ValueError:
        pass
    else:
        raise PortableProjectError(
            "invalid_target_root", "Target drafts root cannot be inside the delivery package"
        )
    if target_root.exists():
        if _is_reparse(target_root) or not target_root.is_dir():
            raise PortableProjectError(
                "invalid_target_root", "Target drafts root is not a safe directory"
            )
    else:
        try:
            target_root.mkdir(parents=True)
        except OSError as exc:
            raise PortableProjectError(
                "invalid_target_root", "Target drafts root could not be created"
            ) from exc
    if _existing_reparse_component(target_root) is not None:
        raise PortableProjectError(
            "invalid_target_root", "Target drafts root contains a reparse point"
        )
    return target_root, _target_root_identity(target_root)


def _recheck_target_root(target_root: Path, expected_identity: tuple[int, int]) -> None:
    if (
        _existing_reparse_component(target_root) is not None
        or _target_root_identity(target_root) != expected_identity
    ):
        raise PortableProjectError(
            "invalid_target_root", "Target drafts root changed during import"
        )


def _package_document_set(staged_draft: Path, manifest: Mapping[str, Any]) -> DraftDocumentSet:
    project = manifest.get("project")
    material_rows = manifest.get("materials")
    if not isinstance(project, dict) or not isinstance(material_rows, list):
        raise PortableProjectError("invalid_manifest", "Package project identity is missing")
    timeline_ids = project.get("timeline_ids")
    if not isinstance(timeline_ids, list) or not all(
        isinstance(value, str) for value in timeline_ids
    ):
        raise PortableProjectError("invalid_manifest", "Package timeline inventory is invalid")
    root = _load_json_object(staged_draft / "draft_content.json")
    meta = _load_json_object(staged_draft / "draft_meta_info.json")
    timeline_contents = {
        timeline_id: _load_json_object(
            staged_draft / "Timelines" / timeline_id / "draft_content.json"
        )
        for timeline_id in timeline_ids
    }
    active_id = str(project.get("active_timeline_id") or "")
    active = timeline_contents.get(active_id, root)
    materials: list[MaterialReference] = []
    for raw_row in material_rows:
        if not isinstance(raw_row, dict):
            raise PortableProjectError("invalid_manifest", "Package material row is invalid")
        relative = validate_relative_path(raw_row.get("package_path"))
        material_id = str(raw_row.get("material_id") or "")
        basename = str(raw_row.get("basename") or "")
        materials.append(
            MaterialReference(
                material_id=material_id,
                bucket=str(raw_row.get("kind") or "").casefold(),
                source_path=staged_draft.joinpath(*PurePosixPath(relative).parts),
                display_name=basename,
                basename=basename,
                json_locations=tuple(raw_row.get("json_locations") or ()),
            )
        )
    return DraftDocumentSet(
        draft_dir=staged_draft,
        source_mode="portable_package",
        active_timeline_id=active_id,
        root_content=root,
        active_content=active,
        timeline_contents=timeline_contents,
        meta=meta,
        virtual_store=_load_json_object(staged_draft / "draft_virtual_store.json", required=False),
        timeline_layout=_load_json_object(staged_draft / "timeline_layout.json", required=False),
        timeline_project=_load_json_object(
            staged_draft / "Timelines" / "project.json", required=False
        ),
        materials=tuple(materials),
    )


def _copy_and_rebind(
    package: Path,
    staging: Path,
    final_draft: Path,
    manifest: Mapping[str, Any],
    project_name: str,
) -> None:
    _installation_projection_rows(manifest)
    shutil.copytree(package / "Draft", staging, dirs_exist_ok=True)
    shutil.copytree(package / "Media", staging / "Media", dirs_exist_ok=True)
    documents = _package_document_set(staging, manifest)
    targets = {
        material.material_id: final_draft.joinpath(
            *PurePosixPath(
                next(
                    str(row["package_path"])
                    for row in manifest["materials"]
                    if row.get("material_id") == material.material_id
                )
            ).parts
        )
        for material in documents.materials
    }
    rewritten = rewrite_draft_documents(
        documents,
        targets,
        target_draft_root=final_draft.parent,
        target_draft_dir=final_draft,
        project_name=project_name,
    )
    for relative, payload in rewritten.items():
        _write_json(staging.joinpath(*PurePosixPath(relative).parts), payload)


def _installation_projection_rows(
    manifest: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    return dict(installation_projection(manifest)["files"])


def _validate_staged_package_files(
    staging: Path,
    manifest: Mapping[str, Any],
) -> None:
    rows = manifest.get("files")
    documents = manifest.get("documents")
    if not isinstance(rows, list) or not isinstance(documents, dict):
        raise PortableProjectError("invalid_manifest", "Package file inventory is missing")
    rewritten = {
        validate_relative_path(value).removeprefix("Draft/")
        for value in documents.get("rewritten_json") or []
    }
    rewritten.discard("draft_virtual_store.json")
    projected = _installation_projection_rows(manifest)

    actual = {path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_file()}
    if actual != set(projected):
        raise PortableProjectError(
            "staged_package_hash_mismatch",
            "Installed staging file inventory does not match the package",
        )
    for relative, row in projected.items():
        if relative in rewritten:
            continue
        path = staging.joinpath(*PurePosixPath(relative).parts)
        if _is_reparse(path) or not path.is_file():
            raise PortableProjectError(
                "staged_package_hash_mismatch", "Installed staging contains an unsafe file"
            )
        if path.stat().st_size != row.get("byte_size") or sha256_file(path) != row.get("sha256"):
            raise PortableProjectError(
                "staged_package_hash_mismatch",
                "Installed staging bytes do not match the package manifest",
            )


def import_project(
    package_dir: Path,
    target_drafts_root: Path,
    target_environment: Mapping[str, Any],
    project_name: str | None = None,
    diagnostic_override: bool = False,
) -> dict[str, Any]:
    package = Path(package_dir).resolve()
    target_root = Path(os.path.abspath(target_drafts_root))
    if bool(target_environment.get("process_running")) or bool(
        target_environment.get("any_editor_process_running")
    ):
        raise PortableProjectError(
            "jianying_running", "Close JianYing or CapCut before importing a project"
        )
    probe_status = str(target_environment.get("editor_process_probe_status") or "").strip()
    process_state_unknown = (
        target_environment.get("process_running") is not False
        or target_environment.get("any_editor_process_running") is not False
        or probe_status != "known"
        or target_environment.get("editor_process_state_unknown") is not False
        or target_environment.get("editor_process_gate_clear") is not True
    )
    if process_state_unknown:
        raise PortableProjectError(
            "jianying_process_state_unknown",
            "Cannot confirm that all JianYing and CapCut processes are closed",
        )
    validate_package(package)
    package_tree_before = directory_tree_receipt(
        package,
        error_code="package_changed_during_import",
    )
    manifest = read_manifest(package)
    manifest_digest = str(manifest.get("manifest_sha256") or "")
    project = manifest.get("project")
    if not isinstance(project, dict):
        raise PortableProjectError("invalid_manifest", "Package project identity is missing")
    version_policy = evaluate_version_policy(
        str(project.get("source_app") or ""),
        str(project.get("source_version") or ""),
        str(target_environment.get("app_name") or ""),
        str(target_environment.get("app_version") or ""),
        diagnostic_override=diagnostic_override,
    )
    if version_policy["decision"] == "block":
        raise PortableProjectError(
            str(version_policy["code"]),
            "Target JianYing environment is not compatible with this package",
            {"version_policy": version_policy},
        )
    target_name = _safe_project_name(project_name or project.get("name"))
    target_root, target_root_identity = _ensure_target_root(package, target_root)
    final_draft = target_root / target_name
    if final_draft.exists():
        raise PortableProjectError(
            "target_draft_exists", "A draft with the target project name already exists"
        )

    _recheck_target_root(target_root, target_root_identity)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{target_name}.autocut-import-",
            dir=target_root,
        )
    )
    try:
        try:
            _copy_and_rebind(package, staging, final_draft, manifest, target_name)
        except PortableProjectError:
            raise
        except OSError as exc:
            raise PortableProjectError(
                "import_failed", "Portable project copy was interrupted"
            ) from exc
        try:
            package_tree_after_copy = directory_tree_receipt(
                package,
                error_code="package_changed_during_import",
            )
            if (
                package_tree_after_copy != package_tree_before
                or str(read_manifest(package).get("manifest_sha256") or "") != manifest_digest
            ):
                raise PortableProjectError(
                    "manifest_hash_mismatch", "Portable project identity changed during import"
                )
        except PortableProjectError as exc:
            raise PortableProjectError(
                "package_changed_during_import",
                "Portable project changed while it was being imported",
                {"cause_code": exc.code},
            ) from exc
        try:
            _validate_staged_package_files(staging, manifest)
            installed_report = validate_installed_project(
                staging,
                manifest,
                expected_final_draft_dir=final_draft,
            )
            staged_tree_receipt = installed_report["installed_tree_receipt"]
        except PortableProjectError as exc:
            raise PortableProjectError(
                "installed_draft_validation_failed",
                "Staged JianYing project failed installed-tree validation",
                {"cause_code": exc.code},
            ) from exc
        if final_draft.exists():
            raise PortableProjectError(
                "target_draft_exists", "A draft with the target project name already exists"
            )
        _recheck_target_root(target_root, target_root_identity)
        try:
            os.replace(staging, final_draft)
        except OSError as exc:
            raise PortableProjectError(
                "import_failed", "Portable project could not be promoted atomically"
            ) from exc
        try:
            _recheck_target_root(target_root, target_root_identity)
        except PortableProjectError:
            withdraw_promoted_directory(
                final_draft,
                quarantine_prefix=target_name,
                failure_code="invalid_target_root",
            )
            raise
        try:
            _validate_staged_package_files(final_draft, manifest)
            installed_report = validate_installed_project(
                final_draft,
                manifest,
                expected_final_draft_dir=final_draft,
            )
            if installed_report["installed_tree_receipt"] != staged_tree_receipt:
                raise PortableProjectError(
                    "installed_draft_validation_failed",
                    "Installed draft tree changed during promotion",
                )
        except PortableProjectError as exc:
            withdraw_promoted_directory(
                final_draft,
                quarantine_prefix=target_name,
                failure_code="installed_draft_changed_after_promotion",
            )
            raise PortableProjectError(
                "installed_draft_changed_after_promotion",
                "Installed JianYing project changed while it was being promoted",
                {"cause_code": exc.code},
            ) from exc
        try:
            package_tree_after = directory_tree_receipt(
                package,
                error_code="package_changed_during_import",
            )
            if package_tree_after != package_tree_before:
                raise PortableProjectError(
                    "package_changed_during_import",
                    "Portable project changed while it was being imported",
                )
        except PortableProjectError as exc:
            withdraw_promoted_directory(
                final_draft,
                quarantine_prefix=target_name,
                failure_code="package_changed_during_import",
            )
            raise PortableProjectError(
                "package_changed_during_import",
                "Portable project changed while it was being imported",
                {"cause_code": exc.code},
            ) from exc
        status = (
            "diagnostic_imported_unverified"
            if str(version_policy["code"]).startswith("diagnostic_override_")
            else "imported_static_ready"
        )
        return {
            "status": status,
            "draft_path": str(final_draft),
            "external_media_after_import": installed_report["external_media_after_import"],
            "material_name_mismatch": installed_report["material_name_mismatch"],
            "manifest_sha256": manifest_digest,
            "package_tree_sha256_before": package_tree_before["sha256"],
            "package_tree_sha256_after": package_tree_after["sha256"],
            "package_identity_unchanged": True,
            "material_count": installed_report["material_count"],
            "directory_count": installed_report["directory_count"],
            "file_count": installed_report["file_count"],
            "timeline_count": installed_report["timeline_count"],
            **installed_report["structure"],
            "structure": installed_report["structure"],
            "timeline_structures": installed_report["timeline_structures"],
            "non_file_dependency_count": installed_report["non_file_dependency_count"],
            "non_file_dependencies": installed_report["non_file_dependencies"],
            "installed_tree_receipt": installed_report["installed_tree_receipt"],
            "version_policy": version_policy,
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
