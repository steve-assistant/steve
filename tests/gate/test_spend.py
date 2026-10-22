"""What Steve may spend is asked in one place, of one ledger, by the user's clock.

The caps on model spend are core's (`core/spend.py`), frozen with this test:
nothing the night may change can raise one, or make a call that the ledger
does not count against them. Before step 3 the counting was spread over
seven `spent_today`s, the night's cap was held in memory and started again
at a restart, and "today" was found with SQLite's own idea of local time.

What is pinned here:
- every cap refuses at its limit, the month's first, then its category's,
  then its part's, and says when it resets;
- a day is the user's, from their midnight, and a night runs from the start
  of quiet hours across midnight;
- a bill is put down to the outermost unit of work it was made in, follows
  that work into threads and tasks, and is written even when made in none;
- the user's choice of cap counts only inside its range, and every default
  is inside its own;
- every model client asks before each request and bills after it, once,
  even for a reply cut short; nothing else writes the ledger; and a call
  made outside a client is on a list that says what brings it under one;
- a job spends as its kind, and the night as the night;
- a cap nearly reached is written down once, and one reached once, for each
  period, whatever restarts come between, and a cap of nothing is never
  warned of;
- a request OpenAI refuses because the account's balance is spent is
  written down by the client, once a day, and a rate limit is not.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from core import clock, db, knobs, spend
from core.policy import load_policy
from tests.gate import repo_files

NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
# Twenty past eleven at night in New York: twenty past three the next
# morning in UTC, when a UTC day would already have started again.
LATE = datetime(2026, 10, 12, 23, 20, tzinfo=NEW_YORK)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "local_zone", lambda: NEW_YORK)
    store = db.open_store(tmp_path / "spend.db")
    yield store
    store.close()


def _bill(conn, cost, at: datetime, category="chat", part=None) -> int:
    """A bill as if it had been made at `at`."""
    row = spend.bill(conn, cost, model="gpt-6-luna", unit=spend.Unit(category, part))
    conn.execute("UPDATE usage_events SET created_at = ? WHERE id = ?", (clock.stamp(at), row))
    return row


def test_a_day_is_counted_from_the_users_midnight_not_utcs(conn):
    """A UTC day ends at eight in the evening in New York: counted from it, a
    day's cap would reset in the middle of the user's evening and allow twice
    what it says."""
    # Nine in the morning: the user's today, but UTC's yesterday by LATE.
    _bill(conn, 2.95, datetime(2026, 10, 12, 9, 0, tzinfo=NEW_YORK))
    assert spend.may_spend(conn, unit=spend.Unit("chat"), now=LATE) is None
    _bill(conn, 0.05, datetime(2026, 10, 12, 22, 0, tzinfo=NEW_YORK))
    refused = spend.may_spend(conn, unit=spend.Unit("chat"), now=LATE)
    assert refused is not None and refused.cap == "CHAT_DAY_USD"
    assert refused.window.resets == datetime(2026, 10, 13, 0, 0, tzinfo=NEW_YORK)
    assert refused.window.period == "2026-10-12"
    after_midnight = datetime(2026, 10, 13, 0, 5, tzinfo=NEW_YORK)
    assert spend.may_spend(conn, unit=spend.Unit("chat"), now=after_midnight) is None


def test_the_same_bills_fall_in_another_users_day_by_their_zone(conn, monkeypatch):
    """The zone is the user's, asked of `core.clock`: the same moments are
    yesterday in Tokyo and today in New York."""
    _bill(conn, 3.00, datetime(2026, 10, 12, 9, 0, tzinfo=NEW_YORK))
    assert spend.may_spend(conn, unit=spend.Unit("chat"), now=LATE) is not None
    monkeypatch.setattr(clock, "local_zone", lambda: TOKYO)
    # 9am in New York is 10pm in Tokyo; LATE is the next afternoon there.
    assert spend.may_spend(conn, unit=spend.Unit("chat"), now=LATE) is None


def test_a_night_is_one_window_across_midnight_from_the_start_of_quiet_hours(conn):
    """The night opens in the middle of quiet hours and may run on past
    midnight. A cap counted by the day would give it two days' worth."""
    quiet = {"QUIET_FROM": "22:00", "QUIET_UNTIL": "07:00"}
    knobs.write(quiet)
    _bill(conn, 1.95, datetime(2026, 10, 12, 23, 0, tzinfo=NEW_YORK), "night")
    small_hours = datetime(2026, 10, 13, 2, 0, tzinfo=NEW_YORK)
    assert spend.may_spend(conn, unit=spend.Unit("night"), now=small_hours) is None
    _bill(conn, 0.05, datetime(2026, 10, 13, 1, 0, tzinfo=NEW_YORK), "night")
    refused = spend.may_spend(conn, unit=spend.Unit("night"), now=small_hours)
    assert refused is not None and refused.cap == "NIGHT_USD"
    assert refused.window.period == "2026-10-12"
    assert refused.window.resets == datetime(2026, 10, 13, 22, 0, tzinfo=NEW_YORK)
    next_night = datetime(2026, 10, 13, 22, 30, tzinfo=NEW_YORK)
    assert spend.may_spend(conn, unit=spend.Unit("night"), now=next_night) is None


def test_the_month_cap_stops_every_category(conn):
    """One cap over everything is the user's answer to "what will this cost
    me a month": reached, nothing spends, whatever its own day allows."""
    knobs.write({"MONTH_USD": 5})
    for day in range(1, 6):
        _bill(conn, 1.00, datetime(2026, 10, day, 12, 0, tzinfo=NEW_YORK), "background")
    for category in spend.CATEGORIES:
        refused = spend.may_spend(conn, unit=spend.Unit(category), now=LATE)
        assert refused is not None and refused.cap == "MONTH_USD", category
        assert refused.window.resets == datetime(2026, 11, 1, 0, 0, tzinfo=NEW_YORK)
    november = datetime(2026, 11, 1, 9, 0, tzinfo=NEW_YORK)
    assert spend.may_spend(conn, unit=spend.Unit("chat"), now=november) is None


def test_a_part_is_held_to_its_own_budget_inside_its_category(conn):
    """Background work's four day budgets stay: a day of routines cannot
    spend what triage needs."""
    _bill(conn, 1.00, datetime(2026, 10, 12, 8, 0, tzinfo=NEW_YORK), "background", "routines")
    refused = spend.may_spend(conn, unit=spend.Unit("background", "routines"), now=LATE)
    assert refused is not None and refused.cap == "ROUTINES_DAY_USD"
    assert spend.may_spend(conn, unit=spend.Unit("background", "triage"), now=LATE) is None
    assert spend.may_spend(conn, unit=spend.Unit("background"), now=LATE) is None


def test_each_category_is_held_to_its_own_cap_and_no_other(conn):
    """A day's chat does not use up background work's money, nor the reverse."""
    _bill(conn, 2.50, datetime(2026, 10, 12, 8, 0, tzinfo=NEW_YORK), "background")
    refused = spend.may_spend(conn, unit=spend.Unit("background"), now=LATE)
    assert refused is not None and refused.cap == "BACKGROUND_DAY_USD"
    assert spend.may_spend(conn, unit=spend.Unit("chat"), now=LATE) is None
    assert spend.may_spend(conn, unit=spend.Unit("extensions"), now=LATE) is None


def test_an_allowance_lifts_its_own_work_past_its_category_cap_and_never_the_month_s(conn):
    """A module build the user allowed in the app may pass the day's
    `extensions` cap by what they allowed (agent/modbuild.py). Only that
    build: the rest of the category is still stopped, and no allowance
    reaches past the month's cap, the user's answer to "what will this
    cost me"."""
    knobs.write({"EXTENSIONS_DAY_USD": 1, "MONTH_USD": 5})
    _bill(conn, 1.00, datetime(2026, 10, 12, 8, 0, tzinfo=NEW_YORK), "extensions")
    allowed = spend.Unit("extensions", allowance=0.50)

    assert spend.left(conn, "extensions", now=LATE) == 0.0
    refused = spend.may_spend(conn, unit=spend.Unit("extensions"), now=LATE)
    assert refused is not None and refused.cap == "EXTENSIONS_DAY_USD"
    assert spend.may_spend(conn, unit=allowed, now=LATE) is None
    _bill(conn, 0.50, datetime(2026, 10, 12, 9, 0, tzinfo=NEW_YORK), "extensions")
    refused = spend.may_spend(conn, unit=allowed, now=LATE)
    assert refused is not None and refused.cap == "EXTENSIONS_DAY_USD"
    _bill(conn, 3.50, datetime(2026, 10, 2, 9, 0, tzinfo=NEW_YORK), "background")
    refused = spend.may_spend(conn, unit=spend.Unit("extensions", allowance=100), now=LATE)
    assert refused is not None and refused.cap == "MONTH_USD"


@pytest.mark.parametrize("cap,category", [("NIGHT_USD", "night"),
                                          ("EXTENSIONS_DAY_USD", "extensions")])
def test_a_cap_of_nothing_allows_nothing(conn, cap, category):
    """Zero turns that spending off, before the first call."""
    knobs.write({cap: 0})
    refused = spend.may_spend(conn, unit=spend.Unit(category), now=LATE)
    assert refused is not None and refused.cap == cap and refused.spent == 0


def test_a_cap_the_user_sets_counts_and_one_out_of_its_range_does_not(conn):
    """The user moves a cap only inside its range: a file edited to allow
    $500 a day is read as the default."""
    knobs.write({"CHAT_DAY_USD": 5})
    _bill(conn, 4.00, datetime(2026, 10, 12, 8, 0, tzinfo=NEW_YORK))
    assert spend.may_spend(conn, unit=spend.Unit("chat"), now=LATE) is None
    knobs.knobs_path().write_text('{"CHAT_DAY_USD": 500}', encoding="utf-8", newline="\n")
    refused = spend.may_spend(conn, unit=spend.Unit("chat"), now=LATE)
    assert refused is not None and refused.limit == spend.DEFAULTS["CHAT_DAY_USD"]


def test_a_card_changes_only_the_five_caps_and_only_inside_their_ranges():
    """`set_cap` is the one knob write a tool's card reaches. Any other
    knob -- quiet hours, who may approve, a part's budget -- or a sum the
    Spend page would refuse is refused, and nothing is written."""
    assert set(spend.SETTABLE) == {"MONTH_USD", *spend.CAPS.values()}
    assert spend.set_cap("MONTH_USD", 25) == 25.0
    assert spend.limits()["MONTH_USD"] == 25.0
    before = knobs.knobs_path().read_text(encoding="utf-8")

    others = [name for name in knobs.KNOBS if name not in spend.SETTABLE]
    assert others
    for name, usd in [*((name, 1) for name in others), ("MONTH_USD", 500), ("MONTH_USD", 1),
                      ("CHAT_DAY_USD", -1), ("NIGHT_USD", 3)]:
        with pytest.raises(knobs.BadKnob):
            spend.set_cap(name, usd)

    assert knobs.knobs_path().read_text(encoding="utf-8") == before


def test_every_default_is_inside_its_range_and_the_night_inside_the_policy():
    """A default outside its knob's range would be a cap the user could
    never set back; the night's may not exceed what the frozen policy lets
    the night spend."""
    for name, default in spend.DEFAULTS.items():
        knob = knobs.KNOBS[name]
        assert knob.kind == "usd" and knob.lo <= default <= knob.hi, name
    assert knobs.KNOBS["NIGHT_USD"].hi <= load_policy().coder.night_budget_usd
    parts = sum(spend.DEFAULTS[cap] for cap in spend.PARTS.values())
    assert spend.DEFAULTS["BACKGROUND_DAY_USD"] >= parts
    assert set(spend.CAPS) == set(spend.CATEGORIES)


def test_the_outermost_unit_decides_the_category_and_the_innermost_the_rest(conn):
    """The night's research is the night's, whatever code it runs: a unit
    opened inside another names its part and ids, not its category."""
    session = conn.execute("INSERT INTO sessions (channel) VALUES ('night')").lastrowid
    with spend.spending("night", session_id=session), \
            spend.spending("background", part="routines", job_id=3):
        assert spend.current() == spend.Unit("night", "routines", session_id=session, job_id=3)
        row = spend.bill(conn, 0.01, model="gpt-6-luna")
    got = conn.execute("SELECT category, part, session_id, job_id FROM usage_events"
                       " WHERE id = ?", (row,)).fetchone()
    assert tuple(got) == ("night", "routines", session, 3)
    assert spend.current() is None


def test_the_unit_goes_with_the_work_into_threads_and_tasks():
    """Blocking work runs in `asyncio.to_thread` and workers in tasks of
    their own: a unit left behind there would bill them as nobody's."""
    async def look():
        return spend.current()

    async def run():
        with spend.spending("chat", session_id=1):
            in_thread = await asyncio.to_thread(spend.current)
            in_task = await asyncio.create_task(look())
        return in_thread, in_task

    in_thread, in_task = asyncio.run(run())
    assert in_thread == spend.Unit("chat", session_id=1)
    assert in_task == spend.Unit("chat", session_id=1)


def test_a_bill_made_in_no_unit_is_still_written_and_said(conn, caplog):
    """A call outside every unit is exactly the kind that went unseen: it is
    counted as background work's, marked, and logged."""
    with caplog.at_level(logging.WARNING, logger="spend"):
        row = spend.bill(conn, 0.02, model="gpt-6-luna")
    got = conn.execute("SELECT category, part, cost_usd FROM usage_events WHERE id = ?",
                       (row,)).fetchone()
    assert tuple(got) == ("background", spend.UNATTRIBUTED, 0.02)
    assert "no unit of work" in caplog.text


def test_a_cap_is_warned_of_once_at_four_fifths_and_stopped_once_across_restarts(
        conn, tmp_path):
    """In the database, once per cap and period: the sets in memory that
    kept "told once a day" were empty again after every restart, and the
    user was told again."""
    background = spend.Unit("background")
    spend.bill(conn, 1.90, model="m", unit=background)
    assert spend.notices(conn) == []
    spend.bill(conn, 0.10, model="m", unit=background)  # $2.00 of $2.50
    spend.bill(conn, 0.30, model="m", unit=background)
    again = db.open_store(tmp_path / "spend.db")
    try:
        spend.bill(again, 0.30, model="m", unit=background)
        for store in (conn, again, again):
            assert spend.may_spend(store, unit=background) is not None
    finally:
        again.close()

    found = spend.notices(conn)
    assert [(n.kind, n.cap) for n in found] == [("warned", "BACKGROUND_DAY_USD"),
                                                ("stopped", "BACKGROUND_DAY_USD")]
    assert found[0].spent == pytest.approx(2.00) and found[0].limit == 2.50
    assert found[1].spent == pytest.approx(2.60)
    spend.told(conn, found)
    assert spend.notices(conn, untold=True) == []


def test_a_cap_of_nothing_is_refused_but_never_warned_of(conn):
    """Zero turns that spending off; a warning that it is "at 100% of $0"
    would be noise about a choice the user made."""
    knobs.write({"EXTENSIONS_DAY_USD": 0})
    spend.bill(conn, 0.01, model="m", unit=spend.Unit("extensions"))
    assert spend.notices(conn) == []
    assert spend.may_spend(conn, unit=spend.Unit("extensions")) is not None
    assert [(n.kind, n.cap) for n in spend.notices(conn)] == [("stopped", "EXTENSIONS_DAY_USD")]


def test_a_negative_bill_buys_no_headroom(conn):
    """A bill below nothing would lower the total under every cap."""
    row = _bill(conn, -5.0, datetime(2026, 10, 12, 8, 0, tzinfo=NEW_YORK))
    assert conn.execute("SELECT cost_usd FROM usage_events WHERE id = ?", (row,)).fetchone()[0] == 0


def test_the_ledger_refuses_a_category_that_is_not_one(conn):
    """The column is checked as well, so a bill cannot be filed where no cap
    counts it."""
    with pytest.raises(ValueError), spend.spending("misc"):
        pass
    with pytest.raises(ValueError), spend.spending("background", part="anything"):
        pass
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO usage_events (model, category) VALUES ('m', 'misc')")


def test_windows_name_their_periods_by_the_users_calendar(monkeypatch):
    """A notice is told once per period, so the period must be the user's:
    the day and month by their midnight, the night by the evening it began."""
    monkeypatch.setattr(clock, "local_zone", lambda: NEW_YORK)
    span = spend.windows(LATE, quiet_from=time(22, 0))
    assert span.day.since == "2026-10-12 04:00:00"
    assert span.month.since == "2026-10-01 04:00:00" and span.month.period == "2026-10"
    assert span.night.since == "2026-10-13 02:00:00" and span.night.period == "2026-10-12"


# ---------------------------------------------------------------------------
# Every call billed where every call passes (step 3, item 3)
# ---------------------------------------------------------------------------

# Where Steve's own code is; tests build fakes.
SCANNED = ("agent/", "core/", "tools/", "voice/", "devtools/")
PRICE = (1.0, 0.1, 4.0)


def _sources():
    for rel in repo_files.python(*SCANNED):
        yield rel, repo_files.text(rel) or ""


def _answering(calls: list | None = None, *, tool: bool = False) -> FunctionModel:
    """A model that answers, after one tool call if `tool`; `calls` counts
    the requests that reached it."""
    def respond(messages, info: AgentInfo) -> ModelResponse:
        if calls is not None:
            calls.append(1)
        if tool and len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("look", {})])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(respond)


def _agent(model) -> Agent:
    agent = Agent(model)

    @agent.tool_plain
    def look() -> str:
        return "seen"

    return agent


def test_a_request_past_a_cap_is_refused_before_anything_is_sent(conn):
    """Asked after the request, a cap would let through the call that breaks
    it, and every call after that until someone looked."""
    with conn:
        spend.bill(conn, 30.0, model="gpt-6-luna", unit=spend.Unit("chat"))
    calls: list = []
    guarded = spend.Guarded(_answering(calls), slug="m", price=PRICE)

    async def run():
        with spend.spending("chat", conn=conn):
            await _agent(guarded).run("hello")

    with pytest.raises(spend.Refused) as refused:
        asyncio.run(run())
    assert refused.value.cap == "MONTH_USD"
    assert calls == []


def test_each_request_is_billed_once_to_the_unit_it_was_made_in(conn):
    """One row per request, the tool round's included: billing the turn
    once, at its end, lost every request a retry threw away."""
    with conn:
        session = conn.execute("INSERT INTO sessions (channel) VALUES ('app')").lastrowid
        job = conn.execute("INSERT INTO jobs (kind, prompt) VALUES ('research', 'x')").lastrowid
    guarded = spend.Guarded(_answering(tool=True), slug="gpt-6-luna", price=PRICE, effort="low")

    async def run():
        with spend.spending("background", part="work", conn=conn, session_id=session,
                            job_id=job):
            return await _agent(guarded).run("look it up")

    result = asyncio.run(run())
    rows = conn.execute("SELECT category, part, session_id, job_id, model, effort, cost_usd"
                        " FROM usage_events").fetchall()
    assert [tuple(r)[:6] for r in rows] == [
        ("background", "work", session, job, "gpt-6-luna", "low")] * 2
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(guarded.cost(result.usage))
    assert all(r["cost_usd"] > 0 for r in rows)


def test_a_reply_cut_short_while_streaming_is_billed_all_the_same(conn):
    """The user who stops a reply has still paid for it."""
    async def stream(messages, info: AgentInfo):
        for word in ("one ", "two ", "three"):
            yield word

    guarded = spend.Guarded(FunctionModel(stream_function=stream), slug="m", price=PRICE)

    async def run():
        with spend.spending("chat", conn=conn):
            async with Agent(guarded).run_stream("count") as result:
                async for _ in result.stream_text(delta=True):
                    break

    asyncio.run(run())
    assert conn.execute("SELECT category, COUNT(*) FROM usage_events").fetchone()[:] == (
        "chat", 1)


def test_an_answer_whose_bill_cannot_be_written_is_still_given(conn, monkeypatch, caplog):
    """It was paid for. The lost bill is logged, where a missed row can be
    found, rather than turning a good answer into an error."""
    def broken(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(spend, "_write", broken)
    guarded = spend.Guarded(_answering(), slug="m", price=PRICE)

    async def run():
        with spend.spending("chat", conn=conn):
            return await _agent(guarded).run("hello")

    with caplog.at_level(logging.ERROR, logger="spend"):
        assert asyncio.run(run()).output == "done"
    assert "could not bill" in caplog.text


def _refused_with(code: str, *, stream: bool = False) -> FunctionModel:
    """A model OpenAI answers with a 429 carrying `code`, as it says both a
    rate limit and a spent balance."""
    error = ModelHTTPError(429, "gpt-6-luna", {"message": "no", "type": code, "code": code})

    def respond(messages, info: AgentInfo) -> ModelResponse:
        raise error

    async def respond_in_parts(messages, info: AgentInfo):
        raise error
        yield ""  # an async generator, as a stream function is

    return FunctionModel(stream_function=respond_in_parts) if stream else FunctionModel(respond)


def _ask(conn, guarded, *, stream: bool) -> None:
    async def ask():
        with spend.spending("background", conn=conn):
            if stream:
                async with Agent(guarded).run_stream("hello") as result:
                    await result.get_output()
            else:
                await Agent(guarded).run("hello")

    asyncio.run(ask())


@pytest.mark.parametrize("stream", [False, True], ids=["request", "stream"])
def test_a_request_refused_for_a_spent_balance_is_written_down_once_a_day(conn, stream):
    """Every client asks OpenAI, so with its balance spent nothing that
    needs a model can run, the briefing's writer included. Written down
    where it happens, it can be said with no model (agent/caps.py); once a
    day, however many calls are refused."""
    guarded = spend.Guarded(_refused_with("insufficient_quota", stream=stream), slug="m",
                            price=PRICE)

    for _ in range(2):
        with pytest.raises(ModelHTTPError):
            _ask(conn, guarded, stream=stream)

    [notice] = spend.notices(conn, caps=[spend.BALANCE])
    assert (notice.kind, notice.period) == ("stopped", spend.windows().day.period)
    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0


def test_a_rate_limit_is_not_taken_for_a_spent_balance(conn):
    """The same status, and it clears in seconds: telling the user to top
    up would send them to pay for nothing."""
    guarded = spend.Guarded(_refused_with("rate_limit_exceeded"), slug="m", price=PRICE)

    with pytest.raises(ModelHTTPError):
        _ask(conn, guarded, stream=False)

    assert spend.notices(conn, caps=[spend.BALANCE]) == []


def test_a_spent_balance_is_known_from_the_openai_sdks_own_error_too():
    """The calls made without pydantic-ai -- web search, the embedder, the
    screen's extract -- get the `openai` SDK's error, which keeps the code
    as an attribute, not in a body."""
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")

    def refused(code: str) -> openai.RateLimitError:
        return openai.RateLimitError("no", response=httpx.Response(429, request=request),
                                     body={"message": "no", "type": code, "code": code})

    assert spend.out_of_credit(refused("insufficient_quota"))
    assert not spend.out_of_credit(refused("rate_limit_exceeded"))
    assert not spend.out_of_credit(ValueError("insufficient_quota"))


# What writes the ledger. Only `bill`: a second writer is a second set of
# rules about what a bill is, which is how seven counters came about.
WRITES_LEDGER = re.compile(r"(?i)\b(?:insert(?:\s+or\s+\w+)?\s+into|replace\s+into|update"
                           r"|delete\s+from)\s+usage_events\b")


def test_nothing_but_core_spend_writes_the_ledger():
    writers = sorted(name for name, text in _sources() if WRITES_LEDGER.search(text))
    assert writers == ["core/spend.py"]


# pydantic-ai's model classes, built anywhere in Steve's code.
MODEL_CLASSES = re.compile(r"^(?:OpenAI\w*Model|\w+Model)$")
GUARDS = {"guard", "guarded", "Guarded"}


def _name(node: ast.AST) -> str:
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def test_every_model_client_steve_builds_is_guarded():
    """A client built bare would spend past every cap and bill nothing. Each
    construction of a pydantic-ai model sits directly inside a guard, and no
    agent is given a model by name, which pydantic-ai would build bare."""
    from pydantic_ai import models

    known = {name for name in dir(models) if name.endswith("Model")}
    bare, by_name = [], []
    for name in repo_files.python(*SCANNED):
        tree = repo_files.parsed(name)
        guarded = {id(arg) for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and _name(node.func) in GUARDS for arg in node.args[:1]}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = _name(node.func)
            if (called.startswith("OpenAI") and called.endswith("Model")
                    or called in known - {"Model"}) and id(node) not in guarded:
                bare.append(f"{name}:{node.lineno} {called}")
            if called == "Agent" and node.args and isinstance(node.args[0], ast.Constant):
                by_name.append(f"{name}:{node.lineno}")
    assert bare == [] and by_name == []


# Model calls made outside a pydantic-ai client, by file, each with what
# brings it under the ledger. An entry that outlives its call fails.
RAW = {
    "core/screen.py": "the extract asks `may_spend` before its call and is billed"
                      " (tests/gate/test_injection_screen.py)",
    "core/decide.py": "asks `may_spend` before a call and bills each answer"
                      " (tests/gate/test_decide.py)",
    "core/websearch.py": "a search asks `may_spend` before its call and is billed, fee"
                         " included (tests/gate/test_websearch.py)",
    "core/embed.py": "asks `may_spend` before its calls and bills each one"
                     " (tests/gate/test_embed.py)",
    "core/hearing.py": "asks `may_spend` before a voice note is heard and bills each one"
                       " (tests/gate/test_hearing.py)",
    "core/voicing.py": "asks `may_spend` before a line is said and bills each one"
                       " (tests/gate/test_voicing.py)",
    "voice/live.py": "the desk asks `may_spend` before a live conversation opens and bills"
                     " its minutes as they pass (tests/unit/test_desk_call.py)",
}
# An address of OpenAI's is built from core/endpoints.py now, never written
# out (tests/gate/test_setup_bounds.py), so a raw call by address is found
# by what it adds to that.
RAW_CALL = re.compile(r"openai\.(?:Async)?OpenAI\(|/decisions\"|\.embeddings\.create\("
                      r"|endpoints\.openai(?:_socket)?\(\)\}/(?:responses|chat|embeddings"
                      r"|audio|live)")


def test_model_calls_outside_a_client_are_only_the_listed_ones():
    found = sorted(name for name, text in _sources() if RAW_CALL.search(text))
    assert found == sorted(RAW)


def test_a_job_spends_as_its_kind_and_is_named_on_its_bills(conn):
    """The job runner starts at boot, in no unit: without its own, a goal's
    day budget would never see its runs."""
    from agent import jobs

    seen: dict[str, spend.Unit | None] = {}

    async def handler(job):
        seen[job.kind] = spend.current()
        return "ok"

    kinds = ("routine", "goal", "research")
    worker = jobs.JobWorker(conn, {kind: handler for kind in kinds})
    ids = {kind: jobs.enqueue(conn, kind=kind, prompt="1") for kind in kinds}

    async def run():
        while (job := jobs.claim(conn)) is not None:
            await worker._run_one(job)

    asyncio.run(run())
    assert {kind: (unit.category, unit.part, unit.job_id) for kind, unit in seen.items()} == {
        "routine": ("background", "routines", ids["routine"]),
        "goal": ("background", "goals", ids["goal"]),
        "research": ("background", None, ids["research"]),
    }


def test_the_nights_calls_are_the_nights_and_the_yardstick_is_billed_once(conn, monkeypatch):
    """Its calls through `ask_once` reached no ledger before step 3. The
    yardstick's child bills into scratch databases of its own, so the
    night bills what it reports, here."""
    import agent.night as night
    from agent.night import rehearsal, skilling, yardstick

    seen: list = []

    async def learn(conn_, budget, report):
        seen.append(spend.current())

    async def nothing(*args, **kwargs):
        return 0.0

    async def measured(report):
        return 0.07

    monkeypatch.setattr(night, "learn", learn)
    monkeypatch.setattr(skilling, "distill", nothing)
    monkeypatch.setattr(night, "scout", nothing)
    monkeypatch.setattr(yardstick, "measure", measured)
    monkeypatch.setattr(rehearsal, "rehearse", nothing)

    report = asyncio.run(night.run_night(conn, do_research=False, do_improve=False))

    assert report.stopped_because == "finished"
    assert [unit.category for unit in seen] == ["night"]
    rows = conn.execute("SELECT category, model, cost_usd FROM usage_events").fetchall()
    assert [tuple(r) for r in rows] == [("night", "yardstick", 0.07)]
