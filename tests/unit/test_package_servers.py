"""An MCP server the user adds as an npm package, which Steve runs.

Beside a server reached at its address (tests/unit/test_mcp_servers.py), the
MCP slot takes one Steve installs from npm and runs in a container of its
own (06-modules.md, section 7). A package can change under a name: a
version published yesterday may be a hijacked one, a dependency published
since may slip in under a range, and a new version or a new tree is other
code. So it is named at one exact version a week old, installed as the
registry stood then, its tree's hash shown on the card and pinned, and
started only from that tree; one that may reach any host needs the
passphrase; and declined, removed or switched off, nothing of it stays
running or on disk.
"""

from __future__ import annotations

import asyncio
import functools
import json

import pytest

from agent import accounts, mcptools, modadmit, modshare
from core import db, lifecycle, modules, passphrase, paths, screen
from core.engines import mcp
from core.sandbox import servers
from steve_sdk.errors import Transient
from steve_sdk.manifest import ManifestError, parse
from tests.fakes.mcp import MAYA, GraphWorld, McpServer

TREE = "ab" * 32
NOTES = "api.notes.example"
PUBLISHED = "2026-10-01T00:00:00.000Z"
PHRASE = "a long enough passphrase"


class _Screen:
    def __call__(self, conn, artifact):
        return screen.Verdict(verdict="as_is")


class _Direct(mcp._Rpc):
    """The fake server as the pool hands a started one."""

    def __init__(self, served: McpServer) -> None:
        self.served = served
        self.protocol = "2025-06-18"
        self.server = dict(served.info)

    def open(self) -> dict:
        return self.server

    def _exchange(self, method: str, params: dict) -> tuple[None, dict]:
        return None, self.served._dispatch(method, params)


class _Started:
    def __init__(self, events: list[str], key: str, client: _Direct) -> None:
        self.events = events
        self.key = key
        self.client = client
        self.log = servers.Log(str)

    def alive(self) -> bool:
        return True

    def stop(self) -> None:
        self.events.append(f"stop {self.key}")


class _World:
    """npm, the registry and the pool, as the add sees them, and what each
    was asked."""

    def __init__(self, monkeypatch, *, published: str | None = PUBLISHED,
                 starts: bool = True) -> None:
        self.events: list[str] = []
        self.installed: list[servers.Package] = []
        self.started: list[servers.Package] = []
        self.served = McpServer(GraphWorld(MAYA))
        self.published = published
        self.starts = starts
        self.npm_down = False
        monkeypatch.setattr(servers, "published", self._published)
        monkeypatch.setattr(servers, "install", self._install)
        monkeypatch.setattr(servers, "installed", self._installed)
        monkeypatch.setattr(servers, "POOL", servers.Pool(starter=self._start))
        monkeypatch.setattr(servers.profiles, "delete",
                            lambda name: self.events.append(f"drop {name}"))
        monkeypatch.setattr(modadmit, "admit", functools.partial(modadmit.admit,
                                                                 screener=_Screen()))

    def _published(self, name, version, *, network, now=None):
        self.events.append(f"published {name}@{version}")
        if self.published is None:
            raise servers.NotInstalled(f"{name}@{version} was published 2026-10-14: Steve "
                                       f"installs a version only once it is 7 days old")
        return self.published

    def _install(self, module_id, package, *, network, redact, timeout_s=0):
        if self.npm_down:
            raise servers.NotInstalled(f"npm did not install {package.name}@{package.version} "
                                       "(exit 1): the registry did not answer")
        self.installed.append(package)
        tree = servers.folder(module_id, package) / servers.TREE
        tree.mkdir(parents=True)
        (tree / "package-lock.json").write_text(json.dumps({"packages": {
            "": {}, "node_modules/notes-server": {}, "node_modules/notes-helper": {}}}),
            encoding="utf-8", newline="\n")
        servers.home(module_id, servers.MODULE_KEY).mkdir(parents=True, exist_ok=True)
        return servers.Installed(tree=TREE, packages=2, size=1)

    @staticmethod
    def _installed(module_id, package):
        return (servers.folder(module_id, package) / servers.TREE).is_dir()

    def _start(self, module_id, key, package, *, network, redact, watch=None):
        # As `servers.start`: only from the tree installed as approved.
        if not self._installed(module_id, package):
            raise servers.NotInstalled(f"{package.name}@{package.version} is not installed "
                                       "as approved")
        self.started.append(package)
        self.events.append(f"start {key}")
        if not self.starts:
            raise Transient("the MCP server has stopped")
        return _Started(self.events, key, _Direct(self.served))


@pytest.fixture
def conn():
    conn = db.open_store(paths.db_path())
    modules.register(conn)
    yield conn
    conn.close()


def _add(conn, **kw):
    asks = {"name": "notes", "package": "notes-server@1.0.1", "entry": "index.js",
            "hosts": [NOTES], "label": "Notes", **kw}
    return asyncio.run(mcptools.add_package(conn, **asks))


def test_a_package_the_user_names_is_installed_as_published_and_its_tree_is_on_the_card(
        conn, monkeypatch):
    """The card shows what was installed: the package and version, how many
    packages it brought, the tree's hash, its hosts, and that it holds its
    own sign-in; the manifest pins the hash, so it starts from no other."""
    world = _World(monkeypatch)

    _add(conn)

    [card] = lifecycle.waiting(conn)
    [installed] = world.installed
    assert installed.before == PUBLISHED and installed.lockfile is None
    assert servers.npm_argv(installed)[:3] == ["install", "--save-exact",
                                               f"--before={PUBLISHED}"]
    assert card["server"]["package"] == "notes-server@1.0.1"
    assert card["server"]["tree"] == TREE and card["server"]["packages"] == 2
    assert card["server"]["holds_its_sign_in"] is True
    assert any("notes-server@1.0.1" in line and "container of its own" in line
               for line in card["summary"])
    assert any(NOTES in line for line in card["summary"])
    assert card["passphrase"] is False
    [started] = world.started
    assert started.tree == TREE and started.hosts == {NOTES: (443,)}
    assert [t["name"] for t in card["server"]["tools"]] == [t["name"] for t in
                                                             world.served.tools]
    # Listed, then stopped: nothing of it runs until the yes.
    assert world.events[-1] == "stop module"
    assert modules.module(conn, "notes") is None


def test_a_version_younger_than_a_week_is_refused_and_nothing_of_it_is_kept(
        conn, monkeypatch):
    world = _World(monkeypatch, published=None)

    with pytest.raises(lifecycle.Refused, match="7 days"):
        _add(conn)

    assert world.installed == [] and lifecycle.waiting(conn) == []
    assert not (paths.module_dir("notes") / "server").exists()


def test_a_server_that_fails_once_installed_is_refused_and_nothing_of_it_is_kept(
        conn, monkeypatch):
    """Installed, it may still not start or list its tools; refused then,
    its files and its container are deleted, not left for no card."""
    world = _World(monkeypatch, starts=False)

    with pytest.raises(lifecycle.Refused, match="stopped"):
        _add(conn)

    assert world.installed and lifecycle.waiting(conn) == []
    assert not (paths.module_dir("notes") / "server").exists()
    assert f"drop {servers.container_name('notes', servers.MODULE_KEY)}" in world.events


def test_a_server_that_may_reach_any_host_needs_the_passphrase_for_every_version(
        conn, monkeypatch):
    """It could send what it is given anywhere; the card says so."""
    _World(monkeypatch)

    _add(conn, hosts=[], anywhere=True)

    [card] = lifecycle.waiting(conn)
    assert card["passphrase"] is True and card["server"]["anywhere"] is True
    assert any("any public host" in line for line in card["summary"])


def test_declining_the_card_deletes_the_installed_files_and_its_container(conn, monkeypatch):
    """It was installed, and started once, to show its tools on the card;
    declined, its files and its container are no one's."""
    world = _World(monkeypatch)
    admission = _add(conn)

    lifecycle.decline(conn, admission)

    assert not paths.module_dir("notes").exists()
    assert f"drop {servers.container_name('notes', servers.MODULE_KEY)}" in world.events


def test_once_approved_a_call_starts_the_server_from_the_tree_approved(conn, monkeypatch):
    """The pool starts it on the first call, from the package the manifest
    pins; off stops it, and removal deletes it whole."""
    world = _World(monkeypatch)
    module_id = accounts.approve_server(conn, _add(conn))
    modules.set_enabled(conn, module_id, True)
    world.events.clear()

    tools = mcptools.listed(modules.module(conn, module_id).manifest, lambda _name: None)
    modules.set_enabled(conn, module_id, False)
    off = list(world.events)
    started = world.started[-1]
    modules.remove_module(conn, module_id)

    assert tools and started.tree == TREE and started.before == PUBLISHED
    assert off == ["start module", "stop module"]
    assert f"drop {servers.container_name('notes', servers.MODULE_KEY)}" in world.events
    assert not paths.module_dir("notes").exists()


def _elsewhere(conn, world) -> str:
    """The notes server, exported as a zip and imported into a data folder
    that never installed it, up to its card: the admission id."""
    module_id = accounts.approve_server(conn, _add(conn), free=[])
    _, data = modshare.export_zip(conn, module_id)
    modules.remove_module(conn, module_id)
    assert not (paths.module_dir(module_id) / "server").exists()
    world.installed.clear()
    return asyncio.run(modshare.import_zip(conn, data))


def test_a_package_server_imported_from_a_zip_is_installed_when_its_card_is_approved(
        conn, monkeypatch):
    """A zip carries the manifest that pins the package and its tree, not
    the installed files, which stay in the data folder that installed them.
    Imported and approved, nothing installed it, and every call failed as
    "not installed as approved". Approval installs it, as the manifest pins
    it, and the call starts it."""
    world = _World(monkeypatch)
    module_id = accounts.approve_server(conn, _elsewhere(conn, world), free=[])

    [installed] = world.installed
    assert installed.tree == TREE and installed.before == PUBLISHED
    modules.set_enabled(conn, module_id, True)
    assert mcptools.listed(modules.module(conn, module_id).manifest, lambda _name: None)
    assert world.started[-1].tree == TREE


def test_a_package_server_npm_could_not_install_on_approval_is_installed_by_its_first_call(
        conn, monkeypatch):
    """With npm unreachable at the yes, the module is approved all the
    same; left so, it could never run. Its first call installs it."""
    world = _World(monkeypatch)
    admission = _elsewhere(conn, world)
    world.npm_down = True
    module_id = accounts.approve_server(conn, admission, free=[])
    assert world.installed == [] and modules.module(conn, module_id) is not None
    world.npm_down = False

    modules.set_enabled(conn, module_id, True)
    assert mcptools.listed(modules.module(conn, module_id).manifest, lambda _name: None)
    assert [p.tree for p in world.installed] == [TREE]


@pytest.mark.parametrize("asks, reach", [
    ({}, f"which may reach {NOTES}."),
    ({"hosts": [], "anywhere": True}, "which may reach any public host."),
])
def test_a_call_card_says_the_server_runs_on_this_computer_and_where_it_may_reach(
        conn, monkeypatch, asks, reach):
    """A server Steve runs has no address: its card named the empty url and
    ended "It goes to .", telling the user nothing of where the arguments
    may go. It says the server runs here, and the hosts it may reach."""
    _World(monkeypatch)
    assert passphrase.choose(PHRASE) is None
    module_id = accounts.approve_server(conn, _add(conn, **asks), given=PHRASE, free=[])
    modules.set_enabled(conn, module_id, True)

    [offer, *_] = mcptools.offers(conn)
    said = mcptools.call_card(offer, {"title": "Plan"})

    assert said.endswith(f"It goes to the server Steve runs on this computer, {reach}")


def test_the_modules_page_shows_each_modules_slots_hosts_and_grants(conn, monkeypatch):
    """Once approved, a module's row showed its version, tier and classes
    alone: what the user said yes to, where it may reach above all, could
    not be seen again. Each row now names its slots, its hosts and core's
    reading of its grants; the passphrase the card asked is not asked."""
    world = _World(monkeypatch)
    module_id = accounts.approve_server(conn, _add(conn), free=[])

    listed = {m["id"]: m for m in accounts.listing(conn)["modules"]}
    notes = listed[module_id]
    assert notes["slots"] == ["MCP server"]
    assert notes["hosts"] == [NOTES] and notes["typed"] is False
    assert notes["server"]["package"] == "notes-server@1.0.1"
    assert any(NOTES in line for line in notes["grants"])
    assert any(f"the {len(world.served.tools)} tools of the MCP server notes-server@1.0.1"
               in line for line in notes["grants"])
    assert not any("passphrase" in line for line in notes["grants"])
    imap = listed["imap"]
    assert imap["slots"] == ["connector"] and imap["typed"] is True
    assert any(line.startswith("Can provide") for line in imap["grants"])


def _manifest(mcp_table: dict, hosts=("api.notes.example",)) -> dict:
    return {"manifest_version": 1, "sdk": 1, "id": "notes", "label": "Notes", "for": "Tests",
            "hosts": list(hosts), "mcp": mcp_table}


GOOD = {"package": "notes-server@1.0.1", "entry": "index.js", "before": PUBLISHED,
        "tree": TREE}


@pytest.mark.parametrize("table, hosts, why", [
    ({**GOOD, "url": "https://api.notes.example/mcp"}, (NOTES,), "not both"),
    ({**GOOD, "package": "notes-server@^1.0.0"}, (NOTES,), "exact version"),
    ({**GOOD, "entry": "../../steal.js"}, (NOTES,), "within the package"),
    ({k: v for k, v in GOOD.items() if k != "tree"}, (NOTES,), "tree"),
    ({k: v for k, v in GOOD.items() if k != "before"}, (NOTES,), "published"),
    (GOOD, (), "names none"),
    (GOOD, ("*.notes.example",), "exact names"),
])
def test_a_package_server_in_a_manifest_is_exact_pinned_and_held_to_named_hosts(
        table, hosts, why):
    """Whoever wrote the manifest, Steve or a module from outside, the
    server it names is one version, one tree, and hosts by name."""
    with pytest.raises(ManifestError, match=why):
        parse(_manifest(table, hosts))


def test_a_new_tree_or_version_of_a_package_server_asks_again():
    """The card's yes was to those files: other files are other code."""
    first = parse(_manifest(GOOD))
    for changed in ({**GOOD, "tree": "cd" * 32}, {**GOOD, "package": "notes-server@1.0.2"},
                    {**GOOD, "anywhere": True}):
        assert lifecycle._server_print(parse(_manifest(changed))) != lifecycle._server_print(
            first)
