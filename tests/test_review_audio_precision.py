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

from utils.lite_revision import _validate_lite_audio_timing_sources
from utils.review_audio_precision import (
    alignment_cache_identity,
    apply_audio_plan_to_compiled_payloads,
    build_full_candidate_reverse_report,
    build_lite_split_gap_audio_plan,
    candidate_cache_identity,
    canonical_json_sha256,
    render_source_aligned_candidate,
    resolve_lite_audio_items,
    reverse_asr_cache_identity,
    source_asr_cache_identity,
    wav_duration_seconds,
)
from utils.revision_models import load_revision_request
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

    def test_ambiguous_phrase_downgrades_to_asr_located_label(self):
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
        self.assertFalse(row["asr_alignment"]["authoritative_cut_boundary"])
        self.assertEqual(result["executable_cuts"], [])

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
            "pause_adjustments": [
                {"item_id": "audio-label", "source_time": 5.0, "duration": 1.0}
            ],
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
        self.assertEqual(by_id["audio-label"]["start"], 5.25)
        self.assertEqual(
            by_id["audio-label"]["evidence"]["asr_alignment"]["resolved_time"],
            5.25,
        )
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
            with wave.open(str(candidate), "rb") as rendered:
                rendered.setpos(round(0.6 * 16_000))
                silent = rendered.readframes(1)
                rendered.setpos(round(1.2 * 16_000))
                audible = rendered.readframes(1)
            self.assertEqual(silent, b"\0\0")
            self.assertNotEqual(audible, b"\0\0")

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
            request_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
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
            request_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "must not create pause_adjustments"):
                _validate_lite_audio_timing_sources(
                    load_revision_request(str(request_path)), None
                )

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
            [(row["source_start"], row["duration"]) for row in a1],
            [(0.0, 0.5), (1.25, 1.75)],
        )
        self.assertEqual(
            [(row["doc_item_id"], row["source_start"], row["duration"]) for row in a2],
            [("cut-a", 0.5, 0.5), ("cut-b", 1.0, 0.25)],
        )
        self.assertEqual([row["volume"] for row in a2], [0.0, 0.0])
        lite_request = SimpleNamespace(workflow_mode="lite", lite_cut_layout="split_gap")
        saved_volumes = [
            _saved_audio_delivery_volume(lite_request, SimpleNamespace(**row)) for row in a2
        ]
        self.assertEqual(saved_volumes, [1.0, 1.0])

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
                "executable_cuts": [
                    {"item_id": "cut-zero", "start": 0.0, "end": 1.0}
                ],
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
                source_asr_cache_identity(
                    alignment_audio_sha256="a" * 64, config=config_a
                ),
                source_asr_cache_identity(
                    alignment_audio_sha256="a" * 64, config=config_b
                ),
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
                self.assertNotEqual(
                    canonical_json_sha256(first), canonical_json_sha256(second)
                )


if __name__ == "__main__":
    unittest.main()
