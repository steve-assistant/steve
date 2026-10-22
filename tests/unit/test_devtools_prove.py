"""A new test is shown to fail without the change, not taken on trust.

Upgrade #80 (8 October 2026) came with a test whose judge found that it
"succeeds only on the newly introduced phrase... the prior implementation
already generated and ran a 325th fallback": it passed on the old code, so it
proved nothing, and only a reader noticed. devtools/prove.py runs the tests a
change added or altered against the code from before it. These tests pin how
it decides which tests those are, how it reads what happened, and -- once, for
real, in a scratch git repository -- that it tells a test that needs a fix
from one that does not.
"""

from __future__ import annotations

import subprocess
import textwrap

import pytest

from devtools import junit, prove

BASE = textwrap.dedent('''
    import pytest

    LIMIT = 3

    @pytest.fixture
    def store():
        return {}

    @pytest.fixture
    def other():
        return []

    def helper():
        return LIMIT

    def test_unchanged(store):
        """Says what it does."""
        assert store == {}

    def test_uses_the_helper():
        assert helper() == 3

    def test_uses_the_other_fixture(other):
        assert other == []

    def test_will_be_edited():
        assert 1 == 1

    def test_will_be_moved():
        assert 2 == 2
''')


def _head(old: str, new: str) -> str:
    assert old in BASE, old
    return BASE.replace(old, new)


def test_a_test_whose_syntax_changed_is_in_scope_and_one_that_did_not_is_not():
    head = _head("assert 1 == 1", "assert 1 == 1 and True")

    assert prove.in_scope(BASE, head) == {"test_will_be_edited"}


def test_a_new_test_is_in_scope_and_so_is_every_test_of_a_new_file():
    head = BASE + "\ndef test_added():\n    assert True\n"

    assert prove.in_scope(BASE, head) == {"test_added"}
    assert "test_unchanged" in prove.in_scope(None, head)


def test_a_docstring_or_a_move_is_not_a_change():
    """Neither changes what a test does, and calling them changes would bury
    the real ones in a list of tests that prove nothing because nothing changed."""
    text = BASE.replace('"""Says what it does."""', '"""Says it differently."""')
    first, last = text.index("def test_will_be_edited"), text.index("def test_will_be_moved")
    moved = text[:first] + text[last:] + "\n" + text[first:last]

    assert prove.in_scope(BASE, moved) == set()


def test_a_changed_fixture_puts_in_scope_the_tests_that_use_it_and_no_others():
    head = _head("return []", "return [1]")

    assert prove.in_scope(BASE, head) == {"test_uses_the_other_fixture"}


def test_a_changed_constant_reaches_the_tests_through_the_helper_that_reads_it():
    """The test never names LIMIT: it calls the helper that does."""
    head = _head("LIMIT = 3", "LIMIT = 4")

    assert prove.in_scope(BASE, head) == {"test_uses_the_helper"}


def test_a_change_that_is_not_a_name_puts_every_test_in_scope():
    """An `if` at module level, a loop that builds tests: any test may lean
    on it, and saying so is the safe error."""
    head = BASE + "\nif True:\n    LIMIT = 5\n"

    assert prove.in_scope(BASE, head) == {
        "test_unchanged", "test_uses_the_helper", "test_uses_the_other_fixture",
        "test_will_be_edited", "test_will_be_moved"}


def test_a_base_that_does_not_parse_puts_every_test_in_scope():
    assert "test_unchanged" in prove.in_scope("def broken(:\n", BASE)


def _o(node, status="passed"):
    return junit.Outcome(node, status, 0.1)


def test_a_test_is_judged_by_how_it_came_out_at_each_end():
    scope = {"tests/unit/test_x.py": {"test_new", "TestK::test_in_class"}}
    base = [_o("tests/unit/test_x.py::test_new", "failed"),
            _o("tests/unit/test_x.py::TestK::test_in_class", "passed"),
            _o("tests/unit/test_x.py::test_unrelated", "failed")]
    head = [_o("tests/unit/test_x.py::test_new"),
            _o("tests/unit/test_x.py::TestK::test_in_class"),
            _o("tests/unit/test_x.py::test_unrelated")]

    proofs = prove.judge(scope, base, head)

    assert [(p.node, p.at_base, p.at_head) for p in proofs] == [
        ("tests/unit/test_x.py::test_new", "failed", "passed"),
        ("tests/unit/test_x.py::TestK::test_in_class", "passed", "passed")]


def test_a_file_that_would_not_import_at_the_base_is_an_error_for_every_test_in_it():
    """It needs code the change adds. pytest writes one error for the module
    and nothing for the tests, which is how it is told from "did not run"."""
    scope = {"tests/unit/test_y.py": {"test_a", "test_b"}}
    base = [_o("tests/unit/test_y.py", "error")]
    head = [_o("tests/unit/test_y.py::test_a"), _o("tests/unit/test_y.py::test_b[1]"),
            _o("tests/unit/test_y.py::test_b[2]")]

    proofs = prove.judge(scope, base, head)

    assert {p.at_base for p in proofs} == {"error"} and len(proofs) == 3


def test_parametrised_ids_are_one_line_and_say_how_many():
    proofs = [prove.Proof(f"tests/unit/test_x.py::test_p[{i}]", "failed", "passed")
              for i in range(7)]

    code, lines = prove.verdict(proofs, code_changed=True)

    assert code == 0
    assert lines[0] == "FAILS AT BASE   tests/unit/test_x.py::test_p (x7)"
    assert lines[-1] == "7 of 7 new or changed tests need the change; 0 pass without it"


def test_code_changed_and_every_new_test_passing_without_it_is_unproven():
    """#80's shape: the tests it came with pass on the code it replaced."""
    proofs = [prove.Proof("tests/unit/test_x.py::test_a", "passed", "passed")]

    code, lines = prove.verdict(proofs, code_changed=True)

    assert code == 1
    assert lines[-1].startswith("UNPROVEN")
    assert "PASSES AT BASE  tests/unit/test_x.py::test_a  (it proves nothing new)" in lines


def test_a_rewritten_test_with_no_code_change_passing_at_the_base_is_not_a_finding():
    proofs = [prove.Proof("tests/unit/test_x.py::test_a", "passed", "passed")]

    assert prove.verdict(proofs, code_changed=False)[0] == 0


def test_one_proving_test_is_enough_and_a_test_failing_now_is_never_fine():
    proving = [prove.Proof("a::t1", "failed", "passed"), prove.Proof("a::t2", "passed", "passed")]
    broken = [prove.Proof("a::t1", "failed", "failed")]

    assert prove.verdict(proving, code_changed=True)[0] == 0
    code, lines = prove.verdict(broken, code_changed=True)
    assert code == 1 and "FAILS AT HEAD   a::t1" in lines


def test_code_changed_with_no_test_added_is_noted_not_failed():
    code, lines = prove.verdict([], code_changed=True)

    assert code == 0 and lines[-1].startswith("note:")


@pytest.mark.processes
def test_a_fix_with_a_test_that_needs_it_and_one_that_does_not_is_told_apart(tmp_path):
    """The whole thing, for real, in a scratch repository: a bug committed,
    then its fix and two tests -- one of which fails on the bug and one of
    which would have passed on it. Starts pytest twice, hence `processes`."""

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                        *args], check=True, capture_output=True)

    (tmp_path / "tests").mkdir()
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "tests" / "test_calc.py").write_text(textwrap.dedent('''
        from calc import add

        def test_adding_two_and_three_gives_five():
            assert add(2, 3) == 5

        def test_adding_zero_changes_nothing():
            assert add(4, 0) == 4
    '''), encoding="utf-8")
    said: list[str] = []

    code = prove.prove(tmp_path, "HEAD", say=said.append, scratch=tmp_path / "scratch", timeout=120)

    assert code == 0, said
    assert "FAILS AT BASE   tests/test_calc.py::test_adding_two_and_three_gives_five" in said
    assert any(line.startswith("PASSES AT BASE  tests/test_calc.py::test_adding_zero")
               for line in said)
    assert said[-1] == "1 of 2 new or changed tests need the change; 1 pass without it"
    worktrees = subprocess.run(["git", "-C", str(tmp_path), "worktree", "list"], check=True,
                               capture_output=True, text=True).stdout
    assert len(worktrees.strip().splitlines()) == 1, "the scratch worktree was left behind"


def test_a_test_that_cannot_be_set_up_at_the_base_is_run_again_given_the_new_files():
    """A guard that scans the tree with a tool the change adds errored at the
    base -- "cannot be set up" -- which says nothing about whether the old
    tree breaks it. Step 2's personal-data guard was proven by hand for that
    reason. Its files are the ones run again."""
    scope = {"tests/unit/test_guard.py": {"test_scan"},
             "tests/unit/test_fine.py": {"test_a"},
             "tests/unit/test_gone.py": {"test_b"},
             "tests/unit/test_new.py": {"test_c"}}
    base = [_o("tests/unit/test_guard.py", "error"),
            _o("tests/unit/test_fine.py::test_a", "failed"),
            _o("tests/unit/test_gone.py::test_b[1]", "passed"),
            _o("tests/unit/test_gone.py::test_b[2]", "error")]

    assert prove.unset(scope, base) == ["tests/unit/test_gone.py", "tests/unit/test_guard.py",
                                        "tests/unit/test_new.py"]


def test_a_guard_failing_on_the_old_tree_given_the_new_files_proves_something():
    scope = {"tests/unit/test_guard.py": {"test_scan", "test_tool"}}
    base = [_o("tests/unit/test_guard.py", "error")]
    given_new = [_o("tests/unit/test_guard.py::test_scan", "failed"),
                 _o("tests/unit/test_guard.py::test_tool", "passed")]
    head = [_o("tests/unit/test_guard.py::test_scan"), _o("tests/unit/test_guard.py::test_tool")]

    proofs = prove.judge(scope, base, head, given_new)
    code, lines = prove.verdict(proofs, code_changed=True)

    assert [p.at_base for p in proofs] == ["failed+new", "passed+new"]
    assert code == 0
    assert ("FAILS AT BASE   tests/unit/test_guard.py::test_scan  (given the change's new files)"
            in lines)
    assert ("NEEDS NEW FILES tests/unit/test_guard.py::test_tool  "
            "(it passes on the old tree once they are there)") in lines
    assert lines[-1] == "2 of 2 new or changed tests need the change; 0 pass without it"


def test_a_test_of_new_files_alone_still_proves_the_change_is_needed():
    """A new module with its own tests passes at the base once the module is
    there. It is not #80's shape -- the old code could not have passed it --
    so it is not unproven."""
    proofs = [prove.Proof("a::t1", "passed+new", "passed")]

    code, lines = prove.verdict(proofs, code_changed=True)

    assert code == 0 and not lines[-1].startswith("UNPROVEN")


@pytest.mark.processes
def test_a_guard_built_on_a_new_tool_is_shown_failing_on_the_old_tree(tmp_path):
    """For real: the change adds a scanner and the guard that uses it, and
    cleans the file the guard reads. At the base the guard cannot import the
    scanner; given it, the old file fails the guard. Starts pytest three
    times, hence `processes`."""

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
                        *args], check=True, capture_output=True)

    (tmp_path / "tests").mkdir()
    (tmp_path / "notes.txt").write_text("a secret\n", encoding="utf-8")
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    (tmp_path / "notes.txt").write_text("nothing\n", encoding="utf-8")
    (tmp_path / "scan.py").write_text(
        "from pathlib import Path\n\ndef found():\n"
        "    return 'secret' in Path('notes.txt').read_text()\n", encoding="utf-8")
    (tmp_path / "tests" / "test_notes.py").write_text(
        "from scan import found\n\ndef test_no_note_says_secret():\n    assert not found()\n",
        encoding="utf-8")
    said: list[str] = []

    code = prove.prove(tmp_path, "HEAD", say=said.append, scratch=tmp_path / "scratch", timeout=120)

    assert code == 0, said
    assert ("FAILS AT BASE   tests/test_notes.py::test_no_note_says_secret  "
            "(given the change's new files)") in said
