"""Whether a click on a site Steve is logged in to needs the user's yes is
Jev's call, asked through `core/decide.py` (`agent/toolset/browser.py`).

Jev is shown the element and where it is, never the page's text or anything
of theirs. A click it could not judge has no probability, which
`core/browser/clicks.py` takes as "it does something": a card, not a click.
The consent gate (tests/gate/test_browser_consent.py) drives the whole
click with this call faked; these pin the call itself.
"""

from __future__ import annotations

import pytest

from agent.toolset import browser
from core.browser import clicks
from tests.gate.decisions import install

BUTTON = {"tag": "button", "type": "submit", "label": "Delete course"}


def test_jev_is_asked_about_the_element_and_where_it_is_and_nothing_else(monkeypatch):
    jev = install(monkeypatch, does=0.7)

    said = browser._ask_jev("contoso", "My   courses\n| Contoso", BUTTON, "Delete course")

    assert said == (0.7, "")
    [asked] = jev.asked
    assert asked["questions"] == {"does": {"type": "noul", "instructions": clicks.JUDGE}}
    assert asked["state"] == ("Site: contoso\nPage title: My courses | Contoso\n"
                              "Element: a button (submit) labelled 'Delete course'")


@pytest.mark.parametrize("why", ["Jev down", "no answer", "no key"])
def test_a_click_jev_could_not_judge_has_no_probability_and_says_why(monkeypatch, why):
    """None is a card (`clicks.verdict`); the reason goes to
    `browser_judgements`, where the health check counts it."""
    from core import secrets

    jev = install(monkeypatch, down=RuntimeError("HTTP 503") if why == "Jev down" else None)
    if why == "no key":
        monkeypatch.setattr(secrets, "get", lambda name: None)

    said = browser._ask_jev("contoso", "Courses", BUTTON, "Delete course")

    assert said == (None, {"Jev down": "RuntimeError: HTTP 503", "no answer": "does: not answered",
                           "no key": "no OpenRouter key"}[why])
    assert len(jev.asked) == (0 if why == "no key" else 1)
    assert clicks.verdict("judge", said[0]) == "card"
