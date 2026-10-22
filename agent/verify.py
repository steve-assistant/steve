"""Before an answer goes: does it claim only what was actually done?

`grounded` is the rubric's weakest score, and once the judge could see what
the tools returned (`agent/reflect.transcript`, 25 September 2026) one of the
failures left was plain: saying something was done that no tool did -- "Added
both events to your calendar" with no calendar call.

`unbacked_claims` is code. An effect the reply claims -- added, scheduled, set
a reminder, drafted, sent, marked done -- has to match a tool called in this
conversation. An offer, a question or a negation is not a claim, and a send
queued for their yes is not "sent" -- but one that went is. A claim that fails
gets one correction pass (`note`): do it, or say it was not done.

A second check was tried and taken out: a small model listing details about
their things that the tool results did not support. Replayed on 24 of their real
lookup turns, grounding was better on 4, worse on 5 and the same on 15, for
1.3 s more a turn at the median. Where it was worse, the revision had looked
again and come back with different details, not better-supported ones. It
did not earn its latency.
"""

from __future__ import annotations

import re

_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
# Any "-n't", not a list of them: "don't" was missing from the list, and
# `\bn't` never matched inside a word.
_NOT_A_CLAIM = re.compile(
    r"(?i)\b(?:not|\w+n't|never|if|want me to|"
    r"should i|shall i|can i|could i|would you like|do you want|i can|i could|i'll|let me)\b"
    r"|\?\s*$"
)
# The models write curly apostrophes ("I haven’t sent it"), which no
# pattern here spells.
_STRAIGHT = str.maketrans("‘’ʼ", "'''")

# "Is scheduled for", "was originally moved to": a lookup describing an event,
# not a claim to have scheduled it. On 27 September "Outlook confirms the
# coffee chat was scheduled for September 24" was read as a claim, and the
# correction added "I didn't schedule it" to an answer nobody had asked to
# schedule anything. "Now" is left out on purpose: "it is now scheduled for
# Friday" is how a reply claims to have moved it.
_DESCRIBES_A_TIME = re.compile(
    r"(?i)\b(?:is|are|was|were)\s+(?:\w+ly\s+|already\s+|still\s+)?"
    r"(?:scheduled|rescheduled|moved)\b"
)
SCHEDULED = "scheduled something"

# What a reply may claim to have done, and the tools that would have done it.
EFFECTS: list[tuple[re.Pattern, frozenset[str], str]] = [
    (re.compile(r"(?i)\b(?:added|put|created|booked)\b[^.!?\n]{0,60}\b(?:calendar|events?)\b"),
     frozenset({"create_event", "update_event"}), "added to the user's calendar"),
    # Its own act: an object ("moved your call to", "scheduled it for"), a
    # first person ("I've rescheduled ... for") or "now". Not history: on 9
    # October 2026 "The society was founded in 1787; it moved to
    # Pittsburgh in 1819" sent a spoken answer back for a correction, four seconds
    # of silence, for a meeting nobody had mentioned.
    (re.compile(r"(?i)(?:\b(?:scheduled|rescheduled|moved)\s+(?:it|them|that|this|your|his|the|"
                r"a|an)\b|\b(?:I|I've|I have|we've|we)\s+(?:\w+\s+)?(?:scheduled|rescheduled|"
                r"moved)\b|\bnow\s+(?:scheduled|rescheduled|moved)\b)[^.!?\n]{0,50}\b(?:for|to)\b"),
     frozenset({"create_event", "update_event"}), SCHEDULED),
    (re.compile(r"(?i)\b(?:set|created)\b[^.!?\n]{0,20}\breminder\b|\breminder\b[^.!?\n]{0,10}\bset\b"),
     frozenset({"remind_me"}), "set a reminder"),
    (re.compile(r"(?i)\badded\b[^.!?\n]{0,60}\btasks?\b"),
     frozenset({"add_task"}), "added to the user's tasks"),
    (re.compile(r"(?i)\b(?:drafted|saved (?:a|the|your) draft|draft is (?:in|waiting))\b"),
     frozenset({"draft_email", "send_email"}), "drafted an email"),
    # Its own act, as with scheduling: no subject ("Sent it to Leo.", "Done:
    # sent the reply") or a first person ("I've also sent it"). Someone
    # else's sending is not a claim: "LinkedIn turned the browser away and
    # sent it to a sign-in page" (8 October) and "the system sent it" (9
    # October) each went back for a correction about an email nobody had
    # mentioned.
    (re.compile(r"(?i)(?:^\W*(?:just\s+|also\s+|already\s+)?|[:;,—–]\s*"
                r"|\b(?:I|I've|I have|we|we've|we have)\s+(?:\w+\s+)?)"
                r"sent\b[^.!?\n]{0,40}\b(?:email|message|reply|it)\b"
                r"|\bemail (?:was |has been )?sent\b"),
     frozenset({"send_email", "send_draft"}), "sent an email"),
    (re.compile(r"(?i)\bmarked\b[^.!?\n]{0,40}\b(?:done|complete|completed|finished)\b"),
     frozenset({"commitment_done", "complete_task"}), "marked something done"),
    (re.compile(r"(?i)\b(?:cancelled|canceled|deleted|removed)\b[^.!?\n]{0,40}\breminders?\b"),
     frozenset({"cancel_reminder"}), "cancelled a reminder"),
    (re.compile(r"(?i)\b(?:deleted|removed|threw away)\b[^.!?\n]{0,40}\bdraft\b"),
     frozenset({"delete_draft"}), "deleted a draft"),
    (re.compile(r"(?i)\b(?:deleted|removed|took)\b[^.!?\n]{0,60}\b(?:calendar|events?)\b"),
     frozenset({"delete_event"}), "removed something from the user's calendar"),
    (re.compile(r"(?i)\barchived\b"), frozenset({"modify_email"}), "archived an email"),
]
SENDS = frozenset({"send_email", "send_draft"})
_SENT = re.compile(r"(?i)\bsent\b|\bemail (?:was |has been )?sent\b")
_QUEUED = re.compile(r"(?i)\b(?:queued|approv|card|confirm|waiting|once you say)")


def unbacked_claims(reply: str, used_in_session: set[str], used_this_turn: set[str], *,
                    went_this_turn: set[str]) -> list[str]:
    """What the reply says was done that no tool in this conversation did.

    `went_this_turn` names the actions this turn that were done, not put on a
    card for their yes and not failed: what `act` performed, by its own name.
    """
    problems: list[str] = []
    for sentence in _SENTENCES.split((reply or "").translate(_STRAIGHT)):
        if not sentence.strip() or _NOT_A_CLAIM.search(sentence):
            continue
        for pattern, tools, what in EFFECTS:
            if what == SCHEDULED and _DESCRIBES_A_TIME.search(sentence):
                continue
            if pattern.search(sentence) and not (tools & used_in_session):
                problems.append(f'It says it {what} ("{sentence.strip()[:80]}"), but no tool'
                                " that would have done that was called, or it failed.")
                break
    # Sending usually asks them first, and a card has sent nothing. But a send
    # their approved routine or goal may make goes at once
    # (`core/monitor.envelope_gate`), and so does a note to themselves. This
    # took every send as a card, and its correction had a routine tell the
    # user its email to Mom "hasn't been sent; it's waiting for your
    # approval" when it had gone: 5 of 36 such runs of the yardstick, 11 to
    # 13 October 2026. What went is read from what `act` did, not from the
    # tool's name.
    sends = SENDS & used_this_turn
    if (sends and not SENDS & went_this_turn
            and _SENT.search(reply or "") and not _QUEUED.search(reply or "")):
        problems.append("It says the email was sent, but it is only queued: the user has to"
                        " say yes to the card first.")
    return problems


def note(problems: list[str]) -> str:
    """The instruction for the one correction pass."""
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        "Before that reply goes to the user, check it against what you actually did:\n"
        f"{listed}\n\n"
        "Fix it: if you meant to do something, call the tool now; otherwise say plainly that "
        "it has not been done. Reply with the corrected answer only, written to the user, as if "
        "this note had not been sent."
    )
