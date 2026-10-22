"""The research block: topics the agent picks for itself, read and remembered.

Cheap and unable to break anything, and what it learns lands in the
memories repo where the improvement block can see it (`tinkering`).
"""

from __future__ import annotations

import logging
import sqlite3

from pydantic import BaseModel, Field

from agent.night import model
from agent.night.report import Budget, NightReport

log = logging.getLogger("night")

# A ceiling, so a cheap night cannot turn into a long one.
MAX_TOPICS = 4


class Topics(BaseModel):
    """What the agent decided to read about."""

    topics: list[str] = Field(min_length=1, max_length=MAX_TOPICS)
    why: str = Field(default="", max_length=400)


# What a night's research uses: the web, and the memory it writes to. Every
# request carried the schemas of all ~77 tools, an estimated 11,000 tokens, to use
# these five (5 October 2026) -- and a topic the agent chose for itself had
# their mail, calendar and phone within reach. Besides them, since 11 October
# 2026, a browsing worker for a page that needs a real browser
# (`workers.send_browsing`).
RESEARCH_TOOLS = frozenset({"web_search", "read_page", "search_and_read", "research", "recall",
                            "remember"})


async def research(
    conn: sqlite3.Connection, budget: Budget, report: NightReport
) -> None:
    """Read about things, and keep what is worth keeping."""
    from agent import workers
    from agent.loop import UNATTENDED, Conversation

    try:
        chosen, cost = await model.ask(
            Topics,
            f"{model.context(conn)}\n\nPick up to {MAX_TOPICS} topics for tonight.",
            model.prompt("night_topics"),
        )
        budget.add(cost)
    except Exception as exc:  # noqa: BLE001
        log.exception("could not choose topics")
        report.stopped_because = f"topic choice failed: {type(exc).__name__}"
        return

    for topic in chosen.topics[:MAX_TOPICS]:
        if budget.exhausted(headroom=0.05):
            report.stopped_because = "research budget spent"
            return
        try:
            convo = Conversation(conn, channel="night")
            convo.start = UNATTENDED
            reply = await workers.send_browsing(
                convo,
                f"Research this and write down what you learn: {topic}\n\n"
                "Search the web, read what you find, then use your memory tool to "
                "remember the two or three things most worth keeping. Be specific "
                "and note where each claim came from.",
                RESEARCH_TOOLS,
            )
            convo.end()
            budget.add(reply.cost_usd or 0.0)
            report.researched.append(topic)
            # Not into `learned`. That field was this block's bucket first,
            # and the learning block later took it over for "what I found
            # out about the user" -- so the morning digest ran three real
            # lines and then two 300-character essays, which pushed the
            # real ones past the cap. The substance of a research topic is
            # in the memories it wrote; the digest wants the title.
        except Exception as exc:  # noqa: BLE001 - one bad topic is not the night
            log.exception("research failed for %r", topic)
            report.researched.append(f"{topic} (failed: {type(exc).__name__})")
