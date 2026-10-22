"""The walks' own waiting (tests/unit/ui/walk.py), on a fake locator and a
clock of the test's: no app, no browser."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from tests.unit.ui import walk


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
