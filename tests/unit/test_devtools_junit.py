"""A failing test is reported by its id and its first `E` line, not its dots.

28 September to 8 October 2026: 135 full runs, 34 of them failing, usually
piped through `tail`, which kept the summary line and dropped the lines that
said why. Four of them ended in the same 17 errors in test_zero_downtime and
nothing on screen said what the error was, so each cost another run. The
loop's reports (devtools/check.py) are read from pytest's junit file, and
this is the reading.
"""

from __future__ import annotations

import pytest

from devtools import junit

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="6">
<testcase classname="tests.unit.test_a" name="test_passes" time="0.5"/>
<testcase classname="tests.unit.test_a" name="test_fails[x.y-1]" time="1.25">
  <failure message="assert 1 == 2">def test_fails(x):
&gt;       assert 1 == 2
E       assert 1 == 2
E        +  where 1 = f()</failure></testcase>
<testcase classname="tests.unit.test_a.TestGroup" name="test_in_a_class" time="0.1">
  <failure message="AssertionError: nope">no E lines in this one</failure></testcase>
<testcase classname="tests.gate.test_b" name="test_setup_dies" time="0.0">
  <error message="failed on setup with &quot;RuntimeError: no supervisor&quot;">Traceback...
E   RuntimeError: no supervisor</error></testcase>
<testcase classname="tests.gate.test_b" name="test_skipped" time="0.0">
  <skipped message="why"/></testcase>
<testcase classname="" name="tests.unit.test_c" time="0.0">
  <error message="collection failure">ImportError while importing</error></testcase>
</testsuite></testsuites>
"""


@pytest.fixture
def tree(tmp_path):
    for rel in ("tests/unit/test_a.py", "tests/gate/test_b.py", "tests/unit/test_c.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")
    (tmp_path / "run.xml").write_text(JUNIT, encoding="utf-8")
    return tmp_path


def test_every_outcome_carries_an_id_pytest_would_accept(tree):
    """So a failure can be rerun by pasting the line. A class sits between
    the file and the test, and a parametrised id can contain dots that are
    not part of any path."""
    outcomes = {o.node: o for o in junit.read(tree / "run.xml", tree)}

    assert "tests/unit/test_a.py::test_passes" in outcomes
    assert "tests/unit/test_a.py::test_fails[x.y-1]" in outcomes
    assert "tests/unit/test_a.py::TestGroup::test_in_a_class" in outcomes
    assert "tests/gate/test_b.py::test_setup_dies" in outcomes


def test_a_grouped_tests_id_loses_the_group_that_pytest_would_not_find(tree):
    """xdist's `loadgroup` names a grouped test `test_x@group`, and the check
    reruns a failure by its id and names the slowest for devtools.slow: with
    the suffix, pytest finds nothing. An `@` inside a parameter stays."""
    grouped = ('<testsuites><testsuite tests="2">'
               '<testcase classname="tests.unit.test_a" name="test_x@supervisor" time="1"/>'
               '<testcase classname="tests.unit.test_a" name="test_p[a@b]@supervisor" time="1"/>'
               '</testsuite></testsuites>')
    (tree / "grouped.xml").write_text(grouped, encoding="utf-8")

    nodes = [o.node for o in junit.read(tree / "grouped.xml", tree)]

    assert nodes == ["tests/unit/test_a.py::test_x", "tests/unit/test_a.py::test_p[a@b]"]


def test_a_collection_error_is_the_file_not_a_test(tree):
    """pytest writes a module that would not import as a case with no class
    and the dotted path as its name; every test in the file is then missing,
    which devtools.prove reads as "cannot be set up"."""
    broken = [o for o in junit.read(tree / "run.xml", tree) if o.status == "error"
              and "::" not in o.node]

    assert [o.node for o in broken] == ["tests/unit/test_c.py"]


def test_a_failure_says_its_first_error_line_and_an_error_says_it_too(tree):
    got = {o.node: o for o in junit.read(tree / "run.xml", tree)}

    assert got["tests/unit/test_a.py::test_fails[x.y-1]"].reason == "assert 1 == 2"
    assert got["tests/unit/test_a.py::test_fails[x.y-1]"].status == "failed"
    assert got["tests/gate/test_b.py::test_setup_dies"].reason == "RuntimeError: no supervisor"
    assert got["tests/gate/test_b.py::test_setup_dies"].status == "error"


def test_a_failure_with_no_error_line_falls_back_to_its_message(tree):
    """A failure raised some other way has no `E` line to show."""
    got = {o.node: o for o in junit.read(tree / "run.xml", tree)}

    assert got["tests/unit/test_a.py::TestGroup::test_in_a_class"].reason == "AssertionError: nope"


def test_only_failed_and_errored_tests_are_bad_and_the_slowest_come_first(tree):
    outcomes = junit.read(tree / "run.xml", tree)

    assert {o.status for o in junit.bad(outcomes)} == {"failed", "error"}
    assert len(junit.bad(outcomes)) == 4
    assert [o.seconds for o in junit.slowest(outcomes, 2)] == [1.25, 0.5]


def test_a_file_pytest_died_in_the_middle_of_is_something_the_caller_can_catch(tmp_path):
    """The run that crashes leaves half a file, or none. The caller shows the
    end of pytest's own output instead, and must be able to say so."""
    (tmp_path / "half.xml").write_text("<testsuites><testsuite><testcase ", encoding="utf-8")

    for name in ("half.xml", "never-written.xml"):
        with pytest.raises(junit.UNREADABLE):
            junit.read(tmp_path / name, tmp_path)
