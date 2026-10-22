"""Notes: what needs the user, stuck beside the page until it is answered.

A yes waited in the Approvals tab, a reminder going off was a toast gone in
seconds, and a cap reached sat in Overview, so each was missed unless the
right place was opened (ui/src/StuckNotes.tsx, needs.ts). Now one list,
/control/needs, is drawn in the margin, or folded at the top of a narrower
page, and each note is answered where it is. These drive the built window
in Edge against a backend that serves notes and keeps the answers.
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from agent import appsettings
from agent.worker import control
from core import localapi

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

TODAY = "2026-10-15"
AT = "2026-10-15T14:10:00Z"
PASSPHRASE = "oak leaves in october"
SAID = {"key": "said:1", "kind": "said", "at": AT, "changed": AT, "channel": "tray", "new": False,
        "id": 1, "session_id": 1, "text": "Morning."}


def _card(n: int, text: str, *, word: str = "MAPLE", spends: bool = False,
          detail: str | None = None, items: list[str] | None = None,
          seen: str | None = None) -> dict:
    card = {"kind": "code" if spends else "yes", "id": f"card:{n}", "action_id": n,
            "text": text, "detail": detail, "word": word, "at": AT, "expires": None,
            "actions": ["yes", "no"]}
    return card | ({"items": items} if items else {}) | ({"seen": seen} if seen else {})


def _reminder(n: int, text: str) -> dict:
    return {"kind": "now", "id": f"reminder:{n}", "text": text, "detail": None, "word": None,
            "at": AT, "actions": ["done", "later"]}


def _stopped(note_id: str, text: str, fix: str) -> dict:
    return {"kind": "stopped", "id": note_id, "text": text, "detail": None, "word": None,
            "at": None, "actions": [fix, "not_now"]}


class Backend:
    """Notes the test can change, a day, and the answers the page sends."""

    def __init__(self, needs: list[dict], *, entries: list[dict] | None = None):
        self.needs = list(needs)
        self.entries = [SAID] if entries is None else entries
        self.posted: list[tuple[str, dict]] = []
        # What the day says once a card is said yes to, as the backend's
        # journal writes it.
        self.after_yes: list[dict] = []

    def app(self) -> Starlette:
        async def get_needs(request: Request):
            return JSONResponse({"ok": True, "needs": self.needs})

        async def answer(request: Request):
            body = await request.json()
            self.posted.append((request.url.path, body))
            self.needs = [n for n in self.needs if n["id"] != body["id"]]
            return JSONResponse({"ok": True})

        async def decide(request: Request):
            body = await request.json()
            self.posted.append((request.url.path, body))
            card = next(n for n in self.needs if n.get("action_id") == body["action_id"])
            if card["kind"] == "code" and body["approve"] and body.get("passphrase") != PASSPHRASE:
                return JSONResponse({"ok": False, "error": "wrong passphrase",
                                     "needs": "passphrase"}, status_code=400)
            self.needs = [n for n in self.needs if n is not card]
            if body["approve"]:
                self.entries = [*self.entries, *self.after_yes]
            return JSONResponse({"ok": True})

        async def day(request: Request):
            return JSONResponse({"ok": True, "date": TODAY, "today": TODAY, "turns_at": "04:00",
                                 "as_of": AT, "looked": None, "entries": self.entries})

        async def days(request: Request):
            return JSONResponse({"ok": True, "days": []})

        async def profile(request: Request):
            return JSONResponse({"ok": True, "profile": {"name": "Maya Lindqvist"}})

        async def socket(ws):
            await ws.accept(subprotocol=localapi.subprotocol(ws.scope))
            while (await ws.receive())["type"] != "websocket.disconnect":
                await asyncio.sleep(0)

        async def anything(request: Request):
            return JSONResponse({"detail": "not in this test"}, status_code=404)

        return Starlette(routes=[
            Route("/control/app", control.app_settings, methods=["GET", "POST"]),
            Route("/control/profile", profile),
            Route("/control/day", day),
            Route("/control/days", days),
            Route("/control/needs", get_needs),
            Route("/control/needs/done", answer, methods=["POST"]),
            Route("/control/needs/later", answer, methods=["POST"]),
            Route("/control/needs/not-now", answer, methods=["POST"]),
            Route("/actions/decide", decide, methods=["POST"]),
            WebSocketRoute("/ws", socket),
            Route("/{path:path}", anything, methods=["GET", "POST", "PUT"]),
        ])


# A hand mark's drawn ink, and the box of the words it marks, in the page.
_INK_ROUND_WORDS = """el => {
  const box = (rs) => ({left: Math.min(...rs.map(r => r.left)),
                        top: Math.min(...rs.map(r => r.top)),
                        right: Math.max(...rs.map(r => r.right)),
                        bottom: Math.max(...rs.map(r => r.bottom))});
  const words = document.createRange();
  words.selectNodeContents(el.firstChild);
  return [box([...el.querySelectorAll('.hand-mark path')].map(p => p.getBoundingClientRect())),
          box([words.getBoundingClientRect()])];
}"""


def _until(check, within: float = 15.0):
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time")
        time.sleep(0.05)


def _open(screen, backend: Backend, *, width: int = 1300):
    page = screen(backend.app())
    page.set_viewport_size({"width": width, "height": 800})
    page.locator(".day-sheet").first.wait_for(timeout=15_000)
    return page


def _note(page, note_id: str):
    return page.locator(f'article.note[data-need="{note_id}"]')


def _texts(page, selector: str) -> list[str]:
    return [t.strip() for t in page.locator(selector).all_inner_texts()]


def test_a_note_in_the_margin_shows_what_it_needs_its_word_and_its_pin(screen):
    """The note is the whole question: what, the detail that matters, and
    the word that answers it from the phone. Its tilt comes from its id,
    never flat, so it reads as stuck on, not printed."""
    backend = Backend([_card(5, "Send your reply to Dana Ortiz?",
                             detail="“Re: Saturday”, full text in your Gmail drafts.")])
    page = _open(screen, backend)
    note = _note(page, "card:5")
    note.wait_for(timeout=10_000)
    assert page.locator(".margin .notes").get_attribute("aria-label") == "Needs you, 1"
    assert note.locator(".note-kicker").inner_text() == "Needs you"
    assert note.locator(".note-text").inner_text() == "Send your reply to Dana Ortiz?"
    assert "Gmail drafts" in note.locator(".note-detail").inner_text()
    assert note.locator(".note-pin").count() == 1
    word = note.locator(".note-word")
    assert word.inner_text() == "MAPLE"
    assert word.get_attribute("title") == "Or reply YES MAPLE from your phone."
    page.locator("article.note:not(.note-arrives)").first.wait_for(timeout=5_000)
    tilt = float(note.evaluate("n => getComputedStyle(n).rotate").removesuffix("deg"))
    assert 0.4 <= abs(tilt) <= 1.0
    assert page.locator(".sidebar .day-needs").inner_text() == "1"
    # Circled by hand in red: the ink drawn round the number itself. The
    # mark's own box was round it while a flat oval ran through the digit.
    count = page.locator(".sidebar .day-needs .needs-count")
    ink, digit = count.evaluate(_INK_ROUND_WORDS)
    assert ink["left"] < digit["left"] and ink["right"] > digit["right"], (ink, digit)
    assert ink["top"] < digit["top"] and ink["bottom"] > digit["bottom"], (ink, digit)
    # Drawn to the count's shape, filling the box it is given: scaled whole
    # at its own 100:60, it was an oval as flat as a dash.
    given = count.locator('.hand-mark[data-mark="circle"]').bounding_box()
    assert ink["bottom"] - ink["top"] >= 0.8 * given["height"], (ink, given)


def test_yes_or_no_on_a_card_is_sent_and_the_note_peels_away(screen):
    backend = Backend([_card(5, "Send the deck to Iris?"),
                       _card(6, "Book the 8:10 train?", word="CEDAR")])
    page = _open(screen, backend)
    page.locator(".notes-spread").dispatch_event("click")
    _note(page, "card:5").get_by_role("button", name="Yes").click()
    _until(lambda: ("/actions/decide", {"action_id": 5, "approve": True}) in [
        (path, {k: v for k, v in body.items() if v is not None}) for path, body in backend.posted])
    _note(page, "card:5").wait_for(state="detached", timeout=5_000)
    _note(page, "card:6").get_by_role("button", name="No").click()
    _note(page, "card:6").wait_for(state="detached", timeout=5_000)
    assert backend.posted[-1] == ("/actions/decide", {"action_id": 6, "approve": False})
    assert page.locator("article.note").count() == 0


def test_a_yes_on_a_note_is_on_the_day_page_at_once_not_at_the_next_poll(screen):
    """The note peels away and the day says what was said yes to. That line
    waited for the page's next poll, which runs every five seconds and only
    while Windows calls the window visible: the exit walk, run under load
    with the screen locked, waited 30 s for a "You said yes" that never
    came. Here the window is taken for hidden, so no poll comes at all, and
    only the answer itself can bring the line."""
    backend = Backend([_card(5, "Send the deck to Iris?")])
    later = "2026-10-15T14:20:00Z"
    backend.after_yes = [{
        "key": "action:5", "kind": "action", "at": later, "changed": later, "channel": None,
        "new": False, "id": 5, "status": "approved", "asked": True, "by": "app",
        "what": "Send the deck to Iris", "line": "You said yes · under way", "error": None,
        "decided": later, "executed": None, "undo_until": None}]
    page = _open(screen, backend)
    page.evaluate("""() => {
      window.__STEVE_SHELL__.answers["plugin:window|is_visible"] = false;
      window.dispatchEvent(new Event("blur"));
    }""")
    page.wait_for_timeout(300)  # the window found away, and the day's poll stopped
    _note(page, "card:5").get_by_role("button", name="Yes").click()
    _note(page, "card:5").wait_for(state="detached", timeout=5_000)
    page.locator(".kind-action", has_text="You said yes").wait_for(timeout=5_000)


def test_a_card_that_spends_money_takes_the_passphrase_and_says_when_it_is_wrong(screen):
    """A yes that spends takes more than a click; a wrong passphrase says
    so under the field, which clears for another try."""
    backend = Backend([_card(7, "Pay the plumber $180?", spends=True)])
    page = _open(screen, backend)
    note = _note(page, "card:7")
    assert note.locator(".note-kicker").inner_text() == "Your passphrase"
    note.get_by_role("button", name="Yes").click()
    field = note.get_by_label("Your passphrase")
    field.fill("acorns")
    field.press("Enter")
    note.locator(".note-error").wait_for(timeout=5_000)
    assert note.locator(".note-error").inner_text() == "That isn’t your passphrase."
    assert field.input_value() == ""
    field.fill(PASSPHRASE)
    note.get_by_role("button", name="Yes").click()
    note.wait_for(state="detached", timeout=5_000)
    assert backend.posted[-1] == ("/actions/decide",
                                  {"action_id": 7, "approve": True, "passphrase": PASSPHRASE})


DRAFTS = ["Delete the draft “Saturday”?", "Delete the draft “Sunday”?",
          "Delete the draft “Monday”?"]


def _tick(note, item):
    # The drawn box lies over the real one and catches a click aimed at the
    # input; a person clicks the row, and so does this.
    note.locator(".form-check", has_text=item).click()


# Each ticked box's check, measured in the page: how far its line keeps
# from the inside of each of the box's edges.
_CHECKS = """(note) => [...note.querySelectorAll(".form-check-box")].map((box) => {
  const b = box.getBoundingClientRect();
  const edge = parseFloat(getComputedStyle(box).borderTopWidth);
  const c = box.querySelector(".form-check-mark path").getBoundingClientRect();
  const r = (n) => Math.round(n * 10) / 10;
  return {left: r(c.left - b.left - edge), top: r(c.top - b.top - edge),
          right: r(b.right - edge - c.right), bottom: r(b.bottom - edge - c.bottom)};
})"""


def test_each_ticked_box_has_its_check_inside_it_and_the_same_in_every_box(screen):
    """Until 19 October the check was a brush mark bigger than its box,
    set a little differently in each: beside the box's even line it looked
    like two drawings, and one set far up and to the right looked
    misplaced. Each check now keeps clear of every edge of its box, in the
    same place in every box, and stays put when it is ticked again."""
    backend = Backend([_card(8, "3 on one card: untick any to leave out", items=DRAFTS)])
    page = _open(screen, backend)
    note = _note(page, "card:8")
    checks = note.evaluate(_CHECKS)

    assert len(checks) == 3
    assert all(min(c.values()) >= 2 for c in checks), checks
    assert all(abs(c[side] - checks[0][side]) <= 0.2 for c in checks for side in c), checks

    _tick(note, DRAFTS[1])
    note.get_by_role("button", name="Yes to 2").wait_for()
    _tick(note, DRAFTS[1])
    note.get_by_role("button", name="Yes", exact=True).wait_for()
    again = note.evaluate(_CHECKS)
    # To the screen's rounding: the card's buttons change as a box is
    # unticked, and the boxes land a fraction of a pixel apart.
    assert all(abs(a[side] - c[side]) <= 0.2
               for a, c in zip(again, checks, strict=True) for side in c), (
        "a box's check moved when it was ticked again", checks, again)


def test_a_card_of_several_items_takes_a_yes_to_the_ones_left_ticked(screen):
    """A turn's cards are one card, approved whole or in part; its text says
    "some of them in the app", and the note showed the first item alone
    with a yes for all. Each item now has its box, all ticked; a yes with
    some unticked names the rest, and none ticked is no yes at all."""
    backend = Backend([_card(8, "3 on one card: untick any to leave out", items=DRAFTS)])
    page = _open(screen, backend)
    note = _note(page, "card:8")
    assert _texts(page, ".note-items li") == DRAFTS
    assert note.get_by_role("button", name="Yes", exact=True).is_visible()

    _tick(note, DRAFTS[1])
    note.get_by_role("button", name="Yes to 2").wait_for()
    assert not note.get_by_label(DRAFTS[1]).is_checked()
    for item in (DRAFTS[0], DRAFTS[2]):
        _tick(note, item)
    assert note.get_by_role("button", name="Yes to 0").is_disabled()

    _tick(note, DRAFTS[0])
    _tick(note, DRAFTS[2])
    note.get_by_role("button", name="Yes to 2").click()
    note.wait_for(state="detached", timeout=5_000)
    assert backend.posted[-1] == ("/actions/decide",
                                  {"action_id": 8, "approve": True, "only": [1, 3]})


def test_a_note_whose_card_grows_ticks_its_new_items_and_says_yes_to_what_was_left_ticked(
        screen):
    """A card read as one item and then joined by a second: the note's ticks
    were seeded once, from the first drawing, so the new item's box did
    nothing and the yes, naming no items, took both. The ticks follow the
    card, and the yes names the card as it was read."""
    backend = Backend([_card(9, DRAFTS[0], seen="aaaa1111")])
    page = _open(screen, backend)
    note = _note(page, "card:9")
    note.wait_for(timeout=10_000)
    backend.needs = [_card(9, "2 on one card: untick any to leave out", items=DRAFTS[:2],
                           seen="bbbb2222")]
    note.locator(".note-items").wait_for(timeout=10_000)
    _tick(note, DRAFTS[1])
    note.get_by_role("button", name="Yes to 1").click()
    note.wait_for(state="detached", timeout=5_000)
    assert backend.posted[-1] == ("/actions/decide", {
        "action_id": 9, "approve": True, "only": [1], "seen": "bbbb2222"})


SENDS = ["Send from your Mailbox account\nTo: iris@example.com\nSubject: Plot fees",
         "Send from your Mailbox account\nTo: theo@example.com\nSubject: Seed order"]


def test_a_cards_lines_are_lines_on_its_note_and_on_each_item(screen):
    """The note carries the card whole, as the phone does (agent/worker/
    needs.py): who an email reaches and what it is, a line each. Run
    together, a Bcc reads as part of the line before it."""
    detail = "To: dana@example.com\nBcc: sam@example.com\nSubject: Re: Saturday"
    backend = Backend([_card(5, "Send from your Mailbox account", detail=detail),
                       _card(8, "2 on one card: untick any to leave out", items=SENDS)])
    page = _open(screen, backend)
    _note(page, "card:5").wait_for(timeout=10_000)
    assert _note(page, "card:5").locator(".note-detail").inner_text() == detail
    assert _texts(page, ".note-items li") == SENDS


def test_a_grouped_card_that_spends_lists_its_items_to_approve_together(screen):
    """Its passphrase is bound to everything on it, so its items have no
    boxes, and its yes is to all of them."""
    backend = Backend([_card(9, "2 on one card, approved together", spends=True,
                             items=["Pay the plumber $180?", "Pay the painter $90?"])])
    page = _open(screen, backend)
    note = _note(page, "card:9")
    assert _texts(page, ".note-items li") == ["Pay the plumber $180?", "Pay the painter $90?"]
    assert note.locator("input[type=checkbox]").count() == 0
    note.get_by_role("button", name="Yes").click()
    note.get_by_label("Your passphrase").fill(PASSPHRASE)
    note.get_by_role("button", name="Yes").click()
    note.wait_for(state="detached", timeout=5_000)
    assert backend.posted[-1] == ("/actions/decide",
                                  {"action_id": 9, "approve": True, "passphrase": PASSPHRASE})


def test_a_reminder_going_off_is_done_or_put_off_to_a_time_the_user_picks(screen):
    backend = Backend([_reminder(40, "Call the landlord"), _reminder(41, "Water the ferns")])
    page = _open(screen, backend)
    page.locator(".notes-spread").dispatch_event("click")
    first = _note(page, "reminder:40")
    first.get_by_role("button", name="Later").click()
    assert _texts(page, ".note-later button") == [
        "in 15 minutes", "in an hour", "this evening", "tomorrow morning"]
    first.get_by_role("button", name="this evening").click()
    first.wait_for(state="detached", timeout=5_000)
    _note(page, "reminder:41").get_by_role("button", name="Done").click()
    _note(page, "reminder:41").wait_for(state="detached", timeout=5_000)
    assert backend.posted == [
        ("/control/needs/later", {"id": "reminder:40", "when": "evening"}),
        ("/control/needs/done", {"id": "reminder:41"})]


def test_a_stopped_note_opens_its_one_fix_or_is_put_away_for_now(screen):
    backend = Backend([_stopped("cap:spend:day", "Today's spending cap is reached.",
                                "raise_cap")])
    page = _open(screen, backend)
    note = _note(page, "cap:spend:day")
    assert note.locator(".note-kicker").inner_text() == "Stopped"
    note.get_by_role("button", name="Raise the cap").click()
    page.locator(".back-name", has_text="Spend").wait_for(timeout=5_000)
    assert backend.posted == []
    note.get_by_role("button", name="Not now").click()
    note.wait_for(state="detached", timeout=5_000)
    assert backend.posted == [("/control/needs/not-now", {"id": "cap:spend:day"})]


def test_more_than_one_note_piles_up_three_deep_and_spreads_out_when_clicked(screen):
    """Five notes in a column would push each other out of sight; a pile
    keeps the first readable and says how many more there are."""
    backend = Backend([_reminder(n, f"Reminder {n}") for n in range(1, 6)])
    page = _open(screen, backend)
    page.locator(".notes-pile").wait_for(timeout=10_000)
    assert page.locator(".notes-pile article.note").count() == 3
    assert page.locator(".notes-more").inner_text() == "+2"
    assert page.locator(".notes-pile article.note[inert]").count() == 2
    page.locator(".notes-more").click()
    page.locator(".notes-column").wait_for(timeout=5_000)
    assert _texts(page, ".notes-column .note-text") == [f"Reminder {n}" for n in range(1, 6)]
    assert page.locator("article.note[inert]").count() == 0


def test_a_narrow_window_folds_the_notes_into_one_strip_that_unfolds(screen):
    backend = Backend([_reminder(40, "Call the landlord"), _card(5, "Send the deck to Iris?")])
    page = _open(screen, backend, width=900)
    strip = page.locator(".notes-strip")
    strip.wait_for(timeout=10_000)
    assert re.sub(r"\s+", " ", strip.inner_text()) == "2 notes Call the landlord"
    assert page.locator("article.note").count() == 0
    assert not page.locator(".margin").is_visible()
    strip.click()
    assert _texts(page, ".day-column .note-text") == ["Call the landlord", "Send the deck to Iris?"]
    assert strip.get_attribute("aria-expanded") == "true"


def test_ctrl_j_goes_to_the_first_note_and_the_arrows_move_between_them(screen):
    """No single key answers a note: Ctrl+J reaches it, the arrows move,
    Tab goes through its answers, and Esc goes back to writing."""
    backend = Backend([_reminder(40, "Call the landlord"), _card(5, "Send the deck to Iris?")])
    page = _open(screen, backend)
    page.locator("article.note").first.wait_for(timeout=10_000)
    focused = "() => document.activeElement?.dataset?.need ?? document.activeElement?.tagName"
    page.keyboard.press("Control+j")
    _until(lambda: page.evaluate(focused) == "reminder:40", 5)
    page.keyboard.press("ArrowDown")
    _until(lambda: page.evaluate(focused) == "card:5", 5)
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement.textContent") == "Yes"
    page.keyboard.press("Escape")
    _until(lambda: page.evaluate(focused) == "TEXTAREA", 5)
    assert backend.posted == []


def test_a_new_note_is_announced_and_one_answered_elsewhere_peels_away(screen):
    """A note that comes while the user reads is said to a screen reader;
    one answered on the phone leaves the window too, not only the list."""
    backend = Backend([_card(5, "Send the deck to Iris?")])
    page = _open(screen, backend)
    _note(page, "card:5").wait_for(timeout=10_000)
    backend.needs = [_stopped("signal", "Signal is unlinked, so nothing reaches your phone.",
                              "link")]
    _note(page, "card:5").wait_for(state="detached", timeout=10_000)
    _note(page, "signal").wait_for(timeout=5_000)
    assert page.locator('.notes-said[aria-live="assertive"]').inner_text() == (
        "New note, stopped: Signal is unlinked, so nothing reaches your phone.")


def test_the_first_day_shows_a_sample_note_until_it_is_dismissed(screen):
    backend = Backend([], entries=[])
    page = _open(screen, backend)
    sample = _note(page, "app:sample")
    sample.wait_for(timeout=10_000)
    assert sample.locator(".note-kicker").inner_text() == "A note"
    sample.get_by_role("button", name="Done").click()
    sample.wait_for(state="detached", timeout=5_000)
    assert appsettings.current()["sample_note"] == "dismissed"


def test_the_backend_that_keeps_stopping_is_a_note_with_start_and_its_log(screen):
    """Only the app knows its backend keeps stopping: the backend can't
    say so. The note says what is lost, and starts it."""
    backend = Backend([])
    page = _open(screen, backend)
    page.evaluate("""() => {
      window.__STEVE_SHELL__.answers.backend_control = {
        state: "failing", pid: null, restarts: 5, damaged: null,
        log_tail: ["Traceback (most recent call last):", "ImportError: no module"]};
      window.__STEVE_SHELL__.emit("backend-failing", null);
    }""")
    note = _note(page, "app:backend")
    note.wait_for(timeout=10_000)
    assert note.locator(".note-text").inner_text() == (
        "Steve’s backend keeps stopping. Nothing runs until it starts.")
    note.get_by_role("button", name="Details").click()
    assert "ImportError" in note.locator(".note-log").inner_text()
    note.get_by_role("button", name="Start").click()
    _until(lambda: {"cmd": "backend_control", "args": {"action": "start"}} in page.evaluate(
        "() => window.__STEVE_SHELL__.calls"), 5)
