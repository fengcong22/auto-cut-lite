import math
import re
from collections.abc import Mapping
from numbers import Real
from typing import Any

_DYNAMIC_REVIEW_MARKER_TRACK = re.compile(r"Review Marker [1-9]\d*")
_TOP_BAND_MIN_Y = 0.7
_TOP_BAND_MAX_Y = 0.9
_Y_MISSING_CODE = "review_marker_top_layout.y_missing_or_nonfinite"
_Y_OUT_OF_BAND_CODE = "review_marker_top_layout.y_out_of_band"


def review_marker_top_layout_problems(
    content: Any,
    *,
    min_y: float = 0.7,
    max_y: float = 0.9,
) -> list[str]:
    """Return fixed-top-band problems for one saved draft-content object.

    ``min_y`` and ``max_y`` remain accepted for call compatibility but cannot
    override the repository's fixed ``0.7..0.9`` acceptance band.
    """
    if not isinstance(content, Mapping):
        return []

    problems: list[str] = []
    tracks = content.get("tracks")
    if not isinstance(tracks, list):
        return problems

    for track in tracks:
        if not isinstance(track, Mapping):
            continue
        track_name = track.get("name")
        if not isinstance(track_name, str):
            continue
        if _DYNAMIC_REVIEW_MARKER_TRACK.fullmatch(track_name) is None:
            continue

        segments = track.get("segments")
        if not isinstance(segments, list):
            continue
        for index, segment in enumerate(segments):
            segment_id = f"index {index}"
            y = None
            if isinstance(segment, Mapping):
                segment_id = str(segment.get("id") or segment_id)
                clip = segment.get("clip")
                if isinstance(clip, Mapping):
                    transform = clip.get("transform")
                    if isinstance(transform, Mapping):
                        y = transform.get("y")

            if not isinstance(y, Real) or isinstance(y, bool) or not math.isfinite(float(y)):
                problems.append(
                    f"{_Y_MISSING_CODE}: track={track_name!r}; "
                    f"segment={segment_id!r}; expected finite clip.transform.y "
                    "in fixed band 0.7..0.9."
                )
                continue

            numeric_y = float(y)
            if numeric_y < _TOP_BAND_MIN_Y or numeric_y > _TOP_BAND_MAX_Y:
                problems.append(
                    f"{_Y_OUT_OF_BAND_CODE}: track={track_name!r}; "
                    f"segment={segment_id!r}; clip.transform.y={numeric_y:g}; "
                    "expected fixed band 0.7..0.9."
                )

    return problems


__all__ = ["review_marker_top_layout_problems"]
