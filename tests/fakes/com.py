"""A desktop program's automation for tests of `com.call`: an Outlook with an inbox.

Small fake objects in the shapes `win32com` hands over: methods, values, a
property, a collection to walk, and the two ways past a manifest's list a
module told to leak would try, the Application's `CreateObject` and a mail
item's `Session`. Each records the thread that called it, since a COM
object belongs to the thread that made it.

Each finds a member by its name in any case, as COM does (IDispatch's
GetIDsOfNames), so `mail.send()` is Outlook's Send. Until the review of 15
October these matched exactly, and so hid that a module listing `send` as
a read could send.
"""

from __future__ import annotations

import datetime as dt
import threading

WHEN = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)


class _Dispatch:
    def __getattr__(self, name):
        for real in dir(self):
            if real.lower() == name.lower():
                return getattr(self, real)
        raise AttributeError(name)


class Profile(_Dispatch):
    """A mail item's `Session`: the whole profile, every account in it."""

    Name = "every account"


class Mail(_Dispatch):
    def __init__(self, n: int, outlook: Outlook) -> None:
        self.Subject = f"note {n}"
        self.ReceivedTime = WHEN + dt.timedelta(minutes=n)
        self.Session = Profile()
        self._outlook = outlook

    def Send(self):  # noqa: N802 - the COM name
        self._outlook.sent.append(self.Subject)


class Items(_Dispatch):
    def __init__(self, mails: list[Mail], outlook: Outlook) -> None:
        self._mails = mails
        self._outlook = outlook

    @property
    def Count(self):  # noqa: N802 - the COM name
        return len(self._mails)

    def __iter__(self):
        self._outlook.threads.add(threading.get_ident())
        return iter(self._mails)

    def Restrict(self, text):  # noqa: N802 - the COM name
        if not text:
            raise RuntimeError("Cannot parse condition. Error at \"\".")
        return Items([m for m in self._mails if text in m.Subject], self._outlook)


class Folder(_Dispatch):
    def __init__(self, outlook: Outlook, count: int) -> None:
        self.Name = "Inbox"
        self.Items = Items([Mail(n, outlook) for n in range(count)], outlook)


class Namespace(_Dispatch):
    def __init__(self, outlook: Outlook, count: int) -> None:
        self._folder = Folder(outlook, count)
        self._outlook = outlook

    def GetDefaultFolder(self, number):  # noqa: N802 - the COM name
        self._outlook.threads.add(threading.get_ident())
        assert number == 6, number
        return self._folder


class Outlook(_Dispatch):
    """`Outlook.Application`, with an inbox of `count` notes: what it sent,
    the threads that called it, and each ProgID a dispatch opened it as."""

    def __init__(self, count: int = 3) -> None:
        self.sent: list[str] = []
        self.threads: set[int] = set()
        self.opened: list[str] = []
        self.Session = Profile()
        self._namespace = Namespace(self, count)

    def GetNamespace(self, name):  # noqa: N802 - the COM name
        self.threads.add(threading.get_ident())
        assert name == "MAPI", name
        return self._namespace

    def CreateObject(self, progid):  # noqa: N802 - the COM name
        raise AssertionError(f"opened {progid} past the deny-list")

    def dispatch(self, progid: str) -> Outlook:
        """`core.modctx._dispatch` for this Outlook."""
        self.opened.append(progid)
        return self
