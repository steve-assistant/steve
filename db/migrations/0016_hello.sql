-- Migration 16: a high-risk yes in the app may carry Windows Hello's
-- signature in place of the passphrase.
--
-- Both prove the person at the machine (core/hello.py): the passphrase by
-- what they know, Hello by a key Windows uses only after their face, finger
-- or PIN. A Hello proof is bound to its card and the card's arguments as a
-- passphrase's is, so `factor` takes 'hello' too. SQLite cannot change a
-- CHECK in place, so the table is made again and its rows copied.

CREATE TABLE approval_proofs_new (
    action_id  INTEGER NOT NULL REFERENCES actions(id),
    factor     TEXT NOT NULL CHECK (factor IN ('code', 'passphrase', 'hello')),
    step       INTEGER UNIQUE,         -- a code's 30-second TOTP step (core/totp.py)
    args_hash  TEXT NOT NULL,          -- actions.args_hash when it was given
    at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (action_id, factor),
    CHECK ((factor = 'code') = (step IS NOT NULL))
);

INSERT INTO approval_proofs_new (action_id, factor, step, args_hash, at)
    SELECT action_id, factor, step, args_hash, at FROM approval_proofs;

DROP TABLE approval_proofs;

ALTER TABLE approval_proofs_new RENAME TO approval_proofs;
