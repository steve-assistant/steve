"""Their notes, as the assistant can read them: "what did I note about the
plumber?" Taking them is the desk's, by voice and by code (agent/notes.py,
agent/worker/desk.py); here they are only found.

A quick note and a dictation are the user's words. A lecture's, a
meeting's or a call's notes are a summary of what everyone there said, and
reach the model as the injection screen allows (agent/screening.py): a
voice on a call can address an AI as well as an email can, and a summary
can carry its words on. They reached the model unscreened until the tools
were made to declare what they return.
"""

from __future__ import annotations

import asyncio

from pydantic_ai import RunContext

from agent import notes, offloop, screening
from agent.turn import Deps

# The kinds of notes that hold other people's words (agent/notes.py `start`).
OF_A_ROOM = frozenset({"lecture", "call"})


async def _screened(conn, found: list[dict]) -> list[dict]:
    """Notes as the model may read them: a room's or a call's title and
    summary, screened as one item, or what the screen allows in their
    place; the user's own as they are."""
    theirs = [n for n in found if n.get("kind") in OF_A_ROOM]
    seen = await asyncio.gather(*(screening.shown(
        conn, "meeting_notes", n.get("id"), None,
        f"{n.get('title') or ''}\n\n{n.get('text') or ''}") for n in theirs))
    for note, shown in zip(theirs, seen, strict=True):
        if shown.how != "text":
            note.pop("title", None)
            note.update(screened=shown.how, text=shown.text)
    return found


def register(tool) -> None:
    """Attach the notes tool. `tool` is `register_tools`'s filter."""

    @tool
    async def find_notes(ctx: RunContext[Deps], query: str = "", limit: int = 10) -> list[dict]:
        """Search the notes the user took by voice at their computer: quick
        notes, dictations, and notes of a meeting, a class or a call (their
        summaries and transcripts). An empty query lists the most recent.
        """
        if not query.strip():
            found = await offloop.with_own_conn(ctx.deps.conn, notes.recent, limit)
        else:
            found = await offloop.with_own_conn(ctx.deps.conn, notes.find, query, limit)
        return await _screened(ctx.deps.conn, found)
