from __future__ import annotations

import importlib.util
import hashlib
import json
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
    / "register_personal_marketplace.py"
)
DEPLOYER_PATH = REPO_ROOT / "plugins" / "auto-cut-lite" / "deploy-to-codex.ps1"


def _load_helper():
    spec = importlib.util.spec_from_file_location("register_personal_marketplace", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin(tmp_path: Path) -> Path:
    plugin = tmp_path / "plugins" / "auto-cut-lite"
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"name": "auto-cut-lite", "version": "1.0.2"}), encoding="utf-8"
    )
    return plugin


def test_register_creates_personal_marketplace_with_required_contract(tmp_path: Path) -> None:
    helper = _load_helper()
    plugin = _plugin(tmp_path)
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"

    result = helper.register(plugin, marketplace)

    payload = json.loads(marketplace.read_text(encoding="utf-8"))
    assert result["marketplace_name"] == "personal"
    assert result["marketplace_created"] is True
    assert result["marketplace_backup_path"] is None
    assert payload == {
        "name": "personal",
        "interface": {"displayName": "Personal"},
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


def test_register_preserves_marketplace_metadata_order_and_unrelated_entries(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    plugin = _plugin(tmp_path)
    marketplace = tmp_path / "marketplace.json"
    original_other = {
        "name": "other-plugin",
        "source": {"source": "local", "path": "./plugins/other-plugin"},
        "policy": {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_USE"},
        "category": "Developer Tools",
        "custom": {"preserve": True},
    }
    payload = {
        "name": "my-personal-market",
        "interface": {"displayName": "My plugins", "custom": "kept"},
        "custom_root": [1, 2, 3],
        "plugins": [
            original_other,
            {"name": "auto-cut-lite", "source": {"source": "local", "path": "wrong"}},
            {"name": "last-plugin", "source": {"source": "local", "path": "./plugins/last"}},
        ],
    }
    marketplace.write_text(json.dumps(payload), encoding="utf-8")

    result = helper.register(plugin, marketplace)

    updated = json.loads(marketplace.read_text(encoding="utf-8"))
    assert result["marketplace_name"] == "my-personal-market"
    assert result["entry_action"] == "replaced"
    assert updated["interface"] == payload["interface"]
    assert updated["custom_root"] == payload["custom_root"]
    assert updated["plugins"][0] == original_other
    assert updated["plugins"][2] == payload["plugins"][2]
    assert [entry["name"] for entry in updated["plugins"]].count("auto-cut-lite") == 1
    assert updated["plugins"][1]["source"]["path"] == "./plugins/auto-cut-lite"


def test_register_rejects_malformed_json_without_touching_it(tmp_path: Path) -> None:
    helper = _load_helper()
    plugin = _plugin(tmp_path)
    marketplace = tmp_path / "marketplace.json"
    malformed = b'{"name":"personal","plugins":['
    marketplace.write_bytes(malformed)

    with pytest.raises(ValueError, match="not valid UTF-8 JSON"):
        helper.register(plugin, marketplace)

    assert marketplace.read_bytes() == malformed
    assert list(tmp_path.glob("*.bak")) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_register_backup_and_rollback_are_atomic(tmp_path: Path, monkeypatch) -> None:
    helper = _load_helper()
    plugin = _plugin(tmp_path)
    marketplace = tmp_path / "marketplace.json"
    original = {
        "name": "personal-existing",
        "interface": {"displayName": "Existing"},
        "plugins": [{"name": "kept"}],
    }
    original_bytes = (json.dumps(original, indent=2) + "\n").encode("utf-8")
    marketplace.write_bytes(original_bytes)

    registration = helper.register(plugin, marketplace)
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
        helper.register(plugin, marketplace)
    assert marketplace.read_bytes() == original_bytes
    assert not list(tmp_path.glob("*.tmp"))


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


def test_deployer_validate_only_runs_package_preflight_on_windows_powershell_51(
    tmp_path: Path,
) -> None:
    package = tmp_path / "auto-cut-lite"
    files = {
        ".codex-plugin/plugin.json": json.dumps(
            {"name": "auto-cut-lite", "version": "1.0.2"}
        ).encode(),
        "installer/register_personal_marketplace.py": b"# validation fixture\n",
        "runtime/requirements.txt": b"# validation fixture\n",
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
        json.dumps({"name": "auto-cut-lite", "version": "1.0.2", "files": manifest_rows}),
        encoding="utf-8",
    )

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
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "package_validation=pass" in result.stdout
    assert "plugin_version=1.0.2" in result.stdout


def test_plugin_and_builder_versions_match() -> None:
    plugin_manifest = json.loads(
        (REPO_ROOT / "plugins" / "auto-cut-lite" / ".codex-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    builder = (REPO_ROOT / "scripts" / "release" / "build_lite_plugin.py").read_text(
        encoding="utf-8"
    )
    assert plugin_manifest["version"] == "1.0.2"
    assert 'PLUGIN_VERSION = "1.0.2"' in builder
