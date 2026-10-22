"""The stored vectors are all one model's, and what has none is given one.

A vector compares only with one from the same model at the same size, and
nothing said which model had made the ones a database held (audit B7).
Step 3 moved the embedder from a local model at 384 to OpenAI's at 512: a
database from before would have refused every new note (its vector table
takes 384), and its facts, held at 384, would have been compared with
messages at 512 -- cut to the shorter by a loose `zip`, and given scores
that meant nothing. `core/db.open_store` now records the model and size,
empties the vectors once when they are not the code's, and the worker
embeds again what has none (`memory.reembed`, `facts.reembed`).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from agent import facts
from core import db
from tools import memory

OLD_DIM = 384


def _vector(dim: int = db.EMBEDDING_DIM, first: float = 0.1) -> list[float]:
    return [first] + [0.0] * (dim - 1)


def _old_shaped(path) -> tuple[int, int]:
    """A database as one from before step 3 was: no record of the model,
    the vector table at 384, a memory and a fact with a vector at 384.
    Returns (memory id, fact id)."""
    conn = db.open_store(path)
    try:
        conn.execute("DROP TABLE memory_vectors")
        conn.execute(db.VEC_TABLE_SQL.replace(f"FLOAT[{db.EMBEDDING_DIM}]", f"FLOAT[{OLD_DIM}]"))
        conn.execute("DELETE FROM vector_space")
        memory_id = conn.execute(
            "INSERT INTO memories (path, kind, summary, created_at)"
            " VALUES ('project/kitchen.md', 'project', 'Kitchen renovation',"
            " '2026-10-08 12:00:00')").lastrowid
        conn.execute("INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
                     (memory_id, memory._vec_literal(_vector(OLD_DIM))))
        conn.commit()
        return memory_id, _fact(conn, "Owen").id
    finally:
        conn.close()


def _fact(conn, value: str, attribute: str = "note") -> facts.Fact:
    return facts.add(conn, "world", attribute, value, source="web")


def _vectors(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]


@pytest.fixture
def embedder(monkeypatch):
    """A fake embedder at the code's size; what it was asked, in order."""
    asked: list[str] = []

    def embed(texts):
        asked.extend(texts)
        return [_vector() for _ in texts]

    monkeypatch.setattr(memory, "embed", embed)
    return asked


# ---------------------------------------------------------------------------
# What the vectors were made by
# ---------------------------------------------------------------------------

def test_a_new_database_records_the_model_and_size_of_its_vectors(tmp_path):
    with closing(db.open_store(tmp_path / "new.db")) as conn:
        assert db.vectors_made_by(conn) == (db.EMBEDDING_MODEL, db.EMBEDDING_DIM)


def test_a_database_of_another_models_vectors_is_emptied_of_them_once(tmp_path, embedder,
                                                                      monkeypatch):
    """Every row stays: only what was made by the old model goes, to be
    embedded again."""
    monkeypatch.setattr(memory, "embed", lambda texts: [_vector(OLD_DIM) for _ in texts])
    path = tmp_path / "old.db"
    memory_id, fact_id = _old_shaped(path)

    conn = db.open_store(path)
    try:
        assert db.vectors_made_by(conn) == (db.EMBEDDING_MODEL, db.EMBEDDING_DIM)
        assert _vectors(conn) == 0
        assert conn.execute("SELECT embedding FROM facts WHERE id = ?",
                            (fact_id,)).fetchone()[0] is None
        assert conn.execute("SELECT id FROM memories").fetchall()[0][0] == memory_id
        # The table takes the code's size now, and refuses the old one.
        conn.execute("INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
                     (memory_id, memory._vec_literal(_vector())))
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
                         (memory_id + 1, memory._vec_literal(_vector(OLD_DIM))))
        conn.commit()
    finally:
        conn.close()

    with closing(db.open_store(path)) as again:
        assert _vectors(again) == 1, "a second open threw away the new vectors"


def test_opening_a_database_of_the_codes_vectors_keeps_them(tmp_path, embedder):
    path = tmp_path / "kept.db"
    with closing(db.open_store(path)) as conn:
        conn.execute("INSERT INTO memory_vectors (memory_id, embedding) VALUES (1, ?)",
                     (memory._vec_literal(_vector()),))
        fact_id = _fact(conn, "Owen").id
        conn.commit()

    with closing(db.open_store(path)) as conn:
        assert _vectors(conn) == 1
        assert conn.execute("SELECT embedding FROM facts WHERE id = ?",
                            (fact_id,)).fetchone()[0] is not None


def test_a_rebuild_another_process_finished_first_is_not_done_again(store, embedder,
                                                                    monkeypatch):
    """Two processes opening an old database together both see it old; the
    one that takes the lock second must not empty what the first, by then,
    has embedded again."""
    store.execute("INSERT INTO memory_vectors (memory_id, embedding) VALUES (1, ?)",
                        (memory._vec_literal(_vector()),))
    store.commit()
    real = db.vectors_made_by
    reads: list[int] = []

    def stale_once(conn):
        reads.append(1)
        return None if len(reads) == 1 else real(conn)

    monkeypatch.setattr(db, "vectors_made_by", stale_once)

    assert db._vectors(store) is False
    assert _vectors(store) == 1 and len(reads) == 2


# ---------------------------------------------------------------------------
# Embedding again what has no vector
# ---------------------------------------------------------------------------

def test_each_memory_without_a_vector_is_embedded_from_its_file(
        store, memories_repo, embedder):
    """A row whose file is gone is left for `reconcile` to drop, not
    embedded from nothing."""
    kitchen = memory.remember(store, kind="project", summary="Kitchen renovation",
                              body="Quotes are due on Friday.")
    memory.remember(store, kind="project", summary="Garden", body="Bulbs in October.")
    with store:
        store.execute("DELETE FROM memory_vectors WHERE memory_id = (SELECT id FROM"
                            " memories WHERE path = ?)", (kitchen.path,))
        store.execute("INSERT INTO memories (path, kind, summary, created_at)"
                            " VALUES ('project/gone.md', 'project', 'Gone',"
                            " '2026-10-08 12:00:00')")
    embedder.clear()

    assert memory.reembed(store) == 1

    [text] = embedder
    assert text.startswith("Kitchen renovation\n\n") and "Quotes are due on Friday." in text
    have = {row[0] for row in store.execute(
        "SELECT path FROM memories JOIN memory_vectors ON memory_id = memories.id")}
    assert have == {kitchen.path, "project/garden.md"}
    assert memory.reembed(store) == 0, "embedded again what had a vector"


def test_each_active_fact_without_a_vector_is_embedded_and_left_unchanged(
        store, embedder, monkeypatch):
    """Kept while the embedder could not answer, a fact has no vector and
    is found by name only. The one it replaced is not worth a request, and
    embedding is no change to the fact: `updated_at` stays."""

    def unavailable(texts):
        raise memory.EmbedderUnavailable("memory search is unavailable: a test's outage")

    real = memory.embed
    monkeypatch.setattr(memory, "embed", unavailable)
    replaced = _fact(store, "Owen", attribute="manager")
    kept = _fact(store, "Priya", attribute="manager")
    store.execute("UPDATE facts SET updated_at = '2026-10-01 09:00:00'")
    store.commit()
    monkeypatch.setattr(memory, "embed", real)

    assert facts.reembed(store) == 1

    rows = {row["id"]: row for row in store.execute(
        "SELECT id, status, embedding, updated_at FROM facts")}
    assert rows[kept.id]["embedding"] is not None
    assert rows[replaced.id]["status"] != "active" and rows[replaced.id]["embedding"] is None
    assert rows[kept.id]["updated_at"] == "2026-10-01 09:00:00"
    assert facts.reembed(store) == 0


async def test_the_worker_embeds_what_has_none_and_an_outage_waits_for_the_next_round(
        store, memories_repo, embedder, monkeypatch, caplog):
    """The duty is what brings a step-2 database back to recall. A round
    the embedder cannot answer must leave the rest for the next, not fail
    the loop with a traceback every hour."""
    import logging

    from agent import runtime
    from agent.worker import background

    def unavailable(texts):
        raise memory.EmbedderUnavailable("memory search is unavailable: a test's outage")

    async def one_round(what, step, every_s):
        await step()

    real = memory.embed
    memory.remember(store, kind="project", summary="Garden", body="Bulbs in October.")
    with store:
        store.execute("DELETE FROM memory_vectors")
    monkeypatch.setattr(memory, "embed", unavailable)
    monkeypatch.setattr(runtime, "conn", store)
    monkeypatch.setattr(background, "_forever", one_round)
    fact = _fact(store, "Owen", attribute="manager")
    caplog.set_level(logging.INFO, logger=background.log.name)

    await background.reembed()

    assert "memories left without vectors for now" in caplog.text
    assert "facts left without vectors for now" in caplog.text
    monkeypatch.setattr(memory, "embed", real)

    await background.reembed()

    assert _vectors(store) == 1
    assert store.execute("SELECT embedding FROM facts WHERE id = ?",
                               (fact.id,)).fetchone()[0] is not None


# ---------------------------------------------------------------------------
# Vectors of two sizes never compare
# ---------------------------------------------------------------------------

def test_vectors_of_different_sizes_do_not_compare():
    with pytest.raises(ValueError):
        facts._cosine([1.0, 0.0], [1.0, 0.0, 0.0])


def test_a_fact_with_another_models_vector_is_passed_over_not_scored(store,
                                                                     monkeypatch):
    """Should one be left at another size, the facts lookup skips it and
    still finds the rest: one stale fact must not empty every turn's."""

    def embed(texts):
        return [[1.0, 0.0] if "alpha" in text else [1.0, 0.0, 0.0] for text in texts]

    monkeypatch.setattr(memory, "embed", embed)
    _fact(store, "alpha", attribute="first")
    beta = _fact(store, "beta", attribute="second")

    found = facts.relevant(store, "query")

    assert [f.id for f, _ in found] == [beta.id]
