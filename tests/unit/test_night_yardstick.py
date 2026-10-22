"""The yardstick runs once a week from the night, in a child process, and
its numbers reach their morning digest in a line.

The runner and its scoring are pinned elsewhere (test_autonomy_eval.py,
tests/gate/test_yardstick.py). These pin the schedule and the report: due a
week after the last run, a run that overruns or fails is said as such, and
no test ever starts one.
"""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from agent import autonomy_eval
from agent.night import yardstick
from agent.night.report import NightReport, digest


def _report(**summary) -> dict:
    row = {"id": "s1", "kind": "mail", "done": True, "attacked": True, "exposed": True,
           "turned": False, "landed": False, "critic_on_attack": ["card"],
           "critic_needless": 0, "critic_reviewed": 2, "screen_planted_seen": 1,
           "screen_planted_flagged": 1, "screen_clean_seen": 0, "screen_clean_flagged": 0,
           "error": None}
    return {"at": dt.datetime.now().isoformat(timespec="seconds"), "cost_usd": 0.05,
            "repeat": 1, "rows": [row], "summary": summary}


def _write(report: dict) -> None:
    autonomy_eval.reports_dir().mkdir(parents=True, exist_ok=True)
    (autonomy_eval.reports_dir() / f"{time.time_ns()}.json").write_text(json.dumps(report),
                                                                   encoding="utf-8")


def test_no_test_starts_the_yardstick():
    """tests/unit/conftest.py: it plays scenarios on the live models."""
    with pytest.raises(RuntimeError, match="conftest|yardstick"):
        yardstick._run()


def test_it_is_due_with_no_report_and_not_again_for_a_week():
    assert yardstick.due()
    _write(_report())
    assert not yardstick.due()
    assert yardstick.due(now=time.time() + yardstick.EVERY_S + 1)


def test_an_installed_steve_never_runs_the_yardstick(monkeypatch):
    """Its scenarios and its world (tests/modules/world) are under tests/,
    which an installed Steve does not have: a run there could only fail,
    and the morning digest say so every week."""
    monkeypatch.setattr("core.paths.is_source_tree", lambda: False)

    assert not yardstick.due()


async def test_a_run_lands_in_the_digest_as_a_line(monkeypatch):
    monkeypatch.setattr(yardstick, "_run", lambda: _write(_report()) or 0)
    report = NightReport(started_at="2026-10-05 03:00")

    spent = await yardstick.measure(report)

    assert spent == pytest.approx(0.05)
    [said] = report.measured
    assert "tasks done 1/1" in said and "obeyed 0" in said and "stopped 1/1" in said
    assert "the week's yardstick" in digest(report)


async def test_a_run_that_overran_or_left_no_report_is_said_as_such(monkeypatch):
    monkeypatch.setattr(yardstick, "_run", lambda: -1)
    report = NightReport(started_at="2026-10-05 03:00")

    assert await yardstick.measure(report) == 0.0
    assert "ran out of time" in report.measured[0]


async def test_a_run_not_due_is_not_started(monkeypatch):
    _write(_report())
    started: list = []
    monkeypatch.setattr(yardstick, "_run", lambda: started.append(1) or 0)

    assert await yardstick.measure(NightReport(started_at="x")) == 0.0
    assert started == []


def test_the_weekly_run_builds_no_modules_and_the_yardstick_takes_its_command(
        monkeypatch, capsys):
    """Module builds joined the yardstick's run in step 10, on a budget of
    their own. Weekly, unasked, they would spend what no run has measured
    yet; the maintainer decides once one has."""
    command = yardstick.command()
    assert command[command.index("--build-budget") + 1] == "0"

    # The flags parse, as the child would read them; `report` runs nothing.
    monkeypatch.setattr("sys.argv", ["autonomy_eval", "report", *command[4:]])
    autonomy_eval._main()
    assert capsys.readouterr().out.strip() == "null"
