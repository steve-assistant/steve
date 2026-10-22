"""What Steve may spend on models: asked in one place, written in one.

Until step 3, spend was counted in seven places, each with its own
`spent_today` over its own rows, and capped by four day budgets of background
work. Nothing capped a conversation or a month. The calls that billed
nothing -- the night's, the screen's, the coder's -- were invisible to all
seven. The night's own cap was held in memory and started again at a
restart. And all of it was code the night may change, so a change it made
could raise a budget or skip a count.

So the caps' defaults are here, frozen, as are the ledger's only writer
(`bill`) and the one question asked before a model call (`may_spend`). The
user may move a cap only inside its range (`core/knobs.py`), and this reads
their choice itself rather than taking it from the worker.

Each bill carries a category -- chat, background, the night, extensions --
and, inside one, a part. A part with a budget of its own (`PARTS`) is held
to it as well. The category comes from the unit of work the call is made in
(`spending`), opened where that work starts, rather than passed down thirty
signatures; the outermost unit decides it, so the night's research is the
night's, whatever code it runs. `asyncio.to_thread` and new tasks carry the
unit with them.

Today is the user's day, from their midnight in their zone (`core/clock`),
turned into UTC text so that the column's index is used. A night runs from
the start of quiet hours to the next, since a night crosses midnight.

A cap is told about twice at most: once spending reaches four fifths of it,
and once it refuses; the month's a third time between, with $1 left, to
ask whether to raise it (`MONTH_ASK`). Each is written down where it
happens -- the bill that crossed the line, the question that was refused --
once per cap, period and the cap's amount (`spend_notices`), so a cap raised
in the month is asked about and stopped at again; and each is passed on
later by whoever tells the user (`agent/caps.py`). Until step 3 a spent
budget said so from an in-memory set of days, so every restart said it
again, and nothing warned before.

OpenAI's own balance running out is written down the same way (`BALANCE`,
`ran_out`), though no cap of Steve's stopped anything: every client asks
OpenAI, so when its balance is spent nothing that needs a model can run,
the briefing's writer included, and the user heard only that a turn broke.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time as timer
from collections.abc import AsyncIterator, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from pydantic_ai.models import Model
from pydantic_ai.models.wrapper import WrapperModel

from core import call_policy, clock, knobs, models_guard

log = logging.getLogger("spend")

CATEGORIES = ("chat", "background", "night", "extensions")

# Each cap's default, by its knob; the knob holds its range. Zero stops that
# spending altogether.
DEFAULTS: dict[str, float] = {
    # Everything, from the first of the month. Setup asks for it, and
    # recommends a project key with its own budget in OpenAI's dashboard as
    # the first layer. $30 until 19 October 2026: the maintainer asked for
    # $15, with Steve asking a dollar before it whether to raise it
    # (agent/caps.py). An install that set its own keeps it.
    "MONTH_USD": 15.00,
    "CHAT_DAY_USD": 3.00,
    # Above its four parts' defaults ($2.35), so a part's budget binds first.
    "BACKGROUND_DAY_USD": 2.50,
    # The frozen policy's `coder.night_budget_usd` is its ceiling.
    "NIGHT_USD": 2.00,
    "EXTENSIONS_DAY_USD": 0.50,
    # Background work's four day budgets, from before step 3.
    "TRIAGE_BUDGET_USD": 0.10,
    "WORK_BUDGET_USD": 0.25,
    "ROUTINES_DAY_USD": 1.00,
    "GOALS_DAY_USD": 1.00,
}

# Each category's cap. The night's is per night, the rest per day.
CAPS = {"chat": "CHAT_DAY_USD", "background": "BACKGROUND_DAY_USD",
        "night": "NIGHT_USD", "extensions": "EXTENSIONS_DAY_USD"}
# The parts with a day budget of their own, counted in whatever category.
PARTS = {"triage": "TRIAGE_BUDGET_USD", "work": "WORK_BUDGET_USD",
         "routines": "ROUTINES_DAY_USD", "goals": "GOALS_DAY_USD"}
# Parts counted apart and capped elsewhere: the desk's live minutes
# (`DESK_LIVE_MIN`) and the model trials' month. And what a bill made in no
# unit is put down to, so that it shows.
UNCAPPED_PARTS = ("live", "trials")
UNATTRIBUTED = "unattributed"

# The share of a cap at which the user is warned, once.
WARN_AT = 0.8
# What is left of the month's cap when Steve asks whether to raise it: one
# message, while a reply can still reach a model. Asked unless the user
# chose not (`MONTH_ASK`, a choice of the Spend page's: a switch would be
# off by default, and the maintainer wants the ask on).
ASK_LEFT_USD = 1.00
ASK_DEFAULT = "yes"

# Not a cap: OpenAI's account balance, spent. A notice of its own, kind
# 'stopped', once a day while OpenAI keeps refusing (`ran_out`).
BALANCE = "OPENAI_BALANCE"


@dataclass(frozen=True)
class Unit:
    """A piece of work that spends: what its bills count against, and what
    they belong to."""

    category: str
    part: str | None = None
    session_id: int | None = None
    job_id: int | None = None
    message_id: int | None = None
    # The database its bills go to: the file of the connection the work was
    # started with, so a test's stays the test's. None for Steve's own.
    store: Path | None = None
    # What the user allowed this work past its category's cap, in the app:
    # a module build that would pass the day's `extensions` cap waits for
    # it (agent/modbuild.py). Never past the month's.
    allowance: float = 0.0


_unit: ContextVar[Unit | None] = ContextVar("spend_unit", default=None)


def current() -> Unit | None:
    """The unit of work this code runs in, if any."""
    return _unit.get()


@contextlib.contextmanager
def spending(category: str, *, part: str | None = None, conn: sqlite3.Connection | None = None,
             session_id: int | None = None, job_id: int | None = None,
             message_id: int | None = None, allowance: float | None = None) -> Iterator[Unit]:
    """Open a unit of work: what the model calls made inside it are billed
    to, in the database `conn` is open on. Inside another unit the category
    stays the outer one's, and the rest is this one's where given."""
    if category not in CATEGORIES:
        raise ValueError(f"no spending category {category!r}: one of {CATEGORIES}")
    if part is not None and part not in (*PARTS, *UNCAPPED_PARTS):
        raise ValueError(f"no spending part {part!r}")
    outer = _unit.get() or Unit(category)
    unit = Unit(
        category=outer.category,
        part=part if part is not None else outer.part,
        session_id=session_id if session_id is not None else outer.session_id,
        job_id=job_id if job_id is not None else outer.job_id,
        message_id=message_id if message_id is not None else outer.message_id,
        store=_file(conn) if conn is not None else outer.store,
        allowance=allowance if allowance is not None else outer.allowance,
    )
    token = _unit.set(unit)
    try:
        yield unit
    finally:
        _unit.reset(token)


def _file(conn: sqlite3.Connection) -> Path | None:
    """The file behind a connection; None for one in memory."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


@dataclass(frozen=True)
class Window:
    """A period a cap counts over."""

    since: str        # its start, as SQLite UTC text
    resets: datetime  # its end, in the user's zone
    period: str       # its name: "2026-10" for a month; a day's or a night's date


@dataclass(frozen=True)
class Windows:
    day: Window
    night: Window
    month: Window


def windows(now: datetime | None = None, *, quiet_from: time | None = None) -> Windows:
    """Today, tonight and this month, by the user's clock. A night opens
    when quiet hours start (`QUIET_FROM`) and is named by that date."""
    zone = clock.local_zone()
    here = (now or datetime.now(UTC)).astimezone(zone)
    opens = quiet_from or _quiet_from(knobs.read())
    today = here.date()

    def at(day: date, moment: time = time(0)) -> datetime:
        return datetime.combine(day, moment, tzinfo=zone)

    def window(start: datetime, end: datetime, period: str) -> Window:
        return Window(clock.stamp(start), end, period)

    night = today if at(today, opens) <= here else today - timedelta(days=1)
    first = today.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return Windows(
        day=window(at(today), at(today + timedelta(days=1)), today.isoformat()),
        night=window(at(night, opens), at(night + timedelta(days=1), opens), night.isoformat()),
        month=window(at(first), at(following), first.strftime("%Y-%m")),
    )


def _quiet_from(chosen: dict) -> time:
    given = chosen.get("QUIET_FROM")
    return time.fromisoformat(given) if given else call_policy.QUIET_DEFAULT[0]


def limits(chosen: dict | None = None) -> dict[str, float]:
    """Every cap as the user has set it, or its default."""
    chosen = knobs.read() if chosen is None else chosen
    return {name: float(chosen.get(name, default)) for name, default in DEFAULTS.items()}


# The caps a card may change (agent/toolset/spending.py, and "RAISE 25" at
# the month's cap): the month's and each category's, within their ranges.
# Every other knob is the user's alone, on its page. A card that could set
# any knob could lower what approving takes, or turn the quiet hours off.
SETTABLE = ("MONTH_USD", *CAPS.values())


def set_cap(cap: str, usd: float, path: Path | None = None) -> float:
    """Set one of the `SETTABLE` caps, as its page does, and return the
    value written. knobs.BadKnob, and nothing written, for any other knob
    or a sum outside the cap's range."""
    if cap not in SETTABLE:
        raise knobs.BadKnob(f"{cap!r} is not a spending cap a card may change")
    return float(knobs.write({cap: usd}, path)[cap])


def spent(conn: sqlite3.Connection, since: str, *, category: str | None = None,
          part: str | None = None) -> float:
    """What has been billed since `since` (SQLite UTC text): in all, or in
    one category or part."""
    sql, args = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_events WHERE created_at >= ?", [since]
    if category is not None:
        sql += " AND category = ?"
        args.append(category)
    if part is not None:
        sql += " AND part = ?"
        args.append(part)
    return float(conn.execute(sql, args).fetchone()[0] or 0)


class Refused(Exception):
    """A cap reached: which one, what it allows, what has gone, and the
    window it counts over."""

    def __init__(self, cap: str, limit: float, spent: float, window: Window) -> None:
        self.cap, self.limit, self.spent, self.window = cap, limit, spent, window
        super().__init__(f"{cap}: ${spent:.2f} of ${limit:.2f} spent, until"
                         f" {window.resets:%Y-%m-%d %H:%M}")


def out_of_credit(exc: BaseException) -> bool:
    """Whether a model call failed because the account's balance is spent,
    which no retry will change.

    OpenAI says so with a 429, the rate limit's status, told apart only by
    its code, so a retry rule that reads the status alone waits out every
    backoff for the same answer. Until 12 October 2026 models were also
    reached through OpenRouter, which said it with a 402; the coder's
    fourth live run resumed one twice, for nothing.

    pydantic-ai keeps the code in the error's body; the `openai` SDK's own
    errors, raised by the calls made without pydantic-ai, carry it as
    `code`."""
    from pydantic_ai.exceptions import ModelHTTPError

    if isinstance(exc, ModelHTTPError):
        return isinstance(exc.body, dict) and exc.body.get("code") == "insufficient_quota"
    return getattr(exc, "code", None) == "insufficient_quota"


def ran_out(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    """Write down that OpenAI refused a call because the account's balance
    is spent: once a day while it lasts, for agent/caps.py to pass on. The
    caller holds the transaction. Every client does this itself (`Guarded`);
    a call made without one does it where it catches the error."""
    _note(conn, "stopped", BALANCE, windows(now).day, 0.0, 0.0)


def window_for(cap: str, span: Windows) -> Window:
    """The period `cap` counts over."""
    if cap == "MONTH_USD":
        return span.month
    return span.night if cap == CAPS["night"] else span.day


def _checks(unit: Unit) -> list[tuple[str, str | None, str | None]]:
    """The caps a call in `unit` counts against, broadest first: the
    month's, its category's, and its part's if that has a budget."""
    checks = [("MONTH_USD", None, None), (CAPS[unit.category], unit.category, None)]
    if unit.part in PARTS:
        checks.append((PARTS[unit.part], None, unit.part))
    return checks


def may_spend(conn: sqlite3.Connection, *, unit: Unit | None = None,
              now: datetime | None = None, knob_path: Path | None = None) -> Refused | None:
    """None if a model call may be made now in `unit` (the current one by
    default), or the first cap it would go past: the month's, then its
    category's, then its part's. A refusal is written down, once per cap
    and period, to be passed on (`notices`)."""
    unit = unit or current() or Unit("background")
    chosen = knobs.read(knob_path)
    caps = limits(chosen)
    span = windows(now, quiet_from=_quiet_from(chosen))
    for cap, category, part in _checks(unit):
        window = window_for(cap, span)
        used = spent(conn, window.since, category=category, part=part)
        limit = caps[cap] + (unit.allowance if category is not None else 0.0)
        if used >= limit:
            # Written at the cap's own amount, not with this work's
            # allowance: amounts key the notices, and a build allowed past
            # the day's cap is the same cap stopping.
            _note(conn, "stopped", cap, window, caps[cap], used)
            return Refused(cap, limit, used, window)
    return None


def left(conn: sqlite3.Connection, category: str, *, now: datetime | None = None,
         knob_path: Path | None = None) -> float:
    """What `category` may still spend now before a cap stops it: the least
    left of the month's cap and its own."""
    chosen = knobs.read(knob_path)
    caps = limits(chosen)
    span = windows(now, quiet_from=_quiet_from(chosen))
    return max(0.0, min(caps[cap] - spent(conn, window_for(cap, span).since,
                                          category=cat, part=part)
                        for cap, cat, part in _checks(Unit(category))))


def _warn(conn: sqlite3.Connection, unit: Unit) -> None:
    """A warning for each cap the bill just written has taken to `WARN_AT`
    of itself or past it, and the month's ask once it is within
    `ASK_LEFT_USD` of its cap; each once per cap, period and amount. A cap
    of zero has none: that spending is off, and asking is refused."""
    chosen = knobs.read()
    caps = limits(chosen)
    span = windows(quiet_from=_quiet_from(chosen))
    asks = chosen.get("MONTH_ASK", ASK_DEFAULT) == "yes"
    for cap, category, part in _checks(unit):
        if caps[cap] <= 0:
            continue
        window = window_for(cap, span)
        used = spent(conn, window.since, category=category, part=part)
        if used >= WARN_AT * caps[cap]:
            _note(conn, "warned", cap, window, caps[cap], used)
        if cap == "MONTH_USD" and asks and used >= caps[cap] - ASK_LEFT_USD:
            _note(conn, "asked", cap, window, caps[cap], used)


def _note(conn: sqlite3.Connection, kind: str, cap: str, window: Window, limit: float,
          used: float) -> None:
    conn.execute("INSERT OR IGNORE INTO spend_notices (kind, cap, period, limit_usd, spent_usd)"
                 " VALUES (?, ?, ?, ?, ?)", (kind, cap, window.period, limit, round(used, 6)))


@dataclass(frozen=True)
class Notice:
    """A cap reached (`stopped`) or nearly (`warned`), or the month's with
    $1 left (`asked`), in one period, at the cap's amount then (`limit`)."""

    id: int
    kind: str
    cap: str
    period: str
    limit: float
    spent: float
    at: str               # when, as SQLite UTC text
    told_at: str | None   # when it was passed on; None until then


def notices(conn: sqlite3.Connection, *, caps: Iterable[str] | None = None,
            untold: bool = False, current_only: bool = False,
            now: datetime | None = None) -> list[Notice]:
    """The caps' notices, oldest first: only `caps`' if given, only those
    not yet passed on if `untold`, and only each cap's present period's if
    `current_only`."""
    sql, args = ("SELECT id, kind, cap, period, limit_usd, spent_usd, at, told_at"
                 " FROM spend_notices WHERE 1"), []
    if caps is not None:
        wanted = list(caps)
        sql += f" AND cap IN ({', '.join('?' * len(wanted))})"
        args += wanted
    if untold:
        sql += " AND told_at IS NULL"
    found = [Notice(*row) for row in conn.execute(sql + " ORDER BY id", args).fetchall()]
    if current_only:
        span = windows(now)
        found = [n for n in found if n.period == window_for(n.cap, span).period]
    return found


def told(conn: sqlite3.Connection, passed_on: Iterable[Notice]) -> None:
    """Mark notices passed on to the user, so no one tells them again."""
    ids = [n.id for n in passed_on]
    if ids:
        conn.execute(f"UPDATE spend_notices SET told_at = datetime('now')"
                     f" WHERE told_at IS NULL AND id IN ({', '.join('?' * len(ids))})", ids)


def bill(conn: sqlite3.Connection, cost_usd: float, *, model: str, effort: str | None = None,
         input_tokens: int = 0, cached_tokens: int = 0, output_tokens: int = 0,
         reasoning_tokens: int = 0, latency_ms: int | None = None, unit: Unit | None = None,
         session_id: int | None = None, message_id: int | None = None,
         job_id: int | None = None) -> int:
    """Write one model call's bill to the ledger, and return its row's id.
    The only writer of `usage_events`. The ids default to the unit's; the
    caller holds the transaction.

    A bill made in no unit is still written, as background work's, and
    logged: a call that billed nothing is how the night spent unseen."""
    unit = unit or current()
    if unit is None:
        log.warning("a model call made in no unit of work: %s, $%.6f", model, cost_usd)
        unit = Unit("background", UNATTRIBUTED)
    row = {
        "category": unit.category,
        "part": unit.part,
        "session_id": session_id if session_id is not None else unit.session_id,
        "message_id": message_id if message_id is not None else unit.message_id,
        "job_id": job_id if job_id is not None else unit.job_id,
        "model": model,
        "effort": effort,
        "input_tokens": int(input_tokens or 0),
        "cached_tokens": int(cached_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "reasoning_tokens": int(reasoning_tokens or 0),
        # Never below nothing: a negative bill would buy headroom under a cap.
        "cost_usd": max(0.0, float(cost_usd or 0)),
        "latency_ms": latency_ms,
    }
    row_id = int(conn.execute(
        f"INSERT INTO usage_events ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values())).lastrowid)
    _warn(conn, unit)
    return row_id


class Guarded(WrapperModel):
    """A model client that asks before each request and bills after it.

    Billing call by call, at each caller, missed the night, the mailbox,
    skills, the extract, the audit, the trials and the coder: each was one
    more place to remember, and none of them was. And a run that a retry
    threw away had paid for its requests all the same. So every client
    Steve builds is wrapped in this (`agent/provider.client_for`,
    `core/coder/models.client`): a request goes past no cap, and each one
    is billed, one row, to the unit of work it was made in.

    A request refused raises `Refused` before anything is sent. A bill that
    cannot be written is logged, and the answer still returned: it was paid
    for."""

    def __init__(self, wrapped: Model, *, slug: str, price: tuple[float, float, float],
                 effort: str | None = None) -> None:
        super().__init__(wrapped)
        self.slug = slug
        # Dollars per million tokens: input, cached input, output.
        self.price = price
        self.effort = effort

    def cost(self, usage: Any) -> float:
        """Dollars for one request's usage. Cached tokens are a subset of the
        input, billed at their own rate."""
        return models_guard.usd(
            self.price, input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            cached_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0))

    async def request(self, messages, model_settings, model_request_parameters):
        unit = current()
        await allowed(unit)
        started = timer.perf_counter()
        try:
            response = await self.wrapped.request(messages, model_settings,
                                                  model_request_parameters)
        except Exception as exc:
            await if_spent(unit, exc)
            raise
        await self._billed(unit, response.usage, started)
        return response

    @contextlib.asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters,
                             run_context=None) -> AsyncIterator[Any]:
        unit = current()
        await allowed(unit)
        started = timer.perf_counter()
        try:
            async with self.wrapped.request_stream(messages, model_settings,
                                                   model_request_parameters,
                                                   run_context) as stream:
                try:
                    yield stream
                finally:
                    # Shielded: a reply the user cut off has still been paid for.
                    await asyncio.shield(self._billed(unit, stream.usage, started))
        except Exception as exc:
            await if_spent(unit, exc)
            raise

    async def _billed(self, unit: Unit | None, usage: Any, started: float) -> None:
        details = getattr(usage, "details", None) or {}
        fields = {
            "model": self.slug,
            "effort": self.effort,
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "cached_tokens": int(getattr(usage, "cache_read_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "reasoning_tokens": int(details.get("reasoning_tokens", 0) or 0),
            "latency_ms": int((timer.perf_counter() - started) * 1000),
        }
        await billed(unit, self.cost(usage), **fields)


# What `Guarded` does around each request, for a call made without a client
# (core/websearch.py): ask first, write down a spent balance, bill after.

async def allowed(unit: Unit | None) -> None:
    """Refused, raised, if `unit` may not spend now. Off the event loop, on
    a connection of the thread's own: a `sqlite3` connection belongs to the
    thread that opened it."""
    refused = await asyncio.to_thread(_asked, unit)
    if refused is not None:
        raise refused


async def if_spent(unit: Unit | None, exc: BaseException) -> None:
    """A request refused because OpenAI's balance is spent, written down
    (`ran_out`). Never raises: the refusal is what the caller hears."""
    if not out_of_credit(exc):
        return
    try:
        await asyncio.to_thread(_ran_out_in, unit)
    except Exception:  # noqa: BLE001 - the caller still hears the refusal
        log.exception("could not write down that OpenAI's balance is spent")


async def billed(unit: Unit | None, cost_usd: float, **fields: Any) -> None:
    """One request's bill, written off the event loop (`bill` takes the
    fields). Never raises: the answer came, and only its bill is lost, which
    is logged."""
    try:
        await asyncio.to_thread(_write, unit, cost_usd, fields)
    except Exception:  # noqa: BLE001 - the answer came; only its bill is lost
        log.exception("could not bill a %s request", fields.get("model"))


def if_spent_here(conn: sqlite3.Connection, exc: BaseException) -> None:
    """`if_spent` for a call made on the caller's own connection, outside
    pydantic-ai. Never raises: the refusal is what the caller hears."""
    if not out_of_credit(exc):
        return
    try:
        ran_out(conn)
    except Exception:  # noqa: BLE001 - the caller still hears the refusal
        log.exception("could not write down that OpenAI's balance is spent")


def openai_why(exc: BaseException, timeout_s: float) -> str:
    """Why a call made with the `openai` SDK had no answer, in words the user,
    or the model, can be told. Hearing, voicing, the embedder and the web
    search each had a copy of this."""
    import openai

    from core.secrets import MissingSecret

    if out_of_credit(exc):
        return "OpenAI's balance is used up"
    if isinstance(exc, MissingSecret):
        return "no OpenAI key is stored"
    if isinstance(exc, openai.APITimeoutError):
        return f"OpenAI gave no answer within {timeout_s:.0f} seconds"
    if isinstance(exc, openai.APIConnectionError):
        return "OpenAI could not be reached"
    if isinstance(exc, openai.APIStatusError):
        return f"OpenAI answered {exc.status_code}"
    return f"{type(exc).__name__}: {exc}"[:200]


def _ran_out_in(unit: Unit | None) -> None:
    with contextlib.closing(_open(unit)) as conn:
        ran_out(conn)


def _asked(unit: Unit | None) -> Refused | None:
    with contextlib.closing(_open(unit)) as conn:
        return may_spend(conn, unit=unit)


def _write(unit: Unit | None, cost_usd: float, fields: dict) -> None:
    with contextlib.closing(_open(unit)) as conn:
        bill(conn, cost_usd, unit=unit, **fields)


@contextlib.contextmanager
def ledger(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    """`conn`, or else the current unit's ledger, open for the while: for a
    call made by code that keeps nothing of its own, such as the screen run
    from a command. Without it such a call could not be asked about or
    billed, and spent unseen."""
    if conn is not None:
        yield conn
        return
    with contextlib.closing(_open(current())) as own:
        yield own


def _open(unit: Unit | None) -> sqlite3.Connection:
    """The unit's database, or the data folder's for a call made in none --
    brought up to date first: a command run before any worker has made the
    data folder's database finds no ledger there to ask. Up to date already,
    as the worker's is, that is one quick look."""
    from core import db, paths

    conn = db.connect(unit.store if unit and unit.store else paths.db_path(), load_vec=False)
    try:
        db.migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn
