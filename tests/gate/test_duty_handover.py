"""One worker holds the duties at a time.

The duties are the Signal device, the job queue, the microphone and every
background loop. A blue-green swap overlaps two workers for up to forty
seconds, and both used to hold all of it: the new one requeued the old
one's running job at startup and ran it a second time (for an approved
action, the email twice). Now the supervisor spawns workers on
standby, retires the old one's duties after the flip, and only then
activates the new one.
"""

from __future__ import annotations

import io
import os

import pytest

from agent.worker import duties
from core import supervisor, worker_handle


class _Handle:
    def __init__(self, name: str, events: list[str], *, healthy: bool = True) -> None:
        self.name = name
        self.events = events
        self.healthy = healthy
        self.port = 1
        self.alive = True
        self.inflight = 0

    async def wait_healthy(self, *args, **kwargs) -> bool:
        return self.healthy

    def last_words(self) -> str:
        return "it exited with code 1"

    async def retire_duties(self) -> bool:
        self.events.append(f"{self.name} retires")
        return True

    async def activate(self, attempts: int = 3) -> bool:
        self.events.append(f"{self.name} activates")
        return True

    async def drain(self, *args, **kwargs) -> bool:
        return True

    async def shutdown(self, *args, **kwargs) -> None:
        self.events.append(f"{self.name} shuts down")


@pytest.fixture
def events(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(supervisor.state, "active", _Handle("old", events))
    monkeypatch.setattr(supervisor.state, "draining", [])
    monkeypatch.setattr(supervisor.state, "history", [])
    return events


async def test_the_old_worker_hands_over_before_the_new_one_takes_up(events, monkeypatch):
    monkeypatch.setattr(supervisor, "spawn_worker", lambda name: _Handle("new", events))

    result = await supervisor.swap()

    assert result["ok"], result
    assert events.index("old retires") < events.index("new activates")


async def test_a_new_worker_that_never_became_healthy_is_never_activated(events, monkeypatch):
    """And the old one keeps its duties: a failed swap changes nothing."""
    monkeypatch.setattr(
        supervisor, "spawn_worker", lambda name: _Handle("new", events, healthy=False)
    )

    result = await supervisor.swap()

    assert not result["ok"]
    assert "new activates" not in events
    assert "old retires" not in events


async def test_the_first_worker_is_activated_too(events, monkeypatch):
    """At boot there is no old worker, and a worker left on standby would
    serve every request and do nothing else."""
    monkeypatch.setattr(supervisor.state, "active", None)
    monkeypatch.setattr(supervisor, "spawn_worker", lambda name: _Handle("first", events))

    await supervisor.swap()

    assert "first activates" in events


async def test_the_watchdog_activates_a_worker_left_on_standby(events, monkeypatch):
    worker = _Handle("lost", events)

    class _Response:
        def json(self):
            return {"ok": True, "duties": "standby"}

    class _Client:
        async def get(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(supervisor.state, "client", _Client())
    worker.base_url = "http://127.0.0.1:1"

    await supervisor._ensure_on_duty(worker)

    assert events == ["lost activates"]


def test_every_worker_the_supervisor_starts_is_on_standby(monkeypatch, tmp_path):
    seen: dict = {}

    class _Popen:
        def __init__(self, args, **kwargs):
            seen.update(kwargs)
            self.pid, self.stdin = os.getpid(), io.BytesIO()

        def poll(self):
            return None

    monkeypatch.setattr(worker_handle.subprocess, "Popen", _Popen)
    monkeypatch.setattr("core.paths.logs_dir", lambda: tmp_path)

    worker_handle.spawn_worker("w1-120000", env={"PATH": "x"})

    assert seen["env"][worker_handle.STANDBY_ENV] == "1"
    assert seen["env"]["PATH"] == "x"


def test_the_worker_reads_the_same_switch_the_supervisor_sets():
    assert duties.STANDBY_ENV == worker_handle.STANDBY_ENV
