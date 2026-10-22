"""Who the agent's own mailbox obeys, decided where the agent cannot change it.

The maintainer, 4 October 2026: the agent gets an address of its own -- their
address with `+agent` -- so they can forward it a mail ("deal with this") or
CC it into a thread. Anyone can write to that address, and their name in a
From line costs nothing to forge. So a mail there is their instruction only if
its From is one of the addresses the user opted in, the agent did not send it
itself, and either:

- **a copy of it is in the user's own Sent folder,** at one of their
  connected accounts, sent to the mailbox. Nothing arriving from outside
  can put one there. Its text is read from that copy, never from the one
  that arrived, which someone on the thread who knows its Message-ID could
  have written; or
- **the receiving server says the user sent it:** the topmost
  Authentication-Results header (the one that server wrote, not one further
  down that the sender could have written) passes DMARC for the From's
  domain, the server is one this table trusts for the host the account
  reads its mail from (`TRUST`), and that provider ties the From to the
  signed-in account (`Trust.tied`). DKIM, SPF and DMARC prove a domain, not
  a person: on a custom or work domain, colleagues and a shared host pass
  too, so such an address counts by its Sent copy alone, unless the user
  ticked "only I send as this domain" (`own_domain`).

And a Message-ID gives one instruction, however many copies of it arrive.

Until step 5 the mailbox was a Gmail address in the frozen policy, its
users' addresses beside it, and DKIM or SPF alone, aligned with the From's
domain, was enough: a colleague at the user's firm could pass as them. The
table below stays in core, frozen, keyed by the host an account reads its
mail from, so a module can neither widen it nor claim a host it does not
connect to.

The agent never writes to its own mailbox (`names_mailbox`, applied by
`core/monitor` to every caller): an agent that could mail itself could write
its own instructions -- an email, an invitation or a draft, from an account
the checks above would accept as theirs.

Which account holds the mailbox, and which addresses are the user's, are
the app's to write and nobody else's (`choose`, `opt_in`;
tests/gate/test_only_the_app.py). The mailbox's plumbing is agent/mailbox.py.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import getaddresses
from typing import Any

# The arguments through which a tool names whom it reaches.
RECIPIENT_ARGS = ("to", "cc", "bcc", "attendees", "reply_to", "recipients")
# The domains whose mail ignores dots in an address's name:
# m.lindqvist@ and mlindqvist@ are one mailbox there.
DOTLESS = ("gmail.com", "googlemail.com")
TAG = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
_ADDRESS = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")


def canonical(address: str) -> str:
    """An address as its server delivers it: lower case, and where the
    domain ignores dots (`DOTLESS`), the dots of the name dropped."""
    address = (address or "").strip().lower()
    local, _, domain = address.rpartition("@")
    if not local or not domain:
        return address
    if domain in DOTLESS:
        base, plus, tag = local.partition("+")
        local = base.replace(".", "") + plus + tag
        domain = DOTLESS[0]
    return f"{local}@{domain}"


def alias(address: str, tag: str) -> str:
    """The mailbox's address: the account's, with `+tag` before the @."""
    local, _, domain = canonical(address).partition("@")
    return f"{local.partition('+')[0]}+{tag.strip().lower()}@{domain}" if local else ""


def is_mailbox(address: str, own: str) -> bool:
    """Whether `address` delivers to the mailbox `own`, in any spelling."""
    return bool(own) and canonical(address) == canonical(own)


def addresses_in(value) -> list[str]:
    """Every address a recipient argument names: a list, or text with
    "Name <a@b>" and commas or semicolons between."""
    if value is None or value == "":
        return []
    items = value if isinstance(value, list | tuple) else [str(value)]
    found = []
    for item in items:
        for _, address in getaddresses([str(item).replace(";", ",")]):
            if address:
                found.append(address.strip())
    return found


def names_mailbox(args: dict, own: str) -> bool:
    """Whether a tool call would reach the agent's own mailbox `own`."""
    return any(is_mailbox(a, own)
               for arg in RECIPIENT_ARGS for a in addresses_in((args or {}).get(arg)))


# ---------------------------------------------------------------------------
# Whose authentication results are believed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trust:
    """For an account's mail: whose Authentication-Results header is
    believed, and the domains whose From that provider ties to the
    signed-in account, so that DMARC passing for one proves the person."""

    authserv: tuple[str, ...]
    tied: tuple[str, ...]


_TIED_TO_SIGN_IN = Trust(authserv=("mx.google.com",), tied=DOTLESS)
# By the host an account reads its mail from (`mail_host`). An account on
# any other host counts the user's mail by its Sent copy alone.
TRUST: dict[str, Trust] = {
    "imap.gmail.com": _TIED_TO_SIGN_IN,
    "gmail.googleapis.com": _TIED_TO_SIGN_IN,
}


def mail_host(manifest: Any, preset: str | None, fields: Mapping[str, Any] | None) -> str:
    """The host an account reads its mail from, as core knows it: the IMAP
    engine's server, fixed by the manifest or typed at setup; a built-in's
    own code's `mail_host`. Nothing for an engine that reads elsewhere."""
    spec = manifest.specs.get("mail") if manifest is not None else None
    if spec is None:
        return ""
    settings = manifest.settings("mail", preset)
    if spec.engine == "imap":
        where = str(settings.get("imap") or (fields or {}).get("imap") or "")
        return where.rsplit(":", 1)[0].strip().lower()
    if spec.engine == "code":
        return str(settings.get("mail_host") or "").strip().lower()
    return ""


def plus_addresses(manifest: Any, preset: str | None) -> bool:
    """Whether the manifest says the account's server delivers mail to a
    plus-address of it: only there can the mailbox be."""
    if manifest is None or "mail" not in manifest.specs:
        return False
    return bool(manifest.settings("mail", preset).get("plus_addresses"))


# ---------------------------------------------------------------------------
# Authentication-Results (RFC 8601)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Results:
    authserv: str
    dmarc: tuple[tuple[str, str], ...]  # (result, header.from)


_COMMENT = re.compile(r"\([^()]*\)")


def parse_results(value: str) -> Results:
    """One Authentication-Results header: who wrote it, and its dmarc
    results with the domain each is for."""
    text = _COMMENT.sub(" ", value or "")
    parts = [p.strip() for p in text.split(";")]
    authserv = parts[0].split()[0].lower() if parts and parts[0].split() else ""
    dmarc = []
    for part in parts[1:]:
        words = part.split()
        if not words or "=" not in words[0]:
            continue
        method, _, result = words[0].lower().partition("=")
        if method != "dmarc":
            continue
        props = {}
        for word in words[1:]:
            key, eq, val = word.partition("=")
            if eq:
                props[key.lower()] = val.strip().strip('"').lower()
        dmarc.append((result, props.get("header.from", "")))
    return Results(authserv, tuple(dmarc))


# ---------------------------------------------------------------------------
# Whether a mail is theirs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    own: bool  # the user's own mail
    why: str
    sender: str = ""


def header(headers: Mapping[str, str], name: str) -> str:
    """A header's value, whatever its case: the topmost of its name, which
    is what a provider gives (`MailProvider.headers`)."""
    wanted = name.lower()
    return next((str(v) for n, v in (headers or {}).items() if str(n).lower() == wanted), "")


def from_user(headers: Mapping[str, str], *, user: Mapping[str, bool], sent_copy: bool,
              trust: Trust | None, agent_sent: bool = False, taken: bool = False) -> Verdict:
    """Whether this mail is the user's own. `user` is their opted-in
    addresses, each with whether only they send as its domain; `sent_copy`
    whether a copy of it, sent to the mailbox, is in the Sent folder of an
    account of one of those addresses; `trust` the table's entry for the
    host the mail was read from; `agent_sent` from the ledger; `taken`
    whether its Message-ID has already given an instruction."""
    found = addresses_in(header(headers, "From"))
    if len(found) != 1:
        return Verdict(False, "not from exactly one address")
    sender = canonical(found[0])
    theirs = {canonical(a): bool(only) for a, only in (user or {}).items()}
    if sender not in theirs:
        return Verdict(False, "from someone else", sender)
    if agent_sent:
        return Verdict(False, "one the agent sent itself", sender)
    if taken:
        return Verdict(False, "a copy of a mail already taken as the user's", sender)
    if sent_copy:
        return Verdict(True, "a copy is in the user's own Sent folder", sender)
    if trust is None:
        return Verdict(False, "no copy in the user's Sent folder, which on this account is "
                              "the only proof", sender)
    results = header(headers, "Authentication-Results")
    if not results:
        return Verdict(False, "no copy in the user's Sent folder, and no authentication "
                              "results", sender)
    # The topmost only: the receiving server writes its own above
    # everything the sender sent, and a sender can write one of its own.
    top = parse_results(results)
    if top.authserv not in trust.authserv:
        return Verdict(False, f"authentication results from {top.authserv or 'nobody'}", sender)
    domain = sender.rpartition("@")[2]
    if not any(r == "pass" and canonical(f"x@{d}").partition("@")[2] == domain
               for r, d in top.dmarc):
        return Verdict(False, f"did not pass DMARC for {domain}", sender)
    if domain in trust.tied or theirs[sender]:
        return Verdict(True, f"dmarc=pass for {sender}", sender)
    return Verdict(False, f"{domain} passed DMARC, which anyone sending as {domain} can; "
                          "it counts by its Sent copy alone, unless the user says only "
                          "they send as it", sender)


# ---------------------------------------------------------------------------
# The mailbox and the user's addresses: written by the app alone
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Own:
    """The mailbox: the account it is at, that account's address, and the tag."""

    account: str
    address: str
    tag: str

    @property
    def alias(self) -> str:
        return alias(self.address, self.tag)


def own(conn: sqlite3.Connection) -> Own | None:
    """The mailbox, while the user has one. An account switched off keeps
    it, so mail to it is still refused while the account is off."""
    row = conn.execute(
        "SELECT o.account, o.tag, c.address FROM own_mailbox o JOIN account_classes c"
        " ON c.account = o.account AND c.class = 'mail' WHERE o.id = 1").fetchone()
    if row is None or not row[2]:
        return None
    return Own(account=row[0], address=str(row[2]).lower(), tag=row[1])


def own_address(conn: sqlite3.Connection) -> str:
    """The mailbox's address, or "" while there is none."""
    found = own(conn)
    return found.alias if found is not None else ""


def user_addresses(conn: sqlite3.Connection) -> dict[str, bool]:
    """The addresses the user opted in, each with whether only they send
    as its domain."""
    return {canonical(a): bool(only)
            for a, only in conn.execute("SELECT address, own_domain FROM user_addresses")}


def trust(conn: sqlite3.Connection, account: str) -> Trust | None:
    """The table's entry for the host an account reads its mail from."""
    from core import modules

    row = modules.account(conn, account)
    held = modules.module(conn, row.module) if row is not None else None
    if held is None or held.manifest is None:
        return None
    return TRUST.get(mail_host(held.manifest, row.preset, row.fields))


def choose(conn: sqlite3.Connection, account: str, tag: str = "agent") -> Own:
    """The mailbox at this mail account, `+tag`: the app's choice alone."""
    from core import db, modules

    tag = (tag or "").strip().lower()
    if not TAG.fullmatch(tag):
        raise modules.AccountError("the tag is letters and digits, up to 32")
    row = modules.account(conn, account)
    if row is None or "mail" not in row.classes:
        raise modules.AccountError("no mail account has that key")
    held = modules.module(conn, row.module)
    if held is None or not plus_addresses(held.manifest, row.preset):
        raise modules.AccountError(
            f"{row.name} does not deliver mail to a plus-address, so the agent's own "
            "mailbox cannot be there")
    with db.transaction(conn):
        conn.execute("INSERT INTO own_mailbox (id, account, tag) VALUES (1, ?, ?)"
                     " ON CONFLICT (id) DO UPDATE SET account = excluded.account,"
                     " tag = excluded.tag, chosen_at = datetime('now')", (account, tag))
    found = own(conn)
    assert found is not None
    return found


def stop(conn: sqlite3.Connection) -> None:
    """No mailbox: what reaches the address is no longer read."""
    from core import db

    with db.transaction(conn):
        conn.execute("DELETE FROM own_mailbox")


def opt_in(conn: sqlite3.Connection, address: str, *, own_domain: bool = False) -> str:
    """An address whose mail can be the user's: the app's choice alone."""
    from core import db, modules

    address = canonical(address)
    if not _ADDRESS.fullmatch(address):
        raise modules.AccountError(f"not an address: {address!r}")
    with db.transaction(conn):
        conn.execute("INSERT INTO user_addresses (address, own_domain) VALUES (?, ?)"
                     " ON CONFLICT (address) DO UPDATE SET own_domain = excluded.own_domain",
                     (address, int(bool(own_domain))))
    return address


def opt_out(conn: sqlite3.Connection, address: str) -> None:
    from core import db

    with db.transaction(conn):
        conn.execute("DELETE FROM user_addresses WHERE address = ?", (canonical(address),))
