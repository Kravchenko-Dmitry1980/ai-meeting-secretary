"""Owned Windows process-tree containment. Importing creates no job or handle."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from threading import Lock


class ProcessJobError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OwnedProcessJobProof:
    pid: int
    creation_time: str
    executable_sha256: str
    argv_sha256: str


class WindowsJob:
    """Assign this process to a non-inheritable kill-on-close nested job.

    Children inherit membership, not the job HANDLE. Breakaway is never enabled.
    There is intentionally no close/context/finalizer: the handle must remain
    open until its owning process exits, including an abnormal process death.
    """
    def __init__(self):
        if os.name != "nt":
            raise ProcessJobError("windows_required")
        from .team_process_identity import process_identity
        try:
            owner_identity = process_identity()
        except ValueError:
            raise ProcessJobError("job_ownership_unverified") from None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Counters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE only
        if not handle:
            raise ProcessJobError("job_object_unavailable")
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            kernel.CloseHandle(handle)  # Current process was never assigned.
            raise ProcessJobError("job_object_unavailable")
        self.handle, self.kernel, self.pid = handle, kernel, os.getpid()
        self._owner_identity = owner_identity
        self._limits_type = Extended

    def verify_owned_identity(self):
        """Read actual kernel membership/limits and return the bound identity.

        A duck-typed callback, subclass or user-supplied boolean is not evidence.
        Core must still match this identity to its own participant/run record.
        """
        if (type(self) is not WindowsJob or getattr(self, "pid", None) != os.getpid()
                or not getattr(self, "handle", None) or not getattr(self, "_owner_identity", None)
                or not getattr(self, "_limits_type", None)):
            raise ProcessJobError("job_ownership_unverified")
        # Query real Win32 functions afresh, never a caller-supplied verifier.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        member = wintypes.BOOL()
        limits = self._limits_type()
        returned = wintypes.DWORD()
        if (not kernel.IsProcessInJob(kernel.GetCurrentProcess(), self.handle, ctypes.byref(member)) or not member.value
                or not kernel.QueryInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits), ctypes.byref(returned))
                or returned.value != ctypes.sizeof(limits)
                or not limits.BasicLimitInformation.LimitFlags & 0x2000
                or limits.BasicLimitInformation.LimitFlags & (0x0800 | 0x1000)):
            raise ProcessJobError("job_ownership_unverified")
        from .team_process_identity import process_identity
        try:
            current = process_identity()
        except ValueError:
            raise ProcessJobError("job_ownership_unverified") from None
        if current != self._owner_identity:
            raise ProcessJobError("job_ownership_unverified")
        return OwnedProcessJobProof(**current)


_owned_job = None
_lock = Lock()


def install_process_job():
    """Install once, before factories/native work; retain until process exit."""
    global _owned_job
    with _lock:
        if _owned_job is None:
            _owned_job = WindowsJob()
        if _owned_job.pid != os.getpid():
            raise ProcessJobError("job_owner_changed")
        return _owned_job
