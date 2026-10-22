"""How tools run when the model asks for several at once, and what a turn
reuses.

The maintainer, 5 October 2026: "sure we can parallelize some of the searches, not
everything needs to be since some things can work better sequentially".
Measured that evening, a research turn made its lookups one per model round
trip -- three web searches, three round trips -- though the second and third
(details on two campsites) did not depend on each other. pydantic-ai already
runs the calls of one response at the same time; the model was not asking
for them together. `system.md` now tells it when to; this decides which
tools may then really overlap.

**Not everything may.** Google's clients are cached one per API and shared
between threads (`modules/google_cloud/common._service`), and httplib2 is not
thread-safe: two Gmail calls at once can corrupt each other's replies. The
sandbox browser is one session. Memory writes commit to one git repo. So
every tool belongs to a GROUP whose calls take turns, across every turn
running in the worker; only the PARALLEL tools -- the web, which shares
nothing -- run freely. A tool not named here is in its own one-at-a-time
group by default: a new tool is safe before it is fast.

**A turn's lookups are kept for the turn** (`Deps.turn_cache`). An
escalation used to start the turn over on a stronger tier and look everything
up again: on 5 October "What time Amtrak do I need..." spent 35 s on its
lookups at the first tier, escalated, and spent 111 s more. Now a READ tool
asked the same thing twice in a turn -- by the tier that escalated, then by
the one it handed to -- answers the second time from the first.

**A turn's writes are done once** (`once`): every tool but a READ and the
few named AGAIN, asked the same again in the turn by a retry or an
escalation, gives the first result back.
"""

from __future__ import annotations

import asyncio
import functools
import json
import weakref
from typing import Any

# The web: no shared client, nothing written. Free to overlap.
PARALLEL = frozenset({
    "web_search", "read_page", "search_and_read", "news_summary", "recall", "who_is",
    "find_notes", "due_dates", "list_reminders", "use_skill", "list_skills",
    "my_commitments", "followed_news", "weather", "research",
})

# Calls that share a client take turns.
GROUPS: dict[str, frozenset[str]] = {
    "mail": frozenset({"search_email", "read_email", "email_thread", "draft_email",
                       "delete_draft", "send_draft", "send_email", "modify_email",
                       "writing_style"}),
    "calendar": frozenset({"calendar_agenda", "calendar_free", "calendar_search",
                           "create_event", "update_event", "delete_event"}),
    "drive": frozenset({"search_drive", "read_drive_file", "create_document",
                        "create_spreadsheet", "read_spreadsheet", "update_spreadsheet"}),
    "tasks": frozenset({"add_task", "complete_task", "list_tasks"}),
    # LinkedIn through their one saved login: a read is three page loads in one
    # browser, and two at once would be both interleaved on its one page and
    # twice the pace the account is held to (toolset/linkedin.py).
    "linkedin": frozenset({"linkedin_profile", "linkedin_search_people", "linkedin_company",
                           "linkedin_jobs"}),
}

# Lookups that change nothing: the same question twice in a turn gets the
# first answer (`Deps.turn_cache`).
READ = frozenset({
    "web_search", "read_page", "search_and_read", "news_summary", "who_is", "find_notes",
    "due_dates", "search_email", "read_email", "email_thread", "calendar_agenda",
    "calendar_free", "calendar_search", "search_drive", "read_drive_file",
    "read_spreadsheet", "list_tasks", "my_commitments", "weather", "research",
    # An escalated tier asking the same of LinkedIn gets the first read, not a
    # second set of page loads against the account.
    "linkedin_profile", "linkedin_search_people", "linkedin_company", "linkedin_jobs",
})

# Asked the same again in a turn, these run again: reads whose answer may
# have changed since, and the browser's steps, where a second scroll is a
# second scroll. Every other tool but a READ is done once a turn (`once`).
AGAIN = frozenset({
    "recall", "list_reminders", "list_routines", "list_skills", "use_skill",
    "followed_news", "goal_status", "writing_style", "escalate", "wait_for_workers",
    "browse_open", "browse_login", "browse_read", "browse_scroll", "browse_back",
    "browse_screenshot", "browse_search", "browse_files", "read_download", "browse_close",
})


def group_of(name: str) -> str | None:
    """The group a tool's calls take turns in, or None for a PARALLEL one."""
    if name in PARALLEL:
        return None
    for group, names in GROUPS.items():
        if name in names:
            return group
    return f"tool:{name}"


_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _lock(group: str) -> asyncio.Lock:
    """One lock per group per event loop (tests run many loops)."""
    loop = asyncio.get_running_loop()
    held = _locks.setdefault(loop, {})
    if group not in held:
        held[group] = asyncio.Lock()
    return held[group]


def _key(name: str, args: tuple, kwargs: dict) -> str | None:
    try:
        return name + json.dumps([list(args[1:]), kwargs], sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None


def wrap(fn):
    """`fn`, a tool, run in its group's turn, and answered from the turn's
    lookups when it is a READ asked again."""
    name = fn.__name__
    group = group_of(name)
    cacheable = name in READ

    @functools.wraps(fn)
    async def paced(*args, **kwargs) -> Any:
        ctx = args[0] if args else kwargs.get("ctx")
        cache = getattr(getattr(ctx, "deps", None), "turn_cache", None)
        key = _key(name, args, kwargs) if cacheable and cache is not None else None
        if key is not None and key in cache:
            return cache[key]
        if group is None:
            result = await fn(*args, **kwargs)
        else:
            async with _lock(group):
                result = await fn(*args, **kwargs)
        failed = isinstance(result, dict) and result.get("error")
        if key is not None and not failed:  # a failure is worth asking again
            cache[key] = result
        return result

    return paced


def once(fn):
    """`fn`, a tool, done once a turn unless it is a READ or AGAIN: the same
    asked again in the turn gets the first result back, kept with the
    turn's effects (`Deps.performed`), where the answer check reads what
    the turn did.

    A turn is run again from its start when a request fails past the SDK's
    retries (agent/llm.py `run_agent`), and replayed when it escalates, and
    the model asks again for what it asked before. `act` keeps an effect
    once a turn, but not what a tool writes before or without it: step 10's
    hostile scenarios found three 500s after `remind_me` setting the
    reminder twice, and `create_routine` writes its routine before its
    card. So every write is kept here, by default: a new tool is done once
    before anyone decides it may be done again.

    Inside the noting of a tool as used, as `act`'s own check is, so the
    turn still counts it as called."""
    name = fn.__name__
    if name in READ or name in AGAIN:
        return fn

    @functools.wraps(fn)
    async def kept(ctx, *args, **kwargs) -> Any:
        key = (name, json.dumps([list(args), kwargs], sort_keys=True, default=str))
        if key in ctx.deps.performed:
            return ctx.deps.performed[key]
        result = await fn(ctx, *args, **kwargs)
        ctx.deps.performed[key] = result
        return result

    return kept


def looked_up(cache: dict | None) -> list[str]:
    """What a turn has already looked up, as the calls were made: for the
    tier an escalation hands it to."""
    out = []
    for key in (cache or {}):
        name, _, args = key.partition("[")
        out.append(f"{name}([{args[:160]}")
    return out
