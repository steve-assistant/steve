"""The coder: one task, in one folder, until its tests and the suite pass.

It starts from the context pack (core/coder/context.py), works with the
tools in core/coder/tools.py -- no shell -- and ends with a `CoderReport`.
Its own tests are part of its work; they are not what decides whether the
work is good. That is the test builder's and the judge's (phase 3).
"""

from __future__ import annotations

from core.coder import context
from core.coder.models import CATALOG
from core.coder.roles import CoderReport, RoleRun, prompt, run_role
from core.coder.tools import CODER_TOOLS, Tools


async def code(task: str, tools: Tools, role, *, feedback: str = "", build=None) -> RoleRun:
    """`role` is the policy's coder settings (`core.policy.RoleConfig`).
    `feedback` is the judge's, on a revision round: the coder starts fresh
    with the task, the tree as it left it, and what was wrong."""
    first = context.pack(tools.ws, task, outline=tools.outline)
    tools.calls.clear()  # the pack's outlines are not the coder's own calls
    if feedback:
        # Once a single line after the review. In the fourth live run the
        # coder worked through its revision and did neither of the two things
        # the judge asked, which the judge then found again.
        first += ("\n\n# Your change was sent back\n\n"
                  "The tree holds your last attempt. It will be judged again on exactly "
                  "this, and anything below that is still true is a rejection:\n\n"
                  + feedback.strip() + "\n\n"
                  "Do each thing it asks: remove what it says to remove, change what it "
                  "says to change, add the tests it asks for. Do not start the task over. "
                  "Then run the tests you touched, and finish.")
    return await run_role(
        model=CATALOG[role.model], system_prompt=prompt("coder"), first_message=first,
        tools=tools, tool_names=CODER_TOOLS, output_type=CoderReport,
        budget_usd=role.budget_usd, max_turns=role.max_turns, max_seconds=role.max_seconds,
        build=build,
    )
