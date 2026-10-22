"""What a role knows before its first tool call.

The old coder started from the proposal alone and spent its first turns
finding its way around. Worse, it did not know the rules it wrote under --
where the code runs, what it may import, how tests are named -- which is
where most of its rejected attempts went wrong. So every role starts with:

- the task;
- the module guide, `docs/modules.md`, seen as `_reference/modules.md`: the
  SDK, the manifest, the slots, and for a connector which engine or which
  template;
- the draft's files so far, which for a new version are the old version's;
- the reference files the task most likely concerns, each with its outline:
  a built-in module or a template that does something like it.

Relevance is lexical and deliberately simple: words and identifiers from
the task, weighted in the path over the text. It only has to point at the
right neighbourhood; the role reads from there.
"""

from __future__ import annotations

import re

from core.coder.workspace import REFERENCE, Refused, Workspace

MAX_RELEVANT = 8
GUIDE = REFERENCE + "modules.md"
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{3,}")
_COMMON = {"that", "this", "with", "from", "have", "when", "into", "should", "would",
           "every", "their", "there", "which", "what", "does", "make", "only", "also",
           "test", "tests", "file", "files", "code", "change", "agent", "user"}


def _doc(ws: Workspace, rel: str) -> str:
    try:
        return ws.read(rel)
    except (Refused, OSError):
        return ""


def words(task: str) -> set[str]:
    found = set()
    for word in _WORD.findall(task):
        low = word.lower().strip(".")
        if low not in _COMMON:
            found.add(low)
            found.update(part for part in re.split(r"[._/]", low) if len(part) > 3)
    return found


def relevant(ws: Workspace, task: str, limit: int = MAX_RELEVANT) -> list[str]:
    wanted = words(task)
    if not wanted:
        return []
    scored = []
    for rel in ws.files():
        if not rel.endswith(".py"):
            continue
        path = rel.lower()
        try:
            head = "\n".join(ws.read(rel).splitlines()[:80]).lower()
        except (Refused, OSError):
            continue
        score = sum(3 for w in wanted if w in path) + sum(1 for w in wanted if w in head)
        if score:
            scored.append((score, rel))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [rel for _, rel in scored[:limit]]


def pack(ws: Workspace, task: str, *, outline) -> str:
    """The first message a role reads. `outline` is `Tools.outline`."""
    own = [rel for rel in ws.files() if not rel.startswith(REFERENCE)]
    parts = [
        "# The task\n\n" + task.strip(),
        "# How a module is written\n\n" + (_doc(ws, GUIDE) or f"({GUIDE} is missing)"),
        "# The module's files so far\n\n" + ("\n".join(own) or "none: it is a new module"),
    ]
    files = relevant(ws, task)
    if files:
        parts.append("# Files that look relevant\n\n" + "\n\n".join(
            f"## {rel}\n{outline(rel)}" for rel in files))
    return "\n\n".join(p for p in parts if p.strip())
