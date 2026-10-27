"""Read-only Windows memory measurements; unavailable values deny admission.

No model packages, subprocesses, device changes or guessed CUDA ordinals.
NVML is only queried when the configured profile names a physical GPU UUID.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import re
import threading
import time

from secretary.infrastructure.local_asr_resources import ResourceSnapshot

_MIB = 1024 ** 2
_NVML_LOCK = threading.Lock()


class _PerformanceInformation(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal",
            "PhysicalAvailable", "SystemCache", "KernelTotal", "KernelPaged",
            "KernelNonpaged", "PageSize")
    ] + [(name, wintypes.DWORD) for name in ("HandleCount", "ProcessCount", "ThreadCount")]


def _host_memory() -> tuple[float | None, float | None]:
    if os.name != "nt":
        return None, None
    try:
        library = ctypes.WinDLL("psapi.dll", winmode=0x00000800, use_last_error=True)
        query = library.GetPerformanceInfo
        query.argtypes = [ctypes.POINTER(_PerformanceInformation), wintypes.DWORD]
        query.restype = wintypes.BOOL
        info = _PerformanceInformation()
        info.cb = ctypes.sizeof(info)
        if not query(ctypes.byref(info), info.cb) or info.PageSize <= 0:
            return None, None
        if info.CommitTotal > info.CommitLimit or info.PhysicalAvailable > info.PhysicalTotal:
            return None, None
        return (info.PhysicalAvailable * info.PageSize / _MIB,
                (info.CommitLimit - info.CommitTotal) * info.PageSize / _MIB)
    except (OSError, AttributeError, ValueError):
        return None, None


class _NvmlMemory(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("total", "free", "used")]


def _gpu_memory(physical_gpu_id: str) -> float | None:
    if os.name != "nt" or not re.fullmatch(r"GPU-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", physical_gpu_id):
        return None
    # Serializing init/shutdown avoids competing reference-count lifetimes.
    with _NVML_LOCK:
        initialized = False
        try:
            library = ctypes.WinDLL("nvml.dll", winmode=0x00000800)
            library.nvmlInit_v2.argtypes = []
            library.nvmlInit_v2.restype = ctypes.c_int
            library.nvmlShutdown.argtypes = []
            library.nvmlShutdown.restype = ctypes.c_int
            handle = ctypes.c_void_p()
            library.nvmlDeviceGetHandleByUUID.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
            library.nvmlDeviceGetHandleByUUID.restype = ctypes.c_int
            library.nvmlDeviceGetMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_NvmlMemory)]
            library.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
            if library.nvmlInit_v2() != 0:
                return None
            initialized = True
            if library.nvmlDeviceGetHandleByUUID(physical_gpu_id.encode("ascii"), ctypes.byref(handle)) != 0:
                return None
            info = _NvmlMemory()
            if library.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(info)) != 0:
                return None
            if info.total == 0 or info.free > info.total or info.used > info.total:
                return None
            return info.free / _MIB
        except (OSError, AttributeError, ValueError, UnicodeError):
            return None
        finally:
            if initialized:
                library.nvmlShutdown()


def sample_resources(physical_gpu_id: str | None = None) -> ResourceSnapshot:
    """Timestamp precedes sampling: slow queries become stale, never fresh."""
    sampled_at = time.monotonic()
    ram, commit = _host_memory()
    gpu = {physical_gpu_id: _gpu_memory(physical_gpu_id)} if physical_gpu_id else {}
    return ResourceSnapshot(ram, commit, gpu, sampled_at)
