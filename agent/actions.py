"""The action layer: how anything the agent does to the world is done.

Every effectful tool goes through `act`, which asks `core/consent.py`
whether it needs the user's yes: free actions run at once through `perform`
and are recorded in the ledger; gated ones become an approval card, and
run later -- through the same `perform` -- when the user answers it.

`perform` looks the tool up in `EXECUTORS`, which every toolset module
fills with `@executable` as it is imported. The executors live beside
the tools they serve, in `agent/toolset/<domain>.py`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic_ai import RunContext

from agent.turn import Deps

log = logging.getLogger("actions")


def present(deps: Deps) -> bool:
    """Whether the user is in this run: one of their own conversations, and
    no worker, routine, goal or module's routine. A tool that runs without a
    card where they can see it run is a card where they cannot: what a run
    with no one watching gives it may be what it read from their accounts
    (`agent/mcptools.py`, `agent/modtools.py`)."""
    from core.call_policy import is_autonomous

    return (deps.session_id is not None and not deps.worker and not is_autonomous(deps.channel)
            and deps.routine_id is None and deps.goal_id is None
            and deps.module_routine is None)


def routine_module(deps: Deps) -> tuple[str | None, bool]:
    """The key of the module whose routine this run is (None for any other
    run), and whether that module is on. A card the routine asks for carries
    the key, so turning the module off or removing it withdraws the card
    (`core/modules.withdraw_cards`); one asked once it is off or gone is not
    made at all, as its routine's next run would not be."""
    if deps.module_routine is None:
        return None, True
    module_id = deps.module_routine.rsplit(".", 1)[0]
    row = deps.conn.execute("SELECT key, enabled FROM modules WHERE id = ?",
                            (module_id,)).fetchone()
    return (row[0], bool(row[1])) if row else (None, False)


async def _critic(ctx, tool: str, args: dict, decided, preview: str):
    """An unattended session's action, reviewed by the action critic against
    what the user asked of the session (core/critic.py) -- on its own connection,
    off the loop. In shadow it only records; enforcing, an action it does not
    plainly allow becomes a card that says why. Their own conversations are
    not reviewed: the user is there, and anything outward already asks them."""
    from agent import approvals, offloop
    from core import critic, monitor
    from core.call_policy import is_autonomous
    from core.policy import load_policy

    if not is_autonomous(ctx.deps.channel):
        return decided, preview
    consent = decided.consent
    reviewed = await offloop.with_own_conn(
        ctx.deps.conn, critic.review, mandate=ctx.deps.mandate or "", channel=ctx.deps.channel,
        tool=tool, args=args, reach=str(consent.reach), reversal=str(consent.reversal),
        gate=decided.kind, session_id=ctx.deps.session_id,
        action_key=approvals._hash(tool, args))
    kind = critic.apply(decided.kind, reviewed, load_policy().critic.mode)
    if kind == decided.kind:
        return decided, preview
    why = (f"the critic judged it {reviewed.verdict} for what you asked"
           if reviewed.verdict else f"the critic could not review it ({reviewed.error})")
    # The reason first, where the user reads it before anything else. At the end
    # it was the first thing a card cut at 300 characters lost.
    return (monitor.Gate(kind, consent, why),
            f"(Asked because {why}.)\n{preview}")


async def _judged(ctx, tool: str, args: dict, decided, preview: str):
    """A card asked only because the account cannot write on condition, put
    to Jev (core/lostupdate.py), which may judge it unneeded. Only in the
    user's own conversations: a worker, a routine or a goal keeps what its
    envelope says, and an unattended session acts on what it read, which
    nobody vetted, so a judgement there would be the critic's to check."""
    from agent import offloop
    from core.call_policy import is_autonomous

    if (decided.kind != "card" or not decided.consent.unconditional or ctx.deps.worker
            or ctx.deps.routine_id is not None or ctx.deps.goal_id is not None
            or ctx.deps.module_routine is not None
            or is_autonomous(ctx.deps.channel)):
        return decided
    return await offloop.with_own_conn(ctx.deps.conn, _judge, decided, tool, args, preview,
                                       ctx.deps.session_id)


def _judge(conn, decided, tool: str, args: dict, preview: str, session_id: int | None):
    from core import lostupdate

    return lostupdate.judge(conn, decided, tool=tool, preview=preview,
                            made_here=_made_here(conn, session_id, args))


def _made_here(conn, session_id: int | None, args: dict) -> bool:
    """Whether an earlier action of this conversation answered with the
    item's id: a draft Steve saved, an event it made or moved. Blocking."""
    named = [v for k, v in args.items() if k.endswith("_id") and isinstance(v, str) and v]
    if session_id is None or not named:
        return False
    return any(conn.execute("SELECT 1 FROM actions WHERE session_id = ? AND result_json IS NOT"
                            " NULL AND instr(result_json, ?) > 0 LIMIT 1",
                            (session_id, json.dumps(native))).fetchone()
               for native in named)


def _accounts_known(conn) -> tuple[frozenset[str], frozenset[str]]:
    """What consent knows of the user's accounts: their own addresses, every
    mail account's and not Gmail's alone, and which accounts make their
    writes conditional (`core.consent.classify`). Blocking."""
    from agent import connectors

    return connectors.addresses(conn), connectors.conditional(conn)


# What an approved action calls when it finally runs. Kept beside the tools
# rather than in approvals.py so there is exactly one place that knows how to
# perform a thing, whether it was gated or not.
EXECUTORS: dict[str, Callable[..., Any]] = {}


def executable(name: str):
    """Register the real implementation behind a gated tool name."""

    def register(fn):
        EXECUTORS[name] = fn
        return fn

    return register


async def perform(tool: str, args: dict) -> dict:
    """Run an action: a free one now, or an approved one when its card is
    answered. The job handler calls this, possibly before any conversation
    has imported the toolset -- straight after a restart, say -- so it
    imports the toolset itself, which is what fills `EXECUTORS`.

    What the executor reports is then read back where it lives
    (agent/receipts.py): an action that said it worked and did not reports
    a failure, whichever way it was asked for."""
    import agent.toolset  # noqa: F401 - importing it registers every executor
    from agent import receipts
    from agent.approvals import GROUPED

    fn = EXECUTORS.get(tool)
    if fn is None:
        raise KeyError(f"no executor registered for {tool!r}")
    if isinstance(args.get(GROUPED), list):
        return await _each(tool, fn, args[GROUPED])
    return await receipts.attach(tool, args, await fn(**args))


async def _each(tool: str, fn: Callable[..., Any], group: list[dict]) -> dict:
    """A grouped card's items, each run and read back on its own: one that
    changed since the card was shown is skipped, named, and asked about
    again, and the others still go."""
    from agent import receipts
    from agent.approvals import reported_failure

    results, skipped, again = [], [], []
    for item in group:
        try:
            result = await receipts.attach(tool, item["args"], await fn(**item["args"]))
        except Exception as exc:  # noqa: BLE001 - one item's fault is that item's
            log.exception("an item of a grouped %s raised", tool)
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        results.append(result)
        if (failure := reported_failure(result)) is not None:
            skipped.append({"item": " ".join(str(item["preview"]).split())[:160],
                            "why": failure})
            if isinstance(result, dict) and result.get("again"):
                again.append(result["again"])
    done = len(group) - len(skipped)
    out: dict = {"ok": done > 0, "done": done, "of": len(group), "results": results}
    if skipped:
        out["skipped"] = skipped
    if not done:
        out["error"] = "none of them went: " + "; ".join(
            f"{s['item']}: {s['why']}" for s in skipped)
    if again:
        out["again"] = again
    return out


async def run_free(conn, tool: str, args: dict, *, consent, preview: str,
                   session_id: int | None = None, channel: str | None = None,
                   verdict=None) -> dict:
    """A free action, run now and written in the ledger, worked or not:
    `act`'s free path. `verdict` is the monitor's (`core.monitor.Gate`),
    which a send without a card needs (`core/writes.send_draft`)."""
    from agent import approvals

    try:
        with approvals.running_free(verdict):
            result = await perform(tool, args)
    except Exception as exc:  # noqa: BLE001 - reported to the caller and the ledger
        # Executors return a failure rather than raise (protocols §10),
        # and one that raised anyway took the whole turn with it: an
        # unguarded calendar create met the expired Google sign-in on
        # 27 September, and "add it to my calendar" was answered as if
        # the laptop were offline. A failure the model can read
        # and say is the contract; this makes it hold.
        log.exception("%s raised instead of reporting a failure", tool)
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                  "kind": "unexpected"}
    approvals.record_free(
        conn, tool=tool, args=args, consent=consent, preview=preview,
        result=result if isinstance(result, dict) else None,
        session_id=session_id, channel=channel,
    )
    return result


async def _gate(ctx: RunContext[Deps], tool: str, args: dict):
    """What `core/monitor` says this call needs, in this run."""
    from agent import offloop
    from core import monitor

    # In a thread: the first call of a process asks Gmail and Outlook who
    # the user is.
    me, conditional = await offloop.with_own_conn(ctx.deps.conn, _accounts_known)
    if ctx.deps.module_routine is not None:
        # A module's routine: its words, no envelope, so every action asks
        # (agent/modroutines.py).
        return monitor.module_gate(ctx.deps.conn, tool, args, me=me, conditional=conditional)
    if ctx.deps.routine_id is not None:
        # A routine's run: what a worker may do, plus what their approved
        # envelope allows; anything else asks (agent/routines.py).
        return monitor.routine_gate(ctx.deps.conn, ctx.deps.routine_id, tool, args, me=me,
                                    conditional=conditional)
    if ctx.deps.goal_id is not None:
        # A goal's work session: the same rule, its own envelope (agent/goals.py).
        return monitor.goal_gate(ctx.deps.conn, ctx.deps.goal_id, tool, args, me=me,
                                 conditional=conditional)
    return monitor.gate(ctx.deps.conn, tool, args, worker=ctx.deps.worker, me=me,
                        conditional=conditional)


async def asks(ctx: RunContext[Deps], tool: str, args: dict) -> bool:
    """Whether `act` would ask them before doing this: for a tool that puts
    what a card is about somewhere the user can read all of it, when there will be
    a card (`mail.send_email`). The monitor's view only -- enforcing, the
    critic can add a card, never take one away."""
    return (await _gate(ctx, tool, args)).kind == "card"


async def act(ctx: RunContext[Deps], tool: str, args: dict, preview: str) -> dict:
    """Do it, or ask first -- whichever `core/monitor.gate` says.

    Every effectful tool goes through here, including the free ones, so the
    ledger records what happened rather than only what was asked about. What
    to do is decided in the frozen core; this is the plumbing.

    `preview` is the card, shown whole: everything the yes agrees to, and
    never the body of a message it sends (`approvals.quoted_body`).
    """
    from agent import approvals

    # Once per turn, however many times the turn is run. `run_agent` retries
    # a failed request by running the whole agent again, and escalation
    # replays the turn on a stronger tier from the history before it -- so a
    # calendar event created on the first attempt was created again on the
    # second, and again on the escalated tier. The same effect asked for
    # again in the same turn gets the first result back.
    key = (tool, json.dumps(args, sort_keys=True, default=str))
    if key in ctx.deps.performed:
        return ctx.deps.performed[key]
    module_key, module_on = routine_module(ctx.deps)
    if not module_on:
        return {"ok": False, "kind": "refused",
                "error": "Not done: the module whose routine this is was turned off."}

    decided = await _gate(ctx, tool, args)
    consent = decided.consent
    if decided.kind == "refused":
        # The maintainer, 30 September 2026: workers "shouldn't be able to do any
        # irreversible/public impact stuff but drafting an email or putting
        # stuff on the calendar is fine". Refused, not carded: a worker
        # asking them to approve a send would be the public impact by
        # another door. The lead reads why, and can ask them itself.
        # Refused for everyone -- writing to the agent's own mailbox -- is
        # not a worker's limit, and saying it was would send a lead off to
        # ask for what nobody may do.
        result = ({"ok": False, "kind": "not_for_workers",
                   "error": f"A worker cannot do this ({decided.reason}). "
                            "Say in your answer what should be done, and the lead will decide."}
                  if ctx.deps.worker else
                  {"ok": False, "kind": "refused", "error": f"Not done: {decided.reason}."})
        ctx.deps.performed[key] = result
        return result
    decided = await _judged(ctx, tool, args, decided, preview)
    consent = decided.consent
    decided, preview = await _critic(ctx, tool, args, decided, preview)
    if decided.kind == "free":
        result = await run_free(ctx.deps.conn, tool, args, consent=consent, preview=preview,
                                session_id=ctx.deps.session_id, channel=ctx.deps.channel,
                                verdict=decided)
    else:
        result = await approvals.request(
            ctx.deps.conn, tool=tool, args=args, preview=preview, consent=consent,
            session_id=ctx.deps.session_id, channel=ctx.deps.channel,
            routine_module=module_key,
        )
    ctx.deps.performed[key] = result
    return result
