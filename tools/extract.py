"""Pulling commitments out of the user's own sent mail.

This is the one path in the system where a model reads personal content that
has not been redacted, and that is deliberate rather than an oversight: the
whole purpose of the block is to learn identities and obligations, and
redaction removes exactly those. What bounds it instead is *direction* --
only mail the user wrote. Their own words are where "I'll send you the photos
Friday" lives, and it is the half the user authored. Received mail is ingested by
pure code in `tools/ingest.py`, headers only.

The other bound is on what survives extraction. A model asked to find
promises in prose will find promises in prose, including in text that
contains none, and it will phrase them confidently. So:

    every candidate must quote the sentence it came from, verbatim,
    and the quote must actually appear in the source.

That check is a normalised substring test done by code, after the model has
spoken and before anything is stored. It is not a heuristic and cannot be
argued with. A model that paraphrases loses its candidate; a model that
invents one loses it too, because invented text is not in the source.

`tools/evidence.py` then checks the second gap -- whether the *claim* is
actually what the quote says, rather than a plausible expansion of it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from agent.models import MODELS, Role
from agent.provider import model_for, settings_for

# The user's own words, with the mail they answered cut away: "I'll get you
# the numbers Friday" in *their* text is their commitment, not the user's,
# and attributing it to the user is a fabrication with a real quote attached.
# One reader for this and the style guide: this one's own missed Outlook's
# From/Sent block, and cut an interleaved reply at its first quoted line.
from tools.style import own_words

log = logging.getLogger("extract")

# How much of a message the model sees. A commitment lives in the first
# paragraph or it is buried under a quoted thread, and a quoted thread is
# somebody else's words, which is precisely what must not be extracted.
MAX_BODY_CHARS = 2_000

_WHITESPACE = re.compile(r"\s+")


EXTRACT_PROMPT = """You are reading email the user sent, looking for things
they said they would do.

A commitment is a specific future action the user undertook: "I'll drop the
keys off Saturday", "I'll send you the photos by Friday", "I'll call you next
week".

These are NOT commitments:
 - anything somebody else undertook, including in text the user quoted
 - pleasantries: "looking forward to it", "let me know", "happy to help"
 - things already done by the time they wrote: "I've attached it"
 - conditionals with no undertaking: "I could send it if that helps"
 - vague intentions with no action: "I'll think about it"

For each one, return:
 - `text`: the action, in a few words, in the third person. "Send Dana the
   photos". Not a sentence, not a quote.
 - `quote`: the sentence from the message that contains it, copied EXACTLY,
   character for character. Do not tidy it, shorten it, or fix its spelling.
   A quote that does not appear verbatim in the message is discarded, and the
   commitment with it.
 - `due_date`: YYYY-MM-DD if they named or implied a date, otherwise null.
   Resolve "Friday" against the message's own date, not today's.

Return nothing rather than something marginal. An obligation the user does
not have is worse than one you missed, because they will act on it."""


class Candidate(BaseModel):
    text: str = Field(max_length=300)
    quote: str = Field(max_length=600)
    due_date: str | None = None


class Found(BaseModel):
    commitments: list[Candidate] = Field(default_factory=list)


@dataclass
class Checked:
    """One candidate, and whether it survived."""

    candidate: Candidate
    kept: bool
    reason: str


def _normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "")).strip().casefold()


def verify(candidate: Candidate, body: str) -> Checked:
    """Does the quote really appear, and does it really say the claim?

    Two different failures. A quote that is not in the message means the model
    wrote it; a quote that is in the message but does not support the claim
    means the model read more into it than it says. The first is caught by a
    substring test, the second by lexical grounding.
    """
    source = _normalise(own_words(body))
    quote = _normalise(candidate.quote)
    if not quote:
        return Checked(candidate, False, "no quote")
    # Trailing punctuation is forgiven, and only trailing punctuation. A model
    # asked for "the sentence" routinely stops at a clause boundary and closes
    # it with a full stop -- "I'll send you the photos by Friday." where the
    # message reads "...by Friday, and I'll follow up". That is the model
    # being tidy, not the model inventing, and refusing it would throw away
    # most of what this finds while catching nothing.
    if quote not in source and quote.rstrip(" .,;:") not in source:
        return Checked(candidate, False, "quote is not in the message")

    from tools.evidence import DIRECT, INFERRED, Source, ground_claims

    report = ground_claims(
        [candidate.text], [Source(url="message", text=candidate.quote)]
    )
    label = report.verdicts[0].label if report.verdicts else "unsupported"
    if label not in (DIRECT, INFERRED):
        return Checked(candidate, False, f"claim not supported by its own quote ({label})")

    if candidate.due_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate.due_date):
        # A malformed date would sort wrongly forever rather than fail loudly.
        candidate.due_date = None
    return Checked(candidate, True, "ok")


def _agent():
    from pydantic_ai import Agent

    return Agent(
        model_for(Role.BULK),
        output_type=Found,
        system_prompt=EXTRACT_PROMPT,
        model_settings=settings_for(MODELS[Role.BULK]),
        retries=2,
    )


async def from_message(message: dict) -> tuple[list[Checked], float]:
    """Read one sent message. Returns every candidate with its verdict."""
    from agent.llm import run_agent

    body = own_words(message.get("body") or "")[:MAX_BODY_CHARS]
    if len(body.split()) < 10:
        return [], 0.0

    when = message.get("date") or date.today().isoformat()
    prompt = (
        f"Message sent {when}\n"
        f"To: {message.get('to') or 'unknown'}\n"
        f"Subject: {message.get('subject') or '(none)'}\n\n{body}"
    )
    result, record = await run_agent(_agent(), prompt, Role.BULK)
    cost = getattr(record, "cost_usd", 0.0) or 0.0
    return [verify(c, body) for c in result.output.commitments], cost
