"""The tasks class's contract: what every tasks provider must do, against its fake.

One suite for every provider and template (05-connectors.md, section 8).
Each check takes a `TasksCase` and raises AssertionError on a broken
contract; a test file parametrizes over `CHECKS`.

Core's commitments are mirrored to the user's phone as tasks
(`agent/taskmirror.py`): it makes them, reads them back, and ticks them
done when the commitment is kept, through core's write path (`create`,
`modify`). The cases that would go wrong: a ticked task still listed as
open, so the user is reminded of what they did; a new task whose id is not
the one it is found by, so the mirror makes it again every hour; a tick
that landed on another task of the same name; and a change made on the
phone overwritten by Steve's older copy.

A task has no identity beyond its id on most services, so a tasks
snapshot is keyed by native id. The list a task goes to when none is named
is the account's own (`tasks(None)`), as core's mirror uses it.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from steve_sdk import classes
from steve_sdk.classes import Task, TasksProvider
from steve_sdk.errors import TargetChanged
from steve_sdk.testing import World, changed


@dataclass
class TasksCase:
    provider: TasksProvider
    world: World
    writes: Any                       # core.writes, the one write path
    address: str                      # the account's own address


def _seed(case: TasksCase, *items: dict) -> list[str]:
    return case.world.seed("tasks", list(items))


def open_tasks_are_read_and_ticked_ones_left_off(case: TasksCase) -> None:
    ids = _seed(case, {"title": "Buy seed", "due": "2026-11-03"},
                {"title": "Fix the gate", "done": True},
                {"title": "Prune the apples", "list": "Allotment"})
    assert [t.title for t in case.provider.tasks(limit=50)] == ["Buy seed"]
    every = case.provider.tasks(open_only=False, limit=50)
    assert sorted(t.title for t in every) == ["Buy seed", "Fix the gate"]
    assert [t.done for t in every if t.title == "Fix the gate"] == [True]
    lists = {found.title: found.native_id for found in case.provider.lists()}
    assert "Allotment" in lists, lists
    assert [t.title for t in case.provider.tasks(lists["Allotment"], limit=50)] == [
        "Prune the apples"]
    one = case.provider.fetch(ids[0])
    assert one is not None and one.title == "Buy seed" and one.due == "2026-11-03"
    assert not one.done and one.native_id == ids[0]
    other = case.provider.fetch(ids[2], lists["Allotment"])
    assert other is not None and other.title == "Prune the apples"


def a_new_task_is_found_again_by_its_own_id(case: TasksCase) -> None:
    before = case.world.snapshot("tasks")
    made = case.writes.create(case.provider, Task(
        native_id="", title="Call the council", notes="About the gate", due="2026-11-05"))
    assert made.native_id
    assert changed(before, case.world.snapshot("tasks")) == {made.native_id}
    listed = [t for t in case.provider.tasks(limit=50) if t.title == "Call the council"]
    assert [t.native_id for t in listed] == [made.native_id], \
        "a listing names the new task by the id it was made with"
    assert listed[0].due == "2026-11-05" and listed[0].notes == "About the gate"


def of_two_tasks_alike_only_the_one_named_is_ticked(case: TasksCase) -> None:
    one, two = _seed(case, {"title": "Water the beans"}, {"title": "Water the beans"})
    before = case.world.snapshot("tasks")
    done = case.writes.modify(case.provider, one, case.provider.complete)
    assert done.done and done.native_id == one
    assert changed(before, case.world.snapshot("tasks")) == {one}
    assert [t.native_id for t in case.provider.tasks(limit=50)] == [two]


def a_change_made_on_the_phone_since_the_read_is_not_overwritten(case: TasksCase) -> None:
    if not case.provider.has(classes.CONDITIONAL):
        return
    [native] = _seed(case, {"title": "Water the beans"})
    read = case.provider.fetch(native)
    case.world.mutate("tasks", native, title="Water the beans at six")
    mine = copy.deepcopy(read)
    mine.title = "Water the beans and peas"
    before = case.world.snapshot("tasks")
    for what, write in (("a change", lambda: case.provider.update(mine)),
                        ("a tick", lambda: case.provider.complete(read))):
        try:
            write()
        except TargetChanged:
            continue
        raise AssertionError(f"{what} over one made on the phone went ahead")
    assert case.world.snapshot("tasks") == before
    assert case.provider.fetch(native).title == "Water the beans at six"


def a_change_goes_to_the_task_named_alone(case: TasksCase) -> None:
    one, two = _seed(case, {"title": "Water the beans"}, {"title": "Water the beans"})
    read = case.provider.fetch(one)
    read.title, read.due = "Water the beans at six", "2026-11-04"
    before = case.world.snapshot("tasks")
    written = case.provider.update(read)
    assert written.native_id == one and written.title == "Water the beans at six"
    assert changed(before, case.world.snapshot("tasks")) == {one}
    assert case.provider.fetch(two).title == "Water the beans"


CHECKS: tuple[Callable[[TasksCase], None], ...] = (
    open_tasks_are_read_and_ticked_ones_left_off,
    a_new_task_is_found_again_by_its_own_id,
    of_two_tasks_alike_only_the_one_named_is_ticked,
    a_change_made_on_the_phone_since_the_read_is_not_overwritten,
    a_change_goes_to_the_task_named_alone,
)
