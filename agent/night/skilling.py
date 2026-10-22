"""The night's skills (agent/skills.py): the day's runs of what the user asked,
written up as methods where they were worth it, and said in the morning.

A night, not a turn: writing up a run takes two model calls -- the writer
and the reviewer -- and nothing about it is urgent. And in the morning
digest, every skill kept is said with how to take it back, so nothing is
learned that the user did not hear about.
"""

from __future__ import annotations

import logging
import sqlite3

from agent import modtext, skills
from agent.night.report import NightReport

log = logging.getLogger("night.skills")


async def distill(conn: sqlite3.Connection, report: NightReport) -> float:
    """This night's candidates, each written up, checked and reviewed.
    Returns what it spent; never raises. Nothing while the user has the
    `skills` module off: they turned off what this writes."""
    spent = 0.0
    try:
        if not modtext.skills_on(conn):
            return 0.0
        requests = skills.candidates(conn)
    except Exception:  # noqa: BLE001 - the rest of the night still reports
        log.exception("could not find runs to learn from")
        return 0.0
    for asked in requests:
        try:
            out = await skills.distill(conn, asked)
        except Exception as exc:  # noqa: BLE001 - one run is not the night
            log.exception("writing up request %s failed", asked)
            report.skills.append(f"Could not write up a run: {type(exc).__name__}"[:120])
            continue
        spent += float(out.get("cost_usd") or 0.0)
        if "kept" in out:
            report.skills.append(f"Learned skill {out['kept']}, \"{out['title']}\". "
                                 f"REVOKE SKILL {out['kept']} drops it.")
        else:
            log.info("request %s: %s", asked, out.get("refused") or out.get("skipped"))
    return spent
