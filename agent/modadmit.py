"""Admission's checks that need a model or the sandbox, around core's.

Core checks a module's files without running any of them
(`core/lifecycle.stage`). What is left needs the agent, and each step
stands for a failure (06-modules.md, section 5):

- **The screen.** A module's own words reach the card, and its tools'
  descriptions the prompt. A description that tries to instruct a model
  would be read by one beside every tool it has. The screen must pass the
  text as it is; text it could not screen is refused too, to try again.
- **The review.** A second model reads the code and nothing else
  (`core/audit.py`). Its verdict is advice on the card, and says when the
  code was longer than it reads.
- **The tests.** They must pass, in a container of their own, on the
  runtime the module will run on (`core/sandbox/testrun.py`); a module with
  code must have some. On a computer that cannot hold a module's code (D6)
  one with code is refused, and one of text alone is not.

Then core keeps the card (`lifecycle.record`), and only the user's yes in
the app turns a module on. `watched` is the other end: a call's outcome
counted, and the user told when the count turns a module off.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent import offloop
from core import audit, lifecycle, modules, screen
from core.sandbox import testrun
from steve_sdk.manifest import Manifest, ManifestError, load

Screener = Callable[[sqlite3.Connection, screen.Artifact], screen.Verdict]
Reviewer = Callable[[str, str, tuple[str, ...]], Awaitable[audit.Verdict]]
Tester = Callable[[str, Path], testrun.Ran]
Tools = Callable[[], set[str]]


def source_of(folder: Path) -> str:
    """The module's code as the reviewer reads it: each file under its name."""
    return "\n\n".join(f"# --- {path.relative_to(folder).as_posix()}\n"
                       f"{path.read_text(encoding='utf-8')}"
                       for path in sorted(folder.rglob("*.py")))


async def _review(source: str, module_id: str, said: tuple[str, ...]) -> audit.Verdict:
    return await audit.review(source, tool=module_id, capabilities=said)


def _known_tools() -> set[str]:
    from agent import skills

    return skills.known_tools()


def _test(module_id: str, folder: Path) -> testrun.Ran:
    from core.sandbox import probe

    support = probe.verdict()
    if support is None or not support.ok:
        why = support.reason if support is not None else "not checked since this start"
        return testrun.Ran(False, f"this computer cannot run a module's code: {why}")
    return testrun.run(module_id, folder, lpac=support.mode == probe.LPAC)


def _words(manifest: Manifest,
           server_tools: tuple[dict[str, Any], ...] | None = None
           ) -> list[tuple[screen.Artifact, str]]:
    """Its words for the user and its words for Steve, each for the screen,
    with what admission says if the screen will not pass it as it is.
    Screened apart: the first should address no model at all, while the
    second are instructions by design and are asked whether they keep to
    its job."""
    found = []
    for source, words, why in (
            ("module_text", lifecycle.texts(manifest),
             "its words for the user (its name, what it is for, its setup) read as "
             "instructions to a model, which they may not be"),
            (screen.INSTRUCTIONS, lifecycle.instructions(manifest, server_tools),
             "its words for Steve reach past its own job")):
        if words:
            found.append((screen.Artifact(source=source,
                                          source_id=f"{manifest.id}@{manifest.version}",
                                          origin=f"module:{manifest.id}", text=words), why))
    return found


def _broken_rules(manifest: Manifest, tools: Tools) -> str | None:
    """Why its words for Steve break the rules a skill is held to, or None.
    They are meant for the model, so they answer to more than the screen:
    the rules the night's skills are held to, with its manifest in place of
    a request (`lifecycle.text_problems`)."""
    if not (manifest.tools or manifest.prompt or manifest.skills or manifest.attention
            or manifest.routines):
        return None
    from agent.modtools import name_of

    # Its own tools, by the name it gave them or the one Steve offers.
    known = tools() | {n for t in manifest.tools for n in (t.name, name_of(manifest.id, t.name))}
    if found := lifecycle.text_problems(manifest, known):
        return "its words for Steve break the rules a skill is held to: " + "; ".join(found)
    return None


def words_refused(conn: sqlite3.Connection, folder: Path, screener: Screener = screen.check,
                  tools: Tools = _known_tools) -> str | None:
    """What admission would refuse in a draft's words, by the rules a skill
    is held to or by the screen, or None. The coding pipeline runs it among
    its checks (`agent/modbuild.py`), so the coder hears of it while a round
    can still reword them; the screen keeps each verdict, so admission's
    own screen of the same words costs nothing more. In the self-upgrade
    trial's reruns (20 October 2026) a section for Steve's instructions was
    approved and then refused at admission for its words for the user, on
    two builds of two, and an upload for a link in its tool's description:
    a lost build each time. The screen passes a card that says what a section
    does, and refuses one that gives the instruction itself ("whenever you
    mention a time of day..."). Words that could not be screened are
    admission's to refuse, saying to try again: no round can fix that. Run
    after `lifecycle.screened`, which has said why a manifest is not read."""
    try:
        manifest = load(folder / "manifest.toml")
    except (ManifestError, OSError):
        return None
    if broken := _broken_rules(manifest, tools):
        return broken
    for artifact, why in _words(manifest):
        if screener(conn, artifact).verdict not in ("as_is", None):
            return why
    return None


async def admit(conn: sqlite3.Connection, source: Path, *, origin: str, replace: bool = False,
                again: bool = False, version_of: str = "", request: str | None = None,
                came_from: str | None = None, user_said: str | None = None,
                screener: Screener = screen.check, reviewer: Reviewer = _review,
                tester: Tester = _test, tools: Tools = _known_tools) -> int:
    """Run admission on the module in `source`, up to the card; the card's
    admission id. Refused saying why at the first check it fails.
    `replace`, `again` and `version_of` are `lifecycle.stage`'s."""
    staged = await offloop.with_own_conn(conn, lifecycle.stage, source, origin=origin,
                                         replace=replace, again=again, version_of=version_of)
    manifest = staged.manifest
    try:
        for artifact, why in _words(manifest, staged.server_tools):
            verdict = await offloop.with_own_conn(conn, screener, artifact)
            if verdict.verdict != "as_is":
                raise lifecycle.Refused("its own words could not be screened: try again later"
                                        if verdict.verdict is None else why)
        if broken := _broken_rules(manifest, tools):
            raise lifecycle.Refused(broken)
        review: dict[str, Any] | None = None
        tests: str | None = None
        if staged.code:
            code = source_of(staged.folder)
            said = lifecycle.summary(manifest).lines
            found = await reviewer(code, manifest.id, said)
            review = {"approved": found.approved, "reason": found.reason, "model": found.model,
                      "whole": len(code) <= audit.MAX_SOURCE}
            ran = await asyncio.to_thread(tester, manifest.id, staged.folder)
            if not ran.ok:
                raise lifecycle.Refused(f"its tests did not pass in the sandbox: {ran.said}")
            tests = ran.said
        return await offloop.with_own_conn(conn, lifecycle.record, staged, review=review,
                                           tests=tests, request=request, came_from=came_from,
                                           user_said=user_said)
    except BaseException:
        lifecycle.discard(staged)
        raise


async def watched(conn: sqlite3.Connection, module_id: str, ended: str | None,
                  notify: Callable[[str], Awaitable[Any]]) -> str | None:
    """Count one call of a module by how it ended (`lifecycle.outcome`); if
    that turns it off, tell the user why and what they can do. Why, or
    None."""
    why = await offloop.with_own_conn(conn, lifecycle.noted, module_id, ended)
    if why is None:
        return None
    await notify(_turned_off(conn, module_id, why))
    return why


def watched_here(conn: sqlite3.Connection, module_id: str, ended: str | None) -> str | None:
    """`watched`, for a call made in a thread on a connection of its own
    (an attention source's): the user is told through the outbox."""
    from agent import outbox

    why = lifecycle.noted(conn, module_id, ended)
    if why is not None:
        outbox.add(conn, _turned_off(conn, module_id, why), kind="notice")
    return why


def _turned_off(conn: sqlite3.Connection, module_id: str, why: str) -> str:
    manifest = modules.known(conn).get(module_id)
    name = manifest.label if manifest is not None else module_id
    return (f"I turned off {name}, because it kept failing: {why}. On the Extensions page "
            f"you can turn it back on, roll it back to a version before, or remove it.")
