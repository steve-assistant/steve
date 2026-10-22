"""One exact target per write, checked on our side.

On 11 October 2026 a spike's cleanup asked Google's CalDAV for the events
with one UID. Google ignored the filter and returned all 96 events of the
calendar, and the script deleted every one (they came back from Google
Calendar's Trash; `docs/plan/spikes/s3-google.md`). The server's answer was
trusted as the target.

So a write never takes its target from what a server says matched. It
names one item by its native id, fetches it, and checks here that exactly
one item answered and that its identifying field (the Message-ID, the UID)
is the one expected. `core/writes.py` calls these for every class and
provider; a module may call them too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from steve_sdk.errors import AfterWrite, TargetChanged


def norm(ident: str | None) -> str:
    """An identifying field as compared: angle brackets and case of a
    Message-ID do not make it another message, nor surrounding space."""
    text = (ident or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]
    return text.strip().lower()


def one[T](items: Iterable[T], *, expected: str | None, ident: Callable[[T], str],
        what: str = "item") -> T:
    """The single item whose identifying field is `expected`, or TargetChanged.

    `items` is everything the server answered for one exact target. More
    than one, none, or one with another identifying field all refuse: the
    write is never aimed at "whatever came back". With `expected` None or
    empty, the caller has no identifying field to check against (a provider
    whose ids do not carry one); exactly one answer is still required. An
    empty field names no item: "" would match any item that has none."""
    found = list(items)
    if norm(expected):
        matching = [item for item in found if norm(ident(item)) == norm(expected)]
        if len(found) != 1 or len(matching) != 1:
            raise TargetChanged(
                f"expected exactly one {what} with id {expected!r}; the server answered "
                f"{len(found)}, {len(matching)} of them that one")
        return matching[0]
    if len(found) != 1:
        raise TargetChanged(f"expected exactly one {what}; the server answered {len(found)}")
    return found[0]


def changed_only(before: dict[str, str | None], after: dict[str, str | None], *,
                 target: str, gone: bool = False, new: bool = False) -> None:
    """After a write, check that exactly `target` changed among its
    neighbours (ident to condition). `gone`: it was deleted; `new`: it was
    created. Anything else added, removed or changed is AfterWrite --
    reported, since the write already happened, so the user hears of it,
    and never asked again, which would write a second time."""
    before = {norm(k): v for k, v in before.items()}
    after = {norm(k): v for k, v in after.items()}
    key = norm(target)
    added = set(after) - set(before)
    removed = set(before) - set(after)
    changed = {k for k in set(before) & set(after) if before[k] != after[k]}
    expect_added = {key} if new else set()
    expect_removed = {key} if gone else set()
    if added != expect_added or removed != expect_removed or changed - {key}:
        raise AfterWrite(
            f"more changed than the one item: added {sorted(added)}, removed {sorted(removed)}, "
            f"changed {sorted(changed)}")
