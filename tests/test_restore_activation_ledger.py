"""R4 ledger tests use new Windows-private synthetic SQLite stores only."""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from importlib import import_module, util
import os
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid4

import pytest


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows custody contract')
ROLES = ('secretary', 'team', 'billing', 'vikunja')
CAPABILITIES = ('fresh_auth', 'fresh_work', 'native_runtime', 'polza_dispatch',
                'recovery', 'sql_write', 'vikunja_dispatch')


def h(value):
    return sha256(value.encode('ascii')).hexdigest()


def binding():
    return {'protocol': 1, 'restore_id': str(uuid4()), 'r3_id': str(uuid4()),
            **{key: h(key) for key in ('r3_decision_sha256', 'r3_binding_sha256',
                'manifest_sha256', 'metadata_sha256', 'inventory_sha256',
                'source_context_sha256')}}


def api():
    name = 'secretary.infrastructure.restore_activation_ledger'
    assert util.find_spec(name) is not None, 'R4 preparation ledger is missing'
    return import_module(name)


@pytest.fixture
def private(tmp_path):
    path = tmp_path / 'private'
    import_module('secretary.infrastructure.restore_operator').create_private_directory(path)
    return path


def create(private):
    port = api()
    bound, path = binding(), private / 'activation.sqlite3'
    return port, path, bound, port.RestoreActivationLedger.create(path, bound)


def file_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def test_missing_reader_never_creates_a_store_or_parent(private):
    port = api()
    for path in (private / 'missing.sqlite3', private / 'absent' / 'missing.sqlite3'):
        with pytest.raises(port.ActivationLedgerError) as error:
            port.RestoreActivationLedger.open_existing(path, binding())
        assert str(error.value) == error.value.code
        assert not path.exists()
    assert not (private / 'absent').exists()


def test_exclusive_creation_reopens_unchanged_with_an_immutable_blocked_generation(private):
    port, path, bound, ledger = create(private)
    snapshot = ledger.snapshot()
    assert str(UUID(snapshot['ledger_id'])) == snapshot['ledger_id']
    hold = snapshot['generation_hold']
    assert str(UUID(hold['generation_id'])) == hold['generation_id']
    assert hold['ordinal'] == 1 and hold['state'] == 'generation_hold'
    assert snapshot['binding'] == bound and snapshot['epoch'] is None
    assert snapshot['sources'] == {} and set(snapshot['missing_source_roles']) == set(ROLES)
    assert snapshot['state'] == 'epoch_prepared_blocked'
    assert snapshot['activation_supported'] is False and snapshot['outbound_enabled'] is False
    before = file_hash(path)
    assert port.RestoreActivationLedger.open_existing(path, bound).snapshot() == snapshot
    with pytest.raises(port.ActivationLedgerError):
        port.RestoreActivationLedger.create(path, bound)
    assert file_hash(path) == before


def test_binding_is_closed_and_typed_before_any_file_creation(private):
    port = api()
    for index, edits in enumerate(({'protocol': True}, {'restore_id': 'not-uuid'},
            {'r3_id': str(uuid4()).upper()}, {'manifest_sha256': 'A' * 64},
            {'extra': 'forbidden'}, {'inventory_sha256': '0' * 63})):
        path = private / f'bad-{index}.sqlite3'
        with pytest.raises(port.ActivationLedgerError):
            port.RestoreActivationLedger.create(path, {**binding(), **edits})
        assert not path.exists()
    bound = binding()
    del bound['metadata_sha256']
    with pytest.raises(port.ActivationLedgerError):
        port.RestoreActivationLedger.create(private / 'missing-key.sqlite3', bound)
    assert not (private / 'missing-key.sqlite3').exists()


def test_copied_ledger_cannot_be_rebound_to_a_different_restore(private):
    port, path, bound, ledger = create(private)
    copied = private / 'copied.sqlite3'
    copied.write_bytes(path.read_bytes())
    before = file_hash(copied)
    for key in ('restore_id', 'r3_id', 'r3_decision_sha256', 'source_context_sha256'):
        change = str(uuid4()) if key.endswith('_id') else h('changed')
        with pytest.raises(port.ActivationLedgerError):
            port.RestoreActivationLedger.open_existing(copied, {**bound, key: change})
    assert file_hash(copied) == before and ledger.snapshot()['epoch'] is None


def test_create_requires_existing_absolute_local_private_parent(private, tmp_path):
    port = api()
    for path in (private / 'absent' / 'ledger.sqlite3', Path('relative-ledger.sqlite3'),
                 tmp_path / 'not-private.sqlite3'):
        with pytest.raises(port.ActivationLedgerError):
            port.RestoreActivationLedger.create(path, binding())
    assert not (private / 'absent').exists() and not (tmp_path / 'not-private.sqlite3').exists()


def test_missing_changed_or_extra_schema_is_refused_without_repair(private):
    port = api()
    for index, tamper in enumerate(('drop_trigger', 'alter_trigger', 'extra_table', 'extra_view')):
        path, bound = private / f'tampered-{index}.sqlite3', binding()
        ledger = port.RestoreActivationLedger.create(path, bound)
        with sqlite3.connect(path) as conn:
            trigger, table = conn.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone()
            if tamper in ('drop_trigger', 'alter_trigger'):
                conn.execute(f'DROP TRIGGER "{trigger}"')
                if tamper == 'alter_trigger':
                    conn.execute(f'CREATE TRIGGER "{trigger}" BEFORE UPDATE ON "{table}" BEGIN SELECT 1; END')
            elif tamper == 'extra_table':
                conn.execute('CREATE TABLE foreign_evidence(value TEXT)')
            else:
                conn.execute('CREATE VIEW foreign_view AS SELECT 1')
        before = file_hash(path)
        for operation in (ledger.snapshot, lambda: port.RestoreActivationLedger.open_existing(path, bound)):
            with pytest.raises(port.ActivationLedgerError) as error:
                operation()
            assert error.value.code == 'activation_ledger_schema_invalid'
        assert file_hash(path) == before


def test_capabilities_require_a_nonempty_unique_sorted_exact_tuple(private):
    port, path, _, ledger = create(private)
    before = file_hash(path)
    for values in ('sql_write', ['sql_write'], (), ('sql_write', 'sql_write'),
                   ('sql_write', 'fresh_work'), ('max_raw_intake',),
                   ('max_subscription_reconcile',), ('unknown',), (True,)):
        with pytest.raises(port.ActivationLedgerError):
            ledger.prepare_epoch(h('scope'), values)
    assert ledger.snapshot()['epoch'] is None and file_hash(path) == before


def test_preparing_the_same_epoch_is_idempotent_and_changed_scope_conflicts(private):
    port, path, _, ledger = create(private)
    epoch = ledger.prepare_epoch(h('scope'), CAPABILITIES)
    assert str(UUID(epoch['epoch_id'])) == epoch['epoch_id']
    assert epoch['state'] == 'epoch_prepared_blocked'
    assert epoch['generation_id'] == ledger.snapshot()['generation_hold']['generation_id']
    assert epoch['capabilities'] == CAPABILITIES
    before = file_hash(path)
    assert ledger.prepare_epoch(h('scope'), CAPABILITIES) == epoch
    for scope, capabilities in ((h('different'), CAPABILITIES), (h('scope'), ('sql_write',))):
        with pytest.raises(port.ActivationLedgerError) as error:
            ledger.prepare_epoch(scope, capabilities)
        assert error.value.code == 'activation_ledger_epoch_conflict'
    assert file_hash(path) == before


def test_all_four_source_readbacks_are_preserved_but_never_enable_outbound(private):
    _, _, _, ledger = create(private)
    epoch = ledger.prepare_epoch(h('scope'), CAPABILITIES)
    for role in ROLES:
        ledger.record_source(epoch['epoch_id'], role, h(role + ':commitment'),
                             h(role + ':file'), h(role + ':preparation'))
    snapshot = ledger.snapshot()
    assert set(snapshot['sources']) == set(ROLES) and snapshot['missing_source_roles'] == ()
    assert snapshot['preparation_complete'] is True
    assert snapshot['state'] == 'epoch_prepared_blocked'
    assert snapshot['activation_supported'] is False and snapshot['outbound_enabled'] is False
    assert snapshot['outbound'] is False
    snapshot['binding']['manifest_sha256'] = h('caller mutation')
    snapshot['sources']['team']['baseline_file_sha256'] = h('caller mutation')
    assert ledger.snapshot()['sources']['team']['baseline_file_sha256'] == h('team:file')
    assert not hasattr(ledger, 'activate') and not hasattr(ledger, 'approve')


def test_source_record_requires_the_prepared_epoch_and_exact_role_and_digests(private):
    port, path, _, ledger = create(private)
    with pytest.raises(port.ActivationLedgerError):
        ledger.record_source(str(uuid4()), 'secretary', h('c'), h('f'), h('p'))
    epoch = ledger.prepare_epoch(h('scope'), ('sql_write',))
    before = file_hash(path)
    for values in ((str(uuid4()), 'secretary', h('c'), h('f'), h('p')),
                   (epoch['epoch_id'], 'Secretary', h('c'), h('f'), h('p')),
                   (epoch['epoch_id'], 'native', h('c'), h('f'), h('p')),
                   (epoch['epoch_id'], 'team', 'A' * 64, h('f'), h('p')),
                   (epoch['epoch_id'], 'team', h('c'), h('f'), None)):
        with pytest.raises(port.ActivationLedgerError):
            ledger.record_source(*values)
    assert ledger.snapshot()['sources'] == {} and file_hash(path) == before


def test_repeated_source_receipt_is_idempotent_but_each_changed_digest_conflicts(private):
    port, path, _, ledger = create(private)
    epoch = ledger.prepare_epoch(h('scope'), ('sql_write',))
    values = (epoch['epoch_id'], 'team', h('c'), h('f'), h('p'))
    ledger.record_source(*values)
    before = file_hash(path)
    ledger.record_source(*values)
    for field in (2, 3, 4):
        changed = list(values)
        changed[field] = h('changed')
        with pytest.raises(port.ActivationLedgerError) as error:
            ledger.record_source(*changed)
        assert error.value.code == 'activation_ledger_source_conflict'
    assert file_hash(path) == before


def test_partial_preparation_reopens_blocked_with_missing_roles_preserved(private):
    port, path, bound, ledger = create(private)
    epoch = ledger.prepare_epoch(h('scope'), ('fresh_work',))
    ledger.record_source(epoch['epoch_id'], 'secretary', h('c'), h('f'), h('p'))
    before = file_hash(path)
    reopened = port.RestoreActivationLedger.open_existing(path, bound).snapshot()
    assert reopened['epoch'] == epoch and set(reopened['missing_source_roles']) == {'team', 'billing', 'vikunja'}
    assert reopened['preparation_complete'] is False and reopened['outbound_enabled'] is False
    assert file_hash(path) == before


def test_append_only_tables_resist_update_delete_and_replace_with_recursive_triggers_off(private):
    _, path, _, ledger = create(private)
    epoch = ledger.prepare_epoch(h('scope'), ('sql_write',))
    ledger.record_source(epoch['epoch_id'], 'team', h('c'), h('f'), h('p'))
    before = file_hash(path)
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        for table in ('ledger_binding', 'generation_hold', 'activation_epoch', 'activation_sources'):
            row = conn.execute(f'SELECT * FROM {table} LIMIT 1').fetchone()
            for sql, params in ((f'UPDATE {table} SET rowid=rowid', ()),
                                (f'DELETE FROM {table}', ()),
                                (f'INSERT OR REPLACE INTO {table} VALUES({",".join("?" for _ in row)})', row)):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(sql, params)
                conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT OR REPLACE INTO activation_sources VALUES(?,?,?,?,?)',
                         ('team', epoch['epoch_id'], h('changed'), h('f'), h('p')))
        conn.rollback()
    assert file_hash(path) == before


def test_interruption_after_source_insert_rolls_back_before_reopen(private, monkeypatch):
    port, path, bound, ledger = create(private)
    epoch = ledger.prepare_epoch(h('scope'), ('sql_write',))
    original, calls = ledger._validate, 0
    def interrupted(conn):
        nonlocal calls
        result = original(conn)
        calls += 1
        if calls == 3:
            raise RuntimeError('synthetic interruption before commit')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(ledger, '_validate', interrupted)
        with pytest.raises(RuntimeError):
            ledger.record_source(epoch['epoch_id'], 'team', h('c'), h('f'), h('p'))
    reopened = port.RestoreActivationLedger.open_existing(path, bound).snapshot()
    assert reopened['sources'] == {} and reopened['outbound_enabled'] is False


def test_pinned_instance_refuses_a_same_byte_replacement_without_mutating_it(private):
    port, path, _, ledger = create(private)
    other = private / 'replacement.sqlite3'
    other.write_bytes(path.read_bytes())
    before = file_hash(other)
    os.replace(other, path)
    with pytest.raises(port.ActivationLedgerError):
        ledger.snapshot()
    assert file_hash(path) == before


def test_sqlite_open_holds_child_identity_against_delete_and_replacement(private, monkeypatch):
    port, path, _, ledger = create(private)
    foreign = private / 'foreign.sqlite3'
    foreign.write_bytes(path.read_bytes())
    foreign_before = file_hash(foreign)
    original = port.sqlite3.connect
    observed = []
    def attempted_substitution(database, *args, **kwargs):
        # This is the boundary after the ledger probe and before SQLite opens it.
        # Real Windows file operations must fail under the retained child lease.
        if str(database).startswith(path.as_uri()):
            with pytest.raises(PermissionError):
                path.unlink()
            with pytest.raises(PermissionError):
                os.replace(foreign, path)
            observed.append(True)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(port.sqlite3, 'connect', attempted_substitution)
    epoch = ledger.prepare_epoch(h('scope'), ('sql_write',))
    assert observed and ledger.snapshot()['epoch'] == epoch
    assert foreign.exists() and file_hash(foreign) == foreign_before


def test_existing_empty_foreign_or_partial_store_is_never_initialized_or_repaired(private):
    port = api()
    for index, kind in enumerate(('empty', 'foreign', 'partial')):
        path = private / f'foreign-{index}.sqlite3'
        if kind == 'empty':
            path.write_bytes(b'')
        else:
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE foreign_evidence(payload TEXT)')
                conn.execute('INSERT INTO foreign_evidence VALUES(?)', ('SYNTHETIC_PRIVATE_DO_NOT_LOG',))
                if kind == 'partial':
                    conn.execute('CREATE TABLE ledger_binding(singleton INTEGER PRIMARY KEY)')
        before = file_hash(path)
        for operation in (port.RestoreActivationLedger.create, port.RestoreActivationLedger.open_existing):
            with pytest.raises(port.ActivationLedgerError) as error:
                operation(path, binding())
            assert str(error.value) == error.value.code and 'SYNTHETIC_PRIVATE' not in str(error.value)
        assert file_hash(path) == before


def test_failed_initialization_is_atomic_and_existing_partial_file_is_not_adopted(private, monkeypatch):
    port = api()
    path, bound = private / 'failed.sqlite3', binding()
    with monkeypatch.context() as patch:
        patch.setattr(port, '_DDL', (*port._DDL, 'INVALID SYNTHETIC SQL'))
        with pytest.raises(port.ActivationLedgerError):
            port.RestoreActivationLedger.create(path, bound)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT name FROM sqlite_master').fetchall() == []
    before = file_hash(path)
    for operation in (port.RestoreActivationLedger.create, port.RestoreActivationLedger.open_existing):
        with pytest.raises(port.ActivationLedgerError):
            operation(path, bound)
    assert file_hash(path) == before


def test_journal_sidecars_are_refused_and_preserved_without_recovery(private):
    port, path, _, ledger = create(private)
    before = file_hash(path)
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        sidecar.write_bytes(b'synthetic incomplete journal')
        with pytest.raises(port.ActivationLedgerError):
            ledger.snapshot()
        assert sidecar.read_bytes() == b'synthetic incomplete journal' and file_hash(path) == before
        sidecar.unlink()


def test_hardlinked_ledger_is_refused_without_writes(private):
    port, path, _, ledger = create(private)
    linked = private / 'link.sqlite3'
    os.link(path, linked)
    before = file_hash(path)
    with pytest.raises(port.ActivationLedgerError):
        ledger.snapshot()
    assert file_hash(linked) == before


def test_locked_ledger_has_bounded_sanitized_failure_and_retains_the_hold(private):
    port, path, _, ledger = create(private)
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        with pytest.raises(port.ActivationLedgerError) as error:
            ledger.prepare_epoch(h('scope'), ('sql_write',))
        assert str(error.value) == error.value.code and time.monotonic() - started < 2
        conn.rollback()
    assert ledger.snapshot()['epoch'] is None and ledger.snapshot()['outbound_enabled'] is False


def test_file_bound_is_enforced_before_sqlite_reads(private, monkeypatch):
    port, path, _, ledger = create(private)
    before = file_hash(path)
    monkeypatch.setattr(port, '_MAX_DATABASE_BYTES', path.stat().st_size - 1)
    with pytest.raises(port.ActivationLedgerError):
        ledger.snapshot()
    assert file_hash(path) == before


def test_exclusive_creation_pins_descriptor_before_path_substitution(private, monkeypatch):
    port = api()
    path, foreign = private / 'ledger.sqlite3', private / 'foreign-empty.sqlite3'
    foreign.write_bytes(b'')
    original = port.os.fdopen
    @contextmanager
    def switched(fd, *args, **kwargs):
        with original(fd, *args, **kwargs) as stream:
            yield stream
        path.unlink()
        try:
            os.replace(foreign, path)
        except PermissionError:
            # Held private-parent handles can deny the substitution itself.
            # The failed create must still never initialize the foreign inode.
            assert foreign.read_bytes() == b''
    monkeypatch.setattr(port.os, 'fdopen', switched)
    with pytest.raises(port.ActivationLedgerError):
        port.RestoreActivationLedger.create(path, binding())
    assert not path.exists() or path.read_bytes() == b''
    assert not foreign.exists() or foreign.read_bytes() == b''


def test_new_initialization_keeps_exclusive_descriptor_until_sqlite_commit(private, monkeypatch):
    port = api()
    path, foreign = private / 'ledger.sqlite3', private / 'foreign-empty.sqlite3'
    foreign.write_bytes(b'')
    original = port.sqlite3.connect
    attempts = []
    def attempted_substitution(database, *args, **kwargs):
        if str(database) == path.as_uri() + '?mode=rw':
            with pytest.raises(PermissionError):
                path.unlink()
            with pytest.raises(PermissionError):
                os.replace(foreign, path)
            attempts.append(True)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(port.sqlite3, 'connect', attempted_substitution)
    ledger = port.RestoreActivationLedger.create(path, binding())
    assert attempts and ledger.snapshot()['epoch'] is None
    assert foreign.read_bytes() == b''


def test_ledger_preparation_never_opens_or_mutates_finalized_r3_control(private):
    port = api()
    control_api = import_module('secretary.infrastructure.restore_reconciliation_control')
    operator_api = import_module('secretary.infrastructure.restore_operator')
    r3_path = private / 'r3.sqlite3'
    r3_binding = {'protocol': 1, 'restore_id': str(uuid4()), **{key: h(key) for key in (
        'plan_sha256', 'input_sha256', 'scope_sha256', 'evidence_sha256',
        'source_paths_sha256', 'operator_authority_sha256')}}
    control = control_api.RestoreControl(r3_path, r3_binding)
    receipt = operator_api.VerifiedOperatorApproval(r3_binding['operator_authority_sha256'],
        h('synthetic typed receipt, not owner/provider proof'), h('nonce'), int(time.time()) + 120)
    control.consume_approval(receipt)
    for role in ROLES[:3]:
        control.record_preparation(role, h(role + ':receipt'), h(role + ':file'))
    decision = control.complete(approval_digest=receipt.approval_digest, native_file_sha256=h('native'))
    before = file_hash(r3_path)
    bound = {**binding(), 'restore_id': r3_binding['restore_id'], 'r3_id': control.r3_id,
             'r3_decision_sha256': decision['decision_sha256'],
             'r3_binding_sha256': decision['binding_sha256']}
    ledger = port.RestoreActivationLedger.create(private / 'r4.sqlite3', bound)
    epoch = ledger.prepare_epoch(h('scope'), ('sql_write',))
    for role in ROLES:
        ledger.record_source(epoch['epoch_id'], role, h(role + ':c'), h(role + ':f'), h(role + ':p'))
    assert file_hash(r3_path) == before and control.snapshot()['decision'] == decision
    assert ledger.snapshot()['outbound_enabled'] is False
