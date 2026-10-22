"""The fixtures for the app's screens (rig.py).

Here, under tests/unit, so the unit tree's fences hold for these tests as
for every other: a screen test runs the worker's own routes in its process,
and those reach the mailboxes, the web and the vault as any unit test's do.

One build of the screens for the run, and one Edge for each file of tests.
Not one Edge for the run: Playwright's sync API keeps its event loop
marked as running in the thread for as long as it is started, and every
later test in that process that called `asyncio.run` failed (56 of them,
the first time). Ended with its file, it lets go; inside a file of screen
tests, asyncio runs in a thread of its own. Edge starts in under a second.

Every test in this folder is in the xdist group `ui` (`pytestmark` in each
file), so under `--dist loadgroup` they share one worker and that one
build. A test gets a browser context of its own: its own cookies, storage
and stand-in for the shell.

The walks on the real app (test_exit_walk.py, test_app_walks.py) use
neither the build nor Edge, and are the group `app`: one after another, on
a worker beside the screens. In the screens' group, the folder took ten
minutes on one worker once they came (19 October), past the check's limit
for a change's tests.
"""

from __future__ import annotations

import contextlib

import pytest

from tests.unit.ui import rig


@pytest.fixture(scope="module")
def playwright():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        yield driver


@pytest.fixture(scope="module")
def edge(playwright, tmp_path_factory):
    with rig.edge(tmp_path_factory.mktemp("edge"), playwright) as browser:
        yield browser


@pytest.fixture(scope="session")
def screens(tmp_path_factory):
    """The app's screens, built from this checkout's sources for this run,
    with the mark's specimen sheet beside them (test_marks.py)."""
    return rig.build_ui(tmp_path_factory.mktemp("ui") / "dist", mode="specimen")


@pytest.fixture
def screen(edge, screens):
    """Opens the screens in Edge against a backend: `screen(app)` gives the
    page, with the shell's stand-in answering for the given token. `before`
    is given the page before it loads: to install a clock its first timers
    are on."""
    with contextlib.ExitStack() as stack:
        def open_(backend, *, token: str = "a-tests-token", label: str = "main",
                  path: str = "/", before=None):
            served = stack.enter_context(rig.serve(screens, backend))
            context = edge.new_context()
            stack.callback(context.close)
            context.add_init_script(rig.shell(token, label))
            page = context.new_page()
            if before:
                before(page)
            page.goto(served.url + path)
            return page

        yield open_
