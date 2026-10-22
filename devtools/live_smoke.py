"""The live smoke: each of Steve's paths to a provider, once, with a real key.

The suite reaches no provider (tests/gate/fences.py): a model renamed, an
endpoint moved or a reply's shape changed would be found by the first user
to meet it. This runs each path once, through Steve's own code, so the
bills go where Steve's go and the caps apply as they do to Steve:

1. the key, checked as setup checks it (`setup.check_key`), at no cost;
2. a turn, through the conversation's model (`provider.model_for`);
3. an escalation, through the model a turn goes to when reasoning failed;
4. an embedding (`tools/memory.embed`);
5. a web search (`core/websearch.search`);
6. Jev's one decision (`core/decide.ask`), when an OpenRouter key is kept.

It is the maintainer's to run, by hand, and never in CI: it spends real
money. It stops at $0.25 on its own count, what the ledger shows billed
since it began (`spend.spent`), below any cap the knobs allow, and each
reply is held to `MOST_TOKENS`. A step that fails says the provider's
status and error code, never its message, which can carry part of the
key: run once with a key that has no credit, that is how the real one
answers (finding 28).

    python -m devtools.live_smoke          # says what it will do, and asks
    python -m devtools.live_smoke --yes
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

LIMIT_USD = 0.25
# Room for the escalation's reasoning as well as its word; at its price,
# about two cents at most.
MOST_TOKENS = 2048
ASK = "Reply with the one word: ready."


class Skipped(Exception):
    """A step with nothing to run against: no key kept for its provider."""


@dataclass
class Step:
    name: str
    outcome: str  # "ok", "failed" or "skipped"
    said: str
    usd: float


Run = Callable[[sqlite3.Connection], Awaitable[str]]


def _failure(exc: BaseException) -> str:
    """What a failed step says: the provider's status and code, or the
    error's kind. Never the message: a refused key's names its last
    characters."""
    from pydantic_ai.exceptions import ModelHTTPError

    from core import spend

    if isinstance(exc, spend.Refused):
        return f"a cap stopped it ({exc.cap})"
    if isinstance(exc, ModelHTTPError):
        body = exc.body if isinstance(exc.body, dict) else {}
        status, code = exc.status_code, body.get("code") or body.get("type")
    else:
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
    credit = "; the key's balance is spent" if spend.out_of_credit(exc) else ""
    if status is None and code is None:
        return f"{type(exc).__name__}{credit}"
    return f"{type(exc).__name__}: status {status}, code {code}{credit}"


async def _key(conn: sqlite3.Connection) -> str:
    from agent.worker import setup
    from core import secrets

    key = secrets.get("OPENAI_API_KEY")
    if not key:
        raise Skipped("no OpenAI key kept: set one in the app's setup")
    checked = await setup.check_key("openai", key)
    if not checked.get("ok"):
        raise RuntimeError(f"the key was refused ({checked.get('why')})")
    return "the key works"


async def _answer(role_name: str) -> str:
    from pydantic_ai import Agent

    from agent import models, provider

    role = models.Role(role_name)
    got = await Agent(provider.model_for(role)).run(
        ASK, model_settings={"max_tokens": MOST_TOKENS})
    return f"{models.MODELS[role].slug} said {got.output.strip()[:40]!r}"


async def _turn(conn: sqlite3.Connection) -> str:
    return await _answer("converse")


async def _escalation(conn: sqlite3.Connection) -> str:
    return await _answer("escalate")


async def _embedding(conn: sqlite3.Connection) -> str:
    from tools import memory

    [vector] = memory.embed(["The live smoke's one embedding."])
    return f"a vector of {len(vector)} numbers"


async def _search(conn: sqlite3.Connection) -> str:
    from core import websearch

    found = await websearch.search("When did Windows 11 come out?", count=1)
    if not found:
        raise RuntimeError("the search found nothing")
    return f"found {found[0].url}"


async def _jev(conn: sqlite3.Connection) -> str:
    from core import decide, secrets

    if not secrets.get("OPENROUTER_API_KEY"):
        raise Skipped("no OpenRouter key kept")
    # On this thread: the bill is written on `conn`, which belongs to it.
    asked = decide.Predicate("weather", "The text is about the weather.")
    got = decide.ask(conn, "Rain is likely after three this afternoon.", [asked])
    if got.error:
        raise RuntimeError(f"Jev did not answer ({got.error})")
    return f"Jev put {got.answers['weather'].probability:.2f} on it being about the weather"


STEPS: tuple[tuple[str, Run], ...] = (
    ("the key", _key),
    ("a turn", _turn),
    ("an escalation", _escalation),
    ("an embedding", _embedding),
    ("a web search", _search),
    ("Jev's decision", _jev),
)


async def run(conn: sqlite3.Connection, steps: tuple[tuple[str, Run], ...] = STEPS, *,
              say: Callable[[str], None] = print) -> list[Step]:
    """Each step once, in order, billed as background work to `conn`'s
    ledger, until what it has spent reaches `LIMIT_USD`."""
    from agent import sqltime
    from core import spend

    began = sqltime.stamp(datetime.now(UTC))
    done: list[Step] = []
    for name, step in steps:
        before = spend.spent(conn, began)
        if before >= LIMIT_USD:
            say(f"stopped before {name}: ${before:.4f} spent, at the smoke's ${LIMIT_USD:.2f}")
            break
        try:
            with spend.spending("background", conn=conn):
                outcome, said = "ok", await step(conn)
        except Skipped as exc:
            outcome, said = "skipped", str(exc)
        except Exception as exc:  # noqa: BLE001 - each step is reported, and the next run
            outcome, said = "failed", _failure(exc)
        usd = spend.spent(conn, began) - before
        done.append(Step(name, outcome, said, usd))
        say(f"{outcome:<8}{name}: {said} (${usd:.4f})")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m devtools.live_smoke",
        description=f"Each of Steve's paths to a provider, once, with the kept keys: "
                    f"real money, up to ${LIMIT_USD:.2f}.")
    parser.add_argument("--yes", action="store_true", help="spend without asking first")
    args = parser.parse_args(argv)

    from core import db, paths

    print(f"Steve's paths to its providers, once each, billed to {paths.db_path()}:"
          f" up to ${LIMIT_USD:.2f} of real money.")
    if not args.yes and input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing spent.")
        return 1
    with contextlib.closing(db.connect()) as conn:
        db.migrate(conn)
        steps = asyncio.run(run(conn))
    failed = [s for s in steps if s.outcome == "failed"]
    print(f"{sum(s.outcome == 'ok' for s in steps)} ok, {len(failed)} failed,"
          f" {sum(s.outcome == 'skipped' for s in steps)} skipped of {len(STEPS)};"
          f" ${sum(s.usd for s in steps):.4f} spent")
    return 1 if failed or len(steps) < len(STEPS) else 0


if __name__ == "__main__":
    raise SystemExit(main())
