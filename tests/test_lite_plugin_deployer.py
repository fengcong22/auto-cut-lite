from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = (
    REPO_ROOT
    / "plugins"
    / "auto-cut-lite"
    / "installer"
    / "manage_named_marketplace.py"
)
DEPLOYER_PATH = REPO_ROOT / "plugins" / "auto-cut-lite" / "deploy-to-codex.ps1"


def _load_helper():
    spec = importlib.util.spec_from_file_location("manage_named_marketplace", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin(tmp_path: Path) -> Path:
    plugin = tmp_path / "plugins" / "auto-cut-lite"
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"name": "auto-cut-lite", "version": "1.2.1"}), encoding="utf-8"
    )
    return plugin


def test_register_creates_named_marketplace_with_required_contract(tmp_path: Path) -> None:
    helper = _load_helper()
    plugin = _plugin(tmp_path)
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"

    result = helper.register_named(plugin, tmp_path)

    payload = json.loads(marketplace.read_text(encoding="utf-8"))
    assert result["marketplace_name"] == "auto-cut-lite-marketplace"
    assert result["marketplace_display_name"] == "Auto-Cut Lite"
    assert result["marketplace_created"] is True
    assert result["marketplace_backup_path"] is None
    assert payload == {
        "name": "auto-cut-lite-marketplace",
        "interface": {"displayName": "Auto-Cut Lite"},
        "plugins": [
            {
                "name": "auto-cut-lite",
                "source": {"source": "local", "path": "./plugins/auto-cut-lite"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        ],
    }
    rollback = helper.rollback(
        marketplace,
        backup_path=None,
        created_new=True,
        expected_current_sha256=result["marketplace_sha256"],
    )
    assert rollback["action"] == "removed_created_marketplace"
    assert not marketplace.exists()


def test_register_preserves_named_marketplace_metadata_order_and_unrelated_entries(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    plugin = _plugin(tmp_path)
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    original_other = {
        "name": "other-plugin",
        "source": {"source": "local", "path": "./plugins/other-plugin"},
        "policy": {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_USE"},
        "category": "Developer Tools",
        "custom": {"preserve": True},
    }
    payload = {
        "name": "auto-cut-lite-marketplace",
        "interface": {"displayName": "Old label", "custom": "kept"},
        "custom_root": [1, 2, 3],
        "plugins": [
            original_other,
            {"name": "auto-cut-lite", "source": {"source": "local", "path": "wrong"}},
            {"name": "last-plugin", "source": {"source": "local", "path": "./plugins/last"}},
        ],
    }
    marketplace.write_text(json.dumps(payload), encoding="utf-8")

    result = helper.register_named(plugin, tmp_path)

    updated = json.loads(marketplace.read_text(encoding="utf-8"))
    assert result["marketplace_name"] == "auto-cut-lite-marketplace"
    assert result["entry_action"] == "replaced"
    assert updated["interface"] == {"displayName": "Auto-Cut Lite", "custom": "kept"}
    assert updated["custom_root"] == payload["custom_root"]
    assert updated["plugins"][0] == original_other
    assert updated["plugins"][2] == payload["plugins"][2]
    assert [entry["name"] for entry in updated["plugins"]].count("auto-cut-lite") == 1
    assert updated["plugins"][1]["source"]["path"] == "./plugins/auto-cut-lite"


def test_register_rejects_malformed_json_without_touching_it(tmp_path: Path) -> None:
    helper = _load_helper()
    plugin = _plugin(tmp_path)
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    malformed = b'{"name":"personal","plugins":['
    marketplace.write_bytes(malformed)

    with pytest.raises(ValueError, match="not valid UTF-8 JSON"):
        helper.register_named(plugin, tmp_path)

    assert marketplace.read_bytes() == malformed
    assert list(marketplace.parent.glob("*.bak")) == []
    assert list(marketplace.parent.glob("*.tmp")) == []


def test_register_backup_and_rollback_are_atomic(tmp_path: Path, monkeypatch) -> None:
    helper = _load_helper()
    plugin = _plugin(tmp_path)
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    original = {
        "name": "auto-cut-lite-marketplace",
        "interface": {"displayName": "Existing"},
        "plugins": [{"name": "kept"}],
    }
    original_bytes = (json.dumps(original, indent=2) + "\n").encode("utf-8")
    marketplace.write_bytes(original_bytes)

    registration = helper.register_named(plugin, tmp_path)
    backup = Path(registration["marketplace_backup_path"])
    assert backup.read_bytes() == original_bytes
    assert marketplace.read_bytes() != original_bytes

    result = helper.rollback(
        marketplace,
        backup_path=backup,
        created_new=False,
        expected_current_sha256=registration["marketplace_sha256"],
    )
    assert result["action"] == "restored_marketplace_backup"
    assert marketplace.read_bytes() == original_bytes

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(helper.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        helper.register_named(plugin, tmp_path)
    assert marketplace.read_bytes() == original_bytes
    assert not list(marketplace.parent.glob("*.tmp"))


def test_remove_personal_entry_preserves_other_plugins_and_can_rollback(tmp_path: Path) -> None:
    helper = _load_helper()
    marketplace = tmp_path / "marketplace.json"
    original = {
        "name": "personal",
        "interface": {"displayName": "Personal"},
        "plugins": [
            {"name": "kept", "source": {"source": "local", "path": "./plugins/kept"}},
            {"name": "auto-cut-lite", "source": {"source": "local", "path": "./plugins/auto-cut-lite"}},
        ],
    }
    original_bytes = (json.dumps(original, indent=2) + "\n").encode("utf-8")
    marketplace.write_bytes(original_bytes)

    result = helper.remove_personal_entry(marketplace)

    updated = json.loads(marketplace.read_text(encoding="utf-8"))
    assert result["changed"] is True
    assert result["entry_action"] == "removed"
    assert [entry["name"] for entry in updated["plugins"]] == ["kept"]
    rollback = helper.rollback(
        marketplace,
        backup_path=Path(result["marketplace_backup_path"]),
        created_new=False,
        expected_current_sha256=result["marketplace_sha256"],
    )
    assert rollback["action"] == "restored_marketplace_backup"
    assert marketplace.read_bytes() == original_bytes


def test_deployer_has_valid_windows_powershell_syntax() -> None:
    command = (
        "$errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{DEPLOYER_PATH}',"
        "[ref]$null,[ref]$errors)|Out-Null; "
        "if($errors.Count){$errors|ForEach-Object{$_.ToString()};exit 1}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_deployer_uses_named_marketplace_and_separate_audio_runtime_by_default() -> None:
    deployer = DEPLOYER_PATH.read_text(encoding="utf-8")

    assert "$marketplaceName = 'auto-cut-lite-marketplace'" in deployer
    assert "$marketplaceDisplayName = 'Auto-Cut Lite'" in deployer
    assert "'plugin', 'marketplace', 'add', $marketplaceRoot" in deployer
    assert "$pluginName + '@' + $marketplaceName" in deployer
    assert "'plugin', 'remove', $legacyReference" in deployer
    assert "'remove-personal'" in deployer
    assert "$audioRequested = -not $SkipAudio" in deployer
    assert "$audioVenv = Join-Path $targetRoot 'runtime\\.venv-audio'" in deployer
    assert "& $audioPython '-m' 'pip' 'install'" in deployer
    assert "& $runtimePython '-m' 'pip' 'install' '--disable-pip-version-check' '--upgrade' '-r' (Join-Path $targetRoot 'runtime\\requirements-audio.lock')" not in deployer


def test_deployer_validate_only_runs_package_preflight_on_windows_powershell_51(
    tmp_path: Path,
) -> None:
    package = tmp_path / "auto-cut-lite"
    files = {
        ".codex-plugin/plugin.json": json.dumps(
            {"name": "auto-cut-lite", "version": "1.2.1"}
        ).encode(),
        "installer/manage_named_marketplace.py": b"# validation fixture\n",
        "runtime/requirements.txt": b"# validation fixture\n",
        "runtime/requirements-audio.lock": b"# validation fixture\n",
    }
    for relative, data in files.items():
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    deployer = package / "deploy-to-codex.ps1"
    shutil.copy2(DEPLOYER_PATH, deployer)
    files["deploy-to-codex.ps1"] = deployer.read_bytes()
    manifest_rows = [
        {
            "path": relative,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for relative, data in sorted(files.items())
    ]
    (package / "PACKAGE-MANIFEST.json").write_text(
        json.dumps({"name": "auto-cut-lite", "version": "1.2.1", "files": manifest_rows}),
        encoding="utf-8",
    )

    command_bin = tmp_path / "command-bin"
    command_bin.mkdir()
    (command_bin / "codex.cmd").write_text("@exit /b 0\n", encoding="ascii")
    process_environment = os.environ.copy()
    process_environment["PATH"] = str(command_bin) + os.pathsep + process_environment["PATH"]

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(deployer),
            "-ValidateOnly",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=process_environment,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "package_validation=pass" in result.stdout
    assert "environment_validation=pass" in result.stdout
    assert "plugin_version=1.2.1" in result.stdout
    assert "python_version=3.11." in result.stdout
    assert "python_bits=64" in result.stdout
    assert "audio_runtime=required_separate" in result.stdout
    assert "marketplace_name=auto-cut-lite-marketplace" in result.stdout
    assert "marketplace_display_name=Auto-Cut Lite" in result.stdout
    assert "codex_invocation=direct" in result.stdout

    (command_bin / "codex.cmd").write_text("@exit /b 1\n", encoding="ascii")
    (command_bin / "npx.cmd").write_text("@exit /b 0\n", encoding="ascii")
    fallback = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(deployer),
            "-ValidateOnly",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=process_environment,
        check=False,
    )
    assert fallback.returncode == 0, fallback.stdout + fallback.stderr
    assert "codex_invocation=official_npm_fallback" in fallback.stdout


def test_plugin_and_builder_versions_match() -> None:
    plugin_manifest = json.loads(
        (REPO_ROOT / "plugins" / "auto-cut-lite" / ".codex-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    builder = (REPO_ROOT / "scripts" / "release" / "build_lite_plugin.py").read_text(
        encoding="utf-8"
    )
    assert plugin_manifest["version"] == "1.2.1"
    assert 'PLUGIN_VERSION = "1.2.1"' in builder
