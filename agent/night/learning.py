"""The learning block: who the user deals with, what they said they would do, how the user
writes, and what they told the agent about themselves.

First in the night, because it reads the day and the day is over (`learn`).
Ingest is code; only the notes, the commitments and the style guide cost
anything. Sent mail only reaches a model here, and a gate test reads this
package for the query that says so (`tests/gate/test_sent_mail_only.py`).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime

from pydantic import BaseModel, Field

from agent import offloop
from agent.night import model
from agent.night.report import Budget, NightReport

log = logging.getLogger("night")

# Caps for the learning block, in code rather than in the prompt -- the model
# judges what is notable, the code decides how much of it there can be.
MAX_PEOPLE_NOTES = 40
# Not a token budget -- a reading budget: fifteen obligations the user did not
# know they had is already more than a morning can absorb.
MAX_NEW_COMMITMENTS = 15
# Facts kept from their own words in a night, at most.
MAX_HEARD_FACTS = 5


class PersonNote(BaseModel):
    note: str = Field(default="", max_length=400)
    org: str = Field(default="", max_length=120)


class HeardFact(BaseModel):
    fact: str = Field(default="", max_length=400)
    # Whom it is about and what it says, as `remember` keeps a fact
    # (agent/facts.py): "user", "person:<name>", "org:<domain>".
    subject: str = Field(default="user", max_length=120)
    attribute: str = Field(default="", max_length=60)
    # Who the user is (facts.IDENTITY) is a value, not a sentence: the town
    # the forecast is for, the name they are known by.
    value: str = Field(default="", max_length=120)
    quote: str = Field(default="", max_length=600)
    until: str = Field(default="", max_length=10)


class HeardFacts(BaseModel):
    facts: list[HeardFact] = Field(default_factory=list)


class StyleGuide(BaseModel):
    # No max_length: a guide a little long is trimmed in code, where a
    # validation failure would spend the retries on a character count.
    guide: str = ""


# How far back their sent mail is read to learn how the user writes. Two months holds
# the forty emails `tools/style.py` wants in any term week.
STYLE_WINDOW_DAYS = 60


async def learn(conn: sqlite3.Connection, budget: Budget, report: NightReport) -> None:
    """Find out who the user deals with and what they said they would do.

    Runs first, and that is the one ordering decision here that is not
    arbitrary. This block reads *the day*, and the day is over: a research
    topic skipped tonight is still there tomorrow, an inbox read three nights
    late has lost the thread. It is also the cheapest of the three, and its
    output feeds `model.context()`, so the other two are better for it having run.

    Ingest is pure code and is never skipped for budget. Only extraction and
    the notes cost anything, and those stop when the money does.
    """
    from tools import ingest, people

    try:
        # Every mail account, each said apart: a user's Gmail can be the
        # services account while the correspondence is in a work Outlook.
        # Silent for an account not on this machine.
        found = await ingest.mail(conn)
        for name, said in found.get("accounts", {}).items():
            if said["sent"] or said["received"]:
                report.learned.append(
                    f"{name}: {said['sent']} sent, {said['received']} received")
        # The calendar too, and not as an afterthought: on this account it is
        # where the signal is. Ninety days of Gmail yielded three humans and
        # one sent message; the calendar has classes, recruiting events and
        # named guests on it.
        await ingest.calendar(conn)
        # Counted after all three, not after the first. Reporting Gmail's
        # total while Outlook had contributed ten times as many read as
        # "3 people known" next to "300 received" -- a report nobody
        # believes, about numbers that were individually correct.
        # After ingest, before anything reads the ledger. Exchange writes
        # "Quinn, Nora" and Gmail writes "Nora Quinn", so one
        # professor arrived as two rows.
        folded = people.merge_duplicates(conn)
        if folded:
            report.learned.append(f"merged {folded} duplicate contact(s)")

        totals = people.counts(conn)
        report.learned.append(
            f"{totals['real']} people known, {totals['filtered']} filtered out"
        )
    except Exception as exc:  # noqa: BLE001 - the rest of the night still runs
        log.exception("could not ingest")
        report.learned.append(f"ingest failed: {type(exc).__name__}"[:120])
        ingested = False
    else:
        ingested = True

    # Pure code, never skipped for budget: memory's files and its index
    # agree again, and what is long over is let go.
    await _tidy_memory(conn, report)

    # What costs money, while it lasts, and only on a day that was read.
    if ingested:
        for step in (_extract_commitments, _describe_people, _learn_from_user_words,
                     _learn_style):
            if budget.exhausted(headroom=0.02):
                break
            await step(conn, budget, report)
    # Whatever the money or the mail did (`_mirror_tasks`). Until 11 October
    # 2026 a night whose learning money was spent, or whose mail would not
    # open, returned before it -- and before the tidy, for the mail.
    await _mirror_tasks(conn, report)


async def _tidy_memory(conn, report: NightReport) -> None:
    """Index memory files the index lost (`memory.reconcile`), and forget
    the ones a week past their last day (`memory.expire`)."""
    from tools import memory

    try:
        added = await offloop.with_own_conn(conn, memory.reconcile)
        gone = await offloop.with_own_conn(conn, memory.expire)
    except Exception:  # noqa: BLE001 - the rest of the night still runs
        log.exception("could not tidy memory")
        return
    if added:
        report.learned.append(f"indexed {len(added)} memory file(s) recall could not see")
    if gone:
        report.learned.append(f"let go of {len(gone)} memory(ies) past their date")


async def _mirror_tasks(conn, report: NightReport) -> None:
    """Push due commitments to their phone, and read back what the user ticked.

    Outside the budget checks on purpose: this is pure API work with no
    model in it, and it is the half of the feature the user actually touches.
    Skipping it to save money would save nothing.
    """
    from agent import taskmirror

    try:
        synced = await taskmirror.sync(conn)
    except Exception:  # noqa: BLE001 - the night is over either way
        log.exception("could not sync tasks")
        return
    if synced.get("mirrored") or synced.get("closed"):
        report.learned.append(
            f"tasks: {synced['mirrored']} mirrored, {synced['closed']} ticked off"
        )


async def _sent_for_extraction(conn, window: int) -> list[dict]:
    """Every mail account's sent mail of the last `window` days, newest
    first, with bodies (`style.sent_mail`).

    Every account because the correspondence can be split: one may be the
    account services write to while the people the user writes to are in
    another. Gathering them here keeps `_extract_commitments` ignorant of
    which is which.

    Sent only, from each, and that is the invariant a gate test enforces
    rather than a convention this function happens to follow.
    """
    from agent import offloop
    from tools import style

    return await offloop.with_own_conn(conn, style.sent_mail, days=window, limit=20)


async def _extract_commitments(conn, budget: Budget, report: NightReport) -> None:
    """Read their sent mail for things the user said they would do.

    Sent only. Their own words are where "I'll send you the photos Friday" lives
    and it is the half the user authored; the cost is that obligations *others*
    took on toward them are not captured, which is a gap worth revisiting only
    if it proves annoying in practice.
    """
    from tools import commitments, extract, people

    cursor_name = "commitments"
    window = 3 if people.cursor(conn, cursor_name) else 14
    messages = await _sent_for_extraction(conn, window)

    kept = 0
    for message in messages:
        if kept >= MAX_NEW_COMMITMENTS or budget.exhausted(headroom=0.02):
            break
        try:
            checked, cost = await extract.from_message(message)
        except Exception:  # noqa: BLE001 - one bad message is not the block
            log.exception("extraction failed for %s", message.get("id"))
            continue
        budget.add(cost)

        # `parse_addresses`, not a split on the first comma: Exchange display
        # names contain commas -- "Lowe, Ingrid (Contoso Registrar)" -- so the
        # split yielded `"foley` and every commitment came out attached to
        # nobody. The header parser has always known about quoted commas.
        addressed = people.parse_addresses(message.get("to") or "")
        recipient = addressed[0][0] if addressed else ""
        row = conn.execute(
            "SELECT id FROM people WHERE email = ?", (recipient,)
        ).fetchone()

        for item in checked:
            if not item.kept:
                # Logged rather than stored. The discard rate is the number
                # that says whether this feature is trustworthy.
                log.info("dropped a commitment: %s", item.reason)
                continue
            added = commitments.add(
                conn,
                text=item.candidate.text,
                quote=item.candidate.quote,
                due_date=item.candidate.due_date,
                person_id=int(row["id"]) if row else None,
                source="sent_mail",
                source_id=message.get("id"),
            )
            if added:
                kept += 1
                report.learned.append(f"commitment: {item.candidate.text}")

    people.set_cursor(conn, cursor_name, datetime.now().isoformat(timespec="seconds"))


async def _describe_people(conn, budget: Budget, report: NightReport) -> None:
    """Write a line about anyone real who does not have one yet."""
    from tools import people

    described = 0
    for person in people.recent(conn, limit=MAX_PEOPLE_NOTES):
        if person.note or budget.exhausted(headroom=0.02):
            continue
        history = people.history(conn, person.id, limit=8)
        if len(history) < 2:
            # One exchange is not a relationship, and a note written from it
            # would be a guess wearing the clothes of a record.
            continue
        lines = "\n".join(
            f"  {h['occurred_at'][:10]} {h['direction']}: {h['subject']}"
            for h in history
        )
        try:
            note, cost = await model.ask(
                PersonNote,
                f"{person.name or person.email} <{person.email}>\n"
                f"{person.sent_count} sent, {person.recv_count} received\n\n{lines}",
                model.prompt("night_people"),
            )
            budget.add(cost)
        except Exception:  # noqa: BLE001
            log.exception("could not describe %s", person.email)
            continue
        if note.note.strip():
            people.set_note(conn, person.id, note.note, note.org or None)
            described += 1
    if described:
        report.learned.append(f"described {described} people")


def already_known(fact: str, existing: list[str]) -> bool:
    """Is this a restatement of something already recorded?

    Asking the model not to repeat itself was the first attempt and it did
    not hold: shown two memories and told plainly not to write a variation,
    the second run produced "The user is currently taking a course in
    probability modeling and statistics" against an existing memory that
    listed that course among their others. Both true, one useful.

    So the check is code, like every other cap here. `tools/evidence.py` is
    already the instrument for "do these words say the same thing", used the
    same way commitments are verified against their quotes -- a fact that is
    directly supported or inferable from what is on file is one the user already
    has.
    """
    if not existing:
        return False
    from tools.evidence import DIRECT, INFERRED, Source, ground_claims

    report = ground_claims(
        [fact], [Source(url=f"memory:{i}", text=text) for i, text in enumerate(existing)]
    )
    return bool(report.verdicts) and report.verdicts[0].label in (DIRECT, INFERRED)


# How far back their words are read: a night and the day before it, with room
# for a night that did not run.
HEARD_WINDOW_HOURS = 36
# The most of their messages read, the latest kept.
HEARD_MAX_MESSAGES = 80
# A message longer than this is mostly a paste -- an email, a page -- which
# is someone else's words; its opening is kept, where their own usually are.
HEARD_MESSAGE_CHARS = 1_500
# What the assistant had just said, for the context of their answer.
HEARD_CONTEXT_CHARS = 300
# Their messages just before the quoted one, in the same conversation, count
# as the same words: "What's the Northwind email format I told you?" and then
# "lastname.firstname@northwind.example is the format".
HEARD_TOGETHER_MINUTES = 30
# A message shaped like a paste -- a forwarded email's headers, a table, a
# long block of lines -- is someone else's words though the user sent it: on 7
# October they pasted an exam timetable. A fact learned from one would sit in
# every turn's instructions from then on (8 October 2026 audit), so such a
# message is not read at all. One long paragraph -- how the user dictates -- is
# still theirs.
PASTE_CHARS = 600
PASTE_LINES = 5
_HEADERS = re.compile(r"^\s*(?:from|to|cc|sent|date|subject):\s|^\s*-{2,}\s*(?:original|forwarded)"
                      r" message", re.IGNORECASE | re.MULTILINE)
_TABLE_ROW = re.compile(r"\t|\|")
# A fact rests on a phrase of theirs, not a passage: a quote longer than this is
# text the user passed on.
QUOTE_MAX_CHARS = 200


def _pasted(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    return (len(_HEADERS.findall(text)) >= 2
            or sum(1 for line in lines if _TABLE_ROW.search(line)) >= 3
            or (len(text) > PASTE_CHARS and len(lines) >= PASTE_LINES))


def _straight(text: str) -> str:
    """Curly quotes straightened, spaces collapsed, case folded."""
    from agent.issues import straight

    return " ".join(straight(text).split())


def _user_words(conn) -> list[dict]:
    """Their messages in their own conversations over the window, oldest first,
    each with what the assistant had just said and when, where the user is."""
    from agent import heard, history
    from agent.cut import cut
    from tools.extract import own_words

    out = []
    for row in heard.user_messages(conn, hours=HEARD_WINDOW_HOURS, latest=HEARD_MAX_MESSAGES):
        if _pasted(own_words(row["content"] or "")):
            continue
        before = conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND id < ? AND role = 'assistant'"
            " ORDER BY id DESC LIMIT 1", (row["session_id"], row["id"])).fetchone()
        at = history.local(row["created_at"])
        out.append({
            "id": row["id"], "session": row["session_id"], "channel": row["channel"],
            "at": at, "created_at": row["created_at"],
            # A forwarded email's quoted tail is someone else talking.
            "text": cut(own_words(row["content"] or ""), HEARD_MESSAGE_CHARS,
                        mark=" […a long paste, not shown]"),
            "before": cut(" ".join((before["content"] if before else "").split()),
                          HEARD_CONTEXT_CHARS, mark="…"),
        })
    return out


def _quoted_in(quote: str, said: list[dict]) -> dict | None:
    """The message of theirs that holds `quote` word for word, or None. Trailing
    punctuation is forgiven, as `tools/extract.verify` forgives it."""
    wanted = _straight(quote)
    if len(wanted) < 3:
        return None
    for message in reversed(said):
        text = _straight(message["text"])
        if wanted in text or wanted.rstrip(" .,;:!?") in text:
            return message
    return None


def _together(message: dict, said: list[dict]) -> str:
    """Their words around the quote: their messages in the same conversation in
    the half hour before it, and it."""
    from datetime import timedelta

    start = message["at"] - timedelta(minutes=HEARD_TOGETHER_MINUTES) if message["at"] else None
    near = [m["text"] for m in said
            if m["session"] == message["session"] and m["id"] <= message["id"]
            and (start is None or (m["at"] and m["at"] >= start))]
    return "User: " + " ".join(near)


async def _learn_from_user_words(conn, budget: Budget, report: NightReport) -> None:
    """Keep what the user told the agent about themselves, from their own words.

    Facts the user stated in conversation almost never became memory. Of their 293
    messages to 7 October 2026 there were 8 `remember` calls, all after the user
    had corrected something or asked outright. On 20 September the user said they
    were going camping in the Adirondacks and the Catskills at the beginning
    of October; on 2 October the user had to correct a briefing that called the
    day light. The user gave the Tailspin address format, had to give it again,
    and the Northwind one went unkept after they asked "What's the Northwind email format
    I told you?".

    The model proposes; code decides, as commitments are decided
    (`tools/extract.verify`). A fact is kept only if its quote is in one of
    their messages word for word, the fact is what their words say
    (`tools/evidence.ground_claims` against their messages around the quote),
    it is not already recorded (`already_known`), a fact about them holds no
    one else's address (`facts.misfiled`), and a plan's last day has not
    passed. Each is a typed fact, as `remember` keeps one (agent/facts.py):
    whom it is about, their words with the message they came from, so a turn
    is shown it when their message calls for it and a plan of theirs in the next
    two weeks is in every turn. Told in the morning with the way to drop it.

    It used to write a `user` memory, which a turn saw only if the model
    thought to `recall` it; and a second block inferred facts about them from
    their calendar, two on 20 September and none after, one of them read as
    their own words for three weeks. What their calendar says is not a fact
    about them.
    """
    from agent import facts
    from core import screen
    from tools import memory
    from tools.evidence import DIRECT, INFERRED, Source, ground_claims

    said = _user_words(conn)
    if not said:
        return
    existing = [facts.render_line(f) for f in facts.held(conn)]
    known = "\n".join(f"  · {e}" for e in existing[:40]) or "  (nothing yet)"
    notes = conn.execute(
        "SELECT summary FROM issues WHERE kind IN ('feedback_note', 'correction')"
        f" AND last_seen >= datetime('now', '-{HEARD_WINDOW_HOURS} hours') ORDER BY id"
    ).fetchall()
    lines = []
    for message in said:
        when = message["at"].strftime("%Y-%m-%d %a %H:%M") if message["at"] else "?"
        if message["before"]:
            lines.append(f"[{when}, {message['channel']}] Assistant: {message['before']}")
        lines.append(f"[{when}, {message['channel']}] User: {message['text']}")
    today = memory._today()
    prompt = (
        f"Today is {today.isoformat()}.\n\n"
        f"Already recorded about the user:\n{known}\n\n"
        "What the user told the assistant was wrong, in the same days:\n"
        + ("\n".join(f"  · {n['summary']}" for n in notes) or "  (nothing)")
        + "\n\nTheir messages, oldest first:\n" + "\n".join(lines)
        + f"\n\nWrite at most {MAX_HEARD_FACTS} new facts, or none."
    )
    try:
        found, cost = await model.ask(HeardFacts, prompt, model.prompt("night_heard"))
        budget.add(cost)
    except Exception:  # noqa: BLE001
        log.exception("could not learn from what the user said")
        return

    kept = 0
    for item in found.facts:
        if kept >= MAX_HEARD_FACTS:
            break
        fact = " ".join(item.fact.split())
        if not fact:
            continue
        if len(item.quote.strip()) > QUOTE_MAX_CHARS:
            log.info("dropped a fact resting on a passage, not a phrase: %s", fact[:80])
            continue
        message = _quoted_in(item.quote, said)
        if message is None:
            log.info("dropped a fact whose quote is not the user's: %s", fact[:80])
            continue
        # A paste can sit inside their message, and a fact kept from one is in
        # front of every turn after: anything shaped like an instruction to
        # a model is dropped (core/screen.py `pre_screen`).
        if screen.pre_screen(f"{fact}\n{item.quote}"):
            log.warning("dropped a fact that reads like an injection: %s", fact[:80])
            continue
        verdicts = ground_claims([fact], [Source(url="the user's words",
                                                 text=_together(message, said))]).verdicts
        if not verdicts or verdicts[0].label not in (DIRECT, INFERRED):
            log.info("dropped a fact the user's words do not say: %s", fact[:80])
            continue
        until = memory._until(item.until)
        if until and memory.past(until, today):
            continue  # a plan already over is not worth keeping
        if already_known(fact, existing):
            log.info("skipped a fact already recorded: %s", fact[:80])
            continue
        try:
            subject, _ = facts.subject_of(conn, item.subject or facts.USER)
        except ValueError:
            log.info("dropped a fact about no one it could name: %s", fact[:80])
            continue
        attribute = item.attribute or ("plan" if until else "note")
        if subject == facts.USER and (why := facts.misfiled(subject, attribute, fact,
                                                            conn=conn)):
            log.info("dropped a misfiled fact: %s (%s)", fact[:80], why[:80])
            continue
        value = fact
        if facts.is_identity(subject, attribute):
            # Kept as the sentence, a home town read "The user lives in
            # Leeds." and the forecast could not place it.
            value = " ".join(item.value.split())
            if not value or not facts.said_in(value, item.quote):
                log.info("dropped who the user is, not in their words: %s", fact[:80])
                continue
        source = ("inferred:address" if facts.unchecked(subject, value, conn=conn)
                  else "user_words")
        try:
            # Off the loop, on a connection of its own: a fact is embedded.
            stored = await offloop.with_own_conn(
                conn, facts.add, item.subject or facts.USER, attribute, value, source=source,
                source_ref=f"message:{message['id']}", valid_until=until)
        except Exception:  # noqa: BLE001 - a subject or attribute it got wrong
            log.exception("could not keep a fact the user said")
            continue
        existing.append(facts.render_line(stored))
        kept += 1
        report.remembered.append(f"{fact} (FORGET MEMORY {stored.id})")


async def _learn_style(conn, budget: Budget, report: NightReport, *, today=None) -> None:
    """Relearn how the user writes, once a week (`tools/style.py`).

    From `_sent_for_extraction`, so from sent mail only: the same invariant
    as commitments, checked by the same gate test. A guide that comes back
    too short to be one -- a refusal, an empty answer -- leaves last week's
    in place rather than replacing a useful page with a useless one.
    """
    from tools import style

    today = today or datetime.now().date()
    if not style.due(conn, today):
        return
    try:
        messages = await _sent_for_extraction(conn, STYLE_WINDOW_DAYS)
    except Exception:  # noqa: BLE001 - next week will do
        log.exception("could not read sent mail to learn the user's style")
        return
    text, count = style.sample(messages)
    if count < 5:
        # Five emails show a habit; two show two emails.
        return
    try:
        found, cost = await model.ask(StyleGuide, text, model.prompt("night_style"))
        budget.add(cost)
    except Exception:  # noqa: BLE001
        log.exception("could not learn how the user writes")
        return
    guide = found.guide.strip()
    if len(guide) < 200:
        log.info("style guide came back too short to keep (%d chars)", len(guide))
        return
    try:
        await offloop.with_own_conn(conn, style.save, guide, today)
    except Exception:  # noqa: BLE001
        log.exception("could not store the style guide")
        return
    report.learned.append(f"relearned how the user writes, from {count} sent emails")
