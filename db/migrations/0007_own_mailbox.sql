-- Migration 7: the agent's own mailbox on any mail account that takes
-- plus-addresses, and the user's addresses kept by the app.
--
-- The mailbox was a Gmail address in the frozen policy, with the user's
-- addresses beside it, and was read through Gmail alone (05-connectors.md,
-- section 7). Now the user chooses the account in the app, and opts in
-- each address that counts as theirs. Both are written by the app's routes
-- only (tests/gate/test_only_the_app.py): a tool that could add a sender
-- could make anyone the user.

-- One row, while the mailbox is on: Steve's address is the account's with
-- `+tag`. Removing the account turns the mailbox off.
CREATE TABLE own_mailbox (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    account     TEXT NOT NULL REFERENCES accounts(key) ON DELETE CASCADE,
    tag         TEXT NOT NULL DEFAULT 'agent',
    chosen_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The addresses whose mail can be the user's instruction: a connected
-- account's, once opted in, or one the user typed. `own_domain` is the
-- user's "only I send as this domain", without which a custom or work
-- domain's mail counts by its Sent copy alone.
CREATE TABLE user_addresses (
    address     TEXT PRIMARY KEY COLLATE NOCASE,
    own_domain  INTEGER NOT NULL DEFAULT 0 CHECK (own_domain IN (0, 1)),
    added_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A mail's RFC Message-ID, and the copy its text is read from: for the
-- user's own mail, the copy in their Sent folder, never the inbound one,
-- which someone on the thread could have written. A Message-ID gives one
-- instruction, however many copies of it arrive.
ALTER TABLE mailbox ADD COLUMN ident TEXT;
ALTER TABLE mailbox ADD COLUMN read_from TEXT;
CREATE UNIQUE INDEX mailbox_one_instruction_a_message
    ON mailbox (ident) WHERE kind = 'instruction' AND ident IS NOT NULL;
