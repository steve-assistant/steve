"""Outlook mail goes out from Outlook, and only on the user's yes.

An Outlook email is saved to the user's Outlook Drafts first -- the card's
preview is short, and the draft is where they can read all of it -- and
the card asks them to send that draft.

The model writes "outlook" because the mail section names the account so.
The sending tools called it by another name until October 2026 and
translated the name the prompt gave; these tests pass the prompt's name and
nothing else. Since step 5 the account is one on the slot, here a mailbox
in a dict (tests/gate/mailfake.py); Outlook's own COM is tested in
tests/unit/test_outlook.py and the golden record.
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import actions, loop, toolset
from agent import approvals as ap
from agent.attention import prepare
from agent.loop import Conversation
from agent.turn import Deps
from core import db
from steve_sdk.classes import Message
from tests import persona
from tests.gate.mailfake import Mailbox, connect

RINAS = Message(native_id="abc", thread="conv-1", sender="Rina Sato <rina.sato@contoso.example>",
                to=persona.WORK_EMAIL, subject="DES 203", body="Which section are you in?")


@pytest.fixture
def on_outlook(tmp_path, monkeypatch, memories_repo):
    """A conversation whose model sends one email, and two accounts: Gmail
    and Outlook."""
    conn = db.open_store(tmp_path / "outlook.db")
    boxes = {"gmail": Mailbox(persona.EMAIL), "outlook": Mailbox(persona.WORK_EMAIL, RINAS)}
    state = {"cards": [], "boxes": boxes, "accounts": connect(monkeypatch, **boxes)}

    async def notify(text, **_):
        state["cards"].append(text)
        return True

    monkeypatch.setattr(ap, "_notify", notify)
    monkeypatch.setattr(ap, "_execute_tool", actions.perform)
    monkeypatch.setattr(ap, "HOLD_SECONDS", 0)

    def model(call: dict):
        async def respond(messages, info: AgentInfo) -> ModelResponse:
            if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
                return ModelResponse(parts=[TextPart("It is in your Outlook Drafts.")])
            return ModelResponse(parts=[ToolCallPart("send_email", call)])

        def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                        prompt=None, on=None):
            from pydantic_ai import Agent

            agent = Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")
            toolset.register_tools(agent, allow=allow)
            return agent

        monkeypatch.setattr(loop, "build_agent", build_agent)
        return Conversation(conn, channel="signal")

    state["model"] = model
    state["conn"] = conn
    yield state
    conn.close()


NEW = {"to": "rina.sato@contoso.example", "subject": "DES 203", "account": "outlook",
       "body": f"Hi Rina,\n\nI'm in the Thursday section.\n\nThanks, {persona.FIRST}"}


async def test_an_outlook_email_is_saved_to_their_drafts_and_waits_for_their_yes(on_outlook):
    await on_outlook["model"](NEW).send("email Rina from work that I'm in the Thursday section")
    await ap.asked()  # the card goes out as the turn ends

    outlook, gmail = on_outlook["boxes"]["outlook"], on_outlook["boxes"]["gmail"]
    assert [d.to for d in outlook.drafts()] == ["rina.sato@contoso.example"]
    assert outlook.sent == [] and gmail.items == {}, "it sent before they said yes"
    [waiting] = ap.pending(on_outlook["conn"])
    assert waiting.tool == "send_draft"
    [card] = on_outlook["cards"]
    # The account as the user named it, as every account is named on a card.
    assert "from your outlook account" in card and "outlook Drafts" in card
    assert "rina.sato@contoso.example" in card and "DES 203" in card


async def test_only_their_yes_sends_it_and_only_once(on_outlook):
    await on_outlook["model"](NEW).send("email Rina from work")
    [waiting] = ap.pending(on_outlook["conn"])

    ap.decide(on_outlook["conn"], waiting.id, approve=True, by="signal")
    await ap.execute(on_outlook["conn"], waiting.id, hold=False)
    await ap.execute(on_outlook["conn"], waiting.id, hold=False)

    outlook = on_outlook["boxes"]["outlook"]
    assert [m.to for m in outlook.sent] == ["rina.sato@contoso.example"]
    assert outlook.drafts() == []


async def test_a_no_sends_nothing(on_outlook):
    await on_outlook["model"](NEW).send("email Rina from work")
    [waiting] = ap.pending(on_outlook["conn"])

    ap.decide(on_outlook["conn"], waiting.id, approve=False, by="signal")
    await ap.execute(on_outlook["conn"], waiting.id, hold=False)

    assert on_outlook["boxes"]["outlook"].sent == []


async def test_a_note_to_themselves_from_outlook_goes_at_once_as_from_gmail(on_outlook):
    """Until step 5 the self-addressed free pass was for Gmail notes alone,
    since only Gmail's address was known. Step 5's plan keeps today's two
    direct sends "on every account that can send": a note to the user's own
    address at that account is a note, not correspondence."""
    await on_outlook["model"]({**NEW, "to": persona.WORK_EMAIL}).send("email me from work")

    outlook = on_outlook["boxes"]["outlook"]
    assert [m.to for m in outlook.sent] == [persona.WORK_EMAIL]
    assert ap.pending(on_outlook["conn"]) == []


async def test_a_reply_to_outlook_mail_goes_from_outlook(on_outlook):
    accounts = on_outlook["accounts"]
    call = {"to": "rina.sato@contoso.example", "subject": "RE: DES 203", "body": "Thursday!",
            "reply_to_message_id": accounts["outlook"].id("abc")}

    await on_outlook["model"](call).send("reply Thursday")

    outlook = on_outlook["boxes"]["outlook"]
    assert [d.reply_to for d in outlook.drafted] == ["abc"]
    assert on_outlook["boxes"]["gmail"].items == {}
    [waiting] = ap.pending(on_outlook["conn"])
    [draft] = outlook.drafts()
    assert waiting.tool == "send_draft"
    assert waiting.args["draft_id"] == accounts["outlook"].id(draft.native_id)


def test_nothing_that_runs_on_its_own_can_send():
    """The attention loop's doer never holds a send tool."""
    sends = {"send_email", "send_draft"}

    assert not (prepare.PREPARE_TOOLS & sends)
