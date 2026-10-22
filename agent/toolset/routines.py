"""Routines the user sets by text (agent/routines.py): the tools, and the executor
behind the card that approves what one may do outward.

Only the user, in a conversation they are having, may set one up or restart one
(`_theirs`). Left to the toolsets alone that would not hold: their conversations
are not the only runs offered every tool -- the night's research is one --
and a routine that could make routines could widen itself.
"""

from __future__ import annotations

from pydantic_ai import RunContext

from agent import approvals, routines
from agent.actions import act, executable
from agent.toolset.clients import commitment_store
from agent.turn import Deps


def _theirs(ctx: RunContext[Deps]) -> str | None:
    """Why this run may not set up or restart a routine, or None."""
    from core.call_policy import is_autonomous

    if (is_autonomous(ctx.deps.channel) or ctx.deps.worker or ctx.deps.routine_id is not None
            or ctx.deps.goal_id is not None):
        return ("Only the user, in a conversation they are having, can set up or restart a "
                "routine. Say in your answer what you would set up, and they can ask.")
    return None


@executable("approve_routine")
async def _do_approve_routine(*, routine_id: int, envelope_sha: str, title: str = "") -> dict:
    """Their yes to a routine's targets: it starts, if its envelope is still the
    one the card showed."""
    conn, borrowed = commitment_store()
    try:
        started = routines.activate(conn, int(routine_id), envelope_sha)
    finally:
        if borrowed:
            conn.close()
    if not started:
        return {"ok": False, "error": f"routine {routine_id} changed or was cancelled since "
                                      "that card; it has to be asked for again"}
    return {"ok": True, "routine_id": routine_id, "title": title, "started": True}


def register(tool) -> None:
    """Attach the routine tools. `tool` is `register_tools`'s filter."""

    @tool
    async def create_routine(
        ctx: RunContext[Deps],
        title: str,
        instruction: str,
        at_time: str = "",
        days: list[str] | None = None,
        every_minutes: int = 0,
        until: str = "",
        condition: str = "",
        email_to: list[str] | None = None,
        invite: list[str] | None = None,
        urgent: bool = False,
    ) -> dict:
        """Set up something done on a schedule, or watched for, from now on.

        For "every Sunday at 7pm, recap my week" or "check every 3 hours until
        the tickets go on sale". Not for a one-off: that is `remind_me`, and
        checking something for the next few hours is `keep_checking`.

        - `instruction`: what each run does, written for an assistant who will
          not see this conversation -- the user's words, made to stand alone.
        - Either `at_time` ("19:00") with optional `days` (["sun"], or
          ["mon","tue","wed","thu","fri"]; none is every day), or
          `every_minutes` (30 at least) with `until` (ISO local datetime)
          and/or `condition` (what ends it: "tickets are on sale").
        - `email_to` / `invite`: only when the user asked for the routine to
          write to or invite these people itself. Each run may then do that
          without asking -- only to them -- once the user approves one card
          listing them. Otherwise leave them empty: a run then prepares and
          notes, and never sends.
        - `urgent`: a watch whose news may reach the user in quiet hours.

        Each run reports only what changed. Tell the user how it will behave,
        and that "STOP ROUTINES" pauses them all.
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
            schedule = routines.Schedule.build(
                at_time=at_time, days=days or (), every_minutes=every_minutes,
                until=until, condition=condition)
            routine = routines.create(
                ctx.deps.conn, title=title, instruction=instruction, schedule=schedule,
                envelope=envelope or None, urgent=urgent, session_id=ctx.deps.session_id)
        except routines.BadRoutine as exc:
            return {"ok": False, "error": str(exc)}
        out: dict = {"ok": True, "routine": routine.as_dict()}
        if routine.envelope:
            # Every target on the card, however many: it was cut at 300
            # characters, and a yes to the start of a list is a yes to
            # people the user never saw (agent/approvals.py, "What a card may say").
            # The instruction whole too: it is what each run acts on, and the
            # card showed only the title until step 11's audit (D10). The
            # hash binds all three, so changing any asks again. The first
            # line is the targets alone; each part the model wrote, the
            # schedule's condition too, is on a labelled line of its own
            # after it (`approvals.one_line`), so none can pose as the card.
            preview = ("Each run of this routine may, without asking: "
                       f"{monitor.describe_envelope(routine.envelope)}.\n"
                       f"Routine: {approvals.one_line(routine.title)}\n"
                       f"When: {approvals.one_line(schedule.says())}\n"
                       f"Instruction: {approvals.one_line(routine.instruction)}")
            out["approval"] = await act(
                ctx, "approve_routine",
                {"routine_id": routine.id,
                 "envelope_sha": monitor.holder_sha(ctx.deps.conn, "routine", routine.id),
                 "title": routine.title},
                preview)
            out["note"] = "It does not run until the user approves that card."
        return out

    @tool
    async def list_routines(ctx: RunContext[Deps]) -> list[dict]:
        """The user's routines: what each does, when, what it may do without
        asking, and its id for pausing or cancelling.
        """
        return [r.as_dict() for r in routines.listed(ctx.deps.conn)]

    @tool
    async def pause_routine(ctx: RunContext[Deps], routine_id: int,
                            resume: bool = False) -> dict:
        """Pause a routine, or resume a paused one (`resume=True`)."""
        if resume and (why := _theirs(ctx)):
            return {"ok": False, "error": why}
        done = routines.pause(ctx.deps.conn, routine_id, resume=resume)
        return {"ok": done, "routine_id": routine_id, "resumed" if resume else "paused": done}

    @tool
    async def cancel_routine(ctx: RunContext[Deps], routine_id: int) -> dict:
        """Stop a routine for good."""
        done = routines.cancel(ctx.deps.conn, routine_id)
        return {"ok": done, "routine_id": routine_id, "cancelled": done}
