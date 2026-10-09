"""Exclusive process lease for one Secretary data directory."""
from __future__ import annotations

import os
from pathlib import Path
import threading


class DataDirectoryLockError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_registry_guard = threading.Lock()
_held_directories: set[str] = set()


class DataDirectoryLock:
    """Hold an OS-backed, nonblocking lease until the owning app shuts down."""

    def __init__(self, data_dir: Path | str):
        try:
            self.data_dir = Path(data_dir).expanduser().resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            raise DataDirectoryLockError("data_directory_lock_unavailable") from None
        self.path = self.data_dir / ".secretary-instance.lock"
        self._identity = os.path.normcase(str(self.data_dir))
        self._stream = None
        self._locked = False
        self._released = False

        with _registry_guard:
            if self._identity in _held_directories:
                raise DataDirectoryLockError("data_directory_in_use")
            _held_directories.add(self._identity)

        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._stream = self.path.open("a+b")
        except OSError:
            self.release()
            raise DataDirectoryLockError("data_directory_lock_unavailable") from None

        try:
            self._stream.seek(0, os.SEEK_END)
            if self._stream.tell() == 0:
                self._stream.write(b"\0")
                self._stream.flush()
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
        except OSError:
            self.release()
            raise DataDirectoryLockError("data_directory_in_use") from None
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        stream, self._stream = self._stream, None
        try:
            if stream is not None:
                try:
                    if self._locked:
                        stream.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    # Closing the handle still releases the OS lease.
                    pass
                try:
                    stream.close()
                except OSError:
                    pass
        finally:
            with _registry_guard:
                _held_directories.discard(self._identity)

    def __enter__(self) -> "DataDirectoryLock":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass
