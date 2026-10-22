"""Core reaches a provider's writes only through core/writes.py.

The write path checks one exact target, its condition and what changed
afterwards (`core/writes.py`), and it holds only where it is used: a tool
that called a provider's `delete` itself would skip every step, as the
password mailbox's bare EXPUNGE and the 11 October cleanup did. Every call
of a provider's write method outside the path is found here by its name
and its receiver (a provider: `p`, `provider`, `.provider`), and each must
be listed below with why it may go around: what creates something new has
no target to miss, and an op handed to `writes.modify` gets the item the
path checked.

A call inside a `writes.*` call's arguments is found too. The scan once
passed anything nested in one, so `writes.modify(p, n, lambda m:
p.delete(m))` passed: a delete with only a reversible change's checks, no
card and no check after.
"""

from __future__ import annotations

import ast

from steve_sdk import classes
from tests.gate import repo_files

OUTSIDE = ("modules/", "steve_sdk/", "tests/", "core/writes.py")

# Every method a provider writes through, from the classes' interfaces.
WRITES = frozenset({"make_draft", "send_draft", "delete", "modify", "move", "create",
                    "update", "complete", "create_document", "create_spreadsheet",
                    "update_spreadsheet"})

# (file, method): how many calls, and why each may go around the path.
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("agent/toolset/mail.py", "make_draft"): (
        2, "a draft is new: no target to miss. One is sent only by writes.send_draft"),
    ("agent/toolset/mail.py", "modify"): (
        1, "inside `change`, which runs only as writes.modify's op"),
    ("agent/mailbox.py", "modify"): (
        1, "writes.modify's op, on the message it checked: read and archived, both undone "
           "in a click"),
    ("agent/toolset/drive.py", "create_document"): (1, "a new document"),
    ("agent/toolset/drive.py", "create_spreadsheet"): (1, "a new spreadsheet"),
    ("agent/toolset/drive.py", "update_spreadsheet"): (
        1, "cells of the one spreadsheet named by id, gated in core/consent.yaml, so a "
           "card every time; Sheets' values API takes no condition "
           "(questions-05-06.md, 21)"),
    ("agent/notes.py", "create_document"): (1, "a new document"),
    ("agent/autonomy_eval.py", "make_draft"): (
        1, "the yardstick's world, standing in for an executor"),
}


def _providerish(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "p" or node.id.endswith("provider")
    if isinstance(node, ast.Attribute):
        return node.attr.endswith("provider")
    if isinstance(node, ast.Subscript):  # providers["mail"]
        held = node.value
        return (isinstance(held, ast.Name) and held.id.endswith("providers")) or (
            isinstance(held, ast.Attribute) and held.attr.endswith("providers"))
    return False


def _calls(tree: ast.AST) -> list[tuple[str, int]]:
    """Every call of a provider's write method in `tree`, wherever it is."""
    return [(node.func.attr, node.lineno) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in WRITES and _providerish(node.func.value)]


def _tracked() -> list[str]:
    # New files too: listing what git tracked alone, a new file's write
    # passed the suite run before it was added.
    return [f for f in repo_files.python() if not f.startswith(OUTSIDE)]


def test_the_writes_named_are_every_write_method_a_provider_class_declares():
    """A class that grew a write method this list lacks would be a door the
    scan below does not look at."""
    declared = set()
    for cls in (classes.MailProvider, classes.CalendarProvider, classes.FilesProvider,
                classes.TasksProvider):
        declared |= {name for name in vars(cls) if name in WRITES or name.startswith(
            ("make_", "send_", "delete", "modify", "move", "create", "update", "complete"))}
    assert declared == WRITES


def test_a_providers_write_handed_to_a_writes_call_is_still_found():
    """`writes.modify` checks only that its target is the one named, for a
    reversible change: a delete handed to it as its op would go with no
    card and no check after, past a scan that trusted the call around it."""
    assert _calls(ast.parse("writes.modify(p, n, lambda m: p.delete(m))")) == [("delete", 1)]
    assert _calls(ast.parse("writes.delete(p, n, card=card)")) == []


def test_no_code_outside_the_write_path_calls_a_providers_write_but_whats_listed():
    counted: dict[tuple[str, str], list[int]] = {}
    for path in _tracked():
        tree = repo_files.parsed(path)
        for method, line in _calls(tree):
            counted.setdefault((path, method), []).append(line)

    unlisted = {k: v for k, v in counted.items() if k not in ALLOWED}
    assert unlisted == {}, "a provider's write called outside core/writes.py"
    changed = {k: (len(counted.get(k, [])), n) for k, (n, _) in ALLOWED.items()
               if len(counted.get(k, [])) != n}
    assert changed == {}, "an entry's count no longer matches: (found, listed)"
