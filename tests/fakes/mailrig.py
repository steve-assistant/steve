"""The IMAP engine wired to the fakes: what its tests and the golden world use.

A real account's context (`core.modctx.ModuleContext`) is built from a
manifest, with the password in the fenced Credential Manager, and only its
`Network` replaced: every name resolves to one public address, a socket to
a known port lands on the fake behind it (993 IMAP, 143 IMAP with
STARTTLS, 465 SMTP over TLS, 587 SMTP with STARTTLS), and TLS trusts the
authority `trustme` made for this rig and nothing else. Every rule in
`ctx.tcp` -- hosts, kinds, Steve's own ports -- still runs.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass, field
from typing import Any

import trustme

from core import credstore, modctx, modules
from steve_sdk.context import Account
from steve_sdk.manifest import Manifest
from tests.fakes.imap import STANDARD, ImapServer, ImapWorld
from tests.fakes.smtp import SmtpServer, SmtpWorld

PUBLIC_IP = "93.184.216.34"
# Every host a test or a preset names, on the one certificate.
HOSTS = ("imap.mail.example", "smtp.mail.example", "imap.gmail.com", "smtp.gmail.com",
         "imap.mail.me.com", "smtp.mail.me.com", "imap.fastmail.com", "smtp.fastmail.com",
         "imap.mail.yahoo.com", "smtp.mail.yahoo.com", "mail.home.example")


class RigNetwork(modctx.Network):
    def __init__(self, ca: trustme.CA, ports: dict[int, int]) -> None:
        self.ca = ca
        self.ports = ports
        self.opened: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> list[str]:
        return [PUBLIC_IP]

    def own_ports(self) -> set[int]:
        return {54321}  # Steve's own, as a run file would name it

    def open_tcp(self, address: str, port: int, timeout: float) -> socket.socket:
        self.opened.append((address, port))
        return socket.create_connection(("127.0.0.1", self.ports[port]), timeout=timeout)

    def tls(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=None, capath=None, cadata=None)
        context.load_verify_locations(cadata=self.ca.cert_pem.bytes().decode())
        return context


@dataclass
class Rig:
    world: ImapWorld
    smtp: SmtpWorld
    network: RigNetwork
    servers: list[Any] = field(default_factory=list)

    def context(self, manifest: Manifest, *, address: str = "maya@mail.example",
                preset: str | None = None, fields: dict | None = None,
                password: str | None = None, key: str = "acct0001",
                module_key: str = "m0dkey0001", state_dir=None) -> modctx.ModuleContext:
        account = Account(key=key, name="Mailbox", module=manifest.id, address=address,
                          preset=preset, fields=fields or {})
        for name in manifest.secrets:
            credstore.write(credstore.target(module_key, key, name),
                            password if password is not None else self.world.password)
        return modctx.ModuleContext(manifest=manifest, module_key=module_key, account=account,
                                    state_dir=state_dir, network=self.network)

    def provider(self, manifest: Manifest, **kw):
        from core.engines import imap

        return imap.engine(self.context(manifest, **kw), manifest.specs["mail"])

    def close(self) -> None:
        for server in self.servers:
            server.close()


def rig(mode: str = STANDARD, *, user: str = "maya@mail.example", password: str = "secret-pw",
        caps: frozenset[str] | None = None) -> Rig:
    ca = trustme.CA()
    served = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert(*HOSTS).configure_cert(served)
    world = ImapWorld(mode, user=user, password=password, caps=caps)
    smtp = SmtpWorld(user=user, password=password, imap=world)
    imap_tls, imap_plain = ImapServer(world, served), ImapServer(world, served, starttls=True)
    smtp_tls, smtp_plain = SmtpServer(smtp, served), SmtpServer(smtp, served, starttls=True)
    network = RigNetwork(ca, {993: imap_tls.port, 143: imap_plain.port,
                              465: smtp_tls.port, 587: smtp_plain.port})
    return Rig(world, smtp, network, [imap_tls, imap_plain, smtp_tls, smtp_plain])


def module(module_id: str) -> Manifest:
    """A built-in module's manifest, as the app would add its account."""
    return modules.builtins()[module_id]


def add_account(conn, rig: Rig, *, module_id: str = "imap", preset: str | None = "other",
                address: str = "maya@mail.example", name: str | None = None) -> str:
    """An account added as the app adds one (`agent.accounts.add`), against
    this rig's servers; its key. The caller points the slot at the rig
    (`connectors.NETWORK = rig.network`)."""
    from agent import accounts

    manifest = module(module_id)
    modules.register(conn)
    values: dict[str, str] = {manifest.secrets[0]: rig.world.password}
    if preset == "other":
        values.update(imap="imap.mail.example:993", smtp="smtp.mail.example:465")
    return accounts.add(conn, module_id=module_id, address=address, preset=preset,
                        values=values, name=name, resolve=lambda host, port: [PUBLIC_IP])
