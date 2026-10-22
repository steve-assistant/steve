"""A module's context: the only ways out core gives a module's code.

A provider opened its own connections, to whatever host it named, read its
own secrets, and drove Outlook through whatever COM object it reached. So a
module told "summarise my inbox" by a crafted mail could have sent it
anywhere, with nothing in between that knew which hosts it was meant to
reach. This holds it to what its manifest and its account declare, in code
(05-connectors.md, section 1):

- **Hosts.** A host the manifest fixes is public; a host the user typed into
  the app carries the kind of address it was pinned to at setup (public,
  private, loopback; loopback only for the MCP and IMAP engines). Each is
  vetted by `core/egress.vet`, the one check for every way out, and the
  connection goes to the address it vetted, so a name cannot change between
  the check and the connect.
- **Failures** are the SDK's errors, since a module imports only
  `steve_sdk` and could not name httpx's: a host that does not answer is
  `Transient`, and a certificate that is not trusted a `ConnectorError`,
  which the next try would not pass either. A request the rules here
  refuse is `Refused`, a `PermissionError`.
- **Redirects** are never followed on their own: each hop is checked again,
  HTTPS to HTTP is refused, and a credential header never crosses to another
  origin.
- **Methods.** Code in the sandbox reaches each host with the methods its
  manifest declares for it, checked at every hop, and over HTTPS alone
  (`core/sandbox/broker.py`); a built-in's context checks neither.
- **Headers.** A module may not set `Host`, `Forwarded`, `X-Forwarded-*`,
  `X-Original-URL`, or a hop-by-hop header, nor any value holding CR, LF or
  NUL.
- **Secrets** are filled in, not handed over: `{{secret:NAME}}` in a header
  becomes the value only where NAME is bound to that header, host and port,
  over HTTPS. On a redirect elsewhere the header is dropped. Secrets never go
  in a URL (`steve_sdk.manifest.check_url_template`).
- **COM** opens only the ProgIDs the manifest lists, never one on the
  deny-list, and reaches only the member paths it lists. A wildcard reads
  plain values: it never sets one, never hands over an object, and never
  covers a member that sends, saves, deletes or runs. A listed path is the
  end of the path from the root (`Items.GetFirst`): every step on the way
  must itself be listed, so what a module reaches is what its list
  connects, and a walk down folders (`Folders.Item`, again and again) is
  one line, not one per depth.

`Network` is where connections actually go -- DNS, sockets, and for tests an
HTTP transport per host -- so tests replace only that, and every rule above
still runs.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import re
import socket
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import httpcore
import httpx

from core import clock, credstore, egress, logscrub
from steve_sdk.context import Account, Response
from steve_sdk.errors import ConnectorError, Transient
from steve_sdk.manifest import (
    DENIED_PROGIDS,
    Manifest,
    SecretBinding,
    acting,
    check_url_template,
    known,
    never,
    template_fields,
)

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024
TIMEOUT_S = 30.0
MAX_LOG = 500

# What a module or its server logs that is a credential: anything named or
# shaped as one (`core/logscrub.NAMED`: a JWT, a bearer value, a named token
# or secret), and any long unbroken run of token characters (refresh tokens,
# keys). A sign-in code is short, and is read from the raw line before this
# (`core/sandbox/servers.Log.watch`).
_SECRET_SHAPES = re.compile(rf"{logscrub.NAMED.pattern}|[A-Za-z0-9_~+/=.-]{{40,}}")


def scrub(text: str) -> str:
    """`text` with anything shaped like a credential replaced: every module
    log line, and a local server's stderr. By shape alone, since a server's
    tokens are its own and Steve never knew them, and with no model to load:
    the language-model redactor (`agent/redact.py`) is an optional extra,
    and without it a module's first log line failed its call."""
    return _SECRET_SHAPES.sub("[secret]", text)

FORBIDDEN_HEADERS = frozenset({
    "host", "forwarded", "x-original-url", "x-rewrite-url",
    # hop-by-hop (RFC 9110, 7.6.1) and their proxy cousins
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
})
FORBIDDEN_PREFIXES = ("x-forwarded-",)
# Headers that carry a credential, dropped on any hop to another origin.
CREDENTIAL_HEADERS = frozenset({"authorization", "cookie"})
SECRET = re.compile(r"\{\{secret:([a-z][a-z0-9_]{0,31})\}\}")

def _unreachable(host: str, exc: httpx.TransportError) -> ConnectorError:
    """A connection that failed, in the SDK's words. A certificate that is
    not trusted will not be trusted on the next try either; anything else on
    the way may pass, so it is tried again later."""
    if "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return ConnectorError(f"{host}'s certificate is not trusted")
    if isinstance(exc, httpx.TimeoutException):
        return Transient(f"{host} did not answer in time")
    return Transient(f"could not reach {host} ({type(exc).__name__})")


class Refused(PermissionError):
    """A way out a module may not take, and why."""


# --- where connections go ------------------------------------------------

class Network:
    """DNS and sockets, and, for tests, an HTTP transport per host. The one
    seam: tests replace this and nothing else."""

    def resolve(self, host: str, port: int) -> list[str]:
        return egress._resolve(host, port)

    def open_tcp(self, address: str, port: int, timeout: float) -> socket.socket:
        return socket.create_connection((address, port), timeout=timeout)

    def transport_for(self, host: str) -> httpx.BaseTransport | None:
        return None

    def own_ports(self) -> set[int]:
        return egress._own_ports()

    def tls(self) -> ssl.SSLContext:
        """What a TLS connection is verified against: the system's
        authorities, the name checked. A test trusts its own authority
        here; nothing turns verification off (05-connectors.md, section 4)."""
        return ssl.create_default_context()

    def node_authorities(self) -> str | None:
        """More authorities, as PEM, that a Node process Steve starts
        trusts besides its own: a test's authority, as `tls` is Python's.
        None here: a local server trusts what Node ships."""
        return None


@dataclass(frozen=True)
class Hosts:
    """The hosts one account's module may reach: those its manifest fixes
    (public; `*.example.com` covers names under it), and those the user
    typed, each with the kind it was pinned to."""

    fixed: tuple[str, ...] = ()
    typed: dict[str, str] = field(default_factory=dict)

    def kind(self, host: str) -> str | None:
        name = (host or "").lower().rstrip(".")
        if name in self.typed:
            return self.typed[name]
        for pattern in self.fixed:
            if pattern.startswith("*."):
                if name.endswith(pattern[1:]):
                    return egress.PUBLIC
            elif name == pattern:
                return egress.PUBLIC
        return None


# The engines whose typed hosts may be this computer: a mail bridge the
# user runs (IMAP), and the MCP server they start (05-connectors.md, section 4).
LOOPBACK_ENGINES = frozenset({"mcp", "imap"})


def hosts_for(manifest: Manifest, account: Account) -> Hosts:
    """The hosts an account of `manifest` may reach. Typed hosts come from
    the account's `hosts` field, host to pinned kind, written at setup;
    loopback only where the module is built on the MCP or IMAP engine."""
    engines = {spec.engine for spec in manifest.specs.values()}
    typed = {}
    for host, kind in dict(account.fields.get("hosts") or {}).items():
        if kind not in egress.KINDS:
            continue
        if kind == egress.LOOPBACK and not engines & LOOPBACK_ENGINES:
            continue
        typed[str(host).lower().rstrip(".")] = kind
    return Hosts(fixed=manifest.fixed_hosts(account.preset), typed=typed)


def expand(template: str, values: dict[str, Any], *, secrets: tuple[str, ...] = ()) -> str:
    """A manifest's URL template filled with an account's non-secret fields,
    each percent-encoded, into its path and query only."""
    check_url_template(template, secrets, "url")
    out = template
    for name in template_fields(template):
        if name not in values:
            raise Refused(f"the URL needs {name!r}, which the account does not have")
        out = out.replace("{" + name + "}", quote(str(values[name]), safe="@"))
    return out


# --- HTTP ----------------------------------------------------------------------

class _PinnedBackend(httpcore.SyncBackend):
    """Connects to the address `vet` returned for a host, never to the name."""

    def __init__(self, pins: dict[tuple[str, int], str]) -> None:
        self._pins = pins

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        address = self._pins.get((str(host).lower(), int(port)))
        if address is None:
            raise httpcore.ConnectError(f"{host}:{port} was not vetted")
        return super().connect_tcp(address, port, timeout, local_address, socket_options)


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    port = url.port or (443 if url.scheme == "https" else 80)
    return url.scheme, (url.host or "").lower(), port


class ModuleHttp:
    """`ctx.http`: HTTPS under the module's rules (the docstring above)."""

    def __init__(self, *, hosts: Hosts, bindings: dict[str, SecretBinding],
                 secret: Callable[[str], str | None], network: Network,
                 max_bytes: int = MAX_BYTES, timeout: float = TIMEOUT_S,
                 methods: Callable[[str], tuple[str, ...]] | None = None,
                 https_only: bool = False) -> None:
        self._hosts = hosts
        self._methods = methods
        self._https_only = https_only
        self._bindings = bindings
        self._secret = secret
        self._net = network
        self._max = max_bytes
        self._timeout = timeout
        self._pins: dict[tuple[str, int], str] = {}
        self._clients: dict[int, httpx.Client] = {}
        self._lock = threading.Lock()
        context = ssl.create_default_context()
        transport = httpx.HTTPTransport(verify=context)
        # httpx takes no network backend, so its pool is swapped for one that
        # connects to the vetted address. TLS still checks the name (SNI and
        # the certificate are the URL's host, not the address).
        transport._pool = httpcore.ConnectionPool(
            ssl_context=context, network_backend=_PinnedBackend(self._pins),
            max_connections=10, max_keepalive_connections=5, keepalive_expiry=30.0)
        self._real = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)

    def _client(self, host: str) -> httpx.Client:
        transport = self._net.transport_for(host)
        if transport is None:
            return self._real
        with self._lock:
            client = self._clients.get(id(transport))
            if client is None:
                client = httpx.Client(transport=transport, follow_redirects=False,
                                      trust_env=False)
                self._clients[id(transport)] = client
            return client

    @staticmethod
    def check_headers(headers: dict[str, str]) -> None:
        for name, value in headers.items():
            low = name.strip().lower()
            if low in FORBIDDEN_HEADERS or low.startswith(FORBIDDEN_PREFIXES):
                raise Refused(f"a module may not set the {name} header")
            if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name):
                raise Refused(f"{name!r} is not a header name")
            if any(c in str(value) for c in "\r\n\0"):
                raise Refused(f"the {name} header holds a line break")

    def _check_url(self, url: httpx.URL, *, previous: str | None) -> str:
        """The pinned kind of the URL's host, or Refused."""
        scheme, host, port = _origin(url)
        if url.userinfo:
            raise Refused("a URL with credentials in it")
        kind = self._hosts.kind(host)
        if kind is None:
            raise Refused(f"{host} is not among the hosts this account may reach")
        if scheme == "http":
            if self._https_only or kind != egress.LOOPBACK:
                raise Refused(f"{host}: HTTPS only (plain HTTP only to this computer)")
            if previous == "https":
                raise Refused("a redirect from HTTPS to HTTP")
        elif scheme != "https":
            raise Refused(f"{scheme}: HTTPS only")
        if not 0 < port < 65536:
            raise Refused("a bad port")
        return kind

    def _fill(self, headers: dict[str, str], url: httpx.URL, *, first: bool) -> dict[str, str]:
        """Headers with each `{{secret:NAME}}` filled where NAME is bound to
        this header, host and port over HTTPS. Elsewhere: refused on the
        first hop (the module asked for it), dropped on a redirect."""
        scheme, host, port = _origin(url)
        out = {}
        for name, value in headers.items():
            wanted = SECRET.findall(value)
            if not wanted:
                out[name] = value
                continue
            filled = value
            for secret_name in wanted:
                bind = self._bindings.get(secret_name)
                fits = (bind is not None and scheme == "https" and host == bind.host
                        and port == bind.port and name.lower() == bind.header.lower())
                if not fits:
                    if first:
                        raise Refused(f"secret {secret_name!r} is not bound to the {name} "
                                      f"header for {host}:{port}")
                    filled = None
                    break
                secret = self._secret(secret_name)
                if secret is None:
                    raise Refused(f"secret {secret_name!r} is not set for this account")
                if any(c in secret for c in "\r\n\0"):
                    raise Refused(f"secret {secret_name!r} holds a line break")
                filled = filled.replace("{{secret:" + secret_name + "}}",
                                        bind.form.replace("{}", secret))
            if filled is not None:
                out[name] = filled
        return out

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                content: bytes | str | None = None, json: Any = None,
                params: dict[str, str] | None = None, timeout: float | None = None,
                follow: bool = False) -> Response:
        method = method.upper()
        asked = dict(headers or {})
        self.check_headers(asked)
        current = httpx.URL(url, params=params) if params else httpx.URL(url)
        previous_scheme: str | None = None
        for hop in range(egress.MAX_REDIRECTS + 1):
            kind = self._check_url(current, previous=previous_scheme)
            scheme, host, port = _origin(current)
            if self._methods is not None and method not in self._methods(host):
                raise Refused(f"{method} to {host}: the manifest declares "
                              f"{', '.join(self._methods(host))}")
            sending = self._fill(asked, current, first=hop == 0)
            vetted = egress.vet(host, port, kind=kind, resolve=self._net.resolve,
                                own_ports=self._net.own_ports)
            if isinstance(vetted, str):
                raise Refused(f"{host}: {vetted}")
            self._pins[(host, port)] = vetted[0]
            client = self._client(host)
            request = client.build_request(method, current, headers=sending, content=content,
                                           json=json, timeout=timeout or self._timeout)
            try:
                response = client.send(request, stream=True)
                try:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > self._max:
                            raise Refused(f"the answer from {host} is over {self._max:,} bytes")
                finally:
                    response.close()
            except httpx.TransportError as exc:
                raise _unreachable(host, exc) from exc
            answer = Response(status=response.status_code, url=str(current),
                              headers={k.lower(): v for k, v in response.headers.items()},
                              content=bytes(body))
            location = answer.redirect
            if not (follow and location):
                return answer
            nxt = httpx.URL(urljoin(str(current), location))
            if _origin(nxt)[:3] != (scheme, host, port):
                asked = {k: v for k, v in asked.items()
                         if k.lower() not in CREDENTIAL_HEADERS}
            if answer.status == 303 or (answer.status in (301, 302) and method == "POST"):
                method, content, json = "GET", None, None
            previous_scheme, current = scheme, nxt
        raise Refused(f"more than {egress.MAX_REDIRECTS} redirects")

    def close(self) -> None:
        self._real.close()
        for client in self._clients.values():
            client.close()


# --- COM ---------------------------------------------------------------------

_COM_THREAD = threading.local()


def _dispatch(progid: str):
    """The program's automation object, with COM initialised for this thread
    first, once. Outlook's module did that itself until step 5's item 10, around
    every call, and undid it on the way out while Python could still hold its
    objects. A worker thread that never called `CoInitialize` fails its first
    `Dispatch` with words that read like Outlook being absent."""
    import pythoncom
    import win32com.client

    if not getattr(_COM_THREAD, "ready", False):
        with contextlib.suppress(pythoncom.com_error):  # initialised already, another way
            pythoncom.CoInitialize()
        _COM_THREAD.ready = True
    return win32com.client.Dispatch(progid)


_PLAIN = (str, int, float, bool, bytes, type(None), dt.datetime, dt.date)


class ModuleCom:
    """`ctx.com`: the ProgIDs and member paths a manifest lists, no others,
    and its actions only where it `acts`. A built-in's code in the backend
    acts, keeping its own cards; a module in the sandbox acts only in
    `perform`, on the user's yes (`core/sandbox/broker.Grants.reading`).

    A call, or a value set, is an action too, unless Steve knows the program
    and knows the member only reads (`called`); and a member that runs code
    is never reached (`steve_sdk.manifest.never`)."""

    def __init__(self, progids: tuple[str, ...], members: tuple[str, ...], *,
                 actions: tuple[str, ...] = (), acts: bool = True,
                 dispatch: Callable[[str], Any] | None = None) -> None:
        self._progids = {p.lower(): p for p in progids}
        self._members = tuple(m.split(".") for m in members)
        self._actions = tuple(a.split(".") for a in actions)
        self.acts = acts
        self._dispatch = dispatch
        # The paths refused, the first few: a module's code that catches
        # every exception (Outlook's walks do, so one bad folder is not the
        # mailbox) would otherwise turn a member missing from its list into
        # an empty answer, which only a test reading this would notice.
        self.refused: list[str] = []

    def open(self, progid: str) -> Any:
        key = (progid or "").strip().lower()
        if key.startswith(DENIED_PROGIDS):
            raise Refused(f"{progid} is never opened for a module")
        if key not in self._progids:
            raise Refused(f"{progid} is not among the programs this module lists")
        # `_dispatch` is looked up here, not kept, so that the test fences'
        # door on it holds for a context built before they shut it.
        return Guarded((self._dispatch or _dispatch)(self._progids[key]), "", self, key)

    def listed(self, path: str) -> str | None:
        """How `path`, from the root, is listed: "action" when a listed
        action ends it, "named" when a listed path ends with its member by
        name, "read" when only a wildcard covers that member, None when
        nothing does. A member that sends, saves, deletes or runs is reached
        through an action alone, whatever else would match it.

        A listed path is the end of the path. Matching the whole path from
        the root, as this did until step 5's item 10, could not list a walk
        down folders at any depth, and needed every member of a mail item
        listed once for each way to reach one. Every step is still checked,
        and a wildcard only reads values (`Guarded`), so an object is reached
        only through members listed by name."""
        segments = path.split(".")
        for pattern in self._actions:
            if segments[-len(pattern):] == pattern:
                return "action"
        if acting(segments[-1]):
            return None
        how = None
        for pattern in self._members:
            if len(pattern) > len(segments):
                continue
            tail = segments[len(segments) - len(pattern):]
            # A wildcard reads: it never matches an action, nor a setter
            # (`Subject=`), each of which is listed by name.
            if all(p == s or (p == "*" and not s.endswith("=") and not acting(s))
                   for p, s in zip(pattern, tail, strict=True)):
                if pattern[-1] != "*":
                    return "named"
                how = "read"
        return how

    def reach(self, path: str) -> str:
        """How `path` is listed, or Refused: an action too, where this
        `ctx.com` does not act."""
        last = path.rsplit(".", 1)[-1]
        if never(last):
            self.refuse(path, "runs code, or opens what another program or an address "
                              "names, and is never reached by a module")
        how = self.listed(path)
        if how is None:
            self.refuse(path, "sends, saves, deletes or runs, and is not among this module's "
                              "com_actions" if acting(last)
                        else "is not among the members this module lists")
        if how == "action" and not self.acts:
            self.refuse(path, "sends, saves, deletes or runs, which a module does only in "
                              "`perform`, on the user's yes")
        return how

    def called(self, program: str, path: str) -> None:
        """A method of `program`'s called at `path`, or a value set there
        (`Subject=`): a read where Steve knows the program and knows the
        member only reads (`steve_sdk.manifest.known`), and otherwise an
        action, listed under com_actions. `Guarded` reaches `path` first,
        which refuses an action where this does not act. A name alone said
        nothing of a call until step 11's audit (B1): MMC20's
        ExecuteShellCommand, listed as a member, ran as a read."""
        if path.rsplit(".", 1)[-1].removesuffix("[]").lower() in (known(program) or ()):
            return
        if self.listed(path) != "action":
            self.refuse(path, "is a call or a change that Steve does not know only reads: "
                              "list it under com_actions, which a module reaches only on "
                              "the user's yes")

    def refuse(self, path: str, why: str) -> None:
        if len(self.refused) < 20:
            self.refused.append(path)
        raise Refused(f"{path} {why}")


class Guarded:
    """A COM object reached through `ModuleCom`, knowing its path from the
    root and its program, so every member it reaches, every call and every
    value set is checked against the manifest and the program."""

    __slots__ = ("_obj", "_path", "_com", "_program")

    def __init__(self, obj: Any, path: str, com: ModuleCom, program: str) -> None:
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_com", com)
        object.__setattr__(self, "_program", program)

    def _at(self, name: str) -> str:
        return f"{self._path}.{name}" if self._path else name

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        path = self._at(name)
        how = self._com.reach(path)
        value = getattr(self._obj, name)
        if how == "read" and not isinstance(value, _PLAIN):
            # A message's `Session` is the whole profile: a wildcard that
            # handed it over would make every short listed path apply to it.
            self._com.refuse(path, "is an object or a method, which a wildcard "
                                   "does not reach; list it by name")
        return _wrap(value, path, self._com, self._program)

    def __setattr__(self, name: str, value: Any) -> None:
        path = self._at(name) + "="
        self._com.reach(path)
        self._com.called(self._program, path)
        setattr(self._obj, name, value._obj if isinstance(value, Guarded) else value)

    def __call__(self, *args: Any) -> Any:
        self._com.called(self._program, self._path)
        plain = [a._obj if isinstance(a, Guarded) else a for a in args]
        return _wrap(self._obj(*plain), self._path, self._com, self._program)

    def __iter__(self):
        for child in self._obj:
            yield _wrap(child, self._path + "[]", self._com, self._program)


def _wrap(value: Any, path: str, com: ModuleCom, program: str) -> Any:
    return value if isinstance(value, _PLAIN) else Guarded(value, path, com, program)


# --- the context -------------------------------------------------------------

def local_zone() -> dt.tzinfo:
    """The user's zone by its IANA name (`core.clock.local_zone`), asked
    there at each call so that pinning it pins this. Until step 5's item 9
    it was the machine's fixed offset, which put a module's events past a
    daylight-saving change an hour out."""
    return clock.local_zone()


class ModuleContext:
    """What one account of one module is handed (steve_sdk.context.Context)."""

    def __init__(self, *, manifest: Manifest, module_key: str, account: Account,
                 state_dir: Path, network: Network | None = None,
                 zone: Callable[[], dt.tzinfo] = local_zone,
                 dispatch: Callable[[str], Any] | None = None) -> None:
        self.manifest = manifest
        self.module_key = module_key
        self.account = account
        self.state_dir = state_dir
        self.network = network or Network()
        self._zone = zone
        self._said: set[str] = set()
        self.hosts = hosts_for(manifest, account)
        bindings = {f.name: f.bind for f in manifest.setup.fields if f.bind is not None}
        self.http = ModuleHttp(hosts=self.hosts, bindings=bindings, secret=self._read,
                               network=self.network)
        self.com = ModuleCom(manifest.com, manifest.com_members,
                             actions=manifest.com_actions, dispatch=dispatch)
        self._log = logging.getLogger(f"steve.module.{manifest.id}")

    @property
    def zone(self) -> dt.tzinfo:
        return self._zone()

    def _read(self, name: str) -> str | None:
        if name not in self.manifest.secrets:
            raise Refused(f"{self.manifest.id} declares no secret named {name!r}")
        value = credstore.read(credstore.target(self.module_key, self.account.key, name))
        if value:
            self._said.add(value)
        return value

    def secret(self, name: str) -> str | None:
        return self._read(name)

    def log(self, text: str) -> None:
        line = str(text)
        for value in self._said:
            line = line.replace(value, "[secret]")
        self._log.info("%s: %s", self.account.name, scrub(line[:MAX_LOG]))

    def tcp(self, host: str, port: int, *, timeout: float) -> socket.socket:
        """A vetted TCP connection, for core's engines (IMAP, SMTP)."""
        kind = self.hosts.kind(host)
        if kind is None:
            raise Refused(f"{host} is not among the hosts this account may reach")
        vetted = egress.vet(host, port, kind=kind, resolve=self.network.resolve,
                            own_ports=self.network.own_ports)
        if isinstance(vetted, str):
            raise Refused(f"{host}: {vetted}")
        return self.network.open_tcp(vetted[0], port, timeout)

    def url(self, template: str, values: dict[str, Any]) -> str:
        return expand(template, values, secrets=self.manifest.secrets)

    def server(self, package: Any, *, watch: Callable[[str], None] | None = None) -> Any:
        """The account's local MCP server (`core/sandbox/servers.Pool`), an
        MCP client over its stdio, started in its container if it is not
        running. Blocking. What it says is kept with this account's secrets
        and anything shaped like a credential taken out (`scrub`):
        a server prints its own tokens, which Steve never knew."""
        from core.sandbox import servers

        return servers.POOL.client(self.manifest.id, self.account.key, package,
                                   network=self.network, redact=self._scrub, watch=watch)

    def _scrub(self, text: str) -> str:
        for value in self._said:
            text = text.replace(value, "[secret]")
        return scrub(text)

    def close(self) -> None:
        self.http.close()
