"""Durable memory: what the user has told the agent before.

Two stores behind two tools. Facts (agent/facts.py) are typed lines --
whom, what, from where, until when -- and a turn is shown the ones its
message calls for by code, not by the model thinking to look. Notes are
the markdown memories: their corrections of how the agent works, a project's
state, what the night read.

`remember` once kept every fact about the user as a `user` paragraph, which put
it in front of every turn: on 8 October 2026 "Owen Tate's Northwind email
format is lastname.firstname@northwind.example" became a standing fact about the user
(memory 223). It now keeps a fact under whom it is about, reroutes an
address format to its firm, refuses one from text the user pasted, and says the
number of what it kept. Correcting and forgetting a fact are part of it
(`replaces`, `forget`) rather than tools of their own: every tool needs a
consent class in `core/consent.yaml`, which is frozen, and `remember`'s --
private, reversible -- is already theirs.
"""

from __future__ import annotations

import logging
import sqlite3

from pydantic_ai import RunContext

from agent import offloop
from agent.turn import Deps
from tools import memory

log = logging.getLogger("toolset.memory")

# How far behind the best match a memory may be and still be shown.
#
# Not a floor. The embedder's similarities sit between about 0.51 and 0.68
# whatever is asked, and an absolute cut cannot tell a hit from a miss: on
# their own memories (6 October 2026) "the user's partner" matched "the user's
# advisor is Mira Shah" at 0.592, above the right answer to "the user's class
# schedule" at 0.547. What does separate them is the gap to the best match
# -- the advisor at 0.683 for "the user's advisor", the next at 0.579 -- so a
# clear hit comes back alone. A query nothing answers still comes back as a
# few near-equal guesses (or one wrong one), which is why `recall`'s
# docstring has the model read each against the question. Every call used
# to return five memories; on fifteen of their questions the median answer
# went from 1,440 characters to 850, and "the user's advisor", "how the user
# writes email" and "what classes is the user taking" from five to one.
# (The scores above are recall's own, 1 / (1 + distance), from the local
# embedder these vectors replaced. There is a floor now as well, on cosine
# similarity: `memory.RELEVANCE_FLOOR`. On OpenAI's vectors, measured on 12
# October 2026 against every note of the labelled set, the right note came
# first for all ten queries a note answered, and above the floor only one
# query found a second note -- a near one, 0.014 behind, which the band
# keeps. The band still keeps a clear hit alone.)
RECALL_BAND = 0.03
# A memory's body as recall shows it; the night's research notes run long.
RECALL_BODY_CHARS = 1_500


def shown(found: list) -> list[dict]:
    """`memory.recall`'s answer as the model gets it: the close matches only,
    a summary once however many files repeat it, bodies cut."""
    from agent.cut import cut

    if not found:
        return []
    top = max((m.score or 0.0) for m in found)
    out: list[dict] = []
    summaries: set[str] = set()
    for item in found:
        if (item.score or 0.0) < top - RECALL_BAND:
            continue
        said = " ".join((item.summary or "").lower().split())
        if said in summaries:
            continue
        summaries.add(said)
        entry = item.as_dict()
        entry["body"] = cut(entry.get("body") or "", RECALL_BODY_CHARS)
        out.append(entry)
    return out


def recalled(conn: sqlite3.Connection, query: str, *, limit: int = 5, about: str = "any",
             past: bool = False) -> list[dict]:
    """What `recall` answers: the facts that match, each with its number and
    why, then the memories above the floor. Ways of working (`about="how"`)
    are notes, not facts, and bring back the research notes and the judge's
    lessons, which are otherwise left out. Blocking: it embeds the query."""
    from agent import facts

    about = about if about in memory.ABOUT else "any"
    out: list[dict] = []
    if about != "how":
        try:
            out = [{"fact": fact.id, "says": facts.render_line(fact, why)}
                   for fact, why in facts.search(conn, query, limit=limit)]
        except Exception:  # noqa: BLE001 - the notes can still answer
            log.warning("could not search the facts", exc_info=True)
    try:
        found = memory.recall(conn, query, limit=limit, kinds=memory.ABOUT[about],
                              lessons=about == "how", research=about == "how", past_too=past)
    except memory.EmbedderUnavailable:
        # The facts found still answer. With none, the tool says what is not
        # set up (agent/toolset `_set_up`).
        if not out:
            raise
        log.info("recall: the notes need the embedder; the facts answer alone")
        found = []
    return out + shown(found)


def _source(channel: str) -> str:
    """Kept on their own channels, a fact is their word. Kept anywhere else --
    a job, the initiative loop -- it is the agent's inference, and never in
    the profile every turn reads (agent/facts.py `PROFILE_SOURCES`)."""
    from core.call_policy import PRESENT_CHANNELS

    return "user_words" if channel in PRESENT_CHANNELS else f"inferred:{channel or 'turn'}"


def _their_words(conn: sqlite3.Connection, session_id: int, asked: str) -> str:
    """What the user wrote in this turn and the two before it: "I moved to
    Leeds", then "remember that"."""
    rows = conn.execute("SELECT content FROM messages WHERE session_id = ? AND role = 'user'"
                        " ORDER BY id DESC LIMIT 3", (session_id,)).fetchall()
    return "\n".join([asked or "", *(r[0] or "" for r in rows)])


def _user_message(conn: sqlite3.Connection, session_id: int) -> str | None:
    row = conn.execute("SELECT MAX(id) FROM messages WHERE session_id = ? AND role = 'user'",
                       (session_id,)).fetchone()
    return f"message:{row[0]}" if row and row[0] else None


def keep_fact(conn: sqlite3.Connection, *, subject: str, attribute: str, value: str,
              until: str | None, channel: str, session_id: int, asked: str) -> dict:
    """`remember`'s fact, kept -- or refused, with why, for the model to fix.
    Blocking: it embeds the fact."""
    from agent import facts

    source = _source(channel)
    stored, _ = facts.subject_of(conn, subject)
    if stored == facts.USER:
        if why := facts.misfiled(stored, attribute, value, conn=conn):
            return {"kept": None, "error": why}
        if source == "user_words" and facts.pasted(asked):
            # Material the user passed on, not their words about themselves: kept, and
            # found when a message calls for it, never in the profile.
            source = "inferred:paste"
        if source == "user_words" and facts.unchecked(stored, value, conn=conn):
            source = "inferred:address"
        if (source == "user_words" and facts.is_identity(stored, attribute)
                and not facts.said_in(value, _their_words(conn, session_id, asked))):
            # Their channel, but not their words: a turn that read an email
            # or a page can be talked into keeping what it says as who the
            # user is. And their words for it alone: the value is what the
            # forecast, the profile and their name as a subject read.
            return {"kept": None, "error": facts.IN_THEIR_WORDS.format(attribute=attribute)}
    fact = facts.add(conn, subject, attribute, value, source=source,
                     source_ref=_user_message(conn, session_id), valid_until=until)
    return {"kept": fact.id, "fact": facts.render_line(fact)}


def correct_fact(conn: sqlite3.Connection, number: int, value: str, *, channel: str,
                 session_id: int) -> dict:
    """A fact's new value, the old one superseded. Blocking: it embeds."""
    from agent import facts

    source = _source(channel)
    fact = facts.correct(conn, number, value, source=source,
                         source_ref=_user_message(conn, session_id))
    return {"kept": fact.id, "fact": facts.render_line(fact), "replaced": number}


def forget_fact(conn: sqlite3.Connection, number: int) -> dict:
    """Drop the fact (or memory) with this number, as FORGET MEMORY does."""
    from agent import facts

    said = facts.forget_number(conn, number)
    if said is None:
        return {"forgotten": None, "error": f"Nothing is kept as number {number}."}
    return {"forgotten": number, "was": said}


def register(tool) -> None:
    """Attach the memory tools. `tool` is `register_tools`'s filter."""

    @tool
    async def recall(ctx: RunContext[Deps], query: str, limit: int = 5,
                     about: str = "any", past: bool = False) -> list[dict]:
        """Search what you know: first the facts kept, each with its number
        and why it matched, then the notes close to the question.

        `about` narrows the notes: "user" for notes about the user; "how" for
        ways of working -- their corrections, lessons from past conversations,
        research notes -- and then notes only; "any" (the default) for both,
        without the lessons and the research notes.

        A memory about a date carries `until`, its last day, and once that
        day has gone by it is left out -- a trip that is over is not a plan.
        `past` true brings those back, for a question about what happened
        ("how was the camping trip?"). `created_at` is when it was noted.

        Nothing comes back that is not close to the question: an empty
        answer means nothing kept answers it. Say so rather than guess, and
        read what does come back against the question -- a fact about
        someone else is not an answer about the user.
        """
        # Off the loop: it embeds the query first. See agent/offloop.py.
        return await offloop.with_own_conn(ctx.deps.conn, recalled, query, limit=limit,
                                           about=about, past=past)

    @tool
    async def remember(
        ctx: RunContext[Deps],
        kind: str = "user",
        summary: str = "",
        body: str = "",
        name: str | None = None,
        until: str | None = None,
        subject: str = "user",
        attribute: str | None = None,
        replaces: int | None = None,
        forget: int | None = None,
    ) -> dict:
        """Keep something for later: a fact, or a note.

        A fact (kind "user") is one short line: `summary` is what it says,
        `subject` whom it is about -- "user" (the default), "person:<their
        full name>", "org:<the organisation's domain>" (like
        "org:northwind.example"), or "world" -- and `attribute` a short name
        for what it says: home_city, occupation, allergy, plan. The same
        subject and attribute again replaces the old value. Keep one when the
        user tells you something lasting about themselves, or asks you to
        keep something: a plan with a date (set `until`, its last day,
        YYYY-MM-DD), a standing preference, which account is which. A fact
        about the user is in front of every turn; one about someone else is
        theirs, under their subject.

        Who the user is has fixed attributes -- name, preferred_name,
        pronouns, home_city, occupation -- and their value is the user's own
        words for it and nothing more: "Leeds", "they/them".

        Every fact kept has a number. Say it ("kept as fact 1004"), so
        "FORGET MEMORY 1004" drops it. `replaces`: the number of a fact the
        user says is wrong, with the right value as `summary`. `forget`: the
        number of a fact (or memory) they want dropped; nothing else needed.

        A note (kind feedback, project or reference) is a `summary` and a
        `body`: the user's corrections of how you work, a project's state,
        what you read. The same `name` again updates that note in place.
        """
        deps = ctx.deps
        channel = getattr(deps, "channel", "") or ""
        session_id = getattr(deps, "session_id", 0)
        value = summary.strip() or body.strip()
        # Off the loop, all of it: a fact is embedded, a note embedded and
        # committed to the memory repo. See agent/offloop.py.
        try:
            if forget is not None:
                return await offloop.with_own_conn(deps.conn, forget_fact, int(forget))
            if replaces is not None:
                done = await offloop.with_own_conn(deps.conn, correct_fact, int(replaces), value,
                                                   channel=channel, session_id=session_id)
            elif kind == "user" and channel != "night":
                if not (attribute or name):
                    return {"kept": None, "error": (
                        "A fact needs an `attribute`: a short name for what it says, like "
                        "home_city or preferred_name.")}
                done = await offloop.with_own_conn(
                    deps.conn, keep_fact, subject=subject or "user",
                    attribute=attribute or name, value=value, until=until, channel=channel,
                    session_id=session_id, asked=getattr(deps, "asked", "") or "")
            else:
                return await _keep_note(deps, kind, summary, body, name, until)
        except ValueError as exc:  # a subject, a number or a date it got wrong, said back
            return {"kept": None, "error": str(exc)}
        if done.get("kept") is not None and deps.turn_cache is not None:
            from agent import facts

            deps.turn_cache.setdefault(facts.TURN_KEPT, []).append(done["kept"])
        return done


async def _keep_note(deps: Deps, kind: str, summary: str, body: str, name: str | None,
                     until: str | None) -> dict:
    """A markdown memory, as `remember` kept every one before there were facts."""
    if getattr(deps, "channel", "") == "night":
        # What the night's reading keeps is what it read on the web, not
        # a fact about them: three such notes, filed as `project` on 20-23
        # September 2026, came back for questions about them (`about`).
        kind = "reference"
    # Off the loop: it embeds the text and commits to the memory repo.
    try:
        stored = await offloop.with_own_conn(
            deps.conn, memory.remember, kind=kind, summary=summary, body=body,
            name=name, until=until,
        )
    except ValueError as exc:  # a kind or a date it got wrong, said back to fix
        return {"stored": None, "error": str(exc)}
    until = getattr(stored, "until", None)
    return {"stored": stored.path, "kind": stored.kind, **({"until": until} if until else {})}
