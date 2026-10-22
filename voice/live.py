"""GPT-Live at the desk: the connection, the voice, and the record of one conversation.

These came from the phone. The personal edition's calls ran on GPT-Live,
and the desk's live conversation (`voice/desk_call.py`) borrowed the call's
modules for what the two shared. The product has no phone, so the shared
part lives here and the phone's part is gone: the carrier's 8 kHz audio,
the bridge to it, and the outbound agent and its brief.

What stayed is what the desk still depends on:
- **the connection** (`connect`), on the user's OpenAI key, and the one
  error raised before a word was said (`LiveUnavailable`), so the desk can
  say so and go back to the local voice;
- **the voice and the limits** a hand-over is measured by: a second
  hand-over with nothing new heard inside `SAME_REQUEST_S` is the same
  request twice, and an answer is cut to what one appended context holds;
- **the promise it doesn't keep** (`PROMISE`). GPT-Live says "let me check"
  and sometimes never hands off; the desk's watchdog hears the promise and
  hands off itself;
- **the record** (`Session`, `Turn`): the user's words handed over, the
  answers said, both sides' transcript and what the voice cost.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agent.zones import local_zone
from core import endpoints

# Picked on 24 September 2026 from all 22 voices recorded saying the same
# line. `marin`, the default, sounded unenthusiastic.
VOICE = "cedar"

START_TIMEOUT_S = 8.0
# Appended context is capped at 500 tokens; roughly four characters each,
# with room to spare.
MAX_RESULT_CHARS = 1600
# A second handoff this soon after one with nothing new heard is the same
# request, delegated twice (the watchdog and the model both acting).
SAME_REQUEST_S = 10.0

PROMISE = re.compile(
    r"\b(let me (check|look|see|find|pull)|one (moment|sec|second)|give me a "
    r"(sec|second|moment)|checking( on)? (that|it|now)|looking (that|it) up)\b",
    re.IGNORECASE,
)


def when(now: dt.datetime | None) -> str:
    """The moment as the voice is told it: "Tuesday 13 October, 9:05 AM"."""
    now = (now or dt.datetime.now(dt.UTC)).astimezone(local_zone())
    return f"{now:%A} {now.day} {now:%B}, {now.strftime('%I:%M %p').lstrip('0')}"


class LiveUnavailable(RuntimeError):
    """GPT-Live could not carry this conversation. Raised only before a word
    was said, so the desk can say so and carry on with the local voice."""


async def connect():
    import websockets

    from core import secrets

    return await websockets.connect(
        f"{endpoints.openai_socket()}/live/sessions",
        additional_headers={"Authorization": f"Bearer {secrets.require('OPENAI_API_KEY')}"},
        max_size=None,
        open_timeout=START_TIMEOUT_S,
    )


@dataclass
class Turn:
    """One hand-over: the user's words, and the answer that was spoken."""

    said: str
    heard: str
    # The answer's cost stays 0 here: the answers are the assistant's own
    # turns, billed where they ran.
    cost_usd: float = 0.0
    seconds: float = 0.0


# Their words in; the answer, and what answering cost, out.
Ask = Callable[[str], Awaitable[tuple[str, float]]]


@dataclass
class Session:
    """One live conversation: what was handed over, what was said, and what
    the voice cost."""

    ask: Ask | None = None
    turns: list[Turn] = field(default_factory=list)
    # Everything said, in order: ["them" or "us", words], from GPT-Live's
    # own transcripts. Kept on the desk's thread for reading back.
    lines: list[list[str]] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    # The voice, billed by the second: GPT-Live's own count, over every
    # stretch of the conversation.
    voice_seconds: float = 0.0
    voice_cost_usd: float = 0.0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def note(self, who: str, fragment: str) -> None:
        """A transcript fragment, joined to the line before if it is the same voice's."""
        if self.lines and self.lines[-1][0] == who:
            self.lines[-1][1] = joined([self.lines[-1][1], fragment])
        else:
            self.lines.append([who, joined([fragment])])

    @property
    def transcript(self) -> str:
        names = {"them": "you", "us": "me"}
        return "\n".join(f"{names[who]}: {words}" for who, words in self.lines if words)

    @property
    def cost_usd(self) -> float:
        return sum(turn.cost_usd for turn in self.turns) + self.voice_cost_usd


def joined(parts: list[str]) -> str:
    """Transcript fragments into text. They arrive either with their own
    spaces or without; a space is added only where neither side has one."""
    out = ""
    for part in parts:
        if out and not out[-1].isspace() and part and not part[0].isspace():
            out += " "
        out += part
    return re.sub(r"\s+", " ", out).strip()


def speakable(text: str) -> str:
    """Strip what a voice cannot say: markdown handed to a voice is read out
    as asterisks. The assistant writes for a screen."""
    clean = re.sub(r"[*_`#]+", "", (text or "").strip())
    clean = re.sub(r"\s*\n\s*", " ", clean)
    return re.sub(r"\s{2,}", " ", clean).strip()
