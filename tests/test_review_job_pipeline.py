# ruff: noqa: E402,I001
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from utils.review_job_pipeline import (
    JobStateStore,
    PhaseDefinition,
    PhaseOutcome,
    ReviewJobExecutor,
)


class ReviewJobPipelineTests(unittest.TestCase):
    def _store(self, root: Path) -> JobStateStore:
        return JobStateStore(root / "job_state.json", "job-input-v1", "runner-v1")

    def test_phase_outcome_persists_real_cache_hit_and_output_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = self._store(root)
            output_digest = "a" * 64
            phase = PhaseDefinition(
                "source_asr",
                lambda: PhaseOutcome(
                    result={"artifact": "source-asr.json"},
                    output_digest=output_digest,
                    cache_hit=True,
                ),
                item_ids=("item-1",),
            )

            records = ReviewJobExecutor(state_store=store).run((phase,))

            self.assertEqual(records["source_asr"]["status"], "complete")
            persisted = store.get_phase("source_asr")
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted["output_digest"], output_digest)
            self.assertIs(persisted["cache_hit"], True)
            self.assertEqual(persisted["item_ids"], ["item-1"])

            state = json.loads((root / "job_state.json").read_text(encoding="utf-8"))
            timing = json.loads((root / "job_timing.json").read_text(encoding="utf-8"))
            for payload in (state, timing):
                record = payload["phases"]["source_asr"]
                self.assertEqual(record["output_digest"], output_digest)
                self.assertIs(record["cache_hit"], True)

    def test_resume_check_false_reruns_completed_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            calls = []

            first = PhaseDefinition("compile", lambda: calls.append("first") or {"run": 1})
            first_records = ReviewJobExecutor(state_store=store).run((first,))
            self.assertEqual(first_records["compile"]["status"], "complete")

            second = PhaseDefinition(
                "compile",
                lambda: calls.append("second") or {"run": 2},
                resume_check=lambda: False,
            )
            second_records = ReviewJobExecutor(state_store=store).run((second,))

            self.assertEqual(second_records["compile"]["status"], "complete")
            self.assertEqual(calls, ["first", "second"])

    def test_resume_check_true_restores_without_running_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            calls = []

            first = PhaseDefinition("compile", lambda: calls.append("first") or {"run": 1})
            first_records = ReviewJobExecutor(state_store=store).run((first,))
            self.assertEqual(first_records["compile"]["status"], "complete")

            second = PhaseDefinition(
                "compile",
                lambda: calls.append("unexpected") or {"run": 2},
                resume_check=lambda: True,
            )
            second_records = ReviewJobExecutor(state_store=store).run((second,))

            self.assertEqual(second_records["compile"]["status"], "resumed")
            self.assertEqual(calls, ["first"])

    def test_retry_count_one_retries_once_and_persists_final_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            attempts = []

            def flaky_phase():
                attempts.append(len(attempts))
                if len(attempts) == 1:
                    raise RuntimeError("transient ASR failure")
                return PhaseOutcome(
                    result={"status": "recovered"},
                    output_digest="b" * 64,
                    cache_hit=False,
                )

            phase = PhaseDefinition("source_asr", flaky_phase, retry_count=1)
            records = ReviewJobExecutor(state_store=store).run((phase,))

            self.assertEqual(records["source_asr"]["status"], "complete")
            self.assertEqual(len(attempts), 2)
            persisted = store.get_phase("source_asr")
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted["retry_count"], 1)
            self.assertEqual(persisted["output_digest"], "b" * 64)
            self.assertIs(persisted["cache_hit"], False)

    def test_retry_count_one_never_runs_more_than_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            attempts = []

            def always_fails():
                attempts.append(len(attempts))
                raise RuntimeError("persistent ASR failure")

            phase = PhaseDefinition("source_asr", always_fails, retry_count=1)
            records = ReviewJobExecutor(state_store=store).run((phase,))

            self.assertEqual(records["source_asr"]["status"], "failed")
            self.assertEqual(len(attempts), 2)
            persisted = store.get_phase("source_asr")
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted["status"], "failed")
            self.assertEqual(persisted["retry_count"], 1)

    def test_phase_definition_rejects_more_than_one_retry(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most 1"):
            PhaseDefinition("source_asr", lambda: None, retry_count=2)

    def test_phase_definition_rejects_non_callable_resume_check(self) -> None:
        with self.assertRaisesRegex(TypeError, "resume_check must be callable"):
            PhaseDefinition("source_asr", lambda: None, resume_check=True)


if __name__ == "__main__":
    unittest.main()
