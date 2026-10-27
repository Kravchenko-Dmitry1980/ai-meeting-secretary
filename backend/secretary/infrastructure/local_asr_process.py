"""Synchronous Windows supervisor for one owned, memory-limited process tree.

The parent owns an unnamed Job Object for the entire call. Its child is assigned
while suspended. Job commit is a hard allocation ceiling; working-set limits
are per process, while aggregate RSS is a sampled watchdog, not an atomic cap.
No models, PID-based termination, outer job mutation, or platform fallback.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import os
import subprocess
import threading
import time


CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
KILL_ON_JOB_CLOSE = 0x00002000
WAIT_OBJECT_0 = 0
ERROR_NO_MORE_FILES = 18
CLEANUP_SECONDS = 10.0
MIB = 1024 ** 2
JOB_MEMORY_LIMIT = 0x00000200
WORKING_SET_LIMIT = 0x00000001


@dataclass(frozen=True)
class StageProcessReceipt:
    returncode: int
    stdout: bytes
    stderr: bytes
    cleanup_confirmed: bool
    peak_owned_rss_bytes: int
    job_commit_limit_bytes: int
    working_set_limit_bytes: int


class StageProcessStopped(RuntimeError):
    """A bounded stop. Only the supervisor can supply confirmed cleanup."""
    def __init__(self, code, exit_code, *, cleanup_confirmed=False):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.cleanup_confirmed = cleanup_confirmed


class StageProcessCleanupError(RuntimeError):
    """Containment was attempted but verified exit could not be established."""
    exit_code = 78
    code = "local_asr_cleanup_unverified"
    cleanup_confirmed = False


class StageProcessUnavailable(RuntimeError):
    """Native containment could not be established; no child is launched."""
    code = "local_asr_native_containment_unavailable"
    cleanup_confirmed = False


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


class _MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)]
    _fields_ += [(name, ctypes.c_size_t) for name in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
        "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
        "PagefileUsage", "PeakPagefileUsage")]


class _WindowsJob:
    def __init__(self, *, job_commit_limit_bytes, working_set_limit_bytes):
        if os.name != "nt":
            raise StageProcessUnavailable("local_asr_native_windows_required")
        self.owner_pid = os.getpid()
        self.handle = None
        self.assigned = False
        self.root_handle = None
        self.root_pid = None
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
            "GetProcessId": ([wintypes.HANDLE], wintypes.DWORD),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
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
        limits.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE | JOB_MEMORY_LIMIT | WORKING_SET_LIMIT
        limits.BasicLimitInformation.MinimumWorkingSetSize = MIB
        limits.BasicLimitInformation.MaximumWorkingSetSize = working_set_limit_bytes
        limits.JobMemoryLimit = job_commit_limit_bytes
        try:
            if not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._verify_limits(limits)
            self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
            self.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD]
            self.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        except BaseException as error:
            self.close()
            raise StageProcessUnavailable("local_asr_native_limits_unavailable") from error

    def _verify_limits(self, requested):
        actual, returned = _ExtendedLimits(), wintypes.DWORD()
        if (not self.kernel.QueryInformationJobObject(self.handle, 9, ctypes.byref(actual),
                ctypes.sizeof(actual), ctypes.byref(returned)) or returned.value != ctypes.sizeof(actual)):
            raise StageProcessUnavailable("local_asr_native_limits_unverified")
        expected = requested.BasicLimitInformation
        observed = actual.BasicLimitInformation
        if (observed.LimitFlags != expected.LimitFlags
                or observed.MinimumWorkingSetSize != expected.MinimumWorkingSetSize
                or observed.MaximumWorkingSetSize != expected.MaximumWorkingSetSize
                or actual.JobMemoryLimit != requested.JobMemoryLimit):
            raise StageProcessUnavailable("local_asr_native_limits_unverified")

    def process_ids(self):
        """Read only: PIDs locate measurement handles, never termination targets."""
        self._owned()
        capacity = 16
        for _ in range(5):
            buffer = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(ctypes.c_size_t))
            ok = self.kernel.QueryInformationJobObject(self.handle, 3, buffer, len(buffer), None)
            assigned = wintypes.DWORD.from_buffer(buffer, 0).value
            count = wintypes.DWORD.from_buffer(buffer, 4).value
            if ok and assigned == count and count <= capacity:
                return tuple((ctypes.c_size_t * count).from_buffer(buffer, 8))
            if (not ok and ctypes.get_last_error() != 234) or assigned > 4096:
                break
            capacity = max(capacity * 2, assigned)
        raise StageProcessStopped("local_asr_rss_measurement_unavailable", 78)

    def owned_rss(self):
        for _ in range(3):
            members = self.process_ids()
            value = self._sample_rss(members)
            if set(members) == set(self.process_ids()):
                return value
        raise StageProcessStopped("local_asr_rss_measurement_unstable", 78)

    def _sample_rss(self, members):
        total = 0
        for pid in members:
            # Query/VM-read/synchronize rights only; the handle cannot kill.
            handle = self.kernel.OpenProcess(0x0400 | 0x0010 | 0x100000, False, pid)
            if not handle:
                if pid not in self.process_ids():
                    continue  # The member exited between list and open.
                raise StageProcessStopped("local_asr_rss_measurement_unavailable", 78)
            try:
                member = wintypes.BOOL()
                if not self.kernel.IsProcessInJob(handle, self.handle, ctypes.byref(member)):
                    raise StageProcessStopped("local_asr_rss_measurement_unavailable", 78)
                if not member.value:
                    # PID reuse is never authority to inspect a foreign process.
                    if pid not in self.process_ids():
                        continue
                    raise StageProcessStopped("local_asr_rss_membership_changed", 78)
                counters = _MemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                if not self.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), ctypes.sizeof(counters)):
                    if self.kernel.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0:
                        continue
                    raise StageProcessStopped("local_asr_rss_measurement_unavailable", 78)
                total += counters.WorkingSetSize
            finally:
                if not self.kernel.CloseHandle(handle):
                    raise StageProcessCleanupError("local_asr_measurement_handle_close_failed")
        return total

    def _owned(self):
        if self.owner_pid != os.getpid() or not self.handle:
            raise StageProcessCleanupError("DEV ASR job ownership unavailable")

    def assign(self, process):
        self._owned()
        self.root_handle = int(process._handle)
        self.root_pid = self.kernel.GetProcessId(self.root_handle)
        if not self.root_pid or self.root_pid != process.pid:
            raise StageProcessStopped("local_asr_process_identity_unverified", 78)
        if not self.kernel.AssignProcessToJobObject(self.handle, self.root_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self.assigned = True
        membership = wintypes.BOOL()
        if (not self.kernel.IsProcessInJob(int(process._handle), self.handle, ctypes.byref(membership))
                or not membership.value):
            raise StageProcessCleanupError("DEV ASR job membership unavailable")

    def _initial_thread(self, pid):
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
                if row.th32OwnerProcessID == pid:
                    matches.append(row.th32ThreadID)
                row.dwSize = ctypes.sizeof(row)
                present = self.kernel.Thread32Next(snapshot, ctypes.byref(row))
            if ctypes.get_last_error() != ERROR_NO_MORE_FILES:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if not self.kernel.CloseHandle(snapshot):
                raise StageProcessCleanupError("local_asr_snapshot_handle_close_failed")
        if len(matches) != 1:
            raise StageProcessCleanupError("DEV ASR initial thread is ambiguous")
        return matches[0]

    def resume(self, process):
        self._owned()
        if not self.assigned:
            raise StageProcessCleanupError("DEV ASR uncontained child cannot resume")
        if (int(process._handle) != self.root_handle or process.pid != self.root_pid
                or self.kernel.GetProcessId(self.root_handle) != self.root_pid):
            raise StageProcessStopped("local_asr_process_identity_changed", 78)
        thread = self.kernel.OpenThread(0x0002 | 0x0800, False, self._initial_thread(self.root_pid))
        if not thread:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if (self.kernel.GetProcessIdOfThread(thread) != self.root_pid
                    or self.kernel.WaitForSingleObject(self.root_handle, 0) != 258):
                raise StageProcessCleanupError("DEV ASR initial thread owner changed")
            if self.kernel.ResumeThread(thread) != 1:
                raise StageProcessCleanupError("DEV ASR initial thread was not suspended once")
        finally:
            if not self.kernel.CloseHandle(thread):
                raise StageProcessCleanupError("local_asr_thread_handle_close_failed")

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
        while self.active() or self.process_ids():
            if time.monotonic() >= deadline:
                raise StageProcessCleanupError("DEV ASR owned job exit unverified")
            time.sleep(.02)

    def close(self):
        if self.handle:
            self._owned()
            if not self.kernel.CloseHandle(self.handle):
                raise StageProcessCleanupError("DEV ASR owned job handle close failed")
            self.handle = None


class _Pipes:
    """Drain continuously while retaining at most each configured byte limit."""

    def __init__(self, process, input_bytes, stdout_limit, stderr_limit):
        self.stdout, self.stderr = bytearray(), bytearray()
        self.failure = None
        self.threads = []
        self.streams = (process.stdin, process.stdout, process.stderr)
        for stream, target, limit, code in (
                (process.stdout, self.stdout, stdout_limit, "local_asr_stdout_limit"),
                (process.stderr, self.stderr, stderr_limit, "local_asr_stderr_limit")):
            self.threads.append(threading.Thread(target=self._read,
                args=(stream, target, limit, code), daemon=True))
        self.threads.append(threading.Thread(target=self._write,
            args=(process.stdin, input_bytes), daemon=True))

    def start(self):
        for thread in self.threads:
            thread.start()

    def _read(self, stream, target, limit, code):
        try:
            while data := stream.read(65536):
                available = limit - len(target)
                target.extend(data[:available])
                if len(data) > available:
                    self.failure = self.failure or code
        except Exception:
            self.failure = self.failure or "local_asr_pipe_failed"
        finally:
            stream.close()

    def _write(self, stream, data):
        try:
            view = memoryview(data)
            while view:
                written = stream.write(view[:65536])
                if not written:
                    raise OSError("local_asr_stdin_failed")
                view = view[written:]
        except BrokenPipeError:
            pass  # A child may finish without consuming all supplied input.
        except Exception:
            self.failure = self.failure or "local_asr_pipe_failed"
        finally:
            stream.close()

    def finish(self, deadline):
        for thread in self.threads:
            if thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            raise StageProcessCleanupError("local_asr_pipe_cleanup_unverified")
        for stream in self.streams:
            stream.close()


def _reap(process, job, pipes, *, terminate):
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
        # Accounting may retain a terminated root while our process handle is
        # open. returncode has been established; no later poll/wait needs it.
        process._handle.Close()
        if not terminate and job.active():
            job.terminate()  # Success must not leak a detached descendant.
        job.wait_empty(deadline)
        if pipes is not None:
            pipes.finish(deadline)
        else:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    except BaseException as error:
        raise StageProcessCleanupError("DEV ASR worker cleanup unverified") from error


def run_owned_stage(command, *, environment=None, cwd=None, input_bytes=b"",
                    timeout_seconds=1000, stdout_limit=1024 * 1024, stderr_limit=65536,
                    job_commit_limit_bytes=8192 * MIB, working_set_limit_bytes=4608 * MIB,
                    owned_rss_limit_bytes=4608 * MIB, cancel_requested=None,
                    on_poll=None, on_started=None):
    """Run synchronously; confirmed return/stop means the complete Job is empty.

    ``on_started(Popen)`` runs after containment and before any child code.
    ``on_poll(Popen)`` and ``cancel_requested()`` must return promptly. A callback
    exception stops the owned tree and is chained to a text-free stopped error.
    Polling occurs at most every 100 ms apart from callback/native API latency.
    Native setup/readback failure never falls back to an uncontained process.
    Stdout/stderr are bounded bytes; ``cwd`` can hold caller-owned artifacts.
    """
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("local_asr_deadline_invalid")
    if (not isinstance(command, (list, tuple)) or not command
            or any(not isinstance(value, (str, os.PathLike)) for value in command)):
        raise ValueError("local_asr_command_invalid")
    for value, minimum, maximum, name in (
            (stdout_limit, 0, 64 * MIB, "stdout_limit"),
            (stderr_limit, 0, 64 * MIB, "stderr_limit"),
            (job_commit_limit_bytes, 4096, 8192 * MIB, "job_commit_limit"),
            (working_set_limit_bytes, MIB, 4608 * MIB, "working_set_limit"),
            (owned_rss_limit_bytes, 1, 4608 * MIB, "owned_rss_limit")):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"local_asr_{name}_invalid")
    if not isinstance(input_bytes, bytes) or len(input_bytes) > 64 * MIB:
        raise ValueError("local_asr_input_bytes_invalid")
    try:
        job = _WindowsJob(job_commit_limit_bytes=job_commit_limit_bytes,
                          working_set_limit_bytes=working_set_limit_bytes)
    except StageProcessUnavailable:
        raise
    except Exception as error:
        raise StageProcessUnavailable("local_asr_native_containment_unavailable") from error
    process = pipes = None
    peak_rss = 0
    pending = None
    deadline = time.monotonic() + timeout_seconds
    try:
        # Retain the instance before __init__: a post-creation wrapper (such as
        # the offline process-registration guard) can raise after CreateProcess
        # succeeds. Its still-suspended native child must remain ours to reap.
        process = subprocess.Popen.__new__(subprocess.Popen)
        subprocess.Popen.__init__(process, command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, cwd=cwd,
            env=environment, creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW)
        job.assign(process)
        pipes = _Pipes(process, input_bytes, stdout_limit, stderr_limit)
        pipes.start()
        if on_started is not None:
            on_started(process)
        if cancel_requested is not None and cancel_requested():
            raise StageProcessStopped("local_asr_cancelled", 130)
        if time.monotonic() >= deadline:
            raise StageProcessStopped("local_asr_timeout", 124)
        job.resume(process)
        while True:
            if on_poll is not None:
                on_poll(process)
            if cancel_requested is not None and cancel_requested():
                raise StageProcessStopped("local_asr_cancelled", 130)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StageProcessStopped("local_asr_timeout", 124)
            peak_rss = max(peak_rss, job.owned_rss())
            if peak_rss > owned_rss_limit_bytes:
                raise StageProcessStopped("local_asr_owned_rss_limit", 137)
            if pipes.failure:
                raise StageProcessStopped(pipes.failure, 137)
            if process.poll() is not None:
                break
            time.sleep(min(.1, remaining))
    except BaseException as error:
        pending = error
    finally:
        try:
            if process is not None and getattr(process, "_handle", None) is not None:
                _reap(process, job, pipes, terminate=pending is not None)
            else:
                job.wait_empty(time.monotonic() + CLEANUP_SECONDS)
        finally:
            job.close()  # Last-resort kill-on-close never substitutes for proof.
    # This point requires exhaustive native exit and successful handle close.
    if pending is not None:
        if isinstance(pending, StageProcessCleanupError):
            raise pending
        if isinstance(pending, StageProcessStopped):
            raise StageProcessStopped(pending.code, pending.exit_code, cleanup_confirmed=True) from pending
        if not isinstance(pending, Exception):
            raise pending
        raise StageProcessStopped("local_asr_stage_failed", 78, cleanup_confirmed=True) from pending
    if pipes.failure:
        raise StageProcessStopped(pipes.failure, 137, cleanup_confirmed=True)
    return StageProcessReceipt(process.returncode, bytes(pipes.stdout), bytes(pipes.stderr),
        True, peak_rss, job_commit_limit_bytes, working_set_limit_bytes)
