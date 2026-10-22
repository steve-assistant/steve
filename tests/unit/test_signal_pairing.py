"""Setting Signal up from the app: installed, linked and unlinked.

`agent/worker/pairing.py` replaced a command line the app's user does not
have. What must hold, faked at signal-cli's edge (`channels/signal.py`):
- each step runs only from the state it belongs to, so a second click
  cannot install over a linked device or start a second signal-cli on one
  account folder;
- the state says how it is going, and ends somewhere the app can act on:
  linked and running, or unlinked saying why;
- a newer code, or an unlink, ends the attempt before it, and the older
  attempt's ending changes nothing;
- unlinking stops the bridge, deletes the keys, and stops the messages
  that were waiting for Signal; the app is told what it took back
  (tests/gate/test_routines.py pins that it does);
- and while Signal is not linked, messages are the app's alone.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from agent import outbox, routines, runtime
from agent.worker import links, pairing
from channels import signal
from core import addons, db

LINK = "sgnl://linkdevice?uuid=a-fake-uuid&pub_key=a-fake-key"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(pairing, "_lock", asyncio.Lock())
    monkeypatch.setattr(pairing, "_attempt", {"n": 0, "proc": None})
    monkeypatch.setattr(pairing, "_running", set())
    monkeypatch.setattr(runtime, "signal_state", {"state": "unavailable"})
    monkeypatch.setattr(runtime, "signal", None)
    monkeypatch.setattr(runtime, "conn", None)


@pytest.fixture
def started(monkeypatch) -> list[dict]:
    """`links.start_signal`, recording the state it was called in."""
    calls: list[dict] = []

    async def start():
        calls.append(dict(runtime.signal_state))
        links.signal_is("ready")

    monkeypatch.setattr(links, "start_signal", start)
    return calls


def _body(response) -> dict:
    return json.loads(response.body)


class _Asked:
    """The app's POST with no body, as much of a Starlette request as the
    routes read."""

    async def json(self):
        raise ValueError("no body")


async def _settled() -> None:
    await asyncio.gather(*list(pairing._running))


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

async def test_install_downloads_in_the_background_and_starts_signal(monkeypatch, started):
    def install(progress):
        progress(1_000, 2_000)

    monkeypatch.setattr(signal, "install", install)

    response = await pairing.signal_install(None)

    assert response.status_code == 202
    assert _body(response)["bridge"] == {"state": "installing", "done": 0,
                                         "of": addons.SIGNAL_CLI.size + addons.JAVA.size}
    await _settled()
    # Told as it went, on the loop: the app's progress bar reads it.
    assert started == [{"state": "installing", "done": 1_000, "of": 2_000}]
    assert runtime.signal_state == {"state": "ready"}


async def test_an_install_that_fails_says_why(monkeypatch, started):
    def install(progress):
        raise addons.AddonError("Java did not download as published")

    monkeypatch.setattr(signal, "install", install)

    await pairing.signal_install(None)
    await _settled()

    assert started == []
    assert runtime.signal_state == {"state": "unavailable",
                                    "error": "Java did not download as published"}


async def test_a_second_install_click_joins_the_first(monkeypatch, started):
    gate = threading.Event()
    installs: list[bool] = []

    def install(progress):
        installs.append(True)
        gate.wait(10)

    monkeypatch.setattr(signal, "install", install)

    first = await pairing.signal_install(None)
    second = await pairing.signal_install(None)
    gate.set()
    await _settled()

    assert (first.status_code, second.status_code) == (202, 202)
    assert installs == [True]


@pytest.mark.parametrize("state", ["ready", "starting", "failed", "linking"])
async def test_install_over_a_linked_or_linking_device_is_refused(monkeypatch, started, state):
    monkeypatch.setattr(runtime, "signal_state", {"state": state})
    monkeypatch.setattr(signal, "install", lambda progress: pytest.fail("installed again"))

    response = await pairing.signal_install(None)

    assert response.status_code == 409 and runtime.signal_state == {"state": state}


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------

class _Proc:
    """A signal-cli link process; `finish_link` answers when told."""

    def __init__(self, n: int):
        self.n = n
        self.answer: dict | None = None
        self.answered = threading.Event()

    def answers(self, result: dict) -> None:
        self.answer = result
        self.answered.set()


@pytest.fixture
def linking(monkeypatch) -> dict:
    """signal-cli's link, faked: the processes started, and those ended."""
    made: dict = {"procs": [], "ended": []}

    def start_link():
        proc = _Proc(len(made["procs"]) + 1)
        made["procs"].append(proc)
        return LINK, proc

    def finish_link(proc):
        proc.answered.wait(10)
        return proc.answer

    def end_link(proc):
        made["ended"].append(proc.n)
        proc.answers({"linked": False, "error": "ended"})

    monkeypatch.setattr(signal, "start_link", start_link)
    monkeypatch.setattr(signal, "finish_link", finish_link)
    monkeypatch.setattr(signal, "end_link", end_link)
    monkeypatch.setattr(runtime, "signal_state", {"state": "unlinked"})
    return made


async def test_link_answers_a_qr_code_and_a_scan_starts_signal(linking, started):
    response = await pairing.signal_link(_Asked())

    body = _body(response)
    assert response.status_code == 200 and body["wait_s"] == signal.LINK_WAIT_S
    assert body["qr"].startswith("<svg") or body["qr"].startswith("<?xml")
    assert LINK not in body["qr"]
    assert runtime.signal_state == {"state": "linking", "wait_s": signal.LINK_WAIT_S}

    linking["procs"][0].answers({"linked": True, "account": "+15555550123"})
    await _settled()

    assert started == [{"state": "linking", "wait_s": signal.LINK_WAIT_S}]
    assert runtime.signal_state == {"state": "ready"}


async def test_a_link_that_did_not_finish_is_unlinked_saying_what_to_do(linking, started):
    await pairing.signal_link(_Asked())

    linking["procs"][0].answers({"linked": False, "error": "Show a new code to try again."})
    await _settled()

    assert started == []
    assert runtime.signal_state == {"state": "unlinked",
                                    "error": "Show a new code to try again."}


async def test_a_new_code_ends_the_one_before_and_the_old_ending_changes_nothing(linking,
                                                                                started):
    """Two signal-cli processes on one account folder: the second waits on
    the first's lock. And the first one's "not scanned" must not undo the
    code on the screen now."""
    await pairing.signal_link(_Asked())
    first = list(pairing._running)
    await pairing.signal_link(_Asked())
    await asyncio.gather(*first)

    assert linking["ended"] == [1]
    assert runtime.signal_state["state"] == "linking"

    linking["procs"][1].answers({"linked": True, "account": "+15555550123"})
    await _settled()
    assert runtime.signal_state == {"state": "ready"}


async def test_signal_cli_giving_no_code_is_an_error_the_app_can_show(monkeypatch, linking):
    def start_link():
        raise signal.LinkFailed("signal-cli gave no link to scan: Error: no network")

    monkeypatch.setattr(signal, "start_link", start_link)

    response = await pairing.signal_link(_Asked())

    assert response.status_code == 502
    assert runtime.signal_state == {"state": "unlinked", "error": _body(response)["error"]}


async def test_a_link_refused_because_signal_cli_changed_shows_signal_unavailable(
        monkeypatch, linking):
    """Shown as unlinked, the app would offer the scan again, and it would be
    refused again; unavailable says to set Signal up again."""
    why = "Java has changed since it was installed, so it is not run; set it up again"

    def start_link():
        raise signal.Unready(why)

    monkeypatch.setattr(signal, "start_link", start_link)

    response = await pairing.signal_link(_Asked())

    assert (response.status_code, _body(response)) == (409, {"error": why})
    assert runtime.signal_state == {"state": "unavailable", "hint": why}


@pytest.mark.parametrize("state, why", [
    ("ready", "Signal is linked already."),
    ("failed", "Signal is linked already."),
    ("unavailable", "Set Signal up first."),
    ("installing", "Set Signal up first."),
])
async def test_link_is_refused_unless_signal_is_set_up_and_not_linked(monkeypatch, linking,
                                                                     state, why):
    monkeypatch.setattr(runtime, "signal_state", {"state": state})

    response = await pairing.signal_link(_Asked())

    assert (response.status_code, _body(response)) == (409, {"error": why})
    assert linking["procs"] == []


# ---------------------------------------------------------------------------
# Unlinking
# ---------------------------------------------------------------------------

class _Bridge:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.fixture
def conn(tmp_path, monkeypatch):
    store = db.open_store(tmp_path / "steve.db")
    monkeypatch.setattr(runtime, "conn", store)
    yield store
    store.close()


async def test_unlink_stops_signal_deletes_its_keys_and_its_waiting_messages(monkeypatch,
                                                                           conn):
    bridge = _Bridge()
    forgotten: list[bool] = []
    monkeypatch.setattr(runtime, "signal", bridge)
    monkeypatch.setattr(runtime, "signal_state", {"state": "ready"})
    monkeypatch.setattr(signal, "forget", lambda: forgotten.append(True))
    waiting = outbox.add(conn, "your reminder", kind="reminder", remote="signal")
    for_the_app = outbox.add(conn, "your briefing", kind="briefing")

    response = await pairing.signal_unlink(None)

    assert bridge.stopped and forgotten == [True] and runtime.signal is None
    assert _body(response) == {"bridge": {"state": "unlinked"},
                               "goodbye": signal.SignalChannel.goodbye}
    assert outbox.get(conn, waiting).stopped == "Signal was unlinked"
    assert outbox.get(conn, for_the_app).stopped is None


async def test_the_app_is_told_what_unlinking_paused_and_withdrew(monkeypatch, conn):
    """Unlinking pauses what the chat approved and withdraws its waiting
    cards; the user, who may not have known, reads which and how to undo
    it beside the goodbye."""
    monkeypatch.setattr(runtime, "signal", None)
    monkeypatch.setattr(runtime, "signal_state", {})
    monkeypatch.setattr(signal, "forget", lambda: None)
    with conn:
        chat = conn.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
        conn.execute("INSERT INTO actions (tool, args_json, args_hash, preview, reach,"
                     " reversal, channel) VALUES ('send_email', '{}', 'h', 'p', 'outward',"
                     " 'irreversible', 'signal')")
    routines.create(conn, title="Digest", instruction="Sum up my inbox.",
                    schedule=routines.Schedule.build(at_time="07:00"), session_id=chat)

    said = _body(await pairing.signal_unlink(None))["goodbye"]

    assert said == (f"{signal.SignalChannel.goodbye} 1 routine approved from Signal is paused."
                    " If you turn it on again in the app, it asks you before each send."
                    " 1 card asked there was withdrawn.")


async def test_unlink_ends_a_link_under_way(monkeypatch, linking):
    monkeypatch.setattr(signal, "forget", lambda: None)
    await pairing.signal_link(_Asked())

    await pairing.unlink_signal()
    await _settled()

    assert linking["ended"] == [1]
    assert runtime.signal_state == {"state": "unlinked"}


async def test_keys_that_could_not_be_deleted_are_not_reported_as_unlinked(monkeypatch):
    def forget():
        raise PermissionError("in use")

    monkeypatch.setattr(signal, "forget", forget)
    monkeypatch.setattr(runtime, "signal_state", {"state": "ready"})

    await pairing.unlink_signal()

    assert runtime.signal_state["state"] == "failed"
    assert "could not all be deleted" in runtime.signal_state["error"]


# ---------------------------------------------------------------------------
# Where messages go meanwhile
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state, remote", [
    ("unavailable", None), ("installing", None), ("unlinked", None), ("linking", None),
    ("starting", "signal"), ("ready", "signal"), ("failed", "signal"),
])
def test_messages_are_the_apps_alone_until_signal_is_linked(monkeypatch, state, remote):
    """A message sent to Signal before it is linked would wait a day for a
    device that may never be linked, and then show as not sent."""
    monkeypatch.setattr(links, "signal_installed", lambda: True)
    monkeypatch.setattr(runtime, "signal_state", {"state": state})

    assert links.remote_channel() == remote
