"""Each account's health: five states, and one sentence for each.

On 26 September 2026 Google's sign-in lapsed at 23:37, and Gmail, Calendar
and Drive failed every minute all night in a log nobody read; the user found
out the next morning from a calendar request that came back as "my laptop is
offline". Each provider said its trouble its own way, or not at all, and the
app, the tools and chat each worded it again (05-connectors.md, section 2).

So every account is in one of five states, each with one action:

- **working**;
- **needs you**: sign in again, enter a new password, or check again;
- **can't reach**: retrying, since a given time;
- **can't work on this computer**, and why;
- **off**.

The sentence for each is made here, once. The control route returns it, and
the app, the tools' errors and chat all say it, naming the place in the app
where its one action is. A test holds that no other text says it.

The states are kept in `account_health` (migration 5), so "since 09:12"
survives a restart and what was said on the user's channel is said once.
Where the error named the page that fixes it (migration 6), that is kept
too, and "needs you" sends the user there.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core import clock, db

WORKING, NEEDS_YOU, CANT_REACH, CANT_WORK_HERE, OFF = (
    "working", "needs_you", "cant_reach", "cant_work_here", "off")
STATES = (WORKING, NEEDS_YOU, CANT_REACH, CANT_WORK_HERE, OFF)

# Where the user does what a sentence asks: the app's accounts page.
PLACE = "in Steve's app, under Settings: Accounts"

# What "needs you" asks, by the error behind it (steve_sdk.errors); any other
# error asks the user to check the account again. An MCP server whose tools
# changed since its test is stopped until the user tests it again, which
# pins the new list (`core/engines/mcp.py`).
_NEEDS = {"sign_in": "sign in again", "password": "enter its new password",
          "tools_changed": "test it again"}
# The app's button for each of those, beside the sentence (`action`).
_ACTS = {"sign_in": "sign_in", "password": "password", "tools_changed": "test"}
_STATE_OF = {"sign_in": NEEDS_YOU, "password": NEEDS_YOU, "transient": CANT_REACH,
             "not_connected": CANT_WORK_HERE, "tools_changed": NEEDS_YOU}
# The error kinds that are an account's state rather than one call's
# failure: `agent.connectors.say` says them as the account's health does.
KINDS = tuple(_STATE_OF)

# Said on the user's channel once: after this long in a state that is
# neither working nor off, and this long before a known sign-in's end.
DOWN_FOR = timedelta(hours=6)
ENDS_WITHIN = timedelta(days=1)

MAX_DETAIL = 200
MAX_FIX = 500  # a console page's address, with its project


def state_of(kind: str | None) -> str:
    """The state an error's kind puts an account in; None is working."""
    if kind is None:
        return WORKING
    return _STATE_OF.get(kind, NEEDS_YOU)


def _when(moment: datetime) -> str:
    local = moment.astimezone(clock.local_zone())
    return f"{local:%H:%M} on {local:%A} {local.day} {local:%B}"


def sentence(name: str, state: str, *, kind: str | None = None, detail: str = "",
             since: datetime | None = None, fix: str = "") -> str:
    """The one sentence for an account in `state`, with its one action."""
    if state == WORKING:
        return f"{name} is working."
    if state == NEEDS_YOU:
        action = _NEEDS.get(kind or "") or ("open the page that fixes it" if fix
                                            else "check it again")
        return f"{name} needs you: {action}, {PLACE}."
    if state == CANT_REACH:
        retrying = f" since {_when(since)}" if since else ""
        return f"{name} can't be reached; Steve keeps trying{retrying}."
    if state == CANT_WORK_HERE:
        why = (detail or "").strip().rstrip(".") or "it is not set up here"
        return f"{name} can't work on this computer: {why}."
    if state == OFF:
        return f"{name} is off, {PLACE}."
    raise ValueError(f"not a state: {state!r}")


def action(state: str | None, *, kind: str | None = None, fix: str = "") -> str:
    """The one action the app offers beside a state's sentence, decided as
    the sentence's own words are, so the button never asks for something
    the sentence did not: "sign_in", "password", "test" (check it again),
    "link" (open `fix`), "turn_on", or "" while it works. An account never
    tested (None) is offered its test."""
    if state is None or state == CANT_REACH:
        return "test"
    if state == NEEDS_YOU:
        return _ACTS.get(kind or "") or ("link" if fix else "test")
    if state == CANT_WORK_HERE:
        return "link" if fix else "test"
    if state == OFF:
        return "turn_on"
    if state == WORKING:
        return ""
    raise ValueError(f"not a state: {state!r}")


def ending(name: str, ends: datetime, now: datetime) -> str:
    """Said a day before a known sign-in's end."""
    hours = max(1, round((ends - now).total_seconds() / 3600))
    left = f"{hours} hour{'s' if hours != 1 else ''}"
    return (f"{name}'s sign-in ends in about {left}, at {_when(ends)}. "
            f"To keep it working, sign in again, {PLACE}.")


@dataclass(frozen=True)
class Health:
    account: str
    state: str
    kind: str | None
    detail: str
    since: datetime
    checked_at: datetime
    expires_at: datetime | None
    told: str | None
    fix: str = ""

    def says(self, name: str) -> str:
        return sentence(name, self.state, kind=self.kind, detail=self.detail,
                        since=self.since, fix=self.fix)

    def act(self) -> str:
        return action(self.state, kind=self.kind, fix=self.fix)


def _health(r: tuple) -> Health:
    return Health(account=r[0], state=r[1], kind=r[2], detail=r[3] or "",
                  since=clock.moment(r[4]), checked_at=clock.moment(r[5]),
                  expires_at=clock.moment(r[6]) if r[6] else None, told=r[7],
                  fix=r[8] or "")


_SELECT = ("SELECT account, state, kind, detail, since, checked_at, expires_at, told, fix "
           "FROM account_health")


def read(conn: sqlite3.Connection, key: str) -> Health | None:
    r = conn.execute(_SELECT + " WHERE account = ?", (key,)).fetchone()
    return _health(r) if r else None


def every(conn: sqlite3.Connection) -> dict[str, Health]:
    return {r[0]: _health(r) for r in conn.execute(_SELECT).fetchall()}


def record(conn: sqlite3.Connection, key: str, *, kind: str | None = None,
           state: str | None = None, detail: str = "", expires: datetime | None = None,
           fix: str = "", now: datetime | None = None) -> Health:
    """Record a check of account `key`: the error's `kind` (None if it
    worked), or a `state` outright (off), and the page that fixes it, kept
    only if it is https: the app opens it outside its window. `since` moves
    only when the state does, so it says how long the account has been as
    it is."""
    now = now or datetime.now(UTC)
    fix = fix if fix.startswith("https://") and len(fix) <= MAX_FIX else ""
    state = state or state_of(kind)
    if state not in STATES:
        raise ValueError(f"not a state: {state!r}")
    before = read(conn, key)
    since = before.since if before is not None and before.state == state else now
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO account_health (account, state, kind, detail, since, checked_at, "
            "expires_at, told, fix) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account) "
            "DO UPDATE SET state = excluded.state, kind = excluded.kind, "
            "detail = excluded.detail, since = excluded.since, "
            "checked_at = excluded.checked_at, expires_at = excluded.expires_at, "
            "fix = excluded.fix",
            (key, state, kind, (detail or "")[:MAX_DETAIL], clock.stamp(since),
             clock.stamp(now), clock.stamp(expires) if expires else None,
             before.told if before else None, fix or None))
    found = read(conn, key)
    assert found is not None
    return found


def due(health: Health, name: str, now: datetime) -> tuple[str, str] | None:
    """What to say of this account on the user's channel now, once, and the
    mark that makes it once: after `DOWN_FOR` in a state that is neither
    working nor off, and within `ENDS_WITHIN` of a known sign-in's end."""
    if health.state not in (WORKING, OFF) and now - health.since >= DOWN_FOR:
        mark = f"down:{clock.stamp(health.since)}"
        if health.told != mark:
            return mark, health.says(name)
    if (health.state == WORKING and health.expires_at is not None
            and timedelta(0) < health.expires_at - now <= ENDS_WITHIN):
        mark = f"ends:{clock.stamp(health.expires_at)}"
        if health.told != mark:
            return mark, ending(name, health.expires_at, now)
    return None


def told(conn: sqlite3.Connection, key: str, mark: str) -> None:
    with db.transaction(conn):
        conn.execute("UPDATE account_health SET told = ? WHERE account = ?", (mark, key))


def forget(conn: sqlite3.Connection, key: str) -> None:
    """Inside the caller's transaction: an account's health goes with it."""
    conn.execute("DELETE FROM account_health WHERE account = ?", (key,))
