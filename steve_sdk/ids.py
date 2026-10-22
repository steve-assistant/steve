"""An item's id says which account it came from: `<class>:<account>:<native>`.

A tool took `mailbox="gmail"` beside each id, and the model had to carry
both: an id from one mailbox handed to another's call was read there, or
failed with a provider's own error. With several accounts of one kind that
cannot work at all. So every id names its class and its account key, and a
later call finds the account from the id alone.

The account part is the opaque key the install made (`core/modules.py`), not
the name the user gave, so renaming an account breaks no id. The native part
is the provider's own id, base64url-encoded without padding, since a
Message-ID or an IMAP folder name can hold a colon. Ids are made and read
here only; tests/unit/test_steve_sdk.py round-trips them.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

CLASSES = ("mail", "calendar", "files", "tasks")

# Made by core (`core/modules.new_key`); lower-case letters and digits only,
# so it can never hold the separator.
ACCOUNT_KEY = re.compile(r"^[a-z0-9]{4,32}$")


@dataclass(frozen=True)
class Id:
    klass: str
    account: str
    native: str

    def __str__(self) -> str:
        return make(self.klass, self.account, self.native)


def _encode(native: str) -> str:
    return base64.urlsafe_b64encode(native.encode("utf-8")).decode("ascii").rstrip("=")


def _decode(text: str) -> str:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def make(klass: str, account: str, native: str) -> str:
    if klass not in CLASSES:
        raise ValueError(f"not a connector class: {klass!r}")
    if not ACCOUNT_KEY.match(account):
        raise ValueError(f"not an account key: {account!r}")
    if not native:
        raise ValueError("an id needs the provider's own id")
    return f"{klass}:{account}:{_encode(native)}"


def parse(text: str) -> Id | None:
    """The parts of an id this module made, or None for anything else -- an
    old provider id, a handle, or text a model invented."""
    parts = (text or "").strip().split(":")
    if len(parts) != 3:
        return None
    klass, account, encoded = parts
    if klass not in CLASSES or not ACCOUNT_KEY.match(account) or not encoded:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        return None
    try:
        native = _decode(encoded)
    except (ValueError, UnicodeDecodeError):
        return None
    return Id(klass, account, native) if native else None
