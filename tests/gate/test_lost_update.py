"""A write without a condition asks the user unless Jev is sure it need not
(core/lostupdate.py).

Every delete or move on an account that cannot write on condition was a
card, though the write path reads the item again just before writing: the
user was asked about a draft Steve had made a minute before. Jev now judges
whether asking is needed. These hold what may go without the card, and what
still may not: anything another rule gates, and anything Jev is not sure of.
"""

from __future__ import annotations

import pytest

from core import lostupdate
from core.consent import classify
from core.monitor import Gate
from steve_sdk import ids
from tests.gate.decisions import install

DRAFT = ids.make("mail", "0123456789ab", "INBOX.Drafts\n7\n3\n<d@x>")
EVENT = ids.make("calendar", "0123456789ab", "e1")
NONE_CONDITIONAL = frozenset()
CARD = "Delete a draft in your home Drafts\nTo: dana@example.com\nSubject: Saturday"


@pytest.fixture(autouse=True)
def _keyed(monkeypatch):
    monkeypatch.setattr("core.secrets.get", lambda name: "test-key")


def _gate(tool: str, args: dict, conditional=NONE_CONDITIONAL) -> Gate:
    consent = classify(tool, args, conditional=conditional)
    return Gate("card" if consent.gated else "free", consent)


def _judge(decided: Gate, *, made_here: bool = False) -> Gate:
    return lostupdate.judge(None, decided, tool="delete_draft", preview=CARD,
                            made_here=made_here)


def test_a_card_asked_only_for_want_of_a_condition_is_marked_so():
    """Only the missing condition may be judged away. A move with guests
    tells them, so its card is about the guests, and stays."""
    alone = classify("delete_draft", {"draft_id": DRAFT}, conditional=NONE_CONDITIONAL)
    moved = classify("update_event", {"event_id": EVENT}, conditional=NONE_CONDITIONAL)
    guests = classify("update_event", {"event_id": EVENT, "attendees": ["ada@x"]},
                      conditional=NONE_CONDITIONAL)
    sent = classify("send_draft", {"draft_id": DRAFT}, conditional=NONE_CONDITIONAL)
    on_condition = classify("delete_draft", {"draft_id": DRAFT},
                            conditional=frozenset({"mail:0123456789ab"}))

    assert alone.gated and alone.unconditional
    assert moved.gated and moved.unconditional
    assert guests.gated and not guests.unconditional
    assert sent.gated and not sent.unconditional
    assert not on_condition.gated and not on_condition.unconditional


def test_a_write_jev_is_sure_of_goes_without_its_card_and_says_it_was_judged(monkeypatch):
    jev = install(monkeypatch, lost_update={"go": 0.9, "ask": 0.1})
    decided = _judge(_gate("delete_draft", {"draft_id": DRAFT}), made_here=True)

    assert decided.kind == "free" and decided.waived and not decided.consent.gated
    [shown] = jev.inputs
    assert CARD in shown and "delete_draft" in shown
    assert "made or changed this item itself" in shown


@pytest.mark.parametrize("answer", ["ask", {"go": 0.55, "ask": 0.45}, None],
                         ids=["ask", "unsure", "unanswered"])
def test_a_write_jev_is_not_sure_of_keeps_its_card(monkeypatch, answer):
    install(monkeypatch, **({"lost_update": answer} if answer is not None else {}))
    decided = _judge(_gate("delete_draft", {"draft_id": DRAFT}))

    assert decided.kind == "card" and not decided.waived


def test_with_jev_unreachable_or_no_key_the_card_stays(monkeypatch):
    install(monkeypatch, down=RuntimeError("Jev is down"))
    assert _judge(_gate("delete_draft", {"draft_id": DRAFT})).kind == "card"
    monkeypatch.setattr("core.secrets.get", lambda name: "")
    assert _judge(_gate("delete_draft", {"draft_id": DRAFT})).kind == "card"


def test_jev_is_not_asked_about_a_card_another_rule_gates_or_a_free_write(monkeypatch):
    """A send, a move with guests: each card is the user's consent to what
    the write does, which no judgement of a race can stand in for."""
    jev = install(monkeypatch, lost_update="go")
    for decided in (_gate("update_event", {"event_id": EVENT, "attendees": ["ada@x"]}),
                    _gate("send_draft", {"draft_id": DRAFT}),
                    _gate("delete_draft", {"draft_id": DRAFT},
                          conditional=frozenset({"mail:0123456789ab"}))):
        assert _judge(decided) == decided
    assert jev.asked == []
