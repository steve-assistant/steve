"""Is it in the email? A calendar entry made from an email, checked against it.

On 6 October 2026 the user signed up for a Tailspin networking session and asked
for it on their calendar, "check email for details". The agent did not find
the email, and added an event anyway, at a time it guessed. When it found
the email -- "October 15, 2026 04:00pm - 05:00pm MST" -- it added a second
event at 4 pm Eastern. Both times the event's time came from the model, and
nothing compared it with what the email said.

So the model names the email an event came from (`create_event`'s and
`update_event`'s `from_message_id`) and this reads it: the event's date must
be a date the email gives, its start a time the email gives, and when the
email gives that time in a zone other than theirs, the zone must have been
passed (agent/zones.py converts it) -- and be the same one.

What it does not do: decide anything the email leaves open. An email with no
date in it, or no time, checks nothing on that count; a relative day ("this
Thursday") is not a date. It refuses what the email contradicts, and lets
through what it cannot see.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

_MONTHS = {name: n for n, names in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
    ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
    ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
    ("dec", "december")), start=1) for name in names}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATES = [
    # "October 15", "Oct. 15th, 2026", "Thursday, October 15"
    (re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I), "month day"),
    # "15 October", "15th of Oct"
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH})\b", re.I), "day month"),
    # "2026-10-15"
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    # "10/15", "10/15/2026": theirs are American dates
    (re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])"), "us"),
]
_HALF = r"([ap])\.?\s*m\b\.?"
# "4-5 pm", "4:30 to 5:30pm": the start takes the end's half of the day.
_RANGE = re.compile(rf"\b(\d{{1,2}})(?::(\d{{2}}))?\s*(?:-|–|—|to|until)\s*"
                    rf"(\d{{1,2}})(?::(\d{{2}}))?\s*{_HALF}", re.I)
_TIME = re.compile(rf"\b(\d{{1,2}})(?::(\d{{2}}))?\s*{_HALF}|\b(\d{{1,2}}):(\d{{2}})\b|"
                   r"\b(noon|midday)\b", re.I)
# A zone named just after a time: "4pm MST", "04:00pm - 05:00pm MST",
# "2 p.m. (Mountain Time)", "16:00 CET", "3pm UTC-6".
_ZONE_AFTER = re.compile(
    r"^[\s\-–—to\d:apm.]{0,24}?\(?\s*(?P<zone>(?:[ECMP]|AK|H)[SD]?T|UTC\s*[+-]\s*\d{1,2}|GMT|"
    r"CES?T|BST|(?:Eastern|Central|Mountain|Pacific)(?:\s+(?:Standard|Daylight))?"
    r"(?:\s+Time)?)\b", re.I)


@dataclass(frozen=True)
class Said:
    """A time the email gives: the minutes past midnight it can mean (two,
    for a clock time with no am or pm), the zone named with it, and how the
    email wrote it."""
    minutes: frozenset[int]
    zone: str | None
    shown: str


def dates(text: str) -> set[tuple[int, int]]:
    """Every (month, day) the text gives."""
    found: set[tuple[int, int]] = set()
    for pattern, shape in _DATES:
        for m in pattern.finditer(text):
            if shape == "month day":
                month, day = _MONTHS[m.group(1).lower()], int(m.group(2))
            elif shape == "day month":
                month, day = _MONTHS[m.group(2).lower()], int(m.group(1))
            elif shape == "iso":
                month, day = int(m.group(2)), int(m.group(3))
            else:
                month, day = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                found.add((month, day))
    return found


def _minutes(hour: int, minute: int, half: str | None) -> frozenset[int]:
    if half is None:
        options = {hour % 24} if hour >= 13 or hour == 0 else {hour % 12, hour % 12 + 12}
    else:
        options = {hour % 12 + (12 if half.lower() == "p" else 0)}
    return frozenset(h * 60 + minute for h in options if 0 <= h < 24 and 0 <= minute < 60)


def times(text: str) -> list[Said]:
    """Every time the text gives, each with the zone named just after it."""
    out: list[Said] = []

    def zone_after(end: int) -> str | None:
        after = _ZONE_AFTER.match(text[end:end + 40])
        return " ".join(after.group("zone").split()) if after else None

    for m in _RANGE.finditer(text):
        start_h, start_m = int(m.group(1)), int(m.group(2) or 0)
        end_h, half = int(m.group(3)), m.group(5)
        # "11-1 pm" starts in the morning: the start is the other half.
        start_half = half if start_h % 12 <= end_h % 12 else ("a" if half.lower() == "p" else "p")
        out.append(Said(_minutes(start_h, start_m, start_half), zone_after(m.end()),
                        " ".join(m.group(0).split())))
    for m in _TIME.finditer(text):
        if m.group(6):
            minutes = frozenset({12 * 60})
        elif m.group(4):
            minutes = _minutes(int(m.group(4)), int(m.group(5)), None)
        else:
            minutes = _minutes(int(m.group(1)), int(m.group(2) or 0), m.group(3))
        if minutes:
            out.append(Said(minutes, zone_after(m.end()), " ".join(m.group(0).split())))
    return out


def _offset(name: str, when: dt.datetime) -> dt.timedelta | None:
    from agent import zones

    zone = zones.zone(name)
    return None if zone is None else when.replace(tzinfo=zone).utcoffset()


def refusal(text: str, start: dt.datetime, timezone: str | None,
            theirs: dt.tzinfo) -> str | None:
    """Why an event starting at `start`, as written in `timezone` (their own
    when None), is not what the email `text` says -- or None if it is, or if
    the email does not say."""
    start = start.replace(tzinfo=None)
    given = dates(text)
    if given and (start.month, start.day) not in given:
        shown = ", ".join(dt.date(start.year, m, d).strftime("%b %d").replace(" 0", " ")
                          for m, d in sorted(given)[:4])
        return (f"The email gives {shown}, not {start.strftime('%b %d').replace(' 0', ' ')}. "
                "Check the date, or leave it off the user's calendar.")
    said = times(text)
    if not said:
        return None
    at = start.hour * 60 + start.minute
    matching = [s for s in said if at in s.minutes]
    clock = start.strftime("%I:%M %p").lstrip("0")
    if not matching:
        shown = "; ".join(dict.fromkeys(s.shown + (f" {s.zone}" if s.zone else "")
                                        for s in said[:4]))
        return (f"The email gives {shown} -- not {clock}. Pass the start exactly as the email "
                "writes it, with its zone as `timezone` if it names one.")
    stated = next((s.zone for s in matching if s.zone), None)
    if stated is None:
        return None
    email_offset = _offset(stated, start)
    if timezone is None:
        if email_offset is not None and email_offset != start.replace(tzinfo=theirs).utcoffset():
            return (f"The email gives {clock} in {stated}, which is not the user's zone: pass "
                    f"timezone=\"{stated}\" with the start as written, and it is converted.")
        return None
    given_offset = _offset(timezone, start)
    if email_offset is not None and given_offset is not None and email_offset != given_offset:
        return f"The email gives that time in {stated}, not {timezone}."
    return None
