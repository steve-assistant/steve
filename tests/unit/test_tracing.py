"""Tracing setup, and the privacy decision inside it.

A traced model call carries the prompt and the reply verbatim -- the mail,
the calendar, the whereabouts that the whole of redact.py exists to keep out
of a third party's hands. Shipping that to a remote collector would quietly
undo the phase this sits in, so these pin where content is allowed to go.
"""

from __future__ import annotations

import pytest

from agent import tracing


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """And never the real `instrument_all`: it turns tracing on for every
    agent in the process, and nothing turned it off. Every model test after
    these tried to export spans to a Phoenix that was not there, and the
    noise buried the one line of a real failure (28 September 2026)."""
    instrumented = []
    monkeypatch.setattr("pydantic_ai.Agent.instrument_all",
                        lambda settings=True: instrumented.append(settings))
    monkeypatch.setattr(tracing, "_state", {"enabled": False, "reason": "not configured"})
    monkeypatch.delenv(tracing.ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(tracing.CONTENT_ENV, raising=False)
    yield


# --------------------------------------------------------------------------
# Off by default
# --------------------------------------------------------------------------


def test_tracing_is_off_unless_asked_for():
    """A personal assistant should not open a network connection because a
    library got installed."""
    assert tracing.setup()["enabled"] is False


def test_an_endpoint_from_the_environment_turns_it_on(monkeypatch):
    monkeypatch.setenv(tracing.ENDPOINT_ENV, "http://127.0.0.1:6006/v1/traces")
    assert tracing.setup()["enabled"] is True


def test_a_broken_collector_does_not_stop_the_agent(monkeypatch):
    """A missing collector is a reason to run untraced, not a reason the
    worker fails to start."""

    def explode(*a, **k):
        raise RuntimeError("no exporter here")

    monkeypatch.setattr(tracing, "_content_allowed", explode)
    state = tracing.setup("http://127.0.0.1:6006/v1/traces")
    assert state["enabled"] is False
    assert "RuntimeError" in state["reason"]


# --------------------------------------------------------------------------
# Where content may go
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:6006/v1/traces",
        "http://localhost:6006/v1/traces",
        "http://[::1]:6006/v1/traces",
    ],
)
def test_a_local_collector_may_see_prompts(endpoint):
    """Phoenix on loopback is the intended setup, and a trace without the
    prompt in it is most of the way to useless for debugging a bad answer."""
    assert tracing._content_allowed(endpoint) is True


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://otel.example.com/v1/traces",
        "http://192.168.1.50:4318/v1/traces",
        "http://traces.internal:4318/v1/traces",
    ],
)
def test_a_remote_collector_gets_no_prompt_text(endpoint):
    """Spans keep the shape of the call -- model, tokens, latency, errors --
    and lose the text. Otherwise tracing becomes the leak that reflection was
    careful not to be."""
    assert tracing._content_allowed(endpoint) is False


def test_content_can_be_forced_on_for_a_collector_you_own(monkeypatch):
    """A default, not a lock: someone running their own collector elsewhere
    is allowed to decide that is fine."""
    monkeypatch.setenv(tracing.CONTENT_ENV, "1")
    assert tracing._content_allowed("https://otel.example.com/v1/traces") is True


def test_content_can_be_forced_off_for_a_local_one(monkeypatch):
    monkeypatch.setenv(tracing.CONTENT_ENV, "0")
    assert tracing._content_allowed("http://127.0.0.1:6006/v1/traces") is False


def test_setup_reports_whether_content_is_included(monkeypatch):
    monkeypatch.setenv(tracing.CONTENT_ENV, "0")
    state = tracing.setup("http://127.0.0.1:6006/v1/traces")
    assert state["content"] is False
    assert state["local"] is True
