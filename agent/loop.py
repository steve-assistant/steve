"""The conversation loop: one turn at a time, its escalation, and what is kept.

The routing idea this implements: the fast model is always the front door. It
answers most turns itself, and when it cannot, it says so by calling a tool
rather than by failing. `escalate` re-runs the turn with reasoning enabled;
`defer_to_background` queues the work and lets the conversation continue.

That costs nothing on the common path -- no classifier call, no extra round
trip -- which is why triage is a tool rather than a separate model.

What the model can call lives elsewhere: `agent/toolset/` holds one module per
domain (mail, calendar, phone, ...), each with the tools the model sees and
the executors that act for them; `agent/actions.py` is the one door every
effect goes through; `agent/turn.py` is what a turn carries between them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import RunUsage, UsageLimits

from agent import accounts, caps, closing, context, features, issues, offloop, screening
from agent.cut import cut
from agent.features import Enabled
from agent.history import CUT_NOTE, UNPROMPTED, clean, compact
from agent.llm import CallRecord, record_for, run_agent
from agent.models import MODELS, Role
from agent.provider import key_missing, model_for, settings_for
from agent.toolset import offered, pace, register_tools
from agent.turn import Deps, EscalationRequested
from channels import photos
from core import db, language, spend
from core.call_policy import PRESENT_CHANNELS

# Google is imported inside the tools that use it, not here: a user with no
# Google account has no use for its client libraries.

log = logging.getLogger("loop")


# CONVERSE -> REASON -> ESCALATE. Two hops is the cap: a third would mean the
# model has asked for help twice and still cannot proceed, which is a signal to
# surface the difficulty rather than spend more money on it.
MAX_ESCALATIONS = 2

# Per-hop ceiling on model round trips. The outer replay loop bounds how many
# *tiers* a turn can climb; this bounds how much a single tier can spend
# looping on tools. Without it a model that keeps calling the same tool runs
# until pydantic-ai's default limit, which on the top tier is real money.
#
# Two ceilings. At `HOP_TOOL_CALLS` the tools are taken away and the model
# answers from what it found (`toolset._within_budget`); the hard limit sits
# `TOOL_BURST` above, because one response can ask for several calls at once
# and only a run past both is stopped with nothing to show.
HOP_TOOL_CALLS = 20
TOOL_BURST = 8
HOP_USAGE_LIMITS = UsageLimits(request_limit=25, tool_calls_limit=HOP_TOOL_CALLS + TOOL_BURST)

# How much of a tool's return value is kept. Sized by what it is for rather
# than by taste: the grounding check matches an answer's claims against these
# rows, so anything cut here comes back as a claim the sources do not support.
# Three thousand was the first guess and a week's calendar blew past it, which
# turned a correctly grounded agenda into six false "unsupported" labels.
MAX_TOOL_RESULT_CHARS = 12_000
# A call's arguments, kept beside its result. A cut one is no longer JSON,
# and the replay then falls back to none at all (agent/models_eval.py) -- so
# long ones are shortened value by value instead (`_args_kept`).
MAX_TOOL_ARGS_CHARS = 4_000
# What each long string in an over-long call keeps.
ARG_VALUE_CHARS = 600


def _args_kept(call) -> str | None:
    """A call's arguments as stored: whole if they fit, else with each long
    value cut (agent/cut.py) so they are still JSON. Every `send_email` with
    a body over about 4,000 characters was stored cut mid-string, and its
    replay was a call with no arguments at all."""
    text = call.args_as_json_str()
    if len(text) <= MAX_TOOL_ARGS_CHARS:
        return text

    def shorter(value):
        if isinstance(value, str):
            return cut(value, ARG_VALUE_CHARS)
        if isinstance(value, dict):
            return {key: shorter(inner) for key, inner in value.items()}
        if isinstance(value, list):
            return [shorter(inner) for inner in value]
        return value

    try:
        kept = json.dumps(shorter(call.args_as_dict()), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return kept if len(kept) <= MAX_TOOL_ARGS_CHARS else None

# How much of a conversation survives a worker restart. The user asked for "a
# 24 hour rolling window", and the caps beside it are what keep that from
# becoming an unbounded prompt: every replayed message is billed on every
# subsequent turn, and a day of heavy use would otherwise arrive as one
# enormous uncached prefix.
HISTORY_WINDOW_HOURS = 24
HISTORY_MAX_MESSAGES = 30
HISTORY_MAX_CHARS = 4_000
# Unprompted messages replayed besides those, the latest first. They used to
# count among the thirty: in the window resumed at 23:52 on 7 October 2026,
# six of the thirty were pings, and their own words from that morning were out.
HISTORY_MAX_UNPROMPTED = 8

# What a turn that broke says, to them and into the history. It does not say
# "nothing was done": the break can come after a tool already acted.
BROKE = (
    "Something broke on my side before I could finish that ({error}). "
    "Say “try again” and I'll pick it up."
)

# What a turn says when its model has no key. A step of setting up rather
# than a fault: no issue is recorded, and it says what to do.
NO_KEY = (
    "I can't answer yet: there is no {key} for the model I use. "
    "Set it with `python -m core.secrets set {key}`, then ask again."
)


class TurnFailed(RuntimeError):
    """A turn that broke, already recorded in its conversation.

    Raised rather than returned, because a background job counts on a raise
    to be retried. `text` is what to tell them instead of the raw exception.
    """

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


@dataclass
class Reply:
    text: str
    role: Role
    escalated_from: Role | None
    escalation_reason: str | None
    deferred_jobs: list[int]
    tools_used: list[str]
    cost_usd: float
    latency_ms: int
    cached_tokens: int

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "role": self.role.value,
            "escalated_from": self.escalated_from.value if self.escalated_from else None,
            "escalation_reason": self.escalation_reason,
            "deferred_jobs": self.deferred_jobs,
            "tools_used": self.tools_used,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": self.latency_ms,
            "cached_tokens": self.cached_tokens,
        }


def system_prompt(on: Enabled | None = None) -> str:
    """The house prompt: the core and the sections of the features `on`
    (agent/features.py), or of every feature when `on` is None."""
    return features.assemble(features.everything() if on is None else on)


def cache_key_for(channel: str) -> str:
    """The prompt-cache key a conversation on `channel` sends.

    One per channel, not one per conversation. OpenAI routes a request by
    its key, and caches what that route has seen: a conversation with a key
    of its own started on a cache that had never seen the ~14,000 tokens of
    instructions and tool schemas every turn begins with. Measured 6 October
    2026 on the real prefix: a new conversation's first request read 0% from
    cache under a fresh key, and 100% under a key an earlier conversation had
    used -- the first turn of every job, doer task, night topic, routine and
    goal, and of their first message each day. A channel is far below the ~15
    requests a minute OpenAI suggests per key, so sharing one costs nothing.
    """
    return f"steve-{channel}"


def _now_line() -> str:
    """Tell the model what time it is, in the user's timezone.

    Without this it guesses, and a calendar assistant that guesses the date
    resolves "tomorrow afternoon" against the wrong day -- which it did, on
    the first live test, silently and with a confident answer.

    Regenerated per turn rather than baked into the prompt file: a long
    conversation would otherwise drift, and a session left open overnight
    would be a day out.
    """
    import datetime as dt

    from agent import zones

    return now_line_at(dt.datetime.now(zones.local_zone()))


def now_line_at(now) -> str:
    """`_now_line` for a given moment, local and aware -- the time a past
    turn was asked, when the model scout replays it (agent/models_eval.py).
    A separate function because pydantic-ai calls an instruction with a
    parameter as if the parameter were its RunContext."""
    import datetime as dt

    return (
        f"Right now it is {now.strftime('%A %d %B %Y, %H:%M')} "
        f"({now.tzname()}, {now.strftime('%Y-%m-%dT%H:%M')} local). "
        f"Today is {now.date().isoformat()}; tomorrow is "
        f"{(now.date() + dt.timedelta(days=1)).isoformat()}. "
        "Resolve every relative date against this, and pass absolute "
        "YYYY-MM-DD dates to tools rather than words like 'tomorrow'."
    )


# How long a turn waits for the facts their message calls for. Linking them
# is SQLite; embedding the message takes tens of milliseconds once the model
# is loaded, and loading it about three seconds. Past this, the turn goes on
# without them rather than keep them waiting.
FACTS_WAIT_S = 4.0


async def _about_user_instruction(ctx: RunContext[Deps]) -> str:
    """`_about_user`, as an instruction: async, so it runs on the loop's
    thread where `deps.conn` lives (see `_skills_instruction`)."""
    return _about_user(ctx)


def _about_user(ctx: RunContext[Deps]) -> str:
    """Who the user is, in every turn, not only when the model thinks to `recall`:
    the profile (agent/facts.py `profile`) -- five facts or fewer, about them,
    in their words or confirmed by them, none past its last day. In a stable
    order after the house prompt, so it stays in the cached prefix until a
    fact changes.

    It was every `kind = 'user'` memory until 10 October 2026, and one of
    them, "Owen Tate's Northwind email format is lastname.firstname@northwind.example",
    sat in front of every turn on every channel for two days (E17): a firm's
    format kept as a fact about the user. A fact now says whom it is about."""
    from agent import facts

    deps = getattr(ctx, "deps", None)
    if not isinstance(deps, Deps) or deps.conn is None:
        return ""
    return facts.profile_text(deps.conn)


async def _skills_instruction(ctx: RunContext[Deps]) -> str:
    """`_skills_line`, as an instruction. Async so that pydantic-ai runs it
    on the loop's thread, where `deps.conn` lives. Given the plain function,
    it ran it in a worker thread, and every turn from the day skills shipped
    (5 October 2026) logged "could not list the skills": SQLite refuses a
    connection from another thread, so no skill was ever offered."""
    return _skills_line(ctx)


async def _modules_instruction(ctx: RunContext[Deps]) -> str:
    """`_modules_line`, as an instruction: async, as `_skills_instruction`
    is, so it reads `deps.conn` on the loop's thread."""
    return _modules_line(ctx)


def _modules_line(ctx: RunContext[Deps]) -> str:
    """The sections the modules that are on add to the instructions
    (agent/modtext.py), each in a block naming its module, in the user's own
    conversations only, as skills are: an unattended run is not steered by
    a module's words. Right after the house prompt, so the cached prefix
    holds until a module is switched."""
    from agent import modtext
    from core.call_policy import is_autonomous

    deps = getattr(ctx, "deps", None)
    if not isinstance(deps, Deps) or deps.conn is None or is_autonomous(deps.channel):
        return ""
    try:
        return modtext.sections(deps.conn)
    except Exception:  # noqa: BLE001 - a turn is never lost to a module's words
        log.warning("could not read the modules' sections", exc_info=True)
        return ""


def _skills_line(ctx: RunContext[Deps]) -> str:
    """The skills kept (agent/skills.py), a line each, in their own
    conversations only: an unattended run is never offered one. After the
    house prompt, so it stays in the cached prefix -- it changes only when a
    skill is kept or revoked."""
    from agent import skills
    from core.call_policy import is_autonomous

    deps = getattr(ctx, "deps", None)
    if not isinstance(deps, Deps) or deps.conn is None or is_autonomous(deps.channel):
        return ""
    try:
        return skills.index(deps.conn)
    except Exception:  # noqa: BLE001 - a turn is never lost to its skills
        log.warning("could not list the skills", exc_info=True)
        return ""


def build_agent(
    role: Role,
    *,
    cache_key: str | None = None,
    allow: set[str] | None = None,
    note: str | None = None,
    lead: bool = False,
    clock: bool = True,
    on: Enabled | None = None,
) -> Agent[Deps, str]:
    """Construct an agent for a role, with the full toolset attached, and
    the worker tools too for a background job's `lead` (agent/workers.py).

    The house prompt is assembled for the features `on`, and `note` is
    appended to it. The tools of a feature not `on` are not attached
    (agent/features.py); `on` None is every feature, the whole agent.

    `clock=False` leaves the time out of the instructions, for a caller that
    puts it in the prompt instead (`Conversation.send`): instructions come
    before the whole history, so a line that changes each minute there means
    no turn's history is ever read from the cache.
    """
    model = model_for(role)
    prompt = system_prompt(on)
    if note:
        prompt += note
    agent = Agent(
        model,
        deps_type=Deps,
        # `instructions`, never `system_prompt`. pydantic-ai sends a system
        # prompt only on a run with no message history, and `_resume` reloads
        # history after every worker restart -- so every conversation that
        # outlived a swap, a crash or a sleep went on with no system.md at
        # all, and a turn at the desk with no VOICE_NOTE. Instructions go
        # with every request.
        #
        # The house prompt first and the clock last: the prompt is identical
        # on every turn, so it stays the cached prefix, and only the line
        # that changes each minute falls outside it. The other way round,
        # which is how it was, the cache broke at the very first token.
        # The language next: it changes only when the user changes it.
        instructions=[prompt, language.instruction, _modules_instruction,
                      _about_user_instruction, _skills_instruction,
                      *([_now_line] if clock else [])],
        model_settings=settings_for(MODELS[role], cache_key=cache_key),
        retries=2,
        # Its tools taken away once `Deps.tool_budget` is spent, so a long
        # search ends in an answer rather than a stop (`within_budget`), and
        # the browser's hidden where it would only refuse (`offered`).
        capabilities=[PrepareTools(offered)],
    )
    register_tools(agent, allow=allow, lead=lead, off=features.tools_off(on))
    return agent


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

# Where a turn nobody is waiting on starts: the same model thinking first.
# Every turn used to start at CONVERSE, no reasoning, which is right for a
# reply the user is watching arrive and wrong for the doer, a routine's run or a
# goal's session, which have minutes, act unasked, and are where "Drafted a
# reply offering their class times as free" came from. Over the week to 6
# October 2026 those channels cost $0.19 between them, at no reasoning;
# reasoning tokens are worth spending there.
UNATTENDED = Role.REASON


def streamed(on_text, on_tool):
    """An event handler for a run that is heard as it is written: the reply's
    text as it comes (`on_text`), and each tool as the model reaches for it
    (`on_tool`) -- the voice speaks the first and says a word about the
    second (voice/speaking.py). Either may be None. A callback that fails is
    logged and the turn goes on: the reply matters more than its voice."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        PartDeltaEvent,
        PartStartEvent,
        TextPart,
        TextPartDelta,
    )

    def call(fn, value) -> None:
        if fn is None or not value:
            return
        try:
            fn(value)
        except Exception:  # noqa: BLE001
            log.exception("a streaming callback failed")

    async def handle(ctx, events) -> None:
        async for event in events:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                call(on_text, event.part.content)
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                call(on_text, event.delta.content_delta)
            elif isinstance(event, FunctionToolCallEvent):
                call(on_tool, event.part.tool_name)

    return handle


def _with(words: str, pictures: list) -> str | list:
    """The turn's prompt: its words alone, or its words and then the photos
    that came with them (`photos.load`)."""
    return [words, *pictures] if pictures else words


class Conversation:
    """One ongoing exchange, persisted and costed.

    `start` is the tier a turn starts on (`UNATTENDED` for the background),
    set after construction by the callers that want it."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        channel: str = "tray",
        session_id: int | None = None,
        note: str | None = None,
        routine_id: int | None = None,
        goal_id: int | None = None,
        mandate: str | None = None,
        built: bool | None = None,
        module_routine: str | None = None,
    ) -> None:
        self.conn = conn
        self.channel = channel
        self.note = note
        # Whether each turn's history is rebuilt from the database
        # (agent/context.py): their conversations, by channel.
        self.built = context.builds_here(channel) if built is None else built
        # A run of one of their routines (agent/routines.py): every turn's
        # actions are gated by its approved envelope.
        self.routine_id = routine_id
        # A run of a module's routine (agent/modroutines.py): each action a card.
        self.module_routine = module_routine
        # A work session on one of their goals (agent/goals.py): likewise.
        self.goal_id = goal_id
        # What the user asked of this conversation, for the action critic
        # (core/critic.py); a turn may give its own (`send`).
        self.mandate = mandate
        self.start = Role.CONVERSE
        self.session_id = session_id or self._create_session(channel)
        self.cache_key = cache_key_for(channel)
        self._history: list[ModelMessage] = []
        # What it has read from outside (agent/screening.py `Reading`). The
        # mark is kept on its session, so a worker's restart keeps it; the
        # addresses are not, and reading the item again brings them back.
        self.reading = screening.Reading(read=self._has_read_outside())
        if session_id is not None:
            self._resume()

    def _has_read_outside(self) -> bool:
        row = self.conn.execute("SELECT read_outside_at FROM sessions WHERE id = ?",
                                (self.session_id,)).fetchone()
        return bool(row and row[0])

    def _keep_reading(self) -> None:
        """Writes the session's mark once its conversation has read outside text."""
        if not self.reading.read:
            return
        with db.transaction(self.conn):
            self.conn.execute("UPDATE sessions SET read_outside_at = datetime('now')"
                              " WHERE id = ? AND read_outside_at IS NULL", (self.session_id,))

    def _resume(self) -> None:
        """Reload what was said on this session in the last day.

        `self._history` is in memory, and the worker restarts often -- every
        self-upgrade swap, every crash, every time the laptop sleeps. So a
        follow-up like "what about the second one?" landed on a model that
        had never seen a first one, and it answered anyway.

        Only what was said is replayed, as `history.compact` keeps it live:
        their words with when the user said them, the answers, what was said to them
        unprompted (marked, with its time, and not counted among the
        `HISTORY_MAX_MESSAGES`), and a line for each action a turn took (`history.receipt`).
        Tool results are stored too, but a tool-return part without its
        matching call is malformed history, and a stale search result is
        worth less than the sentence that summarised it.

        Times because a replayed "tomorrow" has none: on 7 October 2026
        session 238 was resumed at 11:44, 13:10 and 23:52, and after each
        every earlier "tomorrow" could have been any day.
        """
        from pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            TextPart,
            UserPromptPart,
        )

        from agent import history as hist

        receipts = ", ".join(f"'{name}'" for name in sorted(hist.RECEIPT_TOOLS))  # constants
        rows = self.conn.execute(
            "SELECT role, content, model, tool_name, tool_args, created_at FROM messages"
            " WHERE session_id = ?"
            f"   AND (role IN ('user', 'assistant') OR tool_name IN ({receipts}))"
            f"   AND created_at >= datetime('now', '-{HISTORY_WINDOW_HOURS} hours')"
            " ORDER BY id DESC LIMIT ?",
            (self.session_id, (HISTORY_MAX_MESSAGES + HISTORY_MAX_UNPROMPTED) * 4),
        ).fetchall()

        kept, words, pings = [], 0, 0
        for row in rows:
            if row["role"] == "tool":
                kept.append(row)
            elif row["role"] == "assistant" and row["model"] == UNPROMPTED:
                if pings < HISTORY_MAX_UNPROMPTED:
                    kept.append(row)
                    pings += 1
            elif words < HISTORY_MAX_MESSAGES:
                kept.append(row)
                words += 1
            else:
                break
        kept.reverse()

        history: list[ModelMessage] = []
        for row in kept:
            at = hist.local(row["created_at"])
            if row["role"] == "tool":
                # Onto their request, where `compact` puts it; one with no
                # request before it lost its turn to the window.
                if history and isinstance(history[-1], ModelRequest):
                    history[-1].parts.append(UserPromptPart(
                        content=hist.receipt(row["tool_name"], row["tool_args"], row["content"])))
                continue
            # Cut with a mark (agent/cut.py): replayed silently cut, a long
            # answer read back as if it had ended there.
            content = cut(row["content"], HISTORY_MAX_CHARS,
                          mark=" […{n:,} more characters of this message, not replayed]")
            if not content:
                continue
            if row["role"] == "user":
                content = hist.asked(content, at)
                history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
                continue
            if row["model"] == UNPROMPTED:
                content = hist.unprompted(content, at)
            history.append(ModelResponse(parts=[TextPart(content=content)]))
        self._history = history
        if history:
            log.info(
                "resumed session %s with %d messages", self.session_id, len(history)
            )

    def _context_for(self, question_id: int) -> tuple[list[ModelMessage], str, str]:
        """The history this turn reads, what the user is most likely replying to,
        and what is open: rebuilt from the database for their own
        conversations (agent/context.py), so it is the same whether the
        worker has been up a minute or a week; the history kept in memory
        for every other run. Never lost to a fault in the building: the kept
        history is the old behaviour."""
        if not self.built:
            return list(self._history), "", ""
        try:
            return (context.history(self.conn, self.session_id, exclude_id=question_id),
                    context.replying_to(self.conn, self.session_id, exclude_id=question_id),
                    context.open_now(self.conn))
        except Exception:  # noqa: BLE001 - see above
            log.warning("could not build the turn's context; using the kept history",
                        exc_info=True)
            return list(self._history), "", ""

    async def _facts_for(self, text: str) -> str:
        """The facts their newest message calls for (`facts.relevant`), as
        lines for the turn, on their own channels only. Off the loop -- it
        embeds the message -- and never the turn's undoing: past
        `FACTS_WAIT_S`, or on any fault, the turn goes on without them."""
        if not self.built:
            return ""
        import asyncio

        from agent import facts

        try:
            return await asyncio.wait_for(
                offloop.with_own_conn(self.conn, facts.relevant_text, text), FACTS_WAIT_S)
        except Exception:  # noqa: BLE001 - see above
            log.warning("could not find the facts for this turn; going on without",
                        exc_info=True)
            return ""

    def _create_session(self, channel: str) -> int:
        with db.transaction(self.conn):
            cur = self.conn.execute("INSERT INTO sessions (channel) VALUES (?)", (channel,))
        return int(cur.lastrowid)

    def _store_message(
        self,
        role: str,
        content: str,
        *,
        model: str | None = None,
        tool_name: str | None = None,
        tool_args: str | None = None,
    ) -> int:
        with db.transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO messages (session_id, role, content, model, tool_name, tool_args)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (self.session_id, role, content, model, tool_name, tool_args),
            )
        return int(cur.lastrowid)

    def _store_stats(self, record: CallRecord, message_id: int) -> None:
        """What the run that answered did, beside its answer. Its cost is not
        written here: each request was billed as it was made, by the client
        (`core/spend.Guarded`), the hops that escalated or broke included."""
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO turn_stats (message_id, requests, tool_calls) VALUES (?, ?, ?)",
                (message_id, record.requests, record.tool_calls))

    def _store_tool_results(self, result) -> None:
        """Persist what the tools actually returned, not just their names.

        The `messages` table has had a 'tool' role and a `tool_name` column
        since the first schema and nothing ever wrote one. That is not a
        cosmetic gap: the rubric asks reflection whether factual claims were
        "supported by tool results", and with no tool results stored, the
        judge was answering from the conversation text alone. `grounded` is
        the lowest-scoring dimension by a distance, and this is why.

        Payloads are capped per row. A search result is the largest thing in a
        session and the tail of one is never the part that grounds a claim.

        This turn's messages only. `all_messages()` includes the history the
        turn was given, so every turn stored every earlier turn's results
        again, and the record showed tools being called over and over.

        What each tool was called with is kept beside what it returned. The
        model scout replays their turns to try other models on them
        (agent/models_eval.py), and without the arguments a replayed call
        had none: on 2 October 2026 a candidate read its own empty
        `remember` call back to them as a question it should have asked.
        """
        from pydantic_ai.messages import ToolCallPart, ToolReturnPart

        new = list(result.new_messages())
        called = {part.tool_call_id: part for message in new
                  for part in getattr(message, "parts", []) if isinstance(part, ToolCallPart)}
        for message in new:
            for part in getattr(message, "parts", []):
                if not isinstance(part, ToolReturnPart):
                    continue
                content = part.model_response_str()
                call = called.get(part.tool_call_id)
                if content:
                    self._store_message(
                        "tool", cut(content, MAX_TOOL_RESULT_CHARS),
                        tool_name=part.tool_name,
                        tool_args=_args_kept(call) if call else None,
                    )
                if (isinstance(part.content, dict) and part.content.get("error")
                        and part.content.get("kind") != "not_set_up"):
                    # Executors that raised come back this way too (`act`).
                    # What is not set up (agent/toolset `_set_up`) is the
                    # user's to set, not a fault for the issues list.
                    error = str(part.content["error"])
                    issues.record(self.conn, "tool_error", f"{part.tool_name}: {error[:60]}",
                                  f"{part.tool_name} reported: {error[:160]}",
                                  excerpt=content[:400], session_id=self.session_id)

    async def send(self, text: str, *, allow: set[str] | None = None, crew=None,
                   seen: set[str] | None = None, mandate: str | None = None,
                   on_text=None, on_tool=None, on_restart=None,
                   images: list[Path] | None = None) -> Reply:
        """Run one turn, replaying it on a stronger model if asked to.

        `allow` narrows the toolset for this conversation.

        `crew` makes this turn a lead that may start workers
        (agent/workers.py): one crew for the whole turn, every hop, so a
        replay finds the workers the first hop started.

        `seen` is what an unattended turn may read from the web besides what
        it searches up (`core/egress.unseen`): URLs in their own words. Never
        the turn's own text, which for the doer holds an email's preview.

        `on_restart` is told when a run is about to stream again from its first
        word -- the turn replayed on a stronger model, or a failed call
        retried -- so a voice that has spoken the first run's sentences does
        not speak them twice (voice/pipeline.py `_Reply.restart`).

        `images` are photos that came with `text` (channels/photos.py),
        shown to the model on this turn only. Later turns read the line
        `text` carries for them ("[1 photo attached]"), not the photos.

        Its model calls are billed as chat on the user's own channels and as
        background work elsewhere, unless it runs inside work whose category
        is set already (core/spend.py `spending`): a routine's turn is the
        routine's, and the night's is the night's.
        """
        from agent import approvals

        category = "chat" if self.channel in PRESENT_CHANNELS else "background"
        if self.channel in PRESENT_CHANNELS:
            # Their own words: a link they paste may be read, and a search
            # may type them, whatever the conversation has read before.
            self.reading.heard(text)
        # Every tool call of the turn marks this conversation's reading, the
        # cards asked at its end among them.
        token = screening.reading.set(self.reading)
        try:
            # The turn's cards are asked when it ends, those for the same tool
            # and account as one (agent/approvals.py `held`).
            async with approvals.held(self.conn):
                with spend.spending(category, conn=self.conn, session_id=self.session_id):
                    return await self._turn(text, allow=allow, crew=crew, seen=seen,
                                            mandate=mandate, on_text=on_text,
                                            on_tool=on_tool, on_restart=on_restart,
                                            images=images or [])
        finally:
            screening.reading.reset(token)
            self._keep_reading()

    async def _turn(self, text: str, *, allow: set[str] | None, crew, seen: set[str] | None,
                    mandate: str | None, on_text, on_tool, on_restart,
                    images: list[Path]) -> Reply:
        if self.channel in PRESENT_CHANNELS and issues.is_correction(text):
            # "wait, why did you cancel gym and runs?" -- the clearest signal
            # there is that the last answer was wrong, and on 27 September it
            # was found only by a person reading the conversation afterwards.
            # Only where the words are theirs: on 10 October the brief for work
            # done unasked ("The user is not at their phone and is not waiting for an
            # answer") was filed as their correction, and the checks before a
            # ping read the ledger's corrections as their words (`vet.user_words`).
            said = self._last_said()
            issues.record(self.conn, "correction", text, f"The user corrected it: {text[:120]}",
                          excerpt=f"It said: {said[:300]}\nThe user said: {text[:280]}",
                          session_id=self.session_id)
        question_id = self._store_message("user", text)
        if images:
            # The day shows them on the message, not the line naming them.
            photos.link(self.conn, question_id, images)
        if self.channel in PRESENT_CHANNELS:
            # Before the model, so the turn reads it closed: "Solve is done"
            # closed nothing on 8 October, and the model answering it
            # reached for the nearest open thing, the Northwind email format.
            try:
                closing.from_words(self.conn, text)
            except Exception:  # noqa: BLE001 - a missed close is the old behaviour
                log.warning("could not check the user's words against the ledger",
                            exc_info=True)
        # "RAISE 25" makes its card with no model call, before the cap is
        # asked: at the month's cap, the model is what the cap stops.
        if self.channel in PRESENT_CHANNELS and (usd := caps.raised_by_word(text)) is not None:
            said = await caps.ask_to_raise(self.conn, usd, session_id=self.session_id,
                                           channel=self.channel)
            return self._said_without_a_model(text, said, role=self.start)
        # Asked before anything that spends -- finding the facts embeds their
        # words -- and answered, if refused, with no model call.
        if refusal := spend.may_spend(self.conn):
            return self._capped(text, refusal, role=self.start)
        before, replying, open_block = self._context_for(question_id)
        known = await self._facts_for(text)
        # Read once, for every hop: a turn replayed on a stronger model is
        # shown the same photos.
        pictures = await asyncio.to_thread(photos.load, images) if images else []
        built = self.built
        # What this user has set up, asked once a turn: the house prompt and
        # the tools follow it (agent/features.py).
        on = await features.now(self.conn)
        # The accounts Steve could not check, with every message: asked
        # "anything new?" while a mailbox is signed out, "no" is not true.
        unchecked = " ".join(await offloop.with_own_conn(self.conn, accounts.unchecked))

        def clock() -> str:
            if not built:
                return _now_line()
            import datetime as dt

            return _now_line() + context.small_hours(dt.datetime.now(dt.UTC))

        role = self.start
        started_from: Role | None = None
        escalation_reason: str | None = None
        total_cost = 0.0
        total_latency = 0
        cached = 0
        deferred: list[int] = []
        tools_used: list[str] = []
        performed: dict = {}  # shared by every hop: see `act`
        # Likewise. A conversation they are in keeps one set for all its
        # turns -- what it searched up, what it read and their words -- so
        # once it has read outside text it may still open those
        # (`toolset/web.py` `unshown`). A run nobody watches keeps today's
        # set per turn, and never counts an address in what it read.
        if seen is None:
            seen = self.reading.urls if self.channel in PRESENT_CHANNELS else set()
        seen_urls: set[str] = seen
        # Likewise the turn's lookups (agent/toolset/pace.py): an escalation
        # no longer throws them away and looks everything up again.
        turn_cache: dict = {}
        handed_over = ""
        # Sites that refused us, shared with the crew's workers when there is one.
        refused: Counter = getattr(crew, "refused", None)
        if refused is None:
            refused = Counter()

        def restarting() -> None:
            if on_restart is None:
                return
            try:
                on_restart()
            except Exception:  # noqa: BLE001 - the reply matters more than its voice
                log.exception("a streaming callback failed")

        for _hop in range(MAX_ESCALATIONS + 1):
            if _hop:
                restarting()  # replayed on a stronger model: it speaks from the top again
            deps = Deps(
                conn=self.conn,
                session_id=self.session_id,
                channel=self.channel,
                role=role,
                performed=performed,
                crew=crew,
                seen_urls=seen_urls,
                tool_budget=HOP_TOOL_CALLS,
                refused=refused,
                routine_id=self.routine_id,
                module_routine=self.module_routine,
                goal_id=self.goal_id,
                mandate=mandate if mandate is not None else self.mandate,
                turn_cache=turn_cache,
                asked=text,
            )
            if missing := key_missing():
                raise self._no_key(text, role, missing)
            agent = build_agent(
                role, cache_key=self.cache_key, allow=allow, note=self.note,
                lead=crew is not None, clock=False, on=on,
            )
            # Passed in so the tokens an abandoned hop burned are still
            # readable after it raises.
            hop_usage = RunUsage()
            hop_started = time.perf_counter()

            try:
                result, record = await run_agent(
                    agent,
                    # The time rides with their words, not in the instructions:
                    # there it sat before the whole history and changed every
                    # minute, so the history was never cached -- 0% of the
                    # app's input and 41% of Signal's, measured 5 October 2026.
                    # What is open comes after both (agent/context.py).
                    _with(context.turn_text(f"{text}{handed_over}", clock=clock(),
                                            replying=replying, facts=known,
                                            open_block=open_block,
                                            unchecked=unchecked), pictures),
                    role,
                    deps=deps,
                    # Replays start from the history as it was *before* this
                    # turn, so an abandoned attempt never reaches the
                    # transcript.
                    message_history=list(before),
                    usage=hop_usage,
                    usage_limits=HOP_USAGE_LIMITS,
                    **({"event_stream_handler": streamed(on_text, on_tool)}
                       if on_text or on_tool else {}),
                    on_retry=restarting,
                )
            except UsageLimitExceeded:
                # The tier burned its round-trip budget without answering --
                # usually a tool-call loop. Report it as a turn that failed
                # rather than letting a stack trace reach the user, and bill
                # what it spent.
                latency = int((time.perf_counter() - hop_started) * 1000)
                record = record_for(role, hop_usage, latency, 1)
                total_cost += record.cost_usd
                total_latency += record.latency_ms
                cached += record.cached_tokens
                tools_used.extend(deps.tools_used)
                looped = ", ".join(sorted(set(deps.tools_used))) or "no tools"
                issues.record(self.conn, "turn_loop", looped,
                              f"A turn went round in tool calls ({looped}) until stopped",
                              excerpt=text, session_id=self.session_id)

                text_out = (
                    "I got stuck repeating myself on that one and stopped before "
                    "spending more. Try rephrasing it, or ask me to break it into "
                    "smaller pieces."
                )
                message_id = self._store_message(
                    "assistant", text_out, model=MODELS[role].slug
                )
                self._remember(text, text_out)
                return Reply(
                    text=text_out,
                    role=role,
                    escalated_from=started_from,
                    escalation_reason=escalation_reason,
                    deferred_jobs=deferred,
                    tools_used=tools_used,
                    cost_usd=total_cost,
                    latency_ms=total_latency,
                    cached_tokens=cached,
                )
            except EscalationRequested as escalation:
                # The hop produced no answer but still cost money. Billing only
                # the winning hop would quietly understate every hard turn.
                latency = int((time.perf_counter() - hop_started) * 1000)
                record = record_for(role, hop_usage, latency, 1)
                total_cost += record.cost_usd
                total_latency += record.latency_ms
                cached += record.cached_tokens
                deferred.extend(deps.deferred)
                tools_used.extend(deps.tools_used)

                started_from = started_from or role
                escalation_reason = escalation.reason
                role = escalation.to
                # What it found is not looked for again: the tier taking over
                # is told what was asked, and asking again is instant.
                if made := pace.looked_up(turn_cache):
                    handed_over = (
                        "\n\n[Handed to you after these lookups this turn; asking for any "
                        "of them again returns its result at once: " + "; ".join(made) + "]")
                continue
            except spend.Refused as refusal:
                # A cap reached between two of this turn's requests: the one
                # in flight finished, and was billed (core/spend.Guarded).
                latency = int((time.perf_counter() - hop_started) * 1000)
                record = record_for(role, hop_usage, latency, 1)
                return self._capped(text, refusal, role=role, partway=True,
                                    cost_usd=total_cost + record.cost_usd,
                                    latency_ms=total_latency + record.latency_ms)
            except Exception as exc:
                if spend.out_of_credit(exc):
                    raise self._no_credit(text, role) from exc
                # Anything else -- a tool that raised, a provider that gave
                # up. The question was stored but joined no history, so on
                # 27 September "try again" reached a model that had never
                # seen "add Game Theory to my calendar", and it retried the
                # request before it: a search for somebody else's coffee chat.
                raise self._broke(text, role, exc) from exc

            total_cost += record.cost_usd
            total_latency += record.latency_ms
            cached += record.cached_tokens

            # Checked before it is accepted: it claims only what a tool did,
            # or it gets one pass to put that right (agent/verify.py).
            self._store_tool_results(result)
            try:
                result, history, extra = await self._verified(agent, role, deps, result)
            except spend.Refused as refusal:
                # Its correction refused: an answer that claims what was not
                # done does not go out unchecked.
                return self._capped(text, refusal, role=role, partway=True,
                                    cost_usd=total_cost, latency_ms=total_latency)
            for checked in extra:
                total_cost += checked.cost_usd
                total_latency += checked.latency_ms
            deferred.extend(deps.deferred)
            tools_used.extend(deps.tools_used)

            # Accepted: this is the answer. What the next turn carries is what
            # was said, not every page read on the way (agent/history.py).
            answer = clean(str(result.output or ""))
            from agent import facts

            if kept := turn_cache.get(facts.TURN_KEPT):
                # Each fact kept is told with its number, so FORGET MEMORY
                # works for it: memory 223 was written mid-turn, never
                # numbered, and could not be dropped (8 October 2026).
                answer = facts.acknowledged(answer, kept)
            if self.channel in PRESENT_CHANNELS:
                # The turn that took their day's chat past four fifths of its
                # cap says so, once (agent/caps.py).
                answer += caps.warning_for_the_reply(self.conn)
            self._history = compact(history)
            message_id = self._store_message(
                "assistant", answer, model=MODELS[role].slug
            )
            self._store_stats(record, message_id)
            if total_latency > issues.SLOW_TURN_MS:
                issues.record(self.conn, "slow_turn", role.value,
                              f"A reply took {total_latency / 1000:.0f}s",
                              excerpt=text, session_id=self.session_id, message_id=message_id)
            if hop_usage.tool_calls >= HOP_TOOL_CALLS:
                # Answered, but only because its tools were taken away: the
                # ledger still wants to know a turn needed that many.
                spent = ", ".join(sorted(set(deps.tools_used))) or "no tools"
                issues.record(self.conn, "turn_loop", spent,
                              f"A turn used all {HOP_TOOL_CALLS} tool calls ({spent}) and "
                              "answered with what it had",
                              excerpt=text, session_id=self.session_id, message_id=message_id)
            if record.attempts > 1:
                issues.record(self.conn, "model_retry", role.value,
                              f"A {role.value} call needed {record.attempts} attempts",
                              excerpt=text, session_id=self.session_id, message_id=message_id)
            return Reply(
                text=answer,
                role=role,
                escalated_from=started_from,
                escalation_reason=escalation_reason,
                deferred_jobs=deferred,
                tools_used=tools_used,
                cost_usd=total_cost,
                latency_ms=total_latency,
                cached_tokens=cached,
            )

        # Unreachable: at the top tier next_tier() returns None, so the
        # escalate tool returns a string instead of raising and the model
        # must answer. Kept so a future ladder change fails loudly.
        raise RuntimeError("escalation loop exhausted without producing a reply")

    async def _verified(self, agent, role: Role, deps: Deps, result):
        """The answer, checked; corrected once if it claims what was not done.
        Returns the result that goes, the history to keep, and what the
        correction cost.

        The correction note is spliced out of the history: the next turn sees
        the question and the corrected answer, not the assistant being told off.
        """
        from agent import verify
        from agent.approvals import reported_failure, waiting

        reply = str(result.output or "")
        this_turn = set(deps.tools_used)
        # An action whose every call this turn failed backs no claim, unless
        # it was called earlier in the conversation too: the duplicate check
        # refusing `create_event` and the reply still saying "Added it to
        # your calendar" is the claim this exists to catch. The rows below
        # include this turn's, so "earlier" is more rows than calls now.
        worked: dict[str, bool] = {}
        for (tool, _args), outcome in deps.performed.items():
            worked[tool] = worked.get(tool, False) or reported_failure(outcome) is None
        failed = {tool for tool, ok in worked.items() if not ok}
        rows = Counter(
            r["tool_name"] for r in self.conn.execute(
                "SELECT tool_name FROM messages WHERE session_id = ? AND role = 'tool'",
                (self.session_id,),
            ) if r["tool_name"]
        )
        calls = Counter(deps.tools_used)
        used = (this_turn - failed) | {
            tool for tool, n in rows.items() if tool not in failed or n > calls[tool]
        }
        went = {tool for (tool, _args), outcome in deps.performed.items()
                if reported_failure(outcome) is None and not waiting(outcome)}
        problems = verify.unbacked_claims(reply, used, this_turn - failed, went_this_turn=went)
        spent: list = []
        if not problems:
            return result, list(result.all_messages()), spent

        log.info("correcting an answer: %s", problems)
        issues.record(self.conn, "unbacked_claim", str(problems)[:100],
                      f"An answer claimed what no tool did: {str(problems)[:160]}",
                      excerpt=reply, session_id=self.session_id)
        try:
            corrected, record = await run_agent(
                agent, verify.note(problems), role, deps=deps,
                message_history=list(result.all_messages()), usage_limits=HOP_USAGE_LIMITS,
            )
        except Exception:  # noqa: BLE001 - the first answer still goes
            log.exception("the correction pass failed")
            return result, list(result.all_messages()), spent
        spent.append(record)
        self._store_tool_results(corrected)
        history = list(result.all_messages())[:-1] + list(corrected.new_messages())[1:]
        return corrected, history, spent

    def _last_said(self) -> str:
        """The agent's last words in this conversation, from the history."""
        from pydantic_ai.messages import ModelResponse, TextPart

        for message in reversed(self._history):
            if isinstance(message, ModelResponse):
                text = " ".join(p.content for p in message.parts if isinstance(p, TextPart))
                if text:
                    return text
        return ""

    def _remember(self, question: str, answer: str) -> None:
        """An exchange that did not come from an accepted turn, into the
        history the next turn starts from."""
        from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

        self._history.append(ModelRequest(parts=[UserPromptPart(content=question)]))
        self._history.append(ModelResponse(parts=[TextPart(content=answer)]))

    def _capped(self, question: str, refusal: spend.Refused, *, role: Role,
                partway: bool = False, cost_usd: float = 0.0, latency_ms: int = 0) -> Reply:
        """A turn a spending cap stopped, before its first model call or
        between two. Said plainly where the user is present, since the reply
        is how they hear of it. Raised anywhere else, so the routine, goal or
        job it belongs to stops quietly, and the next briefing says why
        (agent/caps.py). Recorded either way, so the conversation reads it."""
        said = caps.refused(refusal, partway=partway)
        self._store_message("assistant", said)
        self._remember(question, said)
        if self.channel not in PRESENT_CHANNELS:
            raise refusal
        caps.told_in_the_reply(self.conn, refusal)
        return Reply(text=said, role=role, escalated_from=None, escalation_reason=None,
                     deferred_jobs=[], tools_used=[], cost_usd=cost_usd,
                     latency_ms=latency_ms, cached_tokens=0)

    def _said_without_a_model(self, question: str, said: str, *, role: Role) -> Reply:
        """A reply written in code, recorded as any reply is, so the
        conversation reads it."""
        self._store_message("assistant", said)
        self._remember(question, said)
        return Reply(text=said, role=role, escalated_from=None, escalation_reason=None,
                     deferred_jobs=[], tools_used=[], cost_usd=0.0, latency_ms=0,
                     cached_tokens=0)

    def _no_key(self, question: str, role: Role, key: str) -> TurnFailed:
        """A turn whose model has no key, said so plainly. It went to the
        provider with a stand-in key and came back as "Something broke"
        with an authentication error in it."""
        said = NO_KEY.format(key=key)
        self._store_message("assistant", said, model=MODELS[role].slug)
        self._remember(question, said)
        return TurnFailed(said)

    def _no_credit(self, question: str, role: Role) -> TurnFailed:
        """A turn OpenAI refused because the account's balance is spent,
        said plainly with where to top it up. Not recorded as a broken turn:
        nothing here is broken, and the night would set out to mend it.
        Where the user is not present, a message says it once quiet hours
        are over (agent/caps.py)."""
        said = caps.NO_CREDIT
        self._store_message("assistant", said, model=MODELS[role].slug)
        self._remember(question, said)
        if self.channel in PRESENT_CHANNELS:
            caps.no_credit_told(self.conn)
        return TurnFailed(said)

    def _broke(self, question: str, role: Role, exc: Exception) -> TurnFailed:
        """Record a broken turn as one, and say so plainly."""
        log.error("a %s turn broke", self.channel, exc_info=exc)
        issues.record(self.conn, "turn_broke", f"{type(exc).__name__}: {str(exc)[:60]}",
                      f"A turn broke: {type(exc).__name__}: {str(exc)[:120]}",
                      excerpt=question, session_id=self.session_id)
        said = BROKE.format(error=f"{type(exc).__name__}: {exc}"[:160])
        self._store_message("assistant", said, model=MODELS[role].slug)
        self._remember(question, said)
        return TurnFailed(said)

    def note_exchange(self, question: str, answer: str, *, by: str) -> None:
        """Something the user asked outside this conversation's turns, and
        what came of it, `by` whom (`history.LIVE`): stored as well as
        remembered, so a worker that restarts replays it too (`_resume`)."""
        question, answer = (question or "").strip(), (answer or "").strip()
        if not question:
            return
        self._store_message("user", question)
        if answer:
            self._store_message("assistant", answer, model=by)
        self._remember(question, answer or "(no answer)")

    def cut_short(self, heard: str) -> None:
        """The last reply, as far as the user heard it before the user cut in.

        Spoken replies are interrupted -- "hey Steve" over one, the hotkey --
        and the whole reply stayed in the history and the record as if said,
        so a later turn could build on a sentence the user never heard ("as I
        mentioned..."). What the speaker played is kept, and the rest is said
        not to have been: voice systems keep the spoken part only."""
        from pydantic_ai.messages import ModelResponse, TextPart

        said = f"{heard.strip()} {CUT_NOTE}".strip()
        for message in reversed(self._history):
            if isinstance(message, ModelResponse) and any(
                    isinstance(p, TextPart) for p in message.parts):
                kept = [p for p in message.parts if not isinstance(p, TextPart)]
                message.parts = [*kept, TextPart(content=said)]
                break
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE messages SET content = ? WHERE id = (SELECT MAX(id) FROM messages"
                " WHERE session_id = ? AND role = 'assistant')", (said, self.session_id))

    def note_said(self, text: str) -> None:
        """Something said on this channel outside a turn -- a ping, a card, a
        briefing -- so that a reply to it has something to reply to.

        Without this, the agent's own unprompted messages were in nobody's
        history: it would text "Rain from 3pm, your 4pm workshop is in
        person", the user would answer "move it to Zoom then", and the model
        answering them had never seen "it". Stored as an assistant message,
        so a worker that restarts replays it too (`_resume`), and marked in
        `model` so it can be told apart from a reply -- and in the history,
        with its time, as `_resume` marks it (`history.unprompted`).
        """
        import datetime as dt

        from pydantic_ai.messages import ModelResponse, TextPart

        from agent import history as hist

        text = (text or "").strip()
        if not text:
            return
        self._store_message("assistant", text, model=UNPROMPTED)
        now = hist.local(dt.datetime.now(dt.UTC))
        self._history.append(ModelResponse(parts=[TextPart(content=hist.unprompted(text, now))]))

    def end(self) -> None:
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE sessions SET ended_at = datetime('now') WHERE id = ?",
                (self.session_id,),
            )
