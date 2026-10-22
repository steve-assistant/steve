"""A small IMAP server on loopback, over TLS, for the IMAP engine's tests.

The password mailbox's tests faked `imaplib` itself, one method at a time, so they
tested the code against what its author believed a server says. This is a
server: the engine speaks to it through imapclient, over a socket, with TLS
from a certificate authority `trustme` makes for the test, as it speaks to
a real one. It is our own, not GreenMail (05-connectors.md, section 8):
small enough to read, and able to misbehave on purpose.

Three modes:
- `standard`: special-use folders, UIDPLUS, MOVE and CONDSTORE (each can be
  taken away, to drive the engine's fallbacks);
- `gmail`: one message in several folders at once (All Mail and its
  labels), with `X-GM-RAW`, `X-GM-LABELS` and `X-GM-THRID`, and SMTP filing
  its own copy in Sent;
- `ignores_filters`: every search answers every message in the folder, as
  Google's CalDAV ignored a UID filter on 11 October 2026.

It records every command, so a test can say a bare EXPUNGE was never sent.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import email
import email.policy
import email.utils
import itertools
import re
import socket
import ssl
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

from steve_sdk.testing import World

STANDARD, GMAIL, IGNORES_FILTERS = "standard", "gmail", "ignores_filters"
MODES = (STANDARD, GMAIL, IGNORES_FILTERS)
CAPS = frozenset({"IMAP4rev1", "UIDPLUS", "MOVE", "CONDSTORE", "SPECIAL-USE"})
REFUSED = "[AUTHENTICATIONFAILED] Invalid credentials (Failure)"

_STANDARD_BOXES = (("INBOX", ""), ("Sent", "\\Sent"), ("Drafts", "\\Drafts"),
                   ("Trash", "\\Trash"), ("Junk", "\\Junk"), ("Archive", "\\Archive"))
_GMAIL_BOXES = (("INBOX", ""), ("[Gmail]/All Mail", "\\All"), ("[Gmail]/Sent Mail", "\\Sent"),
                ("[Gmail]/Drafts", "\\Drafts"), ("[Gmail]/Trash", "\\Trash"),
                ("[Gmail]/Spam", "\\Junk"))
# A Gmail folder that is a label's view, and the label it shows.
_LABEL_OF = {"INBOX": "\\Inbox", "[Gmail]/Sent Mail": "\\Sent", "[Gmail]/Drafts": "\\Draft"}
# The roles a contract suite seeds by, whatever the folders are called.
_ROLES = {"sent": "\\Sent", "drafts": "\\Drafts", "trash": "\\Trash", "junk": "\\Junk"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class Stored:
    uid: int
    raw: bytes
    flags: set[str]
    internal: dt.datetime
    modseq: int
    gm: int = 0                       # the message, across its Gmail folders
    thrid: int = 0
    labels: set[str] = field(default_factory=set)

    @property
    def message(self) -> email.message.EmailMessage:
        return email.message_from_bytes(self.raw, policy=email.policy.default)

    @property
    def ident(self) -> str:
        return str(self.message.get("Message-ID") or "")


@dataclass
class Box:
    name: str
    special: str
    validity: int
    next_uid: int = 1
    items: list[Stored] = field(default_factory=list)

    def by_uid(self, uid: int) -> Stored | None:
        return next((s for s in self.items if s.uid == uid), None)

    def seq(self, stored: Stored) -> int:
        return self.items.index(stored) + 1


def build(*, sender: str = "Ada <ada@else.example>", to: str = "maya@mail.example",
          cc: str = "", subject: str = "Hello", body: str = "Hi there.",
          date: dt.datetime | None = None, message_id: str | None = None,
          headers: dict[str, str] | None = None) -> bytes:
    """A message as a server would hold it."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    when = date or dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC)
    message["Date"] = email.utils.format_datetime(when)
    message["Message-ID"] = message_id or email.utils.make_msgid(domain="else.example")
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body)
    return message.as_bytes(policy=email.policy.SMTP)


class ImapWorld(World):
    """The server's store, and the World a contract suite drives."""

    def __init__(self, mode: str = STANDARD, *, user: str = "maya@mail.example",
                 password: str = "secret-pw", caps: frozenset[str] | None = None) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode
        self.user = user
        self.password = password
        self.caps = set(caps if caps is not None else CAPS)
        if mode == GMAIL:
            self.caps.add("X-GM-EXT-1")
        self.refusal = REFUSED
        self.offers_starttls = True  # off: a plain port that never turns on TLS
        # How many of the next reads of a whole message the connection drops
        # partway through. A search's fetches, of a message's start, are
        # left alone: its own searching again, wider, would hide the drop.
        self.drop_reads = 0
        # Told of each write as it is made, for a record of what reached the
        # server (tests/unit/golden_world.py).
        self.on_write: Any = None
        self.lock = threading.RLock()
        self.commands: list[str] = []
        self.logins: list[str] = []
        self._modseq = itertools.count(100)
        self._gm = itertools.count(1)
        self._validity = itertools.count(7001)
        self.boxes: dict[str, Box] = {}
        for name, special in (_GMAIL_BOXES if mode == GMAIL else _STANDARD_BOXES):
            self.boxes[name] = Box(name, special, next(self._validity))

    # --- the store ------------------------------------------------------------

    def told(self, verb: str, **what: Any) -> None:
        if self.on_write is not None:
            self.on_write({"imap": verb, **what})

    def role(self, special: str) -> Box:
        return next(b for b in self.boxes.values() if b.special == special)

    def _put(self, box: Box, raw: bytes, flags: set[str], *, gm: int = 0, thrid: int = 0,
             labels: set[str] | None = None, internal: dt.datetime | None = None) -> Stored:
        stored = Stored(uid=box.next_uid, raw=raw, flags=set(flags),
                        internal=internal or dt.datetime.now(dt.UTC).replace(microsecond=0),
                        modseq=next(self._modseq), gm=gm, thrid=thrid,
                        labels=set(labels or ()))
        box.next_uid += 1
        box.items.append(stored)
        return stored

    def add(self, folder: str, raw: bytes, flags: set[str] | None = None, *,
            internal: dt.datetime | None = None, thrid: int = 0) -> Stored:
        """Put a message in `folder`; in Gmail's mode, in All Mail too,
        carrying the folder's label. The copy in `folder`."""
        with self.lock:
            box = self.boxes[folder]
            flags = set(flags or ())
            if self.mode != GMAIL or box.special in ("\\All", "\\Trash", "\\Junk"):
                return self._put(box, raw, flags, internal=internal)
            gm = next(self._gm)
            thrid = thrid or gm
            labels = {_LABEL_OF.get(folder, folder)}
            here = self._put(box, raw, flags, gm=gm, thrid=thrid, labels=labels,
                             internal=internal)
            self._put(self.role("\\All"), raw, flags, gm=gm, thrid=thrid, labels=labels,
                      internal=internal)
            return here

    def copies(self, stored: Stored) -> list[tuple[Box, Stored]]:
        """Every copy of one message: in Gmail's mode, each folder it is in."""
        if not stored.gm:
            return [(b, s) for b in self.boxes.values() for s in b.items if s is stored]
        return [(b, s) for b in self.boxes.values() for s in b.items if s.gm == stored.gm]

    def find(self, native: str) -> tuple[Box, Stored]:
        from core.engines import imap

        where = imap.Where.parse(native)
        assert where is not None, native
        box = self.boxes[where.folder]
        stored = box.by_uid(int(where.uid))
        assert stored is not None, native
        return box, stored

    def native(self, box: Box, stored: Stored) -> str:
        from core.engines import imap

        return imap.Where(box.name, str(box.validity), str(stored.uid), stored.ident).native()

    def bump(self, stored: Stored) -> None:
        for _, copy in self.copies(stored):
            copy.modseq = next(self._modseq)

    def set_flags(self, stored: Stored, flags: set[str]) -> None:
        for _, copy in self.copies(stored):
            copy.flags = set(flags)
        self.bump(stored)

    def expunge(self, box: Box, stored: Stored) -> None:
        """One message out of one folder: in Gmail's mode, out of a label's
        folder takes the label; out of Drafts, All Mail or Trash, every copy."""
        box.items.remove(stored)
        if self.mode != GMAIL or not stored.gm:
            return
        if box.name in _LABEL_OF and box.name != "[Gmail]/Drafts" or box.special == "":
            for _, copy in self.copies(stored):
                copy.labels.discard(_LABEL_OF.get(box.name, box.name))
            return
        for other, copy in self.copies(stored):
            other.items.remove(copy)

    def move(self, box: Box, stored: Stored, target: Box) -> Stored:
        if self.mode == GMAIL and stored.gm:
            if target.special in ("\\Trash", "\\Junk"):
                for other, copy in self.copies(stored):
                    other.items.remove(copy)
                return self._put(target, stored.raw, stored.flags)
            labels = set(stored.labels)
            if box.special != "\\All":
                box.items.remove(stored)
                labels.discard(_LABEL_OF.get(box.name, box.name))
            if target.special != "\\All":
                labels.add(_LABEL_OF.get(target.name, target.name))
            for _, copy in self.copies(stored):
                copy.labels = set(labels)
            if target.special == "\\All":
                return next(s for b, s in self.copies(stored) if b is target)
            return self._put(target, stored.raw, stored.flags, gm=stored.gm, thrid=stored.thrid,
                             labels=labels)
        box.items.remove(stored)
        return self._put(target, stored.raw, stored.flags, internal=stored.internal)

    def file_sent(self, raw: bytes) -> None:
        """What Gmail's SMTP does after a send: its own copy in Sent, unless
        the account already holds a message with that Message-ID. Gmail
        keeps one message per Message-ID: a draft sent with its own left no
        copy but the draft (the live smoke of 15 October 2026)."""
        ident = str(email.message_from_bytes(raw).get("Message-ID") or "").strip("<> ").lower()
        with self.lock:
            held = any(stored.ident.strip("<> ").lower() == ident
                       for box in self.boxes.values() if box.special not in ("\\Trash", "\\Junk")
                       for stored in box.items)
        if not held:
            self.add(self.role("\\Sent").name, raw, {"\\Seen"})

    # --- World ----------------------------------------------------------------

    def seed(self, klass: str, items: list[dict[str, Any]]) -> list[str]:
        """Messages in, as another client would put them. Each item may
        give role (inbox, sent, drafts) or folder, sender, to, cc,
        subject, body, date, message_id, headers, unread, flags, thrid. The
        ids are those the engine hands out:
        in Gmail's mode, of the copy in All Mail, except a draft's."""
        assert klass == "mail"
        out = []
        for item in items:
            role = item.get("role")
            folder = (self.role(_ROLES[role]).name if role and role != "inbox"
                      else item.get("folder", "INBOX"))
            flags = set(item.get("flags", ()))
            if not item.get("unread", False):
                flags.add("\\Seen")
            if self.boxes[folder].special == "\\Drafts":
                flags.add("\\Draft")
            raw = build(**{k: v for k, v in item.items()
                           if k in ("sender", "to", "cc", "subject", "body", "date",
                                    "message_id", "headers")})
            when = item.get("date")
            here = self.add(folder, raw, flags, internal=when, thrid=item.get("thrid", 0))
            box = self.boxes[folder]
            if self.mode == GMAIL and box.special != "\\Drafts" and here.gm:
                box, here = next((b, s) for b, s in self.copies(here) if b.special == "\\All")
            out.append(self.native(box, here))
        return out

    def mutate(self, klass: str, native_id: str, **changes: Any) -> None:
        """Change one message as another client would: `flags` (set) or
        `unread`; `body` or `subject` save it again under a new UID, as a
        draft edited in webmail is; `gone` expunges it; `rebuilt` gives its
        folder a new UIDVALIDITY."""
        with self.lock:
            box, stored = self.find(native_id)
            if "flags" in changes:
                self.set_flags(stored, set(changes["flags"]))
            if "unread" in changes:
                flags = set(stored.flags)
                (flags.add if not changes["unread"] else flags.discard)("\\Seen")
                self.set_flags(stored, flags)
            if "body" in changes or "subject" in changes:
                message = stored.message
                if "subject" in changes:
                    del message["Subject"]
                    message["Subject"] = changes["subject"]
                if "body" in changes:
                    message.set_content(changes["body"])
                self.expunge(box, stored)
                self.add(box.name, message.as_bytes(policy=email.policy.SMTP), stored.flags)
            if changes.get("gone"):
                self.expunge(box, stored)
            if changes.get("rebuilt"):
                box.validity = next(self._validity)

    def snapshot(self, klass: str) -> dict[str, Any]:
        """Each message by its Message-ID: every copy's folder and flags
        (and labels), so a second copy in Sent is a difference."""
        out: dict[str, list] = {}
        with self.lock:
            for box in self.boxes.values():
                for stored in box.items:
                    out.setdefault(stored.ident.strip("<>").lower(), []).append(
                        (box.name, tuple(sorted(stored.flags)), tuple(sorted(stored.labels))))
        return {key: sorted(value) for key, value in out.items()}

    def in_folder(self, special_or_name: str) -> list[email.message.EmailMessage]:
        box = (self.boxes.get(special_or_name)
               or next(b for b in self.boxes.values() if b.special == special_or_name))
        return [s.message for s in box.items]

    def bare_expunges(self) -> int:
        return sum(1 for c in self.commands if re.match(r"\S+ EXPUNGE\b", c, re.I))


# --- the wire ----------------------------------------------------------------

def _tokens(data: bytes, literals: list[bytes]) -> list:
    """A command's arguments: atoms (str), quoted strings (str), literals
    (bytes, in order), and parenthesised lists (list). A `[...]` belongs to
    its atom, spaces and all, as in BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]."""
    stack: list[list] = [[]]
    i, n = 0, len(data)
    while i < n:
        c = data[i:i + 1]
        if c == b" ":
            i += 1
        elif c == b"(":
            stack.append([])
            i += 1
        elif c == b")":
            done = stack.pop()
            stack[-1].append(done)
            i += 1
        elif c == b'"':
            j, out = i + 1, bytearray()
            while data[j:j + 1] != b'"':
                if data[j:j + 1] == b"\\":
                    j += 1
                out += data[j:j + 1]
                j += 1
            stack[-1].append(out.decode("utf-8"))
            i = j + 1
        elif c == b"\x00":
            stack[-1].append(literals.pop(0))
            i += 1
        else:
            j, depth = i, 0
            while j < n:
                ch = data[j:j + 1]
                if ch == b"[":
                    depth += 1
                elif ch == b"]":
                    depth -= 1
                elif depth == 0 and ch in (b" ", b"(", b")"):
                    break
                j += 1
            stack[-1].append(data[i:j].decode("utf-8"))
            i = j
    return stack[0]


def _uids(spec: str, box: Box) -> list[Stored]:
    top = max((s.uid for s in box.items), default=0)
    wanted: set[int] = set()
    for part in spec.split(","):
        low, _, high = part.partition(":")
        a = top if low == "*" else int(low)
        b = a if not high else (top if high == "*" else int(high))
        wanted.update(range(min(a, b), max(a, b) + 1))
    return [s for s in box.items if s.uid in wanted]


def _date(text: str) -> dt.date:
    day, month, year = text.split("-")
    return dt.date(int(year), _MONTHS.index(month.title()) + 1, int(day))


def _internal(when: dt.datetime) -> str:
    return f"{when.day:02d}-{_MONTHS[when.month - 1]}-{when.year} {when:%H:%M:%S} +0000"


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _plist(items) -> str:
    return "(" + " ".join(items) + ")"


def _gmail_raw(stored: Stored, query: str) -> bool:
    """A little of Gmail's search language, enough for the engine's queries."""
    message = stored.message
    sender = str(message.get("From") or "").lower()
    to = " ".join(str(message.get(h) or "") for h in ("To", "Cc")).lower()
    subject = str(message.get("Subject") or "").lower()
    body = (message.get_body(("plain",)) or message).get_content().lower() \
        if message.get_body(("plain",)) else ""
    for word in query.lower().split():
        head, sep, value = word.partition(":")
        value = value.strip('"')
        if sep and head == "from":
            ok = value in sender
        elif sep and head in ("to", "cc"):
            ok = value in to
        elif sep and head == "subject":
            ok = value in subject
        elif sep and head == "is" and value == "unread":
            ok = "\\Seen" not in stored.flags
        elif sep and head == "in" and value == "sent":
            ok = "\\Sent" in stored.labels
        elif sep and head == "in" and value == "inbox":
            ok = "\\Inbox" in stored.labels
        elif sep and head == "label":
            ok = value in {label.lower() for label in stored.labels}
        elif sep and head == "rfc822msgid":
            ok = value.strip("<>") in stored.ident.lower()
        elif sep:
            ok = True   # newer_than and the like: the engine checks dates itself
        else:
            ok = word in f"{sender} {to} {subject} {body}"
        if not ok:
            return False
    return True


class _Session:
    def __init__(self, world: ImapWorld, conn: socket.socket, *, tls: ssl.SSLContext,
                 starttls: bool) -> None:
        self.world = world
        self.conn = conn
        self.tls = tls
        self.plain = starttls
        self.file = conn.makefile("rb")
        self.authed = False
        self.box: Box | None = None
        self.readonly = True

    # --- io

    def send(self, text: str | bytes) -> None:
        data = text.encode("utf-8") if isinstance(text, str) else text
        self.conn.sendall(data)

    def line(self) -> tuple[bytes, list[bytes]] | None:
        """One command, with its literals read in and marked by NUL."""
        out, literals = bytearray(), []
        while True:
            raw = self.file.readline()
            if not raw:
                return None
            raw = raw.rstrip(b"\r\n")
            match = re.search(rb"\{(\d+)(\+?)\}$", raw)
            if not match:
                out += raw
                return bytes(out), literals
            out += raw[:match.start()] + b"\x00"
            if not match[2]:
                self.send("+ Ready\r\n")
            literals.append(self.file.read(int(match[1])))

    def caps(self) -> str:
        caps = set(self.world.caps)
        if self.plain:
            caps |= {"LOGINDISABLED"} | ({"STARTTLS"} if self.world.offers_starttls else set())
        return " ".join(sorted(caps))

    def run(self) -> None:
        try:
            self.send(f"* OK [CAPABILITY {self.caps()}] fake ready\r\n")
            while True:
                got = self.line()
                if got is None:
                    return
                data, literals = got
                text = data.replace(b"\x00", b"{literal}").decode("utf-8", "replace")
                with self.world.lock:
                    self.world.commands.append(text)
                parts = data.split(b" ", 2)
                if len(parts) < 2:
                    continue
                tag = parts[0].decode()
                verb = parts[1].decode().upper()
                args = _tokens(parts[2], literals) if len(parts) > 2 else []
                if verb == "UID":
                    verb, args = "UID " + str(args[0]).upper(), args[1:]
                try:
                    with self.world.lock:
                        done = self.handle(tag, verb, args)
                except Exception as exc:  # noqa: BLE001 - said to the client, as a server would
                    self.send(f"{tag} BAD {type(exc).__name__}: {exc}\r\n")
                    continue
                if done == "logout":
                    return
                if done == "starttls":
                    self.conn = self.tls.wrap_socket(self.conn, server_side=True)
                    self.file = self.conn.makefile("rb")
                    self.plain = False
        except (OSError, ssl.SSLError):
            return
        finally:
            with contextlib.suppress(OSError):
                self.conn.close()

    # --- commands

    def handle(self, tag: str, verb: str, args: list) -> str | None:
        w = self.world
        ok = f"{tag} OK {verb} completed\r\n"
        if verb == "CAPABILITY":
            self.send(f"* CAPABILITY {self.caps()}\r\n" + ok)
        elif verb == "NOOP":
            self.send(ok)
        elif verb == "LOGOUT":
            self.send("* BYE fake out\r\n" + ok)
            return "logout"
        elif verb == "STARTTLS":
            self.send(ok)
            return "starttls"
        elif verb == "LOGIN":
            if self.plain:
                self.send(f"{tag} NO [PRIVACYREQUIRED] TLS first\r\n")
                return None
            user, password = (a.decode() if isinstance(a, bytes) else a for a in args[:2])
            w.logins.append(user)
            if user != w.user or password != w.password:
                self.send(f"{tag} NO {w.refusal}\r\n")
                return None
            self.authed = True
            self.send(f"{tag} OK [CAPABILITY {self.caps()}] signed in\r\n")
        elif not self.authed:
            self.send(f"{tag} NO sign in first\r\n")
        elif verb in ("ENABLE", "ID"):
            self.send(ok)
        elif verb == "LIST":
            out = []
            for box in w.boxes.values():
                flags = ["\\HasNoChildren"] + ([box.special] if box.special else [])
                out.append(f'* LIST {_plist(flags)} "/" {_quote(box.name)}\r\n')
            self.send("".join(out) + ok)
        elif verb in ("SELECT", "EXAMINE"):
            box = w.boxes.get(str(args[0]))
            if box is None:
                self.send(f"{tag} NO no such folder\r\n")
                return None
            self.box, self.readonly = box, verb == "EXAMINE"
            lines = [f"* {len(box.items)} EXISTS\r\n", "* 0 RECENT\r\n",
                     "* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)\r\n",
                     f"* OK [UIDVALIDITY {box.validity}] UIDs valid\r\n",
                     f"* OK [UIDNEXT {box.next_uid}] next\r\n"]
            if "CONDSTORE" in w.caps:
                top = max((s.modseq for s in box.items), default=1)
                lines.append(f"* OK [HIGHESTMODSEQ {top}] modseq\r\n")
            mode = "READ-ONLY" if self.readonly else "READ-WRITE"
            self.send("".join(lines) + f"{tag} OK [{mode}] {verb} completed\r\n")
        elif verb in ("CLOSE", "UNSELECT"):
            self.box = None
            self.send(ok)
        elif verb == "APPEND":
            self.append(tag, args)
        elif self.box is None:
            self.send(f"{tag} NO select a folder first\r\n")
        elif verb == "UID SEARCH":
            self.search(tag, args)
        elif verb == "UID FETCH":
            self.fetch(tag, args)
        elif verb == "UID STORE":
            self.store(tag, args)
        elif verb in ("UID MOVE", "UID COPY"):
            self.move(tag, verb, args)
        elif verb == "UID EXPUNGE":
            if "UIDPLUS" not in w.caps:
                self.send(f"{tag} BAD UID EXPUNGE needs UIDPLUS\r\n")
                return None
            gone = [s for s in _uids(str(args[0]), self.box) if "\\Deleted" in s.flags]
            self.expunge(tag, gone)
        elif verb == "EXPUNGE":
            self.expunge(tag, [s for s in self.box.items if "\\Deleted" in s.flags])
        else:
            self.send(f"{tag} BAD unknown command {verb}\r\n")
        return None

    def append(self, tag: str, args: list) -> None:
        w = self.world
        box = w.boxes.get(str(args[0]))
        if box is None:
            self.send(f"{tag} NO [TRYCREATE] no such folder\r\n")
            return
        flags: set[str] = set()
        raw = args[-1]
        for arg in args[1:-1]:
            if isinstance(arg, list):
                flags = {str(f) for f in arg}
        stored = w.add(box.name, raw, flags)
        w.told("APPEND", folder=box.name, flags=sorted(flags),
               subject=str(stored.message["Subject"] or ""), to=str(stored.message["To"] or ""))
        said =(f"[APPENDUID {box.validity} {stored.uid}] " if "UIDPLUS" in w.caps else "")
        self.send(f"{tag} OK {said}APPEND completed\r\n")

    def search(self, tag: str, args: list) -> None:
        if args and str(args[0]).upper() == "CHARSET":
            args = args[2:]
        args = [a.decode("utf-8") if isinstance(a, bytes) else a for a in args]
        box = self.box
        if self.world.mode == IGNORES_FILTERS:
            found = list(box.items)
        else:
            found = [s for s in box.items if self._all(s, list(args))]
        self.send("* SEARCH " + " ".join(str(s.uid) for s in found) + "\r\n"
                  + f"{tag} OK SEARCH completed\r\n")

    def _all(self, stored: Stored, keys: list) -> bool:
        while keys:
            if not self._one(stored, keys):
                return False
        return True

    def _one(self, stored: Stored, keys: list) -> bool:
        key = keys.pop(0)
        if isinstance(key, list):
            return self._all(stored, list(key))
        word = str(key).upper()
        message = stored.message

        def header(name: str) -> str:
            return " ".join(str(v) for v in (message.get_all(name) or [])).lower()

        if word == "ALL":
            return True
        if word in ("SEEN", "UNSEEN", "DELETED", "UNDELETED", "DRAFT", "UNDRAFT",
                    "FLAGGED", "UNFLAGGED"):
            flag = "\\" + word.removeprefix("UN").title()
            return (flag in stored.flags) != word.startswith("UN")
        if word in ("SINCE", "BEFORE", "ON"):
            day = _date(str(keys.pop(0)))
            mine = stored.internal.date()
            return {"SINCE": mine >= day, "BEFORE": mine < day, "ON": mine == day}[word]
        if word in ("FROM", "TO", "CC", "BCC", "SUBJECT"):
            return str(keys.pop(0)).lower() in header(word.title() if word != "BCC" else "Bcc")
        if word in ("BODY", "TEXT"):
            needle = str(keys.pop(0)).lower()
            return needle in stored.raw.decode("utf-8", "replace").lower()
        if word == "HEADER":
            name, needle = str(keys.pop(0)), str(keys.pop(0)).lower()
            return needle in header(name) if needle else bool(message.get(name))
        if word == "OR":
            # Each side is a whole key with its arguments; both are read off
            # the list, whatever the first one says.
            a = self._one(stored, keys)
            b = self._one(stored, keys)
            return a or b
        if word == "NOT":
            return not self._one(stored, keys)
        if word == "UID":
            return stored in _uids(str(keys.pop(0)), self.box)
        if word == "X-GM-RAW" and self.world.mode == GMAIL:
            return _gmail_raw(stored, str(keys.pop(0)))
        if word == "X-GM-THRID" and self.world.mode == GMAIL:
            return stored.thrid == int(keys.pop(0))
        raise ValueError(f"search key {word} not known to the fake")

    def fetch(self, tag: str, args: list) -> None:
        box = self.box
        wanted = args[1] if isinstance(args[1], list) else [args[1]]
        since = None
        modifier = args[2] if len(args) > 2 and isinstance(args[2], list) else None
        if modifier and str(modifier[0]).upper() == "CHANGEDSINCE":
            since = int(modifier[1])
        out = bytearray()
        for stored in _uids(str(args[0]), box):
            if since is not None and stored.modseq <= since:
                continue
            parts: list[bytes] = [f"UID {stored.uid}".encode()]
            for item in wanted:
                parts.append(self._item(stored, str(item)))
            if since is not None and not any(str(i).upper() == "MODSEQ" for i in wanted):
                parts.append(f"MODSEQ ({stored.modseq})".encode())
            out += f"* {box.seq(stored)} FETCH (".encode() + b" ".join(parts) + b")\r\n"
            if any(str(i).upper().startswith("BODY[") for i in wanted) and not self.readonly:
                stored.flags.add("\\Seen")
        whole = any(str(i).upper() in ("BODY[]", "BODY.PEEK[]", "RFC822") for i in wanted)
        if whole and self.world.drop_reads > 0:
            # Half the answer, then the connection goes, as a server's does
            # when it is restarted or the network drops.
            self.world.drop_reads -= 1
            self.send(bytes(out[:len(out) // 2]))
            self.conn.shutdown(socket.SHUT_RDWR)
            return
        self.send(bytes(out) + f"{tag} OK FETCH completed\r\n".encode())

    def _item(self, stored: Stored, item: str) -> bytes:
        upper = item.upper()
        if upper == "UID":
            return f"UID {stored.uid}".encode()
        if upper == "FLAGS":
            return f"FLAGS {_plist(sorted(stored.flags))}".encode()
        if upper == "INTERNALDATE":
            return f'INTERNALDATE "{_internal(stored.internal)}"'.encode()
        if upper == "RFC822.SIZE":
            return f"RFC822.SIZE {len(stored.raw)}".encode()
        if upper == "MODSEQ":
            return f"MODSEQ ({stored.modseq})".encode()
        if upper == "X-GM-THRID":
            return f"X-GM-THRID {stored.thrid}".encode()
        if upper == "X-GM-MSGID":
            return f"X-GM-MSGID {stored.gm}".encode()
        if upper == "X-GM-LABELS":
            return f"X-GM-LABELS {_plist(_quote(x) for x in sorted(stored.labels))}".encode()
        match = re.fullmatch(r"BODY(?:\.PEEK)?\[(.*?)\](?:<(\d+)\.(\d+)>)?", item, re.I | re.S)
        if not match:
            raise ValueError(f"fetch item {item} not known to the fake")
        section, start, count = match[1], match[2], match[3]
        head, _, rest = stored.raw.partition(b"\r\n\r\n")
        if section == "":
            data = stored.raw
        elif section.upper() == "HEADER":
            data = head + b"\r\n\r\n"
        elif section.upper() == "TEXT":
            data = rest
        elif section.upper().startswith("HEADER.FIELDS"):
            names = {n.lower() for n in re.findall(r"[\w-]+", section[len("HEADER.FIELDS"):])}
            lines = re.split(rb"\r\n(?![ \t])", head)
            data = b"".join(line + b"\r\n" for line in lines
                            if line.split(b":", 1)[0].decode().lower() in names) + b"\r\n"
        else:
            raise ValueError(f"section {section} not known to the fake")
        key = f"BODY[{section}]"
        if start is not None:
            data = data[int(start):int(start) + int(count)]
            key += f"<{start}>"
        return key.encode() + f" {{{len(data)}}}\r\n".encode() + data

    def store(self, tag: str, args: list) -> None:
        w, box = self.world, self.box
        if self.readonly:
            self.send(f"{tag} NO read-only folder\r\n")
            return
        rest = args[1:]
        unchanged = None
        if isinstance(rest[0], list):
            unchanged = int(rest[0][1])
            rest = rest[1:]
        op, values = str(rest[0]).upper(), rest[1] if isinstance(rest[1], list) else [rest[1]]
        values = [v.decode() if isinstance(v, bytes) else str(v) for v in values]
        silent = op.endswith(".SILENT")
        op = op.removesuffix(".SILENT")
        modified, out = [], []
        for stored in _uids(str(args[0]), box):
            if unchanged is not None and stored.modseq > unchanged:
                modified.append(stored.uid)
                continue
            if op.endswith("X-GM-LABELS"):
                labels = set(stored.labels)
                if op.startswith("+"):
                    labels |= set(values)
                elif op.startswith("-"):
                    labels -= set(values)
                else:
                    labels = set(values)
                for _, copy in w.copies(stored):
                    copy.labels = set(labels)
                w.bump(stored)
                if "\\Inbox" not in labels and (inbox := w.boxes.get("INBOX")):
                    for copy in [s for s in inbox.items if s.gm and s.gm == stored.gm]:
                        inbox.items.remove(copy)
            else:
                flags = set(stored.flags)
                if op.startswith("+"):
                    flags |= set(values)
                elif op.startswith("-"):
                    flags -= set(values)
                else:
                    flags = set(values)
                w.set_flags(stored, flags)
            if not silent:
                out.append(f"* {box.seq(stored)} FETCH (UID {stored.uid} "
                           f"FLAGS {_plist(sorted(stored.flags))} MODSEQ ({stored.modseq}))\r\n")
        w.told("STORE", folder=box.name, uids=str(args[0]), change=f"{op} {' '.join(values)}",
               unchanged_since=unchanged, refused=modified)
        said = f"[MODIFIED {','.join(map(str, modified))}] " if modified else ""
        self.send("".join(out) + f"{tag} OK {said}STORE completed\r\n")

    def move(self, tag: str, verb: str, args: list) -> None:
        w, box = self.world, self.box
        if verb == "UID MOVE" and "MOVE" not in w.caps:
            self.send(f"{tag} BAD MOVE is not supported\r\n")
            return
        target = w.boxes.get(str(args[1]))
        if target is None:
            self.send(f"{tag} NO [TRYCREATE] no such folder\r\n")
            return
        moving = _uids(str(args[0]), box)
        old, new, gone = [], [], []
        for stored in moving:
            seq = box.seq(stored)
            if verb == "UID COPY":
                made = w._put(target, stored.raw, stored.flags)
            else:
                made = w.move(box, stored, target)
                gone.append(seq)
            old.append(str(stored.uid))
            new.append(str(made.uid))
        w.told(verb.removeprefix("UID "), folder=box.name, uids=old, to=target.name)
        lines = [f"* OK [COPYUID {target.validity} {','.join(old)} {','.join(new)}] moved\r\n"]
        lines += [f"* {seq} EXPUNGE\r\n" for seq in sorted(gone, reverse=True)]
        self.send("".join(lines) + f"{tag} OK {verb} completed\r\n")

    def expunge(self, tag: str, gone: list[Stored]) -> None:
        lines = []
        for stored in gone:
            lines.append(f"* {self.box.seq(stored)} EXPUNGE\r\n")
            self.world.expunge(self.box, stored)
        self.world.told("EXPUNGE", folder=self.box.name, uids=[str(s.uid) for s in gone])
        self.send("".join(lines) + f"{tag} OK EXPUNGE completed\r\n")


class ImapServer:
    """The world served on 127.0.0.1, one thread per connection. With
    `starttls`, the port is plain until the client asks for TLS."""

    def __init__(self, world: ImapWorld, tls: ssl.SSLContext, *, starttls: bool = False) -> None:
        self.world = world
        self.tls = tls
        self.starttls = starttls
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.connections = 0
        self._open: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            self._open.append(conn)
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        if not self.starttls:
            try:
                conn = self.tls.wrap_socket(conn, server_side=True)
            except (OSError, ssl.SSLError):
                conn.close()
                return
        _Session(self.world, conn, tls=self.tls, starttls=self.starttls).run()

    def close(self) -> None:
        self.listener.close()
        for conn in self._open:
            with contextlib.suppress(OSError):
                conn.close()
