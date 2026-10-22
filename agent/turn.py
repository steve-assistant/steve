"""What one conversational turn carries, and how it climbs the ladder.

`Deps` is the per-turn state every tool can read and write. The ladder
is how a turn that is too hard for its tier is replayed on the next one
up: the `escalate` tool raises `EscalationRequested`, and
`agent.loop.Conversation` catches it and runs the turn again.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from agent.models import Role


class EscalationRequested(Exception):  # noqa: N818 - control flow, not an error
    """Raised by the escalate tool to end the current hop immediately.

    Returning a string that asks the model to stop is advice it can ignore --
    and a model that ignores it loops until a usage limit fires. Raising makes
    escalation genuinely terminal.
    """

    def __init__(self, to: Role, reason: str) -> None:
        self.to = to
        self.reason = reason
        super().__init__(f"escalate to {to.value}: {reason}")


# The ladder, as data. Extracted from the tool so it can be tested without a
# live model -- the bug this guards against (a replay not knowing which tier it
# was already on) is invisible in prose and only shows up in the transition.
#
# The roles *absent* from this map are the point of it. ESCALATE is the top.
# BULK is reflection, which nobody is waiting for. And CODE has no automatic
# escalation by design: retrying a failed self-modification on a pricier model
# with no human looking is how a cheap loop becomes an expensive one.
#
# A second copy of this lived in `agent/models.py` and was wired to nothing,
# which is the shape of bug where somebody tunes the ladder in the file that
# reads more naturally and nothing changes.
ESCALATION_LADDER: dict[Role, Role] = {
    Role.CONVERSE: Role.REASON,
    Role.REASON: Role.ESCALATE,
}


# Their words asking for depth. The maintainer, 5 October 2026: "high effort reasoning
# has its uses but doesn't need to be always". The ladder climbed a rung each
# time the model asked, and a tier given reasoning asked again at once:
# comparing two campsites went none (2 s), medium (3 s, escalated straight
# away), high (90-108 s). Medium is now as far as the model takes itself;
# high is for when the user asks, and then it goes straight there.
DEPTH_ASKED = re.compile(
    r"\b(think (it )?(hard|harder|carefully|deeply|it through|through)|take your time|"
    r"deep[- ]dive|be thorough|really think|reason (it )?(through|carefully)|high effort|"
    r"think long)\b", re.IGNORECASE)


def escalation_target(current: Role, pending: Role | None, asked: str) -> Role | None:
    """Where an escalation goes: one rung, to the reasoning tier; to the top
    only when the user asked for depth in this turn's words. None means stay."""
    at = pending or current
    if at is Role.CONVERSE:
        return Role.ESCALATE if DEPTH_ASKED.search(asked or "") else Role.REASON
    if at is Role.REASON and DEPTH_ASKED.search(asked or ""):
        return Role.ESCALATE
    return None


def next_tier(current: Role, pending: Role | None = None) -> Role | None:
    """The tier to escalate to, or None if already at the top.

    `pending` handles a model calling escalate twice inside one turn: the
    second call must advance from the tier the first one selected, not from
    the tier currently executing.
    """
    return ESCALATION_LADDER.get(pending or current)


@dataclass
class Deps:
    """Per-turn state the tools can read and write."""

    conn: sqlite3.Connection
    session_id: int
    # Where this turn arrived from. A card needs it so an approval can be
    # matched back, and so the tray and Signal are distinguishable in the
    # ledger.
    channel: str = "tray"
    # The tier currently running. Without this the escalate tool cannot tell a
    # first escalation from a replay, because deps are rebuilt on every hop.
    role: Role = Role.CONVERSE
    escalate_to: Role | None = None
    escalate_reason: str | None = None
    deferred: list[int] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    # Effects already carried out this turn, keyed by tool and arguments.
    # One dict for the whole turn, shared by every hop and every retry --
    # see `act` for why.
    performed: dict = field(default_factory=dict)
    # The workers this turn may start (agent/workers.py `Crew`): set only for
    # a background job's lead, and never for a worker, so no worker can
    # start one.
    crew: Any = None
    # This turn is a worker's: `act` lets it do only what is private and
    # reversible, and asks the user nothing.
    worker: bool = False
    # URLs this turn was shown -- search results, and what the user wrote -- the
    # only ones an unattended run may read (`core/egress.unseen`). One set
    # for the whole turn, like `performed`; a crew shares its lead's.
    seen_urls: set = field(default_factory=set)
    # How many tool calls this run may make before its tools are taken away
    # and it must answer from what it found (`toolset._within_budget`). None:
    # no budget but the hard one.
    tool_budget: int | None = None
    # Sites that refused a fetch, by host, and how often (`tools/web.py`
    # `refused_before`): after two, the rest of the task stops asking them.
    # Like `seen_urls`, one for the turn, and a crew shares its lead's.
    refused: Counter = field(default_factory=Counter)
    # The routine this run belongs to (agent/routines.py): `act` asks
    # `core/monitor.routine_gate`, which lets it do outward only what their
    # approved envelope allows. None for every other run.
    routine_id: int | None = None
    # A run of a module's routine (agent/modroutines.py), `<module>.<n>`:
    # `act` asks `core/monitor.module_gate`, which makes each action a card.
    module_routine: str | None = None
    # Likewise the goal a work session belongs to (agent/goals.py), gated by
    # `core/monitor.goal_gate`; and `goal_update` writes only to this one.
    goal_id: int | None = None
    # What the user asked of this session, in their words where there are any --
    # the routine's instruction, the goal's ask, their note to the mailbox, the
    # task they asked for. The action critic (core/critic.py) judges an
    # unattended session's actions against it, and sees nothing else.
    mandate: str | None = None
    # A clean-room browsing worker's (agent/workers.py `Crew.start_browsing`):
    # the sites its browser is kept to, and the key its browser is held under
    # -- its own, never the lead's conversation's. Empty for every other run,
    # which an unattended one may not browse at all (`toolset.browser.refusal`).
    browse_sites: tuple = ()
    browser_key: int | None = None
    # This turn's lookups, shared by every hop (agent/toolset/pace.py): an
    # escalated tier asks again and gets the first answer at once.
    turn_cache: dict | None = None
    # The words this turn answers, for `escalation_target`: whether the user asked
    # it to think hard.
    asked: str = ""
    # Calendar writes this hop has already been asked "which email is this
    # from?" about (`toolset/calendar._where_from`): each is asked once. Not
    # carried across hops, as `performed` is -- an escalated tier replays the
    # turn from the history, and never saw the first tier's refusal.
    asked_where_from: set = field(default_factory=set)
