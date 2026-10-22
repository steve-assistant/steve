"""The canary's count: whether Steve answers worse since a change.

A change that starts and answers worse -- more turns that fell over, more
claims it had to correct, more tool errors, more of the user's corrections
and thumbs-down -- passes every health check. The failure ledger
(agent/issues.py) reads the agent, and this counts what it read: replies
and bad events in a window (`count`), and whether a window after a change
is clearly worse than the one before it (`verdict`).

The model switch watches a new model with it for a day
(agent/models_switch.py, through `core/models_guard.watch_verdict`), and
puts the old one back on a rise. It also watched every self-upgrade the
night merged, from the supervisor, and put main back on a rise; Steve no
longer merges its own changes, and that watch went with the merging. A
module from outside is judged by the same comparison, on counts of its own
calls (`core/lifecycle.noted`).

**Frozen**, so a change cannot make the numbers say what it likes.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass, field

# What counts as the agent doing worse. Not a slow reply, a retried model
# call or a source that failed -- those are the providers' and the network's
# -- nor their notes, which come with a thumbs-down already counted.
BAD_KINDS = ("turn_broke", "turn_loop", "unbacked_claim", "tool_error", "correction",
             "feedback_down", "worker_crashed")
# Nor what the network did. On 30 September both broken turns of the day
# were "ModelAPIError: Connection error." in an hour when the laptop's DNS
# was failing (getaddrinfo, connections reset): a merge in that hour would
# have been blamed for them. Matched in an issue's summary, lower-cased.
NETWORK = ("connection error", "connecterror", "connectionreseterror",
           "connectionabortederror", "getaddrinfo", "winerror 10053", "winerror 10054",
           "errno 11001", "name resolution", "network is unreachable")

# The bar: at least this many bad events, and a rate at least double the
# baseline's and 15 points above it. Fewer replies than MIN_TURNS are not a
# rate -- unless every one of them went wrong.
MIN_BAD = 3
MIN_TURNS = 5
RISE = 0.15
# The baseline's rate when it saw too few replies to have one.
DEFAULT_RATE = 0.15


def sqltime(epoch: float) -> str:
    """A moment as SQLite's `datetime('now')` writes it: UTC, to the second."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


@dataclass
class Counts:
    turns: int = 0
    bad: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def rate(self) -> float | None:
        return self.bad / self.turns if self.turns >= MIN_TURNS else None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Counts:
        return cls(turns=int(data.get("turns") or 0), bad=int(data.get("bad") or 0),
                   by_kind={str(k): int(v) for k, v in (data.get("by_kind") or {}).items()})


def count(conn: sqlite3.Connection, since: str, until: str) -> Counts:
    """Replies, and bad events in the ledger, in [since, until). Runs in the
    worker, on its connection."""
    turns = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE role = 'assistant'"
        " AND created_at >= ? AND created_at < ?", (since, until)).fetchone()[0]
    marks = ", ".join("?" * len(BAD_KINDS))
    offline = " OR ".join("lower(i.summary) LIKE ?" for _ in NETWORK)
    rows = conn.execute(
        f"SELECT i.kind, COUNT(*) FROM issue_events e JOIN issues i ON i.id = e.issue_id"
        f" WHERE e.at >= ? AND e.at < ? AND i.kind IN ({marks}) AND NOT ({offline})"
        f" GROUP BY i.kind",
        (since, until, *BAD_KINDS, *(f"%{n}%" for n in NETWORK))).fetchall()
    by_kind = {str(kind): int(n) for kind, n in rows}
    return Counts(turns=int(turns), bad=sum(by_kind.values()), by_kind=by_kind)


def verdict(before: Counts | None, after: Counts, *, unit: str = "replies") -> str | None:
    """Why the agent is doing worse since the change, or None if it is not
    (or it is too early to say). `unit` is what `turns` counts.

    All bad events together, and then each kind alone. The second is not
    redundant: on 30 September the two days before held 51 replies and 16
    bad events, 11 of them one tool the model kept calling with the wrong
    id -- so the whole rate was 31%, its bar 62%, and a change that broke
    three turns in twenty (none in the 51 before) would have hidden under
    it."""
    base_turns = before.turns if before is not None else 0
    why = _worse("failures", before.bad if before is not None else 0, base_turns,
                 after.bad, after.turns, unit)
    if why is not None:
        return f"{why}; {_kinds(after)}"
    if before is None or before.turns < MIN_TURNS:
        return None
    for kind, n in sorted(after.by_kind.items(), key=lambda kv: -kv[1]):
        why = _worse(kind, before.by_kind.get(kind, 0), before.turns, n, after.turns, unit)
        if why is not None:
            return why
    return None


def _worse(what: str, base_bad: int, base_turns: int, bad: int, turns: int,
           unit: str = "replies") -> str | None:
    """The bar, for one count against its baseline."""
    if bad < MIN_BAD:
        return None
    base = base_bad / base_turns if base_turns >= MIN_TURNS else DEFAULT_RATE
    if turns < MIN_TURNS:
        # Too few replies for a rate -- unless every one of them went wrong.
        if bad >= max(turns, 1):
            return f"{bad} {what} in {turns} {unit} since the change"
        return None
    now = bad / turns
    if now < max(2 * base, base + RISE):
        return None
    return (f"{bad} {what} in {turns} {unit} since the change ({now:.0%}), "
            f"against {base:.0%} before")


def _kinds(counts: Counts) -> str:
    return ", ".join(f"{n} {k}" for k, n in sorted(counts.by_kind.items(), key=lambda kv: -kv[1]))
