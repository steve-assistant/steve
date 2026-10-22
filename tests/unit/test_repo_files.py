"""The files the repository-wide tests read (tests/gate/repo_files.py) are
git's list, tracked and new: never a walk of the disk, and never what git
tracks alone."""
from __future__ import annotations

import subprocess

from tests.gate import repo_files


def _git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_a_new_file_not_yet_added_to_git_is_listed_and_an_ignored_one_is_not(tmp_path):
    """The suite runs before a change is added to git. Scanning only what git
    tracked, step 3's test_keys.py named two dropped providers, passed the
    suite, and was committed; the next run failed on it."""
    _git("init", "-q", cwd=tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8", newline="\n")
    for name in ("added.txt", "new.txt", "ignored.txt"):
        (tmp_path / name).write_text("x\n", encoding="utf-8", newline="\n")
    _git("add", "added.txt", cwd=tmp_path)

    assert repo_files.listed_in(tmp_path) == (".gitignore", "added.txt", "new.txt")


def test_a_folder_git_ignores_and_a_nested_worktree_are_never_read(tmp_path):
    """Session worktrees, the venv, spikes and the installed tree in
    `build/` each held thousands of files that are not Steve's code; read,
    they made a test take a minute and then never finish. A folder git
    ignores is skipped without being named, and so is a nested worktree."""
    _git("init", "-q", cwd=tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\nbuild/\n", encoding="utf-8", newline="\n")
    for rel in ("agent/real.py", "tools/sub/ok.py", ".venv/Lib/site-packages/dep.py",
                "build/staging/backend/Lib/site-packages/dep.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x = 1\n", encoding="utf-8", newline="\n")
    _git("add", "agent/real.py", cwd=tmp_path)
    session = tmp_path / ".claude" / "worktrees" / "w1"
    (session / "agent").mkdir(parents=True)
    (session / "agent" / "old.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    _git("init", "-q", cwd=session)

    assert repo_files.listed_in(tmp_path) == (".gitignore", "agent/real.py", "tools/sub/ok.py")


def test_a_file_deleted_in_the_working_tree_is_not_listed(tmp_path):
    """Git still lists a tracked file the change deletes; a test that read it
    failed on a file that is gone."""
    _git("init", "-q", cwd=tmp_path)
    for name in ("kept.py", "gone.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8", newline="\n")
    _git("add", ".", cwd=tmp_path)
    (tmp_path / "gone.py").unlink()

    assert repo_files.listed_in(tmp_path) == ("kept.py",)


def test_the_python_files_are_those_listed_under_the_folders_named():
    every = repo_files.python()

    assert every and all(rel.endswith(".py") for rel in every)
    assert "tests/gate/repo_files.py" in every
    assert set(repo_files.python("agent/", "core/")) == {
        rel for rel in every if rel.startswith(("agent/", "core/"))}
