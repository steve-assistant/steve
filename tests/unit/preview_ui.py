"""The app's account and module screens against a backend on the fakes, for a person to look at.

Not a test, and not collected as one (its name does not start `test_`):
`python -m devtools.preview` runs this file alone under pytest, so every
fence holds as it does for a test (no live account, no real Credential
Manager, a data folder of its own) while the app's own routes answer a
browser. Run outside pytest, the same code would write the user's real
Credential Manager the moment it added an account.

The world it serves has an account in each health state, two cards waiting
on one of them, and the agent's own mailbox chosen, so each screen has
something to show. Adding an account works against the fake mail server for
the persona's Gmail address and its app password (`PASSWORD`); any other
address fails its test as a wrong password would.

The Extensions page has the built-ins (Skills among them), an imported module
on its second kept version with a third waiting as a card, a card for a
module Steve built (its request, its own words, a review and tests; it
needs the passphrase, `PHRASE`), a card for an imported one, a card for an
MCP server that takes a secret (`SECRET`), builds under way and ended, and
two reports. Admission's screen, reviewer and test run are the unit tests'
fakes (tests/unit/test_lifecycle.py), so a zip imported or a server added
in the browser reaches a card as well: the fake server answers at `URL`.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import secrets
import socket
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from agent import accounts, connectors, mcptools, modadmit, runtime
from agent.worker import accounts as account_routes
from core import db, health, lifecycle, modules, passphrase, paths
from core import mailbox as rules
from core.sandbox import profiles
from tests import persona
from tests.fakes import google as fake_google
from tests.fakes import imap as fake_imap
from tests.fakes import mailrig
from tests.fakes.mcp import MAYA, GraphWorld, McpNetwork, McpServer
from tests.unit.test_lifecycle import (
    HARBOUR,
    PHRASE,
    SAILING,
    Reviewer,
    Screen,
    Trial,
    _append,
    _write,
)
from tests.unit.test_mcp_servers import PUBLIC, SECRET, URL

PASSWORD = "abcdefghijklmnop"  # an app password's form
# Where the launcher is told the port and the token, and how long to serve.
ANNOUNCE = "STEVE_UI_PREVIEW_FILE"
MINUTES = "STEVE_UI_PREVIEW_MINUTES"


def _world(conn) -> dict[str, str]:
    """An account in each state, by the state's name; each added as the
    app adds it, and put in its state as a test or a check would."""
    modules.register(conn)
    keys = {"working": accounts.add(conn, module_id="gmail_password", address=persona.GMAIL,
                                    wanted=["mail"], values={"app_password": PASSWORD})}
    keys["needs a password"] = accounts.add(
        conn, module_id="imap", preset="fastmail", address=persona.OWN_DOMAIN_EMAIL,
        name="Home", values={"password": "an-old-password"})
    health.record(conn, keys["needs a password"], kind="password")
    keys["needs a sign-in"] = fake_google.add(conn, address=persona.WORK_EMAIL, name="Work")
    health.record(conn, keys["needs a sign-in"], kind="sign_in")
    keys["can't reach"] = accounts.add(
        conn, module_id="imap", preset="icloud", address="maya@family.example",
        name="Family", values={"password": "a-family-password"})
    health.record(conn, keys["can't reach"], kind="transient", detail="no answer")
    keys["can't work here"] = accounts.add(conn, module_id="outlook_classic")
    health.record(conn, keys["can't work here"], kind="not_connected",
                  detail="Outlook is not installed on this computer")
    keys["off"] = accounts.add(conn, module_id="ics", name="Course site", values={
        "link": "https://courses.example/maya/feed.ics", "due_dates": True},
        resolve=lambda host, port: [mailrig.PUBLIC_IP])
    accounts.edit(conn, keys["off"], enabled=False)
    for n in range(2):
        conn.execute("INSERT INTO actions (tool, args_json, args_hash, preview, reach, "
                     "reversal, account, status) VALUES ('send_draft', '{}', ?, "
                     "'Send the draft to Jane', 'r', 'v', ?, 'pending')",
                     (f"preview-{n}", keys["working"]))
    rules.choose(conn, keys["working"])
    rules.opt_in(conn, persona.GMAIL)
    conn.execute("INSERT INTO mailbox (message_id, sender, subject, kind, why) VALUES "
                 "('preview-1', ?, 'Plan the trip', 'instruction', 'her own Sent copy'), "
                 "('preview-2', 'someone@elsewhere.example', 'Hello', 'ignored', "
                 "'not from an address of hers')", (persona.GMAIL,))
    conn.commit()
    return keys


def _named(folder: Path, module_id: str, label: str, for_: str) -> Path:
    """`_write`'s module under a name of its own: it writes every one as
    Webby, whose tests import `webby`, which the import check refuses in a
    module of another id."""
    manifest = folder / "manifest.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8")
                        .replace('label = "Webby"', f'label = "{label}"')
                        .replace('for = "Looking things up on Webby"', f'for = "{for_}"'),
                        encoding="utf-8", newline="\n")
    test = folder / "tests" / "test_tool.py"
    if test.exists():
        text = test.read_text(encoding="utf-8")
        test.write_text(text.replace("from webby ", f"from {module_id} "), encoding="utf-8",
                        newline="\n")
    return folder


def _tides(tmp: Path, version: str, hosts: tuple[str, ...] = ("api.tides.example",)) -> Path:
    return _named(_write(tmp / f"tides-{version}", id="tides", version=version, hosts=hosts,
                         reads=()), "tides", "Tides", "The harbour's tide times")


# A build in each state the page shows: under way, waiting past the day's
# cap, ended as a card, and ended as a report. Written as rows, since a
# real build runs the coding pipeline, which would reach a model.
BUILDS = (
    ("Read the ferry timetable and say when the last boat leaves", "running", 0.80, 720, 0.31,
     240, ""),
    ("Track the price of a train ticket to the coast", "needs_allow", 1.50, 900, 0.0, 0, ""),
    ("Answer the door when someone rings", "cannot", 0.80, 600, 0.12, 95,
     "Steve has no slot for a doorbell"),
)


async def _modules_world(conn, tmp: Path) -> None:
    """Something in each state on the Extensions page; admission's checks are
    the fakes `modadmit.admit` was given."""
    assert passphrase.choose(PHRASE) is None
    # Imported, on, on its second kept version, its watch counting; a third
    # version, reaching one more host, waits as a card.
    for version in ("1", "2"):
        admission = await modadmit.admit(conn, _tides(tmp, version), origin="imported",
                                         again=version != "1")
        lifecycle.approve(conn, admission)
        if version == "1":
            modules.set_enabled(conn, "tides", True)
        (paths.module_state_dir("tides") / "seen.txt").write_text(
            f"the tides as version {version} saw them", encoding="utf-8", newline="\n")
    conn.execute("UPDATE module_watch SET calls = 14, errors = 1, timeouts = 1 "
                 "WHERE module = 'tides'")
    await modadmit.admit(conn, _tides(tmp, "3", ("api.tides.example", "upload.tides.example")),
                         origin="imported", again=True)

    # Built by Steve: a request whose words name one of its two hosts and
    # none of what it reads, its own words for Steve's instructions, and
    # what it looks at and runs unwatched.
    built = _write(tmp / "webby", hosts=("api.webby.example", "collect.example"),
                   reads=("mail",))
    _append(built, SAILING + HARBOUR.replace('code = "tool.py"\n', ""))
    carded = await modadmit.admit(
        conn, built, origin="built", came_from="a conversation",
        request="Plan my sails from the tide times on api.webby.example",
        tools=lambda: {"web_fetch"})

    # Imported, new: its card shows its hash first.
    await modadmit.admit(conn, _named(_write(tmp / "almanac", id="almanac",
                                             hosts=("api.almanac.example",), reads=()),
                                      "almanac", "Almanac", "Sunrise, sunset and the moon"),
                         origin="imported")

    # An MCP server the user added, which takes a secret: its tools are
    # the fake server's, listed through its fake network.
    await mcptools.add(conn, name="notes", url=URL, secret=SECRET, label="Notes")

    for request, state, usd, seconds, cost, took, said in BUILDS:
        build = conn.execute(
            "INSERT INTO module_builds (request, origin, came_from, state, estimate_usd, "
            "estimate_s, cost_usd, seconds, said) VALUES (?, 'user', 'a conversation', ?, ?, "
            "?, ?, ?, ?)", (request, state, usd, seconds, cost, took, said)).lastrowid
        if state == "cannot":
            conn.execute("INSERT INTO module_reports (build_id, kind, text) VALUES (?, 'slot', "
                         "?)", (build, "A doorbell slot: a module that is told when someone "
                                       "rings, and may answer through a speaker by the door."))
            conn.execute("INSERT INTO module_reports (build_id, kind, text) VALUES (?, 'core', "
                         "?)", (build, "The calendar tool gave the wrong day for an event at "
                                       "midnight on [a date]."))
    conn.execute("INSERT INTO module_builds (request, origin, came_from, state, estimate_usd, "
                 "estimate_s, cost_usd, seconds, admission, finished_at) VALUES (?, 'user', "
                 "'a conversation', 'carded', 0.8, 720, 0.74, 610, ?, datetime('now'))",
                 ("Plan my sails from the tide times on api.webby.example", carded))
    conn.commit()


class _Token(BaseHTTPMiddleware):
    """Every route needs the token, as the supervisor asks it: here the
    launcher's, which Vite's proxy adds so the page never holds it."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return JSONResponse({"ok": False, "error": "no token"}, status_code=401)
        return await call_next(request)


async def _unknown(request) -> JSONResponse:
    # The app asks for its other pages too; they are not served here.
    return JSONResponse({"ok": False, "error": f"{request.url.path} is not in the preview"},
                        status_code=404)


async def test_serve_the_account_and_module_screens_on_the_fakes(monkeypatch, tmp_path):
    """Serve until the launcher stops it, or for `MINUTES`."""
    rig = mailrig.rig(fake_imap.GMAIL, user=persona.GMAIL, password=PASSWORD)
    monkeypatch.setattr(connectors, "NETWORK", rig.network)
    # Admission's model calls and sandbox run, as the unit tests fake them;
    # an MCP server reached through the fake network; and a removed
    # module's sandbox profile, which a unit test may not delete.
    monkeypatch.setattr(modadmit, "admit", functools.partial(
        modadmit.admit, screener=Screen(), reviewer=Reviewer(), tester=Trial()))
    monkeypatch.setattr(mcptools, "NETWORK", McpNetwork(McpServer(
        GraphWorld(MAYA), secret=SECRET, host=PUBLIC, port=443)))
    monkeypatch.setattr(profiles, "delete", lambda name: True)
    conn = db.open_store(paths.db_path())
    monkeypatch.setattr(runtime, "conn", conn)
    try:
        keys = _world(conn)
        await accounts.test(conn, keys["working"])
        await _modules_world(conn, tmp_path)
        token = secrets.token_urlsafe(24)
        app = Starlette(routes=account_routes.ROUTES)
        app.add_middleware(_Token, token=token)
        app.add_route("/{rest:path}", _unknown, methods=["GET", "POST"])
        listening = socket.socket()
        listening.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        Path(os.environ[ANNOUNCE]).write_text(json.dumps(
            {"port": listening.getsockname()[1], "token": token}), encoding="utf-8")
        until = time.monotonic() + 60 * float(os.environ.get(MINUTES, "30"))

        async def stop() -> None:
            while time.monotonic() < until:
                await asyncio.sleep(1)
            server.should_exit = True

        stopping = asyncio.create_task(stop())
        await server.serve(sockets=[listening])
        stopping.cancel()
    finally:
        conn.close()
        rig.close()
