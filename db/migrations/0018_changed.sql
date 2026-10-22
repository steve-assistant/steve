-- Migration 18: when an action, a message or a listening note last changed
-- (agent/worker/journal.py).
--
-- The open day's page asks only for what changed since its last look. An
-- action's outcome (done, failed, expired) was written with no time, so a
-- send that failed after the page last looked kept saying "You said yes ·
-- under way" until the page was reloaded. A message the outbox stopped
-- trying, and a listening note's summary and document arriving after it
-- ended, went unseen the same way.
--
-- Stamped by trigger, not by each write: there are several ways to write
-- each, and the next one added would forget.

ALTER TABLE actions ADD COLUMN changed_at TEXT;
ALTER TABLE outbox ADD COLUMN changed_at TEXT;
ALTER TABLE notes ADD COLUMN changed_at TEXT;

CREATE TRIGGER actions_changed AFTER UPDATE OF status, error ON actions
BEGIN
    UPDATE actions SET changed_at = datetime('now') WHERE id = NEW.id;
END;

CREATE TRIGGER outbox_changed AFTER UPDATE OF stopped ON outbox
BEGIN
    UPDATE outbox SET changed_at = datetime('now') WHERE id = NEW.id;
END;

CREATE TRIGGER notes_changed AFTER UPDATE OF status, title, body, doc_url ON notes
BEGIN
    UPDATE notes SET changed_at = datetime('now') WHERE id = NEW.id;
END;
