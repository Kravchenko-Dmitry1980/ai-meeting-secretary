"""Offline R3 control protocol tests; only synthetic private SQLite files."""
from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from importlib import import_module
from pathlib import Path
import json
import os
import sqlite3
import time
from uuid import UUID, uuid4

import pytest


def h(value):
    return sha256(value.encode()).hexdigest()


def binding():
    return {'protocol': 1, 'restore_id': str(uuid4()), **{key: h(key) for key in (
        'plan_sha256', 'input_sha256', 'scope_sha256', 'evidence_sha256',
        'source_paths_sha256', 'operator_authority_sha256')}}


def api():
    return import_module('secretary.infrastructure.restore_reconciliation_control')


def approval(bound, *, nonce='nonce', digest='approval', expires=None):
    dto = import_module('secretary.infrastructure.restore_operator').VerifiedOperatorApproval
    return dto(operator_digest=bound['operator_authority_sha256'], approval_digest=h(digest),
               nonce_hash=h(nonce), expires_at=expires or int(time.time()) + 120)


def create(tmp_path):
    port = api()
    bound = binding()
    path = tmp_path / 'control.sqlite3'
    return port, path, bound, port.RestoreControl(path, bound)


def prepare(control):
    for role in ('secretary', 'team', 'billing'):
        control.record_preparation(role, h(role + ':receipt'), h(role + ':file'))


def file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def test_new_control_exclusively_creates_stable_hold_and_reopens(tmp_path):
    port, path, bound, control = create(tmp_path)
    assert str(UUID(control.r3_id)) == control.r3_id
    before = file_hash(path)
    reopened = port.RestoreControl(path, dict(bound))
    assert reopened.r3_id == control.r3_id and file_hash(path) == before
    snapshot = reopened.snapshot()
    assert snapshot['state'] == 'restore_hold' and snapshot['binding'] == bound
    assert snapshot['preparations'] == {} and snapshot['approval_digest'] is None
    assert snapshot['decision'] is None and snapshot['activation_supported'] is False
    assert snapshot['outbound'] is False and snapshot['outbound_enabled'] is False
    assert not Path(str(path) + '-wal').exists() and not Path(str(path) + '-shm').exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    snapshot['binding']['plan_sha256'] = h('mutated copy')
    assert control.snapshot()['binding'] == bound


@pytest.mark.parametrize('edit', [
    {'protocol': True}, {'protocol': 2}, {'restore_id': 'not-uuid'},
    {'plan_sha256': 'A' * 64}, {'input_sha256': '0' * 63}, {'extra': 'no'},
])
def test_invalid_binding_does_not_create_any_file(tmp_path, edit):
    port = api()
    bound = {**binding(), **edit}
    path = tmp_path / 'absent.sqlite3'
    with pytest.raises(port.ControlError):
        port.RestoreControl(path, bound)
    assert not path.exists()


def test_missing_binding_key_and_missing_parent_are_rejected_without_mkdir(tmp_path):
    port = api()
    bound = binding()
    del bound['scope_sha256']
    with pytest.raises(port.ControlError):
        port.RestoreControl(tmp_path / 'none.sqlite3', bound)
    absent = tmp_path / 'absent-parent'
    with pytest.raises(port.ControlError):
        port.RestoreControl(absent / 'none.sqlite3', binding())
    assert not absent.exists()


@pytest.mark.parametrize('kind', ['empty', 'foreign', 'partial'])
def test_existing_non_control_store_is_refused_read_only(tmp_path, kind):
    port = api()
    path = tmp_path / 'unrelated.sqlite3'
    if kind == 'empty':
        path.write_bytes(b'')
    else:
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE owner_evidence(payload TEXT)')
            conn.execute('INSERT INTO owner_evidence VALUES(?)', ('SYNTHETIC_PRIVATE_NOT_FOR_ERRORS',))
            if kind == 'partial':
                conn.execute('CREATE TABLE r3_hold(singleton INTEGER PRIMARY KEY)')
    before = file_hash(path)
    with pytest.raises(port.ControlError) as caught:
        port.RestoreControl(path, binding())
    assert str(caught.value) == caught.value.code
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value) and file_hash(path) == before


def test_changed_binding_is_rejected_read_only(tmp_path):
    port, path, bound, control = create(tmp_path)
    before = file_hash(path)
    with pytest.raises(port.ControlError):
        port.RestoreControl(path, {**bound, 'evidence_sha256': h('changed')})
    assert file_hash(path) == before and control.snapshot()['state'] == 'restore_hold'


@pytest.mark.parametrize('tamper', ['drop_trigger', 'alter_trigger', 'extra_table', 'extra_view', 'drop_table'])
def test_missing_altered_or_extra_schema_objects_are_rejected_read_only(tmp_path, tamper):
    port, path, bound, control = create(tmp_path)
    with sqlite3.connect(path) as conn:
        trigger, table = conn.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger' ORDER BY name LIMIT 1").fetchone()
        if tamper in ('drop_trigger', 'alter_trigger'):
            conn.execute(f'DROP TRIGGER "{trigger}"')
            if tamper == 'alter_trigger':
                conn.execute(f'CREATE TRIGGER "{trigger}" BEFORE UPDATE ON "{table}" BEGIN SELECT 1; END')
        elif tamper == 'extra_table':
            conn.execute('CREATE TABLE external_table(payload TEXT)')
        elif tamper == 'extra_view':
            conn.execute('CREATE VIEW external_view AS SELECT 1 AS value')
        else:
            conn.execute('DROP TABLE r3_decision')
    before = file_hash(path)
    for operation in (lambda: port.RestoreControl(path, bound), control.snapshot):
        with pytest.raises(port.ControlError):
            operation()
    assert file_hash(path) == before


def test_exact_dto_and_operator_binding_are_required(tmp_path):
    port, _, bound, control = create(tmp_path)
    valid = approval(bound)
    for bad in ({'operator_digest': valid.operator_digest}, object(),
                replace(valid, operator_digest=h('wrong operator'))):
        with pytest.raises(port.ControlError):
            control.consume_approval(bad)
    class Subclass(type(valid)):
        pass
    with pytest.raises(port.ControlError):
        control.consume_approval(Subclass(valid.operator_digest, valid.approval_digest, valid.nonce_hash, valid.expires_at))
    assert control.snapshot()['approval_digest'] is None


def test_expired_and_too_far_future_approvals_cannot_be_consumed(tmp_path, monkeypatch):
    port, _, bound, control = create(tmp_path)
    now = int(time.time())
    monkeypatch.setattr(port.time, 'time', lambda: now)
    for expiry in (now - 1, now + 301):
        with pytest.raises(port.ControlError):
            control.consume_approval(approval(bound, expires=expiry))
    assert control.snapshot()['approval_digest'] is None


def test_nonce_is_consumed_once_and_latest_approval_is_authoritative(tmp_path):
    port, _, bound, control = create(tmp_path)
    first = approval(bound)
    control.consume_approval(first)
    with pytest.raises(port.ControlError):
        control.consume_approval(first)
    second = approval(bound, nonce='second', digest='second')
    control.consume_approval(second)
    assert control.snapshot()['approval_digest'] == second.approval_digest
    prepare(control)
    with pytest.raises(port.ControlError):
        control.complete(approval_digest=first.approval_digest, native_file_sha256=h('native'))
    assert control.snapshot()['decision'] is None


def test_preparation_receipts_are_immutable_and_exact_repeat_is_idempotent(tmp_path):
    port, path, _, control = create(tmp_path)
    control.record_preparation('secretary', h('receipt'), h('file'))
    before = file_hash(path)
    control.record_preparation('secretary', h('receipt'), h('file'))
    assert file_hash(path) == before
    for args in (('secretary', h('different'), h('file')), ('secretary', h('receipt'), h('different')),
                 ('vikunja', h('receipt'), h('file')), ('Secretary', h('receipt'), h('file'))):
        with pytest.raises(port.ControlError):
            control.record_preparation(*args)
    assert control.snapshot()['preparations'] == {'secretary': {'receipt_sha256': h('receipt'), 'file_sha256': h('file')}}


def test_no_decision_before_all_receipts_and_verified_approval(tmp_path):
    port, _, bound, control = create(tmp_path)
    verified = approval(bound)
    with pytest.raises(port.ControlError):
        control.complete(approval_digest=verified.approval_digest, native_file_sha256=h('native'))
    control.consume_approval(verified)
    control.record_preparation('secretary', h('receipt'), h('file'))
    with pytest.raises(port.ControlError):
        control.complete(approval_digest=verified.approval_digest, native_file_sha256=h('native'))
    assert control.snapshot()['decision'] is None
    assert control.snapshot()['state'] == 'restore_hold'


def test_complete_is_immutable_bound_idempotent_and_never_activation(tmp_path):
    port, path, bound, control = create(tmp_path)
    verified = approval(bound)
    control.consume_approval(verified)
    prepare(control)
    # Three records, including a fresh reopen, still do not imply a decision/grant.
    assert port.RestoreControl(path, bound).snapshot()['decision'] is None
    decision = control.complete(approval_digest=verified.approval_digest, native_file_sha256=h('native'))
    assert decision['state'] == 'complete_prepared_blocked' and decision['control_state'] == 'restore_hold'
    assert decision['activation_supported'] is False and decision['outbound'] is False
    assert decision['outbound_enabled'] is False
    assert decision['r3_id'] == control.r3_id and decision['binding'] == bound
    assert decision['operator_digest'] == bound['operator_authority_sha256']
    assert decision['approval_digest'] == verified.approval_digest
    assert decision['native_file_sha256'] == h('native') and len(decision['preparations']) == 3
    assert len(decision['decision_sha256']) == 64
    before = file_hash(path)
    assert control.complete(approval_digest=verified.approval_digest, native_file_sha256=h('native')) == decision
    assert file_hash(path) == before
    with pytest.raises(port.ControlError):
        control.complete(approval_digest=verified.approval_digest, native_file_sha256=h('changed'))
    with pytest.raises(port.ControlError):
        control.consume_approval(approval(bound, nonce='later', digest='later'))
    assert port.RestoreControl(path, bound).snapshot()['decision'] == decision
    assert control.snapshot()['state'] == 'restore_hold'


def test_append_only_tables_resist_raw_update_delete_and_replace(tmp_path):
    _, path, bound, control = create(tmp_path)
    verified = approval(bound)
    control.consume_approval(verified)
    prepare(control)
    control.complete(approval_digest=verified.approval_digest, native_file_sha256=h('native'))
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        for table in ('r3_hold', 'r3_approvals', 'r3_preparations', 'r3_decision'):
            row = conn.execute(f'SELECT * FROM {table} LIMIT 1').fetchone()
            assert row is not None
            for sql, params in ((f'UPDATE {table} SET rowid=rowid', ()),
                                (f'DELETE FROM {table}', ()),
                                (f'INSERT OR REPLACE INTO {table} VALUES({",".join("?" for _ in row)})', row)):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(sql, params)
                conn.rollback()


def test_locked_control_has_bounded_busy_failure_and_preserves_snapshot(tmp_path):
    port, path, bound, control = create(tmp_path)
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        with pytest.raises(port.ControlError):
            control.consume_approval(approval(bound))
        assert time.monotonic() - started < 2
        conn.rollback()
    assert control.snapshot()['approval_digest'] is None


def test_approval_cap_is_enforced_without_partial_record(tmp_path, monkeypatch):
    port, _, bound, control = create(tmp_path)
    monkeypatch.setattr(port, 'MAX_APPROVALS', 2)
    for index in range(2):
        control.consume_approval(approval(bound, nonce=str(index), digest=str(index)))
    before = control.snapshot()
    with pytest.raises(port.ControlError):
        control.consume_approval(approval(bound, nonce='overflow', digest='overflow'))
    assert control.snapshot() == before


def test_initialization_error_is_atomic_and_partial_file_is_not_reinitialized(tmp_path, monkeypatch):
    port = api()
    path = tmp_path / 'failed.sqlite3'
    bound = binding()
    with monkeypatch.context() as patch:
        patch.setattr(port, '_DDL', (*port._DDL, 'INVALID SYNTHETIC SQL'))
        with pytest.raises(port.ControlError):
            port.RestoreControl(path, bound)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT name FROM sqlite_master').fetchall() == []
    before = file_hash(path)
    with pytest.raises(port.ControlError):
        port.RestoreControl(path, bound)
    assert file_hash(path) == before


def test_new_empty_control_does_not_inherit_another_decision(tmp_path):
    _, _, bound, original = create(tmp_path)
    verified = approval(bound)
    original.consume_approval(verified)
    prepare(original)
    original.complete(approval_digest=verified.approval_digest, native_file_sha256=h('native'))
    other = api().RestoreControl(tmp_path / 'new-control.sqlite3', bound)
    assert other.r3_id != original.r3_id and other.snapshot()['decision'] is None
    assert other.snapshot()['preparations'] == {} and other.snapshot()['state'] == 'restore_hold'


def test_nonce_mismatched_digest_and_reopen_cannot_replay_approval(tmp_path):
    port, path, bound, control = create(tmp_path)
    first = approval(bound)
    control.consume_approval(first)
    reopened = port.RestoreControl(path, bound)
    before = file_hash(path)
    with pytest.raises(port.ControlError) as caught:
        reopened.consume_approval(approval(bound, digest='changed but same nonce'))
    assert caught.value.code == 'restore_control_nonce_consumed'
    assert file_hash(path) == before and reopened.snapshot()['approval_digest'] == first.approval_digest


def test_concurrent_nonce_consumption_has_only_one_durable_winner(tmp_path):
    port, path, bound, control = create(tmp_path)
    second_control = port.RestoreControl(path, bound)
    verified = approval(bound)
    def consume(instance):
        try:
            instance.consume_approval(verified)
            return True
        except port.ControlError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, (control, second_control)))
    assert results.count(True) == 1
    assert control.snapshot()['approval_digest'] == verified.approval_digest
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM r3_approvals').fetchone() == (1,)


@pytest.mark.parametrize('stage', ['approval', 'preparation', 'decision'])
def test_interruption_before_commit_leaves_no_partial_receipt(tmp_path, monkeypatch, stage):
    port, path, bound, control = create(tmp_path)
    verified = approval(bound)
    if stage == 'decision':
        control.consume_approval(verified)
        prepare(control)
    before = control.snapshot()
    original = control._transaction
    @contextmanager
    def interrupted():
        with original() as transaction:
            yield transaction
            raise port.ControlError('restore_control_io_failed')
    monkeypatch.setattr(control, '_transaction', interrupted)
    with pytest.raises(port.ControlError):
        if stage == 'approval':
            control.consume_approval(verified)
        elif stage == 'preparation':
            control.record_preparation('secretary', h('receipt'), h('file'))
        else:
            control.complete(approval_digest=verified.approval_digest, native_file_sha256=h('native'))
    assert port.RestoreControl(path, bound).snapshot() == before
    assert not Path(str(path) + '-journal').exists()


@pytest.mark.parametrize('sidecar', ['-journal', '-wal', '-shm'])
def test_crash_sidecars_are_refused_without_recovery_or_mutation(tmp_path, sidecar):
    port, path, bound, control = create(tmp_path)
    sibling = Path(str(path) + sidecar)
    sibling.write_bytes(b'SYNTHETIC_INTERRUPTED_TRANSACTION')
    before, sibling_before = file_hash(path), file_hash(sibling)
    with pytest.raises(port.ControlError):
        port.RestoreControl(path, bound)
    with pytest.raises(port.ControlError):
        control.snapshot()
    assert file_hash(path) == before and file_hash(sibling) == sibling_before


def test_uninitialized_exact_dto_and_mutated_bad_fields_fail_sanitized(tmp_path):
    port, _, bound, control = create(tmp_path)
    dto = type(approval(bound))
    malformed = object.__new__(dto)
    with pytest.raises(port.ControlError) as caught:
        control.consume_approval(malformed)
    assert str(caught.value) == 'restore_control_approval_invalid'
    for field, value in (('nonce_hash', 'UPPER' * 20), ('expires_at', True)):
        malformed = approval(bound)
        object.__setattr__(malformed, field, value)
        with pytest.raises(port.ControlError):
            control.consume_approval(malformed)
    assert control.snapshot()['approval_digest'] is None


def test_expiry_at_complete_does_not_grant_or_replace_accepted_nonce(tmp_path, monkeypatch):
    port, path, bound, control = create(tmp_path)
    verified = approval(bound)
    control.consume_approval(verified)
    prepare(control)
    monkeypatch.setattr(port.time, 'time', lambda: verified.expires_at)
    with pytest.raises(port.ControlError) as caught:
        control.complete(approval_digest=verified.approval_digest, native_file_sha256=h('native'))
    assert caught.value.code == 'restore_control_approval_expired'
    assert control.snapshot()['decision'] is None
    assert port.RestoreControl(path, bound).snapshot()['approval_digest'] == verified.approval_digest


@pytest.mark.parametrize('target', ['binding', 'receipt', 'decision_hash', 'decision_flag', 'extra_index'])
def test_content_tamper_with_restored_schema_still_fails_read_only(tmp_path, target):
    port, path, bound, control = create(tmp_path)
    verified = approval(bound)
    control.consume_approval(verified)
    prepare(control)
    control.complete(approval_digest=verified.approval_digest, native_file_sha256=h('native'))
    with sqlite3.connect(path) as conn:
        if target == 'extra_index':
            conn.execute('CREATE INDEX external_index ON r3_approvals(approval_digest)')
        else:
            table = {'binding': 'r3_hold', 'receipt': 'r3_preparations',
                     'decision_hash': 'r3_decision', 'decision_flag': 'r3_decision'}[target]
            name = table + '_immutable_update'
            trigger_sql = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
            conn.execute('DROP TRIGGER ' + name)
            if target == 'binding':
                conn.execute('UPDATE r3_hold SET binding_sha256=?', (h('wrong binding'),))
            elif target == 'receipt':
                conn.execute("UPDATE r3_preparations SET receipt_sha256=? WHERE authority='team'", (h('wrong receipt'),))
            elif target == 'decision_hash':
                conn.execute('UPDATE r3_decision SET decision_sha256=?', (h('wrong decision'),))
            else:
                payload = json.loads(conn.execute('SELECT payload FROM r3_decision').fetchone()[0])
                payload['outbound'] = 0  # Equal to False in Python, but a different canonical type.
                conn.execute('UPDATE r3_decision SET payload=?',
                             (json.dumps(payload, sort_keys=True, separators=(',', ':')),))
            conn.execute(trigger_sql)
    before = file_hash(path)
    for operation in (control.snapshot, lambda: port.RestoreControl(path, bound),
                      lambda: control.record_preparation('team', h('team:receipt'), h('team:file'))):
        with pytest.raises(port.ControlError):
            operation()
    assert file_hash(path) == before


def test_store_read_bounds_reject_oversized_file_before_sqlite(tmp_path, monkeypatch):
    port, path, bound, control = create(tmp_path)
    with path.open('ab') as stream:
        stream.truncate(port._MAX_DATABASE_BYTES + 1)
    before = file_hash(path)
    def forbidden(*args, **kwargs):
        pytest.fail('SQLite must not open an oversized control file')
    monkeypatch.setattr(port.sqlite3, 'connect', forbidden)
    with pytest.raises(port.ControlError):
        port.RestoreControl(path, bound)
    with pytest.raises(port.ControlError):
        control.snapshot()
    assert file_hash(path) == before


def test_replace_guard_blocks_changed_primary_keys_even_without_recursive_triggers(tmp_path):
    _, path, bound, control = create(tmp_path)
    first = approval(bound)
    control.consume_approval(first)
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT OR REPLACE INTO r3_approvals VALUES(1,?,?,?,?)', (
                first.operator_digest, h('new digest'), h('new nonce'), first.expires_at))
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT OR REPLACE INTO r3_approvals VALUES(2,?,?,?,?)', (
                first.operator_digest, h('new digest'), first.nonce_hash, first.expires_at))
        conn.rollback()
        conn.execute('INSERT INTO r3_preparations VALUES(?,?,?)', ('team', h('receipt'), h('file')))
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT OR REPLACE INTO r3_preparations VALUES(?,?,?)',
                         ('team', h('changed receipt'), h('changed file')))
    assert control.snapshot()['approval_digest'] == first.approval_digest


def test_replaced_new_path_is_rejected_before_foreign_store_initialization(tmp_path, monkeypatch):
    port = api()
    path, unrelated = tmp_path / 'control.sqlite3', tmp_path / 'unrelated.sqlite3'
    with sqlite3.connect(unrelated) as conn:
        conn.execute('CREATE TABLE unrelated_evidence(payload TEXT)')
        conn.execute('INSERT INTO unrelated_evidence VALUES(?)', ('SYNTHETIC_FOREIGN_CONTENT',))
    conn.close()
    before = file_hash(unrelated)
    original = port.RestoreControl._initialize
    def substituted(instance):
        instance._path.unlink()
        os.replace(unrelated, instance._path)
        original(instance)
    monkeypatch.setattr(port.RestoreControl, '_initialize', substituted)
    with pytest.raises(port.ControlError):
        port.RestoreControl(path, binding())
    assert file_hash(path) == before
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT name FROM sqlite_master').fetchall() == [('unrelated_evidence',)]


def test_exclusive_descriptor_identity_is_pinned_before_path_can_be_substituted(tmp_path, monkeypatch):
    port = api()
    path, foreign = tmp_path / 'control.sqlite3', tmp_path / 'foreign-empty.sqlite3'
    foreign.write_bytes(b'')
    original = port.os.fdopen
    @contextmanager
    def switched(fd, *args, **kwargs):
        with original(fd, *args, **kwargs) as stream:
            yield stream
        path.unlink()
        os.replace(foreign, path)
    monkeypatch.setattr(port.os, 'fdopen', switched)
    with pytest.raises(port.ControlError):
        port.RestoreControl(path, binding())
    assert path.read_bytes() == b''
