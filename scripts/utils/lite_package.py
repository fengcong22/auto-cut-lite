"""Build the final offline ZIP for the High School History lite workflow.

The lite delivery is intentionally a byte-preserving file package.  It does
not invoke JianYing, rewrite draft JSON, or import a portable-project format.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from utils.jianying_native_delivery import capture_draft_tree_receipt


PACKAGE_SCHEMA_VERSION = 2
RELINK_TOOL_FILENAME = "Auto-Cut剪映素材重链工具.exe"
INSTRUCTIONS_FILENAME = "使用说明.txt"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_INVALID_COMPONENT_CHARS = set('<>:"/\\|?*')
_NAME_RESOLUTION_REQUIRED_FIELDS = frozenset(
    {"requested_name", "final_name", "source", "sanitized"}
)
_NAME_RESOLUTION_OPTIONAL_FIELDS = frozenset(
    {"pre_fallback_name", "draft_fallback_applied"}
)


class LitePackageError(ValueError):
    """A safe, user-actionable lite package failure."""


def _normalize_name_resolution(
    value: Mapping[str, Any] | None,
    *,
    draft_name: str,
) -> dict[str, Any]:
    if value is None:
        return {
            "requested_name": draft_name,
            "final_name": draft_name,
            "source": "draft_directory",
            "sanitized": False,
            "draft_fallback_applied": False,
        }
    if not isinstance(value, Mapping):
        raise LitePackageError("name_resolution must be an object")
    fields = set(value)
    allowed = _NAME_RESOLUTION_REQUIRED_FIELDS | _NAME_RESOLUTION_OPTIONAL_FIELDS
    if not _NAME_RESOLUTION_REQUIRED_FIELDS.issubset(fields) or not fields.issubset(allowed):
        raise LitePackageError("name_resolution has an invalid schema")
    normalized = dict(value)
    for field in ("requested_name", "final_name", "source"):
        if not isinstance(normalized.get(field), str) or not normalized[field].strip():
            raise LitePackageError(f"name_resolution.{field} must be non-empty text")
    if normalized["final_name"] != draft_name:
        raise LitePackageError(
            "name_resolution.final_name must equal the actual draft name: "
            f"expected={draft_name!r} actual={normalized['final_name']!r}"
        )
    if not isinstance(normalized.get("sanitized"), bool):
        raise LitePackageError("name_resolution.sanitized must be a boolean")
    fallback_applied = normalized.get("draft_fallback_applied", False)
    if not isinstance(fallback_applied, bool):
        raise LitePackageError("name_resolution.draft_fallback_applied must be a boolean")
    normalized["draft_fallback_applied"] = fallback_applied
    pre_fallback = normalized.get("pre_fallback_name")
    if pre_fallback is not None and (
        not isinstance(pre_fallback, str) or not pre_fallback.strip()
    ):
        raise LitePackageError("name_resolution.pre_fallback_name must be non-empty text")
    if fallback_applied and pre_fallback is None:
        raise LitePackageError(
            "name_resolution.pre_fallback_name is required when fallback was applied"
        )
    return normalized


def _normalize_execution_input_digest(value: str) -> str:
    if not isinstance(value, str):
        raise LitePackageError("execution_input_digest must be text")
    if value and (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LitePackageError("execution_input_digest must be empty or a lowercase SHA-256")
    return value


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LitePackageError(f"无法读取路径：{path}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _require_directory(path: str | os.PathLike[str], label: str) -> Path:
    raw = Path(path).expanduser()
    if raw.exists() and _is_reparse(raw):
        raise LitePackageError(f"{label}包含重解析点：{raw}")
    resolved = raw.resolve(strict=False)
    if _is_reparse(resolved) or not resolved.is_dir():
        raise LitePackageError(f"{label}不是可用的文件夹：{resolved}")
    return resolved


def _require_file(path: str | os.PathLike[str], label: str) -> Path:
    raw = Path(path).expanduser()
    if raw.exists() and _is_reparse(raw):
        raise LitePackageError(f"{label}包含重解析点：{raw}")
    resolved = raw.resolve(strict=False)
    if _is_reparse(resolved) or not resolved.is_file():
        raise LitePackageError(f"{label}不是可用的文件：{resolved}")
    return resolved


def _single_component(value: str | None, label: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."}:
        raise LitePackageError(f"{label}不能为空")
    if any(char in _INVALID_COMPONENT_CHARS or ord(char) < 32 for char in text):
        raise LitePackageError(f"{label}包含不安全字符")
    if len(PurePosixPath(text).parts) != 1 or text.endswith((".", " ")):
        raise LitePackageError(f"{label}必须是单层文件夹名称")
    return text


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_packaged_material(draft: Path, raw_path: Any) -> Path:
    value = str(raw_path or "").strip().replace("/", "\\")
    markers = ("\\resources\\local\\", "\\resources\\audioalg\\")
    matches = [(marker, value.casefold().find(marker)) for marker in markers]
    matches = [(marker, index) for marker, index in matches if index >= 0]
    if len(matches) != 1:
        raise LitePackageError(f"Lite draft contains an external material path: {raw_path}")
    marker, index = matches[0]
    relative_parts = value[index + 1 :].split("\\")
    if any(part in {"", ".", ".."} for part in relative_parts):
        raise LitePackageError(f"Lite draft contains an unsafe material path: {raw_path}")
    target = draft.joinpath(*relative_parts).resolve(strict=False)
    allowed = (
        draft / "Resources" / ("audioAlg" if "audioalg" in marker else "local")
    ).resolve(strict=False)
    if not _inside(target, allowed) or not target.is_file() or _is_reparse(target):
        raise LitePackageError(f"Lite draft packaged material is missing: {target}")
    return target


def _validate_packaged_materials(draft: Path) -> int:
    content_paths = [draft / "draft_content.json"]
    timelines = draft / "Timelines"
    if timelines.is_dir():
        content_paths.extend(sorted(timelines.glob("*/draft_content.json")))

    reference_count = 0
    for content_path in content_paths:
        try:
            content = json.loads(content_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise LitePackageError(
                f"Lite draft content is not readable JSON: {content_path}"
            ) from exc
        materials = content.get("materials") if isinstance(content, dict) else None
        if not isinstance(materials, dict):
            raise LitePackageError(f"Lite draft content is missing materials: {content_path}")
        for rows in materials.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for field in ("path", "file_path", "file_Path"):
                    if str(row.get(field) or "").strip():
                        _resolve_packaged_material(draft, row[field])
                        reference_count += 1

    meta_path = draft / "draft_meta_info.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise LitePackageError(f"Lite draft metadata is not readable JSON: {meta_path}") from exc
    if not isinstance(metadata, dict):
        raise LitePackageError(f"Lite draft metadata root is not an object: {meta_path}")
    if str(metadata.get("draft_name") or "") != draft.name:
        raise LitePackageError(
            "Lite draft internal name must equal its directory name: "
            f"internal={metadata.get('draft_name')!r} directory={draft.name!r}"
        )
    for bucket in metadata.get("draft_materials") or []:
        if not isinstance(bucket, dict):
            continue
        for row in bucket.get("value") or []:
            if isinstance(row, dict) and str(row.get("file_Path") or "").strip():
                _resolve_packaged_material(draft, row["file_Path"])
                reference_count += 1
    return reference_count


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=False)
    for entry in sorted(os.scandir(source), key=lambda item: (item.name.casefold(), item.name)):
        source_path = Path(entry.path)
        target_path = target / entry.name
        if _is_reparse(source_path):
            raise LitePackageError(f"草稿树包含重解析点：{source_path}")
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise LitePackageError(f"无法读取草稿条目：{source_path}") from exc
        if stat.S_ISDIR(metadata.st_mode):
            _copy_tree(source_path, target_path)
        elif stat.S_ISREG(metadata.st_mode):
            shutil.copy2(source_path, target_path, follow_symlinks=False)
            if (
                target_path.stat().st_size != metadata.st_size
                or sha256_file(source_path) != sha256_file(target_path)
            ):
                raise LitePackageError(f"草稿文件复制校验失败：{source_path}")
        else:
            raise LitePackageError(f"草稿树包含非普通文件：{source_path}")


def _copy_tree_contents(source: Path, target: Path) -> None:
    """Copy a draft's entries directly into an already-created package root."""

    for entry in sorted(os.scandir(source), key=lambda item: (item.name.casefold(), item.name)):
        source_path = Path(entry.path)
        target_path = target / entry.name
        if _is_reparse(source_path):
            raise LitePackageError(f"草稿树包含重解析点：{source_path}")
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise LitePackageError(f"无法读取草稿条目：{source_path}") from exc
        if stat.S_ISDIR(metadata.st_mode):
            _copy_tree(source_path, target_path)
        elif stat.S_ISREG(metadata.st_mode):
            shutil.copy2(source_path, target_path, follow_symlinks=False)
            if (
                target_path.stat().st_size != metadata.st_size
                or sha256_file(source_path) != sha256_file(target_path)
            ):
                raise LitePackageError(f"草稿文件复制校验失败：{source_path}")
        else:
            raise LitePackageError(f"草稿树包含非普通文件：{source_path}")


def _safe_zip_name(name: str) -> str:
    candidate = PurePosixPath(name)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or any("\\" in part for part in candidate.parts)
    ):
        raise LitePackageError(f"ZIP 路径不安全：{name}")
    return candidate.as_posix()


def _zip_info(name: str, *, is_dir: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o40755 if is_dir else 0o100644) << 16
    if is_dir:
        info.external_attr |= 0x10
    return info


def _write_instructions(draft_name: str, *, has_relink_tool: bool) -> str:
    lines = [
        "Auto-Cut 高中历史精简版剪映草稿 ZIP",
        "",
        "本包由精简版全流程后台自动生成，未打开剪映，也未改写草稿 JSON。",
        f"草稿文件夹：{draft_name}",
        "",
        "使用方式：",
        "1. 解压整个 ZIP。",
        f"2. 将“{draft_name}”文件夹放入目标电脑最终的剪映草稿目录。",
        "3. 固定路径一致时，直接打开剪映即可。",
    ]
    if has_relink_tool:
        lines.extend(
            [
                "4. 如果目标电脑路径不同，先完全退出剪映，再在此草稿文件夹内运行“Auto-Cut剪映素材重链工具.exe”。",
                "5. 工具会优先自动识别同目录的 draft_content.json；确认后再备份并重链。",
                "6. 重链完成后重新打开剪映。工具只处理草稿 Resources/local 或 Resources/audioAlg 中的本地素材。",
            ]
        )
    lines.extend(
        [
            "",
            "安全说明：",
            "- 打包阶段不会打开剪映、下载素材、移动素材或修改草稿 JSON。",
            "- 重链工具只在用户主动运行时修改草稿，并会先写入 .autocut-relink-backup。",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def _archive_staging(staging: Path, archive_path: Path) -> int:
    names_seen: set[str] = set()
    names_casefolded: set[str] = set()
    entries: list[tuple[Path, str, bool]] = []
    for current, directories, files in os.walk(staging, topdown=True, followlinks=False):
        current_path = Path(current)
        if _is_reparse(current_path):
            raise LitePackageError(f"打包树包含重解析点：{current_path}")
        directories[:] = sorted(directories, key=lambda value: (value.casefold(), value))
        files[:] = sorted(files, key=lambda value: (value.casefold(), value))
        for name in directories:
            path = current_path / name
            if _is_reparse(path):
                raise LitePackageError(f"打包树包含重解析点：{path}")
            relative = _safe_zip_name(path.relative_to(staging).as_posix()) + "/"
            entries.append((path, relative, True))
        for name in files:
            path = current_path / name
            if _is_reparse(path) or not path.is_file():
                raise LitePackageError(f"打包树包含非普通文件：{path}")
            relative = _safe_zip_name(path.relative_to(staging).as_posix())
            entries.append((path, relative, False))

    for _path, name, _is_dir in entries:
        normalized = name.rstrip("/").casefold()
        if name in names_seen or normalized in names_casefolded:
            raise LitePackageError(f"ZIP 存在重复或大小写冲突路径：{name}")
        names_seen.add(name)
        names_casefolded.add(normalized)

    with zipfile.ZipFile(
        archive_path,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for path, name, is_dir in entries:
            if is_dir:
                archive.writestr(_zip_info(name, is_dir=True), b"")
            else:
                with path.open("rb") as source, archive.open(
                    _zip_info(name, is_dir=False), "w"
                ) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
    return len(entries)


def _validate_archive(
    archive_path: Path, extracted_root: Path, expected_tree: Mapping[str, Any]
) -> dict[str, Any]:
    with zipfile.ZipFile(archive_path, "r") as archive:
        if archive.testzip() is not None:
            raise LitePackageError("ZIP CRC 校验失败")
        names = archive.namelist()
        if len(names) != len({name.rstrip("/").casefold() for name in names}):
            raise LitePackageError("ZIP 存在重复或大小写冲突路径")
        for name in names:
            _safe_zip_name(name.rstrip("/"))
        archive.extractall(extracted_root)
    actual_tree = capture_draft_tree_receipt(extracted_root)
    if actual_tree["tree_sha256"] != expected_tree["tree_sha256"]:
        raise LitePackageError(
            "ZIP 解压树校验失败："
            f"expected={expected_tree['tree_sha256']} actual={actual_tree['tree_sha256']}"
        )
    return actual_tree


def _write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def package_lite_delivery(
    draft_dir: str | os.PathLike[str],
    output_zip: str | os.PathLike[str],
    *,
    relink_tool: str | os.PathLike[str] | None = None,
    package_root_name: str | None = None,
    receipt_json: str | os.PathLike[str] | None = None,
    name_resolution: Mapping[str, Any] | None = None,
    execution_input_digest: str = "",
) -> dict[str, Any]:
    """Create and validate the final lite ZIP without opening JianYing."""

    source = _require_directory(draft_dir, "草稿目录")
    if not (source / "draft_content.json").is_file() or not (source / "draft_meta_info.json").is_file():
        raise LitePackageError("草稿目录缺少 draft_content.json 或 draft_meta_info.json")
    if source.name.casefold().endswith(".zip"):
        raise LitePackageError("草稿目录名不能带 .zip 后缀")

    output = Path(output_zip).expanduser().resolve(strict=False)
    if output.suffix.casefold() != ".zip":
        raise LitePackageError("最终交付文件必须是 .zip")
    expected_output_name = f"{source.name}.zip"
    if output.name != expected_output_name:
        raise LitePackageError(
            "ZIP 文件名必须与草稿目录名一致："
            f"expected={expected_output_name!r} actual={output.name!r}"
        )
    if output.exists():
        raise LitePackageError(f"最终 ZIP 已存在，不覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if _inside(output, source):
        raise LitePackageError("最终 ZIP 不能写入草稿目录内部")

    receipt = (
        Path(receipt_json).expanduser().resolve(strict=False)
        if receipt_json is not None
        else output.with_name(f"{output.name}.receipt.json")
    )
    if receipt.exists():
        raise LitePackageError(f"ZIP 收据已存在，不覆盖：{receipt}")
    if receipt == output:
        raise LitePackageError("ZIP 收据路径不能与最终 ZIP 相同")
    if _inside(receipt, source):
        raise LitePackageError("ZIP 收据不能写入草稿目录内部")

    tool: Path | None = None
    if relink_tool is not None:
        tool = _require_file(relink_tool, "重链工具")
        if _inside(tool, source):
            raise LitePackageError("重链工具不能位于草稿目录内部")

    root_name = _single_component(package_root_name or source.name, "ZIP 根目录名称")
    if root_name != source.name:
        raise LitePackageError(
            "ZIP 根目录名必须与草稿目录名一致："
            f"expected={source.name!r} actual={root_name!r}"
        )
    if source.name in {RELINK_TOOL_FILENAME, INSTRUCTIONS_FILENAME}:
        raise LitePackageError("草稿名称与 ZIP 根目录文件名冲突")
    resolved_name = _normalize_name_resolution(name_resolution, draft_name=source.name)
    input_digest = _normalize_execution_input_digest(execution_input_digest)
    localized_material_reference_count = _validate_packaged_materials(source)
    source_tree = capture_draft_tree_receipt(source)
    staging_parent = Path(tempfile.mkdtemp(prefix="autocut-lite-package-", dir=output.parent))
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(handle)
    temporary_zip = Path(temporary_name)
    temporary_zip.unlink()
    extracted_parent = Path(tempfile.mkdtemp(prefix="autocut-lite-extract-", dir=output.parent))
    staging = staging_parent / root_name
    try:
        staging.mkdir()
        staged_draft = staging
        _copy_tree_contents(source, staged_draft)
        source_after_copy = capture_draft_tree_receipt(source)
        if source_after_copy["tree_sha256"] != source_tree["tree_sha256"]:
            raise LitePackageError("草稿在打包期间发生变化，未生成交付 ZIP")
        staged_source_tree = capture_draft_tree_receipt(staged_draft)
        if staged_source_tree["tree_sha256"] != source_tree["tree_sha256"]:
            raise LitePackageError("草稿复制后的源树哈希不一致")

        tool_hash = ""
        if tool is not None:
            staged_tool = staging / RELINK_TOOL_FILENAME
            if staged_tool.exists():
                raise LitePackageError(
                    f"草稿内已有同名重链工具文件，无法安全封包：{staged_tool}"
                )
            shutil.copy2(tool, staged_tool, follow_symlinks=False)
            if sha256_file(tool) != sha256_file(staged_tool):
                raise LitePackageError("重链工具复制校验失败")
            tool_hash = sha256_file(tool)
        instructions_path = staging / INSTRUCTIONS_FILENAME
        if instructions_path.exists():
            raise LitePackageError(
                f"草稿内已有同名说明文件，无法安全封包：{instructions_path}"
            )
        _write_text(
            instructions_path,
            _write_instructions(source.name, has_relink_tool=tool is not None),
        )

        # The archive is rooted at ``root_name``.  Capture its parent so the
        # expected receipt has the same directory boundary as extraction.
        package_tree = capture_draft_tree_receipt(staging_parent)
        entry_count = _archive_staging(staging_parent, temporary_zip)
        extracted_tree = _validate_archive(temporary_zip, extracted_parent, package_tree)
        archive_sha256 = sha256_file(temporary_zip)
        os.replace(temporary_zip, output)
        payload: dict[str, Any] = {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "status": "pass",
            "workflow_mode": "lite",
            "delivery_mode": "lite_zip",
            "archive_path": str(output),
            "archive_sha256": archive_sha256,
            "archive_byte_size": output.stat().st_size,
            "archive_entry_count": entry_count,
            "package_root_name": root_name,
            "draft_name": source.name,
            "name_resolution": resolved_name,
            "execution_input_digest": input_digest,
            "package_layout": "draft_root_bundle_v2",
            "relink_tool_in_draft_root": tool is not None,
            "instructions_in_draft_root": True,
            "source_draft_path": str(source),
            "source_tree_sha256": source_tree["tree_sha256"],
            "package_tree_sha256": package_tree["tree_sha256"],
            "extracted_tree_sha256": extracted_tree["tree_sha256"],
            "source_file_count": source_tree["file_count"],
            "package_file_count": package_tree["file_count"],
            "extracted_file_count": extracted_tree["file_count"],
            "source_byte_size": source_tree["byte_size"],
            "localized_material_reference_count": localized_material_reference_count,
            "package_byte_size": package_tree["byte_size"],
            "extracted_byte_size": extracted_tree["byte_size"],
            "relink_tool_included": tool is not None,
            "relink_tool_sha256": tool_hash,
            "instructions_included": True,
            "zip_crc_pass": True,
            "zip_tree_identity_pass": True,
            "path_traversal_rejected": True,
            "reparse_points_rejected": True,
            "json_rewritten": False,
            "ui_invoked": False,
            "opened_jianying": False,
            "portable_package_invoked": False,
        }
        _write_receipt(receipt, payload)
        payload["receipt_path"] = str(receipt)
        payload["receipt_sha256"] = sha256_file(receipt)
        return payload
    except Exception:
        try:
            temporary_zip.unlink()
        except FileNotFoundError:
            pass
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        try:
            receipt.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
        shutil.rmtree(extracted_parent, ignore_errors=True)


__all__ = [
    "INSTRUCTIONS_FILENAME",
    "LitePackageError",
    "PACKAGE_SCHEMA_VERSION",
    "RELINK_TOOL_FILENAME",
    "package_lite_delivery",
    "sha256_file",
]
