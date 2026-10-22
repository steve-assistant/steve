"""Core's MCP engine on its fake server (05-connectors.md, item 11).

An MCP server's tools are made for a model and can change under an account,
and nothing in MCP binds a tool to the item it was asked about. These hold
the engine to its profile, its pinned tool list, and a write path that
asks by card every time and checks the item before and after, against a
server that changes its tools, acts on another id than the one given, and
lets an event change between Steve's read and its write.
"""

from __future__ import annotations

import copy
import datetime as dt
import shutil
from pathlib import Path

import pytest

from agent import accounts, connectors
from core import db, health, modules, paths, writes
from core.engines import mcp
from steve_sdk import classes, manifest
from steve_sdk.classes import Event
from steve_sdk.errors import (
    AfterWrite,
    ConnectorError,
    NeedsCard,
    NotConnected,
    SignInNeeded,
    TargetChanged,
    Transient,
    Unsupported,
    WrongPassword,
)
from steve_sdk.testing import calendar, changed, mail
from tests.fakes import mcp as fake

MAYA = fake.MAYA
MONDAY = dt.datetime(2026, 11, 2, tzinfo=dt.UTC)


def _at(day: int, hour: int) -> dt.datetime:
    return MONDAY + dt.timedelta(days=day, hours=hour)


@pytest.fixture
def make_rig(tmp_path):
    made: list[fake.McpRig] = []

    def make(**server):
        rig = fake.rig(tmp_path / f"rig{len(made)}", **server)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.close()


def _swim(rig: fake.McpRig) -> str:
    [native] = rig.world.seed("calendar", [{"title": "Swim", "start": _at(3, 7),
                                            "end": _at(3, 8)}])
    return native


def _later(event: Event) -> Event:
    event.start, event.end = _at(3, 9), _at(3, 10)
    return event


# --- the contract, through the server --------------------------------------------

@pytest.mark.parametrize("check", mail.CHECKS, ids=lambda c: c.__name__)
def test_mail_through_an_mcp_server_keeps_the_contract(make_rig, check):
    rig = make_rig()
    check(mail.MailCase(rig.tested("mail"), rig.world, writes, MAYA))


@pytest.mark.parametrize("check", calendar.CHECKS, ids=lambda c: c.__name__)
def test_a_calendar_through_an_mcp_server_keeps_the_contract(make_rig, check):
    rig = make_rig()
    check(calendar.CalendarCase(rig.tested("calendar"), rig.world, writes, MAYA))


@pytest.mark.parametrize("check", mail.CHECKS, ids=lambda c: c.__name__)
def test_a_server_answering_in_event_streams_with_a_session_and_a_secret_keeps_it(
        make_rig, check):
    """The other shapes Streamable HTTP allows: a stream for each answer,
    a session id to repeat, and a bearer secret."""
    rig = make_rig(stream=True, session=True, secret="pass-phrase-1")
    check(mail.MailCase(rig.tested("mail", secret="pass-phrase-1"), rig.world, writes, MAYA))
    later = [r for r in rig.server.seen if b'"initialize"' not in r.content]
    assert later and all(r.headers.get("mcp-session-id") in rig.server.sessions for r in later)


# --- the plan's exit criteria --------------------------------------------------

def test_an_update_or_a_delete_needs_a_card_every_time(make_rig):
    """The server's tools take no change key, so no write can be made
    conditional on what Steve read: each asks the user first."""
    rig = make_rig()
    provider = rig.tested("calendar")
    native = _swim(rig)
    assert not provider.has(classes.CONDITIONAL)
    before = rig.world.snapshot("calendar")
    with pytest.raises(NeedsCard):
        writes.update(provider, native, _later)
    with pytest.raises(NeedsCard):
        writes.delete(provider, native)
    assert rig.world.snapshot("calendar") == before and rig.server.writes() == []
    card = provider.fetch(native).digest()
    writes.update(provider, native, _later, card=card)
    assert provider.fetch(native).start == _at(3, 9)


def test_an_event_changed_between_the_card_and_the_write_is_refused(make_rig):
    rig = make_rig()
    provider = rig.tested("calendar")
    native = _swim(rig)
    card = provider.fetch(native).digest()
    rig.world.mutate("calendar", native, title="Swim, moved on the phone")
    before = rig.world.snapshot("calendar")
    with pytest.raises(TargetChanged, match="since the card"):
        writes.update(provider, native, _later, card=card)
    with pytest.raises(TargetChanged, match="since the card"):
        writes.delete(provider, native, card=card)
    assert rig.world.snapshot("calendar") == before and rig.server.writes() == []


def test_an_event_changed_between_the_read_and_the_write_is_refused(make_rig):
    """A change the card does not show (its place) made just after the
    write path's read: the provider's own read before the write sees the
    change key move."""
    rig = make_rig()
    provider = rig.tested("calendar")
    native = _swim(rig)
    card = provider.fetch(native).digest()
    reads = []

    def between(tool, args):
        if tool == "get-calendar-event":
            reads.append(args)
            if len(reads) == 1:
                rig.world.mutate("calendar", native, location="Pool B")

    rig.server.after.append(between)
    with pytest.raises(TargetChanged, match="since it was read"):
        writes.update(provider, native, _later, card=card)
    assert rig.server.writes() == []
    assert provider.fetch(native).start == _at(3, 7)


def test_after_a_write_exactly_that_event_changed(make_rig):
    rig = make_rig()
    provider = rig.tested("calendar")
    one, two = rig.world.seed("calendar", [{"title": "Lunch", "start": _at(3, 12),
                                            "end": _at(3, 13)}] * 2)
    event = provider.fetch(one)
    before = rig.world.snapshot("calendar")
    writes.update(provider, one, _later, card=event.digest())
    assert changed(before, rig.world.snapshot("calendar")) == {event.ident}
    assert [name for name, _ in rig.server.calls if name == "update-calendar-event"] == \
        ["update-calendar-event"]
    sent = next(args for name, args in rig.server.calls if name == "update-calendar-event")
    assert sent["eventId"] == one and set(sent["body"]) == {"start", "end", "isAllDay"}, \
        "only what changed is written"


@pytest.mark.parametrize("server", ["answers 404", "answers 200 and changes nothing"])
def test_an_update_the_server_did_not_make_is_never_reported_as_made(make_rig, server):
    """The engine took a 404 for "no answer" and read the event back as it
    was, and the write path's check after was skipped, since the server's
    listing came in pages: the user was told the event moved. A 404 is
    now the event not found, so nothing written; a success that changed
    nothing is caught by the event read back, whatever its neighbours."""
    rig = make_rig()
    provider = rig.tested("calendar")
    one, _ = rig.world.seed("calendar", [{"title": "Lunch", "start": _at(3, 12),
                                          "end": _at(3, 13)}] * 2)
    event = provider.fetch(one)
    if server == "answers 404":
        rig.server.failing["update-calendar-event"] = 404
        refused = TargetChanged
    else:
        kept = copy.deepcopy(rig.world.events[one])

        def ignored(tool, args):
            if tool == "update-calendar-event":
                rig.world.events[one] = copy.deepcopy(kept)

        rig.server.after.append(ignored)
        refused = AfterWrite

    with pytest.raises(refused):
        writes.update(provider, one, _later, card=event.digest())
    assert provider.fetch(one).start == event.start


def test_a_server_that_changes_another_event_than_the_one_named_is_caught(make_rig):
    rig = make_rig()
    provider = rig.tested("calendar")
    one, two = rig.world.seed("calendar", [{"title": "Lunch", "start": _at(3, 12),
                                            "end": _at(3, 13)}] * 2)
    rig.server.misdirect(one, two)
    # After the write, so said as written, and never asked again. The event
    # named is read back first, and has not moved.
    with pytest.raises(AfterWrite, match="does not read back as written"):
        writes.update(provider, one, _later, card=provider.fetch(one).digest())
    with pytest.raises(AfterWrite, match="still there"):
        writes.delete(provider, one, card=provider.fetch(one).digest())


def test_a_server_that_answers_for_another_item_than_the_one_asked_is_caught(make_rig):
    rig = make_rig()
    provider = rig.tested("calendar")
    one, two = rig.world.seed("calendar", [{"title": "Lunch", "start": _at(3, 12),
                                            "end": _at(3, 13)}] * 2)
    rig.server.misdirect(one, two, reads=True)
    with pytest.raises(TargetChanged, match="the server answered"):
        writes.update(provider, one, _later, card="any")
    assert rig.server.writes() == []


def test_a_server_that_sends_or_throws_away_another_draft_is_caught(make_rig):
    rig = make_rig()
    provider = rig.tested("mail")
    from steve_sdk.classes import Draft

    one, two = (provider.make_draft(Draft(to="ada@else.example", subject="Saturday",
                                          body="See you at ten.")) for _ in range(2))
    rig.server.misdirect(one.native_id, two.native_id)
    with pytest.raises(AfterWrite):
        writes.delete(provider, one.native_id, card=one.digest())
    with pytest.raises(AfterWrite, match="another may have gone"):
        writes.send_draft(provider, one.native_id, card=one.digest())


def test_a_changed_tool_list_stops_the_account_until_it_is_tested_again(make_rig):
    rig = make_rig()
    rig.tested("calendar")
    _swim(rig)
    rig.server.change_tools()
    rig.server.calls.clear()
    for klass, read in (("calendar", lambda p: p.events(_at(0, 0), _at(7, 0), limit=10)),
                        ("mail", lambda p: p.search("", limit=5))):
        with pytest.raises(mcp.ToolsChanged) as stopped:
            read(rig.provider(klass))
        assert rig.server.calls == [], "no tool is called once the list changed"
    said = connectors.say(stopped.value, "Outlook")
    assert said.startswith("Outlook needs you: test it again, in Steve's app")
    assert health.state_of(stopped.value.kind) == health.NEEDS_YOU
    # Testing it again in the app is the user's yes to the new list.
    rig.tested("calendar")
    assert [e.title for e in rig.provider("calendar").events(_at(0, 0), _at(7, 0),
                                                              limit=10)] == ["Swim"]


def test_an_account_never_tested_calls_no_tool(make_rig):
    rig = make_rig()
    with pytest.raises(mcp.ToolsChanged, match="not been checked"):
        rig.provider("mail").search("", limit=5)
    assert rig.server.calls == []


# --- profiles ---------------------------------------------------------------------

def test_a_server_that_matches_no_profile_is_refused_and_nothing_is_pinned(make_rig):
    rig = make_rig()
    rig.server.info["name"] = "SomeOtherMailServer"
    with pytest.raises(ConnectorError, match="matches no profile"):
        rig.provider("mail").test()
    rig.server.info["name"] = "Microsoft365MCP"
    view = next(t for t in rig.server.tools if t["name"] == "get-calendar-view")
    del view["inputSchema"]["properties"]["top"]
    with pytest.raises(ConnectorError, match="takes no top"):
        rig.provider("calendar").test()
    assert not (rig.state / "acct0003" / mcp.PIN_FILE).exists()


def test_a_method_a_profile_lacks_is_a_capability_the_account_lacks(make_rig, tmp_path):
    home = tmp_path / "outlook_mcp"
    shutil.copytree(paths.SOURCE / "modules" / "outlook_mcp", home)
    profile = home / "profiles" / "softeria.toml"
    text = profile.read_text(encoding="utf-8")
    for method in ("calendar.methods.update", "calendar.methods.delete", "mail.methods.thread",
                   "mail.methods.send_draft"):
        start = text.index(f"[{method}]")
        end = text.find("\n\n", start)
        text = text[:start] + (text[end + 2:] if end != -1 else "")
    profile.write_text(text, encoding="utf-8", newline="\n")
    found = manifest.load(home / "manifest.toml")
    rig = make_rig()
    cal = rig.tested("calendar", manifest=found)
    post = rig.tested("mail", manifest=found)
    assert cal.capabilities == {classes.CREATE} and post.capabilities == frozenset()
    native = _swim(rig)
    with pytest.raises(Unsupported):
        writes.update(cal, native, _later, card=cal.fetch(native).digest())


def test_the_shipped_profile_fits_the_tools_its_server_lists():
    """What the fake serves is the fixture of the server's tools; the
    profile must take only arguments those tools have."""
    import json

    doc = json.loads(fake.TOOLS.read_text(encoding="utf-8"))
    module = modules.builtins()["outlook_mcp"]
    for klass in ("mail", "calendar"):
        found = mcp.profiles(module, klass)
        assert mcp.match(found, klass, doc["serverInfo"], doc["tools"]).server == \
            "Microsoft365MCP"


# --- the client -------------------------------------------------------------------

def test_the_secret_goes_to_the_one_server_as_a_bearer_and_a_wrong_one_is_said(make_rig):
    rig = make_rig(secret="pass-phrase-1")
    rig.tested("mail", secret="pass-phrase-1")
    assert {r.headers.get("authorization") for r in rig.server.seen} == {"Bearer pass-phrase-1"}
    assert {str(r.url) for r in rig.server.seen} == {f"http://{fake.HOST}:{fake.PORT}/mcp"}
    with pytest.raises(WrongPassword):
        rig.provider("mail", secret="another", key="acct0004").test()


def test_a_session_the_server_forgot_is_opened_again_once(make_rig):
    rig = make_rig(session=True)
    provider = rig.tested("mail")
    rig.world.seed("mail", [{"subject": "Plot", "date": dt.datetime.now(dt.UTC)}])
    rig.server.sessions.clear()
    assert [m.subject for m in provider.search("", limit=5)] == ["Plot"]


def test_an_address_without_its_port_is_said_and_nothing_is_sent(make_rig):
    rig = make_rig()
    with pytest.raises(NotConnected, match="port"):
        rig.provider("mail", server=fake.HOST).test()
    assert rig.server.seen == []


@pytest.mark.parametrize(("status", "error"), [(401, SignInNeeded), (503, Transient),
                                               (400, ConnectorError)])
def test_a_tool_that_fails_is_said_as_the_accounts_error(make_rig, status, error):
    rig = make_rig()
    provider = rig.tested("mail")
    rig.server.failing["list-mail-messages"] = status
    with pytest.raises(error):
        provider.search("plot", limit=5)


def test_a_message_gone_from_the_server_is_none(make_rig):
    rig = make_rig()
    provider = rig.tested("mail")
    [native] = rig.world.seed("mail", [{"subject": "Plot"}])
    rig.world.mutate("mail", native, gone=True)
    assert provider.fetch(native) is None


def test_an_html_body_is_read_as_text(make_rig):
    rig = make_rig()
    provider = rig.tested("mail")
    [native] = rig.world.seed("mail", [{"subject": "Plot", "body": "Water &amp; seeds",
                                        "html": True}])
    assert provider.fetch(native).body == "Water & seeds"


def test_a_search_word_cannot_close_the_query_or_start_a_clause():
    parsed = mcp.query_syntax.parse('plot" OR from:x subject:re: back\\slash')
    said = mcp._kql(parsed)
    assert '"' not in said and "\\" not in said
    assert said.count(":") == said.count("from:") + said.count("subject:")


# --- adding the account as the app does --------------------------------------------

@pytest.fixture
def store(monkeypatch, make_rig):
    rig = make_rig()
    monkeypatch.setattr(connectors, "NETWORK", rig.network)
    conn = db.open_store(paths.db_path())
    modules.register(conn)
    yield rig, conn
    conn.close()


@pytest.mark.parametrize("domain", ["outlook.com", "hotmail.com", "live.com", "msn.com"])
def test_a_personal_microsoft_address_picks_the_mcp_module(domain):
    assert accounts.pick(f"maya.lindqvist@{domain}") == {"module": "outlook_mcp",
                                                         "preset": None}


async def test_adding_the_account_tests_it_and_pins_its_servers_tools(store):
    rig, conn = store
    rig.world.seed("calendar", [{"title": "Swim", "start": dt.datetime.now(dt.UTC),
                                 "end": dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)}])
    key = accounts.add(conn, module_id="outlook_mcp", address=MAYA, preset=None,
                       values={"server": f"{fake.HOST}:{fake.PORT}"},
                       resolve=lambda host, port: [fake.HOST])
    tested = await accounts.test(conn, key)
    assert tested["ok"], tested
    assert all("matched to Softeria's ms-365-mcp-server" in said and "tools pinned" in said
               for said in tested["found"])
    assert (modules.account_state_dir("outlook_mcp", key) / mcp.PIN_FILE).exists()
    rig.server.change_tools()
    found = connectors.accounts(conn, "calendar")
    with pytest.raises(mcp.ToolsChanged):
        await connectors.call(found[0], lambda p: p.events(
            dt.datetime.now(dt.UTC), dt.datetime.now(dt.UTC) + dt.timedelta(days=1), limit=5))
    assert (await accounts.test(conn, key))["ok"], "testing it again pins the new list"


def test_the_fake_keeps_graphs_shapes_for_the_profiles_paths(make_rig):
    """A guard on the fake itself: a message read through it has what the
    profile's paths name, so a contract check cannot pass on a field the
    fake left empty."""
    rig = make_rig()
    provider = rig.tested("mail")
    [native] = rig.world.seed("mail", [{"subject": "Plot", "sender": "Ada <ada@else.example>",
                                        "to": MAYA, "message_id": "<plot@else.example>"}])
    got = provider.fetch(native)
    assert (got.ident, got.sender, got.to, got.condition) == (
        "<plot@else.example>", "ada@else.example", MAYA, rig.world.mail[native]["changeKey"])
    assert copy.deepcopy(got).thread == rig.world.mail[native]["conversationId"]


def test_profiles_are_read_from_the_modules_own_folder_only(tmp_path):
    home = tmp_path / "outlook_mcp"
    shutil.copytree(paths.SOURCE / "modules" / "outlook_mcp", home)
    text = (home / "manifest.toml").read_text(encoding="utf-8")
    (home / "manifest.toml").write_text(
        text.replace('profiles = ["profiles/softeria.toml"]\npath = "/mcp"\n\n[calendar]',
                     'profiles = ["../elsewhere.toml"]\npath = "/mcp"\n\n[calendar]'),
        encoding="utf-8", newline="\n")
    found = manifest.load(home / "manifest.toml")
    with pytest.raises(ConnectorError, match="module's own folder"):
        mcp.profiles(found, "mail")
    assert Path(found.path).parent == home
