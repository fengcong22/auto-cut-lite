from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.release import build_lite_plugin

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO_ROOT / "plugins" / "auto-cut-lite" / "installer" / "manage_named_marketplace.py"
DEPLOYER_PATH = REPO_ROOT / "plugins" / "auto-cut-lite" / "deploy-to-codex.ps1"
ONE_CLICK_PATH = REPO_ROOT / "plugins" / "auto-cut-lite" / "installer" / "one_click_deploy.ps1"
UNINSTALL_PATH = (
    REPO_ROOT / "plugins" / "auto-cut-lite" / "installer" / "uninstall_auto_cut_lite.ps1"
)
ONE_CLICK_LAUNCHER = (
    REPO_ROOT / "plugins" / "auto-cut-lite" / "一键安装或升级-Auto-Cut-Lite.cmd"
)
ONE_CLICK_UNINSTALLER = REPO_ROOT / "plugins" / "auto-cut-lite" / "一键卸载-Auto-Cut-Lite.cmd"


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
    manifest.write_text(json.dumps({"name": "auto-cut-lite", "version": "1.2.1"}), encoding="utf-8")
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
            {
                "name": "auto-cut-lite",
                "source": {"source": "local", "path": "./plugins/auto-cut-lite"},
            },
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


def test_remove_named_entry_preserves_unrelated_plugin_objects(tmp_path: Path) -> None:
    helper = _load_helper()
    marketplace = tmp_path / "marketplace.json"
    unrelated = {
        "name": "kept-plugin",
        "source": {"source": "local", "path": "./plugins/kept-plugin"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
        "category": "Developer Tools",
        "custom": {"bytes": "must-stay-identical"},
    }
    marketplace.write_text(
        json.dumps(
            {
                "name": "auto-cut-lite-marketplace",
                "interface": {"displayName": "Auto-Cut Lite"},
                "plugins": [
                    unrelated,
                    {
                        "name": "auto-cut-lite",
                        "source": {"source": "local", "path": "./plugins/auto-cut-lite"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = helper.remove_named_entry(marketplace)

    updated = json.loads(marketplace.read_text(encoding="utf-8"))
    assert updated["plugins"] == [unrelated]
    assert result["remaining_plugin_count"] == 1
    assert result["unrelated_plugins_unchanged"] is True


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


def test_one_click_launcher_has_valid_powershell_and_beginner_contract() -> None:
    command = (
        "$errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{ONE_CLICK_PATH}',"
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

    uninstall_command = (
        "$errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{UNINSTALL_PATH}',"
        "[ref]$null,[ref]$errors)|Out-Null; "
        "if($errors.Count){$errors|ForEach-Object{$_.ToString()};exit 1}"
    )
    uninstall_syntax = subprocess.run(
        ["powershell", "-NoProfile", "-Command", uninstall_command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert uninstall_syntax.returncode == 0, uninstall_syntax.stdout + uninstall_syntax.stderr

    launcher = ONE_CLICK_LAUNCHER.read_text(encoding="utf-8")
    wrapper = ONE_CLICK_PATH.read_text(encoding="utf-8")
    beginner = (
        REPO_ROOT / "plugins" / "auto-cut-lite" / "Auto-Cut-Lite新手部署说明.md"
    ).read_text(encoding="utf-8")
    assert "installer\\one_click_deploy.ps1" in launcher
    assert "PACKAGE-MANIFEST.json" in launcher
    assert "-UseChinaMirrors" not in launcher
    assert "if (-not $OfficialNetwork)" in wrapper
    assert "$deployArguments.UseChinaMirrors = $true" in wrapper
    assert "System.Windows.Forms.FolderBrowserDialog" in wrapper
    assert "Auto-cut-lite\\Auto-cut-lite" in beginner
    assert "Get-ExistingWorkspaceRoot" in wrapper
    assert "MessageBoxButtons]::YesNoCancel" in wrapper
    assert "workspace_scope -ne 'repo'" in wrapper
    assert "workspace_label -ne 'Auto-cut-lite'" in wrapper
    assert "Set-Clipboard -Value $installedWorkspace" in wrapper
    assert "一键安装或升级-Auto-Cut-Lite.cmd" in beginner
    assert "一键卸载-Auto-Cut-Lite.cmd" in beginner
    assert ONE_CLICK_UNINSTALLER.is_file()
    assert "installer\\uninstall_auto_cut_lite.ps1" in ONE_CLICK_UNINSTALLER.read_text(
        encoding="utf-8"
    )
    assert "不需要先打开 PowerShell" in beginner


def test_deployer_uses_named_marketplace_and_separate_audio_runtime_by_default() -> None:
    deployer = DEPLOYER_PATH.read_text(encoding="utf-8")

    assert "$marketplaceName = 'auto-cut-lite-marketplace'" in deployer
    assert "$marketplaceDisplayName = 'Auto-Cut Lite'" in deployer
    assert "'plugin', 'marketplace', 'add', $marketplaceRoot" in deployer
    assert "$pluginName + '@' + $marketplaceName" in deployer
    assert "'plugin', 'remove', $legacyReference" in deployer
    assert "'remove-personal'" in deployer
    assert "[string]$WorkspaceRoot" in deployer
    assert "[switch]$UseChinaMirrors" in deployer
    assert "$defaultWorkspaceRoot = $packageRoot" in deployer
    assert "$workspaceRootSource = 'package_root'" in deployer
    assert "$workspaceRootSource = 'existing_receipt'" in deployer
    assert "parameter_then_existing_receipt_then_package_root" in deployer
    assert "$env:PIP_INDEX_URL = 'https://mirrors.aliyun.com/pypi/simple/'" in deployer
    assert "$env:npm_config_registry = 'https://registry.npmmirror.com'" in deployer
    assert "$env:npm_config_update_notifier = 'false'" in deployer
    assert "function Invoke-NativeCommand" in deployer
    assert "$ErrorActionPreference = 'Continue'" in deployer
    assert "$global:LASTEXITCODE = $nativeExitCode" in deployer
    assert "WorkspaceRoot must be an absolute path" in deployer
    assert "WorkspaceRoot folder name must be exactly" in deployer
    assert "'installer/manage_workspace.py'" in deployer
    assert "workspace_scope=repo" in deployer
    assert "Plugin manifest must not expose user-scoped skills" in deployer
    assert "plugin_manifest_path = $pluginManifestInstalledPath" in deployer
    assert "runtime_root = $runtimeRoot" in deployer
    assert deployer.index("$workspaceRollbackNeeded = $true") < deployer.index(
        "$workspaceInstall = $workspaceOutput | ConvertFrom-Json"
    )
    assert "$audioRequested = -not $SkipAudio" in deployer
    assert "manage_runtime_dependencies.py" in deployer
    assert "--previous-plugin-root" in deployer
    assert "$report.components.python.dependencies = 'installed'" in deployer
    assert "$report.components.python.dependency_action =" in deployer
    assert "requirements_sha256" in deployer
    assert "dependency rollback returned a failure code" in deployer
    assert "'-m' 'pip' 'install'" not in deployer
    assert "$env:LOCALAPPDATA" in deployer
    assert "$env:USERPROFILE" in deployer


def test_deployer_commits_dependencies_before_reporting_installed_and_fails_closed() -> None:
    deployer = DEPLOYER_PATH.read_text(encoding="utf-8")

    commit_index = deployer.index("$dependencyCommitOutput =")
    committed_index = deployer.index("$dependencyTransactionCommitted = $true", commit_index)
    installed_index = deployer.index("$report.deployment_status = 'installed'", commit_index)
    report_index = deployer.index("Write-DeploymentReport -Payload $report", installed_index)
    cleanup_index = deployer.index("$backupCleanup = Remove-OwnedPluginBackups", report_index)
    catch_index = deployer.index("\ncatch {", cleanup_index)
    failed_index = deployer.index("$report.deployment_status = 'failed'", catch_index)
    not_evaluated_index = deployer.index("$report.readiness = 'not_evaluated'", catch_index)
    attempt_report_index = deployer.index(
        "Write-DeploymentReport -Payload $report -DestinationPath $attemptReportPath",
        failed_index,
    )

    assert commit_index < committed_index < installed_index < report_index < cleanup_index
    assert cleanup_index < catch_index < failed_index < attempt_report_index
    assert catch_index < not_evaluated_index < attempt_report_index


def test_deployer_preserves_the_previous_committed_report_after_complete_rollback() -> None:
    deployer = DEPLOYER_PATH.read_text(encoding="utf-8")

    assert "$attemptReportPath = Join-Path $stateRoot 'deployment-attempt-report.json'" in deployer
    assert "function Get-PreservableDeploymentReport" in deployer
    assert "[int]$payload.schema_version -ne 2" in deployer
    assert "[string]$payload.plugin_name -ne $pluginName" in deployer
    assert "[string]$payload.deployment_status -ne 'installed'" in deployer
    assert "Test-EquivalentDeploymentPath -SavedPath ([string]$payload.target_root)" in deployer
    assert "[string]$installedManifest.version -ne [string]$payload.plugin_version" in deployer
    assert "function Test-PreservableDeploymentAnchors" in deployer
    assert "workspaceReceipt.installed_package_sha256.'PACKAGE-MANIFEST.json'" in deployer
    assert "runtime/scripts/utils/runtime_integrity.py" in deployer
    assert "'.runtime-venv\\Scripts\\python.exe'" in deployer
    capture_index = deployer.index("$previousInstalledReport = Get-PreservableDeploymentReport")
    mutation_index = deployer.index("Copy-InventoriedPackage", capture_index)
    failed_index = deployer.index("$report.deployment_status = 'failed'", mutation_index)
    identity_index = deployer.index("Test-PreservedPluginIdentity", failed_index - 500)
    preserve_index = deployer.index(
        "$preservePreviousReport = $null -ne $previousInstalledReport -and "
        "$rollbackErrors.Count -eq 0",
        failed_index,
    )
    restore_index = deployer.index(
        "Restore-PreservedDeploymentReport -Snapshot $previousInstalledReport",
        preserve_index,
    )
    attempt_index = deployer.index(
        "Write-DeploymentReport -Payload $report -DestinationPath $attemptReportPath",
        restore_index,
    )
    failed_main_index = deployer.index(
        "Write-DeploymentReport -Payload $report -DestinationPath $reportPath",
        restore_index,
    )

    assert capture_index < mutation_index < identity_index < failed_index
    assert failed_index < preserve_index < restore_index < attempt_index < failed_main_index
    failure_branch = deployer[preserve_index:failed_main_index + 100]
    assert "if ($preservePreviousReport)" in failure_branch
    assert "if (-not $preservePreviousReport)" in failure_branch
    assert "deployment report restore failed" in failure_branch


def test_deployer_requires_a_verified_baseline_for_in_place_redeployment() -> None:
    deployer = DEPLOYER_PATH.read_text(encoding="utf-8")

    capture_index = deployer.index("$previousInstalledReport = Get-PreservableDeploymentReport")
    guard_index = deployer.index(
        "if ($sourceIsTarget -and $null -eq $previousInstalledReport)", capture_index
    )
    package_validation_index = deployer.index(
        "$packageManifest = Read-AndValidatePackageManifest", guard_index
    )
    dependency_index = deployer.index("manage_runtime_dependencies.py", package_validation_index)

    assert capture_index < guard_index < package_validation_index < dependency_index
    assert "requires a verified previous installed report" in deployer[
        guard_index:package_validation_index
    ]


def test_deployer_removes_all_verified_owned_backups_after_success_report() -> None:
    deployer = DEPLOYER_PATH.read_text(encoding="utf-8")

    helper_start = deployer.index("function Remove-OwnedPluginBackup")
    helper_end = deployer.index("\nfunction Remove-OwnedPluginBackups", helper_start)
    helper = deployer[helper_start:helper_end]
    assert "[System.IO.Path]::GetDirectoryName($resolved)" in helper
    assert "[string]::Equals($actualParent, $expectedParent" in helper
    assert "^\\.auto-cut-lite\\.backup\\.\\d{14}\\.[0-9a-f]{32}$" in helper
    assert "Assert-NoReparseInExistingPath -Path $resolved -StopAt $targetParent" in helper
    assert "[System.Collections.Generic.Stack[string]]::new()" in helper
    assert "Get-ChildItem -LiteralPath $directory -Force" in helper
    assert "$directories.Push($item.FullName)" in helper
    assert "Get-ChildItem -LiteralPath $resolved -Recurse" not in helper
    assert "Join-Path $resolved '.codex-plugin\\plugin.json'" in helper
    assert "[string]$manifest.name -ne $pluginName" in helper
    assert "Join-Path $resolved 'PACKAGE-MANIFEST.json'" in helper
    assert "Plugin backup package file failed inventory validation" in helper
    assert "installer/manage_runtime_dependencies.py" in helper
    assert "Remove-Item -LiteralPath $resolved -Recurse -Force" in helper

    aggregate_start = helper_end
    aggregate_end = deployer.index("\ntry {", aggregate_start)
    aggregate = deployer[aggregate_start:aggregate_end]
    assert "Get-ChildItem -LiteralPath $targetParent -Force" in aggregate
    assert "StartsWith(" in aggregate
    assert "'.auto-cut-lite.backup.'" in aggregate
    assert "Remove-OwnedPluginBackup -Path $candidate.FullName" in aggregate
    assert "$removed.Add([string]$candidate.FullName)" in aggregate
    assert "$deferred.Add(" in aggregate

    commit_index = deployer.index("$dependencyTransactionCommitted = $true")
    installed_report_index = deployer.index(
        "Write-DeploymentReport -Payload $report", commit_index
    )
    cleanup_index = deployer.index("$backupCleanup = Remove-OwnedPluginBackups", installed_report_index)
    deferred_index = deployer.index("$report.plugin_backup_cleanup = 'deferred'", cleanup_index)
    clear_index = deployer.index("$report.plugin_backup_path = $null", cleanup_index)
    outer_catch_index = deployer.index("\ncatch {", deferred_index)

    assert commit_index < installed_report_index < cleanup_index < outer_catch_index
    assert cleanup_index < deferred_index < clear_index
    assert "$report.plugin_backup_cleanup_removed_count = $removedBackupCount" in deployer
    assert "$report.plugin_backup_cleanup_deferred = $deferredBackups" in deployer
    assert "$report.plugin_backup_cleanup_error = $_.Exception.Message" in deployer
    assert "$report.deployment_status = 'failed'" not in deployer[
        cleanup_index:outer_catch_index
    ]


def test_owned_backup_cleanup_removes_valid_history_and_defers_invalid(
    tmp_path: Path,
) -> None:
    target_parent = tmp_path / "marketplace" / "plugins"
    target_parent.mkdir(parents=True)

    def write_backup(name: str, *, corrupt: bool = False) -> Path:
        backup = target_parent / name
        files = {
            ".codex-plugin/plugin.json": json.dumps(
                {"name": "auto-cut-lite", "version": "1.5.9"}
            ).encode(),
            "deploy-to-codex.ps1": b"# deployer anchor\n",
            "installer/manage_runtime_dependencies.py": b"# dependency anchor\n",
        }
        rows = []
        for relative, data in files.items():
            target = backup / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            rows.append(
                {
                    "path": relative,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        if corrupt:
            rows[-1]["sha256"] = "0" * 64
        (backup / "PACKAGE-MANIFEST.json").write_text(
            json.dumps(
                {"name": "auto-cut-lite", "version": "1.5.9", "files": rows}
            ),
            encoding="utf-8",
        )
        return backup

    first = write_backup(".auto-cut-lite.backup.20260827010101." + "a" * 32)
    second = write_backup(".auto-cut-lite.backup.20260827020202." + "b" * 32)
    invalid = write_backup(
        ".auto-cut-lite.backup.20260827030303." + "c" * 32, corrupt=True
    )

    deployer = DEPLOYER_PATH.read_text(encoding="utf-8")
    assert_start = deployer.index("function Assert-NoReparseInExistingPath")
    assert_end = deployer.index("\nfunction Resolve-ManifestRelativePath", assert_start)
    cleanup_start = deployer.index("function Remove-OwnedPluginBackup")
    cleanup_end = deployer.index("\ntry {", cleanup_start)
    quoted_parent = str(target_parent).replace("'", "''")
    harness = "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            "Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop",
            "$pluginName = 'auto-cut-lite'",
            f"$targetParent = '{quoted_parent}'",
            deployer[assert_start:assert_end],
            deployer[cleanup_start:cleanup_end],
            "$result = Remove-OwnedPluginBackups",
            "$result | ConvertTo-Json -Depth 8 -Compress",
        )
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", harness],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    cleanup = json.loads(result.stdout)
    assert len(cleanup["removed"]) == 2, cleanup
    assert len(cleanup["deferred"]) == 1
    assert not first.exists()
    assert not second.exists()
    assert invalid.exists()
    assert "inventory validation" in cleanup["deferred"][0]["error"]


def test_failed_upgrade_preserves_committed_report_bytes_but_first_install_fails_main(
    tmp_path: Path,
) -> None:
    package = tmp_path / "Auto-cut-lite"
    package.mkdir()
    deployer = package / "deploy-to-codex.ps1"
    shutil.copy2(DEPLOYER_PATH, deployer)

    upgrade_local_app_data = tmp_path / "UpgradeLocalAppData"
    upgrade_user_profile = tmp_path / "UpgradeUserProfile"
    state_root = upgrade_local_app_data / "Auto-Cut" / "auto-cut-lite"
    target_root = state_root / "marketplace" / "plugins" / "auto-cut-lite"
    installed_manifest_path = target_root / ".codex-plugin" / "plugin.json"
    installed_manifest_path.parent.mkdir(parents=True)
    installed_manifest_path.write_text(
        json.dumps({"name": "auto-cut-lite", "version": "1.5.9"}),
        encoding="utf-8",
    )
    runtime_root = target_root / "runtime"
    runtime_integrity_path = runtime_root / "scripts" / "utils" / "runtime_integrity.py"
    runtime_entry_path = runtime_root / "scripts" / "jy_wrapper.py"
    runtime_integrity_path.parent.mkdir(parents=True)
    runtime_entry_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_integrity_path.write_bytes(b"# integrity anchor\n")
    runtime_entry_path.write_bytes(b"# runtime entry anchor\n")
    runtime_python = target_root / ".runtime-venv" / "Scripts" / "python.exe"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"runtime python anchor")
    package_rows = []
    for relative, path in (
        (".codex-plugin/plugin.json", installed_manifest_path),
        ("runtime/scripts/utils/runtime_integrity.py", runtime_integrity_path),
        ("runtime/scripts/jy_wrapper.py", runtime_entry_path),
    ):
        data = path.read_bytes()
        package_rows.append(
            {"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    installed_package_manifest = target_root / "PACKAGE-MANIFEST.json"
    installed_package_manifest.write_text(
        json.dumps(
            {"name": "auto-cut-lite", "version": "1.5.9", "files": package_rows}
        ),
        encoding="utf-8",
    )
    workspace_receipt_path = state_root / "workspace-install-receipt.json"
    report_path = state_root / "deployment-report.json"
    previous_workspace_root = package
    workspace_receipt_path.write_text(
        json.dumps(
            {
                "status": "installed",
                "plugin_name": "auto-cut-lite",
                "plugin_version": "1.5.9",
                "workspace_root": str(previous_workspace_root),
                "deployment_report_path": str(report_path),
                "plugin_root": str(target_root),
                "runtime_root": str(runtime_root),
                "installed_package_sha256": {
                    "PACKAGE-MANIFEST.json": hashlib.sha256(
                        installed_package_manifest.read_bytes()
                    ).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )
    committed_report = {
        "schema_version": 2,
        "plugin_name": "auto-cut-lite",
        "plugin_version": "1.5.9",
        "deployment_status": "installed",
        "target_root": str(target_root),
        "plugin_manifest_path": str(installed_manifest_path),
        "runtime_root": str(runtime_root),
        "workspace_receipt_path": str(workspace_receipt_path),
        "components": {
            "python": {
                "status": "detected",
                "dependencies": "installed",
                "runtime_path": str(runtime_python),
            }
        },
        "custom_committed_field": ["preserve", "these", "bytes"],
    }
    committed_bytes = (
        json.dumps(committed_report, ensure_ascii=False, indent=3) + "\r\n"
    ).encode("utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(committed_bytes)

    upgrade = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(deployer),
            "-LocalAppDataRoot",
            str(upgrade_local_app_data),
            "-UserProfileRoot",
            str(upgrade_user_profile),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert upgrade.returncode != 0
    assert "PACKAGE-MANIFEST.json is missing" in upgrade.stdout + upgrade.stderr
    assert report_path.read_bytes() == committed_bytes
    upgrade_attempt = json.loads(
        (state_root / "deployment-attempt-report.json").read_text(encoding="utf-8")
    )
    assert upgrade_attempt["deployment_status"] == "failed"
    assert upgrade_attempt["previous_deployment_report_preserved"] is True

    first_local_app_data = tmp_path / "FirstInstallLocalAppData"
    first_user_profile = tmp_path / "FirstInstallUserProfile"
    first_install = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(deployer),
            "-LocalAppDataRoot",
            str(first_local_app_data),
            "-UserProfileRoot",
            str(first_user_profile),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert first_install.returncode != 0
    first_state_root = first_local_app_data / "Auto-Cut" / "auto-cut-lite"
    first_main = json.loads(
        (first_state_root / "deployment-report.json").read_text(encoding="utf-8")
    )
    first_attempt = json.loads(
        (first_state_root / "deployment-attempt-report.json").read_text(encoding="utf-8")
    )
    assert first_main["deployment_status"] == "failed"
    assert first_attempt["deployment_status"] == "failed"
    assert first_main["previous_deployment_report_preserved"] is False
    assert first_attempt["previous_deployment_report_preserved"] is False


def test_incomplete_installed_looking_report_does_not_mask_failure(
    tmp_path: Path,
) -> None:
    package = tmp_path / "Auto-cut-lite"
    package.mkdir()
    deployer = package / "deploy-to-codex.ps1"
    shutil.copy2(DEPLOYER_PATH, deployer)

    local_app_data = tmp_path / "LocalAppData"
    user_profile = tmp_path / "UserProfile"
    state_root = local_app_data / "Auto-Cut" / "auto-cut-lite"
    target_root = state_root / "marketplace" / "plugins" / "auto-cut-lite"
    installed_manifest = target_root / ".codex-plugin" / "plugin.json"
    installed_manifest.parent.mkdir(parents=True)
    installed_manifest.write_text(
        json.dumps({"name": "auto-cut-lite", "version": "1.5.9"}), encoding="utf-8"
    )
    report_path = state_root / "deployment-report.json"
    incomplete_bytes = json.dumps(
        {
            "schema_version": 2,
            "plugin_name": "auto-cut-lite",
            "plugin_version": "1.5.9",
            "deployment_status": "installed",
            "target_root": str(target_root),
            "plugin_manifest_path": str(installed_manifest),
            "runtime_root": str(target_root / "runtime"),
        }
    ).encode("utf-8")
    report_path.write_bytes(incomplete_bytes)

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(deployer),
            "-LocalAppDataRoot",
            str(local_app_data),
            "-UserProfileRoot",
            str(user_profile),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode != 0
    assert report_path.read_bytes() != incomplete_bytes
    failed_report = json.loads(report_path.read_text(encoding="utf-8"))
    attempt_report = json.loads(
        (state_root / "deployment-attempt-report.json").read_text(encoding="utf-8")
    )
    assert failed_report["deployment_status"] == "failed"
    assert failed_report["previous_deployment_report_preserved"] is False
    assert attempt_report["previous_deployment_report_preserved"] is False


def test_uninstaller_is_receipt_scoped_and_preserves_unrelated_registrations() -> None:
    uninstaller = UNINSTALL_PATH.read_text(encoding="utf-8")

    assert "[Console]::OutputEncoding = $utf8NoBom" in uninstaller
    assert "$OutputEncoding = $utf8NoBom" in uninstaller
    assert "$env:PYTHONUTF8 = '1'" in uninstaller
    assert "$env:PYTHONIOENCODING = 'utf-8'" in uninstaller
    assert "'uninstall'" in uninstaller
    assert "'remove-named'" in uninstaller
    assert "unrelated_plugins_unchanged" in uninstaller
    assert "unrelated_unchanged" in uninstaller
    assert "$expectedRuntimePython = Join-Path $targetRoot '.runtime-venv\\Scripts\\python.exe'" in uninstaller
    assert "[string]$deployment.components.python.runtime_path" in uninstaller
    assert "if ($null -eq $pythonCommand)" in uninstaller
    assert "Get-Command 'python'" in uninstaller
    managed_python_index = uninstaller.index("$reportedRuntimePython =")
    fallback_python_index = uninstaller.index("Get-Command 'python'", managed_python_index)
    assert managed_python_index < fallback_python_index
    assert "Remove-OwnedPluginTree -Path $targetRoot" in uninstaller
    assert "'workspace-staging'" in uninstaller
    assert "'deployment-attempt-report.json'" in uninstaller
    assert "Remove-Item -LiteralPath $stateRoot -Recurse" not in uninstaller
    assert "Deployment report target root does not match" in uninstaller


def test_uninstaller_uses_managed_python_utf8_and_removes_attempt_report(
    tmp_path: Path,
) -> None:
    local_app_data = tmp_path / "LocalAppData"
    user_profile = tmp_path / "UserProfile"
    state_root = local_app_data / "Auto-Cut" / "auto-cut-lite"
    marketplace_root = state_root / "marketplace"
    target_root = marketplace_root / "plugins" / "auto-cut-lite"
    runtime_python = target_root / ".runtime-venv" / "Scripts" / "python.exe"
    runtime_python.parent.mkdir(parents=True)
    shutil.copy2(getattr(sys, "_base_executable", sys.executable), runtime_python)

    plugin_manifest = target_root / ".codex-plugin" / "plugin.json"
    plugin_manifest.parent.mkdir(parents=True)
    plugin_manifest.write_text(
        json.dumps({"name": "auto-cut-lite", "version": "1.6.0"}),
        encoding="utf-8",
    )
    helpers = {
        "installer/manage_named_marketplace.py": (
            "import json\n"
            "print(json.dumps({"
            "'changed': False, 'marketplace_backup_path': None, "
            "'unrelated_plugins_unchanged': True, 'unrelated_plugins_sha256': 'marketplace', "
            "'remaining_plugin_count': 0}, ensure_ascii=False))\n"
        ),
        "installer/manage_workspace.py": (
            "import json, sys\n"
            "print(json.dumps({"
            "'status': 'uninstalled', 'unrelated_unchanged': True, "
            "'workspace_root': '中文工作区::' + sys.executable, "
            "'unrelated_file_count': 0, 'unrelated_tree_sha256': 'workspace'"
            "}, ensure_ascii=False))\n"
        ),
    }
    manifest_rows = []
    for relative_path, source in helpers.items():
        helper = target_root / Path(relative_path)
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text(source, encoding="utf-8")
        data = helper.read_bytes()
        manifest_rows.append(
            {
                "path": relative_path,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    (target_root / "PACKAGE-MANIFEST.json").write_text(
        json.dumps({"name": "auto-cut-lite", "files": manifest_rows}),
        encoding="utf-8",
    )

    marketplace_path = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True)
    marketplace_path.write_text(
        json.dumps({"name": "auto-cut-lite-marketplace", "plugins": []}),
        encoding="utf-8",
    )
    (state_root / "workspace-install-receipt.json").write_text("{}", encoding="utf-8")
    (state_root / "dependency-transaction.json").write_text("{}", encoding="utf-8")
    workspace_staging = state_root / "workspace-staging"
    workspace_staging.mkdir()
    (workspace_staging / "stale-operation.json").write_text("{}", encoding="utf-8")
    attempt_report = state_root / "deployment-attempt-report.json"
    attempt_report.write_text('{"deployment_status":"failed"}', encoding="utf-8")
    (state_root / "deployment-report.json").write_text(
        json.dumps(
            {
                "plugin_name": "auto-cut-lite",
                "plugin_version": "1.6.0",
                "deployment_status": "installed",
                "target_root": str(target_root),
                "workspace_root": str(tmp_path / "workspace"),
                "components": {"python": {"runtime_path": str(runtime_python)}},
            }
        ),
        encoding="utf-8",
    )

    command_bin = tmp_path / "command-bin"
    command_bin.mkdir()
    (command_bin / "codex.cmd").write_text(
        "@echo off\nif \"%~1\"==\"--version\" echo codex-cli 0.149.1\nexit /b 0\n",
        encoding="ascii",
    )
    (command_bin / "python.cmd").write_text(
        "@echo PATH Python must not be used 1>&2\n@exit /b 97\n",
        encoding="ascii",
    )
    process_environment = os.environ.copy()
    process_environment["PATH"] = str(command_bin) + os.pathsep + process_environment["PATH"]

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(UNINSTALL_PATH),
            "-LocalAppDataRoot",
            str(local_app_data),
            "-UserProfileRoot",
            str(user_profile),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=process_environment,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PATH Python must not be used" not in result.stdout + result.stderr
    assert not attempt_report.exists()
    assert not workspace_staging.exists()
    assert not state_root.exists()
    uninstall_report = json.loads(
        (local_app_data / "Auto-Cut" / "auto-cut-lite-uninstall-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert uninstall_report["status"] == "uninstalled"
    assert uninstall_report["managed_runtime_removed"] is True
    assert uninstall_report["workspace_root"] == f"中文工作区::{runtime_python}"


def test_deployer_validate_only_runs_package_preflight_on_windows_powershell_51(
    tmp_path: Path,
) -> None:
    package = tmp_path / "Auto-cut-lite"
    files = {
        ".codex-plugin/plugin.json": json.dumps(
            {"name": "auto-cut-lite", "version": "1.3.0"}
        ).encode(),
        "AGENTS.md": b"# Portable workspace rules\n",
        "PORTABLE-CAPABILITIES.json": json.dumps(
            {
                "workspace_installation": {
                    "beginner_guide": "Auto-Cut-Lite新手部署说明.md",
                    "post_install_guide": "Auto-Cut-Lite部署成功后操作说明.md",
                    "one_click_launcher": "一键安装或升级-Auto-Cut-Lite.cmd",
                    "one_click_uninstaller": "一键卸载-Auto-Cut-Lite.cmd",
                }
            }
        ).encode(),
        "Auto-Cut-Lite新手部署说明.md": b"# Beginner\n",
        "Auto-Cut-Lite部署成功后操作说明.md": b"# Next steps\n",
        "一键安装或升级-Auto-Cut-Lite.cmd": b"@exit /b 0\n",
        "一键卸载-Auto-Cut-Lite.cmd": b"@exit /b 0\n",
        "installer/manage_named_marketplace.py": b"# validation fixture\n",
        "installer/one_click_deploy.ps1": b"# validation fixture\n",
        "installer/manage_runtime_dependencies.py": b"# validation fixture\n",
        "installer/manage_workspace.py": b"# validation fixture\n",
        "installer/uninstall_auto_cut_lite.ps1": b"# validation fixture\n",
        "runtime/requirements.txt": b"# validation fixture\n",
        "runtime/requirements-audio.lock": b"# validation fixture\n",
    }
    for skill_name in sorted(build_lite_plugin.EXPECTED_SKILLS):
        files[f"workspace-payload/skills/{skill_name}/SKILL.md"] = (
            f"---\nname: {skill_name}\ndescription: fixture\n---\n"
        ).encode()
        files[f"workspace-payload/skills/{skill_name}/agents/openai.yaml"] = b"interface: {}\n"
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
        json.dumps({"name": "auto-cut-lite", "version": "1.3.0", "files": manifest_rows}),
        encoding="utf-8",
    )

    command_bin = tmp_path / "command-bin"
    command_bin.mkdir()
    (command_bin / "codex.cmd").write_text("@exit /b 0\n", encoding="ascii")
    process_environment = os.environ.copy()
    process_environment["PATH"] = str(command_bin) + os.pathsep + process_environment["PATH"]
    process_environment["LOCALAPPDATA"] = str(tmp_path / "IsolatedLocalAppData")
    process_environment["USERPROFILE"] = str(tmp_path / "IsolatedUserProfile")

    custom_workspace = tmp_path / "OtherWorkspaceParent" / "Auto-cut-lite"
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(deployer),
            "-ValidateOnly",
            "-WorkspaceRoot",
            str(custom_workspace),
            "-UseChinaMirrors",
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
    assert "plugin_version=1.3.0" in result.stdout
    assert "python_version=3.11." in result.stdout
    assert "python_bits=64" in result.stdout
    assert "audio_runtime=required_separate" in result.stdout
    assert "marketplace_name=auto-cut-lite-marketplace" in result.stdout
    assert "marketplace_display_name=Auto-Cut Lite" in result.stdout
    assert f"workspace_root={custom_workspace}" in result.stdout
    assert "workspace_root_source=parameter" in result.stdout
    assert "workspace_root_customizable=true" in result.stdout
    assert "workspace_mode=combined_package_workspace" in result.stdout
    assert f"workspace_package_root={custom_workspace}" in result.stdout
    assert "workspace_upgrade_precedence=parameter_then_existing_receipt_then_package_root" in result.stdout
    assert "workspace_label=Auto-cut-lite" in result.stdout
    assert "workspace_scope=repo" in result.stdout
    assert "workspace_skill_count=17" in result.stdout
    assert "workspace_skill_payload=workspace-payload/skills" in result.stdout
    assert "plugin_top_level_skills_present=false" in result.stdout
    assert "china_mirrors_enabled=True" in result.stdout
    assert "codex_invocation=direct" in result.stdout
    assert f"local_app_data_root={tmp_path / 'IsolatedLocalAppData'}" in result.stdout
    assert f"user_profile_root={tmp_path / 'IsolatedUserProfile'}" in result.stdout

    invalid_workspace = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(deployer),
            "-ValidateOnly",
            "-WorkspaceRoot",
            str(tmp_path / "wrong-name"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=process_environment,
        check=False,
    )
    assert invalid_workspace.returncode != 0
    assert "folder name must be exactly" in (invalid_workspace.stdout + invalid_workspace.stderr)

    (command_bin / "codex.cmd").write_text("@exit /b 1\n", encoding="ascii")
    (command_bin / "npx.cmd").write_text(
        "@echo npm notice 1>&2\n@exit /b 0\n", encoding="ascii"
    )
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
    if "workspace_root_source=package_root" in fallback.stdout:
        assert f"workspace_root={package}" in fallback.stdout
    else:
        assert "workspace_root_source=existing_receipt" in fallback.stdout


def test_plugin_and_builder_versions_match() -> None:
    plugin_manifest = json.loads(
        (REPO_ROOT / "plugins" / "auto-cut-lite" / ".codex-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    builder = (REPO_ROOT / "scripts" / "release" / "build_lite_plugin.py").read_text(
        encoding="utf-8"
    )
    capabilities = json.loads(
        (REPO_ROOT / "plugins" / "auto-cut-lite" / "PORTABLE-CAPABILITIES.json").read_text(
            encoding="utf-8"
        )
    )
    assert f'PLUGIN_VERSION = "{plugin_manifest["version"]}"' in builder
    assert capabilities["plugin_version"] == plugin_manifest["version"]


def test_builder_uses_one_combined_workspace_archive_root(tmp_path: Path) -> None:
    output = tmp_path / build_lite_plugin.ARCHIVE_NAME

    receipt = build_lite_plugin.build(REPO_ROOT, output, require_clean=False)

    assert receipt["archive_root"] == "Auto-cut-lite"
    assert receipt["workspace_mode"] == "combined_package_workspace"
    assert receipt["workspace_root_default"] == "extracted_package_root"
    assert receipt["workspace_root_precedence"] == (
        "parameter_then_existing_receipt_then_package_root"
    )
    assert receipt["workspace_package_sync"] == "manifest_verified_transactional"
    assert receipt["workspace_package_rollback"] is True
    assert receipt["one_click_launcher"] == "一键安装或升级-Auto-Cut-Lite.cmd"
    assert receipt["one_click_uninstaller"] == "一键卸载-Auto-Cut-Lite.cmd"
    assert receipt["one_click_default_network"] == "china_mirrors"
    assert receipt["one_click_internal_manifest_validation"] is True
    assert receipt["post_install_codex_guide"] == "Auto-Cut-Lite部署成功后操作说明.md"
    assert len(receipt["source_git_commit"]) == 40
    assert isinstance(receipt["source_git_clean"], bool)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
    assert names
    assert {name.split("/", 1)[0] for name in names} == {"Auto-cut-lite"}
    assert "Auto-cut-lite/Auto-Cut-Lite新手部署说明.md" in names
    assert "Auto-cut-lite/Auto-Cut-Lite部署成功后操作说明.md" in names
    assert "Auto-cut-lite/一键安装或升级-Auto-Cut-Lite.cmd" in names
    assert "Auto-cut-lite/一键卸载-Auto-Cut-Lite.cmd" in names
    assert "Auto-cut-lite/START-AUTO-CUT-LITE.cmd" not in names
    assert "Auto-cut-lite/BEGINNER_DEPLOYMENT.md" not in names
    assert "Auto-cut-lite/CODEX_NEXT_STEPS.md" not in names
    assert "Auto-cut-lite/installer/one_click_deploy.ps1" in names
    assert "Auto-cut-lite/PACKAGE-MANIFEST.json" in names
    assert not any(name.startswith("auto-cut-lite/") for name in names)


def test_builder_rejects_a_dirty_release_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "release-test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Release Test"], cwd=repo, check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
    tracked.write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="worktree is dirty"):
        build_lite_plugin.build(repo, tmp_path / "release.zip")
