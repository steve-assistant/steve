"""A time as its source wrote it, turned into the user's.

On 6 October 2026 a Tailspin confirmation said "October 15, 2026 04:00pm -
05:00pm MST", and the event went on their calendar at 4 pm Eastern, two hours
early. `create_event` took a single `start` in "the user's local time", so the
Mountain-to-Eastern sum was the model's, done in its head and checked by
nobody -- and earlier in the same conversation it had called 4 pm Eastern
"2-3 p.m. MDT", the sum run backwards. Converting a time is a lookup, not a
judgement. The model now passes the time exactly as written and the zone it
was written in, and this does the arithmetic, daylight saving included.

An abbreviation is read as its region: "MST" is Mountain time, not the
literal UTC-7. Event systems print a zone's standard name all year round,
and in October Denver -- where that Tailspin office is -- keeps MDT. Arizona,
which really is on MST all year, is named "Phoenix" or "America/Phoenix".
Ambiguous abbreviations ("IST": India, Israel or Ireland) are not guessed:
an unknown zone is refused, and the model asks or passes a city.
"""

from __future__ import annotations

import datetime as dt
import re
import zoneinfo
from functools import cache
from zoneinfo import ZoneInfo

from core import clock

# North America first, read as regions (above). "CST" is US Central, not
# China: these are where the user's contacts are.
_ABBREVIATIONS = {
    **dict.fromkeys(("ET", "EST", "EDT", "EASTERN"), "America/New_York"),
    **dict.fromkeys(("CT", "CST", "CDT", "CENTRAL"), "America/Chicago"),
    **dict.fromkeys(("MT", "MST", "MDT", "MOUNTAIN"), "America/Denver"),
    **dict.fromkeys(("PT", "PST", "PDT", "PACIFIC"), "America/Los_Angeles"),
    **dict.fromkeys(("AKT", "AKST", "AKDT", "ALASKA"), "America/Anchorage"),
    **dict.fromkeys(("HT", "HST", "HAWAII"), "Pacific/Honolulu"),
    **dict.fromkeys(("AT", "AST", "ADT", "ATLANTIC"), "America/Halifax"),
    **dict.fromkeys(("UTC", "GMT", "Z", "ZULU"), "UTC"),
    **dict.fromkeys(("BST", "UK"), "Europe/London"),
    **dict.fromkeys(("CET", "CEST"), "Europe/Paris"),
    **dict.fromkeys(("EET", "EEST"), "Europe/Athens"),
    "JST": "Asia/Tokyo", "KST": "Asia/Seoul", "SGT": "Asia/Singapore",
    "HKT": "Asia/Hong_Kong",
    **dict.fromkeys(("AEST", "AEDT"), "Australia/Sydney"),
    **dict.fromkeys(("NZST", "NZDT"), "Pacific/Auckland"),
}
# Words around a zone's name that say nothing about which zone it is:
# "Mountain Standard Time", "Eastern Time (US & Canada)".
_FILLER = frozenset({"TIME", "STANDARD", "DAYLIGHT", "SUMMER", "ZONE", "US", "CANADA",
                     "USA", "AND"})
# Cities the user is likely to see that are not themselves zone names.
_CITIES = {
    **dict.fromkeys(("san francisco", "seattle", "las vegas", "san diego", "bay area"),
                    "America/Los_Angeles"),
    **dict.fromkeys(("boston", "washington", "washington dc", "dc",
                     "philadelphia", "pittsburgh", "atlanta", "miami", "nyc"),
                    "America/New_York"),
    **dict.fromkeys(("dallas", "houston", "austin", "san antonio", "minneapolis",
                     "st louis"), "America/Chicago"),
    **dict.fromkeys(("salt lake city", "boulder", "colorado springs", "albuquerque"),
                    "America/Denver"),
    **dict.fromkeys(("montreal", "ottawa"), "America/Toronto"),
    **dict.fromkeys(("manchester", "edinburgh"), "Europe/London"),
    **dict.fromkeys(("munich", "frankfurt", "hamburg"), "Europe/Berlin"),
    **dict.fromkeys(("barcelona", "seville"), "Europe/Madrid"),
    **dict.fromkeys(("milan", "florence", "naples"), "Europe/Rome"),
    **dict.fromkeys(("mumbai", "bombay", "delhi", "new delhi", "bangalore", "bengaluru",
                     "chennai", "hyderabad"), "Asia/Kolkata"),
    **dict.fromkeys(("beijing", "shenzhen", "guangzhou"), "Asia/Shanghai"),
    **dict.fromkeys(("osaka", "kyoto"), "Asia/Tokyo"),
}
_OFFSET = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


@cache
def _by_city() -> dict[str, str]:
    """"denver" -> "America/Denver": the last part of every IANA name."""
    out: dict[str, str] = {}
    for name in zoneinfo.available_timezones():
        if "/" in name and not name.startswith(("Etc/", "SystemV/", "US/")):
            out.setdefault(name.rsplit("/", 1)[1].replace("_", " ").lower(), name)
    return out


def zone(name: str | None) -> dt.tzinfo | None:
    """The zone `name` means, or None when it is not one this can be sure of.

    Takes an IANA name ("America/Denver"), an abbreviation or region
    ("MST", "Mountain Time"), a UTC offset ("UTC-6", "-06:00") or a city
    ("Denver", "San Francisco")."""
    text = (name or "").strip()
    if not text:
        return None
    # The order matters. An IANA name first, but only one with a slash:
    # "MST" and "EST" are names in the zone database too -- fixed offsets,
    # never on daylight time -- and taken as such, 4 pm MST in October is 7
    # pm Eastern, not 6. And "Pacific/Honolulu" read as an abbreviation
    # would be California.
    if "/" in text:
        known = _iana(text) or _iana(text.replace(" ", "_"))
        if known:
            return known
    offset = _OFFSET.match(text.replace(" ", ""))
    if offset:
        sign, hours, minutes = offset.groups()
        delta = dt.timedelta(hours=int(hours), minutes=int(minutes or 0))
        if delta > dt.timedelta(hours=14):
            return None
        return dt.timezone(-delta if sign == "-" else delta)
    words = [w for w in re.findall(r"[A-Za-z]+", text.upper()) if w not in _FILLER]
    if words and words[0] in _ABBREVIATIONS:
        return ZoneInfo(_ABBREVIATIONS[words[0]])
    place = re.sub(r"[^a-z ]", "", text.lower().replace("_", " ")).strip()
    place = re.sub(r"\s+time$", "", place)
    found = _CITIES.get(place) or _by_city().get(place)
    return ZoneInfo(found) if found else _iana(text)


def _iana(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None


def local_zone() -> ZoneInfo:
    """The zone the user is in: `core.clock.local_zone`, asked there at each
    call, so that pinning that one pins this and core's count of the day."""
    return clock.local_zone()


def _clock(moment: dt.datetime) -> str:
    return moment.strftime("%a %d %b %I:%M %p").replace(" 0", " ")


def in_user_time(start: str, name: str | None) -> tuple[str, str | None]:
    """`start`, written in zone `name`, as the user's local time -- and a line
    saying what was converted, for the model to repeat rather than redo.

    Returns `start` untouched when no zone is given. A `start` that carries
    its own offset ("...T16:00-06:00") is converted from that, whatever
    `name` says: a number is more exact than a word. Raises ValueError for a
    zone it cannot place or a time it cannot read, so nothing lands at a
    guessed hour."""
    if not (name or "").strip():
        return start, None
    try:
        written = dt.datetime.fromisoformat(start)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cannot read {start!r} as an ISO time, like 2026-10-15T16:00") from exc
    source = written.tzinfo or zone(name)
    if source is None:
        raise ValueError(
            f"unknown timezone {name!r}: pass an IANA name such as America/Denver, "
            "or the city the time is given in")
    theirs = local_zone()
    there = written if written.tzinfo else written.replace(tzinfo=source)
    here = there.astimezone(theirs)
    local = here.replace(tzinfo=None).isoformat(timespec="minutes")
    if here.utcoffset() == there.utcoffset():
        return local, None
    label = getattr(source, "key", None) or str(source)
    return local, f"{_clock(there)} {label} is {_clock(here)} the user's time ({theirs})"
