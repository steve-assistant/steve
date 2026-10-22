"""Rewrite what only developers read so that it names no one: a one-off.

The comments, docstrings and docs were written by one person about his own
agent, by first name and by pronoun: "<Name> wants", "his calendar", "he
said yes". Some 3,000 lines say so, across every package. Edited by hand,
that many lines invite a slip that changes code; edited by a word list,
they invite grammar a reader trips on. So this tool touches only comment
tokens and docstrings (found with `tokenize` and `ast`), Markdown prose
outside code, and comments in YAML and SQL, and it proves the rest is
untouched: a module's AST with its docstrings removed must be identical
before and after, and a YAML file must load to the same data.

The substitutions keep the grammar without conjugating a verb, which is
where a tool goes wrong: <Name> -> the user, <Name>'s -> the user's,
he -> the user, he's -> the user is, he'd -> the user would, his -> their,
him -> them, himself -> themselves. "He wants" becomes "The user wants",
and nothing else in the sentence moves. "His" becomes "theirs" only where
nothing but punctuation or a conjunction follows it ("it is his."): a list
of words that "may" follow a standalone "his" once turned "his runs" into
"theirs runs".

Where the "he" may be someone else, it flags rather than guesses: a pronoun
in quoted or reported speech ("the note says that he..."), "he or she", a
sentence that also names a person or a person-noun ("the contact said he'd
call back"), a "he's" or "he'd" whose next word could make it "has" or
"had". It flags the user's name, too, where it is not the user acting,
since "Hi the user," is worse than the name: in a greeting or a sign-off,
addressing the user, as a cited example, or as an ordinary word the name
also is. Those are listed for a person to settle, along with any of the
user's own names it did not replace.

Who the user is comes from the people map (`private/people_map.tsv`,
git-ignored because it holds the names): its `# --- the user` section,
whose full names and first name become "the user" and whose other entries
(a surname, an address) are flagged. Without the map the tool stops: a run
that knew no name would rewrite the pronouns, find no name, and look clean.
The map's other sections swap real people, firms and places for the
fictional cast's.

Docstrings the model may read are left alone, because they were rewritten
by hand with the prompts. No reading of the AST can tell them all apart (a
schema may subclass BaseModel through another class; a tool may be
registered by name), so only two kinds count as a developer's: a module's
own, and an undecorated private function's. A class's or a public
function's is left, and so is every docstring in a module that sets a
`__doc__`; `core/coder/tools.py`, which registers its tools by name, is
left whole. Text in `code` spans is never touched: it names code, which a
later step renames.

Every tracked text file it does not rewrite (TypeScript, Rust, PowerShell,
TOML, and so on) is listed under SKIPPED, and the run fails if one of them
names someone in the map: a file the tool never read is where a name would
survive unseen.

    python -m devtools.depersonalise PATH ... [--write] [--diff] [--map FILE]

Without --write it changes nothing and prints what it would change.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import io
import re
import subprocess
import sys
import tokenize
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP = ROOT / "private" / "people_map.tsv"

# Files a later step deletes or rewrites whole: polishing them is wasted
# (step 7: the wake-word samples and the rehearsal scripts).
LATER = ("voice/wake_train.py", "devtools/rehearse/",
         "devtools/depersonalise.py", "devtools/people_sweep.py",
         # What the model reads was rewritten by hand with the prompts.
         "agent/prompts/", "core/coder/prompts/")

# Left whole, and listed as skipped: its tools are registered by name
# through `getattr` (`tool_plain(name=...)`), so nothing in a function's
# own AST says its docstring is a tool's description.
HANDS_OFF = ("core/coder/tools.py",)

# The pronoun, its contraction, and the verb after it when that verb is one
# of the four whose form follows "they" by a fixed rule (`_BE`). The user's
# names go in front of the pronouns (`People.word`).
_PRONOUNS = r"[Hh]imself|[Hh]im|[Hh]is|[Hh]e"
_TAIL = r"(?:['’](s|d|ll))?(?:(\s+)(is|was|has|does)(?![\w'’-]))?(?![\w-])"
_JOINED = re.compile(r"\b(?:and|or|but)\s+(?:\w+ly\s+)?(?:\w+[^s\W]s|is|has|was|does)\b")
_BE = {"is": "are", "was": "were", "has": "have", "does": "do"}
# The next word, across a comment's line break or a held `code` span.
_NEXT = re.compile(r"(?:\s|#|//|\*)*(\x00|[A-Za-z]+)")
# After "he's" or "he'd", a word that may make it "has" or "had".
_PERFECT = re.compile(r"(?:\w+ed|been|seen|done|gone|given|taken|written|gotten|got|had|"
                      r"made|said|sent|told|left|set|put|read|heard|met|already|just|"
                      r"never|ever|always|since|not)$")
# A second "he" in one sentence is "they" -- "the user said the user would"
# reads as two people -- but only before a word whose form is the same after
# "they": a modal, or a past tense. Before anything else it stays "the
# user", because "he owes" -> "they owe" is the conjugating a tool gets wrong.
_THEY = re.compile(r"(?:\w+ed|will|would|can|could|may|might|must|should|shall|won|"
                   r"wouldn|couldn|shouldn|didn|said|wrote|had|did|got|made|sent|saw|"
                   r"knew|thought|told|left|came|went|gave|took|kept|read|put|set|met|"
                   r"heard|found|meant|ran|sat|spent|chose|spoke|woke|held|let|lost|"
                   r"paid|built|brought|bought|felt|began|forgot)$")
# After "his", the words that leave it standing alone ("his and nobody
# else's"). Before any other word "his" owns it: "his runs" are their runs,
# whatever else "runs" can be.
_JOINS = {"and", "or", "but", "nor"}
# ...except these, which no possessive stands before ("a note of his is",
# "the call is his to make", "his to-do"): "their" would be wrong and
# "theirs" a guess, so the line is flagged.
_NO_NOUN_AFTER = {
    "to", "is", "was", "are", "were", "has", "had", "have", "does", "did", "can", "could",
    "will", "would", "shall", "should", "may", "might", "must", "not", "alone", "too",
    "either", "that", "which", "who", "in", "at", "on", "with", "from", "for", "as", "so",
    "since", "when", "where", "while", "said", "says", "wrote",
}
# Reported speech: "the note says that he..." is about whoever it reports.
_REPORTED = re.compile(r"\b(?:said|says|wrote|writes|asked|asks|told(?:\s+\w+)?)\s+that\s+$")
# Who else a "he" in the same sentence might be: a man by his relation, a
# role a contact has, or another pronoun ("he or she", "his/her": anyone at
# all). "The sender" and "the contact" were once left out, as nearly always
# the user; then "the contact said he'd call back" became the user's
# promise, and nothing showed it. A flag costs a glance; a wrong rewrite is
# read as true.
_PERSONS = {
    "man", "guy", "father", "dad", "brother", "son", "husband", "boyfriend", "uncle",
    "grandfather", "classmate", "interviewer", "boss", "coach", "mentor", "advisor",
    "adviser", "sender", "contact", "caller", "organiser", "organizer", "guest",
    "recipient", "attendee", "host", "landlord", "doctor", "friend", "colleague",
    "manager", "recruiter", "professor", "teacher", "partner",
    "she", "her", "hers", "herself",
}
_CAST = {"Theo", "Dana", "Sam", "Alex", "Ortiz", "Kim", "Marsh"}
# Before the user's name, what makes it something other than the user
# acting: a greeting or a sign-off, a cited example, a determiner ("the
# <Name> font" uses the name as a word).
_GREETING = re.compile(r"(?i)(?:\b(?:hi|hello|hey|dear|thanks|thank you)[,!]?"
                       r"|\b(?:morning|evening|cheers|best|regards|love|yours|sincerely)[,!])"
                       r"[ \t]*$")
_CITED = re.compile(r"(?i)(?:(?:\be\.g\.|\bi\.e\.),?|\bnamed|\bcalled)[ \t]*$")
_DETERMINER = re.compile(r"(?i)\b(?:a|an|the|this|these|those|every|each|any|some|no)[ \t]+$")
_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)
_OPENERS = re.compile(r"[\s\"“‘(\[*_]*$")
# A sentence ends at a stop, but not at the one in "e.g." or "i.e.": read as
# an end, "the recruiter calls, e.g. he rang" lost the recruiter.
_STOPS = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\bcf)(?<!\bvs)[.!?;](?=\s)"
                    r"|\n[ \t]*(?:#+|--|//|\*)?[ \t]*(?:[-+]|\d+[.)])\s|\n\s*\n")
_ABBREVIATED = re.compile(r"\b(?:e\.g|i\.e|cf|vs)\.$")
_MARKS = re.compile(r"(?m)^[ \t]*(?:#+|--|//|/?\*+)[ \t]?")
_BULLET = re.compile(r"[ \t]*(?:#+|--|//|/?\*+)?[ \t]*(?:[-+]|\d+[.)])")


def _alternatives(names: list[str]) -> str:
    return "|".join(map(re.escape, sorted(names, key=len, reverse=True)))


@dataclass
class People:
    """Who the user is, who else is real, and the words that make a "he"
    ambiguous."""
    swaps: dict[str, str] = field(default_factory=dict)
    # The user's names that become "the user": full names and the first name.
    user: list[str] = field(default_factory=list)
    # The rest of the user's section (a surname, an address): flagged.
    others: list[str] = field(default_factory=list)
    names: set[str] = field(default_factory=lambda: set(_CAST))

    def word(self) -> re.Pattern:
        """The user's names and the pronouns, each with what may follow.
        Not after a slash: the "he" in "s/he" is anyone, not the user."""
        names = _alternatives(self.user)
        return re.compile(r"(?<![\w'’/-])(" + (names + "|" if names else "") + _PRONOUNS + ")"
                          + _TAIL)

    def subject(self) -> re.Pattern:
        """The user as a sentence's subject, by pronoun or by name."""
        names = _alternatives(self.user)
        return re.compile(r"(?<![\w'’-])(?:[Hh]e|[Tt]he user" + ("|" + names if names else "")
                          + r")(?![\w-])")

    def own(self) -> re.Pattern | None:
        """Any of the user's names or addresses, as written."""
        names = _alternatives(self.user + self.others)
        return re.compile(rf"(?<!\w)(?:{names})(?!\w)") if names else None

    def mentioned(self, text: str) -> bool:
        """Whether `text` names anyone in the map, the user included, in any
        case and even inside an identifier."""
        names = _alternatives([*self.swaps, *self.user, *self.others])
        return bool(names) and re.search(rf"(?i)(?<![A-Za-z])(?:{names})(?![A-Za-z])",
                                         text) is not None

    def swap(self, text: str) -> str:
        """Every real name in `text` for its stand-in, in one pass, the
        longest first. Applied one line of the map after another, a
        stand-in was read again by the lines below it: "Ingrid" became
        "Ivy", and then "Lowe, Ivy" matched, so Ingrid Lowe came out as the
        stand-in for someone else."""
        if not self.swaps:
            return text
        found = re.compile(r"(?<!\w)(?:" + _alternatives(list(self.swaps)) + r")(?!\w)")
        return found.sub(lambda m: self.swaps[m.group()], text)


def people(path: Path) -> People:
    """Read the map: real -> stand-in per line, sections headed `# --- `.

    The user's section is not swapped (dev text says "the user", not the
    tests' persona): its full names and their first word become "the user",
    and the rest of it is flagged wherever it survives. A missing map, or
    one with no user, is an error rather than a run that finds no name."""
    if not path.exists():
        raise FileNotFoundError(f"no people map at {path}: the user's names come from its "
                                f"'# --- the user' section")
    found = People()
    section, reverse, own = "", {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ---"):
            section = line[5:].strip()
            continue
        if not line.strip() or line.startswith("#") or "\t" not in line:
            continue
        real, stand_in = (part.strip() for part in line.split("\t", 1))
        if section == "the user":
            own.append(real)
            continue
        found.swaps[real] = stand_in
        if section.startswith("people"):
            found.names.update(w for w in (real + " " + stand_in).split()
                               if w[:1].isupper() and w.isalpha())
            # Exchange writes "Lowe, Ingrid": each full name in that
            # order too, unless the map says otherwise.
            if len(real.split()) == len(stand_in.split()) == 2 and "," not in real:
                (first, last), (first2, last2) = real.split(), stand_in.split()
                reverse.setdefault(f"{last}, {first}", f"{last2}, {first2}")
    for real, stand_in in reverse.items():
        found.swaps.setdefault(real, stand_in)
    full = [n for n in own if len(n.split()) > 1
            and all(re.fullmatch(r"[A-Z][\w'’-]*", w) for w in n.split())]
    found.user = sorted({*full, *(n.split()[0] for n in full)}, key=len, reverse=True)
    found.others = [n for n in own if n not in found.user]
    if not found.user:
        raise ValueError(f"{path} names no user: its '# --- the user' section needs the "
                         f"user's full name")
    return found


@dataclass
class Result:
    text: str
    flags: list[tuple[int, str]]


def _starts_sentence(before: str) -> bool:
    """Whether a word after `before` begins a sentence, a bullet or a
    paragraph (a blank line, or a comment line with nothing on it)."""
    kept = _OPENERS.sub("", before)
    if _BULLET.fullmatch(kept.rsplit("\n", 1)[-1]):
        return True
    flat = _OPENERS.sub("", _MARKS.sub("", kept))
    gap = _OPENERS.search(_MARKS.sub("", before)).group()
    return (not flat or (flat[-1] in ".!?" and not _ABBREVIATED.search(flat))
            or gap.count("\n") >= 2)


def _sentence(text: str, at: int) -> str:
    """The sentence (or bullet) around `at`."""
    start, end = 0, len(text)
    for m in _STOPS.finditer(text):
        if m.end() <= at:
            start = m.end()
        elif m.start() >= at:
            end = m.start() + 1
            break
    return text[start:end]


def _sentence_start(text: str, at: int) -> int:
    start = 0
    for m in _STOPS.finditer(text, 0, at):
        start = m.end()
    return start


def _quoted(text: str, at: int) -> bool:
    before = text[:at]
    return before.count('"') % 2 == 1 or before.count("“") > before.count("”")


def _ambiguous(sentence: str, names: set[str]) -> bool:
    words = re.findall(r"[A-Za-z]+", _MARKS.sub(" ", sentence))
    return any(w.lower() in _PERSONS for w in words) or any(w in names for w in words)


def _not_the_user(work: str, start: int, end: int, tail: str | None) -> str:
    """Why the user's name at `start` is not the user acting, or "". "Hi
    <Name>," rewritten is "Hi the user,"; a name that is also a word ("the
    <Name> font") rewritten is nonsense."""
    line_start = work.rfind("\n", 0, start) + 1
    line_end = work.find("\n", end)
    before = _MARKS.sub("", work[line_start:start])
    after = work[end:] if line_end < 0 else work[end:line_end]
    if _GREETING.search(before) or (not before.strip(" \t-—–") and not after.strip()):
        return "in a greeting or a sign-off"
    if after.startswith(",") and re.search(r"(?i)\byour?s?\b", _sentence(work, start)):
        return "used to address the user"
    if _CITED.search(before):
        return "cited as an example"
    if _DETERMINER.search(before) or re.match(r"[ \t]+[a-z]+[ \t]+(?:are|were)\b", after):
        return "as a word"
    prev = re.search(r"[A-Z][\w'’-]*[ \t]+$", before)
    if prev and not _starts_sentence(work[:start - len(prev.group())]):
        return "inside another name"
    if not tail and re.match(r"[ \t]+[A-Z]", after):
        return "inside another name"
    return ""


def rewrite(text: str, who: People, line0: int = 1) -> Result:
    """`text` with the user named "the user" and real people swapped, and
    the lines a person must settle. `line0` is the line `text` starts on."""
    held: list[str] = []

    def keep(kept: str) -> str:
        """Hide `kept` from every later pass, to be put back at the end: a
        `code` span, or a word left for a person (flagged once, here, not
        again as a name that survived)."""
        held.append(kept)
        return f"\x00{len(held) - 1}\x00"

    work = _CODE.sub(lambda m: keep(m.group(0)), text)
    flags: list[tuple[int, str]] = []
    subject = who.subject()

    def line_of(at: int) -> int:
        # Counted with the spans put back: a held `code` span may cross a line.
        return line0 + _restore(work[:at], held).count("\n")

    def flag(at: int, why: str) -> None:
        # Put back before the spaces are folded: a `code` span may hold a
        # line break, and a flag is one line.
        sentence = _restore(_MARKS.sub(" ", _sentence(work, at)), held)
        flags.append((line_of(at), f"{why}: {' '.join(sentence.split())}"))

    def again(at: int, end: int) -> bool:
        """Whether the user is already the subject earlier in this sentence,
        and no verb later in it is joined to this one ("they are about and
        has" is what follows a "they" given one verb and not the next)."""
        later = work[end:end + 80].split(".")[0]
        return (bool(subject.search(work, _sentence_start(work, at), at))
                and not _JOINED.search(later))

    def swap(m: re.Match) -> str:
        word, tail, gap, verb = m.groups()
        lower = word.lower()
        end = m.end(2) if tail else m.end(1)
        pronoun, rest = work[m.start():end], (gap or "") + (verb or "")
        nxt = _NEXT.match(work, end)
        following = nxt.group(1).lower() if nxt else ""

        def cap(out: str) -> str:
            return out[0].upper() + out[1:] if word[0].isupper() else out

        def leave(why: str) -> str:
            flag(m.start(), why)
            return keep(m.group(0))

        if _quoted(work, m.start()):
            return leave("quoted")
        if word in who.user:
            why = _not_the_user(work, m.start(), end, tail)
            if why:
                return leave(f"the user's name {why}?")
            out = "the user" + pronoun[len(word):]
            out = out[0].upper() + out[1:] if _starts_sentence(work[:m.start()]) else out
            return out + rest
        if _REPORTED.search(_MARKS.sub(" ", work[_sentence_start(work, m.start()):m.start()])):
            return leave("reported speech: someone else?")
        if _ambiguous(_sentence(work, m.start()), who.names):
            return leave("someone else?")
        if lower == "he":
            if tail in ("s", "d") and _PERFECT.match(following):
                return leave(f"he'{tail}: has/had?")
            if tail is None and again(m.start(), m.end()):
                if verb:
                    return cap("they") + gap + _BE[verb]
                out = "they" if _THEY.match(following) else "the user"
            elif tail == "d" and again(m.start(), m.end()):
                out = "they would"
            else:
                out = {"s": "the user is", "d": "the user would", "ll": "the user will",
                       None: "the user"}[tail]
        elif lower == "his":
            # "*his*" and "his/" stress or pair it; neither says whose.
            if work[end:end + 1] in ("/", "*") or work[m.start() - 1:m.start()] == "*":
                return leave("his: whose?")
            if not nxt:
                # Only punctuation or the end follows: "it is his." A quote
                # or a number after it is what it owns: his "why?".
                out = "their" if re.match(r"\s*[\"“‘(\d]", work[end:]) else "theirs"
            elif following in _JOINS:
                out = "theirs"
            elif following in _NO_NOUN_AFTER:
                return leave("his: their or theirs?")
            else:
                out = "their"
        else:
            out = {"him": "them", "himself": "themselves"}[lower]
        return cap(out) + rest

    work = who.word().sub(swap, work)
    work = who.swap(work)
    own = who.own()
    for m in own.finditer(work) if own else ():
        flag(m.start(), "the user's name")
    return Result(_restore(work, held), flags)


def _restore(text: str, held: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], text)


# --- Python ---------------------------------------------------------------

def _copies_doc(tree: ast.AST) -> bool:
    """Whether the module sets a `__doc__` (`look.__doc__ = _note.__doc__`,
    `setattr(f, "__doc__", ...)`): any docstring in it may become a tool's."""
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Attribute, ast.Name)) and isinstance(node.ctx, ast.Store)
                and getattr(node, "attr", getattr(node, "id", "")) == "__doc__"):
            return True
        if isinstance(node, ast.Constant) and node.value == "__doc__":
            return True
    return False


def _model_reads(node: ast.AST, copied: bool) -> bool:
    """Whether the model may be shown this node's docstring.

    Decorators and bases once decided it, and missed a schema that
    subclasses BaseModel through another class, a dataclass or Enum handed
    to the model, and a tool registered by name. So only what cannot be a
    tool or a schema is a developer's: the module's own docstring and an
    undecorated private function's (`_name`, not `__dunder__`), and not
    even those in a module that sets a `__doc__`."""
    if copied:
        return True
    if isinstance(node, ast.Module):
        return False
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        private = node.name.startswith("_") and not node.name.endswith("__")
        return not (private and not node.decorator_list)
    return True


def _docstrings(tree: ast.AST) -> dict[tuple[int, int], bool]:
    """Where each docstring starts, and whether the model may read it."""
    found = {}
    copied = _copies_doc(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                found[(first.value.lineno, first.value.col_offset)] = _model_reads(node, copied)
    return found


def _bare(tree: ast.AST) -> str:
    """The tree's dump with every docstring blanked: what must not change."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                first.value.value = ""
    return ast.dump(tree)


def python(source: str, who: People) -> Result:
    tree = ast.parse(source)
    docs = _docstrings(tree)
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def at(pos: tuple[int, int]) -> int:
        return starts[pos[0] - 1] + pos[1]

    edits: list[tuple[int, int, str]] = []
    flags: list[tuple[int, str]] = []
    block: list[tokenize.TokenInfo] = []

    def close() -> None:
        if block:
            done = rewrite("\n".join(t.string for t in block), who, block[0].start[0])
            flags.extend(done.flags)
            for tok, new in zip(block, done.text.split("\n"), strict=True):
                if new != tok.string:
                    edits.append((at(tok.start), at(tok.end), new))
            block.clear()

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            if block and block[-1].start[0] != tok.start[0] - 1:
                close()
            block.append(tok)
            continue
        if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            close()
        reads = docs.get(tok.start) if tok.type == tokenize.STRING else None
        if reads and who.mentioned(tok.string):
            # Left alone, a name there would otherwise go unreported.
            flags.append((tok.start[0], "a name in a docstring the model may read, left as it is"))
        if reads is False:
            quote = re.match(r"[rRuUbB]*(\"\"\"|'''|\"|')", tok.string).group(0)
            body = tok.string[len(quote):len(tok.string) - len(quote.lstrip("rRuUbB"))]
            done = rewrite(body, who, tok.start[0])
            flags.extend(done.flags)
            if done.text != body:
                if "'" in quote and "'" in done.text and "'" not in body:
                    flags.append((tok.start[0], "a quote the string's own quotes would end"))
                    continue
                new = quote + done.text + quote.lstrip("rRuUbB")
                edits.append((at(tok.start), at(tok.end), new))
    close()

    out = source
    for start, end, new in sorted(edits, reverse=True):
        out = out[:start] + new + out[end:]
    if _bare(ast.parse(out)) != _bare(ast.parse(source)):
        raise ValueError("the code changed, not just its comments and docstrings")
    return Result(out, flags)


# --- Markdown, YAML, SQL ----------------------------------------------------

def markdown(source: str, who: People) -> Result:
    """Prose paragraphs rewritten; fenced code left as it is."""
    out: list[str] = []
    flags: list[tuple[int, str]] = []
    para: list[str] = []
    para_line = 0
    fence = ""

    def close() -> None:
        if para:
            done = rewrite("".join(para), who, para_line)
            out.append(done.text)
            flags.extend(done.flags)
            para.clear()

    for n, line in enumerate(source.splitlines(keepends=True), 1):
        mark = re.match(r"\s*(```+|~~~+)", line)
        if fence or mark:
            close()
            out.append(line)
            if mark and not fence:
                fence = mark.group(1)
            elif mark and mark.group(1).startswith(fence):
                fence = ""
            continue
        if not line.strip():
            close()
            out.append(line)
            continue
        if not para:
            para_line = n
        para.append(line)
    close()
    return Result("".join(out), flags)


# Where a line's comment starts (-1: none), given what was left open at the
# end of the line before; and what is left open at the end of this one.
Finder = Callable[[str, str], tuple[int, str]]


def _comments(source: str, who: People, find: Finder) -> Result:
    """Each line's comment, from where `find` says it starts, rewritten in
    blocks of consecutive lines; everything before it left as it is."""
    lines = source.splitlines(keepends=True)
    flags: list[tuple[int, str]] = []
    block: list[tuple[int, int]] = []
    state = ""

    def close() -> None:
        if block:
            olds = [lines[i][at:].rstrip("\n") for i, at in block]
            done = rewrite("\n".join(olds), who, block[0][0] + 1)
            flags.extend(done.flags)
            for (i, at), old, new in zip(block, olds, done.text.split("\n"), strict=True):
                lines[i] = lines[i][:at] + new + lines[i][at + len(old):]
            block.clear()

    for i, line in enumerate(lines):
        at, state = find(line, state)
        if at < 0:
            close()
        else:
            block.append((i, at))
    close()
    return Result("".join(lines), flags)


def _yaml_comment(line: str, state: str) -> tuple[int, str]:
    """A whole-line `#` comment. One after data is left: telling it from a
    `#` inside a quoted value takes a YAML parser."""
    text = line.lstrip()
    return (len(line) - len(text) if text.startswith("#") else -1), state


def _sql_comment(line: str, state: str) -> tuple[int, str]:
    """Where `line`'s `--` comment starts, after code or alone. `state` is
    what is open: a 'string', a "quoted name" or a /* comment */, in which
    `--` is data -- rewritten, it would change the schema."""
    i = 0
    while i < len(line):
        if state == "/*":
            if line.startswith("*/", i):
                state, i = "", i + 2
                continue
        elif state:
            if line[i] == state:
                state = ""
        elif line.startswith("--", i):
            return i, state
        elif line.startswith("/*", i):
            state, i = "/*", i + 2
            continue
        elif line[i] in "'\"":
            state = line[i]
        i += 1
    return -1, state


def yaml_comments(source: str, who: People) -> Result:
    """Whole-line comments rewritten; refused if the data then loads
    differently (a `#` line inside a block scalar is data)."""
    import yaml
    done = _comments(source, who, _yaml_comment)
    if yaml.safe_load(done.text) != yaml.safe_load(source):
        raise ValueError("the data changed, not just its comments")
    return done


def sql_comments(source: str, who: People) -> Result:
    """`--` comments rewritten, whole-line or after code; strings, quoted
    names and /* comments */ left as they are."""
    return _comments(source, who, _sql_comment)


SUFFIXES: dict[str, Callable[[str, People], Result]] = {
    ".py": python, ".md": markdown, ".yaml": yaml_comments, ".yml": yaml_comments,
    ".sql": sql_comments,
}


def tracked(paths: list[str]) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", *paths], cwd=ROOT, capture_output=True,
                         check=True).stdout.decode("utf-8")
    return [ROOT / p for p in out.split("\0") if p and not p.startswith(LATER)]


def sweep(files: list[Path], root: Path, who: People, *, write: bool = False,
          diff: bool = False) -> int:
    """Rewrite `files` (or, without `write`, say what would change), print
    the flags, and list under SKIPPED every text file it did not rewrite.
    Binary files are passed over quietly. Returns 1 when a skipped file
    names someone in the map, so a name it never saw cannot pass for done."""
    changed = flagged = 0
    skipped: list[tuple[str, str, bool]] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        how = None if rel in HANDS_OFF else SUFFIXES.get(path.suffix)
        if how is None:
            try:
                data = path.read_bytes()
            except OSError as exc:
                skipped.append((rel, f"unreadable: {exc.strerror}", False))
                continue
            if b"\0" not in data[:8192]:
                why = "hands off" if rel in HANDS_OFF else "a kind it does not rewrite"
                skipped.append((rel, why, who.mentioned(data.decode("utf-8", "replace"))))
            continue
        try:
            before = path.read_text(encoding="utf-8")
            done = how(before, who)
        except (ValueError, SyntaxError) as exc:
            print(f"{rel}: NOT CHANGED, {exc}", file=sys.stderr)
            continue
        for line, why in done.flags:
            print(f"{rel}:{line}: {why}")
        flagged += len(done.flags)
        if done.text == before:
            continue
        changed += 1
        if diff:
            sys.stdout.writelines(difflib.unified_diff(
                before.splitlines(keepends=True), done.text.splitlines(keepends=True),
                f"a/{rel}", f"b/{rel}", n=0))
        if write:
            path.write_text(done.text, encoding="utf-8", newline="\n")
    for rel, why, named in skipped:
        print(f"SKIPPED {rel} ({why})" + ("  NAMES SOMEONE IN THE MAP" if named else ""))
    named = sum(n for _, _, n in skipped)
    print(f"{changed} files {'changed' if write else 'to change'}, {flagged} flags, "
          f"{len(skipped)} skipped ({named} naming someone in the map)", file=sys.stderr)
    return 1 if named else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--write", action="store_true", help="change the files")
    parser.add_argument("--diff", action="store_true", help="print each file's diff")
    parser.add_argument("--map", type=Path, default=MAP)
    args = parser.parse_args(argv)

    try:
        who = people(args.map)
    except (OSError, ValueError) as exc:
        print(f"depersonalise: {exc}", file=sys.stderr)
        return 2
    return sweep(tracked(args.paths), ROOT, who, write=args.write, diff=args.diff)


if __name__ == "__main__":
    raise SystemExit(main())
