"""File-only custody: real Windows sharing checks and separate injected faults.

The runner supplies a fresh guarded tmp_path. No child, SQLite or network work.
"""
from contextlib import contextmanager
import ctypes
from dataclasses import FrozenInstanceError, asdict, replace
import hashlib
import importlib.util
import os
from pathlib import Path
import sys

import pytest

from secretary.infrastructure import restore_native_custody as custody
from secretary.infrastructure.restore_operator import create_private_directory


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='real Windows custody required')
PAYLOAD = b'SQLite format 3\x00' + bytes(range(256)) * 600


@pytest.fixture
def source(tmp_path):
    parent = tmp_path / 'private'
    create_private_directory(parent)
    path = parent / 'synthetic.sqlite3'
    path.write_bytes(PAYLOAD)
    return path


@contextmanager
def writer_handle(path):
    """Independent actual Win32 writer, sharing read/write but never delete."""
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    handle = kernel.CreateFileW(str(path), 0x40000000, 3, None, 3, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError('synthetic_writer_denied')
    try:
        yield handle
    finally:
        assert kernel.CloseHandle(handle), 'synthetic_writer_close_failed'


def assert_locked(path, *, write):
    if write:
        with pytest.raises(OSError):
            with writer_handle(path):
                pass
    with pytest.raises(OSError):
        path.unlink()
    with pytest.raises(OSError):
        path.rename(path.with_name('replacement.sqlite3'))
    assert path.exists()


def no_receipt(session):
    with pytest.raises(custody.NativeCustodyError):
        _ = session.receipt


def check_diagnostic(value, path):
    text = repr(value)
    assert str(path) not in text
    assert 'SQLite format' not in text
    assert value.identity
    assert value.length == len(PAYLOAD)
    assert value.sha256 == hashlib.sha256(PAYLOAD).hexdigest()


def fail_method(monkeypatch, name, *, at=1):
    original = getattr(custody._Win32FileAPI, name)
    calls = []
    def failing(self, *args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == at:
            raise OSError('DO_NOT_LEAK_RAW_OS_ERROR')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(custody._Win32FileAPI, name, failing)
    return calls


def test_initial_final_actual_writer_delete_rename_exclusion(source):
    with custody._native_file_custody(source) as session:
        assert session.state == 'initial_nowrite'
        check_diagnostic(session.baseline, source)
        assert session.baseline.links == 1
        no_receipt(session)
        assert_locked(source, write=True)
        session.enter_nodelete_phase()
        assert session.state == 'shared_nodelete'
        with writer_handle(source):
            pass
        assert_locked(source, write=False)
        interim = session.restore_nowrite_and_verify()
        assert session.state == 'returned_nowrite'
        check_diagnostic(interim, source)
        assert not hasattr(interim, 'closed')
        no_receipt(session)
        assert_locked(source, write=True)
    assert session.state == 'closed'
    receipt = session.receipt
    check_diagnostic(receipt, source)
    assert receipt.closed and receipt.diagnostic_only
    assert receipt.activation_supported is False
    assert receipt.outbound_enabled is False
    with writer_handle(source):
        pass
    assert source.read_bytes() == PAYLOAD


def test_context_without_shared_phase_verifies_and_closes(source):
    with custody._native_file_custody(source) as session:
        baseline = session.baseline
    assert session.receipt.sha256 == baseline.sha256
    assert session.receipt.identity == baseline.identity


def test_facts_state_and_receipt_are_immutable(source):
    with custody._native_file_custody(source) as session:
        with pytest.raises((FrozenInstanceError, AttributeError)):
            session.baseline.length = 0
        with pytest.raises(AttributeError):
            session.baseline = None
        with pytest.raises(AttributeError):
            session.state = 'closed'
        with pytest.raises(TypeError):
            type(session)()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        session.receipt.closed = False
    assert set(asdict(session.receipt)) == {
        'identity', 'attributes', 'links', 'length', 'sha256', 'closed',
        'diagnostic_only', 'activation_supported', 'outbound_enabled'}


def test_both_handoffs_overlap_and_handles_are_readonly_noninherited(source, monkeypatch):
    opened, live, overlaps = [], set(), []
    original_open = custody._Win32FileAPI.open
    original_close = custody._Win32FileAPI.close
    original_metadata = custody._Win32FileAPI.metadata
    def opening(self, path, *, deny_write):
        handle = original_open(self, path, deny_write=deny_write)
        opened.append((handle, deny_write))
        live.add(handle)
        assert self.inheritance(handle) is False
        if len(opened) > 1:
            assert opened[-2][0] in live
            overlaps.append(tuple(live))
            assert_locked(source, write=True)
        return handle
    def closing(self, handle):
        result = original_close(self, handle)
        live.remove(handle)
        return result
    identities = []
    def metadata(self, handle):
        assert handle in live
        result = original_metadata(self, handle)
        identities.append(result.identity)
        return result
    monkeypatch.setattr(custody._Win32FileAPI, 'open', opening)
    monkeypatch.setattr(custody._Win32FileAPI, 'close', closing)
    monkeypatch.setattr(custody._Win32FileAPI, 'metadata', metadata)
    with custody._native_file_custody(source) as session:
        session.enter_nodelete_phase()
        session.restore_nowrite_and_verify()
    assert [deny for _, deny in opened] == [True, False, True]
    assert len(overlaps) == 2 and not live
    assert len(set(identities)) == 1


def test_actual_open_writer_prevents_return_and_failure_is_sticky(source):
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source) as session:
            session.enter_nodelete_phase()
            with writer_handle(source):
                with pytest.raises(custody.NativeCustodyError):
                    session.restore_nowrite_and_verify()
            assert session.state == 'failed'
            with pytest.raises(custody.NativeCustodyError):
                session.restore_nowrite_and_verify()
    no_receipt(session)
    with writer_handle(source):
        pass


@pytest.mark.parametrize('mutation', ['same_size', 'size', 'header'])
def test_actual_shared_byte_size_header_drift_is_preserved(source, mutation):
    changed = {'same_size': PAYLOAD[:-1] + b'X', 'size': PAYLOAD + b'X',
               'header': b'CORRUPTED HEADER!' + PAYLOAD[17:]}[mutation]
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source) as session:
            session.enter_nodelete_phase()
            source.write_bytes(changed)
            session.restore_nowrite_and_verify()
    no_receipt(session)
    assert source.read_bytes() == changed


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
@pytest.mark.parametrize('phase', ['initial', 'return', 'final'])
def test_sidecars_rejected_before_read_and_preserved(source, monkeypatch, suffix, phase):
    reads = []
    original = custody._Win32FileAPI.read
    def reading(self, handle, size):
        reads.append(handle)
        return original(self, handle, size)
    monkeypatch.setattr(custody._Win32FileAPI, 'read', reading)
    sidecar = Path(str(source) + suffix)
    if phase == 'initial':
        sidecar.write_bytes(b'journal evidence')
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source) as session:
            before = len(reads)
            sidecar.write_bytes(b'journal evidence')
            if phase == 'return':
                session.enter_nodelete_phase()
                with pytest.raises(custody.NativeCustodyError):
                    session.restore_nowrite_and_verify()
                assert len(reads) == before
            elif phase == 'final':
                pass
    assert len(reads) == (0 if phase == 'initial' else before)
    assert sidecar.read_bytes() == b'journal evidence'
    assert source.read_bytes() == PAYLOAD


@pytest.mark.parametrize('bad', ['relative', 'unc', 'device', 'ads', 'missing', 'directory'])
def test_invalid_input_before_read(source, monkeypatch, bad):
    candidates = {'relative': Path('synthetic.sqlite3'), 'unc': r'\\invalid\share\file',
                  'device': r'\\?\C:\file', 'ads': str(source) + ':stream',
                  'missing': source.with_name('missing'), 'directory': source.parent}
    def forbidden(*args, **kwargs):
        pytest.fail('invalid input reached source ReadFile')
    monkeypatch.setattr(custody._Win32FileAPI, 'read', forbidden)
    with pytest.raises(custody.NativeCustodyError) as caught:
        with custody._native_file_custody(candidates[bad]):
            pytest.fail('invalid input accepted')
    assert str(source) not in str(caught.value)


def test_unprotected_parent_is_rejected(tmp_path):
    path = tmp_path / 'plain.sqlite3'
    path.write_bytes(PAYLOAD)
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(path):
            pytest.fail('unprotected parent accepted')


def test_actual_multiple_hardlinks_are_rejected(source):
    alias = source.with_name('hardlink.sqlite3')
    os.link(source, alias)
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source):
            pytest.fail('multiple hard links accepted')
    assert alias.read_bytes() == PAYLOAD


def test_actual_reparse_is_rejected_when_host_supports_symlink(source):
    alias = source.with_name('symlink.sqlite3')
    try:
        alias.symlink_to(source)
    except OSError:
        pytest.skip('host does not permit synthetic symlink creation')
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(alias):
            pytest.fail('reparse accepted')
    assert alias.is_symlink()


@pytest.mark.parametrize('field', ['identity', 'links', 'attributes', 'length'])
def test_injected_metadata_drift_despite_same_bytes(source, monkeypatch, field):
    original = custody._Win32FileAPI.metadata
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source) as session:
            session.enter_nodelete_phase()
            def changed(self, handle):
                facts = original(self, handle)
                value = {'identity': (0, 0), 'links': 2,
                         'attributes': facts.attributes | 0x400,
                         'length': facts.length + 1}[field]
                return replace(facts, **{field: value})
            monkeypatch.setattr(custody._Win32FileAPI, 'metadata', changed)
            session.restore_nowrite_and_verify()
    no_receipt(session)
    assert source.read_bytes() == PAYLOAD


@pytest.mark.parametrize('phase', ['return', 'final'])
def test_foreign_identity_rejected_before_readfile(source, monkeypatch, phase):
    """Injected metadata boundary, not evidence of real replace under custody."""
    original_metadata = custody._Win32FileAPI.metadata
    original_read = custody._Win32FileAPI.read
    reads = []
    def reading(self, handle, size):
        reads.append(handle)
        return original_read(self, handle, size)
    monkeypatch.setattr(custody._Win32FileAPI, 'read', reading)
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source) as session:
            if phase == 'return':
                session.enter_nodelete_phase()
            reads.clear()
            def foreign_metadata(self, handle):
                facts = original_metadata(self, handle)
                foreign = (facts.identity[0], facts.identity[1] + 1)
                return replace(facts, identity=foreign)
            monkeypatch.setattr(custody._Win32FileAPI, 'metadata', foreign_metadata)
            if phase == 'return':
                session.restore_nowrite_and_verify()
    assert reads == []
    no_receipt(session)


@pytest.mark.parametrize('code', [pytest.param([], id='list'), pytest.param({}, id='dict')])
def test_unhashable_error_code_is_sanitized(code):
    error = custody.NativeCustodyError(code)
    assert error.code == 'native_custody_invalid'
    assert str(error) == 'native_custody_invalid'


@pytest.mark.parametrize('flag,value', [('closed', False), ('diagnostic_only', False),
                                      ('activation_supported', True), ('outbound_enabled', True)])
def test_diagnostic_receipt_flags_cannot_be_constructed_or_replaced(source, flag, value):
    with custody._native_file_custody(source) as session:
        pass
    receipt = session.receipt
    facts = {field: getattr(receipt, field)
             for field in ('identity', 'attributes', 'links', 'length', 'sha256')}
    with pytest.raises((TypeError, ValueError)):
        type(receipt)(**facts, **{flag: value})
    with pytest.raises((TypeError, ValueError)):
        replace(receipt, **{flag: value})


@pytest.mark.parametrize('stage', ['initial', 'handoff', 'return'])
def test_injected_open_failure_closes_owned_handles(source, monkeypatch, stage):
    attempts = {'initial': 1, 'handoff': 2, 'return': 3}
    calls = fail_method(monkeypatch, 'open', at=attempts[stage])
    with pytest.raises(custody.NativeCustodyError) as caught:
        with custody._native_file_custody(source) as session:
            session.enter_nodelete_phase()
            session.restore_nowrite_and_verify()
    assert len(calls) >= attempts[stage]
    assert 'DO_NOT_LEAK' not in str(caught.value)
    with writer_handle(source):
        pass


@pytest.mark.parametrize('method', ['read', 'metadata', 'inheritance', 'rewind'])
def test_injected_initial_native_error_is_sanitized_and_cleanup_runs(source, monkeypatch, method):
    fail_method(monkeypatch, method)
    with pytest.raises(custody.NativeCustodyError) as caught:
        with custody._native_file_custody(source):
            pytest.fail('fault accepted')
    assert str(source) not in str(caught.value)
    assert 'DO_NOT_LEAK' not in str(caught.value)
    with writer_handle(source):
        pass


@pytest.mark.parametrize('at', [1, 2, 3])
def test_injected_close_failure_is_sticky_and_remaining_handles_close(source, monkeypatch, at):
    original = custody._Win32FileAPI.close
    original_open = custody._Win32FileAPI.open
    closed, calls = [], []
    incarnations = {}
    acquisitions = []
    def opening(self, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        acquisitions.append(handle)
        incarnations[handle] = len(acquisitions)
        return handle
    def closing(self, handle):
        ordinal = incarnations.pop(handle)
        calls.append(ordinal)
        result = original(self, handle)  # Real resource released; outcome fails.
        closed.append(ordinal)
        if len(calls) == at:
            raise OSError('DO_NOT_LEAK_CLOSE_ERROR')
        return result
    monkeypatch.setattr(custody._Win32FileAPI, 'open', opening)
    monkeypatch.setattr(custody._Win32FileAPI, 'close', closing)
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source) as session:
            session.enter_nodelete_phase()
            session.restore_nowrite_and_verify()
    no_receipt(session)
    assert len(calls) >= at
    assert len(set(closed)) == len(closed)
    assert len(closed) == len(acquisitions) and not incarnations
    with writer_handle(source):
        pass


@pytest.mark.parametrize('bound', ['bytes', 'deadline'])
def test_deterministic_hash_bound_rejects_without_receipt(source, monkeypatch, bound):
    if bound == 'bytes':
        monkeypatch.setattr(custody, 'MAX_BYTES', len(PAYLOAD) - 1)
    else:
        tick = iter([0.0, custody.MAX_SECONDS + 1.0] * 100)
        monkeypatch.setattr(custody, '_clock', lambda: next(tick))
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source):
            pytest.fail('bound accepted')
    with writer_handle(source):
        pass


def test_stream_is_bounded_and_full_sha256_comes_from_owned_handle(source, monkeypatch):
    original = custody._Win32FileAPI.read
    lengths, handles = [], set()
    def reading(self, handle, size):
        assert 0 < size <= custody.CHUNK_BYTES
        chunk = original(self, handle, size)
        lengths.append(len(chunk))
        handles.add(handle)
        return chunk
    monkeypatch.setattr(custody._Win32FileAPI, 'read', reading)
    with custody._native_file_custody(source) as session:
        check_diagnostic(session.baseline, source)
    assert sum(lengths) == 2 * len(PAYLOAD)
    assert len(handles) == 1 and len(lengths) > 2


def test_injected_eof_before_metadata_length_is_rejected(source, monkeypatch):
    monkeypatch.setattr(custody._Win32FileAPI, 'read', lambda *args: b'')
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source):
            pytest.fail('short read accepted')


@pytest.mark.parametrize('operation', ['return_initial', 'enter_twice', 'return_twice'])
def test_wrong_or_repeated_transition_is_sticky(source, operation):
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source) as session:
            if operation != 'return_initial':
                session.enter_nodelete_phase()
            if operation == 'return_twice':
                session.restore_nowrite_and_verify()
            with pytest.raises(custody.NativeCustodyError):
                (session.enter_nodelete_phase if operation == 'enter_twice'
                 else session.restore_nowrite_and_verify)()
            no_receipt(session)
    no_receipt(session)


def test_closed_session_rejects_transitions_and_context_cannot_reenter(source):
    context = custody._native_file_custody(source)
    with context as session:
        pass
    for method in (session.enter_nodelete_phase, session.restore_nowrite_and_verify):
        with pytest.raises(custody.NativeCustodyError):
            method()
    with pytest.raises((custody.NativeCustodyError, AttributeError)):
        with context:
            pytest.fail('context reentered')


def test_incomplete_shared_phase_releases_resources_but_has_no_receipt(source):
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source) as session:
            session.enter_nodelete_phase()
    no_receipt(session)
    with writer_handle(source):
        pass


def test_body_error_survives_successful_final_verification(source):
    error = RuntimeError('synthetic body error')
    with pytest.raises(RuntimeError) as caught:
        with custody._native_file_custody(source) as session:
            raise error
    assert caught.value is error
    with writer_handle(source):
        pass


def test_failed_final_verification_overrides_body_error(source):
    sidecar = Path(str(source) + '-journal')
    with pytest.raises(custody.NativeCustodyError):
        with custody._native_file_custody(source) as session:
            sidecar.write_bytes(b'preserve final evidence')
            raise RuntimeError('synthetic body error')
    no_receipt(session)
    assert sidecar.read_bytes() == b'preserve final evidence'


def test_import_has_no_native_store_process_or_network_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('import performed native or file I/O')
    monkeypatch.setattr(ctypes, 'WinDLL', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(Path, 'write_bytes', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    canonical_error = custody.NativeCustodyError
    name = 'secretary.infrastructure._isolated_native_custody_import_test'
    spec = importlib.util.spec_from_file_location(name, custody.__file__)
    isolated = importlib.util.module_from_spec(spec)
    try:
        sys.modules[name] = isolated
        spec.loader.exec_module(isolated)
        assert custody.NativeCustodyError is canonical_error
    finally:
        sys.modules.pop(name, None)
