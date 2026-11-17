"""Recall rules derived from the doc2 (商朝) gold-standard review.

Each positive case mirrors a real spoken deletion that the v9 planner left
label-only; each negative case pins the guard that keeps the rule safe.
"""

import math
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from utils.lite_revision import _authoritative_window_matches_asr  # noqa: E402
from utils.review_audio_precision import (  # noqa: E402
    WavEnergyProbe,
    _acoustic_edge_extension,
    resolve_lite_audio_items,
)


def _source_asr(words):
    return {
        "provider": "volc_asr",
        "resource_id": "volc.bigasr.auc",
        "adapter_version": "test-adapter-v1",
        "input_sha256": "a" * 64,
        "service_job_id": "source-job",
        "service_result_sha256": "b" * 64,
        "words": words,
    }


def _chars(text, start, step=0.1):
    return [
        {
            "text": character,
            "start": round(start + index * step, 6),
            "end": round(start + (index + 1) * step, 6),
        }
        for index, character in enumerate(text)
    ]


def _resolve(source_text, delete, words, duration=900.0, kind="phrase_delete"):
    result = resolve_lite_audio_items(
        [
            {
                "id": "item",
                "kind": kind,
                "source_text": source_text,
                "execution_required": True,
                "evidence": {"delete": delete, "must_keep": []},
            }
        ],
        _source_asr(words),
        source_duration_seconds=duration,
    )
    return result["rows"][0]


class SingleCharacterSubstitutionTests(unittest.TestCase):
    def test_rare_character_misrecognition_inside_long_phrase_resolves(self):
        words = [
            *_chars("青铜", 730.0),
            *_chars("这个鬳就是一种蒸煮器对吧", 732.0),
            *_chars("那个", 735.0),
        ]
        row = _resolve(
            "12：11-12：15，删除“这个鬲就是一种蒸煮器。对吧？”",
            "这个鬲就是一种蒸煮器。对吧？",
            words,
        )
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["match_method"], "time_anchored_character_substitution")
        self.assertEqual(row["asr_match"].get("text_similarity") is not None, True)
        self.assertEqual(row["asr_word_cut_windows"], [[732.0, 733.2]])

    def test_substitution_rejected_for_short_phrase(self):
        words = [*_chars("我们", 9.0), *_chars("张三丰来了", 10.0), *_chars("然后", 12.0)]
        row = _resolve("00:10 删除张山丰来了", "张山丰来了", words, duration=20.0)
        self.assertFalse(row["execution_required"])

    def test_substitution_rejected_at_phrase_edge(self):
        words = [*_chars("前面", 9.0), *_chars("乙二三四五六七八九", 10.0), *_chars("后面", 12.0)]
        row = _resolve("00:10 删除甲二三四五六七八九", "甲二三四五六七八九", words, duration=20.0)
        # The edge mismatch is never absorbed as a substitution; at most the
        # pre-existing single edge-omission rule cuts the recognized remainder.
        self.assertNotEqual(row.get("match_method"), "time_anchored_character_substitution")
        if row["execution_required"]:
            self.assertEqual(row["match_method"], "time_anchored_partial_phrase")
            self.assertEqual(row["asr_word_cut_windows"], [[10.1, 10.9]])

    def test_substitution_must_be_the_only_difference(self):
        words = [
            *_chars("前面", 9.0),
            *_chars("一二乙四五六七八啊九十", 10.0),
            *_chars("后面", 12.0),
        ]
        row = _resolve(
            "00:10 删除一二三四五六七八九十", "一二三四五六七八九十", words, duration=20.0
        )
        self.assertNotEqual(row.get("match_method"), "time_anchored_character_substitution")


class FillerTolerantAnchorTests(unittest.TestCase):
    def test_short_ellipsis_head_with_interjection_resolves(self):
        words = [
            *_chars("说这些", 774.0),
            *_chars("对吧啊最终", 775.9, 0.2),
            *_chars("中间保留原话", 777.0),
            *_chars("同学们有兴趣可以对这个问题进行关注", 789.0),
            *_chars("好", 793.0),
        ]
        row = _resolve(
            "12：55-13：11，删除“对吧，最终……同学们有兴趣可以对这个问题进行关注”",
            "对吧，最终……同学们有兴趣可以对这个问题进行关注",
            words,
            kind="ellipsis_range_delete",
        )
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["match_method"], "ellipsis_anchor_range")
        self.assertEqual(row["asr_word_cut_windows"][0][0], 775.9)
        self.assertEqual(row["asr_word_cut_windows"][-1][1], 790.7)

    def test_interjection_is_never_a_cut_edge(self):
        words = [*_chars("啊对吧", 10.0), *_chars("后面", 11.0)]
        row = _resolve("00:10 删除对吧", "对吧", words, duration=20.0)
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["asr_word_cut_windows"], [[10.1, 10.3]])


class ConnectorInsertionTests(unittest.TestCase):
    def test_spoken_ranhou_inside_long_tail_anchor_resolves(self):
        words = [
            *_chars("那同学们知道", 525.5),
            *_chars("中间保留原话", 527.0, 1.0),
            *_chars("从旧石器时代到新石器时代然后进入到这个青铜时代那么", 541.4),
            *_chars("商代", 547.4),
        ]
        row = _resolve(
            "08：43-09：06，删除“那同学们知道……从旧石器时代到新石器时代，进入到这个青铜时代。那么”",
            "那同学们知道……从旧石器时代到新石器时代，进入到这个青铜时代。那么",
            words,
            kind="ellipsis_range_delete",
        )
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["asr_word_cut_windows"], [[525.5, 543.9]])

    def test_connector_at_candidate_edge_is_not_absorbed(self):
        words = [*_chars("然后", 9.8), *_chars("一二三四五六", 10.0), *_chars("后面", 12.0)]
        row = _resolve("00:10 删除一二三四五六", "一二三四五六", words, duration=20.0)
        self.assertEqual(row["asr_word_cut_windows"], [[10.0, 10.6]])


class EllipsisTailExtensionTests(unittest.TestCase):
    def test_point_timestamp_tail_beyond_window_resolves(self):
        words = [
            *_chars("连续性", 493.2),
            *_chars("对吧文字没有中断", 494.1),
            *_chars("它实际上反映的是整个文明没有中断", 495.7, 0.15),
            *_chars("这是我们华夏文明的一个突出的特征", 498.4, 0.17),
            *_chars("哎", 502.8),
        ]
        row = _resolve(
            "08：13，删除“对吧？文字没有中断，……这是我们华夏文明的一个突出的特征。”",
            "对吧？文字没有中断，……这是我们华夏文明的一个突出的特征。",
            words,
            kind="ellipsis_range_delete",
        )
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["asr_word_cut_windows"][0][0], 494.1)
        self.assertAlmostEqual(row["asr_word_cut_windows"][-1][1], 501.12, places=6)

    def test_ambiguous_extended_tail_stays_label_only(self):
        words = [
            *_chars("开头甲乙丙", 10.0),
            *_chars("中间", 11.0, 2.0),
            *_chars("结尾丁戊己", 20.0),
            *_chars("中间", 21.0, 2.0),
            *_chars("结尾丁戊己", 30.0),
        ]
        row = _resolve(
            "00:10，删除“开头甲乙丙……结尾丁戊己”",
            "开头甲乙丙……结尾丁戊己",
            words,
            duration=60.0,
            kind="ellipsis_range_delete",
        )
        self.assertFalse(row["execution_required"])


class GapPaddingTests(unittest.TestCase):
    def test_padding_stays_inside_adjacent_silence(self):
        words = [
            {"text": "前", "start": 9.0, "end": 9.5},
            {"text": "对", "start": 10.0, "end": 10.2},
            {"text": "吧", "start": 10.2, "end": 10.4},
            {"text": "后", "start": 10.6, "end": 10.8},
        ]
        row = _resolve("00:10 删除对吧", "对吧", words, duration=20.0)
        self.assertEqual(row["asr_word_cut_windows"], [[10.0, 10.4]])
        # start: min(0.20, 0.5 * 0.45); end: min(0.25, 0.2 * 0.45)
        self.assertEqual(row["source_cut_windows"], [[9.8, 10.49]])
        self.assertEqual(row["cut_boundary_padding"], [[0.2, 0.09]])
        self.assertLess(row["source_cut_windows"][0][1], 10.6)
        self.assertGreater(row["source_cut_windows"][0][0], 9.5)

    def test_no_padding_when_words_touch(self):
        words = _chars("前对吧后", 10.0)
        row = _resolve("00:10 删除对吧", "对吧", words, duration=20.0)
        self.assertEqual(row["source_cut_windows"], [[10.1, 10.3]])
        self.assertEqual(row["cut_boundary_padding"], [[0.0, 0.0]])

    def test_validator_accepts_only_recorded_bounded_padding(self):
        refinement = {"status": "anchor_gap", "padding_seconds": [0.2, 0.09]}
        self.assertTrue(_authoritative_window_matches_asr(9.8, 10.49, 10.0, 10.4, refinement))
        self.assertFalse(_authoritative_window_matches_asr(9.8, 10.49, 10.0, 10.4, None))
        self.assertFalse(
            _authoritative_window_matches_asr(
                9.5, 10.4, 10.0, 10.4, {"status": "anchor_gap", "padding_seconds": [0.5, 0.0]}
            )
        )
        self.assertFalse(
            _authoritative_window_matches_asr(
                9.8,
                10.49,
                10.0,
                10.4,
                {"status": "asr_character_edge", "padding_seconds": [0.2, 0.09]},
            )
        )



def _tone_wav(path, bursts, sample_rate=16000, seconds=4.0):
    """Mono PCM16 WAV: ``bursts`` of (start, end) hold a tone, the rest silence."""

    count = int(seconds * sample_rate)
    samples = array("h", [0]) * count
    for start, end in bursts:
        first = int(start * sample_rate)
        last = min(count, int(end * sample_rate))
        for index in range(first, last):
            samples[index] = int(12000 * math.sin(2 * math.pi * 220 * index / sample_rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


class AcousticEdgeExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.wav_path = Path(self.temporary.name) / "alignment.wav"

    def tearDown(self):
        self.temporary.cleanup()

    def test_trailing_filler_the_asr_missed_extends_the_cut(self):
        # "俄" ends at 1.0; an unrecognized "啊" sounds until 1.5, then silence.
        _tone_wav(self.wav_path, [(0.4, 1.0), (1.06, 1.5)])
        words = [
            {"text": "全", "start": 0.4, "end": 0.7},
            {"text": "俄", "start": 0.7, "end": 1.0},
            {"text": "在", "start": 2.6, "end": 2.9},
        ]
        start, end, start_ext, end_ext = _acoustic_edge_extension(
            WavEnergyProbe(self.wav_path), words, 0, 1, 0.4, 1.0, "全俄啊"
        )
        self.assertEqual(start, 0.4)
        self.assertEqual(start_ext, 0.0)
        self.assertGreater(end, 1.4)
        self.assertLess(end, 1.6)
        self.assertAlmostEqual(end_ext, end - 1.0, places=3)

    def test_extension_stops_before_the_next_word(self):
        _tone_wav(self.wav_path, [(0.4, 2.4)])
        words = [
            {"text": "全", "start": 0.4, "end": 0.7},
            {"text": "俄", "start": 0.7, "end": 1.0},
            {"text": "在", "start": 1.2, "end": 1.5},
        ]
        _, end, _, _ = _acoustic_edge_extension(
            WavEnergyProbe(self.wav_path), words, 0, 1, 0.4, 1.0, "全俄啊"
        )
        self.assertLessEqual(end, 1.2 - 0.08 + 1e-6)

    def test_no_extension_without_a_named_edge_filler(self):
        _tone_wav(self.wav_path, [(0.4, 1.5)])
        words = [
            {"text": "全", "start": 0.4, "end": 0.7},
            {"text": "俄", "start": 0.7, "end": 1.0},
            {"text": "在", "start": 2.6, "end": 2.9},
        ]
        self.assertEqual(
            _acoustic_edge_extension(
                WavEnergyProbe(self.wav_path), words, 0, 1, 0.4, 1.0, "全俄"
            ),
            (0.4, 1.0, 0.0, 0.0),
        )

    def test_no_extension_without_a_probe(self):
        words = [{"text": "全", "start": 0.4, "end": 1.0}]
        self.assertEqual(
            _acoustic_edge_extension(None, words, 0, 0, 0.4, 1.0, "全啊"),
            (0.4, 1.0, 0.0, 0.0),
        )

    def test_validator_accepts_recorded_acoustic_extension(self):
        refinement = {
            "status": "acoustic_gap_refined",
            "acoustic_extension_seconds": [0.0, 0.6],
            "padding_seconds": [0.0, 0.09],
        }
        self.assertTrue(
            _authoritative_window_matches_asr(10.0, 10.69, 10.0, 10.0, refinement)
        )
        over_cap = {
            "status": "acoustic_gap_refined",
            "acoustic_extension_seconds": [0.0, 2.0],
            "padding_seconds": [0.0, 0.0],
        }
        self.assertFalse(_authoritative_window_matches_asr(10.0, 12.0, 10.0, 10.0, over_cap))
        missing_record = {
            "status": "acoustic_gap_refined",
            "padding_seconds": [0.0, 0.0],
        }
        self.assertFalse(
            _authoritative_window_matches_asr(10.0, 10.6, 10.0, 10.0, missing_record)
        )


if __name__ == "__main__":
    unittest.main()
