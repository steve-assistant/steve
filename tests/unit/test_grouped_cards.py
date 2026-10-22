"""A turn's cards for one tool and account are one card, and a yes binds what it showed.

Step 5 makes a delete or an update on an account that cannot write on
condition a card every time (`core/consent.yaml`, `unconditional`). Cleaning
up five drafts there would be five messages on the user's phone, each
answered on its own; so the cards one turn makes for the same tool and
account are held until the turn ends and asked as one, approved whole or in
part (`agent/approvals.held`).

A card keeps the digest of what it showed, and the write reads the item
again and compares (`core/writes.py`). A draft whose Cc changed after the
card is not sent: the yes was to the draft as it was. The item is asked
about again as it is now, and the others on its card still go.

Driven through the real tools, approvals and write path, against two
accounts that are mailboxes in a dict (tests/gate/mailfake.py).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import actions, outbox, runtime
from agent import approvals as ap
from agent.toolset import mail
from agent.turn import Deps
from core import db
from steve_sdk import ids
from steve_sdk.classes import Message
from tests import persona
from tests.gate.mailfake import Mailbox, connect


def _draft(native: str, subject: str, to: str = "dana@example.com") -> Message:
    return Message(native_id=native, ident=f"<{native}@example.com>", to=to, subject=subject,
                   body="See you then.", direction="out", draft=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    conn = db.open_store(tmp_path / "cards.db")
    monkeypatch.setattr(runtime, "conn", conn)
    boxes = {"gmail": Mailbox(persona.EMAIL, _draft("d1", "Saturday"), _draft("d2", "Sunday")),
             "outlook": Mailbox(persona.WORK_EMAIL, _draft("d3", "Monday"))}
    accounts = connect(monkeypatch, **boxes)
    shown: list[str] = []

    async def notify(text, *, action_id=None, **_):
        shown.append(text)
        # Sent to the phone as the outbox sends a card (`links.send_due`): a
        # bare yes there is about the card that is the last thing in its chat.
        message = outbox.add(conn, text, kind="card", remote="signal", action_id=action_id)
        outbox.sent(conn, message)
        with conn:
            conn.execute("INSERT INTO messages (session_id, role, content)"
                         " VALUES (?, 'assistant', ?)", (session, text))
        return True

    monkeypatch.setattr(ap, "_notify", notify)
    monkeypatch.setattr(ap, "_execute_tool", actions.perform)
    monkeypatch.setattr(ap, "HOLD_SECONDS", 0)
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
    ctx = SimpleNamespace(deps=Deps(conn=conn, session_id=int(session), channel="signal"))
    tools: dict = {}
    mail.register(lambda fn: tools.setdefault(fn.__name__, fn))

    def call(name: str, account: str, native: str, **args):
        return tools[name](ctx, draft_id=accounts[account].id(native), **args)

    yield SimpleNamespace(conn=conn, boxes=boxes, shown=shown, call=call)
    conn.close()


async def _clean_up(world, *drafts: tuple[str, str]) -> list[ap.Action]:
    """One turn deleting these drafts; the cards it left, once asked."""
    async with ap.held(world.conn):
        for account, native in drafts:
            await world.call("delete_draft", account, native)
        assert world.shown == [], "a card was shown before the turn ended"
    await ap.asked()
    return ap.pending(world.conn)


def _card_for(cards: list[ap.Action], subject: str) -> ap.Action:
    return next(c for c in cards if subject in c.preview)


async def test_one_turns_cards_for_the_same_tool_and_account_are_one_card_asked_as_it_ends(world):
    cards = await _clean_up(world, ("gmail", "d1"), ("gmail", "d2"), ("outlook", "d3"))

    assert len(cards) == 2 and len(world.shown) == 2, "one card per account, each asked once"
    gmail = _card_for(cards, "Saturday")
    assert [ids.parse(i["args"]["draft_id"]).native for i in ap.items(gmail)] == ["d1", "d2"]
    assert "Sunday" in gmail.preview
    assert "2 on one card" in gmail.preview
    assert "Monday" not in gmail.preview, "another account's draft is another card"
    assert all(len(box.drafts()) for box in world.boxes.values()), "nothing went before a yes"


async def test_a_yes_to_the_whole_card_deletes_each_draft_on_it(world):
    cards = await _clean_up(world, ("gmail", "d1"), ("gmail", "d2"))
    [card] = cards

    ap.decide(world.conn, card.id, approve=True, by="signal")
    said = await ap.execute(world.conn, card.id, hold=False)

    assert said["ok"] and said["result"]["done"] == 2
    assert world.boxes["gmail"].deleted == ["d1", "d2"]


async def test_a_yes_to_part_of_a_card_runs_that_part_and_names_what_was_left_out(world):
    [card] = await _clean_up(world, ("gmail", "d1"), ("gmail", "d2"))

    ap.decide(world.conn, card.id, approve=True, by="tray", only=[2])
    said = await ap.execute(world.conn, card.id, hold=False)

    assert said["ok"] and world.boxes["gmail"].deleted == ["d2"]
    row = world.conn.execute("SELECT error FROM actions WHERE id = ?", (card.id,)).fetchone()
    assert row["error"] == "left out item 1"


class _Asked:
    """The little of a Starlette request the route reads."""

    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


async def test_the_apps_yes_to_some_items_runs_only_those_and_its_task_names_only_them(world):
    """The card's text promised a yes to some of them in the app, and the
    app's route took only all or nothing. A number given as text or true,
    or none at all, is refused, not read as an item nobody ticked."""
    from agent.worker import routes

    [card] = await _clean_up(world, ("gmail", "d1"), ("gmail", "d2"))
    for wrong in ("2", [True], [], [1.5]):
        refused = await routes.action_decide(
            _Asked({"action_id": card.id, "approve": True, "only": wrong}))
        assert refused.status_code == 400, wrong
    assert ap.get(world.conn, card.id).status == "pending"

    answer = await routes.action_decide(
        _Asked({"action_id": card.id, "approve": True, "only": [2]}))

    assert answer.status_code == 200
    job = json.loads(answer.body)["job_id"]
    why = world.conn.execute("SELECT why FROM jobs WHERE id = ?", (job,)).fetchone()["why"]
    assert "Sunday" in why and "Saturday" not in why
    said = await ap.execute(world.conn, card.id, hold=False)
    assert said["ok"] and world.boxes["gmail"].deleted == ["d2"]


def test_a_high_risk_card_is_approved_whole_or_not_at_all():
    """Its second factor was bound to everything on the card."""
    group = [{"args": {"amount": 5}, "preview": "Pay 5"},
             {"args": {"amount": 7}, "preview": "Pay 7"}]
    card = ap.Action(id=1, tool="purchase", args={ap.GROUPED: group}, preview="", status="pending",
                     token=None, reach="outward", reversal="irreversible", reasons=[],
                     spend_usd=12.0, expires_at=None)

    assert ap._part(card, [1, 2]) == {"left_out": ""}
    assert "whole" in ap._part(card, [1])


async def test_a_draft_changed_since_its_card_is_skipped_named_and_asked_about_again(world):
    [card] = await _clean_up(world, ("gmail", "d1"), ("gmail", "d2"))
    world.boxes["gmail"].edit("d1", cc="eve@example.com")

    ap.decide(world.conn, card.id, approve=True, by="signal")
    said = await ap.execute(world.conn, card.id, hold=False)
    await ap.asked()

    assert world.boxes["gmail"].deleted == ["d2"], "the unchanged one still went"
    [skipped] = said["result"]["skipped"]
    assert "Saturday" in skipped["item"] and "changed" in skipped["why"]
    [again] = ap.pending(world.conn)
    assert again.preview.startswith("Asked again: it changed after you said yes.")
    assert "Saturday" in again.preview and world.shown[-1] == ap.card(again)

    ap.decide(world.conn, again.id, approve=True, by="signal")
    await ap.execute(world.conn, again.id, hold=False)
    assert world.boxes["gmail"].deleted == ["d2", "d1"], "asked again, it is the draft as it is now"


async def test_a_draft_whose_cc_changed_after_its_card_is_not_sent(world):
    """Step 5's exit criterion: a Cc added after the yes must not ride along."""
    async with ap.held(world.conn):
        await world.call("send_draft", "gmail", "d1")
    [card] = ap.pending(world.conn)
    world.boxes["gmail"].edit("d1", cc="eve@example.com")

    ap.decide(world.conn, card.id, approve=True, by="signal")
    said = await ap.execute(world.conn, card.id, hold=False)
    await ap.asked()

    assert said["ok"] is False and world.boxes["gmail"].sent == []
    [again] = ap.pending(world.conn)
    assert again.tool == "send_draft" and "eve@example.com" in again.preview


async def test_a_card_its_turn_still_holds_is_no_note_and_a_yes_to_it_as_first_read_is_refused(
        world):
    """The app read every card pending every 5 s, a card of a turn still
    running among them (`held`), and the turn could then join an item to it:
    a yes to the one item read took both. A card its turn still holds is no
    note and takes no yes; a yes names the card as it was read, and one read
    before the card grew is refused."""
    from agent.worker import needs, routes

    async with ap.held(world.conn):
        await world.call("delete_draft", "gmail", "d1")
        [card] = ap.pending(world.conn)
        assert needs._cards(world.conn) == []
        mid_turn = await routes.action_decide(_Asked({"action_id": card.id, "approve": True}))
        assert mid_turn.status_code == 400
        read = card.args_hash
        await world.call("delete_draft", "gmail", "d2")
    await ap.asked()

    [note] = needs._cards(world.conn)
    assert len(note["items"]) == 2 and note["seen"] != read
    for wrong in (5, ["x"]):
        refused = await routes.action_decide(
            _Asked({"action_id": card.id, "approve": True, "seen": wrong}))
        assert refused.status_code == 400, wrong
    stale = await routes.action_decide(
        _Asked({"action_id": card.id, "approve": True, "seen": read}))
    assert stale.status_code == 400
    assert "changed since you read it" in json.loads(stale.body)["error"]
    assert not ap.decide(world.conn, card.id, approve=True, by="tray", seen=read)["ok"]
    assert ap.get(world.conn, card.id).status == "pending"

    answer = await routes.action_decide(
        _Asked({"action_id": card.id, "approve": True, "seen": note["seen"], "only": [1]}))
    assert answer.status_code == 200
    said = await ap.execute(world.conn, card.id, hold=False)
    assert said["ok"] and world.boxes["gmail"].deleted == ["d1"]


async def test_a_card_its_turn_still_holds_takes_no_yes_from_a_phone(world, monkeypatch):
    """The phone has not seen it, and the turn may yet join items to it, so
    a bare "yes" there is about something else. It decided the card all the
    same when it was the only one pending (step 11's audit)."""
    from agent.worker import answering

    decided: list[int] = []

    async def decide(action_id, *, approve, by, **_):
        decided.append(action_id)
        return {"ok": True}

    monkeypatch.setattr(answering, "decide_action", decide)
    async with ap.held(world.conn):
        await world.call("delete_draft", "gmail", "d1")
        [card] = ap.pending(world.conn)
        assert await answering.intercept_approval("yes", "signal") is None
        await answering.intercept_approval(f"YES {card.token}", "signal")
        assert decided == []
    await ap.asked()

    assert await answering.intercept_approval("yes", "signal") is not None
    assert decided == [card.id], "asked once its turn ended, a yes is about it"
