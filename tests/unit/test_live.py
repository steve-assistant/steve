"""The desk's GPT-Live pieces, after the phone that first carried them went.

The live conversation at the desk (`voice/desk_call.py`) shared its
connection, its record and its text helpers with the personal edition's
phone calls, in modules the product does not have, so they live in
`voice/live.py`. These are the guarantees the desk relied on from them,
kept now that nothing else holds them.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from voice import live


def test_fragments_are_joined_as_words():
    """GPT-Live's transcript arrives in fragments, some with their own spaces
    and some without; a request handed over must read as words."""
    assert live.joined(["what's on", " tomorrow?"]) == "what's on tomorrow?"
    assert live.joined(["what's", "on", "tomorrow"]) == "what's on tomorrow"


def test_markdown_is_not_read_out_as_symbols():
    """The assistant writes for a screen; handed to a voice, asterisks and
    headings were read out as characters."""
    assert live.speakable("**Two** things:\n# Rent\n`due` today") == "Two things: Rent due today"


def test_a_conversation_keeps_both_sides_in_order_as_said():
    """The transcript kept on the desk's thread is what "it misses half of
    what I say" is checked against: each voice's fragments joined into its
    line, the lines in the order they were spoken."""
    session = live.Session()
    session.note("them", "what's on")
    session.note("them", "tomorrow")
    session.note("us", "Two things.")

    assert session.transcript == "you: what's on tomorrow\nme: Two things."


def test_what_a_conversation_cost_is_its_voice_and_its_answers():
    """The spend page counts a live conversation by what it reports here."""
    session = live.Session(voice_cost_usd=0.05)
    session.turns.append(live.Turn(said="Two things.", heard="what's on", cost_usd=0.01))

    assert round(session.cost_usd, 4) == 0.06


def test_the_promise_it_does_not_keep_is_recognised():
    """GPT-Live says "let me check" and sometimes never hands off; the desk's
    watchdog hands off when it hears the promise. A sentence that only
    mentions checking is not one."""
    assert live.PROMISE.search("Sure, let me check your calendar.")
    assert live.PROMISE.search("One moment.")
    assert not live.PROMISE.search("I checked it yesterday.")


def test_the_time_is_told_in_the_users_own_zone(monkeypatch):
    """The voice is told the day and time before a word is said, in the
    user's zone, not UTC."""
    monkeypatch.setattr(live, "local_zone", lambda: ZoneInfo("America/Chicago"))

    assert live.when(dt.datetime(2026, 9, 25, 15, tzinfo=dt.UTC)) == "Friday 25 September, 10:00 AM"


def test_the_live_pieces_load_without_the_attention_loop():
    """A call once imported the whole attention package on the way to its
    first words, about two seconds cold. The desk opens a
    live conversation on request, and should not wait on it either."""
    tree = ast.parse(Path(live.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.Import) for alias in node.names
    }

    assert not {m for m in imported if m.startswith("agent.attention")}, imported
