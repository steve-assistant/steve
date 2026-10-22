"""The settings the user may turn from the control panel, and how far.

Until October 2026 every one of these was a constant in code
(`agent/attention/settings.py`, the routines' and goals' day budgets), and
changing one meant an edit, a test run and a swap. The maintainer, 4 October: a
control panel on the desktop app, with the autonomy knobs in it.

The values live in a file of theirs (`data/knobs.json`), written only by the
panel. What keeps a knob a knob rather than a hole is here, frozen: which
settings exist, and the bounds of each. The mutable worker applies them
(`agent/worker/control.apply_knobs`), and the frozen gate
(tests/gate/test_knobs.py) pins that whatever is in the file -- a value the
panel wrote, or one a tampered file holds -- comes out inside these bounds,
and that the bounds sit inside the ceilings of tests/gate/test_attention_bounds.py.
So nothing the self-upgrade loop can change lets a knob send twelve hundred
messages a day; the most it can do is what the user could, from the panel.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path

from core import call_policy, language, paths, settingsfile

log = logging.getLogger("knobs")

# Each change of the file's shape, oldest first (core/settingsfile.py).
STEPS = (settingsfile.stamped,)


def knobs_path() -> Path:
    return paths.data_dir() / "knobs.json"


@dataclass(frozen=True)
class Knob:
    # "int" | "usd" | "minutes" | "clock" | "choice" | "language" | "region"
    kind: str
    lo: float | str
    hi: float | str
    says: str
    group: str
    # A choice's values and what the panel calls each, least to most: `lo`
    # and `hi` are its first and last. A language or a region has no range:
    # both are what Steve falls back to.
    choices: tuple[tuple[str, str], ...] = ()


# Each knob's range. The ceilings of the attention loop's limits are those
# of tests/gate/test_attention_bounds.py; the day's pings start at four so
# the night's (critical only, at most three) are always fewer.
KNOBS: dict[str, Knob] = {
    "MAX_DAY_PINGS": Knob("int", 4, 12, "Messages a day it may send you unasked", "attention"),
    "MAX_NIGHT_PINGS": Knob("int", 1, 3, "Messages in quiet hours, critical only", "attention"),
    # Quiet hours (`core/call_policy.within_quiet_hours`): nothing rings, and
    # only a critical item may message. They were 22:00 to 08:00, fixed in
    # code, which suits one sleeper; then a night that had to start in the
    # evening, which suits nobody who works nights. Any times now, on
    # either side of midnight, as long as they last `QUIET_HOURS`. The
    # briefings likewise: any time outside quiet hours
    # (`agent/worker/control.set_knobs`).
    "QUIET_FROM": Knob("clock", "00:00", "23:59", "Quiet hours start", "attention"),
    "QUIET_UNTIL": Knob("clock", "00:00", "23:59", "Quiet hours end", "attention"),
    "MORNING": Knob("clock", "00:00", "23:59", "Morning briefing", "attention"),
    "EVENING": Knob("clock", "00:00", "23:59", "Evening look-ahead", "attention"),
    "BEFORE_LEAD_MIN": Knob("minutes", 15, 120, "Minutes before an event its heads-up arrives",
                            "attention"),
    # Looking up the people in a meeting and their organisation on the web,
    # for its heads-up (`agent/attention/prepare`). It was done for every
    # meeting with anyone in it, which gave a school pickup a dossier and
    # sent the guests' names to a search engine. Off unless chosen.
    "MEETING_RESEARCH": Knob("choice", "off", "on", "Before a meeting, look up who is in it",
                             "attention",
                             choices=(("off", "No: a heads-up from my own mail and calendar"),
                                      ("on", "Yes: who they are and their organisation's"
                                             " news, from the web"))),
    # Asking how a meeting went, once it is over (the `after` source). It
    # used to ask after the kinds of meeting one life had in it -- a coffee
    # chat, an interview, a recruiting event -- named in the judge's prompt.
    # Now the user chooses: never, after every meeting, or after those a
    # decision model judges worth a follow-up for them.
    "FOLLOW_UP_AFTER": Knob("choice", "off", "every", "Ask how a meeting went", "attention",
                            choices=(("off", "Never"),
                                     ("judged", "When it is worth asking"),
                                     ("every", "After every meeting"))),
    # The weather's units (`sources.weather.units`). They were °F and
    # inches, for everyone.
    "UNITS": Knob("choice", "auto", "us", "Units for the weather", "attention",
                  choices=(("auto", "Those of where you live"),
                           ("metric", "°C and millimetres"),
                           ("us", "°F and inches"))),
    # The language Steve writes in and the region it reads the news, the
    # web and searches for (`core/language`): Windows' own until the user
    # chooses, so setup doesn't ask. Both were US English, fixed.
    "LANGUAGE": Knob("language", *language.FALLBACK[:1] * 2, "The language Steve writes in",
                     "language"),
    "REGION": Knob("region", *language.FALLBACK[1:] * 2, "Your region, for news and searches",
                   "language"),
    "TRIAGE_BUDGET_USD": Knob("usd", 0.02, 0.25, "Judging what it notices, per day", "budgets"),
    "WORK_BUDGET_USD": Knob("usd", 0.05, 0.50, "Preparing things unasked, per day", "budgets"),
    "ROUTINES_DAY_USD": Knob("usd", 0.10, 2.00, "All routines' runs, per day", "budgets"),
    "GOALS_DAY_USD": Knob("usd", 0.10, 2.00, "All goals' sessions, per day", "budgets"),
    # Live conversation at the desk (voice/desk_call.py), $0.05 a minute:
    # at the top, $6 a day. Zero keeps every desk request one at a time.
    "DESK_LIVE_MIN": Knob("minutes", 0, 120, "Live conversation at the desk, minutes a day",
                          "budgets"),
    # What Steve may spend on models (core/spend.py, which holds their
    # defaults and asks before every call). Before step 3 nothing capped a
    # conversation or a month. Zero, where allowed, turns that spending off.
    "MONTH_USD": Knob("usd", 5, 200, "Everything, per month", "spend"),
    "CHAT_DAY_USD": Knob("usd", 0.50, 10, "Conversations, per day", "spend"),
    "BACKGROUND_DAY_USD": Knob("usd", 0.25, 5, "Background work, per day", "spend"),
    # No higher than the frozen policy's `coder.night_budget_usd`.
    "NIGHT_USD": Knob("usd", 0, 2.00, "Overnight work, per night", "spend"),
    "EXTENSIONS_DAY_USD": Knob("usd", 0, 2, "Extensions, per day", "spend"),
    # With $1 of the month left, one message asking whether to raise the cap
    # (core/spend.py `ASK_LEFT_USD`). Before 19 October 2026 the first word
    # was the stop itself, with everything already paused.
    "MONTH_ASK": Knob("choice", "no", "yes", "Ask before the month's cap is reached", "spend",
                      choices=(("no", "No: say so once it is reached"),
                               ("yes", "Yes: with $1 left, ask whether to raise it"))),
}

# The switches: what Steve may use on this computer, each off until the user
# turns it on. A fresh install used to start the browser and its background
# workers (on in the frozen policy, for the one user who had checked them),
# open Outlook through COM and read its mail, build a Docker image and arm a
# wake timer in Task Scheduler -- none of it asked for. Off is the default
# here, frozen, and not a value in the code the night may change: a switch
# the agent could default on would be a choice the user never made. Where
# the frozen policy also has a say (the browser), both must allow it.
SWITCH = (("off", "Off"), ("on", "On"))


def _switch(says: str) -> Knob:
    return Knob("choice", "off", "on", says, "features", choices=SWITCH)


KNOBS |= {
    # core/browser: the sandboxed browser, in Docker, for their conversations.
    "BROWSER": _switch("The sandboxed browser (needs Docker)"),
    # And the clean-room browsing workers a background task may start
    # (agent/workers.py): only with the browser on as well.
    "BACKGROUND_BROWSING": _switch("Browsing in background tasks"),
    # `run_code` (tools/code_exec.py), and the image with pandas in it that
    # it runs in, built once it is on.
    "RUN_CODE": _switch("Running code for analysis (needs Docker)"),
    # A scheduled task that wakes the computer for a reminder (agent/wake.py).
    "WAKE_TIMER": _switch("Wake the computer for reminders"),
    # A power request held while the computer runs on mains power, so it
    # does not sleep while idle (agent/awake.py).
    "KEEP_AWAKE": _switch("Keep the computer awake while it is plugged in"),
}
# The app signing out after 30 minutes with no input (ui/src-tauri/src/
# signin.rs, told by the keep-alive's answer). Always on until 19 October
# 2026, when the maintainer asked for it off unless turned on; Windows'
# lock, sleep and quitting the app still end a session. A switch, so its
# default is the frozen off. Shown under Settings' signing in, not with
# the features.
KNOBS |= {
    "SIGN_OUT_IDLE": Knob("choice", "off", "on", "Sign out after 30 minutes away", "signin",
                          choices=SWITCH),
}
SWITCHES = frozenset(name for name, knob in KNOBS.items() if knob.choices == SWITCH)


def _approving(says: str) -> Knob:
    return Knob("choice", "never", "high", says, "approvals",
                choices=(("never", "Never: answer cards in the app"),
                         ("ordinary", "Everyday cards; spending money in the app"),
                         ("high", "Spending money too, with your authenticator's code")))


# What a yes from each channel may approve (core/monitor.py `ceiling`): the
# user may lower it, and core takes the lower of this and its own table, so
# no value here raises one. Unset, a channel has the table's.
KNOBS |= {
    "APPROVE_ON_SIGNAL": _approving("Answering cards on Signal"),
    "APPROVE_ON_TELEGRAM": _approving("Answering cards on Telegram"),
    "APPROVE_BY_VOICE": _approving("Answering cards by voice at the desk"),
}

_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# How long quiet hours may last, in hours: at least five, so no setting
# turns the night into day and the night's few critical messages are
# counted across a real night; at most sixteen, so none turns the day into
# night.
QUIET_HOURS = (5, 16)


class BadKnob(ValueError):
    """A value a knob cannot take, and why."""


def _night(values: dict) -> None:
    """BadKnob unless the quiet hours in `values` -- or the code's, for a
    half not set -- last `QUIET_HOURS`."""
    start = str(values.get("QUIET_FROM") or call_policy.QUIET_DEFAULT[0].strftime("%H:%M"))
    end = str(values.get("QUIET_UNTIL") or call_policy.QUIET_DEFAULT[1].strftime("%H:%M"))
    hours = call_policy.night_hours(time.fromisoformat(start), time.fromisoformat(end))
    lo, hi = QUIET_HOURS
    if not lo <= hours <= hi:
        raise BadKnob(f"Quiet hours: between {lo} and {hi} hours long, and {start} to {end}"
                      f" is {hours:g}")


def value(name: str, raw) -> float | int | str:
    """`raw` as knob `name` holds it, or BadKnob. Never clamps: a value out
    of range is refused, so the panel says so instead of quietly saving
    something else."""
    knob = KNOBS.get(name)
    if knob is None:
        raise BadKnob(f"there is no setting {name!r}")
    if knob.kind == "choice":
        allowed = [choice for choice, _ in knob.choices]
        if raw not in allowed:
            raise BadKnob(f"{knob.says}: one of {', '.join(allowed)}, not {raw!r}")
        return raw
    if knob.kind in ("language", "region"):
        pattern = language.LANGUAGE if knob.kind == "language" else language.REGION
        if not isinstance(raw, str) or not pattern.fullmatch(raw):
            raise BadKnob(f"{knob.says}: a tag like {knob.lo}, not {raw!r}")
        return raw
    if knob.kind == "clock":
        text = str(raw).strip()
        if not _CLOCK.match(text):
            raise BadKnob(f"{knob.says}: a time like 08:00, not {raw!r}")
        if not str(knob.lo) <= text <= str(knob.hi):
            raise BadKnob(f"{knob.says}: between {knob.lo} and {knob.hi}")
        return text
    if isinstance(raw, bool):
        raise BadKnob(f"{knob.says}: a number, not {raw!r}")
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise BadKnob(f"{knob.says}: a number, not {raw!r}") from exc
    if number != number or not float(knob.lo) <= number <= float(knob.hi):
        raise BadKnob(f"{knob.says}: between {knob.lo:g} and {knob.hi:g}")
    if knob.kind in ("int", "minutes"):
        if number != int(number):
            raise BadKnob(f"{knob.says}: a whole number")
        return int(number)
    return round(number, 2)


def read(path: Path | None = None) -> dict:
    """What the user set, each value checked again: one a tampered or hand-edited
    file holds out of range is left out (its default stands), never used."""
    path = path or knobs_path()
    try:
        raw = settingsfile.load(path, STEPS)
    except (OSError, ValueError):
        log.warning("%s is unreadable; every knob at its default", path)
        return {}
    if raw is None:
        return {}
    out = {}
    for name, given in raw.items():
        try:
            out[name] = value(name, given)
        except BadKnob as exc:
            log.warning("ignoring %s in %s: %s", name, path.name, exc)
    try:
        _night(out)
    except BadKnob as exc:
        log.warning("ignoring the quiet hours in %s: %s", path.name, exc)
        out.pop("QUIET_FROM", None)
        out.pop("QUIET_UNTIL", None)
    return out


def on(switch: str, path: Path | None = None) -> bool:
    """Whether the user has turned `switch` on. Off unless they have, and
    off when the file cannot be read. Read from the file each time, so a
    switch turned in the panel counts at the next check."""
    if switch not in SWITCHES:
        raise KeyError(f"{switch!r} is not a switch")
    return read(path).get(switch) == "on"


def write(changes: dict, path: Path | None = None, *, check=None) -> dict:
    """Merge `changes` into what the user set -- a value of None puts that knob
    back to its default -- and return the whole of it. BadKnob, and nothing
    written, if any change will not do, the quiet hours as they would be
    included. `check`, given what would be written, may refuse it too with
    BadKnob: the worker's rules that need its defaults."""
    path = path or knobs_path()
    current = read(path)
    for name, given in changes.items():
        if given is None:
            if name not in KNOBS:
                raise BadKnob(f"there is no setting {name!r}")
            current.pop(name, None)
        else:
            current[name] = value(name, given)
    _night(current)
    if check is not None:
        check(current)
    try:
        settingsfile.save(path, current, STEPS, indent=2)
    except settingsfile.Newer as exc:
        raise BadKnob(str(exc)) from exc
    return current
