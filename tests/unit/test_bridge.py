"""Bridge behaviour, against a fake channel.

No Signal, no models. What is worth pinning here is the handling of the edges
-- a voice note, a failing turn, two messages at once -- because each has a
failure mode that looks like nothing happening, which on a phone is
indistinguishable from the agent being dead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from channels import bridge
from channels.base import Capability, Channel, Incoming, Outgoing
from channels.bridge import ChannelBridge


class FakeChannel(Channel):
    name = "fake"

    def __init__(self, *, capabilities: set[Capability] | None = None) -> None:
        self._capabilities = capabilities or {
            Capability.TEXT, Capability.VOICE_IN, Capability.VOICE_OUT,
            Capability.TYPING, Capability.REACTIONS,
        }
        self.inbox: asyncio.Queue[Incoming | None] = asyncio.Queue()
        self.sent: list[Outgoing] = []
        self.typing_calls: list[bool] = []
        self.read: list[str] = []
        self.reactions: list[tuple[str, bool]] = []
        self.started = False
        self.send_ok = True

    @property
    def capabilities(self) -> set[Capability]:
        return self._capabilities

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def receive(self) -> AsyncIterator[Incoming]:
        while True:
            item = await self.inbox.get()
            if item is None:
                return
            yield item

    async def send(self, reply: Outgoing, *, to: str | None = None) -> bool:
        self.sent.append(reply)
        return self.send_ok

    async def typing(self, on: bool, *, to: str | None = None) -> None:
        self.typing_calls.append(on)

    async def mark_read(self, message: Incoming) -> None:
        self.read.append(message.message_id)

    async def react(self, message: Incoming, emoji: str, *, remove: bool = False) -> None:
        self.reactions.append((emoji, remove))

    def deliver(self, text: str = "hello", *, audio: Path | None = None) -> None:
        self.inbox.put_nowait(
            Incoming(
                text=text, channel=self.name, sender="+1", message_id="m1",
                timestamp=1, audio_path=audio,
            )
        )


async def _settle(bridge: ChannelBridge, expected: int, timeout: float = 3.0) -> None:
    """Wait until the channel has sent `expected` messages."""
    deadline = asyncio.get_running_loop().time() + timeout
    while len(bridge.channel.sent) < expected:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"expected {expected} sends, saw {len(bridge.channel.sent)}"
            )
        await asyncio.sleep(0.02)


@pytest.fixture
def channel():
    return FakeChannel()


async def _run(bridge: ChannelBridge, expected: int) -> None:
    await bridge.start()
    try:
        await _settle(bridge, expected)
    finally:
        await bridge.stop()


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------


async def test_a_text_message_produces_a_reply(channel):
    async def reply(text: str):
        return f"you said {text}", {}

    bridge = ChannelBridge(channel, reply)
    channel.deliver("hello")
    await _run(bridge, 1)

    assert channel.sent[0].text == "you said hello"
    assert bridge.handled == 1


async def test_a_reply_to_a_message_that_waited_on_sleep_begins_with_the_line_from_code(channel):
    """Eight hours of silence looks like a fault on a phone; the line says
    why. It is worked out from when the message was sent, as it is read, and
    a reply to a message that did not wait carries nothing. A line that
    cannot be worked out costs only the line."""
    from datetime import datetime

    asked: list[datetime] = []

    def waited(sent: datetime) -> str | None:
        asked.append(sent)
        if len(asked) == 2:
            raise RuntimeError("database is locked")
        if len(asked) == 1:
            return "Sorry for the wait: the computer was asleep until 07:45."
        return None

    async def reply(text: str):
        return f"you said {text}", {}

    bridge = ChannelBridge(channel, reply, waited=waited)
    for n, text in enumerate(("hello", "again", "and again")):
        channel.inbox.put_nowait(Incoming(text=text, channel="fake", sender="+1",
                                          message_id=f"m{n}", timestamp=1_791_000_000_000 + n))
    await _run(bridge, 3)

    assert [m.text for m in channel.sent] == [
        "Sorry for the wait: the computer was asleep until 07:45.\n\nyou said hello",
        "you said again", "you said and again"]
    assert asked[0] == datetime.fromtimestamp(1_791_000_000)


async def test_messages_are_marked_read(channel):
    bridge = ChannelBridge(channel, lambda t: _ok("ack"))
    channel.deliver("hi")
    await _run(bridge, 1)
    assert channel.read == ["m1"]


async def test_an_empty_message_does_not_start_a_turn(channel):
    called = False

    async def reply(text: str):
        nonlocal called
        called = True
        return "should not happen", {}

    bridge = ChannelBridge(channel, reply)
    channel.deliver("   ")
    await bridge.start()
    await asyncio.sleep(0.2)
    await bridge.stop()

    assert called is False
    assert channel.sent == []


async def _ok(text: str):
    return text, {}


# --------------------------------------------------------------------------
# Photos (13 October 2026: until then the words went alone)
# --------------------------------------------------------------------------


def _deliver_with(channel, text: str, *, images=(), unopened=()) -> None:
    channel.inbox.put_nowait(Incoming(
        text=text, channel=channel.name, sender="+1", message_id="m1", timestamp=1,
        images=list(images), unopened=list(unopened)))


async def test_photos_reach_the_turn_with_a_line_saying_they_came(channel, tmp_path):
    """The model is shown them; the line is how later turns, which are not,
    know they were sent. A photo with no words is a turn too: it was
    dropped unanswered."""
    first, second = tmp_path / "p1", tmp_path / "p2"
    asked = []

    async def reply(text: str, *, images=None):
        asked.append((text, images))
        return "A heron.", {}

    bridge = ChannelBridge(channel, reply)
    _deliver_with(channel, "what bird is this", images=[first])
    _deliver_with(channel, "", images=[first, second])
    await _run(bridge, 2)

    assert asked == [("[1 photo attached]\nwhat bird is this", [first]),
                     ("[2 photos attached]", [first, second])]


async def test_what_was_not_opened_is_said_to_the_turn(channel):
    asked = []

    async def reply(text: str):
        asked.append(text)
        return "I can't read PDFs yet.", {}

    bridge = ChannelBridge(channel, reply)
    _deliver_with(channel, "summarise this", unopened=["lease.pdf, a file Steve cannot open"])
    await _run(bridge, 1)

    assert asked == ["[Not opened: lease.pdf, a file Steve cannot open]\nsummarise this"]


async def test_a_file_alone_that_cannot_be_opened_is_answered_without_a_turn(channel):
    async def reply(text: str):
        raise AssertionError("a turn with nothing in it to read")

    bridge = ChannelBridge(channel, reply)
    _deliver_with(channel, "", unopened=["lease.pdf, a file Steve cannot open"])
    await _run(bridge, 1)

    assert channel.sent[0].text == ("I couldn't open what you sent: "
                                    "lease.pdf, a file Steve cannot open.")


# --------------------------------------------------------------------------
# Failure
# --------------------------------------------------------------------------


async def test_a_failing_turn_still_answers(channel):
    """Silence is indistinguishable from the agent being dead, and a phone is
    exactly where the user cannot check."""

    async def reply(text: str):
        raise RuntimeError("model exploded")

    bridge = ChannelBridge(channel, reply)
    channel.deliver("hi")
    await _run(bridge, 1)

    assert "went wrong" in channel.sent[0].text
    assert "RuntimeError" in channel.sent[0].text
    assert bridge.failed == 1


async def test_one_bad_message_does_not_stop_the_channel(channel):
    calls = []

    async def reply(text: str):
        calls.append(text)
        if text == "boom":
            raise RuntimeError("nope")
        return "fine", {}

    bridge = ChannelBridge(channel, reply)
    channel.deliver("boom")
    channel.deliver("second")
    await _run(bridge, 2)

    assert calls == ["boom", "second"]
    assert bridge.handled == 1 and bridge.failed == 1


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


async def test_turns_do_not_overlap(channel):
    """Concurrent turns in one conversation interleave tool calls, and the
    second reply gets built on a half-finished first."""
    active = 0
    overlapped = False

    async def reply(text: str):
        nonlocal active, overlapped
        active += 1
        if active > 1:
            overlapped = True
        await asyncio.sleep(0.05)
        active -= 1
        return text, {}

    bridge = ChannelBridge(channel, reply)
    for i in range(4):
        channel.deliver(f"m{i}")
    await _run(bridge, 4)

    assert overlapped is False
    assert bridge.handled == 4


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------


def _heard_as(text: str):
    async def transcribe(path: Path) -> str:
        return text

    return transcribe


def _not_heard(exc: Exception):
    async def transcribe(path: Path) -> str:
        raise exc

    return transcribe


async def test_a_voice_note_is_heard_echoed_and_answered_in_text(channel, tmp_path):
    """A reply is text, whatever the channel can carry: an `Outgoing` holds
    no audio."""
    audio = tmp_path / "note.m4a"
    audio.write_bytes(b"x")

    bridge = ChannelBridge(channel, lambda t, **_: _ok("Tokyo."),
                           transcribe=_heard_as("what is the capital of japan"))
    channel.deliver("", audio=audio)
    await _run(bridge, 2)

    # The echo matters: misrecognition is the commonest voice failure and is
    # otherwise invisible behind a confident answer.
    assert channel.sent[0].text == "heard: what is the capital of japan"
    assert channel.sent[1].text == "Tokyo."


async def test_a_voice_note_tells_the_turn_it_was_spoken_and_typed_text_does_not(channel,
                                                                                 tmp_path):
    """The intercept answers a card from a voice note only when it names the
    card's word (agent/worker/answering.py): a forwarded recording of "Yes."
    approved the one card waiting, its transcript taken as typed text
    (step 11's audit, D11). So the turn has to be told."""
    audio = tmp_path / "note.m4a"
    audio.write_bytes(b"x")
    asked: list[tuple[str, dict]] = []

    async def reply(text, **more):
        asked.append((text, more))
        return "ok", {}

    bridge = ChannelBridge(channel, reply, transcribe=_heard_as("Yes."))
    channel.deliver("", audio=audio)
    channel.deliver("Yes.")
    await _run(bridge, 3)

    assert asked == [("Yes.", {"spoken": True}), ("Yes.", {})]


async def test_a_voice_note_that_cannot_be_heard_says_why_and_asks_for_it_typed(
        channel, tmp_path):
    """The 25 September bug: without the voice add-on, every voice note was
    answered "I can't transcribe voice notes right now", and the user, away
    from the computer, could only guess why or what to do."""
    from core.hearing import HearingUnavailable

    audio = tmp_path / "n.m4a"
    audio.write_bytes(b"x")
    asked: list[str] = []

    async def reply(text):
        asked.append(text)
        return "never", {}

    bridge = ChannelBridge(channel, reply,
                           transcribe=_not_heard(HearingUnavailable("no OpenAI key is stored")))
    channel.deliver("", audio=audio)
    await _run(bridge, 1)

    assert channel.sent[0].text == ("I couldn't hear that voice note: no OpenAI key is stored."
                                    " Could you type it?")
    assert asked == []


async def test_a_voice_note_hearing_failed_on_still_asks_for_it_typed(channel, tmp_path):
    audio = tmp_path / "n.m4a"
    audio.write_bytes(b"x")
    bridge = ChannelBridge(channel, lambda t: _ok("never"),
                           transcribe=_not_heard(RuntimeError("a bug")))
    channel.deliver("", audio=audio)
    await _run(bridge, 1)
    assert channel.sent[0].text == "I couldn't hear that voice note. Could you type it?"


async def test_a_voice_note_with_no_speech_in_it_says_so(channel, tmp_path):
    audio = tmp_path / "n.m4a"
    audio.write_bytes(b"x")
    bridge = ChannelBridge(channel, lambda t: _ok("never"), transcribe=_heard_as("   "))
    channel.deliver("", audio=audio)
    await _run(bridge, 1)
    assert "didn't catch" in channel.sent[0].text


async def test_voice_note_without_hearing_configured_is_honest(channel, tmp_path):
    audio = tmp_path / "n.m4a"
    audio.write_bytes(b"x")
    bridge = ChannelBridge(channel, lambda t: _ok("never"))  # no transcriber
    channel.deliver("", audio=audio)
    await _run(bridge, 1)
    assert channel.sent[0].text == "I can't hear voice notes here. Could you type it?"


# --------------------------------------------------------------------------
# Unpairing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["unlink", "Unlink ", "/unpair"])
async def test_unlink_typed_says_goodbye_then_unpairs_without_a_turn(channel, typed):
    """From code, not the model: a turn could answer "unlink" with anything,
    and the goodbye has to go before the channel it goes on is gone."""
    order: list[str] = []
    unpaired = asyncio.Event()
    asked: list[str] = []

    async def unpair():
        order.append(f"unpaired after {len(channel.sent)} sent")
        unpaired.set()

    async def reply(text):
        asked.append(text)
        return "never", {}

    bridge = ChannelBridge(channel, reply, unpair=unpair)
    channel.deliver(typed)
    await bridge.start()
    try:
        await asyncio.wait_for(unpaired.wait(), 3)
    finally:
        await bridge.stop()

    assert [m.text for m in channel.sent] == [channel.goodbye]
    assert order == ["unpaired after 1 sent"] and asked == []


async def test_unlink_spoken_in_a_voice_note_is_an_ordinary_message(channel, tmp_path):
    """A mishearing must not unpair a channel: only the typed word does."""
    audio = tmp_path / "n.m4a"
    audio.write_bytes(b"x")
    unpaired: list[bool] = []

    async def unpair():
        unpaired.append(True)

    bridge = ChannelBridge(channel, lambda t, **_: _ok("answered"),
                           transcribe=_heard_as("unlink"), unpair=unpair)
    channel.deliver("", audio=audio)
    await _run(bridge, 2)
    assert channel.sent[1].text == "answered" and unpaired == []


async def test_a_sentence_with_unlink_in_it_is_an_ordinary_message(channel):
    unpaired: list[bool] = []

    async def unpair():
        unpaired.append(True)

    bridge = ChannelBridge(channel, lambda t: _ok("answered"), unpair=unpair)
    channel.deliver("how do I unlink my bank account?")
    await _run(bridge, 1)
    assert channel.sent[0].text == "answered" and unpaired == []


# --------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------


async def test_typing_is_shown_then_cleared(channel):
    async def slow(text: str):
        await asyncio.sleep(0.1)
        return "done", {}

    bridge = ChannelBridge(channel, slow)
    channel.deliver("hi")
    await _run(bridge, 1)

    assert True in channel.typing_calls, "never showed a typing indicator"
    assert channel.typing_calls[-1] is False, "left the indicator on"


async def test_a_channel_without_typing_is_not_asked_to_type(tmp_path):
    channel = FakeChannel(capabilities={Capability.TEXT})
    bridge = ChannelBridge(channel, lambda t: _ok("ok"))
    channel.deliver("hi")
    await _run(bridge, 1)
    assert channel.typing_calls == []


# --------------------------------------------------------------------------
# Acknowledging a message immediately
# --------------------------------------------------------------------------


async def test_a_message_is_marked_before_the_turn_starts(channel):
    """A question sent to a phone should show something within a second, not
    sit there looking ignored while a turn searches the web."""
    order = []

    async def slow(text: str):
        order.append("turn")
        await asyncio.sleep(0.05)
        return "done", {}

    channel.react = lambda m, e, remove=False: order.append(
        "unreact" if remove else "react"
    ) or asyncio.sleep(0)

    bridge = ChannelBridge(channel, slow)
    channel.deliver("hi")
    await _run(bridge, 1)

    assert order[0] == "react", "marked the message after doing the work"
    assert order[-1] == "unreact"


async def test_the_mark_comes_off_when_a_turn_fails(channel):
    """A message left permanently marked "working" is worse than never having
    marked it -- it reads as the agent still thinking, forever."""
    calls = []
    channel.react = lambda m, e, remove=False: calls.append(remove) or asyncio.sleep(0)

    async def explode(text: str):
        raise RuntimeError("model died")

    bridge = ChannelBridge(channel, explode)
    channel.deliver("hi")
    await _run(bridge, 1)

    assert calls == [False, True], "the reaction was not removed after a failure"


async def test_a_channel_without_reactions_is_not_asked_to_react(tmp_path):
    """Same guard as typing: a channel that said it cannot do this should not
    be called anyway and rely on the exception being swallowed."""
    channel = FakeChannel(capabilities={Capability.TEXT})
    called = []
    channel.react = lambda m, e, remove=False: called.append(e) or asyncio.sleep(0)

    bridge = ChannelBridge(channel, lambda t: _ok("ok"))
    channel.deliver("hi")
    await _run(bridge, 1)
    assert called == []


async def test_a_failing_reaction_does_not_break_the_turn(channel):
    """It is decoration. The reply matters and the emoji does not."""

    async def broken(message, emoji, remove=False):
        raise RuntimeError("reaction API is down")

    channel.react = broken
    bridge = ChannelBridge(channel, lambda t: _ok("the answer"))
    channel.deliver("hi")
    await _run(bridge, 1)
    assert channel.sent[0].text == "the answer"


# --------------------------------------------------------------------------
# Reactions: one tap instead of four
# --------------------------------------------------------------------------


def test_the_handler_is_actually_on_the_class():
    """Guards a slip, not a feature. Adding the reaction helpers above
    `_handle` once left it nested inside a module-level function -- the
    module still imported, every test that built a bridge still passed, and
    the bridge simply never handled a message again."""
    assert callable(getattr(bridge.ChannelBridge, "_handle", None))


CARD = "Email Bob about Friday\n\nReply YES MAPLE to go ahead, NO MAPLE to drop it."


@pytest.mark.parametrize("emoji", ["👍", "✅", "✔"])
def test_a_thumbs_up_on_a_card_approves_that_card(emoji):
    assert bridge.reaction_as_answer(emoji, CARD) == "YES MAPLE"


@pytest.mark.parametrize("emoji", ["👎", "❌", "🚫"])
def test_a_thumbs_down_on_a_card_drops_that_card(emoji):
    assert bridge.reaction_as_answer(emoji, CARD) == "NO MAPLE"


@pytest.mark.parametrize("emoji", ["❤️", "😂", "🎉", "", "👀"])
def test_every_other_reaction_means_nothing(emoji):
    """Reacting to a message is ordinary conversation. If a heart woke the
    model, every acknowledgement would cost a turn and a reply."""
    assert bridge.reaction_as_answer(emoji, CARD) is None


@pytest.mark.parametrize("on", ["Done, it's in your calendar for 3pm.", "", None])
def test_a_thumbs_up_on_anything_but_a_card_means_nothing(on):
    """The first version made every thumbs-up a bare "yes", so thumbs-upping
    "Done, it's in your calendar" approved whatever card was waiting."""
    assert bridge.reaction_as_answer("👍", on) is None


def test_the_card_line_the_bridge_reads_is_the_one_cards_are_written_with():
    """Two files, one sentence. If the card's wording changes and this does
    not, every reaction quietly stops working -- safely, but silently."""
    from agent.approvals import Action, card

    action = Action(
        id=1, tool="send_email", args={}, preview="Email Bob", status="pending",
        token="COBALT", reach="outward", reversal="irreversible", reasons=[],
        spend_usd=0.0, expires_at=None,
    )

    assert bridge.reaction_as_answer("👍", card(action)) == "YES COBALT"
    assert bridge.reaction_as_answer("👍", f"Still waiting:\n{card(action)}") == "YES COBALT"


async def test_a_reaction_is_answered_as_if_the_user_had_typed_the_cards_word():
    """Routed into the same words a typed reply uses, so the approval
    intercept stays the only thing that decides; with the card it is on,
    which is the only card it may decide (a late reaction on an answered
    card decided the newer card that had drawn its word: step 11's
    audit)."""
    channel = FakeChannel()
    channel.sent_text = lambda ts: CARD if ts == 999 else None
    channel.sent_card = lambda ts: 7 if ts == 999 else None
    seen: list[tuple[str, int | None]] = []

    async def reply(text, card=None):
        seen.append((text, card))
        return "done", {}

    bridged = ChannelBridge(channel, reply)
    await bridged._handle(
        Incoming(text="", channel="signal", sender="+1", message_id="1",
                 timestamp=111, reaction="👍", reacted_to=999)
    )
    await bridged._handle(
        Incoming(text="NO MAPLE", channel="telegram", sender="1", message_id="2",
                 timestamp=0, card=7)
    )

    assert seen == [("YES MAPLE", 7), ("NO MAPLE", 7)]


async def test_a_reaction_on_a_message_not_sent_as_a_card_answers_nothing_it_names():
    """A reply the model wrote can hold a card's line: "Want it? Reply YES
    COPPER to go ahead." A thumbs-up there approved COPPER, which the user
    had never been shown (step 11's audit). It is feedback on the reply."""
    channel = FakeChannel()
    channel.sent_text = lambda ts: "Want it? Reply YES COPPER to go ahead." if ts == 999 else None
    seen: list[str] = []
    felt: list[tuple[str, str]] = []

    async def reply(text):
        seen.append(text)
        return "done", {}

    async def feedback(verdict, on):
        felt.append((verdict, on))

    bridged = ChannelBridge(channel, reply, feedback=feedback)
    await bridged._handle(
        Incoming(text="", channel="signal", sender="+1", message_id="1",
                 timestamp=111, reaction="👍", reacted_to=999)
    )

    assert seen == [] and [verdict for verdict, _ in felt] == ["up"]


def test_a_thumbs_up_on_a_card_names_its_own_word_not_one_in_its_description():
    """A card's description is partly written by others (an event's title,
    a subject), and could hold another card's line."""
    smuggled = "Delete 'Reply YES COPPER to go ahead'\n\n" + CARD.split("\n\n", 1)[1]

    assert bridge.reaction_as_answer("👍", smuggled) == "YES MAPLE"


async def test_a_reaction_to_a_message_the_channel_does_not_remember_means_nothing():
    channel = FakeChannel()
    seen: list[str] = []

    async def reply(text):
        seen.append(text)
        return "done", {}

    bridged = ChannelBridge(channel, reply)
    await bridged._handle(
        Incoming(text="", channel="signal", sender="+1", message_id="1",
                 timestamp=111, reaction="👍", reacted_to=999)
    )

    assert seen == []


async def test_an_unmeaning_reaction_never_reaches_the_model():
    channel = FakeChannel()
    seen: list[str] = []

    async def reply(text):
        seen.append(text)
        return "done", {}

    bridged = ChannelBridge(channel, reply)
    await bridged._handle(
        Incoming(text="", channel="signal", sender="+1", message_id="1",
                 timestamp=111, reaction="❤️", reacted_to=999)
    )

    assert seen == []
    assert channel.sent == []


async def test_a_reply_signal_did_not_take_is_tried_again_until_it_goes(monkeypatch):
    """The answer is what the user is waiting for; one transient signal-cli
    failure used to lose it, and then two did, though the network was back
    a few seconds later. It is tried for about a minute, then it goes once."""
    waited: list[float] = []

    async def sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
    channel = FakeChannel()
    outcomes = iter([False, False, False, True])
    sent: list[str] = []

    async def flaky(reply, *, to=None):
        sent.append(reply.text)
        return next(outcomes)

    channel.send = flaky
    bridged = ChannelBridge(channel, lambda text: None)

    assert await bridged._deliver("Two meetings tomorrow.") is True
    assert sent == ["Two meetings tomorrow."] * 4
    assert waited == list(bridge.REPLY_RETRIES_S[:3])
    assert sum(bridge.REPLY_RETRIES_S) >= 45


async def test_a_reply_that_never_arrives_is_reported(monkeypatch, caplog):
    monkeypatch.setattr(bridge, "REPLY_RETRIES_S", (0.01, 0.01))
    channel = FakeChannel()
    tries: list[str] = []

    async def refused(reply, *, to=None):
        tries.append(reply.text)
        return False

    channel.send = refused
    bridged = ChannelBridge(channel, lambda text: None)

    assert await bridged._deliver("Two meetings tomorrow.") is False
    assert len(tries) == 3
    assert "could not be delivered" in caplog.text
