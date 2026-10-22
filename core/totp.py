"""Authenticator codes: what makes a yes theirs when the words came over a
remote channel.

An approval is the words "YES MAPLE" arriving on their channels, and a remote
channel's servers or linked devices could put those words there
(docs/security-model.md). For what the user judged high-risk on 1 October 2026
-- spending money, and calling anyone but them while Steve had a phone -- the
yes must also carry the six digits from the authenticator app on their phone: a
secret no channel holds, checked here, on the laptop.

RFC 6238 (TOTP: HMAC-SHA1, 30-second steps, six digits) on the standard
library, so no dependency carries the check. A code is good for the step it
was made in and one either side. Single use and binding to one card are
`core/monitor.py`'s, which records each step it accepts.

Wrong codes are counted (`wrong`), as the passphrase's wrong tries are
(core/passphrase.py): five within a day and no code is taken for 15 minutes,
counted in a file in the data folder, so a restart does not reset it. Before
this nothing counted them, and with three codes good at any moment, about
30 guesses a minute on a channel someone else holds found one in a day
about one time in eight (step 11's audit). The count spans a try's read,
check and write (`TRYING`), so guesses made at once are each counted.

Its review found the count failing open: a file that could not be read
counted zero, and a write that failed raised after the guess was checked.
Now a count that cannot be read or written stops codes too, and a stop that
comes less than a day after the last one lasts twice as long, up to a day.
The second review found a right code starting the count again: each code
the user sent bought a guesser four more tries. So the count is of wrong
codes over the last day, and only a stop clears it.

The app sets it up (agent/worker/approvers.py): it shows the secret once, as a
QR code, and keeps it only once a code made from it comes back. In a checkout
without the app:

    python -m core.totp setup     # once, at the laptop: make the secret
    python -m core.totp check     # type a code from the app, see it match

Either way the secret goes into the keychain and nowhere else -- never a chat.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _random
import struct
import threading
import time
from pathlib import Path
from urllib.parse import quote

from core import identity, paths, settingsfile

SECRET_KEY = "APPROVAL_TOTP_SECRET"
STEP_S = 30
DIGITS = 6
WINDOW = 1  # steps either side of now
ISSUER = identity.NAME

# Wrong codes within a day before none is taken, and for how long then: the
# passphrase's numbers. A stop less than a day after the last one ended lasts
# twice as long, up to a day (`_next_pause`).
TRIES = 5
PAUSED_S = 15 * 60
DAY_S = 24 * 60 * 60
# Held across each try's read of the count, its check and its write
# (`core/monitor.accept_code`), as the passphrase's `_TURN` is.
TRYING = threading.RLock()


def _rolling(count: dict) -> dict:
    """Version 1 to 2. Step 11's second review replaced `failed`, the wrong
    codes in a row, with `wrong_at`, the times of those within a day, and
    left the version at 1. Each Steve then read the other's count as none
    (its third review): the update found no `wrong_at`, and an older one,
    after a downgrade, found no `failed`. When each wrong code came is not
    known, so each counts from now: none is lost, and a day is the most
    any is held longer than it would have been."""
    failed = count.get("failed") or 0
    if isinstance(failed, bool) or not isinstance(failed, int) or failed < 0:
        raise TypeError(f"failed is {failed!r}")
    kept = {k: v for k, v in count.items() if k != "failed"}
    kept.setdefault("wrong_at", [time.time()] * min(failed, TRIES))
    return kept


# Each change of the count file's shape, oldest first (core/settingsfile.py).
STEPS = (settingsfile.stamped, _rolling)
# Stops for a count that could not be read or written, by count file: until
# when, and why (`_uncounted`).
_UNCOUNTED: dict[Path, tuple[float, str]] = {}


def new_secret() -> str:
    """160 random bits, base32, as authenticator apps take them."""
    return base64.b32encode(_random.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    """RFC 4226: HMAC-SHA1 over the counter, dynamically truncated."""
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def step_at(when: float) -> int:
    return int(when) // STEP_S


def code_at(secret: str, when: float) -> str:
    return hotp(_key(secret), step_at(when))


def matching_step(secret: str, code: str, when: float | None = None) -> int | None:
    """The step `code` belongs to, within WINDOW of now -- or None."""
    code = (code or "").replace(" ", "")
    # ASCII digits alone: `hmac.compare_digest` raises on any other text.
    if len(code) != DIGITS or not (code.isascii() and code.isdigit()):
        return None
    now = step_at(time.time() if when is None else when)
    key = _key(secret)
    for step in range(now - WINDOW, now + WINDOW + 1):
        if hmac.compare_digest(hotp(key, step), code):
            return step
    return None


def uri(secret: str, account: str = "Steve") -> str:
    """The otpauth:// address an authenticator app reads from a QR code."""
    label = quote(f"{ISSUER}:{account}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP_S}")


def tries_path() -> Path:
    return paths.data_dir() / "code_tries.json"


def paused_until(now: float | None = None) -> float | None:
    """When codes are taken again, as seconds since the epoch; None while
    they are taken. The app shows it (agent/worker/approvers.py)."""
    stop = _stopped(time.time() if now is None else now)
    return stop and stop[0]


def paused(now: float | None = None) -> str | None:
    """Why no code is taken now, or None."""
    now = time.time() if now is None else now
    stop = _stopped(now)
    if stop is None:
        return None
    until, why = stop
    return (f"{why}, so a yes to spending is not taken by message or voice for "
            f"{_span(until - now)} more: answer it in the app")


def wrong(now: float | None = None) -> str:
    """Count a wrong code; what to tell whoever sent it."""
    now = time.time() if now is None else now
    with TRYING:
        held = _count()
        if held is None:
            return _uncounted(now)
        # Those within a day, a clock set back counting its later ones too.
        recent = [t for t in held["wrong_at"] if now - t < DAY_S] + [now]
        if len(recent) < TRIES:
            if not _save({**held, "wrong_at": recent}):
                return _uncounted(now)
            return (f"that code does not match -- check it and send it again ({TRIES - len(recent)}"
                    f" more wrong within a day and codes stop for "
                    f"{_span(_next_pause(held, now))})")
        pause = _next_pause(held, now)
        if not _save({"wrong_at": [], "paused_until": now + pause, "pause_s": pause}):
            return _uncounted(now)
        return (f"that code does not match, and after {TRIES} wrong codes within a day a yes to "
                f"spending is not taken by message or voice for {_span(pause)}: answer it in "
                "the app")


def _next_pause(held: dict, now: float) -> float:
    """How long the next stop lasts: PAUSED_S, or twice the last one when it
    ended less than a day ago, up to a day. Fifteen minutes alone still let
    480 guesses a day through, so one stop after another grows."""
    if held["pause_s"] and now - held["paused_until"] < DAY_S:
        return min(2 * held["pause_s"], DAY_S)
    return PAUSED_S


def _stopped(now: float) -> tuple[float, str] | None:
    """Until when no code is taken, and why; None while they are. A count
    that cannot be read stops them too, for PAUSED_S at a time."""
    with TRYING:
        held = _count()
        if held is None:
            _uncounted(now)
        stops = [(held["paused_until"], f"{TRIES} wrong codes within a day")] if held else []
        stops.append(_UNCOUNTED.get(tries_path(), (0.0, "")))
        until, why = max(stops)
        return (until, why) if until > now else None


def _uncounted(now: float) -> str:
    """The count cannot be read or written: no code is taken for PAUSED_S,
    held in memory for this data folder. Counting zero instead, as the count
    did until step 11's review, let a file someone made unreadable, or a
    disk that refused the write, take every guess uncounted."""
    path = tries_path()
    until, _ = _UNCOUNTED.get(path, (0.0, ""))
    if until <= now:
        _UNCOUNTED[path] = (now + PAUSED_S, "wrong codes cannot be counted just now")
    return (f"that code was not checked: wrong codes cannot be counted just now, so a yes to "
            f"spending is not taken by message or voice for {_span(PAUSED_S)}: answer it "
            "in the app")


def _span(seconds: float) -> str:
    minutes = int(max(seconds, 0) // 60) + (seconds % 60 > 0)
    if minutes < 120:
        return f"{minutes} minute{'s' * (minutes != 1)}"
    hours = -(-minutes // 60)
    return f"{hours} hours"


def _count() -> dict | None:
    """The count as held: no file is no wrong codes; None when it cannot be
    read, holds what this did not write, or a newer Steve wrote it, whose
    count may be kept another way (`_stopped` then stops codes). Deleting
    the file resets the count, but whoever can write the data folder can
    do worse."""
    try:
        held = settingsfile.load(tries_path(), STEPS, newer_ok=False) or {}
        wrong_at = held.get("wrong_at") or []
        if not isinstance(wrong_at, list):
            raise TypeError("wrong_at is not a list")
        return {"wrong_at": [float(t) for t in wrong_at][-TRIES:],
                "paused_until": float(held.get("paused_until") or 0),
                "pause_s": float(held.get("pause_s") or 0)}
    except (OSError, ValueError, TypeError, settingsfile.Newer):
        return None


def _save(count: dict) -> bool:
    """Whether the count was kept. A newer Steve's file raises Newer here,
    and a full disk OSError: either way the guess is reported uncounted."""
    try:
        settingsfile.save(tries_path(), count, STEPS, private=True)
    except (OSError, ValueError, TypeError, settingsfile.Newer):
        return False
    return True


def _main() -> None:
    import argparse

    from core import secrets

    parser = argparse.ArgumentParser(description="Authenticator codes for high-risk approvals.")
    parser.add_argument("action", choices=["setup", "check"])
    parser.add_argument("--replace", action="store_true",
                        help="make a new secret even if one exists "
                             "(the old app entry stops working)")
    args = parser.parse_args()
    existing = secrets.get(SECRET_KEY)

    if args.action == "setup":
        if existing and not args.replace:
            print("An authenticator secret is already set up. Use --replace to make a new one.")
            return
        secret = new_secret()
        secrets.set_secret(SECRET_KEY, secret)
        grouped = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
        print("Add this to your authenticator app (Google Authenticator, 1Password, ...):")
        print(f"  'Enter a setup key' -> account '{ISSUER}', key below, time-based.\n")
        print(f"    {grouped}\n")
        print(f"Or paste this into an app that takes a link:\n    {uri(secret)}\n")
        print("Then run `python -m core.totp check` and type a code from the app.")
        return

    if not existing:
        print("No authenticator secret yet: run `python -m core.totp setup` first.")
        return
    code = input("Code from the app: ").strip()
    step = matching_step(existing, code)
    print("Matches." if step is not None else "Does not match -- check the phone's clock and "
          "that you added the newest key.")


if __name__ == "__main__":
    _main()
