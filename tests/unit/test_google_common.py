"""A Google read survives the connection the laptop's sleep broke; a send
is never repeated; an expired sign-in is reported, and a new one is used.

The cached client's connection did not survive sleep, and the first call
after waking failed -- 27 attention ticks in a few days of logs saw nothing of
their Gmail, calendar or Drive. The library retries a dropped connection only
when asked, and nothing asked.
"""

from __future__ import annotations

import inspect
import json
import threading
import time

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from google_auth_httplib2 import AuthorizedHttp

from modules.google_cloud import auth
from modules.google_cloud import common as google_common
from steve_sdk.errors import NotConnected
from steve_sdk.manifest import SIGN_IN
from tests.fakes import google as fake_google


class _Flaky:
    """An HTTP layer whose first `drops` requests find the connection gone."""

    def __init__(self, drops: int = 1, status: int = 200) -> None:
        self.drops = drops
        self.status = status
        self.calls = 0

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        self.calls += 1
        if self.calls <= self.drops:
            raise ConnectionAbortedError(10053, "An established connection was aborted")
        return httplib2.Response({"status": str(self.status)}), b'{"ok": true}'


def _request(http, method="GET"):
    req = google_common.RetryingRequest(
        http, lambda resp, content: content, "https://gmail.googleapis.com/x", method=method)
    req._sleep = lambda seconds: None  # the library backs off between tries
    return req


def test_a_read_after_the_laptop_slept_is_retried():
    http = _Flaky(drops=1)

    assert _request(http).execute() == b'{"ok": true}'
    assert http.calls == 2


def test_a_send_is_never_repeated():
    """A dropped connection after Google took the send would, retried, be
    the same email twice."""
    http = _Flaky(drops=1)

    with pytest.raises(ConnectionAbortedError):
        _request(http, method="POST").execute()
    assert http.calls == 1


def test_a_read_that_keeps_failing_still_fails():
    http = _Flaky(drops=10)

    with pytest.raises(ConnectionAbortedError):
        _request(http).execute()
    assert http.calls == google_common.READ_RETRIES + 1


# ---------------------------------------------------------------------------
# One connection per thread (22-30 September 2026: six native crashes)
# ---------------------------------------------------------------------------


class _Fresh:
    """Credentials that never need refreshing."""

    def before_request(self, request, method, url, headers):
        headers["authorization"] = "Bearer fresh"


class _Socket(_Flaky):
    """A connection that notes which threads used it."""

    def __init__(self) -> None:
        super().__init__(drops=0)
        self.threads: set[int] = set()

    def request(self, *args, **kwargs):
        self.threads.add(threading.get_ident())
        return super().request(*args, **kwargs)


def test_each_thread_reaches_google_over_its_own_connection(monkeypatch):
    """The cached client's one connection was used by every thread at once,
    and two threads on one TLS socket corrupt OpenSSL's heap: the worker
    died of it six times in eight days. A shared `Http` crashed a test
    process in seconds; one per thread never did."""
    made: list[_Socket] = []
    monkeypatch.setattr(google_common, "_fresh_http", lambda: made.append(_Socket()) or made[-1])
    shared = AuthorizedHttp(_Fresh(), http=_Socket())

    def two_reads():
        for _ in range(2):
            assert _request(shared).execute() == b'{"ok": true}'

    threads = [threading.Thread(target=two_reads) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert shared.http.calls == 0
    assert sorted(len(s.threads) for s in made) == [1, 1, 1]
    assert [s.calls for s in made] == [2, 2, 2]  # reused within a thread


def test_a_new_sign_in_gets_its_own_connection(monkeypatch):
    """Clients are cached per sign-in; a thread's connection must carry the
    credentials of the client it is for, not the last one it served."""
    monkeypatch.setattr(google_common, "_fresh_http", _Socket)
    old, new = AuthorizedHttp(_Fresh(), http=_Socket()), AuthorizedHttp(_Fresh(), http=_Socket())

    assert _request(old).http is _request(old).http
    assert _request(new).http is not _request(old).http
    assert _request(new).http.credentials is new.credentials


class _DeadConnection:
    """A cached connection whose socket the laptop's sleep killed."""

    sock = object()  # still there, as far as httplib2 knows
    timeout = None

    def __init__(self) -> None:
        self.closed = False

    def request(self, *args, **kwargs):
        raise ConnectionAbortedError(10053, "An established connection was aborted")

    def close(self) -> None:
        self.closed = True


def test_a_connection_a_socket_error_killed_is_not_tried_again():
    """httplib2 re-raises a send that failed on a dead socket without closing
    it, so the library's retries and the next call used the same socket:
    on 30 September Gmail, Calendar and Drive each failed three tries out of
    three after the laptop woke, on and off for an hour."""
    http = google_common._fresh_http()
    dead = _DeadConnection()
    http.connections["https:gmail.googleapis.com"] = dead

    with pytest.raises(ConnectionAbortedError):
        http.request("https://gmail.googleapis.com/gmail/v1/users/me/messages")

    assert dead.closed and http.connections == {}


def test_a_connection_left_idle_is_closed_before_it_is_used_not_after_it_fails(monkeypatch):
    """The far end drops a kept-alive socket left idle a few minutes, and
    attention sources come round every three to ten: healing after the
    failure was 380 failed requests and back-off sleeps in one 13-hour log
    (5 October 2026). One in steady use is kept, so a burst of calls still
    shares its handshake."""
    import httplib2

    sent_on: list[dict] = []
    monkeypatch.setattr(httplib2.Http, "request",
                        lambda self, *a, **k: sent_on.append(dict(self.connections)) or ("ok", b""))
    http = google_common._fresh_http()
    idle = _DeadConnection()
    http.connections["https:gmail.googleapis.com"] = idle
    http._used = time.monotonic() - google_common.IDLE_REUSE_S - 1

    http.request("https://gmail.googleapis.com/gmail/v1/users/me/messages")
    assert idle.closed and sent_on == [{}]

    busy = _DeadConnection()
    http.connections["https:gmail.googleapis.com"] = busy
    http.request("https://gmail.googleapis.com/gmail/v1/users/me/messages")
    assert not busy.closed and sent_on[-1] == {"https:gmail.googleapis.com": busy}


def test_a_threads_connection_is_the_self_healing_one_set_up_like_the_librarys():
    """The library's timeout, and 308 not followed as a redirect: Drive's
    resumable uploads answer with one."""
    own = google_common._own_connection(AuthorizedHttp(_Fresh(), http=_Socket()))

    assert isinstance(own.http, google_common._Http)
    assert own.http.timeout and 308 not in own.http.redirect_codes


def test_a_request_still_serialises_as_the_library_expects():
    request = _request(AuthorizedHttp(_Fresh(), http=_Socket()))

    assert json.loads(request.to_json())["uri"] == "https://gmail.googleapis.com/x"


def test_every_client_is_built_with_the_retrying_request(monkeypatch):
    built = {}

    def build(api, version, **kwargs):
        built.update(kwargs)
        return object()

    monkeypatch.setattr(google_common, "build", build)
    google_common._service.cache_clear()
    try:
        with google_common.as_account(fake_google.account()):
            google_common.service("gmail", "v1")
    finally:
        google_common._service.cache_clear()

    assert built["requestBuilder"] is google_common.RetryingRequest


# ---------------------------------------------------------------------------
# The sign-in (26 September 2026: Google's seven-day limit for an app in
# Testing expired it mid-conversation)
# ---------------------------------------------------------------------------


def test_an_expired_sign_in_reads_as_one_the_user_has_to_renew():
    """The model read the raw `invalid_grant` as "the token is expired or
    revoked" and told them so. What the user needs is what to do, and that
    nothing happened. Where to do it is the account's health to say: a
    command to run in a terminal was no use to them on a phone."""
    dead = RefreshError("invalid_grant: Token has been expired or revoked.")

    said = google_common.explain(dead)

    assert said["kind"] == "auth" and said["fixable_by_user"] is True
    assert "sign in again" in said["error"] and "Nothing was changed" in said["error"]
    assert "python -m" not in said["error"]
    assert google_common.guard(lambda: (_ for _ in ()).throw(dead))() == said


@pytest.fixture
def clients(monkeypatch):
    """`service` for one account, whose sign-in the test changes."""
    monkeypatch.setattr(google_common, "build", lambda *a, **k: object())
    google_common._service.cache_clear()
    ctx = fake_google.Context()

    def sign_in(refresh_token: str) -> None:
        ctx.secrets[SIGN_IN] = json.dumps({"refresh_token": refresh_token})

    with google_common.as_account(auth.Account(ctx)):
        yield sign_in
    google_common._service.cache_clear()


def test_signing_in_again_is_used_without_a_restart(clients):
    """The cached client kept the dead credentials: a fresh login wrote a good
    token to disk, and every call still failed until the worker restarted."""
    clients("dead-refresh-token")
    before = google_common.service("calendar", "v3")

    clients("new-refresh-token")

    assert google_common.service("calendar", "v3") is not before


def test_the_same_sign_in_does_not_rebuild_every_client(clients):
    """The hourly access token is refreshed in memory and the refresh token
    stays; rebuilding a client for each would fetch a discovery document
    every time."""
    clients("same")
    before = google_common.service("calendar", "v3")

    clients("same")

    assert google_common.service("calendar", "v3") is before


def test_a_call_with_no_account_reaches_no_sign_in():
    """With no account set around it, a call has no sign-in to use, and
    says so: before item 9 it used the one sign-in there was."""
    with pytest.raises(NotConnected):
        google_common.service("gmail", "v1")


# Public functions in the Google modules that are deliberately not guarded,
# each with the reason. Anything else that talks to Google must be.
UNGUARDED = {
    "modules.google_cloud.calendar": {
        "finished": "pure: filters events it is given",
    },
}


@pytest.mark.parametrize("name", ["modules.google_cloud.calendar", "modules.google_cloud.gmail",
                                  "modules.google_cloud.drive", "modules.google_cloud.tasks"])
def test_every_google_function_reports_failure_instead_of_raising(name):
    """`@guard` once slid off `create` onto a helper when two functions were
    inserted above it. An unguarded create raised on the expired sign-in and
    took the whole turn down with it."""
    import importlib

    module = importlib.import_module(name)
    allowed = UNGUARDED.get(name, {})
    bare = [
        fn_name for fn_name, fn in inspect.getmembers(module, inspect.isfunction)
        if fn.__module__ == name and not fn_name.startswith("_")
        and fn_name not in allowed and not hasattr(fn, "__wrapped__")
    ]

    assert bare == []
