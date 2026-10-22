"""Their live view of a sandboxed browser, at the laptop: where the user logs in to a
site themselves the first time, and saves that login.

The maintainer, 1 October 2026: the first login to any site is theirs, at the laptop;
after that the agent uses what was saved. So these routes, behind
`core/localonly` like every route here, give the desktop app a sandbox
browser's screen a few times a second and send their clicks and keys to it
(ui/src/LiveView.tsx draws it). Nothing of it leaves the laptop, and no model
is in the loop: these routes send the driver's `human_*` commands, which no
tool a model calls can send (tests/gate/test_browser_logins.py reads the
toolset for them).

Until 13 October 2026 the worker served the view as a page of its own, at
`/browser/live`, which the app showed in a frame. A frame cannot carry the
token every request now needs (core/localapi.py), so the app draws it.

Saving a login writes, through `core/browser/vault.py`, the site's name,
its domains and its tier (`ask`: the agent asks them before each use;
`free`: it may just use it), the session the page now holds, and -- if the user
types them into the save form, not the page -- their username and password,
straight to the Windows Credential Manager. The vault's writes come only
from here; that is pinned too.

Since 4 October 2026 the user usually signs in somewhere better: the desktop
app's own private sign-in window, at full speed, with passkeys and Windows
Hello (ui/src-tauri/src/lib.rs). Its cookies come here, to `import_login`,
which keeps only the login's domains' and only from a page on them; the
vault is still written from this module and nowhere else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent.browser import session as sessions
from agent.browser.session import cookie_belongs
from agent.worker import bodies
from core import egress
from core.browser import vault as vaults

log = logging.getLogger("browser.live")

# The live session's key in the registry's terms: no conversation has it.
LIVE = -1
# Ten minutes without a click, a key, an address or a save -- not without a
# frame: the app polls for a picture every 350 ms for as long as it is open,
# and counting those kept the sandbox of 8 October 2026 alive for 28 minutes
# after the user had saved the login and gone (until the container's own timeout).
IDLE_S = 600
# After a save the sandbox is no longer needed: it is closed this long after, so
# the user can still see the page they saved from, and a click or key after it keeps it.
SAVE_GRACE_S = 60
_live: list[sessions.BrowserSession] = []
_used: list[float] = []
_close_at: list[float] = []
_opening: list[asyncio.Future] = []


async def _start() -> sessions.BrowserSession:
    session = await sessions.open_sandbox(LIVE)
    _live[:] = [session]
    _used[:] = [time.monotonic()]
    return session


async def _session(*, create: bool, touch: bool = True) -> sessions.BrowserSession | None:
    """The live browser, started if `create` and there is none. `touch` says
    this is them doing something (it resets the idle clock and cancels a close
    after a save); a frame poll is not."""
    if _live and _live[0].alive():
        if touch:
            _used[:] = [time.monotonic()]
            _close_at.clear()
        return _live[0]
    if not create:
        return None
    # One start at a time, and one that cannot be cancelled half way: a second
    # open while the first was starting made a second sandbox, and a page that
    # went away mid-start left one with no owner (the orphan of 8 October
    # 2026, which ran 28 minutes). The started one is registered whoever is
    # still waiting for it.
    here = asyncio.get_running_loop()
    if not _opening or _opening[0].done() or _opening[0].get_loop() is not here:
        _opening[:] = [asyncio.ensure_future(_start())]
    return await asyncio.shield(_opening[0])


async def close() -> None:
    _close_at.clear()
    if _live:
        session = _live.pop()
        with contextlib.suppress(Exception):
            await session.close()


async def sweep(now: float | None = None) -> bool:
    """Close the live view's browser when the user has left it, or a minute after
    they saved a login from it."""
    now = time.monotonic() if now is None else now
    if _live and (not _live[0].alive(now) or (_used and now - _used[0] > IDLE_S)
                  or (_close_at and now >= _close_at[0])):
        await close()
        return True
    return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def open_url(request: Request) -> JSONResponse:
    url = str((await bodies.body(request)).get("url") or "").strip()
    if not url:
        return bodies.bad("an address to open")
    if "://" not in url:
        url = "https://" + url
    if why := await asyncio.to_thread(egress.refusal, url):
        return bodies.bad(f"not opened: {why}")
    try:
        session = await _session(create=True)
    except sessions.BrowserUnavailable as exc:
        return bodies.bad(str(exc), 503)
    return JSONResponse(await session.ask("open", url=url))


async def frame(_: Request) -> JSONResponse:
    # Not touched: a picture polled for is not them using the browser.
    session = await _session(create=False, touch=False)
    if session is None:
        return JSONResponse({"ok": False, "idle": True})
    return JSONResponse(await session.ask("frame", quality=55))


HUMAN = {"click": ("human_click", ("x", "y")), "type": ("human_type", ("text",)),
         "key": ("human_key", ("key",)), "scroll": ("human_scroll", ("dy",))}


async def act(request: Request) -> JSONResponse:
    """Their click, keys or scroll, to the page."""
    body = await bodies.body(request)
    found = HUMAN.get(str(body.get("kind") or ""))
    if found is None:
        return bodies.bad("kind is click, type, key or scroll")
    session = await _session(create=False)
    if session is None:
        return bodies.bad("open a page first")
    cmd, keys = found
    return JSONResponse(await session.ask(cmd, **{k: body.get(k) for k in keys}))


def _form(body: dict) -> tuple[str, str, list[str], str | None]:
    """The save form's name, tier and domains, and why they will not do."""
    name = str(body.get("name") or "").strip().lower()
    tier = str(body.get("tier") or "ask")
    domains = [d.strip().lower() for d in str(body.get("domains") or "").replace(",", " ").split()]
    why = (None if vaults.NAME.match(name) else
           "Name: give the login a name in lower-case letters, digits and dashes, like "
           "northwind-portal" if not name else
           f"Name: {name!r} will not do -- lower-case letters, digits and dashes only") or (
        None if tier in vaults.TIERS else "tier is ask or free") or next(
        (r for r in map(vaults.domain_refusal, domains) if r), None) or (
        None if domains else "the domains the login belongs to")
    return name, tier, domains, why


def _keep(store: vaults.Vault, site: vaults.Site, body: dict) -> JSONResponse | None:
    """Their username and password, if the user typed them into the form: straight
    to the Credential Manager. A refusal, or None."""
    username, password = str(body.get("username") or ""), str(body.get("password") or "")
    if username or password:
        try:
            store.set_credentials(site.name, username, password)
        except ValueError as exc:
            return bodies.bad(f"the session was saved, the password was not: {exc}")
    return None


async def save(request: Request) -> JSONResponse:
    """Save the login the page now holds: its name, domains and tier; the
    session; and, if the user gave them here, their username and password."""
    body = await bodies.body(request)
    session = await _session(create=False)
    if session is None:
        return bodies.bad("open the site and log in first")
    store = vaults.vault()
    # Everything checked before anything is written: a login saved without
    # its session would be one the user thinks they have.
    name, tier, domains, why = _form(body)
    if why:
        return bodies.bad(why)
    exported = await session.ask("export_state", domains=domains)
    if not exported.get("ok"):
        return bodies.bad(f"nothing was saved: {exported.get('error')}")
    # Only the login's own cookies and storage, as an import from the sign-in
    # window and a save-back are held to: the sandbox's jar holds every site
    # its pages touched, and all of it would go into each session started from
    # this login (8 October 2026 audit: 11 other sites' cookies after theirs
    # 16:07 saves).
    state = sessions.own_state(exported.get("state") or {}, domains)
    if not state["cookies"]:
        return bodies.bad("nothing was saved: the browser holds no cookies for these domains "
                          "-- log in to the site first, and add every domain its sign-in "
                          "passes through")
    try:
        site = store.save_site(name, domains, tier)
    except ValueError as exc:
        return bodies.bad(str(exc))
    store.save_state(site.name, state)
    if refused := _keep(store, site, body):
        return refused
    # Nothing more is wanted of this sandbox: it closes in a minute, not when
    # its idle time is up -- or the container's own half hour.
    _close_at[:] = [time.monotonic() + SAVE_GRACE_S]
    log.info("saved the login %s (%s, %s; %d cookies of %d)", site.name,
             ", ".join(site.domains), site.tier, len(state["cookies"]),
             len((exported.get("state") or {}).get("cookies") or []))
    return JSONResponse({"ok": True, "site": store.site(site.name).as_dict(),
                         "closing_in_s": SAVE_GRACE_S})


# A sign-in window holds a few dozen cookies; a list of thousands is not one.
MAX_COOKIES = 400
MAX_COOKIE_CHARS = 8192


def _cookie(raw) -> dict | None:
    """One cookie from the sign-in window, as the sandbox browser loads it
    (Playwright's storage state), or None if it is not one.

    Its domain comes without its leading dot, always: the window reads it
    through the Rust `cookie` crate, whose `domain()` strips the dot
    (ui/src-tauri/src/lib.rs, `collect`), and Playwright takes a dotless
    domain for a host-only cookie. On 7 October 2026 that made LinkedIn's
    `.linkedin.com` cookies host-only for linkedin.com, never sent to
    www.linkedin.com, and its `.www.linkedin.com` ones copies the site could
    neither replace nor delete -- the first logged-in open worked, and the
    ones after it looped. Which cookies were host-only cannot be told any
    more, so every one is put back on its domain: over-scoped inside the
    login's own domains a cookie is merely sent to a sibling host, where
    under-scoped the session breaks. A `__Host-` cookie is host-only by its
    name's rule, and Chromium refuses one with a domain, so it stays as it
    is; a twin the site later sets host-only is kept once at the next
    save-back (`session.kept_state`)."""
    if not isinstance(raw, dict):
        return None
    name, value = str(raw.get("name") or ""), str(raw.get("value") or "")
    domain = str(raw.get("domain") or "").strip().lower()
    if not name or "." not in domain.lstrip(".") or len(name) + len(value) > MAX_COOKIE_CHARS:
        return None
    if not domain.startswith(".") and not name.startswith("__Host-"):
        domain = "." + domain
    try:
        expires = float(raw.get("expires", -1))
    except (TypeError, ValueError):
        expires = -1.0
    secure = bool(raw.get("secure"))
    same = str(raw.get("sameSite") or "Lax").capitalize()
    # Chromium drops a SameSite=None cookie that is not Secure; Lax keeps it.
    if same not in ("Strict", "Lax", "None") or (same == "None" and not secure):
        same = "Lax"
    return {"name": name, "value": value, "domain": domain,
            "path": str(raw.get("path") or "/"), "expires": expires if expires > 0 else -1,
            "httpOnly": bool(raw.get("httpOnly")), "secure": secure, "sameSite": same}


async def import_login(request: Request) -> JSONResponse:
    """Save a login the user signed into in the desktop app's own sign-in window
    (ui/src-tauri, `open_signin` and `signin_cookies`) rather than in the
    sandbox: its name, domains and tier as from the live view's form; of the
    window's cookies, only those for the login's domains; and only if the
    window's page is on them -- the rule `export_state` holds the sandbox to.
    Behind `core/localonly` like every route here: the app, at this laptop."""
    from core.browser.driver import on_site

    body = await bodies.body(request)
    name, tier, domains, why = _form(body)
    if why:
        return bodies.bad(why)
    url = str(body.get("url") or "")
    if not on_site(url, domains):
        here = urlsplit(url).hostname or "no page"
        return bodies.bad(f"the sign-in window is on {here}, which is not in these domains: "
                          "add it, or finish signing in first")
    raw = body.get("cookies")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_COOKIES:
        return bodies.bad("no cookies came from the sign-in window: sign in first")
    kept = [c for c in map(_cookie, raw) if c and cookie_belongs(c["domain"], domains)]
    if not kept:
        return bodies.bad("the sign-in window holds no cookies for these domains: sign in first")
    # A username and password the window saw them type are kept only if the user
    # typed them on one of this login's own pages: the window reports the
    # host each came from, read from the page itself, not from what it says.
    typed_on = str(body.get("credentials_from") or "").strip().lower()
    if (typed_on and (body.get("username") or body.get("password"))
            and not on_site(f"https://{typed_on}/", domains)):
        return bodies.bad(f"the username and password were typed on {typed_on}, which is not "
                          "one of this login's domains: nothing was saved")
    store = vaults.vault()
    try:
        site = store.save_site(name, domains, tier)
    except ValueError as exc:
        return bodies.bad(str(exc))
    store.save_state(site.name, {"cookies": kept, "origins": []})
    if refused := _keep(store, site, body):
        return refused
    log.info("saved the login %s from the sign-in window (%s, %s; %d cookies of %d)",
             site.name, ", ".join(site.domains), site.tier, len(kept), len(raw))
    return JSONResponse({"ok": True, "site": store.site(site.name).as_dict(),
                         "cookies": len(kept), "left_behind": len(raw) - len(kept)})


async def logins(_: Request) -> JSONResponse:
    """The saved logins: names, domains, tiers, what is kept -- never a secret."""
    return JSONResponse({"ok": True, "logins": [s.as_dict() for s in vaults.vault().sites()]})


async def forget(request: Request) -> JSONResponse:
    name = str((await bodies.body(request)).get("name") or "")
    return JSONResponse({"ok": vaults.vault().forget(name)})


async def set_tier(request: Request) -> JSONResponse:
    """Whether the agent asks them before using a login, changed on its own:
    its domains, session and password stay as they are. The maintainer, 4 October
    2026: changing it should not mean signing in all over again."""
    body = await bodies.body(request)
    name, tier = str(body.get("name") or ""), str(body.get("tier") or "")
    store = vaults.vault()
    site = store.site(name)
    if site is None:
        return bodies.bad(f"no saved login called {name!r}")
    if tier not in vaults.TIERS:
        return bodies.bad("tier is ask or free")
    try:
        changed = store.save_site(site.name, list(site.domains), tier)
    except ValueError as exc:
        return bodies.bad(str(exc))
    log.info("the login %s is now %s", changed.name, changed.tier)
    return JSONResponse({"ok": True, "site": changed.as_dict()})


async def stop(_: Request) -> JSONResponse:
    await close()
    return JSONResponse({"ok": True})


async def outbox(request: Request) -> JSONResponse:
    """A file the user puts in the outbox: what the agent may upload, with their
    card each time (core/browser/files.py)."""
    import base64
    import binascii

    from core.browser import files

    body = await bodies.body(request)
    try:
        data = base64.b64decode(str(body.get("b64") or ""), validate=True)
    except (binascii.Error, ValueError):
        return bodies.bad("the file did not arrive")
    if not data or len(data) > files.MAX_FILE_BYTES:
        return bodies.bad(f"a file of up to {files.MAX_FILE_BYTES // (1024 * 1024)} MB")
    name = files.quarantine_name(str(body.get("name") or "file"))
    files.outbox_dir().mkdir(parents=True, exist_ok=True)
    (files.outbox_dir() / name).write_bytes(data)
    return JSONResponse({"ok": True, "file": f"outbox/{name}", "size": len(data)})


ROUTES = [
    Route("/browser/live/open", open_url, methods=["POST"]),
    Route("/browser/live/frame", frame),
    Route("/browser/live/act", act, methods=["POST"]),
    Route("/browser/live/save", save, methods=["POST"]),
    Route("/browser/live/stop", stop, methods=["POST"]),
    Route("/browser/live/outbox", outbox, methods=["POST"]),
    Route("/browser/logins", logins),
    Route("/browser/logins/forget", forget, methods=["POST"]),
    Route("/browser/logins/import", import_login, methods=["POST"]),
    Route("/browser/logins/tier", set_tier, methods=["POST"]),
]
