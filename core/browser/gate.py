"""The sandboxed browser's only way out: a proxy that applies `core/egress`
to every connection it makes.

The browser runs on a Docker network of its own with no route anywhere --
not the internet, not this laptop, not their router (`core/browser/sandbox.py`).
This runs in a second, small container on that network and on Docker's
bridge, and is the browser's proxy. Whatever a page does -- a script, an
image tag, a form, a redirect -- reaches the world through here or not at
all, so this is where "a page cannot reach the supervisor, the router or
anything else on their network" is made true for the browser, as
`core/egress.refusal` makes it true for `read_page`.

For each connection: ports 80 and 443 only; the name checked by
`egress.addresses` (no bare address, no local name, every address it
resolves to public); and then a connection to *an address that check
returned*, never to the name again, so a name that resolves somewhere
public for the check and somewhere private for the connection has no second
lookup to win. Bytes sent upstream are counted for the session and capped:
nothing of theirs is in the sandbox to send, and the cap makes sure nothing in
bulk could be. Every connection is logged as one JSON line on stdout --
host, port, verdict, bytes -- which is the record of where a session went.

Plain-HTTP requests are forwarded one per client connection
(`Connection: close`), so a kept-alive connection cannot carry a second
request to a host that was never checked.

Standard library only: it runs in its own image beside a copy of
`egress.py`, and on the host in the gate tests, with the resolver and the
connector handed in. Frozen, with the rest of `core/`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

try:
    from core import egress
except ImportError:  # inside the gate image, egress.py sits beside this file
    import egress  # type: ignore[no-redef]

PORT = 3128
ALLOWED_PORTS = frozenset({80, 443})
UPSTREAM_CAP_BYTES = 16 * 1024 * 1024
HEAD_LIMIT = 16 * 1024
HEAD_TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 10.0
CHUNK = 64 * 1024
# Hop-by-hop headers dropped from a forwarded plain-HTTP request, and the
# one added in their place.
_HOP = (b"connection", b"proxy-connection", b"keep-alive", b"proxy-authorization", b"te",
        b"trailer", b"upgrade")

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


def _print(entry: dict) -> None:
    print(json.dumps(entry, sort_keys=True), flush=True)


async def _open(ip: str, port: int):
    return await asyncio.open_connection(ip, port)


@dataclass
class Gate:
    """One session's gate: its checks, its count of bytes sent, its log."""

    resolve: Callable[[str, int], list[str] | str] = egress.addresses
    connect: Connector = _open
    cap: int = UPSTREAM_CAP_BYTES
    log: Callable[[dict], None] = _print
    sent: int = 0
    _open_tunnels: set = field(default_factory=set)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError,
                ConnectionError):
            await _close(writer)
            return
        request_line, _, headers = head.partition(b"\r\n")
        try:
            method, target, version = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            await self._refuse(writer, {"method": "?", "host": "?", "port": 0},
                               "a malformed request")
            return

        entry: dict = {"at": round(time.time(), 3), "method": method.upper()}
        if method.upper() == "CONNECT":
            host, port = _host_port(target, default=443)
            first = b""  # the tunnel carries TLS; nothing goes before it
        else:
            parts = urlsplit(target)
            if parts.scheme.lower() != "http" or not parts.hostname:
                entry.update(host=target[:120], port=0)
                await self._refuse(writer, entry, "only CONNECT, or an http:// request")
                return
            host = parts.hostname
            try:
                port = parts.port or 80
            except ValueError:
                port = -1
            path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            first = _forwarded(method, path, version, headers)
        entry.update(host=(host or "?")[:255], port=port)

        if why := self._why_not(host, port):
            await self._refuse(writer, entry, why)
            return
        vetted = await asyncio.to_thread(self.resolve, host, port)
        if isinstance(vetted, str):
            await self._refuse(writer, entry, vetted)
            return

        upstream = None
        for ip in vetted:
            try:
                upstream = await asyncio.wait_for(self.connect(ip, port), CONNECT_TIMEOUT_S)
                entry["ip"] = ip
                break
            except (OSError, TimeoutError):
                continue
        if upstream is None:
            entry.update(ok=False, why="could not connect")
            self.log(entry)
            await _reply(writer, b"502 Bad Gateway", "could not connect")
            return

        up_reader, up_writer = upstream
        counts = {"up": 0, "down": 0}
        try:
            if first:
                if not self._spend(len(first)):
                    raise _Capped
                up_writer.write(first)
                await up_writer.drain()
                counts["up"] += len(first)
            else:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            # Each pipe closes the side it writes to when it ends, so either
            # side hanging up -- or the cap -- ends the other as well.
            ended = await asyncio.gather(
                self._pipe(reader, up_writer, counts, upstream=True),
                self._pipe(up_reader, writer, counts, upstream=False),
                return_exceptions=True)
            if any(isinstance(e, _Capped) for e in ended):
                raise _Capped
            entry.update(ok=True)
        except _Capped:
            entry.update(ok=False, why="the session's upstream byte cap was reached")
        except (ConnectionError, OSError):
            entry.update(ok=True)
        finally:
            await _close(up_writer)
            await _close(writer)
            entry.update(up=counts["up"], down=counts["down"])
            self.log(entry)

    def _why_not(self, host: str | None, port: int) -> str | None:
        if not host:
            return "no host"
        if port not in ALLOWED_PORTS:
            return f"port {port}: only 80 and 443"
        if self.sent >= self.cap:
            return "the session's upstream byte cap was reached"
        return None

    def _spend(self, n: int) -> bool:
        self.sent += n
        return self.sent <= self.cap

    async def _pipe(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter,
                    counts: dict, *, upstream: bool) -> None:
        try:
            while chunk := await source.read(CHUNK):
                if upstream and not self._spend(len(chunk)):
                    raise _Capped
                counts["up" if upstream else "down"] += len(chunk)
                sink.write(chunk)
                await sink.drain()
        finally:
            await _close(sink)

    async def _refuse(self, writer: asyncio.StreamWriter, entry: dict, why: str) -> None:
        entry.update(ok=False, why=why)
        self.log(entry)
        await _reply(writer, b"403 Forbidden", why)


class _Capped(Exception):
    """The session has sent all it may."""


def _host_port(target: str, *, default: int) -> tuple[str | None, int]:
    try:
        parts = urlsplit("//" + target.strip())
        return parts.hostname, parts.port or default
    except ValueError:
        return None, -1


def _forwarded(method: str, path: str, version: str, headers: bytes) -> bytes:
    """A plain-HTTP request rewritten for the origin: origin-form target, the
    hop-by-hop headers dropped, and `Connection: close`."""
    kept = [line for line in headers.split(b"\r\n")
            if line and line.split(b":", 1)[0].strip().lower() not in _HOP]
    head = f"{method} {path} {version}".encode("latin-1")
    return b"\r\n".join([head, *kept, b"Connection: close", b"", b""])


async def _reply(writer: asyncio.StreamWriter, status: bytes, why: str) -> None:
    body = (why + "\n").encode("utf-8", "replace")
    reason = why.encode("ascii", "replace")[:200]
    with contextlib.suppress(ConnectionError, OSError):
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain\r\n"
                     b"Connection: close\r\nX-Agent-Refused: " + reason + b"\r\n"
                     + f"Content-Length: {len(body)}".encode() + b"\r\n\r\n" + body)
        await writer.drain()
    await _close(writer)


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()


async def serve(host: str = "0.0.0.0", port: int = PORT, gate: Gate | None = None) -> None:  # noqa: S104 - inside its container, on the browser's network and Docker's default bridge, no port published
    gate = gate or Gate()
    server = await asyncio.start_server(gate.handle, host, port, limit=HEAD_LIMIT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    print(json.dumps({"gate": "listening", "port": PORT}), flush=True)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        sys.exit(0)
