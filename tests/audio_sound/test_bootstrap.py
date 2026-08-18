from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import audio_sound.bootstrap as bootstrap
from audio_sound.bootstrap import (
    _is_python_311,
    build_install_commands,
    build_respiro_setup_commands,
    detect_runtime,
    format_runtime_report,
    prune_workspace,
    run_install,
    run_respiro_setup,
)


class BootstrapTests(unittest.TestCase):
    def test_resolve_runtime_path_rejects_anchored_or_escaping_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)

            for unsafe_path in ("C:outside", r"\outside", "../outside"):
                with self.subTest(unsafe_path=unsafe_path):
                    self.assertIsNone(bootstrap._resolve_runtime_path(unsafe_path, root=root))

    def test_resolve_runtime_path_normalizes_relative_paths_inside_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)

            resolved = bootstrap._resolve_runtime_path(
                "tools/audio_sound_runtime/model.bin",
                root=root,
            )

        self.assertEqual(
            resolved,
            root / "tools" / "audio_sound_runtime" / "model.bin",
        )

    def test_external_model_policy_is_idempotent(self) -> None:
        raw_report = {
            "status": "full",
            "full": True,
            "degraded": False,
            "unavailable": False,
            "python": {"ok": True},
            "ffmpeg": {"ok": True},
            "ffprobe": {"ok": True},
            "deepfilternet": {"ok": True, "identity": "verified-deepfilter"},
            "respiro_en": {"ok": True, "identity": "verified-respiro"},
            "spectramini": {"ok": True},
        }

        first = bootstrap.apply_external_model_execution_policy(raw_report)
        second = bootstrap.apply_external_model_execution_policy(first)

        self.assertEqual(second["status"], "degraded")
        for component_name in ("deepfilternet", "respiro_en"):
            self.assertFalse(second[component_name]["ok"])
            self.assertTrue(second[component_name]["asset_verification_ok"])
            self.assertEqual(
                second[component_name]["execution_status"],
                "external_unavailable",
            )

    def test_detect_runtime_applies_external_models_fail_closed_policy(self) -> None:
        python_info = {
            "ok": True,
            "path": "audio-python",
            "version": "3.11.9",
            "deepfilternet_ok": True,
            "deepfilternet_identity": "deepfilternet@0.5.6",
            "spectramini_runtime_ok": True,
            "spectramini_identity": "local-spectramini",
        }
        with (
            mock.patch("audio_sound.bootstrap._inspect_python_runtime", return_value=python_info),
            mock.patch(
                "audio_sound.bootstrap.shutil.which",
                side_effect=lambda name: f"C:/tools/{name}.exe",
            ),
            mock.patch(
                "audio_sound.bootstrap._inspect_binary",
                side_effect=lambda path, **_kwargs: {
                    "ok": True,
                    "path": path,
                    "version": "8.1.1",
                    "identity": path,
                    "error": "",
                },
            ),
            mock.patch(
                "audio_sound.bootstrap._verify_respiro_runtime",
                return_value={"ok": True, "identity": "verified-respiro", "notes": "verified"},
            ),
            mock.patch(
                "audio_sound.bootstrap._verify_deepfilternet_model",
                return_value={"ok": True, "identity": "verified-deepfilter", "notes": "verified"},
            ),
            mock.patch(
                "audio_sound.bootstrap._probe_deepfilternet_adapter",
                return_value={"ok": True, "command": [], "error": ""},
            ),
        ):
            payload = detect_runtime(python_executable="audio-python")

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["execution_policy"], "external_models_fail_closed")
        for component_name in ("deepfilternet", "respiro_en"):
            self.assertFalse(payload[component_name]["ok"])
            self.assertTrue(payload[component_name]["asset_verification_ok"])
            self.assertEqual(
                payload[component_name]["execution_status"],
                "external_unavailable",
            )

    def test_run_install_applies_external_policy_to_returned_runtime(self) -> None:
        raw_runtime = {
            "status": "full",
            "full": True,
            "degraded": False,
            "unavailable": False,
            "python": {"ok": True},
            "ffmpeg": {"ok": True},
            "ffprobe": {"ok": True},
            "deepfilternet": {"ok": True},
            "respiro_en": {"ok": True},
            "spectramini": {"ok": True},
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch(
                "audio_sound.bootstrap._run_install_unlocked",
                return_value={"ok": True, "code": "ok", "data": {"runtime": raw_runtime}},
            ):
                payload = run_install(repo_root=Path(tmp_dir))

        runtime = payload["data"]["runtime"]
        self.assertEqual(runtime["status"], "degraded")
        self.assertEqual(runtime["execution_policy"], "external_models_fail_closed")
        self.assertTrue(runtime["deepfilternet"]["asset_verification_ok"])
        self.assertTrue(runtime["respiro_en"]["asset_verification_ok"])

    def test_python_version_check_accepts_only_python_311_patch_versions(self) -> None:
        self.assertTrue(_is_python_311("3.11.9"))
        self.assertTrue(_is_python_311("3.11.0rc1"))
        self.assertFalse(_is_python_311("3.10.14"))
        self.assertFalse(_is_python_311("3.11"))
        self.assertFalse(_is_python_311("3.11.unknown"))

    def test_build_install_commands_bootstrap_source_build_before_audio_lock(
        self,
    ) -> None:
        root = Path("S:/Agent/Auto jianji/Auto-Cut")
        commands = build_install_commands(repo_root=root, python_executable="audio-python")
        pip_prefix = [
            "audio-python",
            "-I",
            "-m",
            "pip",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--timeout",
            "60",
            "--retries",
            "10",
            "install",
        ]

        self.assertEqual(
            commands,
            [
                [
                    *pip_prefix,
                    "--requirement",
                    str(root / "requirements-audio-build.lock"),
                ],
                [
                    *pip_prefix,
                    "--no-build-isolation",
                    "--no-deps",
                    "intervaltree==3.1.0",
                ],
                [
                    *pip_prefix,
                    "--requirement",
                    str(root / "requirements-audio.lock"),
                ],
                ["audio-python", "-I", "-m", "pip", "check"],
            ],
        )

    def test_build_install_commands_default_to_the_isolated_interpreter(self) -> None:
        root = Path("S:/Agent/Auto jianji/Auto-Cut")

        commands = build_install_commands(repo_root=root)

        self.assertEqual(
            commands[0][0],
            str(root / ".venv-audio" / "Scripts" / "python.exe"),
        )

    def test_build_install_commands_rejects_an_unhashed_local_wheel(self) -> None:
        root = Path("S:/Agent/Auto jianji/Auto-Cut")

        with self.assertRaises(ValueError):
            build_install_commands(
                repo_root=root,
                python_executable="audio-python",
                local_wheel=root / "tmp" / "torch.whl",
            )

    def test_validate_package_index_url_accepts_https(self) -> None:
        result = bootstrap._validate_package_index_url(
            "https://mirrors.cloud.tencent.com/pypi/simple"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["data"]["package_index_url"],
            "https://mirrors.cloud.tencent.com/pypi/simple",
        )

    def test_validate_package_index_url_rejects_unsafe_values(self) -> None:
        for value in (
            "",
            "http://mirror.example/simple",
            "https://user:secret@mirror.example/simple",
            "https://mirror.example/simple?token=secret",
            "https://mirror.example/simple#fragment",
            "https:///simple",
            "https://mirror.example:invalid/simple",
            "https://mirror.example/\n/simple",
            "https://mirror.example\\evil/simple",
        ):
            with self.subTest(value=value):
                result = bootstrap._validate_package_index_url(value)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "invalid_package_index_url")

    def test_run_install_rejects_an_invalid_index_before_staging(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            mock.patch("audio_sound.bootstrap._create_install_staging") as create,
        ):
            payload = run_install(
                repo_root=Path(tmp_dir),
                index_url="http://mirror.example/simple",
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "invalid_package_index_url")
        create.assert_not_called()

    def test_build_install_commands_applies_index_only_to_network_resolution(
        self,
    ) -> None:
        root = Path("S:/Agent/Auto jianji/Auto-Cut")
        staged_wheel = root / ".venv-audio.codex-staging-test" / ".install-tmp" / "torch.whl"
        index_url = "https://mirrors.cloud.tencent.com/pypi/simple"

        commands = build_install_commands(
            repo_root=root,
            python_executable="audio-python",
            local_wheel=staged_wheel,
            local_wheel_sha256="a" * 64,
            index_url=index_url,
        )

        self.assertNotIn("--index-url", commands[0])
        for command in commands[1:4]:
            position = command.index("--index-url")
            self.assertEqual(command[position + 1], index_url)
        self.assertNotIn("--index-url", commands[4])

    def test_build_install_commands_offline_uses_only_verified_wheelhouse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            bundle_root = Path(tmp_dir) / "offline-bundle"
            wheelhouse = bundle_root / "wheelhouse" / "audio"
            requirements = bundle_root / "requirements"
            wheelhouse.mkdir(parents=True)
            requirements.mkdir()
            for name in (
                "requirements-audio-build.lock",
                "requirements-audio.lock",
                "requirements-offline-audio.lock",
            ):
                (requirements / name).write_text("demo==1.0\n", encoding="utf-8")

            commands = build_install_commands(
                repo_root=Path(tmp_dir) / "Auto-Cut",
                python_executable="audio-python",
                offline_wheelhouse=wheelhouse,
            )

        pip_installs = [command for command in commands if "install" in command]
        self.assertEqual(len(pip_installs), 1)
        for command in pip_installs:
            self.assertIn("--no-index", command)
            position = command.index("--find-links")
            self.assertEqual(command[position + 1], str(wheelhouse))
            self.assertNotIn("--index-url", command)
            self.assertIn("--only-binary=:all:", command)
            self.assertIn("--require-hashes", command)
        self.assertEqual(
            pip_installs[0][-2:],
            ["--requirement", str(requirements / "requirements-offline-audio.lock")],
        )

    def test_run_install_rejects_network_or_unverified_wheel_with_offline_wheelhouse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            wheelhouse = root / "offline" / "wheelhouse" / "audio"
            wheelhouse.mkdir(parents=True)

            index_result = run_install(
                repo_root=root,
                offline_wheelhouse=wheelhouse,
                index_url="https://example.com/simple",
            )
            wheel_result = run_install(
                repo_root=root,
                offline_wheelhouse=wheelhouse,
                local_wheel=root / "torch.whl",
                local_wheel_sha256="a" * 64,
            )

        self.assertEqual(index_result["code"], "offline_source_conflict")
        self.assertEqual(wheel_result["code"], "offline_source_conflict")

    def test_offline_install_environment_removes_proxy_and_index_configuration(self) -> None:
        injected = {
            "HTTP_PROXY": "http://proxy.invalid",
            "HTTPS_PROXY": "http://proxy.invalid",
            "ALL_PROXY": "socks://proxy.invalid",
            "NO_PROXY": "localhost",
            "PIP_INDEX_URL": "https://index.invalid/simple",
        }
        with mock.patch.dict(os.environ, injected):
            environment = bootstrap._isolated_install_environment(offline=True)

        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            self.assertEqual(environment[key], "http://127.0.0.1:9")
        self.assertEqual(environment["NO_PROXY"], "")
        self.assertNotIn("PIP_INDEX_URL", environment)
        self.assertEqual(environment["PIP_NO_INDEX"], "1")

    def test_run_install_reports_index_on_an_early_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = run_install(
                repo_root=root,
                python_executable=root / "other-python.exe",
                index_url="https://mirrors.cloud.tencent.com/pypi/simple",
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "unsafe_python_target")
        self.assertEqual(
            payload["data"]["package_index_url"],
            "https://mirrors.cloud.tencent.com/pypi/simple",
        )

    def test_run_install_reports_index_on_local_wheel_validation_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = run_install(
                repo_root=Path(tmp_dir),
                local_wheel="tmp/torch.whl",
                index_url="https://mirrors.cloud.tencent.com/pypi/simple",
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "local_wheel_arguments_incomplete")
        self.assertEqual(
            payload["data"]["package_index_url"],
            "https://mirrors.cloud.tencent.com/pypi/simple",
        )

    def test_validate_local_wheel_requires_path_and_digest_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            missing_digest = bootstrap._validate_local_wheel(root, "tmp/torch.whl", None)
            missing_path = bootstrap._validate_local_wheel(root, None, "a" * 64)

        self.assertEqual(missing_digest["code"], "local_wheel_arguments_incomplete")
        self.assertEqual(missing_path["code"], "local_wheel_arguments_incomplete")

    def test_validate_local_wheel_accepts_a_matching_repo_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            wheel = root / "tmp" / "torch.whl"
            wheel.parent.mkdir()
            wheel.write_bytes(b"verified-wheel")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()

            result = bootstrap._validate_local_wheel(root, wheel, digest.upper())

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["filename"], "torch.whl")
        self.assertEqual(result["data"]["size"], len(b"verified-wheel"))
        self.assertEqual(result["data"]["sha256"], digest)

    def test_validate_local_wheel_rejects_external_and_nonwheel_paths(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            tempfile.TemporaryDirectory() as external_dir,
        ):
            root = Path(tmp_dir)
            external = Path(external_dir) / "torch.whl"
            external.write_bytes(b"wheel")
            text_file = root / "tmp" / "torch.txt"
            text_file.parent.mkdir()
            text_file.write_bytes(b"wheel")
            digest = hashlib.sha256(b"wheel").hexdigest()

            outside = bootstrap._validate_local_wheel(root, external, digest)
            wrong_type = bootstrap._validate_local_wheel(root, text_file, digest)

        self.assertEqual(outside["code"], "unsafe_local_wheel")
        self.assertEqual(wrong_type["code"], "unsafe_local_wheel")

    def test_validate_local_wheel_rejects_a_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            missing = root / "tmp" / "missing.whl"

            result = bootstrap._validate_local_wheel(
                root, missing, hashlib.sha256(b"wheel").hexdigest()
            )

        self.assertEqual(result["code"], "unsafe_local_wheel")

    def test_validate_local_wheel_rejects_a_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            wheel = root / "tmp" / "torch.whl"
            wheel.parent.mkdir()
            wheel.write_bytes(b"wheel")
            link = root / "tmp" / "linked-torch.whl"
            try:
                link.symlink_to(wheel)
            except OSError as exc:
                self.skipTest(f"cannot create file symlink: {exc}")

            result = bootstrap._validate_local_wheel(
                root, link, hashlib.sha256(b"wheel").hexdigest()
            )

        self.assertEqual(result["code"], "unsafe_local_wheel")

    def test_validate_local_wheel_rejects_invalid_and_mismatched_digests(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            wheel = root / "tmp" / "torch.whl"
            wheel.parent.mkdir()
            wheel.write_bytes(b"wheel")

            malformed = bootstrap._validate_local_wheel(root, wheel, "not-a-sha256")
            mismatch = bootstrap._validate_local_wheel(root, wheel, "0" * 64)

        self.assertEqual(malformed["code"], "invalid_local_wheel_sha256")
        self.assertEqual(mismatch["code"], "local_wheel_sha256_mismatch")

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_validate_local_wheel_rejects_a_junction_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "repo"
            external = Path(tmp_dir) / "external"
            root.mkdir()
            external.mkdir()
            wheel = external / "torch.whl"
            wheel.write_bytes(b"wheel")
            junction = root / "tmp"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")

            result = bootstrap._validate_local_wheel(
                root,
                junction / "torch.whl",
                hashlib.sha256(b"wheel").hexdigest(),
            )

        self.assertEqual(result["code"], "unsafe_local_wheel")

    def test_build_install_commands_prepends_local_wheel_without_dependencies(
        self,
    ) -> None:
        root = Path("S:/Agent/Auto jianji/Auto-Cut")
        staged_wheel = root / ".venv-audio.codex-staging-test" / ".install-tmp" / "torch.whl"

        commands = build_install_commands(
            repo_root=root,
            python_executable="audio-python",
            local_wheel=staged_wheel,
            local_wheel_sha256="a" * 64,
        )

        self.assertEqual(
            commands[0],
            [
                "audio-python",
                "-I",
                "-m",
                "pip",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "install",
                "--no-deps",
                "--require-hashes",
                f"{bootstrap._lexical_absolute(staged_wheel).as_uri()}#sha256={'a' * 64}",
            ],
        )
        pip_prefix = [
            "audio-python",
            "-I",
            "-m",
            "pip",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--timeout",
            "60",
            "--retries",
            "10",
            "install",
        ]
        self.assertEqual(
            commands[1:],
            [
                [
                    *pip_prefix,
                    "--requirement",
                    str(root / "requirements-audio-build.lock"),
                ],
                [
                    *pip_prefix,
                    "--no-build-isolation",
                    "--no-deps",
                    "intervaltree==3.1.0",
                ],
                [
                    *pip_prefix,
                    "--requirement",
                    str(root / "requirements-audio.lock"),
                ],
                ["audio-python", "-I", "-m", "pip", "check"],
            ],
        )
        self.assertEqual(len(commands), 5)

    def test_run_install_resolves_py311_when_invoking_python_is_not_311(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            mock.patch("audio_sound.bootstrap.sys.executable", "C:/Python310/python.exe"),
            mock.patch("audio_sound.bootstrap.sys.version_info", (3, 10, 14)),
            mock.patch(
                "audio_sound.bootstrap.shutil.which",
                side_effect=lambda name: "C:/Windows/py.exe" if name == "py" else None,
            ),
            mock.patch(
                "audio_sound.bootstrap.subprocess.run",
                side_effect=FileNotFoundError("resolved interpreter missing"),
            ),
        ):
            payload = run_install(repo_root=Path(tmp_dir))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "python_3_11_unavailable")
        self.assertEqual(
            payload["data"]["steps"][0]["command"][:4],
            ["C:/Windows/py.exe", "-3.11", "-I", "-c"],
        )
        self.assertIn("-c", payload["data"]["steps"][0]["command"])
        self.assertNotIn("venv", payload["data"]["steps"][0]["command"])

    def test_run_install_falls_back_when_py_launcher_has_no_python311(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            observed_commands: list[list[str]] = []

            def fake_run(command, **_kwargs):
                observed_commands.append(command)
                if "auto_cut_audio_python_creator_probe_v1" in " ".join(command):
                    if command[0] == "C:/Windows/py.exe":
                        return subprocess.CompletedProcess(
                            command,
                            1,
                            stdout="",
                            stderr="Requested Python version not installed",
                        )
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps({"version": [3, 11, 9], "bits": "64bit"}),
                        stderr="",
                    )
                if command[-3:-1] == ["-m", "venv"]:
                    staged_python = Path(command[-1]) / "Scripts" / "python.exe"
                    staged_python.parent.mkdir(parents=True, exist_ok=True)
                    staged_python.write_bytes(b"placeholder")
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            def inspect_python(python_executable):
                return {
                    "ok": True,
                    "path": python_executable,
                    "version": "3.11.9",
                    "deepfilternet_ok": False,
                    "spectramini_runtime_ok": False,
                }

            with (
                mock.patch("audio_sound.bootstrap.sys.executable", "C:/Python310/python.exe"),
                mock.patch("audio_sound.bootstrap.sys.version_info", (3, 10, 14)),
                mock.patch(
                    "audio_sound.bootstrap.shutil.which",
                    side_effect=lambda name: {
                        "py": "C:/Windows/py.exe",
                        "python3.11": "C:/Python311/python.exe",
                    }.get(name),
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run),
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime",
                    side_effect=inspect_python,
                ),
                mock.patch(
                    "audio_sound.bootstrap.detect_runtime",
                    return_value={"status": "degraded"},
                ),
            ):
                payload = run_install(repo_root=root)

        self.assertTrue(payload["ok"])
        probe_commands = [
            command
            for command in observed_commands
            if "auto_cut_audio_python_creator_probe_v1" in " ".join(command)
        ]
        self.assertEqual(
            [command[:2] for command in probe_commands],
            [["C:/Windows/py.exe", "-3.11"], ["C:/Python311/python.exe", "-I"]],
        )
        create_command = next(command for command in observed_commands if "venv" in command)
        self.assertEqual(create_command[0], "C:/Python311/python.exe")

    def test_run_install_rejects_current_32_bit_python311_before_venv_creation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            mock.patch("audio_sound.bootstrap.sys.executable", "C:/Python311-32/python.exe"),
            mock.patch("audio_sound.bootstrap.sys.version_info", (3, 11, 9)),
            mock.patch("audio_sound.bootstrap.shutil.which", return_value=None),
            mock.patch(
                "audio_sound.bootstrap.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="32-bit"),
            ) as run,
        ):
            payload = run_install(repo_root=Path(tmp_dir))

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "python_3_11_unavailable")
        probe_command = run.call_args.args[0]
        self.assertEqual(probe_command[:3], ["C:/Python311-32/python.exe", "-I", "-c"])
        self.assertNotIn("venv", probe_command)

    def test_run_install_falls_back_when_first_python311_cannot_create_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            observed_commands: list[list[str]] = []

            def fake_run(command, **_kwargs):
                observed_commands.append(command)
                if "auto_cut_audio_python_creator_probe_v1" in " ".join(command):
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                if command[-3:-1] == ["-m", "venv"]:
                    if command[0] == "C:/Windows/py.exe":
                        return subprocess.CompletedProcess(
                            command, 1, stdout="", stderr="venv module failed"
                        )
                    staged_python = Path(command[-1]) / "Scripts" / "python.exe"
                    staged_python.parent.mkdir(parents=True, exist_ok=True)
                    staged_python.write_bytes(b"placeholder")
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            def inspect_python(python_executable):
                return {
                    "ok": True,
                    "path": python_executable,
                    "version": "3.11.9",
                    "deepfilternet_ok": False,
                    "spectramini_runtime_ok": False,
                }

            with (
                mock.patch("audio_sound.bootstrap.sys.executable", "C:/Python310/python.exe"),
                mock.patch("audio_sound.bootstrap.sys.version_info", (3, 10, 14)),
                mock.patch(
                    "audio_sound.bootstrap.shutil.which",
                    side_effect=lambda name: {
                        "py": "C:/Windows/py.exe",
                        "python3.11": "C:/Python311/python.exe",
                    }.get(name),
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run),
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime",
                    side_effect=inspect_python,
                ),
                mock.patch(
                    "audio_sound.bootstrap.detect_runtime",
                    return_value={"status": "degraded"},
                ),
            ):
                payload = run_install(repo_root=root)

        self.assertTrue(payload["ok"])
        create_commands = [command for command in observed_commands if "venv" in command]
        self.assertEqual(
            [command[0] for command in create_commands],
            ["C:/Windows/py.exe", "C:/Python311/python.exe"],
        )
        self.assertNotEqual(create_commands[0][-1], create_commands[1][-1])

    def test_run_install_reports_verified_wheel_on_an_early_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            wheel = root / "tmp" / "torch.whl"
            wheel.parent.mkdir()
            wheel.write_bytes(b"verified-wheel")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()

            payload = run_install(
                repo_root=root,
                python_executable=root / "other-python.exe",
                local_wheel=wheel,
                local_wheel_sha256=digest,
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "unsafe_python_target")
        self.assertEqual(payload["data"]["local_wheel"]["sha256"], digest)

    def test_run_install_rejects_a_staged_non_python_311_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            isolated_python = root / ".venv-audio" / "Scripts" / "python.exe"
            isolated_python.parent.mkdir(parents=True)
            isolated_python.write_bytes(b"placeholder")

            def fake_run(command, **_kwargs):
                if "auto_cut_audio_python_creator_probe_v1" in " ".join(command):
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                staged_python = Path(command[-1]) / "Scripts" / "python.exe"
                staged_python.parent.mkdir(parents=True)
                staged_python.write_bytes(b"staged")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            def inspect_python(python_executable):
                return {
                    "ok": True,
                    "path": python_executable,
                    "version": "3.10.14",
                    "deepfilternet_ok": False,
                    "respiro_runtime_ok": False,
                }

            with (
                mock.patch("audio_sound.bootstrap.sys.executable", "C:/Python311/python.exe"),
                mock.patch("audio_sound.bootstrap.sys.version_info", (3, 11, 9)),
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime",
                    side_effect=inspect_python,
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run) as run,
            ):
                payload = run_install(repo_root=root)

            self.assertTrue(isolated_python.exists())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "python_3_11_required")
        self.assertEqual(payload["data"]["detected_version"], "3.10.14")
        self.assertEqual(run.call_count, 2)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_run_install_rejects_a_junction_backed_audio_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "repo"
            external = base / "external-audio-environment"
            root.mkdir()
            external_python = external / "Scripts" / "python.exe"
            external_python.parent.mkdir(parents=True)
            external_python.write_bytes(b"placeholder")
            junction = root / ".venv-audio"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")

            completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime",
                    return_value={
                        "ok": True,
                        "path": str(external_python),
                        "version": "3.11.9",
                        "deepfilternet_ok": False,
                        "respiro_runtime_ok": False,
                    },
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", return_value=completed) as run,
            ):
                payload = run_install(repo_root=root)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "unsafe_runtime_path")
        run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_run_install_rejects_a_junction_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            real_root = base / "real-repo"
            real_root.mkdir()
            isolated_python = real_root / ".venv-audio" / "Scripts" / "python.exe"
            isolated_python.parent.mkdir(parents=True)
            isolated_python.write_bytes(b"placeholder")
            lexical_root = base / "repo-link"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(lexical_root), str(real_root)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")

            with mock.patch("audio_sound.bootstrap.subprocess.run") as run:
                payload = run_install(repo_root=lexical_root)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "unsafe_repo_root")
        run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_run_install_rejects_a_junction_backed_cargo_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "repo"
            external = base / "external-cargo-home"
            root.mkdir()
            external.mkdir()
            isolated_python = root / ".venv-audio" / "Scripts" / "python.exe"
            isolated_python.parent.mkdir(parents=True)
            isolated_python.write_bytes(b"placeholder")
            junction = root / ".cargo-home"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")
            python_info = {
                "ok": True,
                "path": str(isolated_python),
                "version": "3.11.9",
                "deepfilternet_ok": False,
                "spectramini_runtime_ok": False,
            }
            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime", return_value=python_info
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run") as run,
            ):
                payload = run_install(repo_root=root)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "unsafe_runtime_path")
        self.assertFalse((external / "audio-sound").exists())
        run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_run_install_rejects_a_nested_junction_in_audio_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "repo"
            external = base / "external-site-packages"
            root.mkdir()
            external.mkdir()
            isolated_python = root / ".venv-audio" / "Scripts" / "python.exe"
            isolated_python.parent.mkdir(parents=True)
            isolated_python.write_bytes(b"placeholder")
            site_packages = root / ".venv-audio" / "Lib" / "site-packages"
            site_packages.parent.mkdir(parents=True)
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(site_packages), str(external)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")
            python_info = {
                "ok": True,
                "path": str(isolated_python),
                "version": "3.11.9",
                "deepfilternet_ok": False,
                "spectramini_runtime_ok": False,
            }
            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime", return_value=python_info
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run") as run,
            ):
                payload = run_install(repo_root=root)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "unsafe_runtime_path")
        run.assert_not_called()

    def test_run_install_uses_isolated_pip_without_target_redirection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            isolated_python = root / ".venv-audio" / "Scripts" / "python.exe"
            isolated_python.parent.mkdir(parents=True)
            isolated_python.write_bytes(b"placeholder")
            python_info = {
                "ok": True,
                "path": str(isolated_python),
                "version": "3.11.9",
                "deepfilternet_ok": False,
                "spectramini_runtime_ok": False,
            }

            def fake_run(command, **_kwargs):
                if command[-3:-1] == ["-m", "venv"]:
                    staged_python = Path(command[-1]) / "Scripts" / "python.exe"
                    staged_python.parent.mkdir(parents=True, exist_ok=True)
                    staged_python.write_bytes(b"placeholder")
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            def inspect_python(python_executable):
                return {**python_info, "path": python_executable}

            redirected_environment = {
                "PIP_TARGET": str(root.parent / "external-target"),
                "PIP_PREFIX": str(root.parent / "external-prefix"),
                "PIP_ROOT": str(root.parent / "external-root"),
                "PIP_USER": "1",
                "PYTHONHOME": str(root.parent / "external-python-home"),
                "PYTHONPATH": str(root.parent / "external-python-path"),
            }
            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime", side_effect=inspect_python
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run) as run,
                mock.patch(
                    "audio_sound.bootstrap.detect_runtime", return_value={"status": "degraded"}
                ),
                mock.patch.dict(os.environ, redirected_environment),
            ):
                payload = run_install(repo_root=root)

        self.assertTrue(payload["ok"])
        pip_calls = [call for call in run.call_args_list if "pip" in call.args[0]]
        self.assertEqual(len(pip_calls), 4)
        for call in pip_calls:
            self.assertIn("-I", call.args[0])
            for key in redirected_environment:
                self.assertNotIn(key, call.kwargs["env"])
            self.assertEqual(call.kwargs["env"]["PYTHONNOUSERSITE"], "1")
            self.assertEqual(call.kwargs["env"]["PIP_CONFIG_FILE"], os.devnull)
            self.assertEqual(call.kwargs["env"]["PIP_NO_CACHE_DIR"], "1")
            staging_root = Path(call.args[0][0]).parents[1]
            self.assertEqual(call.kwargs["env"]["TEMP"], str(staging_root / ".install-tmp"))
            self.assertEqual(call.kwargs["env"]["TMP"], str(staging_root / ".install-tmp"))
        self.assertIn("--no-cache-dir", pip_calls[0].args[0])

    def test_run_install_builds_in_unique_staging_before_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            isolated_python = root / ".venv-audio" / "Scripts" / "python.exe"
            python_info = {
                "ok": True,
                "path": "",
                "version": "3.11.9",
                "deepfilternet_ok": False,
                "spectramini_runtime_ok": False,
            }
            pip_pythons = []

            def fake_run(command, **_kwargs):
                self.assertFalse(isolated_python.exists())
                if command[-3:-1] == ["-m", "venv"]:
                    staged_python = Path(command[-1]) / "Scripts" / "python.exe"
                    staged_python.parent.mkdir(parents=True, exist_ok=True)
                    staged_python.write_bytes(b"placeholder")
                if "pip" in command:
                    pip_pythons.append(Path(command[0]))
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            def inspect_python(python_executable):
                return {**python_info, "path": python_executable}

            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime", side_effect=inspect_python
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run) as run,
                mock.patch(
                    "audio_sound.bootstrap.detect_runtime", return_value={"status": "degraded"}
                ),
            ):
                payload = run_install(repo_root=root)

            self.assertTrue(payload["ok"])
            self.assertNotIn("local_wheel", payload["data"])
            self.assertNotIn("package_index_url", payload["data"])
            self.assertTrue(isolated_python.exists())
            self.assertFalse(
                any(path.name.startswith(".venv-audio.codex-staging-") for path in root.iterdir())
            )

        self.assertEqual(len(pip_pythons), 4)
        self.assertTrue(
            all(path.parts[-3].startswith(".venv-audio.codex-staging-") for path in pip_pythons)
        )
        self.assertTrue(any("pip" in call.args[0] for call in run.call_args_list))

    def test_run_install_preinstalls_verified_local_wheel_in_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            wheel = root / "tmp" / "torch.whl"
            wheel.parent.mkdir()
            wheel.write_bytes(b"verified-wheel")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            pip_commands = []

            def fake_run(command, **_kwargs):
                if command[-3:-1] == ["-m", "venv"]:
                    staged_python = Path(command[-1]) / "Scripts" / "python.exe"
                    staged_python.parent.mkdir(parents=True, exist_ok=True)
                    staged_python.write_bytes(b"placeholder")
                if "pip" in command:
                    pip_commands.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            def inspect_python(python_executable):
                return {
                    "ok": True,
                    "path": python_executable,
                    "version": "3.11.9",
                    "deepfilternet_ok": False,
                    "spectramini_runtime_ok": False,
                }

            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime",
                    side_effect=inspect_python,
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run),
                mock.patch(
                    "audio_sound.bootstrap.detect_runtime",
                    return_value={"status": "degraded"},
                ),
            ):
                payload = run_install(
                    repo_root=root,
                    local_wheel=wheel,
                    local_wheel_sha256=digest,
                    index_url="https://mirrors.cloud.tencent.com/pypi/simple",
                )

            self.assertTrue(payload["ok"])
            self.assertEqual(len(pip_commands), 5)
            self.assertIn("--no-deps", pip_commands[0])
            self.assertIn("--require-hashes", pip_commands[0])
            self.assertEqual(
                pip_commands[1][-2:],
                ["--requirement", str(root / "requirements-audio-build.lock")],
            )
            self.assertEqual(
                pip_commands[2][-3:],
                ["--no-build-isolation", "--no-deps", "intervaltree==3.1.0"],
            )
            self.assertEqual(
                pip_commands[3][-2:],
                ["--requirement", str(root / "requirements-audio.lock")],
            )
            self.assertEqual(pip_commands[4][-1], "check")
            self.assertTrue(pip_commands[0][-1].endswith(f".install-tmp/torch.whl#sha256={digest}"))
            self.assertEqual(payload["data"]["local_wheel"]["sha256"], digest)
            self.assertEqual(
                payload["data"]["package_index_url"],
                "https://mirrors.cloud.tencent.com/pypi/simple",
            )
            self.assertFalse((root / ".venv-audio" / ".install-tmp").exists())

    def test_run_install_rejects_a_changed_staged_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            wheel = root / "tmp" / "torch.whl"
            wheel.parent.mkdir()
            wheel.write_bytes(b"verified-wheel")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()

            def fake_run(command, **_kwargs):
                if command[-3:-1] == ["-m", "venv"]:
                    staged_python = Path(command[-1]) / "Scripts" / "python.exe"
                    staged_python.parent.mkdir(parents=True, exist_ok=True)
                    staged_python.write_bytes(b"placeholder")
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            def inspect_python(python_executable):
                return {
                    "ok": True,
                    "path": python_executable,
                    "version": "3.11.9",
                    "deepfilternet_ok": False,
                    "spectramini_runtime_ok": False,
                }

            def corrupt_copy(_source, destination):
                Path(destination).write_bytes(b"changed-after-validation")
                return str(destination)

            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime",
                    side_effect=inspect_python,
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run) as run,
                mock.patch(
                    "audio_sound.bootstrap.shutil.copyfile",
                    side_effect=corrupt_copy,
                ),
            ):
                payload = run_install(
                    repo_root=root,
                    local_wheel=wheel,
                    local_wheel_sha256=digest,
                )

            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "local_wheel_staging_failed")
            self.assertTrue(payload["data"]["staging_cleanup"]["ok"])
            self.assertFalse(any("pip" in call.args[0] for call in run.call_args_list))

    def test_run_install_reports_verified_wheel_after_post_promotion_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            wheel = root / "tmp" / "torch.whl"
            wheel.parent.mkdir()
            wheel.write_bytes(b"verified-wheel")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()

            def fake_run(command, **_kwargs):
                if command[-3:-1] == ["-m", "venv"]:
                    staged_python = Path(command[-1]) / "Scripts" / "python.exe"
                    staged_python.parent.mkdir(parents=True, exist_ok=True)
                    staged_python.write_bytes(b"placeholder")
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

            def inspect_python(python_executable):
                return {
                    "ok": True,
                    "path": python_executable,
                    "version": "3.11.9",
                    "deepfilternet_ok": False,
                    "spectramini_runtime_ok": False,
                }

            real_safety_check = bootstrap._install_safety_failure

            def fail_after_promotion(repo_root, *, steps, extra_paths=()):
                if (repo_root / ".venv-audio").exists():
                    return {
                        "ok": False,
                        "code": "unsafe_runtime_path",
                        "reason": "post-promotion safety failure",
                        "data": {"steps": steps},
                    }
                return real_safety_check(repo_root, steps=steps, extra_paths=extra_paths)

            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime",
                    side_effect=inspect_python,
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run),
                mock.patch(
                    "audio_sound.bootstrap._install_safety_failure",
                    side_effect=fail_after_promotion,
                ),
            ):
                payload = run_install(
                    repo_root=root,
                    local_wheel=wheel,
                    local_wheel_sha256=digest,
                )

            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "unsafe_runtime_path")
            self.assertEqual(payload["data"]["local_wheel"]["sha256"], digest)

    def test_run_install_doctor_uses_the_current_repository_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            isolated_python = root / ".venv-audio" / "Scripts" / "python.exe"
            isolated_python.parent.mkdir(parents=True)
            isolated_python.write_bytes(b"placeholder")
            python_info = {
                "ok": True,
                "path": str(isolated_python),
                "version": "3.11.9",
                "deepfilternet_ok": False,
                "spectramini_runtime_ok": False,
            }

            def fake_run(command, **_kwargs):
                if command[-3:-1] == ["-m", "venv"]:
                    staged_python = Path(command[-1]) / "Scripts" / "python.exe"
                    staged_python.parent.mkdir(parents=True, exist_ok=True)
                    staged_python.write_bytes(b"placeholder")
                return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

            def inspect_python(python_executable):
                return {**python_info, "path": python_executable}

            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime", side_effect=inspect_python
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run),
                mock.patch(
                    "audio_sound.bootstrap.detect_runtime", return_value={"status": "degraded"}
                ) as doctor,
            ):
                payload = run_install(repo_root=root)

        self.assertTrue(payload["ok"])
        doctor.assert_called_once_with(python_executable=str(isolated_python), repo_root=root)

    def test_run_install_doctor_uses_verified_offline_ffmpeg_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ffmpeg = root / "offline" / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
            ffprobe = ffmpeg.with_name("ffprobe.exe")
            with mock.patch(
                "audio_sound.bootstrap._run_install_unlocked",
                return_value={"ok": True, "code": "ok", "data": {"runtime": {}}},
            ) as unlocked:
                payload = run_install(
                    repo_root=root,
                    ffmpeg_bin=ffmpeg,
                    ffprobe_bin=ffprobe,
                )

        self.assertTrue(payload["ok"])
        self.assertEqual(unlocked.call_args.kwargs["ffmpeg_bin"], str(ffmpeg))
        self.assertEqual(unlocked.call_args.kwargs["ffprobe_bin"], str(ffprobe))

    def test_format_runtime_report_returns_json_string(self) -> None:
        payload = {"python": {"ok": True}, "ffmpeg": {"ok": False}}
        rendered = format_runtime_report(payload)
        self.assertIn('"python"', rendered)
        self.assertIn('"ffmpeg"', rendered)

    def test_read_runtime_env_ignores_a_non_utf8_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / ".env").write_bytes(b"AUDIO_SOUND_RESPIRO_REPO=repo\xff")

            values = bootstrap._read_runtime_env(root)

        self.assertEqual(values, {})

    def test_inspect_binary_rejects_a_different_ffmpeg_tool(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 0, stdout="ffplay version 8.1.1-full_build", stderr=""
        )
        with mock.patch("audio_sound.bootstrap.subprocess.run", return_value=completed):
            payload = bootstrap._inspect_binary("C:/tools/ffmpeg.exe", expected_program="ffmpeg")

        self.assertFalse(payload["ok"])
        self.assertIn("identity", payload["error"].lower())

    def test_inspect_binary_accepts_the_expected_ffprobe_identity(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 0, stdout="ffprobe version 8.1.1-full_build", stderr=""
        )
        with mock.patch("audio_sound.bootstrap.subprocess.run", return_value=completed):
            payload = bootstrap._inspect_binary("C:/tools/ffprobe.exe", expected_program="ffprobe")

        self.assertTrue(payload["ok"])

    def test_detect_runtime_reports_a_missing_isolated_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_python = Path(tmp_dir) / ".venv-audio" / "Scripts" / "python.exe"

            payload = detect_runtime(
                python_executable=str(missing_python),
                ffmpeg_bin="missing-ffmpeg",
                ffprobe_bin="missing-ffprobe",
            )

        self.assertFalse(payload["python"]["ok"])
        self.assertEqual(payload["python"]["path"], str(missing_python))
        self.assertTrue(payload["python"]["error"])

    def test_python_runtime_probe_uses_isolated_mode(self) -> None:
        evidence = {
            "ok": True,
            "path": "audio-python",
            "version": "3.11.9",
            "deepfilternet_ok": False,
            "deepfilternet_identity": "",
            "spectramini_runtime_ok": False,
            "spectramini_identity": "",
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(evidence), stderr="")
        with mock.patch("audio_sound.bootstrap.subprocess.run", return_value=completed) as run:
            payload = bootstrap._inspect_python_runtime("audio-python")

        self.assertTrue(payload["ok"])
        self.assertEqual(run.call_args.args[0][:3], ["audio-python", "-I", "-c"])

    def test_python_runtime_probe_imports_production_spectramini_smoke_from_controlled_root(
        self,
    ) -> None:
        evidence = {
            "ok": True,
            "path": "audio-python",
            "version": "3.11.9",
            "deepfilternet_ok": False,
            "deepfilternet_identity": "",
            "spectramini_runtime_ok": False,
            "spectramini_identity": "",
            "spectramini_smoke_status": "failed",
            "spectramini_algorithm_identity": "auto_cut_spectramini_style_smoke_v1",
            "spectramini_smoke_checks": {},
            "spectramini_smoke_metrics": {},
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(evidence), stderr="")
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with mock.patch("audio_sound.bootstrap.subprocess.run", return_value=completed) as run:
                bootstrap._inspect_python_runtime("audio-python", repo_root=root)

        probe_command = run.call_args.args[0]
        self.assertEqual(probe_command[:3], ["audio-python", "-I", "-c"])
        self.assertEqual(probe_command[4], str(root.resolve()))
        self.assertIn(
            "from audio_sound.pipeline import run_spectramini_style_smoke",
            probe_command[3],
        )
        self.assertIn("run_spectramini_style_smoke()", probe_command[3])
        self.assertNotIn("def spectramini_style_process", probe_command[3])

    def test_python_runtime_probe_rejects_import_only_spectramini_readiness(self) -> None:
        import_only_evidence = {
            "ok": True,
            "path": "audio-python",
            "version": "3.11.9",
            "deepfilternet_ok": False,
            "deepfilternet_identity": "",
            "spectramini_runtime_ok": True,
            "spectramini_identity": "librosa@1;numpy@1;scipy@1;soundfile@1",
        }
        completed = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(import_only_evidence), stderr=""
        )
        with mock.patch("audio_sound.bootstrap.subprocess.run", return_value=completed):
            payload = bootstrap._inspect_python_runtime("audio-python")

        self.assertFalse(payload["spectramini_runtime_ok"])
        self.assertEqual(payload["spectramini_smoke_status"], "evidence_missing")

    def test_detect_runtime_reports_spectramini_smoke_evidence(self) -> None:
        python_info = {
            "ok": True,
            "path": "audio-python",
            "version": "3.11.9",
            "deepfilternet_ok": False,
            "spectramini_runtime_ok": True,
            "spectramini_identity": "local-spectramini",
            "spectramini_smoke_status": "passed",
            "spectramini_algorithm_identity": "auto_cut_spectramini_style_smoke_v1",
            "spectramini_smoke_checks": {"click_peak_reduced": True},
            "spectramini_smoke_metrics": {"sample_count": 1024},
        }
        with (
            mock.patch("audio_sound.bootstrap._inspect_python_runtime", return_value=python_info),
            mock.patch("audio_sound.bootstrap.shutil.which", return_value=None),
        ):
            payload = detect_runtime(python_executable="audio-python")

        self.assertEqual(payload["spectramini"]["smoke_status"], "passed")
        self.assertEqual(
            payload["spectramini"]["algorithm_identity"],
            "auto_cut_spectramini_style_smoke_v1",
        )
        self.assertEqual(
            payload["spectramini"]["smoke_checks"],
            {"click_peak_reduced": True},
        )
        self.assertEqual(
            payload["spectramini"]["smoke_metrics"],
            {"sample_count": 1024},
        )

    def test_detect_runtime_rejects_python_outside_repo_audio_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            external_python = root.parent / "Python311" / "python.exe"
            with mock.patch("audio_sound.bootstrap._inspect_python_runtime") as inspect_python:
                payload = detect_runtime(
                    python_executable=str(external_python),
                    ffmpeg_bin="missing-ffmpeg",
                    ffprobe_bin="missing-ffprobe",
                    repo_root=root,
                )

        self.assertFalse(payload["python"]["ok"])
        self.assertIn(".venv-audio", payload["python"]["error"])
        inspect_python.assert_not_called()

    def test_trusted_adapter_executes_the_verified_bytes_from_stdin(self) -> None:
        adapter_bytes = b"print('trusted adapter')\n"
        completed = subprocess.CompletedProcess([], 0, stdout=b'{"ok": true}', stderr=b"")
        with mock.patch("audio_sound.bootstrap.subprocess.run", return_value=completed) as run:
            probe = bootstrap._run_trusted_adapter(
                python_executable="audio-python",
                adapter_bytes=adapter_bytes,
                display_path=Path("D:/repo/audio_sound/adapter.py"),
                arguments=["--self-check", "--json"],
                cwd=Path("D:/repo"),
                timeout=30,
            )

        self.assertEqual(probe.returncode, 0)
        self.assertEqual(run.call_args.args[0][:3], ["audio-python", "-I", "-c"])
        self.assertEqual(run.call_args.kwargs["input"], adapter_bytes)
        self.assertNotEqual(run.call_args.args[0][1], "D:/repo/audio_sound/adapter.py")

    def test_trusted_adapter_runner_preserves_script_argv_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            display_path = Path(tmp_dir) / "audio_sound" / "adapter.py"
            source = b"import json, sys\nprint(json.dumps({'argv': sys.argv, 'file': __file__}))\n"
            completed = bootstrap._run_trusted_adapter(
                python_executable=sys.executable,
                adapter_bytes=source,
                display_path=display_path,
                arguments=["--self-check", "--json"],
                cwd=Path(tmp_dir),
                timeout=30,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["argv"], [str(display_path), "--self-check", "--json"])
        self.assertEqual(payload["file"], str(display_path))

    def test_trusted_repo_file_never_consults_parent_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            parent = Path(tmp_dir) / "parent"
            root = parent / "extracted-package"
            (parent / ".git").mkdir(parents=True)
            adapter = root / "audio_sound" / "adapter.py"
            adapter.parent.mkdir(parents=True)
            adapter.write_bytes(b"trusted-looking-bytes")
            with mock.patch(
                "audio_sound.bootstrap.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout=adapter.read_bytes(), stderr=b""
                ),
            ) as run:
                content, error = bootstrap._read_trusted_repo_file(
                    root,
                    Path("audio_sound") / "adapter.py",
                )

        self.assertIsNone(content)
        self.assertIn("package-local .git", error)
        run.assert_not_called()

    def test_detect_runtime_never_claims_unverified_respiro_assets_are_ready(self) -> None:
        python_info = {
            "ok": True,
            "path": "audio-python",
            "version": "3.11.9",
            "deepfilternet_ok": True,
            "respiro_runtime_ok": True,
            "spectramini_runtime_ok": True,
        }
        with (
            mock.patch("audio_sound.bootstrap._inspect_python_runtime", return_value=python_info),
            mock.patch(
                "audio_sound.bootstrap.shutil.which",
                return_value=None,
            ),
        ):
            payload = detect_runtime(python_executable="audio-python")

        self.assertFalse(payload["respiro_en"]["ok"])
        self.assertIn("verified", payload["respiro_en"]["notes"].lower())
        self.assertFalse(payload["deepfilternet"]["ok"])
        self.assertTrue(payload["spectramini"]["ok"])

    def test_detect_runtime_reports_degraded_status_and_component_identities(self) -> None:
        python_info = {
            "ok": True,
            "path": "audio-python",
            "version": "3.11.9",
            "deepfilternet_ok": False,
            "spectramini_runtime_ok": True,
        }
        with (
            mock.patch("audio_sound.bootstrap._inspect_python_runtime", return_value=python_info),
            mock.patch(
                "audio_sound.bootstrap.shutil.which",
                side_effect=lambda name: f"C:/tools/{name}.exe",
            ),
            mock.patch(
                "audio_sound.bootstrap._inspect_binary",
                side_effect=lambda path, **_kwargs: {
                    "ok": True,
                    "path": path,
                    "version": "",
                    "identity": path,
                    "error": "",
                },
            ),
        ):
            payload = detect_runtime(python_executable="audio-python")

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["python"]["identity"], "python@3.11.9")
        self.assertEqual(payload["ffmpeg"]["identity"], "C:/tools/ffmpeg.exe")
        self.assertEqual(payload["ffprobe"]["identity"], "C:/tools/ffprobe.exe")

    def test_detect_runtime_reports_unavailable_when_required_runtime_is_missing(self) -> None:
        python_info = {
            "ok": False,
            "path": "missing-python",
            "version": "",
            "deepfilternet_ok": False,
            "spectramini_runtime_ok": False,
        }
        with (
            mock.patch("audio_sound.bootstrap._inspect_python_runtime", return_value=python_info),
            mock.patch(
                "audio_sound.bootstrap.shutil.which",
                return_value=None,
            ),
        ):
            payload = detect_runtime(python_executable="missing-python")

        self.assertEqual(payload["status"], "unavailable")

    def test_detect_runtime_requires_python_311_for_an_available_audio_runtime(self) -> None:
        python_info = {
            "ok": True,
            "path": "audio-python",
            "version": "3.10.14",
            "deepfilternet_ok": False,
            "spectramini_runtime_ok": True,
        }
        with (
            mock.patch("audio_sound.bootstrap._inspect_python_runtime", return_value=python_info),
            mock.patch(
                "audio_sound.bootstrap.shutil.which",
                side_effect=lambda name: f"C:/tools/{name}.exe",
            ),
        ):
            payload = detect_runtime(python_executable="audio-python")

        self.assertFalse(payload["python"]["ok"])
        self.assertFalse(payload["spectramini"]["ok"])
        self.assertEqual(payload["status"], "unavailable")

    def test_detect_runtime_records_executable_binary_versions(self) -> None:
        python_info = {
            "ok": True,
            "path": "audio-python",
            "version": "3.11.9",
            "deepfilternet_ok": False,
            "spectramini_runtime_ok": True,
        }
        completed = [
            subprocess.CompletedProcess([], 0, stdout="ffmpeg version 8.1.1\nrest", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="ffprobe version 8.1.1\nrest", stderr=""),
        ]
        with (
            mock.patch("audio_sound.bootstrap._inspect_python_runtime", return_value=python_info),
            mock.patch(
                "audio_sound.bootstrap.shutil.which",
                side_effect=["C:/tools/ffmpeg.exe", "C:/tools/ffprobe.exe"],
            ),
            mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=completed),
        ):
            payload = detect_runtime(python_executable="audio-python")

        self.assertEqual(payload["ffmpeg"]["version"], "ffmpeg version 8.1.1")
        self.assertEqual(payload["ffprobe"]["version"], "ffprobe version 8.1.1")
        self.assertEqual(payload["ffmpeg"]["identity"], "ffmpeg@ffmpeg version 8.1.1")

    def test_detect_runtime_records_respiro_static_assets_without_claiming_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            audio_python = root / ".venv-audio" / "Scripts" / "python.exe"
            respiro_repo = root / "tools" / "Respiro-en"
            respiro_repo.mkdir(parents=True)
            revision = "a" * 40
            git_dir = respiro_repo / ".git"
            git_dir.mkdir()
            (git_dir / "HEAD").write_text(revision + "\n", encoding="ascii")
            weights = root / "tools" / "respiro-en.pt"
            weights.write_bytes(b"verified-weights")
            adapter = root / "audio_sound" / "respiro_adapter.py"
            adapter.parent.mkdir(parents=True)
            adapter.write_text("print('ok')\n", encoding="utf-8")
            manifest_path = root / "audio_sound" / "runtime-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "assets": {
                            "respiro_en": {
                                "repo_path": "tools/Respiro-en",
                                "revision": revision,
                                "license": "MIT",
                                "weights_path": "tools/respiro-en.pt",
                                "weights_size": weights.stat().st_size,
                                "weights_sha256": hashlib.sha256(weights.read_bytes()).hexdigest(),
                                "adapter_path": "audio_sound/respiro_adapter.py",
                                "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
                                "adapter_version": "adapter-1",
                                "probe_command": ["dangerous-executable"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "AUDIO_SOUND_RESPIRO_REPO=tools/Respiro-en\n"
                "AUDIO_SOUND_RESPIRO_WEIGHTS=tools/respiro-en.pt\n",
                encoding="utf-8",
            )
            python_info = {
                "ok": True,
                "path": str(audio_python),
                "version": "3.11.9",
                "deepfilternet_ok": False,
                "spectramini_runtime_ok": True,
            }

            observed_commands = []

            def fake_run(command, **kwargs):
                observed_commands.append(command)
                if command[0] == "git":
                    raise AssertionError("doctor must not run Git in an external repository")
                if command[:3] == [str(audio_python), "-I", "-c"]:
                    raise AssertionError("doctor must never execute a Respiro adapter")
                binary_name = Path(command[0]).stem.lower()
                return subprocess.CompletedProcess(
                    command, 0, stdout=f"{binary_name} version 8.1.1", stderr=""
                )

            def trusted_file(_root, relative_path):
                return (root / relative_path).read_bytes(), ""

            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime", return_value=python_info
                ),
                mock.patch(
                    "audio_sound.bootstrap.shutil.which",
                    side_effect=lambda name: f"C:/tools/{name}.exe",
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run),
                mock.patch(
                    "audio_sound.bootstrap._read_trusted_repo_file",
                    side_effect=trusted_file,
                ),
            ):
                payload = detect_runtime(
                    python_executable=str(audio_python),
                    repo_root=root,
                )

        self.assertFalse(payload["respiro_en"]["ok"])
        self.assertFalse(payload["respiro_en"]["asset_verification_ok"])
        self.assertTrue(payload["respiro_en"]["static_assets_ok"])
        self.assertEqual(
            payload["respiro_en"]["execution_status"],
            "external_unavailable",
        )
        self.assertIn(revision, payload["respiro_en"]["identity"])
        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(
            any(command[:3] == [str(audio_python), "-I", "-c"] for command in observed_commands)
        )
        self.assertFalse(any(command[0] == "git" for command in observed_commands))
        self.assertNotIn(["dangerous-executable"], observed_commands)

    def test_python_runtime_probe_does_not_import_deepfilternet_code(self) -> None:
        observed: dict[str, list[str]] = {}

        def fake_run(command, **_kwargs):
            observed["command"] = command
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "path": "audio-python",
                        "version": "3.11.9",
                        "deepfilternet_ok": True,
                        "deepfilternet_identity": "deepfilternet@0.5.6",
                        "spectramini_runtime_ok": True,
                        "spectramini_identity": "local",
                    }
                ),
                stderr="",
            )

        with mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run):
            payload = bootstrap._inspect_python_runtime("audio-python")

        self.assertTrue(payload["deepfilternet_ok"])
        probe_script = observed["command"][3]
        self.assertNotIn('probe_module("df.enhance")', probe_script)
        self.assertNotIn("importlib.import_module('df.enhance')", probe_script)
        self.assertIn('package_version("deepfilternet")', probe_script)

    def test_respiro_reports_an_untrusted_adapter_before_missing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            section = {
                "_env": {},
                "_manifest_path": str(root / "audio_sound" / "runtime-manifest.json"),
                "adapter_path": "../untrusted-adapter.py",
            }
            with mock.patch(
                "audio_sound.bootstrap._load_runtime_asset_section",
                return_value=(section, ""),
            ):
                payload = bootstrap._verify_respiro_runtime(
                    repo_root=root,
                    python_executable="audio-python",
                )

        self.assertFalse(payload["ok"])
        self.assertIn("not under audio_sound", payload["notes"])

    def test_respiro_rejects_adapter_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            section = {
                "_env": {},
                "_manifest_path": str(root / "audio_sound" / "runtime-manifest.json"),
                "adapter_path": "audio_sound/../tracked-but-untrusted.py",
            }
            with (
                mock.patch(
                    "audio_sound.bootstrap._load_runtime_asset_section",
                    return_value=(section, ""),
                ),
                mock.patch(
                    "audio_sound.bootstrap._read_trusted_repo_file",
                    return_value=(b"untrusted", ""),
                ) as trusted_file,
            ):
                payload = bootstrap._verify_respiro_runtime(
                    repo_root=root,
                    python_executable="audio-python",
                )

        self.assertFalse(payload["ok"])
        self.assertIn("not under audio_sound", payload["notes"])
        trusted_file.assert_not_called()

    def test_respiro_static_verification_never_runs_repo_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            repo = root / "tools" / "Respiro-en"
            repo.mkdir(parents=True)
            revision = "b" * 40
            git_dir = repo / ".git"
            git_dir.mkdir()
            (git_dir / "HEAD").write_text(revision + "\n", encoding="ascii")
            (git_dir / "config").write_text(
                '[filter "unsafe"]\n\tprocess = should-never-run.exe\n',
                encoding="utf-8",
            )
            (repo / ".gitattributes").write_text("*.py filter=unsafe\n", encoding="utf-8")
            (repo / "injected.py").write_text("print('dirty')\n", encoding="utf-8")
            weights = root / "tools" / "respiro-en.pt"
            weights.write_bytes(b"weights")
            adapter = root / "audio_sound" / "respiro_adapter.py"
            adapter.parent.mkdir(parents=True)
            adapter_bytes = b"print('adapter')\n"
            adapter.write_bytes(adapter_bytes)
            weights_sha = hashlib.sha256(weights.read_bytes()).hexdigest()
            section = {
                "_env": {
                    "AUDIO_SOUND_RESPIRO_REPO": str(repo),
                    "AUDIO_SOUND_RESPIRO_WEIGHTS": str(weights),
                },
                "_manifest_path": str(root / "audio_sound" / "runtime-manifest.json"),
                "revision": revision,
                "license": "MIT",
                "weights_size": weights.stat().st_size,
                "weights_sha256": weights_sha,
                "adapter_path": "audio_sound/respiro_adapter.py",
                "adapter_sha256": hashlib.sha256(adapter_bytes).hexdigest(),
                "adapter_version": "adapter-1",
            }

            with (
                mock.patch(
                    "audio_sound.bootstrap._load_runtime_asset_section",
                    return_value=(section, ""),
                ),
                mock.patch(
                    "audio_sound.bootstrap._read_trusted_repo_file",
                    return_value=(adapter_bytes, ""),
                ),
                mock.patch(
                    "audio_sound.bootstrap.subprocess.run",
                    side_effect=AssertionError("external repository command executed"),
                ),
            ):
                payload = bootstrap._verify_respiro_runtime(
                    repo_root=root,
                    python_executable="audio-python",
                )

        self.assertFalse(payload["ok"])
        self.assertTrue(payload["static_assets_ok"])
        self.assertIn("not verified", payload["notes"].lower())
        self.assertIn(revision, payload["identity"])

    def test_detect_runtime_degrades_for_malformed_runtime_manifest_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            audio_python = root / ".venv-audio" / "Scripts" / "python.exe"
            model = root / "tools" / "deepfilter-model.bin"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"model")
            adapter = root / "audio_sound" / "deepfilter_adapter.py"
            adapter.parent.mkdir(parents=True)
            adapter.write_text("print('ok')\n", encoding="utf-8")
            manifest_path = root / "audio_sound" / "runtime-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "assets": {
                            "deepfilternet": {
                                "path": "tools/deepfilter-model.bin",
                                "size": "not-an-integer",
                                "sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                                "version": "0.5.6",
                                "license": "MIT",
                                "adapter_path": "audio_sound/deepfilter_adapter.py",
                                "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
                                "adapter_version": "adapter-1",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            python_info = {
                "ok": True,
                "path": str(audio_python),
                "version": "3.11.9",
                "deepfilternet_ok": True,
                "deepfilternet_identity": "deepfilternet@0.5.6",
                "spectramini_runtime_ok": True,
            }
            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime", return_value=python_info
                ),
                mock.patch(
                    "audio_sound.bootstrap.shutil.which",
                    side_effect=lambda name: f"C:/tools/{name}.exe",
                ),
                mock.patch(
                    "audio_sound.bootstrap._inspect_binary",
                    side_effect=lambda path, **_kwargs: {
                        "ok": True,
                        "path": path,
                        "version": "",
                        "identity": path,
                        "error": "",
                    },
                ),
                mock.patch(
                    "audio_sound.bootstrap._read_trusted_repo_file",
                    side_effect=lambda _root, relative: ((root / relative).read_bytes(), ""),
                ),
            ):
                payload = detect_runtime(python_executable=str(audio_python), repo_root=root)

        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["deepfilternet"]["ok"])
        self.assertIn("invalid", payload["deepfilternet"]["notes"].lower())

    def test_detect_runtime_statically_verifies_deepfilternet_without_loading_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            audio_python = root / ".venv-audio" / "Scripts" / "python.exe"
            model = root / "tools" / "deepfilter-model.bin"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"verified-model")
            adapter = root / "audio_sound" / "deepfilter_adapter.py"
            adapter.parent.mkdir(parents=True)
            adapter.write_text("print('ok')\n", encoding="utf-8")
            manifest_path = root / "audio_sound" / "runtime-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "assets": {
                            "deepfilternet": {
                                "path": "tools/deepfilter-model.bin",
                                "size": model.stat().st_size,
                                "sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                                "version": "model-1",
                                "license": "MIT",
                                "adapter_path": "audio_sound/deepfilter_adapter.py",
                                "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
                                "adapter_version": "adapter-1",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            python_info = {
                "ok": True,
                "path": str(audio_python),
                "version": "3.11.9",
                "deepfilternet_ok": True,
                "deepfilternet_identity": "deepfilternet@0.5.6",
                "spectramini_runtime_ok": True,
            }
            observed_commands = []

            def fake_run(command, **kwargs):
                observed_commands.append(command)
                if command[0].endswith("ffmpeg.exe"):
                    return subprocess.CompletedProcess(
                        command, 0, stdout="ffmpeg version 8.1.1", stderr=""
                    )
                if command[0].endswith("ffprobe.exe"):
                    return subprocess.CompletedProcess(
                        command, 0, stdout="ffprobe version 8.1.1", stderr=""
                    )
                if command[:3] == [str(audio_python), "-I", "-c"]:
                    raise AssertionError("doctor must never execute a DeepFilterNet adapter")
                raise AssertionError(f"unexpected command: {command}")

            with (
                mock.patch(
                    "audio_sound.bootstrap._inspect_python_runtime", return_value=python_info
                ),
                mock.patch(
                    "audio_sound.bootstrap.shutil.which",
                    side_effect=lambda name: f"C:/tools/{name}.exe",
                ),
                mock.patch("audio_sound.bootstrap.subprocess.run", side_effect=fake_run),
                mock.patch(
                    "audio_sound.bootstrap._read_trusted_repo_file",
                    side_effect=lambda _root, relative: ((root / relative).read_bytes(), ""),
                ),
            ):
                payload = detect_runtime(python_executable=str(audio_python), repo_root=root)

        self.assertFalse(payload["deepfilternet"]["ok"])
        self.assertTrue(payload["deepfilternet"]["asset_verification_ok"])
        self.assertEqual(
            payload["deepfilternet"]["execution_status"],
            "external_unavailable",
        )
        self.assertTrue(payload["deepfilternet"]["adapter_ok"])
        self.assertFalse(
            any(command[:3] == [str(audio_python), "-I", "-c"] for command in observed_commands)
        )

    @unittest.skipUnless(os.name == "nt", "Windows lock regression")
    def test_cleanup_lock_path_normalizes_windows_path_case(self) -> None:
        upper = bootstrap._cleanup_lock_path(Path("C:/Work/Auto-Cut"))
        lower = bootstrap._cleanup_lock_path(Path("c:/work/auto-cut"))

        self.assertEqual(upper, lower)

    def test_staging_promotion_restores_previous_environment_after_final_scan_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / ".venv-audio"
            staging = root / ".venv-audio.codex-staging-test"
            target.mkdir()
            staging.mkdir()
            (target / "old.bin").write_bytes(b"old")
            (staging / "new.bin").write_bytes(b"new")
            real_tree_size = bootstrap._safe_tree_size
            target_scans = 0

            def fail_final_target_scan(path):
                nonlocal target_scans
                if path == target:
                    target_scans += 1
                    if target_scans == 2:
                        return None
                return real_tree_size(path)

            with mock.patch(
                "audio_sound.bootstrap._safe_tree_size",
                side_effect=fail_final_target_scan,
            ):
                payload = bootstrap._promote_install_staging(staging, root=root)

            rejected_path = Path(payload["rejected_path"])
            self.assertTrue((target / "old.bin").exists())
            self.assertFalse((target / "new.bin").exists())
            self.assertTrue((rejected_path / "new.bin").exists())

        self.assertFalse(payload["ok"])
        self.assertIn("final", payload["error"].lower())

    @unittest.skipUnless(os.name == "nt", "Windows lock regression")
    def test_cleanup_mutex_locks_before_entering_without_writing(self) -> None:
        import msvcrt

        events = []

        def record_lock(_fd, mode, _length):
            events.append("unlock" if mode == msvcrt.LK_UNLCK else "lock")

        with (
            mock.patch("audio_sound.bootstrap.os.open", return_value=41),
            mock.patch(
                "audio_sound.bootstrap.os.close", side_effect=lambda _fd: events.append("close")
            ),
            mock.patch("audio_sound.bootstrap.os.lseek"),
            mock.patch("audio_sound.bootstrap.os.write") as write,
            mock.patch("msvcrt.locking", side_effect=record_lock),
        ):
            with bootstrap._cleanup_mutex(Path("C:/Work/Auto-Cut")):
                events.append("entered")

        write.assert_not_called()
        self.assertEqual(events, ["lock", "entered", "unlock", "close"])

    @unittest.skipUnless(os.name == "nt", "Windows lock regression")
    def test_cleanup_mutex_does_not_unlock_when_lock_acquisition_fails(self) -> None:
        with (
            mock.patch("audio_sound.bootstrap.os.open", return_value=41),
            mock.patch("audio_sound.bootstrap.os.close") as close,
            mock.patch("audio_sound.bootstrap.os.lseek"),
            mock.patch("audio_sound.bootstrap.os.write"),
            mock.patch("msvcrt.locking", side_effect=OSError("busy")) as locking,
        ):
            with self.assertRaisesRegex(OSError, "busy"):
                with bootstrap._cleanup_mutex(Path("C:/Work/Auto-Cut")):
                    self.fail("mutex entered without acquiring the lock")

        self.assertEqual(locking.call_count, 1)
        close.assert_called_once_with(41)

    @unittest.skipUnless(os.name == "nt", "Windows lock regression")
    def test_cleanup_mutex_closes_descriptor_when_unlock_fails(self) -> None:
        import msvcrt

        def lock_then_fail(_fd, mode, _length):
            if mode == msvcrt.LK_UNLCK:
                raise OSError("unlock failed")

        with (
            mock.patch("audio_sound.bootstrap.os.open", return_value=41),
            mock.patch("audio_sound.bootstrap.os.close") as close,
            mock.patch("audio_sound.bootstrap.os.lseek"),
            mock.patch("msvcrt.locking", side_effect=lock_then_fail),
        ):
            with self.assertRaisesRegex(OSError, "unlock failed"):
                with bootstrap._cleanup_mutex(Path("C:/Work/Auto-Cut")):
                    pass

        close.assert_called_once_with(41)

    def test_prune_workspace_removes_only_audio_owned_generated_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "output" / "run-1").mkdir(parents=True)
            (root / "output" / "run-1" / "audio.wav").write_bytes(b"wav")
            (root / "scratch" / "audio-sound").mkdir(parents=True)
            (root / "scratch" / "audio-sound" / "draft.wav").write_bytes(b"draft")
            (root / "audio_sound" / "__pycache__").mkdir(parents=True)
            (root / "audio_sound" / "__pycache__" / "pipeline.cpython-311.pyc").write_bytes(
                b"cache"
            )
            (root / ".venv" / "Lib").mkdir(parents=True)
            (root / ".venv" / "Lib" / "keep.pyc").write_bytes(b"venv")
            (root / ".venv-audio" / "Lib").mkdir(parents=True)
            (root / ".venv-audio" / "Lib" / "remove.pyc").write_bytes(b"audio-venv")
            (root / ".worktrees" / "keep").mkdir(parents=True)
            (root / ".omx" / "keep").mkdir(parents=True)
            (root / "tools" / "audio_sound_runtime").mkdir(parents=True)
            (root / "tools" / "audio_sound_runtime" / "model.bin").write_bytes(b"model")
            (root / "README.md").write_text("keep", encoding="utf-8")

            payload = prune_workspace(repo_root=root, dry_run=False)

            self.assertTrue((root / "output" / "run-1" / "audio.wav").exists())
            self.assertFalse((root / "scratch" / "audio-sound").exists())
            self.assertFalse((root / "audio_sound" / "__pycache__").exists())
            self.assertTrue((root / ".venv" / "Lib" / "keep.pyc").exists())
            self.assertFalse((root / ".venv-audio").exists())
            self.assertTrue((root / ".worktrees" / "keep").exists())
            self.assertTrue((root / ".omx" / "keep").exists())
            self.assertFalse((root / "tools" / "audio_sound_runtime").exists())
            self.assertTrue((root / "README.md").exists())
            self.assertEqual(payload["removed_count"], 4)
            self.assertEqual(payload["bytes_reclaimed"], 25)

    def test_prune_workspace_marks_dry_run_bytes_as_estimated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "scratch" / "audio-sound"
            target.mkdir(parents=True)
            (target / "draft.wav").write_bytes(b"draft")

            payload = prune_workspace(repo_root=root, dry_run=True)
            self.assertTrue(target.exists())

        self.assertTrue(payload["estimated"])
        self.assertEqual(payload["removed_count"], 1)

    def test_prune_workspace_dry_run_reports_skipped_targets_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "scratch" / "audio-sound"
            target.mkdir(parents=True)
            with mock.patch("audio_sound.bootstrap._safe_tree_size", return_value=None):
                payload = prune_workspace(repo_root=root, dry_run=True)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "partial")
        self.assertIn(str(target), payload["skipped_targets"])

    def test_prune_workspace_reports_bytes_deleted_before_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "scratch" / "audio-sound"
            target.mkdir(parents=True)
            first = target / "first.bin"
            second = target / "second.bin"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            def remove_one_then_fail(quarantine, *, root):
                (quarantine / "first.bin").unlink()
                return False

            with mock.patch(
                "audio_sound.bootstrap._remove_tree_without_reparse",
                side_effect=remove_one_then_fail,
            ):
                payload = prune_workspace(repo_root=root, dry_run=False)

            self.assertFalse(first.exists())
            self.assertTrue(second.exists())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["bytes_reclaimed"], len(b"first"))
        self.assertIn("partial", payload["errors"][0]["error"].lower())

    def test_prune_workspace_skips_directory_symlink_to_external_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "repo"
            external = base / "external-symlink-target"
            root.mkdir()
            external.mkdir()
            payload_file = external / "keep.bin"
            payload_file.write_bytes(b"external")
            link = root / ".venv-audio"
            try:
                link.symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            payload = prune_workspace(repo_root=root, dry_run=False)

            self.assertTrue(payload_file.exists())
            self.assertTrue(os.path.lexists(link))
            self.assertIn(str(link), payload["skipped_targets"])

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "partial")

    def test_prune_workspace_reports_a_quarantine_that_cannot_be_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "scratch" / "audio-sound"
            target.mkdir(parents=True)
            (target / "generated.bin").write_bytes(b"generated")

            def fail_removal(_quarantine, *, root):
                target.mkdir(parents=True)
                return False

            with mock.patch(
                "audio_sound.bootstrap._remove_tree_without_reparse",
                side_effect=fail_removal,
            ):
                payload = prune_workspace(repo_root=root, dry_run=False)

            quarantined_targets = [Path(path) for path in payload["quarantined_targets"]]
            self.assertEqual(len(quarantined_targets), 1)
            self.assertTrue(quarantined_targets[0].exists())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "partial")
        self.assertTrue(payload["errors"])
        self.assertIn("rollback", payload["errors"][0]["error"].lower())

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_prune_workspace_skips_junction_to_external_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "repo"
            external = base / "external-junction-target"
            root.mkdir()
            external.mkdir()
            payload_file = external / "keep.bin"
            payload_file.write_bytes(b"external")
            junction = root / ".venv-audio"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")

            payload = prune_workspace(repo_root=root, dry_run=False)

            self.assertTrue(payload_file.exists())
            self.assertTrue(os.path.lexists(junction))
            self.assertIn(str(junction), payload["skipped_targets"])

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_prune_workspace_skips_junction_cache_inside_code_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "repo"
            code_root = root / "audio_sound"
            external = base / "external-cache-target"
            code_root.mkdir(parents=True)
            external.mkdir()
            payload_file = external / "keep.pyc"
            payload_file.write_bytes(b"external-cache")
            junction = code_root / "__pycache__"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")

            payload = prune_workspace(repo_root=root, dry_run=False)

            self.assertTrue(payload_file.exists())
            self.assertTrue(os.path.lexists(junction))
            self.assertIn(str(junction), payload["skipped_targets"])

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_prune_workspace_skips_target_replaced_by_junction_after_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "repo"
            target = root / "scratch" / "audio-sound"
            external = base / "external-race-target"
            target.mkdir(parents=True)
            (target / "generated.bin").write_bytes(b"generated")
            external.mkdir()
            external_file = external / "keep.bin"
            external_file.write_bytes(b"external")
            real_safe_tree_size = bootstrap._safe_tree_size
            target_scans = 0

            def replace_after_scan(path):
                nonlocal target_scans
                size = real_safe_tree_size(path)
                if path == target:
                    target_scans += 1
                    if target_scans == 2:
                        shutil.rmtree(target)
                        created = subprocess.run(
                            ["cmd", "/c", "mklink", "/J", str(target), str(external)],
                            capture_output=True,
                            check=False,
                            text=True,
                        )
                        if created.returncode != 0:
                            self.skipTest(
                                f"cannot create junction: {created.stderr or created.stdout}"
                            )
                return size

            with mock.patch(
                "audio_sound.bootstrap._safe_tree_size", side_effect=replace_after_scan
            ):
                payload = prune_workspace(repo_root=root, dry_run=False)

            self.assertTrue(external_file.exists())
            self.assertTrue(os.path.lexists(target))
            self.assertIn(str(target), payload["skipped_targets"])

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_recursive_removal_never_follows_child_replaced_by_junction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "repo"
            target = root / "scratch" / "audio-sound"
            child = target / "child"
            external = base / "external"
            child.mkdir(parents=True)
            (child / "generated.bin").write_bytes(b"generated")
            external.mkdir()
            external_file = external / "keep.bin"
            external_file.write_bytes(b"keep")
            real_is_reparse = bootstrap._is_reparse_point
            replaced = False

            def replace_child_after_check(path):
                nonlocal replaced
                result = real_is_reparse(path)
                if path == child and not result and not replaced:
                    replaced = True
                    shutil.rmtree(child)
                    created = subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(child), str(external)],
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                    if created.returncode != 0:
                        self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")
                return result

            with mock.patch(
                "audio_sound.bootstrap._is_reparse_point",
                side_effect=replace_child_after_check,
            ):
                removed = bootstrap._remove_tree_without_reparse(target, root=root)

            self.assertFalse(removed)
            self.assertTrue(external_file.exists())
            self.assertTrue(os.path.lexists(child))
            child.rmdir()

    def test_prune_workspace_skips_target_that_disappears_after_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "scratch" / "audio-sound"
            target.mkdir(parents=True)
            (target / "generated.bin").write_bytes(b"generated")
            real_safe_tree_size = bootstrap._safe_tree_size
            target_scans = 0

            def remove_after_scan(path):
                nonlocal target_scans
                size = real_safe_tree_size(path)
                if path == target:
                    target_scans += 1
                    if target_scans == 2:
                        shutil.rmtree(target)
                return size

            with mock.patch("audio_sound.bootstrap._safe_tree_size", side_effect=remove_after_scan):
                payload = prune_workspace(repo_root=root, dry_run=False)

            self.assertFalse(target.exists())
            self.assertIn(str(target), payload["skipped_targets"])

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_prune_workspace_rejects_a_junction_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            real_root = base / "real-repo"
            real_root.mkdir()
            keep_file = real_root / "scratch" / "audio-sound" / "keep.wav"
            keep_file.parent.mkdir(parents=True)
            keep_file.write_bytes(b"keep")
            lexical_root = base / "repo-link"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(lexical_root), str(real_root)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")

            payload = prune_workspace(repo_root=lexical_root, dry_run=False)

            self.assertTrue(keep_file.exists())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "unsafe_repo_root")

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_prune_workspace_rejects_a_parent_junction_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            real_parent = base / "real-parent"
            real_root = real_parent / "repo"
            real_root.mkdir(parents=True)
            keep_file = real_root / "tools" / "audio_sound_runtime" / "keep.bin"
            keep_file.parent.mkdir(parents=True)
            keep_file.write_bytes(b"keep")
            lexical_parent = base / "parent-link"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(lexical_parent), str(real_parent)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")

            payload = prune_workspace(repo_root=lexical_parent / "repo", dry_run=False)

            self.assertTrue(keep_file.exists())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "unsafe_repo_root")

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_prune_workspace_rechecks_repo_root_after_acquiring_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "repo"
            original = base / "original-repo"
            external = base / "external-root-target"
            root.mkdir()
            external_keep = external / "scratch" / "audio-sound" / "keep.wav"
            external_keep.parent.mkdir(parents=True)
            external_keep.write_bytes(b"keep")

            @contextmanager
            def swap_root_after_lock(_root):
                root.rename(original)
                created = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(root), str(external)],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                if created.returncode != 0:
                    self.skipTest(f"cannot create junction: {created.stderr or created.stdout}")
                try:
                    yield
                finally:
                    if os.path.lexists(root):
                        root.rmdir()
                    original.rename(root)

            with mock.patch(
                "audio_sound.bootstrap._cleanup_mutex", side_effect=swap_root_after_lock
            ):
                payload = prune_workspace(repo_root=root, dry_run=False)

            self.assertTrue(external_keep.exists())

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "unsafe_repo_root")

    def test_unverified_respiro_download_commands_are_disabled(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "verified runtime manifest"):
            build_respiro_setup_commands(
                repo_root="S:/Agent/Auto jianji/Auto-Cut",
                tools_dir="S:/Agent/Auto jianji/Auto-Cut/tools/audio_sound_runtime",
            )

    def test_respiro_setup_returns_a_structured_disabled_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            payload = run_respiro_setup(
                repo_root=root,
                tools_dir=root / "tools" / "audio_sound_runtime",
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "unverified_asset_install_disabled")
        self.assertNotIn("steps", payload.get("data", {}))


if __name__ == "__main__":
    unittest.main()
