"""An edit whose `old` is almost right lands, or says where to look.

135 of the 591 edit_file calls in the records of upgrades 59-88 (23%) were
refused with "`old` occurs 0 times", most often in docs/flows.md, which has
29 arrows and 29 em dashes; six coders ran out of their 60 turns. The
likeliest cause is a model typing those back as -> and --. The rules that
make the tools the security model still hold: a frozen path is refused
however `old` matches, and nothing loose touches Python's indentation.
"""

from __future__ import annotations

import pytest

from core.coder.tools import Ran, Tools, summary
from core.coder.workspace import Workspace, coder_may_write

FLOWS = (
    "## 12. A change to itself\n"
    "\n"
    "The night → the ledger → the coder — then the judge.\n"
    "Maya’s “agree 57” is recorded.  Nothing else.\n"
)
CODE = "def add(a, b):\n    return a + b\n\n\ndef twice(a):\n    return add(a, a)\n"


class _Runner:
    def __call__(self, argv, cwd, timeout_s=600):
        return Ran(0, "1 passed", 0.1)


@pytest.fixture
def tools(tmp_path):
    for rel, text in {"docs/flows.md": FLOWS, "agent/calc.py": CODE,
                      "core/policy.yaml": "frozen: true — always\n"}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8", newline="\n")
    return Tools(Workspace(tmp_path, coder_may_write), _Runner(), "python.exe")


def _read(tools, rel):
    return (tools.ws.root / rel).read_text(encoding="utf-8")


def test_dashes_arrows_and_quotes_typed_plain_still_find_their_line(tools):
    said = tools.edit_file("docs/flows.md",
                           old="The night -> the ledger -> the coder -- then the judge.\n",
                           new="The night -> the ledger -> the coder -> the judge.\n")

    assert said.startswith("edited docs/flows.md (loose match)")
    assert _read(tools, "docs/flows.md") == FLOWS.replace(
        "The night → the ledger → the coder — then the judge.\n",
        "The night -> the ledger -> the coder -> the judge.\n")


def test_curly_quotes_and_a_doubled_space_typed_plain_still_match(tools):
    said = tools.edit_file("docs/flows.md", old="Maya's \"agree 57\" is recorded. Nothing",
                           new="Her \"agree 57\" is recorded. Nothing")

    assert said.startswith("edited")
    assert _read(tools, "docs/flows.md").endswith("Her \"agree 57\" is recorded. Nothing else.\n")


def test_indentation_is_never_read_loosely(tools):
    """Python's syntax: an `old` indented differently is a different line."""
    said = tools.edit_file("agent/calc.py", old="def add(a, b):\n        return a + b\n",
                           new="def add(a, b):\n        return a - b\n")

    assert said.startswith("refused:")
    assert _read(tools, "agent/calc.py") == CODE


def test_a_loose_match_in_more_than_one_place_is_refused(tools):
    tools.write_file("docs/twice.md", "a — b\nc\na -- b\n")

    said = tools.edit_file("docs/twice.md", old="a - b", new="x")

    assert said.startswith("refused: `old` occurs 0 times")
    assert _read(tools, "docs/twice.md") == "a — b\nc\na -- b\n"


def test_a_frozen_path_is_refused_however_old_matches(tools):
    said = tools.edit_file("core/policy.yaml", old="frozen: true -- always", new="frozen: false")

    assert said.startswith("refused:")
    assert _read(tools, "core/policy.yaml") == "frozen: true — always\n"


def test_a_refusal_names_the_nearest_line_so_the_next_try_starts_from_it(tools):
    said = tools.edit_file("agent/calc.py", old="    return add(a, b)\n", new="x")

    assert said.startswith("refused: `old` occurs 0 times in agent/calc.py")
    assert "line 6: return add(a, a)" in said


def test_an_old_that_occurs_twice_is_refused_saying_where(tools):
    said = tools.edit_file("agent/calc.py", old="    return a", new="x")

    assert said.startswith("refused: `old` occurs 2 times") and "lines 2, 6" in said


def test_the_record_counts_loose_edits_apart_from_exact_ones_and_refusals(tools):
    """So the stage record says whether the guess about typographic
    characters was right."""
    tools.edit_file("docs/flows.md", old="the coder -- then", new="the coder, then")
    tools.edit_file("agent/calc.py", old="    return a + b\n", new="    return b + a\n")
    tools.edit_file("agent/calc.py", old="nothing like this", new="x")

    line = summary(tools.log)

    assert "edit_file 3 (1 loose, 1 refused)" in line
