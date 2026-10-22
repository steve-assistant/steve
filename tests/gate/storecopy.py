"""A test's new database is a copy of one migrated once in its process.

Each test has a data folder of its own (fences.py), so nearly every test
opens a new database, and migrating one from nothing ran all the
migrations again: about 92 ms a database against 24 ms for a copy, and
thousands of databases in a run of the suite (21 October 2026).

The copy is taken only where migrating would make exactly it: a database
with nothing in it yet, migrated by the migrations the template was made
from. A test that migrates by hand with some of the migrations, or an
edited one, or opens a database that already holds tables, migrates for
real; so does every test marked `real_migrations`, as the tests of
migrating itself are (tests/unit/test_store.py). The real `migrate` runs
after the copy all the same, and checks each migration's fingerprint
against the files, as it does on any database already there.
"""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from core import db

_real_migrate = db.migrate
_template: tuple[list[tuple[int, Path]], Path] | None = None


def copy_new_stores(monkeypatch, folder: Path) -> None:
    """Make each new, empty database a copy of a template migrated once.

    `folder` is the process's own (pytest's base temporary folder), where
    the template is made the first time, before the test can change the
    migrations."""
    global _template
    if _template is None:
        path = folder / "store-template.db"
        with contextlib.closing(db.connect(path)) as conn:
            _real_migrate(conn)
        _template = (db.migrations(), path)
    known, path = _template

    def migrate(conn: sqlite3.Connection, *, refuse_newer: bool = False) -> int:
        if (db.migrations() == known
                and conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None):
            # Opened afresh for each copy: a connection may be used only in
            # the thread that made it, and a store is opened in many.
            with contextlib.closing(sqlite3.connect(f"{path.as_uri()}?immutable=1",
                                                    uri=True)) as source:
                source.backup(conn)
        return _real_migrate(conn, refuse_newer=refuse_newer)

    monkeypatch.setattr(db, "migrate", migrate)


# A new database for a test of what is stored, under the two names tests give
# it. Both trees' conftests take these from here: 42 test files each defined
# one of them, word for word. They stay two databases, as they were, so a
# test that asks for both still has two.
@pytest.fixture
def store(tmp_path):
    conn = db.open_store(tmp_path / "store.db")
    yield conn
    conn.close()


@pytest.fixture
def conn(tmp_path):
    conn = db.open_store(tmp_path / "conn.db")
    yield conn
    conn.close()
