import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from utils.replacement_timebase import resolve_review_timebases


class ReplacementTimebaseTests(unittest.TestCase):
    def test_replacement_rows_use_local_clock_and_global_timeline(self):
        rows, warnings, unresolved, anchors = resolve_review_timebases(
            [
                {"id": "anchor", "source_text": "07:33-09:32 替换为以下视频", "start": 453, "end": 572},
                {"id": "local", "source_text": "00:05-00:13 删除补录片段", "start": 5, "end": 13},
                {"id": "handoff", "source_text": "02:01-结尾，延长至原视频 09:42", "start": 121, "end": 125.04},
                {"id": "main", "source_text": "09:45 删除主片", "start": 585, "end": 586},
            ],
            project={"media_duration_seconds": 627.48},
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(anchors[0]["timeline_start"], 453.0)
        self.assertEqual(rows[1]["source_role"], "replacement_video")
        self.assertEqual(rows[1]["source_time_range"], [5.0, 13.0])
        self.assertEqual(rows[1]["timeline_time_range"], [458.0, 466.0])
        self.assertEqual(rows[1]["start"], 458.0)
        self.assertEqual(rows[2]["timeline_time_range"], [574.0, 582.0])
        self.assertEqual(rows[3]["source_role"], "main_video")
        self.assertEqual(rows[3]["timeline_time_range"], [585.0, 586.0])
        self.assertFalse(warnings)

    def test_multiple_replacement_sections_are_independent(self):
        rows, _, unresolved, _ = resolve_review_timebases(
            [
                {"id": "a", "source_text": "01:00 替换为以下视频", "start": 60, "end": 65, "replacement_duration_seconds": 20},
                {"id": "a-local", "source_text": "00:02-00:04 删除", "start": 2, "end": 4},
                {"id": "a-back", "source_text": "00:10 回到原视频 01:20", "start": 10, "end": 10.1},
                {"id": "b", "source_text": "03:00 替换为以下视频", "start": 180, "end": 185, "replacement_duration_seconds": 20},
                {"id": "b-local", "source_text": "00:01-00:03 删除", "start": 1, "end": 3},
            ],
            project={"media_duration_seconds": 400},
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(rows[1]["timeline_time_range"], [62.0, 64.0])
        self.assertEqual(rows[2]["timeline_time_range"], [70.0, 80.0])
        self.assertEqual(rows[4]["timeline_time_range"], [181.0, 183.0])

    def test_unanchored_replacement_is_unresolved_and_never_exposes_global_start(self):
        rows, warnings, unresolved, _ = resolve_review_timebases(
            [{"id": "local", "source_role": "replacement_video", "source_text": "00:05-00:08 删除", "start": 5, "end": 8}],
            project={"media_duration_seconds": 100},
        )
        self.assertEqual(unresolved, ["local"])
        self.assertNotIn("start", rows[0])
        self.assertNotIn("end", rows[0])
        self.assertEqual(rows[0]["timebase"]["status"], "unresolved_no_anchor")
        self.assertTrue(any("no executable global range" in warning for warning in warnings))

    def test_explicit_main_role_does_not_inherit_active_replacement(self):
        rows, _, unresolved, _ = resolve_review_timebases(
            [
                {"id": "a", "source_text": "100 替换为以下视频", "start": 100, "end": 105},
                {"id": "main", "source_role": "main_video", "source_text": "01:10 删除主片", "start": 70, "end": 71},
            ],
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(rows[1]["timeline_time_range"], [70.0, 71.0])

    def test_missing_replacement_boundary_fails_closed_for_the_whole_section(self):
        rows, warnings, unresolved, _ = resolve_review_timebases(
            [
                {"id": "anchor", "source_text": "01:00 替换为以下视频", "start": 60, "end": 65},
                {"id": "local-a", "source_text": "00:02-00:04 删除补录内容", "start": 2, "end": 4},
                {"id": "local-b", "source_text": "00:08-00:09 删除补录内容", "start": 8, "end": 9},
            ],
            project={"media_duration_seconds": 100},
        )
        self.assertEqual(unresolved, ["local-a", "local-b"])
        self.assertTrue(all("start" not in row and "timeline_time_range" not in row for row in rows[1:]))
        self.assertTrue(any("no duration or explicit main-video handoff" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
