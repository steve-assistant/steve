"""Mail and calendars through an MCP server: core's MCP engine, and its profiles.

Some accounts have no protocol Steve can speak. A personal Outlook.com
account has taken no app passwords since September 2024, and Graph from
Steve itself would need an app registration of Steve's own, which is
outward. An MCP server on the user's computer holds the account's sign-in
instead and offers tools. Those tools are made for a model, which is the
danger: their descriptions are the server's own text, the list can change
under an account, and nothing in MCP binds a tool to the item it was asked
about. So (05-connectors.md, section 5):

- The model never sees the server's tools. A profile, a table under the
  module's `profiles/`, maps each method of the class's provider onto one
  tool: its name, the arguments it is called with, and where each field
  of an item is in the tool's JSON answer. The model calls core's class
  tools; what the server returns is mail and events, screened like any
  other. A server that matches no profile is refused.
- The tool list is pinned. The app's test of the account matches the
  server to a profile and keeps a hash of every tool and its schema in the
  account's own folder; every later connection compares, and a list
  changed since stops the account until the user tests it again in the
  app (`ToolsChanged`).
- Writes go through core's write path (`core/writes.py`). Where the
  profile's tools take no etag or change key back, no write can be
  conditional, so an update or a delete asks by card every time. The
  provider reads the item again just before writing and refuses if its
  change key moved; the write path reads again afterwards and checks that
  exactly that item changed among its neighbours, so a server that acted on
  another id than the one it was given is caught and said. A write that
  would reach anyone else (guests on an event, a repeating series) is
  refused, as CalDAV's is.
- Every request goes through `ctx.http`: HTTPS, or plain HTTP to the
  loopback address the user typed. The optional secret goes in one header
  to that one address, and no redirect is followed.

The client speaks Streamable HTTP: initialise, list the tools, call one,
each a JSON-RPC message POSTed to one address and answered with JSON or an
event stream. Not the `mcp` SDK: it brings a web server's dependencies, and
its own HTTP stack would go around `ctx.http`'s checks. It offers no
sampling, roots or elicitation, so a server that asks is not answered.
`Stdio` speaks the same messages, a line each, to a server Steve starts in
its container (`core/sandbox/servers.py`); one that asks for any of those
three is told Steve does not offer it.

A profile's methods, by class, and the values each may use besides
`{select}` (its class's `select`, or `full` where bodies are wanted) and
`{top}` (how many to ask for):
- mail: `search` (`{kql}`, the query in Microsoft's search syntax),
  `recent`, `inbox` and `sent` (`{since}`), `thread` (`{thread}`), `drafts`,
  `fetch` and `headers` (`{id}`), `make_draft` (`{item}`), `reply_draft`
  (`{reply_to}`, `{item}`), `send_draft` and `delete` (`{id}`);
- calendar: `events` (`{start}`, `{end}`), `all`, `fetch` and `delete`
  (`{id}`), `create` (`{item}`), `update` (`{id}`, `{item}`).
A write that also takes `{condition}` makes the class conditional. Each of
`fetch`, and `search` or `events`, is required; any other a profile lacks is
a capability the account does not have.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import itertools
import json
import re
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from core import modctx
from core.modctx import Refused
from steve_sdk import classes, exact
from steve_sdk import query as query_syntax
from steve_sdk.classes import CalendarProvider, Draft, Event, Guest, MailProvider, Message
from steve_sdk.errors import (
    AfterWrite,
    ConnectorError,
    NotConnected,
    SignInNeeded,
    TargetChanged,
    Transient,
    Unsupported,
    WrongPassword,
)
from steve_sdk.link import Broken

# The protocol versions this client speaks: the three use initialise, list
# and call alike.
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
TIMEOUT_S = 60.0
MAX_LINE = 4 << 20      # one message from a server Steve started
PIN_FILE = "mcp-pin.json"
# A server module's tools as the user approved them, in its version's
# folder: what Steve offers, with no network, and each call checks against.
TOOLS_FILE = "mcp-tools.json"
MAX_TOOLS = 200
MAX_TOOLS_BYTES = 256 * 1024
MAX_TOOL_NAME = 128
# What a model's tool name may be: a tool whose offered name does not fit
# is shown on the card and not offered.
OFFERED_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
RECENT_DAYS = 30        # how far back "everything since" reaches when no date is given
MAX_TOOL_PAGES = 20
# An id handed out by a server, as it may be put into a tool's argument or
# a filter: no quote, space or control character can ride along.
_ID = re.compile(r"[A-Za-z0-9=+/_.:@<>-]{1,400}")
_WHOLE = re.compile(r"^\{([a-z_]+)\}$")
_PART = re.compile(r"\{([a-z_]+)\}")
_REQUIRED = {"mail": ("fetch",), "calendar": ("fetch", "events")}
_WRITES = {"mail": ("delete",), "calendar": ("update", "delete")}


class ToolsChanged(ConnectorError):
    """The server's tools are not those pinned when the account was last
    tested in the app: the account stops until the user tests it again."""

    kind = "tools_changed"


class Stopped(Transient, Broken):
    """A server Steve runs ended, or broke the protocol, during a call.
    For a remote server, no answer is the network's and counts against no
    one; this one is Steve's own process, so its watch counts a crash."""


class Silent(Transient, TimeoutError):
    """A server Steve runs did not answer in time: its watch counts a
    timeout, as it does for a module's own code."""


# --- the client ------------------------------------------------------------------

class _SessionGone(Exception):
    """The server forgot the session (404 to a request that carried it)."""


def _events(text: str) -> list[Any]:
    """The JSON-RPC messages in an event stream's `data:` lines."""
    out = []
    for block in re.split(r"\r?\n\r?\n", text):
        data = "\n".join(line[5:].lstrip(" ") for line in re.split(r"\r?\n", block)
                         if line.startswith("data:"))
        if data:
            try:
                out.append(json.loads(data))
            except ValueError:
                continue
    return out


class _Rpc:
    """What both ways of reaching a server share: initialise, list the
    tools, call one. A subclass sends a request and returns its reply."""

    protocol: str | None
    server: dict[str, Any]

    def _exchange(self, method: str, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        raise NotImplementedError

    @staticmethod
    def _result(method: str, reply: dict[str, Any]) -> dict[str, Any]:
        if "error" in reply:
            error = reply["error"] if isinstance(reply["error"], dict) else {}
            # A server's own tokens, which Steve never knew, by their shape.
            said = modctx.scrub(str(error.get("message") or ""))[:200]
            raise ConnectorError(f"the MCP server refused {method}: {said}")
        if not isinstance(reply.get("result"), dict):
            raise ConnectorError(f"the MCP server's answer to {method} held no result")
        return reply["result"]

    def _initialise(self) -> tuple[Any, str]:
        answer, result = self._exchange("initialize", {
            "protocolVersion": PROTOCOLS[0], "capabilities": {},
            "clientInfo": {"name": "steve", "version": "1"}})
        version = result.get("protocolVersion")
        if version not in PROTOCOLS:
            raise ConnectorError(f"the MCP server speaks version {str(version)[:40]!r} of "
                                 "the protocol, which Steve does not")
        info = result.get("serverInfo")
        self.server = info if isinstance(info, dict) else {}
        return answer, version

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._exchange(method, params)[1]

    def tools(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            listed = result.get("tools")
            if not isinstance(listed, list):
                raise ConnectorError("the MCP server listed no tools")
            out += [t for t in listed if isinstance(t, dict)]
            cursor = result.get("nextCursor")
            if not cursor:
                return out
        raise ConnectorError(f"the MCP server's tools ran past {MAX_TOOL_PAGES} pages")

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": tool, "arguments": arguments})


class Client(_Rpc):
    """Streamable HTTP to one MCP server, through `ctx.http`. `headers` are
    more of the request's own: a server module's secret goes as
    `{{secret:NAME}}`, which the broker fills where it is bound."""

    def __init__(self, ctx: Any, url: str, *, secret: str | None,
                 headers: dict[str, str] | None = None) -> None:
        self.ctx = ctx
        self.url = url
        self._secret = secret
        self._extra = dict(headers or {})
        self.session: str | None = None
        self.protocol: str | None = None
        self.server: dict[str, Any] = {}
        self._ids = itertools.count(1)

    def _headers(self) -> dict[str, str]:
        out = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json", **self._extra}
        if self._secret:
            out["Authorization"] = f"Bearer {self._secret}"
        if self.session:
            out["Mcp-Session-Id"] = self.session
        if self.protocol:
            out["MCP-Protocol-Version"] = self.protocol
        return out

    def _post(self, message: dict[str, Any], ok: tuple[int, ...]) -> Any:
        try:
            answer = self.ctx.http.request("POST", self.url, headers=self._headers(),
                                           content=json.dumps(message).encode("utf-8"),
                                           timeout=TIMEOUT_S)
        except Refused as exc:
            raise NotConnected(str(exc)) from exc
        except Transient as exc:
            raise Transient(f"the MCP server did not answer ({exc}); is it running?") from exc
        except httpx.HTTPError as exc:
            raise Transient(f"the MCP server did not answer ({type(exc).__name__}); "
                            "is it running?") from exc
        if answer.status in ok:
            return answer
        if answer.status == 404 and self.session:
            raise _SessionGone
        if answer.status == 401:
            raise WrongPassword("the MCP server did not take its secret")
        if answer.status == 429 or answer.status >= 500:
            raise Transient(f"the MCP server answered {answer.status}")
        raise ConnectorError(f"the MCP server refused the request ({answer.status})")

    @staticmethod
    def _reply(answer: Any, ident: int) -> dict[str, Any]:
        kind = answer.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind == "text/event-stream":
            messages = _events(answer.text)
        elif kind == "application/json":
            try:
                body = json.loads(answer.content)
            except ValueError as exc:
                raise ConnectorError("the MCP server's answer is not JSON") from exc
            messages = body if isinstance(body, list) else [body]
        else:
            raise ConnectorError(f"the MCP server answered {kind or 'nothing'}, not JSON")
        for message in messages:
            if (isinstance(message, dict) and message.get("id") == ident
                    and ("result" in message or "error" in message)):
                return message
        raise ConnectorError("the MCP server's answer held no reply to the request")

    def _exchange(self, method: str, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        ident = next(self._ids)
        answer = self._post({"jsonrpc": "2.0", "id": ident, "method": method,
                             "params": params}, ok=(200,))
        return answer, self._result(method, self._reply(answer, ident))

    def open(self) -> dict[str, Any]:
        """Initialise; the server's name and version, as it gives them."""
        self.session = self.protocol = None
        answer, version = self._initialise()
        session = answer.headers.get("mcp-session-id")
        if session is not None and not re.fullmatch(r"[\x21-\x7e]{1,200}", session):
            raise ConnectorError("the MCP server's session id is not one")
        self.session, self.protocol = session, version
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                   ok=(200, 202, 204))
        return self.server

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._exchange(method, params)[1]
        except _SessionGone:
            # A server restarted under a session: once more, from the start.
            self.open()
            return self._exchange(method, params)[1]


class Stdio(_Rpc):
    """One MCP server over its own standard input and output, a JSON
    message a line: a server Steve started in its container. A thread
    reads what it says; a request of the server's own (sampling, roots,
    elicitation) is answered that Steve does not offer it, and a line that
    is not a message is passed over. `timeout` holds each request."""

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO, *,
                 timeout: float = TIMEOUT_S) -> None:
        self._in = stdin
        self._out = stdout
        self.timeout = timeout
        self.protocol: str | None = None
        self.server: dict[str, Any] = {}
        self._ids = itertools.count(1)
        self._waiting: set[int] = set()
        self._replies: dict[int, dict[str, Any]] = {}
        self._ended: str | None = None
        self._arrived = threading.Condition()
        self._sending = threading.Lock()
        threading.Thread(target=self._read, daemon=True, name="mcp-stdio").start()

    def _send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._sending:
            try:
                self._in.write(line)
                self._in.flush()
            except (OSError, ValueError) as exc:
                raise Stopped("the MCP server has stopped") from exc

    def _read(self) -> None:
        why = "the MCP server has stopped"
        try:
            while line := self._out.readline(MAX_LINE + 1):
                if len(line) > MAX_LINE:
                    why = f"the MCP server sent a message past {MAX_LINE >> 20} MB"
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    self._send({"jsonrpc": "2.0", "id": message["id"], "error": {
                        "code": -32601, "message": "Steve does not offer this"}})
                    continue
                ident = message.get("id")
                if "result" in message or "error" in message:
                    with self._arrived:
                        # A list or an object is unhashable: the TypeError
                        # ended this thread, and the server read as stopped
                        # to every call from then on.
                        if isinstance(ident, int) and ident in self._waiting:
                            self._replies[ident] = message
                            self._arrived.notify_all()
        except (OSError, ValueError, Transient):
            pass
        finally:
            with self._arrived:
                self._ended = why
                self._arrived.notify_all()

    def _exchange(self, method: str, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        ident = next(self._ids)
        with self._arrived:
            if self._ended:
                raise Stopped(self._ended)
            self._waiting.add(ident)
        try:
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            with self._arrived:
                if not self._arrived.wait_for(
                        lambda: ident in self._replies or self._ended, self.timeout):
                    raise Silent(f"the MCP server did not answer {method} "
                                 f"in {self.timeout:.0f} seconds")
                reply = self._replies.pop(ident, None)
                ended = self._ended
        finally:
            # A reply landing as the wait gave up would otherwise stay kept.
            with self._arrived:
                self._waiting.discard(ident)
                self._replies.pop(ident, None)
        if reply is None:
            raise Stopped(ended or "the MCP server has stopped")
        return None, self._result(method, reply)

    def open(self) -> dict[str, Any]:
        """Initialise, once; the server's name and version, as it gives
        them. A server the pool started is opened already."""
        if self.protocol is not None:
            return self.server
        _, self.protocol = self._initialise()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.server


def fingerprint(tools: list[dict[str, Any]]) -> str:
    """Every tool, its schemas and its notes, as one hash: what is pinned."""
    kept = sorted(({k: t.get(k) for k in ("name", "title", "description", "inputSchema",
                                           "outputSchema", "annotations")} for t in tools),
                  key=lambda t: str(t["name"]))
    text = json.dumps(kept, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def kept(tools: Any) -> list[dict[str, Any]]:
    """A server's tools as Steve keeps them, or ConnectorError: objects
    each with a name of its own, within the limits. What a server lists is
    its own text, which is pinned, shown on a card and offered from."""
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise ConnectorError(f"the MCP server's tools are not a list of at most {MAX_TOOLS}")
    names: set[str] = set()
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not 0 < len(name) <= MAX_TOOL_NAME:
            raise ConnectorError("the MCP server listed a tool without a name")
        if name in names:
            raise ConnectorError(f"the MCP server listed the tool {name[:60]!r} twice")
        names.add(name)
    if len(json.dumps(tools, ensure_ascii=False).encode("utf-8")) > MAX_TOOLS_BYTES:
        raise ConnectorError(f"the MCP server's tools run past {MAX_TOOLS_BYTES // 1024} KB")
    return tools


def pinned(folder: Path) -> list[dict[str, Any]]:
    """The tools pinned in a server module's folder (`TOOLS_FILE`)."""
    try:
        listed = json.loads((folder / TOOLS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectorError(f"its server's tools could not be read ({type(exc).__name__})"
                             ) from exc
    return kept(listed)


def prints(tools: list[dict[str, Any]]) -> dict[str, str]:
    """Each tool's own fingerprint, by name: what a new listing is held to."""
    return {str(t["name"]): fingerprint([t]) for t in tools}


def reads(tool: dict[str, Any]) -> bool:
    """Whether the server labels the tool a read: `readOnlyHint` true and
    nothing else. A tool it does not label is a write."""
    notes = tool.get("annotations")
    return isinstance(notes, dict) and notes.get("readOnlyHint") is True


def offered_as(module_id: str, tool: str) -> str | None:
    """The name a server module's tool is offered to the model under, or
    None where that would not be a model's tool name."""
    name = f"mcp__{module_id}__{tool}"
    return name if OFFERED_NAME.fullmatch(name) else None


# --- profiles --------------------------------------------------------------------

@dataclass(frozen=True)
class Method:
    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Mapping:
    """One class in a profile."""

    items: str
    more: str
    select: str
    full: str
    fields: dict[str, Any]
    methods: dict[str, Method]


@dataclass(frozen=True)
class Profile:
    file: str
    name: str
    server: str
    checked: str
    status: str
    classes: dict[str, Mapping]
    package: Any = None          # the server Steve runs for it (servers.Package), if any


_PROFILES: dict[tuple[str, float], Profile] = {}


def _profile_error(file: str, said: str) -> ConnectorError:
    return ConnectorError(f"profile {file}: {said}")


def load(path: Path) -> Profile:
    """One profile file, checked as far as its shape; cached by its time."""
    key = (str(path), path.stat().st_mtime)
    if key in _PROFILES:
        return _PROFILES[key]
    file = path.name
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _profile_error(file, f"not read: {exc}") from exc
    found: dict[str, Mapping] = {}
    for klass in ("mail", "calendar"):
        table = data.get(klass)
        if table is None:
            continue
        methods = {}
        for name, method in (table.get("methods") or {}).items():
            if not (isinstance(method, dict) and isinstance(method.get("tool"), str)
                    and isinstance(method.get("args", {}), dict)):
                raise _profile_error(file, f"{klass}.methods.{name} names no tool")
            methods[name] = Method(tool=method["tool"], args=dict(method.get("args", {})))
        if missing := [m for m in _REQUIRED[klass] if m not in methods]:
            raise _profile_error(file, f"{klass} has no {', '.join(missing)}")
        fields = dict(table.get("fields") or {})
        if not {"native_id", "ident"} <= set(fields):
            raise _profile_error(file, f"{klass}.fields name no native_id and ident")
        found[klass] = Mapping(items=str(table.get("items", "value")),
                               more=str(table.get("more", "")),
                               select=str(table.get("select", "")),
                               full=str(table.get("full", table.get("select", ""))),
                               fields=fields, methods=methods)
    if not isinstance(data.get("server"), str) or not found:
        raise _profile_error(file, "names no server, or maps no class")
    profile = Profile(file=file, name=str(data.get("name", file)), server=data["server"],
                      checked=str(data.get("checked", "")), status=str(data.get("status", "")),
                      classes=found, package=_package(data.get("package"), path))
    _PROFILES[key] = profile
    return profile


def _package(table: Any, path: Path) -> Any:
    """A profile's `[package]`: the npm package of the server Steve installs
    and runs for it (`core/sandbox/servers.py`), at one exact version, held
    to the hosts it names on 443, from the release's reviewed lockfile
    beside the profile and the tree's hash the release approved."""
    from core.sandbox import servers

    if table is None:
        return None
    file = path.name
    if not isinstance(table, dict):
        raise _profile_error(file, "[package] is not a table")
    if unknown := set(table) - {"package", "entry", "args", "hosts", "lockfile", "tree",
                                "sign_in", "env"}:
        raise _profile_error(file, f"[package] has no {', '.join(sorted(unknown))}")
    try:
        name, _, version = str(table["package"]).rpartition("@")
        lockfile = None
        if table.get("lockfile"):
            lockfile = (path.parent / str(table["lockfile"])).resolve()
            if path.parent.resolve() not in lockfile.parents:
                raise ValueError("its lockfile is not beside the profile")
        sign_in = table.get("sign_in")
        hosts = table["hosts"]
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise ValueError("hosts is not a list of names")
        return servers.Package(
            name=name, version=version, entry=str(table["entry"]),
            hosts={h.lower(): (443,) for h in hosts},
            args=tuple(str(a) for a in table.get("args", ())), lockfile=lockfile,
            tree=str(table["tree"]) if table.get("tree") else None,
            sign_in=servers.SignIn(**sign_in) if isinstance(sign_in, dict) else None,
            env={str(k): str(v) for k, v in (table.get("env") or {}).items()})
    except (KeyError, TypeError, ValueError) as exc:
        raise _profile_error(file, f"[package]: {exc}") from None


def profiles(manifest: Any, klass: str) -> list[Profile]:
    """The profiles a module's manifest names for `klass`, each inside the
    module's own folder."""
    if manifest.path is None:
        raise ConnectorError("the module's folder is not known, so neither are its profiles")
    home = Path(manifest.path).parent.resolve()
    out = []
    for name in manifest.specs[klass].settings.get("profiles", []):
        path = (home / str(name)).resolve()
        if home not in path.parents or path.suffix != ".toml":
            raise ConnectorError(f"profile {name}: not a table in the module's own folder")
        profile = load(path)
        if klass in profile.classes:
            out.append(profile)
    if not out:
        raise ConnectorError(f"the module names no profile for its {klass}")
    return out


def _fits(mapping: Mapping, tools: dict[str, dict[str, Any]]) -> str:
    """Why the server's tools do not fit the mapping; '' where they do."""
    for name, method in mapping.methods.items():
        tool = tools.get(method.tool)
        if tool is None:
            return f"it has no tool {method.tool!r} (for {name})"
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        known = schema.get("properties")
        if isinstance(known, dict):
            if extra := sorted(set(method.args) - set(known)):
                return f"its {method.tool!r} takes no {', '.join(extra)}"
            needed = schema.get("required") if isinstance(schema.get("required"), list) else []
            if missing := sorted(set(needed) - set(method.args)):
                return f"its {method.tool!r} needs {', '.join(missing)}"
    return ""


def match(found: list[Profile], klass: str, server: dict[str, Any],
          tools: list[dict[str, Any]]) -> Profile:
    """The first profile for the server's name whose every tool it serves,
    taking the arguments the profile gives; or ConnectorError saying why."""
    named = {str(t.get("name")): t for t in tools}
    said = str(server.get("name") or "a server with no name")[:80]
    why = "Steve has no profile for it"
    for profile in found:
        if profile.server != server.get("name"):
            continue
        why = _fits(profile.classes[klass], named)
        if not why:
            return profile
    raise ConnectorError(f"the MCP server {said!r} {str(server.get('version') or '')[:20]} "
                         f"matches no profile Steve has: {why}")


def _capabilities(found: list[Profile], klass: str) -> set[str]:
    """What every profile can do, so an account has it whichever matches."""
    sets = []
    for profile in found:
        mapping = profile.classes[klass]
        have = set(mapping.methods)
        caps = set()
        if klass == "mail":
            caps |= {classes.SEND} if "send_draft" in have else set()
            caps |= {classes.THREADS} if "thread" in have else set()
        else:
            caps |= {c for c, m in ((classes.CREATE, "create"), (classes.UPDATE, "update"),
                                    (classes.DELETE, "delete")) if m in have}
        writes = [mapping.methods[m] for m in _WRITES[klass] if m in have]
        if writes and all("{condition}" in json.dumps(m.args) for m in writes):
            caps.add(classes.CONDITIONAL)
        sets.append(caps)
    return set.intersection(*sets) if sets else set()


# --- reading and writing an item by its paths ------------------------------------

def get(item: Any, path: str) -> Any:
    """The value at `path`: `a.b` a key inside a key, `a[].b` the same key
    in each of a list (empty values left out)."""
    if "[]" in path:
        head, _, rest = path.partition("[]")
        listed = get(item, head) if head else item
        if not isinstance(listed, list):
            return []
        rest = rest.lstrip(".")
        values = [get(el, rest) if rest else el for el in listed]
        return [v for v in values if v not in (None, "")]
    value = item
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def put(out: dict[str, Any], path: str, value: Any) -> None:
    """`value` at `path` in `out`, the tables on the way made; `a[].b` makes
    a list of one table per value."""
    if "[]" in path:
        head, _, rest = path.partition("[]")
        rest = rest.lstrip(".")
        made = []
        for v in value:
            if rest:
                one: dict[str, Any] = {}
                put(one, rest, v)
                made.append(one)
            else:
                made.append(v)
        put(out, head, made)
        return
    keys = path.split(".")
    for key in keys[:-1]:
        out = out.setdefault(key, {})
    out[keys[-1]] = value


def _spec(spec: Any) -> dict[str, Any]:
    return {"path": spec} if isinstance(spec, str) else dict(spec)


def _flat(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def _zone(name: Any) -> dt.tzinfo:
    if name in (None, "", "UTC", "Etc/UTC", "Coordinated Universal Time"):
        return dt.UTC
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConnectorError(f"a time zone this engine cannot read: {str(name)[:60]}") from exc


def moment(text: Any, zone: Any = None) -> dt.datetime | None:
    """An ISO time, as the server wrote it, in UTC."""
    if not text:
        return None
    try:
        when = dt.datetime.fromisoformat(str(text))
    except ValueError as exc:
        raise ConnectorError(f"not a time: {str(text)[:40]}") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=_zone(zone))
    return when.astimezone(dt.UTC)


def iso(when: dt.datetime | dt.date) -> str:
    if not isinstance(when, dt.datetime):
        when = dt.datetime.combine(when, dt.time(), dt.UTC)
    if when.tzinfo is None:
        raise ValueError("a time without its zone")
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(item: dict[str, Any], fields: dict[str, Any], name: str) -> Any:
    """One field of an item, by the profile's spec for it; None if unmapped."""
    if name not in fields:
        return None
    spec = _spec(fields[name])
    if "each" in spec:
        listed = get(item, spec["each"])
        return [{k: get(el, p) for k, p in spec.items() if k != "each"}
                for el in (listed if isinstance(listed, list) else []) if isinstance(el, dict)]
    value = get(item, spec["path"])
    if "not" in spec:
        return value != spec["not"]
    if "zone" in spec:
        return moment(value, get(item, spec["zone"]))
    if "html" in spec and isinstance(value, str) \
            and str(get(item, spec["html"]) or "").lower() == "html":
        return _flat(value)
    return value


def write(out: dict[str, Any], fields: dict[str, Any], name: str, value: Any) -> None:
    """One field into a write's content, at the profile's path for it."""
    if name not in fields:
        raise Unsupported(f"writing an item's {name} to this account")
    spec = _spec(fields[name])
    if "each" in spec or "not" in spec:
        raise Unsupported(f"writing an item's {name} to this account")
    path = spec.get("write", spec["path"])
    if "zone" in spec:
        when = value if isinstance(value, dt.datetime) else \
            dt.datetime.combine(value, dt.time(), dt.UTC)
        put(out, path, iso(when).rstrip("Z"))
        put(out, spec["zone"], "UTC")
        return
    put(out, path, value)
    if "html" in spec:
        put(out, spec["html"], "text")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _addresses(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    return [a.strip() for a in (value or []) if isinstance(a, str) and a.strip()]


def _safe(value: str, what: str = "id") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise TargetChanged(f"that is not an {what} this account handed out")
    return value


def _top(limit: int) -> int:
    return max(10, min(int(limit), 250))


def _ms(when: dt.datetime | None) -> int:
    return int(when.timestamp() * 1000) if when else 0


def _kql(parsed: query_syntax.Query) -> str:
    """The query in Microsoft's search syntax, as far as it can say it; the
    rest is checked on this side. No quote, backslash or colon of a word
    gets through, so a word cannot close the search or start a clause."""
    def clean(word: str) -> str:
        return re.sub(r'["\\:\x00-\x1f]', " ", word).strip()

    pairs = ([("from:", w) for w in parsed.sender] + [("to:", w) for w in parsed.recipient]
             + [("subject:", w) for w in parsed.subject] + [("", w) for w in parsed.terms])
    out = []
    for prefix, word in pairs:
        word = clean(word)
        if word and " " not in word:
            out.append(prefix + word)
    return " ".join(out)


# --- the providers -----------------------------------------------------------------

class _Mcp:
    """What both classes share: the server, matched and checked against the
    pin once per provider, and a profile's method called by its name."""

    klass: str

    def __init__(self, ctx: Any, spec: Any) -> None:
        self.ctx = ctx
        self.spec = spec
        account = ctx.account
        self.address = (account.address or "").strip()
        # A profile that cannot be read fails this account's calls, said;
        # raised here, it would fail the listing of every account with it.
        self._broken: ConnectorError | None = None
        try:
            self.found = profiles(ctx.manifest, self.klass)
        except ConnectorError as exc:
            self.found, self._broken = [], exc
        self.capabilities = frozenset(_capabilities(self.found, self.klass))
        self._lock = threading.Lock()
        self._client: _Rpc | None = None
        self.profile: Profile | None = None
        self.tools: list[dict[str, Any]] = []

    # --- the server

    def _url(self) -> str:
        settings = self.ctx.manifest.settings(self.klass, self.ctx.account.preset)
        if settings.get("url"):
            return str(settings["url"])
        fields = dict(self.ctx.account.fields or {})
        hosts = dict(fields.get("hosts") or {})
        typed = next((str(fields[f.name]) for f in self.ctx.manifest.setup.fields
                      if f.type == "host" and fields.get(f.name)), "")
        host, sep, port = typed.strip().lower().rpartition(":")
        if not sep or not port.isdigit():
            raise NotConnected("the MCP server's address needs its port, as 127.0.0.1:port")
        name = host.strip("[]")
        scheme = "http" if hosts.get(name) == "loopback" else "https"
        shown = f"[{name}]" if ":" in name else name
        return f"{scheme}://{shown}:{int(port)}{settings.get('path', '/')}"

    def runs(self) -> Any:
        """The package of the server Steve runs for this account, or None
        where the user runs it: the account's choice, its preset's `runs`."""
        settings = self.ctx.manifest.settings(self.klass, self.ctx.account.preset)
        if settings.get("runs", "") != "steve":
            return None
        if self.ctx.manifest.tier != "backend":
            # Only a module Steve ships has the backend tier. Admission refuses
            # a package from outside (`core/lifecycle._runs_a_server`); this
            # holds the same where the server would be installed and started.
            raise NotConnected("only a module Steve ships has Steve run its server")
        package = next((p.package for p in self.found if p.package is not None), None)
        if package is None:
            raise NotConnected("no profile of this module names a server Steve can run")
        return package

    def _secret(self) -> str | None:
        names = [f.name for f in self.ctx.manifest.setup.fields if f.secret]
        return self.ctx.secret(names[0]) if len(names) == 1 else None

    def _pin_path(self) -> Path:
        if self.ctx.state_dir is None:
            raise ConnectorError("the account has no folder to keep its server's tools in")
        return Path(self.ctx.state_dir) / PIN_FILE

    def _pinned(self) -> dict[str, Any]:
        try:
            kept = json.loads(self._pin_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}
        return kept if isinstance(kept, dict) else {}

    def _pin(self, server: dict[str, Any], hashed: str, profile: Profile) -> None:
        path = self._pin_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        part.write_text(json.dumps({"server": server.get("name"), "tools": hashed,
                                    "profile": profile.file}, sort_keys=True),
                        encoding="utf-8", newline="\n")
        part.replace(path)

    def _open(self, *, pin: bool = False) -> tuple[_Rpc, Profile]:
        if self._broken is not None:
            raise self._broken
        with self._lock:
            # A server Steve runs is asked of the pool each time: one stopped
            # when idle, or ended, is started afresh, and its tools checked
            # against the pin again, as a new server's would be. One the
            # user runs is reached at the address they typed.
            package = self.runs()
            local = self.ctx.server(package) if package is not None else None
            if (self._client is not None and self.profile is not None and not pin
                    and (local is None or local is self._client)):
                return self._client, self.profile
            if local is not None:
                client, server = local, local.server   # the pool opened it
            else:
                client = Client(self.ctx, self._url(), secret=self._secret())
                server = client.open()
            tools = client.tools()
            hashed = fingerprint(tools)
            if not pin:
                kept = self._pinned()
                if not kept:
                    raise ToolsChanged("its server's tools have not been checked yet")
                if kept.get("tools") != hashed or kept.get("server") != server.get("name"):
                    raise ToolsChanged("its server's tools changed since the account was "
                                       "last tested")
            profile = match(self.found, self.klass, server, tools)
            if pin:
                # Before the pin: tools pinned for the wrong mailbox would
                # serve it under this account's name until tested again.
                if local is not None:
                    self._signed_in_as_the_account(local, profile)
                self._pin(server, hashed, profile)
            self._client, self.profile, self.tools = client, profile, tools
            return client, profile

    @property
    def mapping(self) -> Mapping:
        return self._open()[1].classes[self.klass]

    def _has(self, method: str) -> bool:
        return method in self.mapping.methods

    def _call(self, method: str, **values: Any) -> Any:
        """The profile's tool for `method`, called with its arguments filled;
        the JSON it answered. None where the server said the item is not
        there (404)."""
        client, profile = self._open()
        mapping = profile.classes[self.klass]
        found = mapping.methods.get(method)
        if found is None:
            raise Unsupported(f"{method.replace('_', ' ')} through this account's server")

        def fill(template: Any) -> Any:
            if isinstance(template, str):
                whole = _WHOLE.match(template)
                try:
                    if whole:
                        return values[whole[1]]
                    return _PART.sub(lambda m: str(values[m[1]]), template)
                except KeyError as exc:
                    raise ConnectorError(f"profile {profile.file}: {method} asks for "
                                         f"{exc.args[0]!r}, which it is not given") from exc
            if isinstance(template, dict):
                return {k: fill(v) for k, v in template.items()}
            if isinstance(template, list):
                return [fill(v) for v in template]
            return template

        result = client.call(found.tool, fill(found.args))
        return self._answer(result, profile, found.tool)

    def _answer(self, result: dict[str, Any], profile: Profile, tool: str) -> Any:
        structured = result.get("structuredContent")
        text = next((c.get("text") for c in result.get("content") or []
                     if isinstance(c, dict) and c.get("type") == "text"), None)
        if result.get("isError"):
            status = get(result, profile.status) if profile.status else None
            said = _text(text)[:200]
            if status == 404:
                return None
            if status in (401, 403):
                raise SignInNeeded(f"the MCP server's own sign-in was refused: {said}")
            if status == 412:
                raise TargetChanged("the item changed on the server since it was read")
            if status == 429 or (isinstance(status, int) and status >= 500):
                raise Transient(f"the server behind the MCP server answered {status}")
            raise ConnectorError(f"the MCP server's {tool} failed: {said}")
        if isinstance(structured, dict):
            return structured
        if text is None:
            return {}
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ConnectorError(f"the MCP server's {tool} answered text, not JSON") from exc

    def _list(self, method: str, **values: Any) -> tuple[list[dict[str, Any]], bool]:
        """A listing's items, and whether the server said more remain."""
        answer = self._call(method, **values)
        mapping = self.mapping
        items = get(answer, mapping.items) if isinstance(answer, dict) else answer
        if not isinstance(items, list):
            raise ConnectorError(f"the MCP server's {method} answered no list")
        more = bool(mapping.more and isinstance(answer, dict) and get(answer, mapping.more))
        return [i for i in items if isinstance(i, dict)], more

    def _neighbours(self, method: str) -> dict[str, str | None] | None:
        if not self._has(method):
            return None
        fields = self.mapping.fields
        items, more = self._list(method)
        if more:
            return None  # cut short: a partial list would show changes that are not
        out: dict[str, str | None] = {}
        for item in items:
            ident = _text(read(item, fields, "ident"))
            if ident:
                out[ident] = _text(read(item, fields, "condition")) or None
        return out

    def _test(self) -> str:
        self._open(pin=True)
        profile = self.profile
        assert profile is not None
        client = self._client
        assert client is not None
        version = str(client.server.get("version") or "")[:20]
        return (f"{profile.server} {version} answered, matched to {profile.name}; "
                f"its {len(self.tools)} tools pinned")

    def _signed_in_as_the_account(self, client: _Rpc, profile: Profile) -> None:
        """A server Steve runs signs itself in, in the user's browser, which
        may be signed in to another account, the user's own or someone
        else's; that mailbox would then answer under this account's name.
        So the address the server says it signed in as must be the
        account's, where the profile names the tool that says it."""
        package = self.runs()
        sign_in = package.sign_in if package is not None else None
        if sign_in is None or not sign_in.whoami:
            return
        answer = self._answer(client.call(sign_in.whoami, {}), profile, sign_in.whoami)
        said = get(answer, sign_in.address)
        if isinstance(said, list):
            # Each account the server holds: one, or none is this account's alone.
            said = said[0] if len(said) == 1 else None
        said = _text(said).strip().lower()
        if not said or said != self.address.lower():
            raise SignInNeeded(f"the server signed in as {said or 'no address'}: sign it in "
                               f"again as {self.address}")


class McpMail(_Mcp, MailProvider):
    """The mail class through an MCP server, for one account."""

    klass = "mail"

    def _message(self, item: dict[str, Any], *, body: bool, body_chars: int = 20_000
                 ) -> Message | None:
        fields = self.mapping.fields
        native = _text(read(item, fields, "native_id"))
        if not native:
            return None
        sender = _text(read(item, fields, "sender"))
        text = _text(read(item, fields, "body")) if body else None
        return Message(
            native_id=native, ident=_text(read(item, fields, "ident")),
            condition=_text(read(item, fields, "condition")) or None,
            thread=_text(read(item, fields, "thread")), sender=sender,
            to=", ".join(_addresses(read(item, fields, "to"))),
            cc=", ".join(_addresses(read(item, fields, "cc"))),
            bcc=", ".join(_addresses(read(item, fields, "bcc"))),
            subject=_text(read(item, fields, "subject")),
            when=_ms(moment(read(item, fields, "when"))),
            snippet=_text(read(item, fields, "snippet")),
            body=text[:body_chars] if text is not None else None,
            body_length=len(text or ""), unread=bool(read(item, fields, "unread")),
            folder=_text(read(item, fields, "folder")),
            direction="out" if sender.lower() == self.address.lower() else "in",
            draft=bool(read(item, fields, "draft")))

    def _messages(self, method: str, *, body: bool, **values: Any) -> list[Message]:
        mapping = self.mapping
        items, _ = self._list(method, select=mapping.full if body else mapping.select,
                              **values)
        return [m for m in (self._message(i, body=body) for i in items) if m is not None]

    def search(self, query: str, *, limit: int, since: dt.datetime | None = None
               ) -> list[Message]:
        parsed = query_syntax.parse(query)
        now = dt.datetime.now(dt.UTC)
        if parsed.days:
            since = max(since or now - dt.timedelta(days=parsed.days),
                        now - dt.timedelta(days=parsed.days))
        kql = _kql(parsed)
        # A plain word may be anywhere in a message, so its text is read to
        # check it; the server's word for it is not taken.
        with_text = bool(parsed.terms)
        if kql and self._has("search"):
            found = self._messages("search", body=with_text, kql=kql, top=_top(limit))
        else:
            start = since or now - dt.timedelta(days=RECENT_DAYS)
            found = self._messages("recent", body=with_text, since=iso(start),
                                   top=_top(limit))
        floor = _ms(since)
        kept = [m for m in found
                if not m.draft and m.when >= floor
                and query_syntax.matches(parsed, subject=m.subject, sender=m.sender,
                                         recipient=f"{m.to} {m.cc}", unread=m.unread,
                                         text=f"{m.snippet} {m.body or ''}")]
        kept = [replace(m, body=None, body_length=0) for m in kept]
        kept.sort(key=lambda m: m.when, reverse=True)
        return kept[:limit]

    def ignored(self, query: str) -> list[str]:
        return list(query_syntax.parse(query).ignored)

    def arrived(self, *, since: dt.datetime, limit: int,
                seen: Callable[[list[str]], set[str]]) -> list[Message]:
        if not self._has("inbox"):
            return super().arrived(since=since, limit=limit, seen=seen)
        floor = _ms(since)
        found = [m for m in self._messages("inbox", body=False, since=iso(since),
                                           top=_top(limit))
                 if not m.draft and m.direction != "out" and m.when >= floor]
        found.sort(key=lambda m: m.when, reverse=True)
        found = found[:limit]
        old = seen([m.native_id for m in found])
        return [m for m in found if m.native_id not in old]

    def fetch(self, native_id: str, *, body_chars: int = 20_000) -> Message | None:
        item = self._call("fetch", id=_safe(native_id), select=self.mapping.full)
        if not isinstance(item, dict):
            return None
        return self._message(item, body=True, body_chars=body_chars)

    def headers(self, native_id: str) -> dict[str, str]:
        item = self._call("headers", id=_safe(native_id))
        if not isinstance(item, dict):
            raise TargetChanged("that message is no longer there")
        fields = self.mapping.fields
        if _text(read(item, fields, "native_id")) != native_id:
            raise TargetChanged("the server answered for another message")
        listed = read(item, fields, "headers")
        out: dict[str, str] = {}
        for h in listed if isinstance(listed, list) else []:
            if isinstance(h, dict) and h.get("name"):
                # The topmost of each name, as the class says.
                out.setdefault(str(h["name"]), str(h.get("value") or ""))
        return out

    def thread(self, thread_id: str, *, limit: int) -> list[Message]:
        if not self._has("thread"):
            raise Unsupported("threads")
        found = [m for m in self._messages("thread", body=False,
                                           thread=_safe(thread_id, "thread"), top=_top(limit))
                 if m.thread == thread_id]
        found.sort(key=lambda m: m.when)
        return found[:limit]

    def sent_items(self, *, since: dt.datetime, limit: int, to: str = "") -> list[Message]:
        """From the Sent folder alone, with bodies; `to` checked on each."""
        if to and not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+", to):
            raise ValueError(f"not one address: {to!r}")
        if not self._has("sent"):
            raise Unsupported("reading sent mail")
        floor = _ms(since)
        found = [m for m in self._messages("sent", body=True, since=iso(since), top=_top(limit))
                 if m.when >= floor and not m.draft
                 and (not to or to.lower() in (a.lower() for a in _addresses(m.to)))]
        found.sort(key=lambda m: m.when, reverse=True)
        return found[:limit]

    def envelopes(self, *, sent: bool, since: dt.datetime, limit: int) -> list[Message]:
        method = "sent" if sent else "inbox"
        if not self._has(method):
            raise Unsupported("listing a window of mail")
        floor = _ms(since)
        found = [m for m in self._messages(method, body=False, since=iso(since),
                                           top=_top(limit))
                 if m.when >= floor and not m.draft]
        found.sort(key=lambda m: m.when, reverse=True)
        return found[:limit]

    def neighbours(self, item: Any) -> dict[str, str | None] | None:
        """The Drafts folder, by Message-ID and change key: the only folder
        this account writes in."""
        return self._neighbours("drafts")

    def test(self) -> str:
        said = self._test()
        now = dt.datetime.now(dt.UTC)
        week = self.envelopes(sent=False, since=now - dt.timedelta(days=7), limit=50) \
            if self._has("inbox") else self.search("", limit=50)
        return f"{said}; {len(week)} messages in the last week"

    # --- drafts

    def _content(self, draft: Draft, *, reply: bool) -> dict[str, Any]:
        fields = self.mapping.fields
        out: dict[str, Any] = {}
        if not reply:
            write(out, fields, "subject", draft.subject)
        write(out, fields, "body", draft.body)
        for name in ("to", "cc", "bcc"):
            listed = _addresses(getattr(draft, name))
            if listed:
                write(out, fields, name, listed)
        return out

    def make_draft(self, draft: Draft) -> Message:
        if draft.attachments:
            raise Unsupported("attachments on a draft in this account")
        if draft.sender and draft.sender.lower() != self.address.lower():
            raise Unsupported("sending as another address from this account")
        if draft.reply_to:
            made = self._call("reply_draft", reply_to=_safe(draft.reply_to),
                              item=self._content(draft, reply=True))
        else:
            if not self._has("make_draft"):
                raise Unsupported("drafts")
            made = self._call("make_draft", item=self._content(draft, reply=False))
        first = self._message(made, body=False) if isinstance(made, dict) else None
        if first is None:
            raise ConnectorError("the server did not say which draft it made")
        # The draft as stored, read back by the id it was given: it must be
        # a draft, and hold the recipients asked for.
        again = self.fetch(first.native_id)
        if again is None or again.native_id != first.native_id or not again.draft:
            raise TargetChanged("the server's new draft is not where it said")
        for name in ("to", "cc", "bcc"):
            want = sorted(a.lower() for a in _addresses(getattr(draft, name)))
            have = sorted(a.lower() for a in _addresses(getattr(again, name)))
            if want != have:
                raise TargetChanged(f"the server's new draft has other {name} recipients "
                                    "than asked")
        return again

    def _exact(self, message: Message) -> Message:
        """The message read again by its id, checked here: the same one,
        unchanged since it was read."""
        stored = self.fetch(message.native_id)
        if stored is None:
            raise TargetChanged("the message is gone from the server")
        if stored.native_id != message.native_id \
                or exact.norm(stored.ident) != exact.norm(message.ident):
            raise TargetChanged("the server's message at that id is another one")
        if message.condition is not None and stored.condition != message.condition:
            raise TargetChanged("the message changed on the server since it was read")
        return stored

    def send_draft(self, message: Message) -> Message:
        """Send the one draft the write path checked; then check it is the
        one that left Drafts."""
        stored = self._exact(message)
        if not stored.draft:
            raise TargetChanged("that is not a draft; nothing was sent")
        before = self.neighbours(stored)
        self._call("send_draft", id=stored.native_id)
        still = self.fetch(stored.native_id)
        if still is not None and still.draft:
            # After the send: asked again, a yes would send a second time.
            raise AfterWrite("the server said it sent the draft, and it is still in "
                             "Drafts: another may have gone instead")
        after = self.neighbours(stored)
        if before is not None and after is not None:
            exact.changed_only(before, after, target=stored.ident, gone=True)
        return replace(stored, draft=False, direction="out")

    def delete(self, message: Message) -> None:
        """Throw away one draft, and nothing else."""
        stored = self._exact(message)
        if not stored.draft:
            raise Unsupported("this account throws away drafts only")
        self._call("delete", id=stored.native_id)


class McpCalendar(_Mcp, CalendarProvider):
    """The calendar class through an MCP server, for one account."""

    klass = "calendar"

    def _event(self, item: dict[str, Any]) -> Event | None:
        fields = self.mapping.fields
        native = _text(read(item, fields, "native_id"))
        if not native:
            return None
        all_day = bool(read(item, fields, "all_day"))
        start, end = read(item, fields, "start"), read(item, fields, "end")
        if all_day:
            start = start.date() if isinstance(start, dt.datetime) else start
            end = end.date() if isinstance(end, dt.datetime) else end
        listed = read(item, fields, "guests") or []
        guests = []
        for g in listed:
            g = g if isinstance(g, dict) else {"email": g}
            email = _text(g.get("email")).strip()
            if email and email.lower() != self.address.lower():
                guests.append(Guest(email=email, name=_text(g.get("name")),
                                    response=_text(g.get("response"))))
        updated = moment(read(item, fields, "updated"))
        busy = read(item, fields, "busy")
        return Event(
            native_id=native, ident=_text(read(item, fields, "ident")),
            condition=_text(read(item, fields, "condition")) or None,
            title=_text(read(item, fields, "title")), start=start, end=end, all_day=all_day,
            location=_text(read(item, fields, "location")),
            description=_text(read(item, fields, "description")), guests=tuple(guests),
            organizer=_text(read(item, fields, "organizer")),
            link=_text(read(item, fields, "link")),
            recurring=bool(read(item, fields, "recurring")),
            status="cancelled" if read(item, fields, "cancelled") is True else "",
            busy=True if busy is None else bool(busy),
            extra={"updated": iso(updated) if updated else "",
                   "series": _text(read(item, fields, "series"))})

    def _aware(self, when: dt.datetime | dt.date | None) -> dt.datetime | None:
        if when is None or isinstance(when, dt.datetime):
            return when
        return dt.datetime.combine(when, dt.time(), self.ctx.zone())

    def events(self, start: dt.datetime, end: dt.datetime, *, limit: int) -> list[Event]:
        items, _ = self._list("events", start=iso(start), end=iso(end),
                              top=max(10, min(int(limit), 1000)), select=self.mapping.select)
        out = []
        for item in items:
            event = self._event(item)
            if event is None or event.start is None:
                continue
            first = self._aware(event.start)
            last = self._aware(event.end) or first
            # Checked here, whatever the server answered for the range.
            if first < end and (last > start or (last == first and first >= start)):
                out.append(event)
        out.sort(key=lambda e: self._aware(e.start))
        return out[:limit]

    def fetch(self, native_id: str) -> Event | None:
        item = self._call("fetch", id=_safe(native_id), select=self.mapping.select)
        return self._event(item) if isinstance(item, dict) else None

    def neighbours(self, item: Any) -> dict[str, str | None] | None:
        """Every single event and series, by UID and change key."""
        return self._neighbours("all")

    def test(self) -> str:
        said = self._test()
        now = dt.datetime.now(dt.UTC)
        week = self.events(now, now + dt.timedelta(days=7), limit=500)
        return f"{said}; {len(week)} events this week"

    # --- writing

    def _writable(self, event: Event) -> None:
        if event.recurring:
            raise Unsupported("changing a repeating event from this calendar; change it in "
                              "the calendar's own app")
        if event.guests:
            raise Unsupported("changing an event with guests from this calendar, which would "
                              "tell them; change it in the calendar's own app")

    def _content(self, event: Event, names: tuple[str, ...]) -> dict[str, Any]:
        fields = self.mapping.fields
        out: dict[str, Any] = {}
        for name in names:
            value = getattr(event, name)
            if name in ("start", "end") and value is None:
                continue
            write(out, fields, name, value)
        return out

    def create(self, event: Event) -> Event:
        if event.guests:
            raise Unsupported("inviting guests from this calendar")
        if not self._has("create"):
            raise Unsupported("adding events to this calendar")
        names = ("title", "start", "end", "all_day") + tuple(
            n for n in ("location", "description") if getattr(event, n))
        made = self._call("create", item=self._content(event, names))
        found = self._event(made) if isinstance(made, dict) else None
        if found is None:
            raise ConnectorError("the server did not say which event it made")
        return found

    def _exact(self, event: Event) -> Event:
        """The event read again by its id, checked here: the same one, and
        unchanged since it was read (its change key)."""
        stored = self.fetch(event.native_id)
        if stored is None:
            raise TargetChanged("the event is gone from the server")
        if stored.native_id != event.native_id \
                or exact.norm(stored.ident) != exact.norm(event.ident):
            raise TargetChanged("the server's event at that id is another one")
        if not event.condition or stored.condition != event.condition:
            raise TargetChanged("the event changed on the server since it was read")
        return stored

    def update(self, event: Event) -> Event:
        """Exactly what changed, and only that: the server's tools take no
        change key, so the event is read again just before."""
        self._writable(event)
        stored = self._exact(event)
        self._writable(stored)
        changed = tuple(n for n in ("title", "start", "end", "all_day", "location",
                                    "description") if getattr(event, n) != getattr(stored, n))
        if not changed:
            return stored
        if "start" in changed or "end" in changed or "all_day" in changed:
            changed = tuple(dict.fromkeys(changed + ("start", "end", "all_day")))
        answer = self._call("update", id=stored.native_id, item=self._content(event, changed),
                            condition=stored.condition)
        if answer is None:
            # The server's 404 (`_answer`): it found no such event, and so
            # changed none. Passed over, the event read back as it was and
            # the user was told it moved.
            raise TargetChanged("the server found no such event to change")
        return self.fetch(event.native_id) or event

    def delete(self, event: Event) -> None:
        self._writable(event)
        stored = self._exact(event)
        self._writable(stored)
        self._call("delete", id=stored.native_id, condition=stored.condition)


def engine(ctx: Any, spec: Any) -> McpMail | McpCalendar:
    if spec.klass == "mail":
        return McpMail(ctx, spec)
    if spec.klass == "calendar":
        return McpCalendar(ctx, spec)
    raise ValueError(f"the MCP engine fills mail and calendars, not {spec.klass}")
