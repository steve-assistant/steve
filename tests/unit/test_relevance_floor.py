"""The relevance floors drop every labelled miss, and keep most hits.

Recall's floor was 0.72, set for a local model whose unrelated texts scored
above 0.5. On OpenAI's vectors, whose related texts score lower, it kept 2
of 20 labelled hits: recall would have come back nearly empty, and no test
would have said so, since every test fakes the embedder. So the floors are
held to scores measured on the model the code uses
(tests/unit/fixtures/floors/pairs.json, by `python -m devtools.floors`).

No floor on any model tried both keeps every hit and drops every miss. They
are set, as the personal edition chose, to drop every miss -- a wrong note
or fact in front of a turn is believed -- and keep what that allows.
"""

from __future__ import annotations

import json

import pytest

from core import db
from devtools import floors

FIXTURE = json.loads(floors.PAIRS.read_text(encoding="utf-8"))
KINDS = ("fact", "memory")
# Hits of each kind the floors must keep, of ten: what dropping every miss
# allowed on 12 October 2026.
KEEPS_AT_LEAST = 6


def _scores(kind: str, hit: bool) -> list[float]:
    return [p["score"] for p in FIXTURE["pairs"] if p["kind"] == kind and p["hit"] is hit]


def test_the_scores_are_from_the_model_and_size_the_code_embeds_with():
    """Another model's scores say nothing of this one's: change the model
    and this fails until the floors are measured again."""
    assert (FIXTURE["model"], FIXTURE["dim"]) == (db.EMBEDDING_MODEL, db.EMBEDDING_DIM)


@pytest.mark.parametrize("kind", KINDS)
def test_each_kind_is_measured_on_ten_hits_and_ten_misses(kind):
    """Pairs taken out to make a floor pass would leave it measured on
    nothing."""
    assert len(_scores(kind, True)) == 10 and len(_scores(kind, False)) == 10
    for pair in FIXTURE["pairs"]:
        assert floors.stored_text(FIXTURE, pair), pair


@pytest.mark.parametrize("kind", KINDS)
def test_every_labelled_miss_is_below_its_kinds_floor(kind):
    floor = floors.floor_for(kind)

    assert [s for s in _scores(kind, False) if s >= floor] == []


@pytest.mark.parametrize("kind", KINDS)
def test_the_floor_keeps_most_labelled_hits(kind):
    floor = floors.floor_for(kind)

    assert sum(s >= floor for s in _scores(kind, True)) >= KEEPS_AT_LEAST
