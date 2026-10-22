"""Nothing blocks the worker's event loop.

A synchronous call on the loop freezes every conversation, Signal and
`/health` together, and the supervisor's watchdog kills a worker whose
`/health` stops answering -- so a slow call became a killed worker and a
crash loop with nothing crashing. See agent/offloop.py for the ones found.

The rule is checked mechanically below, over every `async def` in the code
that runs in the worker, against a list of calls known to block. Add to the
list whenever a new one is found; never add an exception for a call site
instead.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from pathlib import Path

import pytest

from agent import offloop
from core import db
from tests.gate import repo_files

REPO = Path(__file__).resolve().parents[2]

# Calls that block, by how they are written at the call site.
BLOCKING = {
    "subprocess.run", "subprocess.check_output", "subprocess.call", "time.sleep",
    "requests.get", "requests.post", "urllib.request.urlopen",
    "code_exec.run_python", "code_exec.status", "code_exec.ensure_analysis_image",
    "memory.recall", "memory.remember", "memory.embed", "memory.reembed", "embed.vectors",
    "_keep_lesson",  # reflection's memory.remember, one call removed
    # Facts embed what they keep and what they are asked (agent/facts.py).
    "facts.add", "facts.correct", "facts.relevant", "facts.relevant_text", "facts.search",
    "facts.forget_number", "facts.reembed",
    "keep_fact", "correct_fact", "forget_fact", "recalled",
    "models_scout.scan",  # OpenAI's model list, over the network
    # The agent's own mailbox, from its account (agent/mailbox.py).
    "mailbox.poll", "mailbox.read", "_archive_now",
    "code.sign_in",  # the browser sign-in, which waits for the user
    # The accounts' own services, reached in the caller's thread (agent/connectors.py).
    "connectors.blocking", "connectors.addresses", "connectors.conditional",
    "_accounts_known", "read_any",
    # The keychain, the files in the data folder and the database (core/monitor.py,
    # core/totp.py, core/passphrase.py).
    "monitor.accept_code", "totp.paused_until", "totp.paused", "passphrase.is_set",
}

SCANNED = ("agent/", "tools/", "channels/", "voice/", "modules/")


def _blocking_calls_in_async_defs() -> list[str]:
    found = []
    for rel in repo_files.python(*SCANNED):
        tree = repo_files.parsed(rel)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            # A nested plain function or lambda is what gets handed to a
            # thread, so calls inside one are not on the loop.
            nested = {
                id(inner)
                for node in ast.walk(fn)
                if node is not fn and isinstance(node, (ast.FunctionDef, ast.Lambda))
                for inner in ast.walk(node)
            }
            for node in ast.walk(fn):
                if id(node) in nested or not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                if name in BLOCKING:
                    found.append(f"{rel}:{node.lineno} {fn.name}() calls {name}")
    return found


def test_no_async_function_calls_something_known_to_block():
    offenders = _blocking_calls_in_async_defs()

    assert not offenders, (
        "blocking calls on the event loop -- use asyncio.to_thread, or "
        "offloop.with_own_conn if it needs the database:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_would_see_one():
    """The guard on the guard: a scan that finds nothing because it looks in
    the wrong place passes forever."""
    tree = ast.parse("async def f():\n    code_exec.run_python('1')\n")
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]

    assert calls == ["code_exec.run_python"]
    assert calls[0] in BLOCKING
    assert (REPO / "agent" / "loop.py").exists()


# --------------------------------------------------------------------------
# with_own_conn
# --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    conn = db.open_store(tmp_path / "offloop.db")
    yield conn
    conn.close()


async def test_the_work_runs_in_another_thread_on_its_own_connection(store):
    loop_thread = threading.get_ident()
    seen = {}

    def work(conn, value):
        seen["thread"] = threading.get_ident()
        seen["conn"] = conn
        seen["file"] = offloop.db_path(conn)
        return value * 2

    assert await offloop.with_own_conn(store, work, 21) == 42
    assert seen["thread"] != loop_thread
    assert seen["conn"] is not store


async def test_it_opens_the_same_database_never_the_live_one(store, tmp_path):
    """The other half of the trap: a thread that opened `db.open_store()`
    with no path would reach the real data/agent.db from inside a test."""
    def where(conn):
        return offloop.db_path(conn)

    opened = await offloop.with_own_conn(store, where)
    assert opened.resolve() == (tmp_path / "offloop.db").resolve()


async def test_what_the_thread_writes_the_loop_can_read(store):
    def write(conn):
        with conn:
            conn.execute("INSERT INTO sessions (channel) VALUES ('offloop')")

    await offloop.with_own_conn(store, write)

    written = store.execute("SELECT COUNT(*) FROM sessions WHERE channel = 'offloop'")
    assert written.fetchone()[0] == 1


async def test_an_in_memory_database_runs_in_place():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    try:
        assert await offloop.with_own_conn(conn, lambda c: c is conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------


async def test_a_slow_probe_does_not_stop_the_loop():
    """The probe is slow until the loop has ticked ten times, which it can
    only do if the probe is somewhere else. It used to sleep 0.3 s and count
    the ticks that fitted in -- about nineteen on Windows' 15.6 ms timer, so
    a loaded machine that gave the loop half its wake-ups failed it with the
    code right. Run on the loop itself, the probe would wait out its 10 s
    with the ticker unable to run, and fail."""
    ticked = threading.Event()
    probe = offloop.Probe(lambda: "up" if ticked.wait(10) else "the loop stood still", ttl_s=60)
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            ticks += 1
            if ticks >= 10:
                ticked.set()
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    try:
        assert await probe.get() == "up"
    finally:
        ticker.cancel()


async def test_callers_at_once_share_one_check():
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        time.sleep(0.1)
        return calls

    probe = offloop.Probe(check, ttl_s=60)
    results = await asyncio.gather(*(probe.get() for _ in range(5)))

    assert calls == 1
    assert results == [1] * 5


async def test_a_remembered_value_is_reused_until_it_goes_stale(monkeypatch):
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        return calls

    probe = offloop.Probe(check, ttl_s=60)
    await probe.get()
    await probe.get()
    assert calls == 1

    probe._at -= 61
    assert await probe.get() == 2
