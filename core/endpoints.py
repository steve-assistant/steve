"""Where Steve reaches the services it pays or posts to -- OpenAI,
OpenRouter, Telegram and Brave: each one's own address, or, in a source tree
only, a fake on this computer named by an environment variable.

The exit walk (tests/unit/ui) and the end-to-end tests drive the real app
and backend, and must reach none of them, so the worker they start is
given the addresses of the tests' fakes (tests/fakes/). An
installed Steve that took an address from the environment would send the
user's key, and every conversation, wherever a stray variable pointed. So
an install never reads one, and a checkout takes only an address on this
computer; anything else is ignored, with a warning.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from core import localonly, paths

log = logging.getLogger(__name__)

OPENAI = "https://api.openai.com/v1"
OPENROUTER = "https://openrouter.ai/api"
TELEGRAM = "https://api.telegram.org"
BRAVE = "https://api.search.brave.com"


def _address(variable: str, own: str) -> str:
    given = os.environ.get(variable)
    if not given or not paths.is_source_tree():
        return own
    parts = urlsplit(given)
    if parts.scheme != "http" or parts.hostname not in localonly.LOOPBACK_HOSTS:
        log.warning("%s names %s, not this computer; using the service's own address",
                    variable, parts.hostname)
        return own
    return given.rstrip("/")


def openai() -> str:
    """OpenAI's API, as the SDK's `base_url`: no trailing slash."""
    return _address("STEVE_OPENAI_URL", OPENAI)


def openai_socket() -> str:
    """The same address for a WebSocket: wss for OpenAI's https, ws for a
    fake's http."""
    return "ws" + openai().removeprefix("http")


def openrouter() -> str:
    """OpenRouter's API, under which Jev's decisions (`core/decide.py`) and
    the key's credit (`agent/attention/sources/health.py`) are."""
    return _address("STEVE_OPENROUTER_URL", OPENROUTER)


def telegram() -> str:
    """Telegram's Bot API, before `/bot<token>/` (`channels/telegram.py`)."""
    return _address("STEVE_TELEGRAM_URL", TELEGRAM)


def brave() -> str:
    """Brave's search API, before its path (`core/websearch.py`)."""
    return _address("STEVE_BRAVE_URL", BRAVE)
