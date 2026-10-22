"""The coder's tools: what each role can read and write, and nothing else.

The old coder had a harness's shell. These are the whole of what a role can
do now, so their edges are the security model: nothing outside the
worktree, nothing in `data/`, no frozen path written, and the test builder
limited to adding test files it cannot see the coder's versions of.
"""

from __future__ import annotations

import pytest

from core.coder import context, snapshot
from core.coder.tools import CODER_TOOLS, TESTER_TOOLS, Ran, Tools, summary
from core.coder.workspace import (
    Refused,
    Workspace,
    builder_may_write,
    coder_may_write,
    is_readable,
)

FILES = {
    "agent/weather.py": '"""Weather for the briefing."""\n\n\ndef forecast(place):\n'
                        '    """Three days ahead."""\n    return place\n',
    "core/policy.yaml": "frozen: true\n",
    "tests/unit/test_weather.py": "def test_forecast():\n    assert True\n",
    "data/agent.db": "her whole life\n",
}


@pytest.fixture
def repo(tmp_path, folder):
    return folder(FILES, dest=tmp_path)


class _Runner:
    def __init__(self):
        self.argv = []

    def __call__(self, argv, cwd, timeout_s=600):
        self.argv.append(argv)
        return Ran(0, "1 passed", 0.1)


def _coder(repo):
    runner = _Runner()
    return Tools(Workspace(repo, coder_may_write), runner, "python.exe"), runner


def _tester(repo, hidden=None):
    return Tools(Workspace(repo, builder_may_write, hidden=hidden or {}, new_files_only=True),
                 _Runner(), "python.exe")


# ---------------------------------------------------------------------------
# Where a role can reach
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["../outside.txt", "agent/../../outside.txt",
                                  r"C:\Users\Maya\secret.txt", "/etc/passwd"])
def test_nothing_outside_the_worktree_can_be_read(repo, path):
    tools, _ = _coder(repo)
    assert tools.read_file(path).startswith("refused:")


def test_their_data_is_never_read_or_listed(repo):
    tools, _ = _coder(repo)

    assert tools.read_file("data/agent.db").startswith("refused:")
    assert "data/" not in tools.list_files("**/*")
    assert "her whole life" not in tools.search("life", glob="**/*")


def test_a_frozen_path_cannot_be_written(repo):
    tools, _ = _coder(repo)

    assert tools.write_file("core/policy.yaml", "frozen: false\n").startswith("refused:")
    assert tools.edit_file("core/policy.yaml", "true", "false").startswith("refused:")
    assert (repo / "core/policy.yaml").read_text(encoding="utf-8") == "frozen: true\n"


def test_the_coder_writes_everything_else_and_always_as_lf(repo):
    tools, _ = _coder(repo)

    assert tools.write_file("agent/new.py", "a = 1\r\nb = 2\r\n").startswith("wrote")
    assert (repo / "agent/new.py").read_bytes() == b"a = 1\nb = 2\n"


def test_an_edit_must_match_exactly_once(repo):
    tools, _ = _coder(repo)
    tools.write_file("agent/twice.py", "x = 1\nx = 1\n")

    assert "occurs 2 times" in tools.edit_file("agent/twice.py", "x = 1", "x = 2")
    assert tools.edit_file("agent/weather.py", "return place", "return place.upper()") \
        .startswith("edited")


def test_no_role_has_a_shell():
    """The old harness ran `--auto` with a shell: a run could push, or delete
    files outside its worktree."""
    for names in (CODER_TOOLS, TESTER_TOOLS):
        assert not {"shell", "bash", "run", "execute", "run_command"} & set(names)


# ---------------------------------------------------------------------------
# Running the code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["-p evil_plugin", "tests; rm -rf /", "agent/weather.py",
                                    "tests/unit/test_x.py --rootdir=/"])
def test_only_a_test_path_reaches_pytest(repo, target):
    """An argument is how a plugin gets loaded; the target is a path or
    node id under tests/ and nothing else."""
    tools, runner = _coder(repo)

    assert tools.run_tests(target).startswith("refused:")
    assert runner.argv == []


def test_tests_run_through_the_runner_with_a_fixed_command(repo):
    tools, runner = _coder(repo)

    assert "1 passed" in tools.run_tests("tests/unit/test_weather.py", keyword="forecast")
    assert runner.argv == [["python.exe", "-m", "pytest", "tests/unit/test_weather.py", "-q",
                            "-p", "no:cacheprovider", "--tb=short", "-rf", "-k", "forecast"]]


# ---------------------------------------------------------------------------
# The test builder
# ---------------------------------------------------------------------------


def test_the_test_builder_cannot_see_the_coders_tests(repo):
    """Its tests are meant to be its own reading of the code. Shown the
    coder's, it writes the same tests again."""
    (repo / "tests/unit/test_weather.py").write_text(
        "def test_forecast():\n    assert CODER_WAS_HERE\n", encoding="utf-8")
    (repo / "tests/unit/test_new.py").write_text("def test_new(): pass\n", encoding="utf-8")
    tools = _tester(repo, hidden={
        "tests/unit/test_weather.py": FILES["tests/unit/test_weather.py"],
        "tests/unit/test_new.py": None,
    })

    assert "CODER_WAS_HERE" not in tools.read_file("tests/unit/test_weather.py")
    assert tools.read_file("tests/unit/test_new.py").startswith("no such file")
    assert "test_new.py" not in tools.list_files("tests/**/*.py")
    assert "CODER_WAS_HERE" not in tools.search("CODER_WAS_HERE", glob="**/*")


def test_the_test_builder_only_adds_test_files(repo):
    tools = _tester(repo)

    assert tools.write_file("tests/unit/test_independent.py", "def test_a(): pass\n") \
        .startswith("wrote")
    assert tools.write_file("tests/unit/test_independent.py", "def test_b(): pass\n") \
        .startswith("wrote"), "it may rewrite what it made"
    assert tools.write_file("agent/weather.py", "broken").startswith("refused:")
    assert tools.write_file("tests/unit/test_weather.py", "x").startswith("refused:")
    assert tools.write_file("tests/unit/conftest.py", "x").startswith("refused:")


def test_the_test_builder_cannot_edit_existing_code_at_all():
    assert "edit_file" not in TESTER_TOOLS


# ---------------------------------------------------------------------------
# What a role knows up front
# ---------------------------------------------------------------------------


@pytest.fixture
def library(tmp_path_factory, folder):
    """What a role reads beside the draft: the module guide, and a built-in
    module to learn from, kept outside the draft as the real ones are."""
    base = tmp_path_factory.mktemp("reference")
    folder({"modules.md": "Import only steve_sdk. Test names are sentences.\n",
            "modules/forecast/forecast.py": "def days():\n    return 5\n"}, dest=base)
    return {"modules.md": base / "modules.md", "modules": base / "modules"}


def test_the_context_carries_the_module_guide_the_draft_and_the_relevant_files(repo, library):
    """A role that starts without the guide spends its first turns finding its
    way, and its rejected attempts went to rules it was never shown; one that
    does not see the draft's files writes a new module over an old one."""
    tools = Tools(Workspace(repo, coder_may_write, library=library), _Runner(), "python.exe")

    first = context.pack(tools.ws, "Make the weather forecast cover five days",
                         outline=tools.outline)

    assert "Import only steve_sdk" in first
    assert "agent/weather.py" in first.split("# The module's files so far")[1]
    assert "## agent/weather.py" in first and "def forecast" in first
    assert "## _reference/modules/forecast/forecast.py" in first


def test_a_new_module_s_context_says_it_has_no_files_yet(tmp_path, library):
    """The coder must know whether it is writing a module or changing one: a
    new version keeps the old one's id and tests."""
    draft = tmp_path / "draft"
    draft.mkdir()
    ws = Workspace(draft, coder_may_write, library=library)

    first = context.pack(ws, "Read the tides", outline=lambda rel: "")

    assert "none: it is a new module" in first


def test_the_reference_library_is_readable_and_never_writable(repo, library):
    """The coder learns from the SDK and the built-in modules, but they are
    every module's, not this draft's: a draft that could edit them would
    change what every other module runs against."""
    tools = Tools(Workspace(repo, coder_may_write, library=library), _Runner(), "python.exe")

    assert "return 5" in tools.read_file("_reference/modules/forecast/forecast.py")
    assert "_reference/modules.md" in tools.list_files("_reference/*")
    for said in (tools.write_file("_reference/modules.md", "anything goes\n"),
                 tools.edit_file("_reference/modules/forecast/forecast.py",
                                 old="return 5", new="return 6"),
                 tools.write_file("_reference/new.py", "x = 1\n")):
        assert "may not be written" in said
    assert (library["modules"] / "forecast/forecast.py").read_text(encoding="utf-8").endswith(
        "return 5\n")
    assert not (repo / "_reference").exists()


def test_a_link_in_the_library_cannot_reach_outside_it(repo, library):
    """A `..` in a path is taken out before the library is reached
    (`Workspace.rel`), but a link inside a library folder is followed when
    the path is resolved: one that led outside would read whatever it
    points at. The library is a checkout's own folders, where a developer's
    junction is no stranger than anywhere else."""
    import _winapi

    outside = library["modules"].parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not for the coder\n", encoding="utf-8", newline="\n")
    _winapi.CreateJunction(str(outside), str(library["modules"] / "forecast" / "elsewhere"))
    tools = Tools(Workspace(repo, coder_may_write, library=library), _Runner(), "python.exe")

    said = tools.read_file("_reference/modules/forecast/elsewhere/secret.txt")

    assert "not for the coder" not in said and "outside the reference" in said


def test_the_outline_lists_definitions_with_lines(repo):
    tools, _ = _coder(repo)

    assert "L4 def forecast -- Three days ahead." in tools.outline("agent/weather.py")


def test_a_refusal_is_an_answer_not_an_exception(repo):
    tools, _ = _coder(repo)

    with pytest.raises(Refused):
        tools.ws.read("data/agent.db")
    assert tools.read_file("data/agent.db").startswith("refused:")


# ---------------------------------------------------------------------------
# The audit of 27 September 2026
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["Data/agent.db", "DATA/agent.db", "data./agent.db",
                                  "data /agent.db", "data/agent.db:secret", "DaTa/Agent.DB"])
def test_their_data_is_not_readable_by_another_spelling(repo, path):
    """Windows does not care how a name is spelled; the guard compared text.
    `Data/agent.db` would have read their database."""
    tools, _ = _coder(repo)

    assert tools.read_file(path).startswith("refused:")


@pytest.mark.parametrize("path", ["Core/policy.yaml", "CORE/policy.yaml", "core./policy.yaml",
                                  "core/policy.yaml:x", "Scripts/setup.ps1"])
def test_a_frozen_file_is_not_writable_by_another_spelling(repo, path):
    tools, _ = _coder(repo)
    (repo / "scripts").mkdir(exist_ok=True)

    assert tools.write_file(path, "changed\n").startswith("refused:")
    assert (repo / "core/policy.yaml").read_text(encoding="utf-8") == "frozen: true\n"


def test_a_path_is_checked_as_it_is_spelled_on_disk(repo):
    """Each layer on its own: resolution to the real spelling, which the
    other rules then read."""
    tools, _ = _coder(repo)

    assert tools.ws.rel("Data/agent.db") == "data/agent.db"
    assert tools.ws.rel("AGENT/Weather.py") == "agent/weather.py"


def test_a_colon_is_refused_even_where_nothing_else_would_be(repo):
    """An alternate data stream on an ordinary file: no other rule objects."""
    tools, _ = _coder(repo)

    with pytest.raises(Refused, match="colon"):
        tools.ws.rel("agent/weather.py:hidden")


def test_a_frozen_folder_not_made_yet_is_frozen_in_any_spelling(repo):
    """Nothing to resolve against: only the lower-case check sees that
    `Scripts/` is `scripts/**`."""
    tools, _ = _coder(repo)

    assert not (repo / "scripts").exists()
    assert tools.write_file("Scripts/setup.ps1", "evil\n").startswith("refused:")


def test_the_scripts_the_user_runs_as_administrator_are_frozen(repo):
    tools, _ = _coder(repo)

    assert tools.write_file("scripts/install.ps1", "evil\n").startswith("refused:")


@pytest.mark.parametrize("target", ["tests/../agent/weather.py", "tests/unit/../../x.py",
                                    "tests/./unit", "tests/..", "tests/unit/..::x"])
def test_a_test_path_cannot_climb_out_of_tests(repo, target):
    """`..` would reach out of the folder."""
    tools, runner = _coder(repo)

    assert tools.run_tests(target).startswith("refused:")
    assert runner.argv == []


def test_the_diff_covers_the_whole_attempt_from_the_folder_as_it_began(repo):
    """Measured from the coder's finished work, the test builder's view of
    the change was empty (audit of 27 September 2026). It is measured from
    the snapshot taken before the coder began, new files included."""
    base = snapshot.take(repo, is_readable)
    (repo / "agent/weather.py").write_text("def forecast(place):\n    return 5\n",
                                           encoding="utf-8")
    (repo / "agent/new.py").write_text("x = 1\n", encoding="utf-8")

    tools = Tools(Workspace(repo, builder_may_write), _Runner(), "python.exe", base=base)
    diff = tools.show_diff()

    assert "+    return 5" in diff
    assert "+++ b/agent/new.py" in diff


def test_the_diff_leaves_out_the_coders_tests_the_test_builder_may_not_see(repo):
    """The test builder reads the coder's tests as they were; a diff that
    showed them would hand them over by another door."""
    base = snapshot.take(repo, is_readable)
    (repo / "tests/unit/test_weather.py").write_text("def test_secret():\n    assert 1\n",
                                                     encoding="utf-8")
    (repo / "agent/weather.py").write_text("changed = True\n", encoding="utf-8")
    hidden = {"tests/unit/test_weather.py": base.had("tests/unit/test_weather.py")}

    tools = Tools(Workspace(repo, builder_may_write, hidden=hidden), _Runner(), "python.exe",
                  base=base)
    diff = tools.show_diff()

    assert "changed = True" in diff
    assert "test_secret" not in diff


def test_the_test_builder_runs_only_the_tests_it_wrote(repo):
    """Running tests/unit printed the coder's test names and failing asserts
    into the builder's context -- the coder's tests, by another door."""
    runner = _Runner()
    tools = Tools(Workspace(repo, builder_may_write, new_files_only=True), runner,
                  "python.exe", own_tests_only=True)
    tools.write_file("tests/unit/test_mine_independent.py", "def test_a(): pass\n")

    assert tools.run_tests("tests/unit").startswith("refused:")
    assert tools.run_tests("tests/unit/test_weather.py").startswith("refused:")
    assert "1 passed" in tools.run_tests("tests/unit/test_mine_independent.py::test_a")
    assert len(runner.argv) == 1


def test_the_test_builder_lints_only_the_files_it_wrote(repo):
    """It was given run_lint after the first live run lost an approved change
    to an unused import in its file. Over the whole tree, ruff's findings in
    the coder's tests -- hidden from it but on disk -- would name them."""
    runner = _Runner()
    tools = Tools(Workspace(repo, builder_may_write, new_files_only=True), runner,
                  "python.exe", own_tests_only=True)

    assert tools.run_lint().startswith("nothing to lint") and runner.argv == []
    tools.write_file("tests/unit/test_mine_independent.py", "def test_a(): pass\n")
    tools.run_lint()

    assert "run_lint" in TESTER_TOOLS
    linted = runner.argv[-1][runner.argv[-1].index("check") + 1:]
    assert linted[0] == "tests/unit/test_mine_independent.py" and "." not in linted


# ---------------------------------------------------------------------------
# The record of what the calls did
# ---------------------------------------------------------------------------


def test_every_call_is_recorded_with_what_it_was_for_and_how_it_went(repo):
    """The second live run's coder took sixty turns and changed nothing, and
    a list of tool names could not say whether it never tried to edit or
    every edit was refused."""
    class _Failing(_Runner):
        def __call__(self, argv, cwd, timeout_s=600):
            self.argv.append(argv)
            return Ran(1, "FAILED tests/unit/test_weather.py::test_forecast\n1 failed", 0.1)

    tools = Tools(Workspace(repo, coder_may_write), _Failing(), "python.exe")

    tools.read_file("agent/weather.py")
    tools.edit_file("agent/weather.py", old="return nothing", new="return 5")
    tools.edit_file("core/policy.yaml", old="frozen: true", new="frozen: false")
    tools.run_tests("tests/unit/test_weather.py")

    assert [(c.name, c.about, c.outcome) for c in tools.log] == [
        ("read_file", "agent/weather.py", "ok"),
        ("edit_file", "agent/weather.py", "refused"),
        ("edit_file", "core/policy.yaml", "refused"),
        ("run_tests", "tests/unit/test_weather.py", "failed"),
    ]
    line = summary(tools.log)
    assert line.startswith("4 calls")
    assert "edit_file 2 (2 refused)" in line and "run_tests 1 (1 failed)" in line
    assert "`old` occurs 0 times in agent/weather.py" in line


def test_a_long_plain_k_expression_is_run_and_punctuation_is_still_refused(repo):
    """The coder's -k of six test names joined by "or" was refused for its
    length, with a message saying it was not a plain expression."""
    tools, runner = _coder(repo)
    names = " or ".join(f"test_the_scheduling_check_case_{n}" for n in range(6))

    assert "1 passed" in tools.run_tests("tests/unit/test_weather.py", keyword=names)
    assert runner.argv[-1][-2:] == ["-k", names]
    assert tools.run_tests("tests/unit/test_weather.py", keyword="x; rm").startswith("refused:")
