"""The exit walk: a stranger's first minutes, on the real app and backend.

Every screen test before this one drove the shipped pages against a small
backend written for it, so the pages, the worker and the app's shell had
only ever been tested apart. A route renamed on one side, a setting written
where the other never reads it, or a worker that can't answer a first
message on a fresh data folder would each pass every test and fail the
first person to install Steve. These start the real app (`rig.desktop_app`)
on a fresh data folder, with the fake OpenAI standing in for OpenAI.

The test fences' keychain gives the worker a stand-in OpenAI key, so the
walk keeps it on the key page with one press, as someone whose key an
earlier install left would; pasting and checking one is walked in
test_setup_screen.py against the same fake.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import re

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from core import knobs
from tests.fakes.openai import REPLY, ROUTINE_SET
from tests.unit.ui import rig
from tests.unit.ui.walk import (
    STARTED_MS,
    WAIT_MS,
    appears,
    fake_openai,
    send,
    set_up_already,
    sign_in,
    until,
)

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("app")]


def _on(page, question: str, within_ms: int = WAIT_MS) -> None:
    """On that page of setup; if not, the failure says which page it is on
    and what it says is wrong, not only that a heading never came."""
    heading = page.get_by_role("heading", name=question)
    try:
        appears(heading, within_ms)
    except PlaywrightTimeout as timed_out:
        shown = page.locator("h1, h2, [role=alert]").all_inner_texts()
        # The page's own script reads `shown`; Playwright's queries run
        # apart from it, and this says whether they can see the heading.
        # `is_visible`, not `count`, which waits without a limit.
        try:
            seen = heading.is_visible()
        except PlaywrightTimeout:
            seen = "no answer"
        raise AssertionError(f"not on {question!r}; the page shows {shown}; the heading"
                             f" visible to Playwright now: {seen}\n{timed_out.message}") from None


def _past_hello(page) -> None:
    """Where this laptop has Windows Hello, setup offers it after the
    passphrase. The walk says not now: a real Hello prompt would open on
    the desktop, and it is the user's to answer."""
    offered = page.get_by_role("heading", name="Use Windows Hello too")
    offered.or_(page.get_by_role("heading", name="Answer Steve from your phone")).wait_for(
        timeout=WAIT_MS)
    if offered.count():
        page.get_by_role("button", name="Not now").click()


def test_a_stranger_goes_from_a_fresh_install_to_a_first_answer_and_a_first_yes(
        playwright, tmp_path, monkeypatch):
    """Setup with the channel and mail skipped; a first message answered;
    a first card, answered Yes; and both on today's page."""
    with fake_openai(monkeypatch) as fake, rig.desktop_app(tmp_path, playwright) as app:
        page = app.page()
        _on(page, "I'm Steve. Let's get me working.", STARTED_MS)
        page.get_by_role("button", name="Begin").click()

        _on(page, "Your OpenAI key")
        page.get_by_role("button", name="Keep this key").click()

        _on(page, "How much may Steve spend a month?")
        # The name step reads the profile kept so far as it opens, and puts it
        # in the form when it comes, over anything typed before then
        # (ui/src/Setup.tsx `Name`). On CI's slow backend the walk typed
        # first and its name was wiped (22 October 2026), so it waits.
        with page.expect_request_finished(
                lambda r: r.method == "GET" and r.url.endswith("/control/profile")):
            page.get_by_role("button", name="Continue").click()

        _on(page, "What should Steve call you?")
        page.get_by_label("Your name").fill("Maya Lindqvist")
        page.get_by_role("button", name="Continue").click()

        _on(page, "Choose a passphrase")
        page.get_by_placeholder("A new passphrase, 8 characters or more").fill(
            "blue kettle at dawn")
        page.get_by_placeholder("The same again").fill("blue kettle at dawn")
        page.locator(".setup-panel button.primary").click()

        _past_hello(page)
        _on(page, "Answer Steve from your phone")
        page.get_by_role("button", name="Skip: answer only at this laptop").click()

        _on(page, "Your mail and calendar")
        page.get_by_role("button", name="Skip for now").click()

        # The backend reads this laptop's Docker Desktop by its program and
        # its pipe, asking it nothing; skipped whatever it finds.
        _on(page, "Let Steve browse and run code?")
        page.get_by_role("button", name="Skip for now").click()

        _on(page, "How much should Steve do unasked?")
        page.get_by_role("button", name="Continue").click()

        # An app on a data folder of its own has no say over Windows' one
        # login entry for Steve (lib.rs `run`): the page says so.
        _on(page, "Start Steve with Windows?")
        page.get_by_text("can't start with Windows").wait_for()
        page.get_by_role("button", name="Continue").click()

        _on(page, "Before you start")
        page.get_by_role("button", name="Start").click()
        page.locator("nav.sidebar").wait_for(timeout=WAIT_MS)
        # The tour follows setup; a stranger in a hurry skips it.
        page.get_by_role("button", name="Skip the tour").click()
        page.locator(".tour").wait_for(state="detached", timeout=WAIT_MS)

        # The desk card, wanted now that setup is done, in a window that
        # showing never activates; on the screen while Windows says it is
        # free, and hidden while it is locked or a full-screen app is in
        # front (src/deskcard.rs).
        card = app.page("deskcard")
        until(lambda: card.locator(".desk").get_attribute("data-wanted") == "yes")
        until(lambda: rig.window(app.process.pid, "Steve desk card", visible=False) != 0)
        hwnd = rig.window(app.process.pid, "Steve desk card", visible=False)
        assert user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_NOACTIVATE
        until(lambda: bool(user32.IsWindowVisible(hwnd)) == rig.screen_free())

        send(page, "Hello Steve")
        page.locator(".kind-reply .prose", has_text=REPLY).wait_for(timeout=WAIT_MS)

        send(page, "Every Monday at 8, email me a plan for the week")
        page.locator(".kind-reply .prose", has_text=ROUTINE_SET).wait_for(timeout=WAIT_MS)
        # What the tool told the model, as the fake was sent it: a routine
        # set up and waiting on the user's yes, not a refusal.
        told = [message["content"] for body in fake.conversations
                for message in body.get("messages", []) if message.get("role") == "tool"]
        assert told and '"approval"' in told[-1], told
        # A window narrower than the notes' column folds them into a strip
        # ("1 note"), opened first, as the user would.
        page.locator(".sidebar .day-needs", has_text="1").wait_for(timeout=WAIT_MS)
        strip = page.locator(".notes-strip")
        if strip.is_visible():
            strip.click()
        # By its words, not its place: the sample note may still be on its
        # way out, and a night begun in the walk adds its own.
        note = page.locator("article.note", has_text="The week ahead")
        note.wait_for(timeout=WAIT_MS)
        note.get_by_role("button", name="Yes").click()
        note.wait_for(state="detached", timeout=WAIT_MS)

        page.locator(".kind-action", has_text="You said yes").wait_for(timeout=WAIT_MS)
        said = page.locator(".kind-said .slip").all_inner_texts()
        assert said[:2] == ["Hello Steve", "Every Monday at 8, email me a plan for the week"]

    # The three turns were the app's, streamed to the fake: two messages, and
    # the answer to the routine's tool call. A worker started in the night's
    # window also runs the night, which asks the fake too.
    turns = [body for body in fake.conversations if body.get("stream")]
    assert len(turns) >= 3


# Settings ------------------------------------------------------------------

# The controls this test leaves alone, and why:
# - starts_at_login: Windows' Run key, not Steve's; an app on a data folder
#   of its own has no say over it, and doesn't show it.
# - tap: not a setting but a recording, of what the laptop plays.
# - update: a button that looks now, not a choice.
LEFT = {"starts_at_login", "tap", "update"}
# Switched on, these act on Windows or Docker at once (a scheduled task, the
# laptop kept awake, a browser's container); they are knobs like every other,
# saved by the same route, so only turning one off is tried here.
ONLY_OFF = {name for name, knob in knobs.KNOBS.items() if knob.group == "features"}

READ = """(root) => Object.fromEntries(
  [...root.querySelectorAll("[data-knob], [data-setting]")].map((el) => {
    const box = el.querySelector("input[type=checkbox]");
    const radio = el.querySelector("[role=radio][aria-checked=true]");
    const select = el.querySelector("select");
    const typed = [...el.querySelectorAll("input:not([type=checkbox])")].map((i) => i.value);
    return [el.dataset.knob || el.dataset.setting,
            box ? String(box.checked) : radio ? radio.textContent
              : select ? select.value : typed.join(" ")];
  }))"""


def _read(page) -> dict[str, str]:
    values = page.locator("main").evaluate(READ)
    hotkey = page.get_by_label("The hotkey")
    if hotkey.count():
        values["hotkey"] = hotkey.input_value()
    return {name: value for name, value in values.items() if name not in LEFT}


def _move(page, name: str) -> None:
    """One control moved off where it is, by the user's own means."""
    if name == "hotkey":
        field = page.get_by_label("The hotkey")
        options = field.locator("option").evaluate_all("os => os.map((o) => o.value)")
        field.select_option(next(o for o in options if o and o != field.input_value()))
        page.locator("section", has=field).locator(".form-saved").wait_for(timeout=WAIT_MS)
        return
    field = page.locator(f'[data-knob="{name}"], [data-setting="{name}"]')
    saved = field.locator(".form-saved")
    if field.locator("input[type=checkbox]").count():
        field.locator(".form-check").click()
    elif field.get_by_role("radio").count():
        field.locator("[role=radio][aria-checked=false]").first.click()
    elif field.locator("select").count():
        select = field.locator("select")
        options = select.locator("option").evaluate_all("os => os.map((o) => o.value)")
        select.select_option(next(o for o in options if o != select.input_value()))
    else:
        more = field.get_by_role("button", name=re.compile("^More: "))
        (more if more.is_enabled() else field.get_by_role("button", name=re.compile(
            "^Less: "))).click()
    saved.wait_for(timeout=WAIT_MS)


def _page(page, name: str) -> None:
    page.locator(".who").click()
    page.get_by_role("menuitem", name=name, exact=True).click()
    # The knobs, which come in a request of their own after the app's own
    # settings are drawn. Waiting for the first control of either kind, a
    # restarted backend's slow first answer read Settings without its
    # language, region and units.
    page.locator("main [data-knob]").first.wait_for(timeout=WAIT_MS)


user32 = ctypes.WinDLL("user32", use_last_error=True)
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000


def _size(page) -> tuple[int, int]:
    return tuple(page.evaluate("[window.innerWidth, window.innerHeight]"))


def test_every_setting_and_the_windows_size_survive_a_restart_of_the_app_and_backend(
        playwright, tmp_path, monkeypatch):
    """Each control on Settings and What Steve may do is moved through the
    page, and the window resized; after the app and its backend are ended
    and started again, each reads back as it was left."""
    set_up_already()
    pages = ("Settings", "What Steve may do")
    with fake_openai(monkeypatch), rig.desktop_app(tmp_path, playwright) as app:
        page = app.page()
        sign_in(page)
        page.locator("nav.sidebar").wait_for(timeout=WAIT_MS)
        before, moved = {}, {}
        for name in pages:
            _page(page, name)
            before[name] = _read(page)
            for setting, value in before[name].items():
                if setting in ONLY_OFF and value == "false":
                    continue
                _move(page, setting)
            moved[name] = _read(page)

        # Resized by Windows, as a drag would, not by the app's own call, to
        # a size that fits the screen: the app fits a kept size to the work
        # area as it starts, and on CI's 1024 by 720 one a window left 781
        # tall came back 720 (22 October 2026).
        opened = _size(page)
        hwnd = rig.window(app.process.pid)
        wide, tall = _work_area()
        until(lambda: user32.SetWindowPos(hwnd, 0, 0, 0, min(1000, wide), min(820, tall),
                                          0x0002 | 0x0004 | 0x0010))
        # Kept in logical pixels, a fraction at some scales; the page's are whole.
        until(lambda: _size(page)[0] < opened[0] and _kept_width(app) is not None
               and abs(_kept_width(app) - _size(page)[0]) < 1,
              said=lambda: _sizes(app, page))
        sized = _size(page)

    with fake_openai(monkeypatch), rig.desktop_app(tmp_path, playwright) as app:
        page = app.page()
        # Not still signed in: the session was the ended app's, in its memory.
        sign_in(page)
        page.locator("nav.sidebar").wait_for(timeout=WAIT_MS)
        until(lambda: all(abs(a - b) <= 1 for a, b in zip(_size(page), sized, strict=True)),
              said=lambda: f"left at {sized}; {_sizes(app, page)}")
        for name in pages:
            _page(page, name)
            assert _read(page) == moved[name], name

    for name in pages:
        unmoved = {k for k, v in moved[name].items()
                   if v == before[name][k] and k not in ONLY_OFF}
        assert not unmoved, (name, unmoved)


def _sizes(app, page) -> str:
    """The sizes a failed wait saw: the page's, the one kept, and the
    screen's work area, to which the app fits a kept size as it starts
    (ui/src-tauri/src/windowsize.rs `read`). On CI (22 October 2026) the
    restarted window's size missed the one left, and the wait said only
    that it had timed out."""
    try:
        kept = (app.data / "window.json").read_text(encoding="utf-8")
    except OSError:
        kept = "nothing"
    return f"the page {_size(page)}, kept {kept}, the work area {_work_area()}"


def _work_area() -> tuple[int, int]:
    """The screen's work area, its width and height, in this process's
    pixels, as SetWindowPos takes them from it."""
    area = (ctypes.c_long * 4)()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)  # SPI_GETWORKAREA
    return area[2] - area[0], area[3] - area[1]


def _kept_width(app) -> float | None:
    """The width the app kept in its data folder, in logical pixels."""
    with contextlib.suppress(OSError, ValueError, KeyError):
        return json.loads((app.data / "window.json").read_text(encoding="utf-8"))["width"]
    return None
