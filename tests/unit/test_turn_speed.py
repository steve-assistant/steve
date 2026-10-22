"""A turn carries what was said, not every page read before, and its prefix
stays cacheable. The maintainer, 5 October 2026: "the agent is being a bit slow to
answer". A Signal thread was re-sending up to 935,000 tokens a turn -- every
earlier turn's pages and search results -- and the clock line, sitting in the
instructions ahead of the whole history, kept any of it from being cached."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent import context, history, loop
from agent.loop import Conversation
from agent.turn import Deps

PAGE = "PAGE-MARKER " + "x" * 30_000


@pytest.fixture
def store(tmp_path):
    from core import db

    conn = db.open_store(tmp_path / "t.db")
    yield conn
    conn.close()


@pytest.fixture
def isolated_memories(memories_repo):
    """Memory in a throwaway repo, so no test touches the real one."""
    return memories_repo


@pytest.fixture
def scripted(store, isolated_memories, monkeypatch):
    """A Conversation whose model is a script: `respond(messages)` per
    request, with every request it was sent kept in `sent`."""

    def make(respond):
        sent: list[list] = []

        async def model(messages, info: AgentInfo) -> ModelResponse:
            sent.append(list(messages))
            return respond(messages)

        def build_agent(role, *, cache_key=None, allow=None, note=None, lead=False, clock=True,
                    prompt=None, on=None):
            from pydantic_ai import Agent

            agent = Agent(FunctionModel(model), deps_type=Deps, instructions="house")

            @agent.tool_plain
            def read_page(url: str) -> str:
                """Read a page."""
                return PAGE

            return agent

        monkeypatch.setattr(loop, "build_agent", build_agent)
        return Conversation(store, channel="signal"), sent

    return make


def _found_text(messages) -> str:
    return " ".join(str(p.content) for m in messages if isinstance(m, ModelRequest)
                    for p in m.parts if isinstance(p, UserPromptPart)
                    and str(p.content).startswith("[what your tools found"))


def _tool_text(messages) -> str:
    return " ".join(str(p.content) for m in messages if isinstance(m, ModelRequest)
                    for p in m.parts if isinstance(p, ToolReturnPart))


async def test_a_page_read_two_turns_ago_is_not_sent_again(scripted):
    """The camping thread: each turn's pages were in every later turn."""
    def respond(messages):
        last = messages[-1]
        asked = any(isinstance(p, UserPromptPart) and "find" in str(p.content)
                    for p in last.parts)
        if asked:
            return ModelResponse(parts=[ToolCallPart("read_page", {"url": "https://x"})])
        return ModelResponse(parts=[TextPart("Two sites: Pine Ridge and Elk Lake.")])

    convo, sent = scripted(respond)
    await convo.send("find camping sites")
    sent.clear()
    await convo.send("which is closer?")
    # Their conversations read a digest of what the last exchange's tools found,
    # beside their request (agent/context.py); others keep the cut tool result.
    follow_up = _tool_text(sent[0]) + _found_text(sent[0])
    assert "PAGE-MARKER" in follow_up, "the follow-up loses the page it is about"
    assert len(follow_up) <= max(history.LAST_TURN_TOOL_CHARS, context.FOUND_TOTAL) + 200

    sent.clear()
    await convo.send("thanks")
    later = sent[0]
    assert "PAGE-MARKER" not in _tool_text(later) + _found_text(later)
    said = " ".join(str(p.content) for m in later for p in m.parts
                    if isinstance(p, UserPromptPart | TextPart))
    assert "find camping sites" in said and "Pine Ridge" in said, "what was said is kept"


async def test_the_clock_rides_with_user_words_so_the_instructions_never_change(
        store, isolated_memories, monkeypatch):
    """The real agent. Instructions come before the whole history; with the
    time in them, no turn's history was ever read from the cache."""
    seen: list[tuple[str, str]] = []

    async def model(messages, info: AgentInfo) -> ModelResponse:
        prompt = " ".join(str(p.content) for p in messages[-1].parts
                          if isinstance(p, UserPromptPart))
        seen.append((messages[-1].instructions or "", prompt))
        return ModelResponse(parts=[TextPart("ok")])

    monkeypatch.setattr(loop, "model_for", lambda role: FunctionModel(model))
    clocks = iter(["Right now it is Monday 05 October 2026, 18:34",
                   "Right now it is Monday 05 October 2026, 18:41"])
    monkeypatch.setattr(loop, "_now_line", lambda: next(clocks))
    convo = Conversation(store, channel="tray")

    await convo.send("hello")
    await convo.send("and now?")

    (first_instructions, first_prompt), (second_instructions, second_prompt) = seen
    assert first_instructions == second_instructions
    assert "Right now" not in first_instructions
    assert "18:34" in first_prompt and "18:41" in second_prompt


async def test_their_kept_skills_reach_the_prompt(store, isolated_memories, monkeypatch, caplog):
    """`_skills_line` ran in a worker thread, where the loop's SQLite
    connection is refused, so every turn logged "could not list the skills"
    and no skill was ever offered."""
    seen: list[str] = []

    async def model(messages, info: AgentInfo) -> ModelResponse:
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    with store:
        store.execute("INSERT INTO skills (title, when_to_use, steps, tools, status)"
                      " VALUES ('Weekly recap', 'when she asks for her week', '[]', '[]',"
                      " 'active')")
    monkeypatch.setattr(loop, "model_for", lambda role: FunctionModel(model))
    await Conversation(store, channel="tray").send("hello")

    assert "Weekly recap" in seen[0]
    assert "could not list the skills" not in caplog.text


async def test_every_turn_knows_who_the_user_is_without_having_to_recall(store, isolated_memories,
                                                                    monkeypatch, caplog):
    """The model called `recall` on 13 of their 148 turns, so the assistant the user
    talks to knew less about them than the triage judge, which always gets
    these lines. Their profile (agent/facts.py), each fact once, in a stable
    order, after the house prompt."""
    seen: list[str] = []

    async def model(messages, info: AgentInfo) -> ModelResponse:
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    from agent import facts

    facts.add(store, "user", "advisor", "Mira Shah", source="user_words")
    facts.add(store, "user", "advisor", "Mira Shah", source="user_words")
    facts.add(store, "user", "job", "product designer at Contoso", source="confirmed")
    facts.add(store, "org:northwind.example", "email_pattern",
              "lastname.firstname@northwind.example",
              source="user_words")
    monkeypatch.setattr(loop, "model_for", lambda role: FunctionModel(model))
    await Conversation(store, channel="signal").send("hello")

    said = seen[0]
    # After the house prompt, so the prompt stays the cached prefix.
    assert said.index(loop.system_prompt()[:60]) < said.index("designer at Contoso")
    assert said.count("Mira Shah") == 1 and "designer at Contoso" in said
    assert "northwind.example" not in said, "only what is about her, not everything kept"
    assert "could not read the profile" not in caplog.text


async def test_the_models_citation_marks_never_reach_them(scripted, store):
    convo, _ = scripted(lambda messages: ModelResponse(parts=[TextPart(
        "No. Sunny tomorrow, high near 64°F. citeturn0search1")]))
    reply = await convo.send("will it rain tomorrow")
    stored = store.execute("SELECT content FROM messages WHERE role = 'assistant'"
                           " ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert reply.text == "No. Sunny tomorrow, high near 64°F." == stored


# ---------------------------------------------------------------------------
# compact, on its own
# ---------------------------------------------------------------------------


def _turn(asked, *, tool=None, answer="ok"):
    out = [ModelRequest(parts=[UserPromptPart(asked)])]
    if tool:
        out += [ModelResponse(parts=[TextPart("Let me check."),
                                     ToolCallPart("read_page", {"url": "u"}, tool_call_id="c")]),
                ModelRequest(parts=[ToolReturnPart("read_page", tool, tool_call_id="c")])]
    out.append(ModelResponse(parts=[TextPart(answer)]))
    return out


def test_something_said_to_them_outside_a_turn_is_kept():
    """A ping or a card (`note_said`) is what their next words may answer."""
    said = _turn("find sites", tool=PAGE, answer="Two sites.") + [
        ModelResponse(parts=[TextPart("Rain from 3pm; your 4pm is in person.")])
    ] + _turn("move it to zoom")
    kept = history.compact(said)
    texts = [p.content for m in kept for p in m.parts if isinstance(p, TextPart)]
    assert "Rain from 3pm; your 4pm is in person." in texts
    assert "Let me check." not in texts, "interim words on the way to a tool are dropped"
    assert "PAGE-MARKER" not in _tool_text(kept)


def test_only_the_last_turns_are_kept():
    many = [m for i in range(40) for m in _turn(f"question {i}")]
    kept = history.compact(many)
    asked = [p.content for m in kept for p in m.parts if isinstance(p, UserPromptPart)]
    assert len(asked) == history.MAX_TURNS and asked[-1] == "question 39"


def test_the_latest_turn_keeps_its_tool_calls_paired_with_their_results():
    kept = history.compact(_turn("first") + _turn("find", tool=PAGE))
    calls = [p for m in kept for p in m.parts if isinstance(p, ToolCallPart)]
    returns = [p for m in kept for p in m.parts if isinstance(p, ToolReturnPart)]
    assert len(calls) == len(returns) == 1 and calls[0].tool_call_id == returns[0].tool_call_id
    assert len(str(returns[0].content)) <= history.LAST_TURN_TOOL_CHARS + 200


def test_an_earlier_turn_keeps_when_it_was_asked_not_what_time_it_was_then():
    """Each request carries ~90 tokens of "Right now it is ... resolve every
    relative date against this". Kept whole in history, fifteen turns made
    fifteen claims about what time it is now, all wrong but the last."""
    import datetime as dt

    then = dt.datetime(2026, 10, 4, 9, 5, tzinfo=dt.timezone(dt.timedelta(hours=-4), "EDT"))
    stamped = f"find campsites\n\n[{loop.now_line_at(then)}]"
    kept = history.compact(_turn(stamped) + _turn("and the second one?"))

    asked = [p.content for m in kept for p in m.parts if isinstance(p, UserPromptPart)]
    assert asked[0] == "find campsites\n\n[asked Sunday 04 October 2026, 09:05]"
    assert "Right now" not in " ".join(asked)


def test_a_new_conversation_starts_on_the_cache_its_channel_already_warmed(store):
    """OpenAI routes by the cache key. A key per conversation sent every new
    one to a cache that had never seen the ~14,000 tokens each turn starts
    with: 0% cached on a new conversation's first request, against 100% on
    a key already used (measured 6 October 2026)."""
    first, second = Conversation(store, channel="job"), Conversation(store, channel="job")

    assert first.cache_key == second.cache_key == loop.cache_key_for("job")
