"""A browsing worker reads the sites it was given, holds nothing of theirs, and
can do nothing.

The maintainer, 1 October 2026, for background tasks: clean-room browsing workers.
Only a lead starts one: at first only for a task the user asked for; since 11
October 2026, once the injection screen enforced, also for the attention
loop's doer, the night's research and -- that evening -- a task the agent
set itself, each from pages its own searches returned and never from an
address in what it was handed;
it has the web and the browser's reading commands and nothing else -- no
mail, calendar, files, memory or people, no action of any kind; it opens
only pages the lead was shown, public ones, and its browser is kept to their
sites; it never logs in, fills in a form or handles a file; at most two run
at once; and the lead, every other worker and every other unattended run
still cannot browse at all (tests/gate/test_browser_consent.py). Built on 4
October; pinned here, frozen, because agent/workers.py and
agent/toolset/browser.py are not.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

import core.browser
from agent import actions, workers
from agent.browser import session as sessions
from agent.toolset import browser
from agent.turn import Deps
from core.consent import classify

PAGE = "https://93.184.216.34/hours"  # a bare public address: nothing is looked up
SITE = "93.184.216.34"
CLEAN = dict(channel="job", worker=True, browse_sites=(SITE,), browser_key=-7)


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps
        self.usage = type("U", (), {"tool_calls": 0})()


def _tools() -> dict:
    found: dict = {}
    browser.register(lambda fn: found.setdefault(fn.__name__, fn))
    return found


@pytest.fixture
def opened(monkeypatch):
    """Every browser a tool would open, recorded: under which key, logged in
    to what, kept to which sites."""
    asked: list[dict] = []

    class _Session:
        box = type("B", (), {"session": "s"})()
        steps = 1
        site = ""

        async def ask(self, cmd, **args):
            return {"ok": True, "url": PAGE, "title": "t", "text": "", "elements": []}

    async def session_for(conversation, site=None, *, domains=()):
        asked.append({"key": conversation, "site": site, "domains": tuple(domains)})
        return _Session()

    monkeypatch.setattr(sessions, "session_for", session_for)
    monkeypatch.setattr(sessions, "current", lambda key: None)
    monkeypatch.setattr(core.browser, "enabled", lambda: True)
    monkeypatch.setattr(core.browser, "background", lambda: True)
    # Bare addresses are refused outright by the egress rule; here it is the
    # sites a worker is kept to that are under test.
    monkeypatch.setattr("core.egress.refusal", lambda url: None)
    return asked


def test_it_holds_nothing_of_theirs_and_no_tool_of_it_acts():
    """Every tool it is given is a read: none is gated, none runs an action,
    and none reaches their mail, calendar, files, memory or people."""
    from pydantic_ai import Agent

    from agent.toolset import register_tools

    agent = Agent("test", deps_type=Deps)
    register_tools(agent, allow=set(workers.BROWSING_TOOLS))
    given = set(agent._function_toolset.tools)

    assert {"browse_open", "browse_read"} <= given
    assert not given & set(actions.EXECUTORS), "no tool of it runs an action"
    assert not any(classify(name, {}).gated for name in given)
    private = ("email", "calendar", "drive", "spreadsheet", "recall", "remember", "who_is",
               "commitment", "task", "reminder", "draft", "event", "document", "code",
               "submit", "login", "upload", "download", "files", "worker", "style")
    assert not [name for name in given if any(word in name for word in private)]


async def test_only_a_lead_given_browsing_workers_may_start_one():
    crew = workers.Crew(None, 1, seen={PAGE}, browsing=False)

    said = await crew.start_browsing("Read the hours.", [PAGE])

    assert said["ok"] is False and "not given" in said["error"] and not crew.workers


async def test_it_reads_only_pages_the_lead_was_shown(monkeypatch):
    monkeypatch.setattr(core.browser, "background", lambda: True)
    monkeypatch.setattr("core.egress.refusal", lambda url: None)
    crew = workers.Crew(None, 1, seen={PAGE}, browsing=True)

    said = await crew.start_browsing("Read it.", ["https://203.0.113.9/elsewhere"])

    assert said["ok"] is False and "the user named" in said["error"] and not crew.workers


async def test_it_reads_only_public_pages(monkeypatch):
    monkeypatch.setattr(core.browser, "background", lambda: True)
    crew = workers.Crew(None, 1, seen={"http://192.168.1.1/admin"}, browsing=True)

    said = await crew.start_browsing("Read it.", ["http://192.168.1.1/admin"])

    assert said["ok"] is False and not crew.workers


async def test_its_browser_is_its_own_kept_to_its_sites_and_never_logged_in(opened,
                                                                            monkeypatch):
    """Even on a site the user has a saved login for, which opens freely in their
    own conversations."""
    from core.browser import vault

    saved = type("Site", (), {"name": "museum", "tier": "free", "domains": (SITE,)})()
    monkeypatch.setattr(vault, "vault", lambda: type("V", (), {
        "site_for": lambda self, url: saved, "site": lambda self, name: saved})())
    tools = _tools()
    deps = Deps(conn=None, session_id=1, **CLEAN)

    off = await tools["browse_open"](_Ctx(deps), url="https://203.0.113.9/other")
    on = await tools["browse_open"](_Ctx(deps), url=PAGE)

    assert off["ok"] is False and "kept to" in off["error"]
    assert on["ok"] is True
    assert opened == [{"key": -7, "site": None, "domains": (SITE,)}], (
        "its own key, never the lead's conversation; no login; kept to its site")


@pytest.mark.parametrize("name", ["browse_use_login", "browse_login", "browse_submit",
                                  "browse_upload", "browse_files", "read_download"])
async def test_it_never_logs_in_fills_in_or_handles_files(name, opened):
    args = {"browse_use_login": {"site": "contoso", "purpose": "x"},
            "browse_login": {"user_ref": "e1", "pass_ref": "e2"},
            "browse_submit": {"fields": [{"ref": "e1", "value": "x"}], "button": "e2"},
            "browse_upload": {"ref": "e1", "file": "outbox/cv.pdf"},
            "read_download": {"name": "1-x.pdf"}}.get(name, {})

    said = await _tools()[name](_Ctx(Deps(conn=None, session_id=1, **CLEAN)), **args)

    assert said["ok"] is False and said["error"] == browser.CLEAN_ROOM and opened == []


def test_the_lead_and_every_other_worker_still_cannot_browse(monkeypatch):
    monkeypatch.setattr(core.browser, "enabled", lambda: True)
    monkeypatch.setattr(core.browser, "background", lambda: True)
    assert browser.refusal(Deps(conn=None, session_id=1, channel="job"))
    assert browser.refusal(Deps(conn=None, session_id=1, channel="job", worker=True))
    assert browser.refusal(Deps(conn=None, session_id=1, **CLEAN)) is None


async def test_at_most_two_run_at_once(monkeypatch):
    monkeypatch.setattr(core.browser, "background", lambda: True)
    monkeypatch.setattr(workers, "_browsing", set())
    monkeypatch.setattr("core.egress.refusal", lambda url: None)

    async def runs(self, worker):
        await asyncio.sleep(3600)

    monkeypatch.setattr(workers.Crew, "_run", runs)
    crew = workers.Crew(None, 1, seen={PAGE}, browsing=True)

    said = [await crew.start_browsing(f"Read part {n}.", [PAGE]) for n in (1, 2, 3)]
    await crew.close()

    assert [s["ok"] for s in said] == [True, True, False]
    assert "already running" in said[2]["error"]


async def test_the_switch_turns_them_off(monkeypatch):
    from core import policy

    real = policy.load_policy()
    off = dataclasses.replace(real, browser=dataclasses.replace(real.browser, background=False))
    monkeypatch.setattr("core.policy.load_policy", lambda *a, **k: off)
    crew = workers.Crew(None, 1, seen={PAGE}, browsing=True)

    assert core.browser.background() is False
    assert "off" in (await crew.start_browsing("Read it.", [PAGE]))["error"]
    assert "off" in browser.refusal(Deps(conn=None, session_id=1, **CLEAN))


# --------------------------------------------------------------------------
# The doer, the night's research and the agent's own tasks (the user, 11
# October 2026)
# --------------------------------------------------------------------------

# An address in what an unattended turn was handed: an email's preview, a
# topic the agent chose, a task it wrote. Not one it was shown.
HANDED = "https://93.184.216.34/rsvp"


@pytest.fixture
def turns(monkeypatch):
    """Every unattended turn sent, with the tools and the crew it was given.
    Each tries a browsing worker on the address in its own text, as a turn
    steered by that text would."""
    import agent.loop
    from agent.worker import handlers

    sent: list[dict] = []

    class Convo:
        session_id, note, mandate = 1, None, None

        def __init__(self, conn, *, channel, note=None, mandate=None):
            self.conn, self.channel = conn, channel
            self.note, self.mandate = note, mandate

        async def send(self, text, *, allow=None, crew=None, seen=None):
            tried = await crew.start_browsing("Read it.", [HANDED]) if crew else None
            sent.append({"allow": allow, "crew": crew, "seen": seen, "tried": tried,
                         "note": self.note})

            class Reply:
                text, tools_used, cost_usd = "NOTHING TO DO", [], 0.0

            return Reply()

        def end(self):
            pass

    monkeypatch.setattr(agent.loop, "Conversation", Convo)
    monkeypatch.setattr(handlers, "Conversation", Convo)
    monkeypatch.setattr(core.browser, "background", lambda: True)
    monkeypatch.setattr("core.egress.refusal", lambda url: None)
    return sent


async def _the_doer(tmp_path) -> frozenset:
    from agent.attention import prepare, store, triage
    from core import db

    conn = db.open_store(tmp_path / "doer.db")
    try:
        [obs] = store.record_new(conn, [store.Observation("gmail", "mail:x", f"RSVP: {HANDED}")])
        triage.apply(conn, [obs], [triage.Verdict(key="mail:x", say="now",
                                                  task=f"Read {HANDED} and add the event")])
        await prepare.prepare(conn)
        [judged] = store.get(conn, [obs.id])
    finally:
        conn.close()
    return prepare.tools_for(judged)


async def _the_night(tmp_path, monkeypatch) -> frozenset:
    from agent.night import model, reading
    from agent.night.report import Budget, NightReport
    from core import db

    async def topics(output_type, prompt, system):
        return reading.Topics(topics=[f"the page at {HANDED}"]), 0.0

    monkeypatch.setattr(model, "ask", topics)
    conn = db.open_store(tmp_path / "night.db")
    try:
        await reading.research(conn, Budget(1.0), NightReport(started_at="03:00"))
    finally:
        conn.close()
    return reading.RESEARCH_TOOLS


@pytest.mark.parametrize("run", ["doer", "night"])
async def test_the_doer_and_the_nights_research_may_start_one_from_their_own_searches_only(
        run, turns, tmp_path, monkeypatch):
    """Their text is an email's preview or a topic the agent chose, never their
    words, so their crew starts with nothing shown: an address in that text
    is refused, and what their own searches return is what a worker may
    open. It may browse and never split -- a worker of theirs with their mail
    would be a second doer nobody bounded."""
    tools = await (_the_doer(tmp_path) if run == "doer" else _the_night(tmp_path, monkeypatch))

    [turn] = turns
    crew = turn["crew"]
    assert turn["allow"] == set(tools) | workers.LEAD_BROWSING_TOOLS
    assert crew.browsing and not crew.split
    assert crew.seen is turn["seen"], "the turn's own searches are what a worker may open"
    assert turn["tried"]["ok"] is False and "search for it" in turn["tried"]["error"]
    assert not crew.workers and crew.closed, "stopped when the turn answers"
    assert "start_browsing_worker" in turn["note"]


@pytest.mark.parametrize("run", ["doer", "night"])
async def test_with_the_switch_off_the_doer_and_the_night_have_no_crew(
        run, turns, tmp_path, monkeypatch):
    monkeypatch.setattr(core.browser, "background", lambda: False)

    tools = await (_the_doer(tmp_path) if run == "doer" else _the_night(tmp_path, monkeypatch))

    [turn] = turns
    assert turn["crew"] is None and turn["allow"] == set(tools)


async def test_a_task_the_agent_set_itself_may_start_one_from_its_own_searches_only(
        turns, tmp_path, monkeypatch):
    """Its prompt was written by a model -- the night's, the doer's, one that
    may just have read a page telling it what to queue -- so, as for the
    doer, an address in it is refused (`handlers.user_urls` gives it none),
    and what its own searches return is what a worker may open."""
    from agent import jobs, runtime
    from agent.worker import handlers
    from core import db

    async def no_split(task):
        return workers.Split(offer=False)

    monkeypatch.setattr(workers, "worth_splitting", no_split)
    conn = db.open_store(tmp_path / "jobs.db")
    monkeypatch.setattr(runtime, "conn", conn)
    try:
        job = jobs.enqueue(conn, kind="research", prompt=f"Read {HANDED} and add the event")
        await handlers.handle_research(jobs.get(conn, job))
    finally:
        conn.close()

    [turn] = turns
    crew = turn["crew"]
    assert crew.browsing and crew.seen == set() and crew.seen is turn["seen"]
    assert turn["tried"]["ok"] is False and "search for it" in turn["tried"]["error"]
    assert not crew.workers and crew.closed, "stopped when the task answers"
    assert "start_browsing_worker" in turn["note"]
