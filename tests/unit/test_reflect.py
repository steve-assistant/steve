"""Reflection: scoring a finished session without leaking it.

The load-bearing test here is the first one. Reflection is the only path that
ships whole conversations -- mail, calendar, whereabouts -- to a model for a
purpose the user never asked for, and the value of a rubric score is small
next to the cost of leaking any of it.

So rather than trusting that `reflect()` calls `redact()`, these plant
realistically-shaped detail in a session and assert on **what the model was
actually handed**. That is the only claim that means anything: a test that
mocks the redactor proves the code calls a function, not that the function
worked.
"""

from __future__ import annotations

import json

import pytest

from agent import jobs, reflect
from agent.redact import Redacted
from core.rubric import POOR
from tests.gate.extras import needs_reflect

# Invented, but structurally valid -- the obvious placeholders are allow-listed
# by presidio and would pass this test while proving nothing.
PLANTED = {
    "email": "maya@contoso.example",
    "phone": "412-555-0148",
    "address": "12 Maple Street",
    "card": "4111-1111-1111-1111",
    "ssn": "521-84-3372",
}


def _session(conn, turns, *, channel="signal", ended=True):
    cur = conn.execute(
        "INSERT INTO sessions (channel, title, ended_at) VALUES (?, ?, ?)",
        (channel, "test", "2026-09-20 12:00:00" if ended else None),
    )
    session_id = int(cur.lastrowid)
    for role, content, *rest in turns:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, model)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, rest[0] if rest else None, None),
        )
    conn.commit()
    return session_id


def _verdict(**overrides):
    base = dict(answered=4, grounded=4, efficient=4, safe=5, clear=4)
    base.update(overrides)
    return reflect.Verdict(**base)


@pytest.fixture
def spy_judge(monkeypatch):
    """Capture exactly what would have gone over the wire."""
    seen = {}

    async def fake_run_agent(agent, prompt, role, **kwargs):
        seen["prompt"] = prompt
        seen["role"] = role
        result = type("R", (), {"output": seen.get("verdict") or _verdict()})()

        class Rec:
            cost_usd = 0.0001

            def as_usage_row(self):
                return {
                    "model": "openai/gpt-6-luna", "effort": "none",
                    "input_tokens": 900, "cached_tokens": 0, "output_tokens": 80,
                    "reasoning_tokens": 0, "cost_usd": 0.0001, "latency_ms": 1200,
                }

        return result, Rec()

    import agent.llm as llm

    monkeypatch.setattr(llm, "run_agent", fake_run_agent)
    return seen


# --------------------------------------------------------------------------
# Nothing personal reaches the judge
# --------------------------------------------------------------------------


@needs_reflect
async def test_planted_detail_never_reaches_the_model(store, spy_judge):
    """The whole point of the phase. Asserted on the prompt the model was
    handed, not on redact() having been called."""
    session_id = _session(
        store,
        [
            ("user", f"Email {PLANTED['email']} about the lease."),
            ("assistant", f"Sent. I also have your number as {PLANTED['phone']}."),
            ("user", f"I live at {PLANTED['address']}. Card is {PLANTED['card']}."),
            ("user", f"And my SSN is {PLANTED['ssn']} if the form needs it."),
        ],
    )

    await reflect.reflect(store, session_id)

    sent = spy_judge["prompt"]
    assert sent, "the judge was never called"
    for name, value in PLANTED.items():
        assert value not in sent, f"{name} reached the judge verbatim"


@needs_reflect
async def test_the_transcript_still_reads_as_a_conversation(store, spy_judge):
    """Redaction that destroys structure makes the score meaningless, so the
    turn markers and the shape of the exchange have to survive it."""
    session_id = _session(
        store,
        [
            ("user", "What did Alice say about the lease?"),
            ("assistant", "Alice asked to move the date; Bob has not replied."),
        ],
    )
    await reflect.reflect(store, session_id)

    sent = spy_judge["prompt"]
    assert "\nassistant:" in sent and sent.startswith("user [")
    assert "Alice" not in sent and "Bob" not in sent
    assert "lease" in sent, "redaction ate ordinary words"


def test_user_messages_carry_the_time_the_user_sent_them_on_their_clock(store, monkeypatch):
    """The assistant is told the date and time with every message; the judge
    was not, and marked "today is Monday" and "8:01 PM EDT" unsupported."""
    from zoneinfo import ZoneInfo

    monkeypatch.setattr("tzlocal.get_localzone", lambda: ZoneInfo("America/New_York"))
    session_id = _session(store, [("user", "what time is it?"),
                                  ("assistant", "It's 8:01 PM EDT.")])
    store.execute("UPDATE messages SET created_at = '2026-10-06 00:01:00' WHERE session_id = ?",
                  (session_id,))

    text = reflect.transcript(store, session_id)

    assert "user [Mon 05 Oct 2026 20:01 EDT]: what time is it?" in text
    assert "stamped with the local time" in reflect.JUDGE_PROMPT


@needs_reflect
async def test_reflection_is_recorded_as_redacted(store, spy_judge):
    session_id = _session(store, [("user", "Book me a table for Thursday please."),
         ("assistant", "Booked for 7pm.")])
    await reflect.reflect(store, session_id)

    row = store.execute("SELECT * FROM reflections WHERE session_id = ?",
                        (session_id,)).fetchone()
    assert row is not None
    assert row["redacted"] == 1, "0 here is a bug, never a choice"
    assert set(json.loads(row["scores_json"])) == set(reflect.KEYS)


def test_the_judge_refuses_a_raw_transcript():
    """Type, not discipline: passing a str has to fail rather than send."""
    import asyncio

    with pytest.raises(TypeError, match="Redacted"):
        asyncio.run(reflect.judge("my SSN is 521-84-3372"))


def test_a_redacted_transcript_is_the_only_way_in():
    """The constructor exists, so the guard is about the call site, not an
    impossible state -- this pins that the type is what is checked."""
    assert isinstance(Redacted(text="clean"), Redacted)


async def test_a_redacted_transcript_reaches_the_bulk_model_and_its_verdict_comes_back(
        spy_judge):
    """The judge asks through `llm.ask_once`, as every one-shot question
    does. The tests of a whole reflection need the redaction extra, so
    without this one nothing here ran the judge's own call."""
    from agent.models import Role

    verdict, record = await reflect.judge(Redacted(text="user: hello"))

    assert (spy_judge["prompt"], spy_judge["role"]) == ("user: hello", Role.BULK)
    assert verdict == _verdict() and record.cost_usd == 0.0001


# --------------------------------------------------------------------------
# Cheap tier
# --------------------------------------------------------------------------


@needs_reflect
async def test_reflection_runs_on_the_bulk_tier(store, spy_judge):
    """Latency is irrelevant here and the bill is not."""
    from agent.models import Role

    session_id = _session(store, [("user", "Summarise what we did this week."),
         ("assistant", "Three things shipped.")])
    await reflect.reflect(store, session_id)
    assert spy_judge["role"] is Role.BULK


# --------------------------------------------------------------------------
# What gets kept
# --------------------------------------------------------------------------


@needs_reflect
async def test_a_lesson_is_written_to_the_memories_repo(store, spy_judge, monkeypatch):
    kept = {}

    def fake_remember(conn, *, kind, summary, body, name=None):
        kept.update(kind=kind, summary=summary, body=body)
        return type("M", (), {"path": "feedback/check-the-calendar.md"})()

    import tools.memory as memory

    monkeypatch.setattr(memory, "remember", fake_remember)
    spy_judge["verdict"] = _verdict(grounded=2, lesson="Check the calendar before guessing.")

    session_id = _session(store, [("user", "When am I free on Thursday?"),
         ("assistant", "After 2pm.")])
    result = await reflect.reflect(store, session_id)

    assert result.lesson_path == "feedback/check-the-calendar.md"
    # "feedback" is guidance-on-how-to-work, which is what a lesson is; a
    # bespoke kind would sit in a category nothing ever searches.
    assert kept["kind"] == "feedback"
    assert "Why:" in kept["body"], "a lesson without its reason is an instruction"


@needs_reflect
async def test_only_their_conversations_write_lessons(store, spy_judge, monkeypatch):
    """Lessons are what `recall` brings back as how to work with them. 83% of
    the sessions reflected on were the agent's own, and memory filled with
    lessons about the night's research (6 October 2026). Those are still
    scored, and write nothing."""
    kept: list[str] = []

    def fake_remember(conn, *, kind, summary, body, name=None):
        kept.append(summary)
        return type("M", (), {"path": f"feedback/{len(kept)}.md"})()

    import tools.memory as memory

    monkeypatch.setattr(memory, "remember", fake_remember)
    spy_judge["verdict"] = _verdict(grounded=2, lesson="Cite only what the pages said.")
    turns = [("user", "Research tent camping in Delaware."), ("assistant", "Done.")]

    night = await reflect.reflect(store, _session(store, turns, channel="night"))
    theirs = await reflect.reflect(store, _session(store, turns, channel="signal"))

    assert night.lesson_path is None and night.scores, "scored, but no lesson"
    assert theirs.lesson_path == "feedback/1.md" and len(kept) == 1


@needs_reflect
async def test_no_lesson_when_there_is_nothing_to_learn(store, spy_judge):
    spy_judge["verdict"] = _verdict(lesson="")
    session_id = _session(store, [("user", "Thanks, that is all sorted now."),
         ("assistant", "Glad to hear it.")])
    result = await reflect.reflect(store, session_id)
    assert result.lesson_path is None


# --------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------


def _filed(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, summary, excerpt, count, session_id FROM issues WHERE kind = 'reflection'")]


@needs_reflect
async def test_a_poor_score_files_a_proposal_in_the_failure_ledger(store, spy_judge):
    """Queued as an `upgrade` job, it was claimed by the job worker at once,
    in the day, so the night never saw one. In the ledger the night weighs
    it with everything else."""
    spy_judge["verdict"] = _verdict(
        efficient=POOR, proposal="Cache the calendar lookup for the current day."
    )
    session_id = _session(store, [("user", "What is on my calendar today?"),
         ("assistant", "Two meetings.")])
    result = await reflect.reflect(store, session_id)

    [filed] = _filed(store)
    assert result.proposal_issue_id == filed["id"]
    assert "efficient" in filed["summary"] and "Cache the calendar lookup" in filed["summary"]
    assert filed["excerpt"].startswith("Cache the calendar lookup")
    assert filed["session_id"] == session_id


@needs_reflect
async def test_the_same_weakness_twice_is_one_issue_seen_twice(store, spy_judge):
    """One issue per rubric dimension, whatever each proposal's wording."""
    for proposal in ("Cache the calendar lookup.", "Remember today's agenda between turns."):
        spy_judge["verdict"] = _verdict(efficient=POOR, proposal=proposal)
        await reflect.reflect(store, _session(store, [("user", "What is on today?"),
                                                      ("assistant", "Two meetings.")]))

    [filed] = _filed(store)
    assert filed["count"] == 2 and "Remember today's agenda" in filed["summary"]


@needs_reflect
async def test_a_merely_average_score_does_not_file_anything(store, spy_judge):
    """Otherwise every session files a proposal and the ledger becomes noise."""
    spy_judge["verdict"] = _verdict(efficient=POOR + 1, proposal="Could be faster.")
    session_id = _session(store, [("user", "What is on my calendar today?"),
         ("assistant", "Two meetings.")])
    result = await reflect.reflect(store, session_id)

    assert result.proposal_issue_id is None
    assert _filed(store) == []


@needs_reflect
async def test_a_proposal_is_filed_never_applied_nor_queued_as_theirs(store, spy_judge):
    """The distance from "noticed a weakness" to "may edit the codebase" is
    the whole of phase 6. And no job: a session on an upgrade job reads as
    "the user asked for this", which is how reflection's ideas once ran past
    the self-upgrade switch as their requests."""
    spy_judge["verdict"] = _verdict(safe=1, proposal="Rewrite the tool dispatcher.")
    session_id = _session(store, [("user", "Delete all my drafts."),
         ("assistant", "Deleted them.")])
    await reflect.reflect(store, session_id)

    assert all(j.kind != "upgrade" for j in jobs.recent(store))
    assert len(_filed(store)) == 1


# --------------------------------------------------------------------------
# Bookkeeping
# --------------------------------------------------------------------------


@needs_reflect
async def test_a_session_is_only_reflected_once(store, spy_judge):
    session_id = _session(store, [("user", "Remind me to call the bank tomorrow."),
         ("assistant", "Reminder set.")])
    assert session_id in reflect.unreflected(store)

    await reflect.reflect(store, session_id)
    assert session_id not in reflect.unreflected(store)


async def test_an_unfinished_session_is_not_picked_up(store):
    """Reflecting mid-conversation would score a turn that has not happened."""
    session_id = _session(store, [("user", "hold on")], ended=False)
    assert session_id not in reflect.unreflected(store)


async def test_a_session_the_agent_never_answered_is_not_judged(store, spy_judge):
    """Every dimension of the rubric asks about a reply, so there is nothing
    to score -- and judging it would still cost."""
    session_id = _session(store, [("user", "hi")])
    assert await reflect.reflect(store, session_id) is None
    assert "prompt" not in spy_judge, "paid to judge an unanswered message"
    assert session_id not in reflect.unreflected(store)


@needs_reflect
async def test_a_short_but_real_exchange_is_still_judged(store, spy_judge):
    """An earlier version skipped transcripts under 40 characters, which threw
    away exactly this -- the commonest kind of session there is."""
    session_id = _session(
        store,
        [("user", "Free Thursday?"), ("assistant", "After 2pm.")],
    )
    assert await reflect.reflect(store, session_id) is not None
    assert spy_judge["prompt"], "a real exchange went unjudged"


def test_averages_are_reported_per_dimension(store):
    for scores in ({"answered": 5, "grounded": 3}, {"answered": 3, "grounded": 1}):
        store.execute(
            "INSERT INTO reflections (session_id, rubric, scores_json) VALUES (NULL, '1', ?)",
            (json.dumps(scores),),
        )
    store.commit()
    avg = reflect.averages(store)
    assert avg["answered"] == 4.0
    assert avg["grounded"] == 2.0


def test_averages_on_an_empty_table_is_not_an_error(store):
    assert reflect.averages(store) == {}


# --------------------------------------------------------------------------
# Transcript rendering
# --------------------------------------------------------------------------


def test_the_judge_sees_what_the_tools_returned(store):
    """Shown by name only, a calendar result could not show that "9am
    Friday" was right, and `grounded` read correct answers as unsupported."""
    import json

    event = {"id": "abc123", "summary": "DES 203", "start": "2026-09-25T09:00:00-04:00",
             "etag": "x", "location": ""}
    session_id = _session(store, [
        ("user", "When is DES 203?"),
        ("tool", json.dumps({"events": [event]}), "calendar_search"),
        ("assistant", "Friday at 9am."),
    ])

    text = reflect.transcript(store, session_id)

    assert '[tool: calendar_search returned {"events":[{"summary":"DES 203",' in text
    assert "2026-09-25T09:00:00-04:00" in text
    assert "abc123" not in text and "etag" not in text and "location" not in text


def test_old_and_huge_tool_results_stay_out(store, monkeypatch):
    monkeypatch.setattr(reflect, "SHOWN_TOOL_RESULTS", 1)
    session_id = _session(store, [
        ("user", "What is the weather?"),
        ("tool", "old result", "web_search"),
        ("tool", "x" * 50_000, "read_page"),
        ("assistant", "Sunny."),
    ])

    text = reflect.transcript(store, session_id)

    assert "[tool: web_search]" in text and "old result" not in text
    assert len(text) < reflect.TOOL_RESULT_CHARS + 500


def test_a_long_transcript_keeps_its_ending(store, monkeypatch):
    """The outcome is where the failures show; the opening is pleasantries."""
    monkeypatch.setattr(reflect, "MAX_TRANSCRIPT_CHARS", 200)
    session_id = _session(
        store,
        [("user", "x" * 400), ("assistant", "THE ENDING")],
    )
    text = reflect.transcript(store, session_id)
    assert "THE ENDING" in text
    assert "truncated" in text


# --------------------------------------------------------------------------
# Ending a session
# --------------------------------------------------------------------------


def _age_messages(conn, session_id, minutes):
    conn.execute(
        "UPDATE messages SET created_at = datetime('now', ?) WHERE session_id = ?",
        (f"-{minutes} minutes", session_id),
    )
    conn.execute(
        "UPDATE sessions SET started_at = datetime('now', ?) WHERE id = ?",
        (f"-{minutes} minutes", session_id),
    )
    conn.commit()


def test_a_quiet_session_is_ended(store):
    """Nothing else ever sets ended_at: a voice session has no hang-up and a
    Signal thread has no close button, so silence is the only signal."""
    session_id = _session(
        store, [("user", "night"), ("assistant", "goodnight")], ended=False
    )
    _age_messages(store, session_id, reflect.IDLE_MINUTES + 5)

    assert reflect.close_idle(store) == [session_id]
    row = store.execute("SELECT ended_at FROM sessions WHERE id = ?",
                        (session_id,)).fetchone()
    assert row["ended_at"] is not None


def test_a_live_conversation_is_left_alone(store):
    """Ending a session mid-exchange would split one conversation in two and
    reflect on half of it."""
    _session(store, [("user", "hold on")], ended=False)
    assert reflect.close_idle(store) == []


def test_an_empty_session_still_ends_on_its_start_time(store):
    """A session opened and never used has no messages to measure silence
    from, and would otherwise stay open forever."""
    cur = store.execute(
        "INSERT INTO sessions (channel, started_at)"
        " VALUES ('voice', datetime('now', '-90 minutes'))"
    )
    store.commit()
    assert int(cur.lastrowid) in reflect.close_idle(store)


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------


def test_the_sweep_ends_and_queues_in_one_pass(store):
    session_id = _session(
        store, [("user", "what is the time"), ("assistant", "noon")], ended=False
    )
    _age_messages(store, session_id, reflect.IDLE_MINUTES + 5)

    queued = reflect.sweep(store)
    assert len(queued) == 1
    job = jobs.get(store, queued[0])
    assert job.kind == "reflect"
    assert job.prompt == str(session_id)
    # Below even an upgrade proposal: reflection is the lowest-priority thing
    # the agent does and must never delay a real request.
    assert job.priority < -1


def test_sweeping_twice_does_not_queue_the_same_session_again(store):
    """The sweep runs every few minutes; without this the queue fills with
    duplicate reflections of one conversation."""
    session_id = _session(
        store, [("user", "hi there"), ("assistant", "hello")], ended=False
    )
    _age_messages(store, session_id, reflect.IDLE_MINUTES + 5)

    first = reflect.sweep(store)
    second = reflect.sweep(store)
    assert len(first) == 1
    assert second == []


def test_a_sweep_with_nothing_to_do_is_quiet(store):
    assert reflect.sweep(store) == []


@needs_reflect
async def test_the_judges_own_spend_lands_in_the_ledger(store, spy_judge):
    """Reflection runs on every session without anybody asking for it, so it
    is exactly the cost that goes unwatched. Nine real reflections had
    produced zero usage rows before this."""
    session_id = _session(
        store,
        [("user", "What is on for tomorrow?"), ("assistant", "One meeting at 9.")],
    )
    await reflect.reflect(store, session_id)

    row = store.execute(
        "SELECT * FROM usage_events WHERE session_id = ?", (session_id,)
    ).fetchone()
    assert row is not None, "reflection spent money and left no trace of it"
    assert row["cost_usd"] > 0
    assert row["effort"] == "none", "reflection must stay on the cheap tier"


# --------------------------------------------------------------------------
# Grounding: the half of the rubric the judge could not see
# --------------------------------------------------------------------------


def _session_with(store, *, answer: str, tool_output: str | None) -> int:
    with store:
        cur = store.execute("INSERT INTO sessions (channel) VALUES ('tray')")
        session_id = int(cur.lastrowid)
        store.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)",
            (session_id, "what did they say?"),
        )
        if tool_output is not None:
            store.execute(
                "INSERT INTO messages (session_id, role, content, tool_name)"
                " VALUES (?, 'tool', ?, 'search_email')",
                (session_id, tool_output),
            )
        store.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', ?)",
            (session_id, answer),
        )
    return session_id


def test_a_claim_the_tool_returned_is_marked_supported(store):
    session_id = _session_with(
        store,
        answer="The deadline for the Northwind application is the fourth of November.",
        tool_output="Subject: Northwind\nThe deadline for the Northwind application is the "
                    "fourth of November, no extensions.",
    )

    report = reflect.grounding(store, session_id)

    assert report.counts["directly supported"] == 1


def test_a_claim_no_tool_returned_is_marked_unsupported(store):
    """The one that matters. An invented specific is invisible in a
    transcript that shows only which tools were called."""
    session_id = _session_with(
        store,
        answer="Your flight leaves from gate B14 at twenty past six in the morning.",
        tool_output="Subject: Northwind\nThe deadline for the application is November.",
    )

    report = reflect.grounding(store, session_id)

    assert report.counts["unsupported"] == 1


def test_a_conversation_with_no_tool_calls_is_not_graded_on_grounding(store):
    """It is not ungrounded, it is a conversation. Handing the judge an
    all-unsupported report for small talk would teach it to mark everything
    down."""
    session_id = _session_with(store, answer="Yes, that is right.", tool_output=None)

    assert reflect.grounding(store, session_id) is None


def test_pleasantries_are_not_treated_as_claims(store):
    session_id = _session_with(
        store, answer="Sure.", tool_output="Subject: Northwind\nDeadline is November.",
    )

    assert reflect.grounding(store, session_id) is None


async def test_the_evidence_notes_are_redacted_with_the_transcript(store, monkeypatch):
    """They quote tool output verbatim, so they carry exactly the personal
    detail redaction exists to strip. Appending after the redaction pass would
    send an unredacted mail body to the judge."""
    seen = {}

    def fake_redact(text):
        seen["text"] = text
        from agent.redact import Redacted

        return Redacted(text="[redacted]", found={"PERSON": 1})

    async def fake_judge(clean):
        seen["judged"] = clean.text
        return reflect.Verdict(
            answered=4, grounded=4, efficient=4, safe=4, clear=4,
            notes="fine", lesson="", proposal="",
        ), None

    monkeypatch.setattr(reflect, "redact", fake_redact)
    monkeypatch.setattr(reflect, "judge", fake_judge)
    session_id = _session_with(
        store,
        answer="The deadline for the Northwind application is the fourth of November.",
        tool_output="From: recruiting@northwind.example\nThe deadline for the Northwind "
                    "application is the fourth of November.",
    )

    await reflect.reflect(store, session_id)

    assert "Evidence notes" in seen["text"], "the notes skipped redaction"
    assert "recruiting@northwind.example" not in seen["judged"]


def test_a_markdown_list_is_many_claims_not_one():
    """The first live run's failure. A week's agenda came back as bullets
    with no full stops, so sentence splitting produced a single 900-character
    "claim" that matched nothing and was reported unsupported -- a loud false
    negative on an answer that was entirely grounded."""
    answer = (
        "This week:\n"
        "- 10:05-11:25 - Budget review, Larkspur Hall 4410\n"
        "- 1:00-2:20 - Design workshop, Larkspur Hall 2200\n"
        "- 7:00-9:00 - Fabrikam Design Kickoff\n"
    )
    claims = reflect._claims(answer)

    assert len(claims) == 3
    assert claims[0].startswith("10:05"), "the bullet survived into the claim"


def test_the_same_line_twice_is_one_claim():
    """Four days of the same lecture would otherwise spend the whole claim
    budget re-checking one fact."""
    answer = "- Budget review, Larkspur Hall 4410\n- Budget review, Larkspur Hall 4410"
    assert len(reflect._claims(answer)) == 1


async def test_a_truncated_payload_is_declared_rather_than_scored_as_invention(
    store, monkeypatch
):
    """Past the storage cap, "unsupported" stops meaning "invented" and
    starts meaning "further down a payload nobody kept". Those want opposite
    responses from the judge, so it has to be told which it is looking at."""
    seen = {}

    def fake_redact(text):
        seen["text"] = text
        from agent.redact import Redacted

        return Redacted(text=text)

    async def fake_judge(clean):
        return reflect.Verdict(
            answered=4, grounded=4, efficient=4, safe=4, clear=4,
            notes="", lesson="", proposal="",
        ), None

    monkeypatch.setattr(reflect, "redact", fake_redact)
    monkeypatch.setattr(reflect, "judge", fake_judge)
    monkeypatch.setattr("agent.loop.MAX_TOOL_RESULT_CHARS", 40)
    session_id = _session_with(
        store,
        answer="The deadline for the Northwind application is the fourth of November.",
        tool_output="x" * 60,
    )

    await reflect.reflect(store, session_id)

    assert "too long to keep in full" in seen["text"]


def test_a_heading_is_not_a_claim():
    """"This week, Monday through Sunday:" introduces the claims rather than
    making one, and no calendar payload can support it."""
    answer = "This week, Monday through Sunday:\n- Budget review, Larkspur Hall 4410"
    assert reflect._claims(answer) == ["Budget review, Larkspur Hall 4410"]


# --------------------------------------------------------------------------
# A conversation the user keeps coming back to (6 October 2026: 118 of their Signal
# messages sat in 3 sessions, each graded once)
# --------------------------------------------------------------------------


def _said(conn, session_id, role, content, minutes_ago):
    with conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at)"
            " VALUES (?, ?, ?, datetime('now', ?))",
            (session_id, role, content, f"-{minutes_ago} minutes"))


def _earlier(conn, session_id, minutes_ago):
    with conn:
        conn.execute("UPDATE messages SET created_at = datetime('now', ?) WHERE session_id = ?",
                     (f"-{minutes_ago} minutes", session_id))


@needs_reflect
async def test_a_session_the_user_came_back_to_is_graded_again_on_what_is_new(store, spy_judge):
    session_id = _session(store, [("user", "Remind me to call the bank tomorrow."),
                                  ("assistant", "Reminder set.")])
    _earlier(store, session_id, 300)
    await reflect.reflect(store, session_id)
    assert session_id not in reflect.unreflected(store)

    _said(store, session_id, "user", "What is on tomorrow morning?", 120)
    _said(store, session_id, "assistant", "Accounting at 9, then a coffee chat.", 119)
    assert session_id in reflect.unreflected(store)

    await reflect.reflect(store, session_id)

    sent = spy_judge["prompt"]
    assert "What is on tomorrow morning?" in sent
    assert f"{reflect.EARLIER} user: Remind me to call the bank" in sent, "context, marked"
    assert sent.count("Remind me to call the bank") == 1, "not graded a second time"
    assert session_id not in reflect.unreflected(store)
    count = store.execute("SELECT COUNT(*) FROM reflections WHERE session_id = ?",
                          (session_id,)).fetchone()[0]
    assert count == 2


@needs_reflect
async def test_a_conversation_still_going_on_is_not_graded_mid_way(store, spy_judge):
    session_id = _session(store, [("user", "Remind me to call the bank."),
                                  ("assistant", "Reminder set.")])
    _earlier(store, session_id, 300)
    await reflect.reflect(store, session_id)

    _said(store, session_id, "user", "And the dentist?", 5)

    assert session_id not in reflect.unreflected(store)


@needs_reflect
async def test_a_message_sent_while_the_judge_works_waits_for_the_next_part(store, spy_judge):
    session_id = _session(store, [("user", "Remind me to call the bank."),
                                  ("assistant", "Reminder set.")])
    _earlier(store, session_id, 300)
    graded_through = store.execute("SELECT MAX(created_at) FROM messages WHERE session_id = ?",
                                   (session_id,)).fetchone()[0]

    import agent.llm as llm

    judged = llm.run_agent

    async def slow_judge(*args, **kwargs):
        _said(store, session_id, "user", "Also the dentist.", 0)
        return await judged(*args, **kwargs)

    llm.run_agent = slow_judge
    try:
        await reflect.reflect(store, session_id)
    finally:
        llm.run_agent = judged

    marked = store.execute("SELECT reflected_at FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()[0]
    assert marked == graded_through, "the new message was marked graded unread"


@needs_reflect
async def test_a_run_of_their_agents_own_pings_is_not_graded(store, spy_judge):
    session_id = _session(store, [("user", "Remind me to call the bank."),
                                  ("assistant", "Reminder set.")])
    _earlier(store, session_id, 300)
    await reflect.reflect(store, session_id)
    spy_judge.clear()

    _said(store, session_id, "assistant", "Rain from 3pm today.", 120)
    _said(store, session_id, "assistant", "Your 4pm moved to Zoom.", 100)
    result = await reflect.reflect(store, session_id)

    assert result is None and "prompt" not in spy_judge
    assert session_id not in reflect.unreflected(store)


def test_redactions_email_check_reads_the_bundled_suffix_list_and_fetches_nothing(monkeypatch):
    """presidio checks each email it finds with `tldextract.extract`, whose
    extractor fetched the public-suffix list from publicsuffix.org on first
    use. CI's first run with the reflect extra (22 October 2026) failed every
    email redaction at the test fences; outside them, each reflection would
    have told a third party it was running. The redactor points that
    extractor at the bundled list before presidio loads."""
    import tldextract
    import tldextract.tldextract as tld

    from agent import redact

    monkeypatch.setattr(tld, "TLD_EXTRACTOR", tld.TLD_EXTRACTOR)
    redact._offline_suffixes()

    assert tld.TLD_EXTRACTOR.suffix_list_urls == ()
    found = tldextract.extract("mail.example.co.uk")
    assert (found.subdomain, found.domain, found.suffix) == ("mail", "example", "co.uk")
