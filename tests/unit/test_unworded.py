"""No model reads the word of a card still waiting (agent/provider.Unworded).

A yes naming a card's word approves that card. A model that knew a waiting
card's word could write a card of its own around it, and the user's yes to
that would approve the real one. Step 11's third review took the word out
of what the model is told of a card it asked for; its fourth found the card
itself written into the conversation, "Reply YES MAPLE" and all, and read
by the model on its next turn. These tests send cards through the paths a
worker sends them by and look at what the model is given next, whole.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from functools import partial

import pytest
from pydantic_ai.messages import (
    InstructionPart,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import approvals, cardwords, loop, provider, runtime
from agent.models import MODELS
from agent.worker import answering, links
from core import db, secrets, totp
from core.consent import classify

EMAIL = {"to": "hugo@example.com", "subject": "Lunch", "body": "Thursday works."}


class _Channel:
    """Signal, as the outbox duty sends to it: whatever it is given."""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, outgoing) -> bool:
        self.sent.append(outgoing.text)
        return True


@dataclass
class _Bridge:
    channel: _Channel = field(default_factory=_Channel)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """A worker's store, Signal linked, and cards asked through the outbox."""
    conn = db.open_store(tmp_path / "worker.db")
    monkeypatch.setattr(runtime, "conn", conn)
    monkeypatch.setattr(runtime, "signal", _Bridge())
    monkeypatch.setattr(runtime, "telegram", None)
    monkeypatch.setattr(runtime, "listeners", set())
    monkeypatch.setattr(approvals, "_notify", partial(links.notify_user, kind="card"))
    yield conn
    conn.close()


async def _card(conn) -> approvals.Action:
    receipt = await approvals.request(conn, tool="send_email", args=EMAIL,
                                      preview="Email to hugo@example.com: Lunch",
                                      consent=classify("send_email", EMAIL))
    await approvals.asked()
    return approvals.get(conn, receipt["action_id"])


def _recorded(conn) -> str:
    """What the conversation keeps of what was sent and said to the user.
    The user's own words are kept as they wrote them, and masked when read."""
    return "\n".join(row[0] or "" for row in conn.execute(
        "SELECT content FROM messages WHERE role != 'user'"))


def _names(text: str, word: str) -> bool:
    """Whether `text` holds `word` as a word: a card's word may be part of
    another ("ember" in "remember"), which is no answer to anything."""
    return re.search(rf"(?<![a-z]){word}(?![a-z])", text, re.IGNORECASE) is not None


def _read(requests) -> str:
    """Everything a model was given: the instructions and every part."""
    said = []
    for messages, instructions in requests:
        said.append(instructions or "")
        for message in messages:
            for part in message.parts:
                said.append(json.dumps(getattr(part, "content", None), default=str))
                said.append(json.dumps(getattr(part, "args", None), default=str))
    return "\n".join(said)


async def test_what_the_model_reads_next_holds_no_waiting_cards_word(
        worker, memories_repo, monkeypatch):
    """A card and a briefing listing it go out on Signal, and into the
    conversation; then the user writes, quoting the word in a message too
    long to be an answer. Neither what the conversation recorded of the
    card and the briefing nor the request the model is sent holds the
    word."""
    action = await _card(worker)
    await links.notify_user("Waiting for you:\n" + cardwords.listed(
        action.token, action.preview.splitlines()[0]))
    assert await links.send_due() == 2
    requests = []

    async def model(messages, info: AgentInfo):
        requests.append((messages, info.instructions))
        return ModelResponse(parts=[TextPart("Booked.")])

    monkeypatch.setattr(loop, "model_for",
                        lambda role: provider.guard(FunctionModel(model), MODELS[role]))
    await answering.voice_reply(
        f"yes {action.token.lower()} and also book us a table for Friday at eight", "signal")

    read = _read(requests)
    assert requests and not _names(read, action.token)
    assert "Email to hugo@example.com" in read and cardwords.PLACEHOLDER in read
    assert not _names(_recorded(worker), action.token)
    # What the phone was sent: the card with its word, the briefing too.
    assert sum(action.token in text for text in runtime.signal.channel.sent) == 2


async def test_a_card_recorded_without_its_word_is_still_the_last_thing_in_the_chat(worker):
    """A bare yes answers the card only while it is the last thing in the
    chat, found there by what the conversation recorded of it
    (`approvals.moved_on_since`)."""
    action = await _card(worker)
    assert await links.send_due() == 1

    said = await answering.intercept_approval("yes", "signal")

    assert said is not None and said.startswith("Going ahead"), said
    assert approvals.get(worker, action.id).status == "approved"


async def test_every_part_of_a_request_is_sent_without_the_word_and_the_history_is_kept(worker):
    """Instructions, the system prompt, the user's words, tools' results
    of any shape, retries, and what the model said before: each without the
    word. The messages given are copies; the conversation is left as it
    was."""
    word = (await _card(worker)).token

    @dataclass
    class Found:
        note: str

    messages = [
        ModelRequest(parts=[SystemPromptPart(f"cards: {word}"),
                            UserPromptPart([f"yes {word.lower()}", "and more"])],
                     instructions=f"Waiting: {word}"),
        ModelResponse(parts=[TextPart(f"Reply YES {word}"),
                             ToolCallPart("look", {"q": word})]),
        ModelRequest(parts=[ToolReturnPart("look", {"cards": [{"word": word}]}, "c1"),
                            ToolReturnPart("look", Found(note=word), "c2"),
                            RetryPromptPart(f"{word} is not a tool", tool_name="look")]),
    ]
    parameters = ModelRequestParameters(
        instruction_parts=[InstructionPart(content=f"say YES {word}")])

    sent, given = provider.unworded(messages, parameters)

    read = _read([(sent, sent[0].instructions + given.instruction_parts[0].content)])
    assert not _names(read, word) and read.count(cardwords.PLACEHOLDER) == 9
    assert word in messages[0].instructions and messages[1].parts[0].content.endswith(word)


async def test_with_no_card_waiting_a_request_is_sent_as_it_came(worker):
    messages = [ModelRequest(parts=[UserPromptPart("yes maple")])]
    parameters = ModelRequestParameters()

    assert provider.unworded(messages, parameters) == (messages, parameters)


async def test_the_live_voice_is_given_no_waiting_cards_word(worker):
    """GPT-Live is a model too, and one that speaks: told a word, it could
    say a card of its own around it aloud, and a yes at the desk would
    answer the real one."""
    from voice import desk_call

    word = (await _card(worker)).token

    start = desk_call.session_start(history=[("user", f"is {word.lower()} the email?"),
                                             ("assistant", f"Reply YES {word} to go ahead.")])

    assert not _names(json.dumps(start), word)
    assert cardwords.PLACEHOLDER in json.dumps(start)


async def test_a_second_high_risk_card_is_refused_by_the_first_ones_line_not_its_word(worker):
    """The refusal is a tool's result, which the model reads: it names the
    card that waits by what it shows."""
    cap, more = {"daily_usd": 40}, {"daily_usd": 60}
    first = await approvals.request(worker, tool="raise_spending_cap", args=cap,
                                    preview="Raise the daily cap to $40",
                                    consent=classify("raise_spending_cap", cap))
    await approvals.asked()
    word = approvals.get(worker, first["action_id"]).token

    refused = await approvals.request(worker, tool="raise_spending_cap", args=more,
                                      preview="Raise the daily cap to $60",
                                      consent=classify("raise_spending_cap", more))

    assert refused["status"] == "refused" and "Raise the daily cap to $40" in refused["error"]
    assert not _names(json.dumps(refused), word)


async def test_a_wrong_codes_answer_names_the_card_by_its_whole_first_line(worker, monkeypatch):
    """The code's answers say a waiting card's word only on a line that says
    what its yes approves, whole, which keeps the word on the way to the
    phone (agent/cardwords.py)."""
    secret = base64.b32encode(b"0123456789abcdefghij").decode().rstrip("=")
    real_get = secrets.get
    monkeypatch.setattr(secrets, "get", lambda key, default=None: secret
                        if key == totp.SECRET_KEY else real_get(key, default))
    cap = {"daily_usd": 40}
    receipt = await approvals.request(worker, tool="raise_spending_cap", args=cap,
                                      preview="Raise the daily cap to $40\nIt is $20 now",
                                      consent=classify("raise_spending_cap", cap))
    await approvals.asked()
    word = approvals.get(worker, receipt["action_id"]).token
    wrong = str((int(totp.code_at(secret, time.time())) + 1) % 1_000_000).zfill(6)

    said = await answering.intercept_approval(f"YES {word} {wrong}", "signal")

    assert said.startswith("Not done: ")
    assert said.splitlines()[-1] == cardwords.listed(word, "Raise the daily cap to $40")
    assert cardwords.redact(said, cardwords.waiting(worker)) == said
