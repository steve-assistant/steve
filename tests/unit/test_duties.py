"""A worker takes up its duties when activated, and gives them back when
retired. The supervisor's half -- the order of the handover -- is pinned in
tests/gate/test_duty_handover.py."""

from __future__ import annotations

import asyncio

import pytest

import agent.server as server
from agent import approvals, runtime
from agent.worker import duties, links, routes
from core import db

# Every duty, by the module it lives in and its name there.
DUTIES = (
    ("links", "warm_voice"), ("background", "heartbeat"),
    ("background", "build_sandbox"), ("background", "build_browser"),
    ("background", "reembed"),
    ("background", "reflection_sweep"), ("background", "spoken_sweep"),
    ("background", "voice_level"),
    ("background", "night_loop"), ("background", "attention_loop"),
    ("background", "briefing_loop"),
    ("background", "reminder_loop"), ("background", "action_sweep"),
    ("background", "wake_loop"),
)
NAMES = tuple(name for _, name in DUTIES)


class _Queue:
    def __init__(self, *args, **kwargs):
        self.events: list[str] = []

    def start(self):
        self.events.append("start")

    def stop_claiming(self):
        self.events.append("stop claiming")

    async def stop(self):
        self.events.append("stop")


class _Signal:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.fixture
def worker(monkeypatch, tmp_path):
    started: list[str] = []

    def record(name):
        async def duty():
            started.append(name)
            await asyncio.sleep(3600)
        return duty

    from agent.worker import background

    homes = {"links": links, "background": background}
    for home, name in DUTIES:
        monkeypatch.setattr(homes[home], name, record(name))
    real_open = db.open_store
    monkeypatch.setattr(db, "open_store", lambda *a, **k: real_open(tmp_path / "w.db"))
    monkeypatch.setattr(server.jobs, "JobWorker", _Queue)
    monkeypatch.setattr(server.tracing, "setup", lambda: None)
    monkeypatch.delenv(duties.INERT_ENV, raising=False)
    monkeypatch.setitem(duties._duties, "state", "standby")
    monkeypatch.setattr(duties, "_duty_tasks", [])
    monkeypatch.setattr(runtime, "signal", None)
    monkeypatch.setattr(runtime, "voice", None)
    # The lifespan sets it; put back after, so no other test finds it set.
    monkeypatch.setattr(approvals, "_answers_from", None)
    return started


async def test_the_worker_counts_an_answer_only_from_a_channel_linked_now(worker):
    """Without it, an answer from a chat unpaired while it was being read
    was recorded after the take-back (approvals.decide)."""
    async with server.lifespan(server.app):
        assert approvals._answers_from is links.answers_from


async def test_a_standby_worker_does_nothing_until_activated(worker, monkeypatch):
    monkeypatch.setenv(duties.STANDBY_ENV, "1")

    async with server.lifespan(server.app):
        await asyncio.sleep(0.05)
        assert worker == [], f"a standby worker started {worker}"
        assert runtime.jobs.events == []

        response = await duties.activate(None)
        await asyncio.sleep(0.05)

        assert response.status_code == 200
        assert sorted(worker) == sorted(NAMES)
        assert runtime.jobs.events == ["start"]


async def test_retiring_hands_back_signal_and_the_queue(worker, monkeypatch):
    monkeypatch.setenv(duties.STANDBY_ENV, "1")
    signal = _Signal()

    async with server.lifespan(server.app):
        await duties.activate(None)
        monkeypatch.setattr(runtime, "signal", signal)
        tasks = list(duties._duty_tasks)

        await duties.retire(None)
        await asyncio.sleep(0.05)

        assert signal.stopped, "the retiring worker kept the Signal device"
        assert "stop claiming" in runtime.jobs.events
        assert all(t.cancelled() or t.done() for t in tasks)


async def test_a_retired_worker_does_not_come_back(worker, monkeypatch):
    """After the handover, a stray activation would put the duties back in
    two workers at once."""
    monkeypatch.setenv(duties.STANDBY_ENV, "1")

    async with server.lifespan(server.app):
        await duties.activate(None)
        await duties.retire(None)
        worker.clear()

        again = await duties.activate(None)

        assert again.status_code == 409
        assert worker == []


async def test_activating_twice_takes_up_the_duties_once(worker, monkeypatch):
    monkeypatch.setenv(duties.STANDBY_ENV, "1")

    async with server.lifespan(server.app):
        await duties.activate(None)
        await duties.activate(None)
        await asyncio.sleep(0.05)

        assert len(worker) == len(NAMES)


async def test_a_worker_run_by_hand_takes_up_its_duties_at_once(worker, monkeypatch):
    """No supervisor, nobody to activate it."""
    monkeypatch.delenv(duties.STANDBY_ENV, raising=False)

    async with server.lifespan(server.app):
        await asyncio.sleep(0.05)
        assert sorted(worker) == sorted(NAMES)


async def test_health_says_which(worker, monkeypatch):
    import json

    monkeypatch.setenv(duties.STANDBY_ENV, "1")
    async with server.lifespan(server.app):
        before = json.loads((await routes.health(None)).body)["duties"]
        await duties.activate(None)
        after = json.loads((await routes.health(None)).body)["duties"]

    assert (before, after) == ("standby", "active")


async def test_a_stopped_worker_leaves_no_closed_connection_for_what_runs_after_it(
        worker, monkeypatch):
    """Executors outside a request take the worker's connection while it is
    set (`commitment_store`); one closed and left set is a closed database
    for whatever runs next in the process."""
    from agent.toolset.clients import commitment_store

    monkeypatch.setenv(duties.STANDBY_ENV, "1")
    async with server.lifespan(server.app):
        pass

    assert runtime.conn is None
    conn, own = commitment_store()
    try:
        assert own and conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_worker_configures_its_own_logging(monkeypatch):
    """Unconfigured, INFO was dropped and every worker log was empty."""
    import logging

    calls: dict = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: calls.update(kw))

    server._configure_logging()

    assert calls["level"] == logging.INFO
    assert "%(asctime)s" in calls["format"]
    assert logging.getLogger("httpx").level == logging.WARNING
    # The client the project actually uses: its request lines were a third
    # of every worker log and carried their searches to disk (5 October 2026).
    assert logging.getLogger("httpx2").level == logging.WARNING
    # And no secret it holds (core/logscrub.py).
    assert getattr(logging.getLogRecordFactory(), "scrubs", False)


async def test_a_duty_loop_outlives_a_round_that_fails_and_paces_each_round(caplog, monkeypatch):
    """The loops run for the worker's life (`background._forever`): one bad
    round -- a locked database, a link down -- must not stop reminders until
    the next restart. And a pause worked out each round, as the briefing
    loop's is, is worked out after every round, not once."""
    from agent.worker import background

    # Each round opens a unit of work on the worker's database; an earlier
    # test's server leaves its connection closed there, and every round
    # would then fail before its step, round after round, never stopping.
    monkeypatch.setattr(runtime, "conn", None)
    rounds: list[int] = []
    pauses: list[None] = []

    async def step():
        rounds.append(len(rounds))
        if len(rounds) == 1:
            raise RuntimeError("database is locked")
        if len(rounds) == 3:
            raise asyncio.CancelledError  # the worker giving the duty back

    with pytest.raises(asyncio.CancelledError):
        await background._forever("reminder tick", step, lambda: pauses.append(None) or 0)

    assert len(rounds) == 3 and len(pauses) == 2
    assert "reminder tick failed" in caplog.text


@pytest.mark.parametrize(("loop", "what"), [("action_sweep", "retiring unanswered cards"),
                                           ("browser_sweep", "sweeping the browser")])
async def test_a_sweep_round_that_fails_is_logged_and_the_next_goes_ahead(
        loop, what, caplog, monkeypatch):
    """The card and browser sweeps swallowed every failure: a bug in either
    stopped unanswered cards retiring, or idle browsers closing, and nothing
    in the log said so. They run in `_forever` now, as the other loops do."""
    from agent.browser import session as sessions
    from agent.worker import background

    monkeypatch.setattr(runtime, "conn", None)  # as the test above says
    rounds: list[int] = []

    def fails(*_args, **_kwargs):
        rounds.append(len(rounds))
        if len(rounds) == 1:
            raise RuntimeError("database is locked")
        raise asyncio.CancelledError  # the worker giving the duty back

    async def fails_async(*args, **kwargs):
        fails(*args, **kwargs)

    async def no_pause(_seconds):
        pass

    monkeypatch.setattr(runtime, "nap", no_pause)
    monkeypatch.setattr(approvals, "expire_stale", fails)
    monkeypatch.setattr(sessions, "open_sessions", lambda: 1)
    monkeypatch.setattr(sessions, "sweep", fails_async)

    with pytest.raises(asyncio.CancelledError):
        await getattr(background, loop)()

    assert len(rounds) == 2
    assert f"{what} failed" in caplog.text


async def test_a_duty_loops_round_is_background_work(tmp_path, monkeypatch):
    """Its conversations are on channels of their own, some of them the
    user's: counted by channel, a mailbox round would have spent the user's
    allowance for talking."""
    from agent.worker import background
    from core import spend

    store = db.open_store(tmp_path / "loop.db")
    monkeypatch.setattr(runtime, "conn", store)
    seen: list = []

    async def step():
        seen.append(spend.current())
        raise asyncio.CancelledError

    try:
        with pytest.raises(asyncio.CancelledError):
            await background._forever("mailbox tick", step, 0)
        [unit] = seen
        assert (unit.category, unit.part, unit.store) == ("background", None,
                                                          spend._file(store))
    finally:
        store.close()
