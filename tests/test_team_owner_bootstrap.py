"""Offline first-owner bootstrap: only explicitly new synthetic workspaces."""
from __future__ import annotations

import copy
from importlib import util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from secretary.domain.team import TeamMember
from secretary.infrastructure.team_database import SCHEMA_VERSION, TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/team/bootstrap_owner.py'


@pytest.fixture
def bootstrap():
    assert SCRIPT.is_file(), 'Missing bounded first-owner bootstrap implementation'
    spec = util.spec_from_file_location('team_owner_bootstrap_under_test', SCRIPT)
    module = util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def case(tmp_path):
    root = tmp_path / 'synthetic-project'
    root.mkdir()
    target = root / '.runtime/team/fresh-team.sqlite3'
    owner = dict(id=str(uuid4()), display_name='Synthetic owner', role='owner', enabled=True,
        max_user_id='9007199254740993', vikunja_user_id='9007199254740995',
        person_profile_id=None, project_ids=['7'], revision=0)
    config = dict(schema=1, project_root=str(root), target_team_database=str(target), owner=owner)
    return root, target, config


def invoke(module, case, *, apply=False, config=None):
    root, _, original = case
    return module.bootstrap_owner(json.dumps(config or original), project_root=root, apply=apply)


def test_preview_creates_no_files_and_never_opens_database(bootstrap, case, monkeypatch):
    monkeypatch.setattr(bootstrap, '_create_database', lambda *a: pytest.fail('Preview opened Team DB'))
    root, target, config = case
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob('*'))
    result = invoke(bootstrap, case)
    assert result == dict(state='preview', code='owner_bootstrap_ready', owner_uuid=config['owner']['id'],
        config_digest=result['config_digest'])
    assert len(result['config_digest']) == 64 and not target.exists()
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob('*')) == before
    assert config['owner']['max_user_id'] not in json.dumps(result)


def test_apply_publishes_complete_actual_schema_and_exact_owner(bootstrap, case, monkeypatch):
    root, target, config = case
    calls = []
    original = TeamRepository.upsert_member
    def record(repository, member, *, expected_revision):
        calls.append((member, expected_revision))
        return original(repository, member, expected_revision=expected_revision)
    monkeypatch.setattr(TeamRepository, 'upsert_member', record)
    result = invoke(bootstrap, case, apply=True)
    assert result['state'] == 'created' and result['code'] == 'owner_bootstrap_created'
    assert len(calls) == 1 and calls[0][1] is None
    member = TeamMember.model_validate(config['owner'])
    assert calls[0][0] == member
    with sqlite3.connect(target) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == SCHEMA_VERSION
        assert conn.execute('SELECT COUNT(*) FROM team_members').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='team_due_resolution_previews'").fetchone()
    assert not any(target.with_name(target.name + suffix).exists() for suffix in ('-wal', '-shm', '-journal'))
    repository = TeamRepository(TeamDatabase(target))
    assert repository.get_member(member.id) == member
    assert repository.resolve_member(member.max_user_id) == member
    assert repository.list_members(member.id, '7') == (member,)
    assert target.stat().st_nlink == 1
    assert not list(target.parent.glob('.owner-bootstrap-*'))


@pytest.mark.parametrize('existing', ['empty', 'foreign', 'team', 'restored', 'directory'])
def test_existing_target_never_opens_or_changes(bootstrap, case, monkeypatch, existing):
    _, target, _ = case
    target.parent.mkdir(parents=True)
    if existing == 'directory':
        target.mkdir()
    elif existing == 'empty':
        target.write_bytes(b'')
    elif existing == 'foreign':
        target.write_bytes(b'SYNTHETIC FOREIGN DATA')
    else:
        TeamDatabase(target)
        if existing == 'restored':
            with sqlite3.connect(target) as conn:
                conn.execute('CREATE TABLE maintenance_restore_guard(id INTEGER PRIMARY KEY,reconciliation_required INTEGER)')
                conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,1)')
    before = target.read_bytes() if target.is_file() else None
    monkeypatch.setattr(bootstrap, '_create_database', lambda *a: pytest.fail('Existing DB was opened'))
    for apply in (False, True):
        with pytest.raises(bootstrap.BootstrapError, match='bootstrap_target_exists'):
            invoke(bootstrap, case, apply=apply)
    assert (target.read_bytes() if target.is_file() else None) == before


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_orphan_sidecar_blocks_new_target(bootstrap, case, suffix):
    _, target, _ = case
    target.parent.mkdir(parents=True)
    sidecar = target.with_name(target.name + suffix)
    sidecar.write_bytes(b'SYNTHETIC EXISTING SIDECAR')
    with pytest.raises(bootstrap.BootstrapError, match='bootstrap_target_exists'):
        invoke(bootstrap, case, apply=True)
    assert not target.exists() and sidecar.read_bytes() == b'SYNTHETIC EXISTING SIDECAR'


def test_target_race_refuses_without_overwriting_foreign_bytes(bootstrap, case, monkeypatch):
    _, target, _ = case
    publish = bootstrap._publish_no_overwrite
    def raced(source, destination):
        destination.write_bytes(b'SYNTHETIC RACE WINNER')
        publish(source, destination)
    monkeypatch.setattr(bootstrap, '_publish_no_overwrite', raced)
    with pytest.raises(bootstrap.BootstrapError, match='bootstrap_target_exists'):
        invoke(bootstrap, case, apply=True)
    assert target.read_bytes() == b'SYNTHETIC RACE WINNER'
    assert not list(target.parent.glob('.owner-bootstrap-*'))


def test_existing_hardlink_refused_without_touching_either_alias(bootstrap, case):
    root, target, _ = case
    target.parent.mkdir(parents=True)
    original = root / 'synthetic-source.bin'
    original.write_bytes(b'SYNTHETIC LINKED CONTENT')
    os.link(original, target)
    with pytest.raises(bootstrap.BootstrapError, match='bootstrap_target_exists'):
        invoke(bootstrap, case, apply=True)
    assert target.read_bytes() == original.read_bytes() == b'SYNTHETIC LINKED CONTENT'
    assert original.stat().st_nlink == 2


def test_symlink_ancestor_cannot_redirect_creation(bootstrap, case):
    root, target, _ = case
    outside = root.parent / 'synthetic-neighbor'
    outside.mkdir()
    try:
        (root / '.runtime').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Host disallows creating synthetic symlinks')
    with pytest.raises(bootstrap.BootstrapError, match='bootstrap_path_unsafe'):
        invoke(bootstrap, case, apply=True)
    assert list(outside.iterdir()) == [] and not target.exists()


@pytest.mark.parametrize('change', ['schema_bool', 'schema_unknown', 'extra', 'role', 'disabled',
    'revision', 'missing_member_field', 'empty_projects', 'duplicate_project', 'numeric_max',
    'numeric_vikunja', 'leading_zero', 'oversized_id', 'root_mismatch', 'outside', 'nested', 'unc'])
def test_invalid_config_refuses_before_writes(bootstrap, case, change):
    root, _, original = case
    config = copy.deepcopy(original)
    if change == 'schema_bool': config['schema'] = True
    elif change == 'schema_unknown': config['schema'] = 2
    elif change == 'extra': config['token'] = 'SYNTHETIC_REJECTED_SECRET_FIELD'
    elif change == 'role': config['owner']['role'] = 'member'
    elif change == 'disabled': config['owner']['enabled'] = False
    elif change == 'revision': config['owner']['revision'] = 1
    elif change == 'missing_member_field': del config['owner']['enabled']
    elif change == 'empty_projects': config['owner']['project_ids'] = []
    elif change == 'duplicate_project': config['owner']['project_ids'] = ['7', '7']
    elif change == 'numeric_max': config['owner']['max_user_id'] = 9007199254740993
    elif change == 'numeric_vikunja': config['owner']['vikunja_user_id'] = 11
    elif change == 'leading_zero': config['owner']['max_user_id'] = '011'
    elif change == 'oversized_id': config['owner']['max_user_id'] = str(2**63)
    elif change == 'root_mismatch': config['project_root'] = str(root.parent)
    elif change == 'outside': config['target_team_database'] = str(root.parent / 'foreign.sqlite3')
    elif change == 'nested': config['target_team_database'] = str(root / '.runtime/team/nested/owner.sqlite3')
    elif change == 'unc': config['target_team_database'] = r'\\synthetic-host\share\owner.sqlite3'
    with pytest.raises(bootstrap.BootstrapError):
        invoke(bootstrap, case, apply=True, config=config)
    assert list(root.iterdir()) == []


def test_duplicate_json_keys_are_rejected_without_writes(bootstrap, case):
    root, _, config = case
    raw = json.dumps(config).replace('"schema": 1', '"schema": 1, "schema": 1')
    with pytest.raises(bootstrap.BootstrapError, match='bootstrap_config_invalid'):
        bootstrap.bootstrap_owner(raw, project_root=root, apply=True)
    assert list(root.iterdir()) == []


def test_failed_private_staging_never_opens_database(bootstrap, case, monkeypatch):
    def rejected(path):
        raise bootstrap.BootstrapError('bootstrap_private_path_failed')
    monkeypatch.setattr(bootstrap, '_create_private_directory', rejected)
    monkeypatch.setattr(bootstrap, '_create_database', lambda *a: pytest.fail('Unprotected DB opened'))
    with pytest.raises(bootstrap.BootstrapError, match='bootstrap_private_path_failed'):
        invoke(bootstrap, case, apply=True)
    assert not case[1].exists()


def test_failed_integrity_never_publishes_and_cleans_only_owned_staging(bootstrap, case, monkeypatch):
    def broken(*args):
        raise bootstrap.BootstrapError('bootstrap_database_invalid')
    monkeypatch.setattr(bootstrap, '_verify_database', broken)
    with pytest.raises(bootstrap.BootstrapError, match='bootstrap_database_invalid'):
        invoke(bootstrap, case, apply=True)
    assert not case[1].exists()
    assert not list(case[1].parent.glob('.owner-bootstrap-*'))


@pytest.mark.parametrize('apply', [False, True], ids=['preview', 'apply'])
def test_cli_child_is_sanitized_and_only_uses_new_synthetic_workspace(bootstrap, case, apply):
    root, _, config = case
    input_path = root / 'bootstrap.json'
    input_path.write_text(json.dumps(config), encoding='utf-8')
    code = ('import importlib.util,pathlib,sys; '
        's=importlib.util.spec_from_file_location("bootstrap_child",sys.argv[1]); '
        'm=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m); '
        'args=["--config",sys.argv[2]]+(["--apply"] if sys.argv[4]=="1" else []); '
        'raise SystemExit(m.main(args,project_root=pathlib.Path(sys.argv[3])))')
    completed = subprocess.run([sys.executable, '-B', '-c', code, str(SCRIPT), str(input_path), str(root), str(int(apply))],
        capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result['state'] == ('created' if apply else 'preview') and result['owner_uuid'] == config['owner']['id']
    assert config['owner']['max_user_id'] not in completed.stdout
    assert completed.stderr == ''
    if apply:
        with sqlite3.connect(case[1]) as conn:
            assert conn.execute('SELECT COUNT(*) FROM team_members').fetchone()[0] == 1
    else:
        assert list(root.iterdir()) == [input_path]


@pytest.mark.parametrize('suffix', ['.sqlite', '.sqlite3'])
def test_canonical_new_database_suffixes_supported(bootstrap, case, suffix):
    root, _, original = case
    config = copy.deepcopy(original)
    target = root / '.runtime/team' / ('team' + suffix)
    config['target_team_database'] = str(target)
    assert invoke(bootstrap, case, apply=True, config=config)['state'] == 'created'
    assert target.is_file()


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_sidecar_appearing_before_publish_preserved_and_prevents_publish(bootstrap, case, monkeypatch, suffix):
    target = case[1]
    verify = bootstrap._verify_database
    def raced(path, owner):
        verify(path, owner)
        target.with_name(target.name + suffix).write_bytes(b'SYNTHETIC SIDECAR RACE')
    monkeypatch.setattr(bootstrap, '_verify_database', raced)
    with pytest.raises(bootstrap.BootstrapError, match='bootstrap_target_exists'):
        invoke(bootstrap, case, apply=True)
    assert not target.exists()
    assert target.with_name(target.name + suffix).read_bytes() == b'SYNTHETIC SIDECAR RACE'
    assert not list(target.parent.glob('.owner-bootstrap-*'))


def test_cleanup_refuses_unexpected_staging_entry_without_recursive_delete(bootstrap, case, monkeypatch):
    stages = []
    def refused(path, owner):
        stages.append(path.parent)
        (path.parent / 'unrecognized.bin').write_bytes(b'SYNTHETIC FOREIGN ENTRY')
        raise bootstrap.BootstrapError('bootstrap_database_invalid')
    monkeypatch.setattr(bootstrap, '_verify_database', refused)
    with pytest.raises(bootstrap.BootstrapError, match='bootstrap_cleanup_refused'):
        invoke(bootstrap, case, apply=True)
    assert not case[1].exists()
    assert (stages[0] / 'unrecognized.bin').read_bytes() == b'SYNTHETIC FOREIGN ENTRY'
    assert (stages[0] / 'team.sqlite3').is_file()


@pytest.mark.skipif(os.name != 'nt', reason='Windows ancestor share-delete contract')
def test_windows_ancestor_cannot_be_renamed_during_database_creation(bootstrap, case, monkeypatch):
    create = bootstrap._create_database
    root = case[0]
    def verify_pinned(path, owner):
        with pytest.raises(OSError):
            (root / '.runtime').rename(root / 'renamed-runtime')
        assert not (root / 'renamed-runtime').exists()
        create(path, owner)
    monkeypatch.setattr(bootstrap, '_create_database', verify_pinned)
    assert invoke(bootstrap, case, apply=True)['state'] == 'created'


@pytest.mark.skipif(os.name != 'nt', reason='Windows current-user protected DACL contract')
def test_windows_private_stage_acl_is_protected_before_first_sqlite_open(bootstrap, case, monkeypatch):
    import ctypes
    from ctypes import wintypes
    create = bootstrap._create_database
    checked = []
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    class ACL(ctypes.Structure):
        _fields_ = [('revision', ctypes.c_ubyte), ('sbz1', ctypes.c_ubyte), ('size', wintypes.WORD),
            ('ace_count', wintypes.WORD), ('sbz2', wintypes.WORD)]
    def verify_private(path, owner):
        descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            assert advapi.GetNamedSecurityInfoW(str(path.parent), 1, 4, None, None,
                ctypes.byref(dacl), None, ctypes.byref(descriptor)) == 0
            control, revision = wintypes.WORD(), wintypes.DWORD()
            assert advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
            assert control.value & 0x1000  # SE_DACL_PROTECTED: no inherited broad grants.
            assert dacl and ctypes.cast(dacl, ctypes.POINTER(ACL)).contents.ace_count == 1
            checked.append(True)
        finally:
            if descriptor: kernel.LocalFree(descriptor)
        create(path, owner)
    monkeypatch.setattr(bootstrap, '_create_database', verify_private)
    assert invoke(bootstrap, case, apply=True)['state'] == 'created'
    assert checked == [True]


def test_cli_invalid_arguments_do_not_echo_raw_values(bootstrap, case, capsys):
    secret = 'SYNTHETIC_REJECTED_ARG_NOT_A_REAL_SECRET'
    assert bootstrap.main(['--token', secret], project_root=case[0]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {'state': 'rejected', 'code': 'bootstrap_arguments_invalid'}
    assert secret not in captured.out and captured.err == ''


@pytest.mark.skipif(os.name != 'nt', reason='Windows stage share-delete contract')
def test_windows_stage_cannot_be_replaced_before_repository_write(bootstrap, case, monkeypatch):
    create = bootstrap._create_database
    def pinned_stage(path, owner):
        with pytest.raises(OSError):
            path.parent.rename(path.parent.with_name('synthetic-replaced-stage'))
        assert path.exists()
        create(path, owner)
    monkeypatch.setattr(bootstrap, '_create_database', pinned_stage)
    assert invoke(bootstrap, case, apply=True)['state'] == 'created'


@pytest.mark.skipif(os.name != 'nt', reason='Windows cleanup ancestor share-delete contract')
def test_windows_cleanup_holds_parent_pins_until_owned_unlinks_finish(bootstrap, case, monkeypatch):
    root, target, _ = case
    foreign = root.parent / 'synthetic-cleanup-neighbor'
    foreign.mkdir()
    foreign_file = foreign / 'team.sqlite3'
    foreign_file.write_bytes(b'SYNTHETIC FOREIGN CLEANUP BYTES')
    original = Path.iterdir
    checks = []
    def guarded_iteration(path):
        if path.name.startswith('.owner-bootstrap-'):
            with pytest.raises(OSError):
                target.parent.rename(root / 'redirected-team')
            checks.append(True)
        return original(path)
    monkeypatch.setattr(Path, 'iterdir', guarded_iteration)
    assert invoke(bootstrap, case, apply=True)['state'] == 'created'
    assert checks == [True]
    assert foreign_file.read_bytes() == b'SYNTHETIC FOREIGN CLEANUP BYTES'
    assert not (root / 'redirected-team').exists()


def test_successful_publish_with_cleanup_failure_reports_created_honestly(bootstrap, case, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError('SYNTHETIC CLEANUP FAILURE')
    monkeypatch.setattr(bootstrap, '_cleanup_owned', unavailable)
    result = invoke(bootstrap, case, apply=True)
    assert result['state'] == 'created' and result['code'] == 'owner_bootstrap_created_cleanup_required'
    with sqlite3.connect(case[1]) as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_members').fetchone()[0] == 1
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.skipif(os.name != 'nt', reason='Windows existing shared DACL refusal')
def test_existing_shared_team_directory_is_not_repaired_or_used(bootstrap, case):
    import ctypes
    from ctypes import wintypes
    root, target, _ = case
    bootstrap._create_private_directory(root / '.runtime')
    bootstrap._create_private_directory(target.parent)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    try:
        assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            'D:P(A;OICI;FA;;;WD)', 1, ctypes.byref(descriptor), None)
        assert advapi.SetFileSecurityW(str(target.parent), 0x80000004, descriptor)
    finally:
        if descriptor: kernel.LocalFree(descriptor)
    preserved = target.parent / 'unrelated.bin'
    preserved.write_bytes(b'SYNTHETIC EXISTING DIRECTORY CONTENT')
    for apply in (False, True):
        with pytest.raises(bootstrap.BootstrapError, match='bootstrap_private_path_failed'):
            invoke(bootstrap, case, apply=apply)
    assert not target.exists() and preserved.read_bytes() == b'SYNTHETIC EXISTING DIRECTORY CONTENT'
    assert list(target.parent.iterdir()) == [preserved]


@pytest.mark.skipif(os.name != 'nt', reason='Windows immutable source leaf pin')
def test_verified_source_leaf_cannot_be_replaced_or_written_at_atomic_link(bootstrap, case, monkeypatch):
    link = bootstrap.os.link
    def substitution(source, target):
        with pytest.raises(OSError):
            Path(source).unlink()
        with pytest.raises(OSError):
            Path(source).write_bytes(b'SYNTHETIC FOREIGN SOURCE')
        link(source, target)
    monkeypatch.setattr(bootstrap.os, 'link', substitution)
    assert invoke(bootstrap, case, apply=True)['state'] == 'created'
    with sqlite3.connect(case[1]) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('SELECT COUNT(*) FROM team_members').fetchone()[0] == 1


def test_cleanup_preserves_foreign_substituted_alias_and_reports_created(bootstrap, case, monkeypatch):
    cleanup = bootstrap._cleanup_owned
    aliases = []
    def substituted(stage, identity, **kwargs):
        alias = stage / 'team.sqlite3'
        alias.unlink()
        alias.write_bytes(b'SYNTHETIC FOREIGN CLEANUP ALIAS')
        aliases.append(alias)
        return cleanup(stage, identity, **kwargs)
    monkeypatch.setattr(bootstrap, '_cleanup_owned', substituted)
    result = invoke(bootstrap, case, apply=True)
    assert result['state'] == 'created' and result['code'] == 'owner_bootstrap_created_cleanup_required'
    assert aliases[0].read_bytes() == b'SYNTHETIC FOREIGN CLEANUP ALIAS'
    with sqlite3.connect(case[1]) as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_members').fetchone()[0] == 1


def test_owned_leaf_cleanup_does_not_use_path_unlink_after_identity_scan(bootstrap, case, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Cleanup used replaceable filename instead of owned handle')
    monkeypatch.setattr(Path, 'unlink', forbidden)
    result = invoke(bootstrap, case, apply=True)
    assert result['code'] == 'owner_bootstrap_created' and case[1].stat().st_nlink == 1


def test_corruption_between_checkpoint_and_file_pin_is_not_published(bootstrap, case, monkeypatch):
    verify = bootstrap._verify_database
    def corrupt(path, owner):
        verify(path, owner)
        path.write_bytes(b'SYNTHETIC CORRUPT STAGED BYTES')
    monkeypatch.setattr(bootstrap, '_verify_database', corrupt)
    with pytest.raises(bootstrap.BootstrapError):
        invoke(bootstrap, case, apply=True)
    assert not case[1].exists()
