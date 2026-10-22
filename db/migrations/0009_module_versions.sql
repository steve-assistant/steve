-- Migration 9: a module from outside, from its admission to its versions.
--
-- Until now every module was built in, fixed by the release that shipped it,
-- and `modules` held one row each with nothing to approve. A module Steve
-- builds or the user imports is code from outside (06-modules.md, section 5):
-- it is checked, waits for the user's card, runs as the exact bytes approved,
-- and can be rolled back to a version before. `core/lifecycle.py` writes
-- these tables; nothing else does.
--
-- `modules.approved_hash` and `grants_json` go: nothing wrote them, and a
-- version's hash and grants are `module_versions`' now, the current one being
-- the row `modules.version` names. Two places holding the current hash would
-- be two answers to "may this code run".

ALTER TABLE modules DROP COLUMN approved_hash;
ALTER TABLE modules DROP COLUMN grants_json;

-- Each approved version still kept, the current one among them: the hash of
-- its folder (`code/<version>/`) as approved, the grants approved with it,
-- and when the module's `state/` was copied aside as this version left it,
-- before the next one started. Rolling back to a version restores that copy.
-- `id` orders them: two approved in the same second tie on `approved_at`,
-- and a table's hidden rowid may change when the database is vacuumed.
CREATE TABLE module_versions (
    id              INTEGER PRIMARY KEY,
    module          TEXT NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    version         TEXT NOT NULL,
    hash            TEXT NOT NULL,
    grants_json     TEXT NOT NULL,
    approved_at     TEXT NOT NULL DEFAULT (datetime('now')),
    copied_at       TEXT,
    UNIQUE (module, version)
);

-- A module checked and waiting for the user's card, or decided. Its files
-- wait in their own folder, `staged` under `core/paths.staged_dir`, and
-- `hash` is theirs as they were checked: approval installs them only if they
-- still match. The card is what the app shows, in its order, computed when
-- it was checked. At most one waits for each id: a newer one supersedes it.
CREATE TABLE module_admissions (
    id              INTEGER PRIMARY KEY,
    module          TEXT NOT NULL,
    version         TEXT NOT NULL,
    origin          TEXT NOT NULL CHECK (origin IN ('built', 'imported')),
    kind            TEXT NOT NULL CHECK (kind IN ('new', 'version', 'replace')),
    staged          TEXT NOT NULL,
    hash            TEXT NOT NULL,
    card_json       TEXT NOT NULL,
    made_at         TEXT NOT NULL DEFAULT (datetime('now')),
    state           TEXT NOT NULL DEFAULT 'waiting'
                    CHECK (state IN ('waiting', 'approved', 'declined', 'superseded')),
    decided_at      TEXT
);

CREATE UNIQUE INDEX idx_module_admissions_waiting ON module_admissions (module)
    WHERE state = 'waiting';

-- Each module from outside, watched since it was last turned on or changed
-- version: its calls, and those that failed by its own fault. `before_json`
-- is the counts of the version before an upgrade (`core/canary.Counts`), the
-- baseline the new one is judged against; none after a plain turn-on.
CREATE TABLE module_watch (
    module          TEXT PRIMARY KEY REFERENCES modules(id) ON DELETE CASCADE,
    since           TEXT NOT NULL DEFAULT (datetime('now')),
    calls           INTEGER NOT NULL DEFAULT 0,
    errors          INTEGER NOT NULL DEFAULT 0,
    crashes         INTEGER NOT NULL DEFAULT 0,
    timeouts        INTEGER NOT NULL DEFAULT 0,
    before_json     TEXT
);
