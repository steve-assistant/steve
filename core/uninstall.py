"""Removing Steve takes all of it off the computer, and nothing that is not
Steve's (docs/plan/08-packaging.md, section 6).

The uninstaller deletes the install's files, its shortcuts and the app's Run
value. Everything else Steve put on the computer belongs to the user's
account, where nothing would ever remove it:

- an AppContainer profile for each module, each account's server, npm, the
  start's probe and each admission's trial, each with a folder in AppData;
- the wake task, which would go on starting a Python that is gone;
- the containers and networks in Docker, the browser's and a code run's,
  left by a worker that was ended rather than stopped;
- and when the user ticks "Delete the application data": the data folder,
  every secret in the Credential Manager, the Windows Hello key, and the
  browser's images unless another install of Steve may use them.

Each is found by Steve's exact names (core/identity.py) and checked here,
never by a looser pattern or on someone else's filter alone. Another program
of the user's may be called steve-something, and a developer's `steve-spike`
keys sit beside Steve's own; another install of Steve on the same account
names its profiles, its wake task and its Hello key with its own install id,
and labels its containers with it. On 11 October 2026 a cleanup that trusted
a server's filter deleted a whole calendar.

PowerShell writes what it pipes in the console's code page, 437 on an
English Windows, and Python read it in the ANSI one: for C:\\Users\\Müller
the read failed and uninstall stopped before removing anything, and for Zoë
the task's program never matched (the review of steps 7 to 9, finding 9).
It is told to write UTF-8, and read as UTF-8.

The keyring library names a service's first key after the service and every
later one `<key>@<service>`, and cannot list them. So the Credential Manager
is listed whole and each name matched here: a key a later step adds is found
without a list of keys to keep up (R5-F-24).

The installer's hook (ui/src-tauri/windows/hooks.nsh) runs this with the
bundled Python once the app, and with it the backend, is closed:
`python -m core.uninstall [--data]`. With `--dry-run` it says what it would
remove and removes nothing; outside an install that is all it does.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core import credstore, identity, paths
from core.sandbox import profiles
from core.secrets import KNOWN_KEYS

Runner = Callable[[list[str], float], subprocess.CompletedProcess]
Shell = Callable[[str], "subprocess.CompletedProcess | None"]
App = Callable[[list[str]], "subprocess.CompletedProcess | None"]

# Where Windows lists the user's AppContainer profiles: a key for each
# package SID, its `Moniker` the name the profile was made with.
MAPPINGS = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
            r"\CurrentVersion\AppContainer\Mappings")

# Seconds between tries at the data folder while something still holds it:
# about ten in all.
DATA_WAITS = (0.5, 1.0, 1.5, 2.0, 2.5, 2.5)

SIGNAL = ("If you linked Signal to Steve, your phone still lists it as a linked device. "
          "Remove it there, in Signal's settings.")


def our_secret(target: str) -> bool:
    """A Credential Manager entry Steve wrote: a module's secret, or a
    keyring service's entry, its first key's or a later `<key>@<service>`.

    Under `steve` only Steve's own keys, the only ones `core/secrets` will
    write: any `<key>@steve` took another program's that happened to use
    the name."""
    if target.startswith(f"{identity.MODULE_SECRETS}/"):
        return True
    key, at, service = target.rpartition("@")
    if not at:
        return target in identity.KEYRING_SERVICES
    if service == identity.KEYCHAIN:
        return key in KNOWN_KEYS
    return service == identity.BROWSER_LOGINS and bool(key)


def our_profile(name: str, install: str) -> bool:
    """An AppContainer profile of this install (core/identity.py): its
    prefix, then a module's, a server's, npm's or a trial's, or the probe."""
    head = f"{identity.CONTAINER_PREFIX}{install}."
    if not name.startswith(head):
        return False
    kind, dot, rest = name[len(head):].partition(".")
    return (kind == "probe" and not dot) or (kind in ("m", "s", "n", "t") and bool(rest))


def _mappings() -> list[str]:
    """Every AppContainer profile of this Windows user, by name."""
    import winreg

    names: list[str] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, MAPPINGS)
    except OSError:
        return names
    with root:
        for index in itertools.count():
            try:
                sid = winreg.EnumKey(root, index)
            except OSError:
                break
            with contextlib.suppress(OSError), winreg.OpenKey(root, sid) as key:
                names.append(str(winreg.QueryValueEx(key, "Moniker")[0]))
    return names


UTF8 = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "


def _run(argv: list[str]) -> subprocess.CompletedProcess | None:
    """A program's answer, read as UTF-8; None if it could not be run."""
    try:
        return subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def _powershell(script: str) -> subprocess.CompletedProcess | None:
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return None
    return _run([shell, "-NoProfile", "-NonInteractive", "-Command", UTF8 + script])


def _app(args: list[str]) -> subprocess.CompletedProcess | None:
    """The installed app, run with `args`. Hello's keys are WinRT's, which
    this Python cannot reach; the app makes them, and forgets them."""
    return _run([str(paths.install_dir() / f"{identity.NAME}.exe"), *args])


def _docker(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    # The browser's one door to Docker, which the test fences hold.
    from core.browser import sandbox

    return sandbox._docker(args, timeout)


def _wake_program(shell: Shell, task: str) -> str | None:
    """The program the wake task runs: "" if there is no such task, None if
    PowerShell could not say."""
    done = shell(f"$t = Get-ScheduledTask -TaskPath '\\' -TaskName '{task}' "
                 "-ErrorAction SilentlyContinue; if ($t) { $t.Actions[0].Execute }")
    if done is None or done.returncode != 0 or done.stdout is None:
        return None
    return done.stdout.strip()


def _other_installs(names: list[str], install: str | None) -> list[str]:
    """The ids of other installs of Steve on this account, by their sandbox
    profiles."""
    ids = {name[len(identity.CONTAINER_PREFIX):].partition(".")[0]
           for name in names if name.startswith(identity.CONTAINER_PREFIX)}
    return sorted(ids - {install, ""})


def _in_docker(run: Runner, install: str | None
               ) -> tuple[list[str], list[str], list[str]] | None:
    """This install's containers and networks, and Steve's images; None if
    Docker is not answering. Docker's filter narrows what it lists, and each
    label and name is checked again here."""
    label = '{{.ID}}\t{{.Label "' + identity.DOCKER_INSTALL_LABEL + '"}}'
    narrowed = ["--filter", f"label={identity.DOCKER_INSTALL_LABEL}={install}",
                "--format", label]

    def labelled(args: list[str]) -> list[str]:
        if install is None:
            return []
        rows = run([*args, *narrowed], 30).stdout.splitlines()
        return [ident.strip() for ident, _, value in (row.partition("\t") for row in rows)
                if ident.strip() and value.strip() == install]

    try:
        if run(["info", "--format", "{{.ServerVersion}}"], 30).returncode != 0:
            return None
        containers = labelled(["ps", "-a", "--no-trunc"])
        networks = labelled(["network", "ls", "--no-trunc"])
        images = []
        listed = run(["images", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}"], 30)
        for row in listed.stdout.splitlines():
            ref, _, ident = row.partition("\t")
            if ref.startswith(identity.DOCKER_IMAGES):
                images.append(ident.strip() if ref.endswith(":<none>") else ref.strip())
    except (OSError, subprocess.SubprocessError):
        return None
    return containers, networks, images


@dataclass
class Found:
    """What uninstall would remove, and what it leaves, with why."""

    profiles: list[str] = field(default_factory=list)
    task: str | None = None
    secrets: list[str] = field(default_factory=list)
    containers: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    hello: str | None = None
    notifications: bool = False
    data: Path | None = None
    left: list[str] = field(default_factory=list)


def find(data: bool, *, mappings: Callable[[], list[str]] | None = None,
         shell: Shell | None = None, docker: Runner | None = None,
         store: credstore.Store | None = None) -> Found:
    """Everything of this install's to remove; with `data`, its data too."""
    # Each expires in a day anyway (ui/src-tauri/src/toast.rs), and once the
    # app is gone a click on one opens nothing: they go with or without the
    # data, as their text is shown nowhere else.
    found = Found(notifications=True)
    install = profiles.made()
    names = (mappings or _mappings)() if install is not None or data else []

    if install is None:
        found.left.append("Sandbox profiles, the wake task and Docker's containers, if any: "
                          "the data folder holds no install id to tell this install's apart.")
    else:
        found.profiles = sorted(n for n in names if our_profile(n, install))
        # Its program is this Python's windowless twin (agent/wake.py), in
        # this install's folder: a data folder copied from another install
        # brings that one's id, and its task runs its own Python.
        task = identity.wake_task(install)
        program = _wake_program(shell or _powershell, task)
        ours = str(Path(sys.executable).with_name("pythonw.exe"))
        if program is None:
            found.left.append(f"The task {task}, if there is one: PowerShell did not answer.")
        elif program and os.path.normcase(program) == os.path.normcase(ours):
            found.task = task
        elif program:
            found.left.append(f"The task {task}: it runs {program}, another install's Python.")

    if (in_docker := _in_docker(docker or _docker, install)) is None:
        found.left.append("Anything of Steve's in Docker: Docker is not running.")
    else:
        found.containers, found.networks, images = in_docker
        # An image is no one install's: one another install uses would be
        # gone the next time its browser starts, and only a build by hand
        # brings it back.
        if data and (others := _other_installs(names, install)):
            found.left.append(f"Docker's images: another install of Steve on this account "
                              f"({', '.join(others)}) may use them.")
        elif data:
            found.images = images

    folder = paths.data_dir()
    if not data:
        found.left.append(f"Your data, in {folder}, its secrets in the Credential Manager "
                          "and the browser's images, as you chose.")
        return found
    keys = store or credstore.store()
    found.secrets = [target for target in keys.targets("") if our_secret(target)]
    # Its public half is in the data; the key would be left in the user's
    # Hello container, one more with each reinstall.
    if install is not None:
        found.hello = identity.hello_key(install)
    if os.environ.get(paths.DATA_DIR_ENV, "").strip():
        found.left.append(f"The data folder {folder}: you chose it ({paths.DATA_DIR_ENV}), "
                          "so it is yours to delete.")
    elif not paths.is_installed():
        found.left.append(f"The data folder {folder}: it is a checkout's.")
    elif folder.exists():
        found.data = folder
    for name in (paths.MEMORIES_DIR_ENV, paths.MODEL_CACHE_ENV):
        if chosen := os.environ.get(name, "").strip():
            found.left.append(f"{chosen}: you chose it ({name}), so it is yours to delete.")
    return found


def report(found: Found, *, dry_run: bool) -> str:
    """What is removed and what is left, for the uninstaller's log."""
    items = [f"the sandbox profile {name}" for name in found.profiles]
    items += [f"the scheduled task {found.task}"] if found.task else []
    items += [f"Docker's container {ident}" for ident in found.containers]
    items += [f"Docker's network {ident}" for ident in found.networks]
    items += [f"Docker's image {ref}" for ref in found.images]
    items += [f"the secret {target}" for target in found.secrets]
    items += [f"the Windows Hello key {found.hello}, if there is one"] if found.hello else []
    items += ["Steve's notifications in Windows' Notification Centre"] if found.notifications \
        else []
    items += [f"the data folder {found.data}"] if found.data else []
    lines = ["Would remove:" if dry_run else "Removing:"]
    lines += [f"  {item}" for item in items] or ["  nothing"]
    if found.left:
        lines += ["Left:", *(f"  {why}" for why in found.left)]
    return "\n".join(lines)


def remove(found: Found, *, shell: Shell | None = None, docker: Runner | None = None,
           store: credstore.Store | None = None,
           delete_profile: Callable[[str], bool] | None = None,
           app: App | None = None) -> list[str]:
    """Remove what `find` found. Returns what could not be removed, and why;
    the rest goes on regardless."""
    failed: list[str] = []
    for name in found.profiles:
        try:
            (delete_profile or profiles.delete)(name)
        except OSError as exc:
            failed.append(f"the sandbox profile {name}: {exc}")

    if found.task:
        done = (shell or _powershell)(f"Unregister-ScheduledTask -TaskPath '\\' "
                                      f"-TaskName '{found.task}' -Confirm:$false")
        if done is None or done.returncode != 0:
            failed.append(f"the scheduled task {found.task}")

    # Containers first: an image or a network in use is not removed.
    run = docker or _docker
    for what, args, items in (("container", ["rm", "-f"], found.containers),
                              ("network", ["network", "rm"], found.networks),
                              ("image", ["rmi"], found.images)):
        for item in items:
            try:
                ok = run([*args, item], 60).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            if not ok:
                failed.append(f"Docker's {what} {item}")

    if found.secrets:
        keys = store or credstore.store()
        for target in found.secrets:
            try:
                keys.delete(target)
            except Exception as exc:  # pywintypes.error, which is no OSError
                failed.append(f"the secret {target}: {exc}")

    if found.notifications:
        done = (app or _app)(["--clear-notifications"])
        if done is None:
            failed.append("Steve's notifications: the app could not be run")
        elif done.returncode != 0:
            failed.append(f"Steve's notifications: {(done.stderr or '').strip()}")

    # Before the data: the app finds nothing of the install's in it, only
    # the key's name it is given (ui/src-tauri/src/hello.rs, `forget`).
    if found.hello:
        done = (app or _app)(["--forget-hello", found.hello])
        if done is None:
            failed.append(f"the Windows Hello key {found.hello}: the app could not be run")
        elif done.returncode != 0:
            failed.append(f"the Windows Hello key {found.hello}: {(done.stderr or '').strip()}")

    if found.data:
        from core.sandbox import servers

        # The hook closes the app just before this, and the backend, ended
        # with it in its job, lets go of the database a moment later. Tried
        # once, the open database, its WAL and the logs stayed.
        for wait in (*DATA_WAITS, None):
            servers.remove(found.data)
            if wait is None or not found.data.exists():
                break
            time.sleep(wait)
        if found.data.exists():
            failed.append(f"the data folder {found.data}: some of it is still in use")
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.uninstall",
                                     description="Remove what Steve put on this computer.")
    parser.add_argument("--data", action="store_true",
                        help="delete the data, the secrets and the browser's images too")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be removed, and remove nothing")
    args = parser.parse_args(argv)
    if not args.dry_run and not paths.is_installed():
        print("This is a checkout, not an installed Steve: only --dry-run runs here.",
              file=sys.stderr)
        return 2
    found = find(args.data)
    print(report(found, dry_run=args.dry_run))
    if args.dry_run:
        return 0
    failed = remove(found)
    for why in failed:
        print(f"Could not remove {why}")
    if args.data:
        print(SIGNAL)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
