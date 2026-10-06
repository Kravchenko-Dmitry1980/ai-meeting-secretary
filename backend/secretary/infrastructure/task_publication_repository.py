"""Source-side publication intent, immutable evidence, and fenced dispatch.

No source eligibility rules or gateway transport live here. The source callback
runs in the same BEGIN IMMEDIATE transaction as acceptance/start authorization.
After a durable start there is no path back to a POST-capable queue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3

from pydantic import ValidationError

from secretary.domain.team import (
    AcceptanceReceipt, CommandReceipt, ExecutionState, TeamConflict, TeamForbidden,
    canonical, content_hash, uuid_string,
)
from secretary.domain.task_delivery import (
    DeliveryReceipt, PublicationItemReceipt, PublicationScope, PublicationTask,
    PublicationWatermarks, PublishCommand, PublishPreview, delivery_uuid, publication_uuid,
)


TERMINAL = frozenset({'applied', 'conflict', 'rejected'})
ONGOING = frozenset({'queued', 'running', 'reconciling', 'uncertain'})
MAX_JSON_BYTES = 2 * 1024 * 1024

PUBLICATION_MIGRATION = (
    '''CREATE TABLE task_publication_previews(
        preview_id TEXT PRIMARY KEY NOT NULL,actor_id TEXT NOT NULL,meeting_id TEXT NOT NULL,
        summary_version INTEGER NOT NULL CHECK(typeof(summary_version)='integer' AND summary_version>=0),
        payload TEXT NOT NULL CHECK(json_valid(payload)),preview_hash TEXT NOT NULL CHECK(length(preview_hash)=64),
        expires_at REAL NOT NULL,
        CHECK(json_extract(payload,'$.preview_id') IS preview_id),
        CHECK(json_extract(payload,'$.actor_id') IS actor_id),
        CHECK(json_extract(payload,'$.scope.meeting_id') IS meeting_id),
        CHECK(json_extract(payload,'$.scope.summary_version') IS summary_version),
        FOREIGN KEY(meeting_id,summary_version) REFERENCES summaries(meeting_id,summary_version))''',
    '''CREATE TABLE task_publication_commands(
        operation_id TEXT PRIMARY KEY NOT NULL,actor_id TEXT NOT NULL,
        preview_id TEXT NOT NULL REFERENCES task_publication_previews(preview_id),
        meeting_id TEXT NOT NULL REFERENCES meetings(id),payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
        payload TEXT NOT NULL CHECK(json_valid(payload)),acceptance TEXT NOT NULL CHECK(json_valid(acceptance)),
        created_at REAL NOT NULL,
        CHECK(json_extract(payload,'$.operation_id') IS operation_id),
        CHECK(json_extract(payload,'$.actor_id') IS actor_id),
        CHECK(json_extract(payload,'$.preview_id') IS preview_id),
        CHECK(json_extract(payload,'$.scope.meeting_id') IS meeting_id),
        CHECK(json_extract(acceptance,'$.operation_id') IS operation_id),
        CHECK(json_extract(acceptance,'$.payload_hash') IS payload_hash))''',
    '''CREATE TABLE task_publication_items(
        delivery_operation_id TEXT PRIMARY KEY NOT NULL,publication_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,meeting_id TEXT NOT NULL,summary_version INTEGER NOT NULL,
        project_id TEXT NOT NULL,scope TEXT NOT NULL CHECK(json_valid(scope)),
        watermarks TEXT NOT NULL CHECK(json_valid(watermarks)),item TEXT NOT NULL CHECK(json_valid(item)),
        item_hash TEXT NOT NULL CHECK(length(item_hash)=64),created_at REAL NOT NULL,
        CHECK(json_extract(scope,'$.meeting_id') IS meeting_id),
        CHECK(json_extract(scope,'$.summary_version') IS summary_version),
        CHECK(json_extract(scope,'$.destination_project_id') IS project_id),
        FOREIGN KEY(meeting_id,summary_version) REFERENCES summaries(meeting_id,summary_version))''',
    '''CREATE TABLE task_publication_batch_items(
        operation_id TEXT NOT NULL REFERENCES task_publication_commands(operation_id),
        ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=0 AND ordinal<100),
        delivery_operation_id TEXT NOT NULL REFERENCES task_publication_items(delivery_operation_id),
        PRIMARY KEY(operation_id,ordinal),UNIQUE(operation_id,delivery_operation_id))''',
    '''CREATE TABLE task_publication_outbox(
        delivery_operation_id TEXT PRIMARY KEY NOT NULL REFERENCES task_publication_items(delivery_operation_id),
        state TEXT NOT NULL CHECK(state IN ('queued','running','reconciling','uncertain','applied','conflict','rejected')),
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),
        execution TEXT NOT NULL CHECK(json_valid(execution)),
        gateway_receipt TEXT CHECK(gateway_receipt IS NULL OR json_valid(gateway_receipt)),
        gateway_revision INTEGER NOT NULL DEFAULT -1 CHECK(typeof(gateway_revision)='integer' AND gateway_revision>=-1),
        gateway_payload_hash TEXT CHECK(gateway_payload_hash IS NULL OR length(gateway_payload_hash)=64),
        worker_id TEXT,fence INTEGER NOT NULL DEFAULT 0 CHECK(typeof(fence)='integer' AND fence>=0),
        lease_until REAL,started_at REAL,
        CHECK(json_extract(execution,'$.state') IS state),
        CHECK(json_extract(execution,'$.revision') IS revision),
        CHECK((worker_id IS NULL)=(lease_until IS NULL)),
        CHECK(started_at IS NULL OR gateway_payload_hash IS NOT NULL),
        CHECK(gateway_receipt IS NULL OR (started_at IS NOT NULL AND
            json_extract(gateway_receipt,'$.acceptance_receipt.operation_id') IS delivery_operation_id AND
            json_extract(gateway_receipt,'$.acceptance_receipt.payload_hash') IS gateway_payload_hash AND
            json_extract(gateway_receipt,'$.execution_state.revision') IS gateway_revision)),
        CHECK(state!='applied' OR (gateway_receipt IS NOT NULL AND
            json_extract(gateway_receipt,'$.acceptance_receipt.decision') IS 'accepted' AND
            json_extract(gateway_receipt,'$.execution_state.state') IS 'applied' AND
            json_extract(gateway_receipt,'$.execution_state.verified_at') IS NOT NULL AND
            json_extract(execution,'$.task_id') IS NOT NULL AND
            json_extract(gateway_receipt,'$.execution_state.task_id') IS json_extract(execution,'$.task_id') AND
            json_extract(gateway_receipt,'$.current.task_id') IS json_extract(execution,'$.task_id'))))''',
    'CREATE INDEX task_publication_meeting ON task_publication_commands(meeting_id,actor_id,created_at)',
    'CREATE INDEX task_publication_project ON task_publication_items(meeting_id,project_id)',
    'CREATE INDEX task_publication_queue ON task_publication_outbox(state,started_at,lease_until)',
    *tuple(f'''CREATE TRIGGER {table}_immutable_{verb.lower()} BEFORE {verb} ON {table}
        BEGIN SELECT RAISE(ABORT,'Immutable publication evidence'); END'''
        for table in ('task_publication_previews', 'task_publication_commands',
                      'task_publication_items', 'task_publication_batch_items') for verb in ('UPDATE', 'DELETE')),
    *tuple(f'''CREATE TRIGGER {table}_replace_guard BEFORE INSERT ON {table}
        WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition})
        BEGIN SELECT RAISE(ABORT,'Immutable publication evidence'); END'''
        for table, condition in (
            ('task_publication_previews', 'preview_id=NEW.preview_id'),
            ('task_publication_commands', 'operation_id=NEW.operation_id'),
            ('task_publication_items', 'delivery_operation_id=NEW.delivery_operation_id'),
            ('task_publication_batch_items', 'operation_id=NEW.operation_id AND (ordinal=NEW.ordinal OR delivery_operation_id=NEW.delivery_operation_id)'),
            ('task_publication_outbox', 'delivery_operation_id=NEW.delivery_operation_id'))),
    '''CREATE TRIGGER task_publication_outbox_no_delete BEFORE DELETE ON task_publication_outbox
        BEGIN SELECT RAISE(ABORT,'Publication dispatch evidence cannot be deleted'); END''',
    '''CREATE TRIGGER task_publication_outbox_monotonic BEFORE UPDATE ON task_publication_outbox
        WHEN NEW.delivery_operation_id IS NOT OLD.delivery_operation_id OR NEW.rowid!=OLD.rowid OR NEW.fence<OLD.fence
          OR NEW.revision<OLD.revision OR NEW.gateway_revision<OLD.gateway_revision
          OR (OLD.gateway_payload_hash IS NOT NULL AND NEW.gateway_payload_hash IS NOT OLD.gateway_payload_hash)
          OR (OLD.started_at IS NOT NULL AND NEW.started_at IS NOT OLD.started_at)
          OR (OLD.state IN ('applied','conflict','rejected') AND NEW.state IS NOT OLD.state)
          OR (NEW.revision=OLD.revision AND (NEW.execution IS NOT OLD.execution OR NEW.gateway_receipt IS NOT OLD.gateway_receipt))
          OR (OLD.state IN ('applied','conflict','rejected') AND
              (NEW.execution IS NOT OLD.execution OR NEW.gateway_receipt IS NOT OLD.gateway_receipt OR NEW.gateway_revision!=OLD.gateway_revision))
        BEGIN SELECT RAISE(ABORT,'Publication dispatch cannot regress'); END''',
)


def migrate_task_publications(db):
    """Atomic source migration 9; safe to repeat, including after rollback."""
    with db.transaction() as conn:
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=9').fetchone():
            return
        for statement in PUBLICATION_MIGRATION:
            conn.execute(statement)
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise sqlite3.IntegrityError('Publication migration foreign key failure')
        conn.execute('INSERT INTO schema_migrations VALUES(9,?)', (datetime.now(timezone.utc).isoformat(),))


@dataclass(frozen=True)
class StoredPublication:
    scope: PublicationScope
    watermarks: PublicationWatermarks
    item: PublicationTask
    actor_id: str
    publication_id: str
    delivery_operation_id: str
    execution_state: ExecutionState
    gateway_receipt: CommandReceipt | None
    dispatch_started: bool
    gateway_payload_hash: str | None


@dataclass(frozen=True)
class PublicationClaim(StoredPublication):
    worker_id: str
    fence: int
    lease_until: datetime


def _data(value):
    return value.model_dump(mode='json', exclude={'preview_hash'} if isinstance(value, PublishPreview) else set())


def _json(value):
    text = canonical(_data(value) if hasattr(value, 'model_dump') else value)
    if len(text.encode('utf-8')) > MAX_JSON_BYTES:
        raise TeamConflict('publication_payload_too_large')
    return text


def _model(kind, raw):
    try:
        return kind.model_validate_json(raw) if isinstance(raw, str) else kind.model_validate(raw)
    except (ValueError, TypeError, ValidationError):
        raise TeamConflict('publication_data_invalid') from None


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise TeamConflict('invalid_payload_hash')
    return value


def _semantic(actor, scope, item):
    value = _data(item)
    if item.intent == 'propose_update':
        for key in ('expected_task_revision', 'expected_task_fingerprint', 'target_snapshot'):
            value.pop(key, None)
    return content_hash({'actor_id': actor, 'scope': _data(scope), 'item': value})


def _identity(scope, item):
    publication = publication_uuid(scope, item.action_id, item.separate_id)
    if item.publication_id is not None and item.publication_id != publication:
        raise TeamConflict('publication_identity_mismatch')
    return publication, delivery_uuid(scope, item)


class TaskPublicationRepository:
    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('clock_requires_timezone')
        return value.astimezone(timezone.utc)

    @staticmethod
    def _preview(row):
        if row is None:
            raise TeamForbidden('publication_preview_unavailable')
        value = _model(PublishPreview, row['payload'])
        if (value.preview_id != row['preview_id'] or value.actor_id != row['actor_id']
                or value.scope.meeting_id != row['meeting_id'] or value.scope.summary_version != row['summary_version']
                or value.preview_hash != row['preview_hash'] or value.expires_at is None
                or value.expires_at.timestamp() != row['expires_at']):
            raise TeamConflict('publication_preview_corrupt')
        return value

    def save_preview(self, preview: PublishPreview):
        preview = _model(PublishPreview, _data(preview))
        if preview.actor_id is None or preview.expires_at is None:
            raise TeamConflict('preview_requires_actor_and_expiry')
        for item in preview.items:
            _identity(preview.scope, item)
        payload = _json(preview)
        with self.db.transaction() as conn:
            old = conn.execute('SELECT * FROM task_publication_previews WHERE preview_id=?', (preview.preview_id,)).fetchone()
            if old:
                if self._preview(old) != preview:
                    raise TeamConflict('preview_id_conflict')
                return preview
            if preview.expires_at <= self._now():
                raise TeamConflict('preview_expired')
            conn.execute('INSERT INTO task_publication_previews VALUES(?,?,?,?,?,?,?)',
                (preview.preview_id, preview.actor_id, preview.scope.meeting_id, preview.scope.summary_version,
                 payload, preview.preview_hash, preview.expires_at.timestamp()))
            return preview

    def get_preview(self, preview_id, actor_id):
        preview_id, actor_id = uuid_string(preview_id), uuid_string(actor_id)
        with self.db.connection() as conn:
            row = conn.execute('SELECT * FROM task_publication_previews WHERE preview_id=? AND actor_id=?',
                               (preview_id, actor_id)).fetchone()
            return self._preview(row)

    @staticmethod
    def _gateway_proof(record, receipt, expected_hash):
        acceptance, execution = receipt.acceptance_receipt, receipt.execution_state
        if (acceptance.operation_id != record.delivery_operation_id or acceptance.payload_hash != expected_hash
                or record.gateway_payload_hash != expected_hash or not record.dispatch_started):
            raise TeamConflict('gateway_receipt_identity_mismatch')
        if acceptance.decision == 'rejected' and execution.state not in ('rejected', 'conflict'):
            raise TeamConflict('gateway_receipt_decision_mismatch')
        current = receipt.current
        if current and current.project_id != record.scope.destination_project_id:
            raise TeamConflict('gateway_receipt_scope_mismatch')
        task_ids = [value for value in (execution.task_id, current.task_id if current else None,
                                        record.item.target_task_id) if value is not None]
        if len(set(task_ids)) > 1:
            raise TeamConflict('gateway_receipt_task_mismatch')
        if execution.state == 'applied' and (acceptance.decision != 'accepted' or execution.task_id is None
                or execution.verified_at is None or current is None or current.task_id != execution.task_id):
            raise TeamConflict('gateway_applied_proof_required')

    def _stored(self, conn, delivery_id):
        row = conn.execute('SELECT * FROM task_publication_items WHERE delivery_operation_id=?', (delivery_id,)).fetchone()
        state = conn.execute('SELECT * FROM task_publication_outbox WHERE delivery_operation_id=?', (delivery_id,)).fetchone()
        if row is None or state is None:
            raise TeamConflict('publication_delivery_unavailable')
        scope, watermarks, item = (_model(PublicationScope, row['scope']),
            _model(PublicationWatermarks, row['watermarks']), _model(PublicationTask, row['item']))
        pub_id, expected_id = _identity(scope, item)
        if (row['delivery_operation_id'] != expected_id or row['publication_id'] != pub_id
                or row['meeting_id'] != scope.meeting_id or row['summary_version'] != scope.summary_version
                or row['project_id'] != scope.destination_project_id
                or row['item_hash'] != _semantic(uuid_string(row['actor_id']), scope, item)):
            raise TeamConflict('publication_delivery_corrupt')
        execution = _model(ExecutionState, state['execution'])
        receipt = _model(CommandReceipt, state['gateway_receipt']) if state['gateway_receipt'] else None
        if execution.state != state['state'] or execution.revision != state['revision']:
            raise TeamConflict('publication_execution_corrupt')
        result = StoredPublication(scope, watermarks, item, row['actor_id'], pub_id, expected_id,
            execution, receipt, state['started_at'] is not None, state['gateway_payload_hash'])
        if result.dispatch_started and result.gateway_payload_hash is None:
            raise TeamConflict('publication_dispatch_hash_missing')
        if result.gateway_payload_hash is not None:
            _digest(result.gateway_payload_hash)
        if receipt:
            self._gateway_proof(result, receipt, result.gateway_payload_hash)
            if receipt.execution_state.revision != state['gateway_revision']:
                raise TeamConflict('publication_gateway_revision_corrupt')
        elif state['gateway_revision'] != -1:
            raise TeamConflict('publication_gateway_revision_corrupt')
        if execution.state == 'applied':
            if (receipt is None or receipt.execution_state.state != 'applied'
                    or execution.task_id != receipt.execution_state.task_id
                    or execution.verified_at != receipt.execution_state.verified_at):
                raise TeamConflict('publication_applied_proof_missing')
        return result

    @staticmethod
    def _item_receipt(record):
        return PublicationItemReceipt(publication_id=record.publication_id,
            delivery_operation_id=record.delivery_operation_id, action_id=record.item.action_id,
            intent=record.item.intent, execution_state=record.execution_state, gateway_receipt=record.gateway_receipt)

    def _receipt(self, conn, operation_id, actor_id):
        row = conn.execute('SELECT * FROM task_publication_commands WHERE operation_id=? AND actor_id=?',
                           (operation_id, actor_id)).fetchone()
        if row is None:
            raise TeamForbidden('publication_command_unavailable')
        command = _model(PublishCommand, row['payload'])
        acceptance = _model(AcceptanceReceipt, row['acceptance'])
        if (command.operation_id != operation_id or command.actor_id != actor_id
                or command.preview_id != row['preview_id'] or command.scope.meeting_id != row['meeting_id']
                or content_hash(_data(command)) != row['payload_hash'] or acceptance.operation_id != operation_id
                or acceptance.payload_hash != row['payload_hash'] or acceptance.decision != 'accepted'):
            raise TeamConflict('publication_command_corrupt')
        links = conn.execute('SELECT ordinal,delivery_operation_id FROM task_publication_batch_items WHERE operation_id=? ORDER BY ordinal',
                             (operation_id,)).fetchall()
        if len(links) != len(command.items):
            raise TeamConflict('publication_batch_corrupt')
        records = []
        for ordinal, (link, item) in enumerate(zip(links, command.items)):
            record = self._stored(conn, link['delivery_operation_id'])
            if (link['ordinal'] != ordinal or record.delivery_operation_id != delivery_uuid(command.scope, item)
                    or _semantic(actor_id, command.scope, item) != _semantic(record.actor_id, record.scope, record.item)):
                raise TeamConflict('publication_batch_corrupt')
            records.append(self._item_receipt(record))
        return DeliveryReceipt(operation_id=operation_id, scope=command.scope, acceptance_receipt=acceptance, items=tuple(records))

    def accept(self, actor_id, command: PublishCommand, validate):
        actor_id = uuid_string(actor_id)
        command = _model(PublishCommand, _data(command))
        payload, digest = _json(command), content_hash(_data(command))
        with self.db.transaction() as conn:
            old = conn.execute('SELECT actor_id,payload_hash FROM task_publication_commands WHERE operation_id=?',
                               (command.operation_id,)).fetchone()
            if old:
                if old['actor_id'] != actor_id or old['payload_hash'] != digest:
                    raise TeamConflict('operation_id_conflict')
                return self._receipt(conn, command.operation_id, actor_id)
            preview = self._preview(conn.execute('SELECT * FROM task_publication_previews WHERE preview_id=?',
                                                 (command.preview_id,)).fetchone())
            if preview.actor_id != actor_id or command.actor_id != actor_id:
                raise TeamForbidden('publication_preview_unavailable')
            expected = _data(command)
            expected.pop('operation_id')
            expected.pop('preview_hash')
            if command.preview_hash != preview.preview_hash or expected != _data(preview):
                raise TeamConflict('preview_payload_mismatch')
            if preview.expires_at is None or preview.expires_at <= self._now():
                raise TeamConflict('preview_expired')
            validate(conn, preview)
            stamp = self._now()
            if preview.expires_at <= stamp:
                raise TeamConflict('preview_expired')
            acceptance = AcceptanceReceipt(operation_id=command.operation_id, payload_hash=digest,
                decision='accepted', decided_at=stamp)
            conn.execute('INSERT INTO task_publication_commands VALUES(?,?,?,?,?,?,?,?)',
                (command.operation_id, actor_id, command.preview_id, command.scope.meeting_id,
                 digest, payload, _json(acceptance), stamp.timestamp()))
            for ordinal, item in enumerate(command.items):
                pub_id, delivery_id = _identity(command.scope, item)
                item_hash = _semantic(actor_id, command.scope, item)
                prior = conn.execute('SELECT item_hash FROM task_publication_items WHERE delivery_operation_id=?', (delivery_id,)).fetchone()
                if prior:
                    old_record = self._stored(conn, delivery_id)
                    if prior['item_hash'] != item_hash or old_record.actor_id != actor_id:
                        raise TeamConflict('publication_delivery_conflict')
                else:
                    conn.execute('INSERT INTO task_publication_items VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (delivery_id, pub_id, actor_id, command.scope.meeting_id, command.scope.summary_version,
                         command.scope.destination_project_id, _json(command.scope), _json(command.watermarks),
                         _json(item), item_hash, stamp.timestamp()))
                    execution = ExecutionState(state='queued')
                    conn.execute('''INSERT INTO task_publication_outbox(delivery_operation_id,state,revision,execution)
                        VALUES(?,'queued',0,?)''', (delivery_id, _json(execution)))
                conn.execute('INSERT INTO task_publication_batch_items VALUES(?,?,?)', (command.operation_id, ordinal, delivery_id))
            return self._receipt(conn, command.operation_id, actor_id)

    def read(self, operation_id, actor_id):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            return self._receipt(conn, uuid_string(operation_id), uuid_string(actor_id))

    def list(self, meeting_id, actor_id, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_publication_limit')
        actor_id = uuid_string(actor_id)
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            rows = conn.execute('''SELECT operation_id FROM task_publication_commands WHERE meeting_id=? AND actor_id=?
                ORDER BY created_at DESC,operation_id LIMIT ?''', (meeting_id, actor_id, limit)).fetchall()
            return tuple(self._receipt(conn, row['operation_id'], actor_id) for row in rows)

    def publications(self, meeting_id, project_id):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            rows = conn.execute('''SELECT delivery_operation_id FROM task_publication_items WHERE meeting_id=? AND project_id=?
                ORDER BY created_at,delivery_operation_id LIMIT 1001''', (meeting_id, project_id)).fetchall()
            if len(rows) > 1000:
                raise TeamConflict('publication_scan_limit')
            return tuple(self._stored(conn, row['delivery_operation_id']) for row in rows)

    @staticmethod
    def _execution(conn, record, state, **fields):
        value = ExecutionState(state=state, revision=record.execution_state.revision + 1,
            task_id=fields.get('task_id', record.execution_state.task_id),
            verified_at=fields.get('verified_at'), error_code=fields.get('error_code'), retry_after=fields.get('retry_after'))
        conn.execute('UPDATE task_publication_outbox SET state=?,revision=?,execution=? WHERE delivery_operation_id=?',
                     (value.state, value.revision, _json(value), record.delivery_operation_id))

    def _expire(self, conn, stamp):
        rows = conn.execute('''SELECT delivery_operation_id,started_at FROM task_publication_outbox
            WHERE worker_id IS NOT NULL AND lease_until<=? AND state='running' ''', (stamp.timestamp(),)).fetchall()
        for row in rows:
            record = self._stored(conn, row['delivery_operation_id'])
            self._execution(conn, record, 'uncertain' if row['started_at'] is not None else 'queued',
                            error_code='publication_dispatch_lease_expired' if row['started_at'] is not None else None)
            conn.execute('UPDATE task_publication_outbox SET worker_id=NULL,lease_until=NULL,fence=fence+1 WHERE delivery_operation_id=?',
                         (record.delivery_operation_id,))

    @staticmethod
    def _claim(record, row):
        return PublicationClaim(**record.__dict__, worker_id=row['worker_id'], fence=row['fence'],
            lease_until=datetime.fromtimestamp(row['lease_until'], timezone.utc))

    def claim_next(self, worker_id, lease_seconds=60):
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 200:
            raise ValueError('invalid_publication_worker')
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('invalid_publication_lease')
        with self.db.transaction() as conn:
            stamp = self._now()
            self._expire(conn, stamp)
            row = conn.execute('''SELECT delivery_operation_id FROM task_publication_outbox
                WHERE state='queued' AND started_at IS NULL AND worker_id IS NULL ORDER BY rowid LIMIT 1''').fetchone()
            if row is None:
                return None
            record = self._stored(conn, row['delivery_operation_id'])
            self._execution(conn, record, 'running')
            conn.execute('''UPDATE task_publication_outbox SET worker_id=?,fence=fence+1,lease_until=?
                WHERE delivery_operation_id=?''', (worker_id, (stamp + timedelta(seconds=lease_seconds)).timestamp(), record.delivery_operation_id))
            row = conn.execute('SELECT * FROM task_publication_outbox WHERE delivery_operation_id=?', (record.delivery_operation_id,)).fetchone()
            return self._claim(self._stored(conn, record.delivery_operation_id), row)

    def _require_claim(self, conn, claim):
        if not isinstance(claim, PublicationClaim):
            raise TeamConflict('invalid_publication_claim')
        record = self._stored(conn, claim.delivery_operation_id)
        row = conn.execute('SELECT * FROM task_publication_outbox WHERE delivery_operation_id=?', (claim.delivery_operation_id,)).fetchone()
        if (row['worker_id'] != claim.worker_id or row['fence'] != claim.fence or row['lease_until'] is None
                or row['lease_until'] <= self._now().timestamp() or row['lease_until'] != claim.lease_until.timestamp()
                or record.execution_state.state != 'running'
                or any(getattr(record, key) != getattr(claim, key) for key in
                       ('scope', 'watermarks', 'item', 'actor_id', 'publication_id'))):
            raise TeamConflict('stale_publication_claim')
        return record, row

    def mark_started(self, claim, validate, *, expected_payload_hash):
        expected_payload_hash = _digest(expected_payload_hash)
        with self.db.transaction() as conn:
            record, row = self._require_claim(conn, claim)
            if record.dispatch_started:
                raise TeamConflict('publication_dispatch_already_started')
            validate(conn, record.scope, record.watermarks, record.item, record.actor_id)
            self._require_claim(conn, claim)  # callback cannot consume the remaining lease
            conn.execute('UPDATE task_publication_outbox SET started_at=?,gateway_payload_hash=? WHERE delivery_operation_id=?',
                         (self._now().timestamp(), expected_payload_hash, record.delivery_operation_id))
            self._execution(conn, record, 'running')
            return self._claim(self._stored(conn, record.delivery_operation_id), row)

    def polling(self, limit=100, *, after_delivery_operation_id=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid_publication_limit')
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            after_rowid = 0
            if after_delivery_operation_id is not None:
                cursor = conn.execute('SELECT rowid FROM task_publication_outbox WHERE delivery_operation_id=?',
                    (uuid_string(after_delivery_operation_id),)).fetchone()
                if cursor is None:
                    raise TeamConflict('publication_poll_cursor_unavailable')
                after_rowid = cursor['rowid']
            rows = conn.execute('''SELECT delivery_operation_id FROM task_publication_outbox WHERE started_at IS NOT NULL
                AND rowid>? AND state IN ('queued','running','reconciling','uncertain') ORDER BY rowid LIMIT ?''',
                (after_rowid, limit)).fetchall()
            return tuple(self._stored(conn, row['delivery_operation_id']) for row in rows)

    def update_gateway(self, delivery_operation_id, receipt: CommandReceipt, expected_payload_hash):
        delivery_operation_id, expected_payload_hash = uuid_string(delivery_operation_id), _digest(expected_payload_hash)
        receipt = _model(CommandReceipt, _data(receipt))
        _json(receipt)
        with self.db.transaction() as conn:
            record = self._stored(conn, delivery_operation_id)
            self._gateway_proof(record, receipt, expected_payload_hash)
            old = record.gateway_receipt
            if old:
                if receipt.acceptance_receipt != old.acceptance_receipt:
                    raise TeamConflict('gateway_acceptance_changed')
                if receipt.execution_state.revision < old.execution_state.revision:
                    raise TeamConflict('gateway_revision_regression')
                if receipt.execution_state.revision == old.execution_state.revision:
                    if receipt.execution_state != old.execution_state:
                        raise TeamConflict('gateway_revision_conflict')
                    if (record.execution_state.state == receipt.execution_state.state
                            and record.execution_state.error_code == receipt.execution_state.error_code
                            and record.execution_state.verified_at == receipt.execution_state.verified_at):
                        return record
            if record.execution_state.state in TERMINAL:
                if receipt.execution_state.state != record.execution_state.state:
                    raise TeamConflict('gateway_terminal_regression')
                return record
            remote = receipt.execution_state
            # Store remote proof before projecting applied; the SQL CHECK also
            # refuses applied without the linked receipt, task ID and timestamp.
            value = ExecutionState(state=remote.state, revision=record.execution_state.revision + 1,
                task_id=remote.task_id, verified_at=remote.verified_at, error_code=remote.error_code, retry_after=remote.retry_after)
            conn.execute('''UPDATE task_publication_outbox SET state=?,revision=?,execution=?,gateway_receipt=?,
                gateway_revision=?,worker_id=NULL,lease_until=NULL WHERE delivery_operation_id=?''',
                (value.state, value.revision, _json(value), _json(receipt), remote.revision, delivery_operation_id))
            return self._stored(conn, delivery_operation_id)

    def note_poll_error(self, delivery_operation_id, error_code):
        delivery_operation_id = uuid_string(delivery_operation_id)
        if not isinstance(error_code, str) or not re.fullmatch('[a-z0-9_]{1,160}', error_code):
            raise TeamConflict('invalid_publication_error_code')
        with self.db.transaction() as conn:
            record = self._stored(conn, delivery_operation_id)
            if not record.dispatch_started or record.execution_state.state not in ONGOING:
                raise TeamConflict('publication_not_pollable')
            if record.execution_state.state == 'uncertain' and record.execution_state.error_code == error_code:
                return record
            self._execution(conn, record, 'uncertain', error_code=error_code)
            conn.execute('UPDATE task_publication_outbox SET worker_id=NULL,lease_until=NULL WHERE delivery_operation_id=?',
                         (delivery_operation_id,))
            return self._stored(conn, delivery_operation_id)

    def fail_claim(self, claim, state, error_code):
        if state not in ('rejected', 'conflict', 'uncertain'):
            raise TeamConflict('invalid_publication_failure_state')
        if not isinstance(error_code, str) or not re.fullmatch('[a-z0-9_]{1,160}', error_code):
            raise TeamConflict('invalid_publication_error_code')
        with self.db.transaction() as conn:
            record, _ = self._require_claim(conn, claim)
            if record.dispatch_started and state != 'uncertain':
                raise TeamConflict('started_publication_requires_gateway_proof')
            self._execution(conn, record, state, error_code=error_code)
            conn.execute('UPDATE task_publication_outbox SET worker_id=NULL,lease_until=NULL WHERE delivery_operation_id=?',
                         (record.delivery_operation_id,))
            return self._stored(conn, record.delivery_operation_id)
