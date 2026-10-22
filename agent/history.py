"""What a conversation carries into its next turn: what was said, not every
page and search result read on the way.

The maintainer, 5 October 2026: "the agent is being a bit slow to answer". Measured
the same evening, a Signal thread about camping sites was re-sending 357,000
to 935,000 tokens on every turn -- the raw web pages and searches of every
earlier turn, kept in the in-memory history after each answer and sent again
with each new question. "Needs to basically have tent camping" took one tool
call and 357,000 tokens of yesterday's pages. Time tracked size: a simple
question in a fresh conversation answered in 2-5 s, the same kind of question
at the end of that thread in 37-116 s.

A worker that restarted already dropped them -- `Conversation._resume`
replays only what was said, because "a stale search result is worth less than
the sentence that summarised it" -- so a long thread was fast or slow by the
accident of when the worker last restarted. `compact` makes the live history
what a restart would have made it, with one exception: the latest turn keeps
its tool results, each cut to LAST_TURN_TOOL_CHARS, for the follow-up that
asks about them ("which of those two sites..."). Every earlier turn keeps their
words, the answer, and anything said to them outside a turn (a ping, a card),
and nothing the tools returned -- except one line for each thing it did
(`receipt`).

That exception is from 7 October 2026. Earlier turns kept only words, so the
event it had created nine hours before was gone from the history: it put
"Tailspin Denver Networking Session" in the Fabrikam event's slot, said "You're
free then", renamed the wrong event into a duplicate, and could not say what
it had made. A line per action -- what, when, its id -- costs a few dozen
tokens and is what "the event you added" refers to.

And whose words and when. An unprompted message read as an answer, and
carried no time, nor did a replayed question, so after a restart every
earlier "tomorrow" was any day. `asked` and `unprompted` say who and when.
"""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from agent.cut import cut

# The latest turn's tool results, each, at most: enough for a follow-up
# about what was found; the rest of a page is in the reply or not needed.
LAST_TURN_TOOL_CHARS = 4_000
# Older turns, as `_resume` cuts them (agent/loop.HISTORY_MAX_CHARS).
TEXT_CHARS = 4_000
# Turns kept at most, as `_resume` keeps 30 messages.
MAX_TURNS = 15

# GPT's own citation markers -- "citeturn0search1" --
# which leaked into two replies on Signal in the two weeks to 5 October 2026.
_CITATION = re.compile("[^]*")


# The clock line each turn's request carries (agent/loop.py `_now_line`),
# once that turn is over. Left whole, every earlier request kept its ~90
# tokens of "Right now it is ... Resolve every relative date against this":
# fifteen turns of history made fifteen claims about what time it is now,
# each one wrong but the last. Kept as when the user asked, which is all an old
# turn's clock is still good for ("yesterday you asked about...").
_CLOCK = re.compile(r"\n\n\[Right now it is (\w+ \d{1,2} \w+ \d{4}, \d{2}:\d{2}) .*\]\Z",
                    re.DOTALL)


def as_asked(text: str) -> str:
    """An earlier turn's words, its clock line cut to when it was asked."""
    return _CLOCK.sub(lambda m: f"\n\n[asked {m.group(1)}]", text)


def _words(content) -> str:
    """A request's text: all of it, or the text parts of one sent with
    photos."""
    if isinstance(content, str):
        return content
    return "\n".join(item for item in content if isinstance(item, str))


# --------------------------------------------------------------------------
# Who said it, and when
# --------------------------------------------------------------------------

# How a moment is written in the history: as `_now_line` writes it, so a
# replayed "[asked ...]" reads like a live one.
WHEN = "%A %d %B %Y, %H:%M"

# An unprompted report about the agent itself, kept to its headline: on 7
# October 2026 one "Upgrade #88 did not go in", with its pytest output, was
# 1,414 characters of the resumed window.
# The `model` recorded on a stored assistant message no model wrote in reply
# to them there and then. Read back by the loop's replay and by `context`;
# here, so neither imports the other for them. A ping, a card, a briefing,
# put into the conversation by `note_said`:
UNPROMPTED = "(unprompted)"
# and the turn closing what the user said in a live conversation at the desk that
# ended before it was answered (worker/desk.py `to_thread`).
LIVE = "(live)"

SELF_REPORT = re.compile(r"^Upgrade #\d+ ")
SELF_REPORT_CHARS = 300


def _zone():
    """Where the user is: the zone the clock line is written in (loop._now_line)."""
    from agent import zones

    return zones.local_zone()


def local(stamp: str | datetime | None) -> datetime | None:
    """A stored moment -- SQLite UTC text, or an aware time -- where the user is."""
    from agent import sqltime

    if stamp is None:
        return None
    try:
        moment = sqltime.moment(stamp) if isinstance(stamp, str) else stamp
        return moment.astimezone(_zone())
    except (AttributeError, TypeError, ValueError):
        return None


def clock(stamp: str | datetime | None, fmt: str = WHEN) -> str | None:
    """A stored moment on their clock, in the history's words (`WHEN`) unless
    `fmt` says otherwise; None if it is not a moment."""
    moment = local(stamp)
    return moment.strftime(fmt) if moment else None


def asked(text: str, at: datetime | None) -> str:
    """Their stored words with when the user said them, as a live turn carries it."""
    return f"{text}\n\n[asked {at.strftime(WHEN)}]" if at else text


def unprompted(text: str, at: datetime | None) -> str:
    """Something said to them outside a turn, marked as such, with its time.
    A report about the agent itself keeps its headline only."""
    if SELF_REPORT.match(text):
        text = cut(text.split("\n\n", 1)[0], SELF_REPORT_CHARS,
                   mark=" […the rest of this report is not replayed]")
    when = f", {at.strftime(WHEN)}" if at else ""
    return f"[sent unprompted{when}] {text}"


# The notes above sit on an assistant's earlier messages, where a model that
# writes the next one can copy them: a reply opening "[sent unprompted, Tue
# 08:00]" would reach their phone with the history's own annotation on it.
# (The receipts, which a reply could copy as easily, are put on their side of
# the history for that reason.) Cut from the front of a reply, and only there.
NOTE = re.compile(r"\s*\[(?:sent |asked [A-Z])[^\]]*\]\s*")


def _unnote(text: str) -> str:
    while found := NOTE.match(text):
        text = text[found.end():]
    return text


# What `Conversation.cut_short` writes after the part of a spoken reply the user
# heard. Like the notes above it sits on an assistant message, so a model can
# copy it -- anywhere in a reply, not only at the front -- and a streamed
# voice reply said it aloud (audit of 8 October 2026). `clean` takes it out
# wherever it is.
CUT_NOTE = "[the user cut in here; the rest was not said]"
_CUT_NOTE = re.compile(r"\s*\[the user cut in here[^\]]*\]")


# --------------------------------------------------------------------------
# What an earlier turn did
# --------------------------------------------------------------------------

# The actions whose effect a later turn has to know of: what it created,
# changed, drafted, sent, set or signed in to.
RECEIPT_TOOLS = frozenset({
    "create_event", "update_event", "delete_event", "draft_email", "send_email",
    "send_draft", "remind_me", "add_task", "browse_use_login", "allow_links",
})
RECEIPT_CHARS = 160
RECEIPT_HEAD = "[your tools, for this: "

# What says which thing it was, in the order worth reading.
_WHAT = ("summary", "title", "subject", "to", "what", "task", "start", "when", "due_date",
         "event_id", "site", "url")
_WHICH = ("id", "event_id", "draft_id", "message_id", "commitment_id")


def as_dict(value) -> dict:
    """A tool call's arguments or result as stored: a dict as it is, the
    JSON text of an object parsed, and anything else -- not JSON, a list,
    nothing kept -- as no arguments, rather than an error in whatever is
    reading an old conversation."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def receipt(tool: str, args, result) -> str:
    """One line of what an action did -- `[your tools, for this:
    create_event "Fabrikam Denver" 2026-10-15T16:00 -> done, id 24rkm]` --
    from its call's arguments and what it returned."""
    called = as_dict(args)
    said = as_dict(result)
    bits = []
    for key in _WHAT:
        value = called.get(key)
        if value not in (None, "", [], {}):
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            bits.append(f'"{cut(text, 60)}"' if key in ("summary", "title", "subject")
                        else cut(text, 60))
    if said.get("status") in ("awaiting_approval", "already_queued"):
        outcome = "waiting for their yes"
    elif said.get("ok") is False or said.get("error"):
        outcome = f"failed: {cut(str(said.get('error') or 'no reason given'), 60)}"
    else:
        which = next((f"{key} {said[key]}" for key in _WHICH if said.get(key)), "")
        outcome = "done" + (f", {which}" if which else "")
    line = " ".join([tool, *bits[:4]]) + f" -> {outcome}"
    return f"{RECEIPT_HEAD}{cut(line, RECEIPT_CHARS)}]"


def _receipts(turn: list[ModelMessage]) -> list[str]:
    """A receipt for each action this turn's tools took, in order."""
    calls = {p.tool_call_id: p for m in turn if isinstance(m, ModelResponse)
             for p in m.parts if isinstance(p, ToolCallPart)}
    out = []
    for message in turn:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, ToolReturnPart) and part.tool_name in RECEIPT_TOOLS:
                call = calls.get(part.tool_call_id)
                out.append(receipt(part.tool_name, call.args if call else None, part.content))
    return out


def clean(text: str) -> str:
    """A reply without the model's private citation markers, and without a
    note of the history's that it copied onto its own opening (`NOTE`) or the
    one a cut-off reply carries (`CUT_NOTE`) wherever it put it."""
    if text:
        text = _unnote(text)
        if "[the user cut in here" in text:
            text = _CUT_NOTE.sub("", text).strip()
    if "" not in (text or ""):
        return text
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", _CITATION.sub("", text)).strip()


def _turns(history: list[ModelMessage]) -> list[list[ModelMessage]]:
    """The history cut where the user speaks: each turn starts with their words."""
    turns: list[list[ModelMessage]] = []
    for message in history:
        starts = isinstance(message, ModelRequest) and any(
            isinstance(p, UserPromptPart) for p in message.parts)
        if starts or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def _said(turn: list[ModelMessage]) -> list[ModelMessage]:
    """A turn as words only: their request, and every reply that called no
    tool -- the answer, and anything said to them after it -- with a line on
    their request for each action it took (`receipt`)."""
    out: list[ModelMessage] = []
    receipts = _receipts(turn)
    for message in turn:
        if isinstance(message, ModelRequest):
            # Words only: a photo sent with them (channels/photos.py) is shown
            # on its own turn, and kept, it would be sent and billed again on
            # every turn after. Their words say it came ("[1 photo attached]").
            words = [dataclasses.replace(p, content=cut(as_asked(_words(p.content)), TEXT_CHARS))
                     for p in message.parts if isinstance(p, UserPromptPart)]
            if words and receipts:
                # On their request, not as words of the agent's: a reply that
                # began "[your tools..." would be copied into the next one.
                words += [UserPromptPart(content=line) for line in receipts]
                receipts = []
            if words:
                out.append(ModelRequest(parts=words))
        elif isinstance(message, ModelResponse):
            if any(isinstance(p, ToolCallPart) for p in message.parts):
                continue  # "let me check", on the way to the answer
            text = [dataclasses.replace(p, content=cut(p.content, TEXT_CHARS))
                    for p in message.parts if isinstance(p, TextPart) and p.content.strip()]
            if text:
                out.append(ModelResponse(parts=text, model_name=message.model_name))
    return out


def _capped(turn: list[ModelMessage]) -> list[ModelMessage]:
    """The latest turn whole, its tool results cut to LAST_TURN_TOOL_CHARS
    and its photos left out (see `_said`)."""
    out: list[ModelMessage] = []
    for message in turn:
        if isinstance(message, ModelRequest):
            out.append(dataclasses.replace(message, parts=[_kept(p) for p in message.parts]))
        else:
            out.append(message)
    return out


def _kept(part):
    """One part of the latest turn's requests, as the next turn reads it."""
    if isinstance(part, ToolReturnPart):
        return dataclasses.replace(part, content=cut(part.model_response_str(),
                                                     LAST_TURN_TOOL_CHARS))
    if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
        return dataclasses.replace(part, content=_words(part.content))
    return part


def compact(history: list[ModelMessage]) -> list[ModelMessage]:
    """The history the next turn starts from: earlier turns as words, the
    latest with its tool results cut down, at most MAX_TURNS turns."""
    turns = _turns(history)[-MAX_TURNS:]
    out: list[ModelMessage] = []
    for i, turn in enumerate(turns):
        out.extend(_capped(turn) if i == len(turns) - 1 else _said(turn))
    return out
