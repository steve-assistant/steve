"""Checks in code between the judge and delivery.

The judge is one cheap model call that reads a subject line and a snippet
and says whether the user should hear about it. Everything it got wrong between
24 September and 10 October 2026 was knowable in code, from data already
in the row, and nothing looked:

- **Who sent it.** Trey Research's "A Personal Invitation from the Trey Research CEO"
  (studies@treyresearch.qualtrics.com) was judged `never` as mail on 8
  October (observation 3762); the next day `sources/threads` raised the same
  message as "Marcus Bell wrote 2 days ago and you haven't replied"
  (observation 4385) and it went into the evening briefing with "Want me to
  draft a reply?". A Substack post (1677), a Litware info-session
  blast (2649) and Lena's "Hi Everyone" kickoff (271) became replies the user
  owed. 41 of the 68 mail items the user was told came from automated senders --
  Fabrikam's "Thank you for registering" twice (3467, 3468).
- **Which message it is.** One message is a `mail:` row, a `thread:` row
  and, for mail that reaches two mailboxes, a copy in the password mailbox; each was
  judged on its own, and 12 subjects were told twice.
- **Where it is.** At 23:03 on 8 October a hurricane warning for
  Ridgeview, Alabama woke them "where they are going" -- for a ropes course whose
  own address ended "Pittsburgh, PA 15222" (observations 4125/4126/4128/4143).
  The model's `critical` was the whole of the night rule.
- **Whether it is still true.** At 00:24 the user wrote "I'm in Pittsburgh"; the
  08:00 briefing still carried three Ridgeview items, because a verdict,
  once given, was never looked at again.

So each of these is a pure function with its reason in words, and the code
here applies them: when an item is judged (`after_judging`), before the
judge sees a repeat at all (`before_judging`), and again just before
anything is said (`recheck`, from `deliver` and `briefing`). The user's rule,
10 October 2026: automated mail is never "important" unless it changes their
plans. The night takes only what is still critical after these checks
(`deliver`).

What each check decided is kept in `verdicts` (`store.vetted`), so a dropped
item says why, and "why did you tell me that?" has an answer (`told.py`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from agent import heard, sqltime
from agent.attention import store
from agent.attention.store import Observation, is_mail
from core import modules

log = logging.getLogger("attention")

# The judge's reason, a fixed code: what makes it worth their attention. Fixed
# so that code can act on it -- a sentence cannot be checked.
REASONS = ("changes-plans", "reply-owed", "deadline", "money", "safety", "fyi")
# Reasons that put an item under "Needs you" in a briefing.
NEEDS_USER = frozenset(REASONS) - {"fyi"}


@dataclass(frozen=True)
class Check:
    """One check's answer, and why, in words the user could read."""

    ok: bool
    why: str


def _yes(why: str) -> Check:
    return Check(True, why)


def _no(why: str) -> Check:
    return Check(False, why)


# ---------------------------------------------------------------------------
# Who sent it
# ---------------------------------------------------------------------------

# Services that send for somebody else: newsletter and marketing senders,
# event invitations, surveys, social networks. A message from one is a
# notification, however personal its sender's name -- Trey Research's
# "Personal Invitation from the Trey Research CEO" came, signed by a person,
# from a survey service. Matched on the domain and any domain under it.
#
# Kept to services anyone's mail passes through. The list this replaced also
# held one inbox's senders (a lab's own domain, a campus's event and course
# systems, recruiting systems), which told every user that inbox's life, and
# a recruiting system relays a recruiter's own mail, which is owed a reply.
# What a list misses, the headers a mailing system adds and the address's
# shape (`people.looks_automated`: no-reply, notifications@, news.example.com)
# catch.
PLATFORM_DOMAINS = frozenset({
    "substack.com", "medium.com", "beehiiv.com", "convertkit.com",
    "mailchimp.com", "mcsv.net", "list-manage.com", "sendgrid.net", "mailgun.org",
    "constantcontact.com", "hubspotemail.net",
    "eventbrite.com", "evite.com", "meetup.com", "lu.ma",
    "qualtrics.com", "surveymonkey.com",
    "linkedin.com", "facebookmail.com",
})

# The kinds of sender an account's own sorting may name (`sorted_as`, in a
# message's mailing headers): Gmail's categories, as its module gives them.
# This file knew Gmail's labels itself until step 5's item 9.
SORTED = ("automated", "group")

# Addressed to this many people or more, it is a group's mail: a reply to it
# is not one the user owes.
GROUP_RECIPIENTS = 10

# A greeting to a crowd, at the start of the snippet: "Hi Everyone, We're
# excited to see you all at the kickoff" (Lena's kickoff blast, obs 271),
# "Hello All," (a course's notices), "Hello from Litware!" (obs 2649).
_GROUP_GREETING = re.compile(
    r"^\W*(?:hi|hello|hey|dear|greetings|good (?:morning|afternoon|evening))[\s,]+"
    r"(?:every(?:one|body)|all|y'?all|team|folks|friends|students|class(?:mates)?|"
    r"colleagues|community|members|guys|candidates|applicants)\b",
    re.IGNORECASE,
)
_ORG_HELLO = re.compile(r"^\W*(?:hello|hi|greetings|warm greetings) from\s+\S", re.IGNORECASE)


@dataclass(frozen=True)
class Sender:
    """What kind of sender a message has: `person`, `group` (a person writing
    to many, or a list), `automated` (nobody wrote it to them), or `unknown`
    (no address to judge)."""

    kind: str
    why: str

    @property
    def bulk(self) -> bool:
        return self.kind in ("automated", "group")


def _domain_platform(host: str) -> str | None:
    host = host.lower().strip(".")
    for domain in PLATFORM_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return None


def sender_class(
    address: str,
    name: str | None = None,
    *,
    headers: dict | None = None,
    snippet: str = "",
    recipients: int | None = None,
    listed_as_person: bool | None = None,
) -> Sender:
    """Who a message is from, by what is structural about it.

    Strongest first: the headers a mailing system adds (List-Unsubscribe,
    List-Id, Precedence, Auto-Submitted), the account's own sorting, then the
    address (no-reply, a platform's domain, the contact ledger's flag), then
    how many it went to and whether it greets a crowd. The address alone was
    the only test before (`people.looks_automated`), and a named sender at
    a platform passed it.
    """
    from tools import people

    headers = headers or {}
    auto = str(headers.get("auto_submitted") or "").strip().lower()
    if auto and auto != "no":
        return Sender("automated", f"Auto-Submitted: {auto}")
    precedence = str(headers.get("precedence") or "").strip().lower()
    if precedence in ("bulk", "junk"):
        return Sender("automated", f"Precedence: {precedence}")
    if headers.get("list_id"):
        return Sender("group", f"sent through a mailing list ({headers['list_id']})")
    if precedence == "list":
        return Sender("group", "Precedence: list")
    if headers.get("list_unsubscribe"):
        return Sender("automated", "a List-Unsubscribe header: a list the user can leave")
    if (sorted_as := str(headers.get("sorted_as") or "")) in SORTED:
        filed = str(headers.get("filed_under") or "").strip()[:40] or sorted_as
        return Sender(sorted_as, f"its account files it under {filed}")

    address = (address or "").strip().lower()
    if not address or "@" not in address:
        return Sender("unknown", "no sender address")
    if people.looks_automated(address, name):
        return Sender("automated", f"{address} is an address nobody reads")
    if platform := _domain_platform(address.rsplit("@", 1)[1]):
        return Sender("automated", f"sent through a platform ({platform})")
    if listed_as_person is False:
        return Sender("automated", f"the contact ledger has {address} as a list, not a person")
    if recipients is not None and recipients >= GROUP_RECIPIENTS:
        return Sender("group", f"addressed to {recipients} people")
    start = snippet.strip()[:80]
    if _GROUP_GREETING.search(start) or _ORG_HELLO.search(start):
        greeting = re.split(r"[,!.\n]", start, maxsplit=1)[0].strip()
        return Sender("group", f"it greets a crowd (\"{greeting}\")")
    return Sender("person", f"{address} writes as a person")


# What `sources/mail` puts before an email's own words: its account's name,
# whatever the user called it, and whether it was read.
_MAILBOX_PREFIX = re.compile(r"^[^,\n]{1,80}, (?:unread|already read)\.\s*")
_THREAD_PREFIX = re.compile(r"^[^;]*; a reply [^;]*; \d+ message\(s\) in the thread(?:;\s*)?")


def snippet_of(obs: Observation) -> str:
    """The message's own opening words, without what the source put first."""
    text = obs.detail or ""
    if obs.source == "threads":
        return _THREAD_PREFIX.sub("", text, count=1)
    return _MAILBOX_PREFIX.sub("", text, count=1)


def _from(obs: Observation) -> tuple[str, str | None]:
    from tools import people

    raw = obs.ref.get("with") if obs.source == "threads" else obs.ref.get("from")
    return people.parse_address(str(raw or ""))


def sender_of(conn: sqlite3.Connection | None, obs: Observation) -> Sender | None:
    """The sender class of a mail or thread item; None for anything else.

    A thread item carries no headers of its own (it is read from envelopes
    in bulk), so it borrows the ones its `mail:` twin was observed with."""
    if not is_mail(obs.source) and obs.source != "threads":
        return None
    address, name = _from(obs)
    headers, recipients = obs.ref.get("headers") or {}, obs.ref.get("recipients")
    listed = None
    if conn is not None and address:
        if obs.source == "threads" and not headers:
            for twin in _twins(conn, obs):
                ref = json.loads(twin["ref"] or "{}")
                if ref.get("headers"):
                    headers = ref["headers"]
                    recipients = ref.get("recipients", recipients)
                    break
        row = conn.execute("SELECT is_person FROM people WHERE email = ?",
                           (address,)).fetchone()
        listed = None if row is None else bool(row["is_person"])
    return sender_class(address, name, headers=headers,
                        snippet=snippet_of(obs), recipients=recipients,
                        listed_as_person=listed)


# A receipt for something the user did themselves: there is nothing in it to tell them.
_RECEIPT = re.compile(
    r"\b(?:thanks? (?:you )?for (?:registering|signing up|subscribing|"
    r"your (?:order|purchase|payment|registration|application|submission|rsvp))|"
    r"registration (?:is )?(?:confirmed|complete|received|successful)|"
    # Not "You're signed up to receive a weekly report": a digest's footer,
    # over a course site's report that carried a due date (obs 766).
    r"you(?:'re| are| have been) (?:registered|confirmed|signed up)(?! to receive)|"
    r"(?:you )?successfully registered|confirmed for\b|"
    r"(?:order|booking|reservation|payment) (?:confirmation|confirmed|received)|"
    r"your receipt|receipt for)",
    re.IGNORECASE,
)

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATED = re.compile(
    r"\b(?:today|tonight|tomorrow|(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"(?:" + "|".join(_MONTHS) + r")[a-z]*\.? \d{1,2}\b|\d{1,2} (?:"
    + "|".join(_MONTHS) + r")[a-z]*\b|\d{1,2}/\d{1,2}\b|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def dated(text: str) -> bool:
    """Whether a date is stated: a day of the week, a month and day, a
    numeric date, today or tomorrow. A bill with no date in view is not due."""
    return bool(_DATED.search(text or ""))


@dataclass(frozen=True)
class Change:
    """What a check does to a verdict: a new `say`, critical or reason."""

    why: str
    say: str | None = None
    critical: bool | None = None
    reason: str | None = None


def sender_rule(obs: Observation, sender: Sender | None, reason: str | None) -> Change | None:
    """The user's rule, 10 October 2026: automated mail is never "important"
    unless it changes their plans. In code, because the judge was told this in
    words and still put 41 automated items of 68 in front of them.

    - a thread from a bulk sender is never a reply the user owes (obs 4385, 1677,
      2649, 271);
    - an automated receipt for something the user did themselves is nothing (Fabrikam's
      "Thank you for registering", 3467/3468) -- unless the judge gave it a
      task, which then still runs;
    - other automated mail is FYI unless its reason is changes-plans or
      a payment with a stated date;
    - a group's mail never owes them a reply.
    """
    if sender is None or not sender.bulk:
        return None
    if obs.source == "threads":
        if obs.ref.get("waiting") == "owed by the user":
            return Change(f"a bulk sender is never a reply the user owes: {sender.why}",
                          say="never")
        return None
    # The message's own words, not the judge's headline: "Payment Reminder
    # ... is due on 10/07" got the headline "...; a payment confirmation
    # says a payment is processing", and read as a receipt (obs 2351).
    text = " ".join((obs.title, snippet_of(obs)))
    if sender.kind == "automated":
        if _RECEIPT.search(text) and not obs.task:
            return Change(f"a receipt for something the user did themselves, from {sender.why}",
                          say="never")
        if reason == "changes-plans":
            return None
        if reason == "money" and dated(text):
            return None
        return Change(f"automated mail is FYI unless it changes the user's plans ({sender.why})",
                      say="briefing" if obs.say in ("now", "before") else None,
                      critical=False, reason="fyi")
    if reason == "reply-owed":
        return Change(f"mail to many owes no reply ({sender.why})",
                      say="briefing" if obs.say == "now" else None, reason="fyi")
    return None


# ---------------------------------------------------------------------------
# Which message it is
# ---------------------------------------------------------------------------


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def _subject(text: str) -> str:
    return _plain(re.sub(r"^\s*(?:(?:re|fw|fwd|aw)\s*:\s*)+", "", text or "", flags=re.I))


def names(obs: Observation) -> set[str]:
    """Every name this item's message goes by: the mailbox's own id (a
    `mail:` row and the `thread:` row for the same message share it), the
    RFC Message-ID where the source read one, and a fingerprint of sender,
    subject and opening words -- what a password mailbox's copy and the Outlook original
    have in common when nothing else is (3159 and 3160, "RE: Thank You!",
    told twice in one evening briefing)."""
    if not is_mail(obs.source) and obs.source != "threads":
        return set()
    found: set[str] = set()
    if message := obs.ref.get("message_id"):
        found.add(f"msg:{message}")
    if rfc := obs.ref.get("rfc_id"):
        found.add(f"rfc:{str(rfc).strip().strip('<>').lower()}")
    address, _ = _from(obs)
    opening = _plain(snippet_of(obs))[:120]
    subject = _subject(str(obs.ref.get("subject") or ""))
    if address and subject and len(opening) >= 20:
        digest = hashlib.sha1(f"{address}|{subject}|{opening}".encode()).hexdigest()[:16]
        found.add(f"fp:{digest}")
    return found


# How far back a repeat is looked for. Threads are raised two to seven days
# after the message; the mail sources read two days back.
REPEAT_DAYS = 14


def _twins(conn: sqlite3.Connection, obs: Observation) -> list[sqlite3.Row]:
    """Earlier rows, judged, for the same message. Earlier by id, not just
    other: a row recorded after this one is not a verdict that stood when
    this one came in (and the replay, `replay.py`, walks rows in order)."""
    mine = names(obs)
    if not mine:
        return []
    rows = conn.execute(
        "SELECT o.*, v.reason AS reason, v.sender AS sender FROM observations o"
        " LEFT JOIN verdicts v ON v.obs_id = o.id"
        " WHERE (o.source LIKE 'mail:%' OR o.source = 'threads')"
        f"  AND {modules.live(conn, 'observations', 'o')}"
        "   AND o.id < ? AND o.triaged_at IS NOT NULL"
        "   AND o.first_seen >= datetime('now', ?) ORDER BY o.id",
        (obs.id if obs.id is not None else 2**62,
         f"-{REPEAT_DAYS} days")).fetchall()
    return [r for r in rows if _same_message(mine, obs, Observation.from_row(r))]


# A fingerprint (sender, subject, opening words) says "the same message" only
# for copies that arrive together: LinkedIn's "please verify your new device"
# and Google's "Security alert" open with the same words every time, and a
# new one ten days later is a new event (obs 1584 and 3724, 1037 and 3153).
FINGERPRINT_HOURS = 48


def _seen(obs: Observation) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(str(obs.first_seen))
    except ValueError:
        return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _same_message(mine: set[str], obs: Observation, other: Observation) -> bool:
    shared = names(other) & mine
    if any(not n.startswith("fp:") for n in shared):
        return True
    return bool(shared) and abs(_seen(obs) - _seen(other)) <= dt.timedelta(
        hours=FINGERPRINT_HOURS)


def _day(stamp: str | None) -> str:
    try:
        return dt.datetime.fromisoformat(str(stamp)).strftime("%a %d %b")
    except ValueError:
        return str(stamp or "")


def repeat(conn: sqlite3.Connection, obs: Observation) -> Change | None:
    """An earlier verdict on the same message stands unless its state changed.

    Told already: not told again. Judged `never`: still never (Trey Research, judged
    `never` as mail on 8 October, raised again as a thread on the 9th). Held
    for a briefing still to come: that row carries it. The one change of
    state that counts is time passing on a reply the user was told the user owes: a
    thread nudge after a told `reply-owed` from a person is judged afresh."""
    twins = _twins(conn, obs)
    if not twins:
        return None
    told = [t for t in twins if t["delivered_in"] not in (None, "dropped")]
    if told:
        last = told[-1]
        nudge = (obs.source == "threads" and obs.ref.get("waiting") == "owed by the user"
                 and last["reason"] == "reply-owed" and last["sender"] == "person")
        if nudge:
            return None
        return Change(f"already told in the {last['delivered_in']} on {_day(last['first_seen'])}"
                      f" (observation {last['id']}): the same message", say="never")
    nevers = [t for t in twins if t["say"] == "never"]
    if nevers:
        first = nevers[0]
        return Change(f"judged never as {first['source']} mail on {_day(first['first_seen'])}"
                      f" (observation {first['id']}): the same message", say="never")
    held = twins[0]
    return Change(f"observation {held['id']} already carries the same message", say="never")


# ---------------------------------------------------------------------------
# Where it is
# ---------------------------------------------------------------------------

_GOING = re.compile(r"in effect in (?P<p>.+?), where the user is going")
_THERE = re.compile(
    r" in (?P<p>[A-Z][\w .'-]+?) on \w{3} \w{3} \d{1,2}, while the user is there")
_AT_HOME = re.compile(r"at home \((?P<p>[^)]+)\)")


def place_of(obs: Observation) -> str | None:
    """Where a weather item is about: home, a trip's town, or what the
    title of a row written before places were kept says."""
    if obs.source != "weather":
        return obs.place
    if trip := obs.ref.get("trip"):
        return str(trip)
    if obs.ref.get("for") == "home" and obs.ref.get("home"):
        return str(obs.ref["home"])
    for pattern in (_AT_HOME, _GOING, _THERE):
        if match := pattern.search(obs.title or ""):
            return match.group("p").strip()
    return None


def _home_by_title(obs: Observation) -> bool:
    return bool(_AT_HOME.search(obs.title or ""))


def location(obs: Observation) -> Check:
    """A weather alert reaches them only for home or a verified trip.

    A trip is verified where it is found (`trips.verify`): a travel event --
    a flight, a hotel, several days -- or an address beyond `AWAY_KM` whose
    own ZIP or town agrees with the place it was geocoded to. The ropes
    course at "…Pittsburgh, PA 15222, United States" was one three-hour event
    whose ZIP said Pennsylvania, and became a trip to Ridgeview, Alabama."""
    if obs.source != "weather":
        return _yes("not about a place")
    kind = obs.key.split(":")[1] if obs.key.count(":") >= 1 else ""
    if kind not in ("alert", "trip"):
        return _yes("home's forecast")
    if obs.ref.get("for") == "home" or (not obs.ref.get("trip") and _home_by_title(obs)):
        return _yes(f"for home ({place_of(obs) or 'home'}): the alert is for home's point")
    trip = obs.ref.get("trip")
    if trip and obs.ref.get("trip_verified"):
        return _yes(f"a verified trip to {trip}: {obs.ref.get('trip_why') or 'verified'}")
    place = place_of(obs) or "a place"
    if trip:
        return _no(f"{place} is not a verified trip: "
                   f"{obs.ref.get('trip_why') or 'no travel event and no address that agrees'}")
    return _no(f"{place}: the user is not known to be there or going there (no trip was "
               "verified)")


# ---------------------------------------------------------------------------
# Whether it is still true
# ---------------------------------------------------------------------------

# Sources whose `when` is the thing itself -- an event's start, a due time,
# a forecast's day -- so that once it is past, so is the item.
_EVENT_SOURCES = frozenset({"calendar", "horizon", "deadlines", "weather"})
_UNTIL = re.compile(r"until (\d{4}-\d{2}-\d{2}T[\d:]+(?:[+-]\d{2}:\d{2}|Z)?)")


def _moment(text: str | None) -> dt.datetime | None:
    """A source's time as local naive, or None. A date alone is its end."""
    if not text:
        return None
    text = str(text).strip()
    try:
        if len(text) == 10:
            return dt.datetime.combine(dt.date.fromisoformat(text), dt.time(23, 59))
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def passed(obs: Observation, now: dt.datetime) -> Check:
    """The thing it is about has happened: the event started, the
    assignment came due, the alert ended, the forecast's day is over."""
    if obs.source not in _EVENT_SOURCES:
        return _yes("not about a moment")
    if obs.source == "weather" and obs.key.startswith("weather:alert:"):
        end = obs.ref.get("ends") or (m.group(1) if (m := _UNTIL.search(obs.detail)) else None)
        moment = _moment(end)
        if moment and moment <= now:
            return _no(f"the alert ended {moment:%a %H:%M}")
        return _yes("the alert is in effect")
    moment = _moment(obs.when)
    if moment and moment <= now:
        return _no(f"it has passed ({moment:%a %d %b %H:%M})")
    return _yes("still ahead")


@dataclass(frozen=True)
class Said:
    """Something the user said, and when (SQLite UTC)."""

    text: str
    at: str


# Their words that say something was wrong or is over. `issues.is_correction`
# catches "that's wrong", "not my", "why did you"; these are what it misses
# and an item needs: where the user is, that a thing is done, doubt.
# The claim is matched in any case, the place by its capitals: "I'm in
# Denver" is a place, "I'm in class" is not. (Matched lower-case only, it
# missed their own "I'm in Pittsburgh" of 9 October.)
_LOCATION_CLAIM = re.compile(
    r"\b(?i:i'?m|i am)\s+(?i:currently\s+|still\s+|back\s+)?(?i:in|at)\s+"
    r"(?P<place>[A-Za-z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,2})",
)
_DONE = re.compile(
    r"\b(?:is|are|it'?s|was) (?:done|finished|over|cancel+ed|submitted|handled)\b|"
    r"\bi (?:already |just )?(?:finished|did|submitted|completed|handled|sent)\b|"
    r"\balready (?:done|did|finished)\b",
    re.IGNORECASE,
)
_DOUBT = re.compile(
    r"\bare you sure\b|\bwhy do you think\b|\bthere(?:'?s| is) no\b|\bnot (?:going|there)\b",
    re.IGNORECASE,
)
# Words that say nothing about which item is meant.
_GENERIC = frozenset([
    "about", "after", "again", "alert", "also", "already", "anything", "around", "before",
    "being", "check", "could", "days", "draft", "email", "event", "events", "from", "going",
    "have", "haven", "into", "issued", "just", "likely", "local", "meeting", "message",
    "national", "reply", "replied", "service", "since", "statement", "still", "that", "their",
    "there", "these", "this", "today", "tomorrow", "tonight", "until", "want", "warning",
    "watch", "weather", "week", "what", "when", "where", "which", "while", "will", "with",
    "wrote", "your", "yours", "you'll", "you've", "effect", "committed", "today's", "monday",
    "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
    "march", "april", "june", "july", "august", "september", "october", "november", "december"
])


def _terms(text: str) -> set[str]:
    """Distinctive words: four letters or more, not generic."""
    return {w for w in re.findall(r"[a-z][a-z'-]{3,}", (text or "").lower())
            if w not in _GENERIC}


def _proper(text: str) -> set[str]:
    """Capitalised words in an item that are not just a sentence's first."""
    words = re.findall(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z'-]{2,})", text or "")
    return {w.lower() for w in words if w.lower() not in _GENERIC}


def _town(place: str) -> str:
    return _plain(place.split(",")[0])


def corrected(obs: Observation, said: list[Said]) -> Check:
    """The user has corrected it since it was judged.

    Two ways: the user said where they are, and it is about somewhere else ("I'm in
    Pittsburgh", 00:24 on 9 October, against three Ridgeview items held
    for the morning); or their words read as a correction, a doubt or "done"
    and name something the item names ("Solve is done", against "Tailspin
    Solve is due today")."""
    from agent import issues

    since = obs.triaged_at or obs.first_seen or ""
    place = place_of(obs)
    home = str(obs.ref.get("home") or "")
    item = " ".join((obs.title, obs.headline))
    proper = _proper(item) | ({_town(place)} if place else set())
    terms = _terms(item)
    for words in said:
        if words.at < since:
            continue
        text = issues.straight(words.text)
        if place:
            # A phone's curly apostrophe made straight, the case kept.
            for claim in _LOCATION_CLAIM.finditer(words.text.replace("’", "'")):
                claimed = claim.group("place").strip()
                if not (claimed[:1].isupper() or (home and _town(claimed) == _town(home))):
                    continue  # "I'm in class" is not a place
                if _town(claimed) and _town(claimed) not in _town(place) \
                        and _town(place) not in _town(claimed):
                    return _no(f"the user said \"{words.text.strip()[:80]}\" ({words.at} UTC);"
                               f" this is about {place}")
        pointed = bool(_DONE.search(text) or _DOUBT.search(text))
        if not (pointed or issues.is_correction(words.text)):
            continue
        mine = _terms(text)
        named = (mine & proper) | (mine & terms)
        if pointed:
            # "Solve is done": one name the item carries is enough.
            shared = (mine & proper) or (mine & terms if len(mine & terms) >= 2 else set())
        else:
            # A bare "that's wrong" is usually about something the agent did:
            # "you used the wrong email addresses ... the tailspin format"
            # (29 September) is not about a news story that names Tailspin.
            shared = named if len(named) >= 2 else set()
        if place and _town(place) in _plain(text):
            shared = shared or {_town(place)}
        if shared:
            return _no(f"the user corrected it: \"{words.text.strip()[:80]}\" ({words.at} UTC)")
    return _yes("not corrected")


# How far back their words are read for a correction, and the failure
# ledger's corrections.
SAID_HOURS = 48
ISSUE_HOURS = 24


def user_words(conn: sqlite3.Connection, since: str | None = None,
              until: str | None = None) -> list[Said]:
    """What the user said in their conversations since a moment (SQLite UTC, at most
    two days back), and the corrections the failure ledger noted in the
    last day. `until` (SQLite UTC) is now unless a replay says otherwise."""
    until = until or conn.execute("SELECT datetime('now')").fetchone()[0]
    floor = conn.execute("SELECT datetime(?, ?)", (until, f"-{SAID_HOURS} hours")).fetchone()[0]
    since = max(since or floor, floor)
    out = [Said(r["content"] or "", r["created_at"])
           for r in heard.user_messages(conn, since=since, until=until)]
    for row in conn.execute(
            "SELECT summary, last_seen FROM issues WHERE kind = 'correction'"
            " AND last_seen >= datetime(?, ?) AND last_seen <= ?",
            (until, f"-{ISSUE_HOURS} hours", until)):
        words = re.sub(r"^The user corrected it:\s*", "", row["summary"] or "")
        out.append(Said(words, row["last_seen"]))
    return out


def closed(obs: Observation, closed_refs: set[str]) -> Check:
    """The ledger has it closed: done, dropped or expired. Through
    `owed.closed_refs`, over the commitments until the obligations ledger
    lands."""
    commitment = obs.ref.get("commitment_id")
    if commitment is not None and f"commitment:{commitment}" in closed_refs:
        return _no(f"commitment {commitment} is closed")
    return _yes("nothing closed it")


def fresh(obs: Observation, now: dt.datetime, *, said: list[Said],
          closed_refs: set[str]) -> Check:
    """Still worth saying: not passed, not corrected, not closed. Staleness
    was by age alone (`briefing.STALE_AFTER`), so a correction made an hour
    before a briefing changed nothing in it."""
    for check in (passed(obs, now), corrected(obs, said), closed(obs, closed_refs)):
        if not check.ok:
            return check
    return _yes("still current")


# ---------------------------------------------------------------------------
# Whether it is already done
# ---------------------------------------------------------------------------

# Drafts and mail sent through Steve, by the tools that write them.
_WRITING = ("draft_email", "send_email")


def written_to(conn: sqlite3.Connection, obs: Observation) -> Check:
    """For a meeting that ended (`after`): whether the user has written to
    someone who was in it since it began -- a draft or a mail through Steve,
    or mail they sent themselves -- and so has already followed it up.

    The judge was told this in words, as one incident's rule: "no 'want a
    thank-you note?' after you wrote one". On 1 October 2026 it asked how a
    coffee chat went and offered the note nine minutes after the agent had
    drafted it. The draft was in the database; nothing looked."""
    from core import mailbox
    from tools import people

    if obs.source != "after":
        return _yes("not a meeting that ended")
    guests = {mailbox.canonical(people.parse_address(str(g))[0])
              for g in obs.ref.get("people") or []} - {""}
    began = _moment(obs.ref.get("start"))
    if not guests or began is None:
        return _yes("nobody to have written to")
    for row in conn.execute(
            f"SELECT tool, args_json FROM actions WHERE tool IN ({', '.join('?' * len(_WRITING))})"
            " AND status IN ('pending', 'approved', 'executing', 'done')"
            f" AND {modules.live(conn, 'actions')}"
            " AND created_at >= ?", (*_WRITING, sqltime.stamp(began))):
        try:
            to = str(json.loads(row["args_json"] or "{}").get("to") or "")
        except ValueError:
            continue
        for address, _ in people.parse_addresses(to):
            if mailbox.canonical(address) in guests:
                return _no(f"a {row['tool'].replace('_', ' ')} to {address} since it began")
    marks = ", ".join("?" * len(guests))
    for row in conn.execute(
            "SELECT p.email, i.occurred_at FROM interactions i JOIN people p ON p.id = i.person_id"
            f" WHERE i.kind = 'email' AND i.direction = 'sent' AND p.email IN ({marks})"
            f"  AND {modules.live(conn, 'interactions', 'i')}",
            tuple(guests)):
        if (sent := _moment(row["occurred_at"])) and sent >= began:
            return _no(f"the user wrote to {row['email']} since it began")
    return _yes("nobody in it written to since")


# ---------------------------------------------------------------------------
# Applying them
# ---------------------------------------------------------------------------


def _settle(conn: sqlite3.Connection, obs: Observation, why: str) -> None:
    """Never, without asking the judge: the checks already know."""
    store.judged(conn, obs.id, say="never", critical=False, headline="", task="",
                 deliver_at=None)
    sender = sender_of(conn, obs)
    store.vetted(conn, obs.id, outcome="dropped", why=why,
                 sender=sender.kind if sender else None,
                 sender_why=sender.why if sender else None)


def before_judging(conn: sqlite3.Connection, batch: list[Observation]) -> list[Observation]:
    """What the judge need not see: a message it has already judged, or
    that this look holds twice, a thread from a bulk sender, and a meeting
    the user has already followed up. Settled as `never` here, with the
    reason; the rest are returned for the judge."""
    left: list[Observation] = []
    seen: dict[str, int] = {}
    for obs in batch:
        mine = names(obs)
        if twin := next((seen[n] for n in mine if n in seen), None):
            _settle(conn, obs, f"observation {twin} carries the same message, from the same look")
            continue
        if change := repeat(conn, obs):
            _settle(conn, obs, change.why)
            continue
        if obs.source == "threads" and obs.ref.get("waiting") == "owed by the user":
            sender = sender_of(conn, obs)
            if sender is not None and sender.bulk:
                _settle(conn, obs, f"a bulk sender is never a reply the user owes: {sender.why}")
                continue
        if obs.source == "after" and obs.ref.get("follow_up") == "not worth asking":
            _settle(conn, obs, "a follow-up judged not worth asking: "
                    + str(obs.ref.get("follow_up_why") or "no reason given"))
            continue
        if not (followed := written_to(conn, obs)).ok:
            _settle(conn, obs, f"already followed up: {followed.why}")
            continue
        for name in mine:
            seen[name] = obs.id
        left.append(obs)
    return left


def _vet_one(conn: sqlite3.Connection, obs: Observation, now: dt.datetime,
             *, closed_refs: set[str]) -> None:
    say, critical, reason = obs.say, obs.critical, obs.reason
    changes: list[str] = []
    sender = sender_of(conn, obs)
    if say != "never" and (change := sender_rule(obs, sender, reason)):
        changes.append(change.why)
        say = change.say or say
        critical = critical if change.critical is None else change.critical
        reason = change.reason or reason
    if say != "never" and not (where := location(obs)).ok:
        changes.append(where.why)
        say = "never"
    # Their words since it was judged are none yet: it was judged just now.
    if say != "never" and not (current := fresh(obs, now, said=[],
                                                closed_refs=closed_refs)).ok:
        changes.append(current.why)
        say = "never"
    if say == "never":
        critical = False  # nothing dropped stays critical (the night reads it)
    if say == "never" and obs.say != "never":
        outcome = "dropped"
    elif reason == "fyi" and changes:
        outcome = "fyi"
    else:
        outcome = "kept"
    store.vetted(
        conn, obs.id, outcome=outcome,
        why="; ".join(changes) or "passed the checks",
        say=say if say != obs.say else None,
        critical=critical if critical != obs.critical else None,
        reason=reason if reason != obs.reason else None,
        sender=sender.kind if sender else None, sender_why=sender.why if sender else None,
        place=place_of(obs),
    )


def after_judging(conn: sqlite3.Connection, ids: list[int],
                  now: dt.datetime | None = None) -> None:
    """Check what the judge just said about these, and keep what came of it."""
    from agent.attention import owed

    now = now or dt.datetime.now()
    refs = owed.closed_refs(conn)
    for obs in store.get(conn, ids):
        try:
            _vet_one(conn, obs, now, closed_refs=refs)
        except Exception:  # noqa: BLE001 - an unchecked item is held, not lost or sent
            log.exception("could not check observation %s; held for the briefing", obs.id)
            store.vetted(conn, obs.id, outcome="held", why="the checks failed; held",
                         say="briefing" if obs.say == "now" else None, critical=False)


def recheck(
    conn: sqlite3.Connection, items: list[Observation], now: dt.datetime
) -> tuple[list[Observation], list[tuple[Observation, str]]]:
    """Just before anything is said: check what was never checked (judged
    before the checks existed), and whether each is still current against
    their words since it was judged. What is stale is dropped there and then,
    with its reason. Returns what may still be said, and what was dropped."""
    from agent.attention import owed

    kept: list[Observation] = []
    dropped: list[tuple[Observation, str]] = []
    unvetted = [o.id for o in items if o.vetted is None]
    if unvetted:
        after_judging(conn, unvetted, now)
        items = store.get(conn, [o.id for o in items])
        for obs in items:
            if obs.say == "never":
                row = conn.execute("SELECT vet_why FROM verdicts WHERE obs_id = ?",
                                   (obs.id,)).fetchone()
                dropped.append((obs, (row["vet_why"] if row else None) or "dropped"))
        items = [o for o in items if o.say != "never"]
    if not items:
        return kept, dropped
    since = min((o.triaged_at or o.first_seen or "") for o in items) or None
    said = user_words(conn, since)
    refs = owed.closed_refs(conn)
    for obs in items:
        current = fresh(obs, now, said=said, closed_refs=refs)
        if current.ok:
            # Asked about once the user may have written to them since.
            current = written_to(conn, obs)
        if current.ok:
            kept.append(obs)
            continue
        store.vetted(conn, obs.id, outcome="dropped", why=current.why, say="never",
                     critical=False)
        dropped.append((obs, current.why))
    return kept, dropped
