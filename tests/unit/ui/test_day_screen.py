"""The day page: everything that happened today, in order, and the composer.

Home was a dashboard and Chat a transcript held in the window's memory: a
reply on the phone, a briefing or a task finished overnight never reached
either, and a restart emptied Chat (ui/src/Day.tsx, journal.ts). Now the
page is drawn from the backend's day. These drive the built window in Edge
against a backend that serves a day of every kind of entry, and keeps what
the page sends it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import re
import time
from datetime import UTC, datetime, timedelta

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute

from agent import appsettings
from agent.worker import control
from core import localapi

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

TODAY = "2026-10-15"


# A photo of one pixel, enough for an <img> to load.
TINY_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAC"
                            "hwGA60e6kgAAAABJRU5ErkJggg==")


def _entry(kind: str, key: str, at: str, **more) -> dict:
    return {"key": key, "kind": kind, "at": at, "changed": at, "channel": None, "new": False,
            **more}


def _local(page, iso: str) -> str:
    """A moment as the page shows it: the clock time in the window's zone
    and its locale's form ("14:05" or "02:05 PM"), until Settings has a
    region of its own (step 9, item 13)."""
    return page.evaluate(
        "iso => new Date(iso).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})", iso)


DAY = [
    _entry("asleep", "asleep:1", "2026-10-15T04:50:00Z", id=1, gap="asleep",
           **{"from": "2026-10-15T04:50:00Z", "to": "2026-10-15T12:10:00Z"}),
    _entry("briefing", "outbox:1", "2026-10-15T12:00:00Z", id=1, of="briefing", which="morning",
           text="Morning, Maya.\n\n- Dentist at 15:00\n- Rain after four",
           sent="2026-10-15T13:14:00Z", stopped=None, channel="signal"),
    _entry("said", "said:10", "2026-10-15T14:00:00Z", id=10, session_id=1, channel="tray",
           text="What's on tomorrow?"),
    _entry("reply", "reply:11", "2026-10-15T14:00:20Z", id=11, session_id=1, channel="tray",
           text="Two meetings:\n\n- **Design review** at 10:00\n- Lunch with Sam", reaction=None),
    _entry("spoken", "spoken:20", "2026-10-15T15:00:00Z", session_id=2, channel="voice",
           minutes=4, first_words="Remind me about Friday", summary=None,
           ended="2026-10-15T15:04:00Z", lines=[
               {"role": "user", "text": "Remind me about Friday", "at": "2026-10-15T15:00:00Z"},
               {"role": "assistant", "text": "I'll remind you tonight.",
                "at": "2026-10-15T15:00:30Z"}]),
    _entry("action", "action:5", "2026-10-15T16:00:00Z", id=5, status="pending", asked=True,
           by=None, what="Send the deck to Iris", line="Asked you about: Send the deck to Iris",
           error=None, decided=None, executed=None, undo_until=None),
    _entry("action", "action:6", "2026-10-15T16:30:00Z", id=6, status="failed", asked=False,
           by="auto", what="Send the notes", line="Couldn't: Send the notes",
           error="Gmail refused the sign-in. Sign in again in Accounts.", decided=None,
           executed=None, undo_until=None),
    _entry("task", "task:7", "2026-10-15T17:00:00Z", id=7, of="research",
           title="Flights to Lisbon", detail=None, status="running", finished=None),
    _entry("update", "outbox:2", "2026-10-15T18:00:00Z", id=2, of="heads_up", which=None,
           text="The 10:00 moved to 10:30.", sent=None, stopped=None),
    _entry("update", "outbox:3", "2026-10-15T18:05:00Z", id=3, of="heads_up", which=None,
           text="Your flight's gate changed to B12.", sent=None, stopped=None),
    _entry("update", "outbox:4", "2026-10-15T18:10:00Z", id=4, of="question", which=None,
           text="How did lunch with Sam go?", sent=None, stopped=None),
]


class Backend:
    """A day the test can add to, the days before it, and a socket that
    says what the test queues and keeps what the page sends."""

    def __init__(self, entries: list[dict] | None = None, *, folded: list[dict] | None = None,
                 older: dict[str, list[dict]] | None = None):
        self.entries = list(DAY if entries is None else entries)
        self.folded = folded or []
        self.older = older or {}
        self.sent: list[dict] = []
        self.posted: list[tuple[str, dict]] = []
        self.asked: list[str] = []
        self.say: queue.Queue = queue.Queue()
        self.down = False
        # What a search finds, whatever is asked; and what was asked.
        self.found: list[dict] = []
        self.searched: list[str] = []
        # Each ask for the days before, by its query.
        self.days_asked: list[str] = []
        # Photos given, in order, and each asked for, by its query; the
        # first of message 30's is kept, the rest are gone.
        self.uploads: list[bytes] = []
        self.photos_asked: list[str] = []
        # The sidebar's counts, with whether Steve can build here; None is
        # an older worker's 404.
        self.places: dict | None = None
        # The backend's today: a test moves it on to turn the page.
        self.today = TODAY
        # How long a day's page takes to come, by its date: a slow network.
        self.slow: dict[str, float] = {}

    def day(self, date: str) -> dict:
        entries = self.entries if date == TODAY else self.older.get(date, [])
        return {"ok": True, "date": date, "today": self.today, "turns_at": "04:00",
                "as_of": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "looked": None, "entries": entries}

    def app(self) -> Starlette:
        async def get_day(request: Request):
            self.asked.append(str(request.url.query))
            date = request.query_params.get("date") or TODAY
            await asyncio.sleep(self.slow.get(date, 0))
            return JSONResponse(self.day(date))

        async def get_days(request: Request):
            self.days_asked.append(str(request.url.query))
            return JSONResponse({"ok": True, "days": self.folded})

        async def keep(request: Request):
            self.posted.append((request.url.path, await request.json()))
            return JSONResponse({"ok": True})

        async def note(request: Request):
            body = await request.json()
            self.posted.append((request.url.path, body))
            if body.get("op") == "delete":
                # Gone from the database, as the backend's: no answer has it again.
                self.entries = [e for e in self.entries if e["key"] != f"listening:{body['id']}"]
            return JSONResponse({"ok": True})

        async def search(request: Request):
            q = request.query_params.get("q", "")
            self.searched.append(q)
            return JSONResponse({"ok": True, "q": q, "today": TODAY, "days": self.found})

        async def profile(request: Request):
            return JSONResponse({"ok": True, "profile": {"name": "Maya Lindqvist"}})

        async def socket(ws):
            if self.down:
                await ws.close()
                return
            await ws.accept(subprotocol=localapi.subprotocol(ws.scope))
            while not self.down:
                while not self.say.empty():
                    await ws.send_json(self.say.get())
                try:
                    message = await asyncio.wait_for(ws.receive(), 0.1)
                except TimeoutError:
                    continue
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text"):
                    self.sent.append(json.loads(message["text"]))
            await ws.close()

        async def give_photo(request: Request):
            self.uploads.append(await request.body())
            return JSONResponse({"ok": True, "name": f"p{len(self.uploads) - 1}.png"})

        async def get_photo(request: Request):
            self.photos_asked.append(str(request.url.query))
            if (request.query_params.get("message"), request.query_params.get("n")) == ("30", "0"):
                return Response(TINY_PNG, media_type="image/png")
            return JSONResponse({"ok": False, "error": "no longer kept"}, status_code=404)

        async def anything(request: Request):
            return JSONResponse({"detail": "not in this test"}, status_code=404)

        async def places(request: Request):
            if self.places is None:
                return await anything(request)
            return JSONResponse(self.places)

        return Starlette(routes=[
            Route("/control/app", control.app_settings, methods=["GET", "POST"]),
            Route("/control/profile", profile),
            Route("/control/places", places),
            Route("/control/day", get_day),
            Route("/control/days", get_days),
            Route("/control/search", search),
            Route("/control/react", keep, methods=["POST"]),
            Route("/control/looked", keep, methods=["POST"]),
            Route("/actions/decide", keep, methods=["POST"]),
            Route("/control/note", note, methods=["POST"]),
            Route("/control/photo", give_photo, methods=["POST"]),
            Route("/control/photo", get_photo, methods=["GET"]),
            WebSocketRoute("/ws", socket),
            Route("/{path:path}", anything, methods=["GET", "POST", "PUT"]),
        ])


def _until(check, within: float = 15.0):
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time")
        time.sleep(0.05)


def _open(screen, backend: Backend):
    page = screen(backend.app())
    page.locator(".day-sheet").first.wait_for(timeout=15_000)
    return page


def _texts(page, selector: str) -> list[str]:
    return [t.strip() for t in page.locator(selector).all_inner_texts()]


def test_every_kind_of_entry_is_drawn_on_the_day_in_its_own_way(screen):
    """A card, a briefing, a reply and a task looked alike in a list; each
    has its sign and its form, so the page reads at a glance."""
    page = _open(screen, Backend())

    assert _texts(page, ".day-head h2") == ["Today"]
    assert _texts(page, ".kind-said .slip") == ["What's on tomorrow?"]
    assert page.locator(".starters").count() == 0, "a day with anything on it needs no starters"
    # Its text, not innerText, which counts the mark round "Design review"
    # (placed, so a block) as a line; a copy of the words reads as one.
    assert page.locator(".kind-reply .prose li").all_text_contents() == [
        "Design review at 10:00", "Lunch with Sam"]
    assert _texts(page, ".kind-reply .prose strong") == ["Design review"]
    gutter = _texts(page, ".kind-briefing .gutter")[0].split("\n")
    assert gutter == [_local(page, "2026-10-15T12:00:00Z"),
                      f"sent {_local(page, '2026-10-15T13:14:00Z')}", "Signal"]
    assert page.locator('[data-key="action:5"] .sign-needs').count() == 1
    assert _texts(page, '[data-key="action:6"] .failed-words') == ["Couldn't: Send the notes."]
    assert page.locator('[data-key="task:7"] .sign.turning').count() == 1
    went, woke = _local(page, "2026-10-15T04:50:00Z"), _local(page, "2026-10-15T12:10:00Z")
    assert _texts(page, ".entry.asleep") == [f"Asleep {went} – {woke}"]

    spoken = page.locator(".spoken-head")
    assert spoken.inner_text().split("\n") == ["Spoken · 4 min", "Remind me about Friday"]
    spoken.click()
    assert len(page.locator(".transcript li").all()) == 2

    # Updates in a row: under a bracket, the first two shown.
    assert _texts(page, ".run-head") == ["While you were away · 3"]
    assert len(page.locator(".run .entry").all()) == 2
    page.get_by_role("button", name="1 more").click()
    assert len(page.locator(".run .entry").all()) == 3
    page.get_by_role("button", name="Reply").click()
    assert page.locator(".composer textarea").input_value() == "> How did lunch with Sam go?\n"


def test_a_link_that_will_not_open_says_why_beside_it(screen):
    """A link in Steve's words that would not open said nothing, so the
    click seemed lost. Here the shell refuses to open it."""
    page = _open(screen, Backend(entries=[
        _entry("reply", "reply:12", "2026-10-15T14:00:20Z", id=12, session_id=1, channel="tray",
               text="It's in [the forecast](https://example.com/rain).", reaction=None)]))
    page.evaluate("""() => { window.__STEVE_SHELL__.answers.open_link = () => {
      throw "no browser to open it in"; }; }""")

    assert page.locator(".prose-link-failed").count() == 0
    page.get_by_role("button", name="the forecast").click()
    page.locator(".prose-link-failed").wait_for(timeout=5_000)
    assert _texts(page, ".prose-link-failed") == ["(no browser to open it in)"]


def test_a_spoken_exchanges_summary_takes_its_first_words_place_when_the_poll_brings_it(screen):
    """An exchange is summed up minutes after it ends, while the page is
    open: the line under it changes then, without a reload."""
    backend = Backend()
    page = _open(screen, backend)
    head = page.locator(".spoken-head")
    assert head.inner_text().split("\n") == ["Spoken · 4 min", "Remind me about Friday"]

    backend.entries = [
        {**e, "summary": "Asked to be reminded about Friday's trip"} if e["kind"] == "spoken"
        else e for e in backend.entries]

    page.locator(".spoken-head", has_text="Friday's trip").wait_for(timeout=15_000)
    assert head.inner_text().split("\n") == ["Spoken · 4 min",
                                             "Asked to be reminded about Friday's trip"]


NIGHT = _entry("task", "task:9", "2026-10-15T08:04:00Z", id=9, of="night",
               title="The night's work", detail="done", status="done",
               finished="2026-10-15T08:04:00Z", minutes=4,
               summary=("Overnight I read about 2 topics and rehearsed the desk. "
                        "It took 4 minutes and $0.01."),
               parts=[{"title": "The desk, rehearsed",
                       "items": ["Desk rehearsal did not finish: it exited 1."]},
                      {"title": "Read about", "items": ["Tide tables", "Ferry timetables"]}])


def test_the_nights_work_is_folded_under_its_summary_and_opens_to_its_parts(screen):
    """The night showed as "The night's work" and "done", saying nothing of
    what it did. It folds as a spoken exchange does, under a summary of two
    sentences, and opens to its parts; folded again, a poll that brings
    the day's next entry leaves it folded and draws it once."""
    backend = Backend(entries=[NIGHT, *DAY[2:4]])
    page = _open(screen, backend)
    head = page.locator(".night .spoken-head")
    assert head.inner_text().split("\n") == ["Overnight · 4 min", NIGHT["summary"]]
    assert head.get_attribute("aria-expanded") == "false"
    assert page.locator(".night-parts").count() == 0
    # Its diamond on its first line's middle, as a spoken exchange's is.
    page.evaluate("document.fonts.ready.then(() => true)")
    assert [d for d in page.evaluate(_SIGN_DRIFT) if abs(d["drift"]) > 1.0] == []

    head.click()
    assert _texts(page, ".night-parts .part-title") == ["The desk, rehearsed", "Read about"]
    assert _texts(page, ".night-parts li") == [
        "Desk rehearsal did not finish: it exited 1.", "Tide tables", "Ferry timetables"]
    head.click()
    assert page.locator(".night-parts").count() == 0

    backend.entries.append(_entry("reply", "reply:12", "2026-10-15T14:05:00Z", id=12,
                                  session_id=1, text="And a third.", reaction=None))
    backend.say.put({"type": "reply", "text": "", "session_id": 1})
    page.locator(".kind-reply .prose", has_text="And a third.").wait_for(timeout=15_000)
    assert page.locator(".night").count() == 1
    assert head.get_attribute("aria-expanded") == "false"
    assert page.locator(".night-parts").count() == 0


def test_the_mark_and_the_held_voice_button_follow_the_voices_level(screen):
    """Listening showed a still diamond whatever the microphone heard, and
    speaking showed nothing. The cyan diamond follows the level the backend
    tells, the held button's ring with it; speaking says so; quiet settles."""
    backend = Backend()
    page = _open(screen, backend)
    mark = page.locator(".presence svg.mark")
    cyan = mark.locator(".diamond.voice")
    button = page.locator(".composer .voice")

    def style(of, what: str) -> str:
        return of.evaluate(f"e => getComputedStyle(e).{what}")

    def levelled(of) -> str:
        return of.evaluate("e => e.style.getPropertyValue('--level')")

    backend.say.put({"type": "level", "of": "speaking", "level": 0.4})
    page.locator(".presence-line", has_text="Speaking").wait_for(timeout=10_000)
    assert mark.get_attribute("data-state") == "speaking"
    _until(lambda: style(cyan, "scale") == "1.12")
    assert levelled(button) == ""  # not held

    backend.say.put({"type": "level", "of": None, "level": 0})
    page.locator(".presence-line", has_text="Idle").wait_for(timeout=10_000)
    assert levelled(mark) == "" and cyan.count() == 0

    # Listening before a level is told rests at the design's 15% larger,
    # not at idle's size.
    backend.say.put({"type": "listening"})
    page.locator(".presence-line", has_text="Listening").wait_for(timeout=10_000)
    _until(lambda: style(cyan, "scale") == "1.15")
    assert levelled(mark) == ""
    backend.say.put({"type": "level", "of": "listening", "level": 0.8})
    _until(lambda: levelled(button) == "0.8")
    _until(lambda: style(cyan, "scale") == "1.24")
    _until(lambda: style(button, "boxShadow").endswith("0px 0px 0px 3.2px"))


def test_a_press_of_the_voice_button_carries_the_session_the_shell_holds(screen):
    """The backend opens no microphone for a press without the app's live
    session (agent/worker/routes.py): signed out, none goes with it."""
    backend = Backend()
    page = _open(screen, backend)
    button = page.locator(".composer .voice")
    pressed = lambda: [m for m in backend.sent if m.get("type") == "voice_start"]  # noqa: E731

    button.dispatch_event("pointerdown")
    _until(pressed)
    button.dispatch_event("pointerup")
    page.evaluate("""() => {
      sessionStorage.setItem('shell.session', 's-maya');
      window.__STEVE_SHELL__.emit('session', { live: true });
    }""")

    def press_again():
        button.dispatch_event("pointerdown")
        button.dispatch_event("pointerup")
        return len(pressed()) > 1 and pressed()[-1].get("session")

    _until(press_again)
    assert pressed()[0] == {"type": "voice_start"}
    assert pressed()[-1] == {"type": "voice_start", "session": "s-maya"}


def test_the_lenses_show_the_talk_or_the_log_and_the_keyboard_switches_them(screen):
    """Talk is what the two of them said; Log is what Steve did on its own."""
    page = _open(screen, Backend())
    kinds = lambda: {k.split("kind-")[1].split()[0] for k in  # noqa: E731
                     page.eval_on_selector_all(".day-sheet .entry[class*=kind-]",
                                               "els => els.map(e => e.className)")}

    page.get_by_role("button", name="Talk").click()
    assert kinds() == {"said", "reply", "spoken"}
    page.keyboard.press("Control+3")
    assert kinds() == {"briefing", "action", "task", "update"}
    page.keyboard.press("Control+1")
    assert kinds() == {"briefing", "said", "reply", "spoken", "action", "task", "update"}
    assert page.get_by_role("button", name="All").get_attribute("aria-pressed") == "true"


def test_a_message_sent_waits_on_the_page_then_the_working_line_then_the_reply(screen):
    """Sent, the words show at once; while Steve works the line under them
    says what it is doing; the reply lands where the line was, read from
    the day, so it is the same reply the phone would show."""
    backend = Backend(entries=[])
    page = _open(screen, backend)
    box = page.locator(".composer textarea")
    box.fill("Book a table for Friday")
    box.press("Shift+Enter")
    assert box.input_value() == "Book a table for Friday\n"
    box.press("Enter")

    _until(lambda: {"type": "chat", "text": "Book a table for Friday"} in backend.sent)
    assert _texts(page, ".unsent .slip") == ["Book a table for Friday"]
    assert box.input_value() == ""

    now = datetime.now(UTC)
    backend.entries.append(_entry("said", "said:30", now.isoformat().replace("+00:00", "Z"),
                                  id=30, session_id=1, text="Book a table for Friday"))
    backend.say.put({"type": "thinking"})
    backend.say.put({"type": "doing", "text": "Checking your calendar", "of": "turn"})
    _until(lambda: _texts(page, ".entry.working") == ["Checking your calendar"])
    _until(lambda: _texts(page, ".kind-said:not(.unsent) .slip") == ["Book a table for Friday"])
    assert page.locator(".entry.working .sign.turning").count() == 1

    later = (now + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    backend.entries.append(_entry("reply", "reply:31", later, id=31, session_id=1,
                                  text="Booked for 19:30.", reaction=None))
    backend.say.put({"type": "doing", "text": None, "of": "turn"})
    backend.say.put({"type": "reply", "text": "Booked for 19:30.", "session_id": 1})
    _until(lambda: _texts(page, ".kind-reply:not(.first-day) .prose") == ["Booked for 19:30."],
           within=4)
    assert page.locator(".entry.working").count() == 0


def test_a_message_the_backend_could_not_take_is_marked_and_sent_again_on_retry(screen):
    """A message lost to a dropped connection looked sent; it stays in its
    slip, turned red, with Retry."""
    backend = Backend(entries=[])
    page = _open(screen, backend)
    _until(lambda: page.evaluate("document.querySelector('.presence-line')?.textContent")
           == "Idle")
    backend.down = True
    _until(lambda: page.evaluate("document.querySelector('.presence-line')?.textContent")
           != "Idle")

    box = page.locator(".composer textarea")
    box.fill("Are you there?")
    box.press("Enter")
    retry = page.get_by_role("button", name="Not sent · Retry")
    retry.wait_for(timeout=5_000)
    assert page.locator(".unsent.failed .slip").inner_text() == "Are you there?"

    backend.down = False
    _until(lambda: page.evaluate("document.querySelector('.presence-line')?.textContent")
           == "Idle", within=20)
    retry.click()
    _until(lambda: {"type": "chat", "text": "Are you there?"} in backend.sent)
    assert page.locator(".unsent.failed").count() == 0


def _presence(page) -> str | None:
    return page.evaluate("document.querySelector('.presence-line')?.textContent")


def test_a_message_answered_before_the_page_turned_is_not_shown_again_as_sending_after_it(screen):
    """In a window left open overnight, two messages sent and answered the
    evening before came back at the foot of the next day, greyed as if
    still on their way. The window kept every message it had sent and
    matched them only against the newest page, so once the page turned at
    4 a.m. nothing there held them. A message leaves the window's list once
    a page holds it, each stored message taking one sent one; one the
    socket never took stays, with its Retry, across the turn."""
    backend = Backend(entries=[])
    page = _open(screen, backend)
    _until(lambda: _presence(page) == "Idle")
    box = page.locator(".composer textarea")

    # The control: a message the socket couldn't take, waiting for Retry. It
    # stays even once the same words, typed again, are held.
    backend.down = True
    _until(lambda: _presence(page) != "Idle")
    box.fill("Good night")
    box.press("Enter")
    page.get_by_role("button", name="Not sent · Retry").wait_for(timeout=5_000)
    backend.down = False
    _until(lambda: _presence(page) == "Idle", within=20)

    for _ in range(2):
        box.fill("Thanks")
        box.press("Enter")
    _until(lambda: backend.sent.count({"type": "chat", "text": "Thanks"}) == 2)
    assert _texts(page, ".unsent:not(.failed) .slip") == ["Thanks", "Thanks"]

    now = datetime.now(UTC)

    def at(seconds: int) -> str:
        return (now + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    # One stored message takes one of the two sent with its words, not both.
    backend.entries.append(_entry("said", "said:30", at(0), id=30, session_id=1, text="Thanks"))
    backend.say.put({"type": "reply", "text": "", "session_id": 1})
    _until(lambda: _texts(page, ".kind-said:not(.unsent) .slip") == ["Thanks"])
    assert _texts(page, ".unsent:not(.failed) .slip") == ["Thanks"]
    backend.entries += [
        _entry("reply", "reply:31", at(1), id=31, session_id=1, text="Any time.", reaction=None),
        _entry("said", "said:32", at(2), id=32, session_id=1, text="Thanks"),
        _entry("reply", "reply:33", at(3), id=33, session_id=1, text="Again, any time.",
               reaction=None)]
    backend.say.put({"type": "reply", "text": "", "session_id": 1})
    _until(lambda: _texts(page, ".kind-said:not(.unsent) .slip") == ["Thanks", "Thanks"])
    assert _texts(page, ".unsent .slip") == ["Good night"]

    # The last, sent just before 4 a.m.: the answer that brings it on its
    # own day's page turns the page too, so it is held by a page not the newest.
    box.fill("Good night")
    box.press("Enter")
    _until(lambda: {"type": "chat", "text": "Good night"} in backend.sent)
    backend.entries.append(_entry("said", "said:34", at(4), id=34, session_id=1,
                                  text="Good night"))
    backend.today = "2026-10-16"
    backend.say.put({"type": "reply", "text": "", "session_id": 1})
    _until(lambda: page.locator(".day-sheet[data-day='2026-10-16']").count() == 1)
    assert _texts(page, ".kind-said:not(.unsent) .slip") == ["Thanks", "Thanks", "Good night"]
    assert _texts(page, ".unsent .slip") == ["Good night"]
    assert page.locator(".unsent.failed").count() == 1


def test_a_check_or_a_cross_on_a_reply_is_told_to_the_backend_and_stays_drawn(screen):
    """The marks are the failure ledger's thumbs (agent/issues.py): a mark
    that vanished on the next poll would look unrecorded."""
    backend = Backend()
    page = _open(screen, backend)
    reply = page.locator('[data-key="reply:11"]')
    right = reply.get_by_role("button", name="Right", exact=True)
    reply.hover()
    right.click()

    _until(lambda: ("/control/react", {"message_id": 11, "verdict": "right"}) in backend.posted)
    assert right.get_attribute("aria-pressed") == "true"
    page.mouse.move(0, 0)
    assert reply.locator(".reactions").evaluate("e => getComputedStyle(e).opacity") == "1"

    reply.hover()
    right.click()
    _until(lambda: ("/control/react", {"message_id": 11, "verdict": None}) in backend.posted)


def test_undo_counts_down_beside_a_held_action_and_stops_it(screen):
    """Mail approved by mistake is held thirty seconds; Undo is where it
    happened, counting down, and gone when the hold is."""
    until = (datetime.now(UTC) + timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
    backend = Backend(entries=[_entry(
        "action", "action:9", "2026-10-15T14:00:00Z", id=9, status="executing", asked=True,
        by="tray", what="Send the deck to Iris", line="You said yes · under way", error=None,
        decided="2026-10-15T14:00:00Z", executed=None, undo_until=until)])
    page = _open(screen, backend)
    undo = page.locator(".undo")
    undo.wait_for(timeout=5_000)
    assert re.fullmatch(r"Undo · 0:(1\d|20)", undo.inner_text())
    undo.click()
    _until(lambda: ("/actions/decide", {"action_id": 9, "approve": False}) in backend.posted)


def test_a_listening_note_is_read_in_place_and_deleted_only_once_asked_twice(screen):
    """The Notes view went with item 13, and with it the only way to read a
    dictation in the app or to delete one with its transcript. The day is
    where a listening note is now, so both are there; a delete can't be
    undone, so it asks first. And the note leaves the page: a poll only
    adds and replaces, so it stayed, and a second delete failed."""
    backend = Backend(entries=[
        _entry("listening", "listening:4", "2026-10-15T15:00:00Z", id=4, of="dictation",
               title="Thank-you note", text="Dear Ines,\nthank you for the books.",
               doc_url=None, status="done", ended="2026-10-15T15:02:00Z"),
        _entry("listening", "listening:5", "2026-10-15T16:00:00Z", id=5, of="lecture",
               title="Pricing strategy", text="So far: anchoring.", doc_url=None,
               status="open", ended=None)])
    page = _open(screen, backend)
    done, open_ = page.locator(".listening").nth(0), page.locator(".listening").nth(1)

    assert done.locator(".listening-text").count() == 0
    done.locator(".show-note").click()
    assert done.locator(".listening-text").inner_text() == "Dear Ines,\nthank you for the books."
    assert open_.locator(".delete-note").count() == 0  # still being taken

    done.locator(".delete-note").click()
    assert ("/control/note", {"id": 4, "op": "delete"}) not in backend.posted
    done.locator(".confirm-delete button", has_text="Yes").click()
    _until(lambda: ("/control/note", {"id": 4, "op": "delete"}) in backend.posted)
    _until(lambda: page.locator(".listening").count() == 1)
    assert "Pricing strategy" in page.locator(".listening").inner_text()


def test_days_before_are_folded_to_a_line_and_open_in_place(screen):
    """A year of pages costs nothing until it's read: each earlier day is
    one band, opened where it is."""
    backend = Backend(
        folded=[{"date": "2026-10-14", "line": "Quiet: two exchanges.", "count": 3},
                {"date": "2026-10-13", "line": "A quiet day", "count": 0}],
        older={"2026-10-14": [_entry("said", "said:2", "2026-10-14T15:00:00Z", id=2,
                                     session_id=1, text="Yesterday's words")]})
    page = _open(screen, backend)
    page.locator(".earlier").click()
    band = page.locator('.folded[data-day="2026-10-14"]')
    band.wait_for(timeout=5_000)
    assert "Quiet: two exchanges." in band.inner_text()
    assert band.locator(".folded-count").inner_text() == "3"

    band.click()
    _until(lambda: _texts(page, ".day-head h2") == ["Yesterday", "Today"])
    assert "date=2026-10-14" in backend.asked
    assert "Yesterday's words" in _texts(page, '[data-day="2026-10-14"] .slip')
    # The sidebar lists the days of this week that held anything.
    assert _texts(page, ".days .day") == ["Today", "Yesterday"]


YESTERDAY_BAND = {"date": "2026-10-14", "line": "Two exchanges", "count": 2}

def test_a_page_too_short_to_scroll_asks_for_earlier_days_only_when_the_link_is_clicked(screen):
    """A short page always shows its top, so the days before loaded on
    their own, a week at once, and pushed today below the window: on the
    first day after setup the user saw a week of empty bands and no today."""
    backend = Backend(entries=[], folded=[YESTERDAY_BAND])
    page = _open(screen, backend)
    page.locator(".earlier").wait_for(timeout=5_000)
    time.sleep(1.0)
    assert backend.days_asked == []
    assert page.locator(".folded").count() == 0

    page.locator(".earlier").click()
    page.locator('.folded[data-day="2026-10-14"]').wait_for(timeout=5_000)
    assert backend.days_asked == [f"before={TODAY}&n=7"]


def test_a_page_that_scrolls_asks_for_earlier_days_once_scrolled_to_its_top(screen):
    """Scrolling up is the ask: a long day reads back into the week before
    without a click, and opening on its foot asks for nothing."""
    many = [_entry("said", f"said:{n}", f"2026-10-15T{8 + n // 6:02d}:{n % 6 * 10:02d}:00Z",
                   id=n, session_id=1, text=f"Line {n} of a long day") for n in range(1, 60)]
    backend = Backend(entries=many, folded=[YESTERDAY_BAND])
    page = _open(screen, backend)
    _until(lambda: _at_foot(page))
    time.sleep(0.5)
    assert backend.days_asked == []

    # Moved without a hand, as a fold or an open moves it: nothing is asked.
    page.locator(".day-stream").evaluate("box => { box.scrollTop = 0; }")
    page.wait_for_timeout(400)
    assert backend.days_asked == []

    page.locator(".day-stream").evaluate("box => { box.scrollTop = box.scrollHeight; }")
    page.locator(".day-stream").hover()
    page.mouse.wheel(0, -100_000)
    page.locator('.folded[data-day="2026-10-14"]').wait_for(timeout=5_000)
    assert backend.days_asked[0] == f"before={TODAY}&n=7"


def test_a_run_of_empty_days_is_one_thin_line_and_a_band_is_one_line(screen):
    """A week away was seven bands, each two lines of "nothing": the page
    said nothing in a screenful. Empty days run together into one line."""
    backend = Backend(entries=[], folded=[
        {"date": "2026-10-14", "line": "Two exchanges about the trip", "count": 2},
        {"date": "2026-10-13", "line": "", "count": 0},
        {"date": "2026-10-12", "line": "", "count": 0},
        {"date": "2026-10-11", "line": "", "count": 0},
        {"date": "2026-10-10", "line": "A note on the lease", "count": 1}])
    page = _open(screen, backend)
    page.locator(".earlier").click()
    page.locator('.folded[data-day="2026-10-14"]').wait_for(timeout=5_000)

    assert page.locator(".folded").count() == 2
    quiet = _texts(page, ".folded-quiet")
    assert len(quiet) == 1 and quiet[0].endswith("nothing happened")
    assert "11" in quiet[0] and "13" in quiet[0]
    heights = page.locator(".folded").evaluate_all("els => els.map(e => e.offsetHeight)")
    assert all(h < 40 for h in heights), heights
    # Oldest first: the band, the quiet run, then the day after it.
    order = page.evaluate("""() => [...document.querySelectorAll('.folded, .folded-quiet')]
      .map(e => e.dataset.day || 'quiet')""")
    assert order == ["2026-10-10", "quiet", "2026-10-14"]


def test_an_opened_earlier_day_folds_back_to_its_band_in_its_place(screen):
    """An opened day had no way back, so a week read back stayed open,
    page after page, above today. Fold returns its band where it was; the
    newest page, the one being written, never folds."""
    backend = Backend(
        folded=[{"date": "2026-10-14", "line": "Two exchanges", "count": 2},
                {"date": "2026-10-13", "line": "", "count": 0}],
        older={"2026-10-14": [_entry("said", "said:2", "2026-10-14T15:00:00Z", id=2,
                                     session_id=1, text="Yesterday's words")]})
    page = _open(screen, backend)
    page.locator(".earlier").click()
    page.locator('.folded[data-day="2026-10-14"]').click()
    _until(lambda: _texts(page, ".day-head h2") == ["Yesterday", "Today"])
    assert page.locator('.day-sheet[data-day="2026-10-14"] .day-fold').count() == 1
    assert page.locator(f'.day-sheet[data-day="{TODAY}"] .day-fold').count() == 0

    page.locator('.day-sheet[data-day="2026-10-14"] .day-fold').click()
    _until(lambda: _texts(page, ".day-head h2") == ["Today"])
    order = page.evaluate("""() => [...document.querySelectorAll(
      '.folded, .folded-quiet, .day-sheet')]
      .map(e => e.classList.contains('folded-quiet') ? 'quiet' : e.dataset.day)""")
    assert order == ["quiet", "2026-10-14", TODAY]
    assert backend.asked.count("date=2026-10-14") == 1
    # Its band was kept while it was open: folding asks the backend nothing.
    assert len(backend.days_asked) == 1

# How far each sign's middle is from the middle of its first line's
# lowercase letters: the baseline found from the first glyph's box and the
# font's own ascent, and half the height of an "x", both as the canvas
# measures them in the face the line is set in.
_SIGN_DRIFT = """() => [...document.querySelectorAll('.entry')].flatMap((entry) => {
  const sign = entry.querySelector('.gutter .sign');
  const body = entry.querySelector('.entry-body');
  if (!sign || !body) return [];
  const walk = document.createTreeWalker(body, NodeFilter.SHOW_TEXT,
    { acceptNode: (n) => (n.textContent.trim()
      ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP) });
  const text = walk.nextNode();
  const style = getComputedStyle(text.parentElement);
  const range = document.createRange();
  const at = text.textContent.search(/\\S/);
  range.setStart(text, at);
  range.setEnd(text, at + 1);
  const glyph = range.getClientRects()[0];
  const pen = document.createElement('canvas').getContext('2d');
  pen.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
  const font = pen.measureText('x');
  const area = font.fontBoundingBoxAscent + font.fontBoundingBoxDescent;
  const baseline = glyph.top + (glyph.height - area) / 2 + font.fontBoundingBoxAscent;
  const middle = baseline - font.actualBoundingBoxAscent / 2;
  const box = sign.getBoundingClientRect();
  return [{ key: entry.dataset.key || entry.className, drift: box.top + box.height / 2 - middle }];
})"""


def test_each_sign_sits_on_the_middle_of_its_first_lines_lowercase_letters_at_each_size(screen):
    """The diamond was set 0.62em down the gutter's own type, whatever
    the line beside it: a reminder's smaller line had it hanging low, and a
    larger text size moved it further off. Within a pixel, at each size."""
    page = _open(screen, Backend())
    page.evaluate("document.fonts.ready.then(() => true)")
    for size in ("normal", "larger"):
        page.evaluate("s => { document.documentElement.dataset.size = s; }", size)
        page.wait_for_timeout(100)
        drifts = page.evaluate(_SIGN_DRIFT)
        assert len(drifts) >= 6, drifts
        off = [d for d in drifts if abs(d["drift"]) > 1.0]
        assert off == [], (size, off)

def test_the_microphone_button_shows_a_drawn_microphone(screen):
    """A 10 by 14 rounded outline beside the composer read as a nought: the
    button that talks shows what it is, at a size that can be read."""
    page = _open(screen, Backend())
    mic = page.locator('.voice svg[data-mark="mic"]')
    assert mic.count() == 1
    box = mic.bounding_box()
    assert box["width"] >= 18 and box["height"] >= 18, box


def test_the_hand_underlines_the_day_circles_the_lens_and_ticks_a_reminder_that_went_off(
        screen):
    """The hand's marks were small and showed only at a moment, so nobody
    saw them. The day's name is underlined by hand, the chosen lens circled
    rather than underlined like a link, and a reminder that went off is
    ticked, where a heads-up keeps its diamond."""
    fired = _entry("update", "outbox:8", "2026-10-15T09:00:00Z", id=8, of="reminder",
                   which=None, text="Water the ferns.", sent=None, stopped=None)
    told = _entry("update", "outbox:9", "2026-10-15T09:30:00Z", id=9, of="heads_up",
                  which=None, text="The 10:00 moved to 10:30.", sent=None, stopped=None)
    page = _open(screen, Backend(entries=[fired, told]))

    assert page.locator('[data-key="outbox:8"] .sign-ticked[data-mark="check"]').count() == 1
    assert page.locator('[data-key="outbox:9"] .sign-ticked').count() == 0
    assert page.locator('[data-key="outbox:9"] span.sign-told').count() == 1

    name = page.locator(".day-head h2").bounding_box()
    line = page.locator('.day-head h2 .hand-mark[data-mark="underline"]').bounding_box()
    assert line["x"] <= name["x"] and line["x"] + line["width"] >= name["x"] + name["width"]
    assert name["y"] + name["height"] / 2 < line["y"] <= name["y"] + name["height"]
    # Under the letters, not a line of its own: the date below doesn't move.
    assert page.locator(".day-head h2").evaluate(
        "h => h.getBoundingClientRect().height <= parseFloat(getComputedStyle(h).lineHeight) + 1")

    def circled() -> list[str]:
        return page.eval_on_selector_all(
            ".lenses button", "bs => bs.filter(b => b.querySelector('[data-mark=circle]'))"
                              ".map(b => b.textContent)")

    assert circled() == ["All"]
    page.keyboard.press("Control+2")
    _until(lambda: circled() == ["Talk"])
    talk = page.get_by_role("button", name="Talk")
    ink, word = talk.evaluate("""b => {
      const box = (rs) => ({left: Math.min(...rs.map(r => r.left)),
                            top: Math.min(...rs.map(r => r.top)),
                            right: Math.max(...rs.map(r => r.right)),
                            bottom: Math.max(...rs.map(r => r.bottom))});
      const words = document.createRange();
      words.selectNodeContents(b.firstChild);
      return [box([...b.querySelectorAll('.hand-mark path')].map(p => p.getBoundingClientRect())),
              box([words.getBoundingClientRect()])];
    }""")
    assert ink["left"] < word["left"] and ink["right"] > word["right"], (ink, word)
    assert ink["top"] < word["top"] and ink["bottom"] > word["bottom"], (ink, word)
    assert talk.evaluate("b => getComputedStyle(b).textDecorationLine") == "none"


MARKED = ("Your flight is at **6:40**, from **Terminal B, the upper gates**.\n\n"
          "> Bring the printed pass;\n> the airline's app is refused at the gate.\n"
          "> Bags over 23 kg are paid for at the desk,\n> which closes an hour before.\n\n"
          "Also **$42** for the bag, and **Maya** meets you.")


def _marks(page, key: str) -> list[tuple[str, str]]:
    return page.eval_on_selector_all(
        f'[data-key="{key}"] .prose .hand-mark',
        "ms => ms.map(m => [m.dataset.mark, m.dataset.variant])")


def _ink_and_words(page, selector: str) -> tuple[dict, dict]:
    """The bounds of a mark's drawn ink and of the words it marks."""
    return page.locator(selector).evaluate("""el => {
      const box = (rs) => ({left: Math.min(...rs.map(r => r.left)),
                            top: Math.min(...rs.map(r => r.top)),
                            right: Math.max(...rs.map(r => r.right)),
                            bottom: Math.max(...rs.map(r => r.bottom))});
      const words = document.createRange();
      const said = [...el.childNodes].filter(n => !(n instanceof SVGElement));
      words.setStartBefore(said[0]);
      words.setEndAfter(said[said.length - 1]);
      return [box([...el.querySelectorAll('.hand-mark path')].map(p => p.getBoundingClientRect())),
              box([...words.getClientRects()])];
    }""")


def test_the_hand_marks_a_replys_first_three_bold_or_quoted_parts_the_same_at_every_redraw(
        screen):
    """Bold was drawn as plain bold, so what Steve said not to miss looked
    like the rest. A bold is underlined, a quote lined down its side, in
    the reply's own ink; past three marks the rest stays bold, or a page of
    marks marks nothing. A reply already there shows its marks drawn; one
    that arrives as the user looks draws them in. Until 19 October a short
    bold was circled in red, which crowded the words beside it and read as
    a correction."""
    now = datetime.now(UTC)
    at = now.isoformat().replace("+00:00", "Z")
    backend = Backend(entries=[_entry("reply", "reply:40", at, id=40, session_id=1,
                                      text=MARKED, reaction=None)])
    page = _open(screen, backend)
    reply = '[data-key="reply:40"] .prose'

    assert [name for name, _ in _marks(page, "reply:40")] == ["underline", "underline", "bracket"]
    assert _texts(page, f"{reply} strong.marked") == ["6:40", "Terminal B, the upper gates"]
    assert _texts(page, f"{reply} strong:not(.marked)") == ["$42", "Maya"]
    assert page.locator(f"{reply} .hand-mark.draws").count() == 0, "there already: drawn"

    for nth in (0, 1):
        ink, words = _ink_and_words(page, f"{reply} strong.marked >> nth={nth}")
        assert ink["left"] <= words["left"] + 2 and ink["right"] >= words["right"] - 2, (ink, words)
        assert words["bottom"] - 4 < ink["top"], (ink, words)
        assert ink["bottom"] < words["bottom"] + 8, (ink, words)
    words_ink = page.eval_on_selector(reply, "p => getComputedStyle(p).color")
    assert page.eval_on_selector_all(
        f"{reply} .hand-mark", "ms => ms.map(m => getComputedStyle(m).color)") == [words_ink] * 3
    quote = page.locator(f"{reply} blockquote").bounding_box()
    line = page.locator(f'{reply} blockquote .hand-mark[data-mark="bracket"]').bounding_box()
    assert line["x"] + line["width"] <= quote["x"] + 16, "beside the words, not over them"
    # Its ink, not its box, which the stylesheet sizes either way: scaled
    # whole, a bracket beside four lines stopped two thirds of the way down.
    ink = page.eval_on_selector_all(
        f'{reply} blockquote .hand-mark path',
        "ps => { const rs = ps.map(p => p.getBoundingClientRect());"
        " return Math.max(...rs.map(r => r.bottom)) - Math.min(...rs.map(r => r.top)); }")
    assert ink >= quote["height"] * 0.8, (ink, quote)

    shapes = _marks(page, "reply:40")
    page.reload()
    page.locator(reply).wait_for()
    assert _marks(page, "reply:40") == shapes

    later = (now + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    backend.entries.append({**_entry("reply", "reply:41", later, id=41, session_id=1,
                                     text="Leave by **5:15**.", reaction=None), "new": True})
    backend.say.put({"type": "reply", "text": "Leave by **5:15**.", "session_id": 1})
    _until(lambda: page.locator('[data-key="reply:41"] .hand-mark.draws').count() == 1)


def _hit(key: str, kind: str, at: str, line: str) -> dict:
    return {"key": key, "kind": kind, "at": at, "line": line}


def test_ctrl_k_searches_the_journal_and_a_found_line_opens_its_day_at_that_entry(screen):
    """A thing said weeks ago was found by scrolling back through every day
    between. What is found takes the days' place, a day at a time with its
    words marked; a line opens its page, however far back, at that entry,
    even one a lens hides or a run has folded; Esc returns to the days."""
    backend = Backend(older={"2026-10-02": [
        _entry("said", "said:2", "2026-10-02T15:00:00Z", id=2, session_id=1,
               text="Book the dentist for Theo")]})
    backend.found = [
        {"date": TODAY, "more": 0, "hits": [
            _hit("outbox:4", "update", "2026-10-15T18:10:00Z", "How did lunch with Sam go?")]},
        {"date": "2026-10-02", "more": 3, "hits": [
            _hit("said:2", "said", "2026-10-02T15:00:00Z", "Book the dentist for Theo")]}]
    page = _open(screen, backend)
    box = page.locator(".search-box")

    page.keyboard.press("Control+k")
    _until(lambda: box.evaluate("e => e === document.activeElement"))
    page.keyboard.type("Dentist")
    page.locator(".found-line").first.wait_for(timeout=10_000)
    assert backend.searched[-1] == "Dentist"
    assert not page.locator(".days").is_visible()
    assert _texts(page, ".found-day")[0] == "Today"
    assert _texts(page, ".found-line mark") == ["dentist"]
    assert _texts(page, ".found-more") == ["3 more that day"]

    # A day weeks back: read, and opened at the line.
    page.locator(".found-line", has_text="Book the dentist").click()
    sought = page.locator('[data-day="2026-10-02"] [data-key="said:2"]')
    sought.wait_for(timeout=10_000)
    assert "date=2026-10-02" in backend.asked
    _until(lambda: sought.get_attribute("data-sought") is not None)

    # The Talk lens hides an update, and the run folds the third: both give
    # way to the search.
    page.keyboard.press("Control+2")
    _until(lambda: page.locator('[data-key="outbox:2"]').count() == 0)
    page.locator(".found-line", has_text="lunch with Sam").click()
    found = page.locator('[data-key="outbox:4"]')
    found.wait_for(timeout=10_000)
    assert page.locator(".lenses button[aria-pressed=true]").inner_text() == "All"
    _until(lambda: found.get_attribute("data-sought") is not None)

    # Ctrl+K again takes the words, so the next search types over them.
    page.keyboard.press("Control+k")
    page.keyboard.type("lunch")
    assert box.input_value() == "lunch"

    box.press("Escape")
    page.locator(".days").wait_for(timeout=5_000)
    assert box.input_value() == "" and page.locator(".found").count() == 0


def test_in_a_narrow_window_ctrl_k_opens_the_folded_sidebar_for_the_search(screen):
    """Folded to a rail, or to nothing at all, the sidebar has no room for
    a search box: Ctrl+K opens it whole, and Esc folds it again."""
    backend = Backend()
    page = _open(screen, backend)
    sidebar = page.locator("nav.sidebar")
    box = page.locator(".search-box")
    for width in (700, 440):
        page.set_viewport_size({"width": width, "height": 700})
        page.keyboard.press("Control+k")
        _until(lambda: box.evaluate("e => e === document.activeElement"))
        assert sidebar.bounding_box()["width"] == 240
        box.press("Escape")
        _until(lambda: not sidebar.is_visible() or sidebar.bounding_box()["width"] == 64)


def test_an_entry_new_since_the_user_looked_is_highlighted_and_the_look_is_told(screen):
    """What arrived while the window was hidden is marked once, then fades;
    the backend hears the user has seen it, so it isn't new next time."""
    fresh = _entry("update", "outbox:8", "2026-10-15T09:00:00Z", id=8, of="heads_up",
                   which=None, text="Your flight is on time.", sent=None, stopped=None)
    fresh["new"] = True
    backend = Backend(entries=[fresh])
    page = _open(screen, backend)
    entry = page.locator('[data-key="outbox:8"]')
    assert "new" in entry.get_attribute("class")
    page.bring_to_front()
    page.evaluate("window.focus()")
    _until(lambda: "seen" in entry.get_attribute("class"))
    _until(lambda: any(path == "/control/looked" for path, _ in backend.posted), within=8)


def test_the_first_day_greets_the_user_by_name(screen):
    """An empty page with a box at the foot says nothing about what it is
    for; the first entry, written by the app, does."""
    page = _open(screen, Backend(entries=[]))
    first = page.locator(".first-day .prose")
    _until(lambda: first.count() == 1 and first.inner_text().startswith("Hello, Maya."))
    assert "one page a day" in first.inner_text()


def _chosen(page) -> tuple[str, str]:
    """The composer's text, and the part of it chosen."""
    return tuple(page.evaluate("""(() => { const t = document.querySelector('.composer textarea');
        return [t.value, t.value.slice(t.selectionStart, t.selectionEnd)]; })()"""))


def test_an_empty_day_offers_starters_that_fill_the_composer_at_their_blank_and_send_nothing(
        screen):
    """A blank page with a chat box showed none of what Steve does. Each
    starter writes its ask into the composer with its blank chosen, so the
    first key typed fills it; nothing goes, and nothing costs, until Enter.
    They step aside while the composer holds anything, so none writes over
    it. "Build me a tool" shows only where Steve can build one."""
    backend = Backend(entries=[])
    backend.places = {"ok": True, "coming": 0, "calls": 0,
                      "extensions": {"installed": 0, "building": 0, "can_build": True}}
    page = _open(screen, backend)
    starters = page.locator(".starters .starter")
    _until(lambda: _texts(page, ".starters .starter") == [
        "Remind me to … at 5 pm", "What's on my calendar tomorrow?", "Read this page for me: …",
        "Build me a tool that …"])

    starters.first.click()
    assert _chosen(page) == ("Remind me to … at 5 pm", "…")
    page.keyboard.type("water the plants")
    assert _chosen(page)[0] == "Remind me to water the plants at 5 pm"
    assert starters.count() == 0, "nothing may write over what is typed"
    page.keyboard.press("Control+a")
    page.keyboard.press("Backspace")
    page.get_by_role("button", name="What's on my calendar tomorrow?").click()
    assert _chosen(page) == ("What's on my calendar tomorrow?", "")
    time.sleep(0.5)
    assert [m for m in backend.sent if m.get("type") == "chat"] == [], "a starter never sends"

    page.keyboard.press("Enter")
    _until(lambda: [m.get("text") for m in backend.sent if m.get("type") == "chat"] == [
        "What's on my calendar tomorrow?"])


def _inbox(kind: str, text: str, n: int) -> dict:
    return {"type": "inbox", "message": {
        "id": n, "kind": kind, "text": text, "action_id": None, "remote": None,
        "created_at": "2026-10-15T16:00:00Z", "seen_at": None, "sent_at": None, "attempts": 0,
        "next_try_at": None, "stopped": None}}


def test_a_note_or_a_briefing_makes_a_toast_even_with_the_window_in_front(screen):
    """Notes and briefings go to Windows' notification centre too, where the
    user looks for what they missed (presence.md, Windows toasts). A reply
    or a result they are watching arrive would only be said twice."""
    backend = Backend()
    page = _open(screen, backend)
    page.bring_to_front()
    assert page.evaluate("document.hasFocus()"), "the window is in front"
    for n, (kind, text) in enumerate([
            ("reply", "At three, in Room 4."), ("card", "Send the reply to Theo Brandt?"),
            ("job", "The Northwind invoices are filed."),
            ("briefing", "Morning, Maya."), ("reminder", "Call Contoso")]):
        backend.say.put(_inbox(kind, text, n))

    def toasts():
        calls = page.evaluate("window.__STEVE_SHELL__.calls")
        return [(c["args"]["kind"], c["args"]["text"]) for c in calls if c["cmd"] == "notify"]
    _until(lambda: len(toasts()) >= 3)
    page.wait_for_timeout(300)
    assert toasts() == [("card", "Send the reply to Theo Brandt?"),
                        ("briefing", "Morning, Maya."), ("reminder", "Call Contoso")]


def test_a_result_makes_no_toast_while_the_desk_card_shows_but_a_note_still_does(screen):
    """Away from the window, a finished task's result is a toast; but while
    the desk card is on the screen it is the quiet way to see it, and a
    toast would say it twice (presence.md, Windows toasts). A note is in
    the notification centre either way."""
    backend = Backend()
    page = _open(screen, backend)
    page.evaluate("""() => {
      window.__STEVE_SHELL__.answers["plugin:window|is_visible"] = false;
      window.__STEVE_SHELL__.answers["deskcard_showing"] = true;
      window.dispatchEvent(new Event("blur"));
    }""")

    def toasts():
        calls = page.evaluate("window.__STEVE_SHELL__.calls")
        return [c["args"]["text"] for c in calls if c["cmd"] == "notify"]

    def asked_card() -> int:
        calls = page.evaluate("window.__STEVE_SHELL__.calls")
        return sum(c["cmd"] == "deskcard_showing" for c in calls)

    page.wait_for_timeout(300)  # the window found away
    backend.say.put(_inbox("job", "The Northwind invoices are filed.", 1))
    _until(lambda: asked_card() == 1)
    backend.say.put(_inbox("card", "Send the reply to Theo Brandt?", 2))
    _until(lambda: toasts() == ["Send the reply to Theo Brandt?"])

    page.evaluate("window.__STEVE_SHELL__.answers['deskcard_showing'] = false")
    backend.say.put(_inbox("job", "The Contoso contract is summarised.", 3))
    _until(lambda: len(toasts()) == 2)
    assert toasts() == ["Send the reply to Theo Brandt?", "The Contoso contract is summarised."]


def _emit(page, event: str, payload=None) -> None:
    page.evaluate("([e, p]) => window.__STEVE_SHELL__.emit(e, p)", [event, payload])


def _at_foot(page) -> bool:
    return page.evaluate("""() => {
      const box = document.querySelector(".day-stream");
      return box.scrollHeight - box.scrollTop - box.clientHeight < 4;
    }""")


def test_the_quick_bars_open_the_app_shows_the_newest_exchange_wherever_the_page_was(screen):
    """Ctrl+Enter in the quick bar, a toast's click and the desk card's all
    say "open today": the page left scrolled to the morning stayed there, and
    the exchange just had was out of sight below."""
    talk = [_entry("said", f"said:{n}", f"2026-10-15T{8 + n // 6:02d}:{n % 6 * 10:02d}:00Z",
                   id=n, session_id=1, channel="tray", text=f"Line {n} of a long morning")
            for n in range(40)]
    page = _open(screen, Backend(talk))
    _until(lambda: _at_foot(page))
    page.evaluate("document.querySelector('.day-stream').scrollTop = 0")
    page.wait_for_timeout(200)
    assert not _at_foot(page)

    _emit(page, "open-inbox")
    _until(lambda: _at_foot(page))


def _long_day(date: str, first: int, words: str) -> list[dict]:
    return [_entry("said", f"said:{n}", f"{date}T{8 + (n - first) // 6:02d}:"
                   f"{(n - first) % 6 * 10:02d}:00Z", id=n, session_id=1, text=f"{words} {n}")
            for n in range(first, first + 30)]


def _marked(page) -> list[str]:
    return _texts(page, '.days .day[aria-current="page"]')


def _in_view(page, selector: str) -> bool:
    """Whether the element is drawn inside the page's scrolling box."""
    return page.locator(selector).evaluate("""el => {
      const box = document.querySelector(".day-stream").getBoundingClientRect();
      const at = el.getBoundingClientRect();
      return at.bottom > box.top && at.top < box.bottom;
    }""")


def _scrolled_to(page, selector: str) -> list[str]:
    """The days marked a few frames after a scroll to the element: the
    window redraws every few seconds anyway, and the mark must not wait
    for that."""
    page.locator(selector).evaluate("""el => new Promise((done) => {
      el.scrollIntoView({ block: "start" });
      const frame = (n) => requestAnimationFrame(() => (n ? frame(n - 1) : done()));
      frame(3);
    })""")
    return _marked(page)


def test_today_goes_to_the_newest_exchange_and_the_sidebar_marks_the_day_in_view(screen):
    """Today in the sidebar did nothing on the journal, wherever it was
    scrolled, and the sidebar marked Today with a page of yesterday filling
    the window. Today is the foot of today's page, its newest exchange, from
    anywhere, even with a search's slow day still coming; the sidebar marks
    the day being read, and a poll leaves the reader where they were."""
    backend = Backend(
        _long_day(TODAY, 100, "Today, line"),
        folded=[{"date": "2026-10-13", "line": "A walk", "count": 30}, YESTERDAY_BAND],
        older={"2026-10-14": _long_day("2026-10-14", 200, "Yesterday, line"),
               "2026-10-13": _long_day("2026-10-13", 300, "Tuesday, line")})
    page = _open(screen, backend)
    _until(lambda: _at_foot(page))
    assert _marked(page) == ["Today"]
    page.locator(".earlier").click()
    page.locator('.folded[data-day="2026-10-13"]').wait_for(timeout=5_000)

    # A day from the sidebar: its page in view, and marked.
    page.locator(".days .day", has_text="Yesterday").click()
    page.locator('.day-sheet[data-day="2026-10-14"]').wait_for(timeout=5_000)
    _until(lambda: _marked(page) == ["Yesterday"])
    assert _in_view(page, '[data-key="said:200"]') and not _at_foot(page)

    # Today from the sidebar, with the journal already showing: the foot.
    page.locator(".days .day", has_text="Today").click()
    _until(lambda: _at_foot(page))
    assert _in_view(page, '[data-key="said:129"]')
    _until(lambda: _marked(page) == ["Today"])

    # Scrolled by hand to the day before: it is marked, and a poll that
    # brings a new line to today moves nothing.
    assert _scrolled_to(page, '.day-sheet[data-day="2026-10-14"]') == ["Yesterday"]
    top = page.locator(".day-stream").evaluate("box => box.scrollTop")
    backend.entries.append(_entry("reply", "reply:900", "2026-10-15T20:00:00Z", id=900,
                                  session_id=1, text="A poll's new line"))
    backend.say.put({"type": "reply", "text": "", "session_id": 1})
    page.locator('[data-key="reply:900"]').wait_for(state="attached", timeout=15_000)
    page.wait_for_timeout(300)
    assert page.locator(".day-stream").evaluate("box => box.scrollTop") == top
    assert _marked(page) == ["Yesterday"]

    # The oldest day opened in place, and read: its own name is marked.
    page.locator('.folded[data-day="2026-10-13"]').click()
    sheet = page.locator('.day-sheet[data-day="2026-10-13"]')
    sheet.wait_for(timeout=5_000)
    tuesday = page.locator(".days .day").nth(2).inner_text().strip()
    assert _scrolled_to(page, '.day-sheet[data-day="2026-10-13"]') == [tuesday]

    # A search's day weeks back, slow to come, and Today asked for before
    # it has: Today it stays.
    backend.older["2026-10-02"] = [_entry("said", "said:2", "2026-10-02T15:00:00Z", id=2,
                                          session_id=1, text="Book the dentist for Theo")]
    backend.found = [{"date": "2026-10-02", "more": 0, "hits": [
        _hit("said:2", "said", "2026-10-02T15:00:00Z", "Book the dentist for Theo")]}]
    backend.slow["2026-10-02"] = 1.5
    page.keyboard.press("Control+k")
    page.keyboard.type("dentist")
    page.locator(".found-line").first.click()
    page.keyboard.press("Control+t")
    page.locator('.day-sheet[data-day="2026-10-02"]').wait_for(timeout=10_000)
    page.wait_for_timeout(500)
    assert _at_foot(page) and _in_view(page, '[data-key="reply:900"]')
    assert _marked(page) == ["Today"]


def test_settings_follow_a_setting_another_window_changed(screen):
    """The tray's "Show the desk card" saves the setting in the card's own
    window: Settings here went on saying the card was off until the app was
    opened again, and a click there would have turned it off once more. Its
    own notices, which every window hears, it leaves: reloaded, a change's
    answer could land after the next change and show it back as it was."""
    appsettings.write({"desk_card": "off"})
    page = _open(screen, Backend())
    _emit(page, "open-view", "settings")
    shown = page.locator("[data-setting=desk_card] input")
    shown.wait_for(timeout=10_000)
    assert not shown.is_checked()

    appsettings.write({"desk_card": "on"})
    _emit(page, "app-settings", "main")
    page.wait_for_timeout(500)
    assert not shown.is_checked(), "its own notice reloaded the settings"
    _emit(page, "app-settings", "deskcard")
    _until(lambda: shown.is_checked())


def _photo(name: str, kind: str = "image/png") -> dict:
    return {"name": name, "mimeType": kind, "buffer": TINY_PNG}


# A photo pasted into the composer, or dropped on the page, as Windows
# hands one to the window.
_GIVE = """([how, name]) => {
  const given = new DataTransfer();
  given.items.add(new File([new Uint8Array([137, 80, 78, 71])], name, { type: "image/png" }));
  if (how === "paste") {
    document.querySelector(".composer textarea").dispatchEvent(
      new ClipboardEvent("paste", { clipboardData: given, bubbles: true, cancelable: true }));
    return;
  }
  const page = document.querySelector(".day-page");
  for (const kind of ["dragover", "drop"]) {
    page.dispatchEvent(
      new DragEvent(kind, { dataTransfer: given, bubbles: true, cancelable: true }));
  }
}"""


def test_photos_are_added_by_the_paperclip_a_paste_or_a_drop_up_to_four_and_taken_off_by_a_cross(
        screen):
    """The app had no way in for a photo, though the phones took them. Each
    way in adds to the message, above the composer, and the phones' limits
    are said as a photo is added rather than after the message is sent."""
    page = _open(screen, Backend(entries=[]))
    chooser = page.locator(".composer input[type=file]")
    thumbs = page.locator(".attached .thumb img")
    chooser.set_input_files([_photo("heron.png"), _photo("notes.pdf", "application/pdf")])
    _until(lambda: thumbs.count() == 1)
    assert page.inner_text(".attached-left") == (
        "Not added: notes.pdf, not a JPEG, PNG, WebP or GIF.")

    page.evaluate(_GIVE, ["paste", "screenshot.png"])
    page.evaluate(_GIVE, ["drop", "dropped.png"])
    _until(lambda: thumbs.count() == 3)
    chooser.set_input_files([_photo("egret.png"), _photo("crane.png")])
    _until(lambda: thumbs.count() == 4)
    assert "crane.png, past the 4 Steve looks at in a message" in page.inner_text(".attached-left")

    page.get_by_role("button", name="Take off screenshot.png").click()
    _until(lambda: [img.get_attribute("alt") for img in thumbs.all()]
           == ["heron.png", "dropped.png", "egret.png"])
    assert page.get_by_role("button", name="Send").count() == 1, "photos alone are a message"


def test_a_message_with_photos_sends_them_first_and_the_day_shows_them_on_it_until_they_go(
        screen):
    """Each photo goes up on its own request, since the socket takes a
    megabyte a message, and the message names them. The day shows them on
    the message, as they showed while it was on its way; once a photo's day
    is over, the day says "a photo" in its place."""
    backend = Backend(entries=[])
    page = _open(screen, backend)
    page.locator(".composer input[type=file]").set_input_files(
        [_photo("heron.png"), _photo("egret.png")])
    _until(lambda: page.locator(".attached .thumb img").count() == 2)
    box = page.locator(".composer textarea")
    box.fill("What birds are these?")
    box.press("Enter")

    _until(lambda: any("photos" in sent for sent in backend.sent))
    assert backend.sent[-1] == {"type": "chat", "text": "What birds are these?",
                                "photos": ["p0.png", "p1.png"]}
    assert backend.uploads == [TINY_PNG, TINY_PNG]
    assert page.locator(".attached .thumb").count() == 0
    assert page.locator(".unsent .photos .thumb").count() == 2

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    backend.entries.append(_entry("said", "said:30", now, id=30, session_id=1,
                                  text="What birds are these?", photos=2))
    said = page.locator('[data-key="said:30"]')
    _until(lambda: said.locator(".photo img").count() == 1)
    assert said.locator(".photo.gone").inner_text() == "a photo"
    assert _texts(page, '[data-key="said:30"] .slip') == ["What birds are these?"]
    assert page.locator(".unsent").count() == 0
    assert sorted(backend.photos_asked) == ["message=30&n=0", "message=30&n=1"]
