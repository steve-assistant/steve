"""The limits on what the agent does unasked.

The attention loop (`agent/attention/`) runs every minute with nobody
watching: it reads their mail and calendar, decides what matters, prepares
things with their tools and messages them. Everything it does is approval-tier
code the agent could one day propose to change -- so the bounds that make it
safe to leave running are pinned here, in a frozen file the gate runs
before any change can merge.

  **What the doer may touch.** Only tools that are free, private and
  reversible in the frozen consent file: it can draft but never send, add
  an event with no guests but never invite, and never call. Since 11
  October 2026 that includes starting a clean-room browsing worker
  (tests/gate/test_browsing_workers.py).

  **Who it is.** Its conversations run on a channel the frozen channel rule
  calls autonomous, so nothing it says can approve a card, start a call or
  file a change to the agent as the user's request.

  **Ask first.** The user's rule, 24 September 2026: what could need their own
  words -- a thank-you note after a meeting, a reply the user owes -- is asked
  about and never drafted unasked.

  **The night.** The user's rule, 24 September 2026: pings at night only for
  something critical, never a call. Tested by driving the real delivery
  code at 3am.

  **The numbers.** Every cap and budget is set, finite, and below a ceiling
  written here -- so "just raise the limit" is a human edit to this file.
"""

from __future__ import annotations

import datetime as dt

import pytest

from agent import modtools, workers
from agent.attention import deliver, prepare, settings, store, triage
from core import call_policy, db, knobs, spend
from core.call_policy import is_autonomous
from core.consent import Reach, Reversal, classify, load_rules
from tests.fakes import google as fake_google

THREE_AM = dt.datetime(2026, 9, 25, 3, 0)
AFTERNOON = dt.datetime(2026, 9, 24, 14, 0)


# --------------------------------------------------------------------------
# What the doer may touch
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(prepare.PREPARE_TOOLS | workers.LEAD_BROWSING_TOOLS))
def test_every_tool_the_doer_holds_is_free_private_and_reversible(tool):
    """Classified with no arguments, the way the frozen file states the
    tool's nature; an argument that would make it outward (guests on an
    event) is gated by consent at call time, on top of this. A built-in
    module's tool is classed by its manifest, checked against its grants."""
    if tool.startswith(modtools.PREFIX):
        assert tool in modtools.free_builtins(), f"{tool} is not a free read"
        return
    consent = classify(tool, {})

    assert tool in load_rules().free, f"{tool} is not classified free"
    assert consent.free and consent.reach is Reach.PRIVATE
    assert consent.reversal is Reversal.REVERSIBLE


def test_the_doer_holds_nothing_that_sends_deletes_spends_or_changes_the_agent():
    never = {
        "send_email", "send_draft", "delete_event", "update_event", "trash_file", "purchase",
        "build_module", "run_code", "defer_to_background", "remember", "update_spreadsheet",
        "modify_email", "delete_draft",
    }

    held = set(prepare.PREPARE_TOOLS) | workers.LEAD_BROWSING_TOOLS
    assert held & never == set()
    assert held.isdisjoint(load_rules().gated)


@pytest.mark.parametrize("browsing", [False, True])
def test_the_doer_is_given_exactly_its_tools(tmp_path, monkeypatch, browsing):
    """The allowlist has to be *passed*: a `Conversation` sent without
    `allow` holds the whole toolset. With browsing workers on, the lead's
    tools for them too, and nothing else."""
    import asyncio

    import agent.loop
    import core.browser

    monkeypatch.setattr(core.browser, "background", lambda: browsing)
    sent: list = []

    class Convo:
        session_id, note, mandate = 1, None, None

        def __init__(self, conn, *, channel):
            self.conn, self.channel = conn, channel

        async def send(self, text, *, allow=None, crew=None, seen=None):
            sent.append((self.channel, allow))

            class Reply:
                text, tools_used, cost_usd = "NOTHING TO DO", [], 0.0

            return Reply()

        def end(self):
            pass

    monkeypatch.setattr(agent.loop, "Conversation", Convo)
    conn = db.open_store(tmp_path / "bounds.db")
    try:
        [obs] = store.record_new(conn, [store.Observation("gmail", "mail:x", "x")])
        triage.apply(conn, [obs], [triage.Verdict(key="mail:x", say="now", task="do it")])
        asyncio.run(prepare.prepare(conn))
    finally:
        conn.close()

    [(channel, allow)] = sent
    given = prepare.tools_for(obs)
    assert given <= prepare.PREPARE_TOOLS
    assert allow == set(given) | (workers.LEAD_BROWSING_TOOLS if browsing else set())
    assert is_autonomous(channel), f"the doer runs as {channel!r}, which counts as Maya"


def test_the_channel_the_loops_conversations_run_on_is_autonomous():
    assert is_autonomous(settings.CHANNEL)


# --------------------------------------------------------------------------
# Ask first
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "ref", "reason"), [
    ("after", {"event_id": "e1", "people": ["Jane Doe <jane@northwind.example>"]}, ""),
    ("threads", {"waiting": "owed by the user", "message_id": "m1"}, ""),
    # Seen as it arrived rather than days later as a thread: the same reply.
    (f"mail:{fake_google.KEY}", {"message_id": "m1"}, "reply-owed"),
])
def test_what_could_need_user_words_is_asked_about_never_drafted(tmp_path, source, ref,
                                                                 reason):
    """The maintainer's rule, 24 September 2026: a note after a meeting, a
    reply the user owes -- anything that could need something only the user knows -- is
    asked about, and drafted in the conversation once the user answers. Whatever
    the judge says, the doer never receives the task."""
    conn = db.open_store(tmp_path / "ask.db")
    try:
        [obs] = store.record_new(conn, [store.Observation(source, f"{source}:x", "x", ref=ref)])
        triage.apply(conn, [obs], [triage.Verdict(
            key=obs.key, say="now", headline="How did it go?",
            task="Draft a thank-you note to Jane", reason=reason,
        )])
        waiting = store.tasks_waiting(conn, 10)
        row = conn.execute("SELECT task, say FROM observations").fetchone()
    finally:
        conn.close()

    assert waiting == [] and row["task"] is None
    assert row["say"] == "now", "the question itself still goes to them"


# --------------------------------------------------------------------------
# The night
# --------------------------------------------------------------------------


def _one(conn, key, *, critical):
    [obs] = store.record_new(conn, [store.Observation("gmail", key, key)])
    triage.apply(conn, [obs], [triage.Verdict(
        key=key, say="now", critical=critical, headline=f"about {key}",
    )])


# Someone who works nights, and sleeps from 08:00 to 16:00.
BY_DAY = (dt.time(8, 0), dt.time(16, 0))


@pytest.mark.parametrize(("when", "quiet"), [
    (THREE_AM, None), (THREE_AM.replace(hour=23), None), (THREE_AM.replace(hour=7), None),
    (THREE_AM.replace(hour=12), BY_DAY), (THREE_AM.replace(hour=8), BY_DAY),
])
def test_at_night_nothing_but_a_critical_item_reaches_their_phone(tmp_path, monkeypatch,
                                                                  when, quiet):
    """Whenever their night is: quiet hours from 08:00 to 16:00 were read as
    crossing midnight, so the whole day was night."""
    import asyncio

    if quiet:
        monkeypatch.setattr(call_policy, "QUIET_FROM", quiet[0])
        monkeypatch.setattr(call_policy, "QUIET_UNTIL", quiet[1])

    sent: list[str] = []

    async def notify(text):
        sent.append(text)
        return True

    conn = db.open_store(tmp_path / "night.db")
    try:
        _one(conn, "mail:newsletter", critical=False)
        asyncio.run(deliver.deliver(conn, notify=notify, now=when))
        assert sent == [], f"a non-critical item went out at {when:%H:%M}"

        _one(conn, "mail:breach", critical=True)
        asyncio.run(deliver.deliver(conn, notify=notify, now=when))
    finally:
        conn.close()

    [text] = sent
    assert "breach" in text and "newsletter" not in text


def test_the_night_is_the_frozen_one():
    """Not a copy that could drift, and not the reminders' (which ignore the
    hour because the user set them)."""
    from core import call_policy

    assert settings.within_quiet_hours is call_policy.within_quiet_hours
    assert settings.within_quiet_hours(THREE_AM)
    assert not settings.within_quiet_hours(AFTERNOON)


# --------------------------------------------------------------------------
# The numbers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "ceiling"),
    [
        ("MAX_DAY_PINGS", 12),
        ("MAX_NIGHT_PINGS", 3),
        ("MAX_TASKS_PER_TICK", 6),
        ("MAX_TASKS_PER_DAY", 30),
        ("MAX_PER_SOURCE", 50),
        ("MAX_TRIAGE_BATCH", 60),
    ],
)
def test_every_limit_is_set_and_under_its_ceiling(name, ceiling):
    """A limit at zero or None silently is not one; a limit the agent could
    raise to a thousand is not one either. The whole project runs on about
    ten dollars a month."""
    value = getattr(settings, name)

    assert isinstance(value, int | float) and 0 < value <= ceiling, f"{name} = {value}"


@pytest.mark.parametrize(("name", "ceiling"), [("TRIAGE_BUDGET_USD", 0.25),
                                               ("WORK_BUDGET_USD", 0.50)])
def test_the_loops_day_budgets_are_set_and_under_their_ceilings(name, ceiling):
    """Its two parts of background work (core/spend.PARTS): the default,
    and the most the panel may set, both above nothing and under the
    ceiling."""
    assert 0 < spend.DEFAULTS[name] <= ceiling, name
    assert 0 < float(knobs.KNOBS[name].lo) <= float(knobs.KNOBS[name].hi) <= ceiling, name


def test_the_night_allows_fewer_messages_than_the_day():
    assert settings.MAX_NIGHT_PINGS < settings.MAX_DAY_PINGS


def test_no_source_polls_faster_than_once_a_minute():
    """A cadence of zero would call Google on every tick, forever."""
    assert all(seconds >= 60 for seconds in settings.CADENCE_S.values())
