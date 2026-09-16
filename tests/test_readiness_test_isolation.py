"""Development fixtures must never read or write the operator's readiness state."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from utils import review_document_intake as intake

from tests import test_review_document_runner as runner_support
from tests.readiness_support import IsolatedReadinessTestCase, isolated_readiness


def test_runner_helper_explicitly_forwards_disposable_readiness(tmp_path):
    with patch.object(runner_support.runner, "run_review_document", return_value={}) as run:
        runner_support.ReviewDocumentRunnerTests._run(
            tmp_path / "snapshot.json",
            tmp_path / "project.json",
            job_root=tmp_path / "job",
            drafts_root=tmp_path / "drafts",
            package_zip=tmp_path / "delivery.zip",
            cache_root=tmp_path / "cache",
        )
    readiness_path = run.call_args.kwargs.get("readiness_path")
    assert readiness_path is not None, "Fixture must not use production readiness defaults"
    assert Path(readiness_path).is_absolute()


def test_patched_runtime_overrides_inherited_operator_readiness(tmp_path):
    operator_path = tmp_path / "operator" / "runtime-readiness.json"
    with patch.dict(os.environ, {"AUTOCUT_LITE_READINESS_PATH": str(operator_path)}):
        with runner_support.ReviewDocumentRunnerTests()._patched_runtime():
            actual = Path(os.environ["AUTOCUT_LITE_READINESS_PATH"])
            assert actual != operator_path, "Mock ASR must not inherit operator readiness"
    assert not operator_path.exists()


@pytest.mark.parametrize("operation", ["read", "write", "evaluate", "lark", "invalidate", "asr"])
def test_readiness_access_outside_test_directory_is_blocked(tmp_path, operation):
    outside = tmp_path.parent / f"forbidden-readiness-{operation}.json"
    assert not outside.exists()
    versions = {
        "runtime_version": "test-runtime",
        "lark_version": "test-lark",
        "asr_adapter_version": "test-adapter-v1",
    }
    operations = {
        "read": lambda: intake._read_readiness(outside),
        "write": lambda: intake._write_readiness(outside, {}),
        "evaluate": lambda: intake.evaluate_runtime_readiness(path=outside, **versions),
        "lark": lambda: intake.mark_lark_verified({}, path=outside, **versions),
        "invalidate": lambda: intake.invalidate_lark_readiness("test", path=outside),
        "asr": lambda: intake.mark_asr_verified(
            provider="test",
            model_or_resource="test",
            adapter_version="test-adapter-v1",
            path=outside,
        ),
    }
    with pytest.raises(AssertionError, match="outside isolated test directory"):
        operations[operation]()
    assert not outside.exists()


def test_readiness_environment_escape_is_blocked(tmp_path):
    outside = tmp_path.parent / "forbidden-environment-readiness.json"
    with patch.dict(os.environ, {"AUTOCUT_LITE_READINESS_PATH": str(outside)}):
        with pytest.raises(AssertionError, match="outside isolated test directory"):
            intake.invalidate_lark_readiness("test")
    assert not outside.exists()


def test_missing_override_cannot_fall_back_to_operator_state(tmp_path):
    with patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_path.parent)}):
        os.environ.pop("AUTOCUT_LITE_READINESS_PATH", None)
        with pytest.raises(AssertionError, match="outside isolated test directory"):
            intake.invalidate_lark_readiness("must-not-use-default")


def test_existing_outside_state_remains_byte_identical(tmp_path):
    operator_path = tmp_path.parent / f"operator-{tmp_path.name}.json"
    original = b'{"fixture":"read-only sentinel, never production content"}'
    operator_path.write_bytes(original)
    try:
        with pytest.raises(AssertionError, match="outside isolated test directory"):
            intake._write_readiness(operator_path, {"mutated": True})
        assert operator_path.read_bytes() == original
    finally:
        operator_path.unlink()


@pytest.mark.parametrize("attempt", range(2))
def test_each_test_starts_with_independent_pending_state(disposable_runtime_readiness, attempt):
    path = disposable_runtime_readiness.path
    initial = json.loads(path.read_text(encoding="utf-8"))
    assert initial == {
        "schema_version": intake.READINESS_SCHEMA_VERSION,
        "lark": {"status": "pending_validation"},
        "asr": {"status": "pending_validation"},
    }
    intake.invalidate_lark_readiness(f"modified-by-test-{attempt}")


def test_nested_fixture_reuses_only_this_tests_state(disposable_runtime_readiness):
    intake.invalidate_lark_readiness("nested-test-state")
    with isolated_readiness() as nested:
        assert nested == disposable_runtime_readiness
        assert intake._read_readiness(nested.path)["lark"]["reason_code"] == "nested-test-state"


def test_unittest_base_provides_the_same_guard(disposable_runtime_readiness):
    seen = []

    class Probe(IsolatedReadinessTestCase):
        def runTest(self):
            seen.append(self.readiness_sandbox.path)
            intake.invalidate_lark_readiness("unittest-fixture")

    result = unittest.TestResult()
    Probe().run(result)
    assert result.wasSuccessful(), result.errors
    assert seen == [disposable_runtime_readiness.path]


def test_direct_unittest_has_fresh_state_and_preserves_inherited_file(tmp_path):
    sentinel = tmp_path / "operator-state.json"
    sentinel.write_bytes(b'{"operator-state":"must remain byte-identical"}')
    before = sentinel.read_bytes()
    script = """
import json, os, sys, unittest
from pathlib import Path
from tests.readiness_support import IsolatedReadinessTestCase
sys.path.insert(0, str(Path.cwd() / 'scripts'))
from utils import review_document_intake as intake

operator_path = Path(os.environ['AUTOCUT_LITE_READINESS_PATH'])
seen = []

class Probe(IsolatedReadinessTestCase):
    def runTest(self):
        state = self.readiness_sandbox
        assert state.path != operator_path
        assert state.path not in seen
        seen.append(state.path)
        payload = intake._read_readiness(state.path)
        assert payload['lark'] == {'status': 'pending_validation'}
        assert payload['asr'] == {'status': 'pending_validation'}
        intake.invalidate_lark_readiness('changed-in-this-test')
        with self.assertRaises(AssertionError):
            intake.invalidate_lark_readiness('must-not-write', path=operator_path)

result = unittest.TestResult()
unittest.TestSuite([Probe(), Probe()]).run(result)
assert result.wasSuccessful(), result.errors
assert Path(os.environ['AUTOCUT_LITE_READINESS_PATH']) == operator_path
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "AUTOCUT_LITE_READINESS_PATH": str(sentinel)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert sentinel.read_bytes() == before


def test_global_fixture_does_not_import_main_runtime_dependencies():
    script = """
import importlib.abc, runpy, sys
class WithoutPsutil(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'psutil':
            raise ModuleNotFoundError('isolated audio runtime has no psutil', name='psutil')
sys.meta_path.insert(0, WithoutPsutil())
runpy.run_path('tests/conftest.py')
from tests.readiness_support import isolated_readiness
with isolated_readiness() as state:
    assert state.path.is_file()
assert 'utils.review_document_intake' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_intake_test_file_can_be_invoked_directly():
    completed = subprocess.run(
        [sys.executable, "tests/test_review_document_intake.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_late_runtime_import_is_guarded_and_restored():
    script = """
import sys
from pathlib import Path
from tests.readiness_support import isolated_readiness
sys.path.insert(0, str(Path.cwd() / 'scripts'))
assert 'utils.review_document_intake' not in sys.modules
with isolated_readiness() as state:
    from utils import review_document_intake as intake
    try:
        intake.invalidate_lark_readiness('forbidden', path=state.root.parent / 'never-created.json')
    except AssertionError:
        pass
    else:
        raise AssertionError('late import bypassed readiness guard')
    intake.invalidate_lark_readiness('allowed')
assert intake._read_readiness.__module__ == 'utils.review_document_intake'
assert intake._write_readiness.__module__ == 'utils.review_document_intake'
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
