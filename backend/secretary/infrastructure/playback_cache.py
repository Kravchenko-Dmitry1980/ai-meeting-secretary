"""Bounded immutable playback snapshots with reader-aware eviction."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from threading import RLock
import time
from typing import Callable

from secretary.infrastructure.storage_space import InsufficientStorageError, StorageSpaceError, ensure_free_space

MAX_PLAYBACK_CACHE_BYTES = 2 * 1024**3


class PlaybackCacheCapacityError(StorageSpaceError):
    """The configured cache cap cannot be met without deleting an active reader."""

    def __init__(self):
        super().__init__(
            "Кэш воспроизведения заполнен активными записями. Закройте плееры и повторите запрос."
        )


class PlaybackCache:
    def __init__(self, data_dir: Path, max_bytes: int = MAX_PLAYBACK_CACHE_BYTES):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("Некорректный предел кэша воспроизведения")
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "audio"
        self.max_bytes = max_bytes
        self._lock = RLock()
        self._readers: Counter[Path] = Counter()
        self._last_used: dict[Path, int] = {}

    def _entries(self) -> list[tuple[Path, int, int]]:
        if not self.root.is_dir():
            return []
        entries = []
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            for path in directory.glob("playback-*.wav"):
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                key = path.resolve()
                entries.append((path, stat.st_size, self._last_used.get(key, stat.st_mtime_ns)))
        return entries

    def _remove_oldest(self) -> bool:
        for path, _, _ in sorted(self._entries(), key=lambda item: item[2]):
            if self._readers[path.resolve()] > 0:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                self._last_used.pop(path.resolve(), None)
                return True
            except OSError:
                continue
            self._last_used.pop(path.resolve(), None)
            return True
        return False

    def _make_room(self, incoming_bytes: int) -> None:
        if incoming_bytes > self.max_bytes:
            raise PlaybackCacheCapacityError()
        while sum(size for _, size, _ in self._entries()) + incoming_bytes > self.max_bytes:
            if not self._remove_oldest():
                raise PlaybackCacheCapacityError()

    def _make_disk_room(self, required_bytes: int) -> None:
        while True:
            try:
                ensure_free_space(self.data_dir, required_bytes=required_bytes)
                return
            except InsufficientStorageError:
                if not self._remove_oldest():
                    raise

    def get_or_create(self, path: Path, expected_bytes: int,
                      builder: Callable[[], None]) -> "PlaybackLease":
        path = Path(path)
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError:
            raise ValueError("Путь кэша находится вне каталога аудио") from None
        if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 1:
            raise ValueError("Некорректный ожидаемый размер кэша")
        with self._lock:
            if path.is_file():
                actual = path.stat().st_size
                key = path.resolve()
                if actual == expected_bytes and actual <= self.max_bytes:
                    self._readers[key] += 1
                    self._last_used[key] = time.time_ns()
                    try:
                        self._make_room(0)
                    except BaseException:
                        if self._readers[key] <= 1:
                            self._readers.pop(key, None)
                        else:
                            self._readers[key] -= 1
                        raise
                    return PlaybackLease(self, path)
                if self._readers[key] > 0:
                    raise StorageSpaceError("Активный файл воспроизведения имеет неожиданный размер.")
                path.unlink(missing_ok=True)
                self._last_used.pop(key, None)
            self._make_room(expected_bytes)
            self._make_disk_room(expected_bytes)
            path.parent.mkdir(parents=True, exist_ok=True)
            builder()
            if not path.is_file():
                raise StorageSpaceError("Не удалось создать файл воспроизведения.")
            actual = path.stat().st_size
            if actual > self.max_bytes:
                path.unlink(missing_ok=True)
                raise PlaybackCacheCapacityError()
            if actual != expected_bytes:
                path.unlink(missing_ok=True)
                raise StorageSpaceError("Размер подготовленного аудиокэша не совпал с расчётом.")
            key = path.resolve()
            self._readers[key] += 1
            self._last_used[key] = time.time_ns()
            return PlaybackLease(self, path)

    def _release(self, path: Path) -> None:
        key = path.resolve()
        with self._lock:
            if self._readers[key] <= 1:
                self._readers.pop(key, None)
            else:
                self._readers[key] -= 1


class PlaybackLease:
    def __init__(self, cache: PlaybackCache, path: Path):
        self._cache = cache
        self.path = Path(path)
        self._released = False
        self._lock = RLock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._cache._release(self.path)
