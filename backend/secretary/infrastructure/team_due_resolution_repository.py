"""Immutable previews and atomic once-only deadline confirmations in Team SQLite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from secretary.domain.team import TaskCommand, TeamConflict, TeamForbidden, canonical, content_hash, uuid_string
from secretary.domain.team_due_resolution import (
    DueResolutionCandidate, DueResolutionPreview, StoredDueResolution,
)


DUE_RESOLUTION_MIGRATION = (
    '''CREATE TABLE team_due_resolution_previews(preview_id TEXT PRIMARY KEY NOT NULL,
        actor_id TEXT NOT NULL,project_id TEXT NOT NULL,task_id TEXT NOT NULL,
        observation_id TEXT NOT NULL REFERENCES team_sync_observations(id),
        payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE team_due_resolution_consumptions(preview_id TEXT PRIMARY KEY NOT NULL
        REFERENCES team_due_resolution_previews(preview_id),operation_id TEXT UNIQUE NOT NULL
        REFERENCES team_commands(operation_id) DEFERRABLE INITIALLY DEFERRED,confirmed_at TEXT NOT NULL)''',
    'CREATE INDEX team_due_resolution_scope ON team_due_resolution_previews(project_id,task_id)',
    *tuple(f'''CREATE TRIGGER {table}_immutable_{verb.lower()} BEFORE {verb} ON {table}
        BEGIN SELECT RAISE(ABORT,'Immutable due resolution evidence'); END'''
        for table in ('team_due_resolution_previews', 'team_due_resolution_consumptions') for verb in ('UPDATE', 'DELETE')),
    *tuple(f'''CREATE TRIGGER {table}_replace_guard BEFORE INSERT ON {table}
        WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition})
        BEGIN SELECT RAISE(ABORT,'Immutable due resolution evidence'); END'''
        for table, condition in (('team_due_resolution_previews', 'preview_id=NEW.preview_id'),
            ('team_due_resolution_consumptions', 'preview_id=NEW.preview_id OR operation_id=NEW.operation_id'))),
)


def _stored(conn, identifier):
    row = conn.execute('SELECT * FROM team_due_resolution_previews WHERE preview_id=?', (identifier,)).fetchone()
    if row is None:
        raise TeamForbidden('due_resolution_preview_unavailable')
    value = StoredDueResolution.model_validate_json(row['payload'])
    candidate = value.preview.candidate
    if (row['payload_hash'] != content_hash(value.model_dump(mode='json'))
            or row['preview_id'] != value.preview.preview_id or row['actor_id'] != value.actor_id
            or row['project_id'] != candidate.project_id or row['task_id'] != candidate.task_id
            or row['observation_id'] != candidate.observation_id
            or content_hash(value.before_image) != candidate.observed_fingerprint):
        raise TeamConflict('due_resolution_evidence_invalid')
    return value


def authorize_resolution(conn, team, actor_id, command):
    """Called on every claim/step/final boundary; never relax ordinary _fresh."""
    if command.action != 'resolve_due':
        raise TeamConflict('due_resolution_action_required')
    value = _stored(conn, command.resolution_id)
    preview, candidate = value.preview, value.preview.candidate
    actor = team._scope(conn, actor_id, candidate.project_id)
    if actor.role != 'owner' or actor.id != value.actor_id:
        raise TeamForbidden('owner_required')
    team._actor_revision(actor, value.actor_revision)
    consumption = conn.execute('SELECT operation_id FROM team_due_resolution_consumptions WHERE preview_id=?',
                               (preview.preview_id,)).fetchone()
    if consumption is None or consumption['operation_id'] != command.operation_id:
        raise TeamForbidden('due_resolution_confirmation_required')
    expected = {'due_at': preview.model_dump(mode='json')['due_at'],
                'due_confirmed': True, 'reason': preview.reason}
    if (command.project_id != candidate.project_id or command.task_id != candidate.task_id
            or command.expected_revision != candidate.baseline_revision
            or command.expected_fingerprint != candidate.baseline_fingerprint
            or command.values.model_dump(mode='json', exclude_unset=True) != expected):
        raise TeamConflict('due_resolution_intent_mismatch')
    baseline = team._projection(conn, candidate.task_id)
    if (baseline is None or baseline.project_id != candidate.project_id
            or baseline.revision != candidate.baseline_revision
            or baseline.remote_fingerprint != candidate.baseline_fingerprint):
        raise TeamConflict('task_revision_conflict')
    return value


class TeamDueResolutionRepository:
    def __init__(self, team, clock=None):
        self.team, self.db = team, team.db
        self.clock = clock or team._now

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('due_resolution_clock_invalid')
        return value.astimezone(timezone.utc)

    def _owner(self, conn, actor, project, expected_actor_revision=None):
        member = self.team._scope(conn, actor, project)
        self.team._actor_revision(member, expected_actor_revision)
        if member.role != 'owner':
            raise TeamForbidden('owner_required')
        return member

    def _pending(self, conn, actor, task_id, expected_actor_revision=None, observation_id=None):
        baseline = self.team._projection(conn, task_id)
        if baseline is None:
            raise TeamForbidden('task_unavailable')
        owner = self._owner(conn, actor, baseline.project_id, expected_actor_revision)
        held = conn.execute('SELECT operation_id FROM team_resources WHERE resource_key=?', ('task:' + task_id,)).fetchone()
        if held and held['operation_id'] is not None:
            raise TeamConflict('due_resolution_task_busy')
        latest = conn.execute('''SELECT rowid AS evidence_sequence,* FROM team_sync_observations WHERE project_id=? AND task_id=?
            ORDER BY rowid DESC LIMIT 1''', (baseline.project_id, task_id)).fetchone()
        observation = (conn.execute('SELECT rowid AS evidence_sequence,* FROM team_sync_observations WHERE id=?', (observation_id,)).fetchone()
                       if observation_id else latest)
        if (not latest or not observation or latest['error_code'] != 'remote_due_unconfirmed'
                or observation['error_code'] != 'remote_due_unconfirmed'
                or observation['project_id'] != baseline.project_id or observation['task_id'] != task_id
                or observation['before_fingerprint'] != baseline.remote_fingerprint
                or latest['before_fingerprint'] != baseline.remote_fingerprint
                or not observation['after_fingerprint']
                or observation['after_fingerprint'] != latest['after_fingerprint']):
            raise TeamConflict('due_resolution_not_required')
        superseded = conn.execute('''SELECT 1 FROM team_sync_observations WHERE project_id=? AND task_id=?
            AND rowid>? AND after_fingerprint IS NOT ? LIMIT 1''',
            (baseline.project_id, task_id, observation['evidence_sequence'], observation['after_fingerprint'])).fetchone()
        if superseded:
            # Immutable evidence invalidates A→B→A previews too. A same-fact
            # periodic poll may use a new UUID without changing the decision.
            raise TeamConflict('due_resolution_candidate_changed')
        return owner, baseline, observation

    def pending(self, actor, task_id, *, expected_actor_revision=None, observation_id=None):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            return self._pending(conn, actor, task_id, expected_actor_revision, observation_id)

    def save_preview(self, actor, request, candidate, before_image, *, expected_actor_revision=None, ttl_seconds=600):
        candidate = DueResolutionCandidate.model_validate(candidate.model_dump())
        captured_image = json.loads(canonical(before_image))
        if (content_hash(captured_image) != candidate.observed_fingerprint
                or len(canonical(captured_image).encode('utf-8')) > 2 * 1024 * 1024):
            raise TeamConflict('due_resolution_evidence_invalid')
        with self.db.transaction() as conn:
            owner, baseline, observation = self._pending(conn, actor, candidate.task_id,
                expected_actor_revision, candidate.observation_id)
            if (request.observation_id != candidate.observation_id or request.expected_revision != baseline.revision
                    or candidate.baseline_revision != baseline.revision
                    or candidate.baseline_fingerprint != baseline.remote_fingerprint
                    or request.expected_fingerprint != observation['after_fingerprint']
                    or candidate.observed_fingerprint != observation['after_fingerprint']):
                raise TeamConflict('due_resolution_candidate_changed')
            now = self._now()
            preview = DueResolutionPreview(preview_id=str(uuid4()), candidate=candidate,
                due_at=request.due_at, reason=request.reason, created_at=now, expires_at=now + timedelta(seconds=ttl_seconds))
            value = StoredDueResolution(preview=preview, actor_id=owner.id,
                actor_revision=owner.revision, before_image=captured_image)
            conn.execute('INSERT INTO team_due_resolution_previews VALUES(?,?,?,?,?,?,?)',
                (preview.preview_id, owner.id, baseline.project_id, baseline.task_id, candidate.observation_id,
                 content_hash(value.model_dump(mode='json')), value.model_dump_json()))
            return preview

    def confirmation_state(self, actor, preview_id, operation_id, *, expected_actor_revision=None):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            value = _stored(conn, uuid_string(preview_id))
            owner = self._owner(conn, actor, value.preview.candidate.project_id, expected_actor_revision)
            if owner.id != value.actor_id:
                raise TeamForbidden('owner_required')
            old = conn.execute('SELECT operation_id FROM team_due_resolution_consumptions WHERE preview_id=?',
                               (preview_id,)).fetchone()
            if old:
                if old['operation_id'] != operation_id:
                    raise TeamConflict('due_resolution_already_confirmed')
                return value, self.team._receipt(conn, operation_id)
            self.team._actor_revision(owner, value.actor_revision)
            if self._now() >= value.preview.expires_at:
                raise TeamConflict('due_resolution_preview_expired')
            self._pending(conn, actor, value.preview.candidate.task_id, expected_actor_revision,
                          value.preview.candidate.observation_id)
            return value, None

    def accept(self, actor, value, operation_id, observed_fingerprint, *, expected_actor_revision=None):
        with self.db.transaction() as conn:
            stored = _stored(conn, value.preview.preview_id)
            if stored != value:
                raise TeamConflict('due_resolution_evidence_invalid')
            owner = self._owner(conn, actor, value.preview.candidate.project_id, expected_actor_revision)
            if owner.id != value.actor_id:
                raise TeamForbidden('owner_required')
            old = conn.execute('SELECT operation_id FROM team_due_resolution_consumptions WHERE preview_id=?',
                               (value.preview.preview_id,)).fetchone()
            if old:
                if old['operation_id'] != operation_id:
                    raise TeamConflict('due_resolution_already_confirmed')
                return self.team._receipt(conn, operation_id)
            self.team._actor_revision(owner, value.actor_revision)
            if self._now() >= value.preview.expires_at:
                raise TeamConflict('due_resolution_preview_expired')
            candidate = value.preview.candidate
            _, baseline, observation = self._pending(conn, actor, candidate.task_id,
                expected_actor_revision, candidate.observation_id)
            if (baseline.revision != candidate.baseline_revision or baseline.remote_fingerprint != candidate.baseline_fingerprint
                    or observation['after_fingerprint'] != candidate.observed_fingerprint
                    or observed_fingerprint != candidate.observed_fingerprint):
                raise TeamConflict('due_resolution_remote_changed')
            command = TaskCommand(operation_id=operation_id, project_id=candidate.project_id, task_id=candidate.task_id,
                expected_revision=candidate.baseline_revision, expected_fingerprint=candidate.baseline_fingerprint,
                action='resolve_due', resolution_id=value.preview.preview_id,
                values={'due_at': value.preview.due_at, 'due_confirmed': True, 'reason': value.preview.reason})
            conn.execute('INSERT INTO team_due_resolution_consumptions VALUES(?,?,?)',
                         (value.preview.preview_id, operation_id, self._now().isoformat()))
            receipt = self.team.accept_command_in_transaction(conn, actor, command,
                expected_actor_revision=expected_actor_revision)
            if receipt.acceptance_receipt.decision != 'accepted':
                raise TeamConflict(receipt.acceptance_receipt.error_code or 'due_resolution_acceptance_failed')
            return receipt
