from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from utils.revision_models import load_revision_request
from utils.revision_runner import execute_revision_request
from utils.runtime_integrity import (
    RuntimeIntegrityError,
    validate_deployed_lite_runtime,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fake_deployment(tmp_path: Path) -> dict[str, Path]:
    state_root = tmp_path / "LocalAppData" / "Auto-Cut" / "auto-cut-lite"
    plugin_root = state_root / "marketplace" / "plugins" / "auto-cut-lite"
    runtime_root = plugin_root / "runtime"
    runtime_python = plugin_root / ".runtime-venv" / "Scripts" / "python.exe"
    version = "1.5.3-test"

    runtime_files = {
        "scripts/jy_wrapper.py": b"# wrapper\n",
        "scripts/utils/lite_revision.py": b"# lite\n",
        "scripts/utils/revision_runner.py": b"# runner\n",
        "scripts/utils/runtime_integrity.py": b"# integrity\n",
        "requirements.txt": b"requests==2.32.5\n",
    }
    for relative, content in runtime_files.items():
        target = runtime_root.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    runtime_python.parent.mkdir(parents=True, exist_ok=True)
    runtime_python.write_bytes(b"python")

    plugin_manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    _write_json(plugin_manifest_path, {"name": "auto-cut-lite", "version": version})
    manifest_rows = []
    package_files = {".codex-plugin/plugin.json": plugin_manifest_path}
    package_files.update(
        {
            f"runtime/{PurePosixPath(relative).as_posix()}": runtime_root.joinpath(
                *PurePosixPath(relative).parts
            )
            for relative in runtime_files
        }
    )
    for relative, target in sorted(package_files.items()):
        manifest_rows.append(
            {
                "path": relative,
                "size": target.stat().st_size,
                "sha256": _sha256(target),
            }
        )
    package_manifest_path = plugin_root / "PACKAGE-MANIFEST.json"
    _write_json(
        package_manifest_path,
        {"name": "auto-cut-lite", "version": version, "files": manifest_rows},
    )

    report_path = state_root / "deployment-report.json"
    receipt_path = state_root / "workspace-install-receipt.json"
    _write_json(
        receipt_path,
        {
            "status": "installed",
            "plugin_name": "auto-cut-lite",
            "plugin_version": version,
            "deployment_report_path": str(report_path),
            "plugin_root": str(plugin_root),
            "runtime_root": str(runtime_root),
            "installed_package_sha256": {
                "PACKAGE-MANIFEST.json": _sha256(package_manifest_path)
            },
        },
    )
    _write_json(
        report_path,
        {
            "schema_version": 2,
            "plugin_name": "auto-cut-lite",
            "plugin_version": version,
            "deployment_status": "installed",
            "target_root": str(plugin_root),
            "plugin_manifest_path": str(plugin_manifest_path),
            "runtime_root": str(runtime_root),
            "workspace_receipt_path": str(receipt_path),
            "components": {
                "python": {
                    "status": "detected",
                    "dependencies": "installed",
                    "runtime_path": str(runtime_python),
                }
            },
        },
    )
    return {
        "report": report_path,
        "receipt": receipt_path,
        "plugin": plugin_root,
        "runtime": runtime_root,
        "python": runtime_python,
        "manifest": package_manifest_path,
}


def _validate(paths: dict[str, Path]) -> dict[str, object]:
    return validate_deployed_lite_runtime(
        deployment_report_path=paths["report"],
        active_runtime_root=paths["runtime"],
        active_python=paths["python"],
    )


def test_matching_deployed_runtime_passes(tmp_path: Path) -> None:
    paths = _fake_deployment(tmp_path)

    result = _validate(paths)

    assert result["status"] == "pass"
    assert result["validated_runtime_file_count"] == 5
    assert result["runtime_root"] == str(paths["runtime"])


@pytest.mark.parametrize(
    "deployment_status",
    ["dependency_commit_pending", "installed_report_pending", "failed"],
)
def test_unresolved_deployment_attempt_stops_execution(
    tmp_path: Path, deployment_status: str
) -> None:
    paths = _fake_deployment(tmp_path)
    attempt_path = paths["report"].with_name("deployment-attempt-report.json")
    _write_json(
        attempt_path,
        {
            "schema_version": 2,
            "plugin_name": "auto-cut-lite",
            "deployment_status": deployment_status,
            "previous_deployment_report_preserved": False,
        },
    )

    with pytest.raises(RuntimeIntegrityError, match="deployment attempt report"):
        _validate(paths)


def test_failed_attempt_with_verified_previous_report_remains_executable(tmp_path: Path) -> None:
    paths = _fake_deployment(tmp_path)
    attempt_path = paths["report"].with_name("deployment-attempt-report.json")
    _write_json(
        attempt_path,
        {
            "schema_version": 2,
            "plugin_name": "auto-cut-lite",
            "deployment_status": "failed",
            "previous_deployment_report_preserved": True,
        },
    )

    assert _validate(paths)["status"] == "pass"


@pytest.mark.parametrize("failure_mode", ["modified", "missing"])
def test_runtime_file_drift_fails_closed(tmp_path: Path, failure_mode: str) -> None:
    paths = _fake_deployment(tmp_path)
    target = paths["runtime"] / "scripts" / "utils" / "revision_runner.py"
    if failure_mode == "modified":
        target.write_text("# modified after deployment\n", encoding="utf-8")
    else:
        target.unlink()

    with pytest.raises(RuntimeIntegrityError, match="manifest drift"):
        _validate(paths)


def test_uninventoried_runtime_code_fails_closed(tmp_path: Path) -> None:
    paths = _fake_deployment(tmp_path)
    extra = paths["runtime"] / "scripts" / "utils" / "hot_patch.py"
    extra.write_text("raise RuntimeError('unexpected')\n", encoding="utf-8")

    with pytest.raises(RuntimeIntegrityError, match="unlisted files"):
        _validate(paths)


def test_target_local_asset_indexes_and_sync_cache_are_mutable(tmp_path: Path) -> None:
    paths = _fake_deployment(tmp_path)
    (paths["runtime"] / "data").mkdir(parents=True, exist_ok=True)
    (paths["runtime"] / "data" / "favorite_flower_texts.local.csv").write_text(
        "identifier,name\nlocal-1,Local\n", encoding="utf-8"
    )
    sync_asset = paths["runtime"] / "assets" / "jy_sync" / "local.mp3"
    sync_asset.parent.mkdir(parents=True, exist_ok=True)
    sync_asset.write_bytes(b"target-local-cache")

    result = _validate(paths)

    assert result["status"] == "pass"


@pytest.mark.parametrize("failure_mode", ["missing", "invalid", "receipt_mismatch"])
def test_manifest_failure_stops_execution(tmp_path: Path, failure_mode: str) -> None:
    paths = _fake_deployment(tmp_path)
    if failure_mode == "missing":
        paths["manifest"].unlink()
    elif failure_mode == "invalid":
        paths["manifest"].write_text("not json", encoding="utf-8")
    else:
        paths["manifest"].write_text(
            paths["manifest"].read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )

    with pytest.raises(RuntimeIntegrityError, match="(?i)manifest"):
        _validate(paths)


@pytest.mark.parametrize("failure_mode", ["failed_status", "missing_python", "wrong_root"])
def test_invalid_deployment_report_stops_execution(
    tmp_path: Path, failure_mode: str
) -> None:
    paths = _fake_deployment(tmp_path)
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    if failure_mode == "failed_status":
        report["deployment_status"] = "failed"
    elif failure_mode == "missing_python":
        report["components"]["python"].pop("runtime_path")
    else:
        report["runtime_root"] = str(tmp_path / "another-runtime")
    _write_json(paths["report"], report)

    with pytest.raises(RuntimeIntegrityError):
        _validate(paths)


def test_lite_gate_fails_before_draft_creation(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    _write_json(
        request_path,
        {
            "workflow_mode": "lite",
            "project": {
                "draft_name": "MustNotBeCreated",
                "source_video": str(tmp_path / "source.mp4"),
            },
        },
    )
    request = load_revision_request(str(request_path))
    drafts_root = tmp_path / "drafts"

    with mock.patch(
        "utils.runtime_integrity.validate_current_lite_runtime",
        side_effect=RuntimeIntegrityError("forced integrity failure"),
    ):
        with pytest.raises(RuntimeIntegrityError, match="forced integrity failure"):
            execute_revision_request(
                request,
                drafts_root=str(drafts_root),
                mock_media=False,
            )

    assert not (drafts_root / "MustNotBeCreated").exists()
