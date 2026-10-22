"""When it is night, and who is in the conversation.

In the gate because these are the answers the agent must not be able to
widen. Every check of whether the user is present, and every check of
whether something unprompted may interrupt them, reads `core/call_policy.py`;
that file and this one are frozen, so a nightly upgrade cannot change
either answer.

The file once also held the telephone's limits: numbers it could not dial,
the disclosure said first, two confirmations and a grant for a call to
anyone else. The phone went with the server that placed its calls, and its
tests went with it.
"""

from __future__ import annotations

import pytest

from core import call_policy as policy

# --------------------------------------------------------------------------
# Who is in the conversation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel",
    ["initiative", "night", "reminder", "watch", "job", "attention", "", None,
     "a-loop-named-tomorrow", "phone"],
)
def test_every_session_but_the_users_own_is_unprompted(channel):
    """Whatever runs without them -- a sweep, the night, a reminder, a job,
    and a loop nobody has named yet -- may not answer a card, file a change
    to Steve as their request, or do anything else only they may ask for.
    "phone" is here because it is no longer one of theirs: a session still
    labelled so from before is not taken for the user."""
    assert policy.is_autonomous(channel), channel


def test_being_present_is_an_allowlist_so_a_new_channel_is_unprompted():
    """The list used to name the autonomous channels, so anything missing
    from it counted as the user talking -- and background jobs, which run in
    sessions called "job", were missing: research the night queued for
    itself could file a `modify_self` labelled as their request, past the
    self-upgrade pause. Pinned against the set itself, so widening it is a
    change to this frozen file and not a quiet addition somewhere else."""
    assert {"signal", "telegram", "tray", "voice"} == policy.PRESENT_CHANNELS
    for channel in ("signal", "Signal ", "telegram", "tray", "voice"):
        assert not policy.is_autonomous(channel), channel


# --------------------------------------------------------------------------
# The ceilings, which a prompt cannot argue with
# --------------------------------------------------------------------------


def test_a_live_conversation_hands_over_a_limited_number_of_times():
    """A cap left at zero or None is a cap that silently is not one, and a
    live conversation is billed by the minute while it goes in circles."""
    assert isinstance(policy.MAX_TURNS_PER_CALL, int) and policy.MAX_TURNS_PER_CALL > 0


def test_it_is_night_from_ten_to_eight_unless_the_user_says_otherwise():
    """The attention loop's night, and deliberately not `reminders.py`'s: an
    alarm the user set for 6am should go off at 6am, and a message Steve
    decided on its own to send at 6am should not."""
    from datetime import datetime

    assert policy.within_quiet_hours(datetime(2026, 9, 21, 3, 0))
    assert policy.within_quiet_hours(datetime(2026, 9, 21, 23, 30))
    assert not policy.within_quiet_hours(datetime(2026, 9, 21, 14, 0))


def test_the_attention_loop_uses_these_quiet_hours_not_its_own():
    """Two different answers to "is it too late to interrupt them" would be
    one of them being wrong -- so there is one, here, and the attention loop
    imports it rather than keeping a copy that could drift."""
    from agent.attention import settings

    assert settings.within_quiet_hours is policy.within_quiet_hours
    # Nor a copy of the times: they are knobs, set here, and a copy taken
    # at import would keep the defaults after the user changed them.
    assert not hasattr(settings, "QUIET_FROM") and not hasattr(settings, "QUIET_UNTIL")


# --------------------------------------------------------------------------
# This is not a sandbox either
# --------------------------------------------------------------------------


def test_the_policy_asks_no_model_anything():
    """Every function here compares a name or clock times. A policy that
    needed inference to evaluate would be a policy that could be argued
    with, which is the thing the frozen file exists to prevent.

    Checked by reading the source: no import of the agent, the models, or
    any client.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("core/call_policy.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {"datetime", "__future__"}, sorted(imported)
