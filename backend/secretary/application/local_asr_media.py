"""Verify an imported analysis against application-owned input, without decoding.

No caller-supplied filesystem path is accepted. Multi-chunk recordings require a
separately qualified canonical input; a hash of chunk metadata is not audio SHA256.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from contextlib import contextmanager
from uuid import UUID


class LocalAsrMediaError(ValueError):
    pass


def verified_meeting_input(db, data_dir: Path, meeting_id: str, *, max_bytes: int,
                           _copy_to: Path | None = None) -> str:
    """Hash the original upload or a single, complete owned recording chunk.

    The source is checked before and after streaming. An actively recorded or
    modified file, out-of-root DB path, ambiguous multichannel input, and missing
    media all fail closed. Errors deliberately contain no private paths.
    """
    try:
        if str(UUID(meeting_id)) != meeting_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise LocalAsrMediaError("local_asr_invalid_meeting") from None
    meeting = db.one("SELECT * FROM meetings WHERE id=?", (meeting_id,))
    if meeting is None:
        raise LocalAsrMediaError("local_asr_meeting_not_found")
    if meeting["recording"]:
        raise LocalAsrMediaError("local_asr_recording_in_progress")
    root = Path(data_dir).absolute()
    expected_chunk_hash = None
    if meeting["media_path"]:
        candidate = Path(meeting["media_path"])
        allowed = root / "uploads" / meeting_id
    else:
        chunks = db.chunks(meeting_id)
        if len(chunks) != 1 or chunks[0]["offset_ms"] != 0:
            raise LocalAsrMediaError("local_asr_canonical_input_required")
        candidate = Path(chunks[0]["path"])
        expected_chunk_hash = chunks[0]["sha256"]
        allowed = root / "audio" / meeting_id
    try:
        if not candidate.is_absolute() or ".." in candidate.parts or not candidate.is_relative_to(allowed):
            raise LocalAsrMediaError("local_asr_input_outside_meeting")
        def checked_stat():
            for part in (candidate, *candidate.parents):
                info = part.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise LocalAsrMediaError("local_asr_input_outside_meeting")
            info = candidate.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise LocalAsrMediaError("local_asr_input_unavailable")
            return info
        before = checked_stat()
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise LocalAsrMediaError("local_asr_input_size_invalid")
        digest = hashlib.sha256()
        count = 0
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        from contextlib import nullcontext
        # The same checked descriptor supplies the digest and the owned copy.
        # Opening the DB path a second time would introduce a swap race.
        with os.fdopen(descriptor, "rb") as source, (
                _copy_to.open("xb") if _copy_to is not None else nullcontext()) as copied:
            opened = os.fstat(source.fileno())
            if identity(opened) != identity(before):
                raise LocalAsrMediaError("local_asr_input_changed")
            while block := source.read(1024 * 1024):
                count += len(block)
                if count > max_bytes:
                    raise LocalAsrMediaError("local_asr_input_size_invalid")
                digest.update(block)
                if copied is not None:
                    copied.write(block)
            finished = os.fstat(source.fileno())
            if identity(finished) != identity(opened) or finished.st_ctime_ns != opened.st_ctime_ns:
                raise LocalAsrMediaError("local_asr_input_changed")
        after = checked_stat()
        if identity(before) != identity(after) or count != before.st_size:
            raise LocalAsrMediaError("local_asr_input_changed")
        result = digest.hexdigest()
        if expected_chunk_hash is not None and result != expected_chunk_hash:
            raise LocalAsrMediaError("local_asr_input_hash_mismatch")
        return result
    except (OSError, RuntimeError):
        raise LocalAsrMediaError("local_asr_input_unavailable") from None


def copy_verified_meeting_input(db, data_dir: Path, meeting_id: str, destination: Path,
                                *, max_bytes: int, expected_sha256: str) -> Path:
    """Copy into a fresh execution-owned file while verifying the source handle.

    A failed copy is retained in its isolated attempt and never executed. The
    caller must allocate the parent beneath the private local-ASR work root.
    """
    destination = Path(destination).absolute()
    work_root = Path(data_dir).absolute() / "local-asr-work"
    if not destination.is_relative_to(work_root) or ".." in destination.parts:
        raise LocalAsrMediaError("local_asr_copy_destination_invalid")
    try:
        if destination.exists():
            raise LocalAsrMediaError("local_asr_copy_destination_exists")
        for parent in destination.parents:
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise LocalAsrMediaError("local_asr_copy_destination_invalid")
        with _lock_copy_directories(work_root, destination.parent):
            result = verified_meeting_input(db, data_dir, meeting_id, max_bytes=max_bytes,
                                            _copy_to=destination)
        if result != expected_sha256:
            raise LocalAsrMediaError("local_asr_input_hash_mismatch")
        return destination
    except (OSError, RuntimeError):
        raise LocalAsrMediaError("local_asr_copy_destination_invalid") from None


@contextmanager
def _lock_copy_directories(root: Path, parent: Path):
    """Bind the owned destination chain while creating/copying a new file."""
    if os.name != "nt":
        raise LocalAsrMediaError("local_asr_copy_lock_unavailable")
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x00000800)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handles = []
    try:
        chain = [root]
        for part in parent.relative_to(root).parts:
            chain.append(chain[-1] / part)
        for directory in chain:
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise LocalAsrMediaError("local_asr_copy_destination_invalid")
            # FILE_READ_ATTRIBUTES, share READ only, directory semantics.
            # No write/delete sharing: a directory cannot become a junction
            # or be renamed between validation and destination creation.
            handle = kernel.CreateFileW(str(directory), 0x80, 1, None, 3, 0x02000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise LocalAsrMediaError("local_asr_copy_lock_unavailable")
            handles.append(handle)
            buffer = ctypes.create_unicode_buffer(32768)
            size = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not 0 < size < len(buffer):
                raise LocalAsrMediaError("local_asr_copy_lock_unavailable")
            resolved = buffer.value.removeprefix('\\\\?\\')
            if os.path.normcase(resolved) != os.path.normcase(str(directory)):
                raise LocalAsrMediaError("local_asr_copy_destination_invalid")
        yield
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


@contextmanager
def lock_verified_input_copy(path: Path, *, expected_sha256: str, max_bytes: int):
    """Keep the exact Windows input readable but deny writes/deletes until exit.

    Holding this handle covers normalization and all later stages. A post-run
    hash alone would miss a concurrent change-and-restore of the attempt copy.
    """
    if os.name != "nt":
        raise LocalAsrMediaError("local_asr_input_lock_unavailable")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    path = Path(path).absolute()
    handle = None
    descriptor = None
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x00000800)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    try:
        for parent in (path, *path.parents):
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise LocalAsrMediaError("local_asr_input_lock_invalid")
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= max_bytes:
            raise LocalAsrMediaError("local_asr_input_lock_invalid")
        # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, OPEN_REPARSE_POINT.
        handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise LocalAsrMediaError("local_asr_input_lock_unavailable")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None  # CRT now owns the native handle.
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            opened = os.fstat(stream.fileno())
            identity = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_nlink)
            if identity(opened) != identity(before):
                raise LocalAsrMediaError("local_asr_input_changed")
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256:
                raise LocalAsrMediaError("local_asr_input_hash_mismatch")
            yield path
            if identity(os.fstat(stream.fileno())) != identity(opened):
                raise LocalAsrMediaError("local_asr_input_changed")
    except OSError:
        raise LocalAsrMediaError("local_asr_input_lock_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if handle is not None:
            kernel.CloseHandle(handle)
