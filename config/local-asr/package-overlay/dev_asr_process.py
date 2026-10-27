"""Windows-only, owned Job Object containment for one canonical ASR stage.

No model imports, process-name kills, PID reopening or outer job manipulation.
The child is assigned while suspended, before it can create descendants. This
helper qualifies process containment only, not the model/resource profile.

API contracts: learn.microsoft.com/windows/win32/api/jobapi2/
nf-jobapi2-assignprocesstojobobject and nf-jobapi2-terminatejobobject.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import os
import subprocess
import time


CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
KILL_ON_JOB_CLOSE = 0x00002000
WAIT_OBJECT_0 = 0
ERROR_NO_MORE_FILES = 18
CLEANUP_SECONDS = 10.0


class StageProcessStopped(RuntimeError):
    def __init__(self, message, exit_code):
        super().__init__(message)
        self.exit_code = exit_code


class StageProcessCleanupError(RuntimeError):
    """Containment was attempted but verified exit could not be established."""
    exit_code = 78


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
               ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
               ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
    _fields_ += [(name, wintypes.DWORD) for name in (
        "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
               ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
               ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
               ("dwFlags", wintypes.DWORD)]


class _WindowsJob:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("DEV ASR owned stage requires Windows")
        self.owner_pid = os.getpid()
        self.handle = None
        self.assigned = False
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        declarations = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
            "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetProcessIdOfThread": ([wintypes.HANDLE], wintypes.DWORD),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        handle = self.kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def _owned(self):
        if self.owner_pid != os.getpid() or not self.handle:
            raise StageProcessCleanupError("DEV ASR job ownership unavailable")

    def assign(self, process):
        self._owned()
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.assigned = True
        membership = wintypes.BOOL()
        if (not self.kernel.IsProcessInJob(int(process._handle), self.handle, ctypes.byref(membership))
                or not membership.value):
            raise StageProcessCleanupError("DEV ASR job membership unavailable")

    def _initial_thread(self, process):
        snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        matches = []
        try:
            row = _ThreadEntry()
            row.dwSize = ctypes.sizeof(row)
            present = self.kernel.Thread32First(snapshot, ctypes.byref(row))
            while present:
                if row.dwSize < _ThreadEntry.th32OwnerProcessID.offset + ctypes.sizeof(wintypes.DWORD):
                    raise StageProcessCleanupError("DEV ASR thread snapshot incomplete")
                if row.th32OwnerProcessID == process.pid:
                    matches.append(row.th32ThreadID)
                row.dwSize = ctypes.sizeof(row)
                present = self.kernel.Thread32Next(snapshot, ctypes.byref(row))
            if ctypes.get_last_error() != ERROR_NO_MORE_FILES:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(snapshot)
        if len(matches) != 1:
            raise StageProcessCleanupError("DEV ASR initial thread is ambiguous")
        return matches[0]

    def resume(self, process):
        self._owned()
        if not self.assigned:
            raise StageProcessCleanupError("DEV ASR uncontained child cannot resume")
        thread = self.kernel.OpenThread(0x0002 | 0x0800, False, self._initial_thread(process))
        if not thread:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if (self.kernel.GetProcessIdOfThread(thread) != process.pid
                    or self.kernel.WaitForSingleObject(int(process._handle), 0) != 258):
                raise StageProcessCleanupError("DEV ASR initial thread owner changed")
            if self.kernel.ResumeThread(thread) != 1:
                raise StageProcessCleanupError("DEV ASR initial thread was not suspended once")
        finally:
            self.kernel.CloseHandle(thread)

    def active(self):
        self._owned()
        accounting = _Accounting()
        returned = wintypes.DWORD()
        if (not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                      ctypes.sizeof(accounting), ctypes.byref(returned))
                or returned.value != ctypes.sizeof(accounting)):
            raise StageProcessCleanupError("DEV ASR job accounting unavailable")
        return accounting.ActiveProcesses

    def terminate(self):
        self._owned()
        if not self.kernel.TerminateJobObject(self.handle, 78) and self.active():
            raise StageProcessCleanupError("DEV ASR owned job termination failed")

    def wait_empty(self, deadline):
        while self.active():
            if time.monotonic() >= deadline:
                raise StageProcessCleanupError("DEV ASR owned job exit unverified")
            time.sleep(.02)

    def close(self):
        if self.handle:
            self._owned()
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise StageProcessCleanupError("DEV ASR owned job handle close failed")


def _reap(process, job, *, terminate):
    deadline = time.monotonic() + CLEANUP_SECONDS
    try:
        if terminate:
            if not job.assigned and process.poll() is None:
                # This child never resumed or spawned descendants. Popen.kill
                # uses the retained CreateProcess handle, never a reopened PID.
                # Do this before any empty-job query which could itself fail.
                process.kill()
            elif job.assigned:
                job.terminate()
        process.wait(timeout=max(.01, deadline - time.monotonic()))
        if process.stderr:
            process.stderr.close()
        # Accounting may retain a terminated root while our process handle is
        # open. returncode has been established; no later poll/wait needs it.
        process._handle.Close()
        if not terminate and job.active():
            job.terminate()  # Success must not leak a detached descendant.
        job.wait_empty(deadline)
    except BaseException as error:
        raise StageProcessCleanupError("DEV ASR worker cleanup unverified") from error


def run_owned_stage(command, *, environment=None, timeout_seconds=1000,
                    cancel_requested=None, on_poll=None):
    """Return the original (returncode, stderr), with an owned bounded tree."""
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("DEV ASR stage deadline must be finite and positive")
    if (not isinstance(command, (list, tuple)) or not command
            or any(not isinstance(value, (str, os.PathLike)) for value in command)):
        raise ValueError("DEV ASR stage requires an explicit argument list")
    job = _WindowsJob()
    process = None
    deadline = time.monotonic() + timeout_seconds
    try:
        # Retain the instance before __init__: a post-creation wrapper (such as
        # the offline process-registration guard) can raise after CreateProcess
        # succeeds. Its still-suspended native child must remain ours to reap.
        process = subprocess.Popen.__new__(subprocess.Popen)
        subprocess.Popen.__init__(process, command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=environment, creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW)
        job.assign(process)
        job.resume(process)
        while True:
            if on_poll is not None:
                on_poll(process)
            if cancel_requested is not None and cancel_requested():
                raise StageProcessStopped("DEV ASR worker cancelled", 130)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            try:
                _, stderr = process.communicate(timeout=min(.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException as error:
        if process is not None and getattr(process, "_handle", None) is not None:
            _reap(process, job, terminate=True)
        if isinstance(error, subprocess.TimeoutExpired):
            raise StageProcessStopped("DEV ASR worker timed out", 124) from error
        raise
    else:
        _reap(process, job, terminate=False)
        return process.returncode, stderr
    finally:
        job.close()  # KILL_ON_JOB_CLOSE also covers abnormal cleanup paths.
