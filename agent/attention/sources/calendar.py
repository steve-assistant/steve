"""Their calendar, asked two different questions.

**What changed** (`changes`): invitations, moves and cancellations since
the last look. Asked with `updatedMin` rather than read off the agenda,
because the agenda is what the user already knows is coming -- asking it what is
new returned their entire timetable, twenty-odd lectures they scheduled
themselves, which is the opposite of news. Keyed by the event and its
`updated` stamp, so each change is one mention and an unchanged event is
none.

**What is coming** (`horizon`): the week ahead, so things can be prepared.
Almost everything worth preparing for is only visible from here -- a trip
is a calendar entry long before anybody writes about it. Each event gets two
looks, keyed by how near it is: once when it appears (`ahead`), once inside
two days (`soon`), which is the pass where looking up a forecast or the
people in the room stops being premature. A key without the pass would
mean "check nearer the time" never comes; the probe run that designed this
concluded exactly that about a flight. A recurring event -- a lecture --
gets only the `soon` look: its first appearance was months ago.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from agent.attention.sources import ok
from agent.attention.store import Observation

# How far ahead the horizon looks, and inside what the second look comes.
HORIZON_DAYS = 7
NEAR = dt.timedelta(hours=48)

# How far back `changes` asks. Longer than any cadence, so a laptop that
# slept through an invitation still finds it; the key keeps a change from
# being mentioned twice however often it is seen.
CHANGED_WITHIN_HOURS = 24

# The most people carried with an event for preparing (`people_in`).
MAX_PEOPLE = 8

# How long after a meeting it may still be asked about. Longer than the
# source's cadence, so a laptop asleep through the end still asks; past
# this, "how did it go?" arrives as an afterthought.
ENDED_WITHIN_HOURS = 3

# The judged follow-up (`settings.FOLLOW_UP_AFTER`): what the decision model
# is asked about a meeting that ended, and the probability it must give.
FOLLOW_UP_QUESTION = (
    "Should this person's assistant ask them how this meeting went, offering to help "
    "with what comes next? Yes when what was said there may need something from them "
    "afterwards -- a reply, something they promised, a decision -- or when they would "
    "want to note how it went. No when nothing follows from it.")
WORTH_ASKING_AT = 0.5


def _screened(conn: sqlite3.Connection | None, event: dict) -> dict:
    """An event as the judge may read it. One someone else wrote goes
    through the injection screen (agent/screening.py) -- its title, place
    and description are theirs, screened as one item, in place, since the
    sources run in a thread with a connection of their own. One the screen
    did not pass keeps its time and its people, and its description becomes
    what the screen allows (`_said`). Their own events are their words.
    Descriptions reached the judge unscreened until 11 October 2026."""
    if event.get("organizer_self"):
        return event
    from agent import screening

    shown = screening.shown_here(
        conn, "calendar_event", event.get("id"), event.get("organizer"),
        f"{event.get('summary') or ''}\n{event.get('location') or ''}\n\n"
        f"{event.get('description') or ''}")
    return event if shown.how == "text" else {
        **event, "summary": "An event (screened)", "location": None,
        "description": shown.text, "screened": shown.how}


def _said(event: dict, chars: int) -> str:
    """An event's description as the judge reads it: cut at `chars` -- but
    whole if it is what the screen allowed in its place, whose first lines
    only say that it is one."""
    said = str(event.get("description") or "")
    return said if event.get("screened") else said[:chars]


def _pass(start: str | None, now: dt.datetime | None = None) -> str:
    """`soon` inside two days, `ahead` beyond. Unknown counts as soon."""
    if not start:
        return "soon"
    try:
        when = dt.datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return "soon"
    now = now or (dt.datetime.now(when.tzinfo) if when.tzinfo else dt.datetime.now())
    return "soon" if (when - now) <= NEAR else "ahead"


def _where(event: dict) -> str:
    bits = []
    if event.get("location"):
        bits.append(f"at {event['location']}")
    guests = [a.get("email") for a in event.get("attendees") or []
              if a.get("email") and not a.get("self")]
    # An invitation to a whole organisation lists nobody, only a count
    # (`outlook_calendar.MAX_GUESTS_READ`) -- and "352 guests" is what says
    # it is not a meeting to prepare for.
    total = max(len(guests), int(event.get("attendee_count") or 0) - 1)
    if guests:
        shown = ", ".join(guests[:6]) + (f" +{total - 6}" if total > 6 else "")
        bits.append(f"with {shown}")
    elif total > 0:
        bits.append(f"{total} guests")
    # Not when it is them: every event on their calendar is copied in by them, so
    # "organiser me@..." told the judge nothing but "they created this", and
    # it filed their Northwind coffee chat with the routine events the user makes themselves.
    if event.get("organizer") and not event.get("organizer_self"):
        bits.append(f"organiser {event['organizer']}")
    if event.get("recurring"):
        bits.append("recurring")
    if event.get("all_day"):
        bits.append("all day")
    return "; ".join(bits)


def people_in(event: dict) -> list[str]:
    """Who the user will be with, as "Name <email>": the guests and the organiser,
    without them. Rooms and calendars are off the list already: each provider
    marks its own (`Guest.room`), and `calendars.shown` leaves them out.

    Capped, because the one kind of event with a hundred guests -- a lecture,
    a club mailing list invited wholesale -- is the kind where knowing each
    of them is not preparation.
    """
    from tools import people

    found: list[str] = []
    seen: set[str] = set()
    guests = [a for a in event.get("attendees") or [] if not a.get("self")]
    if event.get("organizer") and not event.get("organizer_self"):
        guests.append({"email": event["organizer"]})
    for guest in guests:
        email = (guest.get("email") or "").lower()
        if not email or email in seen or people.looks_automated(email, guest.get("name")):
            continue
        seen.add(email)
        found.append(f"{guest['name']} <{email}>" if guest.get("name") else email)
    return found[:MAX_PEOPLE]


def _ref(event: dict) -> dict:
    return {
        "event_id": event.get("id"),
        "start": event.get("start"),
        "end": event.get("end"),
        "location": event.get("location"),
        "all_day": bool(event.get("all_day")),
        "people": people_in(event),
        "link": event.get("link"),
    }


def _not_due(found: dict) -> list[dict]:
    """The events listed, less the things due on a calendar of due dates
    (`calendars.shown` marks them). Those are the deadlines source's, keyed
    by when they are due: read here too, a course site's assignment was a
    meeting "a few days out" to prepare for, and one that "just ended" at
    its deadline."""
    return [e for e in found.get("events") or [] if not e.get("due")]


def changes(conn: sqlite3.Connection) -> list[Observation]:
    from agent import calendars

    found = ok(
        calendars.changed(conn, since_hours=CHANGED_WITHIN_HOURS, limit=25), "calendar changes"
    )
    out = []
    for event in _not_due(found):
        event = _screened(conn, event)
        cancelled = event.get("status") == "cancelled"
        stamp = event.get("updated") or event.get("start") or ""
        out.append(
            Observation(
                source="calendar",
                key=f"calendar:{event.get('id')}:{stamp}",
                title=("CANCELLED: " if cancelled else "Changed or new: ")
                + str(event.get("summary") or "(untitled event)"),
                detail="; ".join(
                    part for part in (
                        _where(event),
                        f"created {event.get('created')}" if event.get("created") else "",
                        _said(event, 200),
                    ) if part
                ),
                when=event.get("start"),
                ref=_ref(event),
            )
        )
    return out


def _lasted(event: dict) -> str:
    """"lasted 15 min": with no guest list on their calendar, length is the
    clearest sign of a chat rather than a class. Given without it, the judge
    asked about their fifteen-minute Northwind chat on one run in two."""
    try:
        start = dt.datetime.fromisoformat(str(event.get("start")))
        end = dt.datetime.fromisoformat(str(event.get("end")))
    except ValueError:
        return ""
    minutes = int((end - start).total_seconds() // 60)
    return f"lasted {minutes} min" if minutes > 0 else ""


def worth_asking(event: dict, occupation: str = "", conn: sqlite3.Connection | None = None
                 ) -> tuple[bool, str]:
    """Whether to ask the user how `event` went, in the judged mode of
    `settings.FOLLOW_UP_AFTER`: Jev's yes (`core/decide.py`), and why, the
    ask billed on `conn`. Blocking; never raises.

    The judge's prompt used to decide it from a list of the meetings one
    life had in it -- a coffee chat, an interview, a recruiting event, a
    professor -- which asks a nurse or a retiree about none of theirs. The
    model is shown the meeting's shape and what the user does, as they put
    it: the title, how long it lasted, how many others were invited. Not
    its description, its guests or anything else of theirs.

    No key, or no answer, is no: a question nobody wanted costs more than
    one not asked.
    """
    from core.decide import Predicate, ask  # by name: agent/workers.py says why

    others = len([a for a in event.get("attendees") or [] if not a.get("self")])
    others = max(others, int(event.get("attendee_count") or 0) - 1)
    state = (f"What they do: {occupation or 'not said'}\n"
             f"Meeting: {' '.join(str(event.get('summary') or 'untitled').split())[:120]}\n"
             f"{_lasted(event) or 'length unknown'}; one-off, not recurring\n"
             f"Others invited: {others}")
    said = ask(conn, state, [Predicate("follow_up", FOLLOW_UP_QUESTION)])
    answer = said.answers.get("follow_up")
    if answer is None or answer.probability is None:
        return False, f"not asked: {said.error or 'no answer'}"[:200]
    p = answer.probability
    return p >= WORTH_ASKING_AT, f"a decision model gave it {p:.2f}"


def following_up(conn: sqlite3.Connection) -> bool:
    """Whether the user wants to be asked how meetings went
    (`settings.FOLLOW_UP_AFTER`), and it can be done, and so whether `after`
    looks at all and the judge reads its paragraph.

    The judged mode, the default, needs a decision model. Without one it
    looked at every meeting, asked about none, and left a paragraph in the
    judge's prompt about questions that never came; now it is off until
    there is one."""
    from agent.attention import settings

    if settings.FOLLOW_UP_AFTER == "judged":
        return can_judge()
    return settings.FOLLOW_UP_AFTER == "every"


def can_judge() -> bool:
    """Whether Jev is there to say a meeting is worth asking about
    (`worth_asking`): an OpenRouter key, which every install is set up
    with."""
    from core import secrets

    return bool(secrets.get("OPENROUTER_API_KEY"))


def after(conn: sqlite3.Connection) -> list[Observation]:
    """Meetings that just ended, so the user can be asked how they went:
    every one, those judged worth asking about (`worth_asking`; never a
    recurring one), or none, as they chose (`settings.FOLLOW_UP_AFTER`). One judged not worth it is
    recorded with why, and settled before the judge sees it
    (`vet.before_judging`), so it is asked about once.

    Asked, not acted on: a thank-you note needs what was said in the room,
    which only the user knows, so an `after` item never carries a task
    (`triage.ask_first`, pinned in the frozen gate test). The ping asks; a
    draft waits for their answer. One key per event and day, so a meeting
    is asked about once.
    """
    from agent import calendars, facts
    from agent.attention import settings
    from agent.attention.store import known

    if not following_up(conn):
        return []
    mode = settings.FOLLOW_UP_AFTER
    found = ok(calendars.ended(conn, since_hours=ENDED_WITHIN_HOURS), "calendar ended")
    events = [(f"after:{e.get('id')}:{str(e.get('start') or '')[:10]}", e)
              for e in _not_due(found)]
    seen = known(conn, [key for key, _ in events])
    occupation = facts.identity(conn)["occupation"] if mode == "judged" else ""
    out = []
    for key, event in events:
        if key in seen:
            continue
        event = _screened(conn, event)
        ref = _ref(event)
        if mode == "judged":
            # The same meeting every week is not news each week: settled
            # without asking the model, which is shown only one-off ones.
            worth, why = ((False, "a recurring meeting") if event.get("recurring")
                          else worth_asking(event, occupation, conn))
            ref["follow_up"] = "worth asking" if worth else "not worth asking"
            ref["follow_up_why"] = why
        out.append(
            Observation(
                source="after",
                key=key,
                title="Just ended: " + str(event.get("summary") or "(untitled event)"),
                detail="; ".join(
                    part for part in (
                        _lasted(event), _where(event), _said(event, 200),
                    ) if part
                ),
                when=event.get("end"),
                ref=ref,
            )
        )
    return out


def horizon(conn: sqlite3.Connection) -> list[Observation]:
    from agent import calendars

    found = ok(calendars.agenda(conn, HORIZON_DAYS, limit=50), "calendar agenda")
    out = []
    for event in _not_due(found):
        look = _pass(event.get("start"))
        if event.get("recurring") and look == "ahead":
            continue
        event = _screened(conn, event)
        out.append(
            Observation(
                source="horizon",
                key=f"horizon:{event.get('id')}:{look}",
                title=str(event.get("summary") or "(untitled event)"),
                detail="; ".join(
                    part for part in (
                        _where(event),
                        _said(event, 300),
                        "inside two days" if look == "soon" else "a few days out",
                    ) if part
                ),
                when=event.get("start"),
                ref=_ref(event),
            )
        )
    return out
