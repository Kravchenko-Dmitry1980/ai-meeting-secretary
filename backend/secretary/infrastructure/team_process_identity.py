"""Sanitized Windows process identity for durable maintenance participation."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Literal

import psutil


@dataclass(frozen=True)
class DeadProcessProof:
    pid: int
    creation_time: str
    executable_sha256: str
    argv_sha256: str
    state: Literal["exited", "pid_reused"]
    observed_at: str


def verify_dead_identity(identity):
    """Prove the stored process lifetime ended; never terminate another process.

    This proof does not authorize retiring a ticket without its durable receipt
    checkpoint. Access denied and unknown states always raise, never mean dead.
    """
    fields = {"pid", "creation_time", "executable_sha256", "argv_sha256"}
    if (not isinstance(identity, dict) or set(identity) != fields
            or type(identity["pid"]) is not int or identity["pid"] < 1
            or not isinstance(identity["creation_time"], str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", identity["creation_time"])
            or any(not isinstance(identity[key], str) or not re.fullmatch(r"[0-9a-f]{64}", identity[key]) for key in ("executable_sha256", "argv_sha256"))):
        raise ValueError("process_identity_invalid")
    if os.name != "nt":
        raise ValueError("windows_process_identity_required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, identity["pid"])
    if not handle:
        # ERROR_INVALID_PARAMETER for a valid positive PID means no such process;
        # ERROR_ACCESS_DENIED (including protected processes) remains unknown.
        if ctypes.get_last_error() != 87 or psutil.pid_exists(identity["pid"]):
            raise ValueError("process_identity_unavailable")
        state = "exited"
    else:
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                raise ValueError("process_identity_unavailable")
            creation = times[0].dwHighDateTime * 2**32 + times[0].dwLowDateTime
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ValueError("process_identity_unavailable")
            if str(creation) != identity["creation_time"]:
                state = "pid_reused"
            elif code.value != 259:
                state = "exited"
            else:
                raise ValueError("process_still_alive")
        finally:
            kernel.CloseHandle(handle)
    return DeadProcessProof(**identity, state=state, observed_at=datetime.now(timezone.utc).isoformat())


def process_identity(pid=None):
    """Read own identity, or an explicitly supplied owned-evidence PID.

    creation_time is decimal Windows FILETIME (100ns ticks since 1601 UTC).
    This read helper grants no authority to terminate a PID.
    """
    if os.name != "nt":
        raise ValueError("windows_process_identity_required")
    pid = os.getpid() if pid is None else pid
    if type(pid) is not int or pid < 1:
        raise ValueError("process_identity_invalid")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ValueError("process_identity_unavailable")
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            raise ValueError("process_identity_unavailable")
        creation = times[0].dwHighDateTime * 2**32 + times[0].dwLowDateTime
        process = psutil.Process(pid)
        executable = Path(process.exe())
        digest = hashlib.sha256()
        with executable.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        argv = json.dumps(process.cmdline(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        exit_code = wintypes.DWORD()
        if (not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259
                or abs(process.create_time() - (creation / 10**7 - 11644473600)) > .001):
            raise ValueError("process_identity_changed")
        return {"pid": pid, "creation_time": str(creation), "executable_sha256": digest.hexdigest(),
                "argv_sha256": hashlib.sha256(argv.encode("utf-8")).hexdigest()}
    except (psutil.Error, OSError):
        raise ValueError("process_identity_unavailable") from None
    finally:
        kernel.CloseHandle(handle)
