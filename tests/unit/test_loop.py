"""Unit tests for routing, escalation, and the job queue.

Deterministic: the model is scripted with FunctionModel, so these assert on the
loop's behaviour rather than on whatever a live model happened to decide. The
escalation ladder in particular had a bug that live testing hid -- it only
reached the top tier when the model happened to call the tool twice in one
turn, which looked like success.
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import actions, jobs, loop, toolset
from agent.loop import Conversation
from agent.models import Role
from agent.turn import Deps, next_tier
from core import db, knobs, language, spend


@pytest.fixture
def isolated_memories(memories_repo):
    """Point memory at a throwaway repo so tests never touch the real store."""
    return memories_repo


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_ladder_advances_one_step_at_a_time():
    """From the tier executing, not from what is pending: the escalate tool
    used to read only `escalate_to`, which is rebuilt as None on every
    replay, so it always believed it was starting from CONVERSE and could
    never advance past REASON. (That case, `next_tier(REASON, None)`, was
    once a test of its own; `None` is the default, so it was this one.)"""
    assert next_tier(Role.CONVERSE) is Role.REASON
    assert next_tier(Role.REASON) is Role.ESCALATE


def test_ladder_terminates_at_the_top():
    """There must be nowhere above the top rung to spend money."""
    assert next_tier(Role.ESCALATE) is None


def test_second_call_in_one_turn_advances_from_the_pending_tier():
    """A model that calls escalate twice in a single turn should land two
    rungs up, not re-select the same one."""
    assert next_tier(Role.CONVERSE, Role.REASON) is Role.ESCALATE


@pytest.mark.parametrize("role", [Role.BULK, Role.TRIAGE])
def test_non_conversational_roles_have_no_ladder(role):
    """Only the conversation path escalates. A background run that quietly
    retried on a pricier model without a human looking is how a cheap loop
    becomes an expensive one."""
    assert next_tier(role) is None


# ---------------------------------------------------------------------------
# Escalation through the real loop, with a scripted model
# ---------------------------------------------------------------------------


def _scripted(script: list[str]):
    """A model that calls `escalate` on the listed turns, then answers.

    `script` holds one entry per model invocation: "escalate" or "answer".
    """
    state = {"i": 0}

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        step = script[min(state["i"], len(script) - 1)]
        state["i"] += 1
        if step == "escalate":
            return ModelResponse(
                parts=[ToolCallPart("escalate", {"reason": "scripted"})]
            )
        return ModelResponse(parts=[TextPart("final answer")])

    # Guarded, as every client Steve builds is (agent/provider.guard), so
    # the turn is billed the way a live one is.
    return spend.Guarded(FunctionModel(respond), slug="scripted", price=(1.0, 0.1, 4.0))


@pytest.fixture
def scripted_conversation(store, isolated_memories, monkeypatch):
    """Build a Conversation whose model is scripted rather than live."""

    def make(script: list[str], channel: str = "test"):
        model = _scripted(script)

        def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
            from pydantic_ai import Agent

            agent = Agent(model, deps_type=Deps, system_prompt="test", retries=2)
            toolset.register_tools(agent, allow=allow)
            return agent

        monkeypatch.setattr(loop, "build_agent", build_agent)
        return Conversation(store, channel=channel)

    return make


async def test_a_photo_is_shown_on_every_hop_of_its_turn_and_on_no_turn_after(
        store, isolated_memories, monkeypatch, tmp_path):
    """Until 13 October 2026 no photo reached the model. Shown on its turn,
    and on that turn's replay on a stronger model; not carried in the kept
    history, where it would be sent and billed again on every turn after."""
    from pydantic_ai import Agent, BinaryContent
    from pydantic_ai.messages import UserPromptPart

    photo = tmp_path / "heron.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0 a heron")
    shown: list[list[bytes]] = []
    script = iter(["escalate", "answer", "answer"])

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        shown.append([item.data for message in messages for part in message.parts
                      if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
                      for item in part.content if isinstance(item, BinaryContent)])
        if next(script) == "escalate":
            return ModelResponse(parts=[ToolCallPart("escalate", {"reason": "scripted"})])
        return ModelResponse(parts=[TextPart("A heron.")])

    model = spend.Guarded(FunctionModel(respond), slug="scripted", price=(1.0, 0.1, 4.0))

    def build_agent(role, **_kw):
        agent = Agent(model, deps_type=Deps, system_prompt="test", retries=2)
        toolset.register_tools(agent)
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)
    convo = Conversation(store, channel="test")

    await convo.send("[1 photo attached]\nwhat bird", images=[photo])
    await convo.send("and where do they nest?")

    assert shown == [[photo.read_bytes()], [photo.read_bytes()], []]
    # Kept with its message, so the day shows it there (channels/photos.py `link`).
    first = store.execute("SELECT id FROM messages WHERE role = 'user' ORDER BY id").fetchone()[0]
    linked = [tuple(row) for row in store.execute("SELECT message_id, n, path FROM message_photos")]
    assert linked == [(first, 0, str(photo))]


async def test_no_escalation_when_the_model_just_answers(scripted_conversation):
    convo = scripted_conversation(["answer"])
    reply = await convo.send("hello")
    assert reply.escalated_from is None
    assert reply.role is Role.CONVERSE
    assert reply.text == "final answer"


async def test_one_escalation_lands_on_reason(scripted_conversation):
    convo = scripted_conversation(["escalate", "answer"])
    reply = await convo.send("something hard")
    assert reply.escalated_from is Role.CONVERSE
    assert reply.role is Role.REASON
    assert reply.escalation_reason == "scripted"


async def test_the_top_tier_is_for_when_the_user_asks_for_depth(scripted_conversation):
    """The maintainer, 5 October 2026: "high effort reasoning has its uses but doesn't
    need to be always". A reasoning tier that escalates again stays where it
    is unless the user asked it to think hard -- and then it goes straight there."""
    convo = scripted_conversation(["escalate", "escalate", "answer"])
    reply = await convo.send("compare these two campsites")
    assert reply.escalated_from is Role.CONVERSE
    assert reply.role is Role.REASON, "climbed to high effort unasked"

    convo = scripted_conversation(["escalate", "answer"])
    reply = await convo.send("think hard about which campsite is better")
    assert reply.role is Role.ESCALATE, "she asked for depth and did not get it"


async def test_a_model_that_never_answers_degrades_gracefully(scripted_conversation):
    """A model stuck in a tool loop must not reach the user as a stack trace.

    Two ceilings apply here. The ladder stops it climbing tiers, and the
    per-hop usage limit stops it looping on tools within a tier. Together they
    bound the spend; this test asserts the user gets a sentence rather than an
    exception when both fire.
    """
    convo = scripted_conversation(["escalate"] * 40)
    reply = await convo.send("loop forever, and think hard")
    assert reply.role is Role.ESCALATE, "should have climbed to the top tier"
    assert "stuck" in reply.text.lower()
    assert reply.cost_usd >= 0


async def test_abandoned_attempts_do_not_pollute_history(scripted_conversation, store):
    """A replay starts from the history as it was before the turn, so the
    escalate tool call never appears in the transcript the user sees."""
    convo = scripted_conversation(["escalate", "answer"])
    await convo.send("hard")
    stored = [
        r["role"] for r in store.execute("SELECT role FROM messages ORDER BY id")
    ]
    assert stored == ["user", "assistant"], stored


async def test_every_hop_is_billed(scripted_conversation, store):
    """Cost accounting must include the abandoned attempt; pretending an
    escalated turn cost one call understates spend by design. One bill per
    request: the hop that escalated made one, the answer another."""
    convo = scripted_conversation(["escalate", "answer"])
    reply = await convo.send("hard")
    assert reply.latency_ms >= 0
    rows = store.execute("SELECT session_id, category FROM usage_events").fetchall()
    assert len(rows) == 2
    assert {(r["session_id"], r["category"]) for r in rows} == {(convo.session_id, "background")}


@pytest.mark.parametrize("channel,category", [("tray", "chat"), ("signal", "chat"),
                                              ("routine", "background")])
async def test_a_turn_is_chats_where_the_user_is_present_and_backgrounds_elsewhere(
        scripted_conversation, store, channel, category):
    """The user's day allowance for talking is theirs: a routine's turn
    spending it would leave them unable to ask anything, and a chat billed
    as background would stop the routines instead."""
    convo = scripted_conversation(["answer"], channel=channel)
    await convo.send("hello")
    assert {r["category"] for r in store.execute("SELECT category FROM usage_events")} == {
        category}


async def test_each_hop_says_how_many_requests_it_made_and_tools_it_called(
        scripted_conversation, store):
    """The slow turns of 5-7 October 2026 were the ones with ten or more tool
    calls, each result resent on every later request; the token totals said
    they were heavy, not why. The context redesign is measured by this."""
    convo = scripted_conversation(["answer"])

    await convo.send("what's on today?")

    [row] = store.execute(
        "SELECT t.requests, t.tool_calls, m.role FROM turn_stats t"
        " JOIN messages m ON m.id = t.message_id").fetchall()
    assert row["requests"] >= 1 and row["tool_calls"] == 0 and row["role"] == "assistant"


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------


def test_claim_is_atomic(store):
    """Two claims must never return the same job."""
    for i in range(3):
        jobs.enqueue(store, kind="research", prompt=f"task {i}")
    seen = {jobs.claim(store).id for _ in range(3)}
    assert len(seen) == 3
    assert jobs.claim(store) is None


def test_priority_beats_age(store):
    jobs.enqueue(store, kind="research", prompt="old, low priority")
    urgent = jobs.enqueue(store, kind="research", prompt="new, high priority", priority=5)
    assert jobs.claim(store).id == urgent


def test_failure_requeues_until_attempts_are_exhausted(store):
    job_id = jobs.enqueue(store, kind="research", prompt="flaky")
    for _ in range(2):
        jobs.claim(store)
        assert jobs.fail(store, job_id, "boom") == "queued"
    jobs.claim(store)
    assert jobs.fail(store, job_id, "boom") == "failed"
    assert jobs.claim(store) is None


def _claimed_by_a_dead_worker(store, job_id):
    with store:
        store.execute("UPDATE jobs SET claimed_by = '999999:1' WHERE id = ?", (job_id,))


def test_crashed_jobs_are_requeued_on_startup(store):
    """A worker that died mid-run leaves a job 'running' forever otherwise."""
    job_id = jobs.enqueue(store, kind="research", prompt="interrupted")
    jobs.claim(store)
    assert jobs.get(store, job_id).status == "running"
    _claimed_by_a_dead_worker(store, job_id)

    assert jobs.requeue_stale(store) == 1
    assert jobs.get(store, job_id).status == "queued"


def test_a_job_whose_worker_is_still_alive_is_left_to_it(store):
    """The swap. The old worker is alive and finishing the job, and the new
    one used to requeue it at startup and run it a second time -- for an
    approved action, the email sent twice."""
    job_id = jobs.enqueue(store, kind="action", prompt="send the email")
    jobs.claim(store)  # claimed by this, a living process

    assert jobs.requeue_stale(store) == 0
    assert jobs.get(store, job_id).status == "running"


def test_a_claim_names_the_process_and_when_it_started(store):
    """A pid alone is reused; with its start time it names one process."""
    import os

    job_id = jobs.enqueue(store, kind="research", prompt="x")
    jobs.claim(store)
    claimed_by = store.execute(
        "SELECT claimed_by FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()[0]

    assert claimed_by.startswith(f"{os.getpid()}:")
    assert jobs._claimant_alive(claimed_by)
    assert not jobs._claimant_alive(f"{os.getpid()}:1")  # same pid, other process


def test_a_row_from_before_claims_were_recorded_counts_as_orphaned(store):
    job_id = jobs.enqueue(store, kind="research", prompt="legacy")
    jobs.claim(store)
    with store:
        store.execute("UPDATE jobs SET claimed_by = NULL WHERE id = ?", (job_id,))

    assert jobs.requeue_stale(store) == 1


def test_completion_is_reported_once(store):
    job_id = jobs.enqueue(store, kind="research", prompt="task")
    jobs.claim(store)
    jobs.complete(store, job_id, "the answer")

    pending = jobs.pending_notifications(store)
    assert [j.id for j in pending] == [job_id]

    jobs.mark_notified(store, [job_id])
    assert jobs.pending_notifications(store) == []


# ---------------------------------------------------------------------------
# Deferral
# ---------------------------------------------------------------------------


def _deferring_model(times: int):
    """Calls defer_to_background `times`, then answers."""
    state = {"i": 0}

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        state["i"] += 1
        if state["i"] <= times:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "defer_to_background",
                        {"task": "research something", "why": "too big for live"},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("queued it")])

    return FunctionModel(respond)


@pytest.fixture
def deferring_conversation(store, isolated_memories, monkeypatch):
    def make(times: int, channel: str = "tray"):
        model = _deferring_model(times)

        def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
            from pydantic_ai import Agent

            agent = Agent(model, deps_type=Deps, system_prompt="test", retries=2)
            toolset.register_tools(agent, allow=allow)
            return agent

        monkeypatch.setattr(loop, "build_agent", build_agent)
        return Conversation(store, channel=channel)

    return make


async def test_a_single_deferral_queues_one_job(deferring_conversation, store):
    convo = deferring_conversation(1)
    reply = await convo.send("do something big")
    assert len(reply.deferred_jobs) == 1
    assert store.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1


async def test_repeated_deferral_still_queues_one_job(deferring_conversation, store):
    """Regression: one request became three research jobs.

    Each queued copy costs a full research run, so a model that calls this
    three times triples the bill for work the user asked for once.
    """
    convo = deferring_conversation(3)
    reply = await convo.send("do something big")
    assert store.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1, (
        "duplicate jobs were queued"
    )
    assert len(set(reply.deferred_jobs)) == 1


async def test_each_tool_result_is_stored_once_in_the_turn_that_made_it(
    store, isolated_memories, monkeypatch
):
    """Stored from `all_messages()`, every turn wrote every earlier turn's
    tool results again under its own time: four questions about smart
    glasses on 25 September each recorded three `add_task` calls, so the
    record -- and the reflection judge reading it -- showed tasks being added
    fifteen times when they were added once."""
    from pydantic_ai.messages import ToolReturnPart

    async def respond(messages, info: AgentInfo) -> ModelResponse:
        if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(
            "defer_to_background", {"task": "look into it", "why": "big"})])

    def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
        from pydantic_ai import Agent

        agent = Agent(FunctionModel(respond), deps_type=Deps, system_prompt="test")
        toolset.register_tools(agent, allow=allow)
        return agent

    monkeypatch.setattr(loop, "build_agent", build_agent)
    convo = Conversation(store, channel="tray")
    for question in ("one", "two", "three"):
        await convo.send(question)

    stored = store.execute(
        "SELECT COUNT(*) n FROM messages WHERE session_id = ? AND role = 'tool'",
        (convo.session_id,),
    ).fetchone()["n"]
    assert stored == 3, f"{stored} tool rows for three calls"


@pytest.mark.parametrize("channel", ["night", "job", "initiative"])
async def test_a_conversation_nobody_is_waiting_on_does_not_defer(
    deferring_conversation, store, channel
):
    """The night's research deferred each topic, the background job it became
    deferred again, and every hop came back to their phone at 3am as "Queued a
    background research task". Deferring is for someone who is waiting."""
    convo = deferring_conversation(1, channel=channel)
    reply = await convo.send("research something")

    assert reply.deferred_jobs == []
    assert store.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 0


# --------------------------------------------------------------------------
# Tool profiles
# --------------------------------------------------------------------------


def _tool_names(agent) -> list[str]:
    toolset = getattr(agent, "_function_toolset", None)
    return sorted(getattr(toolset, "tools", {}) or {})


def test_the_full_toolset_is_the_default():
    from agent.models import Role

    assert len(_tool_names(loop.build_agent(Role.CONVERSE))) > 15


# --------------------------------------------------------------------------
# What reaches the model as its instructions
# --------------------------------------------------------------------------


def _instructions_sent(agent, history=None) -> str:
    """What reached the model as its instructions, on one real run.

    Read off the request itself rather than the agent's private attributes:
    what the agent was configured with and what the model was sent are
    exactly the two things that turned out to differ.
    """
    seen: list[str] = []

    def capture(messages, info: AgentInfo):
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    with agent.override(model=FunctionModel(capture)):
        agent.run_sync("hello", message_history=list(history or []))
    return seen[0]


def _resumed_history():
    """History shaped exactly the way `Conversation._resume` rebuilds it
    from the database after a worker restart: text only, no system part."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    return [
        ModelRequest(parts=[UserPromptPart(content="What is on my calendar today?")]),
        ModelResponse(parts=[TextPart(content="Two meetings.")]),
    ]


def test_a_resumed_conversation_still_gets_the_system_prompt():
    """pydantic-ai sends a `system_prompt` only on a run with no history,
    and `_resume` reloads history after every worker restart. So every
    conversation that outlived a swap, a crash or a sleep carried on with
    no system.md -- no persona, no rules about drafting mail as them, no
    tool guidance -- and nothing looked wrong until it answered like a
    stranger."""
    house = loop.system_prompt().strip().splitlines()[0]

    sent = _instructions_sent(loop.build_agent(Role.CONVERSE), _resumed_history())

    assert house in sent


def test_every_turn_tells_the_model_the_users_language():
    """It answered in English whatever the user's Windows was in, until
    they asked otherwise -- and after a restart, again. The user's choice
    goes with every request, a resumed conversation's included."""
    knobs.write({"LANGUAGE": "fr-FR"})

    for history in (None, _resumed_history()):
        sent = _instructions_sent(loop.build_agent(Role.CONVERSE), history)
        assert f"{language.name('fr-FR')} (fr-FR)" in sent


def test_a_resumed_phone_call_keeps_its_note():
    """The phone note is how the model knows it is being spoken aloud."""
    agent = loop.build_agent(Role.CONVERSE, note="\n\nPHONE-NOTE-MARKER")

    assert "PHONE-NOTE-MARKER" in _instructions_sent(agent, _resumed_history())


def test_the_clock_comes_after_the_prompt_so_the_prompt_stays_cached():
    """The prompt is identical every turn and the time line changes every
    minute. With the time first, the cached prefix ended at token zero."""
    house = loop.system_prompt().strip().splitlines()[0]
    sent = _instructions_sent(loop.build_agent(Role.CONVERSE))
    clock = loop._now_line()[:30]  # "Right now it is <weekday> <date>"

    assert sent.index(house) < sent.index(clock)


def test_a_message_carries_a_readable_local_time():
    """Both mailboxes return `internal_date` as epoch milliseconds, which is
    right for sorting and wrong for a model. Asked for unread Contoso mail it
    rendered every timestamp four hours out, because nothing in the payload
    says which zone the number is in and UTC is the obvious guess.

    Same instinct as `_now_line`: a timestamp the model has to convert is
    one it will eventually convert wrongly, and the machine already knows.
    """
    import datetime as dt

    from tzlocal import get_localzone

    from agent.toolset.mail import localise as _localise

    zone = get_localzone()
    moment = dt.datetime(2026, 9, 21, 14, 5, tzinfo=zone)
    [message] = _localise([{"internal_date": int(moment.timestamp() * 1000)}])

    assert message["when"].startswith("2026-09-21 14:05")


def test_a_message_with_no_timestamp_is_left_alone():
    """A Gmail draft has no `internalDate`, and inventing 1970 for it would
    be worse than saying nothing."""
    from agent.toolset.mail import localise as _localise

    [message] = _localise([{"subject": "no date"}])

    assert "when" not in message


# --------------------------------------------------------------------------
# A conversation that survives a restart
# --------------------------------------------------------------------------


def test_a_conversation_reloads_what_was_said_today(tmp_path):
    """`_history` lives in memory and the worker restarts constantly -- every
    self-upgrade swap, every crash, every time the laptop sleeps. So a
    follow-up landed on a model that had never seen the thing it followed,
    and answered anyway rather than saying it had lost the thread.
    """
    from agent.loop import Conversation

    conn = db.open_store(tmp_path / "h.db")
    try:
        first = Conversation(conn, channel="signal")
        first._store_message("user", "what are my emails today?")
        first._store_message("assistant", "Two: a Fabrikam kick-off and an ITS ticket.")

        resumed = Conversation(conn, channel="signal", session_id=first.session_id)

        spoken = [
            part.content
            for message in resumed._history
            for part in message.parts
        ]
        # Their words carry when the user said them (tests/unit/test_history_context.py).
        assert spoken[0].startswith("what are my emails today?\n\n[asked ")
        assert spoken[1:] == ["Two: a Fabrikam kick-off and an ITS ticket."]
    finally:
        conn.close()


def test_yesterdays_conversation_is_not_reloaded(tmp_path):
    """A rolling window, not an archive. Resuming a week of chat would bill
    the whole thing on every turn and bury the current question in it.
    """
    from agent.loop import Conversation

    conn = db.open_store(tmp_path / "h.db")
    try:
        convo = Conversation(conn, channel="signal")
        convo._store_message("user", "ancient history")
        with conn:
            conn.execute(
                "UPDATE messages SET created_at = datetime('now', '-30 hours')"
                " WHERE session_id = ?",
                (convo.session_id,),
            )
        convo._store_message("user", "today's question")

        resumed = Conversation(conn, channel="signal", session_id=convo.session_id)

        spoken = [p.content.split("\n\n[asked ")[0] for m in resumed._history for p in m.parts]
        assert spoken == ["today's question"]
    finally:
        conn.close()


def test_tool_rows_are_not_replayed_as_history(tmp_path):
    """Tool results are stored for reflection's benefit, but a tool-return
    part without its matching call is malformed history -- and a stale
    search result is worth less than the sentence that summarised it.
    """
    from agent.loop import Conversation

    conn = db.open_store(tmp_path / "h.db")
    try:
        convo = Conversation(conn, channel="signal")
        convo._store_message("user", "check my mail")
        convo._store_message("tool", '{"messages": []}', tool_name="search_email")
        convo._store_message("assistant", "Nothing new.")

        resumed = Conversation(conn, channel="signal", session_id=convo.session_id)

        spoken = [p.content.split("\n\n[asked ")[0] for m in resumed._history for p in m.parts]
        assert spoken == ["check my mail", "Nothing new."]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# An effect happens once per turn, however often the turn is run
# --------------------------------------------------------------------------


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


@pytest.fixture
def free_effects(store, monkeypatch):
    """A free (ungated) tool whose executions are counted."""
    from agent import approvals

    ran: list[dict] = []

    async def perform(tool, args):
        ran.append(args)
        return {"ok": True, "created": args.get("title")}

    monkeypatch.setattr(actions, "perform", perform)
    monkeypatch.setattr(approvals, "record_free", lambda *a, **k: None)
    return ran


async def test_a_retried_or_escalated_turn_does_not_repeat_its_effects(store, free_effects):
    """`run_agent` retries by running the whole agent again, and escalation
    replays the turn on a stronger tier -- so a calendar event created on
    the first attempt was created again on each later one. Every hop of a
    turn shares one record of what it has already done."""
    performed: dict = {}
    first_hop = _Ctx(Deps(conn=store, session_id=1, performed=performed))
    escalated_hop = _Ctx(Deps(conn=store, session_id=1, role=Role.REASON, performed=performed))
    args = {"title": "Dentist", "start": "2026-09-24T10:00"}

    once = await actions.act(first_hop, "add_task", args, "Add Dentist")
    again = await actions.act(first_hop, "add_task", dict(args), "Add Dentist")  # a retry
    replayed = await actions.act(escalated_hop, "add_task", dict(args), "Add Dentist")

    assert len(free_effects) == 1
    assert once == again == replayed


async def test_a_different_effect_in_the_same_turn_still_happens(store, free_effects):
    ctx = _Ctx(Deps(conn=store, session_id=1))

    await actions.act(ctx, "add_task", {"title": "Dentist"}, "Add Dentist")
    await actions.act(ctx, "add_task", {"title": "Dry cleaning"}, "Add Dry cleaning")

    assert len(free_effects) == 2


def test_every_hop_of_a_turn_shares_one_record():
    """The memo only works if the turn hands the same dict to each hop's
    Deps; a fresh one per hop is the bug again."""
    import ast
    import inspect

    source = inspect.getsource(loop.Conversation._turn)
    calls = [n for n in ast.walk(ast.parse(source.strip() if source.startswith(" ") else source))
             if isinstance(n, ast.Call) and ast.unparse(n.func) == "Deps"]

    assert calls and all(any(k.arg == "performed" for k in c.keywords) for c in calls)


async def test_a_retried_model_call_says_so_in_the_log(caplog, monkeypatch):
    """27 September 2026: a reply took five minutes, one stalled request and
    a retry, and the log showed nothing between the message arriving and the
    answer. A slow turn nobody can explain afterwards is one nobody fixes."""
    import logging

    import httpx2
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from agent import llm

    calls = {"n": 0}

    async def stalls_once(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx2.ReadTimeout("timed out")
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(llm, "backoff_delay", lambda attempt: 0.0)
    with caplog.at_level(logging.WARNING, logger="agent.llm"):
        result, record = await llm.run_agent(Agent(FunctionModel(stalls_once)), "hi",
                                             Role.CONVERSE)

    assert result.output == "done" and record.attempts == 2
    assert any("ReadTimeout" in r.getMessage() and "retry 1" in r.getMessage()
               for r in caplog.records)


async def test_a_reply_cut_off_mid_stream_is_tried_again(monkeypatch):
    """The SDK tries a request again when its connection drops, but not a
    stream it has handed over: a reply cut off partway reached the turn as
    pydantic-ai's `ModelAPIError`, which was never tried again. On a
    laptop's network the commonest failure ended the turn at once."""
    from pydantic_ai import Agent
    from pydantic_ai.exceptions import ModelAPIError
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from agent import llm

    calls = {"n": 0}

    async def drops_once(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ModelAPIError("gpt-6-luna", "Connection error.")
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(llm, "backoff_delay", lambda attempt: 0.0)
    result, record = await llm.run_agent(Agent(FunctionModel(drops_once)), "hi",
                                         Role.CONVERSE)

    assert result.output == "done" and record.attempts == 2


@pytest.mark.parametrize("code,calls_made", [("insufficient_quota", 1),
                                             ("rate_limit_exceeded", 2)])
async def test_a_spent_balance_is_not_retried_though_it_comes_as_a_429(monkeypatch, code,
                                                                       calls_made):
    """OpenAI says an account's balance is spent with the rate limit's
    status. Read by its status alone, a turn waited out every backoff, up to
    half a minute, for the same answer; a real rate limit still waits."""
    from pydantic_ai import Agent
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from agent import llm

    calls = {"n": 0}

    async def refuses_once(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ModelHTTPError(429, "gpt-6-luna", {"message": "no", "type": code,
                                                     "code": code})
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(llm, "backoff_delay", lambda attempt: 0.0)
    agent = Agent(FunctionModel(refuses_once))
    if calls_made == 1:
        with pytest.raises(ModelHTTPError):
            await llm.run_agent(agent, "hi", Role.CONVERSE)
    else:
        result, _ = await llm.run_agent(agent, "hi", Role.CONVERSE)
        assert result.output == "done"
    assert calls["n"] == calls_made


def test_the_sdk_s_own_retries_reach_the_worker_log():
    """The worker quiets the openai logger to WARNING, and the SDK reports
    the retries it makes on its own at INFO -- so those were invisible too."""
    import logging

    from agent import server

    server._configure_logging()

    assert logging.getLogger("openai._base_client").isEnabledFor(logging.INFO)
    assert not logging.getLogger("openai").isEnabledFor(logging.INFO)


def test_a_worker_that_dies_natively_leaves_every_threads_stack_in_its_log():
    """Three workers died between 24 and 29 September on an access violation
    or heap corruption in ntdll.dll, and each log just stopped: no Python
    traceback, so no way to tell which library did it. pytest turns
    faulthandler on itself, so it is turned off first."""
    import faulthandler

    from agent import server

    faulthandler.disable()
    try:
        server._configure_logging()
        assert faulthandler.is_enabled()
    finally:
        faulthandler.enable()
