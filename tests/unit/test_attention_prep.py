"""Walking into a meeting prepared: who is in the room, and room to say it.

Two failures this pins. The brief that never arrived: the doer's words were
kept only if it had acted or searched the web, so a brief built from their
own mail, Drive and contact ledger -- the useful kind -- was thrown away as
"did nothing". And the brief built from nothing: without the guests handed
over, the doer had to think of looking each one up.
"""

from __future__ import annotations

import datetime as dt

from agent.attention import briefing, deliver, prepare, store, triage
from agent.attention.sources import calendar as cal_source
from agent.attention.store import Observation
from tools import commitments, people


def _event(**kw):
    return {"id": "e1", "summary": "Coffee chat", "start": "2026-09-25T11:00:00-04:00", **kw}


# --------------------------------------------------------------------------
# Who is in the room
# --------------------------------------------------------------------------


def test_the_people_in_a_meeting_are_carried_without_them_or_a_mailer():
    event = _event(
        attendees=[
            {"email": "maya@contoso.example", "self": True},
            {"email": "Jane@northwind.example", "name": "Jane Doe"},
            {"email": "noreply@zoom.us"},
            {"email": "jane@northwind.example"},
        ],
        organizer="rina@contoso.example",
    )

    assert cal_source.people_in(event) == ["Jane Doe <jane@northwind.example>",
                                           "rina@contoso.example"]


def test_an_event_the_user_organised_does_not_list_them():
    event = _event(attendees=[{"email": "jane@northwind.example"}],
                   organizer="steve.test.user@gmail.com",
                   organizer_self=True)

    assert cal_source.people_in(event) == ["jane@northwind.example"]


def test_a_lecture_that_invites_the_class_carries_only_a_few():
    event = _event(attendees=[{"email": f"student{i}@contoso.example"} for i in range(90)])

    assert len(cal_source.people_in(event)) == cal_source.MAX_PEOPLE


def test_the_horizon_hands_the_guests_on_with_the_event(conn, monkeypatch):
    from agent import calendars

    monkeypatch.setattr(calendars, "agenda", lambda conn, days, limit: {"events": [
        _event(attendees=[{"email": "jane@northwind.example", "name": "Jane Doe"}]),
    ]})

    [obs] = cal_source.horizon(conn)

    assert obs.ref["people"] == ["Jane Doe <jane@northwind.example>"]


# --------------------------------------------------------------------------
# What the doer is told about them
# --------------------------------------------------------------------------


def _jane(conn) -> int:
    person_id = people.upsert(conn, "jane@northwind.example", name="Jane Doe", sent=True)
    with conn:
        conn.execute("UPDATE people SET org = 'Northwind', note = 'Recruiter; met at the Contoso "
                     "fair'"
                     " WHERE id = ?", (person_id,))
    for day, direction, subject in [(18, "sent", "Thanks for the chat"),
                                    (19, "received", "Coffee next week?"),
                                    (20, "sent", "Re: Coffee next week?"),
                                    (10, "received", "Nice to meet you")]:
        people.record_interaction(
            conn, person_id, kind="email", direction=direction, source_id=f"m{day}",
            occurred_at=f"2026-09-{day}T10:00:00", subject=subject,
        )
    commitments.add(conn, text="Send Jane the resume", quote="I'll send my resume over",
                    person_id=person_id, due_date="2026-09-26")
    return person_id


def test_the_doer_is_told_what_their_records_say_about_each_guest(conn):
    _jane(conn)
    with conn:
        conn.execute(
            "INSERT INTO people (email, name, is_person) "
            "VALUES ('club@list.example.org', 'Club', 0)"
        )
    obs = Observation(source="horizon", key="horizon:e1:soon", title="Coffee chat",
                      ref={"event_id": "e1", "people": [
                          "Jane Doe <jane@northwind.example>", "club@list.example.org",
                              "sam@tailspin.example"]},
                      task="Prepare a brief")

    text = prepare.describe(obs, conn)

    assert ("- Jane Doe <jane@northwind.example>: Northwind, Recruiter; met at the Contoso fair"
            in text)
    # The three latest exchanges, newest first; the fourth is left out.
    assert text.index("2026-09-20 sent email: Re: Coffee next week?") < text.index(
        "2026-09-18 sent email: Thanks for the chat")
    assert "Nice to meet you" not in text
    assert "the user owes them:" in text and "resume" in text
    assert "- sam@tailspin.example: not in the user's records" in text
    assert "club@list.example.org" not in text


def test_an_alias_finds_the_person_it_was_folded_into(conn):
    person_id = _jane(conn)
    with conn:
        conn.execute("INSERT INTO person_aliases (person_id, email) VALUES (?, "
                     "'jdoe@northwind.example')",
                     (person_id,))

    block = prepare.people_block(conn, ["jdoe@northwind.example"])

    assert block.startswith("- Jane Doe <jane@northwind.example>: Northwind")


# --------------------------------------------------------------------------
# Room for the brief
# --------------------------------------------------------------------------


BRIEF = ("Jane Doe, Northwind recruiter; you met at the Contoso fair.\n"
         "You owe her your resume (by Fri 26 Sep).\n"
         "\n"
         "Northwind announced a Boston expansion on Monday.\n"
         "A fourth line that should not survive.")


def test_a_meeting_brief_keeps_three_lines_and_what_it_looked_up_counts():
    """Built from their mail, Drive and ledger, a brief uses no tool that acts
    or searches the web. Before this, it was discarded as having done
    nothing."""
    kept = prepare.outcome_of(BRIEF, ["who_is", "search_drive"], brief=True)

    assert kept.splitlines() == BRIEF.replace("\n\n", "\n").splitlines()[:3]
    assert len(kept) <= prepare.BRIEF_CHARS


def test_anything_else_is_still_one_line_and_looking_is_not_doing():
    assert prepare.outcome_of(BRIEF, ["who_is", "search_drive"]) is None
    assert prepare.outcome_of(BRIEF, ["web_search"]) == BRIEF.splitlines()[0]


def test_the_brief_reaches_their_phone_as_one_item():
    obs = Observation(source="horizon", key="k", title="Coffee chat", say="before",
                      headline="Coffee chat with Jane Doe (Northwind)", when="2026-09-25T11:00:00",
                      outcome=prepare.outcome_of(BRIEF, ["who_is"], brief=True))

    text = deliver.line_for(obs, dt.datetime(2026, 9, 25, 10, 15))

    assert text.splitlines() == [
        "In 45 min: Coffee chat with Jane Doe (Northwind)",
        "  Jane Doe, Northwind recruiter; you met at the Contoso fair.",
        "  You owe her your resume (by Fri 26 Sep).",
        "  Northwind announced a Boston expansion on Monday.",
    ]


def test_a_brief_folded_into_the_morning_stays_under_its_meeting():
    lines = briefing._agenda(
        [{"id": "e1", "summary": "Coffee chat", "start": "2026-09-25T09:00:00"}],
        dt.date(2026, 9, 25), {"e1": "Jane Doe, Northwind recruiter.\nYou owe her your resume."},
    )

    assert lines[1:] == ["         prep: Jane Doe, Northwind recruiter.",
                         "               You owe her your resume."]


class _Convo:
    seen: list = []

    def __init__(self, conn, *, channel, mandate=None):
        pass

    async def send(self, text, *, allow=None):
        _Convo.seen.append(text)

        class Reply:
            text = BRIEF
            tools_used = ["who_is", "search_drive"]
            cost_usd = 0.001

        return Reply()

    def end(self):
        pass


async def test_a_meeting_brief_is_prepared_from_the_guests_and_kept(conn, monkeypatch):
    import agent.loop

    _Convo.seen = []
    monkeypatch.setattr(agent.loop, "Conversation", _Convo)
    monkeypatch.setattr("core.browser.background", lambda: False)  # the gate's
    _jane(conn)
    later = (dt.datetime.now() + dt.timedelta(hours=5)).isoformat(timespec="seconds")
    [obs] = store.record_new(conn, [Observation(
        source="horizon", key="horizon:e1:soon", title="Coffee chat", when=later,
        ref={"event_id": "e1", "people": ["Jane Doe <jane@northwind.example>"]},
    )])
    triage.apply(conn, [obs], [triage.Verdict(
        key=obs.key, say="before", headline="Coffee chat with Jane", task="Prepare a brief",
    )])

    await prepare.prepare(conn)

    assert ("People in it, from the user's records:\n- Jane Doe <jane@northwind.example>: Northwind"
            in _Convo.seen[0])
    kept = conn.execute("SELECT outcome FROM observations WHERE key = ?", (obs.key,)).fetchone()
    assert kept[0].count("\n") == 2
