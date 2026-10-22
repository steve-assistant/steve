"""Unit tests for the local store and credential handling."""

from __future__ import annotations

import sqlite3

import pytest

from core import db, paths, secrets

# These test migrating itself, so each database here migrates from nothing
# rather than being copied from the template (tests/gate/storecopy.py).
pytestmark = pytest.mark.real_migrations


@pytest.fixture
def store(tmp_path):
    conn = db.open_store(tmp_path / "test.db")
    yield conn
    conn.close()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def _schema(conn: sqlite3.Connection) -> list[tuple]:
    """Every object's SQL, which an added column changes too, and the
    versions recorded."""
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
    versions = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    return [tuple(r) for r in rows] + [("version", v[0], None) for v in versions]


def test_a_new_database_is_at_the_newest_migration(store):
    assert db.current_version(store) == db.migrations()[-1][0]


def test_migrating_again_changes_nothing(store):
    """Every open migrates; a database already there must come out as it
    went in."""
    before = _schema(store)
    assert db.migrate(store) == db.migrations()[-1][0]
    db.migrate(store)
    assert _schema(store) == before


def test_a_database_migrated_release_by_release_equals_a_new_one(store, tmp_path, monkeypatch):
    """A user's database meets each migration as their Steve updates; a new
    user's meets them all at once. Both must end the same."""
    every = db.migrations()
    conn = db.connect(tmp_path / "updated.db")
    try:
        for upto in range(1, len(every) + 1):
            monkeypatch.setattr(db, "migrations", lambda upto=upto: every[:upto])
            assert db.migrate(conn) == upto
        conn.execute(db.VEC_TABLE_SQL)
        assert _schema(conn) == _schema(store)
    finally:
        conn.close()


def test_a_database_from_before_accounts_were_tagged_loses_the_work_tied_to_old_ids_only(
        tmp_path, monkeypatch):
    """Data from before step 5 is not converted: a cursor, a queued
    observation or a handle under today's connection names belongs to no
    account, so no switch could ever reach it, and a card waiting on an old
    id would act on nothing the slot can route. History stays: a judged
    observation and the people ledger's rows already under slot ids."""
    from agent import approvals

    every = db.migrations()
    tagged = next(i for i, (version, *_) in enumerate(every) if version == 4)
    conn = db.connect(tmp_path / "updated.db")
    try:
        monkeypatch.setattr(db, "migrations", lambda: every[:tagged])
        db.migrate(conn)
        conn.executemany("INSERT INTO sync_cursors (source, cursor) VALUES (?, '1')",
                         [("attention:gmail",), ("gmail_sent",), ("outlook_received",),
                          ("briefing:morning",)])
        conn.executemany(
            "INSERT INTO observations (source, key, title, triaged_at) VALUES (?, ?, 't', ?)",
            [("gmail", "gmail:m1", None), ("dreamhost", "dreamhost:m2", None),
             ("threads", "thread:gmail:a:b", None), ("gmail", "gmail:m3", "2026-10-01"),
             ("calendar", "calendar:e1", None)])
        conn.execute("INSERT INTO handles (handle, full) VALUES ('outlook#1', 'AAMk1')")
        conn.execute("INSERT INTO newsletters_said (key, briefing) VALUES ('letter:m1', 'b')")
        person = conn.execute("INSERT INTO people (email) VALUES ('dana@example.com')").lastrowid
        conn.executemany(
            "INSERT INTO interactions (person_id, kind, direction, source_id, occurred_at)"
            " VALUES (?, ?, 'received', ?, '2026-10-01')",
            [(person, "email", "m1"), (person, "email", "mail:67d3d26cc730:m2"),
             (person, "event", "e1")])
        conn.executemany(
            "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal, status)"
            " VALUES (?, '{}', ?, 'p', 'outward', 'irreversible', ?)",
            [("send_draft", "h1", "pending"), ("create_event", "h2", "pending"),
             ("send_draft", "h3", "approved"), ("remember", "h4", "pending")])
        conn.commit()

        monkeypatch.setattr(db, "migrations", lambda: every[:tagged + 1])
        db.migrate(conn)

        def left(sql: str) -> list:
            return [tuple(r) if len(r) > 1 else r[0] for r in conn.execute(sql)]

        assert left("SELECT source FROM sync_cursors") == ["briefing:morning"]
        assert sorted(left("SELECT key FROM observations")) == ["calendar:e1", "gmail:m3"]
        assert left("SELECT COUNT(*) FROM handles") == [0]
        assert left("SELECT COUNT(*) FROM newsletters_said") == [0]
        assert sorted(left("SELECT source_id FROM interactions")) == [
            "e1", "mail:67d3d26cc730:m2"]
        assert left("SELECT tool, args_hash, status FROM actions ORDER BY id") == [
            ("send_draft", "h1", "withdrawn"), ("create_event", "h2", "withdrawn"),
            ("send_draft", "h3", "approved"), ("remember", "h4", "pending")]
        # Told why, as a card withdrawn by a switch is, within today's window.
        said = approvals.withdrawn(conn)
        assert sorted(a.tool for a in said) == ["create_event", "send_draft"]
        assert all("Steve was updated" in (a.withdrawn_reason or "") for a in said)
    finally:
        conn.close()


def _newer(store) -> int:
    newer = db.migrations()[-1][0] + 1
    store.execute("INSERT INTO schema_version (version, sha256) VALUES (?, 'unknown')",
                  (newer,))
    return newer


def test_a_starting_worker_refuses_a_database_from_a_newer_steve(store, tmp_path):
    """Its newer migrations are unknown here, and writing to it could undo
    them."""
    newer = _newer(store)
    with pytest.raises(db.NewerDatabase, match=f"schema version {newer}"):
        db.migrate(store, refuse_newer=True)
    with pytest.raises(db.NewerDatabase):
        db.open_store(tmp_path / "test.db", refuse_newer=True)
    server = (paths.SOURCE / "agent" / "server.py").read_text(encoding="utf-8")
    assert "db.open_store(refuse_newer=True)" in server, "the worker's own open refuses"


def test_a_worker_being_drained_still_opens_the_database_its_successor_migrated(store,
                                                                               tmp_path):
    """In a swap the new worker migrates first, and the old one finishes its
    jobs on the newer schema. Refused at every open, those jobs failed."""
    newer = _newer(store)
    before = _schema(store)

    assert db.migrate(store) == newer
    db.open_store(tmp_path / "test.db").close()
    assert _schema(store) == before


def test_a_migration_that_fails_leaves_the_database_as_it_was(store, tmp_path, monkeypatch):
    every = db.migrations()
    broken = tmp_path / f"{len(every) + 1:04d}_broken.sql"
    broken.write_text("CREATE TABLE half_done (x);\nCREATE TABLE half_done (x);\n",
                      encoding="utf-8", newline="\n")
    monkeypatch.setattr(db, "migrations", lambda: [*every, (len(every) + 1, broken)])
    before = _schema(store)

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        db.migrate(store)
    assert _schema(store) == before
    assert not store.in_transaction


def test_migrations_are_numbered_from_one_without_gaps(tmp_path, monkeypatch):
    """A file passed over is a change every later migration assumes."""
    monkeypatch.setattr(db, "MIGRATIONS", tmp_path)
    (tmp_path / "0001_first.sql").write_text("", encoding="utf-8")
    (tmp_path / "0003_third.sql").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not numbered"):
        db.migrations()
    (tmp_path / "0003_third.sql").rename(tmp_path / "2-second.sql")
    with pytest.raises(RuntimeError, match="named like"):
        db.migrations()


def _with_migration(monkeypatch, tmp_path, sql: str) -> int:
    """The shipped migrations and one more, `sql`; returns its number."""
    every = db.migrations()
    extra = tmp_path / f"{len(every) + 1:04d}_extra.sql"
    extra.write_text(sql, encoding="utf-8", newline="\n")
    monkeypatch.setattr(db, "migrations", lambda: [*every, (len(every) + 1, extra)])
    return len(every) + 1


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def test_a_migration_that_rebuilds_a_table_keeps_the_rows_that_refer_to_it(
        store, tmp_path, monkeypatch):
    """The usual way to change a column -- a new table, a copy, the old one
    dropped -- deleted every message through ON DELETE CASCADE while
    foreign keys were on, and raised nothing."""
    store.execute("INSERT INTO sessions (id, channel) VALUES (1, 'tray')")
    store.execute("INSERT INTO messages (session_id, role, content) VALUES (1, 'user', 'hi')")
    sql = store.execute("SELECT sql FROM sqlite_master WHERE name = 'sessions'").fetchone()[0]
    version = _with_migration(monkeypatch, tmp_path, (
        sql.replace("CREATE TABLE sessions", "CREATE TABLE sessions_new", 1) + ";\n"
        "INSERT INTO sessions_new SELECT * FROM sessions;\n"
        "DROP TABLE sessions;\n"
        "ALTER TABLE sessions_new RENAME TO sessions;\n"))

    assert db.migrate(store) == version
    assert _count(store, "messages") == 1
    assert store.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "on again after"


def test_a_migration_that_leaves_rows_pointing_nowhere_is_rolled_back(
        store, tmp_path, monkeypatch):
    """With foreign keys off nothing stops a migration orphaning rows, so
    `foreign_key_check` must come back empty before it commits."""
    store.execute("INSERT INTO sessions (id, channel) VALUES (1, 'tray')")
    store.execute("INSERT INTO messages (session_id, role, content) VALUES (1, 'user', 'hi')")
    _with_migration(monkeypatch, tmp_path, "DELETE FROM sessions;\n")
    before = _schema(store)

    with pytest.raises(db.BrokenMigration, match="pointing nowhere.*messages"):
        db.migrate(store)
    assert (_count(store, "sessions"), _count(store, "messages")) == (1, 1)
    assert _schema(store) == before and not store.in_transaction
    assert store.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("statement", [
    "BEGIN;", "COMMIT;", "END TRANSACTION;", "ROLLBACK;", "VACUUM;",
    "PRAGMA foreign_keys = ON;", "-- a note first\nbegin immediate;"])
def test_a_migration_cannot_end_the_transaction_it_runs_in(store, tmp_path, monkeypatch,
                                                           statement):
    """A COMMIT in the middle committed half a migration; a BEGIN failed on
    the transaction already open. Refused before anything runs."""
    _with_migration(monkeypatch, tmp_path,
                    f"CREATE TABLE half (x);\n{statement}\nCREATE TABLE other (x);\n")
    before = _schema(store)

    with pytest.raises(db.BrokenMigration, match="inside a transaction"):
        db.migrate(store)
    assert _schema(store) == before


def test_a_trigger_s_begin_and_end_belong_to_the_trigger(tmp_path):
    script = ("CREATE TABLE t (x);\n"
              "CREATE TRIGGER t_seen AFTER INSERT ON t\nBEGIN\n  DELETE FROM t;\nEND;\n"
              "-- the end\n")
    path = tmp_path / "0002_trigger.sql"
    path.write_text(script, encoding="utf-8", newline="\n")

    assert len(db.statements(script)) == 2
    assert db.check(path) == script


def test_every_shipped_migration_runs_inside_a_transaction():
    for _, path in db.migrations():
        db.check(path)


def test_a_migration_edited_after_the_database_applied_it_is_refused(store, tmp_path,
                                                                    monkeypatch):
    """Only the version was recorded, so a database made before an in-place
    edit of 0001 went on with the old schema, unnoticed."""
    every = db.migrations()
    edited = tmp_path / every[0][1].name
    edited.write_text(every[0][1].read_text(encoding="utf-8") + "\n-- edited\n",
                      encoding="utf-8", newline="\n")
    monkeypatch.setattr(db, "migrations", lambda: [(1, edited), *every[1:]])

    with pytest.raises(db.ChangedMigration, match="move the database aside"):
        db.migrate(store)


def test_a_migration_s_line_endings_are_not_a_change(store, tmp_path, monkeypatch):
    every = db.migrations()
    crlf = tmp_path / every[0][1].name
    crlf.write_bytes(every[0][1].read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(db, "migrations", lambda: [(1, crlf), *every[1:]])

    assert db.migrate(store) == every[-1][0]


def test_a_database_from_before_fingerprints_is_refused_with_what_to_do(tmp_path):
    conn = db.connect(tmp_path / "old.db")
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        with pytest.raises(db.ChangedMigration, match="Move it aside"):
            db.migrate(conn)
    finally:
        conn.close()


def test_the_switch_to_wal_waits_out_another_process_s_lock(monkeypatch):
    """The busy timeout does not cover it: a supervisor and its first worker
    opening a new database together, the second failed."""
    class _Locked:
        tries = 0

        def execute(self, sql):
            self.tries += 1
            if self.tries < 3:
                raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "WAL_PAUSE_S", 0)
    conn = _Locked()
    db.wal(conn)
    assert conn.tries == 3


def test_processes_opening_a_new_database_at_once_all_get_it_migrated_once(tmp_path):
    """Each read the version before the other had written it, and both ran
    the first migration: "table schema_version already exists"."""
    import threading

    for attempt in range(4):
        path = tmp_path / f"new-{attempt}.db"
        start = threading.Barrier(3)
        failed: list[BaseException] = []

        def open_it(path=path, start=start, failed=failed):
            start.wait()
            try:
                db.open_store(path).close()
            except BaseException as exc:  # noqa: BLE001 - reported below
                failed.append(exc)

        openers = [threading.Thread(target=open_it) for _ in range(3)]
        for opener in openers:
            opener.start()
        for opener in openers:
            opener.join()

        assert failed == []
        conn = db.connect(path)
        try:
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
        finally:
            conn.close()
        assert versions == [v for v, _ in db.migrations()]


def test_a_relative_data_folder_is_made_absolute_once_for_every_process(monkeypatch,
                                                                       tmp_path):
    """Resolved by each process against its own working folder, it put the
    supervisor's data in one place and its workers' in another. Children
    inherit the absolute folder."""
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(paths.DATA_DIR_ENV, "steve-data")

    assert paths.data_dir() == (tmp_path / "steve-data").resolve()
    assert os.environ[paths.DATA_DIR_ENV] == str((tmp_path / "steve-data").resolve())


def test_the_schema_has_one_source():
    """db/schema.sql ran at every open, and `ADDED_COLUMNS` added what it
    could not: two lists of columns, and one added to the first alone
    reached new databases only."""
    assert not (paths.SOURCE / "db" / "schema.sql").exists()
    assert not hasattr(db, "ADDED_COLUMNS")
    assert not hasattr(db, "ensure_schema")


def test_a_database_already_migrated_still_enforces_its_foreign_keys(store, tmp_path):
    """The pragma is per connection. It was in db/schema.sql, which ran at
    every open; a migration runs once, so connect() sets it."""
    again = db.open_store(tmp_path / "test.db")
    try:
        assert again.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        again.close()


def _sessions(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def test_a_block_that_fails_halfway_writes_nothing(store):
    """`with conn:` on Steve's connections, which are in autocommit, kept
    the first half: a fact inserted and the old one never superseded."""
    with pytest.raises(RuntimeError), store:
        store.execute("INSERT INTO sessions (channel) VALUES ('tray')")
        raise RuntimeError("the second statement fails")
    assert _sessions(store) == 1, "the premise: sqlite3's own block keeps the half"
    store.execute("DELETE FROM sessions")

    with pytest.raises(RuntimeError), db.transaction(store):
        store.execute("INSERT INTO sessions (channel) VALUES ('tray')")
        raise RuntimeError("the second statement fails")

    assert _sessions(store) == 0
    assert not store.in_transaction


def test_a_block_inside_another_undoes_only_its_own_writes(store):
    """A helper that writes in a transaction of its own may be called from
    inside another. Its failure, caught, must not undo the caller's writes
    or commit them early."""
    with db.transaction(store):
        store.execute("INSERT INTO sessions (channel) VALUES ('tray')")
        with pytest.raises(RuntimeError), db.transaction(store):
            store.execute("INSERT INTO sessions (channel) VALUES ('signal')")
            raise RuntimeError("the inner block fails")
        assert store.in_transaction, "the outer one is still open"

    assert [r[0] for r in store.execute("SELECT channel FROM sessions")] == ["tray"]


def test_a_transaction_holds_the_write_lock_from_its_start(store, tmp_path):
    """IMMEDIATE: a block that reads, then writes what it read, is not
    interleaved with another connection's write."""
    other = db.connect(tmp_path / "test.db")
    other.execute("PRAGMA busy_timeout = 0")
    try:
        with db.transaction(store), pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
    finally:
        other.close()


def test_no_code_takes_with_conn_for_a_transaction():
    """It is none on these connections (see above). Every block that
    writes as one uses `db.transaction`."""
    import ast

    from tests.gate import repo_files

    found = []
    for rel in repo_files.python("agent/", "core/", "tools/", "voice/", "channels/", "modules/",
                                 "devtools/"):
        for node in ast.walk(repo_files.parsed(rel)):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                expr = item.context_expr
                name = getattr(expr, "id", None) or getattr(expr, "attr", None) or ""
                if name.endswith("conn"):
                    found.append(f"{rel}:{node.lineno}")

    assert not found, f"`with conn:` is not a transaction here; use db.transaction: {found}"


def test_vector_table_is_queryable(store):
    """Proves the sqlite-vec extension actually loaded, not just that DDL ran."""
    store.execute(
        "INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
        (1, str([0.1] * db.EMBEDDING_DIM)),
    )
    row = store.execute(
        "SELECT memory_id, distance FROM memory_vectors "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
        (str([0.1] * db.EMBEDDING_DIM),),
    ).fetchone()
    assert row["memory_id"] == 1
    assert row["distance"] == pytest.approx(0.0, abs=1e-5)


def test_foreign_keys_cascade(store):
    store.execute("INSERT INTO sessions (id, channel) VALUES (1, 'tray')")
    store.execute(
        "INSERT INTO messages (session_id, role, content) VALUES (1, 'user', 'hi')"
    )
    store.execute("DELETE FROM sessions WHERE id = 1")
    assert store.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0


def test_job_status_is_constrained(store):
    """A typo'd status must fail loudly rather than creating an unrunnable job."""
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "INSERT INTO jobs (kind, prompt, status) VALUES ('research', 'x', 'kweued')"
        )


def test_wal_mode_is_active(store):
    mode = store.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


def test_status_reports_every_declared_key():
    keys = {s.key for s in secrets.status()}
    assert keys == set(secrets.KNOWN_KEYS)


def test_status_never_returns_secret_values():
    """A status listing gets shown in the UI and pasted into issues."""
    for s in secrets.status():
        assert not hasattr(s, "value")


def test_missing_secret_raises_with_a_usable_hint(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setattr(secrets.keyring, "get_password", lambda *_: None)
    with pytest.raises(secrets.MissingSecret) as exc:
        secrets.require("BRAVE_API_KEY")
    assert "python -m core.secrets set" in str(exc.value)


def test_the_command_line_asks_for_a_key_unseen_then_keeps_or_deletes_it(monkeypatch, capsys):
    """`python -m core.secrets set` and `delete`, as the README and the hint
    above give them. The value is asked for, never taken as an argument,
    where the shell's history would keep it, and never echoed. Found tested
    by nothing in step 11's map of features to tests."""
    import getpass
    import io
    import sys

    done: list[tuple] = []
    monkeypatch.setattr(secrets, "set_secret", lambda key, value: done.append(("set", key, value)))
    monkeypatch.setattr(secrets, "delete", lambda key: done.append(("delete", key)))
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "typed-unseen")
    # A key read as plain input would come from here, and fail the assert
    # below rather than on pytest's refusal to read a captured stdin.
    monkeypatch.setattr(sys, "stdin", io.StringIO("typed-in-plain-sight\n"))
    for argv in (["set", "OPENAI_API_KEY"], ["delete", "OPENAI_API_KEY"]):
        monkeypatch.setattr(sys, "argv", ["core.secrets", *argv])
        secrets._main()

    assert done == [("set", "OPENAI_API_KEY", "typed-unseen"), ("delete", "OPENAI_API_KEY")]
    assert "typed-unseen" not in capsys.readouterr().out


def test_environment_is_used_when_keyring_is_empty(monkeypatch):
    """A machine with no keyring backend still reads the environment."""
    monkeypatch.setattr(secrets.keyring, "get_password", lambda *_: None)
    monkeypatch.setenv("BRAVE_API_KEY", "from-env")
    assert secrets.get("BRAVE_API_KEY") == "from-env"


def test_broken_keyring_backend_falls_back_rather_than_crashing(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("no backend available")

    monkeypatch.setattr(secrets.keyring, "get_password", explode)
    monkeypatch.setenv("BRAVE_API_KEY", "fallback")
    assert secrets.get("BRAVE_API_KEY") == "fallback"


def test_steves_own_name_for_a_vendor_key_wins_over_the_vendors(monkeypatch):
    """`STEVE_BRAVE_API_KEY` is set for Steve alone; `BRAVE_API_KEY` may be
    set for some other program."""
    monkeypatch.setattr(secrets.keyring, "get_password", lambda *_: None)
    monkeypatch.setenv("BRAVE_API_KEY", "the vendor's name")
    monkeypatch.setenv("STEVE_BRAVE_API_KEY", "Steve's own")
    assert secrets.get("BRAVE_API_KEY") == "Steve's own"


@pytest.mark.parametrize("key", ["APPROVAL_TOTP_SECRET", "TELEGRAM_BOT_TOKEN"])
def test_a_setting_of_steves_own_is_never_read_under_its_bare_name(monkeypatch, key):
    """A bare `TELEGRAM_BOT_TOKEN` in the user's environment belongs to some
    other program, or to another install of this code; Steve's is
    `STEVE_TELEGRAM_BOT_TOKEN` (core/identity.py)."""
    monkeypatch.setattr(secrets.keyring, "get_password", lambda *_: None)
    monkeypatch.setenv(key, "someone else's")
    assert secrets.get(key) is None
    assert not next(s for s in secrets.status() if s.key == key).present

    monkeypatch.setenv("STEVE_" + key, "Steve's")
    assert secrets.get(key) == "Steve's"


def test_a_key_already_named_for_steve_is_read_under_that_name_alone():
    assert secrets.env_names("STEVE_EXAMPLE_PIN") == ("STEVE_EXAMPLE_PIN",)


def test_undeclared_keys_are_rejected():
    """Catches typos at write time instead of returning None much later."""
    with pytest.raises(KeyError):
        secrets.set_secret("OPENROTUER_API_KEY", "oops")


# --------------------------------------------------------------------------
# Orphan detection
#
# A substring search for "agent.server" matched the shell that launched the
# tests and nearly killed a healthy worker. These pin the matching shut.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmdline,expected",
    [
        ([r"C:\py\python.exe", "-m", "agent.server", "--port", "5000"], True),
        (["/usr/bin/python3", "-m", "agent.server", "--port", "1"], True),
        # The bug: a shell whose command line merely mentions the module.
        ([r"C:\Git\bin\bash.exe", "-c", "python -m agent.server --port 1"], False),
        # A supervisor is not a worker.
        ([r"C:\py\python.exe", "-m", "core.supervisor", "--port", "8790"], False),
        # A python process that only prints the string.
        ([r"C:\py\python.exe", "-c", "print('agent.server')"], False),
        ([], False),
        (["python.exe"], False),
    ],
)
def test_only_real_workers_match(cmdline, expected):
    from core.worker_handle import _is_worker_process

    assert _is_worker_process(cmdline) is expected
