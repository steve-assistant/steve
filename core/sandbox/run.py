"""A module's code, called in the sandbox: its container, its files, its
grants and its process, once per call.

Core calls a module's code (a tool, `perform`) by starting it here: the
runtime's Python on `steve_sdk/run.py`, in the module's own AppContainer,
with read on the code and modify on its state alone, under a job's caps,
and the broker answering whatever it asks. A process per call costs a
start, and keeps nothing between calls but the state folder: a call cannot
leave something in memory for the next, or for another account's.

What runs is what the user approved. A module from outside runs from its
current version's folder, and only while its files hash to what was
approved (`lifecycle.check_start`); its grants are read from that copy's
manifest. A built-in that runs here is copied from its release into the
data folder first, so every module runs from the one place, under the one
kind of grant. And outside `perform`, which runs only on a card's yes, the
grants are `Grants.reading()`: a call may look, never change.

Without the sandbox nothing runs (decision D6): `NotHeld` says why.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core import addons, clock, lifecycle, modules, paths
from core.sandbox import broker, grants, launch, probe, profiles, runtime
from core.sandbox import job as jobs
from steve_sdk.context import Account
from steve_sdk.manifest import Manifest

TIMEOUT_S = 60.0
LIMITS = jobs.Limits(memory=512 << 20, processes=1, cpu_cores=1, ui=True,
                     die_on_unhandled=True)
_BYTECODE = frozenset({"__pycache__"})
_copying = threading.Lock()


class NotHeld(RuntimeError):
    """This computer cannot hold a module's code in, so none runs."""


class NotStarted(RuntimeError):
    """The computer would not set the module up (its container, its
    folders' grants, its process): not the module's fault, and not counted
    against it. A module's own error can be an OSError too (a refusal comes
    back as PermissionError), so this one is told apart by type."""


class TimedOut(broker.Broken):
    """The call ran past its time and the module was ended."""


def installed(conn: sqlite3.Connection, module_id: str) -> modules.Module:
    """The module as installed and on; Refused otherwise."""
    held = modules.module(conn, module_id)
    if held is None:
        raise lifecycle.Refused(f"no module {module_id!r} is installed")
    if not held.enabled:
        raise lifecycle.Refused(f"{module_id} is turned off")
    return held


def code(conn: sqlite3.Connection, held: modules.Module, manifest: Manifest) -> Path:
    """The folder `manifest`'s module runs from, as approved: a module from
    outside's current version, its files checked; a built-in's copy of its
    release, made again when the release changes. Blocking."""
    if held.origin != "builtin":
        lifecycle.check_start(conn, manifest.id)
        return paths.module_code_dir(manifest.id, held.version)
    if manifest.path is None:
        raise lifecycle.Refused(f"{manifest.id} has no release folder")
    release = manifest.path.parent
    target = paths.module_code_dir(manifest.id, manifest.version)
    with _copying:
        want = addons.tree_hash(release, skip=_BYTECODE)
        if not target.is_dir() or addons.tree_hash(target) != want:
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(release, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def _account(account: Account | None) -> str:
    if account is None:
        return "-"
    return json.dumps({"key": account.key, "name": account.name, "address": account.address,
                       "fields": account.fields, "preset": account.preset})


def call(conn: sqlite3.Connection, manifest: Manifest, method: str, params: dict, *,
         account: Account | None = None, perform: bool = False,
         reader: broker.Reader | None = None, actor: broker.Actor | None = None,
         redact: Callable[[str], str], keep: Callable[[str], None] | None = None,
         network: broker.Network | None = None, timeout_s: float = TIMEOUT_S) -> Any:
    """Start `manifest`'s module, call `method` with `params`, and end it:
    the module's answer, or its error as the SDK's (`steve_sdk.link`);
    `TimedOut` past `timeout_s`; `NotHeld`, `NotStarted` or
    `lifecycle.Refused` where it never ran. `perform` gives it the methods its
    manifest declares, for an action the user approved; any other call
    has GET and HEAD alone. Blocking: run it off the event loop, on a
    connection of the thread's own."""
    support = probe.verdict()
    if support is None or not support.ok:
        raise NotHeld(support.reason if support is not None
                      else "the sandbox has not been checked on this computer yet")
    held = installed(conn, manifest.id)
    try:
        folder = code(conn, held, manifest)
        root = runtime.ready()
        python = runtime.python(root)
        container = profiles.ensure(profiles.for_module(manifest.id))
        state = paths.module_state_dir(manifest.id)
        if not grants.holds(folder, container.sid):
            grants.grant(folder, container.sid, grants.READ)
        if not grants.holds(state, container.sid):
            grants.grant(state, container.sid, grants.MODIFY)
    except OSError as exc:
        raise NotStarted(f"{manifest.id} could not be set up: {exc}") from exc
    granted = broker.grants_for(manifest, account)
    held_broker = broker.Broker(
        module=manifest.id, grants=granted if perform else granted.reading(),
        secret=(broker.stored(held.key, account.key) if account is not None
                else lambda name: None),
        network=network or broker.Network(), redact=redact, keep=keep, reader=reader,
        actor=actor)
    zone = getattr(clock.local_zone(), "key", "UTC")
    lpac = support.mode == probe.LPAC
    try:
        process = launch.start(
            [str(python), *runtime.FLAGS, "-m", "steve_sdk.run", manifest.id, str(folder),
             str(state), zone, _account(account)],
            container=container, cwd=folder, limits=LIMITS,
            env=launch.environment([python.parent]),
            capabilities=runtime.capabilities(lpac), lpac=lpac)
    except OSError as exc:
        held_broker.close()
        raise NotStarted(f"{manifest.id} could not be started: {exc}") from exc
    session = broker.Session(process, held_broker)
    try:
        with process.deadline(timeout_s):
            try:
                return session.call(method, params)
            except broker.Broken:
                if process.timed_out:
                    raise TimedOut(f"{manifest.id} ran past {timeout_s:.0f} seconds") from None
                raise
    finally:
        session.close()
