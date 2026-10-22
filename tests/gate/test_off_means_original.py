"""An account or module switched off leaves Steve as if it had never been added.

Core keeps no module's state, but some core rows come from a module's items:
queued observations, cards, commitments, the people ingesting mail found
(05-connectors.md, "Off means original"). `core/modules.TAGGED` says how each
such table names its account, and `core/modules.live` skips the rows of an
account that is off. Here: that every table which could hold such rows is
listed, that each listed table's rows are skipped, and that a card waiting
on an account switched off is withdrawn and says so wherever it is named --
a yes given later would act on what was true before.

And the whole of it: with every module switched off again, what Steve is
given -- its prompt's sections, its tools, what it watches, what it runs,
what its judge is told and the cards it waits on -- is byte for byte what it
was before any was on. That holds for the built-ins' accounts, and for
modules from outside with something in every slot (06-modules.md, "Off
means original"): a tool, words for the prompt, a skill, an attention
source and its judge paragraph, a routine, a connector, and a server's
tools.

Which reads go through `live` is `test_tagged_reads.py`'s.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import difflib
import json
from types import SimpleNamespace

import pytest

from core import db, modules
from steve_sdk import ids
from tests.fakes import google as fake_google
from tests.fakes import outlook as fake_outlook

# OFF: a mailbox the user added, an account of `imap`, which is switched off
# below; ON: a Google account, added and signed in as the app does it.
OFF, ON = "5e1f0a2b3c4d", fake_google.KEY


def _add_off(conn) -> None:
    """The user's own mailbox, added as an account of `imap` under OFF's key."""
    from tests import persona

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(modules, "new_key", lambda: OFF)
        modules.add_account(conn, module_id="imap", name="Home", classes=["mail"],
                            addresses=[persona.OWN_DOMAIN_EMAIL], preset="other")


@pytest.fixture
def fresh(tmp_path):
    """A data folder with no account the user added."""
    c = db.open_store(tmp_path / "off.db")
    modules.register(c)
    yield c
    c.close()


@pytest.fixture
def conn(fresh):
    _add_off(fresh)
    fake_google.signed_in(fresh)
    return fresh


def _tables(conn) -> dict[str, set[str]]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                        " AND name NOT LIKE 'sqlite_%'").fetchall()
    return {r[0]: {c[1] for c in conn.execute(f"PRAGMA table_info({r[0]})")} for r in rows}


def test_every_table_that_could_hold_an_accounts_rows_is_tagged_or_said_to_be_cores(conn):
    """A migration that adds a table with a `source` or an `*_id` column
    has to say which it is: rows an account's items made, skipped while it
    is off, or core's own. Left unsaid, the new table's rows would speak for
    an account the user turned off."""
    tables = _tables(conn)
    could = {name for name, columns in tables.items()
             if any(c == "source" or c.endswith("_id") for c in columns)}

    assert not sorted(could - set(modules.TAGGED) - set(modules.CORE)), \
        "add each to core/modules.TAGGED, with how its rows name their account, or to CORE"
    assert not sorted(set(modules.TAGGED) & set(modules.CORE))
    assert not sorted((set(modules.TAGGED) | set(modules.CORE)) - set(tables)), \
        "a table named in core/modules that the schema no longer has"


def _tag(key: str, native: str) -> str:
    return ids.make("mail", key, native)


def _person(conn, email: str) -> int:
    return conn.execute("INSERT INTO people (email) VALUES (?)", (email,)).lastrowid


# One row of each tagged table, naming the account `key` (None: no account,
# core's own); its rowid. A table added to TAGGED needs its row here.
ROWS = {
    "observations": lambda conn, key, n: conn.execute(
        "INSERT INTO observations (source, key, title) VALUES ('mail:x', ?, 't')",
        (f"thread:{_tag(key, n)}" if key else f"reminder:{n}",)).lastrowid,
    "sync_cursors": lambda conn, key, n: conn.execute(
        "INSERT INTO sync_cursors (source, cursor) VALUES (?, 'c')",
        (f"ingest:{_tag(key, n)}" if key else f"briefing:{n}",)).lastrowid,
    "newsletters_said": lambda conn, key, n: conn.execute(
        "INSERT INTO newsletters_said (key, briefing) VALUES (?, 'b')",
        (f"letter:{_tag(key, n)}" if key else f"news:{n}",)).lastrowid,
    "actions": lambda conn, key, n: conn.execute(
        "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal, account)"
        " VALUES ('send_draft', '{}', ?, 'p', 'r', 'v', ?)", (n, key)).lastrowid,
    "interactions": lambda conn, key, n: conn.execute(
        "INSERT INTO interactions (person_id, kind, direction, source_id, occurred_at)"
        " VALUES (?, 'email', 'sent', ?, '2026-10-01')",
        (_person(conn, f"{n}@northwind.example"), _tag(key, n) if key else f"fact:{n}"),
    ).lastrowid,
    "obligations": lambda conn, key, n: conn.execute(
        "INSERT INTO obligations (kind, text, source, source_id)"
        " VALUES ('commitment', 't', 'sent_mail', ?)",
        (_tag(key, n) if key else None,)).lastrowid,
    "handles": lambda conn, key, n: conn.execute(
        "INSERT INTO handles (handle, full) VALUES (?, ?)",
        (f"h{n}", _tag(key, n) if key else f"https://example.org/{n}")).lastrowid,
    "mailbox": lambda conn, key, n: conn.execute(
        "INSERT INTO mailbox (message_id, kind) VALUES (?, 'instruction')",
        (_tag(key, n) if key else n,)).lastrowid,
}


def _people_rows(conn) -> dict[str, int]:
    """A person is from an account through their interactions."""
    made = {}
    for name, keys in {"only_off": [OFF], "off_and_on": [OFF, ON], "only_on": [ON],
                       "none": []}.items():
        person = _person(conn, f"{name}@northwind.example")
        for key in keys:
            conn.execute("INSERT INTO interactions (person_id, kind, direction, source_id,"
                         " occurred_at) VALUES (?, 'email', 'sent', ?, '2026-10-01')",
                         (person, _tag(key, f"{name}-{key}")))
        made[name] = person
    return made


@pytest.mark.parametrize("table", sorted(modules.TAGGED))
def test_a_tagged_tables_rows_are_skipped_while_their_account_is_off(conn, table):
    """Generated from `TAGGED`, one per table: a row of the account that is
    off is skipped, and one of a live account and one of no account kept."""
    modules.set_enabled(conn, "imap", False)
    if table == "people":
        made = _people_rows(conn)
        kept = {made["off_and_on"], made["only_on"], made["none"]}
    else:
        assert table in ROWS, f"give {table} a row in ROWS"
        rows = {name: ROWS[table](conn, key, name)
                for name, key in (("off", OFF), ("on", ON), ("none", None))}
        kept = {rows["on"], rows["none"]}

    found = {r[0] for r in conn.execute(
        f"SELECT rowid FROM {table} WHERE {modules.live(conn, table)}")}

    assert found == kept
    modules.set_enabled(conn, "imap", True)
    assert len({r[0] for r in conn.execute(
        f"SELECT rowid FROM {table} WHERE {modules.live(conn, table)}")}) == len(kept) + 1, \
        "kept, and back when the account is"


def test_a_built_in_switched_off_takes_its_accounts_out(conn):
    from agent import connectors

    assert ON in modules.live_keys(conn)
    modules.set_enabled(conn, "google_cloud", False)

    assert ON not in modules.live_keys(conn)
    assert ON not in {a.key for a in connectors.accounts(conn, "mail")}
    assert OFF in modules.live_keys(conn)


# --------------------------------------------------------------------------
# Withdrawn cards
# --------------------------------------------------------------------------


def _card(conn, key: str, token: str) -> int:
    return conn.execute(
        "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal, account,"
        " token, status, asked_at) VALUES ('send_draft', ?, ?, ?, 'r', 'v', ?, ?, 'pending',"
        " datetime('now'))",
        (json.dumps({"draft_id": _tag(key, token)}), token, f"Send the draft to Jane ({token})",
         key, token)).lastrowid


def _withdraw(conn):
    off = _card(conn, OFF, "MAPLE")
    on = _card(conn, ON, "CEDAR")
    modules.set_enabled(conn, "imap", False)
    return off, on


def test_switching_an_account_off_withdraws_its_waiting_cards_and_says_why(conn):
    from agent import approvals

    off, on = _withdraw(conn)

    assert approvals.get(conn, off).status == "withdrawn"
    assert approvals.get(conn, off).withdrawn_reason == modules.WITHDRAWN_OFF
    assert [a.id for a in approvals.pending(conn)] == [on]


def test_a_late_yes_to_a_withdrawn_card_is_told_why_and_nothing_is_done(conn):
    """Not "it may have expired": the user turned the account off, and is
    told that is why."""
    from agent import approvals

    _withdraw(conn)

    said = approvals.parse_reply("yes maple", approvals.pending(conn),
                                 withdrawn=approvals.withdrawn(conn))

    assert said is not None and said.action_id is None and said.approved is None
    assert modules.WITHDRAWN_OFF in said.reply and "Nothing was done" in said.reply


def test_the_monitor_refuses_a_yes_to_a_withdrawn_card(conn):
    from agent import approvals
    from core import monitor

    off, _ = _withdraw(conn)

    why = monitor.may_decide(conn, approvals.get(conn, off), approve=True, by="tray")

    assert why and modules.WITHDRAWN_OFF in why


def test_steves_next_turn_knows_the_card_will_not_happen_and_why(conn):
    from agent import closing

    _withdraw(conn)

    view = closing.open_now(conn)

    assert any("MAPLE" in line and modules.WITHDRAWN_OFF in line for line in view.closed)
    assert any("CEDAR" in line for line in view.open)
    assert not any("MAPLE" in line for line in view.open)


# --------------------------------------------------------------------------
# The surface
# --------------------------------------------------------------------------

# The first modules, whose accounts the user adds: Google, classic Outlook
# and `imap`.
BUILT_INS = sorted({"outlook_classic", "imap", "google_cloud"})


def _surface(conn) -> str:
    """What Steve is given in this data folder, as a conversation's agent is
    built (`loop.build_agent`) and a run starts: the house prompt with core's
    feature sections, the modules' words and the skills; the tools with
    their schemas, core's and those the modules and servers that are on
    bring; the attention sources, the judge's prompt with the modules'
    paragraphs, and the queue it reads; the cards waiting; the worker's
    duties, the modules' routines and the wake alarm. Every collection
    sorted. The profile and memory are left out: they change with use, and
    the user's history stays."""
    from agent import approvals, features, loop, modroutines
    from agent.attention import sources, store, triage
    from agent.models import Role
    from agent.turn import Deps
    from agent.worker import duties

    on = features.enabled(conn)
    agent = loop.build_agent(Role.CONVERSE, on=on, lead=True)
    ctx = SimpleNamespace(deps=Deps(conn=conn, session_id=1))
    tools = dict(agent._function_toolset.tools)
    # The modules' tools and the servers' are read as each run starts
    # (`modtools.register`, `mcptools.register`), so they are asked here
    # as a run asks them.
    for dynamic in agent._dynamic_toolsets:
        tools.update(asyncio.run(dynamic.toolset_func(ctx)).tools)
    watched = sources.watching(conn)
    watching = sorted(s.name for s in watched)
    return json.dumps({
        "prompt": [loop.system_prompt(on), loop._modules_line(ctx), loop._skills_line(ctx)],
        "tools": {name: {"schema": tool.function_schema.json_schema,
                         "description": tool.description}
                  for name, tool in tools.items()},
        "sources": watching,
        "duties": [f"{d.__module__}.{d.__name__}" for d in duties.all_duties()],
        "routines": [[r.id, r.title, r.instruction, r.schedule.to_json()]
                     for r in modroutines.listed(conn)],
        "wake": modroutines.next_run(conn),
        "judge": triage.judge_prompt(set(watching), [s.judge for s in watched if s.judge]),
        "queue": sorted(f"{o.source}: {o.title}" for o in store.untriaged(conn, 1000)),
        "cards": sorted(a.preview for a in approvals.pending(conn, 1000)),
    }, sort_keys=True, indent=1, default=str)


def _accounts_on_fakes(conn, monkeypatch) -> dict:
    """The user's accounts, each class on a fake of its own, signed in:
    Outlook on this computer, a Google account and a mailbox, added. The
    registry still decides which are live (`connectors.accounts`), so a
    module switched off takes its fakes out as it would the real ones."""
    from agent import connectors
    from steve_sdk.classes import Event, FileItem, Message, Task
    from tests import persona
    from tests.gate import mailfake, slotfake

    soon = dt.datetime.now().astimezone() + dt.timedelta(hours=3)
    when = int((dt.datetime.now() - dt.timedelta(hours=1)).timestamp() * 1000)

    def mail(address: str, native: str) -> mailfake.Mailbox:
        return mailfake.Mailbox(address, Message(
            native, sender="Dana Ortiz <dana@example.com>", to=address, when=when,
            subject="Lunch on Friday?", body="Are you free for lunch on Friday?", unread=True))

    fakes = {
        ("mail", fake_google.KEY): mail(persona.GMAIL, "g1"),
        ("mail", fake_outlook.KEY): mail(persona.WORK_EMAIL, "o1"),
        ("mail", OFF): mail(persona.OWN_DOMAIN_EMAIL, "d1"),
        ("calendar", fake_google.KEY): slotfake.Calendar(Event(
            "e1", title="Design review", start=soon, end=soon + dt.timedelta(hours=1))),
        ("calendar", fake_outlook.KEY): slotfake.Calendar(),
        ("files", fake_google.KEY): slotfake.Drive(FileItem(
            "f1", name="Q3 plan", owners=("eli@contoso.example",), modified="t1")),
        ("tasks", fake_google.KEY): slotfake.TaskBoard(Task("k1", title="Renew the permit")),
    }
    monkeypatch.setitem(connectors.ENGINES, "imap", lambda ctx, spec: fakes[("mail", OFF)])
    # A built-in's code, by its provider's class: here, each class's fake.
    code = {"GmailMail": "mail", "GoogleCalendar": "calendar", "GoogleDrive": "files",
            "GoogleTasks": "tasks", "OutlookMail": "mail", "OutlookCalendar": "calendar"}
    monkeypatch.setitem(connectors.ENGINES, "code", lambda ctx, spec: fakes[
        (code[spec.settings["provider"]], ctx.account.key)])
    _add_off(conn)
    fake_google.signed_in(conn, address=persona.GMAIL)
    fake_outlook.add(conn)
    return fakes


def _use(conn) -> dict:
    """One tick of each source, one call of each class's tool, and one card:
    the rows a module's items leave in core."""
    from pydantic_ai import Agent

    from agent.attention import sources, store
    from agent.toolset import register_tools
    from agent.turn import Deps

    ticked = {s.name: len(store.record_new(conn, sources.collect(conn, s)))
              for s in sources.watching(conn)}
    agent = Agent("test", deps_type=Deps)
    register_tools(agent, lead=True)
    tools = agent._function_toolset.tools
    ctx = SimpleNamespace(deps=Deps(conn=conn, session_id=1))
    called = {name: asyncio.run(tools[name].function(ctx, **args)) for name, args in (
        ("search_email", {"query": "lunch"}), ("calendar_agenda", {"days": 2}),
        ("search_drive", {"query": "plan"}), ("list_tasks", {}))}
    _card(conn, ON, "MAPLE")
    return {"ticked": ticked, "called": called}


def test_with_every_module_off_again_steve_is_given_what_it_was_before_any_was_on(
        fresh, monkeypatch):
    """The promise of a module: switched off, it leaves nothing behind that
    reaches a model or a loop -- no section, tool or source of its accounts,
    and no row of theirs read as live. Taken in a fresh data folder, with no
    account and every module off -- the stock product -- and again after the
    user added accounts, used them and switched every module off, the
    surface must be the same, byte for byte. Taken both times with the
    accounts there, a part that ignored the switch would be the same both
    times and pass."""
    from agent import connectors

    conn = fresh
    for module in BUILT_INS:
        modules.set_enabled(conn, module, False)
    before = _surface(conn)

    for module in BUILT_INS:
        modules.set_enabled(conn, module, True)
    _accounts_on_fakes(conn, monkeypatch)
    assert _surface(conn) != before, "with its modules on, Steve has their accounts"
    used = _use(conn)
    for module in BUILT_INS:
        modules.set_enabled(conn, module, False)

    # It was used: each account's mail was seen, and each class's tool
    # found what its fake holds (by id: a gate test's screen is the real one).
    keys = (ON, fake_outlook.KEY, OFF)
    assert [used["ticked"].get(f"mail:{key}") for key in keys] == [1] * 3
    said = {name: json.dumps(out, default=str) for name, out in used["called"].items()}
    assert sorted(used["called"]["search_email"]["found_in"].values()) == [1, 1, 1], \
        said["search_email"]
    assert ids.make("calendar", ON, "e1") in said["calendar_agenda"], said["calendar_agenda"]
    assert ids.make("files", ON, "f1") in said["search_drive"], said["search_drive"]
    assert "Renew the permit" in said["list_tasks"], said["list_tasks"]
    assert connectors.accounts(conn, "mail") == []
    assert _surface(conn) == before


# --------------------------------------------------------------------------
# Modules from outside, in every slot
# --------------------------------------------------------------------------

# A module Steve built, with something in each slot a module's code fills: a
# tool, words for the instructions, a skill, an attention source with a
# paragraph for the judge, and a routine.
HARBOUR = """manifest_version = 1
sdk = 1
id = "harbour"
version = "1"
label = "Harbour"
for = "The harbour's tides and notices"
code = "harbour.py"
hosts = ["tides.harbour.example"]
prompt = "Harbour tide times are given in local time."

[[tools]]
name = "tides"
class = "read"
description = "Today's tides at the harbour"
params = []

[[skills]]
title = "Plan a sail"
when = "a sail is asked about"
steps = ["Read the tides", "Say the window, in local time"]

[attention]
looks_at = "the harbour's notices"
judge = "A notice that closes the harbour today is now; one about fees is fyi."
every_min = 30

[[routines]]
title = "Morning tides"
instruction = "Say today's high tides."
at = "07:30"
"""
HARBOUR_CODE = ("def tides(ctx):\n    return {}\n\n\n"
                "def collect(ctx):\n    return []\n")
HARBOUR_TEST = "def test_nothing():\n    assert True\n"

# A connector from outside: a mail module on the IMAP engine, text alone.
HARBOUR_MAIL = """manifest_version = 1
sdk = 1
id = "harbour_mail"
version = "1"
label = "Harbour Mail"
for = "Mail at Harbour, by password"
classes = ["mail"]
domains = ["harbour.example"]

[mail]
engine = "imap"
imap = "imap.mail.example:993"
smtp = "smtp.mail.example:465"

[setup]
steps = ["address", "fields", "test"]
fields = [{ name = "password", type = "secret", label = "Password" }]
"""

# A server the user added by address, on the fake MCP server.
SERVER_SECRET = "s3cret-token-for-notes"
SERVER_URL = "https://mcp.notes.example/mcp"
SERVER_READ = "list-mail-messages"
# A Saturday morning, pinned, before the routine's time.
SAT_7 = dt.datetime(2026, 10, 3, 7, 0)


class Admission:
    """The screen, the reviewer and the trial at admission: they pass. What
    they judge is admission's tests'; here it is what is left behind."""

    def screener(self, conn, artifact):
        from core import screen

        return screen.Verdict(verdict="as_is")

    async def reviewer(self, source, module_id, said):
        from core import audit

        return audit.Verdict(True, "it does what it says", model="fake")

    def tester(self, module_id, folder):
        from core.sandbox import testrun

        return testrun.Ran(True, "1 passed")


async def _passed(screen, conn, source, source_id, origin, text):
    return screen.Shown(text, "text")


def _write(folder, manifest: str, files: dict[str, str] | None = None):
    for name, text in {"manifest.toml": manifest, **(files or {})}.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8", newline="\n")
    return folder


def _from_outside(conn, folder, *, origin: str) -> str:
    """A module admitted from outside, approved and switched on, as the app
    does it; its key."""
    from agent import modadmit
    from core import lifecycle

    fakes = Admission()
    admitted = asyncio.run(modadmit.admit(conn, folder, origin=origin,
                                          screener=fakes.screener, reviewer=fakes.reviewer,
                                          tester=fakes.tester))
    module_id = lifecycle.approve(conn, admitted, given="")
    modules.set_enabled(conn, module_id, True)
    return next(m.key for m in modules.modules(conn) if m.id == module_id)


def _every_slot(conn, tmp_path, monkeypatch) -> dict:
    """The three modules from outside, installed and on: Harbour, Harbour
    Mail with the user's account at it, and a server. The sandbox and the
    server are fakes; the registry, core's rows and the toolsets are real."""
    import functools

    from agent import accounts, connectors, mcptools, modadmit, screening
    from core import screen
    from core.sandbox import run
    from steve_sdk.classes import Message
    from tests import persona
    from tests.fakes.mcp import GraphWorld, McpNetwork, McpServer
    from tests.gate import mailfake

    answers = {"tool": {"high": "10:42", "low": "16:55"},
               "collect": [{"key": "n1", "title": "The harbour closes at noon"}]}
    monkeypatch.setattr(run, "call", lambda conn, manifest, method, params, **kw:
                        answers[method])
    # The screen asks a model, which a gate test may not reach; what it
    # withholds is its own tests'. Here every item passes as it came.
    monkeypatch.setattr(screening, "shown_here", lambda conn, source, source_id, origin,
                        text: screen.Shown(text, "text"))
    monkeypatch.setattr(screening, "shown", functools.partial(_passed, screen))
    address = f"{persona.FIRST.lower()}@harbour.example"
    when = int((dt.datetime.now() - dt.timedelta(hours=1)).timestamp() * 1000)
    mailbox = mailfake.Mailbox(address, Message(
        "h1", sender="Dana Ortiz <dana@example.com>", to=address, when=when,
        subject="Slip fees for November", body="The new fees are attached.", unread=True))
    monkeypatch.setitem(connectors.ENGINES, "imap", lambda ctx, spec: mailbox)
    fakes = Admission()
    monkeypatch.setattr(modadmit, "admit", functools.partial(
        modadmit.admit, screener=fakes.screener))
    monkeypatch.setattr(mcptools, "NETWORK", McpNetwork(McpServer(
        GraphWorld(), secret=SERVER_SECRET, host="mcp.notes.example", port=443)))

    keys = {"harbour": _from_outside(conn, _write(tmp_path / "harbour", HARBOUR, {
        "harbour.py": HARBOUR_CODE, "tests/test_harbour.py": HARBOUR_TEST}), origin="built")}
    keys["harbour_mail"] = _from_outside(conn, _write(tmp_path / "harbour_mail", HARBOUR_MAIL),
                                         origin="imported")
    keys["account"] = modules.add_account(conn, module_id="harbour_mail", name="Harbour",
                                          classes=["mail"], addresses=[address])
    admitted = asyncio.run(mcptools.add(conn, name="notes", url=SERVER_URL,
                                        secret=SERVER_SECRET, label="Notes"))
    server = accounts.approve_server(conn, admitted, secrets={"token": SERVER_SECRET},
                                     free=[SERVER_READ])
    modules.set_enabled(conn, server, True)
    keys["server"] = server
    return keys


def _use_every_slot(conn) -> dict:
    """Each slot used once: the source ticked and the mailbox's, Harbour's
    tool and the server's called as a run calls them, the routine looked
    at, and a card raised by Harbour as `modtools` raises one. What each
    left is what switching off must hide."""
    from agent import approvals, mcptools, modroutines, modtools
    from agent.attention import sources, store
    from agent.turn import Deps
    from core import consent

    ticked = {s.name: len(store.record_new(conn, sources.collect(conn, s)))
              for s in sources.watching(conn)}
    ctx = SimpleNamespace(deps=Deps(conn=conn, session_id=1))
    offer = next(o for o in modtools.offers(conn) if o.module == "harbour")
    tides = asyncio.run(modtools.use(ctx, offer, {}))
    read = next(o for o in mcptools.offers(conn) if o.tool_name == SERVER_READ)
    server = asyncio.run(mcptools.use(ctx, read, {}))
    modroutines.due(conn, SAT_7)
    args = {"module": "harbour", "module_key": offer.key, "version": offer.version,
            "action": "book_slip", "args": {"day": "Saturday"}}
    _, card = approvals.queue(conn, tool=modtools.PERFORM, args=args,
                              preview=modtools.perform_card(offer, "book_slip", args["args"]),
                              consent=consent.classify(modtools.PERFORM, args))
    return {"ticked": ticked, "tides": tides, "server": server, "server_tool": read.name,
            "card": card}


def test_with_a_module_from_outside_in_every_slot_used_and_switched_off_steve_is_as_before(
        fresh, tmp_path, monkeypatch):
    """A module Steve built or the user imported fills a slot of core's, and
    switched off it must leave nothing that reaches a model or a loop: no
    tool, words, skill, source, judge paragraph, routine, account, server
    tool, queued item or waiting card. Each mark is first shown on the
    surface while the modules are on -- a surface that could not see a slot
    would pass for it -- and then gone, with the surface byte for byte what
    it was before any was installed."""
    from agent import approvals
    from core.sandbox import probe

    conn = fresh
    # A computer the sandbox runs on, before and after: what that offers
    # (`build_module`) is the computer's, not a module's.
    monkeypatch.setattr(probe, "verdict", lambda: SimpleNamespace(ok=True))
    before = _surface(conn)
    keys = _every_slot(conn, tmp_path, monkeypatch)

    used = _use_every_slot(conn)
    on = _surface(conn)
    marks = {
        "tool": "mod__harbour__tides",
        "words": "Harbour tide times are given in local time.",
        "skill": "Plan a sail",
        "source": "module:harbour",
        "judge": "closes the harbour today is now",
        "routine": "Morning tides",
        "account": f"mail:{keys['account']}",
        "queued": "The harbour closes at noon",
        "card": "asks to book slip",
        "server tool": used["server_tool"],
    }
    assert {slot: mark in before for slot, mark in marks.items()} == dict.fromkeys(marks, False)
    assert {slot: mark in on for slot, mark in marks.items()} == dict.fromkeys(marks, True)
    assert used["ticked"]["module:harbour"] == 1
    assert used["ticked"][f"mail:{keys['account']}"] == 1
    assert used["tides"] == {"ok": True, "result": {"high": "10:42", "low": "16:55"}}
    assert used["server"].get("ok") is not False, used["server"]
    assert used["card"] is not None and used["card"].status == "pending"

    for module in ("harbour", "harbour_mail", keys["server"]):
        modules.set_enabled(conn, module, False)

    assert approvals.get(conn, used["card"].id).status == "withdrawn"
    after = _surface(conn)
    assert after == before, "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(), "before", "after", lineterm="", n=1))
