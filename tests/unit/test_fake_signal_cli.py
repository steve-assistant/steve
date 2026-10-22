"""The fake signal-cli's daemon speaks as `channels/signal.py` reads it.

The end-to-end tests talk to Steve on Signal through
`tests/fakes/signal_cli.py`, which the worker starts as it starts the real
one. If what it reports drifted from what the channel reads, every Signal
scenario would fail inside the channel, far from the cause; if it kept a
send wrongly, a scenario would pass on a reply never sent. So the channel
itself is driven here, against the fake run as a process.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socket
import sys
import threading
import time

import pytest

from channels import signal
from channels.base import Outgoing
from tests.fakes import signal_cli

pytestmark = [pytest.mark.processes,
              pytest.mark.skipif(sys.platform != "win32", reason="the launcher is a .bat")]


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """The fake as `STEVE_SIGNAL_CLI`, run as the real one is, through a
    launcher .bat, with a phone already linked. Gives its folder."""
    launcher = tmp_path / "signal-cli.bat"
    launcher.write_text(f'@"{sys.executable}" "{signal_cli.__file__}" %*\r\n',
                        encoding="utf-8")
    monkeypatch.setenv(signal.CLI_ENV, str(launcher))
    signal_cli.linked(signal.signal_data())
    return signal.signal_data()


def _refused(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return True
    return False


async def test_a_note_to_self_comes_in_and_the_reply_and_a_reaction_to_it_go_round(daemon):
    channel = signal.SignalChannel()
    await channel.start(timeout=30)
    port = channel.port
    try:
        assert channel.account == signal_cli.NUMBER
        events = channel.receive()
        stamp = signal_cli.says(daemon, "Remind me to call Iris at 5")
        heard = await asyncio.wait_for(anext(events), 15)
        assert await channel.send(Outgoing(text="**Done.** At 5, then."))
        await channel.typing(True)
        reply = signal_cli.sent(daemon)[0]
        signal_cli.reacts(daemon, "👍", reply["timestamp"])
        reacted = await asyncio.wait_for(anext(events), 15)
        await events.aclose()
    finally:
        await channel.stop()

    assert (heard.text, heard.timestamp) == ("Remind me to call Iris at 5", stamp)
    assert [call["method"] for call in signal_cli.sent(daemon)] == ["send", "sendTyping"]
    assert reply["params"] == {"message": "Done. At 5, then.",
                               "recipient": [signal_cli.NUMBER], "notifySelf": True}
    # The reaction names the reply by the timestamp the daemon gave it, and
    # the channel reads the reply's words back by it, as a card's yes is.
    assert (reacted.reaction, reacted.reacted_to) == ("👍", reply["timestamp"])
    assert channel.sent_text(reacted.reacted_to) == "Done. At 5, then."
    # Stopped, the daemon is gone with its launcher: a fake left serving
    # would answer the next test's channel.
    deadline = time.monotonic() + 10
    while not _refused(port) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _refused(port) and not channel.alive


async def test_a_stream_opened_again_gets_only_what_came_after_the_last(daemon):
    """The channel reconnects on its own; a stream that replayed the inbox
    would hand the worker every message again, and each would be answered
    twice."""
    channel = signal.SignalChannel()
    await channel.start(timeout=30)
    try:
        first = channel.receive()
        signal_cli.says(daemon, "one")
        assert (await asyncio.wait_for(anext(first), 15)).text == "one"
        await first.aclose()
        again = channel.receive()
        heard = asyncio.ensure_future(anext(again))
        # Said once the new stream is open: until then the old one, closed
        # but not yet found so, would take it.
        deadline = time.monotonic() + 15
        while signal_cli.streams(daemon) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        signal_cli.says(daemon, "two")
        assert (await asyncio.wait_for(heard, 15)).text == "two"
        await again.aclose()
    finally:
        await channel.stop()


class _Squatter(http.server.BaseHTTPRequestHandler):
    """Another program on signal-cli's port, answering every call as
    signal-cli would and keeping the methods it was asked."""

    asked: list[str] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.asked.append(body["method"])
        answer = json.dumps({"jsonrpc": "2.0", "id": body["id"],
                             "result": {"version": "0.13.20"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


async def test_a_port_taken_before_signal_cli_binds_it_is_asked_nothing_but_the_version(
        daemon, monkeypatch):
    """The port is free when the channel chooses it, and signal-cli's only
    once Java has started, seconds later. Another Windows account's program
    that took it in between answers `version` as easily, and would then be
    sent the user's messages and report their answers to cards (step 11's
    audit). Whose the listener is decides, and signal-cli is stopped."""
    monkeypatch.setattr(_Squatter, "asked", [])
    squatter = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Squatter)
    threading.Thread(target=squatter.serve_forever, daemon=True).start()
    monkeypatch.setattr(signal, "_free_port", lambda: squatter.server_address[1])
    channel = signal.SignalChannel()
    try:
        with pytest.raises(RuntimeError, match="served by another program"):
            await channel.start(timeout=30)
        assert set(_Squatter.asked) == {"version"}
        assert channel.proc is None
    finally:
        await channel.stop()
        squatter.shutdown()
        squatter.server_close()


def test_the_users_words_never_take_the_timestamp_of_a_reply(tmp_path):
    """The channel drops a message stamped as one of its replies, as its own
    echo: a fake that gave the user's words a reply's timestamp would lose
    them, and the scenario would wait for an answer that never comes."""
    now = int(time.time() * 1000)
    replies = [{"method": "send", "params": {}, "timestamp": now + n} for n in range(5000)]
    (tmp_path / "sent.jsonl").write_text(
        "".join(json.dumps(reply) + "\n" for reply in replies), encoding="utf-8", newline="\n")

    assert signal_cli.says(tmp_path, "hello") == now + 5000
