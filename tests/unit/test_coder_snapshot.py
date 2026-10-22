"""The folder as it was, which a change is measured from (core/coder/snapshot.py).

The base was a git commit until step 6. A snapshot has to answer the same
three questions -- what changed, what a file was, and the diff -- or the test
builder sees the coder's tests, the judge reads half a change, and a final
run of the checks that rewrote the folder passes.
"""

from __future__ import annotations

import os

import pytest

from core.coder import snapshot
from core.coder.workspace import is_readable


@pytest.fixture
def made(folder):
    return folder({"agent/x.py": "x = 1\n", "tests/unit/test_x.py": "def test_x():\n    pass\n",
                   "data/agent.db": "private\n", "keep.md": "kept\n"})


def test_a_file_added_changed_or_removed_since_is_changed_and_nothing_else(made):
    base = snapshot.take(made, is_readable)
    (made / "agent/x.py").write_text("x = 2\n", encoding="utf-8")
    (made / "agent/new.py").write_text("y = 1\n", encoding="utf-8")
    (made / "tests/unit/test_x.py").unlink()

    assert base.changed() == ["agent/new.py", "agent/x.py", "tests/unit/test_x.py"]


def test_what_a_file_was_is_its_text_then_or_none_if_it_was_not_there(made):
    base = snapshot.take(made, is_readable)
    (made / "agent/x.py").write_text("x = 2\n", encoding="utf-8")

    assert base.had("agent/x.py") == "x = 1\n"
    assert base.had("agent/new.py") is None


def test_the_diff_shows_new_and_removed_files_against_nothing(made):
    base = snapshot.take(made, is_readable)
    (made / "agent/new.py").write_text("y = 1\n", encoding="utf-8")
    (made / "keep.md").unlink()

    diff = base.diff()

    assert "--- /dev/null\n+++ b/agent/new.py" in diff and "+y = 1" in diff
    assert "--- a/keep.md\n+++ /dev/null" in diff and "-kept" in diff


def test_what_no_role_may_read_is_neither_in_the_snapshot_nor_walked_into(made):
    """Their data is not code: a snapshot that held it would put it in a diff
    a model reads."""
    (made / ".git").mkdir()
    (made / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    base = snapshot.take(made, is_readable)

    assert sorted(base.files) == ["agent/x.py", "keep.md", "tests/unit/test_x.py"]


@pytest.mark.skipif(os.name != "nt", reason="a junction is Windows'")
def test_a_junction_out_of_the_folder_is_not_followed(made, tmp_path):
    """A test can leave a link in the folder; followed, the next walk would
    read and diff whatever it points at."""
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("theirs\n", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(made / "agent" / "link"))

    assert not any(rel.startswith("agent/link") for rel in snapshot.take(made, is_readable).files)
