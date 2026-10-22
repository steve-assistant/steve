"""A gate test meets the same fences as a unit test.

Frozen. Until 8 October 2026 the doors to their keychain, Docker and every
model client were shut for tests/unit alone, so a gate test ran
in one environment on the laptop -- their real keys answering -- and another
as the sandbox account, with nothing. tests/gate/test_desk.py's lecture test built a
model client that way and failed every night's attempt in the sandbox
(shadow attempts #82-#87, and their request #88) while passing here. Each
fence is asked the way the code asks it, through tests/gate/conftest.py,
so a conftest that stops calling tests/gate/fences.py fails here.
"""

from __future__ import annotations

import asyncio
import importlib
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests.gate import fences

REPO = Path(__file__).resolve().parents[2]


def test_a_gate_test_reads_no_real_key_from_their_keychain():
    """On the laptop, without the fence, this read their real OpenRouter key;
    in the sandbox, nothing. Now both read the same dummy.

    Compared to booleans before asserting: a failing comparison prints both
    sides, and the side that fails here is their real key."""
    from core import secrets

    dummy = secrets.get("OPENROUTER_API_KEY") == "test-openrouter_api_key"
    assert dummy, "a gate test read a real API key from her keychain"
    unset = [k for k in ("TELEGRAM_BOT_TOKEN",) if secrets.get(k)]
    assert unset == [], "a gate test read these from her keychain"


async def test_a_gate_test_cannot_make_a_real_model_call():
    from pydantic_ai import Agent

    from agent.models import Role
    from agent.provider import model_for

    with pytest.raises(RuntimeError, match="live model"):
        await Agent(model_for(Role.CONVERSE)).run("hello")


def test_a_gate_test_finds_signal_inside_its_own_data_folder():
    """A shell that names signal-cli and its account would hand a test the
    user's linked device, and a message the test sent would reach their
    phone. Whatever the shell says, both are inside the test's own folder,
    where neither is installed."""
    from channels import signal
    from core import paths

    home = paths.data_dir().resolve()
    for found in (signal.signal_cli(), signal.signal_data()):
        assert found.resolve().is_relative_to(home), found


def test_a_gate_test_finds_the_add_ons_inside_its_own_data_folder_and_downloads_none():
    """In a checkout the add-ons are its `.addons/`: a test that installed
    or replaced one there would change the programs the developer's own
    Steve runs, and a test that downloaded one would fetch 180 MB."""
    from core import addons, paths

    home = paths.data_dir().resolve()
    assert paths.addons_dir().resolve().is_relative_to(home)
    for addon in addons.ALL:
        assert addons.home(addon).resolve().is_relative_to(home), addon.name
    with pytest.raises(RuntimeError, match="add-on downloads"):
        addons._download(addons.JAVA.url, paths.scratch_dir() / "java.zip",
                         most=addons.JAVA.size, progress=None)


def test_a_gate_test_finds_docker_fenced_on_every_machine():
    """Not "not running" here and a fence there: the same answer on the
    laptop, which has Docker, and in the sandbox, which does not."""
    from agent.browser import session

    with pytest.raises(RuntimeError, match="live Docker"):
        session.AVAILABLE()
    with pytest.raises(RuntimeError, match="live Docker"):
        session.READY()


def test_a_gate_test_cannot_start_docker_by_any_way_of_naming_it():
    """Modules and self-upgrade use no Docker (06-modules.md), so a test of
    them passes with it absent. Refused at the process's start, whatever
    starts it: on a computer that has Docker the call would otherwise
    succeed, and the path pass here and fail where it is not installed."""
    bin_ = r"C:\Program Files\Docker\Docker\resources\bin"
    starts = {
        "a list": lambda: subprocess.run(["docker", "ps"], capture_output=True),
        "a full path": lambda: subprocess.run([rf"{bin_}\docker.EXE", "ps"]),
        "a command line": lambda: subprocess.run(rf'"{bin_}\docker.exe" ps'),
        "the executable": lambda: subprocess.run(["ps"], executable=rf"{bin_}\docker.exe"),
        "the shell": lambda: subprocess.run("docker ps", shell=True),
        "PowerShell": lambda: subprocess.run(["powershell", "-Command", "docker ps"]),
        "os.system": lambda: __import__("os").system("docker-compose up"),
        "asyncio": lambda: asyncio.run(asyncio.create_subprocess_exec("com.docker.cli", "ps")),
    }
    refused = {}
    for how, start in starts.items():
        try:
            start()
        except RuntimeError as exc:
            refused[how] = "live Docker" in str(exc)
    assert refused == dict.fromkeys(starts, True)
    done = subprocess.run([sys.executable, "-c", "print('started')"], capture_output=True,
                          text=True, timeout=60)
    assert done.stdout.strip() == "started", "any other program still starts"


def test_a_process_a_test_starts_cannot_start_docker(tmp_path):
    done = subprocess.run([sys.executable, "-c", "import subprocess\n"
                           "subprocess.run(['docker', 'version'])\nprint('started')"],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "live Docker" in done.stderr, done.stdout


@pytest.mark.docker
def test_a_test_marked_docker_may_start_it_and_a_process_it_starts_may_not(tmp_path):
    """The code tool runs in Docker on purpose, and its own tests, marked
    `docker`, need the real one. The mark opens Docker to that test's own
    process and no further: a child it starts is fenced as any test's is,
    so a module path cannot borrow the opening."""
    # The event a start of Docker raises, without starting it.
    sys.audit("subprocess.Popen", "docker", ["docker", "ps"], None, None)
    done = subprocess.run([sys.executable, "-c", "import subprocess\n"
                           "subprocess.run(['docker', 'version'])\nprint('started')"],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "live Docker" in done.stderr, done.stdout


def test_a_gate_test_cannot_show_windows_sign_in_or_sign_anyone_in():
    """Windows' dialog would wait on a person, and a wrong password a test
    tried would count towards locking their account (core/winsignin.py)."""
    from core import winsignin

    with pytest.raises(RuntimeError, match="live Windows' sign-in"):
        winsignin.prove("a test")


def test_a_gate_test_cannot_reach_their_google_account():
    from modules.google_cloud import common as google_common
    from tests.fakes import google as fake_google

    bound = importlib.import_module("modules.google_cloud.gmail").service
    with google_common.as_account(fake_google.account()), pytest.raises(
            RuntimeError, match="live Google"):
        bound("gmail", "v1")


def test_a_door_in_changeable_code_that_moved_is_reported_and_the_others_stay_shut(
        monkeypatch, tmp_path):
    """This file is frozen and names about twenty doors in code the night's
    coder may change. A change that renamed one (then the password mailbox's `_connect`)
    used to error every test in both trees at setup, with the one file that
    names it out of its reach (the failure #87 met in test_cards, multiplied by
    the suite). Now the door is reported -- tests/unit/test_live_guards.py says
    so where a change can answer -- and every other is shut."""
    from agent import models_scout
    from agent.attention import fetch

    monkeypatch.delattr(fetch, "_get")

    # A folder inside tmp_path, as the conftest's: the fenced Python is made
    # two folders above it, in the worker's own folder. Given tmp_path
    # itself, it was made in the run's, which every worker shares, and two
    # workers' builds of it raced on CI (22 October 2026).
    gone = fences.live_services(monkeypatch, tmp_path / "fenced", "a gate test", "here")

    assert Path(sys.executable).is_relative_to(tmp_path.parent)
    assert gone == ["agent.attention.fetch._get"]
    with pytest.raises(RuntimeError, match="live OpenAI's model list"):
        models_scout._get("https://models.example/v1/models")


def test_a_door_in_frozen_code_that_moved_is_an_error_not_a_skip(monkeypatch, tmp_path):
    """Only a human moves core/, and then moves this file with it: a fence
    that quietly stopped fencing Jev's one call (core/decide.py), which the
    screen and the critic ask through, would be a test that rings a real
    model."""
    from core import decide

    monkeypatch.delattr(decide, "_post")

    with pytest.raises(AttributeError):
        fences.live_services(monkeypatch, tmp_path / "fenced", "a gate test", "here")


def test_a_gate_test_reaches_no_real_credential_manager():
    """Module secrets live in the Credential Manager (core/credstore.py). A
    test that set up an account would otherwise have written its password
    there, as the user, beside their real ones. Each test has an empty store
    of its own; putting the real one back is refused."""
    from core import credstore

    assert isinstance(credstore.store(), credstore.MemoryStore), "a test reached the real one"
    assert credstore.store().targets("") == []
    credstore.use(None)
    with pytest.raises(RuntimeError, match="live the Credential Manager"):
        credstore.store()


def test_pywin32s_credential_calls_answer_from_memory_in_a_test():
    """Below credstore too: code that called win32cred itself would reach the
    real one. Checked as patched before anything is called."""
    import win32cred

    assert getattr(win32cred, "_steve_fenced", False), "win32cred is the real one"
    win32cred.CredWrite({"TargetName": "steve-modules/k/a/X", "CredentialBlob": "v"}, 0)
    assert win32cred.CredRead("steve-modules/k/a/X", win32cred.CRED_TYPE_GENERIC)[
        "CredentialBlob"] == "v".encode("utf-16-le")
    win32cred.CredDelete("steve-modules/k/a/X", win32cred.CRED_TYPE_GENERIC)
    with pytest.raises(RuntimeError, match="not Steve's"):
        win32cred.CredRead("other-app/x", win32cred.CRED_TYPE_GENERIC)


CHILD_CREDENTIALS = """
import sys
import win32cred
if not getattr(win32cred, "_steve_fenced", False):
    sys.exit("the real Credential Manager")  # before any call
from core import credstore
name = credstore.target("k0001", "a0001", "PASSWORD")
secret = "p\\u00e4ss" + "x" * 3000
credstore.write(name, secret)
assert credstore.read(name) == secret
print("fenced")
"""


def test_a_process_a_test_starts_writes_its_secrets_nowhere_real(tmp_path):
    """A worker a test started reads and writes secrets like the product does,
    through core/credstore.py; there, the Credential Manager is one in its
    own memory."""
    done = subprocess.run([sys.executable, "-c", CHILD_CREDENTIALS], cwd=REPO,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "fenced", done.stderr[-2000:]


def test_the_credential_managers_stand_in_refuses_what_windows_refuses():
    """The stand-in took the blob as bytes, which the real `CredWrite`
    refuses, so every test passed on a store that could save no secret at
    all; the live check of 15 October 2026 found it. What Windows did then
    is held here: bytes refused, text stored as UTF-16, 1,280 characters
    taken and 1,281 refused with error 1783."""
    import types

    import pywintypes

    from tests.gate.childfence.sitecustomize import fence_win32cred

    win32cred = types.SimpleNamespace(CRED_TYPE_GENERIC=1, CRED_PERSIST_LOCAL_MACHINE=2)
    fence_win32cred(win32cred)
    name = "steve-modules/k0001/a0001/PASSWORD"
    cred = {"Type": 1, "TargetName": name, "UserName": "steve", "Persist": 2}

    with pytest.raises(TypeError, match="can not be converted to Unicode"):
        win32cred.CredWrite({**cred, "CredentialBlob": b"x"}, 0)
    with pytest.raises(pywintypes.error) as refused:
        win32cred.CredWrite({**cred, "CredentialBlob": "x" * 1281}, 0)
    assert refused.value.winerror == 1783

    win32cred.CredWrite({**cred, "CredentialBlob": "é" * 1280}, 0)
    assert win32cred.CredRead(name, 1)["CredentialBlob"] == ("é" * 1280).encode("utf-16-le")


def test_a_gate_test_cannot_connect_off_this_computer():
    """Step 3's mutation run reached OpenAI with a dummy key: only the
    personal edition's port was refused. 192.0.2.1 is a documentation
    address, so where the fence is missing the attempt goes nowhere."""
    with socket.socket() as sock:
        sock.setblocking(False)
        with pytest.raises(RuntimeError, match="off this computer"):
            sock.connect_ex(("192.0.2.1", 443))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, \
            pytest.raises(RuntimeError, match="off this computer"):
        udp.sendto(b"x", ("192.0.2.1", 53))


async def test_an_asyncio_connection_off_this_computer_is_refused_too():
    """On Windows asyncio connects through ConnectEx, which raises no audit
    event; the proactor's connect is wrapped beside the hook."""
    with pytest.raises(RuntimeError, match="off this computer"):
        await asyncio.wait_for(asyncio.open_connection("192.0.2.1", 443), 5)


def test_a_connection_on_this_computer_still_goes_through():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.create_connection(server.getsockname(), timeout=5):
            pass


CHILD_SOCKET = """
import socket
sock = socket.socket()
sock.setblocking(False)
sock.connect_ex(("192.0.2.1", 443))
print("connected")
"""


def test_a_process_a_test_starts_cannot_connect_off_this_computer(tmp_path):
    done = subprocess.run([sys.executable, "-c", CHILD_SOCKET], cwd=tmp_path,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "off this computer" in done.stderr, done.stdout


def test_a_provider_door_that_moved_is_reported_not_an_error(monkeypatch, tmp_path):
    """Item 4 moves Google's and Outlook's code into modules/. Shut
    strictly, its doors would have errored every test's setup at the first
    move, with this frozen file out of the change's reach."""
    import modules.google_cloud
    import modules.google_cloud.auth

    monkeypatch.delattr(modules.google_cloud.auth, "_HELD")
    monkeypatch.delattr(modules.google_cloud, "common", raising=False)
    monkeypatch.setitem(sys.modules, "modules.google_cloud.common", None)

    gone = fences.live_services(monkeypatch, tmp_path / "fenced", "a gate test", "here")

    assert "modules.google_cloud.auth._HELD" in gone
    assert "modules.google_cloud.common.build" in gone


def test_a_fenced_python_left_half_made_is_made_again_not_trusted(tmp_path):
    """A run stopped after the venv was made and before the fence went into
    it would leave a Python with no fence. Taken as made, it would start
    every child of every later test in that folder unfenced."""
    folder = tmp_path / "fenced-python"
    fences.fenced_python(folder)
    fence = next(folder.rglob("sitecustomize.py"))
    fence.unlink()
    next(folder.rglob("steve-fenced.pth")).unlink()

    fences.fenced_python(folder)

    assert fence.read_bytes() == (fences.CHILD_FENCE / "sitecustomize.py").read_bytes()
