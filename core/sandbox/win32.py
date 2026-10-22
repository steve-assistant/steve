"""The Windows calls the sandbox makes, typed, and bound when first asked.

Spike S1 reached every call the sandbox needs through ctypes, with no
helper program and no pywin32. Two of its faults were layouts: a structure
one field short, and a pointer passed where Windows wanted the address of
one. Each corrupted memory quietly rather than failing. So every structure
is written out once here, from the headers, and every function is bound with
its argument and result types, so that a wrong argument raises in Python
instead of reaching Windows.

The bindings are made on first use, not at import: a module that only names
the sandbox (the health source, the tests' fences) imports this without
touching a DLL. `userenv()` is the one door to AppContainer profiles, and the
tests' fences shut it for every test not marked `appcontainer`
(tests/gate/fences.py): a profile is written to the user's registry and
their AppData, outside any test's own folder.
"""

from __future__ import annotations

import ctypes
import functools
from ctypes import wintypes as wt
from types import SimpleNamespace

H, P, D, B = wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.BOOL
SZ = ctypes.c_size_t

# Process creation.
HANDLE_FLAG_INHERIT = 0x1
STARTF_USESTDHANDLES = 0x100
CREATE_SUSPENDED = 0x4
DETACHED_PROCESS = 0x8
CREATE_UNICODE_ENVIRONMENT = 0x400
EXTENDED_STARTUPINFO_PRESENT = 0x80000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x20009
PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY = 0x2000F
PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT = 0x1
SE_GROUP_ENABLED = 0x4
WAIT_TIMEOUT = 0x102
STILL_ACTIVE = 259

# Job objects.
JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
# Handles, both ways of the clipboard, system parameters, display settings,
# global atoms, desktops, and logging off or shutting down.
JOB_OBJECT_UILIMIT_ALL = 0xFF

# Access lists.
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x4
SDDL_REVISION_1 = 1
GRANT_ACCESS, REVOKE_ACCESS = 1, 4
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
TRUSTEE_IS_SID = 0
FILE_READ_EXECUTE = 0x1200A9  # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
FILE_MODIFY = 0x1301BF  # read, write, execute and delete
TOKEN_QUERY = 0x8
TokenUser = 1

# Named pipes.
PIPE_ACCESS_DUPLEX = 0x3
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_TYPE_BYTE = 0x0
PIPE_READMODE_BYTE = 0x0
PIPE_WAIT = 0x0
PIPE_REJECT_REMOTE_CLIENTS = 0x8
PIPE_UNLIMITED_INSTANCES = 255
ERROR_PIPE_CONNECTED = 535
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
# What a read or write says once the pipe's other end has gone.
PIPE_ENDED = frozenset({109, 232, 233})  # BROKEN_PIPE, NO_DATA, PIPE_NOT_CONNECTED
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# What CreateAppContainerProfile answers for a name already made.
HR_ALREADY_EXISTS = 0x800700B7
# What DeleteAppContainerProfile answers for a name that is not there.
HR_NOT_FOUND = 0x80070002


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", D), ("lpSecurityDescriptor", P), ("bInheritHandle", B)]


class OVERLAPPED(ctypes.Structure):
    # The offsets' union is two DWORDs; a pipe uses neither.
    _fields_ = [("Internal", SZ), ("InternalHigh", SZ), ("Offset", D), ("OffsetHigh", D),
                ("hEvent", H)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", P), ("Attributes", D)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", P), ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
                ("CapabilityCount", D), ("Reserved", D)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", D), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                ("lpTitle", wt.LPWSTR), ("dwX", D), ("dwY", D), ("dwXSize", D),
                ("dwYSize", D), ("dwXCountChars", D), ("dwYCountChars", D),
                ("dwFillAttribute", D), ("dwFlags", D), ("wShowWindow", wt.WORD),
                ("cbReserved2", wt.WORD), ("lpReserved2", P), ("hStdInput", H),
                ("hStdOutput", H), ("hStdError", H)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", P)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", H), ("hThread", H), ("dwProcessId", D), ("dwThreadId", D)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", D),
                ("MinimumWorkingSetSize", SZ), ("MaximumWorkingSetSize", SZ),
                ("ActiveProcessLimit", D), ("Affinity", SZ), ("PriorityClass", D),
                ("SchedulingClass", D)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", SZ),
                ("JobMemoryLimit", SZ), ("PeakProcessMemoryUsed", SZ),
                ("PeakJobMemoryUsed", SZ)]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    # The second field is a union of CpuRate, Weight and a pair of WORDs;
    # the hard cap uses CpuRate, in hundredths of a percent of the machine.
    _fields_ = [("ControlFlags", D), ("CpuRate", D)]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [("pMultipleTrustee", P), ("MultipleTrusteeOperation", ctypes.c_int),
                ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int),
                ("ptstrName", P)]


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [("grfAccessPermissions", D), ("grfAccessMode", ctypes.c_int),
                ("grfInheritance", D), ("Trustee", TRUSTEE_W)]


def _bind(dll, table: dict) -> SimpleNamespace:
    bound = {}
    for name, (result, *arguments) in table.items():
        function = getattr(dll, name)
        function.restype = result
        function.argtypes = arguments
        bound[name] = function
    return SimpleNamespace(**bound)


@functools.cache
def kernel32() -> SimpleNamespace:
    return _bind(ctypes.WinDLL("kernel32", use_last_error=True), {
        "CloseHandle": (B, H),
        "GetCurrentProcess": (H,),
        "CreatePipe": (B, ctypes.POINTER(H), ctypes.POINTER(H),
                       ctypes.POINTER(SECURITY_ATTRIBUTES), D),
        "SetHandleInformation": (B, H, D, D),
        "InitializeProcThreadAttributeList": (B, P, D, D, ctypes.POINTER(SZ)),
        "UpdateProcThreadAttribute": (B, P, D, SZ, P, SZ, P, P),
        "DeleteProcThreadAttributeList": (None, P),
        "CreateProcessW": (B, wt.LPCWSTR, wt.LPWSTR, P, P, B, D, P, wt.LPCWSTR, P,
                           ctypes.POINTER(PROCESS_INFORMATION)),
        "CreateJobObjectW": (H, P, wt.LPCWSTR),
        "SetInformationJobObject": (B, H, ctypes.c_int, P, D),
        "QueryInformationJobObject": (B, H, ctypes.c_int, P, D, ctypes.POINTER(D)),
        "AssignProcessToJobObject": (B, H, H),
        "TerminateJobObject": (B, H, wt.UINT),
        "ResumeThread": (D, H),
        "TerminateProcess": (B, H, wt.UINT),
        "WaitForSingleObject": (D, H, D),
        "GetExitCodeProcess": (B, H, ctypes.POINTER(D)),
        "LocalFree": (P, P),
        "CreateNamedPipeW": (H, wt.LPCWSTR, D, D, D, D, D, D,
                             ctypes.POINTER(SECURITY_ATTRIBUTES)),
        "ConnectNamedPipe": (B, H, P),
        "DisconnectNamedPipe": (B, H),
        "GetNamedPipeClientProcessId": (B, H, ctypes.POINTER(wt.ULONG)),
        "CancelIoEx": (B, H, P),
        "CreateEventW": (H, P, B, B, wt.LPCWSTR),
        "ReadFile": (B, H, P, D, P, P),
        "WriteFile": (B, H, P, D, P, P),
        "GetOverlappedResult": (B, H, P, ctypes.POINTER(D), B),
    })


@functools.cache
def advapi32() -> SimpleNamespace:
    return _bind(ctypes.WinDLL("advapi32", use_last_error=True), {
        "ConvertSidToStringSidW": (B, P, ctypes.POINTER(P)),
        "ConvertStringSidToSidW": (B, wt.LPCWSTR, ctypes.POINTER(P)),
        "FreeSid": (P, P),
        "GetNamedSecurityInfoW": (D, wt.LPCWSTR, ctypes.c_int, D, P, P, ctypes.POINTER(P),
                                  P, ctypes.POINTER(P)),
        "SetNamedSecurityInfoW": (D, wt.LPWSTR, ctypes.c_int, D, P, P, P, P),
        "SetEntriesInAclW": (D, ctypes.c_ulong, ctypes.POINTER(EXPLICIT_ACCESS_W), P,
                             ctypes.POINTER(P)),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
            B, wt.LPCWSTR, D, ctypes.POINTER(P), ctypes.POINTER(D)),
        "ConvertSecurityDescriptorToStringSecurityDescriptorW": (
            B, P, D, D, ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(D)),
        "OpenProcessToken": (B, H, D, ctypes.POINTER(H)),
        "GetTokenInformation": (B, H, ctypes.c_int, P, D, ctypes.POINTER(D)),
    })


@functools.cache
def userenv() -> SimpleNamespace:
    """AppContainer profiles: the door the tests' fences shut."""
    return _bind(ctypes.WinDLL("userenv", use_last_error=True), {
        "CreateAppContainerProfile": (ctypes.c_long, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, P, D,
                                      ctypes.POINTER(P)),
        "DeriveAppContainerSidFromAppContainerName": (ctypes.c_long, wt.LPCWSTR,
                                                      ctypes.POINTER(P)),
        "DeleteAppContainerProfile": (ctypes.c_long, wt.LPCWSTR),
    })


@functools.cache
def kernelbase() -> SimpleNamespace:
    return _bind(ctypes.WinDLL("kernelbase", use_last_error=True), {
        "DeriveCapabilitySidsFromName": (B, wt.LPCWSTR, ctypes.POINTER(P), ctypes.POINTER(D),
                                         ctypes.POINTER(P), ctypes.POINTER(D)),
    })


def check(ok, what: str) -> None:
    """Raise the last Windows error, named, if `ok` is false."""
    if not ok:
        error = ctypes.get_last_error()
        raise OSError(None, f"{what} failed: {ctypes.FormatError(error).strip()}", None, error)


def check_hr(hr: int, what: str) -> None:
    if hr:
        raise OSError(None, f"{what} failed with 0x{hr & 0xFFFFFFFF:08X}")


def sid_text(psid) -> str:
    """A SID's `S-1-...` form."""
    out = P()
    check(advapi32().ConvertSidToStringSidW(psid, ctypes.byref(out)), "ConvertSidToStringSidW")
    try:
        return ctypes.wstring_at(out.value)
    finally:
        kernel32().LocalFree(out)


class Sid:
    """A SID from its `S-1-...` form, freed when the block ends."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pointer = P()

    def __enter__(self) -> P:
        check(advapi32().ConvertStringSidToSidW(self.text, ctypes.byref(self.pointer)),
              f"ConvertStringSidToSidW({self.text})")
        return self.pointer

    def __exit__(self, *exc) -> None:
        kernel32().LocalFree(self.pointer)


def user_sid() -> str:
    """The SID of the Windows user this process runs as."""
    token = H()
    check(advapi32().OpenProcessToken(kernel32().GetCurrentProcess(), TOKEN_QUERY,
                                      ctypes.byref(token)), "OpenProcessToken")
    try:
        return token_sid(token)
    finally:
        kernel32().CloseHandle(token)


def token_sid(token) -> str:
    """The SID of the user a token is for."""
    size = D()
    advapi32().GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    check(advapi32().GetTokenInformation(token, TokenUser, buffer, size, ctypes.byref(size)),
          "GetTokenInformation(TokenUser)")
    return sid_text(SID_AND_ATTRIBUTES.from_buffer(buffer).Sid)


def security_descriptor(sddl: str) -> P:
    """A self-relative security descriptor from SDDL; LocalFree it."""
    out = P()
    check(advapi32().ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, SDDL_REVISION_1, ctypes.byref(out), None), "ConvertStringSecurityDescriptor")
    return out
