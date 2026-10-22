"""What is coming due, from the calendars the user marked as due dates.

The personal edition read one course site's feed, under that site's name
and a key of its own. This reads every calendar that declares due dates
(`classes.DUE_DATES`: a calendar link whose "due dates" box is ticked),
through `agent/calendars.due`. Things due only: a class session on the
same feed has a length, and the calendar source already has the user's
timetable.

Keyed by the item's id and its due moment, so a deadline that moves is
news again and the same one seen every few hours is not. A feed cannot say
whether anything was handed in, which the judge is told. Its titles and
notes are whoever runs the feed's, so each goes through the injection
screen, as a calendar event someone else wrote does; the course-feed
source passed them to the judge unscreened.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3

from agent.attention.sources import SourceError
from agent.attention.store import Observation

log = logging.getLogger("attention")

# How far ahead "coming due" reaches, in days: tomorrow's problem set is
# worth a word, next week's is the briefing's to mention when it is closer.
DUE_WITHIN_DAYS = 3


def _when(item: dict) -> str:
    """As a headline says it: "Fri 2 Oct, 8:00pm", or "Thu 1 Oct" all day."""
    text = str(item.get("due") or "")
    try:
        if item.get("all_day"):
            day = dt.date.fromisoformat(text)
            return f"{day:%a} {day.day} {day:%b}"
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return text
    clock = f"{moment:%I:%M%p}".lstrip("0").lower()
    return f"{moment:%a} {moment.day} {moment:%b}, {clock}"


def _screened(conn: sqlite3.Connection | None, item: dict) -> dict:
    from agent import screening

    shown = screening.shown_here(conn, "calendar_event", item.get("id"), None,
                                 f"{item.get('title') or ''}\n\n{item.get('about') or ''}")
    return item if shown.how == "text" else {
        **item, "title": "Something due (screened)", "about": shown.text,
        "screened": shown.how}


def due(conn: sqlite3.Connection) -> list[Observation]:
    from agent import calendars

    found = calendars.due(conn, DUE_WITHIN_DAYS)
    if "error" in found:
        raise SourceError(str(found["error"]))
    out = []
    for item in (_screened(conn, i) for i in found.get("items") or []):
        about = str(item.get("about") or "")
        out.append(Observation(
            source="deadlines",
            key=f"deadlines:{item.get('id')}:{item.get('due')}",
            title=f"{item.get('title') or '(untitled)'}, due {_when(item)}",
            detail=(f"Due on {item.get('calendar')}; the feed does not say whether it "
                    f"has been handed in. "
                    + (about if item.get("screened") else about[:200])).strip(),
            when=item.get("due"),
            ref={"url": item.get("link") or None, "calendar": item.get("calendar")},
        ))
    return out
