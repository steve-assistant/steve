"""A Google account for tests, added as the app adds one: its client file
chosen, its classes, and a browser sign-in that opens no browser.

Google's API is faked where the tools always met it, `common.build`: a
test hands in a resource of its own, or the golden world's
(tests/unit/golden_world.py). What this file fakes is the rest of the way
in: the client file a user downloads from Google Cloud, and the sign-in
(`Flow`, in place of google-auth-oauthlib's), which answers as Google's
token endpoint does, with an ID token naming who signed in, and, for a
client in Testing, when its refresh token ends.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from typing import Any

import pytest

from core import credstore, modules
from steve_sdk.context import Account
from steve_sdk.manifest import SIGN_IN
from tests import persona

# The key today's Google connection had before item 9 made it an account
# like any other: the ids in the tests' fixtures and the golden record
# carry it.
KEY = "67d3d26cc730"
ADDRESS = persona.EMAIL
PROJECT = "steve-maya-4417"
CLIENT_ID = "584031-m4ya.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-fake-client-secret"
REFRESH = "1//0g-fake-refresh-token"


def client_file(kind: str = "installed") -> str:
    """A client file as Google Cloud's console downloads it."""
    return json.dumps({kind: {
        "client_id": CLIENT_ID, "project_id": PROJECT, "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "redirect_uris": ["http://localhost"]}})


def id_token(email: str, *, verified: bool = True) -> str:
    """An ID token as Google's token endpoint returns it; the signature is
    not read (`modules/google_cloud/auth._address`)."""
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return ".".join([part({"alg": "RS256", "typ": "JWT"}),
                     part({"iss": "https://accounts.google.com", "email": email,
                           "email_verified": verified}), "c2lnbmF0dXJl"])


class _Session:
    def __init__(self) -> None:
        self.token: dict[str, Any] = {}


class Flow:
    """google-auth-oauthlib's `InstalledAppFlow`, as a test needs it: what
    it was built with and asked, and Google's answer, or a failure."""

    def __init__(self, *, address: str = ADDRESS, ends_in_s: int | None = 604_799,
                 fail: Exception | None = None, refresh: str = REFRESH) -> None:
        self.answer: dict[str, Any] = {"access_token": "ya29.fake", "refresh_token": refresh,
                                       "id_token": id_token(address), "expires_in": 3599}
        if ends_in_s is not None:
            self.answer["refresh_token_expires_in"] = ends_in_s
        self.fail = fail
        self.config: dict[str, Any] = {}
        self.scopes: list[str] = []
        self.asked: dict[str, Any] = {}
        self.oauth2session = _Session()

    def from_client_config(self, config: dict, scopes: list[str]) -> Flow:
        self.config, self.scopes = config, list(scopes)
        return self

    def run_local_server(self, **kwargs: Any) -> None:
        self.asked = kwargs
        if self.fail is not None:
            raise self.fail
        self.oauth2session.token = dict(self.answer)


def add(conn, *, address: str = ADDRESS, classes: list[str] | None = None, key: str = KEY,
        name: str | None = None) -> str:
    """The account, added as the app adds it (`agent.accounts.add`), not yet
    signed in; its key."""
    from agent import accounts

    modules.register(conn)
    with pytest.MonkeyPatch.context() as keyed:
        keyed.setattr(modules, "new_key", lambda: key)
        return accounts.add(conn, module_id="google_cloud", address=address, wanted=classes,
                            values={"client": client_file()}, name=name)


def keep_sign_in(conn, key: str = KEY, *, ends: dt.datetime | None = None,
                 refresh: str = REFRESH) -> None:
    """What a finished sign-in keeps (`agent.accounts.sign_in`), without one."""
    held = modules.module(conn, "google_cloud")
    assert held is not None
    kept: dict[str, Any] = {"refresh_token": refresh}
    if ends is not None:
        kept["ends"] = ends.isoformat()
    credstore.write(credstore.target(held.key, key, SIGN_IN), json.dumps(kept))


def signed_in(conn, *, address: str = ADDRESS, classes: list[str] | None = None,
              key: str = KEY, name: str | None = None,
              ends: dt.datetime | None = None) -> str:
    """The account, added and signed in; its key."""
    found = add(conn, address=address, classes=classes, key=key, name=name)
    keep_sign_in(conn, found, ends=ends)
    return found


def provider(conn, klass: str, key: str = KEY):
    """The account's provider of `klass`, as the slot builds it."""
    from agent import connectors

    return next(c.provider for c in connectors.accounts(conn, klass) if c.key == key)


class Context:
    """The least context a Google provider is built from, for a test that
    fakes Google's client below `service` and needs no registry: the
    account, its client, and its sign-in."""

    def __init__(self, *, key: str = KEY, address: str = ADDRESS, refresh: str = REFRESH,
                 ends: dt.datetime | None = None) -> None:
        self.account = Account(key=key, name="gmail", module="google_cloud", address=address)
        kept: dict[str, Any] = {"refresh_token": refresh}
        if ends is not None:
            kept["ends"] = ends.isoformat()
        self.secrets = {
            "client": json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                                  "project_id": PROJECT}),
            SIGN_IN: json.dumps(kept)}

    @property
    def zone(self):
        """The user's zone, as core's context gives it."""
        from core import clock

        return clock.local_zone()

    def secret(self, name: str) -> str | None:
        return self.secrets.get(name)


def account(**kw: Any):
    """A Google account's sign-in, for a test that calls Google's functions
    directly, inside `modules.google_cloud.common.as_account`."""
    from modules.google_cloud import auth

    return auth.Account(Context(**kw))
