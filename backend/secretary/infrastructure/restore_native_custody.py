"""Internal Windows file custody. Diagnostic facts never authorize activation.

The shared phase permits writing and excludes deletion only. The coordinator
must independently stop/reap its exact child before returning to NoWrite. This
module owns no child and makes no writable-mapping exclusion claim.

Receipt.closed qualifies this component's file HANDLEs only. Parent custody
uses protected_scope; its dependency does not qualify directory CloseHandle
outcomes. That dependency and process cleanup remain coordinator obligations.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import threading
import time

from .restore_operator import _local, _win32_path, protected_scope


MAX_BYTES = 256 * 1024 * 1024
MAX_SECONDS = 30.0
CHUNK_BYTES = 64 * 1024
_clock = time.monotonic
_TOKEN = object()


class NativeCustodyError(ValueError):
    def __init__(self, code='native_custody_invalid'):
        self.code = code if type(code) is str and code in {'native_custody_invalid', 'native_custody_state',
            'native_custody_failed', 'native_custody_platform'} else 'native_custody_invalid'
        super().__init__(self.code)


def _fail(code='native_custody_invalid'):
    raise NativeCustodyError(code) from None


@dataclass(frozen=True)
class _Metadata:
    identity: tuple[int, int]
    attributes: int
    links: int
    length: int


@dataclass(frozen=True)
class _FileFacts(_Metadata):
    sha256: str


@dataclass(frozen=True)
class _DiagnosticReceipt(_FileFacts):
    closed: bool = field(default=True, init=False)
    diagnostic_only: bool = field(default=True, init=False)
    activation_supported: bool = field(default=False, init=False)
    outbound_enabled: bool = field(default=False, init=False)


class _Win32FileAPI:
    """Lazy native adapter; each read and identity check uses the owned HANDLE."""
    def __init__(self):
        if os.name != 'nt':
            _fail('native_custody_platform')
        from ctypes import wintypes as w
        class Information(ctypes.Structure):
            _fields_ = [('attributes', w.DWORD), ('creation', w.FILETIME),
                ('access', w.FILETIME), ('write', w.FILETIME), ('volume', w.DWORD),
                ('size_high', w.DWORD), ('size_low', w.DWORD), ('links', w.DWORD),
                ('index_high', w.DWORD), ('index_low', w.DWORD)]
        self._information = Information
        self._w = w
        self._kernel = kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                      w.DWORD, w.DWORD, w.HANDLE]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle.restype = w.BOOL
        kernel.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(Information)]
        kernel.GetFileInformationByHandle.restype = w.BOOL
        kernel.GetHandleInformation.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        kernel.GetHandleInformation.restype = w.BOOL
        kernel.SetFilePointerEx.argtypes = [w.HANDLE, ctypes.c_longlong,
                                          ctypes.POINTER(ctypes.c_longlong), w.DWORD]
        kernel.SetFilePointerEx.restype = w.BOOL
        kernel.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD,
                                   ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        kernel.ReadFile.restype = w.BOOL

    def open(self, path, *, deny_write):
        # No SECURITY_ATTRIBUTES => non-inheritable. OPEN_EXISTING only.
        handle = self._kernel.CreateFileW(_win32_path(path), 0x80000000,
            1 if deny_write else 3, None, 3, 0x00200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            _fail()
        return handle

    def close(self, handle):
        if not self._kernel.CloseHandle(handle):
            _fail()

    def metadata(self, handle):
        info = self._information()
        if not self._kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            _fail()
        return _Metadata((info.volume, (info.index_high << 32) | info.index_low),
            info.attributes, info.links, (info.size_high << 32) | info.size_low)

    def inheritance(self, handle):
        flags = self._w.DWORD()
        if not self._kernel.GetHandleInformation(handle, ctypes.byref(flags)):
            _fail()
        return bool(flags.value & 1)

    def rewind(self, handle):
        if not self._kernel.SetFilePointerEx(handle, 0, None, 0):
            _fail()

    def read(self, handle, size):
        if type(size) is not int or not 0 < size <= CHUNK_BYTES:
            _fail()
        buffer = ctypes.create_string_buffer(size)
        count = self._w.DWORD()
        if not self._kernel.ReadFile(handle, buffer, size, ctypes.byref(count), None):
            _fail()
        if count.value > size:
            _fail()
        return buffer.raw[:count.value]


def _valid_metadata(info):
    if (type(info) is not _Metadata or type(info.identity) is not tuple
            or len(info.identity) != 2 or any(type(n) is not int or n < 0 for n in info.identity)
            or type(info.attributes) is not int or info.attributes & (0x10 | 0x40 | 0x400)
            or info.links != 1 or type(info.length) is not int
            or not 0 <= info.length <= MAX_BYTES):
        _fail()


class _NativeFileCustody:
    def __init__(self, token, path):
        if token is not _TOKEN:
            _fail('native_custody_state')
        self._path = path
        self._api = _Win32FileAPI()
        self._owner = (os.getpid(), threading.get_ident())
        self._handles = []
        self._current = None
        self._state = 'initial_nowrite'
        self._baseline = None
        self._receipt = None
        self._failed = False

    @property
    def state(self):
        return self._state

    @property
    def baseline(self):
        return self._baseline

    @property
    def receipt(self):
        if self._receipt is None:
            _fail('native_custody_state')
        return self._receipt

    def _check_state(self, *states):
        if (self._owner != (os.getpid(), threading.get_ident())
                or self._failed or self._state not in states):
            if self._state != 'closed':
                self._failed = True
                self._state = 'failed'
            _fail('native_custody_state')

    def _guard(self, operation):
        try:
            return operation()
        except Exception:
            self._failed = True
            self._state = 'failed'
            _fail('native_custody_failed')

    def _open(self, *, deny_write):
        handle = self._api.open(self._path, deny_write=deny_write)
        self._handles.append(handle)
        if self._api.inheritance(handle) is not False:
            _fail()
        return handle

    def _close(self, handle):
        # Remove before native close: never retry an ambiguous CloseHandle on a
        # recycled numeric value. Failure is permanent even if release occurred.
        self._handles.remove(handle)
        self._api.close(handle)

    def _sidecars(self):
        for suffix in ('-wal', '-shm', '-journal'):
            candidate = Path(_win32_path(Path(str(self._path) + suffix)))
            try:
                candidate.lstat()
            except FileNotFoundError:
                continue
            _fail()

    def _metadata(self, handle):
        if self._api.inheritance(handle) is not False:
            _fail()
        facts = self._api.metadata(handle)
        _valid_metadata(facts)
        return facts

    def _snapshot(self, handle):
        self._sidecars()  # Never read native bytes in the presence of a journal.
        before = self._metadata(handle)
        if self._baseline is not None and before.identity != self._baseline.identity:
            _fail()
        deadline = _clock() + MAX_SECONDS
        self._api.rewind(handle)
        total, digest = 0, hashlib.sha256()
        while True:
            if _clock() >= deadline:
                _fail()
            chunk = self._api.read(handle, CHUNK_BYTES)
            if type(chunk) is not bytes or len(chunk) > CHUNK_BYTES:
                _fail()
            total += len(chunk)
            if total > MAX_BYTES or total > before.length or _clock() >= deadline:
                _fail()
            digest.update(chunk)
            if not chunk:
                break
        after = self._metadata(handle)
        self._sidecars()
        if before != after or total != before.length or _clock() >= deadline:
            _fail()
        return _FileFacts(before.identity, before.attributes, before.links,
                          total, digest.hexdigest())

    def _start(self):
        def start():
            self._current = self._open(deny_write=True)
            self._baseline = self._snapshot(self._current)
        self._guard(start)

    def enter_nodelete_phase(self):
        self._check_state('initial_nowrite')
        def enter():
            prior = self._current
            new = self._open(deny_write=False)
            facts = self._metadata(new)
            if facts.identity != self._baseline.identity:
                _fail()
            self._close(prior)
            self._current = new
            self._state = 'shared_nodelete'
        self._guard(enter)

    def restore_nowrite_and_verify(self):
        self._check_state('shared_nodelete')
        def restore():
            prior = self._current
            new = self._open(deny_write=True)
            facts = self._snapshot(new)
            if facts != self._baseline:
                _fail()
            self._close(prior)
            self._current = new
            self._state = 'returned_nowrite'
            return facts
        return self._guard(restore)

    def _finish(self):
        self._check_state('initial_nowrite', 'returned_nowrite')
        def verify():
            if self._snapshot(self._current) != self._baseline:
                _fail()
        self._guard(verify)

    def _cleanup(self):
        failed = False
        for handle in tuple(self._handles):
            try:
                self._close(handle)
            except Exception:
                failed = True
        self._current = None
        if failed:
            self._failed = True
            self._state = 'failed'
            _fail('native_custody_failed')


@contextmanager
def _native_file_custody(path):
    """Keep context alive until exact child stop; exit itself proves no stop."""
    session, body_error, final_error = None, None, None
    try:
        local = _local(path)
        _win32_path(local)  # Ordinary absolute drive-local input only.
        with protected_scope(local.parent):
            try:
                session = _NativeFileCustody(_TOKEN, local)
                session._start()
                try:
                    yield session
                except BaseException as error:
                    body_error = error
                try:
                    session._finish()
                except Exception:
                    final_error = NativeCustodyError('native_custody_failed')
            finally:
                if session is not None:
                    try:
                        session._cleanup()
                    except Exception:
                        final_error = NativeCustodyError('native_custody_failed')
        # Scope exit must also succeed before publishing a closed receipt.
        if final_error is not None:
            raise final_error from None
        if body_error is not None:
            raise body_error
        session._state = 'closed'
        facts = session.baseline
        session._receipt = _DiagnosticReceipt(facts.identity, facts.attributes,
            facts.links, facts.length, facts.sha256)
    except BaseException as error:
        if session is not None and session._state != 'closed':
            session._failed = True
            session._state = 'failed'
        if error is body_error:
            raise
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, NativeCustodyError):
            raise
        _fail('native_custody_failed')
