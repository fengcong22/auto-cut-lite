"""One disposable readiness state per test, shared by pytest and unittest.

Only the readiness environment override is changed. Production credentials,
readiness files and automatic-execution configuration are never modified.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch


@dataclass(frozen=True)
class ReadinessSandbox:
    root: Path
    path: Path


_ACTIVE: ContextVar[ReadinessSandbox | None] = ContextVar("test_readiness", default=None)
_INTAKE_MODULES = {"utils.review_document_intake", "scripts.utils.review_document_intake"}


class _ReadinessLoader(importlib.abc.Loader):
    def __init__(self, loader, guard):
        self.loader = loader
        self.guard = guard

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module):
        self.loader.exec_module(module)
        self.guard(module)


class _ReadinessFinder(importlib.abc.MetaPathFinder):
    """Guard a later runtime import without loading its dependencies eagerly."""

    def __init__(self, guard):
        self.guard = guard

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _INTAKE_MODULES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _ReadinessLoader(spec.loader, self.guard)
        return spec


@contextmanager
def isolated_readiness(*, test_roots: tuple[Path, ...] = ()):
    """Protect every readiness read/write, including explicit paths and failures.

    Nested fixture helpers reuse the test's state so resume tests can exercise
    persistence. A new test always receives a fresh, unverified state. Temporary
    directories created by legacy unittest tests are placed beneath this root.
    """
    active = _ACTIVE.get()
    if active is not None:
        with patch.dict(os.environ, {"AUTOCUT_LITE_READINESS_PATH": str(active.path)}):
            yield active
        return

    with ExitStack() as stack:
        root = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="autocut-test-"))
        ).resolve()
        allowed_roots = (root, *(path.resolve() for path in test_roots))
        sandbox = ReadinessSandbox(root=root, path=root / "runtime-readiness.json")

        def checked_path(path):
            resolved = Path(path).expanduser().resolve()
            if not any(resolved.is_relative_to(allowed) for allowed in allowed_roots):
                raise AssertionError("Test readiness access outside isolated test directory")
            return resolved

        guarded_modules = set()

        def guard(module):
            if id(module) in guarded_modules:
                return
            original_read = module._read_readiness
            original_write = module._write_readiness
            stack.enter_context(
                patch.object(
                    module,
                    "_read_readiness",
                    side_effect=lambda path: original_read(checked_path(path)),
                )
            )
            stack.enter_context(
                patch.object(
                    module,
                    "_write_readiness",
                    side_effect=lambda path, payload: original_write(checked_path(path), payload),
                )
            )
            guarded_modules.add(id(module))

        for name in _INTAKE_MODULES:
            if name in sys.modules:
                guard(sys.modules[name])
        finder = _ReadinessFinder(guard)
        sys.meta_path.insert(0, finder)
        stack.callback(sys.meta_path.remove, finder)
        stack.enter_context(
            patch.dict(os.environ, {"AUTOCUT_LITE_READINESS_PATH": str(sandbox.path)})
        )
        stack.enter_context(patch.object(tempfile, "tempdir", str(root)))
        sandbox.path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "lark": {"status": "pending_validation"},
                    "asr": {"status": "pending_validation"},
                }
            ),
            encoding="utf-8",
        )
        token = _ACTIVE.set(sandbox)
        stack.callback(_ACTIVE.reset, token)
        yield sandbox


class IsolatedReadinessTestCase(unittest.TestCase):
    """Also protect direct ``python -m unittest`` invocations (no conftest)."""

    def setUp(self):
        super().setUp()
        scope = isolated_readiness()
        self.readiness_sandbox = scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
