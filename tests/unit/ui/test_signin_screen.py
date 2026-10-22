"""Signing in to the app: nothing of Steve's shows until the user has.

A laptop left unlocked showed whoever sat down everything Steve holds, and a
high-risk yes was the passphrase typed into a card. Now, once a passphrase
is set, the app signs in first, with Windows Hello where it is set up, and
the shell ends the session on lock, sleep and quit, and after 30 idle
minutes if the user turned that on (ui/src/SignIn.tsx, session.ts;
agent/worker/signin.py). The quick bar and
the desk card show nothing while signed out, and send the user to the app.

These drive the built windows in Edge against the real sign-in routes, with
a software key standing in for the one Windows Hello keeps.
"""

from __future__ import annotations

import asyncio
import base64
import sqlite3
import threading
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute

from agent import appsettings, runtime
from agent.worker import approvers, signin
from core import db, hello, localapi, monitor, passphrase, winsignin
from core.sandbox import win32 as sandbox_win32
from tests.unit.ui import test_back_pages_screen as back
from tests.unit.ui import test_day_screen as day
from tests.unit.ui import test_deskcard_screen as card
from tests.unit.ui import test_notes_screen as notes
from tests.unit.ui import test_quickbar_screen as bar

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

PASSPHRASE = notes.PASSPHRASE
SPENDS = notes._card(7, "Pay Larkspur Studio $240 for the logo", spends=True)


@pytest.fixture
def fresh(monkeypatch):
    """No session and no challenge left from another test, and a passphrase
    set, so the app must sign in."""
    monkeypatch.setattr(signin, "_session", {"digest": None, "seen": 0.0})
    monkeypatch.setattr(hello, "_open", {})
    assert passphrase.choose(PASSPHRASE) is None


@pytest.fixture
def key():
    """The key Windows Hello would keep, registered as this install's."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    assert hello.register(public, PASSPHRASE) is None
    return private


def _with_signin(app: Starlette, *routes: Route) -> Starlette:
    """A test's backend, with the worker's sign-in routes in front of it."""
    return Starlette(routes=[
        *routes,
        *signin.ROUTES,
        Route("/approvals/setup", approvers.setup),
        Mount("/", app=app),
    ])


def _hello_here(private):
    """What a page's `before` gives the shell: Hello on this laptop, and a
    prompt that signs with `private`, as the user's glance would."""
    def sign(message_b64: str) -> str:
        signature = private.sign(base64.b64decode(message_b64), padding.PKCS1v15(),
                                 hashes.SHA256())
        return base64.b64encode(signature).decode()

    def before(page):
        page.expose_function("helloSigns", sign)
        page.add_init_script("""window.__STEVE_SHELL_PRESET__ = {
          hello_available: true,
          hello_sign: (args) => window.helloSigns(args.message),
        };""")
    return before


def _until(check, within: float = 15.0):
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time")
        time.sleep(0.05)


def _calls(page, cmd: str) -> list[dict]:
    return [c["args"] for c in page.evaluate("window.__STEVE_SHELL__.calls") if c["cmd"] == cmd]


def _welcome(page) -> None:
    page.get_by_role("heading", name="Welcome back").wait_for(timeout=15_000)


def _held(page) -> str | None:
    return page.evaluate("sessionStorage.getItem('shell.session')")


def test_with_a_passphrase_set_the_app_shows_nothing_until_it_is_typed(screen, fresh):
    """The day, the notes and the sidebar stay behind the sign-in; a wrong
    passphrase is refused and says so; the right one opens the app, and
    the shell holds a session the backend knows."""
    page = screen(_with_signin(notes.Backend([SPENDS]).app()))
    _welcome(page)
    assert page.locator(".day-sheet, article.note, nav").count() == 0
    assert "Larkspur" not in page.inner_text("body")
    assert page.get_by_text("Forgotten? Reset it with your Windows sign-in").count() == 1

    page.get_by_label("Your passphrase").fill("maple leaves in november")
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.get_by_text("that is not the passphrase").wait_for(timeout=10_000)
    assert _held(page) is None

    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.locator(".day-sheet").first.wait_for(timeout=15_000)
    assert signin.live(_held(page))


def test_a_passphrase_whose_file_was_deleted_is_reset_on_the_sign_in_page_by_windows_sign_in(
        screen, fresh, monkeypatch):
    """A deleted passphrase.json locks rather than unsets (the step 11
    audit, E1), so its owner needs a way back from the sign-in page itself:
    without Hello, Windows' own sign-in."""
    db.open_store().close()
    assert passphrase.state() == "set"
    passphrase.path().unlink()
    asked = []
    monkeypatch.setattr(winsignin, "_ask", lambda why: asked.append(why) or
                        sandbox_win32.user_sid())
    page = screen(_with_signin(notes.Backend([]).app(), Route(
        "/approvals/passphrase/reset/windows", approvers.reset_passphrase_by_windows,
        methods=["POST"])))
    _welcome(page)
    page.get_by_text("passphrase file was removed or changed").wait_for()

    page.get_by_role("button", name="Forgotten it?").click()
    new = "plum trees by the river"
    page.get_by_placeholder("A new passphrase, 8 characters or more").fill(new)
    page.get_by_placeholder("The same again").fill(new)
    page.get_by_role("button", name="Reset it with your Windows sign-in").click()
    page.locator(".day-sheet").first.wait_for(timeout=15_000)
    assert asked and passphrase.check(new) is None and signin.live(_held(page))


def test_the_passphrase_goes_only_to_a_backend_proven_just_before(screen, fresh):
    """The app trusted its last proof of the backend until a request
    failed. After a restart, a program that took the old port and answered
    everything failed none, and was sent the passphrase (step 11's audit).
    Here the backend restarts, with a new token, while the sign-in is
    shown: the passphrase goes with the new one."""
    heard: list[str | None] = []

    async def recorded(request: Request):
        heard.append(request.headers.get("authorization"))
        return await signin.with_passphrase(request)

    page = screen(_with_signin(notes.Backend([SPENDS]).app(),
                               Route("/signin/passphrase", recorded, methods=["POST"])))
    _welcome(page)
    page.evaluate("window.__STEVE_SHELL__.backend.token = 'after-the-restart'")
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.locator(".day-sheet").first.wait_for(timeout=15_000)

    assert heard == ["Bearer after-the-restart"]


def test_once_the_socket_closes_the_app_proves_the_backend_again(screen, fresh):
    """A socket closes when its backend stops: the first word of a
    restart. The app kept its last proof, and the socket it opened next,
    and every read, went with it to whatever then held the old port (step
    11's audit)."""
    tokens: list[str] = []
    restarted = threading.Event()

    async def socket(ws):
        tokens.append(next((p for p in ws.scope.get("subprotocols", [])
                            if p.startswith("token.")), ""))
        first = len(tokens) == 1
        await ws.accept(subprotocol=localapi.subprotocol(ws.scope))
        if first:
            while not restarted.is_set():
                await asyncio.sleep(0.05)
            await ws.close()
            return
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass

    page = screen(_with_signin(notes.Backend([SPENDS]).app(), WebSocketRoute("/ws", socket)))
    _welcome(page)
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.locator(".day-sheet").first.wait_for(timeout=15_000)
    _until(lambda: len(tokens) == 1)

    page.evaluate("window.__STEVE_SHELL__.backend.token = 'after-the-restart'")
    restarted.set()
    _until(lambda: len(tokens) == 2)

    assert tokens[1] == "token.after-the-restart" != tokens[0]


def test_a_lock_signs_every_page_out_and_says_why(screen, fresh):
    """The shell's word that Windows locked takes the app back to the
    sign-in at once, with the reason, and shows nothing of the day."""
    page = screen(_with_signin(notes.Backend([SPENDS]).app()))
    _welcome(page)
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.locator(".day-sheet").first.wait_for(timeout=15_000)

    page.evaluate("window.__STEVE_SHELL__.signOut('locked')")
    _welcome(page)
    page.get_by_text("Signed out when Windows locked.").wait_for(timeout=5_000)
    assert page.locator(".day-sheet").count() == 0


class _CantSay:
    """The backend's word on the sign-in, until `broken`: then it can't
    say, as while it restarts after a lapse."""

    def __init__(self):
        self.broken = False
        self.refused = 0

    async def state(self, request: Request) -> JSONResponse:
        if self.broken:
            self.refused += 1
            return JSONResponse({"ok": False, "error": "starting"}, status_code=503)
        return await signin.state(request)

    def route(self) -> Route:
        return Route("/signin/state", self.state)


def test_a_session_ended_while_the_backend_cant_say_still_signs_the_app_out(screen, fresh):
    """At a session's end the app asked whether a sign-in was needed, and
    took a backend that couldn't answer as a no: the day it held opened
    again to whoever was at the desk. Once known, the answer stands."""
    cant = _CantSay()
    page = screen(_with_signin(notes.Backend([SPENDS]).app(), cant.route()))
    _welcome(page)
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.locator(".day-sheet").first.wait_for(timeout=15_000)

    cant.broken = True
    page.evaluate("window.__STEVE_SHELL__.signOut('locked')")
    _until(lambda: cant.refused)
    page.wait_for_timeout(500)
    assert page.get_by_role("heading", name="Welcome back").count() == 1
    assert page.locator(".day-sheet, article.note, nav").count() == 0


def test_a_session_ended_while_the_backend_cant_say_still_hides_the_desk_card(screen, fresh):
    """The desk card asks the same at each end of a session, and showed its
    lines again on a backend that couldn't answer."""
    cant = _CantSay()
    backend = card.Backend([bar.CARD])
    held = signin.start()

    def signed_in(page):
        page.add_init_script(f"sessionStorage.setItem('shell.session', {held!r});")

    page = screen(_with_signin(backend.app(), cant.route()), label="deskcard",
                  path="/#deskcard", before=signed_in)
    _until(lambda: backend.looks and _calls(page, "deskcard_want"))
    away = ".desk-tile [aria-label='Steve: sign in to see more']"
    assert page.locator(away).count() == 0, "signed in, the card shows what it has"

    cant.broken = True
    page.evaluate("window.__STEVE_SHELL__.signOut('locked')")
    _until(lambda: page.locator(away).count())


def test_signed_out_a_toast_says_only_that_something_came(screen, fresh):
    """The socket stays open signed out, and a card or a reminder made a
    toast with its text, which the app's own pages keep behind the
    sign-in. The shell is told to hide it until the user signs in."""
    backend = day.Backend()
    page = screen(_with_signin(backend.app()))
    _welcome(page)
    backend.say.put(day._inbox("card", "Send the reply to Theo Brandt?", 1))
    _until(lambda: _calls(page, "notify"))
    assert _calls(page, "notify")[-1]["signedOut"] is True

    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.locator(".day-sheet").first.wait_for(timeout=15_000)
    backend.say.put(day._inbox("reminder", "Call Contoso", 2))
    _until(lambda: len(_calls(page, "notify")) == 2)
    assert _calls(page, "notify")[-1]["signedOut"] is False


def test_without_a_passphrase_the_app_opens_as_before(screen, monkeypatch):
    """A sign-in is asked for only once there is something to sign in with:
    a fresh install, or one that never chose a passphrase, opens straight
    to the day."""
    monkeypatch.setattr(signin, "_session", {"digest": None, "seen": 0.0})
    page = screen(_with_signin(notes.Backend([SPENDS]).app()))
    page.locator(".day-sheet").first.wait_for(timeout=15_000)
    assert page.get_by_role("heading", name="Welcome back").count() == 0


def test_with_windows_hello_set_up_the_app_asks_for_it_at_once(screen, fresh, key):
    """In front, the sign-in asks Windows Hello by itself, so a glance opens
    the app; the backend checks the signature over its own challenge."""
    page = screen(_with_signin(notes.Backend([SPENDS]).app()), before=_hello_here(key))
    page.locator(".day-sheet").first.wait_for(timeout=15_000)
    assert [c["name"] for c in _calls(page, "hello_sign")] == [hello.key_name()]
    assert signin.live(_held(page))


def test_a_hello_that_signs_with_another_key_is_refused(screen, fresh, key):
    """A key that is not the one registered signs nothing in: the page says
    so and offers the passphrase."""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    page = screen(_with_signin(notes.Backend([SPENDS]).app()), before=_hello_here(other))
    _welcome(page)
    page.get_by_text("did not match").wait_for(timeout=15_000)
    assert _held(page) is None
    page.get_by_role("button", name="Use my passphrase").click()
    page.get_by_text("Forgotten? Sign in with Windows Hello, then reset it").wait_for()
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.locator(".day-sheet").first.wait_for(timeout=15_000)


def test_a_high_risk_yes_carries_a_hello_signature_over_that_card(screen, fresh, key, monkeypatch):
    """The note's Windows Hello button asks for a challenge bound to the
    card, signs it, and the yes carries the signature and the session; the
    backend's check of both is the real one."""
    monkeypatch.setattr(runtime, "conn", sqlite3.connect(":memory:", check_same_thread=False))
    monkeypatch.setattr(monitor, "hello_binding",
                        lambda conn, action_id: f"{action_id}:3f2a" if action_id == 7 else None)
    backend = notes.Backend([SPENDS])
    decided: list[dict] = []

    async def decide(request: Request):
        body = await request.json()
        decided.append(body)
        if not signin.live(body.get("session")):
            return JSONResponse({"ok": False, "needs": "signin"}, status_code=400)
        proof = body.get("hello") or {}
        if why := hello.verify(proof.get("nonce", ""), proof.get("signature", ""), "yes",
                               f"{body['action_id']}:3f2a"):
            return JSONResponse({"ok": False, "error": why, "needs": "passphrase"},
                                status_code=400)
        backend.needs = []
        return JSONResponse({"ok": True})

    page = screen(_with_signin(backend.app(), Route("/actions/decide", decide, methods=["POST"])),
                  before=_hello_here(key))
    page.set_viewport_size({"width": 1300, "height": 800})
    note = notes._note(page, "card:7")
    note.wait_for(timeout=15_000)
    note.get_by_role("button", name="Yes").click()
    note.get_by_role("button", name="Use Windows Hello").click()
    _until(lambda: note.count() == 0)
    assert len(decided) == 1 and decided[0]["approve"] is True
    assert "passphrase" not in decided[0]


def test_a_yes_the_backend_refuses_for_want_of_a_session_brings_back_the_sign_in(screen, fresh):
    """A backend that restarted holds no session; the app's still did. The
    refused yes ends the app's too, so the user signs in again rather than
    pressing a Yes that can never work."""
    backend = notes.Backend([notes._card(4, "Tell Theo the deck is ready")])

    async def decide(request: Request):
        body = await request.json()
        if not signin.live(body.get("session")):
            return JSONResponse({"ok": False, "error": "sign in to Steve again first",
                                 "needs": "signin"}, status_code=400)
        backend.needs = []
        return JSONResponse({"ok": True})

    page = screen(_with_signin(backend.app(), Route("/actions/decide", decide, methods=["POST"])))
    page.set_viewport_size({"width": 1300, "height": 800})
    _welcome(page)
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    note = notes._note(page, "card:4")
    note.wait_for(timeout=15_000)

    signin.end()
    note.get_by_role("button", name="Yes").click()
    _welcome(page)
    assert _held(page) is None
    assert backend.needs, "the card was answered without a session"


class _SignedBar(bar.Backend):
    def app(self) -> Starlette:
        return _with_signin(super().app())


class _SignedCard(card.Backend):
    def app(self) -> Starlette:
        return _with_signin(super().app())


def test_signed_out_the_quick_bar_shows_nothing_and_hears_nothing(screen, fresh):
    """Over another window, it says to sign in and opens the app; no note,
    no next event, no last entry, and a held key starts no listening."""
    backend = _SignedBar([bar.CARD])
    page = bar._open(screen, backend)
    page.get_by_text("Sign in to Steve first.").wait_for(timeout=10_000)
    body = page.inner_text("body")
    assert "Design review" not in body and bar.LAST not in body
    assert page.locator("textarea").count() == 0

    page.evaluate("window.__STEVE_SHELL__.emit('quickbar-talk', true)")
    time.sleep(0.5)
    assert page.locator(".quickbar-listening").count() == 0
    assert not [m for m in backend.sent if m.get("type") == "voice_start"]

    page.get_by_role("button", name="Open Steve").click()
    _until(lambda: _calls(page, "quickbar_open_app"))


def test_signed_in_a_press_in_the_quick_bar_carries_the_session(screen, fresh):
    """The backend opens no microphone for a press without the live session
    (agent/worker/routes.py): the bar sends the one the shell holds."""
    backend = _SignedBar([bar.CARD])
    page = bar._open(screen, backend)
    page.get_by_text("Sign in to Steve first.").wait_for(timeout=10_000)
    session = signin.start()
    page.evaluate("""(secret) => {
      sessionStorage.setItem('shell.session', secret);
      window.__STEVE_SHELL__.emit('session', { live: true });
    }""", session)
    page.locator("textarea").wait_for(timeout=10_000)

    page.evaluate("window.__STEVE_SHELL__.emit('quickbar-talk', true)")
    _until(lambda: [m for m in backend.sent if m.get("type") == "voice_start"])
    assert [m for m in backend.sent if m.get("type") == "voice_start"] == [
        {"type": "voice_start", "session": session}]


def test_signed_out_the_desk_card_is_the_mark_alone_and_opens_the_app(screen, fresh):
    """No line, no peek of a note waiting, and nothing on a pointer: the
    card says nothing to whoever passes. A click opens the app to sign in."""
    backend = _SignedCard([bar.CARD])
    page = screen(backend.app(), label="deskcard", path="/#deskcard")
    _until(lambda: page.locator(".desk-tile [aria-label='Steve: sign in to see more']").count())
    _until(lambda: _calls(page, "deskcard_want"))
    assert backend.looks == 0, "signed out, the card asked what needs the user"
    assert page.locator(".desk-peek").count() == 0
    page.locator(".desk-tile").hover()
    time.sleep(0.5)
    assert page.locator(".desk-lines").count() == 0
    page.locator(".desk-tile").click()
    _until(lambda: _calls(page, "deskcard_open"))


def test_signed_out_the_trays_take_notes_brings_the_sign_in_and_starts_nothing(screen, fresh):
    """The tray's menu is there whether or not anyone has signed in, and its
    "Take notes on what's playing" started notes for whoever was at the
    desk: the user's own switch, hearing and spending for them. Signed out
    it brings the sign-in, and its tick goes back; signed in, it works."""
    backend = day.Backend()
    tapped = []

    async def tap(request: Request):
        tapped.append(await request.json())
        return JSONResponse({"ok": True})

    page = screen(_with_signin(backend.app(), Route("/voice/tap", tap, methods=["POST"])))
    _welcome(page)
    page.evaluate("window.__STEVE_SHELL__.emit('tap-toggle', null)")
    _until(lambda: tapped or _calls(page, "plugin:window|set_focus"))
    assert tapped == [], "signed out, notes started from the tray"
    assert _calls(page, "set_tapping")[-1] == {"on": False}

    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.locator(".day-sheet").first.wait_for(timeout=15_000)
    page.evaluate("window.__STEVE_SHELL__.emit('tap-toggle', null)")
    _until(lambda: tapped == [{"on": True}])


def test_signed_out_the_trays_show_the_desk_card_leaves_the_users_setting_alone(screen, fresh):
    """"Show the desk card" turned the user's setting back on for whoever
    was at the desk. Signed out, the setting stays as they left it and the
    app opens on its sign-in."""
    appsettings.write({"desk_card": "off"})
    page = screen(_SignedCard([]).app(), label="deskcard", path="/#deskcard")
    _until(lambda: _calls(page, "deskcard_want"))

    def tray_click():
        # Until the card has read its settings, a click has nothing to go
        # by and does nothing: clicked once, the test lost that race under
        # the check's load.
        page.evaluate("window.__STEVE_SHELL__.emit('deskcard-show', null)")
        return _calls(page, "deskcard_open") or appsettings.read().get("desk_card") == "on"

    _until(tray_click)
    assert appsettings.read().get("desk_card") == "off", "signed out, the tray turned it on"
    assert _calls(page, "deskcard_open")
    assert _calls(page, "deskcard_want")[-1]["on"] is False


def test_a_forgotten_passphrase_is_reset_in_settings_with_a_glance(screen, fresh, key):
    """Forgotten, the passphrase was reset only by deleting a file from the
    data folder, which the form said and the user had to find. Signed in
    with Hello, Settings offers the reset, and the glance stands in for the
    old passphrase."""
    new = "plum trees by the river"
    page = screen(_with_signin(back.Backend().app(),
                               Route("/approvals/passphrase/reset", approvers.reset_passphrase,
                                     methods=["POST"])),
                  before=_hello_here(key))
    page.set_viewport_size({"width": 1300, "height": 860})
    page.locator("nav.sidebar").wait_for(timeout=15_000)
    back._go(page, "Settings")
    page.locator(".knob", has_text="Passphrase, for a yes").get_by_role("button",
                                                                        name="change").click()
    page.get_by_text("it can be reset here with Windows Hello").wait_for()
    page.get_by_role("button", name="Forgotten it?").click()
    assert page.get_by_placeholder("The passphrase now").count() == 0

    page.get_by_placeholder("A new passphrase, 8 characters or more").fill(new)
    page.get_by_placeholder("The same again").fill(new)
    page.get_by_role("button", name="Reset it with Windows Hello").click()
    page.get_by_placeholder("The same again").wait_for(state="detached", timeout=10_000)
    assert passphrase.check(new) is None
    assert [c["name"] for c in _calls(page, "hello_sign")] == [hello.key_name()] * 2
