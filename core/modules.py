"""The modules installed, which are on, and the accounts they serve.

Mail, calendar and files were names in code: `gmail`, `outlook` and
the password mailbox were branched on in about seventy files, and each had its own
switch in the knobs or the frozen policy. A user could have one of each and
no more. Now every provider is a module, and what the user connects is an
account (05-connectors.md, section 1):

- **Built-in modules** live in the repo under `modules/<id>/`, ship inside the
  install, which nothing at runtime writes, and are parsed by
  `steve_sdk/manifest.py`, the one parser. Each gets a registry row on first
  sight, on by default, with a key the install makes.
- **The registry** (`modules`) is the switch, not the module's state, which is
  in its own folder (`core/paths.module_dir`).
- **Accounts** are rows written only through these functions, which only
  core's control API calls; a gate test holds it from step 5's item 5.
  Each has an opaque key, the name the user gave it, its module, its
  classes and its addresses. "One address, one account per
  class" is a unique key in the database, not a check that could be skipped.
- **Modules from outside,** built by Steve or imported, are admitted and
  versioned by `core/lifecycle.py`; their manifests are read from the
  version the registry names, only while its files are the ones approved
  (`known`), and one turns on only as the bytes approved.
- **Removing** a module removes its accounts in the same transaction.

**Off means original.** `TAGGED` lists every core table holding rows that
came from a module's items, and how each row names its account (05-context.md,
section 7). While an account or its module is off, live paths read those
tables through `live`, which skips its rows, and its pending cards are
withdrawn with a reason (`withdraw_cards`): a yes given later would act on
what was true before, and a card that just vanished would leave the user's
late reply told only that it "may have expired".
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from core import addons, credstore, db, health, paths
from steve_sdk.context import Account
from steve_sdk.manifest import Manifest, ManifestError, load

log = logging.getLogger("modules")

BUILTIN_DIR = paths.SOURCE / "modules"


def _by_id(column: str) -> str:
    """Keep a row unless the id or key in `column` names an account that is
    not live; a row naming none is core's own, and kept."""
    return f"coalesce(account_in({{t}}.{column}) IN ({{live}}), 1)"


# Tables with rows from a module's items (05-context.md, section 7), and the
# SQL that keeps a row while its account is live: `{t}` is the table as the
# query names it, `{live}` the keys of the live accounts and of the modules
# that are on (`live_keys`). Each row names its account inside an id Steve
# handed out (`steve_sdk.ids`), or, for a card, in a column of its own; a
# module's own card names the module, and so does one its routine asked for
# (`routine_module`), and its attention items name it in
# their key (`module:<key>:...`). `live` is the one way a live path reads these tables; a gate
# test finds any other read, and a schema test fails on a new table with a
# `source` or `*_id` column that is in neither this nor `CORE`.
TAGGED: dict[str, str] = {
    "observations": _by_id("key"),
    "sync_cursors": _by_id("source"),
    "newsletters_said": _by_id("key"),
    "actions": ("coalesce({t}.account IN ({live}), 1) "
                "AND coalesce({t}.routine_module IN ({live}), 1)"),
    "interactions": _by_id("source_id"),
    "obligations": _by_id("source_id"),
    "handles": _by_id("full"),
    "mailbox": _by_id("message_id"),
    # A person is from an account through their interactions: skipped only
    # when every interaction they have is from an account that is not live.
    # One with none (named by the user, or from a fact) is the user's own.
    "people": ("(NOT EXISTS (SELECT 1 FROM interactions WHERE person_id = {t}.id "
               "AND account_in(source_id) IS NOT NULL) OR EXISTS (SELECT 1 FROM "
               "interactions WHERE person_id = {t}.id AND coalesce(account_in(source_id) "
               "IN ({live}), 1)))"),
}

# Tables with a `source` or `*_id` column whose rows are core's own, and why.
CORE: dict[str, str] = {
    "approval_proofs": "a card's proof of approval, kept with the card's history",
    "browser_steps": "the browser's own steps",
    "crews": "jobs Steve runs",
    "critic_reviews": "Steve's own turns",
    "facts": "what the user said is true; may quote mail, and stays as their history",
    "goals": "the user's goals",
    "issue_attempts": "the night's tries at Steve's own issues",
    "issue_events": "Steve's own issues",
    "issues": "Steve's own issues, by Steve's message ids",
    "jobs": "jobs Steve runs",
    "looked": "when the user last looked at the journal",
    "memory_vectors": "the memory's vectors",
    "memory_vectors_chunks": "the memory's vectors",
    "memory_vectors_rowids": "the memory's vectors",
    "message_photos": "the photos on a user's message, with the conversation",
    "messages": "the conversation, the user's history",
    "module_builds": "Steve's own builds of modules",
    "module_reports": "Steve's own builds of modules",
    "model_switches": "Steve's own evaluations",
    "outbox": "what Steve sent the user",
    "person_aliases": "a person's other names, with the person",
    "put_off": "the app's notes answered or put off",
    "reactions": "the user's marks on Steve's replies, with the conversation",
    "reflections": "Steve's own turns",
    "reminders": "the user's reminders",
    "routines": "the user's routines",
    "screened": "a cache keyed by content, holding no account",
    "skill_uses": "Steve's own skills",
    "spoken_summaries": "a spoken exchange's summary, with the conversation",
    "told": "what Steve told the user, their history",
    "turn_stats": "Steve's own turns",
    "usage_events": "Steve's own spending",
    "verdicts": "an observation's, deleted with it",
}

WITHDRAWN_OFF = "Steve's access to that account was turned off"
WITHDRAWN_REMOVED = "that account was removed from Steve"
WITHDRAWN_MOVED = "that account's {klass} was moved to another account in Steve"


class AccountError(ValueError):
    """An account change refused, said for the user."""


class Held(AccountError):
    """An address's class another account already holds: the app offers to
    move it here, which adding again with `take` does."""

    def __init__(self, klass: str, address: str, name: str) -> None:
        super().__init__(f"{address}'s {klass} is already connected as \"{name}\"; "
                         f"move it here instead")
        self.klass, self.address, self.name = klass, address, name


@dataclass(frozen=True)
class Module:
    id: str
    key: str
    version: str
    tier: str
    origin: str
    enabled: bool
    manifest: Manifest | None


@dataclass(frozen=True)
class AccountRow:
    key: str
    module: str
    name: str
    preset: str | None
    enabled: bool
    classes: tuple[str, ...]
    addresses: tuple[str, ...]
    fields: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def address(self) -> str:
        return self.addresses[0] if self.addresses else ""

    def as_account(self) -> Account:
        return Account(key=self.key, name=self.name, module=self.module,
                       address=self.address, fields=dict(self.fields), preset=self.preset)


def new_key() -> str:
    """An opaque key for a module or an account: 12 lower-case hex digits,
    which `steve_sdk.ids` accepts as an id's account part."""
    return secrets.token_hex(6)


def read_builtins(folder: Path | None = None) -> dict[str, Manifest]:
    """Every built-in module's manifest under `folder`, by id. A manifest
    that does not parse is an error here, not a module quietly missing."""
    root = folder or BUILTIN_DIR
    found: dict[str, Manifest] = {}
    if not root.is_dir():
        return found
    for manifest_path in sorted(root.glob("*/manifest.toml")):
        manifest = load(manifest_path)
        if manifest.id != manifest_path.parent.name:
            raise ManifestError(f"{manifest_path}: id {manifest.id!r} is not its folder's name")
        found[manifest.id] = manifest
    return found


@cache
def builtins() -> dict[str, Manifest]:
    return read_builtins()


def known(conn: sqlite3.Connection) -> dict[str, Manifest]:
    """Every module's manifest, by id: the built-ins', and each module from
    outside's as its current version has it. One whose files are gone, or
    are not the ones the user approved, is left out, and so is off."""
    found = dict(builtins())
    rows = conn.execute(
        "SELECT m.id, m.version, v.hash FROM modules m LEFT JOIN module_versions v "
        "ON v.module = m.id AND v.version = m.version WHERE m.origin != 'builtin'").fetchall()
    for module_id, version, approved in rows:
        try:
            folder = paths.module_code_dir(module_id, version)
            seen = _seen(folder)
        except (OSError, ValueError):
            continue
        if approved and (manifest := _approved(str(folder), approved, seen)) is not None:
            found[module_id] = manifest
    return found


def _seen(folder: Path) -> tuple[tuple[str, int, int], ...]:
    """Each file under `folder`, with its time and size."""
    found = []
    for path in folder.rglob("*"):
        if path.is_file():
            stat = path.stat()
            found.append((path.relative_to(folder).as_posix(), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(found))


@lru_cache(maxsize=64)
def _approved(folder: str, approved: str, _seen: tuple) -> Manifest | None:
    # A module's manifest holds its words for Steve's instructions, its
    # routines and its tools' descriptions, read on every turn, long after
    # `lifecycle.check_start` hashed its files where its code starts.
    # Parsed again whenever its time or size changed, with no hash, a
    # manifest edited on disk after approval fed the next turn its new
    # words, with no card. So it is parsed only as the files approved: the
    # manifest first, then the tree, so that an edit in between is caught
    # by the hash. Keyed by every file's time and size (and by the hash, as
    # a module replaced by another of the same id and version has the same
    # folder), a file changed since is hashed again; one put back to its
    # time and size finds the words approved, parsed before.
    root = Path(folder)
    try:
        manifest = load(root / "manifest.toml")
        if addons.tree_hash(root) != approved:
            log.warning("%s: its files are not the ones approved, so it is left out", folder)
            return None
        return manifest
    except (OSError, ManifestError):
        return None


def register(conn: sqlite3.Connection, manifests: dict[str, Manifest] | None = None) -> None:
    """Give every built-in a registry row, once, on by default; keep each
    row's version current. A row whose built-in is gone stays, switched off
    by having no manifest (`modules` says so)."""
    manifests = builtins() if manifests is None else manifests
    with db.transaction(conn):
        for manifest in manifests.values():
            conn.execute(
                "INSERT INTO modules (id, key, version, tier, origin) VALUES (?, ?, ?, ?, "
                "'builtin') ON CONFLICT(id) DO UPDATE SET version = excluded.version",
                (manifest.id, new_key(), manifest.version, manifest.tier))


def modules(conn: sqlite3.Connection, manifests: dict[str, Manifest] | None = None
            ) -> list[Module]:
    manifests = known(conn) if manifests is None else manifests
    rows = conn.execute("SELECT id, key, version, tier, origin, enabled FROM modules "
                        "ORDER BY id").fetchall()
    return [Module(id=r[0], key=r[1], version=r[2], tier=r[3], origin=r[4],
                   enabled=bool(r[5]) and r[0] in manifests, manifest=manifests.get(r[0]))
            for r in rows]


def module(conn: sqlite3.Connection, module_id: str,
           manifests: dict[str, Manifest] | None = None) -> Module | None:
    return next((m for m in modules(conn, manifests) if m.id == module_id), None)


def set_enabled(conn: sqlite3.Connection, module_id: str, on: bool) -> None:
    """Switch a module on or off; off withdraws its accounts' pending cards,
    in the same transaction. A module from outside turns on only as the
    files approved, and is watched afresh from then (`core/lifecycle.py`)."""
    from core import lifecycle

    if on:
        lifecycle.check_start(conn, module_id)
    with db.transaction(conn):
        changed = conn.execute("UPDATE modules SET enabled = ? WHERE id = ?",
                               (int(on), module_id)).rowcount
        if changed and not on:
            withdraw_cards(conn, keys_of(conn, module_id), WITHDRAWN_OFF)
        if changed and on:
            lifecycle.rewatch(conn, module_id)
    if not on:
        # Off stops its servers, its own and its accounts' (06-modules.md,
        # section 7); a call that needs one is refused while it is off.
        from core.sandbox import servers

        servers.POOL.stop_module(module_id)
    if not changed:
        raise AccountError(f"no module {module_id!r} is installed")


def keys_of(conn: sqlite3.Connection, module_id: str) -> list[str]:
    """Every account key of a module, and its own, which its tools' and
    actions' cards carry (`agent/modtools.py`)."""
    return [r[0] for r in conn.execute(
        "SELECT key FROM accounts WHERE module = ? UNION ALL SELECT key FROM modules "
        "WHERE id = ?", (module_id, module_id)).fetchall()]


# --- what is live --------------------------------------------------------------

_KEY = re.compile(r"[0-9a-f]{12}")


def live_keys(conn: sqlite3.Connection, manifests: dict[str, Manifest] | None = None
              ) -> frozenset[str]:
    """The keys of every live account -- those that are on, of modules that
    are on -- and of every module that is on, whose own cards and attention
    items carry its key (`keys_of`). With accounts' keys alone, a module's
    card was hidden from the cards waiting while the module was on, and
    its items stayed in the judge's queue after it was off."""
    return frozenset(a.key for a in accounts(conn, manifests=manifests)) | {
        m.key for m in modules(conn, manifests) if m.enabled}


def live(conn: sqlite3.Connection, table: str, alias: str | None = None, *,
         keys: frozenset[str] | None = None) -> str:
    """The SQL condition that keeps `table`'s rows (named `alias` in the
    query) whose account is live, and its rows of no account (`TAGGED`).
    The keys are written in as text: each is checked to be one core made."""
    keys = live_keys(conn) if keys is None else keys
    # With no live account, a key no account has, not an empty list: SQLite
    # answers `NULL IN ()` false, not NULL, so every row of no account went
    # too. A stand-in's key always filled the list until step 5's item 10.
    listed = ", ".join(f"'{k}'" for k in sorted(keys) if _KEY.fullmatch(k)) or "''"
    return "(" + TAGGED[table].format(t=alias or table, live=listed) + ")"


def withdraw_cards(conn: sqlite3.Connection, keys: list[str], reason: str) -> list[int]:
    """Withdraw the pending cards acting on these accounts, or asked for by
    the routine of the module whose key is among them, saying why: a yes
    given later would act on what was true before. The ids withdrawn."""
    if not keys:
        return []
    marks = ",".join("?" * len(keys))
    # A routine's card for one of core's tools names no account of its
    # module's, only the module that asked (migration 12).
    which = f"status = 'pending' AND (account IN ({marks}) OR routine_module IN ({marks}))"
    ids_ = [r[0] for r in conn.execute(f"SELECT id FROM actions WHERE {which}",
                                       (*keys, *keys)).fetchall()]
    # `decided_at` is the local time, as `agent/approvals.decide` writes it.
    conn.execute(f"UPDATE actions SET status = 'withdrawn', withdrawn_reason = ?, "
                 f"decided_at = ? WHERE {which}",
                 (reason, datetime.now().isoformat(timespec="seconds"), *keys, *keys))
    return ids_


def waiting(conn: sqlite3.Connection) -> dict[str, int]:
    """How many pending cards act on each account: what switching it off or
    removing it would withdraw, which the app says before either."""
    return dict(conn.execute("SELECT account, COUNT(*) FROM actions WHERE status = 'pending' "
                             "AND account IS NOT NULL GROUP BY account").fetchall())


# --- accounts ----------------------------------------------------------------

def _row(conn: sqlite3.Connection, r: tuple) -> AccountRow:
    links = conn.execute("SELECT class, address FROM account_classes WHERE account = ? "
                         "ORDER BY rowid", (r[0],)).fetchall()
    classes = tuple(dict.fromkeys(c for c, _ in links))
    addresses = tuple(dict.fromkeys(a for _, a in links if a))
    return AccountRow(key=r[0], module=r[1], name=r[2], preset=r[3], enabled=bool(r[4]),
                      classes=classes, addresses=addresses,
                      fields=json.loads(r[5] or "{}"), settings=json.loads(r[6] or "{}"))


_SELECT = ("SELECT key, module, name, preset, enabled, fields_json, settings_json "
           "FROM accounts")


def accounts(conn: sqlite3.Connection, *, klass: str | None = None,
             live_only: bool = True, manifests: dict[str, Manifest] | None = None
             ) -> list[AccountRow]:
    """The accounts, by name; with `live_only`, only those switched on whose
    module is installed and on. With `klass`, only those in that class."""
    on = {m.id for m in modules(conn, manifests) if m.enabled}
    rows = [_row(conn, r) for r in conn.execute(_SELECT + " ORDER BY name").fetchall()]
    if klass is not None:
        rows = [a for a in rows if klass in a.classes]
    if live_only:
        rows = [a for a in rows if a.enabled and a.module in on]
    return rows


def account(conn: sqlite3.Connection, key: str) -> AccountRow | None:
    r = conn.execute(_SELECT + " WHERE key = ?", (key,)).fetchone()
    return _row(conn, r) if r else None


def holder(conn: sqlite3.Connection, klass: str, address: str) -> AccountRow | None:
    """The account that already has `address` in `klass`, if any."""
    r = _held(conn, klass, address)
    return account(conn, r[0]) if r else None


def _held(conn: sqlite3.Connection, klass: str, address: str) -> tuple[str, str] | None:
    """The account holding `address`'s mailbox in `klass`, and the address
    as it holds it. One mailbox however it is written
    (`core/mailbox.canonical`): Gmail delivers maya.lindqvist@gmail.com
    and mayalindqvist@googlemail.com to one, which compared case-blind
    alone could be added twice, every message then shown twice."""
    from core.mailbox import canonical

    wanted = canonical(address)
    for key, held in conn.execute("SELECT account, address FROM account_classes "
                                  "WHERE class = ? AND address IS NOT NULL", (klass,)):
        if canonical(held) == wanted:
            return key, held
    return None


# What `_remove` says of an account it removed: its module's id and key, and
# its own key.
Removed = tuple[str, str, str]


def add_account(conn: sqlite3.Connection, *, module_id: str, name: str,
                classes: list[str], addresses: list[str], preset: str | None = None,
                fields: dict[str, Any] | None = None, settings: dict[str, Any] | None = None,
                take: bool = False, manifests: dict[str, Manifest] | None = None) -> str:
    """Add an account; its key. Refused, saying why, if the module is not
    installed, a class is not the module's, the name is taken, or an address
    is already held in a class by another account. With `take`, such a
    class is moved here instead, in the same transaction (`move_class`)."""
    found = module(conn, module_id, manifests)
    if found is None or found.manifest is None:
        raise AccountError(f"no module {module_id!r} is installed")
    if bad := [c for c in classes if c not in found.manifest.classes]:
        raise AccountError(f"{found.manifest.label} does not do {', '.join(bad)}")
    if not classes:
        raise AccountError("an account needs at least one of the module's classes")
    name = name.strip()
    if not name:
        raise AccountError("an account needs a name")
    clean = [a.strip() for a in addresses if a and a.strip()]
    if not take:
        for klass in classes:
            for address in clean:
                if (other := holder(conn, klass, address)) is not None:
                    raise Held(klass, address, other.name)
    key = new_key()
    emptied: list[Removed] = []
    try:
        with db.transaction(conn):
            conn.execute("INSERT INTO accounts (key, module, name, preset, fields_json, "
                         "settings_json) VALUES (?, ?, ?, ?, ?, ?)",
                         (key, module_id, name, preset, json.dumps(fields or {}),
                          json.dumps(settings or {})))
            for klass in classes:
                for address in clean or [None]:
                    if address is not None and take:
                        emptied += _take(conn, klass, address, key)
                    else:
                        conn.execute("INSERT INTO account_classes (account, class, address) "
                                     "VALUES (?, ?, ?)", (key, klass, address))
    except sqlite3.IntegrityError as exc:
        if "accounts.name" in str(exc):
            raise AccountError(f"an account is already named \"{name}\"") from None
        raise AccountError(f"refused: {exc}") from None
    _forget_outside(emptied)
    return key


def rename_account(conn: sqlite3.Connection, key: str, name: str) -> None:
    """Give an account another name. Its key, which every id it handed out
    carries, stays: a rename breaks nothing (section 1)."""
    name = name.strip()
    if not name:
        raise AccountError("an account needs a name")
    try:
        with db.transaction(conn):
            if not conn.execute("UPDATE accounts SET name = ? WHERE key = ?",
                                (name, key)).rowcount:
                raise AccountError("no such account")
    except sqlite3.IntegrityError:
        raise AccountError(f"an account is already named \"{name}\"") from None


EVERY_MIN = (1, 30)  # as a manifest's every_min (05-connectors.md, section 2)


def defaults(manifest: Manifest, classes: list[str]) -> dict[str, Any]:
    """A new account's settings: watched, at the interval its manifest gives
    its mail, or else the shortest of its classes'."""
    specs = [manifest.specs[c] for c in classes if c in manifest.specs]
    mail = manifest.specs.get("mail") if "mail" in classes else None
    every = mail.every_min if mail else min((s.every_min for s in specs), default=15)
    return {"watched": True, "every_min": every}


def set_account_settings(conn: sqlite3.Connection, key: str,
                         changes: dict[str, Any]) -> dict[str, Any]:
    """Change some of an account's settings; all of them, after. Refused,
    saying why, for a setting there is not or a value out of its bounds.
    One account at most is the default calendar."""
    row = account(conn, key)
    if row is None:
        raise AccountError("no such account")
    settings = dict(row.settings)
    for name, value in changes.items():
        if name in ("watched", "default_calendar"):
            if not isinstance(value, bool):
                raise AccountError(f"{name} is on or off")
            if name == "default_calendar" and value and "calendar" not in row.classes:
                raise AccountError(f"\"{row.name}\" has no calendar")
        elif name == "every_min":
            low, high = EVERY_MIN
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise AccountError(f"how often to look is whole minutes, from {low} to {high}")
        else:
            raise AccountError(f"an account has no setting {name!r}")
        settings[name] = value
    with db.transaction(conn):
        if settings.get("default_calendar"):
            for other in conn.execute("SELECT key, settings_json FROM accounts WHERE key != ?",
                                      (key,)).fetchall():
                kept = json.loads(other[1] or "{}")
                if kept.pop("default_calendar", None) is not None:
                    conn.execute("UPDATE accounts SET settings_json = ? WHERE key = ?",
                                 (json.dumps(kept), other[0]))
        conn.execute("UPDATE accounts SET settings_json = ? WHERE key = ?",
                     (json.dumps(settings), key))
    return settings


def move_class(conn: sqlite3.Connection, *, klass: str, address: str, to_key: str,
               manifests: dict[str, Manifest] | None = None) -> None:
    """Move `address`'s `klass` to the account `to_key` from the account
    that holds it: one address has one account per class, and a class can
    be moved (section 3). The account it leaves has its pending cards
    withdrawn, and goes if it is left with nothing."""
    target = account(conn, to_key)
    if target is None:
        raise AccountError("no such account")
    found = module(conn, target.module, manifests)
    if found is None or found.manifest is None or klass not in found.manifest.classes:
        label = found.manifest.label if found and found.manifest else target.module
        raise AccountError(f"{label} does not do {klass}")
    if holder(conn, klass, address) is None:
        raise AccountError(f"no account has {address}'s {klass}")
    with db.transaction(conn):
        emptied = _take(conn, klass, address.strip(), to_key)
    _forget_outside(emptied)


def add_addresses(conn: sqlite3.Connection, key: str, addresses: list[str]) -> list[str]:
    """Link `addresses` to the account `key` in each of its classes; those
    it did not hold. Refused, linking none, if another account holds one in
    a class: classic Outlook merges every account in its profile, so one
    of them connected again elsewhere would show each message twice."""
    row = account(conn, key)
    if row is None:
        raise AccountError("no such account")
    from core.mailbox import canonical

    new = [a for a in dict.fromkeys(a.strip().lower() for a in addresses if a and "@" in a)
           if canonical(a) not in {canonical(h) for h in row.addresses}]
    for klass in row.classes:
        for address in new:
            if (other := holder(conn, klass, address)) is not None and other.key != key:
                raise AccountError(f"{address}'s {klass} is already connected as "
                                   f"\"{other.name}\"; move it here instead")
    with db.transaction(conn):
        for klass in row.classes:
            for address in new:
                _take(conn, klass, address, key)
    return new


def _take(conn: sqlite3.Connection, klass: str, address: str, to_key: str
          ) -> list[Removed]:
    """Inside a transaction: link `klass` at `address` to `to_key`, taking
    it from its holder. What `_remove` says of a holder left with nothing,
    removed here, whose secrets and state go once the transaction is in."""
    r = _held(conn, klass, address)
    if r is not None and r[0] == to_key:
        return []
    emptied = []
    if r is not None:
        conn.execute("DELETE FROM account_classes WHERE account = ? AND class = ? AND "
                     "address = ?", (r[0], klass, r[1]))
        withdraw_cards(conn, [r[0]], WITHDRAWN_MOVED.format(klass=klass))
        if not conn.execute("SELECT 1 FROM account_classes WHERE account = ?",
                            (r[0],)).fetchone():
            emptied.append(_remove(conn, r[0], forget=False))
    conn.execute("DELETE FROM account_classes WHERE account = ? AND class = ? AND "
                 "address IS NULL", (to_key, klass))
    conn.execute("INSERT INTO account_classes (account, class, address) VALUES (?, ?, ?)",
                 (to_key, klass, address))
    return [e for e in emptied if e is not None]


def set_account_enabled(conn: sqlite3.Connection, key: str, on: bool) -> None:
    """Switch an account on or off; off withdraws its pending cards."""
    with db.transaction(conn):
        if not conn.execute("UPDATE accounts SET enabled = ? WHERE key = ?",
                            (int(on), key)).rowcount:
            raise AccountError("no such account")
        if not on:
            withdraw_cards(conn, [key], WITHDRAWN_OFF)


# What goes with a removed account (section 1), by the column naming it: the
# work queued from its items, its cursors and its handles. What Steve learned
# from its items stays, detached from it (`DETACHED`), unless the user chose
# "Remove, and forget what Steve learned" (decided, 13 October). Its cards
# are withdrawn and kept as history. The agent's own mailbox keeps its rows,
# so a message once taken as an instruction is never taken again (item 13).
QUEUED = {"observations": "key", "sync_cursors": "source", "newsletters_said": "key",
          "handles": "full"}
DETACHED = {"interactions": "source_id", "obligations": "source_id"}
KEPT = {"actions": "withdrawn, as the card's history", "mailbox": "the instructions taken",
        "people": "through their interactions"}


def _clear(conn: sqlite3.Connection, keys: list[str], *, forget: bool) -> None:
    """Inside a transaction: what `QUEUED` and `DETACHED` say, for `keys`."""
    marks = ",".join("?" * len(keys))
    for table, column in QUEUED.items():
        conn.execute(f"DELETE FROM {table} WHERE account_in({column}) IN ({marks})", keys)
    if forget:
        # A person known only from these accounts' items goes with them; one
        # also met elsewhere, or with no such interaction, stays.
        people = [r[0] for r in conn.execute(
            f"SELECT DISTINCT person_id FROM interactions WHERE account_in(source_id) "
            f"IN ({marks})", keys).fetchall()]
        conn.execute(f"DELETE FROM interactions WHERE account_in(source_id) IN ({marks})",
                     keys)
        for person in people:
            conn.execute("DELETE FROM people WHERE id = ? AND NOT EXISTS (SELECT 1 FROM "
                         "interactions WHERE person_id = ?)", (person, person))
    else:
        # Not an id any more, so `live` keeps it whatever is switched off;
        # unique still, by the row's own id.
        conn.execute(f"UPDATE interactions SET source_id = 'detached:' || id "
                     f"WHERE account_in(source_id) IN ({marks})", keys)
    conn.execute(f"UPDATE obligations SET source_id = NULL WHERE account_in(source_id) "
                 f"IN ({marks})", keys)
    conn.execute(f"UPDATE obligations SET task_id = NULL WHERE account_in(task_id) "
                 f"IN ({marks})", keys)


def _remove(conn: sqlite3.Connection, key: str, *, forget: bool) -> Removed | None:
    """Inside a transaction: remove one account and what goes with it."""
    r = conn.execute("SELECT m.id, m.key FROM accounts a JOIN modules m ON m.id = a.module "
                     "WHERE a.key = ?", (key,)).fetchone()
    if r is None:
        return None
    withdraw_cards(conn, [key], WITHDRAWN_REMOVED)
    _clear(conn, [key], forget=forget)
    conn.execute("DELETE FROM account_classes WHERE account = ?", (key,))
    conn.execute("DELETE FROM accounts WHERE key = ?", (key,))
    health.forget(conn, key)
    return r[0], r[1], key


def _forget_outside(removed: list[Removed]) -> None:
    """After the transaction: the removed accounts' secrets, and their
    state (`account_state_dir`). Neither is in the database, so neither
    could roll back with it; done after, a failure leaves a secret no row
    names, which removing the module clears."""
    for module_id, module_key, key in removed:
        credstore.forget(credstore.prefix(module_key, key))
        shutil.rmtree(account_state_dir(module_id, key), ignore_errors=True)
        _forget_server(module_id, key)


def _forget_server(module_id: str, key: str) -> None:
    """The account's local MCP server: stopped, then its home, where it
    keeps its sign-in, and its container (`core/sandbox/servers.forget`).
    The container is named for the account, so the module's folder going
    does not take it."""
    from core.sandbox import servers

    try:
        servers.forget(module_id, key)
    except OSError:
        log.warning("could not delete %s's server for %s", module_id, key, exc_info=True)


def account_state_dir(module_id: str, key: str) -> Path:
    """What a module stores for one account (`ModuleContext.state_dir`):
    its own folder, so removing the account removes exactly it."""
    if not _KEY.fullmatch(key):
        raise ValueError(f"not an account key: {key!r}")
    return paths.module_dir(module_id) / "state" / key


def remove_account(conn: sqlite3.Connection, key: str, *, forget: bool = False) -> None:
    """Remove an account: its pending cards withdrawn, its queued work and
    cursors deleted, what Steve learned from it kept detached unless
    `forget`, then its secrets and state."""
    with db.transaction(conn):
        removed = _remove(conn, key, forget=forget)
        if removed is None:
            raise AccountError("no such account")
    _forget_outside([removed])


def remove_module(conn: sqlite3.Connection, module_id: str, *, forget: bool = False
                  ) -> list[str]:
    """Remove a module's registry row, its versions, its accounts and the
    items its accounts and its own attention found, in one transaction,
    then its secrets, its folder and, for one from outside, its sandbox
    profile; the account keys removed."""
    from core import lifecycle

    row = conn.execute("SELECT key, origin FROM modules WHERE id = ?", (module_id,)).fetchone()
    with db.transaction(conn):
        keys = [r[0] for r in conn.execute("SELECT key FROM accounts WHERE module = ?",
                                           (module_id,)).fetchall()]
        held = keys_of(conn, module_id)
        withdraw_cards(conn, held, WITHDRAWN_REMOVED)
        if held:
            # Its own key with its accounts': what its attention found is
            # kept under it (`module:<key>:...`), and was left behind when
            # only its accounts' items were cleared.
            _clear(conn, held, forget=forget)
        for key in keys:
            conn.execute("DELETE FROM account_classes WHERE account = ?", (key,))
            health.forget(conn, key)
        conn.execute("DELETE FROM accounts WHERE module = ?", (module_id,))
        conn.execute("DELETE FROM module_versions WHERE module = ?", (module_id,))
        conn.execute("DELETE FROM module_watch WHERE module = ?", (module_id,))
        conn.execute("DELETE FROM modules WHERE id = ?", (module_id,))
    if row is not None:
        credstore.forget(credstore.prefix(row[0]))
        from core.sandbox import servers

        for key in [*keys, servers.MODULE_KEY]:
            _forget_server(module_id, key)
        # A server's installed tree nests past 260 characters, which a
        # plain rmtree cannot reach and, ignoring errors, left behind.
        servers.remove(paths.module_dir(module_id))
        if row[1] != "builtin":
            # Named for the id alone: left behind, it and whatever was
            # granted to it would be a later module's of the same id.
            lifecycle.drop_container(module_id)
    return keys

