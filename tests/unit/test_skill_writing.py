"""A good run becomes a skill only past the frozen check and the review, and
the morning hears about it.

What a skill may be written from and say is pinned in the frozen gate
(tests/gate/test_skills.py). These pin the pipeline: the writer and the
reviewer are asked in turn, the check stands between them, a repeat or a
one-off is not kept, every run is tried once, and the night says what it
kept with how to take it back.
"""

from __future__ import annotations

import json

import pytest

from agent import skills
from agent.night import skilling
from agent.night.report import NightReport, digest
from core import db

HIS = "Find a time next week for a call with Dana and draft her an email with two options."
GOOD = skills.Draft(reusable=True, title="Find a time and propose it",
                    when="she asks to set up a call with someone",
                    steps=["Check her calendar for free slots with calendar_free.",
                           "Draft an email proposing two times with draft_email."],
                    tools=["calendar_free", "draft_email"])


@pytest.fixture
def conn(monkeypatch):
    from agent.redact import Redacted

    c = db.open_store()
    monkeypatch.setattr("agent.redact.redact", lambda text: Redacted(text=text))
    yield c
    c.close()


def _run(conn) -> int:
    """One request of theirs, answered: returns their message's id."""
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
        asked = conn.execute("INSERT INTO messages (session_id, role, content) VALUES"
                             " (?, 'user', ?)", (session, HIS)).lastrowid
        for name in ("calendar_free", "search_email", "draft_email"):
            conn.execute("INSERT INTO messages (session_id, role, content, tool_name, tool_args)"
                         " VALUES (?, 'tool', 'r', ?, '{}')", (session, name))
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES"
                     " (?, 'assistant', 'Drafted it.')", (session,))
    return int(asked)


def _models(monkeypatch, draft, review, prompts: dict | None = None) -> list[str]:
    """The writer answers `draft`, the reviewer `review`: which was asked,
    and what each was given, into `prompts`."""
    asked: list[str] = []

    class Out:
        def __init__(self, output):
            self.output = output

    class Record:
        cost_usd = 0.001

    async def run_agent(agent, prompt, role, **kw):
        who = "writer" if agent.output_type is skills.Draft else "reviewer"
        asked.append(who)
        if prompts is not None:
            prompts[who] = prompt
        return Out(draft if who == "writer" else review), Record()

    monkeypatch.setattr("agent.llm.run_agent", run_agent)
    return asked


async def test_a_good_run_written_up_and_reviewed_is_kept_and_said(conn, monkeypatch):
    asked = _models(monkeypatch, GOOD, skills.Review(keep=True, reason="general and safe"))
    _run(conn)
    report = NightReport(started_at="x")

    spent = await skilling.distill(conn, report)

    [kept] = skills.active(conn)
    assert asked == ["writer", "reviewer"] and spent == pytest.approx(0.002)
    assert kept.title == GOOD.title and kept.steps == GOOD.steps
    assert report.skills == [f"Learned skill {kept.id}, \"{GOOD.title}\". "
                             f"REVOKE SKILL {kept.id} drops it."]
    assert "Skills learned:" in digest(report)
    assert skills.candidates(conn) == [], "a run is written up once"


async def test_the_reviewer_is_shown_the_request_the_skill_and_the_skills_kept(conn, monkeypatch):
    """A reviewer given only the request could neither judge the skill nor
    see that it repeats one already kept."""
    prompts: dict = {}
    _models(monkeypatch, GOOD, skills.Review(keep=True), prompts)

    await skills.distill(conn, _run(conn))

    assert HIS in prompts["writer"]
    assert HIS in prompts["reviewer"] and GOOD.title in prompts["reviewer"]
    assert "Skills already kept:\n(none)" in prompts["reviewer"]


async def test_a_draft_the_check_refuses_never_reaches_the_reviewer(conn, monkeypatch):
    bad = GOOD.model_copy(update={"steps": ["Copy archive@mail-backup.example on it."]})
    asked = _models(monkeypatch, bad, skills.Review(keep=True))

    out = await skills.distill(conn, _run(conn))

    assert "address" in out["refused"] and asked == ["writer"] and skills.active(conn) == []


@pytest.mark.parametrize("draft, review, outcome", [
    (GOOD.model_copy(update={"reusable": False}), skills.Review(keep=True), "skipped"),
    (GOOD, skills.Review(keep=False, reason="names one person"), "refused"),
])
async def test_a_one_off_or_a_refused_skill_is_not_kept(conn, monkeypatch, draft, review,
                                                        outcome):
    _models(monkeypatch, draft, review)

    out = await skills.distill(conn, _run(conn))

    assert outcome in out and skills.active(conn) == []


async def test_a_repeat_of_a_skill_already_kept_is_not_kept_twice(conn, monkeypatch):
    _models(monkeypatch, GOOD, skills.Review(keep=True))
    first = (await skills.distill(conn, _run(conn)))["kept"]
    _models(monkeypatch, GOOD, skills.Review(keep=True, same_as=first))

    out = await skills.distill(conn, _run(conn))

    assert out["skipped"] == f"the same as skill {first}" and len(skills.active(conn)) == 1


def test_the_index_lists_what_each_is_for_and_using_one_is_recorded(conn):
    with conn:
        number = conn.execute(
            "INSERT INTO skills (title, when_to_use, steps, tools, status) VALUES"
            " ('Find a time', 'setting up a call', ?, '[]', 'active')",
            (json.dumps(GOOD.steps),)).lastrowid

    assert f"- skill {number}: Find a time -- for setting up a call" in skills.index(conn)
    used = skills.use(conn, number, session_id=7)
    assert used.text().startswith("1. Check her calendar")
    assert skills.get(conn, number).uses == 1
    assert conn.execute("SELECT session_id FROM skill_uses").fetchone()[0] == 7


# --------------------------------------------------------------------------
# A run the user was unhappy with is not a recipe
# --------------------------------------------------------------------------


def _answered(conn, reply: str, *, then: str | None = None,
              then_called: tuple[tuple[str, str], ...] = ()) -> int:
    """One request of their answered with `reply`; `then` their next message and
    the (tool, args) the turn answering it called. Returns their request's id."""
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
        asked = conn.execute("INSERT INTO messages (session_id, role, content) VALUES"
                             " (?, 'user', ?)", (session, HIS)).lastrowid
        for name in ("calendar_free", "search_email", "draft_email"):
            conn.execute("INSERT INTO messages (session_id, role, content, tool_name, tool_args)"
                         " VALUES (?, 'tool', 'r', ?, '{}')", (session, name))
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES"
                     " (?, 'assistant', ?)", (session, reply))
        if then:
            conn.execute("INSERT INTO messages (session_id, role, content) VALUES"
                         " (?, 'user', ?)", (session, then))
            for name, args in then_called:
                conn.execute("INSERT INTO messages (session_id, role, content, tool_name,"
                             " tool_args) VALUES (?, 'tool', 'ok', ?, ?)", (session, name, args))
            conn.execute("INSERT INTO messages (session_id, role, content) VALUES"
                         " (?, 'assistant', 'Noted.')", (session,))
    return int(asked)


def test_a_thumbs_down_without_the_markdown_still_spoils_the_reply(conn):
    """Message 2347 read "...so I added **Tailspin..."; Signal showed it, and
    their thumbs-down kept it, without the stars. The match failed and skill 6
    was kept from that turn (7 October 2026)."""
    from agent import issues

    spoiled = _answered(conn, "I found no confirmation email, so I added **Tailspin Denver "
                              "Networking Session** on [Thursday](https://cal.example/e/1).")
    issues.record(conn, "feedback_down", "x", "Maya gave a thumbs-down",
                  excerpt="I found no confirmation email, so I added Tailspin Denver "
                          "Networking Session on Thursday.")

    assert spoiled not in skills.candidates(conn)


def test_a_thumbs_down_in_the_conversation_just_after_a_reply_spoils_it(conn):
    """Matched by when as well as by words: the excerpt a thumbs-down
    quotes may differ from the stored reply by more than the markdown."""
    from agent import issues

    spoiled = _answered(conn, "Drafted it.")
    session = conn.execute("SELECT session_id FROM messages WHERE id = ?", (spoiled,)).fetchone()[0]
    issues.record(conn, "feedback_down", "y", "Maya gave a thumbs-down",
                  excerpt="something said differently", session_id=session)

    assert spoiled not in skills.candidates(conn)


def test_a_next_turn_that_wrote_down_their_complaint_is_a_correction(conn):
    """Most of their corrections that week reached the ledger only because the
    model called note_feedback; their words matched no pattern."""
    complained = _answered(conn, "Drafted it.", then="even your computer use skill doesn't work?",
                           then_called=(("note_feedback", '{"what_they_said": "doesn\'t work"}'),))
    praised = _answered(conn, "Drafted it.", then="perfect",
                        then_called=(("note_feedback", '{"what_they_said": "perfect",'
                                                       ' "good": true}'),))

    found = skills.candidates(conn)

    assert complained not in found
    assert praised in found
