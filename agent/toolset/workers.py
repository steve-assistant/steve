"""The lead's tools for its workers: start one, start a browsing one, wait
for them, stop one.

Registered only for a lead with a crew (agent/workers.py) -- a background
job's, or the doer's or the night's research turn, which are given the
browsing ones alone (`workers.send_browsing`); never a conversation the user is
waiting on, and never a worker -- and shown only as its crew allows
(`toolset.offered`): `start_worker` when Jev judged the task worth
splitting, `start_browsing_worker` when the crew may browse. Each also
refuses unless the turn holds a crew and is not a worker's, so a
registration mistake cannot let a worker start workers.
"""

from __future__ import annotations

from pydantic_ai import RunContext

from agent.turn import Deps

NOT_A_LEAD = {"ok": False, "error": "only the lead of a background task can manage workers"}


def _crew(ctx: RunContext[Deps]):
    return None if ctx.deps.worker else ctx.deps.crew


def register(tool) -> None:
    @tool
    async def start_worker(ctx: RunContext[Deps], task: str, name: str = "") -> dict:
        """Start a worker on one independent part of this task. Returns at once.

        The worker is a fresh assistant with none of your context, running at
        the same time as you and any other workers: put everything it needs
        in `task` -- who or what, what to find or do, and what to hand back.
        `name` is a short label ("Dana", "flights"). It has your tools for
        looking things up and for what can be undone; it cannot send, call,
        delete or start workers. Collect its answer with wait_for_workers.
        """
        crew = _crew(ctx)
        if crew is None:
            return NOT_A_LEAD
        return crew.start(task, name)

    @tool
    async def start_browsing_worker(ctx: RunContext[Deps], task: str, urls: list[str],
                                    name: str = "") -> dict:
        """Start a worker with a real, sandboxed browser, for pages read_page
        cannot read -- they need scripts, or refuse plain fetchers. Returns at
        once; collect its answer with wait_for_workers.

        It only reads: it has nothing of the user's -- no accounts, files or
        memory -- and cannot log in, fill in forms or handle files. Its
        browser is kept to the sites of `urls`, which must be pages you were
        shown (search results, or addresses in the user's request); it may
        follow links within those sites. Put what to find in `task`. At most
        two run at once.
        """
        crew = _crew(ctx)
        if crew is None:
            return NOT_A_LEAD
        return await crew.start_browsing(task, urls, name)

    @tool
    async def wait_for_workers(ctx: RunContext[Deps], timeout_s: int = 180) -> dict:
        """Wait for running workers, up to `timeout_s` seconds (at most 240),
        and get each finished one's answer, once. Those still running are
        listed; wait again, or stop them."""
        crew = _crew(ctx)
        if crew is None:
            return NOT_A_LEAD
        return await crew.wait(timeout_s)

    @tool
    async def stop_worker(ctx: RunContext[Deps], worker: int) -> dict:
        """Stop a worker that is no longer needed. What it spent is kept."""
        crew = _crew(ctx)
        if crew is None:
            return NOT_A_LEAD
        return await crew.stop(worker)
