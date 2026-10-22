"""What the checks in code would have changed, replayed over recorded days.

    python -m agent.attention vet --dry-run --db <copy of agent.db> [--days 14]

A check that has only passed its own fixtures has not met their data. Every
check in `vet.py` was written from an incident, and the incidents are in
the database: Trey Research's invitation raised as a reply the user owed (observations
3762 and 4385), the Ridgeview hurricane warnings (4125/4126/4128/4143),
Fabrikam's receipts (3467/3468), one message told twice (3159/3160). So the
replay walks the recorded observations in order, through the same code the
loop runs -- `before_judging`, `after_judging`, and for what was actually
said, the check made just before saying it (`fresh`, against their words up
to that moment) -- and reports what would have changed.

It never writes to the file it is given: the database is read once,
read-only, into memory, and the replay changes only that copy. The judge is
not asked again; its recorded verdicts are what the checks act on. Rows
judged before reasons were kept carry none, so `sender_rule` treats them as
it would a judge that gave no reason. A night ping is replayed by the night
rule: critical, after the checks.

What it cannot replay: which trips were verified then (the kept forecasts
are today's); and the commitments closed then (it reads today's).
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from agent import sqltime
from agent.attention import settings, store, vet
from core import db

# The incidents the checks were written from, shown whatever else happened.
INCIDENTS = (3762, 4385, 4125, 4126, 4128, 4143, 3467, 3468, 3159, 3160)
TOLD = ("ping", "before", "morning", "evening")


def open_copy(path: Path) -> sqlite3.Connection:
    """The database at `path`, read-only, copied into memory, with this
    branch's tables added (`verdicts`, `told`)."""
    from core import db

    source = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(":memory:", isolation_level=None)
        source.backup(copy)
    finally:
        source.close()
    copy.row_factory = sqlite3.Row
    db.functions(copy)  # the vetting it replays reads through `core/modules.live`
    db.migrate(copy)
    return copy


def _local(stamp: str) -> dt.datetime:
    return sqltime.moment(stamp).astimezone().replace(tzinfo=None)


@dataclass
class Outcome:
    id: int
    source: str
    title: str
    judge_say: str
    judge_critical: bool
    told_as: str | None      # what really happened: ping, morning, ... or None
    told_at: str | None
    say: str                 # what the replay ends with
    critical: bool
    stage: str               # where it changed: "", "before the judge", ...
    why: str
    night: str = ""          # for a real night ping: what the night rule makes of it

    @property
    def told(self) -> bool:
        return self.told_as in TOLD

    @property
    def still_told(self) -> bool:
        return self.told and self.say != "never"


def _verdict(conn: sqlite3.Connection, obs_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM verdicts WHERE obs_id = ?", (obs_id,)).fetchone()


def replay(conn: sqlite3.Connection, *, days: int = 14) -> list[Outcome]:
    """Walk the window's judged observations in order through the checks,
    on `conn` (a copy: it is changed)."""
    from agent.attention import owed

    rows = conn.execute(
        "SELECT * FROM observations WHERE first_seen >= datetime('now', ?)"
        " AND triaged_at IS NOT NULL AND say IS NOT NULL ORDER BY id",
        (f"-{days} days",)).fetchall()
    real = {r["id"]: dict(r) for r in rows}
    # Undone, so each row is said or not as the replay decides; put back
    # below for what the replay would still have said.
    with db.transaction(conn):
        conn.executemany("UPDATE observations SET delivered_at = NULL, delivered_in = NULL"
                         " WHERE id = ?", [(i,) for i in real])
    refs = owed.closed_refs(conn)
    out: list[Outcome] = []
    for obs_id, was in real.items():
        [obs] = store.get(conn, [obs_id])
        stage = ""
        if not vet.before_judging(conn, [obs]):
            stage = "before the judge"
        else:
            vet.after_judging(conn, [obs_id], now=_local(was["triaged_at"]))
            [obs] = store.get(conn, [obs_id])
            if obs.say != was["say"] or obs.critical != bool(was["critical"]):
                stage = "after the judge"
            if was["delivered_in"] in TOLD and obs.say != "never":
                said = vet.user_words(conn, obs.triaged_at, until=was["delivered_at"])
                check = vet.fresh(obs, _local(was["delivered_at"]), said=said, closed_refs=refs)
                if not check.ok:
                    store.vetted(conn, obs_id, outcome="dropped", why=check.why, say="never")
                    stage = "before it was said"
                else:
                    with db.transaction(conn):
                        conn.execute("UPDATE observations SET delivered_at = ?, delivered_in = ?"
                                     " WHERE id = ?",
                                     (was["delivered_at"], was["delivered_in"], obs_id))
        [obs] = store.get(conn, [obs_id])
        verdict = _verdict(conn, obs_id)
        result = Outcome(
            id=obs_id, source=was["source"], title=was["title"], judge_say=was["say"],
            judge_critical=bool(was["critical"]), told_as=was["delivered_in"],
            told_at=was["delivered_at"], say=obs.say or "never", critical=obs.critical,
            stage=stage, why=(verdict["vet_why"] if verdict else "") or "")
        if was["delivered_in"] == "ping" and settings.within_quiet_hours(
                _local(was["delivered_at"])):
            result.night = _night(obs)
        out.append(result)
    return out


def _night(obs: store.Observation) -> str:
    """What the night rule -- critical, after the checks -- makes of a real
    night ping."""
    if obs.say == "now" and obs.critical:
        return "would still go"
    return f"held for the morning ({obs.say}, critical={obs.critical})"


def _kind(why: str) -> str:
    """A check's sentence without its particulars, to count alike ones."""
    text = re.sub(r'"[^"]*"|\([^)]*\)|\d+', "", why.split(";")[0])
    return " ".join(text.split())[:70]


def summary(outcomes: list[Outcome], *, path: str, days: int) -> str:
    told = [o for o in outcomes if o.told]
    lines = [
        f"Replayed {len(outcomes)} judged observations from the last {days} days of {path}"
        f" ({sum(o.judge_say != 'never' for o in outcomes)} the judge thought worth"
        f" something; {len(told)} were told).",
    ]
    for stage in ("before the judge", "after the judge", "before it was said"):
        changed = [o for o in outcomes if o.stage == stage]
        if not changed:
            lines.append(f"{stage.capitalize()}: nothing changed.")
            continue
        dropped = sum(o.say == "never" and o.judge_say != "never" for o in changed)
        lines.append(f"{stage.capitalize()}: {len(changed)} changed, {dropped} of them"
                     f" dropped from something the judge would have said.")
        for kind, n in Counter(_kind(o.why) for o in changed).most_common(6):
            lines.append(f"    {n:4d}  {kind}")
    fyi = [o for o in outcomes if o.judge_critical and not o.critical and o.say != "never"]
    lines.append(f"Critical taken away, still said as FYI: {len(fyi)}.")
    lost = [o for o in told if not o.still_told]
    lines.append(f"Told: {len(told)} in fact; {len(told) - len(lost)} would have been"
                 f" ({len(lost)} fewer).")
    by_source = Counter(o.source for o in lost)
    if by_source:
        lines.append("    not told, by source: " + ", ".join(
            f"{s} {n}" for s, n in by_source.most_common()))
    nights = [o for o in outcomes if o.night]
    lines.append(f"Night pings: {len(nights)} in fact;"
                 f" {sum(o.night.startswith('would still go') for o in nights)} would go now.")
    for o in nights:
        lines.append(f"    {o.id} {o.title[:60]!r}: {o.night}")
    lines.append("Incidents:")
    by_id = {o.id: o for o in outcomes}
    for obs_id in INCIDENTS:
        o = by_id.get(obs_id)
        if o is None:
            lines.append(f"    {obs_id}: not in the window")
            continue
        lines.append(f"    {o.id} {o.source} {o.title[:55]!r}")
        lines.append(f"         in fact: judged {o.judge_say}"
                     f"{' critical' if o.judge_critical else ''},"
                     f" told {o.told_as or 'never'}"
                     f"{' at ' + o.told_at if o.told_as in TOLD else ''}")
        lines.append(f"         replay:  {o.say}{' critical' if o.critical else ''}"
                     f"{' (' + o.stage + ')' if o.stage else ''} -- {o.why[:160]}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m agent.attention vet",
        description="What the checks in code would have changed, without writing.")
    parser.add_argument("--dry-run", action="store_true", required=True,
                        help="required: this only ever reports")
    parser.add_argument("--db", required=True, help="a copy of agent.db, opened read-only")
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args(argv)
    conn = open_copy(Path(args.db))
    try:
        outcomes = replay(conn, days=args.days)
    finally:
        conn.close()
    print(summary(outcomes, path=args.db, days=args.days))
    return 0
