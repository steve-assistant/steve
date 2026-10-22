"""Goals the user sets (agent/goals.py): their tools, a session's one tool, and the
executor behind the card that approves what a goal may do outward.

Only the user, in a conversation they are having, may start, resume or answer a
goal (`_theirs`). An answer is an instruction the goal follows, so a goal that
could answer itself -- or one a routine or the night could start -- could
steer itself anywhere. A session is offered `goal_update` and none of the
others (`goals.toolset`), and `goal_update` writes only to the goal the
session belongs to (`Deps.goal_id`), never one named in its arguments: a
session cannot touch another goal, and nothing outside a session can pose
as one.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic_ai import RunContext

from agent import approvals, goals
from agent.actions import act, executable
from agent.toolset.clients import commitment_store
from agent.turn import Deps


class StepChange(TypedDict, total=False):
    n: int
    status: Literal["todo", "doing", "waiting_on_user", "waiting_on_someone", "done", "dropped"]
    note: str


def _theirs(ctx: RunContext[Deps]) -> str | None:
    """Why this run may not start, resume or answer a goal, or None."""
    from core.call_policy import is_autonomous

    if (is_autonomous(ctx.deps.channel) or ctx.deps.worker or ctx.deps.routine_id is not None
            or ctx.deps.goal_id is not None):
        return ("Only the user, in a conversation they are having, can start, resume or "
                "answer a goal. Say in your answer what you would do, and they can ask.")
    return None


@executable("approve_goal")
async def _do_approve_goal(*, goal_id: int, envelope_sha: str, title: str = "") -> dict:
    """Their yes to a goal's targets: it starts, if its envelope is still the
    one the card showed."""
    conn, borrowed = commitment_store()
    try:
        started = goals.activate(conn, int(goal_id), envelope_sha)
    finally:
        if borrowed:
            conn.close()
    if not started:
        return {"ok": False, "error": f"goal {goal_id} changed or was dropped since that "
                                      "card; it has to be asked for again"}
    return {"ok": True, "goal_id": goal_id, "title": title, "started": True}


def register(tool) -> None:
    """Attach the goal tools. `tool` is `register_tools`'s filter."""

    @tool
    async def start_goal(
        ctx: RunContext[Deps],
        title: str,
        ask: str,
        done_when: str,
        steps: list[str],
        check_every_hours: float = goals.EVERY_HOURS,
        deadline: str = "",
        email_to: list[str] | None = None,
        invite: list[str] | None = None,
        budget_usd: float = goals.BUDGET_USD,
    ) -> dict:
        """Take on something that needs work over days: "find three times next
        week for a call with Alex", "get my deposit back from the old
        landlord", "plan Dana's birthday dinner". It works on it in the
        background, in sessions, until it is done -- asking the user only
        when it must.

        Not for something one turn can do (do it now), or a background task
        that ends in one answer (`defer_to_background`), or the same thing on
        a schedule (`create_routine`).

        - `ask`: what the user wants, in their words, made to stand alone for
          an assistant who will not see this conversation.
        - `done_when`: what has to be true for it to be finished ("a time is
          agreed with Alex and booked").
        - `steps`: the plan, in order, each one doable or waitable ("find
          three free hours next week", "offer them to Alex", "wait for Alex's
          reply", "invite Alex to the time chosen").
        - `check_every_hours`: how often it looks again (1 to 168; 12 if
          unsure). It also looks again when the user answers it, and when
          someone it is waiting on writes back.
        - `deadline`: ISO date or datetime, if they gave one.
        - `email_to` / `invite`: only when the user asked for it to write to
          or invite these people itself. It may then do that without asking
          -- only to them -- once the user approves one card listing them.
          Otherwise leave them empty: every message or invitation is then a
          card the user approves.
        - `budget_usd`: what it may spend on models in all (default $2, at
          most $10).

        Show the user the plan briefly, and tell them "STOP GOALS" pauses
        them all.
        """
        if why := _theirs(ctx):
            return {"ok": False, "error": why}
        from core import monitor

        envelope: dict = {}
        if email_to:
            envelope["send_email"] = {"targets": list(email_to)}
        if invite:
            envelope["create_event"] = {"targets": list(invite)}
        try:
            goal = goals.create(
                ctx.deps.conn, title=title, ask=ask, done_when=done_when, steps=steps,
                every_hours=check_every_hours, deadline=deadline, envelope=envelope or None,
                budget_usd=budget_usd, session_id=ctx.deps.session_id)
        except goals.BadGoal as exc:
            return {"ok": False, "error": str(exc)}
        out: dict = {"ok": True, "goal": goal.as_dict(full=True)}
        if goal.envelope:
            # Every target on the card, however many, and what it works
            # toward, whole, on lines of their own (as for a routine).
            preview = ("While this goal works it may, without asking: "
                       f"{monitor.describe_envelope(goal.envelope)}.\n"
                       f"Goal: {approvals.one_line(goal.title)}\n"
                       f"Ask: {approvals.one_line(goal.ask)}\n"
                       f"Done when: {approvals.one_line(goal.done_when)}")
            out["approval"] = await act(
                ctx, "approve_goal",
                {"goal_id": goal.id,
                 "envelope_sha": monitor.holder_sha(ctx.deps.conn, "goal", goal.id),
                 "title": goal.title},
                preview)
            out["note"] = "It does not start until the user approves that card."
        else:
            out["note"] = "Its first session starts within a minute or so (not in quiet hours)."
        return out

    @tool
    async def goal_status(ctx: RunContext[Deps], goal_id: int = 0) -> dict:
        """The user's goals and how far each has got; with `goal_id`, the whole
        record: plan, findings, its questions and their answers.
        """
        if goal_id:
            goal = goals.get(ctx.deps.conn, goal_id)
            if goal is None:
                return {"ok": False, "error": f"no goal {goal_id}"}
            return {"ok": True, "goal": goal.as_dict(full=True)}
        return {"ok": True, "goals": [g.as_dict() for g in goals.listed(ctx.deps.conn)]}

    @tool
    async def answer_goal(ctx: RunContext[Deps], goal_id: int, answer: str,
                          question: int = 0) -> dict:
        """Pass on what the user says to one of their goals: their answer to its
        question (`question`, the number it gave), or -- with no question --
        anything they want it to know or do differently. It acts on it at its
        next session, which this brings forward. Use their words.
        """
        if why := _theirs(ctx):
            return {"ok": False, "error": why}
        try:
            return {"ok": True, "goal_id": goal_id,
                    **goals.answer(ctx.deps.conn, goal_id, answer, question=question)}
        except goals.BadGoal as exc:
            return {"ok": False, "error": str(exc)}

    @tool
    async def pause_goal(ctx: RunContext[Deps], goal_id: int, resume: bool = False,
                         add_budget_usd: float = 0.0) -> dict:
        """Pause a goal, or resume a paused one (`resume=True`) -- with more
        budget (`add_budget_usd`) if what it had is spent."""
        if resume and (why := _theirs(ctx)):
            return {"ok": False, "error": why}
        try:
            done = goals.pause(ctx.deps.conn, goal_id, resume=resume,
                               add_budget_usd=add_budget_usd)
        except goals.BadGoal as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": done, "goal_id": goal_id, "resumed" if resume else "paused": done}

    @tool
    async def drop_goal(ctx: RunContext[Deps], goal_id: int) -> dict:
        """Stop working on a goal for good."""
        done = goals.drop(ctx.deps.conn, goal_id)
        return {"ok": done, "goal_id": goal_id, "dropped": done}

    @tool
    async def goal_update(
        ctx: RunContext[Deps],
        steps: list[StepChange] | None = None,
        add_steps: list[str] | None = None,
        findings: list[str] | None = None,
        ask_user: str = "",
        waiting_on: list[str] | None = None,
        done: bool = False,
        outcome: str = "",
        next_check_hours: float = 0.0,
    ) -> dict:
        """Record this session's progress on the goal: the only record the
        next session will have.

        - `steps`: changes to steps by number: their new status, and a short
          note (what you did; ids of drafts and events you made).
        - `add_steps`: steps the plan turned out to need.
        - `findings`: facts worth keeping, each with where it came from.
        - `ask_user`: one question, only when you cannot go on without the user.
        - `waiting_on`: the addresses whose reply you are waiting for (all of
          them; [] when no longer waiting). Mail from them brings the next
          session forward.
        - `done` with `outcome`: when what "done" means holds.
        - `next_check_hours`: when to look again, if not on its usual cadence.
        """
        if ctx.deps.goal_id is None or ctx.deps.worker:
            return {"ok": False, "error": "only a goal's own work session can update it"}
        return goals.apply_update(
            ctx.deps.conn, ctx.deps.goal_id, steps=[dict(s) for s in steps or ()],
            add_steps=add_steps, findings=findings, ask_user=ask_user, waiting_on=waiting_on,
            done=done, outcome=outcome, next_check_hours=next_check_hours)
