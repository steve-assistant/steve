"""A named pipe that one container can open: a local server's one way out.

A server Steve starts for a module runs in a container with no network
capability (06-modules.md, section 7). Its connections to the hosts it was
approved for go through a pipe Steve makes for it, and the broker on this
end connects them, one host at a time (`core/sandbox/relay.py`). A pipe
anyone could open, or name first, would undo that:

- **the name** is random and made with `FILE_FLAG_FIRST_PIPE_INSTANCE`, so a
  program that made a pipe of that name first, to hear what the container
  says, makes this one fail instead of share it;
- **remote clients** are refused (`PIPE_REJECT_REMOTE_CLIENTS`);
- **the access list** holds the user and the container's package SID, with
  a Low label so the container, which runs at Low integrity, may write. Any
  other container, whose package SID is not on it, is refused. The user's
  own processes can open it, as they can read the user's files anyway;
- **the client** is checked: the broker serves a connection only from the
  process it started (`Connection.client_pid`).

Each instance is overlapped. Windows runs a synchronous handle's reads and
writes one at a time, so a relay waiting to read what the server sends
would hold back every byte its service sends the other way: the first
version, synchronous, served one request and answer and could not have
carried a TLS connection.
"""

from __future__ import annotations

import contextlib
import ctypes
import secrets
import threading
from dataclasses import dataclass

from core import identity
from core.sandbox import win32

BUFFER = 64 * 1024
CLOSE_S = 5.0


class _Overlapped:
    """One operation's OVERLAPPED and the event it signals."""

    def __init__(self) -> None:
        self.event = win32.kernel32().CreateEventW(None, True, False, None)
        win32.check(self.event, "CreateEventW")
        self.value = win32.OVERLAPPED()
        self.value.hEvent = self.event

    def close(self) -> None:
        win32.kernel32().CloseHandle(self.event)


class Stream:
    """A connected instance, read and written at once from two threads.
    `read` returns b"" once the other end has gone; `close` from any
    thread ends both."""

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._closed = False
        self._busy = 0
        self._idle = threading.Condition()

    def _io(self, write: bool, buffer: ctypes.Array, size: int) -> int:
        with self._idle:
            if self._closed:
                raise BrokenPipeError("the pipe is closed")
            self._busy += 1
        api = win32.kernel32()
        op = _Overlapped()
        try:
            call = api.WriteFile if write else api.ReadFile
            if not call(self._handle, buffer, size, None, ctypes.byref(op.value)):
                error = ctypes.get_last_error()
                if error != win32.ERROR_IO_PENDING:
                    return self._ended(error, write)
            done = win32.D()
            if not api.GetOverlappedResult(self._handle, ctypes.byref(op.value),
                                           ctypes.byref(done), True):
                return self._ended(ctypes.get_last_error(), write)
            return done.value
        finally:
            op.close()
            with self._idle:
                self._busy -= 1
                self._idle.notify_all()

    @staticmethod
    def _ended(error: int, write: bool) -> int:
        # Cancelled is `close`'s doing, from another thread: an end too.
        if error in win32.PIPE_ENDED or error == win32.ERROR_OPERATION_ABORTED:
            if write:
                raise BrokenPipeError(error, "the pipe's other end has gone")
            return 0
        raise ctypes.WinError(error)

    def read(self, size: int = BUFFER) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        try:
            got = self._io(False, buffer, size)
        except BrokenPipeError:
            return b""
        return buffer.raw[:got]

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        while view:
            chunk = bytes(view[:BUFFER])
            sent = self._io(True, ctypes.create_string_buffer(chunk, len(chunk)), len(chunk))
            view = view[sent:]
        return len(data)

    def flush(self) -> None:
        """Nothing is held: each write is the pipe's."""

    def close(self, *, reset: bool = False) -> None:
        """End both ways. The client reads what is left, then the end; or,
        with `reset`, an error at once, as a reset TCP connection gives."""
        with self._idle:
            if self._closed:
                return
            self._closed = True
            api = win32.kernel32()
            # Ends a read or write another thread waits in; each returns.
            api.CancelIoEx(self._handle, None)
            self._idle.wait_for(lambda: self._busy == 0, CLOSE_S)
            # Closed, not disconnected, unless asked. DisconnectNamedPipe
            # discards what the client has not read yet, and the relay
            # writes a server's last chunk and closes at once, so a reply
            # that ended with its connection lost its tail (CI, 22 October
            # 2026). But a plain close reads as a clean end, which a refused
            # connection is not: Node reports no error for one on a plain
            # socket, and a server waiting for its connection's error waited
            # for ever (tests/gate/test_local_servers.py).
            if reset:
                api.DisconnectNamedPipe(self._handle)
            api.CloseHandle(self._handle)


@dataclass(eq=False)
class Connection:
    """One client on one instance of the pipe."""

    client_pid: int
    stream: Stream

    def close(self, *, reset: bool = False) -> None:
        with contextlib.suppress(OSError):
            self.stream.close(reset=reset)


class Pipe:
    """The pipe one container may open. Each `accept` makes an instance and
    waits for one client on it."""

    def __init__(self, package_sid: str, install: str) -> None:
        self.name = f"{identity.PIPE_PREFIX}{install}.{secrets.token_hex(8)}"
        self.access = (f"D:P(A;;GA;;;{win32.user_sid()})(A;;GRGW;;;{package_sid})"
                       "S:(ML;;NW;;;LW)")
        self._made = 0
        # The first instance is made now, so the name is this pipe's from the start.
        self._waiting: int | None = self._instance()

    def _instance(self) -> int:
        api = win32.kernel32()
        descriptor = win32.security_descriptor(self.access)
        try:
            attributes = win32.SECURITY_ATTRIBUTES(ctypes.sizeof(win32.SECURITY_ATTRIBUTES),
                                                   descriptor.value, False)
            mode = win32.PIPE_ACCESS_DUPLEX | win32.FILE_FLAG_OVERLAPPED
            if not self._made:
                mode |= win32.FILE_FLAG_FIRST_PIPE_INSTANCE
            handle = api.CreateNamedPipeW(
                self.name, mode,
                win32.PIPE_TYPE_BYTE | win32.PIPE_READMODE_BYTE | win32.PIPE_WAIT
                | win32.PIPE_REJECT_REMOTE_CLIENTS,
                win32.PIPE_UNLIMITED_INSTANCES, BUFFER, BUFFER, 0, ctypes.byref(attributes))
            win32.check(handle and handle != win32.INVALID_HANDLE_VALUE,
                        f"CreateNamedPipeW({self.name})")
        finally:
            win32.kernel32().LocalFree(descriptor)
        self._made += 1
        return handle

    def accept(self, timeout: float) -> Connection | None:
        """The next client, or None if none comes within `timeout` seconds.

        The next instance is made before this one is waited on: with none
        waiting, the name would be free for another program to make a pipe
        of, and the container's next connection would go to it."""
        handle = self._waiting if self._waiting is not None else self._instance()
        self._waiting = self._instance()
        api = win32.kernel32()
        op = _Overlapped()
        try:
            error = 0
            if not api.ConnectNamedPipe(handle, ctypes.byref(op.value)):
                error = ctypes.get_last_error()
            if error == win32.ERROR_IO_PENDING:
                if api.WaitForSingleObject(op.event, max(0, int(timeout * 1000))) \
                        == win32.WAIT_TIMEOUT:
                    api.CancelIoEx(handle, ctypes.byref(op.value))
                done = win32.D()
                error = 0 if api.GetOverlappedResult(handle, ctypes.byref(op.value),
                                                     ctypes.byref(done), True) \
                    else ctypes.get_last_error()
        finally:
            op.close()
        if error == win32.ERROR_OPERATION_ABORTED:
            api.CloseHandle(handle)
            return None
        if error not in (0, win32.ERROR_PIPE_CONNECTED):
            api.CloseHandle(handle)
            raise OSError(None, f"ConnectNamedPipe({self.name})", None, error)
        client = win32.wt.ULONG()
        if not api.GetNamedPipeClientProcessId(handle, ctypes.byref(client)):
            error = ctypes.get_last_error()
            api.CloseHandle(handle)
            raise OSError(None, "GetNamedPipeClientProcessId", None, error)
        return Connection(client.value, Stream(handle))

    def close(self) -> None:
        """Close the instance still waiting, if any. Connections close themselves."""
        if self._waiting is not None:
            win32.kernel32().CloseHandle(self._waiting)
            self._waiting = None
