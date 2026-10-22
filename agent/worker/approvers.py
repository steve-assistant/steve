"""Who may approve: the passphrase and the authenticator, set in the app.

A high-risk card -- spending money -- takes a second factor
(core/monitor.py): the passphrase when it is answered in the app, the
authenticator's code when it is answered over a channel. Until step 4 the
authenticator was set up by a command at a terminal (`python -m core.totp
setup`), which a user of the app does not have, and the app needed nothing.

These routes, the app's alone, set them up:
- `GET /approvals/setup`: which of the two is set, and until when codes
  are not taken after five wrong ones, or a count that failed (`codes_paused_until`).
- `POST /approvals/passphrase` `{"new", "old"}`: chosen, or changed with
  the old one.
- `POST /approvals/authenticator` `{"passphrase"}`: a new secret to add to
  the authenticator app, as a QR code and as text, held here for ten
  minutes and written nowhere yet. It takes the passphrase: whoever can
  replace the authenticator can approve from the phone.
- `POST /approvals/authenticator/confirm` `{"code"}`: a code the app made
  from it, which proves it was added; only then is it kept, in the keychain.
  Both authenticator routes take the app's sign-in as well, once a
  passphrase is set (`signin.refused`): an authenticator approves from the
  phone for as long as it stays, past the app's sign-out (step 11's audit).
- `POST /approvals/hello` `{"public_key", "passphrase"}`: the Windows Hello
  key the app made, kept so its signature can stand in for the passphrase
  (core/hello.py); and `POST /approvals/hello/forget` `{"passphrase"}`.
  Both take the passphrase: a key registered without it would approve
  spending for whoever registered it.
- `POST /approvals/passphrase/reset` `{"new", "nonce", "signature"}`: a
  forgotten passphrase replaced, on that key's signature over a `reset`
  challenge from `/signin/challenge` (core/hello.reset_passphrase). Before
  it, a forgotten passphrase was reset only by deleting its file.
- `POST /approvals/passphrase/reset/windows` `{"new"}`: the same, where no
  Hello key can sign, on this Windows account's password typed into
  Windows' own dialog, which the backend shows and Windows checks
  (core/winsignin.py). Until step 11 deleting the file still worked, and
  let anyone at an unlocked desk choose a passphrase of their own.

No tool reaches these (tests/gate/test_who_may_approve.py): what may
approve is never the model's to change.
"""

from __future__ import annotations

import asyncio
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from agent import offloop, runtime
from agent.worker import bodies, signin
from core import hello, monitor, passphrase, secrets, totp, winsignin

# How long a new authenticator secret waits for its first code.
PENDING_S = 600
# What Windows' dialog says it is for.
PROVE_WHY = ("Steve asks for your Windows password before it takes a new passphrase "
             "in place of a forgotten one.")

# The secret shown and not yet confirmed, and until when (monotonic).
_pending: dict = {"secret": None, "until": 0.0}


def _has_authenticator() -> bool:
    return bool(secrets.get(totp.SECRET_KEY))


async def setup(_: Request) -> JSONResponse:
    # `codes_paused_until`: after five wrong codes, when a code is taken
    # again (core/totp.py), so the app can say why a yes by message fails.
    # Each off the loop: the passphrase and the count are files, the
    # authenticator the keychain.
    return JSONResponse({"passphrase": await asyncio.to_thread(passphrase.is_set),
                         "authenticator": await asyncio.to_thread(_has_authenticator),
                         "hello": await asyncio.to_thread(hello.is_set),
                         "codes_paused_until": await asyncio.to_thread(totp.paused_until)})


async def choose_passphrase(request: Request) -> JSONResponse:
    body = await bodies.body(request)
    old = body.get("old")
    why = await asyncio.to_thread(passphrase.choose, str(body.get("new") or ""),
                                  old=None if old is None else str(old))
    return bodies.bad(why) if why else JSONResponse({"ok": True})


async def reset_passphrase(request: Request) -> JSONResponse:
    body = await bodies.body(request)
    why = await asyncio.to_thread(hello.reset_passphrase, str(body.get("new") or ""),
                                  str(body.get("nonce") or ""), str(body.get("signature") or ""))
    return bodies.bad(why) if why else JSONResponse({"ok": True})


async def reset_passphrase_by_windows(request: Request) -> JSONResponse:
    new = str((await bodies.body(request)).get("new") or "")
    # A passphrase that won't do is refused before Windows' dialog is shown.
    if why := passphrase.unfit(new):
        return bodies.bad(why)
    if why := await asyncio.to_thread(winsignin.prove, PROVE_WHY):
        return bodies.bad(why)
    why = await asyncio.to_thread(passphrase.reset, new)
    return bodies.bad(why) if why else JSONResponse({"ok": True})


async def start_authenticator(request: Request) -> JSONResponse:
    from channels.signal import qr_svg

    body = await bodies.body(request)
    if (refusal := await signin.refused(body)) is not None:
        return refusal
    given = str(body.get("passphrase") or "")
    if why := await asyncio.to_thread(passphrase.check, given):
        return bodies.bad(why)
    secret = totp.new_secret()
    _pending.update(secret=secret, until=time.monotonic() + PENDING_S)
    return JSONResponse({"ok": True, "qr": qr_svg(totp.uri(secret)), "key": secret})


async def confirm_authenticator(request: Request) -> JSONResponse:
    body = await bodies.body(request)
    if (refusal := await signin.refused(body)) is not None:
        return refusal
    code = monitor.code_in(str(body.get("code") or ""))
    secret = _pending["secret"]
    if not secret or time.monotonic() > _pending["until"]:
        return bodies.bad("that setup ran out of time: start it again")
    if code is None or totp.matching_step(secret, code) is None:
        return bodies.bad("that code does not match: type the one the app shows for Steve now")
    await asyncio.to_thread(secrets.set_secret, totp.SECRET_KEY, secret)
    _pending.update(secret=None, until=0.0)
    return JSONResponse({"ok": True})


async def register_hello(request: Request) -> JSONResponse:
    body = await bodies.body(request)
    why = await asyncio.to_thread(hello.register, str(body.get("public_key") or ""),
                                  str(body.get("passphrase") or ""))
    return bodies.bad(why) if why else JSONResponse({"ok": True})


async def forget_hello(request: Request) -> JSONResponse:
    given = str((await bodies.body(request)).get("passphrase") or "")
    why = await asyncio.to_thread(hello.forget, given)
    return bodies.bad(why) if why else JSONResponse({"ok": True})


async def accept_passphrase(action_id: int, tool: str, given: str) -> str | None:
    """`monitor.accept_passphrase`, off the loop: scrypt takes a tenth of a
    second by design."""
    return await offloop.with_own_conn(runtime.conn, monitor.accept_passphrase, action_id,
                                       tool, given)


async def accept_hello(action_id: int, tool: str, nonce: str, signature: str) -> str | None:
    """`monitor.accept_hello`, on its own connection, as the passphrase's."""
    return await offloop.with_own_conn(runtime.conn, monitor.accept_hello, action_id, tool,
                                       nonce, signature)
