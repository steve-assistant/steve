"""Cutting text short, and saying so.

Every `text[:N]` decides, silently, that the rest does not matter. In a log
line or a ledger excerpt it usually does not. It matters wherever a person
decides from what is left, or a model reasons from it, because then a silent
cut misleads:
- a card showed the first 180 characters of a Gmail email the user was asked to
  approve, with no sign that there was more;
- a call script the user approves by reading it ("the user approves a sentence they have
  read") lost its last line at 300 characters;
- a forwarded mail cut at 12,000 characters was handed to a model as if
  whole, and "the email does not mention a deadline" may only mean the
  deadline came after the cut.

So where it matters, text is cut here instead: at a word or a sentence
rather than mid-word, with a mark that says how much is missing, and never
longer than the limit it was given.
"""

from __future__ import annotations

import re

MARK = " […{n:,} more characters not shown]"
_LINK = re.compile(r"https?://[^\s<>\"'\])]+")


def cut(text: str | None, limit: int, *, mark: str = MARK, sentence: bool = False) -> str:
    """`text` whole if it fits in `limit` characters; otherwise as much as
    fits, ended at a word -- or at a sentence, with `sentence` -- and `mark`,
    whose `{n}` is how many characters were left out.

    Never backs off more than half the room to find a boundary: one
    unbroken 300-character URL is cut where it is rather than dropped.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    widest = mark.format(n=len(text))
    if len(widest) >= limit:
        return text[:limit]
    room = limit - len(widest)
    head = text[:room]
    floor = room // 2
    stop = -1
    if sentence:
        stop = max(head.rfind(end) for end in (". ", "! ", "? ", ".\n", "\n"))
        stop = stop + 1 if stop >= floor else -1
    if stop < 0 and not text[room].isspace():
        space = max(head.rfind(" "), head.rfind("\n"))
        stop = space if space >= floor else -1
    if stop >= 0:
        head = head[:stop]
    head = head.rstrip()
    return head + mark.format(n=len(text) - len(head))


def keeping_links(text: str | None, limit: int, *, most: int = 3) -> str:
    """`cut`, and after it the links from the part left out.

    What people look for at the bottom of a long description is the link:
    a Teams or Zoom invitation puts its join link under the agenda, so cut
    at 1,000 characters, "what's the link for my 3pm" would have been
    answered "there isn't one". The result can run past `limit` by those
    links.
    """
    text = text or ""
    shown = cut(text, limit)
    if shown == text:
        return text
    kept = []
    for link in _LINK.findall(text):
        link = link.rstrip(".,;:!?")
        if link not in shown and link not in kept:
            kept.append(link)
    if not kept:
        return shown
    return shown + "\nLinks further down: " + " ".join(kept[:most])
