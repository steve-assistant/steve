"""The working tree is in the shape every checkout will see.

`core/policy.lock` hashes the frozen files as they sit on disk, and
`.gitattributes` checks every file out with LF line endings. So a file saved
with CRLF -- which is what Python's `write_text` produces on Windows unless
told otherwise -- makes the lock agree with this checkout and disagree with
every other one: a fresh worktree reported the frozen core as tampered with.
Ten frozen files were in that state before this test existed.
"""

from __future__ import annotations

import re
import subprocess
from functools import cache
from pathlib import Path

from tests.gate import repo_files

REPO = Path(__file__).resolve().parents[2]


@cache
def _tracked_text_files() -> tuple[Path, ...]:
    # The checks read each file's bytes, not repo_files.text: Python's text
    # reading turns a CRLF into LF, the very thing looked for here.
    return tuple(path for path in map(REPO.joinpath, repo_files.listed())
                 if b"\x00" not in path.read_bytes()[:8192])  # not binary


def test_no_file_has_windows_line_endings():
    offenders = [
        path.relative_to(REPO).as_posix()
        for path in _tracked_text_files()
        if b"\r\n" in path.read_bytes()
    ]

    assert not offenders, (
        "CRLF line endings; write files with newline='\\n' (see CLAUDE.md):\n  "
        + "\n  ".join(offenders)
    )


# Characters a reader cannot see: zero-width ones, the bidirectional marks,
# embeddings, overrides and isolates, the invisible operators, and the byte
# order mark. Written here as escapes, as everywhere else.
UNSEEN = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")


def test_no_file_holds_a_character_its_reader_cannot_see():
    """An override or an isolate makes an editor show a line in another order
    than Python reads it ("Trojan Source"), and a zero-width character hides
    inside a word: a reviewer approves code that does something else. Until
    step 11's audit, `core/screen.py`'s pattern for such characters in mail
    held them raw. The pattern meant them, but its line could not be read."""
    offenders = [
        f"{path.relative_to(REPO).as_posix()}:{number}"
        for path in _tracked_text_files()
        for number, line in enumerate(
            path.read_bytes().decode("utf-8", "replace").splitlines(), 1)
        if UNSEEN.search(line)
    ]

    assert not offenders, (
        "a character no reader sees; write it as a \\u escape:\n  "
        + "\n  ".join(offenders)
    )


def test_the_check_looks_at_the_real_tree():
    files = {p.relative_to(REPO).as_posix() for p in _tracked_text_files()}

    assert "core/policy.lock" in files
    assert "agent/loop.py" in files


def test_agents_md_says_what_claude_md_says():
    """AGENTS.md is CLAUDE.md for other coding agents (Codex). Untracked, it
    sat unchanged from 24 September while CLAUDE.md moved on. By 10 October
    it still said "no Anthropic models", listed six of eleven frozen paths
    and told agents to ask before spawning helpers. The maintainer, 10 October 2026:
    copy the content over. Edit CLAUDE.md, then copy it."""
    claude = (REPO / "CLAUDE.md").read_bytes()
    agents = (REPO / "AGENTS.md").read_bytes()

    assert agents == claude, "AGENTS.md differs from CLAUDE.md: copy CLAUDE.md over it"


def test_the_code_passes_its_own_lint():
    """`ruff check .` is one of the policy's gate commands, and a commit
    went through tonight with it failing, because the command was run and
    its result not checked. In the suite, it cannot be skipped."""
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "ruff", "check", ".", "--output-format", "concise"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )

    assert done.returncode == 0, done.stdout[-2000:]


def test_the_unit_fences_leave_nothing_in_the_machines_temp_folder():
    """Five `tempfile.mkdtemp()` folders a test, never removed: on 6 October
    2026 their %TEMP% held 721,477 empty `tmp*` folders, ~11,000 more with
    every suite run, and every temp-file operation on the laptop paid for
    the directory's size. The fences' scratch lives in the test's own
    tmp_path, which pytest rotates away."""
    import ast

    tree = ast.parse((REPO / "tests" / "unit" / "conftest.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, "attr", getattr(node.func, "id", "")) == "mkdtemp"]

    assert not calls, f"mkdtemp at line {calls[0].lineno}" if calls else ""
