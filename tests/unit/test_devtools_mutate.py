"""Breaking code on purpose never touches the tree it is run from, and never lies.

For ten days (28 September to 8 October 2026) the mutation check was a new
script per change, about sixty in one scratchpad, each pointed by hand at a
checkout other than the live one: the live agent spawns its workers from its
working tree, so a mutated file there is a mutated agent. One runner now.
What it must not do is the point: edit the tree it was started in, call a
replacement that matched nothing "caught", call a test that already failed
"caught everything", or leave a worktree registered in the repository the
watchdog reads.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devtools import mutate, worktrees


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                           *args], check=True, capture_output=True, text=True).stdout


def _write(path: Path, text: str) -> None:
    """LF, as every file in this repository is (CRLF is what `write_text`
    makes on Windows, and the runner matches bytes)."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    # As this repository's own .gitattributes: without it a Windows checkout
    # of a worktree turns the LF files into CRLF, and the bytes no longer match.
    _write(root / ".gitattributes", "* text=auto eol=lf\n")
    _write(root / "a.py", "x = 1\ny = 1\n")
    _write(root / "b.py", "z = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _m(name="m", file="a.py", old="x = 1\n", new="x = 2\n", test="tests/test_a.py::test_x"):
    return mutate.Mutation(name, file, old, new, test)


def test_a_replacement_that_matches_nowhere_or_twice_is_refused_before_anything_runs(repo):
    """It would have changed nothing -- or the wrong thing -- and every test
    would have passed, and the report would have said the test was blind."""
    _write(repo / "a.py", "x = 1\nx = 1\n")

    wrong = mutate.problems(repo, [_m("twice", old="x = 1\n"), _m("nowhere", old="q = 1\n"),
                                   _m("same", old="y", new="y"), _m("no file", file="c.py"),
                                   _m("no test", old="z", test=" ")])

    assert "twice: `old` occurs 2 times in a.py, not once" in wrong
    assert "nowhere: `old` occurs 0 times in a.py, not once" in wrong
    assert "same: `new` is the same as `old`" in wrong
    assert "no file: no file c.py" in wrong
    assert any(line.startswith("no test: no test named") for line in wrong)


def test_two_mutations_may_not_share_a_name(repo):
    assert "two mutations share a name" in mutate.problems(repo, [_m(), _m(old="y = 1\n")])


def test_a_good_spec_has_nothing_wrong_with_it(repo):
    assert mutate.problems(repo, [_m(), _m("second", file="b.py", old="z = 1\n")]) == []


@pytest.mark.parametrize("code, outcome", [(0, "MISSED"), (1, "CAUGHT"), (2, "CAUGHT"),
                                           (3, "BROKEN"), (4, "BROKEN"), (5, "BROKEN")])
def test_pytests_exit_code_is_read_as_missed_caught_or_broken(code, outcome):
    """0 every test passed with the code broken; 1 one failed; 2 collection
    broke; 3 and over the run itself went wrong (a timeout, no test named,
    nothing collected) -- which says nothing about the test."""
    assert mutate.verdict("m", code, "E   AssertionError: no").outcome == outcome


def test_a_run_whose_every_test_skipped_is_broken_not_a_miss():
    """pytest exits 0 when every test skips, as the screens' tests do in a
    tree without the UI's packages: a stylesheet's mutation was reported
    "missed" though no test had looked at it. Skips beside passes stay a
    miss, and say how many skipped."""
    skipped = mutate.verdict("m", 0, "sss\n3 skipped in 0.2s\n")
    some = mutate.verdict("m", 0, "..s\n2 passed, 1 skipped in 0.2s\n")

    assert (skipped.outcome, skipped.why) == ("BROKEN", "every test skipped (3): none tried it")
    assert (some.outcome, some.why) == ("MISSED",
                                           "every test passed with the code broken (1 skipped)")
    assert mutate.verdict("m", 0, "..\n2 passed in 0.1s\n").why == (
        "every test passed with the code broken")


def test_the_line_shown_is_the_first_error_not_the_last_line_of_the_run():
    output = "x\nE   AssertionError: the real reason\nE   more\n== 1 failed in 0.1s ==\n"

    assert mutate.first_failure(output) == "E   AssertionError: the real reason"
    assert mutate.first_failure("no errors\n3 passed") == "3 passed"
    assert mutate.first_failure("") == "(no output)"


def test_an_installed_steve_is_refused(tmp_path, monkeypatch):
    """An installed agent runs from that folder."""
    monkeypatch.setattr(worktrees, "LIVE", (tmp_path,))

    with pytest.raises(SystemExit, match="installed Steve"):
        mutate.run(tmp_path, [_m()])


def test_a_worktree_of_head_carries_the_changes_and_the_new_files_and_goes_away(repo, tmp_path):
    """What is tested is what is in front of you, not the last commit: an
    edit not yet committed, a file git has not been told about. And the
    worktree is unregistered after, or the repository keeps naming it."""
    _write(repo / "a.py", "x = 2\ny = 1\n")
    _write(repo / "fresh.py", "new = True\n")
    slot = tmp_path / "slot"

    made = mutate.Slot.make(repo, slot)

    assert _read(slot / "a.py") == "x = 2\ny = 1\n"
    assert _read(slot / "fresh.py") == "new = True\n"
    assert _read(repo / "a.py") == "x = 2\ny = 1\n", "the tree it was run from is untouched"
    made.remove(repo)
    worktrees.prune(repo)
    assert not slot.exists()
    assert len(_git(repo, "worktree", "list").strip().splitlines()) == 1


class _Machine:
    """pytest, as the slot would run it: an exit code per call, in order."""

    def __init__(self, *codes, boom=False):
        self.codes, self.boom, self.seen = list(codes), boom, []

    def __call__(self, argv, **kwargs):
        self.seen.append((argv, kwargs["cwd"]))
        if self.boom:
            raise RuntimeError("the runner died")
        return subprocess.CompletedProcess(argv, self.codes.pop(0), "E   boom\n", "")


def test_the_file_is_put_back_even_when_the_run_dies(tmp_path):
    """The reason this exists as one runner: a script that died between
    writing the mutation and restoring the file left the tree mutated."""
    _write(tmp_path / "a.py", "x = 1\n")

    with pytest.raises(RuntimeError):
        mutate.Slot(tmp_path).mutate_and_run(_m(old="x = 1\n", new="x = 9\n"), "py", 5,
                                             _Machine(boom=True))

    assert _read(tmp_path / "a.py") == "x = 1\n"


def test_a_mutation_is_run_with_the_file_changed_and_judged_by_the_exit_code(tmp_path):
    _write(tmp_path / "a.py", "x = 1\n")
    seen = []

    class Looks(_Machine):
        def __call__(self, argv, **kwargs):
            seen.append(_read(tmp_path / "a.py"))
            return super().__call__(argv, **kwargs)

    result = mutate.Slot(tmp_path).mutate_and_run(_m(old="x = 1\n", new="x = 9\n"), "py", 5,
                                                  Looks(1))

    assert seen == ["x = 9\n"] and result.outcome == "CAUGHT"
    assert _read(tmp_path / "a.py") == "x = 1\n"


class _Slot(mutate.Slot):
    """A slot that runs no pytest and has no worktree to remove: what each
    target does is decided by `decide(test, text_of_a.py)`."""

    def __init__(self, path, decide):
        super().__init__(path)
        self.decide = decide

    def pytest(self, test, python, timeout, runner):
        return self.decide(test, _read(self.path / "a.py"))

    def remove(self, root, runner=subprocess.run):
        pass


@pytest.fixture
def slot_dir(tmp_path):
    path = tmp_path / "slot"
    path.mkdir()
    _write(path / "a.py", "x = 1\ny = 1\n")
    return path


def test_a_test_that_already_fails_is_broken_not_a_catch_of_everything(repo, slot_dir):
    """Run unmutated first. One that fails with nothing wrong would "catch"
    every mutation and the whole report would be a lie."""
    results = mutate.run(repo, [_m()], slots=1, say=lambda line: None,
                         make_slot=lambda root, path: _Slot(
                             slot_dir, lambda test, text: (1, "E   it was always red\n")))

    assert [(r.outcome, r.why) for r in results] == [
        ("BROKEN", "fails with nothing mutated: E   it was always red")]


def test_each_mutation_is_caught_missed_or_broken_on_its_own_and_the_file_is_put_back(
        repo, slot_dir):
    """Read from what the slot's own pytest did with the file as mutated: one
    mutation a test notices, one it does not, one that leaves it nothing to
    run (pytest's exit 5)."""
    def decide(test, text):
        if "x = 2" in text:
            return 1, "E   it noticed\n"
        if "y = 3" in text:
            return 5, "no tests ran"
        return 0, "ok"

    said: list[str] = []
    results = mutate.run(
        repo, [_m("noticed", test="t::one"),
               _m("blind", old="y = 1\n", new="y = 2\n", test="t::two"),
               _m("lost", old="y = 1\n", new="y = 3\n", test="t::three")],
        slots=1, say=said.append, make_slot=lambda root, path: _Slot(slot_dir, decide))

    assert {r.name: r.outcome for r in results} == {
        "noticed": "CAUGHT", "blind": "MISSED", "lost": "BROKEN"}
    assert said[0] == "CAUGHT  noticed -- E   it noticed"
    assert _read(slot_dir / "a.py") == "x = 1\ny = 1\n"


def test_the_command_exits_one_when_anything_was_missed_and_zero_when_nothing_was(
        repo, monkeypatch, tmp_path):
    """So a loop that calls it, or a session that checks, stops on a miss."""
    spec = tmp_path / "spec.json"
    _write(spec, '[{"name": "m", "file": "a.py", "old": "x = 1\\n", "new": "x = 2\\n", '
                 '"test": ["t::a", "t::b"]}]')
    monkeypatch.setattr(mutate, "ROOT", repo)

    assert mutate.load(spec)[0].test == "t::a t::b", "a list of targets is one run of several"
    monkeypatch.setattr(mutate, "run", lambda *a, **k: [mutate.Verdict("m", "MISSED", "x")])
    assert mutate.main([str(spec)]) == 1
    monkeypatch.setattr(mutate, "run", lambda *a, **k: [mutate.Verdict("m", "CAUGHT", "x")])
    assert mutate.main([str(spec)]) == 0
    with pytest.raises(SystemExit, match="no mutation to run"):
        mutate.main([str(spec), "--only", "nothing is called this"])


def test_a_target_with_a_space_in_it_stays_one_target(tmp_path):
    """A parametrised id can hold spaces ("[Contoso 325th anniversary event]"); a
    spec quotes it, and pytest is handed it whole."""
    machine = _Machine(0)

    mutate.Slot(tmp_path).pytest("t::a 't::b[with a space]'", "py", 5, machine)

    argv = machine.seen[0][0]
    assert argv[3:5] == ["t::a", "t::b[with a space]"]
