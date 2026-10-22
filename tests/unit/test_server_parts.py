"""The parts of a local MCP server Steve starts that run without a container.

`core/sandbox/servers.py` installs and starts a server in its container
(tests/gate/test_local_servers.py proves that, with Node). What is checked
here is what decides before or around it: which package and version may be
named, how npm is asked, how old a version the user names must be, what is
kept of what a server says, which sign-in page and code are shown, when a
server is started and stopped, and that removing an account or a module
deletes the server's home and its container by name.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import threading
from pathlib import Path

import httpx
import pytest

from core import db, modctx, modules, paths
from core.modctx import Network
from core.sandbox import servers
from steve_sdk.manifest import parse

NOTES = "api.notes.example"
PAGE = "https://login.notes.example/device"
SIGN_IN = servers.SignIn(PAGE, r"[A-Z0-9]{4}-[A-Z0-9]{4}")
KEY = "0123456789ab"


def _package(**kw) -> servers.Package:
    return servers.Package(**{"name": "@notes/server", "version": "1.2.3",
                              "entry": "dist/index.js", "hosts": {NOTES: (443,)}, **kw})


@pytest.mark.parametrize("name, version, entry", [
    ("notes-server", "^1.2.3", "index.js"),
    ("notes-server", "latest", "index.js"),
    ("Notes Server", "1.2.3", "index.js"),
    ("notes-server", "1.2.3", "../escape.js"),
    ("notes-server", "1.2.3", "C:/elsewhere/index.js"),
    ("notes-server", "1.2.3", "index.sh"),
])
def test_a_package_is_named_at_one_exact_version_and_starts_from_a_script_inside_it(
        name, version, entry):
    """A range or a tag moves under the user's approval, and an entry that
    leaves the package would start code the tree's hash does not cover."""
    with pytest.raises(ValueError):
        servers.Package(name=name, version=version, entry=entry, hosts={NOTES: (443,)})


@pytest.mark.parametrize("env", [
    {"HOME": "C:/elsewhere"},
    {"APPDATA": "C:/elsewhere"},
    {"NODE_OPTIONS": "--require=C:/elsewhere/x.js"},
    {"STEVE_PIPE": "elsewhere"},
    {"HTTPS_PROXY": "http://proxy.example:8080"},
    {"not a name": "x"},
])
def test_a_packages_environment_cannot_move_its_home_its_runtime_or_its_network(env):
    """A profile sets what its server reads at start, such as its tenant.
    Its home is where its sign-in is kept, Node's own variables load other
    code, and a proxy is a way around the relay: none is a profile's to set."""
    assert _package(env={"MS365_MCP_TENANT_ID": "consumers"}).env
    with pytest.raises(ValueError):
        _package(env=env)


def test_a_server_is_installed_as_approved_only_while_its_tree_hashes_to_the_approval():
    """A file changed after the card's yes is code the user never saw: a
    start checks the hash, and a package with no hash approved never starts."""
    tree = servers.folder("notes", _package()) / servers.TREE
    tree.mkdir(parents=True)
    (tree / "index.js").write_text("// as approved\n", encoding="utf-8", newline="\n")
    approved = _package(tree=servers.tree_hash(servers.folder("notes", _package())))

    assert servers.installed("notes", approved)
    assert not servers.installed("notes", _package())
    (tree / "index.js").write_text("// changed\n", encoding="utf-8", newline="\n")
    assert not servers.installed("notes", approved)


def test_npm_is_asked_for_the_lockfile_or_for_the_registry_as_it_stood_and_never_runs_scripts():
    """`npm ci` from a reviewed lockfile installs exactly it; a server the
    user adds resolves as the registry stood when its version came out, so
    a dependency published since cannot slip in. Install scripts run code
    before anything is approved, so neither runs them."""
    built_in = servers.npm_argv(_package(lockfile=Path("reviewed.json")))
    added = servers.npm_argv(_package(before="2026-09-01T00:00:00.000Z"))

    assert built_in[0] == "ci"
    assert added[:2] == ["install", "--save-exact"]
    assert "--before=2026-09-01T00:00:00.000Z" in added
    for argv in (built_in, added):
        assert "--ignore-scripts" in argv


def test_npm_never_resolves_as_the_registry_stood_less_than_seven_days_ago():
    """A server module's manifest names its own `before`, and one imported
    from a zip could name today: npm would then resolve the newest of every
    dependency, the hijacked release the seven days are there to wait out."""
    with pytest.raises(servers.NotInstalled, match="7 days"):
        servers.npm_argv(_package(before="2026-10-09T13:00:00.000Z"), now=NOW)
    with pytest.raises(servers.NotInstalled, match="7 days"):
        servers.npm_argv(_package(before="2026-11-01T00:00:00.000Z"), now=NOW)
    assert "--before=2026-10-08T11:00:00.000Z" in servers.npm_argv(
        _package(before="2026-10-08T11:00:00.000Z"), now=NOW)
    with pytest.raises(servers.NotInstalled):
        servers.npm_argv(_package())


class _Stopped(Exception):
    """Where a test's install ends: at npm's container, before npm."""


class _NpmContainer:
    """npm's container, as an install reaches it once the version's folder
    is written: each install that gets here waits a moment for another,
    then stops."""

    def __init__(self) -> None:
        self.inside = self.most = 0
        self.both = threading.Event()
        self._lock = threading.Lock()

    def ensure(self, name: str):
        with self._lock:
            self.inside += 1
            self.most = max(self.most, self.inside)
            if self.inside == 2:
                self.both.set()
        self.both.wait(0.5)
        with self._lock:
            self.inside -= 1
        raise _Stopped(name)

    def delete(self, name: str) -> None:
        pass


def _held_npm(monkeypatch) -> _NpmContainer:
    held = _NpmContainer()
    monkeypatch.setattr(servers, "_supported", lambda: False)
    monkeypatch.setattr(servers.profiles, "ensure", held.ensure)
    monkeypatch.setattr(servers.profiles, "delete", held.delete)
    return held


def test_two_installs_of_one_modules_server_take_turns_in_its_folder(monkeypatch):
    """Each install deletes the version's folder and writes it, with npm in
    a container named for the module, which each deletes as it ends. Two at
    once, as when two accounts sign in together, would delete each other's
    files and container mid-install."""
    held = _held_npm(monkeypatch)
    package = _package(before="2026-10-01T00:00:00.000Z")
    ended: list[str] = []

    def install() -> None:
        with pytest.raises(_Stopped):
            servers.install("notes", package, network=Network(), redact=str)
        ended.append("stopped at npm")

    both = [threading.Thread(target=install) for _ in range(2)]
    for thread in both:
        thread.start()
    for thread in both:
        thread.join(10)
    assert ended == ["stopped at npm"] * 2
    assert held.most == 1, "two installs were in the module's folder at once"


def test_an_install_that_finds_the_tree_approved_installed_keeps_it_and_runs_no_npm(
        monkeypatch):
    """The one that waited its turn: the other installed the tree approved,
    which a server may be running from by now. Deleted and installed again,
    it would be pulled from under that server, for nothing."""
    _held_npm(monkeypatch)
    where = servers.folder("notes", _package()) / servers.TREE
    (where / "node_modules" / "@notes" / "server").mkdir(parents=True)
    (where / "node_modules" / "@notes" / "server" / "index.js").write_text(
        "// hi\n", encoding="utf-8", newline="\n")
    (where / "package-lock.json").write_text(json.dumps(
        {"packages": {"": {}, "node_modules/@notes/server": {"version": "1.2.3"}}}),
        encoding="utf-8")
    approved = _package(lockfile=Path("reviewed.json"),
                        tree=servers.tree_hash(where.parent))

    done = servers.install("notes", approved, network=Network(), redact=str)

    assert done.tree == approved.tree and done.packages == 1 and done.size > 0
    assert servers.installed("notes", approved)


def test_a_moment_too_recent_installs_nothing_and_starts_no_npm(monkeypatch):
    """Every install comes here, the user's add and a server module's first
    use alike, so the age holds for a manifest's own `before` too; refused,
    it starts no container and leaves the folder as it was."""
    held = _held_npm(monkeypatch)
    yesterday = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")

    with pytest.raises(servers.NotInstalled, match="7 days"):
        servers.install("notes", _package(before=yesterday), network=Network(), redact=str)
    assert held.most == 0
    assert not servers.folder("notes", _package()).exists()


class _Registry(Network):
    """The registry's answer for one package, and the requests it saw."""

    def __init__(self, times: dict[str, str] | None, status: int = 200) -> None:
        self.asked: list[httpx.Request] = []
        self._times = times
        self._status = status

    def resolve(self, host: str, port: int) -> list[str]:
        return ["93.184.216.40"]

    def own_ports(self) -> set[int]:
        return set()

    def transport_for(self, host: str) -> httpx.BaseTransport:
        def answer(request: httpx.Request) -> httpx.Response:
            self.asked.append(request)
            return httpx.Response(self._status, json={"name": "@notes/server",
                                                      "time": self._times})
        return httpx.MockTransport(answer)


NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)


def test_a_version_the_user_names_is_installed_only_once_it_is_seven_days_old():
    """A hijacked release is usually found and pulled within days; one
    named before then could be it. The date is the registry's, read
    through the broker from the registry alone."""
    registry = _Registry({"1.2.3": "2026-10-08T11:00:00.000Z",
                          "1.2.4": "2026-10-09T13:00:00.000Z"})

    assert servers.published("@notes/server", "1.2.3", network=registry,
                             now=NOW) == "2026-10-08T11:00:00.000Z"
    with pytest.raises(servers.NotInstalled, match="7 days"):
        servers.published("@notes/server", "1.2.4", network=registry, now=NOW)
    with pytest.raises(servers.NotInstalled, match="no @notes/server@9.9.9"):
        servers.published("@notes/server", "9.9.9", network=registry, now=NOW)
    assert {str(r.url) for r in registry.asked} == {
        "https://registry.npmjs.org/@notes%2Fserver"}


@pytest.mark.parametrize("times, status", [
    ({"1.2.3": "last week"}, 200),
    ({"1.2.3": 1757000000}, 200),
    (None, 200),
    ({"1.2.3": "2026-09-01T00:00:00.000Z"}, 404),
])
def test_a_registry_answer_without_a_clear_date_installs_nothing(times, status):
    """A date npm's `--before` could not take, or none, would install the
    newest of everything, which is what the rule is there to stop."""
    with pytest.raises(servers.NotInstalled):
        servers.published("@notes/server", "1.2.3", network=_Registry(times, status), now=NOW)


def test_the_sign_in_shown_is_the_profiles_page_and_a_code_of_its_shape():
    """A server writes its own words; shown as they are, they could send
    the user to a page of the server's choosing. Only the page the profile
    declares is shown, and only a code its pattern fits."""
    said = f"To sign in, open {PAGE} and enter the code ABCD-1234."

    assert servers.prompt(said, SIGN_IN) == (PAGE, "ABCD-1234")
    assert servers.prompt(said.replace(PAGE, PAGE + ".evil.example"), SIGN_IN) is None
    assert servers.prompt(said.replace(PAGE, "https://evil.example/device"), SIGN_IN) is None
    assert servers.prompt(f"Open {PAGE} and enter abcd-12", SIGN_IN) is None
    assert servers.prompt(f"Open {PAGE} and enter XABCD-12345", SIGN_IN) is None
    with pytest.raises(ValueError):
        servers.SignIn("http://login.notes.example/device", ".+")


def test_what_a_server_says_is_kept_redacted_cut_and_only_its_last_lines():
    """A server may print a token on stderr; a watcher (the sign-in) reads
    the raw line as it passes, and only the redacted line is kept, so the
    raw one is nowhere once it has gone by."""
    seen: list[str] = []
    said = servers.Log(lambda text: text.replace("secret-7f", "[redacted]"))
    said.watch(seen.append)
    said.watch(lambda line: 1 / 0)  # a broken watcher loses no line

    said.add("token secret-7f\n")
    for n in range(servers.LOG_LINES + 5):
        said.add(f"line {n} " + "x" * servers.LOG_LINE)

    assert seen[0] == "token secret-7f\n"
    assert len(said.lines) == servers.LOG_LINES
    assert all(len(line) <= servers.LOG_LINE for line in said.lines)
    assert said.lines[-1].startswith(f"line {servers.LOG_LINES + 4} ")
    first = servers.Log(lambda text: text.replace("secret-7f", "[redacted]"))
    first.add("token secret-7f\n")
    assert list(first.lines) == ["token [redacted]"]


class _Started:
    """A server as the pool sees one: alive until told otherwise."""

    def __init__(self, events: list[str], at: tuple[str, str]) -> None:
        self.events = events
        self.at = at
        self.client = object()
        self.ended = False
        self.log = servers.Log(str)

    def alive(self) -> bool:
        return not self.ended

    def stop(self) -> None:
        self.events.append(f"stop {self.at[1]}")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _pool(events: list[str], clock: _Clock) -> tuple[servers.Pool, list[_Started]]:
    started: list[_Started] = []

    def starter(module_id, key, package, *, network, redact, watch=None):
        events.append(f"start {key}")
        started.append(_Started(events, (module_id, key)))
        return started[-1]

    return servers.Pool(clock=clock, starter=starter), started


def test_a_server_starts_when_a_call_needs_it_and_stops_when_left_idle():
    """Each server holds up to half a gigabyte: started for the call that
    needs it, kept while calls come, stopped once none has for a while."""
    events: list[str] = []
    clock = _Clock()
    pool, started = _pool(events, clock)
    asks = {"package": _package(), "network": Network(), "redact": str}

    first = pool.client("notes", "acct1", **asks)
    assert pool.client("notes", "acct1", **asks) is first
    pool.client("notes", "acct2", **asks)
    clock.now += servers.IDLE_S - 1
    pool.client("notes", "acct2", **asks)
    clock.now += 1

    assert pool.stop_idle() == [("notes", "acct1")]
    assert events == ["start acct1", "start acct2", "stop acct1"]
    assert pool.running("notes", "acct1") is None
    assert pool.running("notes", "acct2") is started[1]


def test_a_sign_in_watching_a_server_already_running_sees_what_it_says_next():
    """The sign-in step may find its account's server started by an
    earlier call; the code that server prints next must still reach it."""
    pool, started = _pool([], _Clock())
    asks = {"package": _package(), "network": Network(), "redact": str}
    pool.client("notes", "acct1", **asks)
    seen: list[str] = []

    pool.client("notes", "acct1", **asks, watch=seen.append)
    started[0].log.add(f"open {PAGE} and enter ABCD-1234\n")

    assert len(started) == 1 and seen == [f"open {PAGE} and enter ABCD-1234\n"]


def test_a_server_that_ended_by_itself_is_started_afresh_and_counted():
    """One that ended between calls is counted and logged (one that ends
    during a call is its watch's crash); the next call gets a server, not
    the dead one's closed pipes."""
    events: list[str] = []
    pool, started = _pool(events, _Clock())
    asks = {"package": _package(), "network": Network(), "redact": str}

    pool.client("notes", "acct1", **asks)
    started[0].ended = True
    again = pool.client("notes", "acct1", **asks)

    assert again is started[1].client
    assert events == ["start acct1", "stop acct1", "start acct1"]
    assert pool.crashes == {("notes", "acct1"): 1}


def _account_world(monkeypatch, tmp_path):
    """A module with a server for each of two accounts, its homes written,
    and the pool and the container store recording what is done."""
    events: list[str] = []
    pool, _ = _pool(events, _Clock())
    monkeypatch.setattr(servers, "POOL", pool)
    monkeypatch.setattr(servers.profiles, "delete", lambda name: events.append(f"drop {name}"))
    manifest = parse({"manifest_version": 1, "sdk": 1, "id": "notesish", "label": "Notes",
                      "for": "Tests", "classes": ["calendar"], "calendar": {"engine": "ics"},
                      "setup": {"steps": ["fields", "test"]}})
    conn = db.open_store(tmp_path / "servers.db")
    modules.register(conn, {"notesish": manifest})
    keys = [modules.add_account(conn, module_id="notesish", name=name, classes=["calendar"],
                                addresses=[], manifests={"notesish": manifest})
            for name in ("Work", "Home")]
    for key in keys:
        pool.client("notesish", key, _package(), network=Network(), redact=str)
        (servers.home("notesish", key) / ".cache").mkdir(parents=True)
        (servers.home("notesish", key) / ".cache" / "token.json").write_text(
            json.dumps({"refresh": "r-1"}), encoding="utf-8", newline="\n")
    events.clear()
    return conn, keys, events


def test_removing_an_account_stops_its_server_then_deletes_its_home_and_container(
        monkeypatch, tmp_path):
    """The server keeps its sign-in in its home; left behind, the sign-in
    outlives the account. Its container is named for the account, so
    nothing else would delete it. Stopped first: a running server holds
    its home open and would write it again."""
    conn, (work, home_key), events = _account_world(monkeypatch, tmp_path)
    try:
        modules.remove_account(conn, work)
    finally:
        conn.close()

    assert events == [f"stop {work}", f"drop {servers.container_name('notesish', work)}"]
    assert not servers.home("notesish", work).exists()
    assert servers.home("notesish", home_key).exists()


def test_removing_a_module_deletes_every_servers_container_and_its_deepest_files(
        monkeypatch, tmp_path):
    """npm nests a package's files past Windows' 260 characters; removing
    the module's folder with a plain `rmtree`, errors ignored, left them,
    and a container named for each account outlives the folder."""
    conn, keys, events = _account_world(monkeypatch, tmp_path)
    deep = paths.module_dir("notesish") / "server" / "tree" / "node_modules"
    for n in range(12):
        deep = deep / f"nested-package-{n:02d}"
    long = "\\\\?\\" + str(deep.resolve())
    os.makedirs(long)
    with open(long + "\\cached.js", "w", encoding="utf-8") as f:
        f.write("// deep")
    os.chmod(long + "\\cached.js", stat.S_IREAD)
    assert len(str(deep / "cached.js")) > 260
    try:
        modules.remove_module(conn, "notesish")
    finally:
        conn.close()

    assert sorted(e for e in events if e.startswith("drop")) == sorted(
        f"drop {servers.container_name('notesish', key)}" for key in keys)
    assert not os.path.exists("\\\\?\\" + str(paths.module_dir("notesish").resolve()))


@pytest.mark.parametrize("key", ["../other", "a/b", "..", "", "x" * 41])
def test_an_account_key_is_never_a_path_in_a_servers_home(key):
    """The home is deleted whole on removal; a key that climbed out would
    delete, or hand a container, a folder that is not the server's."""
    with pytest.raises(ValueError):
        servers.home("notesish", key)


@pytest.mark.parametrize("line", [
    "token eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJtYXlhIn0.c2lnbmF0dXJlLXZhbHVl",
    "Authorization: Bearer EwBwA8l6BAAUs5+HQn0N0h2FxWzLaM",
    'cache {"refresh_token": "M.C540_BAY.0.U.-Cr5bq7x"}',
    "client_secret=abcd1234efgh",
    "key 0123456789abcdef0123456789abcdef0123456789",
])
def test_what_is_shaped_like_a_credential_is_scrubbed_from_a_servers_log(line):
    """A server prints its own tokens, which Steve never knew, so they are
    found by shape; and without a model to load, which an install without
    reflection has not got."""
    scrubbed = modctx.scrub(line)

    assert "[secret]" in scrubbed
    assert not any(part in scrubbed for part in ("eyJzdWIi", "EwBwA8l6", "M.C540",
                                                "abcd1234", "0123456789abcdef"))


def test_a_sign_in_sentence_and_its_code_are_kept_readable_in_a_servers_log():
    said = f"To sign in, open {PAGE} and enter the code ABCD-1234. Listening on stdio."

    assert modctx.scrub(said) == said
