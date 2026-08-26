from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO_ROOT / "plugins" / "auto-cut-lite" / "installer" / "manage_workspace.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("manage_workspace", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    user_profile = tmp_path / "User"
    local_app_data = tmp_path / "LocalAppData"
    user_profile.mkdir()
    local_app_data.mkdir()
    monkeypatch.setenv("USERPROFILE", str(user_profile))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    workspace = user_profile / "Documents" / "Codex" / "Auto-cut-lite"
    state = local_app_data / "Auto-Cut" / "auto-cut-lite"
    receipt = state / "workspace-install-receipt.json"
    return workspace, state, receipt


def _write_package_manifest(plugin: Path, *, version: str = "1.3.0") -> None:
    manifest_path = plugin / "PACKAGE-MANIFEST.json"
    if manifest_path.exists():
        manifest_path.unlink()
    files = []
    for path in sorted(item for item in plugin.rglob("*") if item.is_file()):
        relative = path.relative_to(plugin).as_posix()
        data = path.read_bytes()
        files.append(
            {
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest_path.write_text(
        json.dumps({"name": "auto-cut-lite", "version": version, "files": files}),
        encoding="utf-8",
    )


def _plugin(
    tmp_path: Path,
    *,
    expose_skills: bool = False,
    parent: str = "plugin",
    leaf: str = "auto-cut-lite",
) -> Path:
    plugin = tmp_path / parent / leaf
    manifest = {
        "name": "auto-cut-lite",
        "version": "1.3.0",
    }
    if expose_skills:
        manifest["skills"] = "./skills/"
    manifest_path = plugin / ".codex-plugin" / "plugin.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (plugin / "AGENTS.md").write_text("# New workspace rules\n", encoding="utf-8")
    for name in ("auto-cut", "auto-cut-audio-restoration"):
        skill = plugin / "workspace-payload" / "skills" / name
        (skill / "agents").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture\n---\n", encoding="utf-8"
        )
        (skill / "agents" / "openai.yaml").write_text("interface: {}\n", encoding="utf-8")
    _write_package_manifest(plugin)
    return plugin


def test_install_and_rollback_workspace_are_scoped_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    _, state, receipt = _paths(tmp_path, monkeypatch)
    workspace = tmp_path / "ExternalWorkspaces" / "Auto-cut-lite"
    plugin = _plugin(tmp_path)

    old_skill = workspace / ".codex" / "skills" / "auto-cut"
    old_skill.mkdir(parents=True)
    (old_skill / "old.txt").write_text("old skill\n", encoding="utf-8")
    unrelated = workspace / ".codex" / "skills" / "unrelated-skill"
    unrelated.mkdir(parents=True)
    (unrelated / "keep.txt").write_text("keep\n", encoding="utf-8")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text("# Old rules\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("preserve\n", encoding="utf-8")

    result = helper.install_workspace(
        plugin_root=plugin,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )

    assert result["status"] == "installed"
    assert result["workspace_scope"] == "repo"
    assert result["workspace_label"] == "Auto-cut-lite"
    assert result["plugin_manifest_exposes_skills"] is False
    assert result["plugin_top_level_skills_present"] is False
    assert result["workspace_skill_payload"] == "workspace-payload/skills"
    assert result["deployment_report_path"] == str(state / "deployment-report.json")
    assert result["plugin_root"] == str(plugin)
    assert result["runtime_root"] == str(plugin / "runtime")
    assert result["workspace_skill_count"] == 2
    assert set(result["installed_skill_names"]) == {
        "auto-cut",
        "auto-cut-audio-restoration",
    }
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "# New workspace rules\n"
    assert (workspace / ".codex" / "skills" / "auto-cut" / "SKILL.md").is_file()
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "preserve\n"

    rollback = helper.rollback_workspace(receipt_path=receipt)

    assert rollback["status"] == "rolled_back"
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "# Old rules\n"
    assert (old_skill / "old.txt").read_text(encoding="utf-8") == "old skill\n"
    assert not (workspace / ".codex" / "skills" / "auto-cut-audio-restoration").exists()
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_workspace_installer_rejects_user_scoped_plugin_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path, expose_skills=True)

    with pytest.raises(ValueError, match="must not expose user-scoped skills"):
        helper.install_workspace(
            plugin_root=plugin,
            workspace_root=workspace,
            state_root=state,
            receipt_path=receipt,
        )


def test_workspace_installer_restores_previous_content_when_receipt_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path)

    old_skill = workspace / ".codex" / "skills" / "auto-cut"
    old_skill.mkdir(parents=True)
    (old_skill / "old.txt").write_text("old skill\n", encoding="utf-8")
    unrelated = workspace / ".codex" / "skills" / "unrelated-skill"
    unrelated.mkdir(parents=True)
    (unrelated / "keep.txt").write_text("keep\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("# Old rules\n", encoding="utf-8")

    def fail_receipt_write(*args, **kwargs):
        raise OSError("simulated receipt write failure")

    monkeypatch.setattr(helper, "_atomic_write_json", fail_receipt_write)

    with pytest.raises(OSError, match="simulated receipt write failure"):
        helper.install_workspace(
            plugin_root=plugin,
            workspace_root=workspace,
            state_root=state,
            receipt_path=receipt,
        )

    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "# Old rules\n"
    assert (old_skill / "old.txt").read_text(encoding="utf-8") == "old skill\n"
    assert not (workspace / ".codex" / "skills" / "auto-cut-audio-restoration").exists()
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert not receipt.exists()


def test_workspace_installer_rejects_a_different_workspace_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path)

    with pytest.raises(ValueError, match="folder name must be exactly"):
        helper.install_workspace(
            plugin_root=plugin,
            workspace_root=workspace.with_name("wrong-name"),
            state_root=state,
            receipt_path=receipt,
        )


def test_workspace_installer_rejects_a_relative_workspace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    _, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path)

    with pytest.raises(ValueError, match="must be an absolute path"):
        helper.install_workspace(
            plugin_root=plugin,
            workspace_root=Path("Auto-cut-lite"),
            state_root=state,
            receipt_path=receipt,
        )


def test_workspace_installer_relocates_an_active_workspace_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path)

    first_install = helper.install_workspace(
        plugin_root=plugin,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )
    unrelated_old = workspace / ".codex" / "skills" / "unrelated-old"
    unrelated_old.mkdir()
    (unrelated_old / "keep.txt").write_text("keep old\n", encoding="utf-8")

    relocated = tmp_path / "Other" / "Auto-cut-lite"
    target_skill = relocated / ".codex" / "skills" / "auto-cut"
    target_skill.mkdir(parents=True)
    (target_skill / "target-old.txt").write_text("target old\n", encoding="utf-8")
    (relocated / "AGENTS.md").write_text("# Target old rules\n", encoding="utf-8")
    unrelated_target = relocated / ".codex" / "skills" / "unrelated-target"
    unrelated_target.mkdir()
    (unrelated_target / "keep.txt").write_text("keep target\n", encoding="utf-8")

    result = helper.install_workspace(
        plugin_root=plugin,
        workspace_root=relocated,
        state_root=state,
        receipt_path=receipt,
    )

    assert result["workspace_action"] == "relocated"
    assert result["relocated_from_workspace_root"] == str(workspace)
    assert result["previous_install_receipt"] == first_install
    assert not (workspace / "AGENTS.md").exists()
    assert not (workspace / ".codex" / "skills" / "auto-cut").exists()
    assert not (workspace / ".codex" / "skills" / "auto-cut-audio-restoration").exists()
    assert not (workspace / "PACKAGE-MANIFEST.json").exists()
    assert (relocated / "PACKAGE-MANIFEST.json").is_file()
    assert set(result["relocated_package_files"]) == set(first_install["installed_package_files"])
    assert (unrelated_old / "keep.txt").read_text(encoding="utf-8") == "keep old\n"
    assert (relocated / "AGENTS.md").read_text(encoding="utf-8") == "# New workspace rules\n"
    assert (unrelated_target / "keep.txt").read_text(encoding="utf-8") == "keep target\n"

    rollback = helper.rollback_workspace(receipt_path=receipt)

    assert rollback["action"] == "restored_relocated_workspace"
    assert rollback["restored_workspace_root"] == str(workspace)
    assert json.loads(receipt.read_text(encoding="utf-8")) == first_install
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "# New workspace rules\n"
    assert (workspace / ".codex" / "skills" / "auto-cut" / "SKILL.md").is_file()
    assert (workspace / "PACKAGE-MANIFEST.json").is_file()
    assert not (relocated / "PACKAGE-MANIFEST.json").exists()
    assert (target_skill / "target-old.txt").read_text(encoding="utf-8") == "target old\n"
    assert (relocated / "AGENTS.md").read_text(encoding="utf-8") == "# Target old rules\n"
    assert (unrelated_target / "keep.txt").read_text(encoding="utf-8") == "keep target\n"


def test_workspace_relocation_refuses_modified_managed_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path)

    helper.install_workspace(
        plugin_root=plugin,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )
    changed = workspace / ".codex" / "skills" / "auto-cut" / "SKILL.md"
    changed.write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed after deployment"):
        helper.install_workspace(
            plugin_root=plugin,
            workspace_root=tmp_path / "Other" / "Auto-cut-lite",
            state_root=state,
            receipt_path=receipt,
        )


def test_workspace_relocation_restores_both_roots_when_receipt_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path)
    first_install = helper.install_workspace(
        plugin_root=plugin,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )

    relocated = tmp_path / "Other" / "Auto-cut-lite"
    target_skill = relocated / ".codex" / "skills" / "auto-cut"
    target_skill.mkdir(parents=True)
    (target_skill / "target-old.txt").write_text("target old\n", encoding="utf-8")
    (relocated / "AGENTS.md").write_text("# Target old rules\n", encoding="utf-8")

    def fail_receipt_write(*args, **kwargs):
        raise OSError("simulated relocation receipt failure")

    monkeypatch.setattr(helper, "_atomic_write_json", fail_receipt_write)

    with pytest.raises(OSError, match="simulated relocation receipt failure"):
        helper.install_workspace(
            plugin_root=plugin,
            workspace_root=relocated,
            state_root=state,
            receipt_path=receipt,
        )

    assert json.loads(receipt.read_text(encoding="utf-8")) == first_install
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "# New workspace rules\n"
    assert (workspace / ".codex" / "skills" / "auto-cut" / "SKILL.md").is_file()
    assert (workspace / "PACKAGE-MANIFEST.json").is_file()
    assert not (relocated / "PACKAGE-MANIFEST.json").exists()
    assert (target_skill / "target-old.txt").read_text(encoding="utf-8") == "target old\n"
    assert (relocated / "AGENTS.md").read_text(encoding="utf-8") == "# Target old rules\n"


def test_fresh_extracted_package_is_adopted_as_combined_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    _, state, receipt = _paths(tmp_path, monkeypatch)
    workspace = _plugin(tmp_path, leaf="Auto-cut-lite")
    manifest_before = (workspace / "PACKAGE-MANIFEST.json").read_bytes()

    result = helper.install_workspace(
        plugin_root=workspace,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )

    assert result["workspace_mode"] == "combined_package_workspace"
    assert result["workspace_package_root"] == str(workspace)
    assert result["package_sync_action"] == "adopted_extracted_package"
    assert result["package_written_files"] == []
    assert len(result["package_adopted_files"]) == result["workspace_package_file_count"]
    assert (workspace / ".codex" / "skills" / "auto-cut" / "SKILL.md").is_file()

    helper.rollback_workspace(receipt_path=receipt)

    assert (workspace / "PACKAGE-MANIFEST.json").read_bytes() == manifest_before
    assert (workspace / ".codex-plugin" / "plugin.json").is_file()


def test_old_split_workspace_migrates_to_combined_mode_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path)
    first = helper.install_workspace(
        plugin_root=plugin,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )
    for relative in first["installed_package_files"]:
        target = workspace.joinpath(*relative.split("/"))
        target.unlink()
    legacy = {
        key: value
        for key, value in first.items()
        if key
        not in {
            "workspace_mode",
            "workspace_package_root",
            "workspace_package_file_count",
            "installed_package_files",
            "installed_package_sha256",
            "package_written_files",
            "package_adopted_files",
            "package_retained_files",
            "backed_up_package_files",
            "package_sync_action",
        }
    }
    receipt.write_text(json.dumps(legacy), encoding="utf-8")

    migrated = helper.install_workspace(
        plugin_root=plugin,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )

    assert migrated["package_sync_action"] == "migrated_split_workspace"
    assert set(migrated["package_written_files"]) == set(migrated["installed_package_files"])
    assert (workspace / "PACKAGE-MANIFEST.json").is_file()

    helper.rollback_workspace(receipt_path=receipt)

    assert json.loads(receipt.read_text(encoding="utf-8")) == legacy
    assert not (workspace / "PACKAGE-MANIFEST.json").exists()
    assert (workspace / ".codex" / "skills" / "auto-cut" / "SKILL.md").is_file()


def test_combined_upgrade_replaces_adds_and_removes_managed_package_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    old_plugin = _plugin(tmp_path, parent="old-package")
    (old_plugin / "obsolete.txt").write_text("old only\n", encoding="utf-8")
    _write_package_manifest(old_plugin)
    first = helper.install_workspace(
        plugin_root=old_plugin,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )
    old_manifest = (workspace / "PACKAGE-MANIFEST.json").read_bytes()

    new_plugin = tmp_path / "new-package" / "auto-cut-lite"
    shutil.copytree(old_plugin, new_plugin)
    (new_plugin / "obsolete.txt").unlink()
    plugin_manifest = new_plugin / ".codex-plugin" / "plugin.json"
    plugin_manifest.write_text(
        json.dumps({"name": "auto-cut-lite", "version": "1.4.0"}), encoding="utf-8"
    )
    (new_plugin / "new.txt").write_text("new only\n", encoding="utf-8")
    _write_package_manifest(new_plugin, version="1.4.0")

    upgraded = helper.install_workspace(
        plugin_root=new_plugin,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )

    assert upgraded["package_sync_action"] == "upgraded"
    assert not (workspace / "obsolete.txt").exists()
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "new only\n"
    assert json.loads(plugin_manifest.read_text(encoding="utf-8"))["version"] == "1.4.0"
    assert json.loads((workspace / ".codex-plugin" / "plugin.json").read_text())["version"] == "1.4.0"

    helper.rollback_workspace(receipt_path=receipt)

    assert json.loads(receipt.read_text(encoding="utf-8")) == first
    assert (workspace / "obsolete.txt").read_text(encoding="utf-8") == "old only\n"
    assert not (workspace / "new.txt").exists()
    assert (workspace / "PACKAGE-MANIFEST.json").read_bytes() == old_manifest


def test_combined_install_refuses_unmanaged_package_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path)
    collision = workspace / ".codex-plugin" / "plugin.json"
    collision.parent.mkdir(parents=True)
    collision.write_text("user file\n", encoding="utf-8")

    with pytest.raises(ValueError, match="collides with an unmanaged file"):
        helper.install_workspace(
            plugin_root=plugin,
            workspace_root=workspace,
            state_root=state,
            receipt_path=receipt,
        )

    assert collision.read_text(encoding="utf-8") == "user file\n"
    assert not receipt.exists()


def test_combined_upgrade_refuses_modified_managed_package_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path)
    helper.install_workspace(
        plugin_root=plugin,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )
    changed = workspace / ".codex-plugin" / "plugin.json"
    changed.write_text("changed by user\n", encoding="utf-8")

    with pytest.raises(ValueError, match="package file changed after deployment"):
        helper.install_workspace(
            plugin_root=plugin,
            workspace_root=workspace,
            state_root=state,
            receipt_path=receipt,
        )


def test_receipt_failure_removes_new_combined_package_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    plugin = _plugin(tmp_path)

    def fail_receipt_write(*args, **kwargs):
        raise OSError("simulated combined receipt failure")

    monkeypatch.setattr(helper, "_atomic_write_json", fail_receipt_write)
    with pytest.raises(OSError, match="simulated combined receipt failure"):
        helper.install_workspace(
            plugin_root=plugin,
            workspace_root=workspace,
            state_root=state,
            receipt_path=receipt,
        )

    assert not (workspace / "PACKAGE-MANIFEST.json").exists()
    assert not (workspace / ".codex-plugin" / "plugin.json").exists()


def test_upgrade_receipt_failure_restores_previous_package_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load_helper()
    workspace, state, receipt = _paths(tmp_path, monkeypatch)
    old_plugin = _plugin(tmp_path, parent="old-upgrade-package")
    (old_plugin / "obsolete.txt").write_text("restore me\n", encoding="utf-8")
    _write_package_manifest(old_plugin)
    first = helper.install_workspace(
        plugin_root=old_plugin,
        workspace_root=workspace,
        state_root=state,
        receipt_path=receipt,
    )
    old_manifest = (workspace / "PACKAGE-MANIFEST.json").read_bytes()

    new_plugin = tmp_path / "failed-upgrade-package" / "auto-cut-lite"
    shutil.copytree(old_plugin, new_plugin)
    (new_plugin / "obsolete.txt").unlink()
    (new_plugin / "new.txt").write_text("remove me\n", encoding="utf-8")
    _write_package_manifest(new_plugin)

    original_write = helper._atomic_write_json

    def fail_new_receipt(path, payload, state_root):
        if payload.get("previous_install_receipt") is not None:
            raise OSError("simulated upgrade receipt failure")
        return original_write(path, payload, state_root)

    monkeypatch.setattr(helper, "_atomic_write_json", fail_new_receipt)
    with pytest.raises(OSError, match="simulated upgrade receipt failure"):
        helper.install_workspace(
            plugin_root=new_plugin,
            workspace_root=workspace,
            state_root=state,
            receipt_path=receipt,
        )

    assert json.loads(receipt.read_text(encoding="utf-8")) == first
    assert (workspace / "obsolete.txt").read_text(encoding="utf-8") == "restore me\n"
    assert not (workspace / "new.txt").exists()
    assert (workspace / "PACKAGE-MANIFEST.json").read_bytes() == old_manifest
