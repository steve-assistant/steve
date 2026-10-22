"""The saved logins' doctor shows what a login holds and never what its
cookies say, and its cookie-scope check runs with no network and builds
nothing.

On 7 October 2026 the investigation into their broken LinkedIn login could not
see the saved session -- decrypting it was refused to a model -- so this is
the look the user runs themselves (agent/browser/doctor.py). The conftest's
throwaway vault and a fake Docker stand in for theirs.
"""

from __future__ import annotations

import json
import subprocess
import time

from agent.browser import doctor
from core import images
from core.browser import sandbox
from core.browser import vault as vaults

LATER = time.time() + 30 * 86400


def _linkedin_with_twins():
    store = vaults.vault()
    store.save_site("linkedin", ["linkedin.com"], "free")
    store.save_state("linkedin", {
        "cookies": [
            {"name": "li_at", "value": "SECRET-TOKEN-1", "domain": "www.linkedin.com",
             "path": "/", "expires": LATER},
            {"name": "li_at", "value": "SECRET-TOKEN-2", "domain": ".www.linkedin.com",
             "path": "/", "expires": LATER},
            {"name": "NID", "value": "SECRET-GOOGLE", "domain": ".google.com", "path": "/",
             "expires": -1},
        ],
        "origins": [{"origin": "https://www.linkedin.com",
                     "localStorage": [{"name": "voyager-key", "value": "SECRET-STORAGE"}]}]})
    return store


def test_the_logins_doctor_shows_scope_and_twins_but_never_a_value():
    report = doctor.logins_report(_linkedin_with_twins())

    assert "linkedin: linkedin.com (free)" in report
    assert "3 cookies: 1 host-only, 2 in twins, 1 for other sites" in report
    assert "TWIN" in report and "other site" in report and "(1 items)" in report
    assert "SECRET" not in report and "voyager-key" not in report


def test_a_cookies_row_holds_no_value_whatever_the_state_holds():
    rows = doctor.describe({"cookies": [{"name": "s", "value": "SECRET",
                                         "domain": ".contoso.example",
                                         "expires": 1.0}]}, ["contoso.example"], now=2.0)

    assert rows == [{"name": "s", "domain": ".contoso.example", "path": "/", "host_only": False,
                     "expires": "1970-01-01 00:00", "expired": True, "belongs": True,
                     "twin": False}]


class _Docker:
    """Docker as the doctor sees it: the images it holds, by ID, and the
    reproduction's answer; every command it was given kept."""

    def __init__(self, held: tuple[str, ...], answer: list | None) -> None:
        self.held, self.answer = held, answer
        self.asked: list[list[str]] = []

    def __call__(self, args, timeout):
        self.asked.append(args)
        if args[:2] == ["image", "inspect"]:
            there = args[-1] in self.held
            return subprocess.CompletedProcess(args, 0 if there else 1,
                                               f"{args[-1]}\n" if there else "", "")
        out = "chromium noise\n" + json.dumps(self.answer) if self.answer else ""
        return subprocess.CompletedProcess(args, 0, out, "")


LOOPED = [{"imported_as": "www.example.test", "sid_left_on": ["www.example.test"],
           "outcome": "Page.goto: net::ERR_TOO_MANY_REDIRECTS at http://www.example.test:8080/"},
          {"imported_as": ".www.example.test", "sid_left_on": [],
           "outcome": "loaded: signed out cleanly"}]


def test_the_cookie_scope_check_runs_with_no_network_in_the_image_already_built():
    images.remember(sandbox.image("browser"), "sha256:built")
    docker = _Docker(("sha256:built",), LOOPED)

    said = doctor.cookie_scope(docker)

    assert said["ok"] and said["confirmed"] and said["image"] == "sha256:built"
    run = docker.asked[-1]
    assert run[:4] == ["run", "--rm", "--network", "none"] and "--read-only" in run
    assert not any(a[0] in ("build", "pull") for a in docker.asked), "it builds nothing"


def test_a_reproduction_that_did_not_loop_is_not_called_a_confirmation():
    both_loaded = [{**LOOPED[0], "outcome": "loaded: signed out cleanly"}, LOOPED[1]]

    images.remember(sandbox.image("browser"), "sha256:built")
    said = doctor.cookie_scope(_Docker(("sha256:built",), both_loaded))

    assert said["ok"] and not said["confirmed"]


def test_with_no_browser_image_the_check_says_how_to_build_one():
    """Nor is an image someone else put under the browser's tag one: the
    check runs only what Steve built (core/images.py)."""
    said = doctor.cookie_scope(_Docker(("sha256:someone-elses",), None))

    assert not said["ok"] and "core.browser setup" in said["error"]
