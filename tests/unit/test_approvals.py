"""Asking before acting, and doing it exactly once.

Two failures dominate this file and both are silent. Sending an email twice
looks like nothing went wrong from inside the system -- the ledger says done,
the model said it queued one thing, and only the recipient knows. And
approving the wrong card when two are waiting produces a confident, complete,
entirely incorrect action with no error anywhere.

So most of what follows is about concurrency and about refusing to guess.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
from datetime import datetime, timedelta

import pytest

from agent import approvals as ap
from core import db, monitor, passphrase
from core.consent import Consent, Reach, Reversal, Rules, classify

OUTWARD = Consent(
    gated=True, reach=Reach.OUTWARD, reversal=Reversal.IRREVERSIBLE,
    reasons=["a sent email cannot be unsent"],
)
PRIVATE = Consent(gated=False, reach=Reach.PRIVATE, reversal=Reversal.REVERSIBLE)


@pytest.fixture
def store(tmp_path):
    conn = db.open_store(tmp_path / "ap.db")
    yield conn
    conn.close()


@pytest.fixture
def wired(monkeypatch):
    """A notifier and an executor that record rather than act."""
    state = {"cards": [], "ran": [], "result": {"ok": True}, "explode": False}

    async def notify(text: str, **_) -> bool:
        state["cards"].append(text)
        return True

    async def execute(tool: str, args: dict) -> dict:
        if state["explode"]:
            raise RuntimeError("gmail is down")
        state["ran"].append((tool, args))
        return state["result"]

    monkeypatch.setattr(ap, "_notify", notify)
    monkeypatch.setattr(ap, "_execute_tool", execute)
    return state


async def _ask(store, *, tool="send_email", args=None, consent=OUTWARD, preview="Email to Clara"):
    receipt = await ap.request(
        store, tool=tool, args=args or {"to": "clara@contoso.example"},
        preview=preview, consent=consent,
    )
    # The model's receipt has no word (agent/cardwords.py); the card has.
    if "action_id" not in receipt:
        return receipt
    return {**receipt, "token": ap.get(store, receipt["action_id"]).token}


# --------------------------------------------------------------------------
# Exactly once
# --------------------------------------------------------------------------


async def test_approving_twice_sends_one_email(store, wired):
    """The most important test here. A tray click and a Signal "yes" can
    arrive together, and the recipient is the only one who would ever find
    out that it happened twice."""
    receipt = await _ask(store)
    ap.decide(store, receipt["action_id"], approve=True, by="signal")

    first, second = await asyncio.gather(
        ap.execute(store, receipt["action_id"], hold=False),
        ap.execute(store, receipt["action_id"], hold=False),
    )

    assert len(wired["ran"]) == 1, "the action ran twice"
    assert first["ok"] != second["ok"], "both calls claimed to have run it"


async def test_executing_an_unapproved_action_does_nothing(store, wired):
    receipt = await _ask(store)
    result = await ap.execute(store, receipt["action_id"], hold=False)

    assert result["ok"] is False
    assert wired["ran"] == []


async def test_a_rejected_action_never_runs(store, wired):
    receipt = await _ask(store)
    ap.decide(store, receipt["action_id"], approve=False, by="signal")

    assert (await ap.execute(store, receipt["action_id"], hold=False))["ok"] is False
    assert wired["ran"] == []


async def test_the_same_request_twice_is_one_card(store, wired):
    """`defer_to_background` grew this guard because one request became three
    jobs. Here it would be three emails."""
    first = await _ask(store)
    second = await _ask(store)
    # The card is fired rather than awaited, so give the loop a tick before
    # counting -- asserting immediately tests the scheduler, not the guard.
    await asyncio.sleep(0.05)

    assert second["status"] == "already_queued"
    assert second["action_id"] == first["action_id"]
    assert len(ap.pending(store)) == 1
    assert len(wired["cards"]) == 1, "the repeat asked her a second time"


# --------------------------------------------------------------------------
# The ledger records everything, not just what was asked about
# --------------------------------------------------------------------------


def test_a_free_action_is_still_recorded(store):
    """An audit log of only the things you were already watching is not an
    audit log."""
    action_id = ap.record_free(
        store, tool="modify_email", args={"archive": True},
        consent=PRIVATE, preview="Archived 3 messages",
    )
    row = store.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()

    assert row["status"] == "done"
    assert row["reach"] == "private"
    assert row["executed_at"] is not None


async def test_the_gated_path_records_why(store, wired):
    receipt = await _ask(store)
    row = store.execute(
        "SELECT reasons FROM actions WHERE id = ?", (receipt["action_id"],)
    ).fetchone()
    assert "cannot be unsent" in json.loads(row["reasons"])[0]


# --------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------


async def test_a_card_reaches_them_without_holding_the_turn(store, wired):
    """The notify is fired, not awaited -- a Signal round trip must not be
    inside the conversation's latency."""
    await _ask(store)
    await asyncio.sleep(0.05)

    assert wired["cards"], "nothing was sent"
    assert "YES" in wired["cards"][0]


async def test_the_card_carries_the_token_and_the_reason(store, wired):
    receipt = await _ask(store)
    await asyncio.sleep(0.05)
    card = wired["cards"][0]

    assert receipt["token"] in card
    assert "cannot be unsent" in card


async def test_a_long_card_is_stored_and_sent_whole(store, wired):
    """Cut at 300 characters until 4 October 2026, and what the user approved was
    whatever survived the cut. What keeps mail text off a channel now is that
    a card never quotes a body (the next test), not its length."""
    guests = ", ".join(f"guest{i:02d}@example.org" for i in range(40))
    await _ask(store, tool="create_event", args={"attendees": guests},
               preview=f"Club dinner\nInvites: {guests}")
    await asyncio.sleep(0.05)

    row = store.execute("SELECT preview FROM actions").fetchone()
    assert row["preview"].endswith(guests) and guests in wired["cards"][0]


async def test_a_card_that_quotes_the_body_it_would_send_is_not_asked(store, wired):
    body = "Hi Clara,\n\nThe quarterly numbers came in at 4.2 million this time.\n\nMaya"

    receipt = await _ask(store, args={"to": "clara@contoso.example", "body": body},
                         preview=f"Email to Clara\n\n{body}")

    assert receipt["status"] == "refused" and "never quotes" in receipt["error"]
    assert store.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert wired["cards"] == []


async def test_the_receipt_tells_the_model_to_stop(store, wired):
    """Without this the model calls the tool again, having read its own
    "queued" as a failure."""
    receipt = await _ask(store)
    assert "Do not call this again" in receipt["note"]
    assert receipt["status"] == "awaiting_approval"


def test_a_new_card_never_draws_the_word_of_a_waiting_card_whose_module_is_off(
        store, monkeypatch):
    """The cards waiting hide one whose module or account is off, and the
    unique index still counts it: a second card drew its word 1 time in 48,
    and the turn that asked failed (the persona eval's Outlook day)."""
    monkeypatch.setattr(secrets, "choice", lambda words: words[0])  # the same draw each time
    _, hidden = ap.queue(store, tool="perform", args={"module_key": "m0dkey0001", "do": "x"},
                         preview="A module's action", consent=OUTWARD)
    assert hidden is not None and ap.pending(store) == []
    _, made = ap.queue(store, tool="send_email", args={"to": "clara@contoso.example"},
                       preview="Email to Clara", consent=OUTWARD)
    assert made is not None and made.token != hidden.token


def test_an_answered_cards_word_comes_round_again_only_after_every_other(store, monkeypatch):
    """A word went back into the draw the moment its card was answered, so
    "YES MAPLE" typed late, from a card read long after, approved the next
    card to draw MAPLE: one the user had not seen (step 11's audit)."""
    monkeypatch.setattr(secrets, "choice", lambda words: words[0])  # the same draw each time
    _, answered = ap.queue(store, tool="send_email", args={"to": "clara@contoso.example"},
                           preview="Email to Clara", consent=OUTWARD)
    ap.decide(store, answered.id, approve=False, by="app")

    _, made = ap.queue(store, tool="send_email", args={"to": "hugo@contoso.example"},
                       preview="Email to Hugo", consent=OUTWARD)

    assert made.token != answered.token
    every = {w.upper(): n for n, w in enumerate(ap.TOKEN_WORDS, start=1)}
    assert ap.token_for(set(), every) == ap.TOKEN_WORDS[0].upper()
    assert ap.token_for({ap.TOKEN_WORDS[0]}, every) == ap.TOKEN_WORDS[1].upper()


def test_once_every_word_has_had_a_turn_the_next_cannot_be_told_in_advance(monkeypatch):
    """Drawn as the one unused longest, the next card's word was known to
    anyone who had seen the cards before it, the model too: a reply could
    say "Reply YES <it>" before that card existed (step 11's fourth review).
    It is drawn at random from the older half, which still keeps a late yes
    from meeting a newer card with its word until half the rest have gone."""
    offered = []
    monkeypatch.setattr(secrets, "choice", lambda words: offered.append(list(words)) or words[-1])
    every = {w.upper(): n for n, w in enumerate(ap.TOKEN_WORDS, start=1)}

    drawn = ap.token_for(set(), every)

    assert offered == [list(ap.TOKEN_WORDS[:len(ap.TOKEN_WORDS) // 2])]
    assert drawn == offered[0][-1].upper()


def test_with_few_words_never_used_left_the_next_is_still_one_of_many(monkeypatch):
    """Drawn from the never-used words alone, the last of them was the next
    card's word for certain once every other had had its turn, and one of
    two the card before: known in advance to whoever had seen the cards
    before, the model too (step 11's fifth review). The draw is from them
    and the longest unused beside them, half the free words in all; while
    more than half are never used, from every one of them."""
    offered = []
    monkeypatch.setattr(secrets, "choice", lambda words: offered.append(list(words)) or words[0])
    half = len(ap.TOKEN_WORDS) // 2
    for left in (1, 2, half - 1, half + 2):
        used = {w.upper(): n for n, w in enumerate(ap.TOKEN_WORDS[:-left], start=1)}

        drawn = ap.token_for(set(), used)

        assert drawn == ap.TOKEN_WORDS[-left].upper()
        assert sorted(offered[-1]) == sorted(
            ap.TOKEN_WORDS[-left:] + ap.TOKEN_WORDS[:max(0, half - left)])


def test_cards_made_at_once_on_two_connections_never_draw_the_same_word(tmp_path, monkeypatch):
    """A briefing makes its cards on a thread and connection of its own while a
    turn makes its own. Each read the words taken before either wrote, and
    both drew the same one: one card was lost, or one yes answered two."""
    path = tmp_path / "ap.db"
    db.open_store(path).close()
    monkeypatch.setattr(secrets, "choice", lambda words: words[0])
    first_drew, second_drew = threading.Event(), threading.Event()
    draw = ap.token_for

    def token_for(taken, *rest):
        word = draw(taken, *rest)
        if threading.current_thread().name == "first":
            first_drew.set()
            second_drew.wait(1.0)  # the second draws now, unless the first holds the lock
        else:
            second_drew.set()
        return word

    monkeypatch.setattr(ap, "token_for", token_for)
    made, failed = {}, []

    def make(name: str, to: str) -> None:
        conn = db.connect(path)
        try:
            made[name] = ap.queue(conn, tool="send_email", args={"to": to},
                                  preview=f"Email to {to}", consent=OUTWARD)[1].token
        except Exception as exc:  # noqa: BLE001 - the failure is the finding
            failed.append(repr(exc))
        finally:
            conn.close()

    first = threading.Thread(target=make, args=("first", "clara@contoso.example"), name="first")
    first.start()
    assert first_drew.wait(5.0)
    second = threading.Thread(target=make, args=("second", "dana@contoso.example"),
                              name="second")
    second.start()
    first.join(15.0)
    second.join(15.0)
    assert failed == [] and len(set(made.values())) == 2, (made, failed)


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------


async def test_an_expired_action_cannot_be_approved(store, wired):
    receipt = await _ask(store)
    stale = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    store.execute("UPDATE actions SET expires_at = ? WHERE id = ?",
                  (stale, receipt["action_id"]))
    store.commit()

    outcome = ap.decide(store, receipt["action_id"], approve=True, by="signal")
    assert outcome["ok"] is False
    assert "expired" in outcome["error"]


async def test_expiry_is_checked_on_approval_not_only_by_a_sweeper(store, wired):
    """A sweeper lags. The check on the approve path is the one that decides
    whether it runs."""
    receipt = await _ask(store)
    stale = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    store.execute("UPDATE actions SET expires_at = ? WHERE id = ?",
                  (stale, receipt["action_id"]))
    store.commit()

    ap.decide(store, receipt["action_id"], approve=True, by="signal")
    assert ap.get(store, receipt["action_id"]).status == "expired"


async def test_sweeping_marks_old_cards_expired(store, wired):
    """Stamped in local time, because that is what `request()` writes. This
    test used to set a UTC value and so encoded the very mismatch the sweep
    had -- a test agreeing with a bug rather than with production."""
    receipt = await _ask(store)
    stale = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    store.execute(
        "UPDATE actions SET expires_at = ? WHERE id = ?", (stale, receipt["action_id"])
    )
    store.commit()

    assert ap.expire_stale(store) == 1
    assert ap.pending(store) == []


# --------------------------------------------------------------------------
# The hold
# --------------------------------------------------------------------------


async def test_an_irreversible_action_can_be_undone_during_the_hold(store, wired, monkeypatch):
    """It converts the one class of mistake that cannot be taken back, for
    the cost of thirty seconds."""
    monkeypatch.setattr(ap, "HOLD_SECONDS", 0.2)
    receipt = await _ask(store)
    ap.decide(store, receipt["action_id"], approve=True, by="signal")

    running = asyncio.create_task(ap.execute(store, receipt["action_id"]))
    await asyncio.sleep(0.05)
    assert ap.cancel_during_hold(store, receipt["action_id"]) is True

    assert (await running)["ok"] is False
    assert wired["ran"] == [], "it sent anyway"


async def test_undo_of_an_approved_action_still_waiting_its_turn_stops_it(store, wired,
                                                                        monkeypatch):
    """Approved behind another job, it was not yet `executing`: UNDO said
    "too late -- that already went", and then it ran."""
    from agent import runtime
    from agent.worker import answering

    monkeypatch.setattr(runtime, "conn", store)
    receipt = await _ask(store)
    ap.decide(store, receipt["action_id"], approve=True, by="signal")

    said = await answering.decide_action(receipt["action_id"], approve=False, by="signal")

    assert said.get("undone") is True
    assert (await ap.execute(store, receipt["action_id"], hold=False))["ok"] is False
    assert wired["ran"] == []


async def test_a_new_card_never_shares_its_word_with_one_still_in_its_undo_window(
        store, wired, monkeypatch):
    """Words were kept apart among pending cards only. One the user had just said
    yes to holds its word for the undo window, and replies look there first:
    a new card given the same word had "YES SABLE" answered "SABLE is
    already going", and "NO SABLE" stop the other action. A gate test failed
    on it, one run in 48 (8 October 2026)."""
    monkeypatch.setattr("core.consent.TOKEN_WORDS", ["SABLE"])
    first = await _ask(store)
    ap.decide(store, first["action_id"], approve=True, by="signal")
    assert [a.token for a in ap.holding(store)] == ["SABLE"]

    second = await _ask(store, args={"to": "rina@contoso.example"}, preview="Email to Rina")

    assert second["token"] != "SABLE"


async def test_a_reversible_action_does_not_wait(store, wired, monkeypatch):
    """The hold is for things that cannot be taken back. Making everything
    wait would be friction for nothing."""
    monkeypatch.setattr(ap, "HOLD_SECONDS", 30)
    receipt = await _ask(store, consent=Consent(
        gated=True, reach=Reach.OUTWARD, reversal=Reversal.REVERSIBLE, reasons=["x"]))
    ap.decide(store, receipt["action_id"], approve=True, by="signal")

    result = await asyncio.wait_for(ap.execute(store, receipt["action_id"]), timeout=2)
    assert result["ok"] is True


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


async def test_a_spend_over_the_cap_is_refused_even_when_approved(store, wired):
    """Approval does not override a ceiling. That is what makes it a ceiling
    rather than a default -- it is what a tired tap at eleven cannot undo."""
    consent = Consent(gated=True, reach=Reach.OUTWARD, reversal=Reversal.IRREVERSIBLE,
                      reasons=["money"], spend_usd=5000.0)
    receipt = await _ask(store, tool="purchase", args={"amount_usd": 5000}, consent=consent)
    # Approved properly, in the app with the passphrase (core/monitor.py):
    # the cap must hold however the yes was given.
    passphrase.choose("correct horse battery")
    monitor.accept_passphrase(store, receipt["action_id"], "purchase", "correct horse battery")
    assert ap.decide(store, receipt["action_id"], approve=True, by="tray")["ok"]

    result = await ap.execute(store, receipt["action_id"], hold=False)
    assert result["ok"] is False
    assert "cap" in result["error"]
    assert wired["ran"] == []


def test_spending_accumulates_across_actions(store):
    for _ in range(3):
        store.execute(
            "INSERT INTO actions (tool,args_json,args_hash,preview,reach,reversal,"
            "spend_usd,status,executed_at) VALUES ('purchase','{}','h','p','outward',"
            "'irreversible',30.0,'done',datetime('now'))"
        )
    store.commit()
    assert ap.spent_since(store, 1) == pytest.approx(90.0)


def test_a_free_action_is_not_capped(store):
    assert ap.over_cap(store, 0.0) is None


async def test_an_action_is_not_counted_twice_against_its_own_cap(store, wired, monkeypatch):
    """By the cap's check the action is `executing`, which the day's total
    counts: $15 spent and $45 more is $60, under a $60 cap, not $105."""
    monkeypatch.setattr(ap, "spend_caps", lambda: {"per_action_usd": 0, "per_day_usd": 60,
                                                   "per_week_usd": 0})
    store.execute(
        "INSERT INTO actions (tool,args_json,args_hash,preview,reach,reversal,"
        "spend_usd,status,executed_at) VALUES ('purchase','{}','h','p','outward',"
        "'irreversible',15.0,'done',datetime('now'))")
    store.commit()
    consent = Consent(gated=True, reach=Reach.OUTWARD, reversal=Reversal.IRREVERSIBLE,
                      reasons=["money"], spend_usd=45.0)
    receipt = await _ask(store, tool="purchase", args={"amount_usd": 45}, consent=consent)
    passphrase.choose("correct horse battery")
    monitor.accept_passphrase(store, receipt["action_id"], "purchase", "correct horse battery")
    ap.decide(store, receipt["action_id"], approve=True, by="tray")

    said = await ap.execute(store, receipt["action_id"], hold=False)

    assert said["ok"] is True, said
    assert len(wired["ran"]) == 1


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -100.0])
def test_an_amount_that_is_not_a_price_is_refused_by_the_caps(store, amount):
    """The amount is the model's argument. NaN passed every comparison and
    SQLite's SUM skipped it; a negative one lowered the totals."""
    assert ap.over_cap(store, amount)


@pytest.mark.parametrize("written", ["fifty dollars", "nan", "NaN", float("nan")])
def test_an_amount_the_model_wrote_that_is_not_a_number_is_never_free(written):
    """It was 0, and NaN was stored as NULL and read back as 0: a purchase
    with no price the caps could see."""
    rules = Rules(free={}, gated={"purchase": {"spend_arg": "amount_usd"}}, spend={})

    asked = classify("purchase", {"amount_usd": written}, rules=rules)

    assert asked.spend_usd == float("inf")


# --------------------------------------------------------------------------
# Failure
# --------------------------------------------------------------------------


async def test_a_failing_action_is_recorded_not_raised(store, wired):
    wired["explode"] = True
    receipt = await _ask(store)
    ap.decide(store, receipt["action_id"], approve=True, by="signal")

    result = await ap.execute(store, receipt["action_id"], hold=False)
    assert result["ok"] is False
    assert ap.get(store, receipt["action_id"]).status == "failed"


async def test_an_unknown_action_is_an_error_not_a_crash(store):
    assert ap.decide(store, 9999, approve=True, by="tray")["ok"] is False
    assert (await ap.execute(store, 9999, hold=False))["ok"] is False


# --------------------------------------------------------------------------
# Classification feeds the ledger
# --------------------------------------------------------------------------


def test_what_the_classifier_says_is_what_gets_stored(store):
    consent = classify("send_email", {"to": "x@y.example"})
    action_id = ap.record_free(store, tool="send_email", args={}, consent=consent)
    row = store.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()

    assert row["reach"] == "outward"
    assert row["reversal"] == "irreversible"


async def test_a_fresh_card_is_not_swept_away_early(store, wired):
    """The sweep and `Action.expired` must give the same answer.

    They did not. `expires_at` is written by Python in local time and the
    sweep compared it against SQLite's UTC `now`, which runs four hours ahead
    here -- so a twelve-hour TTL retired after eight, and a card the user could
    still legitimately approve had already been marked expired underneath
    them. Two answers to one question, differing by the UTC offset.
    """
    receipt = await _ask(store)
    action_id = receipt["action_id"]

    assert ap.get(store, action_id).expired is False
    assert ap.expire_stale(store) == 0, "the sweep retired a live card"
    assert ap.get(store, action_id).status == "pending"


async def test_the_sweep_and_the_property_agree_once_it_really_has_expired(store, wired):
    """The other direction, so the fix cannot be "never expire anything"."""
    receipt = await _ask(store)
    stale = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    store.execute(
        "UPDATE actions SET expires_at = ? WHERE id = ?", (stale, receipt["action_id"])
    )
    store.commit()

    assert ap.get(store, receipt["action_id"]).expired is True
    assert ap.expire_stale(store) == 1


# --------------------------------------------------------------------------
# A failure reported in the result is a failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [{"ok": False, "error": "recipient address rejected"}, {"error": "quota exceeded"}],
)
async def test_an_executor_that_reports_failure_is_not_done(store, wired, monkeypatch, result):
    """Executors return a dict and never raise, so a refused send comes back
    as `{"ok": False, ...}` -- and only an exception used to count. The email
    that bounced was marked done and announced as "Done — Email to Clara"."""
    monkeypatch.setattr(ap, "HOLD_SECONDS", 0)
    wired["result"] = result
    receipt = await _ask(store)
    ap.decide(store, receipt["action_id"], approve=True, by="signal")

    outcome = await ap.execute(store, receipt["action_id"])

    assert outcome["ok"] is False
    assert outcome["error"] == result["error"]
    assert ap.get(store, receipt["action_id"]).status == "failed"


@pytest.mark.parametrize(
    "result", [{"ok": True}, {"sent": True, "id": "x"}, {"ok": True, "error": None}, "done"]
)
def test_a_result_without_a_failure_is_a_success(result):
    assert ap.reported_failure(result) is None


def test_a_free_action_that_failed_is_recorded_as_failed(store):
    """The ledger said "done" for everything that did not need asking, so
    the daily cap on calls to the user counted calls that were refused."""
    from core.consent import classify

    consent = classify("add_task", {"title": "x"})
    refused = ap.record_free(store, tool="add_task", args={"title": "x"}, consent=consent,
                             result={"ok": False, "error": "Google Tasks is not connected"})
    placed = ap.record_free(store, tool="add_task", args={"title": "y"}, consent=consent,
                            result={"ok": True, "id": "t1"})

    assert ap.get(store, refused).status == "failed"
    assert ap.get(store, placed).status == "done"


async def test_a_card_goes_to_the_outbox_once_naming_the_action_it_asks_about(
        store, monkeypatch):
    """A card Signal did not take used to sit pending, unseen, until it
    expired; then it was asked twice, ten seconds apart, and never again.
    The outbox now retries it, and stops once it is answered
    (tests/unit/test_outbox.py), which it can only do knowing the action."""
    asked: list[tuple[str, dict]] = []

    async def notify(text, **kw):
        asked.append((text, kw))
        return 1

    monkeypatch.setattr(ap, "_notify", notify)
    receipt = await _ask(store)
    await asyncio.sleep(0.05)

    [(text, kw)] = asked
    assert receipt["token"] in text
    assert kw == {"action_id": receipt["action_id"],
                  "noted": text.replace(receipt["token"], "[the card's word]")}
