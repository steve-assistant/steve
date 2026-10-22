"""Deciding when the user hears, and how often.

The judge says what is worth saying; this says whether it goes out now.
All of it is code, because the question "have I interrupted them too much
today?" needs a memory the model does not have.

**One message per tick.** Everything ready at once goes out together: three
separate buzzes for one minute's news is what makes a notifier feel like an
interruption rather than a heads-up. The caps count messages, not items.

**The night has a higher bar, not a wall.** The user asked for this on 24
September: pings at night are fine for something important; calls are not
(those are `core/call_policy`'s, and unchanged). So between 22:00 and 08:00
only an item the judge marked critical goes out, at most
`MAX_NIGHT_PINGS` times a night; everything else waits for the morning
briefing, where it costs nothing. Critical after the checks in code
(`vet.py`): automated mail is never critical, an alert must be for where the user
is, and what is stale or corrected is dropped -- the hurricane warning for
Ridgeview, Alabama that woke them on 8 October would not pass them. The user,
10 October 2026: "only truly important stuff", a little looser than
emergencies code can verify.

**Checked again just before it is said** (`vet.recheck`): an item judged
hours ago may have passed, been corrected ("I'm in Pittsburgh") or closed
since. And every line sent is recorded with where it came from
(`told.record`), so "why did you tell me that?" has an answer.

**Late is worse than never, for some things.** A `now` item that could not
go out within `STALE_AFTER` -- the day's cap, or quiet hours -- becomes
briefing material instead of arriving hours late. A pre-event
brief for an event that has started is dropped.

**A send is not a delivery** (protocols §10). An item is marked delivered
once its message is in the outbox, which shows it in the app and retries
the phone until it goes (agent/outbox.py); its lines in `told` are said
only when the user has them.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agent.attention import settings, store, told, vet
from agent.attention.store import Observation

log = logging.getLogger("attention")

# Writes the message to the outbox; returns its row (agent/worker/links.py).
Notify = Callable[[str], Awaitable[int | None]]


@dataclass
class DeliveryResult:
    sent: list[str] = field(default_factory=list)
    held: int = 0
    demoted: int = 0
    dropped: int = 0
    skipped: str = ""

    def as_dict(self) -> dict:
        return {
            "sent": self.sent, "held": self.held, "demoted": self.demoted,
            "dropped": self.dropped, "skipped": self.skipped,
        }


def _minutes_until(when: str | None, now: dt.datetime) -> int | None:
    if not when or "T" not in when:
        return None
    try:
        start = dt.datetime.fromisoformat(when)
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.astimezone()
    return max(0, round((start - now.astimezone()).total_seconds() / 60))


def line_for(obs: Observation, now: dt.datetime) -> str:
    """One item, as the user reads it: the headline, then what was done."""
    head = obs.headline or obs.title
    if obs.say == "before":
        minutes = _minutes_until(obs.when, now)
        if minutes is not None:
            head = f"In {minutes} min: {head}"
    if obs.source == "after" and obs.ref.get("people"):
        # The ping joins the conversation (`note_said`), so when the user answers
        # "yes, thank her for the intro" the recipient is already in it.
        head += f"\n  with {', '.join(obs.ref['people'])}"
    if obs.outcome:
        # A meeting brief is up to three lines; each is indented under the
        # headline, or the second reads as a separate item.
        return head + "".join(f"\n  {line}" for line in obs.outcome.splitlines())
    return head


def compose(items: list[Observation], now: dt.datetime, *, at_night: bool = False) -> str:
    """One message for everything going out this tick."""
    if not items:
        return ""
    lines = [line_for(o, now) for o in items]
    body = lines[0] if len(lines) == 1 else "\n".join(f"· {line}" for line in lines)
    # Said so, because a buzz at 2am with no reason given reads as a fault.
    return f"Sorry for the hour -- this one can't wait.\n{body}" if at_night else body


def _window_start(now: dt.datetime, at_night: bool) -> dt.datetime:
    """Where the current day or night began, locally, for counting pings:
    the last time quiet hours ended, or began. The day was taken to begin
    at today's end of quiet hours, which for a night that ends in the
    afternoon is after the day it is counting."""
    from core import call_policy

    began = call_policy.QUIET_FROM if at_night else call_policy.QUIET_UNTIL
    start = now.replace(hour=began.hour, minute=began.minute, second=0, microsecond=0)
    return start if start <= now else start - dt.timedelta(days=1)


async def deliver(
    conn: sqlite3.Connection,
    *,
    notify: Notify | None = None,
    now: dt.datetime | None = None,
) -> DeliveryResult:
    """Send whatever is ready, within the caps. Never raises for a failed send.

    `notify` defaults to the worker's own; tests pass fakes.
    """
    if notify is None:
        from agent.worker.links import notify_user as notify

    now = now or dt.datetime.now()
    result = DeliveryResult()
    result.demoted = store.demote_stale(conn, int(settings.STALE_AFTER.total_seconds()))
    result.dropped = store.drop_missed(conn, int(settings.BEFORE_LEAD.total_seconds()))

    at_night = settings.within_quiet_hours(now)
    ready = store.to_say_now(conn) + store.before_due(conn)
    ready = _still_true(conn, ready, now, result)
    if at_night:
        going = [o for o in ready if o.critical]
        store.hold_for_morning(
            conn, [o.id for o in ready if o.id not in {g.id for g in going} and o.say == "now"]
        )
        cap = settings.MAX_NIGHT_PINGS
    else:
        going = ready
        cap = settings.MAX_DAY_PINGS
    result.held = len(ready) - len(going)
    if not going:
        return result

    used = store.pings_since(conn, _window_start(now, at_night), at_night=at_night)
    if used >= cap:
        result.held = len(ready)
        result.skipped = f"{'night' if at_night else 'daily'} ping limit reached ({used})"
        return result

    text = compose(going, now, at_night=at_night)
    try:
        message_id = await notify(text)
    except Exception as exc:  # noqa: BLE001 - the items stay ready for the next tick
        log.warning("could not write %d item(s) to the outbox: %s", len(going), exc)
        result.held = len(ready)
        result.skipped = "could not write it to the outbox"
        return result

    store.delivered(conn, [o.id for o in going if o.say == "now"], "ping")
    store.delivered(conn, [o.id for o in going if o.say == "before"], "before")
    store.pinged(
        conn, items=len(going), critical=any(o.critical for o in going),
        at_night=at_night, at=now,
    )
    result.sent = [line_for(o, now) for o in going]
    _record_told(conn, going, now, message_id)
    return result


def _still_true(conn: sqlite3.Connection, ready: list[Observation], now: dt.datetime,
                result: DeliveryResult) -> list[Observation]:
    """`vet.recheck`, failing open: a fault in the checks must not silence an
    emergency, so the items go on as they were, logged."""
    if not ready:
        return ready
    try:
        kept, dropped = vet.recheck(conn, ready, now)
    except Exception:  # noqa: BLE001 - see above
        log.exception("could not recheck %d item(s) before saying them", len(ready))
        return ready
    result.dropped += len(dropped)
    for obs, why in dropped:
        log.info("not said, no longer true: %s (%s)", obs.key, why)
    # An item checked for the first time here may have been made FYI
    # (automated mail): it is the briefing's now, not a ping's.
    return [o for o in kept if o.say in ("now", "before")]


def _record_told(conn: sqlite3.Connection, going: list[Observation], now: dt.datetime,
                 message_id: int | None) -> None:
    """What went out, each line with its provenance, said once the outbox
    row carrying it reaches the user. After the outbox has it, and never
    able to undo it: a failure here loses the record, not the ping."""
    try:
        stamp = told.now_stamp(now)
        for how in ("now", "before"):
            items = [o for o in going if o.say == how]
            if items:
                told.record(conn, f"{'ping' if how == 'now' else 'before'}:{stamp}",
                            "ping" if how == "now" else "before",
                            [told.of(o, line_for(o, now)) for o in items],
                            outbox_id=message_id)
    except Exception:  # noqa: BLE001 - see above
        log.exception("could not record what was said")
