"""SQLite access and schema management.

Frozen, and deliberately so. Schema changes are a MAJOR change requiring
human approval -- an agent that can silently reshape its own storage can
silently lose your data.

The schema is the numbered files in db/migrations/, each applied once, in
order and in a transaction, by `migrate`. The vector table is created from
Python rather than a migration because vec0 is a virtual table provided by an
extension that has to be loaded into the connection first.

And the vectors are made by one model at one size, recorded beside them
(`vector_space`, migration 2). Nothing recorded it (audit B7), and the
table was created once, at the size it had then: a model changed in code
would have gone on comparing its queries with another model's vectors, and
a size changed would have failed every insert. `open_store` rebuilds them
when the record is not the code's (`_vectors`).

What `migrate` guards against, each found by the audit of 12 October 2026:

- Foreign keys were on while a migration ran, so the standard way to change
  a table -- build a new one, copy, drop the old -- deleted every row that
  referenced the old one, through ON DELETE CASCADE, without an error. They
  are off while a migration runs, and `foreign_key_check` must come back
  empty before it commits.
- Two processes opening a new database at once crashed: the switch to WAL
  failed on the other's lock, and the version was read outside the
  transaction, so both ran the first migration. The switch is retried, and
  each migration re-reads the version inside a write transaction.
- Only the version number was recorded, so an edited migration went
  unnoticed: a database made before an in-place edit of 0001 carried on
  with the old schema. Each file's SHA-256 is recorded and compared.
- A migration holding BEGIN, COMMIT or VACUUM broke the transaction it runs
  in. Such a statement is refused before anything runs.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import sqlite_vec

from core import paths

MIGRATIONS = paths.SOURCE / "db" / "migrations"
_MIGRATION = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")

# What the vectors are: OpenAI's text-embedding-3-small, cut to 512
# dimensions (core/embed.py). A vector compares only with one of the same
# model and size, so changing either empties every stored vector
# (`_vectors`): a schema change, not a config. (A local model's, bge-small
# at 384, until step 3.)
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 512

VEC_TABLE_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
    memory_id INTEGER PRIMARY KEY,
    embedding FLOAT[{EMBEDDING_DIM}]
);
"""

# The account a row's id or key names: the first "<class>:<key>" in it, as
# `steve_sdk.ids` makes ids, where the key is the 12 hex digits core gives
# every account (`core/modules.new_key`). Exactly 12, so the class word of an
# id inside a key ("calendar:calendar:<key>:...") is never taken for a key.
# Or the module a module's attention item came from, "module:<key>:..."
# (`agent/attention/sources/modules.py`), which goes when the module is off.
_ACCOUNT_IN = re.compile(r"(?<![A-Za-z0-9_])(?:mail|calendar|files|tasks|module):"
                         r"([0-9a-f]{12})(?![0-9A-Za-z_-])")

# How long a new database's switch to WAL is retried while another process
# holds the lock it needs: the busy timeout does not cover that switch.
WAL_TRIES, WAL_PAUSE_S = 50, 0.1


def connect(path: Path | None = None, *, load_vec: bool = True) -> sqlite3.Connection:
    """Open the store, creating the parent directory if this is a first run.

    `path` defaults to `paths.db_path()` *when called*, not when this was
    defined. A default bound at definition time is a live-database path that
    nothing can redirect: the tests point the data folder at a temporary
    one, and a call that forgot its path used to open the real database.
    """
    path = paths.db_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; we manage txns
    conn.row_factory = sqlite3.Row

    # The busy timeout first, so the switch below already waits on a lock.
    conn.execute("PRAGMA busy_timeout = 10000")
    # The nightly upgrade path runs in a worker thread with its own
    # connection, so there is now genuinely more than one writer. WAL lets a
    # reader carry on during a write, and the busy timeout turns the rare
    # collision into a short wait rather than an immediate "database is
    # locked" in the middle of a three-minute upgrade attempt.
    wal(conn)
    # Both hold for this connection only. They were set by the schema
    # script, which ran at every open; a migration runs once.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")  # WAL makes full fsync unnecessary
    functions(conn)

    if load_vec:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            # Leaving extension loading enabled is an unnecessary foothold.
            conn.enable_load_extension(False)

    return conn


def account_in(text: object) -> str | None:
    """The key of the account, or module, `text` names (`_ACCOUNT_IN`), or
    None."""
    found = _ACCOUNT_IN.search(text) if isinstance(text, str) else None
    return found.group(1) if found else None


def functions(conn: sqlite3.Connection) -> None:
    """The SQL functions core's queries use, on `conn`: `account_in`, which
    `core/modules.live` reads a tagged row's account with. A copy of the
    database opened some other way (`agent/attention/replay.py`) calls this
    too."""
    conn.create_function("account_in", 1, account_in, deterministic=True)


def wal(conn) -> None:
    """Put the database in WAL mode, waiting out another process's lock.

    A new database's switch needs a lock the busy timeout does not wait for:
    a supervisor and its first worker opening it together, the second failed
    with "database is locked"."""
    for attempt in range(WAL_TRIES):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == WAL_TRIES - 1:
                raise
            time.sleep(WAL_PAUSE_S)


_SAVEPOINTS = itertools.count(1)


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A block whose writes all happen, or none do.

    `with conn:` reads as that and is not. Every connection here is in
    autocommit (`connect`), and there sqlite3's context manager commits a
    transaction if one is open but never opens one: each statement in the
    block was its own transaction. A fact inserted, then a failure before
    the old one was superseded, left both active; two writers that each
    read before writing interleaved. This opens one, IMMEDIATE, so the
    write lock is held from the first read in the block to the commit.

    Inside another transaction it is a savepoint of that one: its failure
    undoes its own writes, and the outer block decides the rest."""
    if conn.in_transaction:
        name = f"inner_{next(_SAVEPOINTS)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if conn.in_transaction:
        conn.execute("COMMIT")


class NewerDatabase(RuntimeError):
    """The database was written by a newer Steve than this one."""


class ChangedMigration(RuntimeError):
    """A migration this database applied is not the file this Steve has."""


class BrokenMigration(RuntimeError):
    """A migration that cannot run in a transaction, or left references
    pointing nowhere."""


def migrations() -> list[tuple[int, Path]]:
    """The files in db/migrations/, numbered from 1 with none missing.

    A file this cannot place is an error rather than skipped: a migration
    passed over silently is a column every later one assumes."""
    found = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        match = _MIGRATION.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"{path.name}: a migration is named like 0002_what_it_does.sql")
        found.append((int(match.group(1)), path))
    if [version for version, _ in found] != list(range(1, len(found) + 1)):
        raise RuntimeError(f"the migrations in {MIGRATIONS} are not numbered 1, 2, 3, ...")
    return found


# Statements that end, or try to open, the transaction a migration runs in;
# and the foreign-key switch, which `migrate` holds and SQLite ignores inside
# a transaction anyway.
_NOT_IN_A_MIGRATION = re.compile(
    r"(?:BEGIN|COMMIT|END|ROLLBACK|VACUUM|PRAGMA\s+foreign_keys)\b", re.IGNORECASE)
_LEADING_COMMENTS = re.compile(r"(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.DOTALL)


def statements(script: str) -> list[str]:
    """`script` cut into statements where SQLite itself would end them, so
    a trigger's BEGIN ... END stays one statement."""
    found, pending = [], ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            found.append(pending)
            pending = ""
    if _LEADING_COMMENTS.fullmatch(pending) is None:
        found.append(pending)
    return found


def check(path: Path) -> str:
    """The migration's text, once it is known to run inside a transaction."""
    script = path.read_text(encoding="utf-8")
    for statement in statements(script):
        body = statement[_LEADING_COMMENTS.match(statement).end():]
        if _NOT_IN_A_MIGRATION.match(body):
            raise BrokenMigration(
                f"{path.name}: {body.split(';')[0].strip()!r} -- a migration runs inside "
                f"a transaction `migrate` opens and commits, with foreign keys off")
    return script


def digest(script: str) -> str:
    """A migration's fingerprint; line endings do not count."""
    return hashlib.sha256(script.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _where(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return row["file"] or "the database"


def applied(conn: sqlite3.Connection) -> dict[int, str]:
    """Each migration the database has applied, with the digest it applied."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(schema_version)")}
    if not columns:
        return {}
    if "sha256" not in columns:
        raise ChangedMigration(
            f"{_where(conn)} was made by a development build of Steve from before "
            f"migrations were fingerprinted. Move it aside and Steve will make a new one.")
    return {row["version"]: row["sha256"]
            for row in conn.execute("SELECT version, sha256 FROM schema_version")}


def migrate(conn: sqlite3.Connection, *, refuse_newer: bool = False) -> int:
    """Bring the database up to the newest migration; return its version.

    Each file runs in one transaction with the row that records it, so a
    migration that fails leaves the database as it was.

    A database newer than this code is left alone, and refused only when
    `refuse_newer` says so: a worker starting (agent/server.py) must not
    write to a schema it does not know, but during a swap the worker being
    drained is older than the database its successor has just migrated, and
    refusing every open failed its last jobs.
    """
    known = migrations()
    newest = known[-1][0] if known else 0
    done = applied(conn)
    current = max(done, default=0)
    if current > newest and refuse_newer:
        raise NewerDatabase(
            f"the database is at schema version {current}, and this Steve knows up to "
            f"{newest}. A newer Steve wrote it; update this one rather than open it.")
    for version, path in known:
        if version in done and done[version] != digest(path.read_text(encoding="utf-8")):
            raise ChangedMigration(
                f"{path.name} is not the file {_where(conn)} applied as schema version "
                f"{version}. Before v0.1 that file may still be edited: move the database "
                f"aside and Steve will make a new one. After v0.1 a migration never "
                f"changes; a change is a new numbered file.")
    pending = [(version, path) for version, path in known if version > current]
    if not pending:
        return current
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for version, path in pending:
            _apply(conn, version, path)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return newest


def _apply(conn: sqlite3.Connection, version: int, path: Path) -> None:
    script = check(path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Read again under the write lock: another process opening the same
        # new database may have applied it since the version was read.
        if version in applied(conn):
            conn.execute("ROLLBACK")
            return
        # One statement at a time, not `executescript`, which commits an
        # open transaction before it starts: the version check above and the
        # foreign-key check below would each have been a transaction apart.
        for statement in statements(script):
            conn.execute(statement)
        if broken := conn.execute("PRAGMA foreign_key_check").fetchall():
            first = broken[0]
            raise BrokenMigration(
                f"{path.name} left {len(broken)} row(s) pointing nowhere, the first in "
                f"{first['table']} (row {first['rowid']}) to {first['parent']}")
        conn.execute("INSERT INTO schema_version (version, sha256) VALUES (?, ?)",
                     (version, digest(script)))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def current_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row["v"] is None else int(row["v"])


def open_store(path: Path | None = None, *, refuse_newer: bool = False) -> sqlite3.Connection:
    """connect() + migrate() + the vector table, which is what every caller
    actually wants."""
    conn = connect(path)
    try:
        migrate(conn, refuse_newer=refuse_newer)
        _vectors(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def vectors_made_by(conn: sqlite3.Connection) -> tuple[str, int] | None:
    """The model and size the stored vectors were made by, or None if
    nothing says."""
    row = conn.execute("SELECT model, dim FROM vector_space WHERE id = 1").fetchone()
    return (row["model"], int(row["dim"])) if row else None


def _vectors(conn: sqlite3.Connection) -> bool:
    """The vector table, for `EMBEDDING_MODEL` at `EMBEDDING_DIM`; True if
    the vectors were emptied to get there.

    Emptied once: under the write lock, read again, so two processes
    opening the database together do not both rebuild, and the second does
    not throw away what the first has since embedded. What was emptied is
    embedded again by the worker (`memory.reembed`, `facts.reembed`)."""
    space = (EMBEDDING_MODEL, EMBEDDING_DIM)
    if vectors_made_by(conn) == space:
        conn.execute(VEC_TABLE_SQL)
        return False
    with transaction(conn):
        if vectors_made_by(conn) == space:
            conn.execute(VEC_TABLE_SQL)
            return False
        conn.execute("DROP TABLE IF EXISTS memory_vectors")
        conn.execute(VEC_TABLE_SQL)
        conn.execute("UPDATE facts SET embedding = NULL WHERE embedding IS NOT NULL")
        conn.execute(
            "INSERT INTO vector_space (id, model, dim) VALUES (1, ?, ?) ON CONFLICT(id) DO"
            " UPDATE SET model = excluded.model, dim = excluded.dim, made_at = datetime('now')",
            space)
    return True


if __name__ == "__main__":
    with open_store() as c:
        tables = [
            r["name"]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        print(f"{paths.db_path()}")
        print(f"schema version {current_version(c)}")
        print(f"{len(tables)} tables: {', '.join(tables)}")
