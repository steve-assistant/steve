"""Their saved logins: which sites, on which domains, how freely the agent may
use them, and the secrets themselves -- none of which a model ever holds.

The maintainer, 1 October 2026: the user logs in to a site the first time themselves, at the
laptop (the live view, `agent/browser/live.py`); after that the agent may
use the saved session, or have their username and password typed into the
right fields -- without ever seeing them -- and each login is one it must
ask them before using (`ask`, the default) or may just use (`free`).

Three stores, each for what it holds:

- **Sites** -- a name, the domains the login belongs to, its tier -- in
  `data/browser/sites.json`. No secrets; a list the tray can show.
- **Credentials** in the Windows Credential Manager, under a service of
  their own (`SERVICE`), not the agent's key list (`core/secrets.py`): read
  only by `credentials`, whose one caller hands them to the driver to type
  (`agent/browser/session.py`), never to a tool's result.
- **Sessions** -- the cookies and storage a login leaves -- encrypted with
  Windows DPAPI, so only their Windows account on this laptop can read them,
  under `data/browser/sessions/`.

Where a login may be used is `on_site`: the page's host must be one of the
site's domains or below one, on a label boundary -- `sso.contoso.example` is
Contoso's, `evilcontoso.example` and `contoso.example.example.com` are not. Writes come only
from the live view, which is theirs; a gate test pins that.

Built as a class so the tests can hand it a folder, a keychain and a cipher
of their own: nothing here reaches their real keychain from a test. Frozen.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core import identity, logscrub, paths, settingsfile
from core.browser.driver import on_site

SERVICE = identity.BROWSER_LOGINS
TIERS = ("ask", "free")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAX_DOMAINS = 12


@dataclass(frozen=True)
class Site:
    name: str
    domains: tuple[str, ...]
    tier: str = "ask"
    has_password: bool = False
    has_session: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "domains": list(self.domains), "tier": self.tier,
                "has_password": self.has_password, "has_session": self.has_session}


def domain_refusal(domain: str) -> str | None:
    """Why `domain` cannot be a login's domain, or None. A registrable name
    with at least one dot: not an address, not a bare suffix like "edu"."""
    d = (domain or "").strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(d)
        return f"{d} is an address, not a domain"
    except ValueError:
        pass
    if not _DOMAIN.match(d):
        return f"{d!r} is not a domain like example.com"
    if d.endswith((".local", ".localhost", ".internal", ".lan")):
        return f"{d} is a local name"
    # A public suffix -- herokuapp.com, github.io, co.uk -- is one anyone can
    # host a page under: a login saved for it would be typed into a
    # look-alike at evil.herokuapp.com. The site's own name under it is fine.
    if not _suffixes()(d).domain:
        return (f"{d} is shared by everyone hosting under it; give the site's own domain, "
                f"like yoursite.{d}")
    return None


_extract: list = []


def _suffixes():
    """The public-suffix list, from tldextract's bundled copy -- never
    fetched, so a check here never reaches the network."""
    if not _extract:
        import tldextract

        _extract.append(tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None,
                                              include_psl_private_domains=True))
    return _extract[0]


def _dpapi_protect(data: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptProtectData(data, "steve browser session", None, None, None, 0)


def _dpapi_unprotect(blob: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]


class _Keyring:
    """The Windows Credential Manager, under the vault's own service."""

    def get(self, key: str) -> str | None:
        import keyring

        return keyring.get_password(SERVICE, key)

    def set(self, key: str, value: str) -> None:
        import keyring

        from core import secrets

        secrets.kept_here()
        keyring.set_password(SERVICE, key, value)

    def delete(self, key: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        # Not there is gone. Any other failure is said: swallowed, a login
        # the user forgot kept its password in the Credential Manager while
        # the app said it was forgotten.
        with contextlib.suppress(PasswordDeleteError):
            keyring.delete_password(SERVICE, key)


class Vault:
    def __init__(self, root: Path | None = None, keyring=None,
                 protect: Callable[[bytes], bytes] = _dpapi_protect,
                 unprotect: Callable[[bytes], bytes] = _dpapi_unprotect) -> None:
        self.root = Path(root) if root is not None else paths.data_dir() / "browser"
        # `is None`, not `or`: an empty keychain is falsy, and `or` swapped
        # the tests' own for their real one (caught by test_live_guards).
        self.keyring = keyring if keyring is not None else _Keyring()
        self.protect = protect
        self.unprotect = unprotect

    # -- sites ---------------------------------------------------------------

    @property
    def _sites_file(self) -> Path:
        return self.root / "sites.json"

    def _session_file(self, name: str) -> Path:
        return self.root / "sessions" / f"{name}.bin"

    def _raw(self) -> dict:
        try:
            return json.loads(self._sites_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, raw: dict) -> None:
        # Whole or not at all: `_raw` reads a torn file as no logins, and
        # the next save wrote that back over every one.
        settingsfile.write_whole(
            self._sites_file, (json.dumps(raw, indent=1, sort_keys=True) + "\n").encode("utf-8"))

    def sites(self) -> list[Site]:
        found = []
        for name, spec in sorted(self._raw().items()):
            found.append(Site(name=name, domains=tuple(spec.get("domains") or ()),
                              tier=spec.get("tier") if spec.get("tier") in TIERS else "ask",
                              has_password=self.keyring.get(name) is not None,
                              has_session=self._session_file(name).exists()))
        return found

    def listed(self) -> list[Site]:
        """The saved logins' names, domains and tiers alone -- what the
        model is told of them on every request (agent/toolset/browser.py)
        -- without reading the keychain or the sessions to say what is kept:
        their password is not fetched into memory to tell a model it exists."""
        return [Site(name=name, domains=tuple(spec.get("domains") or ()),
                     tier=spec.get("tier") if spec.get("tier") in TIERS else "ask")
                for name, spec in sorted(self._raw().items())]

    def site(self, name: str) -> Site | None:
        return next((s for s in self.sites() if s.name == name), None)

    def site_for(self, url: str) -> Site | None:
        """The saved login a page belongs to: the one whose domain matches
        most specifically, so a portal saved under its own subdomain wins
        over a whole university."""
        best: tuple[int, Site] | None = None
        for s in self.sites():
            for d in s.domains:
                if on_site(url, (d,)) and (best is None or len(d) > best[0]):
                    best = (len(d), s)
        return best[1] if best else None

    def save_site(self, name: str, domains: list[str], tier: str = "ask") -> Site:
        """Add or change a saved login's name, domains and tier. Raises
        ValueError, in words. Called only from the live view (theirs)."""
        name = (name or "").strip().lower()
        if not NAME.match(name):
            raise ValueError("a login's name is lower-case letters, digits and dashes")
        if tier not in TIERS:
            raise ValueError(f"tier is one of {', '.join(TIERS)}")
        cleaned = []
        for d in domains or []:
            d = d.strip().lower().rstrip(".")
            if why := domain_refusal(d):
                raise ValueError(why)
            if d not in cleaned:
                cleaned.append(d)
        if not cleaned or len(cleaned) > MAX_DOMAINS:
            raise ValueError(f"a login belongs to between 1 and {MAX_DOMAINS} domains")
        raw = self._raw()
        raw[name] = {"domains": cleaned, "tier": tier}
        self._write(raw)
        return self.site(name)  # type: ignore[return-value]

    def forget(self, name: str) -> bool:
        """Remove a saved login entirely: its record, password and session.
        The password goes first: if the Credential Manager refuses, the
        login is still listed, to be forgotten again."""
        self.keyring.delete(name)
        raw = self._raw()
        known = raw.pop(name, None) is not None
        self._write(raw)
        session = self._session_file(name)
        if session.exists():
            session.unlink()
            known = True
        return known

    # -- credentials ---------------------------------------------------------

    def set_credentials(self, name: str, username: str, password: str) -> None:
        if self.site(name) is None:
            raise ValueError(f"no saved login called {name!r}")
        if not username or not password:
            raise ValueError("both a username and a password")
        logscrub.hold(password)
        self.keyring.set(name, json.dumps({"username": username, "password": password}))

    def credentials(self, name: str) -> tuple[str, str] | None:
        """Their username and password for `name`. The one caller types them
        through the driver; nothing returns them to a model."""
        raw = self.keyring.get(name)
        if not raw:
            return None
        try:
            found = json.loads(raw)
            return str(found["username"]), logscrub.hold(str(found["password"]))
        except (ValueError, KeyError, TypeError):
            return None

    # -- sessions ------------------------------------------------------------

    def save_state(self, name: str, state: dict) -> None:
        """A login's cookies and storage, encrypted to their Windows account."""
        if self.site(name) is None:
            raise ValueError(f"no saved login called {name!r}")
        settingsfile.write_whole(self._session_file(name),
                                 self.protect(json.dumps(state).encode("utf-8")))

    def load_state(self, name: str) -> dict | None:
        path = self._session_file(name)
        if not path.exists():
            return None
        try:
            return json.loads(self.unprotect(path.read_bytes()).decode("utf-8"))
        except Exception:  # noqa: BLE001 - unreadable is absent; the user logs in again
            return None


_default: list[Vault] = []


def vault() -> Vault:
    """The real one: their data folder, their keychain, DPAPI."""
    if not _default:
        _default.append(Vault())
    return _default[0]
