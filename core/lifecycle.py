"""A module from outside, from its folder to on, and back again.

A module Steve builds or the user imports is code from outside
(06-modules.md, section 5). With nothing between its folder and its switch,
each of these would happen:

- **Checked, then changed.** Code checked one day would run the next as
  other bytes. Here the files approved are the files checked, by hash,
  copied into a folder of their version that nothing writes after, and a
  module turns on only while that folder still has the hash
  (`check_start`).
- **Approved on its own word.** A module whose description said "reads your
  calendar" could also post it anywhere. The card opens with core's summary
  of what its grants allow (`summary`): what it reads, and where it can send
  it. Its own words come after, marked as its own. One that can send what it
  reads somewhere needs the passphrase, and a new version asks again only if
  it asks for more (`more`), its own words that the model reads among it,
  which its card shows as they were and as they are (`reworded`).
- **A tool's word for itself.** A tool that calls itself a read could still
  send what it reads, or change what its hosts hold. Core decides from the
  grants which calls ask first (`needs_card`), and says so on the card.
- **A bad version, and no way back but removal.** A new version starts only
  after `state/` is copied aside, and rolling back switches to an earlier
  approved version and restores the copy it left (`rollback`). The last
  three versions are kept.
- **Kept on while failing.** A module from outside is watched from each
  start: its calls, and those that failed by its own fault, judged by
  `core/canary.verdict`'s comparison (`noted`). A bad verdict turns it off.
  An account's trouble (a wrong password, a lapsed sign-in, a provider down)
  is not the module's, and is not counted (`outcome`).
- **A name reused.** Replacing an installed module deletes its secrets and
  state first; removing one deletes its sandbox profile, which is named for
  the id, so that the next module of that id inherits nothing granted to it
  (`drop_container`). A built-in's id is never taken.
- **A server's tools taken on trust.** A module with `[mcp]` brings the
  tools a remote server lists, which the server can change at any time.
  They come staged as its `mcp-tools.json`: screened with its other words,
  shown on the card, and hashed tool by tool into its grants, so a changed
  list is a new version whose card names what changed (`more`).

Admission's mechanical checks are here (`stage`): the files' caps, text
only, so no native code that no read of the source would see; the one
manifest parser; a module from outside in the sandbox tier, with no
libraries; and the import check (`core/sandbox/imports.py`). The injection
screen, the review and the tests in the sandbox are the agent's to run
(`agent/modadmit.py`), and come back as the card's verdicts. A pass is not
trust: only the user's approval in the app turns a module on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from core import addons, canary, db, hello, modules, passphrase, paths, skills
from core.engines import mcp as mcp_engine
from core.sandbox import imports
from steve_sdk.errors import ConnectorError
from steve_sdk.ids import CLASSES
from steve_sdk.link import Broken
from steve_sdk.manifest import READ_ONLY, Manifest, ManifestError, Tool, known, load

log = logging.getLogger("lifecycle")

ORIGINS = ("built", "imported")
# How many of a module's program actions its card names, by their paths.
COM_ACTIONS_SHOWN = 8
KEPT = 3
# A module's files: text alone, so that nothing runs that a read of its
# source would not see, and few enough to read.
TEXT = (".py", ".toml", ".md", ".txt", ".json", ".csv")
MAX_FILES = 200
MAX_FILE = 512 * 1024
MAX_TOTAL = 4 * 1024 * 1024
MAX_MANIFEST = 64 * 1024
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# How a call of a module ended, as the watch counts it (`noted`): "excused"
# is a failure the module says was its account's or the network's, and
# "answer" an error that is an answer, not a failure.
OUTCOMES = ("ok", "answer", "excused", "error", "crash", "timeout")
_COUNTED = {"error": "errors", "crash": "crashes", "timeout": "timeouts"}
# A connector's errors that are its account's or the network's, never the
# module's: counted, a lapsed sign-in would turn off the module that said so.
NOT_ITS_OWN = ("sign_in", "password", "not_connected", "transient")
# Errors that are an answer, not a failure.
ANSWERS = ("target_changed", "needs_card", "unsupported")
# But the module names the kind, so one that always said its failures were
# not its own was never turned off (step 11's audit, B4). This many failures
# in a row, of any kind, turn it off; a call that works starts the run again,
# and an answer neither breaks nor adds to it.
FAILED_IN_A_ROW = 10
WITHDRAWN_WATCH = "Steve turned that module off, because it kept failing"


class Refused(modules.AccountError):
    """Why a module was not admitted, or a step on one not taken, said for
    the user."""


# --- what it can do, in core's words ---------------------------------------------

@dataclass(frozen=True)
class Summary:
    reads: tuple[str, ...]
    hosts: tuple[str, ...]
    typed: bool  # a host or a link the user gives it
    passphrase: bool
    lines: tuple[str, ...]


def _hosts(manifest: Manifest) -> tuple[str, ...]:
    presets = (None, *(p.id for p in manifest.setup.presets))
    return tuple(sorted({h for p in presets for h in manifest.fixed_hosts(p)}))


def marks(manifest: Manifest, request: str) -> list[dict[str, str]]:
    """What a module Steve built asks for that the request it was built for
    does not name: each host it may reach, each class of account it serves
    and each it reads, held to the user's words as `skills.trusted_value`
    holds a value. The card shows them beside the request: the coder writes
    the manifest, and a grant the request gave no reason for is the one the
    user, the only person who reads every card, should stop at."""
    asked = ([("host", host) for host in _hosts(manifest)]
             + [("class", klass) for klass in manifest.classes]
             + [("reads", klass) for klass in manifest.reads])
    return [{"what": what, "value": value} for what, value in asked
            if not skills.trusted_value(value, request)]


def _reads(manifest: Manifest) -> tuple[str, ...]:
    """The classes of the user's accounts the module's own code reads: those
    it asks for (`connector.read`), and those it provides with its code
    (`engine = "code"`), whose every item passes through that code. Until
    the review of 15 October only the first counted, so a mail module from
    outside with a second host was carded as reading none of the user's
    mail, with no passphrase, and its code could pass each message there.
    A class on one of core's engines is read by core, not the module."""
    return tuple(c for c in CLASSES if c in manifest.reads
                 or (c in manifest.classes and manifest.specs[c].engine == "code"))


def _typed(manifest: Manifest) -> bool:
    return any(f.type in ("host", "link") or f.form == "link" for f in manifest.setup.fields)


def _listed(words: list[str] | tuple[str, ...]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def needs_card(manifest: Manifest, tool: Tool) -> bool:
    """Whether each call of a module's tool asks the user first, decided
    from the grants rather than the tool's class. A read is free only when
    its module reads none of the user's accounts and declares no method
    beyond GET and HEAD: otherwise what it returns may have carried their
    mail somewhere on the way, or its class may hide a change. An action's
    call is free unless its module reads their accounts, since every call
    runs with GET and HEAD alone (`core/sandbox/run.py`) and what it would
    change goes through `ctx.act`, a card every time. A module that opens
    a program on this computer (`com`) reads what the user keeps there, as
    one that reads their accounts does, and so does one that provides an
    account with its own code (`_reads`)."""
    if _reads(manifest) or manifest.com:
        return True
    if tool.klass != "read":
        return False
    return any(m not in READ_ONLY for said in manifest.methods.values() for m in said)


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary(manifest: Manifest, server_tools: tuple[dict[str, Any], ...] | None = None
            ) -> Summary:
    """What the module's grants let it do, worked out by core from its
    manifest, never taken from what it says of itself. `connector.read`
    together with any host means it can send what it reads there.
    `server_tools` are an `[mcp]` module's, as staged or pinned."""
    reads = _reads(manifest)
    hosts = _hosts(manifest)
    typed = _typed(manifest)
    server = manifest.mcp
    places = [*hosts, *(["a host you give it"] if typed else []),
              *([f"a server on this computer ({server.host}:{server.port})"]
                if server is not None and server.loopback else []),
              *(["any public host"] if server is not None and server.anywhere else [])]
    lines = [f"Reads your {_listed(reads)}." if reads
             else "Reads none of your mail, calendar, files or tasks."]
    if reads and places:
        lines.append(f"Can send your {_listed(reads)} to {_listed(places)}.")
    else:
        lines.append(f"Reaches {_listed(places)}." if places else "Reaches no host.")
    if manifest.sends and places:
        # The module's words, quoted as such: core cannot check them.
        lines.append(f'Says what it sends there: "{manifest.sends}".')
    if manifest.com:
        if programs := [p for p in manifest.com if known(p) is not None]:
            lines.append(f"Opens {_listed(programs)} on this computer and reads there what "
                         f"its manifest lists, which may be your mail, calendar or files.")
        if strange := [p for p in manifest.com if known(p) is None]:
            # Steve cannot say what such a program's members do: approving
            # the module is the user's yes to it (step 11's audit, B1).
            lines.append(f"Opens {_listed(strange)} on this computer, "
                         f"{'a program' if len(strange) == 1 else 'programs'} Steve does not "
                         f"know: it may read anything there, and anything else it does "
                         f"there asks you first, every time.")
        if places:
            lines.append(f"Can send what it reads there to {_listed(places)}.")
        if manifest.com_actions:
            shown = list(manifest.com_actions[:COM_ACTIONS_SHOWN])
            if more_actions := len(manifest.com_actions) - len(shown):
                shown.append(f"{more_actions} more")
            lines.append(f"Anything it would send, save, change or delete there, it asks you "
                         f"first, every time ({_listed(shown)}).")
    if manifest.classes:
        lines.append(f"Can provide {_listed(manifest.classes)} for accounts you add; what it "
                     f"changes there goes through Steve's checks, as a built-in's does.")
    if manifest.tools:
        names = [t.name for t in manifest.tools]
        lines.append(f"Gives Steve the tool{'s' if len(names) > 1 else ''} {_listed(names)}.")
        if asked := [t.name for t in manifest.tools if needs_card(manifest, t)]:
            lines.append(f"Steve asks you before each use of {_listed(asked)}.")
        if any(t.klass == "action" for t in manifest.tools):
            lines.append("Anything it would change, it asks you first, every time.")
    if server is not None:
        listed = server_tools or ()
        labelled = sum(1 for t in listed if mcp_engine.reads(t))
        if server.local:
            lines.append(f"Gives Steve the {_count(len(listed), 'tool')} of the MCP server "
                         f"{server.package}, {labelled} of them labelled by the server as "
                         f"reads. Steve installs it and runs it in a container of its own, "
                         f"where it reads none of your files and holds its own sign-in; "
                         f"it starts only from the files whose hash this card shows.")
        else:
            lines.append(f"Gives Steve the {_count(len(listed), 'tool')} of the MCP server "
                         f"at {server.url}, {labelled} of them labelled by the server as "
                         f"reads.")
        lines.append("Steve asks you before each use of a tool it labels as a write; once in "
                     "a conversation you are in, for one it labels as a read; and before "
                     "every use when no one is watching.")
    if manifest.prompt:
        lines.append("Adds its own section to Steve's instructions, shown below, marked as "
                     "the module's words.")
    if manifest.skills:
        titles = [s.title for s in manifest.skills]
        lines.append(f"Gives Steve the skill{'s' if len(titles) > 1 else ''} "
                     f"{_listed(titles)}: steps it may follow, granting nothing.")
    if manifest.attention:
        lines.append(f"Looks at {manifest.attention.looks_at} {_every(manifest.attention)}, "
                     "with no one watching, for what is new; Steve decides what you hear, "
                     "reading the module's own paragraph, shown below.")
    if manifest.routines:
        titles = [r.title for r in manifest.routines]
        lines.append(f"Runs the routine{'s' if len(titles) > 1 else ''} {_listed(titles)} on "
                     "its own schedule, with no one watching; anything a run would do, it "
                     "asks you first.")
    # A server that may reach any host could send what it is given anywhere,
    # for every version (06-modules.md, section 7).
    needs = (bool((reads or manifest.com) and places)
             or bool(server is not None and server.anywhere))
    if needs:
        lines.append("Approving it needs your passphrase.")
    return Summary(reads=reads, hosts=hosts, typed=typed, passphrase=needs, lines=tuple(lines))


def grants(manifest: Manifest, server_tools: tuple[dict[str, Any], ...] | None = None
           ) -> dict[str, Any]:
    """What approving the module grants, as kept with its version and
    compared with the next one's: an `[mcp]` module's server, and each of
    its tools by fingerprint, so a tool that changed is named; and each of
    its own words the model reads, by digest, so words that changed are
    named too (`words`)."""
    hosts = _hosts(manifest)
    return {"reads": sorted(_reads(manifest)), "hosts": list(hosts), "typed": _typed(manifest),
            "methods": {h: sorted(manifest.methods_for(h)) for h in hosts},
            "secrets": sorted(f.name for f in manifest.setup.fields if f.type == "secret"),
            "classes": sorted(manifest.classes),
            "tools": sorted(t.name for t in manifest.tools),
            "skills": sorted(s.title for s in manifest.skills),
            "prompt": bool(manifest.prompt),
            "attention": manifest.attention.looks_at if manifest.attention else "",
            "routines": sorted(r.title for r in manifest.routines),
            "com": sorted(manifest.com), "com_members": sorted(manifest.com_members),
            "com_actions": sorted(manifest.com_actions),
            "server": _server_print(manifest),
            "server_tools": mcp_engine.prints(list(server_tools or ())),
            "words": {where: _digest(text) for where, text in words(manifest).items()}}


def words(manifest: Manifest) -> dict[str, str]:
    """Each of the module's own texts that the model reads, by what the
    card calls it: its name, its section for Steve's instructions, its
    tools' descriptions, its skills, its paragraph for the judge and its
    routines' instructions. A routine told to send the day's mail where it
    had said the tides needs no grant it did not have: its words are what
    steer the grants it has. So a version is compared on them as well,
    since by title alone a routine rewritten was the same routine, and its
    version asked for nothing more and owed no passphrase (`more`)."""
    found = {"its name": manifest.label}
    if manifest.prompt:
        found["its section for Steve's instructions"] = manifest.prompt
    for tool in manifest.tools:
        found[f"the tool {tool.name}"] = "\n".join(
            [tool.description, *(f"{p.name}: {p.description}" for p in tool.params)])
    for skill in manifest.skills:
        found[f"the skill {skill.title}"] = "\n".join(
            [_skill_text(skill), f"tools: {', '.join(skill.tools)}"])
    if manifest.attention:
        found["its paragraph for the judge"] = (f"{manifest.attention.looks_at}\n"
                                                f"{manifest.attention.judge}")
    for routine in manifest.routines:
        found[f"the routine {routine.title}"] = routine.instruction
    return found


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def reworded(old: Manifest | None, new: Manifest, had: dict[str, str]
             ) -> list[dict[str, str]]:
    """Each of `new`'s own words that are new, or not as the version
    installed had them (`had`, its grants' digests), with what they were
    and what they are now: the card of a version shows them, so the user
    reads them before any passphrase. What they were is read from `old`,
    the version installed, which `modules.known` gives only while its files
    are the ones approved; else it is left blank."""
    was = words(old) if old is not None else {}
    return [{"where": where, "was": was.get(where, "") if where in had else "", "now": text}
            for where, text in words(new).items()
            if had.get(where) != _digest(text) and (text or was.get(where))]


def _every(attention) -> str:
    minutes = attention.every_min
    if minutes % 60:
        return f"every {minutes} minutes"
    return "every hour" if minutes == 60 else f"every {minutes // 60} hours"


_WORDS = {"reads": "reads your {}", "hosts": "reaches {}", "secrets": "the secret {}",
          "classes": "provides {}", "tools": "the tool {}", "skills": "the skill {}",
          "routines": "the routine {}", "com": "opens {}",
          "com_members": "the program's member {}", "com_actions": "the program's action {}"}


def more(old: dict[str, Any], new: dict[str, Any], *, changed: bool = True) -> list[str]:
    """What `new` grants that `old` did not, in words; [] when nothing.
    `changed` names a server's tool whose fingerprint differs, which is
    not said again among what a version no longer grants."""
    found = [_WORDS[key].format(name) for key in _WORDS
             for name in sorted(set(new.get(key, [])) - set(old.get(key, [])))]
    if new["typed"] and not old.get("typed"):
        found.append("reaches a host you give it")
    if new.get("prompt") and not old.get("prompt"):
        found.append("adds to Steve's instructions")
    if new.get("attention") and not old.get("attention"):
        found.append(f"looks at {new['attention']} for Steve's attention")
    for host, methods in new["methods"].items():
        if host in old.get("hosts", []):
            had = old.get("methods", {}).get(host, [])
            found += [f"{m} to {host}" for m in methods if m not in had]
    if new.get("server") and new.get("server") != old.get("server"):
        found.append(f"reaches the MCP server at {new['server']}")
    pinned = old.get("server_tools", {})
    for name, printed in sorted(new.get("server_tools", {}).items()):
        if name not in pinned:
            found.append(f"the server's tool {name}")
        elif changed and pinned[name] != printed:
            found.append(f"a change to the server's tool {name}")
    # Words of something it had already; something new is named above.
    had = old.get("words", {})
    found += [f"{where}, in new words" for where, digest in new.get("words", {}).items()
              if changed and where in had and had[where] != digest]
    return found


def _tool_text(tool: dict[str, Any]) -> str:
    return json.dumps({k: tool.get(k) for k in ("title", "description", "inputSchema",
                                                 "outputSchema") if tool.get(k)},
                      ensure_ascii=False)


def texts(manifest: Manifest) -> str:
    """The module's words for the user -- its name, what it is for, its
    setup -- each with where it is, for the injection screen as outside
    text: none of them has any business addressing a model."""
    setup = manifest.setup
    parts = [("label", manifest.label), ("for", manifest.for_), ("adds", manifest.adds),
             ("effort", manifest.effort), ("sends", manifest.sends),
             *((f"preset {p.id}", p.label) for p in setup.presets),
             *((f"field {f.name}", f.label) for f in setup.fields),
             *(("guide", g.text) for g in setup.guide),
             *((f"error {i}", f"{e.says} / {e.tell}") for i, e in enumerate(setup.errors))]
    return _lines(parts)


def instructions(manifest: Manifest, server_tools: tuple[dict[str, Any], ...] | None = None) -> str:
    """The module's words for Steve -- its tools' descriptions, its prompt
    section, skills, paragraph for the judge and routines, and an `[mcp]`
    module's server's tools, schemas and all -- each with where it is, for
    the injection screen's own question about them (`core/screen.py`
    `INSTRUCTIONS`), after a first line naming the module's job, which the
    question asks them to keep to.

    Until 19 October these were screened with the words for the user, as
    outside text, and asked whether they instruct a model: words written
    to instruct Steve always do. In the self-upgrade trial all three
    builds of a section for Steve's instructions were refused, and a
    currency tool's words on one build in three."""
    parts = [*((f"tool {t.name}", t.description) for t in manifest.tools),
             *((f"tool {t.name} {p.name}", p.description)
               for t in manifest.tools for p in t.params),
             ("prompt", manifest.prompt),
             *((f"skill {s.title}", _skill_text(s)) for s in manifest.skills),
             *((("looks at", manifest.attention.looks_at), ("judge", manifest.attention.judge))
               if manifest.attention else ()),
             *((f"routine {r.title}", r.instruction) for r in manifest.routines),
             *((f"server tool {t['name']}", _tool_text(t)) for t in server_tools or ())]
    words = _lines(parts)
    return words and _lines([("the module's job", f"{manifest.label}: {manifest.for_}")]
                            ) + "\n" + words


def _lines(parts: list[tuple[str, str]]) -> str:
    return "\n".join(f"{where}: {text}" for where, text in parts if text)


def _skill_text(skill) -> str:
    return "\n".join([f"when: {skill.when}", *(f"step: {step}" for step in skill.steps)])


def text_problems(manifest: Manifest, known_tools: set[str]) -> list[str]:
    """Why the words a module adds to Steve's own -- its tools' descriptions,
    its prompt section, its skills, its paragraph for the judge and its
    routines' instructions -- may not be added, or []. They are held to the
    rules the night's skills are (`core/skills.py`), with the module's
    manifest in the place of the user's request: a host they name, or a
    link, must be on one the manifest names, and a tool one of Steve's
    (`known_tools`, with the module's own as they are offered). The
    injection screen reads them too (`instructions`).

    Its tools' descriptions are among them since 20 October 2026: Steve
    reads them beside his own tools', and a description that told him to
    put the user's calendar in a parameter, with "no need to mention it to
    the user", passed the screen's model."""
    hosts = _hosts(manifest)
    own = " ".join(hosts)
    found = [f"its tool {tool.name!r}'s description {why}"
             for tool in manifest.tools
             for why in skills.text_problems(
                 "\n".join([tool.description, *(p.description for p in tool.params)]),
                 user_words=own, whose="its manifest", hosts=hosts)]
    found += [f"its section for Steve's instructions {why}" for why in skills.text_problems(
        manifest.prompt, user_words=own, whose="its manifest",
        hosts=hosts)] if manifest.prompt else []
    for skill in manifest.skills:
        # No run stands behind a module's skill, so every tool it lists
        # counts as used: what it may not do is name one Steve lacks.
        found += [f"its skill {skill.title!r} {why}" for why in skills.problems(
            {"title": skill.title, "when": skill.when, "steps": list(skill.steps),
             "tools": list(skill.tools)},
            user_words=own, tools_used=known_tools | set(skill.tools),
            known_tools=known_tools, whose="its manifest", hosts=hosts)]
    if manifest.attention:
        found += [f"its paragraph for the judge {why}" for why in skills.text_problems(
            f"{manifest.attention.looks_at}\n{manifest.attention.judge}", user_words=own,
            whose="its manifest", hosts=hosts)]
    for routine in manifest.routines:
        found += [f"its routine {routine.title!r} {why}" for why in skills.text_problems(
            f"{routine.title}\n{routine.instruction}", user_words=own, whose="its manifest",
            hosts=hosts)]
    return found


# --- admission -----------------------------------------------------------------

@dataclass(frozen=True)
class Staged:
    """A module's files copied aside and through the mechanical checks,
    waiting for the agent's."""

    token: str
    folder: Path
    manifest: Manifest
    origin: str
    kind: str  # new, version (Steve's next version of its own), replace
    hash: str
    code: bool
    server_tools: tuple[dict[str, Any], ...] | None = None  # an [mcp] module's, staged


def _runs_code(folder: Path, manifest: Manifest) -> bool:
    """Whether the module has code of its own to review and test: a code
    file its manifest names, or any Python outside its `tests/`. Until 20
    October 2026 any Python at all counted, so a skill whose builder wrote
    tests reading its manifest had those tests reviewed as its code, and
    the self-upgrade trial's reviewer refused two sound skills for them."""
    return bool(manifest.code) or any(
        path.relative_to(folder).parts[0] != "tests" for path in folder.rglob("*.py"))


def runs_code(folder: Path) -> bool:
    """`_runs_code` for a draft: whether the coding pipeline's second
    reading has code to write tests for (`core/coder/pipeline.py`). A draft
    whose manifest cannot be read is taken to; admission says what is wrong
    with it."""
    try:
        return _runs_code(folder, _manifest(folder))
    except (Refused, OSError):
        return True


def _server_tools(folder: Path) -> tuple[dict[str, Any], ...]:
    try:
        return tuple(mcp_engine.pinned(folder))
    except ConnectorError as exc:
        raise Refused(str(exc)) from None


def stage(conn: sqlite3.Connection, source: Path, *, origin: str,
          replace: bool = False, again: bool = False, version_of: str = "") -> Staged:
    """Copy the module in `source` aside and check what core checks
    without running it, or Refused saying why not. `replace` is the user's
    pick to replace an installed module of the same id; `again`, core's
    next version of one it holds, as a server's tools pinned again;
    `version_of`, the module a build was asked to make a new version of.
    Otherwise an installed module's id is refused."""
    if origin not in ORIGINS:
        raise ValueError(f"not an origin: {origin!r}")
    token = secrets.token_hex(8)
    folder = paths.staged_dir() / token
    try:
        _copy(source, folder)
        manifest = _manifest(folder)
        kind = _kind(conn, manifest, origin=origin, replace=replace, again=again,
                     version_of=version_of)
        _imports(folder, manifest)
        _here(folder)
        return Staged(token=token, folder=folder, manifest=manifest, origin=origin, kind=kind,
                      hash=addons.tree_hash(folder), code=_runs_code(folder, manifest),
                      server_tools=_server_tools(folder) if manifest.mcp else None)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def walk(folder: Path, rel: PurePosixPath):
    """Every entry under `folder` by its name under `rel`, with its stat, or
    None for a folder; Refused at the first link (export walks it too)."""
    # By hand, not rglob: a link or a junction is refused before anything
    # under it is read, wherever it points.
    with os.scandir(folder) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            info = entry.stat(follow_symlinks=False)
            name = rel / entry.name
            if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & _REPARSE:
                raise Refused(f"{name} is a link, which could point anywhere")
            if entry.is_dir(follow_symlinks=False):
                yield name, None
                yield from walk(Path(entry.path), name)
            else:
                yield name, info


def _copy(source: Path, into: Path) -> None:
    if not (source / "manifest.toml").is_file():
        raise Refused("it has no manifest.toml at its top")
    into.mkdir(parents=True)
    files = total = 0
    for name, info in walk(source, PurePosixPath()):
        if "__pycache__" in name.parts:
            continue  # what running its tests outside the sandbox left
        if info is None:
            (into / name).mkdir()
            continue
        if Path(name.name).suffix.lower() not in TEXT:
            raise Refused(f"{name}: a module holds text files only ({', '.join(TEXT)})")
        files += 1
        total += info.st_size
        if info.st_size > MAX_FILE:
            raise Refused(f"{name} is over {MAX_FILE // 1024} KB")
        if files > MAX_FILES or total > MAX_TOTAL:
            raise Refused(f"it has more than {MAX_FILES} files or {MAX_TOTAL >> 20} MB")
        shutil.copyfile(source / name, into / name)


def _runs_a_server(manifest: Manifest) -> bool:
    """Whether a profile of the module names a package for Steve to install
    and run (its `[package]`). The review of steps 5 and 6 (15 October)
    copied `outlook_mcp` as a module from outside and added a host to its
    profile's package: admission took it, its card showed no host and asked
    no passphrase, and the account's sign-in would have installed and run
    that package. Putting the package on the card would not be enough, since
    nothing checks a package a module names (its age, its code) the way
    Steve checks its own. So a module from outside may reach a remote
    server only. A profile that does not load names nothing: the engine
    finds no profile either, and runs no server."""
    for klass in manifest.classes:
        if manifest.specs[klass].engine != "mcp":
            continue
        try:
            if any(p.package is not None for p in mcp_engine.profiles(manifest, klass)):
                return True
        except (ConnectorError, OSError):
            continue
    return False


def _manifest(folder: Path) -> Manifest:
    path = folder / "manifest.toml"
    if path.stat().st_size > MAX_MANIFEST:
        raise Refused(f"its manifest is over {MAX_MANIFEST // 1024} KB")
    try:
        manifest = load(path)
    except ManifestError as exc:
        raise Refused(f"its manifest: {str(exc).replace(str(folder), '')}") from None
    if manifest.tier != "sandbox":
        raise Refused("it asks to run in the backend, which no module from outside does")
    if manifest.libraries:
        raise Refused("it declares libraries; a module from outside has the standard "
                      "library and steve_sdk alone")
    if _runs_a_server(manifest):
        raise Refused("a profile of it names an MCP server for Steve to install and run; "
                      "Steve runs only the servers of the modules it ships")
    if manifest.id in sys.stdlib_module_names or manifest.id == "steve_sdk":
        raise Refused(f"its id {manifest.id!r} is a module of Python's own, which it would hide")
    return manifest


def _imports(folder: Path, manifest: Manifest) -> None:
    if found := imports.problems(folder, own=manifest.id):
        raise Refused("its code imports what a module may not: " + "; ".join(found[:10]))


def _here(folder: Path) -> None:
    """Refused if a file names a folder of this computer's: the user's
    home, Steve's data or Steve's code. A module runs in a sandbox that
    reaches none of them, and a module shared would carry the user's name
    with it. In the self-upgrade trial (19 October 2026) builds asked about
    a file on this computer wrote its path into their code and tests, and
    one of them was approved."""
    places = (("your home folder", Path.home()), ("Steve's data", paths.data_dir()),
              ("Steve's code", paths.SOURCE))
    # The whole folder: C:\Users\Ana is not inside C:\Users\Anabel, nor
    # C:\Users\Ana.old. A full stop that ends a sentence ends the path:
    # excluded too until CI's first run (22 October 2026), "Run from
    # D:\a\steve\steve." named nothing.
    found = [(name, re.compile(re.escape(_flat(str(place))) + r"(?![\w-]|\.\w)"))
             for name, place in places]
    # The files admission copies (`_copy`): a draft's bytecode holds the
    # path it was compiled at, and is never kept.
    for path in sorted(p for p in folder.rglob("*") if p.is_file()
                       and p.suffix.lower() in TEXT and "__pycache__" not in p.parts):
        text = _flat(path.read_text(encoding="utf-8", errors="replace"))
        for name, place in found:
            if place.search(text):
                raise Refused(f"{path.relative_to(folder).as_posix()} names {name} on this "
                              "computer, which a module can neither reach nor take elsewhere")


def _flat(text: str) -> str:
    """One spelling of a path: as written, escaped in a string, or with
    forward slashes; Windows' paths are not case-sensitive."""
    return text.replace("\\\\", "\\").replace("/", "\\").casefold()


def screened(folder: Path, conn: sqlite3.Connection | None = None, *,
             version_of: str = "") -> str | None:
    """What admission would refuse in `folder` from reading it alone, its
    manifest and its imports, or None; with `conn`, whether a build may
    take its id too (`_kind`, with `version_of` as `stage` has it). The
    coding pipeline runs it among its checks (`agent/modbuild.py`), so the
    coder hears of it while a round can still fix it. In the
    module-building eval's first run (15 October) a coder's manifest gave a
    tool no class; the judge approved, admission refused, and the build was
    lost."""
    if not (folder / "manifest.toml").is_file():
        return "it has no manifest.toml at its top"
    try:
        manifest = _manifest(folder)
        if conn is not None:
            _kind(conn, manifest, origin="built", replace=False, version_of=version_of)
        _imports(folder, manifest)
        _here(folder)
    except Refused as exc:
        return str(exc)
    return None


def _kind(conn: sqlite3.Connection, manifest: Manifest, *, origin: str, replace: bool,
          again: bool = False, version_of: str = "") -> str:
    # A new version takes over its module's secrets, state and switch, so
    # only the caller may say a module is one: a build the user asked for
    # as a new version of that module (`version_of`), or core's next
    # version of one it holds (`again`). Taken from the id alone, a build
    # whose coder named a new module like one built before became its
    # version, and the new code inherited the old one's key.
    module_id = manifest.id
    if version_of and module_id != version_of:
        raise Refused(f"it was to be a new version of {version_of!r}, but its manifest names "
                      f"{module_id!r}: a new version keeps its module's id")
    row = conn.execute("SELECT origin FROM modules WHERE id = ?", (module_id,)).fetchone()
    if row is None:
        if version_of:
            raise Refused(f"the module {version_of!r} is not installed any more")
        return "new"
    if row[0] == "builtin":
        raise Refused(f"{module_id!r} is a built-in module's id")
    if replace:
        return "replace"
    if (again or version_of) and row[0] == origin:
        if conn.execute("SELECT 1 FROM module_versions WHERE module = ? AND version = ?",
                        (module_id, manifest.version)).fetchone():
            raise Refused(f"version {manifest.version} of {module_id} is kept already")
        return "version"
    raise Refused(f"a module {module_id!r} is installed already: replace it, which deletes "
                  f"its secrets and data first, or give this one another id")


def _current_grants(conn: sqlite3.Connection, module_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT v.grants_json FROM modules m JOIN module_versions v "
                       "ON v.module = m.id AND v.version = m.version WHERE m.id = ?",
                       (module_id,)).fetchone()
    return json.loads(row[0]) if row else None


def record(conn: sqlite3.Connection, staged: Staged, *, review: dict[str, Any] | None,
           tests: str | None, request: str | None = None,
           came_from: str | None = None, user_said: str | None = None) -> int:
    """Keep a module that passed admission's checks as a card waiting for
    the user, in the card's order; its id. Any card waiting for the same
    module id is superseded. `review` and `tests` are the agent's verdicts
    (None for a module with no code); `request` and `came_from`, for a
    module Steve built, what it was asked in the words it was asked, and
    where; `user_said`, the user's own last message where they asked, shown
    beside the request because the model wrote that."""
    manifest = staged.manifest
    said = summary(manifest, staged.server_tools)
    granted = grants(manifest, staged.server_tools)
    changes = None
    needs = said.passphrase
    if staged.kind == "version" and (old := _current_grants(conn, manifest.id)) is not None:
        changes = {"more": more(old, granted), "fewer": more(granted, old, changed=False),
                   "words": reworded(modules.known(conn).get(manifest.id), manifest,
                                     old.get("words", {}))}
        needs = needs and bool(changes["more"])
    card = {
        "id": manifest.id, "version": manifest.version, "origin": staged.origin,
        "kind": staged.kind, "summary": list(said.lines), "passphrase": needs,
        "request": request, "came_from": came_from, "user_said": user_said,
        "marks": marks(manifest, request) if request is not None else None,
        "own_words": {"label": manifest.label, "for": manifest.for_, "adds": manifest.adds,
                      "effort": manifest.effort, "sends": manifest.sends,
                      "prompt": manifest.prompt,
                      "skills": [{"title": s.title, "when": s.when, "steps": list(s.steps)}
                                 for s in manifest.skills],
                      "attention": {"looks_at": manifest.attention.looks_at,
                                    "judge": manifest.attention.judge,
                                    "every_min": manifest.attention.every_min}
                      if manifest.attention else None,
                      "routines": [{"title": r.title, "instruction": r.instruction,
                                    "at": r.at, "days": list(r.days)}
                                   for r in manifest.routines]},
        "changes": changes,
        "server": _server_card(manifest, staged.server_tools or ()) if manifest.mcp else None,
        "details": {
            "manifest": (staged.folder / "manifest.toml").read_text(encoding="utf-8"),
            "hash": staged.hash, "hosts": list(said.hosts), "typed": said.typed,
            "files": sorted(p.relative_to(staged.folder).as_posix()
                            for p in staged.folder.rglob("*") if p.is_file()),
            "review": review, "tests": tests,
        },
        "grants": granted,
    }
    with db.transaction(conn):
        older = [r[0] for r in conn.execute(
            "SELECT staged FROM module_admissions WHERE module = ? AND state = 'waiting'",
            (manifest.id,)).fetchall()]
        conn.execute("UPDATE module_admissions SET state = 'superseded', decided_at = "
                     "datetime('now') WHERE module = ? AND state = 'waiting'", (manifest.id,))
        cur = conn.execute(
            "INSERT INTO module_admissions (module, version, origin, kind, staged, hash, "
            "card_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (manifest.id, manifest.version, staged.origin, staged.kind, staged.token,
             staged.hash, json.dumps(card)))
    for token in older:
        shutil.rmtree(paths.staged_dir() / token, ignore_errors=True)
    return int(cur.lastrowid)


def _server_print(manifest: Manifest) -> str:
    """What of a server, changed, asks again: its address; or the package,
    the files installed and whether it may reach anywhere."""
    server = manifest.mcp
    if server is None:
        return ""
    if server.local:
        return f"{server.package} {server.tree} {'anywhere' if server.anywhere else ''}"
    return server.url


def _server_card(manifest: Manifest, tools: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """An `[mcp]` module's server and every tool it listed, in its own
    words (screened), each with the server's label and whether Steve can
    offer it under a model's tool name."""
    assert manifest.mcp is not None

    def said(value: Any) -> str:
        return value if isinstance(value, str) else ""

    server = manifest.mcp
    local = {}
    if server.local:
        from core.sandbox import servers

        local = {"package": server.package, "tree": server.tree, "anywhere": server.anywhere,
                 "packages": servers.counted(manifest.id, server.package),
                 "holds_its_sign_in": True}
    return {"url": server.url, "loopback": server.loopback, "secret": server.secret, **local,
            "tools": [{"name": t["name"], "title": said(t.get("title")),
                       "description": said(t.get("description")),
                       "read": mcp_engine.reads(t),
                       "offered": mcp_engine.offered_as(manifest.id, t["name"]) is not None}
                      for t in tools]}


def discard(staged: Staged) -> None:
    """Delete a staged module that the agent's checks refused."""
    shutil.rmtree(staged.folder, ignore_errors=True)


def waiting(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The cards waiting for the user, oldest first."""
    rows = conn.execute("SELECT id, made_at, card_json FROM module_admissions "
                        "WHERE state = 'waiting' ORDER BY id").fetchall()
    return [{"admission": r[0], "made_at": r[1], **json.loads(r[2])} for r in rows]


def decline(conn: sqlite3.Connection, admission: int) -> None:
    """The user's no: the card closes and the files go."""
    row = _waiting(conn, admission)
    with db.transaction(conn):
        conn.execute("UPDATE module_admissions SET state = 'declined', decided_at = "
                     "datetime('now') WHERE id = ?", (admission,))
    shutil.rmtree(paths.staged_dir() / row[3], ignore_errors=True)
    if conn.execute("SELECT 1 FROM modules WHERE id = ?", (row[0],)).fetchone() is None:
        # A server the user added was installed, and started once to list
        # its tools, before its card: declined, its files and its container
        # are no one's.
        from core.sandbox import servers

        servers.forget(row[0], servers.MODULE_KEY)
        servers.remove(paths.module_dir(row[0]))


def _waiting(conn: sqlite3.Connection, admission: int) -> tuple:
    row = conn.execute("SELECT module, version, origin, staged, kind, hash, card_json, state "
                       "FROM module_admissions WHERE id = ?", (admission,)).fetchone()
    if row is None or row[7] != "waiting":
        raise Refused("that module is no longer waiting for your card")
    return row


def hello_binding(conn: sqlite3.Connection, admission: int) -> str | None:
    """What a Windows Hello signature for this card signs: the card and the
    hash of the files that were checked, as `monitor.hello_binding` binds an
    action's card. Named as a module's, so a yes signed for action 5 is no
    yes to module card 5. None for a card no longer waiting."""
    row = conn.execute("SELECT hash FROM module_admissions WHERE id = ? AND state = 'waiting'",
                       (admission,)).fetchone()
    return None if row is None else f"module {admission}:{row[0]}"


def approve(conn: sqlite3.Connection, admission: int, *, given: str = "",
            signed: tuple[str, str] | None = None) -> str:
    """The user's yes, in the app: install the module's files as they were
    checked, as a version of their own, and return its id. A new module
    stays off until the user turns it on, after entering any secrets it
    names; a new version of one keeps its switch, and starts after its
    data is copied aside. Where the card needs the passphrase, `given` is
    it, or `signed` is Windows Hello's (nonce, signature) over this card's
    challenge."""
    module_id, version, origin, token, kind, digest, card_json, _ = _waiting(conn, admission)
    card = json.loads(card_json)
    folder = paths.staged_dir() / token
    if not folder.is_dir() or addons.tree_hash(folder) != digest:
        raise Refused("its files changed after they were checked: import or build it again")
    if card["passphrase"]:
        # The binding is the digest just checked against the files, so a
        # glance at one version's card approves no other.
        why = (hello.verify(*signed, "yes", f"module {admission}:{digest}") if signed is not None
               else passphrase.check(given))
        if why is not None:
            raise Refused(why)
    row = conn.execute("SELECT origin, version FROM modules WHERE id = ?",
                       (module_id,)).fetchone()
    if row is not None and row[0] == "builtin":
        raise Refused(f"{module_id!r} is a built-in module's id")
    if kind == "new" and row is not None:
        raise Refused(f"a module {module_id!r} was installed meanwhile")
    if kind == "version" and (row is None or row[0] != origin):
        raise Refused(f"{module_id!r} is not installed as it was when you were asked")
    if kind == "replace" and row is not None:
        modules.remove_module(conn, module_id)
        row = None
    target = paths.module_code_dir(module_id, version)
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(folder, target)
    if addons.tree_hash(target) != digest:
        shutil.rmtree(target, ignore_errors=True)
        raise Refused("its files did not copy whole: try again")
    if row is not None:
        _copy_aside(module_id, row[1])
    with db.transaction(conn):
        if row is None:
            conn.execute("INSERT INTO modules (id, key, version, tier, origin, enabled) "
                         "VALUES (?, ?, ?, 'sandbox', ?, 0)",
                         (module_id, modules.new_key(), version, origin))
        else:
            before = counts(conn, module_id)
            conn.execute("UPDATE module_versions SET copied_at = datetime('now') "
                         "WHERE module = ? AND version = ?", (module_id, row[1]))
            conn.execute("UPDATE modules SET version = ? WHERE id = ?", (version, module_id))
            rewatch(conn, module_id, before=before)
        conn.execute("INSERT INTO module_versions (module, version, hash, grants_json) "
                     "VALUES (?, ?, ?, ?)", (module_id, version, digest,
                                             json.dumps(card["grants"])))
        conn.execute("UPDATE module_admissions SET state = 'approved', decided_at = "
                     "datetime('now') WHERE id = ?", (admission,))
    shutil.rmtree(folder, ignore_errors=True)
    _prune(conn, module_id)
    return module_id


def _copy_aside(module_id: str, version: str) -> None:
    state = paths.module_dir(module_id) / "state"
    copy = paths.module_copy_dir(module_id, version)
    shutil.rmtree(copy, ignore_errors=True)
    if state.is_dir():
        shutil.copytree(state, copy)
    else:
        copy.mkdir(parents=True)


def _prune(conn: sqlite3.Connection, module_id: str) -> None:
    rows = conn.execute("SELECT id, version FROM module_versions WHERE module = ? "
                        "ORDER BY id DESC", (module_id,)).fetchall()
    for row_id, version in rows[KEPT:]:
        conn.execute("DELETE FROM module_versions WHERE id = ?", (row_id,))
        _drop_version(module_id, version)


def _drop_version(module_id: str, version: str) -> None:
    shutil.rmtree(paths.module_code_dir(module_id, version), ignore_errors=True)
    shutil.rmtree(paths.module_copy_dir(module_id, version), ignore_errors=True)


# --- versions ------------------------------------------------------------------

def versions(conn: sqlite3.Connection, module_id: str) -> list[dict[str, Any]]:
    """A module from outside's kept versions, newest first: when each was
    approved, and for each before the current one, the date its data would
    return to."""
    current = conn.execute("SELECT version FROM modules WHERE id = ?", (module_id,)).fetchone()
    rows = conn.execute("SELECT version, approved_at, copied_at FROM module_versions "
                        "WHERE module = ? ORDER BY id DESC", (module_id,)).fetchall()
    return [{"version": v, "approved_at": a, "data_from": c,
             "current": current is not None and v == current[0]} for v, a, c in rows]


def rollback(conn: sqlite3.Connection, module_id: str, version: str) -> str:
    """Switch back to an earlier approved version, its data restored to the
    copy taken when it was left; the versions after it go. Returns the
    time that copy was taken, which the app says before."""
    row = conn.execute("SELECT origin, version FROM modules WHERE id = ?",
                       (module_id,)).fetchone()
    if row is None or row[0] == "builtin":
        raise Refused("only a module from outside has versions to go back to")
    if version == row[1]:
        raise Refused(f"version {version} is the one it runs")
    held = conn.execute("SELECT id, hash, copied_at FROM module_versions "
                        "WHERE module = ? AND version = ?", (module_id, version)).fetchone()
    if held is None or held[2] is None:
        raise Refused(f"no version {version} of {module_id} is kept to go back to")
    code = paths.module_code_dir(module_id, version)
    copy = paths.module_copy_dir(module_id, version)
    if not code.is_dir() or addons.tree_hash(code) != held[1]:
        raise Refused(f"version {version}'s files are not the ones you approved")
    if not copy.is_dir():
        raise Refused(f"the copy of its data from {held[2]} is gone")
    later = [r[0] for r in conn.execute("SELECT version FROM module_versions WHERE module = ? "
                                        "AND id > ?", (module_id, held[0])).fetchall()]
    state = paths.module_dir(module_id) / "state"
    shutil.rmtree(state, ignore_errors=True)
    shutil.copytree(copy, state)
    with db.transaction(conn):
        conn.execute("DELETE FROM module_versions WHERE module = ? AND id > ?",
                     (module_id, held[0]))
        conn.execute("UPDATE module_versions SET copied_at = NULL WHERE id = ?", (held[0],))
        conn.execute("UPDATE modules SET version = ? WHERE id = ?", (version, module_id))
        rewatch(conn, module_id)
    for gone in later:
        _drop_version(module_id, gone)
    shutil.rmtree(copy, ignore_errors=True)
    return str(held[2])


def check_start(conn: sqlite3.Connection, module_id: str) -> None:
    """Refused unless a module from outside's current version is one the
    user approved, with its files as they were then. Nothing for a
    built-in, which its release fixes."""
    row = conn.execute("SELECT origin, version FROM modules WHERE id = ?",
                       (module_id,)).fetchone()
    if row is None or row[0] == "builtin":
        return
    held = conn.execute("SELECT hash FROM module_versions WHERE module = ? AND version = ?",
                        (module_id, row[1])).fetchone()
    if held is None:
        raise Refused(f"{module_id} has no version you approved")
    folder = paths.module_code_dir(module_id, row[1])
    if not folder.is_dir() or addons.tree_hash(folder) != held[0]:
        raise Refused(f"{module_id}'s files are not the ones you approved: roll it back, "
                      f"or remove it")


def remove(conn: sqlite3.Connection, module_id: str, *, forget: bool = False) -> None:
    """The user's removal of a module (`modules.remove_module`). A built-in
    is turned off, never removed: the next start would install it again."""
    row = conn.execute("SELECT origin FROM modules WHERE id = ?", (module_id,)).fetchone()
    if row is None:
        raise Refused(f"no module {module_id!r} is installed")
    if row[0] == "builtin":
        raise Refused("a built-in module can be turned off, not removed")
    modules.remove_module(conn, module_id, forget=forget)


def drop_container(module_id: str) -> None:
    """Delete the module's sandbox profile, and with it whatever was
    granted to its name."""
    from core.sandbox import profiles

    try:
        profiles.delete(profiles.for_module(module_id))
    except OSError:
        log.warning("could not delete %s's sandbox profile", module_id, exc_info=True)


# --- watching ------------------------------------------------------------------

def rewatch(conn: sqlite3.Connection, module_id: str,
            before: canary.Counts | None = None) -> None:
    """Start a module from outside's count afresh, judged against `before`
    (the version before's) or, after a plain start, the canary's default."""
    row = conn.execute("SELECT origin FROM modules WHERE id = ?", (module_id,)).fetchone()
    if row is None or row[0] == "builtin":
        return
    conn.execute(
        "INSERT INTO module_watch (module, before_json) VALUES (?, ?) ON CONFLICT(module) "
        "DO UPDATE SET since = datetime('now'), calls = 0, errors = 0, crashes = 0, "
        "timeouts = 0, failed_in_a_row = 0, before_json = excluded.before_json",
        (module_id, json.dumps(before.as_dict()) if before is not None else None))


def counts(conn: sqlite3.Connection, module_id: str) -> canary.Counts | None:
    """A watched module's count since it started, as the canary reads one."""
    row = conn.execute("SELECT calls, errors, crashes, timeouts FROM module_watch "
                       "WHERE module = ?", (module_id,)).fetchone()
    if row is None:
        return None
    calls, *bad = row
    by_kind = {k: n for k, n in zip(("errors", "crashes", "timeouts"), bad, strict=True) if n}
    return canary.Counts(turns=calls, bad=sum(bad), by_kind=by_kind)


def outcome(error: BaseException | None = None, *, timed_out: bool = False) -> str | None:
    """How a call ended, as the watch counts it (`OUTCOMES`)."""
    if timed_out:
        return "timeout"
    if error is None:
        return "ok"
    if isinstance(error, Broken):
        return "crash"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectorError):
        if error.kind in NOT_ITS_OWN:
            return "excused"
        if error.kind in ANSWERS:
            return "answer"
    return "error"


def noted(conn: sqlite3.Connection, module_id: str, ended: str | None) -> str | None:
    """Count one call of a watched module, by `outcome`. If that makes the
    canary's verdict bad, or ends `FAILED_IN_A_ROW` failures in a row, turn
    the module off, withdraw its pending cards and stop its servers, as
    switching it off does (`modules.set_enabled`); return why, which the
    caller tells the user. Otherwise None. Turned off here, it once kept its
    servers running until they idled out, still holding their accounts'
    sign-ins and connections."""
    if ended is None:
        return None
    if ended not in OUTCOMES:
        raise ValueError(f"not an outcome: {ended!r}")
    column = _COUNTED.get(ended)
    bump = ", ".join(
        (["calls = calls + 1"] if ended != "excused" else [])
        + ([f"{column} = {column} + 1"] if column else [])
        + (["failed_in_a_row = 0"] if ended == "ok" else
           [] if ended == "answer" else ["failed_in_a_row = failed_in_a_row + 1"]))
    with db.transaction(conn):
        if not conn.execute(f"UPDATE module_watch SET {bump} WHERE module = ?",
                            (module_id,)).rowcount:
            return None
        row = conn.execute("SELECT before_json, failed_in_a_row FROM module_watch "
                           "WHERE module = ?", (module_id,)).fetchone()
        if row[1] >= FAILED_IN_A_ROW:
            why = f"its last {row[1]} calls failed"
        elif column is None:
            return None
        else:
            before = canary.Counts.from_dict(json.loads(row[0])) if row[0] else None
            after = counts(conn, module_id)
            why = canary.verdict(before, after, unit="calls") if after is not None else None
        if why is None or not conn.execute(
                "UPDATE modules SET enabled = 0 WHERE id = ? AND enabled = 1",
                (module_id,)).rowcount:
            return None
        modules.withdraw_cards(conn, modules.keys_of(conn, module_id), WITHDRAWN_WATCH)
    from core.sandbox import servers

    servers.POOL.stop_module(module_id)
    return why
