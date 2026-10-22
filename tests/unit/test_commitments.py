"""Things the user said the user would do, and the two ways this feature dies.

**A fabricated obligation.** The user reads "you told Clara you'd send the deck
Friday", acts on it, finds there was no such message, and never trusts the
list again. One wrong row is worth more damage than ten missing ones, which
is why every commitment carries the sentence it came from and why the check
that it really says that is done by code after the model has spoken.

**A commitment closed on a guess.** Nothing here infers that something got
done. An item quietly marked complete fails invisibly, and the user finds out on
the day it was due -- which is the one moment the feature existed to prevent.
"""

from __future__ import annotations

from tools import commitments, people
from tools.extract import Candidate, own_words, verify

BODY = (
    "Hi Clara,\n\n"
    "Good to meet you today. I'll send you the deck by Friday, and I'll "
    "follow up about the case study next week.\n\n"
    "Maya\n\n"
    "On Tue, 16 Sep 2026, Clara Moss wrote:\n"
    "> Thanks! I'll get you the scheduling link tomorrow.\n"
)


# --------------------------------------------------------------------------
# What survives extraction
# --------------------------------------------------------------------------


def test_a_real_quote_survives():
    checked = verify(
        Candidate(text="Follow up about the case study",
                  quote="I'll follow up about the case study next week."),
        BODY,
    )
    assert checked.kept, checked.reason


def test_a_quote_cut_at_a_clause_boundary_survives():
    """A model asked for "the sentence" routinely stops at a comma and
    closes it with a full stop. That is tidiness, not invention, and
    refusing it would throw away most real finds to catch nothing."""
    checked = verify(
        Candidate(text="Send Clara the deck",
                  quote="I'll send you the deck by Friday."),
        BODY,
    )
    assert checked.kept, checked.reason


def test_an_invented_quote_is_discarded():
    """The failure this whole design exists for. The claim is plausible, the
    wording is plausible, and the message does not contain it."""
    checked = verify(
        Candidate(text="Send the deck", quote="I shall forward the presentation Friday."),
        BODY,
    )
    assert not checked.kept
    assert "not in the message" in checked.reason


def test_a_commitment_from_the_quoted_reply_is_discarded():
    """Clara promised the scheduling link, not the user. Attributing it to them
    would be a fabrication with a genuine quote attached, which is the worst
    shape this failure can take -- it survives every check but the right
    one."""
    checked = verify(
        Candidate(text="Send the scheduling link",
                  quote="I'll get you the scheduling link tomorrow."),
        BODY,
    )
    assert not checked.kept


def test_the_quoted_reply_is_cut_before_the_model_ever_sees_it():
    kept = own_words(BODY)
    assert "deck by Friday" in kept
    assert "scheduling link" not in kept


def test_an_outlook_reply_is_cut_at_its_from_and_sent_block_too():
    """Outlook quotes under a From/Sent block, with no "wrote:" line. The
    extractor's own reader missed it, so the other person's promises read
    as the user's."""
    body = ("Thanks, I'll send the deck by Friday.\n\n"
            "From: Clara Moss <clara@northwind.example>\n"
            "Sent: Tuesday, September 16, 2026 9:12 AM\n"
            "Subject: Deck\n\n"
            "I'll get you the scheduling link tomorrow.\n")

    kept = own_words(body)

    assert "deck by Friday" in kept and "scheduling link" not in kept


def test_whitespace_differences_do_not_discard_a_real_quote():
    """A model re-wrapping a line is not a fabrication, and treating it as
    one would throw away most of what it finds."""
    checked = verify(
        Candidate(text="Send Clara the deck",
                  quote="I'll send you\n  the deck   by Friday."),
        BODY,
    )
    assert checked.kept


def test_a_claim_its_own_quote_does_not_support_is_discarded():
    """The second gap: the quote is real, and the claim is an expansion of
    it. "Sign the lease" is not what "I'll look at the lease" says."""
    checked = verify(
        Candidate(text="Wire the deposit and sign the lease on the apartment",
                  quote="I'll send you the deck by Friday."),
        BODY,
    )
    assert not checked.kept


def test_a_malformed_due_date_is_dropped_rather_than_stored():
    """It would sort wrongly against every real date forever, silently."""
    checked = verify(
        Candidate(text="Send Clara the deck",
                  quote="I'll send you the deck by Friday.", due_date="next Friday"),
        BODY,
    )
    assert checked.kept
    assert checked.candidate.due_date is None


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


def test_a_commitment_cannot_be_stored_without_its_quote(store):
    assert commitments.add(store, text="Send the deck", quote="") is None
    assert commitments.counts(store)["total"] == 0


def test_the_same_commitment_from_the_same_message_is_stored_once(store):
    """Nightly windows overlap by three days, so re-reading a message is the
    normal case."""
    for _ in range(2):
        commitments.add(
            store, text="Send Clara the deck",
            quote="I'll send you the deck by Friday.", source_id="m1",
        )
    assert commitments.counts(store)["total"] == 1


def test_open_ones_put_the_soonest_first_and_the_undated_last(store):
    commitments.add(store, text="later", quote="q", due_date="2026-10-30", source_id="a")
    commitments.add(store, text="someday", quote="q", source_id="b")
    commitments.add(store, text="soon", quote="q", due_date="2026-09-22", source_id="c")

    assert [c.text for c in commitments.open_ones(store)] == ["soon", "later", "someday"]


def test_due_soon_includes_what_is_already_late(store):
    """Overdue is the most urgent case, not a past case to be filtered out."""
    commitments.add(store, text="late", quote="q", due_date="2020-01-01", source_id="a")
    commitments.add(store, text="distant", quote="q", due_date="2099-01-01", source_id="b")

    found = commitments.due_soon(store)

    assert [c.text for c in found] == ["late"]
    assert found[0].overdue


def test_the_digest_section_is_capped(store):
    """"Here are your 14 open commitments" every morning is how this section
    gets muted, and a muted section is worse than no section."""
    for i in range(10):
        commitments.add(
            store, text=f"thing {i}", quote="q", due_date="2020-01-01", source_id=str(i)
        )

    assert len(commitments.due_soon(store)) == commitments.MAX_IN_DIGEST


def test_closing_one_takes_it_off_the_list(store):
    commitment_id = commitments.add(
        store, text="Send the deck", quote="I'll send you the deck.", source_id="m1"
    )

    assert commitments.resolve(store, commitment_id) is True
    assert commitments.open_ones(store) == []


def test_closing_it_twice_reports_the_second_as_a_no_op(store):
    commitment_id = commitments.add(
        store, text="Send the deck", quote="I'll send you the deck.", source_id="m1"
    )
    commitments.resolve(store, commitment_id)

    assert commitments.resolve(store, commitment_id) is False


def test_there_is_no_way_to_close_one_except_by_saying_so():
    """Guards the design rather than a behaviour. If anything grows an
    inference path, a commitment can be closed on something the user did not do --
    and that failure is invisible until the deadline.

    Since 10 October 2026 commitments live in the obligations ledger, and
    every close goes through `obligations.close`, which takes the status
    from its caller and must be told what closed it (`by`). So: no SQL
    anywhere writes a closed status as a literal, and the one UPDATE that
    closes takes it as a parameter. Read from the SQL strings, whatever
    their spacing or line breaks."""
    import ast
    import inspect
    import re
    from pathlib import Path

    from tools import obligations

    sql = []
    for path in [*Path("agent").rglob("*.py"), *Path("tools").rglob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sql += [(path, " ".join(n.value.split())) for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    literal = [(str(p), s) for p, s in sql
               if re.search(r"\bUPDATE\s+(?:obligations|commitments)\b.*\bstatus\s*=\s*"
                            r"'(?:done|dropped|expired)'", s, re.IGNORECASE)]
    assert not literal, f"something closes an item with a status of its own: {literal}"

    closes = [s for p, s in sql if p.name == "obligations.py"
              and re.search(r"\bUPDATE\s+obligations\s+SET\s+status\s*=\s*\?", s, re.IGNORECASE)]
    assert len(closes) == 1, f"more than one thing closes an item: {closes}"
    assert "by" in inspect.signature(obligations.close).parameters, (
        "a close must say what closed it -- her words, a tick, a run, her no")


def test_a_commitment_remembers_who_it_is_to(store):
    person_id = people.upsert(store, "clara@northwind.example", name="Clara Moss")
    commitments.add(
        store, text="Send the deck", quote="I'll send you the deck.",
        person_id=person_id, source_id="m1",
    )

    found = commitments.for_person(store, person_id)

    assert len(found) == 1
    assert "Clara Moss" in found[0].line()


def test_the_same_message_reworded_is_still_one_commitment(store):
    """The real duplicate, from the night of 21 September. Windows overlap
    by three days, so a sent message is read on several nights, and the
    model summarises it differently each time -- "Send the doctor's note"
    and then "Send Zoe the doctor's note". Both landed, because the
    UNIQUE constraint is on the *text*. The quote is verbatim and does not
    drift, so it is what identity hangs on."""
    quote = "Will send the doctor's note once I get it."
    first = commitments.add(store, text="Send the doctor's note", quote=quote,
                            source_id="entry-1")
    second = commitments.add(store, text="Send Zoe the doctor's note",
                             quote=quote, source_id="entry-1")

    assert first is not None
    assert second is None
    assert commitments.counts(store)["total"] == 1


def test_the_same_words_from_a_different_message_are_two_commitments(store):
    """The user can promise the same thing to two people. Deduping on the quote
    alone would silently drop the second."""
    quote = "I'll send it over tomorrow."
    commitments.add(store, text="Send it", quote=quote, source_id="entry-1")
    commitments.add(store, text="Send it", quote=quote, source_id="entry-2")

    assert commitments.counts(store)["total"] == 2
