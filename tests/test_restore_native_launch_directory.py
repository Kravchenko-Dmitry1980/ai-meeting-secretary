"""Real Windows CWD acceptance with an owned Python child, never native/HTTP.

The existing actual four-source input/custody test remains authoritative for
source leases and config checks. This adds the Win32 CreateProcess behavior
that extended-path file opens alone cannot validate.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import test_restore_managed_native as existing
from test_restore_fresh_evidence import sources, copy, setup, prepared, staged


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='real Windows CreateProcess CWD acceptance')


@pytest.fixture
def tmp_path(tmp_path_factory):
    # A realistic 140-character project root makes the old nested purpose
    # journal/input directory deterministically exceed the Win32 CWD limit.
    base = tmp_path_factory.mktemp('native-cwd-regression')
    component = 'p' * max(8, 140 - len(str(base)) - 1)
    root = base / component
    root.mkdir()
    return root


def _python_cwd_probe(folder, environment):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, '-B', '-c', 'import os,json;print(json.dumps(os.getcwd()))'],
            cwd=folder, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
        handle = int(process._handle)  # Original HANDLE; no numeric PID lookup.
        assert kernel.WaitForSingleObject(handle, 5000) == 0, 'owned CWD probe must terminate within 5 seconds'
        output, _ = process.communicate(timeout=5)
        assert process.returncode == 0
        assert len(output) <= 4096, 'literal CWD probe output must stay bounded'
        observed = json.loads(output)
        assert type(observed) is str
        assert os.path.normcase(os.path.normpath(observed)) == os.path.normcase(os.path.normpath(str(folder)))
        return observed
    finally:
        if process is not None:
            handle = int(process._handle)
            try:
                signal = kernel.WaitForSingleObject(handle, 0)
                assert signal in (0, 258), 'owned retained HANDLE status must be known'
                if signal == 258:
                    process.terminate()  # Popen targets this original HANDLE.
                    assert kernel.WaitForSingleObject(handle, 5000) == 0, 'owned CWD probe cleanup timed out'
                process.wait(timeout=0)  # Reap even a terminal exit code 259.
            finally:
                if process.stdout is not None:
                    process.stdout.close()
                process._handle.Close()


def test_native_inputs_cwd_is_accepted_by_real_windows_process_creation(sources, monkeypatch):
    lane = existing.lane
    from secretary.infrastructure.restore_native_observation_store import NativeObservationStore
    original_environment = lane._windows_environment
    original_inputs = lane._inputs
    original_create = NativeObservationStore.create
    launches, journal_creations, journal_digest = [], [], {}

    def environment_with_owned_cwd_probe(folder):
        environment = original_environment(folder)
        launches.append(_python_cwd_probe(folder, environment))
        return environment

    def record_create(cls, path, binding, *, private_folder):
        result = original_create(path, binding, private_folder=private_folder)
        journal_creations.append(Path(path))
        journal_digest[str(path)] = hashlib.sha256(Path(lane._win32_path(path)).read_bytes()).hexdigest()
        return result

    @contextmanager
    def inputs_with_permanent_journal_check(root, config, evidence, held):
        with original_inputs(root, config, evidence, held) as inputs:
            # The durable purpose journal is fixed by restore/epoch identity;
            # accepting another CWD must not relocate or recreate its history.
            permanent = (root / '.runtime/team-operator/activations' / evidence.restore_id /
                'source-epochs' / evidence.epoch_id / 'native-observations')
            assert inputs['directory'] == permanent
            assert journal_creations == [permanent / 'observations.sqlite3']
            yield inputs

    monkeypatch.setattr(lane, '_windows_environment', environment_with_owned_cwd_probe)
    monkeypatch.setattr(lane, '_inputs', inputs_with_permanent_journal_check)
    monkeypatch.setattr(NativeObservationStore, 'create', classmethod(record_create))
    existing.test_inputs_are_built_from_pinned_config_and_fixed_environment_under_actual_leases(sources, monkeypatch)
    assert len(launches) == 1, 'the actual accepted CWD must be exercised once'
    assert len(journal_creations) == 1, 'launch validation must preserve create-once journal history'
    for journal in journal_creations:
        assert hashlib.sha256(Path(lane._win32_path(journal)).read_bytes()).hexdigest() == journal_digest[str(journal)]


def _root_for_launch_units(lane, units, *, astral=False):
    stem = 'seed' + ('\U0001f600' * 8 if astral else '')
    base = Path('D:/') / stem
    baseline = lane._launch_directory(base)
    padding = units - len(str(baseline).encode('utf-16-le')) // 2
    assert padding >= 0
    return Path('D:/') / (stem + 'p' * padding)


@pytest.mark.parametrize('units', [259, 260])
@pytest.mark.parametrize('astral', [False, True])
def test_launch_directory_counts_windows_utf16_units_at_boundary(monkeypatch, units, astral):
    lane = existing.lane
    monkeypatch.setattr(lane, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    root = _root_for_launch_units(lane, units, astral=astral)
    if units == 259:
        folder = lane._launch_directory(root)
        assert len(str(folder).encode('utf-16-le')) // 2 == 259
        if astral:
            assert len(str(folder)) < 259, 'astral characters occupy two Windows UTF16 units'
    else:
        with pytest.raises(lane.ManagedNativeError, match='managed_native_configuration'):
            lane._launch_directory(root)


def test_excessive_launch_directory_refused_before_custody_creation_or_authority(monkeypatch):
    lane = existing.lane
    from secretary.infrastructure.restore_native_observation_store import NativeObservationStore
    monkeypatch.setattr(lane, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    root = _root_for_launch_units(lane, 260, astral=True)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('unexpected_side_effect_port')
        raise AssertionError('over-limit CWD must fail before any custody, file, settings or authority port')
    for name in ('ExitStack', '_native_file_custody', 'load_team_settings', '_principal_projection',
            'create_private_directory', 'protected_scope', 'OperatorAuthority', '_windows_environment'):
        monkeypatch.setattr(lane, name, forbidden)
    monkeypatch.setattr(NativeObservationStore, 'create', forbidden)
    monkeypatch.setattr(NativeObservationStore, 'open_existing', forbidden)
    with pytest.raises(lane.ManagedNativeError, match='managed_native_configuration'):
        with lane._inputs(root, root / 'synthetic-config.json', object(), object()):
            pytest.fail('over-limit launch directory was admitted')
    assert calls == []
