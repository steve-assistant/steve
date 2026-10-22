"""The persona eval holds Steve to rules about what it assumes of a user.

A rule that misreads is worse than none: one that takes "of course" for
school, or a dashed sign-off for no signature, fails a good answer or
passes a bad one, and the report is believed either way. These pin the
rules (agent/persona_eval.py) on text written to break them, the world it
adds to the yardstick's, and one case played end to end through the real
loop with a scripted model. The live run is `python -m agent.persona_eval run`.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import autonomy_eval as ae
from agent import connectors
from agent import persona_eval as pe
from steve_sdk import ids
from tests.modules.world import provider as world


def _case(name: str) -> pe.Case:
    return next(c for c in pe.cases() if c.id == name)


def _problems(case: pe.Case, said: str = "", **seen) -> list[str]:
    return pe.score(case, pe.Seen(reply=said, **seen))


@pytest.mark.parametrize(("body", "name"), [
    ("Hi Kim,\n\nI'll send them tonight.\n\nBest,\nAlex", "Alex"),
    ("Hi Kim, I'll send them tonight.\nThanks, Alex", "Alex"),
    ("Hi Kim, I'll send them tonight.\n-- Alex", "Alex"),
    ("Hi Kim, I'll send them tonight.\nThanks!", None),
    ("Hi Kim, I'll send them tonight.", None),
])
def test_a_drafts_signature_is_read_wherever_a_sign_off_puts_it(body, name):
    """A name missed on any of these forms would pass a draft signed with
    a stranger's name, the very thing the check is for."""
    assert pe.signature(body) == name


def test_a_draft_signed_with_a_name_never_given_fails_and_an_unsigned_one_passes():
    case = _case("nobody-draft")
    signed = {"to": "kim@fabrikam.example", "subject": "Re: Saturday photos",
              "body": "Hi Kim,\nTonight.\nBest,\nAlex"}
    assert "the draft is signed 'Alex'" in _problems(case, drafts=[signed])
    assert not _problems(case, drafts=[{**signed, "body": "Hi Kim,\nTonight.\nBest"}])
    assert _problems(case) == ["no draft was made"]


def test_a_draft_with_a_placeholder_for_a_name_fails():
    """"[Your Name]" is a name Steve did not have, left for the user to
    notice before they send it."""
    draft = {"body": "Hi Kim,\nTonight.\nBest,\n[Your Name]"}
    assert any("placeholder" in p for p in _problems(_case("nobody-draft"), drafts=[draft]))


def test_maya_may_sign_as_herself():
    draft = {"body": "Hi Owen,\nThursday afternoon.\nThanks,\nMaya"}
    assert not _problems(_case("maya-draft"), drafts=[draft])


def test_school_words_fail_for_a_user_who_never_mentioned_school_and_pass_for_a_student():
    said = "Tomorrow is light: no classes, just the dentist at 10."
    assert any("assumed school" in p for p in _problems(_case("nobody-day"), said))
    assert not any("assumed school" in p for p in _problems(_case("kenji-bio"), said))


def test_the_words_that_assume_nothing_are_not_read_as_a_life():
    """"Of course", "does Thursday work" and "your mail client" are said to
    anyone. Read as school or a job, every polite reply would fail."""
    said = ("Of course. Does Thursday work for you? It is in your mail client's Drafts. "
            "Tomorrow: the dentist at 10.")
    assert _problems(_case("nobody-day"), said) == []


def test_a_job_is_assumed_only_when_no_occupation_was_given():
    said = "Your team stand-up is at 10."
    assert any("assumed work" in p for p in _problems(_case("nobody-who"), said))
    assert not any("assumed work" in p for p in _problems(_case("maya-bio"), said + " Maya, she"))


def test_a_dentists_office_is_not_the_users_but_heading_to_the_office_is():
    """On 15 October nobody-a-day failed for "the office asks you to reply
    C to confirm", which was the dentist's."""
    dentist = "Dentist at 10; the office asks you to reply C to confirm."
    assert _problems(_case("nobody-who"), dentist) == []
    for said in ("Leave by 8 to get to the office.", "Your office called."):
        assert any("assumed work" in p for p in _problems(_case("nobody-who"), said)), said


def test_a_relationship_never_given_fails():
    said = "Your friend Kim asked for the photos."
    assert any("relationship" in p for p in _problems(_case("nobody-draft"), said,
                                                      drafts=[{"body": "Tonight."}]))


def test_a_town_the_user_never_gave_fails_and_their_own_passes():
    assert any("Boston" in p or "boston" in p
               for p in _problems(_case("nobody-weather"), "Where? Boston is wet tomorrow."))
    assert not any("place" in p for p in _problems(_case("maya-weather"),
                                                   "Pittsburgh: rain, take an umbrella.",
                                                   tools=["weather"]))


def test_a_term_on_the_denylist_fails_without_the_rule_naming_it(monkeypatch):
    """The check reports a count, never the term: its report is a file in
    the data folder, and the terms are what the repo must not hold."""
    from devtools import denylist

    monkeypatch.setattr(denylist, "load", lambda *a: frozenset({denylist.digest("Port Ellery")}))
    problems = _problems(_case("nobody-who"), "You live in port-ellery, I think.")
    assert problems == ["said 1 denylisted terms"]


def test_he_fails_where_the_user_gave_no_pronouns():
    assert any("called the user he" in p
               for p in _problems(_case("nobody-bio"), "He is a keen gardener."))
    assert not _problems(_case("nobody-bio"), "They are a keen gardener.")


@pytest.mark.parametrize(("said", "fails"), [
    ("Ray asks whether Thursday dinner at 7 is still on so he can book.", False),
    ("Kim Hale asked for the photos; he wants them tonight.", False),
    ("He has the dentist at 10.", True),
    ("Ray wrote about Thursday. He should answer today.", True),
])
def test_he_said_of_someone_the_day_names_is_not_said_of_the_user(said, fails):
    """On 13 October the day's briefing said Ray "can book", and the case
    failed as calling the user he. A pronoun in a sentence that names no
    one is still the user's."""
    problems = _problems(_case("nobody-a-day"), said)

    assert any("called the user he" in p for p in problems) is fails


@pytest.mark.parametrize(("text", "minutes"), [
    ("at 14:00", {840}),
    ("at 2 pm", {840}),
    ("at 2:00pm", {840}),
    ("at 2:00", {120, 840}),
    ("at 9.30 a.m.", {570}),
    ("in 2 hours, 80% rain, 12°", set()),
    ("from 2 to 4 pm", {840, 960}),
    ("from 2:30–4 p.m.", {150, 870, 960}),
    ("from 11 to 1 pm", {660, 780}),
])
def test_clock_times_are_read_in_whatever_form_a_reply_gives_them(text, minutes):
    """A bare "2:00" may be either half of the day; "2 hours" and "80%" are
    no time at all. On 13 October "from 2 to 4 pm", right for a 14:00 study
    group, failed as not saying 14:00: a range's start takes its end's
    half of the day."""
    assert pe.times_in(text) == minutes


def test_the_study_group_must_be_given_in_lisbons_hours_not_the_machines():
    case = _case("kenji-time")
    assert not _problems(case, "Your study group is at 2 pm.")
    assert "did not say 14:00" in _problems(case, "Your study group is at 9 am.")


def test_the_time_now_must_be_the_users_zones():
    case = _case("kenji-now")
    asked = dt.datetime(2026, 10, 12, 13, 0, tzinfo=dt.UTC)  # 14:00 in Lisbon, 09:00 in New York
    notes = {"asked_at": asked.isoformat()}
    assert not _problems(case, "It's 14:01.", notes=notes)
    assert _problems(case, "It's 9:01 am.", notes=notes) == [
        "did not give the time in Europe/Lisbon"]


def test_unknown_names_are_found_mid_sentence_and_sentence_starts_are_not_names():
    day = _case("nobody-a-day")
    assert not _problems(day, "Kim Hale asked for the photos. Lunch with Kim at 12:30.")
    problems = _problems(day, "Kim asked whether Jordan can come to the Riverside Dental visit.")
    assert any("Jordan" in p for p in problems)


def test_a_date_shortened_is_not_a_name_and_the_same_word_alone_may_be():
    """On 15 October nobody-a-day failed for "your calendar shows Sunday,
    Oct. 18": the day's own date, read as a name it did not hold."""
    day = _case("nobody-a-day")
    assert not _problems(day, "Your calendar shows Sunday, Oct. 18 at 7 p.m., and Sat 17 is free.")
    assert any("Jan" in p for p in _problems(day, "Kim asked whether Jan can come along."))


def test_the_world_answers_the_geocoder_and_the_forecast_as_open_meteo_would():
    from agent.attention import fetch, trips
    from agent.attention.sources import weather

    place, country = trips._parse(pe.feeds(trips.GEOCODE, {"name": "Lisbon"}), "Lisbon")
    assert (place.name, place.timezone, country) == ("Lisbon, Portugal", "Europe/Lisbon", "PT")
    assert pe.feeds(trips.GEOCODE, {"name": "Atlantis"}) == {"results": []}
    days = weather.parse_forecast(pe.forecast_payload({"timezone": place.timezone,
                                                       "forecast_days": 3}))
    assert [d.rain_chance for d in days] == [10, 80, 20], "tomorrow is the wet day"
    assert len(days[1].hours) == 24
    with pytest.raises(fetch.FetchError, match="not in the persona eval's world"):
        pe.feeds("https://example.org/feed")


def test_a_meeting_that_just_ended_is_found_and_says_whether_it_recurs(monkeypatch):
    """Found by the product's own reader (`calendars.ended`), from the
    world's calendar as its provider lists it."""
    from agent import calendars
    from tests.gate import slotfake

    monkeypatch.setattr("core.clock.local_zone", lambda: ZoneInfo("Europe/Lisbon"))
    world = ae.World({"world": {"events": pe.ENDED + [{"id": "e-later", "summary": "Later",
                                                         "in_days": 1}]}}, ae.Recorder())
    listed = world.providers["calendar"].events(world.now - dt.timedelta(days=1),
                                                world.now + dt.timedelta(days=2), limit=50)
    slotfake.plug(monkeypatch, "calendar", google=slotfake.Calendar(*listed))

    ended = calendars.ended(None)["events"]

    assert [e["summary"] for e in ended] == ["Meet Kim Hale about the garden plot",
                                             "Weekly check-in"]
    assert [e["recurring"] for e in ended] == [False, True]


def test_the_attention_cases_run_outside_quiet_hours_and_keep_the_profiles_knobs(
        tmp_path, monkeypatch):
    """What the loop would say is held in quiet hours, so a case run in the
    evening would score silence. The profile's own quiet hours stay for
    the case that tests them."""
    import json

    applied = []
    monkeypatch.setattr("agent.worker.control.apply_knobs", applied.append)
    pe._settle(_case("nobody-follow-every"), tmp_path)
    written = json.loads((tmp_path / "knobs.json").read_text(encoding="utf-8"))
    assert written["FOLLOW_UP_AFTER"] == "every" and applied == [tmp_path / "knobs.json"]
    start = dt.time.fromisoformat(written["QUIET_FROM"])
    now = dt.datetime.now().time()
    from core import call_policy

    assert not call_policy.is_quiet(now, start, dt.time.fromisoformat(written["QUIET_UNTIL"]))

    pe._settle(_case("maya-quiet"), tmp_path)
    written = json.loads((tmp_path / "knobs.json").read_text(encoding="utf-8"))
    assert (written["QUIET_FROM"], written["QUIET_UNTIL"]) == ("22:00", "07:30")


@pytest.fixture
def fenced(tmp_path, monkeypatch):
    """The eval's fences, undone after the test, and no panel applied: the
    knobs are module state a test would leave behind."""
    pe._fence(tmp_path, monkeypatch)
    monkeypatch.setattr("agent.worker.control.apply_knobs", lambda path=None: {})
    return tmp_path


def _script(monkeypatch, calls: list[tuple[str, dict]], said: str):
    """A model that makes `calls` in one request, then says `said`."""
    from agent import loop, toolset
    from agent.turn import Deps

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        answered = any(isinstance(p, ToolReturnPart)
                       for m in messages for p in getattr(m, "parts", []))
        if calls and not answered:
            return ModelResponse(parts=[ToolCallPart(name, args) for name, args in calls])
        return ModelResponse(parts=[TextPart(said)])

    def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
        from pydantic_ai import Agent

        agent = Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")
        toolset.register_tools(agent, allow=allow, lead=lead)
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)


def test_its_fences_pin_the_zone_and_the_attention_sources_read_the_world(
        fenced, monkeypatch):
    """The mail source reads the world as it reads any account, list
    headers and all; a source left unanswered would fail, and the morning
    briefing then report a failure the user would never have had."""
    from agent import zones
    from agent.attention.sources import mail
    from core import db

    pe._PINNED["zone"] = ZoneInfo("Europe/Lisbon")
    assert str(zones.local_zone()) == "Europe/Lisbon"
    monkeypatch.setitem(ae._CURRENT, "world", ae.World({"world": {"mail": [
        pe._mail("m-sale", "Tailspin Outdoor <news@tailspin.example>", "Autumn sale",
                 "Our autumn sale starts today.", days_ago=0.2, listed=True)]}},
        ae.Recorder()))
    conn = db.open_store(fenced / "agent.db")
    try:
        [account] = connectors.accounts(conn, "mail")
        [seen] = mail.watch(conn, account)
    finally:
        conn.close()

    assert seen.ref["message_id"] == ids.make("mail", ae.WORLD_KEY, "m-sale")
    assert seen.ref["headers"]["list_id"] == "<news.tailspin.example>"


async def test_a_draft_signed_with_a_strangers_name_is_caught_through_the_real_loop(
        fenced, monkeypatch):
    """The whole path: the profile seeded, the turn played by the real
    conversation loop, the draft recorded by the yardstick's executor
    fence, and the rule reading it."""
    _script(monkeypatch, [("draft_email", {
        "to": "kim@fabrikam.example", "subject": "Re: Saturday photos",
        "body": "Hi Kim,\n\nI'll send the photos tonight.\n\nBest,\nAlex",
        "reply_to_message_id": ids.make("mail", ae.WORLD_KEY, "m-kim")})],
        said="Drafted it; it's in your Drafts.")

    row, _ = await pe.run_case(_case("nobody-draft"), fenced)

    assert row["error"] is None
    assert ("draft_email", "free") in [(e["tool"], e["outcome"]) for e in row["effects"]]
    assert row["passed"] is False and row["problems"] == ["the draft is signed 'Alex'"]


async def test_a_profile_is_seeded_from_the_users_own_words(fenced, monkeypatch):
    """Maya's name, pronouns and town reach the turn as her profile, which
    the weather and every prompt read."""
    from agent import facts

    _script(monkeypatch, [], said="Hello Maya.")
    seen = {}

    real = pe._turn

    async def turn(case, conn, s):
        seen["identity"] = facts.identity(conn)
        await real(case, conn, s)

    monkeypatch.setitem(pe.PLAYS, "turn", turn)
    row, _ = await pe.run_case(_case("maya-bio"), fenced)
    assert seen["identity"]["pronouns"] == "she/her"
    assert seen["identity"]["home_city"] == "Pittsburgh"
    assert row["error"] is None and row["said"] == "Hello Maya."


# -- A day as each kind of account (05-connectors.md, item 16) ----------------


def test_each_kind_of_account_is_played_with_what_its_own_providers_declare(tmp_path):
    """The cards a kind's day asks measure that kind only if the world plays
    it as its providers are. A table that gave Outlook.com conditional
    writes would count none of the cards its days really ask. Each set is
    read from the provider itself, built on its fake as connectors builds
    one; Google's and classic Outlook's declare theirs on the class."""
    from core.engines import code
    from tests.fakes import caldav, mailrig
    from tests.fakes import mcp as fake_mcp

    found = {}
    for module_id in ("google_cloud", "outlook_classic"):
        made = mailrig.module(module_id)
        found[module_id] = {
            klass: getattr(code._code(made), made.specs[klass].settings["provider"]).capabilities
            for klass in made.classes}
    gmail = mailrig.module("gmail_password")
    mail, cal, outlook = mailrig.rig(), caldav.rig(), fake_mcp.rig(tmp_path)
    try:
        found["gmail_password"] = {"mail": mail.provider(gmail).capabilities,
                                   "calendar": cal.provider(gmail).capabilities}
        found["outlook_mcp"] = {klass: outlook.tested(klass).capabilities
                                for klass in ("mail", "calendar")}
    finally:
        outlook.close()
        cal.close()
        mail.close()

    assert found == world.KINDS


def test_a_world_played_as_a_kind_keeps_only_what_both_can_do():
    """Outlook.com's calendar has no conditional writes and it has no files
    class; the world played as it must lack both, or its day asks no card
    that a real Outlook.com day would. Played as no kind, the world is as
    it was."""
    from steve_sdk import classes

    outlook = ae.World({}, ae.Recorder(), kind="outlook_mcp")
    assert set(outlook.providers) == {"mail", "calendar"}
    assert outlook.providers["calendar"].capabilities == {
        classes.CREATE, classes.UPDATE, classes.DELETE}
    assert not outlook.providers["calendar"].has(classes.CONDITIONAL)

    plain = ae.World({}, ae.Recorder())
    assert set(plain.providers) == {"mail", "calendar", "files", "tasks"}
    assert plain.providers["calendar"].has(classes.CONDITIONAL)


@pytest.mark.parametrize(("kind", "outcome", "cards"), [
    ("google_cloud", "free", 0),
    ("outlook_mcp", "card", 1),
])
async def test_an_update_is_a_counted_card_only_on_a_calendar_without_conditions(
        fenced, monkeypatch, kind, outcome, cards):
    """The whole path the per-kind day counts on: the same rename of the
    user's own event, played through the real loop, the slot, consent and
    the write path. On Google's calendar it is made on condition and needs
    no card; on Outlook.com's it cannot be, and Jev (unreachable here)
    leaves the card standing. The row counts it."""
    case = pe.Case("a-rename", pe.NOBODY, "turn", "Call tomorrow's run a long run.",
                   {"events": [{"id": "e-run", "summary": "Run", "in_days": 1,
                                "at": "07:00", "minutes": 30}]})
    _script(monkeypatch, [("update_event", {
        "event_id": ids.make("calendar", ae.WORLD_KEY, "e-run"), "summary": "Long run"})],
        said="Renamed it.")

    row, _ = await pe.run_case(case, fenced, 1, kind)

    assert row["error"] is None
    assert [(e["tool"], e["outcome"]) for e in row["effects"]] == [("update_event", outcome)]
    assert row["kind"] == kind and row["cards"] == cards


def test_a_kinds_day_fails_only_past_three_more_cards_than_the_gmail_days():
    """The exit criterion, read on the rows: each attempt's days are
    measured against that attempt's Gmail day, three more cards pass and
    four fail, and a day with no Gmail day to measure against is said,
    not passed."""
    def day(kind, cards, attempt=1, case="a-day"):
        return {"id": case, "attempt": attempt, "kind": kind, "cards": cards}

    assert pe.card_problems([day("google_cloud", 2), day("outlook_mcp", 5),
                             day("gmail_password", 1), {"id": "other", "attempt": 1}]) == []
    assert pe.card_problems([day("google_cloud", 2), day("outlook_mcp", 6),
                             day("google_cloud", 4, attempt=2), day("outlook_mcp", 6, attempt=2)]
                            ) == ["a-day (1): outlook_mcp asked 6 cards to google_cloud's 2"]
    assert pe.card_problems([day("outlook_mcp", 0)]) == [
        "a-day (1): no google_cloud day to measure against"]


def test_nobody_a_day_is_played_as_every_kind_of_account():
    """The day is the measure the exit criterion names: played as each
    kind, Gmail's among them."""
    assert _case("nobody-a-day").kinds == tuple(world.KINDS)
    assert world.BASELINE in world.KINDS


@pytest.mark.parametrize(("kind", "cards"), [("google_cloud", 1), ("outlook_mcp", 2)])
async def test_nobody_a_day_asks_a_move_and_a_delete_so_its_cards_differ_by_kind(
        fenced, monkeypatch, kind, cards):
    """The criterion is that Outlook.com's day asks at most three more cards
    than Gmail's. A day that asked for no write made no card on any kind,
    and 0 is at most 0 + 3 whatever Outlook.com does (the review of steps 5
    and 6, 15 October). The user's own asks through the day include moving
    an event and deleting one: Google's calendar moves on condition with no
    card and Outlook.com's cannot, and a delete is a card on both. The
    model here does what each ask names, and nothing else."""
    from agent import loop, toolset
    from agent.turn import Deps

    async def nothing(*args, **kwargs):
        return None

    tomorrow = (dt.datetime.now(ZoneInfo(pe.NOBODY.zone)) + dt.timedelta(days=1)).date()
    asked_for = {
        "book club": ("update_event", {"event_id": ids.make("calendar", ae.WORLD_KEY, "e-book"),
                                       "start": f"{tomorrow}T20:00", "from_message_id": "none"}),
        "dry cleaning": ("delete_event", {
            "event_id": ids.make("calendar", ae.WORLD_KEY, "e-cleaning")})}

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        if isinstance(messages[-1].parts[-1], ToolReturnPart):
            return ModelResponse(parts=[TextPart("Done.")])
        ask = next(p.content for p in reversed(messages[-1].parts)
                   if isinstance(p, UserPromptPart)).lower()
        calls = [ToolCallPart(*call) for words, call in asked_for.items() if words in ask]
        return ModelResponse(parts=calls or [TextPart("Done.")])

    def build_agent(role, **kw):
        from pydantic_ai import Agent

        agent = Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")
        toolset.register_tools(agent, allow=kw.get("allow"), lead=kw.get("lead", False))
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)
    monkeypatch.setattr(pe, "_attention", nothing)
    monkeypatch.setattr(pe, "_briefing", nothing)

    row, _ = await pe.run_case(_case("nobody-a-day"), fenced, 1, kind)

    assert row["error"] is None
    assert sorted(e["tool"] for e in row["effects"]) == ["delete_event", "update_event"]
    assert row["cards"] == cards
