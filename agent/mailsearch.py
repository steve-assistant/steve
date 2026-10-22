"""Searching their mail, and searching again, wider, when the first search
misses.

The maintainer, 4 October 2026: asked for "the Contoso 325th anniversary event", the
agent searched "325th anniversary" and then "anniversary", found nothing,
and said so. The email was there: "Invitation to celebrate Contoso's 325th |
October 9". Outlook's search matches plain words against the subject and the
people, and every word must match, so "325th" alone would have found it.
The user had to give the subject themselves, and then asked: "do slightly deeper
searches with more terms (in parallel?) if you can't find it".

So `search` runs their query on every mailbox asked for at once, and when it
misses -- nothing came back, or nothing that mentions the query's telling
terms (`telling`) -- runs a few narrower searches (`variants`), at most
`EXTRA_SEARCHES`, and merges what they find without repeats:

- **Telling terms first**: a dated or numbered word ("325th"), a date kept
  whole ("October 9", not "October" and "9"), a quoted phrase; then names
  (a capitalised word, its possessive dropped -- "Contoso's" is searched as
  "Contoso", never "contosos"); then other words long enough to mean something;
  then a month or weekday on its own. A bare small number ("3") or a filler
  word never spends a search. With three terms or more, the two most
  telling are searched together first.
- **Ranked by what they show** (`_ranked`): the results that mention the
  most of their terms first, wherever they came from.
- **Their operators kept**: `from:dana newer_than:7d budget review` is
  narrowed to `from:dana newer_than:7d budget`, never widened past who or
  when the user asked about.
- **In parallel by account, in turn within one**: every account is
  searched at the same time; one account's searches run one after another
  on one thread, because Outlook's COM and Gmail's HTTP client are not safe
  to share between threads (`agent/connectors.py`).

Upgrade #79 (the night's coder, 4 October) tried this and the judge refused
it: it widened only when nothing at all came back, turned "Contoso's" into
"contosos", split dates into tokens, and let a requested "3" use up the
searches. Rewritten by hand, 5 October.
"""

from __future__ import annotations

import re
from collections.abc import Callable

EXTRA_SEARCHES = 4

_OPERATOR = re.compile(r'(?<!\S)[\w-]+:(?:"[^"]*"|\S+)')
_QUOTED = re.compile(r'"([^"]+)"')
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
          r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DATES = re.compile(
    rf"\b{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z0-9][\w'’.-]*")
_POSSESSIVE = re.compile(r"['’]s$", re.IGNORECASE)
_FILLER = frozenset({
    "about", "after", "again", "also", "and", "any", "are", "before", "email", "emails",
    "find", "for", "from", "have", "his", "into", "mail", "message", "messages", "my",
    "please", "search", "that", "the", "their", "there", "this", "was", "were", "what",
    "when", "where", "which", "with", "your", "you", "inbox", "sent", "thing", "stuff",
    "last", "latest", "recent", "first",
})
# Capitalised, but not names: "October" alone matches every email that
# mentions the month. Kept as a term, after the words that say what it is.
_CALENDAR_WORDS = re.compile(
    rf"^(?:{_MONTH}|mon|tue|wed|thu|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?$",
    re.IGNORECASE)


def _clean(word: str) -> str:
    return _POSSESSIVE.sub("", word.strip(".,;:!?'’\"-"))


def telling(query: str) -> list[str]:
    """The query's terms, most telling first: dates and numbered words and
    quoted phrases, then names, then other words of four letters or more.
    Operators, filler and bare small numbers are not terms."""
    text = _OPERATOR.sub(" ", query)
    first: list[str] = [m.strip() for m in _QUOTED.findall(text)]
    text = _QUOTED.sub(" ", text)
    first += [m.group(0) for m in _DATES.finditer(text)]
    text = _DATES.sub(" ", text)
    names: list[str] = []
    rest: list[str] = []
    late: list[str] = []
    for raw in _WORD.findall(text):
        word = _clean(raw)
        low = word.lower()
        if len(word) < 3 or low in _FILLER:
            continue
        if word.isdigit() and len(word) < 4:
            continue  # "3", "10": a count, not something to find
        if any(c.isdigit() for c in word):
            first.append(word)
        elif _CALENDAR_WORDS.match(word):
            late.append(word)
        elif word[0].isupper():
            names.append(word)
        elif len(word) >= 4:
            rest.append(word)
    out: list[str] = []
    for term in [*first, *names, *rest, *late]:
        if term and term.lower() not in {t.lower() for t in out}:
            out.append(term)
    return out


def variants(query: str, limit: int = EXTRA_SEARCHES) -> list[str]:
    """Narrower searches for a query that missed, with the query's operators
    kept, never the query itself again: its two most telling terms together
    when it has three or more, then each term alone, most telling first.

    The pair first because every mailbox wants every word: "Tailspin Denver
    October networking" (6 October 2026) found nothing, since the
    confirmation's subject has no "networking", while "Tailspin" alone
    brought back ten Gmail newsletters. "Tailspin Denver" is the
    confirmation."""
    operators = _OPERATOR.findall(query)
    original = " ".join(query.lower().split())

    def shown(term: str) -> str:
        return f'"{term}"' if " " in term and not _DATES.fullmatch(term) else term

    terms = telling(query)
    groups = ([terms[:2]] if len(terms) >= 3 else []) + [[t] for t in terms]
    out: list[str] = []
    for group in groups:
        search = " ".join([*operators, *map(shown, group)])
        if " ".join(search.lower().split()) != original:
            out.append(search)
        if len(out) >= limit:
            break
    return out


def how_many(message: dict, terms: list[str]) -> int:
    """How many of the terms a result mentions, where a person would look:
    the subject, who it is from or to, the snippet."""
    seen = " ".join(str(message.get(k) or "") for k in ("subject", "from", "to", "snippet"))
    seen = seen.lower().replace("’", "'")
    return sum(t.lower().replace("’", "'") in seen for t in terms)


def mentions(message: dict, terms: list[str]) -> bool:
    return how_many(message, terms) > 0


def missed(query: str, found: list[dict]) -> str | None:
    """Why the first search missed, or None if it found what was asked."""
    if not found:
        return "nothing matched"
    sharp = [t for t in telling(query)[:2] if any(c.isdigit() for c in t) or t[:1].isupper()]
    if sharp and not any(mentions(m, sharp) for m in found):
        return f"nothing it found mentions {' or '.join(sharp)}"
    return None


async def search(query: str, limit: int, accounts: list, shown: Callable, *,
                 named: bool = False) -> dict:
    """Their query on each account asked, at once; when it misses, the
    narrower `variants` too, merged without repeats. `shown(account,
    message)` is a result as the model reads it. `named`: the user asked
    for these accounts by name, so one that is not here is said to be."""
    from agent import connectors
    from steve_sdk.errors import NotConnected

    answered: set[str] = set()
    ignored: dict[str, list[str]] = {}

    async def run(queries: list[str]) -> tuple[list[tuple[int, dict]], list[str]]:
        """Each result with its place in its own account's answer."""
        def each(provider) -> list:
            # In turn within the account, on its thread (`connectors.ask_all`).
            return [(q, provider.search(q, limit=limit)) for q in queries]

        messages, problems = [], []
        for answer in await connectors.ask_all(accounts, each):
            name = answer.account.name
            if answer.error is not None:
                # An account not on this computer is skipped unless it was
                # asked for: Outlook closed is not a fault in "all my mail".
                if named or not isinstance(answer.error, NotConnected):
                    problems.append(connectors.say(answer.error, name))
                continue
            answered.add(name)
            for q, found in answer.result:
                for place, message in enumerate(found):
                    item = shown(answer.account, message)
                    if q != query:
                        item["found_by"] = q
                    messages.append((place, item))
        return messages, problems

    first, problems = await run([query])
    for account in accounts:
        # Said rather than swallowed: a dropped operator widens the answer,
        # and the model should say so rather than present it as exact. Each
        # mailbox worked this out, and nothing passed it on until step 5.
        if account.name in answered and (dropped := account.provider.ignored(query)):
            ignored[account.name] = dropped
    found = [message for _, message in first]
    everything = list(first)
    out: dict = {"query": query}
    if why := missed(query, found):
        wider = variants(query)
        if wider:
            more, more_problems = await run(wider)
            everything += more
            found = _ranked(first, more, [query, *wider], telling(query))[:max(limit, 10)]
            problems += [p for p in more_problems if p not in problems]
            new = {_key(m) for _, m in more} - {_key(m) for _, m in first}
            out["widened"] = {"because": why, "searched": wider, "found": len(new)}
            if not found:
                out["note"] = (f"Also searched {', '.join(wider)} in "
                               f"{', '.join(a.name for a in accounts)}: nothing.")
    # Which accounts were searched, and how many each found before any
    # cut. On 6 October 2026, its own-domain result cut from a widened list,
    # it told them it "wasn't checking" that mailbox -- it had been.
    unique = {_key(m) for _, m in everything}
    out["found_in"] = {a.name: sum(box == a.name for box, _ in unique)
                       for a in accounts if a.name in answered}
    out.update(count=len(found), messages=found, problems=problems)
    if ignored:
        out["ignored_terms"] = ignored
    return out


def _key(message: dict) -> tuple:
    return message.get("account"), message.get("id")


def _ranked(first: list[tuple[int, dict]], more: list[tuple[int, dict]],
            searches: list[str], terms: list[str]) -> list[dict]:
    """Everything the searches found, once each, best first.

    Ranked on what can be seen of each: how many of their terms it mentions,
    how many of the searches found it, the most telling search that did,
    then its place in its own mailbox's answer -- which takes the mailboxes
    in turn rather than one after another.

    It was the most telling search first, mailbox by mailbox, and that is
    how on 6 October 2026 ten Gmail newsletters that "Tailspin" matched in
    their bodies filled the list, and the own-domain confirmation, "Get to
    Know Tailspin Denver - October 2026", was cut. And what the first search
    found came first whatever it was -- though it was searching wider
    because none of that mentioned what the user asked about.
    """
    entries: dict[tuple, list] = {}
    for place, message in [*first, *more]:
        search = searches.index(message.get("found_by", searches[0]))
        entry = entries.get(_key(message))
        if entry is None:
            entries[_key(message)] = [message, 1, search, place]
        else:
            entry[1] += 1
    return [entry[0] for entry in sorted(
        entries.values(), key=lambda e: (-how_many(e[0], terms), -e[1], e[2], e[3]))]
