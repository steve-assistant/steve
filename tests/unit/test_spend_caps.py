"""What the user sees when a spending cap is reached, and just before.

`core/spend.py` decides what may be spent; `agent/caps.py` says so, once,
where the user will see it. Before step 3 nothing capped chat at all; the
attention loop's budget ran out with no word, so the user saw only that it
had gone quiet; the routines' and goals' said so by message from a set held
in memory, so every restart said it again; and the night's $2 was held in
memory too, and started afresh after a swap.

What is pinned here:
- a conversation past its cap is answered in code, with no model call,
  saying which cap, when it resets and where to raise it; one that reaches
  it partway says it stopped partway; one that passes four fifths of it
  says so once, after its answer;
- work no one is present for stops instead, and quietly: a job a cap
  stopped is neither tried again nor announced;
- the month's cap, which stops everything, is a message of its own, held
  through quiet hours, and sent once;
- "RAISE 15" raises it from where the model cannot be asked: read in code,
  it makes the raise's card, which takes the user's code as a purchase
  does; a card's yes changes the cap only from the sum the card named,
  and the model's own tools (agent/toolset/spending.py) only queue it;
- so is OpenAI's balance running out, which stops everything too: said in
  the reply where the user is present, not taken for a broken turn, and
  shown in the app with where to top up.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import approvals, caps, jobs, loop, provider, toolset
from agent.loop import Conversation
from agent.models import MODELS
from agent.turn import Deps
from core import call_policy, clock, consent, db, knobs, spend

NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
def talk(conn, monkeypatch, memories_repo):
    """A conversation whose model is billed as a real one is, by its guard,
    and follows a script a step a call: a callable step runs first (another
    turn spending meanwhile), "call" asks for a tool that is not there, a
    (name, arguments) pair for one that is, anything else is the reply."""
    state = {"script": [], "calls": 0}

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        state["calls"] += 1
        step = state["script"].pop(0)
        while callable(step):
            step()
            step = state["script"].pop(0)
        if step == "call":
            return ModelResponse(parts=[ToolCallPart("no_such_tool", {})])
        if isinstance(step, tuple):
            return ModelResponse(parts=[ToolCallPart(*step)])
        return ModelResponse(parts=[TextPart(step)])

    def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
        from pydantic_ai import Agent

        agent = Agent(provider.guard(FunctionModel(respond), MODELS[role]), deps_type=Deps,
                      system_prompt="test")
        toolset.register_tools(agent, allow=allow)
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)
    return state


def _chat_spent(conn, usd: float) -> None:
    spend.bill(conn, usd, model="m", unit=spend.Unit("chat"))


@pytest.mark.parametrize("channel", ["signal", "telegram"])
async def test_a_conversation_past_its_cap_is_answered_with_no_model_call(conn, talk, channel):
    """The reply is the only place a user in a conversation hears of it, and
    the model it would have asked is what the cap stops, on either phone."""
    _chat_spent(conn, 3.00)

    reply = await Conversation(conn, channel=channel).send("what's on tomorrow?")

    assert talk["calls"] == 0
    assert reply.text == ("I've reached today's $3.00 limit for chat. It resets at midnight,"
                          " or you can raise it under Autonomy → Spending caps.")
    assert reply.cost_usd == 0
    # Said in the reply: no one says it again.
    assert spend.notices(conn, caps=["CHAT_DAY_USD"], untold=True) == []


async def test_a_turn_that_reaches_the_cap_partway_stops_and_says_so(conn, talk):
    """The request in flight finishes and is billed; the next is not made."""
    talk["script"] = [lambda: _chat_spent(conn, 3.00), "call", "never asked"]

    reply = await Conversation(conn, channel="signal").send("plan my week")

    assert talk["calls"] == 1
    assert reply.text.startswith("I stopped partway: I've reached today's $3.00 limit for chat.")
    assert reply.cost_usd > 0


async def test_a_turn_past_four_fifths_of_the_cap_says_so_once_after_its_answer(conn, talk):
    talk["script"] = [lambda: _chat_spent(conn, 2.45), "Your week is clear.", "Noted."]
    convo = Conversation(conn, channel="signal")

    first = await convo.send("anything this week?")
    second = await convo.send("thanks")

    assert first.text.startswith("Your week is clear.")
    assert first.text.endswith("(Spending on chat is at $2.45 of today's $3.00 limit.)")
    assert second.text == "Noted."


async def test_work_no_one_is_present_for_is_stopped_not_answered(conn, talk):
    """A routine's turn raises, so the routine stops and the briefing says
    why; a reply about the cap would be a message nobody asked for."""
    spend.bill(conn, 2.50, model="m", unit=spend.Unit("background"))

    with pytest.raises(spend.Refused) as refused:
        await Conversation(conn, channel="routine").send("check the ticket page")

    assert refused.value.cap == "BACKGROUND_DAY_USD" and talk["calls"] == 0


async def test_a_job_a_cap_stopped_is_not_tried_again_or_announced(conn):
    """Tried again it is refused again; announced, it reads as a failure the
    user caused. The next briefing says the cap was reached."""
    spend.bill(conn, 2.50, model="m", unit=spend.Unit("background"))
    job_id = jobs.enqueue(conn, kind="research", prompt="the lease terms")
    finished = []

    async def handler(job):
        if refusal := spend.may_spend(conn):
            raise refusal
        return "done"

    worker = jobs.JobWorker(conn, {"research": handler}, on_finish=finished.append)
    await worker._run_one(jobs.get(conn, job_id))

    job = jobs.get(conn, job_id)
    assert job.status == "failed" and "BACKGROUND_DAY_USD" in (job.error or "")
    assert finished == [] and jobs.pending_notifications(conn) == []


async def test_the_months_cap_is_a_message_held_through_quiet_hours_and_sent_once(conn):
    knobs.write({"MONTH_USD": 5})
    spend.bill(conn, 5.00, model="m", unit=spend.Unit("background"))
    assert spend.may_spend(conn, unit=spend.Unit("chat"))
    sent = []

    async def notify(text):
        sent.append(text)
        return True

    night = dt.datetime.combine(dt.date(2026, 10, 12), call_policy.QUIET_FROM)
    morning = dt.datetime.combine(dt.date(2026, 10, 13), call_policy.QUIET_UNTIL)

    assert await caps.by_message(conn, notify, now=night) == 0 and sent == []
    # The warning, the ask and the stop, said as the stop: one message.
    assert await caps.by_message(conn, notify, now=morning) == 3
    assert await caps.by_message(conn, notify, now=morning) == 0

    [text] = sent
    assert text.startswith("I've reached this month's $5.00 limit, so everything is paused,"
                           " chat included. Reply RAISE 15 to raise it to $15.00, or raise it"
                           " under Autonomy → Spending caps. It resets on 1 ")


@pytest.mark.parametrize("channel", ["tray", "signal", "telegram"])
async def test_raise_at_the_months_cap_makes_the_high_risk_card_with_no_model_call(
        conn, talk, channel):
    """At the month's cap the model is what is stopped, so "raise it to 15"
    could only be answered with the refusal. Read in code, "RAISE 15" makes
    the raise's card, which takes the user's code as a purchase does, and
    one such card waits at a time."""
    knobs.write({"MONTH_USD": 5})
    spend.bill(conn, 5.00, model="m", unit=spend.Unit("background"))
    convo = Conversation(conn, channel=channel)

    refused = await convo.send("what's on tomorrow?")
    asked = await convo.send("RAISE 15")
    again = await convo.send("raise $20")
    await approvals.asked()

    assert talk["calls"] == 0
    assert refused.text.startswith("I've reached this month's $5.00 limit. Reply RAISE 15 to"
                                   " raise it to $15.00, or raise it under Autonomy → Spending"
                                   " caps. It resets on 1 ")
    assert asked.text == ("I've asked for your yes: raise the month's spending cap from $5.00"
                          " to $15.00.")
    assert asked.cost_usd == 0
    assert again.text.startswith("Another card is waiting for your yes and your code.")
    [card] = approvals.pending(conn)
    assert card.tool == caps.RAISE and consent.high_risk(card.tool)
    assert card.args == {"cap": "MONTH_USD", "from_usd": 5.0, "to_usd": 15.0}
    assert spend.limits()["MONTH_USD"] == 5.0


async def test_the_model_changes_a_cap_only_by_a_card_naming_it_as_it_is(conn, talk):
    """Asked in chat, below the cap, the model calls the tool; the tool
    queues the card and changes nothing, and a sum out of range is refused
    before any card."""
    talk["script"] = [("raise_spending_cap", {"cap": "month", "to_usd": 25}), "Asked.",
                      ("lower_spending_cap", {"cap": "chat", "to_usd": 500}), "Can't."]
    convo = Conversation(conn, channel="tray")

    await convo.send("raise my monthly cap to 25")
    await convo.send("set chat to 500 a day")
    await approvals.asked()

    [card] = approvals.pending(conn)
    assert card.tool == caps.RAISE
    assert card.preview == "Raise the month's spending cap from $15.00 to $25.00"
    assert spend.limits()["MONTH_USD"] == 15.0 and spend.limits()["CHAT_DAY_USD"] == 3.0


async def test_a_raise_out_of_its_range_or_not_higher_says_why_and_asks_nothing(conn, talk):
    convo = Conversation(conn, channel="tray")

    too_much = await convo.send("RAISE 500")
    not_higher = await convo.send("RAISE 15")

    assert too_much.text == "The month's spending cap can be from $5.00 to $200.00."
    assert not_higher.text == ("The month's spending cap is $15.00 already; a raise takes it"
                               " higher.")
    assert approvals.pending(conn) == [] and talk["calls"] == 0


@pytest.mark.parametrize("text, usd", [
    ("RAISE 25", 25.0), ("raise $25.50.", 25.5), ("  Raise 30 ", 30.0),
    ("raise 25 for the gift", None), ("can you raise 25", None), ("RAISE", None)])
def test_raise_is_read_in_code_only_when_it_is_the_whole_message(text, usd):
    """Anything more is a request for the model, which may be about
    something else entirely."""
    assert caps.raised_by_word(text) == usd


def test_the_top_of_the_months_range_offers_no_raise():
    knobs.write({"MONTH_USD": 200})
    refusal = spend.Refused("MONTH_USD", 200.0, 200.0, spend.windows().month)

    assert caps.suggested(200.0) is None
    assert "RAISE" not in caps.refused(refusal)


def test_a_card_names_the_cap_as_it_is_and_a_yes_after_it_moved_changes_nothing():
    """The card said "from $3.00"; a yes once the Spend page made it $4.00
    would be a yes to a change the user never saw."""
    asked = caps.change_asked(caps.LOWER, "chat", 1)
    assert asked == {"args": {"cap": "CHAT_DAY_USD", "from_usd": 3.0, "to_usd": 1.0},
                     "preview": "Lower the daily cap for chat from $3.00 to $1.00"}

    knobs.write({"CHAT_DAY_USD": 4})
    moved = caps.change(**asked["args"])
    assert not moved["ok"] and "is $4.00 now, not the $3.00 the card said" in moved["error"]
    assert spend.limits()["CHAT_DAY_USD"] == 4.0

    again = caps.change_asked(caps.LOWER, "chat", 1)
    assert caps.change(**again["args"]) == {"ok": True, "cap": "CHAT_DAY_USD",
                                            "from_usd": 4.0, "to_usd": 1.0}
    assert spend.limits()["CHAT_DAY_USD"] == 1.0


def test_a_change_to_no_such_cap_or_the_wrong_way_is_refused_before_any_card():
    assert caps.change_asked(caps.RAISE, "lunch", 5) == {
        "error": "there is no 'lunch' cap; the caps are month, chat, background, night,"
                 " extensions"}
    assert caps.change_asked(caps.LOWER, "month", 20) == {
        "error": "the month's spending cap is $15.00; a lowering takes it lower"}
    assert caps.change_asked(caps.RAISE, "night", 3) == {
        "error": "the nightly cap for overnight work can be from $0.00 to $2.00"}


def test_a_raise_takes_the_users_code_a_lowering_a_plain_yes_and_reading_none():
    """A raise spends money. A lowering can't, but one the user never asked
    for, from a page that fooled the model, would stop Steve for the month."""
    assert consent.high_risk(caps.RAISE) and not consent.high_risk(caps.LOWER)
    assert consent.classify(caps.LOWER, {"cap": "MONTH_USD"}).gated
    assert not consent.classify("spending_caps", {}).gated


def test_each_cap_is_read_with_what_is_spent_left_and_its_range(conn):
    _chat_spent(conn, 1.25)

    found = {c["cap"]: c for c in caps.caps_now(conn)}

    assert list(found) == ["month", "chat", "background", "night", "extensions"]
    assert found["month"] | {"resets": None} == {
        "cap": "month", "holds": "everything", "limit_usd": 15.0, "spent_usd": 1.25,
        "left_usd": 13.75, "resets": None, "range_usd": [5.0, 200.0]}
    assert (found["chat"]["spent_usd"], found["chat"]["left_usd"]) == (1.25, 1.75)
    assert found["background"]["spent_usd"] == 0


async def test_a_month_message_that_did_not_go_is_tried_again(conn):
    knobs.write({"MONTH_USD": 5})
    spend.bill(conn, 4.50, model="m", unit=spend.Unit("background"))
    morning = dt.datetime.combine(dt.date(2026, 10, 13), call_policy.QUIET_UNTIL)

    async def down(text):
        return False

    assert await caps.by_message(conn, down, now=morning) == 0
    warned, asked = spend.notices(conn, caps=["MONTH_USD"], untold=True)
    assert (warned.kind, asked.kind) == ("warned", "asked")
    assert asked.spent == pytest.approx(4.50)


def _sent_to(sent: list[str]):
    async def notify(text):
        sent.append(text)
        return True
    return notify


EVENING = dt.datetime(2026, 10, 13, 18, 0)


def _month(found: list) -> list:
    """The month's notices: a bill that nears it passes a day's cap too."""
    return [n for n in found if (n["cap"] if isinstance(n, dict) else n.cap) == "MONTH_USD"]


async def test_a_dollar_before_the_months_cap_one_message_asks_to_raise_it(conn):
    """The stop used to be the first word, with everything already paused.
    The ask comes while a reply can still reach a model, and is the only
    push: the four fifths before it goes to the next briefing."""
    spend.bill(conn, 12.50, model="m", unit=spend.Unit("background"))
    sent = []

    assert await caps.by_message(conn, _sent_to(sent), now=EVENING) == 0
    assert [n.kind for n in _month(caps.for_the_briefing(conn))] == ["warned"]
    assert caps.lines(_month(caps.for_the_briefing(conn)), EVENING)[0].endswith(
        "this month's spending was at $12.50 of its $15.00 monthly limit.")

    spend.bill(conn, 1.55, model="m", unit=spend.Unit("background"))
    assert await caps.by_message(conn, _sent_to(sent), now=EVENING) == 2
    assert await caps.by_message(conn, _sent_to(sent), now=EVENING) == 0

    assert sent == ["This month's spending is at $14.05 of $15.00. Want to raise the cap? Reply"
                    " RAISE and the new cap, like RAISE 25, or just tell me."]
    assert _month(caps.for_the_briefing(conn)) == []


async def test_the_ask_waits_out_quiet_hours(conn):
    spend.bill(conn, 14.20, model="m", unit=spend.Unit("background"))
    night = dt.datetime.combine(dt.date(2026, 10, 12), call_policy.QUIET_FROM)
    sent = []

    assert await caps.by_message(conn, _sent_to(sent), now=night) == 0 and sent == []
    # A briefing meanwhile leaves the four fifths to the ask, which says more.
    assert _month(caps.for_the_briefing(conn)) == []
    assert await caps.by_message(conn, _sent_to(sent), now=EVENING) == 2
    assert sent[0].startswith("This month's spending is at $14.20 of $15.00.")


async def test_no_ask_when_the_user_chose_none(conn):
    knobs.write({"MONTH_ASK": "no"})
    spend.bill(conn, 14.20, model="m", unit=spend.Unit("background"))
    sent = []

    assert await caps.by_message(conn, _sent_to(sent), now=EVENING) == 0 and sent == []
    assert [n.kind for n in spend.notices(conn, caps=["MONTH_USD"])] == ["warned"]


async def test_a_raised_cap_is_asked_about_and_stopped_at_again_and_the_old_stop_is_gone(conn):
    """Notices were one per cap and period, so a cap raised in the month was
    never asked about or stopped at again, and the banner kept saying the
    old cap was reached."""
    spend.bill(conn, 15.00, model="m", unit=spend.Unit("background"))
    assert spend.may_spend(conn, unit=spend.Unit("chat"))
    sent = []
    await caps.by_message(conn, _sent_to(sent), now=EVENING)
    assert [n["kind"] for n in _month(caps.standing(conn))] == ["stopped"]

    spend.set_cap("MONTH_USD", 25)

    assert _month(caps.standing(conn)) == []
    spend.bill(conn, 9.20, model="m", unit=spend.Unit("background"))
    assert await caps.by_message(conn, _sent_to(sent), now=EVENING) == 2  # with its warning
    assert sent[-1].startswith("This month's spending is at $24.20 of $25.00.")
    spend.bill(conn, 0.80, model="m", unit=spend.Unit("background"))
    assert spend.may_spend(conn, unit=spend.Unit("chat"))
    assert await caps.by_message(conn, _sent_to(sent), now=EVENING) == 1
    assert sent[-1].startswith("I've reached this month's $25.00 limit")
    assert [n["kind"] for n in _month(caps.standing(conn))] == ["stopped"]


async def test_a_stop_at_an_amount_since_raised_past_is_not_sent(conn):
    """A stop written in quiet hours, then the cap raised on the Spend page
    before the morning: "I've reached this month's $15.00 limit" would be
    about a cap that is not there."""
    spend.bill(conn, 15.00, model="m", unit=spend.Unit("background"))
    assert spend.may_spend(conn, unit=spend.Unit("chat"))
    spend.set_cap("MONTH_USD", 25)
    sent = []

    assert await caps.by_message(conn, _sent_to(sent), now=EVENING) == 0 and sent == []
    assert spend.notices(conn, caps=["MONTH_USD"], untold=True) == [
        n for n in spend.notices(conn, caps=["MONTH_USD"]) if n.kind == "warned"]


def test_a_database_from_before_the_ask_keeps_its_notices_and_takes_one_per_amount(
        tmp_path, monkeypatch):
    """Migration 21 makes the table again to change its CHECK and its key;
    a notice already told must stay told, or the month's stop is sent twice."""
    every = db.migrations()
    conn = db.connect(tmp_path / "before.db")
    try:
        monkeypatch.setattr(db, "migrations", lambda: every[:20])
        db.migrate(conn)
        conn.execute("INSERT INTO spend_notices (kind, cap, period, limit_usd, spent_usd, told_at)"
                     " VALUES ('stopped', 'MONTH_USD', '2026-10', 15, 15.2,"
                     " '2026-10-12 08:00:00')")
        monkeypatch.setattr(db, "migrations", lambda: every)
        db.migrate(conn)

        [kept] = spend.notices(conn)
        assert (kept.kind, kept.limit, kept.told_at) == ("stopped", 15.0, "2026-10-12 08:00:00")
        for kind, limit in [("stopped", 25), ("asked", 25), ("stopped", 25)]:
            conn.execute("INSERT OR IGNORE INTO spend_notices"
                         " (kind, cap, period, limit_usd, spent_usd)"
                         " VALUES (?, 'MONTH_USD', '2026-10', ?, 25.1)", (kind, limit))
        assert [(n.kind, n.limit) for n in spend.notices(conn)] == [
            ("stopped", 15.0), ("stopped", 25.0), ("asked", 25.0)]
    finally:
        conn.close()


def test_a_build_allowed_past_the_days_cap_stopping_is_the_same_notice(conn):
    """Notices are kept per cap amount (db/migrations/0021_asked.sql).
    Written with a build's allowance, the day's cap stopping that build and
    the rest of its category would be two notices, said twice."""
    knobs.write({"EXTENSIONS_DAY_USD": 1})
    spend.bill(conn, 1.50, model="m", unit=spend.Unit("extensions"))

    assert spend.may_spend(conn, unit=spend.Unit("extensions"))
    assert spend.may_spend(conn, unit=spend.Unit("extensions", allowance=0.50))

    [stop] = [n for n in spend.notices(conn, caps=["EXTENSIONS_DAY_USD"]) if n.kind == "stopped"]
    assert stop.limit == 1.0


def test_a_cap_passed_in_one_bill_is_said_to_be_past_its_limit(conn):
    """One bill can carry a cap from under four fifths to beyond it, and a
    part's cap can be passed while its category's stopped the work first.
    The live check of 12 October 2026 read "Routines reached $2.60 of its
    $1.00 daily limit", a share of a limit already gone by."""
    spend.bill(conn, 2.60, model="m", unit=spend.Unit("background", part="routines"))
    spend.bill(conn, 3.10, model="m", unit=spend.Unit("chat"))

    [routines] = [n for n in caps.for_the_briefing(conn) if n.cap == "ROUTINES_DAY_USD"]
    [line] = caps.lines([routines])
    assert line.endswith(", routines had spent $2.60, past its $1.00 daily limit.")
    assert caps.warning_for_the_reply(conn).endswith(
        "(Spending on chat is at $3.10, past today's $3.00 limit.)")


def _balance_spent():
    """OpenAI's answer when the account's balance is used up: the rate
    limit's status, told apart only by its code."""
    raise ModelHTTPError(429, "gpt-6-luna", {"message": "You exceeded your current quota",
                                             "type": "insufficient_quota",
                                             "code": "insufficient_quota"})


async def test_a_spent_openai_balance_is_said_in_the_reply_and_not_as_a_broken_turn(conn,
                                                                                     talk):
    """Until 12 October 2026 the user read "something broke" with OpenAI's
    error in it, and the night was handed a broken turn to mend."""
    talk["script"] = [_balance_spent]

    with pytest.raises(loop.TurnFailed) as failed:
        await Conversation(conn, channel="signal").send("what's on tomorrow?")

    assert str(failed.value) == caps.NO_CREDIT
    assert "platform.openai.com" in caps.NO_CREDIT
    assert talk["calls"] == 1  # asked once: waiting does not fill a balance
    assert conn.execute("SELECT COUNT(*) FROM issues WHERE kind = 'turn_broke'").fetchone()[0] == 0
    # Said in the reply, so no message follows it.
    [notice] = spend.notices(conn, caps=[spend.BALANCE])
    assert notice.told_at is not None


async def test_a_spent_balance_no_one_heard_of_is_a_message_after_quiet_hours_sent_once(
        conn, monkeypatch):
    """A routine refused at eleven at night stops quietly; the user hears
    why in the morning, with when, since it may have been topped up since."""
    monkeypatch.setattr(clock, "local_zone", lambda: NEW_YORK)
    night = dt.datetime(2026, 10, 12, 23, 5)
    morning = dt.datetime.combine(dt.date(2026, 10, 13), call_policy.QUIET_UNTIL)
    spend.ran_out(conn, now=night.replace(tzinfo=NEW_YORK))
    conn.execute("UPDATE spend_notices SET at = ?", (clock.stamp(night.replace(tzinfo=NEW_YORK)),))
    sent = []

    async def notify(text):
        sent.append(text)
        return True

    assert await caps.by_message(conn, notify, now=night) == 0 and sent == []
    assert await caps.by_message(conn, notify, now=morning) == 1
    assert await caps.by_message(conn, notify, now=morning) == 0
    assert sent == ["OpenAI refused a request at 11:05pm yesterday because the account's"
                    " balance is used up. Until it's topped up at platform.openai.com, under"
                    " Settings → Billing, I can't answer or do anything else that needs a"
                    " model."]


async def test_a_balance_message_that_did_not_go_is_tried_again(conn):
    spend.ran_out(conn)
    morning = dt.datetime.combine(dt.date(2026, 10, 13), call_policy.QUIET_UNTIL)

    async def down(text):
        return False

    assert await caps.by_message(conn, down, now=morning) == 0
    assert len(spend.notices(conn, caps=[spend.BALANCE], untold=True)) == 1


def test_the_app_shows_a_spent_balance_with_where_to_top_up(conn):
    """Nothing in the app can raise it, so the banner says where it is
    raised."""
    spend.ran_out(conn)

    [shown] = [s for s in caps.standing(conn) if s["cap"] == spend.BALANCE]

    assert shown["kind"] == "stopped"
    assert shown["text"].startswith("At ")
    assert shown["text"].endswith("OpenAI refused a request: the account's balance was used"
                                  " up. It is topped up at platform.openai.com, under"
                                  " Settings → Billing.")
    # Not a cap of Steve's: the briefing's cap lines leave it to the message.
    assert all(n.cap != spend.BALANCE for n in caps.for_the_briefing(conn))
