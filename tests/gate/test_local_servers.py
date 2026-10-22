"""A local server Steve starts reaches its approved hosts and nothing else.

06-modules.md, section 7: a server Steve installs runs in its own container,
whose one way out is a pipe the broker serves (`core/sandbox/relay.py`).
Each test here installs a small MCP server package with npm, in npm's own
container, through that pipe from a fake registry; starts it in its
container; and has it try what a server could: its approved host, another
host, another name behind the approved one, this computer, the private
network, the user's files, the Credential Manager, its home. And code that
skips Steve's loading script.

The registry and the service are fakes behind the broker's network seam,
speaking TLS with an authority made for the test, which Node is told to
trust; nothing here reaches npm or any live service. Nothing is skipped: a
runtime that is not built fails the test (`python -m core.sandbox build`).

Frozen, because a coding pipeline that could edit this could decide its
walls had held.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import socket
import ssl
import tarfile
import threading
from pathlib import Path

import pytest
import trustme

from core import identity, paths
from core.modctx import Network
from core.sandbox import job as jobs
from core.sandbox import launch, probe, profiles, relay, runtime, servers

pytestmark = [pytest.mark.processes, pytest.mark.appcontainer]

REGISTRY_ADDRESS = "151.101.0.162"
NOTES = "api.notes.example"
NOTES_ADDRESS = "93.184.216.34"
OTHER = "other.notes.example"
OTHER_ADDRESS = "93.184.216.35"
INSIDE = "inside.notes.example"      # approved, and pointing into the private network
INSIDE_ADDRESS = "10.0.0.5"
SECRET = "s3cret-token-in-the-log"
WSAEACCES = 10013

# A small MCP server over stdio: each tool tries one way out and says what
# happened, by Node's error code where there is one.
SERVER_JS = r"""
'use strict';
const fs = require('node:fs');
const https = require('node:https');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const tls = require('node:tls');
const readline = require('node:readline');

const failed = (error) => ({ failed: error.code || String(error.message || error) });

function get(url) {
  return new Promise((resolve) => {
    https.get(url, (res) => {
      let body = '';
      res.on('data', (c) => { body += c; });
      res.on('end', () => resolve({ status: res.statusCode, body }));
    }).on('error', (e) => resolve(failed(e)));
  });
}

async function fetched(url) {
  try { const r = await fetch(url); return { status: r.status, body: await r.text() }; }
  catch (e) { return failed(e.cause || e); }
}

function greet(host, servername) {
  return new Promise((resolve) => {
    const s = tls.connect({ host, port: 443, servername }, () => {
      s.end();
      resolve({ greeted: true });
    });
    s.on('error', (e) => resolve(failed(e)));
  });
}

function raw(host, port) {
  return new Promise((resolve) => {
    const s = net.connect({ host, port }, () => { s.end(); resolve({ connected: true }); });
    s.on('error', (e) => resolve(failed(e)));
  });
}

function read(file) {
  try { return { read: fs.readFileSync(file, 'utf8').length }; } catch (e) { return failed(e); }
}

function home() {
  const file = path.join(os.homedir(), 'signed-in.json');
  fs.writeFileSync(file, '{"signed_in": true}');
  return { home: os.homedir(), tmp: os.tmpdir(), wrote: file };
}

function plant() {
  // Code left in the home, where Node looks for a module the tree lacks.
  const where = path.join(os.homedir(), '.node_modules', 'planted');
  fs.mkdirSync(where, { recursive: true });
  fs.writeFileSync(path.join(where, 'index.js'), "module.exports = 'planted';");
  try { return { loaded: require('planted') }; } catch (e) { return failed(e); }
}

const tools = {
  get: (a) => get(a.url),
  fetch: (a) => fetched(a.url),
  greet: (a) => greet(a.host, a.servername),
  raw: (a) => raw(a.host, a.port),
  read: (a) => read(a.file),
  home: () => home(),
  plant: () => plant(),
  say: (a) => { console.error(a.line); return { said: true }; },
};

const send = (m) => process.stdout.write(JSON.stringify(m) + '\n');
readline.createInterface({ input: process.stdin }).on('line', async (line) => {
  const m = JSON.parse(line);
  if (m.method === 'initialize') {
    return send({ jsonrpc: '2.0', id: m.id, result: { protocolVersion: '2025-06-18',
      capabilities: { tools: {} }, serverInfo: { name: 'notes', version: '1.0.0' } } });
  }
  if (m.method === 'tools/list') {
    return send({ jsonrpc: '2.0', id: m.id, result: { tools: Object.keys(tools).map(
      (name) => ({ name, inputSchema: { type: 'object' } })) } });
  }
  if (m.method === 'tools/call') {
    const out = await tools[m.params.name](m.params.arguments || {});
    return send({ jsonrpc: '2.0', id: m.id, result: { content: [
      { type: 'text', text: JSON.stringify(out) }] } });
  }
  if (m.id !== undefined) {
    send({ jsonrpc: '2.0', id: m.id, error: { code: -32601, message: 'no' } });
  }
});
"""


def _tarball(name: str, version: str, files: dict[str, str],
             dependencies: dict[str, str] | None = None) -> bytes:
    raw = io.BytesIO()
    meta = {"name": name, "version": version, "main": "index.js",
            "dependencies": dependencies or {}}
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for file, text in {"package.json": json.dumps(meta), **files}.items():
            data = text.encode()
            info = tarfile.TarInfo(f"package/{file}")
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


class Registry:
    """npm's registry, as much of it as an install reads: each package's
    versions, with when each was published, and their tarballs."""

    def __init__(self) -> None:
        self.versions: dict[str, dict[str, tuple[bytes, str, dict[str, str]]]] = {}
        self.asked: list[str] = []

    def publish(self, name: str, version: str, files: dict[str, str], when: str,
                dependencies: dict[str, str] | None = None) -> bytes:
        tgz = _tarball(name, version, files, dependencies)
        self.versions.setdefault(name, {})[version] = (tgz, when, dependencies or {})
        return tgz

    def url(self, name: str, version: str) -> str:
        return f"https://{servers.REGISTRY}/{name}/-/{name}-{version}.tgz"

    def answer(self, path: str) -> tuple[int, str, bytes]:
        self.asked.append(path)
        name = path.strip("/").split("/")[0]
        if name not in self.versions:
            return 404, "application/json", b"{}"
        if "/-/" in path:
            version = path.rsplit("-", 1)[1].removesuffix(".tgz")
            return 200, "application/octet-stream", self.versions[name][version][0]
        versions = self.versions[name]
        latest = max(versions, key=lambda v: tuple(int(p) for p in v.split(".")))
        document = {
            "name": name, "dist-tags": {"latest": latest},
            "versions": {v: {"name": name, "version": v, "dependencies": deps,
                             "dist": {"tarball": self.url(name, v),
                                      "integrity": _integrity(tgz)}}
                         for v, (tgz, _, deps) in versions.items()},
            "time": {"created": "2026-01-01T00:00:00.000Z",
                     "modified": max(w for _, w, _ in versions.values()),
                     **{v: w for v, (_, w, _) in versions.items()}}}
        return 200, "application/json", json.dumps(document).encode()

    def lockfile(self, name: str, version: str) -> dict:
        tgz = self.versions[name][version][0]
        return {"name": "steve-server", "version": "0.0.0", "lockfileVersion": 3,
                "requires": True,
                "packages": {"": {"name": "steve-server", "version": "0.0.0",
                                  "dependencies": {name: version}},
                             f"node_modules/{name}": {"version": version,
                                                      "resolved": self.url(name, version),
                                                      "integrity": _integrity(tgz)}}}


class Services(Network):
    """The network behind the broker: the registry and the notes service,
    each at its own public address, speaking TLS with the test's authority."""

    def __init__(self, ca: trustme.CA, registry: Registry) -> None:
        self.ca = ca
        self.registry = registry
        self.names = {servers.REGISTRY: [REGISTRY_ADDRESS], NOTES: [NOTES_ADDRESS],
                      OTHER: [OTHER_ADDRESS], INSIDE: [INSIDE_ADDRESS]}
        self.opened: list[tuple[str, int]] = []
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ca.issue_cert(servers.REGISTRY, NOTES, OTHER).configure_cert(self._context)

    def resolve(self, host: str, port: int) -> list[str]:
        if host not in self.names:
            raise socket.gaierror(host)
        return self.names[host]

    def own_ports(self) -> set[int]:
        return set()

    def node_authorities(self) -> str:
        return self.ca.cert_pem.bytes().decode()

    def open_tcp(self, address: str, port: int, timeout: float) -> socket.socket:
        self.opened.append((address, port))
        ours, theirs = socket.socketpair()
        threading.Thread(target=self._serve, args=(address, theirs), daemon=True).start()
        return ours

    def _answer(self, address: str, path: str) -> tuple[int, str, bytes]:
        if address == REGISTRY_ADDRESS:
            return self.registry.answer(path)
        return 200, "text/plain", f"notes from {address}".encode()

    def _serve(self, address: str, raw: socket.socket) -> None:
        with contextlib.suppress(OSError, ValueError), \
                self._context.wrap_socket(raw, server_side=True) as tls:
            reader = tls.makefile("rb")
            while line := reader.readline():
                path = line.decode().split(" ")[1]
                length = 0
                while (header := reader.readline()) not in (b"\r\n", b""):
                    key, _, value = header.decode().partition(":")
                    if key.strip().lower() == "content-length":
                        length = int(value)
                reader.read(length)
                status, kind, body = self._answer(address, path)
                tls.sendall(f"HTTP/1.1 {status} OK\r\nContent-Type: {kind}\r\n"
                            f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


def _redact(text: str) -> str:
    return text.replace(SECRET, "[redacted]")


@pytest.fixture
def registry():
    found = Registry()
    found.publish("notes-server", "1.0.0", {"index.js": SERVER_JS}, "2026-10-01T00:00:00.000Z")
    return found


@pytest.fixture
def network(registry):
    return Services(trustme.CA(), registry)


@pytest.fixture
def supported(monkeypatch):
    """The start's check, as this computer passes it (tests/gate/test_module_sandbox.py)."""
    verdict = probe.Support(True, probe.LPAC, "")
    monkeypatch.setattr(probe, "_verdict", verdict)
    return verdict


def _package(registry: Registry, **kw) -> servers.Package:
    lock = paths.scratch_dir() / "reviewed-lock.json"
    lock.write_text(json.dumps(registry.lockfile("notes-server", "1.0.0")), encoding="utf-8")
    return servers.Package(name="notes-server", version="1.0.0", entry="index.js",
                           hosts={NOTES: (443,)}, lockfile=lock, **kw)


def _installed(registry, network) -> servers.Package:
    package = _package(registry)
    done = servers.install("notes", package, network=network, redact=_redact)
    assert done.packages == 1
    return servers.Package(**{**package.__dict__, "tree": done.tree})


@contextlib.contextmanager
def _running(package, network, key="acct1"):
    running = servers.start("notes", key, package, network=network, redact=_redact)
    try:
        yield running
    finally:
        running.stop()


def _call(running: servers.Running, tool: str, **arguments) -> dict:
    result = running.client.call(tool, arguments)
    return json.loads(result["content"][0]["text"])


def test_a_server_is_installed_through_its_pipe_and_started_only_from_the_tree_approved(
        supported, registry, network):
    """npm runs in its own container and reaches the registry through the
    pipe alone. The tree it installed is hashed, and a start checks the
    hash, so a file changed after the user approved is never run."""
    package = _package(registry)
    done = servers.install("notes", package, network=network, redact=_redact)
    assert done.packages == 1 and done.size > 0
    assert registry.asked == ["/notes-server/-/notes-server-1.0.0.tgz"]
    assert network.opened == [(REGISTRY_ADDRESS, 443)]
    assert not list(paths.scratch_dir().glob("npm-*")), "npm's cache was left behind"
    with pytest.raises(servers.NotInstalled):
        servers.start("notes", "acct1", package, network=network, redact=_redact)
    approved = servers.Package(**{**package.__dict__, "tree": done.tree})
    with _running(approved, network) as running:
        assert running.client.server == {"name": "notes", "version": "1.0.0"}
        assert {t["name"] for t in running.client.tools()} >= {"get", "raw", "home"}
    entry = servers.folder("notes", approved) / servers.TREE / "node_modules" / \
        "notes-server" / "index.js"
    entry.write_text(entry.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
    with pytest.raises(servers.NotInstalled):
        servers.start("notes", "acct1", approved, network=network, redact=_redact)


def test_a_server_reaches_its_approved_host_and_nothing_else(supported, registry, network):
    """The card named its hosts. Another host, another name behind one, an
    address and this computer are each refused at the broker, and none is
    dialled. So is an approved name that resolves inside the network: the
    card's word does not carry a connection past core's check of where a
    name points, and that check alone refuses it."""
    package = servers.Package(**{**_installed(registry, network).__dict__,
                                 "hosts": {NOTES: (443,), INSIDE: (443,)}})
    network.opened.clear()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with _running(package, network) as running:
            assert _call(running, "get", url=f"https://{NOTES}/today") == {
                "status": 200, "body": f"notes from {NOTES_ADDRESS}"}
            assert _call(running, "fetch", url=f"https://{NOTES}/today")["status"] == 200
            refused = [
                _call(running, "get", url=f"https://{OTHER}/today"),
                _call(running, "greet", host=NOTES, servername=OTHER),
                _call(running, "raw", host="127.0.0.1", port=port),
                _call(running, "raw", host="localhost", port=port),
                _call(running, "get", url=f"https://{INSIDE}/"),
                _call(running, "get", url=f"https://{NOTES_ADDRESS}/today"),
            ]
            said = list(running.log.lines)
        listener.setblocking(False)
        with pytest.raises(BlockingIOError):
            listener.accept()
    assert all("failed" in r for r in refused), refused
    assert network.opened == [(NOTES_ADDRESS, 443), (NOTES_ADDRESS, 443)]
    reasons = running.relay.refused
    assert f"{OTHER}:443 is not a host the server was approved for" in reasons
    assert f"it asked for {NOTES} and greeted {OTHER}" in reasons
    assert any("is an address" in r for r in reasons)
    assert f"{INSIDE} was set up as a public address and now resolves to a private one" \
        in reasons
    assert any(r.startswith("steve: a connection refused") for r in said)


def test_code_that_skips_the_loading_script_finds_no_network(supported, registry, network):
    """The script is a convenience. Node started in the server's container
    without it has no network at all: not the internet, not this computer."""
    package = _installed(registry, network)
    with _running(package, network):
        pass
    name = servers.container_name("notes", "acct1")
    container = profiles.ensure(name)
    node = runtime.node(runtime.ready())
    script = (
        "const net = require('node:net');"
        "const tryIt = (host, port) => new Promise((done) => {"
        " const s = net.connect({host, port}, () => { s.end(); done('connected'); });"
        " s.on('error', (e) => done(e.code || String(e.errno))); });"
        "Promise.all([tryIt('192.0.2.1', 443), tryIt('127.0.0.1', Number(process.argv[1]))])"
        ".then((r) => console.log(JSON.stringify(r)));")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        argv = [str(node), *runtime.NODE_FLAGS, "-e", script,
                str(listener.getsockname()[1])]
        with launch.start(argv, container=container, cwd=node.parent,
                          limits=jobs.Limits(memory=256 << 20, processes=1, ui=True),
                          env=launch.environment([]),
                          capabilities=runtime.capabilities(supported.mode == probe.LPAC),
                          lpac=supported.mode == probe.LPAC) as process, \
                process.deadline(30):
            said = json.loads(process.stdout.read() or b"null")
        listener.setblocking(False)
        with pytest.raises(BlockingIOError):
            listener.accept()
    assert said is not None and "connected" not in said, said


def test_a_server_writes_its_home_and_reads_nothing_else_of_the_users(
        supported, registry, network):
    """Its sign-in is kept in its home, as on the user's own computer. The
    data folder beside it holds every other module's state and the user's
    database; the Credential Manager every account's secret."""
    package = _installed(registry, network)
    canary = paths.data_dir() / "canary.txt"
    canary.write_text("the user's", encoding="utf-8")
    with _running(package, network) as running:
        wrote = _call(running, "home")
        refused = [_call(running, "read", file=str(canary)),
                   _call(running, "read", file=str(Path.home() / "NTUSER.DAT"))]
        name = running.container
    assert Path(wrote["wrote"]) == servers.home("notes", "acct1") / "signed-in.json"
    assert (servers.home("notes", "acct1") / "signed-in.json").is_file()
    assert all(r.get("failed") in ("EPERM", "EACCES", "EBUSY") for r in refused), refused
    temp = Path(wrote["tmp"])
    assert servers._storage(name) in temp.parents, temp
    assert not any((servers._storage(name) / "Temp").glob("*")), "its temp outlived it"
    python = runtime.python(runtime.ready())
    child = (
        "import ctypes, sys\n"
        "advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)\n"
        "found = ctypes.c_void_p()\n"
        "ok = advapi32.CredReadW(sys.argv[1], 1, 0, ctypes.byref(found))\n"
        "print('ok' if ok else ctypes.get_last_error())\n")
    lpac = supported.mode == probe.LPAC
    with launch.start([str(python), *runtime.FLAGS, "-c", child,
                       f"{identity.KEYCHAIN}-notes/absent"],
                      container=profiles.ensure(name), cwd=python.parent,
                      limits=jobs.Limits(memory=256 << 20, processes=1, ui=True),
                      env=launch.environment([python.parent]),
                      capabilities=runtime.capabilities(lpac), lpac=lpac) as process, \
            process.deadline(30):
        credential = process.stdout.read().decode().strip()
    assert credential in ("5", "1702"), credential

    # The sign-in is its server's alone: the same server on another account
    # runs in a container of its own, and finds the home closed to it.
    other = profiles.ensure(servers.container_name("notes", "acct2"))
    peek = ("import sys\n"
            "try:\n"
            "    open(sys.argv[1], 'rb').read()\n"
            "    print('read')\n"
            "except OSError as error:\n"
            "    print(error.errno)\n")
    try:
        with launch.start([str(python), *runtime.FLAGS, "-c", peek,
                           str(servers.home("notes", "acct1") / "signed-in.json")],
                          container=other, cwd=python.parent,
                          limits=jobs.Limits(memory=256 << 20, processes=1, ui=True),
                          env=launch.environment([python.parent]),
                          capabilities=runtime.capabilities(lpac), lpac=lpac) as process, \
                process.deadline(30):
            peeked = process.stdout.read().decode().strip()
    finally:
        profiles.delete(other.name)
    assert peeked == "13", peeked


def test_code_a_server_leaves_in_its_home_is_never_loaded_at_its_next_start(
        supported, registry, network):
    """Its home is its own to write, and Node looks there for a module its
    tree lacks (`.node_modules` under USERPROFILE). What it left there
    would run at each start after, past the hash its tree is checked
    against; Steve's loading script takes the home off that search."""
    package = _installed(registry, network)
    for _ in range(2):
        with _running(package, network) as running:
            assert _call(running, "plant") == {"failed": "MODULE_NOT_FOUND"}
    planted = servers.home("notes", "acct1") / ".node_modules" / "planted" / "index.js"
    assert planted.is_file(), "the server could not write its home: the test proves nothing"


def test_what_a_server_says_is_kept_redacted_and_a_sign_in_code_is_read_as_it_passes(
        supported, registry, network):
    package = _installed(registry, network)
    sign_in = servers.SignIn(page="https://login.notes.example/device", code=r"[A-Z0-9]{8}")
    seen: list[tuple[str, str]] = []
    with _running(package, network) as running:
        running.log.watch(lambda line: (found := servers.prompt(line, sign_in))
                          and seen.append(found))
        _call(running, "say", line=f"token {SECRET} cached")
        _call(running, "say", line="To sign in, open https://login.notes.example/device "
                                   "and enter the code ABCD2345.")
        said = list(running.log.lines)
    assert seen == [("https://login.notes.example/device", "ABCD2345")]
    assert "token [redacted] cached" in said and not any(SECRET in line for line in said)


def test_removing_an_account_deletes_its_servers_home_and_its_containers_storage(
        supported, registry, network):
    package = _installed(registry, network)
    with _running(package, network) as running:
        _call(running, "home")
        name = running.container
    assert servers.home("notes", "acct1").is_dir() and servers._storage(name).is_dir()
    servers.forget("notes", "acct1")
    assert not servers.home("notes", "acct1").exists()
    assert not servers._storage(name).exists()


def test_a_server_the_user_adds_is_resolved_as_the_registry_stood_when_it_was_published(
        supported, registry, network):
    """A dependency published after the version the user named could slip
    in under a range; resolved before that moment, it cannot."""
    registry.publish("notes-helper", "1.0.0", {"index.js": ""}, "2026-09-01T00:00:00.000Z")
    registry.publish("notes-helper", "1.1.0", {"index.js": "// newer"},
                     "2026-10-10T00:00:00.000Z")
    registry.publish("notes-server", "1.0.1", {"index.js": SERVER_JS},
                     "2026-10-02T00:00:00.000Z", dependencies={"notes-helper": "^1.0.0"})
    package = servers.Package(name="notes-server", version="1.0.1", entry="index.js",
                              hosts={NOTES: (443,)}, before="2026-10-02T00:00:00.000Z")
    done = servers.install("notes", package, network=network, redact=_redact)
    lock = json.loads((servers.folder("notes", package) / servers.TREE
                       / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"]["node_modules/notes-helper"]["version"] == "1.0.0"
    assert done.packages == 2
    assert relay.Reach({servers.REGISTRY: (443,)}).why_not(servers.REGISTRY, 443) is None


def test_a_server_the_user_adds_is_installed_listed_from_its_container_and_waits_for_a_card(
        supported, registry, network, monkeypatch):
    """Adding one, whole, as the app asks it: npm in its container resolving
    as the registry stood when the version came out, the tree's hash pinned
    in the manifest Steve writes, the server started from that tree in its
    own container to list its tools for the card, and stopped; nothing of
    it runs until the yes."""
    import asyncio
    import functools

    from agent import mcptools, modadmit
    from core import db, lifecycle, modules, screen

    registry.publish("notes-server", "1.0.1", {"index.js": SERVER_JS},
                     "2026-10-02T00:00:00.000Z")
    monkeypatch.setattr(mcptools, "NETWORK", network)
    monkeypatch.setattr(servers, "published", lambda name, version, *, network, now=None:
                        registry.versions[name][version][1])
    monkeypatch.setattr(modadmit, "admit", functools.partial(
        modadmit.admit, screener=lambda conn, artifact: screen.Verdict(verdict="as_is")))
    conn = db.open_store(paths.db_path())
    try:
        modules.register(conn)
        asyncio.run(mcptools.add_package(conn, name="notes", package="notes-server@1.0.1",
                                         entry="index.js", hosts=[NOTES], label="Notes"))
        [card] = lifecycle.waiting(conn)
    finally:
        conn.close()

    assert card["server"]["package"] == "notes-server@1.0.1"
    assert card["server"]["packages"] == 1 and len(card["server"]["tree"]) == 64
    assert {t["name"] for t in card["server"]["tools"]} >= {"get", "raw", "home"}
    assert "/notes-server/-/notes-server-1.0.1.tgz" in registry.asked
    assert servers.POOL.running("notes", servers.MODULE_KEY) is None
