"""Steve never shares a name with the personal edition it was copied from.

Both run as the same Windows user on the machine Steve is developed on, so
they share that user's environment variables, Credential Manager, loopback
ports, Task Scheduler and Run key, and Docker. The copy arrived using the
personal edition's names for all of them. Among them were the relay pair
`AGENT_RELAY_URL` and `AGENT_LINK_TOKEN`, set in that user's environment: a
product worker started by hand would have dialled the personal relay, and,
the newest link winning there, taken the personal agent's Signal away. Its
coding sandbox read `OPENAI_API_KEY@agent-tool` directly, its browser vault
used a second keychain service, and its Docker label, images and devtools
scratch folders were the personal edition's too.

core/identity.py now holds every shared name. These tests check the tree
for the old ones, and each shared name from the side of the code that uses
it. Each was proven failing against the unfixed copy (2b401e4).

Steve's names are pinned here as literals, not read from core/identity.py:
a test comparing each use with that module would pass if the module itself
were changed back.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.gate import repo_files
from tests.gate.fences import PERSONAL_PORT

REPO = Path(__file__).resolve().parents[2]

NAME, KEYCHAIN, ENV_PREFIX = "Steve", "steve", "STEVE_"

# This file and the fences name the personal edition's identity on purpose.
SELF = {"tests/gate/test_identity.py", "tests/gate/fences.py",
        "tests/gate/childfence/sitecustomize.py"}

# Code that still names the personal edition, with the step that removes it.
# A path leaves this list in that step; a path that no longer names it fails
# test_every_exemption_is_still_needed, so the list can only shrink.
EXEMPT: dict[str, str] = {}

# Also the bare prefix in quotes, as a scrub or a lookup by prefix writes it.
OLD_VARIABLE = re.compile(r"\bAGENT_(?:[A-Z]|[\"'])")
OLD_PORT = re.compile(rf"\b{PERSONAL_PORT}\b")
# Any spelling of the old name, the old sandbox account's, and the old name
# of the browser's containers.
OLD_NAME = re.compile(r"(?i)agent[-_ ]?tool|AgentCoder|agent-browser")
# The personal edition's hotkeys (its ui/src/api.ts): to talk, the quick
# bar and its desk.
PERSONAL_HOTKEYS = {"Alt+End", "Alt+Backquote", "Alt+Home"}


def _tracked() -> list[str]:
    return [rel for rel in repo_files.listed()
            if rel not in SELF]


def _text(rel: str) -> str:
    return repo_files.text(rel) or ""  # binary


def _lines_matching(pattern: re.Pattern, rels) -> list[str]:
    found = []
    for rel in rels:
        for number, line in enumerate(_text(rel).splitlines(), 1):
            if pattern.search(line):
                found.append(f"{rel}:{number}: {line.strip()[:120]}")
    return found


def test_no_file_names_a_personal_edition_variable():
    """Code reading one would read the personal edition's setting; a doc
    naming one would tell a reader to set it."""
    found = _lines_matching(OLD_VARIABLE, _tracked())
    assert not found, f"use {ENV_PREFIX}* names (core/identity.py):\n  " + "\n  ".join(found)


def test_no_file_names_the_personal_edition_port():
    found = _lines_matching(OLD_PORT, _tracked())
    assert not found, ("Steve's supervisor takes a port the system chooses:\n  "
                       + "\n  ".join(found))


def test_no_code_uses_a_personal_edition_name():
    """Keychain services, task names, Docker labels and images, scratch
    folders: anything in code or configuration. Prose (*.md) may still tell
    the history."""
    code = [rel for rel in _tracked() if not rel.endswith(".md") and rel not in EXEMPT]
    found = _lines_matching(OLD_NAME, code)
    assert not found, "take the name from core/identity.py:\n  " + "\n  ".join(found)


def test_every_exemption_is_still_needed():
    stale = [rel for rel in EXEMPT if not _lines_matching(OLD_NAME, [rel])]
    assert not stale, f"no longer names the personal edition; drop from EXEMPT: {stale}"


def test_each_shared_name_is_steves():
    from agent import wake
    from channels import signal
    from core import localonly, secrets, totp
    from core.browser import vault

    assert secrets.SERVICE == KEYCHAIN
    assert vault.SERVICE.startswith(KEYCHAIN + "-")
    assert wake.task_name().startswith(f"{NAME}Wake.")
    assert signal.DEVICE_NAME == NAME and totp.ISSUER == NAME
    ports = {int(m) for o in localonly.ALLOWED_ORIGINS for m in re.findall(r":(\d+)$", o)}
    assert ports == {5173}, "Vite's dev server only: the app's own origins have no port"


def test_the_module_sandbox_is_steves():
    """AppContainer profiles, their capability and the pipes belong to the
    Windows user, shared by every program of theirs. Named for Steve and for
    this install, a profile Steve deletes is never another install's, and a
    pipe it serves is never one another install's module dials. Until step 6
    this pinned the coding sandbox's account, folder and firewall rule, which
    went with self-upgrade v1."""
    from core import identity

    assert identity.container("ab12cd34", "notes") == f"{KEYCHAIN}.ab12cd34.m.notes"
    assert identity.probe_container("ab12cd34") == f"{KEYCHAIN}.ab12cd34.probe"
    assert f"{KEYCHAIN}.runtime" == identity.RUNTIME_CAPABILITY
    assert rf"\\.\pipe\{KEYCHAIN}." == identity.PIPE_PREFIX


def test_steves_containers_and_images_are_labelled_as_steves():
    """Docker is the user's, shared with the personal edition: a label or an
    image name in common, and one install's cleanup removes the other's
    containers."""
    from core.browser import sandbox
    from tools import code_exec

    assert f"{KEYCHAIN}.browser" == sandbox.LABEL
    assert sandbox.image("browser").startswith(f"{KEYCHAIN}/browser-")
    assert code_exec.analysis_image().startswith(f"{KEYCHAIN}/")


def test_the_identity_module_holds_steves_names():
    from core import identity

    assert (identity.NAME, identity.KEYCHAIN, identity.ENV_PREFIX) == (NAME, KEYCHAIN, ENV_PREFIX)


def test_the_supervisor_listens_on_a_loopback_port_the_system_chose_and_names_it_in_the_run_file(
        monkeypatch):
    """A fixed port, 8790, could be taken first by another account's program
    on the same PC, which the app would then have sent everything. So the
    port is the system's choice at each start, on loopback alone, and the
    run file that names it goes when the supervisor stops."""
    from core import localapi, supervisor

    served = {}

    def serve(self, sockets=None):
        served["address"] = sockets[0].getsockname()
        served["published"] = localapi.read()
        sockets[0].close()

    monkeypatch.setattr(supervisor.uvicorn.Server, "run", serve)
    monkeypatch.setattr(supervisor.state, "backend", None)
    monkeypatch.setattr(supervisor.state, "server", None)
    monkeypatch.setattr("sys.argv", ["supervisor"])
    supervisor.main()

    host, port = served["address"]
    assert host == "127.0.0.1" and port not in (0, PERSONAL_PORT)
    assert served["published"].port == port and len(served["published"].token) == 64
    assert localapi.read() is None, "the run file outlived the supervisor"


def test_the_supervisors_port_takes_a_connection_from_the_moment_it_is_published():
    """The first worker starts before uvicorn listens, and a caller that read
    the run file meanwhile met a port bound but not listening, which Windows
    takes two seconds to refuse: two seconds lost per try, after the
    supervisor was ready."""
    import socket

    from core import worker_handle

    sock = worker_handle.listening()
    try:
        with socket.create_connection(sock.getsockname(), timeout=1.0):
            pass
    finally:
        sock.close()


def test_the_supervisors_clients_of_its_workers_read_no_certificates(monkeypatch):
    """They speak plain HTTP to 127.0.0.1, and a client left to make its own
    TLS context read certifi's bundle: a third of a second each, three a
    swap (core/localapi.tls)."""
    import ssl

    from core import localapi, worker_handle

    localapi.tls()  # the one shared context, made before the watch begins

    def read(*args, **kwargs):
        raise AssertionError("a loopback client read certificates")

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", read)
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", read)

    worker_handle._client(2.0)


def test_the_app_is_steves():
    """The Tauri identifier keys the app's own storage; the product name is
    its install folder and its uninstall entry."""
    conf = json.loads((REPO / "ui" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    assert conf["productName"] == NAME
    assert conf["identifier"] == "dev.steve.desktop"
    # No address of its own: the shell reads the run file (core/localapi.py).
    for name in ("api.ts", "control.ts", "logins.ts"):
        source = (REPO / "ui" / "src" / name).read_text(encoding="utf-8")
        assert not re.search(r"127\.0\.0\.1:\d|localhost:\d", source), name
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == KEYCHAIN


def test_the_apps_hotkeys_are_not_the_personal_editions():
    """Both run as the same user, and Windows gives a hotkey to whichever
    registers it first: with the same default, one edition's key did
    nothing, and which one depended on which started first. The window
    binds every hotkey (ui/src/api.ts); the shell binds none of its own."""
    source = (REPO / "ui" / "src" / "api.ts").read_text(encoding="utf-8")
    offered: set[str] = set()
    for name in ("HOTKEY_FALLBACKS", "HOTKEY_CHOICES"):
        block = re.search(rf"export const {name} = \[(.*?)\] as const;", source, re.S)
        assert block, f"{name} is not in ui/src/api.ts"
        offered |= set(re.findall(r'"([^"]+)"', block.group(1)))
    assert "Super+Alt+S" in offered, "the keys were read"
    assert not offered & PERSONAL_HOTKEYS
    shell = (REPO / "ui" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert not re.search(r"Shortcut::new|with_shortcuts?\(|\.register\(", shell)


@pytest.mark.processes
def test_a_worker_started_with_the_personal_editions_pair_dials_nothing(tmp_path):
    """The copy's relay client read `AGENT_RELAY_URL` and `AGENT_LINK_TOKEN`
    from the user environment and would have dialled that relay. The client
    is gone; this starts a worker by hand with the pair pointing at a
    listener here, waits for it to take up its duties, and checks that
    nothing came. (That no file names such a variable is
    `test_no_file_names_a_personal_edition_variable`.)"""
    import socket
    import time

    import httpx

    old = "AGENT" + "_"
    with socket.socket() as listener, socket.socket() as spare:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
        spare.close()
        env = {**os.environ,
               old + "RELAY_URL": f"ws://127.0.0.1:{listener.getsockname()[1]}/link",
               old + "LINK_TOKEN": "the personal edition's token"}
        # The token it will require, as a supervisor would publish it.
        from core import localapi

        backend = localapi.publish(port)
        log = (tmp_path / "worker.log").open("w", encoding="utf-8")
        worker = subprocess.Popen([sys.executable, "-m", "agent.server", "--port", str(port)],
                                  cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            duties = None
            deadline = time.monotonic() + 120
            while duties != "active" and time.monotonic() < deadline:
                assert worker.poll() is None, (tmp_path / "worker.log").read_text("utf-8")[-2000:]
                try:  # the shared context: a client's own took half a second (localapi.tls)
                    duties = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2,
                                       headers=backend.headers,
                                       verify=localapi.tls()).json().get("duties")
                except httpx.HTTPError:
                    time.sleep(0.5)
            assert duties == "active", "the worker never took up its duties"
            time.sleep(3)  # its loops' first passes
            with pytest.raises(BlockingIOError):
                listener.accept()
        finally:
            worker.kill()
            worker.wait(timeout=30)
            log.close()


def test_a_test_cannot_reach_the_personal_editions_port():
    import socket

    with socket.socket() as sock, pytest.raises(RuntimeError, match="personal edition"):
        sock.connect(("127.0.0.1", PERSONAL_PORT))


def test_a_test_cannot_reach_the_personal_editions_port_through_asyncio():
    """asyncio on Windows connects through `ConnectEx`, which raises no audit
    event: the hook alone let every async client through -- httpx's, and
    the websockets the supervisor uses."""
    async def dial():
        await asyncio.open_connection("127.0.0.1", PERSONAL_PORT)

    with pytest.raises(RuntimeError, match="personal edition"):
        asyncio.run(dial())


def test_a_test_cannot_read_the_personal_editions_keychain():
    from core import secrets

    with pytest.raises(RuntimeError, match="not Steve's"):
        secrets.keyring.get_password("agent-tool", "OPENAI_API_KEY")


def test_a_test_cannot_change_the_keychain():
    """Reads were fenced and writes were not: a test that stored or deleted
    a secret through core.secrets changed the real Credential Manager.
    Checked by identity first, so a missing fence fails here rather than
    by writing."""
    import keyring.core

    from core import secrets

    assert secrets.keyring.set_password is not keyring.core.set_password
    assert secrets.keyring.delete_password is not keyring.core.delete_password
    with pytest.raises(RuntimeError, match="the keychain's writes"):
        secrets.set_secret("BRAVE_API_KEY", "a test's")


def test_a_test_sees_none_of_the_personal_editions_variables():
    """The user's environment holds that edition's own variables."""
    assert not [n for n in os.environ if n.upper().startswith("AGENT_")]


# Run in a process a test starts. It prints what it was allowed, never a
# value: with the fence missing, the keychain read would be a real one.
CHILD = '''
import json, os, socket
import keyring, keyring.core
out = {}
try:
    socket.create_connection(("127.0.0.1", PERSONAL_PORT), timeout=2).close()
    out["port"] = "connected"
except RuntimeError as e:
    out["port"] = "refused" if "personal edition" in str(e) else "other"
except OSError:
    out["port"] = "unreachable"
try:
    keyring.get_password("agent-tool", "OPENAI_API_KEY")
    out["other_keychain"] = "read"
except RuntimeError:
    out["other_keychain"] = "refused"
out["writes_fenced"] = keyring.set_password is not keyring.core.set_password
out["own_key_a_dummy"] = keyring.get_password("steve", "OPENAI_API_KEY") == \\
    "test-openai_api_key"
out["agent_vars"] = [n for n in os.environ if n.upper().startswith("AGENT_")]
print(json.dumps(out))
'''


@pytest.mark.processes
def test_a_process_a_test_starts_is_fenced_like_the_test():
    """A supervisor, worker or script a test starts is a new interpreter,
    and inherited none of the fences: a worker a gate test started read the
    real `steve` keychain. Every Python a test starts now imports
    tests/gate/childfence/sitecustomize.py first."""
    done = subprocess.run(
        [sys.executable, "-c", CHILD.replace("PERSONAL_PORT", str(PERSONAL_PORT))],
        capture_output=True, text=True, timeout=120, cwd=REPO)

    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout) == {
        "port": "refused", "other_keychain": "refused", "writes_fenced": True,
        "own_key_a_dummy": True, "agent_vars": []}


# Reads no keychain and dials nothing real: if the fence is missing, the
# address is one no network routes (TEST-NET-1), and the keychain is only
# looked at, not read.
FENCED = '''
import json, socket, subprocess, sys
import keyring, keyring.core
out = {}
try:
    socket.create_connection(("192.0.2.1", 9), timeout=1).close()
    out["off_this_computer"] = "connected"
except RuntimeError:
    out["off_this_computer"] = "refused"
except OSError:
    out["off_this_computer"] = "unreachable"
out["keychain_fenced"] = keyring.set_password is not keyring.core.set_password
if len(sys.argv) > 1:
    child = subprocess.run([sys.executable, "-E", "-s", "-B", "-c", sys.argv[1]],
                           capture_output=True, text=True, timeout=60)
    out["its_worker"] = json.loads(child.stdout or "null")
print(json.dumps(out))
'''


@pytest.mark.processes
@pytest.mark.parametrize("flags", [["-E", "-s", "-B"], ["-I", "-B"]],
                         ids=["as a worker starts", "as the app starts its backend"])
def test_a_python_started_with_flags_that_drop_pythonpath_is_fenced_all_the_same(flags):
    """A worker starts with `-E -s` and the app's backend with `-I`, and both
    drop PYTHONPATH, which was how the fence reached a test's children. So
    a worker a gate test's supervisor started had the real `steve` keychain
    and the network. The Python a test starts now carries the fence in its
    own site-packages, and so does whatever that starts in turn."""
    done = subprocess.run([sys.executable, *flags, "-c", FENCED, FENCED],
                          capture_output=True, text=True, timeout=120, cwd=REPO)

    assert done.returncode == 0, done.stderr[-2000:]
    fenced = {"off_this_computer": "refused", "keychain_fenced": True}
    assert json.loads(done.stdout) == {**fenced, "its_worker": fenced}


def test_scrubbing_removes_the_personal_editions_variables_in_any_case():
    """Windows compares variable names without case."""
    from core import identity

    env = {"AGENT_RELAY_URL": "ws://x", "agent_link_token": "t",
           "STEVE_INERT": "1", "MAGENT_X": "kept"}

    assert sorted(identity.scrub_environment(env)) == ["AGENT_RELAY_URL",
                                                     "agent_link_token"]
    assert env == {"STEVE_INERT": "1", "MAGENT_X": "kept"}


def test_the_supervisor_scrubs_them_before_it_starts_anything(monkeypatch):
    """Every worker, coder run and script inherits its environment."""
    from core import supervisor

    seen = {}

    def serve(self, sockets=None):
        seen.update(env=dict(os.environ))
        sockets[0].close()

    monkeypatch.setattr(supervisor.uvicorn.Server, "run", serve)
    monkeypatch.setattr(supervisor.state, "backend", None)
    monkeypatch.setattr(supervisor.state, "server", None)
    monkeypatch.setattr("sys.argv", ["supervisor"])
    monkeypatch.setenv("AGENT_RELAY_URL", "ws://127.0.0.1:9/link")
    supervisor.main()

    assert "AGENT_RELAY_URL" not in seen["env"]


@pytest.mark.parametrize(("rel", "function"), [
    ("core/supervisor.py", "main"), ("agent/server.py", "main")])
def test_each_way_in_scrubs_the_environment_first(rel, function):
    """A worker started by hand or by a test starts children, and the
    supervisor's scrub does not reach it. (A worker is not imported here:
    see agent/server.py's alias.)"""
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    body = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == function).body
    first = [ast.unparse(node.value) for node in body[:3] if isinstance(node, ast.Expr)]
    assert "identity.scrub_environment()" in first
