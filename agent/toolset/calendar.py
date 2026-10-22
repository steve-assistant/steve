"""Their calendars: every account's read as one (`agent/calendars.py`), and
events added, moved and deleted on the account they are on, through the
connector slot (`agent/connectors.py`)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging

from pydantic_ai import RunContext

from agent import calendars, connectors, offloop, zones
from agent.actions import act, executable
from agent.approvals import one_line
from agent.connectors import Connected, NoAccount
from agent.toolset import oncard
from agent.toolset.clients import commitment_store
from agent.turn import Deps
from core import writes
from steve_sdk import classes
from steve_sdk.classes import Event, Guest
from steve_sdk.errors import ConnectorError

log = logging.getLogger("toolset.calendar")

# What the model reads when the create is stopped. The way out is spelled
# out because the wrong one is what it reached for on 6 October 2026: with
# a duplicate made and no way to delete it, it renamed the extra event
# "Fabrikam — Remove duplicate calendar entry" and left it on their calendar.
DUPLICATE = (
    "Not added: the user's calendar already has what looks like this event "
    "(already_on_calendar). If it is the same one, tell them it is there -- and "
    "if its time or title is wrong, correct it with update_event rather than "
    "adding another. Pass allow_duplicate=true only if they have said it is a "
    "separate event."
)
# Said for an event on an account Steve can read and not change: an
# invitation on a calendar the organisation keeps, for one.
READ_ONLY = (
    "That is on the user's {name} calendar, which can be read but not changed "
    "from here. Tell them to change it there."
)
# Said for an event with guests on an account that cannot tell them of a
# change (a calendar by password, 05-connectors.md, section 4): refused
# before the card, since the user's yes would only meet the account's no.
CANNOT_TELL = (
    "That event has guests, and the user's {name} calendar cannot tell them of "
    "a change from here. Tell the user to change it in their calendar's own app."
)
CANNOT_INVITE = (
    "The user's {name} calendar cannot invite anyone from here. Offer to add the "
    "event without guests, for them to invite people from their calendar's own app."
)


def _account_for(account: str) -> Connected:
    """The calendar a new event goes to: the one named, else the default
    (`calendars.default`). Raises NoAccount."""
    conn, owned = commitment_store()
    try:
        chosen = (connectors.named(conn, "calendar", account)[0] if account
                  else calendars.default(conn))
    finally:
        if owned:
            conn.close()
    if chosen is None:
        raise NoAccount("no connected calendar can add events")
    return chosen


def _moment(text: str, tz) -> dt.datetime:
    """An ISO time as written; with no zone, the user's own."""
    moment = dt.datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


def _listed(found: Connected, event: Event) -> dict:
    """A written event as the listings show it, and the calendar it is on."""
    shown = calendars.shown(found, event, tz=zones.local_zone(), default_key=found.key)
    return {**shown, "calendar": found.name}


def _guests(event: Event) -> list[str]:
    """Who a change to `event` emails: everyone on it but the user."""
    return [g.email for g in event.guests if g.email]


# What a card says that the model, or whoever sent the invitation, wrote --
# a title, a reason, the guests -- is on one line of its own after a label
# (`approvals.one_line`): with a line break, an event's title made its card's
# first line a routine's envelope and another card's answer line, and the
# card was sent whole (step 11's fourth review).


def _update_card(event: Event, args: dict, converted: str | None = None) -> str:
    changed = ", ".join(k for k in ("summary", "start", "end", "location") if args.get(k))
    guests = _guests(event)
    who = f", notifying {len(guests)} guest(s)" if guests else ""
    return (f"Change the event {one_line(event.title)!r}: {changed or 'nothing'}{who}"
            + (f"\n({converted})" if converted else ""))


def _delete_card(event: Event, why: str = "") -> str:
    tz = zones.local_zone()
    guests = _guests(event)
    who = (f"Cancels it for {len(guests)} guest(s): {one_line(', '.join(guests))}" if guests
           else "No guests")
    return (f"Delete {one_line(event.title)!r}\n{calendars._text(event.start, tz)} to "
            f"{calendars._text(event.end, tz)}\n{who}"
            + (f"\nWhy: {one_line(why)}" if why else ""))


@executable("create_event")
async def _do_create_event(
    *,
    summary: str,
    start: str,
    duration_minutes: int = 60,
    location: str | None = None,
    attendees: list[str] | None = None,
    allow_duplicate: bool = False,
    account: str = "",
) -> dict:
    found = None
    try:
        found = _account_for(account)
        connectors.require(found, classes.CREATE)
        if attendees:
            connectors.require(found, classes.GUESTS)
    except NoAccount as exc:
        return {"ok": False, "error": str(exc)}
    except ConnectorError as exc:
        return oncard.failed(found, exc)
    # Checked here rather than asked for in a prompt. The unprompted sweep
    # is *told* to search the calendar first, and on its first live run it
    # added "Studio Workshop #3" next to the "Studio Workshop 3" already
    # there. "Check before you act" is exactly the instruction a model drops
    # on the third item of a list, and a doubled calendar is the thing that
    # would make them turn the whole feature off. On 6 October 2026 it
    # searched first, by a title it had just made up, found nothing, and
    # doubled their Tailspin event anyway.
    #
    # A check that fails stops the create rather than skipping the check:
    # creating blind is how the doubled workshop happened.
    if not allow_duplicate:
        try:
            clash = await asyncio.to_thread(
                calendars.existing_like, found, summary, start,
                duration_minutes=duration_minutes)
        except Exception as exc:  # noqa: BLE001 - executors report, never raise
            log.warning("could not check the calendar before adding %r: %s", summary, exc)
            return oncard.failed(found, exc)
        if clash:
            return {"ok": False, "created": False, "kind": "usage",
                    "already_on_calendar": clash, "error": DUPLICATE}
    try:
        begins = _moment(start, zones.local_zone())
    except ValueError:
        return {"ok": False, "kind": "usage", "error": f"not a time: {start!r}"}
    event = Event(native_id="", title=summary, start=begins,
                  end=begins + dt.timedelta(minutes=duration_minutes or 60),
                  location=location or "",
                  guests=tuple(Guest(email=email) for email in attendees or ()))
    try:
        made = await connectors.call(found, lambda p: writes.create(p, event))
    except ConnectorError as exc:
        return oncard.failed(found, exc)
    return {"created": True, **_listed(found, made)}


@executable("update_event")
async def _do_update_event(
    *,
    event_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    shown: str | None = None,
    ident: str | None = None,
) -> dict:
    # `attendees` is who *will be told*, not who to add -- carried this far so
    # the gate could read it and so the ledger records who was emailed. The
    # change itself leaves the guest list alone.
    if summary is None and not start and not end and location is None:
        return {"ok": False, "error": "nothing to update", "kind": "usage"}
    args = {"event_id": event_id, "summary": summary, "start": start, "end": end,
            "location": location, "attendees": attendees, "shown": shown,
            **({"ident": ident} if ident else {})}
    tz = zones.local_zone()

    def change(event: Event) -> Event:
        if summary is not None:
            event.title = summary
        if start:
            event.start = _moment(start, tz)
        if end:
            event.end = _moment(end, tz)
        if location is not None:
            event.location = location
        return event

    from agent import approvals

    # Read here: the account's call runs in a thread of its own. Without a
    # card, a calendar that cannot write on condition needs Jev's verdict.
    verdict = approvals.free_verdict()
    said = await oncard.run(
        args, "event_id",
        lambda p, native, card, ident: writes.update(p, native, change, expected=ident,
                                                     card=card, verdict=verdict),
        lambda found, now: _update_card(now, args))
    if "done" not in said:
        return said
    return {"updated": True, **_listed(said["found"], said["done"])}


@executable("delete_event")
async def _do_delete_event(*, event_id: str, attendees: list[str] | None = None,
                           shown: str | None = None, ident: str | None = None,
                           why: str = "") -> dict:
    # Guests are told: deleting a meeting the user organised without telling them
    # leaves them waiting in it. The card said so before the user agreed, and
    # the provider tells whoever is on the event it deletes, not who the
    # arguments say.
    args = {"event_id": event_id, "attendees": attendees, "shown": shown,
            **({"ident": ident} if ident else {}), **({"why": why} if why else {})}
    said = await oncard.run(
        args, "event_id",
        lambda p, native, card, ident: writes.delete(p, native, expected=ident, card=card),
        lambda found, now: _delete_card(now, why))
    if "done" not in said:
        return said
    return {"deleted": True, "id": event_id, "calendar": said["found"].name}


LISTED_DESCRIPTION_CHARS = 500
LISTED_GUESTS = 10


async def _screened(conn, found: dict) -> dict:
    """A listing with each event someone else wrote as the injection screen
    allows (agent/screening.py): its title, place and description are
    theirs, screened as one item. One the screen did not pass keeps its
    time, its id and who is on it, and loses its words to what the screen
    allows. Their own events are their words. Descriptions reached the model
    unscreened until 11 October 2026."""
    from agent import screening

    theirs = [e for e in found.get("events") or []
              if isinstance(e, dict) and not e.get("organizer_self")]
    seen = await asyncio.gather(*(screening.shown(
        conn, "calendar_event", e.get("id"), e.get("organizer"),
        f"{e.get('summary') or ''}\n{e.get('location') or ''}\n\n{e.get('description') or ''}")
        for e in theirs))
    for event, shown in zip(theirs, seen, strict=True):
        if shown.how != "text":
            for key in ("summary", "location", "description"):
                event.pop(key, None)
            event.update(screened=shown.how, text=shown.text)
    return found


async def _screened_due(conn, found: dict) -> dict:
    """Due dates as the screen allows: a feed's titles and notes are written
    by whoever runs it (a course site, someone who shared a calendar), never
    by the user. The personal edition's course-feed tool handed them to the
    model unscreened."""
    from agent import screening

    items = [i for i in found.get("items") or [] if isinstance(i, dict)]
    seen = await asyncio.gather(*(screening.shown(
        conn, "calendar_event", i.get("id"), None,
        f"{i.get('title') or ''}\n\n{i.get('about') or ''}") for i in items))
    for item, shown in zip(items, seen, strict=True):
        if shown.how != "text":
            item.pop("title", None)
            item.pop("about", None)
            item.update(screened=shown.how, text=shown.text)
    return found


def for_the_model(found: dict) -> dict:
    """A calendar listing with what the model would only read past left out.

    Over their last 119 calendar reads (6 October 2026) the biggest fields
    were the description, the guest list and Google's event link -- 18,000
    characters of links the user has never been given -- and every event repeated
    the zone the listing already states, `"status": "confirmed"`, and three
    false flags. What is left: what it is, when, where, who, its id to act
    on. A description is cut at 500 with its join links kept (agent/cut.py);
    the guests are named, the user's own reply apart, ten at most. Over those
    reads an event averaged 809 characters, and is 406."""
    from agent.cut import keeping_links

    events = []
    for event in found.get("events") or []:
        event = dict(event)
        if event.get("organizer_self"):
            event.pop("organizer", None)
        for key in ("link", "timezone", "organizer_self", "created", "updated", "series"):
            event.pop(key, None)
        for key in ("all_day", "recurring"):
            if not event.get(key):
                event.pop(key, None)
        if event.get("status") == "confirmed":
            event.pop("status")
        if not event.get("location"):
            event.pop("location", None)
        if event.get("description"):
            event["description"] = keeping_links(event["description"],
                                                  LISTED_DESCRIPTION_CHARS)
        else:
            event.pop("description", None)
        guests = event.pop("attendees", None) or []
        for guest in guests:
            if guest.get("self") and guest.get("response"):
                event["user_response"] = guest["response"]
        named = [
            f"{g.get('name') or g.get('email')}"
            + (f" <{g['email']}>" if g.get("name") and g.get("email") else "")
            + (f" ({g['response']})" if g.get("response") else "")
            for g in guests if not g.get("self")
        ]
        if named:
            event["guests"] = named[:LISTED_GUESTS] + (
                [f"+{len(named) - LISTED_GUESTS} more"] if len(named) > LISTED_GUESTS else [])
        events.append(event)
    return {**found, "events": events}


# What reading their mail looks like among a turn's tool calls.
MAIL_READS = frozenset({"read_email", "search_email", "email_thread"})

# Said when a calendar write names no email in a turn that read their mail. The
# way out is spelled out, as `DUPLICATE`'s is: "none" is a real answer.
NO_SOURCE = (
    "This turn read the user's email, and this one names no email it came from. Name the email "
    "it came from -- pass its id as from_message_id, and its date, time and zone are "
    "checked against what it says -- or say none: ask again with from_message_id=\"none\" "
    "if it comes from no email."
)


def _named_email(from_message_id: str | None) -> str | None:
    """The email an event says it came from, or None: nothing named, or the
    answer "none"."""
    named = (from_message_id or "").strip()
    return None if not named or named.lower() == "none" else named


def _where_from(ctx, tool: str, what: str, from_message_id: str | None) -> str | None:
    """Why a calendar write that names no email is refused -- once -- or None.

    The check against the email (`_not_in_email`) ran only when the model
    volunteered `from_message_id`, and the 6 October 2026 failure was the model
    not naming the email it had just read: a 4 pm taken for Eastern, from a
    confirmation that said MST. So a write made after this turn read their mail
    (`MAIL_READS`) and naming no email is sent back to say which, or say none;
    asked again with an answer, or with the same call, it goes through. Once
    per event, so a turn making three from one email is asked three times and
    a model that has decided is never held up twice (8 October 2026 audit)."""
    deps = ctx.deps
    if (from_message_id or "").strip():  # an email, or "none"
        return None
    if not MAIL_READS & set(getattr(deps, "tools_used", None) or ()):
        return None
    asked = getattr(deps, "asked_where_from", None)
    if asked is None:  # a stand-in for Deps that predates the field
        asked = deps.asked_where_from = set()
    key = f"{tool}:{what}"
    if key in asked:
        return None
    asked.add(key)
    return NO_SOURCE


async def _not_in_email(message_id: str, written: str, timezone: str | None) -> str | None:
    """Why an event starting at `written` (as given, in `timezone`) is not
    what the email it came from says -- or None (agent/provenance.py)."""
    from agent import provenance
    from agent.toolset.mail import read_any

    found = await asyncio.to_thread(read_any, message_id, 20_000)
    if not isinstance(found, dict) or found.get("error"):
        why = (found or {}).get("error") if isinstance(found, dict) else found
        return (f"Could not read that email to check the event against it ({why}). Read it "
                "with read_email and try again.")
    text = f"{found.get('subject') or ''}\n{found.get('body') or ''}"
    return provenance.refusal(text, dt.datetime.fromisoformat(written), timezone,
                              zones.local_zone())


def _user_time(start: str | None, timezone: str | None) -> tuple[str | None, str | None]:
    """`start` in the user's time, converted from `timezone` (agent/zones.py)."""
    if not start:
        return start, None
    return zones.in_user_time(start, timezone)


def register(tool) -> None:
    """Attach the calendar tools. `tool` is `register_tools`'s filter."""

    # Reading goes through `agent/calendars.py`, every calendar as one;
    # creating goes to the account named or the default, and changing to the
    # account an event's id names.

    @tool
    async def calendar_agenda(ctx: RunContext[Deps], days: int = 1) -> dict:
        """Upcoming events on the user's calendars, read as one.

        Times come back in the user's local timezone. Each event lists who
        else is in it (`guests`) when the calendar knows, and `user_response`
        when the user was invited; an event that is not on their main
        calendar names the one it is on (`calendar`). Some calendars can be
        read but not changed, and update_event says so. A list that had to
        stop says so in `cut`.
        """
        # Room for a week of classes: 25 events was a day's worth, and two
        # busy days filled it.
        limit = min(100, 25 * max(1, days))
        return for_the_model(await _screened(ctx.deps.conn, await offloop.with_own_conn(
            ctx.deps.conn, calendars.agenda, days, limit=limit)))

    @tool
    async def due_dates(ctx: RunContext[Deps], days: int = 14) -> dict:
        """What is due over the next `days`, from the calendars the user
        marked as due dates (a course site's feed, say): each with its due
        time, or its day, and the calendar it is on.

        A feed cannot say whether something was already handed in -- say
        that when it matters, rather than calling something outstanding.
        """
        return await _screened_due(ctx.deps.conn, await offloop.with_own_conn(
            ctx.deps.conn, calendars.due, days))

    @tool
    async def calendar_free(ctx: RunContext[Deps], date: str = "today") -> dict:
        """Gaps in a day's schedule, across the user's calendars. Date as
        YYYY-MM-DD, or "today".
        """
        return await offloop.with_own_conn(ctx.deps.conn, calendars.free_slots, date)

    @tool
    async def create_event(
        ctx: RunContext[Deps],
        summary: str,
        start: str,
        duration_minutes: int = 60,
        location: str | None = None,
        attendees: list[str] | None = None,
        timezone: str | None = None,
        allow_duplicate: bool = False,
        from_message_id: str | None = None,
        account: str = "",
    ) -> dict:
        """Create a calendar event.

        `start` is the time exactly as its source gives it, in ISO form, e.g.
        "2026-10-15T16:00". When the email, invitation or person names a zone
        ("4pm MST", "2pm Pacific", "15:00 CET"), pass it as `timezone` and do
        not convert the time yourself: it is converted to the user's here,
        and the result's `converted` says what it became -- tell them that.
        With no `timezone`, `start` is the user's own local time.

        Add only what you have the details for: a time you guessed is worse
        than asking. When the event comes from an email, pass its id as
        `from_message_id`: the date, the time and the zone are checked against
        what the email says, and a mismatch is refused with what it does say.
        If you read the user's mail this turn and this event comes from none
        of it, pass `from_message_id` as "none"; with no answer you are asked
        once. Something like it already on their calendar that day stops the
        create and comes back as `already_on_calendar`.

        An event with no guests is created immediately. One *with* guests is
        queued for the user to approve instead, because the invitation email
        is the part that cannot be taken back -- say what you queued and
        stop.

        `account` names the calendar to add it to, as a listed event's
        `calendar` names one; without it, the event goes on their main
        calendar.
        """
        if account:
            try:
                connectors.named(ctx.deps.conn, "calendar", account)
            except NoAccount as exc:
                return {"ok": False, "kind": "usage", "error": str(exc)}
        if attendees:
            # Before the card, as a change to an event with guests is checked:
            # a calendar by password invites no one, and the user's yes met
            # the account's refusal only when it ran (19 October 2026, step
            # 10's calendar scenario).
            try:
                found = _account_for(account)
            except NoAccount as exc:
                return {"ok": False, "kind": "usage", "error": str(exc)}
            if not found.provider.has(classes.GUESTS):
                return {"ok": False, "kind": "usage",
                        "error": CANNOT_INVITE.format(name=found.name)}
        written = start
        try:
            start, converted = _user_time(start, timezone)
        except ValueError as exc:
            return {"ok": False, "kind": "usage", "error": str(exc)}
        if named := _named_email(from_message_id):
            if wrong := await _not_in_email(named, written, timezone):
                return {"ok": False, "kind": "usage", "error": wrong}
        elif said := _where_from(ctx, "create_event", f"{summary.strip().lower()}|{written}",
                                 from_message_id):
            return {"ok": False, "kind": "usage", "error": said}
        args = {
            "summary": summary, "start": start,
            "duration_minutes": duration_minutes,
            "location": location, "attendees": attendees,
        }
        if allow_duplicate:
            args["allow_duplicate"] = True
        if account:
            args["account"] = account
        guests = one_line(", ".join(attendees)) if attendees else "no guests"
        when = (f"{one_line(start)} for {duration_minutes} min"
                + (f"\n({converted})" if converted else ""))
        # Every guest on the card, however many: cut at 300 characters, a
        # card inviting a dozen people showed the user about eight of them.
        result = await act(
            ctx, "create_event", args,
            preview=f"New event: {one_line(summary)}\n{when}\nInvites: {guests}"
                    + (f"\nOn their {one_line(account)} calendar" if account else ""),
        )
        if converted and isinstance(result, dict):
            result = {**result, "converted": converted}
        return result

    @tool
    async def calendar_search(
        ctx: RunContext[Deps], query: str, days_ahead: int = 365
    ) -> dict:
        """Find events by title, location or attendee, on the user's calendars.

        For "when is my dentist appointment" -- reaching a year out, where
        `calendar_agenda` only sees the next few days.
        """
        return for_the_model(await _screened(ctx.deps.conn, await offloop.with_own_conn(
            ctx.deps.conn, calendars.search, query, days_ahead=days_ahead)))

    @tool
    async def update_event(
        ctx: RunContext[Deps],
        event_id: str,
        summary: str | None = None,
        start: str | None = None,
        duration_minutes: int | None = None,
        location: str | None = None,
        timezone: str | None = None,
        from_message_id: str | None = None,
    ) -> dict:
        """Change an existing event. Only the fields you pass are touched.

        Moving an event that has guests emails every one of them, so that
        case is queued for the user rather than done. Changing one of their
        own private events happens immediately. You do not have to work out
        which -- the guest list is looked up here.

        `start` is ISO, read as the user's local time unless you pass the
        zone its source gave as `timezone` -- as for `create_event`, never
        convert it yourself; and a new time from an email is checked against
        it when you pass its id as `from_message_id` ("none" if you read the
        user's mail this turn and the new time is not from any of it). Pass
        duration_minutes to change the length.
        """
        try:
            found, native = connectors.by_id(ctx.deps.conn, event_id)
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        if not found.provider.has(classes.UPDATE):
            return {"ok": False, "kind": "usage", "error": READ_ONLY.format(name=found.name)}
        written = start
        try:
            start, converted = _user_time(start, timezone)
        except ValueError as exc:
            return {"ok": False, "kind": "usage", "error": str(exc)}
        if written:
            if named := _named_email(from_message_id):
                if wrong := await _not_in_email(named, written, timezone):
                    return {"ok": False, "kind": "usage", "error": wrong}
            elif said := _where_from(ctx, "update_event", f"{event_id}|{written}",
                                     from_message_id):
                return {"ok": False, "kind": "usage", "error": said}

        # Looked up rather than taken from the model. Whether this notifies
        # anyone is a property of the event, and asking the caller to supply
        # it would mean "move my 3pm" on a meeting with five guests classifies
        # as private -- correctly, on the arguments given, and wrongly in
        # every way that matters.
        try:
            existing = await connectors.call(found, lambda p: p.fetch(native))
        except ConnectorError as exc:
            return oncard.failed(found, exc)
        if existing is None:
            return {"ok": False, "kind": "not_found",
                    "error": f"there is no such event on {found.name} now; it may have been "
                             "moved or deleted"}
        if _guests(existing) and not found.provider.has(classes.GUESTS):
            return {"ok": False, "kind": "usage", "error": CANNOT_TELL.format(name=found.name)}

        end = None
        if duration_minutes or start:
            # A move keeps the event's length. Moved with a start alone, it
            # kept its old end: "move my 3pm to 5pm" became 5pm to 4pm, which
            # the calendar refuses as an empty range.
            with contextlib.suppress(ValueError, TypeError):
                length = (dt.timedelta(minutes=duration_minutes) if duration_minutes
                          else existing.end - existing.start)
                tz = zones.local_zone()
                base = _moment(start, tz) if start else existing.start
                # The user's wall clock, as `start` is written.
                end = ((base + length).astimezone(tz).replace(tzinfo=None)
                       .isoformat(timespec="minutes"))

        args = {
            "event_id": event_id, "summary": summary, "start": start,
            "end": end, "location": location, "attendees": _guests(existing),
            **oncard.bound(existing),
        }
        result = await act(ctx, "update_event", args,
                           preview=_update_card(existing, args, converted))
        if converted and isinstance(result, dict):
            result = {**result, "converted": converted}
        return result

    @tool
    async def delete_event(ctx: RunContext[Deps], event_id: str, why: str = "") -> dict:
        """Take an event off the user's calendar: one you added by mistake, a
        duplicate, or one they ask to remove. Never rename an event to mark
        it for deletion -- that leaves a stray entry on their calendar.

        Always queued for the user's yes, with the event and `why` on the
        card: say what you queued and stop. Any guests are sent the
        cancellation.
        """
        try:
            found, native = connectors.by_id(ctx.deps.conn, event_id)
        except NoAccount as exc:
            return {"ok": False, "error": str(exc)}
        if not found.provider.has(classes.DELETE):
            return {"ok": False, "kind": "usage", "error": READ_ONLY.format(name=found.name)}
        try:
            existing = await connectors.call(found, lambda p: p.fetch(native))
        except ConnectorError as exc:
            return oncard.failed(found, exc)
        if existing is None:
            return {"ok": False, "kind": "not_found",
                    "error": f"there is no such event on {found.name} now; it may be gone "
                             "already"}
        if _guests(existing) and not found.provider.has(classes.GUESTS):
            return {"ok": False, "kind": "usage", "error": CANNOT_TELL.format(name=found.name)}
        args = {"event_id": event_id, "attendees": _guests(existing),
                **oncard.bound(existing), **({"why": why} if why else {})}
        return await act(ctx, "delete_event", args, preview=_delete_card(existing, why))
