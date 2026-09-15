# ruff: noqa: E402
import errno
import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from utils import atomic_io


class AtomicStateWriteTests(unittest.TestCase):
    def test_windows_tempfile_permission_retry_chain_is_reproduced_with_a_safe_cap(self):
        # Bound the old standard-library loop in the test; never run its actual
        # Windows TMP_MAX (2,147,483,647 on the affected Python 3.11 installation).
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(tempfile, "TMP_MAX", 16),
                mock.patch.object(tempfile._os, "name", "nt"),
                mock.patch.object(tempfile._os.path, "isdir", return_value=True),
                mock.patch.object(tempfile._os, "access", return_value=True),
                mock.patch.object(
                    tempfile._os,
                    "open",
                    side_effect=PermissionError(errno.EACCES, "denied"),
                ) as opening,
                self.assertRaises(FileExistsError),
            ):
                tempfile.mkstemp(dir=directory)
            self.assertEqual(opening.call_count, 16)

    def test_permission_error_never_retries_or_changes_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_bytes(b'{"status":"previous"}')
            started = time.monotonic()
            with (
                mock.patch.object(
                    atomic_io.os,
                    "open",
                    side_effect=PermissionError(errno.EACCES, "denied"),
                ) as opening,
                self.assertRaises(PermissionError),
            ):
                atomic_io.atomic_write_bytes(target, b'{"status":"new"}')
            self.assertEqual(opening.call_count, 1)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(target.read_bytes(), b'{"status":"previous"}')
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_name_collisions_have_a_small_finite_retry_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            with (
                mock.patch.object(
                    atomic_io.os,
                    "open",
                    side_effect=FileExistsError(errno.EEXIST, "collision"),
                ) as opening,
                self.assertRaises(FileExistsError),
            ):
                atomic_io.atomic_write_bytes(target, b"new")
            self.assertEqual(opening.call_count, atomic_io.MAX_TEMPFILE_ATTEMPTS)
            self.assertFalse(target.exists())

    def test_fsync_and_replace_failures_preserve_previous_state_and_allow_retry(self):
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "state.json"
                target.write_bytes(b"previous")
                with (
                    mock.patch.object(
                        atomic_io.os, operation, side_effect=PermissionError("denied")
                    ),
                    self.assertRaises(PermissionError),
                ):
                    atomic_io.atomic_write_bytes(target, b"new")
                self.assertEqual(target.read_bytes(), b"previous")
                self.assertEqual(list(target.parent.iterdir()), [target])
                digest = atomic_io.atomic_write_bytes(target, b"new")
                self.assertEqual(target.read_bytes(), b"new")
                self.assertEqual(digest, hashlib.sha256(b"new").hexdigest())

    def test_cleanup_failure_cannot_mask_primary_write_error(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_bytes(b"previous")
            primary = OSError(errno.ENOSPC, "disk full")
            with (
                mock.patch.object(atomic_io.os, "fsync", side_effect=primary),
                mock.patch.object(Path, "unlink", side_effect=PermissionError("cleanup denied")),
                self.assertRaises(OSError) as raised,
            ):
                atomic_io.atomic_write_bytes(target, b"new")
            self.assertIs(raised.exception, primary)
            self.assertEqual(target.read_bytes(), b"previous")

    def test_atomic_replace_receives_complete_flushed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_bytes(b"previous")
            real_replace = os.replace
            observations = []

            def replace(source, destination):
                observations.append((Path(source).read_bytes(), Path(destination).read_bytes()))
                real_replace(source, destination)

            with mock.patch.object(atomic_io.os, "replace", side_effect=replace):
                atomic_io.atomic_write_bytes(target, b"complete new state")
            self.assertEqual(observations, [(b"complete new state", b"previous")])


if __name__ == "__main__":
    unittest.main()
