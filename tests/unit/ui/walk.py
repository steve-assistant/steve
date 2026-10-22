"""What the walks on the real app share: a user already set up, the fake
OpenAI in place of OpenAI, the sign-in, a message sent, and how long each
wait may take (test_exit_walk.py, test_app_walks.py)."""

from __future__ import annotations

import contextlib
import time
from datetime import UTC, datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from agent import sqltime
from agent.worker import setup
from core import knobs, passphrase, settingsfile
from tests.fakes.openai import FakeOpenAI

WAIT_MS = 30_000
# What the app shows first waits on its backend, whose first start imports
# everything: about ten seconds alone. Under the full suite's load it took
# over thirty, and the walk failed on setup's "Starting Steve…" (18
# October). A process that must start gets a generous limit, free when it
# starts at once; but short of the check's limit for a whole test
# (`devtools.stuck.STUCK_S`, two minutes), which stops a test without the
# app's story (`rig.Desktop.story`). At 120 s, a walk waiting on a page
# that never showed the sign-in was stopped there, and said nothing.
STARTED_MS = 90_000
# A wait on the app is made of short ones. On CI (22 October 2026), in about
# one walk of twenty-five, a page's first `wait_for` stuck for its whole
# limit: its call log never passed its first line, while the page showed
# the heading and a `count()` just after found it. Playwright's wait polls
# the page inside one call, so a reply from the page that never comes holds
# that call to its end; each short wait asks the page afresh.
TRY_MS = 3_000
PASSPHRASE = "blue kettle at dawn"


def appears(locator, within_ms: float = WAIT_MS) -> None:
    """`locator.wait_for()`, in waits of at most `TRY_MS`, for `within_ms`
    in all. Failing, the timeout says how many waits were made: every one
    of them stuck is a page Playwright cannot query at all, not one lost
    reply."""
    deadline = time.monotonic() + within_ms / 1000
    tries = 0
    while True:
        tries += 1
        left_ms = (deadline - time.monotonic()) * 1000
        try:
            locator.wait_for(timeout=max(1.0, min(TRY_MS, left_ms)))
            return
        except PlaywrightTimeout as timed_out:
            if time.monotonic() >= deadline:
                raise PlaywrightTimeout(f"{timed_out.message}\n(in {tries} waits of up to"
                                        f" {TRY_MS / 1000:.0f} s each)") from None


def until(check, within: float = 30.0, said=None):
    """Waits for `check`; on the deadline, says what `said` reads then: a
    bare timeout on CI left only a guess at what had been seen."""
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time" + (f": {said()}" if said else ""))
        time.sleep(0.1)


@contextlib.contextmanager
def fake_openai(monkeypatch):
    """The fake OpenAI, and nothing to spend at night. The app runs on the
    real clock: run in the night's window, its backend's free time put its
    own turns beside the walk's, and its prompt on the day, once the fake
    answered them (tests/unit/test_night.py tests the night)."""
    knobs.write({"NIGHT_USD": 0})
    fake = FakeOpenAI()
    with fake.running() as url:
        monkeypatch.setenv("STEVE_OPENAI_URL", url)
        yield fake


def set_up_already() -> None:
    """Setup done, as the exit walk leaves it, passphrase and all, so the
    app opens on the sign-in and then the day."""
    settingsfile.save(setup.path(), {"answered": {"hello": "seen", "preset": "balanced",
                                                   "login": "off"},
                                     "done_at": sqltime.stamp(datetime.now(UTC))}, setup.STEPS)
    knobs.write({"MONTH_USD": 30})
    assert passphrase.choose(PASSPHRASE) is None


def sign_in(page) -> None:
    """The sign-in, with the passphrase `set_up_already` chose. The shell
    holds the session in memory only: each start of the app asks again."""
    appears(page.get_by_role("heading", name="Welcome back"), STARTED_MS)
    page.get_by_label("Your passphrase").fill(PASSPHRASE)
    page.get_by_role("button", name="Sign in", exact=True).click()


def send(page, text: str) -> None:
    box = page.locator(".composer textarea")
    box.fill(text)
    box.press("Enter")
