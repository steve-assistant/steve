"""Characters no one sees.

A card shows what a yes lets run, and the user decides by reading it. Text
holding a character no one sees shows one thing and is another: a
right-to-left override turns an address around, a zero-width space splits
a word that still reads whole, and tag characters spell out an instruction
that the card shows as nothing and that every run reads (step 11's
reviews).

Unicode's category C (control, format, unassigned, private use) was the
first check. The third review found what it missed: the default-ignorable
code points outside it, which a renderer shows as nothing -- the combining
grapheme joiner U+034F, the Hangul fillers U+115F, U+1160, U+3164 and
U+FFA0, the variation selectors -- and the Braille blank U+2800, which
shows as a space but is not one. Line breaks and tabs are seen, as breaks.
"""

from __future__ import annotations

import unicodedata

# Default_Ignorable_Code_Point, from Unicode's DerivedCoreProperties.txt,
# which `unicodedata` does not carry. Those in category C are caught by it
# too, and listed anyway so the list reads as the standard's.
_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
# Shown as a space, and not one: a word with it in reads as two.
_BLANKS = frozenset("⠀")
# Seen: they break the line, or indent it.
_SEEN_CONTROLS = frozenset("\t\n\r")


def unseen(ch: str) -> bool:
    """Whether `ch` is a character the eye does not see, or sees as
    something else."""
    if ch in _SEEN_CONTROLS:
        return False
    if ch in _BLANKS or unicodedata.category(ch).startswith("C"):
        return True
    point = ord(ch)
    return any(low <= point <= high for low, high in _IGNORABLE)


def first_unseen(text: str) -> str | None:
    """The first character in `text` that is not seen, named as Unicode
    names it, or None when every one is."""
    for ch in text or "":
        if unseen(ch):
            return f"U+{ord(ch):04X} {unicodedata.name(ch, 'unnamed')}"
    return None


def refusal(**parts: str | None) -> str | None:
    """Why text a model wrote for something that runs on its own cannot be
    kept, or None: the first part, by its name, holding a character no one
    sees. A routine's card showed its instruction without them, and each
    run read them all the same (step 11's third review)."""
    for name, text in parts.items():
        if found := first_unseen(text or ""):
            return (f"the {name} holds {found}, which is not shown: what the card cannot "
                    "show, the runs must not read. Write it again in plain text")
    return None
