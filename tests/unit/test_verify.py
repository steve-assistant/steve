"""An answer claims only what was actually done.

With the judge able to see tool results, one failure left in `grounded`'s low
score (25 September 2026) was plain: "Added both events to your calendar" with
no calendar call.
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import loop, toolset, verify
from agent.loop import Conversation
from agent.turn import Deps
from core import db

# ---------------------------------------------------------------------------
# Claimed effects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("reply", "used", "flagged"), [
    ("I've added the review session to your calendar for Friday.", set(), True),
    ("I've added the review session to your calendar for Friday.", {"create_event"}, False),
    ("Want me to add it to your calendar?", set(), False),
    ("I haven't added it to your calendar yet.", set(), False),
    ("Added to your Tasks: wish Hazel happy birthday.", set(), True),
    ("Added to your Tasks: wish Hazel happy birthday.", {"add_task"}, False),
    ("Reminder set for 7:15 AM Friday.", set(), True),
    ("I marked the doctor's note commitment as done.", set(), True),
    ("You have three meetings on Friday.", set(), False),
    # 27 September 2026: a lookup describing an event, read as a claim.
    ("I found an Outlook confirmation: the Lena Fischer coffee chat was scheduled for"
     " September 24, 3:00–3:20 PM.", set(), False),
    ("Your Tailspin presentation is scheduled for Monday at 7 PM.", set(), False),
    ("It was originally scheduled for Tuesday.", set(), False),
    ("I've scheduled the review session for Friday.", set(), True),
    ("It is now scheduled for Friday.", set(), True),
    ("Both classes were added to your calendar.", set(), True),
    # 9 October 2026: a spoken history of Pittsburgh sent back for correction.
    ("Pennsylvania chartered a college in 1802; it moved to Pittsburgh in 1868.", set(), False),
    ("The company moved to Boston in 2010 and was rescheduled for listing in 2012.", set(),
     False),
    ("Moved your call with Rina to Friday at 3.", set(), True),
    ("I moved it to Thursday.", set(), True),
    ("I've rescheduled for Friday at 3.", set(), True),
    ("Rescheduled the dentist for next week.", set(), True),
    # 8 and 9 October 2026: someone else's sending, each sent back for a
    # correction about an email nobody had mentioned.
    ("LinkedIn turned the browser away and sent it to a sign-in page.", set(), False),
    ("The system sent it again this morning.", set(), False),
    ("I don’t know why the system sent it or which Ridgeview it meant.", set(), False),
    ("I haven’t sent it yet.", set(), False),
    ("I haven’t added it to your calendar yet.", set(), False),
    ("I’ve sent the email to Rina.", set(), True),
    ("Sent it to Leo.", set(), True),
    ("Just sent the reply to Leo.", set(), True),
    ("Done: sent the email.", set(), True),
    ("I've also sent the email to Rina.", set(), True),
    ("Your email was sent.", set(), True),
])
def test_a_claimed_effect_needs_the_tool_that_does_it(reply, used, flagged):
    assert bool(verify.unbacked_claims(reply, used, set(), went_this_turn=set())) is flagged


def test_a_queued_email_is_not_a_sent_one():
    """`send_email` puts a card on their phone; nothing has gone until the user says yes."""
    assert verify.unbacked_claims("Sent! It's on its way to Clara.", {"send_email"},
                                  {"send_email"}, went_this_turn={"draft_email"})
    assert not verify.unbacked_claims(
        "Queued for your approval: tap yes on the card to send it.", {"send_email"},
        {"send_email"}, went_this_turn={"draft_email"})


def test_an_email_that_went_may_be_called_sent():
    """A routine the user approved may email Mom, and it goes at once. Told
    it was only queued, the reply said it was waiting for their yes."""
    assert not verify.unbacked_claims("The email was sent to Mom.", {"send_email"},
                                      {"send_email"}, went_this_turn={"send_email"})


# ---------------------------------------------------------------------------
# In the loop
# ---------------------------------------------------------------------------


@pytest.fixture
def talk(tmp_path, monkeypatch, memories_repo):
    """A conversation whose model answers from a script, one reply a call."""
    conn = db.open_store(tmp_path / "verify.db")
    state = {"script": [], "calls": 0, "seen": []}

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        state["calls"] += 1
        state["seen"].append(messages)
        step = state["script"][min(state["calls"] - 1, len(state["script"]) - 1)]
        if step == "lookup":
            if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
                return ModelResponse(parts=[TextPart(state["after_lookup"])])
            return ModelResponse(parts=[ToolCallPart("my_commitments", {})])
        if step == "send":
            if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
                return ModelResponse(parts=[TextPart(state["after_send"])])
            return ModelResponse(parts=[ToolCallPart(
                "send_email", {"to": "mom@example.org", "subject": "This week",
                               "body": "Hi Mom, a quick note about my week."})])
        if step == "create":
            if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
                return ModelResponse(parts=[TextPart(state["after_create"])])
            return ModelResponse(parts=[ToolCallPart(
                "create_event", {"summary": "Review session", "start": "2026-10-09T15:00"})])
        return ModelResponse(parts=[TextPart(step)])

    def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
        from pydantic_ai import Agent

        agent = Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")
        toolset.register_tools(agent, allow=allow)
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)
    state["open"] = lambda channel="tray": Conversation(conn, channel=channel)
    state["conn"] = conn
    yield state
    conn.close()


async def test_a_claim_nothing_did_is_corrected_once_before_it_goes(talk):
    talk["script"] = ["I've added the review session to your calendar for Friday.",
                      "I haven't added it yet. Want me to put it on your calendar?"]
    convo = talk["open"]()

    reply = await convo.send("There's a review session Friday at 3")

    assert reply.text.startswith("I haven't added it yet")
    stored = talk["conn"].execute(
        "SELECT content FROM messages WHERE role='assistant'").fetchall()
    assert [r["content"] for r in stored] == [reply.text]
    history = " ".join(str(m) for m in convo._history)
    assert "Before that reply goes" not in history, "the correction note stayed in the history"
    assert "I've added the review session" not in history


async def test_something_done_earlier_in_the_conversation_may_be_said(talk):
    """"I added it to your tasks earlier" is true when an earlier turn did."""
    convo = talk["open"]()
    convo._store_message("tool", '{"ok": true, "commitment_id": 4}', tool_name="add_task")
    talk["script"] = ["I added it to your tasks earlier."]

    reply = await convo.send("did you add the Hazel thing?")

    assert talk["calls"] == 1 and reply.text == "I added it to your tasks earlier."


async def test_an_action_that_failed_does_not_back_a_claim(talk, monkeypatch):
    """The duplicate check refuses `create_event`, and "I've added it to your
    calendar" would have gone out anyway: the tool was *called*, and that
    was all this used to ask."""
    from steve_sdk.classes import Event
    from tests.gate import slotfake

    held = slotfake.Calendar(Event(native_id="x", title="Review session",
                                   start=slotfake.at("2026-10-09T15:00"),
                                   end=slotfake.at("2026-10-09T16:00")))
    slotfake.plug(monkeypatch, "calendar", google=held)
    talk["script"] = ["create", "create", "It's already on your calendar, so I didn't add it."]
    talk["after_create"] = "I've added the review session to your calendar."

    reply = await talk["open"]("signal").send("add the review session, Friday 3pm")

    assert talk["calls"] == 3
    assert reply.text.startswith("It's already on your calendar")
    assert held.writes == [], "a duplicate must not be created"


@pytest.fixture
def mail(talk, monkeypatch):
    """Gmail, added and faked, and the monitor's decision for each action,
    set by the test. Until step 5's item 10 the mail tools found today's
    Outlook stand-in instead."""
    from agent import actions
    from core import monitor
    from tests.fakes import google as fake_google

    fake_google.signed_in(talk["conn"], classes=["mail"])
    decide: dict[str, str] = {}
    done: list[str] = []

    async def gate(ctx, tool, args):
        return monitor.Gate(decide.get(tool, "free"), monitor.classify(tool, args, me=None))

    async def perform(tool, args):
        done.append(tool)
        return {"ok": True, "draft_id": "d1"} if tool == "draft_email" else {"ok": True,
                                                                              "id": "m1"}

    monkeypatch.setattr(actions, "_gate", gate)
    monkeypatch.setattr(actions, "perform", perform)
    return decide, done


async def test_an_email_an_approved_routine_sent_is_told_as_sent(talk, mail):
    """Their routine may email Mom without asking, so the email goes at once
    and "sent" is the truth. The check took every send as a card, and its
    correction had the reply say the email "hasn't been sent; it's waiting
    for your approval" -- the user then thinks Mom has heard nothing."""
    decide, done = mail
    talk["script"] = ["send", "send", "The email hasn't been sent; it's waiting for you."]
    talk["after_send"] = "The email was sent to Mom."

    reply = await talk["open"]().send("send Mom this week's note")

    assert done == ["send_email"]
    assert talk["calls"] == 2 and reply.text == "The email was sent to Mom."


async def test_an_email_waiting_for_their_yes_is_not_told_as_sent(talk, mail):
    """The usual send: the draft is saved, and a card asks them to send it.
    Nothing has gone, so "sent" is corrected."""
    decide, done = mail
    decide["send_email"] = decide["send_draft"] = "card"
    talk["script"] = ["send", "send", "It's waiting for your yes on the card."]
    talk["after_send"] = "The email was sent to Mom."

    reply = await talk["open"]().send("send Mom this week's note")

    assert done == ["draft_email"]
    assert talk["calls"] == 3 and reply.text == "It's waiting for your yes on the card."


async def test_there_is_only_ever_one_correction(talk):
    talk["script"] = ["I've added it to your calendar."] * 3
    reply = await talk["open"]().send("add it")

    assert talk["calls"] == 2 and reply.text == "I've added it to your calendar."


async def test_an_answer_that_is_fine_is_not_touched(talk):
    talk["script"] = ["lookup"]
    talk["after_lookup"] = "You have no open commitments."

    reply = await talk["open"]("signal").send("what do I owe people?")

    assert talk["calls"] == 2 and reply.text == "You have no open commitments."
