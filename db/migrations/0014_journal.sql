-- Migration 14: the day page's own state (agent/worker/journal.py).
--
-- A reaction to a reply went only into the failure ledger, keyed by the
-- reply's words, so nothing could say which reply the user had marked, and
-- the page could not keep the mark drawn. And "new since you last looked"
-- needs the one moment the user last looked: per-entry read state was only
-- the outbox's, and the design needs nothing finer.

CREATE TABLE reactions (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    verdict    TEXT NOT NULL CHECK (verdict IN ('right', 'not right')),
    at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE looked (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    until TEXT NOT NULL                    -- UTC: what the user had seen, up to then
);
