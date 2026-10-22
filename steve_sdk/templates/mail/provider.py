"""Example Mail: the template for a mail provider with a web API of its own.

To make a module of it, copy this folder to `modules/<id>/`, fill each
line of `manifest.toml` marked FILL, and rewrite each call below for the
service's API. Keep what each method returns and refuses: core's write
path (`core/writes.py`) relies on it, and the mail contract suite
(`steve_sdk.testing.mail`) checks it, against `fake.py` once that answers
as the service does (`steve_sdk/templates/__init__.py` says how).

Example's API, which this speaks (`API`):

- `GET /messages?folder=&q=&since=&thread=&body=&limit=&cursor=`: messages
  newest first, a page at a time, `next` the cursor for more. `folder` is
  inbox, sent or drafts; `q` is words, searched loosely; `since` an ISO
  time; `body=1` includes each body.
- `GET /messages/{id}`: one message, with its body and headers; 404 once
  it is gone.
- `PATCH /messages/{id}`: read or unread.
- `POST /drafts`: a draft made. `POST /messages/{id}/send`: a draft sent;
  the service files it in Sent itself, under a new id.
- `DELETE /messages/{id}`: to Trash, under a new id.
- Every message has a `version`. A write sends it as `If-Match`, and a 412
  says the message changed since.

What every copy keeps:

- **Only `ctx.http`, and no secret in hand.** The token is named in the
  header, `{{secret:token}}`; core fills it in, and only for the host and
  header the manifest binds it to. Nothing is imported but `steve_sdk`
  and the standard library's plain parts.
- **Exactly the message named.** `fetch` is by id, and None once it is
  gone. A write goes to the one message core's write path fetched and
  checked, never to what a search returned.
- **On a condition, where the service has one.** A write sends the
  version the message was read at, so a change made on another device
  since is refused (`TargetChanged`), not overwritten. Declare CONDITIONAL
  only where the service really refuses such a write: without it, core
  asks the user before each delete.
- **The server's answer is checked here.** A search's words and dates,
  and the folder of what came back, are checked again on this side: a
  service that drops a filter answers with more than was asked.
- **Errors core can say.** Each failure is one of `steve_sdk.errors`, so
  the account's health says what to do: a refused token asks for a new
  one, and a busy service is tried again later.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import replace
from email.utils import formataddr, getaddresses
from typing import Any

from steve_sdk import classes
from steve_sdk import query as query_syntax
from steve_sdk.classes import Draft, MailProvider, Message
from steve_sdk.context import Context
from steve_sdk.errors import (
    ConnectorError,
    NotConnected,
    TargetChanged,
    Transient,
    Unsupported,
    WrongPassword,
)

API = "https://api.example.com/v1"   # FILL: the service's API, on a host the manifest names
TIMEOUT_S = 30.0
PAGES = 10                           # a listing reads at most this many pages
ID = re.compile(r"[A-Za-z0-9._~-]{1,200}")


# --- the service --------------------------------------------------------------------

def call(ctx: Context, method: str, path: str, *, params: dict[str, str] | None = None,
         body: Any = None, version: str | None = None) -> Any:
    """One request: the JSON answered ({} for none), or None for a 404."""
    headers = {"Authorization": "{{secret:token}}", "Accept": "application/json"}
    if version is not None:
        headers["If-Match"] = version
    try:
        answer = ctx.http.request(method, f"{API}{path}", headers=headers, params=params,
                                  json=body, timeout=TIMEOUT_S)
    except PermissionError as exc:
        # The host rules refused it: the manifest or the account is wrong,
        # which trying again will not mend.
        raise NotConnected(str(exc)) from exc
    if answer.ok:
        return answer.json() if answer.content else {}
    if answer.status == 404:
        return None
    if answer.status in (401, 403):
        raise WrongPassword("the service did not take the token")
    if answer.status == 412:
        raise TargetChanged("the message changed on the service since it was read")
    if answer.status == 429 or answer.status >= 500:
        raise Transient(f"the service answered {answer.status}")
    raise ConnectorError(f"the service answered {answer.status}")


def listing(ctx: Context, path: str, params: dict[str, str], *, limit: int
            ) -> tuple[list[dict[str, Any]], bool]:
    """Up to `limit` items of a listing, page by page; and whether there
    were more than were read."""
    found: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(PAGES):
        asked = {**params, "limit": str(min(max(limit - len(found), 1), 100))}
        if cursor:
            asked["cursor"] = cursor
        page = call(ctx, "GET", path, params=asked) or {}
        found += page.get("items") or []
        cursor = page.get("next") or ""
        if not cursor or len(found) >= limit:
            break
    return found[:limit], bool(cursor) or len(found) > limit


def path_id(native_id: str) -> str:
    """An id as it goes into a path: never one that could name another path."""
    if not ID.fullmatch(native_id or ""):
        raise TargetChanged(f"{native_id!r} is not an id this service gives")
    return native_id


def iso(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms(when: dt.datetime | str) -> int:
    if isinstance(when, str):
        when = dt.datetime.fromisoformat(when)
    return int(when.timestamp() * 1000)


def addresses(text: str) -> list[str]:
    return [address.lower() for _, address in getaddresses([text or ""]) if "@" in address]


def _people(raw: list[dict[str, str]] | None) -> str:
    return ", ".join(formataddr((p.get("name") or "", p.get("email") or ""))
                     for p in raw or [])


def _to_people(text: str) -> list[dict[str, str]]:
    return [{"name": name, "email": address} for name, address in getaddresses([text or ""])
            if "@" in address]


def _message(raw: dict[str, Any]) -> Message:
    """Example's message as the SDK's. `direction` and `draft` come from
    the folder, which is the service's word, not a guess from the sender."""
    folder = str(raw.get("folder") or "")
    body = raw.get("body")
    return Message(
        native_id=str(raw["id"]), ident=str(raw.get("message_id") or ""),
        condition=raw.get("version"), thread=str(raw.get("thread") or ""),
        sender=_people([raw["from"]] if raw.get("from") else []),
        to=_people(raw.get("to")), cc=_people(raw.get("cc")), bcc=_people(raw.get("bcc")),
        subject=str(raw.get("subject") or ""), when=ms(str(raw["date"])),
        snippet=str(raw.get("snippet") or ""), body=body,
        body_length=len(body or ""), unread=bool(raw.get("unread")), folder=folder,
        direction="out" if folder in ("sent", "drafts") else "in", draft=folder == "drafts")


# --- the provider -------------------------------------------------------------------

class ExampleMail(MailProvider):
    # FILL: what the service can do beyond the class's minimum. Each one
    # declared is checked by its part of the contract suite.
    capabilities = frozenset({classes.THREADS, classes.FLAGS, classes.SEND,
                              classes.CONDITIONAL})

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def _list(self, params: dict[str, str], *, limit: int) -> tuple[list[Message], bool]:
        raw, more = listing(self.ctx, "/messages", params, limit=limit)
        return [_message(item) for item in raw], more

    def test(self) -> str:
        """Setup's test and the health check: one read that needs the token."""
        found, _ = self._list({"folder": "inbox"}, limit=1)
        return f"Example answers for {self.ctx.account.address}" + (
            "" if found else "; the inbox is empty")

    def search(self, query: str, *, limit: int, since: dt.datetime | None = None
               ) -> list[Message]:
        """Messages matching `query`, in the tools' syntax (`steve_sdk.query`),
        newest first, without bodies; drafts are not mail found."""
        parsed = query_syntax.parse(query)
        if parsed.days:
            floor = dt.datetime.now(dt.UTC) - dt.timedelta(days=parsed.days)
            since = max(since, floor) if since else floor
        words = parsed.terms + parsed.subject + parsed.sender + parsed.recipient
        params = {"q": " ".join(words)}
        if since:
            params["since"] = iso(since)
        if parsed.terms:
            # A plain word may be anywhere in a message, so each body comes
            # too, to check the word on this side.
            params["body"] = "1"
        found, _ = self._list(params, limit=max(limit, 50))
        floor_ms = ms(since) if since else 0
        kept = [m for m in found
                if not m.draft and m.when >= floor_ms
                and query_syntax.matches(parsed, subject=m.subject, sender=m.sender,
                                         recipient=f"{m.to} {m.cc}", unread=m.unread,
                                         text=f"{m.snippet} {m.body or ''}")]
        kept.sort(key=lambda m: m.when, reverse=True)
        return [replace(m, body=None, body_length=0) for m in kept[:limit]]

    def ignored(self, query: str) -> list[str]:
        return list(query_syntax.parse(query).ignored)

    def arrived(self, *, since: dt.datetime, limit: int,
                seen: Callable[[list[str]], set[str]]) -> list[Message]:
        """The inbox since `since`, less what `seen` says was seen before.
        A listing carries every envelope, so one call is the whole poll."""
        found = self.envelopes(sent=False, since=since, limit=limit)
        old = seen([m.native_id for m in found])
        return [m for m in found if m.native_id not in old]

    def envelopes(self, *, sent: bool, since: dt.datetime, limit: int) -> list[Message]:
        folder = "sent" if sent else "inbox"
        found, _ = self._list({"folder": folder, "since": iso(since)}, limit=limit)
        kept = [m for m in found if m.folder == folder and m.when >= ms(since)]
        kept.sort(key=lambda m: m.when, reverse=True)
        return kept

    def fetch(self, native_id: str, *, body_chars: int = 20_000) -> Message | None:
        raw = call(self.ctx, "GET", f"/messages/{path_id(native_id)}")
        if raw is None:
            return None
        message = _message(raw)
        body = message.body or ""
        return replace(message, body=body[:body_chars], body_length=len(body))

    def headers(self, native_id: str) -> dict[str, str]:
        raw = call(self.ctx, "GET", f"/messages/{path_id(native_id)}")
        return {str(k): str(v) for k, v in ((raw or {}).get("headers") or {}).items()}

    def thread(self, thread_id: str, *, limit: int) -> list[Message]:
        """The conversation, oldest first."""
        found, _ = self._list({"thread": thread_id}, limit=limit)
        kept = [m for m in found if m.thread == thread_id and not m.draft]
        return sorted(kept, key=lambda m: m.when)

    def sent_items(self, *, since: dt.datetime, limit: int, to: str = "") -> list[Message]:
        """What the user sent since `since`, with bodies: from Sent alone,
        never a search across folders, since only the user's own words may
        reach learning unredacted. The folder and `to` are checked on each."""
        if to and not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+", to):
            raise ValueError(f"not one address: {to!r}")
        found, _ = self._list({"folder": "sent", "since": iso(since), "body": "1"},
                              limit=max(limit, 50) if to else limit)
        kept = [m for m in found
                if m.folder == "sent" and m.when >= ms(since)
                and (not to or to.lower() in addresses(m.to))]
        kept.sort(key=lambda m: m.when, reverse=True)
        return kept[:limit]

    def neighbours(self, item: Message) -> dict[str, str | None] | None:
        """The messages in the item's folder, by Message-ID, with their
        versions: core checks after a write that only the one meant
        changed. None where the folder is too big to read whole."""
        if not item.folder:
            return None
        found, more = self._list({"folder": item.folder}, limit=500)
        if more:
            return None
        return {m.ident: m.condition for m in found if m.folder == item.folder}

    def modify(self, message: Message, *, read: bool | None = None, archive: bool = False,
               add_labels: tuple[str, ...] = (), remove_labels: tuple[str, ...] = ()
               ) -> Message:
        if archive or add_labels or remove_labels:
            raise Unsupported("archiving and labels")
        if read is None:
            return message
        changed = call(self.ctx, "PATCH", f"/messages/{path_id(message.native_id)}",
                       body={"unread": not read}, version=message.condition)
        if changed is None:
            raise TargetChanged("the message was gone when it was to be changed")
        return _message(changed)

    def make_draft(self, draft: Draft) -> Message:
        """Save a draft; the draft as stored, read back, with its id and
        Message-ID."""
        if draft.attachments:
            raise Unsupported("attachments")
        if draft.sender:
            raise Unsupported("sending from another address")
        body: dict[str, Any] = {"to": _to_people(draft.to), "cc": _to_people(draft.cc),
                                "bcc": _to_people(draft.bcc), "subject": draft.subject,
                                "body": draft.body}
        if draft.reply_to:
            body["in_reply_to"] = path_id(draft.reply_to)
        made = call(self.ctx, "POST", "/drafts", body=body) or {}
        again = self.fetch(str(made.get("id") or "")) if made.get("id") else None
        if again is None or not again.draft:
            raise ConnectorError("the service kept no draft")
        if sorted(addresses(f"{again.to},{again.cc},{again.bcc}")) != sorted(
                addresses(f"{draft.to},{draft.cc},{draft.bcc}")):
            raise TargetChanged("the draft was stored with other recipients than were given")
        return again

    def send_draft(self, message: Message) -> Message:
        """Send the one draft core's write path fetched and checked against
        its card, on its version: a draft changed since is not sent. The
        service files the sent copy itself, so Steve files none."""
        sent = call(self.ctx, "POST", f"/messages/{path_id(message.native_id)}/send",
                    version=message.condition)
        if not sent:
            raise TargetChanged("the draft was gone when it was to be sent")
        return _message(sent)

    def delete(self, message: Message) -> None:
        """To Trash: the one message core's write path fetched and checked,
        on its version."""
        if call(self.ctx, "DELETE", f"/messages/{path_id(message.native_id)}",
                version=message.condition) is None:
            raise TargetChanged("the message was gone when it was to be deleted")
