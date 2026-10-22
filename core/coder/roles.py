"""Running one role -- coder, test builder or judge -- within its budget.

The old path learned what a run cost from its harness's event stream, and when
the stream lost its last event the run read as free: once, that quietly
disabled the nightly budget. Here the cost is the model's own token count
priced from core/coder/models.py, checked after every model step, and the
run stops the moment it is over -- a budget that is only checked at the
end is a report, not a limit.

A role stops for one of four reasons, all recorded: it finished, it spent
its budget, it used its turns, or it ran out of time. A provider failing
underneath it is not one of them: the role resumes, a bounded number of
times, on the same budget, turns and clock.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from core.coder import models
from core.coder.models import CoderModel, spent
from core.coder.tools import Tools, register, summary

PROMPTS = Path(__file__).resolve().parent / "prompts"

# How often a role picks up again after a provider returns something
# malformed or fails outright, before that counts as the role failing.
RESUMES = 2


def _transient(exc: BaseException) -> bool:
    """Worth picking up after: a malformed answer, a timeout, a dropped
    connection, a rate limit, the provider's own failure. Not a refusal that
    will be the same next time -- the fourth live run resumed a 402 (the
    OpenRouter balance was spent) twice, for nothing, and OpenAI says the
    same with a 429 (`spend.out_of_credit`)."""
    from pydantic_ai.exceptions import ModelHTTPError

    from core import spend

    if isinstance(exc, ModelHTTPError):
        if spend.out_of_credit(exc):
            return False
        return exc.status_code in (408, 409, 425, 429) or exc.status_code >= 500
    return True


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


class CoderReport(BaseModel):
    """What the coder says it did. The judge reads it beside the diff; the
    diff, not this, says what changed.

    It once had a `look_closely` field. Nothing in the code read it, but a
    coder filled it with "Please verify...". The judge refuses a report
    that addresses the reviewer, so it rejected a correct build (the
    module-building eval, 15 October). A field that asks the coder to talk
    to its reviewer is a trap."""

    summary: str
    tests_added: list[str] = []
    # Set only when the request cannot be a module, and then nothing else
    # is judged: "slot" when it needs a kind of module Steve has no slot
    # for, "core" when a bug in Steve itself stands in the way. `why` says
    # which, in words the user will read.
    cannot: Literal["", "slot", "core"] = ""
    why: str = ""


@dataclass
class RoleRun:
    output: object | None
    cost_usd: float = 0.0
    requests: int = 0
    seconds: float = 0.0
    stopped: str | None = None  # None when it finished; else why it did not
    resumed: int = 0  # times it picked up again after a provider failure
    tool_calls: list[str] = field(default_factory=list)
    tools: str = ""  # tools.summary of this run's calls: what failed, where the time went

    @property
    def finished(self) -> bool:
        return self.stopped is None and self.output is not None


async def run_role(
    *,
    model: CoderModel,
    system_prompt: str,
    first_message: str,
    tools: Tools | None,
    tool_names: tuple[str, ...],
    output_type,
    budget_usd: float,
    max_turns: int,
    max_seconds: float,
    build=None,
) -> RoleRun:
    from pydantic_ai import Agent
    from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.usage import RunUsage, UsageLimits

    run = RoleRun(output=None)
    if budget_usd <= 0:
        # Checked before anything is built or asked, not after the first
        # step: a role with nothing left used to make one request anyway.
        run.stopped = "budget: none left"
        return run

    # Looked up now, not bound at import: a default argument is fixed when
    # the module loads, and a fence set later on `models.client` never
    # reached it -- a gate test's coder built a real client that way.
    llm, settings = (build or models.client)(model)
    agent = Agent(llm, output_type=output_type, system_prompt=system_prompt,
                  model_settings=settings, retries=2)
    if tools is not None:
        register(agent, tools, tool_names)

    started = time.perf_counter()
    # This run's calls only: the coder's fix round reuses its first run's tools.
    calls_at, log_at = (len(tools.calls), len(tools.log)) if tools is not None else (0, 0)
    # One usage for every resumption, so the budget and the turn limit count
    # them all: a resume must never buy a role more than it was given.
    usage = RunUsage()
    current = {}

    async def once(prompt, past) -> None:
        async with agent.iter(prompt, message_history=past, usage=usage,
                              usage_limits=UsageLimits(request_limit=max_turns)) as live:
            current["run"] = live
            async for _node in live:
                run.cost_usd = spent(model, usage)
                run.requests = int(getattr(usage, "requests", 0) or 0)
                if run.cost_usd > budget_usd:
                    run.stopped = f"budget: ${run.cost_usd:.3f} of ${budget_usd:.2f}"
                    return
            if live.result is not None:
                run.output = live.result.output

    async def go():
        # A provider's bad answer is not the role's mistake. In the first live
        # run OpenRouter answered with finish_reason "error" ten minutes into
        # the coder's work, and the exception threw all of it away. The run's
        # messages survive it -- they end with the request that was being
        # sent, tool results included -- so the role picks up from there, and
        # no tool runs twice.
        prompt, past = first_message, None
        for attempt in range(RESUMES + 1):
            try:
                await once(prompt, past)
                return
            except (UnexpectedModelBehavior, ModelAPIError) as exc:
                if not _transient(exc):
                    raise
                so_far = list(current["run"].all_messages()) if current else []
                if attempt == RESUMES or not so_far or not isinstance(so_far[-1], ModelRequest):
                    raise
                prompt, past = None, so_far
                run.resumed += 1

    try:
        await asyncio.wait_for(go(), timeout=max_seconds)
    except TimeoutError:
        run.stopped = f"time: {max_seconds:.0f}s"
    except UsageLimitExceeded:
        run.stopped = f"turns: {max_turns}"
    except Exception as exc:  # noqa: BLE001 - a failed role is an outcome to record
        run.stopped = f"error: {type(exc).__name__}: {exc}"[:300]
    run.seconds = time.perf_counter() - started
    if tools is not None:
        run.tool_calls = tools.calls[calls_at:]
        run.tools = summary(tools.log[log_at:])
    return run
