"""Classic Outlook for tests: the account, added as the app adds it, and
Outlook itself as the module reaches it, through `ctx.com`.

A test builds its mailbox from small fake objects of its own: the namespace
`GetNamespace("MAPI")` answers (tests/unit/test_outlook.py, the golden
world's). What this file adds is the way in. The context is core's own,
built from the module's real manifest, so each member the module reaches is
checked against its real list, and one reached without being listed fails
the test (`refused`). The module's walks catch every exception, so that one
bad folder is not the mailbox; a refusal would otherwise read as an empty
folder.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from core import modules
from core.modctx import ModuleCom, ModuleContext
from steve_sdk.context import Account
from tests import persona

# The key today's Outlook connection stood in with before item 10 made it an
# account like any other: the ids in the tests' fixtures and the golden
# record carry it.
KEY = "9174c4f353d1"
ADDRESS = persona.WORK_EMAIL
NAME = "outlook"


class Application:
    """`Outlook.Application`: the namespace a test made, and new items from
    that namespace's own `Application`, where the tests' fakes keep it."""

    def __init__(self, namespace: Any) -> None:
        self._namespace = namespace

    def GetNamespace(self, name: str) -> Any:  # noqa: N802 - the COM name
        assert name == "MAPI", name
        return self._namespace

    def CreateItem(self, kind: int) -> Any:  # noqa: N802 - the COM name
        return self._namespace.Application.CreateItem(kind)


class ComError(Exception):
    """`pywintypes.com_error`'s shape: its HRESULT, its words, and the
    exception Outlook raised inside it, whose last field is that one's."""


def failing(hresult: int) -> Callable[[str], Any]:
    """A dispatch that fails as Windows does, with `hresult`."""
    def dispatch(progid: str) -> Any:
        raise ComError(hresult, "Invalid class string", None, None)

    return dispatch


def context(namespace: Any, *, key: str = KEY, address: str = ADDRESS,
            dispatch: Callable[[str], Any] | None = None) -> ModuleContext:
    """The context an Outlook account's providers are built from, over a
    test's namespace."""
    return ModuleContext(
        manifest=modules.builtins()["outlook_classic"], module_key="outlookmod01",
        account=Account(key=key, name=NAME, module="outlook_classic", address=address),
        state_dir=modules.account_state_dir("outlook_classic", key),
        dispatch=dispatch or (lambda progid: Application(namespace)))


@contextlib.contextmanager
def reached(namespace: Any, **kw: Any) -> Iterator[ModuleContext]:
    """Outlook's functions, called inside, reach `namespace` as the
    account's, and nothing their manifest does not list."""
    from modules.outlook_classic import outlook

    ctx = context(namespace, **kw)
    with outlook.as_account(ctx):
        yield ctx
    assert ctx.com.refused == [], f"reached without being listed: {ctx.com.refused}"


def serve(monkeypatch: pytest.MonkeyPatch, namespace: Any) -> list[str]:
    """Outlook for every context built from here on, in place of the
    fences' refusal: the namespace a test made. Returns the paths any of
    them refused, for the test to find empty."""
    refused: list[str] = []
    monkeypatch.setattr("core.modctx._dispatch", lambda progid: Application(namespace))
    real = ModuleCom.refuse

    def refuse(self: ModuleCom, path: str, why: str) -> None:
        refused.append(path)
        real(self, path, why)

    monkeypatch.setattr(ModuleCom, "refuse", refuse)
    return refused


def add(conn: Any, *, key: str = KEY, address: str = ADDRESS, name: str = NAME,
        classes: list[str] | None = None) -> str:
    """The account, added as the app adds it (`agent.accounts.add`); its key."""
    from agent import accounts

    modules.register(conn)
    with pytest.MonkeyPatch.context() as keyed:
        keyed.setattr(modules, "new_key", lambda: key)
        return accounts.add(conn, module_id="outlook_classic", address=address,
                            wanted=classes, name=name)


def provider(conn: Any, klass: str, key: str = KEY) -> Any:
    """The account's provider of `klass`, as the slot builds it."""
    from agent import connectors

    return next(c.provider for c in connectors.accounts(conn, klass) if c.key == key)
