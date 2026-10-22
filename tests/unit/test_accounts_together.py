"""Accounts of every kind, called together, each failing on its own.

Before step 5 Steve had one mailbox of each kind, and a search asked them in
turn: one server that hung held up the answer, and one that failed was
silently missing from it. Now a class is every account the user added, of
any module, asked at once, each within its own time limit. These hold that
on the fakes, through the modules themselves rather than stand-ins: four
mail modules answering one search, one taken down and one that blocks named
while the rest answer, and two accounts of one module working side by side
without reaching into each other.
"""

from __future__ import annotations

import ssl
import threading

import pytest

from agent import accounts, connectors, mailsearch
from agent.toolset import mail as mail_tools
from core import db, modctx, modules, writes
from core.engines import imap
from steve_sdk import ids
from steve_sdk.classes import Draft
from tests.fakes import mailrig
from tests.fakes import mcp as fake_mcp
from tests.fakes.imap import STANDARD
from tests.unit import golden_world as golden

WORDS = "harvest festival"


@pytest.fixture
def four(tmp_path, monkeypatch):
    """The golden world's Google, classic Outlook and own-domain mailbox,
    and an Outlook.com account through its MCP server: four mail modules,
    each holding one message about the festival."""
    world = golden.World()
    golden.wire(monkeypatch, world)
    server = fake_mcp.rig(tmp_path)
    network = world.mail.network
    feeds = network.transport_for
    monkeypatch.setattr(network, "transport_for", lambda host: (
        server.network.transport_for(host) if host == fake_mcp.HOST else feeds(host)))
    monkeypatch.setattr(network, "resolve", lambda host, port: (
        [fake_mcp.HOST] if host == fake_mcp.HOST else [mailrig.PUBLIC_IP]))

    world.gmail.put(sender="Iris Novak <iris@fabrikam.example>", to=golden.ME,
                    subject="Harvest festival rota", body="Your slot is noon.",
                    when=golden.at(hours=-2), labels={"INBOX"})
    world.outlook.put(golden.Outlook.INBOX, golden.OutlookMail(
        world.outlook, subject="Harvest festival tickets", sender=("Bo", "bo@else.example"),
        body="Two for Saturday.", when=golden.at(hours=-3)))
    world.mail.world.seed("mail", [{"sender": "Ada <ada@else.example>",
                                    "subject": "Harvest festival stalls",
                                    "date": golden.at(hours=-4)}])
    server.world.seed("mail", [{"sender": "Cy <cy@else.example>",
                                "subject": "Harvest festival parking",
                                "date": golden.at(hours=-5)}])

    conn = db.open_store()
    key = accounts.add(conn, module_id="outlook_mcp", address=fake_mcp.MAYA, preset=None,
                       values={"server": f"{fake_mcp.HOST}:{fake_mcp.PORT}"},
                       resolve=lambda host, port: [fake_mcp.HOST])
    yield conn, world, server, key
    conn.close()
    server.close()


async def _search(conn) -> dict:
    return await mailsearch.search(WORDS, 20, connectors.named(conn, "mail", "all"),
                                   mail_tools.shown)


async def test_one_search_over_all_returns_each_mail_modules_results(four):
    conn, world, server, key = four
    assert (await accounts.test(conn, key))["ok"]

    said = await _search(conn)

    by_module = {c.name: c.module for c in connectors.accounts(conn, "mail")}
    assert sorted(by_module.values()) == ["google_cloud", "imap", "outlook_classic",
                                          "outlook_mcp"]
    found = {m["account"]: m["subject"] for m in said["messages"]}
    assert sorted(found.values()) == ["Harvest festival parking", "Harvest festival rota",
                                      "Harvest festival stalls", "Harvest festival tickets"]
    assert set(found) == set(by_module) and said["problems"] == []


async def test_an_account_taken_down_and_one_that_blocks_are_named_while_the_rest_answer(
        four, monkeypatch):
    """The own-domain server is gone and the Outlook.com server hangs: the
    answer names both, and still holds Google's and Outlook's messages."""
    conn, world, server, key = four
    assert (await accounts.test(conn, key))["ok"]
    world.mail.close()
    stuck = threading.Event()
    answer = server.server.handle

    def hangs(request):
        stuck.wait(10)
        return answer(request)

    monkeypatch.setattr(server.server, "handle", hangs)
    ask_all = connectors.ask_all

    async def within_a_second(found, fn, *, limit_s=1.0):  # the default is a minute
        return await ask_all(found, fn, limit_s=limit_s)

    monkeypatch.setattr(connectors, "ask_all", within_a_second)
    try:
        said = await _search(conn)
    finally:
        stuck.set()

    assert sorted(m["subject"] for m in said["messages"]) == [
        "Harvest festival rota", "Harvest festival tickets"]
    names = {c.name: c.module for c in connectors.accounts(conn, "mail")}
    named = sorted(names[next(n for n in names if p.startswith(n + " "))]
                   for p in said["problems"])
    assert named == ["imap", "outlook_mcp"], said["problems"]


# --- two accounts of one module ---------------------------------------------------

HOME = "maya@home.example"
HOME_IP = "93.184.216.35"


class _TwoServers(modctx.Network):
    """Each account's servers on their own address: the work mailbox at
    imap.mail.example, the home one at mail.home.example."""

    def __init__(self, work: mailrig.Rig, home: mailrig.Rig) -> None:
        self.work, self.home = work, home

    def resolve(self, host: str, port: int) -> list[str]:
        return [HOME_IP] if host == "mail.home.example" else [mailrig.PUBLIC_IP]

    def own_ports(self) -> set[int]:
        return {54321}

    def open_tcp(self, address: str, port: int, timeout: float):
        rig = self.home if address == HOME_IP else self.work
        return rig.network.open_tcp(address, port, timeout)

    def tls(self) -> ssl.SSLContext:
        context = self.work.network.tls()
        context.load_verify_locations(cadata=self.home.network.ca.cert_pem.bytes().decode())
        return context


@pytest.fixture
def two(monkeypatch):
    work, home = mailrig.rig(STANDARD), mailrig.rig(STANDARD, user=HOME)
    monkeypatch.setattr(connectors, "NETWORK", _TwoServers(work, home))
    monkeypatch.setattr(imap, "SENT_WAIT_S", 0.2)
    conn = db.open_store()
    modules.register(conn)
    mailrig.add_account(conn, work, name="work")
    accounts.add(conn, module_id="imap", preset="other", address=HOME, name="home",
                 values={"password": home.world.password, "imap": "mail.home.example:993",
                         "smtp": "mail.home.example:465"},
                 resolve=lambda host, port: [HOME_IP])
    yield conn, work, home
    conn.close()
    work.close()
    home.close()


async def test_two_accounts_of_one_module_work_side_by_side(two):
    """Two mailboxes on core's IMAP engine: a search finds each one's mail
    under its own name, and a draft made, read and thrown away on one is
    never seen on the other."""
    conn, work, home = two
    work.world.seed("mail", [{"sender": "Ada <ada@else.example>", "subject": "Allotment rota"}])
    home.world.seed("mail", [{"sender": "Bo <bo@else.example>", "subject": "Allotment keys"}])
    chosen = {c.name: c for c in connectors.accounts(conn, "mail")}
    assert set(chosen) == {"work", "home"}
    assert chosen["work"].module == chosen["home"].module == "imap"

    said = await mailsearch.search("allotment", 10, list(chosen.values()), mail_tools.shown)
    assert {m["account"]: m["subject"] for m in said["messages"]} == {
        "work": "Allotment rota", "home": "Allotment keys"}

    work_before = work.world.snapshot("mail")
    made = await connectors.call(chosen["home"], lambda p: p.make_draft(
        Draft(to="bo@else.example", subject="Keys", body="Under the mat.")))
    slot_id = ids.make("mail", chosen["home"].key, made.native_id)
    found, native = connectors.by_id(conn, slot_id)
    assert found.key == chosen["home"].key
    target = writes.target(found.provider, native, expected=made.ident)
    await connectors.call(found, lambda p: writes.delete(
        p, native, expected=made.ident, card=target.digest()))

    assert work.world.snapshot("mail") == work_before
    assert found.provider.fetch(native) is None
