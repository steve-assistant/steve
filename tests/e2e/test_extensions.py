"""Extensions, end to end: a tool the user asks for in the chat is built by
Steve's coder, tested and approved, used, switched off and removed, with
its code run in the real sandbox (an AppContainer, 06-modules.md).

The coder, the independent test builder, the judge and the admission's
reviewer are the fake OpenAI playing a script (tests/fakes/openai.py), each
told apart by the tools it is offered or the words it is sent. Everything
else is as shipped: the worker's check at its start that this computer
can hold a module (`core/sandbox/probe.py`), the build in its thread
(`agent/modbuild.py`), the module's own tests and the tester's run in a
container (`core/sandbox/testrun.py`), the card, the yes, the call in the
module's own container (`core/sandbox/run.py`), and the removal that
deletes it. The unit tests script each part alone
(tests/unit/test_coder_pipeline.py, tests/unit/test_modbuild.py); the
sandbox's walls are the gate's (tests/gate/test_module_sandbox.py).

The backend makes AppContainer profiles, in the user's registry and
AppData, outside any test's folder: the file is marked `appcontainer`,
and its fixture deletes the module's profile whatever happened.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from core import identity
from core.sandbox import profiles
from tests.e2e.backend import Backend, running, until
from tests.fakes.jev import FakeJev
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.world import world

pytestmark = pytest.mark.appcontainer

ASK_BUILD = "Can you build me a tool that works out the day's high tides from the first one?"
REQUEST = ("A tool that gives the day's two high tides from the first one: the second "
           "comes 12 hours and 25 minutes after it.")
BUILDING = "I'm building it now. Its card will be on the Extensions page when it's ready."
ASK_TIDES = "The first high tide today is at 06:10. When is the second?"
TOLD = "The second high tide is at 18:35."

TOOL = "mod__tides__high_tides"
MANIFEST = """manifest_version = 1
sdk = 1
id = "tides"
version = "1"
label = "Tides"
for = "Working out the day's two high tides from the first one"
tier = "sandbox"
code = "tides.py"

[[tools]]
name = "high_tides"
class = "read"
description = "The day's two high tides, 12 hours and 25 minutes apart, from the first."
params = [
  { name = "first", type = "string", description = "The first high tide, as HH:MM" },
]
"""
CODE = '''"""High tides come about 12 hours and 25 minutes apart."""

GAP = 12 * 60 + 25


def high_tides(ctx, first: str) -> dict:
    hours, minutes = (int(part) for part in first.split(":"))
    second = (hours * 60 + minutes + GAP) % (24 * 60)
    return {"high_tides": [first, f"{second // 60:02d}:{second % 60:02d}"]}
'''
OWN_TEST = '''from tides import tides


def test_the_second_tide_is_twelve_hours_and_25_minutes_after_the_first():
    assert tides.high_tides(None, "06:10")["high_tides"] == ["06:10", "18:35"]
'''
INDEPENDENT = "tests/test_tides_independent.py"
THEIR_TEST = '''from tides import tides


def test_an_evening_tide_gives_the_next_mornings_time():
    assert tides.high_tides(None, "20:00")["high_tides"] == ["20:00", "08:25"]
'''

# The coder is offered `edit_file`, the tester only `write_file`: the
# coder's rules come first and name the tool the tester lacks.
CODER = "edit_file"
RULES = [
    Rule(said="high tides from the first", offered="build_module", call="build_module",
         arguments={"request": REQUEST}),
    Rule(after="build_module", say=BUILDING),
    Rule(said="Build a module for this request", offered=CODER, call="write_file",
         arguments={"path": "manifest.toml", "content": MANIFEST}),
    Rule(result="wrote manifest.toml", offered=CODER, call="write_file",
         arguments={"path": "tides.py", "content": CODE}),
    Rule(result="wrote tides.py", offered=CODER, call="write_file",
         arguments={"path": "tests/test_tides.py", "content": OWN_TEST}),
    Rule(result="wrote tests/test_tides.py", offered=CODER, call="final_result",
         arguments={"summary": "A module whose one tool adds 12 hours 25 minutes.",
                    "tests_added": ["tests/test_tides.py"]}),
    Rule(said="Build a module for this request", offered="write_file", call="write_file",
         arguments={"path": INDEPENDENT, "content": THEIR_TEST}),
    Rule(result=f"wrote {INDEPENDENT}", offered="write_file", call="final_result",
         arguments={"summary": "One test across midnight.", "tests": [INDEPENDENT],
                    "checks": ["an evening tide gives the next morning's"]}),
    Rule(said="# The independent test builder's report", call="final_result",
         arguments={"decision": "approve", "reasons": "It does what was asked.",
                    "rulings": [], "keep_tests": [INDEPENDENT]}),
    Rule(said="What its manifest grants it", call="final_result",
         arguments={"approved": True, "reason": "It only adds to the time it is given."}),
    Rule(said="When is the second?", offered=TOOL, call=TOOL, arguments={"first": "06:10"}),
    Rule(after=TOOL, say=TOLD),
]


def offered(kept) -> set[str]:
    """The tools a request offered, by either endpoint."""
    return {tool.get("name") or tool.get("function", {}).get("name")
            for tool in kept.body.get("tools") or []}


def asked_with(made, words: str) -> list[set[str]]:
    """The tools offered with each request that carried these words."""
    return [offered(kept) for kept in list(made.openai.requests)
            if words in json.dumps(kept.body)]


def listing(steve: Backend) -> dict:
    return steve.get("/control/modules").json()


def installed(steve: Backend, module: str) -> dict | None:
    return next((m for m in listing(steve)["modules"] if m["id"] == module), None)


def folder_of(profile: str) -> Path:
    """Where Windows keeps an AppContainer profile's files."""
    return Path.home() / "AppData" / "Local" / "Packages" / profile


@pytest.fixture(scope="module")
def steve(fenced):
    # A module's result is outside text, which the screen reads as it is
    # unless Jev chooses otherwise (agent/screening.py); the screen's own
    # scenarios are the red team's.
    jev = FakeJev(otherwise=0.1, handling="as_is")
    with (world(fenced / "world", openai=FakeOpenAI(RULES), jev=jev, sandbox=True) as made,
          running(made, fenced / "data") as backend):
        try:
            yield backend
        finally:
            with contextlib.suppress(OSError):
                install = (fenced / "data" / profiles.INSTALL_FILE).read_text("ascii")[:8]
                profiles.delete(identity.container(install, "tides"))


async def test_a_tool_asked_for_is_built_tested_used_switched_off_and_removed(steve):
    """The whole of an extension's life, as a user lives it. Before the
    yes, the module is not offered to the model; on it and switched on, its
    tool runs in the module's own container and its answer reaches the
    model; switched off, it is offered no more; removed, it is gone with its
    container."""
    made = steve.world
    # The worker asks at its start whether modules can run here, and
    # offers no build until it knows: the app's Extensions page waits on
    # the same answer.
    assert until(lambda: steve.get("/control/places").json()["extensions"]["can_build"],
                 within=60), "no answer on whether modules can run here\n" + steve.logs()
    said = await steve.chat(ASK_BUILD)
    assert said["text"] == BUILDING, (said, steve.logs())

    assert until(lambda: any(b["state"] not in ("running", "needs_allow")
                             for b in listing(steve)["builds"]), within=240), (
        f"the build still running: {listing(steve)['builds']}\n{steve.logs()}")
    build = listing(steve)["builds"][0]
    assert build["state"] == "carded", (build, steve.logs())
    card = next(c for c in listing(steve)["admissions"] if c["id"] == "tides")
    assert installed(steve, "tides") is None, "installed before the yes"

    approved = steve.post("/control/modules", {"op": "approve",
                                               "admission": card["admission"]}).json()
    assert approved == {"ok": True, "id": "tides"}, approved
    assert not installed(steve, "tides")["enabled"], "a new module starts off"
    switched = steve.post("/control/modules", {"op": "switch", "id": "tides", "on": True})
    assert switched.json() == {"ok": True}, switched.text

    said = await steve.chat(ASK_TIDES)
    assert said["text"] == TOLD, (said, steve.logs())
    ran = [text for tool, text in made.openai.results if tool == TOOL]
    assert len(ran) == 1 and "18:35" in ran[0], made.openai.results
    install = (steve.data / profiles.INSTALL_FILE).read_text("ascii")[:8]
    profile = identity.container(install, "tides")
    assert folder_of(profile).is_dir(), f"no container {profile} made for its call"

    switched = steve.post("/control/modules", {"op": "switch", "id": "tides", "on": False})
    assert switched.json() == {"ok": True}, switched.text
    await steve.chat(ASK_TIDES)
    assert TOOL in asked_with(made, ASK_TIDES)[0]
    assert TOOL not in asked_with(made, ASK_TIDES)[-1], "offered while off"

    removed = steve.post("/control/modules", {"op": "remove", "id": "tides", "forget": True})
    assert removed.json().get("ok"), removed.text
    assert installed(steve, "tides") is None
    assert not folder_of(profile).exists(), f"{profile} kept after the module went"
