"""Every judge writes its reason before its decision.

The fields of a structured answer are written in the order the schema
gives them, and at effort none there is no thinking but what is written:
the triage judge decided whether to ping their phone (`say`) before a word
about why, and the reflection judge scored before its notes (5 October
2026). A reason written after the decision justifies it; one written before
can change it.
"""

from __future__ import annotations

import pytest

from agent import reflect, skills
from agent.attention import triage


@pytest.mark.parametrize(("model", "reason", "decisions"), [
    (triage.Verdict, "why", ["say", "critical", "headline", "task"]),
    (reflect.Verdict, "notes", list(reflect.KEYS)),
    (skills.Review, "reason", ["keep"]),
])
def test_the_reason_comes_before_the_decision(model, reason, decisions):
    order = list(model.model_json_schema()["properties"])

    assert all(order.index(reason) < order.index(field) for field in decisions), order
