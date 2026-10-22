"""Shared plumbing for the Google tools.

Service objects are cached per API because `build()` fetches and parses a
discovery document, which is slow enough to notice on a voice turn. They hold
credentials that refresh in place, so a long-lived cache is correct rather
than merely convenient. Their connection is not shared: each thread has its
own (see `RetryingRequest.http`), because a shared one crashed the worker.

Every tool here returns a dict on failure instead of raising. A 403 from
Google is information the model should read and relay -- "that calendar is not
shared with you" -- not an exception that aborts the turn. The slot's own
errors (`steve_sdk.errors`) pass through: they are already said once, by core.

Which account a call is for is set around it (`as_account`), by its
provider (`provider.py`). Until step 5's item 9 there was one Google
account, read from one stored token, and these functions took no account:
a second could not exist. The functions keep that shape, and each call
reads the account its provider set, in its own thread or task.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import httplib2
from google.auth.exceptions import RefreshError
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, build_http

from steve_sdk.errors import ConnectorError, NotConnected

from .auth import Account

# Per-mailbox and per-project quotas are generous for one user, but a
# runaway loop is still worth bounding.
MAX_RESULTS = 100

# How often a read is retried after a dropped connection.
READ_RETRIES = 2


class RetryingRequest(HttpRequest):
    """A Google request that retries a read whose connection dropped.

    The client is cached (below), and the connection inside it does not
    survive the laptop sleeping: the first call after waking failed with
    ConnectionAbortedError or ConnectionResetError -- 27 times in a few days
    of logs, each one an attention tick that saw nothing of their Gmail,
    calendar or Drive. The library already retries a dropped connection,
    and 5xx and 429 answers, but only when `execute` is given `num_retries`,
    and none of the calls here ever gave it.

    Reads only. A retried send whose first attempt did reach Google is the
    same email sent twice, and a retried insert is a duplicate draft, event
    or task.
    """

    def execute(self, http=None, num_retries=None):
        if num_retries is None:
            num_retries = READ_RETRIES if str(self.method).upper() == "GET" else 0
        return super().execute(http=http, num_retries=num_retries)

    # Every use of the connection -- `execute`, an upload's `next_chunk`, a
    # Drive download's chunks (`MediaIoBaseDownload` reads `request.http`) --
    # goes through this attribute, so answering it per thread covers them
    # all, whichever thread built the request. The shared one stays under
    # its own name in `__dict__`, where the library's `to_json` expects it.
    @property
    def http(self):
        return _own_connection(self.__dict__["http"])

    @http.setter
    def http(self, value):
        self.__dict__["http"] = value


# Each thread's own connections to Google, one per sign-in.
_connections = threading.local()


def _own_connection(shared):
    """This thread's own copy of a cached client's connection.

    The cached client came with one `httplib2.Http`, and every thread used
    it: attention sources, tool calls and the night all reach Google from
    worker threads, and httplib2 keeps one open socket per host. Two threads
    on one TLS socket is undefined behaviour in OpenSSL, and it corrupts the
    heap. The worker died of it six times between 22 and 30 September
    (access violations and heap corruption in ntdll.dll); the first dump
    faulthandler caught had four threads inside Google calls as it died.
    Eight threads on one shared `Http` crashed a test process within half a
    minute, three times out of three; one `Http` per thread ran 17,508
    requests without an error. Google's guide to this client says the same:
    httplib2 is not thread-safe, so give each thread its own.

    Per thread rather than per request, so a thread's calls still reuse
    their connection instead of paying for a TLS handshake each. A fake
    connection with no credentials (the tests') is used as it is.
    """
    credentials = getattr(shared, "credentials", None)
    if credentials is None:
        return shared
    held = getattr(_connections, "by_signin", None)
    if held is None or len(held) > 8:
        held = _connections.by_signin = {}
    own = held.get(id(credentials))
    if own is None or own.credentials is not credentials:
        own = held[id(credentials)] = AuthorizedHttp(credentials, http=_fresh_http())
    return own


class _Http(httplib2.Http):
    """httplib2's client, except that a connection a socket error killed is
    forgotten rather than tried again.

    httplib2 keeps one open connection per host, and when sending on it
    fails with anything but "network unreachable" it re-raises without
    closing it -- so the dead socket stays cached, and the library's retry
    and the next call reuse it. After the laptop wakes (and the VPN
    reconnects), every thread's cached socket is dead: on 30 September
    each of the Gmail, Calendar and Drive sources failed three tries out of
    three with WinError 10053, on and off for an hour after 11:47. With a
    socket shut under it, a plain client failed the next two requests; this
    one fails only the request that found it dead, so the retry succeeds.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._used = time.monotonic()

    def request(self, *args, **kwargs):
        if self.connections and time.monotonic() - self._used > IDLE_REUSE_S:
            self._forget()
        try:
            return super().request(*args, **kwargs)
        except OSError:  # aborted, reset, broken pipe, timed out
            self._forget()
            raise
        finally:
            self._used = time.monotonic()

    def _forget(self) -> None:
        for conn in list(self.connections.values()):
            with contextlib.suppress(Exception):
                conn.close()
        self.connections.clear()


# How long a connection may sit unused and still be reused. Healing after
# the failure (above) still cost a failed request, a back-off sleep and the
# new handshake anyway, and it was not only after sleep: the far end drops a
# kept-alive socket left idle a few minutes, and attention sources come
# round every three to ten -- 380 retries in one 13-hour worker log on 5
# October 2026, about forty an hour overnight. One left longer than this is
# closed before use: a handshake, and no failure.
IDLE_REUSE_S = 45


def _fresh_http() -> httplib2.Http:
    """A connection set up as the library's `build_http` would -- its
    timeout, and 308 not followed as a redirect -- that heals itself."""
    made = build_http()
    own = _Http(timeout=made.timeout)
    own.redirect_codes = getattr(made, "redirect_codes", own.redirect_codes)
    return own


# The account the calls in this thread or task are for: set by its provider
# around each call (`as_account`), so the functions below need no argument.
_ACCOUNT: contextvars.ContextVar[Account | None] = contextvars.ContextVar(
    "google_account", default=None)


@contextlib.contextmanager
def as_account(account: Account) -> Iterator[Account]:
    """The calls inside are `account`'s."""
    token = _ACCOUNT.set(account)
    try:
        yield account
    finally:
        _ACCOUNT.reset(token)


def current() -> Account | None:
    """The account the calls here are for, if one is set."""
    return _ACCOUNT.get()


def service(api: str, version: str):
    """A cached Google API client for the current account, whose reads
    survive a dropped connection.

    Cached per account and per sign-in as well as per API. The client holds
    the credentials it was built with, so when Google expired the sign-in on
    26 September 2026, signing in again stored a good token and changed
    nothing: every cached client kept refreshing the dead one until the
    worker was restarted. A new sign-in is a new refresh token, and a new
    cache entry; another account is another entry, and never this one's.
    """
    account = _ACCOUNT.get()
    if account is None:
        raise NotConnected("no Google account is in use here")
    signin, creds = account.signed()
    return _service(api, version, account.ctx.account.key, signin, creds)


@functools.lru_cache(maxsize=32)
def _service(api: str, version: str, key: str, signin: str, creds: Any):
    """cache_discovery=False because the default on-disk discovery cache warns
    noisily under Python 3.13 and buys nothing when the process is long-lived."""
    del key, signin  # only the cache key
    return build(api, version, credentials=creds, cache_discovery=False,
                 requestBuilder=RetryingRequest)


# What the model reads when the sign-in itself is dead. Plain, and with the
# fix, because the model's own reading of `invalid_grant` was "the token is
# expired or revoked" -- true, and no use to them on a phone. Where to sign
# in again is the account's health to say (core/health.py), not this.
SIGNED_OUT = "Google's sign-in has ended: sign in again. Nothing was changed."

# The reasons Google gives for an API the project has not turned on.
_DISABLED = ("SERVICE_DISABLED", "accessNotConfigured")


def _disabled(exc: HttpError) -> str | None:
    """The API a 403 says is not turned on in the client's project, as
    "gmail.googleapis.com"; None for any other 403."""
    try:
        said = json.loads(exc.content.decode("utf-8", "replace"))["error"]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    reasons = [d for d in [*said.get("details", []), *said.get("errors", [])]
               if isinstance(d, dict)]
    if not any(d.get("reason") in _DISABLED for d in reasons):
        return None
    for d in reasons:
        if service := (d.get("metadata") or {}).get("service"):
            return str(service)
    return ""


def explain(exc: Exception) -> dict:
    """Turn a Google exception into something a model can act on.

    The raw messages are long and XML-ish; these are the cases worth
    distinguishing, because each one implies a different next step.
    """
    if isinstance(exc, RefreshError):
        # Raised from inside a cached client when its refresh token is
        # refused: lapsed, revoked, or a client in Testing past its week.
        return {"error": SIGNED_OUT, "kind": "auth", "fixable_by_user": True}

    if isinstance(exc, HttpError):
        code = exc.resp.status
        if code == 403 and (service := _disabled(exc)) is not None:
            name = service.split(".")[0].capitalize() if service else "an API it needs"
            return {"error": f"{name} API is not turned on in the account's Google Cloud "
                             f"project. Nothing was changed.",
                    "kind": "disabled", "status": code, "service": service}
        detail = getattr(exc, "reason", None) or str(exc)
        kinds = {
            401: ("auth", "Google rejected the credentials; re-authorise."),
            403: ("permission", "Permission denied, or a quota was exceeded."),
            404: ("not_found", "That item does not exist, or is not visible."),
            429: ("rate_limit", "Rate limited by Google; try again shortly."),
        }
        kind, hint = kinds.get(code, ("http", detail))
        return {
            "error": f"{hint} ({code})",
            "kind": kind,
            "status": code,
            "detail": detail[:300],
        }

    return {"error": f"{type(exc).__name__}: {exc}"[:300], "kind": "unexpected"}


def guard(fn):
    """Wrap a tool so Google failures become results, not exceptions."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            return fn(*args, **kwargs)
        except ConnectorError:
            raise  # the slot's own, said once by core
        except HttpError as exc:
            return explain(exc)
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the turn
            return explain(exc)

    return wrapper


def clamp(value: int | None, default: int, maximum: int = MAX_RESULTS) -> int:
    if value is None:
        return default
    return max(1, min(int(value), maximum))
