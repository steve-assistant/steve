"""A module is shared only past a check for the user's own names, and a
database that cannot be read stops the share rather than emptying the check.

The names come from the store (agent/modshare.terms): the user's name, home
city and addresses from `facts`, the people they know from `people`. Each
read had a fallback for a database from before those tables existed, which
also caught "database is locked" -- and read it as nobody to keep out, so a
module's code went out unchecked for every person the user knows.
"""

from __future__ import annotations

import sqlite3

import pytest

from agent import facts, modshare
from core import db


class _Locked:
    """The store with one table unreadable, as when another process holds
    the database past the wait."""

    def __init__(self, conn: sqlite3.Connection, table: str):
        self.conn, self.table = conn, table

    def execute(self, sql: str, *args):
        if f"FROM {self.table}" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self.conn.execute(sql, *args)


@pytest.fixture
def store(tmp_path):
    conn = db.open_store(tmp_path / "share.db")
    with conn:
        conn.execute("INSERT INTO people (email, name, is_person) "
                     "VALUES ('iris@example.com', 'Iris Bergstrom', 1)")
    yield conn
    conn.close()


def test_the_check_holds_the_people_the_user_knows(store):
    assert "Iris Bergstrom" in modshare.terms(store)


def test_a_share_stops_when_the_people_cannot_be_read(store):
    with pytest.raises(sqlite3.OperationalError):
        modshare.terms(_Locked(store, "people"))


def test_the_users_name_is_never_read_as_not_given_when_the_facts_cannot_be_read(store):
    with pytest.raises(sqlite3.OperationalError):
        facts.identity(_Locked(store, "facts"))
