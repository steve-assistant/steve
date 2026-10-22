"""Gmail: search, read, send.

Bodies are decoded and flattened to plain text here rather than handed to the
model raw. A modern marketing email is 200KB of nested MIME and inlined CSS;
passing that through would cost more in tokens than the answer is worth and
bury the two sentences that matter.
"""

from __future__ import annotations

import base64
import json
import re
from email.message import EmailMessage
from typing import Any

from googleapiclient.errors import HttpError

from .common import clamp, guard, service

# Enough of a message to answer "what does this say" without pasting a
# newsletter into the context window.
MAX_BODY_CHARS = 4_000

_HTML_TAG = re.compile(r"<[^>]+>")
_STYLE_OR_SCRIPT = re.compile(r"<(style|script)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_WHITESPACE = re.compile(r"[ \t]*\n[ \t]*")


def _gmail():
    return service("gmail", "v1")


def _header(payload: dict, name: str) -> str:
    for header in payload.get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _metadata(message_id: str, headers: list[str], service=None) -> dict:
    """One message's named headers and snippet, without its body: Gmail's
    cheap call, which four functions here spelled out in full."""
    return (service or _gmail()).users().messages().get(
        userId="me", id=message_id, format="metadata", metadataHeaders=headers).execute()


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", "replace")


def _html_to_text(html: str) -> str:
    text = _STYLE_OR_SCRIPT.sub(" ", html)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG.sub(" ", text)
    for entity, char in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
        ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
    ):
        text = text.replace(entity, char)
    return text


def _extract_body(payload: dict) -> str:
    """Walk the MIME tree, preferring text/plain over text/html.

    Multipart messages carry both; the plain part is what the sender's client
    produced for exactly this purpose, so prefer it and fall back to stripping
    tags only when there is no choice.
    """
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if data := body.get("data"):
            if mime == "text/plain":
                plain.append(_decode(data))
            elif mime == "text/html":
                html.append(_decode(data))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    text = "\n".join(plain) if plain else _html_to_text("\n".join(html))
    text = _WHITESPACE.sub("\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _summarise(message: dict, *, include_body: bool, body_chars: int = MAX_BODY_CHARS) -> dict:
    payload = message.get("payload", {})
    out: dict[str, Any] = {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "from": _header(payload, "From"),
        "to": _header(payload, "To"),
        "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
        "snippet": message.get("snippet", ""),
        "labels": message.get("labelIds", []),
        "unread": "UNREAD" in message.get("labelIds", []),
        # Epoch milliseconds, the same field Outlook summaries carry. Missing
        # until the attention loop needed to sort both mailboxes together:
        # `localise` could not stamp a Gmail message with a readable time,
        # and a merged listing sorted every Gmail message last.
        "internal_date": int(message.get("internalDate") or 0),
    }
    if include_body:
        body = _extract_body(payload)
        out["body"] = body[:body_chars]
        out["body_truncated"] = len(body) > body_chars
        out["body_length"] = len(body)
    return out


@guard
def search(query: str = "is:unread", limit: int = 10) -> dict:
    """Search mail using Gmail's own query syntax.

    Accepts exactly what the Gmail search box accepts: `from:sarah
    has:attachment newer_than:3d`, `is:unread label:important`, and so on.
    Passing the syntax straight through beats inventing a worse one.
    """
    limit = clamp(limit, 10, 50)
    listing = (
        _gmail().users().messages()
        .list(userId="me", q=query, maxResults=limit)
        .execute()
    )
    ids = [m["id"] for m in listing.get("messages", [])]

    # metadata format: headers without bodies, which is the cheap call. The
    # model asks for a body explicitly when it needs one.
    messages = [_summarise(_metadata(message_id, ["From", "To", "Subject", "Date"]),
                           include_body=False) for message_id in ids]

    return {
        "query": query,
        "count": len(messages),
        "estimated_total": listing.get("resultSizeEstimate"),
        "messages": messages,
    }


@guard
def message_ids(query: str, limit: int = 50) -> dict:
    """The ids a query matches, and nothing else: one call.

    For a watcher polling every few minutes. `search` fetches headers for
    every hit, one call each, so polling it re-downloads the same fifty
    messages every time; this lets the caller fetch only what it has not
    seen, which on a quiet afternoon is nothing.
    """
    listing = (
        _gmail().users().messages()
        .list(userId="me", q=query, maxResults=clamp(limit, 50))
        .execute()
    )
    return {"ids": [m["id"] for m in listing.get("messages", [])]}


# What a mailing system adds, asked for by name in the same metadata call:
# whether a message was written to them or sent to a list the user is on. The
# attention loop's sender check reads them (`agent/attention/vet.py`); the
# address alone let Trey Research's "Personal Invitation from the Trey Research CEO" pass
# as a person on 9 October.
MAILING_HEADERS = {
    "Cc": "cc", "Message-ID": "rfc_id", "List-Id": "list_id",
    "List-Unsubscribe": "list_unsubscribe", "Precedence": "precedence",
    "Auto-Submitted": "auto_submitted",
}


@guard
def summary(message_id: str) -> dict:
    """One message's headers and snippet, in the shape `search` returns,
    plus the mailing headers (`MAILING_HEADERS`) -- no extra request."""
    full = _metadata(message_id, ["From", "To", "Subject", "Date", *MAILING_HEADERS])
    out = _summarise(full, include_body=False)
    payload = full.get("payload", {})
    for header, key in MAILING_HEADERS.items():
        out[key] = _header(payload, header)
    return out


@guard
def headers(query: str, limit: int = 300) -> dict:
    """Envelope headers for many messages, paging past the search cap.

    `search` is the model's tool and is capped at 50 on purpose -- fifty
    results is already more than an answer needs. This is for the ingest,
    which wants a whole window and never reads a body.

    `internal_date` is Gmail's own epoch-milliseconds timestamp rather than
    the Date: header. The header is whatever the sending client claimed, in
    RFC 2822, in a timezone of its choosing; the ledger sorts and compares on
    this field and needs one that cannot be argued with.
    """
    service = _gmail()
    ids: list[str] = []
    page = None
    while len(ids) < limit:
        listing = (
            service.users().messages()
            .list(
                userId="me", q=query, pageToken=page,
                maxResults=min(500, limit - len(ids)),
            )
            .execute()
        )
        ids.extend(m["id"] for m in listing.get("messages", []))
        page = listing.get("nextPageToken")
        if not page:
            break

    messages = []
    for message_id in ids[:limit]:
        full = _metadata(message_id, ["From", "To", "Cc", "Subject", "Date"], service)
        summary = _summarise(full, include_body=False)
        summary["cc"] = _header(full.get("payload", {}), "Cc")
        summary["internal_date"] = int(full.get("internalDate") or 0)
        messages.append(summary)

    return {"query": query, "count": len(messages), "messages": messages}


NO_THREAD = ("That email thread could not be found. Search email again by query, then use "
             "the thread id from the results.")


@guard
def thread(thread_id: str, limit: int = 20) -> dict:
    """Every message in a conversation, oldest first.

    Gmail thread ids are opaque ASCII identifiers. Reject names, email addresses,
    and other malformed inputs locally: sending them to Gmail only produces an
    opaque 400 and invites the caller to retry the same impossible lookup.
    """
    thread_id = thread_id.strip() if isinstance(thread_id, str) else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id):
        return {"error": NO_THREAD, "kind": "invalid_input"}

    try:
        data = _gmail().users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()
    except HttpError as exc:
        if exc.resp.status == 400:
            try:
                payload = json.loads(exc.content.decode("utf-8"))
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            detail = (payload.get("error") or {}).get("message", "")
            if detail == "Invalid id value":
                return {"error": NO_THREAD, "kind": "not_found", "status": 400,
                        "detail": detail}
        raise

    messages = [
        _summarise(m, include_body=True) for m in data.get("messages", [])[: clamp(limit, 20)]
    ]
    return {"thread_id": thread_id, "count": len(messages), "messages": messages}


def _compose(
    to: str,
    subject: str,
    body: str,
    *,
    cc: str | None = None,
    bcc: str | None = None,
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    """Build the payload Gmail wants, for a send or a draft alike.

    Shared so a draft is byte-for-byte the message that would have been sent.
    A draft that threads differently from the send is worse than no draft: the user
    reads the one and gets the other.
    """
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message.set_content(body)

    payload: dict[str, Any] = {}
    if reply_to_message_id:
        original = _metadata(reply_to_message_id, ["Message-ID", "References", "Subject"])
        original_id = _header(original.get("payload", {}), "Message-ID")
        if original_id:
            message["In-Reply-To"] = original_id
            existing = _header(original.get("payload", {}), "References")
            message["References"] = f"{existing} {original_id}".strip()
        payload["threadId"] = original.get("threadId")

    payload["raw"] = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return payload


@guard
def draft(
    to: str,
    subject: str,
    body: str,
    *,
    cc: str | None = None,
    bcc: str | None = None,
    reply_to_message_id: str | None = None,
) -> dict:
    """Write an email without sending it, and return a link that opens it.

    Authorised by the `gmail.modify` scope already granted -- no re-consent.

    The link is the point. Pasting proposed text into a chat window looks
    like drafting and is not: the user cannot edit it, cannot send it, and has to
    retype it into Gmail themselves. A draft the user can open on their phone, change a
    word in and send is the thing that was being promised.
    """
    payload = _compose(
        to, subject, body, cc=cc, bcc=bcc, reply_to_message_id=reply_to_message_id
    )
    created = (
        _gmail().users().drafts().create(userId="me", body={"message": payload}).execute()
    )
    message_id = (created.get("message") or {}).get("id")
    return {
        "drafted": True,
        "draft_id": created.get("id"),
        "message_id": message_id,
        "thread_id": (created.get("message") or {}).get("threadId"),
        # Gmail resolves a draft by its *message* id here, not the draft id --
        # they differ, and using the wrong one opens an empty compose window.
        "url": f"https://mail.google.com/mail/u/0/#drafts?compose={message_id}",
        "to": to,
        "subject": subject,
    }


@guard
def delete_draft(draft_id: str) -> dict:
    """Throw a draft away. The inverse of `draft`."""
    _gmail().users().drafts().delete(userId="me", id=draft_id).execute()
    return {"deleted": True, "draft_id": draft_id}


@guard
def envelope(message_id: str) -> dict:
    """A message's headers -- all of them, top first, in the order the
    servers wrote them -- with its labels and thread, and no body. For the
    agent's mailbox (agent/mailbox.py), which must read Gmail's own
    Authentication-Results above whatever the sender wrote
    (core/mailbox.py)."""
    found = _gmail().users().messages().get(
        userId="me", id=message_id, format="metadata").execute()
    return {"id": found.get("id"), "thread_id": found.get("threadId"),
            "labels": found.get("labelIds", []),
            "headers": [(h.get("name", ""), h.get("value", ""))
                        for h in found.get("payload", {}).get("headers", [])],
            "internal_date": int(found.get("internalDate") or 0)}


@guard
def profile() -> dict:
    """Which mailbox is connected, and how big it is."""
    data = _gmail().users().getProfile(userId="me").execute()
    return {
        "email": data.get("emailAddress"),
        "messages_total": data.get("messagesTotal"),
        "threads_total": data.get("threadsTotal"),
    }
