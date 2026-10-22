"""Redaction must exist wherever reflection can run.

This lives in the gate rather than in unit tests because it is a deployment
invariant, not a behaviour: reflection ships whole conversations to a judge
model, and the only thing standing between that and a leak is presidio being
installed. redact.py already fails closed if it is missing -- this makes the
failure happen at the gate instead of at three in the morning.

Scoped to "wherever reflection is wired up": an install that does not
reflect has no business carrying a 600MB language model for code it cannot
reach. presidio is the `reflect` extra, which Steve's base install leaves out.
Where it is missing, the gate checks that redaction refuses instead.
"""

from __future__ import annotations

import importlib.util

import pytest


def _reflection_is_wired_up() -> bool:
    """Is this a deployment that runs the reflection pass at all?"""
    return importlib.util.find_spec("agent.reflect") is not None


def test_without_presidio_redaction_refuses():
    """A redactor that is not installed must raise, not pass the text on:
    reflection then stops before the judge sees anything."""
    if importlib.util.find_spec("presidio_analyzer"):
        pytest.skip("presidio is installed; the tests below check it works")

    from agent.redact import redact

    with pytest.raises(ImportError):
        redact("Ask Theo to forward the lease to maya.lindqvist@example.com.")


def test_the_ner_model_is_installed_too():
    """presidio without a spaCy model detects patterns and no names at all,
    which looks like it is working."""
    if not _reflection_is_wired_up():
        pytest.skip("no reflection here")
    if not importlib.util.find_spec("presidio_analyzer"):
        pytest.skip("covered by the test above")

    assert importlib.util.find_spec("en_core_web_lg"), (
        "presidio is installed but its NER model is not, so PERSON and "
        "LOCATION would silently never be detected."
    )


def test_redaction_actually_removes_a_name():
    """The end-to-end check. Both imports can succeed while the pipeline is
    misconfigured, and a redactor that returns its input unchanged is worse
    than no redactor because it looks fine."""
    if not _reflection_is_wired_up() or not importlib.util.find_spec("presidio_analyzer"):
        pytest.skip("redaction not installed here")

    from agent.redact import redact

    out = redact("Ask Theo to forward the lease to maya.lindqvist@example.com.")
    assert "Theo" not in out.text
    assert "maya.lindqvist@example.com" not in out.text
