"""The Extensions page in the back pages' terms, and a module's card
answered with Windows Hello.

Step 6's page took the back pages' stylesheet but kept its tabs' shapes:
boxed cards, a switch, chips for a choice of two, and confirmations in
boxes of their own. And a module's card took the passphrase alone, so a
user who signed in and said yes to spending with a glance had to find the
passphrase for the one card that also asks for it.

The backend is the worker's own routes for modules and sign-in, on a
database whose cards admission made with the unit tests' fakes for its
screen, reviewer and test run (tests/unit/test_lifecycle.py).
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import functools
import sqlite3

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import modadmit, runtime
from agent.worker import accounts as account_routes
from agent.worker import signin
from core import db, hello, lifecycle, modules, passphrase, paths
from tests.unit.preview_ui import _named, _tides
from tests.unit.test_lifecycle import Reviewer, Screen, Trial, _write
from tests.unit.ui import test_back_pages_screen as back
from tests.unit.ui import test_notes_screen as notes
from tests.unit.ui.test_signin_screen import _calls, _until, _welcome, _with_signin

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

PASSPHRASE = notes.PASSPHRASE


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Tides imported and on; a card for a module Steve built, which reads
    the mail and reaches a host, so it needs the passphrase; a card for an
    imported one; a build under way; and a report."""
    monkeypatch.setattr(signin, "_session", {"digest": None, "seen": 0.0})
    monkeypatch.setattr(hello, "_open", {})
    assert passphrase.choose(PASSPHRASE) is None
    # The server has a thread of its own, as in test_setup_screen.py; the
    # test reads the same database between clicks, never while a page writes.
    with monkeypatch.context() as opening:
        opening.setattr(sqlite3, "connect", functools.partial(sqlite3.connect,
                                                              check_same_thread=False))
        conn = db.open_store(paths.db_path())
    monkeypatch.setattr(runtime, "conn", conn)

    def admit(source, **kw):
        # In a thread, on a connection of its own: Playwright's own loop is
        # running in this one, and asyncio.run refuses to start beside it.
        def run():
            own = db.open_store(paths.db_path())
            try:
                return asyncio.run(modadmit.admit(own, source, screener=Screen(),
                                                  reviewer=Reviewer(), tester=Trial(), **kw))
            finally:
                own.close()

        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(run).result()

    lifecycle.approve(conn, admit(_tides(tmp_path, "1"), origin="imported"))
    modules.set_enabled(conn, "tides", True)
    built = admit(_write(tmp_path / "webby"), origin="built", came_from="a conversation",
                  request="Look things up on Webby for me", tools=lambda: {"web_fetch"})
    imported = admit(_named(_write(tmp_path / "almanac", id="almanac",
                                   hosts=("api.almanac.example",), reads=()),
                            "almanac", "Almanac", "Sunrise, sunset and the moon"),
                     origin="imported")
    build = conn.execute(
        "INSERT INTO module_builds (request, origin, came_from, state, estimate_usd, estimate_s, "
        "cost_usd, seconds) VALUES ('Read the ferry timetable', 'user', 'a conversation', "
        "'running', 0.8, 720, 0.31, 240)").lastrowid
    conn.execute("INSERT INTO module_reports (build_id, kind, text) VALUES (?, 'slot', ?)",
                 (build, "A doorbell slot: a module told when someone rings."))
    conn.commit()
    yield conn, {"built": built, "imported": imported}
    conn.close()


def _extensions(page):
    page.locator("nav.sidebar").wait_for(timeout=15_000)
    back._go(page, "Extensions")
    page.locator(".extensions").wait_for(timeout=15_000)


def _answered(page, admission: int) -> None:
    """Waits for the card to go, its answer taken, or fails with what the
    page said instead: an answer sent without the sign-in's session is
    refused in words on the card, or signs the app out."""
    card = f'[data-admission="{admission}"]'
    page.wait_for_function(
        "sel => !document.querySelector(sel) || document.querySelector('.signin')"
        " || document.querySelector(sel + ' [role=alert]')", arg=card, timeout=10_000)
    said = page.locator(f"{card} [role=alert]")
    assert page.locator(card).count() == 0, (
        said.inner_text() if said.count() else "the app signed out")


def _choices(card) -> list[str]:
    return [t.strip() for t in card.locator(".form-actions button").all_inner_texts()]


def _switched(page, module_id: str) -> bool:
    return page.locator(f'[data-module="{module_id}"] .form-check input').first.is_checked()


def test_extensions_reads_as_a_back_page_and_its_card_keeps_section_fives_order(screen, world):
    """Each section says what it is for; nothing is a boxed card, a switch,
    a chip or a boxed question; a card opens with core's reading of the
    manifest, then the request, then the module's own words, then the
    details; an import shows its hash first; a module's switch asks before
    it turns one off; and Remove names what it deletes before it does."""
    conn, cards = world
    page = screen(_with_signin(back.Backend().app(), *account_routes.ROUTES))
    _welcome(page)
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    _extensions(page)

    assert page.locator(".extensions .form-title").all_inner_texts() == [
        "Waiting for your yes", "Installed", "Builds", "Reports"]
    assert page.locator(".extensions .form-why").count() == 4
    assert page.locator(".extensions :is(.item, .inline-check, .chips, .question, "
                        ".status-pill)").count() == 0

    card = page.locator(f'[data-admission="{cards["built"]}"]')
    text = card.inner_text()
    order = [text.index(s) for s in ("What it can do, as Steve reads its manifest",
                                     "Asked for in a conversation", "The module's own words",
                                     "Details")]
    assert order == sorted(order), text
    assert _choices(card) == ["Approve and turn on", "Approve, leave off", "Decline"]

    digest = conn.execute("SELECT hash FROM module_admissions WHERE id = ?",
                          (cards["imported"],)).fetchone()[0]
    imported = page.locator(f'[data-admission="{cards["imported"]}"]')
    warning = imported.locator(".form-warning").first.inner_text()
    assert "Imported, so unsigned" in warning and digest in warning

    tides = page.locator('[data-module="tides"]')
    tides.locator(".form-check").click()
    tides.get_by_text("Nothing is deleted.").wait_for()
    tides.get_by_role("button", name="Keep it on").click()
    assert tides.get_by_text("Nothing is deleted.").count() == 0
    assert modules.module(conn, "tides").enabled
    tides.locator(".form-check").click()
    tides.get_by_role("button", name="Turn off").click()
    _until(lambda: not modules.module(conn, "tides").enabled)

    # Destructive is red ink (docs/design/back-pages.md, Forms): the back
    # pages' rule for every other button outranked it.
    remove = tides.get_by_role("button", name="Remove", exact=True)
    red = page.evaluate("""() => {
      const probe = document.createElement("span");
      probe.style.color = "var(--red-ink)";
      document.body.append(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      return color;
    }""")
    assert remove.evaluate("b => getComputedStyle(b).color") == red
    remove.click()
    tides.get_by_text("Remove Tides? Its files, secrets, accounts and sandbox are").wait_for()
    tides.get_by_role("button", name="Keep", exact=True).click()
    tides.get_by_role("button", name="Export").wait_for()
    assert modules.module(conn, "tides") is not None

    card.get_by_label("Your passphrase").fill(PASSPHRASE)
    card.get_by_role("button", name="Approve, leave off").click()
    _answered(page, cards["built"])
    assert modules.module(conn, "webby") is not None


def test_where_steve_can_build_the_page_asks_for_a_tool_and_the_ask_goes_to_the_conversation(
        screen, world):
    """Building its own tools was Steve's least visible power: nothing on
    screen said it could be asked for. Where the sandbox works the page
    opens with the ask, and it goes to the conversation as the user's own
    message, where a build is followed and said yes to."""
    backend = back.Backend()

    async def places(request):
        return JSONResponse({"ok": True, "coming": 0, "calls": 0,
                             "extensions": {"installed": 1, "building": 1, "can_build": True}})

    page = screen(_with_signin(backend.app(), Route("/control/places", places),
                               *account_routes.ROUTES))
    _welcome(page)
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    _extensions(page)
    _until(lambda: page.locator(".extensions .form-title").first.inner_text() == "Ask for one")
    ask = page.get_by_role("button", name="Ask Steve")
    assert ask.is_disabled(), "an empty ask sends nothing"
    page.get_by_label("Describe a tool Steve doesn't have").fill("tracks my plants")
    ask.click()
    _until(lambda: page.locator(".back-page").count() == 0)
    _until(lambda: [m.get("text") for m in backend.said if m.get("type") == "chat"] == [
        "Build me a tool that tracks my plants"])


def test_an_extension_steve_built_can_be_improved_from_its_card_and_no_other_can(screen, world):
    """Asking Steve to improve a tool it built had no way in from the page
    that lists it. A module Steve built offers Improve where Steve can
    build; the ask goes to the conversation as the user's own message,
    naming the module's id, which the build tool takes for a new version.
    An imported one offers none: Steve rewrites only what it wrote; and
    none does where Steve cannot build."""
    conn, cards = world
    lifecycle.approve(conn, cards["built"], given=PASSPHRASE)
    backend = back.Backend()
    can = {"build": False}

    async def places(request):
        return JSONResponse({"ok": True, "coming": 0, "calls": 0, "extensions": {
            "installed": 2, "building": 1, "can_build": can["build"]}})

    page = screen(_with_signin(backend.app(), Route("/control/places", places),
                               *account_routes.ROUTES))
    _welcome(page)
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    _extensions(page)
    built = page.locator('[data-module="webby"]')
    built.get_by_role("button", name="Export").wait_for(timeout=10_000)
    assert built.get_by_role("button", name="Improve").count() == 0

    # The places are read again every five seconds.
    can["build"] = True
    _until(lambda: built.get_by_role("button", name="Improve").count() == 1, within=20)
    assert page.locator('[data-module="tides"]').get_by_role("button", name="Improve").count() \
        == 0

    built.get_by_role("button", name="Improve").click()
    ask = built.get_by_role("button", name="Ask Steve")
    assert ask.is_disabled(), "an empty ask sends nothing"
    built.get_by_label("What should it do better?").fill("take ounces as well as grams")
    ask.click()
    _until(lambda: page.locator(".back-page").count() == 0)
    label = modules.module(conn, "webby").manifest.label
    _until(lambda: [m.get("text") for m in backend.said if m.get("type") == "chat"] == [
        f"Improve the {label} extension you built (webby): take ounces as well as grams"])


def test_a_new_extensions_main_choice_approves_it_and_turns_it_on_and_a_wrong_yes_does_neither(
        screen, world):
    """An approved extension dropped into the list switched off, where it
    was easy to miss. The card's main choice is Approve and turn on, then
    Approve, leave off, then Decline. A wrong passphrase installs nothing
    and turns nothing on, says why on the card, and the same button tries
    again; Decline installs nothing."""
    conn, cards = world
    page = screen(_with_signin(back.Backend().app(), *account_routes.ROUTES))
    _welcome(page)
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()
    _extensions(page)

    card = page.locator(f'[data-admission="{cards["built"]}"]')
    assert _choices(card) == ["Approve and turn on", "Approve, leave off", "Decline"]
    main = card.get_by_role("button", name="Approve and turn on")
    assert "primary" in (main.get_attribute("class") or "")

    card.get_by_label("Your passphrase").fill("not the passphrase")
    main.click()
    card.get_by_role("alert").wait_for(timeout=10_000)
    assert modules.module(conn, "webby") is None
    card.get_by_label("Your passphrase").fill(PASSPHRASE)
    main.click()
    _answered(page, cards["built"])
    assert modules.module(conn, "webby").enabled
    page.locator('[data-module="webby"]').wait_for(timeout=10_000)
    assert _switched(page, "webby")

    imported = page.locator(f'[data-admission="{cards["imported"]}"]')
    imported.get_by_role("button", name="Decline").click()
    _answered(page, cards["imported"])
    assert modules.module(conn, "almanac") is None
    assert lifecycle.waiting(conn) == []


def _hello_refusable(private):
    """`_hello_here`, with a glance the test can refuse, as the user's
    Cancel on Windows' prompt."""
    def sign(message_b64: str) -> str:
        signature = private.sign(base64.b64decode(message_b64), padding.PKCS1v15(),
                                 hashes.SHA256())
        return base64.b64encode(signature).decode()

    def before(page):
        page.expose_function("helloSigns", sign)
        page.add_init_script("""window.__STEVE_SHELL_PRESET__ = {
          hello_available: true,
          hello_sign: (args) => window.refuseHello ? Promise.reject("cancelled")
            : window.helloSigns(args.message),
        };""")
    return before


def test_a_card_that_needs_the_passphrase_takes_a_glance_where_steve_has_windows_hello(
        screen, world):
    """Signed in with Hello, a card that needs the passphrase takes a glance
    when none is typed, with either choice, and the glance signs this
    card's challenge. A glance refused installs nothing and turns nothing
    on, and the card says so. A card that asks no passphrase asks no
    glance."""
    conn, cards = world
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    assert hello.register(public, PASSPHRASE) is None
    page = screen(_with_signin(back.Backend().app(), *account_routes.ROUTES),
                  before=_hello_refusable(private))
    _extensions(page)

    card = page.locator(f'[data-admission="{cards["built"]}"]')
    assert _choices(card) == ["Approve and turn on", "Approve, leave off", "Decline"]
    page.evaluate("window.refuseHello = true")
    card.get_by_role("button", name="Approve and turn on").click()
    card.get_by_role("alert").wait_for(timeout=10_000)
    assert "Cancelled." in card.get_by_role("alert").inner_text()
    assert modules.module(conn, "webby") is None

    page.evaluate("window.refuseHello = false")
    card.get_by_role("button", name="Approve, leave off").click()
    _answered(page, cards["built"])
    installed = modules.module(conn, "webby")
    assert installed is not None and not installed.enabled
    assert [c["name"] for c in _calls(page, "hello_sign")] == [hello.key_name()] * 3

    imported = page.locator(f'[data-admission="{cards["imported"]}"]')
    imported.get_by_role("button", name="Approve and turn on").click()
    _answered(page, cards["imported"])
    assert modules.module(conn, "almanac").enabled
    assert len(_calls(page, "hello_sign")) == 3
