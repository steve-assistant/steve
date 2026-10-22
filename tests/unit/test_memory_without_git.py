"""Memory keeps its history on a machine with no git.

A stranger's Windows has no git. Memory committed each remembered thing by
running the `git` program, so on an installed Steve the first "remember
this" would have failed, and the turn that asked with it (step 8). It now
writes the same repository through dulwich. These tests take git off the
path to prove it, and check that a repository the `git` program made, as
every one before step 8 was, reads and grows unchanged.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from core import db
from tools import memory

GIT = shutil.which("git")


@pytest.fixture
def no_git(memories_repo, tmp_path, monkeypatch):
    """The memories repository, with git nowhere on the path."""
    monkeypatch.setattr(memory, "embed", lambda texts: [[0.1] * db.EMBEDDING_DIM for _ in texts])
    empty = tmp_path / "no-git"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert shutil.which("git") is None
    return memories_repo


def _git(root, *args: str) -> str:
    """The real git, by its full path, reading what was written without it."""
    return subprocess.run([GIT, "-C", str(root), *args], capture_output=True, text=True,
                          check=True).stdout


pytestmark = pytest.mark.skipif(GIT is None, reason="reading the repository back needs git")


def test_a_machine_with_no_git_remembers_and_forgets(no_git, store):
    """The first "remember this" on an install must not break the turn."""
    remembered = memory.remember(store, kind="user", summary="likes tea", body="green")
    sha = store.execute("SELECT commit_sha FROM memories WHERE path = ?",
                              (remembered.path,)).fetchone()[0]

    assert sha and memory.forget(store, remembered.path)
    log = _git(no_git, "log", "--format=%H %an <%ae> %s").splitlines()
    assert [line.split(" ", 1)[1] for line in log] == [
        f"Steve memory <memory@steve.local> forget: {remembered.path}",
        "Steve memory <memory@steve.local> remember(user): likes tea"]
    assert log[1].split()[0] == sha
    assert _git(no_git, "status", "--porcelain") == ""


def test_a_repository_git_made_reads_and_grows_unchanged(memories_repo, tmp_path, monkeypatch,
                                                         store):
    """Every memories repository before step 8 was made and written by git."""
    (memories_repo / "user").mkdir()
    (memories_repo / "user" / "old.md").write_text("an old memory\n", encoding="utf-8",
                                                    newline="\n")
    _git(memories_repo, "add", "-A")
    _git(memories_repo, "commit", "-q", "-m", "remember(user): an old memory")
    monkeypatch.setattr(memory, "embed", lambda texts: [[0.1] * db.EMBEDDING_DIM for _ in texts])
    monkeypatch.setenv("PATH", str(tmp_path))

    memory.remember(store, kind="user", summary="a new memory", body="new")

    assert _git(memories_repo, "log", "--format=%s").splitlines() == [
        "remember(user): a new memory", "remember(user): an old memory"]
    assert _git(memories_repo, "ls-files").split() == ["user/a-new-memory.md", "user/old.md"]
    subprocess.run([GIT, "-C", str(memories_repo), "fsck", "--strict"], check=True,
                   capture_output=True)


def test_the_memories_repository_is_made_without_git(tmp_path, monkeypatch):
    """On a fresh install nothing has made it yet."""
    root = tmp_path / "fresh" / "memories"
    monkeypatch.setenv("STEVE_MEMORIES_DIR", str(root))
    monkeypatch.setenv("PATH", str(tmp_path))

    assert memory.ensure_repo() == root
    assert _git(root, "rev-parse", "--git-dir").strip() == ".git"


def test_nothing_changed_is_no_commit(no_git):
    (no_git / "note.md").write_text("x", encoding="utf-8")

    assert memory._commit("a note", "note.md")
    assert memory._commit("the same note", "note.md") is None
    assert len(_git(no_git, "log", "--format=%s").splitlines()) == 1


def test_a_commit_takes_only_the_paths_it_names(no_git):
    """A half-written memory beside it must not ride along in another's
    commit, as `git commit -- <paths>` kept it out."""
    (no_git / "mine.md").write_text("mine", encoding="utf-8")
    (no_git / "other.md").write_text("half-written", encoding="utf-8")

    memory._commit("mine", "mine.md")

    assert _git(no_git, "ls-files").split() == ["mine.md"]


def test_a_commit_naming_nothing_on_disk_is_refused(no_git):
    with pytest.raises(RuntimeError, match="matches no file"):
        memory._commit("nothing", "user/nope.md")
