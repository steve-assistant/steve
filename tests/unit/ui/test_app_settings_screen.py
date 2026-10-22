"""The window's choices live with the backend, and what its storage held moves there once.

The hotkey and the update checks were kept in the window's localStorage,
inside WebView2's folder, where a reinstall lost them and no test could read
them (ui/src/appSettings.ts, agent/appsettings.py). These drive the shipped
window in Edge against the worker's own route for them.
"""

from __future__ import annotations

import time

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from agent import appsettings
from agent.worker import control
from core import localapi

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

STORED = ("steve.hotkey", "steve.quickbar-hotkey", "steve.updates.check")


def _backend(*, starting: bool = False, add_on: bool | None = None):
    """The worker's route for the app's choices, a socket that opens, and
    nothing else: the window's other requests are not this test's. While
    `starting`, the route answers as a worker not yet ready. With `add_on`,
    the voice's status says whether the add-on call notes need is there."""
    asked: list[str] = []

    async def voice(request):
        asked.append("voice")
        tap = {"on": False, "ok": False, "error": None, "add_on": add_on}
        return JSONResponse({"warm": {"state": "cold"}, "listening": False, "speaking": False,
                             "hands_free": {"tap": tap, "note": None}})

    async def app_settings(request):
        asked.append(request.method)
        if starting:
            return JSONResponse({"detail": "starting"}, status_code=503)
        return await control.app_settings(request)

    async def socket(ws):
        await ws.accept(subprotocol=localapi.subprotocol(ws.scope))
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass

    async def anything(request):
        return JSONResponse({"detail": "not in this test"}, status_code=404)

    return Starlette(routes=[
        Route("/control/app", app_settings, methods=["GET", "POST"]),
        *([Route("/voice/status", voice)] if add_on is not None else []),
        WebSocketRoute("/ws", socket),
        Route("/{path:path}", anything, methods=["GET", "POST", "PUT"]),
    ]), asked


def _until(check, within: float = 15.0):
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time")
        time.sleep(0.05)


def _bound(page) -> list[str]:
    calls = page.evaluate("window.__STEVE_SHELL__.calls")
    return [key for call in calls if call["cmd"] == "plugin:global-shortcut|register"
            for key in call["args"]["shortcuts"]]


def test_what_the_window_stored_moves_to_the_backend_once_and_binds_as_chosen(screen):
    """An update to this release must keep the hotkey the user chose; and a
    value the backend refuses is dropped, or it would be sent at every start."""
    backend, asked = _backend()
    page = screen(backend)
    _until(lambda: "GET" in asked)
    page.evaluate("""() => {
      localStorage.setItem("steve.hotkey", "Alt+Insert");
      localStorage.setItem("steve.quickbar-hotkey", "press the big red button");
      localStorage.setItem("steve.updates.check", "off");
    }""")
    asked.clear()
    page.reload()

    _until(lambda: asked.count("GET") >= 1)
    assert appsettings.read() == {"hotkey": "Alt+Insert", "update_checks": "off"}
    assert page.evaluate("ks => ks.map(k => localStorage.getItem(k))", list(STORED)) == [
        None, None, None]
    _until(lambda: len(_bound(page)) >= 1)
    page.wait_for_timeout(300)
    assert _bound(page) == ["Alt+Insert"], "one hotkey, the chosen one, never the default first"

    asked.clear()
    page.reload()
    _until(lambda: "GET" in asked)
    assert asked == ["GET"], "nothing left to move"


def test_the_window_binds_no_hotkey_before_the_backend_says_which(screen):
    """Bound from the default before the choice arrived, the default would
    take the key from whatever else uses it, if only for a moment."""
    backend, asked = _backend(starting=True)
    page = screen(backend)
    _until(lambda: "GET" in asked)
    # Bound from a loaded choice, the keys follow within milliseconds.
    page.wait_for_timeout(500)
    assert _bound(page) == []


def _settings(page) -> None:
    page.locator(".who").click()
    page.get_by_role("menuitem", name="Settings", exact=True).click()
    page.get_by_role("heading", name="The hotkey").wait_for()


def test_with_none_chosen_the_hotkey_is_the_first_one_windows_lets_steve_have(screen):
    """Win+Alt+S held by another app left the user with no hotkey, and no
    word why; the next of the fallbacks is bound instead, and Settings says
    which, and why it isn't the usual one."""
    backend, asked = _backend()
    page = screen(backend)
    _until(lambda: "GET" in asked)
    page.evaluate("window.__STEVE_SHELL__.take('Super+Alt+S')")
    page.reload()

    _until(lambda: "Super+Alt+J" in _bound(page))
    page.wait_for_timeout(300)
    assert _bound(page) == ["Super+Alt+S", "Super+Alt+J"], "tried in order, and no further"
    _settings(page)
    assert page.get_by_label("The hotkey").input_value() == "Super+Alt+J"
    page.get_by_text("Win+Alt+S is held by another app, so Steve uses Win+Alt+J.").wait_for()


def test_a_tap_opens_the_quick_bar_and_a_hold_talks_in_it_until_let_go(screen):
    """One key for both (presence.md): a tap that started listening, or a
    hold that only opened the bar, would leave the user unsure which they
    did. The hold is told to the quick bar, where its words and answer show."""
    backend, asked = _backend()
    page = screen(backend)
    _until(lambda: "Super+Alt+S" in _bound(page))

    def asked_for():
        calls = page.evaluate("window.__STEVE_SHELL__.calls")
        return [(c["cmd"], c["args"].get("on")) for c in calls
                if c["cmd"] in ("show_quickbar", "quickbar_talk")]

    page.evaluate("window.__STEVE_SHELL__.hotkey('Super+Alt+S', 'Pressed')")
    page.evaluate("window.__STEVE_SHELL__.hotkey('Super+Alt+S', 'Released')")
    _until(lambda: asked_for() == [("show_quickbar", None)])

    page.evaluate("window.__STEVE_SHELL__.hotkey('Super+Alt+S', 'Pressed')")
    page.wait_for_timeout(400)
    assert asked_for()[1:] == [("quickbar_talk", True)], "held past the tap's 250 ms"
    page.evaluate("window.__STEVE_SHELL__.hotkey('Super+Alt+S', 'Released')")
    _until(lambda: asked_for()[1:] == [("quickbar_talk", True), ("quickbar_talk", False)])


def test_talk_turned_off_at_the_sidebars_foot_makes_a_hold_a_tap_and_takes_the_microphone_away(
        screen):
    """Hold-to-talk could be turned off only in no place at all: a hold
    opened the microphone, and the composer always had a button for it.
    Talk, at the sidebar's foot, turns it off everywhere: a hold of the
    hotkey opens the quick bar as a tap does, and the microphone button goes."""
    backend, asked = _backend()
    page = screen(backend)
    _until(lambda: "Super+Alt+S" in _bound(page))
    page.locator(".composer .voice").wait_for()
    talk = page.locator(".foot-switches .form-check", has_text="Talk")

    def asked_for():
        calls = page.evaluate("window.__STEVE_SHELL__.calls")
        return [(c["cmd"], c["args"].get("on")) for c in calls
                if c["cmd"] in ("show_quickbar", "quickbar_talk")]

    talk.click()
    _until(lambda: appsettings.read().get("talk") == "off")
    page.locator(".composer .voice").wait_for(state="detached")
    page.evaluate("window.__STEVE_SHELL__.hotkey('Super+Alt+S', 'Pressed')")
    page.wait_for_timeout(400)
    assert asked_for() == [], "held past the tap's 250 ms, and nothing heard"
    page.evaluate("window.__STEVE_SHELL__.hotkey('Super+Alt+S', 'Released')")
    _until(lambda: asked_for() == [("show_quickbar", None)])

    talk.click()
    _until(lambda: appsettings.read().get("talk", "on") == "on")
    page.locator(".composer .voice").wait_for()


@pytest.mark.parametrize("add_on", [False, True])
def test_call_notes_sit_at_the_sidebars_foot_with_the_voice_add_on_and_without_it_settings_says_so(
        screen, add_on):
    """Without the voice add-on, which v0.1 doesn't ship, Settings offered
    the call-notes switch and pressing it brought only a refusal. Installed,
    the switch sits at the sidebar's foot beside Talk; not, Settings says
    what it needs and offers nothing to press."""
    backend, asked = _backend(add_on=add_on)
    page = screen(backend)
    _until(lambda: "voice" in asked)
    foot = page.locator(".foot-switches")
    foot.get_by_text("Talk").wait_for()
    _settings(page)
    knob = page.locator('[data-setting="tap"]')
    if add_on:
        foot.get_by_text("Call notes").wait_for()
        assert knob.locator("input").is_enabled()
        assert "Needs the voice add-on" not in knob.inner_text()
    else:
        _until(lambda: "Needs the voice add-on" in knob.inner_text())
        assert knob.locator("input").is_disabled()
        assert foot.get_by_text("Call notes").count() == 0


def test_a_hotkey_chosen_in_settings_takes_the_place_of_the_one_bound(screen):
    """Changed in Settings, the old key must be let go: kept, it would still
    open the bar, and the user could not give it back to the app that wants it."""
    backend, asked = _backend()
    page = screen(backend)
    _until(lambda: "Super+Alt+S" in _bound(page))
    _settings(page)
    # Each binding first unregisters its own key: only what follows the
    # change says whether the old one was let go.
    before = page.evaluate("window.__STEVE_SHELL__.calls.length")
    page.get_by_label("The hotkey").select_option("Alt+Insert")

    _until(lambda: "Alt+Insert" in _bound(page))
    assert appsettings.read()["hotkey"] == "Alt+Insert"
    calls = page.evaluate("window.__STEVE_SHELL__.calls")[before:]
    let_go = [key for c in calls if c["cmd"] == "plugin:global-shortcut|unregister"
              for key in c["args"]["shortcuts"]]
    assert "Super+Alt+S" in let_go
