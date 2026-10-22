"""Windows' own sign-in, asked for by the backend, where no Windows Hello
key of Steve's can stand in for a forgotten passphrase.

Until step 11, a passphrase file deleted at an unlocked desk left no
passphrase at all: whoever sat there chose their own, and it answered every
high-risk re-ask (the audit's E1). Now Steve remembers that one was set
(`core/passphrase.state`), and a new one takes a proof that the person at
the desk owns this Windows account: a glance, where a Hello key is
registered for Steve (`core/hello.reset_passphrase`), or else this:
Windows' credential dialog, and the account's password checked by Windows.

What holds:
- **The backend asks, and checks.** The app's word that someone signed in
  would be the word of anything holding the backend's token. So the dialog
  is the backend's own (`CredUIPromptForWindowsCredentialsW`), and what was
  typed is checked by Windows (`LogonUserW`), whose token must be this
  user's: another account's password proves nothing.
- **The password is held for the one check.** It is unpacked into a buffer
  that is wiped once Windows has answered, and never written, logged or
  returned. Step 7 turned this prompt down because Steve would hold the
  password (core/hello.py); where there is no Hello key it is the one proof
  Windows gives.
- **An account with no password proves nothing,** and is told why.
- **One dialog at a time** (`_ASKING`).

Tests never show the dialog or sign anyone in to Windows: the fences refuse
`_ask` (tests/gate/fences.py), and a test fakes it.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes as wt

from core.sandbox import win32

# CREDUIWIN_GENERIC: the dialog takes a user name and a password and gives
# them back as typed, rather than a package's sign-in buffer.
GENERIC = 0x1
CANCELLED = 1223  # ERROR_CANCELLED
LOGON_INTERACTIVE = 2
PROVIDER_DEFAULT = 0
NAME_SAM_COMPATIBLE = 2  # GetUserNameExW: MACHINE\name
_LONGEST = 513  # CREDUI_MAX_USERNAME_LENGTH and the password's, with the null

# Windows' refusals that are the person's to put right.
SAID = {
    1326: "that is not this Windows account's password",
    1327: ("this Windows account has no password, so its sign-in can't stand in for the "
           "passphrase: set one in Windows' settings, or set up Windows Hello"),
    1909: "this Windows account is locked out: try again later",
}

_ASKING = threading.Lock()


class _UI(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hwndParent", wt.HWND),
                ("pszMessageText", wt.LPCWSTR), ("pszCaptionText", wt.LPCWSTR),
                ("hbmBanner", wt.HBITMAP)]


def prove(why: str) -> str | None:
    """Ask for this Windows account's password in Windows' own dialog, saying
    `why`, and have Windows check it; why not, or None when it was this
    account's. "cancelled" when the dialog was closed."""
    if not _ASKING.acquire(blocking=False):
        return "Windows' sign-in is open already: answer it there"
    try:
        sid = _ask(why)
    except OSError as error:
        return SAID.get(getattr(error, "winerror", None) or 0,
                        "Windows couldn't check that sign-in: try again")
    finally:
        _ASKING.release()
    if sid is None:
        return "cancelled"
    if sid != win32.user_sid():
        return "that was another Windows account's sign-in: use your own"
    return None


def _ask(why: str) -> str | None:
    """Show Windows' dialog, with this account's name filled in, and sign in
    what was typed: the SID Windows signed in, or None when the dialog was
    closed. Raises OSError, with Windows' error, when the sign-in fails."""
    credui = ctypes.WinDLL("credui", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    secur = ctypes.WinDLL("secur32", use_last_error=True)
    ole = ctypes.WinDLL("ole32")
    user32 = ctypes.WinDLL("user32")
    credui.CredUIPromptForWindowsCredentialsW.argtypes = [
        ctypes.POINTER(_UI), wt.DWORD, ctypes.POINTER(wt.ULONG), ctypes.c_void_p, wt.ULONG,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.ULONG), ctypes.POINTER(wt.BOOL),
        wt.DWORD]
    credui.CredPackAuthenticationBufferW.argtypes = [
        wt.DWORD, wt.LPWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
    credui.CredUnPackAuthenticationBufferW.argtypes = [
        wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD), wt.LPWSTR,
        ctypes.POINTER(wt.DWORD), wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    advapi.LogonUserW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, wt.DWORD,
                                  ctypes.POINTER(wt.HANDLE)]
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    user32.GetForegroundWindow.restype = wt.HWND

    # This account's name, filled in: the dialog then asks for its password.
    name = ctypes.create_unicode_buffer(_LONGEST)
    length = wt.ULONG(_LONGEST)
    secur.GetUserNameExW(NAME_SAM_COMPATIBLE, name, ctypes.byref(length))
    given = ctypes.create_string_buffer(1024)
    given_size = wt.DWORD(len(given))
    empty = ctypes.create_unicode_buffer("")
    if not credui.CredPackAuthenticationBufferW(0, name, empty, given, ctypes.byref(given_size)):
        given, given_size = None, wt.DWORD(0)

    # Over the window in front, which is the app's: the user just asked.
    ui = _UI(ctypes.sizeof(_UI), user32.GetForegroundWindow(), why, "Steve", None)
    package, out, out_size, save = wt.ULONG(0), ctypes.c_void_p(), wt.ULONG(0), wt.BOOL(False)
    error = credui.CredUIPromptForWindowsCredentialsW(
        ctypes.byref(ui), 0, ctypes.byref(package), given, given_size.value, ctypes.byref(out),
        ctypes.byref(out_size), ctypes.byref(save), GENERIC)
    if error == CANCELLED:
        return None
    if error:
        raise OSError(None, "CredUIPromptForWindowsCredentialsW", None, error)

    user = ctypes.create_unicode_buffer(_LONGEST)
    domain = ctypes.create_unicode_buffer(_LONGEST)
    password = ctypes.create_unicode_buffer(_LONGEST)
    sizes = [wt.DWORD(_LONGEST) for _ in range(3)]
    try:
        if not credui.CredUnPackAuthenticationBufferW(
                0, out, out_size.value, user, ctypes.byref(sizes[0]), domain,
                ctypes.byref(sizes[1]), password, ctypes.byref(sizes[2])):
            raise ctypes.WinError(ctypes.get_last_error())
        # LogonUserW takes MACHINE\name as a domain and a name, and a name
        # with an @ (a Microsoft account's) whole.
        who, where = user.value, domain.value or None
        if where is None and "\\" in who:
            where, who = who.split("\\", 1)
        elif where is None and "@" not in who:
            where = "."
        token = wt.HANDLE()
        if not advapi.LogonUserW(who, where, password, LOGON_INTERACTIVE, PROVIDER_DEFAULT,
                                 ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return win32.token_sid(token)
        finally:
            win32.kernel32().CloseHandle(token)
    finally:
        ctypes.memset(password, 0, ctypes.sizeof(password))
        if out.value:
            ctypes.memset(out, 0, out_size.value)
            ole.CoTaskMemFree(out)
