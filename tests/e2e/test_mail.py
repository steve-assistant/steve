"""Mail, end to end: a message read from the server and summarised; a
reply saved to Drafts, shown on a card with its recipient, and sent only
on a yes; a yes to part of a card that asks about two emails.

The mailbox is the world's (tests/fakes/world.py): IMAP and SMTP servers
on this computer, under the names a user types in setup's "Other"
(imap.mail.example:993, smtp.mail.example:465), which the backend reaches
from its own processes through the world's network
(tests/fakes/worldnet.py). The account is added as setup adds it, through
`/control/accounts`, and tested there against the servers. So every step
of a send runs as shipped: the draft written by IMAP, the card bound to
it, the hold, the send by SMTP. The unit tests drive the engine in the
test's process (tests/unit/test_imap_engine.py) and a card's parts a call
at a time (tests/unit/test_grouped_cards.py).
"""

from __future__ import annotations

import email
import email.policy
import json

import pytest

from tests.e2e.backend import Backend, running, until
from tests.fakes.jev import FakeJev
from tests.fakes.openai import FakeOpenAI, Rule
from tests.fakes.world import World, world

ROTA = {"sender": "Iris Moreau <iris@example.com>", "subject": "Allotment rota",
        "body": "Could you take the Saturday watering this week? The hose is by the shed.",
        "message_id": "<rota-1@example.com>"}
CHOIR = {"sender": "Theo Brandt <theo@example.com>", "subject": "Choir on Thursday",
         "body": "Are you coming to choir on Thursday? We start at seven.",
         "message_id": "<choir-1@example.com>"}

ASK_READ = "What did Iris say about the allotment rota?"
SUMMARY = "Iris asks whether you can take the Saturday watering. The hose is by the shed."
ASK_REPLY = "Tell Theo yes, I'll be at choir on Thursday."
ASK_TWO = "Email Iris about the plot fees and Theo about the seed order."
PLOT = {"to": "iris@example.com", "subject": "Plot fees",
        "body": "Hi Iris, I'll pay the plot fees on Saturday.\n\nMaya"}
SEEDS = {"to": "theo@example.com", "subject": "Seed order",
         "body": "Hi Theo, shall we put in the seed order this week?\n\nMaya"}


def first_id(result: str) -> dict:
    """The id of the first message a search found, as the model reads it."""
    return {"message_id": json.loads(result)["messages"][0]["id"]}


def reply_to_choir(result: str) -> dict:
    found = json.loads(result)["messages"][0]["id"]
    return {"to": "theo@example.com", "subject": "Re: Choir on Thursday",
            "body": "Yes, I'll be there on Thursday.\n\nMaya", "reply_to_message_id": found}


RULES = (
    Rule(said=ASK_READ, call="search_email", arguments={"query": "allotment rota"}),
    Rule(after="search_email", result="Allotment rota", call="read_email", arguments=first_id),
    Rule(after="read_email", result="Saturday watering", say=SUMMARY),
    Rule(said=ASK_REPLY, call="search_email", arguments={"query": "choir"}),
    Rule(after="search_email", result="Choir on Thursday", call="send_email",
         arguments=reply_to_choir),
    Rule(said=ASK_TWO, call="send_email", arguments=PLOT),
    Rule(after="send_email", result="Seed order", say="Both wait in your Drafts for your yes."),
    Rule(after="send_email", result="Plot fees", call="send_email", arguments=SEEDS),
    Rule(after="send_email", result="", say="It waits in your Drafts for your yes."),
)


@pytest.fixture(scope="module")
def steve(fenced):
    # The injection screen reads every email as it is unless Jev chooses
    # otherwise (core/screen.py); these are plain notes from friends. The
    # screen's own scenarios are the red team's.
    jev = FakeJev(otherwise=0.1, handling="as_is")
    with (world(fenced / "world", openai=FakeOpenAI(RULES), jev=jev, mail=True) as made,
          running(made, fenced / "data") as backend):
        made.mail.seed("mail", [ROTA, CHOIR])
        added = backend.post("/control/accounts", made.mailbox()).json()
        assert added.get("ok") and added["test"]["ok"], (added, backend.logs())
        yield backend


def sent(made: World, to: str) -> list[email.message.EmailMessage]:
    """What the SMTP server took for `to`."""
    return [email.message_from_bytes(s.raw, policy=email.policy.default)
            for s in list(made.smtp.sent) if to in s.recipients]


def drafts(made: World, subject: str) -> list[email.message.EmailMessage]:
    """The messages in the server's Drafts with this subject."""
    return [stored.message for stored in list(made.mail.role("\\Drafts").items)
            if stored.message["Subject"] == subject]


def action(steve: Backend, note: dict) -> str:
    return next(a["status"] for a in steve.get("/control/activity").json()["items"]
                if a["kind"] == "action" and a["id"] == note["action_id"])


async def test_a_message_is_read_from_the_server_and_summarised(steve):
    """The summary is said only once `read_email` has handed the model the
    message's body, read from the server by IMAP: the script answers the
    body's words, and nothing else carries them to it."""
    said = await steve.chat(ASK_READ)
    assert said["type"] == "reply", steve.logs()
    assert said["text"] == SUMMARY, said


async def test_a_reply_waits_in_drafts_on_a_card_naming_its_recipient_and_goes_only_on_yes(
        steve):
    """Nothing is sent without a yes, and the yes is an informed one: the
    whole reply waits in the server's Drafts, and the card names who it
    reaches and what it is. On the yes, the server takes it for Theo alone,
    threaded under his message."""
    made = steve.world
    said = await steve.chat(ASK_REPLY)
    assert said["type"] == "reply", steve.logs()
    note = steve.card("To: theo@example.com")
    assert "Subject: Re: Choir on Thursday" in note["detail"]
    [draft] = drafts(made, "Re: Choir on Thursday")
    assert "I'll be there on Thursday" in draft.get_body(("plain",)).get_content()
    assert not sent(made, "theo@example.com")

    assert steve.decide(note, True).json()["ok"]

    assert until(lambda: action(steve, note) == "done"), steve.logs()
    [reply] = sent(made, "theo@example.com")
    assert reply["In-Reply-To"] == CHOIR["message_id"]
    assert [s.recipients for s in made.smtp.sent] == [["theo@example.com"]]


async def test_a_yes_to_part_of_a_card_sends_that_part_and_nothing_else(steve):
    """Two emails to one account in one turn make one card, which asks
    about both. A yes to the first alone sends it; the second is never
    sent, and the card's record names it as left out."""
    made = steve.world
    said = await steve.chat(ASK_TWO)
    assert said["type"] == "reply", steve.logs()
    note = steve.card("Subject: Plot fees")
    assert [item.splitlines()[1:3] for item in note["items"]] == [
        ["To: iris@example.com", "Subject: Plot fees"],
        ["To: theo@example.com", "Subject: Seed order"]]

    assert steve.decide(note, True, only=[1]).json()["ok"]

    assert until(lambda: action(steve, note) == "done"), steve.logs()
    assert [m["Subject"] for m in sent(made, "iris@example.com")] == ["Plot fees"]
    assert not [m for m in sent(made, "theo@example.com") if m["Subject"] == "Seed order"]
