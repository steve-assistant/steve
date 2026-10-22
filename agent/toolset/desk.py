"""At the desk: taking notes, and -- under decision A only -- talking live.

`take_notes` is for notes asked for in their own words. The desk catches "take
notes on this meeting" in code, with no model; on 7 October 2026, minutes
before a Zoom meeting, "I'm going to need you to turn on the meeting
transcription" was not caught, and the model, with no way to start notes,
told them to try Zoom's captions and queued a change to its own code.
Offered in their conversations (`call_policy.PRESENT_CHANNELS`), never to
work nobody is watching: notes hold other people's words.

`talk_it_through` let their assistant open a live conversation on GPT-Live
when it judged a request needed a back-and-forth. On 9 October 2026 it
opened two that repeated what the local voice had just said, and the user
chose that a live conversation opens only when the user asks for one by name
(voice/engine.py `LIVE_OPENED_BY`); the back-and-forth is the local
follow-up window. It is kept for that one constant's other setting, and is
not offered otherwise (`toolset.offered`). All it does is ask, on the
turn's own record: the desk opens the conversation once the turn is over,
on the reply it has just said, as history.
"""

from __future__ import annotations

from pydantic_ai import RunContext

from agent import runtime
from agent.turn import Deps

NAMES = frozenset({"talk_it_through"})
NOTE_NAMES = frozenset({"take_notes"})


def live_by_assistant() -> bool:
    """Whether their assistant may open a live conversation (decision A)."""
    from voice import engine

    return engine.LIVE_OPENED_BY != "user"


def register(tool) -> None:
    """Attach the desk's tools. `tool` is `register_tools`'s filter."""

    @tool
    async def talk_it_through(ctx: RunContext[Deps]) -> dict:
        """Carry this on out loud as a live conversation, when what the user
        asked needs a back-and-forth: planning something together, writing
        something with them, quizzing them, or several questions you need to
        ask. Only at their desk. Your reply to this turn is said first, and
        the conversation carries on from it: end it with your first question.
        """
        if ctx.deps.channel != "voice":
            return {"ok": False, "error": "only in a spoken turn at the user's desk"}
        if not live_by_assistant():
            return {"ok": False, "error": "a live conversation opens only when the user asks "
                                          "for one by name: just answer this one"}
        # Only a turn the desk's engine is running reads what it asks, once
        # it is over; set on a hotkey turn or a live conversation's hand-over
        # it was read by nobody, and opened a billed conversation on the next
        # unrelated "hey Steve" (audit of 8 October 2026).
        turn = runtime.desk_turn.get()
        if turn is None or turn.source not in ("wake", "follow"):
            return {"ok": False, "error": "only when the user says hey Steve at their desk; "
                                          "just answer this one"}
        # The user cut in over a reply: the capture holds the desk's own voice, and
        # on 9 October 2026 "stop" arrived as "I'll talk." and opened a billed
        # conversation.
        if turn.cut_in:
            return {"ok": False, "error": "the user cut in over a reply, so a live "
                                          "conversation is not opened for this: just answer "
                                          "it; they can say let's talk live if they want one"}
        turn.asks["live"] = True
        return {"ok": True, "note": "a live conversation opens after this reply"}

    @tool
    async def take_notes(ctx: RunContext[Deps], on: bool = True, source: str = "laptop",
                         title: str | None = None) -> dict:
        """Start or stop taking notes on a meeting, call, class or video,
        however the user puts it: "transcribe this meeting", "turn on the
        meeting transcription", "take minutes", "stop the notes".

        `source` is where the speech is. "laptop": a Zoom, Teams or Meet
        call, a video, anything the computer plays -- no microphone needed,
        and the user's own side is added when hands-free is on. "room":
        people talking where they are, through their microphone (needs
        hands-free listening, which this version does not offer). `title`
        names the notes ("Tenants' meeting").

        Only text is kept, on this computer; when they stop, the transcript
        is summarised and the user is told where it is. Recording other
        people may need their consent, which is the user's to ask for.
        """
        if source not in ("laptop", "room"):
            return {"ok": False, "kind": "usage", "error": "source is 'laptop' or 'room'"}
        from agent.worker import desk

        return await desk.notes_asked(source, on, title=title, channel=ctx.deps.channel,
                                      asked=ctx.deps.asked)
