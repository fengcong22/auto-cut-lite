import math
import re
from dataclasses import dataclass, replace
from typing import Iterable, List, Optional, Sequence, Union

import pyJianYingDraft as draft
from utils.formatters import safe_tim

_DYNAMIC_REVIEW_MARKER_TRACK = re.compile(r"Review Marker [1-9]\d*")
_REVIEW_MARKER_TOP_MIN_Y = 0.7
_REVIEW_MARKER_TOP_Y = 0.8
_REVIEW_MARKER_TOP_MAX_Y = 0.9


def _is_dynamic_review_marker_track(name: object) -> bool:
    return _DYNAMIC_REVIEW_MARKER_TRACK.fullmatch(str(name)) is not None


def _review_marker_track_group_is_mixed(names: Sequence[str]) -> bool:
    kinds = {_is_dynamic_review_marker_track(name) for name in names}
    return len(kinds) > 1


@dataclass
class ReviewMarkerItem:
    label: str
    start_time: Union[str, int, float]
    duration: Union[str, int, float] = "1.2s"
    detail: str = ""
    lane: Optional[int] = None
    item_id: str = ""
    source_text: str = ""
    verbatim_status: str = "legacy"
    segment_id: str = ""
    material_id: str = ""
    track_name: str = ""
    kind: str = "review_only"
    background_color: str = ""


@dataclass(frozen=True)
class ReviewMarkerLayoutCell:
    x: float
    y: float
    cell_width: float
    cell_height: float


@dataclass(frozen=True)
class ReviewMarkerTextLayout:
    font_size: float
    background_width: float
    background_height: float
    max_line_width: float
    estimated_line_count: int
    estimated_text_height: float


def _sec_label(value: float) -> str:
    total = max(0, int(round(value)))
    minutes = total // 60
    seconds = total % 60
    return f"{minutes:02d}:{seconds:02d}"


def _to_seconds(value: Union[str, int, float]) -> float:
    return safe_tim(value) / 1_000_000.0


class ReviewMarkerOpsMixin:
    """
    Reusable review-marker track helpers.

    Default behavior:
    - create ASCII `Review Marker 1/2/3` text tracks
    - place concurrent labels across one horizontal top safe band
    - write a human-readable markdown manifest alongside the draft
    """

    REVIEW_MARKER_TRACKS = (
        "Review Marker 1",
        "Review Marker 2",
        "Review Marker 3",
        "Review Marker 4",
        "Review Marker 5",
        "Review Marker 6",
    )
    REVIEW_MARKER_X = (-0.72, -0.43, -0.14, 0.14, 0.43, 0.72)
    REVIEW_MARKER_MIN_FONT_SIZE = 0.001
    REVIEW_MARKER_MAX_FONT_SIZE = 3.2
    REVIEW_MARKER_CANVAS_BOUND = 0.9
    REVIEW_MARKER_CELL_SAFETY = 0.88
    REVIEW_MARKER_CHAR_WIDTH_FACTOR = 0.006
    REVIEW_MARKER_LINE_HEIGHT_FACTOR = 0.025
    # Lite mode intentionally uses a separate, grouped layout.  Keep these
    # values out of the default dynamic layout so the full workflow retains
    # its established marker sizing and one-row lane behavior.
    LITE_GROUPED_MARKER_MIN_FONT_SIZE = 4.0
    LITE_GROUPED_MARKER_MAX_FONT_SIZE = 5.0
    LITE_GROUPED_MARKER_BACKGROUND_COLOR = "#15803D"
    LITE_GROUPED_MARKER_GROUPS = (
        ("delete", "Review Marker Delete"),
        ("visual", "Review Marker Visual"),
        ("animation", "Review Marker Animation"),
    )
    LITE_GROUPED_DELETE_KINDS = frozenset(
        {
            "spoken_delete",
            "phrase_delete",
            "ellipsis_range_delete",
            "colored_span_delete",
            "range_delete",
            "gap_delete",
            "pause_delete",
            "visual_delete",
            "noise_cleanup",
            "review_only",
        }
    )
    LITE_GROUPED_VISUAL_KINDS = frozenset(
        {"pointer_overlay", "visual_overlay", "visual_addition", "other_visual"}
    )
    REVIEW_MARKER_BACKGROUND_COLORS = {
        "spoken_delete": "#B42318",
        # Keep each spoken-delete interpretation visually distinct in the
        # timeline.  These are intentionally separate from the generic
        # spoken-delete red so a reviewer can tell whether a row came from an
        # explicit delete note, an ellipsis range, a blue rich-text span, or
        # a pause/gap instruction without opening the source ledger.
        "phrase_delete": "#B42318",
        "range_delete": "#991B1B",
        "ellipsis_range_delete": "#7F1D1D",
        "colored_span_delete": "#6D28D9",
        "gap_delete": "#A16207",
        "pause_delete": "#C2410C",
        "visual_delete": "#9F1239",
        "pointer_overlay": "#1D4ED8",
        "visual_overlay": "#2563EB",
        "animation_timing": "#B45309",
        "review_only": "#4B5563",
    }

    def add_review_markers(
        self,
        markers: Sequence[ReviewMarkerItem],
        *,
        track_names: Optional[Sequence[str]] = None,
        x_positions: Optional[Sequence[float]] = None,
        layout_mode: str = "dynamic",
    ) -> List[ReviewMarkerItem]:
        if not markers:
            return []

        if str(layout_mode or "dynamic").strip().casefold() == "lite_grouped":
            return self._add_lite_grouped_review_markers(markers)
        if str(layout_mode or "dynamic").strip().casefold() != "dynamic":
            raise ValueError(f"Unsupported review marker layout mode: {layout_mode!r}.")

        configured_track_names = tuple(track_names) if track_names is not None else None
        x_positions = tuple(x_positions) if x_positions is not None else None
        configured_default_names = tuple(str(name) for name in self.REVIEW_MARKER_TRACKS)
        active_name_group = (
            configured_track_names
            if configured_track_names is not None
            else configured_default_names
        )
        if _review_marker_track_group_is_mixed(active_name_group):
            raise ValueError("track_names cannot mix dynamic and custom review marker tracks.")
        default_names_are_custom = bool(configured_default_names) and all(
            not _is_dynamic_review_marker_track(name) for name in configured_default_names
        )
        if configured_track_names is None and x_positions is not None and default_names_are_custom:
            configured_track_names = tuple(self._default_track_names(len(x_positions)))
        if configured_track_names is not None and len(set(configured_track_names)) != len(
            configured_track_names
        ):
            raise ValueError("track_names must be unique.")

        custom_layout = bool(configured_track_names) and all(
            not _is_dynamic_review_marker_track(name) for name in configured_track_names
        )
        dynamic_layout = (
            not custom_layout
            if configured_track_names is not None
            else not default_names_are_custom
        )
        if custom_layout:
            track_names = configured_track_names
            if x_positions is None:
                x_positions = self._default_x_positions(len(track_names))
            if len(track_names) != len(x_positions):
                raise ValueError("track_names and x_positions must have the same length.")
            assigned = self._assign_review_marker_lanes(
                markers,
                lane_count=len(track_names),
            )
        else:
            markers_for_assignment = (
                [replace(marker, lane=None) for marker in markers] if dynamic_layout else markers
            )
            assigned = self._assign_review_marker_lanes(
                markers_for_assignment,
                lane_count=(
                    len(configured_track_names) if configured_track_names is not None else None
                ),
            )
            lane_count = max((item.lane or 0) for item in assigned) + 1
            track_names = (
                configured_track_names[:lane_count]
                if configured_track_names is not None
                else self._default_track_names(lane_count)
            )

        layout_cells = self._default_marker_layout(len(track_names))
        if not custom_layout:
            x_positions = tuple(cell.x for cell in layout_cells)
        for track_name in track_names:
            self._ensure_track(draft.TrackType.text, track_name)

        receipts: List[ReviewMarkerItem] = []
        for marker in assigned:
            lane = marker.lane or 0
            cell = layout_cells[lane]
            text_layout = self._marker_text_layout(
                marker.label,
                cell_width=cell.cell_width,
                cell_height=cell.cell_height,
            )
            segment = self.add_text_simple(
                marker.label,
                start_time=marker.start_time,
                duration=marker.duration,
                track_name=track_names[lane],
                clip_settings=draft.ClipSettings(
                    transform_x=x_positions[lane],
                    transform_y=cell.y,
                ),
                style=draft.TextStyle(
                    size=text_layout.font_size,
                    bold=True,
                    color=(1.0, 1.0, 1.0),
                    align=1,
                    auto_wrapping=True,
                    max_line_width=text_layout.max_line_width,
                ),
                border=draft.TextBorder(
                    color=(0.0, 0.0, 0.0),
                    alpha=1.0,
                    width=16.0,
                ),
                background=draft.TextBackground(
                    color=(
                        marker.background_color
                        or self.REVIEW_MARKER_BACKGROUND_COLORS.get(
                            str(marker.kind or "review_only"),
                            self.REVIEW_MARKER_BACKGROUND_COLORS["review_only"],
                        )
                    ),
                    style=1,
                    alpha=0.92,
                    round_radius=0.16,
                    height=text_layout.background_height,
                    width=text_layout.background_width,
                ),
            )
            receipts.append(
                replace(
                    marker,
                    segment_id=segment.segment_id,
                    material_id=segment.material_id,
                    track_name=track_names[lane],
                )
            )
        return receipts

    def _lite_group_for_marker(self, marker: ReviewMarkerItem) -> str:
        kind = str(marker.kind or "review_only").strip().casefold()
        if kind == "animation_timing":
            return "animation"
        if kind in self.LITE_GROUPED_VISUAL_KINDS:
            return "visual"
        # Keep unknown/review-only source items visible in the stable three
        # groups rather than dropping them or creating an unclassified lane.
        return "delete"

    def _lite_grouped_marker_layout(self) -> ReviewMarkerLayoutCell:
        # One label owns the complete safe width.  Its left edge is -0.882 at
        # most, leaving a small stage margin even after JianYing rounds values.
        return ReviewMarkerLayoutCell(
            x=0.0,
            y=_REVIEW_MARKER_TOP_Y,
            cell_width=self.REVIEW_MARKER_CANVAS_BOUND * 2.0,
            cell_height=_REVIEW_MARKER_TOP_MAX_Y - _REVIEW_MARKER_TOP_MIN_Y,
        )

    def _add_lite_grouped_review_markers(
        self,
        markers: Sequence[ReviewMarkerItem],
    ) -> List[ReviewMarkerItem]:
        """Render lite markers in Delete/Visual/Animation track families.

        This opt-in layout deliberately does not change the full-workflow
        dynamic marker behavior.  Lanes are allocated independently inside
        each family, so an overlap adds ``Review Marker <Family> 2`` instead
        of mixing an unrelated kind into the same track.
        """
        layout_cell = self._lite_grouped_marker_layout()
        grouped: dict[str, list[ReviewMarkerItem]] = {
            group: [] for group, _prefix in self.LITE_GROUPED_MARKER_GROUPS
        }
        for marker in markers:
            grouped[self._lite_group_for_marker(marker)].append(
                replace(marker, lane=None)
            )

        assigned_by_group: dict[str, list[ReviewMarkerItem]] = {}
        track_names_by_group: dict[str, tuple[str, ...]] = {}
        for group, prefix in self.LITE_GROUPED_MARKER_GROUPS:
            group_markers = grouped[group]
            if not group_markers:
                continue
            assigned = self._assign_review_marker_lanes(group_markers, lane_count=None)
            assigned_by_group[group] = assigned
            lane_count = max((item.lane or 0) for item in assigned) + 1
            names = tuple(f"{prefix} {index}" for index in range(1, lane_count + 1))
            track_names_by_group[group] = names
            for track_name in names:
                self._ensure_track(draft.TrackType.text, track_name)

        receipts: List[ReviewMarkerItem] = []
        # Preserve the canonical source-plan order in receipts while rendering
        # each family on its own track set.
        rendered: dict[str, ReviewMarkerItem] = {}
        for group, _prefix in self.LITE_GROUPED_MARKER_GROUPS:
            for marker in assigned_by_group.get(group, []):
                lane = marker.lane or 0
                text_layout = self._marker_text_layout(
                    marker.label,
                    cell_width=layout_cell.cell_width,
                    cell_height=layout_cell.cell_height,
                    minimum_font_size=self.LITE_GROUPED_MARKER_MIN_FONT_SIZE,
                    maximum_font_size=self.LITE_GROUPED_MARKER_MAX_FONT_SIZE,
                    cell_safety=0.98,
                    line_height_factor=0.02,
                )
                background_color = marker.background_color
                if group == "visual":
                    # Visual/pointer labels are intentionally green in lite
                    # mode, even when an upstream marker carried a legacy
                    # blue/other override.
                    background_color = self.LITE_GROUPED_MARKER_BACKGROUND_COLOR
                segment = self.add_text_simple(
                    marker.label,
                    start_time=marker.start_time,
                    duration=marker.duration,
                    track_name=track_names_by_group[group][lane],
                    clip_settings=draft.ClipSettings(
                        transform_x=layout_cell.x,
                        transform_y=layout_cell.y,
                    ),
                    style=draft.TextStyle(
                        size=text_layout.font_size,
                        bold=True,
                        color=(1.0, 1.0, 1.0),
                        align=0,
                        auto_wrapping=True,
                        max_line_width=text_layout.max_line_width,
                    ),
                    border=draft.TextBorder(
                        color=(0.0, 0.0, 0.0),
                        alpha=1.0,
                        width=16.0,
                    ),
                    background=draft.TextBackground(
                        color=(
                            background_color
                            or self.REVIEW_MARKER_BACKGROUND_COLORS.get(
                                str(marker.kind or "review_only"),
                                self.REVIEW_MARKER_BACKGROUND_COLORS["review_only"],
                            )
                        ),
                        style=1,
                        alpha=0.92,
                        round_radius=0.16,
                        height=text_layout.background_height,
                        width=text_layout.background_width,
                    ),
                )
                rendered[marker.item_id] = replace(
                    marker,
                    segment_id=segment.segment_id,
                    material_id=segment.material_id,
                    track_name=track_names_by_group[group][lane],
                )

        for marker in markers:
            receipt = rendered.get(marker.item_id)
            if receipt is not None:
                receipts.append(receipt)
        return receipts

    def _default_track_names(self, lane_count: int) -> Sequence[str]:
        configured = tuple(str(name) for name in self.REVIEW_MARKER_TRACKS)
        names = list(configured[:lane_count])
        if len(names) >= lane_count:
            return tuple(names)

        first_name = configured[0] if configured else "Review Marker"
        first_match = re.match(r"^(.*?)(\d+)\s*$", first_name)
        base_name = (first_match.group(1) if first_match else first_name).rstrip()
        numbered_names = [re.match(r"^.*?(\d+)\s*$", name) for name in configured]
        numbered_names = [match for match in numbered_names if match is not None]
        next_number = (
            int(numbered_names[-1].group(1)) + 1 if numbered_names else len(configured) + 1
        )
        while len(names) < lane_count:
            names.append(f"{base_name} {next_number}")
            next_number += 1
        return tuple(names)

    def _default_marker_layout(
        self,
        lane_count: int,
    ) -> Sequence[ReviewMarkerLayoutCell]:
        if lane_count <= 0:
            return ()
        layout_bound = max(0.0, self.REVIEW_MARKER_CANVAS_BOUND - 1e-6)
        span = layout_bound * 2.0
        cell_width = span / lane_count
        y = _REVIEW_MARKER_TOP_Y
        cell_height = (
            min(
                y - _REVIEW_MARKER_TOP_MIN_Y,
                _REVIEW_MARKER_TOP_MAX_Y - y,
            )
            * 2.0
        )
        return tuple(
            ReviewMarkerLayoutCell(
                x=-layout_bound + (lane + 0.5) * cell_width,
                y=y,
                cell_width=cell_width,
                cell_height=cell_height,
            )
            for lane in range(lane_count)
        )

    def _default_x_positions(self, lane_count: int) -> Sequence[float]:
        return tuple(cell.x for cell in self._default_marker_layout(lane_count))

    def _default_y_positions(self, lane_count: int) -> Sequence[float]:
        return tuple(cell.y for cell in self._default_marker_layout(lane_count))

    def _marker_text_layout(
        self,
        text: str,
        *,
        cell_width: float,
        cell_height: float,
        minimum_font_size: Optional[float] = None,
        maximum_font_size: Optional[float] = None,
        cell_safety: Optional[float] = None,
        line_height_factor: Optional[float] = None,
    ) -> ReviewMarkerTextLayout:
        safe_cell_width = max(float(cell_width), 1e-9)
        safe_cell_height = max(float(cell_height), 1e-9)
        safety = (
            self.REVIEW_MARKER_CELL_SAFETY
            if cell_safety is None
            else max(0.01, min(1.0, float(cell_safety)))
        )
        line_height = (
            self.REVIEW_MARKER_LINE_HEIGHT_FACTOR
            if line_height_factor is None
            else max(1e-6, float(line_height_factor))
        )
        background_width = safe_cell_width * safety
        max_background_height = min(
            1.0,
            safe_cell_height * safety,
        )
        max_line_width = background_width * 0.82
        max_text_height = max_background_height * 0.78

        def estimate(font_size: float) -> tuple[int, float]:
            character_width = max(
                font_size * self.REVIEW_MARKER_CHAR_WIDTH_FACTOR,
                1e-12,
            )
            characters_per_line = max(1, int(max_line_width / character_width))
            line_count = sum(
                max(1, math.ceil(len(line) / characters_per_line))
                for line in str(text or "").split("\n")
            )
            text_height = line_count * font_size * line_height
            return line_count, text_height

        minimum_size = (
            self.REVIEW_MARKER_MIN_FONT_SIZE
            if minimum_font_size is None
            else max(0.001, float(minimum_font_size))
        )
        maximum_size = (
            self.REVIEW_MARKER_MAX_FONT_SIZE
            if maximum_font_size is None
            else max(minimum_size, float(maximum_font_size))
        )
        if estimate(maximum_size)[1] <= max_text_height:
            font_size = maximum_size
        elif estimate(minimum_size)[1] > max_text_height:
            # A grouped lite marker never drops below the requested 4pt
            # floor.  The full dynamic layout retains its old shrinking rule.
            font_size = minimum_size
        else:
            low = minimum_size
            high = maximum_size
            for _ in range(40):
                midpoint = (low + high) / 2.0
                if estimate(midpoint)[1] <= max_text_height:
                    low = midpoint
                else:
                    high = midpoint
            font_size = low

        estimated_line_count, estimated_text_height = estimate(font_size)
        background_height = min(
            max_background_height,
            max(
                estimated_text_height * 1.1,
                min(0.04, safe_cell_height * 0.1),
            ),
        )
        return ReviewMarkerTextLayout(
            font_size=font_size,
            background_width=background_width,
            background_height=background_height,
            max_line_width=max_line_width,
            estimated_line_count=estimated_line_count,
            estimated_text_height=estimated_text_height,
        )

    def export_review_markers_manifest(
        self,
        output_path: str,
        markers: Iterable[ReviewMarkerItem],
    ) -> None:
        markers = list(markers)
        lines = [
            f"# {self.name} 校对标记清单",
            "",
            f"- 标记数量：{len(markers)}",
            f"- 说明：草稿内新增 `{self.REVIEW_MARKER_TRACKS[0]}/{self.REVIEW_MARKER_TRACKS[1]}/{self.REVIEW_MARKER_TRACKS[2]}` 轨道。",
            "",
            "| 输出时间 | 标记 | 说明 |",
            "| --- | --- | --- |",
        ]
        for marker in markers:
            lines.append(
                f"| {_sec_label(_to_seconds(marker.start_time))} | {marker.label} | {marker.detail or '-'} |"
            )
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _assign_review_marker_lanes(
        self,
        markers: Sequence[ReviewMarkerItem],
        *,
        lane_count: Optional[int],
    ) -> List[ReviewMarkerItem]:
        if lane_count is not None and lane_count <= 0:
            raise ValueError("Insufficient review marker lanes: no tracks were configured.")

        lane_ends = [-1.0] * (lane_count or 0)
        ordered = sorted(markers, key=lambda item: (_to_seconds(item.start_time), item.label))
        assigned: List[ReviewMarkerItem] = []

        for marker in ordered:
            start_sec = _to_seconds(marker.start_time)
            if marker.lane is not None:
                lane = int(marker.lane)
                if lane < 0 or (lane_count is not None and lane >= lane_count):
                    raise ValueError(f"Review marker lane {marker.lane} is out of range.")
                while lane >= len(lane_ends):
                    lane_ends.append(-1.0)
                if start_sec < lane_ends[lane]:
                    raise ValueError(
                        f"Insufficient review marker lanes: {marker.label!r} overlaps lane {lane}."
                    )
            else:
                lane = None
                for idx, lane_end in enumerate(lane_ends):
                    if start_sec >= lane_end:
                        lane = idx
                        break
                if lane is None:
                    if lane_count is not None:
                        raise ValueError(
                            "Insufficient review marker lanes: "
                            f"{marker.label!r} overlaps all {lane_count} configured lanes."
                        )
                    lane = len(lane_ends)
                    lane_ends.append(-1.0)

            end_sec = start_sec + max(0.0, _to_seconds(marker.duration))
            lane_ends[lane] = end_sec
            assigned.append(replace(marker, lane=lane))
        return assigned
