"""The control panel's knobs turn only as far as the frozen bounds allow.

The maintainer, 4 October 2026: the desktop app's control panel may change the
attention loop's limits, the briefing times and the day budgets
(core/knobs.py) -- settings that were constants in code, each capped by
tests/gate/test_attention_bounds.py. Moved into a file the panel writes,
those caps would no longer be checked by anything frozen unless this is:

- the bounds themselves sit inside the attention ceilings, and the night's
  messages stay fewer than the day's whatever is chosen;
- quiet hours last five to sixteen hours whatever is set, on either side
  of midnight, and are read the same way everywhere;
- a value out of range is refused, and nothing is written;
- whatever a file holds -- the highest the panel allows, or a tampered file
  -- the worker puts into force only values inside the bounds
  (`agent/worker/control.apply_knobs`, the mutable half, pinned from here);
- the panel's endpoints are the worker's, behind its local-only check;
- every switch (what Steve may use on this computer) is off until the user
  turns it on, and the browser needs the frozen policy's yes as well.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sys
from collections.abc import Iterator
from datetime import time, timedelta

import pytest

from agent.attention import settings
from agent.worker import accounts as account_routes
from agent.worker import control
from core import call_policy, knobs, modules, spend

# The ceilings of tests/gate/test_attention_bounds.py, for the knobs that
# reach those limits.
CEILINGS = {"MAX_DAY_PINGS": 12, "MAX_NIGHT_PINGS": 3}
# The day budgets of background work's parts (core/spend.PARTS), and how
# high each may ever go: the attention loop's are test_attention_bounds.py's.
BUDGET_CEILINGS = {"TRIAGE_BUDGET_USD": 0.25, "WORK_BUDGET_USD": 0.50,
                   "ROUTINES_DAY_USD": 2.00, "GOALS_DAY_USD": 2.00}


_PLAIN = (bool, int, float, str, time, timedelta)


def _module_values() -> dict[tuple[str, str], object]:
    """Every plain value Steve's own modules hold, the knobs in force among them."""
    return {(name, attr): value
            for name, module in list(sys.modules.items())
            if module is not None and name.split(".")[0] in ("agent", "core", "voice")
            for attr, value in list(vars(module).items())
            if not attr.startswith("__") and type(value) in _PLAIN}


@contextlib.contextmanager
def _put_back() -> Iterator[None]:
    """Every plain module value changed inside the block is set back after it."""
    before = _module_values()
    try:
        yield
    finally:
        for (name, attr), value in before.items():
            module = sys.modules.get(name)
            if module is not None and getattr(module, attr, value) != value:
                setattr(module, attr, value)


def _at_the_codes_values(folder) -> None:
    """Apply no knobs at all: every module `apply_knobs` writes is loaded, and
    holds the code's own value, before a snapshot is taken of them."""
    control.apply_knobs(folder / "no-knobs.json")


@pytest.fixture
def restore(tmp_path):
    """Whatever a test applies is put back: these are the live module values,
    and the next test in the same worker reads them. The values were once
    named here one by one, and the desk's live minutes were left out: the
    lowest knobs left the desk 0 minutes a day, and every live-call test the
    same worker ran later failed (17 October 2026). So whatever `apply_knobs`
    sets, wherever it lives, is found by looking."""
    _at_the_codes_values(tmp_path)
    with _put_back():
        yield


def _inside_the_ceilings(path) -> None:
    for name, ceiling in CEILINGS.items():
        value = getattr(settings, name)
        assert 0 < value <= ceiling, f"{name} = {value}"
    assert settings.MAX_NIGHT_PINGS < settings.MAX_DAY_PINGS
    assert all(seconds >= 60 for seconds in settings.CADENCE_S.values())
    # The budgets as core reads them where it asks (`spend.may_spend`).
    held = spend.limits(knobs.read(path))
    for name, ceiling in BUDGET_CEILINGS.items():
        assert 0 < held[name] <= ceiling, f"{name} = {held[name]}"
    _a_real_night(call_policy.QUIET_FROM, call_policy.QUIET_UNTIL)


# How long quiet hours may last, in hours, whatever the panel allows.
SHORTEST_NIGHT, LONGEST_NIGHT = 5, 16


def _a_real_night(start, end) -> None:
    """Quiet hours of at least five hours, so no setting turns the night into
    day -- nothing rings in them, and the night's few critical messages are
    counted across them -- and at most sixteen, so none turns the day into
    night. On either side of midnight: someone who works nights sleeps by
    day."""
    assert SHORTEST_NIGHT <= call_policy.night_hours(start, end) <= LONGEST_NIGHT, (start, end)


def test_the_bounds_sit_inside_the_attention_ceilings():
    for name, ceiling in CEILINGS.items():
        knob = knobs.KNOBS[name]
        assert 0 < float(knob.lo) <= float(knob.hi) <= ceiling, name
    assert knobs.KNOBS["MAX_NIGHT_PINGS"].hi < knobs.KNOBS["MAX_DAY_PINGS"].lo
    # How often an account is looked at is its own setting now, inside a
    # frozen floor: never more than once a minute.
    assert modules.EVERY_MIN[0] >= 1
    for name, ceiling in BUDGET_CEILINGS.items():
        assert 0 < float(knobs.KNOBS[name].lo) <= float(knobs.KNOBS[name].hi) <= ceiling, name
    assert SHORTEST_NIGHT <= knobs.QUIET_HOURS[0] <= knobs.QUIET_HOURS[1] <= LONGEST_NIGHT


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def test_any_quiet_hours_the_panel_takes_are_a_real_night(tmp_path):
    """Every pair of times on a half-hour grid: taken exactly when the night
    lasts five to sixteen hours, whichever side of midnight it falls."""
    for start in range(0, 24 * 60, 30):
        for end in range(0, 24 * 60, 30):
            pair = (time.fromisoformat(_clock(start)), time.fromisoformat(_clock(end)))
            hours = call_policy.night_hours(*pair)
            # A new panel for each pair: one file read back 2304 times took
            # twenty seconds, and the pair being written decides it either way.
            path = tmp_path / f"{start}-{end}.json"
            try:
                knobs.write({"QUIET_FROM": _clock(start), "QUIET_UNTIL": _clock(end)}, path)
            except knobs.BadKnob:
                assert not SHORTEST_NIGHT <= hours <= LONGEST_NIGHT, pair
            else:
                _a_real_night(*pair)


@pytest.mark.parametrize(("start", "end"), [
    ("22:00", "08:00"), ("08:00", "16:00"), ("00:30", "07:00"), ("18:00", "10:00"),
    ("23:00", "04:00"), ("13:00", "21:00"),
])
def test_quiet_hours_are_read_as_long_as_they_say_they_are(start, end):
    """The night is read the same way it is bounded: a day sleeper's 08:00
    to 16:00 was read as crossing midnight, and so covered the whole day."""
    pair = (time.fromisoformat(start), time.fromisoformat(end))
    quiet = sum(call_policy.is_quiet(time.fromisoformat(_clock(m)), *pair)
                for m in range(24 * 60))
    assert quiet == call_policy.night_hours(*pair) * 60


@pytest.mark.parametrize("name,given", [
    ("MAX_DAY_PINGS", 1000), ("MAX_DAY_PINGS", 0), ("MAX_NIGHT_PINGS", 5),
    ("MAX_DAY_PINGS", True), ("MAX_DAY_PINGS", 7.5), ("TRIAGE_BUDGET_USD", 3),
    ("MORNING", "7:30"), ("MORNING", "8am"), ("DESK_LIVE_MIN", -1),
    ("GOALS_DAY_USD", 50), ("WORK_BUDGET_USD", float("nan")),
    ("QUIET_FROM", "12:00"), ("QUIET_UNTIL", "15:00"), ("QUIET_FROM", "24:00"),
    ("QUIET_FROM", "04:00"),
    ("FOLLOW_UP_AFTER", "always"), ("FOLLOW_UP_AFTER", 1), ("UNITS", "imperial"),
    ("MEETING_RESEARCH", "yes"), ("MEETING_RESEARCH", True),
    ("agent_initiated", True), ("auto_switch", True),
])
def test_a_value_out_of_range_is_refused_and_nothing_is_written(tmp_path, name, given):
    path = tmp_path / "knobs.json"
    knobs.write({"MAX_DAY_PINGS": 10}, path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(knobs.BadKnob):
        knobs.write({"EVENING": "19:00", name: given}, path)
    assert path.read_text(encoding="utf-8") == before


def test_the_highest_the_panel_allows_stays_inside_the_ceilings(tmp_path, restore):
    path = tmp_path / "knobs.json"
    others = {name: knob for name, knob in knobs.KNOBS.items()
              if name not in ("QUIET_FROM", "QUIET_UNTIL")}
    # With the longest night the panel takes, and the shortest.
    knobs.write({**{name: knob.hi for name, knob in others.items()},
                 "QUIET_FROM": "18:00", "QUIET_UNTIL": "10:00"}, path)
    control.apply_knobs(path)
    _inside_the_ceilings(path)
    knobs.write({**{name: knob.lo for name, knob in others.items()},
                 "QUIET_FROM": "23:00", "QUIET_UNTIL": "04:00"}, path)
    control.apply_knobs(path)
    _inside_the_ceilings(path)


def test_knobs_applied_in_a_test_leave_every_module_value_as_they_found_it(tmp_path):
    """The next test in the same worker reads these values. Left at the
    lowest, the desk had 0 live minutes a day, and every live-call test the
    worker ran after it failed: the desk never opened one."""
    from agent.worker import desk

    path = tmp_path / "knobs.json"
    others = {name: knob for name, knob in knobs.KNOBS.items()
              if name not in ("QUIET_FROM", "QUIET_UNTIL")}
    knobs.write({**{name: knob.lo for name, knob in others.items()},
                 "QUIET_FROM": "23:00", "QUIET_UNTIL": "04:00"}, path)
    _at_the_codes_values(tmp_path)
    before, minutes = _module_values(), desk.LIVE_DAY_MIN
    with _put_back():
        control.apply_knobs(path)
        assert desk.LIVE_DAY_MIN == 0 and _module_values() != before
    assert desk.LIVE_DAY_MIN == minutes > 0 and _module_values() == before


def test_a_tampered_file_cannot_push_a_knob_past_its_bounds(tmp_path, restore):
    path = tmp_path / "knobs.json"
    path.write_text(json.dumps({
        "MAX_DAY_PINGS": 1000, "MAX_NIGHT_PINGS": 99, "MORNING": "3am",
        "QUIET_FROM": "12:00", "QUIET_UNTIL": "13:00",
        "GMAIL_EVERY_MIN": 0, "OUTLOOK_EVERY_MIN": -5, "TRIAGE_BUDGET_USD": 5,
        "WORK_BUDGET_USD": "lots", "GOALS_DAY_USD": 100, "ROUTINES_DAY_USD": None,
        "agent_initiated": True,
    }), encoding="utf-8")

    applied = control.apply_knobs(path)

    _inside_the_ceilings(path)
    assert applied == control.defaults()


def test_the_panels_endpoints_are_the_workers_behind_its_local_only_check():
    import agent.server as server
    from core import localonly

    paths = {getattr(route, "path", None) for route in server.app.routes}
    assert {route.path for route in control.ROUTES} <= paths
    assert {route.path for route in account_routes.ROUTES} <= paths
    assert server.app.user_middleware[0].cls is localonly.LocalOnly


def test_every_switch_is_off_until_the_user_turns_it_on(tmp_path):
    """A fresh install started the browser, opened Outlook, built a Docker
    image and armed a wake timer, none of it asked for. The default is
    here, in frozen code, and a file that cannot be read is off too."""
    path = tmp_path / "knobs.json"
    assert {"BROWSER", "BACKGROUND_BROWSING", "RUN_CODE",
            "WAKE_TIMER", "KEEP_AWAKE"} <= knobs.SWITCHES
    assert not any(knobs.on(name, path) for name in knobs.SWITCHES)
    assert {name: control.defaults()[name] for name in knobs.SWITCHES} == dict.fromkeys(
        knobs.SWITCHES, "off")

    path.write_text("{not json", encoding="utf-8")
    assert not any(knobs.on(name, path) for name in knobs.SWITCHES)
    path.write_text(json.dumps(dict.fromkeys(knobs.SWITCHES, True)), encoding="utf-8")
    assert not any(knobs.on(name, path) for name in knobs.SWITCHES), "only 'on' is on"

    knobs.write({"RUN_CODE": "on"}, path)
    assert [name for name in knobs.SWITCHES if knobs.on(name, path)] == ["RUN_CODE"]


def test_the_browser_runs_only_with_the_policys_yes_and_the_users(monkeypatch):
    """The frozen policy is the ceiling and the user's switch the choice:
    the stricter of the two wins, and background browsing needs the
    browser on as well as its own switch."""
    import core.browser
    from core import policy

    real = policy.load_policy()

    def with_ceiling(enabled: bool, background: bool):
        browser = policy.BrowserConfig(enabled=enabled, background=background)
        monkeypatch.setattr(policy, "load_policy",
                            lambda *a, **k: dataclasses.replace(real, browser=browser))

    def switches(**on):
        knobs.write({name: "on" if on.get(name) else "off"
                     for name in ("BROWSER", "BACKGROUND_BROWSING")})
        return core.browser.enabled(), core.browser.background()

    with_ceiling(True, True)
    assert switches() == (False, False)
    assert switches(BACKGROUND_BROWSING=True) == (False, False)
    assert switches(BROWSER=True) == (True, False)
    assert switches(BROWSER=True, BACKGROUND_BROWSING=True) == (True, True)
    with_ceiling(False, True)
    assert switches(BROWSER=True, BACKGROUND_BROWSING=True) == (False, False)
    with_ceiling(True, False)
    assert switches(BROWSER=True, BACKGROUND_BROWSING=True) == (True, False)
