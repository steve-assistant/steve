"""CI's workflow (.github/workflows/ci.yml), read rather than run.

The repository has no remote until step 12, so nothing has ever run the
workflow; a job or flag that drifted from the suite it drives would be
found only at the first push, or, for the nightly and installer jobs, the
first night or the first time someone thought to try. These read the
workflow as GitHub would and hold it to what the suite and the
maintainer's rules need.
"""

from __future__ import annotations

import re
import shlex

import yaml

from core import paths
from devtools import check, package

WORKFLOW = paths.SOURCE / ".github" / "workflows" / "ci.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)
JOBS = DOC["jobs"]
# The tier that starts the whole backend per test, run each night, and not
# on every push.
E2E = "tests/e2e"


def _runs(job: str) -> list[str]:
    return [step.get("run", "") for step in JOBS[job]["steps"]]


def _pytest(job: str) -> list[str]:
    [line] = [run for run in _runs(job) if "pytest" in run]
    return shlex.split(line)


def _app_walks() -> set[str]:
    """The files of the app's walks: those whose tests share the `app`
    xdist group, as each drives the one real window."""
    marked = re.compile(r'^pytestmark = .*xdist_group\("app"\)', re.MULTILINE)
    found = set()
    for path in (paths.SOURCE / "tests").rglob("test_*.py"):
        if marked.search(path.read_text(encoding="utf-8")):
            found.add(path.relative_to(paths.SOURCE).as_posix())
    return found


def test_every_pytest_run_in_ci_deals_tests_out_by_group_as_the_check_does():
    """Without loadgroup, xdist starts a module's supervisor on every worker
    that runs one of its tests, and the screens' and the supervisors'
    groups are no longer kept apart."""
    for job in ("check", "nightly"):
        argv = _pytest(job)
        assert argv[argv.index("--dist") + 1] == check.DIST, job
        assert argv[argv.index("-n") + 1] == "3", job


def test_the_app_is_built_before_the_tests_that_start_it():
    """Unbuilt, the app's tests skip, and CI passes without having opened
    the window once."""
    for job in ("check", "nightly"):
        runs = _runs(job)
        built = next(i for i, run in enumerate(runs) if "cargo build" in run)
        assert "npm run build" in runs[built], job
        assert "--features tauri/custom-protocol" in runs[built], job
        assert built < next(i for i, run in enumerate(runs) if "pytest" in run), job


def test_the_module_runtime_is_built_before_the_tier_that_runs_modules():
    """Unbuilt, the worker says modules cannot run here, and the extension's
    end-to-end test failed waiting for an answer: CI's first nightly run, 22
    October 2026. The tier ran only on the maintainer's laptop, where the
    runtime had been built long before."""
    runs = _runs("nightly")
    built = next(i for i, run in enumerate(runs) if "python -m core.sandbox build" in run)
    assert built < next(i for i, run in enumerate(runs) if "pytest" in run)


def test_each_push_runs_the_shells_rust_tests_as_the_check_does():
    """The shell's tests ran nowhere until 19 October 2026. CI runs the
    check's own command, after the build whose crates it reuses."""
    runs = _runs("check")
    [rust] = [i for i, run in enumerate(runs) if "cargo test" in run]
    argv = shlex.split(runs[rust])
    assert argv[:len(check.RUST_TESTS)] == list(check.RUST_TESTS)
    assert argv[argv.index("-j") + 1] == "3"
    assert next(i for i, run in enumerate(runs) if "cargo build" in run) < rust


def test_the_nightly_job_runs_exactly_what_each_push_leaves_out():
    """A walk or end-to-end file left out of both would never run in CI; one
    in both would double a push's time for nothing."""
    pushed = _pytest("check")
    left_out = {arg.removeprefix("--ignore=") for arg in pushed if arg.startswith("--ignore=")}
    nightly = {arg for arg in _pytest("nightly")[1:] if arg.startswith("tests")}

    assert left_out == nightly == {E2E} | _app_walks()
    assert "tests/" in pushed


def test_pushes_run_the_suite_nights_run_the_rest_and_the_installer_only_by_hand():
    on = DOC.get("on", DOC.get(True))  # PyYAML reads the bare `on` as true
    assert set(on) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    pushes = "github.event_name == 'push' || github.event_name == 'pull_request'"
    assert JOBS["check"]["if"] == JOBS["ui"]["if"] == pushes
    assert JOBS["nightly"]["if"] == ("github.event_name == 'schedule'"
                                     " || github.event_name == 'workflow_dispatch'")
    assert JOBS["installer"]["if"] == "github.event_name == 'workflow_dispatch'"


def test_the_installer_job_signs_with_a_key_made_for_its_run_and_the_rig_stays_up():
    """The release key is the maintainer's alone and never a CI secret. The
    rig shuts its machine down at the end, as Windows Sandbox needs; on the
    runner that would end the job before its results were read."""
    steps = JOBS["installer"]["steps"]
    made = next(i for i, s in enumerate(steps) if "tauri signer generate" in s.get("run", ""))
    built = next(i for i, s in enumerate(steps) if "sandbox_check build" in s.get("run", ""))
    rig = next(i for i, s in enumerate(steps) if "sandbox_inside.ps1" in s.get("run", ""))
    report = next(i for i, s in enumerate(steps) if "sandbox_check report" in s.get("run", ""))
    assert made < built < rig < report

    key = re.search(r"-w (\S+)", steps[made]["run"]).group(1)
    assert f"--key {key}" in steps[built]["run"]
    assert f"--pubkey {key[:-1]}.pub\"" in steps[built]["run"]
    assert steps[rig]["env"] == {"STEVE_RIG_STAYS_UP": "1"}


def test_ci_reads_the_repository_and_nothing_more_and_never_spends():
    """CI holds no key: what spends on a real model (the live smoke, the
    yardstick's real run) is the maintainer's to run, by hand."""
    assert DOC["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in JOBS.values())
    # `secrets.` as an expression names it, not as in `.secrets.baseline`.
    assert not re.search(r"(?<![.\w])secrets\.", TEXT)
    for never in ("live_smoke", "autonomy_eval", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        assert never not in TEXT, never


def test_ci_and_every_checkout_run_the_python_the_installer_carries():
    """`requires-python` is only a floor, so uv took the newest Python on
    the runner. CI's first run, on 22 October 2026, got 3.15: the reflect
    extra's spaCy pulls in `blis`, which had no wheel for it, and building
    it from source failed. The jobs that did install were testing a Python
    no install runs. `.python-version`, which every `uv sync` reads, names
    the installer's interpreter's minor version, and no job overrides it."""
    pinned = (paths.SOURCE / ".python-version").read_text(encoding="utf-8").strip()
    shipped = package.INTERPRETER.version.split("+")[0]
    assert pinned == ".".join(shipped.split(".")[:2]), (pinned, shipped)
    for override in ("python-version", "UV_PYTHON", "--python"):
        assert override not in TEXT, override


def test_each_action_is_pinned_to_a_commit_and_checkout_keeps_no_token():
    """A tag such as `v4` is whatever its owner points it at next: a
    taken-over action account moves it, and the next run executes their
    code with the repository checked out. A commit can't be moved. And
    checkout otherwise leaves the run's token in `.git/config` for every
    later step to read; no step here pushes, so none needs it."""
    steps = [step for job in JOBS.values() for step in job["steps"]]
    actions = [step for step in steps if "uses" in step]
    assert actions
    for step in actions:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    for step in actions:
        if step["uses"].startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False
