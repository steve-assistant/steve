"""The fake world's canaries find a secret out of its place, and its
keychain is the one a backend's processes read and write.

Every end-to-end test ends with the scan (tests/e2e/conftest.py), and
passes when it finds nothing. A scan that looked in the wrong place, or
counted a secret's one place as a leak, would pass or fail every scenario
for its own reasons; so it is shown finding what was planted here. And a
backend reads its keys from the world's keychain only through the child
fence (tests/gate/childfence/sitecustomize.py), in processes the test did
not write, so that is driven from a child too.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import egress
from tests.fakes import world as worlds
from tests.fakes.openai import FakeOpenAI, Kept
from tests.fakes.telegram import TOKEN

REPO = Path(__file__).resolve().parents[2]


def test_a_secret_out_of_its_place_is_named_with_the_place_and_its_own_place_is_not(tmp_path):
    made = worlds.World(tmp_path / "world", worlds.canaries(), openai=FakeOpenAI())
    openai, jev = made.secrets["OPENAI_API_KEY"], made.secrets["OPENROUTER_API_KEY"]
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "worker.log").write_text(f"asked with {openai}\n", encoding="utf-8")
    # In a database page, among bytes that are no text.
    (data / "agent.db").write_bytes(b"\x00\xff" + jev.encode() + b"\x00\x81")
    (data / "run").mkdir()
    (data / "run" / "backend.json").write_text('{"token": "tok-abc"}', encoding="utf-8")
    made.openai.requests.append(Kept("/v1/responses", {"Authorization": f"Bearer {openai}"},
                                     {"input": f"the key is {jev}"}))

    found = made.leaks(data, also={worlds.TOKEN: "tok-abc"})

    assert sorted(found) == sorted([
        "OPENAI_API_KEY in the data folder (logs/worker.log)",
        "OPENROUTER_API_KEY in the data folder (agent.db)",
        "OPENROUTER_API_KEY in the fake OpenAI (body)",
    ])


def test_a_secret_written_in_utf16_is_found_and_a_file_no_one_may_read_is_said(tmp_path):
    """Windows writes UTF-16 (the registry's exports, PowerShell's output,
    a .NET program's log), and the scan read each file as Latin-1 alone:
    a key written that way passed it. And a file held open by a process
    that shares it with no one was passed over in silence, so whatever it
    held was never looked at."""
    import ctypes
    from ctypes import wintypes

    made = worlds.World(tmp_path / "world", worlds.canaries())
    openai = made.secrets["OPENAI_API_KEY"]
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "tool.log").write_bytes(f"\ufeffkey {openai}\n".encode("utf-16-le"))
    held = data / "held.db"
    held.write_bytes(b"\x00")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    # GENERIC_READ, shared with no one, OPEN_EXISTING.
    handle = kernel32.CreateFileW(str(held), 0x80000000, 0, None, 3, 0, None)
    assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    try:
        found = made.leaks(data)
        alone = worlds.found_in(data, {"OPENAI_API_KEY": openai}, "the app's folder")
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))

    assert sorted(found) == ["OPENAI_API_KEY in the data folder (logs/tool.log)",
                             "could not read the data folder (held.db)"]
    assert sorted(alone) == ["OPENAI_API_KEY in the app's folder (logs/tool.log)",
                             "could not read the app's folder (held.db)"]


def test_each_secret_is_a_value_of_this_runs_in_the_shape_its_check_takes():
    from channels import telegram
    from core import totp

    one, two = worlds.canaries(), worlds.canaries()
    assert set(one) == set(two) and not set(one.values()) & set(two.values())
    assert telegram.TOKEN_SHAPE.fullmatch(one["TELEGRAM_BOT_TOKEN"])
    assert totp.code_at(one["APPROVAL_TOTP_SECRET"], 0).isdigit()
    assert one["TELEGRAM_BOT_TOKEN"] != TOKEN


def test_the_world_points_a_backend_at_each_fake_and_each_fake_takes_its_canary(tmp_path):
    with worlds.world(tmp_path, telegram=True, signal=True) as made:
        env = made.env
        assert env["STEVE_OPENAI_URL"].startswith("http://127.0.0.1:")
        assert env["STEVE_OPENROUTER_URL"].endswith("/api")
        assert env["STEVE_TELEGRAM_URL"].startswith("http://127.0.0.1:")
        assert env[worlds.WORLD_ENV] == str(tmp_path)
        assert made.secrets["OPENAI_API_KEY"] in made.openai.keys
        assert made.secrets["OPENROUTER_API_KEY"] in made.jev.keys
        assert made.telegram.token == made.secrets["TELEGRAM_BOT_TOKEN"]
        assert (made.signal / "data" / "registered").is_file()
        assert env["STEVE_SIGNAL_DATA"] == str(made.signal)
        assert made.kept() == made.secrets


def test_a_fresh_installs_world_starts_with_nothing_kept(tmp_path):
    with worlds.world(tmp_path, openai=False, jev=False, kept=False) as made:
        assert made.kept() == {} and made.secrets


READ_WRITE = """
import json, keyring
read = keyring.get_password("steve", "OPENAI_API_KEY")
keyring.set_password("steve", "STEVE_PASS", "typed")
keyring.delete_password("steve", "BRAVE_API_KEY")
print(json.dumps([read, keyring.get_password("steve", "STEVE_PASS")]))
"""


@pytest.mark.processes
def test_a_process_started_in_a_world_reads_and_keeps_its_secrets_in_the_worlds_keychain(
        tmp_path):
    with worlds.world(tmp_path / "world", openai=False, jev=False) as made:
        env = {**os.environ, **made.env}
        ran = subprocess.run([sys.executable, "-E", "-s", "-c", READ_WRITE], env=env,
                             capture_output=True, text=True, timeout=60)
        assert ran.returncode == 0, ran.stderr
        assert json.loads(ran.stdout) == [made.secrets["OPENAI_API_KEY"], "typed"]
        kept = made.kept()
    assert kept["STEVE_PASS"] == "typed" and "BRAVE_API_KEY" not in kept


@pytest.mark.processes
def test_a_process_started_outside_a_world_still_reads_dummies_and_writes_nothing():
    ran = subprocess.run([sys.executable, "-E", "-s", "-c", READ_WRITE],
                         capture_output=True, text=True, timeout=60)
    assert ran.returncode != 0
    assert "tried to change the real keychain" in ran.stderr


CONSTANTS = """
import json
from agent import approvals, reminders
from agent.worker import background
from core.engines import imap
print(json.dumps([approvals.HOLD_SECONDS, reminders.TICK_S, background.ROUTINE_TICK_S,
                  background.OUTBOX_EVERY_S, background.BRIEFING_CHECK_S, imap.SENT_WAIT_S]))
"""


@pytest.mark.processes
def test_a_process_started_in_a_world_takes_its_settings_as_each_module_is_imported(tmp_path):
    """A scenario's yes waits out 2 s, not the product's 30 (`BRISK`); a
    test's own setting for a module runs after the world's, and the world's
    others for that module stand. Outside a world, the product's own values
    stand."""
    def run(env: dict) -> list:
        ran = subprocess.run([sys.executable, "-E", "-s", "-c", CONSTANTS], cwd=REPO, env=env,
                             capture_output=True, text=True, timeout=120)
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout)

    with worlds.world(tmp_path / "world", openai=False, jev=False,
                      after_import={"agent.reminders": "TICK_S = 3",
                                    "agent.worker.background": "BRIEFING_CHECK_S = 5"}) as made:
        assert run({**os.environ, **made.env}) == [2, 3, 1, 1, 5, 0.5]
    assert run(dict(os.environ)) == [30, 20, 30, 30, 300, 4.0]


NETWORK = """
import json, sys
import httpx
from core import egress, modctx
link = sys.argv[1]
host = httpx.URL(link).host
net = modctx.Network()
out = {"address": net.resolve(host, 443), "pinned": egress.pin_kind(host, 443)}
with httpx.Client(transport=net.transport_for(host)) as client:
    out["feed"] = client.get(link).text
try:
    net.resolve("calendar.else.example", 443)
    out["else"] = "resolved"
except Exception as exc:
    out["else"] = type(exc).__name__
print(json.dumps(out))
"""


@pytest.mark.processes
def test_a_process_in_a_world_reaches_its_servers_by_name_and_nothing_else(tmp_path):
    """The backend reaches a provider only through `core.modctx.Network`,
    in its own process. In a world that serves a feed, its host resolves to
    a public address (so setup pins it, and the egress checks pass as for a
    real provider), and HTTPS to it reaches the world's server, its
    certificate checked against the world's authority under the host's
    name. A host the world does not serve goes where it would have gone,
    and the child fence refuses it."""
    from tests.fakes import ics, worldnet

    feed = ics.FeedWorld()
    feed.seed("calendar", [{"title": "Essay due", "start": dt.date(2026, 10, 23)}])
    with worlds.world(tmp_path / "world", openai=False, jev=False, feed=feed) as made:
        ran = subprocess.run([sys.executable, "-E", "-s", "-c", NETWORK, made.feed_link],
                             cwd=REPO, env={**os.environ, **made.env}, capture_output=True,
                             text=True, timeout=120)
        assert ran.returncode == 0, ran.stderr
        out = json.loads(ran.stdout)
        network = json.loads((made.folder / "world.json").read_text(encoding="utf-8"))
    assert out["address"] == [worldnet.addresses(network["network"])[worlds.FEEDS]]
    assert out["pinned"] == egress.PUBLIC
    assert "SUMMARY:Essay due" in out["feed"]
    assert out["else"] != "resolved"


OWN_ADDRESS = """
import json
from core.sandbox import probe
print(json.dumps(probe.own_address()))
"""


@pytest.mark.processes
def test_a_world_that_runs_modules_gives_the_probe_this_computers_address(tmp_path):
    """The worker's check that modules can run here tries this computer's
    own address from inside a container. The probe finds it by a socket's
    connect, which the child fence refuses, and then gave no answer, so no
    world's backend offered to build a module. A world with `sandbox` gives
    the probe the address from the host name instead."""
    from tests.fakes import worldnet

    with worlds.world(tmp_path / "world", openai=False, jev=False, sandbox=True) as made:
        ran = subprocess.run([sys.executable, "-E", "-s", "-c", OWN_ADDRESS], cwd=REPO,
                             env={**os.environ, **made.env}, capture_output=True, text=True,
                             timeout=120)
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout) == worldnet.own_address() != ""
