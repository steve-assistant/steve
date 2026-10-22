"""The world's network, as the backend's own processes see it.

A world (tests/fakes/world.py) serves mail, a calendar and a feed on this
computer, each on a loopback port, under the names a user types in setup
(imap.mail.example, caldav.mail.example). The backend reaches a server
only through `core.modctx.Network`: DNS, sockets, TLS, and an HTTP
transport per host. So the child fence changes that class as
`core.modctx` is imported (`world.json`'s `after_import`): a world name
resolves to an address of its own, a connection to that address and port
goes to its server's loopback port, HTTPS to a world host goes to its
server with the name kept for SNI and the certificate, and TLS trusts the
world's authority. Every other host goes where it would have gone, and
the child fence refuses what would leave this computer.

The class is changed, not an instance: the worker makes its own
(`agent/connectors.py`, `agent/mcptools.py`), and a test cannot reach
into another process to swap one.

Setup pins the kind of address a typed server resolves to when the account
is added (`agent/accounts._pinned`, through `core.egress.pin_kind`), by
the system's DNS rather than the Network's. Its default resolver is
pointed here too; without it no world name could be added at all.

Only the standard library and httpx: this runs inside Steve's processes,
which have no `trustme`.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
from pathlib import Path
from typing import Any

WORLD_ENV = "STEVE_TEST_WORLD"
# The addresses world names resolve to, one each, from a public range: the
# egress checks see a public host, as they would a real provider's.
FIRST = (93, 184, 216, 1)


def own_address() -> str:
    """This computer's address on its network, from its host name, for the
    module probe's child to try (core/sandbox/probe.py `own_address`). The
    probe finds it by a UDP socket's connect to a documentation address,
    which sends nothing, but the child fence refuses every connect off this
    computer; and the probe, catching only an OSError, then gave no answer,
    so no world's backend offered to build a module. The gate's own probe
    test asks the host name the same way (tests/gate/test_module_sandbox.py)."""
    for *_, (address, _port) in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        if not address.startswith("127."):
            return address
    return ""


def addresses(network: dict[str, Any]) -> dict[str, str]:
    """Each world host's address, the same in the test as in the backend."""
    names = sorted(set(network.get("hosts", {})) | set(network.get("https", {})))
    a, b, c, d = FIRST
    return {name: f"{a}.{b}.{c}.{d + i}" for i, name in enumerate(names)}


def _name(host: str | None) -> str:
    return (host or "").strip().rstrip(".").lower()


def attach(network_class: type, egress: Any) -> None:
    """Point `network_class` (core.modctx.Network) and setup's pin
    (`egress.pin_kind`) at the world's servers, when the world serves any."""
    import httpx

    folder = os.environ.get(WORLD_ENV)
    if not folder:
        return
    given = json.loads((Path(folder) / "world.json").read_text(encoding="utf-8"))
    network = given.get("network")
    if not network:
        return
    by_name = addresses(network)
    by_address = {address: name for name, address in by_name.items()}
    ports = {name: {int(port): to for port, to in served.items()}
             for name, served in network.get("hosts", {}).items()}
    ca = network["ca"]
    # The class's own methods, before they are replaced: a host outside the
    # world goes where it would have gone.
    real_resolve, real_open = network_class.resolve, network_class.open_tcp
    real_transport, real_pin = network_class.transport_for, egress._resolve

    class Routed(httpx.BaseTransport):
        """HTTPS to a world host, sent to its server's loopback port with the
        host's name kept for SNI and the certificate's check."""

        def __init__(self, port: int) -> None:
            self.port = port
            self.inner = httpx.HTTPTransport(verify=ssl.create_default_context(cadata=ca))

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            host = request.url.host
            request.url = request.url.copy_with(host="127.0.0.1", port=self.port)
            request.extensions = {**request.extensions, "sni_hostname": host}
            return self.inner.handle_request(request)

    routes = {name: Routed(port) for name, port in network.get("https", {}).items()}

    def resolve(self, host: str, port: int) -> list[str]:
        name = _name(host)
        return [by_name[name]] if name in by_name else real_resolve(self, host, port)

    def open_tcp(self, address: str, port: int, timeout: float) -> socket.socket:
        name = by_address.get(address)
        if name is None:
            return real_open(self, address, port, timeout)
        to = ports.get(name, {}).get(port)
        if to is None:
            raise ConnectionRefusedError(f"the world serves nothing on {name}:{port}")
        return socket.create_connection(("127.0.0.1", to), timeout=timeout)

    def transport_for(self, host: str) -> httpx.BaseTransport | None:
        return routes.get(_name(host)) or real_transport(self, host)

    def tls(self) -> ssl.SSLContext:
        return ssl.create_default_context(cadata=ca)

    def node_authorities(self) -> str | None:
        return ca

    def pinned(host: str, port: int) -> list[str]:
        name = _name(host)
        return [by_name[name]] if name in by_name else real_pin(host, port)

    for attribute, value in (("resolve", resolve), ("open_tcp", open_tcp),
                             ("transport_for", transport_for), ("tls", tls),
                             ("node_authorities", node_authorities)):
        setattr(network_class, attribute, value)
    egress.pin_kind.__kwdefaults__["resolve"] = pinned
