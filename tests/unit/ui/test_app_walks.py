"""Three walks beside the exit walk, on the real app and backend.

The exit walk takes a stranger through setup to a first answer and a first
yes. These take someone already set up through what then comes to them
(docs/plan/10-end-to-end.md, section 8): a card answered from each window
that answers one, a day arriving with its reminder and its briefing, and
the spending cap, said in a reply and fixed from its note. Each starts the
real app (`rig.desktop_app`) on a fresh data folder, seeded before the
start with what the walk needs: the backend migrates the same database and
finds it there.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta

import pytest

from agent import approvals, reminders
from core import db, knobs, paths, spend
from core.consent import classify
from tests.fakes.openai import ROUTINE_SET
from tests.unit.ui import rig, walk

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("app")]

BUY = {"amount_usd": 48.0, "merchant": "Zinc", "item": "a table for two"}

# What the shell tells the quick bar as it shows it (lib.rs `place_quickbar`).
SHOWN = """() => window.__TAURI_INTERNALS__.invoke(
  "plugin:event|emit", {event: "quickbar-shown", payload: null})"""


def _seeded():
    """The app's database, made before the app starts it."""
    return contextlib.closing(db.open_store(paths.db_path()))


def _note(page, words: str):
    """A note on the day's page, by its words. A window narrower than the
    notes' column folds them into a strip, opened first, as the user would.
    The strip comes only with a note, so the two are waited for together:
    looked for at once, after the reply, the strip wasn't there yet, and
    the note came folded into it."""
    strip = page.locator(".notes-strip")
    note = page.locator("article.note", has_text=words)
    walk.until(lambda: note.is_visible() or strip.is_visible())
    if not note.is_visible():
        strip.click()
    note.wait_for(timeout=walk.WAIT_MS)
    return note


def test_a_spending_card_takes_the_passphrase_in_its_note_and_another_a_yes_in_the_quick_bar(
        playwright, tmp_path, monkeypatch):
    """A card that spends money is answered in its note with the
    passphrase; a plain one with a Yes in the quick bar, which sends the
    other to the app. Each window asks the same backend, so an answer in
    one is gone from the other.

    The quick bar's page is driven where it is, its window never shown.
    Showing it takes the foreground (lib.rs `show_quickbar`), which Windows
    refuses an app a test started, and tao then presses Alt to take it
    anyway, into whatever the developer is using. The walk tells the bar
    it was shown, as the shell does."""
    walk.set_up_already()
    with _seeded() as conn:
        _, made = approvals.queue(conn, tool="purchase", args=dict(BUY),
                                  preview="Buy a table for two from Zinc: $48.00",
                                  consent=classify("purchase", BUY))
    assert made is not None and made.high_risk

    with walk.fake_openai(monkeypatch), rig.desktop_app(tmp_path, playwright) as app:
        page = app.page()
        walk.sign_in(page)
        page.locator("nav.sidebar").wait_for(timeout=walk.WAIT_MS)
        walk.send(page, "Every Monday at 8, email me a plan for the week")
        page.locator(".kind-reply .prose", has_text=ROUTINE_SET).wait_for(timeout=walk.WAIT_MS)

        bar = app.page("quickbar")
        bar.evaluate(SHOWN)
        routine = bar.locator(".quickbar-note", has_text="The week ahead")
        bought = bar.locator(".quickbar-note", has_text="a table for two")
        routine.wait_for(timeout=walk.WAIT_MS)
        # A passphrase's yes is the app's: the bar offers to open it instead.
        assert bought.get_by_role("button", name="Open Steve").count() == 1
        assert bought.get_by_role("button", name="Yes").count() == 0
        routine.get_by_role("button", name="Yes").click()
        routine.wait_for(state="detached", timeout=walk.WAIT_MS)

        note = _note(page, "a table for two")
        note.get_by_role("button", name="Yes").click()
        note.get_by_label("Your passphrase").fill(walk.PASSPHRASE)
        note.get_by_role("button", name="Yes").click()
        note.wait_for(state="detached", timeout=walk.WAIT_MS)
        page.locator("article.note", has_text="The week ahead").wait_for(
            state="detached", timeout=walk.WAIT_MS)
        assert page.locator(".kind-action", has_text="You said yes").count() >= 1


def test_a_reminder_goes_off_and_a_briefing_comes_on_the_days_page(
        playwright, tmp_path, monkeypatch):
    """What the day brings without being asked: a reminder going off, on
    the day's page and as a note the user marks done, and the morning's
    briefing on the page.

    The briefing makes no note: it comes as a toast, which these tests
    leave to the shell (lib.rs `notify`, toast.rs)."""
    walk.set_up_already()
    now = datetime.now()
    # The morning's hour just gone, and the quiet hours moved past now, so
    # the briefing goes as the worker starts (tests/e2e/backend.py `briefed`).
    morning = max(now - timedelta(minutes=1), now.replace(hour=0, minute=0))
    knobs.write({"MORNING": morning.strftime("%H:%M"),
                 "QUIET_FROM": (now + timedelta(hours=1)).strftime("%H:%M"),
                 "QUIET_UNTIL": (now + timedelta(hours=9)).strftime("%H:%M")})
    # Booked a while ago and due a minute ago: it goes off at the first tick.
    with _seeded() as conn:
        reminders.schedule(conn, kind="remind", text="Water the plants",
                           when=(now - timedelta(minutes=1)).isoformat(timespec="seconds"),
                           now=now - timedelta(minutes=5))

    with walk.fake_openai(monkeypatch), rig.desktop_app(tmp_path, playwright) as app:
        page = app.page()
        walk.sign_in(page)
        page.locator("nav.sidebar").wait_for(timeout=walk.WAIT_MS)

        page.locator(".briefing").wait_for(timeout=walk.WAIT_MS)
        page.locator(".kind-update", has_text="Water the plants").wait_for(timeout=walk.WAIT_MS)
        note = _note(page, "Water the plants")
        note.get_by_role("button", name="Done").click()
        note.wait_for(state="detached", timeout=walk.WAIT_MS)


def test_at_the_cap_the_reply_says_so_and_its_notes_fix_opens_spend(
        playwright, tmp_path, monkeypatch):
    """The month's cap reached: a message is answered with what stopped
    and where to change it, unasked of any model, and the note's fix opens
    Spend on the cap and its notice."""
    walk.set_up_already()
    knobs.write({"MONTH_USD": 5})
    with _seeded() as conn, db.transaction(conn):
        spend.bill(conn, 5.0, model="gpt-5-mini", unit=spend.Unit("chat"))

    with walk.fake_openai(monkeypatch) as fake, rig.desktop_app(tmp_path, playwright) as app:
        page = app.page()
        walk.sign_in(page)
        page.locator("nav.sidebar").wait_for(timeout=walk.WAIT_MS)
        walk.send(page, "What's on my calendar tomorrow?")
        page.locator(".kind-reply .prose", has_text="I've reached this month's $5.00 limit"
                     ).wait_for(timeout=walk.WAIT_MS)

        note = _note(page, "$5.00")
        note.get_by_role("button", name="Raise the cap").click()
        # The month's stop, beside the warning the seeded bill took chat's
        # own cap past.
        page.locator(".cap-notice.stopped", has_text="$5.00 monthly limit").wait_for(
            timeout=walk.WAIT_MS)
        page.locator('main [data-knob="MONTH_USD"]').wait_for(timeout=walk.WAIT_MS)

    assert not [body for body in fake.conversations if body.get("stream")]
