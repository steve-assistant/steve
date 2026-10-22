"""A mail search in the tools' syntax, read as far as a server without it can say it.

The mail tools take one query string for every account, in the syntax the
first mailbox spoke (`from:`, `is:unread`, `newer_than:7d`). Classic
Outlook has no such language, and before this existed an ordinary
`is:unread` was matched there as literal text against subject lines, which
can never hit: the mailbox did not answer "none unread", it answered "no
message contains the words is:unread", and upstream those look identical.

It lived in Outlook's module until the IMAP engine needed it too (step 5,
item 6). A server with the syntax's own extension is handed the query as it
is; any other gets what `parse` reads of it, and `matches` checks each
message on our side, since a server's filter is never the last word.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Operators that mean something without the syntax's own server.
KNOWN_OPERATORS = frozenset({
    "is", "from", "to", "subject", "newer_than", "older_than", "in", "label",
    "has", "category", "filename", "after", "before", "cc", "bcc",
})


@dataclass
class Query:
    terms: list[str]
    unread_only: bool = False
    sender: list[str] = field(default_factory=list)
    recipient: list[str] = field(default_factory=list)
    subject: list[str] = field(default_factory=list)
    days: int | None = None
    ignored: list[str] = field(default_factory=list)


def parse(query: str) -> Query:
    """Read as much of a query as means anything without its own server.

    An operator this does not understand is dropped rather than matched
    literally, and named in `ignored` so the answer can say so. Dropping
    widens the search, which returns too much; keeping it as text narrows to
    nothing. Between a wrong answer and an empty one, the wrong answer is at
    least visibly wrong.
    """
    parsed = Query(terms=[])
    for word in (query or "").split():
        token = word.strip().lower()
        if not token:
            continue
        head, sep, value = token.partition(":")
        if not sep or head not in KNOWN_OPERATORS:
            parsed.terms.append(token)
            continue
        value = value.strip('"')
        if head == "is" and value in {"unread", "new"}:
            parsed.unread_only = True
        elif head == "from":
            parsed.sender.append(value)
        elif head in {"to", "cc", "bcc"}:
            parsed.recipient.append(value)
        elif head == "subject":
            parsed.subject.append(value)
        elif head == "newer_than" and value[:-1].isdigit():
            span = {"d": 1, "w": 7, "m": 30, "y": 365}.get(value[-1], 1)
            parsed.days = int(value[:-1]) * span
        else:
            parsed.ignored.append(word)
    return parsed


def matches(parsed: Query, *, subject: str, sender: str, recipient: str,
            unread: bool, text: str = "") -> bool:
    """Every clause of the query, against one message's envelope.

    Substring matching, not the full language. Outlook has a filter syntax,
    but its date and property forms vary by locale and version, and a wrong
    one returns an empty set rather than an error -- the same silent-nothing
    failure that made the first sent-mail probe report zero. Walking what
    was fetched is cheap and cannot lie. A plain word may also be in
    `text`, the message's own, where the server searched it there.
    """
    if parsed.unread_only and not unread:
        return False
    subject, sender, recipient = subject.lower(), sender.lower(), recipient.lower()
    if any(s not in subject for s in parsed.subject):
        return False
    if parsed.sender and not any(s in sender for s in parsed.sender):
        return False
    if parsed.recipient and not any(r in recipient for r in parsed.recipient):
        return False
    haystack = f"{subject} {sender} {recipient} {text.lower()}"
    return all(term in haystack for term in parsed.terms)
