"""Source publication evidence and dispatch recovery, isolated SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from importlib import import_module
from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest


def uid():
    return str(uuid4())


def api():
    assert (Path(__file__).resolve().parents[1] / 'backend/secretary/infrastructure/task_publication_repository.py').is_file(), 'T5 repository absent'
    names = {}
    for module in ('domain.team', 'domain.task_delivery', 'infrastructure.database',
                   'infrastructure.task_publication_repository'):
        names.update(vars(import_module('secretary.' + module)))
    return SimpleNamespace(**names)


@pytest.fixture
def case(tmp_path):
    a = api()
    db = a.Database(tmp_path / 'source.sqlite3')
    meeting = db.create_meeting('Synthetic publication')['id']
    db.execute('UPDATE meetings SET transcript_version=1 WHERE id=?', (meeting,))
    db.execute('INSERT INTO summaries VALUES(?,?,?,?)', (meeting, 1, 1, '{}'))
    clock = [datetime(2026, 10, 3, 10, tzinfo=timezone.utc)]
    repo = a.TaskPublicationRepository(db, clock=lambda: clock[0])
    actor = uid()
    scope = a.PublicationScope(meeting_id=meeting, transcript_version=1, summary_version=1, destination_project_id='7')
    item = a.PublicationTask(action_id='action-1', title='Synthetic task', assignee_id=uid(),
        source_segment_ids=('segment-1',), evidence_quote='Agreed synthetic action', due_confirmed=True,
        source_fingerprint='assignment-evidence-v1:' + 'a' * 64,
        publication_id=a.publication_uuid(scope, 'action-1'))
    preview = a.PublishPreview(preview_id=uid(), scope=scope, actor_id=actor,
        expires_at=clock[0] + timedelta(minutes=5),
        watermarks=a.PublicationWatermarks(assignment_revision=1, roster_revision=2,
            attribution_revision=3, context_hash='b' * 64), items=(item,))
    return SimpleNamespace(a=a, db=db, repo=repo, clock=clock, actor=actor, meeting=meeting,
                           scope=scope, item=item, preview=preview)


def command(c, preview=None, **changes):
    preview = preview or c.preview
    return c.a.PublishCommand(**{**preview.model_dump(exclude={'preview_hash'}),
        'preview_hash': preview.preview_hash, 'operation_id': uid(), **changes})


def accepted(c, preview=None):
    preview = preview or c.preview
    c.repo.save_preview(preview)
    cmd = command(c, preview)
    return cmd, c.repo.accept(c.actor, cmd, lambda conn, stored: None)


def started(c):
    cmd, receipt = accepted(c)
    claim = c.repo.claim_next('worker')
    claim = c.repo.mark_started(claim, lambda *args: None, expected_payload_hash='f' * 64)
    return cmd, receipt, claim


def gateway(c, delivery_id, state='queued', revision=0, **changes):
    task_id = '9007199254740997' if state == 'applied' else None
    execution = c.a.ExecutionState(state=state, revision=revision, task_id=task_id,
        verified_at=c.clock[0] if state == 'applied' else None)
    snapshot = c.a.TaskSnapshot(task_id=task_id, project_id='7', revision=0,
        remote_fingerprint='c' * 64, title=c.item.title, assignee_id=c.item.assignee_id) if task_id else None
    return c.a.CommandReceipt(**{'acceptance_receipt': c.a.AcceptanceReceipt(operation_id=delivery_id,
        payload_hash='f' * 64, decision='accepted', decided_at=c.clock[0]),
        'execution_state': execution, 'current': snapshot, **changes})


def test_migration_nine_is_idempotent_and_foreign_keys_are_valid(case):
    c = case
    assert c.db.one('SELECT MAX(version) AS version FROM schema_migrations')['version'] == 9
    c.a.Database(c.db.path)
    with c.db.connection() as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_preview_is_actor_bound_immutable_and_computed_hash_roundtrips(case):
    c = case
    assert c.repo.save_preview(c.preview) == c.preview
    assert c.repo.save_preview(c.preview) == c.preview
    assert c.repo.get_preview(c.preview.preview_id, c.actor) == c.preview
    with pytest.raises(c.a.TeamForbidden):
        c.repo.get_preview(c.preview.preview_id, uid())
    with pytest.raises(c.a.TeamConflict):
        c.repo.save_preview(c.preview.model_copy(update={'expires_at': c.preview.expires_at + timedelta(seconds=1)}))


def test_acceptance_replay_precedes_expiry_and_callback_but_payload_and_actor_cannot_change(case):
    c = case
    cmd, receipt = accepted(c)
    c.clock[0] += timedelta(days=1)
    def must_not_validate(*args):
        raise AssertionError('accepted replay must not revalidate source')
    assert c.repo.accept(c.actor, cmd, must_not_validate) == receipt
    with pytest.raises(c.a.TeamConflict):
        c.repo.accept(uid(), cmd, must_not_validate)
    with pytest.raises(c.a.TeamConflict):
        c.repo.accept(c.actor, cmd.model_copy(update={'preview_hash': 'd' * 64}), must_not_validate)


@pytest.mark.parametrize('change', ['scope', 'watermarks', 'items', 'hash', 'actor', 'expired'])
def test_new_command_must_exactly_match_fresh_stored_preview(case, change):
    c = case
    c.repo.save_preview(c.preview)
    cmd = command(c)
    if change == 'scope':
        cmd = cmd.model_copy(update={'scope': c.scope.model_copy(update={'destination_project_id': '8'})})
    elif change == 'watermarks':
        cmd = cmd.model_copy(update={'watermarks': c.preview.watermarks.model_copy(update={'assignment_revision': 2})})
    elif change == 'items':
        cmd = cmd.model_copy(update={'items': (c.item.model_copy(update={'title': 'Changed'}),)})
    elif change == 'hash':
        cmd = cmd.model_copy(update={'preview_hash': 'c' * 64})
    elif change == 'actor':
        cmd = cmd.model_copy(update={'actor_id': uid()})
    else:
        c.clock[0] += timedelta(minutes=6)
    with pytest.raises((c.a.TeamConflict, c.a.TeamForbidden)):
        c.repo.accept(c.actor, cmd, lambda *args: None)
    assert c.repo.claim_next('worker') is None


def test_source_validation_runs_inside_accept_transaction_and_rollback_leaves_no_delivery(case):
    c = case
    c.repo.save_preview(c.preview)
    cmd = command(c)
    def refuse(conn, preview):
        assert conn.in_transaction and preview == c.preview
        conn.execute("UPDATE meetings SET title='temporary callback write' WHERE id=?", (c.meeting,))
        raise c.a.TeamConflict('source_changed')
    with pytest.raises(c.a.TeamConflict, match='source_changed'):
        c.repo.accept(c.actor, cmd, refuse)
    assert c.db.meeting(c.meeting)['title'] == 'Synthetic publication'
    assert c.repo.publications(c.meeting, '7') == () and c.repo.list(c.meeting, c.actor) == ()
    assert c.repo.accept(c.actor, cmd, lambda *args: None).acceptance_receipt.decision == 'accepted'


def test_different_batches_reuse_same_delivery_once_and_keep_original_watermarks(case):
    c = case
    first_cmd, first = accepted(c)
    newer = c.preview.model_copy(update={'preview_id': uid(),
        'watermarks': c.preview.watermarks.model_copy(update={'assignment_revision': 2})})
    seen = []
    second_cmd, second = accepted(c, newer)
    assert first_cmd.operation_id != second_cmd.operation_id
    assert first.items[0].delivery_operation_id == second.items[0].delivery_operation_id
    values = c.repo.publications(c.meeting, '7')
    assert len(values) == 1 and values[0].watermarks == c.preview.watermarks
    assert c.repo.claim_next('one') is not None and c.repo.claim_next('two') is None


def test_same_delivery_with_changed_creation_item_conflicts_atomically(case):
    c = case
    accepted(c)
    newer = c.preview.model_copy(update={'preview_id': uid(), 'items': (c.item.model_copy(update={'title': 'Changed'}),)})
    c.repo.save_preview(newer)
    with pytest.raises(c.a.TeamConflict):
        c.repo.accept(c.actor, command(c, newer), lambda *args: None)
    assert len(c.repo.list(c.meeting, c.actor)) == 1


def test_claim_is_exclusive_and_expired_never_started_work_can_be_claimed_once(case):
    c = case
    accepted(c)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(c.repo.claim_next, ('one', 'two')))
    first = next(claim for claim in claims if claim)
    assert sum(claim is not None for claim in claims) == 1
    c.clock[0] += timedelta(seconds=61)
    second = c.repo.claim_next('replacement')
    assert second.delivery_operation_id == first.delivery_operation_id and second.fence > first.fence
    with pytest.raises(c.a.TeamConflict):
        c.repo.mark_started(first, lambda *args: None, expected_payload_hash='f' * 64)


def test_mark_started_is_durable_before_post_and_cannot_be_replayed(case):
    c = case
    cmd, receipt, claim = started(c)
    assert claim.dispatch_started and claim.gateway_payload_hash == 'f' * 64
    with pytest.raises(c.a.TeamConflict):
        c.repo.mark_started(claim, lambda *args: None, expected_payload_hash='f' * 64)
    restarted = c.a.TaskPublicationRepository(c.a.Database(c.db.path), clock=lambda: c.clock[0])
    c.clock[0] += timedelta(seconds=61)
    assert restarted.claim_next('replacement') is None
    ongoing = restarted.polling()
    assert len(ongoing) == 1 and ongoing[0].execution_state.state == 'uncertain'
    assert restarted.read(cmd.operation_id, c.actor).items[0].execution_state.state == 'uncertain'


def test_dispatch_source_callback_is_atomic_and_leaves_no_started_marker_on_refusal(case):
    c = case
    accepted(c)
    claim = c.repo.claim_next('worker')
    def refuse(conn, scope, watermarks, item, actor):
        assert conn.in_transaction and (scope, watermarks, item, actor) == (c.scope, c.preview.watermarks, c.item, c.actor)
        raise c.a.TeamConflict('source_stale')
    with pytest.raises(c.a.TeamConflict, match='source_stale'):
        c.repo.mark_started(claim, refuse, expected_payload_hash='f' * 64)
    record = c.repo.publications(c.meeting, '7')[0]
    assert not record.dispatch_started and record.gateway_payload_hash is None
    c.repo.fail_claim(claim, state='rejected', error_code='source_stale')
    assert c.repo.claim_next('another') is None


def test_gateway_ack_queued_remains_pollable_and_only_verified_applied_is_terminal(case):
    c = case
    cmd, _, claim = started(c)
    ack = gateway(c, claim.delivery_operation_id)
    record = c.repo.update_gateway(claim.delivery_operation_id, ack, expected_payload_hash='f' * 64)
    assert record.execution_state.state == 'queued'
    assert c.repo.claim_next('other') is None and len(c.repo.polling()) == 1
    result = gateway(c, claim.delivery_operation_id, state='applied', revision=2)
    c.repo.update_gateway(claim.delivery_operation_id, result, expected_payload_hash='f' * 64)
    receipt = c.repo.read(cmd.operation_id, c.actor)
    assert receipt.items[0].execution_state.state == 'applied'
    assert receipt.items[0].execution_state.task_id == result.current.task_id
    assert c.repo.polling() == ()
    assert c.repo.update_gateway(claim.delivery_operation_id, result, expected_payload_hash='f' * 64).gateway_receipt == result
    with pytest.raises(c.a.TeamConflict):
        c.repo.update_gateway(claim.delivery_operation_id, ack, expected_payload_hash='f' * 64)


@pytest.mark.parametrize('damage', ['operation', 'hash', 'project', 'task', 'missing-proof'])
def test_gateway_receipt_must_match_bound_operation_payload_and_applied_scope(case, damage):
    c = case
    _, _, claim = started(c)
    value = gateway(c, claim.delivery_operation_id, state='applied', revision=1)
    if damage == 'operation':
        value = value.model_copy(update={'acceptance_receipt': value.acceptance_receipt.model_copy(update={'operation_id': uid()})})
    elif damage == 'hash':
        value = value.model_copy(update={'acceptance_receipt': value.acceptance_receipt.model_copy(update={'payload_hash': 'a' * 64})})
    elif damage == 'project':
        value = value.model_copy(update={'current': value.current.model_copy(update={'project_id': '8'})})
    elif damage == 'task':
        value = value.model_copy(update={'current': value.current.model_copy(update={'task_id': '99'})})
    else:
        value = value.model_copy(update={'current': None})
    with pytest.raises(c.a.TeamConflict):
        c.repo.update_gateway(claim.delivery_operation_id, value, expected_payload_hash='f' * 64)
    assert c.repo.polling()[0].gateway_receipt is None


def test_read_and_list_do_not_expose_other_actor_receipts(case):
    c = case
    cmd, _ = accepted(c)
    with pytest.raises(c.a.TeamForbidden):
        c.repo.read(cmd.operation_id, uid())
    assert c.repo.list(c.meeting, uid()) == ()
    assert len(c.repo.list(c.meeting, c.actor)) == 1


def test_poll_error_preserves_bound_receipt_and_identical_verified_get_clears_uncertainty(case):
    c = case
    _, _, claim = started(c)
    ack = gateway(c, claim.delivery_operation_id)
    c.repo.update_gateway(claim.delivery_operation_id, ack, expected_payload_hash='f' * 64)
    failed = c.repo.note_poll_error(claim.delivery_operation_id, 'gateway_get_timeout')
    assert failed.execution_state.state == 'uncertain' and failed.gateway_receipt == ack
    assert failed.gateway_payload_hash == 'f' * 64
    assert c.repo.note_poll_error(claim.delivery_operation_id, 'gateway_get_timeout') == failed
    assert c.repo.claim_next('never-resend') is None
    restored = c.repo.update_gateway(claim.delivery_operation_id, ack, expected_payload_hash='f' * 64)
    assert restored.execution_state.state == 'queued' and restored.execution_state.error_code is None
    assert restored.execution_state.revision > failed.execution_state.revision
    assert restored.gateway_receipt.execution_state.revision == 0


def test_poll_error_cannot_start_unstarted_delivery_or_regress_terminal(case):
    c = case
    _, receipt = accepted(c)
    delivery = receipt.items[0].delivery_operation_id
    with pytest.raises(c.a.TeamConflict):
        c.repo.note_poll_error(delivery, 'gateway_get_timeout')
    claim = c.repo.mark_started(c.repo.claim_next('worker'), lambda *args: None, expected_payload_hash='f' * 64)
    c.repo.update_gateway(delivery, gateway(c, delivery, 'applied', 1), expected_payload_hash='f' * 64)
    with pytest.raises(c.a.TeamConflict):
        c.repo.note_poll_error(delivery, 'gateway_get_timeout')


def test_gateway_update_cannot_bind_hash_after_start_or_replace_prebound_hash(case):
    c = case
    _, receipt = accepted(c)
    delivery = receipt.items[0].delivery_operation_id
    with pytest.raises(c.a.TeamConflict):
        c.repo.update_gateway(delivery, gateway(c, delivery), expected_payload_hash='f' * 64)
    c.repo.mark_started(c.repo.claim_next('worker'), lambda *args: None, expected_payload_hash='a' * 64)
    with pytest.raises(c.a.TeamConflict):
        c.repo.update_gateway(delivery, gateway(c, delivery), expected_payload_hash='f' * 64)


def test_expiry_during_acceptance_validation_rolls_back_before_acceptance(case):
    c = case
    c.repo.save_preview(c.preview)
    def too_slow(*args):
        c.clock[0] += timedelta(minutes=6)
    with pytest.raises(c.a.TeamConflict, match='preview_expired'):
        c.repo.accept(c.actor, command(c), too_slow)
    assert c.repo.list(c.meeting, c.actor) == ()


def test_lease_expiry_inside_start_callback_cannot_authorize_dispatch(case):
    c = case
    accepted(c)
    claim = c.repo.claim_next('worker')
    def too_slow(*args):
        c.clock[0] += timedelta(seconds=61)
    with pytest.raises(c.a.TeamConflict):
        c.repo.mark_started(claim, too_slow, expected_payload_hash='f' * 64)
    assert not c.repo.publications(c.meeting, '7')[0].dispatch_started


def test_forged_claim_actor_item_and_expired_worker_cannot_change_execution(case):
    c = case
    accepted(c)
    claim = c.repo.claim_next('worker')
    for forged in (replace(claim, actor_id=uid()), replace(claim, fence=claim.fence + 1),
                   replace(claim, item=claim.item.model_copy(update={'title': 'Injected'}))):
        with pytest.raises(c.a.TeamConflict):
            c.repo.fail_claim(forged, state='rejected', error_code='source_stale')
    c.clock[0] += timedelta(seconds=61)
    with pytest.raises(c.a.TeamConflict):
        c.repo.fail_claim(claim, state='uncertain', error_code='timeout')


def test_identical_proposal_reuses_original_target_guard_after_poll(case):
    c = case
    first_item = c.item.model_copy(update={'intent': 'propose_update', 'target_publication_id': uid(),
        'target_task_id': '123', 'expected_task_revision': 1, 'expected_task_fingerprint': 'd' * 64})
    first_preview = c.preview.model_copy(update={'items': (first_item,)})
    _, first = accepted(c, first_preview)
    newer_item = first_item.model_copy(update={'expected_task_revision': 5, 'expected_task_fingerprint': 'e' * 64})
    newer_preview = first_preview.model_copy(update={'preview_id': uid(), 'items': (newer_item,)})
    _, second = accepted(c, newer_preview)
    assert first.items[0].delivery_operation_id == second.items[0].delivery_operation_id
    records = c.repo.publications(c.meeting, '7')
    assert len(records) == 1 and records[0].item.expected_task_revision == 1


@pytest.mark.parametrize('table', ['task_publication_previews', 'task_publication_commands',
    'task_publication_items', 'task_publication_batch_items', 'task_publication_outbox'])
def test_replace_is_blocked_even_when_recursive_triggers_are_disabled(case, table):
    c = case
    started(c)
    with c.db.connection() as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        row = conn.execute(f'SELECT * FROM {table} LIMIT 1').fetchone()
        placeholders = ','.join('?' for _ in row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f'INSERT OR REPLACE INTO {table} VALUES({placeholders})', tuple(row))


@pytest.mark.parametrize('table', ['task_publication_previews', 'task_publication_commands',
    'task_publication_items', 'task_publication_batch_items'])
def test_immutable_evidence_rejects_update_and_delete(case, table):
    c = case
    accepted(c)
    with c.db.connection() as conn:
        column = conn.execute(f'PRAGMA table_info({table})').fetchone()['name']
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f'UPDATE {table} SET {column}={column}')
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f'DELETE FROM {table}')


def test_sql_rejects_clearing_started_hash_or_fabricating_applied_without_proof(case):
    c = case
    _, _, claim = started(c)
    with c.db.connection() as conn:
        for statement in (
            'UPDATE task_publication_outbox SET gateway_payload_hash=NULL',
            'UPDATE task_publication_outbox SET started_at=NULL',
            "UPDATE task_publication_outbox SET state='applied',execution=json_set(execution,'$.state','applied')"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)


def test_read_detects_gateway_receipt_scope_corruption_even_if_json_is_valid(case):
    c = case
    cmd, _, claim = started(c)
    c.repo.update_gateway(claim.delivery_operation_id, gateway(c, claim.delivery_operation_id, 'applied', 1), expected_payload_hash='f' * 64)
    with c.db.connection() as conn:
        conn.execute('DROP TRIGGER task_publication_outbox_monotonic')  # synthetic corrupt storage, not supported API
        conn.execute("UPDATE task_publication_outbox SET gateway_receipt=json_set(gateway_receipt,'$.current.project_id','8')")
    with pytest.raises(c.a.TeamConflict):
        c.repo.read(cmd.operation_id, c.actor)


def test_concurrent_acceptance_of_same_operation_yields_one_immutable_batch(case):
    c = case
    c.repo.save_preview(c.preview)
    cmd = command(c)
    def accept_once(_):
        return c.repo.accept(c.actor, cmd, lambda *args: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(accept_once, range(2)))
    assert receipts[0] == receipts[1] and len(c.repo.list(c.meeting, c.actor)) == 1


def test_migration_failure_rolls_back_all_new_tables_and_can_retry(tmp_path, monkeypatch):
    a = api()
    module = import_module('secretary.infrastructure.task_publication_repository')
    migrate = module.migrate_task_publications
    monkeypatch.setattr(module, 'migrate_task_publications', lambda db: None)
    db = a.Database(tmp_path / 'schema8.sqlite3')
    monkeypatch.setattr(module, 'migrate_task_publications', migrate)
    statements = module.PUBLICATION_MIGRATION
    monkeypatch.setattr(module, 'PUBLICATION_MIGRATION', (*statements[:2], 'INVALID SQL FOR ROLLBACK'))
    with pytest.raises(sqlite3.OperationalError):
        migrate(db)
    assert db.one('SELECT MAX(version) AS version FROM schema_migrations')['version'] == 8
    assert db.one("SELECT name FROM sqlite_master WHERE name='task_publication_previews'") is None
    monkeypatch.setattr(module, 'PUBLICATION_MIGRATION', statements)
    migrate(db)
    assert db.one('SELECT MAX(version) AS version FROM schema_migrations')['version'] == 9


def test_gateway_rejected_conflict_is_a_legitimate_terminal_t3_receipt(case):
    c = case
    _, _, claim = started(c)
    receipt = gateway(c, claim.delivery_operation_id, 'conflict', 1)
    receipt = receipt.model_copy(update={'acceptance_receipt': receipt.acceptance_receipt.model_copy(update={
        'decision': 'rejected', 'error_code': 'task_revision_conflict'})})
    result = c.repo.update_gateway(claim.delivery_operation_id, receipt, expected_payload_hash='f' * 64)
    assert result.execution_state.state == 'conflict' and c.repo.polling() == ()


@pytest.mark.parametrize('state', ['conflict', 'rejected'])
def test_local_failure_after_start_cannot_claim_definite_terminal_outcome(case, state):
    c = case
    _, _, claim = started(c)
    with pytest.raises(c.a.TeamConflict):
        c.repo.fail_claim(claim, state=state, error_code='gateway_unavailable')
    assert c.repo.fail_claim(claim, state='uncertain', error_code='gateway_unavailable').execution_state.state == 'uncertain'


def test_sql_cannot_rewrite_terminal_receipt_or_execution_at_same_revision(case):
    c = case
    _, _, claim = started(c)
    c.repo.update_gateway(claim.delivery_operation_id, gateway(c, claim.delivery_operation_id, 'applied', 1), expected_payload_hash='f' * 64)
    with c.db.connection() as conn:
        for statement in (
            "UPDATE task_publication_outbox SET gateway_receipt=json_set(gateway_receipt,'$.current.project_id','8')",
            "UPDATE task_publication_outbox SET execution=json_set(execution,'$.verified_at',NULL)"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)


def test_polling_cursor_reaches_101st_uncertain_delivery_and_survives_terminal_cursor(case):
    c = case
    items = tuple(c.item.model_copy(update={'action_id': f'action-{number}',
        'publication_id': c.a.publication_uuid(c.scope, f'action-{number}')}) for number in range(101))
    for part in (items[:100], items[100:]):
        preview = c.preview.model_copy(update={'preview_id': uid(), 'items': part})
        accepted(c, preview)
    for number in range(101):
        claim = c.repo.claim_next('dispatcher')
        claim = c.repo.mark_started(claim, lambda *args: None, expected_payload_hash='f' * 64)
        c.repo.fail_claim(claim, state='uncertain', error_code='lost_ack')
    first = c.repo.polling(limit=100)
    assert len(first) == 100
    cursor = first[-1].delivery_operation_id
    second = c.repo.polling(limit=100, after_delivery_operation_id=cursor)
    assert len(second) == 1 and second[0].delivery_operation_id not in {r.delivery_operation_id for r in first}
    c.repo.update_gateway(cursor, gateway(c, cursor, 'applied', 1), expected_payload_hash='f' * 64)
    assert c.repo.polling(limit=100, after_delivery_operation_id=cursor) == second
    assert c.repo.polling(limit=100, after_delivery_operation_id=second[-1].delivery_operation_id) == ()
    with pytest.raises(c.a.TeamConflict, match='cursor'):
        c.repo.polling(after_delivery_operation_id=uid())
