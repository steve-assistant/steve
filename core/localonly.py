"""Only this machine's own clients, holding this start's token, may drive the
agent over localhost.

The supervisor on its port, and every worker on its own, answer with the
user's mail, calendar, memory and approvals behind them. Listening on
localhost keeps the network out; it does not keep out the browser the user is
using, or any other program on this machine, and that is the gap this closes.

**CORS was never the protection it looked like.** It stops a page *reading*
a cross-origin response. It does not stop the request: a `text/plain` POST
is never preflighted, so any page the user visited could POST to
`/actions/decide`, `/signal/send` or `/_sup/swap`, and it would run. And
CORS does not apply to websockets at all -- any page could open the
supervisor's `/ws`, chat with the agent as them ("what are my newest
emails?") and read every answer back. Workers were exposed the same way on
their own ports, bypassing the supervisor entirely.

Three checks, on every HTTP request and every websocket handshake:

- **Origin.** Browsers attach it to every cross-origin request and every
  websocket handshake, and a page cannot forge it. Present and not one of
  the app's own origins: refused.
- **Host.** DNS rebinding points an attacker's domain at 127.0.0.1, making
  the page same-origin with itself; its requests then carry the attacker's
  hostname in Host. Anything but a loopback name: refused.
- **The token** (core/localapi.py). Until 13 October 2026 a request with no
  Origin was let through, on the reasoning that a program already running
  as the user could read the keyring anyway. But any user's programs reach
  127.0.0.1, not only this user's, and a program that took the fixed port
  first was sent everything the app said. So every request carries the
  token this start made, which only this user can read.

Two requests pass without the token, and neither can reach any other
route: the proof, which a caller asks for before it sends the token
anywhere, and whose own route (`supervisor.sup_prove`) answers the proof
and nothing else; and a CORS preflight, which browsers send without
credentials and the CORS middleware answers with headers alone.
tests/gate/test_local_token.py sends a request without the token to every
route of the supervisor and a worker.
"""

from __future__ import annotations

import logging

from core import localapi

log = logging.getLogger("localonly")

# The app's own origins: the packaged Tauri app on Windows, and the Vite
# dev server while working on the UI. Explicitly never "*".
ALLOWED_ORIGINS = frozenset({
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
})

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _hostname(host: str) -> str:
    """"127.0.0.1:8080" -> "127.0.0.1", "[::1]:8080" -> "[::1]"."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def refusal(headers: dict[str, str]) -> str | None:
    """Why this request may not reach the agent from where it came, or None
    if it may: the Origin and Host checks.

    `headers` with lower-cased names, as ASGI delivers them.
    """
    origin = headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        return f"origin {origin!r} is not allowed"
    host = headers.get("host")
    if host is not None and _hostname(host) not in LOOPBACK_HOSTS:
        return f"host {host!r} is not this machine"
    return None


def needs_token(scope, headers: dict[str, str]) -> bool:
    """Every request but the proof and a CORS preflight."""
    if scope["type"] != "http":
        return True
    method, path = scope.get("method", ""), scope.get("path", "")
    if (method, path) == (localapi.PROVE_METHOD, localapi.PROVE_PATH):
        return False
    preflight = (method == "OPTIONS" and "origin" in headers
                 and "access-control-request-method" in headers)
    return not preflight


class LocalOnly:
    """ASGI middleware applying `refusal` and the token to HTTP and websocket
    scopes.

    Outermost, so nothing -- not CORS, not routing, not a proxy to a worker
    -- sees a request it refuses.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers") or []
        }
        status, reason = 403, refusal(headers)
        if reason is None and needs_token(scope, headers) and not localapi.authorised(headers):
            status, reason = 401, "this request carries no valid token"
        if reason is None:
            await self.app(scope, receive, _unstored(send) if scope["type"] == "http" else send)
            return
        # The reason, never the token.
        log.warning("refused %s %s: %s", scope["type"], scope.get("path", ""), reason)
        if scope["type"] == "websocket":
            # Closing before accepting refuses the handshake with a 403.
            await send({"type": "websocket.close", "code": 1008, "reason": reason})
            return
        body = reason.encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def _unstored(send):
    """`send`, with every response marked `no-store`, whatever its route
    said. What the backend answers is the user's own: a photo, a chat, a
    memory. The app's webview keeps a disk cache in its own folder, and a
    photo's `private, max-age=3600` let one deleted after a day stay there
    (step 11's audit). Here, the one middleware every server runs, no route
    can forget it."""

    async def sending(message) -> None:
        if message["type"] == "http.response.start":
            kept = [(k, v) for k, v in message.get("headers") or []
                    if k.lower() != b"cache-control"]
            message = {**message, "headers": [*kept, (b"cache-control", b"no-store")]}
        await send(message)

    return sending


def served_by(port: int, pid: int) -> bool:
    """Whether 127.0.0.1:`port` is served by the process `pid`, or by one of
    its own children, and by nothing else. BLOCKING: it reads the system's
    table of TCP sockets, a few milliseconds.

    The programs Steve starts that bind a port of their own (signal-cli, the
    local decision model) are told a port that was free when Steve chose it,
    and bind it seconds later, once they have started. Until then the port
    is anyone's. Another Windows account's program can read that table,
    process ids and all, see Steve's connects waiting on a port no one
    holds, bind it first and answer as the program would: a version is easy
    to fake. Then it is sent the user's messages, and answers in the
    program's name (step 11's audit). So before anything that matters is
    sent, this asks the table whose the listener is. A listener on every
    address takes 127.0.0.1's connections too, so it counts as well.
    """
    import psutil

    try:
        ours = {pid, *(child.pid for child in psutil.Process(pid).children(recursive=True))}
    except psutil.Error:
        return False
    listeners = [c.pid for c in psutil.net_connections(kind="tcp4")
                 if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port
                 and c.laddr.ip in ("127.0.0.1", "0.0.0.0")]
    return bool(listeners) and all(owner in ours for owner in listeners)
