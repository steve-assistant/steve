"""The app's routes for modules and accounts: the only way one is added,
changed or removed (05-connectors.md, sections 3 and 10).

- `GET /control/modules`: each module, its setup as the app draws it (no
  value of any field), each account with its health, and the modules Steve
  is building or built lately (`builds`) with what they found they could
  not be (`reports`).
- `POST /control/modules`: switch a module on or off (`id`, `on`); or by
  `op`, `approve` a module waiting for the user's card (`admission`, and
  `passphrase`, or `hello` `{nonce, signature}`, where the card needs it)
  or `decline` it, `rollback` a module from outside to a kept `version`,
  or `remove` one (`forget`);
  `export` one from outside as a zip (answered as base64 `zip`, with the
  `name` to save it by), or `import` a `zip` the user chose, up to its card
  (`replace` for one of the same id);
  `allow` a `build` waiting past the day's spending cap, or `dismiss` a
  waiting `build` or a `report`. Approval and allowance are here alone: no
  remote channel and no tool reaches them. Each of these four answers a
  card, so each needs the app's live sign-in (`session`), as an action's
  card does (agent/worker/signin.py `refused`).
- `POST /control/accounts`: `op` is one of `pick` (the address first),
  `add` (tested at once, unless its setup signs in first: then it answers
  `next: browser_sign_in`), `sign_in` (in the browser, then the test),
  `test`, `edit` (name, settings, on or off), `move` (a class, from the
  account that holds the address), and `remove` (with `forget`, what Steve
  learned from it goes too). A test or a sign-in that fails says why, and
  the page that fixes it where there is one (`fix`).
- `GET /control/mailbox`: the agent's own mailbox: its address, the mail
  accounts that could hold it, the addresses that count as the user's, and
  what arrived lately with why it counted or not (setup's test: "send any
  mail to you+agent@... from your phone").
- `POST /control/mailbox`: `op` is `choose` (an account's `key`, and a
  `tag`), `stop`, `address` (an `address` opted in or out with `on`, and
  `own_domain` for "only I send as this domain"), or `look` (read it now).

They are app-only, as every `/control/` route is: the worker answers only
on this computer, with this start's token (`core/localonly.py`), and no
remote channel reaches them. No model tool calls `agent/accounts.py`
(tests/gate/test_only_the_app.py), so Steve can only hand the user a link.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import accounts, mcptools, modbuild, modshare, offloop, runtime
from agent.worker import bodies, signin
from core import lifecycle, modules
from core import mailbox as rules
from steve_sdk.errors import ConnectorError

log = logging.getLogger("control.accounts")


def _listing(conn) -> dict[str, Any]:
    return {**accounts.listing(conn), "builds": modbuild.builds(conn),
            "reports": modbuild.reports(conn)}


async def module_list(_: Request) -> JSONResponse:
    listing = await offloop.with_own_conn(runtime.conn, _listing)
    return JSONResponse({"ok": True, **listing})


def _an_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def _build_op(op: str, body: dict[str, Any]) -> JSONResponse:
    """"Allow this build", which starts it, and putting a build or a report
    away. Allowing is the app's alone: a build past the day's cap spends
    money the user did not set aside, and a remote channel could be
    someone holding their phone."""
    build, report = body.get("build"), body.get("report")
    try:
        if op == "allow":
            if not _an_id(build):
                return bodies.bad("say which build")
            row = await offloop.with_own_conn(runtime.conn, modbuild.allow, build)
            modbuild.start(runtime.conn, row["id"],
                           notify=modbuild.notifier(asyncio.get_running_loop()))
            return JSONResponse({"ok": True})
        if _an_id(report):
            await offloop.with_own_conn(runtime.conn, modbuild.dismiss, report_id=report)
        elif _an_id(build):
            await offloop.with_own_conn(runtime.conn, modbuild.dismiss, build_id=build)
        else:
            return bodies.bad("say which build or report")
        return JSONResponse({"ok": True})
    except modbuild.Refused as exc:
        return bodies.bad(str(exc))


# The ops that answer a card: a module's or a server's, and a build's.
ANSWERS = ("approve", "decline", "allow", "dismiss")


async def module_op(request: Request) -> JSONResponse:
    """Switch a module on or off, off withdrawing its accounts' pending
    cards; or the module's card, its versions and its removal (`op`)."""
    body = await bodies.body(request)
    op = body.get("op", "switch")
    module_id = body.get("id")
    if op in ANSWERS and (refusal := await signin.refused(body)) is not None:
        return refusal
    if op in ("allow", "dismiss"):
        return await _build_op(op, body)
    try:
        if op == "switch":
            on = body.get("on")
            if not isinstance(module_id, str) or not isinstance(on, bool):
                return bodies.bad("say which module, and on or off")
            await offloop.with_own_conn(runtime.conn, modules.set_enabled, module_id, on)
            return JSONResponse({"ok": True})
        if op in ("approve", "decline"):
            admission = body.get("admission")
            if not isinstance(admission, int) or isinstance(admission, bool):
                return bodies.bad("say which card")
            if op == "decline":
                await offloop.with_own_conn(runtime.conn, lifecycle.decline, admission)
                return JSONResponse({"ok": True})
            # The passphrase, or Windows Hello's signature over this card's
            # challenge (`/signin/challenge`, for a yes with the admission).
            signed = body.get("hello")
            if signed is not None and not (isinstance(signed, dict)
                                           and isinstance(signed.get("nonce"), str)
                                           and isinstance(signed.get("signature"), str)):
                return bodies.bad("hello is a nonce and a signature")
            proof = {"given": str(body.get("passphrase") or ""),
                     "signed": signed and (signed["nonce"], signed["signature"])}
            if "secrets" in body or "free" in body:
                # A server module's card: the secret the user typed, and
                # the tools they freed, checked against the card.
                secrets, free = body.get("secrets") or {}, body.get("free")
                if not (isinstance(secrets, dict)
                        and all(isinstance(v, str) for v in secrets.values())):
                    return bodies.bad("secrets is a set of named texts")
                if free is not None and not (isinstance(free, list)
                                             and all(isinstance(t, str) for t in free)):
                    return bodies.bad("free is a list of the server's tools")
                approved = await offloop.with_own_conn(
                    runtime.conn, accounts.approve_server, admission, secrets=secrets,
                    free=free, **proof)
            else:
                approved = await offloop.with_own_conn(runtime.conn, lifecycle.approve,
                                                       admission, **proof)
            if body.get("on") is not True:
                return JSONResponse({"ok": True, "id": approved})
            # The card's main choice: approved and on. Approved alone, a
            # new extension dropped into the list switched off, where it
            # was easy to miss. On is the switch's own step, with its check
            # that these are the files approved; refused, the yes stands,
            # the module stays off, and the switch tries again.
            try:
                await offloop.with_own_conn(runtime.conn, modules.set_enabled, approved, True)
            except modules.AccountError as exc:
                return JSONResponse({"ok": True, "id": approved, "on": False, "why": str(exc)})
            return JSONResponse({"ok": True, "id": approved, "on": True})
        if op == "add_server" and body.get("package"):
            hosts = body.get("hosts") or []
            if not (isinstance(hosts, list) and all(isinstance(h, str) for h in hosts)):
                return bodies.bad("hosts is a list of names")
            admission = await mcptools.add_package(
                runtime.conn, name=_text(body, "name"), package=_text(body, "package"),
                entry=_text(body, "entry"), hosts=hosts, label=_text(body, "label"),
                anywhere=body.get("anywhere") is True)
            return JSONResponse({"ok": True, "admission": admission})
        if op == "add_server":
            admission = await mcptools.add(
                runtime.conn, name=_text(body, "name"), url=_text(body, "url"),
                secret=_text(body, "secret"), label=_text(body, "label"))
            return JSONResponse({"ok": True, "admission": admission})
        if op == "import":
            admission = await modshare.import_zip(runtime.conn, _zip(body.get("zip")),
                                                  replace=body.get("replace") is True)
            return JSONResponse({"ok": True, "admission": admission})
        if not isinstance(module_id, str):
            return bodies.bad("say which module")
        if op == "export":
            name, data = await offloop.with_own_conn(runtime.conn, modshare.export_zip,
                                                     module_id)
            return JSONResponse({"ok": True, "name": name,
                                 "zip": base64.b64encode(data).decode("ascii")})
        if op == "rollback":
            version = body.get("version")
            if not isinstance(version, str):
                return bodies.bad("say which version")
            data_from = await offloop.with_own_conn(runtime.conn, lifecycle.rollback,
                                                    module_id, version)
            return JSONResponse({"ok": True, "data_from": data_from})
        if op == "remove":
            await offloop.with_own_conn(runtime.conn, lifecycle.remove, module_id,
                                        forget=bool(body.get("forget")))
            return JSONResponse({"ok": True})
    except modules.AccountError as exc:
        return bodies.bad(str(exc))
    return bodies.bad(f"not something done to a module: {op!r}")


def _zip(text: object) -> bytes:
    if not isinstance(text, str) or not text:
        raise modules.AccountError("choose a zip to import")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise modules.AccountError("the zip did not arrive whole") from None


def _text(body: dict, name: str) -> str:
    value = body.get(name, "")
    if not isinstance(value, str):
        raise modules.AccountError(f"{name} is text")
    return value


def _maybe(body: dict, name: str) -> str | None:
    """A text the request may leave out: None then."""
    return _text(body, name) or None if body.get(name) is not None else None


async def account_op(request: Request) -> JSONResponse:
    body = await bodies.body(request)
    op = body.get("op")
    try:
        if op == "pick":
            address = _text(body, "address")
            installed = await offloop.with_own_conn(runtime.conn, modules.known)
            return JSONResponse({"ok": True, **accounts.pick(address, installed=installed)})
        if op == "add":
            wanted = body.get("classes")
            if wanted is not None and not (isinstance(wanted, list)
                                           and all(isinstance(c, str) for c in wanted)):
                return bodies.bad("classes is a list of classes")
            values = body.get("fields") or {}
            if not isinstance(values, dict):
                return bodies.bad("fields is a set of named values")
            key = await offloop.with_own_conn(
                runtime.conn, lambda conn: accounts.add(
                    conn, module_id=_text(body, "module"), address=_text(body, "address"),
                    wanted=wanted, preset=_maybe(body, "preset"), values=values,
                    name=_maybe(body, "name"), take=body.get("take") is True))
            if step := await offloop.with_own_conn(runtime.conn, _next_step, key):
                # Tested before its sign-in, it could only fail.
                return JSONResponse({"ok": True, "key": key, "next": step})
            tested = await accounts.test(runtime.conn, key)
            return JSONResponse({"ok": True, "key": key, "test": tested})
        if op == "sign_in":
            return JSONResponse({"ok": True, "test": await accounts.sign_in(
                runtime.conn, _text(body, "key"))})
        if op == "server_sign_in":
            return JSONResponse({"ok": True, **await accounts.server_sign_in(
                runtime.conn, _text(body, "key"))})
        if op == "test":
            return JSONResponse({"ok": True, "test": await accounts.test(
                runtime.conn, _text(body, "key"))})
        if op == "edit":
            settings = body.get("settings")
            if settings is not None and not isinstance(settings, dict):
                return bodies.bad("settings is a set of named values")
            enabled = body.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                return bodies.bad("enabled is on or off")
            values = body.get("fields")
            if values is not None and not isinstance(values, dict):
                return bodies.bad("fields is a set of named values")
            await offloop.with_own_conn(
                runtime.conn, lambda conn: accounts.edit(
                    conn, _text(body, "key"), name=_maybe(body, "name"), settings=settings,
                    enabled=enabled, values=values))
            return JSONResponse({"ok": True})
        if op == "move":
            await offloop.with_own_conn(
                runtime.conn, lambda conn: accounts.move(
                    conn, klass=_text(body, "class"), address=_text(body, "address"),
                    to_key=_text(body, "key")))
            return JSONResponse({"ok": True})
        if op == "remove":
            await offloop.with_own_conn(
                runtime.conn, lambda conn: accounts.remove(
                    conn, _text(body, "key"), forget=body.get("forget") is True))
            return JSONResponse({"ok": True})
    except modules.Held as exc:
        # The app offers "Move the calendar for this address here", which
        # adds again with `take`.
        return JSONResponse({"ok": False, "error": str(exc), "held": {
            "class": exc.klass, "address": exc.address, "name": exc.name}}, status_code=400)
    except modules.AccountError as exc:
        return bodies.bad(str(exc))
    except ConnectorError as exc:
        return JSONResponse({"ok": False, "error": exc.detail or exc.kind, "fix": exc.fix},
                            status_code=400)
    return bodies.bad(f"no such thing to do to an account: {op!r}")


async def mailbox_view(_: Request) -> JSONResponse:
    from agent import mailbox

    return JSONResponse({"ok": True, **await offloop.with_own_conn(runtime.conn,
                                                                    mailbox.settings)})


async def mailbox_op(request: Request) -> JSONResponse:
    """The own mailbox's settings, which only the app writes (core/mailbox.py)."""
    body = await bodies.body(request)
    op = body.get("op")
    try:
        if op == "choose":
            found = await offloop.with_own_conn(
                runtime.conn, lambda conn: rules.choose(conn, _text(body, "key"),
                                                        _text(body, "tag") or "agent"))
            return JSONResponse({"ok": True, "address": found.alias})
        if op == "stop":
            await offloop.with_own_conn(runtime.conn, rules.stop)
            return JSONResponse({"ok": True})
        if op == "address":
            on, own_domain = body.get("on"), body.get("own_domain", False)
            if not isinstance(on, bool) or not isinstance(own_domain, bool):
                return bodies.bad("on, and own_domain, are on or off")
            address = _text(body, "address")
            await offloop.with_own_conn(
                runtime.conn, lambda conn: rules.opt_in(conn, address, own_domain=own_domain)
                if on else rules.opt_out(conn, address))
            return JSONResponse({"ok": True})
        if op == "look":
            from agent.worker import background

            return JSONResponse({"ok": True, "done": await background.mailbox_tick()})
    except modules.AccountError as exc:
        return bodies.bad(str(exc))
    return bodies.bad(f"no such thing to do to the mailbox: {op!r}")


def _next_step(conn, key: str) -> str | None:
    row = modules.account(conn, key)
    held = modules.module(conn, row.module) if row else None
    if row is None or held is None or held.manifest is None:
        return None
    return accounts.next_step(held.manifest, row.preset)


ROUTES = [
    Route("/control/modules", module_list),
    Route("/control/modules", module_op, methods=["POST"]),
    Route("/control/accounts", account_op, methods=["POST"]),
    Route("/control/mailbox", mailbox_view),
    Route("/control/mailbox", mailbox_op, methods=["POST"]),
]
