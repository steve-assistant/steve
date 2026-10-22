"""The app's sign-in: a session the app holds while the person who set
Steve up is at the machine.

The token in the run file lets the app talk to the backend, and any
program of the user's can read it; the passphrase guards only spending.
So whoever sat down at a laptop left unlocked saw everything Steve holds,
and could answer every card. Now, once a passphrase is set, the app asks
for a sign-in before it shows anything (security model, section 4.2): the
passphrase, or a glance at Windows Hello (core/hello.py). The backend gives
a session for it, and an answer to a card from the app needs one live.

What ends it:
- The app ends it when Windows locks, the laptop sleeps, or the app quits
  (`ui/src-tauri/src/signin.rs`); and after 30 minutes idle if the user
  turned that on (the `SIGN_OUT_IDLE` switch, which the keep-alive's
  answer carries, so a change counts within a minute).
- An app that stops saying it is alive (`ALIVE_S`) lets it lapse: an app
  that crashed, or a laptop that slept before the app could say so.
- A restart of the backend: the session is in this process's memory only.

One session at a time: signing in again replaces it. Only its digest is
kept, compared in constant time.

Routes, the app's alone:
- `GET /signin/state`: whether a sign-in is needed, whether Hello is set
  up, and the name of this install's Hello key.
- `POST /signin/passphrase` `{"passphrase"}`: a session, for the passphrase
  (its wrong tries count with a card's, core/passphrase.py).
- `POST /signin/challenge` `{"for": "signin"}`, `{"for": "yes",
  "action_id"}`, `{"for": "yes", "admission"}` (a module's card) or
  `{"for": "reset"}`: what Windows Hello is to sign (core/hello.py); a
  module's signature goes to `/control/modules`, a reset's to
  `/approvals/passphrase/reset`.
- `POST /signin/hello` `{"nonce", "signature"}`: a session, for a signature.
- `POST /signin/alive` and `POST /signin/end`, each `{"session"}`; the
  first answers `{"live", "sign_out_idle"}`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import offloop, runtime
from agent.worker import bodies
from core import hello, knobs, lifecycle, monitor, passphrase

# How long a session lasts without the app saying it is alive. The app says
# so every minute (signin.rs); three missed is an app that is gone.
ALIVE_S = 180

# The live session's digest and when the app last said it was alive.
_session: dict = {"digest": None, "seen": 0.0}


def required() -> bool:
    """Whether the app must sign in: once there is a passphrase to sign in
    with. Before, during setup, there is nothing to sign in with yet."""
    return passphrase.is_set()


def start(*, now: float | None = None) -> str:
    """A new session, replacing any other; its secret, which only the app
    that signed in holds."""
    given = secrets.token_urlsafe(32)
    _session.update(digest=_digest(given), seen=time.time() if now is None else now)
    return given


def live(given: object, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    digest = _session["digest"]
    return (isinstance(given, str) and digest is not None and now - _session["seen"] < ALIVE_S
            and hmac.compare_digest(_digest(given), digest))


def held(*, now: float | None = None) -> bool:
    """Whether the app holds a live session at all: for what no request
    asks for, the hands-free microphone."""
    now = time.time() if now is None else now
    return _session["digest"] is not None and now - _session["seen"] < ALIVE_S


def signed_in(given: object) -> bool:
    """Whether a request carrying `given` comes from the person who set Steve
    up: no sign-in is needed yet, or it is the live session. Reads the
    passphrase's file."""
    return not required() or live(given)


def end() -> None:
    _session.update(digest=None, seen=0.0)


async def refused(body: dict) -> JSONResponse | None:
    """The refusal of an answer to a card that comes without the app's live
    session, or None when it may go on. Every route that answers a card
    asks here: an action's (`/actions/decide`), a module's or a server's,
    and a build's allowance (`/control/modules`). Until step 10 only the
    first did, so signed out, a module's card still took a yes; the refusal
    says what the app should ask for (`needs`), and the app signs in again."""
    if not await asyncio.to_thread(signed_in, body.get("session")):
        return JSONResponse({"ok": False, "error": "sign in to Steve again first",
                             "needs": "signin"}, status_code=400)
    return None


def _digest(given: str) -> str:
    return hashlib.sha256(given.encode()).hexdigest()


async def state(_: Request) -> JSONResponse:
    return JSONResponse({"required": await asyncio.to_thread(required),
                         "gone": await asyncio.to_thread(passphrase.state) == "gone",
                         "hello": await asyncio.to_thread(hello.is_set),
                         "hello_name": await asyncio.to_thread(hello.key_name)})


async def with_passphrase(request: Request) -> JSONResponse:
    given = str((await bodies.body(request)).get("passphrase") or "")
    if why := await asyncio.to_thread(passphrase.check, given):
        return bodies.bad(why)
    return JSONResponse({"ok": True, "session": start()})


async def challenge(request: Request) -> JSONResponse:
    body = await bodies.body(request)
    purpose = body.get("for")
    if purpose in ("signin", "reset"):
        binding = ""
    elif purpose == "yes":
        # A module's card or an action's: each binds its own, and a module's
        # binding is named so, so one card's signature approves no other.
        card, bind = ((body.get("admission"), lifecycle.hello_binding) if "admission" in body
                      else (body.get("action_id"), monitor.hello_binding))
        try:
            card = int(card)
        except (TypeError, ValueError):
            return bodies.bad("which card?")
        binding = await offloop.with_own_conn(runtime.conn, bind, card)
        if binding is None:
            return bodies.bad("that card is gone")
    else:
        return bodies.bad("a challenge is for signing in, a yes, or a reset")
    nonce, message = hello.challenge(purpose, binding)
    return JSONResponse({"ok": True, "nonce": nonce,
                         "message": base64.b64encode(message).decode()})


async def with_hello(request: Request) -> JSONResponse:
    body = await bodies.body(request)
    if why := hello.verify(str(body.get("nonce") or ""), str(body.get("signature") or ""),
                           "signin", ""):
        return bodies.bad(why)
    return JSONResponse({"ok": True, "session": start()})


async def alive(request: Request) -> JSONResponse:
    given = (await bodies.body(request)).get("session")
    if not live(given):
        return JSONResponse({"live": False})
    _session["seen"] = time.time()
    return JSONResponse({"live": True, "sign_out_idle": knobs.on("SIGN_OUT_IDLE")})


async def finish(request: Request) -> JSONResponse:
    # Only the session's holder ends it: a stale app's end must not end the
    # app signed in now.
    if live((await bodies.body(request)).get("session")):
        end()
    return JSONResponse({"ok": True})


ROUTES = [
    Route("/signin/state", state),
    Route("/signin/passphrase", with_passphrase, methods=["POST"]),
    Route("/signin/challenge", challenge, methods=["POST"]),
    Route("/signin/hello", with_hello, methods=["POST"]),
    Route("/signin/alive", alive, methods=["POST"]),
    Route("/signin/end", finish, methods=["POST"]),
]
