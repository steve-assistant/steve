"""What the user has set up decides what the model is told and offered
(agent/features.py): a feature describes itself only when it is on."""

from __future__ import annotations

import re

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import facts, features, loop
from agent.loop import Conversation
from agent.models import Role
from core import knobs
from tests.fakes import google as fake_google
from tests.fakes import outlook as fake_outlook

# The words that name each feature to a reader. Prose only: a parameter such
# as `email_to` is part of a tool whoever uses it, and is checked as a name
# by the tool lists below instead.
NAMED_BY = {
    "mail": r"\bmail|e-?mail|inbox|\bdrafts?\b|gmail|outlook",
    "mailbox": r"\+agent|own address",
    "calendar": r"calendar|\bevents?\b",
    "deadlines": r"canvas|assignment|\bdue dates\b|\bcourses?\b",
    "drive": r"\bdrive\b|spreadsheet|google doc",
    "tasks": r"google tasks",
    "browser": r"browser|linkedin",
    "code": r"\bpython\b|run(?:ning)? code",
    "desk": r"\bdesk\b|out loud|microphone",
    "weather": r"weather|forecast",
    "self_upgrade": r"self-upgrade|build(?:ing)? (?:it|a module)|modules page",
}


def _prose(text: str) -> str:
    return re.sub(r"`[^`]*`", "", text)


def _named(text: str, off: set[str]) -> dict[str, list[str]]:
    prose = _prose(text)
    found = {f: sorted({m.group(0).lower() for m in re.finditer(NAMED_BY[f], prose, re.I)})
             for f in off}
    return {f: words for f, words in found.items() if words}


def _offered(on) -> dict[str, str]:
    """Each tool a turn's agent is given, with the description the model reads."""
    agent = loop.build_agent(Role.CONVERSE, on=on, lead=True)
    return {name: t.description or "" for name, t in agent._function_toolset.tools.items()}


@pytest.fixture
def nothing_set_up(monkeypatch):
    """A user who has installed Steve and set up nothing: in a test's own
    data folder there is no token, keychain entry, home, add-on or switch
    turned on, and no mailbox is chosen. The voice add-on is
    the developer's machine's, so it is turned off here."""
    monkeypatch.setattr(features, "_voice_installed", lambda: False)


def test_a_user_with_nothing_set_up_is_told_of_nothing_they_lack(store, nothing_set_up):
    """The prompt was written for one user with three mailboxes, a course
    feed and a phone. Given to anyone else, the model offers to search mail
    that is not there, and a tool that can only fail reads as a broken one.
    With nothing set up, only the weather is on (it needs nothing), and no
    section or offered tool names a feature that is off, and no tool of one
    is offered."""
    on = features.enabled(store)
    assert on == {"weather": ""}

    prompt = loop.system_prompt(on)
    every = {f.name for f in features.all_features()} - set(on)
    assert _named(prompt, every) == {}
    feature_tools = features.tools_off(on)
    assert not [n for n in re.findall(r"`(\w+)`", prompt) if n in feature_tools]

    offered = _offered({})
    assert not set(offered) & feature_tools
    naming = {name: words for name, text in offered.items()
              if (words := _named(text, every))}
    assert naming == {}, f"tools offered to everyone that name a feature: {naming}"
    mentioning = {name: sorted(set(re.findall(r"`(\w+)`", text)) & feature_tools)
                  for name, text in offered.items()}
    assert {n: m for n, m in mentioning.items() if m} == {}


def test_a_user_with_everything_set_up_is_told_of_all_of_it():
    """The other end: every section, and every tool of every feature."""
    prompt = loop.system_prompt()
    for feature in features.all_features():
        assert features.section(feature.name).splitlines()[0] in prompt
    assert features.tools_off(features.everything()) == frozenset()
    offered = _offered(features.everything())
    linked = {f.name: sorted(f.tools - set(offered)) for f in features.all_features()}
    assert {n: t for n, t in linked.items() if t} == {}


def test_the_weather_is_offered_with_no_home_and_asks_where_for_one(store, nothing_set_up):
    """Off until a home was given, "will it rain tomorrow?" went to a web
    search before Steve asked where, and "the weather in Boston" -- which
    needs no home -- went to one for good: the slow answer the tool was
    built to replace."""
    from agent.toolset import weather

    on = features.enabled(store)
    assert "weather" in on and "weather" in _offered(on)
    assert "## Weather" in loop.system_prompt(on)
    assert "ask where" in weather.lookup(store, "")["error"]


def test_the_mail_section_lists_the_accounts_the_user_connected(store, nothing_set_up):
    """Mail tools take a mailbox by name. The section says which the user
    has, and no others: an Outlook search on a machine without Outlook
    would be one more failure to explain."""
    from tests.fakes import google as fake_google

    fake_google.signed_in(store, address="maya@example.com", name="gmail")

    on = features.enabled(store)
    prompt = loop.system_prompt(on)

    assert on["mail"] == "- gmail: maya@example.com"
    assert "- gmail: maya@example.com" in prompt
    assert "outlook" not in prompt.lower()
    assert {"mail", "calendar", "drive"} <= set(on)


def _due_dates_calendar(conn) -> None:
    """A course site's calendar link, added as the app adds one, with its
    "due dates" box ticked."""
    from agent import accounts
    from core import modules
    from tests.fakes.ics import LINK, PUBLIC_IP

    modules.register(conn)
    accounts.add(conn, module_id="ics", values={"link": LINK, "due_dates": True},
                 name="Course site", resolve=lambda host, port: [PUBLIC_IP])


def test_a_calendar_marked_as_due_dates_turns_the_deadlines_on(store, nothing_set_up):
    """The personal edition's deadlines were one course site's, read from a
    key of its own. Now any calendar link the user marks as due dates is
    one, and with none there is nothing due to offer."""
    assert "deadlines" not in features.enabled(store)

    _due_dates_calendar(store)

    assert "deadlines" in features.enabled(store)


def test_a_check_that_fails_is_a_feature_not_set_up(store, nothing_set_up, monkeypatch):
    """A turn is never lost to a broken keychain: the feature is off."""
    def broken(conn, klass):
        raise OSError("the keychain is locked")

    monkeypatch.setattr("agent.connectors.accounts", broken)

    assert "deadlines" not in features.enabled(store)


def test_the_core_has_the_standing_rules_and_no_feature(store):
    """With nothing set up, a turn reads the core and nothing else."""
    core = features.core()

    assert features.SECTIONS_HERE not in core
    assert features.SECTIONS_HERE not in loop.system_prompt({})
    assert loop.system_prompt({}) == core
    assert "## When you need the user" in core


async def test_a_turn_is_offered_only_what_the_user_has_set_up(store, monkeypatch,
                                                              memories_repo):
    """End to end: the request a turn sends has the sections and tools of
    the features on, read when the turn starts."""
    seen = {}

    def capture(messages, info: AgentInfo):
        seen["tools"] = {t.name for t in info.function_tools}
        seen["instructions"] = messages[-1].instructions or ""
        return ModelResponse(parts=[TextPart("ok")])

    async def weather_only(conn):
        return {"weather": ""}

    monkeypatch.setattr(features, "now", weather_only)
    monkeypatch.setattr(loop, "model_for", lambda role: FunctionModel(capture))

    await Conversation(store, channel="test").send("will it rain?")

    assert "weather" in seen["tools"] and "search_email" not in seen["tools"]
    assert "## Weather" in seen["instructions"] and "## Mail" not in seen["instructions"]


def test_a_judge_for_a_user_with_nothing_set_up_hears_of_no_source_they_lack(
        store, nothing_set_up):
    """The judge was told of two mailboxes, a course feed and the meetings
    of a recruiting season, whoever it judged for. With nothing set up it
    watches what every user has, and its instructions name nothing else."""
    from agent.attention import sources, triage

    watching = {s.name for s in sources.watching(store)}
    judge = triage.judge_prompt(watching)

    assert watching == {"commitments", "health"}
    assert _named(judge, {f.name for f in features.all_features()}) == {}
    assert "anything they have already" in judge


def test_a_judge_for_a_user_with_everything_set_up_reads_every_source(store, monkeypatch):
    from agent.attention import interests, sources, triage

    monkeypatch.setattr(features, "enabled", lambda conn: features.everything())
    fake_outlook.add(store, name=features.ADDED_TOO[0])
    facts.add(store, facts.USER, "home_city", "Pittsburgh, PA", source="confirmed")
    interests.follow(store, "rail strikes")

    watching = {s.name for s in sources.watching(store)}
    judge = triage.judge_prompt(watching)

    assert watching == {s.name for s in sources.all_sources(store)}
    for _names, says, paragraph in triage.JUDGE_SOURCES:
        assert says in judge and paragraph in judge
    assert "{" not in judge


def test_outlook_is_listed_once_the_user_adds_it_and_never_asked_for_it(store, nothing_set_up,
                                                                       monkeypatch):
    """Asking whether Outlook holds an account starts Outlook. A fresh
    install asked at its first turn, then read and watched the mail in it;
    later a switch had to be on first. Now the account the user adds is the
    yes, and telling what is set up never starts Outlook."""
    asked = []
    monkeypatch.setattr("core.modctx._dispatch", lambda progid: asked.append(progid))

    assert "mail" not in features.enabled(store)

    fake_outlook.add(store)
    on = features.enabled(store)
    assert on["mail"] == f"- outlook: {fake_outlook.ADDRESS}"
    # Each line names the account as the calendar tools take it (`account=`).
    assert on["calendar"] == f"- outlook: {fake_outlook.ADDRESS}"
    assert asked == []


def test_running_code_is_offered_only_once_the_user_turns_it_on(store, nothing_set_up):
    """It needs Docker. Offered to everyone, and told to use it for sums,
    the model's first sum failed on a computer without it."""
    assert "code" not in features.enabled(store)
    assert "run_code" not in _offered(features.enabled(store))

    knobs.write({"RUN_CODE": "on"})
    on = features.enabled(store)
    assert "code" in on and "run_code" in _offered(on)
    assert "## Running code" in loop.system_prompt(on)


async def test_run_code_refuses_while_its_switch_is_off():
    """Checked again where the code runs: a card approved later, or a caller
    that builds its agent with every feature, still meets the switch."""
    from agent.toolset import code

    result = await code._do_run_code(code="print(1)")
    assert result["error"] == features.switched_off("RUN_CODE")


def test_a_switch_that_is_off_says_where_in_the_app_it_is_turned_on():
    """"The user turns it on in Steve's settings" sent them to the Settings
    tab, where there is no switch. The answer names the switch and the page
    and group the app shows it in."""
    from pathlib import Path

    ui = Path(__file__).resolve().parents[2] / "ui" / "src"
    said = features.switched_off("BROWSER")

    assert knobs.KNOBS["BROWSER"].says in said
    assert features.SWITCHES_PAGE in said and features.SWITCHES_GROUP in said
    assert f'name: "{features.SWITCHES_PAGE}"' in (ui / "App.tsx").read_text(encoding="utf-8")
    assert f'features: "{features.SWITCHES_GROUP}"' in (ui / "Autonomy.tsx").read_text(
        encoding="utf-8")


def test_every_switch_says_what_it_adds_and_what_it_takes_beside_itself():
    """The switches said only "(needs Docker)", and nothing said what one
    was for, so whether to turn one on was a guess. A switch added later
    without its line fails here, not on a user's page."""
    from agent.worker import control

    rows = {row["name"]: row for row in control._knob_rows(control.current_knobs())}
    for name in knobs.SWITCHES:
        adds, takes = features.OFFERS[name]
        assert adds and takes.startswith("It takes "), name
        assert (rows[name]["adds"], rows[name]["takes"]) == (adds, takes)
    assert "adds" not in rows["MAX_DAY_PINGS"]


def test_every_app_page_the_backend_sends_the_user_to_is_one_the_apps_menu_has():
    """Step 9's menu calls the modules' page Extensions, and eleven of the
    backend's answers still said "the Modules page": a module turned off for
    failing, a build's card, a skill's revoking. The user looked for a page
    the menu doesn't have. Every "the ... page" a string in agent/ says (its
    docstrings aside) is a page in `MENU`."""
    import ast
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    app = (root / "ui" / "src" / "App.tsx").read_text(encoding="utf-8")
    menu = app[app.index("const MENU"):]
    menu = set(re.findall(r'name: "([^"]+)"', menu[:menu.index("];")]))
    said = re.compile(r"\b(?:the|on|in) ([A-Z][\w']*(?: [\w']+){0,4}?) page\b")

    wrong = []
    for path in sorted((root / "agent").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                           ast.AsyncFunctionDef))
                      and node.body and isinstance(node.body[0], ast.Expr)}
        wrong += [f"{path.relative_to(root).as_posix()}:{node.lineno} {page!r}"
                  for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and id(node) not in docstrings
                  for page in said.findall(node.value) if page not in menu]

    assert "Extensions" in menu and "What Steve may do" in menu
    assert wrong == []


def test_a_check_that_fails_keeps_its_last_answer(store, nothing_set_up, monkeypatch):
    """A keychain that hiccupped took the mail tools away for that turn,
    and changed the cached prompt twice."""
    _due_dates_calendar(store)
    assert "deadlines" in features.enabled(store)

    def broken(conn, klass):
        raise OSError("the keychain is locked")

    monkeypatch.setattr("agent.connectors.accounts", broken)
    assert "deadlines" in features.enabled(store)


def test_each_mailbox_is_watched_only_where_it_is_connected(store):
    """On with any mail feature, the Gmail source failed every three
    minutes for a user whose only mailbox was another."""
    from agent.attention import sources

    fake_outlook.add(store)
    watching = {s.name for s in sources.watching(store)}

    assert f"mail:{fake_outlook.KEY}" in watching
    assert f"mail:{fake_google.KEY}" not in watching
    assert features.accounts(features.everything()["mail"]) == {features.ADDED[0],
                                                                 features.ADDED_TOO[0]}
