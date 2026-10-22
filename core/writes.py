"""The write path: one sequence for every write, by every provider.

Each provider wrote its own checks, or none. A password mailbox's delete was a bare
EXPUNGE, removing every message marked deleted in the folder, not just the
one meant; Google's calendar updates and deletes went by id with no etag;
and on 11 October 2026 a spike's cleanup trusted a server's filter and
deleted all 96 events of a calendar (`steve_sdk/exact.py`). A provider is
now a few methods, and core runs every write the same way around them, in
its own process, so no provider can skip a step -- and from step 6, no
sandboxed module either, since this never runs in the module's process:

1. fetch the one item by its native id;
2. check that exactly that item answered, and that its identifying field
   (Message-ID, UID) is the one expected (`exact.one`);
3. take its condition (an etag, UIDVALIDITY with MODSEQ, a change key), or
   note that it has none;
4. with no condition, an update or a delete must have come through a card,
   and the item must still match what the card showed (its digest) -- or
   Jev must have judged the card unneeded (`core/lostupdate.py`, a
   verdict `waived`);
5. write, conditional where it can be;
6. read again, and check that an update reads back as written, and that
   exactly that item changed among its neighbours. What this finds is
   `AfterWrite`, not `TargetChanged`: the write went, so the user is told,
   and is not asked again to make it a second time.

Creating has no target to miss and is checked by step 6 alone. Reversible
changes to a message (flags, moving, archiving) still check the target by
steps 1 and 2. `tests/gate/test_writes.py` drives each step with a fake that
answers wrongly at it.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable
from typing import Any

from steve_sdk import classes, exact
from steve_sdk.errors import AfterWrite, NeedsCard, TargetChanged, Unsupported


def _fetch(provider: classes.Provider, native: str) -> Any:
    return provider.fetch(native)  # type: ignore[attr-defined]


def target(provider: classes.Provider, native: str, *, expected: str | None = None) -> Any:
    """Steps 1 and 2: the one item `native` names, checked on our side."""
    found = _fetch(provider, native)
    items = [found] if found is not None else []
    item = exact.one(items, expected=expected, ident=lambda i: i.ident, what=provider.klass)
    if item.native_id != native:
        # A server that answered for another id than the one asked: the
        # write would land on whatever it chose.
        raise TargetChanged(f"asked for {native!r}, the server answered {item.native_id!r}")
    return item


def conditional(provider: classes.Provider, item: Any) -> bool:
    return provider.has(classes.CONDITIONAL) and item.condition is not None


def _waived(verdict: Any) -> bool:
    """Whether `verdict` is Jev's judgement that a card asked only for want
    of a condition is not needed. A free verdict of any other kind -- a
    routine's envelope, which is about the kind of action and not this item
    -- is not one."""
    from core.monitor import Gate

    return isinstance(verdict, Gate) and verdict.kind == "free" and verdict.waived


def _guard(provider: classes.Provider, item: Any, card: str | None, what: str,
           verdict: Any = None) -> None:
    """Steps 3 and 4."""
    if card is not None and item.digest() != card:
        raise TargetChanged(f"the {provider.klass} item changed since the card was shown; "
                            "asking again")
    if not conditional(provider, item) and card is None and not _waived(verdict):
        raise NeedsCard(f"this account cannot {what} conditionally, so it asks first")


def _neighbours(provider: classes.Provider, *items: Any) -> dict[str, str | None] | None:
    """The neighbours of each of `items` as one listing, or None where the
    provider cannot list them. An update reads them around where the item
    was and where it goes, before the write and after alike, so that a
    provider listing by time (CalDAV lists the item's days) compares the
    same stretch both times."""
    out: dict[str, str | None] = {}
    for item in items:
        found = provider.neighbours(item)
        if found is None:
            return None
        out.update(found)
    return out


def _check_after(provider: classes.Provider, before: dict | None, *items: Any,
                 target_ident: str, gone: bool = False, new: bool = False) -> None:
    """Step 6 among the neighbours: AfterWrite if more than the one changed."""
    if before is None:
        return
    after = _neighbours(provider, *items)
    if after is not None:
        exact.changed_only(before, after, target=target_ident, gone=gone, new=new)


def _read_again(provider: classes.Provider, native: str, ident: str) -> Any:
    """The item read again after its write, by steps 1 and 2. What they
    refuse here, they refuse after the write: AfterWrite."""
    try:
        return target(provider, native, expected=ident or None)
    except TargetChanged as exc:
        raise AfterWrite(str(exc)) from exc


def delete(provider: classes.Provider, native: str, *, expected: str | None = None,
           card: str | None = None, verdict: Any = None) -> Any:
    """Delete exactly the item `native` names; the item as it was."""
    item = target(provider, native, expected=expected)
    _guard(provider, item, card, "delete", verdict)
    before = _neighbours(provider, item)
    provider.delete(item)  # type: ignore[attr-defined]
    still = _fetch(provider, native)
    # An empty identifying field names no item. Classic Outlook reads no
    # Message-ID, and its Delete moves a draft to Deleted Items, where a .pst
    # store keeps its EntryID: "" matching "" said a delete that went had
    # not. Without one, the delete rests on the provider's answer and the
    # neighbours.
    if still is not None and exact.norm(item.ident) \
            and exact.norm(still.ident) == exact.norm(item.ident):
        raise AfterWrite("the server said it deleted the item, and it is still there")
    _check_after(provider, before, item, target_ident=item.ident, gone=True)
    return item


def update(provider: classes.Provider, native: str, change: Callable[[Any], Any], *,
           expected: str | None = None, card: str | None = None, verdict: Any = None) -> Any:
    """Apply `change` to exactly the item `native` names; the item as written."""
    item = target(provider, native, expected=expected)
    _guard(provider, item, card, "change it", verdict)
    changed = change(copy.deepcopy(item))
    if changed.native_id != item.native_id or exact.norm(changed.ident) != exact.norm(item.ident):
        raise TargetChanged("a change may not make the item another one")
    changed.condition = item.condition
    before = _neighbours(provider, item, changed)
    provider.update(changed)  # type: ignore[attr-defined]
    again = _read_again(provider, native, item.ident)
    _as_written(provider, item, changed, again)
    _check_after(provider, before, item, again, target_ident=item.ident)
    return again


def _as_written(provider: classes.Provider, item: Any, changed: Any, again: Any) -> None:
    """Step 6 for the one item, whatever its neighbours: each field the
    change set reads back as set, or AfterWrite.

    An Outlook.com server answered an update with a 404, or with success
    while changing nothing, and the event read back as it was; with its
    listing in pages there were no neighbours to compare, and the user was
    told the event moved."""
    meant = [f.name for f in dataclasses.fields(changed) if f.name != "condition"
             and getattr(changed, f.name) != getattr(item, f.name)]
    missed = [name for name in meant
              if not _same(getattr(again, name), getattr(changed, name))]
    if missed:
        raise AfterWrite(f"the {provider.klass} item does not read back as written: "
                         f"{', '.join(missed)} not as set")


def _same(read: Any, written: Any) -> bool:
    """A field read back against the value written: text without the space
    a server trims, a time as the same moment in any zone."""
    if isinstance(read, str) and isinstance(written, str):
        return read.strip() == written.strip()
    return read == written


def create(provider: classes.Provider, item: Any) -> Any:
    """Make `item`; the item as stored, found again by its own id."""
    before = _neighbours(provider, item)
    made = provider.create(item)  # type: ignore[attr-defined]
    if item.ident and exact.norm(made.ident) != exact.norm(item.ident):
        raise AfterWrite("the server stored the new item under another identity")
    again = _read_again(provider, made.native_id, made.ident)
    _check_after(provider, before, again, target_ident=again.ident, new=True)
    return again


def modify(provider: classes.Provider, native: str, op: Callable[[Any], Any], *,
           expected: str | None = None) -> Any:
    """A reversible change to a message (flags, labels, a move, archiving):
    no card, but still exactly the item named. `op` gets the checked item
    and returns it as changed."""
    item = target(provider, native, expected=expected)
    result = op(item)
    if result is not None and exact.norm(result.ident) != exact.norm(item.ident):
        raise TargetChanged("the server changed another message than the one named")
    return result if result is not None else item


def send_draft(provider: classes.MailProvider, native: str, *, expected: str | None = None,
               card: str | None = None, verdict: Any = None) -> Any:
    """Send exactly the draft `native` names, as its card showed it.

    Without a card it goes only on the monitor's verdict that this call is
    free (`core.monitor.Gate`, kind "free"): today, a note to the user's own
    address, or a send inside an approved routine's or goal's envelope.
    Until step 5's item 4e the caller said so with a flag, and its only
    caller passed `without_card=card is None`: a send that reached here with
    no card and no verdict went, and the refusal below could never fire.
    Every other send needs the card's digest, and a draft that changed
    since -- a Cc added, a word -- is not sent."""
    from core.monitor import Gate

    if not provider.has(classes.SEND):
        raise Unsupported("sending from this account")
    draft = target(provider, native, expected=expected)
    if card is not None:
        if draft.digest() != card:
            raise TargetChanged("the draft changed since the card was shown; asking again")
    elif not (isinstance(verdict, Gate) and verdict.kind == "free"):
        raise NeedsCard("a message is sent only after its card")
    return provider.send_draft(draft)
