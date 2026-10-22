"""Mail by password: core's IMAP and SMTP engine, for every module built on it.

The one mailbox Steve reached by password had a client of its own:
raw imaplib, LIST lines parsed by a regular
expression, folders found by name, and a draft thrown away by marking it
and sending a bare EXPUNGE, which removes every message marked deleted in
the folder, not just the one meant. Another provider would have been
another copy of all that. Now a provider that speaks IMAP is a manifest
(its hosts, whether its SMTP wants STARTTLS, whether its server files its
own copy of what is sent), and this engine fills the mail class for each
(05-connectors.md, section 4):

- Every connection goes through `ctx.tcp`: to the manifest's hosts, or the
  ones the user typed, of the kind pinned at setup. TLS is never off:
  implicit, or STARTTLS where declared, verified against the system's
  authorities (`Network.tls`).
- Folders are found by their special-use flag, since names are localised,
  and by the usual names only on a server that flags none. Where there is
  an All Mail folder, search runs there alone, so a message is not counted
  once per label.
- An id carries the folder, UIDVALIDITY, UID and Message-ID. A message is
  fetched by its UID alone, its Message-ID checked here, and a rebuilt
  folder (a new UIDVALIDITY) refuses it: what the server answers is never
  taken as the target.
- A draft is thrown away by moving it to Trash where the server has MOVE,
  else by marking that one UID and expunging that one UID; never a bare
  EXPUNGE. A server with neither refuses, and says why.
- A send files its copy in Sent exactly once: the server's own (Gmail
  files one), Steve's, or, where it is not known, Steve's only if, after a
  short wait, Sent holds none with that Message-ID.

Each call opens its own session and closes it, so nothing is shared between
threads and every socket has this account's own timeout: a thread that has
timed out cannot be stopped, so it must not be left waiting forever.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import email
import email.policy
import email.utils
import functools
import html
import imaplib
import re
import smtplib
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import imapclient
from imapclient import exceptions as imap_errors

from core.modctx import Refused
from steve_sdk import classes, exact
from steve_sdk import query as query_syntax
from steve_sdk.classes import Draft, MailProvider, Message
from steve_sdk.errors import (
    ConnectorError,
    NotConnected,
    TargetChanged,
    Transient,
    Unsupported,
    WrongPassword,
)

TIMEOUT_S = 20.0
SEARCH_DAYS = 365
# Enough of a message for its text; an attachment past this is not read.
PEEK_BYTES = 65_536
SNIPPET_CHARS = 400
# Where the manifest does not know whether the server files its own copy
# of what is sent: how long to wait for one before filing Steve's.
SENT_WAIT_S = 4.0
# Messages each side of one written, read before and after the write to
# check that exactly that one changed (core/writes.py, step 6).
NEAR = 25

_ALL, _SENT, _DRAFTS, _TRASH, _JUNK, _ARCHIVE = (
    "\\all", "\\sent", "\\drafts", "\\trash", "\\junk", "\\archive")
# The usual names, for a server that flags no folder by its use. A real
# mailbox had two folders of sent mail, `INBOX.Sent` and `INBOX.Sent
# Messages` (an Apple Mail habit); knowing only one, its own sent notes
# came back as mail the user had received (27 September 2026).
_NAMES = {
    _SENT: ("sent", "sent items", "sent messages", "sent mail"),
    _DRAFTS: ("drafts",),
    _TRASH: ("trash", "deleted messages", "deleted items", "bin"),
    _JUNK: ("junk", "spam", "junk e-mail"),
    _ARCHIVE: ("archive",),
    _ALL: (),
}
_SKIPPED = (_DRAFTS, _TRASH, _JUNK)
# The headers a mailing system adds, kept when present for the attention
# loop's sender check (`agent/attention/vet.py`).
_LISTING = {"List-Id": "list_id", "List-Unsubscribe": "list_unsubscribe",
            "Precedence": "precedence", "Auto-Submitted": "auto_submitted"}
_XGM = "X-GM-EXT-1"
# The slot's word for a star, the label Gmail's API calls it, which the mail
# tools ask for. IMAP keeps a star as the \Flagged flag, and Gmail's IMAP
# shows that flag as its star; "STARRED" handed on as an X-GM-LABELS label
# made a user label of that name, starred nothing, and read back as done.
_STAR = "STARRED"


# --- ids -----------------------------------------------------------------------

@dataclass(frozen=True)
class Where:
    """Where one message is: its folder, the folder's UIDVALIDITY, its UID,
    and its Message-ID, checked whenever it is read again."""

    folder: str
    validity: str
    uid: str
    ident: str

    def native(self) -> str:
        # A newline cannot be in a folder name or a Message-ID; the id is
        # base64'd on its way out (steve_sdk/ids.py).
        return "\n".join((self.folder, self.validity, self.uid, self.ident))

    @classmethod
    def parse(cls, native: str) -> Where | None:
        parts = (native or "").split("\n")
        if len(parts) != 4 or not parts[1].isdigit() or not parts[2].isdigit():
            return None
        return cls(*parts)


# --- connections ---------------------------------------------------------------

class _Wire(imaplib.IMAP4):
    """imaplib over a socket core vetted, rather than one it opens itself."""

    def __init__(self, opener: Callable[[], socket.socket], host: str, port: int) -> None:
        self._opener = opener
        super().__init__(host, port)

    def _create_socket(self, timeout: float | None) -> socket.socket:
        return self._opener()


class _Client(imapclient.IMAPClient):
    def __init__(self, opener: Callable[[], socket.socket], host: str, port: int, *,
                 implicit: bool) -> None:
        self._opener = opener
        super().__init__(host, port, ssl=implicit)

    def _create_IMAP4(self) -> imaplib.IMAP4:  # noqa: N802 - imapclient's name
        return _Wire(self._opener, self.host, self.port)

    def labels(self, uids: list[int], change: bytes, labels: list[str]) -> None:
        """Labels added (`+X-GM-LABELS`) or taken off (`-X-GM-LABELS`), by
        the X-GM-EXT-1 extension, through imapclient's own quoting. Its
        public methods for this carry a provider's name, which core's code
        does not (tests/gate/test_no_provider_names.py)."""
        self._gm_label_store(change, uids, labels, silent=True)


class _Smtp(smtplib.SMTP):
    def __init__(self, opener: Callable[[], socket.socket], host: str, port: int,
                 timeout: float) -> None:
        self._opener = opener
        # A fixed greeting name: the default looks up this computer's own
        # name and hands it to every mail server.
        super().__init__(local_hostname="localhost", timeout=timeout)
        self._host = host   # what STARTTLS checks the certificate against
        self.connect(host, port)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return self._opener()


def _endpoint(text: str, default_port: int) -> tuple[str, int]:
    host, sep, port = (text or "").strip().lower().rpartition(":")
    if not sep or not port.isdigit():
        return (text or "").strip().lower(), default_port
    return host, int(port)


class _Dropped(Transient):
    """The IMAP connection went under a call: the server restarted, or the
    network blinked. A read is tried once more, on a new one (`_read`)."""


def _read(method: Callable[..., Any]) -> Callable[..., Any]:
    """A read, tried once more on a new connection when the first dropped.
    It changes nothing on the server, so a second try is safe; without it,
    one dropped connection told the user their mail could not be reached
    (step 10's hostile scenarios). A timeout is not tried again: it has
    already waited its time. A write never is: it may have gone."""
    @functools.wraps(method)
    def again(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except _Dropped:
            return method(self, *args, **kwargs)
    return again


@contextlib.contextmanager
def _calling() -> Iterator[None]:
    """A library's or a socket's failure, as one of the SDK's errors."""
    try:
        yield
    except ConnectorError:
        raise
    except Refused as exc:
        raise NotConnected(str(exc)) from exc
    except ssl.SSLCertVerificationError as exc:
        raise ConnectorError(f"the server's certificate is not trusted: "
                             f"{exc.verify_message or exc}"[:300]) from exc
    except smtplib.SMTPAuthenticationError as exc:
        raise WrongPassword(_said(exc.smtp_error)) from exc
    except (ConnectionError, imaplib.IMAP4.abort) as exc:
        raise _Dropped(f"{type(exc).__name__}: {exc}"[:300]) from exc
    except (TimeoutError, smtplib.SMTPServerDisconnected) as exc:
        raise Transient(f"{type(exc).__name__}: {exc}"[:300]) from exc
    # In the server's own words: Python's form of a refusal reached the user
    # as "(554, b'5.7.1 Message rejected as spam')" (step 10's hostile
    # scenarios).
    except smtplib.SMTPResponseException as exc:
        raise ConnectorError(f"the server refused that: {exc.smtp_code} "
                             f"{_said(exc.smtp_error)}"[:300]) from exc
    # Before OSError: smtplib's errors are OSErrors, and a refused
    # recipient is not worth trying again.
    except (imaplib.IMAP4.error, imap_errors.IMAPClientError, smtplib.SMTPException) as exc:
        raise ConnectorError(f"the server refused that: {exc}"[:300]) from exc
    except OSError as exc:
        raise Transient(f"{type(exc).__name__}: {exc}"[:300]) from exc


def _said(text: Any) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return " ".join(str(text or "").split())[:200]


# --- folders --------------------------------------------------------------------

def _folders(client: imapclient.IMAPClient) -> list[tuple[str, set[str]]]:
    """(name, lower-cased flags) for every folder that can be opened."""
    found = []
    for flags, _, name in client.list_folders():
        lowered = {(f.decode() if isinstance(f, bytes) else str(f)).lower() for f in flags}
        if "\\noselect" in lowered or "\\nonexistent" in lowered:
            continue
        found.append((str(name), lowered))
    return found


def _leaf(name: str) -> str:
    return re.split(r"[./]", name.lower())[-1]


def _role(folders: list[tuple[str, set[str]]], role: str) -> str | None:
    """The folder flagged for `role`, else, on a server that flags none
    for it, the one by its usual name."""
    for name, flags in folders:
        if role in flags:
            return name
    for name, _ in folders:
        if _leaf(name) in _NAMES[role]:
            return name
    return None


def _sent_folders(folders: list[tuple[str, set[str]]]) -> set[str]:
    return {name for name, flags in folders if _SENT in flags or _leaf(name) in _NAMES[_SENT]}


def _skipped(name: str, flags: set[str], folders: list[tuple[str, set[str]]]) -> bool:
    """Drafts, Trash and Junk, and anything filed under them: `INBOX.Trash.keep`
    was once searched because only the last part of a name was checked."""
    parts = re.split(r"[./]", name.lower())
    if flags & set(_SKIPPED) or any(p in _NAMES[r] for r in _SKIPPED for p in parts):
        return True
    return any(name.startswith(f"{other}{sep}") for other, other_flags in folders
               if other_flags & set(_SKIPPED) for sep in "./")


# --- messages -------------------------------------------------------------------

# Where an HTML body's lines were: its breaks and the ends of its blocks.
_BREAKS = re.compile(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|blockquote|pre|table)\s*>")


def _text(message: EmailMessage) -> str:
    """The readable text of a message, in its lines: its plain part, or its
    HTML flattened. Spaces within a line close up, and blank lines to one.
    It used to be joined into one line, and what reads a mail by its lines
    lost them: the agent's own mailbox tells the user's note from what they
    forwarded by the forward's own line (agent/mailbox.py `split_note`), so
    on every account this engine reads, a forward's text was taken as the
    user's note -- their instruction, never screened."""
    try:
        part = message.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        content = str(part.get_content())
        if part.get_content_subtype() == "html":
            content = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", content)
            content = _BREAKS.sub("\n", content)
            content = html.unescape(re.sub(r"<[^>]+>", " ", content))
        lines = (" ".join(line.split()) for line in content.splitlines())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    except Exception:  # noqa: BLE001 - a malformed part is not the message
        return ""


def _root(message: EmailMessage) -> str:
    """The conversation a message belongs to, where the server keeps no
    thread: the first Message-ID it refers to, or its own."""
    references = str(message.get("References") or "").split()
    return (references[0] if references else "") or str(message.get("In-Reply-To") or "") \
        or str(message.get("Message-ID") or "")


def _epoch_ms(message: EmailMessage, internal: Any) -> int:
    with contextlib.suppress(Exception):
        return int(email.utils.parsedate_to_datetime(str(message["Date"])).timestamp() * 1000)
    if isinstance(internal, dt.datetime):
        aware = internal if internal.tzinfo else internal.replace(tzinfo=dt.UTC)
        return int(aware.timestamp() * 1000)
    return 0


def _arrived_at(internal: Any, message: EmailMessage) -> dt.datetime | None:
    """When the server received it (INTERNALDATE), else its Date header:
    what a date range is checked against on our side."""
    if isinstance(internal, dt.datetime):
        return internal if internal.tzinfo else internal.replace(tzinfo=dt.UTC)
    with contextlib.suppress(Exception):
        return email.utils.parsedate_to_datetime(str(message["Date"]))
    return None


def _addresses(*values: str) -> list[str]:
    return [address for _, address in email.utils.getaddresses([v for v in values if v])
            if address]


def _body(data: dict) -> bytes:
    for key, value in data.items():
        if isinstance(key, bytes) and key.startswith(b"BODY[") and isinstance(value, bytes):
            return value
    return b""


def _str(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


# --- the provider ---------------------------------------------------------------

class ImapMail(MailProvider):
    """The mail class over IMAP and SMTP, for one account of one module."""

    def __init__(self, ctx: Any, spec: Any) -> None:
        self.ctx = ctx
        account = ctx.account
        settings = ctx.manifest.settings("mail", account.preset)
        fields = dict(account.fields or {})
        # A host the manifest fixes wins; one the user typed (the "Other"
        # preset) is used only where the manifest leaves it open.
        self.imap = _endpoint(settings.get("imap") or fields.get("imap", ""), 993)
        self.smtp = _endpoint(settings.get("smtp") or fields.get("smtp", ""), 465)
        self.starttls = bool(settings.get("starttls", fields.get("starttls", False)))
        self.sent_copy = settings.get("sent_copy") or "check"
        self.plus = bool(settings.get("plus_addresses", False))
        self.address = (account.address or "").strip()
        typed = str(fields.get("username") or "").strip()
        local = self.address.partition("@")[0]
        self.usernames = ([typed] if typed else
                          [local, self.address] if settings.get("username") == "local"
                          else [self.address])
        secrets = ctx.manifest.secrets
        self.secret_name = secrets[0] if len(secrets) == 1 else None
        self.hints = tuple(getattr(ctx.manifest.setup, "errors", ()))
        caps = {classes.SEND, classes.FLAGS, classes.MOVE, classes.ARCHIVE, classes.THREADS}
        if settings.get("labels"):
            caps.add(classes.LABELS)
        self.capabilities = frozenset(caps)
        self.timeout = TIMEOUT_S
        self._xgm: bool | None = None

    # --- sessions

    def _opener(self, host: str, port: int, implicit: bool) -> Callable[[], socket.socket]:
        def opener() -> socket.socket:
            sock = self.ctx.tcp(host, port, timeout=self.timeout)
            if implicit:
                sock = self.ctx.network.tls().wrap_socket(sock, server_hostname=host)
            return sock

        return opener

    def _password(self) -> str:
        if self.secret_name is None:
            raise NotConnected("its module names no one password for its mail")
        password = self.ctx.secret(self.secret_name)
        if not password:
            raise NotConnected("no password is kept for it")
        return password

    def _hint(self, said: str) -> str:
        """What the server said, in the module's words where it gave some
        (`setup.errors`: "Application-specific password required" is the
        account's main password given where an app password belongs)."""
        lowered = said.lower()
        for hint in self.hints:
            if hint.says.lower() in lowered:
                return hint.tell
        return said

    @contextlib.contextmanager
    def _session(self) -> Iterator[imapclient.IMAPClient]:
        host, port = self.imap
        if not host:
            raise NotConnected("it has no mail server")
        implicit = port != 143
        with _calling():
            client = _Client(self._opener(host, port, implicit), host, port, implicit=implicit)
            try:
                if not implicit:
                    # Port 143 is plain until STARTTLS; a server that does
                    # not offer it is refused, never used in the clear.
                    if not client.has_capability("STARTTLS"):
                        raise ConnectorError("the mail server does not offer STARTTLS")
                    client.starttls(self.ctx.network.tls())
                # Times as the server gave them, zone and all: imapclient's
                # default turns them into this computer's local time, naive,
                # and a range check then reads them hours out.
                client.normalise_times = False
                self._login(client)
                self._xgm = client.has_capability(_XGM)
                yield client
            finally:
                with contextlib.suppress(Exception):
                    client.logout()

    def _login(self, client: imapclient.IMAPClient) -> None:
        password = self._password()
        said = ""
        for username in self.usernames:
            try:
                client.login(username, password)
                return
            except imap_errors.LoginError as exc:
                said = _said(str(exc))
        if any(word in said.upper() for word in ("UNAVAILABLE", "TRY AGAIN", "LATER")):
            raise Transient(said)
        raise WrongPassword(self._hint(said))

    @contextlib.contextmanager
    def _smtp_session(self) -> Iterator[smtplib.SMTP]:
        host, port = self.smtp
        if not host:
            raise NotConnected("it has no server to send by")
        with _calling():
            server = _Smtp(self._opener(host, port, not self.starttls), host, port, self.timeout)
            try:
                if self.starttls:
                    server.ehlo()
                    if not server.has_extn("starttls"):
                        raise ConnectorError("the sending server does not offer STARTTLS")
                    server.starttls(context=self.ctx.network.tls())
                server.ehlo()
                password = self._password()
                last: smtplib.SMTPAuthenticationError | None = None
                for username in self.usernames:
                    try:
                        server.login(username, password)
                        last = None
                        break
                    except smtplib.SMTPAuthenticationError as exc:
                        last = exc
                if last is not None:
                    raise WrongPassword(self._hint(_said(last.smtp_error)))
                yield server
            finally:
                with contextlib.suppress(Exception):
                    server.quit()

    # --- reading

    def _select(self, client: imapclient.IMAPClient, folder: str, *,
                write: bool = False) -> str:
        info = client.select_folder(folder, readonly=not write)
        return str(info.get(b"UIDVALIDITY", ""))

    def _items(self, *, body: str, xgm: bool) -> list[str]:
        items = ["UID", "FLAGS", "INTERNALDATE", body]
        if xgm:
            items += ["X-GM-THRID", "X-GM-LABELS"]
        return items

    def _message(self, data: dict, *, folder: str, validity: str, uid: int,
                 sent: set[str], drafts: str | None, body_chars: int | None = None) -> Message:
        raw = _body(data)
        message = email.message_from_bytes(raw, policy=email.policy.default)
        flags = {_str(f).lower() for f in data.get(b"FLAGS", ())}
        # Starred is the flag alone: a user label that happens to be named
        # "STARRED" (as the engine once made one) is not a star.
        labels = tuple(x for x in (_str(x) for x in data.get(b"X-GM-LABELS", ())) if x != _STAR)
        if "\\flagged" in flags:
            labels += (_STAR,)
        thrid = data.get(b"X-GM-THRID")
        modseq = data.get(b"MODSEQ")
        ident = str(message.get("Message-ID") or "").strip()
        text = _text(message)
        out = folder in sent or "\\Sent" in labels
        made = Message(
            native_id=Where(folder, validity, str(uid), ident).native(), ident=ident,
            condition=f"{validity}:{modseq[0]}" if modseq else None,
            thread=str(thrid) if thrid else _root(message),
            sender=str(message.get("From") or ""), to=str(message.get("To") or ""),
            cc=str(message.get("Cc") or ""), bcc=str(message.get("Bcc") or ""),
            subject=str(message.get("Subject") or ""),
            when=_epoch_ms(message, data.get(b"INTERNALDATE")),
            snippet=text[:SNIPPET_CHARS], unread="\\seen" not in flags,
            labels=labels, folder=folder,
            attachments=tuple(p.get_filename() or "" for p in message.iter_attachments()),
            direction="out" if out else "in",
            draft="\\draft" in flags or (drafts is not None and folder == drafts),
            extra={key: str(message[h]) for h, key in _LISTING.items() if message.get(h)})
        if body_chars is not None:
            made.body = text[:body_chars]
            made.body_length = len(text)
        return made

    def _one(self, client: imapclient.IMAPClient, where: Where, *, body_chars: int = 20_000,
             folders: list[tuple[str, set[str]]] | None = None, write: bool = False
             ) -> Message | None:
        """The one message `where` names, its Message-ID checked; None if gone."""
        folders = folders if folders is not None else _folders(client)
        if where.folder not in {name for name, _ in folders}:
            return None
        if self._select(client, where.folder, write=write) != where.validity:
            raise TargetChanged("that folder was rebuilt on the server; search for the "
                                "message again")
        uid = int(where.uid)
        items = self._items(body="BODY.PEEK[]", xgm=bool(self._xgm))
        if client.has_capability("CONDSTORE"):
            items.append("MODSEQ")
        # imapclient keeps only the answer for the UID asked; a server that
        # answered for another message answered nothing for this one.
        data = client.fetch([uid], items).get(uid)
        if not data:
            return None
        found = self._message(data, folder=where.folder, validity=where.validity, uid=uid,
                              sent=_sent_folders(folders), drafts=_role(folders, _DRAFTS),
                              body_chars=body_chars)
        if exact.norm(found.ident) != exact.norm(where.ident):
            raise TargetChanged(f"the message there now is another one ({found.ident!r}, "
                                f"not {where.ident!r})")
        return found

    def _found(self, client: imapclient.IMAPClient, folder: str, validity: str,
               uids: list[int], *, folders: list[tuple[str, set[str]]], xgm: bool,
               body: str = f"BODY.PEEK[]<0.{PEEK_BYTES}>", body_chars: int | None = None
               ) -> list[tuple[Message, dt.datetime | None, str]]:
        if not uids:
            return []
        data = client.fetch(uids, self._items(body=body, xgm=xgm))
        sent, drafts = _sent_folders(folders), _role(folders, _DRAFTS)
        out = []
        for uid in uids:
            if uid not in data:
                continue
            found = self._message(data[uid], folder=folder, validity=validity, uid=uid,
                                  sent=sent, drafts=drafts, body_chars=body_chars)
            raw = email.message_from_bytes(_body(data[uid]), policy=email.policy.default)
            out.append((found, _arrived_at(data[uid].get(b"INTERNALDATE"), raw), _text(raw)))
        return out

    def _criteria(self, parsed: query_syntax.Query, since: dt.datetime) -> list:
        """IMAP SEARCH keys for a query, as far as IMAP can say it. A word
        that is not ASCII is left to the check after fetching: a SEARCH with
        a charset needs literals, and one mistake there returns nothing."""
        keys: list = ["UNDELETED", "SINCE", since.date()]
        if parsed.unread_only:
            keys.append("UNSEEN")
        for word in filter(str.isascii, parsed.sender):
            keys += ["FROM", word]
        for word in filter(str.isascii, parsed.recipient):
            keys += ["TO", word]
        for word in filter(str.isascii, parsed.subject):
            keys += ["SUBJECT", word]
        for word in filter(str.isascii, parsed.terms):
            keys += ["OR", "OR", "FROM", word, "SUBJECT", word, "TO", word]
        return keys

    @_read
    def search(self, query: str, *, limit: int, since: dt.datetime | None = None
               ) -> list[Message]:
        now = dt.datetime.now(dt.UTC)
        parsed = query_syntax.parse(query)
        since = since or now - dt.timedelta(days=SEARCH_DAYS)
        if parsed.days:
            since = max(since, now - dt.timedelta(days=parsed.days))
        found: list[tuple[Message, dt.datetime | None, str]] = []
        with self._session() as client:
            xgm = bool(self._xgm)
            folders = _folders(client)
            everything = _role(folders, _ALL)
            names = ([everything] if everything else
                     [name for name, flags in folders if not _skipped(name, flags, folders)])
            for name in names:
                validity = self._select(client, name)
                if xgm and query.strip():
                    charset = None if query.isascii() else "UTF-8"
                    uids = client.search(["X-GM-RAW", query, "SINCE", since.date()],
                                         charset=charset)
                else:
                    uids = client.search(self._criteria(parsed, since))
                found += self._found(client, name, validity, sorted(uids)[-limit:],
                                     folders=folders, xgm=xgm)
        # Checked here whatever the server answered: its dates, and the
        # query's clauses as far as they can be read.
        kept = [m for m, arrived, text in found
                if not m.draft and (arrived is None or arrived >= since)
                and query_syntax.matches(parsed, subject=m.subject, sender=m.sender,
                                         recipient=f"{m.to} {m.cc}", unread=m.unread,
                                         text=text)]
        kept.sort(key=lambda m: m.when, reverse=True)
        return kept[:limit]

    @_read
    def arrived(self, *, since: dt.datetime, limit: int,
                seen: Callable[[list[str]], set[str]]) -> list[Message]:
        """The inbox only, so the user's own sent mail is never handed to the
        judge as new. The ids come from the Message-ID header alone, a few
        bytes each, and only what `seen` does not know is fetched: the old
        poll fetched the start of every message of two days to find nothing
        new, ~150 MB a day (measured 6 October 2026)."""
        with self._session() as client:
            xgm = bool(self._xgm)
            folders = _folders(client)
            inbox = next((n for n, _ in folders if n.upper() == "INBOX"), "INBOX")
            validity = self._select(client, inbox)
            uids = sorted(client.search(["UNDELETED", "SINCE", since.date()]))[-limit:]
            heads = client.fetch(uids, ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"]) if uids else {}
            natives = {}
            for uid in uids:
                if uid in heads:
                    head = email.message_from_bytes(_body(heads[uid]), policy=email.policy.default)
                    ident = str(head.get("Message-ID") or "").strip()
                    natives[uid] = Where(inbox, validity, str(uid), ident).native()
            old = seen(list(natives.values())) if natives else set()
            fresh = [uid for uid in uids if uid in natives and natives[uid] not in old]
            found = self._found(client, inbox, validity, fresh, folders=folders, xgm=xgm)
        kept = [m for m, arrived, _ in found if arrived is None or arrived >= since]
        kept.sort(key=lambda m: m.when, reverse=True)
        return kept

    def ignored(self, query: str) -> list[str]:
        # A server with the syntax's own extension is handed the query whole.
        return [] if self._xgm else list(query_syntax.parse(query).ignored)

    @_read
    def fetch(self, native_id: str, *, body_chars: int = 20_000) -> Message | None:
        where = Where.parse(native_id)
        if where is None:
            return None
        with self._session() as client:
            return self._one(client, where, body_chars=body_chars)

    @_read
    def headers(self, native_id: str) -> dict[str, str]:
        where = Where.parse(native_id)
        if where is None:
            raise TargetChanged("that is not an id this account handed out")
        with self._session() as client:
            if self._select(client, where.folder) != where.validity:
                raise TargetChanged("that folder was rebuilt on the server")
            data = client.fetch([int(where.uid)], ["BODY.PEEK[HEADER]"]).get(int(where.uid))
        if not data:
            raise TargetChanged("that message is no longer there")
        head = email.message_from_bytes(_body(data), policy=email.policy.default)
        if exact.norm(str(head.get("Message-ID") or "")) != exact.norm(where.ident):
            raise TargetChanged("the message there now is another one")
        # The topmost of each name, which the receiving server wrote: a dict
        # built in order kept the last, and an Authentication-Results at the
        # bottom is one the sender may have written.
        out: dict[str, str] = {}
        for name, value in head.items():
            out.setdefault(name, str(value))
        return out

    @_read
    def thread(self, thread_id: str, *, limit: int) -> list[Message]:
        with self._session() as client:
            xgm = bool(self._xgm)
            folders = _folders(client)
            everything = _role(folders, _ALL)
            found: list[tuple[Message, Any, str]] = []
            if xgm and everything and thread_id.isdigit():
                validity = self._select(client, everything)
                uids = sorted(client.search(["X-GM-THRID", int(thread_id)]))[-limit:]
                found = self._found(client, everything, validity, uids, folders=folders,
                                    xgm=True)
            else:
                names = [n for n, f in folders if not _skipped(n, f, folders)]
                for name in names:
                    validity = self._select(client, name)
                    uids = client.search(["OR", "HEADER", "Message-ID", thread_id,
                                          "OR", "HEADER", "References", thread_id,
                                          "HEADER", "In-Reply-To", thread_id])
                    found += self._found(client, name, validity, sorted(uids)[-limit:],
                                         folders=folders, xgm=xgm)
        wanted = exact.norm(thread_id)
        kept = [m for m, _, _ in found
                if exact.norm(m.thread) == wanted or exact.norm(m.ident) == wanted]
        kept.sort(key=lambda m: m.when)
        return kept[-limit:]

    @_read
    def sent_items(self, *, since: dt.datetime, limit: int, to: str = "") -> list[Message]:
        """From the Sent folders alone, with bodies: the folder is the rule,
        the search only a filter, and `to` is checked on each message."""
        if to and not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+", to):
            raise ValueError(f"not one address: {to!r}")
        found: list[tuple[Message, Any, str]] = []
        with self._session() as client:
            xgm = bool(self._xgm)
            folders = _folders(client)
            for name in sorted(_sent_folders(folders)):
                validity = self._select(client, name)
                keys: list = ["UNDELETED", "SINCE", since.date()] + (["TO", to] if to else [])
                uids = sorted(client.search(keys))[-limit:]
                found += self._found(client, name, validity, uids, folders=folders,
                                     xgm=xgm, body_chars=20_000)
        kept = [m for m, arrived, _ in found
                if (arrived is None or arrived >= since)
                and (not to or to.lower() in {a.lower() for a in _addresses(m.to, m.cc, m.bcc)})]
        for m in kept:
            m.direction = "out"
        kept.sort(key=lambda m: m.when, reverse=True)
        return kept[:limit]

    @_read
    def envelopes(self, *, sent: bool, since: dt.datetime, limit: int) -> list[Message]:
        fields = "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)]"
        found: list[tuple[Message, Any, str]] = []
        with self._session() as client:
            xgm = bool(self._xgm)
            folders = _folders(client)
            names = (sorted(_sent_folders(folders)) if sent else
                     [next((n for n, _ in folders if n.upper() == "INBOX"), "INBOX")])
            for name in names:
                validity = self._select(client, name)
                uids = sorted(client.search(["UNDELETED", "SINCE", since.date()]))[-limit:]
                found += self._found(client, name, validity, uids, folders=folders,
                                     xgm=xgm, body=fields)
        kept = [m for m, arrived, _ in found if arrived is None or arrived >= since]
        kept.sort(key=lambda m: m.when, reverse=True)
        return kept[:limit]

    # --- the after-check

    def neighbours(self, item: Any) -> dict[str, str | None] | None:
        """The messages within NEAR UIDs of `item` in its folder, by
        Message-ID, with their flags: what the write path compares before
        and after a write, so a bare EXPUNGE taking others would show."""
        where = Where.parse(getattr(item, "native_id", ""))
        if where is None:
            return None
        uid = int(where.uid)
        out: dict[str, str | None] = {}
        with self._session() as client:
            if self._select(client, where.folder) != where.validity:
                return None
            # Each UID named: imapclient keeps only the UIDs it was asked
            # for, and cannot read a range as a list of them.
            window = list(range(max(1, uid - NEAR), uid + NEAR + 1))
            data = client.fetch(window, ["UID", "FLAGS", "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
        for other in sorted(data):
            head = email.message_from_bytes(_body(data[other]), policy=email.policy.default)
            ident = str(head.get("Message-ID") or "").strip() or f"uid {other}"
            key = ident if ident not in out else f"{ident}#{other}"
            out[key] = " ".join(sorted(_str(f).lower() for f in data[other].get(b"FLAGS", ())))
        return out

    # --- writing

    def make_draft(self, draft: Draft) -> Message:
        if draft.attachments:
            raise Unsupported("attachments on a draft in this account")
        sender = self._sender(draft.sender)
        with self._session() as client:
            folders = _folders(client)
            drafts = _role(folders, _DRAFTS)
            if drafts is None:
                raise Unsupported("drafts: this account has no Drafts folder")
            message = EmailMessage()
            message["From"] = sender
            to, subject = draft.to, draft.subject
            if draft.reply_to:
                where = Where.parse(draft.reply_to)
                original = self._one(client, where, folders=folders) if where else None
                if original is None:
                    raise TargetChanged("the message being answered is no longer there")
                if not to:
                    to = original.sender
                if not subject:
                    subject = original.subject if original.subject.lower().startswith("re:") \
                        else f"Re: {original.subject}".strip()
                if original.ident:
                    message["In-Reply-To"] = original.ident
                    raw = self._raw(client, where)
                    refs = str(raw.get("References") or "").split()
                    message["References"] = " ".join(refs + [original.ident])
            message["To"] = to
            if draft.cc:
                message["Cc"] = draft.cc
            if draft.bcc:
                message["Bcc"] = draft.bcc
            message["Subject"] = subject
            message["Date"] = email.utils.format_datetime(dt.datetime.now().astimezone())
            ident = email.utils.make_msgid(domain=self.address.rpartition("@")[2] or None)
            message["Message-ID"] = ident
            message.set_content(draft.body)
            said = client.append(drafts, message.as_bytes(policy=email.policy.SMTP),
                                 flags=(b"\\Draft", b"\\Seen"))
            validity = self._select(client, drafts)
            appended = re.search(rb"APPENDUID (\d+) (\d+)", said if isinstance(said, bytes)
                                 else str(said).encode())
            if appended and appended[1].decode() == validity:
                uid = appended[2].decode()
            else:
                uid = self._find(client, ident)
            made = self._one(client, Where(drafts, validity, uid, ident), folders=folders)
        if made is None:
            raise ConnectorError("the draft was saved but could not be read again")
        return made

    def _sender(self, wanted: str) -> str:
        """The From of a message from this account: its address, or a
        plus-address of it where the manifest allows them."""
        if not wanted or wanted.lower() == self.address.lower():
            return self.address
        local, _, domain = self.address.lower().partition("@")
        mine = wanted.lower()
        if self.plus and mine.endswith(f"@{domain}") and mine.startswith(f"{local}+"):
            return wanted
        raise Unsupported(f"sending as {wanted}: this account sends as {self.address}")

    def _raw(self, client: imapclient.IMAPClient, where: Where) -> EmailMessage:
        """The message at `where` as stored, in a folder already selected."""
        data = client.fetch([int(where.uid)], ["BODY.PEEK[]"]).get(int(where.uid))
        if not data:
            raise TargetChanged("that message is no longer there")
        message = email.message_from_bytes(_body(data), policy=email.policy.default)
        if exact.norm(str(message.get("Message-ID") or "")) != exact.norm(where.ident):
            raise TargetChanged("the message there now is another one")
        return message

    def _find(self, client: imapclient.IMAPClient, ident: str) -> str:
        """The UID of the one message with Message-ID `ident` in the folder
        selected, checked here: the search is a filter, not the answer."""
        uids = client.search(["HEADER", "Message-ID", ident])
        heads = client.fetch(uids, ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"]) if uids else {}
        mine = [uid for uid in uids if uid in heads and exact.norm(str(
            email.message_from_bytes(_body(heads[uid]), policy=email.policy.default)
            .get("Message-ID") or "")) == exact.norm(ident)]
        if len(mine) != 1:
            raise TargetChanged(f"expected exactly one message {ident!r}; found {len(mine)}")
        return str(mine[0])

    def _has(self, client: imapclient.IMAPClient, folder: str, ident: str) -> bool:
        self._select(client, folder)
        try:
            self._find(client, ident)
            return True
        except TargetChanged:
            return False

    def send_draft(self, message: Message) -> Message:
        """Send the one draft the write path checked, then file it in Sent
        once and throw the draft away by its UID. Once the mail has gone it
        is reported sent, with `draft_kept` saying why the draft is still
        in Drafts where it could not be thrown away."""
        where = Where.parse(message.native_id)
        with self._session() as client:
            folders = _folders(client)
            drafts = _role(folders, _DRAFTS)
            if where is None or drafts is None or where.folder != drafts:
                raise TargetChanged("that is not a draft in this account's Drafts; "
                                    "nothing was sent")
            if self._select(client, where.folder) != where.validity:
                raise TargetChanged("the Drafts folder was rebuilt; nothing was sent")
            outgoing = self._raw(client, where)
            # Sent as a message of its own, as a mail client sends a draft.
            # Gmail keeps one message per Message-ID: sent with the draft's
            # own, it filed no copy in Sent and delivered none, and throwing
            # the draft away took the only copy to Trash (the live smoke of
            # 15 October 2026 found the message in no folder but Trash).
            del outgoing["Message-ID"]
            outgoing["Message-ID"] = email.utils.make_msgid(
                domain=self.address.rpartition("@")[2] or None)
            sender = email.utils.parseaddr(str(outgoing.get("From") or ""))[1]
            self._sender(sender)
            recipients = _addresses(str(outgoing.get("To") or ""), str(outgoing.get("Cc") or ""),
                                    str(outgoing.get("Bcc") or ""))
            if not recipients:
                raise ConnectorError("the draft has no one to send to")
            # The Bcc stays in the message: smtplib's send_message never
            # transmits it (its documented contract, which the tests hold),
            # and the user's own Sent copy keeps it, as any mail client's
            # does. Deleting it here took it from that copy alone.
            with self._smtp_session() as server:
                refused = server.send_message(outgoing, from_addr=sender, to_addrs=recipients)
            # The mail has gone, so nothing after this raises. Filing it and
            # throwing the draft away are tidying: an error from either said
            # it was not sent, and the card asked again, or a retry ran,
            # would send it twice. The answer says what was left instead.
            try:
                filed = self._file_sent(client, folders, outgoing)
            except Exception:
                filed = False
            kept = ""
            try:
                self._drop(client, where, folders, message.condition)
            except Exception as exc:
                kept = (str(exc) if isinstance(exc, ConnectorError)
                        else f"{type(exc).__name__}: {exc}")[:300]
        sent = Message(native_id="", ident=str(outgoing["Message-ID"]),
                       to=str(outgoing.get("To") or ""),
                       cc=str(outgoing.get("Cc") or ""), subject=str(outgoing.get("Subject") or ""),
                       direction="out", extra={"filed_in_sent": "yes" if filed else "no"})
        if refused:
            sent.extra["refused"] = ", ".join(sorted(refused))
        if kept:
            sent.extra["draft_kept"] = kept
        return sent

    def _file_sent(self, client: imapclient.IMAPClient, folders: list[tuple[str, set[str]]],
                   outgoing: EmailMessage) -> bool:
        """Sent holds one copy: the server's, where the manifest says it
        files one; else Steve's, unless one appears within SENT_WAIT_S where
        the manifest does not know."""
        sent = _role(folders, _SENT)
        if sent is None:
            return False
        ident = str(outgoing.get("Message-ID") or "")
        if self.sent_copy == "server":
            return True
        if self.sent_copy == "check":
            deadline = time.monotonic() + SENT_WAIT_S
            while True:
                if self._has(client, sent, ident):
                    return True
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                # Never past the deadline: a whole half-second step made a
                # wait of 0.2 s, as tests set it, last 0.5 s every send.
                time.sleep(min(0.5, left))
        try:
            client.append(sent, outgoing.as_bytes(policy=email.policy.SMTP), flags=(b"\\Seen",))
        except (imaplib.IMAP4.error, imap_errors.IMAPClientError):
            return False   # it went; only the filing failed, and the answer says so
        return True

    def _drop(self, client: imapclient.IMAPClient, where: Where,
              folders: list[tuple[str, set[str]]], condition: str | None) -> None:
        """One message out of its folder, by UID: moved to Trash where the
        server has MOVE, else marked and expunged by that UID alone. A
        server with neither keeps it: a bare EXPUNGE would take every
        message anyone marked deleted in the folder.

        `condition` is the message's MODSEQ as the write path read it.
        Where the server keeps one (CONDSTORE), the mark is made only if the
        message is unchanged since (UNCHANGEDSINCE), and a move only if it
        still reads the same."""
        trash = _role(folders, _TRASH)
        if self._select(client, where.folder, write=True) != where.validity:
            raise TargetChanged("that folder was rebuilt on the server")
        self._raw(client, where)
        uid = int(where.uid)
        modseq = condition.partition(":")[2] if condition else ""
        conditional = modseq.isdigit() and client.has_capability("CONDSTORE")
        if trash and where.folder != trash and client.has_capability("MOVE"):
            if conditional:
                now = client.fetch([uid], ["MODSEQ"]).get(uid, {}).get(b"MODSEQ", (None,))[0]
                if str(now) != modseq:
                    raise TargetChanged("the message changed on the server since it was read")
            client.move([uid], trash)
        elif client.has_capability("UIDPLUS"):
            if conditional:
                # The modifier rides with the operation: imaplib's UID STORE
                # takes exactly three arguments since Python 3.13, and a
                # fourth raised before anything reached the server.
                client._imap.uid("STORE", str(uid), f"(UNCHANGEDSINCE {modseq}) +FLAGS",
                                 "(\\Deleted)")
            else:
                client.add_flags([uid], [b"\\Deleted"])
            flags = client.fetch([uid], ["FLAGS"]).get(uid, {}).get(b"FLAGS", ())
            if b"\\Deleted" not in {f if isinstance(f, bytes) else str(f).encode()
                                     for f in flags}:
                raise TargetChanged("the message changed on the server since it was read")
            client.uid_expunge([uid])
        else:
            raise Unsupported("deleting one message on this server: it has neither MOVE nor "
                              "UID EXPUNGE, and an EXPUNGE could remove others")

    def delete(self, message: Message) -> None:
        """Throw away one draft, and nothing else."""
        where = Where.parse(message.native_id)
        with self._session() as client:
            folders = _folders(client)
            if where is None or where.folder != _role(folders, _DRAFTS):
                raise Unsupported("this account throws away drafts only")
            self._drop(client, where, folders, message.condition)

    def modify(self, message: Message, *, read: bool | None = None, archive: bool = False,
               add_labels: tuple[str, ...] = (), remove_labels: tuple[str, ...] = ()
               ) -> Message:
        where = Where.parse(message.native_id)
        if where is None:
            raise TargetChanged("that is not an id this account handed out")
        with self._session() as client:
            folders = _folders(client)
            if self._select(client, where.folder, write=True) != where.validity:
                raise TargetChanged("that folder was rebuilt on the server")
            self._raw(client, where)
            uid = int(where.uid)
            if read is not None:
                (client.add_flags if read else client.remove_flags)([uid], [b"\\Seen"])
            if _STAR in add_labels or _STAR in remove_labels:
                (client.add_flags if _STAR in add_labels else client.remove_flags)(
                    [uid], [b"\\Flagged"])
                add_labels = tuple(x for x in add_labels if x != _STAR)
                remove_labels = tuple(x for x in remove_labels if x != _STAR)
            if add_labels or remove_labels:
                if not (self.has(classes.LABELS) and self._xgm):
                    raise Unsupported("labels on this account")
                if add_labels:
                    client.labels([uid], b"+X-GM-LABELS", list(add_labels))
                if remove_labels:
                    client.labels([uid], b"-X-GM-LABELS", list(remove_labels))
            if archive:
                if self._xgm:
                    client.labels([uid], b"-X-GM-LABELS", ["\\Inbox"])
                elif where.folder.upper() == "INBOX":
                    # Out of the inbox, as the SDK has it. Moving from any
                    # folder took the user's own copy out of Sent, where the
                    # agent's mailbox had read an instruction from it.
                    where = self._move(client, where, folders, _role(folders, _ARCHIVE),
                                       what="archiving")
            return self._one(client, where, folders=folders) or message

    def move(self, message: Message, folder: str) -> Message:
        where = Where.parse(message.native_id)
        if where is None:
            raise TargetChanged("that is not an id this account handed out")
        with self._session() as client:
            folders = _folders(client)
            if folder not in {name for name, _ in folders}:
                raise ConnectorError(f"there is no folder {folder!r}")
            if self._select(client, where.folder, write=True) != where.validity:
                raise TargetChanged("that folder was rebuilt on the server")
            self._raw(client, where)
            moved = self._move(client, where, folders, folder, what="moving")
            return self._one(client, moved, folders=folders) or message

    def _move(self, client: imapclient.IMAPClient, where: Where,
              folders: list[tuple[str, set[str]]], target: str | None, *, what: str) -> Where:
        """Move one message by UID, in a folder selected for writing; where
        it is now, found again by its Message-ID."""
        if target is None:
            raise Unsupported(f"{what}: this account has no such folder")
        if not client.has_capability("MOVE"):
            raise Unsupported(f"{what} on this server, which has no MOVE")
        client.move([int(where.uid)], target)
        validity = self._select(client, target)
        return Where(target, validity, self._find(client, where.ident), where.ident)

    # --- health

    def test(self) -> str:
        with self._session() as client:
            folders = _folders(client)
            inbox = next((n for n, _ in folders if n.upper() == "INBOX"), "INBOX")
            info = client.select_folder(inbox, readonly=True)
            count = int(info.get(b"EXISTS", 0))
        with self._smtp_session():
            pass
        return f"signed in to read and to send; {count} messages in the inbox"


def engine(ctx: Any, spec: Any) -> ImapMail:
    if spec.klass != "mail":
        raise ValueError(f"the IMAP engine fills mail, not {spec.klass}")
    return ImapMail(ctx, spec)
