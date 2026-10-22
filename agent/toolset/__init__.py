"""The model's toolset: what it can call, and where it may call it.

One module per domain, each holding both halves of that domain: the
`@tool` functions the model sees, and the `@executable` functions that
actually act when a tool -- or an approval card, later -- says to.
"""

from __future__ import annotations

import functools
import logging

from pydantic_ai import Agent, RunContext
from pydantic_ai.tools import ToolDefinition

from agent import handles, mcptools, modtools, screening
from agent.toolset import (
    browser,
    calendar,
    code,
    control,
    desk,
    drive,
    goals,
    linkedin,
    mail,
    memory,
    news,
    notes,
    pace,
    people,
    reminders,
    routines,
    skills,
    spending,
    weather,
    web,
    workers,
)
from agent.turn import Deps
from core.call_policy import PRESENT_CHANNELS

log = logging.getLogger("toolset")


def _recorded(fn):
    """`fn`, a tool, noting its name in the turn's `tools_used` when it runs.

    That list is what the turn did: the answer check (`_verified` in
    agent/loop.py) lets "Added it to your calendar" stand only when
    `create_event` is on it, and the turn's record and the attention loop
    read it too. Each tool appended its own name as its first
    line until 11 October 2026 -- eighty-eight copies of one line, where a
    tool that left it out, or misspelled its own name, did things the check
    could not see.

    Innermost of the wrappers, so -- as when each tool noted itself -- a
    call that never reaches the tool is not noted: one refused for a handle
    that names nothing (agent/handles.py), or a lookup answered from the
    turn's earlier one (agent/toolset/pace.py).
    """

    @functools.wraps(fn)
    async def recorded(ctx: RunContext[Deps], *args, **kwargs):
        ctx.deps.tools_used.append(fn.__name__)
        return await fn(ctx, *args, **kwargs)

    return recorded


def _read_through(fn):
    """`fn`, a tool, marking the conversation as having read outside text
    after each call, when what it returns can carry it
    (`screening.read_through`).

    Each tool marked the conversation by hand, through the screen, until
    step 11's audit found Drive's file names and the people ledger's names
    and subjects reaching a watched chat without it. Now the mark follows
    from the tool's declaration (`screening.OUTSIDE`), whatever the tool
    did. In a `finally`: a call that fails may have read already, and
    pydantic-ai hands a `ModelRetry`'s words to the model.
    """

    @functools.wraps(fn)
    async def read(ctx: RunContext[Deps], *args, **kwargs):
        try:
            return await fn(ctx, *args, **kwargs)
        finally:
            screening.read_through(fn.__name__)

    return read


def not_set_up(exc: BaseException) -> str | None:
    """What a tool's exception says is missing on this computer, or None
    for any other failure: a credential not stored, an account not
    connected, Docker not running, an add-on not installed, a model that
    cannot load."""
    from core.secrets import MissingSecret
    from steve_sdk.errors import NotConnected
    from tools.code_exec import DockerUnavailable
    from tools.memory import EmbedderUnavailable

    if isinstance(exc, ModuleNotFoundError):
        return f"{exc.name or 'a module it needs'} is not installed here"
    if isinstance(exc, MissingSecret | DockerUnavailable | NotConnected | EmbedderUnavailable):
        # The message as given: str() of a KeyError, as MissingSecret is,
        # wraps it in quotes.
        said = str(exc.args[0]) if exc.args else type(exc).__name__
        return " ".join(said.split())[:300]
    return None


def _set_up(fn):
    """`fn`, a tool, answering for what is not set up rather than raising.

    A tool that raised for a missing key, account or add-on broke the whole
    turn: "Something broke on my side", with a traceback in the log and
    nothing the model could tell the user. Now it is a result the model
    reads and relays ("Gmail is not connected"). Anything else still
    raises: a fault is not a setting.
    """

    @functools.wraps(fn)
    async def set_up(ctx: RunContext[Deps], *args, **kwargs):
        try:
            return await fn(ctx, *args, **kwargs)
        except Exception as exc:
            if (missing := not_set_up(exc)) is None:
                raise
            log.info("%s: not set up: %s", fn.__name__, missing)
            return {"error": f"not set up: {missing}", "kind": "not_set_up"}

    return set_up


def register_tools(agent: Agent[Deps, str], allow: set[str] | None = None, *,
                   lead: bool = False, off: frozenset[str] = frozenset()) -> None:
    """Attach the toolset.

    Identical across roles, so an escalated turn has exactly the same
    capabilities as the turn it replaced. `allow` narrows it for a run that
    should use less: a goal's work session, a routine's run.

    Tools are filtered at registration rather than refused when called, so a
    model running somewhere reduced never sees a tool it cannot use. Offering
    `send_email` and then failing wastes a turn and reads as a malfunction.

    `lead` adds the worker tools (agent/toolset/workers.py): only a lead
    with a crew gets them -- a background job's, the doer's or the night's
    research turn -- and never a worker (agent/workers.py).

    `off` are the tools of the features this user has not set up
    (`features.tools_off`): no mail tools without a mailbox.

    The modules' tools come last, read as each run starts from the modules
    that are on (`agent/modtools.py`), and their MCP servers' with them
    (`agent/mcptools.py`).
    """

    def tool(fn):
        # A lead is a background task, where deferring is always refused
        # (`defer_to_background`): offered it, the first live lead tried it
        # before anything else, and wasted the call.
        if lead and fn.__name__ in LEAD_NEVER:
            return fn
        if fn.__name__ in off:
            return fn
        if fn.__name__ in ASKED_FOR_ONLY and (allow is None or fn.__name__ not in allow):
            return fn
        if allow is None or fn.__name__ in allow:
            # Every tool takes and hands back short handles for long ids
            # (agent/handles.py): a model cannot be trusted to copy a
            # 156-character Contoso message id back exactly.
            # Run in their group's turn, answered from the turn's lookups
            # when asked again (agent/toolset/pace.py). Noted as used when
            # it runs (`_recorded`), and answering for what is not set up
            # rather than breaking the turn (`_set_up`).
            # A write is done once a turn however often the turn is run
            # (`pace.once`). A call of one whose results carry outside text
            # marks the conversation (`_read_through`).
            agent.tool(pace.wrap(handles.wrap(_recorded(_read_through(
                pace.once(_set_up(fn)))))))
        return fn

    web.register(tool)
    memory.register(tool)
    notes.register(tool)
    desk.register(tool)
    code.register(tool)
    mail.register(tool)
    calendar.register(tool)
    drive.register(tool)
    people.register(tool)
    news.register(tool)
    weather.register(tool)
    control.register(tool)
    reminders.register(tool)
    routines.register(tool)
    goals.register(tool)
    spending.register(tool)
    skills.register(tool)
    browser.register(tool)
    linkedin.register(tool)
    if lead:
        workers.register(tool)
    modtools.register(agent, allow)
    mcptools.register(agent, allow)


async def within_budget(ctx: RunContext[Deps], tools: list[ToolDefinition]) -> list[ToolDefinition]:
    """Every tool, until the run's budget of calls is spent (`Deps.tool_budget`);
    then none, so the next request is one the model can only answer. Given to
    every agent `build_agent` makes, as a `PrepareTools` capability.

    The hard limit stopped a run where it stood and threw away what it had
    found. On 30 September all four Northwind workers in two jobs reached their 16
    calls rewording one search against sites that refused every page, and
    each reported only "went round in tool calls and was stopped" -- the
    profiles their searches had turned up never reached the lead. Taking the
    tools away instead lets the run end the way runs end: with an answer.
    The hard limit stays above this, for a model that asks for many at once.
    """
    budget = getattr(ctx.deps, "tool_budget", None)  # a run with no Deps has no budget
    if budget is not None and ctx.usage.tool_calls >= budget:
        return []
    return tools


async def offered(ctx: RunContext[Deps], tools: list[ToolDefinition]) -> list[ToolDefinition]:
    """What the model is shown this request: no browser tools where they
    would only refuse -- the browser off, or a run nobody is watching
    (`toolset.browser.refusal`) -- and, where they are shown, their saved
    logins named in `browse_open` (`toolset.browser.with_saved_logins`); no
    crew tool its crew would refuse, and nothing at all once the run's
    budget is spent (`within_budget`). What `build_agent` gives every agent."""
    deps = ctx.deps
    if not isinstance(deps, Deps) or browser.refusal(deps):
        tools = [t for t in tools if t.name not in browser.NAMES + linkedin.NAMES]
    else:
        tools = await browser.with_saved_logins(tools, deps)
        # LinkedIn's own, only while a saved login covers linkedin.com.
        tools = await linkedin.offered(tools, deps)
    if not isinstance(deps, Deps) or deps.channel != "voice" or not desk.live_by_assistant():
        # A live conversation can only open at the desk, and their assistant
        # opens one only under decision A (voice/engine.py `LIVE_OPENED_BY`);
        # the user's choice, 9 October 2026, is that the user asks for one by name.
        tools = [t for t in tools if t.name not in desk.NAMES]
    if not isinstance(deps, Deps) or deps.channel not in PRESENT_CHANNELS:
        # Notes hold other people's words: started when the user asks, never by
        # work nobody is watching.
        tools = [t for t in tools if t.name not in desk.NOTE_NAMES]
    crew = getattr(deps, "crew", None)
    if crew is not None:
        # A lead is shown what its crew may do: workers only for a task that
        # splits, browsing workers only where its crew may browse.
        hidden = ({"start_worker"} if not crew.split else set()) | (
            {"start_browsing_worker"} if not crew.browsing else set())
        tools = [t for t in tools if t.name not in hidden]
    return await within_budget(ctx, tools)


# Not for a background task's lead: nobody is waiting, so there is nothing
# to defer to.
LEAD_NEVER = frozenset({"defer_to_background"})

# Offered only to a run that names it in `allow`: a goal's work session
# (agent/goals.toolset). Their conversations get every tool, and one that
# could only ever refuse there is a wasted call waiting to happen.
ASKED_FOR_ONLY = frozenset({"goal_update"})
