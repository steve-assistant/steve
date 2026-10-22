"""A change runs the tests that name it, the repo-wide checks, and not the suite.

A prototype over the last forty commits (8 October 2026): following imports
all the way down selected a median 68% of the suite's time, since a few
modules (`agent.loop`, `core.secrets`, `core.db`) are reached by most test
files; naming the changed file selected 12%. What is picked is what a change
can break that the test of the same subject does not show; what is not is
left to the full suite, which still runs once as the final gate.
"""

from __future__ import annotations

import subprocess

import pytest

from devtools import affected

# What the repository looks like in these tests: a few modules, two folders of
# tests, and the same fence conftests the real ones have.
SOURCE = {
    "agent/alpha.py": "VALUE = 1\n",
    "agent/loop.py": "",
    "agent/pkg/__init__.py": "",
    "agent/pkg/sub.py": "",
    "agent/models.py": "",
    "agent/prompts/system.md": "You are an agent.\n",
    "tools/beta.py": "",
    "core/policy.yaml": "frozen: []\n",
}
TESTS = {
    "tests/__init__.py": "",
    "tests/unit/__init__.py": "",
    "tests/gate/__init__.py": "",
    "tests/gate/fences.py": "",
    "tests/unit/conftest.py": "import agent.models\nfrom tests.gate import fences\n",
    "tests/gate/conftest.py": "from tests.gate import fences\n",
    "tests/unit/test_uses_alpha.py": "from agent import alpha\n",
    "tests/unit/test_patches_alpha.py":
        "def test_x(monkeypatch):\n    monkeypatch.setattr('agent.alpha.VALUE', 2)\n",
    "tests/unit/test_loop.py": "",
    "tests/unit/test_loop_extra.py": "",
    "tests/unit/test_reads_prompt.py":
        "from pathlib import Path\nTEXT = Path('agent/prompts/system.md').read_text()\n",
    "tests/unit/test_uses_sub.py": "import agent.pkg.sub\n",
    "tests/unit/test_uses_beta.py": "from tools import beta\n",
    "tests/unit/test_unrelated.py": "import json\n",
    "tests/unit/test_docs_are_true.py": "",
    "tests/gate/test_frozen_core.py": "",
    "tests/gate/test_uses_fences.py": "from tests.gate import fences\n",
    "tests/gate/test_unrelated_gate.py": "",
    "tests/unit/ui/__init__.py": "",
    "tests/unit/ui/test_a_screen.py": "",
}
REPO_WIDE = ("tests/unit/test_docs_are_true.py", "tests/gate/test_frozen_core.py")


@pytest.fixture
def repo(tmp_path):
    for rel, text in {**SOURCE, **TESTS}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _pick(repo, *changed, remove=()):
    files = sorted(f for f in {**SOURCE, **TESTS} if f.endswith(".py") and f not in remove)
    return affected.select(repo, list(changed), files=files, repo_wide=REPO_WIDE)


def test_a_test_that_imports_the_changed_module_is_picked_and_one_that_does_not_is_not(repo):
    chosen = _pick(repo, "agent/alpha.py")

    assert "tests/unit/test_uses_alpha.py" in chosen.tests
    assert chosen.tests["tests/unit/test_uses_alpha.py"] == ["imports agent.alpha"]
    assert "tests/unit/test_unrelated.py" not in chosen.tests
    assert "tests/unit/test_uses_beta.py" not in chosen.tests


def test_a_test_that_names_the_module_in_a_monkeypatch_string_is_picked(repo):
    """`monkeypatch.setattr("agent.provider.model_for", ...)` is how most of
    these tests reach a module, and it is not an import."""
    chosen = _pick(repo, "agent/alpha.py")

    assert "tests/unit/test_patches_alpha.py" in chosen.tests


def test_a_package_init_picks_the_tests_that_import_anything_in_the_package(repo):
    """`agent/toolset/__init__.py` runs whenever any of the package is
    imported, so its change reaches the tests of the modules beside it."""
    chosen = _pick(repo, "agent/pkg/__init__.py")

    assert "tests/unit/test_uses_sub.py" in chosen.tests


def test_a_test_called_after_the_changed_file_is_picked_without_importing_it(repo):
    chosen = _pick(repo, "agent/loop.py")

    assert {"tests/unit/test_loop.py", "tests/unit/test_loop_extra.py"} <= set(chosen.tests)
    assert "tests/unit/test_unrelated.py" not in chosen.tests


def test_a_test_that_reads_the_changed_file_by_its_path_is_picked(repo):
    """A prompt, a policy, a module read as text: not Python the test imports."""
    chosen = _pick(repo, "agent/prompts/system.md")

    assert chosen.tests["tests/unit/test_reads_prompt.py"] == ["names agent/prompts/system.md"]


def test_the_repo_wide_checks_are_always_picked(repo):
    chosen = _pick(repo, "tools/beta.py")

    assert set(REPO_WIDE) <= set(chosen.tests)
    assert chosen.tests["tests/unit/test_docs_are_true.py"] == ["repo-wide check"]


def test_a_changed_conftest_picks_every_test_in_its_folder_and_no_other(repo):
    """It can change any test beneath it, and none beside it."""
    chosen = _pick(repo, "tests/unit/conftest.py")

    assert {"tests/unit/test_unrelated.py", "tests/unit/test_uses_alpha.py"} <= set(chosen.tests)
    assert "tests/gate/test_unrelated_gate.py" not in chosen.tests


def test_a_change_to_the_apps_sources_picks_every_screen_test(repo):
    """The screens are built from all of ui/src, so a stylesheet no test
    names can break any of them; picked by the names they held, item 10's
    check ran 8 of the 41."""
    for changed in ("ui/src/shell.css", "ui/index.html", "ui/src-tauri/src/caption.rs"):
        chosen = _pick(repo, changed)
        assert "tests/unit/ui/test_a_screen.py" in chosen.tests, changed
        assert changed not in chosen.unmatched
    assert "tests/unit/ui/test_a_screen.py" not in _pick(repo, "agent/alpha.py").tests


def test_a_source_module_a_conftest_merely_imports_does_not_pick_its_folder(repo):
    """The unit conftest imports `agent.models` to fence it. Taking that for
    a dependency of every unit test would make any change to agent/models.py
    a full run -- the thing this exists to avoid."""
    chosen = _pick(repo, "agent/models.py")

    assert "tests/unit/test_unrelated.py" not in chosen.tests
    assert "tests/unit/test_uses_alpha.py" not in chosen.tests


def test_a_changed_helper_under_tests_picks_what_imports_it_and_the_conftests_that_do(repo):
    """tests/gate/fences.py is imported by both conftests, so a change to it
    is a change to every test in both folders -- which is true."""
    chosen = _pick(repo, "tests/gate/fences.py")

    assert "tests/gate/test_uses_fences.py" in chosen.tests
    assert {"tests/gate/test_unrelated_gate.py", "tests/unit/test_unrelated.py"} <= set(
        chosen.tests)


def test_a_deleted_module_still_picks_the_tests_that_imported_it(repo):
    """They would now fail to import it, which is the news."""
    chosen = _pick(repo, "agent/alpha.py", remove=("agent/alpha.py",))

    assert "tests/unit/test_uses_alpha.py" in chosen.tests


def test_a_changed_test_runs_itself(repo):
    chosen = _pick(repo, "tests/unit/test_unrelated.py")

    assert chosen.tests["tests/unit/test_unrelated.py"] == ["changed"]


def test_a_change_no_test_names_is_said_so_and_a_doc_is_not_one(repo):
    """core/policy.yaml here is named by no test: only the repo-wide checks
    stand behind it, and the report says so rather than looking thorough.
    A doc is read by test_docs_are_true, which always runs, and a module a
    test imports is named, not "unmatched"."""
    chosen = _pick(repo, "agent/alpha.py", "core/policy.yaml", "docs/flows.md")

    assert chosen.unmatched == ["core/policy.yaml"]


def test_the_selection_keeps_every_file_the_change_touched_for_the_checks_that_are_not_pytest(
        repo):
    """The check runs the shell's Rust tests when a file under ui/src-tauri/
    changed, and the selection was all it was given: the test files, and the
    Python no test names."""
    chosen = _pick(repo, "agent/alpha.py", "ui/src-tauri/src/signin.rs")

    assert chosen.changed == ["agent/alpha.py", "ui/src-tauri/src/signin.rs"]


def test_every_repo_wide_check_exists_and_is_a_test():
    """A renamed file would otherwise drop out of the always-run set without a
    word, and the loop would go on saying "ok"."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]

    for rel in affected.REPO_WIDE:
        assert (root / rel).is_file(), rel
        assert Path(rel).name.startswith("test_"), rel


# The guards that scan every file git lists, docs and the app's TypeScript
# among them. Pinned here, not read from affected.REPO_WIDE: a test that read
# the list would still pass with a guard taken off it.
TREE_GUARDS = (
    "tests/unit/test_docs_are_true.py",
    "tests/unit/test_no_personal_data.py",
    "tests/unit/test_no_vendor_names.py",
    "tests/unit/test_repo_hygiene.py",
    "tests/gate/test_hosts_named.py",
    "tests/gate/test_identity.py",
    "tests/gate/test_paths.py",
)
# And the guards that read every Python file of Steve's own, pinned alike.
SOURCE_GUARDS = (
    "tests/gate/test_account_health.py",
    "tests/gate/test_module_imports.py",
    "tests/gate/test_no_provider_names.py",
    "tests/gate/test_only_the_app.py",
    "tests/gate/test_sent_mail_only.py",
    "tests/gate/test_spend.py",
    "tests/gate/test_tagged_reads.py",
    "tests/gate/test_who_may_approve.py",
    "tests/gate/test_write_path_only.py",
    "tests/unit/test_commitments.py",
    "tests/unit/test_keys.py",
    "tests/unit/test_store.py",
)


@pytest.mark.parametrize("changed", ["docs/security-model.md", "README.md", "ui/src/App.tsx",
                                     "agent/modshare.py"])
def test_a_change_no_test_names_still_runs_every_guard_that_scans_the_whole_tree(
        tmp_path, changed):
    """Step 4 put OpenRouter's address in docs/security-model.md, and
    test_no_vendor_names failed on it. No test names a doc, so the check
    before the commit never ran that guard: the suite at the step's end was
    the first to. Step 6 read the people table outside `live` in
    agent/modshare.py, which only test_tagged_reads, reading every query,
    could see; it was not on the list either. A guard that scans every file
    runs whatever changed."""
    files = [*TREE_GUARDS, *SOURCE_GUARDS, "tests/unit/test_unrelated.py"]
    for rel in files:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")

    chosen = affected.select(tmp_path, [changed], files=files)

    assert {*TREE_GUARDS, *SOURCE_GUARDS} <= set(chosen.tests)
    assert "tests/unit/test_unrelated.py" not in chosen.tests


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    *args], check=True, capture_output=True)


GROUND = (
    '"""A module."""\n'
    "import os\n"
    "\n"
    "LIMIT = 3\n"
    "\n"
    "\n"
    "def kept(x):\n"
    "    return x\n"
    "\n"
    "\n"
    "@staticmethod\n"
    "def edited(x):\n"
    "    return x + 1\n"
)


@pytest.fixture
def history(tmp_path):
    """A repository whose agent/ground.py three tests import: one names its
    function `edited`, one names `kept`, one names neither."""
    files = {
        "agent/ground.py": GROUND,
        "tests/unit/test_edited.py": "from agent import ground\nground.edited(1)\n",
        "tests/unit/test_kept.py": "from agent import ground\nground.kept(1)\n",
        "tests/unit/test_neither.py": "import agent.ground\n",
        "tests/unit/test_mentions.py":
            '"""Reads nothing, but agent/ground.py is named here."""\n',
        "tests/unit/test_reads.py": "from pathlib import Path\nPath('agent/ground.py')\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _picked(root, new_text):
    (root / "agent/ground.py").write_text(new_text, encoding="utf-8", newline="\n")
    files = sorted(affected.python_files(root))
    return set(affected.select(root, ["agent/ground.py"], files=files, repo_wide=(),
                               base="HEAD").tests)


def test_an_edit_inside_a_function_picks_the_importers_that_name_it_and_no_other(history):
    """core/paths.py is imported by 74 test files: one new function there
    picked 1,408 tests, 633 s of them, though none could reach it."""
    changed = GROUND.replace("return x + 1", "return x + 2")

    assert _picked(history, changed) == {"tests/unit/test_edited.py", "tests/unit/test_reads.py"}


def test_a_new_function_with_its_blank_lines_and_comment_picks_no_importer(history):
    added = GROUND + "\n\n# A helper.\ndef fresh():\n    return 0\n"

    assert _picked(history, added) == {"tests/unit/test_reads.py"}


def test_a_change_to_the_modules_own_lines_picks_every_importer(history):
    """A constant, an import, code run at import: any importer can meet it."""
    for changed in (GROUND.replace("LIMIT = 3", "LIMIT = 4"),
                    GROUND.replace("import os", "import os, sys"),
                    GROUND.replace("LIMIT = 3\n", "")):
        assert _picked(history, changed) >= {
            "tests/unit/test_edited.py", "tests/unit/test_kept.py", "tests/unit/test_neither.py"}


def test_a_decorator_changed_counts_as_its_function(history):
    changed = GROUND.replace("@staticmethod\n", "@classmethod\n")

    assert _picked(history, changed) == {"tests/unit/test_edited.py", "tests/unit/test_reads.py"}


def test_a_file_named_in_a_docstring_is_not_read_by_that_test(history):
    """test_zero_downtime's docstrings mention core/paths.py; it reads it
    nowhere, and a change there picked its 120 s."""
    picked = _picked(history, GROUND.replace("return x + 1", "return x + 2"))

    assert "tests/unit/test_reads.py" in picked and "tests/unit/test_mentions.py" not in picked


def test_without_a_base_every_importer_is_picked_as_before(history):
    (history / "agent/ground.py").write_text(GROUND.replace("x + 1", "x + 2"), encoding="utf-8")
    files = sorted(affected.python_files(history))
    chosen = affected.select(history, ["agent/ground.py"], files=files, repo_wide=())

    assert {"tests/unit/test_kept.py", "tests/unit/test_neither.py"} <= set(chosen.tests)


def test_a_test_file_deleted_but_not_staged_is_neither_listed_nor_read(history):
    """Three test files removed with `rm`, not `git rm`, stopped the check
    with FileNotFoundError before it ran a test (21 October 2026)."""
    (history / "tests/unit/test_kept.py").unlink()

    files = affected.python_files(history)
    chosen = affected.select(history, ["tests/unit/test_kept.py"], repo_wide=(), base="HEAD")

    assert "tests/unit/test_kept.py" not in files and "tests/unit/test_kept.py" not in chosen.tests


def test_the_files_a_change_touches_are_read_from_git_deleted_and_untracked_too(tmp_path):
    """Compared with a base commit in the working tree: an edit not yet
    committed, a file removed, a new file git has not been told of."""

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                        *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    for name in ("kept.py", "edited.py", "removed.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    (tmp_path / "edited.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "removed.py").unlink()
    (tmp_path / "brand_new.py").write_text("", encoding="utf-8")

    assert affected.changed_files(tmp_path, "HEAD") == ["brand_new.py", "edited.py", "removed.py"]
    assert affected.default_base(tmp_path) == subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True,
        check=True).stdout.strip()
