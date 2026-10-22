"""The agent's own control flow: escalating a turn, deferring work to the
background, and asking to change itself.
"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from agent import jobs
from agent.turn import Deps, EscalationRequested, escalation_target, next_tier

log = logging.getLogger("toolset.control")


def register(tool) -> None:
    """Attach the control tools. `tool` is `register_tools`'s filter."""

    @tool
    async def escalate(ctx: RunContext[Deps], reason: str) -> str:
        """Re-run this turn on a model with reasoning enabled.

        Use when the turn needs sustained reasoning rather than recall --
        and decide it early, before looking things up. Stop what you are
        doing after calling this; the turn will be replayed, and what you
        already looked up comes back at once if asked for again.
        """
        nxt = escalation_target(ctx.deps.role, ctx.deps.escalate_to, ctx.deps.asked)
        if nxt is None:
            if next_tier(ctx.deps.role, ctx.deps.escalate_to) is None:
                return "Already at the strongest model; answer as best you can."
            # The reasoning tier asking again: the top is for when the user asks.
            return ("You are already reasoning at the level this needs; answer it here. "
                    "(The strongest effort is used only when the user asks for it.)")
        ctx.deps.escalate_to = nxt
        ctx.deps.escalate_reason = reason
        raise EscalationRequested(nxt, reason)

    @tool
    async def defer_to_background(ctx: RunContext[Deps], task: str, why: str) -> dict:
        """Queue a long task and return immediately.

        Use for work that would stall the conversation. Call this at most once
        per turn, then tell the user what you queued and stop.

        Write `task` to stand alone: the job sees only that text, not this
        conversation -- name the people, dates, places and constraints they
        gave, not "the two sites above".
        """

        # Deferring exists so a conversation someone is waiting on is not
        # stalled, and nobody waits on an unattended one. The night's
        # research deferred its topics, and the background job each became
        # deferred again -- so a topic came back as "Queued a background
        # research task", the work ran outside the night's budget, and the
        # results were pushed to their phone at 3am.
        from core.call_policy import is_autonomous

        if is_autonomous(ctx.deps.channel):
            return {
                "status": "refused",
                "note": "Nobody is waiting on this conversation. Do the work here.",
            }

        # One deferral per turn. A model that calls this repeatedly queues the
        # same work several times over, and each copy costs a full research
        # run -- observed in testing as one request becoming three jobs.
        if ctx.deps.deferred:
            return {
                "job_id": ctx.deps.deferred[0],
                "status": "already_queued",
                "note": "This turn already queued a task. Tell the user and stop.",
            }

        job_id = jobs.enqueue(
            ctx.deps.conn,
            kind="research",
            prompt=task,
            why=why,
            session_id=ctx.deps.session_id,
        )
        ctx.deps.deferred.append(job_id)
        return {"job_id": job_id, "status": "queued"}

    @tool
    async def build_module(ctx: RunContext[Deps], request: str, module: str = "") -> dict:
        """Build yourself a new ability, as a module the user approves.

        Use when the user asks for something you have no tool for that a
        small program could do: check a site, read a feed, work something
        out. A coding model writes the module, an independent test writer
        and a judge check it in a sandbox, and one that passes waits as a
        card on the Extensions page. Nothing turns on until the user approves
        it there.

        `request` is what the user asked for, in their own words: the card
        shows it, and marks anything the module asks for that those words
        do not name. `module` is the id of a module you built before, for a
        new version of it.

        Tell the user what the answer says it is likely to cost and take,
        and that you will tell them when the card is ready.
        """
        import asyncio

        from agent import modbuild, offloop
        from core.call_policy import is_autonomous

        # Only the user, in a conversation they are in, asks: anything else
        # is Steve's own initiative, which the policy's switch decides.
        origin = "agent" if is_autonomous(ctx.deps.channel) else "user"
        # `request` is the model's words, which a mail it read can steer: the
        # card shows the user's own last message beside it, which no model
        # wrote. A run the user is not in has none of theirs to show.
        user_said = ctx.deps.asked if origin == "user" else ""
        try:
            row = await offloop.with_own_conn(
                ctx.deps.conn, modbuild.request, request, origin=origin,
                came_from="a conversation", session_id=ctx.deps.session_id, module=module,
                user_said=user_said)
        except modbuild.Refused as exc:
            return {"status": "refused", "note": str(exc)}
        if row["state"] == "running":
            modbuild.start(ctx.deps.conn, row["id"],
                           notify=modbuild.notifier(asyncio.get_running_loop()))
            return {"build": row["id"], "status": "building", "estimate": row["estimate"]}
        return {"build": row["id"], "status": "waiting", "estimate": row["estimate"],
                "note": ("This build would pass today's spending cap. It waits until the "
                         "user allows it on the Extensions page, in the app.")}

    @tool
    async def note_feedback(ctx: RunContext[Deps], what_they_said: str, about: str = "",
                            good: bool = False) -> dict:
        """Write down the user's word on how you did, when they give one.

        "That was wrong", "don't ping me about newsletters", "that summary
        was useless", or "good call": `what_they_said` in their words,
        `about` what it was about (the message, the ping, the reply), `good`
        true for praise. It goes into the ledger the night chooses its fixes
        from. Only what they said, never your own view of how you did.
        """
        from agent import issues

        kind = "feedback_up" if good else "feedback_note"
        issue = issues.record(ctx.deps.conn, kind, what_they_said[:120],
                              f"The user: {what_they_said[:160]}", excerpt=about[:500],
                              session_id=ctx.deps.session_id)
        return {"ok": issue is not None}
