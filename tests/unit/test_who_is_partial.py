"""`who_is` says how it matched, so "alex" inside "Alexandra" is not a person.

On 5 October 2026 the doer asked the ledger about "Alex", for a prep call
whose title was all it had. `people.find` matches substrings, so the one
row it returned was "Wren, Alexandra" (one email sent in August), and the
brief said "Alex appears to be Alexandra Wren at Contoso" (session 252, msgs
1970 and 1978). It went to their phone on 7 October and the user answered "That's
who Alex is, not Alexandra Wren". Nothing in what `who_is` handed back
told a candidate from an identity.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools import people


def _person(conn, email: str, name: str, *, sent: int = 1, org: str | None = None) -> int:
    person_id = people.upsert(conn, email, name=name, sent=True)
    with conn:
        conn.execute("UPDATE people SET sent_count = ?, org = ? WHERE id = ?",
                     (sent, org, person_id))
    return person_id


def _who_is(conn):
    from agent.toolset import people as tools

    found: dict = {}
    tools.register(lambda fn: found.setdefault(fn.__name__, fn))
    ctx = SimpleNamespace(deps=SimpleNamespace(conn=conn, tools_used=[]))

    async def ask(query: str) -> dict:
        return await found["who_is"](ctx, query)

    return ask


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("alex", "partial"),  # inside "Alexandra", the incident
        ("alexandra", "exact"),
        ("wren", "exact"),  # a surname, though the name is written "Wren, Alexandra"
        ("Alexandra Wren", "exact"),
        ("alexandra.wren@contoso.example", "exact"),
        ("contoso", "exact"),  # a whole word of her address
        ("lexandr", "partial"),
        ("contoso.e", "partial"),
        ("", "partial"),
        ("   ", "partial"),
    ],
)
def test_a_word_inside_a_longer_name_is_only_a_partial_match(conn, query, expected):
    person_id = _person(conn, "alexandra.wren@contoso.example", "Wren, Alexandra")

    assert people.match_quality(people.get(conn, person_id), query) == expected


def test_an_organisation_word_is_a_whole_word_match_and_a_piece_of_it_is_not(conn):
    person_id = _person(conn, "sam@northwind.example", "Sam Lee", org="Northwind Traders")
    sam = people.get(conn, person_id)

    assert people.match_quality(sam, "northwind") == "exact"
    assert people.match_quality(sam, "trade") == "partial"


async def test_who_is_says_how_each_match_matched_and_warns_when_none_is_whole(conn):
    _person(conn, "alexandra.wren@contoso.example", "Wren, Alexandra")

    result = await _who_is(conn)("alex")

    assert [p["match"] for p in result["people"]] == ["partial"]
    assert "None of them is known to be who was meant" in result["note"]


async def test_who_is_gives_no_warning_when_a_match_is_a_whole_word(conn):
    _person(conn, "alexandra.wren@contoso.example", "Wren, Alexandra")

    result = await _who_is(conn)("wren")

    assert [p["match"] for p in result["people"]] == ["exact"]
    assert "note" not in result


async def test_who_is_shows_the_one_whole_word_match_among_busier_partial_ones(conn):
    """The ledger ranks by how much mail a person has, and the row limit is
    four: five busy Alexandras and Alexes hid the one Alex."""
    for email, name in [("alexandra.wren@contoso.example", "Wren, Alexandra"),
                        ("alexis@contoso.example", "Alexis Lee"),
                        ("alexander@contoso.example", "Alexander Park"),
                        ("alexei@contoso.example", "Alexei Roe"),
                        ("alexa@contoso.example", "Alexa Tan")]:
        _person(conn, email, name, sent=30)
    _person(conn, "aarden@alumni.example", "Alex Arden", sent=1)

    result = await _who_is(conn)("alex")

    assert result["found"] == 4
    assert [(p["email"], p["match"]) for p in result["people"]][0] == (
        "aarden@alumni.example", "exact")
    assert [p["match"] for p in result["people"]].count("partial") == 3
    assert "note" not in result


async def test_who_is_with_no_match_still_says_so_plainly(conn):
    result = await _who_is(conn)("nobody")

    assert result == {"found": 0, "query": "nobody", "people": []}
