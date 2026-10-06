"""Independent source-epoch proposal stores; synthetic Windows-private files only."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from importlib import import_module, util
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid4

import pytest


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows custody contract')
ROLES = ('secretary', 'team', 'billing', 'vikunja')


def h(value):
    return sha256(value.encode('ascii')).hexdigest()


def api():
    name = 'secretary.infrastructure.restore_source_preparation_control'
    assert util.find_spec(name) is not None, 'Independent source preparation control is missing'
    return import_module(name)


@pytest.fixture
def private(tmp_path):
    directory = tmp_path / 'private'
    import_module('secretary.infrastructure.restore_operator').create_private_directory(directory)
    return directory


def binding(directory):
    context = dict(protocol=1, restore_id=str(uuid4()), r3_id=str(uuid4()),
        **{key: h(key) for key in ('r3_decision_sha256', 'r3_binding_sha256',
            'manifest_sha256', 'metadata_sha256', 'inventory_sha256',
            'source_context_sha256', 'scope_sha256', 'runtime_evidence_sha256')},
        sources=tuple(dict(role=role, path=str(directory / (role + '.sqlite3')),
            file_id=(1, index + 1), baseline_file_sha256=h(role + ':baseline'),
            preparation_sha256=h(role + ':r3'), source_commitment_sha256=h(role + ':context'))
            for index, role in enumerate(ROLES)))
    return dict(protocol=1, context=context, ledger_id=str(uuid4()),
        generation_id=str(uuid4()), epoch_id=str(uuid4()),
        runtime_deployment_id=str(uuid4()), native_baseline=dict(
            schema_sha256=h('native schema'), migration_ids=('20230101', '20230202'),
            rowids_sha256=h('native rowids')),
        capabilities=('fresh_auth', 'fresh_work', 'native_runtime', 'sql_write'))


def create(private):
    port, bound, path = api(), binding(private), private / 'source-epochs.sqlite3'
    return port, path, bound, port.SourcePreparationControl.create(path, bound)


def approval(*, expires_at=None, nonce='fresh operator nonce'):
    port = import_module('secretary.infrastructure.restore_operator')
    return port.VerifiedOperatorApproval(h('synthetic operator'), h('synthetic proposal approval:' + nonce),
        h(nonce), int(time.time()) + 120 if expires_at is None else expires_at)


def approved(private):
    port, path, bound, control = create(private)
    verified = approval()
    control.consume_approval(verified)
    return port, path, bound, control, verified


def file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def native_head(bound):
    return dict(epoch_id=bound['epoch_id'], sequence=0, nonce=None, digest=h('initial native head'))


def test_missing_reader_creates_neither_store_nor_parent(private):
    port = api()
    for path in (private / 'missing.sqlite3', private / 'absent' / 'missing.sqlite3'):
        with pytest.raises(port.SourcePreparationControlError):
            port.SourcePreparationControl.open_existing(path)
        assert not path.exists()
    assert not (private / 'absent').exists()


def test_one_proposal_reopens_with_authoritative_binding_without_creation(private):
    port, path, bound, control = create(private)
    snapshot = control.snapshot()
    assert str(UUID(snapshot['preparation_id'])) == snapshot['preparation_id']
    assert snapshot['binding'] == json.loads(json.dumps(bound))
    assert snapshot['state'] == 'partial_prepared_blocked'
    assert snapshot['preparations'] == {} and snapshot['approval'] is None and snapshot['approvals'] == ()
    assert snapshot['missing_source_roles'] == ROLES
    assert snapshot['activation_supported'] is False and snapshot['outbound_enabled'] is False
    before = file_hash(path)
    assert port.SourcePreparationControl.open_existing(path).snapshot() == snapshot
    assert port.SourcePreparationControl.open_existing(path, bound).snapshot() == snapshot
    with pytest.raises(port.SourcePreparationControlError):
        port.SourcePreparationControl.create(path, bound)
    assert file_hash(path) == before


@pytest.mark.parametrize('change', ('extra', 'protocol_bool', 'bad_id', 'capability_order',
    'raw_intake', 'context_extra', 'context_hash', 'source_missing', 'source_duplicate',
    'source_extra', 'source_bool_id', 'source_path_relative', 'source_path_remote',
    'deployment_uuid', 'native_baseline_extra', 'native_schema_hash', 'native_ids_order',
    'native_ids_duplicate', 'native_ids_empty', 'native_rowids_hash'))
def test_invalid_closed_binding_is_rejected_before_file_creation(private, change):
    port, bound, path = api(), binding(private), private / 'bad.sqlite3'
    if change == 'extra': bound['grant'] = True
    elif change == 'protocol_bool': bound['protocol'] = True
    elif change == 'bad_id': bound['generation_id'] = 'not-a-uuid'
    elif change == 'capability_order': bound['capabilities'] = ('sql_write', 'fresh_work')
    elif change == 'raw_intake': bound['capabilities'] = ('max_raw_intake',)
    elif change == 'context_extra': bound['context']['consent'] = True
    elif change == 'context_hash': bound['context']['r3_binding_sha256'] = 'A' * 64
    elif change == 'source_missing': bound['context']['sources'] = bound['context']['sources'][:3]
    elif change == 'source_duplicate': bound['context']['sources'][1]['role'] = 'secretary'
    elif change == 'source_extra': bound['context']['sources'][0]['enabled'] = True
    elif change == 'source_bool_id': bound['context']['sources'][0]['file_id'] = (True, 1)
    elif change == 'source_path_relative': bound['context']['sources'][0]['path'] = 'relative.sqlite3'
    elif change == 'source_path_remote': bound['context']['sources'][0]['path'] = r'/\synthetic-server\share\source.sqlite3'
    elif change == 'deployment_uuid': bound['runtime_deployment_id'] = True
    elif change == 'native_baseline_extra': bound['native_baseline']['grant'] = True
    elif change == 'native_schema_hash': bound['native_baseline']['schema_sha256'] = 'x' * 64
    elif change == 'native_ids_order': bound['native_baseline']['migration_ids'] = ('z', 'a')
    elif change == 'native_ids_duplicate': bound['native_baseline']['migration_ids'] = ('a', 'a')
    elif change == 'native_ids_empty': bound['native_baseline']['migration_ids'] = ()
    elif change == 'native_rowids_hash': bound['native_baseline']['rowids_sha256'] = 0
    with pytest.raises(port.SourcePreparationControlError) as error:
        port.SourcePreparationControl.create(path, bound)
    assert error.value.code == 'source_preparation_binding_invalid'
    assert not path.exists()


def test_context_dictionary_caller_mutation_does_not_rebind_store(private):
    port, path, bound, control = create(private)
    original = control.snapshot()
    bound['context']['sources'][0]['baseline_file_sha256'] = h('mutated')
    returned = original['binding']
    returned['context']['sources'][1]['file_id'][0] = 900
    assert control.snapshot()['binding']['context']['sources'][0]['baseline_file_sha256'] == h('secretary:baseline')
    assert control.snapshot()['binding']['context']['sources'][1]['file_id'] == [1, 2]
    with pytest.raises(port.SourcePreparationControlError):
        port.SourcePreparationControl.open_existing(path, bound)


@pytest.mark.parametrize('key', ('epoch_id', 'generation_id', 'ledger_id', 'runtime_deployment_id', 'context', 'native_baseline'))
def test_existing_proposal_cannot_be_rebound_to_new_epoch_or_sources(private, key):
    port, path, bound, control = create(private)
    foreign = deepcopy(bound)
    if key == 'context': foreign[key]['source_context_sha256'] = h('different source context')
    elif key == 'native_baseline': foreign[key]['rowids_sha256'] = h('different rowids')
    else: foreign[key] = str(uuid4())
    before = file_hash(path)
    with pytest.raises(port.SourcePreparationControlError):
        port.SourcePreparationControl.open_existing(path, foreign)
    assert file_hash(path) == before and control.snapshot()['preparations'] == {}


def test_approvals_append_immutably_and_exact_replay_never_changes_them(private):
    port, path, _, control = create(private)
    verified = approval()
    control.consume_approval(verified)
    stored = control.snapshot()['approval']
    assert stored == dict(operator_digest=verified.operator_digest,
        approval_digest=verified.approval_digest, nonce_hash=verified.nonce_hash,
        expires_at=verified.expires_at)
    before = file_hash(path)
    control.consume_approval(verified)
    assert file_hash(path) == before
    next_approval = approval(nonce='second nonce')
    control.consume_approval(next_approval)
    assert len(control.snapshot()['approvals']) == 2
    assert control.snapshot()['approvals'][0] == stored
    altered = approval(nonce='second nonce')
    object.__setattr__(altered, 'expires_at', next_approval.expires_at + 1)
    before = file_hash(path)
    with pytest.raises(port.SourcePreparationControlError) as error:
        control.consume_approval(altered)
    assert error.value.code == 'source_preparation_approval_conflict'
    assert file_hash(path) == before


@pytest.mark.parametrize('kind', ('dict', 'expired', 'future', 'bool_expiry'))
def test_unverified_or_stale_operator_receipt_cannot_authorize_preparation(private, kind):
    port, path, _, control = create(private)
    if kind == 'dict': incoming = dict(operator_digest=h('operator'), approved=True)
    else:
        incoming = approval(expires_at=int(time.time()) - 1 if kind == 'expired' else int(time.time()) + 301)
        if kind == 'bool_expiry': object.__setattr__(incoming, 'expires_at', True)
    before = file_hash(path)
    with pytest.raises(port.SourcePreparationControlError):
        control.consume_approval(incoming)
    assert file_hash(path) == before and control.snapshot()['approval'] is None


def test_preparation_requires_approval_and_exact_repeat_resumes_same_proposal(private):
    port, path, bound, control = create(private)
    with pytest.raises(port.SourcePreparationControlError):
        control.record_preparation('secretary', h('receipt'), h('file'))
    control.consume_approval(approval())
    original_id = control.snapshot()['preparation_id']
    for role in ROLES[:2]: control.record_preparation(role, h(role + ':receipt'), h(role + ':file'))
    reopened = port.SourcePreparationControl.open_existing(path, bound)
    assert reopened.snapshot()['preparation_id'] == original_id
    before = file_hash(path)
    reopened.record_preparation('secretary', h('secretary:receipt'), h('secretary:file'))
    assert file_hash(path) == before
    assert set(reopened.snapshot()['preparations']) == {'secretary', 'team'}


def test_expiry_is_rechecked_after_acquiring_write_lock(private, monkeypatch):
    port, path, _, control = create(private)
    verified = approval(expires_at=int(time.time()) + 1)
    original = control._transaction
    from contextlib import contextmanager
    @contextmanager
    def expire_inside():
        with original() as values:
            monkeypatch.setattr(port.time, 'time', lambda: verified.expires_at)
            yield values
    monkeypatch.setattr(control, '_transaction', expire_inside)
    with pytest.raises(port.SourcePreparationControlError):
        control.consume_approval(verified)
    assert control.snapshot()['approval'] is None


def test_four_receipts_remain_blocked_and_native_initial_head_is_retained(private):
    port, path, bound, control, _ = approved(private)
    for role in ROLES:
        control.record_preparation(role, h(role + ':receipt'), h(role + ':file'),
            native_head=native_head(bound) if role == 'vikunja' else None)
    snapshot = control.snapshot()
    assert snapshot['state'] == 'all_prepared_blocked'
    assert snapshot['missing_source_roles'] == ()
    assert snapshot['preparations']['vikunja']['native_head'] == native_head(bound)
    assert snapshot['activation_supported'] is False and snapshot['outbound_enabled'] is False
    assert not hasattr(control, 'activate') and not hasattr(control, 'complete')
    before = file_hash(path)
    with pytest.raises(port.SourcePreparationControlError):
        control.record_preparation('secretary', h('different receipt'), h('secretary:file'))
    assert file_hash(path) == before


@pytest.mark.parametrize('kind', ('role', 'receipt_hash', 'file_hash', 'native_missing',
    'native_foreign_epoch', 'native_extra', 'native_bool_seq', 'native_nonzero_seq',
    'native_initial_nonce', 'non_native_head'))
def test_malformed_or_foreign_source_receipt_is_refused(private, kind):
    port, path, bound, control, _ = approved(private)
    role, receipt_hash, payload_hash, head = 'secretary', h('receipt'), h('file'), None
    if kind == 'role': role = 'foreign'
    elif kind == 'receipt_hash': receipt_hash = 'x' * 64
    elif kind == 'file_hash': payload_hash = True
    elif kind == 'non_native_head': head = native_head(bound)
    else:
        role, head = 'vikunja', native_head(bound)
        if kind == 'native_missing': head = None
        elif kind == 'native_foreign_epoch': head['epoch_id'] = str(uuid4())
        elif kind == 'native_extra': head['approved'] = True
        elif kind == 'native_bool_seq': head['sequence'] = False
        elif kind == 'native_nonzero_seq': head['sequence'] = 1
        elif kind == 'native_initial_nonce': head['nonce'] = h('nonce')
    before = file_hash(path)
    with pytest.raises(port.SourcePreparationControlError):
        control.record_preparation(role, receipt_hash, payload_hash, native_head=head)
    assert file_hash(path) == before and control.snapshot()['preparations'] == {}


def test_old_approval_allows_readbacks_and_exact_retry_but_not_new_source_writes(private, monkeypatch):
    port, path, bound, control, verified = approved(private)
    control.record_preparation('secretary', h('receipt'), h('file'))
    monkeypatch.setattr(port.time, 'time', lambda: verified.expires_at)
    before = file_hash(path)
    assert port.SourcePreparationControl.open_existing(path).snapshot()['approval'] is not None
    control.record_preparation('secretary', h('receipt'), h('file'))
    with pytest.raises(port.SourcePreparationControlError):
        control.record_preparation('team', h('receipt'), h('file'))
    assert file_hash(path) == before


def test_new_authenticated_approval_resumes_partial_preparation_after_expiry(private, monkeypatch):
    port, path, bound, control, verified = approved(private)
    control.record_preparation('secretary', h('receipt'), h('file'))
    monkeypatch.setattr(port.time, 'time', lambda: verified.expires_at)
    fresh = approval(expires_at=verified.expires_at + 120, nonce='fresh resume approval')
    resumed = port.SourcePreparationControl.open_existing(path, bound)
    resumed.consume_approval(fresh)
    resumed.record_preparation('team', h('receipt'), h('file'))
    snapshot = resumed.snapshot()
    assert len(snapshot['approvals']) == 2
    assert snapshot['approval']['nonce_hash'] == fresh.nonce_hash
    assert set(snapshot['preparations']) == {'secretary', 'team'}


def test_crash_before_second_commit_rolls_back_and_resumes_same_first_receipt(private, monkeypatch):
    port, path, bound, control, _ = approved(private)
    control.record_preparation('secretary', h('secretary:receipt'), h('secretary:file'))
    original = control._validate
    seen = []
    def die_after_insert(conn):
        snapshot = original(conn)
        if 'team' in snapshot['preparations']:
            seen.append(True)
            raise RuntimeError('synthetic crash before durable commit')
        return snapshot
    with monkeypatch.context() as patch:
        patch.setattr(control, '_validate', die_after_insert)
        with pytest.raises(RuntimeError):
            control.record_preparation('team', h('team:receipt'), h('team:file'))
    assert seen
    resumed = port.SourcePreparationControl.open_existing(path, bound)
    assert set(resumed.snapshot()['preparations']) == {'secretary'}
    resumed.record_preparation('team', h('team:receipt'), h('team:file'))
    assert set(resumed.snapshot()['preparations']) == {'secretary', 'team'}


def test_all_business_rows_block_update_delete_and_replace_without_recursive_trigger_setting(private):
    port, path, bound, control, _ = approved(private)
    for role in ROLES:
        control.record_preparation(role, h(role + ':receipt'), h(role + ':file'),
            native_head=native_head(bound) if role == 'vikunja' else None)
    before = file_hash(path)
    with sqlite3.connect(path) as conn:
        tables = ('source_proposal', 'source_operator_approval', 'source_preparations')
        for table in tables:
            for sql in (f'DELETE FROM {table}', f'UPDATE {table} SET rowid=rowid',
                f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}',
                f'REPLACE INTO {table} SELECT * FROM {table}'):
                with pytest.raises(sqlite3.IntegrityError): conn.execute(sql)
                conn.rollback()
    assert file_hash(path) == before and control.snapshot()['state'] == 'all_prepared_blocked'


@pytest.mark.parametrize('kind', ('extra', 'drop', 'changed'))
def test_schema_tamper_is_refused_without_repair(private, kind):
    port, path, _, control = create(private)
    with sqlite3.connect(path) as conn:
        if kind == 'extra': conn.execute('CREATE TABLE extra_evidence(payload TEXT)')
        else:
            name, table = conn.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone()
            conn.execute(f'DROP TRIGGER "{name}"')
            if kind == 'changed': conn.execute(f'CREATE TRIGGER "{name}" BEFORE DELETE ON "{table}" BEGIN SELECT 1; END')
    before = file_hash(path)
    with pytest.raises(port.SourcePreparationControlError): control.snapshot()
    with pytest.raises(port.SourcePreparationControlError): port.SourcePreparationControl.open_existing(path)
    assert file_hash(path) == before


def test_empty_partial_and_foreign_files_are_never_adopted(private):
    port = api()
    for index, kind in enumerate(('empty', 'partial', 'foreign')):
        path = private / f'{index}.sqlite3'
        if kind == 'empty': path.write_bytes(b'')
        else:
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE source_proposal(singleton INTEGER)')
                if kind == 'foreign': conn.execute('CREATE TABLE foreign_data(secret TEXT)')
        before = file_hash(path)
        with pytest.raises(port.SourcePreparationControlError): port.SourcePreparationControl.create(path, binding(private))
        with pytest.raises(port.SourcePreparationControlError): port.SourcePreparationControl.open_existing(path)
        assert file_hash(path) == before


def test_child_descriptor_prevents_actual_windows_substitution_during_sqlite_io(private, monkeypatch):
    port, path, _, control = create(private)
    foreign = private / 'foreign.sqlite3'
    foreign.write_bytes(path.read_bytes())
    before, original, attempts = file_hash(foreign), port.sqlite3.connect, []
    def attempted_substitution(database, *args, **kwargs):
        if str(database).startswith(path.as_uri()):
            with pytest.raises(PermissionError): path.unlink()
            with pytest.raises(PermissionError): os.replace(foreign, path)
            attempts.append(True)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(port.sqlite3, 'connect', attempted_substitution)
    control.consume_approval(approval())
    assert attempts and file_hash(foreign) == before
    assert control.snapshot()['approval'] is not None


def test_exclusive_creation_descriptor_prevents_actual_windows_substitution_before_commit(private, monkeypatch):
    port = api()
    path, foreign = private / 'source.sqlite3', private / 'foreign-empty.sqlite3'
    foreign.write_bytes(b'')
    original, attempts = port.sqlite3.connect, []
    def attempted_substitution(database, *args, **kwargs):
        if str(database) == path.as_uri() + '?mode=rw':
            with pytest.raises(PermissionError): path.unlink()
            with pytest.raises(PermissionError): os.replace(foreign, path)
            attempts.append(True)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(port.sqlite3, 'connect', attempted_substitution)
    control = port.SourcePreparationControl.create(path, binding(private))
    assert attempts and foreign.read_bytes() == b'' and control.snapshot()['preparations'] == {}


def test_sidecar_journals_and_hardlinks_are_preserved_and_never_recovered(private):
    port, path, _, control = create(private)
    before = file_hash(path)
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        sidecar.write_bytes(b'synthetic incomplete journal')
        with pytest.raises(port.SourcePreparationControlError): control.snapshot()
        assert sidecar.read_bytes() == b'synthetic incomplete journal' and file_hash(path) == before
        sidecar.unlink()
    linked = private / 'linked.sqlite3'
    os.link(path, linked)
    with pytest.raises(port.SourcePreparationControlError): control.snapshot()
    assert file_hash(linked) == before


def test_parent_requires_private_existing_local_directory(private, tmp_path):
    port = api()
    for path in (private / 'absent' / 'store.sqlite3', tmp_path / 'unprotected.sqlite3', Path('relative.sqlite3')):
        with pytest.raises(port.SourcePreparationControlError): port.SourcePreparationControl.create(path, binding(private))
    assert not (private / 'absent').exists() and not (tmp_path / 'unprotected.sqlite3').exists()


def test_locked_writer_fails_with_bounded_sanitized_code_without_consumption(private):
    port, path, _, control = create(private)
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        with pytest.raises(port.SourcePreparationControlError) as error: control.consume_approval(approval())
        assert str(error.value) == error.value.code and time.monotonic() - started < 2
        conn.rollback()
    assert control.snapshot()['approval'] is None


def test_sanitized_error_never_echoes_unrecognized_caller_text(private):
    port = api()
    error = port.SourcePreparationControlError('SYNTHETIC_PRIVATE_DO_NOT_LOG')
    assert error.code == 'source_preparation_store_invalid'
    assert str(error) == error.code


def test_none_creation_binding_is_refused_before_any_file(private):
    port, path = api(), private / 'empty-binding.sqlite3'
    with pytest.raises(port.SourcePreparationControlError):
        port.SourcePreparationControl.create(path, None)
    assert not path.exists()


@pytest.mark.parametrize('bad_code', ([], {}, None, True))
def test_non_string_error_codes_are_sanitized_without_formatting_caller_objects(private, bad_code):
    error = api().SourcePreparationControlError(bad_code)
    assert str(error) == 'source_preparation_store_invalid'


@pytest.mark.parametrize('device', ('CON', 'CON.sqlite3', 'NUL.sqlite3', 'COM1.sqlite3'))
def test_reserved_windows_source_paths_are_rejected_before_store_creation(private, device):
    port, bound, path = api(), binding(private), private / 'bad-device.sqlite3'
    bound['context']['sources'][0]['path'] = str(private / device)
    with pytest.raises(port.SourcePreparationControlError):
        port.SourcePreparationControl.create(path, bound)
    assert not path.exists()


def test_approval_append_bound_preserves_prior_approvals_and_receipts(private):
    port, path, _, control, _ = approved(private)
    control.record_preparation('secretary', h('receipt'), h('file'))
    for index in range(1, 64): control.consume_approval(approval(nonce=f'approval-{index}'))
    snapshot, before = control.snapshot(), file_hash(path)
    with pytest.raises(port.SourcePreparationControlError) as error:
        control.consume_approval(approval(nonce='approval-65'))
    assert error.value.code == 'source_preparation_approval_limit'
    assert control.snapshot() == snapshot and file_hash(path) == before


def test_foreign_operator_cannot_extend_existing_proposal_approval_history(private):
    port, path, _, control, _ = approved(private)
    foreign = approval(nonce='foreign nonce')
    object.__setattr__(foreign, 'operator_digest', h('foreign operator'))
    before = file_hash(path)
    with pytest.raises(port.SourcePreparationControlError): control.consume_approval(foreign)
    assert file_hash(path) == before and len(control.snapshot()['approvals']) == 1


def test_failed_initialization_keeps_partial_file_unadopted(private, monkeypatch):
    port, path, bound = api(), private / 'failed.sqlite3', binding(private)
    with monkeypatch.context() as patch:
        patch.setattr(port, '_DDL', (*port._DDL, 'INVALID SYNTHETIC SQL'))
        with pytest.raises(port.SourcePreparationControlError):
            port.SourcePreparationControl.create(path, bound)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT name FROM sqlite_master').fetchall() == []
    before = file_hash(path)
    with pytest.raises(port.SourcePreparationControlError):
        port.SourcePreparationControl.create(path, bound)
    with pytest.raises(port.SourcePreparationControlError):
        port.SourcePreparationControl.open_existing(path)
    assert file_hash(path) == before


def test_long_companion_public_path_creates_and_resumes_same_proposal_with_no_delete_lease(private, monkeypatch):
    # Exact new integration shape naturally exceeds MAX_PATH; shortening this
    # fixture would conceal the actual source-epochs companion failure.
    import ctypes
    from ctypes import wintypes
    port, bound = api(), binding(private)
    directory = private
    segments = ('.runtime', 'team-operator', 'activations', bound['context']['restore_id'],
        'source-epochs', bound['epoch_id'], 'synthetic-companion-custody-segment-' + 'x' * 80)
    for segment in segments:
        directory = directory / segment
        import_module('secretary.infrastructure.restore_operator').create_private_directory(directory)
    path = directory / 'source-epochs.sqlite3'
    assert len(str(path)) > 330
    original, attempts = port.sqlite3.connect, []
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.DeleteFileW.argtypes = [wintypes.LPCWSTR]
    def attempted_substitution(database, *args, **kwargs):
        if str(database).split('?', 1)[0].endswith('source-epochs.sqlite3'):
            assert not kernel.DeleteFileW('\\\\?\\' + str(path))
            assert ctypes.get_last_error() in (5, 32)
            attempts.append(True)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(port.sqlite3, 'connect', attempted_substitution)
    control = port.SourcePreparationControl.create(path, bound)
    assert control._path == path and not str(control._path).startswith('\\\\?\\')
    control.consume_approval(approval())
    control.record_preparation('secretary', h('long receipt'), h('long file'))
    reopened = port.SourcePreparationControl.open_existing(path, bound)
    assert attempts and reopened.snapshot() == control.snapshot()
    assert set(reopened.snapshot()['preparations']) == {'secretary'}
