"""The doors to live things, shut the same way for unit and gate tests.

Frozen, like the rest of tests/gate, and called by both conftests.

Until 8 October 2026 the two trees had separate fences, and the gate's had
grown only where a gate test had already got through: the coder, the
supervisor, the critic. The keychain fence lived in tests/unit/conftest.py
alone. So on the laptop a gate test that built a model client read the user's
real OpenRouter key, and as the sandbox account, whose keychain is empty by design,
the same test failed with MissingSecret: shadow attempts #82 to #87 and their
own request #88 were all rejected on
tests/gate/test_desk.py::test_a_lecture_is_summarised_by_a_model_with_no_tools,
whatever they had changed. That was the third time the laptop and the
sandbox had disagreed about a test's environment (142350e, Docker; 654e6cb,
25 tests on MissingSecret), and each time a fence existed in one tree and
not the other.

One function, called from both, means a door shut for one tree is shut for
the other. What only one tree needs stays in its own conftest: the unit
tree's throwaway browser vault and folders (a gate test inspects the real
ones).

A test that needs one of these brings a fake of its own; monkeypatch in the
test runs after this, so its fake wins.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import shutil
import sys
import sysconfig
import venv
from pathlib import Path

# Steve's keychain service and prefix, pinned here rather than imported from
# core/identity.py: a fence that read them from there would move with them,
# and so would let tests through to the personal edition if they were ever
# changed back. Steve has no fixed port: its supervisor's is chosen at each
# start (core/localapi.py).
KEYCHAIN = "steve"
ENV_PREFIX = "STEVE_"

# No test connects off this computer, and none to the port the personal
# edition this product was copied from listens on, as the same Windows user,
# on the machine the product is developed on. No file in the product names
# that port (tests/gate/test_identity.py). Until step 5 only that port was
# refused, and step 3's mutation run reached OpenAI with a dummy key. An
# audit hook, at import: it covers every socket in the process, can't be
# undone by a test's monkeypatch, and is in place before any test runs.
# asyncio's connections on Windows go through `ConnectEx`, which raises no
# audit event, so they are refused by a wrapper beside the hook. pywin32's
# Credential Manager calls, which core/credstore.py makes for module
# secrets, answer from memory. All of it lives in the folder every process
# a test starts imports first (childfence/), so a test and its children are
# fenced alike.
from tests.gate.childfence import sitecustomize as _child  # noqa: E402

PERSONAL_PORT = _child.PERSONAL_PORT
CHILD_FENCE = Path(_child.__file__).resolve().parent

_child.shut_the_network()
_child.shut_the_credential_manager()

# Set here, at import, because it only counts if it is set before numpy is
# first imported -- and this module is the first thing both conftests import,
# in every pytest process and (through the environment) in every process a
# test starts. OpenBLAS reserves a work buffer for each of the machine's
# threads the moment numpy loads: about 480 MiB of committed memory on this
# laptop's sixteen, for a process that uses 125 MiB, and the same for each
# test worker, supervisor-started worker and gate test's child. The coding
# sandbox's Job Object allows its whole tree 6 GiB of committed memory, and
# on 8 October 2026 a gate run reached it (tests/gate under -n 3: 6.0 GiB at
# the peak, three supervisors with up to three 583 MiB workers each). The
# next process to start then failed in its first allocation. A worker's log
# said "OpenBLAS error: Memory allocation still failed after 10 retries",
# and the swap reported only "new worker never became healthy" -- the
# rejection of upgrade #89, approved by the judge, on
# test_no_requests_dropped_during_swap. One thread is all an inert worker or
# a test needs; a worker starts as fast (3.7 s either way) on 100 MiB.
# `setdefault`, so a session that wants more can say so.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")


def refusal(who: str, what: str, where: str):
    """A stand-in that raises, naming who reached for what. The wording --
    "tried to reach live <what>." -- is what tests/unit/test_live_guards.py
    matches, so a fence that patched the wrong name is caught there."""

    def fail(*args, **kwargs):
        raise RuntimeError(
            f"{who} tried to reach live {what}. Inject a fake instead -- see {where}")

    return fail


def unless_redirected(address, own: str, real, refused):
    """`real` while `address()` names a fake on this computer, `refused`
    otherwise. `core/endpoints.py` gives a service's own address unless its
    `STEVE_*_URL` names this computer, in a checkout; so a test that serves
    a fake over HTTP (tests/fakes/) reaches it as a worker would, and one
    that does not is still refused the real service."""

    def door(*args, **kwargs):
        return (real if address() != own else refused)(*args, **kwargs)

    return door


def fenced_python(folder: Path) -> Path:
    """A Python whose own site-packages holds the child fence, made once in
    `folder` (pytest's folder for the run, which outlives each test's).

    Python's own venv builder, about 15 ms. Its site-packages imports the
    fence as `sitecustomize`, then adds this interpreter's site-packages, so
    it imports what this one imports; and in a process it starts,
    `sys.executable` is itself again, so the fence passes down any number of
    generations. The `.pth` is written last: a folder without it is a build
    that stopped half-way, and is made again rather than trusted unfenced.
    """
    scripts, site = (("Scripts", Path("Lib", "site-packages")) if os.name == "nt"
                     else ("bin", Path("lib", f"python{sys.version_info[0]}.{sys.version_info[1]}",
                                       "site-packages")))
    python = folder / scripts / ("python.exe" if os.name == "nt" else "python")
    done = folder / site / "steve-fenced.pth"
    if not done.exists():
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt", clear=True).create(folder)
        shutil.copyfile(CHILD_FENCE / "sitecustomize.py", folder / site / "sitecustomize.py")
        done.write_text(f"import site; site.addsitedir({sysconfig.get_paths()['purelib']!r})\n",
                        encoding="utf-8", newline="\n")
    return python


def live_services(monkeypatch, scratch: Path, who: str, where: str, *,
                  containers: bool = False, docker: bool = False) -> list[str]:
    """Shut every door from a test to a live service or their real accounts.

    `scratch` is a folder of the test's own (inside its tmp_path, which
    pytest rotates away -- never `mkdtemp`, see test_repo_hygiene). `who`
    and `where` go into the refusal ("a gate test", the file to read).
    `containers` opens AppContainer profiles, for the modules' sandbox's
    walls (tests/gate/test_module_sandbox.py). `docker` lets this process
    start Docker, for the code tool's own tests (tests/unit/test_code_exec.py).

    Returns the doors it could not find. A door in code the night's coder may
    change (`tools/`, `agent/`, `channels/`) is shut by its dotted name and *not
    required to be there*: this file is frozen, and a change that renamed
    `agent.attention.fetch._get` would otherwise error every test in both trees
    at setup, with the one file that names it out of the change's reach --
    the failure #87 met in tests/gate/test_cards.py, multiplied by the suite.
    tests/unit/test_live_guards.py asks each door from the caller's side, and
    tests/unit/test_live_guards.py::test_every_door_the_shared_fences_shut_is_still_there
    lists the ones that moved, which a change can say and a developer moves here.
    Doors in frozen code (`core/`) are shut
    strictly: only a human moves those, and then should move this too. The
    providers' doors are the exception while step 5 moves them into
    `modules/`: Google's sign-in is shut by `door()`.
    """
    refuse = lambda what: refusal(who, what, where)  # noqa: E731 - one line per door below
    gone: list[str] = []

    def door(target: str, value) -> None:
        # Strict, then caught: `raising=False` would not skip a name that is
        # missing, it would create it.
        try:
            monkeypatch.setattr(target, value)
        except (ImportError, AttributeError):
            gone.append(target)

    def door_on(module, name: str, value) -> None:
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
        else:
            gone.append(f"{module.__name__}.{name}")

    def uncache(module, *names: str) -> None:
        for name in names:
            getattr(getattr(module, name, None), "cache_clear", lambda: None)()
    # Everything the agent keeps (core/paths.py): the database, the sign-in
    # token, the memories, the logs, the run file. A folder of the test's
    # own, not yet made, as on a fresh install: nothing real to read, even
    # to hash, nowhere real to write, and a writer that forgets to make its
    # folder fails here. One setting: these were forty
    # constants, each patched where someone remembered to, and the one
    # forgotten wrote to the real folder. Processes a test starts inherit
    # it. The downloaded models are large and only ever read, so they are
    # still read from the checkout's.
    from core import paths

    monkeypatch.setenv(paths.MODEL_CACHE_ENV, str(paths.models_dir()))
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(scratch / "data"))
    monkeypatch.delenv(paths.MEMORIES_DIR_ENV, raising=False)
    # No token accepted until the test says which (core/localapi.accept), so
    # one test's never lets another's requests through.
    monkeypatch.setattr("core.localapi._accepted", None)
    # Logging as each test found it: a test that runs the worker's setup
    # turns the scrub on for the whole process (core/logscrub.py), and the
    # secrets one test read must not blank another's lines.
    import logging

    from core import logscrub

    monkeypatch.setattr(logging, "_logRecordFactory", logging.getLogRecordFactory())
    monkeypatch.setattr(logscrub, "_held", set())
    # The laptop's language and region (core/language) as a US-English
    # Windows has them, so a test's prompts and requests are the same on
    # any computer, as a pinned clock makes its day the same.
    from core import language

    monkeypatch.setattr("core.language._ask_windows", lambda: language.FALLBACK)
    uncache(language, "windows")
    # And the laptop's time zone, the one place it is asked (core/clock.py).
    # CI's runner is on UTC: four tests that pinned the clock and not the
    # zone read 10:05 as 14:05 there (CI's first run, 22 October 2026). A
    # test that is about another zone pins its own, over this.
    from zoneinfo import ZoneInfo

    monkeypatch.setattr("core.clock.local_zone", lambda: ZoneInfo("America/New_York"))

    # Their keychain. Tests that build a model client -- never used, the
    # client fence below refuses its requests -- still read an API key to
    # build it. On the laptop that was their real key; as the sandbox account, nothing.
    # The fence is the keychain read itself, under `core.secrets.get`, so the
    # lookup's own logic still runs: API keys read as dummies, everything
    # else as unset.
    import core.secrets as secrets_module

    def keychain(service: str, key: str) -> str | None:
        if not service.startswith(KEYCHAIN):
            raise RuntimeError(
                f"{who} read keychain service {service!r}, which is not Steve's "
                f"({KEYCHAIN!r}). Inject a fake instead -- see {where}")
        return f"test-{key.lower()}" if key.endswith("_API_KEY") else None

    monkeypatch.setattr(secrets_module.keyring, "get_password", keychain)
    monkeypatch.setattr(secrets_module.keyring, "get_credential",
                        lambda service, username: keychain(service, username) and None)
    # Never a write or a delete: a test that stored or forgot a secret would
    # change the real keychain, Steve's or another's.
    monkeypatch.setattr(secrets_module.keyring, "set_password", refuse("the keychain's writes"))
    monkeypatch.setattr(secrets_module.keyring, "delete_password", refuse("the keychain's writes"))
    # Module secrets (core/credstore.py): an empty store of the test's own,
    # so one test's sign-in is never another's, and the real Credential
    # Manager refused if a test puts the real store back.
    from core import credstore

    monkeypatch.setattr(credstore, "_store", credstore.MemoryStore())
    monkeypatch.setattr(credstore, "WindowsStore", refuse("the Credential Manager"))
    # The environment the lookup falls back to: Steve's own names and the
    # vendors', each unset, so a machine's settings decide no test.
    for key in secrets_module.KNOWN_KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(ENV_PREFIX + key, raising=False)
    # The personal edition's variables, gone for the test and anything it
    # starts (core/identity.scrub_environment does this in the product).
    for name in [n for n in os.environ if n.upper().startswith("AGENT" + "_")]:
        monkeypatch.delenv(name)
    # Every Python a test starts imports childfence/sitecustomize.py first,
    # which shuts the port and the keychain in that process too.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [str(CHILD_FENCE), *filter(None, [os.environ.get("PYTHONPATH")])]))
    # PYTHONPATH alone missed every worker: a worker starts with `-E -s`
    # (core/worker_handle.py) and the app's backend with `-I`, and both
    # flags drop PYTHONPATH. So until step 9 a worker that a gate test's
    # supervisor started ran with no fence at all, the real `steve`
    # keychain and the network open to it. A venv's own site-packages is
    # read whatever those flags say, so the Python a test starts through
    # `sys.executable` is one whose site-packages holds the fence, and
    # whatever that starts through `sys.executable` is the same one.
    monkeypatch.setattr(sys, "executable", str(fenced_python(scratch.parents[1] / "fenced-python")))

    # Signal: the program and its account, pinned inside the test's data
    # folder, where neither is installed. They used to pass straight
    # through, so a shell that set STEVE_SIGNAL_CLI and STEVE_SIGNAL_DATA
    # handed a test the real linked device, and a message the test sent
    # would have reached their phone. A test of Signal points them at a fake.
    signal_home = scratch / "data" / "signal-fenced"
    monkeypatch.setenv("STEVE_SIGNAL_CLI", str(signal_home / "bin" / "signal-cli"))
    monkeypatch.setenv("STEVE_SIGNAL_DATA", str(signal_home / "account"))
    # The downloaded programs (core/addons.py), inside the test's folder
    # too: in a checkout they are its `.addons/`, and a test that installed
    # or removed one would change the developer's.
    monkeypatch.setenv("STEVE_ADDONS_DIR", str(scratch / "data" / "addons-fenced"))

    # Every model client, underneath whatever builds it. A test that wants a
    # model uses FunctionModel; one that reaches a real provider gets a
    # client refusing every request. The provider binds these factories by
    # name at import, so they are patched there, and its caches cleared so no
    # client built earlier slips past.
    import httpx2

    import agent.provider as provider

    def refusing_client(*args, **kwargs):
        def fail(request):
            raise RuntimeError(
                f"{who} tried to reach a live model ({request.url.host}). Use a "
                f"FunctionModel instead -- see {where}"
            )

        return httpx2.AsyncClient(transport=httpx2.MockTransport(fail))

    uncache(provider, "model_for", "_openai_provider")
    door_on(provider, "plain_http_client", refusing_client)
    # Tracing for every agent in the process: switched on for real once, and
    # every model test after it tried to reach a collector on 6006.
    monkeypatch.setattr("pydantic_ai.Agent.instrument_all", refuse("tracing for every agent"))

    # Docker. The sandboxed browser's calls all go through
    # `sandbox._docker`, its driver is a `docker exec` process, and the
    # session asks whether Docker is there at all. Fenced only at `_docker`,
    # a session test met the fence on the laptop and "not running" in the
    # coding sandbox, which has no Docker -- from 2 October every night's
    # attempt failed there, and five loosened a guard to get through it
    # (shadow attempts 64-71, fixed in 142350e).
    monkeypatch.setattr("core.browser.sandbox._docker", refuse("Docker"))
    # Below every door, the audit hook refuses Docker's own programs to any
    # process that starts one (childfence/), but for a test marked `docker`.
    if docker:
        monkeypatch.setattr(_child, "docker_shut", False)
    door("agent.browser.session.SPAWN", refuse("the browser's driver"))
    door("agent.browser.session.AVAILABLE", refuse("Docker"))
    door("agent.browser.session.READY", refuse("Docker"))
    # Setup's Docker page reads this laptop's Docker Desktop: a test that
    # gave no answer of its own would pass here and fail on a machine
    # without it.
    door("agent.worker.setup.docker", refuse("Docker Desktop's state"))

    # Google accounts. Below `service`, not on it: every Google module binds
    # `service` by name at import, so patching it reached none of them (that
    # guard did nothing from the day it was written). `service` looks up
    # `build` when it runs; that is the seam. Each by `door()`: a strict
    # door would error every test's setup when the code moves.
    with contextlib.suppress(ImportError):  # moved: its doors below say so
        uncache(importlib.import_module("modules.google_cloud.common"), "_service")
    door("modules.google_cloud.common.build", refuse("Google"))
    # Each account's credentials, held in memory by its sign-in: none carried
    # from one test to the next.
    door("modules.google_cloud.auth._HELD", {})
    # The browser sign-in, which would open the user's browser at Google, and
    # the endpoint every refresh goes through. On the library's class, not on
    # `auth`'s name for it: a test's fake flow replaces that name, and a
    # function in its place broke `.from_client_config` with an
    # AttributeError instead of refusing. Building the flow is refused, so
    # the refusal is raised as itself; the browser step too, for a flow
    # built some other way, where the sign-in reports it as its cause.
    door("google_auth_oauthlib.flow.InstalledAppFlow.from_client_config",
         refuse("Google sign-in"))
    door("google_auth_oauthlib.flow.InstalledAppFlow.run_local_server",
         refuse("Google sign-in"))
    door("google.oauth2._client.refresh_grant", refuse("Google sign-in"))

    # Every COM program a module opens, Outlook among them: core's one door
    # (`core.modctx.ModuleCom`), looked up at each open, so a context built
    # before the fence is fenced too. Until step 5's item 10 Outlook's module
    # opened COM itself, and two doors of its own stood here.
    door("core.modctx._dispatch", refuse("COM"))
    # AppContainer profiles (core/sandbox/): each is written to the user's
    # registry and AppData, outside any test's folder, and a container's
    # process runs real code. Open only to a gate test marked `appcontainer`,
    # the walls' own tests, which delete what they make.
    if not containers:
        monkeypatch.setattr("core.sandbox.win32.userenv", refuse("an AppContainer"))
    # The start's answer about support is the worker's, kept in its module:
    # one test's answer must not be the next one's.
    monkeypatch.setattr("core.sandbox.probe._verdict", None)
    # A password account's mailbox (core/engines/imap.py) has no door of its
    # own: it reaches its servers only by socket, refused off this computer
    # by the hook above, and its password only from the Credential Manager,
    # an empty store of the test's own.

    # Feeds that change with the day: a test depending on today's headlines
    # passes or fails with the news. (Reading a page, `tools.web.fetch`, is
    # fenced in tests/unit/conftest.py only: the gate's injection tests
    # drive the real `fetch` over a fake network, which is the point.
    # Search's own call is shut below.)
    door("agent.attention.fetch._get", refuse("weather and news feeds"))
    door("agent.models_scout._get", refuse("OpenAI's model list"))

    # Billed calls with their own clients, outside the provider's fence:
    # the screen's extractor, which builds its own OpenAI client.
    monkeypatch.setattr("core.screen._extract_call", refuse("the injection screen's extractor"))
    # Web search (core/websearch.py): a request with OpenAI's search tool,
    # billed per search besides its tokens, and Brave's, where a key is set
    # -- and every API key reads as set in a test.
    monkeypatch.setattr("core.websearch._call", refuse("OpenAI's web search"))
    monkeypatch.setattr("core.winsignin._ask", refuse("Windows' sign-in"))
    monkeypatch.setattr("core.websearch._brave_get", refuse("Brave's search"))
    # Memory's and facts' vectors (core/embed.py), billed per token: a test
    # that keeps a note or a fact without a fake embedder gets none, as an
    # install whose embedder cannot answer does.
    monkeypatch.setattr("core.embed._call", refuse("OpenAI's embeddings"))
    # Voice notes and push-to-talk (core/hearing.py), billed by the minute,
    # the desk's voice (core/voicing.py), billed by the minute it makes, and
    # the add-ons' downloads (core/addons.py), 180 MB from GitHub.
    monkeypatch.setattr("core.hearing._call", refuse("OpenAI's speech-to-text"))
    monkeypatch.setattr("core.voicing._call", refuse("OpenAI's text-to-speech"))
    monkeypatch.setattr("core.addons._download", refuse("the add-on downloads"))
    # The user's microphone and speakers. Push-to-talk's first press played
    # its tick through `sounddevice.play` (voice/audio.tick_alone), and a
    # route test that forgot to fake it played it aloud (16 October 2026).
    # A test fakes the device, as tests/unit/test_push_to_talk.py does.
    for name in ("play", "rec", "playrec", "OutputStream", "InputStream", "Stream"):
        monkeypatch.setattr(f"sounddevice.{name}",
                            refuse("the speakers" if name in ("play", "OutputStream")
                                   else "the microphone"))
    # Telegram's Bot API, under every client channels/telegram.py builds; a
    # test hands it a fake Bot API instead, in its process or served on this
    # computer (tests/fakes/telegram.py). Served, httpx's own transport (the
    # None `_transport` gives) carries the client to the address it names.
    from core import decide, endpoints

    monkeypatch.setattr("channels.telegram._transport", unless_redirected(
        endpoints.telegram, endpoints.TELEGRAM, lambda: None, refuse("Telegram's Bot API")))
    # Jev through core/decide.py, the one call every decision site makes --
    # the screen, the critic, the browser's clicks, splitting tasks and
    # meeting follow-ups -- with a breaker of each test's own: one test's
    # outage would otherwise hold the next test's asks. A fake Jev served on
    # this computer (tests/fakes/jev.py) is asked as a worker asks it.
    monkeypatch.setattr(decide, "_post", unless_redirected(
        endpoints.openrouter, endpoints.OPENROUTER, decide._post, refuse("Jev (core/decide.py)")))
    monkeypatch.setattr(decide, "_breaker", decide.Breaker())
    # The screen's local readers (core/detector.py, core/decider.py): billed by
    # no one, but models read off their disk -- installed on the laptop and
    # nowhere else, so a test that used one would pass for the user and fail in
    # the sandbox -- holding two cores, or 5 GB of the GPU, for seconds an
    # item; the decider's server, a process it starts; and the one download
    # both install through, 740 MB and 6 GB.
    monkeypatch.setattr("core.detector._load", refuse("the local injection detector"))
    monkeypatch.setattr("core.detector._download", refuse("the detector's download"))
    monkeypatch.setattr("core.decider._spawn", refuse("the local decision model"))
    monkeypatch.setattr("core.decider._call", refuse("the local decision model's server"))

    # The coding pipeline's own model clients (core/coder/models.py), and
    # the live supervisor, which a model switch from the command line asks
    # to swap the worker: a test that reached it would swap out the running
    # agent.
    monkeypatch.setattr("core.coder.models._openai", refuse("the coder's OpenAI client"))
    monkeypatch.setattr("agent.control_plane.post_supervisor", refuse("the supervisor"))

    # The yardstick's weekly run: a child process on the live models.
    door("agent.night.yardstick._run", refuse("the yardstick's run"))
    # The module builds it joins: another child, on the live models too.
    door("agent.autonomy_eval._builds", refuse("the module builds' run"))
    # A Windows power request: harmless, but no test should hold the
    # machine awake.
    door("agent.wake._power_request", lambda why: None)
    # The uninstaller's reads of the account: every AppContainer profile in
    # the registry, and Task Scheduler. Its Docker goes through
    # `sandbox._docker`, and its deletes through the doors above; the
    # Windows Hello key's through the installed app.
    door("core.uninstall._mappings", refuse("the registry's AppContainer list"))
    door("core.uninstall._powershell", refuse("PowerShell"))
    door("core.uninstall._app", refuse("the installed app, which deletes a Hello key"))
    return gone
