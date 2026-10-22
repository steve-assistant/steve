"""A card's word is said by its card, and by nothing else.

A card on the phone ends "Reply YES MAPLE to go ahead", and a reply naming
MAPLE answers that card: the word binds the yes to what the card showed.
But the model's replies reach the phone too, and so do the notices that
routines, modules and servers write. Step 11's third review showed one
forging a card. A routine was asked for to someone else's address, and its
real card went out. Then the model's reply said "Each run of this routine
may, without asking: email: mom@example.com. Reply YES MAPLE to go ahead".
It came last, and the user's YES MAPLE approved the routine the real card
showed, which was not the one they read.

First, the model never reads the word of a card still waiting (`mask`).
Not being told it (`approvals._receipt`) was not enough, its fourth review
found: each card sent was written into the conversation, "Reply YES MAPLE"
and all, and the model read it on its next turn. So every request to a
model has the waiting words taken out of all it holds -- instructions,
history, the user's words, tools' results -- at the one place every model
client is built (agent/provider.Unworded), and a card is recorded in the
conversation without its word (`approvals._ask`). A model cannot say a
word it was never given, short of guessing one of the list.

Second, the word of a card still waiting is taken out of every other
message that leaves for a channel (`redact`): the model's replies
(agent/worker/answering.py), and all a phone is sent, in the form the
phone shows it, a card keeping its own word and no other's (`sendable`,
from channels/plaintext.shown). Three
lines the code writes keep it, because each is the card's whole first line
and its word, and nothing else: a list's line for a card that waits
(`listed`), the briefing's question about something still to do
(`question`), and the question back to a reply that may be about a card
(`which`). A line that only contains the first line is not enough: "Email
to Clara", then anything at all, then the word.

A word is found however it is dressed: in any case, in full-width or
mathematical letters, with accents, with Cyrillic or Greek letters that
look like Latin ones, with marks no one sees between its letters, or with
anything else between them or glued on that is not a letter: spaces,
dashes, bullets, a blank filler, Markdown's brackets and backticks, a
trade mark or a footnote's digit. What the user would read as the word is
the word. Letters of scripts that only look like Latin ones, past those
listed here, are not chased: once no model knows a word, what is left is
guessing it.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from core import paths, unseen

PLACEHOLDER = "[the card's word]"

# Letters of other alphabets that look like Latin ones, read as those. By
# code point: written out, each would look like the letter it stands for.
_LOOKALIKES = {
    # Cyrillic
    0x0430: "a", 0x0441: "c", 0x0501: "d", 0x0435: "e", 0x04BB: "h", 0x0456: "i",
    0x0458: "j", 0x043A: "k", 0x04CF: "l", 0x043E: "o", 0x0440: "p", 0x051B: "q",
    0x0455: "s", 0x051D: "w", 0x0445: "x", 0x0443: "y",
    0x0410: "a", 0x0412: "b", 0x0421: "c", 0x0415: "e", 0x041D: "h", 0x0406: "i",
    0x0408: "j", 0x041A: "k", 0x04C0: "l", 0x041C: "m", 0x041E: "o", 0x0420: "p",
    0x051A: "q", 0x0405: "s", 0x0422: "t", 0x051C: "w", 0x0425: "x", 0x0423: "y",
    # Greek
    0x03B1: "a", 0x03B5: "e", 0x03B9: "i", 0x03BA: "k", 0x03B7: "n", 0x03BF: "o",
    0x03C1: "p", 0x03C4: "t", 0x03C5: "u", 0x03BD: "v", 0x03C7: "x", 0x03B3: "y",
    0x0391: "a", 0x0392: "b", 0x0395: "e", 0x0397: "h", 0x0399: "i", 0x039A: "k",
    0x039C: "m", 0x039D: "n", 0x039F: "o", 0x03A1: "p", 0x03A4: "t", 0x03A7: "x",
    0x03A5: "y", 0x0396: "z",
    # Latin small capitals and the like, which have no decomposition
    0x1D00: "a", 0x0251: "a", 0x0299: "b", 0x1D04: "c", 0x1D05: "d", 0x1D07: "e",
    0x0261: "g", 0x0262: "g", 0x029C: "h", 0x026A: "i", 0x0131: "i", 0x1D0A: "j",
    0x1D0B: "k", 0x029F: "l", 0x1D0D: "m", 0x0274: "n", 0x1D0F: "o", 0x1D18: "p",
    0x0280: "r", 0xA731: "s", 0x1D1B: "t", 0x1D1C: "u", 0x1D20: "v", 0x1D21: "w",
    0x028F: "y", 0x1D22: "z",
}
# How many of what is not a letter may stand between a word's letters and
# leave it read as the word: "M A P L E", "M-A-P-L-E", "M•A•P•L•E".
_GAP = 3
# Characters Unicode counts as ignorable that show as a blank, as wide as a
# letter or a space: read as a gap, and sent as a space (`visible`). Read
# as nothing, "MAPLE\u2800to" was "mapleto" to the reading and "MAPLE to" on
# the phone (step 11's fourth review).
_FILLERS = frozenset("\u115f\u1160\u2800\u3164\uffa0")
# What no one sees but an emoji needs: the joiner of a family, the
# selectors that make a heart red. Kept in what is sent; read as nothing.
_GLUE = frozenset("\u200d\ufe0e\ufe0f")


@dataclass(frozen=True)
class Waiting:
    """A card still waiting for its yes: its word, its first line, and
    which card it is."""

    word: str
    shows: str
    id: int | None = None


def waiting(conn: sqlite3.Connection) -> list[Waiting]:
    """The cards a yes naming their word would answer now."""
    rows = conn.execute("SELECT token, preview, id FROM actions WHERE status = 'pending'"
                        " AND token IS NOT NULL").fetchall()
    return [Waiting(row[0], first_line(row[1]), row[2]) for row in rows]


def first_line(preview: str | None) -> str:
    """A card's first line: what a yes to it allows, whole."""
    return ((preview or "").splitlines() or [""])[0]


# The lines the code writes that say a waiting card's word. Each says the
# card's whole first line with it, never cut: a routine's first line spends
# fifty characters before whom it may reach, and cut at seventy, a yes to
# "email: mom@example.com, " approved zed@evil.example too (step 11's
# fourth review).

def listed(word: str, shows: str) -> str:
    """A list's line for a card that waits: its word, and its first line.
    The briefing's, and the answer to several cards or to "pending"
    (agent/approvals.py)."""
    return f"  {word}  {shows}"


def question(shows: str, word: str) -> str:
    """The briefing's question about something still to do, a card whose
    first line is the question (agent/closing.py)."""
    return f"{shows} YES {word} keeps it, NO {word} drops it."


def which(word: str, shows: str) -> str:
    """What a reply that may be about a card, but does not plainly answer
    it, is asked back (agent/approvals.py)."""
    return f"Is that about {word} -- {shows}? Reply YES {word} to go ahead or NO {word} to drop it."


def _read(ch: str) -> str:
    """What a reader takes `ch` for: lower-case letters, a space for a gap,
    or nothing for what is not seen or only marks a letter.

    Everything that is not a letter or a digit is a gap, however it looks:
    a list of what may stand between a word's letters was always one short
    (a bullet, a semicolon, a trade mark, Markdown's brackets left by a
    link with no address)."""
    if ch in _FILLERS:
        return " "
    if unseen.unseen(ch) or unicodedata.category(ch) in ("Mn", "Me"):
        return ""
    letters = unicodedata.normalize("NFKD", ch.translate(_LOOKALIKES))
    letters = "".join(c for c in letters if not unicodedata.combining(c))
    # A letter, a digit, or a sign that is one letter in another form (the
    # circled and parenthesised letters, a ligature like "ﬁ"); a footnote's
    # digit or a trade mark is not one letter, and reads as a gap.
    if unicodedata.category(ch).startswith("L") or ch.isdecimal() or (
            len(letters) == 1 and letters.isalpha()):
        return letters.lower()
    return " "


def folded(word: str) -> str:
    """`word`'s letters as the redaction reads them (`_read`), with no gaps:
    "maple" with a Cyrillic e, or with an accent on its a, is "maple"."""
    return "".join(_read(ch) for ch in word).replace(" ", "")


def visible(text: str) -> str:
    """`text` without what no one sees: a blank filler becomes a space, and
    the rest is dropped but for what an emoji needs (`_GLUE`). What is read
    and what is sent are then the same."""
    if not any(unseen.unseen(ch) for ch in text):
        return text
    return "".join(" " if ch in _FILLERS else ch for ch in text
                   if ch in _FILLERS or ch in _GLUE or not unseen.unseen(ch))


@lru_cache(maxsize=64)
def _pattern(word: str) -> re.Pattern:
    gap = f" {{0,{_GAP}}}"
    return re.compile(r"(?<!\w)" + gap.join(map(re.escape, word.lower())) + r"(?!\w)")


def _reading(line: str) -> tuple[str, list[int]]:
    """`line` as it is read (`_read`), and where in `line` each of its
    characters came from."""
    reading, at = [], []
    for i, ch in enumerate(line):
        for c in _read(ch):
            reading.append(c)
            at.append(i)
    return "".join(reading), at


def _own(line: str, card: Waiting) -> bool:
    """Whether `line` is one the code writes for the card, exactly: the
    briefing puts a bullet before its questions."""
    said = line.strip()
    said = said.removeprefix("· ")
    return said in (listed(card.word, card.shows).strip(), question(card.shows, card.word),
                    which(card.word, card.shows))


def _out(line: str, words: Iterable[str]) -> str:
    """`line` with each of `words` taken out, however dressed."""
    reading, at = _reading(line)
    spans = {(at[m.start()], at[m.end() - 1] + 1)
             for word in words for m in _pattern(word).finditer(reading)}
    for start, end in sorted(spans, reverse=True):
        line = line[:start] + PLACEHOLDER + line[end:]
    return line


def redact(text: str, cards: list[Waiting]) -> str:
    """`text` with each waiting card's word taken out, except on the lines
    the code writes for that card (`listed`, `question`, `which`)."""
    if not text or not cards:
        return text
    return "\n".join(_out(line, [c.word for c in cards if not _own(line, c)])
                     for line in text.split("\n"))


def mask(text: str, words: Iterable[str]) -> str:
    """`text` with each of `words` taken out wherever it is, the code's own
    lines too: what a model is given to read (agent/provider.Unworded)."""
    words = tuple(sorted({w.lower() for w in words if w}))
    if not text or not words:
        return text
    return _masked(text, words)


@lru_cache(maxsize=1024)
def _masked(text: str, words: tuple[str, ...]) -> str:
    # Cached: a conversation's history is sent again with each request of
    # a turn, and read character by character.
    return "\n".join(_out(line, words) for line in text.split("\n"))


def masked(value, words: Iterable[str]):
    """`value` with `mask` applied to each text in it, through lists,
    tuples and dicts: a tool's result as the model is given it."""
    if isinstance(value, str):
        return mask(value, words)
    if isinstance(value, (list, tuple)):
        return type(value)(masked(v, words) for v in value)
    if isinstance(value, dict):
        return {masked(k, words): masked(v, words) for k, v in value.items()}
    return value


def sendable(text: str, card: int | None = None) -> str:
    """`text` as a phone is sent it, already plain (channels/plaintext.shown),
    and seen as it is read (`visible`): each waiting card's word taken out
    but on the code's own lines for it. A card, by its id, keeps its own
    word, and has every other taken out wherever it is: the model wrote
    parts of it -- an event's title, an email's subject -- and sent whole,
    a title could put another card's word above the card's own (step 11's
    fourth review)."""
    if not text:
        return text
    cards = waiting_now()
    if card is not None:
        return mask(visible(text), [c.word for c in cards if c.id != card])
    return redact(visible(text), cards)


def waiting_now() -> list[Waiting]:
    """The cards waiting now: from this worker's store, or, in a process
    with none (the night, a scout), from the data folder's. A store that
    cannot be read raises, rather than letting a word through."""
    from agent import runtime

    if runtime.conn is not None:
        with contextlib.suppress(sqlite3.ProgrammingError):  # another thread's
            return waiting(runtime.conn)
    path = paths.db_path()
    if not path.exists():
        return []
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'actions'").fetchone():
            return []
        return waiting(conn)
    finally:
        conn.close()


def words_now() -> list[str]:
    """The words of the cards waiting now (`waiting_now`)."""
    return [card.word for card in waiting_now()]
