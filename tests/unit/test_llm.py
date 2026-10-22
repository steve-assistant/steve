"""The shared ways a model call is made and counted (agent/llm.py).

A call's bill is no longer written here: every client bills itself
(`core/spend.Guarded`, pinned in tests/gate/test_spend.py).

`ask_once`: a dozen places built a one-question agent by hand, naming the
role once for the model and again for its settings. Pinned here: both come
from the one role, and the answer comes back with that role's bill.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agent.llm import ask_once, plain_http_client
from agent.models import MODELS, Role
from agent.provider import settings_for
from core import knobs


async def test_a_one_shot_question_goes_to_its_roles_model_with_that_roles_settings(monkeypatch):
    """The model and its settings named for different roles -- the
    reasoning model cut to the quick roles' 90 seconds and no effort --
    would still have answered, so nothing but this would notice. (Triage's
    settings are the conversation's, which is why the role here is REASON.)"""
    seen: dict = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen["settings"] = info.model_settings
        seen["system"] = messages[0].parts[0].content
        seen["prompt"] = messages[0].parts[-1].content
        return ModelResponse(parts=[TextPart("two searches")])

    def model_for(role):
        seen["role"] = role
        return FunctionModel(respond)

    monkeypatch.setattr("agent.provider.model_for", model_for)

    answer, record = await ask_once(Role.REASON, "Answer from the sources.", "flights to Denver")

    assert answer == "two searches"
    assert seen["role"] is Role.REASON and record.role is Role.REASON
    assert seen["settings"] == settings_for(MODELS[Role.REASON])
    assert (seen["system"], seen["prompt"]) == ("Answer from the sources.", "flights to Denver")


async def test_a_one_shot_question_is_told_the_users_language(monkeypatch):
    """The briefings, summaries and notes written through `ask_once` came
    out in English whatever the user's language was."""
    seen: dict = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen["instructions"] = messages[0].instructions or ""
        return ModelResponse(parts=[TextPart("Bonjour")])

    monkeypatch.setattr("agent.provider.model_for", lambda role: FunctionModel(respond))
    knobs.write({"LANGUAGE": "fr-FR"})

    await ask_once(Role.BULK, "Write the morning briefing.", "two meetings")

    assert "(fr-FR)" in seen["instructions"]


class _KeptAlive(BaseHTTPRequestHandler):
    """Answers "ok" and keeps the connection open, as the models' API does."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - the name http.server calls
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def kept_alive():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KeptAlive)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


def test_the_models_client_serves_one_event_loop_after_another(kept_alive):
    """The process has one models' client (`agent/provider._openai_provider`),
    and a module build uses it from loops of its own: `agent/modbuild.build`
    runs the pipeline, then admission, each under its own `asyncio.run`, in
    a thread beside the worker's loop. With one connection pool, a later
    loop took up a connection an earlier one had opened and closed with it.
    In the module-building eval of 15 October the code reviewer's call
    failed so ("Event loop is closed") in two builds of four, and a failed
    review is a no on the card."""
    client = plain_http_client()

    async def get() -> str:
        return (await client.get(kept_alive)).text

    assert asyncio.run(get()) == "ok"
    assert asyncio.run(get()) == "ok"
    assert asyncio.run(get()) == "ok"
