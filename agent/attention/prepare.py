"""The doer: carrying out what the judge said to prepare.

A tool-using conversation on the `initiative` channel, one turn per task, a
few tasks per tick. The judge wrote each task as an instruction with the
specifics ("draft a reply to Rina confirming..."); this turns it into a
calendar entry, a reminder, a draft or an answer looked up.

**It can only do what the user could undo.** Every tool in `PREPARE_TOOLS` is
`private` and `reversible` in the frozen `core/consent.yaml` -- read,
search, create an event with no guests, set a reminder, add a task, write
a draft. Nothing here sends, invites, deletes, spends or calls.
`Conversation.send(allow=...)` cannot reach a tool outside the set, the
consent gate would queue anything outward even if it could, and
`tests/gate/test_attention_bounds.py` fails the build if the set ever
widens. Besides them, since 11 October 2026, it may have a page that needs
a real browser read by a clean-room browsing worker, which holds nothing of
theirs and can do nothing (`workers.send_browsing`).

**The channel is `initiative` on purpose.** It is autonomous by the frozen
rule (`core.call_policy.is_autonomous`), so nothing the doer says can
approve a card, start a call or file a change to the agent as the user's
request.

**Two kinds of turn, each told its shape once** (`instructions`). A task
the judge set is done and reported in one line. A heads-up before an event
(`say` "before") is up to three lines of what is worth knowing walking in.
One prompt held both, said "ONE line" and then "up to three", and the first
live heads-up came out as one 300-character line.

**What it may use depends on the item** (`tools_for`). A heads-up reads the
user's own things; it looks up the people in the room and their
organisation on the web only if the user turned that on
(`settings.MEETING_RESEARCH`). Every meeting with anyone in it used to get
a consultant's dossier: a school pickup or a dinner, and the guests' names
sent to a search engine. A draft is offered only for a nudge on the user's
own unanswered email (`triage.ask_first` says why).

**Tools are the evidence.** A turn that used none of `ACTING` or
`RESEARCHING` did nothing, whatever its text says, and its outcome is not
recorded -- the user hears the judge's headline instead. "I looked at your
mail" is not news. A heads-up is the exception that proves it: there the
finding *is* the value, so looking at their own things (`LOOKING`) counts
too.

**The people in the room come from code.** For an event, `describe` hands
the doer what the contact ledger knows about each guest -- who they are,
the last exchanges, what the user promised them -- so the heads-up starts
from facts rather than from whatever the doer thinks to search for.

**A brief is current when it goes out, and names nobody it guessed.** The
brief for the 19 October prep call was written on 5 October, two weeks
early, from a title ("... with Alex") and an event with no guests, and
said "Alex appears to be Alexandra Wren": the only person in the ledger
with "chris" inside her name. Its twin for 7 October went out with that
sentence and the user answered "That's who Alex is, not Alexandra Wren"; by
then the event listed the real Alex, and nothing had looked again. So a
brief written more than a day before its moment is written again once it
is near (`store.refresh_briefs`, run here), one written before the people
listed for its event changed is written again as soon as a look sees them
(`store.record_new`), and the doer is told to name nobody it only guessed
from a first name -- `who_is` marks such a match "partial".
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from agent import handles
from agent.attention import settings, store
from agent.attention.store import Observation
from agent.cut import cut
from core import spend

log = logging.getLogger("attention")

PREPARE_TOOLS = frozenset({
    # look at their own things
    "search_email", "read_email", "email_thread",
    "calendar_agenda", "calendar_search", "calendar_free",
    "search_drive", "read_drive_file",
    "my_commitments", "list_reminders", "list_tasks",
    "who_is", "mod__countdown__days_until", "recall", "writing_style",
    # look things up
    "web_search", "read_page", "search_and_read",
    # act, reversibly
    "create_event", "remind_me", "add_task", "draft_email",
})

# Using one of these means something of theirs changed.
ACTING = frozenset({"create_event", "remind_me", "add_task", "draft_email"})

# Using one of these means it found out something the user would have gone
# looking for; nothing changed, but the answer is the value.
RESEARCHING = frozenset({"web_search", "read_page", "search_and_read"})

# Looking at their own things: evidence only for a heads-up, where what
# was found in their mail, their Drive or the ledger is the whole point.
LOOKING = frozenset({
    "search_email", "read_email", "email_thread", "search_drive", "read_drive_file",
    "who_is", "my_commitments", "recall", "calendar_search",
})

# What a heads-up may use: their own things, and the clock. It changes
# nothing -- it is read on the way to the event, too late to set anything up.
HEADS_UP_TOOLS = LOOKING | {"calendar_agenda", "mod__countdown__days_until"}

# How much of a brief reaches their phone, and how much of anything else.
BRIEF_LINES = 3
BRIEF_CHARS = 600
LINE_CHARS = 300

# The most guests described from the ledger, and exchanges per guest.
PEOPLE_DESCRIBED = 5
EXCHANGES = 3

NOTHING = "NOTHING TO DO"

TASK = """You are doing something for the user while they are not asking you
to. They are not at their phone and are not waiting for an answer.

The item and what to do about it are below. Do it with your tools, then reply
with ONE line, plain text, written for their phone, saying what you did or
found -- "Added the dentist, Thu 11:30, address in the event", "Reminder set
for the permit renewal, Friday 9:00".

Rules that outrank the task:
 - Check before you create. A duplicate of something already on their
   calendar, their reminders or their tasks is worse than nothing.
 - Do not guess. A time, place or fact that is not stated or found stays out;
   a wrong calendar entry is worse than a missing one because they plan
   around it.
 - A reminder is for acting on. For something they have to be at, it goes off
   early enough to get there -- 30 to 60 minutes before, more if it is across
   town -- never at the start time; for something they have to do, early
   enough to do it.
 - Nothing reaches another person: no invitations, no guests on events. Pass
   a message id back exactly as given; to add an event an email describes,
   set from_message_id, and it is checked."""

# Added to `TASK` when the item is a nudge on the user's own unanswered
# email, the one draft written unasked (`tools_for`).
DRAFT = """
 - The draft is a short follow-up to the user's own email, left in their
   drafts and never sent: draft_email with reply_to_message_id. Call
   writing_style with the recipient first and write the way they do; say
   nothing they have not said."""

HEADS_UP = """You are writing a heads-up the user reads shortly before
something they have to be at, on their phone, on the way.

Reply with up to three short lines, a fact to a line, each something worth
knowing walking in: where it is and how to get in, what to bring or have
ready, what was agreed about it in their mail or their files, anything the
user promised the people in it. Start from the event and the people listed
below, who come from the user's own records, then look in their mail and
files. A line with nothing new in it is left out, and so is anything you
did not find.

Rules that outrank the task:
 - Do not guess. A time, place or fact that is not stated or found stays out.
 - Say who someone is only from the event's guest list or an email about
   this event. A first name in its title is not enough, nor is a who_is
   match marked "partial", which found the name inside another ("alex" in
   Alexandra). With nothing else to go on, leave that person out."""

# Added to `HEADS_UP` when the user chose to have the people in a meeting
# looked up (`settings.MEETING_RESEARCH`).
RESEARCH = """
 - With other people in it, a line may say who they are and, for an outside
   organisation, any recent news about it, looked up on the web."""

ENDING = """
 - If on inspection nothing needs doing, reply with exactly: NOTHING TO DO."""


@dataclass
class PrepareResult:
    done: list[str] = field(default_factory=list)
    nothing: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    skipped: str = ""

    def as_dict(self) -> dict:
        return {
            "done": self.done, "nothing": self.nothing, "failed": self.failed,
            "cost_usd": round(self.cost_usd, 5), "skipped": self.skipped,
        }


def people_block(conn: sqlite3.Connection, guests: list[str]) -> str:
    """What the contact ledger knows about the people in the room.

    Pure code, from `tools/people.py` and `tools/commitments.py` -- the same
    facts `who_is` returns, handed over up front: a doer left to find them
    has to think of asking, once per guest, and each ask is a model round
    trip billed to the day's work budget. A guest the ledger does not know
    is still listed, so "nothing known" is visible rather than silent.
    """
    from tools import commitments, people, style

    lines = []
    for guest in guests[:PEOPLE_DESCRIBED]:
        email = style.address_in(guest)
        row = conn.execute("SELECT id FROM people WHERE email = ?", (email,)).fetchone()
        person_id = row["id"] if row else people.resolve_alias(conn, email)
        person = people.get(conn, person_id) if person_id else None
        if person is None:
            lines.append(f"- {guest}: not in the user's records")
            continue
        if not person.is_person:
            continue  # a list or a service, invited wholesale
        about = ", ".join(bit for bit in (person.org, person.note) if bit)
        lines.append(f"- {person.name or guest} <{person.email}>" + (f": {about}" if about else ""))
        for exchange in people.history(conn, person.id, limit=EXCHANGES):
            lines.append(
                f"    {str(exchange['occurred_at'])[:10]} {exchange['direction']}"
                f" {exchange['kind']}: {exchange['subject'] or '(no subject)'}"
            )
        for owed in commitments.for_person(conn, person.id):
            lines.append(f"    the user owes them: {owed.line()}")
    return "\n".join(lines)


def nudge(obs: Observation) -> bool:
    """A thread where the user wrote and nobody has answered: the one item a
    draft may be written for unasked, since it needs nothing from them."""
    return obs.source == "threads" and obs.ref.get("waiting") == "owed to the user"


def tools_for(obs: Observation) -> frozenset[str]:
    """What the doer may use for this item, within `PREPARE_TOOLS`: for a
    heads-up, the user's own things, and the web only if they chose to have
    the people in a meeting looked up; for anything else, all of it, the
    drafting tool only for a `nudge`."""
    if obs.say == "before":
        research = RESEARCHING if settings.MEETING_RESEARCH == "on" else frozenset()
        return frozenset(HEADS_UP_TOOLS | research)
    if nudge(obs):
        return PREPARE_TOOLS
    return PREPARE_TOOLS - {"draft_email"}


def instructions(obs: Observation, tools: frozenset[str]) -> str:
    """What the doer is told, by the kind of item, each shape stated once."""
    if obs.say == "before":
        return HEADS_UP + (RESEARCH if tools & RESEARCHING else "") + ENDING
    return TASK + (DRAFT if "draft_email" in tools else "") + ENDING


def describe(obs: Observation, conn: sqlite3.Connection | None = None) -> str:
    """The item as the doer sees it: what it is, its ids, the people, and the task."""
    kind = "mail" if store.is_mail(obs.source) else obs.source
    lines = [f"Item ({kind}): {obs.title}"]
    if obs.when:
        lines.append(f"When: {obs.when}")
    if obs.detail:
        lines.append(f"Detail: {obs.detail}")
    for name, value in obs.ref.items():
        if value and name != "people":
            # An Outlook message id is 156 characters; the doer is shown a handle
            # it can pass back without mangling (agent/handles.py).
            lines.append(f"{name}: {handles.shorten_text(conn, name, value)}")
    guests = obs.ref.get("people") or []
    if guests and conn is not None:
        if block := people_block(conn, guests):
            lines.append(f"\nPeople in it, from the user's records:\n{block}")
    elif guests:
        lines.append(f"people: {', '.join(guests)}")
    lines.append(f"\nWhat to do: {obs.task}")
    return "\n".join(lines)


def outcome_of(text: str, tools_used: list[str], *, brief: bool = False) -> str | None:
    """What the user should read, or None if the turn did nothing.

    One line, or for a heads-up up to `BRIEF_LINES`.
    """
    text = (text or "").strip()
    if not text or text.upper().startswith(NOTHING):
        return None
    evidence = ACTING | RESEARCHING | (LOOKING if brief else frozenset())
    if not set(tools_used) & evidence:
        return None
    # Cut at a word, with "…" (agent/cut.py): cut mid-word, a line reached
    # their phone looking whole but with its end bitten off.
    if not brief:
        return cut(text.splitlines()[0].strip(), LINE_CHARS, mark="…")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return cut("\n".join(lines[:BRIEF_LINES]), BRIEF_CHARS, mark="…")


async def prepare(conn: sqlite3.Connection) -> PrepareResult:
    """Do up to a tick's worth of waiting tasks, within today's budget: the
    doer's calls are background work's `work` part (core/spend.py)."""
    with spend.spending("background", part="work", conn=conn):
        return await _prepare(conn)


async def _prepare(conn: sqlite3.Connection) -> PrepareResult:
    from agent import workers
    from agent.loop import UNATTENDED, Conversation

    result = PrepareResult()
    if refusal := spend.may_spend(conn):
        result.skipped = f"a spending cap is reached: {refusal.cap}"
        return result
    room = settings.MAX_TASKS_PER_DAY - store.tasks_started_today(conn)
    if room <= 0:
        result.skipped = "daily task limit reached"
        return result

    # A brief written long before its meeting is written again now that the
    # meeting is near. After the budget checks: nothing is sent back that
    # this tick will not look at.
    if sent_back := store.refresh_briefs(conn):
        log.info("sent %d pre-meeting brief(s) back to be written again", sent_back)

    waiting = store.tasks_waiting(conn, min(room, settings.MAX_TASKS_PER_TICK))
    if not waiting:
        return result

    for obs in waiting:
        if refusal := spend.may_spend(conn):
            result.skipped = f"a spending cap was reached mid-tick: {refusal.cap}"
            break
        # Marked before the turn, never after: a crash between the doer
        # creating an event and this row saying so would hand the task
        # to the next tick, which would create it again.
        store.start_task(conn, obs.id)
        # A conversation of its own for each task. One for the whole tick
        # sent each task the ones before it -- their mail, what was looked
        # up and drafted -- as if it were the same piece of work: a draft to
        # one person written beside another person's email, and every later
        # task in the tick paying for the earlier ones' history: a task's
        # turn averaged 30,000 input tokens that week, and one carrying six
        # earlier tasks 124,000 (measured 5 October 2026).
        # The critic judges its actions against the task the attention loop
        # set itself -- said to be its own, not theirs.
        convo = Conversation(conn, channel=settings.CHANNEL)
        convo.mandate = f"The attention loop's own task, not the user's words: {obs.task}"
        convo.start = UNATTENDED
        tools = tools_for(obs)
        try:
            reply = await workers.send_browsing(
                convo, f"{instructions(obs, tools)}\n\n{describe(obs, conn)}", tools
            )
        except Exception as exc:  # noqa: BLE001 - one task is not the tick
            log.warning("preparing %s failed: %s", obs.key, exc)
            result.failed += 1
            continue
        finally:
            convo.end()
        result.cost_usd += reply.cost_usd
        outcome = outcome_of(reply.text, reply.tools_used, brief=obs.say == "before")
        store.finish_task(conn, obs.id, outcome)
        if outcome:
            result.done.append(outcome)
        else:
            result.nothing += 1
    return result
