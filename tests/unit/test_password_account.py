"""A password account, through the agent: searched with the others, watched
for what arrives, and written only as drafts that go on the user's yes.

27 September 2026: a confirmation the user asked about was in their own
domain's mailbox, which the agent could not see, and the answer was "I
couldn't find it". That mailbox was then connected by a client of its own,
with drafts and replies from that address, and "never send without explicit
confirmation". Step 5's item 6 replaced that client with core's IMAP engine,
for any mailbox with a password; these are its tests, carried over, on an
account added as the app adds one, against real servers on loopback
(tests/fakes/mailrig.py). The engine itself is tested in
tests/unit/test_imap_engine.py.
"""

from __future__ import annotations

import re

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import actions, connectors, loop, toolset
from agent import approvals as ap
from agent.loop import Conversation
from agent.turn import Deps
from core import db
from core.engines import imap as engine
from steve_sdk.classes import Draft
from tests import persona
from tests.fakes import mailrig

ME = persona.OWN_DOMAIN_EMAIL
GARDEN = "<plots-14@greenway-garden.example>"


@pytest.fixture
def home(monkeypatch, memories_repo):
    """A mailbox of the user's own domain, added as an account named "home",
    and a conversation whose model makes one call."""
    monkeypatch.setattr(engine, "SENT_WAIT_S", 0.3)
    rig = mailrig.rig(user=ME)
    rig.world.seed("mail", [
        {"sender": "Greenway Garden <committee@greenway-garden.example>", "to": ME,
         "subject": "Plot assignments for spring", "message_id": GARDEN, "unread": True,
         "body": "Your plot is number 14. The first work day is on Saturday at 10."},
        {"role": "sent", "sender": ME, "to": "Dana Ortiz <dana@example.com>",
         "subject": "The soup recipe", "body": "Here is the soup recipe I promised."}])
    monkeypatch.setattr(connectors, "NETWORK", rig.network)
    # The data folder's own store: a tool reads the accounts on a connection of
    # its own (`agent/offloop.py`), which opens that one.
    conn = db.open_store()
    key = mailrig.add_account(conn, rig, address=ME, name="home")
    [account] = [a for a in connectors.accounts(conn, "mail") if a.key == key]
    cards: list[str] = []

    async def notify(text, **_):
        cards.append(text)
        return True

    monkeypatch.setattr(ap, "_notify", notify)
    monkeypatch.setattr(ap, "_execute_tool", actions.perform)
    monkeypatch.setattr(ap, "HOLD_SECONDS", 0)

    def run(tool: str, call: dict) -> Conversation:
        async def respond(messages, info: AgentInfo) -> ModelResponse:
            if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
                return ModelResponse(parts=[TextPart("It is in your home Drafts.")])
            return ModelResponse(parts=[ToolCallPart(tool, call)])

        def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                        prompt=None, on=None):
            from pydantic_ai import Agent

            built = Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")
            toolset.register_tools(built, allow=allow)
            return built

        monkeypatch.setattr(loop, "build_agent", build_agent)
        return Conversation(conn, channel="signal")

    yield {"rig": rig, "conn": conn, "account": account, "cards": cards, "run": run}
    conn.close()
    rig.close()


def _drafts(home) -> list[bytes]:
    return [s.raw for s in home["rig"].world.role("\\Drafts").items]


def _sent(home) -> list:
    return home["rig"].smtp.sent


NEW = {"to": "dana@example.com", "subject": "Friday", "body": "Still on for Friday?",
       "account": "home"}


async def test_an_email_from_a_password_account_waits_in_its_drafts_for_their_yes(home):
    await home["run"]("send_email", NEW).send("email Dana from my own address")
    await ap.asked()  # the card goes out as the turn ends

    assert _sent(home) == [], "it sent before they said yes"
    assert len(_drafts(home)) == 1
    [waiting] = ap.pending(home["conn"])
    assert waiting.tool == "send_draft"
    [card] = home["cards"]
    assert "from your home account" in card and "home Drafts" in card


async def test_only_their_yes_sends_it_and_only_once(home):
    await home["run"]("send_email", NEW).send("email Dana")
    [waiting] = ap.pending(home["conn"])

    ap.decide(home["conn"], waiting.id, approve=True, by="signal")
    await ap.execute(home["conn"], waiting.id, hold=False)
    await ap.execute(home["conn"], waiting.id, hold=False)

    assert [s.recipients for s in _sent(home)] == [["dana@example.com"]]


async def test_the_answer_says_whom_the_server_refused_when_the_mail_went_to_the_rest(home):
    """An SMTP server takes a mail for the recipients it knows and refuses
    the others, and the mail still goes. The engine kept who was refused;
    the tool's answer dropped it, so the user heard the mail had gone to
    everyone the card named (the review of steps 5 and 6)."""
    home["rig"].smtp.refuse_to = {"gone@example.com"}
    await home["run"]("send_email", {**NEW, "to": "dana@example.com, gone@example.com"}).send(
        "email Dana and the old address")
    [waiting] = ap.pending(home["conn"])

    ap.decide(home["conn"], waiting.id, approve=True, by="signal")
    done = await ap.execute(home["conn"], waiting.id, hold=False)

    assert [s.recipients for s in _sent(home)] == [["dana@example.com"]]
    assert done["ok"] and done["result"]["refused"] == "gone@example.com"


async def test_a_send_the_server_refuses_is_said_in_the_servers_words(home):
    """The user is told why it did not go; Python's tuple of a code and
    bytes is not words."""
    home["rig"].smtp.refuse = "554 5.7.1 Message rejected as spam"
    await home["run"]("send_email", NEW).send("email Dana")
    [waiting] = ap.pending(home["conn"])

    ap.decide(home["conn"], waiting.id, approve=True, by="signal")
    done = await ap.execute(home["conn"], waiting.id, hold=False)

    assert not done["ok"] and _sent(home) == []
    assert "554 5.7.1 Message rejected as spam" in done["error"], done
    assert "b'" not in done["error"]


async def test_a_no_sends_nothing(home):
    await home["run"]("send_email", NEW).send("email Dana")
    [waiting] = ap.pending(home["conn"])

    ap.decide(home["conn"], waiting.id, approve=False, by="signal")
    await ap.execute(home["conn"], waiting.id, hold=False)

    assert _sent(home) == []


async def test_a_note_to_themselves_goes_at_once_from_any_account_that_can_send(home):
    """Gmail's free pass for notes to themselves once reached no other
    mailbox. Step 5's plan keeps today's two direct sends "on every account
    that can send": a note to the account's own address is a note."""
    await home["run"]("send_email", {**NEW, "to": ME}).send("email me")

    assert len(_sent(home)) == 1 and ap.pending(home["conn"]) == []


async def test_a_reply_goes_from_the_account_whose_mail_it_answers(home):
    account = home["account"]
    [original] = connectors.blocking(account, lambda mail: mail.search("plot", limit=5))
    call = {"to": "", "subject": "", "body": "I will be there on Saturday.",
            "reply_to_message_id": account.id(original.native_id)}

    await home["run"]("send_email", call).send("reply that I'll be there")

    [draft] = _drafts(home)
    assert f"In-Reply-To: {GARDEN}".encode() in draft
    [waiting] = ap.pending(home["conn"])
    assert waiting.tool == "send_draft"
    assert waiting.args["draft_id"].startswith(f"mail:{account.key}:")


async def test_the_card_for_a_saved_draft_says_whom_it_goes_to(home):
    """A draft written earlier is sent on a card that names the account, the
    recipient and the subject, read from the draft itself: "Send the draft
    <an id>" is not a yes the user can give knowingly."""
    account = home["account"]
    saved = connectors.blocking(account, lambda mail: mail.make_draft(
        Draft(to="dana@example.com", subject="Friday", body="Still on for Friday?")))

    await home["run"]("send_draft", {"draft_id": account.id(saved.native_id)}).send(
        "send that draft")
    await ap.asked()

    assert _sent(home) == [], "it sent before they said yes"
    [card] = home["cards"]
    assert "from your home account" in card
    assert "dana@example.com" in card and "Friday" in card


async def test_search_email_looks_in_the_password_account_too(home):
    await home["run"]("search_email", {"query": "plot"}).send("where is my garden plot?")

    rows = home["conn"].execute(
        "SELECT content FROM messages WHERE role = 'tool' AND tool_name = 'search_email'"
    ).fetchall()
    assert "Plot assignments for spring" in rows[0]["content"]
    assert re.search(r'"account":\s*"home"', rows[0]["content"])


def test_the_attention_loop_reads_its_inbox_and_not_its_sent_mail(home):
    from agent.attention.sources import mail

    seen = mail.watch(home["conn"], home["account"])

    assert [o.title.split(" -- ")[0] for o in seen] == ["Plot assignments for spring"]
    assert seen[0].detail.startswith("home, unread")
