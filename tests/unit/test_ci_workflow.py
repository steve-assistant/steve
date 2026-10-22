"""CI's workflow (.github/workflows/ci.yml), read rather than run.

A job or flag that drifts from the suite it drives is found only when CI
runs it: a push's jobs within half an hour, the nightly and installer jobs
at night or when someone thinks to dispatch them. These read the workflow
as GitHub would and hold it to what the suite and the maintainer's rules
need.
"""

from __future__ import annotations

import json
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
# A push's suite, split three ways so each job builds only what its tests
# need.
PUSHED = ("check", "gate", "app")
# Every job that runs the suite or part of it; focus runs the tests named.
SUITE = PUSHED + ("nightly", "focus")
# The jobs whose tests start the app, and those whose tests run modules.
APP_BUILT = ("app", "nightly", "focus")
RUNTIME_BUILT = ("gate", "nightly", "focus")


def _runs(job: str) -> list[str]:
    return [step.get("run", "") for step in JOBS[job]["steps"]]


def _pytest(job: str) -> list[str]:
    [line] = [run for run in _runs(job) if "pytest" in run]
    return shlex.split(line)


def _test_files() -> set[str]:
    return {path.relative_to(paths.SOURCE).as_posix()
            for path in (paths.SOURCE / "tests").rglob("test_*.py")}


def _selected(job: str) -> set[str]:
    """The test files a job's pytest line collects: those under its paths,
    less those under its `--ignore`s."""
    argv = _pytest(job)
    roots = [arg.rstrip("/") for arg in argv if arg.startswith("tests")]
    ignored = [arg.removeprefix("--ignore=").rstrip("/") for arg in argv
               if arg.startswith("--ignore=")]

    def under(name: str, prefix: str) -> bool:
        return name == prefix or name.startswith(prefix + "/")

    return {name for name in _test_files()
            if any(under(name, root) for root in roots)
            and not any(under(name, gone) for gone in ignored)}


def _marked(group: str) -> set[str]:
    """The test files whose tests share an xdist group: `ui` for the
    screens, `app` for the walks, each driving the one real window."""
    marked = re.compile(rf'^pytestmark = .*xdist_group\("{group}"\)', re.MULTILINE)
    return {name for name in _test_files()
            if marked.search((paths.SOURCE / name).read_text(encoding="utf-8"))}


def test_every_pytest_run_in_ci_deals_tests_out_by_group_on_the_runners_four_cores():
    """Without loadgroup, xdist starts a module's supervisor on every worker
    that runs one of its tests, and the screens' and the supervisors'
    groups are no longer kept apart. Four workers, where the laptop's check
    takes three to leave room for what else runs there: the runner has four
    cores and nothing else on them."""
    for job in SUITE:
        argv = _pytest(job)
        assert argv[argv.index("--dist") + 1] == check.DIST, job
        assert argv[argv.index("-n") + 1] == "4", job


def test_every_test_file_runs_in_one_job_of_a_push_or_in_the_nightly_job():
    """A file left out of every job would never run in CI; one in two would
    run twice, and a push's suite was split into jobs to save its time."""
    jobs = PUSHED + ("nightly",)
    for i, one in enumerate(jobs):
        for other in jobs[i + 1:]:
            assert not _selected(one) & _selected(other), (one, other)
    assert set().union(*map(_selected, jobs)) == _test_files()


def test_the_nightly_job_runs_the_end_to_end_tier_and_the_walks_and_nothing_else():
    """The tiers too slow for every push: each end-to-end test starts the
    whole backend, each walk drives the app's window through a day."""
    e2e = {name for name in _test_files() if name.startswith(E2E + "/")}
    assert _marked("app")
    assert _selected("nightly") == e2e | _marked("app")


def test_the_app_is_built_before_the_tests_that_start_it():
    """Unbuilt, the app's tests skip, and CI passes without having opened
    the window once."""
    for job in APP_BUILT:
        runs = _runs(job)
        built = next(i for i, run in enumerate(runs) if "cargo build" in run)
        assert "npm run build" in runs[built], job
        assert "--features tauri/custom-protocol" in runs[built], job
        assert built < next(i for i, run in enumerate(runs) if "pytest" in run), job
    starts_the_app = _marked("ui") | _marked("app")
    assert starts_the_app
    for job in set(PUSHED + ("nightly",)) - set(APP_BUILT):
        assert not _selected(job) & starts_the_app, job


def test_the_module_runtime_is_built_before_the_tests_that_run_modules():
    """Unbuilt, every test that runs a module in its container failed, and
    the extension's end-to-end test waited for an answer that never came:
    CI's first runs, 22 October 2026. The suite had run only on the
    maintainer's laptop, where the runtime had been built long before."""
    for job in RUNTIME_BUILT:
        runs = _runs(job)
        built = next(i for i, run in enumerate(runs) if "python -m core.sandbox build" in run)
        assert built < next(i for i, run in enumerate(runs) if "pytest" in run), job
    runs_modules = {name for name in _test_files()
                    if name.startswith(("tests/gate/", E2E + "/"))}
    for job in set(PUSHED + ("nightly",)) - set(RUNTIME_BUILT):
        assert not _selected(job) & runs_modules, job


def test_the_webview2_runtime_is_installed_before_the_tests_that_start_the_app():
    """The runner's Windows Server image has Edge and no WebView2 Runtime,
    which Windows 11 carries: each test that started the app waited for a
    webview that never came (CI's first runs, 22 October 2026). Microsoft's
    installer runs only once Windows has found it signed by Microsoft."""
    for job in APP_BUILT:
        runs = _runs(job)
        [installed] = [i for i, run in enumerate(runs) if "MicrosoftEdgeWebview2Setup" in run]
        assert "Get-AuthenticodeSignature" in runs[installed], job
        assert runs[installed].index("throw") < runs[installed].index("Start-Process"), job
        assert installed < next(i for i, run in enumerate(runs) if "pytest" in run), job


def test_the_apps_windows_are_built_with_the_rigs_arguments_as_their_own():
    """On the runner everything runs elevated, and an elevated WebView2
    takes no arguments from outside the app: every test that started the
    app waited 60 s for a debugging port never asked for, given by the
    rig's variable and then by machine policy (22 October 2026). Each job
    that builds the app sets TAURI_CONFIG from the rig before the build, so
    the two can't drift; the installer's build never has it."""
    for job in APP_BUILT:
        runs = _runs(job)
        [own] = [i for i, run in enumerate(runs) if "TAURI_CONFIG" in run]
        assert "from tests.unit.ui.rig import build_config" in runs[own], job
        assert runs[own].rstrip().endswith('>> "$GITHUB_ENV"'), job
        assert own < next(i for i, run in enumerate(runs) if "cargo build" in run), job
    assert "TAURI_CONFIG" not in yaml.safe_dump(JOBS["installer"])
    assert "TAURI_CONFIG" not in yaml.safe_dump(DOC.get("env", {}))


def test_the_test_builds_windows_are_the_apps_own_with_the_rigs_arguments():
    """CI's build of the app differs from the developer's in its windows'
    browser arguments alone: the rig's, and the autoplay policy wry gives a
    window with none of its own. A window left out would be a window the
    tests never see, or one that fails to start beside the others."""
    from tests.unit.ui import rig

    conf = json.loads((rig.UI / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    built = json.loads(rig.build_config())
    assert list(built) == ["app"] and list(built["app"]) == ["windows"]
    windows = built["app"]["windows"]
    assert [{k: v for k, v in w.items() if k != "additionalBrowserArgs"}
            for w in windows] == conf["app"]["windows"]
    for window in windows:
        assert window["additionalBrowserArgs"].startswith(rig.WEBVIEW_ARGS + " "), window["label"]
        assert "--remote-debugging-port=0" in window["additionalBrowserArgs"]


def test_each_push_runs_the_shells_rust_tests_as_the_check_does():
    """The shell's tests ran nowhere until 19 October 2026. CI runs the
    check's own command, after the build whose crates it reuses."""
    runs = _runs("app")
    [rust] = [i for i, run in enumerate(runs) if "cargo test" in run]
    argv = shlex.split(runs[rust])
    assert argv[:len(check.RUST_TESTS)] == list(check.RUST_TESTS)
    assert argv[argv.index("-j") + 1] == "3"
    assert next(i for i, run in enumerate(runs) if "cargo build" in run) < rust


def test_ci_scans_for_secrets_with_the_checks_own_command():
    """The check runs CI's secrets step on a change's files (devtools/check.py
    `SECRETS_HOOK`): a different version or baseline here would pass what
    CI then fails."""
    [scan] = [run for run in _runs("check") if "detect-secrets-hook" in run]
    assert " ".join(check.SECRETS_HOOK) in scan


def test_every_npm_install_in_ci_skips_the_audit_call():
    """npm's call to the audit endpoint it is retiring took 5 to 7 minutes
    of each install of the app's 33 packages on the runner, more than the
    app's whole Rust build from the cache (22 October 2026)."""
    installs = [command for job in JOBS for run in _runs(job)
                for command in re.split(r"&&|\n", run) if "npm ci" in command]
    assert installs
    for command in installs:
        argv = shlex.split(command)
        assert argv[:2] == ["npm", "ci"] and {"--no-audit", "--no-fund"} <= set(argv), command


def test_pushes_run_the_suite_nights_run_the_rest_and_the_rest_runs_by_hand():
    """A dispatch with tests named runs those alone (the focus job), so a
    fix's loop on CI takes minutes, not a whole run's half hour. Only
    main's pushes run the suite, so a fix tried on a branch of its own
    neither runs every job nor cancels main's run."""
    on = DOC.get("on", DOC.get(True))  # PyYAML reads the bare `on` as true
    assert set(on) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    assert on["push"] == {"branches": ["main"]}
    pushes = "github.event_name == 'push' || github.event_name == 'pull_request'"
    for job in PUSHED + ("ui",):
        assert JOBS[job]["if"] == pushes, job
    unnamed = "!inputs.tests"
    assert JOBS["nightly"]["if"] == ("(github.event_name == 'schedule'"
                                     " || github.event_name == 'workflow_dispatch')"
                                     f" && {unnamed}")
    assert JOBS["installer"]["if"] == f"github.event_name == 'workflow_dispatch' && {unnamed}"
    assert JOBS["reflection"]["if"] == f"${{{{ {unnamed} }}}}"
    assert JOBS["focus"]["if"] == "github.event_name == 'workflow_dispatch' && inputs.tests != ''"


def test_no_step_has_an_expression_pasted_into_its_script():
    """GitHub pastes an expression's value into the script before the shell
    reads it, so the focus job's test names, typed by whoever dispatches,
    would run as commands. They reach the script through its environment."""
    for name, job in JOBS.items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), (name, step.get("name"))
    [named] = [step for step in JOBS["focus"]["steps"] if "pytest" in step.get("run", "")]
    assert named["env"] == {"TESTS": "${{ inputs.tests }}"}


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


def test_the_rust_cache_is_shared_by_the_jobs_that_build_alike_and_kept_on_failure():
    """By default each job kept its own cache, saved only when the job
    passed. CI had never passed, so every run built every crate: 7 of the
    check job's 28 minutes (22 October 2026)."""
    for job in APP_BUILT + ("installer",):
        [cache] = [step for step in JOBS[job]["steps"]
                   if step.get("uses", "").startswith("Swatinem/rust-cache@")]
        assert cache["with"]["cache-on-failure"] is True, job
        expected = "release" if job == "installer" else "app"
        assert cache["with"]["shared-key"] == expected, job
