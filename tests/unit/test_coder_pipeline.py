"""The pipeline, end to end, with every role scripted and nothing billed.

The scenario is the one the design exists for: a coder whose own test
passes because it shares the coder's mistake. Only the test builder, blind
to the coder's tests, catches it; the judge sends it back; the coder fixes
the code -- and cannot "fix" the independent test instead.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from pydantic_ai.messages import (
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import modbuild
from core.coder import pipeline
from core.coder.models import CoderModel
from core.coder.tools import Ran
from core.policy import CoderConfig, RoleConfig
from core.sandbox import testrun

FREE = CoderModel("free", "free", 0.0, 0.0, 0.0)
INDEPENDENT = "tests/unit/test_calc_independent.py"
GOOD, BUG = "    return a + b\n", "    return a - b\n"


@pytest.fixture
def repo(tmp_path, monkeypatch, folder):
    files = {
        "agent/calc.py": '"""Arithmetic for the briefing."""\n\n\ndef add(a, b):\n    return 0\n',
        "tests/unit/test_placeholder.py": "def test_nothing():\n    assert True\n",
    }
    for role in ("coder", "tester", "judge"):
        monkeypatch.setattr(f"core.coder.{role}.CATALOG", {"free": FREE})
    return folder(files, dest=tmp_path)


class _Runner:
    """pytest and ruff, decided by whether agent/calc.py is right."""

    def __init__(self, root, *, on_suite=None):
        self.root, self.on_suite, self.argv = root, on_suite, []

    def __call__(self, argv, cwd, timeout_s=600):
        self.argv.append(argv)
        if "ruff" in argv:
            return Ran(0, "All checks passed!", 0.1)
        right = GOOD in (self.root / "agent/calc.py").read_text(encoding="utf-8")
        targets = [a for a in argv[3:] if not a.startswith("-") and a.startswith("tests")]
        if targets == ["tests"] and self.on_suite:
            self.on_suite()
        independent = self.root / INDEPENDENT
        # The independent test fails if the code is wrong, or if it is a
        # wrong test (it expects 2 + 3 to be 6).
        fails = independent.exists() and (
            not right or "== 6" in independent.read_text(encoding="utf-8"))
        # The whole suite collects it too, unless told to leave it out.
        collected = INDEPENDENT in targets or (
            targets == ["tests"] and f"--ignore={INDEPENDENT}" not in argv)
        if fails and collected:
            return Ran(1, f"FAILED {INDEPENDENT}::test_adding_two_and_three_gives_five - "
                          "assert -1 == 5\n1 failed", 0.1)
        return Ran(0, "all passed", 0.1)


def _role(messages) -> str:
    system = " ".join(p.content for p in messages[0].parts if isinstance(p, SystemPromptPart))
    return ("judge" if system.startswith("You are the judge") else
            "tester" if system.startswith("You are the independent") else "coder")


def _world(scripts):
    """One FunctionModel for all three roles, each playing its own script:
    a list of turns, each a list of steps, a step a (tool, args) pair or
    the final report (a dict)."""
    turn = {"coder": 0, "tester": 0, "judge": 0}
    step = {}

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        role = _role(messages)
        key = (role, turn[role])
        if len(messages) == 1:  # a fresh run of this role
            key = (role, turn[role])
            step[key] = 0
            turn[role] += 1
        key = (role, turn[role] - 1)
        plays = scripts[role][min(key[1], len(scripts[role]) - 1)]
        at = step[key]
        step[key] += 1
        play = plays[min(at, len(plays) - 1)]
        if isinstance(play, dict):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, play)])
        tool, args = play
        return ModelResponse(parts=[ToolCallPart(tool, args)])

    return lambda model: (FunctionModel(respond), None)


def _coder_writes(line, own_test=True):
    steps = [("edit_file", {"path": "agent/calc.py", "old": "    return 0\n", "new": line})]
    if own_test:
        # The coder's own test shares its mistake: add(2, 0) is 2 either way.
        steps.append(("write_file", {"path": "tests/unit/test_calc.py", "content":
                                     "def test_adding_zero_keeps_the_number():\n"
                                     "    from agent.calc import add\n"
                                     "    assert add(2, 0) == 2\n"}))
    return [*steps, {"summary": "add adds", "tests_added": ["tests/unit/test_calc.py"]}]


TESTER = [[
    ("write_file", {"path": INDEPENDENT, "content":
                    "def test_adding_two_and_three_gives_five():\n"
                    "    from agent.calc import add\n    assert add(2, 3) == 5\n"}),
    ("run_tests", {"target": INDEPENDENT}),
    {"summary": "one sharp test", "tests": [INDEPENDENT], "checks": ["2 + 3 is 5"]},
]]


CONFIG = CoderConfig(
    night_budget_usd=2.0, attempt_budget_usd=0.5,
    coder=RoleConfig("free", 0.25, 30, 60), tester=RoleConfig("free", 0.1, 30, 60),
    judge=RoleConfig("free", 0.15, 3, 60),
)


def _verdict(decision, *, rulings=(), keep=(INDEPENDENT,), reasons="fine"):
    return {"decision": decision, "reasons": reasons, "rulings": list(rulings),
            "keep_tests": list(keep)}


BUG_RULING = {"test": f"{INDEPENDENT}::test_adding_two_and_three_gives_five",
              "ruling": "code bug", "why": "add subtracts"}


async def _run(repo, scripts, runner=None, cap=None):
    return await pipeline.run(
        "Make add add its arguments", repo, config=CONFIG,
        runner=runner or _Runner(repo), python="python", build=_world(scripts), cap_usd=cap)


def _ran(outcome) -> list[str]:
    """Which runs of lint and the suite there were, by stage."""
    return [r["stage"] for r in outcome.test_runs]


async def test_a_correct_change_is_approved_with_the_independent_tests_kept(repo):
    outcome = await _run(repo, {"coder": [_coder_writes(GOOD)], "tester": TESTER,
                                "judge": [[_verdict("approve")]]})

    assert outcome.approved, outcome.reasons
    assert outcome.kept_tests == [INDEPENDENT] and (repo / INDEPENDENT).exists()
    assert _ran(outcome) == ["coder", "final"], "the checks ran on the whole change, last"
    assert [s.name for s in outcome.stages] == ["coder", "test builder", "judge 1"]
    # What each role's calls did goes into the ledger with the stage.
    assert outcome.stages[0].tools.startswith("2 calls") and outcome.stages[2].tools == ""


async def test_a_bug_only_the_independent_tests_see_is_sent_back_and_fixed(repo):
    """The coder's own test passes against its mistake; the independent one
    does not. The revision fixes the code -- its attempt to rewrite the
    independent test instead is refused."""
    revision = [
        ("write_file", {"path": INDEPENDENT, "content": "def test_x():\n    assert True\n"}),
        ("edit_file", {"path": "agent/calc.py", "old": BUG, "new": GOOD}),
        {"summary": "fixed: add adds"},
    ]
    outcome = await _run(repo, {
        "coder": [_coder_writes(BUG), revision],
        "tester": TESTER,
        "judge": [[_verdict("revise", rulings=[BUG_RULING], reasons="add subtracts")],
                  [_verdict("approve")]],
    })

    assert outcome.approved, outcome.reasons
    assert [s.name for s in outcome.stages] == [
        "coder", "test builder", "judge 1", "coder revision", "judge 2"]
    independent = (repo / INDEPENDENT).read_text(encoding="utf-8")
    assert "add(2, 3) == 5" in independent, "the coder rewrote the independent test"
    assert GOOD in (repo / "agent/calc.py").read_text(encoding="utf-8")


# What the user said, beside the request Steve wrote: the case the
# maintainer met on 20 October, a food tracker that took grams alone, knew
# about ten foods and could not split a dish, so it was of no use.
WANTED = ("I want to log what I eat: any common food, in grams, cups or pieces, and a "
          "dish I cooked split into its ingredients.")
NARROW = "It takes grams only, and cannot split a dish into its ingredients."


def _watched(scripts, seen: list):
    """`_world`, noting each role's system prompt and first message."""
    build = _world(scripts)

    def make(model):
        inner, extra = build(model)

        async def respond(messages, info: AgentInfo) -> ModelResponse:
            if len(messages) == 1:
                parts = messages[0].parts
                seen.append((_role(messages),
                             " ".join(p.content for p in parts if isinstance(p, SystemPromptPart)),
                             " ".join(str(p.content) for p in parts
                                      if isinstance(p, UserPromptPart))))
            return await inner.function(messages, info)

        return FunctionModel(respond), extra

    return make


async def test_the_judge_reads_what_the_user_wanted_and_sends_back_a_safe_tool_too_narrow_for_it(
        repo):
    """A safe tool that passed its tests was approved though it could not do
    what the user wanted. The judge now reads the user's own words beside
    the request Steve wrote, and is asked whether the module does what they
    wanted, used as they will use it; its send-back names what is missing,
    and the coder's next round reads it. The verdicts are a fake model's:
    whether a real judge finds the gap is the build eval's to say."""
    seen: list = []
    task = modbuild.task({"request": "Track my macronutrients", "module": "",
                          "user_said": WANTED})
    outcome = await pipeline.run(
        task, repo, config=CONFIG, runner=_Runner(repo), python="python",
        build=_watched({"coder": [_coder_writes(GOOD), [{"summary": "takes cups too"}]],
                        "tester": TESTER,
                        "judge": [[_verdict("revise", reasons=NARROW)],
                                  [_verdict("approve")]]}, seen))

    assert outcome.approved, outcome.reasons
    judged = [(system, first) for role, system, first in seen if role == "judge"]
    assert len(judged) == 2
    system, first = judged[0]
    assert WANTED in first and "Track my macronutrients" in first
    assert "Does the module do what the user wanted" in system
    revision = [first for role, _, first in seen if role == "coder"][1]
    assert NARROW in revision


async def test_the_judge_cannot_approve_what_it_ruled_a_code_bug(repo):
    outcome = await _run(repo, {"coder": [_coder_writes(BUG)], "tester": TESTER,
                                "judge": [[_verdict("approve", rulings=[BUG_RULING])]]})

    assert outcome.status == "rejected" and "code bug" in outcome.reasons
    assert "final" not in _ran(outcome)


async def test_a_test_the_judge_rules_wrong_neither_blocks_the_revision_nor_is_kept(repo):
    """The test builder can be wrong too. A test ruled "bad test" must not
    fail the coder's revision -- the suite leaves it out -- and must not
    become a permanent test."""
    wrong = [[
        ("write_file", {"path": INDEPENDENT, "content":
                        "def test_adding_two_and_three_gives_five():\n"
                        "    from agent.calc import add\n    assert add(2, 3) == 6\n"}),
        {"summary": "one test", "tests": [INDEPENDENT]},
    ]]
    bad = {**BUG_RULING, "ruling": "bad test", "why": "2 + 3 is 5, not 6"}
    touch_docs = [("write_file", {"path": "docs/calc.md", "content": "add adds.\n"}),
                  {"summary": "documented"}]
    outcome = await _run(repo, {
        "coder": [_coder_writes(GOOD), touch_docs],
        "tester": wrong,
        "judge": [[_verdict("revise", rulings=[bad], keep=(), reasons="document it")],
                  [_verdict("approve", rulings=[bad], keep=())]],
    })

    assert outcome.approved, outcome.reasons
    assert outcome.kept_tests == [] and not (repo / INDEPENDENT).exists()


async def test_still_needing_revision_after_one_round_is_a_rejection(repo):
    outcome = await _run(repo, {
        "coder": [_coder_writes(BUG), [{"summary": "tried"}]],
        "tester": TESTER,
        "judge": [[_verdict("revise", rulings=[BUG_RULING])], [_verdict("revise")]],
    })

    assert outcome.status == "rejected"


async def test_a_changed_conftest_is_flagged_to_the_judge(repo):
    seen = []
    coder_steps = _coder_writes(GOOD)
    coder_steps.insert(0, ("write_file", {"path": "tests/unit/conftest.py",
                                          "content": "import pytest\n"}))

    async def judge_reads(messages, info):
        seen.append(json.dumps([str(p) for p in messages[-1].parts]))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                                                 _verdict("approve"))])

    world = _world({"coder": [coder_steps], "tester": TESTER, "judge": [[_verdict("approve")]]})

    def build(model):
        fm, settings = world(model)
        inner = fm.function

        async def respond(messages, info):
            if _role(messages) == "judge":
                return await judge_reads(messages, info)
            return await inner(messages, info)
        return FunctionModel(respond), settings

    outcome = await pipeline.run("Make add add its arguments", repo, config=CONFIG,
                                 runner=_Runner(repo), python="python", build=build)

    assert outcome.flags == ["changes test infrastructure: tests/unit/conftest.py"]
    assert "Flagged for your attention" in seen[0]


def test_each_role_gets_no_more_than_the_attempt_has_left():
    spend = pipeline._Spend(0.30)
    spend.used = 0.25

    assert spend.role(CONFIG.tester).budget_usd == pytest.approx(0.05)
    assert spend.role(CONFIG.coder).budget_usd == pytest.approx(0.05)


async def test_a_missing_folder_is_refused_before_any_model_is_asked(tmp_path):
    """A stubbed or vanished worktree reached the coder once, and the coder
    spent a real request finding out."""
    def never(model):
        raise AssertionError("a model was built for a folder that does not exist")

    outcome = await pipeline.run("Make add add", tmp_path / "gone", config=CONFIG,
                                 runner=_Runner(tmp_path), python="python", build=never)

    assert outcome.status == "failed" and "no folder" in outcome.reasons
    assert outcome.stages == []


# ---------------------------------------------------------------------------
# The audit of 27 September 2026
# ---------------------------------------------------------------------------


async def _run_with(repo, scripts, **kwargs):
    return await pipeline.run(
        "Make add add its arguments", repo, config=CONFIG,
        runner=kwargs.pop("runner", None) or _Runner(repo), python="python",
        build=_world(scripts), **kwargs)


async def test_a_conftest_added_in_the_revision_is_flagged_too(repo):
    """Test infrastructure was looked for once, after the coder's first run; a
    revision could add a conftest that makes every test pass, unflagged."""
    revision = [
        ("write_file", {"path": "tests/unit/conftest.py", "content": "import pytest\n"}),
        ("edit_file", {"path": "agent/calc.py", "old": BUG, "new": GOOD}),
        {"summary": "fixed"},
    ]
    outcome = await _run_with(repo, {
        "coder": [_coder_writes(BUG), revision], "tester": TESTER,
        "judge": [[_verdict("revise", rulings=[BUG_RULING])], [_verdict("approve")]],
    })

    assert "changes test infrastructure: tests/unit/conftest.py" in outcome.flags


async def test_an_approval_that_ignores_failing_independent_tests_is_not_one(repo):
    """Unkept tests are deleted: an approval that never ruled on the failing
    ones would have had them quietly removed."""
    outcome = await _run_with(repo, {"coder": [_coder_writes(BUG)], "tester": TESTER,
                                     "judge": [[_verdict("approve", rulings=[], keep=())]]})

    assert outcome.status == "rejected" and "did not rule on" in outcome.reasons
    assert "final" not in _ran(outcome)


async def test_with_no_independent_tests_an_approval_is_flagged(repo):
    no_tests = [[{"summary": "nothing to add", "tests": []}]]
    outcome = await _run_with(repo, {"coder": [_coder_writes(GOOD)], "tester": no_tests,
                                     "judge": [[_verdict("approve", keep=())]]})

    assert outcome.approved
    assert any(f.startswith("no independent tests") for f in outcome.flags)


async def test_a_module_of_words_alone_is_judged_without_a_test_builder(repo):
    """It runs nothing for a test to call, and admission neither reviews
    nor tests it. In the self-upgrade trial's rerun (20 October 2026) the
    builder's tests of a skill's wording failed a sound skill."""
    outcome = await _run_with(repo, {"coder": [_coder_writes(GOOD)], "tester": TESTER,
                                     "judge": [[_verdict("approve", keep=())]]},
                              runs_code=lambda root: False)

    assert outcome.approved, outcome.reasons
    assert [s.name for s in outcome.stages] == ["coder", "judge 1"]
    assert not (repo / INDEPENDENT).exists() and outcome.kept_tests == []
    assert not any(f.startswith("no independent tests") for f in outcome.flags)


async def test_a_final_run_of_the_checks_that_changes_the_folder_is_not_a_pass(repo):
    """The checks run the code under test in the folder they judge; code
    that rewrote a file while they ran could pass on what it wrote (the
    gate's own finding, 8 October 2026)."""
    calc = repo / "agent/calc.py"

    def rewrite_once_kept():
        if (repo / INDEPENDENT).exists():
            calc.write_text("def add(a, b):\n    return 42\n", encoding="utf-8")

    outcome = await _run_with(repo, {"coder": [_coder_writes(GOOD)], "tester": TESTER,
                                     "judge": [[_verdict("approve")]]},
                              runner=_Runner(repo, on_suite=rewrite_once_kept))

    assert outcome.status == "rejected"
    assert "the final run of the checks changed files: agent/calc.py" in outcome.reasons


async def test_anything_left_running_is_ended_before_the_coder_and_before_the_final_checks(
        repo):
    runner = _Runner(repo)
    reaped = []
    outcome = await _run_with(repo, {"coder": [_coder_writes(GOOD)], "tester": TESTER,
                                     "judge": [[_verdict("approve")]]},
                              runner=runner, reaper=lambda: reaped.append(len(runner.argv)))

    assert outcome.approved
    # Lint and the suite: the final checks are the last two runs.
    assert reaped == [0, len(runner.argv) - 2]


class _RealRuff(_Runner):
    """The fake pytest above, but the real ruff, on the real files."""

    def __call__(self, argv, cwd, timeout_s=600):
        if "ruff" not in argv:
            return super().__call__(argv, cwd, timeout_s)
        self.argv.append(argv)
        ran = subprocess.run([sys.executable, *argv[1:]], cwd=cwd, capture_output=True,
                             text=True, check=False)
        return Ran(ran.returncode, ran.stdout + ran.stderr, 0.1)


def _tester_writes(content):
    return [[("write_file", {"path": INDEPENDENT, "content": content}),
             {"summary": "one test", "tests": [INDEPENDENT], "checks": ["2 + 3 is 5"]}]]


async def test_a_kept_independent_test_is_lint_fixed_before_it_is_kept(repo):
    """The first live run's store-false task: the change was right and
    approved, and was lost at the gate's lint to an unused import in the
    test builder's file."""
    unused_import = ("import os\n\n\ndef test_adding_two_and_three_gives_five():\n"
                     "    from agent.calc import add\n    assert add(2, 3) == 5\n")
    outcome = await _run_with(repo, {"coder": [_coder_writes(GOOD)],
                                     "tester": _tester_writes(unused_import),
                                     "judge": [[_verdict("approve")]]},
                              runner=_RealRuff(repo))

    assert outcome.approved and outcome.kept_tests == [INDEPENDENT]
    kept = (repo / INDEPENDENT).read_text(encoding="utf-8")
    assert "import os" not in kept and "assert add(2, 3) == 5" in kept


async def test_a_kept_independent_test_lint_cannot_fix_is_dropped_not_kept(repo):
    unfixable = ("def test_adding_two_and_three_gives_five():\n"
                 "    from agent.calc import add\n    unused = 1\n    assert add(2, 3) == 5\n")
    outcome = await _run_with(repo, {"coder": [_coder_writes(GOOD)],
                                     "tester": _tester_writes(unfixable),
                                     "judge": [[_verdict("approve")]]},
                              runner=_RealRuff(repo))

    assert outcome.approved and outcome.kept_tests == []
    assert not (repo / INDEPENDENT).exists()
    assert any(f.startswith("independent tests dropped for failing lint") for f in outcome.flags)


READS_FOREVER = ("read_file", {"path": "agent/calc.py"})


async def test_a_coder_cut_off_with_its_change_on_disk_is_still_checked_and_judged(repo):
    """The first live run's third task: the coder's change was right -- the
    hidden tests passed -- and was thrown away because it ran out of time
    before it said so."""
    cut_off = [("edit_file", {"path": "agent/calc.py", "old": "    return 0\n", "new": GOOD}),
               READS_FOREVER]
    seen = []
    calc = repo / "agent/calc.py"
    outcome = await _run_with(repo, {"coder": [cut_off], "tester": TESTER,
                                     "judge": [[_verdict("approve")]]},
                              runner=_Runner(repo, on_suite=lambda: seen.append(
                                  GOOD in calc.read_text(encoding="utf-8"))))

    assert outcome.approved, outcome.reasons
    assert outcome.stages[0].stopped.startswith("turns")
    assert any(f.startswith("the coder stopped before reporting (turns") for f in outcome.flags)
    assert seen and all(seen), "the suite ran on what it left"


async def test_a_coder_cut_off_having_changed_nothing_is_a_failure(repo):
    outcome = await _run_with(repo, {"coder": [[READS_FOREVER]], "tester": TESTER,
                                     "judge": [[_verdict("approve")]]})

    assert outcome.status == "failed" and "did not finish (turns" in outcome.reasons
    assert [s.name for s in outcome.stages] == ["coder"]


async def test_a_coder_that_says_the_request_cannot_be_a_module_ends_the_build_there(repo):
    """A request that needs a slot Steve lacks, or a fix to Steve itself, if
    forced into a module, is a module doing the wrong thing with the user's
    yes on it. The coder's word ends the build, for the caller to write up as
    a proposal or a problem report, and no later role is paid for."""
    said = {"summary": "not a module", "cannot": "slot", "why": "It needs a voice slot."}
    outcome = await _run(repo, {"coder": [[said]], "tester": TESTER,
                                "judge": [[_verdict("approve")]]})

    assert outcome.status == "failed" and not outcome.approved
    assert outcome.cannot == {"kind": "slot", "why": "It needs a voice slot."}
    assert [s.name for s in outcome.stages] == ["coder"]


SCRATCH = "tests/unit/test_scratch_independent.py"
DOCSTRING = '"""Arithmetic for the briefing."""\n'


async def test_a_file_the_test_builder_left_does_not_fail_the_revision_s_lint(repo):
    """The third live run: a scratch file the test builder left failed lint
    during the revision. The builder's files are read-only to the coder, so
    each edit it tried was refused, and a correct change was rejected over a
    file it could not touch."""
    tester = [[
        ("write_file", {"path": INDEPENDENT, "content":
                        "def test_adding_two_and_three_gives_five():\n"
                        "    from agent.calc import add\n    assert add(2, 3) == 5\n"}),
        ("write_file", {"path": SCRATCH, "content": "import os\n\n\ndef test_x():\n    pass\n"}),
        {"summary": "one sharp test", "tests": [INDEPENDENT], "checks": ["2 + 3 is 5"]},
    ]]
    revision = [("edit_file", {"path": "agent/calc.py", "old": BUG, "new": GOOD}),
                {"summary": "fixed: add adds"}]
    outcome = await _run_with(repo, {
        "coder": [_coder_writes(BUG), revision], "tester": tester,
        "judge": [[_verdict("revise", rulings=[BUG_RULING], reasons="add subtracts")],
                  [_verdict("approve")]],
    }, runner=_RealRuff(repo))

    assert outcome.approved, outcome.reasons
    assert "revision fix" not in [s.name for s in outcome.stages]
    assert not (repo / SCRATCH).exists(), "not kept, so not left behind"


UNUSED = "    unused = 1\n"


def _revision_breaking_lint(fix_round):
    """A revision that leaves an unused local, which ruff does not fix by
    itself (removing it is not a safe fix)."""
    return [
        _coder_writes(BUG),
        [("edit_file", {"path": "agent/calc.py", "old": BUG, "new": UNUSED + GOOD}),
         {"summary": "fixed: add adds"}],
        fix_round,
    ]


async def test_a_revision_that_breaks_lint_gets_one_round_to_fix_it(repo):
    """The first attempt had a round to fix what the checks found; the
    revision did not, and was rejected over lint it could have fixed."""
    fix = [("edit_file", {"path": "agent/calc.py", "old": UNUSED, "new": ""}),
           {"summary": "removed the unused variable"}]
    outcome = await _run_with(repo, {
        "coder": _revision_breaking_lint(fix), "tester": TESTER,
        "judge": [[_verdict("revise", rulings=[BUG_RULING], reasons="add subtracts")],
                  [_verdict("approve")]],
    }, runner=_RealRuff(repo))

    assert outcome.approved, outcome.reasons
    assert [s.name for s in outcome.stages] == [
        "coder", "test builder", "judge 1", "coder revision", "revision fix", "judge 2"]


async def test_a_revision_still_broken_after_its_fix_round_is_rejected_saying_what_failed(repo):
    outcome = await _run_with(repo, {
        "coder": _revision_breaking_lint([{"summary": "nothing to fix"}]),
        "tester": TESTER,
        "judge": [[_verdict("revise", rulings=[BUG_RULING], reasons="add subtracts")],
                  [_verdict("approve")]],
    }, runner=_RealRuff(repo))

    assert outcome.status == "rejected"
    assert "revision broke lint" in outcome.reasons and "F841" in outcome.reasons


async def test_imports_out_of_order_or_unused_are_put_right_without_costing_a_round(repo):
    """Ruff's defaults sort imports, and models order them by hand badly.
    In the self-upgrade trial (19 October 2026) five admitted modules
    carried `# noqa: I001`, and in its rerun a build was lost with two
    unsorted import blocks still failing after its fix round. Ruff's safe
    fixes run before lint, so neither costs the coder a round."""
    imports = DOCSTRING + "\nimport sys\nimport os\nimport json\n"
    coder = [("edit_file", {"path": "agent/calc.py", "old": "    return 0\n",
                            "new": GOOD + "\n\ndef where():\n    return os.sep, sys.platform\n"}),
             ("edit_file", {"path": "agent/calc.py", "old": DOCSTRING, "new": imports}),
             {"summary": "add adds"}]
    runner = _RealRuff(repo)
    outcome = await _run_with(repo, {"coder": [coder], "tester": TESTER,
                                     "judge": [[_verdict("approve")]]}, runner=runner)

    assert outcome.approved, outcome.reasons
    assert [s.name for s in outcome.stages] == ["coder", "test builder", "judge 1"]
    calc = (repo / "agent/calc.py").read_text(encoding="utf-8")
    assert "\nimport os\nimport sys\n" in calc and "json" not in calc
    # A real build's runner refuses what it does not allow, and the fixes'
    # own result is not read: a refusal there would be silent.
    ruffs = [argv[3:] for argv in runner.argv if "ruff" in argv]
    assert ["check", ".", "--fix", "--quiet"] in ruffs
    assert all(testrun.ruff_args(list(args)) is not None for args in ruffs)


def _manifest_screen(root):
    """Admission's screen as the build hands it over (`core/lifecycle.screened`)."""
    return None if (root / "manifest.toml").is_file() else "it has no manifest.toml at its top"


async def test_what_admission_would_refuse_goes_back_to_the_coder_with_the_checks(repo):
    """In the module-building eval's first run a coder's manifest gave a
    tool no class. Lint and the suite passed, the judge approved, admission
    refused, and the build was lost; heard with the checks, it is one
    round's fix."""
    fix = [("write_file", {"path": "manifest.toml", "content": 'id = "calc"\n'}),
           {"summary": "wrote the manifest"}]
    outcome = await _run_with(repo, {"coder": [_coder_writes(GOOD), fix], "tester": TESTER,
                                     "judge": [[_verdict("approve")]]}, screen=_manifest_screen)

    assert outcome.approved, outcome.reasons
    assert _ran(outcome) == ["coder", "coder fix", "final"]
    brief = pipeline._feedback(pipeline.Checked(ok=False, checks={}, refused="its manifest: x"))
    assert "## What Steve would refuse when it admits the module\n\nits manifest: x" in brief


async def test_a_change_admission_would_still_refuse_is_rejected_saying_why(repo):
    outcome = await _run_with(repo, {"coder": [_coder_writes(GOOD), [{"summary": "no change"}]],
                                     "tester": TESTER, "judge": [[_verdict("approve")]]},
                              screen=_manifest_screen)

    assert outcome.status == "rejected"
    assert "admission would refuse it: it has no manifest.toml at its top" in outcome.reasons


def test_the_coder_cannot_spend_what_the_later_roles_need():
    """Its fix round spent a second coder budget and left the judge nothing."""
    spend = pipeline._Spend(0.50)
    spend.used = 0.25  # the first coder run

    fix = spend.role(CONFIG.coder, reserve=CONFIG.tester.budget_usd + CONFIG.judge.budget_usd)

    assert fix.budget_usd == pytest.approx(0.0)
