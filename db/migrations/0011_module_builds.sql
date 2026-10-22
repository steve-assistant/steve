-- Migration 11: what Steve builds for itself, it builds as a module.
--
-- Self-upgrade v2 (06-modules.md, section 8) writes a module in a draft
-- folder of its own, runs the coder, the test builder and the judge over
-- it, and ends at the user's card. `module_builds` is its ledger, which
-- `upgrades` was for v1: the request in the words it was asked and where
-- it came from, what Steve said it would cost and what it did, and how it
-- ended. A build that would pass the day's cap waits for the user's
-- allowance in the app, and one the cap stopped keeps its draft to go on.
--
-- `module_reports` holds what a build found it could not make into a
-- module: a slot Steve lacks, or a problem in Steve itself, waiting on the
-- Modules page. Steve sends neither anywhere.
--
-- `issue_attempts.upgrade_id` becomes `build_id`: the night's tries at an
-- issue are module builds now.

CREATE TABLE module_builds (
    id            INTEGER PRIMARY KEY,
    request       TEXT NOT NULL,                -- in the words it was asked
    origin        TEXT NOT NULL,                -- 'user' | 'night' | 'agent', as policy reads it
    came_from     TEXT NOT NULL DEFAULT '',     -- where, in words for the card
    session_id    TEXT,                         -- the conversation to tell
    module        TEXT NOT NULL DEFAULT '',     -- a new version's module; '' for a new one
    state         TEXT NOT NULL,                -- see agent/modbuild.py
    estimate_usd  REAL NOT NULL DEFAULT 0,
    estimate_s    REAL NOT NULL DEFAULT 0,
    allowed_usd   REAL NOT NULL DEFAULT 0,      -- past the day's cap, from the app
    cost_usd      REAL NOT NULL DEFAULT 0,
    seconds       REAL NOT NULL DEFAULT 0,
    said          TEXT NOT NULL DEFAULT '',     -- why it ended as it did
    admission     INTEGER REFERENCES module_admissions(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT
);

CREATE INDEX module_builds_state ON module_builds (state);

CREATE TABLE module_reports (
    id          INTEGER PRIMARY KEY,
    build_id    INTEGER NOT NULL REFERENCES module_builds(id),
    kind        TEXT NOT NULL,                  -- 'slot' | 'core', as the coder said (core/coder/roles.py)
    text        TEXT NOT NULL,                  -- a problem in core's, redacted
    state       TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'dismissed'
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

ALTER TABLE issue_attempts RENAME COLUMN upgrade_id TO build_id;
