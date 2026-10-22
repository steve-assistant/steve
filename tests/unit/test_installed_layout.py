"""An installed Steve knows it is installed, and acts like it (step 8, item 2).

An install was told from a checkout only by a missing `pyproject.toml`
(core/paths.py), so a stray file would have turned an install into a
checkout: its data written beside its code, and its secrets read from the
environment, where another program's `OPENAI_API_KEY` would have been taken
as Steve's. And code copied anywhere else ran as an install, on the real
data folder. These tests put the packages in a made-up folder and check
each rule from both sides.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from agent.attention.sources import health
from core import paths, secrets, supervisor, worker_handle
from core.sandbox import runtime
from devtools import worktrees


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Packages at `install/backend/app`, as the build lays them out, with
    neither marker yet, and no data folder chosen by the environment."""
    install = tmp_path / "Programs" / "Steve"
    source = install / "backend" / "app"
    source.mkdir(parents=True)
    monkeypatch.setattr(paths, "SOURCE", source)
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.ADDONS_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return source


def _install(source):
    (source / paths.INSTALLED_MARKER).write_text('{"version": "0.1.0"}', encoding="utf-8")


def _checkout(source):
    (source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")


def test_an_install_keeps_its_data_in_local_app_data(tree, tmp_path):
    """The data folder is the user's, never the code's, which an update replaces."""
    _install(tree)

    assert paths.layout() == "installed"
    assert paths.data_dir() == tmp_path / "local" / "Steve"


def test_an_install_whose_folder_and_data_folder_overlap_refuses_to_start(
        tree, tmp_path, monkeypatch):
    """Removing the data would take a program inside its data folder with it,
    and an update the data inside the program's folder. The installer's page
    refused only the data folder itself, and a silent install shows no page
    (the step 11 audit, F8). A checkout keeps its data inside it, as it should."""
    _checkout(tree)
    assert paths.overlap() is None
    _install(tree)
    install = paths.install_dir()
    assert paths.overlap() is None

    for data in (install, install / "data", install.parent, tmp_path):
        monkeypatch.setenv(paths.DATA_DIR_ENV, str(data))
        assert paths.overlap(), data
    # A sibling, though its name starts the same, or is spelled through it.
    for data in (install.parent / "Steve2", install / ".." / "Steve2"):
        monkeypatch.setenv(paths.DATA_DIR_ENV, str(data))
        assert paths.overlap() is None, data

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(install / "data"))
    monkeypatch.setattr(supervisor.identity, "scrub_environment", lambda: None)
    monkeypatch.setattr(supervisor, "listening", lambda: pytest.fail("the supervisor listened"))
    with pytest.raises(SystemExit, match="one holds the other"):
        supervisor.main()


def test_a_stray_pyproject_cannot_turn_an_install_into_a_checkout(tree, tmp_path):
    """The marker is positive and wins: one file left in the install folder
    must not move its data beside the code or open the environment to secrets."""
    _install(tree)
    _checkout(tree)

    assert paths.is_installed() and not paths.is_source_tree()
    assert paths.data_dir() == tmp_path / "local" / "Steve"


def test_a_checkout_keeps_its_data_in_its_own_tree(tree):
    """Each worktree's processes stay inside that worktree."""
    _checkout(tree)

    assert paths.layout() == "source"
    assert paths.data_dir() == tree / "data"


def test_code_that_is_neither_refuses_rather_than_use_the_installed_data(tree):
    """A copy of the code (a spike, a backup) must not quietly run on the
    installed Steve's data folder."""
    with pytest.raises(paths.LayoutUnknown, match="neither an installed Steve"):
        paths.data_dir()
    with pytest.raises(paths.LayoutUnknown):
        paths.addons_dir()


def test_a_chosen_data_folder_needs_no_layout(tree, tmp_path, monkeypatch):
    """Tests and anyone who sets STEVE_DATA_DIR choose for themselves."""
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "chosen"))

    assert paths.data_dir() == tmp_path / "chosen"


def test_an_installs_code_folders_sit_in_its_own_install_folder(tree):
    """Add-ons and the module runtime go beside the installed code, wherever
    the user installed it, not at a fixed path under LOCALAPPDATA."""
    _install(tree)
    install = tree.parent.parent

    assert paths.install_dir() == install
    assert paths.addons_dir() == install / "addons"
    assert runtime.home() == install / "runtime"


def test_a_checkouts_code_folders_stay_in_the_checkout(tree):
    _checkout(tree)

    assert paths.addons_dir() == tree / ".addons"
    assert runtime.home() == tree / ".runtime"
    with pytest.raises(paths.LayoutUnknown):
        paths.install_dir()


class _EmptyKeyring:
    def get_password(self, service, key):
        return None


def test_an_install_never_takes_a_secret_from_the_environment(tree, monkeypatch):
    """Decision D2: another program's OPENAI_API_KEY is not the key the user gave Steve."""
    _install(tree)
    monkeypatch.setattr(secrets, "keyring", _EmptyKeyring())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-another-programs")
    monkeypatch.setenv("STEVE_OPENAI_API_KEY", "sk-from-the-environment")

    assert secrets.get("OPENAI_API_KEY") is None
    assert not next(s for s in secrets.status() if s.key == "OPENAI_API_KEY").present


def test_a_checkout_still_reads_secrets_from_the_environment(tree, monkeypatch):
    """CI has no desktop keyring, so a checkout keeps the fallback."""
    _checkout(tree)
    monkeypatch.setattr(secrets, "keyring", _EmptyKeyring())
    monkeypatch.setenv("STEVE_OPENAI_API_KEY", "sk-from-the-environment")

    assert secrets.get("OPENAI_API_KEY") == "sk-from-the-environment"


class _Spawned(Exception):
    pass


def _worker_command(monkeypatch, tmp_path) -> list[str]:
    seen = []

    def popen(args, **_):
        seen.append(args)
        raise _Spawned

    monkeypatch.setattr(worker_handle.subprocess, "Popen", popen)
    monkeypatch.setattr(worker_handle, "_open_log", lambda port, name: None)
    with pytest.raises(_Spawned):
        worker_handle.spawn_worker("isolated", cwd=tmp_path)
    return seen[0]


def test_a_worker_starts_isolated_from_the_users_python(monkeypatch, tmp_path):
    """The worker took PYTHONPATH and the user's site-packages, so a module of
    the same name elsewhere on the machine could stand in for one of Steve's.
    Run for real: the flags it is given keep a planted folder off the path."""
    command = _worker_command(monkeypatch, tmp_path)
    monkeypatch.undo()  # the real Popen again, for the real runs below
    flags = command[1:command.index("-m")]
    assert "-B" in flags

    planted = tmp_path / "planted"
    planted.mkdir()
    env = {**worker_handle.os.environ, "PYTHONPATH": str(planted)}
    shown = subprocess.run([command[0], *flags, "-c", "import sys; print(chr(10).join(sys.path))"],
                           capture_output=True, text=True, env=env, check=True).stdout
    assert str(planted) not in shown
    # And the same Python without the flags does put it there, so the check can fail.
    plain = subprocess.run([command[0], "-c", "import sys; print(chr(10).join(sys.path))"],
                           capture_output=True, text=True, env=env, check=True).stdout
    assert str(planted) in plain


def test_a_worker_runs_the_code_of_the_folder_it_starts_in(monkeypatch, tmp_path):
    """`-I` took the starting folder off the path, so in a checkout the venv's
    editable install served another copy of the code: a worktree's
    supervisor, and the zero-downtime gate test's copy, ran the main
    checkout's worker, and that test hung."""
    command = _worker_command(monkeypatch, tmp_path)
    monkeypatch.undo()
    flags = command[1:command.index("-m")]
    (tmp_path / "steve_probe_here.py").write_text("print('the folder it starts in')\n",
                                                  encoding="utf-8", newline="\n")

    shown = subprocess.run([command[0], *flags, "-m", "steve_probe_here"], cwd=tmp_path,
                           capture_output=True, text=True, check=False)

    assert shown.stdout.strip() == "the folder it starts in", shown.stderr


def test_the_wake_task_starts_its_python_isolated():
    from agent import wake

    assert wake._ACTION.split()[0] == "-I"


def test_an_install_does_not_walk_the_frozen_lock(tree, monkeypatch):
    """The lock is a checkout's tool; an install's files answer to its manifest."""
    walked = []
    monkeypatch.setattr(supervisor, "verify_lock", lambda policy: walked.append(1) or ["x"])
    _install(tree)

    assert supervisor._frozen_core_violations(object()) == []
    assert walked == []


def test_a_checkout_still_walks_the_frozen_lock(tree, monkeypatch):
    monkeypatch.setattr(supervisor, "verify_lock", lambda policy: ["core/x.py changed"])
    _checkout(tree)

    assert supervisor._frozen_core_violations(object()) == ["core/x.py changed"]


def test_free_disk_is_measured_where_the_data_is(tmp_path, monkeypatch):
    """The data's drive is the one that fills; the code may be on another."""
    measured = []

    class Usage:
        free = 100e9

    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: measured.append(p) or Usage())
    health._disk(None)

    assert measured == [paths.data_dir()]


def test_the_developer_tools_refuse_an_installs_code_folder(tmp_path, monkeypatch):
    """prove and mutate edit the tree they are given; an installed Steve's
    code is under Programs, not only its data."""
    data, code = tmp_path / "Steve", tmp_path / "Programs" / "Steve"
    monkeypatch.setattr(worktrees, "LIVE", (data, code))

    assert worktrees.is_live(code / "backend" / "app")
    assert worktrees.is_live(data)
    assert not worktrees.is_live(tmp_path / "checkout")


def test_the_real_live_folders_are_the_data_dir_and_the_install(monkeypatch):
    local = worktrees._LOCAL

    assert local / "Steve" in worktrees.LIVE
    assert local / "Programs" / "Steve" in worktrees.LIVE


def test_this_checkout_is_a_source_tree():
    """The real one: a checkout has no marker (the build writes it into the
    staged copy only), so nothing here runs as an install."""
    assert paths.layout() == "source"
    assert not (paths.SOURCE / paths.INSTALLED_MARKER).exists()


if sys.platform != "win32":  # pragma: no cover
    pytestmark = pytest.mark.skip("Windows only")
