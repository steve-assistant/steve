"""Every number that decides how much the agent does on its own.

In one file, and in code rather than in a prompt: the `watch.py` lesson. A
model asked "is this worth interrupting them for?" has no memory of the six
times it already did today and no stake in being ignored, so the model
judges *what* is worth saying and these decide *how often*. Tuning the loop
means editing this file; nothing else in the package hardcodes a cadence, a
cap or a budget.

The shape comes from what the user asked for on 24 September 2026: briefings
morning and evening, timely pings, an always-on loop that flags anything
important, pings at night only for something that clears a higher bar, and
never a call at night.
"""

from __future__ import annotations

from datetime import time, timedelta

# Quiet hours are the phone's, not ours: `core/call_policy.py` is frozen, and
# two answers to "is it too late to interrupt?" would mean one of them is
# wrong. At night only a critical item may ping, and nothing may ring. Their
# times are knobs (`QUIET_FROM`, `QUIET_UNTIL`), set on `call_policy` and read
# there at the moment of asking: a copy taken here at import would keep the
# defaults after the user changed them.
from core.call_policy import within_quiet_hours

__all__ = ["within_quiet_hours"]


# How often the loop wakes. Each source keeps its own cadence below; this is
# only how promptly a source that has come due gets run.
TICK_S = 60

# How often each source looks, in seconds. Frequent where it is cheap and
# the answer goes stale fast (mail); rare where nothing changes quickly.
# After the laptop has slept, every source is overdue and runs on the first
# tick, which is the catch-up.
CADENCE_S: dict[str, int] = {
    "calendar": 10 * 60,  # what changed: invites, moves, cancellations
    "horizon": 60 * 60,  # what is coming, for preparing
    "after": 15 * 60,  # what just ended, to ask how it went
    "commitments": 60 * 60,
    "deadlines": 3 * 60 * 60,  # calendars of due dates, which change when someone posts
    "health": 60 * 60,  # what it depends on: credit, the sandbox, Docker, disk, jobs
    "drive": 60 * 60,
    "weather": 30 * 60,  # the forecast and official alerts
    "news": 90 * 60,  # a feed per topic the user follows
    "threads": 6 * 60 * 60,  # replies owed, either way: days, not minutes
}

# When a thread left hanging is worth a word, in days since its last
# message. The user owes a reply after two; someone owes them one after four. Past
# the second number it has gone cold, and a nudge about it is noise.
OWE_REPLY_AFTER_DAYS = 2
OWE_REPLY_UNTIL_DAYS = 7
AWAIT_REPLY_AFTER_DAYS = 4
AWAIT_REPLY_UNTIL_DAYS = 10

# How many new things one source may hand the judge in one go. A blast
# radius, not a token limit: a backlog after a long sleep is real, and a
# number large enough for any backlog is large enough for one bad run to
# fill their calendar.
MAX_PER_SOURCE = 30

# How many things one judgement looks at. More wait for the next tick.
MAX_TRIAGE_BATCH = 40

# A judgement that fails this many times on the same item gives up on it
# rather than paying for the same failure every minute forever.
MAX_TRIAGE_ATTEMPTS = 3

# How much preparing one tick and one day may do. Each task is one turn of
# a tool-using agent; four is a busy tick and twenty a very busy day.
MAX_TASKS_PER_TICK = 4
MAX_TASKS_PER_DAY = 20

# Interruptions. A ping is one message however many things it carries.
MAX_DAY_PINGS = 8  # outside quiet hours
MAX_NIGHT_PINGS = 2  # in quiet hours, critical items only

# Questions the agent asks them a day, about what has gone quiet on their list
# (agent/closing.py `briefing_questions`), carried by the briefings. The user,
# 10 October 2026: reach out "more for sure", not far more. A constant, not a
# panel knob: the panel's bounds are frozen (core/knobs.py).
MAX_QUESTIONS_PER_DAY = 3


# Something the judge said to mention now that has waited this long -- for a
# daily cap, or quiet hours -- is no longer news; it goes in the next
# briefing instead of arriving late and out of context.
STALE_AFTER = timedelta(hours=3)

# How long before an event its heads-up goes out.
BEFORE_LEAD = timedelta(minutes=45)

# Whether an event's heads-up also looks up the people in it and their
# organisation on the web (`prepare.tools_for`): "off", from the user's own
# mail, calendar and contacts only; or "on". A panel knob (core/knobs.py).
MEETING_RESEARCH = "off"

# Whether to ask how a meeting went once it is over (the `after` source):
# "off"; "every" meeting; or "judged", only those a decision model judges
# worth a follow-up for this user (`sources.calendar.worth_asking`), and
# none when it cannot be asked. A panel knob (core/knobs.py).
FOLLOW_UP_AFTER = "judged"

# The units the weather is said in (`sources.weather.units`): "auto", those
# of the country the user's home is in; or "metric" or "us". A panel knob.
UNITS = "auto"

# The doer's conversations, and the escalation call it places, run here.
CHANNEL = "initiative"

# What it may spend is core's (core/spend.py): its judging and its doing are
# background work's `triage` and `work` parts, each with a day budget, asked
# before each step -- the bargain the night makes: a step cannot be stopped
# halfway, but the next one can be told not to start. Judging is kept apart
# and cheap so that doing never starves noticing.

# The briefings.
MORNING = time(8, 0)
EVENING = time(21, 0)
