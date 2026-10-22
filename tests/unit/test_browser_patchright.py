"""The sandbox browser is driven by Patchright where the image has it, and
by Playwright where it does not, with nothing else about it changed.

On 7 October 2026 LinkedIn answered their replayed login with redirect loops, a
blank /feed/ and HTTP 999 while its PerimeterX script fingerprinted the
browser: stock Playwright talks to a page through `Runtime.enable`, which a
page can detect. Patchright is the same API without that. Three things have
to stay true when the library underneath changes: an image without Patchright
(every image before 8 October) keeps working; the two flags Patchright
documents leaving out are not passed back by hand; and the driver asks for
nothing only one of the two libraries understands (`isolated_context`), nor
for a persistent profile, which would keep their session on disk in the clear
where the vault holds it under DPAPI. What a real Chromium does with it is the
doctor's (`python -m core.browser check`), which needs the rebuilt image.
"""

from __future__ import annotations

import ast
import re
import sys
import types
from pathlib import Path

import pytest

from core.browser import driver, sandbox

DRIVER_SOURCE = Path(driver.__file__)


def _library(monkeypatch, name: str, present: bool) -> object:
    """A fake `<name>.async_api` that is there, or an import that fails."""
    marker = object()
    if present:
        package = types.ModuleType(name)
        api = types.ModuleType(f"{name}.async_api")
        api.async_playwright = marker
        package.async_api = api
        monkeypatch.setitem(sys.modules, name, package)
        monkeypatch.setitem(sys.modules, f"{name}.async_api", api)
    else:
        monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.setitem(sys.modules, f"{name}.async_api", None)
    return marker


def test_patchright_is_used_when_the_image_has_it(monkeypatch):
    patchright = _library(monkeypatch, "patchright", present=True)
    _library(monkeypatch, "playwright", present=True)

    assert driver.load_engine() == (patchright, "patchright")


def test_an_image_without_patchright_still_runs_on_playwright(monkeypatch):
    """Every image built before 8 October 2026 has only Playwright, and the
    driver in a new checkout is copied into the image it is built with --
    but a half-built or older one must not stop the browser."""
    _library(monkeypatch, "patchright", present=False)
    playwright = _library(monkeypatch, "playwright", present=True)

    assert driver.load_engine() == (playwright, "playwright")


def test_the_flags_patchright_leaves_out_are_not_passed_back_by_hand():
    """Its README: `--disable-component-update` and `--disable-default-apps`
    are removed from Playwright's defaults "to avoid detection as a Stealth
    Driver"; adding them again would undo it. Everything else, the automation
    flag and the WebRTC policy included, stays."""
    kept = driver.launch_args("patchright")
    whole = driver.launch_args("playwright")

    assert not set(driver.PATCHRIGHT_DROPS) & set(kept)
    assert set(driver.PATCHRIGHT_DROPS) <= set(whole)
    assert set(whole) - set(kept) == set(driver.PATCHRIGHT_DROPS)
    assert "--disable-blink-features=AutomationControlled" in kept
    assert "--disable-background-networking" in kept
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in kept


class _Browser:
    version = "153.0.8010.12"

    async def new_context(self, **kwargs):
        return _Context(kwargs)


class _Context:
    def __init__(self, made: dict) -> None:
        self.made = made
        self.routed: list = []

    async def route(self, pattern, handler):
        self.routed.append(pattern)

    async def new_page(self):
        return _Page()

    def on(self, event, handler):
        return None


class _Page:
    def on(self, event, handler):
        return None


class _Chromium:
    def __init__(self) -> None:
        self.launched: list[dict] = []

    async def launch(self, **kwargs):
        self.launched.append(kwargs)
        return _Browser()


@pytest.mark.parametrize("engine", ["patchright", "playwright"])
async def test_the_start_says_which_library_drove_it_and_launches_with_its_flags(engine):
    chromium = _Chromium()
    d = driver.Driver(type("PW", (), {"chromium": chromium})(), engine)

    said = await d.cmd_start({"proxy": "http://172.18.0.2:3128"})

    assert said == {"ok": True, "engine": engine}
    launched = chromium.launched[0]
    assert launched["args"] == driver.launch_args(engine)
    assert launched["proxy"] == {"server": "http://172.18.0.2:3128", "bypass": "<-loopback>"}
    assert launched["channel"] == "chromium" and launched["headless"] is True


def test_a_driver_made_without_an_engine_is_the_playwright_one():
    assert driver.Driver(playwright=None).engine == "playwright"
    assert driver.Driver(None, "something-else").engine == "playwright"


def _calls(tree: ast.AST):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def test_the_driver_hands_evaluate_nothing_only_patchright_understands():
    """`isolated_context=` is Patchright's own keyword; Playwright refuses it
    with a TypeError, and the fallback would stop working with every read.
    Patchright runs `evaluate` in an isolated world by default, which is what
    `MARK` and `SCROLLED` need: the page's DOM and window geometry, none of
    its variables."""
    tree = ast.parse(DRIVER_SOURCE.read_text(encoding="utf-8"))
    evaluate = [c for c in _calls(tree)
                if isinstance(c.func, ast.Attribute) and c.func.attr == "evaluate"]

    assert evaluate, "the driver no longer evaluates anything in the page"
    assert not [c.lineno for c in evaluate if c.keywords]
    assert not any("isolated" in ast.unparse(c) for c in _calls(tree)
                   if isinstance(c.func, ast.Attribute))


def test_the_driver_asks_for_no_persistent_profile():
    """Patchright's README recommends `launch_persistent_context` with a
    `user_data_dir`. A profile kept between sessions would be their session on
    the laptop's disk in the clear; every browser starts empty and is given
    its saved login through `storage_state` alone."""
    tree = ast.parse(DRIVER_SOURCE.read_text(encoding="utf-8"))
    called = {c.func.attr for c in _calls(tree) if isinstance(c.func, ast.Attribute)}
    keywords = {k.arg for c in _calls(tree) for k in c.keywords}

    assert "launch_persistent_context" not in called
    assert "user_data_dir" not in keywords and "storage_state" in keywords


def test_the_browser_image_pins_patchright_to_the_bases_playwright_and_checks_its_chromium():
    """Patchright's releases are numbered after the Playwright they patch, and
    each expects that Playwright's Chromium, which the base image holds. The
    build no longer downloads a browser for it (no hash covered that
    download); it fails if the Chromium Patchright would launch is missing."""
    pins = (sandbox.HERE / "requirements.txt").read_text(encoding="utf-8")
    [patchright] = re.findall(r"^patchright==(\d+\.\d+\.\d+) ", pins, re.M)
    [playwright] = re.findall(r"^playwright==(\d+\.\d+\.\d+) ", pins, re.M)

    assert patchright == playwright, "the fallback stays installed, at the same version"
    assert f":v{playwright}-" in sandbox.BROWSER_BASE
    assert "patchright install" not in sandbox.BROWSER_DOCKERFILE
    assert "chromium.executable_path" in sandbox.BROWSER_DOCKERFILE


def test_the_image_tag_follows_the_dockerfile_and_the_pins_so_the_old_image_is_not_reused(
        monkeypatch):
    """The user's browsers are tagged by a hash of what built them
    (`sandbox.image`): the tag changes with the Dockerfile and the pinned
    packages, so the image is built again and a worker still on the old tag
    keeps the old image until swapped."""
    now = sandbox.image("browser")
    assert "requirements.txt" in sandbox._context("browser")
    monkeypatch.setattr(sandbox, "BROWSER_DOCKERFILE", sandbox.BROWSER_DOCKERFILE + "# new\n")

    assert sandbox.image("browser") != now
