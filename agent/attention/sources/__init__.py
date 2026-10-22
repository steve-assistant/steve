"""Where the attention loop looks, and how often.

A source is a plain function from a database connection to a list of
`Observation`s. It runs in a worker thread on its own connection
(`offloop.with_own_conn`), because every source is blocking -- Google's
client, Outlook's COM, an HTTP fetch -- and the worker's event loop is also
what answers `/health` (protocols §2).

A source does not judge. It turns what it found into observations with
stable keys and lets the store drop what was seen before. If it can say
cheaply what is new (Gmail lists ids for free), it asks the store first and
fetches only that.

**Cadence is per source, and remembered.** When each last ran is kept in
`sync_cursors` as `attention:<name>`, not in memory, so a swapped worker
does not re-run everything at once, and a laptop that slept all night
finds every source overdue and catches up on its first tick.

**A failed source is still a run.** Its time is recorded with the error, so
an expired token is retried at the source's own cadence rather than every
minute; the other sources carry on.

**A source watches only what the user has.** One that reads an account
names the feature it needs (`agent/features.py`): with no mailbox
connected, nothing looks for mail every three minutes, and the judge is
not told about mail (`triage.judge_prompt`). `watching` is the list. Mail
is one source per mail account (`mail:<account key>`), so an account
switched off is not polled and its cursor not advanced.

To add one: write `collect(conn) -> list[Observation]` in a module here,
list it in `all_sources` with a cadence in `settings.CADENCE_S`, and give
its observations a key that names the thing *in the state worth mentioning*
-- an event's id alone is one mention for its whole life, its id and its
`updated` stamp is one per change. If what it hands the judge carries words
a stranger wrote, they go through the injection screen (agent/screening.py):
in the source, as mail and the calendar do, or as the source's `screen`,
which `collect` gives only what is new and within the cap.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from agent.attention import settings
from agent.attention.store import Observation, known
from agent.features import Enabled
from core import db

log = logging.getLogger("attention")


# How often an account a module adds is polled when its manifest says
# nothing (`ClassSpec.every_min`'s default).
EVERY_S = 15 * 60


class SourceError(RuntimeError):
    """A source's integration reported a failure rather than raising one."""


def ok(result: dict | None, what: str) -> dict:
    """The result of a guarded tool call, or a `SourceError`.

    The Google and Outlook helpers catch their own exceptions and return
    `{"error": ...}` -- right for a model, which reads the error, and
    wrong here, where a failure dressed as a result would be recorded as
    "nothing new" forever.
    """
    if not isinstance(result, dict):
        raise SourceError(f"{what}: no result")
    if result.get("error"):
        raise SourceError(f"{what}: {result['error']}")
    return result


@dataclass(frozen=True)
class Source:
    name: str
    collect: Callable[[sqlite3.Connection], list[Observation]]
    # An observation as the judge may read it, for a source whose items carry
    # a stranger's words (agent/screening.py). Applied to what is new and
    # within the cap: a feed hands over sixty headlines a look, most of them
    # seen before, and each one screened is a call.
    screen: Callable[[sqlite3.Connection, Observation], Observation] | None = None
    # Whether the source has anything to watch for this user: a forecast
    # needs a home, headlines need topics. None for a source that always
    # does. Checked each look, reading SQLite only, so the first topic
    # followed or home given turns its source on without a restart.
    wanted: Callable[[sqlite3.Connection], bool] | None = None
    # The feature it reads through, by its name in `agent/features.py`:
    # "mail" for a mailbox, "calendar" for a calendar. None for a source
    # every user has.
    feature: str | None = None
    # The account it reads, by its name in the feature's lines ("gmail"),
    # for a feature with several. On with any mail feature, the Gmail
    # source failed every three minutes for a user whose only mailbox was
    # another.
    account: str | None = None
    # Its cadence in seconds, for a source made for an account; None reads
    # `settings.CADENCE_S` by name.
    every: int | None = None
    # A module's source only: the words for what it looks at, and its
    # paragraph for the judge, in a block naming the module
    # (`triage.judge_prompt`). Core's own are in `triage.JUDGE_SOURCES`.
    judge: tuple[str, str] | None = None

    def on(self, conn: sqlite3.Connection, have: Enabled) -> bool:
        """Whether it watches anything for this user, who has the
        features `have`."""
        from agent import features

        return ((self.feature is None or self.feature in have)
                and (self.account is None
                     or self.account in features.accounts(have.get(self.feature, "")))
                and (self.wanted is None or self.wanted(conn)))

    @property
    def every_s(self) -> int:
        return self.every if self.every is not None else settings.CADENCE_S[self.name]


def _mail(conn: sqlite3.Connection, found) -> Source | None:
    """The source that polls one mail account, as often as its settings
    say; None for one the user does not want watched."""
    from agent.attention.sources import mail
    from core import modules

    row = modules.account(conn, found.key)
    if row is not None and row.settings.get("watched") is False:
        return None
    every_min = row.settings.get("every_min") if row is not None else None
    if not isinstance(every_min, int):
        held = modules.module(conn, found.module)
        spec = held.manifest.specs.get("mail") if held and held.manifest else None
        every_min = spec.every_min if spec else EVERY_S // 60
    every = every_min * 60
    return Source(f"mail:{found.key}", lambda c: mail.watch(c, found), feature="mail",
                  account=found.name, every=every)


def all_sources(conn: sqlite3.Connection | None = None) -> list[Source]:
    """Every source, in the order a tick runs them: one for each mail
    account (`conn`'s; none without it), then core's own, then one for each
    enabled module with an attention source (`conn`'s too). Blocking: the
    mail accounts and the modules are read from the registry."""
    from agent import connectors
    from agent.attention.sources import (
        calendar,
        deadlines,
        drive,
        health,
        modules,
        news,
        obligations,
        threads,
        weather,
    )

    mail = [] if conn is None else [s for f in connectors.accounts(conn, "mail")
                                    if (s := _mail(conn, f)) is not None]
    return [
        *mail,
        Source("calendar", calendar.changes, feature="calendar"),
        Source("horizon", calendar.horizon, feature="calendar"),
        Source("after", calendar.after, wanted=calendar.following_up, feature="calendar"),
        Source("commitments", obligations.commitments),
        Source("deadlines", deadlines.due, feature="deadlines"),
        Source("health", health.health),
        Source("drive", drive.shared, drive.screened, feature="drive"),
        Source("weather", weather.weather, wanted=weather.wanted),
        Source("news", news.news, news.screened, wanted=news.wanted),
        Source("threads", threads.threads, feature="mail"),
        *([] if conn is None else modules.sources(conn)),
    ]


def watching(conn: sqlite3.Connection) -> list[Source]:
    """The sources this user has (`Source.on`), in a tick's order.
    Blocking: telling what is set up reads the registry and the keychain."""
    from agent import features

    have = features.enabled(conn)
    return [s for s in all_sources(conn) if s.on(conn, have)]


def _cursor(name: str) -> str:
    return f"attention:{name}"


def due(conn: sqlite3.Connection, sources: list[Source]) -> list[Source]:
    """The sources whose cadence has elapsed since they last ran.

    Both times are SQLite's UTC, so they compare directly.
    """
    ready = []
    for source in sources:
        row = conn.execute(
            "SELECT 1 FROM sync_cursors WHERE source = ?"
            " AND ran_at > datetime('now', ?)",
            (_cursor(source.name), f"-{int(source.every_s)} seconds"),
        ).fetchone()
        if row is None:
            ready.append(source)
    return ready


def ran(conn: sqlite3.Connection, name: str, note: str) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO sync_cursors (source, ran_at, note) VALUES (?, datetime('now'), ?)"
            " ON CONFLICT(source) DO UPDATE SET ran_at = excluded.ran_at, note = excluded.note",
            (_cursor(name), note[:300]),
        )


def last_runs(conn: sqlite3.Connection) -> dict[str, dict]:
    """When each source last ran and how it went, for the status page."""
    rows = conn.execute(
        "SELECT source, datetime(ran_at, 'localtime') AS at, note FROM sync_cursors"
        " WHERE source LIKE 'attention:%'"
    ).fetchall()
    return {r["source"].split(":", 1)[1]: {"at": r["at"], "note": r["note"]} for r in rows}


def collect(conn: sqlite3.Connection, source: Source) -> list[Observation]:
    """Run one source, record that it ran, never raise.

    The caller hands this to a thread with a connection of its own.
    """
    try:
        found = source.collect(conn)
    except Exception as exc:  # noqa: BLE001 - one source is never the loop
        log.warning("source %s failed: %s", source.name, exc)
        ran(conn, source.name, f"failed: {type(exc).__name__}: {exc}")
        from agent import issues

        called = source.account or source.name
        issues.record(conn, "source_failed", f"{source.name}: {type(exc).__name__}",
                      f"The {called} source failed: {type(exc).__name__}: {str(exc)[:120]}")
        return []
    # What is new, then the cap -- in that order. Capping first would let
    # the thirty events already considered crowd out the thirty-first, which
    # is new, on every run for the rest of the week.
    seen = known(conn, [o.key for o in found])
    fresh = [o for o in found if o.key not in seen]
    ran(conn, source.name, f"ok: {len(found)} found, {len(fresh)} new")
    fresh = fresh[: settings.MAX_PER_SOURCE]
    return [source.screen(conn, o) for o in fresh] if source.screen else fresh
