"""Adding, testing, changing and removing an account, as the app does it.

Each provider had its own way in (a command for Google, keys typed into the
keychain for a password mailbox, a switch for Outlook), and a second account of any
could not exist. These tests drive `agent/accounts.py` and its routes over
two test manifests and an engine with a dict behind it, so what they hold
is the frame's, whatever module later plugs into it (05-connectors.md,
sections 1 to 3).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from dataclasses import dataclass, field

import httpx
import pytest
from starlette.applications import Starlette

from agent import accounts, calendars, connectors, runtime
from agent.worker import accounts as account_routes
from core import credstore, db, health, modules, paths
from steve_sdk.errors import ConnectorError, SignInNeeded
from steve_sdk.manifest import parse
from tests.gate import mailfake, slotfake

MAILISH = parse({
    "manifest_version": 1, "sdk": 1, "id": "mailish", "label": "Mailish", "for": "Tests",
    "classes": ["mail", "calendar"], "domains": ["mailish.example"],
    "setup": {
        "steps": ["address", "preset", "fields", "test"],
        "fields": [{"name": "password", "type": "secret", "label": "App password"},
                   {"name": "server", "type": "host", "label": "Server", "optional": True},
                   {"name": "imap", "type": "host", "label": "IMAP", "optional": True},
                   {"name": "feed", "type": "link", "label": "Feed", "optional": True},
                   {"name": "folder", "type": "choice", "label": "Folder", "optional": True,
                    "choices": ["Inbox", "All"]}],
        "presets": [{"id": "partner", "label": "Partner", "domains": ["partner.example"],
                     "classes": ["mail"], "revoke": "https://partner.example/app-passwords"}],
        "revoke": "https://mailish.example/security",
    },
    "mail": {"engine": "imap", "imap": "imap.mailish.example:993",
             "smtp": "smtp.mailish.example:465", "every_min": 5},
    "calendar": {"engine": "ics"},
})
# The module the question's "no" picks, with the preset it names.
OTHER = parse({
    "manifest_version": 1, "sdk": 1, "id": "imap", "label": "Other mail", "for": "Tests",
    "classes": ["mail"],
    "setup": {"steps": ["address", "fields", "test"],
              "fields": [{"name": "password", "type": "secret", "label": "Password"}],
              "presets": [{"id": "other", "label": "Another provider"}]},
    "mail": {"engine": "imap", "imap": "imap.other.example:993",
             "smtp": "smtp.other.example:465"},
})
MANIFESTS = {"mailish": MAILISH, "imap": OTHER}
PASSWORD = "app-password-1729"


@dataclass
class World:
    """What the engine's accounts answer: `trouble` raised by every test,
    `ends` as every sign-in's known end, `answers_at` as the addresses every
    mailbox finds it answers at."""

    conn: object = None
    trouble: ConnectorError | None = None
    ends: dt.datetime | None = None
    answers_at: list[str] = field(default_factory=list)
    tested: list[str] = field(default_factory=list)


class Box(mailfake.Mailbox):
    def __init__(self, world: World, address: str) -> None:
        super().__init__(address)
        self.world = world

    def test(self) -> str:
        self.world.tested.append("mail")
        if self.world.trouble is not None:
            raise self.world.trouble
        return "the inbox, read"

    def expires(self) -> dt.datetime | None:
        return self.world.ends

    def addresses(self) -> list[str]:
        return list(self.world.answers_at)


class Diary(slotfake.Calendar):
    def __init__(self, world: World) -> None:
        super().__init__()
        self.world = world

    def test(self) -> str:
        self.world.tested.append("calendar")
        return "one calendar, read"


@pytest.fixture
def world(monkeypatch):
    found = World()
    monkeypatch.setattr(modules, "builtins", lambda: MANIFESTS)
    monkeypatch.setitem(connectors.ENGINES, "imap",
                        lambda ctx, spec: Box(found, ctx.account.address))
    monkeypatch.setitem(connectors.ENGINES, "ics", lambda ctx, spec: Diary(found))
    conn = db.open_store(paths.db_path())
    modules.register(conn)
    found.conn = conn
    yield found
    conn.close()


def _add(world: World, address: str = "maya@mailish.example", **kw) -> str:
    kw.setdefault("values", {"password": PASSWORD})
    return accounts.add(world.conn, module_id=kw.pop("module_id", "mailish"),
                        address=address, **kw)


# --- the address first -----------------------------------------------------------

def test_an_address_picks_its_module_by_a_presets_domain_before_the_modules_own():
    assert accounts.pick("maya@partner.example", MANIFESTS) == {
        "module": "mailish", "preset": "partner"}
    assert accounts.pick("Maya@Mailish.Example", MANIFESTS) == {
        "module": "mailish", "preset": None}


def test_an_address_no_manifest_claims_asks_the_one_question_with_each_answers_module():
    """Nothing is looked up over the network: an unknown domain is a question
    the user answers, and each answer names the module it opens."""
    asked = accounts.pick("maya@northwind.example", MANIFESTS)

    assert asked["question"] == accounts.QUESTION
    assert asked["no"] == {"module": "imap", "preset": "other"}
    assert asked["yes"] == {"module": "outlook_classic", "preset": None}


def test_what_is_not_an_email_address_picks_nothing():
    with pytest.raises(modules.AccountError, match="not an email address"):
        accounts.pick("maya at mailish", MANIFESTS)


def test_each_built_in_domain_picks_the_module_and_preset_that_claims_it():
    """The shipped manifests, not test ones: a domain claimed twice would be
    settled by the order the modules sort in, which no one chose."""
    claims: dict[str, list[tuple[str, str | None]]] = {}
    for module_id, manifest in modules.builtins().items():
        for domain in manifest.domains:
            claims.setdefault(domain, []).append((module_id, None))
        for preset in manifest.setup.presets:
            for domain in preset.domains:
                claims.setdefault(domain, []).append((module_id, preset.id))

    assert claims and all(len(by) == 1 for by in claims.values()), claims
    for domain, [(module_id, preset)] in claims.items():
        assert accounts.pick(f"maya@{domain}") == {"module": module_id, "preset": preset}


def test_google_cloud_is_never_what_an_address_picks_only_an_addition_or_the_fallback():
    """A Gmail address starts with an app password: two minutes, against a
    quarter hour in the user's own Cloud project. Google Cloud is for what
    an app password cannot reach (Drive, Tasks, guests), chosen under
    Advanced, or where an admin has turned app passwords off, which the
    Gmail module's own words say (`test_imap_engine`)."""
    built_ins = modules.builtins()
    asked = accounts.pick("maya@northwind.example")

    assert accounts.pick("maya.lindqvist@gmail.com")["module"] == "gmail_password"
    assert {asked["yes"]["module"], asked["no"]["module"]} == {"outlook_classic", "imap"}
    google = built_ins["google_cloud"]
    assert not google.domains and not any(p.domains for p in google.setup.presets)
    blocked = [e.tell for e in built_ins["gmail_password"].setup.errors
               if "disabled" in e.says.lower()]
    assert blocked and all("Google Cloud" in tell for tell in blocked)


# --- the fields ------------------------------------------------------------------

def test_each_field_is_checked_by_its_type_and_a_secret_kept_apart():
    kept, secret = accounts.fields(MAILISH, {
        "password": f" {PASSWORD} ", "server": " IMAP.Mailish.Example:993 ",
        "feed": "https://mailish.example/feed.ics", "folder": "All"})

    assert secret == {"password": f" {PASSWORD} "}, "a secret is kept as typed"
    assert kept == {"server": "imap.mailish.example:993",
                    "feed": "https://mailish.example/feed.ics", "folder": "All"}


@pytest.mark.parametrize("values, says", [
    ({}, "App password is needed"),
    ({"password": PASSWORD, "colour": "red"}, "asks for no colour"),
    ({"password": PASSWORD, "server": "mail server"}, "not a server's name"),
    ({"password": PASSWORD, "server": "imap.mailish.example:99999"}, "not a server's name"),
    ({"password": PASSWORD, "feed": "http://mailish.example/feed.ics"}, "https://"),
    ({"password": PASSWORD, "folder": "Spam"}, "one of Inbox, All"),
    ({"password": 1729}, "is text"),
    ({"password": "x" * (accounts.MAX_SECRET + 1)}, "too long"),
])
def test_a_field_that_is_missing_unknown_or_not_its_type_is_refused_saying_which(values, says):
    with pytest.raises(modules.AccountError, match=says):
        accounts.fields(MAILISH, values)


@pytest.mark.parametrize("values", [
    {"password": PASSWORD, "username": "maya\r\nA2 DELETE INBOX"},
    {"password": f"{PASSWORD}\n"},
    {"password": PASSWORD, "url": "https://dav.mailish.example/\r\nX-Other: 1"},
])
def test_a_fields_text_with_a_line_break_in_it_is_refused(values):
    """A user name or a password goes into a mail server's command line,
    and a link into a request's: a line break would end that line and
    start one of its own. In a password pasted with its line's end, it is
    a character the user never typed, and the sign-in fails later with
    less to say."""
    imap = modules.read_builtins()["imap"]
    with pytest.raises(modules.AccountError, match="no line break"):
        accounts.fields(imap, values)


def test_an_app_password_is_kept_without_its_spaces_and_a_main_password_is_refused():
    """Google shows an app password in four groups of four, and it is pasted
    that way; a main password pasted instead fails a sign-in later, with
    less to say, so its form is checked here."""
    gmail = modules.read_builtins()["gmail_password"]

    _, secret = accounts.fields(gmail, {"app_password": " abcd efgh ijkl mnop "})
    assert secret == {"app_password": "abcdefghijklmnop"}
    with pytest.raises(modules.AccountError, match="should be 16 letters"):
        accounts.fields(gmail, {"app_password": "correct-horse-battery"})


@pytest.mark.parametrize("module_id", sorted(modules.read_builtins()))
def test_what_a_preset_fills_is_asked_only_under_advanced(module_id):
    """Setup starts from what the user knows (05-connectors.md, principle
    10): an iCloud user was shown five server fields, IMAP and CalDAV by
    name, that the preset had already filled."""
    setup = modules.read_builtins()[module_id].setup
    filled = {name for preset in setup.presets for settings in preset.settings.values()
              for name in settings}
    shown = {f.name for f in setup.fields if not f.advanced}
    assert not filled & shown
    words = re.compile(r"\b(IMAP|SMTP|TLS|STARTTLS|CalDAV)\b")
    assert not [f.label for f in setup.fields if not f.advanced and words.search(f.label)]


def test_a_server_the_user_types_may_be_at_home_or_here_and_is_pinned_to_that_kind(world):
    """A mail bridge on this computer or a server at home is the user's to
    name (05-connectors.md, decision 13); the kind of address it
    resolves to at setup is kept, so `ctx.tcp` refuses it if a public name
    later points at this computer."""
    answers = {"nas.home.example": ["192.168.1.20"], "localhost": ["127.0.0.1"],
               "split.example": ["192.168.1.20", "93.184.216.34"]}

    def resolve(host, port):
        return answers[host]

    home = _add(world, values={"password": PASSWORD, "server": "nas.home.example:993"},
                resolve=resolve)
    here = _add(world, "maya@partner.example",
                values={"password": PASSWORD, "server": "localhost:1143"}, resolve=resolve)
    assert modules.account(world.conn, home).fields["hosts"] == {"nas.home.example": "private"}
    assert modules.account(world.conn, here).fields["hosts"] == {"localhost": "loopback"}
    with pytest.raises(modules.AccountError, match="both inside your network and outside"):
        _add(world, "ada@mailish.example",
             values={"password": PASSWORD, "server": "split.example"}, resolve=resolve)


def test_no_value_can_set_the_pinned_hosts_and_no_server_is_typed_over_a_fixed_one(world):
    """The pins are setup's to write: a value named `hosts` is refused like
    any field the manifest does not ask for, and a server typed where the
    module names its own would be pinned yet never used."""
    with pytest.raises(modules.AccountError, match="asks for no hosts"):
        _add(world, values={"password": PASSWORD, "hosts": {"evil.example": "loopback"}})
    with pytest.raises(modules.AccountError, match="already knows its servers"):
        _add(world, values={"password": PASSWORD, "imap": "evil.example:993"},
             resolve=lambda host, port: ["93.184.216.34"])


# --- adding ----------------------------------------------------------------------

def test_a_new_account_is_named_by_its_address_watched_at_its_manifests_interval(world):
    key = _add(world)

    row = modules.account(world.conn, key)
    assert row.name == "maya@mailish.example"
    assert row.classes == ("mail", "calendar")
    assert row.settings == {"watched": True, "every_min": 5}
    held = modules.module(world.conn, "mailish")
    assert credstore.read(credstore.target(held.key, key, "password")) == PASSWORD
    assert "password" not in row.fields


def test_an_account_takes_the_presets_classes_unless_the_user_chose(world):
    partner = _add(world, "maya@partner.example", preset="partner")
    chosen = _add(world, "maya@mailish.example", wanted=["calendar"])

    assert modules.account(world.conn, partner).classes == ("mail",)
    assert modules.account(world.conn, partner).preset == "partner"
    assert modules.account(world.conn, chosen).classes == ("calendar",)


def test_no_account_is_added_to_a_module_switched_off_or_an_engine_not_here_yet(
        world, monkeypatch):
    modules.set_enabled(world.conn, "mailish", False)
    with pytest.raises(modules.AccountError, match="switched off"):
        _add(world)
    modules.set_enabled(world.conn, "mailish", True)
    monkeypatch.delitem(connectors.ENGINES, "ics")
    with pytest.raises(modules.AccountError, match="cannot add its calendar yet"):
        _add(world)

    assert modules.accounts(world.conn, live_only=False) == []


def test_an_address_the_module_needs_and_a_preset_it_lacks_are_refused(world):
    with pytest.raises(modules.AccountError, match="needs the account's email address"):
        _add(world, "")
    with pytest.raises(modules.AccountError, match="has no 'icloud'"):
        _add(world, preset="icloud")


def test_an_account_whose_secret_could_not_be_kept_is_not_added(world, monkeypatch):
    """Half an account, its row without its password, would fail every look
    with nothing to say why."""
    def refused(name, value):
        raise OSError("the Credential Manager is locked")

    monkeypatch.setattr(credstore, "write", refused)
    with pytest.raises(OSError):
        _add(world)

    assert modules.accounts(world.conn, live_only=False) == []


# --- testing ---------------------------------------------------------------------

async def test_setups_test_reads_each_class_once_and_records_the_account_working(world):
    key = _add(world)

    tested = await accounts.test(world.conn, key)

    assert tested == {"ok": True, "found": ["the inbox, read", "one calendar, read"],
                      "health": "maya@mailish.example is working.", "state": health.WORKING,
                      "detail": "", "fix": ""}
    assert sorted(world.tested) == ["calendar", "mail"]
    assert health.read(world.conn, key).state == health.WORKING


async def test_the_addresses_a_tested_account_finds_are_its_own_or_fail_its_test(world):
    """Classic Outlook's profile can hold several accounts, and Outlook
    answers for them all as one. Each address it finds is kept as that
    account's after a passing test, so a reply to mail sent to any of them
    goes from it. Until step 5's item 10 they were asked of Outlook at every
    turn. One another account already holds fails the test, saying which,
    and none of the addresses found with it is kept."""
    key = _add(world)
    held = _add(world, "rina@mailish.example")
    world.answers_at = ["Maya@Side.example", "maya@mailish.example"]

    tested = await accounts.test(world.conn, key)

    assert tested["ok"] is True
    assert modules.account(world.conn, key).addresses == (
        "maya@mailish.example", "maya@side.example")
    assert modules.holder(world.conn, "calendar", "maya@side.example").key == key

    world.answers_at = ["maya@new.example", "rina@mailish.example"]
    tested = await accounts.test(world.conn, key)

    assert tested["ok"] is False and 'connected as "rina@mailish.example"' in tested["detail"]
    assert modules.holder(world.conn, "mail", "maya@new.example") is None
    assert modules.holder(world.conn, "mail", "rina@mailish.example").key == held


async def test_the_first_tested_calendar_steve_can_write_to_becomes_the_default(world):
    first = _add(world)
    second = _add(world, "rina@mailish.example")

    await accounts.test(world.conn, first)
    await accounts.test(world.conn, second)

    assert modules.account(world.conn, first).settings["default_calendar"] is True
    assert "default_calendar" not in modules.account(world.conn, second).settings
    assert calendars.default(world.conn).key == first


async def test_a_lapsed_sign_in_is_recorded_as_needing_the_user_in_one_sentence(world):
    key = _add(world)
    world.trouble = SignInNeeded("token expired")

    tested = await accounts.test(world.conn, key)

    said = ("maya@mailish.example needs you: sign in again, in Steve's app, "
            "under Settings: Accounts.")
    assert tested["ok"] is False and tested["state"] == health.NEEDS_YOU
    assert tested["health"] == said
    assert accounts.unchecked(world.conn) == [said]
    assert connectors.say(SignInNeeded(), "maya@mailish.example") == said, \
        "a tool's error says what the app says"


async def test_an_account_switched_off_is_off_and_not_said_to_be_unchecked(world):
    key = _add(world)
    world.trouble = SignInNeeded()
    await accounts.test(world.conn, key)

    accounts.edit(world.conn, key, enabled=False)

    assert health.read(world.conn, key).state == health.OFF
    assert accounts.unchecked(world.conn) == []
    checked = await accounts.check_all(world.conn)
    assert checked[key].state == health.OFF
    assert world.tested == ["mail"], "an account switched off is not tried"


# --- changing it -----------------------------------------------------------------

def test_a_rename_keeps_the_key_every_id_carries(world):
    key = _add(world)

    accounts.edit(world.conn, key, name="Work")

    assert modules.account(world.conn, key).name == "Work"
    with pytest.raises(modules.AccountError, match="already named"):
        accounts.edit(world.conn, _add(world, "rina@mailish.example"), name="work")


@pytest.mark.parametrize("changes, says", [
    ({"every_min": 0}, "from 1 to 30"),
    ({"every_min": 31}, "from 1 to 30"),
    ({"every_min": True}, "from 1 to 30"),
    ({"every_min": 2.5}, "from 1 to 30"),
    ({"watched": "yes"}, "on or off"),
    ({"colour": "red"}, "colour"),
])
def test_a_setting_out_of_its_bounds_or_unknown_is_refused(world, changes, says):
    key = _add(world)

    with pytest.raises(modules.AccountError, match=says):
        accounts.edit(world.conn, key, settings=changes)

    assert modules.account(world.conn, key).settings == {"watched": True, "every_min": 5}


def test_one_calendar_is_the_default_and_marking_another_moves_it(world):
    first, second = _add(world), _add(world, "rina@mailish.example")
    accounts.edit(world.conn, first, settings={"default_calendar": True})

    accounts.edit(world.conn, second, settings={"default_calendar": True})

    assert modules.account(world.conn, second).settings["default_calendar"] is True
    assert not modules.account(world.conn, first).settings.get("default_calendar")


def test_an_account_not_watched_is_not_polled_and_one_watched_at_its_own_interval(world):
    from agent.attention import sources

    key = _add(world)
    accounts.edit(world.conn, key, settings={"every_min": 12})
    [mail] = [s for s in sources.all_sources(world.conn) if s.name == f"mail:{key}"]
    assert mail.every_s == 12 * 60

    accounts.edit(world.conn, key, settings={"watched": False})

    assert f"mail:{key}" not in {s.name for s in sources.all_sources(world.conn)}


def test_moving_a_class_takes_it_from_the_account_that_held_the_address(world):
    home = _add(world)
    work = _add(world, "rina@mailish.example", wanted=["calendar"])

    accounts.move(world.conn, klass="mail", address="maya@mailish.example", to_key=work)

    assert modules.account(world.conn, home).classes == ("calendar",)
    assert set(modules.account(world.conn, work).classes) == {"mail", "calendar"}


def test_an_address_held_is_refused_unless_taken_and_a_holder_left_empty_goes(world):
    """One address has one account per class: adding it again is refused,
    saying where it is, or takes it with `take`."""
    first = _add(world, wanted=["mail"])
    with pytest.raises(modules.AccountError, match="already connected"):
        _add(world, name="Again", wanted=["mail"])

    again = _add(world, name="Again", wanted=["mail"], take=True)

    assert modules.account(world.conn, first) is None, "left with nothing, it went"
    assert modules.account(world.conn, again).classes == ("mail",)


def test_one_gmail_mailbox_written_two_ways_is_one_address_held_once(world):
    """Gmail delivers maya.lindqvist@gmail.com and mayalindqvist@googlemail.com
    to one mailbox: a second account at the other spelling would show its
    every message twice, as one at the same address would."""
    first = _add(world, "maya.lindqvist@gmail.com", wanted=["mail"])
    with pytest.raises(modules.Held, match="already connected"):
        _add(world, "mayalindqvist@googlemail.com", name="Again", wanted=["mail"])

    again = _add(world, "mayalindqvist@googlemail.com", name="Again", wanted=["mail"],
                 take=True)

    assert modules.account(world.conn, first) is None, "taken, the first was left empty"
    assert modules.holder(world.conn, "mail", "maya.lindqvist@gmail.com").key == again
    assert modules.holder(world.conn, "mail", "maya.lindqvist+steve@gmail.com") is None


def test_an_account_at_an_address_already_named_is_told_apart_and_a_move_keeps_the_address(
        world):
    """The app sends no name, so an account is named by its address, and
    names are one each: a second account at one address, and "Move the mail
    for this address here", both failed on a name the user never chose
    (found in the app's preview on 15 October)."""
    first = _add(world, wanted=["mail"])
    beside = _add(world, wanted=["calendar"])
    moved = _add(world, wanted=["mail"], take=True)

    assert modules.account(world.conn, beside).name == "maya@mailish.example (Mailish)"
    assert modules.account(world.conn, first) is None, "emptied by the move, it went"
    assert modules.account(world.conn, moved).name == "maya@mailish.example", \
        "the address is free again once the account that held it went"


# --- removing it -----------------------------------------------------------------

def _rows(world: World, key: str) -> dict[str, int]:
    from tests.gate.test_off_means_original import ROWS

    with db.transaction(world.conn):
        return {table: make(world.conn, key, f"{table}-{key}") for table, make in ROWS.items()}


def _there(world: World, table: str, rowid: int) -> tuple | None:
    return world.conn.execute(f"SELECT * FROM {table} WHERE rowid = ?", (rowid,)).fetchone()


@pytest.mark.parametrize("forget", [False, True])
def test_removing_an_account_drops_its_queued_work_and_keeps_or_forgets_what_was_learned(
        world, forget):
    key, other = _add(world), _add(world, "rina@mailish.example")
    held = modules.module(world.conn, "mailish")
    state = modules.account_state_dir("mailish", key)
    state.mkdir(parents=True)
    (state / "cursor").write_text("42", encoding="utf-8")
    gone, kept = _rows(world, key), _rows(world, other)
    health.record(world.conn, key, kind="transient")
    person = world.conn.execute("SELECT person_id FROM interactions WHERE rowid = ?",
                                (gone["interactions"],)).fetchone()[0]

    accounts.remove(world.conn, key, forget=forget)

    assert modules.account(world.conn, key) is None
    for table in modules.QUEUED:
        assert _there(world, table, gone[table]) is None, f"{table}'s row stayed"
    interaction = _there(world, "interactions", gone["interactions"])
    if forget:
        assert interaction is None
        assert _there(world, "people", person) is None, "known only from it, they went"
    else:
        assert _there(world, "people", person) is not None
        assert world.conn.execute("SELECT source_id FROM interactions WHERE rowid = ?",
                                  (gone["interactions"],)).fetchone()[0].startswith("detached:")
    assert world.conn.execute("SELECT source_id FROM obligations WHERE rowid = ?",
                              (gone["obligations"],)).fetchone()[0] is None
    for table in modules.KEPT.keys() - {"people"}:
        assert _there(world, table, gone[table]) is not None, f"{table}'s row went"
    for table, rowid in kept.items():
        assert _there(world, table, rowid) is not None, f"the other account's {table} went"
    assert credstore.names(credstore.prefix(held.key, key)) == []
    assert credstore.names(credstore.prefix(held.key, other)) != []
    assert not state.exists()
    assert health.read(world.conn, key) is None


# --- the routes ------------------------------------------------------------------

class _App:
    def __init__(self) -> None:
        self.app = Starlette(routes=account_routes.ROUTES)

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        async def go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1") as c:
                return await c.request(method, path, **kw)

        return asyncio.run(go())


def test_the_app_adds_tests_and_lists_an_account_and_no_value_of_a_field_comes_back(
        world, monkeypatch):
    monkeypatch.setattr(runtime, "conn", world.conn)
    app = _App()

    added = app.call("POST", "/control/accounts", json={
        "op": "add", "module": "mailish", "address": "maya@mailish.example",
        "fields": {"password": PASSWORD}}).json()
    listed = app.call("GET", "/control/modules")

    assert added["ok"] and added["test"]["state"] == health.WORKING
    assert PASSWORD not in listed.text
    [account] = listed.json()["accounts"]
    assert account["key"] == added["key"]
    assert account["health"] == "maya@mailish.example is working."
    picked = app.call("POST", "/control/accounts",
                      json={"op": "pick", "address": "maya@partner.example"}).json()
    assert picked == {"ok": True, "module": "mailish", "preset": "partner"}


def test_the_app_is_told_where_each_secret_is_revoked(world):
    """A manifest's revoke pages were read and never shown, so only Gmail's
    guide said where to take a leaked app password back (the step 11
    audit, E8). The module's page, and a preset's own."""
    [mailish] = [m for m in accounts.listing(world.conn, manifests=MANIFESTS)["modules"]
                 if m["id"] == "mailish"]
    assert mailish["setup"]["revoke"] == "https://mailish.example/security"
    assert [p["revoke"] for p in mailish["setup"]["presets"]] == [
        "https://partner.example/app-passwords"]


@pytest.mark.parametrize("state, kind, fix, act, asks", [
    (health.NEEDS_YOU, "sign_in", "", "sign_in", "sign in again"),
    (health.NEEDS_YOU, "password", "", "password", "enter its new password"),
    (health.NEEDS_YOU, "tools_changed", "", "test", "test it again"),
    (health.NEEDS_YOU, "unsupported", "https://console.example/api", "link", "open the page"),
    (health.NEEDS_YOU, "unsupported", "", "test", "check it again"),
    (health.CANT_REACH, "transient", "", "test", "keeps trying"),
    (health.CANT_WORK_HERE, "not_connected", "", "test", "can't work on this computer"),
    (health.OFF, None, "", "turn_on", "is off"),
    (health.WORKING, None, "", "", "is working"),
])
def test_each_states_button_does_what_its_sentence_asks(state, kind, fix, act, asks):
    """The app shows the sentence with one button beside it. Decided apart,
    the button could offer a sign-in where the sentence asks for a password."""
    assert health.action(state, kind=kind, fix=fix) == act
    assert asks in health.sentence("Home", state, kind=kind, fix=fix)


async def test_a_new_password_replaces_the_old_and_nothing_else_is_written_through_it(world):
    """Health's "enter its new password" is done in the app, without
    removing the account and losing its settings; a setting or a bad value
    is refused before anything is kept."""
    key = _add(world)
    target = credstore.target(modules.module(world.conn, "mailish").key, key, "password")

    for values, says in [({"server": "imap.mailish.example"}, "keeps no secret called server"),
                         ({"password": ""}, "App password is needed"),
                         ({"password": 1729}, "is text"), ({}, "say what to change")]:
        with pytest.raises(modules.AccountError, match=says):
            accounts.renew(world.conn, key, values)
    assert credstore.read(target) == PASSWORD

    accounts.edit(world.conn, key, values={"password": "new-password-2048"})

    assert credstore.read(target) == "new-password-2048"
    assert (await accounts.test(world.conn, key))["state"] == health.WORKING


def test_the_app_is_shown_each_accounts_one_action_and_the_cards_off_would_withdraw(
        world, monkeypatch):
    """Before off or remove the app says "2 waiting requests will be
    cancelled", so the count must be the cards that would be withdrawn."""
    monkeypatch.setattr(runtime, "conn", world.conn)
    app = _App()
    key, other = _add(world), _add(world, "rina@mailish.example")
    for status in ("pending", "pending", "approved"):
        world.conn.execute(
            "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal, account,"
            " status) VALUES ('send_draft', '{}', ?, 'p', 'r', 'v', ?, ?)",
            (f"{status}-{world.conn.total_changes}", key, status))
    world.conn.commit()
    health.record(world.conn, other, kind="password")

    listed = {a["key"]: a for a in app.call("GET", "/control/modules").json()["accounts"]}
    assert (listed[key]["action"], listed[key]["waiting"]) == ("test", 2), \
        "never tested, it is offered its test; only pending cards are withdrawn"
    assert (listed[other]["action"], listed[other]["waiting"]) == ("password", 0)

    app.call("POST", "/control/accounts", json={"op": "edit", "key": other, "enabled": False})
    renewed = app.call("POST", "/control/accounts", json={
        "op": "edit", "key": other, "fields": {"password": "new-password-2048"}})

    assert renewed.json() == {"ok": True}
    listed = {a["key"]: a for a in app.call("GET", "/control/modules").json()["accounts"]}
    assert listed[other]["action"] == "turn_on"


def test_the_app_is_told_which_account_holds_an_address_so_it_can_offer_to_move_it(
        world, monkeypatch):
    """Setup offers "Move the mail for this address here" rather than a
    dead end, which needs the class and the account that holds it."""
    monkeypatch.setattr(runtime, "conn", world.conn)
    app = _App()
    _add(world, wanted=["mail"])

    refused = app.call("POST", "/control/accounts", json={
        "op": "add", "module": "mailish", "address": "maya@mailish.example", "name": "Again",
        "classes": ["mail"], "fields": {"password": PASSWORD}})
    moved = app.call("POST", "/control/accounts", json={
        "op": "add", "module": "mailish", "address": "maya@mailish.example", "name": "Again",
        "classes": ["mail"], "fields": {"password": PASSWORD}, "take": True})

    assert refused.status_code == 400
    assert refused.json()["held"] == {"class": "mail", "address": "maya@mailish.example",
                                      "name": "maya@mailish.example"}
    assert moved.json()["ok"] is True


def test_the_app_is_told_why_an_account_was_refused(world, monkeypatch):
    monkeypatch.setattr(runtime, "conn", world.conn)
    app = _App()

    refused = app.call("POST", "/control/accounts", json={
        "op": "add", "module": "mailish", "address": "maya@mailish.example", "fields": {}})
    unknown = app.call("POST", "/control/accounts", json={"op": "share"})

    assert refused.status_code == 400 and "App password is needed" in refused.json()["error"]
    assert unknown.status_code == 400 and "share" in unknown.json()["error"]
