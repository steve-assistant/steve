"""Adding an account from its manifest, changing it, and checking its health.

Each provider had its own way in: a command to run for Google, keys typed
into the keychain for a password mailbox and a course site's feed, a switch for Outlook. A user had
to know which, and Steve could not add a second account of any. Now setup is
drawn from the module's manifest, one fixed list of steps the app shows the
same way for every module (05-connectors.md, section 3), and this is the
backend of it:

- `pick`: the address first. Its domain picks the module, from the
  manifests' and their presets' `domains`; any other domain asks one
  question. Nothing is looked up over the network.
- `add`: the fields typed and checked, secrets into the Credential Manager
  (`core/credstore.py`) and nothing secret on disk, the classes, and the
  defaults: named by its address, watched, at its manifest's interval.
- `sign_in`: a built-in's browser sign-in, for the account's classes, kept
  only if the address that signed in is the account's; then its test.
- `test`: connect and read once, show what was found, keep the addresses
  only the account knows (classic Outlook's profile), and make the first
  calendar Steve can write to the default.
- `edit`, `move` and `remove`, each through `core/modules.py`.
- `check_all` and `tell`: every account checked the same way, into one of
  `core/health.py`'s five states, and said once on the user's channel.

Only the app calls these, through `agent/worker/accounts.py`, on the local
control API: no model tool adds, edits or removes an account or a secret
(tests/gate/test_only_the_app.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import sqlite3
import threading
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from agent import connectors, modconnect, offloop
from core import clock, credstore, egress, health, lifecycle, modctx, modules, paths
from core.call_policy import within_quiet_hours
from core.engines import code
from core.mailbox import canonical
from core.modctx import LOOPBACK_ENGINES
from steve_sdk import classes
from steve_sdk.errors import ConnectorError, Unsupported
from steve_sdk.manifest import FORMS, SIGN_IN, Field, Manifest, ManifestError, parse

log = logging.getLogger("accounts")

AccountError = modules.AccountError

EMAIL = re.compile(r"[^@\s]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}", re.IGNORECASE)

# Asked when no manifest claims an address's domain (section 3): an account
# in classic Outlook on this computer, or any other mail server.
QUESTION = "Is it an account in classic Outlook on this computer?"
ANSWERS = {"yes": ("outlook_classic", None), "no": ("imap", "other")}

MAX_SECRET = 16 * 1024  # a password, a token, a link: far less
MAX_FILE = 64 * 1024  # Google's client file is half a kilobyte
MAX_TEXT = 300
# A server's name as a user may type it: dotted or not ("nas", "localhost").
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
TYPED_HOST = re.compile(rf"{_LABEL}(\.{_LABEL})*")

# How long setup's test and the health check wait on one account.
TEST_LIMIT_S = 30.0

# How often every account is checked: a quarter hour, so a lapsed sign-in
# is known well before the six hours after which it is said.
CHECK_EVERY_S = 15 * 60


def _manifests(manifests: dict[str, Manifest] | None) -> dict[str, Manifest]:
    return modules.builtins() if manifests is None else manifests


def pick(address: str, manifests: dict[str, Manifest] | None = None, *,
         installed: dict[str, Manifest] | None = None) -> dict[str, Any]:
    """Which module, and which preset, an address's domain picks; or the one
    question, with the module each answer picks. The modules installed from
    outside (`installed`, `modules.known`) are asked after the built-ins, so
    one that names a built-in's domain cannot take its addresses: a module
    that claimed gmail.com would otherwise be handed the user's Gmail
    password at setup."""
    if not EMAIL.fullmatch(address.strip()):
        raise AccountError(f"{address!r} is not an email address")
    domain = address.strip().rsplit("@", 1)[1].lower()
    manifests = _manifests(manifests)
    outside = {k: m for k, m in (installed or {}).items() if k not in manifests}
    for module_id in [*sorted(manifests), *sorted(outside)]:
        manifest = manifests.get(module_id) or outside[module_id]
        for preset in manifest.setup.presets:
            if domain in preset.domains:
                return {"module": module_id, "preset": preset.id}
        if domain in manifest.domains:
            return {"module": module_id, "preset": None}

    def answer(choice: tuple[str, str | None]) -> dict[str, Any]:
        module_id, preset_id = choice
        found = manifests.get(module_id)
        return {"module": module_id,
                "preset": preset_id if found and found.preset(preset_id) else None}

    return {"question": QUESTION, **{a: answer(c) for a, c in ANSWERS.items()}}


def _value(f: Field, value: Any) -> Any:
    """One field's value, checked against its type; raises saying which."""
    label = f.label or f.name
    if f.type == "flag":
        if not isinstance(value, bool):
            raise AccountError(f"{label} is on or off")
        return value
    if not isinstance(value, str):
        raise AccountError(f"{label} is text")
    text = value if f.secret else value.strip()
    if f.form:
        # An app password is shown in groups with spaces between, and pasted
        # that way; the spaces are not part of it.
        text = "".join(text.split())
        if not FORMS[f.form].fullmatch(text):
            said = "an https:// or webcal:// link" if f.form == "link" else f.form
            raise AccountError(f"{label} should be {said}")
    if f.type != "file" and ("\r" in text or "\n" in text):
        # Each is one line where it is used: a user name or password in a
        # mail server's command, a link in a request. A line break would end
        # that line and start another, or stand in a password as no
        # character the user typed (one pasted with its line's end).
        raise AccountError(f"{label} is one line, with no line break in it")
    limit = MAX_FILE if f.type == "file" else MAX_SECRET if f.secret else MAX_TEXT
    if len(text) > limit:
        raise AccountError(f"{label} is too long")
    if f.type == "address" and not EMAIL.fullmatch(text):
        raise AccountError(f"{label} is not an email address")
    if f.type == "host" and _endpoint(text) is None:
        raise AccountError(f"{label} is not a server's name")
    if f.type == "link":
        parts = urlsplit(text)
        if parts.scheme != "https" or not parts.hostname:
            raise AccountError(f"{label} is not an https:// link")
    if f.type == "choice" and text not in f.choices:
        raise AccountError(f"{label} is one of {', '.join(f.choices)}")
    return text.lower() if f.type == "host" else text


def _endpoint(text: str) -> tuple[str, int | None] | None:
    """A server the user typed, as host and port (None if not given), or
    None if it is not one. Unlike a manifest's hosts, it may be a bare
    address or a name on their own network: a mail bridge on this computer,
    a server at home (05-connectors.md, section 4)."""
    host, sep, port = text.strip().lower().rpartition(":")
    if not sep or "]" in port:
        host, port = text.strip().lower(), ""
    if port and not (port.isdigit() and 0 < int(port) < 65536):
        return None
    host = host.strip("[]")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not TYPED_HOST.fullmatch(host):
            return None
    return host, int(port) if port else None


def _pinned(manifest: Manifest, given: dict[str, Any], resolve) -> dict[str, str]:
    """The kind of address (`core/egress.py`) each typed server resolves to
    now, for `ctx.tcp` to hold it to: a public name that later points at
    this computer is refused rather than followed. Of a secret link, only
    the host is kept: the rest of it is the secret."""
    engines = {spec.engine for spec in manifest.specs.values()}
    pins: dict[str, str] = {}
    for f in manifest.setup.fields:
        if not (f.type in ("host", "link") or f.form == "link") or f.name not in given:
            continue
        if f.type == "link" or f.form == "link":
            # A calendar's address, or its private link: its server is reached
            # as a typed host is.
            parts = urlsplit(given[f.name])
            host, port = (parts.hostname or "").lower(), parts.port
        else:
            host, port = _endpoint(given[f.name]) or ("", None)
        kind = egress.pin_kind(host, port or 443, **({"resolve": resolve} if resolve else {}))
        if kind is None:
            raise AccountError(f"{f.label or f.name}: {host} could not be found, or it "
                               f"points both inside your network and outside it")
        if kind == egress.LOOPBACK and not engines & LOOPBACK_ENGINES:
            raise AccountError(f"{f.label or f.name}: {host} is this computer, which "
                               f"{manifest.label} may not reach")
        pins[host] = kind
    return pins


def fields(manifest: Manifest, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The setup fields given, checked: those kept with the account, and
    the secrets, kept in the Credential Manager only. A built-in with code
    keeps some its own way first: Google's client file as its id, secret
    and project (`core/engines/code.fields`)."""
    try:
        values = code.fields(manifest, values)
    except ValueError as exc:
        raise AccountError(str(exc)) from None
    known = {f.name: f for f in manifest.setup.fields}
    if unknown := sorted(set(values) - set(known)):
        raise AccountError(f"{manifest.label} asks for no {', '.join(unknown)}")
    kept: dict[str, Any] = {}
    secret: dict[str, str] = {}
    for f in manifest.setup.fields:
        value = values.get(f.name)
        if value is None or value == "":
            if not f.optional:
                raise AccountError(f"{f.label or f.name} is needed")
            continue
        checked = _value(f, value)
        if f.secret:
            secret[f.name] = checked
        else:
            kept[f.name] = checked
    return kept, secret


def add(conn: sqlite3.Connection, *, module_id: str, address: str = "",
        wanted: list[str] | None = None, preset: str | None = None,
        values: dict[str, Any] | None = None, name: str | None = None, take: bool = False,
        manifests: dict[str, Manifest] | None = None, resolve=None) -> str:
    """Add an account of `module_id` from setup's answers; its key. Its
    secrets go to the Credential Manager, or the account is not added. A
    server the user typed is pinned to the kind of address it resolves to
    now; `resolve` is the lookup's seam for tests."""
    # The registry's manifest, so a module installed from outside is found.
    held = modules.module(conn, module_id, manifests)
    manifest = held.manifest if held is not None else None
    if manifest is None or held is None:
        raise AccountError(f"no module {module_id!r} is installed")
    if not held.enabled:
        raise AccountError(f"{manifest.label} is switched off")
    address = address.strip()
    if "address" in manifest.setup.steps and not EMAIL.fullmatch(address):
        raise AccountError(f"{manifest.label} needs the account's email address")
    if preset is not None and manifest.preset(preset) is None:
        raise AccountError(f"{manifest.label} has no {preset!r}")
    kept, secret = fields(manifest, values or {})
    # Unless the user chose, every class the preset offers, else the module's,
    # and any a field answered joins (a calendar's address, in "Other").
    offered = manifest.preset(preset).classes if preset is not None else ()
    chosen = list(wanted) if wanted is not None else list(offered or manifest.classes)
    if wanted is None:
        chosen += [f.joins for f in manifest.setup.fields
                   if f.joins and f.name in kept and f.joins not in chosen]
    if missing := [c for c in chosen if c in manifest.specs
                   and not _buildable(manifest, manifest.specs[c].engine)]:
        raise AccountError(f"{manifest.label} cannot add its {', '.join(missing)} yet "
                           f"in this version of Steve")
    # A server typed where the module or its preset already names one would
    # be ignored by the engine, yet pinned and reachable: refused instead.
    fixed = {k for klass in manifest.classes for k in manifest.settings(klass, preset)}
    if typed := [f.label or f.name for f in manifest.setup.fields
                 if f.type in ("host", "link") and f.name in kept and f.name in fixed]:
        raise AccountError(f"{', '.join(typed)}: {manifest.label} already knows its servers")
    if pins := _pinned(manifest, {**kept, **secret}, resolve):
        kept["hosts"] = pins
    # Named by its address unless the user chose. A second account at one
    # address (Drive beside a Gmail account, or a class moved here from the
    # account that held it) is named with its module too, since names are
    # one each; once a move has emptied the holder, the address is free again.
    def names() -> set[str]:
        return {row.name for row in modules.accounts(conn, live_only=False, manifests=manifests)}

    plain = name or address or manifest.label
    given = plain
    if not name:
        taken, n = names(), 1
        while given in taken:
            given = f"{plain} ({manifest.label}{f' {n}' if n > 1 else ''})"
            n += 1
    key = modules.add_account(
        conn, module_id=module_id, name=given, classes=chosen,
        addresses=[address] if address else [], preset=preset, fields=kept,
        settings=modules.defaults(manifest, chosen), take=take, manifests=manifests)
    if given != plain and plain not in names():
        modules.rename_account(conn, key, plain)
    try:
        for secret_name, value in secret.items():
            credstore.write(credstore.target(held.key, key, secret_name), value)
    except Exception:
        modules.remove_account(conn, key)
        raise
    return key


def _buildable(manifest: Manifest, engine: str) -> bool:
    """Whether this version of Steve builds a class on `engine`: an engine
    it has, and for code, a built-in's own or a sandbox module's."""
    return engine in connectors.ENGINES and (engine != "code" or modconnect.buildable(manifest))


def needs_sign_in(manifest: Manifest) -> bool:
    """Whether setup signs the account in, in the browser, before its test."""
    return "browser_sign_in" in manifest.setup.steps


# The accounts whose browser sign-in is open: one at a time each, or two
# would race to keep their token.
_SIGNING: set[str] = set()


def _to_sign_in(conn: sqlite3.Connection, key: str,
                manifests: dict[str, Manifest] | None) -> tuple[Any, modules.AccountRow]:
    row = modules.account(conn, key)
    if row is None:
        raise AccountError("no such account")
    held = modules.module(conn, row.module, manifests)
    if held is None or held.manifest is None or not needs_sign_in(held.manifest):
        raise AccountError(f"{row.name} has no browser sign-in")
    return connectors.context(held, row), row


def _keep_sign_in(conn: sqlite3.Connection, key: str, secret: str,
                  manifests: dict[str, Manifest] | None) -> None:
    row = modules.account(conn, key)
    held = modules.module(conn, row.module, manifests) if row else None
    if row is None or held is None:
        raise AccountError("the account was removed during its sign-in")
    credstore.write(credstore.target(held.key, key, SIGN_IN), secret)


async def sign_in(conn: sqlite3.Connection, key: str, *,
                  manifests: dict[str, Manifest] | None = None) -> dict[str, Any]:
    """Setup's browser sign-in, then its test. The module's sign-in asks
    for the account's classes alone, with its address as the hint, and waits
    for the user in a thread; what it returns is kept only if the address
    that signed in is the account's. A browser signed in to another account,
    the user's own or someone else's, would otherwise put that mailbox under
    this account's name. A sign-in that does not finish raises its
    ConnectorError, with the page that fixes it."""
    if key in _SIGNING:
        raise AccountError("its sign-in is already open in the browser")
    _SIGNING.add(key)
    try:
        ctx, row = await offloop.with_own_conn(conn, _to_sign_in, key, manifests)
        signed = await asyncio.to_thread(code.sign_in, ctx, classes=row.classes)
        # One mailbox however it is written: Google says the address as it
        # was made, dots and all, which the user may have typed without.
        said = canonical(signed.address)
        if not said or said != canonical(row.address):
            who = signed.address.strip() or "an address that is not verified"
            raise AccountError(f"that signed in as {who}: sign in again as {row.address}")
        await offloop.with_own_conn(conn, _keep_sign_in, key, signed.secret, manifests)
    finally:
        _SIGNING.discard(key)
    return await test(conn, key, manifests=manifests)


SERVER_SIGN_IN_S = 60.0  # how long a server Steve runs has to show its code


def server_package(manifest: Manifest, preset: str | None) -> Any:
    """The package of the MCP server Steve runs for an account of this
    module with this preset (a class's `runs = "steve"`), or None. Only a
    module Steve ships, which alone has the backend tier, names one: the
    engine refuses the rest too (`core/engines/mcp.py` `runs`)."""
    from core.engines import mcp

    if manifest.tier != "backend":
        return None
    for klass, spec in manifest.specs.items():
        if spec.engine == "mcp" and manifest.settings(klass, preset).get("runs") == "steve":
            return next((p.package for p in mcp.profiles(manifest, klass)
                         if p.package is not None), None)
    return None


def next_step(manifest: Manifest, preset: str | None) -> str | None:
    """The sign-in an account takes after it is added, before its test,
    which could only fail without it: the browser's, or its server's."""
    if needs_sign_in(manifest):
        return "browser_sign_in"
    if "server_sign_in" in manifest.setup.steps and server_package(manifest, preset):
        return "server_sign_in"
    return None


def _to_server_sign_in(conn: sqlite3.Connection, key: str,
                       manifests: dict[str, Manifest] | None) -> tuple[Any, Any]:
    row = modules.account(conn, key)
    if row is None:
        raise AccountError("no such account")
    held = modules.module(conn, row.module, manifests)
    package = (server_package(held.manifest, row.preset)
               if held is not None and held.manifest is not None else None)
    if package is None or package.sign_in is None:
        raise AccountError(f"{row.name} has no server for Steve to sign in")
    return connectors.context(held, row), package


def _server_code(ctx: Any, package: Any, wait_s: float) -> tuple[str, str]:
    """Install the server if it is not, start it, and wait for the page and
    code it shows, from its first line on stderr or from its sign-in
    tool's answer. Only the profile's page, and a code of its pattern."""
    from core.sandbox import servers

    sign_in = package.sign_in
    shown: list[tuple[str, str]] = []
    seen = threading.Event()

    def look(text: str) -> None:
        if not seen.is_set() and (found := servers.prompt(text, sign_in)) is not None:
            shown.append(found)
            seen.set()

    module_id = ctx.manifest.id
    try:
        if not servers.installed(module_id, package):
            servers.install(module_id, package, network=connectors.NETWORK,
                            redact=modctx.scrub)
        client = ctx.server(package, watch=look)
        try:
            if sign_in.tool:
                result = client.call(sign_in.tool, {})
                look(" ".join(str(c.get("text", "")) for c in result.get("content") or []
                              if isinstance(c, dict)))
            seen.wait(wait_s)
        finally:
            if (running := servers.POOL.running(module_id, ctx.account.key)) is not None:
                running.log.unwatch(look)
    except (servers.NotHeld, servers.NotInstalled) as exc:
        raise AccountError(str(exc)) from exc
    finally:
        ctx.close()
    if not shown:
        raise AccountError("the server showed no sign-in code")
    return shown[0]


async def server_sign_in(conn: sqlite3.Connection, key: str, *,
                         manifests: dict[str, Manifest] | None = None,
                         wait_s: float = SERVER_SIGN_IN_S) -> dict[str, Any]:
    """Setup's `server_sign_in`: the server Steve runs for the account,
    installed if it is not and started in its container, asked to sign in.
    The page and code it shows are returned for the app to show large; the
    user enters the code in their own browser while the server waits, and
    the test that follows checks the address that signed in (the engine's
    `_signed_in_as_the_account`)."""
    if key in _SIGNING:
        raise AccountError("its sign-in is already under way")
    _SIGNING.add(key)
    try:
        ctx, package = await offloop.with_own_conn(conn, _to_server_sign_in, key, manifests)
        page, shown = await asyncio.to_thread(_server_code, ctx, package, wait_s)
    finally:
        _SIGNING.discard(key)
    return {"page": page, "code": shown, "expires_s": package.sign_in.expires_s}


def _connected(conn: sqlite3.Connection, key: str,
               manifests: dict[str, Manifest] | None = None) -> list[connectors.Connected]:
    """The account's providers, one per class, if it is live."""
    found = []
    for klass in classes.INTERFACES:
        found += [c for c in connectors.accounts(conn, klass, manifests=manifests)
                  if c.key == key]
    return found


@dataclass
class Checked:
    """What an account's test found: each class's sentence, the first
    failure, the earliest known end of a sign-in, and the addresses only
    the account knew (`Provider.addresses`)."""

    said: list[str] = field(default_factory=list)
    error: ConnectorError | None = None
    ends: datetime | None = None
    addresses: list[str] = field(default_factory=list)


async def _check(found: list[connectors.Connected]) -> Checked:
    """Each class's test, in turn, stopping at the first failure."""
    checked, ends = Checked(), []
    for account in found:
        try:
            checked.said.append(await connectors.call(account, lambda p: p.test(),
                                                      limit_s=TEST_LIMIT_S))
            if (end := await connectors.call(account, lambda p: p.expires(),
                                             limit_s=TEST_LIMIT_S)) is not None:
                ends.append(end)
            checked.addresses += await connectors.call(account, lambda p: p.addresses(),
                                                       limit_s=TEST_LIMIT_S)
        except Unsupported:
            continue  # nothing to test it by; its other classes still are
        except ConnectorError as exc:
            checked.error = exc
            return checked
        except Exception as exc:  # noqa: BLE001 - a provider's fault is its account's
            log.exception("testing %s failed", account.name)
            checked.error = ConnectorError(f"{type(exc).__name__}: {exc}"[:300])
            return checked
    checked.ends = min(ends) if ends else None
    return checked


def _found(conn: sqlite3.Connection, key: str, manifests: dict[str, Manifest] | None
           ) -> tuple[modules.AccountRow, list[connectors.Connected]]:
    row = modules.account(conn, key)
    if row is None:
        raise AccountError("no such account")
    return row, _connected(conn, key, manifests)


def _record(conn: sqlite3.Connection, key: str, checked: Checked) -> health.Health:
    """The account's health after its test, its addresses kept first: one
    another account already holds fails it, saying which (`core/modules.py`,
    `add_addresses`), since both would show the same mail."""
    error = checked.error
    if error is None:
        try:
            modules.add_addresses(conn, key, checked.addresses)
        except AccountError as exc:
            error = ConnectorError(str(exc))
    return health.record(conn, key, kind=error.kind if error else None,
                         detail=error.detail if error else "", expires=checked.ends,
                         fix=error.fix if error else "")


def _after(conn: sqlite3.Connection, key: str, checked: Checked,
           found: list[connectors.Connected]) -> health.Health:
    state = _record(conn, key, checked)
    if state.state == health.WORKING:
        _default_calendar(conn, found)
    return state


async def test(conn: sqlite3.Connection, key: str, *,
               manifests: dict[str, Manifest] | None = None) -> dict[str, Any]:
    """Setup's test, and the health check's: connect and read once in each
    class; what was found, and the account's health after: its sentence,
    the provider's own words, and the page that fixes it, if one does. The
    registry and the accounts are read in a thread (`agent/offloop.py`):
    the keychain and Outlook block."""
    row, found = await offloop.with_own_conn(conn, _found, key, manifests)
    checked = Checked()
    if not found:
        state = await offloop.with_own_conn(conn, health.record, key, state=health.OFF)
    else:
        checked = await _check(found)
        state = await offloop.with_own_conn(conn, _after, key, checked, found)
    return {"ok": bool(found) and state.state == health.WORKING,
            "found": checked.said, "health": state.says(row.name),
            "state": state.state, "detail": state.detail, "fix": state.fix}


def _default_calendar(conn: sqlite3.Connection, found: list[connectors.Connected]) -> None:
    """The first calendar Steve can write to becomes the default, once
    tested; the user can choose another later (section 3)."""
    if any(a.settings.get("default_calendar") for a in modules.accounts(conn, live_only=False)):
        return
    for account in found:
        if account.klass == "calendar" and account.provider.has(classes.CREATE):
            modules.set_account_settings(conn, account.key, {"default_calendar": True})
            return


def renew(conn: sqlite3.Connection, key: str, values: dict[str, Any]) -> None:
    """New secrets for an account, such as a password changed at its
    provider: health's "enter its new password". Only the account's secret
    fields, each checked as setup checks it, and none kept unless all pass;
    a setting goes through `edit`'s settings, never through here."""
    row = modules.account(conn, key)
    held = modules.module(conn, row.module) if row else None
    manifest = held.manifest if held else None
    if row is None or held is None or manifest is None:
        raise AccountError("no such account")
    try:
        values = code.fields(manifest, values)
    except ValueError as exc:
        raise AccountError(str(exc)) from None
    secrets = {f.name: f for f in manifest.setup.fields if f.secret}
    if not values:
        raise AccountError("say what to change")
    if other := sorted(set(values) - set(secrets)):
        raise AccountError(f"{manifest.label} keeps no secret called {', '.join(other)}")
    checked = {}
    for name, value in values.items():
        if value is None or value == "":
            raise AccountError(f"{secrets[name].label or name} is needed")
        checked[name] = _value(secrets[name], value)
    for name, value in checked.items():
        credstore.write(credstore.target(held.key, key, name), value)


def edit(conn: sqlite3.Connection, key: str, *, name: str | None = None,
         settings: dict[str, Any] | None = None, enabled: bool | None = None,
         values: dict[str, Any] | None = None) -> None:
    if modules.account(conn, key) is None:
        raise AccountError("no such account")
    if values:
        renew(conn, key, values)
    if name is not None:
        modules.rename_account(conn, key, name)
    if settings:
        modules.set_account_settings(conn, key, settings)
    if enabled is not None:
        modules.set_account_enabled(conn, key, enabled)
        if not enabled:
            health.record(conn, key, state=health.OFF)


def move(conn: sqlite3.Connection, *, klass: str, address: str, to_key: str) -> None:
    modules.move_class(conn, klass=klass, address=address, to_key=to_key)


def remove(conn: sqlite3.Connection, key: str, *, forget: bool = False) -> None:
    modules.remove_account(conn, key, forget=forget)


# --- health ---------------------------------------------------------------------

def _to_check(conn: sqlite3.Connection) -> tuple[list[str], dict[str, list[connectors.Connected]]]:
    """Every account's key, and the live ones' connections."""
    live: dict[str, list[connectors.Connected]] = {}
    for klass in classes.INTERFACES:
        for found in connectors.accounts(conn, klass):
            live.setdefault(found.key, []).append(found)
    return [row.key for row in modules.accounts(conn, live_only=False)], live


async def check_all(conn: sqlite3.Connection) -> dict[str, health.Health]:
    """Check every account the user added, the same way: off if it or its
    module is switched off, else its test."""
    keys, live = await offloop.with_own_conn(conn, _to_check)
    checked = {}
    for key in keys:
        if key not in live:
            checked[key] = await offloop.with_own_conn(conn, health.record, key,
                                                       state=health.OFF)
            continue
        checked[key] = await offloop.with_own_conn(conn, _record, key,
                                                   await _check(live[key]))
    return checked


def _due(conn: sqlite3.Connection, now: datetime) -> list[tuple[str, str, str]]:
    names = {a.key: a.name for a in modules.accounts(conn, live_only=False)}
    return [(key, *due) for key, found in health.every(conn).items()
            if key in names and (due := health.due(found, names[key], now)) is not None]


async def tell(conn: sqlite3.Connection, notify: Callable[[str], Awaitable[bool]], *,
               now: datetime | None = None) -> list[str]:
    """Say on the user's channel, once each, what `health.due` says is due:
    never at night, and again next time if it was not delivered."""
    now = now or datetime.now(UTC)
    if within_quiet_hours(now.astimezone(clock.local_zone())):
        return []
    sent = []
    for key, mark, text in await offloop.with_own_conn(conn, _due, now):
        if await notify(text):
            await offloop.with_own_conn(conn, health.told, key, mark)
            sent.append(text)
    return sent


def unchecked(conn: sqlite3.Connection) -> list[str]:
    """A sentence for each account Steve could not check, for the turn and
    the briefing: silence is never "nothing new" (section 2)."""
    names = {a.key: a.name for a in modules.accounts(conn)}
    return [found.says(names[key]) for key, found in sorted(health.every(conn).items())
            if key in names and found.state not in (health.WORKING, health.OFF)]


# The slots a module fills (06-modules.md, section 1), in the plan's order.
_SLOTS = (("connector", lambda m: bool(m.classes)), ("tool", lambda m: bool(m.tools)),
          ("MCP server", lambda m: m.mcp is not None),
          ("attention source", lambda m: m.attention is not None),
          ("routine", lambda m: bool(m.routines)), ("skill", lambda m: bool(m.skills)),
          ("prompt section", lambda m: bool(m.prompt)))


def _reach(held: modules.Module) -> dict[str, Any]:
    """What a module may reach and do, for its row on the Extensions page: the
    slots it fills, the hosts it may reach (`typed`: and one the user
    gives it), and core's reading of its grants, in its card's words. Once
    approved, its row showed only its version, tier and classes, so what
    the user had said yes to could not be seen again."""
    from core.engines import mcp as mcp_engine

    manifest = held.manifest
    assert manifest is not None
    tools: tuple[dict[str, Any], ...] = ()
    # Unreadable, its row says so (`mcptools.view`).
    with contextlib.suppress(ConnectorError):
        if manifest.mcp is not None:
            tools = tuple(mcp_engine.pinned(paths.module_code_dir(held.id, held.version)))
    said = lifecycle.summary(manifest, tools)
    # The card's last line asks the passphrase for the yes, given already.
    lines = said.lines[:-1] if said.passphrase else said.lines
    return {"slots": [name for name, fills in _SLOTS if fills(manifest)],
            "hosts": list(said.hosts), "typed": said.typed, "grants": list(lines)}


def listing(conn: sqlite3.Connection, *, manifests: dict[str, Manifest] | None = None
            ) -> dict[str, Any]:
    """What the app's Accounts page and Add account show: each module, its
    setup without any value, and each account with its health, the one
    action beside it, the sign-in it takes, and the cards switching it off
    or removing it would withdraw, which the app says before either. For
    the Extensions page, each module's slots, hosts and grants (`_reach`), and
    each
    module's origin, its kept versions with the date rolling back to each
    returns its data to, its watch's count, and the cards waiting for the
    user's approval (`core/lifecycle.py`)."""
    from agent import mcptools

    states = health.every(conn)
    waiting = modules.waiting(conn)
    out_modules = []
    by_id: dict[str, Manifest] = {}
    for held in modules.modules(conn, manifests):
        manifest = held.manifest
        if manifest is None:
            continue
        by_id[held.id] = manifest
        setup = manifest.setup
        out_modules.append({
            "id": held.id, "label": manifest.label, "for": manifest.for_,
            "adds": manifest.adds, "effort": manifest.effort, "tier": held.tier,
            "origin": held.origin, "version": held.version,
            "versions": lifecycle.versions(conn, held.id) if held.origin != "builtin" else [],
            "watch": (counted.as_dict() if (counted := lifecycle.counts(conn, held.id))
                      else None),
            "enabled": held.enabled, "classes": list(manifest.classes),
            "domains": list(manifest.domains),
            "available": all(_buildable(manifest, s.engine) for s in manifest.specs.values()),
            "server": mcptools.view(held),
            **_reach(held),
            "setup": {
                "steps": list(setup.steps),
                "fields": [{"name": f.name, "type": f.type, "label": f.label,
                            "optional": f.optional, "choices": list(f.choices),
                            "advanced": f.advanced}
                           for f in setup.fields],
                "presets": [{"id": p.id, "label": p.label, "domains": list(p.domains),
                             "classes": list(p.classes), "revoke": p.revoke}
                            for p in setup.presets],
                "guide": [{"text": g.text, "link": g.link} for g in setup.guide],
                # Where a secret given here is taken back: shown beside its
                # field, which until step 11 only Gmail's guide did.
                "revoke": setup.revoke,
            },
        })
    out_accounts = []
    for row in modules.accounts(conn, live_only=False, manifests=manifests):
        found = states.get(row.key)
        out_accounts.append({
            "key": row.key, "name": row.name, "module": row.module, "preset": row.preset,
            "enabled": row.enabled, "classes": list(row.classes),
            "addresses": list(row.addresses), "settings": dict(row.settings),
            "state": found.state if found else None,
            "health": found.says(row.name) if found else None,
            "fix": found.fix if found else "",
            "action": (health.action(health.OFF) if not row.enabled
                       else found.act() if found else health.action(None)),
            "waiting": waiting.get(row.key, 0),
            # Which sign-in "sign in again" means: the browser's, or the
            # server's Steve runs. The card knew only the first, so an
            # account whose server's sign-in lapsed was refused there.
            "sign_in": (next_step(by_id[row.module], row.preset)
                        if row.module in by_id else None),
        })
    return {"modules": out_modules, "accounts": out_accounts,
            "admissions": [_shown_card(conn, card) for card in lifecycle.waiting(conn)]}


def _installed_server(conn: sqlite3.Connection, card: dict[str, Any]) -> Manifest | None:
    """The installed module a server's card is the next version of, or None
    for a new one, one that replaces another, or one of another origin."""
    if card.get("kind") != "version":
        return None
    held = modules.module(conn, card["id"])
    if held is None or held.manifest is None or held.manifest.mcp is None:
        return None
    return held.manifest if held.origin == card.get("origin") else None


def _secret_kept(conn: sqlite3.Connection, card: dict[str, Any]) -> bool:
    """Whether the secret kept for the installed version goes on to this
    card's: the same secret, bound to the same host, port, header and form.
    The app asked for it again at every change of the server's tools,
    though it was kept already; where its binding changed, it is asked,
    or the secret kept would go wherever the new version binds it."""
    name = (card.get("server") or {}).get("secret")
    installed = _installed_server(conn, card)
    if not name or installed is None or installed.mcp.secret != name:
        return False
    try:
        staged = parse(tomllib.loads(card["details"]["manifest"]))
    except (KeyError, tomllib.TOMLDecodeError, ManifestError):
        return False

    def bound(manifest: Manifest) -> Any:
        return next((f.bind for f in manifest.setup.fields if f.name == name), None)

    return bound(installed) is not None and bound(installed) == bound(staged)


def _shown_card(conn: sqlite3.Connection, card: dict[str, Any]) -> dict[str, Any]:
    """A card as the app shows it: for a server's next version, whether its
    secret is kept (`secret_kept`), and each tool still freed as it was
    when freed (`free`), so approving the change keeps both. The app began
    from no tool freed, so every tool the user had freed was a card again
    after any change of the server's tools. A tool that changed is not
    marked: freeing it was a yes to it as it was."""
    from agent import mcptools

    if card.get("server") is None:
        return card
    installed = _installed_server(conn, card)
    had = mcptools.freed(card["id"]) if installed is not None else {}
    printed = card["grants"].get("server_tools", {})
    tools = [{**t, "free": had.get(t["name"]) is not None
              and had.get(t["name"]) == printed.get(t["name"])}
             for t in card["server"]["tools"]]
    return {**card, "server": {**card["server"], "tools": tools,
                               "secret_kept": _secret_kept(conn, card)}}


def approve_server(conn: sqlite3.Connection, admission: int, *, given: str = "",
                   signed: tuple[str, str] | None = None,
                   secrets: dict[str, str] | None = None, free: list[str] | None = None
                   ) -> str:
    """The user's yes to an MCP server's card (`agent/mcptools.py`), with
    the secret they typed and the tools they freed: both checked against
    the card before it is approved, then kept under the install's key.
    Here, not beside the server's tools, because only the app's backend
    writes a secret or approves a card (`tests/gate/test_only_the_app.py`).
    A server Steve runs is installed here if it is not. Blocking."""
    from agent.mcptools import FREE_FILE, SECRETS, install
    from core.sandbox import servers

    card = next((c for c in lifecycle.waiting(conn) if c["admission"] == admission), None)
    server = card.get("server") if card is not None else None
    if server is None:
        raise lifecycle.Refused("only an MCP server's card takes a secret or tools to free")
    secrets = dict(secrets or {})
    if bad := [n for n in secrets if n != server["secret"] or not server["secret"]]:
        raise lifecycle.Refused(f"the server takes no secret named {bad[0]!r}")
    for value in secrets.values():
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
            raise lifecycle.Refused("a secret is one line of text")
    if server["secret"] and not secrets and not _secret_kept(conn, card):
        raise lifecycle.Refused("the server's secret is needed: type it in")
    printed = card["grants"].get("server_tools", {})
    if free is not None and (bad := [t for t in free if t not in printed]):
        raise lifecycle.Refused(f"the server has no tool {bad[0]!r}")
    module_id = lifecycle.approve(conn, admission, given=given, signed=signed)
    held = modules.module(conn, module_id)
    assert held is not None
    for name, value in secrets.items():
        credstore.write(credstore.target(held.key, SECRETS, name), value)
    if free is not None:
        (paths.module_dir(module_id) / FREE_FILE).write_text(
            json.dumps({t: printed[t] for t in free}, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n")
    if held.manifest is not None and held.manifest.mcp is not None and held.manifest.mcp.local:
        # A server module from a zip comes without its installed files.
        # Approved, it stays approved if npm fails now: its first call
        # installs it then (`mcptools._started`).
        try:
            install(held.manifest)
        except (servers.NotHeld, servers.NotInstalled, OSError) as exc:
            log.warning("%s's server did not install on approval: %s", module_id, exc)
    return module_id
