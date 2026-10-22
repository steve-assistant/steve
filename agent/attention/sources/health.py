"""Whether what the agent depends on is still working.

On 28 September 2026 the OpenRouter balance had been below zero for most of
a day before anyone noticed: the Jev shadow had quietly stopped, the
fallback model with it, and a coding run failed on a 402. Nothing checked.
The same could be said of the module sandbox, Docker, the disk, and jobs
that keep failing -- each fails quietly, and each was found by hand.

Jev, the decision model behind the screen, the critic and three smaller
decisions (`core/decide.py`), is the quietest of all. Each site has a safe
answer for "no answer" -- an extract, a card, no split, no follow-up -- so
an outage changes how Steve behaves without a word of why: mail summarised
from extracts, and what it does on its own held for a yes. `_decisions`
says so.

Each check here is code, not a model's guess. A condition becomes an
observation keyed by what it is and the day, so it is said once a day while
it lasts and not every hour; the judge decides how to say it. Mail and the
accounts' sign-ins are not here: a failing mailbox source is already a
`source_failed` issue, and every account's health, its sign-in's end among
it, is said by its own loop (`agent/accounts.tell`).
"""

from __future__ import annotations

import datetime as dt
import logging
import shutil
import sqlite3

from agent.attention.store import Observation
from agent.cut import cut
from core import endpoints, paths

log = logging.getLogger("attention")

# Below this, top up soon; at or below nothing, things have stopped.
LOW_BALANCE_USD = 1.00
LOW_DISK_GB = 5.0
FAILED_JOBS_A_DAY = 3
# Jev's asks in the last hour that came back with no answer, across the
# sites that keep a row of each, before it is said to be down. One or two
# are a slow reply; a spending cap reached is not Jev's, and has its own
# notice (core/spend.py).
JEV_MISSES_AN_HOUR = 3
# What each decision site does while Jev cannot answer.
WITHOUT_JEV = (
    "Until it does, mail and pages reach the model only as extracts (or as the "
    "local readers judge them, if they are installed), anything done unattended "
    "waits for a yes, clicks on sites Steve is logged in to ask first, background "
    "tasks are not split, and meetings are not followed up."
)


def health(conn: sqlite3.Connection) -> list[Observation]:
    today = dt.date.today().isoformat()
    found = []
    for check in (_decisions, _openrouter, _module_sandbox, _docker, _disk, _failing_jobs):
        try:
            said = check(conn)
        except Exception as exc:  # noqa: BLE001 - one check is never the rest
            log.info("health check %s could not run: %s", check.__name__, exc)
            continue
        if said:
            name, title, detail = said
            found.append(Observation(source="health", key=f"health:{name}:{today}",
                                     title=title, detail=detail))
    return found


def openrouter_credits() -> str:
    """Where the OpenRouter key's credit is read: Jev's, and nothing else's."""
    return f"{endpoints.openrouter()}/v1/credits"


def _openrouter(conn) -> tuple[str, str, str] | None:
    from agent.attention import fetch
    from core import secrets

    key = secrets.get("OPENROUTER_API_KEY")
    if not key:
        return None
    data = fetch.get_json(openrouter_credits(), headers={"Authorization": f"Bearer {key}"})
    data = data.get("data") or {}
    left = float(data.get("total_credits") or 0) - float(data.get("total_usage") or 0)
    # It pays for Jev and nothing else: models are OpenAI's (agent/provider.py).
    if left <= 0:
        return ("openrouter:empty", f"OpenRouter credit is used up (${left:.2f})",
                "Jev, the decision model, stops answering until it is topped up at "
                f"openrouter.ai. {WITHOUT_JEV}")
    if left < LOW_BALANCE_USD:
        return ("openrouter:low", f"OpenRouter credit is low: ${left:.2f} left",
                "It pays for Jev, the decision model, which stops answering when it "
                "runs out. Top up at openrouter.ai.")
    return None


def _decisions(conn) -> tuple[str, str, str] | None:
    """Jev with no key to be asked on, or not answering: three asks or more
    in the last hour with no answer and an error, at the screen, the critic,
    the browser's clicks or task splitting."""
    from core import secrets

    if not secrets.get("OPENROUTER_API_KEY"):
        return ("decisions:nokey", "Jev, the decision model, has no key",
                f"It is asked on an OpenRouter key, and none is set. {WITHOUT_JEV}")
    missed = conn.execute(
        "SELECT error FROM ("
        " SELECT error, at FROM screened WHERE COALESCE(classifier, '') != 'jev'"
        " UNION ALL SELECT error, at FROM critic_reviews WHERE verdict IS NULL"
        " UNION ALL SELECT error, at FROM browser_judgements WHERE p IS NULL"
        " UNION ALL SELECT jev_error, at FROM crews WHERE jev_p IS NULL)"
        " WHERE error IS NOT NULL AND error NOT LIKE 'a spending cap is reached%'"
        " AND at >= datetime('now', '-1 hour') ORDER BY at DESC"
    ).fetchall()
    if len(missed) < JEV_MISSES_AN_HOUR:
        return None
    return ("decisions", "Jev, the decision model, is not answering",
            f"{len(missed)} of its answers were missing in the last hour; the last "
            f"said: {cut(missed[0][0], 160)}. {WITHOUT_JEV}")


def _module_sandbox(conn) -> tuple[str, str, str] | None:
    """What the start's check found (decision D6): nothing until it has run."""
    from core.sandbox import probe

    if (found := probe.verdict()) is None or (found.ok and not found.reason):
        return None
    why = f"{found.reason[:1].upper()}{found.reason[1:]}."
    if not found.ok:
        return ("modules:sandbox", "Modules with code cannot run on this computer",
                f"{why} Skills, prompt sections and the built-in accounts still work.")
    return ("modules:looser", "Modules run in a looser sandbox on this computer", why)


def _docker(conn) -> tuple[str, str, str] | None:
    from tools import code_exec

    if code_exec.available():
        return None
    return ("docker", "Docker is not running",
            "Running code for analysis needs it; Docker Desktop is meant to start at boot.")


def _disk(conn) -> tuple[str, str, str] | None:
    # The data's drive, which is the one that fills: it was the code's
    # (`parents[3]` of this file), which an install keeps elsewhere.
    free_gb = shutil.disk_usage(paths.data_dir()).free / 1e9
    if free_gb >= LOW_DISK_GB:
        return None
    return ("disk", f"The laptop's disk is nearly full: {free_gb:.1f} GB free",
            "Logs, the database and the modules' state all need room.")


def _failing_jobs(conn) -> tuple[str, str, str] | None:
    row = conn.execute(
        "SELECT COUNT(*), GROUP_CONCAT(DISTINCT kind) FROM jobs"
        " WHERE status = 'failed' AND created_at >= datetime('now', '-1 day')"
    ).fetchone()
    if not row or row[0] < FAILED_JOBS_A_DAY:
        return None
    return ("jobs", f"{row[0]} background jobs failed in the last day",
            f"Kinds: {row[1]}.")
