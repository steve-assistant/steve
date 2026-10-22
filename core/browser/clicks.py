"""Which clicks in a logged-in browser need their yes.

Logged in, a click can be the action itself: Follow, Delete, Cancel
reservation, Accept -- one press, no form. The maintainer, 1 October 2026: a judge
should decide which actions are safe, perhaps Jev. So a decision model
judges, but only what is left once deterministic rules have spoken, and
never in the direction of less care than they set:

- **A login the user marked `ask`:** every click that is not a plain link is theirs
  to approve (`card`).
- **A label that names a consequence** -- delete, send, post, follow, book,
  cancel, accept, log out and the rest of `CONSEQUENTIAL` -- is a card,
  whatever a model thinks, link or button.
- **A plain link** within the site (the driver keeps it there) is free.
- **Anything else** is Jev's to judge (`verdict`): carded at or above
  `THRESHOLD`, and carded if Jev could not be asked. A judge that fails
  never lets a click through.

Frozen, so the night cannot widen what is free or lower the bar.
"""

from __future__ import annotations

import re

THRESHOLD = 0.25

CONSEQUENTIAL = re.compile(
    r"\b(delete|remove|send|post|publish|share|repost|retweet|follow|unfollow|connect|"
    r"like|unlike|react|comment|reply|message|invite|accept|decline|reject|ignore|"
    r"cancel|confirm|book|reserve|rsvp|apply|enrol|enroll|drop|withdraw|register|"
    r"sign(?!\s*in\b)|approve|unsubscribe|subscribe|submit|save|update|change|"
    r"block|report|transfer|pay|buy|order|purchase|checkout|check\s*out|"
    r"log\s*out|logout|sign\s*out|archive|mute|leave|join|request|claim|redeem|vote|"
    r"endorse|recommend|upload|attach|forward|schedule|reschedule|refund|return|"
    r"deactivate|close\s+account|mark\s+as|add\s+to|grant|allow|authori[sz]e|trust|"
    r"verify|resend|reset|disconnect|revoke|hide|pin|star|bookmark)\b",
    re.IGNORECASE)


def rule(element: dict, tier: str) -> str:
    """"free", "card" or "judge" for clicking `element` in a browser logged
    in to a login of `tier`."""
    label = " ".join(str(element.get("label") or "").split())
    if CONSEQUENTIAL.search(label):
        return "card"
    is_link = (element.get("tag") or "") == "a" and bool(element.get("href"))
    if is_link and not str(element.get("href") or "").lower().startswith("javascript:"):
        return "free"
    if tier != "free":
        return "card"
    return "judge"


def verdict(ruled: str, p_consequential: float | None) -> str:
    """The click's fate: "free" or "card". A judgement is only asked for
    what `rule` left to it; no answer, or one at the bar, is a card."""
    if ruled != "judge":
        return ruled
    if p_consequential is None or p_consequential >= THRESHOLD:
        return "card"
    return "free"


JUDGE = (
    "The user's assistant is using a website while logged in as them, and is about to "
    "click an element. Would clicking it change something in their account or the world "
    "-- send, "
    "post, follow, book, cancel, delete, accept, buy, sign up, change a setting -- or do "
    "something other people would see? Merely opening a page, a menu, a tab, a filter, "
    "a search, or the next page of results is not. If unsure, say yes."
)
