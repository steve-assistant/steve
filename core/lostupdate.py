"""Whether a write without a condition needs the user's yes: Jev's judgement.

An account that cannot make a write conditional (an IMAP server without
CONDSTORE, a calendar without etags) cannot refuse a delete or a move of an
item that was changed elsewhere after Steve read it. So the consent rules
made every such write a card (`core/consent.yaml`, `unconditional`), and
`core/writes.py` refused one without it. But the write path fetches the item
again just before it writes, so what the card guarded was narrow -- an edit
in that moment, or a draft the user is still writing in their own mail app
-- and the card was asked for every draft Steve itself had made a minute
before. The maintainer, 14 October 2026: let Jev judge whether asking is needed.

So a write that asks only because its account cannot write on condition
(`Consent.unconditional`) is put to Jev (`core/decide.py`): the card the user
would see, and whether Steve made the item itself in this conversation. It
goes without a card only on a sure *go*, at the critic's bar
(`critic.allow_at`); unsure, unanswered, or Jev unreachable, it asks, as
before. The free verdict says it was judged (`Gate.waived`), and only such a
verdict lets `core/writes.py` write without a condition and without a card:
an envelope's free verdict, about the kind of action, is not this one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from core.decide import Choice, ask
from core.monitor import Gate

CRITERIA = {
    "go": "the item is one Steve made itself in this conversation, or nothing suggests "
          "anyone is still working on it, and the user asked for this change",
    # Not "losing a change would matter": that is true of nearly every item,
    # and a private write is the user's to have done without asking (the
    # maintainer, 14 October 2026: a write is fine unless it invites someone
    # or sends). The card is for a sign of someone at work on it now.
    "ask": "something in the conversation suggests the user or someone the item is shared "
           "with is working on it right now",
}
QUESTION = ("The user's assistant is about to make the change above. The account cannot "
            "refuse it if the item was changed elsewhere since the assistant read it, so a "
            "change made meanwhile -- in the user's own mail or calendar app, or by someone "
            "the item is shared with -- would be lost. Can it go ahead without asking?")
LOST = Choice("lost_update", QUESTION, CRITERIA)


def input_for(tool: str, preview: str, *, made_here: bool) -> str:
    """What Jev is shown: the card, and who made the item."""
    return "\n".join((
        f"The change ({tool}), as the user would be asked about it:",
        preview.strip(),
        "",
        "The assistant made or changed this item itself earlier in this conversation."
        if made_here else "The assistant has not made or changed this item in this conversation.",
    ))


def judge(conn: sqlite3.Connection | None, decided: Gate, *, tool: str, preview: str,
          made_here: bool, key: str | None = None) -> Gate:
    """`decided`, or a free verdict where Jev is sure the card is not needed.
    Only a card asked for want of a condition alone is judged. Blocking (an
    HTTP call); never raises."""
    from core.policy import load_policy

    if decided.kind != "card" or not decided.consent.unconditional:
        return decided
    asked = ask(conn, input_for(tool, preview, made_here=made_here), [LOST], key=key)
    answer = asked.answers.get(LOST.name)
    p = answer.probabilities.get("go") if answer is not None else None
    if answer is None or answer.choice != "go" or p is None \
            or float(p) < load_policy().critic.allow_at:
        return decided
    return Gate("free", replace(decided.consent, gated=False),
                "Jev judged it safe without a condition", waived=True)
