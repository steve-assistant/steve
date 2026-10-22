"""The background work queue.

This is what makes "hand it off and keep talking" real. The fast model decides
a task is too heavy for the reply path, enqueues it, and answers immediately;
a worker picks it up on a stronger model while the conversation continues.

SQLite is the queue. For one user that is not a compromise -- it is durable
across restarts, it needs no broker, and `UPDATE ... RETURNING` gives atomic
claim semantics without a lock table.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core import db, spend

log = logging.getLogger(__name__)

# Jobs a worker started but never finished -- almost always a process death
# mid-run. On startup they are returned to the queue rather than left to sit in
# 'running' forever.
STALE_STATUSES = ("running",)

# How often a running job worker looks for jobs whose claimant died. A
# swap's old worker can die mid-job after the new one started, and the new
# one's startup sweep has already run by then.
REQUEUE_EVERY_S = 60

# What a job's model calls are billed to (core/spend.py), by its kind.
# Routines and goals each have their own day inside background's. Any other
# kind is background work. The job runner starts at boot, in no unit, so a
# job never inherits the chat that asked for it: its spending is
# background's, as the user's allowance for talking is not the work's.
SPENDS_AS = {"routine": ("background", "routines"), "goal": ("background", "goals")}


@dataclass
class Job:
    id: int
    kind: str
    prompt: str
    why: str | None
    status: str
    priority: int
    attempts: int
    max_attempts: int
    session_id: int | None
    result: str | None = None
    error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            kind=row["kind"],
            prompt=row["prompt"],
            why=row["why"],
            status=row["status"],
            priority=row["priority"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            session_id=row["session_id"],
            result=row["result"],
            error=row["error"],
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "prompt": self.prompt,
            "why": self.why,
            "status": self.status,
            "attempts": self.attempts,
            "result": self.result,
            "error": self.error,
        }


def enqueue(
    conn: sqlite3.Connection,
    *,
    kind: str,
    prompt: str,
    why: str | None = None,
    session_id: int | None = None,
    priority: int = 0,
    model: str | None = None,
) -> int:
    with db.transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO jobs (session_id, kind, prompt, why, priority, model)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, kind, prompt, why, priority, model),
        )
    return int(cur.lastrowid)


def claim(conn: sqlite3.Connection) -> Job | None:
    """Atomically take the next queued job.

    The UPDATE...RETURNING against a subselect is what makes this safe with
    several workers: whichever transaction commits first owns the row, and the
    others see no queued job rather than a duplicate.
    """
    with db.transaction(conn):
        row = conn.execute(
            """
            UPDATE jobs
               SET status = 'running',
                   started_at = datetime('now'),
                   attempts = attempts + 1,
                   claimed_by = ?
             WHERE id = (
                   SELECT id FROM jobs
                    WHERE status = 'queued'
                    ORDER BY priority DESC, id
                    LIMIT 1
             )
            RETURNING *
            """,
            (this_process(),),
        ).fetchone()
    return Job.from_row(row) if row else None


def this_process() -> str:
    """Who is claiming: pid and start time, because a pid alone is reused."""
    import os

    import psutil

    return f"{os.getpid()}:{psutil.Process().create_time():.0f}"


def _claimant_alive(claimed_by: str | None) -> bool:
    """Is the process that claimed a job still running?

    No claimant is a row from before claims were recorded, and is treated as
    orphaned -- which is exactly how every running row was treated then.
    """
    import psutil

    if not claimed_by:
        return False
    pid_text, _, started = claimed_by.partition(":")
    try:
        process = psutil.Process(int(pid_text))
        return abs(process.create_time() - float(started)) < 2
    except (ValueError, psutil.Error):
        return False


def complete(conn: sqlite3.Connection, job_id: int, result: str) -> None:
    with db.transaction(conn):
        conn.execute(
            """
            UPDATE jobs SET status = 'done', result = ?, finished_at = datetime('now')
             WHERE id = ?
            """,
            (result, job_id),
        )


def fail(conn: sqlite3.Connection, job_id: int, error: str, *, retry: bool = True) -> str:
    """Record a failure, requeueing while attempts remain -- unless `retry`
    is False, for a failure that another attempt would only repeat.

    Returns the resulting status so the caller can tell a retry from a final
    failure without a second query.
    """
    with db.transaction(conn):
        row = conn.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return "missing"
        exhausted = not retry or row["attempts"] >= row["max_attempts"]
        status = "failed" if exhausted else "queued"
        conn.execute(
            """
            UPDATE jobs
               SET status = ?, error = ?,
                   finished_at = CASE WHEN ? = 'failed' THEN datetime('now') ELSE NULL END
             WHERE id = ?
            """,
            (status, error[:4000], status, job_id),
        )
    return status


def requeue_stale(conn: sqlite3.Connection) -> int:
    """Return jobs orphaned by a dead worker to the queue.

    Only jobs whose claimant is gone. During a blue-green swap the old worker
    is alive and may still be finishing a job, and the new worker used to call
    this at startup and requeue it regardless -- then claim it and run it a
    second time alongside the first. For an approved action that is the email
    sent twice; for the self-upgrade it was the duplicate attempts 44/45 and
    50/51 and the worktrees they left. Safe to call at any time, which is why
    the job worker also calls it periodically: a swap's old worker can die
    mid-job after the new one has started.

    Bounded by `max_attempts`, which it used not to be -- and that turned a
    restart loop into an unbounded one. A worker that dies mid-job is
    restarted, requeues the job, claims it, dies again; without this bound the
    attempt count climbs forever. An upgrade job is minutes of model time and
    a git worktree each time round, so eleven of them ran concurrently before
    anyone noticed.

    A job past its attempts is marked failed instead, which is the honest
    outcome: it has now been tried as often as it was ever going to be.
    """
    placeholders = ",".join("?" for _ in STALE_STATUSES)
    rows = conn.execute(
        f"SELECT id, claimed_by FROM jobs WHERE status IN ({placeholders})",
        STALE_STATUSES,
    ).fetchall()
    orphans = [r["id"] for r in rows if not _claimant_alive(r["claimed_by"])]
    if not orphans:
        return 0
    ids = ",".join("?" for _ in orphans)
    with db.transaction(conn):
        cur = conn.execute(
            f"""
            UPDATE jobs
               SET status = 'queued', started_at = NULL, claimed_by = NULL
             WHERE id IN ({ids})
               AND attempts < max_attempts
            """,
            orphans,
        )
        requeued = cur.rowcount
        conn.execute(
            f"""
            UPDATE jobs
               SET status = 'failed',
                   error = COALESCE(error, 'abandoned after ' || attempts
                                    || ' attempts; the worker kept restarting'),
                   finished_at = datetime('now')
             WHERE id IN ({ids})
               AND attempts >= max_attempts
            """,
            orphans,
        )
    return requeued


def pending_notifications(conn: sqlite3.Connection, *, within_hours: int | None = None
                          ) -> list[Job]:
    """Finished jobs the user has not been told about, oldest first -- within the
    last `within_hours` if given (both sides SQLite UTC)."""
    since = "" if within_hours is None else "AND finished_at >= datetime('now', ?)"
    rows = conn.execute(
        f"""
        SELECT * FROM jobs
         WHERE notified = 0 AND status IN ('done', 'failed') {since}
         ORDER BY finished_at
        """,
        () if within_hours is None else (f"-{int(within_hours)} hours",),
    ).fetchall()
    return [Job.from_row(r) for r in rows]


def mark_notified(conn: sqlite3.Connection, job_ids: list[int]) -> None:
    if not job_ids:
        return
    with db.transaction(conn):
        conn.executemany(
            "UPDATE jobs SET notified = 1 WHERE id = ?", [(i,) for i in job_ids]
        )


def get(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[Job]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [Job.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

JobHandler = Callable[[Job], Awaitable[str]]


def _told(hook: Callable[[Job], Any] | None, job: Job) -> None:
    """A hook told of a job; one that fails is logged, not raised. The loop
    runs one job after another, and an exception out of `_run_one` would end
    it, and every job after it."""
    if hook is None:
        return
    try:
        hook(job)
    except Exception:  # noqa: BLE001 - a working line is not worth the queue
        log.exception("a job hook failed on job %s", job.id)


class JobWorker:
    """Polls the queue and runs jobs through a handler.

    Polling rather than notifying: at one job every few minutes the simplicity
    is worth more than the latency, and a poll loop has no wakeup to lose.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        handlers: dict[str, JobHandler],
        *,
        poll_interval_s: float = 1.0,
        on_finish: Callable[[Job], Any] | None = None,
        on_start: Callable[[Job], Any] | None = None,
        on_stop: Callable[[Job], Any] | None = None,
    ) -> None:
        self.conn = conn
        self.handlers = handlers
        self.poll_interval_s = poll_interval_s
        self.on_finish = on_finish
        # Told as a handler takes a job up and as it stops, however it ends:
        # the app's working line (agent/worker/journal.py). `on_finish` hears
        # only of a result, and a job to be retried or stopped at a cap has
        # none, so a line cleared there would have stayed up.
        self.on_start = on_start
        self.on_stop = on_stop
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _run_one(self, job: Job) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            fail(self.conn, job.id, f"no handler registered for kind {job.kind!r}")
            return
        category, part = SPENDS_AS.get(job.kind, ("background", None))
        _told(self.on_start, job)
        try:
            with spend.spending(category, part=part, conn=self.conn, job_id=job.id):
                result = await handler(job)
        except spend.Refused as refusal:
            # A spending cap reached: tried again it is refused again, and
            # announced it is a failure the user did not cause. It stops, and
            # the next briefing says why (agent/caps.py).
            fail(self.conn, job.id, f"stopped at a spending cap: {refusal}", retry=False)
            mark_notified(self.conn, [job.id])  # or the resend sweep would announce it
            return
        except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
            status = fail(self.conn, job.id, f"{type(exc).__name__}: {exc}")
            if status == "failed" and self.on_finish:
                self.on_finish(get(self.conn, job.id))
            return
        finally:
            _told(self.on_stop, job)
        complete(self.conn, job.id, result)
        if self.on_finish:
            self.on_finish(get(self.conn, job.id))

    async def _loop(self) -> None:
        last_sweep = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_sweep >= REQUEUE_EVERY_S:
                requeue_stale(self.conn)
                last_sweep = time.monotonic()
            job = claim(self.conn)
            if job is None:
                # Sleep until the next poll, but wake immediately on stop().
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_s)
                continue
            await self._run_one(job)

    def start(self) -> None:
        requeue_stale(self.conn)
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    def stop_claiming(self) -> None:
        """Take nothing new; a job already running finishes. For a worker
        that is handing over to its replacement."""
        self._stop.set()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=30)
            self._task = None


def summarise_queue(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "raw": json.dumps(counts),
    }
