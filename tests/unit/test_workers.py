"""A background task's lead starts workers, they run at once, and it gets
their answers back -- within limits, and on Jev's say-so.

The three Tailspin people on 29 September were three independent lookups
done one after another in one context; one research turn went round in
tool calls until it was stopped. What a worker may *do* is pinned in the
gate (tests/gate/test_workers_stay_private.py); these pin how a crew runs.
"""

from __future__ import annotations

import asyncio

import pytest

from agent import workers
from core import spend
from tests.gate.decisions import install


class _Record:
    def __init__(self, cost: float) -> None:
        self.cost_usd = cost


class _Result:
    def __init__(self, output: str) -> None:
        self.output = output


@pytest.fixture
def model(monkeypatch):
    """Workers on a fake model: each task waits until released (or answers
    at once), and the most running at the same moment is counted."""
    import agent.llm
    import agent.loop

    state = {"live": 0, "most": 0, "gate": None, "cost": 0.02, "tasks": [], "units": []}

    async def run_agent(agent_, prompt, role, *, deps, **kwargs):
        state["tasks"].append(prompt)
        state["live"] += 1
        state["most"] = max(state["most"], state["live"])
        try:
            if state["gate"] is not None:
                await state["gate"].wait()
            deps.tools_used.append("web_search")
            # Where its client would bill it (core/spend.Guarded): the unit
            # of work the request is made in.
            state["units"].append(spend.current())
            return _Result(f"found: {prompt}"), _Record(state["cost"])
        finally:
            state["live"] -= 1

    monkeypatch.setattr(agent.loop, "build_agent", lambda role, **kwargs: object())
    monkeypatch.setattr(agent.llm, "run_agent", run_agent)
    return state


async def test_workers_run_at_the_same_time_and_their_answers_come_back_once(store, model):
    model["gate"] = asyncio.Event()
    crew = workers.Crew(store, session_id=1, job_id=7)
    for person in ("Hugo Lind", "Jordan Pell", "Omar Haddad"):
        assert crew.start(f"look up {person}", person)["ok"]
    await asyncio.sleep(0)
    assert model["most"] == 3, "one after another, as before"

    model["gate"].set()
    first = await crew.wait(5)
    again = await crew.wait(5)

    assert [w["result"] for w in first["workers"]] == [
        "found: look up Hugo Lind", "found: look up Jordan Pell", "found: look up Omar Haddad"]
    assert again["workers"] == [], "each answer is handed over once"
    assert first["spent_usd"] == pytest.approx(0.06)


async def test_a_crew_holds_its_limits(store, model, monkeypatch):
    model["gate"] = asyncio.Event()
    monkeypatch.setattr(workers, "MAX_LIVE", 2)
    monkeypatch.setattr(workers, "MAX_WORKERS", 3)
    crew = workers.Crew(store, session_id=1)

    assert crew.start("a")["ok"] and crew.start("b")["ok"]
    assert "already running" in crew.start("c")["error"]
    model["gate"].set()
    await crew.wait(5)
    assert crew.start("c")["ok"]
    await crew.wait(5)
    assert "at most 3" in crew.start("d")["error"]


async def test_no_new_workers_once_the_budget_is_spent(store, model):
    model["cost"] = workers.CREW_BUDGET_USD
    crew = workers.Crew(store, session_id=1)
    crew.start("a")
    await crew.wait(5)

    assert "budget" in crew.start("b")["error"]


async def test_the_same_task_twice_is_the_same_worker(store, model):
    """An escalated turn replays the lead from its history; without this,
    every worker would be started twice."""
    crew = workers.Crew(store, session_id=1)

    first = crew.start("look up Hugo Lind")
    again = crew.start("look up Hugo Lind")
    await crew.wait(5)

    assert again["worker"] == first["worker"] and again["already"]
    assert model["tasks"] == ["look up Hugo Lind"]


async def test_the_lead_can_stop_a_worker_and_the_rest_stop_when_it_answers(store, model):
    model["gate"] = asyncio.Event()  # never released
    crew = workers.Crew(store, session_id=1)
    crew.start("a")
    crew.start("b")
    await asyncio.sleep(0)

    stopped = await crew.stop(1)
    await crew.close()

    assert stopped["status"] == "stopped"
    assert [w.status for w in crew.workers] == ["stopped", "stopped"]
    assert "finished" in crew.start("c")["error"]


async def test_a_worker_that_runs_too_long_is_ended(store, model, monkeypatch):
    model["gate"] = asyncio.Event()
    monkeypatch.setattr(workers, "WORKER_TIMEOUT_S", 0.05)
    crew = workers.Crew(store, session_id=1)
    crew.start("a")

    said = await crew.wait(5)

    assert said["workers"][0]["status"] == "failed"
    assert "ran out of time" in said["workers"][0]["result"]


async def test_what_workers_spend_is_billed_to_the_job(store, model):
    with store:
        store.execute("INSERT INTO sessions (id, channel) VALUES (3, 'job')")
        store.execute("INSERT INTO jobs (id, kind, prompt) VALUES (9, 'research', 'p')")
    crew = workers.Crew(store, session_id=3, job_id=9)
    crew.start("a")
    crew.start("b")
    await crew.wait(5)

    assert [(u.category, u.session_id, u.job_id, u.store) for u in model["units"]] == [
        ("background", 3, 9, spend._file(store))] * 2


# --------------------------------------------------------------------------
# Jev decides whether the lead is offered workers
# --------------------------------------------------------------------------


async def test_the_lead_is_offered_workers_when_jev_thinks_the_task_splits(monkeypatch):
    jev = install(monkeypatch, split=0.83)

    split = await workers.worth_splitting("Look up Hugo Lind, Jordan Pell and Omar Haddad")

    assert split.offer and split.p == 0.83 and split.cost_usd == pytest.approx(8.4e-06)
    [asked] = jev.asked
    assert asked["questions"] == {"split": {"type": "noul", "instructions": workers.SPLIT}}
    assert "Omar Haddad" in asked["state"]


async def test_not_offered_when_jev_thinks_it_does_not(monkeypatch):
    install(monkeypatch, split=0.12)

    split = await workers.worth_splitting("What time is my dentist appointment?")

    assert not split.offer and split.p == 0.12


@pytest.mark.parametrize("why", ["down", "no answer", "no key"])
async def test_when_jev_cannot_be_asked_the_task_is_not_split(monkeypatch, why):
    """Offered workers with no answer, a lead split one question three ways
    on 30 September, and its workers ran the same search until each ran out
    of calls. Doing the task alone costs nothing extra, so an outage, a
    missing key or a reply with no answer is no split -- and still said, for
    the `crews` row."""
    from core import secrets

    jev = install(monkeypatch, down=RuntimeError("HTTP 503") if why == "down" else None)
    if why == "no key":
        monkeypatch.setattr(secrets, "get", lambda name: None)

    split = await workers.worth_splitting("Look up three people")

    assert not split.offer and split.p is None
    assert split.error == {"down": "RuntimeError: HTTP 503", "no answer": "split: not answered",
                           "no key": "no OpenRouter key"}[why]
    assert len(jev.asked) == (0 if why == "no key" else 1)


async def test_no_test_can_ask_the_real_jev_whether_to_split():
    """Through `core.decide._post`, which the fence refuses, from the caller's side."""
    split = await workers.worth_splitting("Look up three people")

    assert not split.offer and "Jev" in split.error


def test_the_lead_is_told_the_task_splits_and_how_sure_jev_was():
    """Only a task Jev said splits is offered workers, so the note can say so."""
    note = workers.lead_note(workers.Split(offer=True, p=0.93))

    assert "splits into independent parts" in note and "0.93" in note


# --------------------------------------------------------------------------
# The background job as lead
# --------------------------------------------------------------------------


@pytest.fixture
def job_run(store, monkeypatch):
    from agent import runtime
    from agent.worker import handlers

    seen: dict = {}

    class _Reply:
        text = "Here is what I found."

    class _Convo:
        def __init__(self, conn, channel=None, note=None, **kwargs):
            seen["note"], seen["channel"] = note, channel
            self.session_id = 5

        async def send(self, text, *, crew=None, **kwargs):
            seen["crew"] = crew
            if crew is not None:
                crew.start("a part")
            return _Reply()

        def end(self):
            seen["ended"] = True

    monkeypatch.setattr(runtime, "conn", store)
    monkeypatch.setattr(handlers, "Conversation", _Convo)
    # Browsing workers are offered to every background task apart from
    # Jev's say (tests/unit/test_browsing_worker_runs.py); off here, so these
    # are about splitting alone, whatever core/policy.yaml says.
    monkeypatch.setattr("core.browser.background", lambda: False)
    return seen


def _job(prompt: str):
    from agent import jobs

    return jobs.Job(id=11, kind="research", prompt=prompt, why="", status="running",
                    priority=0, attempts=1, max_attempts=3, session_id=None)


async def test_a_background_job_leads_a_crew_when_jev_says_so(store, job_run, model, monkeypatch):
    from agent.worker import handlers

    model["gate"] = asyncio.Event()  # the worker is still going when the lead answers
    install(monkeypatch, split=0.9)

    said = await handlers.handle_research(_job("Look up three people"))

    assert said == "Here is what I found."
    assert "lead of this background task" in job_run["note"] and "0.90" in job_run["note"]
    assert job_run["crew"] is not None
    assert [w.status for w in job_run["crew"].workers] == ["stopped"], "left running"
    row = store.execute("SELECT job_id, jev_p, offered, workers FROM crews").fetchone()
    assert tuple(row) == (11, 0.9, 1, 1)


async def test_a_job_jev_does_not_split_runs_as_it_always_did(store, job_run, monkeypatch):
    from agent.worker import handlers

    install(monkeypatch, split=0.1)

    await handlers.handle_research(_job("What is on my calendar?"))

    assert job_run["note"] is None and job_run["crew"] is None and job_run["ended"]
    row = store.execute("SELECT jev_p, offered, workers FROM crews").fetchone()
    assert tuple(row) == (0.1, 0, 0)


async def test_a_job_jev_could_not_be_asked_about_runs_alone_and_its_row_says_why(
        store, job_run, monkeypatch):
    """No split, no note, and the `crews` row keeps the reason, so outages
    can be counted against the jobs they touched."""
    from agent.worker import handlers

    install(monkeypatch, down=RuntimeError("HTTP 503"))

    await handlers.handle_research(_job("Look up three people"))

    assert job_run["note"] is None and job_run["crew"] is None and job_run["ended"]
    row = store.execute("SELECT jev_p, offered, jev_error, workers FROM crews").fetchone()
    assert tuple(row) == (None, 0, "RuntimeError: HTTP 503", 0)
