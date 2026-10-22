"""A module's attention source (06-modules.md, section 1): what an enabled
module watches for Steve, asked on its cadence.

The code is a module's, which may be someone else's, and nobody watches it
run. So it runs in the sandbox with GET and HEAD alone, proposes nothing
(`ctx.act` is refused), and what it answers is taken apart here, never
trusted: each item must be a small table of text, cut to size; a link
stays only if it is to a host the module's manifest names, since anything
else is a place the card never showed; and each item passes the injection
screen before the judge reads it, as `module_item` from `module:<id>`. Its
failures count against the module, as a tool call's do, and can turn it
off; the computer's (no sandbox here, the module just turned off) do not.
The judge reads the module's own paragraph about its items, in a block
that names it (`agent/attention/triage.judge_prompt`).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import datetime
from functools import partial
from typing import Any
from urllib.parse import urlsplit

from agent.attention.store import Observation
from agent.cut import cut

log = logging.getLogger(__name__)

PREFIX = "module:"
MAX_KEY = 200
MAX_TITLE = 200
MAX_DETAIL = 1_000


def name_of(module_id: str) -> str:
    """The source's name, which its observations carry and the judge reads."""
    return f"{PREFIX}{module_id}"


def sources(conn: sqlite3.Connection) -> list:
    """A source for each enabled module with an attention source, by module
    id. Blocking: reads the registry."""
    from agent.attention.sources import Source
    from core import modules

    return [Source(name_of(held.id), partial(collect, module_id=held.id), screened,
                   wanted=_sandbox_runs, every=held.manifest.attention.every_min * 60,
                   judge=(held.manifest.attention.looks_at,
                          _paragraph(held.id, held.manifest.label,
                                     held.manifest.attention.judge)))
            for held in modules.modules(conn)
            if held.enabled and held.manifest is not None and held.tier == "sandbox"
            and held.manifest.attention is not None]


def _sandbox_runs(conn: sqlite3.Connection) -> bool:
    from core.sandbox import probe

    support = probe.verdict()
    return support is not None and support.ok


def _paragraph(module_id: str, label: str, judge: str) -> str:
    return (f"**From the module {label}** (items from `{name_of(module_id)}`). The words "
            "below are the module's, which the user approved when they turned it on; they "
            f"are not the user's.\n{judge}")


def collect(conn: sqlite3.Connection, *, module_id: str) -> list[Observation]:
    """What the module has noticed, as observations. Raises SourceError when
    it could not be asked or failed; its own failure is counted first."""
    from agent import modadmit, modreads
    from agent.attention.sources import SourceError
    from core import lifecycle, modctx
    from core.sandbox import run

    try:
        held = run.installed(conn, module_id)
        manifest = held.manifest
        if manifest is None or manifest.attention is None:
            raise lifecycle.Refused(f"{module_id} has no attention source now")
        answer = run.call(conn, manifest, "collect", {},
                          reader=modreads.reader(conn) if manifest.reads else None,
                          actor=None, redact=modctx.scrub)
        found = items(module_id, held.key, manifest, answer)
    except (run.NotHeld, run.NotStarted, lifecycle.Refused) as exc:
        raise SourceError(f"{module_id} could not be asked: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - the module's fault, counted
        modadmit.watched_here(conn, module_id, lifecycle.outcome(
            exc, timed_out=isinstance(exc, run.TimedOut)))
        raise SourceError(f"{module_id}: {type(exc).__name__}: {exc}"[:300]) from exc
    modadmit.watched_here(conn, module_id, lifecycle.outcome())
    return found


def items(module_id: str, module_key: str, manifest, answer: Any) -> list[Observation]:
    """The module's answer as observations: a list of tables, each with a
    `key` and a `title`, and perhaps a `detail`, a `when` and a `link`.
    ValueError for an answer that is not a list; an item that is not a
    table with both is left out.

    Each is keyed `module:<the module's key>:<its key>`, which
    `core/modules.live` reads: switched off, the module's items wait unread
    in the queue, and come back with it. Keyed by the module's id, they
    were core's own to `live`, and the judge went on reading them."""
    if not isinstance(answer, list):
        raise ValueError(f"collect returned {type(answer).__name__}, not a list of items")
    hosts = {h.lower() for h in manifest.fixed_hosts()}
    found: list[Observation] = []
    for item in answer:
        if not isinstance(item, dict):
            continue
        key, title = item.get("key"), item.get("title")
        if not (isinstance(key, str) and key.strip() and isinstance(title, str)
                and title.strip()):
            continue
        detail = item.get("detail")
        found.append(Observation(
            source=name_of(module_id), key=f"{PREFIX}{module_key}:{key.strip()[:MAX_KEY]}",
            title=cut(" ".join(title.split()), MAX_TITLE),
            detail=cut(detail.strip(), MAX_DETAIL) if isinstance(detail, str) else "",
            when=_when(item.get("when")), ref=_ref(item.get("link"), hosts)))
    return found


def _when(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.isoformat(timespec="minutes")


def _ref(link: Any, hosts: set[str]) -> dict:
    if not isinstance(link, str):
        return {}
    parts = urlsplit(link.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() not in hosts:
        return {}
    return {"link": link.strip()}


def screened(conn: sqlite3.Connection, observation: Observation) -> Observation:
    """An item as the judge may read it (agent/screening.py): whoever the
    module read wrote its words. One the screen did not pass is shown as
    what the screen allows, without its link."""
    from agent import screening

    seen = screening.shown_here(conn, "module_item", observation.key, observation.source,
                                f"{observation.title}\n{observation.detail}".strip())
    if seen.how == "text":
        return observation
    return replace(observation, title=f"An item from {observation.source}",
                   detail=seen.text, ref={})
