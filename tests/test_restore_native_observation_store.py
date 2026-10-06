"""Real SQLite journal semantics in disposable Windows-private test folders."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid4

import pytest

from secretary.infrastructure import restore_native_observation_store as port
from secretary.infrastructure.restore_operator import VerifiedOperatorApproval, create_private_directory

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows retained HANDLE custody')


def h(value): return sha256(value.encode('ascii')).hexdigest()
def fingerprint(path): return sha256(path.read_bytes()).hexdigest()
def utc(): return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


@pytest.fixture
def case(tmp_path):
    folder = tmp_path / 'private'
    create_private_directory(folder)
    path = folder / 'managed-native.sqlite3'
    binding = dict(protocol=1, purpose='restore_managed_native_observation',
        restore_id=str(uuid4()), epoch_id=str(uuid4()), preparation_id=str(uuid4()),
        source_snapshot_sha256=h('source'), scope_sha256=h('scope'), binding_sha256=h('binding'))
    commitment = dict(binding=deepcopy(binding), evidence_kind='synthetic_managed_native',
        resources={key: h(key) for key in port._RESOURCES}, attempt_context_sha256=h('context'))
    store = port.NativeObservationStore.create(path, binding, private_folder=folder)
    return folder, path, binding, commitment, store


def approval(nonce='one', expires=None):
    return VerifiedOperatorApproval(h('operator'), h('approval:' + nonce), h('nonce:' + nonce),
        int(time.time()) + 120 if expires is None else expires)


def terminal(started, state='observed', code=None):
    return dict(state=state, evidence_kind=started['commitment']['evidence_kind'],
        code=code or ('managed_native_observed' if state == 'observed' else 'managed_native_process_failed'),
        started_at=started['started_at'], finished_at=utc(),
        **{key: h(key) if state == 'observed' else None for key in port._HASHES},
        diagnostic_only=True, activation_supported=False, outbound_enabled=False)


def finish(store, started, state='observed', code=None):
    return store.finish_attempt(started['attempt_id'], started['commitment_sha256'], terminal(started, state, code))


def test_fresh_store_binding_and_completed_attempt_survive_reopen(case):
    folder, path, binding, commitment, store = case
    assert store.snapshot()['attempts'] == ()
    started = store.start_attempt(approval(), commitment)
    assert str(UUID(started['attempt_id'])) == started['attempt_id']
    assert started['commitment_sha256'] == h(json.dumps(commitment, ensure_ascii=True,
        sort_keys=True, separators=(',', ':')))
    completed = finish(store, started)
    reopened = port.NativeObservationStore.open_existing(path, binding, private_folder=folder)
    snapshot = reopened.snapshot()
    assert snapshot == store.snapshot()
    assert snapshot['attempts'][0]['terminal'] == completed['terminal']
    assert snapshot['events'][1]['previous_record_sha256'] == started['record_sha256']
    assert snapshot['diagnostic_only'] is True
    assert snapshot['activation_supported'] is False and snapshot['outbound_enabled'] is False
    assert snapshot['attempts'][0]['commitment']['evidence_kind'] == 'synthetic_managed_native'


def test_started_attempt_blocks_replay_and_new_launch_even_after_reopen(case):
    folder, path, binding, commitment, store = case
    started = store.start_attempt(approval(), commitment)
    before = fingerprint(path)
    reopened = port.NativeObservationStore.open_existing(path, binding, private_folder=folder)
    for verified in (approval(), approval('fresh')):
        with pytest.raises(port.NativeObservationStoreError, match='unfinished'):
            reopened.start_attempt(verified, commitment)
    assert fingerprint(path) == before
    finish(reopened, started, 'unreconciled', 'native_readonly_get_unreconciled')
    with pytest.raises(port.NativeObservationStoreError, match='replay'):
        reopened.start_attempt(approval(), commitment)
    new = reopened.start_attempt(approval('fresh'), commitment)
    assert new['attempt_id'] != started['attempt_id']


def test_exact_commitment_and_single_terminal_required(case):
    _, path, _, commitment, store = case
    started = store.start_attempt(approval(), commitment)
    before = fingerprint(path)
    with pytest.raises(port.NativeObservationStoreError, match='commitment'):
        store.finish_attempt(started['attempt_id'], h('wrong'), terminal(started))
    assert fingerprint(path) == before
    finish(store, started)
    before = fingerprint(path)
    with pytest.raises(port.NativeObservationStoreError, match='terminal'): finish(store, started)
    assert fingerprint(path) == before


def test_approval_nonce_reuse_rejected_independently_of_digest(case):
    _, path, _, commitment, store = case
    verified = approval()
    started = store.start_attempt(verified, commitment)
    finish(store, started, 'failed')
    reused = VerifiedOperatorApproval(verified.operator_digest, h('different'), verified.nonce_hash, verified.expires_at)
    before = fingerprint(path)
    with pytest.raises(port.NativeObservationStoreError, match='replay'): store.start_attempt(reused, commitment)
    assert fingerprint(path) == before


def test_ttl_expiry_blocks_success_but_allows_terminal_failure(case, monkeypatch):
    _, _, _, commitment, store = case
    started = store.start_attempt(approval(), commitment)
    monkeypatch.setattr(port.time, 'time', lambda: started['expires_at'] + 1)
    with pytest.raises(port.NativeObservationStoreError, match='expired'): finish(store, started)
    finish(store, started, 'failed', 'managed_native_timeout')
    assert store.snapshot()['attempts'][0]['terminal']['state'] == 'failed'


@pytest.mark.parametrize('mutation', ['purpose', 'missing_resource', 'raw_secret', 'extra', 'malformed_sha'])
def test_invalid_commitments_are_rejected_without_consuming_approval(case, mutation):
    _, path, _, commitment, store = case
    bad = deepcopy(commitment)
    if mutation == 'purpose': bad['binding']['purpose'] = 'restore_provider_observation'
    elif mutation == 'missing_resource': bad['resources'].pop('binary_sha256')
    elif mutation == 'raw_secret': bad['resources']['credential_sha256'] = 'tk_SECRET'
    elif mutation == 'extra': bad['caller_verified'] = True
    else: bad['attempt_context_sha256'] = 'f' * 63
    before = fingerprint(path)
    with pytest.raises(port.NativeObservationStoreError): store.start_attempt(approval(), bad)
    assert fingerprint(path) == before
    assert store.start_attempt(approval(), commitment)['attempt_id']


@pytest.mark.parametrize('mutation', ['secret', 'activation', 'outbound', 'diagnostic', 'missing_hash',
    'different_kind', 'started', 'reversed', 'invalid_date', 'bad_code', 'unknown_attempt'])
def test_malformed_terminal_never_promotes_caller_claims(case, mutation):
    _, path, _, commitment, store = case
    started = store.start_attempt(approval(), commitment)
    value = terminal(started)
    attempt = started['attempt_id']
    if mutation == 'secret': value['token'] = 'SECRET'
    elif mutation in ('activation', 'outbound'): value[mutation + '_supported' if mutation == 'activation' else 'outbound_enabled'] = True
    elif mutation == 'diagnostic': value['diagnostic_only'] = 1
    elif mutation == 'missing_hash': value['custody_sha256'] = None
    elif mutation == 'different_kind': value['evidence_kind'] = 'managed_native_diagnostic'
    elif mutation == 'started': value['started_at'] = '2000-01-01T00:00:00Z'
    elif mutation == 'reversed': value['finished_at'] = '2000-01-01T00:00:00Z'
    elif mutation == 'invalid_date': value['finished_at'] = 'not-a-date'
    elif mutation == 'bad_code': value['code'] = 'managed_native_SECRET'
    else: attempt = str(uuid4())
    before = fingerprint(path)
    with pytest.raises(port.NativeObservationStoreError):
        store.finish_attempt(attempt, started['commitment_sha256'], value)
    assert fingerprint(path) == before and store.snapshot()['attempts'][0]['terminal'] is None


@pytest.mark.parametrize('stage', ['started', 'terminal'])
def test_precommit_failure_rolls_back_whole_transition(case, monkeypatch, stage):
    folder, path, binding, commitment, store = case
    started = store.start_attempt(approval(), commitment) if stage == 'terminal' else None
    before = store.snapshot()
    original = store._validate
    def fail_post_insert(conn):
        snapshot = original(conn)
        if len(snapshot['events']) > len(before['events']): raise RuntimeError('synthetic precommit failure')
        return snapshot
    with monkeypatch.context() as patch:
        patch.setattr(store, '_validate', fail_post_insert)
        with pytest.raises(RuntimeError):
            if stage == 'started': store.start_attempt(approval(), commitment)
            else: finish(store, started)
    reopened = port.NativeObservationStore.open_existing(path, binding, private_folder=folder)
    assert reopened.snapshot() == before
    if stage == 'started': assert reopened.start_attempt(approval(), commitment)['attempt_id']
    else: assert finish(reopened, started)['terminal']['state'] == 'observed'


@pytest.mark.parametrize('mutation', ['extra_schema', 'trigger', 'payload', 'chain'])
def test_schema_and_hash_tampering_refused_without_repair(case, mutation):
    folder, path, binding, commitment, store = case
    started = store.start_attempt(approval(), commitment)
    finish(store, started)
    with sqlite3.connect(path) as conn:
        if mutation == 'extra_schema': conn.execute('CREATE TABLE hostile(payload TEXT)')
        elif mutation == 'trigger': conn.execute('DROP TRIGGER native_events_immutable_delete')
        else:
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='native_events_immutable_update'").fetchone()[0]
            conn.execute('DROP TRIGGER native_events_immutable_update')
            if mutation == 'chain': conn.execute('UPDATE native_events SET record_sha256=? WHERE seq=2', (h('forged'),))
            else: conn.execute("UPDATE native_events SET payload=json_set(payload,'$.terminal.activation_supported',1) WHERE seq=2")
            conn.execute(ddl)
    before = fingerprint(path)
    with pytest.raises(port.NativeObservationStoreError): store.snapshot()
    with pytest.raises(port.NativeObservationStoreError):
        port.NativeObservationStore.open_existing(path, binding, private_folder=folder)
    assert fingerprint(path) == before


def test_existing_readers_use_query_only_and_never_recreate(case, monkeypatch):
    folder, path, binding, _, store = case
    original, flags = store._validate, []
    def validate(conn):
        flags.append(conn.execute('PRAGMA query_only').fetchone()[0])
        return original(conn)
    monkeypatch.setattr(store, '_validate', validate)
    before = fingerprint(path)
    store.snapshot()
    assert flags == [1] and fingerprint(path) == before
    path.unlink()
    with pytest.raises(port.NativeObservationStoreError): store.snapshot()
    with pytest.raises(port.NativeObservationStoreError):
        port.NativeObservationStore.open_existing(path, binding, private_folder=folder)
    assert not path.exists()


def test_sql_history_is_immutable_and_stopped_sidecars_are_rejected(case):
    _, path, _, commitment, store = case
    started = store.start_attempt(approval(), commitment)
    finish(store, started)
    before = fingerprint(path)
    with sqlite3.connect(path) as conn:
        for table in ('native_binding', 'native_events'):
            for sql in (f'DELETE FROM {table}', f'UPDATE {table} SET rowid=rowid',
                    f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}'):
                with pytest.raises(sqlite3.IntegrityError): conn.execute(sql)
                conn.rollback()
    for suffix in ('-journal', '-wal', '-shm'):
        sidecar = Path(str(path) + suffix)
        sidecar.write_bytes(b'stopped synthetic sidecar')
        with pytest.raises(port.NativeObservationStoreError): store.snapshot()
        assert sidecar.read_bytes() == b'stopped synthetic sidecar'
        sidecar.unlink()
    assert fingerprint(path) == before


def test_exclusive_create_and_bound_private_folder(case, tmp_path):
    folder, path, binding, _, _ = case
    before = fingerprint(path)
    for target, parent in ((path, folder), (path, tmp_path), (Path('relative.sqlite3'), folder)):
        with pytest.raises(port.NativeObservationStoreError):
            port.NativeObservationStore.create(target, binding, private_folder=parent)
    assert fingerprint(path) == before


def test_provider_entrypoints_and_arbitrary_error_strings_are_closed(case):
    _, _, _, _, store = case
    for call in (lambda: store.consume_approval(approval()), lambda: store.append_observation({})):
        with pytest.raises(port.NativeObservationStoreError): call()
    for value in ('RAW_SECRET', [], {}, None, True):
        error = port.NativeObservationStoreError(value)
        assert str(error) == error.code == 'native_observation_store_invalid'


def test_failed_initialization_retains_unusable_file_and_never_repairs(case, monkeypatch):
    folder, _, binding, _, _ = case
    path = folder / 'failed-initialization.sqlite3'
    original = port.NativeObservationStore._validate
    def fail(conn_self, conn):
        original(conn_self, conn)
        raise RuntimeError('synthetic initialization interruption')
    with monkeypatch.context() as patch:
        patch.setattr(port.NativeObservationStore, '_validate', fail)
        with pytest.raises(RuntimeError):
            port.NativeObservationStore.create(path, binding, private_folder=folder)
    before = fingerprint(path)
    for method in (port.NativeObservationStore.create, port.NativeObservationStore.open_existing):
        with pytest.raises(port.NativeObservationStoreError): method(path, binding, private_folder=folder)
    assert fingerprint(path) == before


@pytest.mark.parametrize('kind', ['expired', 'too_far', 'dict', 'forged'])
def test_malformed_or_stale_approvals_never_start(case, kind):
    _, path, _, commitment, store = case
    if kind == 'expired': verified = approval(expires=int(time.time()) - 1)
    elif kind == 'too_far': verified = approval(expires=int(time.time()) + 3600)
    elif kind == 'dict': verified = vars(approval())
    else:
        verified = approval()
        object.__setattr__(verified, 'approval_digest', 'SECRET')
    before = fingerprint(path)
    with pytest.raises(port.NativeObservationStoreError): store.start_attempt(verified, commitment)
    assert fingerprint(path) == before and store.snapshot()['events'] == ()


@pytest.mark.parametrize('namespace', ['attach', 'temp'])
def test_foreign_namespace_and_temp_shadow_are_rejected(case, namespace):
    _, path, _, _, store = case
    before = fingerprint(path)
    with sqlite3.connect(path) as conn:
        if namespace == 'attach': conn.execute("ATTACH DATABASE ':memory:' AS foreign_store")
        else: conn.execute('CREATE TEMP TABLE native_events(payload TEXT)')
        with pytest.raises(port.NativeObservationStoreError): store._validate(conn)
    assert fingerprint(path) == before


def test_capacity_reserves_terminal_and_no_event_can_overflow(case):
    _, path, _, commitment, store = case
    for index in range(64):
        started = store.start_attempt(approval(str(index)), commitment)
        finish(store, started, 'failed')
    before = fingerprint(path)
    with pytest.raises(port.NativeObservationStoreError, match='limit'):
        store.start_attempt(approval('overflow'), commitment)
    assert len(store.snapshot()['events']) == 128 and fingerprint(path) == before


def test_exact_binding_change_is_rejected_without_readback_mutation(case):
    folder, path, binding, _, _ = case
    before = fingerprint(path)
    for key in ('restore_id', 'epoch_id', 'preparation_id', 'source_snapshot_sha256', 'scope_sha256', 'binding_sha256'):
        changed = deepcopy(binding)
        changed[key] = str(uuid4()) if key.endswith('_id') else h('changed')
        with pytest.raises(port.NativeObservationStoreError):
            port.NativeObservationStore.open_existing(path, changed, private_folder=folder)
    assert fingerprint(path) == before


@pytest.mark.parametrize('collision', ['approval_digest', 'nonce_hash', 'record_sha256', 'attempt_kind'])
def test_sequential_replace_collision_cannot_delete_historical_event(case, collision):
    """New sequence/head must not let SQLite REPLACE silently delete an old row."""
    _, path, _, commitment, store = case
    first = store.start_attempt(approval('historical-one'), commitment)
    finish(store, first, 'failed')
    second = store.start_attempt(approval('historical-two'), commitment)
    finish(store, second, 'failed')
    snapshot = store.snapshot()
    verified = approval('replacement')
    attempt_id = first['attempt_id'] if collision == 'attempt_kind' else str(uuid4())
    payload = dict(attempt_id=attempt_id, commitment=deepcopy(commitment),
        commitment_sha256=second['commitment_sha256'], operator_digest=verified.operator_digest,
        operator_approval_sha256=first['operator_approval_sha256'] if collision == 'approval_digest'
            else verified.approval_digest,
        nonce_hash=first['nonce_hash'] if collision == 'nonce_hash' else verified.nonce_hash,
        expires_at=verified.expires_at, started_at=utc())
    previous = snapshot['events'][-1]['record_sha256']
    record = store._record(snapshot['store_id'], snapshot['binding_digest_sha256'],
        5, 'started', attempt_id, payload, previous)
    # Each candidate satisfies the sequence, current-head and completed-history
    # guards; only one UNIQUE value collides with the first historical start.
    record_hash = first['record_sha256'] if collision == 'record_sha256' else record['record_sha256']
    incoming = (5, 'started', attempt_id, json.dumps(payload, ensure_ascii=True,
        sort_keys=True, separators=(',', ':')), record['payload_sha256'], previous,
        record_hash, payload['operator_approval_sha256'], payload['nonce_hash'])
    before = fingerprint(path)
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        assert conn.execute('PRAGMA recursive_triggers').fetchone() == (0,)
        history = conn.execute('SELECT * FROM native_events ORDER BY seq').fetchall()
        binding = conn.execute('SELECT * FROM native_binding').fetchall()
        conn.execute('BEGIN')
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('INSERT OR REPLACE INTO native_events VALUES(?,?,?,?,?,?,?,?,?)', incoming)
            assert conn.execute('SELECT * FROM native_events ORDER BY seq').fetchall() == history
            assert conn.execute('SELECT * FROM native_binding').fetchall() == binding
        finally:
            # A RED run must not leave the disposable database damaged either.
            conn.rollback()
    assert fingerprint(path) == before and store.snapshot() == snapshot
