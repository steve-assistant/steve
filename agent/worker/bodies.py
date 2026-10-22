"""A route's JSON body, its numbers, and its refusal.

Nine route modules each defined the same two helpers, word for word, and
two more read their bodies by hand: `await request.json()` with nothing
around it, then `int(body["id"])`. A body that was not JSON, JSON that was
not an object, or an id that was not a number raised inside the route, and
the app was answered 500, an error of Steve's, for a request Steve should
have refused (400). Every route reads its body and numbers here.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse


async def body(request: Request) -> dict[str, Any]:
    """The request's JSON object; {} for no body, one that is not JSON, or
    JSON that is not an object. The route then refuses what it misses."""
    try:
        given = await request.json()
    except ValueError:
        return {}
    return given if isinstance(given, dict) else {}


def whole(value: Any) -> int | None:
    """`value` as a whole number, or None for anything that is not one, for
    the route to refuse. True and False are not numbers here, though Python
    counts them."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def bad(error: str, status: int = 400, **more: Any) -> JSONResponse:
    """A refusal, as the app reads one: `ok` false, and why."""
    return JSONResponse({"ok": False, "error": error, **more}, status_code=status)
