"""The judge: the one that decides, seeing everything the other two made.

It reads the task, the coder's report and whole diff, the test builder's
tests and report, and the results of every check -- lint, the full suite,
the coder's tests, the independent tests -- and returns a verdict. It has
no tools: it judges what is in front of it, so it cannot be steered by
anything it goes looking for.

Its verdict is not the last word on its own. The pipeline refuses to treat
an approval as one when the checks say otherwise: a failing suite, a lint
error, or an independent test the judge itself ruled a code bug.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from core.coder.models import CATALOG
from core.coder.roles import RoleRun, prompt, run_role

MAX_DIFF_CHARS = 60_000
MAX_OUTPUT_CHARS = 6_000


class Ruling(BaseModel):
    test: str
    ruling: Literal["code bug", "bad test"]
    why: str


class Verdict(BaseModel):
    decision: Literal["approve", "revise", "reject"]
    reasons: str
    rulings: list[Ruling] = []
    keep_tests: list[str] = []


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[... {len(text) - limit} more]"


def brief(*, task: str, coder_report: str, diff: str, tester_report: str,
          tester_files: dict[str, str], checks: dict[str, str], flags: list[str]) -> str:
    """Everything the judge sees, in one message."""
    parts = [
        "# The task\n\n" + task.strip(),
        "# The coder's report\n\n" + coder_report,
        "# The whole change (coder)\n\n```diff\n" + _cut(diff, MAX_DIFF_CHARS) + "\n```",
        "# The independent test builder's report\n\n" + tester_report,
    ]
    for rel, text in tester_files.items():
        parts.append(f"## {rel}\n\n```python\n{_cut(text, 12_000)}\n```")
    for name, output in checks.items():
        parts.append(f"# Check: {name}\n\n```\n{_cut(output, MAX_OUTPUT_CHARS)}\n```")
    if flags:
        parts.append("# Flagged for your attention\n\n" + "\n".join(f"- {f}" for f in flags))
    return "\n\n".join(parts)


async def judge(message: str, role, *, build=None) -> RoleRun:
    return await run_role(
        model=CATALOG[role.model], system_prompt=prompt("judge"), first_message=message,
        tools=None, tool_names=(), output_type=Verdict,
        budget_usd=role.budget_usd, max_turns=role.max_turns, max_seconds=role.max_seconds,
        build=build,
    )
