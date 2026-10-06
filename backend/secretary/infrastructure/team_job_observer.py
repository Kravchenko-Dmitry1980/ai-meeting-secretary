"""Read-only diagnostic of CURRENT retained Windows Job-member lifetimes.

This observer does not install a Job, enumerate global processes, terminate a
member, inspect argv/env, expand an allowlist or issue authority. Image facts are
the current local path-file bytes, not the process's mapped image. Intermediate
directories remain pinned/non-reparse. Stable administrator-managed DOS/volume
mappings are assumed; hostile OS remapping/network containment is not proved.
The five-second budget is checked around synchronous calls, not OS cancellation.
Imports create no handles and open no files.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
import hashlib
import os
import re
import time

from . import team_process_job as jobs

MAX_SECONDS = 5.0
MAX_MEMBERS = 64
MAX_LIFETIMES = 8
MAX_RECORDS = 32
MAX_IMAGE_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
PROCESS_RIGHTS = 0x00101000  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE only
FILE_SHARE = 1  # READ only, never WRITE/DELETE
_KNOWN_NAMES = frozenset(('python.exe', 'python3.exe', 'pythonw.exe',
    'vikunja-v2.7.0-windows-4.0-amd64.exe', 'wmic.exe', 'powershell.exe',
    'cmd.exe', 'conhost.exe', 'reg.exe', 'systeminfo.exe'))
_CODES = frozenset(('observer_job_invalid', 'observer_platform_unsupported',
    'observer_deadline', 'observer_closed', 'observer_reentered',
    'observer_syscall_failed', 'observer_list_truncated', 'observer_snapshot_invalid',
    'observer_discovery_raced', 'observer_member_bound', 'observer_record_bound',
    'observer_member_unavailable', 'observer_member_identity_changed',
    'observer_member_stale', 'observer_member_missing', 'observer_pid_reused_or_stale',
    'observer_missed_lifetime', 'observer_image_path_invalid', 'observer_image_nonlocal',
    'observer_image_reparse', 'observer_image_alias', 'observer_image_size_bound',
    'observer_image_read_failed', 'observer_image_drift', 'observer_no_capture',
    'observer_handle_cleanup_failed', 'observer_body_interrupted'))


class JobObserverError(ValueError):
    def __init__(self, code, *, winerror=None):
        self.code = code if type(code) is str and code in _CODES else 'observer_syscall_failed'
        self.winerror = winerror if type(winerror) is int and 0 <= winerror < 2**32 else None
        super().__init__(self.code)


def _fail(code, *, winerror=None):
    raise JobObserverError(code, winerror=winerror) from None


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _local_parts(path):
    # Kernel-reported path is still checked before any path-based file open.
    if (type(path) is not str or not 4 <= len(path) <= 32768
            or not re.match(r'^[A-Za-z]:\\', path) or '/' in path
            or any(ord(char) < 32 for char in path) or any(char in path for char in '*?"<>|')):
        _fail('observer_image_path_invalid')
    pieces = path[3:].split('\\')
    if (not 1 <= len(pieces) <= 128 or any(not part or part in ('.', '..')
            or ':' in part or part.rstrip(' .') != part
            or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)
            for part in pieces)):
        _fail('observer_image_path_invalid')
    return path[:3], tuple(pieces)


@dataclass(frozen=True)
class ImageFileFacts:
    volume_serial: int
    file_id: str
    size: int
    sha256: str
    path_sha256: str
    name_sha256: str
    safe_basename: str | None
    current_path_file_only: bool = True
    mapped_image_qualified: bool = False


@dataclass(frozen=True)
class MemberObservation:
    pid: int
    creation_time: str
    alive: bool
    exit_code: int
    image: ImageFileFacts
    ancestry: str = 'UNKNOWN'
    authorization: str = 'NOT_EVALUATED'


@dataclass(frozen=True)
class DiagnosticRecord:
    kind: str
    pids: tuple[int, ...] = ()
    assigned: int | None = None
    listed: int | None = None
    total_processes: int | None = None
    active_processes: int | None = None
    terminated_processes: int | None = None
    pid: int | None = None
    creation_time: str | None = None
    image_path_sha256: str | None = None
    image_name_sha256: str | None = None
    safe_basename: str | None = None
    alive: bool | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class ObserverReceipt:
    result: str
    evidence_kind: str
    root_pid: int
    records: tuple[DiagnosticRecord, ...]
    members: tuple[MemberObservation, ...]
    failures: tuple[str, ...]
    cleanup_complete: bool
    elapsed_seconds: float
    diagnostic_only: bool = True
    activation_supported: bool = False
    allowlist_expansion: bool = False
    mapped_image_qualified: bool = False
    ancestry: str = 'UNKNOWN'
    pre_observation_lifetime_history: str = 'UNKNOWN'
    hard_os_io_cancellation: bool = False


@dataclass(frozen=True)
class _Snapshot:
    pids: tuple[int, ...]
    assigned: int
    listed: int
    total: int
    active: int
    terminated: int


@dataclass(frozen=True)
class _MemberState:
    pid: int
    creation: str
    alive: bool
    code: int
    member: bool
    image_path: str | None


@dataclass
class _Retained:
    handle: int
    state: _MemberState
    image_lease: object
    image: ImageFileFacts


class _FileInfo(ctypes.Structure):
    _fields_ = [('attributes', w.DWORD), ('creation', w.FILETIME), ('access', w.FILETIME),
        ('write', w.FILETIME), ('volume', w.DWORD), ('size_high', w.DWORD), ('size_low', w.DWORD),
        ('links', w.DWORD), ('index_high', w.DWORD), ('index_low', w.DWORD)]


class _FileId(ctypes.Structure):
    _fields_ = [('volume', ctypes.c_ulonglong), ('identifier', ctypes.c_ubyte * 16)]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ('user', 'kernel', 'period_user', 'period_kernel')]
    _fields_ += [(name, w.DWORD) for name in ('faults', 'total', 'active', 'terminated')]


class _Limits(ctypes.Structure):
    _fields_ = [('user', ctypes.c_longlong), ('job_user', ctypes.c_longlong), ('flags', w.DWORD),
        ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active_limit', w.DWORD),
        ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]


class _Counters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ('read', 'write', 'other', 'read_bytes', 'write_bytes', 'other_bytes')]


class _Extended(ctypes.Structure):
    _fields_ = [('basic', _Limits), ('io', _Counters), ('process_memory', ctypes.c_size_t),
        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


def _actual_job(job):
    if type(job) is not jobs.WindowsJob or job is not jobs._owned_job:
        _fail('observer_job_invalid')
    try:
        # Do not use a caller-overridden instance verifier or job.kernel.
        return jobs.WindowsJob.verify_owned_identity(job)
    except (ValueError, OSError, TypeError, AttributeError):
        _fail('observer_job_invalid')


class _Win32Ports:
    def __init__(self, job, check):
        if os.name != 'nt':
            _fail('observer_platform_unsupported')
        self.job, self.check = job, check
        self.borrowed_job_handle = job.handle
        self.handles = []
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'GetProcessId': ([w.HANDLE], w.DWORD),
            'GetProcessTimes': ([w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
            'GetExitCodeProcess': ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
            'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
            'IsProcessInJob': ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
            'QueryFullProcessImageNameW': ([w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
            'QueryInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
            'CreateFileW': ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            'GetDriveTypeW': ([w.LPCWSTR], w.UINT),
            'GetFileType': ([w.HANDLE], w.DWORD),
            'GetFileInformationByHandle': ([w.HANDLE, ctypes.POINTER(_FileInfo)], w.BOOL),
            'GetFileInformationByHandleEx': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            'GetFileSizeEx': ([w.HANDLE, ctypes.POINTER(ctypes.c_longlong)], w.BOOL),
            'GetFinalPathNameByHandleW': ([w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD], w.DWORD),
            'SetFilePointerEx': ([w.HANDLE, ctypes.c_longlong, ctypes.c_void_p, w.DWORD], w.BOOL),
            'ReadFile': ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    def call(self, name, *args):
        self.check()
        try:
            result = getattr(self.kernel, name)(*args)
        except (OSError, ValueError, TypeError, ctypes.ArgumentError):
            _fail('observer_syscall_failed')
        self.check()
        return result

    def required(self, name, *args, code='observer_syscall_failed'):
        if not self.call(name, *args):
            _fail(code, winerror=ctypes.get_last_error())

    def verify_job(self):
        self.check()
        proof = _actual_job(self.job)
        if self.job.handle != self.borrowed_job_handle:
            _fail('observer_job_invalid')
        limits, returned = _Extended(), w.DWORD()
        self.required('QueryInformationJobObject', self.borrowed_job_handle, 9,
            ctypes.byref(limits), ctypes.sizeof(limits), ctypes.byref(returned), code='observer_job_invalid')
        if returned.value != ctypes.sizeof(limits) or limits.basic.flags != 0x2000:
            _fail('observer_job_invalid')
        self.check()
        return proof.pid

    def accounting(self):
        value, returned = _Accounting(), w.DWORD()
        self.required('QueryInformationJobObject', self.borrowed_job_handle, 1,
            ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned))
        if returned.value != ctypes.sizeof(value):
            _fail('observer_snapshot_invalid')
        return value.total, value.active, value.terminated

    def job_snapshot(self):
        class List(ctypes.Structure):
            _fields_ = [('assigned', w.DWORD), ('listed', w.DWORD), ('pids', ctypes.c_size_t * MAX_MEMBERS)]
        before = self.accounting()
        value, returned = List(), w.DWORD()
        if not self.call('QueryInformationJobObject', self.borrowed_job_handle, 3,
                ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned)):
            error = ctypes.get_last_error()
            _fail('observer_list_truncated' if error == 234 else 'observer_syscall_failed', winerror=error)
        minimum = List.pids.offset + value.listed * ctypes.sizeof(ctypes.c_size_t)
        if (value.assigned != value.listed or value.listed > MAX_MEMBERS
                or not minimum <= returned.value <= ctypes.sizeof(value)):
            _fail('observer_list_truncated')
        after = self.accounting()
        if before != after:
            _fail('observer_discovery_raced')
        return _Snapshot(tuple(value.pids[:value.listed]), value.assigned, value.listed, *after)

    def track(self, handle, kind):
        if handle in (None, 0, ctypes.c_void_p(-1).value):
            _fail('observer_member_unavailable' if kind == 'process' else 'observer_syscall_failed',
                winerror=ctypes.get_last_error())
        self.handles.append((handle, kind))
        return handle

    def acquire(self, name, kind, *args):
        self.check()
        try:
            handle = getattr(self.kernel, name)(*args)
        except (OSError, ValueError, TypeError, ctypes.ArgumentError):
            _fail('observer_syscall_failed')
        # A successful acquisition belongs to cleanup BEFORE the post-call
        # deadline check can raise. Never orphan an actual returned HANDLE.
        self.track(handle, kind)
        self.check()
        return handle

    def open_member(self, pid):
        # Only an exact fresh Job PID reaches this method from the collector.
        return self.acquire('OpenProcess', 'process', PROCESS_RIGHTS, False, pid)

    def member_state(self, handle):
        pid = self.call('GetProcessId', handle)
        if not pid:
            _fail('observer_member_unavailable', winerror=ctypes.get_last_error())
        times = [w.FILETIME() for _ in range(4)]
        self.required('GetProcessTimes', handle, *(ctypes.byref(value) for value in times))
        creation = str(times[0].dwHighDateTime * 2**32 + times[0].dwLowDateTime)
        wait = self.call('WaitForSingleObject', handle, 0)
        if wait not in (0, 258):
            _fail('observer_member_unavailable', winerror=ctypes.get_last_error())
        code, member = w.DWORD(), w.BOOL()
        self.required('GetExitCodeProcess', handle, ctypes.byref(code))
        self.required('IsProcessInJob', handle, self.borrowed_job_handle, ctypes.byref(member))
        path = None
        if wait == 258:
            size = w.DWORD(32768); buffer = ctypes.create_unicode_buffer(size.value)
            self.required('QueryFullProcessImageNameW', handle, 0, buffer, ctypes.byref(size))
            if not 0 < size.value < 32768:
                _fail('observer_image_path_invalid')
            path = buffer.value
        return _MemberState(pid, creation, wait == 258, code.value, bool(member.value), path)

    def file_info(self, handle):
        value = _FileInfo()
        self.required('GetFileInformationByHandle', handle, ctypes.byref(value))
        identity = _FileId()
        self.required('GetFileInformationByHandleEx', handle, 18, ctypes.byref(identity), ctypes.sizeof(identity))
        size = ctypes.c_longlong(value.size_high * 2**32 + value.size_low)
        if not value.attributes & 0x10:
            self.required('GetFileSizeEx', handle, ctypes.byref(size))
        return (identity.volume, bytes(identity.identifier).hex(), size.value,
            value.attributes, value.creation.dwHighDateTime * 2**32 + value.creation.dwLowDateTime,
            value.write.dwHighDateTime * 2**32 + value.write.dwLowDateTime)

    def final_path(self, handle):
        buffer = ctypes.create_unicode_buffer(32768)
        count = self.call('GetFinalPathNameByHandleW', handle, buffer, len(buffer), 0)
        if not 0 < count < len(buffer):
            _fail('observer_image_alias')
        value = buffer.value
        if not value.startswith('\\\\?\\'):
            _fail('observer_image_alias')
        value = value[4:]
        _local_parts(value) if len(value) > 3 else None
        return value

    def open_image(self, path):
        root, parts = _local_parts(path)
        if self.call('GetDriveTypeW', root) != 3:  # DRIVE_FIXED only
            _fail('observer_image_nonlocal')
        # Every directory is inspected by its same retained HANDLE before any
        # descendant open; share READ only prevents rename/reparse replacement.
        directories, prefix, volume = [], root, None
        for component in (None, *parts[:-1]):
            if component is not None:
                prefix = prefix.rstrip('\\') + '\\' + component
            handle = self.acquire('CreateFileW', 'directory', prefix, 0x80, FILE_SHARE,
                None, 3, 0x02200000, None)
            info = self.file_info(handle)
            if info[3] & 0x400 or not info[3] & 0x10:
                _fail('observer_image_reparse')
            canonical = self.final_path(handle)
            if canonical.lower().rstrip('\\') != prefix.lower().rstrip('\\'):
                _fail('observer_image_alias')
            if volume is None:
                volume = info[0]
            if info[0] != volume:
                _fail('observer_image_nonlocal')
            directories.append(handle)
        handle = self.acquire('CreateFileW', 'image_file', path, 0x80000000, FILE_SHARE,
            None, 3, 0x00200000, None)
        if self.call('GetFileType', handle) != 1:
            _fail('observer_image_nonlocal')
        info = self.file_info(handle)
        if info[3] & (0x400 | 0x10):
            _fail('observer_image_reparse')
        if info[0] != volume or self.final_path(handle).lower() != path.lower():
            _fail('observer_image_alias')
        lease = (handle, path, info, tuple(directories))
        return lease

    def image_facts(self, lease):
        handle, path, initial, _ = lease
        before = self.file_info(handle)
        if before != initial:
            _fail('observer_image_drift')
        if not 0 < before[2] <= MAX_IMAGE_BYTES:
            _fail('observer_image_size_bound')
        self.required('SetFilePointerEx', handle, ctypes.c_longlong(0), None, 0)
        digest, remaining = hashlib.sha256(), before[2]
        buffer = ctypes.create_string_buffer(CHUNK_BYTES)
        while remaining:
            read = w.DWORD()
            self.required('ReadFile', handle, buffer, min(CHUNK_BYTES, remaining), ctypes.byref(read), None,
                code='observer_image_read_failed')
            if not 0 < read.value <= min(CHUNK_BYTES, remaining):
                _fail('observer_image_read_failed')
            digest.update(buffer.raw[:read.value]); remaining -= read.value
        extra = w.DWORD()
        self.required('ReadFile', handle, buffer, 1, ctypes.byref(extra), None, code='observer_image_read_failed')
        if extra.value or self.file_info(handle) != before:
            _fail('observer_image_drift')
        name = path.rsplit('\\', 1)[-1].lower()
        return ImageFileFacts(before[0], before[1], before[2], digest.hexdigest(),
            _sha(path), _sha(name), name if name in _KNOWN_NAMES else None)

    def close_all(self):
        # Cleanup is not suppressed by the collection deadline or one failed
        # close. Never close the borrowed Job handle.
        errors = []
        for handle, kind in reversed(self.handles):
            try:
                if not self.kernel.CloseHandle(handle):
                    errors.append((kind, ctypes.get_last_error()))
            except (OSError, ValueError, TypeError, ctypes.ArgumentError):
                errors.append((kind, None))
        self.handles.clear()
        return tuple(errors)


class OwnedJobObserver:
    """Closed production entry. Only the current installed actual WindowsJob."""
    def __init__(self, job):
        self._initialize(time.monotonic, 'CURRENT_KERNEL_DIAGNOSTIC')
        self._check()
        _actual_job(job)
        self._check()
        self._ports = _Win32Ports(job, self._check)

    def _initialize(self, clock, kind):
        self._clock, self._kind = clock, kind
        self._started = clock(); self._deadline = self._started + MAX_SECONDS
        self._ports = None
        self._retained, self._records, self._failures = {}, [], []
        self._root = None; self._initial_total = None; self._initial_lifetimes = None
        self._entered = False; self._closed = False; self._receipt = None

    @classmethod
    def _for_test(cls, ports, clock):
        # Private offline syscall seam; this kind never becomes authority or a
        # CURRENT_KERNEL receipt. Production constructor accepts no adapters.
        instance = object.__new__(cls)
        instance._initialize(clock, 'OFFLINE_FAULT_TEST')
        instance._ports = ports
        return instance

    def _sticky(self, error):
        code = error.code if isinstance(error, JobObserverError) else 'observer_syscall_failed'
        if code not in self._failures:
            self._failures.append(code)
        return JobObserverError(code, winerror=getattr(error, 'winerror', None))

    def _check(self):
        if self._closed:
            _fail('observer_closed')
        if self._clock() >= self._deadline:
            _fail('observer_deadline')

    def _record(self, value):
        if len(self._records) >= MAX_RECORDS:
            _fail('observer_record_bound')
        self._records.append(DiagnosticRecord(**value))

    def __enter__(self):
        if self._entered:
            _fail('observer_reentered')
        self._entered = True
        return self

    def _snapshot(self):
        self._check()
        value = self._ports.job_snapshot()
        self._check()
        self._record({'kind': 'actual_job_snapshot', 'pids': value.pids,
            'assigned': value.assigned, 'listed': value.listed, 'total_processes': value.total,
            'active_processes': value.active, 'terminated_processes': value.terminated})
        integers = (value.assigned, value.listed, value.total, value.active, value.terminated)
        if (any(type(item) is not int or not 0 <= item < 2**32 for item in integers)
                or value.assigned != value.listed or value.listed != len(value.pids)
                or value.active != len(value.pids) or value.total < value.active
                or value.terminated > value.total or len(set(value.pids)) != len(value.pids)
                or self._root not in value.pids
                or any(type(pid) is not int or not 0 < pid < 2**32 for pid in value.pids)):
            _fail('observer_snapshot_invalid')
        if len(value.pids) > MAX_MEMBERS:
            _fail('observer_list_truncated')
        return value

    def _state(self, handle, *, pid, creation=None):
        self._check(); state = self._ports.member_state(handle); self._check()
        if (state.pid != pid or not re.fullmatch(r'[1-9][0-9]{0,19}', state.creation)
                or creation is not None and state.creation != creation
                or type(state.code) is not int or not 0 <= state.code < 2**32):
            _fail('observer_member_identity_changed')
        return state

    def _image(self, retained):
        self._check(); facts = self._ports.image_facts(retained.image_lease); self._check()
        if facts != retained.image:
            _fail('observer_image_drift')

    def capture(self):
        if self._failures:
            raise JobObserverError(self._failures[0])
        try:
            self._check()
            root = self._ports.verify_job(); self._check()
            if self._root is not None and root != self._root:
                _fail('observer_job_invalid')
            self._root = root
            before = self._snapshot()
            for pid in before.pids:
                retained = self._retained.get(pid)
                if retained is None:
                    if len(self._retained) >= MAX_LIFETIMES:
                        _fail('observer_member_bound')
                    self._check(); handle = self._ports.open_member(pid); self._check()
                    state = self._state(handle, pid=pid)
                    if not state.alive or not state.member or state.image_path is None:
                        _fail('observer_member_stale')
                    _local_parts(state.image_path)
                    name = state.image_path.rsplit('\\', 1)[-1].lower()
                    self._record({'kind': 'discovered_current_member_image', 'pid': pid,
                        'creation_time': state.creation, 'image_path_sha256': _sha(state.image_path),
                        'image_name_sha256': _sha(name),
                        'safe_basename': name if name in _KNOWN_NAMES else None})
                    self._check(); lease = self._ports.open_image(state.image_path); self._check()
                    self._check(); image = self._ports.image_facts(lease); self._check()
                    after = self._state(handle, pid=pid, creation=state.creation)
                    if not after.alive or not after.member or after.image_path != state.image_path:
                        _fail('observer_member_identity_changed')
                    retained = _Retained(handle, after, lease, image)
                    self._retained[pid] = retained
                    self._record({'kind': 'captured_current_member_lifetime', 'pid': pid,
                        'creation_time': after.creation, 'image_path_sha256': image.path_sha256})
                else:
                    state = self._state(retained.handle, pid=pid, creation=retained.state.creation)
                    if not state.alive:
                        _fail('observer_pid_reused_or_stale')
                    if not state.member or state.image_path != retained.state.image_path:
                        _fail('observer_member_identity_changed')
                    self._image(retained); retained.state = state
            for pid, retained in self._retained.items():
                if pid not in before.pids:
                    state = self._state(retained.handle, pid=pid, creation=retained.state.creation)
                    if state.alive:
                        _fail('observer_member_missing')
                    self._image(retained); retained.state = state
            after = self._snapshot()
            if before != after:
                _fail('observer_discovery_raced')
            self._check()
            if self._ports.verify_job() != self._root:
                _fail('observer_job_invalid')
            self._check()
            if self._initial_total is None:
                self._initial_total, self._initial_lifetimes = after.total, len(self._retained)
            elif (after.total < self._initial_total or after.total - self._initial_total
                    != len(self._retained) - self._initial_lifetimes):
                _fail('observer_missed_lifetime')
            return tuple(MemberObservation(item.state.pid, item.state.creation, item.state.alive,
                item.state.code, item.image) for item in self._retained.values())
        except Exception as error:
            raise self._sticky(error) from None

    def finish(self):
        if self._receipt is not None:
            return self._receipt
        try:
            if not self._records:
                _fail('observer_no_capture')
            self._check()
            if self._ports.verify_job() != self._root:
                _fail('observer_job_invalid')
            self._check()
            final_snapshot = self._snapshot()
            if (self._initial_total is not None and final_snapshot.total - self._initial_total
                    != len(self._retained) - self._initial_lifetimes):
                _fail('observer_missed_lifetime')
            for pid, retained in self._retained.items():
                state = self._state(retained.handle, pid=pid, creation=retained.state.creation)
                if state.alive and (not state.member or pid not in final_snapshot.pids
                        or state.image_path != retained.state.image_path):
                    _fail('observer_member_missing')
                if not state.alive and pid in final_snapshot.pids:
                    _fail('observer_pid_reused_or_stale')
                self._image(retained); retained.state = state
                self._record({'kind': 'terminal_retained_handle_fact', 'pid': pid,
                    'creation_time': state.creation, 'alive': state.alive, 'exit_code': state.code})
            self._check()
            if self._ports.verify_job() != self._root:
                _fail('observer_job_invalid')
            self._check()
            # Last fresh accounting/list snapshot is the observation cutoff.
            # New children during terminal reads/owner verification stay FAIL.
            cutoff = self._snapshot()
            if (self._initial_total is not None and cutoff.total - self._initial_total
                    != len(self._retained) - self._initial_lifetimes):
                _fail('observer_missed_lifetime')
            if cutoff != final_snapshot:
                _fail('observer_discovery_raced')
            self._record({'kind': 'observation_cutoff', 'pids': cutoff.pids,
                'total_processes': cutoff.total, 'active_processes': cutoff.active,
                'terminated_processes': cutoff.terminated})
        except Exception as error:
            self._sticky(error)
        finally:
            try:
                cleanup_errors = self._ports.close_all() if self._ports is not None else ()
            except Exception:
                cleanup_errors = (('observer_owned_handle', None),)
            if cleanup_errors:
                self._sticky(JobObserverError('observer_handle_cleanup_failed'))
            self._closed = True
        members = tuple(MemberObservation(item.state.pid, item.state.creation, item.state.alive,
            item.state.code, item.image) for item in self._retained.values())
        self._receipt = ObserverReceipt('DIAGNOSTIC_FAILED' if self._failures else 'DIAGNOSTIC_COMPLETE',
            self._kind, self._root or 0, tuple(self._records), members, tuple(self._failures),
            not cleanup_errors, max(0.0, self._clock() - self._started))
        return self._receipt

    @property
    def receipt(self):
        if self._receipt is None:
            _fail('observer_closed')
        return self._receipt

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self._sticky(JobObserverError('observer_body_interrupted'))
        self.finish()
        return False
