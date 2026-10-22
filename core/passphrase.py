"""The passphrase: what makes a high-risk yes in the app the user's.

Until step 4 the app's yes needed nothing more than a click, even to spend
money (core/monitor.py's `LOCAL`, now gone): the app was "this laptop", and that was
trusted. But whoever sits at an unlocked laptop, or anything that can drive
the app's window, can click. So a high-risk card answered in the app takes
the passphrase as well, every time, checked here, by the backend, against
what was chosen -- never by the app.

What holds:
- Only a hash is kept: scrypt, with a salt of its own, in the data folder
  (`path`). The passphrase itself is never written down.
- Five wrong tries lock it for 15 minutes, counted in the same file, so a
  restart does not reset the count.
- Guesses made at once are each counted: one lock spans a try's read, hash
  and write (`_TURN`). Without it, ten wrong guesses sent together each read
  a count of 0 and wrote 1, a late write could lift a lock another had just
  set, and the right passphrase was then let in.
- Changing it takes the old one. A forgotten one is reset with a glance
  at Windows Hello (`core/hello.reset_passphrase`) or with Windows' own
  sign-in (`core/winsignin.py`).
- Steve remembers that one was set (`core/kept.py`), so its file deleted
  or swapped reads as locked (`state` is "gone"), never as no
  passphrase. Until step 11, deleting the file let whoever sat at an
  unlocked desk choose their own, which then answered every high-risk
  re-ask (the audit's E1).

Binding a passphrase to the one card it was given for is
`core/monitor.accept_passphrase`'s.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from pathlib import Path

from core import kept, paths, settingsfile

MIN_CHARS = 8
TRIES = 5
LOCKED_S = 15 * 60
# scrypt's cost: 32 MB and about a tenth of a second a try on a laptop.
COST = {"n": 2 ** 15, "r": 8, "p": 1}
_MAXMEM = 64 * 1024 * 1024
# Held across each read, hash and write of the file (`check`, `choose`,
# `reset`); re-entered when `choose` checks the old passphrase. The worker
# is the one process that writes it.
_TURN = threading.RLock()


# Each change of the file's shape, oldest first (core/settingsfile.py).
STEPS = (settingsfile.stamped,)


def path() -> Path:
    return paths.data_dir() / "passphrase.json"


GONE = ("Steve's passphrase file was removed or changed, so the passphrase is locked: "
        "reset it with Windows Hello or your Windows sign-in")


def state() -> str:
    """One of "unset", "set" or "gone": one was set, and its file is missing
    or is not the one Steve kept."""
    return _now()[0]


def is_set() -> bool:
    """Whether a passphrase was set: one gone counts, so it is still asked
    for, and only a reset replaces it."""
    return state() != "unset"


def _now() -> tuple[str, dict | None]:
    held = _read()
    remembered = kept.get("passphrase")
    if remembered is None:
        if held is not None:
            # Set before Steve remembered it: remembered from now on.
            kept.keep("passphrase", _digest(held))
        return ("unset" if held is None else "set"), held
    if held is not None and _digest(held) == remembered:
        return "set", held
    return "gone", None


def _digest(held: dict) -> str:
    return kept.digest(held["salt"], held["hash"])


def choose(new: str, *, old: str | None = None, now: float | None = None) -> str | None:
    """Make `new` the passphrase; why not, or None. Changing one takes the
    old one, under the same count of tries as a card."""
    if why := unfit(new):
        return why
    with _TURN:
        held_now = state()
        if held_now == "gone":
            return GONE
        if held_now == "set" and not old:
            return "changing the passphrase needs the old one"
        if held_now == "set" and (why := check(old or "", now=now)):
            return why
        _keep(new)
    return None


def reset(new: str) -> str | None:
    """Make `new` the passphrase in place of a forgotten one, lifting any
    lock; why not, or None. It takes no old one: its one caller,
    `core/hello.reset_passphrase`, has a Windows Hello signature in its
    place, from a key that was registered with the old one."""
    if why := unfit(new):
        return why
    with _TURN:
        _keep(new)
    return None


def unfit(new: str) -> str | None:
    """Why `new` can't be a passphrase, or None."""
    if len(new or "") < MIN_CHARS:
        return f"a passphrase needs at least {MIN_CHARS} characters"
    return None


def _keep(new: str) -> None:
    salt = os.urandom(16)
    held = {"salt": salt.hex(), **COST, "hash": _hash(new, salt, COST).hex(),
            "failed": 0, "locked_until": 0}
    _write(held)
    kept.keep("passphrase", _digest(held))


def check(given: str, *, now: float | None = None) -> str | None:
    """Why `given` is not the passphrase, or None when it is."""
    now = time.time() if now is None else now
    with _TURN:
        held_now, held = _now()
        if held_now == "gone":
            return GONE
        if held is None:
            return "no passphrase is set yet: choose one first"
        if held["locked_until"] > now:
            minutes = int((held["locked_until"] - now) // 60) + 1
            return f"too many wrong tries: try again in {minutes} minute{'s' * (minutes != 1)}"
        # Nothing typed is not a guess: a click before the box is filled would
        # otherwise spend one of the five tries.
        if not given:
            return "this one needs your passphrase"
        cost = {key: held[key] for key in COST}
        if hmac.compare_digest(_hash(given or "", bytes.fromhex(held["salt"]), cost),
                               bytes.fromhex(held["hash"])):
            if held["failed"]:
                _write({**held, "failed": 0, "locked_until": 0})
            return None
        failed = held["failed"] + 1
        if failed >= TRIES:
            _write({**held, "failed": 0, "locked_until": now + LOCKED_S})
            return (f"that is not the passphrase, and {TRIES} wrong tries lock it for "
                    f"{LOCKED_S // 60} minutes")
        _write({**held, "failed": failed})
        return f"that is not the passphrase ({TRIES - failed} more tries before it locks)"


def _hash(text: str, salt: bytes, cost: dict) -> bytes:
    return hashlib.scrypt(text.encode("utf-8"), salt=salt, n=cost["n"], r=cost["r"],
                          p=cost["p"], maxmem=_MAXMEM, dklen=32)


def _read() -> dict | None:
    """What is held, or None when nothing is -- or when the file is not
    one this wrote, which then counts as no passphrase rather than as
    one any answer matches."""
    try:
        held = settingsfile.load(path(), STEPS)
        if held is None:
            return None
        bytes.fromhex(held["salt"]), bytes.fromhex(held["hash"])
        if not all(isinstance(held[key], int) and held[key] > 0 for key in COST):
            return None
        held["failed"] = int(held.get("failed") or 0)
        held["locked_until"] = float(held.get("locked_until") or 0)
        return held
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write(held: dict) -> None:
    # A newer Steve's file raises Newer here: a failed attempt that can't be
    # counted is not let through uncounted.
    settingsfile.save(path(), held, STEPS, private=True)
