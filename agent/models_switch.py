"""Switching a role to a model the scout found, watching it for a day, and
putting it back.

The switch in force is `data/model_switches.json`, which agent/models.py
lays over the lineup as written (`load_active`), every entry back through
the frozen guard. This module writes that file, and keeps the history and
each switch's watch in `model_switches`. Who may switch is the guard's call
(`models_guard.switch_refusal`): the user by hand, always; the night only while
`models.auto_switch` is on, and only to a model the bar said yes to.

A switch is watched for a day with the canary's count (core/canary.py):
the ledger's failures against the two days before, and the new model's calls
against the old one's, for time and for cost (`models_guard.watch_verdict`).
Worse, and the role goes back on its own and the user is told why. "REVERT
MODELS" from them puts every role back at once, answered before any model
reads it (agent/worker/answering.py).

One switch at a time: two changes watched together cannot be told apart,
and either watch could put back the wrong one.

A change takes effect in the worker that makes it, at once (`apply`); a
worker started later reads the file. The command line, a process of its
own, asks the supervisor for a swap instead.
"""

from __future__ import annotations

import contextlib
import json
import logging
import statistics
import time
from datetime import UTC, datetime, timedelta

from agent import models, sqltime
from agent.models import BASE, Role
from core import canary, db, settingsfile
from core.models_guard import (
    Calls,
    Candidate,
    Decision,
    Refused,
    admit,
    switch_refusal,
    watch_verdict,
)
from core.policy import load_policy

log = logging.getLogger(__name__)

# What the user calls each role, in a message to them.
SAYS = {
    "triage": "sorting incoming messages",
    "converse": "everyday replies",
    "reason": "harder replies",
    "escalate": "the hardest replies",
    "bulk": "background work",
}


def written(role: str) -> str:
    """The model agent/models.py writes down for `role`."""
    return BASE[Role(role)].slug if role in Role._value2member_map_ else "the lineup as written"


class SwitchRefused(RuntimeError):
    """A switch that may not be made, with the reason."""


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def read_switches() -> dict:
    """The switches in force, by role; empty when there are none, or the file
    is unreadable -- which is also how agent/models.py treats it."""
    try:
        raw = settingsfile.load(models.active_path(), models.SWITCH_STEPS)
    except (OSError, ValueError):
        return {}
    roles = (raw or {}).get("roles")
    return dict(roles) if isinstance(roles, dict) else {}


def _write_switches(roles: dict) -> None:
    """Whole or not at all: a worker starting mid-write must not read half."""
    path = models.active_path()
    if not roles:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    settingsfile.save(path, {"roles": roles}, models.SWITCH_STEPS, indent=1)


def apply() -> dict[str, str]:
    """Put the file over this worker's lineup now. Returns what the guard
    refused, by role."""
    from agent import provider

    refused = models.load_active()
    provider.reset_clients()
    return refused


# ---------------------------------------------------------------------------
# Switching and putting back
# ---------------------------------------------------------------------------


def watching(conn):
    """The switch being watched, if one is."""
    return conn.execute("SELECT * FROM model_switches WHERE kind = 'switch'"
                        " AND outcome IS NULL ORDER BY id DESC LIMIT 1").fetchone()


def last_trial(conn, slug: str, role: Role, eval_id: int | None = None):
    """The trial a switch rests on: the one named, or the latest finished."""
    if eval_id is not None:
        return conn.execute("SELECT * FROM model_evals WHERE id = ? AND slug = ? AND role = ?",
                            (eval_id, slug, role.value)).fetchone()
    return conn.execute("SELECT * FROM model_evals WHERE slug = ? AND role = ?"
                        " AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (slug, role.value)).fetchone()


def _decision(trial) -> Decision | None:
    if trial is None or not trial["decision"]:
        return None
    try:
        return Decision(**json.loads(trial["decision"]))
    except (TypeError, ValueError):
        return None


def switch(conn, slug: str, role: Role, *, by: str, eval_id: int | None = None,
           now: float | None = None) -> str:
    """Switch `role` to `slug` and start its watch; returns what to tell
    them. Does not apply it: the caller does (`apply`, or a swap)."""
    now = time.time() if now is None else now
    row = conn.execute("SELECT facts FROM model_candidates WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise SwitchRefused(f"{slug} is not in the last scan")
    try:
        candidate = Candidate.from_dict(json.loads(row["facts"]))
        admit(candidate, role.value, BASE[role])
    except (Refused, ValueError) as exc:
        raise SwitchRefused(f"{slug} may not play {role.value}: {exc}") from exc
    trial = last_trial(conn, slug, role, eval_id)
    decision = _decision(trial)
    if (why := switch_refusal(by, decision)) is not None:
        raise SwitchRefused(why)
    if (busy := watching(conn)) is not None:
        raise SwitchRefused(f"{busy['to_slug']} is still being watched for {busy['role']}")

    current = models.MODELS[role].slug
    window = load_policy().models.watch
    baseline = canary.count(conn, canary.sqltime(now - window["baseline_hours"] * 3600),
                            canary.sqltime(now))
    roles = read_switches()
    roles[role.value] = {"candidate": candidate.to_dict(), "since": canary.sqltime(now),
                         "by": by, "eval_id": trial["id"] if trial else None, "from": current}
    _write_switches(roles)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO model_switches (role, from_slug, to_slug, kind, by, eval_id, why,"
            " baseline, watch_until, at) VALUES (?, ?, ?, 'switch', ?, ?, ?, ?, ?, ?)",
            (role.value, current, slug, by, trial["id"] if trial else None,
             decision.reason if decision else "by hand", json.dumps(baseline.as_dict()),
             canary.sqltime(now + window["hours"] * 3600), canary.sqltime(now)))
    said = SAYS.get(role.value, role.value)
    because = f": {decision.reason}" if decision and decision.switch else ""
    return (f"Switched {said} from {current} to {slug}{because}. Watching it for a day; "
            "reply REVERT MODELS to put it back.")


def revert(conn, roles: list[str] | None = None, *, by: str, why: str,
           now: float | None = None) -> list[str]:
    """Put `roles` (all, if None) back on the lineup as written, and close
    their watches. Returns the roles put back."""
    now = time.time() if now is None else now
    held = read_switches()
    back = [r for r in held if roles is None or r in roles]
    for role in back:
        entry = held.pop(role)
        to = (entry.get("candidate") or {}).get("slug", "?") if isinstance(entry, dict) else "?"
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO model_switches (role, from_slug, to_slug, kind, by, why, at)"
                " VALUES (?, ?, ?, 'revert', ?, ?, ?)",
                (role, to, written(role), by, why, canary.sqltime(now)))
            conn.execute("UPDATE model_switches SET outcome = ? WHERE kind = 'switch'"
                         " AND role = ? AND outcome IS NULL", (f"reverted: {why}", role))
    _write_switches(held)
    return back


# ---------------------------------------------------------------------------
# The watch
# ---------------------------------------------------------------------------


def _calls(conn, slug: str, since: str, until: str, effort: str | None = None) -> Calls:
    sql = ("SELECT latency_ms, cost_usd FROM usage_events WHERE model = ?"
           " AND created_at >= ? AND created_at < ?")
    args: list = [slug, since, until]
    if effort is not None:
        sql += " AND effort = ?"
        args.append(effort)
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return Calls(0, None, None)
    seconds = [r["latency_ms"] / 1000 for r in rows if r["latency_ms"] is not None]
    return Calls(len(rows), statistics.median(seconds) if seconds else None,
                 sum(r["cost_usd"] or 0 for r in rows) / len(rows))


def tick(conn, now: float | None = None) -> list[str]:
    """Look at the switch being watched: put it back if the role is doing
    worse, or keep it once its day is up. Returns what to tell them."""
    now = time.time() if now is None else now
    row = watching(conn)
    if row is None:
        return []
    role = row["role"]
    said = SAYS.get(role, role)
    if role not in read_switches():
        # Put back some other way (REVERT MODELS, a file taken away).
        with db.transaction(conn):
            conn.execute("UPDATE model_switches SET outcome = 'reverted: no longer in force'"
                         " WHERE id = ?", (row["id"],))
        return []
    since = row["at"]
    window = load_policy().models.watch
    before = canary.Counts.from_dict(json.loads(row["baseline"] or "{}"))
    after = canary.count(conn, since, canary.sqltime(now))
    old_from = sqltime.stamp(sqltime.moment(since) - timedelta(hours=window["baseline_hours"]))
    base_effort = BASE[Role(role)].effort if role in Role._value2member_map_ else None
    old = _calls(conn, row["from_slug"], old_from, since, base_effort)
    new = _calls(conn, row["to_slug"], since, canary.sqltime(now))
    if (why := watch_verdict(before, after, old, new)) is not None:
        revert(conn, [role], by="watch", why=why, now=now)
        return [f"Put {said} back on {written(role)}: since switching to "
                f"{row['to_slug']}, {why}. Nothing for you to do."]
    if canary.sqltime(now) >= row["watch_until"]:
        with db.transaction(conn):
            conn.execute("UPDATE model_switches SET outcome = 'kept' WHERE id = ?",
                         (row["id"],))
        return [f"Kept {row['to_slug']} for {said} after a day's watch: {after.turns} "
                f"replies, no worse than before."]
    return []


def status(conn) -> dict:
    """What is switched now, what is being watched, and the last changes."""
    return {
        "in_force": {r: (e.get("candidate") or {}).get("slug") for r, e in read_switches().items()
                     if isinstance(e, dict)},
        "watching": dict(w) if (w := watching(conn)) is not None else None,
        "history": [dict(r) for r in conn.execute(
            "SELECT role, from_slug, to_slug, kind, by, why, outcome, at FROM model_switches"
            " ORDER BY id DESC LIMIT 10")],
        "now": datetime.now(UTC).isoformat(timespec="seconds"),
    }
