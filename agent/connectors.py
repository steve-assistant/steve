"""Each class's accounts, and calling them: one by name or by an item's id,
or every one at once.

Each tool of a class named its providers. `search_email` built a searcher
for Gmail, one for Outlook and one for the password mailbox; `read_email` told them
apart by the shape of an id (Gmail's short hex, the `outlook:` and
the password mailbox's tags); the calendar merged Google with Outlook by name. A
provider a module adds would have needed every tool taught its name, and two
accounts of one kind could not exist at all: both would have been "gmail".

Here a tool asks for a class and gets its accounts (`accounts`), each with
its key, its name and its provider (`steve_sdk.classes`). An item's id
carries its account's key (`steve_sdk.ids`), so a later call goes to the
account that handed the id out, and to no other (`by_id`). A call to every
account (`ask_all`) asks them at once:

- each with its own time limit, so a slow server is named in the answer
  rather than holding up the rest. A thread that has timed out cannot be
  stopped, so each engine sets its own socket timeouts; this only stops
  waiting. A write is never given a limit: "too slow" said of a send that
  then goes is worse than waiting for it;
- one call at a time within an account whose provider says its client cannot
  be shared between threads (`Provider.shareable`): two Gmail calls at once
  on Google's httplib2 client can corrupt each other's replies
  (`agent/toolset/pace.py`). The turn is taken in the call's own thread, and
  a call that has not had its turn by its deadline is never started.

A provider raises the SDK's errors (`steve_sdk.errors`); `say` words each
kind once, naming the account, for the tools, the app and chat alike.

Every account is one the user added, built by its module's engine
(`core/engines/`), or by its own code through the `code` engine. Until step
5's items 6, 9 and 10, the password mailbox, Google and Outlook stood in as
accounts no one added, with fixed keys, each asked whenever its own check
said it was there; Outlook's check started Outlook.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent import modconnect
from core import engines as core_engines
from core import health, logscrub, modules
from steve_sdk import ids
from steve_sdk.classes import CONDITIONAL, INTERFACES, Provider
from steve_sdk.errors import ConnectorError, Transient, Unsupported
from steve_sdk.manifest import ClassSpec, Manifest

log = logging.getLogger("worker")

# How long `ask_all` waits for one account's answer. Outlook's search walks
# its local folders through COM, the slowest of today's, in well under this.
TIME_LIMIT_S = 60.0


@dataclass(frozen=True)
class Connected:
    """One account in one class, ready to call."""

    key: str
    name: str
    klass: str
    module: str
    provider: Provider
    address: str = ""

    def id(self, native: str) -> str:
        """An id for one of this account's items, which `by_id` routes back."""
        return ids.make(self.klass, self.key, native)


@dataclass
class Answer:
    """One account's part of `ask_all`: what it returned, or why not."""

    account: Connected
    result: Any = None
    error: ConnectorError | None = None


class NoAccount(LookupError):
    """No account answers to that name or id; the message says which there are."""


# --- the accounts ------------------------------------------------------------

# An engine builds a class's provider for an account of a module built on it
# (`core/engines/`), from the module's settings for that class. A copy, so a
# test that adds one here leaves core's list as it was.
Engine = Callable[[Any, ClassSpec], Provider]
ENGINES: dict[str, Engine] = dict(core_engines.ENGINES)
# A sandbox module's code runs there, through a stand-in (agent/modconnect.py).
ENGINES["code"] = modconnect.engine
# How an engine's sockets reach the world: None is the real network
# (`core/modctx.Network`). A test hands in one that routes to its fakes.
NETWORK: Any = None


def accounts(conn: sqlite3.Connection, klass: str, *,
             manifests: dict[str, Manifest] | None = None) -> list[Connected]:
    """Every account in `klass` that is on, of modules that are on, by name."""
    if klass not in INTERFACES:
        raise ValueError(f"not a connector class: {klass!r}")
    return [c for row in modules.accounts(conn, klass=klass, manifests=manifests)
            if (c := _connected(conn, row, klass, manifests)) is not None]


def context(held: modules.Module, row: modules.AccountRow) -> Any:
    """The context an account's providers are built from (`core/modctx.py`)."""
    from core import modctx

    assert held.manifest is not None
    return modctx.ModuleContext(manifest=held.manifest, module_key=held.key,
                                account=row.as_account(), network=NETWORK,
                                state_dir=modules.account_state_dir(held.id, row.key))


def _connected(conn: sqlite3.Connection, row: modules.AccountRow, klass: str,
               manifests: dict[str, Manifest] | None) -> Connected | None:
    held = modules.module(conn, row.module, manifests)
    spec = held.manifest.specs.get(klass) if held and held.manifest else None
    engine = ENGINES.get(spec.engine) if spec else None
    if held is None or held.manifest is None or spec is None or engine is None:
        # Not a state the app can make: each engine arrives with its item (6
        # to 11), and setup refuses a module whose engine is not here yet.
        log.warning("account %s: no provider for %s (%s)", row.name, klass,
                    spec.engine if spec else "no such class")
        return None
    return Connected(key=row.key, name=row.name, klass=klass, module=row.module,
                     provider=engine(context(held, row), spec), address=row.address)


# --- choosing ------------------------------------------------------------------

def named(conn: sqlite3.Connection, klass: str, name: str | None, **kw) -> list[Connected]:
    """The accounts a tool's `account=` means: every one for "all" or none
    given, else the one by that name. Raises NoAccount, naming those there are."""
    found = accounts(conn, klass, **kw)
    wanted = (name or "").strip().lower()
    if wanted in ("", "all"):
        return found
    chosen = [c for c in found if c.name.lower() == wanted]
    if not chosen:
        there = ", ".join(c.name for c in found) or "none"
        raise NoAccount(f"no {klass} account is named {name!r}; there are: {there}")
    return chosen


def by_id(conn: sqlite3.Connection, item_id: str, **kw) -> tuple[Connected, str]:
    """The account an id was handed out by, and the provider's own id inside it."""
    parsed = ids.parse(item_id)
    if parsed is None:
        raise NoAccount("that is not an id Steve handed out; search again for the item")
    for found in accounts(conn, parsed.klass, **kw):
        if found.key == parsed.account:
            return found, parsed.native
    raise NoAccount(f"the {parsed.klass} account that id came from is not connected now")


def slot_of(item_id: str) -> str | None:
    """The "<class>:<key>" an id was handed out under, or None for anything
    else. Cards are grouped by it, and consent reads it."""
    parsed = ids.parse(item_id) if isinstance(item_id, str) else None
    return None if parsed is None else f"{parsed.klass}:{parsed.account}"


def conditional(conn: sqlite3.Connection, **kw) -> frozenset[str]:
    """The "<class>:<key>" of every account that makes its updates and
    deletes conditional on the item being as it was read. Consent asks
    first for the others (`core/consent.py`, `unconditional`)."""
    return frozenset(f"{klass}:{found.key}" for klass in INTERFACES
                     for found in accounts(conn, klass, **kw)
                     if found.provider.has(CONDITIONAL))


def addresses(conn: sqlite3.Connection, **kw) -> frozenset[str]:
    """The user's own addresses at every mail account, lower-cased: mail to
    them is a note, not correspondence (consent's `self`), a file they own
    is not news, and the judge can tell an invitation from their own event.
    Each is an account's, kept when it was added or tested: classic
    Outlook's, every account in its profile (`accounts.test`). Until step
    5's item 10 Outlook was asked here, over COM, at the first call.

    One account switched off is not theirs to Steve: its address drops out,
    and mail to it asks first, as it did before the account was added."""
    return frozenset(a.strip().lower() for row in modules.accounts(conn, klass="mail", **kw)
                     for a in row.addresses if "@" in a)


# What a capability is, in a sentence: "<name> cannot <words>".
_CAN = {
    "labels": "label mail", "threads": "read a whole conversation",
    "flags": "mark mail read or unread", "move": "move mail between folders",
    "archive": "archive mail", "send": "send a draft", "create": "add events",
    "update": "change events", "delete": "delete events", "guests": "invite guests",
    "freebusy": "answer free/busy itself", "documents": "make documents",
    "spreadsheets": "make spreadsheets", "presentations": "make presentations",
    "conditional": "make a change conditional on the item being unchanged",
}


def require(found: Connected, capability: str) -> None:
    """Refuse, naming the account, a capability its provider did not declare,
    before anything is called."""
    if not found.provider.has(capability):
        raise Unsupported(f"{found.name} cannot {_CAN.get(capability, capability)}")


# --- calling ---------------------------------------------------------------------

_turns: dict[tuple[str, str], threading.Lock] = {}
_turns_lock = threading.Lock()


def _turn(found: Connected) -> threading.Lock:
    with _turns_lock:
        return _turns.setdefault((found.key, found.klass), threading.Lock())


def _run(found: Connected, fn: Callable[[Provider], Any], deadline: float | None,
         given_up: threading.Event) -> Any:
    """`fn(provider)`, in this thread, in the account's turn where it needs
    one, unless the caller has stopped waiting by the time it gets it."""
    lock = None if found.provider.shareable else _turn(found)
    if lock is not None:
        wait = -1 if deadline is None else max(0.0, deadline - time.monotonic())
        if not lock.acquire(timeout=wait):
            raise Transient(f"{found.name} was busy with another call")
    try:
        if given_up.is_set():
            raise Transient("given up before its turn came")
        return fn(found.provider)
    finally:
        if lock is not None:
            lock.release()


def blocking(found: Connected, fn: Callable[[Provider], Any], *,
             limit_s: float | None = TIME_LIMIT_S) -> Any:
    """`fn(provider)` on one account, in this thread, in its turn: for code
    already off the event loop, such as a read-back. With `limit_s`, a
    Transient if the turn has not come by then; the call itself is not cut."""
    deadline = None if limit_s is None else time.monotonic() + limit_s
    return _run(found, fn, deadline, threading.Event())


async def call(found: Connected, fn: Callable[[Provider], Any], *,
               limit_s: float | None = None) -> Any:
    """`fn(provider)` on one account, in a thread. With `limit_s`, a
    Transient once it has waited that long."""
    deadline = None if limit_s is None else time.monotonic() + limit_s
    given_up = threading.Event()
    running = asyncio.to_thread(_run, found, fn, deadline, given_up)
    if limit_s is None:
        return await running
    try:
        return await asyncio.wait_for(running, limit_s)
    except TimeoutError:
        given_up.set()
        raise Transient(f"no answer in {limit_s:g} seconds") from None


async def ask_all(found: list[Connected], fn: Callable[[Provider], Any], *,
                  limit_s: float = TIME_LIMIT_S) -> list[Answer]:
    """`fn(provider)` on every account at once, in their order; an account
    that fails or is too slow is named in its answer, and the rest answer."""
    async def one(account: Connected) -> Answer:
        try:
            return Answer(account, await call(account, fn, limit_s=limit_s))
        except ConnectorError as exc:
            return Answer(account, error=exc)
        except Exception as exc:  # noqa: BLE001 - one provider's fault is its account's
            log.exception("%s %s failed", account.klass, account.name)
            # The error's text can hold what the call was given; it goes on
            # to the model, and so to its provider.
            said = logscrub.scrub(f"{type(exc).__name__}: {exc}")
            return Answer(account, error=ConnectorError(said[:300]))

    return list(await asyncio.gather(*(one(a) for a in found)))


# --- saying it -------------------------------------------------------------------

# What one call's failure is said as. An error that is the account's state
# (signed out, unreachable, not here) is said as its health is instead.
_SAID = {
    "unsupported": "{name} cannot do that",
    "target_changed": ("what was named in {name} changed or went since it was read, "
                       "so nothing was written"),
    "needs_card": "changing that in {name} needs the user's yes first",
    # The write went: it is not to be asked for again (`AfterWrite`).
    "after_write": ("the change went to {name}, but the check after it did not find just "
                    "that change, so it is not to be made again"),
    "error": "{name} failed",
}


def say(error: ConnectorError, name: str) -> str:
    """One sentence for an account's error, the same wherever it is said.

    An error that is the account's state is said by `core/health.py`, so a
    tool's error, the app and chat name the same one action: before item 5
    a tool said "needs the user to sign in again" while nothing said where.
    """
    detail = (error.detail or "").strip()
    if error.kind in health.KINDS:
        said = health.sentence(name, health.state_of(error.kind), kind=error.kind,
                               detail=detail)
        return said if not detail or detail in said else f"{said[:-1]} ({detail})."
    said = _SAID.get(error.kind, _SAID["error"]).format(name=name)
    return f"{said}: {detail}" if detail and detail not in said else f"{said}."
