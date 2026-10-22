"""Once a week, at night: look for better or cheaper models, and try the
ones worth trying.

The model scout's weekly round (docs/autonomy.md): scan OpenAI's list
(agent/models_scout.py), take the shortlist, and try each on their own turns
(agent/models_eval.py), each trial stopping early when it is plainly losing.
A model that clears the bar is switched to only if the frozen policy lets the
night switch on its own (`models_guard.switch_refusal`); until then the
morning digest says it cleared the bar, and the user decides.

Its money is the scout's own -- $1 a trial, $5 a month, in core/policy.yaml --
not the night's: a week with three trials is still inside a month's budget,
and a month whose budget is spent tries nothing more.

Last in the night, after the blocks that spend the night's own budget, so a
long trial can never crowd them out.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from agent import models_eval, models_scout, models_switch, offloop
from agent.models import Role
from agent.night.report import NightReport

log = logging.getLogger("night")

# A week, less a few hours, so a night window that slides a little later
# each week still finds it due.
EVERY_S = 6.5 * 24 * 3600


def due(conn: sqlite3.Connection, now: float | None = None) -> bool:
    """A week since the last scan, or never scanned."""
    row = conn.execute("SELECT MAX(last_seen) AS at FROM model_candidates").fetchone()
    if row is None or row["at"] is None:
        return True
    from agent import sqltime

    last = sqltime.moment(row["at"]).timestamp()
    return (time.time() if now is None else now) - last >= EVERY_S


async def scout(conn: sqlite3.Connection, report: NightReport, *,
                now: float | None = None) -> float:
    """The week's round, if one is due. Returns what it spent; never raises."""
    if not due(conn, now):
        return 0.0
    spent = 0.0
    try:
        scanned = await offloop.with_own_conn(conn, models_scout.scan)
        new = scanned["new"]
        report.scouted.append(f"Looked at {scanned['listed']} models"
                              + (f"; new: {', '.join(new[:6])}" if new else "; nothing new"))
        for pick in models_scout.shortlist(conn):
            try:
                out = await models_eval.trial(conn, pick["slug"], pick["role"])
            except models_eval.TrialRefused as exc:
                report.scouted.append(f"Stopped trying models: {exc}")
                break
            spent += float(out.get("spent_usd") or 0)
            report.scouted.append(_tried_line(pick, out))
            if (line := _maybe_switch(conn, pick, out)) is not None:
                report.scouted.append(line)
    except Exception as exc:  # noqa: BLE001 - the rest of the night still reports
        log.exception("model scouting failed")
        report.scouted.append(f"Scouting stopped: {type(exc).__name__}: {exc}")
    return spent


def _tried_line(pick: dict, out: dict) -> str:
    role = models_switch.SAYS.get(pick["role"].value, pick["role"].value)
    decision = out.get("decision") or {}
    verdict = ("cleared the bar" if decision.get("switch")
               else f"no -- {decision.get('reason') or 'nothing measured'}")
    return (f"Tried {pick['slug']} for {role} ({pick['why']}): {verdict}"
            f" (${float(out.get('spent_usd') or 0):.2f})")


def _maybe_switch(conn: sqlite3.Connection, pick: dict, out: dict) -> str | None:
    """Switch to a model that cleared the bar, if the night may; otherwise
    say it cleared, for them to decide. Whether the night may is the guard's
    answer, asked inside `models_switch.switch` (`switch_refusal`)."""
    raw = out.get("decision")
    if not raw or not raw.get("switch"):
        return None
    role: Role = pick["role"]
    try:
        said = models_switch.switch(conn, pick["slug"], role, by="night",
                                    eval_id=out.get("eval_id"))
    except models_switch.SwitchRefused as exc:
        return (f"{pick['slug']} cleared the bar for {models_switch.SAYS[role.value]}, "
                f"but was not switched to: {exc}.")
    models_switch.apply()
    return said
