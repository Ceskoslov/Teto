"""Persistent queue and evidence storage for the evaluation service."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class IdempotencyConflict(Exception):
    """The key already belongs to a different request."""


@dataclass(frozen=True)
class WorkItem:
    id: str
    options: dict
    files: dict[str, bytes]


class JobStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    submission_key TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    options TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued', 'running', 'succeeded', 'failed')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status);
                CREATE TABLE IF NOT EXISTS job_files (
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    name TEXT NOT NULL,
                    content BLOB NOT NULL,
                    PRIMARY KEY (job_id, name)
                );
            """)

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def _existing(db, key: str, fingerprint: str) -> str | None:
        row = db.execute(
            "SELECT id, fingerprint FROM jobs WHERE submission_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise IdempotencyConflict
        return row["id"]

    def existing(self, key: str, fingerprint: str) -> str | None:
        with self._connect() as db:
            return self._existing(db, key, fingerprint)

    @staticmethod
    def _write_files(db, job_id: str, files: dict[str, bytes]) -> None:
        db.executemany(
            "INSERT INTO job_files (job_id, name, content) VALUES (?, ?, ?)",
            ((job_id, name, content) for name, content in files.items()),
        )

    def submit(
        self, key: str, fingerprint: str, options: dict, files: dict[str, bytes]
    ) -> str:
        with self._connect(write=True) as db:
            existing = self._existing(db, key, fingerprint)
            if existing is not None:
                return existing
            job_id = uuid4().hex
            db.execute(
                """INSERT INTO jobs (id, submission_key, fingerprint, kind, options)
                   VALUES (?, ?, ?, ?, ?)""",
                (job_id, key, fingerprint, options["kind"], json.dumps(options)),
            )
            self._write_files(db, job_id, files)
            return job_id

    def status(self, job_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT id, kind, status, created_at, started_at, finished_at, error
                   FROM jobs WHERE id = ?""",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["error"] = json.loads(row["error"]) if row["error"] else None
        for field in ("created_at", "started_at", "finished_at"):
            if result[field] is not None:
                result[field] = result[field].replace(" ", "T") + "Z"
        return result

    def file(self, job_id: str, name: str) -> bytes | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT content FROM job_files WHERE job_id = ? AND name = ?",
                (job_id, name),
            ).fetchone()
        return None if row is None else row["content"]

    def claim(self) -> WorkItem | None:
        with self._connect(write=True) as db:
            row = db.execute(
                "SELECT id, options FROM jobs WHERE status = 'queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            job_id = row["id"]
            db.execute(
                "UPDATE jobs SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )
            files = dict(
                db.execute(
                    "SELECT name, content FROM job_files WHERE job_id = ?", (job_id,)
                )
            )
            return WorkItem(job_id, json.loads(row["options"]), files)

    def record_run(self, job_id: str, content: bytes) -> None:
        with self._connect(write=True) as db:
            self._write_files(db, job_id, {"run.json": content})

    def finish(self, job_id: str, files: dict[str, bytes], error: dict | None) -> None:
        with self._connect(write=True) as db:
            updated = db.execute(
                """UPDATE jobs SET status = ?, error = ?, finished_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'running'""",
                (
                    "succeeded" if error is None else "failed",
                    None if error is None else json.dumps(error),
                    job_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("job is not running")
            self._write_files(db, job_id, files)

    @contextmanager
    def worker(self) -> Iterator[None]:
        """Only one worker owns this queue and recovers interrupted jobs."""
        import fcntl

        lock = self.path.with_name(self.path.name + ".worker.lock")
        with lock.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A dead worker may have spent live budget before publishing evidence.
            # Surviving queued work is safe to start. Never replay a claimed job.
            with self._connect(write=True) as db:
                db.execute(
                    """UPDATE jobs SET status = 'failed', error = ?,
                              finished_at = CURRENT_TIMESTAMP WHERE status = 'running'""",
                    (json.dumps({"code": "WORKER_INTERRUPTED"}),),
                )
            yield
