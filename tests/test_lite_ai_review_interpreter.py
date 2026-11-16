"""Claude-assisted locating: proposals are only accepted after deterministic checks."""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from utils import ai_review_interpreter as ai  # noqa: E402
from utils.review_audio_precision import resolve_lite_audio_items  # noqa: E402


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


def _index_of(words, text, start_time):
    for index, word in enumerate(words):
        if word["text"] == text and abs(word["start"] - start_time) < 1e-6:
            return index
    raise AssertionError((text, start_time))


class RecordingResolver:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = []

    def __call__(self, item, words, window):
        self.calls.append((dict(item), window))
        return dict(self.proposal)


def _resolve(source_text, delete, words, resolver, kind="phrase_delete", duration=900.0):
    evidence = {"must_keep": []}
    if delete is not None:
        evidence["delete"] = delete
    result = resolve_lite_audio_items(
        [
            {
                "id": "item",
                "kind": kind,
                "source_text": source_text,
                "execution_required": True,
                "evidence": evidence,
            }
        ],
        _source_asr(words),
        source_duration_seconds=duration,
        ai_resolver=resolver,
    )
    return result["rows"][0]


# 10:15 删除“奥匈帝国作战……让民众对它的不满进一步发展”; the speaker said 进一步的发展.
ELLIPSIS_WORDS = [
    *_chars("前面一句", 612.0),
    *_chars("奥匈帝国作战", 615.0),
    *_chars("中间保留的原话很长", 616.0, 0.5),
    *_chars("让民众对它的不满进一步的发展", 621.0),
    *_chars("后面", 624.0),
]
ELLIPSIS_TEXT = "10：15-10：25，删除“奥匈帝国作战……让民众对它的不满进一步发展”"
ELLIPSIS_DELETE = "奥匈帝国作战……让民众对它的不满进一步发展"


class VerifiedProposalTests(unittest.TestCase):
    def setUp(self):
        self.start = _index_of(ELLIPSIS_WORDS, "奥", 615.0)
        self.end = _index_of(ELLIPSIS_WORDS, "展", 622.3)

    def test_deterministic_miss_is_recovered_by_verified_proposal(self):
        resolver = RecordingResolver(
            {
                "action": "delete_speech",
                "word_start_index": self.start,
                "word_end_index": self.end,
                "confidence": "high",
                "rationale": "两端都对上，ASR 多了一个“的”",
                "model": "claude-opus-5-5",
            }
        )
        row = _resolve(
            ELLIPSIS_TEXT, ELLIPSIS_DELETE, ELLIPSIS_WORDS, resolver, kind="ellipsis_range_delete"
        )
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["match_method"], "ai_proposed_asr_verified")
        self.assertEqual(row["asr_word_cut_windows"], [[615.0, 622.4]])
        self.assertEqual(row["asr_match"]["ai_proposal"]["status"], "accepted")
        self.assertEqual(row["asr_match"]["ai_proposal"]["model"], "claude-opus-5-5")
        self.assertEqual(row["source_text"], ELLIPSIS_TEXT)
        self.assertEqual(len(resolver.calls), 1)

    def test_proposal_pointing_at_other_words_is_rejected(self):
        wrong_start = _index_of(ELLIPSIS_WORDS, "中", 616.0)
        resolver = RecordingResolver(
            {
                "action": "delete_speech",
                "word_start_index": wrong_start,
                "word_end_index": self.end,
                "confidence": "high",
                "rationale": "",
            }
        )
        row = _resolve(
            ELLIPSIS_TEXT, ELLIPSIS_DELETE, ELLIPSIS_WORDS, resolver, kind="ellipsis_range_delete"
        )
        self.assertFalse(row["execution_required"])
        self.assertEqual(row["execution_status"], "label_only_unresolved")
        self.assertEqual(row["ai_proposal"]["reason"], "proposal_text_mismatch")

    def test_low_confidence_and_not_applicable_stay_label_only(self):
        for proposal, reason in (
            (
                {
                    "action": "delete_speech",
                    "word_start_index": self.start,
                    "word_end_index": self.end,
                    "confidence": "low",
                    "rationale": "",
                },
                "proposal_low_confidence",
            ),
            (
                {
                    "action": "not_applicable",
                    "word_start_index": 0,
                    "word_end_index": 0,
                    "confidence": "high",
                    "rationale": "",
                },
                "proposal_not_applicable",
            ),
        ):
            row = _resolve(
                ELLIPSIS_TEXT,
                ELLIPSIS_DELETE,
                ELLIPSIS_WORDS,
                RecordingResolver(proposal),
                kind="ellipsis_range_delete",
            )
            self.assertFalse(row["execution_required"])
            self.assertEqual(row["ai_proposal"]["reason"], reason)

    def test_out_of_range_indexes_are_rejected(self):
        resolver = RecordingResolver(
            {
                "action": "delete_speech",
                "word_start_index": 5,
                "word_end_index": 10_000,
                "confidence": "high",
                "rationale": "",
            }
        )
        row = _resolve(
            ELLIPSIS_TEXT, ELLIPSIS_DELETE, ELLIPSIS_WORDS, resolver, kind="ellipsis_range_delete"
        )
        self.assertFalse(row["execution_required"])
        self.assertEqual(row["ai_proposal"]["reason"], "proposal_invalid_indexes")

    def test_resolver_is_not_called_when_rules_already_match(self):
        words = [*_chars("前面", 9.0), *_chars("对吧", 10.0), *_chars("后面", 11.0)]
        resolver = RecordingResolver({"action": "not_applicable"})
        row = _resolve("00:10 删除对吧", "对吧", words, resolver, duration=20.0)
        self.assertTrue(row["execution_required"])
        self.assertNotEqual(row["match_method"], "ai_proposed_asr_verified")
        self.assertEqual(resolver.calls, [])

    def test_pause_requests_are_never_sent_to_the_model(self):
        words = _chars("相对比较初始", 764.0, 0.3)
        resolver = RecordingResolver({"action": "delete_speech"})
        result = resolve_lite_audio_items(
            [
                {
                    "id": "pause",
                    "kind": "pause_adjustment",
                    "source_text": "12：45，“相对”和“比较”之间停顿较长，删除停顿",
                    "execution_required": True,
                    "evidence": {"must_keep": []},
                }
            ],
            _source_asr(words),
            source_duration_seconds=900.0,
            ai_resolver=resolver,
        )
        self.assertEqual(resolver.calls, [])
        for row in result["rows"]:
            self.assertFalse(row["execution_required"])


class EdgeFillerAndRangeTests(unittest.TestCase):
    def test_leading_interjection_not_in_review_is_trimmed(self):
        words = [
            *_chars("前面", 98.0),
            {"text": "啊", "start": 99.5, "end": 99.7},
            *_chars("同学们注意一下这个地方", 100.0),
            *_chars("后面", 103.0),
        ]
        start = _index_of(words, "啊", 99.5)
        end = _index_of(words, "方", 101.0)
        resolver = RecordingResolver(
            {
                "action": "delete_speech",
                "word_start_index": start,
                "word_end_index": end,
                "confidence": "medium",
                "rationale": "",
            }
        )
        row = _resolve("01:40 删除同学们注意下这个地方", "同学们注意下这个地方", words, resolver)
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["asr_word_cut_windows"], [[100.0, 101.1]])

    def test_time_range_only_comment_accepts_span_inside_range(self):
        words = [*_chars("我们今天来学习一下", 0.5), *_chars("十月革命", 9.0)]
        resolver = RecordingResolver(
            {
                "action": "delete_speech",
                "word_start_index": 0,
                "word_end_index": 8,
                "confidence": "high",
                "rationale": "整段开场白",
            }
        )
        row = _resolve("00：00-00：08，删除", None, words, resolver, kind="spoken_delete")
        self.assertTrue(row["execution_required"])
        self.assertEqual(row["asr_word_cut_windows"], [[0.5, 1.4]])
        self.assertEqual(row["delete"], "我们今天来学习一下")
        self.assertEqual(row["source_text"], "00：00-00：08，删除")

    def test_time_range_only_comment_rejects_span_outside_range(self):
        words = [*_chars("我们今天来学习一下", 0.5, 0.3), *_chars("十月革命", 3.2)]
        resolver = RecordingResolver(
            {
                "action": "delete_speech",
                "word_start_index": 0,
                "word_end_index": 8,
                "confidence": "high",
                "rationale": "",
            }
        )
        row = _resolve("00：00-00：01，删除", None, words, resolver, kind="spoken_delete")
        self.assertFalse(row["execution_required"])
        self.assertEqual(row["ai_proposal"]["reason"], "proposal_outside_review_range")


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _response(payload, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        _request_id="req_test",
        content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
    )


class ResolverClientTests(unittest.TestCase):
    def _client(self, responses):
        messages = FakeMessages(responses)
        return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages

    def test_request_shape_and_cache_reuse(self):
        import tempfile

        payload = {
            "action": "delete_speech",
            "word_start_index": 1,
            "word_end_index": 2,
            "confidence": "high",
            "rationale": "x",
        }
        client, messages = self._client([_response(payload)])
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            resolver = ai.make_resolver(client, cache=ai.ProposalCache(cache_path))
            item = {"id": "a", "source_text": "00:10 删除对吧", "kind": "phrase_delete"}
            words = _chars("前对吧后", 10.0)
            first = resolver(item, words, (0.0, 30.0))
            second = ai.make_resolver(client, cache=ai.ProposalCache(cache_path))(
                item, words, (0.0, 30.0)
            )
        self.assertEqual(first["word_start_index"], 1)
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(messages.requests), 1)
        request = messages.requests[0]
        self.assertEqual(request["model"], "claude-opus-5-5")
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(request["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", request["betas"])
        self.assertNotIn("thinking", request)
        self.assertIn("1\t10.10\t10.20\t对", request["messages"][0]["content"])

    def test_api_error_and_refusal_degrade_to_not_applicable(self):
        client, _ = self._client(
            [RuntimeError("boom"), _response({}, stop_reason="refusal")]
        )
        resolver = ai.make_resolver(client)
        item = {"id": "a", "source_text": "x"}
        words = _chars("前对吧后", 10.0)
        self.assertEqual(resolver(item, words, (0.0, 30.0))["status"], "api_error")
        self.assertEqual(resolver(item, words, (0.0, 30.0))["status"], "refused")

    def test_disabled_by_default(self):
        self.assertIsNone(ai.resolver_from_environment({"ANTHROPIC_API_KEY": "x"}))
        self.assertEqual(ai.identity_from_environment({})["enabled"], False)
        identity = ai.identity_from_environment({"AUTO_CUT_AI_REVIEW": "1"})
        self.assertTrue(identity["enabled"])
        self.assertEqual(identity["model"], "claude-opus-5-5")
        self.assertNotIn("ANTHROPIC_API_KEY", json.dumps(identity))


if __name__ == "__main__":
    unittest.main()


class DraftValidationTests(unittest.TestCase):
    def test_ai_accepted_cuts_pass_lite_spoken_cut_validation(self):
        import tempfile

        sys.path.insert(0, str(REPO_ROOT / "tests"))
        from test_review_audio_precision import _write_pcm16_wav  # noqa: PLC0415
        from utils.lite_revision import _validate_lite_audio_timing_sources  # noqa: PLC0415
        from utils.review_audio_precision import (  # noqa: PLC0415
            apply_audio_plan_to_compiled_payloads,
            build_lite_split_gap_audio_plan,
        )
        from utils.revision_models import (  # noqa: PLC0415
            load_review_items_json,
            load_revision_request,
        )

        words = [
            *_chars("我们今天来学习一下", 0.5),
            *_chars("十月革命", 3.0),
            *_chars("前面一句", 12.0),
            *_chars("奥匈帝国作战", 15.0),
            *_chars("中间原话", 16.0, 0.5),
            *_chars("让民众对它的不满进一步的发展", 18.0),
            *_chars("后面", 21.0),
        ]
        items = [
            {
                "id": "range-only",
                "kind": "spoken_delete",
                "source_text": "00：00-00：02，删除",
                "execution_required": True,
                "evidence": {"must_keep": []},
            },
            {
                "id": "ellipsis",
                "kind": "ellipsis_range_delete",
                "source_text": "00：15-00：20，删除“奥匈帝国作战……让民众对它的不满进一步发展”",
                "execution_required": True,
                "evidence": {"delete": "奥匈帝国作战……让民众对它的不满进一步发展", "must_keep": []},
            },
        ]
        proposals = {
            "range-only": (0, 8),
            "ellipsis": (_index_of(words, "奥", 15.0), _index_of(words, "展", 19.3)),
        }

        def resolver(item, _words, _window):
            start, end = proposals[item["id"]]
            return {
                "action": "delete_speech",
                "word_start_index": start,
                "word_end_index": end,
                "confidence": "high",
                "rationale": "",
            }

        cut_plan = resolve_lite_audio_items(
            items, _source_asr(words), source_duration_seconds=25.0, ai_resolver=resolver
        )
        self.assertEqual(
            [row["execution_status"] for row in cut_plan["rows"]],
            ["asr_resolved", "asr_resolved"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_audio = root / "source.wav"
            candidate_audio = root / "candidate.wav"
            _write_pcm16_wav(source_audio, 25.0)
            _write_pcm16_wav(candidate_audio, 25.0)
            delivery_plan = build_lite_split_gap_audio_plan(
                cut_plan, source_audio_path=source_audio, candidate_audio_path=candidate_audio
            )
            request, ledger = apply_audio_plan_to_compiled_payloads(
                {
                    "workflow_mode": "lite",
                    "project": {"draft_name": "AiVerified", "source_video": "C:/media/s.mp4"},
                    "review_items": items,
                    "edits": [],
                },
                {"review_items": items},
                cut_plan,
                audio_delivery_plan=delivery_plan,
                source_audio_path=source_audio,
                candidate_audio_path=None,
            )
            (root / "request.json").write_text(json.dumps(request, ensure_ascii=False), "utf-8")
            (root / "ledger.json").write_text(json.dumps(ledger, ensure_ascii=False), "utf-8")
            _validate_lite_audio_timing_sources(
                load_revision_request(str(root / "request.json")),
                load_review_items_json(str(root / "ledger.json")),
            )
        self.assertEqual(len(request["edits"]), 2)
        self.assertEqual(
            [item["source_text"] for item in ledger["review_items"]],
            [item["source_text"] for item in items],
        )
