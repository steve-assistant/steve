"""The second local reader: Tev1, a decision model on the laptop's GPU, beside
PIGuard when Jev cannot answer.

When Jev cannot answer -- an outage, no key -- and no pattern flagged an
item, PIGuard decides in its place (`core/detector.py`). PIGuard reads words:
with the patterns it caught 16 of the red team's 20 attacks, and missed quiet
ones written for the screen, an instruction worded as an ordinary request.
The user (11 October 2026): "that local detector doesn't seem that smart, are
there more powerful or accurate local models out there we could run
instead?"

None of the local models tried did better alone. Each was asked the screen's
own question about the red team, about 64 of their items (read on the laptop
and sent nowhere), and about BIPIA's public emails, 50 with an attack planted
and 50 without. Decision models read intent, and caught the quiet attacks,
but missed BIPIA's "do X in your response" lines, which PIGuard caught.
Together, an item flagged if either flags it, PIGuard and Tev1 4B came close
to Jev: 19 of the red team's 20 with the patterns (Jev 20, PIGuard alone 16),
48 of BIPIA's 50 (Jev 40, PIGuard 47), none of its 50 clean emails, and 5 of
their 64 items flagged -- one more than PIGuard alone, all of them benign.
The user chose to add it the same day.

Tev1 is Ollama's decision model (Apache-2.0; 4.2 billion parameters, 8-bit):
asked a question with listed options, it gives a probability for each, as
Jev does through OpenRouter. It is asked exactly what Jev is asked
(`screen.request_for`): the item, or one part of it, and where it came from,
never their context. It reads at most 2,050 tokens at once, the question
included, and refuses a longer part (`TooLong`) rather than cut it, so the
screen gives it shorter parts than it gives Jev.

**How it runs.** Ollama 0.40.3 is an add-on (`core/addons.py`): its Windows
release pinned by its size and SHA-256, unpacked beside the install
(`paths.addons_dir`), and its files hashed again before every start of the
server. Until step 11's audit it was unpacked into the model cache, in the
data folder, and only the presence of ollama.exe was checked before it ran:
a program of the user's that could write there could replace it, and the
next start ran it, with the network open while it pulled the model. The
model, pinned by its manifest, stays in the model cache. Both are installed
by `python -m core.decider setup`. Nothing is fetched at the moment it is
needed, because an outage that took Jev away may have taken the network with
it. The worker starts the server the first time Jev cannot answer -- most
days Jev answers and nothing here runs -- one per worker process:

- on a port of its own on 127.0.0.1, behind Ollama's own Origin and Host
  checks (localhost is not private: docs/protocols.md). The runner it
  starts for the model, llama.cpp's server, listens on a second port with
  neither check: whatever finds that port while the model is loaded can run
  the model, but reads nothing of the items (probed 11 October 2026: its
  `/slots` gives counts, not text);
- with none of the worker's environment, a home of its own, and a proxy that
  refuses, so it has no way out; it needs none once installed;
- in a job object that kills it with the worker, so a worker that dies or is
  swapped out does not leave a server holding the GPU;
- unloading the model after `KEEP_ALIVE` idle, which gives back about 4.6 GB
  of the GPU and 1.2 GB of memory; loading it again takes about 15 seconds.

An answer counts only if it came from the pinned model, wholly on the GPU
(`/api/ps`). A model spilled onto the CPU would take their cores from the
desk's audio, so the server is stopped instead, the model rests for
`REST_S`, and the item is left to PIGuard.

Frozen with the screen, so the agent cannot loosen the reader it is judged
by.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx2

from core import addons, localonly, paths

VERSION = "0.40.3"
RUNTIME = "ollama-windows-amd64.zip"
RUNTIME_URL = f"https://github.com/ollama/ollama/releases/download/v{VERSION}/{RUNTIME}"
RUNTIME_SHA256 = "e5ecff02dfc7cae8ddf6d6b7a7ac5f727ffeedf4491f44ae75ff5b0820dbbbd5"
RUNTIME_ADDON = addons.Addon(
    name="ollama",
    version=VERSION,
    url=RUNTIME_URL,
    size=1_468_053_171,
    sha256=RUNTIME_SHA256,
    top="",
    # Taken on 20 October 2026 from an unpacked copy whose 82 files matched
    # the release's zip by name, size and CRC-32.
    tree="44705aca5fe3dca04c0ba39f63e1f5d875bb3a8ec9977478c76244129d48b276",
    what="The local model's runtime",
)
MODEL = "tev1:4b"
# The manifest as Ollama keeps it names every layer by its digest -- the
# weights (sha256:35f9281a..., from Tev1-4B-Q8_0.gguf), the system prompt,
# the parameters -- so its hash pins them all. `DIGEST` is the registry's
# manifest, which Ollama reports for the model it has loaded (`/api/ps`).
MANIFEST_SHA256 = "199ff65f3ce88138c7f4f3f5aeae6d5b3de1101c9acea7baeeda81e4d417e583"
DIGEST = "9b5bb969e46c4b776826d6f2d401e22893205693f172653af6254897255025b8"

# How long the model stays loaded after its last answer: an outage sends
# items in a run.
KEEP_ALIVE = "5m"
# Measured ready in 6 seconds, most of it finding the GPU.
START_TIMEOUT_S = 60.0
# A request that loads the model first takes about 15 seconds; about 100 the
# first time, while CUDA compiles its kernels, which `setup` does once.
TIMEOUT_S = 120.0
# How long the model is not started again after it did not fit on the GPU,
# or its runtime's files were not the pinned ones: hashing them takes
# seconds, too long to repeat for each item while they stay changed.
REST_S = 600.0


def log_path() -> Path:
    return paths.logs_dir() / "ollama.log"


# Each start adds about 70 KB to the log -- the runner lists the model's
# every tensor -- and an outage of days starts one every few minutes.
LOG_BYTES = 5_000_000
# Ollama's refusal of a part longer than the model reads (captured:
# tests/gate/test_local_decider.py).
_TOO_LONG = re.compile(r"prompt \d+ has \d+ tokens; expected")


class TooLong(ValueError):
    """The part is longer than the model reads at once, and was not read."""


@dataclass
class _Server:
    process: subprocess.Popen
    url: str
    end: Callable[[], None]  # kills it and everything it started


# One request at a time, and one server per process.
_lock = threading.Lock()
_server: _Server | None = None
_resting_until = 0.0
_rest_why = ""


def _rest(why: str) -> None:
    """No server started for `REST_S`, and each request told `why`."""
    global _resting_until, _rest_why
    _resting_until, _rest_why = time.monotonic() + REST_S, why


def home() -> Path:
    """Where the model and the server's own home live: beside PIGuard, in
    the model cache (`STEVE_MODEL_CACHE`, else data/models). The runtime is
    an add-on's (`RUNTIME_ADDON`)."""
    return paths.models_dir() / "ollama"


def _exe() -> Path:
    return addons.home(RUNTIME_ADDON) / "ollama.exe"


def _manifest() -> Path:
    name, tag = MODEL.split(":")
    return home() / "models" / "manifests" / "registry.ollama.ai" / "library" / name / tag


def installed() -> bool:
    """The runtime and the pinned model are in place -- on the laptop only.
    Cheap, for each request: the runtime's files are hashed before each
    start of the server instead (`_start`)."""
    return (sys.platform == "win32" and _exe().is_file() and _manifest().is_file()
            and addons.file_sha256(_manifest()) == MANIFEST_SHA256)


def ask(payload: dict) -> dict:
    """The model's answer to one of the screen's requests, as Ollama's
    `/v1/systemone` gives it. Blocking: a third of a second once the model
    is loaded, about 15 seconds more when it is not; one request at a time.
    Raises `TooLong` for a part longer than the model reads at once, and
    anything else when it cannot answer."""
    with _lock:
        if time.monotonic() < _resting_until:
            raise RuntimeError(f"{_rest_why}; resting")
        if not installed():
            raise FileNotFoundError(f"{MODEL} is not installed in {home()} "
                                    f"(python -m core.decider setup)")
        _start()
        try:
            response = _call("POST", "/v1/systemone", {**payload, "model": MODEL})
            if response.status_code == 400 and _TOO_LONG.search(response.text):
                raise TooLong(_said(response))
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}: {_said(response)}")
            _check_loaded()
            return response.json()
        except httpx2.TransportError:
            # Stuck or gone: the next item starts a fresh one.
            _stop()
            raise


def _check_loaded() -> None:
    """That the model which answered is the pinned one, wholly on the GPU;
    if not, the server is stopped and the answer goes unused."""
    loaded = {m.get("name"): m for m in
              _call("GET", "/api/ps", timeout=10.0).json().get("models") or []}
    model = loaded.get(MODEL) or {}
    if model.get("digest") != DIGEST:
        _stop()
        raise RuntimeError(f"the model that answered is not the pinned {MODEL} "
                           f"(digest {model.get('digest')})")
    if int(model.get("size_vram") or 0) < int(model.get("size") or 1):
        _stop()
        _rest(f"{MODEL} did not fit on the GPU lately")
        raise RuntimeError(f"{MODEL} did not fit on the GPU ({model.get('size_vram')} of "
                           f"{model.get('size')} bytes); resting {REST_S / 60:.0f} minutes")


def _said(response: httpx2.Response) -> str:
    """Ollama's own words for a refusal (docs/protocols.md §11)."""
    with contextlib.suppress(ValueError, AttributeError):
        return str(response.json()["error"])[:300]
    return response.text[:300]


def _start(*, offline: bool = True) -> None:
    """This process's server, started if it is not running, and used only
    once it answers as the pinned version (docs/protocols.md §5)."""
    global _server
    if _server is not None and _server.process.poll() is None:
        return
    _stop()
    changed = addons.verify(RUNTIME_ADDON)
    if changed is not None:
        _rest(changed)
        raise RuntimeError(changed)
    port = _free_port()
    process = _spawn(port, offline=offline)
    _server = _Server(process, f"http://127.0.0.1:{port}", _contain(process))
    deadline = time.monotonic() + START_TIMEOUT_S
    while True:
        try:
            answered = _call("GET", "/api/version", timeout=2.0)
        except httpx2.TransportError:
            answered = None
        if answered is not None:
            version = None
            with contextlib.suppress(ValueError, AttributeError):
                version = answered.json().get("version")
            # Another program took the port before the server could: one
            # that says it is another version, or says it is this one but is
            # not the process started here (core/localonly.served_by).
            if version == VERSION and localonly.served_by(port, process.pid):
                return
            _stop()
            if version == VERSION:
                raise RuntimeError(f"port {port} is served by another program, not the "
                                   f"Ollama started here")
            raise RuntimeError(f"port {port} answered as {version!r}, not Ollama {VERSION}")
        if process.poll() is not None or time.monotonic() > deadline:
            why = (f"exited with {process.returncode}" if process.poll() is not None
                   else f"did not answer in {START_TIMEOUT_S:.0f} seconds")
            _stop()
            raise RuntimeError(f"the Ollama server {why}: {_log_tail()}")
        time.sleep(0.25)


def _stop() -> None:
    """End this process's server and everything it started, if one runs."""
    global _server
    if _server is None:
        return
    _server.end()
    with contextlib.suppress(subprocess.TimeoutExpired):
        _server.process.wait(timeout=10)
    _server = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _environment(port: int, *, offline: bool = True) -> dict[str, str]:
    """All the server is given. Nothing of the worker's environment, which
    is not Ollama's to read. A home of its own, for the key it makes itself
    at start and the kernels CUDA compiles. And when `offline` -- always,
    but for `setup`'s pull -- a proxy that refuses: a running server's one
    call out is Ollama's own fetch of model recommendations, and it needs
    nothing from outside."""
    private = home() / "home"
    system = os.environ.get("SYSTEMROOT", r"C:\Windows")
    env = {
        "SYSTEMROOT": system,
        "WINDIR": system,
        "PATH": os.pathsep.join([str(Path(system) / "System32"), system]),
        **dict.fromkeys(("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"),
                        str(private)),
        "CUDA_CACHE_PATH": str(private / "cuda"),
        "OLLAMA_HOST": f"127.0.0.1:{port}",
        "OLLAMA_MODELS": str(home() / "models"),
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_KEEP_ALIVE": KEEP_ALIVE,
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_NUM_PARALLEL": "1",
        # As the model was measured.
        "OLLAMA_FLASH_ATTENTION": "1",
    }
    if offline:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    return env


def _spawn(port: int, *, offline: bool = True) -> subprocess.Popen:
    """The server process, its output to `log_path()`, never a pipe
    (docs/protocols.md §5). `tests/gate/fences.py` refuses it."""
    (home() / "home").mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    # Emptied in place past `LOG_BYTES`, as the app empties the supervisor's
    # (ui/src-tauri/src/backend.rs): another worker's server may hold it
    # open, in append mode, and its next line lands at the new end.
    with contextlib.suppress(OSError):
        if log_path().stat().st_size > LOG_BYTES:
            os.truncate(log_path(), 0)
    with log_path().open("ab") as log:
        return subprocess.Popen(
            [str(_exe()), "serve"], cwd=home(), env=_environment(port, offline=offline),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)


def _contain(process: subprocess.Popen) -> Callable[[], None]:
    """Put the server in a job object that kills everything in it when its
    last handle closes -- this process's, when it exits, however it exits --
    and return what ends it now. The server starts nothing for its first
    tenth of a second (GPU discovery comes after; the runner with the first
    request), so it is in the job before its first child is."""
    from core.sandbox import job

    # Popen's own handle to the process, not one opened by its pid, which a
    # process that had already exited could have passed on.
    try:
        held = job.contain(int(process._handle))
    except OSError:
        # Never run it outside the job: a server nothing kills is the thing
        # this exists to prevent.
        process.kill()
        raise
    return held.end


def _call(method: str, path: str, body: dict | None = None, *,
          timeout: float = TIMEOUT_S) -> httpx2.Response:
    """One request to this process's server, never through a proxy.
    `tests/gate/fences.py` refuses it."""
    return httpx2.request(method, _server.url + path, json=body, timeout=timeout,
                          trust_env=False)


def _log_tail() -> str:
    with contextlib.suppress(OSError):
        return log_path().read_bytes()[-800:].decode("utf-8", "replace").replace("\0", "")
    return f"(no log at {log_path()})"


def setup() -> Path:
    """Install the pinned runtime and model. The runtime is an add-on,
    downloaded unless its files are the pinned ones, and checked as one
    (`addons.install`): until step 11's audit any ollama.exe already there
    was taken as installed. The model is pulled by the runtime, which checks
    each layer against its digest, and its manifest against the pin, into
    `home()`. Then it answers once, so that CUDA compiles its kernels now --
    a minute and a half -- rather than while an item waits."""
    global _resting_until
    if sys.platform != "win32":
        raise RuntimeError("the local decision model runs on the laptop only")
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    addons.install(RUNTIME_ADDON)
    # A rest for a runtime that had changed ends with the runtime replaced.
    _resting_until = 0.0
    if not installed():
        with _lock:
            _start(offline=False)
            try:
                pulled = _call("POST", "/api/pull", {"model": MODEL, "stream": False},
                               timeout=3600.0)
                if pulled.status_code != 200:
                    raise RuntimeError(f"pulling {MODEL}: HTTP {pulled.status_code}: "
                                       f"{_said(pulled)}")
            finally:
                _stop()
        if not installed():
            raise RuntimeError(f"{MODEL}'s manifest hashed {addons.file_sha256(_manifest())}, "
                               f"not the pinned {MANIFEST_SHA256}")
    from core import screen

    try:
        ask(screen.request_for(screen.Artifact("setup", "warm-up", "", "Open 9 to 5.")))
    finally:
        with _lock:
            _stop()
    return root


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="The local decision model.")
    parser.add_argument("what", choices=["setup"])
    parser.parse_args()
    print(f"{MODEL} installed in {setup()}")


if __name__ == "__main__":
    _main()
