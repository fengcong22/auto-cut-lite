# ruff: noqa: E402
import json
import math
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(CURRENT_DIR)
SCRIPTS_PATH = os.path.join(SKILL_ROOT, "scripts")
if SCRIPTS_PATH not in sys.path:
    sys.path.insert(0, SCRIPTS_PATH)

from utils import revision_markers
from utils.env_setup import setup_env

setup_env()

from core.review_marker_ops import ReviewMarkerItem, ReviewMarkerOpsMixin
from utils.revision_markers import build_marker_plan, map_marker_plan_to_timeline
from utils.revision_models import (
    PauseAdjustment,
    PreservationRules,
    RevisionEdit,
    RevisionMarker,
    RevisionProject,
    RevisionRequest,
    RevisionReviewItem,
    build_revision_summary,
)


class _ReviewMarkerProject(ReviewMarkerOpsMixin):
    def __init__(self):
        self.ensured_tracks = []
        self.text_calls = []

    def _ensure_track(self, track_type, track_name):
        self.ensured_tracks.append((track_type, track_name))

    def add_text_simple(self, text, **kwargs):
        index = len(self.text_calls) + 1
        segment = SimpleNamespace(
            segment_id=f"segment-{index}",
            material_id=f"material-{index}",
        )
        self.text_calls.append({"text": text, **kwargs})
        return segment


class TestReviewMarkerRendering(unittest.TestCase):
    def test_visible_marker_text_comes_from_source_text_not_label(self):
        source_text = "00:12 删除原文中的这一段"
        project = _ReviewMarkerProject()

        receipts = project.add_review_markers(
            [
                ReviewMarkerItem(
                    label="修改07: 已处理 - 删除原文中的这一段",
                    source_text=source_text,
                    start_time="2s",
                    item_id="item-07",
                )
            ]
        )

        self.assertEqual(project.text_calls[0]["text"], source_text)
        self.assertEqual(receipts[0].label, source_text)
        self.assertEqual(receipts[0].source_text, source_text)

    def test_lite_grouped_marker_text_omits_execution_status(self):
        source_text = "05:18 小手只贴在开始位置"
        project = _ReviewMarkerProject()

        receipts = project.add_review_markers(
            [
                ReviewMarkerItem(
                    label="label_only_unresolved | " + source_text,
                    source_text=source_text,
                    execution_status="label_only_unresolved",
                    start_time="5s",
                    item_id="item-pointer",
                    kind="pointer_overlay",
                )
            ],
            layout_mode="lite_grouped",
        )

        self.assertEqual(project.text_calls[0]["text"], source_text)
        self.assertNotIn("label_only_unresolved", project.text_calls[0]["text"])
        self.assertEqual(receipts[0].execution_status, "label_only_unresolved")
        self.assertEqual(receipts[0].label, source_text)

    def test_legacy_marker_without_source_text_keeps_label_compatibility(self):
        project = _ReviewMarkerProject()
        receipts = project.add_review_markers(
            [ReviewMarkerItem(label="legacy marker", start_time="0s", item_id="legacy")]
        )

        self.assertEqual(project.text_calls[0]["text"], "legacy marker")
        self.assertEqual(receipts[0].source_text, "legacy marker")

    def test_lite_marker_mapping_keeps_source_time_for_split_gap(self):
        request = RevisionRequest(
            project=RevisionProject(
                draft_name="LiteMarkerMapping",
                source_video="source.mp4",
            ),
            edits=[
                RevisionEdit(
                    op_type="delete",
                    source_kind="spoken_delete",
                    start=2.0,
                    end=4.0,
                    label="delete",
                    doc_item_id="item-1",
                )
            ],
            markers=[],
            preserve=PreservationRules(),
            workflow_mode="lite",
            lite_cut_layout="split_gap",
        )
        plan = [
            revision_markers.MarkerPlanItem(
                item_id="item-1",
                source_text="delete",
                start=5.0,
                end=7.0,
                verbatim_status="verified",
            )
        ]

        mapped = map_marker_plan_to_timeline(plan, request)

        self.assertEqual([(item.start, item.end) for item in mapped], [(5.0, 7.0)])

    def test_lite_marker_mapping_ignores_label_only_pause_duration(self):
        request = RevisionRequest(
            project=RevisionProject(
                draft_name="LiteMarkerPauseMapping",
                source_video="source.mp4",
            ),
            edits=[],
            markers=[],
            preserve=PreservationRules(),
            pause_adjustments=[
                PauseAdjustment(
                    item_id="pause-1",
                    source_time=3.0,
                    duration=1.25,
                    frame_path="frame.png",
                )
            ],
            workflow_mode="lite",
        )
        plan = [
            revision_markers.MarkerPlanItem(
                item_id="before",
                source_text="before pause",
                start=2.0,
                end=2.5,
                verbatim_status="verified",
            ),
            revision_markers.MarkerPlanItem(
                item_id="after",
                source_text="after pause",
                start=5.0,
                end=5.5,
                verbatim_status="verified",
            ),
        ]

        mapped = map_marker_plan_to_timeline(plan, request)

        self.assertEqual(
            [(item.start, item.end) for item in mapped],
            [(2.0, 2.5), (5.0, 5.5)],
        )

    def test_lite_marker_on_pause_boundary_stays_at_source_time(self):
        request = RevisionRequest(
            project=RevisionProject(
                draft_name="LiteMarkerPauseBoundary",
                source_video="source.mp4",
            ),
            edits=[],
            markers=[],
            preserve=PreservationRules(),
            pause_adjustments=[
                PauseAdjustment(
                    item_id="pause-1",
                    source_time=5.0,
                    duration=1.0,
                    frame_path="frame.png",
                )
            ],
            workflow_mode="lite",
        )
        plan = [
            revision_markers.MarkerPlanItem(
                item_id="pause-1",
                source_text="05:00 在现有停顿基础上增加1秒",
                start=5.0,
                end=7.0,
                verbatim_status="verified",
            )
        ]

        mapped = map_marker_plan_to_timeline(plan, request)

        self.assertEqual([(item.start, item.end) for item in mapped], [(5.0, 7.0)])

    def test_verbatim_marker_receipt_preserves_source_metadata_and_rendered_ids(self):
        source_text = "Canonical source text that must remain exactly unchanged."
        project = _ReviewMarkerProject()

        receipts = project.add_review_markers(
            [
                ReviewMarkerItem(
                    label=source_text,
                    start_time="4.000s",
                    duration="1.500s",
                    item_id="item-007",
                    source_text=source_text,
                    verbatim_status="verified",
                )
            ]
        )

        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual(receipt.item_id, "item-007")
        self.assertEqual(receipt.source_text, source_text)
        self.assertEqual(receipt.verbatim_status, "verified")
        self.assertEqual(receipt.segment_id, "segment-1")
        self.assertEqual(receipt.material_id, "material-1")
        self.assertEqual(receipt.track_name, "Review Marker 1")
        self.assertEqual(project.text_calls[0]["text"], source_text)

    def test_seven_overlapping_markers_expand_across_one_top_layout_row(self):
        project = _ReviewMarkerProject()
        long_source = "Long canonical source text " * 20
        markers = [
            ReviewMarkerItem(
                label=long_source if index == 6 else f"marker {index + 1}",
                start_time="10s",
                duration="2s",
                item_id=f"item-{index + 1}",
            )
            for index in range(7)
        ]

        receipts = project.add_review_markers(markers)

        self.assertEqual([item.lane for item in receipts], list(range(7)))
        self.assertEqual(
            [item.track_name for item in receipts],
            [f"Review Marker {index}" for index in range(1, 8)],
        )
        self.assertEqual(len({call["track_name"] for call in project.text_calls}), 7)
        first_clip = project.text_calls[0]["clip_settings"]
        seventh_clip = project.text_calls[6]["clip_settings"]
        self.assertNotEqual(first_clip.transform_x, seventh_clip.transform_x)
        self.assertEqual(first_clip.transform_y, seventh_clip.transform_y)
        self.assertEqual(first_clip.transform_y, 0.8)
        long_text_call = next(call for call in project.text_calls if call["text"] == long_source)
        self.assertGreater(long_text_call["style"].size, 0.0)
        self.assertEqual(long_text_call["text"], long_source)

    def test_top_marker_preserves_canonical_newlines_code_point_for_code_point(self):
        source_text = " 00:51, keep punctuation??\r\nsecond line\nthird line  "
        project = _ReviewMarkerProject()

        receipts = project.add_review_markers(
            [
                ReviewMarkerItem(
                    label=source_text,
                    source_text=source_text,
                    start_time="0s",
                    item_id="item-multiline",
                    verbatim_status="verified",
                )
            ]
        )

        self.assertEqual(project.text_calls[0]["text"], source_text)
        self.assertEqual(receipts[0].label, source_text)
        self.assertEqual(receipts[0].source_text, source_text)

    def test_default_dynamic_tracks_ignore_colliding_custom_x_positions(self):
        project = _ReviewMarkerProject()
        markers = [
            ReviewMarkerItem(label=f"marker {index}", start_time="0s", duration="2s")
            for index in range(2)
        ]

        receipts = project.add_review_markers(markers, x_positions=(0.0, 0.0))

        self.assertEqual(
            [receipt.track_name for receipt in receipts],
            ["Review Marker 1", "Review Marker 2"],
        )
        self.assertEqual(
            [call["clip_settings"].transform_x for call in project.text_calls],
            [cell.x for cell in project._default_marker_layout(2)],
        )
        self.assertEqual(
            {call["clip_settings"].transform_y for call in project.text_calls},
            {0.8},
        )

    def test_dynamic_top_centerline_cannot_be_overridden_by_subclass(self):
        class MisconfiguredProject(_ReviewMarkerProject):
            REVIEW_MARKER_TOP_Y = 0.85

        project = MisconfiguredProject()

        project.add_review_markers(
            [ReviewMarkerItem(label="marker", start_time="0s", duration="2s")]
        )

        self.assertEqual(project.text_calls[0]["clip_settings"].transform_y, 0.8)

    def test_dynamic_layout_compacts_sparse_caller_lane_to_actual_concurrency(self):
        project = _ReviewMarkerProject()

        receipts = project.add_review_markers(
            [
                ReviewMarkerItem(
                    label="marker",
                    start_time="0s",
                    duration="2s",
                    lane=5,
                )
            ]
        )

        self.assertEqual(receipts[0].lane, 0)
        self.assertEqual(receipts[0].track_name, "Review Marker 1")
        self.assertEqual(project.text_calls[0]["clip_settings"].transform_x, 0.0)

    def test_explicit_dynamic_track_names_ignore_colliding_custom_x_positions(self):
        project = _ReviewMarkerProject()
        markers = [
            ReviewMarkerItem(label=f"marker {index}", start_time="0s", duration="2s")
            for index in range(2)
        ]

        project.add_review_markers(
            markers,
            track_names=("Review Marker 1", "Review Marker 2"),
            x_positions=(0.0, 0.0),
        )

        self.assertEqual(
            [call["clip_settings"].transform_x for call in project.text_calls],
            [cell.x for cell in project._default_marker_layout(2)],
        )

    def test_non_dynamic_custom_tracks_preserve_explicit_x_positions(self):
        project = _ReviewMarkerProject()
        markers = [
            ReviewMarkerItem(label=f"marker {index}", start_time="0s", duration="2s")
            for index in range(2)
        ]

        receipts = project.add_review_markers(
            markers,
            track_names=("Custom A", "Custom B"),
            x_positions=(-0.2, 0.2),
        )

        self.assertEqual(
            [receipt.track_name for receipt in receipts],
            ["Custom A", "Custom B"],
        )
        self.assertEqual(
            [call["clip_settings"].transform_x for call in project.text_calls],
            [-0.2, 0.2],
        )

    def test_non_dynamic_subclass_tracks_preserve_explicit_x_positions(self):
        class CustomTrackProject(_ReviewMarkerProject):
            REVIEW_MARKER_TRACKS = ("Audit Marker 4", "Audit Marker 5")

        project = CustomTrackProject()
        markers = [
            ReviewMarkerItem(label=f"marker {index}", start_time="0s", duration="2s")
            for index in range(2)
        ]

        receipts = project.add_review_markers(markers, x_positions=(-0.25, 0.25))

        self.assertEqual(
            [receipt.track_name for receipt in receipts],
            ["Audit Marker 4", "Audit Marker 5"],
        )
        self.assertEqual(
            [call["clip_settings"].transform_x for call in project.text_calls],
            [-0.25, 0.25],
        )

    def test_mixed_dynamic_and_custom_track_names_are_rejected(self):
        project = _ReviewMarkerProject()

        with self.assertRaisesRegex(ValueError, "cannot mix dynamic and custom"):
            project.add_review_markers(
                [ReviewMarkerItem(label="marker", start_time="0s", duration="2s")],
                track_names=("Review Marker 1", "Custom A"),
                x_positions=(-0.2, 0.2),
            )

    def test_overlapping_markers_raise_when_explicit_track_capacity_is_insufficient(self):
        project = _ReviewMarkerProject()
        markers = [
            ReviewMarkerItem(label=f"marker {index}", start_time="0s", duration="2s")
            for index in range(3)
        ]

        with self.assertRaisesRegex(ValueError, "Insufficient review marker lanes"):
            project.add_review_markers(
                markers,
                track_names=("Custom A", "Custom B"),
                x_positions=(-0.2, 0.2),
            )

    def test_42_lane_default_layout_cells_are_inside_canvas_and_disjoint(self):
        project = _ReviewMarkerProject()

        cells = project._default_marker_layout(42)

        self.assertEqual(len(cells), 42)
        self.assertEqual({cell.y for cell in cells}, {0.8})
        for cell in cells:
            self.assertGreater(cell.cell_width, 0.0)
            self.assertGreater(cell.cell_height, 0.0)
            self.assertGreaterEqual(cell.x - cell.cell_width / 2.0, -0.9)
            self.assertLessEqual(cell.x + cell.cell_width / 2.0, 0.9)
            self.assertGreaterEqual(cell.y - cell.cell_height / 2.0, -0.9)
            self.assertLessEqual(cell.y + cell.cell_height / 2.0, 0.9)
            self.assertGreaterEqual(cell.y, 0.7)
            self.assertLessEqual(cell.y, 0.9)

        for index, first in enumerate(cells):
            for second in cells[index + 1 :]:
                horizontal_overlap = (
                    abs(first.x - second.x) < (first.cell_width + second.cell_width) / 2.0 - 1e-9
                )
                vertical_overlap = (
                    abs(first.y - second.y) < (first.cell_height + second.cell_height) / 2.0 - 1e-9
                )
                self.assertFalse(horizontal_overlap and vertical_overlap)

    def test_42_long_verbatim_markers_fit_disjoint_backgrounds_without_rewriting(self):
        project = _ReviewMarkerProject()
        source_text = "x" * 70
        markers = [
            ReviewMarkerItem(
                label=source_text,
                start_time="10s",
                duration="2s",
                item_id=f"item-{index + 1}",
                source_text=source_text,
            )
            for index in range(42)
        ]

        receipts = project.add_review_markers(markers)
        cells = project._default_marker_layout(42)

        self.assertEqual(len(receipts), 42)
        self.assertEqual([call["text"] for call in project.text_calls], [source_text] * 42)
        backgrounds = []
        for cell, call in zip(cells, project.text_calls):
            text_layout = project._marker_text_layout(
                source_text,
                cell_width=cell.cell_width,
                cell_height=cell.cell_height,
            )
            clip = call["clip_settings"]
            background = call["background"]
            self.assertGreater(call["style"].size, 0.0)
            self.assertLessEqual(text_layout.estimated_text_height, background.height)
            self.assertLessEqual(background.width, cell.cell_width * 0.9 + 1e-9)
            self.assertLessEqual(background.height, cell.cell_height * 0.9 + 1e-9)
            self.assertGreaterEqual(clip.transform_x - background.width / 2.0, -0.9)
            self.assertLessEqual(clip.transform_x + background.width / 2.0, 0.9)
            self.assertGreaterEqual(clip.transform_y - background.height / 2.0, -0.9)
            self.assertLessEqual(clip.transform_y + background.height / 2.0, 0.9)
            backgrounds.append(
                (clip.transform_x, clip.transform_y, background.width, background.height)
            )

        for index, first in enumerate(backgrounds):
            for second in backgrounds[index + 1 :]:
                horizontal_overlap = abs(first[0] - second[0]) < (first[2] + second[2]) / 2.0 - 1e-9
                vertical_overlap = abs(first[1] - second[1]) < (first[3] + second[3]) / 2.0 - 1e-9
                self.assertFalse(horizontal_overlap and vertical_overlap)

    def test_single_dynamic_marker_uses_the_full_top_safe_width(self):
        project = _ReviewMarkerProject()
        source_text = "Long canonical review text " * 12

        project.add_review_markers(
            [
                ReviewMarkerItem(
                    label=source_text,
                    start_time="10s",
                    duration="2s",
                    item_id="item-single",
                    source_text=source_text,
                )
            ]
        )

        cell = project._default_marker_layout(1)[0]
        background = project.text_calls[0]["background"]
        self.assertGreaterEqual(background.width, cell.cell_width * 0.85)
        self.assertLessEqual(background.width, cell.cell_width * 0.9 + 1e-9)

    def test_lite_grouped_layout_separates_kinds_and_keeps_labels_readable(self):
        project = _ReviewMarkerProject()
        markers = [
            ReviewMarkerItem(
                label="delete one",
                source_text="delete one",
                start_time="10s",
                duration="2s",
                item_id="delete-1",
                kind="phrase_delete",
            ),
            ReviewMarkerItem(
                label="delete two",
                source_text="delete two",
                start_time="10s",
                duration="2s",
                item_id="delete-2",
                kind="spoken_delete",
            ),
            ReviewMarkerItem(
                label="point to the supplied object",
                source_text="point to the supplied object",
                start_time="10s",
                duration="2s",
                item_id="visual-1",
                kind="pointer_overlay",
            ),
            ReviewMarkerItem(
                label="advance the animation",
                source_text="advance the animation",
                start_time="10s",
                duration="2s",
                item_id="animation-1",
                kind="animation_timing",
            ),
        ]

        receipts = project.add_review_markers(markers, layout_mode="lite_grouped")

        self.assertEqual(
            {receipt.track_name for receipt in receipts},
            {
                "Review Marker Delete 1",
                "Review Marker Delete 2",
                "Review Marker Visual 1",
                "Review Marker Animation 1",
            },
        )
        self.assertEqual(
            [receipt.track_name for receipt in receipts[:2]],
            ["Review Marker Delete 1", "Review Marker Delete 2"],
        )
        self.assertEqual(project.text_calls[0]["background"].color, "#B42318")
        self.assertEqual(project.text_calls[1]["background"].color, "#B42318")
        self.assertEqual(project.text_calls[2]["background"].color, "#15803D")
        self.assertEqual(project.text_calls[3]["background"].color, "#B45309")
        for call in project.text_calls:
            self.assertEqual(call["style"].align, 0)
            self.assertGreaterEqual(call["style"].size, 4.0)
            self.assertLessEqual(call["style"].size, 5.0)
            self.assertTrue(call["style"].auto_wrapping)
            self.assertTrue(call["style"].force_apply_line_max_width)
            self.assertGreater(call["style"].max_line_width, 0.0)
            self.assertLessEqual(call["style"].max_line_width, 0.9)
            self.assertEqual(call["clip_settings"].transform_x, 0.0)
            background = call["background"]
            self.assertGreaterEqual(
                call["clip_settings"].transform_x - background.width / 2.0,
                -0.9,
            )
            self.assertLessEqual(
                call["clip_settings"].transform_x + background.width / 2.0,
                0.9,
            )

    def test_lite_grouped_long_marker_forces_wrapping_without_rewriting_text(self):
        project = _ReviewMarkerProject()
        source_text = "30，当时法国就是法兰西第二帝国，他的皇帝拿破仑三世和10万官兵就成了俘虏，这法法军就惨败"

        receipts = project.add_review_markers(
            [
                ReviewMarkerItem(
                    label=source_text,
                    source_text=source_text,
                    start_time="10s",
                    duration="2s",
                    item_id="long-delete",
                    kind="colored_span_delete",
                    background_color="#6D28D9",
                )
            ],
            layout_mode="lite_grouped",
        )

        self.assertEqual(receipts[0].source_text, source_text)
        self.assertEqual(project.text_calls[0]["text"], source_text)
        self.assertEqual(project.text_calls[0]["background"].color, "#B42318")
        style = project.text_calls[0]["style"]
        self.assertTrue(style.auto_wrapping)
        self.assertTrue(style.force_apply_line_max_width)
        self.assertLessEqual(style.max_line_width, 0.9)

    def test_default_track_names_respect_subclass_prefix_and_extend_last_number(self):
        class CustomTrackProject(_ReviewMarkerProject):
            REVIEW_MARKER_TRACKS = ("Audit Marker 4", "Audit Marker 5")

        project = CustomTrackProject()
        markers = [
            ReviewMarkerItem(label=f"marker {index}", start_time="0s", duration="2s")
            for index in range(4)
        ]

        receipts = project.add_review_markers(markers)

        self.assertEqual(
            [receipt.track_name for receipt in receipts],
            ["Audit Marker 4", "Audit Marker 5", "Audit Marker 6", "Audit Marker 7"],
        )


class TestRevisionMarkerSkillContract(unittest.TestCase):
    def test_skill_and_checklist_require_saved_dynamic_markers_in_one_top_band(self):
        skill_path = os.path.join(
            SKILL_ROOT,
            "skills",
            "auto-cut-revision-draft",
            "SKILL.md",
        )
        checklist_path = os.path.join(
            SKILL_ROOT,
            "skills",
            "auto-cut-revision-draft",
            "references",
            "checklist.md",
        )
        with open(skill_path, "r", encoding="utf-8") as file:
            skill_text = file.read()
        with open(checklist_path, "r", encoding="utf-8") as file:
            checklist_text = file.read()

        for text in (skill_text, checklist_text):
            normalized_text = text.casefold()
            self.assertIn("one horizontal top safe band", text)
            self.assertIn("`clip.transform.y`", text)
            self.assertIn("`0.7..0.9`", text)
            self.assertIn("centerline `y=0.8`", text)
            self.assertIn("one saved draft-content object per call", text)
            self.assertIn(
                "caller-supplied `x_positions` cannot override",
                normalized_text,
            )
            self.assertIn("explicitly non-dynamic track groups", normalized_text)
            self.assertIn("caller-supplied lane hints", normalized_text)
            self.assertIn("mixed dynamic/custom track-name groups are invalid", normalized_text)


class TestSavedMarkerPlanValidation(unittest.TestCase):
    def _plan(self, texts, *, statuses=None):
        statuses = statuses or ["verified"] * len(texts)
        return [
            revision_markers.MarkerPlanItem(
                item_id=f"item-{index}",
                source_text=text,
                start=float(index),
                end=float(index) + 0.8,
                verbatim_status=statuses[index - 1],
                source=f"doc:block-{index}",
            )
            for index, text in enumerate(texts, start=1)
        ]

    def _content(self, texts):
        materials = []
        segments = []
        for index, text in enumerate(texts, start=1):
            material_id = f"material-{index}"
            materials.append(
                {
                    "id": material_id,
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                }
            )
            segments.append(
                {
                    "id": f"segment-{index}",
                    "material_id": material_id,
                    "target_timerange": {"start": index * 1_000_000, "duration": 800_000},
                }
            )
        return {
            "tracks": [{"name": "Review Marker 1", "type": "text", "segments": segments}],
            "materials": {"texts": materials},
        }

    def _receipts(self, count):
        return [
            {
                "item_id": f"item-{index}",
                "source_text": f"source-{index}",
                "verbatim_status": "verified",
                "segment_id": f"segment-{index}",
                "material_id": f"material-{index}",
                "track_name": "Review Marker 1",
            }
            for index in range(1, count + 1)
        ]

    def test_exact_saved_marker_text_passes_and_preserves_saved_evidence(self):
        text = "00:27-00:45 删除原句，保留全部标点。"
        plan = self._plan([text])
        receipts = self._receipts(1)
        receipts[0]["source_text"] = text

        result = revision_markers.validate_saved_marker_plan(plan, self._content([text]), receipts)

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["metrics"]["expected_count"], 1)
        self.assertEqual(result["metrics"]["actual_count"], 1)
        self.assertEqual(
            result["metrics"]["saved_markers"][0],
            {
                "segment_id": "segment-1",
                "material_id": "material-1",
                "track_name": "Review Marker 1",
                "start": 1_000_000,
                "text": text,
            },
        )

    def test_marker_plan_keeps_execution_status_outside_source_text(self):
        request = RevisionRequest(
            project=RevisionProject(
                draft_name="StatusMarkerDraft",
                source_video="source.mp4",
            ),
            edits=[],
            markers=[],
            preserve=PreservationRules(),
            review_items=[
                RevisionReviewItem(
                    "item-status",
                    "pointer_overlay",
                    "canonical pointer instruction",
                    evidence={"execution_status": "label_only_unresolved"},
                )
            ],
        )

        plan = build_marker_plan(request)

        self.assertEqual(plan[0].source_text, "canonical pointer instruction")
        self.assertEqual(plan[0].execution_status, "label_only_unresolved")

    def test_count_correct_but_summary_text_fails(self):
        result = revision_markers.validate_saved_marker_plan(
            self._plan(["完整的审阅源文，不能概括。"]),
            self._content(["审阅源文摘要"]),
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("verbatim" in error.lower() for error in result["errors"]))

    def test_truncated_saved_marker_text_fails(self):
        result = revision_markers.validate_saved_marker_plan(
            self._plan(["必须保留到这一句的最后一个标点。"]),
            self._content(["必须保留到这一句"]),
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("verbatim" in error.lower() for error in result["errors"]))

    def test_missing_saved_marker_fails_exact_source_ledger_count(self):
        result = revision_markers.validate_saved_marker_plan(
            self._plan(["第一条", "第二条"]), self._content(["第一条"])
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("count mismatch" in error.lower() for error in result["errors"]))

    def test_extra_saved_marker_fails_exact_source_ledger_count(self):
        result = revision_markers.validate_saved_marker_plan(
            self._plan(["第一条"]), self._content(["第一条", "额外标记"])
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("count mismatch" in error.lower() for error in result["errors"]))

    def test_duplicate_saved_text_cannot_replace_other_expected_text(self):
        result = revision_markers.validate_saved_marker_plan(
            self._plan(["相同文本", "必须独立保留的另一条"]),
            self._content(["相同文本", "相同文本"]),
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("verbatim" in error.lower() for error in result["errors"]))

    def test_no_receipts_uses_text_multiset_with_correct_duplicate_multiplicity(self):
        shared = "相同源文"
        content = self._content(["另一条", shared, shared])
        content["tracks"][0]["segments"][0]["target_timerange"]["start"] = 2_000_000
        content["tracks"][0]["segments"][1]["target_timerange"]["start"] = 1_000_000
        result = revision_markers.validate_saved_marker_plan(
            self._plan([shared, "另一条", shared]),
            content,
        )

        self.assertTrue(result["ok"], result["errors"])

    def test_no_receipts_rejects_correct_text_at_wrong_mapped_start(self):
        text = "right text, wrong mapped start"
        content = self._content([text])
        content["tracks"][0]["segments"][0]["target_timerange"]["start"] = 1_250_000

        result = revision_markers.validate_saved_marker_plan(self._plan([text]), content)

        self.assertFalse(result["ok"])
        self.assertTrue(any("timeline start" in error for error in result["errors"]))

    def test_no_receipts_accepts_reordered_pairs_for_identical_text(self):
        shared = "same source text under two distinct item ids"
        content = self._content([shared, shared])
        segments = content["tracks"][0]["segments"]
        segments[0]["target_timerange"]["start"] = 2_000_000
        segments[1]["target_timerange"]["start"] = 1_000_000

        result = revision_markers.validate_saved_marker_plan(self._plan([shared, shared]), content)

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["metrics"]["timing_mismatched_item_ids"], [])

    def test_no_receipts_rejects_wrong_pair_multiset_for_identical_text(self):
        shared = "same source text with one genuinely wrong start"
        content = self._content([shared, shared])
        segments = content["tracks"][0]["segments"]
        segments[0]["target_timerange"]["start"] = 3_000_000
        segments[1]["target_timerange"]["start"] = 1_000_000

        result = revision_markers.validate_saved_marker_plan(self._plan([shared, shared]), content)

        self.assertFalse(result["ok"])
        self.assertEqual(result["metrics"]["timing_mismatched_item_ids"], ["item-2"])
        self.assertTrue(
            any("expected 2000000us, saved 3000000us" in error for error in result["errors"])
        )

    def test_no_receipts_accepts_exact_text_at_exact_mapped_start(self):
        text = "right text at the right mapped start"

        result = revision_markers.validate_saved_marker_plan(
            self._plan([text]), self._content([text])
        )

        self.assertTrue(result["ok"], result["errors"])

    def test_no_receipts_allows_renderer_millisecond_start_rounding(self):
        text = "renderer rounds marker starts to milliseconds"
        plan = [
            revision_markers.MarkerPlanItem(
                item_id="item-rounded",
                source_text=text,
                start=1.2344,
                end=2.0,
                verbatim_status="verified",
                source="doc:block-rounded",
            )
        ]
        content = self._content([text])
        content["tracks"][0]["segments"][0]["target_timerange"]["start"] = 1_234_000

        result = revision_markers.validate_saved_marker_plan(plan, content)

        self.assertTrue(result["ok"], result["errors"])

    def test_literal_question_marks_pass_only_when_saved_text_is_exact(self):
        expected = "原文中的合法占位 ???? 必须原样保留"
        exact = revision_markers.validate_saved_marker_plan(
            self._plan([expected]), self._content([expected])
        )
        changed = revision_markers.validate_saved_marker_plan(
            self._plan([expected]), self._content(["原文中的合法占位 ?? 必须原样保留"])
        )

        self.assertTrue(exact["ok"], exact["errors"])
        self.assertFalse(changed["ok"])

    def test_unverified_source_text_mismatch_warns_without_failing(self):
        result = revision_markers.validate_saved_marker_plan(
            self._plan(["fallback source"], statuses=["unverified_source_unavailable"]),
            self._content(["changed fallback source"]),
        )

        self.assertTrue(result["ok"], result["errors"])
        self.assertTrue(any("unverified" in warning.lower() for warning in result["warnings"]))

    def test_exact_unverified_marker_warns_without_failing(self):
        text = "exact text still carries an unverified status"

        result = revision_markers.validate_saved_marker_plan(
            self._plan([text], statuses=["unverified_timing_unavailable"]),
            self._content([text]),
        )

        self.assertTrue(result["ok"], result["errors"])
        self.assertTrue(
            any(
                "item-1" in warning and "unverified_timing_unavailable" in warning
                for warning in result["warnings"]
            )
        )

    def test_unverified_source_timing_mismatch_warns_without_failing(self):
        text = "fallback source with uncertain timing"
        content = self._content([text])
        content["tracks"][0]["segments"][0]["target_timerange"]["start"] = 1_250_000

        result = revision_markers.validate_saved_marker_plan(
            self._plan([text], statuses=["unverified_source_unavailable"]), content
        )

        self.assertTrue(result["ok"], result["errors"])
        self.assertTrue(any("timeline start" in warning for warning in result["warnings"]))
        self.assertEqual(result["metrics"]["timing_mismatched_item_ids"], ["item-1"])

    def test_unverified_timing_text_mismatch_still_fails_verbatim_validation(self):
        result = revision_markers.validate_saved_marker_plan(
            self._plan(
                ["timing uncertain, source exact"], statuses=["unverified_timing_unavailable"]
            ),
            self._content(["source was changed"]),
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("not verbatim" in error for error in result["errors"]))

    def test_receipt_segment_material_and_track_must_match_saved_marker(self):
        text = "receipt-bound exact source"
        plan = self._plan([text])
        content = self._content([text])
        for field, wrong_value in (
            ("segment_id", "missing-segment"),
            ("material_id", "wrong-material"),
            ("track_name", "Review Marker 2"),
        ):
            with self.subTest(field=field):
                receipts = self._receipts(1)
                receipts[0]["source_text"] = text
                receipts[0][field] = wrong_value

                result = revision_markers.validate_saved_marker_plan(plan, content, receipts)

                self.assertFalse(result["ok"])
                self.assertTrue(any(field in error for error in result["errors"]))

    def test_canonical_legacy_prefixed_source_enforces_text_with_receipt(self):
        expected = "canonical text from an older document system"
        plan = [
            revision_markers.MarkerPlanItem(
                item_id="item-1",
                source_text=expected,
                start=1.0,
                end=2.0,
                verbatim_status="verified",
                source="legacy_document:block-1",
            )
        ]
        receipts = self._receipts(1)
        receipts[0]["source_text"] = expected

        result = revision_markers.validate_saved_marker_plan(
            plan, self._content(["rewritten summary"]), receipts
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("not verbatim" in error for error in result["errors"]))

    def test_canonical_legacy_prefixed_source_enforces_text_without_receipts(self):
        plan = [
            revision_markers.MarkerPlanItem(
                item_id="item-1",
                source_text="canonical external source text",
                start=1.0,
                end=2.0,
                verbatim_status="verified",
                source="legacy_document:block-1",
            )
        ]

        result = revision_markers.validate_saved_marker_plan(
            plan, self._content(["different saved text"])
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("not verbatim" in error for error in result["errors"]))

    def test_internal_legacy_source_sentinels_keep_text_compatibility(self):
        plan = [
            revision_markers.MarkerPlanItem(
                item_id="legacy-marker",
                source_text="old marker label",
                start=1.0,
                end=2.0,
                verbatim_status="unverified_source_unavailable",
                source="legacy_marker",
            ),
            revision_markers.MarkerPlanItem(
                item_id="legacy-edit",
                source_text="old edit label",
                start=2.0,
                end=3.0,
                verbatim_status="unverified_source_unavailable",
                source="legacy_edit",
            ),
        ]

        result = revision_markers.validate_saved_marker_plan(
            plan, self._content(["changed marker", "changed edit"])
        )

        self.assertTrue(result["ok"], result["errors"])
        self.assertFalse(result["metrics"]["source_ledger"])

    def test_receipt_verified_marker_rejects_wrong_saved_start(self):
        text = "receipt-bound verified marker"
        plan = self._plan([text])
        content = self._content([text])
        content["tracks"][0]["segments"][0]["target_timerange"]["start"] = 9_000_000
        receipts = self._receipts(1)
        receipts[0]["source_text"] = text

        result = revision_markers.validate_saved_marker_plan(plan, content, receipts)

        self.assertFalse(result["ok"])
        self.assertTrue(any("timeline start" in error for error in result["errors"]))
        self.assertEqual(result["metrics"]["timing_mismatched_item_ids"], ["item-1"])
        self.assertEqual(result["metrics"]["exact_match_count"], 0)
        self.assertEqual(result["metrics"]["verbatim_match_count"], 1)

    def test_receipt_unverified_marker_warns_for_wrong_saved_start(self):
        text = "receipt literal ???? remains verbatim"
        plan = self._plan([text], statuses=["unverified_timing_unavailable"])
        content = self._content([text])
        content["tracks"][0]["segments"][0]["target_timerange"]["start"] = 9_000_000
        receipts = self._receipts(1)
        receipts[0]["source_text"] = text
        receipts[0]["verbatim_status"] = "unverified_timing_unavailable"

        result = revision_markers.validate_saved_marker_plan(plan, content, receipts)

        self.assertTrue(result["ok"], result["errors"])
        self.assertTrue(any("timeline start" in warning for warning in result["warnings"]))
        self.assertEqual(result["metrics"]["timing_mismatched_item_ids"], ["item-1"])
        self.assertEqual(result["metrics"]["exact_match_count"], 0)
        self.assertEqual(result["metrics"]["verbatim_match_count"], 1)
        self.assertEqual(result["metrics"]["exact_marker_material_ids"], ["material-1"])

    def test_receipt_marker_start_within_serialization_tolerance_passes(self):
        text = "receipt timing within serialization tolerance"
        plan = self._plan([text])
        content = self._content([text])
        content["tracks"][0]["segments"][0]["target_timerange"]["start"] = 1_000_500
        receipts = self._receipts(1)
        receipts[0]["source_text"] = text

        result = revision_markers.validate_saved_marker_plan(plan, content, receipts)

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["metrics"]["timing_mismatched_item_ids"], [])
        self.assertEqual(result["metrics"]["exact_match_count"], 1)

    def test_receipt_marker_omitted_zero_start_is_saved_at_timeline_zero(self):
        text = "receipt marker at timeline zero"
        plan = [
            revision_markers.MarkerPlanItem(
                item_id="item-1",
                source_text=text,
                start=0.0,
                end=0.8,
                verbatim_status="verified",
                source="doc:block-1",
            )
        ]
        content = self._content([text])
        del content["tracks"][0]["segments"][0]["target_timerange"]["start"]
        receipts = self._receipts(1)
        receipts[0]["source_text"] = text

        result = revision_markers.validate_saved_marker_plan(plan, content, receipts)

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["metrics"]["timing_mismatched_item_ids"], [])

    def test_review_marker_item_objects_are_accepted_as_receipts(self):
        text = "object receipt source"
        receipt = ReviewMarkerItem(
            label=text,
            start_time="1s",
            item_id="item-1",
            source_text=text,
            verbatim_status="verified",
            segment_id="segment-1",
            material_id="material-1",
            track_name="Review Marker 1",
        )

        result = revision_markers.validate_saved_marker_plan(
            self._plan([text]), self._content([text]), [receipt]
        )

        self.assertTrue(result["ok"], result["errors"])

    def test_explicit_empty_receipts_are_structurally_missing_for_nonempty_plan(self):
        text = "receipt is required when the caller supplied a receipt list"

        result = revision_markers.validate_saved_marker_plan(
            self._plan([text]), self._content([text]), []
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("missing item_id" in error for error in result["errors"]))

    def test_explicit_empty_receipts_make_empty_plan_authoritative(self):
        result = revision_markers.validate_saved_marker_plan(
            [], self._content(["stale marker from an older ledger"]), []
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("count mismatch" in error for error in result["errors"]))


class TestRevisionMarkerPlan(unittest.TestCase):
    def _request(self, *, edits=None, markers=None, review_items=None):
        return RevisionRequest(
            project=RevisionProject(
                draft_name="ReviewDraft",
                source_video="C:/media/source.mp4",
            ),
            edits=list(edits or []),
            markers=list(markers or []),
            preserve=PreservationRules(),
            review_items=list(review_items or []),
        )

    def test_latest_doc_text_overrides_request_summary_and_merges_action_window(self):
        request = self._request(
            edits=[
                RevisionEdit(
                    op_type="delete",
                    start=120.10,
                    end=123.30,
                    label="删除摘要后半段",
                    doc_item_id="修改007",
                ),
                RevisionEdit(
                    op_type="delete",
                    start=118.95,
                    end=120.00,
                    label="删除摘要前半段",
                    doc_item_id="修改007",
                ),
            ],
            review_items=[
                RevisionReviewItem(
                    item_id="修改007",
                    kind="spoken_delete",
                    source_text="删除一段口播",
                )
            ],
        )
        latest_doc_items = [
            RevisionReviewItem(
                item_id="修改007",
                kind="spoken_delete",
                source_text="01:59-02:00，删除“无产阶级必须”",
                source="doc:block-007",
            )
        ]

        plan = build_marker_plan(request, doc_items=latest_doc_items)

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].item_id, "修改007")
        self.assertEqual(plan[0].source_text, "01:59-02:00，删除“无产阶级必须”")
        self.assertEqual((plan[0].start, plan[0].end), (118.95, 123.30))
        self.assertEqual(plan[0].source, "doc:block-007")

    def test_identical_text_under_different_ids_remains_two_ordered_items(self):
        shared_text = "保留完全相同的原始审阅文字"
        request = self._request(
            review_items=[
                RevisionReviewItem(
                    item_id="修改001",
                    kind="review_only",
                    source_text=shared_text,
                    start=10.0,
                    end=11.0,
                ),
                RevisionReviewItem(
                    item_id="校对002",
                    kind="review_only",
                    source_text=shared_text,
                    start=20.0,
                    end=21.0,
                ),
            ]
        )

        plan = build_marker_plan(request)

        self.assertEqual([item.item_id for item in plan], ["修改001", "校对002"])
        self.assertEqual([item.source_text for item in plan], [shared_text, shared_text])

    def test_duplicate_source_item_id_fails(self):
        request = self._request(
            review_items=[
                RevisionReviewItem("修改007", "review_only", "第一条"),
                RevisionReviewItem("修改007", "review_only", "第二条"),
            ]
        )

        with self.assertRaisesRegex(ValueError, "Duplicate.*修改007"):
            build_marker_plan(request)

    def test_missing_source_uses_longest_unchanged_action_detail(self):
        longest_detail = "  01:59-02:00，删除原句并保留这些标点??\r\n第二行  "
        request = self._request(
            edits=[
                RevisionEdit(
                    op_type="delete",
                    start=118.95,
                    end=120.0,
                    label="修改008 短标签",
                    detail="较短说明",
                    doc_item_id="修改008",
                ),
                RevisionEdit(
                    op_type="delete",
                    start=120.0,
                    end=123.3,
                    label="另一个短标签",
                    detail=longest_detail,
                    doc_item_id="修改008",
                ),
            ],
            review_items=[RevisionReviewItem("修改008", "spoken_delete", "")],
        )

        plan = build_marker_plan(request)

        self.assertEqual(plan[0].source_text, longest_detail)
        self.assertEqual(plan[0].verbatim_status, "unverified_source_unavailable")

    def test_missing_source_prefers_detail_over_longer_summary_label(self):
        detail = "00:51，删除“是吧”"
        request = self._request(
            edits=[
                RevisionEdit(
                    op_type="delete",
                    start=51.0,
                    end=52.0,
                    label="修改003 精准删词和音频修复摘要很长",
                    detail=detail,
                    doc_item_id="修改003",
                )
            ],
            review_items=[RevisionReviewItem("修改003", "spoken_delete", "")],
        )

        plan = build_marker_plan(request)

        self.assertEqual(plan[0].source_text, detail)
        self.assertEqual(plan[0].verbatim_status, "unverified_source_unavailable")

    def test_missing_window_uses_non_blocking_fallback_without_downgrading_source_text(self):
        request = self._request(
            review_items=[
                RevisionReviewItem(
                    item_id="校对009",
                    kind="review_only",
                    source_text="只提供了原始审阅文字",
                )
            ]
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (0.0, 0.8))
        self.assertEqual(plan[0].verbatim_status, "verified")

    def test_verified_source_text_without_timing_remains_verbatim_verified(self):
        source_text = "  00:51, keep punctuation??\r\nsecond line  "
        request = self._request(
            review_items=[
                RevisionReviewItem(
                    item_id="item-without-timing",
                    kind="review_only",
                    source_text=source_text,
                    verbatim_status="verified",
                )
            ]
        )

        plan = build_marker_plan(request)

        self.assertEqual(plan[0].source_text, source_text)
        self.assertEqual(plan[0].verbatim_status, "verified")

    def test_verified_source_text_without_timing_rejects_wrong_saved_marker_text(self):
        source_text = "  00:51, keep punctuation??\r\nsecond line  "
        request = self._request(
            review_items=[
                RevisionReviewItem(
                    item_id="item-without-timing",
                    kind="review_only",
                    source_text=source_text,
                    verbatim_status="verified",
                )
            ]
        )
        plan = build_marker_plan(request)
        content = {
            "tracks": [
                {
                    "name": "Review Marker 1",
                    "type": "text",
                    "segments": [
                        {
                            "id": "segment-1",
                            "material_id": "material-1",
                            "target_timerange": {"start": 0, "duration": 800_000},
                        }
                    ],
                }
            ],
            "materials": {
                "texts": [
                    {
                        "id": "material-1",
                        "content": json.dumps({"text": "wrong summary"}),
                    }
                ]
            },
        }

        result = revision_markers.validate_saved_marker_plan(plan, content)

        self.assertFalse(result["ok"])
        self.assertTrue(any("not verbatim" in error for error in result["errors"]))

    def test_partial_row_times_are_completed_from_matching_actions(self):
        request = self._request(
            edits=[
                RevisionEdit("delete", 12.0, 16.0, "修改010", doc_item_id="修改010"),
                RevisionEdit("delete", 20.0, 26.0, "校对011", doc_item_id="校对011"),
            ],
            review_items=[
                RevisionReviewItem("修改010", "spoken_delete", "保留行起点", start=11.5),
                RevisionReviewItem("校对011", "review_only", "保留行终点", end=25.5),
            ],
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (11.5, 16.0))
        self.assertEqual((plan[1].start, plan[1].end), (20.0, 25.5))

    def test_start_only_row_without_action_preserves_text_verification(self):
        request = self._request(
            review_items=[RevisionReviewItem("修改013", "review_only", "只有起点", start=12.0)]
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (12.0, 12.8))
        self.assertEqual(plan[0].verbatim_status, "verified")

    def test_end_only_row_without_action_preserves_text_verification(self):
        request = self._request(
            review_items=[RevisionReviewItem("校对014", "review_only", "只有终点", end=7.0)]
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (6.2, 7.0))
        self.assertEqual(plan[0].verbatim_status, "verified")

    def test_complete_reversed_row_window_is_normalized(self):
        request = self._request(
            review_items=[
                RevisionReviewItem("修改017", "review_only", "完整倒置窗口", start=25.0, end=20.0)
            ]
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (20.0, 25.0))

    def test_complete_zero_length_row_uses_fallback_duration(self):
        request = self._request(
            review_items=[
                RevisionReviewItem("修改018", "review_only", "完整零长窗口", start=25.0, end=25.0)
            ]
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (25.0, 25.8))

    def test_start_only_row_ignores_action_end_not_after_known_start(self):
        request = self._request(
            edits=[RevisionEdit("delete", 15.0, 20.0, "x", doc_item_id="修改019")],
            review_items=[RevisionReviewItem("修改019", "review_only", "已知起点", start=25.0)],
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (25.0, 25.8))

    def test_end_only_row_ignores_action_start_not_before_known_end(self):
        request = self._request(
            edits=[RevisionEdit("delete", 25.0, 30.0, "x", doc_item_id="校对020")],
            review_items=[RevisionReviewItem("校对020", "review_only", "已知终点", end=20.0)],
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (19.2, 20.0))

    def test_reversed_action_window_is_normalized(self):
        request = self._request(
            edits=[RevisionEdit("delete", 25.0, 20.0, "x", doc_item_id="修改021")],
            review_items=[RevisionReviewItem("修改021", "review_only", "倒置 action")],
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (20.0, 25.0))

    def test_negative_end_only_row_normalizes_to_nonnegative_fallback_window(self):
        request = self._request(
            review_items=[RevisionReviewItem("修改022", "review_only", "", end=-1.0)]
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (0.0, 0.8))

    def test_legacy_reversed_edit_window_is_normalized(self):
        request = self._request(edits=[RevisionEdit("delete", 25.0, 20.0, "legacy reversed")])

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (20.0, 25.0))

    def test_nonfinite_ledger_and_legacy_windows_become_finite(self):
        ledger_request = self._request(
            review_items=[
                RevisionReviewItem(
                    "修改023",
                    "review_only",
                    "nonfinite ledger",
                    start=float("nan"),
                    end=float("inf"),
                )
            ]
        )
        legacy_request = self._request(
            markers=[RevisionMarker("nonfinite legacy", float("nan"), float("inf"))]
        )

        items = [build_marker_plan(ledger_request)[0], build_marker_plan(legacy_request)[0]]

        self.assertEqual(
            [(item.start, item.end) for item in items],
            [(0.0, 0.8), (0.0, 0.8)],
        )
        self.assertTrue(
            all(math.isfinite(value) for item in items for value in (item.start, item.end))
        )

    def test_near_zero_window_uses_fallback_duration(self):
        request = self._request(
            review_items=[
                RevisionReviewItem(
                    "修改024",
                    "review_only",
                    "near zero",
                    start=5.0,
                    end=5.0 + 5e-10,
                )
            ]
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (5.0, 5.8))

    def test_valid_short_window_keeps_original_duration(self):
        request = self._request(
            review_items=[RevisionReviewItem("修改025", "review_only", "short", start=5.0, end=5.1)]
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (5.0, 5.1))

    def test_missing_text_and_time_still_returns_fallback_marker(self):
        request = self._request(review_items=[RevisionReviewItem("校对026", "review_only", "")])

        plan = build_marker_plan(request)

        self.assertEqual(plan[0].source_text, "")
        self.assertEqual((plan[0].start, plan[0].end), (0.0, 0.8))

    def test_action_without_doc_item_id_is_matched_by_review_id_in_text(self):
        request = self._request(
            edits=[
                RevisionEdit(
                    op_type="delete",
                    start=31.0,
                    end=32.5,
                    label="修改012 删除摘要",
                    detail="执行修改012",
                )
            ],
            review_items=[RevisionReviewItem("修改012", "spoken_delete", "原始台账文字")],
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (31.0, 32.5))

    def test_explicit_action_id_wins_over_conflicting_review_id_in_action_text(self):
        request = self._request(
            edits=[
                RevisionEdit(
                    op_type="delete",
                    start=41.0,
                    end=42.5,
                    label="修改016 摘要标签",
                    detail="执行修改016 的文字说明",
                    doc_item_id="修改015",
                )
            ],
            review_items=[
                RevisionReviewItem("修改015", "spoken_delete", "A 的原始台账文字"),
                RevisionReviewItem("修改016", "spoken_delete", "B 的原始台账文字"),
            ],
        )

        plan = build_marker_plan(request)

        self.assertEqual((plan[0].start, plan[0].end), (41.0, 42.5))
        self.assertEqual((plan[1].start, plan[1].end), (0.0, 0.8))

    def test_action_ids_are_indexed_once_for_many_source_rows(self):
        edits = [
            RevisionEdit(
                "delete",
                float(index),
                float(index) + 0.5,
                f"修改{index:03d}",
                doc_item_id=f"修改{index:03d}",
            )
            for index in range(1, 101)
        ]
        review_items = [
            RevisionReviewItem(f"修改{index:03d}", "spoken_delete", f"source row {index}")
            for index in range(1, 101)
        ]
        request = self._request(edits=edits, review_items=review_items)

        with patch.object(
            revision_markers,
            "_extract_action_item_id",
            wraps=revision_markers._extract_action_item_id,
        ) as extract_action_item_id:
            plan = build_marker_plan(request)

        self.assertEqual(len(plan), 100)
        self.assertLessEqual(extract_action_item_id.call_count, 200)

    def test_legacy_plan_and_summary_keep_one_marker_per_explicit_action(self):
        request = self._request(
            edits=[
                RevisionEdit("delete", 0.0, 3.0, "Remove opener"),
                RevisionEdit("replace_audio", 197.0, 205.0, "Replace narration"),
            ],
            markers=[RevisionMarker("Animation check", 220.0, 221.0)],
        )

        plan = build_marker_plan(request)
        summary = build_revision_summary(request)

        self.assertEqual(
            [item.source_text for item in plan],
            ["Animation check", "Remove opener", "Replace narration"],
        )
        self.assertEqual(summary["review_marker_count"], 3)


if __name__ == "__main__":
    unittest.main()
