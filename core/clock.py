"""The user's clock, and SQLite's: the one place each is read.

Core decides what Steve may spend today (`core/spend.py`), and today is the
user's day. A cap counted from UTC midnight resets at eight in the evening in
New York and at nine in the morning in Tokyo; the tray's spend figure once
did exactly that (`agent/sqltime.py`). The zone and SQLite's time format
lived in `agent/zones.py` and `agent/sqltime.py`, which core may not import:
whatever the night may change, core must not depend on. So they live here,
and those two hand their calls on. A test that pins the zone pins
`local_zone` here, and every reader follows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

FORMAT = "%Y-%m-%d %H:%M:%S"


def local_zone() -> ZoneInfo:
    """The zone the user is in: the machine's own, by its IANA name, so
    daylight saving is right on both sides of a change. UTC when the
    machine's zone has no IANA name.

    The one place it is asked. Nine helpers each asked tzlocal, with
    different fallbacks, and a test or an eval that pinned the zone moved
    only some of them; pinning this moves them all.

    Until step 2 the zone was one town's, fixed in code (`agent/home.py`),
    and a user anywhere else was told that town's hours."""
    from tzlocal import get_localzone

    try:
        return ZoneInfo(str(get_localzone()))
    except Exception:  # noqa: BLE001 - a clock in UTC beats no clock
        return ZoneInfo("UTC")


def stamp(moment: datetime) -> str:
    """A moment as SQLite UTC text. A naive moment is read as local time,
    which is what `datetime.now()` hands back."""
    return moment.astimezone(UTC).strftime(FORMAT)


def moment(text: str) -> datetime:
    """SQLite UTC text (a `datetime('now')` column) as an aware moment."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)
