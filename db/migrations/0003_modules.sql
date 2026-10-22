-- Migration 3: modules, and the accounts they serve.
--
-- Three mailboxes were names in code (gmail, outlook, dreamhost), each with
-- its settings in the frozen policy or the knobs, so a user could have one of
-- each and no more, and adding a provider meant editing a dozen files
-- (05-connectors.md). A provider is now a module, and what the user connects
-- is an account: a row here, written only by core's control API, never by a
-- model's tool.
--
-- `modules` is the registry: the switch for each installed module, not its
-- state (which is in its own folder, core/paths.module_dir). Its `key` is made
-- by the install and names the module's secrets in the Credential Manager, so
-- a module that reuses another's id cannot reach them. From step 6 it also
-- holds the approved code hash and grants.
--
-- `account_classes` holds "one address, one account per class": the same
-- mailbox through two modules would show every message twice. An account with
-- no address (a calendar link) has a NULL one, which SQLite never counts as a
-- duplicate.

CREATE TABLE modules (
    id              TEXT PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,
    version         TEXT NOT NULL,
    tier            TEXT NOT NULL CHECK (tier IN ('backend', 'sandbox')),
    origin          TEXT NOT NULL DEFAULT 'builtin'
                    CHECK (origin IN ('builtin', 'built', 'imported')),
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    approved_hash   TEXT,
    grants_json     TEXT,
    installed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE accounts (
    key             TEXT PRIMARY KEY,
    module          TEXT NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    preset          TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    fields_json     TEXT NOT NULL DEFAULT '{}',
    settings_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE account_classes (
    account         TEXT NOT NULL REFERENCES accounts(key) ON DELETE CASCADE,
    class           TEXT NOT NULL CHECK (class IN ('mail', 'calendar', 'files', 'tasks')),
    address         TEXT COLLATE NOCASE,
    UNIQUE (class, address)
);

CREATE INDEX account_classes_account ON account_classes(account);
