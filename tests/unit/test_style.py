"""Drafts in the user's voice: learned from their sent mail, and only from it.

Two failures matter here. The privacy one: an example "of how she writes"
pulled from their inbox is somebody else's email handed to a model, the thing
commitment extraction was built never to do. And the quality one: a sent
reply carries the email it answers, so a guide learned from the whole body
is a guide to other people's voices.
"""

from __future__ import annotations

import datetime as dt

import pytest

from agent import night
from agent.night import learning, model
from core import db
from steve_sdk.classes import Message
from steve_sdk.errors import Unsupported
from tests.gate import mailfake
from tools import memory, people, style

TODAY = dt.date(2026, 9, 24)


@pytest.fixture
def memories(memories_repo, monkeypatch):
    monkeypatch.setattr(memory, "embed", lambda texts: [[0.1] * db.EMBEDDING_DIM for _ in texts])
    return memories_repo


def _ms(days_ago: int) -> int:
    return int((dt.datetime(2026, 9, 24, 12) - dt.timedelta(days=days_ago)).timestamp() * 1000)


class _Sent(mailfake.Mailbox):
    """An account whose sent mail is `sent` and whose inbox fails anything
    that reads it. Says which recipient each read of its sent mail was for."""

    def __init__(self, *sent: Message) -> None:
        super().__init__("maya@example.com", Message(
            "in1", sender="jane@northwind.example", to="maya@example.com",
            subject="Inbox", body="her words, not the user's", when=_ms(0)))
        self.sent = list(sent)
        self.asked: list[str] = []

    def sent_items(self, *, since, limit, to=""):
        self.asked.append(to)
        return super().sent_items(since=since, limit=limit, to=to)

    def search(self, *a, **k):
        raise AssertionError("searched beyond their sent mail")

    def fetch(self, *a, **k):
        raise AssertionError("read something other than their sent mail")


def _sent(native: str, to: str, subject: str, body: str, days_ago: int) -> Message:
    return Message(native, to=to, subject=subject, body=body, when=_ms(days_ago),
                   direction="out")


# --------------------------------------------------------------------------
# The user's words, not other people's
# --------------------------------------------------------------------------


@pytest.mark.parametrize("quoted", [
    "On Tue, Sep 22, 2026 at 10:04 AM Jane Doe <\njane@northwind.example> wrote:\n> Could you do "
        "Thursday?",
    "________________________________\nFrom: Jane Doe <jane@northwind.example>\nSent: "
        "Tuesday\n\nThursday?",
    "From: Jane Doe <jane@northwind.example>\nDate: Tue, 22 Sep 2026\n\nThursday?",
    "> Could you do Thursday?\n> Jane",
    "Sent from my iPhone",
])
def test_the_email_the_user_was_answering_is_cut_so_the_voice_is_theirs(quoted):
    body = f"Hi Jane,\r\n\r\nThursday works. See you then.\r\n\r\nBest,\r\nMaya\r\n\r\n{quoted}"

    assert style.own_words(body) == "Hi Jane,\n\nThursday works. See you then.\n\nBest,\nMaya"


def test_a_forward_with_no_note_of_theirs_is_not_a_sample():
    text, count = style.sample([
        {"to": "a@x.example", "subject": "Fwd: deck",
         "body": "---------- Original Message ----------\nThe deck"},
        {"to": "b@x.example", "subject": "Re: lunch", "body": "Sounds good -- noon.\n\nMaya"},
    ])

    assert count == 1
    assert "Sounds good" in text and "The deck" not in text


# --------------------------------------------------------------------------
# Examples come from sent mail only
# --------------------------------------------------------------------------


def test_examples_are_read_from_their_sent_mail_only(monkeypatch):
    """The gate test holds learning to each account's sent items; this path
    sends mail text to a model too, so it is held to the same line. Every
    account's, newest first."""
    work = _Sent(_sent("w1", "Jane Doe <jane@northwind.example>", "Coffee", "Hi Jane, thanks!", 3),
                 _sent("w2", "Rina <rina@contoso.example>", "403", "Hey Rina", 1))
    home = _Sent(_sent("h1", "jane@northwind.example", "Intro", "Hi Jane --", 9))
    mailfake.connect(monkeypatch, work=work, home=home)

    found = style.examples(None, "jane@northwind.example")

    assert [e["subject"] for e in found] == ["Coffee", "Intro"]
    assert work.asked == home.asked == ["jane@northwind.example"]


def test_a_recipient_cannot_widen_the_search_past_their_sent_mail(monkeypatch):
    """What the model passes as `to` reaches an account's query. Only the
    address in it does."""
    box = _Sent()
    mailfake.connect(monkeypatch, home=box)

    style.examples(None, "jane@northwind.example OR in:inbox")

    assert box.asked == ["jane@northwind.example"]


def test_the_newest_two_are_shown_and_trimmed(monkeypatch):
    """The newest across every account, not each account's newest."""
    mailfake.connect(
        monkeypatch,
        home=_Sent(*[_sent(f"s{d}", "jane@northwind.example", f"s{d}", "x" * 2000, d)
                     for d in (9, 5)]),
        work=_Sent(_sent("s1", "jane@northwind.example", "s1", "x" * 2000, 1)))

    found = style.examples(None, "Jane <JANE@northwind.example>")

    assert [e["subject"] for e in found] == ["s1", "s5"]
    assert all(len(e["text"]) == style.EXAMPLE_CHARS for e in found)


def test_an_account_that_cannot_list_its_sent_mail_is_passed_over(monkeypatch):
    """A password account cannot list its sent mail yet; the draft still
    has the other account's examples."""

    class _NoSent(_Sent):
        def sent_items(self, **kw):
            raise Unsupported("reading this account's sent mail")

    mailfake.connect(monkeypatch, other=_NoSent(),
                     home=_Sent(_sent("h1", "jane@northwind.example", "Intro", "Hi Jane --", 2)))

    assert [e["subject"] for e in style.examples(None, "jane@northwind.example")] == ["Intro"]


def test_what_is_learned_from_a_sent_email_names_its_account(monkeypatch):
    """A commitment keeps the message it came from as its source; by a slot
    id, it goes when the account is switched off."""
    accounts = mailfake.connect(
        monkeypatch, home=_Sent(_sent("h1", "jane@northwind.example", "Intro", "Hi Jane --", 2)))

    [message] = style.sent_mail(None, days=14, limit=20)

    assert message["id"] == accounts["home"].id("h1")
    assert (message["to"], message["body"], message["date"]) == (
        "jane@northwind.example", "Hi Jane --", "2026-09-22")


def test_without_a_recipient_only_the_guide_is_read(monkeypatch, memories, conn):
    box = _Sent()
    mailfake.connect(monkeypatch, home=box)
    style.save(conn, "- Opens with the name and a comma: \"Jane,\"\n" * 10, TODAY)

    found = style.for_drafting(conn, "")

    assert found["guide"].startswith("- Opens with") and "sent_to_them" not in found
    assert box.asked == []


# --------------------------------------------------------------------------
# Learned weekly
# --------------------------------------------------------------------------


@pytest.fixture
def learner(monkeypatch):
    """The night's learner with a fake model and fake sent mail."""
    state = {"asked": 0, "guide": "- Signs off \"Best, Maya\".\n" * 12,
             "sent": [{"to": f"p{i}@x.example", "subject": "s", "body": f"Thanks {i}!"}
                      for i in range(8)]}

    async def fake_ask(output_type, prompt, system):
        state["asked"] += 1
        state["prompt"] = prompt
        return learning.StyleGuide(guide=state["guide"]), 0.002

    async def fake_sent(conn, window):
        return state["sent"]

    monkeypatch.setattr(model, "ask", fake_ask)
    monkeypatch.setattr(learning, "_sent_for_extraction", fake_sent)
    return state


async def _learn(conn, today):
    report = night.NightReport(started_at="now")
    await learning._learn_style(conn, night.Budget(1.0), report, today=today)
    return report


async def test_the_guide_is_relearned_weekly_not_nightly(conn, memories, learner):
    first = await _learn(conn, TODAY)
    await _learn(conn, TODAY + dt.timedelta(days=3))
    assert learner["asked"] == 1
    assert "relearned how the user writes, from 8 sent emails" in first.learned

    await _learn(conn, TODAY + dt.timedelta(days=7))
    assert learner["asked"] == 2
    assert memory.read(style.PATH).body.startswith("- Signs off")
    assert people.cursor(conn, style.CURSOR) == "2026-10-01"


async def test_a_guide_deleted_by_hand_is_relearned_the_next_night(conn, memories, learner):
    await _learn(conn, TODAY)
    (memories / style.PATH).unlink()

    await _learn(conn, TODAY + dt.timedelta(days=1))

    assert learner["asked"] == 2


async def test_a_useless_answer_keeps_last_weeks_guide(conn, memories, learner):
    await _learn(conn, TODAY)
    learner["guide"] = "I can't help with that."

    await _learn(conn, TODAY + dt.timedelta(days=8))

    assert memory.read(style.PATH).body.startswith("- Signs off")


async def test_too_little_sent_mail_teaches_nothing(conn, memories, learner):
    learner["sent"] = learner["sent"][:2]

    await _learn(conn, TODAY)

    assert learner["asked"] == 0 and memory.read(style.PATH) is None


def test_a_long_guide_is_cut_at_a_line_not_mid_rule():
    rule = "- Closes with \"Thanks,\" then \"Maya Lindqvist\" on its own line.\n"
    guide = rule * 60

    cut = style.trimmed(guide)

    assert len(cut) <= style.MAX_GUIDE
    assert cut.endswith("own line.") and cut.count("- Closes") == len(cut.split("\n"))


def test_the_night_prompt_asks_for_their_voice_not_their_business():
    prompt = model.prompt("night_style")

    assert "exactly" in prompt and "No names" in prompt
