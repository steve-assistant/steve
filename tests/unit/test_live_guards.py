"""The guards in conftest.py actually guard.

Each one exists because a unit test once reached something live -- the user's
Gmail, their Exchange cache, a live server -- and the suite went green
anyway. A guard that patches the wrong name is worse than none, because
it looks like protection: the Google one did nothing from the day it was
written. So each is checked from the side a real caller comes in.
"""

from __future__ import annotations

import re

import pytest

# At collection, before any fence is up, as the rest of the suite and the
# worker import it. Imported inside the test instead, a name bound at load
# time would already be the fenced one and the test could not tell.
from modules.outlook_classic import calendar as outlook_calendar


@pytest.mark.parametrize(
    "module",
    ["modules.google_cloud.calendar", "modules.google_cloud.gmail", "modules.google_cloud.drive",
     "modules.google_cloud.tasks"],
)
def test_no_google_module_can_reach_the_real_account(module):
    """The conftest guard used to patch a name no Google module read, so it
    guarded nothing. Asked through each module's own binding, as the tools
    ask, as an account signed in."""
    import importlib

    from modules.google_cloud import common as google_common
    from tests.fakes import google as fake_google

    bound = importlib.import_module(module).service
    with google_common.as_account(fake_google.account()), pytest.raises(
            RuntimeError, match="live Google"):
        bound("calendar", "v3")


def test_the_outlook_calendar_cannot_reach_the_real_outlook():
    """The calendar reader lives in its own module and borrows the mail
    reader's way in. Until step 5's item 10 that was the mail reader's own
    `_namespace`: borrowed by name at import, it would have kept the real one
    past the conftest patch and opened the user's Exchange cache. Now every
    way in is an account's context, which opens Outlook with core's
    `_dispatch`, and that meets the fence."""
    import datetime as dt

    from core import modctx
    from modules.outlook_classic import outlook
    from tests.fakes import outlook as fake_outlook

    now = dt.datetime.now(dt.UTC)
    real = fake_outlook.context(None, dispatch=lambda progid: modctx._dispatch(progid))
    with outlook.as_account(real):
        found = outlook_calendar.calendar(now, now + dt.timedelta(days=1))

    assert "live COM" in (found.get("error") or ""), found


async def test_no_test_can_make_a_real_model_call():
    """A briefing test made a real GPT call on 25 September, through a path
    nothing had stubbed. Asked the way every caller asks: a provider-built
    model, run."""
    from pydantic_ai import Agent

    from agent.models import Role
    from agent.provider import model_for

    for role in (Role.TRIAGE, Role.REASON, Role.ESCALATE):
        with pytest.raises(RuntimeError, match="live model"):
            await Agent(model_for(role)).run("hello")


async def test_no_test_can_reach_the_web():
    """The same test went searching for real articles."""
    from tools import web

    with pytest.raises(RuntimeError, match="live the web"):
        await web.search_and_read("anything")
    with pytest.raises(RuntimeError, match="live the web"):
        await web.fetch("https://example.com")


async def test_no_test_can_make_a_paid_web_search():
    """Below `tools.web.search`, which a test may replace: the request
    itself, OpenAI's or Brave's, which the user pays a fee for on their
    own key."""
    from core import websearch

    with pytest.raises(RuntimeError, match="live OpenAI's web search"):
        await websearch._call(websearch.request_for("anything", 5))
    with pytest.raises(RuntimeError, match="live Brave's search"):
        await websearch._brave_get({"q": "anything", "count": 5}, "a key")


def test_no_test_can_embed_on_openai():
    """Below `memory.embed`, which most tests replace: the request itself.
    Every note kept and every recall embeds, so a test that reached it
    would send what it remembers to OpenAI, and wait on the network."""
    from core import embed

    with pytest.raises(RuntimeError, match="live OpenAI's embeddings"):
        embed._call(embed.request_for(["anything"]), "a key")


def test_no_test_can_sign_in_to_google_or_refresh_a_sign_in():
    """A sign-in opens the user's browser at Google, and every refresh of an
    access token goes to Google's token endpoint. Asked the way the code
    asks: `auth.sign_in` building its flow, and `_client` looked up at call
    time."""
    from google.oauth2 import _client

    from core import credstore
    from modules.google_cloud import auth as google
    from tests.fakes import google as fake_google

    assert isinstance(credstore.store(), credstore.MemoryStore), (
        "a test can see the real Credential Manager, where the token is")
    with pytest.raises(RuntimeError, match="live Google sign-in"):
        google.sign_in(fake_google.Context(), classes=["mail"], open_browser=False)
    with pytest.raises(RuntimeError, match="live Google sign-in"):
        _client.refresh_grant(None, "https://oauth2.googleapis.com/token", "r", "c", "s")


def test_no_test_can_reach_a_password_accounts_mailbox(monkeypatch):
    """A password account (core/engines/imap.py), added as the app adds one
    and asked the way the tools ask, through the slot, with its server's
    name resolving as it would at home. Its one way out is a socket, refused
    before it connects, and its password is in the test's own store."""
    from agent import accounts, connectors
    from core import credstore, db, egress, modules
    from tests.fakes.mailrig import PUBLIC_IP

    assert isinstance(credstore.store(), credstore.MemoryStore), (
        "a test can see the real Credential Manager, where the password is")
    monkeypatch.setattr(egress, "_resolve", lambda host, port: [PUBLIC_IP])
    conn = db.open_store()
    try:
        modules.register(conn)
        key = accounts.add(conn, module_id="imap", address="maya@mail.example", preset="other",
                           values={"password": "pw", "imap": "imap.mail.example:993",
                                   "smtp": "smtp.mail.example:465"},
                           resolve=lambda host, port: [PUBLIC_IP])
        (found,) = [c for c in connectors.accounts(conn, "mail") if c.key == key]
        with pytest.raises(RuntimeError, match="off this computer"):
            connectors.blocking(found, lambda mail: mail.search("plot", limit=1))
    finally:
        conn.close()


def test_no_test_reads_their_real_keychain():
    """Tests building a model client read their real API keys to do it, until
    the suite ran as an account with an empty keychain. Asked the way the
    code asks: `core.secrets.get`, which reads the keychain first."""
    from core import secrets

    # Booleans, not the values: a failing comparison prints both sides, and
    # the side that fails is their real key (seen 8 October 2026, when the
    # gate's twin of this was run against the unfenced conftest).
    dummy = secrets.get("OPENROUTER_API_KEY") == "test-openrouter_api_key"
    assert dummy, "a unit test read a real API key from her keychain"
    read = [k for k in ("TELEGRAM_BOT_TOKEN",) if secrets.get(k)]
    assert read == [], "a unit test read these from her keychain"


def test_no_test_can_make_an_appcontainer():
    """A profile is written to the user's registry and AppData, outside the
    test's own folder, and what runs in it is real code. Only the walls'
    own gate tests make them. Asked the way the sandbox asks: finding a
    module's package SID, which writes nothing, and then making its profile,
    which a fence that failed the first would never reach."""
    from core.sandbox import profiles

    with pytest.raises(RuntimeError, match=re.escape("live an AppContainer.")):
        profiles.derive(profiles.for_module("gmail"))
    with pytest.raises(RuntimeError, match=re.escape("live an AppContainer.")):
        profiles.ensure(profiles.for_module("gmail"))


def test_a_database_opened_without_a_path_is_not_the_live_one():
    """`open_store()`'s default used to be bound when the function was
    defined, so nothing could redirect it and a forgetful test opened the
    real database."""
    from agent import offloop
    from core import db, paths

    live = (paths.SOURCE / "data" / "agent.db").resolve()
    conn = db.open_store()
    try:
        assert offloop.db_path(conn).resolve() != live
    finally:
        conn.close()


def test_no_test_can_ask_the_live_supervisor_to_swap_the_agent():
    """A model switch from the command line asks the supervisor for a swap
    (`control_plane.post_supervisor`). Under the fences that fails closed
    rather than reaching the live supervisor."""
    from agent import control_plane

    with pytest.raises(RuntimeError, match=re.escape("live the supervisor.")):
        control_plane.post_supervisor("/_sup/swap", {})


def test_no_test_can_switch_tracing_on_for_every_agent(monkeypatch):
    """Asked the way the worker asks: `tracing.setup` with an endpoint. It
    must come back off, not instrument the whole test process."""
    from pydantic_ai import Agent

    from agent import tracing

    monkeypatch.setattr(tracing, "_state", {"enabled": False, "reason": "not configured"})
    state = tracing.setup("http://127.0.0.1:6006/v1/traces")

    assert state["enabled"] is False and "live tracing" in state["reason"]
    assert Agent._instrument_default is False


def test_no_test_can_read_a_calendar_links_feed(monkeypatch):
    """A calendar link (core/engines/ics.py), added as the app adds one and
    read the way the tools read, through the slot, with its host resolving
    as it would at home. A course site's link is its password; the one way
    out is the module's HTTP client, whose socket is refused off this
    computer, and the link is in the test's own store."""
    import datetime as dt

    from agent import accounts, connectors
    from core import credstore, db, egress, modules
    from tests.fakes.ics import LINK, PUBLIC_IP

    assert isinstance(credstore.store(), credstore.MemoryStore), (
        "a test can see the real Credential Manager, where the link is")
    monkeypatch.setattr(egress, "_resolve", lambda host, port: [PUBLIC_IP])
    conn = db.open_store()
    try:
        modules.register(conn)
        key = accounts.add(conn, module_id="ics", values={"link": LINK, "due_dates": True},
                           name="Course site", resolve=lambda host, port: [PUBLIC_IP])
        (found,) = [c for c in connectors.accounts(conn, "calendar") if c.key == key]
        now = dt.datetime.now(dt.UTC)
        tomorrow = now + dt.timedelta(days=1)
        with pytest.raises(RuntimeError, match="off this computer"):
            connectors.blocking(found, lambda cal: cal.events(now, tomorrow, limit=5))
    finally:
        conn.close()


def test_no_test_can_read_the_real_openrouter_balance():
    """The health check's one authenticated read goes through the feeds'
    door, and is refused there."""
    from agent.attention.sources import health

    with pytest.raises(RuntimeError, match=re.escape("live weather and news feeds")):
        health._openrouter(None)


def test_no_test_can_ask_the_real_jev_anything(tmp_path):
    """From the caller's side, with the fenced keychain's dummy key: no
    answer, naming the fence, never a call out."""
    from core import db, decide

    conn = db.open_store(tmp_path / "decide.db")
    said = decide.ask(conn, "hello", [decide.Predicate("q", "Is it?")])
    conn.close()

    assert said.answers == {} and "tried to reach live Jev (core/decide.py)" in said.error


def test_no_test_can_ask_the_real_jev_to_screen_anything(tmp_path):
    """Through the screen's own call, from the caller's side: the verdict
    falls back to the patterns, and says why."""
    from core import db, screen

    conn = db.open_store(tmp_path / "screen.db")
    said = screen.check(conn, screen.Artifact("email_body", "m", "x", "hello"), key="k")
    conn.close()

    assert "tried to reach live Jev (core/decide.py)" in said.error and said.verdict is None


def test_no_test_can_load_the_local_injection_detector():
    """The screen's detector is a model off their disk, there on the laptop and
    nowhere else. From the caller's side: with no key, the screen says why
    the detector did not decide."""
    from core import screen

    said = screen.check(None, screen.Artifact("email_body", "m", "x", "hello"), key="")

    assert "the local injection detector" in said.error and said.verdict is None


def test_no_test_can_download_the_local_injection_detector(monkeypatch, tmp_path):
    from core import detector

    monkeypatch.setenv("STEVE_MODEL_CACHE", str(tmp_path))

    with pytest.raises(RuntimeError, match=re.escape("live the detector's download")):
        detector.setup()


def test_no_test_can_start_the_local_decision_model(monkeypatch):
    """Installed on the laptop, as it is after setup, the decider would start
    an Ollama server holding 5 GB of the GPU. From the caller's side: the
    screen, with no key and PIGuard refused, says why it did not decide."""
    from core import decider, screen

    monkeypatch.setattr(decider, "installed", lambda: True)
    # And its runtime's files as pinned, which each start checks first.
    monkeypatch.setattr(decider.addons, "verify", lambda addon, folder=None: None)

    said = screen.check(None, screen.Artifact("email_body", "m", "x", "hello"), key="")

    assert "live the local decision model." in said.error and said.verdict is None


def test_no_test_can_call_the_local_decision_models_server():
    """Nor reach one that is already running: a server left by an earlier
    run, or the live worker's own."""
    from core import decider

    with pytest.raises(RuntimeError, match=re.escape("live the local decision model's server")):
        decider._call("GET", "/api/version")


def test_no_test_can_download_the_local_decision_model(monkeypatch, tmp_path):
    from core import decider

    monkeypatch.setenv("STEVE_MODEL_CACHE", str(tmp_path))

    # The runtime is an add-on: the add-ons' download is the one refused.
    with pytest.raises(RuntimeError, match=re.escape("live the add-on downloads")):
        decider.setup()


@pytest.mark.parametrize("docker_installed", [True, False])
async def test_no_test_can_start_a_real_browser_sandbox(monkeypatch, docker_installed):
    """From the caller's side, every way in: the sandbox's own functions, the
    session registry that starts one, and the driver it would talk to -- on a
    machine with Docker, like the laptop, and one without, like the coding
    sandbox, where this test once failed for every night's attempt."""
    from agent.browser import session as sessions
    from core import images
    from core.browser import sandbox

    if not docker_installed:
        monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)

    # As if built here: a start then goes on to ask Docker for the images.
    for kind in sandbox.KINDS:
        images.remember(sandbox.image(kind), "sha256:" + "0" * 64)
    with pytest.raises(RuntimeError, match="live Docker"):
        sandbox.start("1:1")
    with pytest.raises(RuntimeError, match="live Docker"):
        sandbox.reap()
    with pytest.raises(RuntimeError, match="live Docker"):
        sandbox.build()
    box = sandbox.Sandbox("s", "1:1", *sandbox.names("s"), proxy="http://x:3128")
    with pytest.raises(RuntimeError, match="the browser's driver"):
        await sessions.SPAWN(box)
    sessions._ready[:] = []
    with pytest.raises(RuntimeError, match="live Docker"):
        await sessions.session_for(1)


def test_no_test_can_read_whether_this_laptop_has_docker_desktop():
    """Setup's Docker page reads the machine it runs on. A test that read it
    would pass on the laptop, which has Docker Desktop, and fail where there
    is none."""
    from agent.worker import setup

    with pytest.raises(RuntimeError, match="live Docker Desktop's state"):
        setup.docker()


def test_no_test_can_reach_their_saved_logins():
    """Their browser logins are in their keychain and data/browser, encrypted to
    their Windows account. The vault every caller gets is a throwaway one."""
    from core import paths
    from core.browser import vault

    store = vault.vault()
    assert not store.root.is_relative_to(paths.SOURCE / "data")
    assert not isinstance(store.keyring, vault._Keyring)
    assert store.protect is not vault._dpapi_protect


async def test_no_test_can_rest_or_count_the_live_agents_linkedin(monkeypatch):
    """The LinkedIn pacing -- the hour's page loads and the pause after a
    refusal -- is kept in data/linkedin_pacer.json so a worker swap does not
    forget it (8 October 2026). Asked the way a tool asks: a halt and a load
    on the module's own `PACER`, then where it wrote."""
    from agent.toolset import linkedin
    from core import paths

    live = (paths.SOURCE / "data").resolve()
    there_before = (live / "linkedin_pacer.json").exists()
    assert not linkedin.pacer_file().resolve().is_relative_to(live)

    pacer = linkedin.Pacer()
    pacer.halt("it was refused", "linkedin")
    await pacer.take()

    assert linkedin.pacer_file().exists(), "it was written, to the test's own folder"
    assert (live / "linkedin_pacer.json").exists() is there_before


def test_no_test_can_read_openais_model_list(monkeypatch):
    """The model scout reads OpenAI's model list with their key. Asked the
    way the scout asks: `scan` with no fetch of its own, and a key, since
    without one it stops before the door."""
    from agent import models_scout

    monkeypatch.setattr(models_scout.secrets, "get", lambda name, default=None: "sk-test")
    with pytest.raises(RuntimeError, match=re.escape("live OpenAI's model list.")):
        models_scout.scan(None)


def test_no_test_runs_on_the_scouts_live_switches():
    """The worker's startup puts data/model_switches.json over the lineup,
    and tests run that startup. Asked the way the worker asks:
    `load_active()` with no path."""
    from agent import models
    from core import paths

    live = paths.SOURCE / "data"
    assert not models.active_path().resolve().is_relative_to(live.resolve())
    models.active_path().parent.mkdir(parents=True, exist_ok=True)
    models.active_path().write_text('{"roles": {"converse": {"candidate": {}}}}',
                                  encoding="utf-8")
    assert set(models.load_active()) == {"converse"}


def test_no_test_runs_on_or_turns_their_knobs():
    """The worker's startup puts data/knobs.json over the code's limits, and
    tests run that startup. Asked the way the worker asks: `apply_knobs()`
    and `knobs.write` with no path."""
    from agent.worker import control
    from core import knobs, paths

    live = paths.SOURCE / "data"
    assert not knobs.knobs_path().resolve().is_relative_to(live.resolve())
    knobs.write({"MAX_DAY_PINGS": 5})
    assert knobs.knobs_path().exists() and control.current_knobs()["MAX_DAY_PINGS"] == 5


def test_no_test_can_write_or_tidy_their_real_memories():
    """The night's tidy commits to the memories repo and forgets what is
    past its date; a test running `night.learn` reached it through
    `tools.memory.MEMORIES_DIR`, which pointed at the checkout's own."""
    from core import db, paths
    from tools import memory

    live = (paths.SOURCE / "data" / "memories").resolve()
    assert paths.memories_dir().resolve() != live
    conn = db.open_store()
    try:
        with pytest.raises(RuntimeError, match="git"):
            memory._commit("a test note", "user/x.md")
        assert memory.reconcile(conn) == [] and memory.expire(conn) == []
    finally:
        conn.close()


# Doors tests/gate/fences.py shuts by name that the code no longer has, each
# with where it is now. That file is frozen, so a change that renames or moves
# one says so here -- and shuts the new door in tests/unit/conftest.py -- and
# a developer moves it in fences.py. Empty means every door is where it was.
MOVED: dict[str, str] = {}


def test_every_door_the_shared_fences_shut_is_still_there(monkeypatch, tmp_path):
    """The shared fences name about twenty doors in code the night's coder may
    change, and are frozen, so a renamed `agent.attention.fetch._get` could
    not be mended by the change that renamed it. They skip a door that is gone
    rather than error every test in both trees at setup -- which makes a moved
    door silent, unless something says so. This does."""
    from tests.gate import fences

    gone = fences.live_services(monkeypatch, tmp_path / "scratch", "a unit test", "here")

    unlisted = sorted(set(gone) - set(MOVED))
    back = sorted(set(MOVED) - set(gone))
    assert not unlisted, (
        f"tests/gate/fences.py shuts doors the code no longer has: {unlisted}. Shut the new "
        "door in tests/unit/conftest.py, list the old name in MOVED here with where it went, "
        "and say so in the change -- only a developer edits tests/gate/fences.py")
    assert not back, f"MOVED lists doors that are there again: {back}"
