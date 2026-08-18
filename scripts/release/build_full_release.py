from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.release.audit_runtime_capabilities import audit_runtime_capabilities
from scripts.release.release_policy import (
    collect_release_paths,
    normalize_archive_path,
    scan_release_tree,
)
from scripts.release.release_transaction import (
    assert_release_source_unchanged,
    capture_clean_release_source,
    publish_file_no_replace,
    publish_files_no_replace,
    unique_sibling_temp,
)

FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def _require_runtime_capability_audit(
    repo_root: Path, release_paths: Iterable[str]
) -> dict[str, object]:
    result = audit_runtime_capabilities(repo_root, release_paths)
    findings = result.get("findings") if isinstance(result.get("findings"), list) else []
    if result.get("status") != "ready" or findings:
        codes = sorted(
            {
                str(finding.get("code") or "unknown")
                for finding in findings
                if isinstance(finding, dict)
            }
        )
        raise ValueError(
            "runtime capability audit failed" + (f": {', '.join(codes)}" if codes else "")
        )
    declared = result.get("declared_capability_ids")
    checked = result.get("checked_paths")
    return {
        "status": "ready",
        "capability_count": len(declared) if isinstance(declared, list) else 0,
        "checked_path_count": len(checked) if isinstance(checked, list) else 0,
        "finding_count": 0,
    }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _validate_release_identity(version: str, source_commit: str) -> None:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("release version is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", source_commit):
        raise ValueError("release source commit is invalid")


def _inventory_payload(
    *, repo_root: Path, release_paths: Iterable[str], version: str, source_commit: str
) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for path in sorted(release_paths):
        data = (repo_root / Path(*PurePosixPath(path).parts)).read_bytes()
        files.append({"path": path, "size": len(data), "sha256": _sha256_bytes(data)})
    inventory: dict[str, object] = {
        "schema_version": 1,
        "version": version,
        "source_commit": source_commit,
        "files": files,
    }
    inventory["inventory_sha256"] = _sha256_bytes(_canonical_json(inventory))
    return inventory


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_zip_from_paths(
    *,
    repo_root: Path,
    output_zip: Path,
    release_paths: Iterable[str],
    version: str,
    source_commit: str,
) -> dict[str, object]:
    root = repo_root.resolve()
    if output_zip.exists():
        raise FileExistsError("release ZIP already exists")
    _validate_release_identity(version, source_commit)
    paths = sorted({normalize_archive_path(path) for path in release_paths})
    findings = scan_release_tree(root, paths)
    if findings:
        codes = sorted({finding.code for finding in findings})
        raise ValueError(f"release privacy/integrity scan failed: {', '.join(codes)}")
    inventory = _inventory_payload(
        repo_root=root,
        release_paths=paths,
        version=version,
        source_commit=source_commit,
    )
    entries = {path: (root / Path(*PurePosixPath(path).parts)).read_bytes() for path in paths}
    entries["release-inventory.json"] = _canonical_json(inventory)

    with unique_sibling_temp(output_zip) as temporary:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for path in sorted(entries):
                archive.writestr(_zip_info(path), entries[path])
        zip_sha256 = _sha256_bytes(temporary.read_bytes())
        publish_file_no_replace(temporary, output_zip)

    return {
        "status": "ready",
        "version": version,
        "source_commit": source_commit,
        "file_count": len(paths),
        "inventory_sha256": inventory["inventory_sha256"],
        "zip_sha256": zip_sha256,
    }


def _git_output(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _git_bytes(repo_root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def materialize_committed_files(
    repo_root: Path,
    release_paths: Iterable[str],
    destination: Path,
    *,
    source_commit: str,
) -> None:
    root = repo_root.resolve()
    target_root = destination.resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    for raw_path in sorted(set(release_paths)):
        path = normalize_archive_path(raw_path)
        tree_output = _git_bytes(root, "ls-tree", "-z", source_commit, "--", path)
        tree_rows = [row for row in tree_output.split(b"\x00") if row]
        if not tree_rows:
            raise RuntimeError(f"release path is not present in {source_commit}: {path}")
        if len(tree_rows) != 1:
            raise RuntimeError(f"release path is ambiguous in {source_commit}: {path}")
        metadata_bytes, listed_path_bytes = tree_rows[0].split(b"\t", 1)
        metadata = metadata_bytes.decode("ascii")
        listed_path = listed_path_bytes.decode("utf-8")
        mode, object_type, _object_id = metadata.split()
        if listed_path != path or object_type != "blob":
            raise RuntimeError(f"release path is not a regular committed file: {path}")
        if mode == "120000":
            raise RuntimeError(f"release path is a symbolic link: {path}")
        data = _git_bytes(root, "show", f"{source_commit}:{path}")
        target = target_root / Path(*PurePosixPath(path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _assert_clean_worktree(repo_root: Path) -> None:
    status = _git_output(repo_root, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise RuntimeError("release build requires a clean worktree")


def build_release(repo_root: Path, output_zip: Path) -> dict[str, object]:
    root = repo_root.resolve()
    sidecar = output_zip.with_name(f"{output_zip.name}.sha256")
    if output_zip.exists() or sidecar.exists():
        raise FileExistsError("release output or SHA-256 sidecar already exists")
    source = capture_clean_release_source(root)
    source_commit = source.source_commit
    tracked = (
        _git_bytes(root, "ls-tree", "-r", "--name-only", "-z", source_commit)
        .decode("utf-8")
        .split("\x00")
    )
    release_paths = collect_release_paths(path for path in tracked if path)
    version = source.version
    expected_name = f"Auto-Cut-v{version}-windows-x64.zip"
    if output_zip.name != expected_name:
        raise ValueError("release ZIP filename does not match the committed version")
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="auto-cut-release-stage-", dir=output_zip.parent
    ) as temporary:
        staged_zip = Path(temporary) / output_zip.name
        with unique_sibling_temp(sidecar) as sidecar_temporary:
            staged_root = Path(temporary) / "Auto-Cut"
            materialize_committed_files(
                root,
                release_paths,
                staged_root,
                source_commit=source_commit,
            )
            capability_audit = _require_runtime_capability_audit(staged_root, release_paths)
            result = build_zip_from_paths(
                repo_root=staged_root,
                output_zip=staged_zip,
                release_paths=release_paths,
                version=version,
                source_commit=source_commit,
            )
            result["capability_audit"] = capability_audit
            sidecar_temporary.write_text(
                f"{result['zip_sha256']}  {output_zip.name}\n", encoding="ascii"
            )
            assert_release_source_unchanged(root, source)
            publish_files_no_replace(
                ((staged_zip, output_zip), (sidecar_temporary, sidecar))
            )
            return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a privacy-checked Auto-Cut ZIP.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--output", required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = build_release(Path(args.repo_root), Path(args.output))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        result = {
            "status": "failed",
            "code": "release_build_failed",
            "error_type": type(exc).__name__,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
