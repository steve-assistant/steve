-- Migration 24: what Steve remembers of the passphrase and the Windows
-- Hello key, as Steve last set them.
--
-- Both live in files of the user's own (passphrase.json, hello.json), so
-- someone at an unlocked desk could delete the passphrase's and choose
-- their own, which then answered every high-risk re-ask (step 11's audit,
-- E1). With a digest of each kept here, a file gone or swapped reads as
-- locked, never as unset (core/kept.py).

CREATE TABLE kept_digests (
    name    TEXT PRIMARY KEY CHECK (name IN ('passphrase', 'hello')),
    digest  TEXT NOT NULL,
    kept_at TEXT NOT NULL DEFAULT (datetime('now'))
);
