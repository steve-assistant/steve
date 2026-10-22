"""Running a change to one item, on the account its id names, as its card
showed it.

A card binds what it showed: the digest of the item (`steve_sdk.classes`)
goes into the action's arguments, and the write passes it on only when the
user's yes is what runs it (`approvals.from_card`). An item that changed
after its card is not written; it is asked about again, as it is now
(`core/writes.py`). Mail's draft cards did this first (step 5, item 4c), and
the calendar's cards needed the same: an event moved, or given guests, after
its card was shown would have been changed as if it had not.

Only a refusal before the write is asked again. What the check after a
write finds (`AfterWrite`) is said and left: the write went, and a second
yes would make it twice. It was once a `TargetChanged` too, and a move whose
calendar a phone synced in the same second was carded again.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent import approvals, connectors
from agent.connectors import Connected, NoAccount
from agent.toolset.clients import commitment_store
from steve_sdk.errors import ConnectorError, TargetChanged


def routed(item_id: str) -> tuple[Connected, str]:
    """The account an id came from and its own id inside it, for an executor:
    on the event loop, with the worker's connection. Raises NoAccount."""
    conn, owned = commitment_store()
    try:
        return connectors.by_id(conn, item_id)
    finally:
        if owned:
            conn.close()


def failed(found: Connected | None, exc: Exception,
           label: Callable[[Connected], str] = lambda found: found.name) -> dict:
    """A tool's answer for an account that could not do it, in words the
    model can pass on (`connectors.say`)."""
    if isinstance(exc, ConnectorError) and found is not None:
        return {"ok": False, "kind": exc.kind, "error": connectors.say(exc, label(found))}
    return {"ok": False, "error": str(exc)}


def bound(item: Any) -> dict[str, str]:
    """What a card binds of the item it shows, for its arguments: the
    digest of what it showed (`shown`), and the item's identifying field
    (`ident`: a UID, a Message-ID) where it has one, which the write checks
    the item it finds by (`core/writes.target`, `expected`).

    No caller passed one, so the write path's identity check never ran, and
    CalDAV's check of the address it fetched compared the address with
    itself: an event that another took the place of, alike to the card,
    would have been the one changed."""
    return {"shown": item.digest(), **({"ident": item.ident} if item.ident else {})}


async def _again(found: Connected, native: str, args: dict,
                 card: Callable[[Connected, Any], str]) -> dict | None:
    """The card to ask again with, for an item that changed after its card
    was shown: the same arguments, bound to the item as it is now. None if
    it is gone."""
    try:
        now = await connectors.call(found, lambda p: p.fetch(native))
    except ConnectorError:
        return None
    if now is None:
        return None
    kept = {k: v for k, v in args.items() if k != "ident"}
    return {"args": {**kept, **bound(now)}, "preview": card(found, now)}


async def run(args: dict, id_key: str,
              write: Callable[[Any, str, str | None, str | None], Any],
              card: Callable[[Connected, Any], str],
              label: Callable[[Connected], str] = lambda found: found.name,
              ) -> dict:
    """`write(provider, native, card_digest, ident)` on the account
    `args[id_key]` came from: with the card's digest (`args["shown"]`) when
    the user's yes is what runs it, and the identifying field its card bound
    (`args["ident"]`, `bound`) whenever there is one, for the write to check
    the item by. Answers {"done": what write returned, "found": the
    account}, or a failure, with "again" when the item changed since its card."""
    try:
        found, native = routed(args[id_key])
    except NoAccount as exc:
        return {"ok": False, "error": str(exc)}
    digest = None
    if approvals.from_card():
        if not args.get("shown"):
            return {"ok": False, "error": "this card did not bind what it showed; ask again"}
        digest = args["shown"]
    # Taken free as well as from a card: the tool bound it from the item it
    # read, and a wrong one can only refuse the write.
    ident = args.get("ident") or None
    try:
        return {"done": await connectors.call(found, lambda p: write(p, native, digest, ident)),
                "found": found}
    except TargetChanged as exc:
        out = failed(found, exc, label)
        if (again := await _again(found, native, args, card)) is not None:
            out["again"] = again
        return out
    except ConnectorError as exc:
        return failed(found, exc, label)
