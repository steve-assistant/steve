"""Recall about them finds facts about them, not the night's notes or the judge's lessons.

On 6 October 2026, of 125 memories 7 were facts about them; "Maya class
schedule" came back as lessons about creating events and research notes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import db
from tools import memory


@pytest.fixture
def store(tmp_path, monkeypatch):
    conn = db.open_store(tmp_path / "kinds.db")
    files = tmp_path / "files"
    monkeypatch.setenv("STEVE_MEMORIES_DIR", str(files))
    # Every memory the same distance away: what comes back is decided by the
    # filters alone, in the order the index returns them.
    monkeypatch.setattr(memory, "embed", lambda texts: [[0.1] * db.EMBEDDING_DIM for _ in texts])
    for rel, kind, summary in [
        ("feedback/a.md", "feedback",
         "answered: When asked to add events, use the email's details"),
        ("reference/b.md", "reference",
         "Practical workflow for turning confirmations into events"),
        ("user/c.md", "user", "Maya is a product designer at Contoso who takes pottery"),
        ("feedback/d.md", "feedback", "She wants drafts short, signed Maya"),
        ("project/e.md", "project", "Maya is looking at design roles in Denver"),
    ]:
        (files / rel).parent.mkdir(parents=True, exist_ok=True)
        (files / rel).write_text(f"---\nkind: {kind}\nsummary: {summary}\n---\n\nx\n",
                                 encoding="utf-8", newline="\n")
    memory.reindex(conn)
    yield conn
    conn.close()


def _found(conn, **kwargs):
    return sorted(m.path for m in memory.recall(conn, "class schedule", limit=5, **kwargs))


def test_about_user_is_their_facts_only(store):
    assert _found(store, kinds=memory.ABOUT["user"], lessons=False) == ["project/e.md",
                                                                        "user/c.md"]


def test_anything_leaves_out_the_judges_lessons_but_not_their_corrections(store):
    found = _found(store, kinds=memory.ABOUT["any"], lessons=False)
    assert "feedback/a.md" not in found
    assert {"feedback/d.md", "reference/b.md", "user/c.md"} <= set(found)


def test_how_to_work_includes_the_lessons(store):
    assert _found(store, kinds=memory.ABOUT["how"], lessons=True) == [
        "feedback/a.md", "feedback/d.md", "reference/b.md"]


def test_the_limit_counts_what_is_shown_not_what_was_filtered(store):
    assert len(memory.recall(store, "x", limit=2, kinds=memory.ABOUT["user"],
                             lessons=False)) == 2


async def test_the_recall_tool_passes_what_it_is_about(monkeypatch):
    from agent import facts
    from agent.toolset import memory as tools

    asked = []

    async def with_own_conn(conn, fn, query, **kwargs):
        return fn(conn, query, **kwargs)

    def recall(conn, query, **kwargs):
        asked.append(kwargs)
        return []

    monkeypatch.setattr(tools.offloop, "with_own_conn", with_own_conn)
    monkeypatch.setattr(memory, "recall", recall)
    monkeypatch.setattr(facts, "search", lambda conn, query, **kwargs: [])
    found: dict = {}
    tools.register(lambda fn: found.setdefault(fn.__name__, fn))
    ctx = SimpleNamespace(deps=SimpleNamespace(tools_used=[], conn=None, channel="signal"))

    await found["recall"](ctx, "their classes", about="user")
    await found["recall"](ctx, "weather method")
    await found["recall"](ctx, "x", about="nonsense")
    await found["recall"](ctx, "how to answer weather", about="how")

    assert [(a["kinds"], a["lessons"], a["research"]) for a in asked] == [
        (memory.ABOUT["user"], False, False), (memory.ABOUT["any"], False, False),
        (memory.ABOUT["any"], False, False), (memory.ABOUT["how"], True, True)]


async def test_what_the_night_reads_is_filed_as_reference(monkeypatch):
    """Three of its research notes were filed as `project`, and came back
    for questions about them."""
    from agent.toolset import memory as tools

    stored = []

    async def with_own_conn(conn, fn, **kwargs):
        stored.append(kwargs["kind"])
        return SimpleNamespace(path="x.md", kind=kwargs["kind"])

    monkeypatch.setattr(tools.offloop, "with_own_conn", with_own_conn)
    found: dict = {}
    tools.register(lambda fn: found.setdefault(fn.__name__, fn))

    for channel in ("night", "signal"):
        ctx = SimpleNamespace(deps=SimpleNamespace(tools_used=[], conn=None, channel=channel))
        await found["remember"](ctx, kind="project", summary="Handle vague requests", body="x")

    assert stored == ["reference", "project"]
