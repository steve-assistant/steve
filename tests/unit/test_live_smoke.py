"""The live smoke (devtools/live_smoke.py), run on fake steps.

It spends real money, so the guarantees around it are tested without it:
it stops at its own limit, it never prints what a refused key's message
carries, a step without its key is skipped and the rest still run, and
nothing runs without the maintainer's yes.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from pydantic_ai.exceptions import ModelHTTPError

from core import db, spend
from devtools import live_smoke


@pytest.fixture
def conn():
    with contextlib.closing(db.connect()) as conn:
        db.migrate(conn)
        yield conn


def _billing(usd: float, said: str = "done"):
    async def step(conn) -> str:
        spend.bill(conn, usd, model="test/fake", unit=spend.current())
        return said
    return step


def test_the_smoke_stops_before_a_step_once_it_has_spent_its_25_cents(conn):
    """A smoke that kept going after a price changed tenfold would spend
    whatever the steps cost; its own count stops it, below any cap."""
    said: list[str] = []
    steps = tuple((f"step {n}", _billing(0.10)) for n in range(5))
    done = asyncio.run(live_smoke.run(conn, steps, say=said.append))

    assert [s.name for s in done] == ["step 0", "step 1", "step 2"]
    assert [round(s.usd, 2) for s in done] == [0.10, 0.10, 0.10]
    assert said[-1].startswith("stopped before step 3: $0.3000 spent")
    assert spend.spent(conn, "2000-01-01 00:00:00", category="background") == pytest.approx(0.30)


def test_a_failed_step_says_the_providers_status_and_code_and_never_its_message(conn):
    """A key with no credit is what finding 28 waits to see answered; the
    message a refused key comes back with names the key's last characters."""
    async def broke(conn) -> str:
        raise ModelHTTPError(429, "test/fake", body={
            "code": "insufficient_quota", "type": "insufficient_quota",
            "message": "Incorrect API key provided: sk-proj-abcd1234"})

    said: list[str] = []
    done = asyncio.run(live_smoke.run(conn, (("a turn", broke), ("next", _billing(0))),
                                      say=said.append))

    assert [s.outcome for s in done] == ["failed", "ok"]
    assert done[0].said == ("ModelHTTPError: status 429, code insufficient_quota;"
                            " the key's balance is spent")
    assert not any("sk-proj" in line or "Incorrect" in line for line in said)


def test_a_step_without_its_key_is_skipped_and_the_rest_still_run(conn):
    async def no_key(conn) -> str:
        raise live_smoke.Skipped("no OpenRouter key kept")

    done = asyncio.run(live_smoke.run(conn, (("Jev's decision", no_key), ("next", _billing(0))),
                                      say=lambda _: None))

    assert [(s.outcome, s.said) for s in done] == [
        ("skipped", "no OpenRouter key kept"), ("ok", "done")]


def test_without_yes_it_asks_first_and_a_no_spends_nothing(monkeypatch, capsys):
    """It is the maintainer's to run, and never without their word."""
    async def spends(*args, **kwargs):
        raise AssertionError("ran without a yes")

    monkeypatch.setattr(live_smoke, "run", spends)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert live_smoke.main([]) == 1
    assert "Nothing spent." in capsys.readouterr().out
