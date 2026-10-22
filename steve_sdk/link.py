"""A module's end of the broker: how code in the sandbox asks Steve for anything.

In the sandbox a module has no network, no secrets and none of the user's
data (`core/sandbox/`). Its one way out is the broker, at the other end of
its stdin and stdout (`core/sandbox/broker.py`). A module written against
`ctx.http` in its tests would not run there if the sandbox handed it a
different interface, and one that spoke the protocol itself could get a
frame wrong and be ended for it. So this end speaks it for the module:
`ctx.http.request` becomes `http.fetch`, `ctx.log` a log line, `ctx.read`
a `connector.read`, and each error the broker answers with becomes the
SDK's own again, so the module's code reads the same in either place.

**stdout is the pipe.** `connect` takes it over and points the module's
own `print` at stderr, which is its log: a stray print would otherwise be
read as a message, and the broker ends a module that breaks the protocol.

**A secret is never handed over** in the sandbox: `ctx.secret` refuses,
and a header names one as `{{secret:NAME}}` for the broker to fill.

The protocol is JSON-RPC 2.0, one object per line in UTF-8, with no
batches. Each side may call the other, at any time. While core waits for
an answer it answers the module's calls; while the module waits, a call
from core is held until the module serves, since a module's code is not
written to be entered twice.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import datetime as dt
import json as _json
import os
import sys
import zoneinfo
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlencode, urlsplit, urlunsplit

from steve_sdk.context import Account, Response
from steve_sdk.errors import (
    AfterWrite,
    ConnectorError,
    NeedsCard,
    NotConnected,
    SignInNeeded,
    TargetChanged,
    Transient,
    Unsupported,
    WrongPassword,
)

# JSON-RPC 2.0's own error codes, and two for the SDK's errors: a refusal
# under the module's rules (PermissionError), and a ConnectorError whose
# `kind` the error's data names.
PARSE = -32700
INVALID = -32600
NO_METHOD = -32601
BAD_PARAMS = -32602
INTERNAL = -32603
REFUSED = -32001
FAILED = -32002

_KINDS = {c.kind: c for c in (ConnectorError, SignInNeeded, WrongPassword, NotConnected,
                              Transient, Unsupported, TargetChanged, NeedsCard, AfterWrite)}


class Broken(Exception):
    """The other end broke the protocol, or closed its end."""


def error_of(code: int, message: str, data: Any = None) -> Exception:
    """The exception a JSON-RPC error stands for, raised on the side that
    made the call."""
    if code == REFUSED:
        return PermissionError(message)
    if code == FAILED:
        kind = data.get("kind") if isinstance(data, dict) else None
        return _KINDS.get(kind, ConnectorError)(message)
    if code in (BAD_PARAMS, INVALID):
        return ValueError(message)
    if code == NO_METHOD:
        return NotImplementedError(message)
    return ConnectorError(message)


def error_for(exc: BaseException) -> tuple[int, str, dict | None]:
    """The JSON-RPC error that says `exc` to the side that made the call."""
    if isinstance(exc, PermissionError):
        return REFUSED, str(exc), None
    if isinstance(exc, ConnectorError):
        return FAILED, str(exc), {"kind": exc.kind}
    if isinstance(exc, ValueError):
        return BAD_PARAMS, str(exc), None
    return INTERNAL, f"{type(exc).__name__}: {exc}", None


def line(message: dict) -> bytes:
    return _json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


class Link:
    """One end of the pipe: calls, notifications, and answers to calls."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self._reader = reader
        self._writer = writer
        self._next = 0
        self._held: collections.deque[dict] = collections.deque()

    def _send(self, message: dict) -> None:
        try:
            self._writer.write(line(message))
            self._writer.flush()
        except (OSError, ValueError) as exc:
            raise Broken(f"the other end closed: {exc}") from None

    def _receive(self) -> dict | None:
        raw = self._reader.readline()
        if not raw:
            return None
        try:
            message = _json.loads(raw)
        except ValueError:
            raise Broken("a line that is not JSON") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise Broken("a line that is not a JSON-RPC 2.0 object")
        return message

    def call(self, method: str, params: dict) -> Any:
        """Ask the other end, and wait for its answer."""
        self._next += 1
        ident = self._next
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        while (message := self._receive()) is not None and "method" in message:
            self._held.append(message)
        if message is None:
            raise Broken("the other end closed before it answered")
        if message.get("id") != ident:
            raise Broken("an answer to a call this end did not make")
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            raise error_of(error.get("code", INTERNAL), str(error.get("message", "")),
                           error.get("data"))
        return message.get("result")

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def serve(self, handlers: dict[str, Callable[[dict], Any]]) -> None:
        """Answer the other end's calls until it closes its end."""
        while (message := self._held.popleft() if self._held else self._receive()) is not None:
            if "method" not in message:
                raise Broken("an answer to a call this end did not make")
            ident = message.get("id")
            handler = handlers.get(message["method"])
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if handler is None:
                reply = {"error": {"code": NO_METHOD, "message": message["method"]}}
            else:
                try:
                    reply = {"result": handler(params)}
                except Exception as exc:  # noqa: BLE001 - the caller is told, as an error
                    code, text, data = error_for(exc)
                    reply = {"error": {"code": code, "message": text,
                                       **({"data": data} if data else {})}}
            if "id" in message:
                self._send({"jsonrpc": "2.0", "id": ident, **reply})


def connect() -> Link:
    """The module's end of the pipe, from inside the sandbox. Takes stdout
    over and points `print` at stderr, the module's log."""
    out = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    return Link(sys.stdin.buffer, out)


def _with_params(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = "&".join(q for q in (parts.query, urlencode(params)) if q)
    return urlunsplit(parts._replace(query=query))


class SandboxHttp:
    """`ctx.http` in the sandbox: each request is the broker's `http.fetch`."""

    def __init__(self, link: Link) -> None:
        self._link = link

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                content: bytes | str | None = None, json: Any = None,
                params: dict[str, str] | None = None, timeout: float | None = None,
                follow: bool = False) -> Response:
        sending = dict(headers or {})
        if json is not None:
            content = _json.dumps(json).encode()
            if not any(k.lower() == "content-type" for k in sending):
                sending["Content-Type"] = "application/json"
        if isinstance(content, str):
            content = content.encode()
        got = self._link.call("http.fetch", {
            "method": method, "url": _with_params(url, params) if params else url,
            "headers": sending,
            "content": base64.b64encode(content).decode() if content else None,
            "timeout": timeout, "follow": follow})
        return Response(status=got["status"], url=got["url"], headers=dict(got["headers"]),
                        content=base64.b64decode(got["content"]))


PAGE = 100          # a collection's items asked for at a time
LET_GO = 100        # handles let go together


class SandboxCom:
    """`ctx.com` in the sandbox: each step on a program's object is the
    broker's `com.call`, which keeps the object and checks every member
    against the manifest. An object is a handle here (`ComObject`), let go
    there, a batch at a time, once nothing here holds it."""

    def __init__(self, link: Link) -> None:
        self._link = link
        self._gone: collections.deque[int] = collections.deque()

    def open(self, progid: str) -> Any:
        """The root object of a ProgID the manifest lists."""
        return self.call({"op": "open", "progid": progid})

    def call(self, params: dict) -> Any:
        if len(self._gone) >= LET_GO:
            gone = [self._gone.popleft() for _ in range(LET_GO)]
            self._link.call("com.call", {"op": "release", "handles": gone})
        return self.value(self._link.call("com.call", params))

    def value(self, value: Any) -> Any:
        """What the broker handed back, as the program gave it."""
        if isinstance(value, dict) and len(value) == 1:
            ((kind, raw),) = value.items()
            if kind == "handle":
                return ComObject(self, raw)
            if kind == "datetime":
                return dt.datetime.fromisoformat(raw)
            if kind == "date":
                return dt.date.fromisoformat(raw)
            if kind == "bytes":
                return base64.b64decode(raw)
        return value

    def let_go(self, handle: int) -> None:
        self._gone.append(handle)


def _com_arg(value: Any) -> Any:
    if isinstance(value, ComObject):
        return {"handle": object.__getattribute__(value, "_handle")}
    if isinstance(value, dt.datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, dt.date):
        return {"date": value.isoformat()}
    if isinstance(value, bytes | bytearray):
        return {"bytes": base64.b64encode(value).decode()}
    return value


class ComObject:
    """One of a program's objects, kept by the broker: reading a member,
    setting one, calling one and walking a collection are each a
    `com.call`, checked there."""

    __slots__ = ("_com", "_handle")

    def __init__(self, com: SandboxCom, handle: int) -> None:
        object.__setattr__(self, "_com", com)
        object.__setattr__(self, "_handle", handle)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._com.call({"op": "get", "handle": self._handle, "name": name})

    def __setattr__(self, name: str, value: Any) -> None:
        self._com.call({"op": "set", "handle": self._handle, "name": name,
                        "value": _com_arg(value)})

    def __call__(self, *args: Any) -> Any:
        return self._com.call({"op": "call", "handle": self._handle,
                               "args": [_com_arg(a) for a in args]})

    def __iter__(self) -> Iterator[Any]:
        page = self._com.call({"op": "items", "handle": self._handle, "limit": PAGE})
        try:
            while True:
                for item in page["items"]:
                    yield self._com.value(item)
                if page["cursor"] is None:
                    return
                page = self._com.call({"op": "items", "cursor": page["cursor"],
                                       "limit": PAGE})
        finally:
            # A walk left before its end is let go as an object is.
            if page["cursor"] is not None:
                self._com.let_go(page["cursor"])

    def __del__(self) -> None:
        with contextlib.suppress(Exception):  # at exit, nothing is left to tell
            self._com.let_go(self._handle)


class SandboxContext:
    """`steve_sdk.context.Context` for a module in the sandbox, plus `read`
    and `act`."""

    def __init__(self, link: Link, *, account: Account, state_dir: Path, zone: str) -> None:
        self._link = link
        self.account = account
        self.state_dir = state_dir
        self.http = SandboxHttp(link)
        self.com = SandboxCom(link)
        self._zone = zone

    @property
    def zone(self) -> dt.tzinfo:
        return zoneinfo.ZoneInfo(self._zone)

    def secret(self, name: str) -> str | None:
        raise PermissionError("in the sandbox a module never holds a secret: put "
                              f"{{{{secret:{name}}}}} in the header it is bound to")

    def log(self, text: str) -> None:
        self._link.notify("log", {"text": str(text)})

    def read(self, klass: str, method: str, *, account: str | None = None,
             **args: Any) -> Any:
        """Items from the user's mail, calendar, files or tasks, as the
        manifest's `reads` declares: plain dicts, each with its `id` and
        `account`, screened as outside text. An item the screen did not pass
        keeps only what its sender did not write, with `screened` saying
        so and `text` holding what the screen allows."""
        sent = {k: v.isoformat() if isinstance(v, dt.datetime) else v for k, v in args.items()}
        return self._link.call("connector.read", {
            "class": klass, "method": method, "args": sent,
            **({"account": account} if account else {})})

    def act(self, action: str, args: dict[str, Any]) -> dict:
        """Propose an action: the user is shown `action` and `args` on a
        card, and on their yes core calls the module's `perform(ctx, action,
        args)` with exactly those. What comes back says the card is
        waiting; nothing is done yet. Outside `perform` a module's requests
        are GET and HEAD alone, so this is its one way to change anything."""
        return self._link.call("act", {"action": action, "args": args})
