-- Migration 15: what needs the user, put off or answered (agent/worker/needs.py).
--
-- A note in the app is for something with a cost if it is ignored: a card,
-- a reminder going off, something that stops Steve working. "Not now" puts
-- a stopped note away for an hour, and "Done" or "Later" answers a
-- reminder's; nothing kept either, so a note dismissed came back at the next
-- poll. And a reminder's message named its reminder only in the words the
-- conversation records ("[reminder 12 went off]"), so "Later" had nothing to
-- book again but a parse of prose.

CREATE TABLE put_off (
    kind  TEXT NOT NULL,                   -- the note's: now | stopped
    ref   TEXT NOT NULL,                   -- the note's id, within its kind
    until TEXT,                            -- UTC: back then; NULL: answered, for good
    at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (kind, ref)
);

ALTER TABLE outbox ADD COLUMN reminder_id INTEGER REFERENCES reminders(id) ON DELETE SET NULL;
