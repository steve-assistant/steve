"""The local decision model answers only as the pinned model, wholly on the
GPU, from a server of the worker's own that it can always end, and setup
installs only what is pinned.

core/decider.py runs Tev1 4B through Ollama, beside PIGuard, when Jev cannot
answer (the maintainer, 11 October 2026: "Add Tev1"). These pin what must hold:

- an answer is Ollama's own `/v1/systemone` reply to the screen's request,
  as captured from the pinned runtime and model
  (fixtures/decider/captured.json); a part over the model's limit is refused
  as too long, never cut; any other refusal comes back in Ollama's words;
- an answer from any model but the pinned one, or from one spilled off the
  GPU onto their cores, goes unused, and the server is stopped -- after a
  spill, not started again for a while;
- one server per process, used only once it answers as the pinned version,
  started again only when it has died or hung, and ended, with everything
  it started, when it is stopped or the worker exits however it exits;
- the server is given nothing of the worker's environment, and no way out,
  and its output goes to a log, emptied once past its size;
- setup unpacks only the release that hashes as pinned, and uses only a
  model whose manifest does.

Ollama's own walls were probed live on 11 October 2026, with the decider's
environment: a request carrying a web page's Origin, or a rebound Host,
was refused 403, and a pull was stopped at the refusing proxy. The model
itself is checked outside the suite, where it is installed: `python -m
core.screen redteam --local` runs the attack set through both local readers.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx2
import psutil
import pytest

from core import addons, decider, paths, screen

ROOT = Path(__file__).resolve().parents[2]
# The real one, taken at import: tests/gate/fences.py replaces it for each test.
_SPAWN = decider._spawn
FIXTURES = Path(__file__).parent / "fixtures" / "decider"
CAPTURED = json.loads((FIXTURES / "captured.json").read_text(encoding="utf-8"))
# The manifest as Ollama 0.40.3 kept it after pulling tev1:4b.
MANIFEST = (FIXTURES / "tev1-4b.manifest").read_bytes()
ITEM = screen.Artifact("email_body", "m1", "iris_novak@tailspin.example",
                       "Subject: Great chatting\n\nCould you confirm Tuesday?")


class _Process:
    """A server process, running until it exits."""

    def __init__(self, returncode=None):
        self.returncode, self.pid = returncode, 4321

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class _Ollama:
    """Ollama as captured: the servers started and ended, what each was
    asked, and what each path answers."""

    def __init__(self):
        self.spawned: list[tuple[bool, _Process]] = []
        self.ended = 0
        self.asked: list[tuple[str, dict | None]] = []
        self.version = CAPTURED["version"]
        self.answer = (200, CAPTURED["answer"])
        self.ps = CAPTURED["ps"]
        self.fail: Exception | None = None
        self.exits_at_start = False
        self.pulled: bytes | None = None
        # Whether another program holds the server's port (`served_by`).
        self.squatted = False

    def spawn(self, port, *, offline=True):
        process = _Process(1 if self.exits_at_start else None)
        self.spawned.append((offline, process))
        return process

    def served_by(self, port, pid):
        assert pid == self.spawned[-1][1].pid
        return not self.squatted

    def contain(self, process):
        def end():
            self.ended += 1
            process.returncode = 1

        return end

    def call(self, method, path, body=None, *, timeout=decider.TIMEOUT_S):
        self.asked.append((path, body))
        if path == "/api/version":
            if self.version is None:
                raise httpx2.ConnectError("refused")
            return httpx2.Response(200, json=self.version)
        if path == "/api/ps":
            return httpx2.Response(200, json=self.ps)
        if path == "/api/pull":
            decider._manifest().parent.mkdir(parents=True, exist_ok=True)
            decider._manifest().write_bytes(self.pulled)
            return httpx2.Response(200, json={"status": "success"})
        assert path == "/v1/systemone", path
        if self.fail:
            raise self.fail
        status, answer = self.answer
        if isinstance(answer, str):
            return httpx2.Response(status, text=answer)
        return httpx2.Response(status, json=answer)

    def requests(self):
        return [body for path, body in self.asked if path == "/v1/systemone"]


def _fake(monkeypatch) -> _Ollama:
    fake = _Ollama()
    monkeypatch.setattr(decider, "_spawn", fake.spawn)
    monkeypatch.setattr(decider, "_contain", fake.contain)
    monkeypatch.setattr(decider, "_call", fake.call)
    monkeypatch.setattr(decider.localonly, "served_by", fake.served_by)
    monkeypatch.setattr(decider, "_server", None)
    monkeypatch.setattr(decider, "_resting_until", 0.0)
    return fake


def _runtime(monkeypatch) -> Path:
    """A runtime in its add-on's home, with the pin taken from its files, as
    setup leaves one: what a start checks before it runs anything."""
    folder = addons.home(decider.RUNTIME_ADDON)
    (folder / "lib" / "ollama").mkdir(parents=True, exist_ok=True)
    (folder / "ollama.exe").write_bytes(b"MZ")
    (folder / "lib" / "ollama" / "ggml-base.dll").write_bytes(b"library")
    monkeypatch.setattr(decider, "RUNTIME_ADDON", dataclasses.replace(
        decider.RUNTIME_ADDON, tree=addons.tree_hash(folder)))
    return folder


@pytest.fixture
def ollama(monkeypatch):
    """A fake Ollama behind an installed decider."""
    monkeypatch.setattr(decider, "installed", lambda: True)
    _runtime(monkeypatch)
    return _fake(monkeypatch)


def test_a_runtime_changed_since_it_was_installed_is_never_started(ollama):
    """It was unpacked in the data folder, and only the presence of
    ollama.exe was checked: a program of the user's that could write there
    could replace it, and the next start ran it. Its files are hashed before
    every start, as signal-cli's are, and a changed runtime rests, since
    hashing it again for each item would take seconds each time."""
    (addons.home(decider.RUNTIME_ADDON) / "ollama.exe").write_bytes(b"MZ tampered")

    with pytest.raises(RuntimeError, match="has changed since it was installed"):
        decider.ask(screen.request_for(ITEM))
    with pytest.raises(RuntimeError, match="has changed since it was installed.*; resting"):
        decider.ask(screen.request_for(ITEM))
    assert ollama.spawned == [] and ollama.requests() == []

    # Setup puts the pinned files back (here they are put back by hand, so
    # nothing is downloaded), and the rest ends with it.
    (addons.home(decider.RUNTIME_ADDON) / "ollama.exe").write_bytes(b"MZ")
    decider.setup()
    assert len(ollama.spawned) == 1 and len(ollama.requests()) == 1


def test_the_runtime_is_an_add_on_and_the_model_stays_in_the_model_cache(monkeypatch,
                                                                         tmp_path):
    monkeypatch.setenv("STEVE_MODEL_CACHE", str(tmp_path / "models"))
    assert decider._exe().is_relative_to(paths.addons_dir())
    assert not decider._exe().is_relative_to(paths.models_dir())
    assert decider._manifest().is_relative_to(paths.models_dir())


def test_an_answer_is_ollamas_own_reply_to_the_screens_request(ollama):
    payload = screen.request_for(ITEM)

    answer = decider.ask(payload)

    assert answer == CAPTURED["answer"]
    assert ollama.requests() == [{**payload, "model": decider.MODEL}]


def test_a_part_over_the_models_limit_is_refused_as_too_long_never_cut(ollama):
    """Tev1 reads at most 2,050 tokens and says so rather than cut the part:
    the screen reads it in halves instead. The server is fine."""
    ollama.answer = (CAPTURED["too_long"]["status"], CAPTURED["too_long"]["body"])

    with pytest.raises(decider.TooLong, match="10352 tokens"):
        decider.ask(screen.request_for(ITEM))
    assert ollama.ended == 0


def test_any_other_refusal_comes_back_in_ollamas_own_words(ollama):
    ollama.answer = (CAPTURED["missing_model"]["status"], CAPTURED["missing_model"]["body"])

    with pytest.raises(RuntimeError, match='HTTP 404: model "tev1:9b" not found'):
        decider.ask(screen.request_for(ITEM))


def test_an_answer_from_any_model_but_the_pinned_one_goes_unused(ollama):
    """Ollama has no login: anything on the laptop that reaches its port can
    put a model of its own under the name tev1:4b. Its digest gives it
    away."""
    ollama.ps = {"models": [{**CAPTURED["ps"]["models"][0], "digest": "0" * 64}]}

    with pytest.raises(RuntimeError, match="not the pinned"):
        decider.ask(screen.request_for(ITEM))
    assert ollama.ended == 1


def test_an_answer_from_a_model_spilled_onto_the_cpu_goes_unused_and_it_rests(ollama):
    """With the GPU short -- a game, a training run -- Ollama puts what does
    not fit on the CPU, where it would hold their cores beside the desk's audio
    for seconds a part, and load again for every item."""
    ollama.ps = {"models": [{**CAPTURED["ps"]["models"][0], "size_vram": 2_000_000_000}]}
    payload = screen.request_for(ITEM)

    with pytest.raises(RuntimeError, match="did not fit on the GPU"):
        decider.ask(payload)
    ollama.ps = CAPTURED["ps"]
    with pytest.raises(RuntimeError, match="resting"):
        decider.ask(payload)

    assert ollama.ended == 1 and len(ollama.spawned) == 1, "not started again while it rests"
    decider._resting_until = 0.0
    assert decider.ask(payload) == CAPTURED["answer"] and len(ollama.spawned) == 2


def test_one_offline_server_serves_every_request_until_it_dies(ollama):
    payload = screen.request_for(ITEM)

    decider.ask(payload)
    decider.ask(payload)
    assert len(ollama.spawned) == 1
    ollama.spawned[0][1].returncode = 3
    decider.ask(payload)

    assert len(ollama.spawned) == 2 and all(offline for offline, _ in ollama.spawned)


def test_a_server_is_used_only_once_it_answers_as_the_pinned_version(ollama):
    """The port is free when it is chosen and the server's a moment later: a
    program that took it in between would otherwise be asked about their
    mail."""
    ollama.version = {"version": "0.41.0"}

    with pytest.raises(RuntimeError, match="not Ollama 0.40.3"):
        decider.ask(screen.request_for(ITEM))
    assert ollama.ended == 1 and ollama.requests() == []


def test_a_port_taken_by_another_program_that_answers_as_ollama_is_asked_nothing(ollama):
    """A version is easy to fake: another Windows account's program that
    took the port first could answer as the pinned Ollama, then be sent the
    user's mail and answer for the model (step 11's audit). Whose the
    listener is decides, and the server started here is ended."""
    ollama.squatted = True

    with pytest.raises(RuntimeError, match="served by another program"):
        decider.ask(screen.request_for(ITEM))
    assert ollama.ended == 1 and ollama.requests() == []


def test_a_server_that_exits_while_starting_is_reported_with_its_log(ollama, monkeypatch,
                                                                     tmp_path):
    log = tmp_path / "ollama.log"
    log.write_text("Error: unable to load the CUDA library", encoding="utf-8")
    monkeypatch.setattr(decider, "log_path", lambda: log)
    ollama.version = None
    ollama.exits_at_start = True

    with pytest.raises(RuntimeError, match="exited with 1: Error: unable to load the CUDA"):
        decider.ask(screen.request_for(ITEM))
    assert ollama.ended == 1


def test_a_stuck_server_is_ended_so_the_next_item_starts_a_fresh_one(ollama):
    payload = screen.request_for(ITEM)
    ollama.fail = httpx2.ReadTimeout("timed out")

    with pytest.raises(httpx2.ReadTimeout):
        decider.ask(payload)
    ollama.fail = None
    decider.ask(payload)

    assert ollama.ended == 1 and len(ollama.spawned) == 2


def test_one_request_at_a_time(ollama, monkeypatch):
    """Screens run on threads. Two items arriving together while no server
    ran would otherwise each start one, and the second would take the
    first's place: a server nothing ends, holding 5 GB more of the GPU."""
    active = {"now": 0, "most": 0}
    reply = ollama.call

    def slow(method, path, body=None, *, timeout=decider.TIMEOUT_S):
        active["now"] += 1
        active["most"] = max(active["most"], active["now"])
        time.sleep(0.02)
        active["now"] -= 1
        return reply(method, path, body, timeout=timeout)

    monkeypatch.setattr(decider, "_call", slow)
    threads = [threading.Thread(target=decider.ask, args=(screen.request_for(ITEM),))
               for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert active["most"] == 1 and len(ollama.requests()) == 3


def test_the_server_is_given_nothing_of_the_workers_environment_and_no_way_out(monkeypatch,
                                                                               tmp_path):
    """Ollama is a program from outside: the worker's environment is not its
    to read, and once installed it needs nothing from the network -- its one
    call out while running is a fetch of model recommendations."""
    monkeypatch.setenv("STEVE_MODEL_CACHE", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-for-ollama")

    env = decider._environment(54321)

    assert not [k for k, v in env.items() if "sk-not" in v or k == "OPENAI_API_KEY"]
    assert env["OLLAMA_HOST"] == "127.0.0.1:54321" and env["OLLAMA_NO_CLOUD"] == "1"
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] == "http://127.0.0.1:9"
    private = str(tmp_path / "ollama" / "home")
    assert {env[k] for k in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP",
                             "TMP")} == {private}
    assert env["OLLAMA_MODELS"] == str(tmp_path / "ollama" / "models")
    assert "HTTPS_PROXY" not in decider._environment(54321, offline=False), (
        "only setup's pull reaches the registry")


def _starting(monkeypatch, tmp_path) -> dict:
    """`_spawn` as it is, in a cache and a log of the test's own, with a
    Popen that records what it was given."""
    monkeypatch.setenv("STEVE_MODEL_CACHE", str(tmp_path))
    monkeypatch.setattr(decider, "log_path", lambda: tmp_path / "logs" / "ollama.log")
    started = {}

    def popen(args, **kwargs):
        started.update(args=args, **kwargs)
        return "the server"

    monkeypatch.setattr(decider, "subprocess", SimpleNamespace(
        Popen=popen, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT,
        CREATE_NO_WINDOW=subprocess.CREATE_NO_WINDOW))
    return started


def test_the_server_starts_with_that_environment_and_its_output_goes_to_the_log(monkeypatch,
                                                                              tmp_path):
    """Never to a pipe, which a server nobody reads stops on once it fills
    (docs/protocols.md §5)."""
    started = _starting(monkeypatch, tmp_path)

    assert _SPAWN(54321) == "the server"
    assert started["args"] == [str(decider._exe()), "serve"]
    assert started["env"] == decider._environment(54321)
    assert started["stdout"].name == str(decider.log_path())
    assert started["stderr"] == subprocess.STDOUT
    assert started["stdin"] == subprocess.DEVNULL


def test_the_log_is_kept_until_it_passes_its_size_and_then_emptied(monkeypatch, tmp_path):
    """Each start adds about 70 KB, and an outage of days starts one every
    few minutes."""
    _starting(monkeypatch, tmp_path)
    monkeypatch.setattr(decider, "LOG_BYTES", 100)
    decider.log_path().parent.mkdir(parents=True)

    decider.log_path().write_bytes(b"x" * 100)
    _SPAWN(54321)
    assert decider.log_path().stat().st_size == 100

    decider.log_path().write_bytes(b"x" * 101)
    _SPAWN(54321)
    assert decider.log_path().stat().st_size == 0


def test_ending_the_server_ends_everything_it_started():
    """Ollama runs each model in a process of its own, started after the
    server is in the job. The base interpreter, not the venv's: that one is
    a launcher, which starts the real interpreter at once -- before any job
    could hold it."""
    child = ("import subprocess, sys, time; sys.stdin.readline(); "
             "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
             "time.sleep(60)")
    def runners() -> list[psutil.Process]:
        # A console program's conhost.exe is its child from the start. The
        # wait took it for the runner, and checked the conhost's end instead;
        # on CI (22 October 2026) the runner came as soon, and there were two.
        return [p for p in psutil.Process(process.pid).children() if p.name() != "conhost.exe"]

    with subprocess.Popen([sys._base_executable, "-I", "-c", child], stdin=subprocess.PIPE,
                          creationflags=subprocess.CREATE_NO_WINDOW) as process:
        try:
            end = decider._contain(process)
            process.stdin.write(b"started\n")
            process.stdin.flush()
            deadline = time.monotonic() + 20
            while not runners() and time.monotonic() < deadline:
                time.sleep(0.05)
            [runner] = runners()

            end()

            assert process.wait(timeout=10) == 1, "ended by the job"
            psutil.wait_procs([runner], timeout=10)
            assert not runner.is_running()
        finally:
            process.kill()


def test_the_server_starts_suspended_and_runs_once_its_job_holds_it(monkeypatch, tmp_path):
    """Put in its job only once it had begun, as until 22 October 2026, a
    server could start a process in between that stayed outside, and
    nothing would end it."""
    from core.sandbox import win32

    started = _starting(monkeypatch, tmp_path)
    _SPAWN(54321)
    assert started["creationflags"] & win32.CREATE_SUSPENDED

    ran = tmp_path / "ran"
    with subprocess.Popen([sys._base_executable, "-I", "-c", f"open({str(ran)!r}, 'w').close()"],
                          creationflags=subprocess.CREATE_NO_WINDOW | win32.CREATE_SUSPENDED,
                          ) as process:
        try:
            time.sleep(0.5)
            assert not ran.exists(), "it ran before its job held it"
            end = decider._contain(process)
            assert process.wait(timeout=20) == 0, "let run once its job held it"
            assert ran.exists()
            end()
        finally:
            process.kill()


def test_a_worker_that_exits_takes_its_server_with_it():
    """However the worker ends -- swapped out, crashed, killed -- its handle
    to the job closes with it, and the job ends the server: one left behind
    would hold 5 GB of the GPU until the user noticed."""
    worker = ("import os, subprocess, sys; from core import decider; "
              "server = subprocess.Popen([sys.argv[1], '-c', 'import time; time.sleep(60)'], "
              "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000); "
              "decider._contain(server); print(server.pid, flush=True); os._exit(0)")
    done = subprocess.run([sys.executable, "-c", worker, sys._base_executable], cwd=ROOT,
                          capture_output=True, text=True, timeout=60,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    pid = int(done.stdout.split()[-1])
    try:
        deadline = time.monotonic() + 10
        while psutil.pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not psutil.pid_exists(pid), "the server outlived its worker"
    finally:
        with contextlib.suppress(psutil.Error):
            psutil.Process(pid).kill()


def test_installed_means_the_pinned_runtime_and_the_manifest_as_pinned(monkeypatch, tmp_path):
    """The manifest names every layer by its digest, so its hash pins them
    all. Off Windows, where the pinned runtime cannot run, it is never
    installed."""
    monkeypatch.setenv("STEVE_MODEL_CACHE", str(tmp_path))
    decider._exe().parent.mkdir(parents=True)
    decider._exe().write_bytes(b"MZ")
    decider._manifest().parent.mkdir(parents=True)
    decider._manifest().write_bytes(MANIFEST.replace(b"4482403072", b"4482403073"))
    assert not decider.installed()

    decider._manifest().write_bytes(MANIFEST)
    assert decider.installed()

    # The whole process's platform, so for one call only.
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", "linux")
        off_windows = decider.installed()
    assert not off_windows


def _release(tampered: bool = False) -> bytes:
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as z:
        z.writestr("ollama.exe", b"MZ tampered" if tampered else b"MZ")
        z.writestr("lib/ollama/ggml-base.dll", b"library")
    return body.getvalue()


def _pin(monkeypatch, release: bytes, folder: Path) -> None:
    """The runtime pinned to `release`: its size and hash, and the hash of
    the files it unpacks to (here, into `folder`)."""
    with zipfile.ZipFile(io.BytesIO(release)) as z:
        z.extractall(folder)
    monkeypatch.setattr(decider, "RUNTIME_ADDON", dataclasses.replace(
        decider.RUNTIME_ADDON, size=len(release), sha256=hashlib.sha256(release).hexdigest(),
        tree=addons.tree_hash(folder)))


def test_setup_installs_only_the_release_as_pinned_and_replaces_a_changed_runtime(
        monkeypatch, tmp_path):
    """Setup took any ollama.exe already there as installed, whatever it
    was, and ran it with the network open to pull the model. The runtime is
    now installed as an add-on: the release checked before it is unpacked,
    its files before they are put in place, and a changed one replaced."""
    monkeypatch.setenv("STEVE_MODEL_CACHE", str(tmp_path / "models"))
    good = _release()
    _pin(monkeypatch, good, tmp_path / "pinned")
    served = {"body": _release(tampered=True)}
    asked: list[str] = []

    def download(url, to, *, most, progress=None):
        asked.append(url)
        to.write_bytes(served["body"])
        return hashlib.sha256(served["body"]).hexdigest()

    monkeypatch.setattr(addons, "_download", download)
    ollama = _fake(monkeypatch)
    ollama.pulled = MANIFEST

    with pytest.raises(addons.AddonError, match="did not download as published"):
        decider.setup()
    assert not decider._exe().exists()
    assert asked == [decider.RUNTIME_URL] and ollama.spawned == []

    served["body"] = good
    decider.setup()

    assert decider._exe().read_bytes() == b"MZ" and decider.installed()
    assert not list(paths.scratch_dir().glob("ollama-*")), "the release is not kept"
    assert [offline for offline, _ in ollama.spawned] == [False, True], (
        "online only to pull; the warm-up is offline")
    assert [path for path, _ in ollama.asked if path in ("/api/pull", "/v1/systemone")] == [
        "/api/pull", "/v1/systemone"]
    assert ollama.ended == 2 and decider._server is None

    decider._exe().write_bytes(b"MZ tampered")
    decider.setup()
    assert decider._exe().read_bytes() == b"MZ" and len(asked) == 3


def test_setup_refuses_a_pulled_model_whose_manifest_is_not_the_pinned_one(monkeypatch,
                                                                           tmp_path):
    """A tag moved upstream is a different model, however it is named, and
    it is never asked anything."""
    monkeypatch.setenv("STEVE_MODEL_CACHE", str(tmp_path))
    _runtime(monkeypatch)
    ollama = _fake(monkeypatch)
    ollama.pulled = MANIFEST.replace(b"Tev1-4B", b"Tev2-4B")

    with pytest.raises(RuntimeError, match="manifest hashed"):
        decider.setup()
    assert not decider.installed() and ollama.requests() == []
