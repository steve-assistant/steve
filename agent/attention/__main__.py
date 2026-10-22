"""Run one attention tick by hand, against a copy of the database.

    python -m agent.attention                    # every source, dry run
    python -m agent.attention --only gmail,horizon
    python -m agent.attention vet --dry-run --db copy.db   # the checks, replayed

Reads the real sources and asks the real judge -- that is the point of it --
but writes only to a temporary copy of `data/agent.db`, does no tasks and
sends nothing. What it would send is printed instead. A dry run that wrote
to the live database would mark everything it saw as seen, and the live
loop would then never consider any of it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from core import db


def _copy_of_live(into: Path) -> sqlite3.Connection:
    from core import db

    live = db.open_store()
    try:
        copy = sqlite3.connect(into)
        live.backup(copy)
        copy.close()
    finally:
        live.close()
    return db.open_store(into)


async def _run(conn: sqlite3.Connection, only: list[str] | None) -> dict:
    from agent import attention
    from agent.attention import store

    printed: list[str] = []

    async def show(text: str) -> bool:
        printed.append(text)
        return True

    from agent.attention import deliver, triage

    result = await attention.tick(conn, notify=show, do_tasks=False, only=only, force=True)
    # A backlog is more than one batch; judge all of it, a few batches at most.
    for _ in range(4):
        if not store.untriaged(conn, 1):
            break
        await triage.triage(conn)
    # Tasks are not done in a dry run, and an item waits for its task before
    # it goes out -- so mark them as done, on the copy, to see the message.
    with db.transaction(conn):
        conn.execute("UPDATE observations SET prepared_at = datetime('now') WHERE task IS NOT NULL")
    await deliver.deliver(conn, notify=show)
    judged = conn.execute(
        "SELECT source, key, say, critical, headline, task FROM observations"
        " WHERE triaged_at >= datetime('now', '-10 minutes') AND say <> 'never'"
        " ORDER BY id"
    ).fetchall()
    return {
        "tick": result.as_dict(),
        "worth_something": [dict(r) for r in judged],
        "would_send": printed,
    }


def main() -> None:
    # Headlines and calendar entries carry emoji, and a Windows console's
    # default code page cannot print them: the report died on a birthday.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.argv[1:2] == ["vet"]:
        # What the checks in code would have changed over recorded days
        # (`replay.py`); reads a copy, writes nothing.
        from agent.attention import replay

        raise SystemExit(replay.main(sys.argv[2:]))
    parser = argparse.ArgumentParser(description="One attention tick, dry run.")
    parser.add_argument("--only", help="comma-separated source names")
    args = parser.parse_args()
    only = [s.strip() for s in args.only.split(",")] if args.only else None

    with tempfile.TemporaryDirectory() as folder:
        conn = _copy_of_live(Path(folder) / "dry-run.db")
        try:
            report = asyncio.run(_run(conn, only))
        finally:
            conn.close()
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
