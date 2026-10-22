"""A conversation's browser, from the worker's side.

One sandbox per conversation (`core/browser/sandbox.py`) and one driver
process talking to it over `docker exec` (`core/browser/driver.py`), kept
in a registry so a conversation's second page is opened in the same
browser as its first. Nothing here decides what is safe -- that is the
frozen half's -- but three things are owed to the rest of the worker:

- **It never holds the event loop.** Starting and stopping a sandbox is a
  handful of blocking `docker` calls, run in a thread; the driver is an
  asyncio subprocess, read line by line as it answers, its stderr going to
  a file rather than a pipe nobody empties (protocols section 5).
- **It never outlives its use.** A session idle for `IDLE_S`, or older than
  `MAX_AGE_S` -- just under the containers' own lifetime -- is closed by
  `sweep`, which also reaps what a dead worker left; a session holding a
  submit card waiting for their yes is kept (`hold`) until the card is
  answered or expires. `close_all` runs as the worker stops.
- **It keeps the record.** At close, the gate's log -- every host the
  session's pages contacted, and what was refused -- is saved beside the
  driver's (`data/logs/browser/`).
- **It saves a login back only when it is no worse** (`kept_state`). On 7
  October 2026 their LinkedIn login, saved at 13:07:51, worked once; that
  browser was closed by a worker swap and its cookies written over the
  saved login -- every domain's, Google's and PerimeterX's included -- and
  every logged-in open after it failed: two redirect loops and a blank
  /feed/, which was itself saved back at 13:11. Closing wrote whatever the
  sandbox held, broken or signed out. Now only the login's own cookies are
  kept, one of each, and nothing is written from a browser that hit a
  redirect loop, a sign-in wall or an empty page, or that lost a cookie the
  saved login had.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from core import paths
from core.browser import sandbox
from core.browser.driver import on_site

log = logging.getLogger("browser")


def log_dir() -> Path:
    return paths.logs_dir() / "browser"


# Three minutes, not ten (8 October 2026 audit): an idle logged-in LinkedIn
# browser held 1.1 GiB of the laptop for eleven minutes after its last page,
# and a cold start is only 2.5-4.5 s. Waiting for their yes to a card is not
# idle (`hold`).
IDLE_S = 180
MAX_AGE_S = sandbox.LIFETIME_S - 120

# What Playwright says when the browser, or the page in it, is gone: the
# container killed Chromium for memory ("Target page, context or browser has
# been closed"), the page crashed, or the connection to it dropped. On 8
# October 2026 Chromium was OOM-killed at the 8th LinkedIn load; the driver
# process lived on, so the session still looked alive, stayed registered for
# eleven minutes, and the model was handed `TargetClosedError: Page.evaluate:
# ...` with half a profile.
GONE = re.compile(
    r"TargetClosedError|target (page, context or browser )?(has been )?closed|target crashed|"
    r"page crashed|browser (has been )?closed|browser has disconnected|"
    r"connection closed while reading|the browser failed|the browser stopped",
    re.IGNORECASE)


def browser_gone(said: dict) -> bool:
    """Whether a driver answer says the browser or its page no longer exists:
    nothing more can be asked of it, and what it said is not a verdict on
    the page."""
    return not said.get("ok") and bool(GONE.search(str(said.get("error") or "")))
# Longer than the driver's own per-command limit, so the driver reports a
# slow page itself rather than this giving up on it.
DRIVER_TIMEOUT_S = 75.0
# A screenshot comes back as one base64 line.
STDOUT_LIMIT = 32 * 1024 * 1024
READY_CACHE_S = 60.0


class BrowserUnavailable(RuntimeError):
    """Why the browser cannot be used right now, in words the model can say."""


class DriverChannel:
    """The driver, over `docker exec` stdio: one JSON line out, one back."""

    def __init__(self, process: asyncio.subprocess.Process, stderr) -> None:
        self.process = process
        self.stderr = stderr

    @classmethod
    async def spawn(cls, box: sandbox.Sandbox) -> DriverChannel:
        log_dir().mkdir(parents=True, exist_ok=True)
        stderr = (log_dir() / f"{box.session}.driver.log").open("ab")
        process = await asyncio.create_subprocess_exec(
            *sandbox.driver_argv(box), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=stderr, limit=STDOUT_LIMIT)
        return cls(process, stderr)

    def alive(self) -> bool:
        return self.process.returncode is None

    async def send(self, message: dict, timeout: float) -> dict:
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self.process.stdin.drain()
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
            if not line:
                raise BrowserUnavailable("the browser stopped")
            said = json.loads(line)
            # An answer to a command that timed out earlier: not this one's.
            if said.get("id") == message.get("id"):
                return said

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.process.stdin:
                self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 10)
        except (TimeoutError, ProcessLookupError):
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
        with contextlib.suppress(Exception):
            self.stderr.close()


@dataclass
class BrowserSession:
    conversation: int
    box: sandbox.Sandbox
    channel: DriverChannel
    started: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    steps: int = 0
    # A logged-in browser's saved login, and the domains it is kept on.
    site: str = ""
    domains: tuple = ()
    # Why this logged-in browser's state is not worth saving back -- a
    # redirect loop, a sign-in wall, an empty page -- or "" (`kept_state`).
    # Set by the tools as they see it (agent/toolset/browser.py).
    stale: str = ""
    # Why this browser can no longer be used -- Chromium was killed or the
    # page crashed -- or "". Set by `ask` from what the driver said, so
    # `alive` knows of a dead page and not only of a dead `docker exec`.
    dead: str = ""
    # Page loads asked of it, for a tool that recycles a browser after a few
    # (LinkedIn's, whose pages weigh 130-200 MB each in Chromium).
    opens: int = 0
    _sent: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def alive(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return not self.dead and self.channel.alive() and now - self.started < MAX_AGE_S

    async def ask(self, cmd: str, **args) -> dict:
        """One driver command. Never raises: a browser that failed is an
        answer the model reads."""
        async with self._lock:
            self._sent += 1
            try:
                said = await self.channel.send({"id": self._sent, "cmd": cmd, **args},
                                               DRIVER_TIMEOUT_S)
            except TimeoutError:
                said = {"ok": False, "error": "the browser did not answer in time"}
            except (BrowserUnavailable, OSError, ValueError) as exc:
                said = {"ok": False, "error": f"the browser failed: {exc}"[:300]}
            self.last_used = time.monotonic()
            self.steps += 1
            if cmd == "open":
                self.opens += 1
            if browser_gone(said) and cmd != "close":
                self.dead = str(said.get("error") or "")[:200]
            return said

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.channel.send({"id": -1, "cmd": "close"}, 15), 20)
        await self.channel.close()
        await asyncio.to_thread(_keep_gate_log, self.box)
        await asyncio.to_thread(STOP, self.box)


def _keep_gate_log(box: sandbox.Sandbox) -> None:
    """The gate's record of where the session went, kept past the session.
    Through `sandbox._docker`, which the unit tests fence."""
    with contextlib.suppress(Exception):
        done = sandbox._docker(["logs", box.gate], 30)
        log_dir().mkdir(parents=True, exist_ok=True)
        (log_dir() / f"{box.session}.gate.jsonl").write_text(done.stdout, encoding="utf-8",
                                                           newline="\n")


# What starts, stops and talks to a sandbox: the real ones, and what the unit
# tests put in their place.
START: Callable[[str], sandbox.Sandbox] = sandbox.start
STOP: Callable[[sandbox.Sandbox], None] = sandbox.stop
SPAWN: Callable[[sandbox.Sandbox], Awaitable[DriverChannel]] = DriverChannel.spawn
READY: Callable[[], bool] = sandbox.ready
AVAILABLE: Callable[[], bool] = sandbox.available

# A conversation's browsers, by (conversation, site): "" is the public one,
# a saved login's name its logged-in one (core/browser/vault.py). At most
# PER_CONVERSATION at once -- each is 1 to 2 GiB once a heavy page has been
# open (1.1 GiB idle after three LinkedIn pages, 8 October 2026) -- and the
# one last used is the conversation's current browser.
Key = tuple[int, str]
PER_CONVERSATION = 2
_sessions: dict[Key, BrowserSession] = {}
_current: dict[int, Key] = {}
_opening: dict[Key, asyncio.Lock] = {}
_held: dict[int, float] = {}  # conversation -> until (monotonic)
_approved: dict[Key, float] = {}  # (conversation, login) -> until (monotonic)
_ready: list[float] = []  # when the images were last seen built

# How often the sweep asks Docker for an earlier worker's leftovers while
# this one has no browser open. It asked every two minutes, and each ask
# woke Docker Desktop: Resource Saver pauses its VM only after five minutes
# with nothing asked, so it never paused, and com.docker.backend used a
# quarter of a core all day with no container running (measured 5 October
# 2026). With a browser open, or one just closed, it is asked every sweep as
# before; the first sweep after a start clears what a dead worker left.
REAP_IDLE_S = 1800
_reap = {"at": float("-inf")}
# How long their yes to using an `ask` login lasts in a conversation.
LOGIN_APPROVAL_S = 1800


async def unavailable() -> str | None:
    """Why no session can be opened, or None. The policy switch is the
    tools' to check (`agent/toolset/browser.py`); this is the machine."""
    now = time.monotonic()
    if _ready and now - _ready[0] < READY_CACHE_S:
        return None
    # Two answers, not one: on 1 October Docker Desktop was still starting,
    # and the user was told the sandbox was "not set up" -- true of neither the
    # images nor the fix, which was to wait a minute.
    if not await asyncio.to_thread(AVAILABLE):
        return ("Docker isn't running on the laptop -- Docker Desktop may still be starting; "
                "try again in a minute")
    if not await asyncio.to_thread(READY):
        # Steve builds it once the switch is on (`background.build_browser`);
        # the command is for a build that failed, and says why.
        return ("the browser sandbox isn't built on this laptop yet: Steve builds it once "
                "the browser is switched on, which takes several minutes the first time; "
                "`python -m core.browser setup` builds it by hand")
    _ready[:] = [now]
    return None


async def session_for(conversation: int, site=None, *, domains: tuple = ()) -> BrowserSession:
    """The conversation's browser -- its public one, or the one logged in to
    `site` (a `vault.Site`) -- opened if it has none or its last one ended,
    and made the conversation's current one. A logged-in one starts from
    the login's saved session and stays on its domains (the driver's).
    Whether this conversation may use the login is the caller's to have
    checked. `domains`, for a public one, keeps it to those sites the same
    way: a browsing worker's (agent/workers.py). Raises BrowserUnavailable."""
    key: Key = (conversation, site.name if site is not None else "")
    existing = _sessions.get(key)
    if existing is not None and existing.alive():
        _current[conversation] = key
        return existing
    lock = _opening.setdefault(key, asyncio.Lock())
    async with lock:
        existing = _sessions.get(key)
        if existing is not None and existing.alive():
            _current[conversation] = key
            return existing
        if existing is not None:
            await _close_key(key)
        await _make_room(conversation, key)
        start: dict = {}
        if site is not None:
            from core.browser import vault

            start = {"site_domains": list(site.domains),
                     "storage_state": vault.vault().load_state(site.name)}
        elif domains:
            start = {"site_domains": list(domains)}
        session = await open_sandbox(conversation, **start)
        session.site = site.name if site is not None else ""
        session.domains = tuple(site.domains) if site is not None else tuple(domains)
        _sessions[key] = session
        _current[conversation] = key
        log.info("browser session %s opened for conversation %s%s", session.box.session,
                 conversation, f" logged in to {site.name}" if site is not None else "")
        return session


async def _make_room(conversation: int, keeping: Key) -> None:
    mine = [k for k in _sessions if k[0] == conversation and k != keeping]
    while len(mine) >= PER_CONVERSATION:
        oldest = min(mine, key=lambda k: _sessions[k].last_used)
        await _close_key(oldest)
        mine.remove(oldest)


async def _close_key(key: Key) -> None:
    """Close one browser. A logged-in one's session is saved back to the
    vault first -- refreshed cookies, so the next one starts logged in --
    only from its own site (the driver's `export_state` checks), and only
    if it is no worse than what is saved (`kept_state`)."""
    session = _sessions.pop(key, None)
    if _current.get(key[0]) == key:
        _current.pop(key[0], None)
    if session is None:
        return
    if session.site and session.dead:
        # Nothing to export from a browser that is gone, and asking a dead
        # driver can take as long as its timeout.
        log.info("kept the saved login for %s as it was: the browser died (%s)", session.site,
                 session.dead[:80])
    elif session.site:
        try:
            exported = await session.ask("export_state", domains=list(session.domains))
            if exported.get("ok") and exported.get("state"):
                why = await asyncio.to_thread(_save_back, session, exported)
            else:
                why = str(exported.get("error") or "nothing came back")
            if why:
                log.info("kept the saved login for %s as it was: %s", session.site, why)
        except Exception:  # noqa: BLE001 - a login not saved back is not a close that failed
            log.warning("could not save the login %s back", session.site, exc_info=True)
    await session.close()
    log.info("browser session %s closed", session.box.session)


def _save_back(session: BrowserSession, exported: dict) -> str:
    """Write a closing browser's state over its saved login if `kept_state`
    allows; why not, or "". Blocking: DPAPI and the disk."""
    from core.browser import vault

    store = vault.vault()
    state, why = kept_state(exported["state"], store.load_state(session.site) or {},
                            session.domains, url=str(exported.get("url") or ""),
                            stale=session.stale)
    if state is not None:
        store.save_state(session.site, state)
    return why


def login_saved_at(site: str) -> int | None:
    """When a saved login's session was last written, as the file system's
    nanosecond mark, or None if it has none. Only a mark to compare with
    another taken earlier -- the vault's session changes when the user saves the
    login again or a good browser saves its refreshed cookies back, never
    while one is turned away (`kept_state`) -- so a tool that has stopped on
    it can tell that they have since signed in again (agent/toolset/linkedin.py
    `Pacer`). Reads the file's own entry, never its contents."""
    from core.browser import vault

    try:
        return vault.vault()._session_file(site).stat().st_mtime_ns
    except OSError:
        return None


def cookie_belongs(domain: str, domains) -> bool:
    """Whether a page of the login would be sent this cookie: one set for a
    login domain or below it, or for a parent of one -- ".contoso.example" reaches
    canvas.contoso.example. Anything else a browser collected stays behind: the
    sign-in window's import (agent/browser/live.py) and a save-back alike."""
    d = domain.strip().lower().lstrip(".")
    return "." in d and any(d == x or d.endswith("." + x) or x.endswith("." + d)
                            for x in domains)


# A page a logged-in browser should never be on when it closes: the site
# asking who the user is. LinkedIn's /authwall and /checkpoint/, /uas/login, CAS's
# /cas/login, and the plain /login and /signin of most sites. Whole path
# segments, so /in/login-expert-jones/ is a profile, not a sign-in page.
SIGN_IN_PATH = re.compile(r"(^|/)(authwall|log-?in|sign-?in|logon|checkpoint|uas)(/|$)",
                          re.IGNORECASE)


def signed_out_page(url: str) -> bool:
    """Whether `url` is a site's sign-in page or wall (`SIGN_IN_PATH`)."""
    return bool(SIGN_IN_PATH.search(urlsplit(url or "").path))


def own_state(state: dict, domains) -> dict:
    """A browser's `state` cut to the login's own: the cookies a page of the
    login would be sent (`cookie_belongs`), one of each (`_one_of_each`), and
    the storage of origins on its domains (`on_site`). What a login saved from
    the live view holds: the sandbox's jar has every site its pages touched --
    eleven other sites' cookies were in their LinkedIn login after the 16:07
    saves on 8 October 2026, and went into every session started from it --
    where a save-back (`kept_state`) and the sign-in window's import
    (agent/browser/live.py) were already held to the login's own."""
    domains = [str(d).strip().lower() for d in domains]
    mine = [c for c in (state or {}).get("cookies") or []
            if isinstance(c, dict) and cookie_belongs(str(c.get("domain") or ""), domains)]
    origins = [o for o in (state or {}).get("origins") or []
               if isinstance(o, dict) and on_site(str(o.get("origin") or ""), domains)]
    return {"cookies": _one_of_each(mine, []), "origins": origins}


def kept_state(state: dict, saved: dict, domains, *, url: str = "", stale: str = "",
               now: float | None = None) -> tuple[dict | None, str]:
    """What of a closing logged-in browser's `state` to save over `saved`,
    or None and why not.

    Only the login's own cookies (`cookie_belongs`) and storage (`on_site`):
    a save-back is held to the rule the sign-in window's import is. None
    when the browser was marked `stale`, ended on a sign-in page, holds
    none of the login's cookies, or has lost a lasting cookie the saved
    login had -- a sign-out, which saving would make their saved login too.
    A cookie held twice, host-only and for its domain, is kept once
    (`_one_of_each`)."""
    now = time.time() if now is None else now
    domains = [str(d).strip().lower() for d in domains]
    if stale:
        return None, stale
    if signed_out_page(url):
        return None, f"it ended on a sign-in page ({urlsplit(url).path[:60]})"
    mine = [c for c in (state or {}).get("cookies") or []
            if isinstance(c, dict) and cookie_belongs(str(c.get("domain") or ""), domains)]
    if not mine:
        return None, "it held none of the login's cookies"
    had = [c for c in (saved or {}).get("cookies") or []
           if isinstance(c, dict) and cookie_belongs(str(c.get("domain") or ""), domains)]
    cookies = _one_of_each(mine, had)
    held = {str(c.get("name")) for c in cookies}
    lost = sorted({str(c.get("name")) for c in had
                   if _expires(c) > now and str(c.get("name")) not in held})
    if lost:
        # Names only, never a value: this goes to the log.
        return None, (f"it no longer holds {', '.join(lost[:3])}"
                      f"{f' and {len(lost) - 3} more' if len(lost) > 3 else ''}, which the "
                      "saved login had -- signed out?")
    origins = [o for o in (state or {}).get("origins") or []
               if isinstance(o, dict) and on_site(str(o.get("origin") or ""), domains)]
    return {"cookies": cookies, "origins": origins}, ""


def _expires(cookie: dict) -> float:
    try:
        return float(cookie.get("expires", -1))
    except (TypeError, ValueError):
        return -1.0


def _one_of_each(cookies: list[dict], had: list[dict]) -> list[dict]:
    """One cookie per name and path on a domain, where the browser held it
    twice: host-only for `x` and for the domain `.x`.

    A server can neither replace nor delete the copy it did not set -- a
    Set-Cookie with Domain=.x leaves a host-only `x` alone, and the other
    way round -- so a stale twin is sent beside the fresh one for good; on
    7 October that is the likeliest cause of LinkedIn's redirect loops. The
    copy kept is the one the site touched this session (new, or a new
    value, against the saved login `had`); failing that the one lasting
    longer; failing that the domain cookie, as sites set them."""
    before = {(str(c.get("name")), str(c.get("domain")), str(c.get("path") or "/")):
              c.get("value") for c in had}
    groups: dict[tuple, list[dict]] = {}
    for c in cookies:
        key = (str(c.get("name")), str(c.get("domain") or "").lower().lstrip("."),
               str(c.get("path") or "/"))
        groups.setdefault(key, []).append(c)
    kept = []
    for twins in groups.values():
        if len(twins) == 1:
            kept.append(twins[0])
            continue
        touched = [c for c in twins if before.get(
            (str(c.get("name")), str(c.get("domain")), str(c.get("path") or "/")),
            object()) != c.get("value")]
        if len(touched) == 1:
            kept.append(touched[0])
            continue
        kept.append(max(twins, key=lambda c: (_expires(c),
                                              str(c.get("domain") or "").startswith("."))))
    return kept


def approve_login(conversation: int, site: str, seconds: float = LOGIN_APPROVAL_S) -> None:
    """Their yes to this conversation using an `ask` login, for a while."""
    _approved[(conversation, site)] = time.monotonic() + seconds


def login_approved(conversation: int, site: str) -> bool:
    return _approved.get((conversation, site), 0.0) > time.monotonic()


async def fill_login(session: BrowserSession, user_ref: str, pass_ref: str,
                     submit_ref: str = "") -> dict:
    """Have their username and password typed into a logged-in browser's page,
    and its button pressed. The one place a password is read: from the
    vault, into the driver -- whose `login_refusal` decides whether this
    page may have it -- and never into what is returned."""
    from core.browser import vault

    if not session.site:
        return {"ok": False, "error": "this browser is not one of the user's saved logins"}
    found = vault.vault().credentials(session.site)
    if found is None:
        return {"ok": False, "error": f"no username and password are saved for {session.site}; "
                                      "the user can add them in the live view"}
    username, password = found
    said = await session.ask("fill_login", user_ref=user_ref, pass_ref=pass_ref,
                             submit_ref=submit_ref, username=username, password=password,
                             domains=list(session.domains))
    return {k: v for k, v in said.items() if k in ("ok", "error", "url", "title")}


async def open_sandbox(key: int, **start) -> BrowserSession:
    """A new sandbox and its driver, started -- or BrowserUnavailable, with
    nothing left behind. `start` goes to the driver's `start`: a saved
    login's state and its site's domains, for a logged-in session."""
    if why := await unavailable():
        raise BrowserUnavailable(why)
    try:
        box = await asyncio.to_thread(START, sandbox.this_process())
    except Exception as exc:  # noqa: BLE001 - said to the model, not raised past it
        raise BrowserUnavailable(f"the browser sandbox did not start: {exc}"[:300]) from exc
    try:
        channel = await SPAWN(box)
    except Exception as exc:  # noqa: BLE001
        await asyncio.to_thread(STOP, box)
        raise BrowserUnavailable(f"the browser did not start: {exc}"[:300]) from exc
    session = BrowserSession(conversation=key, box=box, channel=channel)
    from agent.zones import local_zone
    from core import language

    tag = language.language()
    said = await session.ask("start", proxy=box.proxy, timezone=local_zone().key, locale=tag,
                             accept_language=language.accept_language(tag), **start)
    if not said.get("ok"):
        await session.close()
        raise BrowserUnavailable(f"the browser did not start: {said.get('error')}"[:300])
    return session


def get(conversation: int, site: str = "") -> BrowserSession | None:
    """A conversation's particular browser -- public, or logged in to `site`
    -- if it is still there. What a card's executor acts on: the browser
    the card was raised in, not whichever was used last."""
    session = _sessions.get((conversation, site or ""))
    return session if session is not None and session.alive() else None


def current(conversation: int) -> BrowserSession | None:
    """The conversation's browser it last used, if it is still there."""
    session = _sessions.get(_current.get(conversation, (conversation, "")))
    return session if session is not None and session.alive() else None


def hold(conversation: int, seconds: float) -> None:
    """Keep a conversation's browser open for a card waiting on their yes."""
    _held[conversation] = max(_held.get(conversation, 0.0), time.monotonic() + seconds)


def release(conversation: int) -> None:
    _held.pop(conversation, None)


async def close(conversation: int) -> bool:
    """Close all of a conversation's browsers."""
    keys = [k for k in _sessions if k[0] == conversation]
    _held.pop(conversation, None)
    for key in keys:
        await _close_key(key)
    return bool(keys)


async def discard(session: BrowserSession) -> None:
    """Close a browser that has died and take it out of the registry, so the
    next page opens in a fresh one instead of in the dead page's place. A
    logged-in one is not saved back: it is gone, and the vault keeps what it
    had. Never raises: a browser that would not close is not a second
    failure."""
    for key, held in list(_sessions.items()):
        if held is session:
            with contextlib.suppress(Exception):
                await _close_key(key)
            return
    with contextlib.suppress(Exception):
        await session.close()


async def close_site(conversation: int, site: str) -> bool:
    """Close the one browser of a conversation that is logged in to `site`,
    leaving its others. For a browser a site has turned away: it is already
    marked stale (`kept_state` then writes nothing over their login), and the
    next one should start from what the vault holds -- the user may have signed in
    again in the meantime -- not from the state that was refused."""
    key: Key = (conversation, site or "")
    if key not in _sessions:
        return False
    await _close_key(key)
    return True


async def close_all() -> None:
    for conversation in {k[0] for k in _sessions}:
        with contextlib.suppress(Exception):
            await close(conversation)


def open_sessions() -> int:
    return len(_sessions)


# Screenshots are kept this long: they are pages the user may want to see what
# the agent saw on, and pages can hold anything.
SCREENSHOT_DAYS = 14


def purge_screenshots(days: int = SCREENSHOT_DAYS, root: Path | None = None) -> int:
    """Remove screenshots older than `days`, and the folders they leave
    empty. Blocking. Returns how many went."""
    return _purge(root or paths.artifacts_dir() / "browser", "*/*.png", days)


def purge_quarantine(days: int = SCREENSHOT_DAYS, root: Path | None = None) -> int:
    """Remove quarantined downloads older than `days` (core/browser/files.py)."""
    from core.browser import files

    return _purge(root or files.quarantine_dir(), "*/*", days)


def _purge(root: Path, pattern: str, days: int) -> int:
    if not root.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    gone = 0
    for shot in root.glob(pattern):
        with contextlib.suppress(OSError):
            if shot.stat().st_mtime < cutoff:
                shot.unlink()
                gone += 1
    for folder in root.iterdir():
        with contextlib.suppress(OSError):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
    return gone


async def sweep(now: float | None = None) -> list[int]:
    """Close what is idle, too old or dead -- not what a waiting card holds,
    unless it is past the containers' own lifetime -- and reap a dead
    worker's leftovers. Returns the conversations closed."""
    now = time.monotonic() if now is None else now
    closed = []
    for key, session in list(_sessions.items()):
        held = _held.get(key[0], 0.0) > now
        idle = now - session.last_used > IDLE_S
        if not session.alive(now) or (idle and not held):
            with contextlib.suppress(Exception):
                await _close_key(key)
            if key[0] not in closed:
                closed.append(key[0])
    if _sessions or closed or now - _reap["at"] >= REAP_IDLE_S:
        _reap["at"] = now
        with contextlib.suppress(Exception):
            await asyncio.to_thread(sandbox.reap)
    return closed
