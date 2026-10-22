"""The one door to the Windows Credential Manager for module secrets.

Connector secrets were scattered: the Google sign-in was a plain JSON file
in the secrets folder (`modules/google_cloud/auth.py`), a mailbox's password and the
course feed's address were flat keys beside the OpenAI key, and a module of
someone else's could have read any of them by name. Three more traps sat in
the keyring library underneath:

- it names every key after a service's first `<key>@<service>`
  (`_compound_name`), so a `steve*` filter at uninstall misses them;
- it stores text as UTF-16, which halves the Credential Manager's
  2,560-byte limit, and Google's whole token JSON can pass even the full one;
- it cannot list a service's entries, so nothing could find them all.

So module secrets go through this file alone (tests/gate/test_module_imports.py
holds that nothing else imports `win32cred`):

- under `steve-modules/<module key>/<account key>/<NAME>` (the prefix is
  `core/identity.MODULE_SECRETS`), the module key made by the install, so a
  module that reuses another's id cannot reach its secrets;
- as UTF-8 bytes, each handed to Windows as one character: pywin32's
  `CredWrite` takes the blob only as text, which it stores as UTF-16. Bytes
  given to it raise TypeError, which the fences' stand-in accepted; the live
  check of 15 October 2026 found it, before any secret was ever saved;
- a value over 1,280 bytes (2,560 stored) split over numbered entries
  (`<target>#1`, ...), its head entry saying how many -- never moved into a
  file instead;
- listed and deleted by exact prefix.

The fences replace `STORE` with an in-memory one in every test and every
process a test starts (tests/gate/fences.py), so no test writes the real
Credential Manager.
"""

from __future__ import annotations

import threading
from typing import Protocol

from core import identity, logscrub

PREFIX = identity.MODULE_SECRETS
# Bytes of ours in one entry: CRED_MAX_CREDENTIAL_BLOB_SIZE is 2,560 stored,
# and each of our bytes is stored as two (`WindowsStore`). Windows refused
# 1,281 with error 1783, live.
MAX_BLOB = 1280
# The head of a split secret: 0xFF never occurs in UTF-8, so no stored text
# can be mistaken for it.
_SPLIT = b"\xffsplit:"


class Store(Protocol):
    """The Credential Manager's four calls, on raw bytes."""

    def read(self, target: str) -> bytes | None: ...
    def write(self, target: str, blob: bytes) -> None: ...
    def delete(self, target: str) -> None: ...
    def targets(self, prefix: str) -> list[str]: ...


class WindowsStore:
    """The real Credential Manager, through pywin32's `win32cred`: generic
    credentials, persisted on this machine for this Windows user."""

    def __init__(self) -> None:
        import win32cred  # noqa: F401 - Windows only; refused here, not at import

    @staticmethod
    def _missing(exc: Exception) -> bool:
        return getattr(exc, "winerror", None) == 1168  # ERROR_NOT_FOUND

    def read(self, target: str) -> bytes | None:
        import pywintypes
        import win32cred

        try:
            cred = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)
        except pywintypes.error as exc:
            if self._missing(exc):
                return None
            raise
        blob = cred.get("CredentialBlob") or b""
        # The UTF-16 `write` stored; each character one of our bytes again.
        return bytes(blob).decode("utf-16-le").encode("latin-1")

    def write(self, target: str, blob: bytes) -> None:
        """pywin32 takes the blob only as text and stores it as UTF-16, so
        each byte goes as the one character Latin-1 makes of it: any bytes
        at all (a split head's 0xFF too) come back exactly. Calling
        `CredWriteW` ourselves would keep the bytes as they are, but it is a
        door the fences' stand-in for `win32cred` would not hold."""
        import win32cred

        win32cred.CredWrite({
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": target,
            "UserName": identity.KEYCHAIN,
            "CredentialBlob": blob.decode("latin-1"),
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        }, 0)

    def delete(self, target: str) -> None:
        import pywintypes
        import win32cred

        try:
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC)
        except pywintypes.error as exc:
            if not self._missing(exc):
                raise

    def targets(self, prefix: str) -> list[str]:
        import pywintypes
        import win32cred

        try:
            found = win32cred.CredEnumerate(prefix + "*", 0)
        except pywintypes.error as exc:
            if self._missing(exc):
                return []
            raise
        # The filter is a wildcard; the prefix is checked exactly here.
        return sorted(c["TargetName"] for c in found
                      if c["TargetName"].startswith(prefix))


class MemoryStore:
    """A Credential Manager in a dict: the fences' stand-in, and tests'."""

    def __init__(self) -> None:
        self.entries: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def read(self, target: str) -> bytes | None:
        with self._lock:
            return self.entries.get(target)

    def write(self, target: str, blob: bytes) -> None:
        if len(blob) > MAX_BLOB:
            raise ValueError(f"{len(blob)} bytes is over the Credential Manager's {MAX_BLOB}")
        with self._lock:
            self.entries[target] = bytes(blob)

    def delete(self, target: str) -> None:
        with self._lock:
            self.entries.pop(target, None)

    def targets(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(t for t in self.entries if t.startswith(prefix))


_store: Store | None = None
_store_lock = threading.Lock()


def store() -> Store:
    global _store
    with _store_lock:
        if _store is None:
            _store = WindowsStore()
        return _store


def use(replacement: Store | None) -> None:
    """Put another store behind every call (the fences, a test); None puts
    the real one back on next use."""
    global _store
    with _store_lock:
        _store = replacement


def target(module_key: str, account_key: str, name: str) -> str:
    return prefix(module_key, account_key, name)


def prefix(*parts: str) -> str:
    """A name under `PREFIX` from its parts: a module's key, then an
    account's, then a secret's. Fewer parts name what `forget` clears."""
    for part in parts:
        if not part or "/" in part or "#" in part or "*" in part:
            raise ValueError(f"not a part of a secret's name: {part!r}")
    return "/".join((PREFIX, *parts))


def _parts(blob: bytes) -> int | None:
    if not blob.startswith(_SPLIT):
        return None
    try:
        return int(blob[len(_SPLIT):].decode("ascii"))
    except ValueError:
        return None


def read(name: str) -> str | None:
    """The secret stored under `name`, or None."""
    blob = store().read(name)
    if blob is None:
        return None
    if (count := _parts(blob)) is not None:
        pieces = [store().read(f"{name}#{i}") for i in range(1, count + 1)]
        if any(p is None for p in pieces):
            return None  # a split secret missing a part is not half a secret
        blob = b"".join(pieces)  # type: ignore[arg-type]
    return logscrub.hold(blob.decode("utf-8"))


def write(name: str, value: str) -> None:
    """Store `value` under `name`, split if it is over the limit; whatever
    was there before, parts included, is replaced."""
    if not name.startswith(PREFIX + "/"):
        raise ValueError(f"module secrets live under {PREFIX}/, not {name!r}")
    blob = logscrub.hold(value).encode("utf-8")
    delete(name)
    if len(blob) <= MAX_BLOB:
        store().write(name, blob)
        return
    pieces = [blob[i:i + MAX_BLOB] for i in range(0, len(blob), MAX_BLOB)]
    for i, piece in enumerate(pieces, 1):
        store().write(f"{name}#{i}", piece)
    store().write(name, _SPLIT + str(len(pieces)).encode("ascii"))


def delete(name: str) -> None:
    """Forget `name` and any parts it was split over."""
    store().delete(name)
    for leftover in store().targets(name + "#"):
        store().delete(leftover)


def names(prefix: str) -> list[str]:
    """Every secret stored under `prefix`, parts not listed apart."""
    return [t for t in store().targets(prefix) if "#" not in t.rsplit("/", 1)[-1]]


def forget(prefix: str) -> int:
    """Delete every secret under `prefix` (a module's, an account's); how many.
    The prefix ends at a whole part, so forgetting key `ab12` leaves `ab123`."""
    if not prefix.startswith(PREFIX + "/"):
        raise ValueError(f"only module secrets are forgotten here, not {prefix!r}")
    found = names(prefix.rstrip("/") + "/")
    for name in found:
        delete(name)
    return len(found)
