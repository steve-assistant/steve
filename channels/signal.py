"""Signal, via signal-cli linked as a secondary device.

Linked rather than registered with its own number: linking needs no second
phone number and no SMS verification, and it takes about a minute. The
trade-off is that the agent *is* the user on Signal rather than a separate
contact, so the conversation happens in **Note to Self** -- the one thread
where talking to yourself is the expected behaviour.

That choice has a consequence worth stating plainly: a linked device sees the
account's messages. This module only ever acts on Note to Self, and ignores
everything else, but the capability is there because Signal's linking model
grants it. Registering a separate number is the alternative if that ever
matters.

signal-cli runs as a daemon speaking JSON-RPC over HTTP with a server-sent
event stream for inbound messages. On Windows the unix-socket and DBus
transports are unavailable, so HTTP is not a preference but the only option.

signal-cli and the Java 25 it needs are add-ons (core/addons.py): downloaded
when the user chooses Signal, pinned by hash, and checked again before every
start (`unready`). The app links the device by showing the QR code
`start_link` makes, and unlinks it with `forget`.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx2

from channels import photos
from channels.base import KEEP_ATTACHMENTS_S, Capability, Channel, Incoming, Outgoing, delete_older
from channels.plaintext import shown
from core import addons, identity, localonly, paths

log = logging.getLogger("signal")

# How many sent messages to remember for matching reactions. A card is
# answered within the hour or not at all, and far fewer than this go out in
# an hour.
SENT_REMEMBERED = 200

# A signal-cli program to run instead of the add-on, exactly as named:
# a test's fake, or a developer's own `signal-cli.bat`. Not checked.
CLI_ENV = "STEVE_SIGNAL_CLI"
# Where signal-cli keeps its account state instead of the data folder.
DATA_ENV = "STEVE_SIGNAL_DATA"

# How long signal-cli may take to print the link to scan: it starts Java and
# asks Signal's servers for it first. And how long the phone has to scan it.
LINK_URI_WAIT_S = 60.0
LINK_WAIT_S = 300.0


def override() -> Path | None:
    """The program `STEVE_SIGNAL_CLI` names, in a source tree, or None for
    the add-on. An install honoured it too, so a program anything could
    name in the user's environment ran holding their Signal keys, with no
    hash checked (step 11's audit). It is a test's fake or a developer's
    own signal-cli, and neither is in an install."""
    if not paths.is_source_tree():
        return None
    named = os.environ.get(CLI_ENV, "").strip()
    return Path(named) if named else None


def signal_cli() -> Path:
    """Where the signal-cli program is: the one `STEVE_SIGNAL_CLI` names,
    or the add-on's home."""
    return override() or addons.home(addons.SIGNAL_CLI)


def java_home() -> Path:
    """The add-on's Java, which runs signal-cli."""
    return addons.home(addons.JAVA)


# Where signal-cli keeps its account state, in the data folder: the linked
# device's identity keys, which must persist.
def signal_data() -> Path:
    return Path(os.environ.get(DATA_ENV) or paths.data_dir() / "signal")


def attachment_dir() -> Path:
    return paths.data_dir() / "signal-attachments"


DEVICE_NAME = identity.NAME


class NotLinked(RuntimeError):
    """No Signal account is linked on this machine."""


class LinkFailed(RuntimeError):
    """signal-cli gave no link to scan, and why."""


class Unready(RuntimeError):
    """signal-cli may not be run: missing, or changed since it was
    installed (`unready` says which)."""


JAVA_OPTIONS = frozenset({"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS"})


def _env() -> dict:
    """The environment signal-cli runs in.

    On Windows, JAVA_HOME is the add-on's Java, for a signal-cli named by
    `STEVE_SIGNAL_CLI` whose launcher looks there: several are usually
    installed, and the default is too old. Everywhere else the environment
    is left alone: a container sets JAVA_HOME to the JRE it installed, and a
    normal Linux box has a working `java` on PATH. Overriding either with a
    Windows path produces "JAVA_HOME is set to an invalid directory" and a
    link attempt that fails with no URI.

    Java's own option variables are left out: Java reads each at every
    start, and each can load an agent into it (`-javaagent:`), code that
    would run holding the user's Signal keys past the add-on's hash.
    """
    env = {name: value for name, value in os.environ.items()
           if name.upper() not in JAVA_OPTIONS}
    if sys.platform == "win32":
        env["JAVA_HOME"] = str(java_home())
    return env


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _launch() -> list[str]:
    """The command that runs signal-cli, before its arguments.

    The add-on's Java runs it directly. signal-cli's own launcher, a .bat,
    names all eighty of its jars on one line, and under a home longer than
    about 65 characters -- `%LOCALAPPDATA%\\Programs\\Steve\\addons\\...`
    for most user names -- cmd refuses the line ("The input line is too
    long"), so every start would fail. Java reads them as `lib\\*` instead:
    no class is in two of them (checked on 13 October 2026), so their order
    does not matter. And the process is Java itself, not a cmd.exe with Java
    underneath it to be found and killed.
    """
    if (program := override()) is not None:
        return [str(program)]
    java = java_home() / "bin" / ("java.exe" if sys.platform == "win32" else "java")
    return [str(java), "--enable-native-access=ALL-UNNAMED",
            "-classpath", str(signal_cli() / "lib" / "*"), "org.asamk.signal.Main"]


def installed() -> bool:
    """Whether signal-cli is there to run: a file check, safe on the event
    loop. `unready` is the full check, before running it."""
    if (program := override()) is not None:
        return program.is_file()
    return (signal_cli() / "lib").is_dir() and java_home().is_dir()


def unready() -> str | None:
    """Why signal-cli may not be run now, or None if it may. BLOCKING: it
    hashes the add-ons' files, a second or two.

    A program `STEVE_SIGNAL_CLI` names is a test's fake or a developer's
    own, and only has to be there."""
    if (program := override()) is not None:
        return None if program.is_file() else f"signal-cli is not at {program}"
    for addon in (addons.SIGNAL_CLI, addons.JAVA):
        if (why := addons.verify(addon)) is not None:
            return why
    return None


def install(progress: Callable[[int, int], None] | None = None) -> None:
    """signal-cli and its Java, downloaded and checked (core/addons.py);
    `progress(bytes so far, of)` across both. BLOCKING, for minutes on a
    slow line. `addons.AddonError` if either cannot be."""
    total = addons.SIGNAL_CLI.size + addons.JAVA.size
    before = 0
    for addon in (addons.SIGNAL_CLI, addons.JAVA):
        def told(done: int, _of: int, before: int = before) -> None:
            if progress is not None:
                progress(before + done, total)

        addons.install(addon, progress=told)
        before += addon.size


def _cli(*args: str) -> list[str]:
    return [*_launch(), "--config", str(signal_data()), *args]


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a signal-cli process *and anything underneath it*.

    A signal-cli run through a launcher .bat (`STEVE_SIGNAL_CLI`) is cmd.exe
    with the JVM as a grandchild. `terminate()` reaps the shim and leaves
    the JVM running -- still holding the exclusive lock on the account
    directory. Every later start then blocks on that lock, which is how a
    stopped channel takes the *next* worker down with it.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=20,
            )
    proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)
    if proc.poll() is None:
        proc.kill()


def reap_daemons() -> list[int]:
    """Kill any signal-cli JVM holding *our* account directory.

    Only one process can hold the lock, and during a blue-green swap two
    workers briefly both want it. Newest wins: the old worker is draining, and
    a channel that cannot start is worse than one whose stream reconnects.

    Matched on the config path so this cannot touch a signal-cli the user runs
    for something else.
    """
    if sys.platform != "win32":
        return []
    needle = str(signal_data())
    try:
        listing = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=30,
        )
        entries = json.loads(listing.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    if isinstance(entries, dict):
        entries = [entries]

    killed = []
    for entry in entries:
        if needle not in (entry.get("CommandLine") or ""):
            continue
        pid = entry.get("ProcessId")
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
            killed.append(pid)
    return killed


def accounts() -> list[str]:
    """Which Signal accounts this installation is linked to.

    BLOCKING, and not boundedly so: this shells out to signal-cli, which waits
    on the account lock. With a daemon already running it does not return at
    all -- and `timeout=` does not rescue it, because killing a launcher
    leaves the JVM holding the pipes open. Never call this from the event
    loop; use `accounts_async()`.
    """
    if not installed():
        return []
    proc = None
    try:
        proc = subprocess.Popen(
            _cli("--output=json", "listAccounts"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=_env(),
        )
        stdout, _ = proc.communicate(timeout=30)
        if proc.returncode != 0:
            return []
        data = json.loads(stdout or "[]")
        return [entry["number"] for entry in data if entry.get("number")]
    except subprocess.TimeoutExpired:
        if proc is not None:
            _kill_tree(proc)
        return []
    except (OSError, ValueError):
        return []


def known_accounts() -> list[str]:
    """Accounts signal-cli has *state* for, registered or not.

    Read from accounts.json rather than asked of signal-cli, because
    `listAccounts` silently filters out anything not fully registered -- and
    that is precisely the state an interrupted link leaves behind. Telling
    those two apart is the difference between "scan it again" and "the QR
    was never scanned", which are different problems with different fixes.
    """
    path = signal_data() / "data" / "accounts.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        entry["number"]
        for entry in data.get("accounts") or []
        if entry.get("number")
    ]


async def accounts_async() -> list[str]:
    """`accounts()` off the event loop, where it belongs."""
    return await asyncio.to_thread(accounts)


# ---------------------------------------------------------------------------
# Linking, and forgetting
# ---------------------------------------------------------------------------


def start_link(name: str = DEVICE_NAME) -> tuple[str, subprocess.Popen]:
    """Start linking this machine as a device: the `sgnl://linkdevice`
    link for the phone to scan, and the signal-cli process waiting for it
    to (`finish_link`). BLOCKING, up to `LINK_URI_WAIT_S`; `LinkFailed` if
    no link came, `Unready` if signal-cli may not be run.

    signal-cli prints the link and then blocks until the phone completes the
    handshake, so it is read from the live stream rather than the finished
    output. A signal-cli that hangs before printing it is killed at the
    deadline, which ends the read.

    Nothing is written: the link is a credential for the account until it
    is scanned or expires. The personal edition saved it as a PNG and a text
    file in the data folder, deleted only if the attempt ran to its end.
    """
    if (why := unready()) is not None:
        raise Unready(why)
    signal_data().mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        _cli("link", "-n", name),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=_env(), bufsize=1,
    )
    deadline = threading.Timer(LINK_URI_WAIT_S, _kill_tree, [proc])
    deadline.start()
    said: list[str] = []
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("sgnl://linkdevice"):
                return line, proc
            if line:
                said.append(line)
    finally:
        deadline.cancel()
    _kill_tree(proc)
    raise LinkFailed(f"signal-cli gave no link to scan: {' / '.join(said[-3:])[:300]}"
                     if said else f"signal-cli gave no link to scan within"
                     f" {LINK_URI_WAIT_S:.0f} seconds")


def finish_link(proc: subprocess.Popen, timeout: float = LINK_WAIT_S) -> dict:
    """Wait for the phone to finish linking: `{"linked": True, "account":
    ...}`, or `{"linked": False, "error": ...}` saying what to do. BLOCKING,
    up to `timeout`; the process has ended either way."""
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return {"linked": False,
                "error": "The code was not scanned in time. Show a new one to try again."}
    if linked := accounts():
        return {"linked": True, "account": linked[0]}
    # Two failures look the same from `listAccounts`, which returns nothing
    # for either; the account file tells them apart, and the fixes differ.
    # signal-cli writes the account before the phone confirms, and links
    # over a half-linked one, so nothing here needs deleting first.
    if known_accounts():
        return {"linked": False,
                "error": ("The phone scanned the code, but linking did not finish. Remove the"
                          " unfinished device on the phone (Signal, Settings, Linked devices),"
                          " then show a new code.")}
    return {"linked": False, "error": "Linking did not finish. Show a new code to try again."}


def end_link(proc: subprocess.Popen) -> None:
    """End a link attempt before the phone has finished it. BLOCKING."""
    _kill_tree(proc)


def qr_svg(link: str) -> str:
    """`link` as a QR code in SVG, for the app to show, made in memory.

    Error correction is high because it is photographed off a screen. The
    border of four modules is the spec's minimum quiet zone."""
    import qrcode
    import qrcode.image.svg

    return qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, border=4,
                       error_correction=qrcode.constants.ERROR_CORRECT_H
                       ).to_string(encoding="unicode")


def forget() -> None:
    """Delete this device's Signal state: its keys, and the messages and
    attachments it kept. BLOCKING.

    The phone still lists the device until it is removed there; a linked
    device cannot remove itself. The caller says so."""
    reap_daemons()
    for attempt in range(5):
        try:
            shutil.rmtree(signal_data())
            break
        except FileNotFoundError:
            break
        except OSError:
            # The JVM just killed may hold a file for a moment longer.
            if attempt == 4:
                raise
            time.sleep(1)
    shutil.rmtree(attachment_dir(), ignore_errors=True)


def _attachment_ids(node: object) -> list[str]:
    """The stored names of every attachment an envelope names, at any
    depth: a message's own, a quoted one's, and their thumbnails."""
    if isinstance(node, list):
        return [name for item in node for name in _attachment_ids(item)]
    if not isinstance(node, dict):
        return []
    found = [str(item["id"]) for item in node.get("attachments") or []
             if isinstance(item, dict) and item.get("id")]
    if isinstance(thumbnail := node.get("thumbnail"), dict) and thumbnail.get("id"):
        found.append(str(thumbnail["id"]))
    return found + [name for value in node.values() for name in _attachment_ids(value)]


# A photo or voice note sent to Note to Self is kept `KEEP_ATTACHMENTS_S`;
# others' are deleted on arrival.


def sweep_attachments(*, now: float | None = None) -> int:
    """Delete the attachments older than `KEEP_ATTACHMENTS_S`, by the time
    each file was written; how many went. BLOCKING."""
    cutoff = (time.time() if now is None else now) - KEEP_ATTACHMENTS_S
    return delete_older([signal_data() / "attachments", attachment_dir()], cutoff)


def _discard_attachments(event: dict) -> None:
    """Delete the files of the attachments `event` names. Only a bare file
    name in signal-cli's attachment folder: the names came over the
    network."""
    folder = signal_data() / "attachments"
    for name in set(_attachment_ids(event)):
        if Path(name).name != name or name in (".", ".."):
            continue
        with contextlib.suppress(OSError):
            (folder / name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The channel
# ---------------------------------------------------------------------------


class SignalChannel(Channel):
    name = "signal"
    goodbye = ("Unlinked: Steve has deleted its keys for this account and won't read it"
               " again. Your phone still lists the device until you remove it: Signal,"
               " Settings, Linked devices.")

    def __init__(self, account: str | None = None, *, note_to_self_only: bool = True) -> None:
        # Deliberately not resolved here. __init__ gets called from async
        # code, and accounts() blocks on the account lock -- doing it eagerly
        # is what took the worker's event loop down. start() resolves it.
        self.account = account
        # Everything else on the account is ignored. A linked device can see
        # ordinary conversations; deliberately not acting on them keeps the
        # blast radius of a bug to a thread the user is talking to themselves in.
        self.note_to_self_only = note_to_self_only
        self.port: int | None = None
        self.proc: subprocess.Popen | None = None
        self._client: httpx2.AsyncClient | None = None
        self._rpc_id = 0
        self._stop = asyncio.Event()
        # Starts of the daemon in a row that did not come up (`_revive`).
        self._revivals = 0
        # What the app says while the daemon is down; None while it is up.
        self.error: str | None = None
        # What was sent, by signal-cli's timestamp for it, newest last. A
        # reaction names its target only by that timestamp; see `sent_text`.
        self._sent: collections.OrderedDict[int, str] = collections.OrderedDict()
        # Which of those were cards, by the same timestamp (`sent_card`).
        self._cards: dict[int, int] = {}

    @property
    def capabilities(self) -> set[Capability]:
        return {
            Capability.TEXT,
            Capability.VOICE_IN,
            Capability.VOICE_OUT,
            Capability.ATTACHMENTS,
            Capability.TYPING,
            Capability.READ_RECEIPTS,
            Capability.REACTIONS,
        }

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # -- lifecycle ---------------------------------------------------------

    async def start(self, timeout: float = 120.0) -> None:
        if self.alive:
            return

        # Here, where signal-cli is run, and not once as the worker begins:
        # the restart after a crash (`_revive`) comes back through this, and
        # a jar replaced in between would run holding the Signal keys.
        # Off the loop: it hashes the add-ons' files.
        if (why := await asyncio.to_thread(unready)) is not None:
            raise Unready(why)

        # Before anything reads the account directory: a leftover JVM from a
        # retired worker still holds its lock, and every signal-cli call would
        # block on it -- including the one just below.
        await asyncio.to_thread(reap_daemons)

        if self.account is None:
            self.account = (await accounts_async() or [None])[0]
        if not self.account:
            raise NotLinked("no linked Signal account")

        attachment_dir().mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        self._stop.clear()
        self.proc = subprocess.Popen(
            _cli(
                "-a", self.account,
                "daemon",
                "--http", f"127.0.0.1:{self.port}",
                "--receive-mode", "on-start",
                "--ignore-stories",
            ),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_env(),
        )
        self._client = httpx2.AsyncClient(timeout=httpx2.Timeout(60.0, connect=5.0))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive:
                raise RuntimeError("signal-cli daemon exited during startup")
            try:
                await self._rpc("version")
            except Exception:  # noqa: BLE001 - still booting
                await asyncio.sleep(0.5)
                continue
            # The port was free when chosen and signal-cli's once Java has
            # started, seconds later. A program that took it in between
            # answers `version` as easily, and would then be sent the user's
            # messages and report their answers to cards. Whose the listener
            # is decides (core/localonly.served_by).
            if await asyncio.to_thread(localonly.served_by, self.port, self.proc.pid):
                return
            await self.stop()
            raise RuntimeError(f"port {self.port} is served by another program, "
                               f"not the signal-cli started here")
        raise RuntimeError("signal-cli daemon never became reachable")

    async def stop(self) -> None:
        self._stop.set()
        if self._client:
            await self._client.aclose()
            self._client = None
        if self.proc and self.proc.poll() is None:
            # Off the loop: killing the tree shells out to taskkill, and this
            # runs during worker shutdown where a stall is a hung swap.
            await asyncio.to_thread(_kill_tree, self.proc)
        self.proc = None

    # -- JSON-RPC ----------------------------------------------------------

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        if self._client is None:
            raise RuntimeError("channel not started")
        self._rpc_id += 1
        response = await self._client.post(
            f"{self.base_url}/api/v1/rpc",
            json={
                "jsonrpc": "2.0",
                "id": self._rpc_id,
                "method": method,
                "params": params or {},
            },
        )
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"signal-cli {method}: {body['error']}")
        return body.get("result", {})

    # -- receiving ---------------------------------------------------------

    def _to_incoming(self, event: dict) -> Incoming | None:
        """Turn one signal-cli envelope into a message, or None to ignore it.

        An ignored envelope's attachments are deleted. signal-cli downloads
        every attachment of every chat the account is in, and Steve reads
        only Note to Self, so the photos and voice notes of the user's other
        conversations piled up in the data folder, unread and never
        removed (found 13 October 2026)."""
        incoming = self._read(event)
        if incoming is None:
            _discard_attachments(event)
        return incoming

    def _read(self, event: dict) -> Incoming | None:
        """Note to Self arrives as a *sync* message rather than a data message:
        the phone reports what it sent, and the destination is the account's
        own number. Reading only `dataMessage` would see nothing at all.
        """
        envelope = event.get("envelope") or {}
        sender = envelope.get("sourceNumber") or envelope.get("source") or ""

        sync = (envelope.get("syncMessage") or {}).get("sentMessage")
        data = envelope.get("dataMessage")

        if sync:
            destination = sync.get("destinationNumber") or sync.get("destination")
            is_note_to_self = destination == self.account
            if self.note_to_self_only and not is_note_to_self:
                return None
            message = sync
        elif data:
            if self.note_to_self_only:
                return None  # a real inbound message from someone else
            message = data
        else:
            return None  # receipt, typing indicator, read marker

        # Its own message, come back. Sent with `notifySelf`, a reply is an
        # ordinary message to their own number, and read as theirs it would be
        # answered -- by a reply that comes back in turn. signal-cli does not
        # echo a message to the device that sent it today; this makes that a
        # fact about this code rather than about signal-cli's next release.
        stamp = int(message.get("timestamp") or envelope.get("timestamp") or 0)
        if stamp and stamp in self._sent:
            return None

        # A reaction, which until now fell through the `not text and no
        # attachment` check below and was dropped silently. signal-cli sends
        # `targetTimestamp` and reports `targetSentTimestamp`; the asymmetry
        # is real and has bitten before.
        reaction = message.get("reaction") or {}
        if reaction and not reaction.get("isRemove"):
            return Incoming(
                text="",
                channel=self.name,
                sender=sender,
                message_id=str(envelope.get("timestamp") or ""),
                timestamp=int(envelope.get("timestamp") or 0),
                reaction=(reaction.get("emoji") or "").strip(),
                reacted_to=int(reaction.get("targetSentTimestamp") or 0),
                raw=event,
            )

        text = (message.get("message") or "").strip()
        attachments: list[Path] = []
        audio: Path | None = None
        others: list[tuple[Path, str, str | None]] = []

        for item in message.get("attachments") or []:
            stored = str(item.get("id") or "")
            # A bare name in signal-cli's folder: the file it names is read
            # and, if a photo, sent to the model.
            if not stored or Path(stored).name != stored or stored in (".", ".."):
                continue
            path = signal_data() / "attachments" / stored
            if not path.exists():
                continue
            attachments.append(path)
            content_type = (item.get("contentType") or "").lower()
            if audio is None and (item.get("voiceNote") or content_type.startswith("audio/")):
                audio = path
            else:
                others.append((path, content_type, item.get("filename")))

        # Until 13 October 2026 a photo was neither shown to the model nor
        # said to have come, and a photo with no words was dropped unanswered.
        sorted_ = photos.sort(others)
        if not text and audio is None and not sorted_.images and not sorted_.unopened:
            return None

        return Incoming(
            text=text,
            channel=self.name,
            sender=sender,
            message_id=str(envelope.get("timestamp") or message.get("timestamp") or ""),
            timestamp=int(envelope.get("timestamp") or 0),
            audio_path=audio,
            attachments=attachments,
            images=sorted_.images,
            unopened=sorted_.unopened,
            raw=event,
        )

    async def receive(self) -> AsyncIterator[Incoming]:
        """Stream inbound messages from the daemon's SSE endpoint.

        Reconnects on its own: the daemon can be restarted underneath this,
        and a channel that stops listening after one hiccup is a channel that
        silently stops working.
        """
        while not self._stop.is_set():
            try:
                async with self._client.stream(
                    "GET", f"{self.base_url}/api/v1/events",
                    timeout=httpx2.Timeout(None, connect=10.0),
                ) as stream:
                    async for line in stream.aiter_lines():
                        if self._stop.is_set():
                            return
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        message = self._to_incoming(event)
                        if message is not None:
                            yield message
            except Exception:  # noqa: BLE001 - reconnect rather than die
                if self._stop.is_set():
                    return
                if self.proc is not None and not self.alive:
                    await self._revive()
                    continue
                await asyncio.sleep(2.0)

    async def _revive(self) -> None:
        """Start the daemon again, once it has died under the channel.

        Reconnecting alone went on every two seconds, for good, to a port
        nobody listened on, while the app showed Signal ready and every
        reply failed: step 10's hostile scenarios killed the daemon
        (tests/e2e/test_hostile_signal.py). A daemon that will not come up
        is tried less often, up to once a minute."""
        wait = min(2.0 * 2 ** self._revivals, 60.0)
        log.warning("signal-cli's daemon exited (code %s); starting it again in %.0fs",
                    self.proc.returncode if self.proc else None, wait)
        if self._revivals == 0:
            self.error = "Signal's helper program stopped. Steve is starting it again."
        await asyncio.sleep(wait)
        if self._stop.is_set():
            return
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.proc = None
        try:
            await self.start()
        except Exception as exc:  # noqa: BLE001 - tried again on the next turn round
            self._revivals += 1
            self.error = ("Signal's helper program will not start again, so Steve can't read "
                          "or send Signal messages. Steve keeps trying, once a minute at most.")
            log.warning("signal-cli's daemon did not start again: %s", exc)
        else:
            self._revivals = 0
            self.error = None

    # -- sending -----------------------------------------------------------

    async def send(self, reply: Outgoing, *, to: str | None = None) -> bool:
        # Signal carries formatting as style ranges, not syntax, so markdown
        # the model wrote arrives on the phone as literal characters. Plain,
        # and with no waiting card's word but a card's (channels/plaintext.shown).
        params: dict = {"message": shown(reply)}
        if to and not self.note_to_self_only:
            params["recipient"] = [to]
        elif self.account:
            params["recipient"] = [self.account]
        else:
            params["noteToSelf"] = True
        if self.account and self.account in params.get("recipient", []):
            # To their own number as an ordinary message. `noteToSelf` is
            # signal-cli's sync-only path -- "send the message to self without
            # notification", in its own words -- so every reply and ping
            # landed in Note to Self in silence, seen only when the user happened
            # to open Signal. `notifySelf` makes it a message that buzzes.
            params["notifySelf"] = True

        try:
            result = await self._rpc("send", params)
        except Exception:  # noqa: BLE001 - a failed send must not kill the loop
            return False
        stamp = int((result or {}).get("timestamp") or 0) if isinstance(result, dict) else 0
        if stamp:
            self._sent[stamp] = params["message"]
            if reply.card is not None:
                self._cards[stamp] = reply.card
            while len(self._sent) > SENT_REMEMBERED:
                gone, _ = self._sent.popitem(last=False)
                self._cards.pop(gone, None)
        return True

    def sent_text(self, timestamp: int) -> str | None:
        """What went out at `timestamp`. In memory only: after a restart a
        reaction to an older message means nothing, and the user types the word."""
        return self._sent.get(int(timestamp or 0))

    def sent_card(self, timestamp: int) -> int | None:
        """The card that went out at `timestamp`, if it was one. In memory
        only, as `sent_text` is."""
        return self._cards.get(int(timestamp or 0))

    async def react(self, message: Incoming, emoji: str, *, remove: bool = False) -> None:
        """React to one message.

        Signal identifies the target by author plus the message's own
        timestamp, which is why Incoming carries both rather than just an
        opaque id.
        """
        if not message.timestamp:
            return
        params: dict = {
            "emoji": emoji,
            "targetAuthor": message.sender or self.account,
            "targetTimestamp": message.timestamp,
            "remove": remove,
        }
        if self.note_to_self_only or message.sender == self.account:
            params["noteToSelf"] = True
        else:
            params["recipient"] = [message.sender]
        with contextlib.suppress(Exception):
            await self._rpc("sendReaction", params)


    async def typing(self, on: bool, *, to: str | None = None) -> None:
        params: dict = {"stop": not on}
        if to and not self.note_to_self_only:
            params["recipient"] = [to]
        else:
            params["noteToSelf"] = True
        with contextlib.suppress(Exception):
            await self._rpc("sendTyping", params)

    async def mark_read(self, message: Incoming) -> None:
        if not message.sender or not message.timestamp:
            return
        with contextlib.suppress(Exception):
            await self._rpc(
                "sendReceipt",
                {"recipient": message.sender, "targetTimestamp": [message.timestamp],
                 "type": "read"},
            )

    def status(self) -> dict:
        return {
            "channel": self.name,
            "installed": installed(),
            "account": self.account,
            "running": self.alive,
            "error": self.error,
            "note_to_self_only": self.note_to_self_only,
            "capabilities": sorted(self.capabilities),
        }


def _main() -> None:
    """For a developer without the app, which does all of this itself."""
    import argparse

    parser = argparse.ArgumentParser(description=f"Signal channel for {DEVICE_NAME}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install", help="download signal-cli and its Java, pinned by hash")
    link_cmd = sub.add_parser("link", help="link this machine as a Signal device")
    link_cmd.add_argument("--name", default=DEVICE_NAME)
    sub.add_parser("status")
    sub.add_parser("unlink", help="forget the local account state")

    args = parser.parse_args()

    if args.cmd == "install":
        install(lambda done, of: print(f"\r{done * 100 // of}%", end="", flush=True))
        print("\ninstalled")
        return

    if args.cmd == "link":
        if why := unready():
            print(why)
            raise SystemExit(1)
        import qrcode

        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")
        link, proc = start_link(args.name)
        code = qrcode.QRCode(border=4)
        code.add_data(link)
        print("On your phone: Signal, Settings, Linked devices, +. Scan this:")
        code.print_ascii(invert=True)
        result = finish_link(proc)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result.get("linked") else 1)

    if args.cmd == "status":
        # Resolved explicitly: the channel no longer does it in __init__, and
        # from a one-shot CLI blocking on the lock is fine.
        print(json.dumps({"unready": unready(),
                          **SignalChannel((accounts() or [None])[0]).status()}, indent=2))

    elif args.cmd == "unlink":
        forget()
        print(SignalChannel.goodbye)


if __name__ == "__main__":
    _main()
