"""Threads left hanging: a reply the user owes, and one owed to them.

The failure worth fearing is the nag -- "you haven't replied to Rina" about
the reply the user sent this morning, or about the newsletter nobody answers --
because a notifier that nags is muted. So these pin the windows, who counts
as a person, and that a thread is mentioned once per state.
"""

from __future__ import annotations

import datetime as dt

import pytest

from agent.attention.sources import threads
from agent.attention.sources.threads import Message
from agent.connectors import Connected
from steve_sdk.classes import Message as MailMessage
from tests import persona
from tests.gate import mailfake

NOW = dt.datetime(2026, 9, 24, 12, 0)
ME = frozenset({persona.WORK_EMAIL, persona.GMAIL})
WORK = Connected(key="0123456789ab", name="work", klass="mail", module="tests",
                 provider=mailfake.Mailbox(persona.WORK_EMAIL), address=persona.WORK_EMAIL)


def _m(thread, direction, days_ago, who=("rina@contoso.example", "Rina Sato"), *, id=None,
       subject="DES 203 enrollment"):
    return Message(
        account=WORK, id=WORK.id(id or f"{thread}-{direction}-{days_ago}"),
        thread=WORK.id(thread), direction=direction, at=NOW - dt.timedelta(days=days_ago),
        subject=subject, counterparts=[who],
    )


def _keys(conn, messages):
    return [o for o in threads.dropped(conn, messages, NOW, ME)]


def test_a_reply_the_user_owes_is_noticed_after_two_days(conn):
    [found] = _keys(conn, [_m("t1", "received", 3)])

    assert found.title.startswith("Rina Sato wrote 3 days ago and you haven't replied")
    assert found.ref["waiting"] == "owed by the user"
    assert found.ref["with"] == "Rina Sato <rina@contoso.example>"


def test_a_reply_owed_to_them_is_noticed_after_four(conn):
    [found] = _keys(conn, [_m("t1", "sent", 5, ("jane@northwind.example", "Jane Doe"))])

    assert found.title.startswith("No reply from Jane Doe in 5 days")
    assert found.ref["waiting"] == "owed to the user"


@pytest.mark.parametrize(("direction", "days"), [
    ("received", 1), ("received", 9), ("sent", 3), ("sent", 12),
])
def test_too_soon_or_gone_cold_is_left_alone(conn, direction, days):
    """A day is not a dropped thread; two weeks is not worth a nudge."""
    assert _keys(conn, [_m("t1", direction, days)]) == []


def test_a_reply_the_user_sent_ends_it(conn):
    """The nag about the reply the user sent this morning."""
    messages = [_m("t1", "received", 4), _m("t1", "sent", 0)]

    assert _keys(conn, messages) == []


def test_an_answer_to_them_ends_it(conn):
    messages = [_m("t1", "sent", 6, ("jane@northwind.example", "Jane")), _m("t1", "received", 1,
                                                                  ("jane@northwind.example",
                                                                   "Jane"))]

    assert _keys(conn, messages) == []


def test_a_service_or_a_mailing_list_is_not_owed_anything(conn):
    with conn:
        conn.execute(
            "INSERT INTO people (email, name, is_person) "
            "VALUES ('digest@club.example.org', 'Club', 0)"
        )
    messages = [
        _m("t1", "received", 3, ("no-reply@coursework.example", "Canvas")),
        _m("t2", "received", 3, ("digest@club.example.org", "Club")),
        _m("t3", "received", 3, ("maya@contoso.example", "Maya Lindqvist")),
    ]

    assert _keys(conn, messages) == []


def test_each_state_of_a_thread_is_one_key(conn):
    """The same silence, seen every six hours, is one observation; a new
    message in the thread is a new one."""
    first = _keys(conn, [_m("t1", "received", 3, id="a")])
    again = _keys(conn, [_m("t1", "received", 3, id="a")])
    newer = _keys(conn, [_m("t1", "received", 3, id="a"),
                         _m("t1", "received", 2, id="b")])

    assert first[0].key == again[0].key
    assert newer[0].key != first[0].key
    assert first[0].key.startswith(f"thread:mail:{WORK.key}:"), \
        "names its account, so one switched off is skipped"


def test_an_accounts_mail_is_read_by_slot_id_so_a_reply_finds_its_account(conn, monkeypatch):
    sent_at = int((dt.datetime.now() - dt.timedelta(days=3)).timestamp() * 1000)
    accounts = mailfake.connect(monkeypatch, work=mailfake.Mailbox(
        persona.WORK_EMAIL, MailMessage("RAW-ENTRY-ID", thread="conv-1", subject="DES 203",
                                        sender="Rina Sato <rina@contoso.example>",
                                        to=persona.WORK_EMAIL, when=sent_at)))

    [message] = threads._read(conn)

    assert message.id == accounts["work"].id("RAW-ENTRY-ID")
    assert message.thread == accounts["work"].id("conv-1") and message.account.name == "work"
    # Recorded in the ledger too, once however often it is read.
    threads._read(conn)
    count = conn.execute("SELECT recv_count FROM people WHERE email = 'rina@contoso.example'")
    assert count.fetchone()[0] == 1


def test_a_reply_the_user_owes_is_asked_about_never_drafted_unasked(conn):
    """The user's rule: a reply the user owes could need something only the user knows, so
    the judge's draft task is taken off it in code and the user is asked instead.
    A nudge on their own unanswered email needs nothing and keeps its task."""
    from agent.attention import store, triage

    owed_by_user, owed_to_user = threads.dropped(conn, [
        _m("t1", "received", 3), _m("t2", "sent", 5, ("jane@northwind.example", "Jane Doe")),
    ], NOW, ME)
    batch = store.record_new(conn, [owed_by_user, owed_to_user])
    triage.apply(conn, batch, [
        triage.Verdict(key=o.key, say="now", headline="h", task="Draft a reply") for o in batch
    ])

    tasks = {r["key"]: r["task"] for r in conn.execute("SELECT key, task FROM observations")}
    assert tasks[owed_by_user.key] is None
    assert tasks[owed_to_user.key] == "Draft a reply"
