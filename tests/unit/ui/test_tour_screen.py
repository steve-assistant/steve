"""The tour: six notes after setup, each beside what it explains.

On 18 October a review of the running app found Steve's best work out of
sight: the hotkey, the desk card, the extensions Steve builds itself and
the switches that let it browse and run code were each a thing to know
about already (ui/src/Tour.tsx). These drive the built window in Edge
against the day page's backend, with the app's settings the worker's own.
"""

from __future__ import annotations

import pytest

from agent import appsettings
from tests.unit.ui.test_day_screen import Backend, _open, _until

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

PLACES = {"ok": True, "coming": 1, "calls": 0,
          "extensions": {"installed": 1, "building": 0, "can_build": True}}


def _backend() -> Backend:
    backend = Backend(entries=[])
    backend.places = dict(PLACES)
    return backend


def _box(page, selector: str) -> dict:
    return page.locator(selector).first.bounding_box()


def _encloses(ring: dict, inner: dict) -> bool:
    return (ring["x"] <= inner["x"] and ring["y"] <= inner["y"]
            and ring["x"] + ring["width"] >= inner["x"] + inner["width"]
            and ring["y"] + ring["height"] >= inner["y"] + inner["height"])


def _apart(a: dict, b: dict) -> bool:
    return (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
            or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


# Each stop, by what its note points at; None is the desk card, which is in
# the screen's corner, not this window.
STOPS = [".day-sheet .day-head", ".margin", ".composer", None,
         ".places .place >> nth=1", ".who"]


def test_a_tour_due_shows_six_notes_each_beside_what_it_explains_and_then_is_not_due(screen):
    """Each note sits beside its thing, never on it, with a ring round it:
    a note pointing at nothing in particular says nothing about where to
    look. Done, the tour is not due again, so it shows once per install."""
    appsettings.write({"tour": "due"})
    backend = _backend()
    page = _open(screen, backend)
    page.set_viewport_size({"width": 1280, "height": 800})
    note = page.locator(".tour-note")
    _until(lambda: _places_loaded(page))

    for n, target in enumerate(STOPS, start=1):
        _until(lambda n=n: page.locator(f".tour[data-stop='{n}']").count() == 1)
        _until(lambda: note.evaluate("el => el.style.visibility") == "visible")
        assert note.locator(".tour-count").inner_text() == f"{n} of 6"
        if target is None:
            box, view = note.bounding_box(), page.viewport_size
            assert page.locator(".tour-ring").count() == 0
            assert box["x"] + box["width"] > view["width"] - 60, "the corner the card is in"
            assert box["y"] + box["height"] > view["height"] - 60
        else:
            _until(lambda target=target: _encloses(_box(page, ".tour-ring"), _box(page, target)))
            assert _apart(note.bounding_box(), _box(page, target)), f"stop {n} covers its thing"
        page.get_by_role("button", name="Done" if n == 6 else "Next").click()

    _until(lambda: page.locator(".tour").count() == 0)
    _until(lambda: appsettings.current()["tour"] == "off")
    page.reload()
    page.locator(".day-sheet").first.wait_for(timeout=15_000)
    page.wait_for_timeout(500)
    assert page.locator(".tour").count() == 0


def _places_loaded(page) -> bool:
    return page.locator(".places .place").count() >= 2


def test_the_tour_is_skipped_by_its_button_or_esc_and_shown_again_from_the_names_menu(screen):
    """Under two minutes is still too long for someone who knows the app:
    "Skip the tour" ends it at the first note. "Show me around" brings it
    back from the name's menu, and Esc ends it there too."""
    appsettings.write({"tour": "due"})
    page = _open(screen, _backend())
    page.get_by_role("button", name="Skip the tour").click()
    _until(lambda: page.locator(".tour").count() == 0)
    _until(lambda: appsettings.current()["tour"] == "off")

    page.locator(".who").click()
    page.get_by_role("menuitem", name="Show me around").click()
    _until(lambda: page.locator(".tour[data-stop='1']").count() == 1)
    page.get_by_role("button", name="Next").click()
    _until(lambda: page.locator(".tour[data-stop='2']").count() == 1)
    page.keyboard.press("Escape")
    _until(lambda: page.locator(".tour").count() == 0)
    assert appsettings.current()["tour"] == "off"


def test_no_tour_shows_unless_one_is_due(screen):
    """Every start after the first would otherwise open on six notes."""
    page = _open(screen, _backend())
    _until(lambda: _places_loaded(page))
    page.wait_for_timeout(500)
    assert page.locator(".tour").count() == 0
