"""A module's end of the broker (steve_sdk/link.py).

A module's code is written against `ctx`, and tested with a fake one; in
the sandbox the same calls must become the broker's messages and the
broker's errors the SDK's own again, or code that passed its tests fails
there, or catches the wrong thing.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
from pathlib import Path

import pytest

from steve_sdk.context import Account
from steve_sdk.errors import ConnectorError, SignInNeeded, Transient
from steve_sdk.link import (
    BAD_PARAMS,
    FAILED,
    INTERNAL,
    NO_METHOD,
    REFUSED,
    Broken,
    Link,
    SandboxContext,
    SandboxHttp,
    error_for,
    error_of,
    line,
)


def _lines(*messages):
    return io.BytesIO(b"".join(line({"jsonrpc": "2.0", **m}) for m in messages))


def _written(out):
    return [json.loads(raw) for raw in out.getvalue().splitlines()]


class Calls:
    """A link that keeps each call and answers with `result`."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def call(self, method, params):
        self.calls.append((method, params))
        return self.result

    def notify(self, method, params):
        self.calls.append((method, params))


def test_a_call_from_core_that_comes_while_the_module_waits_is_held_until_it_serves():
    """Core may call the module at any moment, as when it calls a tool
    while the module is still starting up and fetching; taken for the
    module's own answer, it would break the link."""
    out = io.BytesIO()
    link = Link(_lines({"id": 1, "method": "double", "params": {"n": 21}},
                       {"id": 1, "result": "fetched"}), out)
    assert link.call("http.fetch", {"url": "https://api.example/"}) == "fetched"
    link.serve({"double": lambda params: params["n"] * 2})
    sent, answered = _written(out)
    assert sent["method"] == "http.fetch" and sent["id"] == 1
    assert answered == {"jsonrpc": "2.0", "id": 1, "result": 42}


@pytest.mark.parametrize("code, data, raised", [
    (REFUSED, None, PermissionError),
    (FAILED, {"kind": "sign_in"}, SignInNeeded),
    (FAILED, {"kind": "transient"}, Transient),
    (FAILED, {"kind": "no such kind"}, ConnectorError),
    (FAILED, "not an object", ConnectorError),
    (BAD_PARAMS, None, ValueError),
    (NO_METHOD, None, NotImplementedError),
    (INTERNAL, None, ConnectorError),
])
def test_each_error_the_broker_answers_with_is_raised_as_the_sdks_own(code, data, raised):
    """A module retries a Transient and asks for sign-in on SignInNeeded;
    the same failure must raise the same class in the sandbox as in its
    tests."""
    error = {"code": code, "message": "said", **({"data": data} if data else {})}
    link = Link(_lines({"id": 1, "error": error}), io.BytesIO())
    with pytest.raises(raised, match="said") as caught:
        link.call("http.fetch", {})
    assert type(caught.value) is raised


def test_an_sdk_error_raised_in_a_module_reaches_core_as_itself():
    for exc in (SignInNeeded("lapsed"), Transient("later"), PermissionError("no")):
        again = error_of(*error_for(exc))
        assert type(again) is type(exc) and str(again) == str(exc)


def test_a_line_that_is_not_the_protocol_breaks_the_link_rather_than_being_guessed_at():
    for raw in (b"not json\n", b'{"id": 1, "result": 2}\n', b'{"jsonrpc": "2.0", "id": 9, '
                b'"result": 2}\n', b""):
        with pytest.raises(Broken):
            Link(io.BytesIO(raw), io.BytesIO()).call("log", {})


def test_a_request_from_the_sandbox_carries_its_query_json_and_body_as_the_broker_takes_them():
    link = Calls({"status": 201, "url": "https://api.example/x", "headers": {"a": "b"},
                  "content": base64.b64encode(b'{"ok": true}').decode()})
    answer = SandboxHttp(link).request("POST", "https://api.example/x?a=1",
                                       params={"b": "2 3"}, json={"k": 1}, timeout=5)
    (method, params), = link.calls
    assert method == "http.fetch"
    assert params == {"method": "POST", "url": "https://api.example/x?a=1&b=2+3",
                      "headers": {"Content-Type": "application/json"},
                      "content": base64.b64encode(b'{"k": 1}').decode(), "timeout": 5,
                      "follow": False}
    assert answer.status == 201 and answer.json() == {"ok": True}


def test_in_the_sandbox_a_module_is_never_handed_a_secret_and_told_how_to_use_one():
    ctx = SandboxContext(Calls(), account=Account(key="acct0001", name="Mine", module="m"),
                         state_dir=Path("."), zone="UTC")
    with pytest.raises(PermissionError, match=r"\{\{secret:token\}\}"):
        ctx.secret("token")


def test_a_read_sends_its_times_with_their_offsets():
    link = Calls([])
    ctx = SandboxContext(link, account=Account(key="acct0001", name="Mine", module="m"),
                         state_dir=Path("."), zone="Europe/Paris")
    since = dt.datetime(2026, 10, 15, 9, tzinfo=ctx.zone)
    ctx.read("mail", "search", query="rent", since=since)
    ctx.read("mail", "search", account="Work")
    assert link.calls == [
        ("connector.read", {"class": "mail", "method": "search",
                            "args": {"query": "rent", "since": "2026-10-15T09:00:00+02:00"}}),
        ("connector.read", {"class": "mail", "method": "search", "args": {},
                            "account": "Work"})]
