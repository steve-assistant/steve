"""Markdown-to-speech normalisation.

Every expectation here came from synthesising the input with Kokoro and
transcribing it back, so these are recordings of real misbehaviour rather than
guesses about what a TTS model might do.
"""

from __future__ import annotations

import pytest

from voice.speakable import to_speech

# --------------------------------------------------------------------------
# Emphasis -- the bug that started this
# --------------------------------------------------------------------------


def test_bold_is_not_read_as_asterisks():
    """Kokoro rendered "**75F**" as "asterisk-esterisk 75 degrees F"."""
    assert to_speech("It's **75 degrees** now.") == "It's 75 degrees now."


def test_italic_is_not_read_as_asterisks():
    assert to_speech("That is *important*.") == "That is important."


def test_bold_italic_together():
    assert to_speech("***Everything*** matters.") == "Everything matters."


def test_strikethrough_keeps_its_text():
    assert to_speech("~~wrong~~ right") == "wrong right."


def test_stray_asterisks_never_survive():
    """Whatever the paired patterns miss must still not be spoken aloud."""
    for text in ["**unclosed bold", "a ** b ** c", "__underline__"]:
        assert "**" not in to_speech(text)
        assert "__" not in to_speech(text)


@pytest.mark.parametrize(
    "written,spoken",
    [
        ("`git status`", "git status."),
        ("Run `ls -la` now.", "Run ls -la now."),
    ],
)
def test_inline_code_keeps_its_content(written, spoken):
    """Backticks are already silent in Kokoro; the content is worth hearing."""
    assert to_speech(written) == spoken


# --------------------------------------------------------------------------
# Emoji
# --------------------------------------------------------------------------


def test_emoji_are_removed_not_narrated():
    """Kokoro says "white heavy checkmark" and "party popper" out loud."""
    result = to_speech("Done ✅ all 3 tests passed \U0001f389")
    assert result == "Done all 3 tests passed."


def test_a_message_that_is_only_emoji_says_nothing():
    """Better silent than narrating pictogram names at someone."""
    assert to_speech("\U0001f389✅") == ""


@pytest.mark.parametrize(
    "emoji", ["\U0001f600", "✅", "❌", "\U0001f680", "⚠", "\U0001f1fa\U0001f1f8"]
)
def test_emoji_ranges_are_covered(emoji):
    assert to_speech(f"ok {emoji} done") == "ok done."


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------


def test_bare_urls_become_their_domain():
    """"https slash slash example com slash docs slash guide" is unusable."""
    assert to_speech("See https://example.com/docs/guide for details.") == (
        "See link to example.com for details."
    )


def test_markdown_links_speak_their_text_not_their_target():
    assert to_speech("Read the [Python docs](https://docs.python.org/3/) first.") == (
        "Read the Python docs first."
    )


def test_www_prefix_is_dropped_from_a_spoken_domain():
    assert "www." not in to_speech("Go to https://www.example.com/thing")


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


def test_headers_lose_their_hashes():
    """"## Summary" was spoken as "hash hash summary"."""
    assert to_speech("## Summary") == "Summary."


def test_bullets_become_separate_sentences():
    """Each item needs its own sentence, or the list is spoken as one
    breathless run-on -- and the chunker has nowhere to split it."""
    assert to_speech("- first item\n- second item") == "first item. second item."


def test_numbered_lists_lose_their_markers():
    assert to_speech("1. one\n2. two") == "one. two."


def test_horizontal_rules_are_silent():
    assert to_speech("---\nAfter the rule.") == "After the rule."


def test_blockquotes_lose_their_markers():
    assert to_speech("> quoted thing") == "quoted thing."


def test_table_borders_do_not_become_words():
    """Pipes leaked through as stray words; the cells are the content."""
    result = to_speech("| Rate | Value |\n| --- | --- |\n| in | $0.20 |")
    assert "|" not in result
    assert "---" not in result
    assert not result.startswith(",")
    assert "Rate" in result and "$0.20" in result


def test_approximately_sign_is_spoken_as_a_word():
    """"~0.04" was read as "tilde 0.04"."""
    assert to_speech("about ~5 minutes") == "about about 5 minutes."


# --------------------------------------------------------------------------
# Code blocks
# --------------------------------------------------------------------------


def test_a_multiline_code_block_is_described_not_recited():
    """Reading code aloud is punctuation soup, and it is on screen anyway."""
    result = to_speech("```python\nfor i in range(10):\n    print(i)\n```\nThat is it.")
    assert "print" not in result
    assert "code block" in result
    assert "That is it." in result


def test_a_one_line_code_block_is_short_enough_to_speak():
    assert to_speech("```\nls -la\n```") == "ls -la."


# --------------------------------------------------------------------------
# Things that must survive untouched
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Plain sentence with nothing special.",
        "The temperature is 75°F today.",
        "Cost: $0.00042 for that call.",
        "She said \"hello\" and left.",
        "It costs 50% more, roughly.",
        "Call me at 555-0100.",
    ],
)
def test_ordinary_prose_is_unchanged(text):
    assert to_speech(text) == text


def test_empty_input_produces_nothing():
    assert to_speech("") == ""
    assert to_speech("   \n  ") == ""


def test_output_never_ends_with_dangling_punctuation():
    for text in ["## Header", "- item", "**bold**", "> quote"]:
        result = to_speech(text)
        assert not result.endswith(("..", ",.", " ."))


def test_a_realistic_reply_reads_cleanly():
    reply = (
        "## Weather\n"
        "It's **75°F (24°C)** in Pittsburgh ☀️\n"
        "- Humidity: ~40%\n"
        "- Wind: 8 mph\n\n"
        "Source: [NWS](https://weather.gov/forecast)"
    )
    spoken = to_speech(reply)
    for banned in ["*", "#", "|", "https://", "☀"]:
        assert banned not in spoken, f"{banned!r} survived into speech: {spoken!r}"
    assert "75" in spoken and "NWS" in spoken
