"""A remote MCP server's tools: offered to the model, reached on the
broker's fetch path, and asked about where a steered model could misuse
them (06-modules.md, section 7).

A server the user adds is code that runs elsewhere, which Steve reaches
with a secret and whose words the model reads. Trusted as it is, it would
be trusted three ways it should not be:

- **Its own way out.** Core's client speaking HTTP for itself would go
  wherever the address resolved, with the secret on every request. Here
  each request is a line the module's broker parses and answers
  (`core/sandbox/broker.Local`), as a sandboxed module's is: POST to the
  server's one host, over HTTPS unless the server is on this computer, and
  the secret filled into its bound header there alone. This code never
  holds it.
- **Its tools as of today.** A server can change its tools at any time: a
  tool approved as one that lists notes could come back as one that sends
  them. The list the user approved is pinned with the module's version
  (`mcp-tools.json`); the model is offered that list, and each call lists
  the server's tools again first. A tool added or changed stops the call
  and becomes the next version's card, naming only what changed. A list
  that only lost tools goes on as it is.
- **A steered model's call.** A mail or a page can steer the model into a
  call the user never wanted. A tool the server labels a write is a card on
  every call. One it labels a read is asked once in a conversation the
  user is in, and then runs there without asking for half an hour. With no
  one watching, every call is a card. At approval the user may free tools,
  which then skip the card in their own conversations alone.

What a tool returns is the server's text, screened before the model reads
it, and each call counts for the module's watch, as a sandboxed module's
does. Not here: a server that lies, which can do harm without the model's
help, and which no card can stop (security model, 2.8).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets as tokens
import shutil
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic_ai import Agent, FunctionToolset, RunContext, Tool

from agent import actions, modadmit, offloop, screening
from agent.actions import executable
from agent.modtools import listing
from agent.turn import Deps
from core import credstore, egress, lifecycle, modctx, modules, paths, screen
from core.engines import mcp
from core.modctx import Hosts, Network
from core.sandbox import broker, run, servers
from steve_sdk.cut import cut
from steve_sdk.errors import ConnectorError
from steve_sdk.manifest import ID, LOOPBACK_HOSTS, Manifest, ManifestError, load

log = logging.getLogger(__name__)

CALL = "mcp.call"
ALLOW = "mcp.allow"
SOURCE = screen.ANSWER
FREE_FILE = "mcp-free.json"  # in the module's folder: the tools the user freed
SECRETS = "module"           # the account part of a server module's own secrets
SECRET_FIELD = "token"       # the secret field a server the user adds is given
ALLOWED_S = 1800             # a read's yes, as `browse_use_login`'s (LOGIN_APPROVAL_S)
MAX_RESULT = 20_000

# The network the broker reaches servers on: the one seam tests replace.
NETWORK: Network = Network()
# The clock a read's half hour is kept on; tests pin it.
clock: Callable[[], float] = time.monotonic

_allowed: dict[tuple[int, str, str, str], float] = {}
_told: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class Server:
    """A server module as installed: its pinned tools, and those the user
    freed, each by the fingerprint it had then."""

    module: str
    key: str
    version: str
    origin: str
    label: str
    manifest: Manifest
    tools: tuple[dict[str, Any], ...]
    free: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Offer:
    """One pinned tool of a server, as the model is offered it."""

    server: Server
    tool: dict[str, Any]
    name: str

    @property
    def tool_name(self) -> str:
        return str(self.tool["name"])

    @property
    def read(self) -> bool:
        return mcp.reads(self.tool)

    @property
    def printed(self) -> str:
        return mcp.fingerprint([self.tool])

    @property
    def freed(self) -> bool:
        return self.server.free.get(self.tool_name) == self.printed


# --- what is installed ---------------------------------------------------------------

def freed(module_id: str) -> dict[str, str]:
    """The tools the user freed, each by the fingerprint it had then."""
    try:
        found = json.loads((paths.module_dir(module_id) / FREE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return ({k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(found, dict) else {})


def _server(held: modules.Module) -> Server:
    """`held`'s server as installed, or ConnectorError if its pin is not
    readable. Blocking."""
    assert held.manifest is not None
    tools = tuple(mcp.pinned(paths.module_code_dir(held.id, held.version)))
    return Server(module=held.id, key=held.key, version=held.version, origin=held.origin,
                  label=held.manifest.label or held.id, manifest=held.manifest, tools=tools,
                  free=freed(held.id))


def _offers_of(server: Server) -> list[Offer]:
    """Its tools whose offered names fit a model's; the rest are shown on
    the card and not offered."""
    return [Offer(server, tool, name) for tool in server.tools
            if (name := mcp.offered_as(server.module, str(tool["name"]))) is not None]


def offers(conn: sqlite3.Connection) -> list[Offer]:
    """The tools of every server module that is on. Blocking."""
    found = []
    for held in modules.modules(conn):
        if not held.enabled or held.manifest is None or held.manifest.mcp is None:
            continue
        try:
            found += _offers_of(_server(held))
        except ConnectorError:
            log.warning("%s's pinned tools could not be read", held.id)
    return found


def _offer(conn: sqlite3.Connection, module_id: str, key: str, version: str, tool: str,
           printed: str) -> Offer:
    """The tool a card named, as its module is now: Refused unless it is
    on, the same install and version with its files as approved, and the
    tool as it was when the user was asked. Blocking."""
    held = run.installed(conn, module_id)
    if (held.key != key or held.version != version or held.manifest is None
            or held.manifest.mcp is None):
        raise lifecycle.Refused(f"{module_id} has changed since you were asked: ask again")
    lifecycle.check_start(conn, module_id)
    try:
        found = next((o for o in _offers_of(_server(held)) if o.tool_name == tool), None)
    except ConnectorError as exc:
        raise lifecycle.Refused(f"{module_id}: {exc}") from None
    if found is None or found.printed != printed:
        raise lifecycle.Refused(f"{module_id} has no tool {tool!r} as it was when you were asked")
    return found


# --- the server, through the broker ----------------------------------------------------

def _grants(manifest: Manifest) -> broker.Grants:
    """POST to the server's one host, and its secret's binding: nothing
    else the manifest names is reached from here."""
    server = manifest.mcp
    assert server is not None
    hosts = (Hosts(typed={server.host: egress.LOOPBACK}) if server.loopback
             else Hosts(fixed=(server.host,)))
    return broker.Grants(
        hosts=hosts, methods=lambda host: ("POST",) if host.lower() == server.host else (),
        bindings={f.name: f.bind for f in manifest.setup.fields if f.bind is not None},
        secrets=manifest.secrets)


class _Pooled:
    """A server Steve runs stays with the pool between calls, which stops
    it when idle; there is nothing to close after one."""

    def close(self) -> None:
        pass


def package_of(manifest: Manifest) -> servers.Package:
    """The npm package of a server module Steve runs, as its manifest
    names it: held to the manifest's hosts on 443, or to any public host."""
    server = manifest.mcp
    assert server is not None and server.local
    name, _, version = server.package.rpartition("@")
    return servers.Package(name=name, version=version, entry=server.entry,
                           hosts={h: (443,) for h in manifest.hosts}, args=server.args,
                           anywhere=server.anywhere, before=server.before, tree=server.tree)


def install(manifest: Manifest) -> None:
    """Install the package of a server module Steve runs, if the tree its
    manifest pins is not installed here. Its files are not in the module's
    zip: they stay in the data folder that installed them, so a server
    module imported from a zip had nothing installed, and every call
    failed as "not installed as approved". npm installs it as the registry
    stood when that version came out, and a tree other than the pinned one
    is refused (`servers.install`). Blocking: npm may take minutes."""
    package = package_of(manifest)
    if not servers.installed(manifest.id, package):
        servers.install(manifest.id, package, network=NETWORK, redact=modctx.scrub)


def _started(manifest: Manifest) -> mcp._Rpc:
    """A server module's server, started in its container on the first call
    and kept for the next. Approval installs its package; where that
    failed (npm unreachable then), the call installs it and starts again."""
    def start() -> mcp._Rpc:
        return servers.POOL.client(manifest.id, servers.MODULE_KEY, package_of(manifest),
                                   network=NETWORK, redact=modctx.scrub)

    try:
        return start()
    except servers.NotInstalled:
        install(manifest)
        return start()


def _client(manifest: Manifest, secret: Callable[[str], str | None]
            ) -> tuple[mcp._Rpc, broker.Local | _Pooled]:
    server = manifest.mcp
    assert server is not None
    if server.local:
        return _started(manifest), _Pooled()
    local = broker.Local(broker.Broker(
        module=manifest.id, grants=_grants(manifest), secret=secret, network=NETWORK,
        redact=modctx.scrub, plain_loopback=server.loopback))
    headers = {}
    if server.secret:
        bind = next(f.bind for f in manifest.setup.fields if f.name == server.secret)
        assert bind is not None
        headers[bind.header] = "{{secret:" + server.secret + "}}"
    return mcp.Client(local, server.url, secret=None, headers=headers), local


def listed(manifest: Manifest, secret: Callable[[str], str | None]) -> list[dict[str, Any]]:
    """The server's tools as it lists them now. Blocking."""
    client, local = _client(manifest, secret)
    try:
        client.open()
        return mcp.kept(client.tools())
    finally:
        local.close()


def _stored(server: Server) -> Callable[[str], str | None]:
    return lambda name: credstore.read(credstore.target(server.key, SECRETS, name))


def _grown(pinned: tuple[dict[str, Any], ...], now: list[dict[str, Any]]) -> list[str]:
    """The tools `now` lists that were not pinned, or not as pinned."""
    had = mcp.prints(list(pinned))
    return [name for name, printed in mcp.prints(now).items() if had.get(name) != printed]


@dataclass
class Ran:
    """How one call went: the server's result or the error the model is
    told, how it ended for the watch, and the tools listed anew where any
    was added or changed (and nothing run)."""

    result: dict[str, Any] | None = None
    error: str | None = None
    ended: str | None = None
    changed: list[dict[str, Any]] | None = None


def _call(offer: Offer, args: dict) -> Ran:
    """List the server's tools, and call the tool if none was added or
    changed. Blocking."""
    ran = Ran()
    client, local = _client(offer.server.manifest, _stored(offer.server))
    try:
        client.open()
        now = mcp.kept(client.tools())
        if _grown(offer.server.tools, now):
            ran.changed = now
            return ran
        ran.result = client.call(offer.tool_name, args)
    except Exception as exc:  # noqa: BLE001 - the server's, told and counted by kind
        ran.error = f"{exc}"[:300] or type(exc).__name__
        ran.ended = lifecycle.outcome(exc)
        return ran
    finally:
        local.close()
    ran.ended = lifecycle.outcome()
    return ran


def _text_of(result: dict[str, Any]) -> str:
    """What a tool returned, as text: its text items, the others named;
    its structured content where it gave no text."""
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif item.get("type") in ("image", "audio"):
            parts.append(f"[{item['type']}, not shown]")
        else:
            parts.append(json.dumps({k: v for k, v in item.items() if k != "data"},
                                    ensure_ascii=False, default=str))
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, default=str))
    return "\n".join(parts)


def _notifier(conn: sqlite3.Connection) -> Callable[[str], Awaitable[Any]]:
    from agent import outbox

    async def notify(text: str) -> Any:
        return await offloop.with_own_conn(conn, outbox.add, text, kind="notice")

    return notify


async def _shown(conn: sqlite3.Connection, offer: Offer, ran: Ran) -> dict:
    """What the model is told of a finished call, as the screen allows it:
    the server's error as much as its answer, since it writes both (a
    JSON-RPC error's message went to the model unscreened: step 11's
    audit)."""
    if ran.error is not None:
        seen = await screening.shown(conn, SOURCE, offer.name, offer.server.module, ran.error)
        told = {"ok": False, "error": f"{offer.server.label}: {seen.text}"}
        return told if seen.how == "text" else {**told, "screened": seen.how}
    result = ran.result or {}
    seen = await screening.shown(conn, SOURCE, offer.name, offer.server.module,
                                 cut(_text_of(result), MAX_RESULT))
    told: dict[str, Any] = {"ok": result.get("isError") is not True, "result": seen.text}
    if seen.how != "text":
        told["screened"] = seen.how
    return told


async def _running(conn: sqlite3.Connection, offer: Offer, args: dict) -> dict:
    """One call, off the event loop: what the model is told."""
    try:
        await offloop.with_own_conn(conn, lifecycle.check_start, offer.server.module)
    except lifecycle.Refused as exc:
        return {"ok": False, "error": f"{offer.server.module}: {exc}"[:300]}
    ran = await asyncio.to_thread(_call, offer, args)
    if ran.changed is not None:
        return await _changed(conn, offer.server, ran.changed)
    await modadmit.watched(conn, offer.server.module, ran.ended, _notifier(conn))
    return await _shown(conn, offer, ran)


# --- a changed list ------------------------------------------------------------------

def _after(version: str, kept: set[str]) -> str:
    """The next version after `version` that is not kept: its last number
    moved on, or `.1` added."""
    while True:
        found = re.fullmatch(r"(.*?)(\d+)", version)
        version = f"{found[1]}{int(found[2]) + 1}" if found else f"{version}.1"
        if version not in kept:
            return version


_VERSION = re.compile(r'^version\s*=\s*"[^"\n]*"[ \t]*$', re.MULTILINE)


def _next_folder(conn: sqlite3.Connection, server: Server, now: list[dict[str, Any]]) -> Path:
    """The server module's next version, in a scratch folder: its files as
    approved, its version moved on, and the tools as the server lists them
    now. Blocking."""
    kept = {r[0] for r in conn.execute("SELECT version FROM module_versions WHERE module = ?",
                                       (server.module,)).fetchall()}
    version = _after(server.version, kept)
    folder = paths.scratch_dir() / f"mcp-{tokens.token_hex(6)}"
    shutil.copytree(paths.module_code_dir(server.module, server.version), folder)
    manifest = folder / "manifest.toml"
    text = manifest.read_text(encoding="utf-8")
    line = f'version = "{version}"'
    text = _VERSION.sub(line, text, count=1) if _VERSION.search(text) else f"{line}\n{text}"
    manifest.write_text(text, encoding="utf-8", newline="\n")
    (folder / mcp.TOOLS_FILE).write_text(json.dumps(now, ensure_ascii=False, indent=1) + "\n",
                                         encoding="utf-8", newline="\n")
    return folder


def _asked(conn: sqlite3.Connection, module_id: str, now: list[dict[str, Any]]) -> bool:
    """Whether a card for exactly these tools is waiting already. Blocking."""
    printed = mcp.prints(now)
    return any(card["id"] == module_id and card["grants"].get("server_tools") == printed
               for card in lifecycle.waiting(conn))


async def _changed(conn: sqlite3.Connection, server: Server, now: list[dict[str, Any]]
                   ) -> dict:
    """The server added or changed a tool: nothing runs until the user
    approves the next version, whose card names what changed. They are
    told once for each new list."""
    key = (server.key, mcp.fingerprint(now))
    if key not in _told and not await offloop.with_own_conn(conn, _asked, server.module, now):
        _told.add(key)
        folder = await offloop.with_own_conn(conn, _next_folder, server, now)
        try:
            await modadmit.admit(conn, folder, origin=server.origin, again=True)
            notice = (f"{server.label}'s MCP server changed its tools. Steve will not use "
                      f"them until you look at the change, on the Extensions page.")
        except lifecycle.Refused as exc:
            notice = (f"{server.label}'s MCP server changed its tools, and the change was "
                      f"refused ({exc}). Steve will not use them.")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        await _notifier(conn)(notice)
    return {"ok": False, "kind": "tools_changed",
            "error": f"{server.label}'s MCP server changed its tools since the user approved "
                     f"them. Nothing was run; the user has been asked about the change."}


# --- in a turn -----------------------------------------------------------------------

def allow(session: int, module_key: str, tool: str, printed: str) -> None:
    """The user's yes to a read in this conversation, for half an hour."""
    _allowed[(session, module_key, tool, printed)] = clock() + ALLOWED_S


def _allowed_now(session: int, offer: Offer) -> bool:
    key = (session, offer.server.key, offer.tool_name, offer.printed)
    return _allowed.get(key, 0.0) > clock()


def _card(offer: Offer, args: dict) -> dict:
    """A card's arguments: the tool as pinned when it was asked, by module,
    install, version and fingerprint, and what the yes runs. `module_key`
    ties the card to the module, so turning it off withdraws the card."""
    return {"module": offer.server.module, "module_key": offer.server.key,
            "version": offer.server.version, "tool": offer.tool_name,
            "printed": offer.printed, "args": args}


def _goes_to(manifest: Manifest) -> str:
    """Where a call goes, in the card's words. A server Steve runs has no
    address (its manifest's url is empty), so naming the url ended the card
    "It goes to ."; what the user weighs is that it runs here and which
    hosts it may pass the arguments on to."""
    server = manifest.mcp
    assert server is not None
    if not server.local:
        return server.url
    package = package_of(manifest)
    reach = "any public host" if package.anywhere else ", ".join(sorted(package.hosts))
    return f"the server Steve runs on this computer, which may reach {reach or 'no host'}"


def call_card(offer: Offer, args: dict) -> str:
    """A call's card, in core's words, every argument shown."""
    label = "labels as a read" if offer.read else "does not label as a read"
    return (f"{offer.server.label} (an MCP server, version {offer.server.version}) would run "
            f"its tool {offer.tool_name}, which the server {label}, with:\n{listing(args)}\n\n"
            f"It goes to {_goes_to(offer.server.manifest)}.")


def allow_card(offer: Offer, args: dict) -> str:
    """A read's card in a conversation the user is in: yes runs it, and
    lets the conversation run it again without asking for half an hour."""
    return (f"{offer.server.label} (an MCP server, version {offer.server.version}) would run "
            f"its tool {offer.tool_name}, which the server labels as a read, with:\n"
            f"{listing(args)}\n\nYes runs it, and lets this conversation use "
            f"{offer.tool_name} without asking for {ALLOWED_S // 60} minutes.")


async def use(ctx: RunContext[Deps], offer: Offer, args: dict) -> dict:
    """One call of a server's tool, as the model made it."""
    deps = ctx.deps
    deps.tools_used.append(offer.name)
    present = actions.present(deps)
    if present and (offer.freed or _allowed_now(deps.session_id, offer)):
        return await _running(deps.conn, offer, args)
    if present and offer.read:
        return await actions.act(ctx, ALLOW, {**_card(offer, args), "session": deps.session_id},
                                 allow_card(offer, args))
    return await actions.act(ctx, CALL, _card(offer, args), call_card(offer, args))


def tool(offer: Offer) -> Tool[Deps]:
    async def call(ctx: RunContext[Deps], **args: Any) -> dict:
        return await use(ctx, offer, args)

    schema = offer.tool.get("inputSchema")
    if not (isinstance(schema, dict) and schema.get("type") == "object"):
        schema = {"type": "object", "properties": {}}
    said = offer.tool.get("description") or offer.tool.get("title") or offer.tool_name
    return Tool.from_schema(call, name=offer.name, description=str(said), json_schema=schema,
                            takes_ctx=True)


def register(agent: Agent[Deps, str], allow_only: set[str] | None = None) -> None:
    """Offer the tools of the server modules that are on, read as each run
    starts, and narrowed by `allow_only` as core's tools are."""

    @agent.toolset(per_run_step=False)
    async def server_tools(ctx: RunContext[Deps]) -> FunctionToolset[Deps]:
        conn = getattr(ctx.deps, "conn", None)
        if conn is None:  # a run with no database has no modules
            return FunctionToolset([])
        try:
            found = await offloop.with_own_conn(conn, offers)
        except sqlite3.Error:
            log.exception("the MCP servers' tools could not be read")
            found = []
        return FunctionToolset([tool(o) for o in found
                                if allow_only is None or o.name in allow_only])


# --- on a card's yes -----------------------------------------------------------------

def _store() -> tuple[sqlite3.Connection, bool]:
    from agent.toolset.clients import commitment_store

    return commitment_store()


async def _approved_call(module: str, module_key: str, version: str, tool: str,
                         printed: str, args: dict, *, session: int | None = None) -> dict:
    conn, owned = _store()
    try:
        try:
            offer = await offloop.with_own_conn(conn, _offer, module, module_key, version,
                                                tool, printed)
        except lifecycle.Refused as exc:
            return {"ok": False, "error": str(exc)}
        if session is not None:
            allow(session, module_key, tool, printed)
        return await _running(conn, offer, args)
    finally:
        if owned:
            conn.close()


@executable(CALL)
async def _do_call(*, module: str, module_key: str, version: str, tool: str, printed: str,
                   args: dict) -> dict:
    """A call the user approved, run now as the card showed it."""
    return await _approved_call(module, module_key, version, tool, printed, args)


@executable(ALLOW)
async def _do_allow(*, session: int, module: str, module_key: str, version: str, tool: str,
                    printed: str, args: dict) -> dict:
    """A read the user approved in a conversation they are in: run now,
    and again there without asking for half an hour."""
    return await _approved_call(module, module_key, version, tool, printed, args,
                                session=session)


# --- adding one ----------------------------------------------------------------------

def _q(text: str) -> str:
    # JSON's string escapes are TOML's basic string's, kept to ASCII.
    return json.dumps(text, ensure_ascii=True)


def _package_text(name: str, label: str, package: servers.Package) -> str:
    lines = ["# An MCP server the user added, which Steve installs and runs in a",
             "# container of its own; written by Steve (agent/mcptools.py).",
             "manifest_version = 1", "sdk = 1", f"id = {_q(name)}", 'version = "1"',
             f"label = {_q(label or name)}", 'for = "The tools of an MCP server you added"']
    if package.hosts:
        lines.append(f"hosts = [{', '.join(_q(h) for h in sorted(package.hosts))}]")
    lines += ["", "[mcp]", f"package = {_q(f'{package.name}@{package.version}')}",
              f"entry = {_q(package.entry)}", f"before = {_q(package.before or '')}",
              f"tree = {_q(package.tree or '')}"]
    if package.args:
        lines.append(f"args = [{', '.join(_q(a) for a in package.args)}]")
    if package.anywhere:
        lines.append("anywhere = true")
    return "\n".join(lines) + "\n"


def _manifest_text(name: str, url: str, label: str, secret: bool) -> str:
    split = urlsplit(url)
    host = (split.hostname or "").lower()
    lines = ["# An MCP server the user added, written by Steve (agent/mcptools.py).",
             "manifest_version = 1", "sdk = 1", f"id = {_q(name)}", 'version = "1"',
             f"label = {_q(label or name)}", 'for = "The tools of an MCP server you added"']
    if host and host not in LOOPBACK_HOSTS:
        lines.append(f"hosts = [{_q(host)}]")
    if secret:
        port = split.port or 443
        bind = (f"{{ host = {_q(host)}, port = {port}, header = \"Authorization\", "
                f"form = \"Bearer {{}}\" }}")
        lines += ["", "[setup]", "fields = [",
                  f"  {{ name = \"{SECRET_FIELD}\", type = \"secret\", "
                  f"label = \"The server's secret\", bind = {bind} }},", "]"]
    lines += ["", "[mcp]", f"url = {_q(url)}"]
    if secret:
        lines.append(f"secret = \"{SECRET_FIELD}\"")
    return "\n".join(lines) + "\n"


def _write(folder: Path, name: str, url: str, label: str, secret: bool) -> Manifest:
    folder.mkdir(parents=True)
    path = folder / "manifest.toml"
    path.write_text(_manifest_text(name, url, label, secret), encoding="utf-8", newline="\n")
    try:
        return load(path)
    except ManifestError as exc:
        raise lifecycle.Refused(str(exc).replace(str(path), "the server")) from None


async def add(conn: sqlite3.Connection, *, name: str, url: str, secret: str = "",
              label: str = "") -> int:
    """A server the user typed in, up to its card: its manifest written by
    core, its tools listed through the broker with the secret held in
    memory alone, and the module admitted as one imported. The card's
    admission id; Refused saying why not."""
    if secret and urlsplit(url).scheme != "https":
        raise lifecycle.Refused("a secret goes over HTTPS only; a server on this computer "
                                "reached over plain HTTP takes none")
    folder = paths.scratch_dir() / f"mcp-{tokens.token_hex(6)}"
    try:
        manifest = await asyncio.to_thread(_write, folder, name, url, label, bool(secret))
        try:
            now = await asyncio.to_thread(
                listed, manifest, lambda field_name: secret if field_name == SECRET_FIELD else None)
        except ConnectorError as exc:
            raise lifecycle.Refused(f"the server: {exc}") from None
        await asyncio.to_thread(
            (folder / mcp.TOOLS_FILE).write_text,
            json.dumps(now, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        return await modadmit.admit(conn, folder, origin="imported")
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)


async def add_package(conn: sqlite3.Connection, *, name: str, package: str, entry: str,
                      hosts: list[str], label: str = "", anywhere: bool = False) -> int:
    """A server the user names as an npm package, up to its card. Named at
    one exact version at least seven days old (`servers.published`), it is
    installed as the registry stood when that version came out, in npm's
    own container with scripts off, and its tree hashed; the manifest pins
    that hash, and the server is started from it in its own container to
    list its tools. The card's admission id; Refused saying why not."""
    if not ID.fullmatch(name or ""):
        raise lifecycle.Refused(f"{name!r} is not a name a module can have")
    if await offloop.with_own_conn(conn, modules.module, name) is not None:
        raise lifecycle.Refused(f"a module named {name} is installed already")
    named, _, version = package.strip().rpartition("@")
    folder = paths.scratch_dir() / f"mcp-{tokens.token_hex(6)}"
    try:
        try:
            wanted = servers.Package(
                name=named, version=version, entry=entry,
                hosts={h.strip().lower(): (443,) for h in hosts if h.strip()},
                anywhere=anywhere)
            before = await asyncio.to_thread(servers.published, named, version,
                                             network=NETWORK)
            done = await asyncio.to_thread(
                servers.install, name, replace(wanted, before=before), network=NETWORK,
                redact=modctx.scrub)
            pinned = replace(wanted, before=before, tree=done.tree)
            folder.mkdir(parents=True)
            path = folder / "manifest.toml"
            path.write_text(_package_text(name, label, pinned), encoding="utf-8",
                            newline="\n")
            manifest = await asyncio.to_thread(load, path)
            now = await asyncio.to_thread(listed, manifest, lambda _field: None)
        except (ValueError, ManifestError, servers.NotHeld, servers.NotInstalled,
                ConnectorError) as exc:
            await asyncio.to_thread(_forget_package, name)
            raise lifecycle.Refused(f"the server: {exc}") from None
        finally:
            await asyncio.to_thread(servers.POOL.stop, name, servers.MODULE_KEY)
        await asyncio.to_thread(
            (folder / mcp.TOOLS_FILE).write_text,
            json.dumps(now, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        return await modadmit.admit(conn, folder, origin="imported")
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)


def _forget_package(name: str) -> None:
    """A server not added: its installed files and its container gone."""
    servers.forget(name, servers.MODULE_KEY)
    servers.remove(paths.module_dir(name) / "server")


def view(held: modules.Module) -> dict[str, Any] | None:
    """A server module's server and pinned tools, for the Extensions page:
    each with its label, whether it is offered, and whether it is freed.
    Blocking."""
    if held.manifest is None or held.manifest.mcp is None:
        return None
    try:
        server = _server(held)
    except ConnectorError:
        return {"url": held.manifest.mcp.url, "tools": [], "error": "its tools are unreadable"}
    offered = {o.tool_name: o for o in _offers_of(server)}
    # A server Steve runs has no address; its package names it.
    return {"url": held.manifest.mcp.url, "loopback": held.manifest.mcp.loopback,
            "package": held.manifest.mcp.package if held.manifest.mcp.local else "",
            "tools": [{"name": t["name"], "read": mcp.reads(t), "offered": t["name"] in offered,
                       "free": t["name"] in offered and offered[t["name"]].freed}
                      for t in server.tools]}
