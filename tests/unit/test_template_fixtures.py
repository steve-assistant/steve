"""The folders and repositories tests share are copies, and each test's is its own.

Twelve unit files made the same empty memories repository by hand
(measured 8 October 2026: `git init` 66 ms). tests/unit/conftest.py builds
it once per worker and copies it. The way that could go wrong is the way a
shared thing always does -- one test's write showing up in another's -- so
that, and the copy being the real thing, is what is pinned. The databases'
copies are pinned the same way in tests/unit/test_storecopy.py.
"""

from __future__ import annotations

import subprocess


def _git(root, *args) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


FILES = {"agent/x.py": "x = 1\n", "docs/y.md": "y\n"}


def test_a_folder_holds_exactly_the_files_it_was_given(folder, tmp_path):
    one = folder(FILES, dest=tmp_path / "one")
    other = folder({"only.txt": "this\n"}, dest=tmp_path / "other")

    assert sorted(p.name for p in one.iterdir()) == ["agent", "docs"]
    assert sorted(p.name for p in other.iterdir()) == ["only.txt"]


def test_the_files_are_written_with_lf_whatever_the_platform(folder):
    """A CRLF file is a different file to everything that hashes it (CLAUDE.md)."""
    made = folder(FILES)

    assert b"\r" not in (made / "agent/x.py").read_bytes()


def test_the_memories_repository_is_a_new_one_for_each_test_with_tools_memory_pointed_at_it(
        memories_repo):
    """And it has an identity of its own: the coding sandbox's account has no
    global git identity, and a memory written there would fail to commit."""
    from tools import memory

    assert memories_repo == memory.paths.memories_dir()
    assert (memories_repo / ".git").is_dir() and not any(
        p for p in memories_repo.iterdir() if p.name != ".git")
    assert _git(memories_repo, "config", "user.email") == "t@example.com"


def test_what_one_test_writes_to_its_memories_repository_never_reaches_the_template(
        memories_repo, _memories_template):
    (memories_repo / "note.md").write_text("kept\n", encoding="utf-8", newline="\n")
    _git(memories_repo, "add", "-A")

    assert not (_memories_template / "note.md").exists()
    assert _git(_memories_template, "status", "--porcelain") == ""

