"""Markdown -> chat text.

Signal renders no markdown at all; formatting rides along as separate style
ranges. So anything the model writes as syntax arrives as itself, and the
first real reply through the bridge came out as literal `**63°F**`.

The interesting cases are the ones where stripping would be *wrong*: a lone
asterisk doing arithmetic, a URL that is the only actionable part of a
sentence, an identifier with underscores in it.
"""

from __future__ import annotations

import pytest

from channels.plaintext import to_plain_text


@pytest.mark.parametrize(
    ("written", "shown"),
    [
        ("it is about **63F** today", "it is about 63F today"),
        ("*mild* and _quiet_", "mild and _quiet_"),
        ("***very*** important", "very important"),
        ("~~cancelled~~ moved", "cancelled moved"),
        ("## Summary", "Summary"),
        ("###### deep", "deep"),
        ("Use `git status` first", "Use git status first"),
    ],
    ids=["bold", "italic", "bold-italic", "strike", "header", "deep-header", "code"],
)
def test_syntax_that_would_show_as_itself_is_removed(written, shown):
    assert to_plain_text(written) == shown


def test_a_bullet_list_keeps_its_shape():
    """Structure survives -- a chat app renders line breaks and dashes fine.
    This is the whole difference from the speech transform, which flattens."""
    out = to_plain_text("- **Highs:** 60s\n* *mild*\n- clear")
    assert out == "- Highs: 60s\n- mild\n- clear"


def test_a_link_keeps_the_part_you_can_act_on():
    assert to_plain_text("See [the forecast](https://x.com/a).") == (
        "See the forecast (https://x.com/a)."
    )


def test_a_bare_url_is_left_alone():
    """Signal linkifies these itself."""
    assert to_plain_text("https://example.com/a_b") == "https://example.com/a_b"


def test_emoji_survive():
    """Unlike the speech path, where they get read aloud by name."""
    assert to_plain_text("Done ✅") == "Done ✅"


# --------------------------------------------------------------------------
# Things that look like markdown and are not
# --------------------------------------------------------------------------


def test_arithmetic_is_not_emphasis():
    assert to_plain_text("2 * 3 * 4 = 24") == "2 * 3 * 4 = 24"


def test_an_identifier_keeps_its_underscores():
    """Stripping `_` would mangle every snake_case name the agent mentions,
    which is a worse trade than leaving the rare italic marker visible."""
    assert to_plain_text("call to_plain_text on it") == "call to_plain_text on it"


def test_a_lone_asterisk_survives():
    assert to_plain_text("rated 4 out of 5 *") == "rated 4 out of 5 *"


def test_a_quote_loses_its_angle_brackets_and_keeps_its_lines():
    """The prompt asks for a passage the user must read as a quote, which
    the app marks with a line down its side. On a phone the "> " would show
    as itself at the start of each line."""
    written = ("The letter says:\n\n> Rent is due on **the 1st**.\n>\n> > Late fees apply.\n\n"
               "That's all.")
    assert to_plain_text(written) == ("The letter says:\n\nRent is due on the 1st.\n\n"
                                      "Late fees apply.\n\nThat's all.")


def test_a_greater_than_inside_a_line_survives():
    assert to_plain_text("the fare is > $40 now") == "the fare is > $40 now"


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------


def test_a_code_block_keeps_its_contents():
    assert to_plain_text("```python\nprint(1)\n```") == "print(1)"


def test_a_horizontal_rule_becomes_nothing():
    assert to_plain_text("above\n\n---\n\nbelow") == "above\n\nbelow"


def test_blank_runs_are_collapsed():
    assert to_plain_text("a\n\n\n\n\nb") == "a\n\nb"


@pytest.mark.parametrize("empty", ["", "   ", "\n\n"])
def test_nothing_in_nothing_out(empty):
    assert to_plain_text(empty) == ""
