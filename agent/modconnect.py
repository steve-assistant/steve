"""A connector from outside (06-modules.md, item 10): an account whose
provider is a module's code, run in its sandbox.

Before this a connector could only be built in. The code engine runs a
provider in core's own process (`core/engines/code.py`), where only
Steve's own code may run, so setup refused any other module's class ("cannot
add its mail yet"). Here the code engine's provider for a sandbox module
is a stand-in: each of its methods starts the module in its container
(`core/sandbox/run.py`), calls the method there on what core passes, and
checks that the answer is what the class's interface returns
(`steve_sdk.wire`) before core reads it.

- **A read runs with GET and HEAD alone.** A write (`WRITES`) runs with
  the methods its manifest declares. Core calls a write only from its write
  path (`core/writes.py`), after the checks and the card that path makes,
  as it does for a built-in provider.
- **The broker fills the account's secrets** into the header and host each
  is bound to. The module never holds one.
- **Its own failures count against it,** as a tool's do
  (`modadmit.watched_here`). The account's and the network's do not, and
  neither do the computer's: a sandbox that cannot start is the account not
  connected.
- **Making one starts nothing,** since core makes an account's providers at
  every turn. What only the code can say, its capabilities, is asked once
  for each version and kept.
"""

from __future__ import annotations

import threading
from typing import Any

from core.engines import code as code_engine
from steve_sdk import wire
from steve_sdk.classes import INTERFACES, Provider
from steve_sdk.errors import ConnectorError, NotConnected
from steve_sdk.manifest import ClassSpec, Manifest

# The methods that change something at the service. Any other runs able
# only to look; a write left off this list is refused there, not widened.
WRITES: dict[str, frozenset[str]] = {
    "mail": frozenset({"make_draft", "send_draft", "delete", "modify", "move"}),
    "calendar": frozenset({"create", "update", "delete"}),
    "files": frozenset({"create_document", "create_spreadsheet", "update_spreadsheet"}),
    "tasks": frozenset({"create", "update", "complete"}),
}

_described: dict[tuple[str, str, str], frozenset[str]] = {}
_describing = threading.Lock()


def runs(manifest: Manifest) -> bool:
    """Whether a class on the code engine runs in `manifest`'s sandbox."""
    return manifest.tier == "sandbox" and bool(manifest.code)


def engine(ctx: Any, spec: ClassSpec) -> Provider:
    """The code engine, as an account's providers are built: a sandbox
    module's is a stand-in here, a built-in's runs in core's process."""
    if runs(ctx.manifest):
        return _remote(spec.klass)(ctx, spec.klass)
    return code_engine.engine(ctx, spec)


def buildable(manifest: Manifest) -> bool:
    """Whether the code engine builds `manifest`'s classes."""
    return runs(manifest) or code_engine.ready(manifest)


class _Remote:
    """What every class's stand-in shares; `_remote` adds the methods."""

    shareable = True  # each call is a process of its own

    def __init__(self, ctx: Any, klass: str) -> None:
        self._ctx = ctx
        self._klass = klass

    @property
    def capabilities(self) -> frozenset[str]:  # type: ignore[override]
        manifest = self._ctx.manifest
        key = (manifest.id, manifest.version, self._klass)
        if key not in _described:
            with _describing:
                if key not in _described:
                    said = self._call("describe", [], {}, check=False)
                    found = said.get("capabilities") if isinstance(said, dict) else None
                    if not wire.fits(found, list[str]):
                        raise ConnectorError(f"{manifest.label} did not say what it can do")
                    _described[key] = frozenset(found)
        return _described[key]

    def _call(self, method: str, args: list, kwargs: dict, *, check: bool = True) -> Any:
        from agent import modadmit, screening
        from core import db, lifecycle, modctx, screen
        from core.sandbox import run

        manifest = self._ctx.manifest
        params = {"klass": self._klass, "method": method, "args": wire.dump(args),
                  "kwargs": wire.dump(kwargs)}
        conn = db.open_store()
        try:
            try:
                said = run.call(conn, manifest, "provider", params, account=self._ctx.account,
                                perform=method in WRITES[self._klass], redact=modctx.scrub,
                                network=self._ctx.network)
                found = wire.answer(self._klass, method, said) if check else said
            except (run.NotHeld, run.NotStarted, lifecycle.Refused) as exc:
                raise NotConnected(f"{manifest.label} could not be started here: {exc}") from exc
            except Exception as exc:
                modadmit.watched_here(conn, manifest.id, lifecycle.outcome(
                    exc, timed_out=isinstance(exc, run.TimedOut)))
                # The module words its errors as freely as its answers, and
                # an error went on to the model unscreened, from any of the
                # places that tell it a connector failed (step 11's audit).
                # Screened here, the one way out of a module's connector.
                own = isinstance(exc, ConnectorError)
                said = screening.shown_here(
                    conn, screen.ANSWER, f"{manifest.id}.{method}", manifest.id,
                    (str(exc) if own else f"{type(exc).__name__}: {exc}")[:300]).text
                if own:
                    raise type(exc)(said, fix=exc.fix) from exc
                raise ConnectorError(f"{manifest.label} failed: {said}") from exc
            modadmit.watched_here(conn, manifest.id, lifecycle.outcome())
            return found
        finally:
            conn.close()


def _forward(name: str):
    def method(self: _Remote, *args: Any, **kwargs: Any) -> Any:
        return self._call(name, list(args), kwargs)

    method.__name__ = name
    return method


_made: dict[str, type] = {}


def _remote(klass: str) -> type:
    """The stand-in for `klass`: its interface, every method forwarded."""
    if klass not in _made:
        iface = INTERFACES[klass]
        body = {name: _forward(name) for name in wire.methods(klass) if name != "has"}
        _made[klass] = type(f"Remote{iface.__name__}", (_Remote, iface),
                            {"klass": klass, "__module__": __name__, **body})
    return _made[klass]
