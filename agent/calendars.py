"""Every calendar the user has connected, read as one.

Invitations land on one of a user's calendars and their own copies on
another. Read from one alone, Steve had never seen a meeting that was only on
the other (24 September 2026), and knew the user's own copies of meetings
without anyone in them. So each reader here asks every calendar account
(`agent/connectors.py`) and counts an event on two of them once (`merge`):
the copy on an account that can change it, carrying the other's guests.

Until step 5 this merged one named service into another, and free time was
that service's own free/busy answer, so a user whose calendar was elsewhere
had none. Free time is now worked out here, from the events of every
account, and a new event goes to the account named or to the default.

When one calendar fails and another answers, the answer names the failure
in `problems` rather than being an error: "I could not reach one of your
calendars" and "you have nothing on" are different things to hear. An
account that is not on this computer is passed over, as mail search does.

Blocking: the accounts are read in turn, in this thread. Call from a thread
(`offloop.with_own_conn`).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from agent import connectors, zones
from agent.connectors import Connected
from agent.cut import keeping_links
from steve_sdk import classes
from steve_sdk.classes import Event
from steve_sdk.errors import ConnectorError, NotConnected

log = logging.getLogger("agent.calendars")

DESCRIPTION_CHARS = 1_000
# Asked of each account for a listing. Not every server lists in time order,
# so a few asked for could miss the morning; the merged list is cut after.
ASKED = 200

# Words that say what kind of thing an event is, not which one. Two titles
# are the same event only if they share what is left: "Northwind Coffee Chat" and
# "Fabrikam Coffee Chat" at the same hour are two chats, not one.
_GENERIC = frozenset({
    "the", "and", "for", "with", "of", "to", "in", "on", "at", "a", "an",
    "chat", "coffee", "meeting", "call", "sync", "event", "session", "invitation",
    "updated", "accepted", "required",
})
SAME_TITLE = 0.5


def _zone() -> ZoneInfo:
    return zones.local_zone()


def default(conn: sqlite3.Connection) -> Connected | None:
    """The account a new event goes to when none is named: the one the user
    marked, else the first that can add events (setup marks the first one
    it tests, `agent/accounts.py`)."""
    from core import modules

    writable = [found for found in connectors.accounts(conn, "calendar")
                if found.provider.has(classes.CREATE)]
    # Without a store (a tool run outside the worker) nothing can be marked.
    marked = {a.key for a in modules.accounts(conn, klass="calendar")
              if a.settings.get("default_calendar")} if conn is not None else set()
    return next((found for found in writable if found.key in marked),
                writable[0] if writable else None)


def _at(moment: dt.datetime | dt.date | None, tz: ZoneInfo) -> dt.datetime | None:
    """A start or end as an aware time; a whole day as its midnight."""
    if moment is None:
        return None
    if isinstance(moment, dt.datetime):
        return moment if moment.tzinfo else moment.replace(tzinfo=tz)
    return dt.datetime.combine(moment, dt.time(0), tzinfo=tz)


def _words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower())
            if len(w) > 1 and w not in _GENERIC}


def likeness(a: dict, b: dict) -> float:
    """How surely two listed events are one: 0 unless they start together,
    then how much of the shorter title the longer one shares.

    Not the end: hand-made copies do not keep it. "Tailspin First Year
    Presentations" is at 19:00 on two calendars and ends at different times
    on each, and required to match it was listed twice.
    """
    if bool(a.get("all_day")) != bool(b.get("all_day")):
        return 0.0
    tz = _zone()
    if _at(_parsed(a.get("start")), tz) != _at(_parsed(b.get("start")), tz):
        return 0.0
    left, right = _words(a.get("summary", "")), _words(b.get("summary", ""))
    if not left or not right:
        # Nothing but generic words ("Coffee Chat"): the same only if the
        # titles are the same.
        same = re.sub(r"\W", "", (a.get("summary") or "").lower()) == re.sub(
            r"\W", "", (b.get("summary") or "").lower())
        return 1.0 if same else 0.0
    return len(left & right) / min(len(left), len(right))


def _text(moment: dt.datetime | dt.date | None, tz: ZoneInfo) -> str | None:
    """As the user reads it: a moment on their clock, a whole day as its date."""
    if isinstance(moment, dt.datetime):
        return _at(moment, tz).astimezone(tz).isoformat()
    return moment.isoformat() if moment is not None else None


def shown(found: Connected, event: Event, *, tz: ZoneInfo, default_key: str | None) -> dict:
    """One event as the calendar tools list it, the same from every account:
    an id the editing tools take back to its account, and, for an account
    other than the default, which calendar it is on. On a calendar of due
    dates, a thing due is marked `due`: it takes no time, and is not a
    meeting to prepare for or ask about afterwards."""
    # A room is not someone the user will be with: the provider marks it
    # (`Guest.room`), as Outlook's module always left rooms off the list.
    attendees: list[dict[str, Any]] = [
        {"email": g.email or None, "name": g.name or None,
         "response": g.response or None, "self": False}
        for g in event.guests if not g.room]
    if mine := event.extra.get("my_response"):
        # The user's own answer, which a provider gives apart from the guests.
        attendees.append({"email": found.address or None, "name": None,
                          "response": mine, "self": True})
    out: dict[str, Any] = {
        "id": found.id(event.native_id), "summary": event.title or "(no title)",
        "start": _text(event.start, tz), "end": _text(event.end, tz),
        "timezone": str(tz), "all_day": event.all_day,
        "location": event.location or None,
        "description": keeping_links(event.description or "", DESCRIPTION_CHARS),
        "attendees": attendees, "organizer": event.organizer or None,
        "organizer_self": bool(event.extra.get("organizer_self")),
        "link": event.link or None, "status": event.status or None,
        "recurring": event.recurring,
    }
    if "guest_count" in event.extra:
        out["attendee_count"] = int(event.extra["guest_count"] or 0)
    for key in ("series", "updated", "created"):
        if event.extra.get(key):
            out[key] = event.extra[key]
    if found.key != default_key:
        out["calendar"] = found.name
    if found.provider.has(classes.DUE_DATES) and _takes_no_time(event):
        out["due"] = True
    return out


def _ask(conn: sqlite3.Connection, fn: Callable[[Any], list[Event]], *,
         only: Callable[[Connected], bool] | None = None
         ) -> tuple[list[tuple[Connected, list[Event]]], list[str], list[ConnectorError]]:
    """`fn(provider)` on every calendar account in turn: each that answered
    with its events, what went wrong with the others as sentences, and the
    errors themselves. With `only`, the accounts it keeps."""
    answered, problems, errors = [], [], []
    for found in connectors.accounts(conn, "calendar"):
        if only is not None and not only(found):
            continue
        try:
            answered.append((found, list(connectors.blocking(found, fn))))
        except NotConnected:
            log.debug("calendar %s is not on this computer", found.name)
        except ConnectorError as exc:
            log.warning("calendar %s: %s", found.name, exc)
            problems.append(connectors.say(exc, found.name))
            errors.append(exc)
        except Exception as exc:  # noqa: BLE001 - one account's fault is that account's
            log.exception("calendar %s failed", found.name)
            error = ConnectorError(f"{type(exc).__name__}: {exc}"[:300])
            problems.append(connectors.say(error, found.name))
            errors.append(error)
    return answered, problems, errors


def _within(events: list[Event], start: dt.datetime, end: dt.datetime,
            tz: ZoneInfo) -> list[Event]:
    """The events that overlap [start, end): a provider may list more."""
    kept = []
    for event in events:
        begins = _at(event.start, tz)
        ends = _at(event.end, tz) or begins
        if begins is not None and begins < end and ends > start:
            kept.append(event)
    return kept


def merge(answers: list[tuple[Connected, list[dict]]]) -> list[dict]:
    """Every account's listed events, an event on two counted once.

    The copy kept is the one on the first account that can change events --
    its id is the one the editing tools take -- given the other copy's
    guests when it has none: a hand-made copy of an invitation has none.
    Each account's events are matched only against those of the accounts
    before it, and each kept event once.
    """
    ordered = sorted(answers, key=lambda pair: not pair[0].provider.has(classes.UPDATE))
    out: list[dict] = []
    for found, events in ordered:
        before = list(out)
        for event in events:
            scored = [(likeness(event, kept), kept) for kept in before]
            best, twin = max(scored, key=lambda pair: pair[0], default=(0.0, None))
            if twin is None or best < SAME_TITLE:
                out.append(dict(event))
                continue
            before.remove(twin)
            twin["also_on"] = ", ".join(filter(None, (twin.get("also_on"), found.name)))
            if event.get("attendees") and not twin.get("attendees"):
                twin["attendees"] = event["attendees"]
                if "attendee_count" in event:
                    twin["attendee_count"] = event["attendee_count"]
                twin["organizer"] = event.get("organizer")
                twin["organizer_self"] = event.get("organizer_self", False)
    tz = _zone()
    latest = dt.datetime.max.replace(tzinfo=dt.UTC)
    out.sort(key=lambda e: _at(_parsed(e.get("start")), tz) or latest)
    return out


def _parsed(text: str | None) -> dt.datetime | dt.date | None:
    if not text:
        return None
    try:
        return (dt.datetime.fromisoformat(text) if "T" in text
                else dt.date.fromisoformat(text))
    except ValueError:
        return None


def _failed(problems: list[str], errors: list[ConnectorError]) -> dict:
    return {"error": " ".join(problems), "kind": errors[0].kind, "problems": problems}


def _listing(conn: sqlite3.Connection, fn: Callable[[Any], list[Event]], *,
             start: dt.datetime, end: dt.datetime, limit: int, asked: int,
             within: bool = True) -> dict:
    """The merged events `fn` finds on each account, cut at `limit` and said
    to be; an error only when no account answered and one failed. Without
    `within`, kept whatever their times: a deleted event may have none."""
    tz = _zone()
    answered, problems, errors = _ask(conn, fn)
    if errors and not answered:
        return _failed(problems, errors)
    chosen = default(conn)
    key = chosen.key if chosen else None
    merged = merge([(found, [shown(found, e, tz=tz, default_key=key)
                             for e in (_within(found_events, start, end, tz) if within
                                       else found_events)])
                    for found, found_events in answered])
    events = merged[:limit]
    out: dict[str, Any] = {"count": len(events), "events": events}
    # Said, not silent: a list cut at `limit` read as everything there was,
    # and "what's on this week" would have ended on Wednesday without a
    # word. An account that filled what was asked of it may have more.
    if events and (len(merged) > limit or any(len(e) >= asked for _, e in answered)):
        out["cut"] = (f"Only the first {len(events)}, up to {events[-1].get('start')}; "
                      "there may be more after. Ask for fewer days, or search.")
    if problems:
        out["problems"] = problems
    return out


def agenda(conn: sqlite3.Connection, days: int = 1, *, limit: int = 25) -> dict:
    """What is on every calendar from now, for `days` days."""
    tz = _zone()
    now = dt.datetime.now(tz)
    end = now + dt.timedelta(days=max(1, days))
    asked = max(limit, ASKED)
    found = _listing(conn, lambda p: p.events(now, end, limit=asked),
                     start=now, end=end, limit=limit, asked=asked)
    if "error" in found:
        return found
    return {"timezone": str(tz), "from": now.isoformat(), "to": end.isoformat(), **found}


def _takes_no_time(event: Event) -> bool:
    """On a calendar of due dates, a thing due is a moment or a day: it ends
    as it starts, or names no end. A timed event with a length is a session
    on that calendar (a class, a lab), not something due."""
    if event.all_day or event.end is None:
        return True
    return (isinstance(event.start, dt.datetime) and isinstance(event.end, dt.datetime)
            and event.end <= event.start)


def due(conn: sqlite3.Connection, days: int = 14, *, limit: int = 50) -> dict:
    """What is due from now, for `days` days, on the calendars the user
    marked as due dates (`classes.DUE_DATES`, a course site's feed, say):
    soonest first, a whole day's at its end, each with its calendar. Such a
    feed cannot say whether anything was handed in."""
    tz = _zone()
    now = dt.datetime.now(tz)
    end = now + dt.timedelta(days=max(1, days))
    answered, problems, errors = _ask(conn, lambda p: p.events(now, end, limit=ASKED),
                                      only=lambda found: found.provider.has(classes.DUE_DATES))
    if errors and not answered:
        return _failed(problems, errors)
    found_due = sorted(
        ((_at(e.end, tz) or _at(e.start, tz), found, e) for found, events in answered
         for e in _within(events, now, end, tz) if _takes_no_time(e)),
        key=lambda t: t[0])
    items = [{"id": found.id(e.native_id), "title": e.title, "due": _text(e.start, tz),
              "all_day": e.all_day,
              "calendar": found.name, "link": e.link,
              "about": " ".join(e.description.split())[:300]}
             for _, found, e in found_due[:limit]]
    out: dict[str, Any] = {
        "note": "Due dates from the calendars marked as such. They do not say whether "
                "anything has already been handed in.",
        "timezone": str(tz), "count": len(items), "items": items}
    if len(found_due) > limit:
        out["cut"] = f"Only the first {limit}; ask for fewer days."
    if problems:
        out["problems"] = problems
    return out


def finished(events: list[dict], now: dt.datetime, *, longest: dt.timedelta) -> list[dict]:
    """The listed events that are over: timed, no longer than `longest`, not
    cancelled.

    Each exclusion is a kind of event nobody asks "how did it go?" about:
    an all-day entry is a birthday or a deadline, a long one is a
    conference or a shift. A recurring one is kept: whether a weekly
    meeting is worth asking about is the user's choice
    (`settings.FOLLOW_UP_AFTER`), and "after every meeting" means every one.
    """
    out = []
    for event in events:
        if event.get("all_day") or event.get("status") == "cancelled":
            continue
        start, end = _parsed(event.get("start")), _parsed(event.get("end"))
        if not isinstance(start, dt.datetime) or not isinstance(end, dt.datetime):
            continue
        if start.tzinfo is None or end.tzinfo is None:
            continue
        if end <= now and dt.timedelta(0) < end - start <= longest:
            out.append(event)
    return out


def ended(conn: sqlite3.Connection, *, since_hours: int = 3, longest_hours: int = 4,
          limit: int = 50) -> dict:
    """Meetings on every calendar that ended in the last `since_hours`
    (`finished`): what "how did it go?" asks about."""
    tz = _zone()
    now = dt.datetime.now(tz)
    longest = dt.timedelta(hours=longest_hours)
    cutoff = now - dt.timedelta(hours=max(1, since_hours))
    start = cutoff - longest
    found = _listing(conn, lambda p: p.events(start, now, limit=ASKED),
                     start=start, end=now, limit=ASKED, asked=ASKED)
    if "error" in found:
        return found
    events = [e for e in finished(found["events"], now, longest=longest)
              if (_at(_parsed(e.get("end")), tz) or now) > cutoff][:limit]
    return {"timezone": str(tz), **found, "count": len(events), "events": events}


def changed(conn: sqlite3.Connection, *, since_hours: int = 24, days_ahead: int = 60,
            limit: int = 25) -> dict:
    """Events on every calendar added, moved or cancelled in the last
    `since_hours`, from now to `days_ahead` days out
    (`CalendarProvider.changed`). A different question from the agenda,
    which is what the user already knows is coming: asked what is new, it
    returns their whole timetable."""
    tz = _zone()
    now = dt.datetime.now(tz)
    since = now - dt.timedelta(hours=max(1, since_hours))
    end = now + dt.timedelta(days=max(1, days_ahead))
    found = _listing(conn, lambda p: p.changed(since, now, end, limit=limit),
                     start=now, end=end, limit=limit, asked=limit, within=False)
    if "error" in found:
        return found
    return {"timezone": str(tz), "since": since.isoformat(), **found}


def search(conn: sqlite3.Connection, query: str, *, days_ahead: int = 365,
           limit: int = 20) -> dict:
    """Events on every calendar whose title, place or guests hold each word
    of `query`, from now to `days_ahead` days out."""
    tz = _zone()
    now = dt.datetime.now(tz)
    end = now + dt.timedelta(days=max(1, days_ahead))
    words = [w for w in (query or "").lower().split() if w]
    found = _listing(conn, lambda p: p.search(words, now, end, limit=limit),
                     start=now, end=end, limit=limit, asked=limit)
    return found if "error" in found else {"query": query, **found}


def _busy(event: Event) -> bool:
    """Whether an event takes the time it spans: not a whole day, not
    cancelled, not marked free, and not one the user declined."""
    return (not event.all_day and event.status != "cancelled" and event.busy
            and event.extra.get("my_response") != "declined")


def free_slots(conn: sqlite3.Connection, date: str = "today", *, start_hour: int = 9,
               end_hour: int = 18, minimum_minutes: int = 30) -> dict:
    """The gaps of at least `minimum_minutes` in a day's working hours,
    across every calendar."""
    tz = _zone()
    try:
        # The user's today, not the process's: late in the evening the two
        # can disagree about the date.
        day = (dt.datetime.now(tz).date() if date in ("today", "")
               else dt.datetime.fromisoformat(date).date())
    except ValueError:
        return {"error": f"not a date: {date!r}; give YYYY-MM-DD, or today", "kind": "usage"}
    window_start = dt.datetime.combine(day, dt.time(start_hour), tzinfo=tz)
    window_end = dt.datetime.combine(day, dt.time(end_hour), tzinfo=tz)
    answered, problems, errors = _ask(
        conn, lambda p: p.events(window_start, window_end, limit=ASKED))
    if errors and not answered:
        return _failed(problems, errors)

    # Clipped to the window and sorted: the walk below assumes start order,
    # and a meeting at 19:00 would otherwise stretch the last gap past the
    # end of the day.
    periods = sorted(
        (max(_at(e.start, tz), window_start), min(_at(e.end, tz), window_end))
        for _, events in answered
        for e in _within(events, window_start, window_end, tz) if _busy(e) and e.end)
    slots: list[dict] = []
    cursor = window_start
    for begins, ends in [*periods, (window_end, window_end)]:
        if (begins - cursor).total_seconds() >= minimum_minutes * 60:
            slots.append({"start": cursor.astimezone(tz).isoformat(),
                          "end": begins.astimezone(tz).isoformat(),
                          "minutes": int((begins - cursor).total_seconds() // 60)})
        cursor = max(cursor, ends)
    out: dict[str, Any] = {"date": day.isoformat(), "timezone": str(tz), "free": slots}
    if problems:
        out["problems"] = problems
    return out


def _slug(title: str) -> str:
    """A title reduced to what it is about: "Studio Workshop 3" and "Studio
    Workshop #3" are the same workshop."""
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())


def existing_like(found: Connected, summary: str, start: str, *,
                  duration_minutes: int = 60) -> list[dict]:
    """Events already on `found`'s calendar that this one would duplicate.

    It used to want the same title at the same minute -- narrow on purpose,
    after an unprompted sweep re-added "Studio Workshop #3" -- and that let
    through both of the duplicates that have happened since:

    - **The same thing at the same time, under another name.** On 6 October
      2026 "Tailspin Denver Networking Session" at 16:00 was followed two
      minutes later by "Get to Know Tailspin Denver — Networking" at 16:00.
      Now: the times overlap and the titles share a word that says *which*
      event it is (`_words` drops "chat", "meeting" and the like, so
      "Northwind Coffee Chat" does not block "Fabrikam Coffee Chat").
    - **The same thing, the same day, at another time** -- a time zone got
      wrong and the event added again rather than moved. Now: the same day,
      and every distinctive word of the shorter title is in the longer.

    And the original, kept for titles made of nothing but generic words: the
    same title once punctuation is gone, at the same minute.

    Only the calendar it is going to: a hand-made copy, on one calendar, of
    an invitation on another is how a user works (`merge`), not a duplicate.

    It raises rather than answering a failure: an error would read as "already
    on their calendar". The executor that calls it reports the failure.
    """
    tz = _zone()
    try:
        begins = _at(dt.datetime.fromisoformat(start), tz)
    except (TypeError, ValueError):
        return []
    ends = begins + dt.timedelta(minutes=max(1, duration_minutes or 60))
    day = begins.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    until = max(day + dt.timedelta(days=1), ends)
    window = _within(connectors.blocking(found, lambda p: p.events(day, until, limit=50)),
                     day, until, tz)

    wanted, slug = _words(summary), _slug(summary)
    clashes = []
    for event in window:
        if event.status == "cancelled" or event.all_day:
            continue
        theirs = (_at(event.start, tz), _at(event.end, tz))
        if theirs[0] is None or theirs[1] is None:
            continue
        words = _words(event.title)
        shared = wanted & words
        overlapping = theirs[0] < ends and theirs[1] > begins
        if ((overlapping and shared)
                or (shared and len(shared) == min(len(wanted), len(words)))
                or (theirs[0] == begins and _slug(event.title) == slug)):
            clashes.append({"id": found.id(event.native_id),
                            "summary": event.title or "(no title)",
                            "start": _text(event.start, tz), "end": _text(event.end, tz)})
    return clashes
