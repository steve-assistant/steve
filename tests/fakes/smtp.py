"""A real SMTP server on loopback (aiosmtpd), over TLS or STARTTLS.

The IMAP engine sends by SMTP after the card. This fake takes a password,
refuses to sign in before TLS, and keeps what it was given: the envelope
and the message as sent, so a test can read the recipients the server was
told (Bcc among them) against the headers it saw (no Bcc). In Gmail's mode
it files its own copy in Sent, through the IMAP fake's world, as Gmail does.
"""

from __future__ import annotations

import email
import email.policy
import socket
import ssl
import threading
from dataclasses import dataclass, field
from typing import Any

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

from tests.fakes.imap import GMAIL, ImapWorld

STOP_S = 10.0  # a stop takes milliseconds once its clients are dropped


@dataclass
class Sent:
    sender: str
    recipients: list[str]
    raw: bytes


@dataclass
class SmtpWorld:
    user: str = "maya@mail.example"
    password: str = "secret-pw"
    imap: ImapWorld | None = None
    sent: list[Sent] = field(default_factory=list)
    refuse: str = ""                  # a reply that refuses every DATA, as "554 ..."
    refuse_to: set[str] = field(default_factory=set)  # recipients refused at RCPT, the rest taken
    on_send: Any = None               # told of each message taken, as ImapWorld.on_write

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options) -> str:  # noqa: N802
        # A server takes a mail for the recipients it knows and refuses the
        # others one by one, and the mail still goes to the rest.
        if address.lower() in self.refuse_to:
            return "550 5.1.1 no such user here"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope) -> str:  # noqa: N802 - aiosmtpd's name
        if self.refuse:
            return self.refuse
        raw = envelope.original_content or envelope.content
        raw = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        self.sent.append(Sent(envelope.mail_from, list(envelope.rcpt_tos), raw))
        if self.on_send is not None:
            message = email.message_from_bytes(raw, policy=email.policy.default)
            body = message.get_body(("plain",))
            self.on_send({"smtp": "send", "to": str(message["To"] or ""),
                          "subject": str(message["Subject"] or ""),
                          "recipients": sorted(envelope.rcpt_tos),
                          "body": body.get_content().strip() if body else ""})
        if self.imap is not None and self.imap.mode == GMAIL:
            self.imap.file_sent(raw)
        return "250 OK queued"

    def authenticate(self, server, session, envelope, mechanism, auth_data) -> AuthResult:
        if (isinstance(auth_data, LoginPassword)
                and auth_data.login.decode() == self.user
                and auth_data.password.decode() == self.password):
            return AuthResult(success=True)
        return AuthResult(success=False, handled=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SmtpServer:
    """`world` served on 127.0.0.1: implicit TLS, or STARTTLS required."""

    def __init__(self, world: SmtpWorld, tls: ssl.SSLContext, *, starttls: bool = False) -> None:
        self.port = _free_port()
        common = {"hostname": "127.0.0.1", "port": self.port, "authenticator": world.authenticate}
        if starttls:
            self.controller = Controller(world, tls_context=tls, require_starttls=True,
                                         auth_require_tls=True, **common)
        else:
            # aiosmtpd counts only STARTTLS as TLS: on a connection that was
            # TLS from its first byte, requiring it would hide AUTH for good.
            self.controller = Controller(world, ssl_context=tls, auth_require_tls=False,
                                         **common)
        self.controller.start()

    def close(self) -> None:
        # Since Python 3.12 an asyncio server's wait_closed() waits for every
        # client connection, and aiosmtpd's stop() waits on it with no bound.
        # A TLS client left idle held a teardown 30 s (the TLS shutdown's own
        # timeout), one left mid-handshake 60 s, and once, in step 7's full
        # suite, one held it 20 minutes. A fake owes its clients no goodbye:
        # it stops listening and drops them all first. The stop is bounded
        # too, so one that still hangs fails, naming what held it.
        controller = self.controller
        server = controller.server

        def drop() -> None:
            server.close()
            server.abort_clients()

        controller.loop.call_soon_threadsafe(drop)
        stopping = threading.Thread(target=controller.stop, daemon=True)
        stopping.start()
        stopping.join(STOP_S)
        if stopping.is_alive():
            held = sorted(map(repr, getattr(server, "_clients", None) or ()))
            raise AssertionError(f"the fake SMTP server on {self.port} did not stop in "
                                 f"{STOP_S:.0f} s; still open: {held}")
