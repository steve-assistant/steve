"""Running Python in the Docker sandbox."""

from __future__ import annotations

import asyncio
import logging

from pydantic_ai import RunContext

from agent.actions import act, executable
from agent.turn import Deps
from tools import code_exec

log = logging.getLogger("toolset.code")


@executable("run_code")
async def _do_run_code(*, code: str, timeout_s: int = 30, network: bool = False) -> dict:
    from agent import features
    from core import knobs

    # Checked here too, where a card approved later runs: a caller that
    # builds its agent with every feature still meets the user's switch.
    if not knobs.on("RUN_CODE"):
        return {"error": features.switched_off("RUN_CODE")}
    # In a thread: the sandbox runs for up to `timeout_s`, and on the loop
    # that was the whole worker frozen for as long -- long enough for the
    # watchdog to kill it mid-turn.
    result = await asyncio.to_thread(
        code_exec.run_python, code, timeout_s=timeout_s, network=network
    )
    return result.as_dict()


def register(tool) -> None:
    """Attach the code tools. `tool` is `register_tools`'s filter."""

    @tool
    async def run_code(
        ctx: RunContext[Deps],
        code: str,
        timeout_s: int = 30,
        network: bool = False,
    ) -> dict:
        """Run Python in a sandboxed container.

        No network unless you ask for it. Write files to /work/out to get them
        back. Prefer this over doing arithmetic yourself.

        Asking for `network=True` queues the run for the user to approve
        rather than running it, so leave it off unless the code genuinely has
        to fetch something.
        """
        args = {"code": code, "timeout_s": timeout_s, "network": network}
        # The code, whole (agent/approvals.py, "What a card may say"). It
        # showed the first line alone -- often an import -- so a yes let code
        # the user had not seen reach the network.
        lines = len(code.strip().splitlines())
        return await act(
            ctx, "run_code", args,
            preview=f"Run {lines} line(s) of code with network access\n\n"
                    f"{code.strip() or '(empty)'}",
        )
