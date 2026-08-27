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
    transaction_id = "a" * 32
    backup_root = state / "dependency-backups" / transaction_id
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
                "transaction_id": transaction_id,
                "state_root": str(state),
                "plugin_root": str(target_root),
                "backup_root": str(backup_root),
                "records": [
                    {
                        "name": "main",
                        "action": "recreate",
                        "changed": True,
                        "target_environment": str(main_target),
                        "backup_environment": str(main_backup),
                    },
                    {
                        "name": "audio",
                        "action": "recreate",
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


def test_dependency_rollback_preserves_target_when_persisted_backup_move_never_started(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    state = tmp_path / "LocalAppData" / "Auto-Cut" / "auto-cut-lite"
    target = state / "marketplace" / "plugins" / "auto-cut-lite" / ".runtime-venv"
    target.mkdir(parents=True)
    (target / "marker.bin").write_bytes(b"previous-good")
    missing_backup = state / "dependency-backups" / ("f" * 32) / "main"

    helper._rollback_records(
        [
            {
                "name": "main",
                "action": "recreate",
                "changed": True,
                "target_environment": str(target),
                "backup_environment": str(missing_backup),
                "backup_move_status": "pending",
            }
        ],
        state,
    )

    assert (target / "marker.bin").read_bytes() == b"previous-good"


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


def test_install_recovers_stale_active_transaction_then_proceeds(
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
    target_environment = plugin / ".runtime-venv"
    (target_environment / "Scripts").mkdir(parents=True)
    (target_environment / "marker.bin").write_bytes(b"interrupted-new")
    (target_environment / "Scripts" / "python.exe").write_bytes(b"fixture")
    stale_id = "b" * 32
    backup_root = state / "dependency-backups" / stale_id
    backup_environment = backup_root / "main"
    (backup_environment / "Scripts").mkdir(parents=True)
    (backup_environment / "marker.bin").write_bytes(b"previous-good")
    (backup_environment / "Scripts" / "python.exe").write_bytes(b"fixture")
    receipt_path = state / "dependency-transaction.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "active",
                "transaction_id": stale_id,
                "state_root": str(state),
                "plugin_root": str(plugin),
                "backup_root": str(backup_root),
                "records": [
                    {
                        "name": "main",
                        "action": "recreate",
                        "target_environment": str(target_environment),
                        "backup_environment": str(backup_environment),
                        "changed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
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
    monkeypatch.setattr(
        helper,
        "_plan_environment",
        lambda *_args, **_kwargs: {
            "action": "reuse",
            "reason": "lock_python_and_health_match",
            "requested_versions": {"demo": "1.0"},
            "requested_lock_sha256": "c" * 64,
        },
    )
    monkeypatch.setattr(
        helper,
        "_verify_environment",
        lambda *_args, **_kwargs: {
            "python_identity": identity,
            "pip_check": "pass",
            "dependency_check": "pass",
        },
    )

    result = helper.install_dependencies(
        plugin_root=plugin,
        previous_plugin_root=plugin,
        base_python=base_python,
        state_root=state,
        include_audio=False,
    )

    assert result["status"] == "prepared"
    assert result["recovery"]["status"] == "rolled_back"
    assert result["recovery"]["recovered"] is True
    assert result["recovery"]["transaction_id"] == stale_id
    assert not backup_root.exists()
    assert (target_environment / "marker.bin").read_bytes() == b"previous-good"
    active = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert active["status"] == "active"
    assert active["transaction_id"] != stale_id
    assert active["recovered_transaction"] == result["recovery"]


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_state",
        "foreign_plugin",
        "foreign_backup_root",
        "foreign_target",
        "foreign_environment_backup",
        "duplicate_environment",
        "unrecorded_backup",
    ],
)
def test_install_fails_closed_for_foreign_or_malformed_active_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    helper = _load_helper()
    state = tmp_path / "LocalAppData" / "Auto-Cut" / "auto-cut-lite"
    plugin = state / "marketplace" / "plugins" / "auto-cut-lite"
    (plugin / "runtime").mkdir(parents=True)
    (plugin / "runtime" / "requirements.txt").write_text("demo==1.0\n", encoding="utf-8")
    (plugin / "runtime" / "requirements-audio.lock").write_text(
        "audio-demo==2.0\n", encoding="utf-8"
    )
    target = plugin / ".runtime-venv"
    target.mkdir(parents=True)
    (target / "marker.bin").write_bytes(b"must-remain")
    transaction_id = "d" * 32
    backup_root = state / "dependency-backups" / transaction_id
    backup = backup_root / "main"
    backup.mkdir(parents=True)
    (backup / "marker.bin").write_bytes(b"backup-must-remain")
    payload = {
        "schema_version": 1,
        "status": "active",
        "transaction_id": transaction_id,
        "state_root": str(state),
        "plugin_root": str(plugin),
        "backup_root": str(backup_root),
        "records": [
            {
                "name": "main",
                "action": "recreate",
                "target_environment": str(target),
                "backup_environment": str(backup),
                "changed": True,
            }
        ],
    }
    foreign = tmp_path / "foreign"
    if mutation == "foreign_state":
        payload["state_root"] = str(foreign)
    elif mutation == "foreign_plugin":
        payload["plugin_root"] = str(foreign / "auto-cut-lite")
    elif mutation == "foreign_backup_root":
        payload["backup_root"] = str(foreign / transaction_id)
    elif mutation == "foreign_target":
        payload["records"][0]["target_environment"] = str(foreign / ".runtime-venv")
    elif mutation == "foreign_environment_backup":
        payload["records"][0]["backup_environment"] = str(foreign / "main")
    elif mutation == "duplicate_environment":
        payload["records"].append(dict(payload["records"][0]))
    else:
        payload["records"][0]["backup_environment"] = None
        payload["records"][0]["changed"] = False
    receipt = state / "dependency-transaction.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    original_receipt = receipt.read_bytes()
    base_python = tmp_path / "python.exe"
    base_python.write_bytes(b"fixture")
    monkeypatch.setattr(
        helper,
        "_python_identity",
        lambda _path: {
            "bits": 64,
            "implementation": "CPython",
            "major": 3,
            "minor": 11,
            "version": "3.11.9",
        },
    )

    with pytest.raises(ValueError, match="dependency transaction"):
        helper.install_dependencies(
            plugin_root=plugin,
            previous_plugin_root=None,
            base_python=base_python,
            state_root=state,
            include_audio=False,
        )

    assert receipt.read_bytes() == original_receipt
    assert (target / "marker.bin").read_bytes() == b"must-remain"
    assert (backup / "marker.bin").read_bytes() == b"backup-must-remain"


def test_install_fails_closed_for_invalid_json_transaction_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    state = tmp_path / "LocalAppData" / "Auto-Cut" / "auto-cut-lite"
    plugin = state / "marketplace" / "plugins" / "auto-cut-lite"
    (plugin / "runtime").mkdir(parents=True)
    target = plugin / ".runtime-venv"
    target.mkdir()
    (target / "marker.bin").write_bytes(b"must-remain")
    receipt = state / "dependency-transaction.json"
    receipt.write_text('{"status":"active","token":', encoding="utf-8")
    original_receipt = receipt.read_bytes()
    base_python = tmp_path / "python.exe"
    base_python.write_bytes(b"fixture")
    monkeypatch.setattr(
        helper,
        "_python_identity",
        lambda _path: {
            "bits": 64,
            "implementation": "CPython",
            "major": 3,
            "minor": 11,
            "version": "3.11.9",
        },
    )

    with pytest.raises(ValueError, match="invalid JSON"):
        helper.install_dependencies(
            plugin_root=plugin,
            previous_plugin_root=None,
            base_python=base_python,
            state_root=state,
            include_audio=False,
        )

    assert receipt.read_bytes() == original_receipt
    assert (target / "marker.bin").read_bytes() == b"must-remain"
