"""The desk's rehearsal runs from the night when the desk changed, or weekly,
in a child process, and says in the morning what passed.

The maintainer, 10 October 2026: "That way it can do voice tests overnight or while
I'm gone". The rig itself (devtools/rehearse/) loads models for minutes and
is never run here: tests/unit/conftest.py refuses its child process. These
pin the schedule, the conditions and the report.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from agent import issues
from agent.night import rehearsal
from agent.night.report import NightReport, digest


def _summary(*, passed=9, of=9, failed=(), sha="abc123", at=None, folder="20261010-030000"):
    out = rehearsal.out_dir() / folder
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps({
        "passed": passed, "of": of, "failed": list(failed), "live": False, "sha": sha,
        "at": (at or dt.datetime.now()).isoformat(timespec="seconds")}), encoding="utf-8")


def test_no_test_starts_the_desk_rehearsal():
    """tests/unit/conftest.py: it loads Whisper and Kokoro on the GPU."""
    with pytest.raises(RuntimeError, match="desk rehearsal"):
        rehearsal._run()


def test_an_install_never_rehearses_the_desk(monkeypatch):
    """An install ships no devtools/ and has no git: rehearsing there failed
    every night, and the morning briefing said so every morning."""
    monkeypatch.setattr(rehearsal.paths, "is_source_tree", lambda: False)
    monkeypatch.setattr(rehearsal, "_changed_since", lambda sha: pytest.fail("asked git"))

    assert rehearsal.due() is None


def test_it_is_due_when_never_run_when_the_desk_changed_and_weekly(monkeypatch):
    changed = {"yes": False}
    monkeypatch.setattr(rehearsal, "_changed_since", lambda sha: changed["yes"])

    assert rehearsal.due() == "never rehearsed"
    _summary()
    assert rehearsal.due() is None
    changed["yes"] = True
    assert rehearsal.due() == "the desk changed"
    changed["yes"] = False
    later = dt.datetime.now().timestamp() + rehearsal.EVERY_S + 1
    assert rehearsal.due(now=later) == "a week since the last"


async def test_a_run_lands_in_the_digest_and_each_failure_in_the_ledger(store, monkeypatch):
    monkeypatch.setattr(rehearsal, "_hands_free_on", lambda: False)
    monkeypatch.setattr(rehearsal, "_run", lambda: _summary(
        passed=8, of=9, failed=[{"scenario": "stop-inside-live", "checks": ["live wake"]}],
        folder="20261011-030000") or 1)
    report = NightReport(started_at="2026-10-11 03:00")

    spent = await rehearsal.rehearse(store, report)

    assert spent == 0.0
    [said] = report.rehearsed
    assert said == ("Desk rehearsal: 8 of 9 scenarios passed; failed: stop-inside-live "
                    "(live wake).")
    assert "The desk, rehearsed:" in digest(report)
    [row] = store.execute("SELECT kind, summary FROM issues").fetchall()
    assert row["kind"] == "rehearsal_failed" and "stop-inside-live" in row["summary"]
    assert "rehearsal_failed" in issues.KINDS


async def test_it_never_rehearses_while_hands_free_is_on(store, monkeypatch):
    """It shares the GPU and processor with a desk the user may be using, and a
    starved audio thread glitched their speakers on 9 October 2026."""
    ran = []
    monkeypatch.setattr(rehearsal, "_hands_free_on", lambda: True)
    monkeypatch.setattr(rehearsal, "_run", lambda: ran.append(1) or 0)
    report = NightReport(started_at="2026-10-11 03:00")

    await rehearsal.rehearse(store, report)

    assert ran == [] and report.rehearsed == ["Desk rehearsal skipped: hands-free was on."]


async def test_a_rehearsal_that_overran_or_left_no_verdict_says_so(store, monkeypatch):
    monkeypatch.setattr(rehearsal, "_hands_free_on", lambda: False)
    monkeypatch.setattr(rehearsal, "_run", lambda: -1)
    report = NightReport(started_at="2026-10-11 03:00")

    await rehearsal.rehearse(store, report)

    [said] = report.rehearsed
    assert "did not finish" in said and "ran out of time" in said
    assert store.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0


async def test_nothing_is_rehearsed_when_nothing_is_due(store, monkeypatch):
    monkeypatch.setattr(rehearsal, "_changed_since", lambda sha: False)
    monkeypatch.setattr(rehearsal, "_run", lambda: pytest.fail("rehearsed with nothing due"))
    _summary()
    report = NightReport(started_at="2026-10-11 03:00")

    await rehearsal.rehearse(store, report)

    assert report.rehearsed == []
