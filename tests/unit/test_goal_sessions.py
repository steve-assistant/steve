"""Goals are worked on in sessions, tell them only what the user needs, and end.

What a session may do outward, and who may set or steer a goal, are pinned
in tests/gate/test_goals.py. These pin the rest: the plan's state, sessions
exactly once and on cadence, sooner when the user answers or the reply it waits
for comes, never in quiet hours, questions reaching them once, done and
deadlines ending it, and the budgets.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from agent import goals, runtime
from agent.worker import background, handlers, links
from core import db, monitor, spend
from tests.fakes import google as fake_google

# A Saturday morning, pinned: quiet hours must not depend on when the suite runs.
SAT_10 = datetime(2026, 10, 3, 10, 0)
ALEX = "alex@example.com"


@pytest.fixture
def conn(monkeypatch):
    c = db.open_store()
    monkeypatch.setattr(runtime, "conn", c)
    yield c
    c.close()


@pytest.fixture
def told(monkeypatch):
    out: list[tuple[str, str | None]] = []

    async def notify(text, *, noted=None, **_):
        out.append((text, noted))
        return len(out)

    monkeypatch.setattr(links, "notify_user", notify)
    return out


def _goal(conn, *, now=SAT_10, **kw) -> goals.Goal:
    args = dict(title="Call with Alex", ask="Find a time for a call with Alex.",
                done_when="a time is agreed and on my calendar",
                steps=["find three free times", "email Alex", "invite Alex"])
    return goals.create(conn, now=now, **(args | kw))


# --- the record --------------------------------------------------------------------


@pytest.mark.parametrize("kw,why", [
    (dict(steps=[]), "plan"),
    (dict(steps=[" "]), "plan"),
    (dict(steps=[f"s{i}" for i in range(goals.MAX_STEPS + 1)]), "at most"),
    (dict(done_when=""), "done looks like"),
    (dict(every_hours=0.25), "between"),
    (dict(every_hours=500), "between"),
    (dict(budget_usd=50), "at most"),
    (dict(deadline="2026-10-01"), "past"),
    (dict(deadline="2027-06-01"), "90 days"),
    (dict(deadline="next friday"), "ISO"),
    (dict(envelope={"purchase": {"targets": [ALEX]}}), "cannot be done"),
])
def test_a_goal_that_will_not_do_says_why(conn, kw, why):
    with pytest.raises(goals.BadGoal, match=why):
        _goal(conn, **kw)


def test_a_bare_date_deadline_means_the_end_of_that_day(conn):
    assert _goal(conn, deadline="2026-10-09").deadline == "2026-10-09T23:59:00"


def test_one_starts_at_once_unless_it_waits_for_its_card(conn):
    plain = _goal(conn)
    outward = _goal(conn, envelope={"send_email": {"targets": [ALEX]}})
    assert plain.status == "active" and plain.next_check_at == SAT_10.isoformat()
    assert [s["status"] for s in plain.steps] == ["todo"] * 3
    assert outward.status == "awaiting_approval" and outward.next_check_at is None

    assert not goals.activate(conn, outward.id, "0" * 64)
    assert goals.activate(conn, outward.id, monitor.holder_sha(conn, "goal", outward.id),
                          now=SAT_10)
    assert goals.get(conn, outward.id).next_check_at == SAT_10.isoformat()


def test_there_is_a_ceiling_on_how_many(conn, monkeypatch):
    monkeypatch.setattr(goals, "MAX_LIVE", 2)
    _goal(conn)
    dropped = _goal(conn)
    with pytest.raises(goals.BadGoal, match="already 2"):
        _goal(conn)
    goals.drop(conn, dropped.id)
    _goal(conn)


# --- sessions -----------------------------------------------------------------------


def test_a_due_goal_is_claimed_exactly_once_and_comes_round_on_its_cadence(conn):
    goal = _goal(conn, every_hours=6)
    assert [g.id for g in goals.due(conn, SAT_10)] == [goal.id]
    assert goals.claim(conn, goal, SAT_10)
    assert not goals.claim(conn, goal, SAT_10)
    claimed = goals.get(conn, goal.id)
    assert claimed.next_check_at == (SAT_10 + timedelta(hours=6)).isoformat()
    assert claimed.running_since == SAT_10.isoformat()


def test_a_claim_from_a_stale_look_does_not_start_a_second_session(conn):
    """Two looks at the queue -- an old worker's and its replacement's during
    a swap -- may both have seen the goal due. Only the first claim counts,
    even once the first session is over."""
    goal = _goal(conn)
    seen = goals.due(conn, SAT_10)[0]
    assert goals.claim(conn, seen, SAT_10)
    goals.after_session(conn, goal.id, cost_usd=0, session_id=None, report=None, now=SAT_10)
    assert not goals.claim(conn, seen, SAT_10 + timedelta(minutes=1))


def test_a_session_can_ask_user_to_send_a_draft_or_an_email(conn):
    """Its sign-off points are cards it raises itself; without these it could
    only ask them a question and wait."""
    assert {"send_email", "send_draft", "draft_email"} <= goals.toolset(_goal(conn))


def test_a_running_session_is_not_started_twice_unless_it_died(conn):
    goal = _goal(conn)
    goals.claim(conn, goal, SAT_10)
    goals.answer(conn, goal.id, "also try Thursday", now=SAT_10 + timedelta(minutes=1))
    assert goals.due(conn, SAT_10 + timedelta(minutes=2)) == []
    later = SAT_10 + goals.RUNNING_STALE + timedelta(minutes=2)
    assert [g.id for g in goals.due(conn, later)] == [goal.id]


def test_after_a_session_its_cost_and_actions_are_kept(conn):
    goal = _goal(conn)
    goals.claim(conn, goal, SAT_10)
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('goal')").lastrowid
        conn.execute("INSERT INTO usage_events (session_id, model, cost_usd) VALUES (?, 'm', 0.03)",
                     (session,))
        conn.execute("INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal,"
                     " status, session_id) VALUES ('draft_email', '{}', 'h', 'Draft to Alex',"
                     " 'private', 'reversible', 'done', ?)", (session,))
    after = goals.after_session(conn, goal.id, cost_usd=0.01, session_id=session,
                                report="Drafted it.", now=SAT_10)
    assert after.running_since is None and after.sessions == 1
    assert after.spent_usd == pytest.approx(0.03)
    assert len(after.actions) == 1 and after.last_report == "Drafted it."
    assert "Draft to Alex -- done" in goals.session_note(conn, after)


def test_a_draft_a_session_made_on_its_own_is_in_the_goals_record(conn, monkeypatch):
    """The first live goal, 4 October 2026, drafted its email without asking
    -- and the ledger kept free actions without their session, so the goal's
    record of what it had done stayed empty."""
    import agent.toolset  # noqa: F401 - registers every executor before the fake replaces one
    from agent import actions
    from agent.turn import Deps

    async def draft(**args):
        return {"drafted": True, "draft_id": "d1"}

    monkeypatch.setitem(actions.EXECUTORS, "draft_email", draft)
    goal = _goal(conn)
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('goal')").lastrowid

    class Ctx:
        deps = Deps(conn=conn, session_id=session, channel="goal", goal_id=goal.id)

    asyncio.run(actions.act(Ctx(), "draft_email", {"to": ALEX, "subject": "Times", "body": "b"},
                            "Draft to Alex"))
    after = goals.after_session(conn, goal.id, cost_usd=0, session_id=session, report=None)
    assert len(after.actions) == 1
    assert "Draft to Alex -- done" in goals.session_note(conn, after)


def test_a_session_is_told_it_can_do_only_what_the_user_has_set_up(conn):
    """It was told to read their mail, calendar and files whatever they had,
    so a goal for someone with no mail connected went looking for a tool it
    did not have, and reported the step failed."""
    goal = _goal(conn)

    def can(on):
        note = goals.session_note(conn, goal, on)
        return next(line for line in note.splitlines() if "without the user:" in line)

    nothing, mail = can({}), can({"mail": ""})

    assert "their mail" not in nothing and "calendar" not in nothing
    assert "search the web" in nothing
    assert "read their mail and write drafts" in mail and "calendar" not in mail


def test_with_nothing_left_it_can_do_alone_it_sleeps_until_the_user_answers(conn):
    goal = _goal(conn, steps=["ask her which week"])
    goals.apply_update(conn, goal.id, steps=[{"n": 1, "status": "waiting_on_user"}],
                       ask_user="Which week?", now=SAT_10)
    goals.claim(conn, goals.get(conn, goal.id), SAT_10)
    asleep = goals.after_session(conn, goal.id, cost_usd=0, session_id=None, report=None)
    assert asleep.next_check_at is None
    assert goals.due(conn, SAT_10 + timedelta(days=3)) == []

    goals.answer(conn, goal.id, "the week of the 12th", question=1, now=SAT_10)
    awake = goals.get(conn, goal.id)
    assert awake.questions[0]["answer"] == "the week of the 12th"
    assert [g.id for g in goals.due(conn, SAT_10)] == [goal.id]


def test_user_note_wakes_an_active_goal_but_not_a_paused_one(conn):
    active, paused = _goal(conn, every_hours=24), _goal(conn, every_hours=24)
    for g in (active, paused):
        goals.claim(conn, g, SAT_10)
        goals.after_session(conn, g.id, cost_usd=0, session_id=None, report=None, now=SAT_10)
    goals.pause(conn, paused.id)
    later = SAT_10 + timedelta(hours=2)
    assert goals.answer(conn, active.id, "Alex prefers mornings", now=later)["wakes"]
    assert not goals.answer(conn, paused.id, "Alex prefers mornings", now=later)["wakes"]
    assert [g.id for g in goals.due(conn, later)] == [active.id]
    assert goals.get(conn, paused.id).notes[0]["text"] == "Alex prefers mornings"
    with pytest.raises(goals.BadGoal, match="no question"):
        goals.answer(conn, active.id, "yes", question=9)
    goals.drop(conn, active.id)
    with pytest.raises(goals.BadGoal, match="not running"):
        goals.answer(conn, active.id, "yes")


def test_mail_from_whom_it_waits_on_brings_it_forward_and_no_one_else(conn):
    waiting, other = _goal(conn, every_hours=24), _goal(conn, every_hours=24)
    for g in (waiting, other):
        goals.claim(conn, g, SAT_10)
        goals.after_session(conn, g.id, cost_usd=0, session_id=None, report=None, now=SAT_10)
    goals.apply_update(conn, waiting.id, waiting_on=["Alex Kim <ALEX@example.com>"])
    assert goals.get(conn, waiting.id).waiting_on == [ALEX]

    soon = SAT_10 + timedelta(minutes=5)
    assert goals.woken_by_mail(conn, ["Eve <eve@attacker.example>"], soon) == 0
    assert goals.woken_by_mail(conn, ["Alex Kim <alex@example.com>"], soon) == 1
    # Not sooner than the gap after its last session, and not later than it was.
    assert goals.get(conn, waiting.id).next_check_at == \
        (SAT_10 + goals.WAKE_GAP).isoformat()
    assert goals.woken_by_mail(conn, [ALEX], soon) == 0
    assert goals.get(conn, other.id).next_check_at == (SAT_10 + timedelta(hours=24)).isoformat()


def test_an_answered_card_brings_its_goal_forward(conn):
    goal = _goal(conn, every_hours=24)
    goals.claim(conn, goal, SAT_10)
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('goal')").lastrowid
        action = conn.execute("INSERT INTO actions (tool, args_json, args_hash, preview, reach,"
                              " reversal, status, session_id) VALUES ('send_email', '{}', 'h',"
                              " 'Email Alex', 'outward', 'irreversible', 'pending', ?)",
                              (session,)).lastrowid
    goals.after_session(conn, goal.id, cost_usd=0, session_id=session, report=None, now=SAT_10)
    hour_later = SAT_10 + timedelta(hours=1)
    assert goals.woken_by_action(conn, 999, hour_later) == 0
    assert goals.woken_by_action(conn, action, hour_later) == 1
    assert goals.get(conn, goal.id).next_check_at == hour_later.isoformat()


# --- what a session writes -----------------------------------------------------------


def test_a_session_moves_its_steps_and_keeps_its_findings(conn):
    goal = _goal(conn)
    result = goals.apply_update(
        conn, goal.id, steps=[{"n": 1, "status": "done", "note": "Tue 2pm, Wed 10am"},
                              {"n": 2, "status": "sideways"}, {"n": 9, "status": "done"}],
        add_steps=["confirm the room"], findings=["Alex is in Boston that week (his email)"],
        now=SAT_10)
    goal = goals.get(conn, goal.id)
    assert goal.steps[0] == {"n": 1, "text": "find three free times", "status": "done",
                             "note": "Tue 2pm, Wed 10am"}
    assert goal.steps[1]["status"] == "todo" and goal.steps[3]["text"] == "confirm the room"
    assert goal.findings[0]["text"].startswith("Alex is in Boston")
    assert len(result["errors"]) == 2 and result["ok"]
    assert goal.progress() == "1 of 4 steps done"


def test_findings_and_steps_are_bounded(conn, monkeypatch):
    monkeypatch.setattr(goals, "MAX_FINDINGS", 3)
    goal = _goal(conn)
    goals.apply_update(conn, goal.id, findings=[f"f{i}" for i in range(5)],
                       add_steps=[f"s{i}" for i in range(goals.MAX_STEPS)])
    goal = goals.get(conn, goal.id)
    assert [f["text"] for f in goal.findings] == ["f2", "f3", "f4"]
    assert len(goal.steps) == goals.MAX_STEPS


def test_a_question_is_asked_once_and_only_a_few_wait_at_a_time(conn):
    goal = _goal(conn)
    goals.apply_update(conn, goal.id, ask_user="Morning or afternoon?")
    again = goals.apply_update(conn, goal.id, ask_user="morning or afternoon?")
    assert "already waiting" in again["errors"][0]
    for i in range(goals.MAX_OPEN_QUESTIONS):
        goals.apply_update(conn, goal.id, ask_user=f"q{i}")
    assert len(goals.get(conn, goal.id).open_questions) == goals.MAX_OPEN_QUESTIONS


def test_done_ends_it_and_a_later_update_is_refused(conn):
    goal = _goal(conn)
    goals.apply_update(conn, goal.id, done=True, outcome="Tue 2pm, invite accepted")
    ended = goals.get(conn, goal.id)
    assert ended.status == "done" and ended.next_check_at is None
    assert ended.outcome == "Tue 2pm, invite accepted"
    assert goals.apply_update(conn, goal.id, findings=["x"])["ok"] is False


def test_a_late_update_never_revives_a_goal_the_user_stopped(conn, monkeypatch):
    """The user says STOP GOALS while a session is between reading its goal and
    writing to it: what it writes must not set the goal going again."""
    goal = _goal(conn)
    snapshot = goals.get(conn, goal.id)
    real_get = goals.get

    def stale_get(c, goal_id):
        goals.drop(c, goal_id)
        monkeypatch.setattr(goals, "get", real_get)
        return snapshot

    monkeypatch.setattr(goals, "get", stale_get)
    goals.apply_update(conn, goal.id, done=True, outcome="done", findings=["x"])
    after = real_get(conn, goal.id)
    assert after.status == "dropped" and after.findings == []


def test_it_may_look_again_sooner_but_not_in_a_tight_loop(conn):
    goal = _goal(conn)
    goals.apply_update(conn, goal.id, next_check_hours=0.01, now=SAT_10)
    assert goals.get(conn, goal.id).next_check_at == (SAT_10 + timedelta(minutes=30)).isoformat()


# --- what reaches them --------------------------------------------------------------


class _Reply:
    def __init__(self, text):
        self.text, self.cost_usd = text, 0.002


@pytest.fixture
def session(monkeypatch, conn):
    """`Conversation` replaced: what a session was built with, and what it
    does -- `seen["work"]` runs as the model's tool calls would."""
    seen: dict = {"work": None}

    class Convo:
        def __init__(self, c, **kw):
            seen["init"] = kw
            with c:
                self.session_id = c.execute(
                    "INSERT INTO sessions (channel) VALUES ('goal')").lastrowid

        async def send(self, text, **kw):
            seen["send"] = kw
            if seen["work"]:
                seen["work"]()
            if isinstance(seen["reply"], Exception):
                raise seen["reply"]
            return _Reply(seen["reply"])

        def end(self):
            pass

    monkeypatch.setattr(handlers, "Conversation", Convo)
    return seen


def _job(goal):
    class Job:
        prompt = str(goal.id)

    return Job()


def _run(goal) -> dict:
    return json.loads(asyncio.run(handlers.handle_goal(_job(goal))))


def test_a_session_runs_as_the_goal_with_its_tools_and_user_urls(conn, told, session):
    goal = _goal(conn, ask="Find a time with Alex; his page is https://alex.example/cal")
    session["reply"] = "NOTHING_NEW"
    out = _run(goal)
    assert session["init"]["channel"] == "goal" and session["init"]["goal_id"] == goal.id
    assert "find three free times" in session["init"]["note"]
    assert session["send"]["allow"] == goals.toolset(goal)
    assert session["send"]["seen"] == {"https://alex.example/cal"}
    assert out["ran"] and not out["reported"] and told == []
    assert goals.get(conn, goal.id).sessions == 1


def test_a_question_reaches_them_once_with_its_number(conn, told, session):
    goal = _goal(conn)
    session["work"] = lambda: goals.apply_update(conn, goal.id, ask_user="Zoom or phone?")
    session["reply"] = "Found three times: Tue 2pm, Wed 10am, Thu 4pm."
    _run(goal)
    session["work"] = None
    session["reply"] = "NOTHING_NEW"
    _run(goal)

    assert len(told) == 1
    text, noted = told[0]
    assert text.startswith('Goal "Call with Alex":') and "Tue 2pm" in text
    assert "Question: Zoom or phone?" in text
    assert f"[goal {goal.id}" in noted and "[question 1: Zoom or phone?]" in noted
    assert goals.get(conn, goal.id).questions[0]["told"] is True


def test_the_same_report_twice_is_said_once(conn, told, session):
    goal = _goal(conn)
    session["reply"] = "Waiting on Alex's reply."
    _run(goal)
    _run(goal)
    assert len(told) == 1


def test_done_is_said_with_its_outcome(conn, told, session):
    goal = _goal(conn)
    session["work"] = lambda: goals.apply_update(conn, goal.id, done=True,
                                                 outcome="Tuesday 2pm is on your calendar.")
    session["reply"] = "NOTHING_NEW"
    out = _run(goal)
    assert out["status"] == "done"
    assert told[0][0] == 'Goal "Call with Alex" is done:\nTuesday 2pm is on your calendar.'


@pytest.fixture
def link(monkeypatch):
    """What reached the outbox, with quiet hours over."""
    state: dict = {"got": []}

    async def notify(text, *, noted=None, **_):
        state["got"].append(text)
        return len(state["got"])

    monkeypatch.setattr(links, "notify_user", notify)
    monkeypatch.setattr("core.call_policy.within_quiet_hours", lambda now=None: False)
    monkeypatch.setattr("agent.jobs.enqueue", lambda c, **kw: 1)
    return state


def test_a_goals_report_and_its_question_go_to_the_outbox_once(conn, link, session):
    """The first live goal's "done", 4 October 2026, went out while the
    channel was down for six seconds, and nothing tried again. Then it was
    owed, and sent again by the goal loop until Signal took it. The outbox
    keeps trying it now (tests/unit/test_outbox.py); owed as well, it would
    be a second message."""
    goal = _goal(conn)
    session["work"] = lambda: goals.apply_update(conn, goal.id, ask_user="Zoom or phone?")
    session["reply"] = "Found three times."
    _run(goal)
    asyncio.run(background.goal_tick(SAT_10))

    [said] = link["got"]
    assert "Found three times." in said and "Zoom or phone?" in said
    after = goals.get(conn, goal.id)
    assert after.unsent is None and not goals.untold(after)


def test_a_goal_that_ran_out_of_time_says_so_after_quiet_hours(conn, link, monkeypatch):
    quiet = {"now": True}
    monkeypatch.setattr("core.call_policy.within_quiet_hours", lambda now=None: quiet["now"])
    goal = _goal(conn, deadline="2026-10-05")
    asyncio.run(background.goal_tick(datetime(2026, 10, 6, 0, 30)))
    assert goals.get(conn, goal.id).status == "expired" and link["got"] == []
    quiet["now"] = False
    asyncio.run(background.goal_tick(datetime(2026, 10, 6, 8, 0)))
    assert len(link["got"]) == 1 and "reached its deadline" in link["got"][0]


def test_a_failed_session_still_frees_the_goal(conn, told, session):
    goal = _goal(conn)
    goals.claim(conn, goal, SAT_10)
    session["reply"] = RuntimeError("model down")
    with pytest.raises(RuntimeError):
        _run(goal)
    after = goals.get(conn, goal.id)
    assert after.running_since is None and after.sessions == 1 and told == []


def test_a_spent_goal_pauses_and_says_so(conn, told, session):
    goal = _goal(conn, budget_usd=0.5)
    conn.execute("UPDATE goals SET spent_usd = 0.5 WHERE id = ?", (goal.id,))
    conn.commit()
    out = _run(goal)
    assert out == {"ran": False, "why": "goal budget"} and "init" not in session
    assert goals.get(conn, goal.id).status == "paused" and "is spent" in told[0][0]
    with pytest.raises(goals.BadGoal, match="spent"):
        goals.pause(conn, goal.id, resume=True)
    assert goals.pause(conn, goal.id, resume=True, add_budget_usd=1.0)


def test_the_days_budget_holds_every_goal_until_tomorrow_and_the_briefing_says_so(
        conn, told, session):
    """Held quietly: a message for each held goal was noise, and the set
    that kept it to one a day was in memory, so a restart said it again.
    The cap's notice is the morning's line (agent/caps.py)."""
    from agent import caps

    spend.bill(conn, 1.0, model="m", unit=spend.Unit("background", "goals"))
    first, second = _goal(conn), _goal(conn)
    goals.claim(conn, first, SAT_10)
    with spend.spending("background", part="goals", conn=conn):
        assert _run(first)["why"] == "day budget" and _run(second)["why"] == "day budget"
    assert "init" not in session and told == []
    assert goals.get(conn, first.id).running_since is None
    assert goals.get(conn, first.id).status == "active"
    [line] = caps.lines(caps.for_the_briefing(conn))
    assert line.startswith("Goals paused at ") and "$1.00 daily limit" in line


def test_only_goal_runs_count_against_the_goals_day_budget(conn):
    """Their conversations' spend is not the goals'. Counted together, a busy
    day of talking would have held every goal; until 11 October 2026 no
    test said so, and routines' twin of this did."""
    for category, part, cost in (("background", "goals", 0.3), ("chat", None, 3.0),
                                 ("background", "routines", 0.4)):
        with spend.spending(category, part=part, conn=conn):
            spend.bill(conn, cost, model="m")
    conn.commit()
    assert spend.spent(conn, spend.windows().day.since, part="goals") == pytest.approx(0.3)
    with spend.spending("background", part="goals", conn=conn):
        assert spend.may_spend(conn) is None


def test_a_goal_not_running_gets_no_session(conn, told, session):
    goal = _goal(conn)
    goals.pause(conn, goal.id)
    assert _run(goal) == {"ran": False, "why": "not active"}


# --- the loop ----------------------------------------------------------------------


def test_the_loop_starts_sessions_but_not_in_quiet_hours(conn, told, monkeypatch):
    queued: list[str] = []
    monkeypatch.setattr("agent.jobs.enqueue", lambda c, **kw: queued.append(kw["prompt"]) or 1)
    goal = _goal(conn)
    monkeypatch.setattr("core.call_policy.within_quiet_hours", lambda now=None: True)
    assert asyncio.run(background.goal_tick(SAT_10)) == []
    monkeypatch.setattr("core.call_policy.within_quiet_hours", lambda now=None: False)
    assert asyncio.run(background.goal_tick(SAT_10)) == [1]
    assert asyncio.run(background.goal_tick(SAT_10)) == []
    assert queued == [str(goal.id)]


def test_a_goal_past_its_deadline_ends_and_the_user_is_told_once(conn, told, monkeypatch):
    monkeypatch.setattr("core.call_policy.within_quiet_hours", lambda now=None: False)
    monkeypatch.setattr("agent.jobs.enqueue", lambda c, **kw: 1)
    goal = _goal(conn, deadline="2026-10-05")
    later = datetime(2026, 10, 6, 9, 0)
    asyncio.run(background.goal_tick(later))
    asyncio.run(background.goal_tick(later))
    assert goals.get(conn, goal.id).status == "expired"
    assert len(told) == 1 and "reached its deadline" in told[0][0]


def test_attention_wakes_a_goal_when_the_reply_it_waits_for_arrives(conn, monkeypatch):
    from agent import attention, offloop
    from agent.attention import sources
    from agent.attention.store import Observation

    # The real clock: `look` wakes it as of now. Only the order matters.
    goal = _goal(conn, every_hours=24, now=datetime.now())
    goals.claim(conn, goal, datetime.now())
    goals.after_session(conn, goal.id, cost_usd=0, session_id=None, report=None,
                        now=datetime.now() - timedelta(hours=2))
    goals.apply_update(conn, goal.id, waiting_on=[ALEX])
    before = goals.get(conn, goal.id).next_check_at


    gmail = f"mail:{fake_google.KEY}"
    reply = Observation(source=gmail, key="mail:1", title="Re: call -- from Alex",
                        detail="Gmail, unread.", ref={"from": f"Alex <{ALEX}>"})
    monkeypatch.setattr(sources, "watching",
                        lambda conn: [sources.Source(gmail, lambda c: [reply], feature="mail")])

    async def direct(c, fn, *args):
        return fn(c, *args)

    monkeypatch.setattr(offloop, "with_own_conn", direct)
    monkeypatch.setattr(sources, "collect", lambda c, source: source.collect(c))
    asyncio.run(attention.look(conn, force=True))
    assert goals.get(conn, goal.id).next_check_at < before


# --- the briefing ------------------------------------------------------------------


def test_the_morning_briefing_has_a_line_a_goal(conn):
    from agent.attention import briefing

    goal = _goal(conn)
    goals.apply_update(conn, goal.id, steps=[{"n": 1, "status": "done"}],
                       ask_user="Zoom or phone?", waiting_on=[ALEX])
    _goal(conn, title="Boston trip", envelope={"send_email": {"targets": [ALEX]}})
    lines = goals.briefing_lines(conn)
    assert lines[0] == ("  · Call with Alex: 1 of 3 steps done; 1 question for you; "
                        "waiting on alex@example.com; $0.00 of $2.00")
    assert "waiting for your yes on its card" in lines[1]

    facts = briefing.Facts(kind="morning", about=SAT_10.date())
    facts.goals = lines
    assert "Goals:\n  · Call with Alex" in briefing.render(facts)
