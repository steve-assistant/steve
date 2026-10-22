"""Only the user says who the user is.

Who the user is -- their name, the name they go by, their pronouns, home
town and occupation (agent/facts.py `IDENTITY`) -- is the most trusted
thing a turn reads. It is in front of every turn, it decides who "the user"
is when the model names a subject, and the home town moves their weather,
time zone and alerts. In the audit of 12 October 2026 any source could
write it: a background job's home town replaced the one the user typed, and
a name read from mail became the user's. Frozen here, so an upgrade cannot
loosen it:

- it is written only from the user's own sources (`PROFILE_SOURCES`): the
  profile page and their own words. Nothing else adds, replaces or corrects
  it;
- what another source wrote is never read as who they are;
- a turn on their own channel keeps it only when they wrote it, not when an
  email or a page the turn read said it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent import facts, home
from core import db
from tools import memory

OTHERS = ["inferred:job", "inferred:paste", "mail", "web", "judge"]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "embed", lambda texts: [[0.1] * db.EMBEDDING_DIM for _ in texts])
    conn = db.open_store(tmp_path / "who.db")
    yield conn
    conn.close()


@pytest.mark.parametrize("source", OTHERS)
def test_no_other_source_adds_replaces_or_corrects_who_the_user_is(store, source):
    """A job, a pasted page, mail, the web and the reviewer each read text
    someone else wrote. None of them may move the user's town or rename
    them."""
    typed = facts.add(store, facts.USER, "home_city", "Pittsburgh, PA", source="confirmed")

    for attribute in facts.IDENTITY:
        with pytest.raises(ValueError):
            facts.add(store, facts.USER, attribute, "Pyongyang", source=source)
    with pytest.raises(ValueError):
        facts.correct(store, typed.id, "Pyongyang", source=source)

    assert facts.get(store, typed.id).status == "active"
    assert facts.identity(store) == {**dict.fromkeys(facts.IDENTITY, ""),
                                     "home_city": "Pittsburgh, PA"}


def test_what_another_source_wrote_is_never_read_as_who_the_user_is(store):
    """A row written before the rule, or past it, is not their name, their
    name as a subject, or their town."""
    with store:
        store.executemany(
            "INSERT INTO facts (subject, attribute, value, source) VALUES ('user', ?, ?, ?)",
            [("name", "Mallory Quinn", "mail"), ("preferred_name", "Mal", "web"),
             ("home_city", "Pyongyang", "inferred:job")])

    assert facts.identity(store) == dict.fromkeys(facts.IDENTITY, "")
    assert home.home_city(store) is None
    for name in ("Mallory", "Mal"):
        with pytest.raises(ValueError):  # someone else, named as person:<name>
            facts.subject_of(store, name)


def _remember(monkeypatch):
    from agent.toolset import memory as toolset

    async def with_own_conn(conn, fn, *args, **kwargs):
        return fn(conn, *args, **kwargs)

    monkeypatch.setattr(toolset.offloop, "with_own_conn", with_own_conn)
    found: dict = {}
    toolset.register(lambda fn: found.setdefault(fn.__name__, fn))
    return found["remember"]


def _turn(store, said: str, asked: str):
    """A turn on Signal, their own channel, after they wrote `said`."""
    with store:
        session = store.execute("INSERT INTO sessions (channel) VALUES ('signal')").lastrowid
        store.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)",
                      (session, said))
    return SimpleNamespace(deps=SimpleNamespace(
        conn=store, session_id=session, channel="signal", asked=asked, tools_used=[],
        turn_cache={}))


def test_a_turn_keeps_who_the_user_is_only_from_what_they_wrote(store, monkeypatch):
    """Their channel, so a fact kept reads as their words -- but the turn
    may have read an email that put the town there."""
    remember = _remember(monkeypatch)
    asked = "what does the latest email from the travel desk say?"

    refused = asyncio.run(remember(_turn(store, asked, asked), kind="user",
                                   summary="Pyongyang", attribute="home_city"))
    kept = asyncio.run(remember(_turn(store, "I moved to Leeds last week", "remember that"),
                                kind="user", summary="Leeds", attribute="home_city"))

    assert refused["kept"] is None
    assert facts.identity(store)["home_city"] == "Leeds" == facts.get(store, kept["kept"]).value
