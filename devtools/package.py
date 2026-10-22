"""Steve's installed tree, built from a checkout (08-packaging.md, section 1).

    python -m devtools.package build [--out build] [--dirty]
    python -m devtools.package smoke [--out build]
    python -m devtools.package compare FOLDER FOLDER
    python -m devtools.package manifest --key KEY [--out build]
    python -m devtools.package scan APP [APP ...]

`build` writes `<out>/staging/`: `backend/`, a Python with the locked
dependencies and Steve's code in `backend/app/`, and `runtime/`, the module
runtime. The app's build takes that folder as its resources (section 3), and
`installed.json` in `backend/app/` is what makes it an install
(`core/paths.is_installed`).

What it prevents:

- **A backend that runs only where it was built.** The interpreter is
  python-build-standalone's, pinned by URL and hashes as an add-on is
  (`core/addons.py`), and the packages are the lock's, each file checked by
  its hash. Nothing comes from this computer's own Python, and there is no
  venv: a venv's `pyvenv.cfg` and its launchers name the folder they were
  made in, and break when it moves to a stranger's AppData.
- **Code without a file it reads.** Steve's packages are copied whole, as the
  checkout lays them out, so every read by `paths.SOURCE / ...` finds its
  file. A wheel would have needed a list of package data that falls behind
  each new prompt, and would have made `db` a top-level package.
- **Two builds of one commit that differ.** Spike S2 found 27 files that
  did: launchers and `RECORD` lines naming the build's own folder,
  `direct_url.json`, and uv's timestamped `uv_cache.json`. They go, files are
  copied without their times, and bytecode is unchecked-hash `.pyc` with the
  build's folder stripped from it. `compare` checks two builds file by file.
- **A release of edits nobody committed.** A checkout with changes is
  refused, unless `--dirty` says it is a developer's build; `installed.json`
  then says so.
- **An install that refuses itself.** `manifest` lists every file of the
  tree with its SHA-256 and signs the list with the update key, and the app
  checks it before it starts the backend (`ui/src-tauri/src/integrity.rs`,
  section 4). A list signed by any key but the one the app is built to
  trust would stop every user's backend; it is refused here instead.
- **A release that names its builder.** A release build took the folder it
  was built in as its checkout, and so named the builder's Windows account
  (the step 11 audit, F7); the compiler names each library's source there
  too, unless the build remaps the profile's folder (README, "Building the
  installer"). `scan` fails on an app that names the builder's profile or
  this checkout.

`smoke` moves the built tree to a folder whose name has a space and an
accent, and runs it with an environment holding only what Windows gives
every process: its migrations apply, its prompts and manifests load, and
its supervisor reports a healthy worker.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

from core import addons, identity, paths
from core.sandbox import runtime

# python-build-standalone's CPython for Windows x64, the install_only build:
# no venv, so it moves. Spike S2 chose and timed it
# (docs/plan/spikes/s2-packaging.md). A security release is a new tag here,
# with its size and hashes.
INTERPRETER = addons.Addon(
    name="python",
    version="3.13.15+20260901",
    url="https://github.com/astral-sh/python-build-standalone/releases/download/20260901/"
        "cpython-3.13.15%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz",
    size=47_042_104,
    sha256="9bcc038a0bf180612ed56dec93d4977d035e80b8d9320ef51a38c287baf134b7",
    top="python",
    tree="f0565dfd1c8e13e140f97e87b1ca7171c3ee23ed5882df2aa913fe1e7679b012",
    what="Python",
)

# Steve's code, and the files outside it that it reads by path:
# agent/modbuild.py hands docs/modules.md to the coder, and the night's
# model trials (agent/models_eval.py) and weekly yardstick (core/yardstick.py)
# read the gate's red-team payloads and autonomy scenarios; left behind until
# 21 October 2026, an install's trials failed on a missing file. Everything
# else in a checkout stays behind -- the rest of tests/,
# devtools/, ui/, the rest of docs/, and pyproject.toml, whose absence is
# half of what tells an install from a checkout (core/paths.py).
PACKAGES = ("agent", "channels", "core", "db", "modules", "steve_sdk", "tools", "voice")
FILES = ("docs/modules.md", "tests/gate/fixtures/injection/redteam.json",
         "tests/gate/fixtures/autonomy/scenarios.json", "LICENSE", "NOTICE")
EXTRAS = ("accounts",)

# The Google APIs Steve's module builds clients for (modules/google_cloud):
# the discovery documents of the other six hundred are 108 MB.
GOOGLE_APIS = ("calendar.v3", "drive.v3", "gmail.v1", "sheets.v4", "tasks.v1")
DISCOVERY = Path("Lib/site-packages/googleapiclient/discovery_cache/documents")

# What a backend never runs: the GUI and its Tcl, the installers, headers
# and import libraries, the standard library's own tests, pip, launchers
# that name the build's folder, and the debug symbols, which are 85 of the
# archive's 152 MB.
TRIMMED = ("tcl", "include", "libs", "Scripts", "Lib/idlelib", "Lib/tkinter", "Lib/turtledemo",
           "Lib/ensurepip", "Lib/test", "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll",
           "DLLs/tk86t.dll", "DLLs/_ctypes_test.pyd")
TRIMMED_GLOBS = ("**/*.pdb", "DLLs/_test*.pyd", "DLLs/xxlimited*.pyd",
                 "Lib/site-packages/*.dist-info/direct_url.json",
                 "Lib/site-packages/*.dist-info/uv_cache.json")

# The four files besides core/identity.py that carry Steve's version.
VERSIONED = ("pyproject.toml", "ui/src-tauri/tauri.conf.json", "ui/src-tauri/Cargo.toml",
             "ui/package.json")

Run = Callable[..., subprocess.CompletedProcess]


class BuildError(RuntimeError):
    """The build cannot go on, and why."""


def _run(args, **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("check", True)
    return subprocess.run([str(a) for a in args], **kwargs)


def _uv_env() -> dict[str, str]:
    """uv held to the lock and to this build: no Python of its own (those
    come from a mirror, not GitHub), no user's config, files copied out of
    its cache rather than linked, and three at a time on a shared laptop."""
    return {**os.environ, "UV_PYTHON_DOWNLOADS": "never", "UV_NO_CONFIG": "1",
            "UV_LINK_MODE": "copy", "UV_CONCURRENT_INSTALLS": "3",
            "UV_CONCURRENT_DOWNLOADS": "3", "UV_CONCURRENT_BUILDS": "1",
            "PYTHONDONTWRITEBYTECODE": "1"}


def versions(checkout: Path) -> dict[str, str]:
    """The version each of `VERSIONED` states."""
    found = {
        "pyproject.toml": tomllib.loads((checkout / VERSIONED[0]).read_text("utf-8"))
        ["project"]["version"],
        VERSIONED[1]: json.loads((checkout / VERSIONED[1]).read_text("utf-8"))["version"],
        VERSIONED[2]: tomllib.loads((checkout / VERSIONED[2]).read_text("utf-8"))
        ["package"]["version"],
        VERSIONED[3]: json.loads((checkout / VERSIONED[3]).read_text("utf-8"))["version"],
    }
    return found


def commit_of(checkout: Path, run: Run = _run) -> tuple[str, bool]:
    """The checkout's commit, and whether anything differs from it."""
    sha = run(["git", "-C", checkout, "rev-parse", "HEAD"], capture_output=True,
              text=True).stdout.strip()
    changed = run(["git", "-C", checkout, "status", "--porcelain"], capture_output=True,
                  text=True).stdout.strip()
    return sha, bool(changed)


def code_files(checkout: Path, run: Run = _run) -> list[str]:
    """Every file of `PACKAGES` and `FILES` git knows or would add, by its
    path in the checkout: never a `__pycache__`, a test's leftovers or
    anything else ignored."""
    listed = run(["git", "-C", checkout, "ls-files", "-z", "--cached", "--others",
                  "--exclude-standard", "--", *PACKAGES, *FILES],
                 capture_output=True).stdout.decode("utf-8")
    return sorted(rel for rel in set(listed.split("\0")) if rel and (checkout / rel).is_file())


def copy_code(checkout: Path, app: Path, run: Run = _run) -> int:
    """Steve's code into `app`, laid out as in the checkout: how many files."""
    files = code_files(checkout, run)
    for rel in files:
        target = app / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / rel, target)
    return len(files)


def write_pth(backend: Path) -> Path:
    """`steve.pth`, which puts `backend/app/` on the path. Relative, so the
    folder can move: site joins each line to site-packages. Python reads it
    even with `-I`."""
    pth = backend / "Lib" / "site-packages" / "steve.pth"
    pth.write_text("../../app\n", encoding="ascii", newline="\n")
    return pth


def trim(backend: Path) -> None:
    """What a backend never runs, and the `RECORD` lines naming it."""
    for rel in TRIMMED:
        target = backend / rel
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    for pattern in TRIMMED_GLOBS:
        for target in backend.glob(pattern):
            target.unlink()
    documents = backend / DISCOVERY
    if documents.is_dir():
        for document in documents.glob("*.json"):
            if document.stem not in GOOGLE_APIS:
                document.unlink()
    _drop_records(backend / "Lib" / "site-packages", "../../Scripts/")


def trim_testing(testing: Path) -> None:
    """pytest's folder for the module runtime, without the launchers uv
    writes into its `bin/`: the runtime calls `pytest.main`, and each
    launcher names the folder it was built in, so two builds differed by
    them."""
    shutil.rmtree(testing / "bin", ignore_errors=True)
    for pattern in TRIMMED_GLOBS:
        for target in testing.glob(pattern):
            target.unlink()
    _drop_records(testing, "bin/")


def _drop_records(site: Path, launchers: str) -> None:
    """The `RECORD` lines for the launchers and uv's files, which are gone."""
    for record in site.glob("*.dist-info/RECORD"):
        lines = record.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [line for line in lines if not line.startswith(launchers)
                and "/direct_url.json," not in line and "/uv_cache.json," not in line]
        if kept != lines:
            record.write_text("".join(kept), encoding="utf-8", newline="")


def _requirement_blocks(text: str) -> dict[str, str]:
    """`uv export`'s requirements, by package name: each with its hashes."""
    blocks: dict[str, str] = {}
    current: list[str] = []
    for line in text.splitlines():
        if current:
            current.append(line)
        elif line and not line[0].isspace() and not line.startswith("#"):
            current = [line]
        if current and not line.endswith("\\"):
            name = re.match(r"[A-Za-z0-9_.\-]+", current[0]).group(0)
            blocks[re.sub(r"[-_.]+", "-", name).lower()] = "\n".join(current)
            current = []
    return blocks


def compile_tree(python: Path, folder: Path, run: Run = _run) -> None:
    """Bytecode beside every source, as unchecked-hash `.pyc`: never
    compared with the source's time, which an installer or a copy changes,
    and never rewritten (the backend runs with `-B`). Without it the first
    start took 25 s. The build's own folder is stripped from it.

    In one process, in compileall's sorted order, with a fixed hash seed.
    `marshal` marks an object for sharing by how many references it has,
    and that depends on what the same process compiled before: with `-j 3`
    two builds of one commit wrote three sizes of pycparser's parser. One
    process twice gave 7,063 identical files, in about 90 s. Not `-I`, which
    would ignore the seed: `-s -P` and an environment with no `PYTHON*`
    keep the developer's Python out instead."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("PYTHON")}
    done = run([python, "-s", "-P", "-m", "compileall", "-q", "-f",
                "--invalidation-mode", "unchecked-hash", "-s", folder, folder],
               env={**env, "PYTHONHASHSEED": "0"}, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise BuildError(f"compileall failed in {folder}:\n{done.stdout[-3000:]}")


_RUNTIME_BUILD = ("import sys, pathlib; sys.path.append(sys.argv[1]); "
                  "from core.sandbox import runtime; "
                  "runtime.build(pathlib.Path(sys.argv[2]), grant=False, say=lambda line: None)")


def build(out: Path, *, checkout: Path | None = None, dirty: bool = False,
          say: Callable[[str], None] = print, run: Run = _run) -> Path:
    """Build the installed tree into `out/staging`, and return it."""
    checkout = checkout or paths.SOURCE
    commit, changed = commit_of(checkout, run)
    if changed and not dirty:
        raise BuildError("the checkout has changes nobody committed: commit them, or pass"
                         " --dirty for a developer's build")
    if drift := {name: v for name, v in versions(checkout).items() if v != identity.VERSION}:
        raise BuildError(f"the version is {identity.VERSION} in core/identity.py, but"
                         f" {', '.join(f'{v} in {name}' for name, v in drift.items())}")
    out.mkdir(parents=True, exist_ok=True)
    staging = out / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    backend = staging / "backend"
    python = backend / "python.exe"

    say(f"Python {INTERPRETER.version}, checked against its pinned hashes")
    shutil.copytree(addons.install(INTERPRETER, folder=out / "cache" / "python"), backend)

    say("the locked dependencies, each file checked against its hash")
    extras = [arg for extra in EXTRAS for arg in ("--extra", extra)]
    exported = run(["uv", "export", "--frozen", "--no-emit-project", "--format",
                    "requirements-txt", *extras], cwd=checkout, env=_uv_env(),
                   capture_output=True, text=True).stdout
    requirements = out / "requirements.txt"
    requirements.write_text(exported, encoding="utf-8", newline="\n")
    run(["uv", "pip", "sync", "--python", python, "--require-hashes", requirements],
        env=_uv_env(), capture_output=True)
    run(["uv", "pip", "uninstall", "--python", python, "pip"], env=_uv_env(),
        capture_output=True)

    say(f"Steve's code into {backend / 'app'}")
    copied = copy_code(checkout, backend / "app", run)
    write_pth(backend)
    trim(backend)
    say(f"  {copied} files")

    say("the module runtime, with pytest for modules' own tests")
    testing = out / "testing"
    if testing.exists():
        shutil.rmtree(testing)
    wanted = runtime.required(runtime.TESTING)
    with_dev = run(["uv", "export", "--frozen", "--no-emit-project", "--format",
                    "requirements-txt", "--extra", "dev"], cwd=checkout, env=_uv_env(),
                   capture_output=True, text=True).stdout
    blocks = _requirement_blocks(with_dev)
    if missing := [name for name in wanted if name not in blocks]:
        raise BuildError(f"the lock has no {', '.join(missing)}")
    (out / "testing.txt").write_text("\n".join(blocks[name] for name in wanted) + "\n",
                                     encoding="utf-8", newline="\n")
    run(["uv", "pip", "install", "--python", python, "--target", testing, "--no-deps",
         "--require-hashes", "-r", out / "testing.txt"], env=_uv_env(), capture_output=True)
    trim_testing(testing)
    shutil.copytree(addons.install(runtime.NODE, folder=out / "cache" / "node"),
                    staging / "runtime" / "node")
    run([python, "-I", "-B", "-c", _RUNTIME_BUILD, testing, staging / "runtime"],
        env={**os.environ, "STEVE_DATA_DIR": str(out / "data")}, capture_output=True)

    say("bytecode")
    compile_tree(python, backend, run)
    compile_tree(python, staging / "runtime" / "python", run)

    lock = hashlib.sha256((checkout / "uv.lock").read_bytes()).hexdigest()
    (backend / "app" / paths.INSTALLED_MARKER).write_text(json.dumps({
        "version": identity.VERSION, "python": INTERPRETER.version, "lock": lock,
        "commit": commit, "dirty": changed}, indent=1, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    say(f"built {staging}")
    return staging


# Run inside the built backend by `smoke`: what a first start reads.
_SMOKE = """
import json
from core import db, paths
from core.policy import load_policy
from steve_sdk import manifest
from agent.features import everything
from agent.loop import system_prompt

assert paths.layout() == "installed", paths.SOURCE
conn = db.open_store()
tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
conn.close()
manifests = sorted(paths.SOURCE.glob("modules/*/manifest.toml")) \\
    + sorted(paths.SOURCE.glob("steve_sdk/templates/*/manifest.toml"))
print(json.dumps({"data": str(paths.data_dir()), "tables": tables,
                  "manifests": [manifest.load(m).id for m in manifests],
                  "prompt": len(system_prompt(everything())),
                  "policy": bool(load_policy())}))
"""

# What Windows gives every process, and nothing a user or a developer set:
# no PATH beyond System32, no PYTHON*, no STEVE_*, no provider keys.
_KEPT = ("SystemRoot", "windir", "SystemDrive", "COMPUTERNAME", "USERNAME", "USERDOMAIN",
         "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "ComSpec", "PATHEXT")
SMOKE_TIMEOUT_S = 180.0


def bare_env(local: Path, temp: Path) -> dict[str, str]:
    """An environment with only what Windows gives every process, its
    LOCALAPPDATA and TEMP moved to `local` and `temp`."""
    env = {name: os.environ[name] for name in _KEPT if name in os.environ}
    system = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    temp.mkdir(parents=True, exist_ok=True)
    env |= {"PATH": str(system), "LOCALAPPDATA": str(local), "TEMP": str(temp),
            "TMP": str(temp), "USERPROFILE": os.environ.get("USERPROFILE", str(local.parent)),
            "APPDATA": str(local.parent / "Roaming")}
    return env


def smoke(staging: Path, out: Path, *, say: Callable[[str], None] = print) -> dict:
    """The built tree, copied as an installer would to a folder named with a
    space and an accent, run with a bare environment: what it read, and its
    supervisor's health. BuildError if any of it fails."""
    import httpx

    place = out / "smoke"
    if place.exists():
        shutil.rmtree(place)
    local = place / "Stévé Ünïcode" / "Local"
    tree = local / "Programs" / identity.NAME
    shutil.copytree(staging, tree)
    env = bare_env(local, place / "temp")
    python = tree / "backend" / "python.exe"
    app = tree / "backend" / "app"

    read = subprocess.run([python, "-I", "-B", "-c", _SMOKE], cwd=app, env=env,
                          capture_output=True, text=True, timeout=SMOKE_TIMEOUT_S, check=False)
    if read.returncode != 0:
        raise BuildError(f"the built backend could not read itself:\n{read.stderr[-3000:]}")
    found = json.loads(read.stdout.strip().splitlines()[-1])
    say(f"read: {found}")

    log = (place / "supervisor.log").open("wb")
    proc = subprocess.Popen([python, "-I", "-B", "-m", "core.supervisor"], cwd=app, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    run_file = local / identity.NAME / "run" / "backend.json"
    try:
        deadline = time.monotonic() + SMOKE_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                raise BuildError(f"the built supervisor exited {proc.returncode}"
                                 f" (log: {place / 'supervisor.log'})")
            if time.monotonic() > deadline:
                raise BuildError(f"the built supervisor reported no healthy worker in"
                                 f" {SMOKE_TIMEOUT_S:.0f} s (log: {place / 'supervisor.log'})")
            try:
                published = json.loads(run_file.read_text(encoding="utf-8"))
                base = f"http://127.0.0.1:{published['port']}"
                headers = {"Authorization": f"Bearer {published['token']}"}
                health = httpx.get(f"{base}/_sup/health", headers=headers, timeout=2.0)
                if health.status_code == 200 and health.json().get("ok"):
                    found["health"] = health.json()
                    break
            except (OSError, ValueError, KeyError, httpx.HTTPError):
                pass
            time.sleep(0.5)
        say(f"healthy: {base}")
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{base}/_sup/stop", headers=headers, timeout=5.0)
        proc.wait(30)
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, check=False)
        log.close()
    return found


def files_of(folder: Path) -> dict[str, str]:
    """Every file under `folder`, by its path there: its SHA-256."""
    return {path.relative_to(folder).as_posix(): addons.file_sha256(path)
            for path in sorted(folder.rglob("*")) if path.is_file()}


def compare(a: Path, b: Path) -> list[str]:
    """Each file that differs between two builds, or is in one only."""
    left, right = files_of(a), files_of(b)
    return sorted(name for name in left.keys() | right.keys() if left.get(name) != right.get(name))


MANIFEST = "manifest.json"
LISTED = ("backend", "runtime")


def write_manifest(staging: Path) -> Path:
    """`manifest.json` beside the tree: every file under `backend/` and
    `runtime/` by its path from the install's folder, with its SHA-256, and
    the version, which the app compares with its own."""
    files = {f"{top}/{name}": digest
             for top in LISTED for name, digest in files_of(staging / top).items()}
    if not files:
        raise BuildError(f"{staging} holds no tree to list: build it first")
    path = staging / MANIFEST
    path.write_text(json.dumps({"files": files, "version": identity.VERSION},
                               indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return path


def trusted_key(checkout: Path) -> str:
    """The update key the app is built to trust, as Tauri's configuration holds it."""
    config = checkout / "ui" / "src-tauri" / "tauri.conf.json"
    return json.loads(config.read_text(encoding="utf-8"))["plugins"]["updater"]["pubkey"]


def _key_id(stored: str) -> bytes:
    """The id of the key in a minisign public key or signature, as Tauri stores
    either: base64 of minisign's text, whose second line is base64 of two bytes
    naming the algorithm, the key's eight-byte id, then the key or signature."""
    lines = base64.b64decode(stored).decode("utf-8").splitlines()
    return base64.b64decode(lines[1])[2:10]


def sign(manifest: Path, key: Path, *, checkout: Path | None = None, trusted: str | None = None,
         run: Run = _run) -> Path:
    """Sign the list with the update key's private half, as the updater's
    files are signed (minisign, by Tauri's own signer), and return the
    signature. A key with a password takes it from
    `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, never the command line.

    The signature must be by the key the app is built to trust: `trusted`,
    a public key as Tauri stores it, for a build given a key of its own
    (CI's installer job makes one for its run); otherwise tauri.conf.json's."""
    checkout = checkout or paths.SOURCE
    signer = checkout / "ui" / "node_modules" / ".bin" / "tauri.cmd"
    if not signer.is_file():
        raise BuildError(f"no Tauri CLI at {signer}: run `npm ci` in ui/")
    signature = manifest.with_name(manifest.name + ".sig")
    signature.unlink(missing_ok=True)
    password = [] if os.environ.get("TAURI_SIGNING_PRIVATE_KEY_PASSWORD") else ["--password", ""]
    done = run([signer, "signer", "sign", "--private-key-path", key, *password, manifest],
               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
    if done.returncode != 0 or not signature.is_file():
        raise BuildError(f"the signer failed:\n{(done.stderr or done.stdout)[-2000:]}")
    if _key_id(signature.read_text(encoding="ascii")) != _key_id(trusted or trusted_key(checkout)):
        signature.unlink()
        named = ("the --pubkey given" if trusted
                 else "`plugins.updater.pubkey` in ui/src-tauri/tauri.conf.json")
        raise BuildError(f"{key} is not the update key the app trusts ({named}):"
                         " the app would refuse every file of this build")
    return signature


def builders_folders(checkout: Path = paths.SOURCE) -> list[str]:
    """The folders of this computer that a release must not name: the
    checkout it is built from, and the profile, which names the account."""
    folders = [str(checkout)]
    if profile := os.environ.get("USERPROFILE"):
        folders.append(profile)
    return folders


def scan(app: Path, folders: list[str]) -> list[str]:
    """Those of `folders` that `app` names: as UTF-8 or UTF-16 text, with
    either slash or doubled backslashes, with or without the drive, and in
    any case."""
    data = app.read_bytes()
    lowered = data.lower()
    named = []
    for folder in folders:
        bare = folder.rstrip("\\/").split(":", 1)[-1]
        if not bare.strip("\\/"):
            continue
        forms = {bare, bare.replace("\\", "/"), bare.replace("\\", "\\\\")}
        if any(form.encode(encoding) in data or form.lower().encode(encoding) in lowered
               for form in forms for encoding in ("utf-8", "utf-16-le")):
            named.append(folder)
    return named


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m devtools.package")
    sub = parser.add_subparsers(dest="command", required=True)
    made = sub.add_parser("build")
    made.add_argument("--out", type=Path, default=paths.SOURCE / "build")
    made.add_argument("--dirty", action="store_true")
    smoked = sub.add_parser("smoke")
    smoked.add_argument("--out", type=Path, default=paths.SOURCE / "build")
    both = sub.add_parser("compare")
    both.add_argument("a", type=Path)
    both.add_argument("b", type=Path)
    signs = sub.add_parser("manifest")
    signs.add_argument("--out", type=Path, default=paths.SOURCE / "build")
    signs.add_argument("--key", type=Path, required=True)
    signs.add_argument("--pubkey", type=Path,
                       help="the public half the app is built to trust, if not tauri.conf.json's")
    scans = sub.add_parser("scan")
    scans.add_argument("app", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            build(args.out.resolve(), dirty=args.dirty)
        elif args.command == "smoke":
            smoke(args.out.resolve() / "staging", args.out.resolve())
        elif args.command == "manifest":
            listed = write_manifest(args.out.resolve() / "staging")
            trusted = args.pubkey.read_text(encoding="ascii").strip() if args.pubkey else None
            print(f"signed {sign(listed, args.key.resolve(), trusted=trusted)}")
        elif args.command == "scan":
            named = [(app, folder) for app in args.app for folder in scan(app, builders_folders())]
            for app, folder in named:
                print(f"{app} names {folder}")
            if named:
                return 1
            print(f"{len(args.app)} file(s) name neither this checkout nor your profile")
        else:
            differ = compare(args.a, args.b)
            for name in differ:
                print(name)
            print(f"{len(differ)} files differ")
            return 1 if differ else 0
    except (BuildError, addons.AddonError) as exc:
        print(f"package: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
