"""A card shows whole what their yes agrees to, and never quotes a message body.

Until 4 October 2026 every card was cut at 300 characters, to keep email
text off the remote channel it crosses. A length did that badly both ways:
the first 180 characters of a Gmail email still went out over it, and every card
the user had to read whole -- a call's script, a routine's recipients, an event's
guests, a form's values, code that would run -- lost its end. Asked about
it, the user: "isn't there a smarter way to stop email text from showing up
than an arbitrary limit?"

So the rule is about what a card says, not its length
(agent/approvals.py, "What a card may say"). Frozen here, so a merged
change cannot quietly bring either failure back:

- every email the user is asked to send waits whole in their Drafts -- Gmail's as
  well as Contoso's -- where the user reads and can edit it, and its card names it
  and quotes none of it;
- a card that would quote the body of the message it sends is never asked;
- a card is never cut, however long what it binds them to.
"""

from __future__ import annotations

import pytest

import agent.toolset  # noqa: F401 - registers every executor before the fakes replace some
from agent import approvals, runtime
from agent.turn import Deps
from core import db
from steve_sdk.classes import Message
from tests import persona
from tests.gate import slotfake
from tests.gate.mailfake import Mailbox, connect

BODY = ("Hi Ann,\n\nThe quarterly numbers came in at 4.2 million, ahead of the plan we "
        "agreed in March.\nI would rather you heard it from me before the board meeting.\n\n"
        "Maya")


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


@pytest.fixture
def store(tmp_path, monkeypatch):
    conn = db.open_store(tmp_path / "cards.db")
    monkeypatch.setattr(runtime, "conn", conn)
    cards: list[str] = []

    async def notify(text, **_):
        cards.append(text)
        return True

    monkeypatch.setattr(approvals, "_notify", notify)
    yield conn, cards
    conn.close()


ANNS = Message(native_id="18c2f4a9b7d1e3f5", thread="18c2f4a9b7d1e3f5",
               sender="Ann Lee <ann@example.org>", to=persona.EMAIL, subject="Q3",
               body="How did the quarter go?")


@pytest.fixture
def mail(monkeypatch):
    """Three accounts on the slot, each a mailbox in a dict
    (tests/gate/mailfake.py), driven by the real tools and write path: what
    waits in their Drafts and what was sent is read off them."""
    boxes = {"gmail": Mailbox(persona.EMAIL, ANNS), "outlook": Mailbox(persona.WORK_EMAIL),
             "home": Mailbox(persona.OWN_DOMAIN_EMAIL)}
    return boxes, connect(monkeypatch, **boxes)


def _waiting(boxes) -> list[Message]:
    return [m for box in boxes.values() for m in box.drafts()]


def _sent(boxes) -> list[Message]:
    return [m for box in boxes.values() for m in box.sent]


def _ctx(conn) -> _Ctx:
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
    return _Ctx(Deps(conn=conn, session_id=int(session), channel="signal"))


_TOOLS: dict = {}


def _tool(name):
    if not _TOOLS:
        from pydantic_ai import Agent

        from agent.toolset import register_tools

        agent = Agent("test", deps_type=Deps)
        register_tools(agent)
        _TOOLS.update({n: t.function for n, t in agent._function_toolset.tools.items()})
    return _TOOLS[name]


def _pending(conn):
    return conn.execute("SELECT tool, preview FROM actions WHERE status = 'pending'").fetchall()


@pytest.mark.parametrize("how", [{}, {"reply": True},
                                 {"account": "outlook"}, {"account": "home"}],
                         ids=["gmail", "gmail-reply", "outlook", "home"])
async def test_every_email_the_user_is_asked_to_send_waits_whole_in_their_drafts(store, mail, how):
    conn, cards = store
    boxes, accounts = mail
    if how.pop("reply", False):
        how["reply_to_message_id"] = accounts["gmail"].id(ANNS.native_id)

    await _tool("send_email")(_ctx(conn), to="ann@example.org", subject="Q3", body=BODY, **how)

    where = boxes[how.get("account", "gmail")]
    assert [d.body for d in where.drafts()] == [BODY], "the whole email, where she reads it"
    assert [d.body for d in _waiting(boxes)] == [BODY] and _sent(boxes) == [], (
        "nothing goes before her yes")
    [card] = _pending(conn)
    assert card["tool"] == "send_draft"
    assert "ann@example.org" in card["preview"] and "Q3" in card["preview"]
    assert "Drafts" in card["preview"]
    lines = [line for line in BODY.splitlines() if len(line) >= 20]
    assert not any(line in card["preview"] for line in lines), "no line of it on the card"
    assert not any(line in text for line in lines for text in cards)


async def test_a_note_to_themselves_is_sent_as_it_was(store, mail):
    """Nothing asks them, so nothing waits in Drafts."""
    conn, _ = store

    await _tool("send_email")(_ctx(conn), to=persona.EMAIL, subject="Note", body=BODY)

    boxes, _ = mail
    assert len(_sent(boxes)) == 1 and _waiting(boxes) == [] and _pending(conn) == []


async def test_a_card_that_would_quote_the_body_it_sends_is_never_asked(store):
    conn, cards = store
    from core.consent import classify

    said = await approvals.request(
        conn, tool="send_email", args={"to": "ann@example.org", "body": BODY},
        preview=f"Email to Ann\n\n{BODY}",
        consent=classify("send_email", {"to": "x@y.example.org"}))

    assert said["status"] == "refused"
    assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0 and cards == []


async def test_a_card_is_never_cut_however_long_what_it_binds_them_to(store, monkeypatch):
    conn, _ = store
    # A calendar that can invite: `create_event` looks one up before its
    # card since it refuses guests a calendar cannot invite (beaf449).
    slotfake.plug(monkeypatch, "calendar", home=slotfake.Calendar())
    people = [f"someone.with.a.long.name{i:02d}@long-department.example.org" for i in range(20)]
    code = "\n".join(f"print({i})" for i in range(100))

    await _tool("create_routine")(_ctx(conn), title="Weekly note", instruction="Email them.",
                                  at_time="19:00", email_to=people[:10], invite=people[10:])
    await _tool("create_event")(_ctx(conn), summary="Dinner", start="2026-10-10T19:00",
                                attendees=people)
    await _tool("run_code")(_ctx(conn), code=code, network=True)

    cards = {row["tool"]: row["preview"] for row in _pending(conn)}
    assert all(p in cards["approve_routine"] for p in people)
    assert all(p in cards["create_event"] for p in people)
    assert code in cards["run_code"]
