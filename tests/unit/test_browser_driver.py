"""The sandbox's browser tells a site it is ordinary Chrome, reports what a
page answered, and can be read past its first 8,000 characters.

On 7 October 2026 two sites refused the headless browser on its first
request -- reserveamerica.com a 403, destateparks.com CloudFront's "request
could not be satisfied" -- and the 403 reached the model as an open that went
fine; a LinkedIn profile's lower sections were never read. The driver
(core/browser/driver.py) runs in the container; here it is driven against a
small fake Playwright, as the frozen gate tests drive it.
"""

from __future__ import annotations

import pytest

from core.browser import driver


def test_the_user_agent_is_ordinary_chrome_at_the_browsers_own_version():
    agent = driver.regular_user_agent("141.0.7390.37")

    assert agent == ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/141.0.0.0 Safari/537.36")
    assert "Headless" not in driver.regular_user_agent("")


class _Browser:
    version = "141.0.7390.37"

    def __init__(self, made: dict) -> None:
        self.made = made

    async def new_context(self, **kwargs):
        self.made["context"] = kwargs
        raise RuntimeError("enough: the context's arguments are what is checked")


class _Chromium:
    def __init__(self, full: bool) -> None:
        self.full = full
        self.launched: list[dict] = []
        self.made: dict = {}

    async def launch(self, **kwargs):
        self.launched.append(kwargs)
        if kwargs.get("channel") == "chromium" and not self.full:
            raise RuntimeError("Executable doesn't exist at /ms-playwright/chromium-1234")
        return _Browser(self.made)


@pytest.mark.parametrize(("zone", "full"), [("America/Chicago", True), (None, False)])
async def test_the_browser_starts_as_ordinary_chrome_through_the_gate(zone, full):
    """Chromium's own headless mode where the image has it, the headless
    shell where it does not; either way a regular user agent, no automation
    flag, and every request still through the gate."""
    chromium = _Chromium(full)
    d = driver.Driver(type("PW", (), {"chromium": chromium})())

    with pytest.raises(RuntimeError, match="enough"):
        await d.cmd_start({"proxy": "http://172.18.0.2:3128"}
                          | ({"timezone": zone} if zone else {}))

    assert chromium.launched[0]["channel"] == "chromium"
    assert len(chromium.launched) == (1 if full else 2)
    last = chromium.launched[-1]
    assert last["proxy"] == {"server": "http://172.18.0.2:3128", "bypass": "<-loopback>"}
    assert "--disable-blink-features=AutomationControlled" in last["args"]
    context = chromium.made["context"]
    assert "Headless" not in context["user_agent"] and "Chrome/141" in context["user_agent"]
    # The laptop's own zone, as the worker sends it; a browser always in one
    # city's zone gave every other user's pages the wrong day after dark.
    assert context["timezone_id"] == (zone or "UTC") and context["locale"] == "en-US"
    assert context["service_workers"] == "block" and context["permissions"] == []


async def test_the_browser_asks_for_pages_in_the_language_the_worker_sends():
    """It was en-US for everyone; the container has no language of its
    own, so the worker sends the user's. Without one, en-US, as before."""
    chromium = _Chromium(True)
    d = driver.Driver(type("PW", (), {"chromium": chromium})())

    with pytest.raises(RuntimeError, match="enough"):
        await d.cmd_start({"proxy": "http://172.18.0.2:3128", "locale": "fr-FR",
                           "accept_language": "fr-FR,fr;q=0.9,en;q=0.8"})
    context = chromium.made["context"]
    assert context["locale"] == "fr-FR"
    assert context["extra_http_headers"] == {"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}

    with pytest.raises(RuntimeError, match="enough"):
        await d.cmd_start({"proxy": "http://172.18.0.2:3128"})
    assert chromium.made["context"]["extra_http_headers"] == {"Accept-Language": "en-US,en;q=0.9"}


class _Response:
    status = 403


class _Page:
    def __init__(self, text: str = "", height: int = 2400, at_end: bool = False) -> None:
        self.url = "https://www.reserveamerica.com/"
        self.text, self.height, self.at_end = text, height, at_end
        self.frames: list = []
        self.mouse = self

    async def goto(self, url, **kwargs):
        self.url = url
        return _Response()

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def wheel(self, dx, dy):
        return None

    async def title(self):
        return "403 Forbidden"

    async def evaluate(self, script, *args):
        if script is driver.MARK:
            return []
        if script is driver.SCROLLED:
            return {"height": self.height, "at_end": self.at_end}
        if "readyState" in script:
            return "complete"
        return self.text


def _driver(page: _Page) -> driver.Driver:
    d = driver.Driver(playwright=None)
    d.page = page
    return d


async def test_an_opened_pages_status_is_reported():
    said = await _driver(_Page()).run({"cmd": "open", "url": "https://www.reserveamerica.com/"})

    assert said["ok"] and said["status"] == 403 and said["title"] == "403 Forbidden"


async def test_a_long_page_is_read_on_from_where_asked():
    text = "a" * driver.TEXT_CHARS + "Experience at Northwind"
    d = _driver(_Page(text))

    first = await d.run({"cmd": "read"})
    rest = await d.run({"cmd": "read", "start": driver.TEXT_CHARS})

    assert first["truncated"] and first["chars"] == len(text) and "Northwind" not in first["text"]
    assert rest["text"] == "Experience at Northwind" and not rest["truncated"]


async def test_a_scroll_says_how_tall_the_page_is_and_whether_it_has_ended():
    said = await _driver(_Page(height=5000, at_end=True)).run({"cmd": "scroll"})

    assert said["height"] == 5000 and said["at_end"] is True


# ---------------------------------------------------------------------------
# Waiting for a page to draw itself (8 October 2026 audit, finding 8)
# ---------------------------------------------------------------------------


class _Drawing(_Page):
    """A page whose main text grows as the page draws itself, one size for
    each look the driver takes; once they are used it stays at the last."""

    def __init__(self, sizes: list[int]) -> None:
        super().__init__("body text")
        self.sizes = list(sizes)
        self.looks = 0
        self.waited_for: list[tuple] = []

    async def wait_for_load_state(self, *args, **kwargs):
        self.waited_for.append((args, kwargs))

    async def evaluate(self, script, *args):
        if script == driver.CONTENT_SIZE:
            self.looks += 1
            return self.sizes.pop(0) if len(self.sizes) > 1 else self.sizes[0]
        return await super().evaluate(script, *args)


@pytest.fixture
def quick_looks(monkeypatch):
    monkeypatch.setattr(driver, "CONTENT_POLL_S", 0)


async def test_a_logged_in_page_is_waited_for_by_its_content_and_not_for_the_network_to_go_quiet(
        quick_looks):
    """LinkedIn never goes quiet -- tracking beacons run for as long as the
    page is open -- so the five-second networkidle wait after every open
    always ran out: about 5 s of each 8.5 s page load on 8 October 2026.
    The page is ready when its main text has substance and has stopped
    growing."""
    page = _Drawing([0, 120, 800, 800, 800, 900])
    d = _driver(page)
    d.logged_in = True

    await d.run({"cmd": "open", "url": "https://www.linkedin.com/in/me/"})

    assert page.looks == 5, "ready once the size had held for two looks, not before"
    assert not [w for w in page.waited_for if "networkidle" in w[0]]


async def test_a_logged_in_page_that_never_settles_is_read_as_it_is_after_a_bounded_wait(
        quick_looks):
    page = _Drawing(list(range(400, 400 + 10 * driver.CONTENT_POLLS * 2, 10)))
    d = _driver(page)
    d.logged_in = True

    said = await d.run({"cmd": "open", "url": "https://www.linkedin.com/in/me/"})

    assert said["ok"] and page.looks == driver.CONTENT_POLLS


async def test_a_logged_in_page_with_almost_no_text_is_not_ready_however_still_it_is(
        quick_looks):
    """A blank page is steady too. It has to have something on it, or a page
    that has not started drawing is read as empty -- which a LinkedIn tool
    takes for a refusal."""
    page = _Drawing([0])
    d = _driver(page)
    d.logged_in = True

    await d.run({"cmd": "open", "url": "https://www.linkedin.com/in/me/"})

    assert page.looks == driver.CONTENT_POLLS


async def test_a_page_that_fails_to_report_its_size_does_not_fail_the_open(quick_looks):
    class _Navigating(_Drawing):
        async def evaluate(self, script, *args):
            if script == driver.CONTENT_SIZE:
                raise RuntimeError("Execution context was destroyed, most likely because of a "
                                   "navigation")
            return await _Page.evaluate(self, script, *args)

    d = _driver(_Navigating([0]))
    d.logged_in = True

    said = await d.run({"cmd": "open", "url": "https://www.linkedin.com/in/me/"})

    assert said["ok"]


async def test_an_ordinary_page_still_waits_for_the_network_to_go_quiet_for_up_to_five_seconds():
    page = _Drawing([800])
    d = _driver(page)

    await d.run({"cmd": "open", "url": "https://www.example.com/"})

    assert page.looks == 0
    assert ((("networkidle",), {"timeout": 5_000}) in page.waited_for)


@pytest.mark.parametrize("message, logged_in", [
    ({"proxy": "http://172.18.0.2:3128", "site_domains": ["linkedin.com"],
      "storage_state": {"cookies": [], "origins": []}}, True),
    ({"proxy": "http://172.18.0.2:3128", "site_domains": ["linkedin.com"],
      "storage_state": None}, True),
    ({"proxy": "http://172.18.0.2:3128", "site_domains": ["example.com"]}, False),
    ({"proxy": "http://172.18.0.2:3128"}, False),
])
async def test_only_a_browser_started_from_a_saved_login_is_a_logged_in_one(message, logged_in):
    """A browsing worker is kept to its sites too, but it reads ordinary
    public pages and keeps the wait they have always had."""
    d = driver.Driver(type("PW", (), {"chromium": _Chromium(True)})())

    with pytest.raises(RuntimeError, match="enough"):
        await d.cmd_start(message)

    assert d.logged_in is logged_in
