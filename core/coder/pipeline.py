"""One task, through the coder, the independent test builder and the judge.

    snapshot -> coder -> checks (one fix round) -> test builder -> judge
          -> (one revision round) -> keep the good independent tests
          -> the checks once more, changing nothing -> approved

The maintainer, 27 September 2026: a coding model that may write its own tests, "a
test builder that's independent of the coder and only sees the
documentation and codebase created by the coder so it can write unbiased and
fair testing, and a judge with access to both that determines final
approval". What this returns is a decision about a folder; what becomes of
it is the caller's.

Until step 6 the folder was a git clone of Steve's own tree: each stage was
a commit, the base was the commit it began at, and a frozen gate decided
what merged. That was self-upgrade v1, and it went (06-modules.md, section
8). The base is now a snapshot of the folder (core/coder/snapshot.py), and
nothing here runs git.

Rules the judge cannot talk its way past, because they are code:

- **The checks outrank the verdict.** An approval with a failing suite, a
  lint error, or an independent test the judge itself ruled a code bug is
  not an approval.
- **Test infrastructure is flagged.** A changed conftest, sitecustomize,
  `.pth` or pytest config is code that runs around every test and can make
  them all pass; the judge is told, and the outcome carries the flag.
- **Money is counted across the roles.** Each role gets the smaller of its
  own budget and what the attempt has left.
- **The last run of the checks changes nothing.** Code under test that
  rewrites the folder it is judged in is not a pass.

Everything that executes the change's code goes through `runner`, the
sandbox's.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.coder import coder, judge, snapshot, tester, testruns
from core.coder.roles import CoderReport
from core.coder.tools import Tools
from core.coder.workspace import Workspace, coder_may_write, is_readable

log = logging.getLogger("coder")

# Anything that runs around or beneath a test rather than being one. The first
# two lines were all of it until 8 October 2026, when the audit found that
# `tests/__init__.py` -- imported inside every pytest, the gate's too -- was
# neither flagged nor frozen, and one line in it made a failing suite exit 0.
# So everything under tests/ that is not a `test_*.py` module is flagged:
# a package marker, a helper, a fixture file, a plugin. Case-insensitive,
# because Windows loads `CONFTEST.PY` as readily as `conftest.py`.
INFRASTRUCTURE = re.compile(
    r"(^|/)(conftest\.py|sitecustomize\.py|usercustomize\.py|pytest\.ini|tox\.ini|setup\.cfg)$"
    r"|\.pth$"
    r"|^tests/(?!(?:[^/]+/)*test_[^/]*\.py$)",
    re.IGNORECASE)
SUITE_TIMEOUT_S = testruns.SUITE_TIMEOUT_S


@dataclass
class Stage:
    name: str
    cost_usd: float
    seconds: float
    stopped: str | None
    resumed: int = 0  # provider failures the role picked up after
    tools: str = ""  # what its tool calls did (core/coder/tools.summary)


@dataclass
class Outcome:
    status: str  # approved | rejected | failed
    reasons: str
    cost_usd: float = 0.0
    stages: list[Stage] = field(default_factory=list)
    verdict: dict | None = None
    flags: list[str] = field(default_factory=list)
    kept_tests: list[str] = field(default_factory=list)
    # The tests that decided a rejection or a failure, and every run of the
    # checks. Kept with the outcome: the reasons for #82-#88 survived only
    # in rotated logs (8 October 2026).
    failed_tests: list[str] = field(default_factory=list)
    test_runs: list[dict] = field(default_factory=list)
    # The coder's word that the request cannot be a module, and why
    # (`CoderReport.cannot`): the build stops there, for the caller to
    # write up as a slot proposal or a problem report.
    cannot: dict | None = None

    @property
    def approved(self) -> bool:
        return self.status == "approved"


@dataclass
class Checked:
    """One run of lint and the suite."""

    ok: bool
    checks: dict[str, str]  # what the judge reads
    lint_ok: bool = True
    caused: list[str] = field(default_factory=list)  # the tests that failed
    detail: str = ""  # their tracebacks and assertions
    refused: str = ""  # what admission would refuse, read without running anything

    def record(self, stage: str) -> dict:
        """For the outcome (`Outcome.test_runs`)."""
        return {"stage": stage, "passed": self.ok, "lint": self.lint_ok,
                "caused": testruns.listed(self.caused)}


def _checks(runner, python: str, root: Path, *,
            ignore: list[str] | tuple[str, ...] = (), screen=None,
            fix: bool = False) -> Checked:
    """Lint and the whole suite, and `screen`, what admission would refuse
    in the folder from reading it (`core/lifecycle.screened`). A manifest
    admission refuses passed lint and the suite, and the judge, and the
    build was lost after its last round. `ignore`: the test builder's
    files, which are judged on their own -- one the judge ruled a bad test
    must not fail the coder's revision forever.

    Left out of lint as well as the suite. In the second live run a
    scratch file the test builder left failed lint during the revision; the
    coder was refused each edit to it (the builder's files are read-only to
    it), and the revision was rejected for a file it could not touch. The
    builder's files that are kept are linted on their own (`_tidy`).

    `fix`: ruff's safe fixes first, such as imports put in order or one
    unused dropped. Ruff's defaults sort imports, and models order them
    by hand badly: in the self-upgrade trial (19 October 2026) five
    admitted modules carried `# noqa: I001`, the coder silencing what ruff
    would have sorted, and in its rerun a build was lost with two unsorted
    import blocks still failing after its fix round. Never in the final
    run, which must change nothing the judge has seen."""
    exclude = ["--extend-exclude", ",".join(ignore)] if ignore else []
    if fix:
        runner([python, "-m", "ruff", "check", ".", "--fix", "--quiet", *exclude],
               root, timeout_s=120)
    lint = runner([python, "-m", "ruff", "check", ".", "--output-format", "concise",
                   *exclude], root, timeout_s=120)
    suite = runner(testruns.suite_argv(python, ignore), root, timeout_s=SUITE_TIMEOUT_S)
    result = Checked(ok=False, checks={}, lint_ok=lint.returncode == 0 and not lint.timed_out)
    suite_ok = suite.returncode == 0 and not suite.timed_out
    if not suite_ok:
        nodes = [] if suite.timed_out else testruns.failing(suite.output)
        if nodes:
            result.caused = nodes
            result.detail = testruns.failures(suite.output)
        else:
            # Nothing named -- a timeout, a crash -- so the whole suite.
            what = ("timed out" if suite.timed_out
                    else f"exit {suite.returncode}, no test named")
            result.caused = [f"(the whole suite: {what})"]
            result.detail = suite.output.strip()[-4000:]
    result.refused = (screen(root) or "") if screen else ""
    result.ok = result.lint_ok and suite_ok and not result.refused
    result.checks = {"lint": f"exit {lint.returncode}\n{lint.output}",
                     "full suite": f"exit {suite.returncode}\n"
                                   f"{testruns.condensed(suite.output)}"}
    if screen:
        result.checks["admission"] = result.refused or "nothing it would refuse"
    return result


def _failures(result: Checked) -> str:
    """What failed, for the rejection: the tests that failed and their
    assertions, and lint's last lines. "The revision broke lint or the
    suite" alone left the second live run's rejection unexplained."""
    parts = []
    if not result.lint_ok:
        parts.append("lint: ..." + result.checks["lint"][-1200:])
    if result.caused:
        parts.append("tests: " + ", ".join(result.caused) + "\n" + result.detail[-1500:])
    if result.refused:
        parts.append("admission would refuse it: " + result.refused)
    return "\n".join(parts) or "(no output)"


def _feedback(result: Checked) -> str:
    """The fix round's brief: each failing test with its assertion, and
    lint. It used to be every check's raw output under "The checks failed",
    and five coders (#64-#70) answered a failure by loosening a safety test
    that was not theirs."""
    parts = ["The checks failed. Below is what failed: fix it in your change, run those "
             "tests again, and finish. A test that was there before your change states "
             "something the user relies on: make the code satisfy it, and do not weaken it, "
             "unless the task is to change that test."]
    if not result.lint_ok:
        parts.append("## Lint\n\n" + result.checks["lint"][-4000:])
    if result.caused:
        parts.append("## Tests that failed\n\n" + "\n".join(f"- {n}" for n in result.caused)
                     + "\n\n" + result.detail)
    if result.refused:
        parts.append("## What Steve would refuse when it admits the module\n\n" + result.refused)
    return "\n\n".join(parts)


def _independent(runner, python: str, root: Path, files: list[str]) -> tuple[bool, str]:
    if not files:
        return True, "the test builder wrote no tests"
    ran = runner([python, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider",
                  "--tb=short", testruns.REPORT], root, timeout_s=SUITE_TIMEOUT_S)
    return ran.returncode == 0, f"exit {ran.returncode}\n{ran.output}"


def _tidy(runner, python: str, root: Path, files: list[str]) -> list[str]:
    """The independent tests that will be kept, lint-clean: ruff's safe fixes
    applied, and any file still failing dropped. The first live run's only
    failure was here -- a correct change, approved, lost at the gate to an
    unused import in the test builder's file, which nothing had linted."""
    if not files:
        return []
    runner([python, "-m", "ruff", "check", "--fix", "--quiet", *files], root, timeout_s=120)
    clean = []
    for f in files:
        ran = runner([python, "-m", "ruff", "check", "--quiet", f], root, timeout_s=120)
        if ran.returncode == 0:
            clean.append(f)
        else:
            (root / f).unlink(missing_ok=True)
    return clean


def _named(node: str, names: set[str]) -> bool:
    """A ruling names a test by node id or by its file."""
    return node in names or node.split("::")[0] in names


def _flag(base: snapshot.Snapshot, flags: list[str]) -> None:
    """Test infrastructure, over everything since `base`. Run on the final
    change too: a revision, or the kept independent tests, come after the
    first look (audit of 27 September 2026)."""
    for p in base.changed():
        flag = f"changes test infrastructure: {p}"
        if INFRASTRUCTURE.search(p) and flag not in flags:
            flags.append(flag)


class _Spend:
    def __init__(self, cap: float) -> None:
        self.cap, self.used = cap, 0.0

    def role(self, role, *, reserve: float = 0.0):
        """What a role may spend: its own budget, or what is left once the
        roles still to come have their share. The coder's fix round used to
        spend a second coder budget and leave the judge nothing."""
        return dataclasses.replace(role, budget_usd=max(0.0, min(
            role.budget_usd, self.cap - self.used - reserve)))

    def add(self, outcome: Outcome, name: str, run) -> None:
        self.used += run.cost_usd
        outcome.cost_usd = self.used
        outcome.stages.append(Stage(name, run.cost_usd, run.seconds, run.stopped, run.resumed,
                                    run.tools))

    @property
    def spent(self) -> bool:
        return self.used >= self.cap - 0.005


async def run(task: str, root: Path, *, config, runner, python: str,
              build=None, cap_usd: float | None = None, reaper=None,
              library: dict[str, Path] | None = None, screen=None,
              runs_code=None) -> Outcome:
    """`config` is policy.coder, `runner` the sandbox's (a fake in tests),
    `reaper` what ends anything an earlier run left going, `library` what
    the roles may read beside the folder (`Workspace.library`), `screen`
    what admission would refuse in the folder, run with every check, and
    `runs_code` whether the folder holds code for the test builder to test
    (`core/lifecycle.runs_code`); without it, every folder does."""
    library = library or {}
    spend = _Spend(min(config.attempt_budget_usd, cap_usd or config.attempt_budget_usd))
    outcome = Outcome("failed", "")

    def done(status: str, reasons: str) -> Outcome:
        outcome.status, outcome.reasons = status, reasons
        return outcome

    # Before any model is paid for: a vanished folder used to reach the
    # coder, which spent a real request finding out.
    if not root.is_dir():
        return done("failed", f"no folder at {root}")
    if reaper:
        reaper()  # nothing an earlier attempt left running sees this one
    base = snapshot.take(root, is_readable)

    # What the roles still to come are kept: the coder may not spend the
    # test builder's or the judge's share.
    after_coder = config.tester.budget_usd + config.judge.budget_usd

    # -- the coder, and one round to fix what the checks found ----------------
    tools = Tools(Workspace(root, coder_may_write, library=library), runner, python,
                  base=base)
    first = await coder.code(task, tools, spend.role(config.coder, reserve=after_coder),
                             build=build)
    spend.add(outcome, "coder", first)
    report = first.output
    if not first.finished:
        if not base.changed():
            return done("failed", f"the coder did not finish ({first.stopped})")
        # The first live run's third task: the change on disk was right -- the
        # hidden tests passed -- and was thrown away because the coder ran out
        # of time before saying it was done. What it left goes through
        # everything a finished change does; the flag tells the judge it was
        # cut off.
        outcome.flags.append(f"the coder stopped before reporting ({first.stopped})")
        report = CoderReport(summary=f"(no report: the coder stopped: {first.stopped})")
    if report.cannot:
        outcome.cannot = {"kind": report.cannot, "why": report.why.strip()[:2000]}
        return done("failed", f"it cannot be a module ({report.cannot}): {report.why.strip()}")

    async def checked(tools, before: str, stage: str, reserve: float, ignore=()):
        """Lint and the suite, and one round for the coder to fix what
        failed. The revision used to get no such round: in the second live
        run a correct change was rejected over lint it could have fixed."""
        result = _checks(runner, python, root, ignore=ignore, screen=screen, fix=True)
        outcome.test_runs.append(result.record(before))
        fix = None
        if not result.ok and not spend.spent:
            fix = await coder.code(task, tools, spend.role(config.coder, reserve=reserve),
                                   build=build, feedback=_feedback(result))
            spend.add(outcome, stage, fix)
            result = _checks(runner, python, root, ignore=ignore, screen=screen, fix=True)
            outcome.test_runs.append(result.record(stage))
        return result, fix

    def still_failing(result: Checked, what: str) -> Outcome:
        outcome.failed_tests = testruns.listed(result.caused)
        return done("rejected", what + _failures(result))

    result, fix = await checked(tools, "coder", "coder fix", after_coder)
    checks = result.checks
    report = (fix.output if fix else None) or report
    if not result.ok:
        return still_failing(result, "the checks still fail after a fix round:\n")
    if not base.changed():
        return done("rejected", "the coder changed nothing")
    _flag(base, outcome.flags)

    # -- the test builder, blind to the coder's tests -------------------------
    # Not for a module of words alone: it runs nothing for a test to call,
    # and admission neither reviews nor tests it. In the self-upgrade trial's
    # rerun (20 October 2026) the builder's tests of a meeting-notes skill's
    # wording failed, the judge approved the skill without ruling on them,
    # and the build was rejected.
    written: list[str] = []
    if runs_code is not None and not runs_code(root):
        tester_report = "(no test builder: a module of words alone runs no code to test)"
    else:
        t_tools = Tools(tester.workspace(root, base, library), runner, python, base=base,
                        own_tests_only=True)
        built = await tester.build_tests(
            task, t_tools, spend.role(config.tester, reserve=config.judge.budget_usd),
            build=build)
        spend.add(outcome, "test builder", built)
        written = sorted(f for f in t_tools.ws.created if (root / f).exists())
        tester_report = built.output.model_dump_json(indent=1) if built.output else (
            f"(the test builder did not finish: {built.stopped})")
        if not written:
            # The design rests on a second, independent reading; without one
            # the change can still be approved, and the judge is told.
            outcome.flags.append("no independent tests: the test builder wrote none")

    # -- the judge, with at most one revision --------------------------------
    verdict = None
    for round_ in (1, 2):
        indep_ok, indep = _independent(runner, python, root, written)
        message = judge.brief(
            task=task, coder_report=report.model_dump_json(indent=1),
            diff=base.diff(leave_out=set(written)), tester_report=tester_report,
            tester_files={f: (root / f).read_text(encoding="utf-8") for f in written},
            checks={**checks, "independent tests": indep}, flags=outcome.flags,
        )
        ruled = await judge.judge(message, spend.role(config.judge), build=build)
        spend.add(outcome, f"judge {round_}", ruled)
        if not ruled.finished:
            return done("failed", f"the judge did not finish ({ruled.stopped})")
        verdict = ruled.output
        outcome.verdict = verdict.model_dump()
        if verdict.decision != "revise" or round_ == 2 or spend.spent:
            break
        # One revision. The independent tests stay, and the coder may read
        # them but not change them.
        tools = Tools(Workspace(root, coder_may_write, read_only=set(written),
                                library=library), runner, python,
                      base=base)
        feedback = verdict.reasons + "\n\n" + "\n".join(
            f"- {r.test}: {r.ruling} -- {r.why}" for r in verdict.rulings)
        if not indep_ok:
            feedback += "\n\nThe independent tests:\n" + indep[-4000:]
        revised = await coder.code(
            task, tools, spend.role(config.coder, reserve=config.judge.budget_usd),
            build=build, feedback=feedback)
        spend.add(outcome, "coder revision", revised)
        report = revised.output or report
        result, fix = await checked(tools, "coder revision", "revision fix",
                                    config.judge.budget_usd, ignore=written)
        checks = result.checks
        report = (fix.output if fix else None) or report
        if not result.ok:
            return still_failing(result, "the revision broke lint, the suite or what "
                                 "admission reads, and a fix round did not repair it:\n")

    if verdict is None or verdict.decision != "approve":
        return done("rejected", (verdict.reasons if verdict else "no verdict")
                    + ("" if verdict is None or verdict.decision != "revise"
                       else "\n(still needed revision after the one round)"))

    # -- the checks outrank the verdict --------------------------------------
    indep_ok, indep = _independent(runner, python, root, written)
    code_bugs = {r.test for r in verdict.rulings if r.ruling == "code bug"}
    bad_tests = {r.test for r in verdict.rulings if r.ruling == "bad test"}
    failing = testruns.failing(indep)
    if any(_named(f, code_bugs) for f in failing) or (code_bugs and not indep_ok):
        return done("rejected", "approved, but an independent test the judge ruled a code "
                    "bug still fails")
    # Every failing independent test needs a ruling: an approval that simply
    # ignored them would have had them deleted as unkept, unseen.
    unruled = [f for f in failing if not _named(f, bad_tests)]
    if unruled or (not indep_ok and not failing):
        return done("rejected", "approved with failing independent tests it did not rule "
                    "on: " + (", ".join(unruled) or indep[-300:]))

    # -- keep the good independent tests --------------------------------------
    keep = [f for f in verdict.keep_tests if f in written]
    for f in written:
        if f not in keep:
            (root / f).unlink(missing_ok=True)
    tidy = _tidy(runner, python, root, keep)
    if dropped := [f for f in keep if f not in tidy]:
        outcome.flags.append("independent tests dropped for failing lint: " + ", ".join(dropped))
    keep = tidy
    if keep:
        kept_ok, kept_out = _independent(runner, python, root, keep)
        if not kept_ok:
            bad = {f.split("::")[0] for f in testruns.failing(kept_out)}
            for f in bad:
                (root / f).unlink(missing_ok=True)
            keep = [f for f in keep if f not in bad]
    outcome.kept_tests = keep
    _flag(base, outcome.flags)

    # -- the checks once more, over the whole change, changing nothing --------
    if reaper:
        reaper()  # nothing left running rewrites the folder while it is judged
    before = base.now()
    final = _checks(runner, python, root, screen=screen)
    outcome.test_runs.append(final.record("final"))
    if not final.ok:
        return still_failing(final, "the final run of the checks failed:\n")
    if moved := before.changed():
        return done("rejected", "the final run of the checks changed files: "
                    + ", ".join(moved[:10]))
    return done("approved", verdict.reasons)
