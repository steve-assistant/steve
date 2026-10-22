"""The broker's end of a local server's pipe: its connections, one host at a time.

A server Steve starts runs with no network of its own (06-modules.md,
section 7). Each connection it makes comes down its pipe
(`core/sandbox/pipe.py`) as one line, the host and port it wants, then the
start of a TLS handshake. Without the checks here on each one, the pipe
would be the network the container was denied:

- **a host the card did not name** would be reached. The line's host must
  be one the server was approved for, at one of its ports; or, for a
  server approved to reach anywhere, any public name on 443;
- **the name checked would not be the one used.** The host is vetted by
  core's one check (`core/egress.vet`), which resolves it once and refuses
  loopback, the private network and Steve's own ports, and the connection
  goes to an address it vetted, never to the name again;
- **the TLS name could differ from the line's:** a server could ask for an
  approved host and greet another behind it, one the same provider serves.
  The handshake's first message is read: the name it asks for (SNI) must
  be the line's host, and a hidden one (Encrypted Client Hello) is refused.
  Nothing after that is read, so what is said stays between the server and
  its service. A connection that is not TLS is refused: its host would go
  unchecked in what it sends;
- **a flood** of connections or bytes would make the server a way to spend
  the user's network: its connections at once and in a minute, and its
  bytes each way in an hour, are capped. Rates, not totals over its run:
  a server whose account is checked every few minutes is never idle
  long enough to be stopped (`servers.IDLE_S`), and a total would in
  time cut it off for good, every connection after refused.

Only the process Steve started is served (`Connection.client_pid`). A
refusal closes the connection and is said to `note`, the server's log,
which its owner keeps redacted; the server sees a connection closed.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from core import egress
from core.modctx import Network
from core.sandbox import pipe as pipes

log = logging.getLogger("sandbox.relay")

MAX_LINE = 300
MAX_HELLO = 64 * 1024
HELLO_S = 10.0     # for the line and the handshake's first message
CONNECT_S = 10.0
ACCEPT_S = 0.5     # how often the accepting thread looks up to see if it should stop
CHUNK = 64 * 1024
ANYWHERE_PORT = 443
# TLS: a handshake record, a ClientHello, and the extensions read.
HANDSHAKE = 0x16
CLIENT_HELLO = 0x01
SERVER_NAME = 0x0000
ENCRYPTED_CLIENT_HELLO = 0xFE0D


class Refused(Exception):
    """A connection the server may not make, and why."""


@dataclass(frozen=True)
class Reach:
    """Where a server's connections may go: each host it was approved for,
    with its ports; or, `anywhere`, any public name on 443."""

    hosts: dict[str, tuple[int, ...]]
    anywhere: bool = False

    def why_not(self, host: str, port: int) -> str | None:
        if port in self.hosts.get(host, ()):
            return None
        if self.anywhere and port == ANYWHERE_PORT:
            return None
        return f"{host}:{port} is not a host the server was approved for"


@dataclass(frozen=True)
class Caps:
    """What a server may use: connections at once and in a minute, and
    bytes each way in an hour (`BYTES_S`)."""

    at_once: int = 16
    per_minute: int = 120
    out: int = 64 << 20
    into: int = 1 << 30


BYTES_S = 3600.0   # the window the byte caps count in


def target(line: bytes) -> tuple[str, int]:
    """The host and port of a connection's first line, `host:port`. A name,
    never an address: the handshake's name is checked against it, and a
    TLS name is never an address."""
    try:
        text = line.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise Refused("its first line is not a host and port") from exc
    host, _, port = text.rpartition(":")
    host = host.rstrip(".").lower()
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise Refused("its first line is not a host and port")
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host.strip("[]"))
        raise Refused(f"{host} is an address; the server names the host it wants")
    return host, int(port)


def hello_name(data: bytes) -> str | None:
    """The name a TLS ClientHello at the start of `data` asks for; None
    while more bytes are needed. Refused if `data` is not one, if it hides
    its name, or if it gives none."""
    handshake = b""
    at = 0
    while True:
        if len(data) - at < 5:
            return None
        kind, _version, size = struct.unpack_from("!BHH", data, at)
        if kind != HANDSHAKE:
            raise Refused("it is not TLS")
        if size == 0 or size > 1 << 14:
            raise Refused("its handshake is malformed")
        if len(data) - at - 5 < size:
            return None
        handshake += data[at + 5:at + 5 + size]
        at += 5 + size
        if len(handshake) >= 4:
            if handshake[0] != CLIENT_HELLO:
                raise Refused("its handshake does not begin with a ClientHello")
            length = int.from_bytes(handshake[1:4], "big")
            if length > MAX_HELLO:
                raise Refused("its ClientHello is too long")
            if len(handshake) >= 4 + length:
                return _server_name(handshake[4:4 + length])


def _server_name(body: bytes) -> str:
    try:
        at = 2 + 32                                    # version, random
        at += 1 + body[at]                             # session id
        at += 2 + int.from_bytes(body[at:at + 2], "big")   # cipher suites
        at += 1 + body[at]                             # compression
        end = at + 2 + int.from_bytes(body[at:at + 2], "big")
        at += 2
        names: list[str] = []
        while at < end:
            kind, size = struct.unpack_from("!HH", body, at)
            value = body[at + 4:at + 4 + size]
            at += 4 + size
            if kind == ENCRYPTED_CLIENT_HELLO:
                raise Refused("it hides the name it asks for (Encrypted Client Hello)")
            if kind == SERVER_NAME:
                names += _names(value)
    except (IndexError, struct.error) as exc:
        raise Refused("its ClientHello is malformed") from exc
    if at != end or end != len(body):
        raise Refused("its ClientHello is malformed")
    if len(names) != 1:
        raise Refused("its ClientHello names no one host")
    return names[0]


def _names(value: bytes) -> list[str]:
    end = 2 + int.from_bytes(value[:2], "big")
    if end != len(value):
        raise Refused("its ClientHello is malformed")
    at, out = 2, []
    while at < end:
        kind, size = struct.unpack_from("!BH", value, at)
        name = value[at + 3:at + 3 + size]
        at += 3 + size
        if kind == 0:
            try:
                out.append(name.decode("ascii").rstrip(".").lower())
            except UnicodeDecodeError as exc:
                raise Refused("its ClientHello's name is not one") from exc
    return out


class Relay:
    """Serves one server's pipe until stopped: each connection from its
    process, checked, to the address its host was vetted at."""

    def __init__(self, held: pipes.Pipe, *, pid: int, reach: Reach, network: Network,
                 note: Callable[[str], None], caps: Caps | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.pipe = held
        self.pid = pid
        self.reach = reach
        self.network = network
        self.note = note
        self.caps = caps or Caps()
        self.clock = clock
        self.out = self.into = 0              # bytes each way over its run
        self._spent = {"out": 0, "into": 0}   # and in the hour from `_since`, capped
        self._since = clock()
        self.refused: list[str] = []
        self._open = 0
        self._live: dict[pipes.Connection, socket.socket | None] = {}
        self._serving: list[threading.Thread] = []
        self._recent: deque[float] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> Relay:
        self._thread = threading.Thread(target=self._accept, daemon=True, name="relay")
        self._thread.start()
        return self

    def stop(self) -> None:
        """No more connections, and those open ended: when it returns,
        `refused` and the counts are whole."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(ACCEPT_S * 4 + 5)
        self.pipe.close()
        with self._lock:
            live = list(self._live.items())
        for connection, upstream in live:
            connection.close()
            if upstream is not None:
                with contextlib.suppress(OSError):
                    upstream.close()
        for thread in self._serving:
            thread.join(HELLO_S + 5)

    def _refuse(self, why: str) -> None:
        with self._lock:
            self.refused.append(why)
        self.note(f"steve: a connection refused: {why}")

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                connection = self.pipe.accept(ACCEPT_S)
            except OSError as exc:
                log.warning("a server's pipe failed: %s", exc)
                return
            if connection is None:
                continue
            if connection.client_pid != self.pid:
                connection.close()
                self._refuse("a process not the server's opened its pipe")
                continue
            if why := self._admit():
                connection.close()
                self._refuse(why)
                continue
            thread = threading.Thread(target=self._serve, args=(connection,), daemon=True,
                                      name="relay-connection")
            self._serving = [t for t in self._serving if t.is_alive()] + [thread]
            thread.start()

    def _admit(self) -> str | None:
        now = self.clock()
        with self._lock:
            while self._recent and self._recent[0] <= now - 60:
                self._recent.popleft()
            if self._open >= self.caps.at_once:
                return f"more than {self.caps.at_once} connections at once"
            if len(self._recent) >= self.caps.per_minute:
                return f"more than {self.caps.per_minute} connections in a minute"
            self._open += 1
            self._recent.append(now)
        return None

    def _serve(self, connection: pipes.Connection) -> None:
        upstream: socket.socket | None = None
        with self._lock:
            self._live[connection] = None
        try:
            upstream, first = self._open_for(connection)
            with self._lock:
                self._live[connection] = upstream
            if self._stop.is_set():
                return
            self._count("out", len(first))
            upstream.sendall(first)
            self._relay(connection, upstream)
        except Refused as exc:
            self._refuse(str(exc))
        except OSError as exc:
            self.note(f"steve: a connection ended: {type(exc).__name__}")
        finally:
            connection.close()
            if upstream is not None:
                with contextlib.suppress(OSError):
                    upstream.close()
            with self._lock:
                self._open -= 1
                self._live.pop(connection, None)

    def _open_for(self, connection: pipes.Connection) -> tuple[socket.socket, bytes]:
        """The connection's host checked and connected, and the bytes read
        so far, to send on first. Held to `HELLO_S`: a server that opens
        and says nothing would hold a connection for nothing."""
        timer = threading.Timer(HELLO_S, connection.close)
        timer.start()
        try:
            data = b""
            while b"\n" not in data:
                if len(data) > MAX_LINE:
                    raise Refused("its first line is not a host and port")
                data += self._read(connection)
            line, _, data = data.partition(b"\n")
            host, port = target(line)
            if why := self.reach.why_not(host, port):
                raise Refused(why)
            while (name := hello_name(data)) is None:
                if len(data) > MAX_HELLO + 5 * 16:
                    raise Refused("its ClientHello is too long")
                data += self._read(connection)
        finally:
            timer.cancel()
        if name != host:
            raise Refused(f"it asked for {host} and greeted {name}")
        vetted = egress.vet(host, port, resolve=self.network.resolve,
                            own_ports=self.network.own_ports)
        if isinstance(vetted, str):
            raise Refused(vetted)
        last: OSError | None = None
        for address in vetted:
            try:
                return self.network.open_tcp(address, port, CONNECT_S), data
            except OSError as exc:
                last = exc
        raise Refused(f"{host} did not answer ({type(last).__name__})")

    @staticmethod
    def _read(connection: pipes.Connection) -> bytes:
        chunk = connection.stream.read(CHUNK)
        if not chunk:
            raise Refused("it closed before saying where to")
        return chunk

    def _count(self, way: str, size: int) -> None:
        now = self.clock()
        with self._lock:
            if now - self._since >= BYTES_S:
                self._since, self._spent = now, {"out": 0, "into": 0}
            setattr(self, way, getattr(self, way) + size)
            self._spent[way] += size
            spent = self._spent[way]
        cap = self.caps.out if way == "out" else self.caps.into
        if spent > cap:
            raise Refused(f"past its {cap >> 20} MB {'out' if way == 'out' else 'in'} "
                          "for the hour")

    def _relay(self, connection: pipes.Connection, upstream: socket.socket) -> None:
        """Bytes each way until either side ends; neither is read."""
        upstream.settimeout(None)
        failed: list[BaseException] = []

        def outward() -> None:
            try:
                while chunk := connection.stream.read(CHUNK):
                    self._count("out", len(chunk))
                    upstream.sendall(chunk)
            except (OSError, Refused) as exc:
                failed.append(exc)
            finally:
                with contextlib.suppress(OSError):
                    upstream.shutdown(socket.SHUT_WR)

        sender = threading.Thread(target=outward, daemon=True, name="relay-out")
        sender.start()
        try:
            while chunk := upstream.recv(CHUNK):
                self._count("into", len(chunk))
                connection.stream.write(chunk)
        finally:
            connection.close()
            with contextlib.suppress(OSError):
                upstream.shutdown(socket.SHUT_RDWR)
            sender.join(5)
        for exc in failed:
            if isinstance(exc, Refused):
                raise exc
