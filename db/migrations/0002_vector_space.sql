-- Migration 2: what the stored vectors were made by.
--
-- A vector compares only with one from the same model at the same size.
-- Nothing said which model had made the ones a database held (audit B7):
-- an embedder changed in code would go on comparing new queries with old
-- vectors, and a size changed would fail every insert into the vector
-- table, which was created once, at the old size. core/db.open_store reads
-- this row and, when it is not the code's model and size, empties
-- `memory_vectors` and `facts.embedding` and records the new ones, once;
-- the worker then embeds again what has no vector.
--
-- No row is a database from before this one, whose vectors are a local
-- model's (bge-small, at 384), and are rebuilt.

CREATE TABLE vector_space (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    made_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
