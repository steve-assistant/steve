"""A test's new database is copied from a template only where migrating would
have made the same database (tests/gate/storecopy.py).

The copy saves about 50 ms a database, and is worth nothing if it changes
what a test sees: a schema a migration test built by hand overwritten, a
test of an older schema handed the newest, or a store that fails in the
thread a worker opens it in.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from core import db
from tests.gate import storecopy


def _schema(conn: sqlite3.Connection) -> list[tuple]:
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
    versions = conn.execute("SELECT version, sha256 FROM schema_version "
                            "ORDER BY version").fetchall()
    return [tuple(r) for r in rows] + [tuple(v) for v in versions]


@pytest.fixture
def applied(monkeypatch):
    """The migrations applied one by one, as the real `migrate` applies them."""
    seen: list[int] = []
    real = db._apply

    def spy(conn, version, path):
        seen.append(version)
        return real(conn, version, path)

    monkeypatch.setattr(db, "_apply", spy)
    return seen


@pytest.mark.real_migrations
def test_a_copied_database_equals_one_migrated_from_nothing(monkeypatch, tmp_path,
                                                             tmp_path_factory):
    """The copy stands in for migrating only if the two are the same database:
    every table, index and trigger, and every version with its fingerprint."""
    reference = db.open_store(tmp_path / "migrated.db")
    storecopy.copy_new_stores(monkeypatch, tmp_path_factory.getbasetemp())
    copied = db.open_store(tmp_path / "copied.db")
    try:
        assert _schema(copied) == _schema(reference)
    finally:
        reference.close()
        copied.close()


def test_a_new_database_is_copied_rather_than_migrated(tmp_path, applied):
    """What the template is for: a test's new store runs no migration."""
    conn = db.open_store(tmp_path / "new.db")
    conn.close()
    assert applied == []


def test_a_database_that_already_holds_a_table_is_migrated_not_overwritten(tmp_path,
                                                                            applied):
    """A test that builds an old schema by hand, then migrates it, tests
    the migrations; a copy over it would erase what it built."""
    conn = db.connect(tmp_path / "built.db")
    try:
        conn.execute("CREATE TABLE kept (note TEXT)")
        conn.execute("INSERT INTO kept VALUES ('built by hand')")
        conn.commit()
        db.migrate(conn)
        assert conn.execute("SELECT note FROM kept").fetchone()[0] == "built by hand"
    finally:
        conn.close()
    assert applied == [version for version, _ in db.migrations()]


def test_a_database_migrated_with_other_migrations_gets_those_alone(monkeypatch, tmp_path,
                                                                     applied):
    """A test of an older release's schema lists fewer migrations; the
    template, made from all of them, would give it the newest."""
    some = db.migrations()[:5]
    monkeypatch.setattr(db, "migrations", lambda: some)
    conn = db.connect(tmp_path / "older.db")
    try:
        assert db.migrate(conn) == some[-1][0]
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
    finally:
        conn.close()
    assert versions == [version for version, _ in some]
    assert applied == versions


def test_a_write_to_one_copy_never_reaches_the_next(tmp_path, applied):
    """Each copy is its own: a row one test writes would otherwise be in
    every store opened after it."""
    first = db.open_store(tmp_path / "first.db")
    try:
        with first:
            first.execute("INSERT INTO sessions (channel) VALUES ('signal')")
    finally:
        first.close()
    second = db.open_store(tmp_path / "second.db")
    try:
        assert second.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        second.close()
    assert applied == []


def test_a_copied_database_is_in_wal_mode(tmp_path):
    """Readers beside a writer need WAL, as `connect` sets it on a real store."""
    conn = db.open_store(tmp_path / "wal.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_a_store_opened_in_another_thread_is_copied_there(tmp_path, applied):
    """The worker opens stores in threads of its own; a template connection
    shared between threads would fail in every one but the first."""
    found: list[object] = []

    def open_one(name: str) -> None:
        try:
            conn = db.open_store(tmp_path / name)
            try:
                found.append(conn.execute("SELECT MAX(version) FROM schema_version")
                             .fetchone()[0])
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - reported below, in the test's thread
            found.append(exc)

    threads = [threading.Thread(target=open_one, args=(f"t{i}.db",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    newest = db.migrations()[-1][0]
    assert found == [newest, newest]
    assert applied == []
