"""No code outside `modules/` names a provider: a ratchet, emptied by step 5's
item 13.

Until step 5 every class had its providers written into the code around it:
`search_email` built a Gmail searcher and an Outlook one, the attention loop
had a source and a cadence per provider, and the night's ingest asked Gmail,
Outlook and the password mailbox in turn. A module the user added could not stand where
they stood without each of those taught its name, and none was. Now the
slot (`agent/connectors.py`) answers for any account, and the code that
names a provider lives in that provider's module.

This reads what runs: Python's names and strings (its comments and
docstrings may still tell a past incident), the prompts in full, and the
settings and the app's code less their comments. A name that is not a
connector -- Google News, Playwright's image on Microsoft's registry -- is
allowed below with its reason. Today's other names are listed per file,
each with the item that removes it; a count may only fall, and a fall is
written down here, so that the list is a true account of what is left.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from functools import cache

from tests.gate import repo_files

PROVIDERS = ("gmail", "google", "outlook", "microsoft", "dreamhost", "icloud", "fastmail",
             "yahoo", "canvas")
_NAME = re.compile("|".join(PROVIDERS), re.IGNORECASE)

# What is read: the code that ships, the developer's tools, and the app. Not
# modules/ (where a provider's name belongs), tests/, the documents, the
# migrations (history, applied once) or private/.
READ = ("agent/", "channels/", "core/", "devtools/", "scripts/", "steve_sdk/", "tools/",
        "voice/", "ui/src/", "ui/src-tauri/src/")
SUFFIXES = (".py", ".md", ".yaml", ".yml", ".toml", ".json", ".ts", ".tsx", ".rs")

# Not connectors: each pattern is taken out of the text before it is read.
ALLOWED: list[tuple[str, str, str]] = [
    ("agent/attention/sources/news.py", r"news\.google\.com",
     "Google News: a public feed of headlines, not an account"),
    ("agent/attention/sources/weather.py", r"meet\.google",
     "a meeting link in an event, which says the meeting is online"),
    ("agent/attention/trips.py", r"meet\.google",
     "a meeting link in an event, which says the meeting is online"),
    ("core/browser/sandbox.py", r"mcr\.microsoft\.com/playwright",
     "Playwright's image, on Microsoft's container registry"),
    ("tools/web.py", r'"Google Chrome";v=', "Chrome's user agent, which a page reads"),
    ("agent/redact.py", r'"google key"', "the pattern that redacts Google's API keys"),
    ("devtools/bipia.py", r"microsoft/BIPIA",
     "Microsoft's BIPIA benchmark of instructions hidden in content"),
    ("devtools/bipia.json", r"microsoft/BIPIA",
     "Microsoft's BIPIA benchmark of instructions hidden in content"),
    ("core/totp.py", r"Google Authenticator", "an app the user may keep their codes in"),
    ("steve_sdk/manifest.py", r'^"(outlook\.application|microsoft\.xml)"$',
     "desktop programs' COM names: one whose reads Steve knows, one never opened"),
    ("agent/persona_eval.py", r'^"(gmail|google)"$',
     "words a reply may hold that are not a person's name"),
    ("agent/autonomy_eval.py",
     r'^"(modules\.google_cloud\.common\.build|the Google sign-in|Google'
     r'|google_auth_oauthlib\.flow\.InstalledAppFlow\.(from_client_config|run_local_server))"$',
     "the yardstick's fence: Google's client and its browser sign-in open their own "
     "connections (05-connectors.md, section 1's exception), and a fence names them"),
    ("agent/accounts.py", r'classic Outlook on this computer|"outlook_classic"',
     "the one question an address no manifest claims is asked, and the module its yes "
     "opens, as the plan fixes them (05-connectors.md, section 3)"),
    ("core/mailbox.py",
     r'^"(gmail\.com|googlemail\.com|mx\.google\.com|imap\.gmail\.com|gmail\.googleapis\.com)"$',
     "the own mailbox's trust table: the providers that tie a From to the signed-in "
     "account, keyed by the host an account reads mail from, kept in core so a module "
     "cannot widen it (05-connectors.md, section 7)"),
    ("devtools/package.py", r'GOOGLE_APIS|"gmail\.v1"|googleapiclient',
     "the build keeps the discovery documents of the five APIs the Google module builds "
     "and trims the other six hundred (08-packaging.md, section 1): a trim, not a connector"),
    ("core/uninstall.py", r"\\Software\\Microsoft\\Windows",
     "the registry key where Windows lists the user's AppContainer profiles, which "
     "uninstall reads (08-packaging.md, section 6): Windows' own, not an account"),
    ("ui/src-tauri/src/trayicon.rs", r"Software\\Microsoft\\Windows",
     "the registry key where Windows keeps the taskbar's light or dark theme, which the "
     "tray's icon follows (09-app-and-setup.md, item 4): Windows' own, not an account"),
]

# The names left, per file: (count, the item of step 5 that removes them).
# Empty since item 13: nothing outside `modules/` names a provider but what
# ALLOWED gives a reason for. A count above zero here is a step back.
NAMED: dict[str, tuple[int, str]] = {}


def _named(text: str) -> int:
    """How many provider names `text` holds: a name begins a word, or a
    capital begins it inside one (`useGoogle`)."""
    count = 0
    for found in _NAME.finditer(text):
        at = found.start()
        if at == 0 or not text[at - 1].isalpha() or text[at].isupper():
            count += 1
    return count


def _docstrings(tree: ast.AST) -> set[tuple[int, int]]:
    where = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                where.add((first.lineno, first.col_offset))
    return where


_STRINGS = {tokenize.NAME, tokenize.STRING}
_STRINGS |= {getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}


def _python(text: str) -> list[str]:
    """A Python file's names and strings, less its docstrings and comments."""
    docs = _docstrings(ast.parse(text))
    return [t.string for t in tokenize.generate_tokens(io.StringIO(text).readline)
            if t.type in _STRINGS and not (t.type == tokenize.STRING and t.start in docs)]


def _uncommented(text: str, marks: tuple[str, ...]) -> list[str]:
    """Each line up to its comment, the comment lines dropped."""
    kept = []
    for line in text.splitlines():
        if line.strip().startswith(marks + ("*",) if "//" in marks else marks):
            continue
        for mark in marks:
            line = line.split(f" {mark}", 1)[0]
        kept.append(line)
    return kept


def _read(rel: str, text: str) -> list[str]:
    if rel.endswith(".py"):
        return _python(text)
    if rel.endswith(".md") or rel.endswith(".json"):
        return text.splitlines()
    if rel.endswith((".yaml", ".yml", ".toml")):
        return _uncommented(text, ("#",))
    return _uncommented(text, ("//", "/*"))


@cache
def counts() -> dict[str, int]:
    """Every file read, with how many provider names it holds."""
    found = {}
    for rel in repo_files.listed():
        if not (rel.startswith(READ) and rel.endswith(SUFFIXES)):
            continue
        allowed = [pattern for path, pattern, _why in ALLOWED if rel == path]
        n = 0
        for part in _read(rel, repo_files.text(rel) or ""):
            for pattern in allowed:
                part = re.sub(pattern, "", part, flags=re.IGNORECASE)
            n += _named(part)
        if n:
            found[rel] = n
    return found


def test_no_file_outside_modules_names_a_provider_more_than_its_entry_allows():
    """A new name outside `modules/` is a provider written back into the
    code around the slot, which a module the user adds cannot stand in."""
    over = {rel: (n, NAMED.get(rel, (0, ""))[0]) for rel, n in counts().items()
            if n > NAMED.get(rel, (0, ""))[0]}

    assert not over, (
        "provider names outside modules/ (found, allowed): "
        f"{over}. Reach the account through the slot (agent/connectors.py); a name "
        "that is not a connector goes in ALLOWED, with its reason.")


def test_every_entry_is_exact_so_the_list_says_what_is_left():
    """A count that fell and was not lowered would let a name come back
    unseen, up to the old count."""
    found = counts()
    stale = {rel: (found.get(rel, 0), n) for rel, (n, _item) in NAMED.items()
             if found.get(rel, 0) != n}

    assert not stale, f"lower these entries in NAMED (found, listed): {stale}"


def test_every_entry_names_the_item_that_removes_it_and_every_allowance_its_reason():
    assert all(re.match(r"items? \d+[\d, and]*: \S", item) for _n, item in NAMED.values())
    assert all(why.strip() for _p, _pattern, why in ALLOWED)


def test_the_reader_skips_comments_and_docstrings_but_not_names_or_strings():
    """The guard's own reading, on a file it would read."""
    text = ('"""Gmail, once."""\n'
            "# Outlook, once\n"
            "def f():\n"
            '    """Google, once."""\n'
            '    return google_cloud.read("Yahoo")  # DreamHost\n')

    assert sum(_named(part) for part in _python(text)) == 2
    assert _named("useGoogle and googleapiclient and a canvasser") == 3
    assert _named("subscanvas") == 0
