"""What Steve knows can be read, corrected and forgotten from the app.

A fact Steve kept about the user was readable only by asking it, and
forgettable only by the number a morning digest gave it, so a wrong one
stayed until it surfaced in a reply (agent/worker/knows.py). These tests
hold the routes the app's "What Steve knows" page uses.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest
from starlette.applications import Starlette

from agent import facts, runtime
from agent.worker import knows
from core import db
from tools import memory


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = db.open_store(tmp_path / "knows.db")
    files = tmp_path / "memories"
    monkeypatch.setenv("STEVE_MEMORIES_DIR", str(files))
    monkeypatch.setattr(memory, "embed", lambda texts: [[0.1] * db.EMBEDDING_DIM for _ in texts])
    monkeypatch.setattr(runtime, "conn", c)
    # Kept as Steve keeps them, so the memories' git holds them.
    memory.remember(c, kind="user", summary="Theo Brandt is Maya's climbing partner",
                    body="x", name="theo")
    memory.remember(c, kind="project", summary="Maya is choosing a kiln for the studio",
                    body="x", name="kiln")
    yield c
    c.close()


def _call(method: str, path: str, **kw) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=Starlette(routes=knows.ROUTES))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.request(method, path, **kw)
    return asyncio.run(go())


def test_the_page_lists_the_users_facts_first_then_others_by_name_and_every_memory(conn):
    """The user reads about themself first; a fact about someone else says
    whom by name, not by a ledger key, and their writing style (which the
    style guide shows) is not listed as a fact."""
    facts.add(conn, "person:theo", "role", "climbing partner", source="user_words",
              name="Theo Brandt")
    facts.add(conn, facts.USER, "home_city", "Duluth", source="confirmed")
    facts.add(conn, facts.USER, "writing_sign_off", "Best, M", source="user_words")

    said = _call("GET", "/control/knows").json()

    assert [(f["about"], f["attribute"], f["value"]) for f in said["facts"]] == [
        ("You", "home city", "Duluth"), ("Theo Brandt", "role", "climbing partner")]
    assert sorted(m["path"] for m in said["memories"]) == ["project/kiln.md", "user/theo.md"]


def test_a_correction_replaces_the_fact_with_the_users_own_word(conn):
    """Corrected here, the value is one the user confirmed, so a later
    guess from a mail can't overwrite it; the old row stays, superseded."""
    old = facts.add(conn, "person:theo", "employer", "Contso", source="mail", name="Theo Brandt")

    said = _call("POST", "/control/knows/correct", json={"id": old.id, "value": "Contoso"}).json()

    assert said["ok"] and said["value"] == "Contoso"
    now = facts.get(conn, said["id"])
    assert (now.value, now.source, facts.get(conn, old.id).status) == (
        "Contoso", "confirmed", "superseded")
    with pytest.raises(ValueError):
        facts.correct(conn, now.id, "Northwind", source="mail")


def test_forgetting_a_fact_stops_holding_it_and_keeps_its_row(conn):
    old = facts.add(conn, facts.USER, "occupation", "potter", source="user_words")

    assert _call("POST", "/control/knows/forget", json={"id": old.id}).json() == {"ok": True}

    assert facts.get(conn, old.id).status == "forgotten"
    assert _call("GET", "/control/knows").json()["facts"] == []
    again = _call("POST", "/control/knows/forget", json={"id": old.id})
    assert again.status_code == 400


def test_forgetting_a_memory_deletes_its_file_and_its_index_row(conn, tmp_path):
    said = _call("POST", "/control/knows/forget", json={"path": "user/theo.md"})

    assert said.json() == {"ok": True}
    assert not (tmp_path / "memories" / "user" / "theo.md").exists()
    assert [m["path"] for m in _call("GET", "/control/knows").json()["memories"]] == [
        "project/kiln.md"]


def test_a_path_the_index_does_not_list_is_never_touched(conn, tmp_path):
    """The path comes from a request: one outside the memories, or one
    never indexed, is refused before anything is joined onto the folder."""
    outside = tmp_path / "keep.md"
    outside.write_text("mine", encoding="utf-8", newline="\n")

    said = _call("POST", "/control/knows/forget", json={"path": "../keep.md"})

    assert said.status_code == 400
    assert outside.exists()


def test_a_request_that_names_both_or_neither_is_refused(conn):
    old = facts.add(conn, facts.USER, "pronouns", "she/her", source="confirmed")
    for body in ({}, {"id": old.id, "path": "user/theo.md"}, {"id": str(old.id)},
                 {"path": ""}):
        assert _call("POST", "/control/knows/forget", json=body).status_code == 400
    assert _call("POST", "/control/knows/correct", json={"id": old.id, "value": "  "}
                 ).status_code == 400
    assert facts.get(conn, old.id).status == "active"
    assert facts.held(conn, today=date(2026, 10, 18))
