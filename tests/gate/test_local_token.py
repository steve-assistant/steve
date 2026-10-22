"""Nothing reaches the backend without this start's token, and neither the
app nor the command line sends the token to a backend that has not proved
itself. The supervisor sends it to its own workers unproved, on ports it
holds from before each starts (tests/unit/test_worker_port.py).

See core/localapi.py. Until 13 October 2026 the supervisor listened on a
fixed port and let in any request without an Origin: any program of any
Windows user on the PC could answer a card through `/actions/decide`, and
one that took the port first was sent everything the app said. The check
is in one place, `LocalOnly`, so these tests send a request without the
token to every route and websocket the supervisor and a worker have, read
from the apps themselves: a route added later is in the list without anyone
adding it. The end-to-end checks against a running supervisor are in
test_zero_downtime.py.
"""

from __future__ import annotations

import http.server
import json
import re
import secrets
import sys
import threading

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core import localapi, localonly
from tests.gate.routes import apps, routes

TOKEN = "a" * 64
REFUSED = "this request carries no valid token"
# The one route that answers without the token (core/localapi.py).
EXEMPT = {(localapi.PROVE_METHOD, localapi.PROVE_PATH)}


def _concrete(path: str) -> str:
    """A route's path with each parameter filled in: `/sessions/{id:int}`
    -> `/sessions/1`, which every convertor accepts."""
    return re.sub(r"\{[^}]+\}", "1", path)


def _inventory():
    """Every (app, kind, method, path) the two apps answer."""
    found = []
    for name, route in routes():
        if isinstance(route, WebSocketRoute):
            found.append((name, "websocket", "GET", _concrete(route.path)))
        else:
            found.extend((name, "http", method, _concrete(route.path))
                         for method in sorted(route.methods))
    return found


INVENTORY = _inventory()


def _client(app, **headers) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False,
                      headers=headers)


def _ws(path: str) -> str:
    """A websocket URL on loopback: the test client's own default host,
    `testserver`, is refused by the Host check before the token is looked at."""
    return "ws://127.0.0.1" + path


def test_the_inventory_holds_both_apps_routes_and_their_websockets():
    """A list that came out empty or half-read would pass the test below
    while checking nothing."""
    names = {(app, kind) for app, kind, _, _ in INVENTORY}
    assert names == {("supervisor", "http"), ("supervisor", "websocket"),
                     ("worker", "http"), ("worker", "websocket")}
    assert len(INVENTORY) > 50


@pytest.mark.parametrize(("app_name", "kind", "method", "path"), INVENTORY,
                         ids=[f"{a}-{m}-{p}" for a, _, m, p in INVENTORY])
@pytest.mark.parametrize("token", [None, "0" * 64, ""], ids=["none", "wrong", "empty"])
def test_every_route_and_websocket_refuses_a_request_without_this_starts_token(
        app_name, kind, method, path, token):
    """The routes answer with the user's mail, calendar, memory and
    approvals. The refusal comes from `LocalOnly`, before routing, so no
    endpoint runs: its body is the middleware's own."""
    if (method, path) in EXEMPT:
        pytest.skip("the proof, which answers without the token (tested below)")
    localapi.accept(TOKEN)
    app = dict(apps())[app_name]
    headers = {} if token is None else {"authorization": f"Bearer {token}"}
    client = _client(app, **headers)

    if kind == "websocket":
        offered = ["steve"] + ([] if token is None else [f"token.{token}"])
        with (pytest.raises(WebSocketDisconnect) as refused,
              client.websocket_connect(_ws(path), subprotocols=offered)):
            pass
        assert refused.value.code == 1008 and refused.value.reason == REFUSED
        return
    answer = client.request(method, path)
    assert answer.status_code == 401, f"{method} {path} answered {answer.status_code}"
    if method != "HEAD":
        assert answer.text == REFUSED


def test_nothing_a_server_answers_may_be_kept_in_the_apps_cache():
    """What the backend answers is the user's own. The app's webview keeps
    a disk cache in its own folder, and a photo's `private, max-age=3600`
    let one deleted after a day stay there. Whatever a route says, the
    middleware every server runs says `no-store`."""
    async def photo(request):
        return Response(b"jpeg", media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})

    localapi.accept(TOKEN)
    app = Starlette(routes=[Route("/control/photo", photo)],
                    middleware=[Middleware(localonly.LocalOnly)])

    answer = _client(app, authorization=f"Bearer {TOKEN}").get("/control/photo")

    assert answer.status_code == 200 and answer.content == b"jpeg"
    assert answer.headers.get_list("cache-control") == ["no-store"]


def test_only_the_proof_is_exempt_and_only_by_its_exact_method_and_path():
    """The supervisor's catch-all proxy answers any path, so an exemption
    by path alone would let `GET /_sup/prove` through to a worker."""
    from core import supervisor

    localapi.accept(TOKEN)
    client = _client(supervisor.app)

    for method, path in [("GET", "/_sup/prove"), ("POST", "/_sup/prove/"),
                         ("POST", "/_sup/PROVE"), ("PUT", "/_sup/prove"),
                         ("POST", "/_sup/prove/x")]:
        assert client.request(method, path).status_code == 401, (method, path)
    assert client.post(localapi.PROVE_PATH, json={"nonce": "0" * 64}).status_code != 401


def test_the_proof_answers_for_this_start_and_this_port(monkeypatch):
    from core import supervisor

    backend = localapi.Backend(port=51234, token=TOKEN, pid=1)
    monkeypatch.setattr(supervisor.state, "backend", backend)
    client = _client(supervisor.app)
    nonce = secrets.token_hex(32)

    answered = client.post(localapi.PROVE_PATH, json={"nonce": nonce})

    assert answered.json() == {"proof": localapi.proof(TOKEN, nonce, 51234)}
    for unfair in ["x", "zz" * 32, "0" * 31, "0" * 129, 7, None]:
        assert client.post(localapi.PROVE_PATH, json={"nonce": unfair}).status_code == 400


def test_the_proof_is_the_one_the_desktop_app_computes():
    """The same vector as `proof_binds_the_port` in ui/src-tauri/src/lib.rs:
    if the two sides ever differed, the app would refuse every backend."""
    nonce = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

    assert localapi.proof("a" * 64, nonce, 51234) == (
        "90d161ef95c0e7706f8794584db2e69d7d0e0658fbb8af07662371a0b9439725")
    assert localapi.proof("a" * 64, nonce, 51235) != localapi.proof("a" * 64, nonce, 51234)


def test_a_cors_preflight_passes_without_the_token_but_reaches_no_endpoint():
    """Browsers send a preflight with no credentials, so it cannot carry the
    token. On the supervisor the CORS middleware answers it with headers
    alone; a worker has no route that takes OPTIONS, so the router turns
    it away."""
    localapi.accept(TOKEN)
    preflight = {"origin": "tauri://localhost", "access-control-request-method": "POST",
                 "access-control-request-headers": "authorization, content-type"}
    for app_name, kind, method, path in INVENTORY:
        if kind != "http" or method == "OPTIONS":
            continue
        app = dict(apps())[app_name]
        answer = _client(app).options(path, headers=preflight)
        if app_name == "supervisor":
            assert answer.status_code == 200 and answer.text == "OK", path
            assert answer.headers["access-control-allow-origin"] == "tauri://localhost"
        else:
            assert answer.status_code == 405, f"a worker's {path} took a preflight"


def test_a_process_that_accepted_no_token_refuses_everything():
    """A worker that found no run file as it started: it may not fall back
    to letting requests in."""
    from core import supervisor

    localapi.accept(None)
    for presented in ["", "None", TOKEN]:
        client = _client(supervisor.app, authorization=f"Bearer {presented}")
        assert client.get("/status").status_code == 401


def test_the_token_on_a_websocket_is_a_subprotocol_and_the_server_answers_steve():
    """A browser's websocket can send no header, so the app offers `steve`
    and `token.<hex>`. The server must pick one of them or the browser
    closes the socket, and must never pick the token's."""
    async def echo(websocket):
        await websocket.accept(subprotocol=localapi.subprotocol(websocket.scope))
        await websocket.send_text("hello")
        await websocket.close()

    app = Starlette(middleware=[Middleware(localonly.LocalOnly)],
                    routes=[WebSocketRoute("/ws", echo)])
    localapi.accept(TOKEN)

    with _client(app).websocket_connect(_ws("/ws"), subprotocols=["steve", f"token.{TOKEN}"]) as ws:
        assert ws.accepted_subprotocol == "steve"
        assert ws.receive_text() == "hello"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
def _the_users_alone(path) -> bool:
    """One entry in `path`'s access list, for this user, and none inherited."""
    import win32api
    import win32security

    descriptor = win32security.GetFileSecurity(str(path),
                                               win32security.DACL_SECURITY_INFORMATION)
    dacl = descriptor.GetSecurityDescriptorDacl()
    process = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                             win32security.TOKEN_QUERY)
    user = win32security.GetTokenInformation(process, win32security.TokenUser)[0]
    return (dacl.GetAceCount() == 1 and dacl.GetAce(0)[2] == user
            and bool(descriptor.GetSecurityDescriptorControl()[0]
                     & win32security.SE_DACL_PROTECTED))


def test_the_run_file_and_its_folder_are_the_current_users_alone():
    """The token is in it, and another Windows account on this PC may read
    the folder it is in. One entry, for this user, and none inherited. The
    folder too: where another account may change the data folder, it could
    rename the run folder and put its own file, and token, in its place
    (step 11's audit, C5)."""
    backend = localapi.publish(50000)
    path = localapi.run_file()

    assert _the_users_alone(path)
    assert _the_users_alone(path.parent)
    assert localapi.read() == backend
    assert not list(path.parent.glob("*.new")), "the staging file was left behind"


def test_the_passphrases_hash_and_the_hello_key_are_the_current_users_alone():
    """Another account that could read passphrase.json could guess at its
    hash offline, and one that could change hello.json could put its own key
    there (step 11's audit, C5)."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from core import hello, passphrase

    assert passphrase.choose("correct horse battery") is None
    public = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    der = public.public_bytes(serialization.Encoding.DER,
                              serialization.PublicFormat.SubjectPublicKeyInfo)
    assert hello.register(base64.b64encode(der).decode(), "correct horse battery") is None

    assert _the_users_alone(passphrase.path())
    assert _the_users_alone(hello.path())
    assert passphrase.check("correct horse battery") is None


def test_a_stopping_backend_leaves_a_later_starts_run_file():
    """Two starts overlapping (the app restarting the backend): the first
    one's exit must not remove the file that names the second."""
    first = localapi.publish(50001)
    second = localapi.publish(50002)

    localapi.withdraw(first)
    assert localapi.read() == second
    localapi.withdraw(second)
    assert localapi.read() is None


class _Squatter(http.server.BaseHTTPRequestHandler):
    """Whatever listens on the run file's port, recording what it is sent."""

    answer = staticmethod(lambda nonce: "")
    seen: list[dict] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        self._record(body)
        reply = b"{}"
        if self.path == localapi.PROVE_PATH:
            reply = json.dumps({"proof": self.answer(json.loads(body)["nonce"])}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def do_GET(self):
        self._record(b"")
        self.send_response(200)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def _record(self, body: bytes):
        type(self).seen.append({"path": self.path, "headers": dict(self.headers),
                                "body": body.decode()})

    def log_message(self, *args):
        pass


@pytest.fixture
def squatter():
    _Squatter.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Squatter)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("answer", ["nothing", "another_port", "another_token"])
def test_a_backend_that_fails_the_proof_is_never_sent_the_token(squatter, answer):
    """A program on the port a stale run file names, or one relaying the
    nonce to the real backend on its own port: it is asked for the proof,
    and sent nothing else."""
    port = squatter.server_address[1]
    backend = localapi.publish(port)
    _Squatter.answer = staticmethod({
        "nothing": lambda nonce: "",
        "another_port": lambda nonce: localapi.proof(backend.token, nonce, port + 1),
        "another_token": lambda nonce: localapi.proof("b" * 64, nonce, port),
    }[answer])

    with pytest.raises(localapi.NotProven):
        localapi.call("GET", "/status", timeout=5.0)

    assert [seen["path"] for seen in _Squatter.seen] == [localapi.PROVE_PATH]
    sent = json.dumps(_Squatter.seen)
    assert backend.token not in sent


def test_a_backend_that_proves_itself_is_sent_the_token(squatter):
    """The other half: without it, the test above would pass on a client
    that never sent the token to anyone."""
    port = squatter.server_address[1]
    backend = localapi.publish(port)
    _Squatter.answer = staticmethod(lambda nonce: localapi.proof(backend.token, nonce, port))

    status, _ = localapi.call("GET", "/status", timeout=5.0)

    assert status == 200
    assert [seen["path"] for seen in _Squatter.seen] == [localapi.PROVE_PATH, "/status"]
    assert _Squatter.seen[1]["headers"]["Authorization"] == f"Bearer {backend.token}"


@pytest.mark.parametrize("words", [["message=hello"], ['{"message": "hello"}']])
def test_the_command_line_posts_json_or_key_value_words(squatter, words):
    """`python -m core.localapi post /chat message=hello` is the README's
    hello: Windows PowerShell 5.1 strips the double quotes from JSON it
    passes to a program, so words are the form that survives it."""
    port = squatter.server_address[1]
    backend = localapi.publish(port)
    _Squatter.answer = staticmethod(lambda nonce: localapi.proof(backend.token, nonce, port))

    assert localapi.main(["post", "/chat", *words]) == 0

    assert json.loads(_Squatter.seen[-1]["body"]) == {"message": "hello"}
    with pytest.raises(SystemExit):
        localapi.main(["post", "/chat", "hello"])
