"""What the agent knows, as typed facts, and which of them a turn is shown.

Memory was a folder of paragraphs with a kind, and the kind decided where a
paragraph went: every `user` memory was put in front of every turn, on every
channel. On 8 October 2026, answering "Solve is done", the model kept
"Owen Tate's Northwind email format is lastname.firstname@northwind.example" as `user`
(memory 223) -- a firm's address format, kept as a standing fact about
the user and said in every turn after, the night's included.
Recall had no floor: none of its seven calls from 6 to 9 October returned
anything that answered the question, and its misses scored as its hits do.
A pasted registration (message 2584) became "The user's Contoso Day Out ... is
scheduled", with "They said it" in its body. And nothing could be corrected or
forgotten except what the night had numbered.

So a fact is a row that says whom it is about (`subject`), what it says
(`attribute`, `value`), where it came from (`source`, `source_ref`) and how
long it holds (`valid_from`, `valid_until`). Code, not the model, decides
what a turn sees:

- `profile`: who the user is (`IDENTITY`), and at most five other facts
  about them, from their own words or confirmed by them -- never an
  inference from their calendar, never someone else's.
- `relevant`: for the newest message only, the facts linked to something in
  it -- a name in the people ledger, a firm's domain, a word a fact carries
  -- and then those close to it in meaning above an absolute floor
  (`tools/memory.py` `FACT_FLOOR`), each with the reason it was chosen.
  Nothing below the floor is returned, however few that leaves.

A new value for the same subject and attribute supersedes the old one, and
nothing is deleted: a forgotten or superseded row stays, with what retired
it. The markdown memories stay as they were, the record of what was kept
before there were facts.

Blocking where it embeds (`add`, `correct`, `relevant`, `reembed`): call
those through `agent/offloop.py`. `profile` and the rest only read SQLite.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from array import array
from dataclasses import dataclass, field
from datetime import date, timedelta

from core import db, modules

log = logging.getLogger("facts")

# Fact numbers start here, so the number in "FORGET MEMORY <n>" is never a
# fact and a memory at once: the memories index numbered from 1 and had
# reached 250 by 10 October 2026, and the night's digests had already told
# them some of those numbers.
FIRST_ID = 1000

# Where a fact came from. `inferred:<what>` too: an inference says from what.
SOURCES = frozenset({"user_words", "confirmed", "mail", "web", "judge"})
# The subject of a fact about the user.
USER = "user"
# Who the user is, in their own words: always in the profile, ahead of the
# other facts and outside `PROFILE_MAX`, and what the app's profile page
# writes (`agent/worker/control.py`). Each is optional. The product's prompts
# say "the user"; these say who that is, so no prompt assumes a name, a
# pronoun, a town or a job.
IDENTITY = ("name", "preferred_name", "pronouns", "home_city", "occupation")
# The only sources a fact about them is said in every turn from. A fact the
# night read off their calendar ("The user is job hunting this autumn",
# 20 September) read as permanent and theirs; it is neither.
PROFILE_SOURCES = ("user_words", "confirmed")
# How strongly a source vouches, to keep the stronger when a fact is said again.
_STRENGTH = {"confirmed": 4, "user_words": 3, "mail": 2, "web": 2}
# Who the user is moves their weather, time zone and alerts, and decides
# who "the user" is. Any source could once write it: in the audit of 12
# October 2026 a background job's `home_city` replaced the one the user
# typed, and a name read from mail became the user's. So only the user's
# own sources (`PROFILE_SOURCES`) write it, and only theirs are read
# (`identity`, `_user_names`, agent/home.home_city).
NOT_THEIRS = ("who the user is ({attribute}) is kept only from their own words: ask them, "
              "or they can set it on the profile page")
# And as a value, not a sentence about it: `home_city` is geocoded, `name`
# decides who "the user" is. "The user lives in Leeds" is neither.
IN_THEIR_WORDS = ("who the user is ({attribute}) is kept as their own words for it and "
                  "nothing more, from what they wrote here -- \"Leeds\" for home_city. If "
                  "they did not write it, ask them, or they can set it on the profile page")

# The profile: five lines or fewer, their plans in the next two weeks first.
PROFILE_MAX = 5
SOON_DAYS = 14
# What `relevant` adds for one message: a few lines, not a page.
RELEVANT_MAX = 6
RELEVANT_CHARS = 600
# A value as a line shows it; a fact is a phrase, not a passage.
VALUE_CHARS = 300
LINE_VALUE_CHARS = 110

# Attributes that hold several values at once; any other takes one, and a
# new value supersedes the old.
MULTI_VALUED = frozenset({"plan", "preference", "note", "background", "alias", "trip"})
# Their own words about how the user writes go to the style guide (`writing_style`),
# not into every turn: a page about thank-you notes in front of a weather
# question was two of their four standing facts on 8 October 2026.
STYLE_PREFIX = "writing_"


@dataclass
class Fact:
    id: int
    subject: str  # "user" | "person:<people id or name>" | "org:<domain>" | "world"
    attribute: str
    value: str
    source: str  # "user_words" | "confirmed" | "inferred:<what>" | "mail" | "web" | "judge"
    source_ref: str | None = None  # e.g. "message:2584"
    valid_from: date | None = None
    valid_until: date | None = None
    status: str = "active"  # active | superseded | forgotten
    superseded_by: int | None = None
    # How the subject reads -- "Owen Tate", "Northwind" -- so a line needs no
    # ledger to say whom it is about.
    name: str | None = None
    created_at: str | None = None
    vector: list[float] | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "subject": self.subject, "name": self.name,
            "attribute": self.attribute, "value": self.value, "source": self.source,
            "source_ref": self.source_ref,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "status": self.status, "superseded_by": self.superseded_by,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Reading a row, and what a fact is said as
# ---------------------------------------------------------------------------


def _day(text) -> date | None:
    """A stored YYYY-MM-DD as a date, or None."""
    if isinstance(text, date):
        return text
    raw = str(text or "").strip()[:10]
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _unpack(blob) -> list[float] | None:
    if not blob:
        return None
    found = array("f")
    found.frombytes(bytes(blob))
    return list(found)


def _pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def _from_row(row: sqlite3.Row) -> Fact:
    keys = row.keys()
    return Fact(
        id=int(row["id"]), subject=row["subject"], attribute=row["attribute"],
        value=row["value"], source=row["source"], source_ref=row["source_ref"],
        valid_from=_day(row["valid_from"]), valid_until=_day(row["valid_until"]),
        status=row["status"], superseded_by=row["superseded_by"], name=row["name"],
        created_at=row["created_at"],
        vector=_unpack(row["embedding"]) if "embedding" in keys else None,
    )


def _today() -> date:
    """The day it is where the user is, as memory has it (so a test pins both)."""
    from tools import memory

    return memory._today()


def _who(fact: Fact) -> str:
    # "the user", not their name: the identity lines say who that is, and a
    # line (and its embedding) stays the same when they change their name.
    if fact.subject == USER:
        return "the user"
    if fact.subject == "world":
        return ""
    kind, _, rest = fact.subject.partition(":")
    if kind == "org":
        return f"{fact.name} ({rest})" if fact.name and fact.name.lower() != rest else rest
    return fact.name or rest


def _short_day(day: date, today: date | None = None) -> str:
    today = today or _today()
    return f"{day.day} {day.strftime('%b')}" + (f" {day.year}" if day.year != today.year else "")


def _noted(fact: Fact) -> date | None:
    """The local day a fact was written down."""
    text = str(fact.created_at or "")
    if len(text) <= 10:
        return _day(text)
    try:
        from agent import history

        moment = history.local(text)
        return moment.date() if moment else _day(text)
    except Exception:  # noqa: BLE001 - a date is decoration, never a failure
        return _day(text)


def _provenance(fact: Fact) -> str:
    source = fact.source
    if source == "user_words":
        said = "the user said so"
    elif source == "confirmed":
        said = "the user confirmed it"
    elif source.startswith("inferred:"):
        what = source.partition(":")[2]
        said = "inferred from their calendar" if what == "calendar" else f"inferred ({what})"
    else:
        said = {"mail": "from their mail", "web": "from the web",
                "judge": "the reviewer's"}.get(source, source)
    if fact.valid_until:
        return f"{said}; until {_short_day(fact.valid_until)}"
    if noted := _noted(fact):
        return f"{said}, {_short_day(noted)}"
    return said


def render_line(fact: Fact, reason: str | None = None) -> str:
    """One fact as a turn reads it: its number, whom it is about, what it
    says, where it came from -- and, from `relevant`, why it is here.

        [1003] Northwind (northwind.example), office hours: "9 to 5, closed Fridays"
        (the user said so, 8 Oct) -- you said "Owen" (Owen Tate, at northwind.example)

    The value is quoted, as data: the profile lines lead every turn, and a
    value written to read as more of the line -- a closing bracket, a
    provenance of its own, an instruction -- stays inside its quotes
    (audit of 12 October 2026, D3)."""
    from agent.cut import cut

    who = _who(fact)
    what = fact.attribute.replace("_", " ")
    head = f"{who}, {what}" if who else what
    value = json.dumps(cut(" ".join(fact.value.split()), LINE_VALUE_CHARS, mark="…"),
                       ensure_ascii=False)
    line = f"[{fact.id}] {head}: {value} ({_provenance(fact)})"
    return f"{line} -- {reason}" if reason else line


def _embedding_text(subject: str, name: str | None, attribute: str, value: str) -> str:
    who = _who(Fact(0, subject, attribute, value, "web", name=name))
    return f"{who} {attribute.replace('_', ' ')}: {value}".strip()


# ---------------------------------------------------------------------------
# What a fact may be, and about whom
# ---------------------------------------------------------------------------

_ATTRIBUTE = re.compile(r"[^a-z0-9]+")
_DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_ADDRESS = re.compile(r"([A-Za-z0-9._%+'-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# What the model may call the user as a subject, besides their own name
# (`_user_names`).
_USER = frozenset({USER, "me", "i", "myself"})


def _attribute(text: str) -> str:
    attribute = _ATTRIBUTE.sub("_", str(text or "").lower()).strip("_")[:40].strip("_")
    if not attribute:
        raise ValueError("a fact needs an attribute: a short name for what it says, like home_city")
    return attribute


def _value(text: str) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        raise ValueError("a fact needs a value")
    return value[:VALUE_CHARS]


def _source(text: str) -> str:
    source = str(text or "").strip().lower()
    if source in SOURCES or (source.startswith("inferred:") and source.partition(":")[2]):
        return source
    raise ValueError(f"source must be one of {sorted(SOURCES)} or inferred:<what>, got {text!r}")


def _ledger_person(conn: sqlite3.Connection, name: str):
    """The one real person in the people ledger with exactly this name, or
    None -- none, several, or no ledger to ask."""
    from tools import people

    key = people.name_key(name)
    if len(key.split()) < people.MIN_NAME_PARTS:
        return None
    try:
        rows = conn.execute(
            "SELECT * FROM people WHERE is_person = 1 AND name IS NOT NULL"
            f" AND {modules.live(conn, 'people')}").fetchall()
    except sqlite3.Error:
        return None
    found = [people.Person.from_row(r) for r in rows if people.name_key(r["name"]) == key]
    return found[0] if len(found) == 1 else None


def _user_names(conn: sqlite3.Connection) -> set[str]:
    """The user's own name and the name they go by, as they gave them,
    lowercased. Not the first word of their name: a friend who shared it
    became the user, and facts about the friend the user's."""
    marks = ", ".join("?" * len(PROFILE_SOURCES))
    try:
        rows = conn.execute(
            "SELECT value FROM facts WHERE subject = ? AND status = 'active'"
            f" AND attribute IN ('name', 'preferred_name') AND source IN ({marks})",
            (USER, *PROFILE_SOURCES)).fetchall()
    except sqlite3.Error:
        return set()
    return {" ".join(str(r["value"]).lower().split()) for r in rows} - {""}


def subject_of(conn: sqlite3.Connection, text: str) -> tuple[str, str | None]:
    """A subject as stored, and how it reads: "user", "world",
    "org:<domain>", or "person:<ledger id>" when the people ledger knows
    exactly one person by that name, else "person:<name>"."""
    raw = " ".join(str(text or "").split())
    low = raw.lower()
    if low in _USER or low in _user_names(conn):
        return USER, None
    if low == "world":
        return "world", None
    kind, sep, rest = raw.partition(":")
    kind, rest = kind.strip().lower(), rest.strip()
    if sep and rest and kind == "org":
        domain = rest.lower().split("@")[-1].removeprefix("www.")
        if _DOMAIN.fullmatch(domain):
            return f"org:{domain}", None
        raise ValueError(f"an org is its domain, like org:northwind.example -- not {rest!r}")
    if sep and rest and kind == "person":
        if rest.isdigit():
            row = conn.execute("SELECT name FROM people WHERE id = ?", (int(rest),)).fetchone()
            return f"person:{int(rest)}", (row["name"] if row else None)
        if person := _ledger_person(conn, rest):
            return f"person:{person.id}", person.name
        return f"person:{rest.lower()}", rest
    raise ValueError('subject must be "user", "person:<name>", "org:<domain>" or "world", '
                     f"got {text!r}")


def user_addresses(conn: sqlite3.Connection | None = None) -> frozenset[str]:
    """The user's own addresses, as mail delivers them
    (`core/mailbox.canonical`): with `conn`, the mail accounts the user
    added and the addresses they opted in for the own mailbox; without, none.

    Until step 5 a frozen policy line held them, shipped empty, so every
    address was someone else's and "my email is ..." was refused. Whole
    addresses, not words in them: the check before that took any address
    with the first name in it as theirs, so a colleague who shared it passed."""
    from core import mailbox as rules
    from core import modules

    if conn is None:
        return frozenset()
    found = [a for row in modules.accounts(conn, klass="mail") for a in row.addresses]
    found += list(rules.user_addresses(conn))
    return frozenset(rules.canonical(a) for a in found if a.strip())


def misfiled(subject: str, attribute: str, value: str, *,
             conn: sqlite3.Connection | None = None) -> str | None:
    """Why this is not a fact about the user, or None. One rule: a fact about
    the user holds no one else's address. Memory 223, a colleague's firm's
    email format, was kept about the user and said in every turn; a firm's
    format or a person's address is theirs, kept under them, where only a
    message that names them brings it up. With no address of theirs known
    it cannot tell, and says nothing (`unchecked`)."""
    from core import mailbox as rules

    if subject != USER:
        return None
    own = None
    for local, domain in _ADDRESS.findall(value):
        own = user_addresses(conn) if own is None else own
        if not own:
            return None
        if rules.canonical(f"{local}@{domain}") not in own:
            return (f"{local}@{domain} is not the user's address, so this is not a fact about "
                    "the user: "
                    'keep it under whom it is about -- "person:<their name>" for '
                    f'someone\'s address, "org:{domain.lower()}" for an organisation\'s.')
    return None


def unchecked(subject: str, value: str, *, conn: sqlite3.Connection | None = None) -> bool:
    """Whether a fact about the user holds an address nothing can check:
    there is one in it, and no address of theirs is known yet. Kept, but as
    an inference -- found when a message calls for it, never in the profile
    every turn reads -- until an account says whose it is."""
    return subject == USER and bool(_ADDRESS.search(value)) and not user_addresses(conn)


# ---------------------------------------------------------------------------
# Their words, or material the user passed on
# ---------------------------------------------------------------------------

# A message this long, or of this many lines, is read as material -- a
# registration, a table, a forwarded mail -- rather than their words about
# themselves. A fact kept from it is the agent's inference (`inferred:paste`):
# found when a message calls for it, never in the profile every turn reads.
# Not refused: some of their own dictations are long, and the user can confirm one.
PASTE_CHARS = 400
PASTE_LINES = 4


def pasted(text: str) -> bool:
    """Whether a message of theirs is to be read as material, not their words."""
    text = (text or "").strip()
    lines = [line for line in text.splitlines() if line.strip()]
    return len(text) > PASTE_CHARS or len(lines) >= PASTE_LINES


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def is_identity(subject: str, attribute: str) -> bool:
    """Whether a fact says who the user is (`IDENTITY`)."""
    return subject == USER and _attribute(attribute) in IDENTITY


def said_in(value: str, words: str) -> bool:
    """Whether `words`, the user's own, say `value`: every telling word of
    it, as they wrote it -- "Leeds" in "I moved to Leeds", "she" and "her"
    in "she/her". Who the user is comes from what they wrote, not from a
    page or an email the turn read; and it is the value alone, which is
    what the rest reads ("The user lives in Leeds" is not a town to
    forecast, and "lives" is not in what they wrote)."""
    theirs = {w.casefold() for w in _WORD.findall(words or "")}
    said = [w.casefold() for w in _WORD.findall(value or "")]
    telling = [w for w in said if w not in _STOP] or said
    return bool(telling) and all(w in theirs for w in telling)


def _refusal(held: Fact, source: str) -> str | None:
    """Why a fact from `source` may not replace `held`, or None. The user's
    own word replaces anything; nothing else replaces who the user is, or a
    fact from a stronger source."""
    if source in PROFILE_SOURCES:
        return None
    if is_identity(held.subject, held.attribute):
        return NOT_THEIRS.format(attribute=held.attribute)
    if _STRENGTH.get(held.source, 1) > _STRENGTH.get(source, 1):
        return (f"fact {held.id} says {held.value!r} ({_provenance(held)}), and what "
                f"{source} says does not replace it: ask the user")
    return None


def _norm(value: str) -> str:
    return " ".join(str(value or "").lower().split()).rstrip(".")


def _same(value: str, other: str, source: str) -> bool:
    """Whether a value from `source` says what `other` does. Typed on the
    profile page, exactly: "maya" corrected to "Maya" is a change the user
    made, and was dropped as the same. Said or found, up to case and a
    closing full stop."""
    return value == other if source == "confirmed" else _norm(value) == _norm(other)


def get(conn: sqlite3.Connection, fact_id: int) -> Fact | None:
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (int(fact_id),)).fetchone()
    return _from_row(row) if row else None


def _embed(subject: str, name: str | None, attribute: str, value: str) -> list[float] | None:
    from tools import memory

    try:
        return memory.embed([_embedding_text(subject, name, attribute, value)])[0]
    except Exception:  # noqa: BLE001 - a fact without a vector is still linked by name
        log.warning("could not embed a fact; it is found by name only", exc_info=True)
        return None


def _settle(conn: sqlite3.Connection, subject: str, attribute: str, value: str,
            source: str, source_ref: str | None, until: date | None,
            ) -> tuple[Fact | None, list[Fact]]:
    """What the facts held make of a new value: the fact itself when it is
    held already -- with the stronger source and the later last day, if
    this one brings either -- else None, with the facts held. Raises
    ValueError when this source may not replace one of them."""
    held = [_from_row(r) for r in conn.execute(
        "SELECT * FROM facts WHERE subject = ? AND attribute = ? AND status = 'active' ORDER BY id",
        (subject, attribute))]
    if attribute not in MULTI_VALUED:
        for fact in held:
            if not _same(value, fact.value, source) and (why := _refusal(fact, source)):
                raise ValueError(why)
    for fact in held:
        if not _same(value, fact.value, source):
            continue
        stronger = _STRENGTH.get(source, 1) > _STRENGTH.get(fact.source, 1)
        later = until is not None and (fact.valid_until is None or until > fact.valid_until)
        if not (stronger or later):
            return fact, held
        with db.transaction(conn):
            conn.execute(
                "UPDATE facts SET source = ?, source_ref = COALESCE(?, source_ref),"
                " valid_until = ?, updated_at = datetime('now') WHERE id = ?",
                (source if stronger else fact.source, source_ref if stronger else None,
                 (until if later else fact.valid_until).isoformat()
                 if (later or fact.valid_until) else None, fact.id))
        return get(conn, fact.id), held
    return None, held


def add(conn: sqlite3.Connection, subject: str, attribute: str, value: str, *, source: str,
        source_ref: str | None = None, valid_until=None, valid_from=None,
        name: str | None = None) -> Fact:
    """Keep a fact. Unique per (subject, attribute) unless the attribute is
    multi-valued (`MULTI_VALUED`): a new value supersedes the one held, and
    the same value said again is the fact already held -- with the stronger
    source and the later last day, if this one brings either.

    Raises ValueError for a subject, attribute, source or date it cannot
    read, said back to the model to fix."""
    subject, ledger_name = subject_of(conn, subject)
    name = " ".join(str(name).split()) if name else ledger_name
    attribute = _attribute(attribute)
    value = _value(value)
    source = _source(source)
    until, start = _day(valid_until), _day(valid_from)
    if valid_until and until is None:
        raise ValueError(f"valid_until must be a date, YYYY-MM-DD, got {valid_until!r}")
    if is_identity(subject, attribute) and source not in PROFILE_SOURCES:
        raise ValueError(NOT_THEIRS.format(attribute=attribute))

    found, _ = _settle(conn, subject, attribute, value, source, source_ref, until)
    if found is not None:
        return found
    # Embedded before the write, so no lock is held across it.
    vector = _embed(subject, name, attribute, value)
    with db.transaction(conn):
        # Settled again under the write lock: another writer may have kept
        # this value, or another, while this one was embedded. Two quick
        # saves of the profile page left two active names.
        found, held = _settle(conn, subject, attribute, value, source, source_ref, until)
        if found is not None:
            return found
        first = conn.execute("SELECT 1 FROM facts LIMIT 1").fetchone() is None
        new_id = conn.execute(
            "INSERT INTO facts (id, subject, name, attribute, value, source, source_ref,"
            " valid_from, valid_until, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " RETURNING id",
            (FIRST_ID if first else None, subject, name, attribute, value, source, source_ref,
             start.isoformat() if start else None, until.isoformat() if until else None,
             _pack(vector) if vector else None)).fetchone()[0]
        if attribute not in MULTI_VALUED:
            for fact in held:
                conn.execute(
                    "UPDATE facts SET status = 'superseded', superseded_by = ?,"
                    " ended_by = 'superseded', updated_at = datetime('now') WHERE id = ?",
                    (new_id, fact.id))
    return get(conn, new_id)


def correct(conn: sqlite3.Connection, fact_id: int, value: str, *,
            source_ref: str | None = None, source: str = "user_words") -> Fact:
    """Replace a fact's value: a new row, the old one superseded by it.
    Raises ValueError if no such fact is held."""
    old = get(conn, fact_id)
    if old is None or old.status != "active":
        raise ValueError(f"no fact {fact_id} is held")
    value = _value(value)
    source = _source(source)
    if _same(value, old.value, source):
        return old
    if why := _refusal(old, source):
        raise ValueError(why)
    vector = _embed(old.subject, old.name, old.attribute, value)
    with db.transaction(conn):
        # Still held? Corrected twice at once, both corrections were kept.
        if get(conn, fact_id).status != "active":
            raise ValueError(f"fact {fact_id} was replaced meanwhile; read it again")
        new_id = conn.execute(
            "INSERT INTO facts (subject, name, attribute, value, source, source_ref,"
            " valid_from, valid_until, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (old.subject, old.name, old.attribute, value, source, source_ref,
             old.valid_from.isoformat() if old.valid_from else None,
             old.valid_until.isoformat() if old.valid_until else None,
             _pack(vector) if vector else None)).fetchone()[0]
        conn.execute(
            "UPDATE facts SET status = 'superseded', superseded_by = ?, ended_by = 'corrected',"
            " updated_at = datetime('now') WHERE id = ?", (new_id, old.id))
    return get(conn, new_id)


def forget(conn: sqlite3.Connection, fact_id: int, *, ended_by: str = "forgotten") -> bool:
    """Stop holding a fact. The row stays, marked forgotten."""
    with db.transaction(conn):
        done = conn.execute(
            "UPDATE facts SET status = 'forgotten', ended_by = ?, updated_at = datetime('now')"
            " WHERE id = ? AND status = 'active'", (ended_by, int(fact_id))).rowcount
    return bool(done)


def forget_number(conn: sqlite3.Connection, number: int) -> str | None:
    """Forget what "FORGET MEMORY <number>" names, and say what it was: a
    fact, from `FIRST_ID` up, when one is held by that number; else the
    memory the index numbers so (`memory.forget_number`), which is how the
    night's digests numbered what they kept until it kept facts (10 October
    2026). None when neither is held.

    A fact first, then a memory: the memories index numbers from 1 and
    could one day pass 1000, and a number the user was told for a fact means the
    fact. Blocking when it falls through to a memory (a git commit)."""
    from tools import memory

    if number >= FIRST_ID and (fact := get(conn, number)) is not None:
        if fact.status != "active" or not forget(conn, number):
            return None
        return render_line(fact)
    return memory.forget_number(conn, number)


# Where a turn's `remember` notes the numbers of the facts it kept, in
# `Deps.turn_cache` (shared by every hop), for the reply to say them.
TURN_KEPT = "facts_kept"


def acknowledgement(kept: list[int]) -> str:
    """What a reply that kept facts says, if it did not say their numbers:
    every new fact is told with its number, so FORGET MEMORY works for any
    of them, not only the night's."""
    ids = list(dict.fromkeys(kept))
    if not ids:
        return ""
    if len(ids) == 1:
        return f"(Kept as fact {ids[0]}; FORGET MEMORY {ids[0]} drops it.)"
    listed = ", ".join(str(i) for i in ids[:-1]) + f" and {ids[-1]}"
    return f"(Kept as facts {listed}; FORGET MEMORY <number> drops one.)"


def acknowledged(reply: str, kept: list[int]) -> str:
    """`reply`, with the numbers of the facts kept in its turn added if it
    does not already give each of them."""
    missing = [i for i in dict.fromkeys(kept) if not re.search(rf"\b{i}\b", reply or "")]
    if not missing:
        return reply
    note = acknowledgement(missing)
    return f"{reply.rstrip()}\n\n{note}" if (reply or "").strip() else note


# ---------------------------------------------------------------------------
# Reading: the profile, and what is relevant to one message
# ---------------------------------------------------------------------------


def _held(conn: sqlite3.Connection, today: date, *, where: str = "",
          args: tuple = ()) -> list[Fact]:
    """Active facts that hold today."""
    day = today.isoformat()
    rows = conn.execute(
        "SELECT * FROM facts WHERE status = 'active'"
        " AND (valid_from IS NULL OR valid_from <= ?)"
        " AND (valid_until IS NULL OR valid_until >= ?)" + where + " ORDER BY id",
        (day, day, *args)).fetchall()
    return [_from_row(r) for r in rows]


def identity(conn: sqlite3.Connection) -> dict[str, str]:
    """Each of `IDENTITY` as the user gave it, "" for one not given."""
    marks = ", ".join("?" * len(IDENTITY))
    theirs = ", ".join("?" * len(PROFILE_SOURCES))
    # Unreadable raises, never reads as nothing given: the user's name here
    # is what agent/modshare.py keeps a module's code from sending out.
    rows = conn.execute(
        "SELECT attribute, value FROM facts WHERE subject = ? AND status = 'active'"
        f" AND attribute IN ({marks}) AND source IN ({theirs}) ORDER BY id",
        (USER, *IDENTITY, *PROFILE_SOURCES)).fetchall()
    given = {r["attribute"]: r["value"] for r in rows}
    return {a: given.get(a, "") for a in IDENTITY}


def held(conn: sqlite3.Connection, *, today: date | None = None) -> list[Fact]:
    """Every fact that holds today, whoever it is about, oldest first."""
    return _held(conn, today or _today())


def profile(conn: sqlite3.Connection, *, today: date | None = None) -> list[Fact]:
    """Who the user is: their identity (`IDENTITY`, in that order), then
    five facts or fewer about them, from their own words or confirmed by
    them, holding today. Their plans in the next two weeks first, then what
    they confirmed, then what they said, the newest first of each: a fact
    the night kept from their words (agent/night/learning.py) does not push
    out one they approved. Never an inference, never a fact about someone
    else, never their words about how they write (the style guide has
    those)."""
    today = today or _today()
    marks = ", ".join("?" * len(PROFILE_SOURCES))
    found = [f for f in _held(conn, today,
                              where=f" AND subject = ? AND source IN ({marks})",
                              args=(USER, *PROFILE_SOURCES))
             if not f.attribute.startswith(STYLE_PREFIX)]
    who = sorted((f for f in found if f.attribute in IDENTITY),
                 key=lambda f: (IDENTITY.index(f.attribute), -f.id))
    found = [f for f in found if f.attribute not in IDENTITY]
    soon = today + timedelta(days=SOON_DAYS)
    plans = sorted((f for f in found if f.valid_until and f.valid_until <= soon),
                   key=lambda f: (f.valid_until, f.id))
    rest = sorted((f for f in found if f not in plans),
                  key=lambda f: (f.source != "confirmed", -f.id))
    return who + (plans + rest)[:PROFILE_MAX]


def stated_style(conn: sqlite3.Connection, *, today: date | None = None) -> list[str]:
    """Their own words about how the user writes (`writing_*` facts about them, said
    or confirmed by them), for the style guide `writing_style` returns before
    a draft. Held out of the profile every turn reads (`profile`), so this
    is the one place they reach a model: written on 10 October 2026 for the
    tool and not wired to it until the 11th, and their confirmed note on
    recruiting thank-you notes reached no draft in between."""
    today = today or _today()
    marks = ", ".join("?" * len(PROFILE_SOURCES))
    return [f"{f.attribute.removeprefix(STYLE_PREFIX).replace('_', ' ')}: {f.value}"
            for f in _held(conn, today, where=f" AND subject = ? AND source IN ({marks})"
                           " AND attribute LIKE ?",
                           args=(USER, *PROFILE_SOURCES, f"{STYLE_PREFIX}%"))]


# Words too common to link a fact by, alone.
_STOP = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does", "did", "for",
    "from", "had", "has", "have", "he", "her", "him", "his", "how", "i", "if", "in", "into",
    "is", "it", "its", "me", "my", "no", "not", "of", "on", "or", "our", "she", "so", "than",
    "that", "the", "their", "them", "then", "there", "they", "this", "to", "too", "up", "us",
    "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with", "you",
    "your", "about", "after", "again", "all", "also", "am", "any", "back", "been", "before",
    "being", "both", "come", "could", "day", "each", "even", "get", "go", "going", "good",
    "got", "great", "just", "know", "last", "let", "like", "look", "make", "many", "more",
    "most", "much", "new", "next", "now", "off", "ok", "okay", "one", "only", "other", "out",
    "over", "please", "right", "said", "say", "see", "should", "some", "still", "sure", "take",
    "tell", "thank", "thanks", "think", "time", "today", "tomorrow", "two", "use", "very",
    "want", "way", "well", "week", "work", "would", "yes", "yet", "email", "emails", "pattern",
    "format", "home", "city", "note", "notes", "plan", "plans", "preference", "background",
    "name", "about", "user"
])
_WORD = re.compile(r"[^\W_]+")
_CAPS_RUN = re.compile(r"(?:[A-Z][\w&'-]*)(?:\s+[A-Z][\w&'-]*)*")
_GENERIC_ATTRIBUTE = frozenset({"email", "pattern", "format", "home", "city", "note", "plan",
                                "preference", "background", "name", "about", "writing",
                                "notes", "style", "info", "detail", "details", "fact"})


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").replace("’", "'").lower())


def _contains(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    if not n:
        return False
    if n == 1:
        return phrase[0] in tokens
    return any(tuple(tokens[i:i + n]) == phrase for i in range(len(tokens) - n + 1))


def _phrase(text: str) -> tuple[str, ...]:
    return tuple(_tokens(text))


def _single_ok(word: str) -> bool:
    return len(word) >= 3 and word not in _STOP and not word.isdigit()


@dataclass
class _Ledger:
    """The people ledger as linking needs it: each real person's name and
    addresses. Empty when there is no ledger to read."""

    people: list[tuple[int, str, set[str]]]

    @classmethod
    def read(cls, conn: sqlite3.Connection) -> _Ledger:
        try:
            rows = conn.execute(
                "SELECT id, name, email FROM people WHERE is_person = 1 AND name IS NOT NULL"
                f" AND {modules.live(conn, 'people')}"
            ).fetchall()
            aliases = conn.execute("SELECT person_id, email FROM person_aliases").fetchall()
        except sqlite3.Error:
            return cls([])
        extra: dict[int, set[str]] = {}
        for row in aliases:
            extra.setdefault(int(row["person_id"]), set()).add(str(row["email"]).lower())
        return cls([(int(r["id"]), str(r["name"]), {str(r["email"]).lower()}
                     | extra.get(int(r["id"]), set())) for r in rows])

    def name_of(self, person_id: int) -> str | None:
        return next((name for pid, name, _ in self.people if pid == person_id), None)

    def at(self, domain: str) -> list[str]:
        """Names of the people with an address at this domain."""
        return [name for _, name, emails in self.people
                if any(e.endswith("@" + domain) or e.endswith("." + domain) for e in emails)]


def _clean_name(name: str) -> str:
    """"Tate, Owen (Northwind)" -> "Owen Tate"."""
    name = re.sub(r"\([^)]*\)", " ", name or "")
    if "," in name:
        last, _, first = name.partition(",")
        name = f"{first} {last}"
    return " ".join(name.split())


def _name_links(name: str, rank: int, reason_of) -> list[tuple[tuple[str, ...], int, str]]:
    """A person's full name, then each part of it on its own, one rank down."""
    name = _clean_name(name)
    whole = _phrase(name)
    out = []
    if len(whole) >= 2:
        out.append((whole, rank, reason_of(name)))
    for part in whole:
        if _single_ok(part):
            out.append(((part,), rank + 1, reason_of(part)))
    return out


def _links(fact: Fact, ledger: _Ledger,
           facts: list[Fact]) -> list[tuple[tuple[str, ...], int, str]]:
    """What in a message links to this fact: (words, rank, reason), lower
    rank first. Names before value words before attribute words."""
    out: list[tuple[tuple[str, ...], int, str]] = []
    kind, _, rest = fact.subject.partition(":")

    def said(words: str, also: str = "") -> str:
        return f'you said "{words}"' + (f" ({also})" if also else "")

    if kind == "person":
        name = (ledger.name_of(int(rest)) if rest.isdigit() else None) or fact.name or rest
        out += _name_links(name, 0, lambda w: said(w))
    elif kind == "org":
        labels = [p for p in rest.split(".") if p]
        label = labels[-2] if len(labels) >= 2 else labels[0]
        if _single_ok(label) or len(label) >= 2:
            out.append(((label,), 0, said(label.upper() if len(label) <= 4 else label)))
        if fact.name:
            out.append((_phrase(fact.name), 0, said(fact.name)))
        # Someone at the firm, by name: "a thank-you to Owen" is about Northwind's
        # address format. From the ledger, and from facts that give a
        # person's address there (a fact can know one the ledger does not).
        names = set(ledger.at(rest))
        for other in facts:
            if other.subject.startswith("person:") and other.name and \
                    re.search(rf"@(?:[\w-]+\.)*{re.escape(rest)}\b", other.value, re.IGNORECASE):
                names.add(other.name)
        for person in sorted(names):
            clean = _clean_name(person)
            out += _name_links(clean, 1, lambda w, c=clean: said(w, f"{c}, at {rest}"))
    # Proper nouns in the value: "Pittsburgh", "Mira Shah", "Contoso".
    for run in _CAPS_RUN.findall(fact.value):
        words = _phrase(run)
        if len(words) >= 2 or words and _single_ok(words[0]) and len(words[0]) >= 4:
            out.append((words, 2, said(run)))
    for part in fact.attribute.split("_"):
        if part not in _GENERIC_ATTRIBUTE and _single_ok(part):
            out.append(((part,), 3, said(part)))
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    """Their cosine similarity. Vectors of different lengths are from
    different models and do not compare: ValueError. Zipped loosely, they
    were cut to the shorter and given a score that meant nothing."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def linked(conn: sqlite3.Connection, text: str, pool: list[Fact]) -> dict[int, tuple[int, str]]:
    """The facts in `pool` that something in `text` names: id -> (rank,
    reason). No embedding: names, domains and words only."""
    tokens = _tokens(text)
    if not tokens or not pool:
        return {}
    ledger = _Ledger.read(conn)
    found: dict[int, tuple[int, str]] = {}
    for fact in pool:
        for words, rank, reason in _links(fact, ledger, pool):
            if _contains(tokens, words) and (fact.id not in found or rank < found[fact.id][0]):
                found[fact.id] = (rank, reason)
    return found


def relevant(conn: sqlite3.Connection, text: str, *, limit: int = RELEVANT_MAX,
             today: date | None = None) -> list[tuple[Fact, str]]:
    """The facts this message calls for, each with why: for the newest
    message only, and never the profile's (which every turn has already).

    Linked first -- a name in the people ledger or on a fact, a firm's
    domain, a word a fact carries -- then close in meaning, at or above the
    absolute floor (`memory.FACT_FLOOR`) and never below it. At most
    `limit` facts and `RELEVANT_CHARS` characters as `render_line` says
    them. Blocking: it embeds the message when any fact is left unlinked."""

    today = today or _today()
    if not (text or "").strip():
        return []
    shown = {f.id for f in profile(conn, today=today)}
    pool = [f for f in _held(conn, today) if f.id not in shown]
    if not pool:
        return []
    found = linked(conn, text, pool)
    _close(text, pool, found, "close to what the user said")
    by_id = {f.id: f for f in pool}
    ordered = sorted(found.items(), key=lambda item: (item[1][0], -item[0]))
    out: list[tuple[Fact, str]] = []
    used = 0
    for fact_id, (_, reason) in ordered:
        line = render_line(by_id[fact_id], reason)
        if out and used + len(line) > RELEVANT_CHARS:
            continue
        out.append((by_id[fact_id], reason))
        used += len(line) + 1
        if len(out) >= limit:
            break
    return out


def search(conn: sqlite3.Connection, query: str, *, limit: int = 5,
           today: date | None = None) -> list[tuple[Fact, str]]:
    """`relevant` for the `recall` tool: the profile's facts too, since a
    question asked of recall is asked of everything held."""

    today = today or _today()
    pool = _held(conn, today)
    if not pool or not (query or "").strip():
        return []
    found = linked(conn, query, pool)
    _close(query, pool, found, "close to the question")
    by_id = {f.id: f for f in pool}
    ordered = sorted(found.items(), key=lambda item: (item[1][0], -item[0]))[:limit]
    return [(by_id[i], reason) for i, (_, reason) in ordered]


def _close(text: str, pool: list[Fact], found: dict[int, tuple[int, str]], why: str) -> None:
    """Add to `found` the unlinked facts in `pool` at or above the floor in
    meaning, ranked after every linked one. An embedder that fails adds
    nothing: what was linked by name is still worth giving."""
    from tools import memory

    rest = [f for f in pool if f.id not in found and f.vector]
    if not rest:
        return
    try:
        vector = memory.embed([text[:memory.EMBED_CHARS]])[0]
    except Exception:  # noqa: BLE001 - see above
        log.warning("could not embed a message for its facts", exc_info=True)
        return
    for fact in rest:
        try:
            score = _cosine(vector, fact.vector)
        except ValueError:
            # Another model's vector, which `db.open_store` should have
            # emptied: found by name only until `reembed` gives it one.
            log.warning("fact %d has a vector of %d, not %d", fact.id, len(fact.vector),
                        len(vector))
            continue
        if score >= memory.FACT_FLOOR:
            found[fact.id] = (10 + round(1 - score, 4), f"{why} ({score:.2f})")


# What a turn reads, in front of the facts `relevant` chose: not their words,
# and not a list to recite.
RELEVANT_HEAD = ("(Not the user's words: what you know that their message calls for, each "
                 "with why it is here. Use what helps the answer; leave the rest unsaid. The "
                 "number corrects or forgets one.)")


# The profile's line for a user who gave a name and no pronouns: the
# neutral ones, as a value, until they give theirs.
NO_PRONOUNS = "- the user, pronouns: they/them (the user has not given theirs)"


def profile_text(conn: sqlite3.Connection, *, today: date | None = None) -> str:
    """The profile as the instructions say it: who the user is, and what
    they have said about themselves. "" only if it cannot be read."""
    try:
        found = profile(conn, today=today)
    except Exception:  # noqa: BLE001 - a turn is never lost to this
        log.warning("could not read the profile", exc_info=True)
        return ""
    if not found:
        return ("About the user: nothing yet. They have not said who they are, and nothing "
                "they said about themselves has been kept.")
    lines = [f"- {render_line(f)}" for f in found]
    given = {f.attribute for f in found}
    if given & {"name", "preferred_name"} and "pronouns" not in given:
        # A name invites a guess at pronouns. Given "Kenji Mori" and no line
        # for them, the model wrote "He is speaking at this community event"
        # (the persona eval, 12 October 2026), the system prompt's "with
        # none given, use their name or they" notwithstanding: a rule read
        # far from the name loses to the name. A bare "none given" beside it
        # did not move it either, and "none given, so their name or they"
        # still slipped one bio in eight. Stated as a value, like any other
        # line here, it held eight of eight.
        lines.insert(sum(f.attribute in IDENTITY for f in found),
                     NO_PRONOUNS)
    return "About the user, from their own words (`recall` has more):\n" + "\n".join(lines)


def relevant_text(conn: sqlite3.Connection, text: str, *, today: date | None = None) -> str:
    """`relevant`'s facts as lines for the turn, or "". Blocking: it embeds."""
    return "\n".join(f"- {render_line(f, why)}"
                     for f, why in relevant(conn, text, today=today))


def reembed(conn: sqlite3.Connection) -> int:
    """Give each active fact without a vector one; return how many.

    Without a vector a fact is found by name only, and two things leave it
    so: `db.open_store` empties every vector for a new model, and a fact
    kept while the embedder could not answer is kept without one. Blocking:
    it embeds, all in one request where it can."""
    from tools import memory

    rows = conn.execute("SELECT id, subject, name, attribute, value FROM facts"
                        " WHERE status = 'active' AND embedding IS NULL ORDER BY id").fetchall()
    if not rows:
        return 0
    found = memory.embed([_embedding_text(r["subject"], r["name"], r["attribute"], r["value"])
                          for r in rows])
    with db.transaction(conn):
        for row, vector in zip(rows, found, strict=True):
            conn.execute("UPDATE facts SET embedding = ? WHERE id = ? AND embedding IS NULL",
                         (_pack(vector), row["id"]))
    return len(rows)


# ---------------------------------------------------------------------------
# From the command line: what is held
# ---------------------------------------------------------------------------


def _main() -> None:
    """What is held, a line each: `python -m agent.facts`."""
    from core import db

    conn = db.open_store()
    for fact in held(conn):
        print(f"  {render_line(fact)}")


if __name__ == "__main__":
    _main()
