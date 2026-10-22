"""When it is night for the user, and whether they are in the conversation.

Frozen, like everything in `core/`, because these two answers decide what
Steve may do on its own. Whether the user is present decides whether a
session may answer a card, file a change to Steve itself, write a skill,
search the web as them or set up a goal; whether it is night decides
whether something Steve chose to say may interrupt them now. A copy that
lived next to the code it governs could be widened by a nightly upgrade,
and these are not limits it gets a vote on.

**Why the numbers are here and not in a prompt.** A model asked "is it too
late to message them?" has no memory of the two times it already did today
and no stake in being ignored. So urgency is judged by a model, and timing
is decided here.

**What is deliberately not here.** Any judgement. `within_quiet_hours`
compares clock times and `is_autonomous` compares a name. Nothing in this
file asks a model anything, because a policy that needs inference to
evaluate is a policy that can be argued with.

The name is from when it also governed a telephone: what numbers it could
dial, how often it could ring the user, and the PIN for ringing it. The
phone went with the server that placed its calls, and its limits went with
it. These, which everything else reads, stayed.
"""

from __future__ import annotations

from datetime import datetime, time

# How many times one live conversation at the desk may hand the user's words
# to their assistant before it wraps up (`voice/desk_call.py`). A
# conversation that needs more is one going in circles, and the meter runs
# by the minute.
MAX_TURNS_PER_CALL = 40

# Quiet hours: the one definition, which the attention loop imports rather
# than copies. Deliberately *not* `agent/reminders.py`'s, which ignores them
# -- an alarm the user set for 6am should go off at 6am, and a message Steve
# decided on its own to send at 6am should not. At night the attention loop
# may still message them about something critical (24 Sep 2026), within its
# night ceiling (`agent/attention/settings.py`).
# These are the code's own; the user may set others from the panel
# (core/knobs.py), on either side of midnight.
QUIET_DEFAULT = (time(22, 0), time(8, 0))
QUIET_FROM, QUIET_UNTIL = QUIET_DEFAULT


def is_quiet(at: time, start: time, end: time) -> bool:
    """Whether the clock time `at` is in quiet hours from `start` to `end`:
    across midnight when they cross it (22:00 to 08:00), within the day
    when they do not (08:00 to 16:00, for one who works nights). They were
    always read as crossing midnight, so a day sleeper's covered the whole
    day."""
    if start <= end:
        return start <= at < end
    return at >= start or at < end


def night_hours(start: time, end: time) -> float:
    """How long quiet hours from `start` to `end` last, in hours."""
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    return (minutes % (24 * 60)) / 60


def within_quiet_hours(now: datetime | None = None) -> bool:
    """Is it the user's night right now?

    Applies to what Steve decides to say on its own. It does not apply to an
    answer to the user, or to a reminder they set -- they know what time it
    is.
    """
    return is_quiet((now or datetime.now()).time(), QUIET_FROM, QUIET_UNTIL)


# The sessions in which the user is the one talking. Everything else -- a
# sweep, the night, a reminder checking on something, a background job,
# and any channel nobody has named yet -- is Steve acting on its own
# initiative.
#
# An allowlist, and it used to be the opposite. The list was of autonomous
# channels, so a channel missing from it counted as the user present. One was
# missing: background jobs run in sessions called "job", including the
# research the night queues for itself, and such a session could file a
# `modify_self` that was labelled as their request -- straight past the
# self-upgrade pause the user had set. A new unprompted loop would have had
# the same hole the day it was named.
PRESENT_CHANNELS = frozenset({"signal", "telegram", "tray", "voice"})


def is_autonomous(channel: str | None) -> bool:
    """Whether a session is running without the user in it."""
    return (channel or "").strip().lower() not in PRESENT_CHANNELS
