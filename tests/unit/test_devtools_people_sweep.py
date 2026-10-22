"""devtools/people_sweep.py: what it lists as a possible person, and what not.

The list is read by a person who decides which names are real. A sweep that
drops a name because the tests also use it as a variable, or because it sits
beside an email address, hides exactly the names that matter most; one that
lists every "Google Calendar" buries them.
"""

from devtools import people_sweep


def _found(text: str) -> set[str]:
    texts = {people_sweep.ROOT / "note.md": text}
    return set(people_sweep.candidates(texts, people_sweep.ordinary(texts)))


def test_two_capitalised_words_the_prose_never_uses_in_lower_case_are_listed():
    """A name is the pair nobody writes in lower case; "Open Calendar" is not
    one, because the prose says "the calendar is" and "the calendar sync"."""
    found = _found("We met Ingrid Lowe at the calendar sync, and the calendar is fine.\n"
                   "Open Calendar now.\n")

    assert "Ingrid Lowe" in found
    assert "Open Calendar" not in found


def test_a_name_used_as_a_variable_in_code_is_not_made_an_ordinary_word():
    """Tests write `ingrid = ...` all the time. Only a lower-case word between
    two others in prose is ordinary, so code cannot hide its own person."""
    found = _found("ingrid = people.add(conn, 'Ingrid Lowe')\n"
                   "ingrid = people.get(conn, ingrid)\n"
                   "Then ask Ingrid about it.\n")

    assert "Ingrid" in found


def test_a_name_written_beside_its_address_is_not_made_an_ordinary_word():
    """Mail fixtures write a display name before its address. The address is
    cut out before words are counted, so the name beside it is not prose."""
    found = _found("cc ingrid ingrid.lowe@example.com\n"
                   "cc ingrid ingrid.lowe@example.com\n"
                   "Then ask Ingrid about it.\n")

    assert "Ingrid" in found


def test_a_first_name_alone_beside_a_verb_a_greeting_or_a_possessive_is_listed():
    """A first name alone is the one a pair never catches."""
    found = _found("Send Ingrid the deck.\nHi Theo, thanks.\nDana said yes.\n"
                   "It is Sam's turn.\nThe calendar is the calendar.\n")

    assert {"Ingrid", "Theo", "Dana", "Sam"} <= found


def test_a_name_whose_first_word_is_also_an_ordinary_word_is_still_listed():
    """"mark" is a word, and Mark is a first name: the surname decides, and a
    pair inside a longer run of capitals is read too."""
    found = _found("We mark the box, and we mark the date.\nAsk Mark Holloway first.\n")

    assert "Mark Holloway" in found


def test_names_with_an_inner_capital_or_an_apostrophe_are_listed():
    """Read as plain `[A-Z][a-z]+`, "McAllister" was never seen and "O'Hara"
    was seen cut short, and they are as likely to be real as any surname."""
    found = _found("We met Ingrid McAllister.\nO'Hara said no.\nAsk DeVries.\n")

    assert {"Ingrid McAllister", "O'Hara", "DeVries"} <= found
