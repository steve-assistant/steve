"""Evidence grounding: claims labelled directly supported / inferred / unsupported."""

from __future__ import annotations

import pytest

from tools.evidence import (
    DIRECT,
    INFERRED,
    UNSUPPORTED,
    Source,
    ground_claims,
)

EIFFEL = Source(
    url="https://example.com/eiffel",
    title="Eiffel Tower",
    text=(
        "The Eiffel Tower is a wrought-iron lattice tower in Paris, France. "
        "It was completed in 1889 and stands 330 metres tall."
    ),
)


# --------------------------------------------------------------------------
# The headline cases
# --------------------------------------------------------------------------


def test_a_claim_quoted_from_the_excerpt_is_directly_supported():
    v = ground_claims(["The Eiffel Tower is a wrought-iron lattice tower."], [EIFFEL]).verdicts[0]
    assert v.label == DIRECT
    assert v.url == EIFFEL.url
    assert v.score >= 0.7


def test_a_short_claim_fully_backed_by_the_excerpt_is_direct():
    """Every content word appears verbatim and the best run spans the claim's
    core -- a quotation, not an inference."""
    v = ground_claims(["The Eiffel Tower is in Paris."], [EIFFEL]).verdicts[0]
    assert v.label == DIRECT
    assert v.quote is not None
    assert "eiffel" in v.quote.lower()


def test_a_paraphrase_with_no_quotable_span_is_inferred():
    v = ground_claims(["The tower was finished in 1889."], [EIFFEL]).verdicts[0]
    assert v.label == INFERRED
    assert v.quote is None
    assert v.url == EIFFEL.url


def test_a_verbatim_span_beyond_which_the_claim_says_more_is_inferred():
    """Half the claim is a quotation, half is backed by nothing; the claim as
    written must not be labelled direct."""
    claim = (
        "The Eiffel Tower is a wrought-iron lattice tower in Paris, France, "
        "and it attracts twelve million visitors a year."
    )
    v = ground_claims([claim], [EIFFEL]).verdicts[0]
    assert v.label == INFERRED
    assert v.quote is not None


def test_an_unrelated_claim_is_unsupported_with_no_url_and_no_quote():
    v = ground_claims(["Berlin demolished the wall in 2001."], [EIFFEL]).verdicts[0]
    assert v.label == UNSUPPORTED
    assert v.url is None
    assert v.quote is None
    assert v.score == 0.0


def test_thin_overlap_still_points_at_the_closest_excerpt():
    """The claim shares "tower" with the source: unsupported, but the note
    points at the closest excerpt so a reader knows what was checked."""
    v = ground_claims(["The tower was demolished by Berlin in 2001."], [EIFFEL]).verdicts[0]
    assert v.label == UNSUPPORTED
    assert v.url == EIFFEL.url
    assert v.quote is None


def test_thin_overlap_is_unsupported_not_inferred():
    """One shared content word out of six is ~17% -- far below the inferred
    floor. Lexical grounding deliberately fails closed here."""
    v = ground_claims(
        ["Gustave Eiffel won the 1886 competition handsomely."], [EIFFEL]
    ).verdicts[0]
    assert v.label == UNSUPPORTED
    assert v.url == EIFFEL.url
    assert v.quote is None


# --------------------------------------------------------------------------
# Choosing the best source
# --------------------------------------------------------------------------


def test_the_best_matching_source_wins():
    other = Source(url="https://example.com/other", text="Towers are tall structures.")
    report = ground_claims(["The Eiffel Tower stands 330 metres tall."], [other, EIFFEL])
    assert report.verdicts[0].url == EIFFEL.url


def test_ties_go_to_the_earlier_source():
    a = Source(url="https://example.com/a", text="The tower is in Paris.")
    b = Source(url="https://example.com/b", text="The tower is in Paris.")
    v = ground_claims(["The tower is in Paris."], [a, b]).verdicts[0]
    assert v.url == a.url


def test_empty_source_list_labels_everything_unsupported():
    report = ground_claims(["Anything at all."], [])
    assert [v.label for v in report.verdicts] == [UNSUPPORTED]
    assert report.verdicts[0].detail == "no sources supplied"


# --------------------------------------------------------------------------
# Matching mechanics
# --------------------------------------------------------------------------


def test_matching_ignores_case_and_punctuation():
    src = Source(url="u", text="PARIS!! hosts the tower...")
    v = ground_claims(["Paris hosts the tower."], [src]).verdicts[0]
    assert v.label == DIRECT


def test_numbers_survive_normalization():
    v = ground_claims(["It was completed in 1889."], [EIFFEL]).verdicts[0]
    assert v.label in {DIRECT, INFERRED}


def test_hyphenated_compounds_match_when_spelled_the_same():
    """Hyphens are stripped within a token, so "wrought-iron" matches itself;
    splitting it into two words is a different token stream and rightly does
    not match."""
    v = ground_claims(["It is a wrought-iron lattice tower."], [EIFFEL]).verdicts[0]
    assert v.label == DIRECT


def test_the_quote_is_a_verbatim_window_with_context():
    claim = "It was completed in 1889 and stands 330 metres tall."
    v = ground_claims([claim], [EIFFEL]).verdicts[0]
    assert v.quote is not None
    assert "completed in 1889" in v.quote
    assert len(v.quote) <= 200


def test_a_single_shared_word_is_too_thin_to_quote():
    v = ground_claims(["The tower was demolished by Berlin in 2001."], [EIFFEL]).verdicts[0]
    assert v.label == UNSUPPORTED
    assert v.quote is None


def test_long_quotes_are_truncated_to_the_cap():
    src = Source(url="u", text="word " * 500)
    v = ground_claims(["word word word"], [src], max_quote_chars=50).verdicts[0]
    assert v.quote is not None
    assert len(v.quote) <= 50
    assert v.quote.endswith("...")


def test_claims_are_never_mutated_and_scores_stay_bounded():
    claims = ["The Eiffel Tower is in Paris.", "   ", "!!!"]
    report = ground_claims(claims, [EIFFEL])
    assert report.claims == claims
    for v in report.verdicts:
        assert 0.0 <= v.score <= 1.0
    # the second and third claims are blank / punctuation-only
    assert [v.label for v in report.verdicts] == [DIRECT, UNSUPPORTED, UNSUPPORTED]


def test_all_stopword_claims_fall_back_to_full_token_coverage():
    v = ground_claims(["it is the"], [Source(url="u", text="it is the")]).verdicts[0]
    assert v.score > 0
    assert v.label == INFERRED


# --------------------------------------------------------------------------
# Tuning and structure
# --------------------------------------------------------------------------


def test_thresholds_are_validated():
    with pytest.raises(ValueError):
        ground_claims(["x"], [EIFFEL], direct_min_run=0)
    with pytest.raises(ValueError):
        ground_claims(["x"], [EIFFEL], direct_min_coverage=1.5)


def test_loosening_thresholds_can_promote_a_label():
    claim = "The tower was finished in 1889."
    strict = ground_claims([claim], [EIFFEL]).verdicts[0].label
    loose = ground_claims(
        [claim], [EIFFEL], direct_min_run=2, direct_min_coverage=0.5
    ).verdicts[0].label
    assert strict == INFERRED
    assert loose == DIRECT


def test_report_counts_cover_all_three_labels():
    report = ground_claims(
        [
            "The Eiffel Tower is a wrought-iron lattice tower in Paris, France.",
            "The tower was finished in 1889.",
            "The tower was demolished by Berlin in 2001.",
        ],
        [EIFFEL],
    )
    assert report.counts == {DIRECT: 1, INFERRED: 1, UNSUPPORTED: 1}


def test_as_dict_round_trips_the_important_fields():
    d = ground_claims(["The Eiffel Tower is in Paris."], [EIFFEL]).as_dict()
    assert d["counts"] == {DIRECT: 1, INFERRED: 0, UNSUPPORTED: 0}
    assert d["sources"][0]["url"] == EIFFEL.url
    v = d["verdicts"][0]
    assert v["label"] == DIRECT
    assert v["url"] == EIFFEL.url
    assert v["quote"]


def test_render_produces_citation_ready_notes():
    report = ground_claims(
        [
            "The Eiffel Tower is a wrought-iron lattice tower in Paris, France.",
            "The tower was finished in 1889.",
            "The tower was demolished by Berlin in 2001.",
        ],
        [EIFFEL],
    )
    text = report.render()
    assert text.startswith("Evidence notes - 3 claim(s) against 1 source(s):")
    assert (
        "1. [directly supported] "
        "The Eiffel Tower is a wrought-iron lattice tower in Paris, France." in text
    )
    assert "2. [inferred] The tower was finished in 1889." in text
    assert "3. [unsupported] The tower was demolished by Berlin in 2001." in text
    assert EIFFEL.url in text


def test_render_flags_unbacked_claims_without_a_url():
    report = ground_claims(["The tower was demolished by Berlin in 2001."], [])
    text = report.render()
    assert "1. [unsupported] The tower was demolished by Berlin in 2001." in text
    assert "do not cite without checking" in text


def test_render_with_no_claims_is_not_blank():
    assert ground_claims([], [EIFFEL]).render() == "Evidence notes: no claims to ground."
