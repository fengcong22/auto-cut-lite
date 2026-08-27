from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = (
    REPO_ROOT
    / "plugins"
    / "auto-cut-lite"
    / "installer"
    / "manage_runtime_dependencies.py"
)


def _load_helper():
    spec = importlib.util.spec_from_file_location("manage_runtime_dependencies", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _choose(helper, **overrides):
    arguments = {
        "previous_environment_exists": True,
        "python_matches": True,
        "pip_healthy": True,
        "previous_lock_sha256": "same",
        "requested_lock_sha256": "same",
        "installed_versions": {"demo": "1.2.0"},
        "requested_versions": {"demo": "1.2.0"},
    }
    arguments.update(overrides)
    return helper.choose_action(**arguments)


def test_dependency_planner_reuses_exact_healthy_environment() -> None:
    helper = _load_helper()

    assert _choose(helper) == ("reuse", "lock_python_and_health_match")


def test_dependency_planner_recreates_drift_from_unchanged_lock() -> None:
    helper = _load_helper()

    assert _choose(
        helper,
        installed_versions={"demo": "1.1.0"},
        requested_versions={"demo": "1.2.0"},
    ) == ("recreate", "installed_versions_drifted_from_unchanged_lock")


def test_dependency_planner_incrementally_upgrades_lower_compatible_versions() -> None:
    helper = _load_helper()

    assert _choose(
        helper,
        previous_lock_sha256="old",
        requested_lock_sha256="new",
        installed_versions={"demo": "1.1.0", "kept": "4.0"},
        requested_versions={"demo": "1.2.0", "kept": "4.0"},
    ) == ("incremental_upgrade", "compatible_lower_versions")


def test_dependency_planner_recreates_missing_damaged_or_incompatible_environments() -> None:
    helper = _load_helper()

    assert _choose(helper, previous_environment_exists=False)[0] == "recreate"
    assert _choose(helper, python_matches=False)[0] == "recreate"
    assert _choose(helper, pip_healthy=False)[0] == "recreate"
    assert _choose(helper, installed_versions={}) == ("recreate", "dependency_missing")
    assert _choose(
        helper,
        previous_lock_sha256="old",
        requested_lock_sha256="new",
        installed_versions={"demo": "2.0.0"},
    ) == ("recreate", "dependency_version_incompatible")


def test_main_and_audio_plans_are_independent() -> None:
    helper = _load_helper()

    main = _choose(helper)
    audio = _choose(
        helper,
        previous_lock_sha256="audio-old",
        requested_lock_sha256="audio-new",
        installed_versions={"demo": "1.0.0"},
    )

    assert main[0] == "reuse"
    assert audio[0] == "incremental_upgrade"


def test_dependency_transaction_rollback_restores_both_environment_trees(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    state = tmp_path / "LocalAppData" / "Auto-Cut" / "auto-cut-lite"
    target_root = state / "marketplace" / "plugins" / "auto-cut-lite"
    backup_root = state / "dependency-backups" / "operation"
    main_target = target_root / ".runtime-venv"
    audio_target = target_root / "runtime" / ".venv-audio"
    main_backup = backup_root / "main"
    audio_backup = backup_root / "audio"
    for path, value in (
        (main_target, b"new-main"),
        (audio_target, b"new-audio"),
        (main_backup, b"old-main"),
        (audio_backup, b"old-audio"),
    ):
        path.mkdir(parents=True)
        (path / "marker.bin").write_bytes(value)
    receipt = state / "dependency-transaction.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "active",
                "backup_root": str(backup_root),
                "records": [
                    {
                        "changed": True,
                        "target_environment": str(main_target),
                        "backup_environment": str(main_backup),
                    },
                    {
                        "changed": True,
                        "target_environment": str(audio_target),
                        "backup_environment": str(audio_backup),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = helper.rollback_transaction(receipt_path=receipt, state_root=state)

    assert result["status"] == "rolled_back"
    assert (main_target / "marker.bin").read_bytes() == b"old-main"
    assert (audio_target / "marker.bin").read_bytes() == b"old-audio"
    assert json.loads(receipt.read_text(encoding="utf-8"))["status"] == "rolled_back"


def test_audio_install_failure_rolls_back_new_main_and_audio_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    state = tmp_path / "LocalAppData" / "Auto-Cut" / "auto-cut-lite"
    plugin = state / "marketplace" / "plugins" / "auto-cut-lite"
    (plugin / "runtime").mkdir(parents=True)
    (plugin / "runtime" / "requirements.txt").write_text("demo==1.0\n", encoding="utf-8")
    (plugin / "runtime" / "requirements-audio.lock").write_text(
        "audio-demo==2.0\n", encoding="utf-8"
    )
    base_python = tmp_path / "python.exe"
    base_python.write_bytes(b"fixture")
    identity = {
        "bits": 64,
        "implementation": "CPython",
        "major": 3,
        "minor": 11,
        "version": "3.11.9",
    }
    monkeypatch.setattr(helper, "_python_identity", lambda _path: identity)

    def fake_run(arguments: list[str]):
        if arguments[1:3] == ["-m", "venv"]:
            environment = Path(arguments[3])
            (environment / "Scripts").mkdir(parents=True)
            (environment / "Scripts" / "python.exe").write_bytes(b"fixture")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[1:4] == ["-m", "pip", "install"]:
            failed = ".venv-audio" in arguments[0]
            return subprocess.CompletedProcess(arguments, 1 if failed else 0, "", "audio failed")
        if arguments[1:4] == ["-m", "pip", "check"]:
            return subprocess.CompletedProcess(arguments, 0, "No broken requirements found.\n", "")
        if arguments[1:5] == ["-m", "pip", "list", "--format=json"]:
            rows = (
                [{"name": "audio-demo", "version": "2.0"}]
                if ".venv-audio" in arguments[0]
                else [{"name": "demo", "version": "1.0"}]
            )
            return subprocess.CompletedProcess(arguments, 0, json.dumps(rows), "")
        raise AssertionError(arguments)

    monkeypatch.setattr(helper, "_run", fake_run)

    with pytest.raises(RuntimeError, match="dependency installation failed"):
        helper.install_dependencies(
            plugin_root=plugin,
            previous_plugin_root=None,
            base_python=base_python,
            state_root=state,
            include_audio=True,
        )

    assert not (plugin / ".runtime-venv").exists()
    assert not (plugin / "runtime" / ".venv-audio").exists()
    receipt = json.loads((state / "dependency-transaction.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "rolled_back"
