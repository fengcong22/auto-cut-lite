"""Fail closed if a development test attempts to use real readiness state."""

import pytest

from tests.readiness_support import isolated_readiness


@pytest.fixture(autouse=True)
def disposable_runtime_readiness(tmp_path):
    with isolated_readiness(test_roots=(tmp_path,)) as sandbox:
        yield sandbox
