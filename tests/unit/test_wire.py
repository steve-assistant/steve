"""What crosses the pipe between core and a connector in its sandbox
(`steve_sdk/wire.py`).

The answer comes from the module's code, and core reads it as if a
provider it trusts had made it. So it is made again only as one of the
classes' own types, each field the type the class declares, and the whole
answer what the interface's method returns; anything else is refused,
before core's code reads a field of it.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

import pytest

from steve_sdk import wire
from steve_sdk.classes import Draft, Event, FileItem, Guest, Message, Task, TaskList

LONDON = dt.timezone(dt.timedelta(hours=1))
MESSAGE = Message(native_id="m-14", ident="<plots-14@greenway-garden.example>",
                  sender="committee@greenway-garden.example", subject="Plot assignments",
                  when=1_760_000_000_000, unread=True, labels=("INBOX", "garden"),
                  extra={"List-Id": "<garden.example>"})


def _crossed(value):
    """`value` as the other side makes it again, through JSON as the pipe is."""
    return wire.load(json.loads(json.dumps(wire.dump(value))))


def test_each_of_the_classes_types_crosses_the_pipe_and_comes_back_equal():
    values = [
        MESSAGE,
        Draft(to="ada@else.example", subject="Water rota", body="Tuesday?",
              attachments=("rota.pdf",)),
        Event(native_id="e-1", title="Work day", start=dt.datetime(2026, 10, 17, 10, tzinfo=LONDON),
              end=dt.datetime(2026, 10, 17, 12, tzinfo=LONDON),
              guests=(Guest(email="ada@else.example", response="accepted"),)),
        Event(native_id="e-2", title="Harvest", start=dt.date(2026, 10, 24),
              end=dt.date(2026, 10, 25), all_day=True),
        FileItem(native_id="f-1", name="Rota", mime="text/plain", owners=("maya@mail.example",)),
        TaskList(native_id="l-1", title="Garden"),
        Task(native_id="t-1", title="Order seeds", due="2026-11-01"),
        [MESSAGE, None, ("a", 2)],
        {"Subject": "Plots", "count": 3},
    ]
    for value in values:
        assert _crossed(value) == value


def test_a_field_of_a_type_the_class_does_not_declare_is_refused_not_made():
    """A subject that is a list would reach core's code and fail far from
    the cause, or a start that is text be compared with a time."""
    bad = wire.dump(MESSAGE)
    bad["fields"]["subject"] = ["Plot", "assignments"]
    with pytest.raises(wire.Refused, match="Message.subject is not str"):
        wire.load(bad)

    flag = wire.dump(MESSAGE)
    flag["fields"]["when"] = True
    with pytest.raises(wire.Refused, match="Message.when is not int"):
        wire.load(flag)

    start = wire.dump(Event(native_id="e-1"))
    start["fields"]["start"] = "2026-10-17T10:00:00+01:00"
    with pytest.raises(wire.Refused, match="Event.start"):
        wire.load(start)


def test_a_field_the_class_does_not_have_and_one_it_needs_are_refused():
    stray = wire.dump(MESSAGE)
    stray["fields"]["password"] = "hunter2"
    with pytest.raises(wire.Refused, match="a Message has no password"):
        wire.load(stray)

    with pytest.raises(wire.Refused, match="a Draft without what it needs"):
        wire.load({"$": "Draft", "fields": {"to": "ada@else.example"}})


def test_only_the_classes_own_types_are_made_on_this_side():
    """A name is not a type: nothing but the classes' dataclasses, a time,
    a date and a tuple is made from what the module sends."""
    for sent in ({"$": "Path", "fields": {}}, {"$": "Provider", "fields": {}},
                 {"$": "Message"}, {"$": ["Message"], "fields": {}},
                 {"$": "datetime", "value": "Saturday"}, {"$": "tuple", "items": "ab"}):
        with pytest.raises(wire.Refused):
            wire.load(sent)
    # One hidden in a table is made, and then fails the table's type.
    extra = wire.dump(MESSAGE)
    extra["fields"]["extra"] = {"List-Id": wire.dump(MESSAGE)}
    with pytest.raises(wire.Refused, match="Message.extra"):
        wire.load(extra)


def test_what_cannot_cross_is_refused_on_the_way_out():
    @dataclass
    class Message:  # a type of the same name is not the class's
        native_id: str

    for value in (Message("m-1"), lambda: None, {1: "one"}, {"$": "Message"}, {"a", "b"}):
        with pytest.raises(TypeError):
            wire.dump(value)


def test_an_answer_is_held_to_what_the_interfaces_method_returns():
    assert wire.answer("mail", "search", [wire.dump(MESSAGE)]) == [MESSAGE]
    assert wire.answer("mail", "fetch", None) is None
    assert wire.answer("calendar", "delete", None) is None
    for method, value in (("search", wire.dump(MESSAGE)), ("make_draft", None),
                          ("fetch", [wire.dump(MESSAGE)]), ("headers", {"To": 3}),
                          ("make_draft", wire.dump(Draft(to="a@b.example", subject="",
                                                        body="")))):
        with pytest.raises(wire.Refused, match=f"mail.{method} returned"):
            wire.answer("mail", method, value)
    with pytest.raises(wire.Refused, match="no method 'drop_everything'"):
        wire.answer("mail", "drop_everything", None)


def test_a_method_that_takes_a_function_is_not_called_over_the_pipe():
    """`arrived` takes the function that says which messages core has
    seen; a function cannot cross, so core runs the interface's own
    `arrived` on the methods that can."""
    assert "arrived" not in wire.methods("mail")
    assert {"search", "fetch", "make_draft", "test"} <= wire.methods("mail")
    assert not any(name.startswith("_") for k in ("mail", "calendar", "files", "tasks")
                   for name in wire.methods(k))
