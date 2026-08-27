from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
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
    catch_index = deployer.index("\ncatch {", report_index)
    failed_index = deployer.index("$report.deployment_status = 'failed'", catch_index)
    not_evaluated_index = deployer.index("$report.readiness = 'not_evaluated'", catch_index)
    failure_report_index = deployer.index(
        "Write-DeploymentReport -Payload $report", failed_index
    )

    assert commit_index < committed_index < installed_index < report_index < catch_index
    assert catch_index < failed_index < failure_report_index
    assert catch_index < not_evaluated_index < failure_report_index


def test_uninstaller_is_receipt_scoped_and_preserves_unrelated_registrations() -> None:
    uninstaller = UNINSTALL_PATH.read_text(encoding="utf-8")

    assert "'uninstall'" in uninstaller
    assert "'remove-named'" in uninstaller
    assert "unrelated_plugins_unchanged" in uninstaller
    assert "unrelated_unchanged" in uninstaller
    assert "Remove-OwnedPluginTree -Path $targetRoot" in uninstaller
    assert "Remove-Item -LiteralPath $stateRoot -Recurse" not in uninstaller
    assert "Deployment report target root does not match" in uninstaller


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
