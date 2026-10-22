"""Turning a written reply into something worth hearing.

The model writes for a screen: bold, headers, bullets, code fences, links,
emoji. Kokoro reads all of it literally, which was measured rather than
assumed by synthesising samples and transcribing them back:

    "**75F (24C)**"          -> "asterisk-esterisk 75 degrees F ... asterisk-esterisk"
    "Done ✅ 🎉"              -> "Done white heavy checkmark ... party popper"
    "## Summary"             -> "hash hash summary"
    "https://example.com/x"  -> "https slash slash example com slash x"
    "~0.04 cents"            -> "tilde 0.04 cents"

So this strips what is punctuation-for-the-eye and rewrites what has a sensible
spoken form. Only the audio path uses it; the transcript in the UI keeps the
markdown, because there the formatting is doing useful work.

Some things Kokoro already handles well and are deliberately left alone:
backticks are silent, `_underscores_` are silent, "°F" becomes "degrees F",
and "$0.00042" is read correctly.
"""

from __future__ import annotations

import re

# Emoji and pictographs. Kokoro names them aloud -- "party popper" -- which is
# never what the sentence meant.
_EMOJI = re.compile(
    "["
    "\U0001f300-\U0001f9ff"  # symbols, pictographs, emoticons, transport
    "\U0001fa00-\U0001faff"  # extended-A
    "\U00002600-\U000027bf"  # misc symbols and dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicators (flags)
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U00002190-\U000021ff"  # arrows
    "\U00002b00-\U00002bff"
    "♀♂⚕⚠✅❌❤"
    "]+",
    flags=re.UNICODE,
)

_FENCED_CODE = re.compile(r"```[\w+-]*\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BARE_URL = re.compile(r"https?://\S+")

_HEADER = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_LIST_MARKER = re.compile(r"^\s{0,6}([-*+]|\d{1,2}[.)])\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_HRULE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$", re.MULTILINE)

_BOLD_ITALIC = re.compile(r"(\*{1,3})(\S.*?\S|\S)\1", re.DOTALL)
_STRIKETHROUGH = re.compile(r"~~(\S.*?\S|\S)~~", re.DOTALL)

_TABLE_DIVIDER = re.compile(r"^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$", re.MULTILINE)

# "~5 minutes" reads as "tilde 5 minutes" unless rewritten.
_APPROX = re.compile(r"~(?=\d)")

_MULTISPACE = re.compile(r"[ \t]{2,}")
_MULTINEWLINE = re.compile(r"\n{3,}")

# Long enough that reading it aloud is worse than describing it.
_CODE_SPOKEN_LIMIT = 80


def _describe_code(match: re.Match[str]) -> str:
    """Replace a fenced block with a mention of it.

    Reading a code block aloud is almost never useful -- punctuation dominates
    and the listener cannot copy it anyway. It is on screen; say that it is.
    """
    body = match.group(1).strip()
    if not body:
        return " "
    lines = body.count("\n") + 1
    if len(body) <= _CODE_SPOKEN_LIMIT and lines == 1:
        return f" {body} "  # a one-liner is worth hearing
    return f" (a {lines}-line code block, shown on screen) "


def _describe_link(match: re.Match[str]) -> str:
    """Speak a markdown link's text, not its target."""
    return match.group(1)


def _describe_url(match: re.Match[str]) -> str:
    """Bare URLs become their domain.

    "https slash slash example dot com slash docs slash guide" is noise. The
    domain is the part a listener can act on; the full path is on screen.
    """
    url = match.group(0).rstrip(".,;:)")
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return f"link to {host}" if host else "a link"


def to_speech(text: str) -> str:
    """Rewrite markdown into something a TTS model should read aloud.

    Never returns markup for its own sake: if stripping leaves nothing, the
    caller gets an empty string and should stay silent rather than read
    punctuation.
    """
    if not text or not text.strip():
        return ""

    out = text

    # Code first: its contents must not be processed as prose.
    out = _FENCED_CODE.sub(_describe_code, out)
    out = _INLINE_CODE.sub(r"\1", out)

    # Links before emphasis, so a bolded link is not half-stripped.
    out = _MD_LINK.sub(_describe_link, out)
    out = _BARE_URL.sub(_describe_url, out)

    out = _HRULE.sub(" ", out)
    out = _TABLE_DIVIDER.sub(" ", out)
    out = _HEADER.sub("", out)
    out = _BLOCKQUOTE.sub("", out)

    # A bullet becomes a pause rather than vanishing, or a list runs together
    # into one breathless sentence.
    out = _LIST_MARKER.sub("", out)

    out = _STRIKETHROUGH.sub(r"\1", out)
    out = _BOLD_ITALIC.sub(r"\2", out)

    out = _EMOJI.sub(" ", out)
    out = _APPROX.sub("about ", out)

    # Table pipes read as "dollar" and similar noise; a comma paces it.
    out = re.sub(r"\s*\|\s*", ", ", out)

    # Any stray emphasis characters the paired patterns could not match.
    out = out.replace("**", "").replace("__", "")

    # Lines become sentences so the chunker can split on them, and so a list
    # is not spoken as a single run-on.
    lines = [line.strip() for line in out.split("\n")]
    rebuilt: list[str] = []
    for raw in lines:
        # Table rows arrive as ", Rate, Value," once the pipes became commas;
        # the outer ones are punctuation for a border that is not being read.
        line = raw.strip(" ,").strip()
        if not line:
            continue
        if line[-1] not in ".!?:;,":
            line += "."
        rebuilt.append(line)
    out = " ".join(rebuilt)

    out = _MULTISPACE.sub(" ", out)
    out = _MULTINEWLINE.sub("\n\n", out)
    out = re.sub(r"\s+([.,!?;:])", r"\1", out)
    out = re.sub(r"([.,!?;:]){2,}", r"\1", out)

    return out.strip()
