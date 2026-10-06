"""Offline faults for a diagnostic-only Job observer; no application launch."""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import ctypes
from ctypes import wintypes as w
from dataclasses import asdict, replace
import json
import sys
from uuid import uuid4
from types import SimpleNamespace

import pytest


def observer_module():
    return importlib.import_module('secretary.infrastructure.team_job_observer')


def test_observer_closed_public_interface_exists():
    name = 'secretary.infrastructure.team_job_observer'
    assert importlib.util.find_spec(name) is not None, 'new owned-Job observer interface is missing'
    module = importlib.import_module(name)
    assert tuple(inspect.signature(module.OwnedJobObserver).parameters) == ('job',)
    assert tuple(inspect.signature(module.OwnedJobObserver.capture).parameters) == ('self',)
    assert tuple(inspect.signature(module.OwnedJobObserver.finish).parameters) == ('self',)


@pytest.mark.parametrize('operation', ['process', 'directory'])
def test_successful_acquisition_is_owned_before_postcall_deadline(operation):
    module = observer_module()
    moment, closed = [0.0], []
    def check():
        if moment[0] >= 5:
            raise module.JobObserverError('observer_deadline')
    def acquire(*args):
        moment[0] = 6.0
        return 777
    ports = object.__new__(module._Win32Ports)
    ports.check, ports.handles = check, []
    ports.kernel = SimpleNamespace(OpenProcess=acquire, CreateFileW=acquire,
        GetDriveTypeW=lambda _: 3, CloseHandle=lambda value: closed.append(value) or True)
    with pytest.raises(module.JobObserverError, match='observer_deadline'):
        if operation == 'process':
            ports.open_member(123)
        else:
            ports.open_image(r'C:\scratch\python.exe')
    assert ports.handles == [(777, operation)], 'successful HANDLE must remain reachable for cleanup'
    assert ports.close_all() == ()
    assert closed == [777]


def test_truncated_pid_return_length_is_refused():
    module = observer_module()
    class List(ctypes.Structure):
        _fields_ = [('assigned', w.DWORD), ('listed', w.DWORD), ('pids', ctypes.c_size_t * 64)]
    def query(handle, kind, buffer, capacity, returned):
        value = ctypes.cast(buffer, ctypes.POINTER(List)).contents
        value.assigned = value.listed = 1
        value.pids[0] = 100
        ctypes.cast(returned, ctypes.POINTER(w.DWORD)).contents.value = 8
        return True
    ports = object.__new__(module._Win32Ports)
    ports.check, ports.borrowed_job_handle = lambda: None, 20
    ports.accounting = lambda: (1, 1, 0)
    ports.kernel = SimpleNamespace(QueryInformationJobObject=query)
    with pytest.raises(module.JobObserverError, match='observer_list_truncated'):
        ports.job_snapshot()


class SyntheticPorts:
    def __init__(self, module):
        self.module = module
        self.pids, self.total, self.terminated = (100,), 1, 0
        self.states = {}
        self.opened, self.closed = [], []
        self.close_errors = ()
        self.image_override = None
        self.calls = []
        self.owner = 100

    def verify_job(self):
        self.calls.append('verify_job')
        return self.owner

    def job_snapshot(self):
        return self.module._Snapshot(self.pids, len(self.pids), len(self.pids), self.total,
            len(self.pids), self.terminated)

    def open_member(self, pid):
        self.opened.append(pid)
        return pid

    def member_state(self, handle):
        return self.states.get(handle, self.module._MemberState(handle, str(handle),
            True, 259, True, r'C:\scratch\python.exe'))

    def open_image(self, path):
        self.module._local_parts(path)
        return path

    def image_facts(self, lease):
        return self.image_override or self.module.ImageFileFacts(7, 'a' * 32, 100, 'b' * 64,
            self.module._sha(lease), 'c' * 64, 'python.exe')

    def close_all(self):
        self.closed.extend(self.opened)
        return self.close_errors


def test_completed_receipt_records_cannot_be_mutated():
    module = observer_module()
    ports = SyntheticPorts(module)
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        observer.capture()
        receipt = observer.finish()
    with pytest.raises((TypeError, AttributeError)):
        receipt.records[0]['pids'] = (123,)


@pytest.mark.parametrize('short_lived', [False, True])
def test_finish_detects_new_lifetime_during_last_image_hash(short_lived):
    module = observer_module()
    ports = SyntheticPorts(module)
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        observer.capture()
        original = ports.image_facts
        def changed(lease):
            ports.total = 2
            if short_lived:
                ports.terminated = 1
            else:
                ports.pids = (100, 200)
            return original(lease)
        ports.image_facts = changed
        receipt = observer.finish()
    assert receipt.result == 'DIAGNOSTIC_FAILED', 'lifetime after final pre-hash snapshot must not be missed'
    assert 'observer_missed_lifetime' in receipt.failures or 'observer_discovery_raced' in receipt.failures
    assert ports.closed == [100]


def test_partial_image_acquisition_failure_retains_only_sanitized_discovery():
    module = observer_module()
    ports = SyntheticPorts(module)
    ports.open_image = lambda _: (_ for _ in ()).throw(module.JobObserverError('observer_syscall_failed', winerror=32))
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        with pytest.raises(module.JobObserverError):
            observer.capture()
        receipt = observer.finish()
    found = [record for record in receipt.records if record.kind == 'discovered_current_member_image']
    assert len(found) == 1
    assert found[0].pid == 100 and found[0].image_path_sha256 == module._sha(r'C:\scratch\python.exe')
    assert found[0].safe_basename == 'python.exe'
    assert receipt.members == () and receipt.result == 'DIAGNOSTIC_FAILED'
    assert r'C:\scratch' not in str(receipt)


@pytest.mark.parametrize('path', [r'\\server\share\x.exe', r'\\?\C:\x.exe', r'\\.\C:\x.exe',
    r'\Device\HarddiskVolume1\x.exe', 'x.exe', r'C:x.exe', r'C:\x.exe:stream', r'C:\..\x.exe',
    r'C:\.\x.exe', 'C:/x.exe', 'C:\\bad.\\x.exe', 'C:\\bad \\x.exe', r'C:\NUL.exe',
    r'C:\CON', r'C:\COM1.txt', 'C:\\x\x00.exe', r'C:\*.exe', r'C:\x?.exe', 'C:\\x\\'])
def test_nonlocal_device_alias_and_ambiguous_paths_are_rejected_before_open(path):
    module = observer_module()
    with pytest.raises(module.JobObserverError, match='observer_image_path_invalid'):
        module._local_parts(path)


def test_local_parts_accept_only_explicit_drive_components():
    module = observer_module()
    assert module._local_parts(r'C:\Tools\python.exe') == ('C:\\', ('Tools', 'python.exe'))


@pytest.mark.parametrize('value', [None, 100, object(), SimpleNamespace(handle=20, pid=100)])
def test_public_entry_rejects_caller_proof_and_duck_job(value):
    module = observer_module()
    with pytest.raises(module.JobObserverError, match='observer_job_invalid'):
        module.OwnedJobObserver(value)


def test_real_type_other_than_current_singleton_is_rejected():
    module = observer_module()
    other = object.__new__(module.jobs.WindowsJob)
    with pytest.raises(module.JobObserverError, match='observer_job_invalid'):
        module.OwnedJobObserver(other)


@pytest.mark.parametrize('argument', ['clock', 'ports', 'pids', 'identity', 'allowlist'])
def test_public_entry_has_no_trusted_adapter_inputs(argument):
    module = observer_module()
    with pytest.raises(TypeError):
        module.OwnedJobObserver(None, **{argument: object()})


def test_valid_offline_capture_never_promotes_authority_and_is_json_serializable():
    module = observer_module()
    ports = SyntheticPorts(module)
    ports.pids, ports.total = (100, 200), 2
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        members = observer.capture()
        receipt = observer.finish()
    assert ports.opened == [100, 200] and ports.closed == [100, 200]
    assert receipt.result == 'DIAGNOSTIC_COMPLETE' and receipt.evidence_kind == 'OFFLINE_FAULT_TEST'
    assert not receipt.activation_supported and not receipt.allowlist_expansion
    assert not receipt.mapped_image_qualified and receipt.diagnostic_only
    assert all(member.authorization == 'NOT_EVALUATED' and member.ancestry == 'UNKNOWN' for member in members)
    assert all(member.image.current_path_file_only and not member.image.mapped_image_qualified for member in members)
    raw = json.dumps(asdict(receipt))
    assert r'C:\scratch' not in raw and 'observation_cutoff' in raw


def test_exit259_is_terminal_when_wait_state_is_exited():
    module = observer_module()
    ports = SyntheticPorts(module)
    ports.pids, ports.total = (100, 200), 2
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        observer.capture()
        ports.pids, ports.terminated = (100,), 1
        ports.states[200] = module._MemberState(200, '200', False, 259, False, None)
        observer.capture()
        receipt = observer.finish()
    child = next(member for member in receipt.members if member.pid == 200)
    assert not child.alive and child.exit_code == 259
    assert receipt.result == 'DIAGNOSTIC_COMPLETE'


@pytest.mark.parametrize('fault', ['creation', 'member', 'path', 'pid', 'initial_exit'])
def test_current_member_lifetime_drift_and_stale_discovery_are_sticky(fault):
    module = observer_module()
    ports = SyntheticPorts(module)
    base = ports.member_state(100)
    calls = [0]
    def changed(handle):
        calls[0] += 1
        if fault == 'initial_exit':
            return replace(base, alive=False, code=7, member=False, image_path=None)
        if calls[0] == 1:
            return base
        return replace(base, **{'creation': {'creation': '999'}, 'member': {'member': False},
            'path': {'image_path': r'C:\scratch\other.exe'}, 'pid': {'pid': 999}}[fault])
    ports.member_state = changed
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        with pytest.raises(module.JobObserverError):
            observer.capture()
        # Changing the data back does not reset the failed collector.
        ports.member_state = lambda _: base
        with pytest.raises(module.JobObserverError):
            observer.capture()
        receipt = observer.finish()
    assert receipt.result == 'DIAGNOSTIC_FAILED' and ports.closed == [100]


def test_unlisted_still_alive_retained_member_is_not_filtered_out():
    module = observer_module()
    ports = SyntheticPorts(module)
    ports.pids, ports.total = (100, 200), 2
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        observer.capture()
        ports.pids, ports.terminated = (100,), 1
        with pytest.raises(module.JobObserverError, match='observer_member_missing'):
            observer.capture()
        receipt = observer.finish()
    assert receipt.result == 'DIAGNOSTIC_FAILED' and len(receipt.members) == 2


def test_discovered_pid_reappearing_after_retained_exit_is_refused():
    module = observer_module()
    ports = SyntheticPorts(module)
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        observer.capture()
        ports.states[100] = module._MemberState(100, '100', False, 0, False, None)
        with pytest.raises(module.JobObserverError, match='observer_pid_reused_or_stale'):
            observer.capture()
        receipt = observer.finish()
    assert receipt.result == 'DIAGNOSTIC_FAILED' and ports.opened == [100]


def test_short_lifetime_between_captures_is_detected_by_accounting():
    module = observer_module()
    ports = SyntheticPorts(module)
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        observer.capture()
        ports.total, ports.terminated = 2, 1
        with pytest.raises(module.JobObserverError, match='observer_missed_lifetime'):
            observer.capture()
        receipt = observer.finish()
    assert receipt.result == 'DIAGNOSTIC_FAILED'


@pytest.mark.parametrize('bad', ['duplicate', 'missing_root', 'count', 'boolean_pid', 'over64'])
def test_invalid_kernel_discovery_is_recorded_before_no_member_open(bad):
    module = observer_module()
    ports = SyntheticPorts(module)
    snapshot = module._Snapshot((100,), 1, 1, 1, 1, 0)
    if bad == 'duplicate': snapshot = module._Snapshot((100, 100), 2, 2, 2, 2, 0)
    if bad == 'missing_root': snapshot = module._Snapshot((200,), 1, 1, 1, 1, 0)
    if bad == 'count': snapshot = module._Snapshot((100,), 2, 2, 2, 2, 0)
    if bad == 'boolean_pid': snapshot = module._Snapshot((100, True), 2, 2, 2, 2, 0)
    if bad == 'over64': snapshot = module._Snapshot(tuple(range(100, 165)), 65, 65, 65, 65, 0)
    ports.job_snapshot = lambda: snapshot
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        with pytest.raises(module.JobObserverError):
            observer.capture()
        receipt = observer.finish()
    assert ports.opened == [] and receipt.records[0].kind == 'actual_job_snapshot'
    assert receipt.result == 'DIAGNOSTIC_FAILED'


def test_eighth_lifetime_bound_does_not_open_ninth_handle():
    module = observer_module()
    ports = SyntheticPorts(module)
    ports.pids, ports.total = tuple(range(100, 109)), 9
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        with pytest.raises(module.JobObserverError, match='observer_member_bound'):
            observer.capture()
        receipt = observer.finish()
    assert len(ports.opened) == 8 and ports.closed == ports.opened
    assert receipt.result == 'DIAGNOSTIC_FAILED'


def test_record_bound_and_finish_cleanup_are_not_unbounded():
    module = observer_module()
    ports = SyntheticPorts(module)
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        with pytest.raises(module.JobObserverError, match='observer_record_bound'):
            for _ in range(40):
                observer.capture()
        receipt = observer.finish()
    assert len(receipt.records) == 32 and receipt.result == 'DIAGNOSTIC_FAILED'
    assert ports.closed == [100]


def test_collection_deadline_never_prevents_close_attempts():
    module = observer_module()
    ports, moment = SyntheticPorts(module), [0.0]
    with module.OwnedJobObserver._for_test(ports, lambda: moment[0]) as observer:
        observer.capture()
        moment[0] = 5.01
        with pytest.raises(module.JobObserverError, match='observer_deadline'):
            observer.capture()
        receipt = observer.finish()
    assert receipt.result == 'DIAGNOSTIC_FAILED' and ports.closed == [100]


def test_file_hash_drift_is_sticky_and_has_no_path_leak():
    module = observer_module()
    ports = SyntheticPorts(module)
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        observer.capture()
        ports.image_override = replace(ports.image_facts(r'C:\scratch\python.exe'), sha256='d' * 64)
        with pytest.raises(module.JobObserverError, match='observer_image_drift'):
            observer.capture()
        receipt = observer.finish()
    assert receipt.result == 'DIAGNOSTIC_FAILED' and r'C:\scratch' not in str(receipt)


def test_finish_is_idempotent_and_capture_after_finish_is_refused():
    module = observer_module()
    ports = SyntheticPorts(module)
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        observer.capture()
        first = observer.finish()
        assert observer.finish() is first
        with pytest.raises(module.JobObserverError, match='observer_closed'):
            observer.capture()
    assert ports.closed == [100]


def test_cleanup_failure_is_in_receipt_before_any_complete_result():
    module = observer_module()
    ports = SyntheticPorts(module)
    ports.close_errors = (('process', 6),)
    with module.OwnedJobObserver._for_test(ports, lambda: 0.0) as observer:
        observer.capture()
        receipt = observer.finish()
    assert receipt.result == 'DIAGNOSTIC_FAILED' and not receipt.cleanup_complete
    assert 'observer_handle_cleanup_failed' in receipt.failures and ports.closed == [100]


def test_caller_body_error_is_not_suppressed_and_all_handles_are_closed():
    module = observer_module()
    ports = SyntheticPorts(module)
    observer = module.OwnedJobObserver._for_test(ports, lambda: 0.0)
    with pytest.raises(RuntimeError, match='synthetic body'):
        with observer:
            observer.capture()
            raise RuntimeError('synthetic body')
    assert observer.receipt.result == 'DIAGNOSTIC_FAILED'
    assert 'observer_body_interrupted' in observer.receipt.failures and ports.closed == [100]


def test_import_opens_no_files_or_handles_and_keeps_canonical_identity(monkeypatch):
    module = observer_module()
    source = inspect.getsource(module)
    alias = 'secretary.infrastructure._observer_import_' + uuid4().hex
    fresh = type(sys)(alias)
    fresh.__package__ = 'secretary.infrastructure'
    monkeypatch.setitem(sys.modules, alias, fresh)
    def forbidden(*args, **kwargs):
        raise AssertionError('import attempted an external effect')
    monkeypatch.setattr(ctypes, 'WinDLL', forbidden)
    monkeypatch.setattr('builtins.open', forbidden)
    exec(compile(source, '<bounded observer import>', 'exec'), fresh.__dict__)
    assert sys.modules[module.__name__] is module
    assert fresh.OwnedJobObserver is not module.OwnedJobObserver


def test_process_open_requests_only_read_and_sync_without_inheritance():
    module = observer_module()
    calls = []
    ports = object.__new__(module._Win32Ports)
    ports.check, ports.handles = lambda: None, []
    ports.kernel = SimpleNamespace(OpenProcess=lambda *args: calls.append(args) or 700)
    assert ports.open_member(123) == 700
    assert calls == [(0x00101000, False, 123)]
    assert not calls[0][0] & (0x1 | 0x2 | 0x8 | 0x10 | 0x20 | 0x100)


def file_open_ports(module, *, fault=None):
    ports = object.__new__(module._Win32Ports)
    ports.check, ports.handles = lambda: None, []
    calls, values, closed = [], {}, []
    def create(path, access, share, security, creation, flags, template):
        calls.append((path, access, share, security, creation, flags, template))
        handle = len(calls) + 700
        values[handle] = path
        return handle
    ports.kernel = SimpleNamespace(GetDriveTypeW=lambda _: 4 if fault == 'network_drive' else 3,
        CreateFileW=create, GetFileType=lambda _: 1,
        CloseHandle=lambda handle: closed.append(handle) or True)
    def info(handle):
        path = values[handle]
        directory = not path.endswith('.exe')
        attr = 0x10 if directory else 0x80
        if fault == 'parent_reparse' and path == r'C:\scratch': attr |= 0x400
        if fault == 'file_reparse' and not directory: attr |= 0x400
        volume = 8 if fault == 'file_volume' and not directory else 7
        return (volume, 'a' * 32, 4, attr, 100, 200)
    def final(handle):
        value = values[handle]
        return r'C:\other\python.exe' if fault == 'file_alias' and value.endswith('.exe') else value
    ports.file_info, ports.final_path = info, final
    return ports, calls, closed


def test_all_path_components_are_pinned_readonly_nonreparse_before_file():
    module = observer_module()
    ports, calls, closed = file_open_ports(module)
    lease = ports.open_image(r'C:\scratch\python.exe')
    assert [call[0] for call in calls] == ['C:\\', r'C:\scratch', r'C:\scratch\python.exe']
    assert all(call[2] == 1 and call[3] is None and call[4] == 3 for call in calls)
    assert all(call[5] & 0x00200000 for call in calls)
    assert all(call[5] & 0x02000000 for call in calls[:-1])
    assert calls[-1][1] == 0x80000000
    assert lease[0] == 703 and lease[3] == (701, 702)
    assert ports.close_all() == () and closed == [703, 702, 701]


@pytest.mark.parametrize('fault,code,opens', [('network_drive', 'observer_image_nonlocal', 0),
    ('parent_reparse', 'observer_image_reparse', 2), ('file_reparse', 'observer_image_reparse', 3),
    ('file_volume', 'observer_image_alias', 3), ('file_alias', 'observer_image_alias', 3)])
def test_reparse_network_volume_and_alias_refusals_close_partial_leases(fault, code, opens):
    module = observer_module()
    ports, calls, closed = file_open_ports(module, fault=fault)
    with pytest.raises(module.JobObserverError, match=code):
        ports.open_image(r'C:\scratch\python.exe')
    assert len(calls) == opens
    assert ports.close_all() == () and len(closed) == opens


@pytest.mark.parametrize('path', [r'\\host\share\x.exe', r'\\?\UNC\host\x.exe', r'C:\NUL.exe'])
def test_invalid_namespace_never_reaches_any_createfile(path):
    module = observer_module()
    ports, calls, closed = file_open_ports(module)
    with pytest.raises(module.JobObserverError):
        ports.open_image(path)
    assert calls == [] and ports.handles == [] and closed == []


def image_read_ports(module, *, fault=None, name='python.exe'):
    ports = object.__new__(module._Win32Ports)
    ports.check, ports.handles = lambda: None, [(77, 'image_file')]
    path = 'C:\\scratch\\' + name
    size = module.MAX_IMAGE_BYTES + 1 if fault == 'size' else 4
    identity = (7, 'a' * 32, size, 0x80, 100, 200)
    position, infos, reads, closed = [0], [0], [], []
    def info(handle):
        assert handle == 77
        infos[0] += 1
        if fault == 'id_drift' and infos[0] > 1:
            return (7, 'd' * 32, size, 0x80, 100, 200)
        return identity
    def seek(handle, offset, result, whence):
        assert handle == 77 and whence == 0
        position[0] = 0
        return True
    def read(handle, buffer, capacity, returned, overlapped):
        assert handle == 77 and overlapped is None
        reads.append((handle, capacity))
        data = b'abcd'[position[0]:position[0] + capacity]
        if fault == 'short': data = b''
        if fault == 'overflow' and position[0] == 4: data = b'x'
        if data: ctypes.memmove(buffer, data, len(data))
        ctypes.cast(returned, ctypes.POINTER(w.DWORD)).contents.value = len(data)
        position[0] += len(data)
        return True
    ports.file_info = info
    # No filename opener is supplied: all reads must use the same file HANDLE.
    ports.kernel = SimpleNamespace(SetFilePointerEx=seek, ReadFile=read,
        CloseHandle=lambda handle: closed.append(handle) or True)
    return ports, (77, path, identity, ()), reads, closed


def test_image_hash_and_identity_come_only_from_the_retained_file_handle():
    module = observer_module()
    ports, lease, reads, closed = image_read_ports(module)
    image = ports.image_facts(lease)
    assert image.sha256 == module.hashlib.sha256(b'abcd').hexdigest()
    assert image.file_id == 'a' * 32 and image.volume_serial == 7 and image.size == 4
    assert reads == [(77, 4), (77, 1)] and image.safe_basename == 'python.exe'
    assert image.path_sha256 == module._sha(r'C:\scratch\python.exe')
    assert ports.close_all() == () and closed == [77]


@pytest.mark.parametrize('fault,code', [('size', 'observer_image_size_bound'),
    ('short', 'observer_image_read_failed'), ('overflow', 'observer_image_drift'), ('id_drift', 'observer_image_drift')])
def test_bounded_full_image_read_refuses_short_extra_and_changed_file(fault, code):
    module = observer_module()
    ports, lease, reads, closed = image_read_ports(module, fault=fault)
    with pytest.raises(module.JobObserverError, match=code):
        ports.image_facts(lease)
    if fault == 'size': assert reads == []
    assert ports.close_all() == () and closed == [77]


def test_unknown_image_name_is_hash_only_and_never_authority():
    module = observer_module()
    ports, lease, _, _ = image_read_ports(module, name='synthetic-private-business.exe')
    facts = ports.image_facts(lease)
    assert facts.safe_basename is None
    assert facts.name_sha256 == module._sha('synthetic-private-business.exe')
    assert 'synthetic-private-business.exe' not in str(facts)
    assert facts.mapped_image_qualified is False


def test_close_all_attempts_remaining_handles_after_a_close_failure():
    module = observer_module()
    closed = []
    ports = object.__new__(module._Win32Ports)
    ports.handles = [(10, 'process'), (11, 'directory'), (12, 'image_file')]
    ports.kernel = SimpleNamespace(CloseHandle=lambda handle: closed.append(handle) or handle != 11)
    errors = ports.close_all()
    assert closed == [12, 11, 10] and len(errors) == 1 and ports.handles == []


def test_syscall_exception_messages_are_sanitized_without_raw_values():
    module = observer_module()
    ports = object.__new__(module._Win32Ports)
    ports.check = lambda: None
    ports.kernel = SimpleNamespace(ReadFile=lambda *args: (_ for _ in ()).throw(
        OSError('DO_NOT_LEAK_SYNTHETIC_PATH')))
    with pytest.raises(module.JobObserverError) as found:
        ports.call('ReadFile')
    assert str(found.value) == 'observer_syscall_failed'
    assert 'DO_NOT_LEAK' not in str(found.value)
