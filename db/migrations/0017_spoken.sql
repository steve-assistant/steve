-- Migration 17: a spoken exchange's one-line summary (agent/worker/journal.py).
--
-- A voice session is one row reused for hours, and the day page folds its
-- turns into exchanges under the user's first words, which are often only
-- "Hey" or a half-sentence. An exchange has no row of its own, so its
-- summary is keyed by the exchange's first message, which is how the page
-- names it. An empty summary is one the model gave nothing for: kept, so it
-- is not asked again every minute.

CREATE TABLE spoken_summaries (
    first_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    summary  TEXT NOT NULL,
    made_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
