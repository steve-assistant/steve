"""An approval is bound to the action the user was asked about, once, while it is
fresh -- and what the agent learns cannot change what needs one.

From the security model (docs/security-model.md, 1 October 2026). An outside
review asked for actions to be "canonically hashed and bound to approvals",
replay-proof, expiring, and immune to parameters changed after the fact. Most
of that already held; these pin it in the gate, where a change to
agent/approvals.py -- mergeable by a judge -- must still pass them.

What runs is the row the laptop wrote when it asked: the arguments are never
in their reply or on the wire, so nothing between their phone and the laptop
can change them.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from datetime import datetime, timedelta

import pytest

from agent import approvals
from core import consent, db
from core.consent import classify
from core.policy import load_policy

EMAIL = {"to": "hugo@example.com", "subject": "Thank you", "body": "It was good to meet."}


@pytest.fixture
def store(tmp_path, monkeypatch):
    conn = db.open_store(tmp_path / "approvals.db")
    monkeypatch.setattr(approvals, "_notify", None)  # no card goes anywhere
    yield conn
    conn.close()


async def _asked(conn, args=EMAIL) -> approvals.Action:
    receipt = await approvals.request(
        conn, tool="send_email", args=dict(args), preview="Email to Hugo",
        consent=classify("send_email", args), session_id=None, channel="signal")
    return approvals.get(conn, receipt["action_id"])


async def test_what_runs_is_what_the_user_was_asked_about_not_what_the_reply_says(store):
    """A reply carrying other arguments -- as a forged or garbled one might
    -- names the card and nothing else."""
    action = await _asked(store)
    said = approvals.parse_reply(f"YES {action.token} to attacker@example.com body: leak",
                                 approvals.pending(store))

    assert said is None or said.action_id in (action.id, None)
    if said is None or said.action_id is None:
        # Too long or too odd to be an answer, or asked about since it says
        # more than yes (step 11's second review): it reached nobody's yes.
        said = approvals.parse_reply(f"YES {action.token}", approvals.pending(store))
    approvals.decide(store, said.action_id, approve=True, by="signal")

    claimed = approvals.claim(store, action.id)
    assert claimed.args == EMAIL


async def test_a_card_is_decided_once_and_run_once(store):
    action = await _asked(store)

    first = approvals.decide(store, action.id, approve=True, by="signal")
    again = approvals.decide(store, action.id, approve=False, by="signal")
    taken = approvals.claim(store, action.id)

    assert first["ok"] and not again["ok"]
    assert taken is not None and approvals.claim(store, action.id) is None


async def test_a_yes_is_not_taken_back_once_its_call_has_begun(store, monkeypatch):
    """Taken back mid-call -- by UNDO, or by unlinking its chat -- an email
    that went was recorded undone: the mailbox keys on done, and could send
    it again, and money spent was not counted (step 11's third review)."""
    action = await _asked(store)
    approvals.decide(store, action.id, approve=True, by="signal")
    tried = {}

    async def executor(tool, args):
        tried["undo"] = approvals.cancel_during_hold(store, action.id)
        tried["unlink"] = approvals.forget_chat(store, "signal", "Signal was unlinked").undone
        return {"ok": True, "id": "sent"}

    monkeypatch.setattr(approvals, "_execute_tool", executor)
    monkeypatch.setattr(approvals, "HOLD_SECONDS", 0)
    await approvals.execute(store, action.id)

    assert tried == {"undo": False, "unlink": []}
    assert approvals.get(store, action.id).status == "done"


@pytest.mark.parametrize("way", ["undo", "unlink"])
async def test_a_yes_past_its_hold_is_not_taken_back_though_nothing_marked_its_call(store, way):
    """A worker from before the call was marked (`_calling`) never marks
    it, nor was it marked on a card executing when the mark came: past its
    hold, such a card's email may have gone, and taken back then it was
    recorded undone though sent (step 11's fourth review). Inside its hold
    it is still taken back."""
    late = (await _asked(store)).id
    early = (await _asked(store, dict(EMAIL, subject="Running late"))).id
    for card, ago in ((late, approvals.HOLD_SECONDS + approvals.CALL_MARGIN_SECONDS + 30),
                      (early, 1)):
        approvals.decide(store, card, approve=True, by="signal")
        assert approvals.claim(store, card) is not None
        with db.transaction(store):
            store.execute("UPDATE actions SET executed_at = datetime('now', ?) WHERE id = ?",
                          (f"-{ago} seconds", card))

    if way == "undo":
        undone = [card for card in (late, early) if approvals.cancel_during_hold(store, card)]
    else:
        undone = approvals.forget_chat(store, "signal", "Signal was unlinked").undone

    assert undone == [early]
    assert approvals.get(store, late).status == "executing"


async def test_what_a_call_did_is_its_cards_status_whatever_else_wrote_there(store, monkeypatch):
    """A worker from before this change, running beside this one through a
    swap, still takes a card back mid-call: what the call did is written
    all the same, or the mailbox could send the email again."""
    action = await _asked(store)
    approvals.decide(store, action.id, approve=True, by="signal")

    async def executor(tool, args):
        with store:
            store.execute("UPDATE actions SET status = 'undone' WHERE id = ?", (action.id,))
        return {"ok": True, "id": "sent"}

    monkeypatch.setattr(approvals, "_execute_tool", executor)
    monkeypatch.setattr(approvals, "HOLD_SECONDS", 0)
    await approvals.execute(store, action.id)

    assert approvals.get(store, action.id).status == "done"


async def test_an_approval_of_a_routine_undone_while_it_runs_stays_undone(store, monkeypatch):
    """`execute` wrote done over whatever the card had become: a routine's
    approval that unlinking its chat undid while it ran was recorded done
    again, and counted after all (step 11's second review). What its call
    did is kept beside the undo."""
    with store:
        card = store.execute(
            "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal, status,"
            " decided_by) VALUES ('approve_routine', ?, 'h', 'p', 'outward', 'irreversible',"
            " 'approved', 'signal')", (json.dumps({"routine_id": 1}),)).lastrowid

    async def executor(tool, args):
        assert approvals.forget_chat(store, "signal", "Signal was unlinked").undone == [card]
        return {"ok": True, "id": "started"}

    monkeypatch.setattr(approvals, "_execute_tool", executor)
    monkeypatch.setattr(approvals, "HOLD_SECONDS", 0)
    await approvals.execute(store, card)

    status, result = store.execute("SELECT status, result_json FROM actions WHERE id = ?",
                                   (card,)).fetchone()
    assert status == "undone"
    assert result is not None, "what it did is kept"
    assert json.loads(result) == {"ok": True, "id": "started"}


async def test_what_a_call_spent_counts_however_it_ended(store, monkeypatch):
    """A call that ran may have spent though it reported failure, and the
    caps must see it; a yes undone before its call spent nothing (step 11's
    third review)."""
    def card(spend: float) -> int:
        with store:
            return store.execute(
                "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal,"
                " spend_usd, status, decided_by) VALUES ('send_email', '{}', 'h', 'p',"
                " 'outward', 'irreversible', ?, 'approved', 'signal')", (spend,)).lastrowid

    ran, stopped = card(5.0), card(7.0)

    async def executor(tool, args):
        return {"ok": False, "error": "the shop timed out after taking the payment"}

    monkeypatch.setattr(approvals, "_execute_tool", executor)
    monkeypatch.setattr(approvals, "HOLD_SECONDS", 0.2)
    await approvals.execute(store, ran)
    stopping = asyncio.create_task(approvals.execute(store, stopped))
    await asyncio.sleep(0.05)
    assert approvals.cancel_during_hold(store, stopped), "undone in its hold"
    await stopping

    assert approvals.get(store, ran).status == "failed"
    assert approvals.get(store, stopped).status == "undone"
    assert approvals.spent_since(store, 1) == 5.0


async def test_a_yes_that_runs_to_the_end_is_done(store, monkeypatch):
    action = await _asked(store)
    approvals.decide(store, action.id, approve=True, by="signal")

    async def executor(tool, args):
        return {"ok": True}

    monkeypatch.setattr(approvals, "_execute_tool", executor)
    monkeypatch.setattr(approvals, "HOLD_SECONDS", 0)
    await approvals.execute(store, action.id)

    assert approvals.get(store, action.id).status == "done"


async def test_an_expired_card_cannot_be_approved(store):
    action = await _asked(store)
    with store:
        store.execute("UPDATE actions SET expires_at = ? WHERE id = ?",
                      ((datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds"),
                       action.id))

    said = approvals.decide(store, action.id, approve=True, by="signal")

    assert not said["ok"] and approvals.get(store, action.id).status == "expired"
    assert approvals.claim(store, action.id) is None


def test_what_needs_user_yes_is_read_only_from_its_frozen_file():
    """Memories, lessons and the prompts the night may edit reach what a
    model says, never what the rules allow. `core/consent.py` imports nothing
    that holds learned state, takes no database, and reads one file -- in
    the frozen core."""
    tree = ast.parse(inspect.getsource(consent))
    imported = {(n.module or "").split(".")[0] for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)}
    imported |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                 for a in n.names}
    parameters = set(inspect.signature(consent.classify).parameters)

    assert not imported & {"agent", "tools", "channels", "voice", "sqlite3"}, imported
    assert not parameters & {"conn", "db", "memory", "session"}, parameters
    assert consent.CONSENT_PATH.relative_to(consent.REPO_ROOT).as_posix() == "core/consent.yaml"
    assert load_policy().is_frozen("core/consent.yaml")
