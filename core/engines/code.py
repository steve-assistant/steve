"""The engine for a built-in module with code of its own: a class's provider
is a class in that code, handed the account's context.

Google's client and Outlook's COM are no standard core could speak for a
module, as it speaks IMAP or CalDAV (05-connectors.md, section 6), so those
built-ins bring their own providers. Before step 5's item 9 core built them
itself, once, as stand-ins with fixed keys and no context: one
Google account, whose sign-in, secrets and errors were its own code's
business, and no way to add a second. Here they are built like any
engine's, per account, from the account's context (`core/modctx.py`).

Only a built-in's code runs here, in the backend: every one has taken its
context since item 10, and the list of those that had went with it. A
module the user installs runs its code in its sandbox (step 6), never here.

Such a module may also take two steps of setup into its own hands:

- `fields(values)`: the setup fields as it keeps them, checked. Google's
  client file is kept as its id, secret and project, and a Web client is
  refused. It raises ValueError, in the user's words.
- `sign_in(ctx, classes=...)`: the browser sign-in, which returns what to
  keep (`steve_sdk.context.SignedIn`). Core keeps it, and checks the address.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType
from typing import Any

from core import paths
from steve_sdk.context import SignedIn
from steve_sdk.errors import NotConnected
from steve_sdk.manifest import ClassSpec, Manifest


def ready(manifest: Manifest) -> bool:
    """Whether this engine builds `manifest`'s providers: a built-in's, from
    the source's own `modules/` folder."""
    where = paths.SOURCE / "modules" / manifest.id
    return (bool(manifest.code) and manifest.path is not None
            and Path(manifest.path).parent.resolve() == where.resolve())


def _code(manifest: Manifest) -> ModuleType:
    if not ready(manifest):
        raise NotConnected(f"{manifest.label}'s code does not run in this version of Steve")
    return importlib.import_module(f"modules.{manifest.id}.{Path(manifest.code).stem}")


def engine(ctx: Any, spec: ClassSpec) -> Any:
    return getattr(_code(ctx.manifest), spec.settings["provider"])(ctx)


def fields(manifest: Manifest, values: dict[str, Any]) -> dict[str, Any]:
    """The setup fields as the module keeps them; as given, if it has no say."""
    hook = getattr(_code(manifest), "fields", None) if ready(manifest) else None
    return hook(values) if hook is not None else values


def sign_in(ctx: Any, *, classes: list[str] | tuple[str, ...]) -> SignedIn:
    """The module's browser sign-in for `classes`: blocking, until the user
    finishes or it gives up."""
    hook = getattr(_code(ctx.manifest), "sign_in", None)
    if hook is None:
        raise NotConnected(f"{ctx.manifest.label} has no browser sign-in")
    signed = hook(ctx, classes=tuple(classes))
    if not isinstance(signed, SignedIn):
        raise TypeError(f"{ctx.manifest.id}'s sign_in returned {type(signed).__name__}")
    return signed
