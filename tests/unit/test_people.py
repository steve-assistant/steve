"""The eight hundred people problem.

A first pass over a real inbox produces hundreds of "people", almost all of
them Substack, LinkedIn and DoorDash. That is the watcher's twenty-five
recurring lectures in a new costume, and the failure is the same: a feature
that is technically working and practically unusable, because the signal is
buried under things nobody would call a contact.

So most of this file is about what gets flagged and what does not, and the
expensive direction is stated once here: **flagging a real person is the bad
mistake.** A newsletter in the contact list is noise; a colleague missing
from it means "who is Clara" comes back empty and the feature looks broken.
Hence `is_person = 0` rather than a delete, and hence one sent message
overriding every heuristic in the module.
"""

from __future__ import annotations

import datetime as dt

import pytest

from steve_sdk.classes import Message
from tests.gate import mailfake
from tools import ingest, people


def _recent() -> int:
    """A day ago, in epoch milliseconds: inside any window the ingest reads."""
    return int((dt.datetime.now() - dt.timedelta(days=1)).timestamp() * 1000)


def _box(*received: Message, sent: tuple[Message, ...] = ()) -> mailfake.Mailbox:
    """The user's one mail account, holding what they received and sent."""
    box = mailfake.Mailbox("maya@example.com", *received)
    box.sent.extend(sent)
    return box


# --------------------------------------------------------------------------
# Reading an address
# --------------------------------------------------------------------------


def test_a_named_address_splits_into_both_parts():
    assert people.parse_address("Clara Moss <clara@northwind.example>") == (
        "clara@northwind.example", "Clara Moss",
    )


def test_a_bare_address_has_no_name():
    assert people.parse_address("clara@northwind.example") == ("clara@northwind.example", None)


def test_a_comma_inside_a_quoted_name_does_not_split_the_header():
    """"Moss, Clara" is one recipient. Splitting on every comma would invent
    a person called Clara with no address at all."""
    parsed = people.parse_addresses('"Moss, Clara" <clara@northwind.example>, dan@fabrikam.example')

    assert [e for e, _ in parsed] == ["clara@northwind.example", "dan@fabrikam.example"]


# --------------------------------------------------------------------------
# What is not a person
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "noreply@x.example",
        "no-reply@x.example",
        "google-noreply@google.com",      # anywhere, not only at the start
        "notifications@link.example",
        "receipts@stripe.com",
        "search-api@search.example",
        "team@news.fly.io",               # the domain is the sender, not the team
        "hello@email.substack.com",
        "x@clinicservicemessage.example",
    ],
)
def test_a_mailbox_nobody_reads_is_flagged(address):
    assert people.looks_automated(address)


@pytest.mark.parametrize(
    "address",
    [
        "clara.moss@northwind.example",
        "maya@contoso.example",
        "captain@contosoballroom.example",
        "j.smith@fabrikam.example",
    ],
)
def test_a_person_is_not_flagged(address):
    """The expensive direction. A colleague filtered out means "who is
    Clara" answers nothing and the feature reads as broken."""
    assert not people.looks_automated(address)


def test_a_relayed_sender_is_flagged_by_its_name():
    """"william (via Google Sheets)" arrives from a per-recipient hash
    address that would otherwise become a new person every single time."""
    assert people.looks_automated(
        "drive-shares-dm-noreply@google.com", "william iris (via Google Sheets)"
    )


def test_writing_to_someone_settles_it(store):
    """The one signal worth more than every heuristic here: nobody replies
    to a shipping notification."""
    people.upsert(store, "receipts@stripe.com", name="Stripe")
    assert store.execute(
        "SELECT is_person FROM people WHERE email = 'receipts@stripe.com'"
    ).fetchone()["is_person"] == 0

    people.upsert(store, "receipts@stripe.com", sent=True)

    assert store.execute(
        "SELECT is_person FROM people WHERE email = 'receipts@stripe.com'"
    ).fetchone()["is_person"] == 1


# --------------------------------------------------------------------------
# Building the ledger
# --------------------------------------------------------------------------


def test_the_name_from_the_first_sighting_is_kept(store):
    """Headers vary per message -- "clara", "Clara Moss", "Moss, Clara". If
    the newest wins, the row changes identity at random."""
    people.upsert(store, "clara@northwind.example", name="Clara Moss")
    people.upsert(store, "clara@northwind.example", name="clara")

    assert people.find(store, "clara")[0].name == "Clara Moss"


def test_a_blank_name_is_filled_in_later(store):
    people.upsert(store, "clara@northwind.example")
    people.upsert(store, "clara@northwind.example", name="Clara Moss")

    assert people.find(store, "clara")[0].name == "Clara Moss"


def test_the_same_message_twice_is_one_interaction(store):
    """Windows overlap by design -- a nightly run reads three days back --
    so re-ingesting the same message is the normal case, not an error."""
    person_id = people.upsert(store, "clara@northwind.example")
    first = people.record_interaction(
        store, person_id, kind="email", direction="received",
        source_id="m1", occurred_at="2026-09-20T09:00:00",
    )
    second = people.record_interaction(
        store, person_id, kind="email", direction="received",
        source_id="m1", occurred_at="2026-09-20T09:00:00",
    )

    assert first is True and second is False
    assert len(people.history(store, person_id)) == 1


async def test_ingest_counts_both_directions(store, monkeypatch):
    mailfake.connect(monkeypatch, home=_box(
        Message("r1", sender="Clara Moss <clara@northwind.example>", when=_recent()),
        Message("r2", sender="no-reply@x.example", when=_recent()),
        sent=(Message("s1", to="Clara Moss <clara@northwind.example>", when=_recent()),)))

    result = await ingest.mail(store, days=7)

    assert result["sent"] == 1 and result["received"] == 2
    assert result["accounts"] == {"home": {"sent": 1, "received": 2}}
    clara = people.find(store, "clara")[0]
    assert clara.sent_count == 1 and clara.recv_count == 1


async def test_reading_the_same_mail_again_does_not_count_it_again(store, monkeypatch):
    """Every night re-reads three days, and the attention loop re-reads a
    week every few hours. Counted on every read, Clara writing twice looked
    like a mailing list writing six times -- and was demoted as one."""
    mailfake.connect(monkeypatch, home=_box(
        Message("r1", sender="Clara Moss <clara@northwind.example>", when=_recent()),
        Message("r2", sender="Clara Moss <clara@northwind.example>", when=_recent())))

    for _ in range(3):
        await ingest.mail(store, days=7)

    clara = people.find(store, "clara")[0]
    assert clara.recv_count == 2 and clara.is_person


def test_a_person_demoted_by_inflated_counts_comes_back(store):
    """The counts that drifted are rebuilt from the log, and the flags from
    the counts -- in both directions."""
    person_id = people.upsert(store, "captain@contoso.example", name="Ballroom Team Captain",
                              count=False)
    for n in range(2):
        people.record_interaction(
            store, person_id, kind="email", direction="received",
            source_id=f"m{n}", occurred_at="2026-09-20T09:00:00",
        )
    with store:
        store.execute("UPDATE people SET recv_count = 6, is_person = 0 WHERE id = ?", (person_id,))
    bulletin = people.upsert(store, "alex.kim@bulletin.example", count=False)
    for n in range(5):
        people.record_interaction(
            store, bulletin, kind="email", direction="received",
            source_id=f"b{n}", occurred_at="2026-09-20T09:00:00",
        )

    assert people.recount(store) == 2

    captain = people.get(store, person_id)
    assert captain.recv_count == 2 and captain.is_person
    assert not people.get(store, bulletin).is_person


class _SentDown(mailfake.Mailbox):
    """An account whose Sent folder cannot be listed tonight."""

    def envelopes(self, *, sent, since, limit):
        if sent:
            raise RuntimeError("the server is down")
        return super().envelopes(sent=sent, since=since, limit=limit)


async def test_a_failure_on_one_side_still_ingests_the_other(store, monkeypatch):
    mailfake.connect(monkeypatch, home=_SentDown(
        "maya@example.com", Message("r1", sender="clara@northwind.example", when=_recent())))

    result = await ingest.mail(store, days=7)

    assert result["received"] == 1
    assert result["errors"] and "sent" in result["errors"][0]


async def test_the_date_header_is_parsed_rather_than_stored_as_written(store, monkeypatch):
    """`last_seen` is compared with `>=` and `MAX()` on a text column, so an
    RFC 2822 header sorts alphabetically. "Fri" before "Mon" is not a
    chronology, and `recent()` would return the wrong people."""
    mailfake.connect(monkeypatch, home=_box(Message(
        "r1", sender="clara@northwind.example",
        extra={"date": "Sat, 19 Sep 2026 18:00:00 -0400"})))

    await ingest.mail(store, days=7)

    assert people.find(store, "clara")[0].last_seen.startswith("2026-09-19")


class _Windows(mailfake.Mailbox):
    """An account that notes how far back each listing reached, in days."""

    def __init__(self) -> None:
        super().__init__("maya@example.com")
        self.days: list[int] = []

    def envelopes(self, *, sent, since, limit):
        self.days.append(round((dt.datetime.now(dt.UTC) - since).total_seconds() / 86400))
        return []


async def test_a_first_run_reads_further_back_than_a_later_one(store, monkeypatch):
    """Without a cursor there is nothing to catch up from, so the window has
    to be the history; with one, three days is plenty and the rest is spend."""
    box = _Windows()
    mailfake.connect(monkeypatch, home=box)

    await ingest.mail(store)
    await ingest.mail(store)

    assert box.days == [ingest.FIRST_RUN_DAYS] * 2 + [3] * 2


# --------------------------------------------------------------------------
# One person, two addresses
# --------------------------------------------------------------------------


def test_the_two_header_conventions_reduce_to_the_same_name():
    """Exchange writes "Quinn, Nora" and Gmail writes "Anya
    Quinn". One professor, two rows, and nothing in either row looks
    wrong."""
    assert people.name_key("Quinn, Nora") == people.name_key("Nora Quinn")
    assert people.name_key("Moss, Clara (Contoso)") == people.name_key("Clara Moss")


def test_two_different_people_do_not_reduce_to_the_same_name():
    assert people.name_key("Clara Moss") != people.name_key("Ben Moss")


def test_merging_keeps_the_history_of_both(store):
    first = people.upsert(store, "nora.quinn@contoso.example", name="Quinn, Nora",
                          sent=True)
    second = people.upsert(store, "nora.quinn@example.com", name="Nora Quinn")
    people.record_interaction(store, first, kind="email", direction="sent",
                              source_id="a", occurred_at="2026-09-01T09:00:00")
    people.record_interaction(store, second, kind="email", direction="received",
                              source_id="b", occurred_at="2026-09-02T09:00:00")

    assert people.merge_duplicates(store) == 1

    found = people.find(store, "quinn")
    assert len(found) == 1
    assert found[0].sent_count == 1 and found[0].recv_count == 1
    assert len(people.history(store, found[0].id)) == 2


def test_a_commitment_follows_the_person_it_was_made_to(store):
    from tools import commitments

    first = people.upsert(store, "a@contoso.example", name="Moss, Clara", sent=True)
    second = people.upsert(store, "b@personal.example", name="Clara Moss")
    commitments.add(store, text="Send the deck", quote="I'll send the deck.",
                    person_id=second, source_id="m1")

    people.merge_duplicates(store)

    assert commitments.for_person(store, first)[0].text == "Send the deck"


def test_a_merged_address_does_not_come_back(store):
    """Without following the alias, the next message to the old address
    recreates the duplicate and the ledger un-merges itself overnight."""
    keep = people.upsert(store, "a@contoso.example", name="Moss, Clara", sent=True)
    people.upsert(store, "b@personal.example", name="Clara Moss")
    people.merge_duplicates(store)

    landed = people.upsert(store, "b@personal.example", name="Clara Moss")

    assert landed == keep
    assert len(people.find(store, "moss")) == 1


def test_a_one_word_name_is_never_evidence(store):
    """"Maya", "Support", "Team". Merging on those would fuse unrelated
    rows, and a wrong merge is invisible in a way a duplicate is not."""
    people.upsert(store, "one@x.example", name="Maya", sent=True)
    people.upsert(store, "two@y.example", name="Maya", sent=True)

    assert people.merge_duplicates(store) == 0
    assert len(people.find(store, "maya")) == 2


def test_a_nameless_row_is_never_merged(store):
    people.upsert(store, "one@x.example", sent=True)
    people.upsert(store, "two@y.example", sent=True)

    assert people.merge_duplicates(store) == 0
