"""The loop's one command: cheap checks first, the suite once, failures in a line.

Between 28 September and 8 October 2026 a quarter of 135 full runs failed,
mostly on things a twenty-second look would have caught (lint, a frozen file
edited without the lock rewritten, a tool unclassified), and the output was
piped through `tail`, which kept the count and lost the reason. These tests
fix the order of the stages, what is never deselected, and what is said about
a failure, with a stand-in for the subprocesses: nothing here runs pytest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devtools import affected, check

CASE = '<testcase classname="{module}" name="{name}" time="0.5">{body}</testcase>'
FAIL = '<failure message="m">trace\nE   {line}</failure>'


def _junit(passed=(), failed=()) -> str:
    cases = [CASE.format(module=m, name=n, body="") for m, n in passed]
    cases += [CASE.format(module=m, name=n, body=FAIL.format(line=line))
              for m, n, line in failed]
    return f'<testsuites><testsuite tests="{len(cases)}">{"".join(cases)}</testsuite></testsuites>'


class _Machine:
    """Stands in for `subprocess.run`: ruff, and pytest that writes the junit
    file it is asked for. `alone` is what a test does when run by itself."""

    def __init__(self, *, lint="", suite=None, alone=None, silent=False, hang=False,
                 hang_stderr=None, exit_code=None, stuck=None, cargo=("", 0), secrets=("", 0),
                 rewrites=None):
        self.lint, self.suite, self.alone = lint, suite or _junit(), alone or {}
        self.cargo = cargo  # what `cargo test` prints, and its exit code
        self.secrets = secrets  # what the secrets hook prints, and its exit code
        self.rewrites = rewrites  # the baseline the hook writes, CRLF as on Windows
        self.silent, self.hang, self.exit_code = silent, hang, exit_code
        self.hang_stderr, self.stuck = hang_stderr, stuck  # stuck: (node, its stacks)
        self.argv: list[list[str]] = []
        self.timeouts: list[int] = []

    def __call__(self, argv, **kwargs):
        self.argv.append(argv)
        if "ruff" in argv:
            return subprocess.CompletedProcess(argv, 1 if self.lint else 0, self.lint, "")
        self.timeouts.append(kwargs["timeout"])
        if argv[0] == "cargo":
            return subprocess.CompletedProcess(argv, self.cargo[1], self.cargo[0], "")
        if argv[0] == "uvx":
            if self.rewrites:
                path, text = self.rewrites
                path.write_bytes(text.replace("\n", "\r\n").encode())
            return subprocess.CompletedProcess(argv, self.secrets[1], self.secrets[0], "")
        if self.stuck:  # as devtools.stuck leaves a stuck test's stacks
            folder = Path(next(a for a in argv if a.startswith("stuck_dir=")).split("=", 1)[1])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "4242.txt").write_text("\n".join(self.stuck), encoding="utf-8")
        if self.hang:
            raise subprocess.TimeoutExpired(argv, 1, output="partial output",
                                            stderr=self.hang_stderr)
        xml = Path(next(a for a in argv if a.startswith("--junitxml=")).split("=", 1)[1])
        nodes = [a for a in argv if "::" in a]
        if self.silent:
            return subprocess.CompletedProcess(argv, 2, "line 1\nINTERNALERROR the end\n", "")
        if nodes:
            xml.write_text(self.alone.get("xml") or _junit(
                passed=[("tests.unit.test_a", n.split("::")[1]) for n in nodes
                        if n in self.alone.get("pass", ())],
                failed=[("tests.unit.test_a", n.split("::")[1], "still wrong")
                        for n in nodes if n not in self.alone.get("pass", ())]),
                encoding="utf-8")
        else:
            xml.write_text(self.suite, encoding="utf-8")
        code = 1 if "<failure" in xml.read_text() else 0
        return subprocess.CompletedProcess(argv, code if self.exit_code is None
                                           else self.exit_code, "boom\n", "")


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "tests/unit").mkdir(parents=True)
    (tmp_path / "tests/unit/test_a.py").write_text("", encoding="utf-8")
    return tmp_path


def _check(tree, machine, said, *, problems=(), selected=("tests/unit/test_a.py",), dist="",
           test_files=100, changed=()):
    chosen = affected.Selection(changed=list(changed))
    for rel in selected:
        chosen.pick(rel, "changed", rel)
    return check.Check(tree, python="py", runner=machine, say=said.append, workers=3,
                       base="HEAD", dist=dist, scratch=tree / "scratch",
                       lock_problems=lambda root: list(problems),
                       select_tests=lambda root, base: chosen,
                       count_tests=lambda root: test_files)


def test_a_lint_failure_stops_the_run_before_any_test_is_started(tree):
    """`ruff` caught five full runs in a week, each after four minutes."""
    machine, said = _Machine(lint="E501 Line too long"), []

    code = _check(tree, machine, said).run(full=True)

    assert code == 1
    assert machine.argv == [["py", "-m", "ruff", "check", "."]]
    assert "  E501 Line too long" in said


def test_a_stale_frozen_lock_stops_the_run_and_says_which_files_and_what_to_do(tree):
    """Three known tests failed after four minutes on 4 October (twice) and
    6 October: a frozen file edited, the lock not rewritten."""
    machine, said = _Machine(), []

    code = _check(tree, machine, said,
                  problems=["frozen file modified: core/supervisor.py"]).run(full=True)

    assert code == 1
    assert not any("pytest" in argv for argv in machine.argv)
    assert "  - frozen file modified: core/supervisor.py" in said
    assert any("--write-lock" in line for line in said)


def test_the_lock_may_be_set_aside_while_a_frozen_file_is_still_being_edited(tree):
    machine, said = _Machine(suite=_junit(passed=[("tests.unit.test_a", "test_x")])), []

    code = _check(tree, machine, said, problems=["frozen file modified: core/x.py"]).run(
        skip_lock=True)

    assert code == 0
    assert any("skipped" in line for line in said)


def test_the_secrets_step_scans_the_files_the_change_left_with_cis_command_before_any_test(tree):
    """CI scans every tracked file; a value's line moves only in a file
    that changed, so those are enough here, and a gone file or the
    baseline itself is no file to scan."""
    machine, said = _Machine(suite=_junit(passed=[("tests.unit.test_a", "test_x")])), []

    code = _check(tree, machine, said, changed=["tests/unit/test_a.py", "gone.py",
                                                check.BASELINE]).run()

    assert code == 0
    assert machine.argv[1] == [*check.SECRETS_HOOK, "tests/unit/test_a.py"]
    assert "pytest" in machine.argv[2]


def test_a_moved_secret_line_stops_the_run_and_leaves_the_rewritten_baseline_to_commit_in_lf(
        tree):
    """Twice on 22 October 2026 CI's check job failed half an hour after a
    push because an edit moved a known value's line. The hook rewrites the
    baseline here instead, with Windows' line ends, which the repository's
    hygiene test refuses."""
    baseline = tree / check.BASELINE
    machine = _Machine(secrets=("The baseline file was updated.\n", 3),
                       rewrites=(baseline, '{\n  "line_number": 233\n}\n'))
    said: list[str] = []

    code = _check(tree, machine, said, changed=["tests/unit/test_a.py"]).run()

    assert code == 1
    assert not any("pytest" in argv for argv in machine.argv)
    assert baseline.read_bytes() == b'{\n  "line_number": 233\n}\n'
    assert "secrets        FAILED" in said and "  The baseline file was updated." in said
    assert any("commit it with the change" in line for line in said)


def test_a_machine_without_uvx_is_told_ci_will_scan_and_its_tests_still_run(tree):
    class _NoUvx(_Machine):
        def __call__(self, argv, **kwargs):
            if argv[0] == "uvx":
                raise FileNotFoundError(argv[0])
            return super().__call__(argv, **kwargs)

    machine, said = _NoUvx(suite=_junit(passed=[("tests.unit.test_a", "test_x")])), []

    code = _check(tree, machine, said, changed=["tests/unit/test_a.py"]).run()

    assert code == 0
    assert any(line.startswith("secrets        skipped") for line in said)


def test_the_quick_run_leaves_out_the_process_tests_and_the_full_run_leaves_nothing_out(tree):
    """The marker is for the loop. The run that counts -- the final gate --
    never deselects a test; one that did could pass on a suite with the
    supervisor tests silently off."""
    suite = _junit(passed=[("tests.unit.test_a", "test_x")])
    quick, full = _Machine(suite=suite), _Machine(suite=suite)

    _check(tree, quick, []).run()
    _check(tree, full, []).run(full=True)

    runs = [a for a in quick.argv if "pytest" in a]
    assert len(runs) == 1 and "not processes" in runs[0]
    assert runs[0].count("-n") == 1 and runs[0][runs[0].index("-n") + 1] == "2"
    last = [a for a in full.argv if "pytest" in a][-1]
    assert "not processes" not in last and last[3] == "tests"
    assert last[last.index("-n") + 1] == "3"
    assert "-p" in last and "no:cacheprovider" in last


def test_a_commits_check_runs_the_named_tests_that_start_processes_and_nothing_else(tree):
    """The whole suite runs once per series of commits, so a commit is checked
    by the tests it names. Left out, a supervisor change would be committed
    with none of its tests run."""
    suite = _junit(passed=[("tests.unit.test_a", "test_x")])
    machine = _Machine(suite=suite)

    code = _check(tree, machine, []).run(processes=True)

    runs = [a for a in machine.argv if "pytest" in a]
    assert code == 0 and len(runs) == 1
    assert "not processes" not in runs[0]
    assert "tests/unit/test_a.py" in runs[0] and "tests" not in runs[0]


NAMED = [f"tests/unit/test_{i}.py" for i in range(40)]


def test_a_change_that_names_most_of_the_suite_is_checked_by_all_of_it_on_every_worker(tree):
    """On 14 October 2026 item 8 named 122 of the 256 test files; on two
    workers they ran into the 900 s limit at 97% with no failure, and the
    whole suite on three took 7 min 14 s. Below the share, the named files
    are still all that runs."""
    suite = _junit(passed=[("tests.unit.test_a", "test_x")])
    most, some = _Machine(suite=suite), _Machine(suite=suite)
    said: list[str] = []

    assert _check(tree, most, said, selected=NAMED).run(processes=True) == 0
    _check(tree, some, [], selected=NAMED[:39]).run(processes=True)

    [run] = [a for a in most.argv if "pytest" in a]
    assert run[3] == "tests" and not [a for a in run if a.startswith("tests/unit/")]
    assert run[run.index("-n") + 1] == "3" and most.timeouts == [check.LIMIT_S["whole"]]
    assert any("most of the suite" in line for line in said)
    [run] = [a for a in some.argv if "pytest" in a]
    assert "tests" not in run and run[run.index("-n") + 1] == "2"
    assert some.timeouts == [check.LIMIT_S["affected"]]


def test_a_commits_check_tells_the_selection_where_the_change_began(monkeypatch, tmp_path):
    """Without the base, every importer of a module is picked for any edit
    in it, though the edit was inside one function (`affected.touched`)."""
    seen: dict = {}

    def select(root, changed, **given):
        seen.update(given, changed=changed)
        return affected.Selection()

    monkeypatch.setattr(affected, "changed_files", lambda root, base: ["core/x.py"])
    monkeypatch.setattr(affected, "select", select)
    check._named_by_the_change(tmp_path, "abc123")

    assert seen == {"base": "abc123", "changed": ["core/x.py"]}


def test_a_full_check_of_most_of_the_suite_runs_the_suite_once(tree):
    """An affected run of most of the suite before the full run was most of
    it twice: every --full on record spent 250 to 440 s on it (step 11).
    With the process tests in or left out, the one run is the full one,
    nothing left out, with the distribution asked for."""
    suite = _junit(passed=[("tests.unit.test_a", "test_x")])
    gate, quick = _Machine(suite=suite), _Machine(suite=suite)

    assert _check(tree, gate, [], selected=NAMED, dist="worksteal").run(
        full=True, processes=True) == 0
    assert _check(tree, quick, [], selected=NAMED, dist="worksteal").run(full=True) == 0

    for machine in (gate, quick):
        [run] = [a for a in machine.argv if "pytest" in a]
        assert "not processes" not in run and run[run.index("--dist") + 1] == "worksteal"


def test_every_run_stops_a_stuck_test_and_a_crashed_run_names_it_from_the_stack(tree):
    """Item 8's check ended in dots and "timed out", and nothing said which
    test hung. Every run loads `devtools.stuck`, which prints the stacks of
    a test stuck past its time on the error stream, and the test's own
    frame is picked out of them (tests/unit/test_devtools_stuck.py runs it)."""
    stack = ("Timeout (0:02:00)!\nThread 0x1 (most recent call first):\n"
             '  File "C:\\x\\Lib\\threading.py", line 359 in wait\n'
             '  File "C:\\x\\Steve\\tests\\unit\\test_b.py", line 42 in test_y\n'
             + '  File "C:\\x\\pluggy\\_callers.py", line 1 in _multicall\n' * 60)
    machine, said = _Machine(hang=True, hang_stderr=stack), []

    code = _check(tree, machine, said).run()

    run = [a for a in machine.argv if "pytest" in a][0]
    assert run[run.index("devtools.stuck") - 1] == "-p"
    assert code == 1
    assert "  on a stack: tests/unit/test_b.py::test_y, line 42" in said


def test_the_slowest_tests_are_named_after_every_run_with_the_command_that_says_why(tree):
    """The zero-downtime tests took 117 s of a 407 s run on 14 October, and
    only a timing audit by hand said so. Every run names its slowest, and
    `devtools.slow` shows where the slowest one's time went."""
    timed = ('<testcase classname="tests.unit.test_a" name="{}" time="{}"/>')
    suite = ('<testsuites><testsuite tests="3">'
             + timed.format("test_slow", "12.0") + timed.format("test_slower_than_most", "6.0")
             + timed.format("test_quick", "0.5") + "</testsuite></testsuites>")
    machine, said = _Machine(suite=suite), []

    code = _check(tree, machine, said).run()

    assert code == 0
    assert "     12.0 s  tests/unit/test_a.py::test_slow" in said
    assert "      6.0 s  tests/unit/test_a.py::test_slower_than_most" in said
    assert not any("test_quick" in line for line in said)
    assert "  where its time went: python -m devtools.slow tests/unit/test_a.py::test_slow" in said


def test_a_test_stuck_under_xdist_is_named_with_its_own_frames_from_the_file_it_left(tree):
    """Under xdist a stuck worker's stacks went to the stream pytest
    captures, and only the test's name was left. `devtools.stuck` writes
    them to a file in a folder the check names, and the check prints the
    frames of the project in the stuck test's thread."""
    dump = ("Timeout (0:02:00)!\nThread 0x1 (most recent call first):\n"
            f'  File "{tree}\\.venv\\Lib\\threading.py", line 359 in wait\n'
            f'  File "{tree}\\core\\x.py", line 7 in waits\n'
            f'  File "{tree}\\tests\\unit\\test_a.py", line 42 in test_y\n'
            '  File "C:\\py\\pluggy\\_callers.py", line 1 in _multicall\n\n'
            "Thread 0x2 (most recent call first):\n"
            f'  File "{tree}\\core\\other.py", line 3 in idle\n')
    crashed = ('<testsuites><testsuite tests="1"><testcase classname="tests.unit.test_a" '
               'name="test_y" time="120.0"><error message="worker \'gw0\' crashed while '
               'running \'tests/unit/test_a.py::test_y\'">crashed</error></testcase>'
               '</testsuite></testsuites>')
    machine, said = _Machine(suite=crashed, stuck=("tests/unit/test_a.py::test_y", dump)), []

    _check(tree, machine, said).run()

    shown = said[said.index("  stuck, and stopped: tests/unit/test_a.py::test_y"):]
    assert shown[1:3] == ["    core/x.py:7 in waits", "    tests/unit/test_a.py:42 in test_y"]
    assert not any("threading" in line or "pluggy" in line or "idle" in line for line in shown)
    assert any(line.startswith("    every thread: ") for line in shown)


def test_the_full_suite_is_only_started_once_the_affected_tests_pass(tree):
    """A four-minute run is not the way to find what a ten-second one shows."""
    machine, said = _Machine(suite=_junit(failed=[("tests.unit.test_a", "test_x", "bad")])), []

    code = _check(tree, machine, said).run(full=True)

    assert code == 1
    assert not [a for a in machine.argv if a[3:4] == ["tests"]], "the whole suite was started"


def test_a_failure_is_printed_with_its_first_error_line_and_run_again_alone(tree):
    machine = _Machine(suite=_junit(failed=[("tests.unit.test_a", "test_x", "assert 1 == 2")]))
    said: list[str] = []

    code = _check(tree, machine, said).run()

    assert code == 1
    assert "    tests/unit/test_a.py::test_x" in said
    assert "      still wrong" in said, "the reason it gives when run by itself"
    alone = [a for a in machine.argv if "tests/unit/test_a.py::test_x" in a]
    assert alone and "-n" not in alone[0], "rerun alone means one process"
    assert any(line.startswith("  FAILS ALONE: 1") for line in said)


def test_a_failure_that_passes_alone_is_called_sensitive_to_load_not_real(tree):
    """The 17 errors in test_zero_downtime under load: a supervisor that took
    too long to start, with the code right. Said, not hidden, and not the
    change's: exit 2, not 1."""
    suite = _junit(failed=[("tests.unit.test_a", "test_x", "TimeoutError")])
    machine = _Machine(suite=suite, alone={"pass": {"tests/unit/test_a.py::test_x"}})
    said: list[str] = []

    code = _check(tree, machine, said).run()

    assert code == 2
    assert any(line.startswith("  PASSES ALONE") for line in said)
    assert not any("FAILS ALONE" in line for line in said)


def test_too_many_failures_are_not_rerun_as_if_they_were_the_load(tree):
    """Forty failures is a broken change or a broken tree, and rerunning them
    one at a time would take longer than the run."""
    many = [("tests.unit.test_a", f"test_{i}", "bad") for i in range(check.ALONE_AT_MOST + 1)]
    machine, said = _Machine(suite=_junit(failed=many)), []

    code = _check(tree, machine, said).run()

    assert code == 1
    assert not any("::" in a for argv in machine.argv for a in argv)
    assert any(line.startswith("  NOT RERUN") for line in said)
    assert any("more, all in" in line for line in said), "only the first few are printed"


def test_a_run_that_leaves_no_junit_file_shows_the_end_of_pytests_own_output(tree):
    """A crash or an internal error writes no file. The last lines are what
    there is to show -- what `| tail` used to be for, and then was not."""
    machine, said = _Machine(silent=True), []

    code = _check(tree, machine, said).run()

    assert code == 1
    assert "  INTERNALERROR the end" in said


def test_a_run_that_outlasts_its_time_is_reported_not_raised(tree):
    machine, said = _Machine(hang=True), []

    code = _check(tree, machine, said).run()

    assert code == 1
    assert any("DID NOT FINISH" in line for line in said)
    assert "  partial output" in said


def test_a_clean_run_says_so_in_one_line_and_lists_no_dots(tree):
    machine, said = _Machine(suite=_junit(passed=[("tests.unit.test_a", "test_x")])), []

    code = _check(tree, machine, said).run(full=True)

    assert code == 0 and said[-1] == "clean"
    assert not any(set(line) <= {".", " "} and line.strip() for line in said)


def test_every_run_keeps_a_group_on_one_worker_and_the_full_run_takes_the_distribution_asked_for(
        tree):
    """test_zero_downtime's module supervisor was started on every worker
    that drew one of its tests, so every run keeps an `xdist_group` on one
    worker. The longest module is 58 s and xdist deals tests out in
    advance, so `worksteal` may finish sooner; it is asked for, not assumed."""
    machine = _Machine(suite=_junit(passed=[("tests.unit.test_a", "test_x")]))

    _check(tree, machine, [], dist="worksteal").run(full=True)

    runs = [a for a in machine.argv if "pytest" in a]
    assert runs[0][runs[0].index("--dist") + 1] == "loadgroup"
    assert runs[-1].count("--dist") == 1
    assert runs[-1][runs[-1].index("--dist") + 1] == "worksteal"


def test_nothing_named_never_means_the_whole_suite(tree):
    """Given no target, pytest runs everything under testpaths. A selection
    that comes out empty must start nothing, not the four-minute run."""
    machine, said = _Machine(), []

    code = _check(tree, machine, said, selected=()).run()

    assert code == 0
    assert not any("pytest" in argv for argv in machine.argv)


def test_every_test_deselected_is_not_a_failure(tree):
    """pytest's exit 5. The affected files can all be process tests, which the
    quick run leaves out."""
    machine, said = _Machine(suite=_junit(), exit_code=5), []

    assert _check(tree, machine, said).run() == 0


def test_a_run_that_fails_with_no_failing_test_shows_what_pytest_said(tree):
    """An internal error after the junit file was written: a verdict file with
    nothing wrong in it is not "ok"."""
    machine, said = _Machine(suite=_junit(passed=[("tests.unit.test_a", "test_x")]),
                             exit_code=3), []

    code = _check(tree, machine, said).run()

    assert code == 1
    assert any("exited with no failing test (pytest exit 3)" in line for line in said)
    assert "  boom" in said


def test_the_junit_files_of_old_runs_are_cleared_and_recent_ones_kept(tree):
    """Their temp folder held 721,477 empty folders on 6 October, from a fence
    that never cleaned up after itself. This writes a file a run, so it does."""
    import os
    import time

    scratch = tree / "scratch"
    scratch.mkdir()
    old, recent = scratch / "affected-1.xml", scratch / "affected-2.xml"
    for path in (old, recent):
        path.write_text("<testsuites/>", encoding="utf-8")
    long_ago = time.time() - 3 * 86400
    os.utime(old, (long_ago, long_ago))

    _check(tree, _Machine(lint="E501 stop here"), []).run()

    assert not old.exists() and recent.exists()


RUST_OK = ("running 7 tests\ntest signin::tests::a ... ok\n\n"
           "test result: ok. 7 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n")


def _shell(tree, *, pages=True):
    """A tree with the shell's crate in it, and its built pages unless not."""
    (tree / "ui/src-tauri").mkdir(parents=True)
    (tree / "ui/src-tauri/Cargo.toml").write_text("", encoding="utf-8")
    if pages:
        (tree / "ui/dist").mkdir()
    return tree


def _cargo_runs(machine):
    return [argv for argv in machine.argv if argv[0] == "cargo"]


def test_a_change_to_the_shell_runs_its_rust_tests_built_as_the_app_is(tree):
    """The shell's tests ran nowhere until 19 October 2026: the sign-in's
    idle tests were run by hand, once. Built with the app's own features,
    they reuse its compiled crates instead of compiling tauri a second time."""
    machine, said = _Machine(cargo=(RUST_OK, 0)), []

    code = _check(_shell(tree), machine, said, changed=["ui/src-tauri/src/signin.rs"]).run()

    assert code == 0
    [argv] = _cargo_runs(machine)
    assert argv[:3] == ["cargo", "test", "--lib"]
    assert argv[argv.index("--features") + 1] == "tauri/custom-protocol"
    assert argv[argv.index("--manifest-path") + 1] == "ui/src-tauri/Cargo.toml"
    assert any(line.startswith("rust tests     ok: 7 tests") for line in said)


def test_a_change_outside_the_shell_runs_no_rust_tests_and_the_full_run_always_does(tree):
    """A Python change has no Rust to test; the final gate leaves nothing out."""
    machine = _Machine(cargo=(RUST_OK, 0))

    _check(_shell(tree), machine, [], changed=["agent/loop.py"]).run()
    assert _cargo_runs(machine) == []

    _check(tree, machine, [], changed=["agent/loop.py"]).run(full=True)
    assert len(_cargo_runs(machine)) == 1


def test_a_failing_rust_test_fails_the_check_by_name_before_the_full_suite_starts(tree):
    """A failure is named, and the seven-minute suite is not run behind it."""
    out = ("running 2 tests\ntest signin::tests::idle_ends_it ... FAILED\n"
           "test signin::tests::b ... ok\n\nfailures:\n\n"
           "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured\n")
    machine, said = _Machine(cargo=(out, 101)), []

    code = _check(_shell(tree), machine, said,
                  changed=["ui/src-tauri/src/signin.rs"]).run(full=True)

    assert code == 1
    assert "rust tests     failed: signin::tests::idle_ends_it" in said
    assert len([argv for argv in machine.argv if "pytest" in argv]) == 1  # the named tests only


def test_a_rust_build_that_breaks_fails_the_check_with_its_last_lines(tree):
    """No test result at all is a failure, with what the compiler said."""
    out = "error[E0425]: cannot find value `x` in this scope\nerror: could not compile `app`\n"
    machine, said = _Machine(cargo=(out, 101)), []

    code = _check(_shell(tree), machine, said, changed=["ui/src-tauri/src/lib.rs"]).run()

    assert code == 1
    assert "rust tests     did not run to the end (exit 101)" in said
    assert "  error: could not compile `app`" in said


def test_without_the_apps_pages_the_rust_tests_say_how_to_build_them_and_fail(tree):
    """The app's features embed its built pages; a skip would read as a pass."""
    machine, said = _Machine(cargo=(RUST_OK, 0)), []

    code = _check(_shell(tree, pages=False), machine, said,
                  changed=["ui/src-tauri/src/lib.rs"]).run()

    assert code == 1
    assert _cargo_runs(machine) == []
    assert any("npm run build" in line for line in said)
