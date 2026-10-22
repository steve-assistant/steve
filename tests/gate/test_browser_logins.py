"""Their saved logins are typed only on their own site, never seen by a model,
and saved only by them.

The maintainer, 1 October 2026: the user logs in to a site the first time themselves, at the
laptop; the agent may then use the saved session, or have their username and
password typed in without ever seeing them; each login asks them first
(`ask`) or may just be used (`free`). Frozen, so the night cannot loosen
it. Nothing here touches their keychain, DPAPI, Docker or a real page: the
vault is given a folder, keychain and cipher of the test's own, and the
driver is driven against small fake pages. The design is
the browser add-on in docs/security-model.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import localapi, localonly
from core.browser import driver
from core.browser import vault as vaults

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Where a login belongs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url, ok", [
    ("https://cas.contoso.example/cas/login", True),
    ("https://contoso.example/", True),
    ("https://CONTOSO.EXAMPLE./x", True),
    ("https://evilcontoso.example/", False),
    ("https://contoso.example.example.com/", False),
    ("https://contoso.examples/", False),
    ("", False),
    ("not a url", False),
])
def test_a_login_belongs_only_to_its_own_domains(url, ok):
    assert driver.on_site(url, ["contoso.example"]) is ok


USER = {"ref": "e1", "tag": "input", "type": "text", "label": "NetID"}
PASSWORD = {"ref": "e2", "tag": "input", "type": "password", "label": "Password"}
CONTOSO = ["contoso.example"]


def test_their_login_is_typed_on_its_own_https_page():
    assert driver.login_refusal("https://cas.contoso.example/cas/login", USER, PASSWORD,
                                CONTOSO) is None


@pytest.mark.parametrize("url, user, password, why", [
    ("https://cas.contoso.example.example.com/login", USER, PASSWORD, "not one of the login's own"),
    ("https://contoso-login.example/", USER, PASSWORD, "not one of the login's own"),
    ("http://cas.contoso.example/login", USER, PASSWORD, "https"),
    ("https://cas.contoso.example/", USER, {**PASSWORD, "type": "text"}, "not a password box"),
    ("https://cas.contoso.example/", USER, {**PASSWORD, "tag": "textarea"}, "not a password box"),
    ("https://cas.contoso.example/", PASSWORD, PASSWORD, "username field"),
    ("https://cas.contoso.example/", {**USER, "tag": "textarea"}, PASSWORD, "username field"),
    ("https://cas.contoso.example/", USER, None, "both fields"),
    ("https://cas.contoso.example/", {**USER, "disabled": True}, PASSWORD, "disabled"),
])
def test_their_login_is_typed_nowhere_else(url, user, password, why):
    """A look-alike page, plain http, or a "password" field that is really a
    search box gets nothing."""
    assert why in (driver.login_refusal(url, user, password, CONTOSO) or "")


def test_what_is_in_a_field_is_never_reported_as_its_label():
    """A filled password is a field's value. Only a button's value -- its
    caption -- may be read back."""
    assert "|| el.value ||" not in driver.MARK
    assert "? el.value : ''" in driver.MARK


# ---------------------------------------------------------------------------
# The driver, against fake pages
# ---------------------------------------------------------------------------


class _Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def fill(self, value, timeout=None):
        self.page.filled.append((self.selector, value))


class _Page:
    def __init__(self, url, elements):
        self.url = url
        self.elements = elements
        self.filled: list = []
        self.went: list = []
        self.frames: list = []

    async def evaluate(self, script, *args):
        return [dict(e) for e in self.elements]

    def locator(self, selector):
        return _Locator(self, selector)

    async def goto(self, url, **kwargs):
        self.went.append(url)

    async def title(self):
        return "t"


def _driver(url: str, elements=(USER, PASSWORD), domains=()) -> driver.Driver:
    d = driver.Driver(playwright=None)
    d.page = _Page(url, list(elements))
    d.domains = list(domains)
    return d


async def test_the_driver_types_their_login_on_its_own_site_and_says_nothing_of_it():
    d = _driver("https://cas.contoso.example/cas/login")

    said = await d.run({"cmd": "fill_login", "user_ref": "e1", "pass_ref": "e2",
                        "username": "ml123", "password": "hunter2", "domains": CONTOSO})

    assert said == {"ok": True, "url": "https://cas.contoso.example/cas/login"}
    assert [v for _, v in d.page.filled] == ["ml123", "hunter2"]
    assert "hunter2" not in repr(said) and "ml123" not in repr(said)


async def test_the_driver_types_nothing_on_a_look_alike():
    d = _driver("https://cas.contoso.example.example.com/login")

    said = await d.run({"cmd": "fill_login", "user_ref": "e1", "pass_ref": "e2",
                        "username": "ml123", "password": "hunter2", "domains": CONTOSO})

    assert said["ok"] is False and d.page.filled == []
    assert "hunter2" not in repr(said)


async def test_a_saved_login_is_only_ever_opened_on_its_own_site():
    d = driver.Driver(playwright=None)

    said = await d.run({"cmd": "start", "proxy": "http://172.18.0.2:3128",
                        "storage_state": {"cookies": []}})

    assert said["ok"] is False and "its own site" in said["error"]


async def test_a_logged_in_browser_is_not_sent_to_another_site():
    d = _driver("https://cas.contoso.example/", domains=CONTOSO)

    said = await d.run({"cmd": "open", "url": "https://attacker.example/?d=grades"})

    assert said["ok"] is False and d.page.went == []


class _Route:
    def __init__(self, url, navigation=True, top=True):
        frame = type("F", (), {"parent_frame": None if top else object()})()
        self.request = type("R", (), {"url": url, "frame": frame,
                                      "is_navigation_request": lambda self: navigation})()
        self.outcome = None

    async def abort(self, reason="failed"):
        self.outcome = "aborted"

    async def continue_(self):
        self.outcome = "continued"


@pytest.mark.parametrize("url, navigation, top, outcome", [
    ("https://attacker.example/?d=grades", True, True, "aborted"),
    ("https://contoso.example.example.com/", True, True, "aborted"),
    ("https://cas.contoso.example/next", True, True, "continued"),
    ("https://cdn.example/app.js", False, True, "continued"),
    ("https://maps.example/embed", True, False, "continued"),
])
async def test_a_logged_in_page_cannot_carry_itself_off_its_site(url, navigation, top, outcome):
    """A link, a script or a redirect taking the page elsewhere is stopped,
    so what it shows cannot ride out in an address. What the page loads for
    itself, and frames inside it, are its own business."""
    d = _driver("https://cas.contoso.example/", domains=CONTOSO)
    route = _Route(url, navigation, top)

    await d._stay_on_site(route)

    assert route.outcome == outcome


async def test_a_session_is_only_saved_from_its_own_site():
    d = _driver("https://attacker.example/", domains=CONTOSO)

    said = await d.run({"cmd": "export_state", "domains": CONTOSO})

    assert said["ok"] is False and "state" not in said


# The desktop app's sign-in window (the maintainer, 4 October 2026): the user signs in there
# rather than in the sandbox, and its cookies come to the live view's
# `import_login` -- held to the sandbox's rule, and to its domains only.

def _import(store, monkeypatch, body: dict) -> dict:
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.testclient import TestClient

    from agent.browser import live

    monkeypatch.setattr(vaults, "vault", lambda: store)
    localapi.accept("a" * 64)
    app = Starlette(middleware=[Middleware(localonly.LocalOnly)], routes=live.ROUTES)
    client = TestClient(app, base_url="http://127.0.0.1",
                        headers={"origin": "tauri://localhost",
                                 "authorization": "Bearer " + "a" * 64})
    return client.post("/browser/logins/import", json=body).json()


SIGNED_IN = [
    {"name": "canvas_session", "value": "c1", "domain": "contoso.instructure.com"},
    {"name": "CASTGC", "value": "tgt", "domain": ".contoso.example"},
    {"name": "tracker", "value": "t", "domain": ".attacker.example"},
    {"name": "NID", "value": "g", "domain": ".google.com"},
]


def test_a_sign_in_windows_session_is_only_saved_from_its_own_site(store, monkeypatch):
    said = _import(store, monkeypatch, {
        "name": "contoso-canvas", "domains": "contoso.instructure.com contoso.example",
        "url": "https://attacker.example/landing", "cookies": SIGNED_IN})

    assert said["ok"] is False
    assert store.sites() == [] and store.load_state("contoso-canvas") is None


def test_only_the_logins_own_cookies_are_kept_from_a_sign_in_window(store, monkeypatch):
    said = _import(store, monkeypatch, {
        "name": "contoso-canvas", "domains": "contoso.instructure.com contoso.example",
        "url": "https://contoso.instructure.com/", "cookies": SIGNED_IN})

    assert said["ok"] is True
    kept = {c["name"] for c in store.load_state("contoso-canvas")["cookies"]}
    assert kept == {"canvas_session", "CASTGC"}


@pytest.mark.parametrize("key", ["Enter", "Shift+Tab", "ArrowLeft"])
async def test_their_keys_are_key_names(key):
    pressed = []
    d = _driver("https://cas.contoso.example/")
    d.page.keyboard = type("K", (), {"press": lambda self, k: _note(pressed, k)})()

    assert (await d.run({"cmd": "human_key", "key": key}))["ok"] and pressed == [key]


async def _note(into: list, value) -> None:
    into.append(value)


async def test_anything_else_is_not_a_key():
    d = _driver("https://cas.contoso.example/")

    said = await d.run({"cmd": "human_key", "key": "a'); fetch('x"})

    assert said["ok"] is False


SIGN_IN = {"ref": "e3", "tag": "button", "type": "submit", "label": "Log in"}


@pytest.mark.parametrize("button", [
    SIGN_IN, {**SIGN_IN, "label": "Sign in"}, {**SIGN_IN, "label": "Continue"},
    {"tag": "input", "type": "submit", "label": "LOGIN"}, {**SIGN_IN, "label": "Next >"},
])
def test_the_sign_in_button_may_be_pressed_with_their_login(button):
    assert driver.sign_in_refusal(button) is None


@pytest.mark.parametrize("button", [
    {**SIGN_IN, "label": "Sign in and post"}, {**SIGN_IN, "label": "Delete account"},
    {"tag": "a", "href": "https://x", "label": "Log in"}, None,
])
def test_no_other_button_is_pressed_with_their_login(button):
    assert driver.sign_in_refusal(button)


async def test_their_login_and_its_sign_in_button_together():
    d = _driver("https://cas.contoso.example/cas/login", elements=(USER, PASSWORD, SIGN_IN))
    clicked = []

    async def click(self, timeout=None):
        clicked.append(self.selector)

    _Locator.click = click
    try:
        said = await d.run({"cmd": "fill_login", "user_ref": "e1", "pass_ref": "e2",
                            "submit_ref": "e3", "username": "ml123", "password": "hunter2",
                            "domains": CONTOSO})
    finally:
        del _Locator.click

    assert said["ok"] and clicked == ['[data-agent-ref="e3"]']


async def test_a_button_that_is_not_sign_in_gets_nothing_typed_before_it():
    d = _driver("https://cas.contoso.example/cas/login",
                elements=(USER, PASSWORD, {**SIGN_IN, "label": "Delete account"}))

    said = await d.run({"cmd": "fill_login", "user_ref": "e1", "pass_ref": "e2",
                        "submit_ref": "e3", "username": "ml123", "password": "hunter2",
                        "domains": CONTOSO})

    assert said["ok"] is False and d.page.filled == []


# ---------------------------------------------------------------------------
# Clicks while logged in (core/browser/clicks.py)
# ---------------------------------------------------------------------------

from core.browser import clicks  # noqa: E402


@pytest.mark.parametrize("element, tier, ruled", [
    ({"tag": "button", "label": "Delete post"}, "free", "card"),
    ({"tag": "a", "href": "https://x.com/logout", "label": "Logout"}, "free", "card"),
    ({"tag": "button", "label": "Follow"}, "free", "card"),
    ({"tag": "button", "label": "Cancel reservation"}, "free", "card"),
    ({"tag": "button", "label": "Accept invitation"}, "free", "card"),
    ({"tag": "a", "href": "https://x.com/inbox", "label": "Inbox"}, "free", "free"),
    ({"tag": "a", "href": "https://x.com/inbox", "label": "Inbox"}, "ask", "free"),
    ({"tag": "a", "href": "javascript:void(0)", "label": "More"}, "free", "judge"),
    ({"tag": "button", "label": "Show more"}, "free", "judge"),
    ({"tag": "button", "label": "Show more"}, "ask", "card"),
])
def test_what_a_logged_in_click_needs(element, tier, ruled):
    """A consequence named is always a card; a plain link is free; a login the user
    marked "ask" cards every other click; what is left is judged."""
    assert clicks.rule(element, tier) == ruled


@pytest.mark.parametrize("ruled, p, fate", [
    ("judge", None, "card"), ("judge", clicks.THRESHOLD, "card"), ("judge", 0.9, "card"),
    ("judge", 0.05, "free"), ("card", 0.0, "card"), ("free", 0.99, "free"),
])
def test_a_judge_never_frees_what_the_rules_card_and_its_silence_is_a_card(ruled, p, fate):
    assert clicks.verdict(ruled, p) == fate


async def test_an_approved_click_is_made_only_on_the_page_and_element_the_user_saw():
    element = {"ref": "e7", "tag": "button", "label": "Cancel reservation"}
    clicked = []

    async def click(self, timeout=None):
        clicked.append(self.selector)

    _Locator.click = click
    try:
        moved = _driver("https://resy.com/elsewhere", elements=(element,))
        relabelled = _driver("https://resy.com/r/1", elements=({**element, "label": "Delete"},))
        same = _driver("https://resy.com/r/1", elements=(element,))
        asked = {"cmd": "act", "expected_url": "https://resy.com/r/1", "ref": "e7",
                 "label": "Cancel reservation"}
        said = [await d.run(asked) for d in (moved, relabelled, same)]
    finally:
        del _Locator.click

    assert said[0]["ok"] is False and said[1]["ok"] is False and said[2]["ok"]
    assert clicked == ['[data-agent-ref="e7"]']


async def test_an_approved_click_on_a_page_that_turned_into_a_checkout_is_refused():
    """The card was for "Continue" on a booking; by the yes, the page asks
    for a card number. Payments in the browser are not enabled, so the
    driver looks again before it clicks, as it does before a submit."""
    element = {"ref": "e7", "tag": "button", "label": "Continue"}
    card_number = {"ref": "e9", "tag": "input", "label": "Card number"}
    clicked = []

    async def click(self, timeout=None):
        clicked.append(self.selector)

    _Locator.click = click
    try:
        checkout = _driver("https://resy.com/r/1", elements=(element, card_number))
        said = await checkout.run({"cmd": "act", "expected_url": "https://resy.com/r/1",
                                   "ref": "e7", "label": "Continue"})
    finally:
        del _Locator.click

    assert said["ok"] is False and "payments are not enabled" in said["error"]
    assert clicked == []


# ---------------------------------------------------------------------------
# The vault
# ---------------------------------------------------------------------------


class _Keys(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value

    def delete(self, key):
        self.pop(key, None)


def _flip(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)


@pytest.fixture
def store(tmp_path) -> vaults.Vault:
    made = vaults.Vault(root=tmp_path, keyring=_Keys(), protect=_flip, unprotect=_flip)
    # An empty keychain once read as "none given" and the real one was used.
    assert not isinstance(made.keyring, vaults._Keyring), "never her real keychain"
    return made


def test_a_saved_password_is_kept_out_of_every_log_line_set_or_read(store):
    """Typed through the driver and never given to a model, and no log line
    may hold it either (core/logscrub.py)."""
    from core import logscrub

    store.save_site("contoso", ["contoso.example"])
    store.set_credentials("contoso", "maya", "canary-pass")
    assert logscrub.scrub("tried canary-pass") == f"tried {logscrub.MARK}"
    logscrub._held.clear()  # as after a restart: saved before, read now
    assert store.credentials("contoso") == ("maya", "canary-pass")
    assert logscrub.scrub("tried canary-pass") == f"tried {logscrub.MARK}"


def test_a_login_asks_them_first_unless_the_user_said_otherwise(store):
    assert store.save_site("contoso", ["contoso.example"]).tier == "ask"
    assert store.save_site("opentable", ["opentable.com"], "free").tier == "free"
    with pytest.raises(ValueError):
        store.save_site("x", ["x.com"], "whenever")


@pytest.mark.parametrize("name, domains", [
    ("Contoso!", ["contoso.example"]), ("", ["contoso.example"]),
    ("contoso", ["edu"]), ("contoso", ["10.0.0.1"]), ("contoso", ["intranet.local"]),
    ("contoso", []), ("contoso", [f"d{i}.example.com" for i in range(vaults.MAX_DOMAINS + 1)]),
    ("test", ["herokuapp.com"]), ("blog", ["github.io"]), ("bbc", ["co.uk"]),
])
def test_a_login_is_a_name_and_real_domains(store, name, domains):
    """Including no public suffix: anyone can host under herokuapp.com, so a
    login saved for it would be typed into a look-alike there."""
    with pytest.raises(ValueError):
        store.save_site(name, domains)


@pytest.mark.parametrize("domain", ["the-internet.herokuapp.com", "bbc.co.uk", "contoso.example",
                                    "cas.contoso.example"])
def test_a_sites_own_domain_under_a_public_suffix_is_fine(store, domain):
    assert store.save_site("site", [domain]).domains == (domain,)


def test_the_most_specific_login_is_the_one_a_page_belongs_to(store):
    store.save_site("contoso", ["contoso.example"])
    store.save_site("canvas", ["canvas.contoso.example"])

    assert store.site_for("https://canvas.contoso.example/courses").name == "canvas"
    assert store.site_for("https://cas.contoso.example/").name == "contoso"
    assert store.site_for("https://evilcontoso.example/") is None


def test_their_password_is_kept_and_forgotten_and_never_listed(store):
    store.save_site("contoso", ["contoso.example"])
    store.set_credentials("contoso", "ml123", "hunter2")
    store.save_state("contoso", {"cookies": [{"name": "s", "value": "secret-cookie"}]})

    listed = [s.as_dict() for s in store.sites()]
    assert listed == [{"name": "contoso", "domains": ["contoso.example"], "tier": "ask",
                       "has_password": True, "has_session": True}]
    assert store.credentials("contoso") == ("ml123", "hunter2")
    assert store.forget("contoso")
    assert store.credentials("contoso") is None and store.load_state("contoso") is None


def test_a_password_the_credential_manager_will_not_delete_keeps_its_login_listed(
        store, monkeypatch):
    """Every failure to delete was swallowed as "not there", so a login the
    user forgot kept its password in the Credential Manager while the app
    said it was gone. Not there is still quiet; any other failure is said,
    and the login stays listed to be forgotten again."""
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError

    def missing(service, key):
        raise PasswordDeleteError("not found")

    def broken(service, key):
        raise KeyringError("the Credential Manager is not answering")

    monkeypatch.setattr(keyring, "delete_password", missing)
    vaults._Keyring().delete("contoso")

    real = vaults.Vault(root=store.root, keyring=vaults._Keyring())
    real.save_site("contoso", ["contoso.example"])
    monkeypatch.setattr(keyring, "delete_password", broken)
    with pytest.raises(KeyringError):
        real.forget("contoso")
    assert [s.name for s in real.listed()] == ["contoso"]


def test_a_saved_session_is_encrypted_on_disk(store):
    store.save_site("contoso", ["contoso.example"])

    store.save_state("contoso", {"cookies": [{"name": "s", "value": "secret-cookie"}]})

    raw = (store.root / "sessions" / "contoso.bin").read_bytes()
    assert b"secret-cookie" not in raw
    assert store.load_state("contoso") == {"cookies": [{"name": "s", "value": "secret-cookie"}]}


def test_nothing_is_kept_for_a_login_the_user_never_saved(store):
    with pytest.raises(ValueError):
        store.set_credentials("ghost", "u", "p")
    with pytest.raises(ValueError):
        store.save_state("ghost", {})


def test_the_real_vault_is_dpapi_and_its_own_keychain_service():
    from core import secrets

    real = vaults.Vault()
    assert real.protect is vaults._dpapi_protect and isinstance(real.keyring, vaults._Keyring)
    assert vaults.SERVICE != secrets.SERVICE


# ---------------------------------------------------------------------------
# Who may do what: read from the source, so a judged change to the mutable
# side cannot quietly add a caller
# ---------------------------------------------------------------------------


def _agent_sources() -> dict[str, str]:
    return {p.relative_to(REPO).as_posix(): p.read_text(encoding="utf-8")
            for root in ("agent", "tools", "channels", "voice")
            for p in (REPO / root).rglob("*.py")}


def _callers(pattern: str, *, of_the_vault: bool = False) -> set[str]:
    """Files whose source matches `pattern` -- with `of_the_vault`, only those
    that import the vault, since `.credentials(` is also Google's."""
    return {path for path, text in _agent_sources().items()
            if re.search(pattern, text)
            and (not of_the_vault or "core.browser import vault" in text
                 or "core.browser.vault" in text)}


def test_no_tool_a_model_calls_can_type_click_as_them_or_take_a_session():
    """Their hands in the live view, their login typed from the vault, a session
    saved for the vault: none is a command any tool sends."""
    assert _callers(r'"human_') <= {"agent/browser/live.py"}
    assert _callers(r'"fill_login"') <= {"agent/browser/session.py"}
    assert _callers(r'"export_state"') <= {"agent/browser/live.py", "agent/browser/session.py"}
    assert not any(p.startswith("agent/toolset/") for p in
                   _callers(r'"human_|"fill_login"|"export_state"'))


def test_only_their_live_view_saves_or_forgets_a_login():
    for method in ("save_site", "set_credentials", "forget"):
        assert _callers(rf"\.{method}\(", of_the_vault=True) <= {"agent/browser/live.py"}, method
    assert _callers(r"\.save_state\(", of_the_vault=True) <= {"agent/browser/live.py",
                                                              "agent/browser/session.py"}


def test_their_password_is_read_only_to_be_typed():
    assert _callers(r"\.credentials\(", of_the_vault=True) <= {"agent/browser/session.py"}


def test_the_live_view_is_behind_the_localhost_check():
    from agent.browser import live
    from agent.server import app

    assert any(m.cls is localonly.LocalOnly for m in app.user_middleware)
    paths = {r.path for r in app.routes}
    assert {r.path for r in live.ROUTES} <= paths
    assert localonly.refusal({"origin": "https://attacker.example", "host": "127.0.0.1:8790"})
    assert localonly.refusal({"host": "attacker.example:8790"})


def test_every_allowed_origin_is_this_machine():
    for origin in localonly.ALLOWED_ORIGINS:
        scheme, _, rest = origin.partition("://")
        host = rest.split(":")[0]
        assert scheme == "tauri" or host in ("localhost", "127.0.0.1", "tauri.localhost"), origin
