"""Gate: the product names no one, and assumes no one's life.

Steve began as its maintainer's own assistant, and the product is a copy of
it. The copy carried that life everywhere: the maintainer's name in the call
scripts, a school as the name of a mailbox, classmates in the tests, a home
town in the weather examples, and prompts that told every user's model about
coffee chats and recruiting. Step 2 took them out, by hand and with a sweep.
This keeps them out, because the next feature is written by copying the code
beside it, and a name copied once is copied again.

Five checks, over every text file git tracks or would add, but the plans
(`docs/plan/`, which leave the repo before it is published):

1. an email address is on a reserved domain, or is a service's own sender;
2. a phone number is in the fictional 555-01xx range;
3. no term from the hashed denylist (`devtools/denylist.py`): the
   maintainer's names and handles, the people and firms in their mail, their
   school, town, courses and places;
4. no he/him/his/himself in what the model or the user reads;
5. no words of one kind of life (recruiting, coffee chats, an MBA) there.

What a later step rewrites waits in `LATER`, which only shrinks: an entry
whose file is gone fails, so the list says what is left and nothing else.
"""

from __future__ import annotations

import ast
import re
from functools import cache

import pytest

from devtools import denylist
from tests.gate import repo_files

# The private folder is git's to ignore, and the denylist is what is
# looked for. The plans, which named the edition the product was copied
# from, were scanned around until they left the tree for private/.
NOT_SCANNED = ("private/", "tests/denylist.txt")

# This file's own samples of what the address and phone checks catch.
SELF = "tests/unit/test_no_personal_data.py"

# What a later step rewrites or deletes, and which step. Polishing it now
# would be work thrown away (the step 2 plan, principle 6).
LATER: dict[str, int] = {}  # empty since step 7: nothing waits

def _later(rel: str) -> bool:
    return any(rel == path or (path.endswith("/") and rel.startswith(path)) for path in LATER)


@cache
def _scanned() -> tuple[tuple[str, str], ...]:
    return tuple((rel, text) for rel in repo_files.listed()
            if not rel.startswith(NOT_SCANNED) and not _later(rel)
            and (text := repo_files.text(rel)) is not None)


def _line(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


# --- 1. Addresses ----------------------------------------------------------

ADDRESS = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+([A-Za-z]{2,}))(?![\w-])")

# Reserved for examples (RFC 2606, RFC 6761), and Steve's own git domain.
RESERVED = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|example|test|invalid|localhost"
                      r"|steve\.local)$", re.IGNORECASE)

# Not addresses: a retina icon's file name.
FILE_NAMES = {"png", "jpg", "jpeg", "svg", "ico", "webp"}

# Real senders the code recognises or the tests imitate. Each is a service's
# own address, which names no one.
SERVICES = {
    "no-reply@accounts.google.com", "google-noreply@google.com",
    "drive-shares-dm-noreply@google.com", "news-webmaster@google.com",
    "receipts@stripe.com", "team@news.fly.io",
    "security-noreply@linkedin.com", "noreply@zoom.us",
    "writer@substack.com", "writer@mail.substack.com", "hello@email.substack.com",
    "x@substack.com.evil.io",
    "noreply@campusgroups.com", "no-reply@campusgroups.com", "events@campusgroups.com",
    "noreply@contoso.campusgroups.com", "studies@treyresearch.qualtrics.com",
    # Fictional newsletters on a real platform, whose address is the point.
    "fieldnotes@substack.com", "opsweekly@substack.com", "kimhaleletters@substack.com",
}

# The fictional user's own Gmail, in the spellings Gmail treats as one
# (tests/persona.py), and her mailbox at each provider a mail preset names
# (modules/imap/manifest.toml), whose domain is the point of the test.
PERSONA_GMAIL = re.compile(
    r"^(?:steve\.?test\.?user|maya\.?lindqvist)(?:\+[\w-]+)?@(?:gmail|googlemail)\.com$"
    r"|^maya\.lindqvist@(?:icloud|fastmail|yahoo)\.com$")


def _unexpected_address(address: str, domain: str, tld: str) -> bool:
    address = address.lower()
    return not (RESERVED.search(domain) or tld.lower() in FILE_NAMES
                or address in SERVICES or PERSONA_GMAIL.match(address)
                or re.match(r"^c_\w+@resource\.calendar\.google\.com$", address))


def test_every_email_address_is_an_example_or_a_service():
    """A real address in a test fixture is a real person's, copied from the
    mail it was written against. `example.com`, `*.example` and the other
    reserved names can belong to no one."""
    found = [f"{rel}:{_line(text, m.start())}: {m.group(0)}"
             for rel, text in _scanned() if rel != SELF
             for m in ADDRESS.finditer(text)
             if _unexpected_address(m.group(0), m.group(1), m.group(2))]
    assert not found, ("addresses that could be someone's; use a reserved domain "
                       "such as name@example.com or name@company.example:\n  "
                       + "\n  ".join(found))


# --- 2. Phone numbers ------------------------------------------------------

PHONE = re.compile(
    r"(?<![\w+])(?:\+1[ .-]?|1-)?\(?([2-9]\d\d)\)?[ .-](\d{3})[ .-](\d{4})(?!\d)"
    r"|(?<![\w+])\+1(\d{3})(\d{3})(\d{4})(?!\d)")

# 900 and 976 are premium lines: the call policy's refusal of them is tested
# with numbers in them, which is the point.
PREMIUM = {"900", "976"}


def _unexpected_phone(area: str, exchange: str, line: str) -> bool:
    if exchange == "555" and line.startswith("01"):
        return False
    return area[0] not in "01" and area not in PREMIUM and exchange not in PREMIUM


def test_every_phone_number_is_fictional():
    """555-0100 to 555-0199 are the numbers set aside for fiction; any other
    number is someone's line."""
    found = []
    for rel, text in _scanned():
        if rel == SELF:
            continue
        for m in PHONE.finditer(text):
            area, exchange, line = (g for g in m.groups() if g is not None)
            if _unexpected_phone(area, exchange, line):
                found.append(f"{rel}:{_line(text, m.start())}: {m.group(0)}")
    assert not found, ("phone numbers outside 555-01xx; use one such as "
                       "+1 412 555 0100:\n  " + "\n  ".join(found))


# --- 3. The denylist -------------------------------------------------------

def test_no_tracked_file_names_anyone_on_the_denylist():
    """The denylist holds hashes, so this file does not publish what it
    guards. A hit names the term as written in the file, and the fix is to
    use the fictional cast in tests/persona.py, or no one."""
    hashed = denylist.load()
    assert len(hashed) > 50, "tests/denylist.txt is missing or nearly empty"
    found = [f"{rel}:{line}: {term}"
             for rel, text in _scanned()
             for line, term in denylist.find(text, hashed)]
    assert not found, "denied terms:\n  " + "\n  ".join(found)


def test_the_denylist_matches_words_whatever_their_case_or_joining():
    """"Port Ellery", "port-ellery" and "PORT_ELLERY" are one term. A term split
    across a line break is still found, on the line where it starts, and
    part of a word is not a word."""
    hashed = frozenset({denylist.digest("Quillon Varr"), denylist.digest("zorbit")})
    text = "one\nmet QUILLON\nvarr at the-zorbit_lab, not zorbits\n"
    assert denylist.find(text, hashed) == [(2, "quillon varr"), (3, "zorbit")]
    assert denylist.digest("Quillon-Varr") == denylist.digest("quillon_varr")
    assert denylist.find(text, frozenset()) == []


# --- 4 and 5. What the model and the user read -----------------------------

HE = re.compile(r"\b(?:he|him|his|himself)\b", re.IGNORECASE)
SHE = re.compile(r"\b(?:she|her|hers|herself)\b", re.IGNORECASE)

# Words that assume the user is a business student looking for a job. An
# optional feature built for that life may use them in its own section;
# nothing else does.
ONE_LIFE = re.compile(
    r"coffee[ -]chats?|\brecruit\w*|networking events?|\bMBAs?\b|\bconsult(?:ing|ancy|ants?)\b"
    r"|\bprofessors?\b|\bclassmates?\b|\binternships?\b|case interviews?", re.IGNORECASE)

# Python whose strings reach the model or the user, and the documents the
# coder reads (agent/modbuild.library) and the maps of the code.
SPOKEN = ("agent/", "core/", "tools/", "voice/", "channels/")
# But not the persona eval's: its strings are the words it listens for
# ("recruit", "called the user he") and the fake world it plays, read by
# no user -- a test's fixtures, kept beside the evals it runs with. The
# denylist and the real names are still checked in it, as everywhere.
UNSPOKEN = {"agent/persona_eval.py"}
BRIEFS = {"docs/modules.md", "docs/architecture.md", "docs/protocols.md"}


def _spoken_strings(rel: str, tree: ast.Module) -> list[tuple[int, str]]:
    """Every string in the module but its docstrings, and the docstrings of
    the tools it registers, which are their descriptions."""
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef
                                | ast.AsyncFunctionDef)
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)}
    found = [(node.lineno, node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and isinstance(node.value, str)
             and id(node) not in docstrings]
    for register in ast.walk(tree):
        if isinstance(register, ast.FunctionDef) and register.name == "register":
            found += [(node.lineno, doc) for node in ast.walk(register)
                      if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                      and node is not register and (doc := ast.get_docstring(node))]
    return found


@cache
def _read_by_the_model() -> tuple[tuple[str, int, str], ...]:
    """What the model or the user reads, as (where, line, text)."""
    from agent import features
    from core import modules

    pieces = [("the assembled prompt", 0, features.assemble(features.everything()))]
    pieces += [(f"the tool {manifest.id}.{tool.name}", 0, said)
               for manifest in modules.builtins().values() for tool in manifest.tools
               for said in (tool.description, *(p.description for p in tool.params))]
    for rel, text in _scanned():
        if ("/prompts/" in rel and rel.endswith(".md")) or rel in BRIEFS:
            pieces += [(rel, n, line) for n, line in enumerate(text.splitlines(), 1)]
        elif rel.endswith(".py") and rel.startswith(SPOKEN) and rel not in UNSPOKEN:
            pieces += [(rel, n, s) for n, s in _spoken_strings(rel, ast.parse(text))]
    return tuple(pieces)


def _prose(text: str) -> bool:
    """Words in a sentence, rather than one word in a set or a pattern that
    matches pronouns in what other people wrote."""
    return " " in text.strip() and "(?:" not in text and "\\b" not in text


def test_nothing_the_model_reads_calls_the_user_he():
    """A prompt that says "his inbox" tells the model who the user is, and
    the model repeats it. A string naming both he and she lists pronouns
    rather than assuming one: a stopword list, or "his or her"."""
    found = [f"{where}:{line}: {HE.search(text).group(0)!r} in {text.strip()[:80]!r}"
             for where, line, text in _read_by_the_model()
             if _prose(text) and HE.search(text) and not SHE.search(text)]
    assert not found, "the user is not 'he'; write 'the user' or 'they':\n  " + "\n  ".join(found)


def test_nothing_the_model_reads_assumes_one_kind_of_life():
    """The prompts were written for a business student recruiting for a job,
    so the model read about coffee chats and case interviews whoever the
    user was, and saw them in every week."""
    found = [f"{where}:{line}: {m.group(0)!r}"
             for where, line, text in _read_by_the_model()
             for m in ONE_LIFE.finditer(text)]
    assert not found, "words of one kind of life:\n  " + "\n  ".join(found)


@pytest.mark.parametrize(("text", "flagged"), [
    ("Tell him the meeting moved.", True),
    ("Ask his assistant for the room.", True),
    ("Tell them the meeting moved.", False),
    ("a an the he she it him her his", False),
    ("his", False),
    (r"\b(?:his|their) approval\b", False),
    ("the theme of history", False),
])
def test_the_pronoun_check_tells_use_from_mention(text, flagged):
    """A check that cries wolf on a stopword list gets switched off."""
    assert bool(_prose(text) and HE.search(text) and not SHE.search(text)) is flagged


@pytest.mark.parametrize(("text", "flagged"), [
    ("Two coffee chats this week", True),
    ("the recruiter wrote back", True),
    ("an internship offer", True),
    ("the MBA program", True),
    ("Consulting firms", True),
    ("a networking event at six", True),
    ("Nothing on your calendar", False),
    ("the chat about coffee", False),
    ("ambassador", False),
])
def test_the_one_life_check_finds_the_words_it_names(text, flagged):
    assert bool(ONE_LIFE.search(text)) is flagged


@pytest.mark.parametrize(("address", "flagged"), [
    ("maya@example.com", False),
    ("ann@northwind.example", False),
    ("steve.test.user+agent@gmail.com", False),
    ("receipts@stripe.com", False),
    ("someone@gmail.com", True),
    ("first.last@university.edu", True),
    ("icon@2x.png", False),
])
def test_the_address_check_tells_an_example_from_a_person(address, flagged):
    m = ADDRESS.search(address)
    assert m and _unexpected_address(m.group(0), m.group(1), m.group(2)) is flagged


@pytest.mark.parametrize(("number", "flagged"), [
    ("+14125550100", False),
    ("(412) 555-0123", False),
    ("+19005551212", False),
    ("+10000000001", False),
    ("+14125551234", True),
    ("203-432-6000", True),
    ("(203) 432 6000", True),
])
def test_the_phone_check_tells_a_fictional_number_from_a_line(number, flagged):
    m = PHONE.search(number)
    assert m, number
    area, exchange, line = (g for g in m.groups() if g is not None)
    assert _unexpected_phone(area, exchange, line) is flagged


def test_every_exemption_is_still_needed():
    """An entry for a file that is gone is a hole nothing fills: a new file
    at that path would be exempt unseen."""
    tracked = repo_files.listed()
    gone = [path for path in LATER
            if not any(rel == path or (path.endswith("/") and rel.startswith(path))
                       for rel in tracked)]
    gone += sorted(UNSPOKEN - set(tracked))
    assert not gone, f"exempt but gone: {gone}. Take them out of LATER or UNSPOKEN."
