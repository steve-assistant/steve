"""The test builder: a second reading of the change, blind to the coder's.

The maintainer, 27 September 2026: "a test builder that's independent of the coder
and only sees the documentation and codebase created by the coder so it can
write unbiased and fair testing". A coder's own tests share its
misunderstandings -- they test what it built, not what was asked -- so they
cannot be what decides.

It sees the code as the coder left it, the task and the docs. It does
not see the coder's transcript, and every test file the coder added or
changed reads as it was before the change (or as absent): `hidden` in its
workspace. It may only add new test modules under tests/, never edit
code, never add a conftest.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from core.coder import context
from core.coder.models import CATALOG
from core.coder.roles import RoleRun, prompt, run_role
from core.coder.snapshot import Snapshot
from core.coder.tools import TESTER_TOOLS, Tools
from core.coder.workspace import Workspace, builder_may_write


class TesterReport(BaseModel):
    summary: str
    tests: list[str] = []
    checks: list[str] = []


def coders_tests(base: Snapshot) -> dict[str, str | None]:
    """Every test file the coder added, changed or removed since `base`, as
    it was then (None where it did not exist)."""
    return {rel: base.had(rel) for rel in base.changed() if rel.startswith("tests/")}


def workspace(root: Path, base: Snapshot,
              library: dict[str, Path] | None = None) -> Workspace:
    return Workspace(root, builder_may_write, hidden=coders_tests(base), new_files_only=True,
                     library=library or {})


async def build_tests(task: str, tools: Tools, role, *, build=None) -> RoleRun:
    """`tools` over `workspace(root, base)`. The files it wrote are
    `tools.ws.created`."""
    first = context.pack(tools.ws, task, outline=tools.outline)
    changed = [rel for rel in (tools.base.changed() if tools.base else [])
               if rel not in tools.ws.hidden]
    if changed:
        first += "\n\n# Files the change touched\n\n" + "\n".join(changed)
    tools.calls.clear()
    return await run_role(
        model=CATALOG[role.model], system_prompt=prompt("tester"), first_message=first,
        tools=tools, tool_names=TESTER_TOOLS, output_type=TesterReport,
        budget_usd=role.budget_usd, max_turns=role.max_turns, max_seconds=role.max_seconds,
        build=build,
    )
