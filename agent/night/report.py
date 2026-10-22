"""What a night did, and the one message about it the user reads in the morning.

`NightReport` is stored with the night's job row (`night._store`) and read
back by the morning briefing, whichever worker is up by then; `Budget` is
the ceiling every block checks between steps.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, fields


@dataclass
class NightReport:
    started_at: str
    researched: list[str] = field(default_factory=list)
    learned: list[str] = field(default_factory=list)
    # The model scout's weekly round (agent/night/scouting.py): what it
    # looked at, what it tried, and any switch -- with how to undo it.
    scouted: list[str] = field(default_factory=list)
    # The yardstick's weekly run (agent/night/yardstick.py): the unattended
    # sessions against planted attacks, in a line.
    measured: list[str] = field(default_factory=list)
    # The desk's rehearsal (agent/night/rehearsal.py): how many of its
    # scenarios passed, run when the desk changed or weekly.
    rehearsed: list[str] = field(default_factory=list)
    # Skills kept from the day's runs (agent/night/skilling.py), each with
    # how to take it back.
    skills: list[str] = field(default_factory=list)
    # Facts about them kept from what the user said (agent/night/learning.py
    # `_learn_from_user_words`), each with its number, so FORGET MEMORY can
    # take one back the morning it is told.
    remembered: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    seconds: float = 0.0
    stopped_because: str = "finished"
    # The blocks a spending cap kept from starting, in order.
    not_done: list[str] = field(default_factory=list)
    # Set when self-improvement did not run because policy.yaml pauses it.
    # Said in the digest every morning it applies, so a pause is never
    # mistaken for a night with nothing to do.
    improvement_paused: str = ""
    # The modules the night built or tried (agent/night/tinkering.py), each
    # a line with its build number and how it ended.
    built: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> NightReport:
        """The report back from `as_dict`, as `_store` wrote it to `jobs`.

        So the morning briefing can carry the night whichever worker ran
        it: a swap between 3am and 8am used to mean no report at all.
        """
        names = {f.name for f in fields(cls)}
        known = {k: v for k, v in data.items() if k in names and k != "seconds"}
        known.setdefault("started_at", "")
        return cls(**known, seconds=float(data.get("minutes") or 0) * 60)

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "learned": self.learned,
            "researched": self.researched,
            "scouted": self.scouted,
            "measured": self.measured,
            "rehearsed": self.rehearsed,
            "skills": self.skills,
            "remembered": self.remembered,
            "cost_usd": round(self.cost_usd, 4),
            "minutes": round(self.seconds / 60, 1),
            "stopped_because": self.stopped_because,
            "not_done": self.not_done,
            "improvement_paused": self.improvement_paused,
            "built": self.built,
        }


# How the user takes back a fact the night kept: answered before any model
# (agent/worker/answering.py `FORGET_MEMORY_RE`).
FORGET_HINT = 'Reply "FORGET MEMORY <number>" to drop one.'


class Budget:
    """A spend ceiling checked between steps rather than enforced mid-call."""

    def __init__(self, limit: float) -> None:
        self.limit = limit
        self.spent = 0.0

    def add(self, amount: float) -> None:
        self.spent += max(0.0, amount or 0.0)

    def remaining(self) -> float:
        return max(0.0, self.limit - self.spent)

    def exhausted(self, headroom: float = 0.0) -> bool:
        return self.remaining() <= headroom


# Each part of the night, as the morning reads it and the day page opens it:
# the report's field and its heading, in order.
PARTS = (
    ("scouted", "Models"),
    ("skills", "Skills learned"),
    ("measured", "Measured, the week's yardstick"),
    ("rehearsed", "The desk, rehearsed"),
    ("remembered", "Remembered about you, from what you said"),
    ("learned", "Learned"),
    ("researched", "Read about"),
    ("built", "Modules built"),
)
# The most of what it learned that is listed; the rest is housekeeping.
LEARNED_SHOWN = 5


def _items(report: NightReport, name: str) -> list[str]:
    items = getattr(report, name)
    return items[:LEARNED_SHOWN] if name == "learned" else items


def sections(report: NightReport) -> list[tuple[str, list[str]]]:
    """The night's parts that did something, each with its heading, then
    why it stopped early and what a pause kept from it: what the day
    page's fold of the night opens to."""
    found = [(heading, _items(report, name)) for name, heading in PARTS if getattr(report, name)]
    if report.improvement_paused:
        found.append(("No modules built", [report.improvement_paused]))
    if report.stopped_because != "finished":
        found.append(("Stopped early", [report.stopped_because]
                      + ([f"Not done: {', '.join(report.not_done)}"] if report.not_done else [])))
    return found


def _counted(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary(report: NightReport) -> str:
    """The night in about two sentences: what it did, then what it took,
    or why it stopped. The day page folds the night under it, as it folds
    a spoken exchange. Read from the report, so it costs nothing and is
    there as soon as the night is."""
    did = [phrase for phrase in (
        report.researched and f"read about {_counted(len(report.researched), 'topic', 'topics')}",
        report.learned and "learned from the day",
        report.remembered and (
            f"kept {_counted(len(report.remembered), 'thing', 'things')} you said"),
        report.skills and f"kept {_counted(len(report.skills), 'skill', 'skills')}",
        report.built and f"worked on {_counted(len(report.built), 'module', 'modules')}",
        report.scouted and "looked over the models",
        report.measured and "ran the week's yardstick",
        report.rehearsed and "rehearsed the desk",
    ) if phrase]
    if not did:
        first = "A quiet night, with nothing worth doing."
    else:
        first = "Overnight I " + (did[0] if len(did) == 1
                                  else ", ".join(did[:-1]) + " and " + did[-1]) + "."
    if report.stopped_because != "finished":
        return f"{first} I stopped early: {report.stopped_because.rstrip('.')}."
    minutes = max(1, round(report.seconds / 60))
    return f"{first} It took {_counted(minutes, 'minute', 'minutes')} and ${report.cost_usd:.2f}."


def digest(report: NightReport, conn: sqlite3.Connection | None = None) -> str:
    """One message, readable on a phone before coffee.

    Leads with what needs a decision, because that is the only part that
    cannot wait. Everything else is there to be skimmed.
    """
    lines = [f"Overnight ({report.started_at}) — ${report.cost_usd:.2f}"]

    # First, and capped hard. Commitments
    # merely loom, and "here are your 14 open commitments" every morning is
    # exactly how this section gets muted.
    if conn is not None:
        from tools import commitments as _commitments

        looming = _commitments.due_soon(conn)
        if looming:
            lines.append("")
            lines.append("Before they're late:")
            lines += [f"  · {c.line()}" for c in looming]

    for name, heading in PARTS:
        items = _items(report, name)
        if items:
            lines.append("")
            lines.append(f"{heading}:")
            lines += [f"  · {item}" for item in items]
            if name == "remembered":
                lines.append(f"  {FORGET_HINT}")

    if not (
        report.researched or report.learned or report.built
        or report.improvement_paused or report.scouted or report.measured
        or report.rehearsed or report.skills or report.remembered
    ):
        lines.append("")
        lines.append("Quiet night — nothing worth doing.")

    if report.improvement_paused:
        lines.append("")
        lines.append(f"No modules built tonight: {report.improvement_paused}.")

    if report.stopped_because != "finished":
        lines.append("")
        lines.append(f"Stopped early: {report.stopped_because}")
        if report.not_done:
            lines.append("Not done: " + ", ".join(report.not_done) + ".")

    return "\n".join(lines)
