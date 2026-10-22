"""The spending caps, read and changed in a conversation.

At its month's cap, Steve answered "raise it to 25" by saying it could not:
no tool could change a cap, so the way out was the Spend page alone.
`spending_caps` reads each cap; `raise_spending_cap` and `lower_spending_cap`
change one, each behind a card. A raise spends money, so it takes the
high-risk yes, as a purchase does (`tier: high` in core/consent.yaml, which
goes by the tool's name, so they are two tools). A lowering takes a plain
yes: one the user never asked for, from a page or a mail that fooled the
model, would stop Steve for the month.

The change itself is `spend.set_cap`'s, in the frozen core: only the five
caps, each within its range. No tool here writes a knob
(tests/gate/test_who_may_approve.py), so none reaches the settings that
decide who approves. At the month's cap, when no model can be asked,
"RAISE 25" makes the raise's card in code (`caps.ask_to_raise`).
"""

from __future__ import annotations

from typing import Literal

from pydantic_ai import RunContext

from agent import caps
from agent.actions import act, executable
from agent.turn import Deps

Cap = Literal["month", "chat", "background", "night", "extensions"]


@executable(caps.RAISE)
async def _do_raise(*, cap: str, from_usd: float, to_usd: float) -> dict:
    return caps.change(cap, float(from_usd), float(to_usd))


@executable(caps.LOWER)
async def _do_lower(*, cap: str, from_usd: float, to_usd: float) -> dict:
    return caps.change(cap, float(from_usd), float(to_usd))


def register(tool) -> None:
    """Attach the spending tools. `tool` is `register_tools`'s filter."""

    @tool
    async def spending_caps(ctx: RunContext[Deps]) -> list[dict]:
        """Each spending cap: what it holds, its limit, what is spent and
        left, when it resets, and the range it may be set within."""
        return caps.caps_now(ctx.deps.conn)

    @tool
    async def raise_spending_cap(ctx: RunContext[Deps], cap: Cap, to_usd: float) -> dict:
        """Raise a spending cap to `to_usd`, with the user's yes and their
        code: "month" holds everything; "chat", "background" and
        "extensions" are a day's; "night" is a night's. Only when they ask
        for it, to the sum they name."""
        asked = caps.change_asked(caps.RAISE, cap, to_usd)
        if "error" in asked:
            return {"ok": False, "error": asked["error"]}
        return await act(ctx, caps.RAISE, asked["args"], asked["preview"])

    @tool
    async def lower_spending_cap(ctx: RunContext[Deps], cap: Cap, to_usd: float) -> dict:
        """Lower a spending cap to `to_usd`, with the user's yes. The caps
        are `raise_spending_cap`'s."""
        asked = caps.change_asked(caps.LOWER, cap, to_usd)
        if "error" in asked:
            return {"ok": False, "error": asked["error"]}
        return await act(ctx, caps.LOWER, asked["args"], asked["preview"])
