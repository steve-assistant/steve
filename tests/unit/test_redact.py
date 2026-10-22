"""Redaction, which runs before a transcript reaches a judge model.

The bar here is not "mostly works". Reflection ships conversations about
the user's mail, calendar and whereabouts to a third party, and the value of a
rubric score is small next to the cost of leaking any of it -- so these tests
plant real-shaped detail and assert none of it survives.
"""

from __future__ import annotations

import pytest

import tests.gate.reflect_extra  # noqa: F401 - skips this module without the reflect extra
from agent.redact import KEEP, placeholders, redact

# Fake, but shaped exactly like the real thing -- which matters more than it
# sounds. The obvious placeholder SSN 123-45-6789 is *explicitly allow-listed*
# by presidio as a published example, so testing with it proves nothing and
# passes for the wrong reason. These are invented but structurally valid.
PLANTED = {
    "EMAIL_ADDRESS": "maya@contoso.example",
    "PHONE_NUMBER": "412-555-0148",
    "CREDIT_CARD": "4111-1111-1111-1111",
    "US_SSN": "521-84-3372",
    "STREET_ADDRESS": "12 Maple Street",
    "SECRET": "sk-or-v1-abcdef0123456789abcdef",
}


@pytest.mark.parametrize(("kind", "value"), PLANTED.items())
def test_planted_detail_does_not_survive(kind, value):
    out = redact(f"Here is the detail: {value} -- please use it.")
    assert value not in out.text, f"{kind} leaked verbatim"
    assert out.count > 0


def test_a_whole_transcript_is_cleaned_at_once():
    """The real shape: several kinds of detail in one message."""
    text = (
        "Email maya@contoso.example or call 412-555-0148. "
        "Maya Lindqvist lives at 12 Maple Street, Pittsburgh. "
        "Card 4111-1111-1111-1111."
    )
    out = redact(text)
    for value in ("maya@contoso.example", "412-555-0148", "12 Maple Street",
                  "4111-1111-1111-1111", "Maya Lindqvist"):
        assert value not in out.text
    assert not out.clean


# --------------------------------------------------------------------------
# Structure has to survive
# --------------------------------------------------------------------------


def test_the_same_person_keeps_the_same_placeholder():
    """Collapsing every name to <PERSON> turns "ask Alice to forward Bob's
    note" into a sentence the judge cannot reason about."""
    out = redact("Ask Alice to forward Bob's note; Alice already has it.")
    found = placeholders(out.text)
    assert found[0] == found[2], "Alice changed identity mid-sentence"
    assert found[0] != found[1], "Alice and Bob were merged"


def test_different_values_get_different_placeholders():
    out = redact("Mail alice@example.com and bob@example.com.")
    assert len(set(placeholders(out.text))) == 2


def test_placeholders_are_numbered_per_kind():
    out = redact("Alice emailed alice@example.com.")
    assert "<PERSON_1>" in out.text
    assert "<EMAIL_ADDRESS_1>" in out.text


def test_dates_and_times_are_kept():
    """Reflection is largely about timing -- "did it check the calendar before
    answering?" is unanswerable once every date is a placeholder."""
    assert "DATE_TIME" in KEEP
    out = redact("The meeting is Tuesday at 3pm.")
    assert "Tuesday" in out.text and "3pm" in out.text


def test_ordinary_text_is_left_alone():
    text = "Summarise the quarterly numbers and make a chart."
    out = redact(text)
    assert out.text == text
    assert out.clean


# --------------------------------------------------------------------------
# Overlaps
# --------------------------------------------------------------------------


def test_an_email_is_not_shredded_into_overlapping_matches():
    """Presidio finds a URL inside an email address; taking both would emit
    nested placeholders and leave fragments of the original behind."""
    out = redact("Write to maya@contoso.example today.")
    assert out.text.count("<") == out.text.count(">") == 1
    assert "contoso.example" not in out.text
    assert "maya" not in out.text


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_nothing_in_nothing_out(empty):
    out = redact(empty)
    assert out.clean
    assert out.text == (empty or "")


def test_counts_report_what_was_removed():
    out = redact("Call 412-555-0148 or 412-555-0149.")
    assert out.found["PHONE_NUMBER"] == 2
    assert out.count == 2


def test_presidio_ignores_its_own_published_sample_ssns():
    """Not a bug to fix -- a trap to remember. 123-45-6789 is allow-listed as
    a documentation example, so a redaction test written with it passes while
    testing nothing. Pinned so the next person reaches for a real-shaped
    number instead of "fixing" the redactor."""
    assert "123-45-6789" in redact("SSN 123-45-6789").text
    assert "521-84-3372" not in redact("SSN 521-84-3372").text


def test_redaction_fails_closed_if_presidio_cannot_load(monkeypatch):
    """The reason presidio is a required dependency rather than an extra.

    If the engine is unavailable the right outcome is a reflection pass that
    stops, not one that quietly sends the transcript through unredacted. An
    exception here becomes a failed job; a silent pass-through becomes a leak
    nobody finds out about."""
    import agent.redact as mod

    def unavailable():
        raise ImportError("no presidio")

    monkeypatch.setattr(mod, "_analyzer", None)
    monkeypatch.setattr(mod, "_get_analyzer", unavailable)

    with pytest.raises(ImportError):
        mod.redact("My SSN is 521-84-3372.")


# --------------------------------------------------------------------------
# Noisy recognisers
# --------------------------------------------------------------------------


def test_quarter_labels_are_not_driver_licences():
    """Found live: a conversation about earnings came back with "Q3" and "Q4"
    replaced by placeholders. The US_DRIVER_LICENSE weak patterns match any
    short alphanumeric token, which costs the judge the meaning of the
    sentence and protects nothing."""
    out = redact("Q3 earnings beat and Q4 guidance was raised.")
    assert "Q3" in out.text and "Q4" in out.text
    assert out.clean


def test_a_real_driver_licence_is_still_caught():
    """The threshold is raised, not the recogniser removed -- with a context
    word a genuine number scores well clear of the noise."""
    out = redact("My driver's license is D1234567.")
    assert "D1234567" not in out.text


def test_fiscal_period_labels_survive():
    """Quarter labels hit twice over -- driver-licence patterns match "Q3" as
    an alphanumeric, and spaCy tags "Q4" as a LOCATION at 0.85, which no
    threshold separates from a real city. Both found in a real transcript."""
    text = "Q3 earnings beat, Q4 guidance rose, FY26 looks flat, H1 was soft."
    assert redact(text).text == text


def test_a_real_place_is_still_redacted():
    """The escape hatch is a handful of tokens, not a hole in the net."""
    assert "Quito" not in redact("She flew to Quito last week.").text


def test_the_ner_model_is_let_go_when_reflection_is_done_with_it(monkeypatch):
    """~740 MB of the worker's memory, held all day after the first
    reflection (6 October 2026); it loads again in under three seconds."""
    import time

    from agent import redact as module

    monkeypatch.setattr(module, "_analyzer", object())
    monkeypatch.setattr(module, "_used_at", time.monotonic())
    assert module.release_if_idle() is False and module._analyzer is not None

    monkeypatch.setattr(module, "_used_at", time.monotonic() - module.IDLE_RELEASE_S - 1)
    assert module.release_if_idle() is True and module._analyzer is None
    assert module.release_if_idle() is False, "nothing loaded, nothing to drop"


def test_using_the_model_keeps_it_loaded(monkeypatch):
    import time

    from agent import redact as module

    engine = object()
    monkeypatch.setattr(module, "_analyzer", engine)
    monkeypatch.setattr(module, "_used_at", time.monotonic() - module.IDLE_RELEASE_S - 1)

    assert module._get_analyzer() is engine
    assert module.release_if_idle() is False
