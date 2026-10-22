"""The environment a test run in a scratch tree gets, in whatever account runs it.

`worktrees.pytest_env` looks for the embedding model in the checkout's own
data/ folder. As the coding sandbox's account that folder cannot even be
looked at -- the walls keep it out of their profile -- and `Path.is_dir()` raised
PermissionError. Twenty tests of test_devtools_check, test_devtools_mutate and
test_devtools_prove build that environment, so on 8 October 2026 the first run
of the suite as the sandbox account after they were added failed all twenty, which is a
baseline that fails: every attempt that night would have been refused as the
harness's own.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from devtools import worktrees


@pytest.mark.processes
def test_pytest_in_a_scratch_tree_imports_only_that_trees_code(tmp_path):
    """The venv's editable install maps core/, agent/ and the rest to this
    checkout, behind the normal lookup. A module the scratch tree lacks --
    the one the change under proof added -- came from here instead, so
    `prove` ran the old code with the new module in it. Starts pytest twice."""
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "test_old_tree.py").write_text(
        "import pytest\n\n\n"
        "def test_a_module_this_tree_lacks_is_not_found():\n"
        "    with pytest.raises(ImportError):\n"
        "        import core.identity  # noqa: F401\n",
        encoding="utf-8", newline="\n")
    args = ["test_old_tree.py", "-q", "-p", "no:cacheprovider"]

    def run(argv):
        return subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True,
                              env=worktrees.pytest_env(), timeout=120)

    if run([sys.executable, "-m", "pytest", *args]).returncode == 0:
        pytest.skip("no editable install here for the old tree to leak through")
    tree_only = run(worktrees.pytest_argv(sys.executable, *args))

    assert tree_only.returncode == 0, tree_only.stdout + tree_only.stderr


def test_the_checkouts_models_folder_is_the_cache_when_it_can_be_read(tmp_path, monkeypatch):
    models = tmp_path / "data" / "models"
    models.mkdir(parents=True)
    monkeypatch.setenv("STEVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("STEVE_MODEL_CACHE", raising=False)

    assert worktrees.pytest_env()["STEVE_MODEL_CACHE"] == str(models)


def test_a_cache_the_caller_named_is_left_alone(tmp_path, monkeypatch):
    (tmp_path / "data" / "models").mkdir(parents=True)
    monkeypatch.setenv("STEVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STEVE_MODEL_CACHE", "elsewhere")

    assert worktrees.pytest_env()["STEVE_MODEL_CACHE"] == "elsewhere"


def test_a_folder_the_account_may_not_look_into_is_no_cache_and_no_error(tmp_path, monkeypatch):
    """Asked as the sandbox's account would be: the answer to "is it a
    folder?" is a refusal, and the environment is built without it."""
    models = tmp_path / "data" / "models"
    models.mkdir(parents=True)
    monkeypatch.setenv("STEVE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("STEVE_MODEL_CACHE", raising=False)
    real = Path.is_dir

    def refused(self, *args, **kwargs):
        if self == models:
            raise PermissionError(5, "Access is denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", refused)

    assert "STEVE_MODEL_CACHE" not in worktrees.pytest_env()


def test_a_scratch_tree_shares_the_checkouts_module_runtime_and_its_removal_keeps_it(tmp_path):
    """The sandbox's escape tests need the module runtime, which is built
    into a checkout's ignored `.runtime/` and so is in no scratch tree: there
    every mutation of a wall was caught by its absence. The tree is given a
    junction to it, and removing the tree must take the junction, not empty
    the checkout's runtime through it."""
    root, tree = tmp_path / "root", tmp_path / "tree"
    (root / ".runtime" / "python").mkdir(parents=True)
    (root / ".runtime" / "built.json").write_text("{}", encoding="utf-8")
    tree.mkdir()

    worktrees.share_builds(root, tree)
    assert (tree / ".runtime" / "built.json").read_text(encoding="utf-8") == "{}"

    worktrees.remove(root, tree, runner=lambda *args, **kwargs: None)
    assert not tree.exists()
    assert (root / ".runtime" / "built.json").read_text(encoding="utf-8") == "{}"


def test_a_checkout_with_no_runtime_gives_its_scratch_trees_none(tmp_path):
    root, tree = tmp_path / "root", tmp_path / "tree"
    root.mkdir()
    tree.mkdir()
    worktrees.share_builds(root, tree)
    assert not (tree / ".runtime").exists()


def test_a_scratch_tree_shares_the_checkouts_ui_packages_and_its_removal_keeps_them(tmp_path):
    """The screens' tests build the UI with the checkout's ignored
    `ui/node_modules` and skip without it, so in a scratch tree every mutation
    of a stylesheet or a screen went untried, and was reported as missed. The
    tree is given a junction to them, which its removal takes alone."""
    root, tree = tmp_path / "root", tmp_path / "tree"
    (root / "ui" / "node_modules" / "vite").mkdir(parents=True)
    (root / "ui" / "node_modules" / "vite" / "package.json").write_text("{}", encoding="utf-8")
    (tree / "ui").mkdir(parents=True)

    worktrees.share_builds(root, tree)
    assert (tree / "ui" / "node_modules" / "vite" / "package.json").exists()

    worktrees.remove(root, tree, runner=lambda *args, **kwargs: None)
    assert not tree.exists()
    assert (root / "ui" / "node_modules" / "vite" / "package.json").exists()
