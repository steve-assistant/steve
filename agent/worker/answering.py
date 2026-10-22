"""How an incoming message becomes an answer.

Signal and the desk microphone arrive at `voice_reply`. It answers approvals
first -- before any model, and only for the user's own channels -- and
otherwise hands the text to the conversation for that channel.
"""

from __future__ import annotations

import logging
import re

from agent import (
    approvals,
    cardwords,
    closing,
    jobs,
    offloop,
    runtime,
)
from agent.loop import HISTORY_WINDOW_HOURS, Conversation, TurnFailed

log = logging.getLogger("worker")


# A turn at the desk is spoken sentence by sentence as it is written, and had
# no note at all: on the 9 October 2026 sound test "tell me about the history
# of Pittsburgh" was silent for 36 seconds behind a web search, then spoken for
# 82. What is written before a tool call is said before it runs, so a first
# answer from what it knows fills the wait -- their idea.
VOICE_NOTE = (
    "\n\nThis turn is spoken at the user's desk: each sentence is read aloud as "
    "you write it. Talk, don't write -- no markdown, lists or links -- and keep "
    "it to a few sentences unless they ask for more. Before a slow lookup (a "
    "web search, research, a page), first say in a sentence or two what you "
    "already know, then look it up and add what you find without repeating "
    "yourself."
)
# The note a channel's turns carry, if any (`agent/models_eval.py` replays
# them with the same).
NOTES = {"voice": VOICE_NOTE}


def conversation(session_id: int | None, channel: str = "tray") -> Conversation:
    if session_id is not None and session_id in runtime.conversations:
        return runtime.conversations[session_id]
    convo = Conversation(
        runtime.conn, channel=channel, session_id=session_id, note=NOTES.get(channel),
    )
    runtime.conversations[convo.session_id] = convo
    return convo


def recent_session(channel: str) -> int | None:
    """The session this channel was last using, if it is still today's.

    The user texts from their phone across worker restarts -- a swap, a crash, a
    night of sleep -- and the old code picked whichever conversation
    happened to be first in a dict, which after a restart was none of them.
    So every message started a new session and "what about the second one?"
    reached a model with no first one.

    Scoped by channel rather than global: the tray and the phone are two
    places the user is talking from, and merging them would put half a thought
    from one into the middle of the other.
    """
    row = runtime.conn.execute(
        "SELECT s.id FROM sessions s JOIN messages m ON m.session_id = s.id"
        " WHERE s.channel = ?"
        f"   AND m.created_at >= datetime('now', '-{HISTORY_WINDOW_HOURS} hours')"
        " ORDER BY m.id DESC LIMIT 1",
        (channel,),
    ).fetchone()
    return int(row["id"]) if row else None


def note_said(text: str, *, channel: str = "signal") -> None:
    """Record an unprompted message in the conversation their reply will join.

    The same session `recent_session` will pick for that reply, so "yes, add
    it" lands on a model that has just read what "it" is. If there is no
    conversation from today, this starts one -- and the reply continues it.
    """
    conversation(recent_session(channel), channel=channel).note_said(text)


def note_sent(text: str, *, channel: str) -> None:
    """`note_said` for a message the user has on `channel`, never raising.
    Only the history would be short; a failure here reported as a failed
    send would send it again (agent/worker/links.send_due)."""
    try:
        note_said(text, channel=channel)
    except Exception:  # noqa: BLE001 - the user has the message; only the history is short
        log.warning("a message the user has could not join the conversation", exc_info=True)


def note_feedback(verdict: str, on: str) -> int | None:
    """Their thumbs-up or -down on a reply, into the failure ledger
    (agent/issues.py). Keyed by the message, so reacting twice to one reply
    is one opinion counted twice rather than two issues."""
    from agent import issues

    if verdict not in ("up", "down") or not on.strip():
        return None
    kind = "feedback_down" if verdict == "down" else "feedback_up"
    word = "thumbs-down" if verdict == "down" else "thumbs-up"
    return issues.record(runtime.conn, kind, on[:200], f"The user gave a {word} to: {on[:140]}",
                         excerpt=on, session_id=recent_session("signal"))


async def decide_action(action_id: int, *, approve: bool, by: str,
                        only: list[int] | None = None, seen: str | None = None) -> dict:
    """Record a yes or a no, and queue the work if it was a yes. `only` is a
    yes to part of a grouped card, and `seen` the card as the app read it
    (`approvals.decide`).

    The undo branch exists because the two words arrive through the same door:
    "NO MAPLE" before the yes is a rejection, and "UNDO MAPLE" after it is a
    cancellation of something already running. Asking the user to know which verb
    the state machine wants would be exactly backwards.
    """
    action = approvals.get(runtime.conn, action_id)
    if action is None:
        return {"ok": False, "error": "no such action"}

    if not approve and action.status in {"approved", "executing"}:
        if approvals.cancel_during_hold(runtime.conn, action_id):
            return {"ok": True, "action_id": action_id, "approved": False, "undone": True}
        now = approvals.get(runtime.conn, action_id)
        return {"ok": False, "error": "too late -- it is already running"
                if now is not None and now.status == "executing"
                else "too late -- that already went"}

    outcome = approvals.decide(runtime.conn, action_id, approve=approve, by=by, only=only,
                               seen=seen)
    if outcome.get("ok") and not approve and action.tool == closing.KEEP_TOOL:
        closing.declined(runtime.conn, action_id)
    if outcome.get("ok") and approve:
        # Read again: a yes to part of a card rewrote its preview to the part.
        decided = approvals.get(runtime.conn, action_id) or action
        outcome["job_id"] = jobs.enqueue(
            runtime.conn, kind="action", prompt=str(action_id), why=decided.preview[:120],
            session_id=action.session_id, priority=1,
        )
    return outcome


# Putting back every model the scout switched to (agent/models_switch.py).
# Two words the user can send from memory, answered here for the reason REVOKE is:
# a model could talk its way past an undo it was asked to perform, and the
# model being undone is the one that would read it. Safe if forged: the worst
# it can do is put the lineup as written back.
REVERT_MODELS_RE = re.compile(r"^\s*revert\s+models?\s*[.!]?\s*$", re.IGNORECASE)


def _revert_models() -> str:
    from agent import models_switch

    back = models_switch.revert(runtime.conn, by="user", why="the user said REVERT MODELS")
    refused = models_switch.apply()
    if not back:
        return "Nothing is switched: every role is on the lineup as written."
    lines = ", ".join(f"{models_switch.SAYS.get(r, r)} on {models_switch.written(r)}"
                      for r in back)
    tail = f" (still refused: {refused})" if refused else ""
    return f"Put back: {lines}.{tail}"


# Pausing every routine the user set up (agent/routines.py), answered before any
# model for the reason REVOKE is: a routine run by a model that has read
# something hostile is exactly the one that should not get to argue. Safe if
# forged: the worst it does is pause what the user can resume.
STOP_ROUTINES_RE = re.compile(r"^\s*stop\s+(?:all\s+)?routines?\s*[.!]?\s*$", re.IGNORECASE)


def _stop_routines() -> str:
    from agent import routines

    paused = routines.pause_all(runtime.conn)
    if not paused:
        return "No routine is running."
    return (f"Paused {paused} routine{'s' if paused != 1 else ''}. Nothing runs until you "
            "ask me to resume one.")


# The same brake for their goals (agent/goals.py): a session that has read
# something hostile does not get to argue either.
STOP_GOALS_RE = re.compile(r"^\s*stop\s+(?:all\s+)?goals?\s*[.!]?\s*$", re.IGNORECASE)


def _stop_goals() -> str:
    from agent import goals

    paused = goals.pause_all(runtime.conn)
    if not paused:
        return "No goal is being worked on."
    return (f"Paused {paused} goal{'s' if paused != 1 else ''}. Nothing more is done on "
            f"{'them' if paused != 1 else 'it'} until you ask me to resume.")


# Taking a skill back (agent/skills.py), as the morning digest says to:
# answered before any model for the reason REVOKE is -- a turn following the
# skill is the one that should not get to keep it. Safe if forged: the worst
# it does is drop guidance.
REVOKE_SKILL_RE = re.compile(r"^\s*(?:revoke|forget|drop)\s+skill\s+#?(\d+)\s*[.!]?\s*$",
                             re.IGNORECASE)


def _revoke_skill(number: int) -> str:
    from agent import skills

    gone = skills.revoke(runtime.conn, number)
    if gone is None:
        kept = ", ".join(str(s.id) for s in skills.active(runtime.conn)) or "none"
        return f"No skill {number} is kept (kept: {kept})."
    return f"Revoked skill {gone.id}, \"{gone.title}\". It won't be used again."


# Dropping a fact the night kept from what the user said, as the morning digest
# says to (agent/night/learning.py `_learn_from_user_words`): answered before
# any model, for the reason REVOKE SKILL is -- a fact the user says is wrong must
# not depend on a model that believes it. Safe if forged: the worst it does
# is drop a memory, which the memories repo's history keeps.
FORGET_MEMORY_RE = re.compile(r"^\s*forget\s+memory\s+#?(\d+)\s*[.!]?\s*$", re.IGNORECASE)


async def _forget_memory(number: int) -> str:
    """A fact by its number from `facts.FIRST_ID` up -- the number a reply
    that kept it gave them -- else the memory the index numbers so. Before
    facts, any number went to the memories index, where 1000 and up was
    never anything: "No memory 1004 is kept"."""
    from agent import facts, offloop

    # Off the loop: forgetting a memory commits to the memories repo.
    said = await offloop.with_own_conn(runtime.conn, facts.forget_number, number)
    if said is None:
        return f"No memory {number} is kept."
    kind = "fact" if number >= facts.FIRST_ID and said.startswith(f"[{number}]") else "memory"
    return f"Forgot {kind} {number}: \"{said}\"."


# Where the user's own words arrive. Only these can answer a card. A reminder's
# text and every other autonomous input also come through `voice_reply`, and
# until this they passed the same intercept: a reminder reading "send the invoice" was
# an approval of whatever card was waiting.
APPROVAL_CHANNELS = frozenset({"signal", "telegram", "voice"})


async def intercept_approval(text: str, channel: str, *,
                             card: int | None = None, spoken: bool = False) -> str | None:
    """Answer a yes or a no before the model ever sees it, or return None.

    This runs ahead of the conversation on purpose, and it is the second half
    of the whole design: an approval that reached the model would be an
    approval the model could produce, and a system that can talk itself into
    sending an email is not gated at all.

    The cost of getting it wrong is real in the other direction too -- every
    message swallowed here is a turn the user never gets an answer to -- which is
    why `parse_reply` returns None for anything that is not unmistakably an
    answer.

    `card` is the card a press or a reaction was on: it decides that card
    or none (`approvals.parse_reply`).

    `spoken` is a voice note's transcript (channels/bridge.py). It answers a
    card only by naming the card's word: a bare "yes" in it is asked about,
    as one after the chat moved on is. Its transcript came in as typed text
    until step 11's audit (D11), and a forwarded recording of someone saying
    "Yes." approved the one card waiting. Anyone's recording can say yes;
    only someone reading the card says its word.
    """
    if channel not in APPROVAL_CHANNELS:
        return None
    if match := REVOKE_SKILL_RE.match(text):
        return _revoke_skill(int(match.group(1)))
    if match := FORGET_MEMORY_RE.match(text):
        return await _forget_memory(int(match.group(1)))
    if REVERT_MODELS_RE.match(text):
        return _revert_models()
    if STOP_ROUTINES_RE.match(text):
        return _stop_routines()
    if STOP_GOALS_RE.match(text):
        return _stop_goals()

    # A card an open turn still holds has not been asked: a goal's or a
    # routine's turn may yet join items to it, and the phone has not seen
    # it. A bare "yes" here was about something else, and decided it all
    # the same when it was the only card pending (step 11's audit). The app
    # refuses a yes to one too (`routes.action_decide`).
    held = approvals.held_now()
    waiting = [a for a in approvals.pending(runtime.conn) if a.id not in held]
    decision = approvals.parse_reply(
        text,
        waiting,
        approvals.holding(runtime.conn),
        bare_ok=len(waiting) == 1 and not spoken
        and not approvals.moved_on_since(runtime.conn, waiting[0], channel),
        withdrawn=approvals.withdrawn(runtime.conn),
        card=card,
    )
    if decision is None:
        return None
    if decision.action_id is None:
        return decision.reply

    action = approvals.get(runtime.conn, decision.action_id)
    token = action.token if action else "that"
    if decision.approved and action is not None and action.status == "pending":
        # A high-risk yes must carry their authenticator code, checked and
        # bound to this card by the frozen core: these words came over a
        # channel someone else could have written on (core/monitor.py).
        from core import monitor

        # Off the loop: the check reads the keychain and the count's file
        # and writes the database. One guess at a time still, as the count's
        # lock is a thread's (core/totp.TRYING).
        if why := await offloop.with_own_conn(runtime.conn, monitor.accept_code, action.id,
                                              action.tool, text, by=channel):
            # The card it is about by the list's line, which says its word
            # with its whole first line, as only the code's lines may.
            return (f"Not done: {why}.\n"
                    + cardwords.listed(token, cardwords.first_line(action.preview)))
    # Recorded as the channel it came from, which decides what it may
    # approve: until step 4 every answer here was recorded as Signal's, a
    # spoken one at the desk too.
    outcome = await decide_action(
        decision.action_id, approve=bool(decision.approved), by=channel
    )

    if not outcome.get("ok"):
        return outcome.get("error") or "I couldn't do that."
    if outcome.get("undone"):
        return f"Stopped {token}."
    if not decision.approved:
        return f"Dropped {token}."
    held = action is not None and action.reversal == "irreversible"
    return (
        f"Going ahead with {token}"
        + (f" in {approvals.HOLD_SECONDS}s — UNDO {token} to stop it." if held else ".")
    )


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------


def cut_short(channel: str, heard: str) -> None:
    """The last spoken reply on `channel`, as far as the user heard it
    (`Conversation.cut_short`): called by the voice when the user cuts in."""
    session_id = recent_session(channel)
    if session_id is None:
        return
    conversation(session_id, channel=channel).cut_short(heard)


async def voice_reply(text: str, channel: str = "signal", *, on_text=None,
                      on_tool=None, on_restart=None, images=None,
                      card=None, spoken: bool = False) -> tuple[str, dict]:
    """Bridge every non-tray input to the same conversation loop text uses.

    `channel` decides which thread this joins, and the default is the one
    that matters: Signal is where the user actually is. The four callers are not
    interchangeable -- a reminder checking on something is not part of the
    conversation the user is having on their phone, and a spoken turn at the desk is
    a third place. Merging them would drop half a thought into the middle of
    another.

    `images` are photos sent with `text`, shown to the model with it
    (channels/photos.py). `card` is the card a press or a reaction answers.
    `spoken` marks a voice note's transcript, which answers a card only by
    its word (`intercept_approval`).

    `on_text` hears the reply as it is written, at the desk, and is not
    redacted as the finished reply is: the model is sent no waiting card's
    word (agent/provider.Unworded), so what it writes cannot say one but by
    guessing, and what is spoken stays on the laptop.
    """
    # Ahead of the conversation, not inside it: see `intercept_approval`.
    answer = await intercept_approval(text, channel, card=card, spoken=spoken)
    if answer is not None:
        return answer, {"intercepted": True}

    from core.call_policy import is_autonomous

    convo = conversation(recent_session(channel), channel=channel)
    try:
        # Unattended (a reminder going off): what it says is what the
        # session was asked, for the action critic.
        if is_autonomous(channel):
            convo.mandate = text
        # Heard as it is written, at the desk (voice/pipeline.py); only then.
        hooks = {name: fn for name, fn in (("on_text", on_text), ("on_tool", on_tool),
                                           ("on_restart", on_restart)) if fn}
        if images:
            hooks["images"] = images
        reply = await convo.send(text, **hooks)
    except TurnFailed as broke:
        # Answered here, by the code that knows what broke.
        return broke.text, {"session_id": convo.session_id, "failed": True}
    # The model's words never carry a waiting card's: a reply that did could
    # show a card of its own, and the user's yes would answer the real one
    # (agent/cardwords.py). The answers above, to a card, are the code's.
    said = cardwords.redact(reply.text, cardwords.waiting(runtime.conn))
    return said, {
        "session_id": convo.session_id,
        "role": reply.role.value,
        "cost_usd": reply.cost_usd,
        "tools_used": reply.tools_used,
        "deferred_jobs": reply.deferred_jobs,
    }
