from __future__ import annotations

import hashlib
import importlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

import pytest


def _offline_bundle_module():
    try:
        return importlib.import_module("scripts.release.offline_bundle")
    except ModuleNotFoundError:
        pytest.fail("scripts.release.offline_bundle is not implemented")


def _offline_builder_module():
    try:
        return importlib.import_module("scripts.release.build_offline_deps")
    except ModuleNotFoundError:
        pytest.fail("scripts.release.build_offline_deps is not implemented")


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _manifest(files: dict[str, bytes]) -> dict[str, object]:
    rows = []
    for path, data in sorted(files.items()):
        rows.append(
            {
                "path": path,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "component": "test",
                "version": "1.0",
                "platform": "win_amd64",
                "license": "MIT",
                "source": "https://example.invalid/artifact/1.0",
            }
        )
    payload: dict[str, object] = {
        "schema_version": 1,
        "release_version": "1.7.0",
        "source_commit": "a" * 40,
        "target": {
            "os": "windows",
            "arch": "x64",
            "python_implementation": "cpython",
            "python_version": "3.11",
            "abi": "cp311",
        },
        "components": {
            "main_wheelhouse": {"included": True},
            "audio_wheelhouse": {"included": True},
            "playwright_chromium": {"included": True},
            "ffmpeg": {"included": True},
            "python_installer": {"included": False},
        },
        "files": rows,
    }
    payload["manifest_sha256"] = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return payload


def _write_bundle(path: Path, files: dict[str, bytes], manifest: dict[str, object]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
        archive.writestr("offline-deps-manifest.json", _canonical_json(manifest))


def test_verify_offline_bundle_accepts_exact_windows_cp311_payload(tmp_path: Path) -> None:
    module = _offline_bundle_module()
    files = {
        "wheelhouse/main/demo-1.0-py3-none-any.whl": b"wheel",
        "browsers/chromium-1169/chrome-win/chrome.exe": b"browser",
        "tools/ffmpeg/bin/ffmpeg.exe": b"ffmpeg",
    }
    bundle = tmp_path / "offline.zip"
    _write_bundle(bundle, files, _manifest(files))

    result = module.verify_offline_bundle(bundle, expected_version="1.7.0")

    assert result["status"] == "ready"
    assert result["manifest"]["target"]["python_version"] == "3.11"
    assert result["file_count"] == 3


def test_verify_offline_bundle_rejects_payload_hash_tampering(tmp_path: Path) -> None:
    module = _offline_bundle_module()
    files = {"wheelhouse/main/demo.whl": b"original"}
    bundle = tmp_path / "tampered.zip"
    _write_bundle(bundle, {"wheelhouse/main/demo.whl": b"modified"}, _manifest(files))

    with pytest.raises(ValueError, match="hash"):
        module.verify_offline_bundle(bundle, expected_version="1.7.0")


@pytest.mark.parametrize("field", ["version", "platform", "license", "source"])
def test_verify_offline_bundle_requires_complete_per_file_metadata(
    tmp_path: Path, field: str
) -> None:
    module = _offline_bundle_module()
    files = {"wheelhouse/main/demo.whl": b"wheel"}
    manifest = _manifest(files)
    del manifest["files"][0][field]
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    bundle = tmp_path / "missing-metadata.zip"
    _write_bundle(bundle, files, manifest)

    with pytest.raises(ValueError, match="metadata"):
        module.verify_offline_bundle(bundle, expected_version="1.7.0")


@pytest.mark.parametrize("location", ["manifest", "file"])
def test_verify_offline_bundle_rejects_schema_extra_properties(
    tmp_path: Path, location: str
) -> None:
    module = _offline_bundle_module()
    files = {"wheelhouse/main/demo.whl": b"wheel"}
    manifest = _manifest(files)
    if location == "manifest":
        manifest["unexpected"] = "not allowed"
    else:
        manifest["files"][0]["unexpected"] = "not allowed"
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    bundle = tmp_path / "schema-extra.zip"
    _write_bundle(bundle, files, manifest)

    with pytest.raises(ValueError, match="fields|metadata"):
        module.verify_offline_bundle(bundle, expected_version="1.7.0")


def test_verify_offline_bundle_rejects_wrong_target_identity(tmp_path: Path) -> None:
    module = _offline_bundle_module()
    files = {"wheelhouse/main/demo.whl": b"wheel"}
    manifest = _manifest(files)
    manifest["target"]["arch"] = "arm64"
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    bundle = tmp_path / "wrong-target.zip"
    _write_bundle(bundle, files, manifest)

    with pytest.raises(ValueError, match="target"):
        module.verify_offline_bundle(bundle, expected_version="1.7.0")


def test_write_offline_bundle_refuses_existing_output(tmp_path: Path) -> None:
    module = _offline_bundle_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "payload.bin").write_bytes(b"payload")
    output = tmp_path / "offline.zip"
    output.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="already exists"):
        module.write_offline_bundle(
            staging,
            output,
            release_version="1.7.0",
            source_commit="a" * 40,
            components={"test": {"included": True}},
            file_metadata={
                "payload.bin": {
                    "component": "test",
                    "version": "1.0",
                    "platform": "win_amd64",
                    "license": "MIT",
                    "source": "https://example.invalid/artifact/1.0",
                }
            },
        )


def test_write_offline_bundle_is_verifiable_and_deterministic(tmp_path: Path) -> None:
    module = _offline_bundle_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "payload.bin").write_bytes(b"payload")
    metadata = {
        "payload.bin": {
            "component": "test",
            "version": "1.0",
            "platform": "win_amd64",
            "license": "MIT",
            "source": "https://example.invalid/artifact/1.0",
        }
    }
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    first_result = module.write_offline_bundle(
        staging,
        first,
        release_version="1.7.0",
        source_commit="a" * 40,
        components={"test": {"included": True}},
        file_metadata=metadata,
    )
    second_result = module.write_offline_bundle(
        staging,
        second,
        release_version="1.7.0",
        source_commit="a" * 40,
        components={"test": {"included": True}},
        file_metadata=metadata,
    )

    assert first_result["status"] == "ready"
    assert first_result["zip_sha256"] == second_result["zip_sha256"]
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert {info.compress_type for info in archive.infolist()} == {zipfile.ZIP_STORED}
        assert {info.extract_version for info in archive.infolist()} == {20}


def test_write_offline_bundle_rejects_private_text_in_generated_manifest(
    tmp_path: Path,
) -> None:
    module = _offline_bundle_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "payload.bin").write_bytes(b"payload")
    output = tmp_path / "offline.zip"
    machine_path = chr(92).join(("C:", "Users", "private-user", "license-cache"))

    with pytest.raises(ValueError, match="manifest privacy"):
        module.write_offline_bundle(
            staging,
            output,
            release_version="1.7.0",
            source_commit="a" * 40,
            components={"test": {"included": True}},
            file_metadata={
                "payload.bin": {
                    "component": "test",
                    "version": "1.0",
                    "platform": "win_amd64",
                    "license": machine_path,
                    "source": "https://example.invalid/artifact/1.0",
                }
            },
        )

    assert not output.exists()


def test_write_offline_bundle_removes_promoted_output_when_self_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _offline_bundle_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "payload.bin").write_bytes(b"payload")
    output = tmp_path / "offline.zip"

    def fail_verification(*_args, **_kwargs):
        raise ValueError("synthetic verification failure")

    monkeypatch.setattr(module, "verify_offline_bundle", fail_verification)

    with pytest.raises(ValueError, match="synthetic verification failure"):
        module.write_offline_bundle(
            staging,
            output,
            release_version="1.7.0",
            source_commit="a" * 40,
            components={"test": {"included": True}},
            file_metadata={
                "payload.bin": {
                    "component": "test",
                    "version": "1.0",
                    "platform": "win_amd64",
                    "license": "MIT",
                    "source": "https://example.invalid/artifact/1.0",
                }
            },
        )

    assert not output.exists()


def test_write_offline_bundle_preserves_output_created_immediately_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _offline_bundle_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "payload.bin").write_bytes(b"payload")
    output = tmp_path / "offline.zip"
    sentinel = b"other process"
    metadata = {
        "payload.bin": {
            "component": "test",
            "version": "1.0",
            "platform": "win_amd64",
            "license": "MIT",
            "source": "https://example.invalid/artifact/1.0",
        }
    }
    real_close = zipfile.ZipFile.close
    injected = False

    def close_then_race(archive: zipfile.ZipFile) -> None:
        nonlocal injected
        filename = Path(archive.filename) if archive.filename is not None else None
        real_close(archive)
        if (
            not injected
            and filename is not None
            and filename.parent == output.parent
            and filename != output
            and filename.name.startswith(f".{output.name}.")
        ):
            output.write_bytes(sentinel)
            injected = True

    monkeypatch.setattr(zipfile.ZipFile, "close", close_then_race)

    with pytest.raises(FileExistsError):
        module.write_offline_bundle(
            staging,
            output,
            release_version="1.7.0",
            source_commit="a" * 40,
            components={"test": {"included": True}},
            file_metadata=metadata,
        )

    assert injected is True
    assert output.read_bytes() == sentinel


def test_extract_offline_bundle_rejects_traversal_entry(tmp_path: Path) -> None:
    module = _offline_bundle_module()
    bundle = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("../escape", b"unsafe")

    with pytest.raises(ValueError, match="unsafe"):
        module.extract_offline_bundle(bundle, tmp_path / "extract")


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "NUL",
        "tools/COM1.txt",
        "tools/runtime:stream",
        "tools/trailing.",
        "tools/trailing ",
    ),
)
def test_offline_bundle_rejects_windows_unsafe_paths(tmp_path: Path, unsafe_path: str) -> None:
    module = _offline_bundle_module()
    bundle = tmp_path / "unsafe-windows.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(unsafe_path, b"unsafe")

    with pytest.raises(ValueError, match="unsafe"):
        module.verify_offline_bundle(bundle)


def test_offline_manifest_schema_rejects_windows_unsafe_paths() -> None:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1] / "schemas/offline-deps-manifest.schema.json"
        ).read_text(encoding="utf-8")
    )
    pattern = re.compile(schema["properties"]["files"]["items"]["properties"]["path"]["pattern"])

    for unsafe_path in (
        "NUL",
        "tools/COM1.txt",
        "tools/trailing.",
        "tools/trailing ",
        'tools/bad"name.txt',
        "tools/bad<name.txt",
        "tools/bad>name.txt",
        "tools/bad|name.txt",
        "tools/bad?name.txt",
        "tools/bad*name.txt",
        f"tools/bad{chr(1)}name.txt",
        f"tools/bad{chr(31)}name.txt",
    ):
        assert pattern.fullmatch(unsafe_path) is None


def test_offline_bundle_rejects_case_colliding_paths(tmp_path: Path) -> None:
    module = _offline_bundle_module()
    files = {
        "tools/Probe.exe": b"first",
        "tools/probe.exe": b"second",
    }
    bundle = tmp_path / "case-collision.zip"
    _write_bundle(bundle, files, _manifest(files))

    with pytest.raises(ValueError, match="duplicate|collide"):
        module.verify_offline_bundle(bundle)


def test_pip_download_command_targets_windows_cp311_without_cache(tmp_path: Path) -> None:
    module = _offline_builder_module()
    command = module.pip_download_command(
        "python.exe",
        tmp_path / "wheelhouse",
        [tmp_path / "requirements.txt"],
    )

    assert command[:4] == ["python.exe", "-I", "-m", "pip"]
    assert "download" in command
    assert "--isolated" in command
    assert "--no-cache-dir" in command
    assert command[command.index("--platform") + 1] == "win_amd64"
    assert command[command.index("--implementation") + 1] == "cp"
    assert command[command.index("--python-version") + 1] == "3.11"
    assert command[command.index("--abi") + 1] == "cp311"
    assert "--only-binary=:all:" in command
    assert "--require-hashes" in command


def test_pip_download_command_can_resolve_a_verified_local_wheel(tmp_path: Path) -> None:
    module = _offline_builder_module()
    wheelhouse = tmp_path / "wheelhouse"
    command = module.pip_download_command(
        "python.exe",
        wheelhouse,
        [tmp_path / "requirements-audio.lock"],
        find_links=[wheelhouse],
    )

    assert command[command.index("--find-links") + 1] == str(wheelhouse)
    assert "--only-binary=:all:" in command
    assert "--require-hashes" in command


def _write_test_wheel(path: Path, *, name: str, version: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA",
            ("Metadata-Version: 2.1\n" f"Name: {name}\n" f"Version: {version}\n" "License: MIT\n"),
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_binding(prefix: str, files: dict[str, bytes]) -> dict[str, object]:
    rows = [
        {
            "path": path,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for path, data in sorted(files.items())
    ]
    canonical = (json.dumps(rows, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return {
        f"{prefix}_tree_file_count": len(rows),
        f"{prefix}_tree_total_size": sum(int(row["size"]) for row in rows),
        f"{prefix}_tree_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def test_required_release_inputs_include_compiled_target_locks() -> None:
    module = _offline_builder_module()

    assert "requirements-offline-main.lock" in module.REQUIREMENT_FILES
    assert "requirements-offline-audio.lock" in module.REQUIREMENT_FILES


def test_validate_direct_pin_parity_accepts_matching_name_and_version(
    tmp_path: Path,
) -> None:
    module = _offline_builder_module()
    direct = tmp_path / "requirements.txt"
    compiled = tmp_path / "requirements-offline-main.lock"
    direct.write_text("Demo_Package==1.2\n", encoding="utf-8")
    compiled.write_text(
        f"demo-package==1.2 --hash=sha256:{'a' * 64}\n",
        encoding="utf-8",
    )

    assert module.validate_direct_pin_parity(direct, compiled) == {"direct_pin_count": 1}


@pytest.mark.parametrize(
    "compiled_requirement",
    (
        "other-package==1.2",
        "demo-package==9.9",
    ),
)
def test_validate_direct_pin_parity_rejects_missing_or_mismatched_pin(
    tmp_path: Path, compiled_requirement: str
) -> None:
    module = _offline_builder_module()
    direct = tmp_path / "requirements.txt"
    compiled = tmp_path / "requirements-offline-main.lock"
    direct.write_text("demo-package==1.2\n", encoding="utf-8")
    compiled.write_text(
        f"{compiled_requirement} --hash=sha256:{'a' * 64}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="direct pin parity"):
        module.validate_direct_pin_parity(direct, compiled)


def test_copy_requirement_inputs_reads_committed_blob_bytes(tmp_path: Path) -> None:
    module = _offline_builder_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo_root, check=True)
    committed_rows = {
        "requirements.txt": "demo==1.0\n",
        "requirements-offline-main.lock": (f"demo==1.0 --hash=sha256:{'a' * 64}\n"),
        "requirements-audio.lock": "audio-demo==2.0\n",
        "requirements-offline-audio.lock": (f"audio-demo==2.0 --hash=sha256:{'b' * 64}\n"),
        "requirements-audio-build.lock": (f"build-demo==3.0 --hash=sha256:{'c' * 64}\n"),
        "requirements-offline-acceptance.lock": (f"accept-demo==4.0 --hash=sha256:{'d' * 64}\n"),
        "requirements-offline-bootstrap.lock": (f"pip==26.1.2 --hash=sha256:{'e' * 64}\n"),
        "scripts/release/offline_sources.json": '{"schema_version": 1}\n',
    }
    for relative, content in committed_rows.items():
        path = repo_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Auto-Cut Test",
            "-c",
            "user.email=auto-cut-test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        cwd=repo_root,
        check=True,
    )
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    (repo_root / "requirements.txt").write_text("demo==9.9\n", encoding="utf-8", newline="\n")
    (repo_root / "scripts/release/offline_sources.json").write_text(
        '{"schema_version": 999}\n', encoding="utf-8", newline="\n"
    )
    staging = tmp_path / "payload"

    module._copy_requirement_inputs(repo_root, staging, source_commit)

    assert (staging / "requirements/requirements.txt").read_bytes() == committed_rows[
        "requirements.txt"
    ].encode("utf-8")
    provenance = json.loads(
        (staging / "provenance/offline-sources.json").read_text(encoding="utf-8")
    )
    assert provenance == {"schema_version": 1}


def test_validate_wheelhouse_lock_closure_accepts_exact_hashed_artifacts(
    tmp_path: Path,
) -> None:
    module = _offline_builder_module()
    wheelhouse = tmp_path / "wheelhouse"
    digest = _write_test_wheel(
        wheelhouse / "Demo_Package-1.2-py3-none-any.whl",
        name="Demo-Package",
        version="1.2",
    )
    lock = tmp_path / "requirements-offline-main.lock"
    lock.write_text(
        f"demo-package==1.2 --hash=sha256:{digest}\n",
        encoding="utf-8",
    )

    result = module.validate_wheelhouse_lock_closure(wheelhouse, [lock])

    assert result == {"lock_package_count": 1, "wheel_count": 1}


def test_validate_wheelhouse_lock_closure_does_not_require_license_identity(
    tmp_path: Path,
) -> None:
    module = _offline_builder_module()
    wheelhouse = tmp_path / "wheelhouse"
    wheel = wheelhouse / "demo-1.2-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "demo-1.2.dist-info/METADATA",
            (
                "Metadata-Version: 2.4\n"
                "Name: demo\n"
                "Version: 1.2\n"
                "License-File: LICENSE.txt\n"
            ),
        )
        archive.writestr("demo-1.2.dist-info/licenses/LICENSE.txt", "MIT\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = tmp_path / "requirements-offline-audio.lock"
    lock.write_text(
        f"demo==1.2 --hash=sha256:{digest}\n",
        encoding="utf-8",
    )

    result = module.validate_wheelhouse_lock_closure(wheelhouse, [lock])

    assert result == {"lock_package_count": 1, "wheel_count": 1}


@pytest.mark.parametrize("case", ["missing", "extra", "hash"])
def test_validate_wheelhouse_lock_closure_rejects_non_exact_payload(
    tmp_path: Path, case: str
) -> None:
    module = _offline_builder_module()
    wheelhouse = tmp_path / "wheelhouse"
    digest = _write_test_wheel(
        wheelhouse / "demo-1.0-py3-none-any.whl",
        name="demo",
        version="1.0",
    )
    lock = tmp_path / "requirements-offline-main.lock"
    if case == "missing":
        (wheelhouse / "demo-1.0-py3-none-any.whl").unlink()
        expected_hash = digest
    elif case == "extra":
        _write_test_wheel(
            wheelhouse / "extra-2.0-py3-none-any.whl",
            name="extra",
            version="2.0",
        )
        expected_hash = digest
    else:
        expected_hash = "0" * 64
    lock.write_text(
        f"demo==1.0 --hash=sha256:{expected_hash}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="closure|hash"):
        module.validate_wheelhouse_lock_closure(wheelhouse, [lock])


def test_prepare_build_runtime_pins_pip_in_a_fresh_temporary_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _offline_builder_module()
    repo_root = tmp_path / "repo"
    staging = tmp_path / "payload"
    requirements = staging / "requirements"
    requirements.mkdir(parents=True)
    bootstrap_lock = requirements / "requirements-offline-bootstrap.lock"
    bootstrap_lock.write_text(
        f"pip==26.1.2 --hash=sha256:{'a' * 64}\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        command = [str(part) for part in command]
        commands.append(command)
        if command[-3:-1] == ["-m", "venv"]:
            build_python = Path(command[-1]) / "Scripts" / "python.exe"
            build_python.parent.mkdir(parents=True, exist_ok=True)
            build_python.write_bytes(b"python")
        if command[-2:] == ["pip", "--version"]:
            return "pip 26.1.2 from temporary-runtime\n"
        return ""

    monkeypatch.setattr(module, "_run", fake_run)

    build_python = module._prepare_build_runtime(repo_root, staging)

    assert build_python == staging.parent / "dependency-build-runtime" / "Scripts" / "python.exe"
    assert commands[0] == [
        module.sys.executable,
        "-I",
        "-m",
        "venv",
        str(staging.parent / "dependency-build-runtime"),
    ]
    bootstrap_download = commands[1]
    assert bootstrap_download[0] == str(build_python)
    assert "download" in bootstrap_download
    assert "--require-hashes" in bootstrap_download
    bootstrap_install = commands[2]
    for option in ("--no-index", "--find-links", "--only-binary=:all:", "--require-hashes"):
        assert option in bootstrap_install
    assert bootstrap_install[-2:] == ["--requirement", str(bootstrap_lock)]
    assert commands[3] == [str(build_python), "-I", "-m", "pip", "--version"]


def test_install_playwright_probe_runtime_uses_hash_locked_main_wheelhouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _offline_builder_module()
    repo_root = tmp_path / "repo"
    staging = tmp_path / "payload"
    wheelhouse = staging / "wheelhouse" / "main"
    wheelhouse.mkdir(parents=True)
    main_lock = staging / "requirements" / "requirements-offline-main.lock"
    main_lock.parent.mkdir(parents=True)
    main_lock.write_text(
        f"playwright==1.52.0 --hash=sha256:{'a' * 64}\n",
        encoding="utf-8",
    )
    build_python = staging.parent / "dependency-build-runtime/Scripts/python.exe"
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        normalized = [str(part) for part in command]
        commands.append(normalized)
        if "importlib.metadata.version" in normalized[-1]:
            return "1.52.0\n"
        return ""

    monkeypatch.setattr(module, "_run", fake_run)

    module._install_playwright_probe_runtime(repo_root, staging, build_python)

    install = commands[0]
    assert install[0] == str(build_python)
    for option in (
        "--no-index",
        "--find-links",
        "--only-binary=:all:",
        "--require-hashes",
    ):
        assert option in install
    assert install[install.index("--find-links") + 1] == str(wheelhouse)
    assert install[-2:] == ["--requirement", str(main_lock)]
    assert commands[1][0] == str(build_python)


def test_read_wheel_identity_requires_license_metadata(tmp_path: Path) -> None:
    module = _offline_builder_module()
    wheel = tmp_path / "demo-1.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.2.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.2\nLicense: MIT\n",
        )

    identity = module.read_wheel_identity(wheel)

    assert identity == {"name": "demo", "version": "1.2", "license": "MIT"}

    unlicensed = tmp_path / "unlicensed-1.0-py3-none-any.whl"
    with zipfile.ZipFile(unlicensed, "w") as archive:
        archive.writestr(
            "unlicensed-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: unlicensed\nVersion: 1.0\n",
        )
    with pytest.raises(ValueError, match="license"):
        module.read_wheel_identity(unlicensed)


def test_read_wheel_identity_never_uses_license_file_name_as_license(
    tmp_path: Path,
) -> None:
    module = _offline_builder_module()
    wheel = tmp_path / "demo-1.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.2.dist-info/METADATA",
            (
                "Metadata-Version: 2.4\n"
                "Name: demo\n"
                "Version: 1.2\n"
                "License-File: LICENSE.txt\n"
                "Classifier: License :: OSI Approved :: BSD License\n"
            ),
        )

    identity = module.read_wheel_identity(wheel)

    assert identity["license"] == "OSI Approved :: BSD License"


def test_read_wheel_identity_prefers_classifier_to_embedded_license_text(
    tmp_path: Path,
) -> None:
    module = _offline_builder_module()
    wheel = tmp_path / "demo-1.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.2.dist-info/METADATA",
            (
                "Metadata-Version: 2.1\n"
                "Name: demo\n"
                "Version: 1.2\n"
                "License: Copyright holder full license text that should not become an ID.\n"
                "Classifier: License :: OSI Approved :: MIT License\n"
            ),
        )

    identity = module.read_wheel_identity(wheel)

    assert identity["license"] == "OSI Approved :: MIT License"


def test_read_wheel_identity_requires_audited_override_when_only_license_file_exists(
    tmp_path: Path,
) -> None:
    module = _offline_builder_module()
    wheel = tmp_path / "demo-1.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.2.dist-info/METADATA",
            (
                "Metadata-Version: 2.4\n"
                "Name: demo\n"
                "Version: 1.2\n"
                "License-File: LICENSE.txt\n"
            ),
        )

    with pytest.raises(ValueError, match="license"):
        module.read_wheel_identity(wheel)

    assert module.read_wheel_identity(
        wheel,
        license_override="Apache-2.0 OR MIT",
    ) == {
        "name": "demo",
        "version": "1.2",
        "license": "Apache-2.0 OR MIT",
    }


def test_wheel_metadata_rows_apply_audited_license_override(tmp_path: Path) -> None:
    module = _offline_builder_module()
    staging = tmp_path / "payload"
    wheelhouse = staging / "wheelhouse" / "audio"
    wheelhouse.mkdir(parents=True)
    (staging / "wheelhouse" / "main").mkdir()
    wheel = wheelhouse / "demo-1.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.2.dist-info/METADATA",
            (
                "Metadata-Version: 2.4\n"
                "Name: demo\n"
                "Version: 1.2\n"
                "License-File: LICENSE.txt\n"
            ),
        )

    metadata, components = module._wheel_metadata_rows(
        staging,
        {
            "intervaltree": {
                "version": "3.1.0",
                "source": "https://example.invalid/intervaltree.tar.gz",
            },
            "python_wheel_license_overrides": {
                "demo==1.2": "Apache-2.0 OR MIT",
            },
        },
    )

    assert metadata["wheelhouse/audio/demo-1.2-py3-none-any.whl"]["license"] == (
        "Apache-2.0 OR MIT"
    )
    assert components["audio_wheelhouse"]["packages"] == [
        {
            "name": "demo",
            "version": "1.2",
            "license": "Apache-2.0 OR MIT",
            "source_kind": "wheel",
        }
    ]


def test_read_wheel_identity_ignores_vendored_dist_info_metadata(tmp_path: Path) -> None:
    module = _offline_builder_module()
    wheel = tmp_path / "demo-1.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.2.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.2\nLicense: MIT\n",
        )
        archive.writestr(
            "demo/_vendor/child-9.9.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: child\nVersion: 9.9\nLicense: MIT\n",
        )

    assert module.read_wheel_identity(wheel)["name"] == "demo"


def test_normalize_built_wheel_removes_timestamp_and_compression_variance(
    tmp_path: Path,
) -> None:
    module = _offline_builder_module()
    first = tmp_path / "demo-1.0-py3-none-any.whl"
    second = tmp_path / "copy" / first.name
    second.parent.mkdir()
    files = {
        "demo/__init__.py": b"",
        "demo-1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\nLicense: MIT\n"
        ),
        "demo-1.0.dist-info/RECORD": b"",
    }
    with zipfile.ZipFile(first, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, (2026, 8, 16, 10, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    with zipfile.ZipFile(second, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in reversed(list(files.items())):
            info = zipfile.ZipInfo(name, (2025, 1, 2, 3, 4, 6))
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)

    first_hash = module.normalize_built_wheel(first)
    second_hash = module.normalize_built_wheel(second)

    assert first_hash == second_hash
    assert first.read_bytes() == second.read_bytes()


def test_stage_ffmpeg_archive_verifies_source_hash_and_filters_payload(tmp_path: Path) -> None:
    module = _offline_builder_module()
    archive_path = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("fixed/LICENSE.txt", "LGPL-3.0-or-later")
        archive.writestr("fixed/bin/ffmpeg.exe", b"ffmpeg")
        archive.writestr("fixed/bin/ffprobe.exe", b"ffprobe")
        archive.writestr("fixed/bin/avcodec.dll", b"dll")
        archive.writestr("fixed/include/header.h", b"not-runtime")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    destination = tmp_path / "tools" / "ffmpeg"

    result = module.stage_ffmpeg_archive(
        archive_path,
        destination,
        {
            "archive_sha256": digest,
            "root_directory": "fixed",
            "version": "n8.1.2-test",
            "license": "LGPL-3.0-or-later",
            "source": "https://example.invalid/ffmpeg.zip",
        },
    )

    assert result["ffmpeg_relative_path"] == "tools/ffmpeg/bin/ffmpeg.exe"
    assert result["ffprobe_relative_path"] == "tools/ffmpeg/bin/ffprobe.exe"
    assert (destination / "bin" / "avcodec.dll").is_file()
    assert not (destination / "include" / "header.h").exists()
    with pytest.raises(ValueError, match="SHA-256"):
        module.stage_ffmpeg_archive(
            archive_path,
            tmp_path / "wrong",
            {
                "archive_sha256": "0" * 64,
                "root_directory": "fixed",
                "version": "n8.1.2-test",
                "license": "LGPL-3.0-or-later",
                "source": "https://example.invalid/ffmpeg.zip",
            },
        )


def test_ffmpeg_source_uses_immutable_committed_asset_identity() -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "scripts/release/offline_sources.json").read_text(
            encoding="utf-8"
        )
    )
    source = config["ffmpeg"]

    assert source["source_kind"] == "committed_repository_asset"
    assert source["release_tag"] == "n8.1.2"
    assert source["source"].startswith("repository:")
    assert source["runtime_archive_path"].startswith("scripts/release/ffmpeg_assets/")
    assert source["archive_sha256"] == (
        "40a5867e1b229b787b1886efdf9dfc1f80afc75d0ecd28af9c51d66f13ecd963"
    )
    assert source["ffmpeg_source_commit"].startswith("sha256:")
    assert source["build_source_commit"].startswith("sha256:")
    assert source["external_codec_libraries"] is False


def test_ffmpeg_build_receipt_has_complete_toolchain_hashes() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    receipt = json.loads(
        (
            repo_root
            / "scripts"
            / "release"
            / "ffmpeg_assets"
            / "build"
            / "build-receipt.json"
        ).read_text(encoding="utf-8")
    )
    packages = receipt["compiler_packages"]

    assert packages
    assert all(re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) for row in packages)
    gmp = next(row for row in packages if row["name"] == "mingw-w64-x86_64-gmp")
    assert gmp["sha256"] == (
        "8924433974c4add46cb46ea4f6ef283b5c5139d3f552375115b5580f855015cc"
    )


def test_ffmpeg_asset_manifest_self_hash_and_file_rows_are_complete() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "scripts" / "release" / "ffmpeg_assets" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    unsigned = dict(manifest)
    claimed = unsigned.pop("manifest_sha256")

    assert claimed == hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    assert manifest["files"]
    assert all(
        isinstance(row["size"], int)
        and row["size"] > 0
        and re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
        and row["version"]
        and row["platform"]
        and row["license"]
        and row["source"]
        for row in manifest["files"]
    )


def test_validate_ffmpeg_source_rejects_floating_release_route() -> None:
    module = _offline_builder_module()
    source = {
        "github_asset_id": 1,
        "release_tag": "latest",
        "source": "https://example.invalid/releases/download/latest/ffmpeg.zip",
        "source_filename": "ffmpeg.zip",
        "archive_size": 1,
        "archive_sha256": "a" * 64,
        "build_source_commit": "b" * 40,
        "ffmpeg_source_commit": "c" * 40,
    }

    with pytest.raises(ValueError, match="fixed release"):
        module._validate_ffmpeg_source_declaration(source)


def test_playwright_browser_archives_and_ffmpeg_licenses_are_hash_pinned() -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "scripts/release/offline_sources.json").read_text(
            encoding="utf-8"
        )
    )
    chromium = config["playwright_chromium"]
    assert chromium["native_binding_schema"] == 1
    assert chromium["browser_version"] == "136.0.7103.25"
    assert chromium["chromium_archive_size"] == 151461854
    assert chromium["chromium_archive_sha256"] == (
        "241f8aa5c0fde70fb0cd9fdedfb65ee34e422fef8c30b39bd1158d0e10fcb884"
    )
    assert chromium["headless_shell_archive_size"] == 93435255
    assert chromium["headless_shell_archive_sha256"] == (
        "c7f0306d4d3bbe7ae4294309193486d3c6592667dfc32d96fd67d2c2e57510e4"
    )
    assert chromium["ffmpeg_revision"] == "1011"
    assert chromium["ffmpeg_archive_size"] == 1411741
    assert chromium["ffmpeg_archive_sha256"] == (
        "8d08827c019ad36e7b9d49d3648447d884534cb2acf200e71c715f6dd834cc50"
    )
    assert chromium["ffmpeg_executable_size"] == 3490816
    assert chromium["ffmpeg_executable_sha256"] == (
        "5b8f3f59ba61685828939ff3c833109748adbdea2fff4b4ae570c9fc0fc1ff4d"
    )
    assert chromium["ffmpeg_license_size"] == 26526
    assert chromium["ffmpeg_license_sha256"] == (
        "b634ab5640e258563c536e658cad87080553df6f34f62269a21d554844e58bfe"
    )
    assert chromium["winldd_revision"] == "1007"
    assert chromium["winldd_archive_size"] == 128684
    assert chromium["winldd_archive_sha256"] == (
        "0069f0d11d4ad6df068a068c003d22fe7dbec192a47bba64b2e115e9c8ce41d8"
    )
    assert chromium["winldd_executable_size"] == 258560
    assert chromium["winldd_executable_sha256"] == (
        "020bac54c953c65a0940b314c8afadd2854de2d83125e7b1393054fafd21f330"
    )
    assert chromium["winldd_license"] == "MIT and Apache-2.0"
    assert chromium["winldd_source_code_size"] == 4841
    assert chromium["winldd_source_code_sha256"] == (
        "c1521493a6ef81c53dcd19dc5a9e845ec07d9c395acc49678a512c50f0586260"
    )
    assert chromium["license_size"] == 1536
    assert chromium["license_sha256"] == (
        "368cca1106be99d39ecd32a38d8305585d802a475effb66380b91ffc9bcf709b"
    )
    assert chromium["playwright_license_size"] == 11399
    assert chromium["playwright_license_sha256"] == (
        "7fab1461b41970ff376f1c9303a637076bfaaeb71cd12dd3a1c44aaf59a1a2b9"
    )
    licenses = config["ffmpeg"]["license_sources"]
    assert {row["filename"] for row in licenses} == {
        "COPYING.GPLv2",
        "COPYING.GPLv3",
        "COPYING.LGPLv2.1",
        "COPYING.LGPLv3",
        "gcc-COPYING.RUNTIME",
        "gcc-COPYING.LIB",
        "gcc-COPYING3",
        "COPYING.MinGW-w64-runtime.txt.b64",
        "COPYING.MinGW-w64.txt",
    }
    assert all(row["size"] > 0 and len(row["sha256"]) == 64 for row in licenses)


def test_ffmpeg_companion_assets_are_excluded_from_general_release() -> None:
    policy = importlib.import_module("scripts.release.release_policy")
    selected = policy.collect_release_paths(
        [
            "scripts/release/ffmpeg_assets/runtime/ffmpeg-minimal-runtime.zip",
            "scripts/release/ffmpeg_assets/source/FFmpeg-n8.1.2.tar.gz",
            "scripts/release/build_offline_deps.py",
        ]
    )
    assert "scripts/release/build_offline_deps.py" in selected
    assert all(not path.startswith("scripts/release/ffmpeg_assets/") for path in selected)


def test_validate_committed_ffmpeg_requires_local_repository_identity() -> None:
    module = _offline_builder_module()
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "scripts/release/offline_sources.json").read_text(
            encoding="utf-8"
        )
    )
    source = dict(config["ffmpeg"])
    source["source"] = "https://example.invalid/ffmpeg.zip"
    with pytest.raises(ValueError, match="repository identity"):
        module._validate_ffmpeg_source_declaration(source)
    licenses = config["ffmpeg"]["license_sources"]
    encoded = next(
        row for row in licenses if row["filename"] == "COPYING.MinGW-w64-runtime.txt.b64"
    )
    assert encoded["encoding"] == "base64"
    assert encoded["content_size"] == 12155
    assert (
        encoded["content_sha256"]
        == "1db8da07b436c68833c0673ffee3d9fcb2526047f3820b81661865dfedc79a1f"
    )


def test_committed_ffmpeg_license_aliases_are_validated_against_source_namespace() -> None:
    module = _offline_builder_module()
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "scripts/release/offline_sources.json").read_text(
            encoding="utf-8"
        )
    )

    module._validate_ffmpeg_source_declaration(config["ffmpeg"])


@pytest.mark.parametrize(
    "attribute,value",
    (
        ("os_name", "posix"),
        ("implementation", "pypy"),
        ("version", (3, 12)),
        ("machine", "ARM64"),
        ("pointer_bits", 32),
    ),
)
def test_offline_builder_rejects_non_windows_cp311_x64_hosts(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: object
) -> None:
    module = _offline_builder_module()
    monkeypatch.setattr(module, "_target_host_identity", lambda: {
        "os": "windows" if attribute != "os_name" else value,
        "implementation": "cpython" if attribute != "implementation" else value,
        "python_version": (3, 11) if attribute != "version" else value,
        "machine": "AMD64" if attribute != "machine" else value,
        "pointer_bits": 64 if attribute != "pointer_bits" else value,
    })
    with pytest.raises(RuntimeError, match=r"Windows x64 CPython 3\.11"):
        module._assert_target_build_host()


def test_offline_builder_accepts_windows_cp311_x64_host(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _offline_builder_module()
    monkeypatch.setattr(
        module,
        "_target_host_identity",
        lambda: {
            "os": "windows",
            "implementation": "cpython",
            "python_version": (3, 11),
            "machine": "AMD64",
            "pointer_bits": 64,
        },
    )
    module._assert_target_build_host()


def test_offline_builder_uses_captured_commit_version_not_worktree_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _offline_builder_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    output = tmp_path / "Auto-Cut-v1.7.0-windows-x64-offline-deps.zip"
    captured = module.ReleaseSource(version="1.7.0", source_commit="a" * 40)
    monkeypatch.setattr(module, "capture_clean_release_source", lambda _root: captured)

    def stop_if_worktree_is_used(*_args, **_kwargs):
        raise RuntimeError("captured commit inputs reached")

    monkeypatch.setattr(module, "_read_committed_blob", stop_if_worktree_is_used)

    with pytest.raises(RuntimeError, match="captured commit inputs"):
        module.build_offline_deps(
            repo_root,
            output,
            work_root=tmp_path / "work",
        )


@pytest.mark.parametrize(
    "mutator",
    (
        lambda row: row.update(filename="../escape.txt"),
        lambda row: row.update(path="scripts/release/other.txt"),
        lambda row: row.update(filename="COPYING.GPLV2"),
    ),
)
def test_committed_ffmpeg_license_paths_are_canonical_and_inside_asset_root(mutator) -> None:
    module = _offline_builder_module()
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "scripts/release/offline_sources.json").read_text(
            encoding="utf-8"
        )
    )
    source = json.loads(json.dumps(config["ffmpeg"]))
    mutator(source["license_sources"][0])

    with pytest.raises(ValueError, match="license"):
        module._validate_ffmpeg_source_declaration(source)


def test_committed_ffmpeg_license_names_are_casefold_unique() -> None:
    module = _offline_builder_module()
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "scripts/release/offline_sources.json").read_text(
            encoding="utf-8"
        )
    )
    source = json.loads(json.dumps(config["ffmpeg"]))
    source["license_sources"][1]["filename"] = source["license_sources"][0]["filename"]
    source["license_sources"][1]["path"] = source["license_sources"][0]["path"]
    source["license_sources"][1]["url"] = source["license_sources"][0]["url"]

    with pytest.raises(ValueError, match="license"):
        module._validate_ffmpeg_source_declaration(source)


def test_extract_browser_archive_accepts_canonical_directory_entries(
    tmp_path: Path,
) -> None:
    module = _offline_builder_module()
    archive_path = tmp_path / "browser.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("chrome-win/", b"")
        archive.writestr("chrome-win/chrome.exe", b"browser")
    destination = tmp_path / "browser"

    module._extract_browser_archive(archive_path, destination)

    assert (destination / "chrome-win/chrome.exe").read_bytes() == b"browser"


@pytest.mark.parametrize(
    "unsafe_path",
    (
        chr(58).join(("C", "/escape.exe")),
        "NUL",
        "chrome-win/trailing.",
    ),
)
def test_source_archive_path_rejects_windows_unsafe_paths(unsafe_path: str) -> None:
    module = _offline_builder_module()

    with pytest.raises(ValueError, match="unsafe source archive path"):
        module._safe_zip_name(unsafe_path)


@pytest.mark.parametrize("schema", [None, 0, 2, "1", True])
def test_stage_chromium_requires_formal_native_binding_schema(
    tmp_path: Path, schema: object
) -> None:
    module = _offline_builder_module()
    declaration: dict[str, object] = {
        "playwright_version": "1.52.0",
        "revision": "1169",
        "browser_version": "136.0.7103.25",
        "ffmpeg_revision": "1011",
        "winldd_revision": "1007",
    }
    if schema is not None:
        declaration["native_binding_schema"] = schema

    with pytest.raises(ValueError, match="native binding schema"):
        module._stage_chromium(
            tmp_path,
            tmp_path / "payload",
            declaration,
            python_executable=tmp_path / "python.exe",
        )


@pytest.mark.parametrize(
    "field",
    (
        "chromium_executable_size",
        "chromium_executable_sha256",
        "headless_shell_executable_size",
        "headless_shell_executable_sha256",
        "chromium_tree_file_count",
        "chromium_tree_total_size",
        "chromium_tree_sha256",
        "headless_shell_tree_file_count",
        "headless_shell_tree_total_size",
        "headless_shell_tree_sha256",
        "ffmpeg_tree_file_count",
        "ffmpeg_tree_total_size",
        "ffmpeg_tree_sha256",
        "winldd_tree_file_count",
        "winldd_tree_total_size",
        "winldd_tree_sha256",
    ),
)
def test_stage_chromium_requires_every_formal_tree_binding_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    module = _offline_builder_module()
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "scripts/release/offline_sources.json").read_text(
            encoding="utf-8"
        )
    )
    declaration = dict(config["playwright_chromium"])
    declaration.pop(field)

    def fail_if_download_reached(*_args, **_kwargs) -> None:
        raise AssertionError("browser download reached before formal binding validation")

    monkeypatch.setattr(module, "_download", fail_if_download_reached)

    with pytest.raises(ValueError, match="formal Chromium tree binding"):
        module._stage_chromium(
            tmp_path,
            tmp_path / "payload",
            declaration,
            python_executable=tmp_path / "python.exe",
        )


def test_stage_chromium_uses_verified_declared_archives_and_measures_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _offline_builder_module()

    def browser_zip(filename: str, entries: dict[str, bytes]) -> bytes:
        path = tmp_path / filename
        with zipfile.ZipFile(path, "w") as archive:
            for relative, data in entries.items():
                archive.writestr(relative, data)
        return path.read_bytes()

    chromium_bytes = browser_zip("chromium.zip", {"chrome-win/chrome.exe": b"chromium"})
    shell_bytes = browser_zip("headless.zip", {"chrome-win/headless_shell.exe": b"headless"})
    ffmpeg_bytes = browser_zip(
        "recording-ffmpeg.zip",
        {
            "ffmpeg-win64.exe": b"recording-ffmpeg",
            "COPYING.LGPLv2.1": b"recording-license",
        },
    )
    winldd_bytes = browser_zip("winldd.zip", {"PrintDeps.exe": b"winldd"})
    winldd_source_bytes = b"MIT licensed PrintDeps source"
    sources = {
        "https://example.invalid/chromium.zip": chromium_bytes,
        "https://example.invalid/headless.zip": shell_bytes,
        "https://example.invalid/recording-ffmpeg.zip": ffmpeg_bytes,
        "https://example.invalid/winldd.zip": winldd_bytes,
        "https://example.invalid/PrintDeps.cpp": winldd_source_bytes,
    }
    declaration = {
        "playwright_version": "1.52.0",
        "revision": "1169",
        "browser_version": "136.0.7103.25",
        "native_binding_schema": 1,
        "chromium_source": "https://example.invalid/chromium.zip",
        "chromium_archive_size": len(chromium_bytes),
        "chromium_archive_sha256": hashlib.sha256(chromium_bytes).hexdigest(),
        "headless_shell_source": "https://example.invalid/headless.zip",
        "headless_shell_archive_size": len(shell_bytes),
        "headless_shell_archive_sha256": hashlib.sha256(shell_bytes).hexdigest(),
        "ffmpeg_revision": "1011",
        "ffmpeg_source": "https://example.invalid/recording-ffmpeg.zip",
        "ffmpeg_archive_size": len(ffmpeg_bytes),
        "ffmpeg_archive_sha256": hashlib.sha256(ffmpeg_bytes).hexdigest(),
        "ffmpeg_executable_size": len(b"recording-ffmpeg"),
        "ffmpeg_executable_sha256": hashlib.sha256(b"recording-ffmpeg").hexdigest(),
        "ffmpeg_license": "LGPL-2.1-or-later",
        "ffmpeg_license_filename": "COPYING.LGPLv2.1",
        "ffmpeg_license_size": len(b"recording-license"),
        "ffmpeg_license_sha256": hashlib.sha256(b"recording-license").hexdigest(),
        "winldd_revision": "1007",
        "winldd_source": "https://example.invalid/winldd.zip",
        "winldd_archive_size": len(winldd_bytes),
        "winldd_archive_sha256": hashlib.sha256(winldd_bytes).hexdigest(),
        "winldd_executable_size": len(b"winldd"),
        "winldd_executable_sha256": hashlib.sha256(b"winldd").hexdigest(),
        "winldd_license": "MIT",
        "winldd_source_code_url": "https://example.invalid/PrintDeps.cpp",
        "winldd_source_code_size": len(winldd_source_bytes),
        "winldd_source_code_sha256": hashlib.sha256(winldd_source_bytes).hexdigest(),
    }
    declaration.update(
        {
            "native_binding_schema": 1,
            "chromium_executable_size": len(b"chromium"),
            "chromium_executable_sha256": hashlib.sha256(b"chromium").hexdigest(),
            "headless_shell_executable_size": len(b"headless"),
            "headless_shell_executable_sha256": hashlib.sha256(b"headless").hexdigest(),
        }
    )
    declaration.update(_tree_binding("chromium", {"chrome-win/chrome.exe": b"chromium"}))
    declaration.update(
        _tree_binding("headless_shell", {"chrome-win/headless_shell.exe": b"headless"})
    )
    declaration.update(
        _tree_binding(
            "ffmpeg",
            {
                "ffmpeg-win64.exe": b"recording-ffmpeg",
                "COPYING.LGPLv2.1": b"recording-license",
            },
        )
    )
    declaration.update(_tree_binding("winldd", {"PrintDeps.exe": b"winldd"}))
    observed_urls: list[str] = []
    observed_commands: list[list[str]] = []

    def fake_download(url: str, destination: Path, **_kwargs) -> None:
        observed_urls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(sources[url])

    def fake_run(command, **_kwargs):
        normalized = [str(part) for part in command]
        observed_commands.append(normalized)
        assert "playwright install" not in " ".join(normalized)
        for browser_root in (
            tmp_path / "payload/browsers/chromium-1169",
            tmp_path / "payload/browsers/chromium_headless_shell-1169",
        ):
            (browser_root / "DEPENDENCIES_VALIDATED").write_bytes(b"")
        return json.dumps(
            {
                "ok": True,
                "browser": "chromium",
                "browser_version": "136.0.7103.25",
                "record_video_verified": True,
                "record_video_size": 4096,
            }
        )

    monkeypatch.setattr(module, "_download", fake_download)
    monkeypatch.setattr(module, "_run", fake_run)
    build_python = tmp_path / "dependency-build-runtime/Scripts/python.exe"

    result = module._stage_chromium(
        tmp_path,
        tmp_path / "payload",
        declaration,
        python_executable=build_python,
    )

    assert observed_urls == [
        declaration["chromium_source"],
        declaration["headless_shell_source"],
        declaration["ffmpeg_source"],
        declaration["winldd_source"],
        declaration["winldd_source_code_url"],
    ]
    assert observed_commands[0][0] == str(build_python)
    assert "record_video_dir" in observed_commands[0][3]
    assert result["browser_version"] == "136.0.7103.25"
    assert result["ffmpeg_revision"] == "1011"
    assert result["winldd_revision"] == "1007"
    assert result["record_video_verified"] is True
    assert "record_video_size" not in result
    assert result["source_archives_verified"] is True
    assert not list((tmp_path / "payload/browsers").rglob("DEPENDENCIES_VALIDATED"))
    receipt = json.loads(
        (tmp_path / "payload/receipts/chromium-launch.json").read_text(encoding="utf-8")
    )
    assert "record_video_size" not in receipt
    assert (
        tmp_path / "payload/browsers/chromium-1169/chrome-win/chrome.exe"
    ).read_bytes() == b"chromium"
    assert (
        tmp_path / "payload/browsers/chromium_headless_shell-1169/chrome-win/headless_shell.exe"
    ).read_bytes() == b"headless"
    assert (
        tmp_path / "payload/browsers/ffmpeg-1011/ffmpeg-win64.exe"
    ).read_bytes() == b"recording-ffmpeg"
    assert (tmp_path / "payload/browsers/winldd-1007/PrintDeps.exe").read_bytes() == b"winldd"
    assert (
        tmp_path / "payload/licenses/playwright-winldd/PrintDeps.cpp"
    ).read_bytes() == winldd_source_bytes


def test_playwright_stage_rejects_preexisting_runtime_marker(tmp_path: Path) -> None:
    module = _offline_builder_module()
    browser_root = tmp_path / "payload" / "browsers"
    marker_root = browser_root / "chromium-1169"
    marker_root.mkdir(parents=True)
    (marker_root / "DEPENDENCIES_VALIDATED").write_bytes(b"")

    with pytest.raises(ValueError, match="marker"):
        module._assert_no_playwright_runtime_markers(browser_root)


def test_stage_chromium_returns_formal_tree_binding_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _offline_builder_module()
    archives = {
        "chromium": b"chromium archive",
        "headless": b"headless archive",
        "recording": b"recording archive",
        "winldd": b"winldd archive",
    }
    declaration = {
        "playwright_version": "1.52.0",
        "revision": "1169",
        "browser_version": "136.0.7103.25",
        "native_binding_schema": 1,
        "chromium_source": "https://example.invalid/chromium",
        "chromium_archive_size": len(archives["chromium"]),
        "chromium_archive_sha256": hashlib.sha256(archives["chromium"]).hexdigest(),
        "headless_shell_source": "https://example.invalid/headless",
        "headless_shell_archive_size": len(archives["headless"]),
        "headless_shell_archive_sha256": hashlib.sha256(archives["headless"]).hexdigest(),
        "ffmpeg_revision": "1011",
        "ffmpeg_source": "https://example.invalid/recording",
        "ffmpeg_archive_size": len(archives["recording"]),
        "ffmpeg_archive_sha256": hashlib.sha256(archives["recording"]).hexdigest(),
        "ffmpeg_executable_size": 1,
        "ffmpeg_executable_sha256": "z" * 64,
        "ffmpeg_license_filename": "COPYING.LGPLv2.1",
        "ffmpeg_license_size": 1,
        "ffmpeg_license_sha256": "l" * 64,
        "winldd_revision": "1007",
        "winldd_source": "https://example.invalid/winldd",
        "winldd_archive_size": len(archives["winldd"]),
        "winldd_archive_sha256": hashlib.sha256(archives["winldd"]).hexdigest(),
        "winldd_executable_size": 1,
        "winldd_executable_sha256": "w" * 64,
        "winldd_source_code_url": "https://example.invalid/PrintDeps.cpp",
        "winldd_source_code_size": 1,
        "winldd_source_code_sha256": "s" * 64,
        "chromium_executable_size": 1,
        "chromium_executable_sha256": "a" * 64,
        "headless_shell_executable_size": 1,
        "headless_shell_executable_sha256": "b" * 64,
        "chromium_tree_file_count": 1,
        "chromium_tree_total_size": 1,
        "chromium_tree_sha256": "c" * 64,
        "headless_shell_tree_file_count": 1,
        "headless_shell_tree_total_size": 1,
        "headless_shell_tree_sha256": "d" * 64,
        "ffmpeg_tree_file_count": 1,
        "ffmpeg_tree_total_size": 1,
        "ffmpeg_tree_sha256": "e" * 64,
        "winldd_tree_file_count": 1,
        "winldd_tree_total_size": 1,
        "winldd_tree_sha256": "f" * 64,
    }
    payload = tmp_path / "payload"

    def fake_download(url: str, destination: Path, **_kwargs) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if url.endswith("PrintDeps.cpp"):
            destination.write_bytes(b"s")
            return
        key = next(name for name in archives if url.endswith(name))
        destination.write_bytes(archives[key])

    def fake_extract(_archive: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        if destination.name == "chromium-1169":
            files = {"chrome-win/chrome.exe": b"x"}
        elif destination.name == "chromium_headless_shell-1169":
            files = {"chrome-win/headless_shell.exe": b"y"}
        elif destination.name == "ffmpeg-1011":
            files = {
                "ffmpeg-win64.exe": b"z",
                "COPYING.LGPLv2.1": b"l",
            }
        else:
            files = {"PrintDeps.exe": b"w"}
        for relative, data in files.items():
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    monkeypatch.setattr(module, "_download", fake_download)
    monkeypatch.setattr(module, "_extract_browser_archive", fake_extract)
    monkeypatch.setattr(module, "_verify_fixed_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module,
        "_verify_tree_receipt",
        lambda _root, _source, prefix: {
            "file_count": declaration[f"{prefix}_tree_file_count"],
            "total_size": declaration[f"{prefix}_tree_total_size"],
            "tree_sha256": declaration[f"{prefix}_tree_sha256"],
        },
    )
    monkeypatch.setattr(
        module,
        "_run",
        lambda *args, **kwargs: json.dumps(
            {
                "ok": True,
                "browser": "chromium",
                "browser_version": "136.0.7103.25",
                "record_video_verified": True,
                "record_video_size": 1,
            }
        ),
    )

    result = module._stage_chromium(
        tmp_path,
        payload,
        declaration,
        python_executable=tmp_path / "python.exe",
    )

    assert result["chromium_executable_size"] == 1
    assert result["chromium_executable_sha256"] == "a" * 64
    assert result["headless_shell_executable_size"] == 1
    assert result["headless_shell_executable_sha256"] == "b" * 64


def test_ffmpeg_license_manifest_rows_use_their_actual_source_urls(tmp_path: Path) -> None:
    module = _offline_builder_module()
    staging = tmp_path / "payload"
    license_path = staging / "tools/ffmpeg/licenses/COPYING.GPLv3"
    license_path.parent.mkdir(parents=True)
    license_path.write_text("license", encoding="utf-8")
    source_url = "https://example.invalid/ffmpeg/COPYING.GPLv3"
    source_config = {
        "intervaltree": {"version": "3.1.0", "source": "https://example.invalid/tree"},
        "playwright_chromium": {"license": "BSD", "license_url": "https://example.invalid"},
        "ffmpeg": {
            "version": "n8.1.2",
            "license": "LGPL",
            "source": "https://example.invalid/ffmpeg.zip",
            "license_sources": [
                {
                    "filename": "COPYING.GPLv3",
                    "url": source_url,
                    "size": 7,
                    "sha256": hashlib.sha256(b"license").hexdigest(),
                }
            ],
        },
    }

    metadata, _components = module._file_metadata(
        staging,
        source_commit="a" * 40,
        source_config=source_config,
    )

    assert metadata["tools/ffmpeg/licenses/COPYING.GPLv3"]["source"] == source_url


def test_validate_ffmpeg_rejects_prefix_colliding_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _offline_builder_module()
    bin_root = tmp_path / "tools" / "ffmpeg" / "bin"
    bin_root.mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe"):
        (bin_root / f"{name}.exe").write_bytes(b"fixture")

    def fake_run(command, **_kwargs):
        name = Path(command[0]).stem
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{name} version n8.1.20-malicious\n",
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="identity"):
        module._validate_ffmpeg(
            tmp_path,
            {"version": "n8.1.2", "included": True},
        )


def test_validate_ffmpeg_receipt_records_only_exact_version_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _offline_builder_module()
    bin_root = tmp_path / "tools" / "ffmpeg" / "bin"
    bin_root.mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe"):
        (bin_root / f"{name}.exe").write_bytes(b"fixture")
    private_build_root = chr(47).join(("C:", "private", "build-root"))

    def fake_run(command, **_kwargs):
        name = Path(command[0]).stem
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{name} version n8.1.2 {private_build_root}\n",
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module._validate_ffmpeg(
        tmp_path,
        {"version": "n8.1.2", "included": True},
    )
    receipt = json.loads((tmp_path / "receipts" / "ffmpeg-tools.json").read_text(encoding="utf-8"))

    assert result == {"ffmpeg_verified": True, "ffprobe_verified": True}
    assert receipt["identities"] == {"ffmpeg": "n8.1.2", "ffprobe": "n8.1.2"}
    assert "private" not in json.dumps(receipt).casefold()


def test_intervaltree_source_build_is_hash_pinned() -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "scripts/release/offline_sources.json").read_text(
            encoding="utf-8"
        )
    )
    source = config["intervaltree"]

    assert source["version"] == "3.1.0"
    assert source["archive_size"] == 32861
    assert source["archive_sha256"] == (
        "902b1b88936918f9b2a19e0e5eb7ccb430ae45cde4f39ea4b36932920d33952d"
    )
    assert source["source"].startswith("https://files.pythonhosted.org/")


def test_offline_release_inputs_pass_release_privacy_scan() -> None:
    policy = importlib.import_module("scripts.release.release_policy")
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "scripts/release/build_offline_deps.py",
        "scripts/release/offline_bundle.py",
        "scripts/release/offline_sources.json",
        "schemas/offline-deps-manifest.schema.json",
        "tests/test_offline_bundle.py",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert policy.scan_text(relative, text) == []


def test_validate_staged_text_privacy_scans_copied_requirement_comments(
    tmp_path: Path,
) -> None:
    module = _offline_builder_module()
    staging = tmp_path / "payload"
    requirement = staging / "requirements" / "requirements.txt"
    requirement.parent.mkdir(parents=True)
    machine_path = chr(47).join(("C" + chr(58), "Users", "example", "private-cache"))
    requirement.write_text(
        f"demo==1.0\n# copied from {machine_path}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="privacy"):
        module.validate_staged_text_privacy(staging)
