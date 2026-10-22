"""The MCP client over a server's standard input and output.

A server Steve starts in its container speaks to Steve on stdio, a JSON
message a line (`core/engines/mcp.Stdio`). Steve may have several calls
waiting on one server; each reply must reach the call it answers, a
request of the server's own must be refused rather than served, and a
server that stops or stays silent must fail the call it holds, not hang
the turn that made it.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable

import pytest

from core import lifecycle
from core.engines import mcp
from steve_sdk.errors import ConnectorError, Transient


class _Server:
    """The other end of the pipes: answers each request as `answer` says,
    in the order `answer` chooses, and records what Steve sent."""

    def __init__(self, answer: Callable[[_Server, dict], None]) -> None:
        to_server, self._steve_writes = os.pipe()
        self._steve_reads, from_server = os.pipe()
        self.stdin = open(self._steve_writes, "wb", buffering=0)  # noqa: SIM115
        self.stdout = open(self._steve_reads, "rb")  # noqa: SIM115
        self._in = open(to_server, "rb")  # noqa: SIM115
        self._out = open(from_server, "wb", buffering=0)  # noqa: SIM115
        self.sent: list[dict] = []
        self.held: list[dict] = []
        self._answer = answer
        threading.Thread(target=self._serve, daemon=True).start()

    def say(self, message: dict | str) -> None:
        line = message if isinstance(message, str) else json.dumps(message)
        self._out.write(line.encode() + b"\n")

    def reply(self, request: dict, result: dict) -> None:
        self.say({"jsonrpc": "2.0", "id": request["id"], "result": result})

    def _serve(self) -> None:
        for line in self._in:
            message = json.loads(line)
            self.sent.append(message)
            self._answer(self, message)

    def end(self) -> None:
        self._out.close()


def _ordinary(server: _Server, message: dict) -> None:
    if message.get("method") == "initialize":
        server.reply(message, {"protocolVersion": mcp.PROTOCOLS[0],
                               "serverInfo": {"name": "notes", "version": "1.0"}})
    elif message.get("method") == "tools/list":
        server.reply(message, {"tools": [{"name": "get", "inputSchema": {}}]})


def test_a_server_on_stdio_is_opened_and_each_reply_reaches_the_call_it_answers():
    """Two calls wait at once; the server answers the second first, with a
    line that is not a message and a notification between. Matched by id,
    each call gets its own answer."""
    def answer(server: _Server, message: dict) -> None:
        if message.get("method") == "tools/call":
            server.held.append(message)
            if len(server.held) == 2:
                server.say("not json at all")
                server.say({"jsonrpc": "2.0", "method": "notifications/progress"})
                for request in reversed(server.held):
                    server.reply(request, {"content": [
                        {"type": "text", "text": request["params"]["arguments"]["say"]}]})
        else:
            _ordinary(server, message)

    server = _Server(answer)
    client = mcp.Stdio(server.stdin, server.stdout, timeout=10)

    assert client.open() == {"name": "notes", "version": "1.0"}
    assert [t["name"] for t in client.tools()] == ["get"]
    got: dict[str, dict] = {}
    calls = [threading.Thread(target=lambda word=word: got.__setitem__(
        word, client.call("get", {"say": word}))) for word in ("first", "second")]
    for call in calls:
        call.start()
    for call in calls:
        call.join(10)

    assert {word: r["content"][0]["text"] for word, r in got.items()} == {
        "first": "first", "second": "second"}
    assert {"jsonrpc": "2.0", "method": "notifications/initialized"} in server.sent
    server.end()


def test_a_servers_own_request_is_refused_so_it_cannot_ask_steves_model_anything():
    """Sampling would let a server write a prompt to Steve's model, and
    roots or elicitation would ask for files or for the user; all are off
    (06-modules.md, section 7). Each is answered that Steve does not offer
    it, and the call under way still gets its reply."""
    def answer(server: _Server, message: dict) -> None:
        if message.get("method") == "tools/call":
            server.say({"jsonrpc": "2.0", "id": "s-1", "method": "sampling/createMessage",
                        "params": {"messages": []}})
            server.reply(message, {"content": []})
        else:
            _ordinary(server, message)

    server = _Server(answer)
    client = mcp.Stdio(server.stdin, server.stdout, timeout=10)
    client.open()

    assert client.call("get", {}) == {"content": []}
    refusal = next(m for m in _waited(server, lambda m: m.get("id") == "s-1"))
    assert refusal["error"]["code"] == -32601 and "result" not in refusal
    server.end()


def test_a_reply_whose_id_is_not_a_number_is_passed_over_and_the_call_still_answered():
    """`"id": [1]` raised a TypeError in the reading thread, which ended it:
    the server read as stopped to every call from then on, the one under
    way included."""
    def answer(server: _Server, message: dict) -> None:
        if message.get("method") == "tools/call":
            server.say({"jsonrpc": "2.0", "id": [message["id"]], "result": {}})
            server.say({"jsonrpc": "2.0", "id": {"n": message["id"]}, "error": {}})
            server.reply(message, {"content": []})
        else:
            _ordinary(server, message)

    server = _Server(answer)
    client = mcp.Stdio(server.stdin, server.stdout, timeout=10)
    client.open()

    assert client.call("get", {}) == {"content": []}
    server.end()


def test_a_servers_refusal_is_said_without_a_token_it_quoted():
    """A server's error goes on to the model; its own tokens, which Steve
    never knew, are taken out by their shape."""
    with pytest.raises(ConnectorError) as raised:
        mcp.Stdio._result("tools/call", {"error": {
            "message": "expired: Bearer canaryTOKEN12345"}})

    assert "canary" not in str(raised.value) and "[secret]" in str(raised.value)


def _waited(server: _Server, wanted: Callable[[dict], bool]) -> list[dict]:
    for _ in range(200):
        if found := [m for m in server.sent if wanted(m)]:
            return found
        threading.Event().wait(0.02)
    return []


def test_a_server_that_stays_silent_or_stops_fails_the_call_it_holds_as_transient():
    """A turn waiting on a server must end: a silent server fails the call
    at its timeout, and one that has stopped fails each call at once."""
    def silent(server: _Server, message: dict) -> None:
        if message.get("method") != "tools/call":
            _ordinary(server, message)

    server = _Server(silent)
    client = mcp.Stdio(server.stdin, server.stdout, timeout=0.3)
    client.open()

    with pytest.raises(Transient, match="did not answer"):
        client.call("get", {})
    server.end()
    with pytest.raises(Transient, match="stopped"):
        for _ in range(50):
            client.call("get", {})


def test_a_server_steve_runs_that_stops_or_stays_silent_counts_against_its_watch():
    """A remote server out of reach is the network's, and counts against
    no module; a server Steve started is its own process, so one that
    hangs is a timeout and one that dies a crash, and enough of them turn
    the module off (06-modules.md, section 7)."""
    def silent(server: _Server, message: dict) -> None:
        if message.get("method") != "tools/call":
            _ordinary(server, message)

    server = _Server(silent)
    client = mcp.Stdio(server.stdin, server.stdout, timeout=0.3)
    client.open()

    with pytest.raises(Transient) as hung:
        client.call("get", {})
    server.end()
    with pytest.raises(Transient) as died:
        for _ in range(50):
            client.call("get", {})

    assert lifecycle.outcome(hung.value) == "timeout"
    assert lifecycle.outcome(died.value) == "crash"
    assert lifecycle.outcome(Transient("no answer from the network")) == "excused"


def test_a_server_that_refuses_a_call_says_so_as_the_servers_refusal():
    def refusing(server: _Server, message: dict) -> None:
        if message.get("method") == "tools/call":
            server.say({"jsonrpc": "2.0", "id": message["id"],
                        "error": {"code": -32602, "message": "no such note"}})
        else:
            _ordinary(server, message)

    server = _Server(refusing)
    client = mcp.Stdio(server.stdin, server.stdout, timeout=10)
    client.open()

    with pytest.raises(ConnectorError, match="no such note"):
        client.call("get", {})
    server.end()


def test_a_message_past_the_cap_ends_the_session_rather_than_filling_memory(monkeypatch):
    """A server is held to its job's memory, but Steve reads its lines in
    the worker's own; one line without end would take the worker's."""
    monkeypatch.setattr(mcp, "MAX_LINE", 1024)

    def flooding(server: _Server, message: dict) -> None:
        if message.get("method") == "tools/call":
            server.say(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                   "result": {"text": "x" * 4096}}))
        else:
            _ordinary(server, message)

    server = _Server(flooding)
    client = mcp.Stdio(server.stdin, server.stdout, timeout=10)
    client.open()

    with pytest.raises(Transient, match="past"):
        client.call("get", {})
    server.end()
