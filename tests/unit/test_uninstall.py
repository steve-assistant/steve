"""Uninstall (core/uninstall.py) removes what Steve put on the computer, and
never what another program, or another install of Steve, did.

It runs against the user's whole account: their Credential Manager, their
AppContainer profiles, their scheduled tasks and Docker. So every name is
matched here by Steve's exact prefixes, and these check a name one character
off each one is never touched. Each door is a small fake; the fences refuse
the real ones.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core import credstore, identity, paths, uninstall
from core.sandbox import profiles

# Taken before the fences shut it, for the one test of what it runs.
_REAL_POWERSHELL = uninstall._powershell


def _done(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, "")


class _Shell:
    """PowerShell, answering for one wake task that runs `program`."""

    def __init__(self, program: str | None) -> None:
        self.program = program
        self.scripts: list[str] = []

    def __call__(self, script: str) -> subprocess.CompletedProcess:
        self.scripts.append(script)
        return _done((self.program or "") if "Get-ScheduledTask" in script else "")


INSTALL = "01234567"


class _Docker:
    """Docker with a few things in it, this install's, another install's and
    others'. Like a server that ignores a filter, it lists everything
    whatever it was asked."""

    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        self.calls.append(args)
        if args[0] == "info":
            return _done("27.0", 0 if self.running else 1)
        if args[0] == "ps":
            return _done(f"c-steve\t{INSTALL}\nc-other\t\nc-steve-other\t01234568\n"
                         f"c-steve-x\t{INSTALL}x\n")
        if args[:2] == ["network", "ls"]:
            return _done(f"n-steve\t{INSTALL}\nn-other\t\nn-steve-other\t01234568\n")
        if args[0] == "images":
            return _done("steve/browser-gate:abc\tsha256:1\n"
                         "steve/browser-browser:<none>\tsha256:2\n"
                         "stevex/thing:1\tsha256:3\nother/steve:1\tsha256:4\n"
                         "registry.example/steve/gate:1\tsha256:5\n")
        return _done()


def _profile_names(install: str, *, others: bool = True) -> list[str]:
    """This install's profiles and near misses; with `others`, two other
    installs' too."""
    head = f"{identity.CONTAINER_PREFIX}{install}."
    return [
        f"{head}m.gmail", f"{head}s.0123456789abcdef", f"{head}n.gmail", f"{head}probe",
        f"{head}t.tok",
        # One character off each: never this install's.
        f"{head}probes", f"{head}x.gmail", f"{head}m.", f"{head}probe.x",
        f"stevex.{install}.m.gmail", "Microsoft.Windows.Photos_8wekyb3d8bbwe",
    ] + ([f"{identity.CONTAINER_PREFIX}{install[:-1]}8.m.gmail",
          f"{identity.CONTAINER_PREFIX}ffffffff.m.gmail"] if others else [])


OURS = [
    "steve", "OPENAI_API_KEY@steve", "APPROVAL_TOTP_SECRET@steve",
    "steve-browser-logins", "site@steve-browser-logins", "a@b.example@steve-browser-logins",
    "steve-modules/k1/a1/TOKEN", "steve-modules/k1/a1/TOKEN#1",
]
NOT_OURS = [
    "stevex", "Steve-spike", "steve-spike", "key@steve-spike", "key@stevex", "steve@other",
    "steve-module/k1/a1/TOKEN", "steve-modulesx/k1", "steve-modules", "x-steve-modules/k1",
    "steve-browser-login", "key@steve-browser-loginsx", "git:https://github.com",
    "key@my-steve", "agent-steve", "site@my-steve-browser-logins",
    # Under Steve's own service, a key Steve never writes: another program's.
    "SOMEKEY@steve", "a@b.example@steve", "@steve", "@steve-browser-logins",
]


def _install() -> str:
    """This install's id, fixed, so that the names one character off it are."""
    (paths.data_dir() / profiles.INSTALL_FILE).write_text(
        "0123456789abcdef0123456789abcdef\n", encoding="ascii", newline="\n")
    assert profiles.made() == INSTALL
    return INSTALL


def _store() -> credstore.MemoryStore:
    store = credstore.MemoryStore()
    for target in OURS + NOT_OURS:
        store.entries[target] = b"x"
    return store


@pytest.fixture
def installed(tmp_path, monkeypatch) -> Path:
    """An installed Steve, its data in the default place."""
    code = tmp_path / "Programs" / "Steve" / "backend" / "app"
    code.mkdir(parents=True)
    (code / paths.INSTALLED_MARKER).write_text("{}\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(paths, "SOURCE", code)
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    data = paths.data_dir()
    assert data == tmp_path / "local" / identity.NAME
    data.mkdir(parents=True)
    return data


def _find(data: bool, *, install: str | None, shell=None, docker=None, store=None,
          others: bool = True):
    names = _profile_names(install or "00000000", others=others)
    return uninstall.find(data, mappings=lambda: names, shell=shell or _Shell(None),
                          docker=docker or _Docker(), store=store or _store())


def test_only_steves_secrets_are_found_and_a_name_one_character_off_never_is(installed):
    found = _find(True, install=_install())
    assert sorted(found.secrets) == sorted(OURS)


def test_only_this_installs_profiles_are_found_and_another_installs_never_are(installed):
    install = _install()
    found = _find(False, install=install)
    head = f"{identity.CONTAINER_PREFIX}{install}."
    assert found.profiles == sorted(
        [f"{head}m.gmail", f"{head}n.gmail", f"{head}probe", f"{head}s.0123456789abcdef",
         f"{head}t.tok"])


def test_with_no_install_id_no_profile_is_taken_and_none_is_made(installed):
    """Without the id, this install's profiles cannot be told from another's;
    and making one would leave a file in a folder being deleted."""
    found = _find(True, install=None)
    assert found.profiles == []
    assert not (installed / profiles.INSTALL_FILE).exists()
    assert any("install id" in why for why in found.left)


def test_docker_is_trusted_for_nothing_it_was_asked_to_filter(installed):
    """Docker lists everything here, as a server that ignored the filter
    would; only what carries this install's id, or Steve's image name, is
    taken. Another install's containers are its own (finding 13)."""
    found = _find(True, install=_install(), others=False)
    assert found.containers == ["c-steve"]
    assert found.networks == ["n-steve"]
    assert found.images == ["steve/browser-gate:abc", "sha256:2"]


def test_the_images_stay_while_another_install_of_steve_may_use_them(installed):
    """An image is no one install's: the other's browser would not start
    again until it was built by hand."""
    found = _find(True, install=_install())
    assert found.images == [] and found.containers == ["c-steve"]
    assert any("another install of Steve" in why and "01234568" in why and "ffffffff" in why
               for why in found.left)


def test_the_wake_task_is_this_installs_and_removed_only_when_it_runs_this_installs_python(
        installed):
    """One `SteveWake` for every install let a second take the first's task
    (finding 13). A data folder copied from another install brings its id."""
    install = _install()
    ours = str(Path(sys.executable).with_name("pythonw.exe"))
    shell = _Shell(ours.upper())
    assert _find(False, install=install, shell=shell).task == identity.wake_task(install)
    assert f"-TaskName '{identity.wake_task(install)}'" in shell.scripts[0]
    other = _find(False, install=install, shell=_Shell(r"C:\Other\Steve\backend\pythonw.exe"))
    assert other.task is None
    assert any("another install" in why for why in other.left)
    none = _find(False, install=install, shell=_Shell(None))
    assert none.task is None and not any(why.startswith("The task") for why in none.left)
    unanswered = _find(False, install=install, shell=lambda script: None)
    assert any("PowerShell did not answer" in why for why in unanswered.left)


@pytest.mark.processes
@pytest.mark.parametrize("user", ["Müller", "Zoë", "O'Brien"])
def test_a_profile_folder_with_an_accent_is_read_back_whole(installed, user):
    """PowerShell wrote its answer in the console's code page and this read
    it in the ANSI one: for Müller the read failed and uninstall stopped
    before removing anything; for Zoë the task's program never matched
    (finding 9). PowerShell is told to write UTF-8, and this reads UTF-8:
    here a child writes the UTF-8 bytes PowerShell then writes."""
    program = rf"C:\Users\{user}\AppData\Local\Programs\Steve\backend\pythonw.exe"
    done = uninstall._run([sys.executable, "-I", "-c",
                           "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))",
                           (program + "\r\n").encode("utf-8").hex()])
    assert done is not None and done.stdout.strip() == program


def test_powershell_is_told_to_write_utf8_before_anything_it_runs(monkeypatch):
    ran: list[list[str]] = []
    monkeypatch.setattr(uninstall.shutil, "which", lambda name: name)
    monkeypatch.setattr(uninstall, "_run", ran.append)
    _REAL_POWERSHELL("Get-Thing")
    [[*_, script]] = ran
    assert script == ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                      "Get-Thing")


def test_without_the_data_box_the_data_its_secrets_and_the_images_are_kept(installed):
    found = _find(False, install=_install())
    assert found.secrets == [] and found.images == [] and found.data is None
    assert found.containers == ["c-steve"]


def test_the_data_folder_is_deleted_only_in_its_default_place(installed, monkeypatch, tmp_path):
    """A folder the user chose may hold anything of theirs; a checkout's data
    is a developer's."""
    assert _find(True, install=None).data == installed

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "mine"))
    chosen = _find(True, install=None)
    assert chosen.data is None
    assert any(paths.DATA_DIR_ENV in why for why in chosen.left)

    monkeypatch.delenv(paths.DATA_DIR_ENV)
    (paths.SOURCE / paths.INSTALLED_MARKER).unlink()
    (paths.SOURCE / "pyproject.toml").write_text("", encoding="utf-8")
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    checkout = _find(True, install=None)
    assert checkout.data is None
    assert any("a checkout's" in why for why in checkout.left)


def test_a_dry_run_names_everything_and_removes_nothing(installed, monkeypatch, capsys):
    install = _install()
    ours = str(Path(sys.executable).with_name("pythonw.exe"))
    store, docker, shell = _store(), _Docker(), _Shell(ours)
    monkeypatch.setattr(uninstall, "_mappings", lambda: _profile_names(install, others=False))
    monkeypatch.setattr(uninstall, "_powershell", shell)
    monkeypatch.setattr(uninstall, "_docker", docker)
    monkeypatch.setattr(credstore, "_store", store)
    deleted: list[str] = []
    monkeypatch.setattr(profiles, "delete", deleted.append)
    apps: list[list[str]] = []
    monkeypatch.setattr(uninstall, "_app", apps.append)

    assert uninstall.main(["--data", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("Would remove:")
    for name in [f"{identity.CONTAINER_PREFIX}{install}.probe", identity.wake_task(install),
                 "c-steve", "steve/browser-gate:abc", "OPENAI_API_KEY@steve",
                 identity.hello_key(install), str(installed)]:
        assert name in out
    assert sorted(store.entries) == sorted(OURS + NOT_OURS)
    assert deleted == [] and installed.exists() and apps == []
    assert not any("Unregister" in script for script in shell.scripts)
    assert all(call[0] in ("info", "ps", "network", "images") for call in docker.calls)
    assert all(call[:2] != ["network", "rm"] for call in docker.calls)


def test_removing_takes_exactly_what_was_found(installed, monkeypatch, capsys):
    install = _install()
    store, docker = _store(), _Docker()
    shell = _Shell(str(Path(sys.executable).with_name("pythonw.exe")))
    monkeypatch.setattr(uninstall, "_mappings", lambda: _profile_names(install, others=False))
    monkeypatch.setattr(uninstall, "_powershell", shell)
    monkeypatch.setattr(uninstall, "_docker", docker)
    monkeypatch.setattr(credstore, "_store", store)
    deleted: list[str] = []
    monkeypatch.setattr(profiles, "delete", deleted.append)
    apps: list[list[str]] = []
    monkeypatch.setattr(uninstall, "_app", lambda args: apps.append(args) or _done())
    ours = _find(False, install=install).profiles

    assert uninstall.main(["--data"]) == 0

    assert sorted(store.entries) == sorted(NOT_OURS)
    assert sorted(deleted) == ours and len(ours) == 5
    assert apps == [["--clear-notifications"], ["--forget-hello", identity.hello_key(install)]]
    assert [s for s in shell.scripts if "Unregister" in s] == [
        f"Unregister-ScheduledTask -TaskPath '\\' -TaskName '{identity.wake_task(install)}' "
        "-Confirm:$false"]
    removed = [call for call in docker.calls
               if call[0] in ("rm", "rmi") or call[:2] == ["network", "rm"]]
    assert removed == [["rm", "-f", "c-steve"], ["network", "rm", "n-steve"],
                       ["rmi", "steve/browser-gate:abc"], ["rmi", "sha256:2"]]
    assert not installed.exists()
    assert uninstall.SIGNAL in capsys.readouterr().out


def test_the_data_is_deleted_once_the_closing_backend_lets_go_of_it(installed, monkeypatch):
    """The hook closes the app just before this runs, and the backend, ended
    with it, lets go of its database a moment later. Tried once, the
    database, its WAL and the logs stayed (the review of steps 7 to 9,
    finding 11)."""
    monkeypatch.setattr(uninstall, "DATA_WAITS", (0.1,) * 30)
    held = (installed / "steve.db").open("wb")
    threading.Timer(0.4, held.close).start()
    try:
        failed = uninstall.remove(uninstall.Found(data=installed))
    finally:
        held.close()
    assert failed == [] and not installed.exists()


def test_data_still_held_after_every_wait_is_reported(installed, monkeypatch):
    """What could not be removed is said, not hidden: the hook tells the user."""
    monkeypatch.setattr(uninstall, "DATA_WAITS", (0.01, 0.01))
    with (installed / "steve.db").open("wb"):
        failed = uninstall.remove(uninstall.Found(data=installed))
    assert failed == [f"the data folder {installed}: some of it is still in use"]


def test_the_hello_key_goes_with_the_data_and_only_this_installs(installed):
    """Left, the key stayed in the user's Hello container, one more with each
    reinstall (finding 12). Kept with the data: its public half is there."""
    install = _install()
    assert _find(True, install=install).hello == identity.hello_key(install)
    assert _find(False, install=install).hello is None
    (installed / profiles.INSTALL_FILE).unlink()
    assert _find(True, install=None).hello is None


def test_steves_notifications_are_cleared_with_or_without_the_data(installed, monkeypatch):
    """A notification's text stays in Windows' history for days, outside
    the data folder (the step 11 audit, E2), and nothing of Steve's is
    left to open it."""
    assert _find(True, install=_install()).notifications
    assert _find(False, install=_install()).notifications
    apps: list[list[str]] = []
    monkeypatch.setattr(uninstall, "_app", lambda args: apps.append(args) or _done())
    assert uninstall.remove(uninstall.Found(notifications=True)) == []
    assert apps == [["--clear-notifications"]]
    refused = uninstall.remove(uninstall.Found(notifications=True), app=lambda args: subprocess.
                               CompletedProcess(args, 1, "", "the history is locked\n"))
    assert refused == ["Steve's notifications: the history is locked"]


def test_a_hello_key_the_app_could_not_forget_is_reported():
    key = identity.hello_key(INSTALL)
    refused = uninstall.remove(uninstall.Found(hello=key), app=lambda args: subprocess.
                               CompletedProcess(args, 1, "", "Windows Hello is locked\n"))
    assert refused == [f"the Windows Hello key {key}: Windows Hello is locked"]
    unrun = uninstall.remove(uninstall.Found(hello=key), app=lambda args: None)
    assert unrun == [f"the Windows Hello key {key}: the app could not be run"]


def test_the_app_is_asked_by_the_flag_it_answers_to():
    lib = (paths.SOURCE / "ui" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert 'const FORGET_HELLO: &str = "--forget-hello";' in lib
    assert "forget_hello(&args, hello::forget)" in lib
    assert 'const CLEAR_NOTIFICATIONS: &str = "--clear-notifications";' in lib


def test_outside_an_install_only_a_dry_run_runs(capsys):
    """A developer's checkout shares the account's secrets with an installed
    Steve: an uninstall run there must not take them."""
    assert not paths.is_installed()
    assert uninstall.main(["--data"]) == 2
    assert "only --dry-run" in capsys.readouterr().err


def test_every_keyring_service_steve_writes_is_one_uninstall_removes():
    """A store added later under a new service would outlive the uninstall
    unless it is named in core/identity.py."""
    from core import secrets
    from core.browser import vault

    assert {secrets.SERVICE, vault.SERVICE} == set(identity.KEYRING_SERVICES)
    users = sorted(
        str(path.relative_to(paths.SOURCE)).replace("\\", "/")
        for top in ("core", "agent", "modules", "tools")
        for path in (paths.SOURCE / top).rglob("*.py")
        if any(use in path.read_text(encoding="utf-8", errors="replace")
               for use in ("import keyring", "from keyring")))
    assert users == ["core/browser/vault.py", "core/secrets.py"]


def test_the_report_says_what_is_left_and_why(installed):
    found = _find(False, install=None, docker=_Docker(running=False))
    text = uninstall.report(found, dry_run=True)
    assert "Docker is not running" in text and "as you chose" in text
