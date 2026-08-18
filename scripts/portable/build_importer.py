from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from portable_project.errors import PortableProjectError
from portable_project.manifest import IMPORTER_FILENAME

PYINSTALLER_VERSION = "6.15.0"
BUILD_VENV_NAME = ".venv-portable-importer"
LOCK_FILENAME = "requirements-portable-importer-build.lock"
CURRENT_POINTER_FILENAME = "current-build.json"
GENERATIONS_DIRNAME = "generations"
BUILD_RECEIPT_SCHEMA_VERSION = 2
_REPARSE_POINT_ATTRIBUTE = 0x400
_GENERATION_PATTERN = re.compile(r"[0-9a-f]{16}-[0-9a-f]{16}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _is_reparse_metadata(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _assert_regular_source_file(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PortableProjectError(
            "unsafe_importer_source", "Portable importer source escaped the repository"
        ) from exc
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise PortableProjectError(
                "importer_source_changed", "Portable importer source disappeared during capture"
            ) from exc
        if _is_reparse_metadata(metadata):
            raise PortableProjectError(
                "unsafe_importer_source", "Portable importer source contains a reparse point"
            )
    if not stat.S_ISREG(metadata.st_mode):
        raise PortableProjectError(
            "unsafe_importer_source", "Portable importer source is not a regular file"
        )


def _source_files(repo_root: Path) -> list[Path]:
    root = Path(repo_root).resolve()
    portable_project_root = root / "scripts" / "portable_project"
    candidates = [
        *(portable_project_root.rglob("*.py") if portable_project_root.is_dir() else ()),
        root / "scripts" / "portable_project_tool.py",
        root / "scripts" / "portable" / "__init__.py",
        root / "scripts" / "portable" / "build_importer.py",
        root / "scripts" / "utils" / "jianying_env.py",
        root / "scripts" / "utils" / "formatters.py",
        root / LOCK_FILENAME,
    ]
    files: dict[str, Path] = {}
    for candidate in candidates:
        path = Path(candidate).absolute()
        if not os.path.lexists(path):
            continue
        _assert_regular_source_file(root, path)
        relative = path.relative_to(root).as_posix()
        files[relative] = path
    return [files[key] for key in sorted(files)]


def compute_source_digest(repo_root: Path) -> str:
    root = Path(repo_root).resolve()
    digest = hashlib.sha256()
    files = _source_files(root)
    if not files:
        raise ValueError("portable importer source set is empty")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def capture_source_snapshot(repo_root: Path, snapshot_root: Path) -> str:
    root = Path(repo_root).resolve()
    snapshot = Path(snapshot_root).resolve(strict=False)
    if snapshot.exists() and any(snapshot.iterdir()):
        raise ValueError("portable importer source snapshot must start empty")
    snapshot.mkdir(parents=True, exist_ok=True)
    source_files = _source_files(root)
    if not source_files:
        raise ValueError("portable importer source set is empty")
    relative_paths = [path.relative_to(root) for path in source_files]
    for source, relative in zip(source_files, relative_paths, strict=True):
        before = source.stat()
        payload = source.read_bytes()
        after = source.stat()
        before_identity = (before.st_size, before.st_mtime_ns, before.st_ino)
        after_identity = (after.st_size, after.st_mtime_ns, after.st_ino)
        if before_identity != after_identity or len(payload) != after.st_size:
            raise PortableProjectError(
                "importer_source_changed",
                "Portable importer source changed during snapshot capture",
            )
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    final_relatives = [path.relative_to(root) for path in _source_files(root)]
    if final_relatives != relative_paths:
        raise PortableProjectError(
            "importer_source_changed",
            "Portable importer source set changed during snapshot capture",
        )
    return compute_source_digest(snapshot)


def assert_isolated_venv(repo_root: Path, venv_dir: Path) -> Path:
    root = Path(repo_root).resolve()
    candidate = Path(venv_dir).resolve(strict=False)
    forbidden = {
        root / ".venv",
        root / ".venv-audio",
        root / BUILD_VENV_NAME,
    }
    if candidate in forbidden or candidate.name != BUILD_VENV_NAME or candidate.exists():
        raise ValueError("portable importer builds require a fresh isolated temporary environment")
    return candidate


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value.casefold()) is not None


def inspect_pe_executable(executable: Path) -> dict[str, str]:
    path = Path(executable)
    try:
        with path.open("rb") as stream:
            dos_header = stream.read(64)
            if len(dos_header) < 64 or dos_header[:2] != b"MZ":
                raise ValueError
            pe_offset = int.from_bytes(dos_header[0x3C:0x40], "little")
            if pe_offset < 64 or pe_offset > 64 * 1024 * 1024:
                raise ValueError
            stream.seek(pe_offset)
            pe_header = stream.read(6)
    except (OSError, ValueError) as exc:
        raise PortableProjectError(
            "invalid_importer_executable", "Importer executable is not a valid Windows PE file"
        ) from exc
    if len(pe_header) != 6 or pe_header[:4] != b"PE\0\0":
        raise PortableProjectError(
            "invalid_importer_executable", "Importer executable is not a valid Windows PE file"
        )
    machine = int.from_bytes(pe_header[4:6], "little")
    if machine != 0x8664:
        raise PortableProjectError(
            "unsupported_build_architecture", "Portable importer PE must target Windows x64"
        )
    return {"platform": "windows", "architecture": "x86_64", "pe_machine": "0x8664"}


def _normalize_build_identity(build_identity: Mapping[str, Any]) -> dict[str, Any]:
    identity = json.loads(json.dumps(dict(build_identity), ensure_ascii=False))
    python_identity = identity.get("python")
    dependencies = identity.get("dependencies")
    if (
        not isinstance(python_identity, dict)
        or python_identity.get("implementation") != "CPython"
        or not str(python_identity.get("version") or "").strip()
        or python_identity.get("architecture") != "x86_64"
        or not _valid_sha256(python_identity.get("executable_sha256"))
        or not _valid_sha256(identity.get("lock_sha256"))
        or not isinstance(dependencies, list)
        or not dependencies
    ):
        raise ValueError("build_identity is incomplete")
    normalized_dependencies: list[dict[str, str]] = []
    for row in dependencies:
        if not isinstance(row, dict):
            raise ValueError("build dependency identity is invalid")
        name = str(row.get("name") or "").strip().casefold().replace("_", "-")
        version = str(row.get("version") or "").strip()
        if not name or not version:
            raise ValueError("build dependency identity is invalid")
        normalized_dependencies.append({"name": name, "version": version})
    if normalized_dependencies != sorted(
        normalized_dependencies, key=lambda row: (row["name"], row["version"])
    ) or len({row["name"] for row in normalized_dependencies}) != len(normalized_dependencies):
        raise ValueError("build dependency identity must be sorted and unique")
    identity["dependencies"] = normalized_dependencies
    return identity


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(dict(payload), stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_build_receipt(
    executable: Path,
    receipt_path: Path,
    *,
    source_digest: str,
    pyinstaller_version: str,
    self_check: Mapping[str, Any],
    build_identity: Mapping[str, Any],
) -> dict[str, Any]:
    artifact = Path(executable)
    digest = str(source_digest or "").casefold()
    if not _valid_sha256(digest):
        raise ValueError("source_digest must be a SHA-256 value")
    self_check_evidence = {
        "status": str(self_check.get("status") or ""),
        "code": str(self_check.get("code") or ""),
    }
    if self_check_evidence != {
        "status": "ready",
        "code": "importer_self_check_ready",
    }:
        raise ValueError("importer self-check evidence is not ready")
    canonical_self_check = json.dumps(
        self_check_evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    self_check_evidence["evidence_sha256"] = hashlib.sha256(canonical_self_check).hexdigest()
    pe_identity = inspect_pe_executable(artifact)
    identity = _normalize_build_identity(build_identity)
    payload = artifact.read_bytes()
    receipt = {
        "schema_version": BUILD_RECEIPT_SCHEMA_VERSION,
        "artifact_name": artifact.name,
        **pe_identity,
        "source_digest": digest,
        "pyinstaller_version": str(pyinstaller_version),
        "build_identity": identity,
        "self_check": self_check_evidence,
        "trust_scope": "integrity_only_not_code_signing",
        "code_signature": {
            "status": "not_verified",
            "authenticity_proven": False,
        },
        "byte_size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    _atomic_write_json(Path(receipt_path), receipt)
    return receipt


def validate_build_receipt(
    executable: Path,
    receipt: Mapping[str, Any],
    *,
    invalid_code: str = "importer_receipt_invalid",
    mismatch_code: str = "importer_hash_mismatch",
) -> dict[str, Any]:
    payload = dict(receipt)
    self_check = payload.get("self_check")
    code_signature = payload.get("code_signature")
    try:
        build_identity = _normalize_build_identity(payload.get("build_identity") or {})
    except (TypeError, ValueError) as exc:
        raise PortableProjectError(
            invalid_code, "Importer build receipt identity is incomplete"
        ) from exc
    if not isinstance(self_check, dict):
        raise PortableProjectError(invalid_code, "Importer self-check evidence is missing")
    self_check_base = {
        "status": self_check.get("status"),
        "code": self_check.get("code"),
    }
    expected_self_check_hash = hashlib.sha256(
        json.dumps(
            self_check_base, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    expected_signature = {"status": "not_verified", "authenticity_proven": False}
    if (
        payload.get("schema_version") != BUILD_RECEIPT_SCHEMA_VERSION
        or payload.get("artifact_name") != IMPORTER_FILENAME
        or payload.get("platform") != "windows"
        or payload.get("architecture") != "x86_64"
        or payload.get("pe_machine") != "0x8664"
        or not _valid_sha256(payload.get("source_digest"))
        or not str(payload.get("pyinstaller_version") or "").strip()
        or self_check_base != {"status": "ready", "code": "importer_self_check_ready"}
        or self_check.get("evidence_sha256") != expected_self_check_hash
        or payload.get("trust_scope") != "integrity_only_not_code_signing"
        or code_signature != expected_signature
        or type(payload.get("byte_size")) is not int
        or payload["byte_size"] < 0
        or not _valid_sha256(payload.get("sha256"))
    ):
        raise PortableProjectError(invalid_code, "Importer build receipt is incomplete")
    try:
        pe_identity = inspect_pe_executable(executable)
    except PortableProjectError as exc:
        raise PortableProjectError(
            invalid_code, "Importer executable platform identity is invalid"
        ) from exc
    artifact = Path(executable)
    if (
        artifact.stat().st_size != payload["byte_size"]
        or hashlib.sha256(artifact.read_bytes()).hexdigest() != payload["sha256"]
    ):
        raise PortableProjectError(
            mismatch_code, "Importer executable does not match its build receipt"
        )
    return {
        "schema_version": BUILD_RECEIPT_SCHEMA_VERSION,
        "artifact_name": IMPORTER_FILENAME,
        **pe_identity,
        "source_digest": str(payload["source_digest"]).casefold(),
        "pyinstaller_version": str(payload["pyinstaller_version"]),
        "build_identity": build_identity,
        "self_check": {
            "status": "ready",
            "code": "importer_self_check_ready",
            "evidence_sha256": expected_self_check_hash,
        },
        "trust_scope": "integrity_only_not_code_signing",
        "code_signature": expected_signature,
        "byte_size": payload["byte_size"],
        "sha256": str(payload["sha256"]).casefold(),
    }


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableProjectError(
            "importer_build_failed", "Importer build receipt is not readable"
        ) from exc
    if not isinstance(payload, dict):
        raise PortableProjectError("importer_build_failed", "Importer build receipt is invalid")
    return payload


def _validate_receipt_pair(executable: Path, receipt_path: Path) -> dict[str, Any]:
    receipt = _load_receipt(receipt_path)
    return validate_build_receipt(
        executable,
        receipt,
        invalid_code="importer_build_failed",
        mismatch_code="importer_build_failed",
    )


@contextmanager
def _exclusive_build_lock(output_dir: Path) -> Iterator[None]:
    lock_path = Path(output_dir) / ".portable-importer-build.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - the builder is Windows-only
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def resolve_current_build(output_dir: Path) -> tuple[Path, Path]:
    output = Path(output_dir).resolve()
    pointer_path = output / CURRENT_POINTER_FILENAME
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableProjectError(
            "importer_build_failed", "Current importer build pointer is invalid"
        ) from exc
    if not isinstance(pointer, dict) or pointer.get("schema_version") != 1:
        raise PortableProjectError(
            "importer_build_failed", "Current importer build pointer is invalid"
        )
    generation_name = str(pointer.get("generation") or "")
    if _GENERATION_PATTERN.fullmatch(generation_name) is None:
        raise PortableProjectError("importer_build_failed", "Current importer generation is unsafe")
    generation = output / GENERATIONS_DIRNAME / generation_name
    executable = generation / IMPORTER_FILENAME
    receipt = generation / "build-receipt.json"
    if not executable.is_file() or not receipt.is_file():
        raise PortableProjectError(
            "importer_build_failed", "Current importer generation is incomplete"
        )
    if (
        pointer.get("artifact") != IMPORTER_FILENAME
        or pointer.get("receipt") != "build-receipt.json"
        or not _valid_sha256(pointer.get("receipt_sha256"))
        or hashlib.sha256(receipt.read_bytes()).hexdigest() != pointer["receipt_sha256"]
    ):
        raise PortableProjectError(
            "importer_build_failed", "Current importer pointer does not match"
        )
    receipt_payload = _validate_receipt_pair(executable, receipt)
    expected_generation_name = (
        f"{str(receipt_payload['source_digest'])[:16]}-" f"{str(receipt_payload['sha256'])[:16]}"
    )
    if generation_name != expected_generation_name:
        raise PortableProjectError(
            "importer_build_failed",
            "Current importer generation name does not match its content identity",
        )
    return executable, receipt


def _promote_build_pair(
    staged_artifact: Path,
    staged_receipt: Path,
    output_dir: Path,
) -> tuple[Path, Path]:
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    receipt_payload = _validate_receipt_pair(staged_artifact, staged_receipt)
    generation_name = (
        f"{str(receipt_payload['source_digest'])[:16]}-{str(receipt_payload['sha256'])[:16]}"
    )
    if _GENERATION_PATTERN.fullmatch(generation_name) is None:
        raise PortableProjectError(
            "importer_build_failed", "Importer generation identity is invalid"
        )
    generations = output / GENERATIONS_DIRNAME
    generations.mkdir(parents=True, exist_ok=True)
    generation = generations / generation_name
    created_generation = False
    temporary_generation: Path | None = None
    temporary_pointer: Path | None = None
    with _exclusive_build_lock(output):
        pointer_path = output / CURRENT_POINTER_FILENAME
        try:
            previous_pointer = pointer_path.read_bytes()
        except FileNotFoundError:
            previous_pointer = None
        except OSError as exc:
            raise PortableProjectError(
                "importer_build_failed", "Previous importer build pointer is not readable"
            ) from exc
        pointer_switched = False
        try:
            if generation.exists():
                existing_artifact = generation / IMPORTER_FILENAME
                existing_receipt = generation / "build-receipt.json"
                existing = _validate_receipt_pair(existing_artifact, existing_receipt)
                if existing != receipt_payload:
                    raise PortableProjectError(
                        "importer_build_failed", "Importer generation identity collided"
                    )
            else:
                temporary_generation = Path(
                    tempfile.mkdtemp(prefix=".generation-", dir=generations)
                )
                os.replace(staged_artifact, temporary_generation / IMPORTER_FILENAME)
                os.replace(staged_receipt, temporary_generation / "build-receipt.json")
                _validate_receipt_pair(
                    temporary_generation / IMPORTER_FILENAME,
                    temporary_generation / "build-receipt.json",
                )
                os.replace(temporary_generation, generation)
                temporary_generation = None
                created_generation = True

            receipt = generation / "build-receipt.json"
            pointer = {
                "schema_version": 1,
                "generation": generation_name,
                "artifact": IMPORTER_FILENAME,
                "receipt": "build-receipt.json",
                "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            }
            descriptor, pointer_name = tempfile.mkstemp(prefix=".current-build-", dir=output)
            temporary_pointer = Path(pointer_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(pointer, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_pointer, pointer_path)
            temporary_pointer = None
            pointer_switched = True
            current = resolve_current_build(output)
            expected = (generation / IMPORTER_FILENAME, receipt)
            if current != expected:
                raise PortableProjectError(
                    "importer_build_failed", "Published importer generation is inconsistent"
                )
            return current
        except BaseException as exc:
            rollback_error: OSError | None = None
            if pointer_switched:
                try:
                    if previous_pointer is None:
                        pointer_path.unlink(missing_ok=True)
                    else:
                        _atomic_write_bytes(pointer_path, previous_pointer)
                except OSError as caught:
                    rollback_error = caught
            if created_generation:
                shutil.rmtree(generation, ignore_errors=True)
            if rollback_error is not None:
                raise PortableProjectError(
                    "importer_build_failed",
                    "Failed importer publication could not restore the previous build pointer",
                ) from exc
            if isinstance(exc, OSError):
                raise PortableProjectError(
                    "importer_build_failed", "Verified importer generation could not be published"
                ) from exc
            raise
        finally:
            if temporary_generation is not None:
                shutil.rmtree(temporary_generation, ignore_errors=True)
            if temporary_pointer is not None:
                temporary_pointer.unlink(missing_ok=True)


def _run(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    cwd: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    output_options: dict[str, Any]
    if capture_output:
        output_options = {"capture_output": True}
    else:
        output_options = {"stdout": sys.stderr}
    try:
        return runner(
            [str(value) for value in command],
            cwd=str(cwd),
            check=True,
            text=True,
            **output_options,
        )
    except subprocess.CalledProcessError as exc:
        raise PortableProjectError(
            "importer_build_failed", "A portable importer build subprocess failed"
        ) from exc


def pip_install_command(venv_python: Path, lock: Path) -> list[str]:
    return [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--require-hashes",
        "--no-deps",
        "-r",
        str(lock),
        "--timeout",
        "120",
        "--retries",
        "8",
    ]


def assert_python_x64(
    python_executable: Path,
    *,
    runner: CommandRunner = subprocess.run,
    cwd: Path,
) -> None:
    result = _run(
        runner,
        [str(python_executable), "-c", "import struct; print(struct.calcsize('P') * 8)"],
        cwd=cwd,
        capture_output=True,
    )
    if str(result.stdout or "").strip() != "64":
        raise PortableProjectError(
            "unsupported_build_architecture",
            "Portable importer requires a 64-bit Python interpreter",
        )


def _verify_windows_x64() -> None:
    machine = platform.machine().casefold()
    if platform.system() != "Windows" or machine not in {"amd64", "x86_64"}:
        raise PortableProjectError(
            "unsupported_build_platform", "Importer EXE must be built on Windows x64"
        )


def _locked_dependencies(lock: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    pattern = re.compile(r"(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s]+)(?P<rest>.*)")
    for raw_line in Path(lock).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if match is None or "--hash=sha256:" not in match.group("rest"):
            raise PortableProjectError(
                "importer_build_failed", "Portable importer lock is not fully hash-pinned"
            )
        rows.append(
            {
                "name": match.group("name").casefold().replace("_", "-"),
                "version": match.group("version"),
            }
        )
    rows.sort(key=lambda row: (row["name"], row["version"]))
    if not rows or len({row["name"] for row in rows}) != len(rows):
        raise PortableProjectError(
            "importer_build_failed", "Portable importer lock dependency identity is invalid"
        )
    return rows


def _inspect_installed_dependencies(
    python_executable: Path,
    expected: list[dict[str, str]],
    *,
    runner: CommandRunner,
    cwd: Path,
) -> list[dict[str, str]]:
    names = [row["name"] for row in expected]
    script = (
        "import importlib.metadata as m,json;"
        f"names={names!r};"
        "print(json.dumps([{'name':n,'version':m.version(n)} for n in names],sort_keys=True))"
    )
    result = _run(
        runner,
        [str(python_executable), "-c", script],
        cwd=cwd,
        capture_output=True,
    )
    try:
        rows = json.loads(str(result.stdout or ""))
    except json.JSONDecodeError as exc:
        raise PortableProjectError(
            "importer_build_failed", "Installed build dependency identity is unavailable"
        ) from exc
    if rows != expected:
        raise PortableProjectError(
            "importer_build_failed", "Installed build dependencies do not match the hash lock"
        )
    return rows


def _inspect_python_identity(
    python_executable: Path,
    *,
    runner: CommandRunner,
    cwd: Path,
) -> dict[str, str]:
    script = (
        "import json,platform,struct;"
        "print(json.dumps({'implementation':platform.python_implementation(),"
        "'version':platform.python_version(),"
        "'architecture':'x86_64' if struct.calcsize('P')*8==64 else 'x86'},sort_keys=True))"
    )
    result = _run(
        runner,
        [str(python_executable), "-c", script],
        cwd=cwd,
        capture_output=True,
    )
    try:
        identity = json.loads(str(result.stdout or ""))
    except json.JSONDecodeError as exc:
        raise PortableProjectError(
            "importer_build_failed", "Build interpreter identity is unavailable"
        ) from exc
    if not isinstance(identity, dict) or identity.get("architecture") != "x86_64":
        raise PortableProjectError(
            "unsupported_build_architecture", "Build interpreter identity is not Windows x64"
        )
    identity["executable_sha256"] = hashlib.sha256(Path(python_executable).read_bytes()).hexdigest()
    return identity


def build_importer(
    repo_root: Path,
    *,
    output_dir: Path | None = None,
    python_executable: Path | str | None = None,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    _verify_windows_x64()
    root = Path(repo_root).resolve()
    output = Path(output_dir or root / "tmp" / "portable-importer").resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    base_python = Path(python_executable or sys.executable).resolve()
    assert_python_x64(base_python, runner=runner, cwd=root)
    workspace = Path(tempfile.mkdtemp(prefix=".autocut-importer-build-", dir=output))
    try:
        source_root = workspace / "source"
        source_digest = capture_source_snapshot(root, source_root)
        lock = source_root / LOCK_FILENAME
        if not lock.is_file():
            raise PortableProjectError(
                "importer_build_failed", "Portable importer build lock is missing"
            )
        expected_dependencies = _locked_dependencies(lock)
        venv_dir = assert_isolated_venv(root, workspace / BUILD_VENV_NAME)
        _run(runner, [str(base_python), "-m", "venv", str(venv_dir)], cwd=source_root)
        venv_python = venv_dir / "Scripts" / "python.exe"
        if not venv_python.is_file():
            raise PortableProjectError(
                "importer_build_failed", "Fresh portable importer environment was not created"
            )
        assert_python_x64(venv_python, runner=runner, cwd=source_root)
        _run(runner, pip_install_command(venv_python, lock), cwd=source_root)
        version_result = _run(
            runner,
            [str(venv_python), "-m", "PyInstaller", "--version"],
            cwd=source_root,
            capture_output=True,
        )
        actual_version = str(version_result.stdout or "").strip()
        if actual_version != PYINSTALLER_VERSION:
            raise PortableProjectError(
                "importer_build_failed", "Isolated PyInstaller version does not match the lock"
            )
        installed_dependencies = _inspect_installed_dependencies(
            venv_python,
            expected_dependencies,
            runner=runner,
            cwd=source_root,
        )
        python_identity = _inspect_python_identity(venv_python, runner=runner, cwd=source_root)
        build_identity = {
            "python": python_identity,
            "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
            "dependencies": installed_dependencies,
        }

        build_root = workspace / "pyinstaller"
        dist_dir = build_root / "dist"
        work_dir = build_root / "work"
        spec_dir = build_root / "spec"
        dist_dir.mkdir(parents=True)
        work_dir.mkdir(parents=True)
        spec_dir.mkdir(parents=True)
        entrypoint = source_root / "scripts" / "portable_project_tool.py"
        _run(
            runner,
            [
                str(venv_python),
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--onefile",
                "--windowed",
                "--name",
                Path(IMPORTER_FILENAME).stem,
                "--distpath",
                str(dist_dir),
                "--workpath",
                str(work_dir),
                "--specpath",
                str(spec_dir),
                "--paths",
                str(source_root / "scripts"),
                str(entrypoint),
            ],
            cwd=source_root,
        )
        built = dist_dir / IMPORTER_FILENAME
        if not built.is_file():
            raise PortableProjectError(
                "importer_build_failed", "PyInstaller did not produce the expected executable"
            )
        inspect_pe_executable(built)
        self_check_path = build_root / "self-check.json"
        _run(
            runner,
            [
                str(built),
                "--self-check",
                "--self-check-output",
                str(self_check_path),
            ],
            cwd=source_root,
        )
        try:
            self_check_payload = json.loads(self_check_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PortableProjectError(
                "importer_build_failed", "Importer self-check did not return JSON"
            ) from exc
        if self_check_payload != {
            "status": "ready",
            "code": "importer_self_check_ready",
        }:
            raise PortableProjectError(
                "importer_build_failed", "Importer executable self-check failed"
            )
        if compute_source_digest(source_root) != source_digest:
            raise PortableProjectError(
                "importer_source_changed", "Portable importer source snapshot changed during build"
            )

        publish_dir = workspace / "publish"
        publish_dir.mkdir()
        staged_artifact = publish_dir / IMPORTER_FILENAME
        staged_receipt = publish_dir / "build-receipt.json"
        shutil.copy2(built, staged_artifact)
        write_build_receipt(
            staged_artifact,
            staged_receipt,
            source_digest=source_digest,
            pyinstaller_version=actual_version,
            self_check=self_check_payload,
            build_identity=build_identity,
        )
        artifact, receipt_path = _promote_build_pair(staged_artifact, staged_receipt, output)
        published_receipt = _validate_receipt_pair(artifact, receipt_path)
        return {
            "status": "importer_built",
            "executable_path": str(artifact),
            "receipt_path": str(receipt_path),
            "current_pointer_path": str(output / CURRENT_POINTER_FILENAME),
            **published_receipt,
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
