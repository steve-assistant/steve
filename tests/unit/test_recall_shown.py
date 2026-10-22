"""Recall shows the close matches, not always five.

Every call returned five memories, most of them about something else: the
judge's lessons and the night's research notes answering "Maya class
schedule". The embedder's scores cannot be cut at a fixed line -- on their own
memories (6 October 2026) a wrong match scored 0.592 and a right one 0.547 --
but a clear hit stands well clear of the rest.
"""

from __future__ import annotations

from agent.toolset.memory import RECALL_BAND, RECALL_BODY_CHARS, shown
from tools.memory import Memory


def _m(summary: str, score: float, body: str = "") -> Memory:
    return Memory(path=f"user/{summary[:10]}.md", kind="user", summary=summary, body=body,
                  created_at="2026-10-01", score=score)


def test_a_clear_hit_comes_back_alone():
    """Their real scores for "Maya's advisor"."""
    said = shown([_m("Maya's advisor is Mira Shah.", 0.683),
                  _m("For thank-you notes to clients, Maya prefers...", 0.579),
                  _m("Maya is a product designer at Contoso", 0.567)])

    assert [m["summary"] for m in said] == ["Maya's advisor is Mira Shah."]


def test_near_equal_matches_all_come_back():
    said = shown([_m("a", 0.547), _m("b", 0.539), _m("c", 0.536), _m("d", 0.50)])
    assert [m["summary"] for m in said] == ["a", "b", "c"]
    assert 0.547 - 0.536 <= RECALL_BAND < 0.547 - 0.50


def test_the_same_memory_filed_twice_is_shown_once():
    said = shown([_m("Maya's advisor is Mira Shah.", 0.6),
                  _m("Maya's  advisor is Mira Shah.", 0.6)])
    assert len(said) == 1


def test_a_long_note_is_cut_and_says_so():
    [said] = shown([_m("Durable framework for assessing urban flood risk", 0.6,
                       body="Rain. " * 1000)])
    assert len(said["body"]) < RECALL_BODY_CHARS + 200
    assert said["body"] != "Rain. " * 1000


def test_nothing_found_is_an_empty_answer():
    assert shown([]) == []
