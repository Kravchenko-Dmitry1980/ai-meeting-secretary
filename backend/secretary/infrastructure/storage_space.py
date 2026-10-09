"""Disk-space checks for audio capture and the local import pipeline."""
from __future__ import annotations

import math
import errno
import shutil
from pathlib import Path
from typing import Iterable

MINIMUM_FREE_SPACE_BYTES = 1024 ** 3
CAPTURE_WORKING_SET_COPIES = 4


class StorageSpaceError(RuntimeError):
    """A storage check failed or the operation would consume the safety reserve."""


class InsufficientStorageError(StorageSpaceError):
    def __init__(self, available_bytes: int, required_bytes: int, reserve_bytes: int):
        self.available_bytes = available_bytes
        self.required_bytes = required_bytes
        self.reserve_bytes = reserve_bytes
        gib = 1024 ** 3
        super().__init__(
            "Недостаточно свободного места: "
            f"доступно {available_bytes / gib:.1f} ГиБ, "
            f"требуется не менее {(required_bytes + reserve_bytes) / gib:.1f} ГиБ "
            f"(включая резерв {reserve_bytes / gib:.1f} ГиБ). Освободите место и повторите."
        )


def is_disk_full_error(error: OSError) -> bool:
    """Recognize POSIX and Windows disk/quota exhaustion without masking other I/O errors."""
    disk_full_errnos = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
    windows_disk_full_codes = {39, 112, 1816}
    return error.errno in disk_full_errnos or getattr(error, "winerror", None) in windows_disk_full_codes


def disk_full_storage_error(error: OSError) -> StorageSpaceError | None:
    if not is_disk_full_error(error):
        return None
    return StorageSpaceError(
        "Диск заполнен или исчерпана квота. Запись остановлена; освободите место и повторите."
    )


def _raw_capture_bytes(devices: Iterable[dict], seconds: float) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Некорректная длительность записи")
    total = 0
    for device in devices:
        try:
            rate = int(device["defaultSampleRate"])
            channels = int(device["maxInputChannels"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("Не удалось оценить размер выбранного аудиоустройства") from None
        if rate < 1 or channels < 1:
            raise ValueError("Аудиоустройство имеет неподдерживаемый формат")
        total += math.ceil(rate * channels * 2 * seconds)
    return total


def estimate_capture_workspace_bytes(devices: Iterable[dict], max_seconds: float) -> int:
    """Reserve raw PCM plus three working copies for derivatives and playback."""
    return _raw_capture_bytes(devices, max_seconds) * CAPTURE_WORKING_SET_COPIES


def remaining_capture_workspace_bytes(devices: Iterable[dict], max_seconds: float,
                                      elapsed_seconds: float) -> int:
    """Keep room for remaining raw PCM and three full-size downstream copies."""
    total_raw = _raw_capture_bytes(devices, max_seconds)
    elapsed = min(max_seconds, max(0.0, elapsed_seconds))
    remaining_raw = _raw_capture_bytes(devices, max_seconds - elapsed)
    return remaining_raw + total_raw * (CAPTURE_WORKING_SET_COPIES - 1)


def ensure_free_space(path: Path, required_bytes: int = 0,
                      reserve_bytes: int = MINIMUM_FREE_SPACE_BYTES) -> int:
    if (isinstance(required_bytes, bool) or not isinstance(required_bytes, int) or required_bytes < 0
            or isinstance(reserve_bytes, bool) or not isinstance(reserve_bytes, int) or reserve_bytes < 0):
        raise ValueError("Некорректный размер резерва диска")
    probe = Path(path).resolve()
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            raise StorageSpaceError("Не удалось проверить свободное место на диске.") from None
        probe = parent
    try:
        available = int(shutil.disk_usage(probe).free)
    except (OSError, ValueError):
        raise StorageSpaceError("Не удалось проверить свободное место на диске.") from None
    if available < required_bytes + reserve_bytes:
        raise InsufficientStorageError(available, required_bytes, reserve_bytes)
    return available
