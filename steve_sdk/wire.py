"""What a provider's methods take and give, on the pipe between core and a
module's code in its sandbox (06-modules.md, section 3).

A connector from outside runs in its sandbox, and core calls each method
of its provider there over the broker. Python's objects do not cross a
pipe, and what comes back is the module's: a `Message` whose subject is a
list, or a start that is a table, would reach core's code as if a provider
it trusts had made it, and fail far from the cause, or not fail. So a
value crosses as JSON with its type named, and is made again on the other
side only as one of the classes' own types (`steve_sdk.classes`), each
field checked against the type the class declares, and each answer
against what the interface's method returns (`answer`). Anything else is
refused, the whole answer with it.
"""

from __future__ import annotations

import collections.abc
import datetime as dt
import functools
import inspect
import types
import typing
from dataclasses import fields, is_dataclass
from typing import Any

from steve_sdk.classes import (
    INTERFACES,
    Draft,
    Event,
    FileItem,
    Guest,
    Message,
    Task,
    TaskList,
)

TYPES: dict[str, type] = {t.__name__: t for t in (Message, Draft, Guest, Event, FileItem,
                                                   TaskList, Task)}
TAG = "$"


class Refused(ValueError):
    """A value that is not what the interface says crosses."""


@functools.cache
def methods(klass: str) -> frozenset[str]:
    """The methods of `klass`'s interface core may call over the pipe. One
    that takes a function (`arrived`'s `seen`) is not among them, since a
    function cannot cross: core runs it as the interface's default, on
    the methods that do."""
    iface = INTERFACES[klass]
    return frozenset(name for name, member in inspect.getmembers(iface, inspect.isfunction)
                     if not name.startswith("_") and not _takes_a_function(member))


def _takes_a_function(member: Any) -> bool:
    hints = typing.get_type_hints(member)
    return any(typing.get_origin(hint) is collections.abc.Callable
               for name, hint in hints.items() if name != "return")


def dump(value: Any) -> Any:
    """`value` as JSON can carry it. TypeError for what does not cross."""
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if is_dataclass(value) and TYPES.get(type(value).__name__) is type(value):
        return {TAG: type(value).__name__,
                "fields": {f.name: dump(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, dt.datetime):
        return {TAG: "datetime", "value": value.isoformat()}
    if isinstance(value, dt.date):
        return {TAG: "date", "value": value.isoformat()}
    if isinstance(value, tuple):
        return {TAG: "tuple", "items": [dump(v) for v in value]}
    if isinstance(value, list):
        return [dump(v) for v in value]
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value) or TAG in value:
            raise TypeError(f"a table's keys are text, and none is {TAG!r}")
        return {k: dump(v) for k, v in value.items()}
    raise TypeError(f"a {type(value).__name__} does not cross the pipe")


def load(value: Any) -> Any:
    """What `dump` made, made again; Refused for anything else."""
    if isinstance(value, list):
        return [load(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value.get(TAG)
    if kind is None:
        return {k: load(v) for k, v in value.items()}
    try:
        if kind == "datetime":
            return dt.datetime.fromisoformat(value["value"])
        if kind == "date":
            return dt.date.fromisoformat(value["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Refused(f"a {kind} that is not one: {exc}") from None
    if kind == "tuple":
        if not isinstance(value.get("items"), list):
            raise Refused("a tuple without its items")
        return tuple(load(v) for v in value["items"])
    made = TYPES.get(kind) if isinstance(kind, str) else None
    given = value.get("fields")
    if made is None or not isinstance(given, dict):
        raise Refused(f"no {kind!r} crosses the pipe")
    hints = _hints(made)
    if stray := sorted(set(given) - set(hints)):
        raise Refused(f"a {kind} has no {', '.join(stray)}")
    loaded = {k: load(v) for k, v in given.items()}
    for name, field_value in loaded.items():
        if not fits(field_value, hints[name]):
            raise Refused(f"{kind}.{name} is not {_said(hints[name])}")
    try:
        return made(**loaded)
    except TypeError as exc:  # a field it must have, missing
        raise Refused(f"a {kind} without what it needs: {exc}") from None


def answer(klass: str, method: str, value: Any) -> Any:
    """A method's answer as the interface says it returns, or Refused."""
    made = load(value)
    want = _returns(klass, method)
    if not fits(made, want):
        raise Refused(f"{klass}.{method} returned what is not {_said(want)}")
    return made


def fits(value: Any, hint: Any) -> bool:
    """Whether `value` is of the type `hint` names, all the way down."""
    if hint is Any:
        return True
    if hint is None or hint is type(None):
        return value is None
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin in (typing.Union, types.UnionType):
        return any(fits(value, a) for a in args)
    if origin is tuple:
        if not isinstance(value, tuple):
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(fits(v, args[0]) for v in value)
        return len(args) == len(value) and all(fits(v, a) for v, a in zip(value, args,
                                                                            strict=True))
    if origin is list:
        return isinstance(value, list) and all(fits(v, args[0]) for v in value) if args \
            else isinstance(value, list)
    if origin is dict:
        key, item = args or (Any, Any)
        return isinstance(value, dict) and all(fits(k, key) and fits(v, item)
                                               for k, v in value.items())
    if hint is bool:
        return isinstance(value, bool)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if hint is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if isinstance(hint, type):
        return isinstance(value, hint)
    return False


@functools.cache
def _hints(made: type) -> dict[str, Any]:
    every = typing.get_type_hints(made)
    return {f.name: every[f.name] for f in fields(made)}


@functools.cache
def _returns(klass: str, method: str) -> Any:
    if method not in methods(klass):
        raise Refused(f"a {klass} provider has no method {method!r}")
    return typing.get_type_hints(getattr(INTERFACES[klass], method)).get("return", Any)


def _said(hint: Any) -> str:
    return getattr(hint, "__name__", None) or str(hint).replace("datetime.", "")
