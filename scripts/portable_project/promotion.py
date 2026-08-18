from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from .errors import PortableProjectError


def withdraw_promoted_directory(
    promoted_dir: Path,
    *,
    quarantine_prefix: str,
    failure_code: str,
) -> None:
    promoted = Path(promoted_dir)
    if not promoted.exists():
        return
    quarantine = promoted.parent / f".{quarantine_prefix}.autocut-rejected-{uuid.uuid4().hex}"
    try:
        os.replace(promoted, quarantine)
    except OSError:
        shutil.rmtree(promoted, ignore_errors=True)
    else:
        shutil.rmtree(quarantine, ignore_errors=True)
    if promoted.exists() or quarantine.exists():
        raise PortableProjectError(
            failure_code, "A changed promoted directory could not be withdrawn safely"
        )
