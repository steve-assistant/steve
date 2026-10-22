"""What Steve remembers of the two files that prove the person at the desk:
the passphrase's (`core/passphrase.py`) and Windows Hello's key
(`core/hello.py`).

Both files are the user's own, in the data folder, so anyone at an unlocked
desk can delete or replace them. Until step 11 a deleted `passphrase.json`
read as no passphrase ever set: whoever sat there chose their own, and it
answered every high-risk re-ask (the audit's E1). And a `hello.json`
holding their own key would have signed the reset of a forgotten one. So
the database keeps a digest of each as Steve last set it, and a file gone
or not matching reads as locked, never as unset.

The database is no wall: someone with time and SQLite's tools can clear a
row. It turns a file's deletion, which anyone can do in Explorer, into an
edit of Steve's database; and a new passphrase then takes Windows' own
proof (`core/winsignin.py`).

A store Steve has not made or migrated yet has nothing to remember: a
test's, or one before the backend's first start. Each function then does
nothing, and the files are read as before. Any other trouble with the
store raises: a lock held too long must not read as nothing remembered.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing

from core import paths

_UNMADE = "no such table: kept_digests"


def digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def get(name: str) -> str | None:
    """The digest kept for `name`, or None when none is."""
    rows = _run("SELECT digest FROM kept_digests WHERE name = ?", (name,))
    return rows[0][0] if rows else None


def keep(name: str, value: str) -> None:
    _run("INSERT INTO kept_digests (name, digest) VALUES (?, ?) ON CONFLICT (name) DO UPDATE"
         " SET digest = excluded.digest, kept_at = datetime('now')", (name, value))


def drop(name: str) -> None:
    _run("DELETE FROM kept_digests WHERE name = ?", (name,))


def _run(sql: str, args: tuple) -> list:
    path = paths.db_path()
    if not path.exists():
        return []
    # Opened read-write but never made here, so a check leaves no empty
    # database behind in a folder that had none.
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None,
                                 timeout=10)) as conn:
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as error:
            if str(error) == _UNMADE:
                return []
            raise
