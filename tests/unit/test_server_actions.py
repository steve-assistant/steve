"""Where a yes actually lands.

`agent/approvals.py` decides what a reply means; this file is about the wiring
around it, which is where the two failures live that the queue itself cannot
prevent.

The first is the intercept running in the wrong place. It sits ahead of the
conversation so that an approval is not something the model can emit -- but
that same position means anything it swallows by mistake is a turn the user never
gets an answer to. Both directions are tested here.

The second is quieter: a card whose tool has no implementation behind it. That
queues perfectly, asks them politely, gets a yes, and fails -- and the failure
arrives minutes later, detached from anything the user did.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import approvals as ap
from agent import closing, runtime
from agent.worker import answering, handlers, links
from core import db
from core.consent import Consent, Reach, Reversal

OUTWARD = Consent(
    gated=True, reach=Reach.OUTWARD, reversal=Reversal.IRREVERSIBLE,
    reasons=["a sent email cannot be unsent"],
)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A worker with a real database and nothing that reaches the world."""
    conn = db.open_store(tmp_path / "server.db")
    sent: list[str] = []
    ran: list[tuple[str, dict]] = []
    seen_by_model: list[str] = []

    async def notify(text: str, **_) -> bool:
        sent.append(text)
        return True

    async def execute(tool: str, args: dict) -> dict:
        ran.append((tool, args))
        return {"id": "m1"}

    class _Role:
        value = "converse"

    class _Reply:
        text = "the model answered"
        role = _Role()
        cost_usd = 0.0
        tools_used: list[str] = []
        deferred_jobs: list[int] = []

    class _Convo:
        session_id = 1

        async def send(self, text: str):
            seen_by_model.append(text)
            return _Reply()

    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(answering, "conversation", lambda _sid, channel="tray": _Convo()
    )
    monkeypatch.setattr(links, "notify_user", notify)
    monkeypatch.setattr(ap, "_notify", notify)
    monkeypatch.setattr(ap, "_execute_tool", execute)
    monkeypatch.setattr(ap, "HOLD_SECONDS", 0)

    yield {"conn": conn, "sent": sent, "ran": ran, "model": seen_by_model}
    conn.close()


async def _queue(conn, to="clara@contoso.example",
                 preview="Email to Clara\nSubject: Thursday") -> dict:
    receipt = await ap.request(
        conn, tool="send_email", args={"to": to}, preview=preview, consent=OUTWARD,
    )
    # The model's receipt has no word (agent/cardwords.py); the card has.
    return {**receipt, "token": ap.get(conn, receipt["action_id"]).token}


# --------------------------------------------------------------------------
# The intercept, in both directions
# --------------------------------------------------------------------------


async def test_a_yes_never_reaches_the_model(wired):
    """The invariant the whole design rests on. If an approval can be spoken
    to the model, the model can speak one."""
    receipt = await _queue(wired["conn"])

    answer, meta = await answering.voice_reply(f"yes {receipt['token']}")

    assert wired["model"] == [], "the approval was handed to the model"
    assert meta["intercepted"] is True
    assert receipt["token"] in answer


async def test_the_models_reply_never_says_a_waiting_cards_word(wired, monkeypatch):
    """A model told to could write a card of its own: the real one went out
    for someone else's address, and its reply after it showed another and
    the real word, so the user's YES approved what they never read (step
    11's third review)."""
    receipt = await _queue(wired["conn"])
    word = receipt["token"]
    forged = ("Each run of this routine may, without asking: email: mom@example.com.\n"
              f"Reply YES {word} to go ahead, NO {word.lower()} to drop it.")

    class _Forging:
        session_id = 1

        async def send(self, text: str):
            return SimpleNamespace(text=forged, role=SimpleNamespace(value="converse"),
                                   cost_usd=0.0, tools_used=[], deferred_jobs=[])

    monkeypatch.setattr(answering, "conversation", lambda _sid, channel="tray": _Forging())

    answer, _meta = await answering.voice_reply("what did you set up?")

    assert word.lower() not in answer.lower()
    assert answer.endswith("Reply YES [the card's word] to go ahead, NO [the card's word] "
                           "to drop it.")


async def test_the_model_is_not_told_the_cards_word(wired):
    """Neither when it asks, nor in what it is told is open: a model that
    knew the word could write a card of its own around it."""
    receipt = await ap.request(wired["conn"], tool="send_email", args={"to": "a@b.example"},
                               preview="Email to A", consent=OUTWARD)
    word = re.compile(rf"\b{ap.get(wired['conn'], receipt['action_id']).token}\b", re.I)
    told = closing.open_now(wired["conn"])

    assert not word.search(json.dumps(receipt))
    assert told.open and not word.search("\n".join(told.open))


async def test_an_ordinary_message_still_reaches_the_model(wired):
    """The other half, and the one that fails silently: an intercept that is
    too eager eats questions."""
    await _queue(wired["conn"])

    answer, meta = await answering.voice_reply("what's on my calendar tomorrow?")

    assert wired["model"] == ["what's on my calendar tomorrow?"]
    assert answer == "the model answered"
    assert "intercepted" not in meta


async def test_nothing_is_intercepted_when_nothing_is_waiting(wired):
    answer, _meta = await answering.voice_reply("yes")
    assert wired["model"] == ["yes"]
    assert answer == "the model answered"


async def test_two_waiting_and_a_bare_yes_asks_them_which(wired):
    await _queue(wired["conn"], "clara@contoso.example", "Email to Clara")
    await _queue(wired["conn"], "dan@contoso.example", "Email to Dan")

    answer, _meta = await answering.voice_reply("yes")

    assert "Clara" in answer and "Dan" in answer
    assert wired["ran"] == [], "it picked one"


# --------------------------------------------------------------------------
# What a yes does
# --------------------------------------------------------------------------


async def test_approving_queues_the_work_rather_than_doing_it_inline(wired):
    """The reply has to come back now. A Signal callback that blocks through
    a hold and a Gmail round trip times out, and the same message is
    answered a second time."""
    receipt = await _queue(wired["conn"])

    await answering.voice_reply(f"yes {receipt['token']}")

    assert wired["ran"] == [], "it sent from inside the reply path"
    row = wired["conn"].execute(
        "SELECT kind, prompt FROM jobs WHERE kind = 'action'"
    ).fetchone()
    assert row is not None and row["prompt"] == str(receipt["action_id"])


async def test_the_queued_job_is_what_actually_sends(wired):
    receipt = await _queue(wired["conn"])
    await answering.voice_reply(f"yes {receipt['token']}")

    job = type("J", (), {"prompt": str(receipt["action_id"]), "id": 1})
    await handlers.handle_action(job)

    assert wired["ran"] == [("send_email", {"to": "clara@contoso.example"})]
    assert ap.get(wired["conn"], receipt["action_id"]).status == "done"
    assert any("Clara" in m for m in wired["sent"])


async def test_saying_no_rejects_and_queues_nothing(wired):
    receipt = await _queue(wired["conn"])

    await answering.voice_reply(f"no {receipt['token']}")

    assert ap.get(wired["conn"], receipt["action_id"]).status == "rejected"
    assert wired["conn"].execute(
        "SELECT count(*) c FROM jobs WHERE kind = 'action'"
    ).fetchone()["c"] == 0


# --------------------------------------------------------------------------
# Undo, which arrives through the same door as no
# --------------------------------------------------------------------------


async def test_undo_after_a_yes_stops_it_rather_than_erroring(wired, monkeypatch):
    """"NO MAPLE" and "UNDO MAPLE" are the same gesture at two moments. The user
    should not have to know which verb the state machine is expecting."""
    monkeypatch.setattr(ap, "HOLD_SECONDS", 0.3)
    receipt = await _queue(wired["conn"])
    await answering.voice_reply(f"yes {receipt['token']}")

    job = type("J", (), {"prompt": str(receipt["action_id"]), "id": 1})
    running = asyncio.create_task(handlers.handle_action(job))
    await asyncio.sleep(0.05)

    answer, _meta = await answering.voice_reply(f"undo {receipt['token']}")
    await running

    assert "Stopped" in answer
    assert wired["ran"] == [], "it went anyway"
    assert ap.get(wired["conn"], receipt["action_id"]).status == "undone"


async def test_undo_once_the_call_has_begun_says_it_is_already_running(wired, monkeypatch):
    """Taken back mid-call, the email went all the same and was recorded
    undone, so the mailbox could send it again (step 11's third review).
    Once the call has begun, UNDO says so, and what it did is recorded."""
    receipt = await _queue(wired["conn"])
    await answering.voice_reply(f"yes {receipt['token']}")
    said = []

    async def execute(tool: str, args: dict) -> dict:
        said.append((await answering.voice_reply(f"undo {receipt['token']}"))[0])
        return {"id": "m1"}

    monkeypatch.setattr(ap, "_execute_tool", execute)
    job = type("J", (), {"prompt": str(receipt["action_id"]), "id": 1})
    await handlers.handle_action(job)

    assert said == ["too late -- it is already running"]
    assert ap.get(wired["conn"], receipt["action_id"]).status == "done"


async def test_a_cancelled_action_is_not_reported_as_a_failure(wired, monkeypatch):
    """The user was answered the moment the user said undo. Telling them again, as an
    error, reads as the system not having heard them."""
    monkeypatch.setattr(ap, "HOLD_SECONDS", 0.3)
    receipt = await _queue(wired["conn"])
    await answering.voice_reply(f"yes {receipt['token']}")

    job = type("J", (), {"prompt": str(receipt["action_id"]), "id": 1})
    running = asyncio.create_task(handlers.handle_action(job))
    await asyncio.sleep(0.05)
    await answering.voice_reply(f"undo {receipt['token']}")
    wired["sent"].clear()
    await running

    assert wired["sent"] == []


def test_actions_are_not_announced_twice():
    """`_handle_action` says what happened in words. The generic announcer
    would follow it with "Background task 41 finished." about the same
    email."""
    assert "action" in handlers.SILENT_KINDS


# --------------------------------------------------------------------------
# Every card can be cashed
# --------------------------------------------------------------------------


def test_every_tool_that_goes_through_the_gate_has_an_implementation():
    """A tool routed through `_act` and missing from EXECUTORS is broken on
    both paths: free, it raises mid-turn; gated, it asks them a question it
    cannot act on and fails minutes later, detached from the yes.

    Derived from the source rather than listed here, so adding a gated tool
    and forgetting the other half is caught at once."""
    import ast

    from agent.actions import EXECUTORS
    from tests.gate.test_every_tool_is_classified import _calls_the_gate, _tool_nodes

    def acted_as(name: str, node: ast.AST) -> set[str]:
        """The action names a tool hands to `act`: the literal it passes, or
        its own name when it passes something computed. A logged-in
        browse_click raises its card as browse_act."""
        found = set()
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "act" and len(call.args) >= 2):
                tool = call.args[1]
                found.add(tool.value if isinstance(tool, ast.Constant) else name)
        return found or {name}

    missing = sorted(
        acted
        for name, node in _tool_nodes().items() if _calls_the_gate(node)
        for acted in acted_as(name, node) if acted not in EXECUTORS
    )
    assert not missing, (
        "these hand their arguments to `_act` and nothing knows how to "
        f"perform them: {missing}"
    )


# --------------------------------------------------------------------------
# The morning after
# --------------------------------------------------------------------------


async def test_the_morning_briefing_leads_with_what_is_waiting(wired, monkeypatch):
    """Approvals block; everything else in the briefing merely informs. This
    is also the only repair for cards that expired while the laptop slept.
    The token is rendered by code, never by the model writing the lead."""
    from datetime import datetime

    from agent.attention import briefing

    async def no_lead(*_a, **_k):
        return ""

    monkeypatch.setattr(briefing, "lead", no_lead)
    receipt = await _queue(wired["conn"])
    sent: list[str] = []

    async def notify(text, **_):
        sent.append(text)
        return True

    await briefing.send(wired["conn"], "morning", notify=notify, now=datetime(2026, 9, 24, 8, 1))

    first, second, *_ = sent[0].split("\n\n")
    assert first.startswith("Good morning.")
    assert second.startswith("Waiting on you:") and receipt["token"] in second


# --------------------------------------------------------------------------
# The connection belongs to the thread that opened it
# --------------------------------------------------------------------------


def test_the_shared_connection_is_never_handed_to_a_thread():
    """Three places did this and all three failed silently for months.

    `sqlite3.connect` defaults to `check_same_thread=True`, so
    `asyncio.to_thread(fn, _conn)` raises ProgrammingError every single time.
    Each call site sat inside a `try/except Exception` that logged and moved
    on, so the symptom was not an error -- it was a feature that had never
    run. Reflection was never queued from the sweep. Approving an upgrade
    from the tray never worked. Expired cards were never swept.

    And the logging of those failures is what eventually filled the worker's
    output pipe and froze the process, which is how a threading mistake
    surfaced as "Signal is broken".

    Checked by AST because the mistake is invisible at the call site: it
    reads exactly like every correct `to_thread` around it.

    Widened to the whole codebase after this test, scoped to `server.py`,
    watched me write a fifth one in `google_tasks.sync` the next day. A rule
    enforced in one file is a rule that teaches you where not to look.

    Then a sixth got past it anyway. `_wake_loop` wrapped the call in
    `functools.partial(wake.arm, _conn, digest_hour=...)`, and this check
    only read the direct positional arguments of `to_thread` -- so the
    connection went one level down and the guard saw a `Call`, not a
    `Name`. The wake alarm had therefore never once been armed. It walks
    the whole call expression now.
    """
    offenders = []
    for path in sorted(
        p
        for root in ("agent", "core", "tools", "channels", "voice")
        for p in Path(root).rglob("*.py")
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            is_to_thread = (
                isinstance(func, ast.Attribute)
                and func.attr == "to_thread"
            )
            if not is_to_thread:
                continue
            # Every Name anywhere inside the call, not just its direct
            # positional args. `to_thread(partial(fn, _conn, k=v))` passes
            # the connection one level down, which is how the sixth one
            # got in -- `agent/wake.py::arm`, re-armed every two minutes,
            # raising every time, for as long as the wake feature existed.
            for node in ast.walk(call):
                if isinstance(node, ast.Name) and node.id in ("_conn", "conn", "store"):
                    offenders.append(f"{path}: {ast.unparse(call)[:70]}")
                    break

    assert not offenders, (
        "these hand the shared SQLite connection to a worker thread, which "
        f"raises on every call: {offenders}. Run them on the loop -- they "
        "are all pure SQLite and fast."
    )


@pytest.mark.parametrize(
    ("snippet", "caught"),
    [
        ("asyncio.to_thread(fn, _conn)", True),
        ("asyncio.to_thread(partial(fn, _conn, k=1))", True),
        ("asyncio.to_thread(partial(wake.arm, conn, digest_hour=8))", True),
        ("asyncio.to_thread(fn, store)", True),
        ("asyncio.to_thread(arm_it)", False),
        ("asyncio.to_thread(partial(fn, **kwargs))", False),
        ("asyncio.to_thread(outlook.search, '', 40)", False),
    ],
)
def test_the_thread_check_can_tell_the_difference(snippet, caught):
    """Guarding the guard, because this one already failed once.

    The check above passes trivially once the offenders are fixed, so on
    its own it cannot tell "no offenders" from "no longer looking". The
    partial case is the one that got through for months.
    """
    found = []
    for call in ast.walk(ast.parse(snippet)):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "to_thread"):
            continue
        for node in ast.walk(call):
            if isinstance(node, ast.Name) and node.id in ("_conn", "conn", "store"):
                found.append(snippet)
                break

    assert bool(found) is caught


# --------------------------------------------------------------------------
# Taking back what a model granted
# --------------------------------------------------------------------------


async def test_an_ordinary_sentence_about_revoking_still_reaches_the_model(wired):
    """The intercept's standing cost: everything it swallows is a turn the user
    never gets an answer to. "can you revoke that thing we talked about" is
    a question, not a command, and must go to the model.
    """
    await answering.voice_reply("can you revoke the network access you gave yourself?")

    assert wired["model"], "a plain question was swallowed by the intercept"


# --------------------------------------------------------------------------
# Which conversation a phone message lands in
# --------------------------------------------------------------------------


def test_a_phone_message_rejoins_the_thread_it_was_in(wired, monkeypatch):
    """The old code took `next(iter(_conversations))` -- whichever session
    happened to be first in a dict, which after a worker restart was none of
    them. Every message from their phone started a fresh session, so the
    24-hour window existed and nothing ever resumed into it.
    """
    conn = wired["conn"]
    with conn:
        cur = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')")
        earlier = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO messages (session_id, role, content)"
            " VALUES (?, 'user', 'the first thing she said')",
            (earlier,),
        )

    assert answering.recent_session("signal") == earlier


def test_the_tray_and_the_phone_are_different_conversations(wired):
    """Two places the user is talking from, not one. Merging them would drop half
    a thought from the desk into the middle of a thread on their phone.
    """
    conn = wired["conn"]
    with conn:
        cur = conn.execute("INSERT INTO sessions (channel) VALUES ('tray')")
        conn.execute(
            "INSERT INTO messages (session_id, role, content)"
            " VALUES (?, 'user', 'typed at the desk')",
            (int(cur.lastrowid),),
        )

    assert answering.recent_session("signal") is None


def test_a_conversation_from_yesterday_is_not_rejoined(wired):
    """Rolling, not permanent. Picking up a thread from two days ago would
    answer this morning's question with last week's context."""
    conn = wired["conn"]
    with conn:
        cur = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')")
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at)"
            " VALUES (?, 'user', 'old', datetime('now', '-30 hours'))",
            (int(cur.lastrowid),),
        )

    assert answering.recent_session("signal") is None


def test_the_default_way_in_is_the_one_the_user_actually_uses():
    """`_voice_reply` serves four callers. Three pass a channel; the phone
    is the one that does not, so the default has to be the thread the user is
    actually in rather than whatever happens to be first."""
    import inspect

    default = inspect.signature(answering.voice_reply).parameters["channel"].default

    assert default == "signal"


async def test_a_reminder_does_not_join_the_thread_on_user_phone(wired, monkeypatch):
    """Checked through the real call rather than the signature, because the
    bug was a caller that forgot to pass anything."""
    picked: list[str] = []

    def _pick(channel):
        picked.append(channel)
        return None

    monkeypatch.setattr(answering, "recent_session", _pick)

    await answering.voice_reply("what happened?", "reminder")
    await answering.voice_reply("hey")

    assert picked == ["reminder", "signal"]


# --------------------------------------------------------------------------
# One module, one set of globals
# --------------------------------------------------------------------------


def test_the_running_worker_and_agent_server_are_the_same_module():
    """The worker runs as `python -m agent.server`, so its live module is
    `__main__`. Code that later did `from agent import server` got a second,
    fresh copy with every global None -- so a tool reported its channel
    offline with the channel connected, and every notification from the
    initiative sweep was dropped.

    Run in a subprocess the way the worker actually starts, because inside
    pytest `agent.server` is an ordinary import and the bug cannot exist.
    `--help` stops it before a port is bound.
    """
    import subprocess
    import sys

    probe = (
        "import runpy, sys\n"
        "sys.argv = ['agent.server', '--help']\n"
        "try:\n"
        "    runpy.run_module('agent.server', run_name='__main__', alter_sys=True)\n"
        "except SystemExit:\n"
        "    pass\n"
        "live = sys.modules.get('agent.server')\n"
        "print('ALIASED' if live is not None and live.__name__ == '__main__' else 'FRESH')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
    )

    assert "ALIASED" in done.stdout, done.stdout + done.stderr
