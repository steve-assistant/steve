"""The coding pipeline, building a module, runs its checks through one
runner (core/sandbox/testrun.py, `for_drafts`): pytest in a container of
the run's own, ruff in the backend reading the files, and nothing else.

Until step 6 the pipeline ran `python -m pytest` in the backend, in the
repository it changed. Pointed at a module draft, that would run the
draft's conftest -- code from outside that no one has approved -- with the
user's files, network and secrets in reach. So the runner takes only the
arguments the pipeline's own calls use: a `-p` loads a plugin, a `-c` or a
`--rootdir` moves where pytest looks, and all of them are the draft's to
write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.coder import testruns
from core.sandbox import testrun

MANIFEST = 'id = "tides"\n'
TOOL = "def high():\n    return 2\n"
PASSES = ("from tides import tool\n\n\n"
          "def test_the_tide_is_high_twice_a_day():\n    assert tool.high() == 2\n")
FAILS = "def test_wrong():\n    assert 1 == 2\n"


def _draft(folder: Path, files: dict[str, str]) -> Path:
    for rel, text in {"manifest.toml": MANIFEST, "tool.py": TOOL, **files}.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8", newline="\n")
    return folder


def _never():
    raise AssertionError("ruff was looked for on a refused call")


@pytest.mark.parametrize("argv", [
    ["python", "-m", "pytest", "tests", "-p", "evil"],
    ["python", "-m", "pytest", "-c", "x.toml", "tests"],
    ["python", "-m", "pytest", "--rootdir=/", "tests"],
    ["python", "-m", "pytest", "tests/../.."],
    ["python", "-m", "pytest", "tests", "--ignore=../elsewhere"],
    ["python", "-m", "pytest", "tests", "-k", "x or __import__('os')"],
    ["python", "-m", "pip", "install", "anything"],
    ["python", "-c", "print(1)"],
    ["cmd", "/c", "dir"],
    ["python", "-m", "ruff", "format", "."],
    ["python", "-m", "ruff", "check", ".."],
    ["python", "-m", "ruff", "check", "--config", "ruff.toml", "."],
    ["python", "-m", "ruff", "check", "--extend-exclude", "../x", "."],
], ids=lambda argv: " ".join(argv[1:]))
def test_anything_but_the_pipeline_s_own_calls_is_refused(tmp_path, argv):
    """What the coder writes reaches these arguments; anything beyond the
    pipeline's own would let a draft choose what runs, or where."""
    ran = testrun.for_drafts(ruff=_never)(argv, _draft(tmp_path, {}))

    assert ran.output.startswith("refused:") and ran.returncode != 0


def test_a_draft_without_its_manifest_is_told_to_write_it_first(tmp_path):
    """Its tests import it by the id the manifest gives it; without one
    there is nothing to import it as."""
    ran = testrun.for_drafts(ruff=_never)(["python", "-m", "pytest", "tests"], tmp_path)

    assert ran.output.startswith("refused:") and "manifest.toml" in ran.output


def test_without_ruff_lint_says_so_rather_than_failing_the_build(tmp_path):
    """Steve as installed may not carry ruff, and the coder cannot fix
    that: the judge reads that nothing was linted, and decides."""
    ran = testrun.for_drafts(ruff=lambda: None)(["python", "-m", "ruff", "check", "."],
                                                _draft(tmp_path, {}))

    assert ran.returncode == 0 and "lint unavailable" in ran.output


@pytest.mark.processes
def test_lint_ignores_the_draft_s_own_configuration_and_leaves_nothing_behind(tmp_path):
    """A draft's ruff.toml could turn every rule off, and a cache written
    into the draft would be installed with the module."""
    draft = _draft(tmp_path, {"ruff.toml": '[lint]\nignore = ["F"]\n',
                              "unused.py": "import os\n"})

    ran = testrun.for_drafts()(["python", "-m", "ruff", "check", ".", "--output-format",
                                "concise"], draft)

    assert ran.returncode == 1 and "F401" in ran.output, ran.output
    assert not (draft / ".ruff_cache").exists()


@pytest.mark.processes
@pytest.mark.appcontainer
def test_a_draft_s_conftest_runs_in_the_sandbox_not_the_backend(tmp_path):
    """The conftest is the first of the draft's code pytest runs. Here it
    writes where the backend may and the sandbox may not: run anywhere
    else, it would have left its mark."""
    beside = tmp_path / "beside"
    beside.mkdir()
    mark = beside / "reached.txt"
    conftest = f"from pathlib import Path\n\nPath({str(mark)!r}).write_text('here')\n"
    draft = _draft(tmp_path / "draft", {"tests/conftest.py": conftest,
                                        "tests/test_tool.py": PASSES})

    ran = testrun.for_drafts()(["python", "-m", "pytest", "tests", "-q"], draft)

    assert not mark.exists()
    assert ran.returncode != 0 and "PermissionError" in ran.output, ran.output


@pytest.mark.processes
@pytest.mark.appcontainer
def test_the_pipeline_s_suite_runs_and_names_its_failures_as_the_pipeline_reads_them(
        tmp_path):
    """The judge is shown which tests failed by their ids; run from another
    folder, or under another root, the ids would name files that are not
    in the draft."""
    draft = _draft(tmp_path / "draft", {"tests/test_tool.py": PASSES,
                                        "tests/test_wrong.py": FAILS,
                                        "tests/test_left_independent.py": FAILS})

    ran = testrun.for_drafts()(
        testruns.suite_argv("python", ["tests/test_left_independent.py"]), draft)

    assert ran.returncode == 1 and "1 failed, 1 passed" in ran.output, ran.output
    assert testruns.failing(ran.output) == ["tests/test_wrong.py::test_wrong"]
