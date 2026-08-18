"""ASR-safe placement for semantic still-frame pauses.

Pause requests are expressed in source-media time.  A raw timestamp can land
inside a spoken word, especially when it came from a rough review timestamp.
This module resolves the request to the interior of a meaningful audio gap
before the timeline writer inserts any still-frame hold.  ASR boundaries are
not acoustically exact, so placing a hold directly on a reported word onset or
tail can still clip a protected syllable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


class PauseAlignmentError(ValueError):
    """Raised when a pause cannot be placed at an ASR-safe boundary."""


@dataclass(frozen=True)
class PauseBoundary:
    requested_time: float
    resolved_time: float
    previous_word_end: float | None
    next_word_start: float | None
    gap_duration: float
    previous_guard_seconds: float
    next_guard_seconds: float
    minimum_edge_guard_seconds: float
    placement: str
    snapped: bool
    reason: str
    previous_utterance_text: str = ""
    next_utterance_text: str = ""
    previous_utterance_end: float | None = None
    next_utterance_start: float | None = None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _read_time(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def extract_word_boundaries(payload: Any) -> list[dict[str, Any]]:
    """Normalize common ASR JSON shapes into sorted word boundaries.

    The resolver intentionally accepts only second-based finite timings.  The
    maintained ASR artifacts use seconds; silently guessing milliseconds here
    would make a bad pause placement harder to diagnose.
    """

    rows: Any = payload
    if isinstance(payload, Mapping):
        rows = payload.get("words")
        if rows is None:
            rows = payload.get("word_segments")
        if rows is None:
            rows = payload.get("result")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise PauseAlignmentError("ASR alignment does not contain a words list.")

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        start = _read_time(row, "start", "start_time", "begin_time", "begin")
        end = _read_time(row, "end", "end_time", "finish_time", "finish")
        if start is None or end is None or end <= start:
            continue
        normalized.append(
            {
                "text": str(row.get("text") or row.get("word") or row.get("value") or ""),
                "start": start,
                "end": end,
            }
        )
    normalized.sort(key=lambda row: (row["start"], row["end"]))
    if not normalized:
        raise PauseAlignmentError("ASR alignment contains no usable word timings.")
    return normalized


def extract_utterance_boundaries(payload: Any) -> list[dict[str, Any]]:
    """Return sentence/utterance ranges when the ASR provider supplies them."""

    rows = payload.get("utterances") if isinstance(payload, Mapping) else None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        start = _read_time(row, "start", "start_time", "begin_time", "begin")
        end = _read_time(row, "end", "end_time", "finish_time", "finish")
        if start is None or end is None or end <= start:
            continue
        normalized.append(
            {
                "text": str(row.get("text") or ""),
                "start": start,
                "end": end,
            }
        )
    normalized.sort(key=lambda row: (row["start"], row["end"]))
    return normalized


def protected_utterance_anchor(text: str, *, leading: bool) -> str:
    characters = [character for character in str(text or "") if character.isalnum()]
    if not characters:
        return ""
    anchor = characters[:2] if leading else characters[-2:]
    return "".join(anchor)


def _boundary_result(
    requested: float,
    resolved: float,
    previous_end: float | None,
    next_start: float | None,
    *,
    minimum_edge_guard_seconds: float,
    snapped: bool,
    reason: str,
    previous_utterance_text: str = "",
    next_utterance_text: str = "",
    previous_utterance_end: float | None = None,
    next_utterance_start: float | None = None,
) -> PauseBoundary:
    gap = 0.0
    previous_guard = 0.0
    next_guard = 0.0
    if previous_end is not None and next_start is not None:
        gap = max(0.0, next_start - previous_end)
        previous_guard = max(0.0, resolved - previous_end)
        next_guard = max(0.0, next_start - resolved)
    return PauseBoundary(
        requested_time=requested,
        resolved_time=resolved,
        previous_word_end=previous_end,
        next_word_start=next_start,
        gap_duration=gap,
        previous_guard_seconds=previous_guard,
        next_guard_seconds=next_guard,
        minimum_edge_guard_seconds=minimum_edge_guard_seconds,
        placement="gap_midpoint",
        snapped=snapped,
        reason=reason,
        previous_utterance_text=previous_utterance_text,
        next_utterance_text=next_utterance_text,
        previous_utterance_end=previous_utterance_end,
        next_utterance_start=next_utterance_start,
    )


def resolve_pause_boundary(
    requested_time: float,
    words: Iterable[Mapping[str, Any]] | Any,
    *,
    min_gap_seconds: float = 0.35,
    search_window_seconds: float = 2.0,
    tolerance_seconds: float = 0.005,
    semantic_gap_seconds: float = 0.8,
    edge_guard_seconds: float = 0.05,
) -> PauseBoundary:
    """Resolve a requested pause time to the midpoint of a safe ASR gap.

    Midpoint placement maximizes the distance from both ASR edges.  This is
    deliberate: reported onsets and tails can differ from the acoustic signal
    by tens of milliseconds. Tiny inter-character gaps are ignored. A request
    that cannot reach a meaningful gap fails closed instead of inserting
    silence into speech.
    """

    requested = _number(requested_time)
    if requested is None or requested < 0:
        raise PauseAlignmentError("pause requested_time must be a finite non-negative number.")
    minimum_gap = _number(min_gap_seconds)
    window = _number(search_window_seconds)
    tolerance = _number(tolerance_seconds)
    edge_guard = _number(edge_guard_seconds)
    if minimum_gap is None or minimum_gap <= 0:
        raise PauseAlignmentError("min_gap_seconds must be positive.")
    if window is None or window <= 0:
        raise PauseAlignmentError("search_window_seconds must be positive.")
    if tolerance is None or tolerance < 0:
        raise PauseAlignmentError("tolerance_seconds must be non-negative.")
    if edge_guard is None or edge_guard <= 0:
        raise PauseAlignmentError("edge_guard_seconds must be positive.")

    normalized = extract_word_boundaries(words)

    # Utterance boundaries are stronger evidence than isolated word gaps.  A
    # rough review timestamp can land several words into the next sentence;
    # choosing the midpoint of a long utterance gap puts the hold between
    # sentences and leaves symmetric protection for both sentence edges.
    utterances = extract_utterance_boundaries(words)
    semantic_gap = _number(semantic_gap_seconds)
    if semantic_gap is None or semantic_gap <= 0:
        raise PauseAlignmentError("semantic_gap_seconds must be positive.")
    semantic_candidates: list[tuple[float, float, float, float, float, float, str, str]] = []
    unsafe_semantic_midpoint = False
    insufficient_word_edge_guard = False
    for index in range(len(utterances) - 1):
        previous_end = float(utterances[index]["end"])
        next_start = float(utterances[index + 1]["start"])
        gap = next_start - previous_end
        if gap < max(semantic_gap, 2.0 * edge_guard) - tolerance:
            continue
        if (
            requested < previous_end - window - tolerance
            or requested > next_start + window + tolerance
        ):
            continue
        boundary = (previous_end + next_start) / 2.0
        if abs(requested - boundary) > window + tolerance:
            continue
        if any(
            float(row["start"]) - tolerance <= boundary <= float(row["end"]) + tolerance
            for row in normalized
        ):
            unsafe_semantic_midpoint = True
            continue
        previous_word_ends = [
            float(row["end"]) for row in normalized if float(row["end"]) <= boundary + tolerance
        ]
        next_word_starts = [
            float(row["start"]) for row in normalized if float(row["start"]) >= boundary - tolerance
        ]
        if not previous_word_ends or not next_word_starts:
            insufficient_word_edge_guard = True
            continue
        previous_word_end = max(previous_word_ends)
        next_word_start = min(next_word_starts)
        if (
            boundary - previous_word_end < edge_guard - tolerance
            or next_word_start - boundary < edge_guard - tolerance
        ):
            insufficient_word_edge_guard = True
            continue
        semantic_candidates.append(
            (
                abs(requested - boundary),
                boundary,
                previous_word_end,
                next_word_start,
                previous_end,
                next_start,
                str(utterances[index].get("text") or ""),
                str(utterances[index + 1].get("text") or ""),
            )
        )
    if semantic_candidates:
        (
            _distance,
            boundary,
            previous_word_end,
            next_word_start,
            previous_utterance_end,
            next_utterance_start,
            previous_text,
            next_text,
        ) = min(
            semantic_candidates,
            key=lambda row: (row[0], -((row[5] - row[4])), row[1]),
        )
        return _boundary_result(
            requested,
            boundary,
            previous_word_end,
            next_word_start,
            minimum_edge_guard_seconds=edge_guard,
            snapped=abs(requested - boundary) > 1e-9,
            reason="nearest_utterance_gap_midpoint",
            previous_utterance_text=previous_text,
            next_utterance_text=next_text,
            previous_utterance_end=previous_utterance_end,
            next_utterance_start=next_utterance_start,
        )
    if unsafe_semantic_midpoint:
        raise PauseAlignmentError(
            "pause utterance-gap midpoint overlaps a spoken word in the bound ASR."
        )
    if insufficient_word_edge_guard:
        raise PauseAlignmentError(
            "pause utterance-gap midpoint does not preserve the configured word-edge guard."
        )

    containing = any(
        float(row["start"]) + tolerance < requested < float(row["end"]) - tolerance
        for row in normalized
    )

    # Find the strongest meaningful gap at or after the rough timestamp. This
    # intentionally skips tiny gaps between adjacent Chinese characters. When
    # utterance boundaries are unavailable, the largest nearby gap is the
    # least-bad semantic fallback; callers can lower the threshold explicitly
    # for a deliberately short same-clause pause.
    latest = requested + window
    word_candidates: list[tuple[float, float, float, float]] = []
    for index in range(len(normalized) - 1):
        previous_end = float(normalized[index]["end"])
        next_start = float(normalized[index + 1]["start"])
        gap = next_start - previous_end
        if gap < max(minimum_gap, 2.0 * edge_guard) - tolerance:
            continue
        if next_start < requested - tolerance:
            continue
        if previous_end > latest + tolerance:
            break
        boundary = (previous_end + next_start) / 2.0
        if abs(requested - boundary) > window + tolerance:
            continue
        word_candidates.append((gap, abs(requested - previous_end), previous_end, next_start))
    if word_candidates:
        gap, _distance, previous_end, next_start = max(
            word_candidates,
            key=lambda row: (row[0], -row[1], -row[2]),
        )
        boundary = (previous_end + next_start) / 2.0
        return _boundary_result(
            requested,
            boundary,
            previous_end,
            next_start,
            minimum_edge_guard_seconds=edge_guard,
            snapped=abs(requested - boundary) > 1e-9,
            reason=(
                "next_meaningful_asr_gap_midpoint" if containing else "meaningful_asr_gap_midpoint"
            ),
        )

    if containing:
        raise PauseAlignmentError(
            f"pause at {requested:.3f}s falls inside a spoken word and no safe ASR gap "
            f"was found within {window:.3f}s."
        )
    raise PauseAlignmentError(f"pause at {requested:.3f}s is not aligned to a safe ASR gap.")


__all__ = [
    "PauseAlignmentError",
    "PauseBoundary",
    "extract_word_boundaries",
    "extract_utterance_boundaries",
    "protected_utterance_anchor",
    "resolve_pause_boundary",
]
