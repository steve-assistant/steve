"""People and what is owed to them: who someone is, commitments, and tasks."""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from pydantic_ai import RunContext

from agent.actions import act, executable
from agent.approvals import one_line
from agent.toolset.clients import commitment_store
from agent.turn import Deps

log = logging.getLogger("toolset.people")

# How many ledger rows `who_is` weighs before showing the best four.
WHO_IS_LOOKED_AT = 24


@executable("add_task")
async def _do_add_task(*, task: str, due_date: str | None = None) -> dict:
    """Record the commitment, and put it on their phone now.

    One system of record: this writes the row, and `agent/taskmirror.py`
    mirrors it outward. Creating the task directly would make Tasks a second
    store, and the failure mode of two stores is an item in the wrong one,
    which is never surfaced at all.

    Mirrored here rather than left to the sync, which ran at 3am: on 25
    September three tasks asked for at 9pm were "added to your Tasks" and
    were not there when the user looked. `on_phone` says whether it is there
    yet, so the reply can say so truthfully.
    """
    from agent import offloop, taskmirror
    from tools import commitments

    conn, borrowed = commitment_store()
    try:
        commitment_id = commitments.add(
            conn,
            text=task,
            # The user said it, so it is its own evidence. The verbatim-quote rule
            # exists to stop a *model* inventing obligations from prose; one
            # the user dictated has nothing to fabricate.
            quote=task,
            due_date=due_date,
            source="conversation",
            source_id=f"said:{task[:60]}",
        )
        if commitment_id is None:
            return {"ok": True, "already_tracked": True}
        mirrored = await offloop.with_own_conn(conn, taskmirror.mirror, commitment_id)
    finally:
        # The early return used to skip this, leaving a borrowed
        # connection open for the life of the process.
        if borrowed:
            conn.close()
    result = {"ok": True, "commitment_id": commitment_id, "due_date": due_date,
              "on_phone": bool(mirrored.get("on_phone"))}
    if not result["on_phone"]:
        result["note"] = ("Tracked, but not on their tasks list yet; "
                          "the hourly sync will try again.")
    return result


@executable("complete_task")
async def _do_complete_task(*, commitment_id: int) -> dict:
    from agent import taskmirror
    from tools import commitments

    conn, borrowed = commitment_store()
    try:
        existing = commitments.get(conn, int(commitment_id))
        closed = commitments.resolve(conn, int(commitment_id))
        # Tick the mirror too, or their phone keeps showing a thing the user has done.
        if closed and existing and taskmirror.mirrored(existing.task_id):
            await taskmirror.tick(conn, existing.task_id)
    finally:
        if borrowed:
            conn.close()
    return {"ok": closed, "commitment_id": commitment_id}


@executable("keep_open_item")
async def _do_keep_open_item(*, obligation_id: int) -> dict:
    """Their YES to a card asking whether the user still wants something
    (`agent/closing.briefing_questions`): it is kept open. Not a tool the model
    holds; only code raises the card."""
    from agent import closing

    conn, borrowed = commitment_store()
    try:
        return await closing.keep(conn, int(obligation_id))
    finally:
        if borrowed:
            conn.close()


async def _screened(conn, person: dict) -> dict:
    """A person from the ledger, with what their own mail wrote as the
    injection screen allows (agent/screening.py): their name is the one
    their messages gave, and the subjects are those of mail they sent, or
    replies to it. One the screen did not pass keeps their address, their
    counts and when each exchange was, and loses their name and the
    subjects to what the screen allows. Both reached the model unscreened,
    and left the conversation free to open any address, until step 11's
    audit."""
    from agent import screening

    words = "\n".join([person.get("name") or "",
                       *(row.get("subject") or "" for row in person["recent"])])
    shown = await screening.shown(conn, "email_preview", person["id"],
                                  person.get("email"), words)
    if shown.how != "text":
        person.pop("name", None)
        person["recent"] = [{k: v for k, v in row.items() if k != "subject"}
                            for row in person["recent"]]
        person.update(screened=shown.how, text=shown.text)
    return person


async def _named(conn, found: list) -> list[dict]:
    """Commitments as the model reads them, with the name of the person each
    is owed to as the injection screen allows (agent/screening.py): it is
    the name the ledger keeps from their own mail, as `who_is` shows it
    (`_screened`). A commitment and its quote are the user's words, from
    their sent mail with any quoted reply cut (tools/extract.py). The names
    reached the model unscreened until the tools were made to declare what
    they return."""
    from agent import screening

    names = {c.person_id: c.person for c in found if c.person}
    seen = dict(zip(names, await asyncio.gather(*(
        screening.shown(conn, "email_preview", person_id, None, name)
        for person_id, name in names.items())), strict=True))
    out = []
    for commitment in found:
        item = commitment.as_dict()
        shown = seen.get(commitment.person_id) if commitment.person else None
        if shown is not None and shown.how != "text":
            # In the name's place: `text` is the user's commitment.
            item.update(person=shown.text, screened=shown.how)
        out.append(item)
    return out


def register(tool) -> None:
    """Attach the people tools. `tool` is `register_tools`'s filter."""

    @tool
    async def who_is(ctx: RunContext[Deps], query: str) -> dict:
        """Look someone up: who they are, and what they and the user have said
        to each other.

        Answers from the people ledger -- everyone the user has been in
        touch with, as you have seen it -- so "who is Sam from the tenants'
        association" needs no web search. Returns their recent exchanges and
        anything the user owes them.

        Pass one distinctive word -- a surname, an organisation, a domain.
        This matches substrings, so "northwind" finds sam@northwind.example
        and the whole question does not. If two words might work, call it
        twice.

        Each match says how it matched: "exact" when your word is a whole
        word of their name, address or organisation, "partial" when it is
        only inside one -- "chris" inside "Christine". A partial match is
        someone it might be, not who it is: never say a person is them on
        that alone. If it comes back empty, or only partial, say you do not
        know who they are rather than guessing from the name.
        """
        from tools import commitments, people

        # `find` ranks by how much mail a person has, so the busiest few
        # "chris" are Christines and the one Alex would be cut at four. Ask
        # for more, and let a whole-word match come first (the sort is
        # stable, so each group keeps its order).
        found = people.find(ctx.deps.conn, query, limit=WHO_IS_LOOKED_AT)
        if not found:
            return {"found": 0, "query": query, "people": []}
        rated = sorted(
            ((person, people.match_quality(person, query)) for person in found),
            key=lambda pair: pair[1] != "exact",
        )[:4]
        result: dict = {
            "found": len(rated),
            "people": [
                {
                    **person.as_dict(),
                    "match": quality,
                    "recent": people.history(ctx.deps.conn, person.id, limit=5),
                    # Without their name: these are theirs, and the name is
                    # screened with their mail (`_screened`), which a line
                    # carrying it again would pass round.
                    "owed": [
                        dataclasses.replace(c, person=None).line()
                        for c in commitments.for_person(ctx.deps.conn, person.id)
                    ],
                }
                for person, quality in rated
            ],
        }
        result["people"] = await asyncio.gather(
            *(_screened(ctx.deps.conn, person) for person in result["people"]))
        if all(quality == "partial" for _, quality in rated):
            result["note"] = (
                "Only partial matches: the word is inside a longer name or address, "
                "not a whole word of one. None of them is known to be who was meant."
            )
        return result

    @tool
    async def my_commitments(ctx: RunContext[Deps], limit: int = 15) -> dict:
        """Things the user said they would do, soonest first.

        Every one carries the sentence it came from. Quote that sentence if
        the user questions an item -- it is the whole reason the list is
        trustworthy.
        """
        from tools import commitments

        found = commitments.open_ones(ctx.deps.conn, limit=limit)
        return {
            "count": len(found),
            "commitments": await _named(ctx.deps.conn, found),
        }

    @tool
    async def commitment_done(
        ctx: RunContext[Deps], commitment_id: int, dropped: bool = False
    ) -> dict:
        """Close a commitment, because the user said it is done or off.

        Only when they say so, never on an inference.
        """
        from agent import closing
        from tools import commitments, obligations

        ok = commitments.resolve(
            ctx.deps.conn, commitment_id, status="dropped" if dropped else "done"
        )
        if ok and (closed := obligations.get(ctx.deps.conn, commitment_id)) is not None:
            # Their phone stopped showing it only when the user ticked it there too.
            closing.untrack_soon(closed)
        return {
            "ok": ok,
            "commitment_id": commitment_id,
            "error": None if ok else "no open commitment with that id",
        }

    @tool
    async def add_task(
        ctx: RunContext[Deps], task: str, due_date: str | None = None
    ) -> dict:
        """Add something the user has to do.

        This records a commitment; there is no separate "task" to manage. It
        comes up in the morning briefing when it is about to be late. Where
        the user keeps a task list you can reach, it goes there too, at
        once: the result's `on_phone` says whether it did, and only then say
        it is on their list.

        `due_date` is YYYY-MM-DD. Leave it out if they did not give one.

        For "remind me at 5pm" use `remind_me` instead. That is an alarm;
        this is an obligation.
        """

        args = {"task": task, "due_date": due_date}
        return await act(
            ctx, "add_task", args,
            # On one line (`approvals.one_line`), as every card's part the
            # model wrote: a line break in it made lines of its own on the card.
            preview=f"Track: {one_line(task)}"
                    + (f" (due {one_line(due_date)})" if due_date else ""),
        )

    @tool
    async def complete_task(ctx: RunContext[Deps], commitment_id: int) -> dict:
        """Mark something done, because the user said it is.

        Same thing as `commitment_done`; this name exists because people
        think of them as tasks. Call `my_commitments` first for the id.
        """
        return await act(
            ctx, "complete_task", {"commitment_id": commitment_id},
            preview=f"Close commitment {commitment_id}",
        )

    @tool
    async def list_tasks(ctx: RunContext[Deps]) -> dict:
        """What is on the user's tasks list right now.

        Reads the list rather than the ledger, so it shows anything they added
        on their phone too. For what you are tracking, use `my_commitments`
        -- that is the record, this is the surface.
        """
        from agent import offloop, taskmirror

        return await offloop.with_own_conn(ctx.deps.conn, taskmirror.open_tasks)
