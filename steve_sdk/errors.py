"""One set of errors for every provider, so core says each once.

Gmail raised Google's `HttpError`, Outlook a COM error or its own
`OutlookOff`, the password mailbox `NotConnected` or an `imaplib` error, and each
caller caught its own few and turned them into words of its own. A user
with two mailboxes heard the same trouble two ways, and a new provider had
to learn every caller's list. A provider raises one of these; core words
each kind once (`agent/connectors.py`), and the tools, the app and chat say
that sentence.
"""

from __future__ import annotations


class ConnectorError(Exception):
    """Something a provider could not do. `kind` names which, for core."""

    kind = "error"

    def __init__(self, detail: str = "", *, fix: str = "") -> None:
        super().__init__(detail or self.kind)
        self.detail = detail
        # The https page that fixes it, where there is one: the API to turn
        # on in the user's own Google Cloud project. Core keeps it with the
        # account's health, and the app opens it (`core/health.py`).
        self.fix = fix


class SignInNeeded(ConnectorError):
    """The account's sign-in has lapsed or was revoked: the user signs in again."""

    kind = "sign_in"


class WrongPassword(ConnectorError):
    """The server refused the password: the user enters a new one."""

    kind = "password"


class NotConnected(ConnectorError):
    """The account is not set up, or is missing what it needs on this computer."""

    kind = "not_connected"


class Transient(ConnectorError):
    """The server did not answer, or answered "later": worth trying again."""

    kind = "transient"


class Unsupported(ConnectorError):
    """The provider cannot do this at all (no such capability)."""

    kind = "unsupported"


class TargetChanged(ConnectorError):
    """The item to be written is not the one that was named or shown: it
    changed, moved, or more or fewer than one answered. Nothing was written."""

    kind = "target_changed"


class NeedsCard(ConnectorError):
    """An update or delete on an item that cannot be written conditionally,
    with no card behind it. Core asks the user first; nothing was written."""

    kind = "needs_card"


class AfterWrite(ConnectorError):
    """The write went, and the check after it did not find just that one
    change: something else changed too, or the item does not read back as
    written. Unlike TargetChanged, something was written, so the user is
    told what was found and is not asked again: a second yes would write a
    second time.

    The check after a write once raised TargetChanged, whose words say
    nothing was written; a calendar synced by a phone in that second made a
    move that went read as one that did not, and its card was asked again."""

    kind = "after_write"


KINDS = {cls.kind: cls for cls in (
    SignInNeeded, WrongPassword, NotConnected, Transient, Unsupported, TargetChanged, NeedsCard,
    AfterWrite)}
