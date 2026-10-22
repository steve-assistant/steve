"""A stand-in for signal-cli, for tests that start it as a process.

Run as `signal_cli.py --config FOLDER [--output=json] COMMAND ...`, as
`channels/signal.py` runs the real one, through a launcher .bat named in
`STEVE_SIGNAL_CLI`. Standard library only: it runs by its path, outside
the test's process. Its helpers (`linked`, `says`, `reacts`, `sent`) are
the test's side of the folder they share.

It knows `link` and `listAccounts`, and does what the file `fake-mode` in
FOLDER says, which is what the real one was seen to do on 13 October 2026:

- `waits`: prints the link to scan and waits for a scan that never comes;
- `links`: prints the link, then writes the account and exits 0, as a
  completed scan does;
- `half`: prints the link, writes the account but leaves it unregistered,
  and exits 1, as a scan the phone did not finish does;
- `fails`: prints the link and exits 1 having written nothing;
- `refuses`: says why it cannot link, and exits 1;
- `silent`: says nothing and hangs.

`listAccounts` lists the account only once a link has completed, as the
real one hides a half-linked account (`linked` makes one so, for a test
that starts from a linked phone).

`-a ACCOUNT daemon --http HOST:PORT` serves the daemon's two doors, as
`channels/signal.py`'s `SignalChannel` uses them, for the end-to-end tests:
- `POST /api/v1/rpc`, JSON-RPC: `version` answers, and every other method
  (`send`, `sendReaction`, `sendTyping`, `sendReceipt`) is kept, one JSON
  line each, in `sent.jsonl` in FOLDER, and answered with a timestamp, as
  `send` is;
- `GET /api/v1/events`, server-sent events: one `receive` for each line
  the test appends to `inbox.jsonl` in FOLDER (`says` and `reacts` write
  one), each given once, to the newest stream; `streams` counts them, so
  a test knows the channel is listening.
And its troubles, set by the test through the folder: `fails` refuses the
next sends as a network failure does, kept in `tried.jsonl`; `dies` ends
the daemon as a crash does. What it gave is kept on disk, so the daemon
started after one dies gives only what is new.
Files rather than memory: the daemon is a process the worker starts, and
the test reaches it only through the folder they share.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NUMBER = "+15555550123"
LINK = "sgnl://linkdevice?uuid=a-fake-uuid&pub_key=a-fake-key"
NL = chr(10)


def linked(config: Path) -> None:
    """The account as a completed link leaves it."""
    (config / "data").mkdir(parents=True, exist_ok=True)
    (config / "data" / "registered").write_text("", encoding="utf-8")


def says(config: Path, text: str) -> int:
    """The user's Note to Self, as the daemon reports one: a sync message
    from the account to itself. Gives its timestamp, never one a reply was
    sent at: the channel drops a message with its own reply's timestamp as
    its echo (channels/signal.py `_read`)."""
    return _note(config, {"message": text})


def reacts(config: Path, emoji: str, to: int) -> int:
    """The user's reaction to the message the daemon sent at `to`. The
    daemon reports it as `targetSentTimestamp`, though a client sends it
    as `targetTimestamp` (channels/signal.py `_read`)."""
    return _note(config, {"reaction": {"emoji": emoji, "targetSentTimestamp": to,
                                       "isRemove": False}})


def _note(config: Path, message: dict) -> int:
    taken = {call["timestamp"] for call in sent(config)}
    stamp = int(time.time() * 1000)
    while stamp in taken:
        stamp += 1
    event = {"envelope": {"sourceNumber": NUMBER, "timestamp": stamp, "syncMessage": {
        "sentMessage": {"destinationNumber": NUMBER, "timestamp": stamp, **message}}}}
    with (config / "inbox.jsonl").open("a", encoding="utf-8") as inbox:
        inbox.write(json.dumps(event) + NL)
    return stamp


def sent(config: Path) -> list[dict]:
    """Every call the daemon was asked to make, but `version`, in order:
    its method, its params and the timestamp it was answered with."""
    kept = config / "sent.jsonl"
    lines = kept.read_text(encoding="utf-8").splitlines() if kept.is_file() else []
    return [json.loads(line) for line in lines]


def streams(config: Path) -> int:
    """How many event streams the daemon has opened: 1 once the channel
    listens, and one more each time it reconnects or a new daemon does."""
    return _number(config / "streams")


def tried(config: Path) -> list[dict]:
    """Every `send` the daemon refused (`fails`), in order."""
    kept = config / "tried.jsonl"
    lines = kept.read_text(encoding="utf-8").splitlines() if kept.is_file() else []
    return [json.loads(line) for line in lines]


def fails(config: Path, sends: int) -> None:
    """The next `sends` sends are refused, as the network failing refuses them."""
    (config / "send-fails").write_text(str(sends), encoding="utf-8")


def dies(config: Path) -> None:
    """The daemon exits, as a crash does, the next time its stream ticks."""
    (config / "dies").write_text("1", encoding="utf-8")


def _number(path: Path) -> int:
    return int(path.read_text(encoding="utf-8") or 0) if path.is_file() else 0


def _count_off(path: Path) -> bool:
    """Whether a count of troubles still to come is above 0; if so, one less."""
    left = _number(path)
    if left > 0:
        path.write_text(str(left - 1), encoding="utf-8")
    return left > 0


def daemon(config: Path, address: str) -> int:
    host, port = address.rsplit(":", 1)
    inbox, outbox = config / "inbox.jsonl", config / "sent.jsonl"
    stamp = [int(time.time() * 1000)]
    delivered = [_number(config / "delivered")]
    # Counted on from the daemon before, so a test sees a restarted one listen.
    streams = [_number(config / "streams")]
    lock = threading.Lock()

    class Daemon(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method, params = body.get("method"), body.get("params") or {}
            answer = {"jsonrpc": "2.0", "id": body.get("id")}
            if method == "version":
                answer["result"] = {"version": "0.13.99-fake"}
            elif method == "send" and _count_off(config / "send-fails"):
                # Kept as tried, so the test sees every attempt.
                with lock, (config / "tried.jsonl").open("a", encoding="utf-8") as kept:
                    kept.write(json.dumps({"method": method, "params": params}) + NL)
                answer["error"] = {"code": -1, "message": "Failed to send message:"
                                   " Network failure (java.net.SocketTimeoutException)"}
            else:
                with lock:
                    stamp[0] = max(stamp[0] + 1, int(time.time() * 1000))
                    with outbox.open("a", encoding="utf-8") as kept:
                        kept.write(json.dumps({"method": method, "params": params,
                                               "timestamp": stamp[0]}) + NL)
                    answer["result"] = {"timestamp": stamp[0]}
            out = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            with lock:
                streams[0] += 1
                mine = streams[0]
                (config / "streams").write_text(str(mine), encoding="utf-8")
            while True:
                # A comment line, which a reader skips: writing it is how a
                # stream its reader has closed is found, and ends.
                self.wfile.write(f":{NL}{NL}".encode())
                self.wfile.flush()
                # Each event once, to the newest stream only: the channel
                # reconnects on its own, and a stream that began at the
                # inbox's first line would hand it everything again. Whole
                # lines only, in case one is still being written.
                with lock:
                    if mine != streams[0]:
                        return
                    if _count_off(config / "dies"):
                        # As a crash: no goodbye, the stream cut mid-way.
                        os._exit(1)
                    text = inbox.read_text(encoding="utf-8") if inbox.exists() else ""
                    lines = text.split(NL)[:-1]
                    new, delivered[0] = lines[delivered[0]:], len(lines)
                    # Kept on disk: a daemon started after this one dies
                    # gives only what this one had not, as the real one's
                    # server holds a message until a device takes it.
                    (config / "delivered").write_text(str(delivered[0]), encoding="utf-8")
                for line in new:
                    self.wfile.write(f"event:receive{NL}data:{line}{NL}{NL}".encode())
                    self.wfile.flush()
                time.sleep(0.1)

    server = ThreadingHTTPServer((host, int(port)), Daemon)
    server.daemon_threads = True
    server.serve_forever()
    return 0


def main(argv: list[str]) -> int:
    at = argv.index("--config")
    config = Path(argv[at + 1])
    rest = argv[at + 2:]
    if rest[:1] == ["-a"]:
        rest = rest[2:]
    if rest[:1] == ["daemon"]:
        return daemon(config, rest[rest.index("--http") + 1])
    command = [arg for arg in rest if not arg.startswith("--")]
    mode_file = config / "fake-mode"
    mode = mode_file.read_text(encoding="utf-8").strip() if mode_file.is_file() else "waits"
    data = config / "data"

    if command[:1] == ["listAccounts"]:
        print(json.dumps([{"number": NUMBER}] if (data / "registered").is_file() else []))
        return 0
    if command[:1] != ["link"]:
        print(f"fake signal-cli knows no {command}", file=sys.stderr)
        return 2

    if mode == "silent":
        time.sleep(120)
        return 1
    if mode == "refuses":
        print("INFO  SignalAccount - Config file is in use by another instance, waiting...")
        print("Error: Config file is in use by another instance.")
        return 1
    print(LINK, flush=True)
    if mode == "waits":
        time.sleep(120)
        return 1
    time.sleep(0.2)
    if mode in ("links", "half"):
        data.mkdir(parents=True, exist_ok=True)
        (data / "accounts.json").write_text(
            json.dumps({"accounts": [{"number": NUMBER, "path": "1"}], "version": 2}),
            encoding="utf-8")
    if mode == "links":
        (data / "registered").write_text("", encoding="utf-8")
        print(f"Associated with: {NUMBER}")
        return 0
    print("Link request error: Connection closed!")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
