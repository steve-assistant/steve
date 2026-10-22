"""The files class's contract: what every files provider must do, against its fake.

One suite for every provider and template (05-connectors.md, section 8).
Each check takes a `FilesCase` and raises AssertionError on a broken
contract; a test file parametrizes over `CHECKS`.

They are the cases that would go wrong: a search that returned files that
did not match, or not newest first; a file read whole into a model's
context when it was asked cut, and not saying it was cut; a new document
whose id is not the one it can be read back by; a change to a spreadsheet
that landed on its twin. A provider that cannot make documents or
spreadsheets says so (`Unsupported`), and changes nothing.

Core makes documents and changes spreadsheets by calling the provider
itself (`agent/toolset/drive.py`), with no card: each is the user's own
file, made or changed as asked. A file has no identity beyond its id on
most services, so a files snapshot is keyed by native id.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from steve_sdk import classes
from steve_sdk.classes import FilesProvider
from steve_sdk.errors import Unsupported
from steve_sdk.testing import World, changed


@dataclass
class FilesCase:
    provider: FilesProvider
    world: World
    writes: Any                       # core.writes, the one write path
    address: str                      # the account's own address


def _ago(days: float) -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(days=days)


def _seed(case: FilesCase, *items: dict) -> list[str]:
    return case.world.seed("files", list(items))


def _refused(what: str, write: Callable[[], Any]) -> None:
    try:
        write()
    except Unsupported:
        return
    raise AssertionError(f"{what} went ahead")


def found_by_name_or_text_newest_first(case: FilesCase) -> None:
    _seed(case, {"name": "Plot budget", "text": "Seeds and compost.", "modified": _ago(3)},
          {"name": "Minutes", "text": "The plot budget was agreed.", "modified": _ago(1)},
          {"name": "Recipes", "text": "Soup.", "modified": _ago(0.5)})
    found = case.provider.search("budget", limit=10)
    assert [f.name for f in found] == ["Minutes", "Plot budget"], found
    assert all(f.text is None for f in found), "a search reads no file's text"
    assert len(case.provider.search("budget", limit=1)) == 1


def a_file_is_read_as_text_cut_where_asked_and_says_so(case: FilesCase) -> None:
    text = "Ada waters on Mondays. " * 400
    [native] = _seed(case, {"name": "Rota notes", "text": text})
    cut = case.provider.read(native, max_chars=100)
    assert cut.native_id == native and cut.name == "Rota notes"
    assert cut.text and len(cut.text) <= 100 and cut.truncated, "cut, and said to be"
    whole = case.provider.read(native, max_chars=len(text) + 100)
    assert whole.text is not None and whole.text.split() == text.split()
    assert not whole.truncated


def a_new_document_is_read_back_by_its_own_id(case: FilesCase) -> None:
    before = case.world.snapshot("files")
    if not case.provider.has(classes.DOCUMENTS):
        _refused("a document made", lambda: case.provider.create_document("Rota", "Ada"))
        assert case.world.snapshot("files") == before
        return
    made = case.provider.create_document("Plot rota", "Ada waters on Mondays.")
    assert made.native_id and made.link, "a new document has a link the user can open"
    assert changed(before, case.world.snapshot("files")) == {made.native_id}
    again = case.provider.read(made.native_id, max_chars=1_000)
    assert again.name == "Plot rota" and "Ada waters on Mondays." in (again.text or "")


def of_two_spreadsheets_alike_only_the_one_named_changes(case: FilesCase) -> None:
    rows = [["Name", "Day"], ["Ada", "Mon"]]
    before = case.world.snapshot("files")
    if not case.provider.has(classes.SPREADSHEETS):
        _refused("a spreadsheet made", lambda: case.provider.create_spreadsheet("Rota", rows))
        assert case.world.snapshot("files") == before
        return
    one = case.provider.create_spreadsheet("Rota", rows)
    two = case.provider.create_spreadsheet("Rota", rows)
    assert one.native_id != two.native_id
    assert case.provider.read_spreadsheet(one.native_id, cells="A1:B2") == rows
    before = case.world.snapshot("files")
    case.provider.update_spreadsheet(one.native_id, cells="A2:B2", rows=[["Bo", "Tue"]])
    assert changed(before, case.world.snapshot("files")) == {one.native_id}
    assert case.provider.read_spreadsheet(one.native_id, cells="A1:B2") == [
        ["Name", "Day"], ["Bo", "Tue"]]
    assert case.provider.read_spreadsheet(two.native_id, cells="A1:B2") == rows


CHECKS: tuple[Callable[[FilesCase], None], ...] = (
    found_by_name_or_text_newest_first,
    a_file_is_read_as_text_cut_where_asked_and_says_so,
    a_new_document_is_read_back_by_its_own_id,
    of_two_spreadsheets_alike_only_the_one_named_changes,
)
