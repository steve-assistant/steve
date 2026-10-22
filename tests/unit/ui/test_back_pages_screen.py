"""The back pages: every one reached from the menu, each saving in place.

Eleven tabs held these before, with the data for all of them in App.tsx and
a toast in the corner to say whether a change took. What Steve kept about
the user could be read only by asking it, and forgotten only by the number a
morning digest gave it (ui/src/Knows.tsx, Profile.tsx, Autonomy.tsx,
Settings.tsx, Diagnostics.tsx, Browser.tsx; agent/worker/knows.py). These
drive the built window in Edge against a backend that keeps what the pages
send, with the real knobs behind What Steve may do.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from agent import accounts, appsettings, features, wake
from agent.attention import settings
from agent.worker import approvers, background, control, desk, readers
from core import call_policy, db, decider, detector, knobs, localapi, modules

pytestmark = [pytest.mark.processes, pytest.mark.xdist_group("ui")]

TODAY = "2026-10-15"
AT = "2026-10-15T14:10:00Z"
MENU = {
    "About you": ["What Steve knows", "Profile"],
    "Connections": ["Accounts", "Channels", "Browser logins"],
    "Steve": ["What Steve may do", "Routines and goals", "Extensions", "Spend", "Activity",
              "Diagnostics", "Settings"],
}


COUNTS = {"active": 0, "paused": 0, "awaiting_approval": 0}
OVERVIEW = {
    "worker": {"ok": True, "version": "0.1.0", "pid": 4242, "duties": "all", "uptime_s": 3720},
    "signal": {"state": "not set up"},
    "sandbox": {"available": False, "reason": "Docker Desktop isn't running"},
    "waiting": 0, "spend_today": 0.42, "routines": COUNTS, "goals": COUNTS, "questions": 0,
    "quiet_hours": {"from": "22:00", "until": "07:00", "now": False},
    "issues": [{"id": 1, "kind": "tool_error", "summary": "The calendar didn't answer.",
                "lately": 2, "last_seen": "2026-10-15 13:02:00"}],
    "mailbox": {"address": "steve.maya@example.com", "enabled": True,
                "today": {"instruction": 1, "thread": 2, "ignored": 0}},
    "awake": {"since": "2026-10-15 07:45:00", "slept": None},
}


def _fact(n: int, about: str, attribute: str, value: str, source: str) -> dict:
    return {"id": n, "about": about, "attribute": attribute, "value": value, "source": source,
            "until": None}


class Backend:
    """What the back pages read, and what they send, kept."""

    def __init__(self, *, routines: list[dict] | None = None):
        self.facts = [_fact(1, "You", "name", "Maya Lindqvist", "user_words"),
                      _fact(2, "Theo Brandt", "employer", "Contso", "mail")]
        self.memories = [{"path": "project/kiln.md", "kind": "project", "at": AT, "until": None,
                          "summary": "Maya is building a kiln controller with Theo."}]
        self.profile = {"name": "Maya Lindqvist", "pronouns": "she/her", "home_city": "",
                        "occupation": ""}
        self.routines = routines or []
        self.sent: list[tuple[str, dict]] = []
        # What the page said over the socket: a chat message, say.
        self.said: list[dict] = []

    def app(self) -> Starlette:
        async def knows(request: Request):
            return JSONResponse({"ok": True, "facts": self.facts, "memories": self.memories})

        async def correct(request: Request):
            body = await request.json()
            self.sent.append((request.url.path, body))
            for fact in self.facts:
                if fact["id"] == body["id"]:
                    fact |= {"value": body["value"], "source": "confirmed"}
            return JSONResponse({"ok": True, "id": body["id"], "value": body["value"]})

        async def forget(request: Request):
            body = await request.json()
            self.sent.append((request.url.path, body))
            self.facts = [f for f in self.facts if f["id"] != body.get("id")]
            self.memories = [m for m in self.memories if m["path"] != body.get("path")]
            return JSONResponse({"ok": True})

        async def profile(request: Request):
            if request.method == "PUT":
                changes = (await request.json())["profile"]
                self.sent.append((request.url.path, changes))
                self.profile |= changes
                home = ({"says": "Leeds, England: the weather and the day's times are read "
                                 "from there."} if "home_city" in changes else None)
                return JSONResponse({"ok": True, "profile": self.profile, "home": home})
            return JSONResponse({"ok": True, "profile": self.profile})

        async def autonomy(request: Request):
            return JSONResponse({"routines": self.routines, "goals": [],
                                 "quiet_hours": control.quiet_hours(),
                                 "knobs": control._knob_rows(control.current_knobs())})

        async def stop(request: Request):
            body = await request.json()
            self.sent.append((request.url.path, body))
            paused = sum(1 for r in self.routines if r["status"] == "active")
            for r in self.routines:
                r["status"] = "paused"
            return JSONResponse({"ok": True, "paused": paused})

        async def overview(request: Request):
            return JSONResponse(OVERVIEW)

        async def day(request: Request):
            return JSONResponse({"ok": True, "date": TODAY, "today": TODAY, "turns_at": "04:00",
                                 "as_of": AT, "looked": None, "entries": []})

        async def days(request: Request):
            return JSONResponse({"ok": True, "days": []})

        async def needs(request: Request):
            return JSONResponse({"ok": True, "needs": []})

        async def socket(ws):
            await ws.accept(subprotocol=localapi.subprotocol(ws.scope))
            while (message := await ws.receive())["type"] != "websocket.disconnect":
                if message.get("text"):
                    self.said.append(json.loads(message["text"]))
                await asyncio.sleep(0)

        async def anything(request: Request):
            return JSONResponse({"detail": "not in this test"}, status_code=404)

        return Starlette(routes=[
            Route("/control/app", control.app_settings, methods=["GET", "POST"]),
            Route("/control/knows", knows),
            Route("/control/knows/correct", correct, methods=["POST"]),
            Route("/control/knows/forget", forget, methods=["POST"]),
            Route("/control/profile", profile, methods=["GET", "PUT"]),
            Route("/control/autonomy", autonomy),
            Route("/control/knobs", control.set_knobs, methods=["POST"]),
            Route("/control/stop", stop, methods=["POST"]),
            Route("/control/overview", overview),
            Route("/control/day", day),
            Route("/control/days", days),
            Route("/control/needs", needs),
            *readers.ROUTES,
            Route("/approvals/setup", approvers.setup),
            WebSocketRoute("/ws", socket),
            Route("/{path:path}", anything, methods=["GET", "POST", "PUT"]),
        ])


@pytest.fixture(autouse=True)
def knob_globals(monkeypatch):
    """A knob saved here is put in force at once (`control.apply_knobs`),
    in this process's modules: each is put back after the test."""
    for module, names in ((settings, ("MAX_DAY_PINGS", "MAX_NIGHT_PINGS", "MORNING", "EVENING",
                                      "BEFORE_LEAD", "FOLLOW_UP_AFTER", "MEETING_RESEARCH",
                                      "UNITS")),
                          (call_policy, ("QUIET_FROM", "QUIET_UNTIL")),
                          (background, ("DIGEST_HOUR",)), (desk, ("LIVE_DAY_MIN",))):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    control.defaults()


def _until(check, within: float = 15.0):
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("not within the time")
        time.sleep(0.05)


def _open(screen, backend: Backend, page_name: str | None = None):
    page = screen(backend.app())
    page.set_viewport_size({"width": 1300, "height": 860})
    page.locator("nav.sidebar").wait_for(timeout=15_000)
    if page_name:
        _go(page, page_name)
    return page


def _go(page, name: str) -> None:
    page.locator(".who").click()
    page.get_by_role("menuitem", name=name, exact=True).click()


def _text(page, selector: str) -> str:
    return page.locator(selector).first.inner_text().strip()


def test_the_menu_reaches_every_back_page_under_its_group_and_none_breaks(screen):
    """A page that throws leaves a blank window and no way to say why; each
    is opened against a backend that answers only some of what it asks."""
    faults: list[str] = []
    page = _open(screen, Backend())
    page.on("pageerror", lambda error: faults.append(str(error)))

    page.locator(".who").click()
    menu = page.locator("[role=menu]")
    assert {g.get_attribute("aria-label"): [i.inner_text().strip()
                                            for i in g.get_by_role("menuitem").all()]
            for g in menu.locator("[role=group]").all()} == MENU
    page.keyboard.press("Escape")

    for group, names in MENU.items():
        for name in names:
            _go(page, name)
            assert (_text(page, ".back-group"), _text(page, ".back-name")) == (group, name)
            page.wait_for_timeout(150)
    assert faults == []


def test_what_steve_knows_is_corrected_and_forgotten_where_it_is_read(screen):
    """A wrong fact Steve read in mail is put right by the user's word, which
    nothing read later replaces; a memory is forgotten after a second step
    that says what goes."""
    backend = Backend()
    page = _open(screen, backend, "What Steve knows")
    theo = page.locator('li[data-fact="2"]')
    theo.wait_for(timeout=10_000)
    assert [h.inner_text() for h in page.locator("h3.knows-about").all()] == [
        "You", "Theo Brandt"], "the user first"
    assert "from your mail" in theo.inner_text()

    theo.get_by_role("button", name="Correct").click()
    box = theo.get_by_role("textbox")
    box.fill("Contoso")
    box.press("Enter")
    _until(lambda: ("/control/knows/correct", {"id": 2, "value": "Contoso"}) in backend.sent)
    _until(lambda: "Contoso" in theo.inner_text() and "you confirmed" in theo.inner_text())
    assert "Corrected" in _text(page, ".page-said")

    kiln = page.locator('li[data-memory="project/kiln.md"]')
    kiln.get_by_role("button", name="Forget").click()
    assert "Forget “Maya is building a kiln controller" in kiln.inner_text()
    assert not any(path.endswith("forget") for path, _ in backend.sent), "not on the first press"
    kiln.get_by_role("button", name="Forget").click()
    _until(lambda: ("/control/knows/forget", {"path": "project/kiln.md"}) in backend.sent)
    _until(lambda: kiln.count() == 0)


def test_the_profile_keeps_each_line_as_it_is_left_and_never_an_empty_name(screen):
    """A form with a Save button at its foot loses what was typed when the
    user leaves without pressing it; each line here is kept as it is left.
    A name emptied would leave Steve nothing to call them."""
    backend = Backend()
    page = _open(screen, backend, "Profile")
    home = page.locator('[data-field="home_city"] input')
    home.wait_for(timeout=10_000)
    home.fill("Leeds")
    home.press("Tab")
    _until(lambda: ("/control/profile", {"home_city": "Leeds"}) in backend.sent)
    _until(lambda: "Leeds, England" in _text(page, '[data-field="home_city"]'))

    name = page.locator('[data-field="name"] input')
    name.fill("")
    name.press("Tab")
    _until(lambda: "Steve needs a name to call you." in _text(page, '[data-field="name"]'))
    assert not any("name" in body for _, body in backend.sent)

    name.fill("Maya L.")
    name.press("Enter")
    _until(lambda: ("/control/profile", {"name": "Maya L."}) in backend.sent)
    _until(lambda: _text(page, ".who-name") == "Maya L.")


def test_what_steve_may_do_saves_a_switch_and_a_number_as_each_is_changed(screen):
    """Each knob is kept, in force, as it is changed, and says so beside
    itself; a number past its bounds is refused with the reason, in place.
    The knobs are the real ones, kept where the worker reads them."""
    page = _open(screen, Backend(), "What Steve may do")
    browser = page.locator('[data-knob="BROWSER"]')
    browser.wait_for(timeout=10_000)
    assert page.locator('[data-knob="QUIET_FROM"]').count() == 0, "the day's times are on Profile"

    browser.locator(".form-check").click()
    _until(lambda: knobs.read().get("BROWSER") == "on")
    _until(lambda: "Saved" in browser.inner_text())

    pings = page.locator('[data-knob="MAX_DAY_PINGS"]')
    was = control.defaults()["MAX_DAY_PINGS"]
    pings.get_by_role("button", name=f"More: {knobs.KNOBS['MAX_DAY_PINGS'].says}").click()
    _until(lambda: knobs.read().get("MAX_DAY_PINGS") == was + 1)

    box = pings.get_by_role("textbox")
    box.fill(str(int(knobs.KNOBS["MAX_DAY_PINGS"].hi) + 5))
    box.press("Enter")
    _until(lambda: pings.locator(".form-error").count() == 1)
    assert knobs.read().get("MAX_DAY_PINGS") == was + 1


def test_each_optional_part_says_what_it_adds_and_takes_and_the_readers_install_here(
        screen, monkeypatch):
    """The switches said only "(needs Docker)", and the local readers, which
    read mail and pages when Jev can't, took a command at a terminal. Each
    switch now says what it adds and what it takes, and the readers install
    with a click, the page saying so while they do. The setups' 7 GB are
    two small fakes here."""
    ran: list[str] = []
    gate = threading.Event()

    def setup(name: str):
        def run():
            gate.wait(timeout=10)
            ran.append(name)
        return run

    monkeypatch.setattr(readers, "_install", {"thread": None, "error": None})
    monkeypatch.setattr(detector, "setup", setup("piguard"))
    monkeypatch.setattr(decider, "setup", setup("tev1"))
    monkeypatch.setattr(detector, "installed", lambda: "piguard" in ran)
    monkeypatch.setattr(decider, "installed", lambda: "tev1" in ran)
    monkeypatch.setattr(wake, "kept_awake", contextlib.nullcontext)

    page = _open(screen, Backend(), "What Steve may do")
    for name in sorted(n for n in knobs.SWITCHES if knobs.KNOBS[n].group == "features"):
        switch = page.locator(f'[data-knob="{name}"]')
        switch.wait_for(timeout=10_000)
        assert all(line in switch.inner_text() for line in features.OFFERS[name]), name

    adds, takes = features.OFFERS["READERS"]
    page.get_by_text(adds).wait_for(timeout=10_000)
    assert page.get_by_text(takes).count() == 1
    page.get_by_role("button", name="Install them").click()
    page.get_by_role("button", name="Installing…").wait_for(timeout=5_000)
    gate.set()
    page.get_by_text("Both are installed").wait_for(timeout=10_000)
    assert ran == ["piguard", "tev1"]
    assert page.get_by_role("button", name="Install them").count() == 0

    _go(page, "Channels")
    page.get_by_text("A channel lets you message Steve").wait_for(timeout=10_000)
    _go(page, "Settings")
    page.get_by_text("It takes an authenticator app on your phone").wait_for(timeout=10_000)


def test_the_brakes_pause_every_routine_at_once_and_say_how_many(screen):
    """STOP ROUTINES from the phone has its button here; disabled with
    nothing running, so a press always does something."""
    backend = Backend(routines=[{
        "id": 4, "title": "Recap my week", "status": "active", "when": "Sundays at 19:00",
        "runs": 3, "next_run": "2026-10-18T19:00:00-05:00", "urgent": False,
        "instruction": "Recap the week", "may_without_asking": "nothing",
        "last_report": None, "held": None}])
    page = _open(screen, backend, "What Steve may do")
    stop = page.get_by_role("button", name="Stop all routines")
    stop.wait_for(timeout=10_000)
    assert page.get_by_role("button", name="Stop all goals").is_disabled()
    stop.click()
    _until(lambda: ("/control/stop", {"what": "routines"}) in backend.sent)
    _until(lambda: "Paused 1 routines." in _text(page, ".page-said"))
    _until(lambda: stop.is_disabled())


def test_settings_change_the_window_at_once_and_keep_the_choice(screen):
    """Reading is for the person who can't read the default: a choice that
    took a restart, or was lost at one, would not help them."""
    page = _open(screen, Backend(), "Settings")
    reading = page.locator('[data-setting="theme"]')
    reading.wait_for(timeout=10_000)
    reading.get_by_role("radio", name="Ink", exact=True).click()
    _until(lambda: page.evaluate("document.documentElement.dataset.theme") == "ink")
    _until(lambda: appsettings.read().get("theme") == "ink")

    page.locator('[data-setting="size"]').get_by_role("radio", name="Larger").click()
    _until(lambda: page.evaluate("document.documentElement.dataset.size") == "larger")
    assert appsettings.read().get("size") == "larger"


def test_signing_out_when_away_is_off_until_turned_on_beside_the_sign_in_in_settings(screen):
    """The app signed out after 30 minutes away, always, until 19 October
    2026. Now it is off unless the user turns it on, in Settings beside the
    sign-in it asks for, saying what it adds and takes; not among the
    features on What Steve may do."""
    page = _open(screen, Backend(), "Settings")
    switch = page.locator('[data-knob="SIGN_OUT_IDLE"]')
    switch.wait_for(timeout=10_000)
    assert not switch.locator("input").is_checked()
    assert all(line in switch.inner_text() for line in features.OFFERS["SIGN_OUT_IDLE"])

    switch.locator(".form-check").click()
    _until(lambda: knobs.read().get("SIGN_OUT_IDLE") == "on")
    _until(lambda: "Saved" in switch.inner_text())

    _go(page, "What Steve may do")
    page.locator('[data-knob="BROWSER"]').wait_for(timeout=10_000)
    assert page.locator('[data-knob="SIGN_OUT_IDLE"]').count() == 0


def test_after_five_wrong_codes_settings_says_why_a_yes_by_message_fails_and_until_when(screen):
    """The phone that sent them is told to answer in the app; the app says
    why a yes to spending by message is not taken, and until when (step
    11's audit counted wrong codes, which nothing did before)."""
    from core import totp

    for _ in range(totp.TRIES):
        totp.wrong()
    page = _open(screen, Backend(), "Settings")

    line = page.get_by_text("Wrong codes were sent, or could not be counted")
    line.wait_for(timeout=10_000)
    assert "not taken until" in line.inner_text() and "answer in this app" in line.inner_text()


def test_ink_in_quiet_hours_is_ink_only_while_they_are_quiet(screen, monkeypatch):
    """The theme the design offers for the night was chosen and then
    followed Windows instead: the window never heard when the quiet hours
    were."""
    quiet = {"now": True}
    monkeypatch.setattr(control, "quiet_hours", lambda: {"from": "22:00", "until": "07:00",
                                                         **quiet})
    appsettings.write({"theme": "ink-in-quiet-hours"})
    page = _open(screen, Backend())
    _until(lambda: page.evaluate("document.documentElement.dataset.theme") == "ink")

    quiet["now"] = False
    page.reload()
    _until(lambda: page.evaluate("document.documentElement.dataset.theme") == "paper")


def test_diagnostics_shows_the_backend_the_end_of_its_log_and_todays_problems(screen):
    """Why it stopped, or what went wrong today, was spread over Home and a
    Settings tab; here it is in one place, with Start and Stop."""
    page = _open(screen, Backend())
    page.evaluate("""() => {
      window.__STEVE_SHELL__.answers.backend_control = {
        state: "running", pid: 4242, restarts: 0, damaged: null,
        log_tail: ["worker 2 started", "calendar: no answer from the server"]};
    }""")
    _go(page, "Diagnostics")
    log = page.locator("pre.form-log")
    log.wait_for(timeout=10_000)
    assert "calendar: no answer from the server" in log.inner_text()
    _until(lambda: "The calendar didn't answer." in _text(page, ".diagnostics"))


def test_the_agent_is_said_in_a_sentence_with_its_words_to_press_not_a_dashboards_card(screen):
    """The agent's panel was a boxed card with a green dot, a bold
    "Running" and "duties active": the one part of the window that looked
    like another program. It says the same in a sentence now, like the rest."""
    page = _open(screen, Backend())
    page.evaluate("""() => {
      window.__STEVE_SHELL__.answers.backend_control = {
        state: "running", pid: 4242, restarts: 0, damaged: null, log_tail: []};
    }""")
    _go(page, "Diagnostics")
    _until(lambda: "version 0.1.0" in _text(page, ".diagnostics .power"))
    power = page.locator(".diagnostics .power")
    said = power.inner_text()
    assert said.startswith("Steve is running: version 0.1.0, up 62 minutes, awake since "), said
    assert "duties" not in said
    assert page.locator(".diagnostics .dot").count() == 0
    looks = power.evaluate("""e => {
      const s = getComputedStyle(e);
      return [s.borderTopStyle, s.backgroundColor];
    }""")
    assert looks == ["none", "rgba(0, 0, 0, 0)"]
    assert [b.strip() for b in power.locator("button").all_inner_texts()] == ["Restart", "Stop"]


def _needs_password(key: str, module: str, preset: str | None) -> dict:
    """An account row as the listing gives it, waiting for a new password."""
    return {"key": key, "name": f"maya@{key}.example", "module": module, "preset": preset,
            "enabled": True, "classes": ["mail"], "addresses": [f"maya@{key}.example"],
            "settings": {}, "state": "needs_you",
            "health": f"maya@{key}.example needs its new password.", "fix": "",
            "action": "password", "waiting": 0, "sign_in": None}


def test_a_secrets_field_says_where_to_revoke_it_from_its_module_or_its_provider(screen):
    """Only Gmail's guide said where to take a leaked app password back; the
    revoke pages the manifests name were never shown (the step 11 audit,
    E8). Read from the built-in manifests themselves: a preset's own page
    comes before its module's."""
    conn = db.open_store()
    modules.register(conn)
    listing = accounts.listing(conn) | {"builds": [], "reports": []}
    conn.close()
    listing["accounts"] = [_needs_password("gmail", "gmail_password", None),
                           _needs_password("fastmail", "imap", "fastmail")]
    backend = Backend()
    app = backend.app()

    async def listed(request: Request):
        return JSONResponse(listing)

    app.router.routes.insert(0, Route("/control/modules", listed))
    page = screen(app)
    page.set_viewport_size({"width": 1300, "height": 860})
    page.locator("nav.sidebar").wait_for(timeout=15_000)
    _go(page, "Accounts")

    for name, page_url in [("maya@gmail.example", "https://myaccount.google.com/apppasswords"),
                           ("maya@fastmail.example",
                            "https://app.fastmail.com/settings/security")]:
        card = page.locator(".item", has_text=name).first
        card.get_by_role("button", name="Enter the new password").click()
        link = card.get_by_role("button", name="Where to revoke it")
        link.wait_for(timeout=10_000)
        assert link.get_attribute("title") == page_url


def test_linkedins_step_waits_for_the_browser_and_for_the_risk_to_be_accepted(screen):
    """Automated reading may break LinkedIn's terms and cost the user the
    account: the step says so before it signs in, and only where the
    sandboxed browser that does the reading is on."""
    page = _open(screen, Backend(), "Browser logins")
    page.locator(".back-name").wait_for(timeout=10_000)
    page.wait_for_timeout(500)
    assert page.locator("[data-linkedin]").count() == 0, "not with the browser off"

    knobs.write({"BROWSER": "on"})
    _go(page, "Settings")
    _go(page, "Browser logins")
    step = page.locator('[data-linkedin="step"]')
    step.wait_for(timeout=10_000)
    assert "may break LinkedIn’s terms" in step.inner_text()
    sign_in = step.get_by_role("button", name="Sign in through the live view")
    assert sign_in.is_disabled()
    step.locator(".form-check").click()
    sign_in.click()
    _until(lambda: page.get_by_placeholder("Address, e.g. login.northwind.example")
           .input_value() == "https://www.linkedin.com/login")


LAYOUT = """() => {
  const body = document.querySelector(".back-body");
  const width = body.getBoundingClientRect().width;
  const scrolls = [...document.querySelectorAll(".back-column *")].filter((el) => {
    const y = getComputedStyle(el).overflowY;
    return (y === "auto" || y === "scroll") && el.scrollHeight > el.clientHeight + 1
      && !el.matches("pre, textarea, select");
  }).map((el) => el.className);
  const narrow = [...body.children].filter((el) =>
    el.getBoundingClientRect().width < width - 1).map((el) => el.className);
  return { scrolls, narrow };
}"""


def test_a_back_page_scrolls_as_one_and_its_view_fills_the_column(screen):
    """Each view scrolling inside the column put a second scrollbar in the
    middle of the window; a page's said line, laid beside its view, took
    half of Browser logins' width; and the old screens' stylesheet, loaded
    after the forms', drew their choices as boxed buttons."""
    knobs.write({"BROWSER": "on"})
    page = _open(screen, Backend())
    for names in MENU.values():
        for name in names:
            _go(page, name)
            page.locator(".back-body > .panel").wait_for(timeout=10_000)
            page.wait_for_timeout(300)
            assert page.evaluate(LAYOUT) == {"scrolls": [], "narrow": []}, name

    _go(page, "Settings")
    chosen = page.locator(".form-choices button.chosen").first
    assert chosen.evaluate("(el) => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
