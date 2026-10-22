"""What a container may touch: one access entry on each folder it is given.

An AppContainer is refused everything a folder's access list does not grant
its package SID or one of its capabilities by name: the user's own entry
does not reach it. So a module's container is given, and holds, only:

- read and execute on its code: the version's folder, granted to its
  package SID;
- modify on its `state/` folder, granted the same way;
- read and execute on the module runtime, granted once to a capability
  every module's container carries (`runtime.py`), so a new container adds
  no grant on the runtime's thousands of files.

Each grant is inheritable, so Windows carries it down to what the folder
holds and to what is made in it later. A grant is taken back the same way,
and removing a module takes back its grants with its profile.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

from core.sandbox import win32

READ = win32.FILE_READ_EXECUTE
MODIFY = win32.FILE_MODIFY


def _apply(path: Path, sid: str, mask: int, mode: int) -> None:
    api = win32.advapi32()
    free = win32.kernel32().LocalFree
    with win32.Sid(sid) as pointer:
        old, descriptor, new = win32.P(), win32.P(), win32.P()
        error = api.GetNamedSecurityInfoW(str(path), win32.SE_FILE_OBJECT,
                                          win32.DACL_SECURITY_INFORMATION, None, None,
                                          ctypes.byref(old), None, ctypes.byref(descriptor))
        if error:
            raise OSError(None, f"GetNamedSecurityInfoW({path})", None, error)
        try:
            entry = win32.EXPLICIT_ACCESS_W()
            entry.grfAccessPermissions = mask
            entry.grfAccessMode = mode
            entry.grfInheritance = win32.SUB_CONTAINERS_AND_OBJECTS_INHERIT
            entry.Trustee.TrusteeForm = win32.TRUSTEE_IS_SID
            entry.Trustee.ptstrName = pointer.value
            error = api.SetEntriesInAclW(1, ctypes.byref(entry), old, ctypes.byref(new))
            if error:
                raise OSError(None, "SetEntriesInAclW", None, error)
            try:
                error = api.SetNamedSecurityInfoW(str(path), win32.SE_FILE_OBJECT,
                                                  win32.DACL_SECURITY_INFORMATION, None, None,
                                                  new, None)
                if error:
                    raise OSError(None, f"SetNamedSecurityInfoW({path})", None, error)
            finally:
                free(new)
        finally:
            free(descriptor)


def grant(path: Path, sid: str, mask: int) -> None:
    """Give `sid` `mask` on `path` and everything under it."""
    _apply(path, sid, mask, win32.GRANT_ACCESS)


def revoke(path: Path, sid: str) -> None:
    """Take back every entry `sid` has on `path`, and under it."""
    _apply(path, sid, 0, win32.REVOKE_ACCESS)


def access_list(path: Path) -> str:
    """The folder's access list, as SDDL."""
    api = win32.advapi32()
    descriptor, text = win32.P(), win32.wt.LPWSTR()
    error = api.GetNamedSecurityInfoW(str(path), win32.SE_FILE_OBJECT,
                                      win32.DACL_SECURITY_INFORMATION, None, None, None, None,
                                      ctypes.byref(descriptor))
    if error:
        raise OSError(None, f"GetNamedSecurityInfoW({path})", None, error)
    try:
        win32.check(api.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, win32.SDDL_REVISION_1, win32.DACL_SECURITY_INFORMATION,
            ctypes.byref(text), None), "ConvertSecurityDescriptorToStringSecurityDescriptor")
        try:
            return text.value
        finally:
            win32.kernel32().LocalFree(ctypes.cast(text, win32.P))
    finally:
        win32.kernel32().LocalFree(descriptor)


def holds(path: Path, sid: str) -> bool:
    """Whether `path` itself grants `sid` anything, inheritably."""
    entries = [part.rstrip(")") for part in access_list(path).split("(")[1:]]
    return any(entry.startswith("A;OICI;") and entry.endswith(";" + sid) for entry in entries)
