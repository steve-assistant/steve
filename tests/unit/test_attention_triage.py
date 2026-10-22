"""The judge deciding what to tell them unasked sees what the agent just did
for them, what the user just said, and what the user has said about pings like these.

1 October 2026: "How did the Fabrikam Seattle coffee chat go? If you'd like a
thank-you note..." nine minutes after the agent drafted that note. 7
October: LinkedIn's new-device alerts judged `now` five minutes after the
agent's own LinkedIn sign-in. Both were in the database the judge never read.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from agent import history
from agent.attention import store, triage
from agent.attention.store import Observation
from core import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_zone", lambda: ZoneInfo("America/New_York"))
    c = db.open_store(tmp_path / "triage.db")
    yield c
    c.close()


def _action(conn, tool: str, preview: str, *, ago: str = "-9 minutes", status: str = "done"):
    with conn:
        conn.execute(
            "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal, status,"
            " created_at) VALUES (?, '{}', ?, ?, 'private', 'reversible', ?,"
            " datetime('now', ?))", (tool, f"{tool}{ago}", preview, status, ago))


def _user_said(conn, text: str, *, channel: str = "signal", ago: str = "-20 minutes"):
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES (?)", (channel,)).lastrowid
        conn.execute("INSERT INTO messages (session_id, role, content, created_at)"
                     " VALUES (?, 'user', ?, datetime('now', ?))", (session, text, ago))


def test_the_judge_knows_who_the_user_is_from_their_profile(conn, monkeypatch):
    """It read `memories` of kind 'user', which nothing has written since the
    profile replaced them: the judge decided what mattered "for this
    person" knowing nothing of them."""
    from agent import calendars, facts

    monkeypatch.setattr(triage, "_CONTEXT", {"at": float("-inf"), "text": ""})
    monkeypatch.setattr(calendars, "agenda", lambda *a, **k: {"events": []})
    facts.add(conn, facts.USER, "occupation", "night-shift nurse", source="confirmed")

    context = triage.day_context(conn)

    assert "What the user has said about themselves" in context
    assert "night-shift nurse" in context


def test_the_people_in_a_meeting_are_looked_up_only_if_the_user_chose_it(monkeypatch):
    """Every meeting with anyone in it got a consultant's brief -- who they
    are, their organisation's news -- so a school pickup got a dossier."""
    from agent.attention import settings

    off = triage.judge_prompt({"calendar"})
    monkeypatch.setattr(settings, "MEETING_RESEARCH", "on")
    on = triage.judge_prompt({"calendar"})

    assert "heads-up" in off and "who they are" not in off
    assert triage.MEETING_RESEARCH_SAYS.strip() in on


def test_the_judges_instructions_open_the_same_whatever_the_user_has():
    """What the user has was named in the first line, so the provider's cache
    of the instructions was lost whenever it changed."""
    one, other = triage.judge_prompt({"gmail"}), triage.judge_prompt({"weather", "news"})
    opening = one.index("What you are shown:")

    assert one[:opening] == other[:opening]


def test_every_source_has_a_paragraph_for_the_judge():
    """A source without one has its items judged by a model told nothing of
    what they are."""
    from agent.attention import sources

    covered = set().union(*(names for names, _, _ in triage.JUDGE_SOURCES))

    assert {s.name for s in sources.all_sources()} <= covered


def test_the_judge_is_told_the_draft_it_just_wrote_and_what_the_user_just_said(conn):
    _action(conn, "draft_email",
            "Draft to Asha Verma <Asha.Verma@fabrikam.example>\nSubject: Thank you")
    _action(conn, "browse_use_login",
            "Use your linkedin login (linkedin.com) to: Open Owen Tate's profile",
            ago="-5 minutes")
    _user_said(conn, "draft thank you note for asha from my work email")

    said = triage.lately(conn)

    assert "What you did for the user" in said
    assert "draft_email: Draft to Asha Verma" in said and "Subject: Thank you" in said
    assert "browse_use_login: Use your linkedin login" in said
    assert '"draft thank you note for asha from my work email"' in said


def test_what_the_user_said_about_pings_like_these_reaches_the_judge(conn):
    from agent import issues

    issues.record(conn, "feedback_note", "no thank you note",
                  "Maya: great, no thank you note it's much more chill",
                  excerpt="WILD interview follow-up; don't suggest a thank-you note")

    said = triage.lately(conn)

    assert "no thank you note it's much more chill" in said and "WILD interview" in said


HURRICANE = ("Hurricane Warning in effect near Ridgeview: tropical-storm-force winds are"
             " forecast early Friday")


def _pinged(conn, line: str, *, how: str = "ping", ago_hours: int = 2, **where) -> None:
    from agent.attention import told

    stamp = conn.execute("SELECT datetime('now', ?)", (f"{-ago_hours:+d} hours",)).fetchone()[0]
    assert stamp is not None
    told.record(conn, f"{how}:{stamp}", how, [told.Line(line, **where)], said_at=stamp)


def test_their_thumbs_down_on_a_ping_reaches_the_judge_with_where_the_ping_came_from(conn):
    """Their thumbs went to the failure ledger alone: 18 thumbs-down from 29
    September to 9 October, one on the night's hurricane warning for
    Ridgeview, and the judge deciding what to send them next read none."""
    from agent import issues

    _pinged(conn, HURRICANE, obs_id=None, source="weather", place="Ridgeview, WA",
            reason="safety")
    issues.record(conn, "feedback_down", "hurricane", "Maya gave a thumbs-down to: ...",
                  excerpt=f"Sorry for the hour -- this one can't wait.\n{HURRICANE}")

    said = triage.lately(conn)

    assert "How the user took what you sent them unasked" in said
    assert "thumbs-down on a ping" in said and HURRICANE[:60] in said
    assert "(weather, about Ridgeview, WA, your reason: safety)" in said


def test_a_thumb_on_a_reply_or_on_a_briefing_is_not_read_as_one_on_a_ping(conn):
    """A thumb on a reply in conversation is about the conversation, and a
    thumb on a briefing does not say which of its lines it means; one given
    before a ping went out cannot be about it."""
    from agent import issues

    _pinged(conn, "Con Ed bill, $84, due tomorrow", how="morning", source="mail")
    _pinged(conn, HURRICANE, ago_hours=-1, source="weather")
    issues.record(conn, "feedback_down", "bill", "Maya gave a thumbs-down to: ...",
                  excerpt="Good morning.\nCon Ed bill, $84, due tomorrow")
    issues.record(conn, "feedback_down", "storm", "Maya gave a thumbs-down to: ...",
                  excerpt=HURRICANE)
    issues.record(conn, "feedback_down", "owen", "Maya gave a thumbs-down to: ...",
                  excerpt="Drafted a thank-you note to Owen Tate from your Outlook inbox")

    assert "How she took" not in triage.lately(conn)


def test_only_lately_only_user_words_and_nothing_that_failed(conn):
    _action(conn, "create_event", "Add Gym at PWG", ago="-8 hours")
    _action(conn, "send_email", "Email to someone", status="rejected")
    _user_said(conn, "an old question", ago="-4 hours")
    _user_said(conn, "The attention loop's own task: draft to Rina", channel="initiative")

    assert triage.lately(conn) == ""


async def test_the_judges_prompt_carries_it_after_the_day_and_before_the_clock(monkeypatch):
    import agent.llm

    sent: list[str] = []

    async def fake_run_agent(agent_, prompt, role, **kwargs):
        sent.append(prompt)
        return type("R", (), {"output": triage.Verdicts(items=[])})(), None

    monkeypatch.setattr(agent.llm, "run_agent", fake_run_agent)
    await triage.judge([Observation(source="gmail", key="mail:a", title="LinkedIn: new device")],
                       "Her next two days:", "What you did for her in the last 6 hours:")

    [prompt] = sent
    assert (prompt.index("Her next two days") < prompt.index("What you did for her")
            < prompt.index("It is ") < prompt.index("mail:a"))


async def test_triage_hands_the_judge_what_happened_lately(conn, monkeypatch):
    store.record_new(conn, [Observation(source="gmail", key="mail:a", title="New device")])
    _action(conn, "browse_use_login", "Use your linkedin login (linkedin.com)")
    monkeypatch.setattr(triage, "day_context", lambda _c: "")
    seen: list[str] = []

    async def judge(batch, context, recent="", **_k):
        seen.append(recent)
        raise RuntimeError("stop here")

    monkeypatch.setattr(triage, "judge", judge)

    await triage.triage(conn)

    assert "browse_use_login" in seen[0]
