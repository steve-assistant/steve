"""A memory about a date says so, stops being said when the date has gone, and
every memory on disk is one recall can find.

On 5 October 2026 "Maya is heading out camping on 2026-10-02; do not assume
tomorrow is light" came back for "my run tomorrow", three days after the
trip, and nothing the user was shown about themselves carried a date. On 8 October ten
memory files were on disk and not in the index, swept into other memories'
commits by `git add -A`.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from zoneinfo import ZoneInfo

import pytest

from agent import history
from core import db
from tools import memory

TODAY = dt.date(2026, 10, 5)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    memories = tmp_path / "memories"
    memories.mkdir()
    subprocess.run(["git", "init", "-q", str(memories)], check=True)
    subprocess.run(["git", "-C", str(memories), "config", "user.email", "t@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(memories), "config", "user.name", "t"], check=True)
    monkeypatch.setenv("STEVE_MEMORIES_DIR", str(memories))
    monkeypatch.setattr(memory, "embed", lambda texts: [[0.1] * db.EMBEDDING_DIM for _ in texts])
    monkeypatch.setattr(memory, "_today", lambda: TODAY)
    monkeypatch.setattr(history, "_zone", lambda: ZoneInfo("America/New_York"))
    return memories


@pytest.fixture
def conn(tmp_path, repo):
    c = db.open_store(tmp_path / "m.db")
    yield c
    c.close()


def _tracked(repo) -> set[str]:
    out = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True,
                         check=True).stdout
    return set(out.split())


def test_a_plan_is_kept_with_its_last_day_in_the_file_and_the_index(conn, repo):
    stored = memory.remember(conn, kind="user", summary="Maya is camping 2026-10-02",
                             body="Adirondacks.", until="2026-10-02")

    assert "until: 2026-10-02" in (repo / stored.path).read_text(encoding="utf-8")
    assert conn.execute("SELECT until FROM memories").fetchone()[0] == "2026-10-02"
    assert memory.read(stored.path).until == "2026-10-02"


def test_a_last_day_that_is_not_a_date_is_refused(conn):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        memory.remember(conn, kind="user", summary="x", body="x", until="next week")


def test_a_memory_written_before_there_were_dates_still_reads(conn, repo):
    """Their files have no `until`; nothing about them may change."""
    (repo / "user").mkdir()
    (repo / "user" / "old.md").write_text(
        "---\nkind: user\nsummary: Maya is a product designer at Contoso.\ncreated: "
            "2026-09-20\n---\n\n"
        "Since 2024.\n", encoding="utf-8", newline="\n")

    assert memory.read("user/old.md").until is None
    assert memory.reindex(conn) == 1
    row = conn.execute("SELECT until, created_at FROM memories").fetchone()
    assert row["until"] is None and row["created_at"] == "2026-09-20"
    assert memory.export_all(conn) == [{"path": "user/old.md", "kind": "user",
                                        "summary": "Maya is a product designer at Contoso.",
                                        "body": "Since 2024."}]


def test_recall_leaves_out_a_plan_that_is_over_unless_the_past_is_asked_for(conn):
    memory.remember(conn, kind="user", summary="Maya is camping on 2026-10-02",
                    body="So 2 October is not light.", until="2026-10-02")
    memory.remember(conn, kind="user", summary="Maya runs in the mornings", body="Usually 8am.")

    now = [m.summary for m in memory.recall(conn, "my run tomorrow")]
    then = [m.summary for m in memory.recall(conn, "how was camping", past_too=True)]

    assert now == ["Maya runs in the mornings"]
    assert "Maya is camping on 2026-10-02" in then


def test_what_every_turn_reads_about_user_drops_a_plan_that_is_over_and_leads_with_one_near(
        conn):
    """On 5 October "heading out camping on 2026-10-02" was still in front of
    every turn, three days after the trip. The profile (agent/facts.py)
    holds a plan to its last day, and puts one in the next two weeks first
    however many other facts there are."""
    from agent import facts

    facts.add(conn, "user", "plan", "camping", source="user_words", valid_until="2026-10-02")
    facts.add(conn, "user", "plan", "a wedding on 2026-10-17", source="user_words",
              valid_until="2026-10-18")
    for i in range(6):
        facts.add(conn, "user", f"thing_{i}", f"a lasting fact, number {i}",
                  source="user_words")

    said = [f.value for f in facts.profile(conn, today=TODAY)]

    assert "camping" not in said, "a trip that is over is not a plan"
    assert said[0] == "a wedding on 2026-10-17" and len(said) == facts.PROFILE_MAX


def test_the_night_forgets_a_memory_a_week_after_its_last_day_and_no_other(conn, repo):
    memory.remember(conn, kind="user", summary="camping 2026-09-20", body="x",
                    until="2026-09-20")
    memory.remember(conn, kind="user", summary="camping 2026-10-02", body="x",
                    until="2026-10-02")
    memory.remember(conn, kind="user", summary="a lasting fact", body="x")

    gone = memory.expire(conn, today=TODAY)

    assert gone == ["user/camping-2026-09-20.md"]
    assert {r[0] for r in conn.execute("SELECT summary FROM memories")} == {
        "camping 2026-10-02", "a lasting fact"}


def test_remembering_commits_its_own_file_and_nothing_half_written_beside_it(conn, repo):
    (repo / "feedback").mkdir()
    (repo / "feedback" / "half-written.md").write_text("---\nkind: feed", encoding="utf-8")

    stored = memory.remember(conn, kind="user", summary="Northwind is lastname.firstname", body="x")

    assert _tracked(repo) == {stored.path}


def test_a_file_the_index_lost_is_found_and_indexed(conn, repo):
    memory.remember(conn, kind="user", summary="indexed already", body="x")
    (repo / "reference").mkdir()
    (repo / "reference" / "pittsburgh-coastal-weather.md").write_text(
        "---\nkind: reference\nsummary: Pittsburgh coastal weather\ncreated: 2026-10-01\n---\n\n"
        "Floods at high tide.\n", encoding="utf-8", newline="\n")

    added = memory.reconcile(conn)

    assert added == ["reference/pittsburgh-coastal-weather.md"]
    assert "reference/pittsburgh-coastal-weather.md" in _tracked(repo)
    assert [m.path for m in memory.recall(conn, "coastal weather", kinds=frozenset(
        {"reference"}))] == ["reference/pittsburgh-coastal-weather.md"]
    assert memory.reconcile(conn) == [], "once is enough"


def test_reconciling_where_there_are_no_files_drops_nothing(tmp_path, monkeypatch):
    """A folder that is not there is not a store emptied file by file."""
    files = tmp_path / "files"
    (files / "user").mkdir(parents=True)
    (files / "user" / "a.md").write_text("---\nkind: user\nsummary: a\n---\n\na\n",
                                         encoding="utf-8", newline="\n")
    monkeypatch.setenv("STEVE_MEMORIES_DIR", str(files))
    monkeypatch.setattr(memory, "embed", lambda texts: [[0.1] * db.EMBEDDING_DIM for _ in texts])
    conn = db.open_store(tmp_path / "agent.db")
    assert memory.reindex(conn) == 1
    monkeypatch.setenv("STEVE_MEMORIES_DIR", str(tmp_path / "absent"))

    assert memory.reconcile(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    conn.close()
