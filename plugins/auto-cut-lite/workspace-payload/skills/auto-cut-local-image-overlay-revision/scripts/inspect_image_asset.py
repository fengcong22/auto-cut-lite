from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np


def _detect_image_format(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    raise ValueError("image asset must contain valid PNG/JPG bytes")


def inspect_image_asset(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve(strict=True)
    raw = resolved.read_bytes()
    image_format = _detect_image_format(raw)
    encoded = np.frombuffer(raw, dtype=np.uint8)
    decoded = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if decoded is None:
        raise ValueError("image asset contains invalid PNG/JPG data")

    height, width = decoded.shape[:2]
    channels = 1 if decoded.ndim == 2 else int(decoded.shape[2])
    has_alpha = channels == 4

    if has_alpha:
        mask = (decoded[:, :, 3] > 0).astype(np.uint8)
        nonzero_alpha_pixels = int(cv2.countNonZero(mask))
        if nonzero_alpha_pixels == 0:
            raise ValueError("image asset has no visible pixels")
        x, y, visible_width, visible_height = cv2.boundingRect(mask)
    else:
        x, y = 0, 0
        visible_width, visible_height = width, height
        nonzero_alpha_pixels = width * height

    return {
        "path": os.fspath(resolved),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "format": image_format,
        "width": int(width),
        "height": int(height),
        "channels": channels,
        "has_alpha": has_alpha,
        "visible_bbox": {
            "x": int(x),
            "y": int(y),
            "width": int(visible_width),
            "height": int(visible_height),
        },
        "nonzero_alpha_pixels": nonzero_alpha_pixels,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect a local PNG/JPG and emit its hash, media contract, and visible bounds."
    )
    parser.add_argument("image", help="Path to one PNG/JPG image asset")
    args = parser.parse_args(argv)

    try:
        receipt = inspect_image_asset(args.image)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps({"ok": True, "receipt": receipt}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
