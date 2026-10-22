-- Migration 4: a card names its account, and can be withdrawn.
--
-- A card waiting for a yes acts on what was true when it was made. When the
-- account it acts on is switched off or removed, a later yes would act on an
-- account the user has turned away from, and a card that just vanished would
-- leave the user's late reply told only that it "may have expired"
-- (05-connectors.md, section 1). So a card carries the key of the account
-- its tool acts on (`account`, NULL for a tool of no account), and its
-- pending cards are withdrawn with a reason (`withdrawn_reason`) the late
-- reply, the history and Steve's next turn all read.
--
-- SQLite cannot widen a CHECK in place, so the table is made again and its
-- rows copied. Foreign keys are off while a migration runs (core/db.migrate),
-- so the tables that point at `actions` keep pointing at it by name.

CREATE TABLE actions_new (
    id             INTEGER PRIMARY KEY,
    tool           TEXT NOT NULL,
    args_json      TEXT NOT NULL,
    args_hash      TEXT NOT NULL,          -- same request twice is one card
    preview        TEXT NOT NULL,
    reach          TEXT NOT NULL,          -- private | outward
    reversal       TEXT NOT NULL,          -- reversible | irreversible
    reasons        TEXT,                   -- JSON array: why it was gated
    spend_usd      REAL NOT NULL DEFAULT 0,
    session_id     INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    channel        TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    token          TEXT,                   -- the spoken word used over Signal
    ask_message_ts INTEGER,                -- so a quote-reply can be matched
    asked_at       TEXT,
    expires_at     TEXT,
    decided_at     TEXT,
    decided_by     TEXT,                   -- signal | tray | voice | auto
    executed_at    TEXT,
    result_json    TEXT,
    undo_json      TEXT,
    undone_at      TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    account        TEXT,                   -- the key of the account it acts on
    withdrawn_reason TEXT,                 -- why it was withdrawn, for the user
    CHECK (status IN ('pending','approved','executing','done','failed',
                      'rejected','expired','undone','withdrawn'))
);

INSERT INTO actions_new (id, tool, args_json, args_hash, preview, reach, reversal, reasons,
                         spend_usd, session_id, channel, status, token, ask_message_ts,
                         asked_at, expires_at, decided_at, decided_by, executed_at,
                         result_json, undo_json, undone_at, error, created_at)
SELECT id, tool, args_json, args_hash, preview, reach, reversal, reasons,
       spend_usd, session_id, channel, status, token, ask_message_ts,
       asked_at, expires_at, decided_at, decided_by, executed_at,
       result_json, undo_json, undone_at, error, created_at
FROM actions;

DROP TABLE actions;

ALTER TABLE actions_new RENAME TO actions;

CREATE INDEX idx_actions_pending
    ON actions(expires_at) WHERE status = 'pending';

-- Partial, so a short wordlist is not exhausted for all time: a token only
-- has to be unique among the things currently waiting for an answer.
CREATE UNIQUE INDEX idx_actions_token
    ON actions(token) WHERE status = 'pending';

CREATE INDEX idx_actions_account
    ON actions(account) WHERE status = 'pending';

-- Data from before step 5 is not converted (05-connectors.md, section 1): no
-- one runs the product yet, and accounts are added again. What today's
-- connections left under their own names, which no account's switch could
-- reach, goes: the mailboxes' poll and ingest cursors, mail and thread
-- observations not yet judged, the handles of the old ids, the newsletters
-- said by them, and the ledger's mail interactions, which the next ingest
-- reads again under the slot's ids (and recounts) rather than twice.
DELETE FROM sync_cursors
 WHERE source IN ('attention:gmail', 'attention:outlook', 'attention:dreamhost',
                  'gmail_sent', 'gmail_received', 'outlook_sent', 'outlook_received');

DELETE FROM observations
 WHERE triaged_at IS NULL
   AND (source IN ('gmail', 'outlook', 'dreamhost')
        OR (source = 'threads' AND key NOT LIKE 'thread:mail:%'));

DELETE FROM handles;

DELETE FROM newsletters_said;

DELETE FROM interactions WHERE kind = 'email' AND source_id NOT LIKE 'mail:%';

-- A card waiting on an id from before acts on nothing the slot can route.
-- `decided_at` is the local time with a 'T', as `agent/approvals.decide`
-- writes it and `approvals.withdrawn` compares it: `datetime()` puts a
-- space there, which sorts before the 'T', so the column would not sort as one.
UPDATE actions
   SET status = 'withdrawn',
       withdrawn_reason = 'Steve was updated after this was asked, and its accounts are added again',
       decided_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')
 WHERE status = 'pending'
   AND tool IN ('send_email', 'send_draft', 'delete_draft', 'create_event', 'update_event',
                'delete_event', 'update_spreadsheet');
