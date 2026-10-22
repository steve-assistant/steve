"""AppContainer profiles: one for each module, named for this install.

A profile belongs to the Windows user, not to Steve: Windows keeps it in
their registry and AppData, and any program of theirs can make or delete
one by name. Named only for the module, a second install of Steve on the
same account would share each module's profile, and so its grants; and a
test that made `steve.m.gmail` and deleted it afterwards would delete the
real one's.

So each name carries the first eight characters of an id this install makes
once and keeps in its data folder (`identity.container`). Every test has a
data folder of its own (tests/gate/fences.py), and so an install id of its
own, and never meets the real profiles. Removing a module deletes its
profile; removing the data folder leaves profiles whose names nothing will
make again, which uninstall deletes by this install's exact prefix (step 8).
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import secrets
from dataclasses import dataclass

from core import identity, paths
from core.sandbox import win32

INSTALL_FILE = "install-id"
_INSTALL = re.compile(r"^[0-9a-f]{32}$")


def install() -> str:
    """This install's id, shortened to the eight characters names carry.
    Made the first time it is asked for. Two processes asking at once agree:
    the file is created exclusively, and the one that loses reads it."""
    path = paths.data_dir() / INSTALL_FILE
    for _ in range(2):
        with contextlib.suppress(FileNotFoundError):
            made = path.read_text(encoding="ascii").strip()
            if _INSTALL.match(made):
                return made[:8]
            raise OSError(f"{path} is not an install id; move it aside to make a new one")
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as f:
                f.write(secrets.token_hex(16) + "\n")
    raise OSError(f"could not make or read {path}")


def made() -> str | None:
    """This install's id if one was made, and None rather than make one:
    uninstall (core/uninstall.py) must not leave a new id behind, nor guess
    at another install's profiles."""
    with contextlib.suppress(OSError):
        recorded = (paths.data_dir() / INSTALL_FILE).read_text(encoding="ascii").strip()
        if _INSTALL.match(recorded):
            return recorded[:8]
    return None


@dataclass(frozen=True)
class Container:
    """A profile made: its name, and the package SID its grants name."""

    name: str
    sid: str


def for_module(module_id: str) -> str:
    """The profile name of one module of this install."""
    return identity.container(install(), module_id)


def for_trial() -> str:
    """A profile name no module has and no other run will make: for code
    not yet approved, which must not run as the module it would replace."""
    return identity.trial_container(install(), secrets.token_hex(6))


def ensure(name: str) -> Container:
    """The profile `name`, made if it is not there yet."""
    api = win32.userenv()
    sid = win32.P()
    hr = api.CreateAppContainerProfile(name, name, f"{identity.NAME} module sandbox", None, 0,
                                       ctypes.byref(sid))
    if hr & 0xFFFFFFFF == win32.HR_ALREADY_EXISTS:
        return Container(name, derive(name))
    win32.check_hr(hr, f"CreateAppContainerProfile({name})")
    try:
        return Container(name, win32.sid_text(sid))
    finally:
        win32.advapi32().FreeSid(sid)


def derive(name: str) -> str:
    """The package SID of the profile `name`, whether or not it is made."""
    sid = win32.P()
    win32.check_hr(win32.userenv().DeriveAppContainerSidFromAppContainerName(
        name, ctypes.byref(sid)), f"DeriveAppContainerSidFromAppContainerName({name})")
    try:
        return win32.sid_text(sid)
    finally:
        win32.advapi32().FreeSid(sid)


def delete(name: str) -> bool:
    """Delete the profile `name`, with its folder in AppData. False if
    there was none."""
    hr = win32.userenv().DeleteAppContainerProfile(name)
    if hr & 0xFFFFFFFF == win32.HR_NOT_FOUND:
        return False
    win32.check_hr(hr, f"DeleteAppContainerProfile({name})")
    return True


def capability(name: str) -> str:
    """The SID of a named capability, as a container carries it.

    Windows answers with two arrays of SIDs it allocated, the group SIDs and
    the capability SIDs, and each SID and each array must be freed."""
    groups, group_count = win32.P(), win32.D()
    sids, sid_count = win32.P(), win32.D()
    win32.check(win32.kernelbase().DeriveCapabilitySidsFromName(
        name, ctypes.byref(groups), ctypes.byref(group_count), ctypes.byref(sids),
        ctypes.byref(sid_count)), f"DeriveCapabilitySidsFromName({name})")
    free = win32.kernel32().LocalFree
    try:
        found = ctypes.cast(sids, ctypes.POINTER(win32.P))
        return win32.sid_text(found[0])
    finally:
        for array, count in ((groups, group_count.value), (sids, sid_count.value)):
            if array.value:
                items = ctypes.cast(array, ctypes.POINTER(win32.P))
                for index in range(count):
                    free(items[index])
                free(array)
