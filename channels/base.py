"""What a messaging channel is, independent of which one.

The agent should not know whether a message arrived from Signal, the tray app,
or something added later. Every channel produces `Incoming` and accepts
`Outgoing`; everything above this line is the same code.

Voice notes are part of the interface rather than an extension because they
change the shape of a turn: an inbound audio attachment has to be transcribed
before the agent sees it, and a reply may be spoken rather than typed. A
channel that cannot do audio simply reports so, and the agent sends text.
"""

from __future__ import annotations

import abc
import contextlib
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Capability(StrEnum):
    TEXT = "text"
    VOICE_IN = "voice_in"  # can deliver audio the agent should transcribe
    VOICE_OUT = "voice_out"  # can carry a spoken reply back
    ATTACHMENTS = "attachments"
    TYPING = "typing"  # can show a pending indicator
    READ_RECEIPTS = "read_receipts"
    REACTIONS = "reactions"  # can put an emoji on a specific message


@dataclass
class Incoming:
    """One message from the outside world."""

    text: str
    channel: str
    sender: str
    message_id: str
    timestamp: int  # milliseconds since epoch, as every provider reports it
    audio_path: Path | None = None  # set when the message was a voice note
    attachments: list[Path] = field(default_factory=list)
    # The photos the model is shown with the message (channels/photos.py), and
    # what else came that it is not: each said in a few words ("report.pdf,
    # a file Steve cannot open"), so the turn knows it was sent.
    images: list[Path] = field(default_factory=list)
    unopened: list[str] = field(default_factory=list)
    # The emoji, when this arrived as a reaction to something rather than as
    # a message. A reaction is one tap where "YES NUTMEG" is four, which is
    # the difference between an approval queue the user uses and one they resent.
    reaction: str | None = None
    # The message the reaction was attached to, in epoch milliseconds.
    reacted_to: int | None = None
    # The card a reaction or a button answers, by its id, when the channel
    # knows it: the answer then decides that card or none
    # (agent/approvals.parse_reply), not whichever card has its word now.
    card: int | None = None
    # Which pairing of its channel this came over, for a channel that pairs
    # more than once in its life (Telegram's `pairing`): an answer read over
    # one since undone does not count (agent/worker/links.answers_from).
    link: int | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_voice(self) -> bool:
        return self.audio_path is not None

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "channel": self.channel,
            "sender": self.sender,
            "message_id": self.message_id,
            "timestamp": self.timestamp,
            "is_voice": self.is_voice,
            "attachments": [str(p) for p in self.attachments],
            "images": [str(p) for p in self.images],
            "unopened": list(self.unopened),
        }


@dataclass
class Outgoing:
    """A reply."""

    text: str
    # The approval card this message is, by its id: set by the outbox alone
    # (agent/worker/links.py), and the only way a reaction or a button on a
    # message answers a card (`Channel.sent_card`).
    card: int | None = None


class Channel(abc.ABC):
    """A two-way message transport.

    Implementations are expected to be resilient rather than correct: a
    channel that raises on a malformed frame takes the agent's only link to
    the outside world down with it, so the contract is to log and continue.
    """

    name: str = "channel"
    # The last thing said on a channel the user has just unpaired
    # (channels/bridge.UNPAIR_WORDS): what is gone, and what is left for them
    # to do at their end.
    goodbye: str = "Unpaired. I won't read this chat again."

    @property
    @abc.abstractmethod
    def capabilities(self) -> set[Capability]:
        """What this channel can actually do, so callers stop guessing."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Connect. Must be safe to call when already started."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Disconnect. Must be safe to call when never started."""

    @abc.abstractmethod
    def receive(self) -> AsyncIterator[Incoming]:
        """Yield messages as they arrive, until stopped."""

    @abc.abstractmethod
    async def send(self, reply: Outgoing, *, to: str | None = None) -> bool:
        """Deliver a reply. Returns whether it went out."""

    async def typing(self, on: bool, *, to: str | None = None) -> None:
        """Show or clear a pending indicator. Optional; default is a no-op."""
        return None

    async def mark_read(self, message: Incoming) -> None:
        """Acknowledge receipt. Optional; default is a no-op."""
        return None

    async def react(self, message: Incoming, emoji: str, *, remove: bool = False) -> None:
        """Put an emoji on a specific message, or take it off again.

        Optional; default is a no-op. Distinct from `typing` because it is
        attached to the message rather than to the conversation -- a typing
        indicator vanishes after a few seconds and says nothing about *which*
        message is being worked on, which matters when two arrive together.
        """
        return None

    def sent_text(self, timestamp: int) -> str | None:
        """What this channel sent at `timestamp`, if it still remembers.

        How a reaction finds out what it was a reaction *to*. A channel that
        cannot say returns None, and a reaction on it then means nothing --
        which is the safe answer, since a thumbs-up on an unknown message
        must never be taken as a yes to whatever card is waiting.
        """
        return None

    def sent_card(self, timestamp: int) -> int | None:
        """Which card went out at `timestamp` (`Outgoing.card`), or None if
        what went then was not sent as a card, or is forgotten.

        Whether a reaction answers a card. Reading the card's line in the
        message text was not enough: the model, or the outside text it
        read, can write "Reply YES COPPER to go ahead" into a reply, or into
        another card's description, and a thumbs-up there approved COPPER,
        which the user had never been shown (step 11's audit).
        """
        return None

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def status(self) -> dict:
        return {"channel": self.name, "capabilities": sorted(self.capabilities)}


# How long a photo or voice note the user sent is kept: a day, long enough
# to ask about it again, and not so long that the data folder becomes a
# second copy of the user's camera roll. Signal's, Telegram's and the app's
# each kept their own copy of it.
KEEP_ATTACHMENTS_S = 24 * 3600


def delete_older(folders: Iterable[Path], cutoff: float) -> int:
    """Delete the files in `folders` last written before `cutoff` (epoch
    seconds): how a channel's photos and voice notes go after their day.
    Returns how many went. BLOCKING."""
    gone = 0
    for folder in folders:
        try:
            files = [entry for entry in folder.iterdir() if entry.is_file()]
        except OSError:
            continue
        for entry in files:
            with contextlib.suppress(OSError):
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    gone += 1
    return gone
