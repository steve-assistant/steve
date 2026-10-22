"""Connecting a channel to the conversation loop.

One message in, one turn, one reply out. The interesting parts are the edges:

* **Voice notes are heard before the agent sees them** (`transcribe`), so a
  spoken message and a typed one are the same kind of turn by the time they
  reach the loop. A note that cannot be heard is answered with why, and a
  request to type it: never silence. Replies are text; spoken ones wait for
  the voices (step 7).
* **"unlink" unpairs the channel** (`unpair`), typed: the goodbye goes, then
  the channel forgets the account, from code rather than a turn.
* **Turns are serialised per channel.** Two messages arriving together would
  otherwise interleave tool calls in one conversation, and the second reply
  would be built on a half-finished first.
* **A failing turn still gets a reply.** Silence is indistinguishable from the
  agent being dead, and a phone is exactly where you cannot check.
* **A reply that waited on sleep says so** (`waited`), in a line from code, so
  eight hours of silence is explained rather than left to look like a fault.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from channels import photos
from channels.base import Capability, Channel, Incoming, Outgoing
from core.hearing import HearingUnavailable

log = logging.getLogger("channel-bridge")

# Put on the message the moment it is picked up, taken off when the reply
# goes out. Not chosen by the model and not part of the answer: it is
# mechanical acknowledgement, so that a question sent to a phone shows
# *something* within a second rather than sitting there looking ignored while
# a turn searches the web.
#
# A reaction rather than only a typing indicator because it sticks to the
# message. Typing fades after a few seconds and says nothing about which
# message is being worked on, which matters when two arrive together.
WORKING_EMOJI = "👀"

# How long to wait before each further try at a reply the channel did not
# take: about a minute in all, which outlasts a network's blip and
# signal-cli's daemon being started again (channels/signal.py `_revive`).
# Two tries two seconds apart lost a reply to a network back a few seconds
# later (step 10's hostile scenarios, tests/e2e/test_hostile_signal.py).
REPLY_RETRIES_S = (2.0, 5.0, 15.0, 30.0)

# Called as `reply_fn(text)`, or `reply_fn(text, images=[...])` for a
# message that came with photos (channels/photos.py), or
# `reply_fn(text, card=id)` for a reaction or a button on a card, or
# `reply_fn(text, spoken=True)` for a voice note's transcript: a recording
# anyone could have made and forwarded, so a bare yes in it answers no card
# (`answering.intercept_approval`).
ReplyFn = Callable[..., Awaitable[tuple[str, dict]]]

# The bridge a reply was read over, and its pairing (`Incoming.link`), while
# the reply's turn runs: an answer to a card counts only if that is still up
# as it is recorded (agent/worker/links.answers_from). Without it the
# answer's channel was only a name, and an answer read before an unpair
# counted once another chat was paired (step 11's third review).
READ_OVER: ContextVar[tuple[ChannelBridge, int | None] | None] = ContextVar(
    "read_over", default=None)

# What, typed alone, unpairs the channel it is typed on: Signal's word,
# Telegram's command, and the plain one.
UNPAIR_WORDS = frozenset({"unlink", "unpair", "/unpair"})


# A reaction that means something. Everything else -- a heart, a laugh -- is
# the user reacting to a message rather than answering a question, and waking
# the model for one would make every acknowledgement cost a turn.
APPROVING = {"👍", "✅", "✔"}
REFUSING = {"👎", "❌", "🚫"}


# The line every approval card ends with -- `agent.approvals.card` -- and
# the only thing a reaction can approve. tests/unit/test_bridge.py checks the
# two stay in step.
CARD_ANSWER = re.compile(r"Reply YES ([A-Za-z]+) to go ahead")


def reaction_as_answer(emoji: str, card: str | None) -> str | None:
    """What a reaction means, or None if it means nothing here.

    `card` is the text of the card it was attached to, or None when what it
    was attached to was not sent as a card (`Channel.sent_card`). A
    thumbs-up counts only on a card, and then it names that card: "YES
    MAPLE", exactly what typing it would have said, taken from the card's
    own last line. The first version turned every thumbs-up into a bare
    "yes" whatever it was on, so thumbs-upping "Done, it's in your
    calendar" approved whatever card happened to be waiting -- an email the
    user never looked at. The second read the first card line anywhere in
    the message, which a card's description, or a reply, could hold for
    another card (step 11's audit).

    Translated into the same words a typed reply uses rather than given its
    own path, so the approval intercept stays the only thing that decides.
    """
    cleaned = (emoji or "").strip()
    if cleaned in APPROVING:
        verb = "YES"
    elif cleaned in REFUSING:
        verb = "NO"
    else:
        return None
    words = CARD_ANSWER.findall(card or "")
    if not words:
        return None
    return f"{verb} {words[-1]}"


def feedback_of(emoji: str) -> str | None:
    """"up" or "down" for a thumbs on a message that is not a card; None
    for anything else -- a heart is a reaction, not a verdict."""
    cleaned = (emoji or "").strip()
    if cleaned in APPROVING:
        return "up"
    if cleaned in REFUSING:
        return "down"
    return None


class ChannelBridge:
    """Runs one channel against the agent until stopped."""

    def __init__(
        self,
        channel: Channel,
        reply_fn: ReplyFn,
        *,
        conn: sqlite3.Connection | None = None,
        transcribe: Callable[[Path], Awaitable[str]] | None = None,
        feedback: Callable[[str, str], Awaitable[None]] | None = None,
        waited: Callable[[datetime], str | None] | None = None,
        unpair: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.channel = channel
        self.reply_fn = reply_fn
        # The line a reply begins with when its message waited on the
        # computer asleep, given when the message was sent, local time
        # (agent/awake.waited_line); None when it did not wait.
        self.waited = waited
        # Where a thumbs-up or -down on one of its own messages goes, when it
        # is not answering a card: their word on that reply, for the failure
        # ledger (agent/issues.py). Before 28 September it was dropped.
        self.feedback = feedback
        self.conn = conn
        # What a voice note says, or `HearingUnavailable` with why
        # (core/hearing.py, through the worker, which bills it).
        self.transcribe = transcribe
        # Forgets this channel's account: run once the goodbye has gone,
        # after this bridge's turn, since it stops the bridge.
        self.unpair = unpair
        self._unpairing: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._turn_lock = asyncio.Lock()
        self.handled = 0
        self.failed = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        await self.channel.start()
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.channel.stop()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            **self.channel.status(),
            "bridge_running": self.running,
            "handled": self.handled,
            "failed": self.failed,
        }

    # -- the loop ----------------------------------------------------------

    async def _run(self) -> None:
        try:
            async for message in self.channel.receive():
                if self._stop.is_set():
                    return
                # Serialised deliberately: concurrent turns in one conversation
                # interleave tool calls and build the second reply on a
                # half-finished first.
                async with self._turn_lock:
                    await self._handle(message)
        except Exception:  # noqa: BLE001 - the listener must outlive one bad message
            log.exception("channel listener crashed; restarting")
            if not self._stop.is_set():
                await asyncio.sleep(2)
                self._task = asyncio.create_task(self._run())

    async def _handle(self, message: Incoming) -> None:
        with contextlib.suppress(Exception):
            await self.channel.mark_read(message)

        if message.reaction is not None:
            stamp = message.reacted_to or 0
            on = self.channel.sent_text(stamp)
            card = self.channel.sent_card(stamp)
            answer = reaction_as_answer(message.reaction, on if card is not None else None)
            if answer is None:
                verdict = feedback_of(message.reaction)
                if verdict and on and self.feedback is not None:
                    with contextlib.suppress(Exception):
                        await self.feedback(verdict, on)
                    return
                log.info("ignoring reaction %r", message.reaction)
                return
            message = replace(message, text=answer, card=card)

        text, spoken_input = await self._resolve_text(message)
        if spoken_input and not text:
            return  # `_resolve_text` has said why
        if not text and not message.images:
            if message.unopened:
                # Nothing for a turn to read: said here, with no model call.
                await self._deliver(f"I couldn't open what you sent: "
                                    f"{'; '.join(message.unopened)}.")
                return
            log.info("ignoring empty message from %s", message.sender)
            return
        if (self.unpair is not None and not spoken_input
                and text.strip().lower() in UNPAIR_WORDS):
            await self._deliver(self.channel.goodbye)
            # A task of its own: it stops this bridge, whose task this is.
            # Held, since the loop keeps only a weak reference to it.
            self._unpairing = asyncio.create_task(self.unpair())
            return
        # As it is read, not once the reply is written: the wait the line is
        # about is the one before Steve read the message.
        sorry = None
        if self.waited is not None and message.timestamp:
            with contextlib.suppress(Exception):
                sorry = self.waited(datetime.fromtimestamp(message.timestamp / 1000))

        await self._mark_working(message, on=True)
        typing = asyncio.create_task(self._keep_typing())
        # The conversation records the line for what came with the message,
        # so later turns know a photo was sent, though they cannot see it.
        asked = "\n".join(part for part in (photos.note(message.images, message.unopened),
                                           text) if part)
        reading = READ_OVER.set((self, message.link))
        try:
            if message.images:
                reply_text, _meta = await self.reply_fn(asked, images=message.images)
            elif message.card is not None:
                reply_text, _meta = await self.reply_fn(asked, card=message.card)
            elif spoken_input:
                reply_text, _meta = await self.reply_fn(asked, spoken=True)
            else:
                reply_text, _meta = await self.reply_fn(asked)
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            log.exception("turn failed")
            # A phone is exactly where the user cannot check whether the agent
            # died, so say something rather than nothing.
            await self.channel.send(
                Outgoing(text=f"That went wrong: {type(exc).__name__}. Try again?")
            )
            return
        finally:
            READ_OVER.reset(reading)
            typing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing
            # Guarded like the refresh loop is: a channel that declared it
            # cannot show a typing indicator should not be asked to clear one.
            if self.channel.supports(Capability.TYPING):
                with contextlib.suppress(Exception):
                    await self.channel.typing(False)
            # In the finally so the emoji comes off on a failed turn too --
            # a message left permanently marked "working" is worse than never
            # having marked it.
            await self._mark_working(message, on=False)

        if sorry and reply_text:
            reply_text = f"{sorry}\n\n{reply_text}"
        await self._send_reply(reply_text)
        self.handled += 1

    async def _resolve_text(self, message: Incoming) -> tuple[str, bool]:
        """Return the text of a turn, and whether it arrived as speech."""
        if not message.is_voice:
            return message.text.strip(), False

        if self.transcribe is None or not self.channel.supports(Capability.VOICE_IN):
            await self._deliver("I can't hear voice notes here. Could you type it?")
            return "", True

        try:
            heard = await self.transcribe(message.audio_path)
        except HearingUnavailable as exc:
            log.warning("a voice note was not heard: %s", exc)
            await self._deliver(f"I couldn't hear that voice note: {exc}. Could you type it?")
            return "", True
        except Exception:  # noqa: BLE001
            log.exception("hearing a voice note failed")
            await self._deliver("I couldn't hear that voice note. Could you type it?")
            return "", True

        heard = (heard or "").strip()
        if not heard:
            await self._deliver("I didn't catch anything in that. Could you type it?")
            return "", True

        # Echo what was heard. Misrecognition is the most common failure of a
        # voice turn and is otherwise invisible -- the user sees a confident
        # answer to a question they did not ask.
        await self.channel.send(Outgoing(text=f"heard: {heard}"))
        return heard, True

    async def _deliver(self, text: str) -> bool:
        """Send the answer, trying again for about a minute if the channel
        does not take it (`REPLY_RETRIES_S`).

        The answer is the one thing the user is waiting for. A single transient
        signal-cli failure used to lose it, with only a log line to say so.
        Kept in order: the next message's answer waits for this one.
        """
        for wait in (*REPLY_RETRIES_S, None):
            if await self.channel.send(Outgoing(text=text)):
                return True
            if wait is not None:
                await asyncio.sleep(wait)
        log.warning("reply could not be delivered: %s", text[:80])
        return False

    async def _send_reply(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            await self._deliver(text)

    async def _mark_working(self, message: Incoming, *, on: bool) -> None:
        """Show that this message is being worked on. Best effort."""
        if not self.channel.supports(Capability.REACTIONS):
            return
        with contextlib.suppress(Exception):
            await self.channel.react(message, WORKING_EMOJI, remove=not on)


    async def _keep_typing(self) -> None:
        """Hold the typing indicator up while a turn runs.

        Signal clears it after a few seconds, and a turn that searches the web
        takes longer than that; without refreshing, the user watches nothing
        happen.
        """
        if not self.channel.supports(Capability.TYPING):
            return
        while True:
            with contextlib.suppress(Exception):
                await self.channel.typing(True)
            await asyncio.sleep(8)
