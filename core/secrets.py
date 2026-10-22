"""Credential access, backed by Windows Credential Manager via keyring.

Frozen. The agent is allowed to *use* credentials through this module; it is
not allowed to change how they are stored, enumerate them, or write them to
disk. `core/policy.yaml` additionally denies the coding harness any shell
command containing "keyring" or ".env", so a self-modification run cannot
exfiltrate them through a subprocess either.

Nothing here ever writes a secret to the repository. The .env fallback exists
only for values that are not secret (model ids, endpoints, feature flags).
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass

import keyring

from core import identity, logscrub, paths

SERVICE = identity.KEYCHAIN

# Every credential the system can ask for. `set_secret` refuses any other, and
# tests/unit/test_keys.py fails on code that asks for one not here: a typo'd
# key would otherwise read as never set, deep in a tool call. Two are needed
# (README); the rest belong to features the user chooses.
KNOWN_KEYS = {
    "OPENROUTER_API_KEY": "OpenRouter: Jev, the decision model",
    "OPENAI_API_KEY": "OpenAI: every model, web search and embeddings",
    "BRAVE_API_KEY": "Brave Search: optional, faster web search in OpenAI's place",
    # The shared secret of their authenticator app, for the approvals a remote
    # channel must not be able to forge (core/totp.py, core/monitor.py). Laptop only:
    # written by the app's setup (agent/worker/approvers.py), or by
    # `python -m core.totp setup` in a checkout; never typed into a chat.
    "APPROVAL_TOTP_SECRET": "Authenticator - the TOTP secret for high-risk approvals",
    # The user's own bot, made with BotFather, for Telegram as the remote
    # channel (channels/telegram.py). Given in the app, which checks it
    # with Telegram first (agent/worker/pairing.telegram_token).
    "TELEGRAM_BOT_TOKEN": "Telegram - your bot's token, from BotFather",
}


class MissingSecret(KeyError):
    """Raised when a required credential has not been configured."""

    def __init__(self, key: str) -> None:
        hint = KNOWN_KEYS.get(key, "unknown key")
        super().__init__(
            f"{key} is not set ({hint}). Store it with:\n"
            f"    python -m core.secrets set {key}"
        )


@dataclass(frozen=True)
class SecretStatus:
    key: str
    description: str
    present: bool
    source: str | None


def env_names(key: str) -> tuple[str, ...]:
    """The environment variables `key` is read from when the keychain has
    none: Steve's own name for it (`STEVE_<KEY>`), and, for a vendor's key,
    the vendor's name, which CI or a container sets.

    Any other bare name is not Steve's. The lookup read every key under its
    bare name, so `APPROVAL_TOTP_SECRET` or `BRAVE_API_KEY` in the
    user's environment -- set for another program, or by another install
    of this code -- was Steve's, against the rule that Steve reads only
    `STEVE_*` and the standard names (core/identity.py)."""
    own = key if key.startswith(identity.ENV_PREFIX) else identity.ENV_PREFIX + key
    vendor = key.endswith("_API_KEY")
    return (own, key) if vendor else (own,)


def _from_environment(key: str) -> str | None:
    """A checkout's fallback, never an install's (decision D2): an installed
    Steve reads its secrets from the Credential Manager alone, so a variable
    some other program set for the same user, `OPENAI_API_KEY` above all,
    is not quietly taken as the key the user gave Steve."""
    if paths.is_installed():
        return None
    for name in env_names(key):
        if (value := os.environ.get(name)) is not None:
            return value
    return None


def get(key: str, default: str | None = None) -> str | None:
    """Credential Manager first, environment second (`env_names`), and the
    environment only in a checkout (`_from_environment`).

    The environment fallback is what makes CI possible without a desktop
    keyring present.
    """
    try:
        if (value := keyring.get_password(SERVICE, key)) is not None:
            return logscrub.hold(value)
    except Exception:  # noqa: BLE001 - a broken backend must not be fatal
        pass
    value = _from_environment(key)
    return default if value is None else logscrub.hold(value)


def require(key: str) -> str:
    value = get(key)
    if not value:
        raise MissingSecret(key)
    return value


def kept_here() -> None:
    """Make keyring's writes stay on this computer.

    Its Windows backend writes CRED_PERSIST_ENTERPRISE, which a domain
    profile roams to every computer the user signs in to. The provider
    keys and the browser's logins went that way, while each module's
    secrets (`core/credstore`) were already LOCAL_MACHINE (step 11's
    audit). Set on the backend before each write, so it holds for the
    keyring a write uses; a backend without the setting is left alone."""
    backend = keyring.get_keyring()
    if hasattr(type(backend), "persist"):
        backend.persist = "local machine"


def set_secret(key: str, value: str) -> None:
    if key not in KNOWN_KEYS:
        raise KeyError(f"{key} is not a declared credential; add it to KNOWN_KEYS first")
    logscrub.hold(value)
    kept_here()
    keyring.set_password(SERVICE, key, value)


def delete(key: str) -> None:
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(SERVICE, key)


def status() -> list[SecretStatus]:
    """Which credentials are configured, without revealing any of them."""
    out: list[SecretStatus] = []
    for key, description in KNOWN_KEYS.items():
        source: str | None = None
        try:
            if keyring.get_password(SERVICE, key):
                source = "keyring"
        except Exception:  # noqa: BLE001
            pass
        if source is None and _from_environment(key):
            source = "env"
        out.append(SecretStatus(key, description, source is not None, source))
    return out


def _main() -> None:
    import argparse
    import getpass

    parser = argparse.ArgumentParser(description="Manage agent credentials")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p_set = sub.add_parser("set")
    p_set.add_argument("key")
    p_del = sub.add_parser("delete")
    p_del.add_argument("key")
    args = parser.parse_args()

    if args.cmd == "status":
        width = max(len(k) for k in KNOWN_KEYS)
        for s in status():
            mark = "OK " if s.present else "-- "
            where = f"({s.source})" if s.source else ""
            print(f"{mark} {s.key:<{width}}  {s.description} {where}")
    elif args.cmd == "set":
        # getpass keeps the value out of shell history and the terminal buffer.
        set_secret(args.key, getpass.getpass(f"{args.key}: "))
        print(f"stored {args.key}")
    elif args.cmd == "delete":
        delete(args.key)
        print(f"deleted {args.key}")


if __name__ == "__main__":
    _main()
