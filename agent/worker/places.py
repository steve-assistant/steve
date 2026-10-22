"""The sidebar's places: Coming up, Extensions and Call notes, each with
its count, and the two pages only they open.

On 18 October a review of the running app found Steve's best work hidden
behind a chat box: reminders, notes on a call, and above all the
extensions Steve builds itself were things to know to ask for. Nothing on
screen said they were there, or how many. What holds now:

- the sidebar names each place under the days, with what it holds
  (`places`): reminders still to go off, extensions installed and any
  build under way, call notes kept. Read from the database alone: the app
  asks every five seconds while it is on screen;
- Coming up lists the reminders by when they go off, and the week's events
  (`coming`). The calendars are asked only while that page is open;
- Call notes lists the notes taken on calls and in rooms, each with the
  page it is on (`calls`), so a click opens that day at the note: the day
  stays the one place a note is read and deleted;
- whether Steve can build an extension here (`features.can_build`), so the
  app offers a build only where one could run.
"""

from __future__ import annotations

from datetime import UTC, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import calendars, features, offloop, runtime, sqltime
from agent.worker import journal
from core import clock

# Builds under way, as the Extensions page groups them (ui/src/Modules.tsx).
UNDER_WAY = ("running", "needs_allow", "stopped")
# The notes taken by listening: on a call, as the laptop played it, or in a
# room, through the microphone (agent/notes.py).
LISTENED = ("call", "lecture")
WEEK = 7


def _wall(now: datetime | None) -> str:
    """Now as a reminder's `next_fire_at` reads: the user's wall time."""
    moment = now or datetime.now(UTC)
    return moment.astimezone(clock.local_zone()).replace(tzinfo=None).isoformat(
        timespec="seconds")


def places(conn, now: datetime | None = None) -> dict:
    """Each place's count: reminders still to go off, extensions installed
    and building, call notes kept. The database only."""
    def one(sql: str, *args) -> int:
        return conn.execute(sql, args).fetchone()[0]

    return {
        "coming": one("SELECT COUNT(*) FROM reminders WHERE status = 'active'"
                      " AND next_fire_at >= ?", _wall(now)),
        "extensions": {
            "installed": one("SELECT COUNT(*) FROM modules WHERE origin != 'builtin'"),
            "building": one("SELECT COUNT(*) FROM module_builds WHERE state IN (?, ?, ?)",
                            *UNDER_WAY),
            "can_build": features.can_build(conn),
        },
        "calls": one("SELECT COUNT(*) FROM notes WHERE kind IN (?, ?)", *LISTENED),
    }


def coming(conn, now: datetime | None = None) -> dict:
    """The reminders still to go off, soonest first, those done on today's
    page, struck through as on a list kept by hand, and the week's events."""
    zone = clock.local_zone()
    reminders = [
        {"id": r["id"], "text": r["text"],
         "at": sqltime.iso(datetime.fromisoformat(r["next_fire_at"]).replace(tzinfo=zone))}
        for r in conn.execute(
            "SELECT id, text, next_fire_at FROM reminders WHERE status = 'active'"
            " AND next_fire_at >= ? ORDER BY next_fire_at LIMIT 50", (_wall(now),))]
    # Today's page, not the last 24 hours: what was struck off yesterday
    # evening is yesterday's, as the day's pages are kept.
    page_start, _ = journal.bounds(journal.page_of(now or datetime.now(UTC)))
    done = [
        {"id": r["id"], "text": r["text"], "at": sqltime.iso(sqltime.moment(r["finished_at"]))}
        for r in conn.execute(
            "SELECT id, text, finished_at FROM reminders WHERE status = 'done'"
            " AND finished_at >= ? ORDER BY finished_at LIMIT 50", (page_start,))]
    events = [
        {"summary": e.get("summary"), "start": e.get("start"), "end": e.get("end"),
         "all_day": bool(e.get("all_day")), "location": e.get("location")}
        for e in calendars.agenda(conn, WEEK, limit=30).get("events", [])]
    return {"reminders": reminders, "done": done, "events": events}


def calls(conn) -> list[dict]:
    """The notes taken by listening, newest first, each with its page."""
    out = []
    for r in conn.execute(
            "SELECT id, kind, title, status, started_at FROM notes WHERE kind IN (?, ?)"
            " ORDER BY started_at DESC LIMIT 100", LISTENED):
        started = sqltime.moment(r["started_at"])
        out.append({"id": r["id"], "kind": r["kind"], "title": r["title"],
                    "status": r["status"], "at": sqltime.iso(started),
                    "date": journal.page_of(started).isoformat(),
                    "key": f"listening:{r['id']}"})
    return out


async def get_places(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, **await offloop.with_own_conn(runtime.conn, places)})


async def get_coming(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, **await offloop.with_own_conn(runtime.conn, coming)})


async def get_calls(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True,
                         "calls": await offloop.with_own_conn(runtime.conn, calls)})


ROUTES = [
    Route("/control/places", get_places),
    Route("/control/coming", get_coming),
    Route("/control/calls", get_calls),
]
