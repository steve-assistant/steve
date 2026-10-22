"""The quick bar: what is waiting before typing, the answer after, and talk on the hotkey's hold.

The old bar was a chat box with the cards under it: nothing in it said what
was waiting, what was next or what Steve had last done, and a held hotkey
talked in the main window, where the user wasn't looking (ui/src/QuickBar.tsx,
presence.md). These drive the bar's own window, built from this checkout, in
Edge, against a backend that serves notes, what's next and a day, and keeps
what the bar sends it.
"""

from __future__ import annotations

import asyncio
import json
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

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

CARD = {"kind": "yes", "id": "card:12", "action_id": 12, "word": "MAPLE", "detail": None,
        "text": "Send the reply to Theo Brandt?\n\nHi Theo, Thursday works for me.",
        "at": "2026-10-15T14:00:00Z", "actions": ["yes", "no"], "seen": "c0ffee12"}
GROUPED = {"kind": "yes", "id": "card:14", "action_id": 14, "word": "BIRCH", "detail": None,
           "text": "2 on one card: untick any to leave out", "at": "2026-10-15T14:02:00Z",
           "actions": ["yes", "no"], "items": ['Delete the draft "Saturday"?',
                                               'Delete the draft "Sunday"?'], "seen": "b1rch000"}
# A send's card as the backend makes its note (agent/worker/needs.py): the
# account first, then everyone it reaches and what it is.
SEND = {"kind": "yes", "id": "card:16", "action_id": 16, "word": "CEDAR",
        "text": "Send from your Mail account",
        "detail": ("To: Maya Lindqvist <maya.lindqvist@example.com>, "
                   "Theo Brandt <theo.brandt@example.com>, Ines Moreau <ines@example.com>\n"
                   "Bcc: Northwind Records <records@northwind.example>\n"
                   "Subject: The allotment rota for November, with the new plots"),
        "at": "2026-10-15T14:03:00Z", "actions": ["yes", "no"], "seen": "cedar016"}
SPEND = {"kind": "code", "id": "card:18", "action_id": 18, "word": "ALDER",
         "text": "Pay Northwind's invoice",
         "detail": "From: your card ending 4242\nAmount: $240.00",
         "at": "2026-10-15T14:04:00Z", "actions": ["yes", "no"], "seen": "a1der018"}
REMINDER = {"kind": "now", "id": "reminder:40", "action_id": None, "word": None, "detail": None,
            "text": "Call Northwind about the invoice", "at": "2026-10-15T14:05:00Z",
            "actions": ["done", "later"]}
STOPPED = {"kind": "stopped", "id": "cap:spend:day", "action_id": None, "word": None,
           "detail": None, "text": "Spending stopped at today's cap.",
           "at": "2026-10-15T14:10:00Z", "actions": ["raise_cap", "not_now"]}
LAST = "Moved the call with Contoso to Friday at 10."
PROMPT = "Ask or tell Steve"


class Backend:
    """Notes, what's next and a day; what the bar posts and sends; and a
    queue of what the socket is to say."""

    def __init__(self, needs: list[dict]):
        self.needs = list(needs)
        self.start = (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="seconds")
        self.posted: list[tuple[str, dict]] = []
        self.sent: list[dict] = []
        self.say: queue.Queue[dict] = queue.Queue()

    def app(self) -> Starlette:
        async def get_needs(request: Request):
            return JSONResponse({"ok": True, "needs": self.needs})

        async def answer(request: Request):
            body = await request.json()
            self.posted.append((request.url.path, body))
            answered = (body.get("id"), f"card:{body.get('action_id')}")
            self.needs = [n for n in self.needs if n["id"] not in answered]
            return JSONResponse({"ok": True})

        async def get_next(request: Request):
            return JSONResponse({"ok": True, "reminder": None, "event": {
                "summary": "Design review", "start": self.start, "end": self.start,
                "location": "Room 4"}})

        async def get_day(request: Request):
            today = datetime.now(UTC).date().isoformat()
            return JSONResponse({
                "ok": True, "date": today, "today": today, "turns_at": "04:00",
                "as_of": datetime.now(UTC).isoformat(), "looked": None, "entries": [
                    {"key": "said:1", "kind": "said", "at": "2026-10-15T13:00:00Z",
                     "changed": None, "channel": None, "new": False, "id": 1, "session_id": 1,
                     "text": "Move the Contoso call to Friday"},
                    {"key": "reply:2", "kind": "reply", "at": "2026-10-15T13:00:20Z",
                     "changed": None, "channel": None, "new": False, "id": 2, "session_id": 1,
                     "text": f"{LAST}\n\nI told them by mail.", "reaction": None},
                    {"key": "said:3", "kind": "said", "at": "2026-10-15T13:05:00Z",
                     "changed": None, "channel": None, "new": False, "id": 3, "session_id": 1,
                     "text": "And tell Theo"},
                ]})

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
                if message.get("text"):
                    self.sent.append(json.loads(message["text"]))

        async def anything(request: Request):
            return JSONResponse({"detail": "not in this test"}, status_code=404)

        return Starlette(routes=[
            Route("/control/needs", get_needs),
            Route("/control/needs/done", answer, methods=["POST"]),
            Route("/control/needs/later", answer, methods=["POST"]),
            Route("/control/needs/not-now", answer, methods=["POST"]),
            Route("/actions/decide", answer, methods=["POST"]),
            Route("/control/next", get_next),
            Route("/control/day", get_day),
            Route("/control/app", control.app_settings, methods=["GET", "POST"]),
            WebSocketRoute("/ws", socket),
            Route("/{path:path}", anything, methods=["GET", "POST", "PUT"]),
        ])


def _until(check, within: float = 15.0):
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time")
        time.sleep(0.05)


def _asked(page) -> list[str]:
    """The shell's commands the bar asked for, in order."""
    return [c["cmd"] for c in page.evaluate("window.__STEVE_SHELL__.calls")]


def _show(page) -> None:
    """The shell's word that the bar is on screen (lib.rs, place_quickbar)."""
    page.evaluate("window.__STEVE_SHELL__.emit('quickbar-shown', null)")


def _open(screen, backend: Backend):
    page = screen(backend.app(), label="quickbar", path="/#quickbar")
    page.locator(".quickbar").wait_for(timeout=15_000)
    _until(lambda: page.evaluate("""() => window.__STEVE_SHELL__.calls.some(
        (c) => c.cmd === "plugin:event|listen" && c.args.event === "quickbar-talk")"""))
    _show(page)
    return page


def _local(page, iso: str) -> str:
    return page.evaluate(
        "iso => new Date(iso).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})", iso)


def test_before_typing_the_bar_lists_the_notes_whats_next_and_steves_last_entry(screen):
    """What the user would otherwise open the app to see, one line each:
    the notes first, as they need an answer, then the next event, then what
    Steve last did. Typing puts them away."""
    backend = Backend([CARD, REMINDER])
    page = _open(screen, backend)
    rows = page.locator(".quickbar-rows > li")
    _until(lambda: rows.count() == 4)

    card, reminder, upcoming, last = (rows.nth(i).inner_text() for i in range(4))
    assert card.startswith("Needs you") and "Send the reply to Theo Brandt?" in card
    assert "Hi Theo" not in card, "a note's first line only"
    assert reminder.startswith("Now") and "Call Northwind about the invoice" in reminder
    assert upcoming == f"Next · Design review, {_local(page, backend.start)}"
    assert last == LAST, "Steve's last entry, its first line; not the user's"

    page.get_by_placeholder(PROMPT).fill("Ask Theo")
    _until(lambda: rows.count() == 0)


def test_a_note_answered_in_the_bar_is_answered_once_and_peels_away(screen):
    """A yes given here must be the card's one yes, and the note must go:
    left in the list, it would be answered twice."""
    backend = Backend([CARD, REMINDER])
    page = _open(screen, backend)
    card = page.locator(".quickbar-note", has_text="Theo Brandt")
    card.get_by_role("button", name="Yes").click()
    _until(lambda: card.count() == 0)
    assert backend.posted == [("/actions/decide",
                               {"action_id": 12, "approve": True, "seen": "c0ffee12"})]

    reminder = page.locator(".quickbar-note", has_text="Northwind")
    reminder.get_by_role("button", name="Later").click()
    reminder.get_by_role("button", name="An hour").click()
    _until(lambda: reminder.count() == 0)
    assert backend.posted[1:] == [("/control/needs/later", {"id": "reminder:40", "when": "1h"})]


def test_a_fix_and_the_notes_that_dont_fit_open_steve_where_they_are_done(screen):
    """Raising a cap or giving a passphrase is done in the app: a button
    here that did nothing, or a list longer than four rows, would hide
    that. Nothing is answered on the user's behalf."""
    backend = Backend([STOPPED, CARD, REMINDER])
    page = _open(screen, backend)
    rows = page.locator(".quickbar-rows > li")
    _until(lambda: rows.count() == 4)
    assert rows.nth(1).inner_text() == "2 more notes in Steve"

    rows.nth(0).get_by_role("button", name="Open Steve").click()
    _until(lambda: "quickbar_open_app" in _asked(page))
    rows.nth(1).get_by_role("button").click()
    _until(lambda: _asked(page).count("quickbar_open_app") == 2)
    assert backend.posted == []


def test_a_question_sent_from_the_bar_is_answered_beneath_it_with_the_working_line_meanwhile(
        screen):
    """The answer shows where the question was asked, and while it comes the
    journal's working line says what Steve is doing; the next question
    carries on the same conversation."""
    backend = Backend([])
    page = _open(screen, backend)
    box = page.get_by_placeholder(PROMPT)
    box.fill("When is the design review?")
    box.press("Enter")

    def chats():
        return [m for m in backend.sent if m.get("type") == "chat"]
    _until(lambda: len(chats()) == 1)
    assert chats() == [{"type": "chat", "text": "When is the design review?", "session_id": None}]
    backend.say.put({"type": "thinking", "session_id": 7})
    backend.say.put({"type": "doing", "of": "turn", "text": "Checking your calendar"})
    page.locator(".quickbar-working", has_text="Checking your calendar").wait_for()

    backend.say.put({"type": "reply", "session_id": 7, "text": "At three, in Room 4."})
    page.locator(".quickbar-answer", has_text="At three, in Room 4.").wait_for()
    assert page.locator(".quickbar-working").count() == 0
    assert page.locator(".quickbar-asked").inner_text() == "When is the design review?"
    assert page.locator(".quickbar-next, .quickbar-last").count() == 0, "for before asking"

    box.fill("And who is coming?")
    box.press("Enter")
    _until(lambda: len(chats()) == 2)
    assert chats()[1]["session_id"] == 7


def test_the_answers_bold_is_underlined_in_the_bar_as_on_the_day(screen):
    """The bar is where a quick answer is read, so what Steve marked not to
    miss is marked there too, drawn in as it arrives."""
    backend = Backend([])
    page = _open(screen, backend)
    box = page.get_by_placeholder(PROMPT)
    box.fill("When is the design review?")
    box.press("Enter")
    _until(lambda: any(m.get("type") == "chat" for m in backend.sent))

    backend.say.put({"type": "reply", "session_id": 7, "text": "At **three**, in Room 4."})
    page.locator(".quickbar-answer", has_text="At three, in Room 4.").wait_for()
    assert page.locator(
        '.quickbar-answer strong.marked .hand-mark.draws[data-mark="underline"]').count() == 1


def test_an_answer_that_reaches_a_closed_bar_goes_to_the_desk_card_and_one_seen_does_not(screen):
    """The user asked and closed the bar: the bar never opens again on its
    own, so its answer's first line goes to the desk card, which opens to
    show it. An answer read in the open bar is not said twice."""
    backend = Backend([])
    page = _open(screen, backend)

    def to_card() -> list:
        calls = page.evaluate("window.__STEVE_SHELL__.calls")
        return [c["args"]["payload"] for c in calls
                if c["cmd"] == "plugin:event|emit" and c["args"]["event"] == "deskcard-reply"]

    backend.say.put({"type": "reply", "session_id": 7, "text": "At three, in Room 4."})
    page.locator(".quickbar-answer", has_text="At three, in Room 4.").wait_for()
    page.wait_for_timeout(300)
    assert to_card() == []

    page.evaluate("window.__STEVE_SHELL__.answers['plugin:window|is_visible'] = false")
    backend.say.put({"type": "reply", "session_id": 7,
                     "text": "Booked for 19:30 at Rosa's.\n\nThe confirmation is in your mail."})
    _until(lambda: to_card() == ["Booked for 19:30 at Rosa's."])


def test_a_held_hotkey_listens_in_the_bar_and_sends_what_was_heard_when_let_go(screen):
    """The hold talks on the bar's own socket, so the words heard and the
    answer show where the user is looking. The tray's Talk has no key to let
    go of: Enter sends."""
    backend = Backend([])
    page = _open(screen, backend)
    page.evaluate("window.__STEVE_SHELL__.emit('quickbar-talk', true)")
    page.get_by_text("Let go of the key, or press Enter, to send.").wait_for()
    _until(lambda: backend.sent == [{"type": "voice_start"}])
    page.evaluate("window.__STEVE_SHELL__.emit('quickbar-talk', false)")
    _until(lambda: backend.sent[1:] == [{"type": "voice_stop"}])

    backend.say.put({"type": "voice_heard", "heard": "What's next today?"})
    page.locator(".quickbar-asked", has_text="What's next today?").wait_for()
    backend.say.put({"type": "voice_turn", "heard": "What's next today?",
                     "reply": "The design review, at three."})
    page.locator(".quickbar-answer", has_text="The design review, at three.").wait_for()

    page.evaluate("window.__STEVE_SHELL__.emit('quickbar-talk', true)")
    _until(lambda: backend.sent[2:] == [{"type": "voice_start"}])
    page.get_by_placeholder("Listening…").press("Enter")
    _until(lambda: backend.sent[3:] == [{"type": "voice_stop"}])


def test_while_steve_speaks_the_bars_mark_says_so_and_follows_the_voice(screen):
    """The answer to a held hotkey is spoken: the bar's mark shows it, as
    the app's does, and settles when the voice stops."""
    backend = Backend([])
    page = _open(screen, backend)
    mark = page.locator(".quickbar svg.mark")

    backend.say.put({"type": "level", "of": "speaking", "level": 0.4})
    _until(lambda: mark.get_attribute("data-state") == "speaking")
    _until(lambda: mark.evaluate("e => e.style.getPropertyValue('--level')") == "0.4")

    backend.say.put({"type": "level", "of": None, "level": 0})
    _until(lambda: mark.get_attribute("data-state") == "idle")


def test_esc_closes_the_bar_and_keeps_what_was_typed_for_ten_minutes(screen):
    """Closed to look something up, the half-typed question must be there
    on the way back; a day later, it is a stranger's."""
    backend = Backend([])
    page = _open(screen, backend)
    box = page.get_by_placeholder(PROMPT)
    box.fill("Ask Theo whether the Contoso")
    box.press("Escape")
    _until(lambda: "plugin:window|hide" in _asked(page))
    _show(page)
    assert box.input_value() == "Ask Theo whether the Contoso"

    box.press("Escape")
    page.evaluate("() => { const real = Date.now; Date.now = () => real() + 11 * 60 * 1000; }")
    _show(page)
    _until(lambda: box.input_value() == "")


def test_ctrl_enter_sends_and_opens_the_app(screen):
    """For a question whose answer wants the whole page."""
    backend = Backend([])
    page = _open(screen, backend)
    box = page.get_by_placeholder(PROMPT)
    box.fill("Draft a reply to Theo")
    box.press("Control+Enter")
    _until(lambda: "quickbar_open_app" in _asked(page))
    _until(lambda: [m.get("text") for m in backend.sent] == ["Draft a reply to Theo"])


def test_the_bar_wears_the_theme_and_reading_the_user_chose(screen):
    """It followed Windows' light or dark, whatever the app was in: a dark
    bar over a paper app, in a face the user had turned away from."""
    appsettings.write({"theme": "ink", "face": "atkinson", "size": "larger"})
    page = _open(screen, Backend([]))
    _until(lambda: page.evaluate("document.documentElement.dataset.theme") == "ink")
    assert page.evaluate("({...document.documentElement.dataset})") == {
        "theme": "ink", "face": "atkinson", "size": "larger"}


def test_a_grouped_card_is_opened_in_steve_not_approved_whole_from_the_bar(screen):
    """A grouped card says "untick any to leave out", and the bar has no
    boxes to untick: its Yes approved every item. The items are chosen in
    the app."""
    backend = Backend([GROUPED])
    page = _open(screen, backend)
    card = page.locator(".quickbar-note", has_text="2 on one card")
    card.wait_for(timeout=10_000)  # drawn, so a count of its buttons means something
    assert card.get_by_role("button", name="Yes").count() == 0
    card.get_by_role("button", name="Open Steve").click()
    _until(lambda: "quickbar_open_app" in _asked(page))
    assert backend.posted == []


def test_a_yes_in_the_bar_is_asked_with_the_card_whole_everyone_it_reaches_in_view(screen):
    """A send's card names its account on its first line and everyone it
    reaches after it (agent/toolset/mail.py `_send_card`). The bar showed
    the first line alone, cut to the row, so its Yes sent mail to people the
    user was never shown there (step 10, every guarantee from every door)."""
    backend = Backend([SEND])
    page = _open(screen, backend)
    card = page.locator(".quickbar-note", has_text="Send from your Mail account")
    line = card.locator(".quickbar-line")
    shown = line.inner_text().splitlines()
    # Each on its own line, as the phone shows them, not run together.
    for part in SEND["detail"].splitlines():
        assert part in shown
    # Wrapped, not cut: nothing of it hides past the row's edge.
    assert line.evaluate("e => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight")
    card.get_by_role("button", name="Yes").click()
    _until(lambda: backend.posted)
    assert backend.posted == [("/actions/decide",
                               {"action_id": 16, "approve": True, "seen": "cedar016"})]


def test_a_high_risk_card_is_opened_in_steve_where_its_passphrase_is_asked(screen):
    """The bar has no passphrase field and no Hello: a Yes here would be a
    bare yes on a spending card. The backend refuses one (e2e
    test_high_risk.py), and the bar does not offer it."""
    backend = Backend([SPEND])
    page = _open(screen, backend)
    card = page.locator(".quickbar-note", has_text="Pay Northwind's invoice")
    card.wait_for(timeout=10_000)
    assert card.get_by_role("button", name="Yes").count() == 0
    card.get_by_role("button", name="Open Steve").click()
    _until(lambda: "quickbar_open_app" in _asked(page))
    assert backend.posted == []


def test_talk_from_a_menu_stops_listening_when_the_bar_is_clicked_away(screen):
    """The tray's and the desk card's Talk hold no key to let go of. A
    click elsewhere hid the bar and left the microphone open until the
    backend's two-minute cap; closed, the bar stops listening, as Esc does."""
    backend = Backend([])
    page = _open(screen, backend)
    page.evaluate("window.__STEVE_SHELL__.emit('quickbar-talk', true)")
    _until(lambda: backend.sent == [{"type": "voice_start"}])
    page.evaluate("window.__STEVE_SHELL__.emit('tauri://blur', null)")
    _until(lambda: "plugin:window|hide" in _asked(page))
    _until(lambda: backend.sent[1:] == [{"type": "voice_stop"}])
