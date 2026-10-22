"""A stranger is walked from a fresh install to a working Steve, one question a page.

Before setup there was no way in but a terminal: the key went into the
Credential Manager by hand, and the app added itself to Windows' start-up
without asking (ui/src/Setup.tsx). These drive the shipped window in Edge
against the worker's own routes for setup, the cap, the profile and the
passphrase, with the keys checked by the fake OpenAI (tests/fakes/openai.py).
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import sqlite3
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from agent import appsettings, facts, runtime
from agent.attention import settings
from agent.worker import approvers, control, setup, signin
from core import db, hello, knobs, localapi, passphrase, secrets
from tests.fakes.jev import FakeJev
from tests.fakes.openai import GOOD, FakeOpenAI

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]


class _Keychain:
    """The Credential Manager, for one test."""

    def __init__(self) -> None:
        self.held: dict[str, str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.held.get(key)

    def set_password(self, service: str, key: str, value: str) -> None:
        self.held[key] = value


@pytest.fixture
def worker(monkeypatch):
    """The worker's state for one test: its database, the attention
    settings a preset moves, a keychain, and the fake OpenAI."""
    # The worker opens its connection on the thread that serves; here the
    # server has a thread of its own, and the test reads the same database
    # between pages, never while a page writes.
    with monkeypatch.context() as opening:
        opening.setattr(sqlite3, "connect", functools.partial(sqlite3.connect,
                                                              check_same_thread=False))
        conn = db.open_store()
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(signin, "_session", {"digest": None, "seen": 0.0})
    for name in ("MAX_DAY_PINGS", "MAX_NIGHT_PINGS", "FOLLOW_UP_AFTER"):
        monkeypatch.setattr(settings, name, getattr(settings, name))
    control.defaults()
    # An unencrypted drive, as this laptop's is, without asking the shell.
    monkeypatch.setattr(setup, "bitlocker", lambda drive: 2)
    held = _Keychain()
    monkeypatch.setattr(secrets.keyring, "get_password", held.get_password)
    monkeypatch.setattr(secrets.keyring, "set_password", held.set_password)
    # Docker Desktop installed and running, as on this laptop, without
    # reading it (the fences refuse that).
    monkeypatch.setattr(setup, "docker", lambda: {"installed": True, "running": True})
    fake = FakeOpenAI()
    with fake.running() as url, FakeJev().running() as jev_url:
        monkeypatch.setenv("STEVE_OPENAI_URL", url)
        monkeypatch.setenv("STEVE_OPENROUTER_URL", jev_url)
        yield conn, held
    conn.close()


def _backend(gate: dict | None = None, *, with_setup: bool = True):
    """Setup's routes and the worker's routes its pages write through, a
    socket that opens, and nothing else. While `gate["down"]`, the socket
    closes and opens no more, as a backend that stopped. Without setup,
    `/setup/` is not found, as on a backend older than setup."""
    gate = gate if gate is not None else {"down": False}

    async def socket(ws):
        if gate["down"]:
            await ws.close()
            return
        await ws.accept(subprotocol=localapi.subprotocol(ws.scope))
        while not gate["down"]:
            try:
                said = await asyncio.wait_for(ws.receive(), 0.1)
            except TimeoutError:
                continue
            if said["type"] == "websocket.disconnect":
                return
        await ws.close()

    async def anything(request):
        return JSONResponse({"detail": "not in this test"}, status_code=404)

    return Starlette(routes=[
        *(setup.ROUTES if with_setup else ()),
        Route("/control/knobs", control.set_knobs, methods=["POST"]),
        Route("/control/profile", control.profile, methods=["GET", "PUT"]),
        Route("/control/app", control.app_settings, methods=["GET", "POST"]),
        Route("/approvals/setup", approvers.setup),
        Route("/approvals/passphrase", approvers.choose_passphrase, methods=["POST"]),
        Route("/approvals/hello", approvers.register_hello, methods=["POST"]),
        *signin.ROUTES,
        WebSocketRoute("/ws", socket),
        Route("/{path:path}", anything, methods=["GET", "POST", "PUT"]),
    ])


def _until(check, within: float = 15.0):
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time")
        time.sleep(0.05)


def _on(page, question: str) -> None:
    page.get_by_role("heading", name=question).wait_for(timeout=15_000)


def _shell(page) -> list[str]:
    return [call["cmd"] for call in page.evaluate("window.__STEVE_SHELL__.calls")]


def test_a_fresh_install_is_walked_from_hello_to_the_app(screen, worker):
    """Every required page, and every optional one skipped, and the app
    opens after the last. Start-up with Windows is turned on only when the
    user says yes to it."""
    conn, held = worker
    page = screen(_backend())

    _on(page, "I'm Steve. Let's get me working.")
    page.get_by_role("button", name="Begin").click()

    _on(page, "Your OpenAI key")
    page.get_by_label("OpenAI key").fill(GOOD)
    page.get_by_role("button", name="Check the key").click()
    page.get_by_text("Works.").wait_for()
    # OpenAI's list of models says nothing of credit: the page says what
    # would, rather than promise a key that may stop at the first answer.
    page.get_by_text("If the account has no credit, a note says so").wait_for()
    assert held.held["OPENAI_API_KEY"] == GOOD
    page.get_by_role("button", name="Continue").click()

    _on(page, "How much may Steve spend a month?")
    page.get_by_role("button", name="Continue").click()

    _on(page, "Jev's key")
    assert knobs.read()["MONTH_USD"] == 15
    page.get_by_role("button", name="Skip for now").click()

    _on(page, "What should Steve call you?")
    page.get_by_label("Your name").fill("Maya Lindqvist")
    page.get_by_role("button", name="Continue").click()

    _on(page, "Choose a passphrase")
    assert facts.identity(conn)["name"] == "Maya Lindqvist"
    page.get_by_placeholder("A new passphrase, 8 characters or more").fill("blue kettle at dawn")
    page.get_by_placeholder("The same again").fill("blue kettle at dawn")
    page.locator(".setup-panel button.primary").click()

    _on(page, "Answer Steve from your phone")
    assert passphrase.is_set()
    page.get_by_role("button", name="Skip: answer only at this laptop").click()

    _on(page, "Your mail and calendar")
    page.get_by_role("button", name="Skip for now").click()

    _on(page, "Let Steve browse and run code?")
    page.get_by_role("button", name="Skip for now").click()

    _on(page, "How much should Steve do unasked?")
    assert not knobs.on("BROWSER") and not knobs.on("RUN_CODE")
    page.get_by_role("radio", name="Quiet").click()
    page.get_by_role("button", name="Continue").click()

    _on(page, "Start Steve with Windows?")
    assert knobs.read()["MAX_DAY_PINGS"] == 4
    assert "plugin:autostart|enable" not in _shell(page)
    page.get_by_role("button", name="No, I'll start it myself").click()

    _on(page, "Before you start")
    # What leaves is said whole: the screen's checks and the web leave too,
    # where the page once said nothing else left.
    for leaves in ("With an OpenRouter key, its checks", "Pages and searches you ask for"):
        page.get_by_text(leaves).wait_for()
    assert "plugin:autostart|enable" not in _shell(page)
    assert "plugin:autostart|disable" in _shell(page)
    page.get_by_role("button", name="Back").click()
    _on(page, "Start Steve with Windows?")
    page.get_by_role("button", name="Yes, start with Windows").click()

    _on(page, "Before you start")
    assert "plugin:autostart|enable" in _shell(page)
    page.get_by_text("isn't encrypted").wait_for()
    page.get_by_role("button", name="Start").click()

    page.locator("nav.sidebar").wait_for(timeout=15_000)
    assert setup.state(conn)["done"]
    # Setup's end makes the tour due, and the day opens on its first note.
    page.locator(".tour[data-stop='1']").wait_for(timeout=15_000)
    assert appsettings.current()["tour"] == "due"
    # Choosing the passphrase signed in: the app opens on the day, not on a
    # sign-in asking for the words just typed.
    assert signin.live(page.evaluate("sessionStorage.getItem('shell.session')"))
    page.reload()
    page.locator("nav.sidebar").wait_for(timeout=15_000)
    assert page.locator(".setup").count() == 0
    # Due until taken or skipped: closed halfway, it is there at the next start.
    page.locator(".tour[data-stop='1']").wait_for(timeout=15_000)


def test_a_pages_title_comes_in_word_by_word_and_its_text_after_it(screen, worker):
    """Each page arrived whole, and setup read as a form. A title's words
    fade in one after another, 60 ms apart and 240 ms each, the page's text
    once the title is in, and the welcome's mark draws itself in. With
    Windows' animations off, nothing moves."""
    page = screen(_backend())
    _on(page, "I'm Steve. Let's get me working.")
    words = page.locator(".setup-question .setup-word")
    assert words.count() == 6
    timing = words.evaluate_all("""els => els.map(e => {
      const s = getComputedStyle(e);
      return [s.animationDelay, s.animationDuration];
    })""")
    assert timing == [[f"{i * 0.06:.2g}s" if i else "0s", "0.24s"] for i in range(6)]
    follows = page.locator(".setup-follows")
    assert follows.evaluate("e => getComputedStyle(e).animationDelay") == "0.54s"
    assert page.locator(".setup-mark.draws").count() == 1

    page.emulate_media(reduced_motion="reduce")
    still = "e => getComputedStyle(e).animationName"
    assert words.first.evaluate(still) == "none" and follows.evaluate(still) == "none"
    assert page.locator(".setup-mark").evaluate(still) == "none"


def test_a_key_kept_from_before_is_shown_on_its_page_and_kept_with_one_press(screen, worker):
    """A key an earlier install left in the Credential Manager skipped the
    page, and the user never entered a key or learned where theirs was. The
    page shows, says a key is kept, and keeps it, or takes a new one."""
    conn, held = worker
    held.held["OPENAI_API_KEY"] = "sk-proj-kept-from-before"
    page = screen(_backend())
    _on(page, "I'm Steve. Let's get me working.")
    page.get_by_role("button", name="Begin").click()

    _on(page, "Your OpenAI key")
    assert "A key is already kept for Steve in Windows' Credential Manager." in page.inner_text(
        ".setup-page")
    page.get_by_role("button", name="Keep this key").click()
    _on(page, "How much may Steve spend a month?")
    assert setup.answered("openai") == "kept"
    assert held.held == {"OPENAI_API_KEY": "sk-proj-kept-from-before"}


def test_a_refused_key_says_why_and_setup_closed_halfway_resumes_where_it_was(screen, worker):
    """A key typed wrong is said so on its page and not kept; setup closed
    after the cap opens again at Jev's page, the first not done; and the
    cap's page, gone back to, keeps the cap chosen."""
    _, held = worker
    page = screen(_backend())
    _on(page, "I'm Steve. Let's get me working.")
    page.get_by_role("button", name="Begin").click()

    _on(page, "Your OpenAI key")
    page.get_by_label("OpenAI key").fill("sk-proj-typed-wrong")
    page.get_by_role("button", name="Check the key").click()
    page.get_by_role("alert").get_by_text("OpenAI did not accept this key").wait_for()
    assert "OPENAI_API_KEY" not in held.held

    page.get_by_label("OpenAI key").fill(GOOD)
    page.get_by_role("button", name="Check the key").click()
    page.get_by_text("Works.").wait_for()
    page.get_by_role("button", name="Continue").click()
    _on(page, "How much may Steve spend a month?")
    page.get_by_role("button", name="More").click()
    page.get_by_role("button", name="Continue").click()
    _on(page, "Jev's key")
    assert knobs.read()["MONTH_USD"] == 20

    page.reload()
    _on(page, "Jev's key")
    assert page.locator(".setup-progress li.done").count() == 3

    # Back on the cap's page, it shows the cap kept: it showed the $15
    # suggestion, and Continue wrote that over the user's own.
    page.get_by_role("button", name="Back").click()
    _on(page, "How much may Steve spend a month?")
    assert page.get_by_label("Dollars a month").input_value() == "20"
    page.get_by_role("button", name="Continue").click()
    _on(page, "Jev's key")
    assert knobs.read()["MONTH_USD"] == 20


def _up_to_the_docker_page(conn, held) -> None:
    held.held["OPENAI_API_KEY"] = GOOD
    for page, given in (("hello", "seen"), ("openai", "kept"), ("jev", "skipped"),
                        ("channel", "skipped"), ("accounts", "skipped")):
        setup._answer(conn, page, given)
    knobs.write({"MONTH_USD": 30})
    facts.add(conn, facts.USER, "name", "Maya Lindqvist", source="confirmed")
    assert passphrase.choose("correct horse battery") is None


def test_the_docker_page_offers_the_switches_only_with_docker_desktop_running_and_sees_it_start(
        screen, worker, monkeypatch):
    """Turned on without Docker Desktop, the browser and running code said
    nothing until Steve first tried one and failed. Not installed, the page
    links to Docker's download and installs nothing; installed, it says to
    start it; started while the page is open, it is seen without a press,
    and only then are the switches offered."""
    conn, held = worker
    machine = {"installed": False, "running": False}
    monkeypatch.setattr(setup, "docker", lambda: dict(machine))
    _up_to_the_docker_page(conn, held)
    page = screen(_backend())

    _on(page, "Let Steve browse and run code?")
    turn_on = page.get_by_role("button", name="Turn them on")
    page.get_by_text("Docker Desktop isn't installed.").wait_for()
    assert turn_on.is_disabled()
    page.get_by_role("button", name="Get Docker Desktop").click()
    _until(lambda: "open_link" in _shell(page))
    opened = [c["args"] for c in page.evaluate("window.__STEVE_SHELL__.calls")
              if c["cmd"] == "open_link"]
    assert opened == [{"url": "https://www.docker.com/products/docker-desktop/"}]

    machine["installed"] = True
    page.get_by_text("Docker Desktop is installed, and not running.").wait_for()
    assert turn_on.is_disabled()
    machine["running"] = True
    page.get_by_text("Docker Desktop is running.").wait_for()
    turn_on.click()

    _on(page, "How much should Steve do unasked?")
    assert knobs.on("BROWSER") and knobs.on("RUN_CODE")
    assert setup.answered("docker") == "on"


def test_setup_says_when_steve_is_not_answering_and_offers_to_start_it(screen, worker):
    """Its pages keep nothing while the backend is down: the page says so,
    and Start asks the app to start it, as Diagnostics does."""
    gate = {"down": False}
    page = screen(_backend(gate))
    _on(page, "I'm Steve. Let's get me working.")

    gate["down"] = True
    page.get_by_role("status").get_by_text("Steve isn't answering").wait_for()
    page.get_by_role("button", name="Start it").click()
    _until(lambda: any(call["cmd"] == "backend_control" and call["args"].get("action") == "start"
                       for call in page.evaluate("window.__STEVE_SHELL__.calls")))
    page.get_by_text("Starting Steve: give it half a minute.").wait_for()

    gate["down"] = False
    page.get_by_role("status").get_by_text("Starting Steve").wait_for(state="hidden",
                                                                      timeout=20_000)
    _on(page, "I'm Steve. Let's get me working.")


def test_a_backend_that_cannot_say_where_setup_stands_opens_the_app_at_once(screen):
    """A backend older than setup, or whose setup failed, has no answer:
    the window opens on the app as before, not on a wait or a setup that
    can't be finished, which would lock the user out of Steve."""
    page = screen(_backend(with_setup=False))
    page.locator("nav.sidebar").wait_for(timeout=5_000)
    assert page.locator(".setup").count() == 0


def _starting(page, state: str | None) -> None:
    """The page's clock, and the shell saying its backend is in `state`
    (None: no answer, as from an app with no backend of its own)."""
    page.clock.install()
    status = "null" if state is None else json.dumps(
        {"state": state, "pid": 1, "restarts": 0, "log_tail": [], "damaged": None})
    page.add_init_script(f"window.__STEVE_SHELL_PRESET__ = {{backend_control: {status}}};")


def _waiting(page) -> bool:
    time.sleep(0.3)  # the look after the clock moved: a call to the shell
    return (page.get_by_text("Starting Steve…").is_visible()
            and page.locator("nav.sidebar").count() == 0)


def test_a_first_start_still_starting_after_twenty_seconds_keeps_the_waiting_page(screen, worker):
    """The app opens anyway once its backend has had 20 s, so a backend that
    never comes up still leaves Start in reach. One the shell says is still
    starting is waited for: opened at 20 s, a slow first start showed a
    stranger an empty day page, then setup over it."""
    gate = {"down": True}
    page = screen(_backend(gate), before=lambda page: _starting(page, "starting"))
    page.get_by_text("Starting Steve…").wait_for()

    page.clock.run_for(25_000)
    assert _waiting(page), "opened while its backend was starting"
    page.clock.run_for(60_000)
    assert _waiting(page)

    gate["down"] = False
    heading = page.get_by_role("heading", name="I'm Steve. Let's get me working.")
    for _ in range(30):  # the socket's next try is on the page's clock
        if heading.is_visible():
            break
        page.clock.run_for(1000)
        time.sleep(0.1)
    _on(page, "I'm Steve. Let's get me working.")


def test_a_backend_that_never_answers_opens_the_app_after_twenty_seconds_or_two_minutes_starting(
        screen, worker):
    """Start must stay in reach: a backend that isn't starting is given 20 s,
    and one that says it is starting but never answers, two minutes."""
    page = screen(_backend({"down": True}), before=lambda page: _starting(page, None))
    page.get_by_text("Starting Steve…").wait_for()
    page.clock.run_for(21_000)
    page.locator("nav.sidebar").wait_for(timeout=5_000)

    page = screen(_backend({"down": True}), before=lambda page: _starting(page, "starting"))
    page.get_by_text("Starting Steve…").wait_for()
    page.clock.run_for(110_000)
    assert _waiting(page)
    page.clock.run_for(15_000)
    page.locator("nav.sidebar").wait_for(timeout=5_000)


def test_where_the_laptop_has_windows_hello_setup_offers_it_after_the_passphrase(screen, worker):
    """Then a glance signs in and says yes to spending. The key is made by
    Windows Hello under this install's name, and registered with the
    passphrase just chosen, not asked for again."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    page = screen(_backend(), before=lambda page: page.add_init_script(
        f"window.__STEVE_SHELL_PRESET__ = {{hello_available: true, "
        f"hello_enrol: {json.dumps(public)}}};"))

    _on(page, "I'm Steve. Let's get me working.")
    page.get_by_role("button", name="Begin").click()
    _on(page, "Your OpenAI key")
    page.get_by_label("OpenAI key").fill(GOOD)
    page.get_by_role("button", name="Check the key").click()
    page.get_by_text("Works.").wait_for()
    page.get_by_role("button", name="Continue").click()
    _on(page, "How much may Steve spend a month?")
    page.get_by_role("button", name="Continue").click()
    _on(page, "Jev's key")
    page.get_by_role("button", name="Skip for now").click()
    _on(page, "What should Steve call you?")
    page.get_by_label("Your name").fill("Maya Lindqvist")
    page.get_by_role("button", name="Continue").click()
    _on(page, "Choose a passphrase")
    page.get_by_placeholder("A new passphrase, 8 characters or more").fill("blue kettle at dawn")
    page.get_by_placeholder("The same again").fill("blue kettle at dawn")
    page.locator(".setup-panel button.primary").click()

    _on(page, "Use Windows Hello too")
    assert not hello.is_set()
    page.get_by_role("button", name="Set up Windows Hello").click()
    _on(page, "Answer Steve from your phone")
    assert hello.is_set()
    enrolled = [c["args"] for c in page.evaluate("window.__STEVE_SHELL__.calls")
                if c["cmd"] == "hello_enrol"]
    assert enrolled == [{"name": hello.key_name()}]
