"""A local server's pipe reaches the hosts it was approved for and nothing else.

`core/sandbox/relay.py` is the broker's end of the pipe a server Steve
starts is given instead of a network. Each test here opens the pipe as the
server would, sends the line and a real TLS handshake, and watches what
reaches the network behind it: a fake one, whose names resolve as each test
says and whose one service speaks TLS with a certificate from an authority
made for the test. The container is left out; tests/gate/test_local_servers.py
runs a real one.
"""

from __future__ import annotations

import contextlib
import os
import socket
import ssl
import struct
import threading
import time

import pytest
import trustme

from core.modctx import Network
from core.sandbox import pipe, profiles, relay

PUBLIC = "93.184.216.34"
HOST = "api.notes.example"
# A package SID's shape: the pipe's access list names one; no container
# opens it here.
PACKAGE = "S-1-15-2-1-2-3-4-5-6-7"


class Service(Network):
    """The network behind the broker: names resolve as `names` says, and a
    connection to a public address reaches one TLS service, which echoes
    what it is sent, in capitals, and hangs up once it has said "BYE"."""

    def __init__(self, ca: trustme.CA, names: dict[str, list[str]]) -> None:
        self.names = names
        self.opened: list[tuple[str, int]] = []
        self.heard: list[bytes] = []
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ca.issue_cert(HOST, "other.notes.example", "anywhere.example").configure_cert(
            self._context)

    def resolve(self, host: str, port: int) -> list[str]:
        if host not in self.names:
            raise socket.gaierror(host)
        return self.names[host]

    def own_ports(self) -> set[int]:
        return set()

    def open_tcp(self, address: str, port: int, timeout: float) -> socket.socket:
        self.opened.append((address, port))
        ours, theirs = socket.socketpair()
        threading.Thread(target=self._serve, args=(theirs,), daemon=True).start()
        return ours

    def _serve(self, raw: socket.socket) -> None:
        with contextlib.suppress(OSError), \
                self._context.wrap_socket(raw, server_side=True) as tls:
            while data := tls.recv(65536):
                self.heard.append(data)
                tls.sendall(data.upper())
                if data == b"bye":
                    break


class Tls:
    """The server's side: TLS over the pipe, as Node's would be."""

    def __init__(self, stream, host: str, context: ssl.SSLContext) -> None:
        self.stream = stream
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.tls = context.wrap_bio(self.incoming, self.outgoing, server_hostname=host)

    def first(self) -> bytes:
        """The ClientHello this side would send first."""
        with contextlib.suppress(ssl.SSLWantReadError):
            self.tls.do_handshake()
        return self.outgoing.read()

    def _pump(self) -> None:
        if data := self.outgoing.read():
            self.stream.write(data)

    def _fill(self) -> None:
        data = self.stream.read(65536)
        if not data:
            raise ConnectionError("the broker closed the connection")
        self.incoming.write(data)

    def handshake(self) -> None:
        while True:
            try:
                self.tls.do_handshake()
                self._pump()
                return
            except ssl.SSLWantReadError:
                self._pump()
                self._fill()

    def say(self, data: bytes) -> bytes:
        self.tls.write(data)
        self._pump()
        while True:
            try:
                return self.tls.read(65536)
            except ssl.SSLWantReadError:
                self._fill()


@pytest.fixture
def ca():
    return trustme.CA()


@pytest.fixture
def trusting(ca):
    context = ssl.create_default_context()
    ca.configure_trust(context)
    return context


@pytest.fixture
def service(ca):
    return Service(ca, {HOST: [PUBLIC], "other.notes.example": [PUBLIC],
                        "anywhere.example": [PUBLIC], "inside.notes.example": ["10.0.0.5"],
                        "home.notes.example": ["127.0.0.1"]})


@contextlib.contextmanager
def serving(service, *, reach=None, pid=None, caps=None, clock=time.monotonic):
    held = pipe.Pipe(PACKAGE, profiles.install())
    notes: list[str] = []
    served = relay.Relay(held, pid=os.getpid() if pid is None else pid,
                         reach=reach or relay.Reach({HOST: (443,)}), network=service,
                         note=notes.append, caps=caps, clock=clock).start()
    try:
        yield served, held.name, notes
    finally:
        served.stop()


def _open(name: str):
    for _ in range(50):
        try:
            return open(name, "r+b", buffering=0)  # noqa: SIM115
        except OSError:
            time.sleep(0.05)
    raise AssertionError("the pipe never opened")


def _reset(stream) -> bool:
    """True once the broker has reset this connection: the server's read
    fails. A clean end instead would read as a service that answered and
    hung up, and Node gives a plain socket no error for one, so a server
    waiting for its refused connection's error would wait for ever."""
    try:
        stream.read(1)
    except OSError:
        return True
    return False


def test_a_connection_to_an_approved_host_reaches_it_and_its_tls_is_the_servers_own(
        service, trusting):
    """The one way out works: the server's TLS runs end to end with its
    service, and the broker counts the bytes without reading them."""
    with serving(service) as (served, name, notes), _open(name) as stream:
        stream.write(f"{HOST}:443\n".encode())
        client = Tls(stream, HOST, trusting)
        client.handshake()
        assert client.say(b"hello") == b"HELLO"
        assert service.opened == [(PUBLIC, 443)] and service.heard == [b"hello"]
        assert served.out > 0 and served.into > 0 and notes == []


def test_a_reply_its_service_hangs_up_after_reaches_the_server_whole_and_then_a_clean_end(
        service, trusting):
    """A service that answers and hangs up is the common case; the broker
    writes its last chunk and closes at once. A reset there would discard
    the chunk the server had not read yet, and tell it the connection
    failed when it ended."""
    with serving(service) as (served, name, notes), _open(name) as stream:
        stream.write(f"{HOST}:443\n".encode())
        client = Tls(stream, HOST, trusting)
        client.handshake()
        assert client.say(b"bye") == b"BYE"
        assert stream.read(1) == b""
    assert served.refused == []


@pytest.mark.parametrize(("line", "why"), [
    ("elsewhere.example:443", "not a host the server was approved for"),
    (f"{HOST}:8443", "not a host the server was approved for"),
    (f"{PUBLIC}:443", "is an address"),
    ("not a line", "not a host and port"),
])
def test_a_host_or_port_the_server_was_not_approved_for_reaches_nothing(
        service, trusting, line, why):
    """The card named the hosts; a connection anywhere else, or to an
    address that names no host, is closed before anything is dialled."""
    with serving(service) as (served, name, notes), _open(name) as stream:
        host = line.split(":")[0]
        stream.write(line.encode() + b"\n" + Tls(stream, host, trusting).first())
        assert _reset(stream)
    assert service.opened == []
    assert any(why in said for said in served.refused), served.refused


def test_a_handshake_for_another_name_than_the_line_asked_for_is_refused(
        service, trusting):
    """Asking for an approved host and greeting another, which the same
    provider may serve at the same address, would reach a service the card
    never named."""
    with serving(service) as (served, name, notes), _open(name) as stream:
        stream.write(f"{HOST}:443\n".encode()
                     + Tls(stream, "other.notes.example", trusting).first())
        assert _reset(stream)
    assert service.opened == []
    assert served.refused == [f"it asked for {HOST} and greeted other.notes.example"]


def test_a_connection_that_is_not_tls_is_refused(service):
    """Plain HTTP would carry its host in what the broker does not read."""
    with serving(service) as (served, name, notes), _open(name) as stream:
        stream.write(f"{HOST}:443\nGET / HTTP/1.1\r\nHost: elsewhere.example\r\n\r\n".encode())
        assert _reset(stream)
    assert service.opened == [] and served.refused == ["it is not TLS"]


def _with_extension(hello: bytes, kind: int, data: bytes) -> bytes:
    """`hello` (one record) with one more extension, its lengths made good."""
    body = bytearray(hello[9:])
    at = 2 + 32
    at += 1 + body[at]
    at += 2 + int.from_bytes(body[at:at + 2], "big")
    at += 1 + body[at]
    size = int.from_bytes(body[at:at + 2], "big")
    extra = struct.pack("!HH", kind, len(data)) + data
    body[at:at + 2] = (size + len(extra)).to_bytes(2, "big")
    body += extra
    handshake = b"\x01" + len(body).to_bytes(3, "big") + bytes(body)
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def test_a_handshake_that_hides_its_name_is_refused(service, trusting):
    """Encrypted Client Hello puts the real name where the broker cannot
    read it, behind an outer one it would check instead."""
    with serving(service) as (served, name, notes), _open(name) as stream:
        hello = Tls(stream, HOST, trusting).first()
        assert relay.hello_name(hello) == HOST
        stream.write(f"{HOST}:443\n".encode()
                     + _with_extension(hello, relay.ENCRYPTED_CLIENT_HELLO, b"\x00" * 8))
        assert _reset(stream)
    assert service.opened == []
    assert served.refused == ["it hides the name it asks for (Encrypted Client Hello)"]


def test_a_handshake_split_across_reads_and_records_is_read_whole(trusting):
    hello = Tls(None, HOST, trusting).first()
    body = hello[5:]
    split = (b"\x16\x03\x01" + struct.pack("!H", 20) + body[:20]
             + b"\x16\x03\x01" + struct.pack("!H", len(body) - 20) + body[20:])
    assert relay.hello_name(split[:30]) is None
    assert relay.hello_name(split) == HOST


@pytest.mark.parametrize("host", ["inside.notes.example", "home.notes.example"])
def test_an_approved_name_that_resolves_inside_the_network_is_never_dialled(
        service, trusting, host):
    """Core's one check stands behind the card: a name that now resolves
    to this computer or the private network is refused, approved or not."""
    with serving(service, reach=relay.Reach({host: (443,)})) as (served, name, notes), \
            _open(name) as stream:
        stream.write(f"{host}:443\n".encode() + Tls(stream, host, trusting).first())
        assert _reset(stream)
    assert service.opened == [] and len(served.refused) == 1


def test_a_server_approved_for_anywhere_reaches_public_names_on_443_alone(
        service, trusting):
    reach = relay.Reach({}, anywhere=True)
    with serving(service, reach=reach) as (served, name, notes):
        with _open(name) as stream:
            stream.write(b"anywhere.example:443\n")
            client = Tls(stream, "anywhere.example", trusting)
            client.handshake()
            assert client.say(b"hi") == b"HI"
        with _open(name) as stream:
            stream.write(b"anywhere.example:22\n"
                         + Tls(stream, "anywhere.example", trusting).first())
            assert _reset(stream)
        with _open(name) as stream:
            stream.write(b"home.notes.example:443\n"
                         + Tls(stream, "home.notes.example", trusting).first())
            assert _reset(stream)
    assert service.opened == [(PUBLIC, 443)] and len(served.refused) == 2


def test_only_the_servers_own_process_is_served(service, trusting):
    """The user's other programs can open the pipe, as they can read the
    user's files; they are not served through it."""
    with serving(service, pid=os.getpid() + 4) as (served, name, notes), \
            _open(name) as stream:
        with contextlib.suppress(OSError):  # closed before it is written to
            stream.write(f"{HOST}:443\n".encode() + Tls(stream, HOST, trusting).first())
        assert _reset(stream)
    assert service.opened == []
    assert served.refused == ["a process not the server's opened its pipe"]


def test_connections_past_a_minutes_cap_are_refused_until_the_minute_passes(
        service, trusting):
    now = [100.0]
    caps = relay.Caps(per_minute=2)
    with serving(service, caps=caps, clock=lambda: now[0]) as (served, name, notes):
        for _ in range(2):
            with _open(name) as stream:
                stream.write(f"{HOST}:443\n".encode())
                Tls(stream, HOST, trusting).handshake()
        with _open(name) as stream:
            assert _reset(stream)
        now[0] += 61
        with _open(name) as stream:
            stream.write(f"{HOST}:443\n".encode())
            Tls(stream, HOST, trusting).handshake()
    assert len(service.opened) == 3
    assert served.refused == ["more than 2 connections in a minute"]


def test_a_server_past_its_bytes_is_cut_off(service, trusting):
    caps = relay.Caps(into=1 << 20)
    with serving(service, caps=caps) as (served, name, notes), _open(name) as stream:
        stream.write(f"{HOST}:443\n".encode())
        client = Tls(stream, HOST, trusting)
        client.handshake()
        assert client.say(b"x" * 1000) == b"X" * 1000
        with pytest.raises((ConnectionError, ssl.SSLError, OSError)):
            for _ in range(200):
                client.say(b"y" * 16000)
    assert any("MB in" in said for said in served.refused), served.refused


def test_a_server_past_its_bytes_for_the_hour_is_served_again_once_the_hour_passes(
        service, trusting):
    """A server whose account is checked every few minutes is never idle
    long enough to be stopped, so its run may last weeks: a cap on the
    run's total would in time cut it off for good, every connection after
    refused, though it never flooded anything."""
    now = [100.0]
    caps = relay.Caps(into=1 << 20)
    with serving(service, caps=caps, clock=lambda: now[0]) as (served, name, notes):
        with _open(name) as stream:
            stream.write(f"{HOST}:443\n".encode())
            client = Tls(stream, HOST, trusting)
            client.handshake()
            with pytest.raises((ConnectionError, ssl.SSLError, OSError)):
                for _ in range(200):
                    client.say(b"y" * 16000)
        now[0] += relay.BYTES_S
        with _open(name) as stream:
            stream.write(f"{HOST}:443\n".encode())
            client = Tls(stream, HOST, trusting)
            client.handshake()
            assert client.say(b"x" * 1000) == b"X" * 1000
    assert served.refused == ["past its 1 MB in for the hour"]
    assert served.into > 1 << 20, "the run's own count goes on"


def test_a_connection_that_never_says_where_is_closed(monkeypatch, service):
    monkeypatch.setattr(relay, "HELLO_S", 0.3)
    with serving(service) as (served, name, notes), _open(name) as stream:
        assert _reset(stream)
    assert served.refused == ["it closed before saying where to"]
