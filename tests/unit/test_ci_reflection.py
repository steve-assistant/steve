"""Optional redaction tests must run somewhere instead of silently skipping.

The base workflow deliberately omits a large NLP model. That must not mean
the privacy boundary is tested only in its absent-dependency state. The
dedicated job loads the actual model and rejects skipped results.
"""

from __future__ import annotations

import ast
import shlex
import subprocess
import sys

import pytest
import yaml

from core import paths

TARGETS = {
    "tests/unit/test_reflect.py", "tests/unit/test_redact.py", "tests/unit/test_modbuild.py",
    "tests/unit/test_modtools.py", "tests/gate/test_redaction_present.py",
}


def _job():
    workflow = paths.SOURCE / ".github/workflows/ci.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    assert "reflection" in jobs, "the optional reflection/redaction tests need a CI job"
    return jobs["reflection"]


def _commands():
    return [step["run"] for step in _job()["steps"] if "run" in step]


def test_reflection_has_its_own_locked_environment_on_every_ci_trigger():
    """Installing the extra in this job keeps the default install small."""
    job = _job()
    assert "if" not in job
    assert job["runs-on"] == "windows-latest"
    [sync] = [shlex.split(run) for run in _commands() if run.startswith("uv sync")]
    assert "--locked" in sync
    extras = {sync[i + 1] for i, word in enumerate(sync) if word == "--extra"}
    assert {"dev", "accounts", "reflect"} <= extras


def test_the_optional_models_are_loaded_before_the_redaction_tests():
    """A present package with a missing model is not usable redaction."""
    commands = _commands()
    [loaded] = [i for i, run in enumerate(commands) if "en_core_web_lg.load()" in run]
    argv = shlex.split(commands[loaded])
    tree = ast.parse(argv[argv.index("-c") + 1])
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    assert {"presidio_analyzer", "presidio_anonymizer", "en_core_web_lg"} <= imported
    assert loaded < next(i for i, run in enumerate(commands) if "pytest" in run)


def test_the_optional_job_exercises_the_real_redactor_and_its_callers():
    """Only the absent-extra gate case is intentionally excluded here;
    the ordinary base job still exercises that fail-closed path."""
    [argv] = [shlex.split(run) for run in _commands() if "pytest" in run]
    assert set(argv) >= TARGETS
    assert "--no-sync" in argv
    assert argv[argv.index("-k") + 1] == "not test_without_presidio_redaction_refuses"
    assert "--junitxml=reflection-results.xml" in argv
    assert "-n" not in argv  # each worker would load its own large model


@pytest.mark.processes
@pytest.mark.parametrize(("cases", "ok"), [
    ('<testcase name="redaction"/>', True),
    ('<testcase name="redaction"><skipped/></testcase>', False),
    ("", False),
])
def test_the_ci_result_check_rejects_skipped_or_empty_coverage(tmp_path, cases, ok):
    """Execute the actual workflow check against synthetic test reports;
    reading that it mentions skips would not prove it rejects them."""
    [argv] = [shlex.split(run) for run in _commands()
              if "ElementTree" in run]
    report = f'<testsuites><testsuite>{cases}</testsuite></testsuites>'
    (tmp_path / "reflection-results.xml").write_text(report, encoding="utf-8", newline="\n")
    code = argv[argv.index("-c") + 1]
    done = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True,
                          timeout=15)
    assert (done.returncode == 0) is ok
