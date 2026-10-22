"""Windows Hello: a high-risk yes, or a sign-in, with a glance.

The passphrase (core/passphrase.py) proves the person at the machine, but
it is typed at every high-risk card and every sign-in, and the app's
sign-in ends at each Windows lock. Typed that often, a passphrase gets
short, or written down. So the app can use Windows Hello instead: a key
Windows makes and keeps, which signs only after the user's face, finger or
PIN. Its private half never leaves Windows; the backend holds the public
half and checks what was signed.

What holds:
- **Registering a key takes the passphrase.** A key registered by anything
  that holds the backend's token would let it approve spending on its own,
  which is what the passphrase exists to stop (decision 3). Forgetting the
  key takes it too.
- **A signature answers one challenge, once.** The backend makes the
  challenge: what it is for (`signin`, `yes` to one card, or `reset` of a
  forgotten passphrase), what it binds
  (the card's id and a digest of its arguments as they are), and a nonce.
  Each nonce is spent by its first answer, right or wrong, and lapses
  after two minutes, so a signature seen once can't be used again, nor one
  for another card or for a card whose arguments changed since.
- **The message names Steve:** a signature for another program's challenge
  never reads as one of these.
- **A forgotten passphrase is reset with a glance** (`reset_passphrase`).
  The key was registered with the old passphrase, so its signature stands
  in for it here as at a yes. Without the key, Windows' own sign-in does
  (`core/winsignin.py`). Until step 11 the way without it was deleting
  passphrase.json, which anyone at an unlocked desk could do.
- **The key is remembered** (`core/kept.py`): a `hello.json` replaced with
  another key signs nothing, so a key of someone else's can't reset the
  passphrase.

Windows Hello's keys are RSA, 2048 bits, and sign with PKCS #1 v1.5 over
SHA-256. The app asks Windows to make and use the key
(`ui/src-tauri/src/hello.rs`); binding a yes to its card is
`core/monitor.accept_hello`'s.
"""

from __future__ import annotations

import base64
import secrets
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

from core import identity, kept, passphrase, paths, settingsfile

# How long a challenge waits for its signature: the Hello prompt, a glance,
# and the time to look for the camera.
CHALLENGE_S = 120
# Open challenges at most; past it the oldest go. One app asks for one at a
# time; this only stops an endless pile.
OPEN_MAX = 16
MIN_BITS = 2048
PURPOSES = ("signin", "yes", "reset")

# Each change of the file's shape, oldest first (core/settingsfile.py).
STEPS = (settingsfile.stamped,)

# nonce -> (purpose, binding, until), in this process's memory only: a
# challenge outlives no restart.
_open: dict[str, tuple[str, str, float]] = {}


def path() -> Path:
    return paths.data_dir() / "hello.json"


def key_name() -> str:
    """The name the app gives Windows for this install's key."""
    from core.sandbox import profiles

    return identity.hello_key(profiles.install())


def is_set() -> bool:
    return _key() is not None


def register(public_key: str, given: str) -> str | None:
    """Keep `public_key` (base64 of its SubjectPublicKeyInfo) as the one
    Hello key; why not, or None. Takes the passphrase."""
    if why := passphrase.check(given):
        return why
    try:
        key = load_der_public_key(base64.b64decode(public_key, validate=True))
    except ValueError:
        return "that is not a key Windows Hello made: set it up again"
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < MIN_BITS:
        return "that is not a key Windows Hello made: set it up again"
    settingsfile.save(path(), {"public_key": public_key}, STEPS, private=True)
    kept.keep("hello", kept.digest(public_key))
    return None


def forget(given: str) -> str | None:
    """Forget the key; why not, or None. Takes the passphrase."""
    if why := passphrase.check(given):
        return why
    path().unlink(missing_ok=True)
    kept.drop("hello")
    return None


def challenge(purpose: str, binding: str, *, now: float | None = None) -> tuple[str, bytes]:
    """A nonce, and the message the key is to sign for it."""
    if purpose not in PURPOSES:
        raise ValueError(f"no such purpose: {purpose}")
    now = time.time() if now is None else now
    for stale in [n for n, (_, _, until) in _open.items() if until <= now]:
        del _open[stale]
    while len(_open) >= OPEN_MAX:
        del _open[next(iter(_open))]
    nonce = secrets.token_urlsafe(24)
    _open[nonce] = (purpose, binding, now + CHALLENGE_S)
    return nonce, message(purpose, binding, nonce)


def message(purpose: str, binding: str, nonce: str) -> bytes:
    return f"{identity.NAME} {purpose} {binding} {nonce}".encode()


def verify(nonce: str, signature: str, purpose: str, binding: str, *,
           now: float | None = None) -> str | None:
    """Why `signature` does not answer this challenge, or None when it
    does. The nonce is spent either way."""
    now = time.time() if now is None else now
    opened = _open.pop(nonce, None)
    if opened is None or opened[2] <= now:
        return "that Windows Hello prompt ran out of time: try again"
    if opened[:2] != (purpose, binding):
        return "that signature was for something else: try again"
    key = _key()
    if key is None:
        return "Windows Hello is not set up for Steve: use the passphrase"
    try:
        key.verify(base64.b64decode(signature, validate=True), message(purpose, binding, nonce),
                   padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError):
        return "Windows Hello's signature did not match: set Hello up again in Settings"
    return None


def reset_passphrase(new: str, nonce: str, signature: str) -> str | None:
    """`new` in place of a forgotten passphrase, on a signature for a
    `reset` challenge; why not, or None. A new passphrase that won't do is
    refused before the signature is checked, which would spend its nonce
    and cost another glance."""
    if why := passphrase.unfit(new):
        return why
    if not is_set():
        return ("Windows Hello is not set up for Steve: reset the passphrase with your "
                "Windows sign-in")
    if why := verify(nonce, signature, "reset", ""):
        return why
    return passphrase.reset(new)


def _key() -> rsa.RSAPublicKey | None:
    """The registered key, or None -- also for a file this didn't write, and
    for one holding a key other than the one Steve kept."""
    try:
        held = settingsfile.load(path(), STEPS)
        if held is None:
            return None
        public = held["public_key"]
        key = load_der_public_key(base64.b64decode(public, validate=True))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < MIN_BITS:
        return None
    remembered = kept.get("hello")
    if remembered is None:
        # Registered before Steve remembered it: remembered from now on.
        kept.keep("hello", kept.digest(public))
    elif kept.digest(public) != remembered:
        return None
    return key
