"""What the doctor (`python -m core.browser check`) runs inside a live
browser container, to prove the sandbox holds from the side a page is on.

Each probe tries something a hostile page would, and prints what happened
as JSON: a lookup of an outside name (must fail: the container has no DNS
but the gate's), a direct connection to a public address and to the laptop
(must fail: the network has no route), and the gate asked to reach
somewhere private (must be refused) and somewhere public (must work).
Standard library only. Frozen, with the rest of `core/`.
"""

from __future__ import annotations

import json
import socket
import sys

GATE_PORT = 3128


def _lookup(name: str) -> dict:
    try:
        socket.getaddrinfo(name, 443)
        return {"reached": True}
    except OSError as exc:
        return {"reached": False, "detail": type(exc).__name__}


def _direct(host: str, port: int) -> dict:
    try:
        with socket.create_connection((host, port), timeout=5):
            return {"reached": True}
    except OSError as exc:
        return {"reached": False, "detail": type(exc).__name__}


def _through_gate(gate: str, target: str) -> dict:
    try:
        with socket.create_connection((gate, GATE_PORT), timeout=10) as s:
            s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
            s.settimeout(20)
            status = s.recv(200).split(b"\r\n", 1)[0].decode("latin-1", "replace")
        return {"reached": " 200 " in f"{status} ", "detail": status}
    except OSError as exc:
        return {"reached": False, "detail": type(exc).__name__}


def main(gate: str, backend_port: int | None = None) -> list[dict]:
    """`backend_port`: the port the laptop's supervisor chose at this start,
    when it is running (core/localapi.py)."""
    blocked = [
        ("an outside name resolves", lambda: _lookup("example.com")),
        ("a public address, directly", lambda: _direct("1.1.1.1", 443)),
    ]
    refused = ["127.0.0.1:443", "10.0.0.1:443", "192.168.1.1:443", "169.254.169.254:80",
               "localhost:443", "example.com:22"]
    if backend_port:
        blocked += [
            ("the laptop's supervisor, directly",
             lambda: _direct("host.docker.internal", backend_port)),
            ("the Docker host gateway, directly", lambda: _direct("172.17.0.1", backend_port)),
        ]
        refused.append(f"host.docker.internal:{backend_port}")
    results = []
    for name, probe in blocked:
        results.append({"probe": name, "expect": "blocked", **probe()})
    for target in refused:
        results.append({"probe": f"the gate, to {target}", "expect": "blocked",
                        **_through_gate(gate, target)})
    results.append({"probe": "the gate, to example.com:443", "expect": "open",
                    **_through_gate(gate, "example.com:443")})
    for r in results:
        r["pass"] = r["reached"] == (r["expect"] == "open")
    return results


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)))
