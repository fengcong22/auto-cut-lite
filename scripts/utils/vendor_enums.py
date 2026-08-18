from __future__ import annotations

from typing import Any

import pyJianYingDraft as draft


def get_vendor_enum(name: str) -> Any:
    enum_cls = getattr(draft, name, None)
    if enum_cls is not None:
        return enum_cls

    try:
        from pyJianYingDraft import metadata as draft_metadata
    except Exception:
        draft_metadata = None

    if draft_metadata is not None:
        enum_cls = getattr(draft_metadata, name, None)
        if enum_cls is not None:
            return enum_cls

    raise AttributeError(f"pyJianYingDraft enum not available: {name}")


def get_vendor_enum_optional(name: str) -> Any:
    try:
        return get_vendor_enum(name)
    except AttributeError:
        return None
