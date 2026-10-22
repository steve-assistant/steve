"""What the connectors return, recorded, so that moving them changes nothing.

Step 5 moves the mail tools, the calendar, Drive, Tasks and the attention
sources onto a slot, and the code beneath them into `modules/`, over six
commits. Each old test fakes a function the move replaces, so each would
pass against a move that changed what the user sees: a draft's subject, an
event's hour, an observation's key, which the attention loop uses to know
what it has already said. This test drives the tools and one tick of each
source against `tests/unit/golden_world.py`, whose fakes sit at the
services' own seams, and compares everything to the record in
`fixtures/golden/connectors.json`: what each tool returned, what each would
have asked, and what reached each service.

The record is written by running with STEVE_GOLDEN_RECORD=1, once, before
the move. It holds until the step's end; a difference made on purpose is
recorded again in the commit that makes it, and the commit says why.
"""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import importlib
import json
import os
import re
import sys
from base64 import urlsafe_b64decode
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit import golden_world
from tests.unit.golden_world import World

RECORD = Path(__file__).parent / "fixtures" / "golden" / "connectors.json"

# Imported before the clock is pinned, so that each is pinned too.
IMPORTS = (
    "agent.actions", "agent.approvals", "agent.mailsearch", "agent.receipts",
    "agent.toolset.mail", "agent.toolset.calendar", "agent.toolset.drive",
    "agent.toolset.people", "agent.attention.sources", "agent.attention.sources.mail",
    "agent.attention.sources.calendar", "agent.attention.sources.obligations",
    "agent.attention.sources.deadlines", "agent.attention.sources.drive",
    "agent.attention.sources.threads", "modules.google_cloud.gmail",
    "modules.google_cloud.calendar",
    "modules.google_cloud.drive", "modules.google_cloud.tasks", "modules.outlook_classic.outlook",
    "modules.outlook_classic.calendar", "core.engines.imap", "core.engines.ics",
    "tools.commitments",
    "tools.ingest", "tools.people", "tools.obligations", "agent.facts", "agent.zones",
    "agent.attention.sources.health", "agent.attention.sources.news",
    "agent.attention.sources.weather", "agent.attention.sources.modules", "core.monitor",
    "core.passphrase", "core.totp",
    "agent.connectors", "modules.google_cloud.provider", "modules.outlook_classic.provider",
    "agent.calendars", "agent.taskmirror", "agent.toolset.oncard",
)

# The connectors' sources. Health, the weather and the news are not connectors.
SOURCES = ("gmail", "outlook", "home", "calendar", "horizon", "after",
           "commitments", "deadlines", "drive", "threads")

# An id as the slot writes it (`steve_sdk/ids.py`: the class, the account's
# twelve-hex key, the service's own id in base64url), read back to the
# service's own: today's tools hand out that, and the record must hold across
# the change. Outlook's own `mail:outlook:` tag is not one.
_SLOT_ID = re.compile(r"\b(?:mail|calendar|files|tasks):[0-9a-f]{12}:([A-Za-z0-9_-]+)")
# Today's ids, read the same way: Outlook's and the IMAP mailbox's ids carry a
# tag naming the provider, where the slot's name the account. Without it, an
# Outlook id is its EntryID and an IMAP id its UIDVALIDITY, UID and folder.
_PROVIDER_TAG = re.compile(r"\boutlook:(?=[0-9A-F]{16,}\b|\d+:\d+:)")


def _native(match: re.Match) -> str:
    """The service's own id, or the text as it was where it is not one: a
    mail account's source is `mail:<key>`, and a cursor of it may follow
    with a word (`...:sent`) that is no base64 of anything."""
    text = match.group(1)
    try:
        return urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode()
    except (UnicodeDecodeError, ValueError):
        return match.group(0)


def normalised(value):
    """The value with each id read back to the service's own, and SQLite's
    own clock, which no test pins, named rather than shown."""
    if isinstance(value, dict):
        return {k: ("<sqlite now>" if k in _SQLITE_NOW and v else normalised(v))
                for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalised(v) for v in value]
    if isinstance(value, str):
        return _PROVIDER_TAG.sub("", _SLOT_ID.sub(_native, value))
    return value


# Fields stamped by `datetime('now')` in the database.
_SQLITE_NOW = {"first_seen", "triaged_at", "checked_at"}


def _tools() -> dict:
    from agent.toolset import calendar, drive, mail, people

    found: dict = {}
    for module in (mail, calendar, drive, people):
        module.register(lambda fn: found.setdefault(fn.__name__, fn))
    return found


def _first(result, key: str):
    """The first `key` anywhere in a tool's result."""
    if isinstance(result, dict):
        if result.get(key) not in (None, ""):
            return result[key]
        result = list(result.values())
    if isinstance(result, list):
        for item in result:
            if (found := _first(item, key)) is not None:
                return found
    return None


def _tick(conn) -> dict:
    """One tick of each source, on the world as it was. A mail account's
    source (`mail:<key>`, since 4e) is labelled by its account's name, as
    each mailbox's own source was before."""
    from agent.attention import sources

    record: dict = {}
    for source in sources.all_sources(conn):
        label = source.account if source.feature == "mail" and source.account else source.name
        if label in SOURCES:
            found = sources.collect(conn, source)
            record[f"attention {label}"] = {
                "ran": sources.last_runs(conn).get(source.name, {}).get("note"),
                "observed": [dataclasses.asdict(o) for o in found],
            }
    return record


async def _use(world: World, conn, monkeypatch) -> dict:
    """The tools, each as the model calls it, with the ids they hand back."""
    from agent import actions, approvals
    from agent.turn import Deps

    acted: list[dict] = []

    async def act(ctx, tool, args, preview):
        asks = await actions.asks(ctx, tool, args)
        acted.append({"tool": tool, "args": args, "preview": preview, "asks": asks})
        if not asks:
            # Free, on the monitor's verdict, as `actions.run_free` runs it.
            with approvals.running_free(await actions._gate(ctx, tool, args)):
                return await actions.perform(tool, args)
        # Their yes, as `approvals.execute` runs it.
        with approvals.approved():
            return await actions.perform(tool, args)

    for name in ("mail", "calendar", "drive", "people"):
        monkeypatch.setattr(f"agent.toolset.{name}.act", act)

    record: dict = {}
    ctx = SimpleNamespace(deps=Deps(conn=conn, session_id=1))
    tools = _tools()
    steps: list = []

    async def call(name: str, **args):
        acted.clear()
        result = await tools[name](ctx, **args)
        steps.append({"tool": name, "args": args, "result": result,
                      "acted": [dict(a) for a in acted]})
        return result

    for box, query in (("gmail", "quarterly plan"), ("outlook", "badge renewal"),
                       ("home", "plot assignments")):
        found = await call("search_email", query=query, account=box)
        message = _first(found, "id")
        read = await call("read_email", message_id=message)
        sender = re.search(r"[\w.+-]+@[\w.-]+", str(read.get("from") or ""))
        drafted = await call("draft_email", to=sender.group(0) if sender else "",
                             subject=f"Re: {read.get('subject', '')}",
                             body="Thanks, I will look at it today.",
                             reply_to_message_id=message, account=box)
        await call("send_draft", draft_id=_first(drafted, "draft_id"))
        spare = await call("draft_email", to="dana@example.com", subject="Saturday",
                           body="Are you free on Saturday morning?", account=box)
        await call("delete_draft", draft_id=_first(spare, "draft_id"))
    await call("search_email", query="Friday", limit=10)

    await call("calendar_agenda", days=2)
    await call("calendar_free", date="today")
    # One event of their own, made, moved and taken off again: written on
    # condition that it is as it was read (the etag), and the delete on a card.
    tomorrow = (golden_world.MOMENT + timedelta(days=1)).date().isoformat()
    made = await call("create_event", summary="Haircut", start=f"{tomorrow}T07:30",
                      duration_minutes=30)
    await call("update_event", event_id=_first(made, "id"), start=f"{tomorrow}T07:45")
    await call("delete_event", event_id=_first(made, "id"), why="booked twice")

    files = await call("search_drive", query="quarterly plan")
    await call("read_drive_file", file_id=_first(files, "id"))

    await call("list_tasks")
    added = await call("add_task", task="Renew the parking permit", due_date="2026-10-20")
    await call("complete_task", commitment_id=_first(added, "commitment_id") or 0)
    await call("list_tasks")

    record["tools"] = steps
    record["wrote"] = world.wrote
    return record


def _seed(conn) -> None:
    from tools import commitments

    commitments.add(conn, text="Send Iris comments on the quarterly plan",
                    quote="I'll send you my comments by Thursday.", due_date="2026-10-15",
                    source="sent_mail", source_id="golden-commitment-1")


def _as_json(value) -> str:
    return json.dumps(value, indent=1, sort_keys=True, default=str, ensure_ascii=False) + "\n"


def test_moving_the_connectors_changes_nothing_they_return_ask_or_write(monkeypatch):
    """The six commits of step 5's item 4 move every connector; each must
    leave what the user sees exactly as it was, which no test of the moved
    functions can say."""
    from core import db

    for name in IMPORTS:
        importlib.import_module(name)
    before = {n for n in sys.modules if golden_world.ours(n)}

    world = World()
    golden_world.wire(monkeypatch, world)
    golden_world.pin_the_clock(monkeypatch)
    conn = db.open_store()
    _seed(conn)

    try:
        record = _tick(conn) | asyncio.run(_use(world, conn, monkeypatch))
    finally:
        world.close()
    record = json.loads(_as_json(normalised(record)))

    late = golden_world.unpinned(
        n for n in sys.modules if golden_world.ours(n) and n not in before)
    assert not late, f"imported during the run, so their clocks ran free: add {late} to IMPORTS"
    assert not world.unhandled, f"the golden world was asked for what it lacks: {world.unhandled}"
    # The world was reached, not a fence: each source ran, and each tool answered.
    ran = [v["ran"] for k, v in record.items() if k.startswith("attention")]
    assert all(str(note).startswith("ok") for note in ran), ran
    assert not [s for s in record["tools"] if _first(s["result"], "error")]

    if os.environ.get("STEVE_GOLDEN_RECORD") == "1":
        RECORD.parent.mkdir(parents=True, exist_ok=True)
        RECORD.write_text(_as_json(record), encoding="utf-8", newline="\n")
        pytest.skip(f"recorded {RECORD.name}")
    kept = json.loads(RECORD.read_text(encoding="utf-8"))
    changed = sorted(k for k in kept.keys() | record.keys() if kept.get(k) != record.get(k))
    diff = "".join(
        line for name in changed for line in difflib.unified_diff(
            _as_json(kept.get(name)).splitlines(keepends=True),
            _as_json(record.get(name)).splitlines(keepends=True),
            fromfile=f"recorded {name}", tofile=f"now {name}"))
    assert not changed, f"the connectors' outputs changed in {changed}:\n{diff[:20000]}"
