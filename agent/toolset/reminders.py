"""Reminders and keep-checking watches."""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from agent.turn import Deps

log = logging.getLogger("toolset.reminders")


def register(tool) -> None:
    """Attach the reminders tools. `tool` is `register_tools`'s filter."""

    @tool
    async def remind_me(ctx: RunContext[Deps], what: str, when: str) -> dict:
        """Say something back to the user at a given time.

        `when` is an ISO 8601 local datetime you work out yourself from the
        current time, which comes with each of their messages -- "at 5pm
        today" becomes "2026-09-20T17:00:00". Do not pass words like
        "tomorrow"; resolve them.

        `what` is the message they will receive, so write it the way you
        would say it: "grab groceries", not "reminder about groceries".

        A reminder whose time passes while the computer is asleep is not
        lost; it fires when the computer wakes. Say so if they ask.
        """
        from agent import reminders

        try:
            booked = reminders.schedule(
                ctx.deps.conn, kind="remind", text=what, when=when,
                session_id=ctx.deps.session_id,
            )
        except reminders.BadTime as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "id": booked.id, "at": booked.next_fire_at}

    @tool
    async def keep_checking(
        ctx: RunContext[Deps],
        question: str,
        every_minutes: int,
        until: str,
        first_check: str = "",
    ) -> dict:
        """Answer a question repeatedly for a while, reporting changes.

        For "check the score every 10 minutes for the next hour". `until` is
        an ISO 8601 local datetime you resolve yourself; `first_check`
        defaults to now.

        The user gets the first answer, any answer that differs from the one
        before, and the last one -- deliberately not the unchanged ones in
        between, because six messages saying 14-7 bury the one saying 21-7.
        Tell them that is how it will behave.
        """
        from datetime import datetime

        from agent import reminders

        try:
            booked = reminders.schedule(
                ctx.deps.conn, kind="check", text=question,
                when=first_check or datetime.now().isoformat(timespec="seconds"),
                every_minutes=every_minutes, until=until,
                session_id=ctx.deps.session_id,
            )
        except reminders.BadTime as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True, "id": booked.id, "first": booked.next_fire_at,
            "until": booked.until_at, "checks": booked.max_fires,
        }

    @tool
    async def list_reminders(ctx: RunContext[Deps]) -> list[dict]:
        """Everything currently scheduled, with ids for cancelling -- then what
        went off or was cancelled in the last twelve hours (status "done" or
        "cancelled", with when)."""
        from agent import reminders

        return ([r.as_dict() for r in reminders.active(ctx.deps.conn)]
                + reminders.recent(ctx.deps.conn))

    @tool
    async def cancel_reminder(
        ctx: RunContext[Deps],
        reminder_ids: list[int] | None = None,
        cancel_everything: bool = False,
    ) -> dict:
        """Stop reminders and recurring checks. Call `list_reminders` first.

        Pass every id in one call. `cancel_everything` is for "cancel all my
        reminders" and ignores the ids. Cancel only what the user named; if
        it is unclear which they mean, ask.
        """
        from agent import reminders

        if cancel_everything:
            return {"ok": True, "cancelled": reminders.cancel_all(ctx.deps.conn)}

        ids = [int(i) for i in (reminder_ids or [])]
        if not ids:
            return {"ok": False, "error": "give at least one id, or set cancel_everything"}
        stopped = reminders.cancel_many(ctx.deps.conn, ids)
        return {
            "ok": stopped > 0,
            "cancelled": stopped,
            "error": None if stopped else "none of those ids were active",
        }
