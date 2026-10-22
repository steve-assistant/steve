"""Google sign-in, one account at a time, on the user's own OAuth client.

Frozen, like everything under modules/. The agent is allowed to *use* these
credentials through the tools; it is not allowed to change how they are
obtained or stored.

Until step 5's item 9 there was one Google account, signed in from a
command line, and it went wrong in four ways:

* **Everything was asked for at once,** mail, calendar, Drive and Tasks,
  whatever the user wanted Steve to have. Now an account asks only for its
  classes' scopes (`scopes`), with its address as Google's hint, and the
  address that signs in must be the account's: core checks it
  (`agent/accounts.sign_in`), so a browser signed in to another account
  cannot hand Steve the wrong mailbox.
* **The whole token was kept,** as google-auth writes it: the hourly access
  token, the scopes and the client's secret again. That can pass the
  Credential Manager's 2,560 bytes. Now only the refresh token is kept,
  with when it ends; the access token stays in memory (`Account`).
* **"Testing" mode expires refresh tokens after seven days.** The countdown
  was guessed from a file of when consent was given. Google says it in the
  token's answer, `refresh_token_expires_in`, and nothing in a published
  client's: the end is kept with the token, and the account's health says
  it a day before (`core/health.py`).
* **The client file sat wherever the user kept it,** its path a key of its
  own. A client file is a secret, for its client secret: only its id, its
  secret and its project are kept, in the Credential Manager (`fields`). A
  Web client is refused: its sign-in cannot come back to this computer.

Scope class decides the rest, verified rather than assumed: `drive.file`
is non-sensitive and enough to make and edit the app's own Docs, Sheets and
Slides; `gmail.send` is merely sensitive. Only *reading* existing mail and
Drive is restricted, and reading is the point.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import threading
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from steve_sdk.context import SignedIn
from steve_sdk.errors import ConnectorError, NotConnected, SignInNeeded
from steve_sdk.manifest import SIGN_IN

# The library's INFO line is the browser's redirect, one-time code and all,
# which the worker's log kept: only its warnings go there.
logging.getLogger("google_auth_oauthlib").setLevel(logging.WARNING)

# The setup field that takes the client file (manifest.toml).
CLIENT = "client"

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CONSOLE = "https://console.cloud.google.com"

# Who signed in: asked of every sign-in, whatever its classes.
IDENTITY = ("openid", "https://www.googleapis.com/auth/userinfo.email")
# Each class's scopes. Full access, because reading mail and Drive is the
# reason to connect; `drive` rather than `drive.readonly` plus `drive.file`,
# because one broad scope is easier to reason about than two overlapping
# ones, and a consent screen that lists fewer, larger scopes is easier for a
# human to actually read.
SCOPES = {
    "mail": ("https://www.googleapis.com/auth/gmail.modify",  # read, label, archive
             "https://www.googleapis.com/auth/gmail.send"),
    "calendar": ("https://www.googleapis.com/auth/calendar",),
    "files": ("https://www.googleapis.com/auth/drive",
              "https://www.googleapis.com/auth/documents",
              "https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/presentations"),
    "tasks": ("https://www.googleapis.com/auth/tasks",),
}

# How long the browser sign-in waits for the user.
SIGN_IN_WAIT_S = 300

SIGNED_IN_PAGE = "Signed in. You can close this tab and go back to Steve."


def scopes(classes: list[str] | tuple[str, ...]) -> list[str]:
    """What a sign-in for `classes` asks for: who signed in, and those
    classes' scopes, no more."""
    return [*IDENTITY, *(s for klass in SCOPES if klass in classes for s in SCOPES[klass])]


# --- the client ------------------------------------------------------------------

def client(text: str) -> dict[str, str]:
    """The parts of a client file Steve keeps: its id, its secret and its
    project (for the console's links). Raises ValueError, in the user's words."""
    try:
        found = json.loads(text)
    except ValueError:
        raise ValueError("that is not a Google OAuth client file") from None
    if isinstance(found, dict) and "web" in found:
        raise ValueError("that is a Web application client: make a Desktop app client "
                         "instead, and choose its file")
    inner = found.get("installed") if isinstance(found, dict) else None
    if not (isinstance(inner, dict) and inner.get("client_id") and inner.get("client_secret")):
        raise ValueError("that is not a Google OAuth client file")
    return {"client_id": str(inner["client_id"]), "client_secret": str(inner["client_secret"]),
            "project_id": str(inner.get("project_id") or "")}


def fields(values: dict[str, Any]) -> dict[str, Any]:
    """Setup's fields as this module keeps them (`core/engines/code.fields`):
    the client file as its three parts, never the file."""
    out = dict(values)
    if isinstance(out.get(CLIENT), str):
        out[CLIENT] = json.dumps(client(out[CLIENT]), separators=(",", ":"))
    return out


def _client(ctx: Any) -> dict[str, str]:
    try:
        found = json.loads(ctx.secret(CLIENT) or "")
    except ValueError:
        found = None
    if not (isinstance(found, dict) and found.get("client_id")):
        raise NotConnected("its OAuth client is not kept here: remove the account and "
                           "add it again")
    return found


def console(page: str, project: str) -> str:
    """A page of the user's own project in Google Cloud's console."""
    return f"{CONSOLE}/{page}" + (f"?project={project}" if project else "")


# --- an account's sign-in ----------------------------------------------------------

# Each account's credentials, by its key and its sign-in: the access token
# lives here, in memory, and a new sign-in is a new entry.
_HELD: dict[tuple[str, str], Credentials] = {}
_LOCK = threading.Lock()


class Account:
    """One account's sign-in, for its providers: credentials built from
    what the Credential Manager keeps, read again at each use so a new
    sign-in is used at once."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def _kept(self) -> dict[str, Any]:
        try:
            found = json.loads(self.ctx.secret(SIGN_IN) or "")
        except ValueError:
            found = None
        if not (isinstance(found, dict) and found.get("refresh_token")):
            raise SignInNeeded("it is not signed in yet")
        return found

    @property
    def project(self) -> str:
        """The client's project in Google Cloud, for the console's links;
        "" when it is not known."""
        try:
            return _client(self.ctx).get("project_id") or ""
        except ConnectorError:
            return ""

    def signed(self) -> tuple[str, Credentials]:
        """Which sign-in this is (a digest of its refresh token, so the token
        itself is never a cache key), and its credentials."""
        token = str(self._kept()["refresh_token"])
        signin = hashlib.sha256(token.encode()).hexdigest()[:16]
        where = (self.ctx.account.key, signin)
        with _LOCK:
            creds = _HELD.get(where)
            if creds is None:
                made = _client(self.ctx)
                creds = _HELD[where] = Credentials(
                    None, refresh_token=token, token_uri=TOKEN_URI,
                    client_id=made["client_id"], client_secret=made["client_secret"])
        return signin, creds

    def ends(self) -> dt.datetime | None:
        """When this sign-in ends, if Google said: a client in Testing."""
        try:
            said = self._kept().get("ends")
        except ConnectorError:
            return None
        try:
            return dt.datetime.fromisoformat(said) if said else None
        except ValueError:
            return None


def _address(id_token: Any) -> str:
    """The address an ID token names. Not checked against Google's keys:
    it came straight from Google's token endpoint over TLS, which OpenID
    Connect accepts in place of the signature (Core 1.0, 3.1.3.7)."""
    try:
        payload = str(id_token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return ""
    if not isinstance(claims, dict) or claims.get("email_verified") is False:
        return ""
    return str(claims.get("email") or "")


def _not_finished(ctx: Any, exc: Exception) -> ConnectorError:
    """Why a sign-in did not finish, with the page that fixes it. A user who
    is not a test user of a client in Testing is stopped on Google's page,
    and it never comes back here: that is a wait that runs out."""
    project = _client(ctx).get("project_id") or ""
    error = getattr(exc, "error", "")
    if error == "invalid_client":
        return ConnectorError("Google did not accept the OAuth client: remove the account "
                              "and add it again with its file")
    said = ConnectorError(
        f"the sign-in did not finish. If Google said the app has not completed its "
        f"verification, add {ctx.account.address} as a test user, then sign in again")
    said.fix = console("auth/audience", project)
    return said


def sign_in(ctx: Any, *, classes: list[str] | tuple[str, ...],
            open_browser: bool = True) -> SignedIn:
    """The browser sign-in, for `classes`, with the account's address as
    the hint; what to keep. Blocking, until the user finishes or
    `SIGN_IN_WAIT_S` runs out.

    `prompt="consent"` is deliberate: without it Google leaves out the
    refresh token on any sign-in after the first, and the result works until
    the access token expires an hour later and then mysteriously stops."""
    made = _client(ctx)
    flow = InstalledAppFlow.from_client_config(
        {"installed": {"client_id": made["client_id"], "client_secret": made["client_secret"],
                       "auth_uri": AUTH_URI, "token_uri": TOKEN_URI,
                       "redirect_uris": ["http://localhost"]}},
        scopes(classes))
    try:
        flow.run_local_server(
            host="localhost", port=0, open_browser=open_browser,
            timeout_seconds=SIGN_IN_WAIT_S, authorization_prompt_message="",
            success_message=SIGNED_IN_PAGE, access_type="offline", prompt="consent",
            login_hint=ctx.account.address)
    except Exception as exc:  # noqa: BLE001 - every way it stops is said the same way
        raise _not_finished(ctx, exc) from exc
    token = dict(getattr(flow.oauth2session, "token", None) or {})
    if not token.get("refresh_token"):
        raise ConnectorError("Google sent no refresh token: sign in again")
    kept: dict[str, Any] = {"refresh_token": token["refresh_token"]}
    ends = None
    if (left := token.get("refresh_token_expires_in")) is not None:
        ends = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(left))
        kept["ends"] = ends.isoformat()
    return SignedIn(secret=json.dumps(kept), address=_address(token.get("id_token")),
                    ends=ends)
