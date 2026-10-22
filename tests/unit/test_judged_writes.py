"""Which turns put an unconditional write to Jev, and what Jev is told
(agent/actions.py, `_judged`; core/lostupdate.py).

Jev may judge away a card asked only because the account cannot write on
condition. That is a card fewer, so it is offered only where the user is in
the conversation: a worker, a routine, a goal or an unattended session acts
on what it read, which nobody vetted, and keeps the card.
"""

from __future__ import annotations

import json

import pytest

from agent import actions
from agent.turn import Deps
from core import db
from core.consent import classify
from core.monitor import Gate
from steve_sdk import ids
from tests.gate.decisions import install

DRAFT = ids.make("mail", "0123456789ab", "Drafts\n7\n3\n<d@x>")
CARD = "Delete a draft in your home Drafts\nTo: dana@example.com\nSubject: Saturday"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("core.secrets.get", lambda name: "test-key")
    conn = db.open_store(tmp_path / "judged.db")
    yield conn
    conn.close()


def _session(conn, channel: str) -> int:
    with conn:
        return int(conn.execute("INSERT INTO sessions (channel) VALUES (?)",
                                (channel,)).lastrowid)


def _card() -> Gate:
    consent = classify("delete_draft", {"draft_id": DRAFT}, conditional=frozenset())
    return Gate("card", consent)


class _Ctx:
    def __init__(self, deps: Deps) -> None:
        self.deps = deps


async def test_in_the_users_own_conversation_jev_may_judge_the_card_away(store, monkeypatch):
    install(monkeypatch, lost_update="go")
    ctx = _Ctx(Deps(conn=store, session_id=_session(store, "signal"), channel="signal"))

    decided = await actions._judged(ctx, "delete_draft", {"draft_id": DRAFT}, _card(), CARD)

    assert decided.kind == "free" and decided.waived


@pytest.mark.parametrize("who", ["worker", "routine", "goal", "unattended"])
async def test_a_worker_routine_goal_or_unattended_session_keeps_the_card_and_jev_is_not_asked(
        store, monkeypatch, who):
    """Each acts on what it read; a judgement in its favour would be a card
    fewer on text nobody vetted."""
    jev = install(monkeypatch, lost_update="go")
    channel = "mailbox" if who == "unattended" else "signal"
    deps = Deps(conn=store, session_id=_session(store, channel), channel=channel)
    if who == "worker":
        deps.worker = True
    elif who == "routine":
        deps.routine_id = 1
    elif who == "goal":
        deps.goal_id = 1
    card = _card()

    decided = await actions._judged(_Ctx(deps), "delete_draft", {"draft_id": DRAFT}, card, CARD)

    assert decided is card and jev.asked == []


async def test_jev_is_told_when_steve_made_the_item_earlier_in_this_conversation(
        store, monkeypatch):
    """A draft Steve saved a minute ago, in this conversation, is the case
    the card was least needed for; another conversation's is not this one's."""
    jev = install(monkeypatch, lost_update="ask")
    here = _session(store, "signal")
    elsewhere = _session(store, "signal")
    with store:
        store.execute("INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal,"
                      " status, result_json, session_id) VALUES ('draft_email', '{}', 'h', 'p',"
                      " 'private', 'reversible', 'done', ?, ?)",
                      (json.dumps({"draft_id": DRAFT}), here))

    for session in (here, elsewhere):
        ctx = _Ctx(Deps(conn=store, session_id=session, channel="signal"))
        await actions._judged(ctx, "delete_draft", {"draft_id": DRAFT}, _card(), CARD)

    made, not_made = jev.inputs
    assert "made or changed this item itself" in made
    assert "has not made or changed this item" in not_made


async def test_a_draft_jev_judges_safe_is_deleted_at_once_through_the_real_tool_and_write_path(
        store, monkeypatch):
    """The verdict travels from the monitor's decision to core's write; one
    dropped on the way leaves the write refusing what Jev allowed."""
    from agent import approvals as ap
    from agent import runtime
    from agent.toolset import mail
    from steve_sdk.classes import Message
    from tests import persona
    from tests.gate.mailfake import Mailbox, connect

    install(monkeypatch, lost_update="go")
    monkeypatch.setattr(runtime, "conn", store)
    box = Mailbox(persona.EMAIL, Message(native_id="d1", ident="<d1@example.com>",
                                         to="dana@example.com", subject="Saturday",
                                         body="See you then.", direction="out", draft=True))
    [account] = connect(monkeypatch, gmail=box).values()
    ctx = _Ctx(Deps(conn=store, session_id=_session(store, "signal"), channel="signal"))
    tools: dict = {}
    mail.register(lambda fn: tools.setdefault(fn.__name__, fn))

    said = await tools["delete_draft"](ctx, draft_id=account.id("d1"))
    await ap.asked()

    assert said.get("deleted") is True, said
    assert box.drafts() == [] and ap.pending(store) == []
