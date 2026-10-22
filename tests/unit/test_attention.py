"""The attention loop: noticing, judging, preparing and telling, unasked.

It runs every minute with nobody watching, so almost everything here is
about restraint and about never doing a thing twice. The failures that
matter are not a missed email. They are the same calendar entry created six
times because a crash forgot what was done; a buzz at 3am for a newsletter;
the eighth ping of an afternoon; and a ping the user answers with "yes, add
it" arriving at a model that has never seen it.

Sources are tested in test_attention_sources.py; what the doer may touch
and the night rule are also pinned, frozen, in
tests/gate/test_attention_bounds.py.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from agent import attention, runtime
from agent.attention import deliver, prepare, settings, sources, store, triage
from agent.attention.store import Observation
from core import call_policy, spend

AFTERNOON = dt.datetime(2026, 9, 24, 14, 0)
THREE_AM = dt.datetime(2026, 9, 25, 3, 0)


def _obs(key: str, *, source: str = "gmail", when: str | None = None, **kw) -> Observation:
    return Observation(source=source, key=key, title=kw.pop("title", key), when=when, **kw)


def _judged(conn, key, say="now", *, critical=False, task="", when=None, headline=None):
    [obs] = store.record_new(conn, [_obs(key, when=when)])
    triage.apply(conn, [obs], [triage.Verdict(
        key=key, say=say, critical=critical, headline=headline or f"about {key}", task=task,
    )])
    return obs.id


class _Notify:
    """Stands in for `notify_user`: records what would have buzzed, and
    returns its outbox row -- or cannot write it, like a locked database."""

    def __init__(self, written: bool = True):
        self.written = written
        self.sent: list[str] = []

    async def __call__(self, text: str) -> int:
        if not self.written:
            raise RuntimeError("database is locked")
        self.sent.append(text)
        return len(self.sent)


# --------------------------------------------------------------------------
# One row per thing, seen once
# --------------------------------------------------------------------------


def test_a_thing_is_recorded_once_however_often_a_source_sees_it(conn):
    """Gmail is polled every three minutes and returns the same inbox each
    time. If the same message were new twice, it would be judged twice and
    prepared twice -- the duplicate calendar entries the old sweep's keys
    existed to prevent."""
    first = store.record_new(conn, [_obs("mail:a"), _obs("mail:b")])
    again = store.record_new(conn, [_obs("mail:a"), _obs("mail:b"), _obs("mail:c")])

    assert [o.key for o in first] == ["mail:a", "mail:b"]
    assert [o.key for o in again] == ["mail:c"]


def test_a_capped_source_still_reaches_what_is_new_beyond_the_cap(conn, monkeypatch):
    """Capping before dropping what was seen let the thirty events already
    considered crowd out the thirty-first, every run for the rest of the
    week."""
    monkeypatch.setattr(settings, "MAX_PER_SOURCE", 3)
    old = [_obs(f"horizon:{i}:ahead") for i in range(3)]
    store.record_new(conn, old)
    source = sources.Source("horizon", lambda c: [*old, _obs("horizon:new:ahead")])

    assert [o.key for o in sources.collect(conn, source)] == ["horizon:new:ahead"]


def test_a_failed_source_counts_as_run_so_it_is_not_retried_every_minute(conn):
    def broken(_c):
        raise sources.SourceError("token expired")

    source = sources.Source("mail:gmail", broken, every=3 * 60)

    assert sources.collect(conn, source) == []
    assert sources.due(conn, [source]) == []
    assert "token expired" in sources.last_runs(conn)["mail:gmail"]["note"]


def test_a_mailbox_that_fails_is_recorded_once_and_the_next_source_still_runs(conn, monkeypatch):
    """The attention loop once retried one IMAP server's handshake timeouts
    itself, three times with jitter, inside the tick. A provider now says
    which of its failures are transient (`steve_sdk.errors`); the loop only
    records a failure and goes on, so one mailbox down leaves every other
    source runnable, tried once a tick."""
    attempts = []
    healthy_ran = []

    def broken_search(_connection):
        attempts.append(True)
        raise TimeoutError("imap.example.com: The handshake operation timed out")

    broken = sources.Source("home", broken_search, feature="mail")
    healthy = sources.Source(
        "work", lambda _connection: healthy_ran.append(True) or [], feature="mail"
    )
    monkeypatch.setattr(sources, "watching", lambda _conn: [broken, healthy])

    ran, noticed = asyncio.run(attention.look(conn, force=True))

    assert ran == ["home", "work"]
    assert noticed == 0
    assert attempts == [True]
    assert healthy_ran == [True]
    assert "TimeoutError" in sources.last_runs(conn)["home"]["note"]


def test_a_guarded_error_is_a_failure_not_an_empty_inbox():
    """The Google helpers return `{"error": ...}` instead of raising. Taken
    as a result, an expired token reads as "nothing new", forever."""
    with pytest.raises(sources.SourceError):
        sources.ok({"error": "invalid_grant"}, "gmail list")


def test_every_source_is_due_on_a_fresh_start(conn):
    """After the laptop has slept, the first tick catches up on everything.
    Mail sources come one per account the user added, at its own cadence,
    and are not among these."""
    everything = sources.all_sources()

    assert sources.due(conn, everything) == everything
    assert set(settings.CADENCE_S) == {s.name for s in everything}


# --------------------------------------------------------------------------
# The judge
# --------------------------------------------------------------------------


def test_an_invented_key_is_dropped_and_a_forgotten_item_is_never(conn):
    batch = store.record_new(conn, [_obs("mail:a"), _obs("mail:b")])

    result = triage.apply(conn, batch, [
        triage.Verdict(key="mail:a", say="briefing", headline="A matters a bit"),
        triage.Verdict(key="mail:zzz", say="now", headline="made up"),
    ])

    assert result.judged == 2
    assert store.untriaged(conn, 10) == []
    assert [o.key for o in store.for_briefing(conn)] == ["mail:a"]
    assert store.to_say_now(conn) == []


def test_before_needs_a_time_to_be_before(conn):
    """An all-day event has a date and no moment; it goes in the briefing."""
    batch = store.record_new(conn, [_obs("horizon:x:soon", when="2026-09-26")])
    triage.apply(conn, batch, [triage.Verdict(key="horizon:x:soon", say="before", headline="x")])

    [held] = store.for_briefing(conn)
    assert held.say == "briefing"


def test_a_brief_is_due_the_lead_time_before_the_start_in_utc(conn):
    """`deliver_at` is compared with SQLite's UTC `now`; stored local, it
    would fire four or five hours off."""
    start = dt.datetime(2026, 9, 26, 11, 30).astimezone()
    batch = store.record_new(conn, [_obs("horizon:y:soon", when=start.isoformat())])
    triage.apply(conn, batch, [triage.Verdict(key="horizon:y:soon", say="before", headline="y")])

    row = conn.execute("SELECT deliver_at FROM observations").fetchone()
    expected = (start - settings.BEFORE_LEAD).astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    assert row["deliver_at"] == expected


def test_only_something_said_now_can_be_critical(conn):
    """Critical is what may break the night; it means nothing on an item
    that is not being said now."""
    _judged(conn, "mail:a", "briefing", critical=True)

    assert conn.execute("SELECT critical FROM observations").fetchone()["critical"] == 0


async def test_a_failed_judgement_waits_then_gives_up(conn, monkeypatch):
    """A model outage should not lose the items, and a poisoned item should
    not be paid for every minute forever."""
    store.record_new(conn, [_obs("mail:a")])

    async def down(*_a, **_k):
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(triage, "judge", down)
    monkeypatch.setattr(triage, "day_context", lambda _c: "")

    for attempt in range(settings.MAX_TRIAGE_ATTEMPTS):
        assert store.untriaged(conn, 10), f"gave up after {attempt}"
        await triage.triage(conn)

    assert store.untriaged(conn, 10) == []
    assert conn.execute("SELECT delivered_in FROM observations").fetchone()[0] == "dropped"


def _spent_on(conn, part: str, usd: float) -> None:
    """Today's spending on one part of background work, in the ledger."""
    spend.bill(conn, usd, model="m", unit=spend.Unit("background", part))


async def test_judging_stops_at_its_budget(conn, monkeypatch):
    store.record_new(conn, [_obs("mail:a")])
    _spent_on(conn, "triage", 1.0)

    async def never(*_a, **_k):
        raise AssertionError("judged past the budget")

    monkeypatch.setattr(triage, "judge", never)

    result = await triage.triage(conn)

    assert "TRIAGE_BUDGET_USD" in result.skipped
    assert store.untriaged(conn, 10)  # still there for tomorrow


async def test_a_day_of_preparing_leaves_the_judge_its_own_budget(conn, monkeypatch):
    """Judging is what notices. A day of heavy preparing must never leave
    the loop unable to notice something urgent."""
    store.record_new(conn, [_obs("mail:a")])
    _spent_on(conn, "work", 1.0)
    judged = []

    async def judge(batch, *_a, **_k):
        judged.append(batch)
        raise RuntimeError("stop here")

    monkeypatch.setattr(triage, "judge", judge)
    monkeypatch.setattr(triage, "day_context", lambda _c: "")

    await triage.triage(conn)

    assert len(judged) == 1


async def test_what_the_doer_spends_is_background_work_whatever_channel_it_runs_on(
        conn, doer, monkeypatch):
    """The doer's conversation runs on the initiative channel; its bill
    must land in background's day, not chat's, or a busy tick would spend
    the user's conversation allowance."""
    _judged(conn, "mail:a", "now", task="Draft a reply to Rina")
    doer.replies = [_Reply("NOTHING TO DO")]
    answer = doer.send

    async def billed(self, text, *, allow=None):
        with spend.spending("chat", conn=self.conn):
            spend.bill(self.conn, 0.02, model="x")
        return await answer(self, text, allow=allow)

    monkeypatch.setattr(doer, "send", billed)

    await prepare.prepare(conn)

    [row] = conn.execute("SELECT category, part FROM usage_events").fetchall()
    assert (row["category"], row["part"]) == ("background", "work")


# --------------------------------------------------------------------------
# The doer
# --------------------------------------------------------------------------


class _Convo:
    """A `Conversation` that records what it was asked and with which tools."""

    replies: list = []
    seen: list = []

    def __init__(self, conn, *, channel, mandate=None):
        self.conn = conn
        self.channel = channel
        self.mandate = mandate

    async def send(self, text, *, allow=None):
        _Convo.seen.append({
            "channel": self.channel,
            "allow": allow,
            "convo": self,
            "mandate": self.mandate,
            "asked": text,
            "started": self.conn.execute(
                "SELECT prepared_at IS NOT NULL FROM observations WHERE task IS NOT NULL"
            ).fetchone()[0],
        })
        return _Convo.replies.pop(0)

    def end(self):
        pass


class _Reply:
    def __init__(self, text, tools=(), cost=0.001):
        self.text = text
        self.tools_used = list(tools)
        self.cost_usd = cost


@pytest.fixture
def doer(monkeypatch):
    import agent.loop

    _Convo.replies, _Convo.seen = [], []
    monkeypatch.setattr(agent.loop, "Conversation", _Convo)
    # Browsing workers off: what the doer's turn is given with them on is
    # the gate's (tests/gate/test_browsing_workers.py).
    monkeypatch.setattr("core.browser.background", lambda: False)
    return _Convo


async def test_a_task_is_marked_started_before_its_turn_runs(conn, doer):
    """A crash between the doer creating the event and the row saying so
    would hand the task to the next tick, which would create it again."""
    _judged(conn, "mail:a", "now", task="Add the workshop, Fri 11:00, room 2200")
    doer.replies = [_Reply("Added the workshop, Fri 11:00", ["calendar_search", "create_event"])]

    result = await prepare.prepare(conn)

    assert doer.seen[0]["started"] == 1
    assert result.done == ["Added the workshop, Fri 11:00"]
    assert store.tasks_waiting(conn, 10) == []


async def test_each_task_has_a_conversation_of_its_own(conn, doer):
    """One conversation for the tick sent each task the ones before it --
    their mail and drafts -- as if it were the same work: a turn carrying
    six earlier tasks was 124,000 input tokens (5 October 2026)."""
    _judged(conn, "mail:a", "now", task="Draft a reply to Rina")
    _judged(conn, "mail:b", "now", task="Add the workshop, Fri 11:00")
    doer.replies = [_Reply("NOTHING TO DO"), _Reply("NOTHING TO DO")]

    await prepare.prepare(conn)

    first, second = doer.seen
    assert first["convo"] is not second["convo"]
    assert "Rina" in first["mandate"] and "workshop" in second["mandate"]


async def test_it_runs_as_an_autonomous_channel_with_only_its_own_tools(conn, doer):
    from core.call_policy import is_autonomous

    _judged(conn, "mail:a", "now", task="Draft a reply")
    doer.replies = [_Reply("NOTHING TO DO")]

    await prepare.prepare(conn)

    assert is_autonomous(doer.seen[0]["channel"])
    # All of them but the drafting tool, which only a nudge gets.
    assert doer.seen[0]["allow"] == set(prepare.PREPARE_TOOLS) - {"draft_email"}


def test_a_draft_is_offered_unasked_only_for_a_nudge_on_their_own_email():
    """Nothing is drafted unasked that could need words only the user has:
    a reply they owe is asked about (`triage.ask_first`), and without the
    drafting tool a task that slipped past cannot write one either. A nudge
    on their own unanswered email needs nothing from them."""
    mail = _obs("mail:a", title="Dana: is Thursday still on?")
    nudge = _obs("thread:b", source="threads", title="Sam has not answered",
                 ref={"waiting": "owed to the user"})

    assert "draft_email" not in prepare.tools_for(mail)
    assert "draft_email" not in prepare.instructions(mail, prepare.tools_for(mail))
    assert "draft_email" in prepare.tools_for(nudge)
    assert "draft_email" in prepare.instructions(nudge, prepare.tools_for(nudge))


def test_a_heads_up_reads_their_own_things_and_the_web_only_if_they_chose(monkeypatch):
    """Every meeting with anyone in it was researched on the web: a school
    pickup got a dossier, and the guests' names went to a search engine."""
    from agent.attention import settings

    heads_up = _obs("horizon:e1:soon", source="horizon", title="Lunch with Sam", say="before")
    off = prepare.tools_for(heads_up)
    monkeypatch.setattr(settings, "MEETING_RESEARCH", "on")
    on = prepare.tools_for(heads_up)

    assert not off & (prepare.RESEARCHING | prepare.ACTING)
    assert prepare.RESEARCHING <= on <= prepare.PREPARE_TOOLS
    assert "web" not in prepare.instructions(heads_up, off)
    assert "web" in prepare.instructions(heads_up, on)


def test_a_heads_up_and_a_task_are_each_told_their_shape_once():
    """One prompt said "ONE line" and then "up to three", and the first live
    heads-up came out as one 300-character line."""
    heads_up = _obs("horizon:e1:soon", source="horizon", say="before")
    task = _obs("mail:a", say="now")

    said_heads_up = prepare.instructions(heads_up, prepare.tools_for(heads_up))
    said_task = prepare.instructions(task, prepare.tools_for(task))

    assert "up to three" in said_heads_up and "ONE line" not in said_heads_up
    assert "ONE line" in said_task and "three" not in said_task


@pytest.mark.parametrize(
    ("text", "tools", "expected"),
    [
        ("Read your mail, nothing needed.", ["search_email", "read_email"], None),
        ("NOTHING TO DO", ["create_event"], None),
        ("Drafted a reply to Rina (Outlook drafts)", ["read_email", "draft_email"],
         "Drafted a reply to Rina (Outlook drafts)"),
        ("Northwind's Q3 hiring is up 8%\nmore detail", ["web_search"],
         "Northwind's Q3 hiring is up 8%"),
    ],
)
def test_the_tools_are_the_evidence_not_the_text(text, tools, expected):
    """"I looked at your mail" every few minutes is exactly what gets this
    muted. Only a tool that changed something or found something counts."""
    assert prepare.outcome_of(text, tools) == expected


async def test_nothing_done_leaves_the_judges_headline(conn, doer):
    obs_id = _judged(
        conn, "mail:a", "now", task="Draft a reply", headline="Rina asked about DES 203"
    )
    doer.replies = [_Reply("NOTHING TO DO")]

    await prepare.prepare(conn)

    [ready] = store.to_say_now(conn)
    assert ready.id == obs_id and ready.outcome is None
    assert deliver.line_for(ready, AFTERNOON) == "Rina asked about DES 203"


async def test_the_day_has_a_task_limit(conn, doer, monkeypatch):
    monkeypatch.setattr(settings, "MAX_TASKS_PER_DAY", 1)
    _judged(conn, "mail:a", "briefing", task="one")
    _judged(conn, "mail:b", "briefing", task="two")
    doer.replies = [_Reply("NOTHING TO DO"), _Reply("NOTHING TO DO")]

    await prepare.prepare(conn)
    second = await prepare.prepare(conn)

    assert len(doer.seen) == 1
    assert "limit" in second.skipped


async def test_the_doer_stops_at_its_budget(conn, doer):
    _judged(conn, "mail:a", "now", task="one")
    _spent_on(conn, "work", 0.25)

    result = await prepare.prepare(conn)

    assert doer.seen == [] and "WORK_BUDGET_USD" in result.skipped


# --------------------------------------------------------------------------
# Telling them
# --------------------------------------------------------------------------


async def test_everything_ready_goes_out_as_one_message(conn):
    """Three buzzes for one minute's news is an interruption; one message
    with three lines is a heads-up. The cap counts the message."""
    for key in ("mail:a", "mail:b", "mail:c"):
        _judged(conn, key, "now")
    notify = _Notify()

    result = await deliver.deliver(conn, notify=notify, now=AFTERNOON)

    assert len(notify.sent) == 1 and notify.sent[0].count("·") == 3
    assert len(result.sent) == 3
    assert store.pings_since(conn, AFTERNOON.replace(hour=8), at_night=False) == 1
    assert store.to_say_now(conn) == []


async def test_at_night_only_something_critical_goes_out(conn):
    """The user: pings at night are fine for something important, and only
    then. A buzz at 3am for anything less is how this gets switched off."""
    _judged(conn, "mail:bill", "now", headline="Con Ed bill due tomorrow")
    _judged(conn, "mail:alert", "now", critical=True, headline="Your Contoso password was reset")
    notify = _Notify()

    result = await deliver.deliver(conn, notify=notify, now=THREE_AM)

    [text] = notify.sent
    assert "password was reset" in text and "Con Ed" not in text
    assert text.startswith("Sorry for the hour")
    assert result.held == 1


async def test_what_the_night_held_back_waits_for_the_morning(conn):
    _judged(conn, "mail:bill", "now", headline="Con Ed bill due tomorrow")

    await deliver.deliver(conn, notify=_Notify(), now=THREE_AM)

    assert [o.headline for o in store.for_briefing(conn)] == ["Con Ed bill due tomorrow"]


async def test_the_night_has_its_own_smaller_limit(conn):
    notify = _Notify()
    for n in range(settings.MAX_NIGHT_PINGS + 1):
        _judged(conn, f"mail:alert{n}", "now", critical=True)
        await deliver.deliver(conn, notify=notify, now=THREE_AM + dt.timedelta(minutes=n))

    assert len(notify.sent) == settings.MAX_NIGHT_PINGS


async def test_the_day_limit_holds_and_says_so(conn):
    notify = _Notify()
    for n in range(settings.MAX_DAY_PINGS + 1):
        _judged(conn, f"mail:{n}", "now")
        result = await deliver.deliver(conn, notify=notify, now=AFTERNOON + dt.timedelta(minutes=n))

    assert len(notify.sent) == settings.MAX_DAY_PINGS
    assert "limit" in result.skipped


async def test_last_nights_pings_do_not_count_against_today(conn):
    """The two windows are counted apart: a critical alert at 3am must not
    cost the user one of their daytime messages, nor the reverse."""
    for n in range(settings.MAX_NIGHT_PINGS):
        at = THREE_AM + dt.timedelta(minutes=n)
        store.pinged(conn, items=1, critical=True, at_night=True, at=at)
    _judged(conn, "mail:a", "now")
    notify = _Notify()

    await deliver.deliver(conn, notify=notify, now=THREE_AM.replace(hour=9))

    assert len(notify.sent) == 1


async def test_a_message_the_outbox_could_not_take_is_not_marked_told(conn):
    """Told means told (protocols §10): an item is delivered once its
    message is in the outbox, and not before."""
    _judged(conn, "mail:a", "now")

    result = await deliver.deliver(conn, notify=_Notify(written=False), now=AFTERNOON)

    assert result.sent == [] and len(store.to_say_now(conn)) == 1
    assert result.skipped == "could not write it to the outbox"
    assert store.pings_since(conn, AFTERNOON.replace(hour=8), at_night=False) == 0


async def test_a_pings_lines_are_said_in_told_only_once_the_outbox_has_sent_it(conn):
    """"Why did you tell me that?" answered "Tue 14:02, in a ping" for a ping
    Signal never took. The line is said once its row reaches the user
    (tests/unit/test_outbox.py)."""
    from agent import outbox

    _judged(conn, "mail:a", "now", headline="Con Ed bill due today")

    async def notify(text):
        return outbox.add(conn, text, kind="notice", remote="signal", now=AFTERNOON)

    await deliver.deliver(conn, notify=notify, now=AFTERNOON)
    [row] = conn.execute("SELECT said_at, outbox_id FROM told").fetchall()
    assert row["said_at"] is None

    outbox.sent(conn, row["outbox_id"], AFTERNOON)
    assert conn.execute("SELECT said_at FROM told").fetchone()["said_at"] is not None


async def test_news_that_waited_too_long_goes_in_the_briefing_instead(conn):
    obs_id = _judged(conn, "mail:a", "now")
    with conn:
        conn.execute(
            "UPDATE observations SET triaged_at = datetime('now', '-4 hours') WHERE id = ?",
            (obs_id,),
        )
    notify = _Notify()

    result = await deliver.deliver(conn, notify=notify, now=AFTERNOON)

    assert notify.sent == [] and result.demoted == 1
    assert [o.id for o in store.for_briefing(conn)] == [obs_id]


async def test_a_brief_goes_out_when_its_moment_comes(conn):
    start = dt.datetime.now().astimezone() + dt.timedelta(minutes=30)
    _judged(conn, "horizon:chat:soon", "before", when=start.isoformat(),
            headline="Northwind coffee chat")
    notify = _Notify()

    await deliver.deliver(conn, notify=notify, now=AFTERNOON)

    [text] = notify.sent
    assert "Northwind coffee chat" in text and text.startswith("In ")


async def test_a_brief_for_something_that_has_started_is_dropped(conn):
    """A brief is for walking in prepared. After the start it is a record of
    having missed the moment."""
    start = dt.datetime.now().astimezone() - dt.timedelta(minutes=5)
    _judged(conn, "horizon:chat:soon", "before", when=start.isoformat())
    notify = _Notify()

    result = await deliver.deliver(conn, notify=notify, now=AFTERNOON)

    assert notify.sent == [] and result.dropped == 1


async def test_an_item_does_not_wait_for_ever_on_a_task_that_never_starts(conn):
    """The day's task limit can leave a task unstarted. The item it belongs
    to must still reach them."""
    obs_id = _judged(conn, "mail:a", "now", task="Draft a reply")
    assert store.to_say_now(conn) == []
    with conn:
        conn.execute(
            "UPDATE observations SET triaged_at = datetime('now', ?) WHERE id = ?",
            (f"-{store.TASK_WAIT_S + 60} seconds", obs_id),
        )

    assert [o.id for o in store.to_say_now(conn)] == [obs_id]
    assert store.tasks_waiting(conn, 10)  # still doable until it goes out
    store.delivered(conn, [obs_id], "ping")
    assert store.tasks_waiting(conn, 10) == []


async def test_a_tick_tells_them_what_it_judged_before_its_doer_starts(conn, doer, monkeypatch):
    """A doer's turn takes a minute or two, and one that has a browsing
    worker read a page up to five (since 11 October 2026). With the doer
    ahead of delivery, every ping in the tick waited for all its tasks, a
    critical one too. What the doer prepares goes out with the next tick:
    its item was waiting for it."""
    from agent import attention

    order: list[str] = []

    class _Told(_Notify):
        async def __call__(self, text: str) -> bool:
            order.append("told")
            return await super().__call__(text)

    class _Doer(doer):
        async def send(self, text, *, allow=None):
            order.append("doer")
            return await super().send(text, allow=allow)

    async def nothing_new(*_a, **_k):
        return [], 0

    in_the_afternoon = deliver.deliver

    async def delivering(c, *, notify=None):
        return await in_the_afternoon(c, notify=notify, now=AFTERNOON)

    monkeypatch.setattr(attention, "look", nothing_new)
    monkeypatch.setattr(deliver, "deliver", delivering)
    monkeypatch.setattr("agent.loop.Conversation", _Doer)
    _judged(conn, "mail:a", "now", critical=True, headline="The interview moved to 9:00")
    _judged(conn, "mail:b", "now", task="Add the workshop, Fri 11:00", headline="A workshop")
    doer.replies = [_Reply("Added the workshop, Fri 11:00", ["calendar_search", "create_event"])]
    notify = _Told()

    await attention.tick(conn, notify=notify)

    assert order == ["told", "doer"]
    assert "interview" in notify.sent[0] and "workshop" not in notify.sent[0]

    await attention.tick(conn, notify=notify)

    assert notify.sent[1] == "A workshop\n  Added the workshop, Fri 11:00"


def test_what_was_done_is_said_under_what_happened():
    obs = Observation(
        source="outlook", key="k", title="t", say="now",
        headline="Rina asks if you are enrolled in DES 203",
        outcome="Drafted a reply confirming it (Outlook drafts)",
    )

    assert deliver.line_for(obs, AFTERNOON) == (
        "Rina asks if you are enrolled in DES 203\n  Drafted a reply confirming it (Outlook drafts)"
    )


# --------------------------------------------------------------------------
# A ping is part of the conversation
# --------------------------------------------------------------------------


@pytest.fixture
def worker(conn, monkeypatch):
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(runtime, "conversations", {})
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setattr(runtime, "signal", None)
    monkeypatch.setattr(runtime, "outbox_wake", None)
    return conn


class _Signal:
    """Stands in for the Signal bridge: `channel.send` says whether Signal
    took the message, and what was sent is kept."""

    def __init__(self, delivered: bool):
        self.delivered = delivered
        self.sent: list[str] = []
        self.channel = self

    async def send(self, message):
        self.sent.append(message.text)
        return self.delivered


async def test_a_ping_joins_the_conversation_their_reply_will_land_in(worker, monkeypatch):
    """The user is texted "Rain from 3pm; your 4pm workshop is in person" and
    answers "move it to Zoom then". The model answering them had never seen
    "it": unprompted messages were in nobody's history."""
    from agent.loop import UNPROMPTED
    from agent.worker import answering, links

    monkeypatch.setattr(runtime, "signal", _Signal(delivered=True))

    await links.notify_user("Rain from 3pm; your 4pm workshop is in person")
    assert await links.send_due() == 1

    session = answering.recent_session("signal")
    assert session is not None
    row = worker.execute(
        "SELECT role, content, model FROM messages WHERE session_id = ?", (session,)
    ).fetchone()
    assert (row["role"], row["model"]) == ("assistant", UNPROMPTED)
    assert "workshop" in row["content"]


async def test_a_loaded_conversation_hears_it_too(worker, monkeypatch):
    """The conversation already in memory is the one that answers next; the
    database alone would reach it only after a restart."""
    from agent.worker import answering, links

    convo = answering.conversation(None, channel="signal")
    convo._store_message("user", "what's on today?")
    monkeypatch.setattr(runtime, "signal", _Signal(delivered=True))

    await links.notify_user("Your 4pm moved to 5pm")
    await links.send_due()

    assert answering.conversation(answering.recent_session("signal"), "signal") is convo
    assert "moved to 5pm" in str(convo._history[-1])


async def test_a_message_that_never_arrived_is_not_in_the_conversation(worker, monkeypatch):
    from agent.worker import answering, links

    monkeypatch.setattr(runtime, "signal", _Signal(delivered=False))

    await links.notify_user("You will never see this")
    await links.send_due()
    assert answering.recent_session("signal") is None


async def test_a_reminder_going_off_is_recorded_as_one_while_the_user_sees_just_its_words(
        worker, monkeypatch):
    """27 September 2026: "put your sheets in the wash" and "Mop" went off,
    and sat in the history as two bare lines the agent had apparently said.
    Their "both complete" was read as the gym and the run the user had mentioned
    earlier, and the week's six gym and run reminders were cancelled."""
    from datetime import datetime, timedelta

    from agent import reminders
    from agent.worker import answering, background, links

    signal = _Signal(delivered=True)
    monkeypatch.setattr(runtime, "signal", signal)
    now = datetime.now()
    booked = reminders.schedule(worker, kind="remind", text="Mop",
                                when=(now + timedelta(minutes=1)).isoformat(timespec="seconds"),
                                now=now)

    await background._fire_reminder(booked)
    await links.send_due()

    assert signal.sent == ["Mop"]
    row = worker.execute(
        "SELECT content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (answering.recent_session("signal"),),
    ).fetchone()
    assert row["content"] == f"[reminder {booked.id} went off] Mop"


# --------------------------------------------------------------------------
# The two paths that used to race at 8am
# --------------------------------------------------------------------------


async def test_what_the_night_held_is_not_also_pinged_in_the_morning(conn):
    """Held at 3am for the morning briefing, it was also still a `now`
    item -- so the first daytime tick pinged it while the briefing carried
    it too. Held means held."""
    _judged(conn, "mail:bill", "now", headline="Con Ed bill due tomorrow")
    await deliver.deliver(conn, notify=_Notify(), now=THREE_AM)

    morning = _Notify()
    await deliver.deliver(conn, notify=morning, now=THREE_AM.replace(hour=8, minute=1))

    assert morning.sent == []
    assert [o.headline for o in store.for_briefing(conn)] == ["Con Ed bill due tomorrow"]


async def test_a_brief_long_past_its_moment_is_the_briefings_not_a_ping(conn):
    """A brief whose moment fell in quiet hours belongs to the morning
    briefing. Pinging it at 8:00 as well said it twice."""
    start = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
    obs_id = _judged(conn, "horizon:lecture:soon", "before", when=start.isoformat())
    with conn:
        conn.execute(
            "UPDATE observations SET deliver_at = datetime('now', '-30 minutes') WHERE id = ?",
            (obs_id,),
        )
    notify = _Notify()

    await deliver.deliver(conn, notify=notify, now=AFTERNOON)

    assert notify.sent == []
    assert obs_id in [o.id for o in store.for_briefing(conn)]


async def test_the_judges_prompt_puts_what_changes_least_first(monkeypatch):
    """The day's context is kept ten minutes, the clock changes every minute:
    with the clock first, nothing after the fixed instructions was read from
    the cache (6 October 2026)."""
    import agent.llm

    sent: list[str] = []

    async def fake_run_agent(agent_, prompt, role, **kwargs):
        sent.append(prompt)
        return type("R", (), {"output": triage.Verdicts(items=[])})(), None

    monkeypatch.setattr(agent.llm, "run_agent", fake_run_agent)
    await triage.judge([_obs("mail:a")], "Her next two days:\n  - 09:00 Accounting")

    [prompt] = sent
    assert prompt.index("Her next two days") < prompt.index("It is ") < prompt.index("mail:a")


@pytest.mark.parametrize(("quiet", "now", "at_night", "began"), [
    (None, "2026-10-12 10:00", False, "2026-10-12 08:00"),
    (None, "2026-10-12 03:00", True, "2026-10-11 22:00"),
    (None, "2026-10-12 23:00", True, "2026-10-12 22:00"),
    ("08:00-16:00", "2026-10-12 02:00", False, "2026-10-11 16:00"),
    ("08:00-16:00", "2026-10-12 12:00", True, "2026-10-12 08:00"),
])
def test_the_days_pings_are_counted_from_when_quiet_hours_last_ended(monkeypatch, quiet, now,
                                                                     at_night, began):
    """The day was taken to begin at today's end of quiet hours. For someone
    whose night ends at 16:00, at 02:00 that was fourteen hours ahead, so
    nothing sent that day counted and the cap never bit."""
    if quiet:
        start, end = quiet.split("-")
        monkeypatch.setattr(call_policy, "QUIET_FROM", dt.time.fromisoformat(start))
        monkeypatch.setattr(call_policy, "QUIET_UNTIL", dt.time.fromisoformat(end))

    assert deliver._window_start(dt.datetime.fromisoformat(now), at_night) == (
        dt.datetime.fromisoformat(began))
