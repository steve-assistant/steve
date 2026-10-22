"""Tracing model calls, when somebody is listening.

pydantic-ai emits OpenTelemetry spans natively, so this is configuration
rather than instrumentation: one global switch at startup and every agent in
the process is traced, including ones added later.

Off unless an endpoint is configured. A personal assistant should not open a
network connection or start a server because a library was installed.

**Span content is personal data.** A traced model call carries the prompt and
the reply verbatim -- the mail, the calendar, the whereabouts that the whole
of `redact.py` exists to keep out of a third party's hands. Sending that to a
remote collector would undo the phase it sits in. So content is included only
for a loopback endpoint, and a remote one gets spans with the shape of the
call and none of its text. That is a default, not a lock: `STEVE_TRACE_CONTENT`
overrides it for someone who genuinely runs their own collector elsewhere and
has decided that is fine.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from core import identity

log = logging.getLogger("tracing")

# Where to send spans. Phoenix listens here by default; any OTLP/HTTP
# collector works.
ENDPOINT_ENV = "STEVE_TRACE_ENDPOINT"
CONTENT_ENV = "STEVE_TRACE_CONTENT"

SERVICE_NAME = identity.KEYCHAIN

_state: dict = {"enabled": False, "reason": "not configured"}


def _is_local(endpoint: str) -> bool:
    host = (urlparse(endpoint).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "[::1]"}


def _content_allowed(endpoint: str) -> bool:
    override = os.environ.get(CONTENT_ENV, "").strip().lower()
    if override in {"1", "true", "yes"}:
        return True
    if override in {"0", "false", "no"}:
        return False
    return _is_local(endpoint)


def status() -> dict:
    return dict(_state)


def setup(endpoint: str | None = None) -> dict:
    """Turn on tracing if an endpoint is configured. Safe to call twice.

    Returns the resulting state rather than raising: a missing collector is a
    reason to run untraced, not a reason the agent fails to start.
    """
    if _state.get("enabled"):
        return status()

    endpoint = endpoint or os.environ.get(ENDPOINT_ENV, "").strip()
    if not endpoint:
        _state.update(enabled=False, reason="not configured")
        return status()

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from pydantic_ai import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings

        provider = TracerProvider(
            resource=Resource.create({"service.name": SERVICE_NAME})
        )
        # Batched, so a slow or dead collector cannot add latency to a turn.
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )

        include_content = _content_allowed(endpoint)
        Agent.instrument_all(
            InstrumentationSettings(
                tracer_provider=provider,
                include_content=include_content,
                include_binary_content=False,  # audio, never useful in a trace
            )
        )
        _state.update(
            enabled=True,
            endpoint=endpoint,
            content=include_content,
            local=_is_local(endpoint),
            reason="ok",
        )
    except Exception as exc:  # noqa: BLE001 - tracing is never worth a crash
        log.exception("could not start tracing")
        _state.update(enabled=False, reason=f"{type(exc).__name__}: {exc}"[:200])
    return status()
