"""A module's tools: offered to the model, run in the sandbox, and asking
first where the grants say so.

Tools used to be written into `tools/` and run in the worker's own process,
admitted by a gate reading their imports: a judgement of what
code might reach, passed on code that then ran holding everything the
worker holds. One import the reading missed and a tool had the user's
accounts. A module's tool runs instead in its module's container, one
process per call (`core/sandbox/run.py`), reaching only what its grants
allow, through the broker. Here, each tool is:

- **offered** as `mod__<module>__<tool>`, from each module that is on, with
  the schema core builds from its manifest, so no module can take the name
  of one of core's tools or another module's. A module turned off leaves
  at the next run;
- **asked about first** where `core/lifecycle.needs_card` says, from the
  grants and never the tool's own word: the card runs the call on the
  user's yes (`module.tool`);
- **unable to change anything itself**: a call has GET and HEAD alone, and
  what the module would change it proposes through `ctx.act`. Each proposal
  is a card once the call ends, and on the yes the module's `perform` runs
  with exactly what the card showed, from the version the card named
  (`module.perform`);
- **screened**: what it returns is outside text, and the model reads only
  what the screen allows (`agent/screening.py`); an answer of numbers,
  true, false and null alone holds no words to screen;
- **watched**: each call counts for its module's watch, and a module that
  keeps failing is turned off and the user told (`agent/modadmit.watched`).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, FunctionToolset, RunContext, Tool

from agent import actions, modadmit, offloop, screening
from agent.actions import executable
from agent.turn import Deps
from core import lifecycle, modctx, modules, screen
from core.sandbox import run
from steve_sdk import calls
from steve_sdk import manifest as sdk_manifest
from steve_sdk.cut import cut

log = logging.getLogger(__name__)

PREFIX = "mod__"
TOOL = "module.tool"
PERFORM = "module.perform"
SOURCE = screen.ANSWER
MAX_RESULT = 20_000   # characters of what a call returns that are screened and shown
MAX_SHOWN = 300       # characters of one argument's value on a card


@dataclass(frozen=True)
class Offer:
    """One tool of a module that is on, as the model is offered it; with no
    tool, the module itself, for its `perform`."""

    module: str
    key: str
    version: str
    label: str
    manifest: sdk_manifest.Manifest
    tool: sdk_manifest.Tool | None = None

    @property
    def name(self) -> str:
        return name_of(self.module, self.tool.name if self.tool is not None else "perform")


def name_of(module_id: str, tool_name: str) -> str:
    """The name a module's tool is offered under: at most 64 characters, as
    a model's tool names are, from an id of 32 and a tool's of 25."""
    return f"{PREFIX}{module_id}__{tool_name}"


def free_builtins() -> set[str]:
    """The built-in modules' tools that change nothing and ask nothing: a
    read, from a module that reads none of the user's accounts and reaches
    its hosts with GET or HEAD alone (`lifecycle.needs_card`). A tool list
    fixed in code for runs no one watches (a worker's, the doer's) may hold
    these beside consent's free tools; an outside module's tools are not
    known until it is installed, so a fixed list cannot name them."""
    return {name_of(manifest.id, tool.name) for manifest in modules.builtins().values()
            for tool in manifest.tools
            if tool.klass == "read" and not lifecycle.needs_card(manifest, tool)}


def offers(conn: sqlite3.Connection) -> list[Offer]:
    """The tools of every module that is on, by module id and then the
    manifest's order. Blocking."""
    return [Offer(module=held.id, key=held.key, version=held.version,
                  label=held.manifest.label or held.id, manifest=held.manifest, tool=tool)
            for held in modules.modules(conn)
            if held.enabled and held.manifest is not None and held.tier == "sandbox"
            for tool in held.manifest.tools]


def _offer(conn: sqlite3.Connection, module_id: str, key: str, version: str,
           tool: str | None = None) -> Offer:
    """The module a card named, as it is now: Refused unless it is on, is
    the same install (`key`) and runs the same version. A yes given to one
    version's card is not a yes to the next's."""
    held = run.installed(conn, module_id)
    if held.key != key or held.version != version or held.manifest is None:
        raise lifecycle.Refused(f"{module_id} has changed since you were asked: ask again")
    found = next((t for t in held.manifest.tools if t.name == tool), None)
    if tool is not None and found is None:
        raise lifecycle.Refused(f"{module_id} has no tool {tool!r} now")
    return Offer(module=held.id, key=held.key, version=held.version,
                 label=held.manifest.label or held.id, manifest=held.manifest, tool=found)


# --- the call, in the sandbox ------------------------------------------------------

@dataclass
class Ran:
    """How one call went: what it returned, or the error the model is told,
    and the actions it proposed, each still to be asked about."""

    result: Any = None
    error: str | None = None
    ended: str | None = None
    proposed: list[tuple[str, dict]] = field(default_factory=list)


def _call(conn: sqlite3.Connection, offer: Offer, method: str, params: dict, *,
          perform: bool = False) -> Ran:
    """Run one call of `offer`'s module in the sandbox. Blocking: on a
    connection of the thread's own. A fault that is not the module's (no
    sandbox here, the module off or changed, the computer refusing a
    grant) is raised; the module's own is returned, and counted."""
    from agent import modreads

    ran = Ran()

    def actor(action: str, args: dict) -> dict:
        ran.proposed.append((action, args))
        return dict(calls.PROPOSED)

    try:
        ran.result = run.call(
            conn, offer.manifest, method, params, perform=perform,
            reader=modreads.reader(conn) if offer.manifest.reads else None,
            # `perform` runs what a card approved, and proposes nothing more.
            actor=None if perform else actor, redact=modctx.scrub)
    except (run.NotHeld, run.NotStarted, lifecycle.Refused):
        raise
    except Exception as exc:  # noqa: BLE001 - the module's fault, told and counted
        ran.error = f"{type(exc).__name__}: {exc}"[:300]
        ran.ended = lifecycle.outcome(exc, timed_out=isinstance(exc, run.TimedOut))
        ran.proposed.clear()
        return ran
    ran.ended = lifecycle.outcome()
    return ran


async def _running(conn: sqlite3.Connection, offer: Offer, method: str, params: dict, *,
                   perform: bool = False) -> Ran | dict:
    """`_call` off the event loop, its module's watch told how it ended; a
    failure that is not the module's comes back as what to say."""
    try:
        ran = await offloop.with_own_conn(conn, _call, offer, method, params, perform=perform)
    except run.NotHeld as exc:
        return {"ok": False, "kind": "not_set_up",
                "error": f"modules cannot run on this computer: {exc}"}
    except (lifecycle.Refused, run.NotStarted) as exc:
        return {"ok": False, "error": f"{offer.module}: {exc}"[:300]}
    await modadmit.watched(conn, offer.module, ran.ended, _notifier(conn))
    return ran


def _notifier(conn: sqlite3.Connection) -> Callable[[str], Awaitable[Any]]:
    from agent import outbox

    async def notify(text: str) -> Any:
        return await offloop.with_own_conn(conn, outbox.add, text, kind="notice")

    return notify


async def _shown(conn: sqlite3.Connection, offer: Offer, ran: Ran) -> dict:
    """What the model is told of a finished call: the module's answer, or
    its error, as the screen allows it.

    A module chooses what its error says as freely as its answer, and the
    error went to the model unscreened, without marking the conversation
    as having read outside text (step 11's audit)."""
    if ran.error is not None:
        seen = await screening.shown(conn, SOURCE, offer.name, offer.module, ran.error)
        told = {"ok": False, "error": f"{offer.label}: {seen.text}"}
        return told if seen.how == "text" else {**told, "screened": seen.how}
    whole = json.dumps(ran.result, ensure_ascii=False, default=str)
    text = cut(whole, MAX_RESULT)
    if text == whole and _wordless(ran.result):
        # Asked about an answer with no words in it, the screen's model has
        # nothing to judge and guesses. In the self-upgrade trial (19 and 20
        # October 2026) a weekend check's answer was withheld from one chat
        # in three; asked again, it leaned to "written mainly to manipulate
        # an AI" for a bare `true` (0.47 against 0.38 for passing it).
        return {"ok": True, "result": ran.result}
    seen = await screening.shown(conn, SOURCE, offer.name, offer.module, text)
    if seen.how != "text":
        return {"ok": True, "screened": seen.how, "result": seen.text}
    # Passed whole, the answer goes as the module gave it; cut, as text.
    return {"ok": True, "result": ran.result if text == whole else text}


def _wordless(value: Any) -> bool:
    """Whether an answer holds no text at all: numbers, true, false and
    null, alone or in lists. An object's keys are text the module chose, so
    an object is screened."""
    if value is None or isinstance(value, bool | int | float):
        return True
    return isinstance(value, list) and all(_wordless(v) for v in value)


# --- cards ---------------------------------------------------------------------------

def _value(value: Any) -> str:
    said = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                           default=str)
    return cut(" ".join(said.split()), MAX_SHOWN)


def listing(args: dict) -> str:
    return "\n".join(f"- {name}: {_value(value)}" for name, value in args.items()) or "- nothing"


def _card(offer: Offer, **what: Any) -> dict:
    """A card's arguments: the module as installed when it was asked, by
    id, install and version, and what the yes runs. `module_key` ties the
    card to the module, so turning it off or removing it withdraws the card
    (`core/modules.withdraw_cards`)."""
    return {"module": offer.module, "module_key": offer.key, "version": offer.version, **what}


def tool_card(offer: Offer, args: dict) -> str:
    """A tool call's card, in core's words, every argument shown."""
    assert offer.tool is not None
    reads = lifecycle.summary(offer.manifest).lines[:2]
    return (f"{offer.label} (a module, version {offer.version}) would run its tool "
            f"{offer.tool.name}, with:\n{listing(args)}\n\n" + " ".join(reads))


def perform_card(offer: Offer, action: str, args: dict) -> str:
    """A proposed action's card, in core's words: the module, the action
    as it named it, and every argument, so the yes covers what it runs."""
    return (f"{offer.label} (a module, version {offer.version}) asks to "
            f"{action.replace('_', ' ')}, with:\n{listing(args)}")


async def _ask(conn: sqlite3.Connection, offer: Offer, proposed: list[tuple[str, dict]],
               ctx: RunContext[Deps] | None) -> list[dict]:
    """A card for each action a call proposed: through `actions.act` in a
    turn, which writes the ledger and keeps a worker from asking; from a
    card's own run, queued as `act` would queue it."""
    from agent import approvals
    from core import consent

    asked = []
    for action, args in proposed:
        what = _card(offer, action=action, args=args)
        preview = perform_card(offer, action, args)
        if ctx is not None:
            asked.append(await actions.act(ctx, PERFORM, what, preview))
        else:
            asked.append(await approvals.request(conn, tool=PERFORM, args=what,
                                                 preview=preview,
                                                 consent=consent.classify(PERFORM, what)))
    return asked


# --- in a turn -----------------------------------------------------------------------

async def use(ctx: RunContext[Deps], offer: Offer, args: dict) -> dict:
    """One call of a module's tool, as the model made it."""
    assert offer.tool is not None
    ctx.deps.tools_used.append(offer.name)
    if (why := offer.tool.problems(args)) is not None:
        return {"ok": False, "error": f"{offer.name}: {why}"}
    # A tool free in the user's own conversation is a card with no one
    # watching: its arguments go to the module's code, which may send them
    # to its hosts, and a routine's run may fill them with the user's mail.
    if lifecycle.needs_card(offer.manifest, offer.tool) or not actions.present(ctx.deps):
        return await actions.act(ctx, TOOL, _card(offer, tool=offer.tool.name, args=args),
                                 tool_card(offer, args))
    ran = await _running(ctx.deps.conn, offer, "tool", {"name": offer.tool.name, "args": args})
    if isinstance(ran, dict):
        return ran
    told = await _shown(ctx.deps.conn, offer, ran)
    if ran.proposed:
        told["asked"] = await _ask(ctx.deps.conn, offer, ran.proposed, ctx)
    return told


def _tool(offer: Offer) -> Tool[Deps]:
    assert offer.tool is not None

    async def call(ctx: RunContext[Deps], **args: Any) -> dict:
        return await use(ctx, offer, args)

    return Tool.from_schema(call, name=offer.name, description=offer.tool.description,
                            json_schema=offer.tool.schema(), takes_ctx=True)


def register(agent: Agent[Deps, str], allow: set[str] | None = None) -> None:
    """Offer the tools of the modules that are on, read as each run starts,
    and narrowed by `allow` as core's tools are."""

    @agent.toolset(per_run_step=False)
    async def module_tools(ctx: RunContext[Deps]) -> FunctionToolset[Deps]:
        conn = getattr(ctx.deps, "conn", None)
        if conn is None:  # a run with no database has no modules
            return FunctionToolset([])
        try:
            found = await offloop.with_own_conn(conn, offers)
        except sqlite3.Error:
            # A registry that cannot be read is no reason to lose the turn.
            log.exception("the modules' tools could not be read")
            found = []
        return FunctionToolset([_tool(o) for o in found if allow is None or o.name in allow])


# --- on a card's yes -----------------------------------------------------------------

def _store() -> tuple[sqlite3.Connection, bool]:
    from agent.toolset.clients import commitment_store

    return commitment_store()


@executable(TOOL)
async def _do_tool(*, module: str, module_key: str, version: str, tool: str,
                   args: dict) -> dict:
    """A tool call the user approved, run now as the card showed it."""
    conn, owned = _store()
    try:
        try:
            offer = await offloop.with_own_conn(conn, _offer, module, module_key, version, tool)
        except lifecycle.Refused as exc:
            return {"ok": False, "error": str(exc)}
        ran = await _running(conn, offer, "tool", {"name": tool, "args": args})
        if isinstance(ran, dict):
            return ran
        told = await _shown(conn, offer, ran)
        if ran.proposed:
            told["asked"] = await _ask(conn, offer, ran.proposed, None)
        return told
    finally:
        if owned:
            conn.close()


@executable(PERFORM)
async def _do_perform(*, module: str, module_key: str, version: str, action: str,
                      args: dict) -> dict:
    """An action the user approved: the module's `perform`, with exactly
    the action and arguments the card showed, from the version it named."""
    conn, owned = _store()
    try:
        try:
            offer = await offloop.with_own_conn(conn, _offer, module, module_key, version)
        except lifecycle.Refused as exc:
            return {"ok": False, "error": str(exc)}
        ran = await _running(conn, offer, "perform", {"action": action, "args": args},
                             perform=True)
        if isinstance(ran, dict):
            return ran
        return await _shown(conn, offer, ran)
    finally:
        if owned:
            conn.close()
