"""Atomic state writes with bounded exclusive temporary-file allocation.

Python 3.11's Windows ``tempfile.mkstemp`` can retry PermissionError up to
TMP_MAX times when access() says a restricted directory is writable.  State
and diagnostic writes must report that denial immediately instead.
"""

from __future__ import annotations

import errno
import hashlib
import os
import uuid
from pathlib import Path

MAX_TEMPFILE_ATTEMPTS = 8


def create_bounded_temporary_file(destination: Path) -> tuple[int, Path]:
    """Create a same-directory file; only genuine name collisions retry."""

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(MAX_TEMPFILE_ATTEMPTS):
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except FileExistsError:
            continue
        return descriptor, temporary
    raise FileExistsError(errno.EEXIST, "Atomic temporary-file allocation exhausted")


def atomic_write_bytes(destination: Path, content: bytes) -> str:
    """Flush a complete same-directory file before replacement; preserve failures.

    Cleanup is best effort so a second filesystem error cannot replace the
    original write/replace failure.  No permission or replacement retries run.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = create_bounded_temporary_file(destination)
    try:
        try:
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return hashlib.sha256(content).hexdigest()
