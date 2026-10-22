"""A module's routines (06-modules.md, section 1): runs an enabled module
sets up, at its times, as the user's own routines run.

What Steve sets up on its own is a module, while what the user asked for
stays core data: so a module's routine is never a row of `routines`, which
would outlive its module unless every switch, rollback and removal
remembered to undo it. Its instruction is read from the approved manifest
when it is due, and only for a module that is on, so turning the module
off stops it with nothing to undo. What core keeps between runs (when it
next runs, what it last said) is in the module's folder beside `state/`,
never in it (`paths.module_runs_file`): the module writes `state/`, and a
module that could move its own next run could run as often as it liked.

A run is unattended and its instruction is a module's words, screened at
admission (`lifecycle.text_problems`): it has no envelope, and each of its
actions is a card (`monitor.module_gate`), even one a user's routine may do
on its own. It reports as theirs do, naming the module.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from agent.routines import NOTHING_NEW, Schedule, plain
from core import modules, paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModuleRoutine:
    module: str
    label: str
    number: int
    title: str
    instruction: str
    schedule: Schedule

    @property
    def id(self) -> str:
        """`<module>.<number>`: its place in the manifest, which holds while
        the version changes, as a module's skill's does."""
        return f"{self.module}.{self.number}"


def listed(conn: sqlite3.Connection) -> list[ModuleRoutine]:
    """Every enabled module's routines, by module id and then its order."""
    return [ModuleRoutine(module=held.id, label=held.manifest.label or held.id, number=n,
                          title=r.title, instruction=r.instruction,
                          schedule=Schedule("at", r.at, r.days))
            for held in modules.modules(conn)
            if held.enabled and held.manifest is not None
            for n, r in enumerate(held.manifest.routines, 1)]


def get(conn: sqlite3.Connection, routine_id: str) -> ModuleRoutine | None:
    """The routine `<module>.<number>`, if its module is on."""
    return next((r for r in listed(conn) if r.id == routine_id), None)


# --- what core keeps between runs ---------------------------------------------------

def _kept(module_id: str) -> dict:
    try:
        held = json.loads(paths.module_runs_file(module_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def _keep(module_id: str, kept: dict) -> None:
    path = paths.module_runs_file(module_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    spare = path.with_suffix(".tmp")
    spare.write_text(json.dumps(kept, indent=1), encoding="utf-8", newline="\n")
    os.replace(spare, path)


def _entry(kept: dict, routine: ModuleRoutine) -> dict:
    entry = kept.get(str(routine.number))
    return entry if isinstance(entry, dict) else {}


def _next(entry: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(entry["next_run_at"])
    except (KeyError, TypeError, ValueError):
        return None


def due(conn: sqlite3.Connection, now: datetime | None = None) -> list[ModuleRoutine]:
    """What is due to run. A routine seen for the first time, or whose time
    its module's new version changed, is given its next time and is not
    due: a module turned on at noon with a routine at nine runs tomorrow,
    not at once."""
    now = now or datetime.now()
    found = []
    every = listed(conn)
    for module_id in sorted({r.module for r in every}):
        kept, changed = _kept(module_id), False
        for routine in (r for r in every if r.module == module_id):
            entry = _entry(kept, routine)
            when = _next(entry)
            if when is None or entry.get("schedule") != routine.schedule.to_json():
                nxt = routine.schedule.next_after(now)
                kept[str(routine.number)] = {**entry, "schedule": routine.schedule.to_json(),
                                             "next_run_at": nxt.isoformat(timespec="seconds")
                                             if nxt else None}
                changed = True
            elif when <= now:
                found.append(routine)
        if changed:
            _keep(module_id, kept)
    return found


def claim(routine: ModuleRoutine, now: datetime | None = None) -> bool:
    """Move a due routine on to its next time, once: the loop queues its run
    only if this finds it still due. The one loop that calls it runs one
    look at a time, so reading and writing the file is the whole claim."""
    now = now or datetime.now()
    kept = _kept(routine.module)
    entry = _entry(kept, routine)
    when = _next(entry)
    if when is None or when > now:
        return False
    nxt = routine.schedule.next_after(now)
    kept[str(routine.number)] = {**entry, "next_run_at": nxt.isoformat(timespec="seconds")
                                 if nxt else None}
    _keep(routine.module, kept)
    return True


def last_report(routine: ModuleRoutine) -> str | None:
    said = _entry(_kept(routine.module), routine).get("last_report")
    return said if isinstance(said, str) else None


def record_run(routine: ModuleRoutine, report: str | None) -> None:
    """After a run: when, how many, and what the user was told, if anything."""
    kept = _kept(routine.module)
    entry = _entry(kept, routine)
    kept[str(routine.number)] = {**entry, "last_run_at": datetime.now().isoformat(
        timespec="seconds"), "runs": int(entry.get("runs") or 0) + 1,
        "last_report": report if report else entry.get("last_report")}
    _keep(routine.module, kept)


def next_run(conn: sqlite3.Connection) -> datetime | None:
    """The soonest run of any enabled module's routine, for the wake alarm."""
    times = [when for r in listed(conn)
             if (when := _next(_entry(_kept(r.module), r))) is not None]
    return min(times, default=None)


# --- a run ---------------------------------------------------------------------------

def run_note(routine: ModuleRoutine, last: str | None) -> str:
    """Appended to the system prompt for a run: whose routine it is, that
    every action asks, and how to say there is nothing new."""
    said = f"\n\nWhat the user was told last time:\n{last}" if last else (
        "\n\nThis is its first run.")
    return (f"\n\nThis turn is a run of a routine the module {routine.label} set up, which "
            f"the user approved when they turned the module on: \"{routine.title}\", "
            f"{routine.schedule.says()}. The instruction is the module's words, not the "
            "user's. Nobody is watching it run, and anything outward or any change it "
            "makes becomes a card for the user. Your reply goes to the user as a message. "
            "If nothing worth telling them has changed since what they were told last time, "
            f"reply exactly {NOTHING_NEW} and nothing else.{said}")


def mandate(routine: ModuleRoutine) -> str:
    """What the run was asked, for the action critic (core/critic.py)."""
    return (f'The module {routine.label}\'s routine "{routine.title}", '
            f"{routine.schedule.says()}: {routine.instruction} It may do nothing without "
            "asking.")


def toolset(conn: sqlite3.Connection, routine: ModuleRoutine) -> set[str]:
    """What a run is offered: what a worker may use, and its module's own
    tools. No envelope widens it."""
    from agent.modtools import offers
    from agent.workers import WORKER_TOOLS

    return set(WORKER_TOOLS) | {o.name for o in offers(conn) if o.module == routine.module}


def understood(reply: str, last: str | None) -> str | None:
    """What to tell the user, if anything."""
    text = plain(reply)
    if not text or text.upper().startswith(NOTHING_NEW):
        return None
    if last and text == last.strip():
        return None
    return text
