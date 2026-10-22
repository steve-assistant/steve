"""Tasks is a view. The commitments table is the record.

The whole risk of this feature is becoming the third overlapping to-do list.
Three lists fail silently: an item in the wrong one is never surfaced, and the user
finds out by missing it — which is worse than not having the feature, because
by then the user was relying on it.

So every test here is really one test asked five ways: **can the two stores
disagree?** Pushing, ticking, deleting, adding by voice, adding twice. If any
path can leave a task saying one thing and a commitment saying another, the
discipline has already failed.

The mirror speaks to whichever tasks account there is, through the slot
(`agent/taskmirror.py`); here that account is a list in a dict
(`tests/gate/slotfake.TaskBoard`).
"""

from __future__ import annotations

import pytest

from agent import taskmirror
from steve_sdk import ids
from steve_sdk.classes import Task
from steve_sdk.errors import ConnectorError, Transient
from tests.gate import slotfake
from tools import commitments


@pytest.fixture
def board(monkeypatch):
    """Their tasks account, with nothing on it yet."""
    found = slotfake.TaskBoard()
    found.account = slotfake.plug(monkeypatch, "tasks", google=found)["google"]
    return found


def _commitment(store, text="Send the deck", due="2026-10-01"):
    return commitments.add(
        store, text=text, quote=f"I'll {text.lower()}.", due_date=due,
        source_id=f"m-{text}",
    )


# --------------------------------------------------------------------------
# Pushing out
# --------------------------------------------------------------------------


async def test_a_dated_commitment_becomes_a_task(store, board):
    _commitment(store)

    result = await taskmirror.sync(store)

    assert result["mirrored"] == 1
    assert board.created[0].title == "Send the deck"
    assert commitments.open_ones(store)[0].task_id == board.account.id("task-1")


async def test_the_quote_travels_with_it(store, board):
    """The sentence is what makes a commitment trustworthy. Stripping it at
    the phone is where the user would be most likely to doubt one."""
    _commitment(store)

    await taskmirror.sync(store)

    assert "I'll send the deck." in board.created[0].notes


async def test_an_undated_commitment_from_their_mail_is_not_mirrored(store, board):
    """A tasks app sorts and nags by date. A pile of undated things their
    sent mail implied is a second copy of a list the user already has."""
    _commitment(store, due=None)

    assert (await taskmirror.sync(store))["mirrored"] == 0
    assert board.created == []


async def test_syncing_twice_does_not_make_two_tasks(store, board):
    """The sync runs nightly and windows overlap. Duplicating on every run
    is the most obvious way this becomes noise."""
    _commitment(store)

    await taskmirror.sync(store)
    await taskmirror.sync(store)

    assert len(board.created) == 1


async def test_a_closed_commitment_is_never_mirrored(store, board):
    commitment_id = _commitment(store)
    commitments.resolve(store, commitment_id)

    assert (await taskmirror.sync(store))["mirrored"] == 0


# --------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------


async def test_ticking_a_task_closes_the_commitment(store, board):
    """The two-way half. This does not violate "never auto-close" -- that
    rule is against *inferring* completion from a later email. A tick is
    the user saying so."""
    _commitment(store)
    await taskmirror.sync(store)
    board.tick("task-1")

    result = await taskmirror.sync(store)

    assert result["closed"] == 1
    assert commitments.open_ones(store) == []
    assert commitments.counts(store)["done"] == 1


async def test_deleting_a_task_does_not_close_the_commitment(store, board):
    """A removed reminder is not a finished obligation. Closing it here
    would lose the thing quietly, which is the one outcome this feature
    exists to prevent."""
    _commitment(store)
    await taskmirror.sync(store)
    board.remove("task-1")

    await taskmirror.sync(store)

    assert len(commitments.open_ones(store)) == 1
    assert commitments.counts(store)["done"] == 0


async def test_a_deleted_task_is_not_put_back(store, board):
    """The user removed it on purpose. Recreating it nightly is the small
    insistence that gets a whole feature muted."""
    _commitment(store)
    await taskmirror.sync(store)
    board.remove("task-1")

    await taskmirror.sync(store)
    await taskmirror.sync(store)

    assert len(board.created) == 1
    assert commitments.open_ones(store)[0].task_id == taskmirror.NO_MIRROR


async def test_a_task_on_another_account_is_neither_ticked_nor_taken_for_deleted(
        store, board):
    """The list is read from one account. A task id another account handed
    out is not on it, and reading its absence as the user's delete would
    stop mirroring something they never touched."""
    from tools import obligations

    commitment_id = _commitment(store)
    elsewhere = ids.make("tasks", "0123456789ab", "task-9")
    obligations.set_task(store, commitment_id, elsewhere)

    await taskmirror.sync(store)

    assert commitments.open_ones(store)[0].task_id == elsewhere
    assert board.created == []


# --------------------------------------------------------------------------
# When the account is unreachable
# --------------------------------------------------------------------------


async def test_an_unreachable_account_is_reported_not_raised(store, monkeypatch):
    """Google's Tasks API is disabled in a Cloud project until somebody
    clicks a button. A nightly run must survive that rather than end on it."""

    class Unreachable(slotfake.TaskBoard):
        def tasks(self, list_id=None, *, open_only=True, limit=50):
            raise Transient("the Tasks API is not enabled")

    slotfake.plug(monkeypatch, "tasks", google=Unreachable())
    _commitment(store)

    result = await taskmirror.sync(store)

    assert result["error"]
    assert result["mirrored"] == 0
    assert len(commitments.open_ones(store)) == 1, "the commitment survived"


async def test_with_no_tasks_account_nothing_is_mirrored_and_it_says_why(store, monkeypatch):
    slotfake.plug(monkeypatch, "tasks")
    _commitment(store)

    result = await taskmirror.sync(store)

    assert result["mirrored"] == 0 and "no tasks account" in result["error"]
    assert commitments.open_ones(store)[0].task_id is None


class _Refusing(slotfake.TaskBoard):
    """An account whose creates fail, until told otherwise."""

    refusing = True

    def create(self, task: Task) -> Task:
        if self.refusing:
            raise ConnectorError("quota")
        return super().create(task)


async def test_a_failed_create_leaves_the_commitment_unmirrored(store, monkeypatch):
    """Rather than recording a task id that does not exist, which would
    make the next sync read a status for nothing and conclude the user deleted
    it."""
    slotfake.plug(monkeypatch, "tasks", google=_Refusing())
    _commitment(store)

    await taskmirror.sync(store)

    assert commitments.open_ones(store)[0].task_id is None


async def test_the_mirror_is_capped(store, board):
    for i in range(taskmirror.MAX_MIRRORED + 5):
        _commitment(store, text=f"thing {i}", due="2026-10-01")

    assert (await taskmirror.sync(store))["mirrored"] == taskmirror.MAX_MIRRORED


# --------------------------------------------------------------------------
# Asked for in conversation
# --------------------------------------------------------------------------


@pytest.fixture
def worker(store, monkeypatch):
    """The executor's view: the worker's connection is this store."""
    from agent import runtime

    monkeypatch.setattr(runtime, "conn", store)
    return store


async def test_a_task_the_user_asks_for_is_on_user_phone_at_once(worker, board):
    """On 25 September three tasks asked for at 9pm were "added to your
    Tasks" and reached the app at 3am, with the nightly sync."""
    from agent.toolset import people

    result = await people._do_add_task(task="Wish Hazel happy birthday",
                                       due_date="2026-09-25")

    assert result["on_phone"] is True
    assert [t.title for t in board.created] == ["Wish Hazel happy birthday"]
    assert board.created[0].notes == "", "her own words repeated as the note"


async def test_an_undated_task_the_user_asks_for_still_reaches_their_phone(worker, board):
    """"Add it to my tasks app" is an instruction about the app."""
    from agent.toolset import people

    result = await people._do_add_task(task="Book the dentist")

    assert result["on_phone"] is True and len(board.created) == 1


async def test_a_task_the_account_refused_says_so_and_the_sync_catches_up(worker, monkeypatch):
    """So the reply does not claim it is on their list when it is not."""
    from agent.toolset import people

    refusing = _Refusing()
    slotfake.plug(monkeypatch, "tasks", google=refusing)
    result = await people._do_add_task(task="Book the dentist")
    assert result["on_phone"] is False and "not on their tasks list" in result["note"]

    refusing.refusing = False
    assert (await taskmirror.sync(worker))["mirrored"] == 1


async def test_asking_twice_makes_one_task(worker, board):
    from agent.toolset import people

    await people._do_add_task(task="Book the dentist")
    again = await people._do_add_task(task="Book the dentist")

    assert again.get("already_tracked") and len(board.created) == 1


async def test_a_task_done_in_conversation_is_ticked_on_the_account_it_is_on(worker, board):
    """`complete_task` ticks exactly the task the commitment names, found
    again by its id, and on the account that handed the id out."""
    from agent.toolset import people

    added = await people._do_add_task(task="Book the dentist")
    await people._do_complete_task(commitment_id=added["commitment_id"])

    assert board.completed == ["task-1"]
    assert commitments.open_ones(worker) == []


def test_a_tick_on_user_phone_stops_the_hourly_reminder(store, board):
    """The attention loop's commitments source reads ticks back before it
    looks for what is coming due. Nightly, a birthday the user had already wished
    was still being raised all day."""
    from agent.attention.sources import obligations

    # A fixed day in the past: `due_soon` counts what is already late.
    commitment_id = commitments.add(store, text="Wish Hazel happy birthday",
                                    quote="Wish Hazel happy birthday", due_date="2026-09-25",
                                    source="conversation", source_id="said:hazel")
    assert taskmirror.mirror(store, commitment_id)["on_phone"]
    assert obligations.commitments(store), "not raised before the tick"

    board.tick("task-1")

    assert obligations.commitments(store) == []
