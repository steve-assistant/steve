"""A module's tool runs in the sandbox, end to end (06-modules.md, item 6).

The tool slot replaced code Steve wrote into its own process, so this
follows the path a model's call takes with nothing faked but the model:
the toolset offers `countdown`'s tool, the call starts the runtime's Python
in the module's own container, from a copy of its release in the data
folder, and the answer comes back through the screen, whose readers are a
fake here as everywhere in the tests. Nothing is skipped: a runtime that is
not built fails it.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agent import modadmit, screening, toolset
from agent.turn import Deps
from core import addons, audit, db, lifecycle, modules, paths, screen
from core.modctx import Network
from core.sandbox import grants, probe, run, testrun

pytestmark = [pytest.mark.processes, pytest.mark.appcontainer]

NAME = "mod__countdown__days_until"

POSTER = """manifest_version = 1
sdk = 1
id = "poster"
version = "1"
label = "Poster"
for = "Posting a note to a service"
hosts = ["api.poster.example"]
code = "poster.py"

[methods]
"api.poster.example" = ["GET", "POST"]

[[tools]]
name = "post"
class = "action"
description = "Posts a note."
"""

POSTER_CODE = """def _try(ctx):
    try:
        return ctx.http.request("POST", "https://api.poster.example/notes").status
    except PermissionError:
        return "refused"


def post(ctx):
    return _try(ctx)


def perform(ctx, action, args):
    return _try(ctx)
"""


class Outside(Network):
    """Every name public, every request answered 201 and kept."""

    def __init__(self):
        self.seen: list[httpx.Request] = []

    def resolve(self, host, port):
        return ["93.184.216.34"]

    def own_ports(self):
        return set()

    def transport_for(self, host):
        def handle(request):
            self.seen.append(request)
            return httpx.Response(201)

        return httpx.MockTransport(handle)


@pytest.fixture
def conn(monkeypatch):
    # The verdict the worker's start gives on this computer: its check is
    # tests/gate/test_module_sandbox.py's, with this computer's own address.
    monkeypatch.setattr(probe, "_verdict", probe.Support(True, probe.LPAC, ""))
    conn = db.open_store(paths.db_path())
    modules.register(conn)
    yield conn
    conn.close()
    lifecycle.drop_container("countdown")
    lifecycle.drop_container("poster")


def test_the_model_calls_countdown_and_it_answers_from_its_own_container(conn, monkeypatch):
    returned, screened = [], []

    def passed(conn, artifact):
        screened.append((artifact.source, artifact.source_id, artifact.text))
        return screen.Shown(artifact.text, "text")

    monkeypatch.setattr(screening, "_read", passed)

    def respond(messages, info):
        answers = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not answers:
            assert NAME in {t.name for t in info.function_tools}
            return ModelResponse(parts=[ToolCallPart(
                NAME, {"target": "2026-12-31", "today": "2026-12-25"})])
        returned.extend(a.content for a in answers)
        return ModelResponse(parts=[TextPart("Six days.")])

    agent = Agent(FunctionModel(respond))
    toolset.register_tools(agent)
    # The user's own conversation: with no one there, the call is a card.
    deps = Deps(conn=conn, session_id=1)
    assert asyncio.run(agent.run("How long until New Year's Eve?", deps=deps)).output == "Six days."

    assert returned == [{"ok": True,
                         "result": {"days": 6, "date": "2026-12-31", "weekday": "Thursday"}}]
    assert deps.tools_used == [NAME]
    assert screened == [("module_result", NAME,
                         '{"days": 6, "date": "2026-12-31", "weekday": "Thursday"}')]
    release = modules.builtins()["countdown"].path.parent
    copy = paths.module_code_dir("countdown", "1")
    assert addons.tree_hash(copy) == addons.tree_hash(release, skip=frozenset({"__pycache__"}))
    assert "S-1-15-2-" in grants.access_list(copy), "read by its container, from the copy"


def test_a_tools_call_cannot_post_where_its_manifest_may_and_its_perform_can(conn, tmp_path):
    """A tool's call is the model's to start, so it runs able only to look:
    a POST its own manifest declares is refused inside the broker before it
    leaves. Only `perform`, which runs on the user's yes to a card, holds
    the methods the manifest declares. Here an outside module, installed as
    the user's yes installs it, tries the same POST both ways."""
    source = tmp_path / "poster"
    (source / "tests").mkdir(parents=True)
    (source / "manifest.toml").write_text(POSTER, encoding="utf-8", newline="\n")
    (source / "poster.py").write_text(POSTER_CODE, encoding="utf-8", newline="\n")
    (source / "tests" / "test_poster.py").write_text(
        "def test_nothing():\n    pass\n", encoding="utf-8", newline="\n")

    async def reviewed(source, module_id, said):
        return audit.Verdict(True, "it posts a note", model="fake")

    admitted = asyncio.run(modadmit.admit(
        conn, source, origin="imported",
        screener=lambda conn, artifact: screen.Verdict(verdict="as_is"),
        reviewer=reviewed, tester=lambda module_id, folder: testrun.Ran(True, "1 passed")))
    lifecycle.approve(conn, admitted, given="")
    modules.set_enabled(conn, "poster", True)
    manifest = {held.id: held for held in modules.modules(conn)}["poster"].manifest
    outside = Outside()

    said = run.call(conn, manifest, "tool", {"name": "post", "args": {}},
                    redact=lambda text: text, network=outside)
    assert said == "refused" and outside.seen == []

    said = run.call(conn, manifest, "perform", {"action": "post", "args": {}}, perform=True,
                    redact=lambda text: text, network=outside)
    assert said == 201 and [r.method for r in outside.seen] == ["POST"]


HARBOUR = """manifest_version = 1
sdk = 1
id = "harbour"
version = "1"
label = "Harbour"
for = "The harbour's notices"
hosts = ["notices.harbour.example"]
code = "harbour.py"

[methods]
"notices.harbour.example" = ["GET", "POST"]

[attention]
looks_at = "the harbour's notices"
judge = "A notice that closes the harbour today is now."
"""

HARBOUR_CODE = """def collect(ctx):
    try:
        posted = ctx.http.request("POST", "https://notices.harbour.example/seen").status
    except PermissionError:
        posted = "refused"
    return [{"key": "n1", "title": f"Closed; posting was {posted}",
             "link": "https://notices.harbour.example/n1"}]
"""


def test_a_modules_source_is_asked_in_its_container_and_may_look_but_never_act(
        conn, tmp_path, monkeypatch):
    """Core asks `collect` with no one watching, so like a tool's call it
    holds no method beyond looking, whatever its manifest declares; what it
    answers becomes the attention loop's observations."""
    from agent.attention.sources import modules as modsources

    source = tmp_path / "harbour"
    (source / "tests").mkdir(parents=True)
    (source / "manifest.toml").write_text(HARBOUR, encoding="utf-8", newline="\n")
    (source / "harbour.py").write_text(HARBOUR_CODE, encoding="utf-8", newline="\n")
    (source / "tests" / "test_harbour.py").write_text(
        "def test_nothing():\n    pass\n", encoding="utf-8", newline="\n")

    async def reviewed(source, module_id, said):
        return audit.Verdict(True, "it reads notices", model="fake")

    admitted = asyncio.run(modadmit.admit(
        conn, source, origin="imported",
        screener=lambda conn, artifact: screen.Verdict(verdict="as_is"),
        reviewer=reviewed, tester=lambda module_id, folder: testrun.Ran(True, "1 passed")))
    lifecycle.approve(conn, admitted, given="")
    modules.set_enabled(conn, "harbour", True)
    outside, real = Outside(), run.call
    monkeypatch.setattr(run, "call", lambda *a, **kw: real(*a, network=outside, **kw))

    try:
        [found] = modsources.collect(conn, module_id="harbour")
    finally:
        lifecycle.drop_container("harbour")

    key = modules.module(conn, "harbour").key
    assert (found.key, found.title) == (f"module:{key}:n1", "Closed; posting was refused")
    assert found.ref == {"link": "https://notices.harbour.example/n1"} and outside.seen == []
