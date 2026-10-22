"""What a module's code is handed: its context, and nothing else of Steve's.

A provider written against Google's client or `imaplib` opened its own
connections, to whatever host it named, and read its own secrets from
wherever it kept them. Nothing could hold it to the hosts it declared, and
the keys it used were readable by any code in the process.

So a module reaches the outside only through its context: `ctx.http` for
HTTPS, held to the hosts its manifest and the account declare, with
secrets filled into one header for one host, never handed over; `ctx.com`
for a desktop program's automation, held to the ProgIDs and members the
manifest lists; `ctx.secret` for a declared secret it must hold itself (a
built-in's OAuth refresh); `ctx.zone` for the user's time zone. Core makes
the context (`core/modctx.py`); in step 6 the same interface is served to a
sandboxed module over the broker, so a module runs unchanged in either.
"""

from __future__ import annotations

import datetime as dt
import json as _json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class Response:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)  # names lower-cased
    content: bytes = b""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.content or b"null")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def redirect(self) -> str | None:
        return self.headers.get("location") if self.status in (301, 302, 303, 307, 308) else None


@dataclass(frozen=True)
class Account:
    """The account a provider serves: its opaque key, the name the user gave
    it, its address, and its non-secret fields (hosts, a preset, settings)."""

    key: str
    name: str
    module: str
    address: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    preset: str | None = None


@dataclass(frozen=True)
class SignedIn:
    """What a built-in's browser sign-in hands core to keep: the secret
    (`manifest.SIGN_IN`), the address that signed in, which core checks is
    the account's, and when the sign-in ends, if the service said."""

    secret: str
    address: str
    ends: dt.datetime | None = None


class Http(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                content: bytes | str | None = None, json: Any = None,
                params: dict[str, str] | None = None, timeout: float | None = None,
                follow: bool = False) -> Response:
        """One request, under the module's host rules. A header value may
        hold `{{secret:NAME}}`, filled only where NAME is bound. `follow`
        follows redirects one hop at a time, each checked again.

        Any status is an answer, not an error. A host that cannot be
        reached or does not answer in time raises `Transient`; a certificate
        that is not trusted, a `ConnectorError`; a request the host rules
        refuse, a `PermissionError`."""


class Com(Protocol):
    def open(self, progid: str) -> Any:
        """The root object of a ProgID the manifest lists."""


class Context(Protocol):
    account: Account
    http: Http
    com: Com
    state_dir: Path

    @property
    def zone(self) -> dt.tzinfo:
        """The user's time zone."""

    def secret(self, name: str) -> str | None:
        """A secret the manifest declares, by name; None if unset."""

    def log(self, text: str) -> None:
        """A line for the module's log, capped and redacted by core."""
