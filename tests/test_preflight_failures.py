# ruff: noqa: E402
"""Regression coverage for denied CLI/readiness and terminal failure reporting."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from utils import atomic_io
from utils import review_document_runner as runner

from tests import test_review_document_runner as runner_support
from tests import test_review_document_runner_source_pairs as manifest_support


def _options(root):
    cli = root / "lark-cli.exe"
    cli.write_bytes(b"not executed")
    return {
        "doc_url": "https://example.feishu.cn/docx/opaque-document",
        "job_root": root / "job",
        "drafts_root": root / "drafts",
        "package_zip": root / "delivery.zip",
        "cache_root": root / "cache",
        "readiness_path": root / "runtime-readiness.json",
        "lark_cli": cli,
    }


def _identity(command):
    output = (
        "version 1.2.3"
        if command[-1] == "--version"
        else json.dumps({"available": True, "identity": "user", "defaultAs": "user"})
    )
    return subprocess.CompletedProcess(command, 0, output, "")


@pytest.mark.parametrize("cli_failure", ["missing", "denied", "timeout", "exit", "identity"])
def test_cli_failure_survives_denied_readiness_and_can_recover(tmp_path, cli_failure):
    options = _options(tmp_path)
    readiness = options["readiness_path"]
    old_bytes = b'{"schema_version":1,"lark":{"status":"verified"}}\n'
    readiness.write_bytes(old_bytes)
    denied_opens = []
    real_open = os.open

    def deny_readiness(path, *args, **kwargs):
        if Path(path).name.startswith(".runtime-readiness.json."):
            denied_opens.append(path)
            raise PermissionError("private diagnostic directory")
        return real_open(path, *args, **kwargs)

    def fail_cli(command):
        if cli_failure == "denied":
            raise PermissionError("private CLI path")
        if cli_failure == "timeout":
            raise subprocess.TimeoutExpired(command, 10, output="private provider output")
        if cli_failure == "identity" and command[-1] == "--version":
            return _identity(command)
        return subprocess.CompletedProcess(command, 9, "", "private provider output")

    options["lark_runner"] = fail_cli
    if cli_failure == "missing":
        options["lark_cli"].unlink()
    events = []
    started = time.monotonic()
    with (
        runner_support.ReviewDocumentRunnerTests()._patched_runtime(),
        patch.object(atomic_io.os, "open", side_effect=deny_readiness),
        pytest.raises(runner.ReviewDocumentRunError) as raised,
    ):
        runner.run_review_document(**options, progress=events.append)
    assert time.monotonic() - started < 5
    assert len(denied_opens) == 1
    assert readiness.read_bytes() == old_bytes
    result = raised.value.result
    expected = (
        "lark_user_identity_unavailable" if cli_failure == "identity" else "lark_cli_unavailable"
    )
    detail = result["failure_details"]["preflight"]
    assert detail["code"] == expected
    assert detail["readiness_persistence"]["code"] == "readiness_write_failed"
    assert result["phases"]["preflight"]["status"] == "failed"
    assert result["phases"]["preflight"]["persisted_status"] == "failed"
    assert result["phases"]["document_fetch"]["persisted_status"] == "skipped"
    assert any(e["phase"] == "preflight" and e["status"] == "failed" for e in events)
    serialized = json.dumps(result)
    for name in ("job_state.json", "job_timing.json"):
        serialized += (options["job_root"] / name).read_text(encoding="utf-8")
    assert "private diagnostic directory" not in serialized
    assert "private CLI path" not in serialized
    assert "private provider output" not in serialized

    # Same job reruns preflight once access is restored; a downstream failure
    # stops this test before any media editing or external provider request.
    options["lark_cli"].write_bytes(b"not executed")
    options["lark_runner"] = _identity
    with (
        runner_support.ReviewDocumentRunnerTests()._patched_runtime(),
        patch.object(
            runner,
            "fetch_lark_document",
            side_effect=runner.ReviewDocumentIntakeError(
                "document_fetch_failed", "Controlled downstream failure"
            ),
        ),
        pytest.raises(runner.ReviewDocumentRunError) as recovered,
    ):
        runner.run_review_document(**options)
    assert recovered.value.result["phases"]["preflight"]["persisted_status"] == "complete"
    assert recovered.value.result["phases"]["document_fetch"]["persisted_status"] == "failed"


def test_readiness_primary_failure_is_not_misreported_as_identity_failure(tmp_path):
    options = _options(tmp_path)
    with (
        runner_support.ReviewDocumentRunnerTests()._patched_runtime(),
        patch(
            "utils.review_document_intake._atomic_write_json",
            side_effect=PermissionError("private"),
        ),
        patch.object(runner, "invalidate_lark_readiness") as invalidate,
        pytest.raises(runner.ReviewDocumentRunError) as raised,
    ):
        runner.run_review_document(**options, lark_runner=_identity)
    assert raised.value.result["failure_details"]["preflight"]["code"] == "readiness_write_failed"
    assert raised.value.result["phases"]["preflight"]["persisted_status"] == "failed"
    invalidate.assert_not_called()


@pytest.mark.parametrize("deny_snapshot", [False, True])
def test_state_diagnostic_failure_cannot_mask_preflight_error(tmp_path, deny_snapshot):
    options = _options(tmp_path)
    real_snapshot = runner.JobStateStore.snapshot

    def snapshot(store):
        if deny_snapshot:
            raise PermissionError("private state read path")
        return real_snapshot(store)

    with (
        runner_support.ReviewDocumentRunnerTests()._patched_runtime(),
        patch.object(
            runner.JobStateStore,
            "fail_phase",
            side_effect=PermissionError("private state write path"),
        ) as save,
        patch.object(runner.JobStateStore, "snapshot", snapshot),
        pytest.raises(runner.ReviewDocumentRunError) as raised,
    ):
        runner.run_review_document(
            **options,
            lark_runner=lambda command: subprocess.CompletedProcess(command, 9, "", "private"),
        )
    assert save.call_count == 2
    result = raised.value.result
    assert result["failure_details"]["preflight"]["code"] == "lark_cli_unavailable"
    assert result["phases"]["preflight"]["status"] == "failed"
    assert "state_persistence_error" in result["phases"]["preflight"]
    if deny_snapshot:
        assert result["failure_details"]["state_read"]["code"] == "state_read_failed"
    else:
        assert result["phases"]["preflight"]["persisted_status"] == "running"
    assert "private state" not in json.dumps(result)


@pytest.mark.parametrize("diagnostic", ["artifact", "receipt", "unexpected"])
def test_unreadable_old_diagnostics_preserve_structured_cli_failure(tmp_path, diagnostic):
    options = _options(tmp_path)
    paths = runner._phase_paths(options["job_root"])
    paths["processed_cut_plan"].parent.mkdir(parents=True)
    paths["processed_cut_plan"].write_text("{}", encoding="utf-8")
    target = "_phase_receipt_valid" if diagnostic == "receipt" else "_result_artifact"
    error = (
        RuntimeError("private diagnostic")
        if diagnostic == "unexpected"
        else PermissionError("private diagnostic")
    )
    with (
        runner_support.ReviewDocumentRunnerTests()._patched_runtime(),
        patch.object(runner, target, side_effect=error),
        pytest.raises(runner.ReviewDocumentRunError) as raised,
    ):
        runner.run_review_document(
            **options,
            lark_runner=lambda command: subprocess.CompletedProcess(command, 9, "", "private"),
        )
    result = raised.value.result
    assert result["failure_details"]["preflight"]["code"] == "lark_cli_unavailable"
    assert result["phases"]["preflight"]["status"] == "failed"
    assert "private diagnostic" not in json.dumps(result)
    assert "lark-cli" in result["error"]


@pytest.mark.parametrize("deny_result", [False, True])
def test_taskboard_blocked_receipt_preserves_original_preflight_error(tmp_path, deny_result):
    options = _options(tmp_path)
    options.pop("doc_url")
    manifest = tmp_path / "source-manifest.json"
    manifest_helper = manifest_support.ReviewDocumentRunnerSourcePairTests()
    manifest_payload = manifest_helper._manifest_payload()
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
    result_path = tmp_path / "driver-result.json"
    real_write = runner.atomic_write_json
    result_attempts = []

    def write_result(path, payload):
        if Path(path) == result_path:
            result_attempts.append(payload)
            if deny_result:
                raise PermissionError("private result path")
        return real_write(path, payload)

    with (
        runner_support.ReviewDocumentRunnerTests()._patched_runtime(),
        patch.dict(
            os.environ,
            manifest_helper._manifest_environment(
                manifest_support.canonical_sha256(manifest_payload)
            ),
        ),
        patch.object(runner, "atomic_write_json", side_effect=write_result),
        patch.object(
            runner,
            "invalidate_lark_readiness",
            side_effect=PermissionError("private readiness path"),
        ),
        pytest.raises(runner.ReviewDocumentRunError) as raised,
    ):
        runner.run_review_document(
            **options,
            source_manifest_json=manifest,
            result_path=result_path,
            lark_runner=lambda command: subprocess.CompletedProcess(command, 9, "", "private"),
        )
    assert len(result_attempts) == 1
    assert result_attempts[0]["status"] == "blocked"
    assert result_attempts[0]["error"]["code"] == "lark_cli_unavailable"
    if deny_result:
        assert (
            raised.value.result["failure_details"]["terminal_result"]["code"]
            == "terminal_result_write_failed"
        )
    else:
        receipt = json.loads(result_path.read_text(encoding="utf-8"))
        assert receipt == result_attempts[0]
    assert "private result path" not in json.dumps(raised.value.result)
