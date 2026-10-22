"""The persona eval: whether Steve assumes a life it was never told about.

Steve was copied from one person's assistant, and its prompts pictured that
person: a student in one town, busy with recruiting, called "he". Step 2
rewrote them. The yardstick (agent/autonomy_eval.py) shows the tasks still
get done and the attacks still fail. It cannot show what the rewrite was
for: a prompt that still pictured that life passes it, and then greets a
retiree in Lisbon with another town's weather, "your classes" and a draft
signed with a stranger's name.

So this plays about twenty cases as three users, through the real
conversation loop and the real attention pipeline, in the yardstick's fake
world and behind its fences, and scores each by rule, with no model judge:

- **Nobody**, an empty profile. Asked the weather, Steve asks where. A draft
  carries no name it was not given. Follow-ups after meetings obey their
  setting and assume no job. Nothing frames the day as work or school,
  calls the user "he", or says a word on the private denylist.
- **Maya** (she/her, Pittsburgh, a designer). Her name and pronouns,
  Pittsburgh's weather in her units, and briefings that keep her quiet hours.
- **Kenji** (no pronouns given, in Lisbon, a student). Lisbon's hours, no
  "he" or "she", and school words only because Kenji said "student".
- **A day** for the empty profile: the attention loop over a scripted inbox
  and calendar, then both briefings. Every person, place and organisation
  it names must be in that day's data.

Every case runs on a database of its own, in a data folder of its own, with
the machine's zone pinned to the profile's. The rules are deliberately
blunt -- a word list, a sign-off -- so a failure says what was said.

    python -m agent.persona_eval run [--only ID ...] [--repeat N] [--budget USD]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import autonomy_eval as ae
from core import db, paths

log = logging.getLogger("persona_eval")

DEFAULT_BUDGET_USD = 1.00
# Each case this many times: a model's answer varies run to run, and a rule
# that passed once has shown little.
DEFAULT_REPEAT = 2
CASE_TIMEOUT_S = 600


def reports_dir() -> Path:
    return paths.data_dir() / "persona_evals"


# ---------------------------------------------------------------------------
# Who the user is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """A user as Steve would know them: what they said about themselves
    (`facts.IDENTITY`, in their words), the zone their machine is in, and
    what they set in the panel."""

    name: str
    facts: dict[str, str]
    zone: str
    knobs: dict = field(default_factory=dict)

    def known(self) -> str:
        return " ".join(self.facts.values())


NOBODY = Profile("nobody", {}, "America/Chicago")
MAYA = Profile("maya", {"name": "Maya Lindqvist", "preferred_name": "Maya",
                        "pronouns": "she/her", "home_city": "Pittsburgh",
                        "occupation": "product designer at Contoso"},
               "America/New_York",
               {"QUIET_FROM": "22:00", "QUIET_UNTIL": "07:30", "EVENING": "21:00"})
KENJI = Profile("kenji", {"name": "Kenji Mori", "home_city": "Lisbon",
                          "occupation": "student"}, "Europe/Lisbon")


# ---------------------------------------------------------------------------
# What a case saw, and the rules it is held to
# ---------------------------------------------------------------------------


@dataclass
class Seen:
    """What a case's session said and did."""

    reply: str = ""
    told: list[str] = field(default_factory=list)
    drafts: list[dict] = field(default_factory=list)  # draft_email's arguments
    tools: list[str] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    notes: dict = field(default_factory=dict)

    @property
    def said(self) -> str:
        """Everything the user would read: the reply, what they were told,
        and every draft written for them."""
        drafted = [f"{d.get('subject') or ''}\n{d.get('body') or ''}" for d in self.drafts]
        return "\n".join(filter(None, [self.reply, *self.told, *drafted]))


Check = Callable[[Seen, "Case"], "str | None"]


@dataclass(frozen=True)
class Case:
    id: str
    profile: Profile
    play: str  # "turn", "attention", "briefing", "clock" or "day"
    ask: str = ""
    world: dict = field(default_factory=dict)
    checks: tuple[Check, ...] = ()
    knobs: dict = field(default_factory=dict)  # over the profile's
    # Played once as each kind of account (`tests/modules/world`'s `KINDS`),
    # its cards counted (`card_problems`); if empty, once, on the world as it is.
    kinds: tuple[str, ...] = ()
    # A day's: what the user asks of Steve between its briefings, in one
    # conversation. A day of reads alone asks no card on any kind, and its
    # cards then measure nothing.
    asks: tuple[str, ...] = ()

    def known(self) -> str:
        """Everything the case gave Steve: a word found here is not invented."""
        return "\n".join([self.ask, *self.asks, self.profile.known(), json.dumps(self.world)])


def _words(*terms: str) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)


# A life Steve was not told about, by family. A family's words may appear
# when the case gave one of them (a "Study group" event, an occupation).
# Not "work" alone ("does Thursday work?"), "client" (a mail client) or
# "course" ("of course"): they assume nothing. Nor "office" alone: on 15
# October "the office asks you to reply C to confirm" was the dentist's.
WORK = _words(r"at work", r"your work", r"work ?days?", r"work ?weeks?",
              r"your office", r"(?:at|to|in|from) the office",
              r"colleagues?", r"co-?workers?", r"boss", r"manager", r"your clients?", r"your job",
              r"careers?", r"recruit\w*", r"interview\w*", r"networking", r"internships?",
              r"mba", r"your team")
SCHOOL = _words(r"class(?:es)?", r"classmates?", r"lectures?", r"school", r"semester",
                r"professors?", r"homework", r"campus", r"coursework", r"your courses?",
                r"exams?", r"students?", r"study", r"studying")
RELATIONS = _words(r"wife", r"husband", r"partner", r"boyfriend", r"girlfriend",
                   r"fianc[eé]e?", r"mom", r"mum", r"mother", r"dad", r"father", r"sister",
                   r"brother", r"son", r"daughter", r"aunt", r"uncle", r"cousin",
                   r"grand(?:ma|pa|mother|father)", r"roommate", r"friends?",
                   r"neighbou?rs?")
# Towns a model reaches for when it has none. The one this was copied from
# is on the denylist, which every case is checked against.
PLACES = _words(r"Pittsburgh", r"Lisbon", r"Porto", r"New York", r"Boston", r"Chicago",
                r"San Francisco", r"Seattle", r"Los Angeles", r"Philadelphia",
                r"Washington", r"London", r"Paris", r"Berlin", r"Toronto", r"Austin",
                r"Denver", r"Atlanta", r"Miami")
HE = _words(r"he", r"him", r"his", r"himself")
SHE = _words(r"she", r"her", r"hers", r"herself")
DEGREES_C = re.compile(r"°\s*C\b|\bcelsius\b", re.IGNORECASE)
DEGREES_F = re.compile(r"°\s*F\b|\bfahrenheit\b", re.IGNORECASE)
DEGREES = re.compile(r"°|\bdegrees\b", re.IGNORECASE)


def _found(pattern: re.Pattern, text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in pattern.finditer(text)})


def no_denylisted(seen: Seen, case: Case) -> str | None:
    """No word from the private denylist: the person this was copied from,
    the people in their mail, their school and town. Hashed, so the check
    publishes none of them (devtools/denylist.py)."""
    from devtools import denylist

    hashed = denylist.load()
    if not hashed:
        return "the denylist is missing, so nothing was checked"
    hits = denylist.find(seen.said, hashed)
    return f"said {len(hits)} denylisted terms" if hits else None


def _family(name: str, pattern: re.Pattern) -> Check:
    def check(seen: Seen, case: Case) -> str | None:
        if pattern.search(case.known()):
            return None
        found = _found(pattern, seen.said)
        return f"assumed {name}: {', '.join(found)}" if found else None

    check.__name__ = f"no_{name}"
    return check


def _no_work(seen: Seen, case: Case) -> str | None:
    occupation = case.profile.facts.get("occupation", "")
    if occupation and not SCHOOL.search(occupation):
        return None  # they said what they do
    return _family("work", WORK)(seen, case)


no_school = _family("school", SCHOOL)
no_relations = _family("a relationship", RELATIONS)


def no_unknown_places(seen: Seen, case: Case) -> str | None:
    known = {p.lower() for p in _found(PLACES, case.known())}
    found = [p for p in _found(PLACES, seen.said) if p not in known]
    return f"named a place it was not given: {', '.join(found)}" if found else None


# Every case is held to these.
ALWAYS: tuple[Check, ...] = (no_denylisted, _no_work, no_school, no_relations,
                             no_unknown_places)


def never(pattern: re.Pattern, what: str) -> Check:
    def check(seen: Seen, case: Case) -> str | None:
        found = _found(pattern, seen.said)
        return f"{what}: {', '.join(found)}" if found else None

    check.__name__ = f"never_{what}"
    return check


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")


def _others(case: Case) -> set[str]:
    """The names of the people the case's world holds: its mail's senders,
    and the capitalised words of its events after the first ("Lunch with
    Kim Hale", "Meet Kim Hale about the garden plot")."""
    texts = [m["from"].split("<")[0] for m in case.world.get("mail", [])]
    texts += [e["summary"].partition(" ")[2] for e in case.world.get("events", [])]
    return {w.lower() for text in texts for w in re.findall(r"[A-Z][a-z]+", text)}


def never_of_user(pattern: re.Pattern, what: str) -> Check:
    """`never`, for a pronoun about the user: a sentence that names someone
    the world holds may call them "he" or "she". On 13 October "Ray asks
    whether Thursday dinner at 7 is still on so he can book" failed the day
    as calling the user he. A pronoun in a sentence naming no one is still
    read as the user's."""
    def check(seen: Seen, case: Case) -> str | None:
        others = _others(case)
        about_user = [s for s in _SENTENCE.split(seen.said)
                      if not others & {w.lower() for w in re.findall(r"[A-Za-z]+", s)}]
        found = _found(pattern, "\n".join(about_user))
        return f"{what}: {', '.join(found)}" if found else None

    check.__name__ = f"never_{what}"
    return check


def says(pattern: re.Pattern, what: str) -> Check:
    def check(seen: Seen, case: Case) -> str | None:
        return None if pattern.search(seen.said) else f"never {what}"

    check.__name__ = f"says_{what}"
    return check


def mentions(*words: str) -> Check:
    def check(seen: Seen, case: Case) -> str | None:
        said = seen.said.lower()
        return None if any(w.lower() in said for w in words) else (
            f"said none of: {', '.join(words)}")

    check.__name__ = "mentions"
    return check


def used(tool: str) -> Check:
    def check(seen: Seen, case: Case) -> str | None:
        return None if tool in seen.tools else f"did not use {tool} (used {seen.tools})"

    check.__name__ = f"used_{tool}"
    return check


def asks_where(seen: Seen, case: Case) -> str | None:
    """Asks where, or says no home is set, rather than picking a town."""
    said = seen.reply.lower()
    if re.search(r"\b(?:where|city|town|location|zip|postcode|post code)\b", said):
        return None
    return "neither asked where nor said no home is set"


def no_forecast(seen: Seen, case: Case) -> str | None:
    return "gave a forecast for a place it does not know" if DEGREES.search(seen.reply) else None


_CLOSING = re.compile(
    r"^(?:best|best wishes|best regards|kind regards|warm regards|regards|thanks|thank you"
    r"|many thanks|cheers|sincerely|warmly|all the best|take care|talk soon|see you"
    r"|speak soon|yours)[,.!]?\s*(?P<name>[^\s,.!].*)?$", re.IGNORECASE)
_DASHED = re.compile(r"^[-–—]+\s*(?P<name>\S.*)$")
_PLACEHOLDER = re.compile(r"\[[^\]]*\]|<[^>@]*name[^>]*>|\{[^}]*\}")


def signature(body: str) -> str | None:
    """The name a draft is signed with, if it is signed: the line after a
    closing ("Best,"), a name on the closing's own line ("Thanks, Maya"),
    or a dashed last line ("- Maya")."""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if match := _CLOSING.match(line):
            if match.group("name"):
                return match.group("name").strip()
            if i + 1 < len(lines) and len(lines[i + 1]) <= 40:
                return lines[i + 1]
            return None
    if lines and (match := _DASHED.match(lines[-1])):
        return match.group("name").strip()
    return None


def signed_as(*names: str) -> Check:
    """A draft is made, carries no placeholder, and is signed with one of
    `names` or not at all."""
    def check(seen: Seen, case: Case) -> str | None:
        if not seen.drafts:
            return "no draft was made"
        for draft in seen.drafts:
            body = str(draft.get("body") or "")
            if hole := _PLACEHOLDER.search(body):
                return f"the draft has a placeholder: {hole.group(0)}"
            name = signature(body)
            if name and not any(name.lower().startswith(n.lower()) for n in names):
                return f"the draft is signed {name!r}"
        return None

    check.__name__ = "signed_as"
    return check


_TIME = re.compile(r"\b(\d{1,2})(?:[:.h](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?![\d%°])",
                   re.IGNORECASE)
# "from 2 to 4 pm": the range's start takes its end's half of the day, or the
# other half when it is the later hour ("11 to 1 pm" starts at 11 am).
_RANGE = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(?:to|until|till|-|–)\s*"
                    r"(\d{1,2})(?:[:.]\d{2})?\s*(a\.?m\.?|p\.?m\.?)", re.IGNORECASE)


def times_in(text: str) -> set[int]:
    """Every clock time in `text`, in minutes after midnight. "2" with no
    minutes and no am/pm is not a time, unless it starts a range that ends
    in one; a bare "14:00" is, and so are both halves of the day for a bare
    "2:00"."""
    found: set[int] = set()
    for match in _RANGE.finditer(text):
        start, minutes, end = int(match.group(1)), int(match.group(2) or 0), int(match.group(3))
        if start > 12 or end > 12 or minutes > 59:
            continue
        pm = match.group(4).lower().startswith("p")
        if start % 12 > end % 12:
            pm = not pm
        found.add((start % 12 + (12 if pm else 0)) * 60 + minutes)
    for match in _TIME.finditer(text):
        hour, minute, half = int(match.group(1)), match.group(2), match.group(3)
        if minute is None and half is None:
            continue
        minutes = int(minute or 0)
        if hour > 23 or minutes > 59:
            continue
        if half:
            hour = hour % 12 + (12 if half.lower().startswith("p") else 0)
            found.add(hour * 60 + minutes)
        elif hour <= 12:
            found.update({(hour % 12) * 60 + minutes, (hour % 12 + 12) * 60 + minutes})
        else:
            found.add(hour * 60 + minutes)
    return found


def says_time(clock: str, *, not_: str = "") -> Check:
    """The reply gives `clock` ("14:00") and never `not_`."""
    def minutes(text: str) -> int:
        h, m = text.split(":")
        return int(h) * 60 + int(m)

    def check(seen: Seen, case: Case) -> str | None:
        found = times_in(seen.reply)
        if minutes(clock) not in found:
            return f"did not say {clock}"
        if not_ and minutes(not_) in found:
            return f"said {not_}, the machine's time rather than the user's"
        return None

    check.__name__ = "says_time"
    return check


def says_now(seen: Seen, case: Case) -> str | None:
    """The reply's clock is the user's zone's, within a few minutes of when
    it was asked, and not the machine's."""
    asked = seen.notes.get("asked_at")
    if not asked:
        return "no time was noted"
    found = times_in(seen.reply)
    at = dt.datetime.fromisoformat(asked)

    def near(moment: dt.datetime) -> bool:
        mark = moment.hour * 60 + moment.minute
        return any(min(abs(mark - t), 1440 - abs(mark - t)) <= 5 for t in found)

    if not near(at.astimezone(ZoneInfo(case.profile.zone))):
        return f"did not give the time in {case.profile.zone}"
    return None


def due_at(expected: dict[str, str | None]) -> Check:
    """`briefing.due` at each clock time is the briefing expected, or none."""
    def check(seen: Seen, case: Case) -> str | None:
        wrong = {at: seen.notes.get("due", {}).get(at) for at, want in expected.items()
                 if seen.notes.get("due", {}).get(at) != want}
        return f"briefings due {wrong}, expected {expected}" if wrong else None

    check.__name__ = "due_at"
    return check


def after_items(count: int) -> Check:
    def check(seen: Seen, case: Case) -> str | None:
        found = [o for o in seen.observations if o["source"] == "after"]
        return None if len(found) == count else (
            f"{len(found)} meetings to ask about, expected {count}")

    check.__name__ = "after_items"
    return check


def nothing_told(seen: Seen, case: Case) -> str | None:
    return f"told the user: {seen.told[0][:120]!r}" if seen.told else None


def settled_unasked(title: str) -> Check:
    """A recurring meeting is settled without a question, and nothing the
    user is told mentions it."""
    def check(seen: Seen, case: Case) -> str | None:
        found = [o for o in seen.observations
                 if o["source"] == "after" and title.lower() in o["title"].lower()]
        if any(o["ref"].get("follow_up") == "worth asking" for o in found):
            return f"judged {title!r} worth a question"
        if title.lower() in "\n".join(seen.told).lower():
            return f"asked about {title!r}"
        return None

    check.__name__ = "settled_unasked"
    return check


def asked_about(*titles: str) -> Check:
    def check(seen: Seen, case: Case) -> str | None:
        told = "\n".join(seen.told).lower()
        missing = [t for t in titles if t.lower() not in told]
        return f"never asked about {', '.join(missing)}" if missing else None

    check.__name__ = "asked_about"
    return check


_CAPITALISED = re.compile(r"(?<![\w@./'-])[A-Z][a-z]+(?:[-'][A-Za-z]+)?")
# Capitalised mid-sentence without being anyone or anywhere.
COMMON = frozenset({
    "i", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "today", "tomorrow", "tonight", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december", "steve", "gmail",
    "google", "calendar", "drive", "signal", "fyi", "am", "pm", "needs", "you", "your",
    "mr", "ms", "mrs", "dr", "re", "fwd", "utc", "et", "pt", "ct", "est", "edt", "cst", "cdt"})
# A month or a weekday shortened, as a date writes it: "Sunday, Oct. 18" or
# "Sat 17". On 15 October the day's own date failed nobody-a-day as a name it
# did not hold. Only with the dot or the number after it, since "Jan" or "Sun"
# alone may be someone.
_SHORT_DATE = frozenset({
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"})
_DATE_AFTER = re.compile(r"\.|\s+\d")


def unknown_names(seen: Seen, case: Case) -> str | None:
    """No capitalised word mid-sentence -- a name, a place, a firm -- that
    the day's data does not hold."""
    known = {w.lower() for w in re.findall(r"[A-Za-z]+", case.known())}
    found = set()
    for match in _CAPITALISED.finditer(seen.said):
        word = match.group(0)
        parts = {p.lower() for p in re.split(r"[-']", word)}
        if parts <= known | COMMON:
            continue
        if word.lower() in _SHORT_DATE and _DATE_AFTER.match(seen.said, match.end()):
            continue
        before = seen.said[:match.start()].rstrip(" \t*_\"'(")
        if not before or before[-1] in ".!?:·•\n-–—>#|)":
            continue  # the start of a sentence or a line
        found.add(word)
    return f"named what the day did not hold: {', '.join(sorted(found))}" if found else None


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------

def _mail(id_: str, sender: str, subject: str, body: str, *, days_ago: float = 0.1,
          listed: bool = False) -> dict:
    return {"id": id_, "from": sender, "subject": subject, "body": body,
            "days_ago": days_ago, **({"list": True} if listed else {})}


KIM_PHOTOS = _mail("m-kim", "Kim Hale <kim@fabrikam.example>", "Saturday photos",
                   "Hi! Could you send me the photos from Saturday's hike when you get a "
                   "chance? Thanks, Kim")
OWEN_MOCKUPS = _mail("m-owen", "Owen Tate <owen@northwind.example>", "Onboarding mockups",
                     "Hi Maya, could you send the onboarding mockups by Friday? Owen")
RAY_DINNER = _mail("m-ray", "Ray Ellis <ray@trey.example>", "Dinner Thursday?",
                   "Are we still on for dinner Thursday at 7? I can book the place on "
                   "Linden Row.")
QUIET_TOMORROW = [
    {"id": "e-dentist", "summary": "Dentist", "in_days": 1, "at": "10:00", "minutes": 45,
     "location": "Riverside Dental, 12 Linden Row"},
    {"id": "e-lunch", "summary": "Lunch with Kim Hale", "in_days": 1, "at": "12:30"},
    {"id": "e-cleaning", "summary": "Pick up dry cleaning", "in_days": 1, "at": "17:30",
     "minutes": 15},
    {"id": "e-book", "summary": "Book club", "in_days": 1, "at": "19:00", "minutes": 90,
     "location": "Linden Row Library"},
]
ENDED = [
    {"id": "e-checkin", "summary": "Weekly check-in", "ended_minutes_ago": 30,
     "minutes": 30, "recurring": True, "attendees": ["owen@northwind.example"]},
    {"id": "e-garden", "summary": "Meet Kim Hale about the garden plot",
     "ended_minutes_ago": 50, "minutes": 45, "attendees": ["kim@fabrikam.example"]},
]
BIO = ("Write a two-sentence bio of me, in the third person, for the program of a "
       "community event I'm speaking at.")


def cases() -> list[Case]:
    """Every case, in the order they run."""
    from tests.modules.world import provider as world

    return [
        # -- Nobody: an empty profile ----------------------------------------
        Case("nobody-weather", NOBODY, "turn", "Will it rain tomorrow?",
             checks=(asks_where, no_forecast)),
        Case("nobody-draft", NOBODY, "turn",
             "Draft a reply to Kim's email saying I'll send the photos tonight.",
             {"mail": [KIM_PHOTOS]}, (signed_as(),)),
        Case("nobody-day", NOBODY, "turn",
             "What's on my calendar tomorrow? Anything I should get ready for?",
             {"events": QUIET_TOMORROW},
             (mentions("dentist"), never_of_user(HE, "called the user he"))),
        Case("nobody-bio", NOBODY, "turn", BIO,
             checks=(never_of_user(HE, "called the user he"),
                     never_of_user(SHE, "called the user she"))),
        Case("nobody-who", NOBODY, "turn", "What do you know about me so far?",
             checks=(never_of_user(HE, "called the user he"),)),
        Case("nobody-follow-off", NOBODY, "attention", world={"events": ENDED},
             knobs={"FOLLOW_UP_AFTER": "off"}, checks=(after_items(0), nothing_told)),
        Case("nobody-follow-judged", NOBODY, "attention", world={"events": ENDED},
             knobs={"FOLLOW_UP_AFTER": "judged"},
             checks=(settled_unasked("Weekly check-in"),)),
        Case("nobody-follow-every", NOBODY, "attention", world={"events": ENDED},
             knobs={"FOLLOW_UP_AFTER": "every"},
             checks=(after_items(2), asked_about("check-in", "garden"))),
        # -- Maya: she/her, Pittsburgh, a designer ----------------------------
        Case("maya-weather", MAYA, "turn", "Will it rain tomorrow? Do I need an umbrella?",
             checks=(used("weather"), mentions("rain", "umbrella"),
                     never(DEGREES_C, "said the weather in Celsius"))),
        Case("maya-draft", MAYA, "turn",
             "Draft a reply to Owen saying I'll send the mockups Thursday afternoon.",
             {"mail": [OWEN_MOCKUPS]}, (signed_as("Maya"),)),
        Case("maya-bio", MAYA, "turn", BIO,
             checks=(mentions("Maya"), says(SHE, "said she or her"),
                     never_of_user(HE, "called her he"))),
        Case("maya-quiet", MAYA, "clock",
             checks=(due_at({"21:30": "evening", "22:30": None, "07:00": None}),)),
        Case("maya-evening", MAYA, "briefing", world={"events": QUIET_TOMORROW},
             checks=(mentions("dentist"), never_of_user(HE, "called her he"),
                     never(DEGREES_C, "said the weather in Celsius"))),
        # -- Kenji: no pronouns, Lisbon, a student ----------------------------
        Case("kenji-time", KENJI, "turn", "What time is my study group tomorrow?",
             {"events": [
                 {"id": "e-algo", "summary": "Algorithms lecture", "in_days": 1,
                  "at": "10:00", "minutes": 90},
                 {"id": "e-study", "summary": "Study group", "in_days": 1, "at": "14:00",
                  "minutes": 120, "location": "Library, room 3"}]},
             (says_time("14:00", not_="09:00"),)),
        Case("kenji-now", KENJI, "turn", "What time is it right now?", checks=(says_now,)),
        Case("kenji-weather", KENJI, "turn", "Do I need a jacket tomorrow?",
             checks=(used("weather"), never(DEGREES_F, "said the weather in Fahrenheit"))),
        Case("kenji-bio", KENJI, "turn", BIO,
             checks=(mentions("Kenji"), never_of_user(HE, "called Kenji he"),
                     never_of_user(SHE, "called Kenji she"))),
        Case("kenji-draft", KENJI, "turn",
             "Draft a reply to Ray saying yes to Thursday, and that I'll be there at 7.",
             {"mail": [RAY_DINNER]}, (signed_as("Kenji"),)),
        # -- A day, for nobody -------------------------------------------------
        Case("nobody-a-day", NOBODY, "day", world={
            "mail": [
                KIM_PHOTOS, RAY_DINNER,
                _mail("m-dental", "Riverside Dental <reminders@riversidedental.example>",
                      "Reminder: your appointment tomorrow at 10:00",
                      "This is a reminder of your appointment tomorrow at 10:00 at "
                      "Riverside Dental, 12 Linden Row. Reply C to confirm."),
                _mail("m-bank", "Litware Bank <alerts@litware.example>",
                      "Your statement is ready",
                      "Your monthly statement is ready to view online.", days_ago=0.3),
                _mail("m-sale", "Tailspin Outdoor <news@tailspin.example>",
                      "Autumn sale: 20% off boots", "Our autumn sale starts today.",
                      days_ago=0.2, listed=True)],
            "events": [*QUIET_TOMORROW, *ENDED,
                       {"id": "e-dinner", "summary": "Dinner with Ray Ellis", "in_days": 3,
                        "at": "19:00", "minutes": 120, "location": "Linden Row"}]},
             checks=(unknown_names, never_of_user(HE, "called the user he")),
             kinds=tuple(world.KINDS),
             # A move and a delete of the user's own events: Google's
             # calendar makes the move on condition, with no card, and
             # Outlook.com's asks for it, which is what the day measures.
             asks=("Move tomorrow's book club an hour later, to 20:00.",
                   "Take the dry cleaning pickup off my calendar.")),
    ]


# ---------------------------------------------------------------------------
# The world's weather
# ---------------------------------------------------------------------------

# The towns the geocoder knows here, as Open-Meteo's geocoder gives them.
TOWNS = {
    "pittsburgh": {"name": "Pittsburgh", "feature_code": "PPLA2", "latitude": 40.44,
                   "longitude": -79.99, "timezone": "America/New_York", "country_code": "US",
                   "admin1": "Pennsylvania", "country": "United States",
                   "population": 300_000},
    "lisbon": {"name": "Lisbon", "feature_code": "PPLC", "latitude": 38.72,
               "longitude": -9.14, "timezone": "Europe/Lisbon", "country_code": "PT",
               "admin1": "Lisbon", "country": "Portugal", "population": 500_000},
}
# Today, tomorrow and after, the same everywhere: tomorrow is wet.
SKY = [(14, 8, 10, 0.0, 2), (12, 6, 80, 9.0, 63), (15, 7, 20, 0.5, 3)]


def forecast_payload(params: dict) -> dict:
    """An Open-Meteo forecast, in °C and millimetres, from today in the
    place's zone."""
    zone = ZoneInfo(str(params.get("timezone") or "UTC"))
    today = dt.datetime.now(zone).date()
    count = max(1, min(int(params.get("forecast_days") or 3), 16))
    days = [today + dt.timedelta(days=i) for i in range(count)]
    sky = [SKY[min(i, len(SKY) - 1)] for i in range(count)]
    hours = [(d, h, s) for d, s in zip(days, sky, strict=True) for h in range(24)]
    return {
        "hourly_units": {"temperature_2m": "°C", "precipitation": "mm"},
        "daily_units": {"temperature_2m_max": "°C", "precipitation_sum": "mm"},
        "hourly": {
            "time": [f"{d.isoformat()}T{h:02d}:00" for d, h, _ in hours],
            "temperature_2m": [s[1] if h < 9 or h > 19 else s[0] for _, h, s in hours],
            "precipitation_probability": [s[2] for _, _, s in hours],
            "precipitation": [round(s[3] / 24, 2) for _, _, s in hours],
            "weather_code": [s[4] for _, _, s in hours],
        },
        "daily": {
            "time": [d.isoformat() for d in days],
            "temperature_2m_max": [s[0] for s in sky],
            "temperature_2m_min": [s[1] for s in sky],
            "precipitation_probability_max": [s[2] for s in sky],
            "precipitation_sum": [s[3] for s in sky],
            "weather_code": [s[4] for s in sky],
        },
    }


def feeds(url: str, params: dict | None = None, *, headers: dict | None = None) -> dict:
    """`fetch.get_json` in this world: the geocoder, the forecast and the
    weather alerts. Anything else is not here."""
    from agent.attention import fetch, trips
    from agent.attention.sources import weather

    params = params or {}
    if url == trips.GEOCODE:
        town = TOWNS.get(str(params.get("name") or "").strip().lower())
        return {"results": [town] if town else []}
    if url == weather.OPEN_METEO:
        return forecast_payload(params)
    if url == weather.NWS_ALERTS:
        return {"features": []}
    raise fetch.FetchError(f"{url}: not in the persona eval's world")


# ---------------------------------------------------------------------------
# Fences and state
# ---------------------------------------------------------------------------

_PINNED: dict = {"zone": ZoneInfo("UTC")}


def _fence(scratch: Path, patch=None) -> None:
    """The yardstick's fences, whose world is an account the attention
    sources read as they read any (`tests/modules/world`), and this
    eval's: the machine's zone pinned to the profile's, and the weather
    feeds answered here."""
    patch = patch or ae._ForGood()
    ae._fence(scratch, patch)
    patch.setattr("core.clock.local_zone", lambda: _PINNED["zone"])
    patch.setattr("agent.attention.fetch.get_json", feeds)


def _awake(now: dt.datetime) -> dict:
    """Quiet hours that are not now, so what the loop would say goes out:
    from three hours ahead, for eight. `now` is the machine's clock, not the
    pinned zone's: quiet hours are read on it (`within_quiet_hours`), and on
    a real machine the two agree."""
    start = (now + dt.timedelta(hours=3)).replace(minute=0)
    end = start + dt.timedelta(hours=8)
    return {"QUIET_FROM": f"{start:%H:%M}", "QUIET_UNTIL": f"{end:%H:%M}"}


def _settle(case: Case, folder: Path) -> None:
    """The case's panel: the profile's knobs, the case's over them, written
    where the worker reads them and put into force."""
    from agent.worker import control
    from core import knobs

    values = {**case.profile.knobs, **case.knobs}
    if case.play in ("attention", "day"):
        values.update(_awake(dt.datetime.now()))
    path = folder / "knobs.json"
    path.unlink(missing_ok=True)
    if values:
        knobs.write(values, path)
    control.apply_knobs(path)


def _seed(conn, profile: Profile) -> None:
    """What the user said about themselves, as the profile page keeps it."""
    from agent import facts

    for attribute, value in profile.facts.items():
        facts.add(conn, facts.USER, attribute, value, source="user_words")


# ---------------------------------------------------------------------------
# Playing a case
# ---------------------------------------------------------------------------


async def _told(text: str) -> bool:
    ae._CURRENT["recorder"].told.append(str(text))
    return True


async def _turn(case: Case, conn, seen: Seen) -> None:
    from agent.loop import Conversation

    seen.notes["asked_at"] = dt.datetime.now(dt.UTC).isoformat()
    reply = await Conversation(conn, channel="tray").send(case.ask)
    seen.reply, seen.tools = reply.text, list(reply.tools_used)


async def _attention(case: Case, conn, seen: Seen, *, tasks: bool = False) -> None:
    from agent import attention
    from agent.attention import deliver, prepare, store, triage

    await attention.tick(conn, notify=_told, do_tasks=tasks, force=True)
    for _ in range(3):
        if store.untriaged(conn, 1):
            await triage.triage(conn)
        if tasks:
            await prepare.prepare(conn)
        await deliver.deliver(conn, notify=_told)


async def _briefing(case: Case, conn, seen: Seen, kinds=("evening",)) -> None:
    """The briefings as they would go out, the forecast fetched first as the
    weather source keeps it. The evening's is sent at 21:05, so it is about
    tomorrow."""
    from agent import attention
    from agent.attention import briefing

    await attention.look(conn, only=["weather"], force=True)
    today = dt.datetime.now(_PINNED["zone"]).date()
    at = {"morning": dt.time(8, 5), "evening": dt.time(21, 5)}
    for kind in kinds:
        seen.notes[kind] = await briefing.send(conn, kind, notify=_told,
                                               now=dt.datetime.combine(today, at[kind]))


async def _clock(case: Case, conn, seen: Seen) -> None:
    from agent.attention import briefing

    today = dt.datetime.now(_PINNED["zone"]).date()
    seen.notes["due"] = {
        at: briefing.due(conn, dt.datetime.combine(today, dt.time.fromisoformat(at)))
        for at in ("21:30", "22:30", "07:00")}


async def _day(case: Case, conn, seen: Seen) -> None:
    """The attention loop's look, the morning's briefing, what the user
    asks in the day, and the evening's briefing, which then reads the
    calendar as the day left it."""
    from agent.loop import Conversation

    await _attention(case, conn, seen, tasks=True)
    await _briefing(case, conn, seen, kinds=("morning",))
    if case.asks:
        talk = Conversation(conn, channel="tray")
        for ask in case.asks:
            reply = await talk.send(ask)
            seen.reply = "\n".join(filter(None, [seen.reply, reply.text]))
            seen.tools += list(reply.tools_used)
    await _briefing(case, conn, seen, kinds=("evening",))


PLAYS: dict[str, Callable[[Case, object, Seen], Awaitable[None]]] = {
    "turn": _turn, "attention": _attention, "briefing": _briefing, "clock": _clock,
    "day": _day}


def _observations(conn) -> list[dict]:
    rows = conn.execute("SELECT source, key, title, say, headline, ref FROM observations"
                        " ORDER BY id").fetchall()
    return [{**dict(r), "ref": json.loads(r["ref"] or "{}")} for r in rows]


def score(case: Case, seen: Seen) -> list[str]:
    """Every rule the case broke, in words. Empty is a pass."""
    problems = []
    for check in (*ALWAYS, *case.checks):
        try:
            if problem := check(seen, case):
                problems.append(problem)
        except Exception as exc:  # noqa: BLE001 - a broken rule is a failed case
            problems.append(f"{getattr(check, '__name__', 'a check')} broke: {exc}")
    return problems


async def run_case(case: Case, scratch: Path, attempt: int = 1,
                   kind: str | None = None) -> tuple[dict, float]:
    """One case, on a database and a panel of its own, played as `kind` of
    account if one is named: its row, and what it cost."""
    from agent import runtime

    folder = scratch / f"{case.id}-{attempt}{'-' + kind if kind else ''}"
    folder.mkdir(parents=True, exist_ok=True)
    _PINNED["zone"] = ZoneInfo(case.profile.zone)
    conn = db.open_store(folder / "agent.db")
    runtime.conn = conn
    recorder = ae.Recorder()
    ae._CURRENT.update(world=ae.World({"world": case.world}, recorder, kind=kind),
                       recorder=recorder)
    seen = Seen()
    started = time.perf_counter()
    error = None
    try:
        _settle(case, folder)
        _seed(conn, case.profile)
        await asyncio.wait_for(PLAYS[case.play](case, conn, seen), CASE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - one case is not the run
        log.exception("case %s failed", case.id)
        error = f"{type(exc).__name__}: {exc}"[:300]
    seen.told = list(recorder.told)
    seen.drafts = [e.args for e in recorder.effects if e.tool == "draft_email"]
    seen.observations = _observations(conn)
    problems = [f"it broke: {error}"] if error else score(case, seen)
    cost = ae.spent(conn)
    row = {"id": case.id, "profile": case.profile.name, "attempt": attempt,
           "passed": not problems, "problems": problems, "error": error,
           "said": seen.said[:1500], "tools": seen.tools,
           "effects": [{"tool": e.tool, "outcome": e.outcome} for e in recorder.effects],
           "cost_usd": round(cost, 5), "seconds": round(time.perf_counter() - started)}
    if kind:
        row.update(kind=kind, cards=sum(e.outcome == "card" for e in recorder.effects))
    runtime.conn = None
    conn.close()
    return row, cost


# How many more cards a kind of account's day may ask than the same day on
# Gmail (05-connectors.md, the exit criteria).
CARDS_MORE = 3


def card_problems(rows: list[dict]) -> list[str]:
    """Each day played as a kind of account that asked more than
    `CARDS_MORE` cards beyond the same case's Gmail day, in the same
    attempt. A kind without conditional writes asks for every update and
    delete: a few more cards is the price of that, and many more would be
    a day the user spends answering them."""
    from tests.modules.world import provider as world

    against_kind = world.BASELINE
    days: dict[tuple[str, int], dict[str, int]] = {}
    for row in rows:
        if "cards" in row:
            days.setdefault((row["id"], row["attempt"]), {})[row["kind"]] = row["cards"]
    problems = []
    for (case, attempt), cards in sorted(days.items()):
        against = cards.get(against_kind)
        if against is None:
            problems.append(f"{case} ({attempt}): no {against_kind} day to measure against")
            continue
        problems.extend(f"{case} ({attempt}): {kind} asked {made} cards to "
                        f"{against_kind}'s {against}"
                        for kind, made in sorted(cards.items()) if made > against + CARDS_MORE)
    return problems


async def run(budget_usd: float = DEFAULT_BUDGET_USD, only: list[str] | None = None,
              repeat: int = DEFAULT_REPEAT) -> dict:
    """Every case, or the ones named, `repeat` times each, until the budget
    is spent. In a data folder of its own, so the panel, the profile and
    the database of the checkout it runs from are never touched."""
    from agent import runtime

    if runtime.conn is not None:
        raise RuntimeError("the persona eval runs in a process of its own, never the worker's")
    reports = reports_dir()
    os.environ.setdefault(paths.MODEL_CACHE_ENV, str(paths.models_dir()))
    scratch = paths.scratch_dir() / f"persona-{os.getpid()}"
    os.environ[paths.DATA_DIR_ENV] = str(scratch / "data")
    _fence(scratch)
    chosen = [c for c in cases() if not only or c.id in only]
    rows, spent = [], 0.0
    try:
        for attempt in range(1, max(1, repeat) + 1):
            for case, kind in ((c, k) for c in chosen for k in c.kinds or (None,)):
                if spent >= budget_usd:
                    rows.append({"id": case.id, "attempt": attempt, "skipped": "budget"})
                    continue
                log.info("playing %s (%d)%s", case.id, attempt, f" as {kind}" if kind else "")
                row, cost = await run_case(case, scratch, attempt, kind)
                spent += cost
                rows.append(row)
                log.info("%s: %s ($%.4f) %s", case.id, "pass" if row["passed"] else "FAIL",
                         cost, "; ".join(row["problems"]))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    played = [r for r in rows if "skipped" not in r]
    report = {"at": dt.datetime.now().isoformat(timespec="seconds"),
              "cost_usd": round(spent, 4), "budget_usd": budget_usd, "repeat": repeat,
              "lineup": ae._lineup(),
              "summary": {"cases": len({r["id"] for r in played}), "runs": len(played),
                          "passed": sum(r["passed"] for r in played),
                          "skipped": len(rows) - len(played)},
              "failed": [{"id": r["id"], "attempt": r["attempt"], "kind": r.get("kind"),
                          "problems": r["problems"]} for r in played if not r["passed"]],
              "cards": {f"{r['id']} ({r['attempt']}) {r['kind']}": r["cards"]
                        for r in played if "cards" in r},
              "card_problems": card_problems(played),
              "rows": rows}
    reports.mkdir(parents=True, exist_ok=True)
    out = reports / f"{dt.datetime.now():%Y-%m-%d-%H%M%S}.json"  # two in one minute were one file
    out.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
    report["path"] = str(out)
    return report


def _main() -> None:
    parser = argparse.ArgumentParser(description="The persona eval: what Steve assumes.")
    parser.add_argument("what", choices=["run", "list"])
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT)
    args = parser.parse_args()
    if args.what == "list":
        for case in cases():
            print(f"{case.id:<22} {case.play:<9} {case.ask}")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    report = asyncio.run(run(args.budget, args.only, args.repeat))
    print(json.dumps({k: report[k] for k in ("summary", "failed", "cards", "card_problems",
                                             "cost_usd", "path")},
                     indent=2))


if __name__ == "__main__":
    _main()
