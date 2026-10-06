"""Closed diagnostic observations in synthetic Windows-private companions only."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
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


def h(value): return sha256(value.encode('ascii')).hexdigest()


def api():
    name = 'secretary.infrastructure.restore_provider_observation_store'
    assert util.find_spec(name) is not None, 'Independent provider observation store is missing'
    return import_module(name)


@pytest.fixture
def private(tmp_path):
    path = tmp_path / 'private'
    import_module('secretary.infrastructure.restore_operator').create_private_directory(path)
    return path


def binding():
    return dict(protocol=1, purpose='restore_provider_observation',
        restore_id=str(uuid4()), epoch_id=str(uuid4()), preparation_id=str(uuid4()),
        source_snapshot_sha256=h('actual four prepared sources'), scope_sha256=h('scope'),
        binding_sha256=h('source proposal binding'))


def approval(nonce='first', *, expires=None, operator='synthetic operator'):
    cls = import_module('secretary.infrastructure.restore_operator').VerifiedOperatorApproval
    return cls(h(operator), h('approval:' + nonce), h('nonce:' + nonce),
        int(time.time()) + 120 if expires is None else expires)


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def payload(bound, verified, *, operation='polza_key_usage', nonce='request', kind='synthetic_get'):
    now = time.time()
    facts = {
        'polza_key_usage': dict(key_tag=h('credential'), limit_micro=3_000_000_000,
            remaining_micro=2_995_780_000, usage_micro=4_220_000, reset='monthly'),
        'polza_generation_history': dict(provider_request_id='generation-17', status='completed',
            confirmed_cost_micro=4_220_001, provider_period=None),
        'max_bot_identity': dict(bot_id='123'),
        'max_subscriptions': dict(bot_id='123', subscriptions=[dict(url_sha256=h('webhook URL'),
            time=None, update_types=['bot_started', 'message_created'])]),
    }[operation]
    return {**deepcopy(bound), 'evidence_kind': kind,
        'operator_approval_sha256': verified.approval_digest,
        'provider': 'polza' if operation.startswith('polza_') else 'max',
        'operation': operation, 'credential_sha256': h('credential'),
        'resource_sha256': h('config plus actual resource plus liability'),
        'request_sha256': h(nonce), 'method': 'GET', 'started_at': utc(now - 1),
        'finished_at': utc(now - .25), 'expires_at': utc(now + 60),
        'state': 'observed', 'facts': facts, 'error_code': None}


def create(private, *, approved=False):
    port, path, bound = api(), private / 'observations.sqlite3', binding()
    store = port.ObservationStore.create(path, bound)
    verified = approval()
    if approved: store.consume_approval(verified)
    return port, path, bound, store, verified


def file_hash(path): return sha256(path.read_bytes()).hexdigest()


def test_existing_only_reader_and_missing_parent_never_create(private):
    port, bound = api(), binding()
    for path in (private / 'absent.sqlite3', private / 'missing-parent' / 'absent.sqlite3'):
        with pytest.raises(port.ObservationStoreError): port.ObservationStore.open_existing(path, bound)
        assert not path.exists()
    assert not (private / 'missing-parent').exists()


def test_creation_reopen_and_detached_snapshot_never_enable_anything(private):
    port, path, bound, store, _ = create(private)
    snapshot, before = store.snapshot(), file_hash(path)
    assert str(UUID(snapshot['store_id'])) == snapshot['store_id']
    assert snapshot['binding'] == bound and snapshot['observations'] == ()
    assert snapshot['approvals'] == () and snapshot['approval'] is None
    assert snapshot['state'] == 'provider_observations_blocked'
    assert snapshot['activation_supported'] is False and snapshot['outbound_enabled'] is False
    assert port.ObservationStore.open_existing(path, bound).snapshot() == snapshot
    snapshot['binding']['scope_sha256'] = h('caller mutation')
    bound['scope_sha256'] = h('different caller mutation')
    assert store.snapshot()['binding']['scope_sha256'] == h('scope')
    assert file_hash(path) == before
    with pytest.raises(port.ObservationStoreError): port.ObservationStore.create(path, binding())
    assert file_hash(path) == before


@pytest.mark.parametrize('key,value', [('protocol', True), ('purpose', 'source_epoch_preparation'),
    ('restore_id', 'NO'), ('epoch_id', True), ('preparation_id', str(uuid4()).upper()),
    ('source_snapshot_sha256', 'x' * 64), ('scope_sha256', 'A' * 64), ('binding_sha256', None),
    ('activation_supported', True)])
def test_closed_binding_rejected_before_creation(private, key, value):
    port, path, bound = api(), private / 'invalid.sqlite3', binding()
    bound[key] = value
    with pytest.raises(port.ObservationStoreError): port.ObservationStore.create(path, bound)
    assert not path.exists()


@pytest.mark.parametrize('key', ['restore_id', 'epoch_id', 'preparation_id',
    'source_snapshot_sha256', 'scope_sha256', 'binding_sha256'])
def test_existing_store_cannot_rebind(private, key):
    port, path, bound, store, _ = create(private)
    foreign = deepcopy(bound)
    foreign[key] = str(uuid4()) if key.endswith('_id') else h('foreign:' + key)
    before = file_hash(path)
    with pytest.raises(port.ObservationStoreError): port.ObservationStore.open_existing(path, foreign)
    assert file_hash(path) == before and store.snapshot()['observations'] == ()


def test_approvals_are_append_only_same_operator_and_fresh(private):
    port, path, _, store, first = create(private, approved=True)
    before = file_hash(path)
    store.consume_approval(first)
    assert file_hash(path) == before
    second = approval('second')
    store.consume_approval(second)
    snapshot = store.snapshot()
    assert len(snapshot['approvals']) == 2 and snapshot['approval']['approval_digest'] == second.approval_digest
    for bad in (approval('foreign', operator='other account'), approval('expired', expires=int(time.time()) - 1),
        approval('future', expires=int(time.time()) + 301), dict(approved=True)):
        before = file_hash(path)
        with pytest.raises(port.ObservationStoreError): store.consume_approval(bad)
        assert file_hash(path) == before
    altered = deepcopy(second)
    object.__setattr__(altered, 'expires_at', second.expires_at + 1)
    with pytest.raises(port.ObservationStoreError): store.consume_approval(altered)


@pytest.mark.parametrize('operation', ['polza_key_usage', 'polza_generation_history',
    'max_bot_identity', 'max_subscriptions'])
def test_closed_observation_appends_and_reopens_without_qualification(private, operation):
    port, path, bound, store, verified = create(private, approved=True)
    evidence = payload(bound, verified, operation=operation)
    record = store.append_observation(evidence)
    assert record['seq'] == 1 and record['payload'] == evidence
    assert len(record['payload_sha256']) == 64 and len(record['record_sha256']) == 64
    assert record['previous_record_sha256'] is None
    before = file_hash(path)
    assert store.append_observation(evidence) == record
    assert file_hash(path) == before
    evidence['facts'] = {'caller_mutation': True}
    snapshot = port.ObservationStore.open_existing(path, bound).snapshot()
    assert snapshot['observations'][0] == record
    assert snapshot['activation_supported'] is False and not hasattr(store, 'activate')


def test_pending_cost_is_unknown_and_unavailable_is_not_provider_success(private):
    _, _, bound, store, verified = create(private, approved=True)
    pending = payload(bound, verified, operation='polza_generation_history')
    pending['facts'].update(status='pending', confirmed_cost_micro=None)
    store.append_observation(pending)
    unavailable = payload(bound, verified, nonce='unavailable')
    unavailable.update(state='unavailable', facts=None, error_code='monthly_budget_account_unavailable')
    store.append_observation(unavailable)
    assert len(store.snapshot()['observations']) == 2
    assert store.snapshot()['observations'][0]['payload']['facts']['confirmed_cost_micro'] is None


@pytest.mark.parametrize('kind', ['extra', 'binding', 'purpose', 'provider', 'operation', 'method',
    'evidence_kind', 'approval', 'credential', 'resource', 'request', 'start_offset', 'backward',
    'slow', 'expired', 'future_finish', 'long_ttl', 'approval_ttl', 'state', 'facts_extra',
    'facts_raw_key', 'key_mismatch', 'money_bool', 'money_negative', 'error_success', 'error_unknown',
    'error_facts', 'pending_cost', 'terminal_unknown', 'provider_period', 'username', 'bot_bool',
    'subscription_url', 'subscription_types_order', 'subscription_duplicate'])
def test_malformed_foreign_or_expired_evidence_never_appends(private, kind):
    port, path, bound, store, verified = create(private, approved=True)
    p = payload(bound, verified)
    if kind == 'extra': p['activation_supported'] = True
    elif kind == 'binding': p['epoch_id'] = str(uuid4())
    elif kind == 'purpose': p['purpose'] = 'fresh_auth'
    elif kind == 'provider': p['provider'] = 'max'
    elif kind == 'operation': p['operation'] = 'paid_transcription'
    elif kind == 'method': p['method'] = 'POST'
    elif kind == 'evidence_kind': p['evidence_kind'] = 'caller_proof'
    elif kind == 'approval': p['operator_approval_sha256'] = h('foreign approval')
    elif kind == 'credential': p['credential_sha256'] = 'RAW_SECRET'
    elif kind == 'resource': p['resource_sha256'] = True
    elif kind == 'request': p['request_sha256'] = 'A' * 64
    elif kind == 'start_offset': p['started_at'] = p['started_at'].replace('Z', '+00:00')
    elif kind == 'backward': p['finished_at'] = utc(time.time() - 10)
    elif kind == 'slow': p['started_at'] = utc(time.time() - 31)
    elif kind == 'expired': p['expires_at'] = utc(time.time() - 1)
    elif kind == 'future_finish': p['finished_at'] = utc(time.time() + 2)
    elif kind == 'long_ttl': p['expires_at'] = utc(time.time() + 301)
    elif kind == 'approval_ttl': p['expires_at'] = utc(verified.expires_at + 1)
    elif kind == 'state': p['state'] = 'settled'
    elif kind == 'facts_extra': p['facts']['settled'] = True
    elif kind == 'facts_raw_key': p['facts']['key_tag'] = 'RAW_SECRET'
    elif kind == 'key_mismatch': p['facts']['key_tag'] = h('different credential')
    elif kind == 'money_bool': p['facts']['usage_micro'] = True
    elif kind == 'money_negative': p['facts']['remaining_micro'] = -1
    elif kind == 'error_success': p['error_code'] = 'provider_observation_unavailable'
    elif kind == 'error_unknown': p.update(state='unavailable', facts=None, error_code='RAW_SECRET')
    elif kind == 'error_facts': p.update(state='unavailable', error_code='provider_observation_unavailable')
    elif kind in ('pending_cost', 'terminal_unknown', 'provider_period'):
        p = payload(bound, verified, operation='polza_generation_history')
        if kind == 'pending_cost': p['facts']['status'] = 'pending'
        elif kind == 'terminal_unknown': p['facts']['confirmed_cost_micro'] = None
        else: p['facts']['provider_period'] = 'guessed month'
    elif kind in ('username', 'bot_bool'):
        p = payload(bound, verified, operation='max_bot_identity')
        if kind == 'username': p['facts']['username'] = 'unneeded personal name'
        else: p['facts']['bot_id'] = True
    else:
        p = payload(bound, verified, operation='max_subscriptions')
        row = p['facts']['subscriptions'][0]
        if kind == 'subscription_url': row['url'] = 'https://secret.invalid/token'
        elif kind == 'subscription_types_order': row['update_types'].reverse()
        else: p['facts']['subscriptions'].append(deepcopy(row))
    before = file_hash(path)
    with pytest.raises(port.ObservationStoreError): store.append_observation(p)
    assert file_hash(path) == before and store.snapshot()['observations'] == ()


def test_unapproved_and_old_approval_cannot_append_but_history_remains_readable(private, monkeypatch):
    port, path, bound, store, verified = create(private)
    evidence = payload(bound, verified)
    with pytest.raises(port.ObservationStoreError): store.append_observation(evidence)
    store.consume_approval(verified)
    store.append_observation(evidence)
    second = approval('second')
    store.consume_approval(second)
    with pytest.raises(port.ObservationStoreError): store.append_observation(payload(bound, verified, nonce='old'))
    monkeypatch.setattr(port.time, 'time', lambda: second.expires_at)
    before = file_hash(path)
    with pytest.raises(port.ObservationStoreError): store.append_observation(payload(bound, second, nonce='stale'))
    assert len(port.ObservationStore.open_existing(path, bound).snapshot()['observations']) == 1
    assert file_hash(path) == before


def test_expiry_after_write_lock_is_refused(private, monkeypatch):
    port, path, bound, store, verified = create(private, approved=True)
    p, original = payload(bound, verified), store._transaction
    @contextmanager
    def expire_inside():
        with original() as values:
            monkeypatch.setattr(port.time, 'time', lambda: verified.expires_at)
            yield values
    monkeypatch.setattr(store, '_transaction', expire_inside)
    before = file_hash(path)
    with pytest.raises(port.ObservationStoreError): store.append_observation(p)
    assert file_hash(path) == before and store.snapshot()['observations'] == ()


def test_crash_before_commit_rolls_back_and_keeps_same_store_id(private, monkeypatch):
    port, path, bound, store, verified = create(private, approved=True)
    first = store.append_observation(payload(bound, verified))
    identity, original = store.snapshot()['store_id'], store._validate
    def fail_after_insert(conn):
        result = original(conn)
        if len(result['observations']) == 2: raise RuntimeError('synthetic precommit crash')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(store, '_validate', fail_after_insert)
        with pytest.raises(RuntimeError): store.append_observation(payload(bound, verified, nonce='second'))
    resumed = port.ObservationStore.open_existing(path, bound)
    assert resumed.snapshot()['store_id'] == identity and resumed.snapshot()['observations'] == (first,)
    assert resumed.append_observation(payload(bound, verified, nonce='second'))['seq'] == 2


def test_sql_update_delete_and_replace_cannot_modify_any_history(private):
    _, path, bound, store, verified = create(private, approved=True)
    store.append_observation(payload(bound, verified))
    before = file_hash(path)
    with sqlite3.connect(path) as conn:
        for table in ('observation_binding', 'observation_operator_approval', 'provider_observations'):
            for sql in (f'DELETE FROM {table}', f'UPDATE {table} SET rowid=rowid',
                f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}', f'REPLACE INTO {table} SELECT * FROM {table}'):
                with pytest.raises(sqlite3.IntegrityError): conn.execute(sql)
                conn.rollback()
    assert file_hash(path) == before and len(store.snapshot()['observations']) == 1


@pytest.mark.parametrize('kind', ['extra', 'drop', 'changed', 'corrupt_payload', 'corrupt_chain'])
def test_tampered_store_is_rejected_and_never_repaired(private, kind):
    port, path, bound, store, verified = create(private, approved=True)
    store.append_observation(payload(bound, verified))
    with sqlite3.connect(path) as conn:
        if kind == 'extra': conn.execute('CREATE TABLE caller_provider_proof(payload TEXT)')
        elif kind in ('drop', 'changed'):
            name, table = conn.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone()
            conn.execute(f'DROP TRIGGER "{name}"')
            if kind == 'changed': conn.execute(f'CREATE TRIGGER "{name}" BEFORE DELETE ON "{table}" BEGIN SELECT 1; END')
        else:
            name, ddl = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='provider_observations' AND name LIKE '%update'").fetchone()
            conn.execute(f'DROP TRIGGER "{name}"')
            if kind == 'corrupt_payload': conn.execute("UPDATE provider_observations SET payload=json_set(payload,'$.method','POST')")
            else: conn.execute('UPDATE provider_observations SET record_sha256=?', (h('forged chain'),))
            conn.execute(ddl)
    before = file_hash(path)
    with pytest.raises(port.ObservationStoreError): store.snapshot()
    with pytest.raises(port.ObservationStoreError): port.ObservationStore.open_existing(path, bound)
    assert file_hash(path) == before


@pytest.mark.parametrize('namespace', ['temp_binding', 'temp_approval', 'temp_observation', 'attach'])
def test_supplied_connection_aliases_are_rejected_readonly(private, namespace):
    port, path, bound, store, verified = create(private, approved=True)
    store.append_observation(payload(bound, verified))
    before = file_hash(path)
    with sqlite3.connect(path) as conn:
        if namespace == 'attach': conn.execute("ATTACH DATABASE ':memory:' AS foreign_db")
        else:
            table = {'temp_binding': 'observation_binding', 'temp_approval': 'observation_operator_approval',
                'temp_observation': 'provider_observations'}[namespace]
            conn.execute(f'CREATE TEMP TABLE {table} AS SELECT * FROM main.{table}')
        with pytest.raises(port.ObservationStoreError): store._validate(conn)
    assert file_hash(path) == before


def test_child_substitution_is_blocked_through_actual_sqlite_close(private, monkeypatch):
    port, path, _, store, verified = create(private)
    foreign = private / 'foreign.sqlite3'
    foreign.write_bytes(path.read_bytes())
    original, attempts = port.sqlite3.connect, []
    def substitute(database, *args, **kwargs):
        if str(database).startswith(path.as_uri()):
            with pytest.raises(PermissionError): path.unlink()
            with pytest.raises(PermissionError): os.replace(foreign, path)
            attempts.append(True)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(port.sqlite3, 'connect', substitute)
    store.consume_approval(verified)
    assert attempts and foreign.exists()


def test_sidecars_hardlinks_and_substituted_identity_are_not_adopted(private):
    port, path, _, store, _ = create(private)
    before = file_hash(path)
    for suffix in ('-wal', '-shm', '-journal'):
        side = Path(str(path) + suffix)
        side.write_bytes(b'synthetic journal')
        with pytest.raises(port.ObservationStoreError): store.snapshot()
        assert side.read_bytes() == b'synthetic journal' and file_hash(path) == before
        side.unlink()
    link = private / 'linked.sqlite3'
    os.link(path, link)
    with pytest.raises(port.ObservationStoreError): store.snapshot()
    link.unlink()
    replacement = private / 'replacement.sqlite3'
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    with pytest.raises(port.ObservationStoreError): store.snapshot()


@pytest.mark.parametrize('kind', ['empty', 'foreign', 'missing_parent', 'unprotected_parent', 'relative', 'device'])
def test_unsafe_or_preexisting_file_is_never_initialized_or_repaired(private, tmp_path, kind):
    port, path = api(), private / 'invalid.sqlite3'
    if kind == 'empty': path.write_bytes(b'')
    elif kind == 'foreign':
        with sqlite3.connect(path) as conn: conn.execute('CREATE TABLE foreign_data(payload TEXT)')
    elif kind == 'missing_parent': path = private / 'absent' / 'store.sqlite3'
    elif kind == 'unprotected_parent': path = tmp_path / 'store.sqlite3'
    elif kind == 'relative': path = Path('relative.sqlite3')
    else: path = private / 'CON.sqlite3'
    before = file_hash(path) if path.exists() else None
    with pytest.raises(port.ObservationStoreError): port.ObservationStore.create(path, binding())
    if before is not None: assert file_hash(path) == before
    elif kind != 'device': assert not path.exists()
    assert not (private / 'absent').exists()


def test_approval_capacity_and_payload_size_are_bounded(private):
    port, path, bound, store, verified = create(private, approved=True)
    for i in range(1, 64): store.consume_approval(approval(str(i)))
    before = file_hash(path)
    with pytest.raises(port.ObservationStoreError): store.consume_approval(approval('65'))
    assert file_hash(path) == before and len(store.snapshot()['approvals']) == 64
    latest = approval('63')
    p = payload(bound, latest, operation='max_subscriptions')
    p['facts']['subscriptions'] = [dict(url_sha256=h(str(i)), time=None,
        update_types=['message_created']) for i in range(1000)]
    with pytest.raises(port.ObservationStoreError): store.append_observation(p)
    assert file_hash(path) == before and store.snapshot()['observations'] == ()


def test_locked_writer_is_bounded_and_errors_are_sanitized(private):
    port, path, _, store, verified = create(private)
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        with pytest.raises(port.ObservationStoreError) as error: store.consume_approval(verified)
        assert str(error.value) == error.value.code and time.monotonic() - started < 2
        conn.rollback()
    for incoming in ('RAW_SECRET_DO_NOT_LOG', [], {}, None, True):
        error = port.ObservationStoreError(incoming)
        assert str(error) == 'provider_observation_store_invalid'


def test_five_hundred_twelve_records_and_sql_bound_preserve_history(private):
    port, path, bound, store, verified = create(private, approved=True)
    # Seed a complete synthetic history in one fixture transaction; the public
    # reader verifies every record, and the actual appender tests the boundary.
    snapshot = store.snapshot()
    previous = None
    with sqlite3.connect(path) as conn:
        for index in range(1, 513):
            p = payload(bound, verified, operation='polza_generation_history', nonce=f'history-{index}')
            record = store._record(snapshot['store_id'], snapshot['binding_digest_sha256'], index, p, previous)
            conn.execute('INSERT INTO provider_observations VALUES(?,?,?,?,?,?)',
                (index, verified.approval_digest, json.dumps(p, sort_keys=True, ensure_ascii=True, separators=(',', ':')),
                    record['payload_sha256'], previous, record['record_sha256']))
            previous = record['record_sha256']
    reopened = port.ObservationStore.open_existing(path, bound)
    assert len(reopened.snapshot()['observations']) == 512
    before = file_hash(path)
    extra = payload(bound, verified, nonce='513')
    with pytest.raises(port.ObservationStoreError) as error: reopened.append_observation(extra)
    assert error.value.code == 'provider_observation_observation_limit'
    with sqlite3.connect(path) as conn:
        record = store._record(snapshot['store_id'], snapshot['binding_digest_sha256'], 513, extra, previous)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO provider_observations VALUES(?,?,?,?,?,?)',
                (513, verified.approval_digest, json.dumps(extra), record['payload_sha256'], previous, record['record_sha256']))
        conn.rollback()
    assert file_hash(path) == before and len(reopened.snapshot()['observations']) == 512


def test_one_mebibyte_total_bound_preserves_prior_observations(private):
    port, path, bound, store, verified = create(private, approved=True)
    count = 0
    while True:
        p = payload(bound, verified, operation='max_subscriptions', nonce=f'large-{count}')
        p['facts']['subscriptions'] = [dict(url_sha256=h(str(i)), time=None,
            update_types=['message_created']) for i in range(400)]
        canonical_bytes = len(json.dumps(p, sort_keys=True, ensure_ascii=True, separators=(',', ':')))
        assert canonical_bytes < 64 * 1024
        snapshot = store.snapshot()
        if snapshot['observation_bytes'] + canonical_bytes > 1024 * 1024:
            before = file_hash(path)
            with pytest.raises(port.ObservationStoreError) as error: store.append_observation(p)
            assert error.value.code == 'provider_observation_byte_limit'
            assert file_hash(path) == before and store.snapshot() == snapshot
            assert count > 1
            break
        store.append_observation(p)
        count += 1
        assert count < 32


def test_creation_descriptor_prevents_actual_substitution_before_first_commit(private, monkeypatch):
    port, path = api(), private / 'new.sqlite3'
    foreign = private / 'foreign-empty.sqlite3'
    foreign.write_bytes(b'')
    original, attempts = port.sqlite3.connect, []
    def substitute(database, *args, **kwargs):
        if str(database) == path.as_uri() + '?mode=rw':
            with pytest.raises(PermissionError): path.unlink()
            with pytest.raises(PermissionError): os.replace(foreign, path)
            attempts.append(True)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(port.sqlite3, 'connect', substitute)
    store = port.ObservationStore.create(path, binding())
    assert attempts and foreign.read_bytes() == b'' and store.snapshot()['observations'] == ()


def test_failed_initialization_retains_file_and_is_never_repaired(private, monkeypatch):
    port, path, bound = api(), private / 'crashed.sqlite3', binding()
    original = port.ObservationStore._validate
    def crash_after_schema(self, conn):
        original(self, conn)
        raise RuntimeError('synthetic initialization crash before commit')
    with monkeypatch.context() as patch:
        patch.setattr(port.ObservationStore, '_validate', crash_after_schema)
        with pytest.raises(RuntimeError): port.ObservationStore.create(path, bound)
    assert path.exists()
    before = file_hash(path)
    with pytest.raises(port.ObservationStoreError): port.ObservationStore.open_existing(path, bound)
    with pytest.raises(port.ObservationStoreError): port.ObservationStore.create(path, bound)
    assert file_hash(path) == before


def test_lost_companion_is_not_recreated_by_existing_object_or_reader(private):
    port, path, bound, store, verified = create(private, approved=True)
    store.append_observation(payload(bound, verified))
    path.unlink()
    with pytest.raises(port.ObservationStoreError): store.snapshot()
    with pytest.raises(port.ObservationStoreError): port.ObservationStore.open_existing(path, bound)
    assert not path.exists()


def test_empty_temp_schema_is_allowed_but_unknown_temp_object_is_not(private):
    port, path, _, store, _ = create(private)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TEMP TABLE harmless(value TEXT)')
        conn.execute('DROP TABLE harmless')
        assert store._validate(conn)['observations'] == ()
        conn.execute('CREATE TEMP VIEW caller_evidence AS SELECT 1')
        with pytest.raises(port.ObservationStoreError): store._validate(conn)


def test_oversized_or_wal_header_file_is_refused_without_recovery(private):
    port, path, _, store, _ = create(private)
    with path.open('r+b') as stream:
        stream.seek(18)
        stream.write(b'\x02\x02')
    before = file_hash(path)
    with pytest.raises(port.ObservationStoreError): store.snapshot()
    assert file_hash(path) == before
    with path.open('r+b') as stream:
        stream.seek(18)
        stream.write(b'\x01\x01')
        stream.truncate(8 * 1024 * 1024 + 1)
    before = file_hash(path)
    with pytest.raises(port.ObservationStoreError): store.snapshot()
    assert file_hash(path) == before


def test_production_label_does_not_change_diagnostic_state_or_mint_owner_grant(private):
    _, _, bound, store, verified = create(private, approved=True)
    store.append_observation(payload(bound, verified, kind='production_get'))
    snapshot = store.snapshot()
    assert snapshot['observations'][0]['payload']['evidence_kind'] == 'production_get'
    assert snapshot['activation_supported'] is False and snapshot['outbound_enabled'] is False
    assert not hasattr(store, 'grant') and not hasattr(store, 'owner_approval')
