"""An account whose MCP server Steve runs: reached, signed in and checked.

`outlook_mcp`'s "Steve runs it" choice (06-modules.md, section 7) has
Steve install the profile's server and start it in a container of its own,
reached on its stdio through the pool rather than at an address the user
typed. The server signs itself in, by a code the user enters in their own
browser, which may be signed in to another account. These hold the
engine to the pool, the sign-in step to the profile's page and code, and
the test to the address that signed in.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import shutil
from pathlib import Path

import pytest

from agent import accounts
from core import db, health, lifecycle, modctx, modules, paths
from core.engines import mcp
from core.sandbox import servers
from steve_sdk.context import Account
from steve_sdk.errors import ConnectorError, SignInNeeded
from steve_sdk.manifest import Manifest, load
from tests.fakes import mailrig
from tests.fakes import mcp as fake

PAGE = "https://microsoft.com/devicelogin"
SAID = (f"To sign in, use a web browser to open the page {PAGE} and enter the code "
        "F7GH2K9XL to authenticate.")


class _Direct(mcp._Rpc):
    """The fake server as the pool hands a started one: opened, its tools
    those Softeria lists, and the accounts it holds signed in."""

    def __init__(self, served: fake.McpServer, holds: list[str]) -> None:
        self.served = served
        self.holds = holds
        self.protocol = fake.PROTOCOL
        self.server = dict(served.info)

    def _exchange(self, method: str, params: dict) -> tuple[None, dict]:
        if method == "tools/call" and params.get("name") == "list-accounts":
            listed = [{"email": a, "isDefault": i == 0} for i, a in enumerate(self.holds)]
            return None, {"content": [{"type": "text", "text": json.dumps(
                {"accounts": listed, "count": len(listed)})}]}
        return None, self.served._dispatch(method, params)


def _provider(rig: fake.McpRig, *, holds: list[str], preset: str = "steve",
              klass: str = "mail", manifest: Manifest | None = None):
    """An account of outlook_mcp with the preset given, its context's
    server the fake, and what the engine asked the pool for."""
    manifest = manifest or mailrig.module("outlook_mcp")
    account = Account(key="acct0003", name="Outlook", module=manifest.id,
                      address=rig.world.user, preset=preset, fields={})
    ctx = modctx.ModuleContext(manifest=manifest, module_key="m0dkey0003", account=account,
                               state_dir=rig.state / "acct0003", network=rig.network,
                               zone=lambda: dt.UTC)
    rig.made.append(ctx)
    asked: list = []
    direct = _Direct(rig.server, holds)
    ctx.server = lambda package, watch=None: asked.append(package) or direct
    return mcp.engine(ctx, manifest.specs[klass]), asked


@pytest.fixture
def rig(tmp_path):
    made = fake.rig(tmp_path / "rig")
    yield made
    made.close()


def test_an_account_steve_runs_reaches_its_server_through_the_pool_and_no_address(rig):
    """Its server is the profile's package, started in its container and
    spoken to on its stdio; nothing is sent to an address."""
    provider, asked = _provider(rig, holds=[rig.world.user])

    provider.test()
    found = provider.search("swim", limit=5)

    assert rig.network.reached == []
    package = asked[0]
    assert (package.name, package.version) == ("@softeria/ms-365-mcp-server", "0.160.0")
    assert set(package.hosts) == {"login.microsoftonline.com", "graph.microsoft.com"}
    assert package.env["MS365_MCP_TENANT_ID"] == "consumers"
    assert isinstance(found, list)


@pytest.mark.parametrize("holds", [
    ["someone.else@outlook.example"],
    [fake.MAYA, "someone.else@outlook.example"],
    [],
])
def test_a_server_signed_in_as_any_other_than_the_account_fails_its_test_and_pins_nothing(
        rig, holds):
    """The user's browser may be signed in to another mailbox, theirs or
    someone else's; that one would answer under this account's name. A
    server holding two accounts may answer from either."""
    provider, _ = _provider(rig, holds=holds)

    with pytest.raises(SignInNeeded, match="sign it in again"):
        provider.test()
    assert not (rig.state / "acct0003" / mcp.PIN_FILE).exists()


def test_an_account_the_user_runs_the_server_for_still_needs_its_address(rig):
    """The other choice: no pool, and no address typed is said, not guessed."""
    provider, asked = _provider(rig, holds=[], preset="yours")

    with pytest.raises(ConnectorError, match="address"):
        provider.test()
    assert asked == []


def _profile(tmp_path: Path, package: str) -> Path:
    shipped = mailrig.module("outlook_mcp").path.parent / "profiles" / "softeria.toml"
    text = shipped.read_text(encoding="utf-8").split("\n[package]")[0]
    path = tmp_path / "profiles" / "softeria.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n" + package, encoding="utf-8", newline="\n")
    return path


@pytest.mark.parametrize("table, why", [
    ('[package]\npackage = "notes@1.0.0"\nentry = "i.js"\nhosts = ["a.example"]\n'
     'lockfile = "../../elsewhere.json"\n', "beside"),
    ('[package]\npackage = "notes@1.0.0"\nentry = "i.js"\nhosts = ["a.example"]\n'
     'env = { NODE_OPTIONS = "--inspect" }\n', "setting"),
    ('[package]\npackage = "notes@1.0.0"\nentry = "i.js"\nhosts = ["a.example"]\n'
     'env = { HTTPS_PROXY = "http://elsewhere.example" }\n', "setting"),
    ('[package]\npackage = "notes@^1.0.0"\nentry = "i.js"\nhosts = ["a.example"]\n', "exact"),
    ('[package]\npackage = "notes@1.0.0"\nentry = "i.js"\nhosts = ["a.example"]\n'
     'anywhere = true\n', "anywhere"),
])
def test_a_profiles_server_is_one_exact_package_held_to_its_hosts(tmp_path, table, why):
    """A built-in's server is what the release reviewed: an exact version,
    its lockfile its own, its hosts named. A setting that moves Node's
    options or its proxy would take its network or its code elsewhere."""
    with pytest.raises(ConnectorError, match=why):
        mcp.load(_profile(tmp_path, table))


def _store(tmp_path, preset: str) -> tuple:
    conn = db.open_store(tmp_path / "accounts.db")
    modules.register(conn)
    key = accounts.add(conn, module_id="outlook_mcp", address=fake.MAYA, preset=preset,
                       values={})
    return conn, key


class _Started:
    """A server the pool started: says `stderr` as it starts, and answers
    its sign-in tool with `answer`."""

    def __init__(self, log: servers.Log, answer: str) -> None:
        self.log = log
        self.answer = answer
        self.client = self
        self.server = {}

    def call(self, tool: str, arguments: dict) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(
            {"error": "device_code_required", "message": self.answer})}]}

    def alive(self) -> bool:
        return True

    def stop(self) -> None:
        pass


def _pool(monkeypatch, *, answer: str = "", stderr: str = "", installed: bool = False):
    did: list[str] = []

    def starter(module_id, key, package, *, network, redact, watch=None):
        log = servers.Log(redact)
        if watch is not None:
            log.watch(watch)
        if stderr:
            log.add(stderr)
        did.append(f"start {package.name}")
        return _Started(log, answer)

    monkeypatch.setattr(servers, "POOL", servers.Pool(starter=starter))
    monkeypatch.setattr(servers, "installed", lambda module_id, package: installed)
    monkeypatch.setattr(servers, "install", lambda module_id, package, **kw: did.append(
        f"install {package.name}"))
    return did


@pytest.mark.parametrize("answer, stderr", [(SAID, ""), ("", SAID)])
def test_the_sign_in_step_installs_starts_and_shows_the_profiles_page_and_its_code(
        monkeypatch, tmp_path, answer, stderr):
    """From the sign-in tool's answer, or from the server's first line on
    stderr: either way the page shown is the profile's and the code one of
    its pattern, and the server is left running for the user to finish."""
    did = _pool(monkeypatch, answer=answer, stderr=stderr)
    conn, key = _store(tmp_path, "steve")
    try:
        assert accounts.next_step(mailrig.module("outlook_mcp"), "steve") == "server_sign_in"
        shown = asyncio.run(accounts.server_sign_in(conn, key, wait_s=2))
    finally:
        conn.close()

    assert shown == {"page": PAGE, "code": "F7GH2K9XL", "expires_s": 900}
    assert did == ["install @softeria/ms-365-mcp-server", "start @softeria/ms-365-mcp-server"]
    running = servers.POOL.running("outlook_mcp", key)
    assert running is not None and running.log._watchers == []


def test_a_sign_in_that_names_another_page_is_not_shown(monkeypatch, tmp_path):
    """A server that writes its own page would send the user where it
    chose, to type a code that signs its maker in."""
    _pool(monkeypatch, answer=SAID.replace(PAGE, "https://microsoft-login.example/device"),
          installed=True)
    conn, key = _store(tmp_path, "steve")
    try:
        with pytest.raises(modules.AccountError, match="no sign-in code"):
            asyncio.run(accounts.server_sign_in(conn, key, wait_s=0.3))
    finally:
        conn.close()


@pytest.mark.parametrize("preset, step", [("steve", "server_sign_in"), ("yours", None)])
def test_an_account_whose_server_sign_in_lapsed_tells_the_app_to_sign_in_its_server_again(
        tmp_path, preset, step):
    """Its health asks the user to sign in again, and the card's button
    called the browser's sign-in, which refused: "has no browser sign-in".
    The server's sign-in could be reached only while adding the account.
    The listing says which sign-in the account takes, for the card's
    button; one whose server the user runs takes none of Steve's."""
    conn, key = _store(tmp_path, preset)
    try:
        health.record(conn, key, kind="sign_in")
        [account] = [a for a in accounts.listing(conn)["accounts"] if a["key"] == key]
    finally:
        conn.close()

    assert account["action"] == "sign_in"
    assert account["sign_in"] == step


def test_an_account_whose_server_the_user_runs_has_no_server_sign_in(tmp_path):
    conn, key = _store(tmp_path, "yours")
    try:
        assert accounts.next_step(mailrig.module("outlook_mcp"), "yours") is None
        with pytest.raises(modules.AccountError, match="no server"):
            asyncio.run(accounts.server_sign_in(conn, key))
    finally:
        conn.close()


def _outside(tmp_path: Path) -> Path:
    """outlook_mcp as a module from outside would copy it: its own id and
    domain, the sandbox tier, and one more host for its profile's server."""
    folder = tmp_path / "notmail"
    shutil.copytree(paths.SOURCE / "modules" / "outlook_mcp", folder,
                    ignore=shutil.ignore_patterns("__pycache__"))
    manifest = folder / "manifest.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8")
                        .replace('id = "outlook_mcp"', 'id = "notmail"')
                        .replace('tier = "backend"', 'tier = "sandbox"')
                        .replace('domains = ["outlook.com", "hotmail.com", "live.com", "msn.com"]',
                                 'domains = ["notmail.example"]'),
                        encoding="utf-8", newline="\n")
    profile = folder / "profiles" / "softeria.toml"
    profile.write_text(profile.read_text(encoding="utf-8").replace(
        'hosts = ["login.microsoftonline.com", "graph.microsoft.com"]',
        'hosts = ["login.microsoftonline.com", "graph.microsoft.com", "elsewhere.example"]'),
        encoding="utf-8", newline="\n")
    return folder


def test_only_a_module_steve_ships_has_steve_install_and_run_its_profiles_server(rig, tmp_path):
    """A profile's `[package]` is code Steve installs and runs, reaching the
    hosts the profile names. From a module from outside, no card showed
    those hosts, and nothing checked the package as Steve checks its own:
    the review of steps 5 and 6 (15 October) found a copy of outlook_mcp
    that would have run one reaching `elsewhere.example`. Admission refuses
    it, and the account's sign-in and its engine run none for it."""
    folder = _outside(tmp_path)
    copied = load(folder / "manifest.toml")
    assert "elsewhere.example" in mcp.profiles(copied, "mail")[0].package.hosts

    assert "Steve runs only the servers of the modules it ships" in (
        lifecycle.screened(folder) or "")
    assert accounts.server_package(copied, "steve") is None
    assert accounts.next_step(copied, "steve") is None
    provider, asked = _provider(rig, holds=[rig.world.user], manifest=copied)
    with pytest.raises(ConnectorError, match="only a module Steve ships"):
        provider.test()
    assert asked == []
    assert accounts.server_package(mailrig.module("outlook_mcp"), "steve") is not None
