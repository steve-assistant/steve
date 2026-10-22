"""Every guarantee meets every door, in a named test or a written reason, and
every route of the backend is placed (tests/e2e/doors.py).

The table is worth its upkeep only if it cannot fall behind. The review of
steps 7 to 9 found guarantees tested from the main window and broken from
the doors beside it (the tray's menu, the quick bar, a toast), because
nothing asked about those doors. So here a door added to the table, a
test renamed away from it, a reason that names no code, and a route added
to the worker without anyone asking which guarantees it touches each fail.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from starlette.routing import WebSocketRoute

from tests.e2e import doors
from tests.gate.routes import routes

ROOT = Path(__file__).resolve().parents[2]
# A file a reason names: `agent/worker/desk.py`, `ui/src-tauri/src/toast.rs:63`.
CITED = re.compile(r"\b((?:agent|channels|core|docs|modules|steve_sdk|ui|tests)/[\w/.-]+\.\w+)")
# A pair that names no test: one that cannot happen, or one still to decide.
REASONS = (doors.Cannot, doors.Open)


def _key(name: str, route) -> str:
    """A route as `ROUTES` writes it: "worker POST /actions/decide"."""
    if isinstance(route, WebSocketRoute):
        return f"{name} WS {route.path}"
    # HEAD is Starlette's echo of GET, not a route of its own.
    return f"{name} {','.join(sorted(route.methods - {'HEAD'}))} {route.path}"


def _cells():
    for guarantee, row in doors.TABLE.items():
        for door, cell in row.items():
            yield guarantee, door, cell


def _named():
    """(guarantee, door, "path::test") for each test the table names."""
    for guarantee, door, cell in _cells():
        if not isinstance(cell, REASONS):
            for test in (cell,) if isinstance(cell, str) else cell:
                yield guarantee, door, test


def _defines(path: Path, name: str) -> bool:
    """Whether the file defines a test of that name: a Python function or
    method, or a Rust `#[test]` function."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".rs":
        return re.search(rf"#\[test\]\s*(#\[[^\]]*\]\s*)*fn {re.escape(name)}\(", text) is not None
    return any(isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
               for node in ast.walk(ast.parse(text)))


def test_every_guarantee_meets_every_door_in_the_tables_order():
    """A pair left out is a door nobody asked about."""
    assert list(doors.TABLE) == list(doors.GUARANTEES)
    for guarantee, row in doors.TABLE.items():
        assert list(row) == list(doors.DOORS), guarantee


def test_each_pair_names_its_tests_or_a_reason_that_cites_code_that_exists():
    """A reason is a claim about the code ("Telegram opens no microphone"),
    so it names the file that makes it true, and that file must be there.
    An open pair names the code that leaves it open, the same way."""
    for guarantee, door, cell in _cells():
        where = f"{guarantee} x {door}"
        if isinstance(cell, REASONS):
            cited = CITED.findall(cell.why)
            assert cited, f"{where}: the reason names no file"
            for path in cited:
                assert (ROOT / path.rstrip(".")).is_file(), f"{where}: {path} is not a file"
        else:
            tests = (cell,) if isinstance(cell, str) else cell
            assert tests and all(isinstance(t, str) for t in tests), where


@pytest.mark.parametrize(("guarantee", "door", "test"), list(_named()),
                         ids=[f"{g}-{d}-{t.rpartition('::')[2]}" for g, d, t in _named()])
def test_every_test_the_table_names_exists(guarantee, door, test):
    """A test renamed or removed would leave its pair claiming a check that
    no longer runs."""
    path, _, name = test.partition("::")
    assert name and "::" not in name, f"{test}: name it as path::function"
    assert path.startswith(("tests/", "ui/src-tauri/src/")), test
    assert (ROOT / path).is_file(), f"{guarantee} x {door}: {path} is not a file"
    assert name.startswith("test") or path.endswith(".rs"), test
    assert _defines(ROOT / path, name), f"{guarantee} x {door}: {path} has no {name}"


def test_every_route_of_the_supervisor_and_the_worker_is_placed():
    """A route added to the worker is a new door. It fails here until
    someone has placed it by the guarantees it touches, and a route
    removed leaves no stale line behind."""
    found = {_key(name, route) for name, route in routes()}
    assert sorted(found - set(doors.ROUTES)) == [], "place these in tests/e2e/doors.py ROUTES"
    assert sorted(set(doors.ROUTES) - found) == [], "these are no longer routes"
    for key, touched in doors.ROUTES.items():
        assert set(touched) <= set(doors.GUARANTEES), key
        assert len(set(touched)) == len(touched), key
