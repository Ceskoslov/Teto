import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evaluator.job_store import IdempotencyConflict, JobStore


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "jobs.sqlite"
        self.store = JobStore(self.path)

    def submit(self, key="submission", fingerprint="digest"):
        return self.store.submit(
            key, fingerprint, {"kind": "import"}, {"input.json": b'{}\r\n'}
        )

    def test_concurrent_retries_create_one_job(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            job_ids = list(pool.map(lambda _: self.submit(), range(16)))
        job_id = job_ids[0]
        self.assertEqual(set(job_ids), {job_id})
        with self.assertRaises(IdempotencyConflict):
            self.submit(fingerprint="different")
        with self.store.worker():
            item = self.store.claim()
            self.assertEqual(item.id, job_id)
            self.assertEqual(item.files["input.json"], b'{}\r\n')
            self.assertIsNone(self.store.claim())
            self.store.finish(job_id, {"score.json": b"{}"}, None)
        reopened = JobStore(self.path)
        self.assertEqual(reopened.status(job_id)["status"], "succeeded")
        self.assertEqual(reopened.file(job_id, "score.json"), b"{}")

    def test_recovery_keeps_queued_jobs_and_does_not_replay_claimed_jobs(self):
        interrupted = self.submit("interrupted")
        queued = self.submit("queued")
        with self.store.worker():
            self.assertEqual(self.store.claim().id, interrupted)
            with self.assertRaises(BlockingIOError), JobStore(self.path).worker():
                self.fail("a second worker acquired the lock")
        reopened = JobStore(self.path)
        with reopened.worker():
            status = reopened.status(interrupted)
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["error"], {"code": "WORKER_INTERRUPTED"})
            self.assertEqual(reopened.file(interrupted, "input.json"), b'{}\r\n')
            self.assertEqual(reopened.claim().id, queued)
            self.assertIsNone(reopened.claim())

    def test_report_write_and_terminal_status_commit_together(self):
        job_id = self.submit()
        with self.store.worker():
            self.store.claim()
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.finish(job_id, {"score.json": b"{}", "input.json": b"replaced"}, None)
            self.assertEqual(self.store.status(job_id)["status"], "running")
            self.assertIsNone(self.store.file(job_id, "score.json"))
            self.assertEqual(self.store.file(job_id, "input.json"), b'{}\r\n')
