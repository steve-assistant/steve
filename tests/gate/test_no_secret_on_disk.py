"""No connector's secret is kept on disk: only in the Credential Manager.

Before step 5, Google's token was `google_token.json` in the secrets folder,
readable by anything the user ran, and its OAuth client a file wherever the
user kept it, its path a key of its own; a mailbox's password and a course
site's private feed were keys in the keychain, typed in by hand. Now setup
writes every secret field through `core/credstore.py`, under the module's
and the account's keys, and nothing else (05-connectors.md, section 9).

This test runs setup end to end on a test module and on a calendar link,
whose address is its password, and a Google account signed in by a fake
browser, all through the app's routes, and reads every file in the data
folder for any of the secrets, as text and as Windows' UTF-16. Step 6
adds one exception, by path: the home of a local MCP server Steve runs,
where the server keeps its own sign-in.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from starlette.applications import Starlette

from agent import connectors, runtime
from agent.worker import accounts as account_routes
from core import credstore, db, egress, modules, paths
from core.sandbox import servers
from steve_sdk.classes import MailProvider
from steve_sdk.manifest import parse
from tests.fakes import ics as feeds

PASSWORD = "app-password-5f1e9c"
CLIENT_FILE = json.dumps({"installed": {"client_id": "id-7c1d", "client_secret": "cs-90ab2e",
                                        "project_id": "maya-1"}})
REFRESH, ACCESS = "rt-3d8f61", "at-b4c27a"
# Of a calendar link, only the host may be kept: its path and the token in
# it each open the feed.
SECRETS = (PASSWORD, "cs-90ab2e", REFRESH, ACCESS, feeds.TOKEN, feeds.PATH)

VAULTISH = parse({
    "manifest_version": 1, "sdk": 1, "id": "vaultish", "label": "Vaultish", "for": "Tests",
    "classes": ["mail"],
    "setup": {"steps": ["address", "fields", "test"],
              "fields": [{"name": "password", "type": "secret", "label": "Password"},
                         {"name": "client", "type": "file", "label": "Client file"}]},
    "mail": {"engine": "imap", "imap": "imap.vaultish.example:993",
             "smtp": "smtp.vaultish.example:465"},
})


class _Box(MailProvider):
    """An account that reads its secrets through its context, as a module
    does, and finds them."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def test(self) -> str:
        assert self.ctx.secret("password") == PASSWORD
        assert self.ctx.secret("client") == CLIENT_FILE
        return "signed in"

    def search(self, *a, **k):
        return []

    def arrived(self, *a, **k):
        return []

    def fetch(self, *a, **k):
        return None

    def make_draft(self, draft):
        raise NotImplementedError

    def send_draft(self, message):
        raise NotImplementedError

    def delete(self, message):
        raise NotImplementedError

    def sent_items(self, *a, **k):
        return []

    def headers(self, *a, **k):
        return {}

    def envelopes(self, *a, **k):
        return []

    def ignored(self, *a, **k):
        return []


def _call(app, method: str, path: str, **kw) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.request(method, path, **kw)

    return asyncio.run(go())


def _a_servers_home(path) -> bool:
    """The one exception: a file in a local MCP server's home
    (`servers.home`), where the server keeps its own sign-in, as it would
    on the user's own computer. By that function's path alone, so moving
    the home moves the exception, and nothing else can hide there."""
    try:
        parts = path.relative_to(paths.modules_dir()).parts
    except ValueError:
        return False
    return (len(parts) > 3 and parts[1] == "server-homes"
            and path.is_relative_to(servers.home(parts[0], parts[2])))


def _found_in(root, secrets) -> list[str]:
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _a_servers_home(path):
            continue
        data = path.read_bytes()
        for secret in secrets:
            if secret.encode() in data or secret.encode("utf-16-le") in data:
                found.append(f"{path.relative_to(root)} holds {secret!r}")
    return found


def test_after_setup_the_data_folder_holds_no_password_token_or_client_secret(monkeypatch):
    manifests = {**modules.builtins(), "vaultish": VAULTISH}
    monkeypatch.setattr(modules, "builtins", lambda: manifests)
    monkeypatch.setitem(connectors.ENGINES, "imap", lambda ctx, spec: _Box(ctx))
    rig = feeds.FeedRig()
    rig.serve(raw=feeds.COURSE)
    monkeypatch.setattr(connectors, "NETWORK", rig.network)
    # Setup pins the link's host to what it resolves to, by the system's
    # lookup: here, the feed's one public address.
    monkeypatch.setattr(egress.socket, "getaddrinfo", lambda host, port, *a, **k: [
        (egress.socket.AF_INET, egress.socket.SOCK_STREAM, 6, "", (feeds.PUBLIC_IP, port))])
    conn = db.open_store(paths.db_path())
    try:
        modules.register(conn)
        monkeypatch.setattr(runtime, "conn", conn)
        app = Starlette(routes=account_routes.ROUTES)

        added = _call(app, "POST", "/control/accounts", json={
            "op": "add", "module": "vaultish", "address": "maya@vaultish.example",
            "fields": {"password": PASSWORD, "client": CLIENT_FILE}}).json()
        listed = _call(app, "GET", "/control/modules")
        _call(app, "POST", "/control/accounts", json={
            "op": "edit", "key": added["key"], "name": "Vault"})
        linked = _call(app, "POST", "/control/accounts", json={
            "op": "add", "module": "ics", "name": "Course site",
            "fields": {"link": feeds.LINK, "due_dates": True}}).json()
        listed = _call(app, "GET", "/control/modules")

        # Google, as the app adds it: the client file, then the browser
        # sign-in, whose answer holds an access token and a refresh token.
        from modules.google_cloud import auth as google
        from tests.fakes import google as fake_google

        flow = fake_google.Flow(refresh=REFRESH)
        flow.answer["access_token"] = ACCESS
        monkeypatch.setattr(google, "InstalledAppFlow", flow)
        googled = _call(app, "POST", "/control/accounts", json={
            "op": "add", "module": "google_cloud", "address": fake_google.ADDRESS,
            "fields": {"client": CLIENT_FILE}}).json()
        signed = _call(app, "POST", "/control/accounts", json={
            "op": "sign_in", "key": googled["key"]}).json()
    finally:
        conn.close()
        rig.close()

    found = _found_in(paths.data_dir(), SECRETS)
    if not paths.scratch_dir().is_relative_to(paths.data_dir()):
        found += _found_in(paths.scratch_dir(), SECRETS)
    assert found == []
    assert not any(s in listed.text for s in SECRETS), "the app was handed a secret"
    assert added["ok"] and added["test"]["ok"], added
    assert linked["ok"] and linked["test"]["ok"], linked
    assert rig.network.requests, "the link was never read, so had no chance to leak"
    assert googled["ok"] and flow.asked, (googled, signed)
    held = credstore.names(credstore.prefix())
    # Vaultish's password and client, the link, Google's client and its
    # sign-in: the refresh token alone.
    assert len(held) == 5, held


def test_a_servers_home_is_the_one_place_a_secret_may_be_and_only_inside_it():
    """A local server keeps its sign-in in its home (06-modules.md, section
    7). The exception is that folder, by path: the same secret one level
    out, or in a folder named like it, is still found."""
    home = servers.home("vaultish", "0123456789ab")
    beside = paths.modules_dir() / "vaultish" / "state" / "0123456789ab"
    lookalike = paths.modules_dir() / "vaultish" / "server-homes-old" / "0123456789ab"
    for folder in (home / ".cache", beside, lookalike, home.parent):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "token.json").write_text(ACCESS, encoding="utf-8", newline="\n")

    found = _found_in(paths.data_dir(), (ACCESS,))

    assert sorted(f.split(" holds ")[0].replace("\\", "/") for f in found) == [
        "modules/vaultish/server-homes-old/0123456789ab/token.json",
        "modules/vaultish/server-homes/token.json",
        "modules/vaultish/state/0123456789ab/token.json",
    ]


def test_no_log_line_or_traceback_holds_a_secret_steve_read_or_one_named(tmp_path):
    """`log.exception` wrote an error's text whole, and a worker's last lines
    went on into worker_deaths.jsonl and the issues table (step 11's audit).
    Each record is scrubbed as it is made: a secret Steve read, by value, and
    a token by its name, in the message, its arguments and the traceback;
    and a line with neither is left as it was."""
    import logging

    from core import logscrub, secrets

    name = credstore.target("mod1", "acct1", "password")
    stored = "canary pässword 1"
    log = logging.getLogger("steve.scrub-test")
    path = tmp_path / "worker.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    logscrub.install()  # the fences put logging back as it was after
    try:
        credstore.write(name, stored)
        log.info("stored %s", stored)
        logscrub._held.clear()  # as after a restart: stored before, read now
        held = credstore.read(name)
        log.info("signed in with %s", held)
        key = secrets.get("OPENAI_API_KEY")
        log.info("asking with %s", key)
        log.info("refreshed: access_token=%s", "ya29.canary-token")
        try:
            raise RuntimeError(f"login refused for {held}")
        except RuntimeError:
            log.exception("the account failed")
        log.info("%s %s answered %d", "GET", "/api/day", 200)
    finally:
        log.removeHandler(handler)
        handler.close()

    text = path.read_text(encoding="utf-8")
    assert "canary" not in text and key not in text
    assert text.count(logscrub.MARK) == 5
    assert "GET /api/day answered 200" in text


def test_the_supervisor_scrubs_its_log_and_writes_no_line_per_request(monkeypatch):
    """Its access lines recorded every path and query the app asked for,
    and its log had no scrub."""
    import logging

    from core import supervisor

    served = {}

    def serve(self, sockets=None):
        served["access_log"] = self.config.access_log
        served["scrubs"] = getattr(logging.getLogRecordFactory(), "scrubs", False)
        sockets[0].close()

    monkeypatch.setattr(supervisor.uvicorn.Server, "run", serve)
    monkeypatch.setattr(supervisor.state, "backend", None)
    monkeypatch.setattr(supervisor.state, "server", None)
    monkeypatch.setattr("sys.argv", ["supervisor"])
    supervisor.main()

    assert served == {"access_log": False, "scrubs": True}


def test_a_dead_workers_last_lines_are_scrubbed_before_they_are_copied_on(monkeypatch):
    """What a worker printed past logging (an uncaught error, a fault) is in
    its log unscrubbed, and its tail is kept where pruning cannot reach."""
    from core import supervisor

    monkeypatch.delenv("STEVE_INERT", raising=False)

    class Dead:
        name = "w1"
        log_file = None

    supervisor._note_death(Dead(), "gone", ["Traceback ...\n",
                                            "ValueError: bad api_key=sk-canary-123456\n"])

    kept = supervisor.deaths_path().read_text(encoding="utf-8")
    assert "canary" not in kept and "[secret]" in kept
