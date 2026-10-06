"""Checkpoint a proven dead gateway before retiring its maintenance tickets.

Only the current gateway worker layout is supported. Source transactions commit
independently: interruption leaves conservative source states and active tickets;
retry is safe. This module never releases reservations, sends requests, or opens
a database at import time. Secretary retirement stays inside its owned admission.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import quote
from uuid import UUID, uuid4

from .budget_repository import BudgetRepository
from .team_database import TeamDatabase
from .team_maintenance import MaintenanceError, MaintenanceTicket, RecoveryCheckpoint, _AMBIENT
from .team_process_identity import DeadProcessProof, process_identity, verify_dead_identity
from .team_repository import TeamRepository
from .team_sync_repository import TeamSyncRepository
from .voice_repository import VoiceRepository
from secretary.domain.voice_commands import VoiceError


# The original Secretary/capture/publication pipelines use the separate helper.
_KINDS = frozenset({'sql', 'intake', 'job_startup', 'job_bot', 'job_voice',
    'job_planning', 'job_budget_refresh', 'job_cleanup', 'outbound_task',
    'outbound_bot', 'outbound_notification', 'sync', 'sync_max_identity',
    'polza_poll', 'outbound_polza', 'job_subscription', 'outbound_subscription', 'crash_recovery'})
_WORKERS = {'team_execution': 'task', 'bot_event_state': 'inbox',
    'bot_reply_state': 'bot', 'voice_job_state': 'voice',
    'voice_notice_state': 'notice', 'notification_state': 'reminder'}
_ACTIVE_STATES = {'team_execution': ('running', 'reconciling'),
    'bot_event_state': ('processing',), 'bot_reply_state': ('sending',),
    'voice_job_state': ('processing',), 'voice_notice_state': ('sending',),
    # A notification preparing claim still has durable state=pending.
    'notification_state': ('pending', 'sending')}
_TEAM_TABLES = (*_WORKERS, 'team_message_state', 'team_resources', 'team_journal',
    'voice_requests', 'voice_responses', 'voice_request_aborts', 'team_sync_state', 'team_sync_results')
_NATIVE_KINDS = frozenset({'job', 'background_job', 'job_voice', 'job_cleanup',
    'job_startup', 'intake'})


def _fail(code='maintenance_recovery_invalid'):
    raise MaintenanceError(code)


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), allow_nan=False)


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            _fail()
    except (ValueError, TypeError, AttributeError):
        _fail()
    return value


def _proof_matches(proof, identity):
    return (isinstance(proof, DeadProcessProof) and proof.state in ('exited', 'pid_reused')
        and all(getattr(proof, key, None) == value for key, value in identity.items()))


def _require_native_containment(maintenance, participant_id, run_id, tickets, *,
    gateway_path=None, secretary_path=None):
    """A new run's Job cannot prove that old native children died with it."""
    if maintenance.has_native_containment(participant_id, run_id):
        return
    if any(ticket.kind in _NATIVE_KINDS or ticket.kind.startswith('capture_') for ticket in tickets):
        _fail('maintenance_recovery_native_containment_required')
    if gateway_path is not None:
        _check_native_rows(gateway_path, (
            ('voice_job_state', "SELECT 1 FROM voice_job_state WHERE state='processing' AND worker_id=? LIMIT 1",
                (participant_id + ':voice',)),))
    if secretary_path is not None:
        _check_native_rows(secretary_path, (
            ('jobs', "SELECT 1 FROM jobs WHERE status='running' LIMIT 1", ()),
            ('meetings', "SELECT 1 FROM meetings WHERE recording=1 OR media_path LIKE 'uploading:%' LIMIT 1", ()),
            ('enrollment_commands', "SELECT 1 FROM enrollment_commands WHERE status='running' LIMIT 1", ()),
            ('enrollment_materials', "SELECT 1 FROM enrollment_materials WHERE state='writing' LIMIT 1", ()),
            ('enrollment_recordings', "SELECT 1 FROM enrollment_recordings WHERE status IN ('starting','recording','stopping','processing') LIMIT 1", ()),
            ('voice_enrollments', "SELECT 1 FROM voice_enrollments WHERE status='pending' LIMIT 1", ())))


def _check_native_rows(path, queries):
    path = Path(path).resolve()
    if not path.exists():
        return
    try:
        with closing(sqlite3.connect('file:' + quote(path.as_posix(), safe='/:') + '?mode=ro', uri=True, timeout=.2)) as conn:
            conn.execute('BEGIN')
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if any(table in tables and conn.execute(query, parameters).fetchone()
                for table, query, parameters in queries):
                _fail('maintenance_recovery_native_containment_required')
    except sqlite3.Error:
        _fail('maintenance_recovery_sources_unavailable')


def _preflight_scope(maintenance, participant, tickets, proof, team_path, billing_path):
    if not isinstance(participant, dict) or not isinstance(tickets, tuple):
        _fail()
    old, run = participant.get('participant_id'), _uuid(participant.get('run_id'))
    if old != 'gateway:' + run:
        _fail('maintenance_recovery_identity_mismatch')
    status = maintenance.status()
    if status['mode'] != 'open':
        _fail('maintenance_blocked')
    if participant not in status['participants']:
        _fail('maintenance_recovery_identity_mismatch')
    ambient = [ticket for key, ticket in _AMBIENT.get()
        if key[:2] == (str(maintenance.path), maintenance.deployment_id)
        and ticket.kind == 'crash_recovery' and ticket.participant_id != old]
    if not ambient or ambient[-1] not in maintenance.active_operations(ambient[-1].participant_id):
        _fail('maintenance_recovery_admission_required')
    recovering = ambient[-1].participant_id
    sources = maintenance.bound_sources()
    if set(sources) != {'secretary', 'team', 'billing', 'vikunja'}:
        _fail('maintenance_sources_unverified')
    for role, path in (('team', team_path), ('billing', billing_path)):
        if (not isinstance(path, (str, Path)) or not Path(path).is_absolute()
            or os.path.normcase(os.path.abspath(path)) != sources[role]):
            _fail('maintenance_sources_mismatch')
    if len(tickets) > 10000 or any(not isinstance(t, MaintenanceTicket) for t in tickets):
        _fail()
    if len({t.id for t in tickets}) != len(tickets) or set(tickets) != set(maintenance.active_operations(old)):
        _fail('maintenance_recovery_ticket_mismatch')
    paid = set()
    for ticket in tickets:
        if (ticket.deployment_id != maintenance.deployment_id or ticket.participant_id != old
            or ticket.run_id != run or ticket.kind not in _KINDS):
            _fail('maintenance_recovery_ticket_unsupported')
        if ticket.kind == 'outbound_polza':
            paid.add(_uuid(ticket.operation_id))
        if ticket.kind == 'outbound_subscription':
            _uuid(ticket.operation_id)
    identity = participant.get('identity')
    if not isinstance(identity, dict) or not _proof_matches(proof, identity):
        _fail('maintenance_recovery_identity_mismatch')
    try:
        fresh = verify_dead_identity(identity)
    except ValueError:
        _fail('maintenance_recovery_process_unverified')
    if not _proof_matches(fresh, identity):
        _fail('maintenance_recovery_process_unverified')
    return old, run, tuple(sorted(paid)), recovering


def _gateway_source_preflight(maintenance, old, run, tickets, team_path, billing_path):
    _assert_not_restored((team_path, billing_path))
    _require_native_containment(maintenance, old, run, tickets, gateway_path=team_path)
    _validate_voice_dispositions(team_path, old)
    if not tickets:
        _reject_unrepresented_gateway_rows(team_path, old)


def _validate_voice_dispositions(path, old):
    """Read before constructors/migrations; old schemas have no abort evidence."""
    path=Path(path).resolve()
    if not path.exists():
        return
    try:
        with closing(sqlite3.connect('file:'+quote(path.as_posix(),safe='/:')+'?mode=ro',uri=True,timeout=.2)) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('BEGIN')
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='voice_job_state'").fetchone():
                return
            for row in conn.execute("SELECT id FROM voice_job_state WHERE worker_id=? AND state='processing'",(old+':voice',)):
                VoiceRepository.recovery_outcome(conn,row['id'])
    except (sqlite3.Error,VoiceError):
        _fail('maintenance_recovery_voice_invalid')


def _reject_unrepresented_gateway_rows(path, old):
    """Empty control inventory is never authority to clear durable worker state."""
    path = Path(path).resolve()
    if not path.exists():
        return
    try:
        with closing(sqlite3.connect('file:' + quote(path.as_posix(), safe='/:') + '?mode=ro', uri=True, timeout=.2)) as conn:
            conn.execute('BEGIN')
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, suffix in _WORKERS.items():
                states = _ACTIVE_STATES[table]
                placeholders = ','.join('?' for _ in states)
                if table in tables and conn.execute(f'SELECT 1 FROM {table} WHERE worker_id=? AND state IN ({placeholders}) LIMIT 1',
                    (old + ':' + suffix, *states)).fetchone():
                    _fail('maintenance_recovery_unrepresented_source_state')
            if 'team_sync_state' in tables and any(row[1] == old + ':sync:' + row[0] for row in
                conn.execute("SELECT project_id,worker_id FROM team_sync_state WHERE worker_id IS NOT NULL AND state='syncing'")):
                _fail('maintenance_recovery_unrepresented_source_state')
            if 'team_message_state' in tables and any(row[0].startswith(old + ':') for row in
                conn.execute("SELECT worker_id FROM team_message_state WHERE worker_id IS NOT NULL AND state='sending'")):
                _fail('maintenance_recovery_unrepresented_source_state')
    except sqlite3.Error:
        _fail('maintenance_recovery_sources_unavailable')


def preflight_dead_gateway(maintenance, participant, tickets, proof, *,
    team_database_path, billing_database_path, clock=None):
    """Read-only checks before opening/migrating either configured source store.

    Invoke for every dead gateway, including zero-ticket participants. This port
    does not create source files, migrate schemas, checkpoint rows or retire
    tickets. Full checkpointing repeats these checks after construction.
    """
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _fail()
    old, run, _, _ = _preflight_scope(maintenance, participant, tickets, proof,
        team_database_path, billing_database_path)
    _gateway_source_preflight(maintenance, old, run, tickets, team_database_path, billing_database_path)


def _preflight(maintenance, participant, tickets, proof, team, billing):
    if not isinstance(team, TeamDatabase) or not isinstance(billing, BudgetRepository) or not tickets:
        _fail('maintenance_recovery_sources_invalid')
    old, run, paid, recovering = _preflight_scope(maintenance, participant, tickets, proof, team.path, billing.path)
    if any(store.maintenance is not maintenance or store.participant_id != recovering for store in (team, billing)):
        _fail('maintenance_recovery_sources_invalid')
    _gateway_source_preflight(maintenance, old, run, tickets, team.path, billing.path)
    return old, run, paid


def _check_workers(conn, old, tickets):
    # Compare complete IDs; no prefix LIKE matching or shared legacy ownership.
    for table, suffix in _WORKERS.items():
        states = _ACTIVE_STATES[table]
        placeholders = ','.join('?' for _ in states)
        for row in conn.execute(f'SELECT worker_id FROM {table} WHERE worker_id IS NOT NULL AND state IN ({placeholders})', states):
            if row['worker_id'].startswith(old + ':') and row['worker_id'] != old + ':' + suffix:
                _fail('maintenance_recovery_worker_unsupported')
    for row in conn.execute("SELECT project_id,worker_id,state FROM team_sync_state WHERE worker_id IS NOT NULL AND state='syncing'"):
        worker = row['worker_id']
        if worker.startswith(old + ':') and worker != old + ':sync:' + row['project_id']:
            _fail('maintenance_recovery_worker_unsupported')
        if worker == 'team-sync' and row['state'] == 'syncing' and any(t.kind == 'sync' for t in tickets):
            _fail('maintenance_recovery_worker_unattributed')
    # No current runtime owns generic team_messages. Do not invent a layout.
    if any(row[0].startswith(old + ':') for row in
        conn.execute("SELECT worker_id FROM team_message_state WHERE worker_id IS NOT NULL AND state='sending'")):
        _fail('maintenance_recovery_worker_unsupported')


def _checkpoint_team(team, old, tickets, clock):
    repository = TeamRepository(team, clock=clock)
    sync = TeamSyncRepository(repository, clock=clock)
    with team.transaction() as conn:
        _check_workers(conn, old, tickets)  # Entire preflight precedes source writes.
        for row in conn.execute("SELECT * FROM team_execution WHERE worker_id=? AND state IN ('running','reconciling')", (old + ':task',)).fetchall():
            operation = row['operation_id']
            repository._transition(conn, operation, 'uncertain', error_code='dead_worker')
            conn.execute('UPDATE team_execution SET worker_id=NULL,lease_until=NULL,fence=fence+1 WHERE operation_id=?', (operation,))
            conn.execute('UPDATE team_resources SET fence=MAX(fence,?) WHERE operation_id=?', (row['fence'] + 1, operation))
        conn.execute("""UPDATE bot_event_state SET state='queued',
            worker_id=NULL,lease_until=NULL,fence=fence+1 WHERE worker_id=? AND state='processing'""", (old + ':inbox',))
        for table, suffix in (('bot_reply_state', 'bot'), ('voice_notice_state', 'notice')):
            conn.execute(f"""UPDATE {table} SET state='uncertain',
                worker_id=NULL,lease_until=NULL,fence=fence+1 WHERE worker_id=? AND state='sending'""", (old + ':' + suffix,))
        for row in conn.execute("SELECT * FROM voice_job_state WHERE worker_id=? AND state='processing'", (old + ':voice',)).fetchall():
            state,error=VoiceRepository.recovery_outcome(conn,row['id'])
            conn.execute('''UPDATE voice_job_state SET state=?,error_code=?,worker_id=NULL,
                lease_until=NULL,fence=fence+1 WHERE id=?''', (state, error, row['id']))
        conn.execute("""UPDATE notification_state SET state=CASE WHEN state='sending' THEN 'uncertain' ELSE state END,
            error_code=CASE WHEN state='sending' THEN 'dead_worker' ELSE error_code END,
            worker_id=NULL,lease_until=NULL,fence=fence+1 WHERE worker_id=? AND state IN ('pending','sending')""", (old + ':reminder',))
        for row in conn.execute("SELECT * FROM team_sync_state WHERE worker_id IS NOT NULL AND state='syncing'").fetchall():
            if row['worker_id'] != old + ':sync:' + row['project_id']:
                continue
            sync._finish(conn, row['run_id'], row['project_id'], 'sync_lease_expired', 1,
                hashlib.sha256(_canonical({'dead_gateway': old, 'run_id': row['run_id']}).encode()).hexdigest())
            conn.execute('UPDATE team_sync_state SET fence=fence+1 WHERE project_id=?', (row['project_id'],))


def _watermark(conn, tables):
    digest = hashlib.sha256()
    for table in tables:
        digest.update(table.encode('ascii') + b'\0')
        # All table names are fixed constants, including immutable evidence. The
        # streaming digest contains no raw data, paths, URLs, or provider keys.
        columns = len(conn.execute(f'PRAGMA table_info({table})').fetchall())
        if not columns and table=='voice_request_aborts' and conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0]<9:
            digest.update(b'absent-before-schema-9\0')
            continue
        order = ','.join(str(index) for index in range(1, columns + 1))
        for row in conn.execute(f'SELECT * FROM {table} ORDER BY {order}'):
            digest.update(_canonical(tuple(row)).encode('utf-8') + b'\n')
    return digest.hexdigest()


def checkpoint_dead_gateway(maintenance, participant, tickets, proof, *,
    team_database, billing_repository, clock=None):
    """Return evidence after source commits; caller separately retires tickets.

    Requires a different live participant's ambient crash_recovery admission and
    the deployment's immutable source-path binding. Never reads configuration or
    changes key-wide charges. Missing charge rows remain absent; a paid POST is
    not authorized before its durable reservation exists.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _fail()
    old, run, paid = _preflight(maintenance, participant, tickets, proof, team_database, billing_repository)
    # Validate billing before changing Team state, including unknown status.
    with billing_repository.transaction() as conn:
        for operation in paid:
            row = conn.execute('SELECT status FROM billing_charges WHERE operation_id=?', (operation,)).fetchone()
            if row and row['status'] not in ('reserved', 'submitted', 'uncertain', 'confirmed', 'released', 'rejected'):
                _fail('maintenance_recovery_billing_invalid')
    _checkpoint_team(team_database, old, tickets, clock)
    with billing_repository.transaction() as conn:
        for operation in paid:
            conn.execute("UPDATE billing_charges SET status='uncertain',updated_ms=? WHERE operation_id=? AND status='submitted'",
                (int(now.timestamp() * 1000), operation))
    # Hash only committed snapshots. A failure here still leaves every ticket
    # active and the durable uncertain/requeued source state safe to retry.
    with team_database.connection() as conn:
        conn.execute('BEGIN')
        team_hash = _watermark(conn, _TEAM_TABLES)
        conn.rollback()
    with billing_repository.transaction() as conn:
        billing_hash = _watermark(conn, ('billing_charges',))
    watermark = (('team', team_hash), ('billing', billing_hash))
    subscriptions = tuple(sorted({t.operation_id for t in tickets if t.kind == 'outbound_subscription'}))
    if subscriptions:
        from .team_runtime_health import RuntimeHealthRepository
        health = RuntimeHealthRepository(maintenance.path, maintenance.deployment_id, clock=clock)
        receipts = tuple(health.recover_subscription_attempt(operation) for operation in subscriptions)
        watermark += (('subscription', hashlib.sha256(_canonical(receipts).encode('utf-8')).hexdigest()),)
    ids = tuple(sorted(ticket.id for ticket in tickets))
    payload = {'participant_id': old, 'run_id': run, 'ticket_ids': ids,
        'billing_operation_ids': paid, 'source_watermarks': watermark}
    return RecoveryCheckpoint(old, run, ids, paid, watermark,
        hashlib.sha256(_canonical(payload).encode('utf-8')).hexdigest())


def _assert_not_restored(paths):
    """Read restore guards before constructors, WAL changes or source updates."""
    from .maintenance_binding import restore_guard_present
    try:
        if restore_guard_present(paths):
            _fail('maintenance_restore_blocked')
    except sqlite3.Error:
        _fail('maintenance_recovery_sources_unavailable')


_SECRETARY_KINDS = frozenset({'sql', 'intake', 'job', 'job_startup', 'background_job', 'polza_poll',
    'outbound_polza', 'outbound_publication', 'capture_meeting', 'capture_enrollment'})


def checkpoint_dead_secretary(config, maintenance, *, old_participant, proof):
    """Recover the fixed Secretary participant and retire under owned admission.

    Caller invokes this before replacing its registered run. Even an idle
    predecessor must be proven dead. Enrollment plaintext/files/devices are never
    touched; interrupted material remains cleanup_pending for normal recovery.
    """
    from .database import Database
    from .task_publication_repository import TaskPublicationRepository
    if not isinstance(old_participant, dict) or old_participant.get('participant_id') != 'secretary':
        _fail('maintenance_recovery_identity_mismatch')
    run = _uuid(old_participant.get('run_id'))
    status = maintenance.status()
    if status['mode'] != 'open':
        _fail('maintenance_blocked')
    if old_participant not in status['participants']:
        _fail('maintenance_recovery_identity_mismatch')
    identity = old_participant.get('identity')
    if not isinstance(identity, dict) or not _proof_matches(proof, identity):
        _fail('maintenance_recovery_identity_mismatch')
    try:
        fresh = verify_dead_identity(identity)
    except ValueError:
        _fail('maintenance_recovery_process_unverified')
    if not _proof_matches(fresh, identity):
        _fail('maintenance_recovery_process_unverified')
    sources = {role: getattr(config, role + '_database_path')
        for role in ('secretary', 'team', 'billing', 'vikunja')}
    maintenance.bind_sources(sources)
    _assert_not_restored(sources.values())
    tickets = maintenance.active_operations('secretary')
    paid = set()
    for ticket in tickets:
        if ticket.run_id != run or ticket.kind not in _SECRETARY_KINDS:
            _fail('maintenance_recovery_ticket_unsupported')
        if ticket.kind == 'outbound_polza':
            paid.add(_uuid(ticket.operation_id))
    _require_native_containment(maintenance, 'secretary', run, tickets, secretary_path=sources['secretary'])
    predecessor_recoveries = []
    for participant in status['participants']:
        identifier = participant['participant_id']
        if not identifier.startswith('secretary-recovery:'):
            continue
        active = maintenance.active_operations(identifier)
        if not active:
            continue
        if (identifier != 'secretary-recovery:' + _uuid(participant['run_id'])
            or any(t.run_id != participant['run_id'] or t.kind not in ('crash_recovery', 'sql') for t in active)):
            _fail('maintenance_recovery_ticket_unsupported')
        try:
            recovered_proof = verify_dead_identity(participant['identity'])
        except ValueError:
            _fail('maintenance_recovery_process_unverified')
        if not _proof_matches(recovered_proof, participant['identity']):
            _fail('maintenance_recovery_process_unverified')
        predecessor_recoveries.append((participant, active, recovered_proof))
    recovery_run = str(uuid4())
    actor = 'secretary-recovery:' + recovery_run
    maintenance.register_participant(actor, run_id=recovery_run, identity=process_identity())
    with maintenance.admission(actor, 'crash_recovery'):
        # The fixed Secretary is the sole owner of its job/capture/enrollment
        # tables. Publications have explicit worker ownership shared by runs.
        db = Database(sources['secretary'], maintenance=maintenance, participant_id=actor)
        billing = BudgetRepository(sources['billing'], maintenance=maintenance, participant_id=actor)
        with billing.transaction() as conn:
            for operation in paid:
                row = conn.execute('SELECT status FROM billing_charges WHERE operation_id=?', (operation,)).fetchone()
                if row and row['status'] not in ('reserved', 'submitted', 'uncertain', 'confirmed', 'released', 'rejected'):
                    _fail('maintenance_recovery_billing_invalid')
        db.recover()
        publications = TaskPublicationRepository(db)
        with db.transaction() as conn:
            conn.execute("UPDATE enrollment_commands SET status='done',error='operation_interrupted' WHERE status='running'")
            conn.execute("UPDATE enrollment_materials SET state='cleanup_pending' WHERE state='writing'")
            conn.execute("UPDATE enrollment_recordings SET status='interrupted',reason='recording_interrupted' WHERE status IN ('starting','recording','stopping','processing')")
            conn.execute("UPDATE voice_enrollments SET status='interrupted',reason_codes='[\"operation_interrupted\"]' WHERE status='pending'")
            rows = conn.execute('SELECT * FROM task_publication_outbox WHERE worker_id=?', ('secretary-publications-' + run,)).fetchall()
            for row in rows:
                if row['state'] in ('running', 'reconciling'):
                    record = publications._stored(conn, row['delivery_operation_id'])
                    publications._execution(conn, record, 'uncertain' if row['started_at'] is not None else 'queued',
                        error_code='publication_dispatch_lease_expired' if row['started_at'] is not None else None)
                conn.execute('UPDATE task_publication_outbox SET worker_id=NULL,lease_until=NULL,fence=fence+1 WHERE delivery_operation_id=?', (row['delivery_operation_id'],))
        with billing.transaction() as conn:
            for operation in paid:
                conn.execute("UPDATE billing_charges SET status='uncertain',updated_ms=? WHERE operation_id=? AND status='submitted'",
                    (int(datetime.now(timezone.utc).timestamp() * 1000), operation))
        if not tickets and not predecessor_recoveries:
            return None
        with db.connection() as conn:
            conn.execute('BEGIN')
            source_hash = _watermark(conn, ('jobs', 'meetings', 'usage', 'enrollment_commands',
                'enrollment_materials', 'enrollment_recordings', 'voice_enrollments', 'task_publication_outbox'))
            conn.rollback()
        with billing.transaction() as conn:
            billing_hash = _watermark(conn, ('billing_charges',))
        watermark = (('secretary', source_hash), ('billing', billing_hash))
        def evidence(identifier, source_run, source_tickets, billing_ids):
            ids = tuple(sorted(t.id for t in source_tickets))
            payload = {'participant_id': identifier, 'run_id': source_run, 'ticket_ids': ids,
                'billing_operation_ids': billing_ids, 'source_watermarks': watermark}
            return RecoveryCheckpoint(identifier, source_run, ids, billing_ids, watermark,
                hashlib.sha256(_canonical(payload).encode('utf-8')).hexdigest())
        checkpoint = evidence('secretary', run, tickets, tuple(sorted(paid))) if tickets else None
        if checkpoint is not None:
            maintenance.retire_dead_participant(checkpoint, proof=proof)
        # An interrupted earlier recovery had only admitted local SQL. After
        # repeating the source checkpoint, its exact inventory can be retired;
        # no outbound/billing operation is inferred from its process identity.
        for participant, active, recovered_proof in predecessor_recoveries:
            recovered = evidence(participant['participant_id'], participant['run_id'], active, ())
            maintenance.retire_dead_participant(recovered, proof=recovered_proof)
        return checkpoint
