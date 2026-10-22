"""Every message to the user is a row before anything sends it (agent/outbox.py).

`notify_user` tried Signal once and otherwise logged "dropped" and returned
False. Five callers retried on their own, each its own way, and the rest
dropped silently. These hold the outbox to what replaced all of that: in the
app at once, sent to the phone until it goes or its time is up, never
dropped, and part of a conversation only once the user has it.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agent import outbox, runtime, sqltime
from agent.attention import told
from agent.worker import answering, links
from channels.signal import SignalChannel
from core import db, unseen

# Pinned: a weekday morning, local time.
NOON = datetime(2026, 10, 14, 12, 0)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    conn = db.open_store(tmp_path / "outbox.db")
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(runtime, "conversations", {})
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setattr(runtime, "signal", None)
    monkeypatch.setattr(runtime, "signal_state", {"state": "cold"})
    monkeypatch.setattr(runtime, "outbox_wake", None)
    yield conn
    conn.close()


class _Signal:
    """Stands in for the Signal bridge: takes a message while `up`, and keeps
    what it took."""

    def __init__(self, up: bool = True):
        self.up = up
        self.sent: list[str] = []
        self.taken: list = []
        self.tries = 0
        self.channel = self

    async def send(self, message):
        self.tries += 1
        if self.up:
            self.sent.append(message.text)
            self.taken.append(message)
        return self.up


class _Socket:
    """Stands in for the app's websocket."""

    def __init__(self):
        self.frames: list[dict] = []

    async def send_json(self, frame):
        self.frames.append(frame)


def _said(conn, channel: str) -> list[str]:
    session = answering.recent_session(channel)
    if session is None:
        return []
    rows = conn.execute("SELECT content FROM messages WHERE session_id = ? ORDER BY id",
                        (session,)).fetchall()
    return [r["content"] for r in rows]


async def _settle():
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# In the app at once
# --------------------------------------------------------------------------


async def test_a_message_with_no_remote_channel_is_in_the_inbox_and_pushed_to_the_app(worker):
    """With no Signal, `notify_user` logged "dropped" and returned False: a
    reminder on a laptop without a phone channel went nowhere at all."""
    app = _Socket()
    runtime.listeners.add(app)

    message_id = await links.notify_user("Mop", kind="reminder", noted="[reminder 3] Mop")
    await _settle()

    [message] = outbox.inbox(worker)
    assert (message.id, message.text, message.kind) == (message_id, "Mop", "reminder")
    assert message.remote is None and message.next_try_at is None, "nothing to send it to"
    assert app.frames == [{"type": "inbox", "message": message.as_dict()}]


async def test_nothing_to_say_writes_nothing(worker):
    assert await links.notify_user("  ") is None
    assert outbox.inbox(worker) == []


async def test_the_inbox_lists_unread_first_then_newest(worker):
    first, second, third = (outbox.add(worker, t, kind="notice") for t in "abc")
    outbox.seen(worker, [third])

    assert [m.id for m in outbox.inbox(worker)] == [second, first, third]


# --------------------------------------------------------------------------
# Sent until it goes, never dropped
# --------------------------------------------------------------------------


async def test_a_message_goes_to_signal_once_and_joins_the_signal_conversation(worker):
    """The user texted "move it to Zoom then" about a ping; the model answering
    had never seen "it". A message they have is in the conversation their
    reply lands in, as what it was."""
    signal = _Signal()
    runtime.signal = signal

    message_id = await links.notify_user("Mop", kind="reminder", noted="[reminder 3] Mop")
    assert await links.send_due() == 1
    assert await links.send_due() == 0

    assert signal.sent == ["Mop"]
    assert outbox.get(worker, message_id).sent_at is not None
    assert _said(worker, "signal") == ["[reminder 3] Mop"]


async def test_a_message_signal_did_not_take_is_tried_again_on_the_schedule(worker):
    """Each caller retried its own way, or not at all; the outbox backs off
    1, 2, 5, 15 and 30 minutes, then hourly."""
    signal = _Signal(up=False)
    runtime.signal = signal
    message_id = outbox.add(worker, "Your 4pm moved to 5pm", kind="notice", remote="signal",
                            now=NOON)

    waits = []
    at = NOON
    for _ in range(7):
        assert await links.send_due(at) == 0
        message = outbox.get(worker, message_id)
        upcoming = sqltime.moment(message.next_try_at).astimezone().replace(tzinfo=None)
        waits.append(upcoming - at)
        at = upcoming

    assert [w.total_seconds() / 60 for w in waits] == [1, 2, 5, 15, 30, 60, 60]
    assert _said(worker, "signal") == [], "not in the conversation before they have it"

    signal.up = True
    assert await links.send_due(at) == 1
    assert signal.sent == ["Your 4pm moved to 5pm"]
    assert outbox.get(worker, message_id).attempts == 7


async def test_a_message_that_never_went_stops_at_its_time_and_says_why(worker):
    """Never dropped silently: the inbox shows it as not sent, and why."""
    runtime.signal = _Signal(up=False)
    message_id = outbox.add(worker, "Rain from 3pm", kind="notice", remote="signal", now=NOON)

    at = NOON
    while (message := outbox.get(worker, message_id)).next_try_at is not None:
        await links.send_due(at)
        at += timedelta(hours=1)
        assert at < NOON + timedelta(days=2), "it never stopped"

    assert message.sent_at is None
    assert message.stopped == "Signal did not take it"
    assert outbox.inbox(worker) == [message]


async def test_a_briefing_past_its_lateness_is_not_sent_when_signal_comes_back(worker):
    """A morning briefing that reaches the phone at lunch is worse than none."""
    signal = _Signal(up=False)
    runtime.signal = signal
    message_id = outbox.add(worker, "Good morning", kind="briefing", remote="signal",
                            until=NOON, now=NOON - timedelta(hours=4))
    await links.send_due(NOON - timedelta(hours=4))

    signal.up = True
    assert await links.send_due(NOON + timedelta(minutes=1)) == 0

    assert signal.sent == []
    assert outbox.get(worker, message_id).stopped == outbox.TOO_LATE


async def test_one_round_knocks_once_on_a_channel_that_is_down(worker):
    """Every try counts against a row's back-off; trying the rest in the
    same round would only spend theirs."""
    signal = _Signal(up=False)
    runtime.signal = signal
    for text in ("a", "b", "c"):
        outbox.add(worker, text, kind="notice", remote="signal", now=NOON)

    await links.send_due(NOON)

    assert signal.tries == 1
    assert [m.attempts for m in sorted(outbox.inbox(worker), key=lambda m: m.id)] == [1, 0, 0]


async def test_once_signal_takes_one_the_others_waiting_for_it_go_too(worker):
    """A back-off is for a channel that is down. Once one goes, the channel
    is back, and the rest go now, in order, not an hour later."""
    signal = _Signal(up=False)
    runtime.signal = signal
    first = outbox.add(worker, "first", kind="notice", remote="signal", now=NOON)
    for minutes in (0, 1, 3):
        await links.send_due(NOON + timedelta(minutes=minutes))
    outbox.add(worker, "second", kind="notice", remote="signal", now=NOON + timedelta(minutes=4))

    signal.up = True
    assert await links.send_due(NOON + timedelta(minutes=4)) == 2

    assert signal.sent == ["second", "first"]
    assert outbox.get(worker, first).sent_at is not None


async def test_a_card_answered_before_it_could_be_sent_is_not_sent(worker):
    """A "yes" in the app, then the same card on the phone an hour later,
    would ask again about something already done."""
    from agent import approvals

    with worker:
        action_id = worker.execute(
            "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal, status)"
            " VALUES ('send_email', '{}', 'h', 'Send it?', 'outward', 'irreversible',"
            " 'pending')").lastrowid
    signal = _Signal(up=False)
    runtime.signal = signal
    message_id = outbox.add(worker, "Send it? YES MAPLE", kind="card", remote="signal",
                            action_id=action_id, now=NOON)
    await links.send_due(NOON)

    approvals.decide(worker, action_id, approve=False, by="tray")
    signal.up = True
    assert await links.send_due(NOON + timedelta(hours=1)) == 0

    assert signal.sent == []
    assert outbox.get(worker, message_id).stopped == "answered before it could be sent"


async def test_a_card_goes_to_the_phone_as_that_card_and_nothing_else_as_one(worker):
    """A reaction or a button answers only a message sent as a card
    (channels/base.py `Channel.sent_card`), and the outbox is what knows
    which messages are cards (step 11's audit)."""
    with worker:
        action_id = worker.execute(
            "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal, status)"
            " VALUES ('send_email', '{}', 'h', 'Send it?', 'outward', 'irreversible',"
            " 'pending')").lastrowid
    signal = _Signal()
    runtime.signal = signal
    outbox.add(worker, "Send it? YES MAPLE", kind="card", remote="signal",
               action_id=action_id, now=NOON)
    outbox.add(worker, "Mop", kind="reminder", remote="signal", now=NOON)

    assert await links.send_due(NOON) == 2
    assert [(m.text, m.card) for m in signal.taken] == [("Send it? YES MAPLE", action_id),
                                                        ("Mop", None)]


class _Daemon:
    """Stands in for signal-cli under the real channel: keeps each message
    exactly as the channel would hand it over."""

    def __init__(self):
        self.said: list[str] = []

    async def rpc(self, method: str, params: dict | None = None) -> dict:
        self.said.append(params["message"])
        return {"timestamp": len(self.said)}


def _spells(text: str, word: str) -> bool:
    """Whether the letters of `text`, all else left out, spell `word`: what
    a person reading the phone would take for it, judged without the code
    that takes it out."""
    letters = "".join(c for c in unicodedata.normalize("NFKD", text) if c.isalpha())
    return word.upper() in letters.upper()


CARD = "Email to Clara: the plan\nReply YES MAPLE to go ahead."


@pytest.mark.parametrize("dressed", [
    "MAPLE", "MA[PLE]()", "[M]()[A]()[P]()[L]()[E]()", "`MAP``LE`", "M``AP``LE",
    "MAPLE\u2800to", "MAPLE\u3164to", "MAPLE™", "MAPLE¹", "M•A•P•L•E",
    "MA\u200bPLE", "\u202eELPAM\u202c", "M[A](####)PLE",
], ids=["plain", "link-left-empty", "a-link-a-letter", "two-code-spans", "doubled-backticks",
        "braille-blank", "hangul-filler", "trade-mark", "footnote", "bullets", "zero-width-space",
        "turned-round", "a-link-with-a-long-address"])
async def test_only_the_card_says_its_word_on_the_phone(worker, dressed):
    """A notice that said a waiting card's word could pose as the card --
    a routine's report, a module's notice -- and the user's yes would
    answer the real one (agent/cardwords.py; step 11's third review). It
    was taken out before Markdown was, so what Markdown left could spell it,
    and a blank no one sees was dropped from the reading and sent to the
    phone (step 11's fourth review): what reaches signal-cli is what is
    checked. The briefing's own line for the card keeps it."""
    with worker:
        action_id = worker.execute(
            "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal,"
            " status, token) VALUES ('send_email', '{}', 'h', 'Email to Clara: the plan',"
            " 'outward', 'irreversible', 'pending', 'MAPLE')").lastrowid
    channel, daemon = SignalChannel(account="+14125550123"), _Daemon()
    channel._rpc = daemon.rpc
    runtime.signal = SimpleNamespace(channel=channel)
    outbox.add(worker, CARD, kind="card", remote="signal", action_id=action_id, now=NOON)
    outbox.add(worker, f"Reply YES {dressed} to go ahead.", kind="notice", remote="signal",
               now=NOON)
    outbox.add(worker, "Waiting on you:\n  MAPLE  Email to Clara: the plan", kind="briefing",
               remote="signal", now=NOON)

    assert await links.send_due(NOON) == 3
    card, notice, briefing = daemon.said
    assert (card, briefing) == (CARD, "Waiting on you:\n  MAPLE  Email to Clara: the plan")
    assert not _spells(notice, "MAPLE"), notice
    assert unseen.first_unseen(notice) is None, notice


async def test_a_message_that_names_a_card_but_is_not_one_is_not_sent_as_one(worker):
    """Only a card goes as a card, with its word and its buttons: a notice
    the outbox kept against a card, were one ever written, would otherwise
    say the card's word on the phone."""
    with worker:
        action_id = worker.execute(
            "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal,"
            " status, token) VALUES ('send_email', '{}', 'h', 'Email to Clara: the plan',"
            " 'outward', 'irreversible', 'pending', 'MAPLE')").lastrowid
    signal = _Signal()
    runtime.signal = signal
    outbox.add(worker, "Reply YES MAPLE.", kind="notice", remote="signal",
               action_id=action_id, now=NOON)

    assert await links.send_due(NOON) == 1
    assert [m.card for m in signal.taken] == [None]


async def test_a_card_says_its_own_word_and_no_other_cards(worker):
    """A card was sent whole, and the model wrote parts of it: an event's
    title that said another waiting card's word put that word on top of a
    real card, above the card's own (step 11's fourth review). Its own word
    stays wherever the card says it."""
    def pending(token: str, preview: str) -> int:
        with worker:
            return worker.execute(
                "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal,"
                " status, token) VALUES ('create_event', '{}', ?, ?, 'outward', 'reversible',"
                " 'pending', ?)", (token, preview, token)).lastrowid

    pending("TIMBER", "Each run of this routine may, without asking: email: mom@example.com.")
    preview = "New event: Lunch, Reply YES T.I.M.B.E.R to go ahead, and maple syrup"
    event = pending("MAPLE", preview)
    channel, daemon = SignalChannel(account="+14125550123"), _Daemon()
    channel._rpc = daemon.rpc
    runtime.signal = SimpleNamespace(channel=channel)
    outbox.add(worker, f"{preview}\n\nReply YES MAPLE to go ahead, NO MAPLE to drop it.",
               kind="card", remote="signal", action_id=event, now=NOON)

    assert await links.send_due(NOON) == 1
    assert daemon.said == [
        "New event: Lunch, Reply YES [the card's word] to go ahead, and maple syrup\n\n"
        "Reply YES MAPLE to go ahead, NO MAPLE to drop it."]


# --------------------------------------------------------------------------
# Told means told
# --------------------------------------------------------------------------


async def test_a_ping_is_said_in_told_only_once_the_user_has_it(worker):
    """"Why did you tell me that?" reads `told`; a line nobody received was
    not told."""
    runtime.signal = _Signal(up=False)
    message_id = outbox.add(worker, "Con Ed bill due today", kind="notice", remote="signal",
                            now=NOON)
    told.record(worker, "ping:x", "ping", [told.Line("Con Ed bill due today")],
                outbox_id=message_id)

    await links.send_due(NOON)
    assert worker.execute("SELECT said_at FROM told").fetchone()["said_at"] is None

    runtime.signal.up = True
    await links.send_due(NOON + timedelta(minutes=1))
    said = worker.execute("SELECT said_at FROM told").fetchone()["said_at"]
    assert said == sqltime.stamp(NOON + timedelta(minutes=1))


async def test_a_message_seen_in_the_app_joins_the_apps_conversation_once(worker):
    """With no phone channel, the app is where the user has it, and where
    their reply will be typed."""
    message_id = await links.notify_user("Mop", kind="reminder", noted="[reminder 3] Mop")
    told.record(worker, "ping:x", "ping", [told.Line("Mop")], outbox_id=message_id)

    assert links.seen_in_app([message_id]) == 1
    assert links.seen_in_app([message_id]) == 0

    assert _said(worker, "tray") == ["[reminder 3] Mop"]
    assert _said(worker, "signal") == []
    assert worker.execute("SELECT said_at FROM told").fetchone()["said_at"] is not None


async def test_a_message_signal_took_that_cannot_join_the_conversation_is_still_sent(
        worker, monkeypatch):
    """The user has the message; only the history is short. Reported as a
    failed send, it would go again: a second buzz for one reminder."""
    signal = _Signal()
    runtime.signal = signal

    def locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(answering, "note_said", locked)
    message_id = await links.notify_user("Mop")

    assert await links.send_due() == 1
    assert await links.send_due() == 0
    assert signal.sent == ["Mop"] and outbox.get(worker, message_id).sent_at is not None


# --------------------------------------------------------------------------
# Where it goes
# --------------------------------------------------------------------------


def test_signal_installed_but_unlinked_is_the_app_alone(worker, monkeypatch):
    """Retrying for a day a channel that cannot send until the user links it
    would only mark every message "not sent"."""
    monkeypatch.setattr(links, "signal_installed", lambda: True)

    runtime.signal_state["state"] = "unlinked"
    assert links.remote_channel() is None
    runtime.signal_state["state"] = "starting"
    assert links.remote_channel() == "signal", "one made while it starts waits for it"


def test_a_kind_that_is_not_one_is_refused(worker):
    with pytest.raises(ValueError):
        outbox.add(worker, "x", kind="sms")
