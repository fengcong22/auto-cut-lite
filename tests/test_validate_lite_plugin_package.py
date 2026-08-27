from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.release import build_lite_plugin
from scripts.release.validate_lite_plugin_package import validate

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "auto-cut-lite"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _stage_package(
    tmp_path: Path,
    mutate: Callable[[Path], None] | None = None,
) -> tuple[Path, Path]:
    stage_parent = tmp_path / "stage"
    root = stage_parent / build_lite_plugin.WORKSPACE_NAME
    shutil.copytree(
        PLUGIN_ROOT,
        root,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    portable_path = root / "PORTABLE-CAPABILITIES.json"
    portable = json.loads(portable_path.read_text(encoding="utf-8"))
    for capability in portable["capabilities"]:
        for raw_path in capability["required_paths"]:
            target = root.joinpath(*Path(raw_path).parts)
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"offline-validator-fixture\n")

    if mutate is not None:
        mutate(root)

    inventory = []
    for path in sorted(
        (candidate for candidate in root.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        inventory.append({"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)})
    _write_json(
        root / "PACKAGE-MANIFEST.json",
        {
            "name": build_lite_plugin.PLUGIN_NAME,
            "version": build_lite_plugin.PLUGIN_VERSION,
            "files": inventory,
        },
    )

    archive_path = tmp_path / build_lite_plugin.ARCHIVE_NAME
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(stage_parent).as_posix(),
        ):
            archive.write(path, path.relative_to(stage_parent).as_posix())

    receipt_path = tmp_path / f"{archive_path.name}.receipt.json"
    _write_json(
        receipt_path,
        {
            "archive_root": build_lite_plugin.WORKSPACE_NAME,
            "archive_sha256": _sha256(archive_path),
            "plugin_version": build_lite_plugin.PLUGIN_VERSION,
        },
    )
    return archive_path, receipt_path


def _mutate_json(root: Path, relative: str, update: Callable[[dict[str, object]], None]) -> None:
    path = root / relative
    payload = json.loads(path.read_text(encoding="utf-8"))
    update(payload)
    _write_json(path, payload)


def test_offline_validator_proves_workspace_skill_and_review_runtime_contract(
    tmp_path: Path,
) -> None:
    archive, receipt = _stage_package(tmp_path)

    result = validate(archive, receipt, tmp_path / "extract")

    assert result["status"] == "pass"
    assert result["portable_capability_closure"] == "pass"
    assert result["workspace_skill_count"] == 17
    assert result["workspace_skill_scope"] == "repo"
    assert result["workspace_skill_label"] == "Auto-cut-lite"
    assert result["plugin_manifest_exposes_skills"] is False
    assert result["plugin_top_level_skills_present"] is False
    assert result["review_runtime_contract"] == "pass"
    assert result["review_runtime_required_path_count"] == 2


def _manifest_exposes_skills(root: Path) -> None:
    _mutate_json(
        root,
        ".codex-plugin/plugin.json",
        lambda payload: payload.__setitem__("skills", "./skills"),
    )


def _add_top_level_skills(root: Path) -> None:
    skill = root / "skills" / "auto-cut-lite" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: auto-cut-lite\n---\n", encoding="utf-8")


def _change_portable_identity(root: Path) -> None:
    _mutate_json(
        root,
        "PORTABLE-CAPABILITIES.json",
        lambda payload: payload.__setitem__("plugin_version", "0.0.0"),
    )


def _change_portable_plugin_name(root: Path) -> None:
    _mutate_json(
        root,
        "PORTABLE-CAPABILITIES.json",
        lambda payload: payload.__setitem__("plugin_name", "other-plugin"),
    )


def _remove_workspace_skill(root: Path) -> None:
    shutil.rmtree(root / "workspace-payload" / "skills" / "auto-cut-lite")


def _change_workspace_scope(root: Path) -> None:
    def update(payload: dict[str, object]) -> None:
        workspace = payload["workspace_installation"]
        assert isinstance(workspace, dict)
        workspace["scope"] = "user"

    _mutate_json(root, "PORTABLE-CAPABILITIES.json", update)


def _change_workspace_label(root: Path) -> None:
    def update(payload: dict[str, object]) -> None:
        workspace = payload["workspace_installation"]
        assert isinstance(workspace, dict)
        workspace["label"] = "Personal"

    _mutate_json(root, "PORTABLE-CAPABILITIES.json", update)


def _remove_review_runtime_declaration(root: Path) -> None:
    def update(payload: dict[str, object]) -> None:
        capabilities = payload["capabilities"]
        assert isinstance(capabilities, list)
        review = next(
            row
            for row in capabilities
            if isinstance(row, dict) and row.get("id") == "review_document_and_replacement_timebase"
        )
        paths = review["required_paths"]
        assert isinstance(paths, list)
        paths.remove("runtime/scripts/utils/review_document_runner.py")

    _mutate_json(root, "PORTABLE-CAPABILITIES.json", update)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_manifest_exposes_skills, "must not expose user-scoped skills"),
        (_add_top_level_skills, "must not contain a top-level skills directory"),
        (_change_portable_identity, "identity does not match"),
        (_change_portable_plugin_name, "identity does not match"),
        (_remove_workspace_skill, "portable skill surface mismatch"),
        (_change_workspace_scope, "workspace installation contract is invalid"),
        (_change_workspace_label, "workspace installation contract is invalid"),
        (_remove_review_runtime_declaration, "omits required runtime paths"),
    ],
)
def test_offline_validator_rejects_self_consistent_but_invalid_deployment_contracts(
    tmp_path: Path,
    mutate: Callable[[Path], None],
    message: str,
) -> None:
    archive, receipt = _stage_package(tmp_path, mutate)

    with pytest.raises(ValueError, match=message):
        validate(archive, receipt, tmp_path / "extract")
