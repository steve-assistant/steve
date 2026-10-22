"""The walks' own waiting (tests/unit/ui/walk.py), and the rig's handing
them a page (rig.py `Desktop.page`), on fake locators and pages and a clock
of the test's: no app, no browser."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from tests.unit.ui import rig, walk


class _Waits:
    """A locator whose waits each run to their limit on `clock`, and whose
    wait number `found_on` finds the element at once."""

    def __init__(self, clock: list[float], found_on: int | None = None) -> None:
        self.clock, self.found_on, self.waits = clock, found_on, []

    def wait_for(self, timeout: float) -> None:
        self.waits.append(timeout)
        if len(self.waits) == self.found_on:
            return
        self.clock[0] += timeout / 1000
        raise PlaywrightTimeout(f"Locator.wait_for: Timeout {timeout:.0f}ms exceeded.\n"
                                "Call log:\n  - waiting for the heading to be visible")


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    now = [0.0]
    monkeypatch.setattr(walk, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def test_a_wait_stuck_on_one_reply_from_the_page_costs_a_few_seconds_not_the_walk(clock):
    """On CI a walk's first wait stuck for its whole 90 s with the heading
    on screen (22 October 2026). Made of short waits, the next one asks the
    page again and finds it."""
    heading = _Waits(clock, found_on=2)
    walk.appears(heading, walk.STARTED_MS)
    assert heading.waits == [walk.TRY_MS, walk.TRY_MS]


def test_a_wait_that_never_sees_its_element_ends_at_its_limit_and_says_how_many_waits_it_made(
        clock):
    """Every short wait failing is a page Playwright cannot query at all,
    which reads differently from one lost reply; the last wait takes only
    what is left of the limit, so the walk ends when it said it would."""
    heading = _Waits(clock)
    with pytest.raises(PlaywrightTimeout) as failed:
        walk.appears(heading, 10_000)
    assert heading.waits == [3_000, 3_000, 3_000, 1_000]
    assert clock[0] == 10.0
    assert "waiting for the heading to be visible" in failed.value.message
    assert failed.value.message.endswith("(in 4 waits of up to 3 s each)")


class _Page:
    """A window's page in which Playwright's first `unqueryable` looks for
    an element each wait out their limit, and the reloads made of it."""

    def __init__(self, url: str, unqueryable: int = 0) -> None:
        self.url, self.unqueryable, self.reloads, self.looks = url, unqueryable, 0, []

    def locator(self, selector: str):
        return SimpleNamespace(wait_for=lambda state, timeout: self._look(selector, timeout))

    def _look(self, selector: str, timeout: float) -> None:
        self.looks.append((selector, timeout))
        if len(self.looks) <= self.unqueryable:
            raise PlaywrightTimeout(f"Locator.wait_for: Timeout {timeout:.0f}ms exceeded.")

    def reload(self) -> None:
        self.reloads += 1


def _app(*pages: _Page) -> rig.Desktop:
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=list(pages))])
    return rig.Desktop(None, browser, Path("data"), Path("webview"), began=0.0)


def test_a_page_playwright_cannot_query_is_reloaded_once_and_one_it_can_is_left_alone():
    """Attached as a page's navigation commits, Playwright has no world of
    its own in the new document, and every wait for an element there runs
    out: on CI (22 October 2026) a walk's sign-in made 29 waits for a
    "Welcome back" its window showed. A reload makes the document again."""
    main = _Page(rig.APP_ORIGIN, unqueryable=1)
    quickbar = _Page(rig.APP_ORIGIN + "index.html#quickbar")
    app = _app(quickbar, main)
    with pytest.warns(UserWarning, match="the main page answered no query"):
        assert app.page() is main
    assert app.page("quickbar") is quickbar
    assert main.reloads == 1 and quickbar.reloads == 0
    assert len(main.looks) == 2 and len(quickbar.looks) == 1
    [said] = app.seen
    assert said.endswith(
        f"the main page answered no query in {rig.QUERYABLE_MS // 1000} s: reloaded")


def test_a_page_still_not_queryable_after_its_reload_fails_the_walk_there():
    """Rather than every wait after it running out its limit, each saying
    only that its element never came."""
    main = _Page(rig.APP_ORIGIN, unqueryable=2)
    with pytest.raises(PlaywrightTimeout), pytest.warns(UserWarning):
        _app(main).page()
    assert main.reloads == 1
