"""Who the user actually deals with, assembled from mail the user already has.

Ingest here is **pure code**. Sender, recipient, subject, timestamp,
direction -- no content leaves the machine and no model is consulted, which is
what makes reading received mail acceptable at all. Extraction, which does
involve a model, reads only their *sent* mail and lives in `tools/extract.py`.

The problem this module is mostly about is not storage, it is the ~800
"people" a real inbox produces, of whom ~780 are Substack, LinkedIn and
DoorDash. That is the watcher's twenty-five-recurring-lectures failure in a
new costume, and the fix is the same shape: filter on a property of the item
*before* it becomes something you have to look at.

The strongest such property is free and structural: **the user wrote to them.**
Nobody replies to a receipt. Everything else -- `noreply@`, `notifications@`,
a domain that has sent forty times and received nothing -- is a heuristic
underneath that one fact.

Flagged rather than dropped (`is_person = 0`), so a false negative is a single
UPDATE later instead of a re-ingest of a year of mail.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from core import db, modules

# Local parts that are a mailbox nobody reads. Deliberately a small list of
# strong signals rather than a long list of weak ones: the expensive mistake
# here is flagging a real person, and `sent_count > 0` rescues anyone this
# gets wrong.
_AUTOMATED = re.compile(
    r"^(notifications?|alerts?|mailer|bounces?|postmaster|updates?|"
    r"newsletter|news|digest|info|support|help|billing|receipts?|invoices?|"
    r"orders?|confirm\w*|automated|robot|bot|system|admin|webmaster)"
    r"([-_.+].*)?$",
    re.IGNORECASE,
)

# Tokens strong enough to mean "nobody reads this" wherever they appear, not
# only at the start. `google-noreply@` slipped through an anchored pattern on
# the first real pass, which is exactly the kind of near-miss that puts a
# service in the contact list forever.
_NEVER_READ = re.compile(
    r"(^|[-_.])(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|unsubscribe|"
    r"noreply)([-_.+]|$)",
    re.IGNORECASE,
)

# Local parts that end in -api. A service writing to you about your account
# is not a correspondent, and "search-api@" was the one the first real pass
# let through.
_API = re.compile(r"(^|[-_.])api$", re.IGNORECASE)

# Domains that exist to send. A company's real people are at `company.com`;
# `news.company.com` and `email.company.com` are the bulk sender, and a
# domain with "message" or "notification" in it has told you what it is.
_SENDING_DOMAIN = re.compile(
    r"^(news|newsletter|email|e|mail|mailer|notify|notification|updates?|"
    r"marketing|campaigns?|send|smtp|reply|alerts?)\.",
    re.IGNORECASE,
)
_SERVICE_WORD = re.compile(
    r"(message|notification|mailer|marketing|campaign|noreply)", re.IGNORECASE
)

# A sender the user has never written to, who has written to the user this many
# times, is a list. A person who mailed the user twice and got no reply is not.
BULK_THRESHOLD = 4

_ADDRESS = re.compile(r"<([^<>]+)>")
_NAME = re.compile(r'^\s*"?([^"<]*?)"?\s*<')


@dataclass
class Person:
    id: int
    email: str
    name: str | None
    org: str | None
    note: str | None
    is_person: bool
    sent_count: int
    recv_count: int
    first_seen: str | None
    last_seen: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Person:
        return cls(
            id=int(row["id"]), email=row["email"], name=row["name"],
            org=row["org"], note=row["note"], is_person=bool(row["is_person"]),
            sent_count=int(row["sent_count"]), recv_count=int(row["recv_count"]),
            first_seen=row["first_seen"], last_seen=row["last_seen"],
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id, "email": self.email, "name": self.name,
            "org": self.org, "note": self.note,
            "sent_count": self.sent_count, "recv_count": self.recv_count,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
        }


# ---------------------------------------------------------------------------
# Reading an address
# ---------------------------------------------------------------------------


def parse_address(raw: str) -> tuple[str, str | None]:
    """"Clara Moss <clara@northwind.example>" -> ("clara@northwind.example", "Clara Moss")."""
    raw = (raw or "").strip()
    match = _ADDRESS.search(raw)
    email = (match.group(1) if match else raw).strip().lower()
    name = None
    named = _NAME.match(raw)
    if named and named.group(1).strip():
        name = named.group(1).strip()
    return email, name


def parse_addresses(raw: str) -> list[tuple[str, str | None]]:
    """Split a To/Cc header. Commas inside quoted names stay put."""
    parts, buf, quoted = [], [], False
    for ch in raw or "":
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [parse_address(p) for p in parts if p.strip()]


def looks_automated(email: str, name: str | None = None) -> bool:
    """Is this a mailbox nobody reads?"""
    local, _, host = (email or "").partition("@")
    if _AUTOMATED.match(local) or _NEVER_READ.search(local) or _API.search(local):
        return True
    if _SENDING_DOMAIN.match(host) or _SERVICE_WORD.search(host):
        return True
    # "Northwind Careers via LinkedIn" and friends: the relay is in the name, not
    # the address, and the address is a per-recipient hash that would
    # otherwise become its own "person" every single time.
    return bool(name and re.search(r"\bvia\b", name, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def upsert(
    conn: sqlite3.Connection,
    email: str,
    *,
    name: str | None = None,
    seen_at: str | None = None,
    sent: bool = False,
    count: bool = True,
) -> int:
    """Record that this address exists, and when it was last seen.

    A name only ever fills a blank, never overwrites: the header name varies
    per message ("clara", "Clara Moss", "Moss, Clara") and letting the most
    recent one win means the row changes identity at random.

    `count=False` leaves the counts alone, for a reader that may see the same
    message again: the ingest counts an exchange only when it is first
    logged (`counted`). Counting on every read inflated them -- each night
    re-reads three days -- and the bulk rule (then `flag_bulk`, now
    `recount`) demoted people who had written twice as though they were a
    mailing list.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return 0
    seen_at = seen_at or datetime.now().isoformat(timespec="seconds")
    column = "sent_count" if sent else "recv_count"
    bump = f"{column} = {column} + 1, " if count else ""

    # A merged address belongs to the surviving row. Without this the next
    # message to it recreates exactly the duplicate the merge removed, and
    # the ledger un-merges itself overnight.
    if (owner := resolve_alias(conn, email)) is not None:
        with db.transaction(conn):
            conn.execute(
                f"UPDATE people SET {bump}"
                f"{'is_person = 1, ' if sent else ''}"
                " last_seen = MAX(COALESCE(last_seen, ''), ?),"
                " updated_at = datetime('now') WHERE id = ?",
                (seen_at, owner),
            )
        return owner

    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO people (email, name, is_person, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                name = COALESCE(people.name, excluded.name),
                last_seen = MAX(COALESCE(people.last_seen, ''), excluded.last_seen),
                first_seen = MIN(COALESCE(people.first_seen, excluded.first_seen),
                                 excluded.first_seen),
                updated_at = datetime('now')
            """,
            (email, name, 0 if looks_automated(email, name) else 1, seen_at, seen_at),
        )
        # Mail the user wrote settles it. Whatever the heuristics thought, the user
        # does not reply to a shipping notification.
        if count or sent:
            conn.execute(
                f"UPDATE people SET {bump}"
                f"{'is_person = 1, ' if sent else ''}"
                " updated_at = datetime('now') WHERE email = ?",
                (email,),
            )
    row = conn.execute("SELECT id FROM people WHERE email = ?", (email,)).fetchone()
    return int(row["id"]) if row else 0


def counted(conn: sqlite3.Connection, person_id: int, *, sent: bool) -> None:
    """Count one newly logged exchange -- once, however often it is read."""
    column = "sent_count" if sent else "recv_count"
    with db.transaction(conn):
        conn.execute(
            f"UPDATE people SET {column} = {column} + 1 WHERE id = ?", (person_id,)
        )


def recount(conn: sqlite3.Connection, threshold: int = BULK_THRESHOLD) -> int:
    """Counts from the log of exchanges, and whether each is a person from them.

    The log (`interactions`) is unique per person and message, so it is the
    truth the counts drifted from. The rule for a person is the one the
    ledger always applied, now in one place: anyone the user has written to is a
    person; otherwise a mailbox that looks automated, or that has written
    `threshold` times without a word back, is not. Returns how many people
    changed side -- in both directions, so those demoted by inflated counts
    come back.
    """
    with db.transaction(conn):
        conn.execute(
            """
            UPDATE people SET
                sent_count = (SELECT COUNT(*) FROM interactions i
                              WHERE i.person_id = people.id AND i.direction = 'sent'),
                recv_count = (SELECT COUNT(*) FROM interactions i
                              WHERE i.person_id = people.id
                                AND i.direction IN ('received', 'both'))
            WHERE EXISTS (SELECT 1 FROM interactions i WHERE i.person_id = people.id)
            """
        )
    changed = []
    for row in conn.execute(
        "SELECT id, email, name, sent_count, recv_count, is_person FROM people"
    ):
        person = row["sent_count"] > 0 or not (
            looks_automated(row["email"], row["name"]) or row["recv_count"] >= threshold
        )
        if person != bool(row["is_person"]):
            changed.append((int(person), row["id"]))
    if changed:
        with db.transaction(conn):
            conn.executemany(
                "UPDATE people SET is_person = ?, updated_at = datetime('now') WHERE id = ?",
                changed,
            )
    return len(changed)


def record_interaction(
    conn: sqlite3.Connection,
    person_id: int,
    *,
    kind: str,
    direction: str,
    source_id: str,
    occurred_at: str,
    subject: str | None = None,
    thread_id: str | None = None,
) -> bool:
    """Log one exchange. False if it was already logged."""
    if not person_id:
        return False
    with db.transaction(conn):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO interactions
                (person_id, kind, direction, subject, source_id, thread_id, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (person_id, kind, direction, subject, source_id, thread_id, occurred_at),
        )
    return cur.rowcount > 0


def set_note(conn: sqlite3.Connection, person_id: int, note: str, org: str | None = None) -> None:
    with db.transaction(conn):
        conn.execute(
            "UPDATE people SET note = ?, org = COALESCE(?, org),"
            " updated_at = datetime('now') WHERE id = ?",
            (note[:600], org, person_id),
        )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def get(conn: sqlite3.Connection, person_id: int) -> Person | None:
    row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    return Person.from_row(row) if row else None


def find(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[Person]:
    """Look someone up by name, address or organisation.

    Real people first, because a search for "chase" should find the person
    before it finds chase.com's receipts mailbox -- which is stored, and
    findable, and almost never what was meant.
    """
    like = f"%{(query or '').strip().lower()}%"
    rows = conn.execute(
        """
        SELECT * FROM people
         WHERE (lower(COALESCE(name,'')) LIKE ?
                OR lower(email) LIKE ?
                OR lower(COALESCE(org,'')) LIKE ?)
           AND {live}
         ORDER BY is_person DESC, (sent_count + recv_count) DESC
         LIMIT ?
        """.format(live=modules.live(conn, "people")),
        (like, like, like, limit),
    ).fetchall()
    return [Person.from_row(r) for r in rows]


# A word: letters and digits, so "john_doe" and "j.doe@contoso.example" split where a
# reader would split them, and "José" stays one word.
_WORD = re.compile(r"[^\W_]+")


def match_quality(person: Person, query: str) -> str:
    """How `find` came to return this person: "exact" when every word of the
    query is a whole word of their name, address or organisation -- a first
    name, a surname, "northwind" -- and "partial" when it is only inside one.

    `find` matches substrings, so "alex" finds "Wren, Alexandra". On 5
    October 2026 the doer took her, the ledger's only match, for the Alex
    of a prep call (session 252: "Alex appears to be Alexandra Wren at
    Contoso"); the brief went out on 7 October and the user answered "That's who
    Alex is, not Alexandra Wren". A partial match is a candidate, not an
    identity, and the caller is told which it has.
    """
    wanted = _WORD.findall((query or "").lower())
    if not wanted:
        return "partial"
    words = set(_WORD.findall(" ".join(
        str(bit or "").lower() for bit in (person.name, person.email, person.org))))
    return "exact" if all(w in words for w in wanted) else "partial"


def history(conn: sqlite3.Connection, person_id: int, limit: int = 8) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, direction, subject, occurred_at FROM interactions"
        f" WHERE person_id = ? AND {modules.live(conn, 'interactions')}"
        " ORDER BY occurred_at DESC LIMIT ?",
        (person_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def recent(conn: sqlite3.Connection, days: int = 14, limit: int = 40) -> list[Person]:
    """Real people seen lately -- the input the night's learning block reads."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM people WHERE is_person = 1 AND COALESCE(last_seen,'') >= ?"
        f" AND {modules.live(conn, 'people')}"
        " ORDER BY (sent_count * 3 + recv_count) DESC LIMIT ?",
        (since, limit),
    ).fetchall()
    return [Person.from_row(r) for r in rows]


def counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(is_person) AS real,"
        " (SELECT COUNT(*) FROM interactions) AS interactions"
        " FROM people"
    ).fetchone()
    return {
        "people": int(row["total"] or 0),
        "real": int(row["real"] or 0),
        "filtered": int(row["total"] or 0) - int(row["real"] or 0),
        "interactions": int(row["interactions"] or 0),
    }


# ---------------------------------------------------------------------------
# One person, several addresses
# ---------------------------------------------------------------------------

# Two rows are the same person only on a strong signal. The cheap ones --
# same surname, same domain -- would merge two classmates called Chen, and a
# wrong merge is worse than a duplicate: it fuses two people's history into
# one dossier and there is nothing in the result that looks wrong.
MIN_NAME_PARTS = 2

_PUNCT = re.compile(r"[^\w\s]")

# Exchange hangs a department off the display name -- "Lowe, Petra (Contoso
# Registrar)". Dropped before comparing, or the same person written by two
# clients never matches. The trade this accepts: two *different* people with
# the same full name in different departments would now collide. Rarer, in
# one person's mailbox, than one person written two ways -- and the merge
# leaves an alias row saying what it did.
_QUALIFIER = re.compile(r"\([^)]*\)")


def name_key(name: str | None) -> str:
    """A name reduced to something comparable across header styles.

    Exchange writes "Quinn, Nora" and Gmail writes "Nora Quinn"
    for the same person -- which is how one professor became two rows. Sorted
    tokens make the two forms identical without having to know which
    convention produced which.
    """
    cleaned = _PUNCT.sub(" ", _QUALIFIER.sub(" ", (name or "").lower()))
    parts = sorted(p for p in cleaned.split() if len(p) > 1)
    return " ".join(parts)


def merge_candidates(conn: sqlite3.Connection) -> list[list[Person]]:
    """Groups of rows that look like one person under two addresses."""
    groups: dict[str, list[Person]] = {}
    rows = conn.execute("SELECT * FROM people WHERE is_person = 1"
                        f" AND {modules.live(conn, 'people')}").fetchall()
    for row in rows:
        person = Person.from_row(row)
        key = name_key(person.name)
        if len(key.split()) < MIN_NAME_PARTS:
            # A one-word name is not evidence. "Maya", "Support", "Team".
            continue
        groups.setdefault(key, []).append(person)
    return [sorted(g, key=lambda p: -(p.sent_count + p.recv_count))
            for g in groups.values() if len(g) > 1]


def merge(conn: sqlite3.Connection, keep_id: int, absorb_id: int) -> bool:
    """Fold one row into another, keeping the address as an alias.

    The absorbed address is recorded rather than discarded, so a later
    message to it finds the person it belongs to instead of recreating the
    duplicate this just removed.
    """
    if keep_id == absorb_id:
        return False
    keep = get(conn, keep_id)
    absorb = get(conn, absorb_id)
    if keep is None or absorb is None:
        return False

    with db.transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO person_aliases (person_id, email) VALUES (?, ?)",
            (keep_id, absorb.email),
        )
        # Interactions move across. `OR IGNORE` because the same message can
        # legitimately already be recorded against the surviving row.
        conn.execute(
            "UPDATE OR IGNORE interactions SET person_id = ? WHERE person_id = ?",
            (keep_id, absorb_id),
        )
        # What is owed them (`tools/obligations.py`).
        conn.execute(
            "UPDATE obligations SET person_id = ? WHERE person_id = ?",
            (keep_id, absorb_id),
        )
        conn.execute(
            "UPDATE people SET sent_count = sent_count + ?, recv_count = recv_count + ?,"
            " name = COALESCE(name, ?), org = COALESCE(org, ?), note = COALESCE(note, ?),"
            " first_seen = MIN(COALESCE(first_seen, ?), ?),"
            " last_seen = MAX(COALESCE(last_seen, ''), ?),"
            " updated_at = datetime('now')"
            " WHERE id = ?",
            (absorb.sent_count, absorb.recv_count, absorb.name, absorb.org,
             absorb.note, absorb.first_seen or "", absorb.first_seen or "",
             absorb.last_seen or "", keep_id),
        )
        conn.execute("DELETE FROM people WHERE id = ?", (absorb_id,))
    return True


def resolve_alias(conn: sqlite3.Connection, email: str) -> int | None:
    """The person an address belongs to, following a merge."""
    row = conn.execute(
        "SELECT person_id FROM person_aliases WHERE email = ?",
        ((email or "").strip().lower(),),
    ).fetchone()
    return int(row["person_id"]) if row else None


def merge_duplicates(conn: sqlite3.Connection) -> int:
    """Fold every confident duplicate into one row. Returns how many.

    Confident means an exact match on the sorted-token name with at least
    two parts. Anything weaker is left alone -- a duplicate is a visible
    annoyance the user can point at, and a bad merge is an invisible one the user
    cannot.
    """
    merged = 0
    for group in merge_candidates(conn):
        keep, *rest = group
        for other in rest:
            if merge(conn, keep.id, other.id):
                merged += 1
    return merged


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------


def cursor(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute(
        "SELECT cursor FROM sync_cursors WHERE source = ?", (source,)
    ).fetchone()
    return row["cursor"] if row else None


def set_cursor(conn: sqlite3.Connection, source: str, value: str, note: str = "") -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO sync_cursors (source, cursor, ran_at, note)"
            " VALUES (?, ?, datetime('now'), ?)"
            " ON CONFLICT(source) DO UPDATE SET"
            " cursor = excluded.cursor, ran_at = excluded.ran_at, note = excluded.note",
            (source, value, note),
        )
