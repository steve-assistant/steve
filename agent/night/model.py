"""The one way the night's blocks ask a model anything, and what they tell it.

One function, `ask`, on the bulk tier, so the tests have one seam to patch
(`agent.night.model.ask`) and every block reaches the model through it --
called as `model.ask(...)`, never imported by name, or a block would keep
the real one past the patch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent import reflect
from agent.models import Role


def context(conn: sqlite3.Connection) -> str:
    """What the agent knows about itself, offered as a hint and not a brief."""
    scores = reflect.averages(conn)
    weakest = min(scores, key=lambda k: scores[k]) if scores else None
    rows = conn.execute(
        "SELECT content FROM messages WHERE role = 'user' ORDER BY id DESC LIMIT 12"
    ).fetchall()
    recent = "; ".join((r["content"] or "")[:80] for r in rows)

    lines = []
    if scores:
        lines.append("Your recent rubric scores: " + ", ".join(
            f"{k} {v}" for k, v in sorted(scores.items())
        ))
        if weakest:
            lines.append(f"Your weakest dimension lately is '{weakest}'.")
    if recent:
        lines.append(f"Things the user asked about recently: {recent}")
    return "\n".join(lines)


async def ask(output_type, prompt: str, system: str):
    from agent.llm import ask_once

    output, record = await ask_once(Role.BULK, system, prompt, output_type=output_type,
                                    retries=2)
    return output, getattr(record, "cost_usd", 0.0) or 0.0


PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def prompt(name: str) -> str:
    """Load a night prompt from `agent/prompts/`.

    Kept in files rather than in this module because of where the policy line
    falls: `agent/night/` needs a human to merge, `agent/prompts/**` does
    not. The loop's taste in what to build is exactly the thing it should be
    able to sharpen on its own -- and the three orphan modules it invented
    early on are the evidence that it needed to.
    """
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
