"""The broker: a sandboxed module's one way out, and the one piece that parses what it says.

A module in the sandbox has no network, no secrets and none of the user's
data (the walls in this package). Anything it does outside goes through
here, so here is where a module told by a crafted mail to "send the inbox
to collect.example" fails. Each rule is code (06-modules.md, section 3):

- **`http.fetch`** goes through `core/modctx.ModuleHttp`, the same rules a
  built-in's `ctx.http` keeps: only hosts the grants hold, each vetted by
  `core/egress.vet` and connected to at the address vetted; redirects one
  hop at a time, each checked again; no `Host`, `Forwarded`,
  `X-Forwarded-*`, `X-Original-URL` or hop-by-hop header, and no value
  holding a line break. Here it also keeps to HTTPS, to the methods the
  manifest declares for each host (GET and HEAD where it declares none),
  checked at every hop, and to caps on time and rate as well as size.
- **Secrets are filled in, never handed over:** `{{secret:NAME}}` in a
  header becomes the value only where NAME is bound to that header, host
  and port. An answer that echoes a filled secret back has it replaced
  before the module sees it, as the log does; an echo in another encoding
  is not caught, which is why a secret is bound to one host only.
- **`connector.read`** reads the classes the grants hold, by the read
  methods in `READS` alone, with arguments of the kinds it names
  (`steve_sdk/calls.py`, which a module's tests meet too). Core's reader
  (`agent/modreads.py`) screens what it returns as outside text.
- **`log`** is capped, has every filled secret replaced, and is redacted
  before it is kept, whether it comes as a message or on stderr.
- **`act`** proposes an action, a name and its arguments, which core's
  actor turns into a card (`agent/modtools.py`); a session may propose a
  few. Outside `perform`, which runs on a card's yes, a session's grants
  are `Grants.reading()`: GET and HEAD alone, so a module changes nothing
  outside its folder but through a card.
- **`com.call`** is a desktop program's automation (06-modules.md, section
  4), held by `core/modctx.ModuleCom` to the ProgIDs and member paths the
  grants list, the checks a built-in's `ctx.com` keeps. The calls run on
  one thread of the broker's own, as COM requires, and the program's
  objects stay here: the module is handed plain values, and handles good
  only in its session. A member that sends, saves, deletes or runs is
  reached only in `perform`, where the grants act.

**Parsing.** What a module writes is outside input, so step 11 fuzzes
`Broker.handle`. It takes one JSON-RPC 2.0 object per line, with no
batches, and only the parameters each method names, of the types it
names; anything else is answered with an error, never guessed at. A line
over `MAX_LINE`, or an answer to a call core did not make, ends the
module (`Broken`): a module that breaks the framing is not read further.

**Both ways.** Core calls the module (a tool, `collect`, `perform`) with
`Session.call`, and while it waits it answers the module's own calls. The
module's end is `steve_sdk/link.py`, whose error codes this shares.

**Core's own use.** A remote MCP server is a module with no code, so
nothing runs in the sandbox, yet its requests keep the rules a module's
do: core's client speaks through `Local`, whose every request is a line
this broker parses and answers as it would one from the sandbox. Only
there may plain HTTP reach a server the user runs on this computer
(`plain_loopback`); no module's own code is given that.
"""

from __future__ import annotations

import base64
import binascii
import collections
import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import itertools
import json
import logging
import queue
import re
import threading
import time
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from core import credstore, egress
from core.modctx import (
    MAX_LOG,
    TIMEOUT_S,
    Guarded,
    Hosts,
    ModuleCom,
    ModuleHttp,
    Network,
    Refused,
    hosts_for,
)
from core.sandbox import launch
from steve_sdk import calls
from steve_sdk.calls import MAX_ACTS, READS
from steve_sdk.context import Account
from steve_sdk.errors import ConnectorError, Transient
from steve_sdk.link import (
    BAD_PARAMS,
    INTERNAL,
    INVALID,
    NO_METHOD,
    PARSE,
    Broken,
    SandboxHttp,
    error_for,
    error_of,
    line,
)
from steve_sdk.manifest import METHODS, READ_ONLY, Manifest, SecretBinding

log = logging.getLogger(__name__)

MAX_LINE = 16 << 20         # bytes in one message from the module
MAX_UPLOAD = 10 << 20       # bytes in one request's body
MAX_URL = 8192
MAX_HEADERS = 64
MAX_HEADER = 8192           # characters in one header's value
MAX_LOG_LINES = 500         # lines kept from one session; the rest are counted
MIN_TIMEOUT_S = 1.0
RATE = (120, 60.0)          # requests, in seconds: fetches and reads together
MAX_HANDLES = 10_000        # a program's objects, and walks, one session holds at once
MAX_PAGE = 200              # a collection's items in one answer
MAX_ARGS = 16
COM_TIMEOUT_S = 30.0
MEMBER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
# Each step `com.call` takes, and its parameters.
COM_OPS = {"open": {"progid"}, "get": {"handle", "name"}, "set": {"handle", "name", "value"},
           "call": {"handle", "args"}, "items": {"handle", "cursor", "limit"},
           "release": {"handles"}}
_END = object()

# `reader(klass, method, args, account)`: the items, as plain JSON.
Reader = Callable[[str, str, dict[str, Any], str | None], Any]
# Core's answer to a module's proposed action (a name and its arguments):
# what the module is told of the card it became.
Actor = Callable[[str, dict[str, Any]], Any]


class Invalid(ValueError):
    """Parameters that are not what the method takes."""


@dataclass(frozen=True)
class Grants:
    """What one module may reach from the sandbox: hosts, the methods for
    each, the secrets bound to them, and the classes it may read."""

    hosts: Hosts
    methods: Callable[[str], tuple[str, ...]]
    bindings: dict[str, SecretBinding]
    secrets: tuple[str, ...]
    reads: tuple[str, ...] = ()
    progids: tuple[str, ...] = ()
    com_members: tuple[str, ...] = ()
    com_actions: tuple[str, ...] = ()
    # Whether a program's member that sends, saves, deletes or runs is reached.
    acts: bool = True

    def reading(self) -> Grants:
        """These grants with GET and HEAD alone, and no program's action: a
        tool's call, which may look but not change, leaving the rest to its
        cards."""
        declared = self.methods
        return dataclasses.replace(
            self, methods=lambda host: tuple(m for m in declared(host) if m in READ_ONLY),
            acts=False)


def grants_for(manifest: Manifest, account: Account | None = None) -> Grants:
    """The grants `manifest` declares, for one of its accounts or, for a
    module with none (a tool's), its fixed hosts alone. The broker is
    handed grants, never a manifest, so what reaches it can be narrowed."""
    hosts = (hosts_for(manifest, account) if account is not None
             else Hosts(fixed=manifest.fixed_hosts(None), typed={}))
    # This computer is for core's own engines (a mail bridge, an MCP
    # server), which run in the backend. From the sandbox it would reach
    # whatever else listens here, the personal edition among them.
    typed = {h: k for h, k in hosts.typed.items() if k != egress.LOOPBACK}
    return Grants(hosts=Hosts(fixed=hosts.fixed, typed=typed), methods=manifest.methods_for,
                  bindings={f.name: f.bind for f in manifest.setup.fields
                            if f.bind is not None},
                  secrets=manifest.secrets, reads=manifest.reads, progids=manifest.com,
                  com_members=manifest.com_members, com_actions=manifest.com_actions)


def stored(module_key: str, account_key: str) -> Callable[[str], str | None]:
    """An account's secrets, read from `steve-modules` by name."""
    return lambda name: credstore.read(credstore.target(module_key, account_key, name))


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def _reply(ident: Any, result: Any) -> bytes:
    return line({"jsonrpc": "2.0", "id": ident, "result": result})


def _error(ident: Any, code: int, message: str, data: dict | None = None) -> bytes:
    error = {"code": code, "message": message[:MAX_LOG]}
    if data:
        error["data"] = data
    return line({"jsonrpc": "2.0", "id": ident, "error": error})


def _only(params: dict, allowed: set[str]) -> None:
    if extra := sorted(set(params) - allowed):
        raise Invalid(f"no parameter {extra[0]!r} here")


def _text(params: dict, name: str, *, limit: int, required: bool = False) -> str | None:
    value = params.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise Invalid(f"{name}: text of 1 to {limit:,} characters")
    return value


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


class _ComThread:
    """One broker's COM calls, on a thread of their own: a program's objects
    belong to the thread that made them. A daemon, so that a call stuck
    behind a question the program put on the desktop holds neither the
    worker's exit nor another session, which has a thread of its own."""

    def __init__(self, name: str) -> None:
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        threading.Thread(target=self._run, name=f"com-{name}", daemon=True).start()

    def _run(self) -> None:
        while (job := self._jobs.get()) is not None:
            work, future = job
            try:
                future.set_result(work())
            except Exception as exc:  # noqa: BLE001 - raised again by the caller
                if isinstance(exc, PermissionError | ConnectorError | ValueError):
                    # Without its frames, which hold the program's objects:
                    # those are let go here, on the thread they belong to.
                    exc.__context__ = None
                    exc = exc.with_traceback(None)
                future.set_exception(exc)

    def run(self, work: Callable[[], Any], timeout: float) -> Any:
        """`work`'s answer, from this thread; `TimeoutError` past `timeout`."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((work, future))
        return future.result(timeout)

    def stop(self, last: Callable[[], Any]) -> None:
        """`last`, and then the thread ends, without waiting for either."""
        self._jobs.put((last, concurrent.futures.Future()))
        self._jobs.put(None)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class Broker:
    """One module's calls, answered under its grants."""

    def __init__(self, *, module: str, grants: Grants, secret: Callable[[str], str | None],
                 network: Network, redact: Callable[[str], str],
                 keep: Callable[[str], None] | None = None, reader: Reader | None = None,
                 actor: Actor | None = None, rate: tuple[int, float] = RATE,
                 clock: Callable[[], float] = time.monotonic,
                 plain_loopback: bool = False,
                 dispatch: Callable[[str], Any] | None = None) -> None:
        self.module = module
        self._grants = grants
        self._secret = secret
        self._redact = redact
        self._keep = keep or logging.getLogger(f"steve.module.{module}").info
        self._reader = reader
        self._actor = actor
        self._acts = 0
        self._rate, self._window = rate
        self._clock = clock
        self._spent: collections.deque[float] = collections.deque()
        self._said: set[str] = set()
        self._lines = 0
        self._log_lock = threading.Lock()
        self.http = ModuleHttp(hosts=grants.hosts, bindings=grants.bindings,
                               secret=self._fill, network=network, methods=grants.methods,
                               https_only=not plain_loopback)
        self.com = ModuleCom(grants.progids, grants.com_members, actions=grants.com_actions,
                             acts=grants.acts, dispatch=dispatch)
        self._com_thread: _ComThread | None = None
        self._held: dict[int, Any] = {}
        self._handles = itertools.count(1)
        self._stuck = ""
        self._methods: dict[str, Callable[[dict], Any]] = {
            "http.fetch": self._fetch, "connector.read": self._read, "log": self._log,
            "act": self._act, "com.call": self._com_call}

    # --- the protocol --------------------------------------------------------

    def handle(self, raw: bytes) -> bytes | None:
        """The answer to one line from the module, or None for a
        notification. Raises nothing for anything the module can send."""
        try:
            message = json.loads(raw, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            return _error(None, PARSE, "not a line of JSON")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID, "one JSON-RPC 2.0 object per line, and no batches")
        notification = "id" not in message
        ident = message.get("id")
        if not notification and not (
                (isinstance(ident, int) and not isinstance(ident, bool))
                or (isinstance(ident, str) and len(ident) <= 64)):
            return _error(None, INVALID, "an id is an integer or a short string")
        method, params = message.get("method"), message.get("params", {})
        if (not isinstance(method, str) or not isinstance(params, dict)
                or set(message) - {"jsonrpc", "id", "method", "params"}):
            return None if notification else _error(ident, INVALID, "not a call")
        handler = self._methods.get(method)
        if handler is None:
            return None if notification else _error(ident, NO_METHOD,
                                                    f"no method {method[:60]!r}")
        try:
            result = handler(params)
        except (Invalid, httpx.InvalidURL) as exc:
            reply = _error(ident, BAD_PARAMS, str(exc))
        except (PermissionError, ConnectorError) as exc:
            code, text, data = error_for(exc)
            reply = _error(ident, code, self._scrub(text), data)
        except httpx.HTTPError as exc:
            reply = _error(ident, *error_for(ConnectorError(f"the answer could not be read: "
                                                            f"{type(exc).__name__}")))
        except Exception:  # noqa: BLE001 - a bug here is the broker's, never the module's to read
            log.exception("the broker failed on %s from %s", method, self.module)
            reply = _error(ident, INTERNAL, "the broker failed")
        else:
            reply = _reply(ident, result)
        return None if notification else reply

    # --- the methods ---------------------------------------------------------

    def _spend(self) -> None:
        now = self._clock()
        while self._spent and now - self._spent[0] >= self._window:
            self._spent.popleft()
        if len(self._spent) >= self._rate:
            raise Transient(f"more than {self._rate} requests in {self._window:.0f} seconds; "
                            f"wait")
        self._spent.append(now)

    def _fill(self, name: str) -> str | None:
        if name not in self._grants.secrets:
            raise Refused(f"{self.module} declares no secret named {name!r}")
        value = self._secret(name)
        if value:
            self._said.add(value)
        return value

    def _scrub(self, text: str) -> str:
        for value in self._said:
            text = text.replace(value, "[secret]")
        return text

    def _fetch(self, params: dict) -> dict:
        _only(params, {"method", "url", "headers", "content", "timeout", "follow"})
        method = (_text(params, "method", limit=10) or "GET").upper()
        if method not in METHODS:
            raise Invalid(f"method: one of {', '.join(METHODS)}")
        url = _text(params, "url", limit=MAX_URL, required=True)
        headers = params.get("headers") or {}
        if not isinstance(headers, dict) or len(headers) > MAX_HEADERS:
            raise Invalid(f"headers: an object of at most {MAX_HEADERS}")
        for name, value in headers.items():
            if not isinstance(value, str) or len(value) > MAX_HEADER or not value.isascii():
                raise Invalid(f"headers: {name[:60]!r} is not ASCII text of at most "
                              f"{MAX_HEADER:,} characters")
        content = params.get("content")
        if content is not None:
            if not isinstance(content, str) or len(content) > MAX_UPLOAD * 4 // 3 + 4:
                raise Invalid(f"content: base64 of at most {MAX_UPLOAD:,} bytes")
            try:
                content = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                raise Invalid("content: not base64") from None
        timeout = params.get("timeout")
        if timeout is not None and not _number(timeout):
            raise Invalid("timeout: a number of seconds")
        timeout = min(max(float(timeout or TIMEOUT_S), MIN_TIMEOUT_S), TIMEOUT_S)
        follow = params.get("follow", False)
        if not isinstance(follow, bool):
            raise Invalid("follow: true or false")
        ModuleHttp.check_headers(headers)
        self._spend()
        answer = self.http.request(method, url, headers=headers, content=content,
                                   timeout=timeout, follow=follow)
        body = answer.content
        for value in self._said:
            body = body.replace(value.encode(), b"[secret]")
        return {"status": answer.status, "url": answer.url,
                "headers": {k: self._scrub(v) for k, v in answer.headers.items()},
                "content": base64.b64encode(body).decode()}

    def _read(self, params: dict) -> Any:
        _only(params, {"class", "method", "args", "account"})
        klass = _text(params, "class", limit=20, required=True)
        if klass not in self._grants.reads:
            raise Refused(f"{self.module} may not read {klass[:20]}")
        method = _text(params, "method", limit=40, required=True)
        kinds = READS.get(klass, {}).get(method)
        if kinds is None:
            raise Refused(f"{klass}.{method[:40]} is not a read a module may make")
        args = params.get("args") or {}
        if not isinstance(args, dict):
            raise Invalid("args: an object")
        try:
            typed = calls.arguments(f"{klass}.{method}", kinds, args)
        except ValueError as exc:
            raise Invalid(str(exc)) from None
        account = _text(params, "account", limit=100)
        if self._reader is None:
            raise Refused(f"{self.module} was started with no reader")
        self._spend()
        return self._reader(klass, method, typed, account)

    def _act(self, params: dict) -> Any:
        _only(params, {"action", "args"})
        action = _text(params, "action", limit=40, required=True)
        args = params.get("args")
        try:
            calls.proposal(action, args)
        except ValueError as exc:
            raise Invalid(str(exc)) from None
        if self._actor is None:
            raise Refused(f"{self.module} may not propose an action here")
        if self._acts >= MAX_ACTS:
            raise Refused(f"{self.module} proposed {MAX_ACTS} actions in one call already")
        self._acts += 1
        return self._actor(action, args)

    # --- COM ---------------------------------------------------------------

    def _com_call(self, params: dict) -> Any:
        op = params.get("op")
        if not isinstance(op, str) or op not in COM_OPS:
            raise Invalid(f"op: one of {', '.join(COM_OPS)}")
        _only(params, COM_OPS[op] | {"op"})
        if self._stuck:
            raise Transient(self._stuck)
        if self._com_thread is None:
            self._com_thread = _ComThread(self.module)
        try:
            return self._com_thread.run(lambda: self._com_step(op, params), COM_TIMEOUT_S)
        except TimeoutError:
            # The thread is still in that call, and every next one would
            # wait behind it: the rest of the session is refused at once.
            self._stuck = (f"the program did not answer in {COM_TIMEOUT_S:.0f} seconds; it "
                           f"may be showing a question on the desktop, which only the user "
                           f"can answer")
            raise Transient(self._stuck) from None

    def _com_step(self, op: str, params: dict) -> Any:
        """One step on the COM thread: each object it reaches is kept here,
        and what it answers is plain."""
        if op == "open":
            progid = _text(params, "progid", limit=120, required=True)
            return self._hand(self._program(lambda: self.com.open(progid)))
        if op == "release":
            handles = params.get("handles")
            if not isinstance(handles, list) or len(handles) > MAX_PAGE:
                raise Invalid(f"handles: a list of at most {MAX_PAGE}")
            for handle in handles:
                if _whole(handle):
                    self._held.pop(handle, None)
            return None
        if op == "items":
            return self._items(params)
        held = self._object(params.get("handle"))
        if op == "call":
            args = params.get("args")
            if not isinstance(args, list) or len(args) > MAX_ARGS:
                raise Invalid(f"args: a list of at most {MAX_ARGS}")
            plain = [self._arg(a) for a in args]
            return self._hand(self._program(lambda: held(*plain)))
        name = params.get("name")
        if not isinstance(name, str) or not MEMBER.fullmatch(name):
            raise Invalid("name: a member's name, [A-Za-z][A-Za-z0-9_]{0,63}")
        if op == "get":
            return self._hand(self._program(lambda: getattr(held, name)))
        value = self._arg(params.get("value"))
        self._program(lambda: setattr(held, name, value))
        return None

    def _items(self, params: dict) -> dict:
        """A page of a collection's items, and the cursor to the rest, or None."""
        limit = params.get("limit", MAX_PAGE)
        if not _whole(limit) or not 1 <= limit <= MAX_PAGE:
            raise Invalid(f"limit: 1 to {MAX_PAGE}")
        if "cursor" in params:
            if "handle" in params:
                raise Invalid("a handle or a cursor, not both")
            cursor = params["cursor"]
            walk = self._held.get(cursor) if _whole(cursor) else None
            if not isinstance(walk, types.GeneratorType):
                raise Invalid("cursor: no such walk in this session")
        else:
            walk = iter(self._object(params.get("handle")))
            cursor = self._hold(walk)
        items: list[Any] = []
        while len(items) < limit:
            item = self._program(lambda: next(walk, _END))
            if item is _END:
                self._held.pop(cursor, None)
                return {"items": items, "cursor": None}
            items.append(self._hand(item))
        return {"items": items, "cursor": cursor}

    def _program(self, work: Callable[[], Any]) -> Any:
        """`work`, with what the program raised said as a ConnectorError: a
        member it lacks, arguments it refused, or its own error."""
        try:
            return work()
        except (PermissionError, ConnectorError, Invalid):
            raise
        except Exception as exc:  # noqa: BLE001 - the program's, said to the module
            raise ConnectorError(f"the program refused: {type(exc).__name__}: {exc}"
                                 [:MAX_LOG]) from None

    def _hold(self, thing: Any) -> int:
        if len(self._held) >= MAX_HANDLES:
            raise Refused(f"{self.module} holds {MAX_HANDLES:,} of the program's objects "
                          f"already; let some go")
        handle = next(self._handles)
        self._held[handle] = thing
        return handle

    def _hand(self, value: Any) -> Any:
        """`value` as the module is handed it: one of the program's objects
        as a handle to it, kept here; a time, a day or bytes tagged; the
        rest as it is."""
        if isinstance(value, Guarded):
            return {"handle": self._hold(value)}
        if isinstance(value, dt.datetime):
            return {"datetime": value.isoformat()}
        if isinstance(value, dt.date):
            return {"date": value.isoformat()}
        if isinstance(value, bytes):
            return {"bytes": base64.b64encode(value).decode()}
        return value

    def _object(self, handle: Any) -> Guarded:
        held = self._held.get(handle) if _whole(handle) else None
        if not isinstance(held, Guarded):
            raise Invalid("handle: no such object in this session; a handle is good only in "
                          "the session that was handed it, until it is let go")
        return held

    def _arg(self, value: Any) -> Any:
        """What the module passed, as the program takes it."""
        if value is None or isinstance(value, bool | int | float | str):
            return value
        if isinstance(value, dict) and len(value) == 1:
            ((kind, raw),) = value.items()
            if kind == "handle":
                return self._object(raw)
            if kind in ("datetime", "date") and isinstance(raw, str) and len(raw) <= 40:
                with contextlib.suppress(ValueError):
                    return (dt.datetime if kind == "datetime" else dt.date).fromisoformat(raw)
            if kind == "bytes" and isinstance(raw, str):
                with contextlib.suppress(binascii.Error, ValueError):
                    return base64.b64decode(raw, validate=True)
        raise Invalid("an argument is text, a number, true, false or null, or one of "
                      "{handle}, {datetime}, {date} and {bytes}")

    def _log(self, params: dict) -> None:
        _only(params, {"text"})
        text = params.get("text")
        if not isinstance(text, str):
            raise Invalid("text: text")
        self.note(text)

    def note(self, text: str) -> None:
        """One line of the module's log, kept: every filled secret replaced
        before it is cut, so no part of one survives the cut, then redacted."""
        with self._log_lock:
            self._lines += 1
            if self._lines > MAX_LOG_LINES:
                if self._lines == MAX_LOG_LINES + 1:
                    self._keep(f"(the log was cut at {MAX_LOG_LINES} lines)")
                return
        self._keep(self._redact(self._scrub(text)[:MAX_LOG]))

    def close(self) -> None:
        self.http.close()
        if self._com_thread is not None:
            # The program's objects are let go on the thread they belong to.
            self._com_thread.stop(self._held.clear)


class Local:
    """Core's own requests on a broker's paths, for a module with no code
    to run: each is a line the broker parses and answers as it would one
    from the sandbox. `http` is `ctx.http` as the sandbox has it."""

    def __init__(self, broker: Broker) -> None:
        self._broker = broker
        self._ids = itertools.count(1)
        self.http = SandboxHttp(self)

    def call(self, method: str, params: dict) -> Any:
        ident = next(self._ids)
        answer = self._broker.handle(line({"jsonrpc": "2.0", "id": ident, "method": method,
                                           "params": params}))
        reply = json.loads(answer or b"{}")
        if isinstance(reply.get("error"), dict):
            error = reply["error"]
            raised = error_of(error.get("code"), str(error.get("message", "")),
                              error.get("data"))
            # As `ctx.http` raises it, so a client written for that reads it.
            raise Refused(str(raised)) if type(raised) is PermissionError else raised
        return reply.get("result")

    def close(self) -> None:
        self._broker.close()


class Session:
    """A module in the sandbox, spoken to over its pipes."""

    def __init__(self, process: launch.Process, broker: Broker) -> None:
        self._process = process
        self._broker = broker
        self._next = 0
        self._stderr = threading.Thread(target=self._drain, name=f"log-{broker.module}",
                                        daemon=True)
        self._stderr.start()

    def _drain(self) -> None:
        stream = self._process.stderr
        with contextlib.suppress(OSError, ValueError):
            while raw := stream.readline(MAX_LOG * 4):
                if raw.strip():
                    self._broker.note(raw.decode("utf-8", "replace").rstrip("\r\n"))

    def _send(self, raw: bytes) -> None:
        try:
            self._process.stdin.write(raw)
        except OSError as exc:
            raise Broken(f"{self._broker.module} closed its end: {exc}") from None

    def _line(self) -> bytes | None:
        raw = self._process.stdout.readline(MAX_LINE + 1)
        if not raw:
            return None
        if len(raw) > MAX_LINE:
            self._process.kill()
            raise Broken(f"{self._broker.module} wrote a line over {MAX_LINE:,} bytes")
        return raw

    def _answer(self, raw: bytes) -> None:
        reply = self._broker.handle(raw)
        if reply is not None:
            self._send(reply)

    def serve(self) -> None:
        """Answer the module's calls until it closes its end."""
        while (raw := self._line()) is not None:
            self._answer(raw)

    def call(self, method: str, params: dict) -> Any:
        """Call the module, answering its calls until it answers this one;
        its error raised as the SDK's (`steve_sdk.link.error_of`)."""
        self._next += 1
        ident = self._next
        self._send(line({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}))
        while (raw := self._line()) is not None:
            try:
                message = json.loads(raw, parse_constant=_no_constant)
            except (ValueError, RecursionError):
                message = None
            if not (isinstance(message, dict) and "method" not in message
                    and ("result" in message or "error" in message)):
                self._answer(raw)
                continue
            if message.get("id") != ident:
                self._process.kill()
                raise Broken(f"{self._broker.module} answered a call core did not make")
            error = message.get("error")
            if error is not None:
                error = error if isinstance(error, dict) else {}
                code = error.get("code") if isinstance(error.get("code"), int) else INTERNAL
                raise error_of(code, str(error.get("message", ""))[:MAX_LOG],
                               error.get("data"))
            return message.get("result")
        raise Broken(f"{self._broker.module} ended before it answered {method}")

    def close(self) -> None:
        """End the module, keep the rest of its log, and let go of both."""
        # Ended first, so its stderr ends and the log is read to the end
        # before the pipes are closed under the thread reading it.
        self._process.kill()
        self._stderr.join(5)
        self._process.close()
        self._broker.close()
