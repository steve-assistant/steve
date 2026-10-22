"""The backend's door on this machine: a port chosen at each start, a token
every request must carry, and the run file that hands both to the app.

The supervisor listened on one fixed port, 8790, and answered any program on
this machine that reached it, short of a web page (core/localonly.py). So
two things were open. Another Windows account on the same PC, or a program
started before Steve, could take 8790 first, and the app would then send it
everything: the user's messages, their approvals, the logins it saves. And
any process of any user could drive the agent: POST `/actions/decide` and
answer a card as if it were the user at the app.

Now:

- The supervisor binds 127.0.0.1 on a port the system chooses, makes 32
  random bytes at each start, and writes both, with its pid, to
  `run_dir()/backend.json` (`publish`). The file's access list names the
  current user alone and inherits nothing from its folder.
- Every request to the supervisor or to a worker carries the token, checked
  in `LocalOnly` through `authorised`: `Authorization: Bearer`, or, on a
  websocket, whose browser API sends no headers, the subprotocols `steve`
  and `token.<hex>`. A worker reads the token from the run file when it
  starts; a process with none refuses everything.
- Before anything sends the token, the backend proves itself (`proof`,
  `proven`): an HMAC, under the token, of the caller's nonce and the port
  the backend listens on. A program squatting on the port a stale run file
  names has no token to answer with. One that relays the nonce to the real
  backend gets back the proof for the real backend's port, not its own.

`python -m core.localapi get /_sup/health` calls the backend this way, in
place of `curl http://127.0.0.1:8790/...`. A body is JSON, or `key=value`
words (`post /chat message=hello`), since Windows PowerShell 5.1 strips the
double quotes out of JSON it passes to a program.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import json
import os
import secrets
import ssl
import string
import sys
from dataclasses import dataclass
from pathlib import Path

from core import paths

RUN_FILE = "backend.json"

# The websocket subprotocols: the one the server answers with, and the
# prefix of the one that carries the token. A browser closes a socket whose
# server picks none of the protocols it offered, so the server picks
# `steve`, never the token.
SUBPROTOCOL = "steve"
TOKEN_PROTOCOL = "token."

# The one request that needs no token: the proof, which a caller asks for
# before it sends the token anywhere.
PROVE_METHOD, PROVE_PATH = "POST", "/_sup/prove"

# What a nonce may be: hex, long enough not to repeat, short enough to cap
# the work an unauthenticated caller can ask for.
NONCE_LENGTHS = range(32, 129)


class NotRunning(RuntimeError):
    """No run file: no backend has started, or the last one stopped."""


class NotProven(RuntimeError):
    """What answers on the run file's port could not prove it holds the token."""


@dataclass(frozen=True)
class Backend:
    port: int
    token: str
    pid: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @property
    def subprotocols(self) -> list[str]:
        return [SUBPROTOCOL, TOKEN_PROTOCOL + self.token]


def run_file() -> Path:
    return paths.run_dir() / RUN_FILE


# ---------------------------------------------------------------------------
# The run file
# ---------------------------------------------------------------------------


def publish(port: int) -> Backend:
    """Make this start's token and write the run file: port, token and pid.

    The file is created and restricted to the current user before the token
    is written into it, then moved into place, so there is no moment when
    the token is in a file anyone else may read."""
    backend = Backend(port=port, token=secrets.token_hex(32), pid=os.getpid())
    target = run_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    # The folder too: in a data folder another account may change (a source
    # tree's under C:\, say), it could rename this one and put its own run
    # file, with its own token, where the app looks (step 11's audit, C5).
    owner_only(target.parent)
    staging = target.with_name(f"{target.name}.{os.getpid()}.new")
    staging.unlink(missing_ok=True)
    staging.touch()
    owner_only(staging)
    staging.write_text(json.dumps({"port": port, "token": backend.token, "pid": backend.pid}),
                       encoding="utf-8", newline="\n")
    os.replace(staging, target)
    return backend


def read() -> Backend | None:
    """The run file's backend, or None if there is none or it is unreadable."""
    try:
        data = json.loads(run_file().read_text(encoding="utf-8"))
        return Backend(port=int(data["port"]), token=str(data["token"]), pid=int(data["pid"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def withdraw(backend: Backend) -> None:
    """Remove the run file as this backend stops, if it is still this
    start's: a later start's file is not this one's to remove."""
    current = read()
    if current is not None and current.token == backend.token:
        run_file().unlink(missing_ok=True)


def owner_only(path: Path) -> None:
    """Let the current user alone open `path`: one entry in its access list,
    and nothing inherited from its folder. A folder's entry is inherited by
    what is made in it."""
    if sys.platform != "win32":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    import ntsecuritycon
    import win32api
    import win32security

    process = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                             win32security.TOKEN_QUERY)
    user = win32security.GetTokenInformation(process, win32security.TokenUser)[0]
    dacl = win32security.ACL()
    inherited = (win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
                 if path.is_dir() else 0)
    dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION, inherited,
                               ntsecuritycon.FILE_ALL_ACCESS, user)
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, dacl, None)


# ---------------------------------------------------------------------------
# The server's side
# ---------------------------------------------------------------------------

# The token this process's routes require: the one the supervisor made, or
# the one a worker read from the run file as it started. None refuses every
# request.
_accepted: str | None = None


def accept(token: str | None) -> None:
    global _accepted
    _accepted = token


def accept_published() -> bool:
    """A worker's start: require the run file's token. False if there is none,
    and then every request is refused."""
    backend = read()
    accept(backend.token if backend else None)
    return backend is not None


def headers() -> dict[str, str]:
    """This process's token, as a header: for the supervisor's own calls to
    its workers."""
    return {"Authorization": f"Bearer {_accepted}"} if _accepted else {}


@functools.cache
def tls() -> ssl.SSLContext:
    """The TLS context of every client of the backend's own addresses.

    They speak plain HTTP to 127.0.0.1 and never use one, but httpx builds a
    context for each client it makes unless it is given one, and reads
    certifi's whole bundle to do it: 0.3 to 0.45 s a client on a laptop,
    against 0.1 ms with this one passed in (14 October 2026). A swap made
    three, and the gate's supervisor tests made dozens. Made once, on first
    use, and shared."""
    return ssl.create_default_context()


def presented(request_headers: dict[str, str]) -> str | None:
    """The token a request carries: a Bearer header, or a `token.`
    subprotocol. `request_headers` with lower-cased names, as ASGI gives them."""
    scheme, _, value = request_headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    for offered in request_headers.get("sec-websocket-protocol", "").split(","):
        if (offered := offered.strip()).startswith(TOKEN_PROTOCOL):
            return offered.removeprefix(TOKEN_PROTOCOL)
    return None


def authorised(request_headers: dict[str, str]) -> bool:
    """Whether the request carries this process's token, compared in
    constant time."""
    given = presented(request_headers)
    if not _accepted or given is None:
        return False
    return hmac.compare_digest(given.encode("utf-8"), _accepted.encode("utf-8"))


def subprotocol(scope) -> str | None:
    """What to accept a websocket with: `steve`, if the client offered it."""
    return SUBPROTOCOL if SUBPROTOCOL in (scope.get("subprotocols") or ()) else None


def fair_nonce(nonce: object) -> bool:
    return (isinstance(nonce, str) and len(nonce) in NONCE_LENGTHS
            and all(c in string.hexdigits for c in nonce))


def proof(token: str, nonce: str, port: int) -> str:
    """HMAC-SHA256, under the token, of the nonce and the port, in hex. The
    desktop app computes the same (ui/src-tauri/src/lib.rs, `proof`)."""
    return hmac.new(token.encode("utf-8"), f"{nonce}:{port}".encode(),
                    hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# A caller's side, for this machine's tools: the CLI below, and the worker's
# own requests to the supervisor (agent/control_plane.py)
# ---------------------------------------------------------------------------


def _request(url: str, method: str, body: dict | None, request_headers: dict[str, str],
             timeout: float) -> tuple[int, object]:
    import urllib.error
    import urllib.request

    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={**request_headers,
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def proven(timeout: float = 5.0) -> Backend:
    """The run file's backend, once it has proved it holds the token. Raises
    NotRunning or NotProven; nothing is sent the token before this returns."""
    backend = read()
    if backend is None:
        raise NotRunning(f"Steve's backend is not running (no {run_file()})")
    nonce = secrets.token_hex(32)
    try:
        status, answer = _request(backend.url + PROVE_PATH, PROVE_METHOD, {"nonce": nonce}, {},
                                  timeout)
    except OSError as exc:
        raise NotRunning(f"nothing answers on port {backend.port}: {exc}") from exc
    expected = proof(backend.token, nonce, backend.port)
    given = answer.get("proof") if isinstance(answer, dict) else None
    if status != 200 or not isinstance(given, str) or not hmac.compare_digest(
            given.encode("utf-8"), expected.encode("utf-8")):
        raise NotProven(f"what answers on port {backend.port} is not this Steve's backend")
    return backend


def call(method: str, path: str, body: dict | None = None, *,
         timeout: float = 30.0) -> tuple[int, object]:
    """Call the backend with its token, once it has proved itself: the status
    and the JSON (or the text) it answered."""
    backend = proven()
    return _request(backend.url + path, method.upper(), body, backend.headers, timeout)


def _body(words: list[str]) -> dict:
    """What to post: one JSON object, or `key=value` words."""
    if len(words) == 1 and words[0].lstrip().startswith("{"):
        return json.loads(words[0])
    pairs = [word.partition("=") for word in words]
    if any(not key or not sep for key, sep, _ in pairs):
        raise ValueError("a body is JSON, or key=value words")
    return {key: value for key, _, value in pairs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.localapi",
        description="Call this Steve's backend with its token, once it has proved itself.")
    parser.add_argument("method", choices=["get", "post"])
    parser.add_argument("path", help="for example /_sup/health or /status")
    parser.add_argument("body", nargs="*",
                        help="JSON to post, or key=value words: message=hello (default {})")
    args = parser.parse_args(argv)
    try:
        body = None if args.method == "get" else _body(args.body)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        status, answer = call(args.method, args.path, body)
    except (NotRunning, NotProven) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(json.dumps(answer, indent=1) if isinstance(answer, (dict, list)) else answer)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
