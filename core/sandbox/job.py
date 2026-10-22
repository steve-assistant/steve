"""A Windows job object: what holds a process to its limits, and ends it.

A process the backend starts can outlive the backend, start processes of its
own, and take the machine's memory and CPU. The local decision model's
server did the first: one left behind held 5 GB of the GPU until the user
noticed (core/decider.py). A module's code could do all of it, and on this
laptop heavy CPU stalls the user's desk voice.

So every such process is put in a job before its first instruction runs (it
is started suspended): the job ends everything in it when its last handle
closes, however the backend ends, and holds the limits below. Assigning can
fail, and then the process is ended, never run outside the job: a process
nothing can stop is the thing the job exists to prevent.

Moved here from self-upgrade v1's coding sandbox (core/coder/sandbox.py, gone
since step 6), where it was written for one caller and borrowed by another
(core/decider._contain); the modules' sandbox is the third.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

from core.sandbox import win32


@dataclass(frozen=True)
class Limits:
    """What a job holds its processes to. None is no limit.

    `cpu_cores` is a hard cap on CPU time, in cores' worth of the machine:
    Windows takes it as hundredths of a percent of every processor, so one
    core on a sixteen-core laptop is 625."""

    memory: int | None = None  # bytes, for each process
    job_memory: int | None = None  # bytes, for all of them together
    processes: int | None = None
    cpu_cores: float | None = None
    ui: bool = False  # no clipboard, desktops, atoms or system settings
    die_on_unhandled: bool = False  # no error dialog that waits for a click


# No limit but the one every job has: what is in it ends when it closes.
KILL_ON_CLOSE = Limits()


def cpu_rate(cores: float) -> int:
    """`cores` as the job's CPU rate: hundredths of a percent of the machine."""
    return max(1, min(10_000, round(cores * 10_000 / (os.cpu_count() or 1))))


class Job:
    """One job object, made with its limits. Kill-on-close always."""

    def __init__(self, limits: Limits = KILL_ON_CLOSE) -> None:
        self.limits = limits
        api = win32.kernel32()
        self.handle = api.CreateJobObjectW(None, None)
        win32.check(self.handle, "CreateJobObjectW")
        try:
            self._set(limits)
        except OSError:
            self.close()
            raise

    def _set(self, limits: Limits) -> None:
        api = win32.kernel32()
        info = win32.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = win32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if limits.memory is not None:
            flags |= win32.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = limits.memory
        if limits.job_memory is not None:
            flags |= win32.JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = limits.job_memory
        if limits.processes is not None:
            flags |= win32.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = limits.processes
        if limits.die_on_unhandled:
            flags |= win32.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        info.BasicLimitInformation.LimitFlags = flags
        win32.check(api.SetInformationJobObject(
            self.handle, win32.JobObjectExtendedLimitInformation, ctypes.byref(info),
            ctypes.sizeof(info)), "SetInformationJobObject(limits)")
        if limits.cpu_cores is not None:
            rate = win32.JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(
                win32.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                | win32.JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP, cpu_rate(limits.cpu_cores))
            win32.check(api.SetInformationJobObject(
                self.handle, win32.JobObjectCpuRateControlInformation, ctypes.byref(rate),
                ctypes.sizeof(rate)), "SetInformationJobObject(CPU rate)")
        if limits.ui:
            ui = win32.D(win32.JOB_OBJECT_UILIMIT_ALL)
            win32.check(api.SetInformationJobObject(
                self.handle, win32.JobObjectBasicUIRestrictions, ctypes.byref(ui),
                ctypes.sizeof(ui)), "SetInformationJobObject(UI)")

    def assign(self, process_handle: int) -> None:
        """Put a process in the job. OSError if Windows refuses; the caller
        then ends the process rather than run it outside."""
        win32.check(win32.kernel32().AssignProcessToJobObject(self.handle, process_handle),
                    "AssignProcessToJobObject")

    def terminate(self, code: int) -> None:
        """End every process in the job now, each with exit code `code`."""
        if self.handle:
            win32.kernel32().TerminateJobObject(self.handle, code)

    def close(self) -> None:
        """Let the job go; kill-on-close ends what is still in it."""
        if self.handle:
            win32.kernel32().CloseHandle(self.handle)
            self.handle = None

    def end(self, code: int = 1) -> None:
        self.terminate(code)
        self.close()

    def held_to(self) -> dict:
        """The limits as Windows holds them, read back from the job rather
        than from what was asked: a cap that did not take shows here."""
        api = win32.kernel32()
        info = win32.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        win32.check(api.QueryInformationJobObject(
            self.handle, win32.JobObjectExtendedLimitInformation, ctypes.byref(info),
            ctypes.sizeof(info), None), "QueryInformationJobObject(limits)")
        rate = win32.JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        win32.check(api.QueryInformationJobObject(
            self.handle, win32.JobObjectCpuRateControlInformation, ctypes.byref(rate),
            ctypes.sizeof(rate), None), "QueryInformationJobObject(CPU rate)")
        ui = win32.D()
        win32.check(api.QueryInformationJobObject(
            self.handle, win32.JobObjectBasicUIRestrictions, ctypes.byref(ui),
            ctypes.sizeof(ui), None), "QueryInformationJobObject(UI)")
        flags = info.BasicLimitInformation.LimitFlags
        hard = (win32.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                | win32.JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP)
        return {
            "kill_on_close": bool(flags & win32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
            "memory": (info.ProcessMemoryLimit
                       if flags & win32.JOB_OBJECT_LIMIT_PROCESS_MEMORY else None),
            "processes": (info.BasicLimitInformation.ActiveProcessLimit
                          if flags & win32.JOB_OBJECT_LIMIT_ACTIVE_PROCESS else None),
            "cpu_rate": rate.CpuRate if rate.ControlFlags & hard == hard else None,
            "ui": ui.value,
        }


def contain(process_handle: int, limits: Limits = KILL_ON_CLOSE) -> Job:
    """A new job holding the process: OSError, with no job left open, if it
    cannot be made or the process cannot be put in it."""
    job = Job(limits)
    try:
        job.assign(process_handle)
    except OSError:
        job.close()
        raise
    return job
