"""The judge: for each new thing, what should happen.

One cheap model call per batch, no tools. It sees what arrived, plus enough
of the user's day to connect the two -- today's and tomorrow's calendar, what
the agent knows about them, what it just did for them and what the user just said
(`lately`) -- and says, for every item:

- **why**: one sentence, written before the rest, so the decision follows a
  reason rather than preceding one;
- **say**: `now` (the user should know within the hour), `before` (tell them
  shortly before it happens), `briefing` (the next morning or evening
  briefing) or `never`, which is most things;
- **critical**: worth waking them for, which is almost nothing;
- **headline**: the fact, one line, for their phone;
- **task**: optional, something reversible to do before the user hears about it.

What it does not decide is how often the user is interrupted, what the doer may
touch, or whether the night is quiet. Those are `settings` and `deliver`,
in code. A model asked "is this urgent?" drifts toward yes, and has no
memory of the times it already said so today.

A key the model invents is discarded, and an item it forgets to mention is
recorded as `never` -- so every item is judged exactly once, and the next
batch never includes it.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from agent import sqltime
from agent.attention import settings, store
from agent.attention.store import Observation
from agent.cut import cut
from core import modules, spend

log = logging.getLogger("attention")

MAX_HEADLINE = 240
MAX_TASK = 600

JUDGE_CORE = """You are the part of the user's assistant that notices things.

Every few minutes you are shown what has newly arrived or changed. The user
is not waiting for you: they are going about their day. For each item,
decide what should happen.

**why** -- first, one sentence: what the item is and whether it needs the
user today. Decide after you have written it.

**reason** -- then one code for what makes it worth their attention:
changes-plans, reply-owed (a person wrote to them and waits on their
answer), deadline, money, safety, or fyi.

**say** -- one of:

  now       They should know within the hour, and it is actionable today. A
            bill or deadline due within about a day -- stated, not guessed: a
            bill with no due date in view is briefing at most. A plan that
            moved, was cancelled, or starts sooner than they would expect.
            Someone who needs an answer today. A security alert on one of
            their accounts. A delivery or appointment that needs them.
            Something that gets harder to fix by waiting.
  before    Tell them shortly before it happens -- only for something with a
            start time that they would want to walk into prepared. Use it for
            the thing itself, not for what was written about it.
  briefing  Worth knowing, not worth interrupting for; it goes into their
            next morning or evening briefing. Something coming up they may
            want to go to. An invitation for next week. A reply that can wait
            a day. A commitment due in a few days.
  never     Everything else, which is most things. Newsletters, marketing,
            receipts for things already done, automated notifications,
            routine things they set up themselves. "never" is the correct
            answer most of the time, not a failure to find something.

**critical** -- true only if it would be worth waking them at three in the
morning: a security breach on one of their accounts, travel in the next day
cancelled or disrupted, something they have the next morning moved or
cancelled, a genuine emergency. Almost nothing is critical. Only with
say=now.

**headline** -- what the user reads when it reaches them: the fact, not the
category. "Water bill, $84, due tomorrow", not "you have a bill". One line,
no preamble, no "you may want to". If several items are one story, headline
one and mark the rest never.

**task** -- optional. Something their assistant should do before they hear
about it, with their own tools, all of which are reversible: set a reminder
for something with a date; look something up they would otherwise look up
themselves (a place, opening hours, what an announcement means for them);
add a task for something they have to do. Write it as an instruction with
the specifics: "Add a task to renew the parking permit by Friday, with the
link to the form." Leave the task empty when nothing needs doing, which is
most items.

What you are shown: {looked_at}.

{paragraphs}

Rules that outrank being helpful:
  - Judge only what you are shown. Do not guess at contents you cannot see.
  - Read what you did for the user, what they said lately and how they took
    what you sent them before anything else. A notice that something you
    just did for them caused is never; so is anything they have already
    handled, asked you to do, or declined.
  - Never propose contacting anyone. Contacting people is the user's to do.
  - A time or place that is not stated is not known; no task acts on a
    guess.

Give a verdict for every key."""

# What each source brings, in the order the judge reads them: the words for
# what it looks at, and its paragraph. A source the user does not have
# brings neither (`judge_prompt`).
JUDGE_SOURCES: tuple[tuple[frozenset[str], str, str], ...] = (
    (frozenset({"mail"}), "mail in their mailboxes",
     """**Mail** items are new email in the user's mailboxes. Mail a system sent
-- receipts, notices, newsletters, a platform's invitations -- is fyi unless
it changes their plans. Mail marked "already read" they have seen: say now
only if it needs doing today and is easy to miss. An email that asks them
something only they can answer is reply-owed, and never a task: the
headline says what it asks and offers a draft ("Dana asks whether
Thursday's viewing still works -- want a draft? Anything to say?"). An
event whose exact time and place an email states may be put on their
calendar."""),
    (frozenset({"calendar", "horizon"}), "changes to their calendar and events coming up",
     """**Calendar** items are events on the user's calendar: new, moved or
cancelled, and those coming up. Something they have to be at -- an
appointment, a meeting, a pickup, a class that moved -- is before, for the
event itself, when there is something worth knowing on the way to it. The
task is then a heads-up from the user's own things: where it is, what to
bring or have ready, what was agreed about it in their mail, and anything
the user promised the people in it, who are listed with the item. A routine
event they created themselves is never; a large one (dozens of guests) or
one they hold alone needs no heads-up."""),
    (frozenset({"commitments"}), "promises they made that are coming due",
     """**Commitments** are things the user said they would do, coming due. Due
today or tomorrow and easy to forget: now. Due in a few days: briefing."""),
    (frozenset({"drive"}), "files other people shared or changed",
     """**Drive** items are files other people shared with the user or changed.
A document someone shared with them: briefing. Edits to a file they already
have: never, unless the change needs them."""),
    (frozenset({"weather"}), "the weather at home",
     """**Weather** items are computed from the forecast and official alerts for
where the user lives, and only exist because something is notable. Rain or a
hard temperature during something they are walking to today: now. A notable
day tomorrow: briefing. An official warning in effect: now, and critical
only if it is severe and imminent (tornado, hurricane, flash flood,
blizzard, extreme heat). Relate it to their plans in the headline: "Rain
from 3pm -- you walk to the 4pm dentist appointment"."""),
    (frozenset({"deadlines"}), "what is coming due on calendars they marked as due dates",
     """**Deadlines** items are things due, from a calendar the user marked as due
dates (a course site's feed, say), with their due times. The feed does not
say whether one has been handed in. Due within about a day: now, with what
it is and the time in the headline ("Problem set 2 due Friday at 8pm"), and
a task to set a reminder a few hours before if it is due tomorrow or later
today. Due in two or three days: briefing. Something due at the start of a
class or meeting: briefing at most -- they know their timetable. Never
critical."""),
    (frozenset({"health"}), "the assistant's own health",
     """**Health** items are about the assistant itself: something it depends on
has stopped or is about to (a model provider's credit, the coding sandbox,
the disk, background jobs failing). Say=now, never critical, and the
headline says what stopped and what fixes it ("The disk is nearly full:
2 GB left. Old recordings can be deleted from Settings."). No task."""),
    (frozenset({"threads"}), "conversations left hanging",
     """**Threads** items are conversations left hanging: a real person wrote and
the user has not replied, or the user wrote and nobody has. Many need
nothing -- an FYI, a group email, a thank-you that ends a thread -- and
those are never. One that does is a briefing line, or now if it is
time-sensitive. A task to draft only for the user's own unanswered email --
a short follow-up nudge, which needs nothing from them. For a reply they
owe, never a task: the headline says what the thread needs and asks whether
they want a draft ("Dana asked whether Thursday's viewing still works --
want me to draft a reply? Anything to say?")."""),
    (frozenset({"after"}), "meetings that just ended",
     """**After** items are meetings that just ended, each one the user has asked
to be asked about. Ask how it went: say=now, and the headline is the
question: "How did lunch with Sam go? Anything to remember or do?" Never a
task -- nothing is done until the user answers."""),
    (frozenset({"news"}), "headlines from the news they follow",
     """**News** items are headlines from topics the user chose to follow. Almost
all are never. Briefing only for a story they would want to know today: a
major development in a topic they follow, or news about an organisation
they deal with. Now only if it changes something they are doing today.
Critical only for something like an attack or disaster where they are.
Several headlines about one story are one headline; at most a few a day
deserve a briefing line."""),
)


# Said after the Calendar paragraph when the user has chosen to have the
# people in a meeting looked up (`settings.MEETING_RESEARCH`).
MEETING_RESEARCH_SAYS = (
    " For a meeting with other people, the heads-up also says who they are"
    " and, for an outside organisation, any recent news about it.")


def judge_prompt(sources: set[str] | frozenset[str],
                 modules: Sequence[tuple[str, str]] = ()) -> str:
    """The judge's instructions for a user whose attention sources are
    `sources` (by their names in `sources.all_sources`): the core, and what
    each of theirs looks at and its paragraph. No home, no Weather
    paragraph; no calendar of due dates, no Deadlines one; with the
    follow-up after meetings off, no After one.

    What the user has is said after the core, not in its first line: the
    core is the same for everyone and every batch, so it is read from the
    provider's cache whatever the sources.

    One text for one user until 11 October 2026, which told every user's
    judge about two mailboxes, a course feed and the meetings of a
    recruiting season. Each paragraph now comes with the source it is
    about, and the core assumes no life: a judge told about a mailbox the
    user does not have is a judge invited to imagine one.

    `modules` are what each enabled module's source brings, by module id
    (`sources.Source.judge`): after core's, so the module's words never
    come between core's paragraphs, each in a block that names it."""
    research = MEETING_RESEARCH_SAYS if settings.MEETING_RESEARCH == "on" else ""
    # Every mail account's source is one kind to the judge.
    have = {"mail" if store.is_mail(s) else s for s in sources}
    brought = [(says, paragraph + (research if "calendar" in names else ""))
               for names, says, paragraph in JUDGE_SOURCES if names & have] + list(modules)
    looked = [says for says, _ in brought] or ["what you are shown"]
    looked_at = looked[0] if len(looked) == 1 else ", ".join(looked[:-1]) + " and " + looked[-1]
    return JUDGE_CORE.format(looked_at=looked_at,
                             paragraphs="\n\n".join(p for _, p in brought))


class Verdict(BaseModel):
    # A reason before the decision, not after it: the fields are written in
    # this order, and at effort none there is no thinking but what is
    # written -- with `say` first, the call to ping their phone was made
    # before a word about why (5 October 2026).
    key: str
    why: str = Field(default="", max_length=300,
                     description="One sentence: what this is and whether it needs the user "
                                 "today")
    # A string, not a Literal of `vet.REASONS`: a code the model misspells
    # is dropped (`_reason`), where a Literal would fail the whole batch.
    reason: str = Field(default="", max_length=40,
                        description="changes-plans | reply-owed | deadline | money"
                                    " | safety | fyi")
    say: Literal["now", "before", "briefing", "never"]
    critical: bool = False
    headline: str = Field(default="", max_length=600)
    task: str = Field(default="", max_length=1200)


class Verdicts(BaseModel):
    items: list[Verdict] = Field(default_factory=list)


@dataclass
class TriageResult:
    judged: int = 0
    now: int = 0
    before: int = 0
    briefing: int = 0
    tasks: int = 0
    cost_usd: float = 0.0
    skipped: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "judged": self.judged, "now": self.now, "before": self.before,
            "briefing": self.briefing, "tasks": self.tasks,
            "cost_usd": round(self.cost_usd, 5), "skipped": self.skipped,
        }


# ---------------------------------------------------------------------------
# What the judge knows about their day
# ---------------------------------------------------------------------------

_CONTEXT: dict = {"at": float("-inf"), "text": ""}
CONTEXT_TTL_S = 10 * 60


def day_context(conn: sqlite3.Connection) -> str:
    """Today and tomorrow, and who the user is, in a few lines. Blocking.

    Cached for ten minutes: the judge runs every few minutes, their calendar
    does not change that fast, and every call is an API round trip.
    """
    if time.monotonic() - _CONTEXT["at"] < CONTEXT_TTL_S:
        return _CONTEXT["text"]

    lines: list[str] = []
    try:
        from agent import calendars

        agenda = calendars.agenda(conn, 2, limit=25)
        events = agenda.get("events") or []
        if events:
            lines.append("The user's next two days:")
            for event in events:
                where = f" @ {event['location']}" if event.get("location") else ""
                lines.append(f"  - {event.get('start')} {event.get('summary')}{where}")
    except Exception as exc:  # noqa: BLE001 - a judge without context still judges
        log.info("no calendar for the judge: %s", exc)

    from agent.attention.sources import weather

    if forecast := weather.forecast_lines(conn):
        lines.append("The weather where the user lives:")
        lines += [f"  - {line}" for line in forecast]

    # Who the user is, as the conversation's instructions say it: their
    # profile (`facts.profile`). It read `memories` of kind 'user', which
    # nothing has written since the profile replaced them, so the judge
    # decided "for this person" knowing nothing of them.
    from agent import facts

    try:
        about = facts.profile(conn)
    except Exception:  # noqa: BLE001 - a judge without it still judges
        log.warning("no profile for the judge", exc_info=True)
        about = []
    if about:
        lines.append("What the user has said about themselves:")
        lines += [f"  - {facts.render_line(f)}" for f in about]

    from agent import connectors

    # So the judge can tell an event the user organised from an invitation.
    # Not knowing them just leaves the line out.
    try:
        mine = sorted(connectors.addresses(conn))
    except Exception:  # noqa: BLE001 - a judge without them still judges
        log.warning("no addresses for the judge", exc_info=True)
        mine = []
    if mine:
        lines.append(f"The user's own addresses: {', '.join(mine)}")

    _CONTEXT.update(at=time.monotonic(), text="\n".join(lines))
    return _CONTEXT["text"]


# What the judge is told of the last few hours, besides their day. Not cached:
# it is what changes between two looks ten minutes apart.
DONE_HOURS = 6
SAID_HOURS = 3
SAID_MAX = 8
SAID_CHARS = 200
NOTES_DAYS = 30
NOTES_MAX = 10


def lately(conn: sqlite3.Connection) -> str:
    """What the agent just did for them, what the user just said, and what they have
    said about messages like these. Blocking (the database).

    The judge saw only their calendar, the weather and eight facts, and
    pinged as if it were the only part of the agent. 1 October 2026,
    22:33: "How did the Fabrikam Seattle coffee chat go? If you'd like a
    thank-you note..." -- nine minutes after the agent had drafted that
    note for them ("i just had you write the thank you note for it"). 7
    October, 17:12: two LinkedIn new-device alerts judged `now`, four
    minutes after the agent's own LinkedIn sign-in ("that's literally your
    linkedin login"). And a note offered after a moderator prep call when
    the user had said, of such meetings, "no thank you note it's much more
    chill". Each was in the database: the action, their words, their note.
    """
    from agent import heard, history
    from agent.cut import cut

    def when(stamp: str) -> str:
        at = history.local(stamp)
        return at.strftime("%a %H:%M") if at else stamp

    lines: list[str] = []
    done = conn.execute(
        "SELECT tool, preview, status, created_at FROM actions"
        " WHERE created_at >= datetime('now', ?)"
        "   AND status IN ('pending', 'approved', 'executing', 'done')"
        f"  AND {modules.live(conn, 'actions')} ORDER BY id",
        (f"-{DONE_HOURS} hours",)).fetchall()
    if done:
        lines.append(f"What you did for the user in the last {DONE_HOURS} hours:")
        for row in done:
            what = cut(" ".join((row["preview"] or "").split()), 160, mark="…")
            waiting = " (waiting for their yes)" if row["status"] == "pending" else ""
            lines.append(f"  - {when(row['created_at'])} {row['tool']}: {what}{waiting}")

    said = heard.user_messages(conn, hours=SAID_HOURS, latest=SAID_MAX)
    if said:
        lines.append(f"What the user said to you in the last {SAID_HOURS} hours:")
        for row in said:
            text = cut(" ".join((row["content"] or "").split()), SAID_CHARS, mark="…")
            lines.append(f"  - {when(row['created_at'])} \"{text}\"")

    notes = conn.execute(
        "SELECT summary, excerpt FROM issues WHERE kind = 'feedback_note'"
        "   AND last_seen >= datetime('now', ?) ORDER BY last_seen DESC LIMIT ?",
        (f"-{NOTES_DAYS} days", NOTES_MAX)).fetchall()
    if notes:
        lines.append("What the user has told you about how you did, lately:")
        for row in notes:
            about = cut(" ".join((row["excerpt"] or "").split()), 120, mark="…")
            lines.append(f"  - {row['summary']}" + (f" (about: {about})" if about else ""))

    from agent.attention import told

    reacted = told.reactions(conn)
    if reacted:
        lines.append("How the user took what you sent them unasked, lately:")
        for r in reacted:
            how = "a ping" if r.how == "ping" else "a pre-event brief"
            said = "; ".join(
                f"\"{cut(' '.join(ln.line.split()), 140, mark='…')}\""
                f" ({ln.source or 'not an observation'}"
                + (f", about {ln.place}" if ln.place else "")
                + (f", your reason: {ln.reason}" if ln.reason else "") + ")"
                for ln in r.lines)
            lines.append(f"  - thumbs-{r.verdict} on {how} sent {when(r.said_at)}: {said}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------


def _deliver_at(when: str | None) -> str | None:
    """When a `before` item should go out, as SQLite UTC -- or None if it
    has no time to be before.

    An all-day event has a date and no moment, so it has no "forty-five
    minutes before"; it belongs in that morning's briefing instead.
    """
    if not when or "T" not in when:
        return None
    try:
        start = dt.datetime.fromisoformat(when)
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.astimezone()  # a naive time here is local
    return sqltime.stamp(start - settings.BEFORE_LEAD)


async def judge(batch: list[Observation], context: str, recent: str = "", *,
                watching: Collection[str] = (),
                modules: Sequence[tuple[str, str]] = ()) -> tuple[list[Verdict], object]:
    """The verdicts on `batch`, from instructions for the sources the user
    has (`watching`, by name) and for any source in the batch: one turned
    off since its item was noticed still has its item judged knowingly.
    `modules`: what the enabled modules' sources bring (`judge_prompt`)."""
    from agent.llm import ask_once
    from agent.models import Role

    listing = "\n".join(o.as_prompt_line() for o in batch)
    # What changes least first: the day's context is kept for ten minutes
    # (`day_context`), the clock changes every minute and the items every
    # call. With the clock first, nothing after the fixed instructions was
    # ever read from the cache -- 1,781 tokens cached of ~2,900, on the calls
    # that hit at all (6 October 2026). What happened lately (`lately`)
    # between them: it changes when the user talks or the agent acts, not each
    # minute.
    prompt = (
        f"{context}\n\n"
        + (f"{recent}\n\n" if recent else "")
        + f"It is {dt.datetime.now().strftime('%A %d %B %Y, %H:%M')}.\n\n"
        f"New since the last look:\n\n{listing}"
    )
    instructions = judge_prompt({*watching, *(o.source for o in batch)}, modules)
    verdicts, record = await ask_once(Role.BULK, instructions, prompt, output_type=Verdicts,
                                      retries=2)
    return verdicts.items, record


def ask_first(obs: Observation, reason: str | None = None) -> bool:
    """Whether this item may only ever be asked about, never acted on unasked.

    The maintainer's rule, 24 September 2026: follow-through must not write a
    draft that could need something from them -- a note after a meeting, a
    reply the user owes someone. It asks, and the draft is written in the
    conversation once the user answers. In code rather than in the prompt,
    because a model given an email and a drafting tool drafts. A follow-up
    nudge on their own unanswered email needs nothing from them and stays
    allowed.

    A reply owed is the same whether a thread noticed it days later or the
    judge saw it arrive (`reason` "reply-owed"): the judge's prompt said
    "draft it" for the one and "never a task" for the other, and code held
    only the thread to its rule. Pinned in tests/gate/test_attention_bounds.py.
    """
    if obs.source == "after":
        return True
    if obs.source == "threads":
        return obs.ref.get("waiting") == "owed by the user"
    return store.is_mail(obs.source) and reason == "reply-owed"


def apply(
    conn: sqlite3.Connection, batch: list[Observation], verdicts: list[Verdict]
) -> TriageResult:
    """Write the verdicts. Invented keys are dropped; forgotten items are `never`."""
    result = TriageResult()
    by_key = {o.key: o for o in batch}
    done: set[str] = set()
    for verdict in verdicts:
        obs = by_key.get(verdict.key)
        if obs is None or verdict.key in done:
            continue
        done.add(verdict.key)

        say = verdict.say
        deliver_at = None
        if say == "before":
            deliver_at = _deliver_at(obs.when)
            if deliver_at is None:
                say = "briefing"
        # Cut at a word, with "…" (agent/cut.py): a task cut mid-sentence
        # is a different instruction ("draft a reply but do not" ...).
        headline = cut(verdict.headline.strip(), MAX_HEADLINE, mark="…")
        if say != "never" and not headline:
            headline = cut(obs.title, MAX_HEADLINE, mark="…")
        task = cut(verdict.task.strip(), MAX_TASK, mark="…") if say != "never" else ""
        if ask_first(obs, _reason(verdict.reason)):
            task = ""

        store.judged(
            conn, obs.id, say=say, critical=verdict.critical and say == "now",
            headline=headline, task=task, deliver_at=deliver_at,
        )
        # The judge's sentence and code, kept as it gave them; what the
        # checks make of them is `vet.after_judging`'s, from `triage`.
        store.record_verdict(conn, obs.id, why=verdict.why.strip(), say=say,
                             critical=verdict.critical and say == "now",
                             reason=_reason(verdict.reason))
        result.judged += 1
        result.tasks += bool(task)
        if say in ("now", "before", "briefing"):
            setattr(result, say, getattr(result, say) + 1)

    for key, obs in by_key.items():
        if key not in done:
            store.judged(
                conn, obs.id, say="never", critical=False, headline="", task="", deliver_at=None,
            )
            store.record_verdict(conn, obs.id, why="the judge gave no verdict for it",
                                 say="never", critical=False, reason=None)
            result.judged += 1
    return result


def _reason(code: str) -> str | None:
    """The judge's reason code, if it is one (`vet.REASONS`)."""
    from agent.attention import vet

    code = (code or "").strip().lower().replace("_", "-").replace(" ", "-")
    return code if code in vet.REASONS else None


async def triage(conn: sqlite3.Connection) -> TriageResult:
    """Judge what is waiting, within today's judging budget: the judge's
    calls are background work's `triage` part (core/spend.py). Never raises."""
    with spend.spending("background", part="triage", conn=conn):
        return await _triage(conn)


async def _triage(conn: sqlite3.Connection) -> TriageResult:
    from agent import offloop

    if refusal := spend.may_spend(conn):
        return TriageResult(skipped=f"a spending cap is reached: {refusal.cap}")

    batch = store.untriaged(conn, settings.MAX_TRIAGE_BATCH)
    if not batch:
        return TriageResult(skipped="nothing new")
    # What code already knows the answer to is settled before the call: a
    # message judged before under another key (Trey Research's invitation, `never`
    # as mail on 8 October and "a reply you owe" as a thread on the 9th), a
    # second copy in this batch, a "reply the user owes" to a list.
    from agent.attention import vet

    before = len(batch)
    batch = vet.before_judging(conn, batch)
    settled = before - len(batch)
    if not batch:
        return TriageResult(judged=settled, skipped="settled by the checks")

    context = await offloop.with_own_conn(conn, day_context)
    try:
        recent = await offloop.with_own_conn(conn, lately)
    except Exception:  # noqa: BLE001 - a judge without it still judges
        log.warning("could not gather what happened lately", exc_info=True)
        recent = ""
    from agent.attention import sources

    watched = await offloop.with_own_conn(conn, sources.watching)
    watching = [s.name for s in watched]
    try:
        verdicts, record = await judge(batch, context, recent, watching=watching,
                                       modules=[s.judge for s in watched if s.judge])
    except spend.Refused as refusal:
        # Not the items' failure: counted as an attempt, a day at the cap
        # would give them up unjudged.
        return TriageResult(skipped=f"a spending cap is reached: {refusal.cap}")
    except Exception as exc:  # noqa: BLE001 - the items wait for the next tick
        log.warning("triage failed on %d items: %s", len(batch), exc)
        store.note_attempt(
            conn, [o.id for o in batch], give_up_after=settings.MAX_TRIAGE_ATTEMPTS
        )
        return TriageResult(skipped=f"{type(exc).__name__}: {exc}"[:200])

    result = apply(conn, batch, verdicts)
    result.judged += settled
    result.cost_usd = record.cost_usd
    # Then the checks on what the judge said: who sent it, where it is, and
    # whether it is still true. Failing open per item (`after_judging`);
    # anything it cannot do at all is checked again before it is said
    # (`vet.recheck`).
    try:
        vet.after_judging(conn, [o.id for o in batch])
    except Exception:  # noqa: BLE001 - see above
        log.exception("could not check %d verdict(s); rechecked before delivery", len(batch))
    return result
