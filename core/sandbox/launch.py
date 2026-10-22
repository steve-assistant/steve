"""Starting a process in a container: suspended, put in its job, then let go.

The order is the wall. A process started normally runs its first
instructions before anything can hold it, and a container's limits that
come after its first instruction come too late for a process that starts
another at once. So the process is made suspended, in its container, with
only its three pipe ends inherited (`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`:
without it, every inheritable handle the backend holds would go too); it is
put in its job, and only then resumed. If the job will not take it, it is
ended, never resumed.

Its environment is made here, not copied: the backend's own holds what the
user set, which may include API keys. Windows fails the launch without
`LOCALAPPDATA` (error 203), and rewrites it and `TEMP` to the container's
own folder.

A call's time is the parent's to keep (`Process.deadline`): the child
cannot be trusted to stop, so a timer outside it ends the job, and the exit
code says it was the timer (`TIMED_OUT`).
"""

from __future__ import annotations

import contextlib
import ctypes
import msvcrt
import os
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from core.sandbox import job as jobs
from core.sandbox import win32
from core.sandbox.profiles import Container

# The exit code of a process its deadline ended, as `timeout(1)` uses; and
# of one ended because the backend no longer wanted it.
TIMED_OUT = 124
ENDED = 1

# What Windows needs to start a process and Python needs to find itself;
# nothing the user set. `LOCALAPPDATA` is required, and rewritten.
_KEPT = ("SystemRoot", "windir", "SystemDrive", "LOCALAPPDATA", "TEMP", "TMP",
         "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PATHEXT")


def environment(path: list[Path], extra: dict[str, str] | None = None) -> dict[str, str]:
    """The child's whole environment: the few names Windows needs, a PATH
    of `path` and System32, and `extra`."""
    kept = {name: os.environ[name] for name in _KEPT if name in os.environ}
    system = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    kept["PATH"] = ";".join(str(p) for p in [*path, system])
    return {**kept, **(extra or {})}


def _block(env: dict[str, str]) -> ctypes.Array:
    # Sorted without regard to case, as Windows requires of a block.
    text = "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper()))
    return ctypes.create_unicode_buffer(text + "\0", len(text) + 1)


def _pipe(child_reads: bool) -> tuple[int, int]:
    """A pipe whose child end alone is inheritable: (parent end, child end)."""
    api = win32.kernel32()
    attributes = win32.SECURITY_ATTRIBUTES(ctypes.sizeof(win32.SECURITY_ATTRIBUTES), None, True)
    read, write = win32.H(), win32.H()
    win32.check(api.CreatePipe(ctypes.byref(read), ctypes.byref(write),
                               ctypes.byref(attributes), 0), "CreatePipe")
    parent, child = (write, read) if child_reads else (read, write)
    win32.check(api.SetHandleInformation(parent, win32.HANDLE_FLAG_INHERIT, 0),
                "SetHandleInformation")
    return parent.value, child.value


@dataclass
class Process:
    """A process in its container and job, and the parent's ends of its pipes."""

    pid: int
    handle: int
    job: jobs.Job
    container: Container
    stdin: BinaryIO
    stdout: BinaryIO
    stderr: BinaryIO
    timed_out: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def returncode(self) -> int | None:
        """Its exit code, or None while it runs."""
        code = win32.D()
        win32.check(win32.kernel32().GetExitCodeProcess(self.handle, ctypes.byref(code)),
                    "GetExitCodeProcess")
        # A process can exit with 259 itself; only a wait can tell.
        if code.value == win32.STILL_ACTIVE and \
                win32.kernel32().WaitForSingleObject(self.handle, 0) == win32.WAIT_TIMEOUT:
            return None
        return code.value

    def wait(self, timeout: float | None = None) -> int | None:
        """Its exit code once it ends, or None if `timeout` seconds pass first."""
        millis = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        if win32.kernel32().WaitForSingleObject(self.handle, millis) == win32.WAIT_TIMEOUT:
            return None
        return self.returncode()

    def kill(self, code: int = ENDED) -> None:
        """End it, and anything in its job, with exit code `code`."""
        with self._lock:
            self.job.terminate(code)

    def _time_out(self) -> None:
        self.timed_out = True
        self.kill(TIMED_OUT)

    @contextlib.contextmanager
    def deadline(self, seconds: float) -> Iterator[None]:
        """End the process if the block takes longer than `seconds`. Kept by
        a timer in this process: what runs in the child cannot stop it."""
        timer = threading.Timer(seconds, self._time_out)
        timer.daemon = True
        timer.start()
        try:
            yield
        finally:
            timer.cancel()

    def close(self) -> None:
        """End it, and let go of every handle to it."""
        self.job.end(ENDED)
        for stream in (self.stdin, self.stdout, self.stderr):
            with contextlib.suppress(OSError):
                stream.close()
        if self.handle:
            win32.kernel32().CloseHandle(self.handle)
            self.handle = 0

    def __enter__(self) -> Process:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _create(api, argv: list[str], cwd: Path, env: dict[str, str], children: list[int],
            sids: list, lpac: bool, process: win32.PROCESS_INFORMATION) -> None:
    """CreateProcessW, suspended, in the container whose package SID is
    `sids[0]`, carrying the capabilities `sids[1:]`, inheriting `children`
    alone."""
    handles = (win32.H * 3)(*children)
    security = win32.SECURITY_CAPABILITIES()
    security.AppContainerSid = sids[0].value
    granted = (win32.SID_AND_ATTRIBUTES * max(1, len(sids) - 1))()
    for index, sid in enumerate(sids[1:]):
        granted[index].Sid, granted[index].Attributes = sid.value, win32.SE_GROUP_ENABLED
    if len(sids) > 1:
        security.Capabilities = ctypes.cast(granted, ctypes.POINTER(win32.SID_AND_ATTRIBUTES))
    security.CapabilityCount = len(sids) - 1
    attributes = [(win32.PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles),
                  (win32.PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, security)]
    if lpac:
        attributes.append((win32.PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY,
                           win32.D(win32.PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT)))
    size = win32.SZ(0)
    api.InitializeProcThreadAttributeList(None, len(attributes), 0, ctypes.byref(size))
    listed = (ctypes.c_byte * size.value)()
    win32.check(api.InitializeProcThreadAttributeList(listed, len(attributes), 0,
                                                      ctypes.byref(size)),
                "InitializeProcThreadAttributeList")
    try:
        # The list points into `attributes`' values, which live until this returns.
        for attribute, value in attributes:
            win32.check(api.UpdateProcThreadAttribute(
                listed, 0, attribute, ctypes.addressof(value), ctypes.sizeof(value), None, None),
                f"UpdateProcThreadAttribute(0x{attribute:X})")
        startup = win32.STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(win32.STARTUPINFOEXW)
        startup.StartupInfo.dwFlags = win32.STARTF_USESTDHANDLES
        (startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput,
         startup.StartupInfo.hStdError) = children
        startup.lpAttributeList = ctypes.addressof(listed)
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        block = _block(env)
        # No console: the child talks over its pipes, and a container cannot
        # attach to the hidden console CREATE_NO_WINDOW makes.
        flags = (win32.EXTENDED_STARTUPINFO_PRESENT | win32.CREATE_SUSPENDED
                 | win32.CREATE_UNICODE_ENVIRONMENT | win32.DETACHED_PROCESS)
        win32.check(api.CreateProcessW(argv[0], command, None, None, True, flags,
                                       ctypes.addressof(block), str(cwd),
                                       ctypes.addressof(startup), ctypes.byref(process)),
                    "CreateProcessW")
    finally:
        api.DeleteProcThreadAttributeList(listed)


def start(argv: list[str], *, container: Container, cwd: Path, limits: jobs.Limits,
          env: dict[str, str], capabilities: tuple[str, ...] = (),
          lpac: bool = True) -> Process:
    """Run `argv` in `container`, held by a job with `limits`.

    `capabilities` are SIDs the container carries (`profiles.capability`);
    with `lpac` it is less-privileged, which leaves it nothing granted to
    every app package, only what names it or its capabilities. `cwd` must
    be a folder the container is granted. `env` is the whole environment
    (`environment`)."""
    api = win32.kernel32()
    process = win32.PROCESS_INFORMATION()
    with contextlib.ExitStack() as cleanup:
        parents = []
        children = []
        for child_reads in (True, False, False):
            parent, child = _pipe(child_reads)
            parents.append(parent)
            children.append(child)
            cleanup.callback(api.CloseHandle, child)
        # The parent's ends are closed here only if the launch fails.
        on_failure = cleanup.enter_context(contextlib.ExitStack())
        for parent in parents:
            on_failure.callback(api.CloseHandle, parent)
        sids = [cleanup.enter_context(win32.Sid(text)) for text in (container.sid, *capabilities)]
        _create(api, argv, cwd, env, children, sids, lpac, process)
        try:
            job = jobs.contain(process.hProcess, limits)
        except OSError:
            api.TerminateProcess(process.hProcess, ENDED)
            api.CloseHandle(process.hThread)
            api.CloseHandle(process.hProcess)
            raise
        on_failure.pop_all()
    api.ResumeThread(process.hThread)
    api.CloseHandle(process.hThread)
    stdin, stdout, stderr = parents
    return Process(
        pid=process.dwProcessId, handle=process.hProcess, job=job, container=container,
        stdin=os.fdopen(msvcrt.open_osfhandle(stdin, 0), "wb", buffering=0),
        stdout=os.fdopen(msvcrt.open_osfhandle(stdout, os.O_RDONLY), "rb"),
        stderr=os.fdopen(msvcrt.open_osfhandle(stderr, os.O_RDONLY), "rb"),
    )
