"""Memories are written one at a time, and a commit another process is
making is waited out rather than failed on.

On 5 October 2026 a turn broke with "git add -A: Unable to create
'.../memories/.git/index.lock': File exists" -- after the memory's file was
written and before it was indexed, so it sat on disk invisible to recall.
The tool, reflection and the night each write from a thread of their own.
"""

from __future__ import annotations

import threading
import time

import pytest

from core import db
from tools import memory


@pytest.fixture
def repo(memories_repo, monkeypatch):
    monkeypatch.setattr(memory, "embed", lambda texts: [[0.1] * db.EMBEDDING_DIM for _ in texts])
    return memories_repo


def test_two_threads_remembering_at_once_take_turns_with_the_repo(repo, tmp_path, monkeypatch):
    inside = {"now": 0, "most": 0}
    guard = threading.Lock()
    real = memory._commit_once

    def watched(*args, **kwargs):
        with guard:
            inside["now"] += 1
            inside["most"] = max(inside["most"], inside["now"])
        time.sleep(0.02)
        try:
            return real(*args, **kwargs)
        finally:
            with guard:
                inside["now"] -= 1

    monkeypatch.setattr(memory, "_commit_once", watched)
    path = tmp_path / "t.db"
    db.open_store(path).close()
    start = threading.Barrier(4)
    failed: list[BaseException] = []

    def write(i: int) -> None:
        conn = db.open_store(path)
        start.wait()
        try:
            memory.remember(conn, kind="project", summary=f"fact {i}", body=f"body {i}")
        except BaseException as exc:  # noqa: BLE001 - reported below
            failed.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    conn = db.open_store(path)
    assert not failed and conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 4
    assert inside["most"] == 1, "two writers were in the repo at once"
    conn.close()


def test_another_processes_commit_is_waited_out(repo, monkeypatch):
    """A retiring worker may be committing during a swap: its index.lock is
    on disk, and nothing may stage until it is gone."""
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    tries = {"n": 0}
    real = memory._commit_once

    def the_other_finishes_on_the_third(*args, **kwargs):
        tries["n"] += 1
        if tries["n"] == 3:
            lock.unlink()
        return real(*args, **kwargs)

    monkeypatch.setattr(memory, "_commit_once", the_other_finishes_on_the_third)
    monkeypatch.setattr(memory, "LOCKED_WAIT_S", 0.0)
    (repo / "note.md").write_text("x", encoding="utf-8")

    assert memory._commit("a note", "note.md") and tries["n"] == 3


def test_a_lock_that_is_never_let_go_fails_the_commit_in_the_end(repo, monkeypatch):
    """Waited out for a while, not for ever: a lock left by a crash would
    otherwise hang the turn."""
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(memory, "LOCKED_WAIT_S", 0.0)
    (repo / "note.md").write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeError, match="index.lock"):
        memory._commit("a note", "note.md")


def test_a_git_failure_that_is_not_the_lock_is_not_retried(repo, monkeypatch):
    tries = {"n": 0}

    def broken(*args, **kwargs):
        tries["n"] += 1
        raise RuntimeError("git: not a git repository")

    monkeypatch.setattr(memory, "_commit_once", broken)

    with pytest.raises(RuntimeError, match="not a git repository"):
        memory._commit("a note", "note.md")
    assert tries["n"] == 1
