"""Which changed files the pipeline flags as test infrastructure.

A flagged change is held for the user: the judge is told, and the outcome carries
the flag. Until 8 October 2026 only a
conftest, a sitecustomize, a `.pth` and pytest's own config were flagged; the
audit found that `tests/__init__.py` -- imported inside every pytest, the
gate's too -- was neither flagged nor frozen, and one line in it made a failing
suite exit 0. So everything under tests/ that is not a test module is flagged.
"""

from __future__ import annotations

import pytest

from core.coder import pipeline, snapshot
from core.coder.workspace import is_readable


@pytest.mark.parametrize("path", [
    "tests/__init__.py",
    "tests/unit/__init__.py",
    "tests/unit/conftest.py",
    "tests/unit/helpers.py",
    "tests/unit/fixtures/attention/nws_alerts.json",
    "tests/unit/sub/data.json",
    "tests/unit/test_x.py.bak",
    "tests/unit/testing.py",
    "tests/unit/_test_x.py",
    "tests/unit/test_dir/helper.py",
    # Windows loads a conftest by any capitalisation, and a folder by any.
    "TESTS/UNIT/CONFTEST.PY",
    "Tests/Unit/helpers.py",
    "conftest.py",
    "pytest.ini",
    "tools/sitecustomize.py",
    "evil.pth",
])
def test_a_file_that_runs_around_the_tests_rather_than_being_one_is_flagged(path):
    assert pipeline.INFRASTRUCTURE.search(path), f"{path} would change the tests unflagged"


@pytest.mark.parametrize("path", [
    "tests/unit/test_x.py",
    "tests/unit/test_a_sentence_about_the_guarantee.py",
    "tests/unit/sub/test_y.py",
    "tests/gate/test_judge.py",
    "tests/test_z.py",
    "agent/helpers.py",
    "agent/prompts/system.md",
    "docs/testing.md",
    "tools/test_thing.py",
    "tests_of_nothing/helpers.py",
    "contests/__init__.py",
])
def test_a_test_module_and_what_is_not_under_tests_are_not_flagged(path):
    """A flag holds a change for the user, so a new test file -- the commonest
    change the night makes -- must not be one."""
    assert not pipeline.INFRASTRUCTURE.search(path), f"{path} is flagged for nothing"


def test_a_changed_helper_under_tests_is_flagged_on_the_outcome(folder):
    """The flags are looked for over the whole change, after the first look
    and the kept independent tests too (audit of 27 September 2026)."""
    root = folder({"tests/unit/test_a.py": "def test_a():\n    pass\n"})
    base = snapshot.take(root, is_readable)
    (root / "tests" / "unit" / "helpers.py").write_text("X = 1\n", encoding="utf-8")
    (root / "tests" / "unit" / "test_b.py").write_text("def test_b():\n    pass\n",
                                                       encoding="utf-8")
    flags: list[str] = []

    pipeline._flag(base, flags)
    pipeline._flag(base, flags)

    assert flags == ["changes test infrastructure: tests/unit/helpers.py"], "once, not twice"
