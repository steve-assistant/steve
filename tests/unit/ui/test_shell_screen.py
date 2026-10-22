"""The window's frame: one page at a time, and the rest a menu away.

Eleven tabs across the top asked a stranger to learn the app's map before
anything else: which tab held a reminder, which a yes, which the backend's
state (ui/src/Shell.tsx). Now the sidebar says what Steve is doing, the
user's name opens the back pages, each has its way back, and the window
narrows to a rail and then to one column. These drive the built window in
Edge against a backend that answers the frame's few questions.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from agent import appsettings
from agent.worker import control
from core import localapi

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

UI = Path(__file__).resolve().parents[3] / "ui"


def _job(job_id: int, why: str, status: str) -> dict:
    return {"id": job_id, "kind": "goal", "prompt": "look it up", "why": why,
            "status": status, "attempts": 1, "result": None, "error": None}


JOBS = [_job(7, "goal: Flights to Lisbon", "running"), _job(8, "goal: Tax return", "done")]


PLACES = {"ok": True, "coming": 3, "calls": 0,
          "extensions": {"installed": 2, "building": 1, "can_build": True}}
COMING = {"ok": True, "events": [],
          "reminders": [{"id": 1, "text": "Call the vet", "at": "2026-10-20T21:00:00Z"}],
          "done": [{"id": 2, "text": "Feed the cat", "at": "2026-10-19T08:00:00Z"}]}
CALLS = {"ok": True, "calls": [{"id": 5, "kind": "call", "title": "Budget review",
                                "status": "done", "at": "2026-10-19T14:00:00Z",
                                "date": "2026-10-19", "key": "listening:5"}]}


def _backend(*, said: list[dict] | None = None, places: dict | None = None):
    """The app's choices, the user's name, the tasks, and a socket that
    says `said`, a second apart, once it opens; with `places`, the sidebar's
    counts, read again at each ask, and the pages they open. Everything else
    is not this test's."""

    async def jobs(request):
        return JSONResponse({"jobs": JOBS})

    async def counts(request):
        if places is None:
            return JSONResponse({"detail": "not in this test"}, status_code=404)
        return JSONResponse(places)

    async def coming(request):
        return JSONResponse(COMING)

    async def calls(request):
        return JSONResponse(CALLS)

    async def profile(request):
        return JSONResponse({"ok": True, "profile": {"name": "Maya Lindqvist"}})

    async def socket(ws):
        await ws.accept(subprotocol=localapi.subprotocol(ws.scope))
        for event in said or []:
            await asyncio.sleep(1)
            await ws.send_json(event)
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass

    async def anything(request):
        return JSONResponse({"detail": "not in this test"}, status_code=404)

    return Starlette(routes=[
        Route("/control/app", control.app_settings, methods=["GET", "POST"]),
        Route("/control/profile", profile),
        Route("/jobs", jobs),
        Route("/control/places", counts),
        Route("/control/coming", coming),
        Route("/control/calls", calls),
        WebSocketRoute("/ws", socket),
        Route("/{path:path}", anything, methods=["GET", "POST", "PUT"]),
    ])


def _until(check, within: float = 15.0, said=None):
    """Waits for `check`; on the deadline, says what `said` reads then."""
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time" + (f": {said()}" if said else ""))
        time.sleep(0.05)


def _calls(page, cmd: str) -> list[dict]:
    return [c["args"] for c in page.evaluate("window.__STEVE_SHELL__.calls") if c["cmd"] == cmd]


def _text(page, selector: str) -> str:
    return page.locator(selector).inner_text().strip()


def test_the_sidebar_says_what_steve_is_doing_and_takes_it_down_when_done(screen):
    """A reply that takes twenty seconds looked like one that never came;
    the line under the mark says what it is doing, and goes when it is done."""
    page = screen(_backend(said=[
        {"type": "doing", "text": "Checking your calendar", "of": "turn"},
        {"type": "doing", "text": None, "of": "turn"},
    ]))
    page.locator("nav.sidebar").wait_for(timeout=15_000)
    _until(lambda: _text(page, ".presence-line") == "Checking your calendar")
    _until(lambda: _text(page, ".presence-line") == "Idle")
    assert _text(page, ".who-name") == "Maya Lindqvist"


def test_ongoing_lists_the_tasks_running_and_opens_today_where_they_are(screen):
    """Background work a stranger can't see looks like none; a task done is
    in the record, not here. A task's entry is on today's page, with what it
    is doing: the Tasks tab it opened before is gone."""
    page = screen(_backend())
    ongoing = page.locator(".ongoing button")
    ongoing.first.wait_for(timeout=15_000)
    assert [b.inner_text() for b in ongoing.all()] == ["◇Flights to Lisbon"]
    page.locator(".who").click()
    page.get_by_role("menuitem", name="Spend").click()
    ongoing.first.click()
    assert page.locator(".back-page").count() == 0
    assert page.locator(".day[aria-current]").inner_text() == "Today"


def test_the_users_name_opens_the_back_pages_and_each_has_its_way_back(screen):
    """A back page opens in place of Today, under its group's name, and
    "← Today", Esc and Ctrl+T each return; a frame with no way back
    strands a stranger on the first page they open."""
    page = screen(_backend())
    page.locator("nav.sidebar").wait_for(timeout=15_000)
    assert page.locator(".day[aria-current]").inner_text() == "Today"

    page.locator(".who").click()
    menu = page.locator("[role=menu]")
    assert [g.get_attribute("aria-label") for g in menu.locator("[role=group]").all()] == [
        "About you", "Connections", "Steve"], "the eleven tabs' last group, For now, is gone"
    menu.get_by_role("menuitem", name="Spend").click()
    assert menu.count() == 0, "the menu closes on a choice"
    assert (_text(page, ".back-group"), _text(page, ".back-name")) == ("Steve", "Spend")
    assert page.locator(".day[aria-current]").count() == 0

    page.keyboard.press("Escape")
    assert page.locator(".back-page").count() == 0
    assert page.locator(".day[aria-current]").count() == 1

    for leave in (lambda: page.keyboard.press("Control+t"),
                  lambda: page.get_by_role("button", name="← Today").click()):
        page.locator(".who").click()
        page.get_by_role("menuitem", name="Accounts").click()
        assert _text(page, ".back-group") == "Connections"
        leave()
        assert page.locator(".back-page").count() == 0

    page.locator(".who").click()
    page.keyboard.press("Escape")
    assert page.locator("[role=menu]").count() == 0, "Esc closes the menu first"
    page.locator(".who").click()
    page.get_by_role("menuitem", name="Quit Steve").click()
    _until(lambda: _calls(page, "quit") == [{}])


def _places(page) -> list[list]:
    return page.evaluate("""[...document.querySelectorAll('.places .place')].map((b) => [
        b.firstChild.textContent, b.querySelector('.day-count')?.textContent ?? null,
        b.querySelector('.place-note')?.textContent ?? null])""")


def test_the_sidebar_names_coming_up_extensions_and_call_notes_with_what_each_holds(screen):
    """Reminders, call notes and the tools Steve builds itself were behind
    the menu or a question, and nobody found them. The sidebar names each
    under the days with what it holds, Call notes once there are any, and
    each opens its page."""
    places = dict(PLACES)
    page = screen(_backend(places=places))
    _until(lambda: _places(page) == [["Coming up", "3", None], ["Extensions", "2", "building"]])

    places["calls"] = 1
    _until(lambda: _places(page)[-1:] == [["Call notes", "1", None]])

    page.locator(".places .place", has_text="Coming up").click()
    assert (_text(page, ".back-group"), _text(page, ".back-name")) == ("Your days", "Coming up")
    assert page.locator(".place[aria-current]").inner_text().startswith("Coming up")
    page.get_by_text("Call the vet", exact=True).wait_for(timeout=15_000)
    # Today's done come after what is to go, struck through by hand across
    # the whole of their words.
    rows = page.locator(".places-page .form-list li")
    assert rows.last.get_attribute("class").split() == ["place-row", "place-done"]
    struck = rows.last.locator("s.place-struck")
    assert struck.inner_text() == "Feed the cat"
    # The words' own box: the element's stretched to its column, and the
    # strike with it, across the empty row.
    words = struck.evaluate("s => { const r = document.createRange();"
                            " r.selectNodeContents(s.firstChild);"
                            " const b = r.getBoundingClientRect();"
                            " return {x: b.x, y: b.y, width: b.width, height: b.height}; }")
    line = struck.locator(".hand-mark[data-mark=strike]").bounding_box()
    assert line["x"] <= words["x"] and line["x"] + line["width"] >= words["x"] + words["width"]
    assert line["width"] <= words["width"] + 8, "struck through the words, not the row"
    assert abs(line["y"] + line["height"] / 2 - (words["y"] + words["height"] / 2)) <= 2

    page.locator(".places .place", has_text="Call notes").click()
    page.get_by_role("button", name=re.compile("Budget review")).click()
    _until(lambda: page.locator(".back-page").count() == 0)
    assert page.locator(".day[aria-current]").inner_text() == "Today", "the call's day, at its note"


def test_the_sidebar_folds_to_a_rail_and_then_opens_over_the_page(screen):
    """Snapped to half a small screen, the window still shows the page:
    the sidebar folds to a 64px rail under 760px, and under 520px it opens
    over the page from the mark."""
    page = screen(_backend())
    page.locator("nav.sidebar").wait_for(timeout=15_000)
    sidebar = page.locator("nav.sidebar")

    page.set_viewport_size({"width": 1100, "height": 760})
    assert sidebar.bounding_box()["width"] == 240
    assert page.locator(".presence-words").is_visible()

    page.set_viewport_size({"width": 700, "height": 760})
    assert sidebar.bounding_box()["width"] == 64
    assert not page.locator(".presence-words").is_visible()
    assert page.locator(".day").is_visible() and page.locator(".who").is_visible()

    page.set_viewport_size({"width": 440, "height": 600})
    assert not sidebar.is_visible()
    page.locator(".sidebar-open").click()
    assert sidebar.is_visible() and sidebar.bounding_box()["width"] == 240
    assert page.locator(".presence-words").is_visible()
    page.keyboard.press("Escape")
    assert not sidebar.is_visible()


def test_the_title_bar_takes_the_colour_of_the_theme_shown(screen):
    """Left alone, Windows paints the title bar its own white or black over
    the cream page. It follows Windows' theme, and the one chosen in
    Settings over it."""
    page = screen(_backend())

    # One CI run (22 October 2026) waited 15 s for "ink" and said no more.
    def said() -> str:
        dark = page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches")
        chosen = page.evaluate("document.documentElement.dataset.theme")
        return f"set_caption {_calls(page, 'set_caption')}, dark media {dark}, chosen {chosen!r}"

    page.emulate_media(color_scheme="dark")
    page.locator("nav.sidebar").wait_for(timeout=15_000)
    _until(lambda: _calls(page, "set_caption")[-1:] == [{"theme": "ink"}], said=said)
    page.emulate_media(color_scheme="light")
    _until(lambda: _calls(page, "set_caption")[-1:] == [{"theme": "paper"}], said=said)

    appsettings.write({"theme": "ink"})
    page.reload()
    _until(lambda: _calls(page, "set_caption")[-1:] == [{"theme": "ink"}], said=said)
    assert page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches") is False


def test_the_title_bars_colours_are_the_themes_desk_and_ink():
    """The Rust side can't read tokens.css, so the colours are written
    twice; this keeps them the same."""
    rust = (UI / "src-tauri" / "src" / "caption.rs").read_text(encoding="utf-8")
    tokens = (UI / "src" / "tokens.css").read_text(encoding="utf-8")

    def token(name: str) -> tuple[str, str]:
        found = re.search(rf"--{name}: light-dark\((#\w{{6}}), (#\w{{6}})\)", tokens)
        assert found, name
        return found[1].lower(), found[2].lower()

    def theme(name: str) -> tuple[str, str]:
        found = re.search(rf'"{name}" => Some\(\(rgb\((.+?)\), rgb\((.+?)\)\)\)', rust)
        assert found, name
        return tuple("#" + "".join(f"{int(x, 16):02x}" for x in part.split(","))
                     for part in (found[1], found[2]))

    (desk_paper, desk_ink), (ink_paper, ink_ink) = token("desk"), token("ink")
    assert theme("paper") == (desk_paper, ink_paper)
    assert theme("ink") == (desk_ink, ink_ink)
