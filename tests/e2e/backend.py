"""Steve's backend, started as the app starts it, for the end-to-end tests.

The supervisor runs as a developer's build of the app runs it
(ui/src-tauri/src/backend.rs): `python -E -s -B -m core.supervisor` in the
checkout, its output to a log, and it starts the worker. The Python is the
fenced one the test runs under (tests/gate/fences.fenced_python), so both
processes shut the same doors a test's own does; the data folder is the
test's (`STEVE_DATA_DIR`); and the world's environment points it at the
fakes (tests/fakes/world.py). It is driven as the app drives it: the port
and token from the run file it writes, the token on every request, and
`/ws` for the conversation.

Everything it starts is in a kill-on-close job from its start: a test that
failed halfway would otherwise leave a worker running, holding its data
folder and a share of the computer, until the next start reaped it.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from core import localapi
from tests.fakes.world import TOKEN, World

REPO = Path(__file__).resolve().parents[2]
# The app's own wait for a first start, under load.
START_S = 90


class Backend:
    def __init__(self, world: World, data: Path, log: Path, process: subprocess.Popen,
                 base: str, token: str) -> None:
        self.world, self.data, self.log, self.process = world, data, log, process
        self.base, self.token = base, token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def action(self, card: dict) -> str:
        """The card's action's status, as the app's activity page has it, or
        what the route answered instead. Three tests read the answer blindly,
        and on CI's busy runner one without `items` (the supervisor's own
        error, its worker out of reach for a moment) stopped a wait with a
        KeyError that hid it (22 October 2026)."""
        reply = self.get("/control/activity")
        if reply.status_code != 200:
            return f"answered {reply.status_code}: {reply.text[:300]}"
        return next(a["status"] for a in reply.json()["items"]
                    if a["kind"] == "action" and a["id"] == card["action_id"])

    def get(self, path: str, **kwargs) -> httpx.Response:
        return httpx.get(self.base + path, headers=self.headers, timeout=30,
                         verify=localapi.tls(), **kwargs)

    def post(self, path: str, body: dict | None = None, **kwargs) -> httpx.Response:
        return httpx.post(self.base + path, json=body or {}, headers=self.headers, timeout=60,
                          verify=localapi.tls(), **kwargs)

    def put(self, path: str, body: dict, **kwargs) -> httpx.Response:
        return httpx.put(self.base + path, json=body, headers=self.headers, timeout=60,
                         verify=localapi.tls(), **kwargs)

    def card(self, words: str, *, kind: str = "yes") -> dict:
        """The one card's note with these words on it (its text, its detail
        or an item), read where the app reads it (`/control/needs`): `kind`
        "code" for a high-risk one."""
        notes = [note for note in self.get("/control/needs").json()["needs"]
                 if note["kind"] == kind and words in "\n".join(
                     [note["text"], note["detail"] or "", *note.get("items", [])])]
        assert len(notes) == 1, notes
        return notes[0]

    def decide(self, note: dict, approve: bool, **more) -> httpx.Response:
        """A yes or a no to a card, as the app gives it: with the card's
        hash as it was read."""
        return self.post("/actions/decide", {"action_id": note["action_id"], "approve": approve,
                                             "seen": note["seen"], **more})

    async def chat(self, text: str, *, within: float = 60, **more) -> dict:
        """One turn over `/ws`, as the app's chat sends it: the reply, or
        the error the socket gave instead."""
        import asyncio

        import websockets

        url = self.base.replace("http://", "ws://") + "/ws"
        async with websockets.connect(url, subprotocols=["steve", f"token.{self.token}"]) as ws:
            assert json.loads(await ws.recv())["type"] == "hello"
            await ws.send(json.dumps({"type": "chat", "text": text, **more}))
            async with asyncio.timeout(within):
                while True:
                    said = json.loads(await ws.recv())
                    if said["type"] in ("reply", "error"):
                        return said

    def leaks(self) -> list[str]:
        """Every secret seen out of its place (tests/fakes/world.py), the
        log the supervisor wrote and this start's token among them."""
        return self.world.leaks(self.data, also={TOKEN: self.token}) + [
            f"{name} in the backend's log" for name, value in
            {**self.world.secrets, TOKEN: self.token}.items()
            if value in self.log.read_text(encoding="utf-8", errors="replace")]

    def logs(self) -> str:
        """What the backend says now (`asked`), the supervisor's output and
        the end of each log in the data folder, for a failure's message."""
        parts = [*self.asked(), f"--- {self.log.name}",
                 self.log.read_text(encoding="utf-8", errors="replace")]
        for path in sorted((self.data / "logs").glob("*.log")):
            text = path.read_text(encoding="utf-8", errors="replace")
            parts += [f"--- {path.name}", text[-6000:]]
        return "\n".join(parts)

    def asked(self) -> list[str]:
        """The supervisor's state and each channel's, as the worker gives
        them now, each with 5 s to answer: a channel stuck starting reads
        "starting", a worker that answers nothing times out. On CI (22
        October 2026) scenarios waited 60 s for Signal or Telegram to
        listen, and the logs said nothing after the worker took up its
        duties."""
        said = []
        for path in ("/_sup/status", "/signal/status", "/telegram/status"):
            began = time.monotonic()
            try:
                answer = httpx.get(self.base + path, headers=self.headers, timeout=5)
                said.append(f"{path}: {answer.status_code} {answer.text[:300]}"
                            f" ({time.monotonic() - began:.1f} s)")
            except httpx.HTTPError as error:
                said.append(f"{path}: {type(error).__name__} after"
                            f" {time.monotonic() - began:.1f} s")
        return said


def until(holds: Callable[[], bool], *, within: float = 20) -> bool:
    """Whether `holds` came true within `within` seconds: the backend does
    its work on its own time, and a scenario waits for the effect."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if holds():
            return True
        time.sleep(0.2)
    return holds()


def morning_now(now: datetime) -> dict[str, str]:
    """The knobs that make the morning briefing due at `now` and nothing
    else: its hour the minute just gone, the quiet hours away from now,
    and the evening's half an hour on, before them. The knobs refuse a
    briefing inside quiet hours, and eight hours from an hour on held the
    evening's 21:00 whenever this ran between noon and eight; moving only
    the quiet hours failed the scenarios each afternoon. Half an hour on,
    the evening's last was 23 and a half hours ago, long past its
    `LATE_BY`, so it is not due either."""
    # The minute just gone, today: at midnight, midnight itself.
    morning = max(now - timedelta(minutes=1), now.replace(hour=0, minute=0))
    return {"MORNING": morning.strftime("%H:%M"),
            "EVENING": (now + timedelta(minutes=30)).strftime("%H:%M"),
            "QUIET_FROM": (now + timedelta(hours=1)).strftime("%H:%M"),
            "QUIET_UNTIL": (now + timedelta(hours=9)).strftime("%H:%M")}


def briefed(steve: Backend) -> None:
    """The morning's briefing, made due now and waited for into the day.
    Sent at start once its hour has passed, and not before, it met the
    trouble a hostile scenario set for its own reply on some mornings
    only. Moved as tests/e2e/test_briefing.py moves it; the caller waits
    for it to reach its channel."""
    moved = steve.post("/control/knobs", {"changes": morning_now(datetime.now())})
    assert moved.json()["ok"], moved.text
    assert until(lambda: any(e["kind"] == "briefing"
                             for e in steve.get("/control/day").json()["entries"])), steve.logs()


def _serving(process: subprocess.Popen, run_file: Path) -> tuple[str, str] | None:
    """The address and token once the supervisor reports a healthy worker;
    None if it died first or never did (tests/gate/test_zero_downtime.py)."""
    base = token = None
    deadline = time.monotonic() + START_S
    while time.monotonic() < deadline and process.poll() is None:
        if base is None:
            try:
                published = json.loads(run_file.read_text(encoding="utf-8"))
                base, token = f"http://127.0.0.1:{published['port']}", published["token"]
            except (OSError, ValueError, KeyError):
                time.sleep(0.2)
                continue
        try:
            answer = httpx.get(f"{base}/_sup/health", timeout=2.0, verify=localapi.tls(),
                               headers={"Authorization": f"Bearer {token}"})
            if answer.status_code == 200 and answer.json().get("ok"):
                return base, token
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return None


@contextlib.contextmanager
def running(world: World, data: Path) -> Iterator[Backend]:
    """The backend on `data`, the folder `STEVE_DATA_DIR` names, for the
    length of the block; stopped as the app stops it, then everything it
    started ended with its job."""
    from core import paths
    from core.sandbox import job

    assert paths.data_dir() == data, "the backend's data folder is the one STEVE_DATA_DIR names"
    log = data.parent / "backend.log"
    env = {**os.environ, **world.env}
    # Added to, so a restart's scan still reads the first start's output.
    with log.open("a", encoding="utf-8", errors="replace") as out:
        process = subprocess.Popen([sys.executable, "-E", "-s", "-B", "-m", "core.supervisor"],
                                   cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=subprocess.STDOUT)
    try:
        held = job.contain(int(process._handle))
    except OSError:
        process.kill()
        raise
    try:
        served = _serving(process, paths.run_dir() / "backend.json")
        backend = Backend(world, data, log, process, *(served or ("", "")))
        if served is None:
            raise RuntimeError(f"the backend did not start in {START_S} s:\n{backend.logs()}")
        yield backend
        with contextlib.suppress(httpx.HTTPError):
            backend.post("/_sup/stop")
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(30)
    finally:
        held.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(10)
