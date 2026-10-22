"""Who may answer a card, and when a bare yes still means the card.

`tests/unit/test_approval_replies.py` covers how a reply is read. This covers
the two things around it that live in the worker: which inputs are allowed
to be read as answers at all, and whether the conversation has moved on
since the card was asked. Both were missing. Every input that goes through
`_voice_reply` -- a reminder's text, a job's brief -- used to pass the
same intercept as the user's own messages, and a bare
yes resolved to a card however much had been said since.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent import approvals, outbox, runtime, sqltime
from agent.worker import answering
from agent.worker.answering import APPROVAL_CHANNELS


@pytest.fixture
def worker(store, monkeypatch):
    """The real intercept, on this test's database, with decisions recorded
    instead of carried out."""
    from types import SimpleNamespace

    decided: list[tuple[int, bool]] = []

    async def decide(action_id, *, approve, by):
        decided.append((action_id, approve))
        return {"ok": True}

    monkeypatch.setattr(runtime, "conn", store)
    monkeypatch.setattr(answering, "decide_action", decide)
    return SimpleNamespace(intercept=answering.intercept_approval, decided=decided)


def _card(conn, token="MAPLE", asked="2026-09-23 10:00:00", *, sent_to="signal") -> int:
    """A card asked at `asked`, and sent to the phone's chat then, as the
    outbox sends one (`links.send_due`): its row marked sent, and its text
    a line in that channel's conversation, written just after (here, the
    next second). `sent_to=None`: the app's alone."""
    with conn:
        card = conn.execute(
            "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal,"
            " status, token, asked_at) VALUES ('send_email', '{}', ?, 'Email Bob',"
            " 'outward', 'irreversible', 'pending', ?, ?)",
            (token, token, asked),
        ).lastrowid
    if sent_to is not None:
        at = datetime.fromisoformat(asked).replace(tzinfo=UTC)
        text = f"Email Bob\n\nReply YES {token} to go ahead."
        message = outbox.add(conn, text, kind="card", remote=sent_to, action_id=card, now=at)
        outbox.sent(conn, message, now=at)
        _said(conn, text, sqltime.stamp(at + timedelta(seconds=1)), role="assistant",
              channel=sent_to)
    return card


def _said(conn, text, at, *, role="user", channel="signal"):
    with conn:
        sid = conn.execute("INSERT INTO sessions (channel) VALUES (?)", (channel,)).lastrowid
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at)"
            " VALUES (?, ?, ?, ?)",
            (sid, role, text, at),
        )


@pytest.mark.parametrize("channel", ["reminder", "watch", "initiative", "night", "job"])
async def test_only_the_users_own_channels_can_answer_a_card(worker, store, channel):
    """A reminder reading "send it" is not the user saying so, and neither
    is a job's brief."""
    _card(store)

    assert await worker.intercept("send it", channel) is None
    assert worker.decided == []


@pytest.mark.parametrize("channel", sorted(APPROVAL_CHANNELS))
async def test_their_own_channels_still_can(worker, store, channel):
    card = _card(store)

    reply = await worker.intercept("YES MAPLE", channel)

    assert reply is not None
    assert worker.decided == [(card, True)]


def test_nothing_said_since_the_card_means_a_bare_yes_is_about_it(store):
    card = _card(store, asked="2026-09-23 10:00:00")
    _said(store, "email bob about friday", "2026-09-23 09:59:58")

    assert not approvals.moved_on_since(store, approvals.get(store, card), "signal")


def test_anything_the_user_said_after_the_card_means_it_may_not_be(store):
    card = _card(store, asked="2026-09-23 10:00:00")
    _said(store, "also, what's on tomorrow?", "2026-09-23 10:03:00")

    assert approvals.moved_on_since(store, approvals.get(store, card), "signal")


async def test_a_bare_yes_after_steve_asked_something_since_asks_which(worker, store):
    """A bare yes answers the last question in the chat. Counting only the
    user's own messages, a card at 10:00 and Steve's "want me to put it in
    your calendar too?" at 10:05 made a yes meant for the question approve
    the card (step 11's audit)."""
    _card(store, asked="2026-09-23 10:00:00")
    _said(store, "Want me to put it in your calendar too?", "2026-09-23 10:05:00",
          role="assistant")

    reply = await worker.intercept("yes", "signal")

    assert reply.startswith("Is that about MAPLE")
    assert worker.decided == []


async def test_a_bare_yes_in_a_chat_the_card_never_reached_asks_which(worker, store):
    """A card the app alone shows, or one sent to the other channel, is not
    the last thing in this chat: a bare yes here was about something else."""
    _card(store, asked="2026-09-23 10:00:00", sent_to="telegram")

    reply = await worker.intercept("yes", "signal")

    assert reply.startswith("Is that about MAPLE")
    assert worker.decided == []


async def test_what_is_said_in_another_chat_does_not_move_this_one_on(worker, store):
    """Steve's words at the desk are not a question on the phone."""
    card = _card(store, asked="2026-09-23 10:00:00")
    _said(store, "It's sunny out.", "2026-09-23 10:05:00", role="assistant", channel="voice")

    await worker.intercept("yes", "signal")

    assert worker.decided == [(card, True)]


async def test_a_bare_yes_after_moving_on_asks_instead_of_acting(worker, store):
    _card(store, asked="2026-09-23 10:00:00")
    _said(store, "also, what's on tomorrow?", "2026-09-23 10:03:00")

    reply = await worker.intercept("yes", "signal")

    assert "YES MAPLE" in reply
    assert worker.decided == []


async def test_a_bare_yes_right_after_the_card_still_just_works(worker, store):
    card = _card(store, asked="2026-09-23 10:00:00")

    await worker.intercept("yes", "signal")

    assert worker.decided == [(card, True)]


async def test_a_voice_note_answers_a_card_only_when_it_says_the_cards_word(worker, store):
    """A forwarded voice note saying "Yes." approved the one card waiting:
    its transcript reached the intercept as typed text does (step 11's
    audit, D11). Anyone's recording can say yes; only someone reading the
    card says MAPLE. Typed, a bare yes still answers."""
    card = _card(store, asked="2026-09-23 10:00:00")

    bare = await worker.intercept("Yes.", "signal", spoken=True)
    named = await worker.intercept("Yes, maple.", "signal", spoken=True)

    assert "YES MAPLE" in bare
    assert worker.decided == [(card, True)] and named.startswith("Going ahead with MAPLE")


async def test_a_voice_note_reaches_the_intercept_marked_as_spoken(worker, store, monkeypatch):
    """The bridge's mark (channels/bridge.py) has to arrive: `voice_reply`
    is the reply function both phones' bridges call."""
    _card(store, asked="2026-09-23 10:00:00")

    said, meta = await answering.voice_reply("Yes.", "signal", spoken=True)

    assert meta == {"intercepted": True} and "YES MAPLE" in said
    assert worker.decided == []


def test_the_intercept_has_no_default_channel():
    """A default is how a new caller ends up answering cards without
    anyone deciding it should."""
    import inspect


    param = inspect.signature(answering.intercept_approval).parameters["channel"]
    assert param.default is inspect.Parameter.empty
