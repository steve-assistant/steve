"""Their live view: their clicks and keys reaching the sandbox, and a login
saved only whole -- its session with it -- and never shown back.

A fake browser session and the conftest's throwaway vault; the routes are
mounted on a small app behind the same localhost check the worker uses.
What may call what is pinned in the frozen gate
(tests/gate/test_browser_logins.py).
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from agent.browser import live
from core import localapi, localonly
from core.browser import vault as vaults

TOKEN = "a" * 64


def _client(app) -> TestClient:
    """The desktop app's requests: its origin and this start's token."""
    localapi.accept(TOKEN)
    return TestClient(app, base_url="http://127.0.0.1",
                      headers={"origin": "tauri://localhost", "authorization": f"Bearer {TOKEN}"})

CASTGC ={"name": "CASTGC", "value": "tgt", "domain": ".contoso.example", "path": "/", "expires": -1}


class _Session:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.answers: dict = {
            "open": {"ok": True, "url": "https://cas.contoso.example/cas/login", "title": "CAS"},
            "frame": {"ok": True, "url": "https://cas.contoso.example/", "jpeg": "AAAA"},
            "export_state": {"ok": True, "url": "https://cas.contoso.example/",
                             "state": {"cookies": [dict(CASTGC)], "origins": []}},
        }

    async def ask(self, cmd, **args):
        self.sent.append((cmd, args))
        return dict(self.answers.get(cmd, {"ok": True}))


@pytest.fixture
def view(monkeypatch):
    import socket

    from core import egress

    # Names resolve here, not on the network: Contoso's to a public address.
    monkeypatch.setattr(egress.socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("130.132.35.53", port))])
    session = _Session()
    opened = {"live": False}

    async def get(*, create, touch=True):
        if create:
            opened["live"] = True
        return session if opened["live"] else None

    monkeypatch.setattr(live, "_session", get)
    app = Starlette(middleware=[Middleware(localonly.LocalOnly)], routes=live.ROUTES)
    return _client(app), session


def test_a_page_somewhere_private_is_not_opened(view):
    client, session = view

    said = client.post("/browser/live/open", json={"url": "http://192.168.1.1/"}).json()

    assert said["ok"] is False and session.sent == []


def test_their_clicks_keys_and_scrolls_reach_the_page_as_theirs(view):
    client, session = view
    client.post("/browser/live/open", json={"url": "https://93.184.216.34/"})
    session.sent.clear()
    client.post("/browser/live/open", json={"url": "cas.contoso.example"})  # no scheme: https

    for body in ({"kind": "click", "x": 10, "y": 20}, {"kind": "type", "text": "ml123"},
                 {"kind": "key", "key": "Enter"}, {"kind": "scroll", "dy": 300}):
        assert client.post("/browser/live/act", json=body).json()["ok"]

    assert [c for c, _ in session.sent] == ["open", "human_click", "human_type", "human_key",
                                           "human_scroll"]
    assert session.sent[0][1] == {"url": "https://cas.contoso.example"}
    assert client.post("/browser/live/act", json={"kind": "eval"}).status_code == 400


def test_a_login_is_saved_whole_and_never_shown_back(view):
    client, session = view
    client.post("/browser/live/open", json={"url": "https://cas.contoso.example/"})

    said = client.post("/browser/live/save", json={
        "name": "Contoso", "domains": "contoso.example, duosecurity.com", "tier": "ask",
        "username": "ml123", "password": "hunter2"}).json()

    assert said == {"ok": True, "site": {"name": "contoso",
                                         "domains": ["contoso.example", "duosecurity.com"],
                                         "tier": "ask", "has_password": True,
                                         "has_session": True},
                    "closing_in_s": live.SAVE_GRACE_S}
    assert ("export_state", {"domains": ["contoso.example", "duosecurity.com"]}) in session.sent
    store = vaults.vault()
    assert store.credentials("contoso") == ("ml123", "hunter2")
    assert store.load_state("contoso") == {"cookies": [CASTGC], "origins": []}
    listed = client.get("/browser/logins").text
    assert "hunter2" not in listed and "CASTGC" not in listed and "ml123" not in listed


@pytest.mark.parametrize("body, why", [
    ({"name": "contoso", "domains": "edu"}, "not a domain"),
    ({"name": "Contoso Login!", "domains": "contoso.example"}, "lower-case"),
    # Left empty, the field showed its grey "contoso" hint, and the old message
    # about lower-case letters read as being about their password (4 Oct 2026).
    ({"name": "", "domains": "contoso.example"}, "Name: give the login a name"),
    ({"name": "contoso", "domains": "contoso.example", "tier": "always"}, "ask or free"),
    ({"name": "contoso", "domains": ""}, "domains"),
])
def test_nothing_is_saved_from_a_form_that_is_wrong(view, body, why):
    client, session = view
    client.post("/browser/live/open", json={"url": "https://cas.contoso.example/"})

    said = client.post("/browser/live/save", json=body).json()

    assert said["ok"] is False and why in said["error"]
    assert vaults.vault().sites() == []
    assert "export_state" not in [c for c, _ in session.sent]


def test_a_page_off_the_logins_site_saves_nothing(view):
    """The user typed contoso.example but the page is somewhere else: nothing is written,
    not even the login's name."""
    client, session = view
    client.post("/browser/live/open", json={"url": "https://cas.contoso.example/"})
    session.answers["export_state"] = {"ok": False, "error": "the page is not on the login's site"}

    said = client.post("/browser/live/save", json={"name": "contoso",
                                                   "domains": "contoso.example"}).json()

    assert said["ok"] is False and vaults.vault().sites() == []


def test_forgetting_a_login_removes_it(view):
    client, _ = view
    client.post("/browser/live/open", json={"url": "https://cas.contoso.example/"})
    client.post("/browser/live/save", json={"name": "contoso", "domains": "contoso.example",
                                            "username": "u", "password": "p"})

    assert client.post("/browser/logins/forget", json={"name": "contoso"}).json()["ok"]

    assert vaults.vault().sites() == [] and vaults.vault().credentials("contoso") is None


def test_a_file_the_user_adds_to_the_outbox_is_kept_under_a_safe_name(view):
    import base64

    from core.browser import files

    client, _ = view

    said = client.post("/browser/live/outbox", json={
        "name": "../../My CV (final).pdf", "b64": base64.b64encode(b"%PDF cv").decode()}).json()

    assert said == {"ok": True, "file": "outbox/My CV _final_.pdf", "size": 7}
    assert (files.outbox_dir() / "My CV _final_.pdf").read_bytes() == b"%PDF cv"
    assert files.upload_source(said["file"]) == (files.outbox_dir() / "My CV _final_.pdf").resolve()


@pytest.mark.parametrize("b64", ["", "not base64!!", None])
def test_nothing_is_added_to_the_outbox_from_a_broken_upload(view, b64):
    from core.browser import files

    client, _ = view

    said = client.post("/browser/live/outbox", json={"name": "x.pdf", "b64": b64}).json()

    assert said["ok"] is False and not files.outbox_dir().exists()


def test_a_page_from_anywhere_else_cannot_use_the_live_view(view):
    client, session = view

    refused = client.post("/browser/live/act", json={"kind": "type", "text": "x"},
                          headers={"origin": "https://attacker.example"})

    assert refused.status_code == 403 and session.sent == []


# --- the desktop app's sign-in window (import_login) ---------------------------------

CANVAS = {"name": "contoso-canvas", "domains": "contoso.instructure.com contoso.example",
          "url": "https://contoso.instructure.com/?login_success=1"}


def _cookies():
    return [
        {"name": "canvas_session", "value": "c1", "domain": "contoso.instructure.com",
         "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        {"name": "_csrf_token", "value": "x", "domain": "contoso.instructure.com",
         "expires": 1893456000, "secure": True, "sameSite": "None"},
        {"name": "CASTGC", "value": "tgt", "domain": ".contoso.example", "sameSite": "None",
         "secure": False, "expires": 0},
        {"name": "ad", "value": "y", "domain": ".doubleclick.net"},
    ]


def test_a_login_signed_into_in_the_app_window_is_saved_with_its_own_cookies(view):
    client, session = view

    said = client.post("/browser/logins/import", json={
        **CANVAS, "tier": "free", "cookies": _cookies(),
        "username": "ml433", "password": "Upper!Case9"}).json()

    assert said["ok"] is True and said["cookies"] == 3 and said["left_behind"] == 1
    assert said["site"] == {"name": "contoso-canvas", "domains": ["contoso.instructure.com",
                                                                  "contoso.example"],
                            "tier": "free", "has_password": True, "has_session": True}
    store = vaults.vault()
    state = store.load_state("contoso-canvas")
    by_name = {c["name"]: c for c in state["cookies"]}
    assert set(by_name) == {"canvas_session", "_csrf_token", "CASTGC"}
    session_cookie = by_name["canvas_session"]
    assert session_cookie["httpOnly"] is True and session_cookie["expires"] == -1
    assert by_name["_csrf_token"]["sameSite"] == "None" and by_name["_csrf_token"]["expires"] > 0
    # Chromium drops SameSite=None without Secure; kept as Lax instead. And
    # an expiry of 0 is "for this session", not long expired.
    assert by_name["CASTGC"]["sameSite"] == "Lax" and by_name["CASTGC"]["expires"] == -1
    assert store.credentials("contoso-canvas") == ("ml433", "Upper!Case9")
    assert session.sent == [], "the sandbox is not involved"
    listed = client.get("/browser/logins").text
    assert "Upper!Case9" not in listed and "tgt" not in listed and "c1" not in listed


def test_a_sign_in_windows_cookies_are_put_back_on_their_domains(view):
    """The window sends every domain without its leading dot -- the Rust
    cookie crate strips it -- and a dotless domain is a host-only cookie to
    the sandbox. On 7 October 2026 LinkedIn's `.linkedin.com` cookies were
    never sent to www.linkedin.com, and the login worked once, then looped.
    This is the shape lib.rs really sends."""
    client, _ = view
    window = [
        {"name": "li_at", "value": "a", "domain": "www.linkedin.com", "secure": True},
        {"name": "bcookie", "value": "b", "domain": "linkedin.com", "secure": True},
        {"name": "__Host-sid", "value": "h", "domain": "www.linkedin.com", "secure": True},
        {"name": "NID", "value": "g", "domain": "google.com"},
    ]

    said = client.post("/browser/logins/import", json={
        "name": "linkedin", "domains": "linkedin.com", "url": "https://www.linkedin.com/feed/",
        "cookies": window}).json()

    assert said["ok"] is True and said["cookies"] == 3 and said["left_behind"] == 1
    by_name = {c["name"]: c["domain"] for c in vaults.vault().load_state("linkedin")["cookies"]}
    assert by_name == {"li_at": ".www.linkedin.com", "bcookie": ".linkedin.com",
                       "__Host-sid": "www.linkedin.com"}


@pytest.mark.parametrize("change, why", [
    ({"name": ""}, "Name: give the login a name"),
    ({"domains": "canvas.contoso.example"}, "not in these domains"),
    ({"url": "https://accounts.google.com/"}, "not in these domains"),
    ({"cookies": []}, "sign in first"),
    ({"cookies": [{"name": "ad", "value": "y", "domain": ".doubleclick.net"}]}, "no cookies for"),
    ({"cookies": [{"name": "x", "value": "y", "domain": "contoso.example"}] * 401},
     "sign in first"),
    ({"cookies": "not a list"}, "sign in first"),
])
def test_nothing_is_saved_from_a_sign_in_window_that_will_not_do(view, change, why):
    client, _ = view

    said = client.post("/browser/logins/import",
                       json={**CANVAS, "cookies": _cookies(), **change}).json()

    assert said["ok"] is False and why in said["error"]
    assert vaults.vault().sites() == []


def test_a_page_from_anywhere_else_cannot_import_a_login(view):
    client, _ = view

    refused = client.post("/browser/logins/import", json={**CANVAS, "cookies": _cookies()},
                          headers={"origin": "https://attacker.example"})

    assert refused.status_code == 403 and vaults.vault().sites() == []


def test_a_password_the_window_saw_typed_on_the_logins_own_page_is_kept(view):
    client, _ = view

    said = client.post("/browser/logins/import", json={
        **CANVAS, "cookies": _cookies(), "username": "ml433", "password": "Upper!Case9",
        "credentials_from": "secure.its.contoso.example"}).json()

    assert said["ok"] is True
    assert vaults.vault().credentials("contoso-canvas") == ("ml433", "Upper!Case9")


def test_a_password_typed_on_some_other_site_is_not_kept_and_nothing_is_saved(view):
    """The window reports where it saw them typed; a look-alike page's
    "Contoso" form is not Contoso's, and keeping it would later type a stranger's
    account in."""
    client, _ = view

    said = client.post("/browser/logins/import", json={
        **CANVAS, "cookies": _cookies(), "username": "ml433", "password": "x",
        "credentials_from": "contoso-login.example"}).json()

    assert said["ok"] is False and "contoso-login.example" in said["error"]
    assert vaults.vault().sites() == []


def test_a_logins_rule_changes_without_signing_in_again(view):
    """The maintainer, 4 October 2026: switching "ask first" to "just use it" should
    not mean redoing the login -- the session and password stay."""
    client, _ = view
    client.post("/browser/logins/import", json={**CANVAS, "cookies": _cookies(),
                                                "username": "ml433", "password": "pw"})

    changed = client.post("/browser/logins/tier", json={"name": "contoso-canvas",
                                                        "tier": "free"}).json()

    assert changed["ok"] is True and changed["site"]["tier"] == "free"
    store = vaults.vault()
    assert store.site("contoso-canvas").domains == ("contoso.instructure.com", "contoso.example")
    assert store.load_state("contoso-canvas") is not None
    assert store.credentials("contoso-canvas") == ("ml433", "pw")
    assert client.post("/browser/logins/tier", json={"name": "contoso-canvas",
                                                     "tier": "always"}).json()["ok"] is False
    assert client.post("/browser/logins/tier", json={"name": "ghost",
                                                     "tier": "ask"}).json()["ok"] is False
    assert store.site("contoso-canvas").tier == "free"


def test_a_page_from_anywhere_else_cannot_change_a_logins_rule(view):
    client, _ = view
    client.post("/browser/logins/import", json={**CANVAS, "cookies": _cookies()})

    refused = client.post("/browser/logins/tier", json={"name": "contoso-canvas", "tier": "free"},
                          headers={"origin": "https://attacker.example"})

    assert refused.status_code == 403 and vaults.vault().site("contoso-canvas").tier == "ask"


@pytest.mark.parametrize("domain, belongs", [
    ("contoso.instructure.com", True), (".contoso.example", True), ("canvas.contoso.example", True),
    (".instructure.com", True), (".edu", False), ("evilcontoso.example", False),
    ("contoso.instructure.com.attacker.example", False), ("", False),
])
def test_a_cookie_belongs_to_a_login_only_if_its_pages_would_be_sent_it(domain, belongs):
    assert live.cookie_belongs(domain, ["contoso.instructure.com", "contoso.example"]) is belongs


# --- what a save keeps, and how long the sandbox lives (8 October 2026 audit) ----------


def _jar() -> dict:
    """What a sandbox browser holds after a LinkedIn sign-in: the site's own
    cookies, twice for one of them (host-only and domain), and every other
    site its pages touched -- Google's, the bot-detection script's, an ad's."""
    return {"cookies": [
        {"name": "li_at", "value": "old", "domain": "www.linkedin.com", "path": "/",
         "expires": 1893456000},
        {"name": "li_at", "value": "new", "domain": ".www.linkedin.com", "path": "/",
         "expires": 1893456000},
        {"name": "bcookie", "value": "b", "domain": ".linkedin.com", "path": "/", "expires": -1},
        {"name": "NID", "value": "g", "domain": ".google.com", "path": "/", "expires": -1},
        {"name": "_px3", "value": "p", "domain": "ri.px-cloud.net", "path": "/", "expires": -1},
        {"name": "IDE", "value": "a", "domain": ".doubleclick.net", "path": "/", "expires": -1},
    ], "origins": [
        {"origin": "https://www.linkedin.com", "localStorage": [{"name": "k", "value": "v"}]},
        {"origin": "https://accounts.google.com", "localStorage": [{"name": "g", "value": "x"}]},
    ]}


def test_a_login_saved_from_the_live_view_keeps_only_its_own_cookies_and_storage(view):
    """The live view stored the whole sandbox jar: 11 other sites' cookies
    were in their LinkedIn login after the 16:07 saves on 8 October 2026, and
    every session started from it carried them. An import from the sign-in
    window and a save-back were already held to the login's own."""
    client, session = view
    session.answers["export_state"] = {"ok": True, "url": "https://www.linkedin.com/feed/",
                                       "state": _jar()}
    client.post("/browser/live/open", json={"url": "https://www.linkedin.com/"})

    said = client.post("/browser/live/save", json={"name": "linkedin",
                                                   "domains": "linkedin.com"}).json()

    assert said["ok"] is True
    state = vaults.vault().load_state("linkedin")
    assert sorted((c["name"], c["domain"]) for c in state["cookies"]) == [
        ("bcookie", ".linkedin.com"), ("li_at", ".www.linkedin.com")]
    assert [c["value"] for c in state["cookies"] if c["name"] == "li_at"] == ["new"], (
        "a cookie held twice is kept once, the one with the later claim")
    assert [o["origin"] for o in state["origins"]] == ["https://www.linkedin.com"]


def test_a_save_from_a_browser_holding_none_of_the_logins_cookies_writes_nothing(view):
    client, session = view
    session.answers["export_state"] = {"ok": True, "url": "https://www.linkedin.com/",
                                       "state": {"cookies": _jar()["cookies"][3:], "origins": []}}
    client.post("/browser/live/open", json={"url": "https://www.linkedin.com/"})

    said = client.post("/browser/live/save", json={"name": "linkedin",
                                                   "domains": "linkedin.com"}).json()

    assert said["ok"] is False and "no cookies for these domains" in said["error"]
    assert vaults.vault().sites() == []


class _Sandbox:
    """A live-view browser that is there, as `sessions.open_sandbox` makes one."""

    def __init__(self) -> None:
        self.closed = False

    def alive(self, now=None) -> bool:
        return not self.closed

    async def close(self) -> None:
        self.closed = True

    async def ask(self, cmd, **args):
        return {"ok": True}


@pytest.fixture
def clean_live(monkeypatch):
    """The module's own state, as a test finds it and leaves it."""
    for name in ("_live", "_used", "_close_at", "_opening"):
        monkeypatch.setattr(live, name, [])


def test_the_live_browser_closes_a_minute_after_a_save_and_not_before(view, clean_live):
    """Nothing more is wanted of it once the login is saved; it ran 28 minutes
    on 8 October 2026, until the container's own timeout."""
    import asyncio
    import time

    client, _ = view
    box = _Sandbox()
    live._live[:] = [box]
    live._used[:] = [time.monotonic()]  # in use just now: only the save can close it
    client.post("/browser/live/open", json={"url": "https://cas.contoso.example/"})
    client.post("/browser/live/save", json={"name": "contoso", "domains": "contoso.example"})
    due = live._close_at[0]

    assert live.SAVE_GRACE_S - 30 < due - time.monotonic() <= live.SAVE_GRACE_S, (
        "about a minute from the save, so she still sees the page she saved from")
    assert asyncio.run(live.sweep(now=due - 1)) is False and not box.closed
    assert asyncio.run(live.sweep(now=due + 1)) is True and box.closed and live._live == []


async def test_a_click_after_a_save_keeps_the_live_browser_open(clean_live):
    """The click marks the real clock, so the sweep is asked a second after
    it. Asked at a made-up 5,001 s, it found the click more than `IDLE_S`
    old on any computer up for less than 73 minutes, and closed the browser:
    CI's runner (22 October 2026)."""
    import time

    base = time.monotonic()
    live._live[:] = [_Sandbox()]
    live._close_at[:] = [base + 30]

    await live._session(create=False)

    assert live._close_at == [] and await live.sweep(now=base + 31) is False


async def test_a_frame_poll_does_not_keep_the_live_browser_alive_but_their_hands_do(clean_live):
    """The page asks for a picture every 350 ms for as long as it is open, and
    each ask reset the idle clock: a sandbox the user had left stayed for as long as
    a tab did."""
    box = _Sandbox()
    live._live[:] = [box]
    live._used[:] = [1_000.0]

    await live._session(create=False, touch=False)

    assert live._used == [1_000.0]
    assert live.IDLE_S <= 15 * 60, "an abandoned live view is gone within a quarter hour"
    assert await live.sweep(now=1_000.0 + live.IDLE_S + 1) is True and box.closed

    live._live[:] = [_Sandbox()]
    live._used[:] = [1_000.0]
    await live._session(create=False)
    assert live._used[0] > 1_000.0


def test_the_apps_picture_poll_is_not_use_but_their_click_is_through_the_real_routes(clean_live):
    """The route the app calls every 350 ms, not only the helper under it."""
    box = _Sandbox()
    live._live[:] = [box]
    live._used[:] = [1_000.0]
    app = Starlette(middleware=[Middleware(localonly.LocalOnly)], routes=live.ROUTES)
    client = _client(app)

    for _ in range(3):
        assert client.get("/browser/live/frame").json()["ok"] is True
    assert live._used == [1_000.0]

    assert client.post("/browser/live/act", json={"kind": "key", "key": "Enter"}).json()["ok"]
    assert live._used[0] > 1_000.0


async def test_two_opens_at_once_start_one_sandbox(clean_live, monkeypatch):
    import asyncio

    from agent.browser import session as sessions

    started, gate = [], asyncio.Event()

    async def open_sandbox(key, **start):
        started.append(key)
        await gate.wait()
        return _Sandbox()

    monkeypatch.setattr(sessions, "open_sandbox", open_sandbox)
    first = asyncio.ensure_future(live._session(create=True))
    second = asyncio.ensure_future(live._session(create=True))
    await asyncio.sleep(0)
    gate.set()

    one, two = await first, await second

    assert one is two and len(started) == 1


async def test_an_open_whose_page_goes_away_half_way_still_leaves_a_sandbox_that_is_swept(
        clean_live, monkeypatch):
    """The orphan of 8 October 2026: a sandbox started for a request that was
    cancelled, held by nothing, ran until its container's timeout."""
    import asyncio

    from agent.browser import session as sessions

    gate = asyncio.Event()

    async def open_sandbox(key, **start):
        await gate.wait()
        return _Sandbox()

    monkeypatch.setattr(sessions, "open_sandbox", open_sandbox)
    waiting = asyncio.ensure_future(live._session(create=True))
    await asyncio.sleep(0)
    waiting.cancel()
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)

    assert waiting.cancelled()
    assert len(live._live) == 1, "the sandbox that was started is owned, and so is swept"
