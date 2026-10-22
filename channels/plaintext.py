"""Turning a written reply into something that reads well in a chat app.

Signal renders no markdown. Formatting is carried as separate style ranges on
the message, so every syntax character the model writes arrives verbatim: a
reply about the weather shows up on the phone as literal `**63°F**`.

This is a lighter transform than the speech one in `voice/speakable.py`, and
the difference is the point. Speech has to delete anything that is punctuation
for the eye -- bullets, URLs, emoji all read badly aloud. A chat app *is* an
eye, so structure stays: line breaks, bullets, emoji and links all render
properly on their own. Only the characters that would show up as themselves
get removed.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"^\s*```[\w+-]*\s*$", re.MULTILINE)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

# "[the forecast](https://…)" -> "the forecast (https://…)". Dropping the URL
# entirely would lose the only part a phone can act on.
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_LINK_OTHER = re.compile(r"\[([^\]]+)\]\([^)\s]*\)")

_HEADER = re.compile(r"^(\s{0,3})#{1,6}\s*", re.MULTILINE)
# A quote's "> ", which the app draws as a line down its side; a phone would
# show the angle brackets. A nested quote's are all taken off. Spaces, not
# `\s`, which would take the newline after a bare ">" and join two lines.
_QUOTE = re.compile(r"^([ \t]{0,3})(?:>[ \t]?)+", re.MULTILINE)
_HRULE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$", re.MULTILINE)

# Emphasis markers, innermost-first via the backreference. Bounded by
# non-space so "2 * 3 * 4" and a lone "*" survive untouched.
_EMPHASIS = re.compile(r"(\*{1,3})(\S|\S.*?\S)\1", re.DOTALL)
_STRIKETHROUGH = re.compile(r"~~(\S|\S.*?\S)~~", re.DOTALL)

# A bullet written with "*" is turned into "-": Signal shows either, but the
# asterisk is what the emphasis pass above has to leave alone, and a leading
# one is unambiguously a list.
_STAR_BULLET = re.compile(r"^(\s{0,6})\*\s+", re.MULTILINE)

_BLANK_RUN = re.compile(r"\n{3,}")


def to_plain_text(text: str) -> str:
    """Strip markdown syntax that a chat client would show as characters."""
    if not text:
        return ""

    out = _FENCE.sub("", text)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _MD_LINK.sub(r"\1 (\2)", out)
    out = _MD_LINK_OTHER.sub(r"\1", out)
    out = _HRULE.sub("", out)
    out = _HEADER.sub(r"\1", out)
    out = _QUOTE.sub(r"\1", out)
    out = _STAR_BULLET.sub(r"\1- ", out)
    out = _STRIKETHROUGH.sub(r"\1", out)
    # Twice: "**bold _and_ italic**" needs the inner pass before the outer.
    out = _EMPHASIS.sub(r"\2", out)
    out = _EMPHASIS.sub(r"\2", out)
    out = _BLANK_RUN.sub("\n\n", out)
    return out.strip()


def shown(reply) -> str:
    """What a phone is sent for `reply` (a channels.base.Outgoing), and all
    it is sent: plain text, made once, and then rid of every waiting card's
    word but a card's own and the code's own lines for one
    (agent/cardwords.sendable).

    The word was taken out of the text before Markdown was, so what Markdown
    left behind could spell it: "MA[PLE]()" was read as "MA", "PLE" and
    gaps, and sent as "MAPLE". Nor may anything convert it again: one pass
    turns "M``AP``LE" into "M`AP`LE", which a second would make "MAPLE"
    (step 11's fourth review). Imported here, not passed in, so no channel
    can be started without it."""
    from agent import cardwords

    return cardwords.sendable(to_plain_text(reply.text or ""), card=reply.card)
