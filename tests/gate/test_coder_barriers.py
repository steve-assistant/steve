"""The walls between the coding pipeline's roles and everything else.

Frozen, and deliberately overlapping tests/unit/test_coder_tools.py: those
can be changed by the pipeline itself, and these are the ones that say what
it may not do.

- No role has a shell.
- No role reads `data/` or climbs out of its folder.
- No role writes a frozen path.
- The test builder cannot see the coder's tests, cannot edit code, and
  cannot add a conftest (code that runs for every test, the coder's too).
- pytest receives a test path, never an argument that loads a plugin.
- Nothing in the pipeline starts a program but the runner it is given.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.coder.tools import CODER_TOOLS, TESTER_TOOLS, Ran, Tools
from core.coder.workspace import Workspace, builder_may_write, coder_may_write


@pytest.fixture
def repo(tmp_path):
    for rel, text in {
        "agent/x.py": "x = 1\n",
        "core/policy.yaml": "frozen\n",
        "tests/gate/test_judge.py": "judge\n",
        "tests/unit/test_x.py": "coder's test\n",
        "data/agent.db": "private\n",
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _never_run(argv, cwd, timeout_s=0):
    raise AssertionError(f"pytest was started with {argv}")


def test_no_role_has_a_shell_or_any_way_to_run_a_command():
    allowed_to_execute = {"run_tests", "run_lint"}
    for names in (CODER_TOOLS, TESTER_TOOLS):
        assert not {"shell", "bash", "run", "exec", "execute", "run_command", "python"} & set(names)
        assert {n for n in names if "run" in n} <= allowed_to_execute


@pytest.mark.parametrize("path", ["core/policy.yaml", "tests/gate/test_judge.py",
                                  "tests/gate/new_judge.py", "modules/google_cloud/auth.py",
                                  "uv.lock"])
def test_the_coder_writes_no_frozen_path(repo, path):
    tools = Tools(Workspace(repo, coder_may_write), _never_run, "python")

    assert tools.write_file(path, "changed\n").startswith("refused:")


@pytest.mark.parametrize("path", ["data/agent.db", "../outside", ".git/config"])
def test_no_role_reads_their_data_or_outside_the_worktree(repo, path):
    for may_write in (coder_may_write, builder_may_write):
        tools = Tools(Workspace(repo, may_write), _never_run, "python")
        assert tools.read_file(path).startswith("refused:")


def test_the_test_builder_sees_none_of_the_coders_tests(repo):
    tools = Tools(Workspace(repo, builder_may_write, hidden={"tests/unit/test_x.py": None},
                            new_files_only=True), _never_run, "python")

    assert "coder's test" not in tools.read_file("tests/unit/test_x.py")
    assert "test_x.py" not in tools.list_files("**/*")
    assert "edit_file" not in TESTER_TOOLS


@pytest.mark.parametrize("path", ["agent/x.py", "tests/unit/conftest.py", "tests/conftest.py",
                                  "tests/unit/test_x.py", "conftest.py"])
def test_the_test_builder_writes_only_new_test_modules(repo, path):
    tools = Tools(Workspace(repo, builder_may_write, new_files_only=True), _never_run, "python")

    assert tools.write_file(path, "x\n").startswith("refused:")


@pytest.mark.parametrize("target", ["-p evil", "--rootdir=/ tests", "tests && del *"])
def test_pytest_gets_a_test_path_and_nothing_else(repo, target):
    tools = Tools(Workspace(repo, coder_may_write), _never_run, "python")

    assert tools.run_tests(target).startswith("refused:")


def test_no_gate_test_can_reach_a_live_model_through_the_coder():
    """27 September 2026: once self-upgrade v1's sandbox account existed, a
    gate test's attempt took the pipeline path and its coder made a real
    model call. Asked the way the pipeline asks: `models.client`."""
    from core.coder import models

    # Each layer by its own message: the client fence, and the provider
    # fences behind it, would otherwise pass for one another.
    with pytest.raises(RuntimeError, match="reach a live model through the coder"):
        models.client(models.CATALOG["gpt-6-luna"])
    with pytest.raises(RuntimeError, match="reach OpenAI through the coder"):
        models._openai()


def test_a_run_goes_through_the_runner_it_was_given(repo):
    seen = []
    tools = Tools(Workspace(repo, coder_may_write),
                  lambda argv, cwd, timeout_s=0: seen.append(cwd) or Ran(0, "ok", 0.0), "python")

    tools.run_tests("tests/unit")

    assert seen == [repo]


# What core/coder may import from core: the policy, and the money and keys
# its model calls need. Nothing that runs a program.
# core.endpoints: OpenAI's address, an environment read and nothing run.
# core.models_guard: the frozen price table and the dollar sum; it imports
# only core.policy, and runs nothing.
CORE_IMPORTS = {"core.policy", "core.spend", "core.secrets", "core.endpoints",
                "core.models_guard"}


def test_nothing_in_the_pipeline_starts_a_program_but_the_runner():
    """Until step 6 a change was read with git, in a folder the change's own
    tests could write: a hook or a filter driver left there would have run
    as the user on the next git command, and every one went through a
    wrapper that sealed them first (8 October 2026 audit). The base is a
    snapshot now (core/coder/snapshot.py). Pinned by what each module may
    import, so a new way to run git, or anything else, has to come through
    this frozen file."""
    imported: dict[str, set[str]] = {}
    for path in sorted((Path(__file__).resolve().parents[2] / "core" / "coder").glob("*.py")):
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names |= ({node.module} if node.module != "core"
                          else {f"core.{a.name}" for a in node.names})
        imported[path.name] = names

    for name, names in imported.items():
        assert not names & {"subprocess", "multiprocessing", "shutil", "pty"}, name
        core = {n for n in names if n == "core" or n.startswith("core.")
                and not n.startswith("core.coder")}
        assert core <= CORE_IMPORTS, (name, core - CORE_IMPORTS)
    assert not any("os" in names and _calls_os_process(name) for name, names in imported.items())


def _calls_os_process(name: str) -> bool:
    text = (Path(__file__).resolve().parents[2] / "core" / "coder" / name).read_text(
        encoding="utf-8")
    return any(f"os.{f}(" in text for f in ("system", "popen", "spawnv", "spawnl", "startfile",
                                             "execv", "execl"))
