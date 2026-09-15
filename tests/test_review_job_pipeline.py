# ruff: noqa: E402,I001
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_phase_errors_are_redacted_in_records_state_and_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = self._store(root)
            secret_url = "https://provider.example/jobs/private_token?access_token=top-secret"
            bare_secret = "provider_private_token"

            def fail_with_provider_details():
                raise RuntimeError(
                    f"request {secret_url} failed; token={bare_secret}; "
                    "Authorization: Bearer eyJprivate.header.signature"
                )

            records = ReviewJobExecutor(state_store=store).run(
                (PhaseDefinition("source_asr", fail_with_provider_details),)
            )

            serialized = json.dumps(records, ensure_ascii=False)
            serialized += (root / "job_state.json").read_text(encoding="utf-8")
            serialized += (root / "job_timing.json").read_text(encoding="utf-8")
            for secret in (secret_url, "top-secret", bare_secret, "eyJprivate"):
                self.assertNotIn(secret, serialized)
            self.assertIn("[redacted", serialized)

    def test_progress_callback_streams_started_retry_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            events = []
            attempts = []

            def flaky_phase():
                attempts.append(len(attempts))
                if len(attempts) == 1:
                    raise RuntimeError("retry")
                return {"ok": True}

            records = ReviewJobExecutor(state_store=store, progress=events.append).run(
                (PhaseDefinition("source_asr", flaky_phase, retry_count=1),)
            )

            self.assertEqual(records["source_asr"]["status"], "complete")
            self.assertEqual(
                [(row["status"], row.get("attempt")) for row in events],
                [("started", 0), ("retrying", 0), ("started", 1), ("complete", 1)],
            )

    def test_progress_callback_reports_resume_without_rerunning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            phase = PhaseDefinition("compile", lambda: {"run": 1})
            ReviewJobExecutor(state_store=store).run((phase,))
            events = []

            records = ReviewJobExecutor(state_store=store, progress=events.append).run(
                (PhaseDefinition("compile", lambda: self.fail("must resume")),)
            )

            self.assertEqual(records["compile"]["status"], "resumed")
            self.assertEqual(events, [{"event": "phase", "phase": "compile", "status": "resumed"}])

    def test_progress_callback_failure_does_not_change_phase_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))

            def broken_progress(_event):
                raise RuntimeError("stderr unavailable")

            records = ReviewJobExecutor(state_store=store, progress=broken_progress).run(
                (PhaseDefinition("compile", lambda: {"ok": True}),)
            )

            self.assertEqual(records["compile"]["status"], "complete")

    def test_phase_definition_rejects_more_than_one_retry(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most 1"):
            PhaseDefinition("source_asr", lambda: None, retry_count=2)

    def test_transient_failure_save_retries_and_persists_original_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            write = store._atomic_write_json
            failures = []

            def transient_denial(destination, payload):
                record = payload.get("phases", {}).get("preflight", {})
                if (
                    destination == store.timing_path
                    and record.get("status") == "failed"
                    and not failures
                ):
                    failures.append("denied")
                    raise PermissionError("diagnostic write denied")
                return write(destination, payload)

            def failed_cli():
                raise RuntimeError("original CLI failure")

            with patch.object(store, "_atomic_write_json", side_effect=transient_denial):
                records = ReviewJobExecutor(state_store=store).run(
                    (PhaseDefinition("preflight", failed_cli),)
                )

            self.assertEqual(failures, ["denied"])
            self.assertEqual(records["preflight"]["error"], "RuntimeError: original CLI failure")
            self.assertNotIn("state_persistence_error", records["preflight"])
            for path in (store.path, store.timing_path):
                record = json.loads(path.read_text(encoding="utf-8"))["phases"]["preflight"]
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error"], records["preflight"]["error"])
            self.assertFalse(store.transaction_path.exists())

    def test_persistent_failure_save_is_bounded_visible_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            write = store._atomic_write_json
            failures = []
            calls = []

            def persistent_denial(destination, payload):
                record = payload.get("phases", {}).get("preflight", {})
                if destination == store.timing_path and record.get("status") == "failed":
                    failures.append("denied")
                    raise PermissionError("credential=private-value C:/Users/private/path")
                return write(destination, payload)

            def failed_cli():
                calls.append("preflight")
                raise RuntimeError("original CLI failure")

            started = time.monotonic()
            with patch.object(store, "_atomic_write_json", side_effect=persistent_denial):
                records = ReviewJobExecutor(state_store=store).run(
                    (
                        PhaseDefinition("preflight", failed_cli, retry_count=1),
                        PhaseDefinition(
                            "compile",
                            lambda: self.fail("blocked phase ran"),
                            depends_on=("preflight",),
                        ),
                    )
                )

            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(failures, ["denied", "denied"])
            self.assertEqual(calls, ["preflight"])
            self.assertEqual(records["preflight"]["error"], "RuntimeError: original CLI failure")
            diagnostic = records["preflight"]["state_persistence_error"]
            self.assertIn("saved phase may be stale", diagnostic)
            self.assertNotIn("private", diagnostic)
            self.assertEqual(records["compile"]["status"], "skipped")
            self.assertEqual(store.status("compile"), "skipped")
            state = json.loads(store.path.read_text(encoding="utf-8"))
            timing = json.loads(store.timing_path.read_text(encoding="utf-8"))
            self.assertEqual(state, timing)
            self.assertFalse(store.transaction_path.exists())
            self.assertEqual(list(store.path.parent.glob("*.tmp-*")), [])

            recovered = ReviewJobExecutor(state_store=store).run(
                (PhaseDefinition("preflight", lambda: {"ok": True}),)
            )
            self.assertEqual(recovered["preflight"]["status"], "complete")
            self.assertEqual(store.status("preflight"), "complete")

    def test_completion_save_failure_converges_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            write = store._atomic_write_json

            def deny_completion(destination, payload):
                record = payload.get("phases", {}).get("preflight", {})
                if destination == store.path and record.get("status") == "complete":
                    raise PermissionError("completion save denied")
                return write(destination, payload)

            with patch.object(store, "_atomic_write_json", side_effect=deny_completion):
                records = ReviewJobExecutor(state_store=store).run(
                    (PhaseDefinition("preflight", lambda: {"ok": True}),)
                )

            self.assertEqual(records["preflight"]["status"], "failed")
            self.assertIn("completion save denied", records["preflight"]["error"])
            self.assertEqual(store.status("preflight"), "failed")
            self.assertEqual(store.snapshot(), store.timing_snapshot())

    def test_atomic_write_cleanup_does_not_replace_original_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            before = store.path.read_bytes()
            primary = PermissionError("original replace denial")
            with (
                patch("utils.review_job_pipeline.os.replace", side_effect=primary),
                patch.object(
                    store, "_remove_temporary", side_effect=PermissionError("cleanup denied")
                ),
            ):
                with self.assertRaises(PermissionError) as raised:
                    store._atomic_write_bytes(store.path, b"replacement")
            self.assertIs(raised.exception, primary)
            self.assertEqual(store.path.read_bytes(), before)

    def test_state_temp_permission_denial_attempts_create_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            before = store.path.read_bytes()
            with patch("builtins.open", side_effect=PermissionError("create denied")) as create:
                with self.assertRaises(PermissionError):
                    store._atomic_write_bytes(store.path, b"replacement")
            self.assertEqual(create.call_count, 1)
            self.assertEqual(store.path.read_bytes(), before)

    def test_state_temp_collision_preserves_the_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            collision = store.path.with_name(f"{store.path.name}.tmp-{'a' * 32}")
            collision.write_bytes(b"owned by another writer")
            with patch("utils.review_job_pipeline.uuid.uuid4") as identifier:
                identifier.return_value.hex = "a" * 32
                with self.assertRaises(FileExistsError):
                    store._atomic_write_bytes(store.path, b"replacement")
            self.assertEqual(collision.read_bytes(), b"owned by another writer")

    def test_rollback_failure_preserves_publish_error_and_verified_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            store.start_phase("preflight")
            before = store.path.read_bytes()
            write = store._atomic_write_json
            primary = PermissionError("original publish denial")

            def deny_state(destination, payload):
                if destination == store.path:
                    raise primary
                return write(destination, payload)

            with (
                patch.object(store, "_atomic_write_json", side_effect=deny_state),
                patch.object(
                    store, "_restore_file_snapshot", side_effect=PermissionError("rollback denied")
                ),
            ):
                with self.assertRaises(PermissionError) as raised:
                    store.fail_phase("preflight", "original CLI failure")
            self.assertIs(raised.exception, primary)
            self.assertTrue(store.transaction_path.exists())
            recovered_store = self._store(Path(temporary))
            self.assertEqual(recovered_store.path.read_bytes(), before)
            self.assertEqual(recovered_store.snapshot(), recovered_store.timing_snapshot())
            recovered_store.fail_phase("preflight", "original CLI failure")
            self.assertEqual(recovered_store.status("preflight"), "failed")

    def test_state_thread_lock_timeout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            entered = threading.Event()
            release = threading.Event()

            def hold_lock():
                with store._lock:
                    entered.set()
                    release.wait(timeout=5)

            thread = threading.Thread(target=hold_lock)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=2))
                started = time.monotonic()
                with patch("utils.review_job_pipeline._STATE_LOCK_TIMEOUT_SECONDS", 0.025):
                    with self.assertRaisesRegex(TimeoutError, "job state lock timed out"):
                        store.get_phase("preflight")
                self.assertLess(time.monotonic() - started, 1)
            finally:
                release.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_state_process_lock_timeout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            entered = threading.Event()
            release = threading.Event()

            def hold_lock():
                with store._process_lock():
                    entered.set()
                    release.wait(timeout=5)

            thread = threading.Thread(target=hold_lock)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=2))
                started = time.monotonic()
                with patch("utils.review_job_pipeline._STATE_LOCK_TIMEOUT_SECONDS", 0.025):
                    with self.assertRaisesRegex(TimeoutError, "job state process lock timed out"):
                        store.get_phase("preflight")
                self.assertLess(time.monotonic() - started, 1)
            finally:
                release.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_start_save_denial_does_not_run_phase_or_damage_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            before = store.path.read_bytes()
            with patch.object(store, "_atomic_write_json", side_effect=PermissionError("denied")):
                result = ReviewJobExecutor(state_store=store)._run_phase(
                    PhaseDefinition("preflight", lambda: self.fail("phase ran without saved start"))
                )
            self.assertEqual(result["status"], "failed")
            self.assertIn("state start failed", result["error"])
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(store.snapshot(), store.timing_snapshot())

    def test_skip_save_denial_is_reported_without_replacing_dependency_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))

            def failed_cli():
                raise RuntimeError("original CLI failure")

            with patch.object(store, "skip_phase", side_effect=PermissionError("private path")):
                records = ReviewJobExecutor(state_store=store).run(
                    (
                        PhaseDefinition("preflight", failed_cli),
                        PhaseDefinition(
                            "compile",
                            lambda: self.fail("blocked phase ran"),
                            depends_on=("preflight",),
                        ),
                    )
                )
            self.assertEqual(records["compile"]["status"], "skipped")
            self.assertIn("blocked by failed dependency", records["compile"]["error"])
            self.assertIn("could not be saved", records["compile"]["state_persistence_error"])
            self.assertNotIn("private", records["compile"]["state_persistence_error"])
            self.assertEqual(store.status("preflight"), "failed")

    def test_phase_definition_rejects_non_callable_resume_check(self) -> None:
        with self.assertRaisesRegex(TypeError, "resume_check must be callable"):
            PhaseDefinition("source_asr", lambda: None, resume_check=True)


if __name__ == "__main__":
    unittest.main()
