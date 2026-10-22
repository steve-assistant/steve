"""A spending card takes a yes only with the passphrase, for the card as read.

Spending is the one high-risk action (`core/consent.yaml`, `tier: high`):
a word or a click is not enough, the yes carries the passphrase (or
Windows Hello) and is bound to the card. No tool makes one in v0.1, so the
card is made as the product makes every card, by `approvals.queue`, in
the backend's own database; everything after is the app's path: the
note, sign-in, `/actions/decide`. The passphrase and the session are
watched as canaries: neither may be kept, logged or sent anywhere.
"""

from __future__ import annotations

import contextlib
import secrets as random

import pytest

from agent import approvals
from core import db
from core.consent import classify
from tests.e2e.backend import running
from tests.fakes.world import world

BUY = {"amount_usd": 48.0, "merchant": "Zinc", "item": "a table for two"}


@pytest.fixture(scope="module")
def steve(fenced):
    with (world(fenced / "world") as made,
          running(made, fenced / "data") as backend):
        yield backend


def test_a_spending_card_refuses_a_yes_without_the_passphrase_and_takes_one_with_it(steve):
    typed = f"harbour lantern {random.token_hex(4)}"
    steve.world.keep("PASSPHRASE", typed)
    assert steve.post("/approvals/passphrase", {"new": typed}).json()["ok"]
    session = steve.post("/signin/passphrase", {"passphrase": typed}).json()["session"]
    steve.world.keep("SESSION", session)
    with contextlib.closing(db.open_store(steve.data / "agent.db")) as conn:
        receipt, made = approvals.queue(conn, tool="purchase", args=dict(BUY),
                                        preview="Buy a table for two from Zinc: $48.00",
                                        consent=classify("purchase", BUY))
    assert made is not None and made.high_risk, receipt
    note = steve.card("a table for two", kind="code")

    signed_out = steve.decide(note, True, passphrase=typed)
    assert signed_out.json()["needs"] == "signin"
    bare = steve.decide(note, True, session=session)
    assert bare.status_code == 400 and bare.json()["needs"] == "passphrase"
    wrong = steve.decide(note, True, session=session, passphrase="harbour lantern")
    assert wrong.status_code == 400
    # A yes for the card as it was read, and no other.
    misread = steve.decide({**note, "seen": "0" * 16}, True, session=session, passphrase=typed)
    assert "the card changed" in misread.json()["error"]
    assert steve.card("a table for two", kind="code")["action_id"] == note["action_id"]

    taken = steve.decide(note, True, session=session, passphrase=typed)

    assert taken.status_code == 200 and taken.json()["ok"], taken.text
    assert not [n for n in steve.get("/control/needs").json()["needs"]
                if n.get("action_id") == note["action_id"]]
