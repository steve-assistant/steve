"""What every fake and every template's store is, so one suite tests them all.

The contract suites (one per class, added as each engine lands) drive every
provider through core's write path against a fake. To test the cases that
matter -- two items that both match a filter, an event changed between the
read and the write, a server that ignores a filter -- a suite must set the
fake up and look inside it the same way whatever the fake is: our IMAP
server, Radicale behind its layer, the MCP fake, or a template's in-memory
store. So each is a `World`:

- `seed` puts items in, behind the provider's back;
- `mutate` changes one, as another device would between read and write;
- `snapshot` is the whole store, compared before and after a write to
  check that exactly one item changed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class World(ABC):
    @abstractmethod
    def seed(self, klass: str, items: list[dict[str, Any]]) -> list[str]:
        """Put `items` into the store; their native ids, in order."""

    @abstractmethod
    def mutate(self, klass: str, native_id: str, **changes: Any) -> None:
        """Change one item as another client would."""

    @abstractmethod
    def snapshot(self, klass: str) -> dict[str, Any]:
        """Every item of `klass`, by its identifying field, as a comparable
        value: two snapshots are equal exactly when nothing changed. A
        message with no Message-ID (a classic Outlook draft, until it is
        sent) is filed under the native id it was made with, normalised
        as `exact.norm` does."""


def changed(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    """The identifying fields that differ between two snapshots: added,
    removed, or changed."""
    return {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
