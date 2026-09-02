# ruff: noqa: E402
import json
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from utils.lite_revision import LITE_TRACKS, _validate_lite_audio_timing_sources
from utils.review_audio_precision import (
    CANDIDATE_RENDERER_VERSION,
    REVERSE_ASR_DIAGNOSTIC_PURPOSE,
    _semantic_join_forbidden_patterns,
    alignment_cache_identity,
    apply_audio_plan_to_compiled_payloads,
    apply_reverse_report_to_payloads,
    build_full_candidate_reverse_report,
    build_lite_split_gap_audio_plan,
    candidate_cache_identity,
    canonical_json_sha256,
    downgrade_reverse_asr_failures,
    render_source_aligned_candidate,
    resolve_lite_audio_items,
    reverse_asr_cache_identity,
    source_asr_cache_identity,
    wav_duration_seconds,
)
from utils.revision_models import (
    lite_review_item_execution_required,
    load_review_items_json,
    load_revision_request,
    review_item_execution_status,
)
from utils.revision_validation import (
    _saved_audio_delivery_volume,
    _spoken_reverse_asr_row_evidence_problems,
)

from audio_sound.volc_asr import VolcAsrConfig


def _write_pcm16_wav(path: Path, duration_seconds: float, sample: int = 1200) -> None:
    frame_count = round(duration_seconds * 16_000)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(struct.pack("<h", sample) * frame_count)


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


def _character_words(text, start=0.0, step=0.1):
    return [
        {
            "text": character,
            "start": round(start + index * step, 6),
            "end": round(start + (index + 1) * step, 6),
        }
        for index, character in enumerate(text)
    ]


class ReviewAudioPrecisionTests(unittest.TestCase):
    def test_source_asr_exact_phrase_resolves_authoritative_cut(self):
        source_text = "00:01 删除“口头赘词”，保留上下文。"
        result = resolve_lite_audio_items(
            [
                {
                    "id": "audio-1",
                    "kind": "spoken_delete",
                    "source_text": source_text,
                    "start": 0.8,
                    "end": 1.8,
                    "execution_required": True,
                    "evidence": {"delete": "口头赘词", "must_keep": ["保留"]},
                }
            ],
            _source_asr(
                [
                    {"text": "保留", "start": 0.2, "end": 0.6},
                    {"text": "口头", "start": 1.0, "end": 1.2},
                    {"text": "赘词", "start": 1.2, "end": 1.5},
                    {"text": "下文", "start": 1.8, "end": 2.2},
                ]
            ),
            source_duration_seconds=3.0,
        )

        row = result["rows"][0]
        self.assertTrue(row["execution_required"])
        self.assertEqual((row["start"], row["end"]), (1.0, 1.5))
        self.assertEqual(row["source_text"], source_text)
        self.assertTrue(row["asr_alignment"]["authoritative_cut_boundary"])
        self.assertEqual(
            [match["text"] for match in row["asr_alignment"]["matches"]],
            ["口头", "赘词"],
        )

    def test_stale_unresolved_audio_retries_but_lite_policy_remains_label_only(self):
        source_asr = _source_asr(
            [
                {"text": "前文", "start": 0.4, "end": 0.8},
                {"text": "测试", "start": 1.0, "end": 1.4},
                {"text": "后文", "start": 1.6, "end": 2.0},
            ]
        )
        common = {
            "kind": "spoken_delete",
            "source_text": "00:01 删除“测试”",
            "start": 0.8,
            "end": 1.6,
            "execution_required": False,
            "evidence": {"delete": "测试"},
        }
        review_items = [
            {
                **common,
                "id": "retry",
                "execution_status": "label_only_unresolved",
                "evidence": {
                    "delete": "测试",
                    "execution_status": "label_only_unresolved",
                },
                "validation": {"layers": [{"status": "LABEL-ONLY-UNRESOLVED"}]},
            },
            {
                **common,
                "id": "policy",
                "execution_status": "label_only_lite_policy",
                "evidence": {
                    "delete": "测试",
                    "execution_status": "label_only_lite_policy",
                },
            },
        ]
        result = resolve_lite_audio_items(
            review_items,
            source_asr,
            source_duration_seconds=3.0,
        )

        rows = {row["item_id"]: row for row in result["rows"]}
        self.assertTrue(rows["retry"]["execution_required"])
        self.assertEqual(rows["retry"]["execution_status"], "asr_resolved")
        self.assertTrue(rows["retry"]["asr_alignment"]["authoritative_cut_boundary"])
        self.assertFalse(rows["policy"]["execution_required"])
        self.assertEqual(rows["policy"]["execution_status"], "label_only_lite_policy")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_audio = root / "source.wav"
            _write_pcm16_wav(source_audio, 3.0)
            updated_request, updated_ledger = apply_audio_plan_to_compiled_payloads(
                {
                    "workflow_mode": "lite",
                    "project": {
                        "draft_name": "RetryableAudio",
                        "source_video": "C:/media/source.mp4",
                    },
                    "review_items": review_items,
                    "edits": [],
                },
                {"review_items": review_items},
                result,
                audio_delivery_plan={
                    "mode": "segmented",
                    "pending": False,
                    "segments": [
                        {
                            "id": "source-1",
                            "role": "source",
                            "asset_path": str(source_audio),
                            "track_name": "Separated Source Audio",
                            "source_start": 0.0,
                            "timeline_start": 0.0,
                            "duration": 3.0,
                        }
                    ],
                },
                source_audio_path=source_audio,
                candidate_audio_path=None,
            )
            request_path = root / "request.json"
            ledger_path = root / "ledger.json"
            request_path.write_text(
                json.dumps(updated_request, ensure_ascii=False), encoding="utf-8"
            )
            ledger_path.write_text(json.dumps(updated_ledger, ensure_ascii=False), encoding="utf-8")
            loaded_request = load_revision_request(str(request_path))
            loaded_ledger = load_review_items_json(str(ledger_path))

        loaded_request_items = {item.item_id: item for item in loaded_request.review_items}
        loaded_ledger_items = {item.item_id: item for item in loaded_ledger}
        for items in (loaded_request_items, loaded_ledger_items):
            self.assertTrue(lite_review_item_execution_required(items["retry"]))
            self.assertEqual(review_item_execution_status(items["retry"]), "asr_resolved")
            self.assertFalse(lite_review_item_execution_required(items["policy"]))
            self.assertEqual(
                review_item_execution_status(items["policy"]),
                "label_only_lite_policy",
            )

    def test_unresolved_replacement_timebase_never_retries_against_main_video_asr(self):
        result = resolve_lite_audio_items(
            [
                {
                    "id": "replacement-local",
                    "kind": "phrase_delete",
                    "source_text": "00：05-00：13，删除“测试”",
                    "start": 451.0,
                    "end": 452.0,
                    "execution_required": True,
                    "execution_status": "asr_resolved",
                    "evidence": {
                        "delete": "测试",
                        "review_search_hint_seconds": 5.0,
                        "review_timestamp_parse": "range",
                        "review_timestamp_role": "search_hint",
                        "timebase": {
                            "kind": "replacement_local",
                            "offset_seconds": 453.0,
                            "status": "unresolved_missing_local_range",
                        },
                    },
                }
            ],
            _source_asr(
                [
                    {"text": "测试", "start": 451.0, "end": 451.4},
                    {"text": "后文", "start": 451.5, "end": 451.9},
                ]
            ),
            source_duration_seconds=627.589002,
        )

        row = result["rows"][0]
        self.assertFalse(row["execution_required"])
        self.assertEqual(row["execution_status"], "label_only_unresolved")
        self.assertEqual(row["resolved_time"], 5.0)
        self.assertEqual(row["reason"], "unresolved_missing_local_range")
        self.assertEqual(result["executable_cuts"], [])
        self.assertEqual(result["unresolved_item_ids"], ["replacement-local"])

    def test_unresolved_replacement_is_removed_from_rebuilt_edits_and_a2_plan(self):
        review_items = [
            {
                "id": "replacement-local",
                "kind": "phrase_delete",
                "source_text": "00：01，删除“误匹配”",
                "start": 1.0,
                "execution_required": True,
                "execution_status": "asr_resolved",
                "evidence": {
                    "delete": "误匹配",
                    "review_search_hint_seconds": 1.0,
                    "review_timestamp_parse": "point",
                    "review_timestamp_role": "search_hint",
                    "timebase": {
                        "kind": "replacement_local",
                        "offset_seconds": 1.0,
                        "status": "unresolved_missing_local_range",
                    },
                },
            },
            {
                "id": "valid-cut",
                "kind": "phrase_delete",
                "source_text": "00：02，删除“有效切词”",
                "start": 1.8,
                "end": 2.8,
                "execution_required": True,
                "evidence": {"delete": "有效切词"},
            },
        ]
        cut_plan = resolve_lite_audio_items(
            review_items,
            _source_asr(
                [
                    {"text": "误匹配", "start": 1.0, "end": 1.4},
                    {"text": "有效", "start": 2.0, "end": 2.2},
                    {"text": "切词", "start": 2.2, "end": 2.5},
                ]
            ),
            source_duration_seconds=3.0,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            candidate = root / "candidate.wav"
            _write_pcm16_wav(source, 3.0)
            _write_pcm16_wav(candidate, 3.0)
            delivery_plan = build_lite_split_gap_audio_plan(
                cut_plan,
                source_audio_path=source,
                candidate_audio_path=candidate,
            )
            request, _ledger = apply_audio_plan_to_compiled_payloads(
                {
                    "workflow_mode": "lite",
                    "project": {},
                    "review_items": review_items,
                    "edits": [
                        {"type": "delete", "doc_item_id": "replacement-local"},
                        {"type": "delete", "doc_item_id": "valid-cut"},
                    ],
                },
                {"review_items": review_items},
                cut_plan,
                audio_delivery_plan=delivery_plan,
                source_audio_path=source,
                candidate_audio_path=candidate,
            )

        a2_ids = [
            row["doc_item_id"]
            for row in delivery_plan["segments"]
            if row["track_name"] == LITE_TRACKS["reused_audio"]
        ]
        self.assertEqual(a2_ids, ["valid-cut"])
        self.assertEqual(
            [edit["doc_item_id"] for edit in request["edits"]],
            ["valid-cut"],
        )
        by_id = {item["id"]: item for item in request["review_items"]}
        self.assertFalse(by_id["replacement-local"]["execution_required"])
        self.assertEqual(
            by_id["replacement-local"]["execution_status"],
            "label_only_unresolved",
        )

    def test_reverse_asr_fallback_downgrades_only_attributable_failures(self):
        alignment = {
            "status": "pass",
            "authoritative_timing": True,
            "authoritative_cut_boundary": True,
            "resolved_cut_window": [2.0, 2.4],
        }
        cut_plan = {
            "rows": [
                {
                    "item_id": "keep-cut",
                    "execution_required": True,
                    "execution_status": "asr_resolved",
                    "start": 1.0,
                    "end": 1.4,
                    "asr_alignment": dict(alignment),
                },
                {
                    "item_id": "failed-cut",
                    "execution_required": True,
                    "execution_status": "asr_resolved",
                    "source_text": "00:05 删除失败词",
                    "review_label_time": 5.0,
                    "start": 2.0,
                    "end": 2.4,
                    "asr_alignment": dict(alignment),
                },
                {
                    "item_id": "already-label",
                    "execution_required": False,
                    "execution_status": "label_only_unresolved",
                    "resolved_time": 3.0,
                },
            ],
            "executable_cuts": [
                {"item_id": "keep-cut", "start": 1.0, "end": 1.4},
                {"item_id": "failed-cut", "start": 2.0, "end": 2.4},
            ],
            "unresolved_item_ids": ["already-label"],
        }

        updated = downgrade_reverse_asr_failures(cut_plan, ["failed-cut"])
        rows = {row["item_id"]: row for row in updated["rows"]}
        self.assertTrue(rows["keep-cut"]["execution_required"])
        self.assertTrue(rows["keep-cut"]["asr_alignment"]["authoritative_cut_boundary"])
        self.assertFalse(rows["failed-cut"]["execution_required"])
        self.assertEqual(rows["failed-cut"]["execution_status"], "label_only_unresolved")
        self.assertEqual(rows["failed-cut"]["rejected_cut_window"], [2.0, 2.4])
        self.assertEqual(rows["failed-cut"]["resolved_time"], 5.0)
        self.assertEqual(
            rows["failed-cut"]["timing_source"],
            "review_timestamp_fallback",
        )
        self.assertFalse(rows["failed-cut"]["asr_alignment"]["authoritative_timing"])
        self.assertFalse(rows["failed-cut"]["asr_alignment"]["authoritative_cut_boundary"])
        self.assertNotIn("start", rows["failed-cut"])
        self.assertEqual([row["item_id"] for row in updated["executable_cuts"]], ["keep-cut"])
        self.assertEqual(updated["unresolved_item_ids"], ["already-label", "failed-cut"])
        with self.assertRaisesRegex(ValueError, "non-executable"):
            downgrade_reverse_asr_failures(cut_plan, ["already-label"])

    def test_ambiguous_phrase_uses_review_timestamp_label(self):
        result = resolve_lite_audio_items(
            [
                {
                    "id": "ambiguous",
                    "kind": "spoken_delete",
                    "source_text": "00:05 删除重复词",
                    "start": 4.5,
                    "end": 6.5,
                    "execution_required": True,
                    "evidence": {"delete": "重复词"},
                }
            ],
            _source_asr(
                [
                    {"text": "重复词", "start": 4.0, "end": 4.4},
                    {"text": "中间", "start": 5.3, "end": 5.7},
                    {"text": "重复词", "start": 6.6, "end": 7.0},
                ]
            ),
            source_duration_seconds=8.0,
        )

        row = result["rows"][0]
        self.assertFalse(row["execution_required"])
        self.assertEqual(row["execution_status"], "label_only_unresolved")
        self.assertEqual(row["reason"], "ambiguous_near_anchor")
        self.assertEqual(row["resolved_time"], 5.0)
        self.assertEqual(row["timing_source"], "review_timestamp_fallback")
        self.assertIsNone(row["asr_alignment"])
        self.assertEqual(result["executable_cuts"], [])

    def test_source_asr_conservative_fuzzy_matching_recovers_real_transcript_variants(self):
        cases = [
            {
                "name": "asr_inserts_spoken_filler",
                "kind": "phrase_delete",
                "source_text": "00:10-00:14 删除第二场战争应该说第一场战争只是牛刀小试",
                "delete": "第二场战争应该说第一场战争只是牛刀小试",
                "transcript": "第二场战争啊应该说第一场战争只是牛刀小试",
                "start": 10.5,
                "method": "conservative_fuzzy_phrase",
            },
            {
                "name": "asr_omits_internal_filler",
                "kind": "phrase_delete",
                "source_text": "00:20 删除在这张呃画上",
                "delete": "在这张呃画上",
                "transcript": "在这张画上",
                "start": 19.8,
                "method": "conservative_fuzzy_phrase",
            },
            {
                "name": "long_sentence_contains_extra_filler",
                "kind": "phrase_delete",
                "source_text": "00:30-00:38 删除我们看到的这个近代的这个意大利统一的过程的完成就是在这个时候",
                "delete": "我们看到的这个近代的这个意大利统一的过程的完成就是在这个时候",
                "transcript": "我们看到的啊这个近代的这个意大利统一的过程的完成就是在这个时候",
                "start": 30.4,
                "method": "conservative_fuzzy_phrase",
            },
            {
                "name": "pronoun_equivalence_in_ellipsis_anchor",
                "kind": "ellipsis_range_delete",
                "source_text": "00:40-00:48 删除而且它大概也宣告……德国会占有很显赫的位置",
                "delete": "而且它大概也宣告……德国会占有很显赫的位置",
                "transcript": "而且他大概也宣告中间保留原话德国会占有很显赫的位置",
                "start": 40.2,
                "method": "ellipsis_anchor_range",
            },
            {
                "name": "ellipsis_anchor_missing_terminal_character",
                "kind": "ellipsis_range_delete",
                "source_text": "00:50-00:56 删除魏蜀吴……三国鼎立",
                "delete": "魏蜀吴……三国鼎立",
                "transcript": "魏蜀中间原话三国鼎立",
                "start": 50.2,
                "method": "ellipsis_anchor_range",
                "omitted_review_text": "吴",
            },
            {
                "name": "continuous_spoken_stutter",
                "kind": "phrase_delete",
                "source_text": "01:00 删除黄河对不起啊",
                "delete": "黄河对不起啊",
                "transcript": "黄河对对不起啊",
                "start": 60.1,
                "method": "conservative_fuzzy_phrase",
                "extra_asr_text": "对",
            },
            {
                "name": "single_chinese_digit_matches_itn_digit",
                "kind": "phrase_delete",
                "source_text": "01:10 删除那五个",
                "delete": "那五个",
                "transcript": "那5个",
                "start": 70.1,
                "method": "normalized_equivalent_phrase",
            },
        ]

        for case in cases:
            with self.subTest(case["name"]):
                words = _character_words(case["transcript"], case["start"])
                result = resolve_lite_audio_items(
                    [
                        {
                            "id": case["name"],
                            "kind": case["kind"],
                            "source_text": case["source_text"],
                            "execution_required": True,
                            "evidence": {"delete": case["delete"], "must_keep": []},
                        }
                    ],
                    _source_asr(words),
                    source_duration_seconds=90.0,
                )

                row = result["rows"][0]
                self.assertTrue(row["execution_required"])
                self.assertEqual(row["match_method"], case["method"])
                self.assertEqual(row["start"], words[0]["start"])
                self.assertEqual(row["end"], words[-1]["end"])
                self.assertTrue(row["asr_alignment"]["authoritative_cut_boundary"])
                if case.get("omitted_review_text"):
                    self.assertEqual(
                        row["asr_match"]["omitted_review_text"],
                        case["omitted_review_text"],
                    )
                if case.get("extra_asr_text"):
                    self.assertEqual(
                        row["asr_match"]["extra_asr_text"],
                        case["extra_asr_text"],
                    )

    def test_source_asr_time_anchored_partial_match_cuts_recognized_remainder(self):
        words = _character_words("解决不了", 10.0)
        result = resolve_lite_audio_items(
            [
                {
                    "id": "missing-leading-character",
                    "kind": "phrase_delete",
                    "source_text": "00:10 删除它解决不了",
                    "execution_required": True,
                    "evidence": {"delete": "它解决不了"},
                }
            ],
            _source_asr(words),
            source_duration_seconds=20.0,
        )

        row = result["rows"][0]
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["match_method"], "time_anchored_partial_phrase")
        self.assertEqual(row["resolved_delete"], "解决不了")
        self.assertEqual(row["asr_match"]["omitted_review_text"], "它")
        self.assertEqual(row["asr_match"]["keyword_coverage"], 0.8)
        self.assertEqual(row["start"], words[0]["start"])
        self.assertEqual(row["end"], words[-1]["end"])
        self.assertTrue(row["asr_alignment"]["authoritative_cut_boundary"])

    def test_source_asr_allows_whitelisted_internal_insertion_near_review_time(self):
        words = _character_words("掌握这个过程的时候", 97.2, 0.1)
        result = resolve_lite_audio_items(
            [
                {
                    "id": "internal-insertion",
                    "kind": "phrase_delete",
                    "source_text": "01:38 删除掌握过程的时候",
                    "execution_required": True,
                    "evidence": {"delete": "掌握过程的时候", "must_keep": []},
                }
            ],
            _source_asr(words),
            source_duration_seconds=110.0,
        )

        row = result["rows"][0]
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["match_method"], "time_anchored_internal_insertion")
        self.assertEqual(row["asr_match"]["extra_asr_text"], "这个")
        self.assertEqual((row["start"], row["end"]), (words[0]["start"], words[-1]["end"]))

    def test_ellipsis_terminal_omission_competes_with_earlier_exact_anchor(self):
        words = [
            *_character_words("开头", 117.0, 0.1),
            *_character_words("魏蜀吴", 118.0, 0.1),
            *_character_words("魏蜀", 125.3, 0.16),
        ]
        result = resolve_lite_audio_items(
            [
                {
                    "id": "near-terminal-omission",
                    "kind": "ellipsis_range_delete",
                    "source_text": "01:57-02:06 删除开头……魏蜀吴",
                    "execution_required": True,
                    "evidence": {"delete": "开头……魏蜀吴", "must_keep": []},
                }
            ],
            _source_asr(words),
            source_duration_seconds=130.0,
        )

        row = result["rows"][0]
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["match_method"], "ellipsis_anchor_range")
        self.assertEqual(row["end"], words[-1]["end"])
        self.assertEqual(row["asr_match"]["omitted_review_text"], "吴")

    def test_source_asr_uses_nearest_word_span_without_joining_repeat(self):
        first = _character_words("哎就有这么个地方叫德意志", 54.45, 0.14)
        second = _character_words("哎就有这么个地方叫德意志", 57.51, 0.14)
        tail = _character_words("对吧", 59.75, 0.2)
        source_asr = _source_asr(first + second + tail)
        result = resolve_lite_audio_items(
            [
                {
                    "id": "nearby-repeat",
                    "kind": "phrase_delete",
                    "source_text": ("00:53-00:57 删除哎就是有这么一个地方叫德意志，对吧"),
                    "execution_required": True,
                    "evidence": {
                        "delete": "哎就是有这么一个地方叫德意志，对吧",
                        "must_keep": [],
                    },
                },
                {
                    "id": "separate-tail",
                    "kind": "phrase_delete",
                    "source_text": "00:59 删除对吧",
                    "execution_required": True,
                    "evidence": {"delete": "对吧", "must_keep": []},
                },
            ],
            source_asr,
            source_duration_seconds=70.0,
        )

        rows = {row["item_id"]: row for row in result["rows"]}
        row = rows["nearby-repeat"]
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["match_method"], "time_anchored_fuzzy_phrase_nearest_anchor")
        self.assertEqual(row["resolved_delete"], "哎就有这么个地方叫德意志")
        self.assertEqual(row["asr_match"]["omitted_review_text"], "是一对吧")
        self.assertEqual(row["start"], first[0]["start"])
        self.assertEqual(row["end"], first[-1]["end"])
        tail_row = rows["separate-tail"]
        self.assertTrue(tail_row["execution_required"])
        self.assertEqual(tail_row["start"], tail[0]["start"])
        self.assertEqual(tail_row["end"], tail[-1]["end"])

    def test_source_asr_rejects_short_multi_omission_and_content_replacement(self):
        cases = [
            ("two-missing", "它能解决不了", "解决不了"),
            ("content-replacement", "它解决得了", "解决不了"),
        ]
        for item_id, delete, transcript in cases:
            with self.subTest(item_id):
                result = resolve_lite_audio_items(
                    [
                        {
                            "id": item_id,
                            "kind": "phrase_delete",
                            "source_text": f"00:10 删除{delete}",
                            "execution_required": True,
                            "evidence": {"delete": delete},
                        }
                    ],
                    _source_asr(_character_words(transcript, 10.0)),
                    source_duration_seconds=20.0,
                )

                row = result["rows"][0]
                self.assertFalse(row["execution_required"])
                self.assertEqual(row["reason"], "phrase_not_found")

    def test_source_asr_keeps_supported_pause_inside_contiguous_word_span(self):
        words = [
            {"text": "萨", "start": 688.55, "end": 688.59},
            {"text": "丁", "start": 688.75, "end": 688.79},
            {"text": "哎", "start": 690.07, "end": 690.15},
        ]
        source_asr = _source_asr(words)
        result = resolve_lite_audio_items(
            [
                {
                    "id": "same-utterance-pause",
                    "kind": "phrase_delete",
                    "source_text": "11:28 删除萨丁哎",
                    "execution_required": True,
                    "evidence": {"delete": "萨丁哎"},
                }
            ],
            source_asr,
            source_duration_seconds=700.0,
        )

        row = result["rows"][0]
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["start"], 688.55)
        self.assertEqual(row["end"], 690.15)

    def test_source_asr_does_not_join_words_across_separate_utterances(self):
        words = _character_words("甲乙", 10.0) + _character_words("丙丁戊", 12.0)
        result = resolve_lite_audio_items(
            [
                {
                    "id": "cross-utterance",
                    "kind": "phrase_delete",
                    "source_text": "00:11 删除甲乙丙丁戊",
                    "execution_required": True,
                    "evidence": {"delete": "甲乙丙丁戊"},
                }
            ],
            _source_asr(words),
            source_duration_seconds=20.0,
        )

        row = result["rows"][0]
        self.assertFalse(row["execution_required"])
        self.assertEqual(row["reason"], "phrase_not_found")
        self.assertEqual(row["timing_source"], "review_timestamp_fallback")

    def test_source_asr_never_promotes_remote_occurrence(self):
        result = resolve_lite_audio_items(
            [
                {
                    "id": "remote-occurrence",
                    "kind": "phrase_delete",
                    "source_text": "00:10 删除远处词语",
                    "execution_required": True,
                    "evidence": {"delete": "远处词语"},
                }
            ],
            _source_asr(_character_words("远处词语", 1.0)),
            source_duration_seconds=20.0,
        )

        row = result["rows"][0]
        self.assertFalse(row["execution_required"])
        self.assertEqual(row["reason"], "phrase_not_found")
        self.assertEqual(row["resolved_time"], 10.0)
        self.assertEqual(row["timing_source"], "review_timestamp_fallback")

    def test_automatic_must_keep_context_excludes_other_delete_windows(self):
        words = _character_words("保留目标一后删留下", 1.0)
        result = resolve_lite_audio_items(
            [
                {
                    "id": "first",
                    "kind": "phrase_delete",
                    "source_text": "00:01 删除目标一",
                    "start": 1.1,
                    "end": 1.8,
                    "execution_required": True,
                    "evidence": {"delete": "目标一"},
                },
                {
                    "id": "second",
                    "kind": "phrase_delete",
                    "source_text": "00:01 删除后删",
                    "start": 1.3,
                    "end": 2.0,
                    "execution_required": True,
                    "evidence": {"delete": "后删"},
                },
            ],
            _source_asr(words),
            source_duration_seconds=3.0,
        )

        rows = {row["item_id"]: row for row in result["rows"]}
        self.assertEqual(rows["first"]["must_keep"], ["保留"])
        self.assertEqual(rows["second"]["must_keep"], ["留下"])

    def test_apply_audio_plan_touches_only_asr_items_and_keeps_source_text_exact(self):
        audio_text = "00:05 这里的读音暂不自动修，原文标记。\n第二行保持。"
        visual_text = "00:07 添加给定图片"
        review_items = [
            {
                "id": "audio-label",
                "kind": "pronunciation_repair",
                "source_text": audio_text,
                "start": 5.0,
                "execution_required": True,
            },
            {
                "id": "visual-exec",
                "kind": "visual_overlay",
                "source_text": visual_text,
                "start": 7.0,
                "execution_required": True,
                "evidence": {"asset_paths": ["C:/media/overlay.png"]},
            },
        ]
        cut_plan = resolve_lite_audio_items(
            review_items,
            _source_asr(
                [
                    {"text": "前文", "start": 4.0, "end": 4.2},
                    {"text": "读音", "start": 5.25, "end": 5.5},
                    {"text": "后文", "start": 5.8, "end": 6.0},
                ]
            ),
            source_duration_seconds=10.0,
        )
        request = {
            "project": {"source_video": "C:/media/source.mp4"},
            "review_items": review_items,
            "edits": [
                {
                    "type": "visual_overlay",
                    "doc_item_id": "visual-exec",
                    "source_kind": "visual_overlay",
                    "start": 7.0,
                    "end": 8.0,
                    "asset_paths": ["C:/media/overlay.png"],
                }
            ],
            "pause_adjustments": [{"item_id": "audio-label", "source_time": 5.0, "duration": 1.0}],
        }
        ledger = {"review_items": json.loads(json.dumps(review_items))}

        with tempfile.TemporaryDirectory() as temp_dir:
            source_audio = Path(temp_dir) / "source.wav"
            _write_pcm16_wav(source_audio, 10.0)
            updated, updated_ledger = apply_audio_plan_to_compiled_payloads(
                request,
                ledger,
                cut_plan,
                audio_delivery_plan={"mode": "legacy"},
                source_audio_path=source_audio,
                candidate_audio_path=None,
            )

        by_id = {item["id"]: item for item in updated["review_items"]}
        ledger_by_id = {item["id"]: item for item in updated_ledger["review_items"]}
        self.assertEqual(by_id["audio-label"]["source_text"], audio_text)
        self.assertEqual(ledger_by_id["audio-label"]["source_text"], audio_text)
        self.assertEqual(by_id["audio-label"]["execution_status"], "label_only_unresolved")
        self.assertEqual(by_id["audio-label"]["start"], 5.0)
        self.assertEqual(
            by_id["audio-label"]["evidence"]["resolved_time"],
            5.0,
        )
        self.assertEqual(
            by_id["audio-label"]["evidence"]["timing_source"],
            "review_timestamp_fallback",
        )
        self.assertNotIn("asr_alignment", by_id["audio-label"]["evidence"])
        self.assertTrue(by_id["visual-exec"]["execution_required"])
        self.assertEqual(by_id["visual-exec"]["evidence"], review_items[1]["evidence"])
        self.assertEqual([edit["doc_item_id"] for edit in updated["edits"]], ["visual-exec"])
        self.assertEqual(updated["pause_adjustments"], [])

    def test_candidate_keeps_source_duration_and_silences_only_delete_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.wav"
            candidate = Path(temp_dir) / "candidate.wav"
            _write_pcm16_wav(source, 2.0)
            result = render_source_aligned_candidate(
                source,
                candidate,
                delete_windows=[{"item_id": "cut", "start": 0.5, "end": 1.0}],
            )

            self.assertEqual(wav_duration_seconds(candidate), wav_duration_seconds(source))
            self.assertEqual(result["duration_seconds"], 2.0)
            self.assertEqual(result["source_duration_seconds"], 2.0)
            self.assertEqual(result["candidate_duration_seconds"], 2.0)
            self.assertTrue(result["source_aligned"])
            self.assertTrue(result["duration_matches_source"])
            self.assertEqual(result["purpose"], REVERSE_ASR_DIAGNOSTIC_PURPOSE)
            self.assertEqual(result["role"], REVERSE_ASR_DIAGNOSTIC_PURPOSE)
            self.assertFalse(result["delivery_eligible"])
            with wave.open(str(candidate), "rb") as rendered:
                rendered.setpos(round(0.6 * 16_000))
                silent = rendered.readframes(1)
                rendered.setpos(round(1.2 * 16_000))
                audible = rendered.readframes(1)
            self.assertEqual(silent, b"\0\0")
            self.assertNotEqual(audible, b"\0\0")

    def test_reverse_report_cannot_promote_diagnostic_candidate_to_delivery(self):
        request = {
            "processed_audio": {
                "candidate_audio_delivery_eligible": True,
                "candidate_audio_purpose": "final_delivery",
                "candidate_audio_role": "replacement",
                "candidate_audio_renderer_version": "old-renderer",
            },
            "review_items": [],
        }
        ledger = {"review_items": []}
        report = {
            "candidate_audio_sha256": "a" * 64,
            "audio_delivery_plan_sha256": "b" * 64,
            "candidate_audio_delivery_eligible": True,
            "candidate_audio_purpose": "final_delivery",
            "candidate_audio_role": "replacement",
            "candidate_audio_renderer_version": "old-renderer",
            "candidate_audio_source_aligned": True,
            "candidate_audio_source_duration_seconds": 3.0,
            "candidate_audio_duration_seconds": 3.0,
            "candidate_audio_duration_matches_source": True,
            "unresolved_ids": [],
            "rows": [],
        }
        updated, _updated_ledger = apply_reverse_report_to_payloads(
            request,
            ledger,
            report,
            report_path="C:/qa/reverse-report.json",
        )
        processed = updated["processed_audio"]
        self.assertEqual(processed["candidate_audio_purpose"], REVERSE_ASR_DIAGNOSTIC_PURPOSE)
        self.assertEqual(processed["candidate_audio_role"], REVERSE_ASR_DIAGNOSTIC_PURPOSE)
        self.assertFalse(processed["candidate_audio_delivery_eligible"])
        self.assertEqual(processed["candidate_audio_renderer_version"], CANDIDATE_RENDERER_VERSION)

    def test_item_level_label_only_asr_passes_without_edit_and_rejects_pause_adjustment(self):
        alignment = {
            "status": "pass",
            "granularity": "word",
            "provider": "volc_asr",
            "resource_id": "volc.bigasr.auc",
            "adapter_version": "test-adapter-v1",
            "input_sha256": "a" * 64,
            "authoritative_cut_boundary": False,
            "matches": [{"text": "读音", "start": 6.25, "end": 6.45}],
            "resolved_time": 6.25,
        }
        payload = {
            "workflow_mode": "lite",
            "project": {
                "draft_name": "LabelOnlyNoEdit",
                "source_video": "C:/media/source.mp4",
            },
            "review_items": [
                {
                    "id": "audio-label",
                    "kind": "pronunciation_repair",
                    "source_text": "02:00 读音问题",
                    "start": 2.0,
                    "execution_required": False,
                    "execution_status": "label_only_unresolved",
                    "evidence": {
                        "execution_status": "label_only_unresolved",
                        "review_timestamp_role": "search_hint",
                        "resolved_time": 6.25,
                        "asr_alignment": alignment,
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            request_path = Path(temp_dir) / "request.json"
            request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            request = load_revision_request(str(request_path))
            _validate_lite_audio_timing_sources(request, None)

            payload["pause_adjustments"] = [
                {
                    "item_id": "audio-label",
                    "source_time": 6.25,
                    "duration": 1.0,
                    "frame_path": "C:/media/frame.png",
                }
            ]
            request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not create pause_adjustments"):
                _validate_lite_audio_timing_sources(load_revision_request(str(request_path)), None)

    def test_a1_complement_and_independent_a2_windows_keep_parser_writer_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.wav"
            candidate = Path(temp_dir) / "candidate.wav"
            _write_pcm16_wav(source, 3.0)
            _write_pcm16_wav(candidate, 3.0)
            plan = build_lite_split_gap_audio_plan(
                {
                    "source_duration_seconds": 3.0,
                    "executable_cuts": [
                        {"item_id": "cut-a", "start": 0.5, "end": 1.0},
                        {"item_id": "cut-b", "start": 1.0, "end": 1.25},
                    ],
                },
                source_audio_path=source,
                candidate_audio_path=candidate,
            )

        a1 = [row for row in plan["segments"] if row["role"] == "source"]
        a2 = [row for row in plan["segments"] if row["role"] == "reference"]
        self.assertEqual(
            {row["track_name"] for row in a1},
            {LITE_TRACKS["source_audio"]},
        )
        self.assertEqual(
            {row["track_name"] for row in a2},
            {LITE_TRACKS["reused_audio"]},
        )
        self.assertEqual(LITE_TRACKS["source_audio"], "Separated Source Audio")
        self.assertEqual(
            [(row["source_start"], row["duration"]) for row in a1],
            [(0.0, 0.5), (1.25, 1.75)],
        )
        self.assertEqual(
            [(row["doc_item_id"], row["source_start"], row["duration"]) for row in a2],
            [("cut-a", 0.5, 0.5), ("cut-b", 1.0, 0.25)],
        )
        self.assertTrue(plan["lite_a2_audible"])
        self.assertEqual([row["volume"] for row in a2], [1.0, 1.0])
        lite_request = SimpleNamespace(workflow_mode="lite", lite_cut_layout="split_gap")
        saved_volumes = [
            _saved_audio_delivery_volume(lite_request, SimpleNamespace(**row)) for row in a2
        ]
        self.assertEqual(saved_volumes, [1.0, 1.0])

    def test_pcm_duration_quantization_allows_one_frame_but_rejects_two(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            candidate = root / "candidate.wav"
            _write_pcm16_wav(source, 1.0)
            _write_pcm16_wav(candidate, 1.0)

            def build_evidence(source_duration_seconds):
                cut_plan = {
                    "source_duration_seconds": source_duration_seconds,
                    "rows": [],
                    "executable_cuts": [{"item_id": "cut", "start": 0.25, "end": 0.5}],
                }
                delivery_plan = build_lite_split_gap_audio_plan(
                    cut_plan,
                    source_audio_path=source,
                    candidate_audio_path=candidate,
                )
                request, ledger = apply_audio_plan_to_compiled_payloads(
                    {"project": {}, "review_items": [], "edits": []},
                    {"review_items": []},
                    cut_plan,
                    audio_delivery_plan=delivery_plan,
                    source_audio_path=source,
                    candidate_audio_path=candidate,
                )
                report = build_full_candidate_reverse_report(
                    request,
                    cut_plan,
                    {
                        "provider": "volc_asr",
                        "resource_id": "volc.bigasr.auc",
                        "adapter_version": "test-adapter-v1",
                        "service_job_id": "reverse-job",
                        "service_result_sha256": "c" * 64,
                        "words": [{"text": "保留", "start": 0.6, "end": 0.8}],
                    },
                    candidate_audio_path=candidate,
                    audio_delivery_plan_sha256=canonical_json_sha256(delivery_plan),
                )
                return delivery_plan, request, ledger, report

            one_frame_duration = 1.0 + (1.0 / 16_000)
            delivery_plan, request, ledger, report = build_evidence(one_frame_duration)
            self.assertTrue(
                delivery_plan["validation_only_audio_metadata"]["duration_matches_source"]
            )
            self.assertTrue(request["processed_audio"]["candidate_audio_duration_matches_source"])
            self.assertTrue(report["candidate_audio_duration_matches_source"])
            apply_reverse_report_to_payloads(
                request,
                ledger,
                report,
                report_path=root / "reverse-report.json",
            )

            two_frame_duration = 1.0 + (2.0 / 16_000)
            delivery_plan, request, ledger, report = build_evidence(two_frame_duration)
            self.assertFalse(
                delivery_plan["validation_only_audio_metadata"]["duration_matches_source"]
            )
            self.assertFalse(request["processed_audio"]["candidate_audio_duration_matches_source"])
            self.assertFalse(report["candidate_audio_duration_matches_source"])
            report["candidate_audio_duration_matches_source"] = True
            with self.assertRaisesRegex(ValueError, "duration does not match"):
                apply_reverse_report_to_payloads(
                    request,
                    ledger,
                    report,
                    report_path=root / "reverse-report.json",
                )

    def test_reverse_report_maps_first_window_from_segmented_candidate_not_source_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            candidate = root / "candidate.wav"
            request_path = root / "request.json"
            _write_pcm16_wav(source, 3.0)
            _write_pcm16_wav(candidate, 3.0)
            cut_plan = {
                "source_duration_seconds": 3.0,
                "rows": [
                    {
                        "item_id": "cut-zero",
                        "kind": "spoken_delete",
                        "source_text": "00:00 删除删词",
                        "execution_required": True,
                        "strategy": "precision_first",
                        "delete": "删词",
                        "must_keep": [],
                        "start": 0.0,
                        "end": 1.0,
                    }
                ],
                "executable_cuts": [{"item_id": "cut-zero", "start": 0.0, "end": 1.0}],
            }
            delivery_plan = build_lite_split_gap_audio_plan(
                cut_plan,
                source_audio_path=source,
                candidate_audio_path=candidate,
            )
            request_payload = {
                "workflow_mode": "lite",
                "project": {
                    "draft_name": "ReverseMapping",
                    "source_video": "C:/media/source.mp4",
                    "source_audio": str(source),
                },
                "review_items": [
                    {
                        "id": "cut-zero",
                        "kind": "spoken_delete",
                        "source_text": "00:00 删除删词",
                        "evidence": {
                            "strategy": "precision_first",
                            "delete": "删词",
                            "must_keep": [],
                            "cut_windows": [[0.0, 1.0]],
                        },
                    }
                ],
                "edits": [
                    {
                        "type": "delete",
                        "source_kind": "spoken_delete",
                        "doc_item_id": "cut-zero",
                        "start": 0.0,
                        "end": 1.0,
                    }
                ],
                "audio_delivery_plan": delivery_plan,
            }
            report = build_full_candidate_reverse_report(
                request_payload,
                cut_plan,
                {
                    "provider": "volc_asr",
                    "resource_id": "volc.bigasr.auc",
                    "adapter_version": "test-adapter-v1",
                    "service_job_id": "reverse-job",
                    "service_result_sha256": "c" * 64,
                    "words": [{"text": "保留内容", "start": 1.05, "end": 1.5}],
                },
                candidate_audio_path=candidate,
                audio_delivery_plan_sha256=canonical_json_sha256(delivery_plan),
            )
            request_path.write_text(
                json.dumps(request_payload, ensure_ascii=False), encoding="utf-8"
            )
            parsed_request = load_revision_request(str(request_path))

        row = report["rows"][0]
        self.assertEqual(row["mapped_join_times"], [1.0])
        self.assertNotEqual(row["mapped_join_times"], [row["source_cut_windows"][0][0]])
        self.assertEqual(
            _spoken_reverse_asr_row_evidence_problems(
                row,
                request=parsed_request,
                item_id="cut-zero",
            ),
            [],
        )

    def test_reverse_report_attributes_only_hits_overlapping_the_cut_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            candidate = root / "candidate.wav"
            _write_pcm16_wav(source, 3.0)
            _write_pcm16_wav(candidate, 3.0)
            cut_plan = {
                "source_duration_seconds": 3.0,
                "rows": [
                    {
                        "item_id": "repeated-later",
                        "kind": "phrase_delete",
                        "source_text": "00:01 删除把它",
                        "execution_required": True,
                        "strategy": "precision_first",
                        "delete": "把它",
                        "must_keep": [],
                        "start": 1.0,
                        "end": 1.3,
                    }
                ],
                "executable_cuts": [{"item_id": "repeated-later", "start": 1.0, "end": 1.3}],
            }
            delivery_plan = build_lite_split_gap_audio_plan(
                cut_plan,
                source_audio_path=source,
                candidate_audio_path=candidate,
            )
            request = {
                "audio_delivery_plan": delivery_plan,
            }
            report = build_full_candidate_reverse_report(
                request,
                cut_plan,
                {
                    "provider": "volc_asr",
                    "resource_id": "volc.bigasr.auc",
                    "adapter_version": "test-adapter-v1",
                    "service_job_id": "reverse-job",
                    "service_result_sha256": "c" * 64,
                    "words": [
                        {"text": "保留", "start": 0.6, "end": 0.9},
                        {"text": "把", "start": 2.0, "end": 2.15},
                        {"text": "它", "start": 2.15, "end": 2.3},
                    ],
                },
                candidate_audio_path=candidate,
                audio_delivery_plan_sha256=canonical_json_sha256(delivery_plan),
            )

        row = report["rows"][0]
        self.assertEqual(row["status"], "pass_adjudicated")
        self.assertEqual(len(row["delete_hits"]), 1)
        self.assertEqual(len(row["kept_recurrence_hits"]), 1)
        self.assertEqual(row["kept_recurrence_hits"][0]["text"], "把它")
        self.assertEqual(
            row["delete_hit_adjudication"]["classification"],
            "kept_recurrence",
        )
        self.assertEqual(
            row["delete_hit_adjudication"]["occurrence_role"],
            "later_kept_occurrence",
        )

    def test_reverse_report_accepts_multiple_adjudicated_kept_recurrences(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            candidate = root / "candidate.wav"
            _write_pcm16_wav(source, 4.0)
            _write_pcm16_wav(candidate, 4.0)
            cut_plan = {
                "source_duration_seconds": 4.0,
                "rows": [
                    {
                        "item_id": "repeated-twice",
                        "kind": "phrase_delete",
                        "source_text": "00:01 删除这个",
                        "execution_required": True,
                        "strategy": "precision_first",
                        "delete": "这个",
                        "must_keep": [],
                        "start": 1.0,
                        "end": 1.3,
                    }
                ],
                "executable_cuts": [{"item_id": "repeated-twice", "start": 1.0, "end": 1.3}],
            }
            delivery_plan = build_lite_split_gap_audio_plan(
                cut_plan,
                source_audio_path=source,
                candidate_audio_path=candidate,
            )
            report = build_full_candidate_reverse_report(
                {"audio_delivery_plan": delivery_plan},
                cut_plan,
                {
                    "provider": "volc_asr",
                    "resource_id": "volc.bigasr.auc",
                    "adapter_version": "test-adapter-v1",
                    "service_job_id": "reverse-job",
                    "service_result_sha256": "c" * 64,
                    "words": [
                        {"text": "这个", "start": 2.0, "end": 2.2},
                        {"text": "保留", "start": 2.2, "end": 2.5},
                        {"text": "这个", "start": 2.6, "end": 2.8},
                    ],
                },
                candidate_audio_path=candidate,
                audio_delivery_plan_sha256=canonical_json_sha256(delivery_plan),
            )

        row = report["rows"][0]
        self.assertEqual(row["status"], "pass_adjudicated")
        self.assertEqual(len(row["kept_recurrence_hits"]), 2)
        self.assertEqual(len(row["delete_hit_adjudications"]), 2)
        self.assertEqual(report["unresolved_ids"], [])

    def test_reverse_report_uses_source_asr_window_for_boundary_drift_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            candidate = root / "candidate.wav"
            _write_pcm16_wav(source, 3.0)
            _write_pcm16_wav(candidate, 3.0)
            cut_plan = {
                "source_duration_seconds": 3.0,
                "rows": [
                    {
                        "item_id": "boundary-drift",
                        "kind": "phrase_delete",
                        "source_text": "00:01 删除删词",
                        "execution_required": True,
                        "strategy": "precision_first",
                        "delete": "删词",
                        "must_keep": ["就是"],
                        "must_keep_source_windows": [
                            {
                                "phrase": "就是",
                                "start": 0.78,
                                "end": 0.98,
                                "source": "automatic_adjacent_asr_context",
                            }
                        ],
                        "start": 1.0,
                        "end": 1.3,
                    }
                ],
                "executable_cuts": [{"item_id": "boundary-drift", "start": 1.0, "end": 1.3}],
            }
            delivery_plan = build_lite_split_gap_audio_plan(
                cut_plan,
                source_audio_path=source,
                candidate_audio_path=candidate,
            )

            def report_for(word_start, word_end):
                return build_full_candidate_reverse_report(
                    {"audio_delivery_plan": delivery_plan},
                    cut_plan,
                    {
                        "provider": "volc_asr",
                        "resource_id": "volc.bigasr.auc",
                        "adapter_version": "test-adapter-v1",
                        "service_job_id": "reverse-job",
                        "service_result_sha256": "c" * 64,
                        "words": [
                            {"text": "就是", "start": word_start, "end": word_end},
                            {"text": "保留", "start": 1.5, "end": 1.8},
                        ],
                    },
                    candidate_audio_path=candidate,
                    audio_delivery_plan_sha256=canonical_json_sha256(delivery_plan),
                )

            drifted = report_for(0.92, 1.05)
            contained = report_for(1.05, 1.15)

        self.assertEqual(drifted["rows"][0]["status"], "pass")
        self.assertTrue(drifted["rows"][0]["keep_hits"]["就是"])
        self.assertEqual(contained["rows"][0]["status"], "review")
        self.assertFalse(contained["rows"][0]["keep_hits"]["就是"])

    def test_reverse_report_distinguishes_automatic_and_explicit_must_keep(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            candidate = root / "candidate.wav"
            _write_pcm16_wav(source, 3.0)
            _write_pcm16_wav(candidate, 3.0)
            base_row = {
                "item_id": "must-keep",
                "kind": "phrase_delete",
                "source_text": "00:01 删除删词",
                "execution_required": True,
                "strategy": "precision_first",
                "delete": "删词",
                "must_keep": ["保护"],
                "must_keep_source_windows": [
                    {
                        "phrase": "保护",
                        "start": 0.7,
                        "end": 0.9,
                        "source": "automatic_adjacent_asr_context",
                    }
                ],
                "start": 1.0,
                "end": 1.3,
            }
            delivery_plan = build_lite_split_gap_audio_plan(
                {
                    "source_duration_seconds": 3.0,
                    "rows": [base_row],
                    "executable_cuts": [{"item_id": "must-keep", "start": 1.0, "end": 1.3}],
                },
                source_audio_path=source,
                candidate_audio_path=candidate,
            )

            def report_for(origin, words):
                row = {**base_row, "must_keep_origin": origin}
                cut_plan = {
                    "source_duration_seconds": 3.0,
                    "rows": [row],
                    "executable_cuts": [{"item_id": "must-keep", "start": 1.0, "end": 1.3}],
                }
                return build_full_candidate_reverse_report(
                    {"audio_delivery_plan": delivery_plan},
                    cut_plan,
                    {
                        "provider": "volc_asr",
                        "resource_id": "volc.bigasr.auc",
                        "adapter_version": "test-adapter-v1",
                        "service_job_id": "reverse-job",
                        "service_result_sha256": "c" * 64,
                        "words": words,
                    },
                    candidate_audio_path=candidate,
                    audio_delivery_plan_sha256=canonical_json_sha256(delivery_plan),
                )

            automatic_missing = report_for(
                "automatic_adjacent_asr_context",
                [{"text": "其他内容", "start": 1.5, "end": 1.8}],
            )
            explicit_missing = report_for(
                "explicit",
                [{"text": "其他内容", "start": 1.5, "end": 1.8}],
            )
            automatic_inside_cut = report_for(
                "automatic_adjacent_asr_context",
                [
                    {"text": "保护", "start": 1.05, "end": 1.15},
                    {"text": "其他内容", "start": 1.5, "end": 1.8},
                ],
            )

        automatic_row = automatic_missing["rows"][0]
        self.assertEqual(automatic_row["status"], "pass")
        self.assertEqual(
            automatic_row["must_keep_warnings"][0]["reason"],
            "automatic_context_not_recognized",
        )
        self.assertEqual(explicit_missing["rows"][0]["status"], "review")
        inside_row = automatic_inside_cut["rows"][0]
        self.assertEqual(inside_row["status"], "review")
        self.assertTrue(inside_row["must_keep_cut_hits"]["保护"])

    def test_reverse_report_still_rejects_delete_hit_at_cut_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.wav"
            candidate = root / "candidate.wav"
            _write_pcm16_wav(source, 3.0)
            _write_pcm16_wav(candidate, 3.0)
            cut_plan = {
                "source_duration_seconds": 3.0,
                "rows": [
                    {
                        "item_id": "residue",
                        "kind": "phrase_delete",
                        "source_text": "00:01 删除把它",
                        "execution_required": True,
                        "strategy": "precision_first",
                        "delete": "把它",
                        "must_keep": [],
                        "start": 1.0,
                        "end": 1.3,
                    }
                ],
                "executable_cuts": [{"item_id": "residue", "start": 1.0, "end": 1.3}],
            }
            delivery_plan = build_lite_split_gap_audio_plan(
                cut_plan,
                source_audio_path=source,
                candidate_audio_path=candidate,
            )
            report = build_full_candidate_reverse_report(
                {"audio_delivery_plan": delivery_plan},
                cut_plan,
                {
                    "provider": "volc_asr",
                    "resource_id": "volc.bigasr.auc",
                    "adapter_version": "test-adapter-v1",
                    "service_job_id": "reverse-job",
                    "service_result_sha256": "c" * 64,
                    "words": [
                        {"text": "把", "start": 1.05, "end": 1.15},
                        {"text": "它", "start": 1.15, "end": 1.25},
                    ],
                },
                candidate_audio_path=candidate,
                audio_delivery_plan_sha256=canonical_json_sha256(delivery_plan),
            )

        row = report["rows"][0]
        self.assertEqual(row["status"], "review")
        self.assertEqual(len(row["delete_hits"]), 1)
        self.assertEqual(report["unresolved_ids"], ["residue"])

    def test_semantic_join_scan_does_not_reject_valid_unpunctuated_phrase(self):
        self.assertEqual(_semantic_join_forbidden_patterns("阶段性成就"), [])
        self.assertEqual(
            _semantic_join_forbidden_patterns("阶段性。成就"),
            ["阶段性。成就"],
        )
        self.assertEqual(_semantic_join_forbidden_patterns("这是发发明"), ["发发明"])

    def test_cache_identities_change_for_every_relevant_input(self):
        config_a = VolcAsrConfig(resource_id="resource-a")
        config_b = VolcAsrConfig(resource_id="resource-b")
        ffmpeg = {"path": "ffmpeg", "version": "v1", "sha256": "f" * 64}

        pairs = [
            (
                alignment_cache_identity(source_sha256="a" * 64, ffmpeg=ffmpeg),
                alignment_cache_identity(source_sha256="b" * 64, ffmpeg=ffmpeg),
            ),
            (
                source_asr_cache_identity(alignment_audio_sha256="a" * 64, config=config_a),
                source_asr_cache_identity(alignment_audio_sha256="a" * 64, config=config_b),
            ),
            (
                candidate_cache_identity(
                    alignment_audio_sha256="a" * 64,
                    executable_cuts=[{"item_id": "one", "start": 1.0, "end": 2.0}],
                ),
                candidate_cache_identity(
                    alignment_audio_sha256="a" * 64,
                    executable_cuts=[{"item_id": "one", "start": 1.1, "end": 2.0}],
                ),
            ),
            (
                reverse_asr_cache_identity(
                    candidate_audio_sha256="a" * 64,
                    cut_plan_sha256="c" * 64,
                    config=config_a,
                ),
                reverse_asr_cache_identity(
                    candidate_audio_sha256="b" * 64,
                    cut_plan_sha256="c" * 64,
                    config=config_a,
                ),
            ),
        ]
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                self.assertNotEqual(canonical_json_sha256(first), canonical_json_sha256(second))


if __name__ == "__main__":
    unittest.main()
