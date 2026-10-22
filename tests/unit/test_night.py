"""Free time: what it may spend, and what it tells you.

Nobody is watching while this runs, so the tests that matter are the limits.
A budget that is a target rather than a stop would be discovered days later.
"""

from __future__ import annotations

import json
from datetime import datetime, time

import pytest

from agent import night, sqltime
from agent.night import (
    Budget,
    NightReport,
    digest,
    learning,
    model,
    reading,
    seconds_until_next_window,
    tinkering,
)
from core import db


@pytest.fixture
def store(tmp_path):
    """An install from before every night these tests name: a database made
    today would owe none of them (`night.take_up`)."""
    conn = db.open_store(tmp_path / "night.db")
    with conn:
        conn.execute("UPDATE schema_version SET applied_at = '2026-01-01 00:00:00'")
    yield conn
    conn.close()


@pytest.fixture
def quiet(monkeypatch):
    """Nothing real runs: no models."""
    state = {"topics": ["how OAuth refresh rotation works"], "ask_cost": 0.01,
             "convo_cost": 0.02}

    async def fake_ask(output_type, prompt, system):
        return reading.Topics(topics=state["topics"]), state["ask_cost"]

    class FakeReply:
        text = "learned some things"

        def __init__(self, cost):
            self.cost_usd = cost

    class FakeConvo:
        def __init__(self, conn, channel=None):
            pass

        async def send(self, prompt, allow=None):
            state.setdefault("allowed", []).append(allow)
            return FakeReply(state["convo_cost"])

        def end(self):
            pass

    monkeypatch.setattr(model, "ask", fake_ask)
    import agent.loop as loop

    monkeypatch.setattr(loop, "Conversation", FakeConvo)
    # Browsing workers off: what a research turn is given with them on is
    # the gate's (tests/gate/test_browsing_workers.py).
    monkeypatch.setattr("core.browser.background", lambda: False)
    return state


# --------------------------------------------------------------------------
# The budget is a stop, not a target
# --------------------------------------------------------------------------


def test_the_budget_stops_between_steps():
    budget = Budget(1.0)
    budget.add(0.4)
    assert not budget.exhausted()
    budget.add(0.7)
    assert budget.exhausted()
    assert budget.remaining() == 0.0


async def test_research_stops_when_its_share_runs_out(store, quiet):
    """Four topics at a cost that eats the research half after two."""
    quiet["topics"] = ["a", "b", "c", "d"]
    quiet["convo_cost"] = 0.20  # research share of a $1 night is $0.50

    report = await night.run_night(store, do_learn=False, budget_usd=1.0, do_improve=False)

    assert len(report.researched) < 4
    assert "budget" in report.stopped_because


async def test_the_nights_research_has_only_the_web_and_its_memory(store, quiet):
    """Every request carried all ~77 tools' schemas to use five, and a topic
    the agent chose for itself had their mail, calendar and phone in reach."""
    from agent.night import reading

    quiet["topics"] = ["a", "b"]
    quiet["convo_cost"] = 0.01

    await night.run_night(store, do_learn=False, budget_usd=1.0, do_improve=False)

    assert quiet["allowed"] == [set(reading.RESEARCH_TOOLS)] * 2
    assert {"send_email", "create_event", "read_email"}.isdisjoint(reading.RESEARCH_TOOLS)


async def test_while_the_switch_is_off_the_night_builds_nothing_and_the_morning_says_why(
        store, quiet):
    """`self_upgrade.agent_initiated` is off by default: the night proposes
    nothing, spends nothing, and the morning says why rather than falling
    silent."""
    report = await night.run_night(store, do_learn=False, do_research=False, do_scout=False)

    assert report.improvement_paused == "self-upgrade is paused: off by default"
    assert "No modules built tonight: self-upgrade is paused" in digest(report)
    assert report.cost_usd == 0


async def test_switched_on_the_night_builds_what_it_proposes_and_says_how_each_ended(
        store, monkeypatch):
    """With the switch on, the night's proposals are module builds
    (agent/modbuild.py), each ending at a card the user decides on, and the
    morning says which. A fix's build is counted against its issue, so a
    fix that keeps failing is not paid for night after night."""
    import dataclasses

    from agent import modbuild
    from agent.night import evidence
    from core import policy as core_policy
    from core.sandbox import probe

    real = core_policy.load_policy

    def switched_on(*args, **kwargs):
        return dataclasses.replace(real(*args, **kwargs),
                                   self_upgrade=core_policy.SelfUpgradeConfig(agent_initiated=True))

    monkeypatch.setattr(core_policy, "load_policy", switched_on)
    monkeypatch.setattr(modbuild, "load_policy", switched_on)
    monkeypatch.setattr(probe, "verdict", lambda: probe.Support(True, probe.LPAC, ""))
    with store:
        issue = store.execute("INSERT INTO issues (kind, fingerprint, summary) VALUES "
                              "('tool_error', 'f1', 'the tides page never parsed')").lastrowid

    async def gathered(conn):
        return evidence.Evidence(issues=[{
            "id": issue, "kind": "tool_error", "lately": 3, "count": 3, "tries": 0,
            "last_seen": "2026-10-14 12:00:00", "summary": "the tides page never parsed",
            "excerpt": "", "trace": ""}])

    async def asked(output_type, prompt, system):
        return tinkering.Proposals(proposals=[
            tinkering.Proposal(title="Tides", fixes=issue, request="Read the tides at Tidewick"),
            tinkering.Proposal(title="Ferries", request="Read the ferry times")]), 0.01

    def built(conn, build_id, *, notify):
        state = "carded" if build_id == 1 else "failed"
        modbuild._update(conn, build_id, state=state, cost_usd=0.2,
                         said="" if state == "carded" else "the judge rejected it")
        return modbuild.get(conn, build_id)

    monkeypatch.setattr(evidence, "gather", gathered)
    monkeypatch.setattr(tinkering.model, "ask", asked)
    monkeypatch.setattr(modbuild, "build", built)
    report, budget = NightReport(started_at="03:00"), Budget(2.0)

    await tinkering.improve(store, budget, report)

    assert report.built == ["#1 Tides: its card waits for you on the Extensions page",
                            "#2 Ferries: failed, the judge rejected it"]
    origins = [r[0] for r in store.execute("SELECT origin FROM module_builds ORDER BY id")]
    assert origins == ["night", "night"]
    attempts = store.execute("SELECT issue_id, build_id, status FROM issue_attempts")
    assert [tuple(r) for r in attempts] == [(issue, 1, "carded")]
    assert budget.spent == pytest.approx(0.41)
    assert "Modules built:" in digest(report)


# --------------------------------------------------------------------------
# Nothing here is important enough to crash
# --------------------------------------------------------------------------


async def test_one_failed_topic_does_not_end_the_night(store, quiet, monkeypatch):
    quiet["topics"] = ["a", "b"]
    calls = {"n": 0}

    class Flaky:
        def __init__(self, conn, channel=None):
            pass

        # `allow` taken: without it every call failed on its arguments
        # before the first line, and the test passed with both topics
        # failed.
        async def send(self, prompt, allow=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("search is down")
            return type("R", (), {"text": "ok", "cost_usd": 0.01})()

        def end(self):
            pass

    import agent.loop as loop

    monkeypatch.setattr(loop, "Conversation", Flaky)
    report = await night.run_night(store, do_learn=False, do_improve=False)

    assert report.researched == ["a (failed: RuntimeError)", "b"]


async def test_the_night_is_recorded_even_when_nothing_happened(store, quiet):
    quiet["topics"] = []
    report = await night.run_night(store, do_learn=False, budget_usd=0.0)

    row = store.execute("SELECT * FROM jobs WHERE kind = 'night'").fetchone()
    assert row is not None
    assert json.loads(row["result"])["cost_usd"] == pytest.approx(report.cost_usd)


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "hours"),
    [("2026-09-20 01:00", 2.0), ("2026-09-20 14:30", 12.5), ("2026-09-20 03:00", 24.0)],
)
def test_the_window_opens_at_three(now, hours):
    assert seconds_until_next_window(datetime.fromisoformat(now)) == pytest.approx(
        hours * 3600, abs=1
    )


@pytest.mark.parametrize(("now", "owed"), [
    ("2026-10-11 02:59", False),  # not open yet: the loop sleeps to 3am
    ("2026-10-11 03:04", True),   # the swap that lost the 11 October night
    ("2026-10-11 05:59", True),
    ("2026-10-11 06:00", False),  # too late to be over before their morning
    ("2026-10-11 14:30", False),
])
def test_a_night_cut_short_is_started_again_while_it_can_end_before_morning(store, now, owed):
    """A worker that takes up its duties in the night runs the night the one
    before it had not finished. On 11 October 2026 a swap at 03:04 retired
    the worker four minutes into its night, and the one that took over slept
    until the 12th."""
    assert night.take_up(store, datetime.fromisoformat(now)) is owed


def test_a_night_is_started_again_once_and_not_after_one_has_finished(store):
    """A worker that keeps dying in the window must not start a fresh $2
    night each time it comes back; and a night that finished is not owed,
    though one that finished the night before does not count."""
    def finished(at):
        with store:
            store.execute("INSERT INTO jobs (kind, prompt, status, finished_at)"
                          " VALUES ('night', 'n', 'done', ?)",
                          (sqltime.stamp(datetime.fromisoformat(at)),))

    finished("2026-10-10 03:50")
    assert night.take_up(store, datetime.fromisoformat("2026-10-11 03:30"))
    assert not night.take_up(store, datetime.fromisoformat("2026-10-11 03:35")), "twice"

    finished("2026-10-12 03:55")
    assert not night.take_up(store, datetime.fromisoformat("2026-10-12 04:10"))


def test_an_install_set_up_after_the_window_opened_waits_for_the_next_night(store):
    """No worker of it was up when tonight's window opened, so no night was
    cut short. A stranger setting Steve up at 3:30 am had the night's
    research start beside their setup, choosing topics from nothing, and
    spending up to the night's $2."""
    with store:
        store.execute("UPDATE schema_version SET applied_at = ?",
                      (sqltime.stamp(datetime.fromisoformat("2026-10-11 03:20")),))
    assert not night.take_up(store, datetime.fromisoformat("2026-10-11 03:30"))
    assert night.take_up(store, datetime.fromisoformat("2026-10-12 03:30")), "the next night"


def test_the_night_runs_in_the_middle_of_whatever_quiet_hours_the_user_keeps(store,
                                                                           monkeypatch):
    """3am, fixed, was the middle of a night-shift worker's shift: the
    night's coding attempts and suites slowed their laptop while they used
    it, every night."""
    from core import call_policy

    monkeypatch.setattr(call_policy, "QUIET_FROM", time(8, 0))
    monkeypatch.setattr(call_policy, "QUIET_UNTIL", time(16, 0))

    assert seconds_until_next_window(datetime.fromisoformat("2026-10-11 03:00")) == 9 * 3600
    assert night.take_up(store, datetime.fromisoformat("2026-10-11 12:30"))
    assert not night.take_up(store, datetime.fromisoformat("2026-10-11 14:00")), "too late"
    assert not night.take_up(store, datetime.fromisoformat("2026-10-12 03:30"))


def test_a_worker_that_cannot_tell_sleeps_to_three_rather_than_lose_the_loop():
    """The night loop asks before its own try, so anything raised here would
    end the duty: no night again until the worker restarted."""
    assert night.take_up(None, datetime.fromisoformat("2026-10-11 03:30")) is False


async def test_the_worker_that_takes_over_in_the_night_runs_it_at_once(monkeypatch):
    """Then sleeps to the next 3am like any night: taking a night up is for
    the one the worker came up into, not every round."""
    import asyncio

    from agent import runtime
    from agent.worker import background

    ran: list[int] = []
    slept: list[int] = []

    async def run_night(conn):
        ran.append(len(slept))
        if len(ran) > 1:
            raise asyncio.CancelledError  # a second night without a sleep: stop it here
        return NightReport(started_at="2026-10-11 03:04")

    def next_window(now=None):
        slept.append(len(ran))
        raise asyncio.CancelledError  # the duty given back while it waits

    monkeypatch.setattr(night, "take_up", lambda conn, now=None: True)
    monkeypatch.setattr(night, "run_night", run_night)
    monkeypatch.setattr(night, "seconds_until_next_window", next_window)
    monkeypatch.setattr(runtime, "conn", None)
    monkeypatch.setattr(runtime, "listeners", set())

    with pytest.raises(asyncio.CancelledError):
        await background.night_loop()

    assert (ran, slept) == ([0], [1]), "ran before any sleep, then slept"


# --------------------------------------------------------------------------
# The morning after
# --------------------------------------------------------------------------


def test_the_digest_says_what_the_night_read_and_what_it_cost():
    """The morning's account of a night it did not watch: what was read,
    and what that cost, in their money."""
    report = NightReport(started_at="2026-09-21 03:00",
                         researched=["OAuth refresh rotation"], cost_usd=0.61)
    text = digest(report)
    assert "Read about" in text and "OAuth refresh rotation" in text and "$0.61" in text


def test_a_quiet_night_says_so_rather_than_sending_an_empty_list():
    text = digest(NightReport(started_at="2026-09-21 03:00"))
    assert "Quiet night" in text


def test_stopping_early_is_surfaced():
    report = NightReport(started_at="x", stopped_because="research budget spent")
    assert "Stopped early" in digest(report)


def test_the_digest_is_plain_enough_for_signal():
    """It goes through the markdown stripper, so it must not depend on
    formatting characters to be readable."""
    from channels.plaintext import to_plain_text

    report = NightReport(started_at="x", learned=["a"], researched=["b"], cost_usd=0.5)
    text = digest(report)
    assert to_plain_text(text).count("·") == text.count("·")


# --------------------------------------------------------------------------
# The prompts it reads at three in the morning
# --------------------------------------------------------------------------


def test_the_night_prompts_are_where_the_loader_looks():
    """They moved out of this module so the loop can tune them without a
    human merge. The cost of that is a rename failing at 3am with nobody
    watching, which is what this catches at test time instead."""
    assert len(model.prompt("night_topics").strip()) > 200


def test_every_prompt_the_night_loads_exists():
    """Four files now, all read at 3am with nobody watching. A rename would
    otherwise fail in the dark."""
    for name in ("night_topics", "night_people", "night_style", "night_heard"):
        assert len(model.prompt(name).strip()) > 100, name


async def test_learning_runs_before_anything_else(store, monkeypatch):
    """Not tidiness: this block reads *the day*, and the day is over. A
    research topic skipped tonight is still there tomorrow; an inbox read
    three nights late has lost the thread."""
    order: list[str] = []

    async def fake_learn(conn, budget, report):
        order.append("learn")

    async def fake_research(conn, budget, report):
        order.append("research")

    async def fake_improve(conn, budget, report):
        order.append("improve")

    monkeypatch.setattr(night, "learn", fake_learn)
    monkeypatch.setattr(night, "research", fake_research)
    monkeypatch.setattr(night, "improve", fake_improve)

    await night.run_night(store, budget_usd=1.0)

    assert order == ["learn", "research", "improve"]


async def test_ingest_still_runs_when_there_is_no_money_left(store, monkeypatch):
    """Ingest is pure code and costs nothing, so skipping it for budget
    would lose a day of mail to save nothing at all."""
    called: list[str] = []

    async def fake_mail(conn, **kwargs):
        called.append("mail")
        return {}

    async def fake_calendar(conn, **kwargs):
        called.append("calendar")
        return {"events": 0, "titles": [], "errors": []}

    monkeypatch.setattr("tools.ingest.mail", fake_mail)
    monkeypatch.setattr("tools.ingest.calendar", fake_calendar)
    report = night.NightReport(started_at="now")

    await night.learn(store, night.Budget(0.0), report)

    assert called == ["mail", "calendar"]
    # Counted from the ledger after every source has run, rather than taken
    # from whichever ingest reported last.
    assert "people known" in report.learned[0]


async def test_a_broken_mailbox_does_not_take_the_night_down(store, monkeypatch):
    async def explode(conn, **kwargs):
        raise RuntimeError("gmail is down")

    monkeypatch.setattr("tools.ingest.mail", explode)
    report = night.NightReport(started_at="now")

    await night.learn(store, night.Budget(1.0), report)

    assert "ingest failed" in report.learned[0]


PAID = ("_extract_commitments", "_describe_people", "_learn_from_user_words", "_learn_style")


@pytest.mark.parametrize(("night_was", "ran"), [
    ("ordinary", ["tidy", *PAID, "mirror"]),
    ("out of money", ["tidy", "mirror"]),
    ("without its mail", ["tidy", "mirror"]),
])
async def test_their_tasks_are_mirrored_whatever_stopped_the_learning(
        store, monkeypatch, night_was, ran):
    """The mirror and the memory tidy are pure code, documented as never
    skipped. Until 11 October 2026 a night whose learning money was spent,
    or whose mail would not open, returned before the mirror -- and before
    the tidy, for the mail. What costs money still waits for both."""
    done: list[str] = []

    async def mail(conn, **kwargs):
        if night_was == "without its mail":
            raise RuntimeError("gmail is down")
        return {}

    async def quiet_source(conn, **kwargs):
        return {"events": 0, "titles": [], "errors": []}

    async def tidy(conn, report):
        done.append("tidy")

    async def sync(conn):
        done.append("mirror")
        return {"mirrored": 0, "closed": 0}

    for name in PAID:
        async def step(conn, budget, report, name=name):
            done.append(name)
        monkeypatch.setattr(learning, name, step)
    monkeypatch.setattr("tools.ingest.mail", mail)
    monkeypatch.setattr("tools.ingest.calendar", quiet_source)
    monkeypatch.setattr(learning, "_tidy_memory", tidy)
    monkeypatch.setattr("agent.taskmirror.sync", sync)
    budget = night.Budget(0.0 if night_was == "out of money" else 1.0)

    await night.learn(store, budget, night.NightReport(started_at="now"))

    assert done == ran


def test_the_recipient_survives_a_comma_in_a_display_name():
    """Exchange writes "Park, Zoe" <a@contoso.example>. Splitting the header on
    the first comma gave `"park` and attached every commitment to nobody --
    which is what happened to the two the agent has actually found."""
    from tools.people import parse_addresses

    parsed = parse_addresses('"Park, Zoe" <zoe.park@contoso.example>')

    assert parsed[0][0] == "zoe.park@contoso.example"


async def test_a_commitment_from_the_night_is_attached_to_a_recipient_with_a_comma(
        store, monkeypatch):
    """Guards the specific regression: this fix was written once, lost to a
    failed assertion in the script that wrote it, and reported as shipped.

    Driven through the night's own extraction, with the mail and the model
    replaced. It read the function's source for `parse_addresses` and the
    absence of `.split(",")[0]` until 8 October 2026; a split written any
    other way would have passed that."""
    from tools import extract, people

    zoe = people.upsert(store, "zoe.park@contoso.example", name="Zoe Park")

    async def sent(conn, window):
        return [{"id": "m1", "to": '"Park, Zoe" <zoe.park@contoso.example>',
                 "body": "I'll send you the deck by Friday."}]

    async def read(message):
        said = extract.Candidate(text="Send Zoe the deck", quote="I'll send you the deck")
        return [extract.Checked(candidate=said, kept=True, reason="her words")], 0.0

    monkeypatch.setattr(learning, "_sent_for_extraction", sent)
    monkeypatch.setattr(extract, "from_message", read)

    await learning._extract_commitments(store, Budget(1.0), NightReport(started_at="03:00"))

    rows = store.execute("SELECT person_id FROM obligations").fetchall()
    assert zoe and [r["person_id"] for r in rows] == [zoe]


# --------------------------------------------------------------------------
# What the night's cap stops
# --------------------------------------------------------------------------


async def test_the_extra_dollar_is_for_building_not_research(store, quiet):
    """At $2 a night, research and learning keep the ceilings they had at
    $1; otherwise the night's larger budget would go to more research."""
    quiet["topics"] = ["a", "b", "c", "d"]
    quiet["convo_cost"] = 0.20

    report = await night.run_night(store, do_learn=False, budget_usd=2.0, do_improve=False)

    assert len(report.researched) < 4 and report.cost_usd <= night.RESEARCH_MAX_USD + 0.2


async def test_a_night_at_its_cap_stops_between_blocks_and_says_what_was_not_done(
        store, quiet, monkeypatch):
    """Its cap is the ledger's, asked before each block: research that took
    the night to its $2 leaves the changes to itself undone, and the morning
    says so, by name."""
    from core import spend

    async def costly_research(conn, budget, report):
        spend.bill(conn, 2.00, model="m")

    monkeypatch.setattr(night, "research", costly_research)

    report = await night.run_night(store, do_learn=False, budget_usd=2.0, do_scout=False)

    assert report.improvement_paused == "", "the block never started"
    assert report.stopped_because == "tonight's $2.00 limit for overnight work was reached"
    assert report.not_done == ["changes to itself"]
    text = digest(report)
    assert "Stopped early: tonight's $2.00 limit for overnight work was reached" in text
    assert "Not done: changes to itself." in text


async def test_a_night_set_to_spend_nothing_skips_its_paid_work_and_never_says_a_limit(
        store, quiet, monkeypatch):
    """Zero is the user's choice of no paid work at night. Asked of the cap,
    every night noted "its $0.00 nightly limit was reached", with an offer
    to raise it, and the morning listed every block as not done."""
    from core import knobs, spend

    knobs.write({"NIGHT_USD": 0})
    ran: list[str] = []

    async def block(conn, budget, report):
        ran.append("a paid block")

    monkeypatch.setattr(night, "learn", block)
    monkeypatch.setattr(night, "research", block)
    monkeypatch.setattr(night, "improve", block)

    report = await night.run_night(store, do_scout=False)

    assert ran == []
    assert (report.stopped_because, report.not_done) == ("finished", [])
    assert spend.notices(store) == []
    assert "Stopped early" not in digest(report)


async def test_a_night_started_again_counts_what_it_spent_before(store, quiet, monkeypatch):
    """A swap restarts the worker, and the night with it: in memory, its
    budget was a fresh $2 each time."""
    from core import spend

    with spend.spending("night", conn=store):
        spend.bill(store, 1.95, model="m")
    seen = []

    async def research(conn, budget, report):
        seen.append(budget.limit)

    monkeypatch.setattr(night, "research", research)

    await night.run_night(store, do_learn=False, budget_usd=2.0, do_improve=False,
                          do_scout=False)

    assert seen == []  # $0.05 left is under the headroom a block needs


async def test_a_cap_reached_inside_a_block_ends_the_night_with_the_reason(store, quiet,
                                                                            monkeypatch):
    """The request in flight was billed (core/spend.Guarded); the refusal of
    the next is the night's stop, not a failure."""
    from core import spend

    async def research(conn, budget, report):
        spend.bill(conn, 2.00, model="m")
        raise spend.may_spend(conn)

    monkeypatch.setattr(night, "research", research)

    report = await night.run_night(store, do_learn=False, budget_usd=2.0, do_improve=False,
                                   do_scout=False)

    assert report.stopped_because == "tonight's $2.00 limit for overnight work was reached"


def test_a_night_may_spend_what_the_policy_allows_the_pipeline_and_no_more():
    """$2: the frozen policy's cap on the pipeline's day. More would only
    be refused there; less could afford two attempts at most."""
    from core.policy import load_policy

    assert night.DEFAULT_BUDGET_USD == load_policy().coder.night_budget_usd == 2.0
