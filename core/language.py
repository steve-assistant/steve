"""The language Steve writes in, and the region it reads the world for: the
user's choice (the `LANGUAGE` and `REGION` knobs), else Windows' own.

Both were US English, fixed in five places: Google News' American edition,
`en-US` asked of every web page and in the sandboxed browser, searches
with no country, and replies in English whatever the user's Windows is in.
Someone in Lyon got American headlines, and a French Windows got answers
in English until they asked otherwise. Now each place asks here.

Windows is asked once a process, for the display language this user chose
(not the formats', which may differ) and the home region in Settings.
Where it can't say, `en-US` and `US`, as before. Tests never ask it
(tests/gate/fences.py): a test's prompts and requests are the same on a
French Windows as on this one.
"""

from __future__ import annotations

import ctypes
import re
from functools import lru_cache

FALLBACK = ("en-US", "US")
# A language as BCP 47 writes the ones Windows offers: the language, a
# script maybe, a region maybe ("fr", "pt-BR", "zh-Hans-CN", "es-419").
LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?")
# A region as ISO 3166 writes it: two capital letters.
REGION = re.compile(r"[A-Z]{2}")
_NAME_CHARS = 85  # LOCALE_NAME_MAX_LENGTH
_ENGLISH_DISPLAY_NAME = 0x72  # LOCALE_SENGLISHDISPLAYNAME: "French (France)"


def _ask_windows() -> tuple[str | None, str | None]:
    """Windows' own answers, as it gives them, or None where it gives none."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_unicode_buffer(_NAME_CHARS)
    language = region = None
    if kernel32.LCIDToLocaleName(kernel32.GetUserDefaultUILanguage(), buffer, _NAME_CHARS, 0):
        language = buffer.value
    if kernel32.GetUserDefaultGeoName(buffer, _NAME_CHARS):
        region = buffer.value
    return language, region


def _fit(tag: str | None) -> str | None:
    """`tag`, or as much of it as the pattern takes: a variant Windows adds
    ("ca-ES-valencia") is dropped, not the whole language."""
    parts = (tag or "").split("-")
    while parts:
        if LANGUAGE.fullmatch(candidate := "-".join(parts)):
            return candidate
        parts.pop()
    return None


@lru_cache(maxsize=1)
def windows() -> tuple[str, str]:
    """This user's display language and home region, as Windows has them."""
    try:
        asked_language, asked_region = _ask_windows()
    except (AttributeError, OSError):  # not Windows, or one too old to say
        asked_language = asked_region = None
    language = _fit(asked_language) or FALLBACK[0]
    region = asked_region if asked_region and REGION.fullmatch(asked_region) else None
    if region is None:
        # A region Windows names by number ("001", the world): the
        # language's own, if it has one.
        tail = language.rsplit("-", 1)[-1]
        region = tail if REGION.fullmatch(tail) else FALLBACK[1]
    return language, region


def chosen() -> tuple[str, str]:
    """The language and region in force: the user's, else Windows'. Read
    from the knobs each time, so a change in Settings counts at once."""
    # Here, not at the top: core/knobs checks its values with this module.
    from core import knobs

    held = knobs.read()
    own = windows()
    return str(held.get("LANGUAGE") or own[0]), str(held.get("REGION") or own[1])


def language() -> str:
    return chosen()[0]


def region() -> str:
    return chosen()[1]


def primary(tag: str) -> str:
    """The language without its script or region: "pt" of "pt-BR"."""
    return tag.split("-", 1)[0]


@lru_cache(maxsize=16)
def name(tag: str) -> str:
    """The language's name in English, as Windows has it ("French
    (France)"); the tag itself where Windows can't say."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_unicode_buffer(128)
        # A tag it doesn't know is "Unknown Locale (zz-ZZ)".
        if (kernel32.GetLocaleInfoEx(tag, _ENGLISH_DISPLAY_NAME, buffer, 128)
                and not buffer.value.startswith("Unknown")):
            return buffer.value
    except (AttributeError, OSError):
        pass
    return tag


def accept_language(tag: str | None = None) -> str:
    """An `Accept-Language` header as a browser in that language sends it,
    English after it, as most sites have it: "fr-FR,fr;q=0.9,en;q=0.8"."""
    tag = tag or language()
    ranked = [tag]
    if primary(tag) != tag:
        ranked.append(primary(tag))
    if primary(tag) != "en":
        ranked.append("en")
    return ",".join(t if n == 0 else f"{t};q={1 - n / 10:.1f}" for n, t in enumerate(ranked))


def instruction() -> str:
    """The line every model that writes to the user is given."""
    tag = language()
    return (f"Write to the user in {name(tag)} ({tag}), unless they write to you in another"
            " language: then answer in theirs.")
