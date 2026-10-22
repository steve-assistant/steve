"""The desk card: what it says, when it opens on its own, and what a click, a hold and a drag do.

Steve had no presence on the desktop but the tray's icon: what it was
doing, what was next and what needed the user meant opening the app
(ui/src/DeskCard.tsx, presence.md). These drive the card's own page, built
from this checkout, in Edge, against a backend that serves notes, what's
next, a day and setup's state, with the shell's commands faked
(rig.SHELL). The window itself (its place, and its hiding for a
full-screen app or the lock) is the shell's, tested in
ui/src-tauri/src/deskcard.rs.
"""

from __future__ import annotations

import asyncio
import queue
import time
from datetime import UTC, datetime, timedelta

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from agent import appsettings
from agent.worker import control
from core import localapi
from tests.unit.ui.test_quickbar_screen import CARD, LAST, REMINDER, _local

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]


class Backend:
    """Notes, what's next, a day and setup's state; and a queue of what the
    socket is to say."""

    def __init__(self, needs: list[dict], *, next_in: timedelta = timedelta(hours=1),
                 set_up: bool = True):
        self.needs = list(needs)
        self.start = (datetime.now(UTC) + next_in).isoformat(timespec="seconds")
        self.set_up = set_up
        self.say: queue.Queue[dict] = queue.Queue()
        self.looks = 0

    def app(self) -> Starlette:
        async def get_needs(request: Request):
            self.looks += 1
            return JSONResponse({"ok": True, "needs": self.needs})

        async def get_next(request: Request):
            return JSONResponse({"ok": True, "reminder": None, "event": {
                "summary": "Design review", "start": self.start, "end": self.start,
                "location": "Room 4"}})

        async def get_day(request: Request):
            today = datetime.now(UTC).date().isoformat()
            return JSONResponse({
                "ok": True, "date": today, "today": today, "turns_at": "04:00",
                "as_of": datetime.now(UTC).isoformat(), "looked": None, "entries": [
                    {"key": "reply:2", "kind": "reply", "at": "2026-10-15T13:00:20Z",
                     "changed": None, "channel": None, "new": False, "id": 2, "session_id": 1,
                     "text": f"{LAST}\n\nI told them by mail.", "reaction": None},
                    {"key": "said:3", "kind": "said", "at": "2026-10-15T13:05:00Z",
                     "changed": None, "channel": None, "new": False, "id": 3, "session_id": 1,
                     "text": "And tell Theo"},
                ]})

        async def setup_state(request: Request):
            return JSONResponse({"next": None if self.set_up else "openai", "done": self.set_up})

        async def socket(ws):
            await ws.accept(subprotocol=localapi.subprotocol(ws.scope))
            while True:
                while not self.say.empty():
                    await ws.send_json(self.say.get())
                try:
                    message = await asyncio.wait_for(ws.receive(), 0.1)
                except TimeoutError:
                    continue
                if message["type"] == "websocket.disconnect":
                    return

        async def anything(request: Request):
            return JSONResponse({"detail": "not in this test"}, status_code=404)

        return Starlette(routes=[
            Route("/control/needs", get_needs),
            Route("/control/next", get_next),
            Route("/control/day", get_day),
            Route("/control/app", control.app_settings, methods=["GET", "POST"]),
            Route("/setup/state", setup_state),
            WebSocketRoute("/ws", socket),
            Route("/{path:path}", anything, methods=["GET", "POST", "PUT"]),
        ])


def _until(check, within: float = 15.0):
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time")
        time.sleep(0.05)


def _calls(page, cmd: str) -> list[dict]:
    """The arguments of each time the page asked the shell for `cmd`."""
    return [c["args"] for c in page.evaluate("window.__STEVE_SHELL__.calls") if c["cmd"] == cmd]


def _emit(page, event: str, payload=None) -> None:
    page.evaluate("([e, p]) => window.__STEVE_SHELL__.emit(e, p)", [event, payload])


def _open(screen, backend: Backend):
    page = screen(backend.app(), label="deskcard", path="/#deskcard")
    page.locator(".desk-card").wait_for(timeout=15_000)
    # Its first look is done.
    _until(lambda: backend.looks and _calls(page, "deskcard_want"))
    _until(lambda: page.evaluate("""() => window.__STEVE_SHELL__.calls.some(
        (c) => c.cmd === "plugin:event|listen" && c.args.event === "deskcard-reply")"""))
    return page


def _is_open(page) -> bool:
    return "open" in (page.locator(".desk-card").get_attribute("class") or "")


def _away(page) -> None:
    page.mouse.move(2, 2)


def test_the_card_is_wanted_only_once_setup_is_done_and_while_its_setting_is_on(screen):
    """On a fresh install it would sit on the desktop with nothing to say,
    beside a window asking for a key. Turned off in Settings, it goes at
    once, not at its next look; kept off the top, the shell is told."""
    backend = Backend([], set_up=False)
    page = _open(screen, backend)
    assert {(a["on"], a["onTop"]) for a in _calls(page, "deskcard_want")} == {(False, True)}

    backend.set_up = True
    _emit(page, "app-settings")
    _until(lambda: _calls(page, "deskcard_want")[-1] == {"on": True, "onTop": True})

    appsettings.write({"desk_card": "off"})
    _emit(page, "app-settings")
    _until(lambda: _calls(page, "deskcard_want")[-1] == {"on": False, "onTop": True})

    appsettings.write({"desk_card": "on", "desk_card_on_top": "off"})
    _emit(page, "app-settings")
    _until(lambda: _calls(page, "deskcard_want")[-1] == {"on": True, "onTop": False})


def test_pointed_at_the_tile_opens_into_the_card_with_whats_doing_next_and_the_notes(screen):
    """One line each: what Steve is doing, whichever window asked; the next
    event; how many notes wait; and, for the pointer on it, Steve's last
    entry. The note's edge peeks out while notes wait. Away, it is the tile
    again."""
    backend = Backend([CARD, REMINDER])
    page = _open(screen, backend)
    assert page.locator(".desk-peek").count() == 1
    assert not _is_open(page) and page.locator(".desk-lines").count() == 0
    backend.say.put({"type": "doing", "text": "Checking your calendar", "of": "turn:3"})

    page.locator(".desk-tile").hover()
    _until(lambda: page.locator(".desk-now").count() == 1 and page.locator(".desk-last").count())
    assert page.locator(".desk-now").inner_text() == "Checking your calendar"
    assert page.locator(".desk-next").inner_text() == (
        f"Next · Design review, {_local(page, backend.start)}")
    assert page.locator(".desk-notes").inner_text() == "◆ 2 need you"
    assert page.locator(".desk-last").inner_text().startswith(LAST), "Steve's, not the user's"
    size = _calls(page, "deskcard_size")[-1]
    assert size["width"] == 320 + 32, "the card's 320, with the window's edge"

    backend.say.put({"type": "doing", "text": None, "of": "turn:3"})
    _until(lambda: page.locator(".desk-now").count() == 0)
    _away(page)
    _until(lambda: not _is_open(page))
    _until(lambda: _calls(page, "deskcard_size")[-1] == {"width": 88, "height": 88})


def test_in_the_tiles_small_window_the_card_asks_for_its_whole_size_at_once(screen):
    """The window holds the tile alone until the card opens. Shrunk to fit
    that window, the card asked for a little more room at each step, and
    each step moved the window: in the screen's bottom-right corner, away
    from the pointer, which closed the card, over and over."""
    backend = Backend([CARD])
    page = _open(screen, backend)
    page.set_viewport_size({"width": 88, "height": 88})
    # The tile's own size, asked once it is drawn. Under load that ask can
    # come after the hover begins, so the card's growth is read past it:
    # counting from the hover, it was taken for the card's first ask.
    _until(lambda: _calls(page, "deskcard_size"))
    closed = _calls(page, "deskcard_size")[0]["width"]
    before = len(_calls(page, "deskcard_size"))

    def grown() -> list[int]:
        return [c["width"] for c in _calls(page, "deskcard_size")[before:] if c["width"] != closed]

    page.locator(".desk-tile").hover()
    _until(lambda: _is_open(page) and grown())
    assert grown()[0] == 320 + 32, "the first size asked for is the whole card's"


def test_the_open_card_and_its_tile_are_one_colour_in_either_theme(screen):
    """The tile is the paper's colour and the card a note's. Open, the tile
    is part of the card; in its own colour it read as a patch on it, most
    of all in the dark theme."""
    backend = Backend([CARD])
    page = _open(screen, backend)
    page.locator(".desk-tile").hover()
    _until(lambda: _is_open(page))
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        tile, card = page.evaluate("""() => [".desk-tile", ".desk-card"].map(
            (s) => getComputedStyle(document.querySelector(s)).backgroundColor)""")
        assert tile == card, f"{scheme}: the tile {tile} on the card {card}"


def test_the_pointer_leaving_for_a_moment_does_not_close_the_card(screen):
    """While the window moves and grows to open the card, the page can hear
    the pointer leave for a frame. Closed at once, the card shrank the
    window back under the pointer, and opened again: the card flickered as
    long as the pointer stayed. Gone for longer, it closes."""
    backend = Backend([CARD])
    page = _open(screen, backend)
    page.locator(".desk-tile").hover()
    _until(lambda: _is_open(page))
    page.evaluate("""() => {
        const card = document.querySelector(".desk-card");
        window.__closed = 0;
        new MutationObserver(() => {
            if (!card.classList.contains("open")) window.__closed += 1;
        }).observe(card, { attributes: true, attributeFilter: ["class"] });
    }""")
    # Back by a raw move, not `hover()`: that waits for the tile to hold
    # still over several frames, which under a loaded suite outlasted the
    # card's 200 ms grace and failed the test on a working card.
    box = page.locator(".desk-tile").bounding_box()
    tile = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    _away(page)
    page.mouse.move(*tile)
    time.sleep(0.5)
    assert page.evaluate("window.__closed") == 0, "a moment away kept it open"

    _away(page)
    _until(lambda: not _is_open(page))


def test_while_steve_speaks_the_cards_mark_says_so_and_follows_the_voice(screen):
    """Spoken to from across the room, the card is what the user sees: its
    mark speaks while the voice does, and settles after."""
    backend = Backend([])
    page = _open(screen, backend)
    mark = page.locator(".desk-tile svg.mark")

    backend.say.put({"type": "level", "of": "speaking", "level": 0.6})
    _until(lambda: mark.get_attribute("data-state") == "speaking")
    _until(lambda: mark.evaluate("e => e.style.getPropertyValue('--level')") == "0.6")

    backend.say.put({"type": "level", "of": None, "level": 0})
    _until(lambda: mark.get_attribute("data-state") == "idle")


def test_with_its_words_off_the_card_shows_only_the_next_time_and_the_count(screen):
    """Anyone passing the screen can read the card. Off, it says the time
    of the next event without its name or place, and how many notes wait:
    not what Steve is doing, nor its last entry, nor a reply."""
    appsettings.write({"desk_card_text": "off"})
    backend = Backend([CARD])
    page = _open(screen, backend)
    backend.say.put({"type": "doing", "text": "Reading your mail from Dana", "of": "job:4"})
    page.locator(".desk-tile").hover()
    _until(lambda: page.locator(".desk-next").count() == 1)
    page.wait_for_timeout(500)  # the doing line and the day would have come by now
    assert page.locator(".desk-next").inner_text() == f"Next · {_local(page, backend.start)}"
    assert page.locator(".desk-notes").inner_text() == "◆ 1 needs you"
    assert page.locator(".desk-now").count() == 0
    assert page.locator(".desk-last").count() == 0
    text = page.locator(".desk-card").inner_text()
    assert "Design review" not in text and "Room 4" not in text and "Dana" not in text

    _away(page)
    _emit(page, "deskcard-reply", "Booked for 19:30 at Rosa's.")
    _until(lambda: _is_open(page))
    assert "Rosa" not in page.locator(".desk-card").inner_text()


def test_a_new_note_opens_the_card_for_eight_seconds_or_while_pointed_at(screen):
    """It opens on its own for a new note, not for the notes already there
    when it started, and closes again after eight seconds unless the
    pointer is on it: a card that stayed open would be one more window."""
    backend = Backend([CARD])
    page = _open(screen, backend)
    page.wait_for_timeout(300)
    assert not _is_open(page), "a note already there is no news"

    page.clock.install()
    backend.needs = [CARD, REMINDER]
    backend.say.put({"type": "inbox", "message": {"id": 1, "kind": "reminder", "text": "x"}})
    _until(lambda: _is_open(page))
    assert page.locator(".desk-notes").inner_text() == "◆ 2 need you"
    page.clock.run_for(7000)
    assert _is_open(page)
    page.clock.run_for(1500)
    _until(lambda: not _is_open(page))

    backend.needs = [CARD, REMINDER, {**REMINDER, "id": "reminder:41"}]
    backend.say.put({"type": "inbox", "message": {"id": 2, "kind": "reminder", "text": "y"}})
    _until(lambda: _is_open(page))
    page.locator(".desk-tile").hover()
    page.clock.run_for(9000)
    assert _is_open(page), "the pointer is on it"
    _away(page)
    _until(lambda: not _is_open(page))


def test_an_event_within_ten_minutes_opens_the_card_once_saying_how_soon_and_where(screen):
    """"Design review in 8 min · Room 4", in time to go. Once for that
    event: its next look, thirty seconds on, doesn't open it again."""
    backend = Backend([], next_in=timedelta(minutes=8, seconds=20))
    page = screen(backend.app(), label="deskcard", path="/#deskcard",
                  before=lambda p: p.clock.install())
    _until(lambda: page.locator(".desk-next").count() == 1)
    assert page.locator(".desk-next").inner_text() == "Design review in 8 min · Room 4"
    assert page.locator(".desk-notes").inner_text() == "Nothing needs you"

    page.clock.run_for(8500)
    _until(lambda: not _is_open(page))
    looked = backend.looks
    page.clock.run_for(31_000)
    _until(lambda: backend.looks > looked)
    page.wait_for_timeout(300)
    assert not _is_open(page)


def test_a_reply_that_reached_a_closed_quick_bar_opens_the_card_with_its_first_line(screen):
    """The user asked in the quick bar and closed it; the answer shows on
    the card, not in a window put in front of their work."""
    page = _open(screen, Backend([]))
    _emit(page, "deskcard-reply", "Booked for 19:30 at Rosa's.")
    _until(lambda: page.locator(".desk-last").count() == 1)
    assert page.locator(".desk-last").inner_text() == "Booked for 19:30 at Rosa's."


def test_a_click_opens_today_the_notes_line_opens_the_quick_bar_and_a_hold_talks(screen):
    """The card's three presses: a click for today in the app, the notes'
    line for the quick bar where a note is answered, and a hold for
    push-to-talk, ended by the release and taken for no click."""
    page = _open(screen, Backend([CARD]))
    tile = page.locator(".desk-tile")
    tile.click()
    _until(lambda: len(_calls(page, "deskcard_open")) == 1)

    tile.hover()
    page.locator(".desk-notes").click()
    _until(lambda: len(_calls(page, "deskcard_quickbar")) == 1)
    assert len(_calls(page, "deskcard_open")) == 1

    box = tile.bounding_box()
    page.mouse.move(box["x"] + 28, box["y"] + 28)
    page.mouse.down()
    _until(lambda: _calls(page, "deskcard_talk") == [{"on": True}])
    _until(lambda: page.locator(".desk-now").count() == 1)
    assert page.locator(".desk-now").inner_text() == "Listening"
    page.mouse.up()
    _until(lambda: _calls(page, "deskcard_talk") == [{"on": True}, {"on": False}])
    page.wait_for_timeout(300)
    assert len(_calls(page, "deskcard_open")) == 1, "a hold is not a click"


def test_with_talk_off_a_hold_of_the_card_is_taken_as_a_click_and_hears_nothing(screen):
    """Talk, at the sidebar's foot, turns hold-to-talk off everywhere. Left
    on the card, a hold would open the microphone for someone who turned it
    off; it is a click instead."""
    appsettings.write({"talk": "off"})
    page = _open(screen, Backend([CARD]))
    box = page.locator(".desk-tile").bounding_box()
    page.mouse.move(box["x"] + 28, box["y"] + 28)
    page.mouse.down()
    page.wait_for_timeout(600)
    assert not _calls(page, "deskcard_talk")
    assert "Listening" not in page.locator(".desk-card").inner_text()
    page.mouse.up()
    _until(lambda: len(_calls(page, "deskcard_open")) == 1)
    assert not _calls(page, "deskcard_talk")


def test_a_drag_moves_the_card_and_its_release_is_kept_not_taken_for_a_click(screen):
    """A drag starts, follows the pointer, and ends where the shell snaps
    the card and keeps its place; it opens nothing and says nothing."""
    page = _open(screen, Backend([]))
    box = page.locator(".desk-tile").bounding_box()
    x, y = box["x"] + 28, box["y"] + 28
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 40, y - 30, steps=6)
    page.mouse.up()
    _until(lambda: [a["phase"] for a in _calls(page, "deskcard_drag")][-1:] == ["end"])
    phases = [a["phase"] for a in _calls(page, "deskcard_drag")]
    assert phases[0] == "start" and "move" in phases and phases.count("end") == 1
    page.wait_for_timeout(400)
    assert not _calls(page, "deskcard_open") and not _calls(page, "deskcard_talk")


def test_the_card_opens_away_from_the_corner_it_sits_in(screen):
    """In the screen's bottom-right corner its tile is at the card's right
    and bottom, so the tile stays put as the card opens up and left; in the
    top-left, at its left and top. The card sits in the window's same
    corner: the shell keeps that corner still as it resizes the window, and
    a card elsewhere in it would jump while the two sizes differ."""
    page = _open(screen, Backend([CARD]))
    view = page.viewport_size

    def edges(selector: str) -> tuple[float, float, float, float]:
        b = page.locator(selector).bounding_box()
        return b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]

    for corner in ("top-left", "bottom-right"):
        # The shell's answer to the next size; in a page the size of a
        # screen, unlike the card's window, the card then moves from under
        # the pointer, so it is pointed at again where it now is. A pointer
        # back within the moment it may be away keeps the card open, and
        # asks for no new size: it is let close first.
        page.evaluate("c => { window.__STEVE_SHELL__.answers.deskcard_size = c; }", corner)
        _away(page)
        _until(lambda: not _is_open(page))
        page.locator(".desk-tile").hover()
        _until(lambda c=corner: page.locator(".desk").get_attribute("data-corner") == c)
        _away(page)
        _until(lambda: not _is_open(page))
        page.locator(".desk-tile").hover()
        _until(lambda: _is_open(page) and page.locator(".desk-notes").count() == 1)
        card, tile = edges(".desk-card"), edges(".desk-tile")
        lines = edges(".desk-lines")
        if corner == "top-left":
            assert (tile[0], tile[1]) == (card[0], card[1]) and lines[0] >= tile[2]
            assert (card[0], card[1]) == (16, 16), "the window's top-left, past its edge"
        else:
            assert (tile[2], tile[3]) == (card[2], card[3]) and lines[2] <= tile[0]
            assert (card[2], card[3]) == (view["width"] - 16, view["height"] - 16), (
                "the window's bottom-right, past its edge")


def test_right_click_asks_for_the_menu_and_hide_lasts_an_hour_or_until_the_page_turns(screen):
    """Windows draws the menu; its Hide items come back to the page, which
    knows the day turns at 4 a.m.: hidden at half past ten at night until
    tomorrow is until 4 the next morning, and at half past one, until 4."""
    page = _open(screen, Backend([]))
    page.locator(".desk-tile").click(button="right")
    _until(lambda: len(_calls(page, "deskcard_menu")) == 1)
    assert not _calls(page, "deskcard_open")

    for now, kind, until in [
            ((2026, 10, 18, 22, 30), "hour", (2026, 10, 18, 23, 30)),
            ((2026, 10, 18, 22, 30), "tomorrow", (2026, 10, 19, 4, 0)),
            ((2026, 10, 19, 1, 30), "tomorrow", (2026, 10, 19, 4, 0))]:
        page.clock.set_fixed_time(datetime(*now))
        asked = len(_calls(page, "deskcard_hide"))
        _emit(page, "deskcard-hide", kind)
        _until(lambda n=asked: len(_calls(page, "deskcard_hide")) == n + 1)
        expected = page.evaluate("([y, mo, d, h, mi]) => new Date(y, mo - 1, d, h, mi).getTime()",
                                 list(until))
        assert _calls(page, "deskcard_hide")[-1] == {"until": expected}, (now, kind)


def test_the_trays_show_the_desk_card_turns_on_a_card_turned_off_in_settings(screen):
    """"Show the desk card" on the tray is how a user who turned it off gets
    it back: the setting goes on, the shell is told it is wanted, and the
    other windows are told the settings changed, so Settings says it."""
    appsettings.write({"desk_card": "off"})
    page = _open(screen, Backend([]))
    _until(lambda: _calls(page, "deskcard_want")[-1] == {"on": False, "onTop": True})
    _emit(page, "deskcard-show")
    _until(lambda: appsettings.read().get("desk_card") == "on")
    _until(lambda: _calls(page, "deskcard_want")[-1] == {"on": True, "onTop": True})
    told = {"event": "app-settings", "payload": "deskcard"}
    _until(lambda: told in [{k: c.get(k) for k in told} for c in _calls(page, "plugin:event|emit")])
