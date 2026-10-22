"""The install, update and uninstall, run on a clean Windows (step 8, item 9).

Until this ran, every test of the installer read its script:
tests/unit/test_installer.py checks what the hooks say, and makensis that
they compile. Neither runs them, and an installer nobody ran breaks in ways
no reading finds: Tauri's per-user default folder was Steve's own data
folder, and was found only by reading the generated script beside a build.
An install on the developer's own Windows would take its Run value and its
data folder for real (docs/plan/08-packaging.md, Questions 9), so this runs
in Windows Sandbox: a clean copy of this Windows, thrown away when it closes.

- `build --key KEY` makes two installers from the committed tree: this
  version, carrying one file the next does not, and the next patch version.
  The second build changes the version files, and puts their bytes back
  whatever happens.
- `run` starts the sandbox with the installers and the rig mapped in
  read-only, a results folder it may write, and no microphone, camera,
  clipboard or network. `sandbox_inside.ps1` runs the steps there, writes
  each as it ends, and shuts the sandbox down. Then the report is written.
  The app's installer downloads WebView2 when Windows lacks it, which this
  sandbox cannot; `run --webview2 FILE` hands the rig WebView2's offline
  installer (the x64 "Evergreen Standalone Installer") for that, and the
  rig's second step says whether it was needed.
- `report` writes the last run's report again.
- `session` opens a sandbox for the maintainer's own run, session P
  (docs/plan/08-session-p.md): the installers read-only, the network and
  the clipboard on, for a real key; the microphone and camera still off.

    python -m devtools.sandbox_check build --key PATH_TO_THE_UPDATE_KEY
    python -m devtools.sandbox_check run

CI's installer job (.github/workflows/ci.yml) builds with a key made for
its run, `build --key K --pubkey K.pub`, and runs the rig on the runner
itself, a clean Windows thrown away after the job, then `report`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from core import identity, paths
from devtools import package

INSIDE = Path(__file__).with_name("sandbox_inside.ps1")
# Where the sandbox sees the folders mapped into it.
MOUNT = "C:\\steve"
# Only the old version carries this file, so after the update it must be
# gone (section 8, step 6): the pre-install hook deletes the old backend.
LEFTOVER = "backend/app/sandbox-check-leftover.txt"
# WebView2's offline installer, for a sandbox without WebView2:
# the app's installer would download it, and the sandbox has no network.
WEBVIEW2_SETUP = "webview2-setup.exe"
SANDBOX = Path(os.environ.get("SYSTEMROOT","C:\\Windows")) / "System32" / "WindowsSandbox.exe"
VERSION_FILES = ("core/identity.py", *package.VERSIONED)
# Each file's own version is the first assignment to a key named version.
_STATED = re.compile(r'^(\s*"?version"?\s*[=:]\s*")[^"]+"', re.IGNORECASE | re.MULTILINE)

Run = Callable[..., subprocess.CompletedProcess]


def base(checkout: Path | None = None) -> Path:
    return (checkout or paths.SOURCE) / "build" / "sandbox"


def report_path(checkout: Path | None = None) -> Path:
    return base(checkout) / "report.md"


def next_patch(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


@contextmanager
def bumped(checkout: Path, version: str) -> Iterator[None]:
    """The version files say `version` inside, and hold their own bytes again
    after, as does Cargo.lock, which cargo rewrites to match."""
    names = [*VERSION_FILES, "ui/src-tauri/Cargo.lock"]
    saved = {name: (checkout / name).read_bytes() for name in names}
    try:
        for name in VERSION_FILES:
            text, count = _STATED.subn(rf'\g<1>{version}"', saved[name].decode("utf-8"), count=1)
            if count != 1:
                raise package.BuildError(f"{name} states no version")
            (checkout / name).write_bytes(text.encode("utf-8"))
        yield
    finally:
        for name, data in saved.items():
            (checkout / name).write_bytes(data)


def execution_level(installer: Path) -> str:
    """The rights an installer asks Windows for, from the manifest Windows
    reads: NSIS's stub carries it near the start, before the packed files.
    The generated script names both levels, each under an `!if`."""
    with installer.open("rb") as stream:
        head = stream.read(4 << 20)
    found = re.search(rb'requestedExecutionLevel\s+level="(\w+)"', head)
    return found.group(1).decode("ascii") if found else "unknown"


def _build_one(checkout: Path, key: Path, *, dirty: bool, leftover: bool, run: Run,
               pubkey: Path | None = None) -> Path:
    """One installer, built as a release is (docs/plan/handoff.md, item 7).
    Given `pubkey`, the list and the app trust that key, not the release's:
    CI's installer job signs with a key made for its run."""
    python = [sys.executable, "-m", "devtools.package"]
    run([*python, "build", *(["--dirty"] if dirty else [])], cwd=checkout, check=True)
    if leftover:
        extra = checkout / "build" / "staging" / LEFTOVER
        extra.write_text("Only the older installer carries this file.\n", encoding="utf-8",
                         newline="\n")
    run([*python, "manifest", "--key", key, *(["--pubkey", pubkey] if pubkey else [])],
        cwd=checkout, check=True)
    configs = ["--config", "src-tauri/tauri.release.conf.json"]
    if pubkey:
        # Merged last, so it wins. A file, not JSON in the argument:
        # tauri.cmd is a batch file, and cmd.exe reads quotes its own way.
        trusting = base(checkout) / "trusted-key.conf.json"
        trusting.parent.mkdir(parents=True, exist_ok=True)
        stored = pubkey.read_text(encoding="ascii").strip()
        trusting.write_text(json.dumps({"plugins": {"updater": {"pubkey": stored}}}) + "\n",
                            encoding="utf-8", newline="\n")
        configs += ["--config", str(trusting)]
    ui = checkout / "ui"
    env = {**os.environ, "TAURI_SIGNING_PRIVATE_KEY": str(key),
           "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "", "CARGO_BUILD_JOBS": "3"}
    run([str(ui / "node_modules" / ".bin" / "tauri.cmd"), "build", *configs], cwd=ui, env=env,
        check=True, stdin=subprocess.DEVNULL)
    version = package.versions(checkout)["pyproject.toml"]
    return (ui / "src-tauri" / "target" / "release" / "bundle" / "nsis"
            / f"{identity.NAME}_{version}_x64-setup.exe")


def build_pair(key: Path, *, pubkey: Path | None = None, checkout: Path | None = None,
               run: Run = subprocess.run, say: Callable[[str], None] = print) -> dict:
    """The two installers the run needs, and what it should know of them."""
    checkout = checkout or paths.SOURCE
    if package.commit_of(checkout)[1]:
        raise package.BuildError("commit first: the second build changes the version files"
                                 " and puts them back")
    old, new = identity.VERSION, next_patch(identity.VERSION)
    installers = base(checkout) / "installers"
    shutil.rmtree(installers, ignore_errors=True)
    installers.mkdir(parents=True)

    say(f"Steve {old}, carrying {LEFTOVER}")
    made = _build_one(checkout, key, dirty=False, leftover=True, run=run, pubkey=pubkey)
    shutil.copy2(made, installers / made.name)
    level = execution_level(made)

    say(f"Steve {new}")
    with bumped(checkout, new):
        made = _build_one(checkout, key, dirty=True, leftover=False, run=run, pubkey=pubkey)
        shutil.copy2(made, installers / made.name)

    plan = {"old": old, "new": new, "leftover": LEFTOVER, "execution_level": level,
            "commit": package.commit_of(checkout)[0]}
    (installers / "versions.json").write_text(json.dumps(plan, indent=1) + "\n",
                                              encoding="utf-8", newline="\n")
    return plan


def _mapped(host: Path, name: str, read_only: bool) -> str:
    return ("    <MappedFolder>\n"
            f"      <HostFolder>{escape(str(host))}</HostFolder>\n"
            f"      <SandboxFolder>{MOUNT}\\{name}</SandboxFolder>\n"
            f"      <ReadOnly>{'true' if read_only else 'false'}</ReadOnly>\n"
            "    </MappedFolder>\n")


def wsb_config(installers: Path, rig: Path, results: Path) -> str:
    """Windows Sandbox's configuration: what it may see, and nothing it may
    hear or reach."""
    return ("<Configuration>\n"
            "  <AudioInput>Disable</AudioInput>\n"
            "  <VideoInput>Disable</VideoInput>\n"
            "  <Networking>Disable</Networking>\n"
            "  <ClipboardRedirection>Disable</ClipboardRedirection>\n"
            "  <PrinterRedirection>Disable</PrinterRedirection>\n"
            "  <MappedFolders>\n"
            + _mapped(installers, "installers", True)
            + _mapped(rig, "rig", True)
            + _mapped(results, "results", False)
            + "  </MappedFolders>\n"
            "  <LogonCommand>\n"
            "    <Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File"
            f" {MOUNT}\\rig\\{INSIDE.name}</Command>\n"
            "  </LogonCommand>\n"
            "</Configuration>\n")


def session_config(installers: Path) -> str:
    """Session P's sandbox (docs/plan/08-session-p.md), used by hand. It
    reaches the network, for a real model, and the clipboard, to paste the
    key in; it still never hears the microphone or sees the camera, and the
    installers stay read-only. "Default" is Windows Sandbox's word for on."""
    return ("<Configuration>\n"
            "  <AudioInput>Disable</AudioInput>\n"
            "  <VideoInput>Disable</VideoInput>\n"
            "  <Networking>Default</Networking>\n"
            "  <ClipboardRedirection>Default</ClipboardRedirection>\n"
            "  <PrinterRedirection>Disable</PrinterRedirection>\n"
            "  <MappedFolders>\n"
            + _mapped(installers, "installers", True)
            + "  </MappedFolders>\n"
            "</Configuration>\n")


def read_results(path: Path) -> dict | None:
    """The run's results so far; None while there are none, or while the
    sandbox is part way through writing them."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _launch(config: Path) -> None:
    subprocess.Popen([str(SANDBOX), str(config)])


def _ready(folder: Path, launch: Callable[[Path], None]) -> Path:
    """The built installers' folder, once there is a sandbox to map it into."""
    installers = folder / "installers"
    if not (installers / "versions.json").exists():
        raise SystemExit("No installers: run `python -m devtools.sandbox_check build` first.")
    if launch is _launch and not SANDBOX.exists():
        raise SystemExit("Windows Sandbox is not turned on. Turn on \"Windows Sandbox\" in"
                         " Windows' optional features (it asks for an administrator), and"
                         " restart.")
    return installers


def open_session(*, checkout: Path | None = None, launch: Callable[[Path], None] = _launch,
                 say: Callable[[str], None] = print) -> Path:
    """Open session P's sandbox, with the installers in it, and return its
    configuration. Nothing runs by itself: the steps are the maintainer's."""
    folder = base(checkout)
    installers = _ready(folder, launch)
    config = folder / "steve-session.wsb"
    config.write_text(session_config(installers), encoding="utf-8", newline="\n")
    say(f"starting Windows Sandbox with {config}; the installers are in"
        f" {MOUNT}\\installers, to run by hand.")
    launch(config)
    return config


def run_sandbox(*, checkout: Path | None = None, timeout: float = 3600,
                webview2: Path | None = None, launch: Callable[[Path], None] = _launch,
                say: Callable[[str], None] = print) -> dict:
    """Start the sandbox, and wait until the rig says it is done."""
    folder = base(checkout)
    installers = _ready(folder, launch)
    rig, results = folder / "rig", folder / "results"
    for made in (rig, results):
        shutil.rmtree(made, ignore_errors=True)
        made.mkdir(parents=True)
    shutil.copy2(INSIDE, rig / INSIDE.name)
    if webview2:
        shutil.copy2(webview2, rig / WEBVIEW2_SETUP)
    config = folder / "steve-check.wsb"
    config.write_text(wsb_config(installers, rig, results), encoding="utf-8", newline="\n")

    say(f"starting Windows Sandbox with {config}")
    launch(config)
    found = results / "results.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = read_results(found)
        if data and data.get("done"):
            return data
        time.sleep(5)
    return read_results(found) or {"steps": [], "done": False}


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def report(data: dict, plan: dict, *, on: date | None = None) -> str:
    """The run, as `report_path` keeps it."""
    lines = [
        "# Step 8: the installer in Windows Sandbox",
        "",
        f"Run on {(on or date.today()).isoformat()} by `python -m devtools.sandbox_check`,"
        f" with the installers of {plan['old']} and {plan['new']}, built from"
        f" `{plan.get('commit', 'unknown')[:7]}`. The installer asks Windows for"
        f" `{plan['execution_level']}` rights.",
        "",
    ]
    if not data.get("done"):
        lines += ["**The run did not finish.** These are the steps it reached.", ""]
    lines += ["| Step | Result | Seconds | What it found |", "|---|---|---:|---|"]
    for step in data.get("steps", []):
        result = "passed" if step.get("ok") else "**failed**"
        lines.append(f"| {_cell(step['name'])} | {result} | {step.get('seconds', '')} |"
                     f" {_cell(step.get('detail', ''))} |")
    for step in data.get("steps", []):
        if left := step.get("left"):
            lines += ["", f"## Left after: {step['name']}", ""]
            lines += [f"- {name.replace('_', ' ')}: {_cell(value)}" for name, value in left.items()]
    failed = [step for step in data.get("steps", []) if not step.get("ok")]
    lines += ["", f"{len(data.get('steps', [])) - len(failed)} steps passed, {len(failed)} failed."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m devtools.sandbox_check")
    sub = parser.add_subparsers(dest="command", required=True)
    made = sub.add_parser("build", help="build the two installers")
    made.add_argument("--key", type=Path, required=True, help="the update key's private half")
    made.add_argument("--pubkey", type=Path,
                      help="its public half, for a key the checkout's app does not trust (CI's)")
    ran = sub.add_parser("run", help="run them in Windows Sandbox, and write the report")
    ran.add_argument("--timeout", type=float, default=3600, help="seconds to wait for the run")
    ran.add_argument("--webview2", type=Path,
                     help="WebView2's offline installer (x64), if the sandbox lacks it")
    sub.add_parser("report", help="write the last run's report again")
    sub.add_parser("session", help="open a sandbox with the network, for session P by hand")
    args = parser.parse_args(argv)

    if args.command == "session":
        open_session()
        return 0
    if args.command == "build":
        try:
            plan = build_pair(args.key.resolve(),
                              pubkey=args.pubkey.resolve() if args.pubkey else None)
        except (package.BuildError, subprocess.CalledProcessError) as exc:
            print(f"sandbox_check: {exc}", file=sys.stderr)
            return 1
        print(f"built {plan['old']} and {plan['new']} into {base() / 'installers'}")
        return 0
    data = run_sandbox(timeout=args.timeout, webview2=args.webview2) if args.command == "run" else (
        read_results(base() / "results" / "results.json") or {"steps": [], "done": False})
    plan = json.loads((base() / "installers" / "versions.json").read_text(encoding="utf-8"))
    text = report(data, plan)
    report_path().parent.mkdir(parents=True, exist_ok=True)
    report_path().write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {report_path()}")
    passed = data.get("done") and all(step.get("ok") for step in data.get("steps", []))
    if not passed:
        # Into the log too: CI's runner goes with its job, and the file with
        # it. The installer job's first failure (22 October 2026) left only
        # the line above, and which step had broken the install went too.
        print(text)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
