"""A mail account for tests, held in a dict, and the slot showing only it.

Since step 5 every mail tool reaches an account through `agent/connectors.py`,
and every write goes through `core/writes.py`, which reads the item back by
its id before it writes. A test that faked the tools' executors, as the card
and sending tests did, no longer reaches the code a card depends on: the
draft read back to say whom it goes to, and the digest that binds the yes to
it. This account is that code's far side: a provider with a dict behind it,
driven by the write path like any other, which the test then reads to see
what waits in Drafts and what was sent.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import itertools

from steve_sdk import classes
from steve_sdk.classes import Draft, MailProvider, Message
from steve_sdk.errors import TargetChanged


class Mailbox(MailProvider):
    """One account's mail, by native id. `drafts()`, `drafted` and `sent` say what a
    test did to it; `edit` changes a draft as the user would in webmail."""

    shareable = True

    def __init__(self, address: str, *messages: Message) -> None:
        self.address = address
        self.capabilities = frozenset({classes.SEND, classes.THREADS})
        self.items: dict[str, Message] = {m.native_id: m for m in messages}
        self.drafted: list[Draft] = []  # what each draft was asked to be
        self.sent: list[Message] = []
        self.deleted: list[str] = []
        self.modified: list[tuple[str, dict]] = []
        # Headers beyond the From and Message-ID each message has, by native id.
        self.heads: dict[str, dict[str, str]] = {}
        self._ids = itertools.count(1)

    def drafts(self) -> list[Message]:
        """What waits in this account's Drafts."""
        return [m for m in self.items.values() if m.draft]

    def edit(self, native_id: str, **change) -> None:
        self.items[native_id] = dataclasses.replace(self.items[native_id], **change)

    def search(self, query: str, *, limit: int, since=None) -> list[Message]:
        """Every word somewhere in the message; `from:`, `to:` (To and Cc)
        and `subject:` in that field."""
        def has(m: Message, word: str) -> bool:
            key, colon, value = word.partition(":")
            fields = {"from": m.sender, "to": f"{m.to} {m.cc}", "subject": m.subject}
            if colon and key in fields:
                return value in fields[key].lower()
            return word in f"{m.sender} {m.to} {m.subject} {m.body}".lower()

        found = [m for m in self.items.values()
                 if not m.draft and all(has(m, w) for w in query.lower().split())]
        found.sort(key=lambda m: m.when, reverse=True)
        return [dataclasses.replace(m, body=None, snippet=(m.body or "")[:200])
                for m in found[:limit]]

    def fetch(self, native_id: str, *, body_chars: int = 20_000) -> Message | None:
        found = copy.deepcopy(self.items.get(native_id))
        if found is not None and found.body is not None:
            found.body_length = len(found.body)
            found.body = found.body[:body_chars]
        return found

    def thread(self, thread_id: str, *, limit: int) -> list[Message]:
        found = [copy.deepcopy(m) for m in self.items.values() if m.thread == thread_id]
        return sorted(found, key=lambda m: m.when)[:limit]

    def make_draft(self, draft: Draft) -> Message:
        native = f"draft-{next(self._ids)}"
        answered = self.items.get(draft.reply_to)
        made = Message(native_id=native, ident=f"<{native}@{self.address.partition('@')[2]}>",
                       thread=answered.thread if answered else "", sender=self.address,
                       to=draft.to, cc=draft.cc, bcc=draft.bcc, subject=draft.subject,
                       body=draft.body, body_length=len(draft.body),
                       attachments=draft.attachments, direction="out", draft=True)
        self.items[native] = made
        self.drafted.append(draft)
        return copy.deepcopy(made)

    def send_draft(self, message: Message) -> Message:
        draft = self.items.pop(message.native_id)
        sent = dataclasses.replace(draft, native_id=f"sent-{next(self._ids)}", draft=False,
                                   labels=("SENT",))
        self.items[sent.native_id] = sent
        self.sent.append(sent)
        return copy.deepcopy(sent)

    def delete(self, message: Message) -> None:
        self.items.pop(message.native_id, None)
        self.deleted.append(message.native_id)

    def sent_items(self, *, since, limit: int, to: str = "") -> list[Message]:
        return [m for m in self.sent if to.lower() in m.to.lower()][-limit:]

    def headers(self, native_id: str) -> dict[str, str]:
        m = self.items.get(native_id)
        if m is None:
            raise TargetChanged("that message is no longer there")
        return {"From": m.sender, "Message-ID": m.ident, **self.heads.get(native_id, {})}

    def modify(self, message: Message, *, read: bool | None = None, archive: bool = False,
               add_labels: tuple[str, ...] = (), remove_labels: tuple[str, ...] = ()
               ) -> Message:
        """Recorded in `modified`, by native id, with what was asked."""
        asked: dict = {} if read is None else {"read": read}
        asked.update((k, v) for k, v in (("archive", archive), ("add_labels", add_labels),
                                         ("remove_labels", remove_labels)) if v)
        self.modified.append((message.native_id, asked))
        return copy.deepcopy(self.items[message.native_id])

    def envelopes(self, *, sent: bool, since, limit: int) -> list[Message]:
        """What was sent (`sent`) or received since `since`, newest first,
        without bodies; a message with no time is kept."""
        pool = self.sent if sent else [m for m in self.items.values()
                                       if not m.draft and m.direction != "out"]
        floor = since.timestamp() * 1000
        found = sorted((m for m in pool if not m.when or m.when >= floor),
                       key=lambda m: m.when, reverse=True)
        return [dataclasses.replace(m, body=None) for m in found[:limit]]


def connect(monkeypatch, **mailboxes: Mailbox) -> dict:
    """These accounts, by name, as the only mail accounts there are: to the
    tools, the write path, and consent's own addresses. Each name's account
    is returned, so a test can make the ids it would have handed out."""
    from agent import connectors

    made = {name: connectors.Connected(
        key=hashlib.sha256(name.encode()).hexdigest()[:12], name=name, klass="mail",
        module="tests", provider=box, address=box.address)
        for name, box in mailboxes.items()}
    real = connectors.accounts

    def accounts(conn, klass, **kw):
        return list(made.values()) if klass == "mail" else real(conn, klass, **kw)

    monkeypatch.setattr(connectors, "accounts", accounts)
    monkeypatch.setattr(connectors, "addresses", lambda conn, **kw: frozenset(
        box.address.lower() for box in mailboxes.values()))
    live(monkeypatch, made)
    return made


def live(monkeypatch, made: dict) -> None:
    """These accounts' rows as live to `core/modules.live`, as a connected
    account's are: its cards listed, its observations judged."""
    from core import modules

    real = modules.live_keys
    keys = frozenset(found.key for found in made.values())
    monkeypatch.setattr(modules, "live_keys",
                        lambda conn, manifests=None: real(conn, manifests) | keys)
