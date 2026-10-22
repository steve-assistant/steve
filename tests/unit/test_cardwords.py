"""A waiting card's word is taken out of what else goes to the phone
(agent/cardwords.py): the model's replies and the outbox's notices. A
reply that said it could pose as the card, and the user's yes would answer
the real one (step 11's third review). These pin that the word is found
however it is dressed, and that nothing else is touched."""

from __future__ import annotations

import pytest

from agent import cardwords
from agent.cardwords import PLACEHOLDER, Waiting
from core import db

MAPLE = Waiting("MAPLE", "Each run of this routine may, without asking: email: eve@example.com.")


def _cyrillic(text: str) -> str:
    """`text` with each a, e, o, p and c Cyrillic: the same on the screen."""
    return text.translate({ord("a"): 0x0430, ord("e"): 0x0435, ord("o"): 0x043E,
                           ord("p"): 0x0440, ord("c"): 0x0441,
                           ord("A"): 0x0410, ord("E"): 0x0415, ord("P"): 0x0420})


@pytest.mark.parametrize("written", [
    "Reply YES MAPLE to go ahead.",
    "Reply yes maple to go ahead.",
    "Reply YES " + "".join(chr(0xFF21 + ord(c) - ord("A")) for c in "MAPLE") + " to go ahead.",
    "Reply YES " + _cyrillic("MAPLE") + " to go ahead.",
    "Reply YES MA" + chr(0x200B) + "PLE to go ahead.",
    "Reply YES MAP" + chr(0x034F) + "LE to go ahead.",
    "Reply YES M A P L E to go ahead.",
    "Reply YES M-A-P-L-E to go ahead.",
    "Reply YES M" + chr(0x0336) + "APLE to go ahead.",
    "Reply YES M" + chr(0x00C1) + "PLE to go ahead.",
    "Reply YES " + "".join(chr(0x1D400 + ord(c) - ord("A")) for c in "MAPLE") + " to go ahead.",
    "Reply YES " + chr(0x1D0D) + chr(0x1D00) + chr(0x1D18) + chr(0x029F) + chr(0x1D07)
    + " to go ahead.",
    "Reply 'YES MAPLE' to go ahead.",
    "Reply YES " + "".join(c + chr(0x20E3) for c in "MAPLE") + " to go ahead.",
    "Reply YES MAPLE™ to go ahead.",
    "Reply YES MAPLE¹ to go ahead.",
    "Reply YES M•A•P•L•E to go ahead.",
    "Reply YES M;A;P;L;E to go ahead.",
    "Reply YES MA[PLE]() to go ahead.",
    "Reply YES M``AP``LE to go ahead.",
], ids=["plain", "lower", "fullwidth", "cyrillic", "zero-width-space", "grapheme-joiner",
        "spaced", "dashed", "struck-through", "accented", "bold-math", "small-capitals",
        "quoted", "keycaps", "trade-mark", "footnote", "bullets", "semicolons",
        "link-left-empty", "doubled-backticks"])
def test_a_waiting_cards_word_is_taken_out_however_it_is_dressed(written):
    said = cardwords.redact(written, [MAPLE])

    assert said.startswith("Reply ") and said.endswith(" to go ahead.")
    assert PLACEHOLDER in said
    assert "maple" not in said.lower()


def test_the_briefings_own_lines_for_a_card_keep_its_word():
    """The briefing lists what is waiting, each card's word beside its first
    line, and asks about what is still to do, a card whose first line is
    the question: each line is the card's first line and its word, and
    nothing else, so it may say the word."""
    asked = Waiting("OAK", "Still to do: “send the deck”?")
    briefing = "\n".join(["Waiting on you:", cardwords.listed("MAPLE", MAPLE.shows),
                          "Needs you:", "  · " + cardwords.question(asked.shows, "OAK"),
                          "Reply YES MAPLE to it."])

    assert cardwords.redact(briefing, [MAPLE, asked]) == briefing.replace(
        "Reply YES MAPLE", f"Reply YES {PLACEHOLDER}")


def test_a_line_that_only_contains_the_cards_first_line_loses_its_word():
    """Anything at all can stand beside the card's first line: a forged
    envelope, then the real one, then the word."""
    forged = f"Each run may email mom@example.com. {MAPLE.shows} Reply YES MAPLE."

    assert cardwords.redact(forged, [MAPLE]) == forged.replace("YES MAPLE",
                                                              f"YES {PLACEHOLDER}")


@pytest.mark.parametrize("written", [
    "Maple syrup is in the cupboard.",
    "The maples on our street turned red.",
    "We use mapleleaf.example for that.",
    "Reply YES OAK to go ahead.",
], ids=["another-sense", "plural", "inside-a-word", "another-word"])
def test_what_is_not_a_waiting_cards_word_is_left_alone(written):
    """Only a whole word, and only a waiting card's. The first is taken out
    too, while MAPLE waits: a word is a word."""
    said = cardwords.redact(written, [MAPLE])

    if written.startswith("Maple syrup"):
        assert said == f"{PLACEHOLDER} syrup is in the cupboard."
    else:
        assert said == written


@pytest.mark.parametrize("filler", ["\u2800", "\u3164", "\u115f", "\u1160", "\uffa0"])
def test_a_blank_filler_after_the_word_is_read_as_the_gap_it_shows(filler):
    """Read as nothing, "MAPLE" and a blank, then "to", was "mapleto" to
    the reading and "MAPLE to" on the phone (step 11's fourth review)."""
    said = cardwords.redact(f"Reply YES MAPLE{filler}to go ahead.", [MAPLE])

    assert said == f"Reply YES {PLACEHOLDER}{filler}to go ahead."


def test_what_is_sent_keeps_what_no_one_sees_only_where_an_emoji_needs_it():
    """A blank filler is sent as the space it shows; what else no one sees
    is not sent, so the reading and the phone see the same text. An emoji's
    joiner and a red heart's selector stay, or the family and the heart
    would come apart."""
    family, heart = "\U0001f468\u200d\U0001f469\u200d\U0001f467", "❤\ufe0f"
    sent = cardwords.visible(f"MA\u200bPLE {family} \u202eELPAM\u202c{heart}\u2800to")

    assert sent == f"MAPLE {family} ELPAM{heart} to"


def test_with_no_card_waiting_nothing_changes():
    assert cardwords.redact("Reply YES MAPLE.", []) == "Reply YES MAPLE."


def test_the_waiting_cards_are_those_still_pending(tmp_path):
    conn = db.open_store(tmp_path / "cards.db")
    try:
        with db.transaction(conn):
            for token, status in (("MAPLE", "pending"), ("COBALT", "done"),
                                  ("HARBOR", "approved")):
                conn.execute(
                    "INSERT INTO actions (tool, args_json, args_hash, preview, reach, reversal,"
                    " status, token) VALUES ('send_email', '{}', ?, 'First line\nmore',"
                    " 'outward', 'irreversible', ?, ?)", (token, status, token))

        assert cardwords.waiting(conn) == [Waiting("MAPLE", "First line", 1)]
    finally:
        conn.close()
