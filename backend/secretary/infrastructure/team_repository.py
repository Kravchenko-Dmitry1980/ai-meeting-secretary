"""Durable Team commands and delivery, separate from canonical Vikunja tasks.

All writes use BEGIN IMMEDIATE. Claims fence local writes, not a remote server:
an expired sender remains uncertain and keeps its task blocked until reconciliation.
T4 must call authorize_claim immediately before sending and verify remote results.
Member/projection writes are trusted local administration/adapter methods, not API routes.
"""
from __future__ import annotations

import sqlite3
import json
import re
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from secretary.domain.team import (
    AcceptanceReceipt, ClaimedCommand, CommandReceipt, DurableMessage, ExecutionState,
    MessageState, TaskCommand, TaskSnapshot, TeamConflict, TeamForbidden, TeamMember,
    canonical, content_hash, uuid_string,
)


def _json(model):
    return canonical(model.model_dump(mode='json'))


def _expected_revision(value):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError('expected_revision_requires_nonnegative_integer')


class RepositoryMemberDirectory:
    """Fresh canonical identities for long-lived clients; no name matching."""
    def __init__(self, repository):
        self.repository = repository

    def get_by_id(self, identifier):
        return self.repository.get_member(identifier)

    def get_by_vikunja_id(self, identifier):
        return self.repository.get_member_by_vikunja_id(identifier)


class TeamRepository:
    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('clock_requires_timezone')
        return value.astimezone(timezone.utc)

    @staticmethod
    def _member(conn, actor_id):
        row = conn.execute('SELECT payload FROM team_members WHERE id=?', (actor_id,)).fetchone()
        if not row:
            raise TeamForbidden('actor_unknown')
        actor = TeamMember.model_validate_json(row['payload'])
        if not actor.enabled:
            raise TeamForbidden('actor_disabled')
        return actor

    @staticmethod
    def _projection(conn, task_id):
        row = conn.execute('SELECT payload FROM team_projections WHERE task_id=?', (task_id,)).fetchone()
        return TaskSnapshot.model_validate_json(row['payload']) if row else None

    def _scope(self, conn, actor_id, project_id):
        actor = self._member(conn, actor_id)
        if project_id not in actor.project_ids:
            raise TeamForbidden('project_forbidden')
        return actor

    def upsert_member(self, member: TeamMember, *, expected_revision):
        _expected_revision(expected_revision)
        member = TeamMember.model_validate(member.model_dump())
        with self.db.transaction() as conn:
            old = conn.execute('SELECT revision FROM team_members WHERE id=?', (member.id,)).fetchone()
            if ((old is None and (expected_revision is not None or member.revision != 0))
                    or (old is not None and (old['revision'] != expected_revision or member.revision != expected_revision + 1))):
                raise TeamConflict('member_revision_conflict')
            try:
                conn.execute('''INSERT INTO team_members VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    max_user_id=excluded.max_user_id,vikunja_user_id=excluded.vikunja_user_id,
                    revision=excluded.revision,payload=excluded.payload''',
                    (member.id, member.max_user_id, member.vikunja_user_id, member.revision, _json(member)))
            except sqlite3.IntegrityError:
                raise TeamConflict('member_identity_conflict') from None
        return member

    def resolve_member(self, max_user_id):
        # Refuse Number coercion at the lookup boundary as well as in DTOs.
        if not isinstance(max_user_id, str):
            raise ValueError('external_id_requires_decimal_string')
        with self.db.connection() as conn:
            row = conn.execute('SELECT payload FROM team_members WHERE max_user_id=?', (max_user_id,)).fetchone()
            value = TeamMember.model_validate_json(row['payload']) if row else None
            return value if value and value.enabled else None

    def get_member(self, identifier):
        """Trusted adapter directory lookup; not an unauthenticated HTTP route."""
        identifier = uuid_string(identifier)
        with self.db.connection() as conn:
            row = conn.execute('SELECT payload FROM team_members WHERE id=?', (identifier,)).fetchone()
            return TeamMember.model_validate_json(row['payload']) if row else None

    def list_members(self, actor, project_id) -> tuple[TeamMember, ...]:
        """Current enabled project directory, available only to its enabled owner."""
        actor = uuid_string(actor)
        if not isinstance(project_id, str) or not re.fullmatch(r'[1-9][0-9]{0,127}', project_id):
            raise ValueError('external_id_requires_decimal_string')
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            owner = self._scope(conn, actor, project_id)
            if owner.role != 'owner':
                raise TeamForbidden('owner_required')
            members = (TeamMember.model_validate_json(row['payload'])
                       for row in conn.execute('SELECT payload FROM team_members ORDER BY id'))
            return tuple(member for member in members
                         if member.enabled and project_id in member.project_ids)

    def get_member_by_vikunja_id(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[1-9][0-9]{0,127}', identifier):
            raise ValueError('external_id_requires_decimal_string')
        with self.db.connection() as conn:
            row = conn.execute('SELECT payload FROM team_members WHERE vikunja_user_id=?', (identifier,)).fetchone()
            return TeamMember.model_validate_json(row['payload']) if row else None

    @staticmethod
    def _save_projection(conn, snapshot, expected_revision):
        _expected_revision(expected_revision)
        old = conn.execute('SELECT project_id,revision,payload FROM team_projections WHERE task_id=?', (snapshot.task_id,)).fetchone()
        if old and old['project_id'] != snapshot.project_id:
            raise TeamConflict('task_project_changed')
        if ((old is None and (expected_revision is not None or snapshot.revision != 0))
                or (old is not None and (old['revision'] != expected_revision or snapshot.revision != expected_revision + 1))):
            raise TeamConflict('projection_revision_conflict')
        conn.execute('''INSERT INTO team_projections VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
            revision=excluded.revision,payload=excluded.payload''',
            (snapshot.task_id, snapshot.project_id, snapshot.revision, _json(snapshot)))

    def save_projection(self, snapshot: TaskSnapshot, *, expected_revision):
        snapshot = TaskSnapshot.model_validate(snapshot.model_dump())
        with self.db.transaction() as conn:
            self._save_projection(conn, snapshot, expected_revision)
        return snapshot

    @staticmethod
    def _actor_revision(member, expected):
        if expected is not None:
            _expected_revision(expected)
            if member.revision != expected:
                raise TeamForbidden('actor_binding_changed')

    def list_projections(self, actor, project_id, *, limit=50, after=None, mine=False,
                         bucket=None, expected_actor_revision=None):
        """Bounded, project-scoped projection page; command execution reads remote fresh."""
        actor = uuid_string(actor)
        for value in (project_id, after):
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,127}', value)):
                raise ValueError('external_id_requires_decimal_string')
        if type(limit) is not int or not 1 <= limit <= 100 or type(mine) is not bool:
            raise ValueError('projection_page_invalid')
        if bucket is not None and bucket not in {'inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled'}:
            raise ValueError('projection_bucket_invalid')
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            member = self._scope(conn, actor, project_id)
            self._actor_revision(member, expected_actor_revision)
            sql = 'SELECT payload FROM team_projections WHERE project_id=?'
            values = [project_id]
            if after is not None:
                sql += ' AND (length(task_id)>? OR (length(task_id)=? AND task_id>?))'
                values.extend((len(after), len(after), after))
            if mine:
                sql += " AND json_extract(payload,'$.assignee_id')=?"
                values.append(actor)
            if bucket is not None:
                sql += " AND json_extract(payload,'$.bucket')=?"
                values.append(bucket)
            sql += ' ORDER BY length(task_id),task_id LIMIT ?'
            values.append(limit + 1)
            return tuple(TaskSnapshot.model_validate_json(row['payload']) for row in conn.execute(sql, values))

    def get_projection(self, actor, task_id, *, expected_actor_revision=None):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            snapshot = self._projection(conn, task_id)
            if snapshot is None:
                raise TeamForbidden('task_unavailable')
            member = self._scope(conn, actor, snapshot.project_id)
            self._actor_revision(member, expected_actor_revision)
            return snapshot

    def _permission(self, conn, actor_id, command):
        actor = self._scope(conn, actor_id, command.project_id)
        if command.action == 'resolve_due':
            from secretary.infrastructure.team_due_resolution_repository import authorize_resolution
            authorize_resolution(conn, self, actor_id, command)
        if command.origin and command.origin.source_kind == 'meeting' and actor.role != 'owner':
            raise TeamForbidden('owner_required')
        snapshot = self._projection(conn, command.task_id) if command.task_id else None
        if command.task_id and (snapshot is None or snapshot.project_id != command.project_id):
            raise TeamForbidden('task_unavailable')
        if actor.role != 'owner':
            if command.action not in {'set_state', 'comment', 'propose_due'}:
                raise TeamForbidden('owner_required')
            if command.action != 'comment' and snapshot.assignee_id != actor.id:
                raise TeamForbidden('task_not_assigned_to_actor')
            if command.action == 'set_state':
                target = command.values.bucket
                if target in {'inbox', 'cancelled'} or snapshot.bucket in {'done', 'cancelled'}:
                    raise TeamForbidden('owner_required')
                if target == 'done' and (snapshot.important is not False or not snapshot.classification_confirmed):
                    raise TeamForbidden('owner_review_required')
                if target == 'done' and not command.values.result:
                    raise TeamForbidden('completion_result_required')
        if command.values.assignee_id:
            assignee = self._scope(conn, command.values.assignee_id, command.project_id)
            if command.expected_assignee_revision is not None and assignee.revision != command.expected_assignee_revision:
                raise TeamConflict('member_mapping_changed')
        return snapshot

    @staticmethod
    def _fresh(command, snapshot):
        if command.task_id and (snapshot.revision != command.expected_revision
                               or snapshot.remote_fingerprint != command.expected_fingerprint):
            raise TeamConflict('task_revision_conflict')

    def _journal(self, conn, operation_id, event, payload):
        conn.execute('INSERT INTO team_journal(operation_id,event,payload,created_at) VALUES(?,?,?,?)',
                     (operation_id, event, canonical(payload), self._now().isoformat()))

    @staticmethod
    def _receipt(conn, operation_id, *, include_current=True):
        row = conn.execute('''SELECT c.acceptance,c.task_id,c.project_id,e.payload FROM team_commands c
            JOIN team_execution e USING(operation_id) WHERE operation_id=?''', (operation_id,)).fetchone()
        if row is None:
            raise TeamForbidden('command_unavailable')
        execution = ExecutionState.model_validate_json(row['payload'])
        task_id = execution.task_id or row['task_id']
        current = TeamRepository._projection(conn, task_id) if include_current and task_id else None
        if current is not None and current.project_id != row['project_id']:
            current = None  # Rejected foreign task IDs must not become a read capability.
        return CommandReceipt(acceptance_receipt=AcceptanceReceipt.model_validate_json(row['acceptance']),
            execution_state=execution, current=current)

    def accept_command(self, actor, command: TaskCommand, *, expected_actor_revision=None):
        with self.db.transaction() as conn:
            return self.accept_command_in_transaction(conn, actor, command,
                expected_actor_revision=expected_actor_revision)

    def accept_command_in_transaction(self, conn, actor, command: TaskCommand, *, expected_actor_revision=None):
        """Canonical acceptance inside a caller-owned Team transaction (bot confirmation).

        The connection must belong to this Team database and hold BEGIN IMMEDIATE.
        No remote work is performed here; the caller commits acceptance and nonce
        consumption together, or rolls both back.
        """
        if not conn.in_transaction or conn.execute('PRAGMA database_list').fetchone()[2] != str(self.db.path):
            raise TeamConflict('team_transaction_required')
        actor = uuid_string(actor)
        command = TaskCommand.model_validate(command.model_dump(exclude_unset=True))
        payload = command.model_dump(mode='json', exclude_unset=True)
        digest = content_hash({'actor_id': actor, 'command': payload})
        if expected_actor_revision is not None:
            self._actor_revision(self._member(conn, actor), expected_actor_revision)
        old = conn.execute('SELECT actor_id,payload_hash FROM team_commands WHERE operation_id=?', (command.operation_id,)).fetchone()
        if old:
            if old['actor_id'] != actor or old['payload_hash'] != digest:
                raise TeamConflict('operation_id_conflict')
            current_actor = self._scope(conn, actor, command.project_id)
            if (command.action == 'resolve_due' or command.origin and command.origin.source_kind == 'meeting') and current_actor.role != 'owner':
                raise TeamForbidden('owner_required')
            return self._receipt(conn, command.operation_id)
        error = None
        state = 'queued'
        try:
            snapshot = self._permission(conn, actor, command)
            self._fresh(command, snapshot)
        except TeamForbidden as exc:
            error, state = str(exc), 'rejected'
        except TeamConflict as exc:
            error, state = str(exc), 'conflict'
        acceptance = AcceptanceReceipt(operation_id=command.operation_id, payload_hash=digest,
            decision='accepted' if state == 'queued' else 'rejected', decided_at=self._now(), error_code=error)
        execution = ExecutionState(state=state, error_code=error)
        conn.execute('INSERT INTO team_commands VALUES(?,?,?,?,?,?,?,?)',
            (command.operation_id, actor, command.project_id, command.task_id, digest, canonical(payload), _json(acceptance), self._now().isoformat()))
        conn.execute('INSERT INTO team_execution(operation_id,state,revision,payload) VALUES(?,?,?,?)',
                     (command.operation_id, state, 0, _json(execution)))
        self._journal(conn, command.operation_id, 'accepted' if state == 'queued' else state, {'actor_id': actor})
        return self._receipt(conn, command.operation_id, include_current=state != 'rejected')

    def get_receipt(self, actor, operation_id, *, expected_actor_revision=None):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            row = conn.execute('SELECT actor_id,project_id,payload FROM team_commands WHERE operation_id=?', (operation_id,)).fetchone()
            if row is None:
                raise TeamForbidden('command_unavailable')
            member = self._scope(conn, actor, row['project_id'])
            self._actor_revision(member, expected_actor_revision)
            stored_command = TaskCommand.model_validate_json(row['payload'])
            if (stored_command.action == 'resolve_due' or stored_command.origin and stored_command.origin.source_kind == 'meeting') and member.role != 'owner':
                raise TeamForbidden('owner_required')
            if member.role != 'owner' and row['actor_id'] != actor:
                raise TeamForbidden('command_unavailable')
            return self._receipt(conn, operation_id)

    @staticmethod
    def _resource(command):
        return 'task:' + command.task_id if command.task_id else 'create:' + command.operation_id

    def _transition(self, conn, operation_id, state, *, snapshot=None, error_code=None):
        row = conn.execute('SELECT revision,payload FROM team_execution WHERE operation_id=?', (operation_id,)).fetchone()
        old = ExecutionState.model_validate_json(row['payload'])
        execution = ExecutionState(state=state, revision=row['revision'] + 1,
            task_id=snapshot.task_id if snapshot else old.task_id,
            verified_at=self._now() if state == 'applied' else None, error_code=error_code)
        conn.execute('UPDATE team_execution SET state=?,revision=?,payload=? WHERE operation_id=?',
                     (state, execution.revision, _json(execution), operation_id))
        self._journal(conn, operation_id, state, {'error_code': error_code})
        return execution

    def _claim(self, conn, row, worker_id, lease_seconds, *, reconciliation=False):
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 4000:
            raise ValueError('worker_id_required')
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('invalid_lease_seconds')
        command = TaskCommand.model_validate_json(row['payload'])
        resource = self._resource(command)
        held = conn.execute('SELECT * FROM team_resources WHERE resource_key=?', (resource,)).fetchone()
        if held and held['operation_id'] not in (None, command.operation_id):
            return None
        fence = (held['fence'] if held else 0) + 1
        until = self._now() + timedelta(seconds=lease_seconds)
        conn.execute('''INSERT INTO team_resources VALUES(?,?,?) ON CONFLICT(resource_key) DO UPDATE SET
            operation_id=excluded.operation_id,fence=excluded.fence''', (resource, command.operation_id, fence))
        self._transition(conn, command.operation_id, 'reconciling' if reconciliation else 'running')
        conn.execute('UPDATE team_execution SET worker_id=?,fence=?,lease_until=?,reconciliation=? WHERE operation_id=?',
            (worker_id, fence, until.timestamp(), int(reconciliation), command.operation_id))
        return ClaimedCommand(command=command, actor_id=row['actor_id'], worker_id=worker_id,
                              fence=fence, lease_until=until, reconciliation=reconciliation)

    def claim_command(self, worker_id, *, lease_seconds=60):
        with self.db.transaction() as conn:
            rows = conn.execute('''SELECT c.* FROM team_commands c JOIN team_execution e USING(operation_id)
                WHERE e.state='queued' ORDER BY c.rowid''').fetchall()
            for row in rows:
                command = TaskCommand.model_validate_json(row['payload'])
                try:
                    snapshot = self._permission(conn, row['actor_id'], command)
                    self._fresh(command, snapshot)
                except (TeamForbidden, TeamConflict) as exc:
                    self._transition(conn, command.operation_id, 'rejected' if isinstance(exc, TeamForbidden) else 'conflict', error_code=str(exc))
                    continue
                result = self._claim(conn, row, worker_id, lease_seconds)
                if result:
                    return result
        return None

    def _validate_claim(self, conn, claim):
        row = conn.execute('SELECT * FROM team_execution WHERE operation_id=?', (claim.command.operation_id,)).fetchone()
        held = conn.execute('SELECT * FROM team_resources WHERE resource_key=?', (self._resource(claim.command),)).fetchone()
        saved = conn.execute('SELECT actor_id,payload FROM team_commands WHERE operation_id=?', (claim.command.operation_id,)).fetchone()
        try:
            validated = TaskCommand.model_validate(claim.command.model_dump(exclude_unset=True))
            supplied_payload = canonical(validated.model_dump(mode='json', exclude_unset=True))
        except (ValidationError, ValueError):
            raise TeamConflict('stale_claim') from None
        if (not row or not held or not saved or saved['actor_id'] != claim.actor_id
                or saved['payload'] != supplied_payload
                or row['state'] not in {'running', 'reconciling'} or row['worker_id'] != claim.worker_id
                or row['fence'] != claim.fence or held['fence'] != claim.fence
                or held['operation_id'] != claim.command.operation_id
                or row['lease_until'] <= self._now().timestamp()
                or bool(row['reconciliation']) != claim.reconciliation):
            raise TeamConflict('stale_claim')
        return row

    def authorize_claim(self, claim):
        with self.db.transaction() as conn:
            self._validate_claim(conn, claim)
            if claim.reconciliation:
                raise TeamConflict('reconciliation_is_read_only')
            snapshot = self._permission(conn, claim.actor_id, claim.command)
            self._fresh(claim.command, snapshot)
            return snapshot

    def authorize_reconciliation(self, claim):
        """Fence and scope a read-only recovery, including a create without ID."""
        with self.db.transaction() as conn:
            self._validate_claim(conn, claim)
            if not claim.reconciliation:
                raise TeamConflict('reconciliation_claim_required')
            actor = self._scope(conn, claim.actor_id, claim.command.project_id)
            if claim.command.action == 'resolve_due':
                from secretary.infrastructure.team_due_resolution_repository import authorize_resolution
                authorize_resolution(conn, self, claim.actor_id, claim.command)
            if claim.command.origin and claim.command.origin.source_kind == 'meeting' and actor.role != 'owner':
                raise TeamForbidden('owner_required')
            return self._projection(conn, claim.command.task_id) if claim.command.task_id else None

    def renew_claim(self, claim, *, lease_seconds=60):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('invalid_lease_seconds')
        with self.db.transaction() as conn:
            self._validate_claim(conn, claim)
            until = self._now() + timedelta(seconds=lease_seconds)
            conn.execute('UPDATE team_execution SET lease_until=? WHERE operation_id=?',
                         (until.timestamp(), claim.command.operation_id))
            return claim.model_copy(update={'lease_until': until})

    @staticmethod
    def _verify_member_bindings(conn, bindings):
        if not isinstance(bindings, list) or len(bindings) > 10:
            raise TeamConflict('invalid_member_bindings')
        for expected in bindings:
            if not isinstance(expected, dict) or set(expected) != {'id', 'revision', 'vikunja_user_id'}:
                raise TeamConflict('invalid_member_bindings')
            if (type(expected['revision']) is not int or expected['revision'] < 0
                    or not isinstance(expected['vikunja_user_id'], str)
                    or not re.fullmatch(r'[1-9][0-9]{0,127}', expected['vikunja_user_id'])):
                raise TeamConflict('invalid_member_bindings')
            try:
                identifier = uuid_string(expected['id'])
            except ValueError:
                raise TeamConflict('invalid_member_bindings') from None
            row = conn.execute('SELECT payload FROM team_members WHERE id=?', (identifier,)).fetchone()
            current = TeamMember.model_validate_json(row['payload']) if row else None
            if (current is None or current.revision != expected['revision']
                    or current.vikunja_user_id != expected['vikunja_user_id']):
                raise TeamConflict('member_mapping_changed')

    def record_remote_result(self, claim, *, state, snapshot=None, error_code=None, expected_member_bindings=None):
        """Record a verified adapter result, never infer an outcome from a timeout.

        A live ``reconciling`` state can describe deterministic partial progress
        of a multi-request command under its original lease. Only a claim created
        by ``claim_reconciliation`` after uncertainty is read-only. T4 owns the
        remote step protocol; this repository never retries a remote mutation.
        Partial snapshots require that future durable step protocol and cannot
        replace the baseline projection or advance its revision here.
        """
        if state not in {'applied', 'conflict', 'uncertain', 'rejected', 'reconciling'}:
            raise ValueError('invalid_result_state')
        if state == 'reconciling' and snapshot is not None:
            raise TeamConflict('partial_snapshot_requires_durable_step_protocol')
        if snapshot is not None:
            snapshot = TaskSnapshot.model_validate(snapshot.model_dump())
        captured_members = json.loads(canonical(expected_member_bindings)) if expected_member_bindings is not None else None
        with self.db.transaction() as conn:
            self._validate_claim(conn, claim)
            if captured_members is not None:
                self._verify_member_bindings(conn, captured_members)
            command = claim.command
            if state == 'applied' and snapshot is None:
                raise TeamConflict('applied_requires_verified_snapshot')
            if state == 'applied' and command.action == 'resolve_due':
                self._fresh(command, self._permission(conn, claim.actor_id, command))
            if state == 'applied' and command.origin and command.origin.source_kind == 'meeting':
                actor = self._scope(conn, claim.actor_id, command.project_id)
                if actor.role != 'owner':
                    raise TeamForbidden('owner_required')
            if snapshot:
                if snapshot.project_id != command.project_id or command.task_id and snapshot.task_id != command.task_id:
                    raise TeamConflict('remote_snapshot_scope_mismatch')
                old = self._projection(conn, snapshot.task_id)
                self._save_projection(conn, snapshot, old.revision if old else None)
            self._transition(conn, command.operation_id, state, snapshot=snapshot, error_code=error_code)
            if state in {'applied', 'conflict', 'rejected'}:
                conn.execute('UPDATE team_resources SET operation_id=NULL WHERE resource_key=? AND operation_id=?',
                             (self._resource(command), command.operation_id))
                conn.execute('UPDATE team_execution SET worker_id=NULL,lease_until=NULL WHERE operation_id=?', (command.operation_id,))
            elif state == 'uncertain':
                conn.execute('UPDATE team_execution SET worker_id=NULL,lease_until=NULL WHERE operation_id=?', (command.operation_id,))
            return self._receipt(conn, command.operation_id)

    @staticmethod
    def _remote_progress(conn, operation_id):
        rows = conn.execute("""SELECT event,payload FROM team_journal WHERE operation_id=?
            AND event IN ('remote_plan','remote_step_started','remote_step_verified','remote_step_uncertain','remote_step_rejected')
            ORDER BY id""", (operation_id,)).fetchall()
        result = {"plan": None, "plan_hash": None, "steps": []}
        for row in rows:
            payload = json.loads(row["payload"])
            if row["event"] == "remote_plan":
                if result["plan"] is not None:
                    raise TeamConflict("remote_plan_conflict")
                result.update(plan=payload["steps"], plan_hash=payload["plan_hash"])
            elif row["event"] == "remote_step_started":
                if payload["ordinal"] != len(result["steps"]):
                    raise TeamConflict("remote_step_journal_conflict")
                result["steps"].append({**payload, "state": "started"})
            else:
                ordinal = payload["ordinal"]
                if not 0 <= ordinal < len(result["steps"]) or result["steps"][ordinal]["state"] != "started":
                    raise TeamConflict("remote_step_journal_conflict")
                result["steps"][ordinal].update(payload)
                result["steps"][ordinal]["state"] = row["event"].removeprefix('remote_step_')
        return result

    def read_remote_progress(self, claim):
        with self.db.transaction() as conn:
            self._validate_claim(conn, claim)
            return self._remote_progress(conn, claim.command.operation_id)

    def save_remote_plan(self, claim, steps):
        if claim.reconciliation:
            raise TeamConflict("reconciliation_is_read_only")
        if not isinstance(steps, list) or len(steps) > 32 or any(not isinstance(step, dict) for step in steps):
            raise ValueError("invalid_remote_plan")
        encoded = canonical(steps)
        if len(encoded.encode()) > 256 * 1024:
            raise ValueError("remote_plan_too_large")
        # The caller can retain and mutate nested dictionaries while we wait
        # for BEGIN IMMEDIATE. Persist the exact captured bytes, not that list.
        captured = json.loads(encoded)
        if not isinstance(captured, list) or len(captured) > 32 or any(not isinstance(step, dict) for step in captured):
            raise ValueError("invalid_remote_plan")
        digest = content_hash(captured)
        with self.db.transaction() as conn:
            self._validate_claim(conn, claim)
            self._permission(conn, claim.actor_id, claim.command)
            progress = self._remote_progress(conn, claim.command.operation_id)
            if progress["plan"] is not None:
                if progress["plan_hash"] != digest:
                    raise TeamConflict("remote_plan_conflict")
                return progress
            self._journal(conn, claim.command.operation_id, "remote_plan", {"steps": captured, "plan_hash": digest})
            return self._remote_progress(conn, claim.command.operation_id)

    def begin_remote_step(self, claim, ordinal, request_hash, before_fingerprint, *, before_image=None):
        if claim.reconciliation:
            raise TeamConflict("reconciliation_is_read_only")
        if (type(ordinal) is not int or not 0 <= ordinal < 32 or not isinstance(request_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", request_hash)):
            raise ValueError("invalid_remote_step")
        if before_image is not None and not isinstance(before_image, dict):
            raise ValueError("invalid_remote_before_image")
        captured_image = json.loads(canonical(before_image))
        if len(canonical(captured_image).encode()) > 2 * 1024 * 1024:
            raise ValueError("remote_before_image_too_large")
        with self.db.transaction() as conn:
            self._validate_claim(conn, claim)
            self._fresh(claim.command, self._permission(conn, claim.actor_id, claim.command))
            progress = self._remote_progress(conn, claim.command.operation_id)
            if progress["plan"] is None or ordinal >= len(progress["plan"]):
                raise TeamConflict("unknown_remote_step")
            self._verify_member_bindings(conn, progress['plan'][ordinal].get('member_bindings', []))
            if ordinal < len(progress["steps"]):
                raise TeamConflict("remote_step_already_started")
            if ordinal != len(progress["steps"]) or (ordinal and progress["steps"][-1]["state"] != "verified"):
                raise TeamConflict("remote_step_predecessor_unverified")
            expected = progress["steps"][-1]["after_fingerprint"] if ordinal else claim.command.expected_fingerprint
            if claim.command.action == 'resolve_due':
                from secretary.infrastructure.team_due_resolution_repository import authorize_resolution
                resolution = authorize_resolution(conn, self, claim.actor_id, claim.command)
                if not ordinal:
                    expected = resolution.preview.candidate.observed_fingerprint
                if content_hash(captured_image) != before_fingerprint:
                    raise TeamConflict('due_resolution_before_image_invalid')
            if before_fingerprint != expected:
                raise TeamConflict("remote_step_fingerprint_conflict")
            self._journal(conn, claim.command.operation_id, "remote_step_started", {
                "ordinal": ordinal, "request_hash": request_hash, "before_fingerprint": before_fingerprint,
                "before_image": captured_image, "fence": claim.fence, "worker_id": claim.worker_id})
            return self._remote_progress(conn, claim.command.operation_id)["steps"][ordinal]

    def remote_start_fingerprint(self, claim):
        """Only the dedicated consumed-preview action starts from outside facts."""
        with self.db.transaction() as conn:
            self._validate_claim(conn, claim)
            self._fresh(claim.command, self._permission(conn, claim.actor_id, claim.command))
            if claim.command.action == 'resolve_due':
                from secretary.infrastructure.team_due_resolution_repository import authorize_resolution
                value = authorize_resolution(conn, self, claim.actor_id, claim.command)
                return value.preview.candidate.observed_fingerprint
            return claim.command.expected_fingerprint

    def record_remote_step(self, claim, ordinal, *, state, after_fingerprint=None, remote_entity_id=None, error_code=None):
        if state not in {"verified", "uncertain", "rejected"} or type(ordinal) is not int or not 0 <= ordinal < 32:
            raise ValueError("invalid_remote_step")
        if state == "verified" and (not isinstance(after_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", after_fingerprint)):
            raise ValueError("invalid_remote_step_fingerprint")
        if remote_entity_id is not None and (not isinstance(remote_entity_id, str) or not re.fullmatch(r"[1-9][0-9]{0,127}", remote_entity_id)):
            raise ValueError("invalid_remote_entity_id")
        if error_code is not None and (not isinstance(error_code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", error_code)):
            raise ValueError("invalid_remote_step_error")
        payload = {"ordinal": ordinal, "after_fingerprint": after_fingerprint,
                   "remote_entity_id": remote_entity_id, "error_code": error_code}
        with self.db.transaction() as conn:
            self._validate_claim(conn, claim)
            progress = self._remote_progress(conn, claim.command.operation_id)
            if ordinal >= len(progress["steps"]):
                raise TeamConflict("remote_step_not_started")
            old = progress["steps"][ordinal]
            if old["state"] != "started":
                if old["state"] != state or any(old.get(key) != value for key, value in payload.items()):
                    raise TeamConflict("remote_step_result_conflict")
                return old
            self._journal(conn, claim.command.operation_id, "remote_step_" + state, payload)
            return self._remote_progress(conn, claim.command.operation_id)["steps"][ordinal]

    def recover_expired(self):
        count = 0
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT operation_id FROM team_execution WHERE state IN ('running','reconciling') AND lease_until<=?",
                                (self._now().timestamp(),)).fetchall()
            for row in rows:
                self._transition(conn, row['operation_id'], 'uncertain', error_code='lease_expired')
                conn.execute('UPDATE team_execution SET worker_id=NULL,lease_until=NULL WHERE operation_id=?', (row['operation_id'],))
                count += 1
            count += conn.execute("UPDATE team_message_state SET state='uncertain',worker_id=NULL,lease_until=NULL,error_code='lease_expired' WHERE state='sending' AND lease_until<=?",
                                  (self._now().timestamp(),)).rowcount
        return count

    def claim_reconciliation(self, operation_id, worker_id, *, lease_seconds=60):
        with self.db.transaction() as conn:
            row = conn.execute('''SELECT c.* FROM team_commands c JOIN team_execution e USING(operation_id)
                WHERE operation_id=? AND e.state='uncertain' ''', (operation_id,)).fetchone()
            if not row:
                raise TeamConflict('command_not_uncertain')
            return self._claim(conn, row, worker_id, lease_seconds, reconciliation=True)

    @staticmethod
    def _message(conn, identifier):
        row = conn.execute('''SELECT m.payload,s.* FROM team_messages m JOIN team_message_state s USING(id)
            WHERE m.id=?''', (identifier,)).fetchone()
        if not row:
            raise TeamConflict('message_not_found')
        return MessageState(message=DurableMessage.model_validate_json(row['payload']), state=row['state'],
            fence=row['fence'], worker_id=row['worker_id'],
            lease_until=datetime.fromtimestamp(row['lease_until'], timezone.utc) if row['lease_until'] is not None else None,
            error_code=row['error_code'], reconciliation=bool(row['reconciliation']))

    def enqueue_message(self, message: DurableMessage):
        message = DurableMessage.model_validate(message.model_dump())
        # Provider dedup identity is independent of a caller's freshly generated UUID.
        digest = content_hash(message.model_dump(mode='json', exclude={'id'}))
        with self.db.transaction() as conn:
            old = conn.execute('SELECT id,payload_hash FROM team_messages WHERE (kind=? AND dedup_key=?) OR id=?',
                               (message.kind, message.dedup_key, message.id)).fetchall()
            if old:
                if len(old) != 1 or old[0]['payload_hash'] != digest:
                    raise TeamConflict('message_identity_conflict')
                return self._message(conn, old[0]['id'])
            self._scope(conn, message.actor_id, message.project_id)
            conn.execute('INSERT INTO team_messages VALUES(?,?,?,?,?,?)',
                (message.id, message.kind, message.dedup_key, digest, _json(message), self._now().isoformat()))
            conn.execute("INSERT INTO team_message_state(id,state) VALUES(?,'pending')", (message.id,))
            return self._message(conn, message.id)

    def get_message(self, identifier):
        """Internal worker view; public APIs must not expose arbitrary inbox payloads."""
        with self.db.connection() as conn:
            return self._message(conn, identifier)

    def claim_message(self, kind, worker_id, *, lease_seconds=60):
        if kind not in {'inbox', 'outbox'} or not worker_id or type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('invalid_message_claim')
        with self.db.transaction() as conn:
            rows = conn.execute('''SELECT m.id FROM team_messages m JOIN team_message_state s USING(id)
                WHERE m.kind=? AND s.state='pending' ORDER BY m.rowid''', (kind,)).fetchall()
            for row in rows:
                message = self._message(conn, row['id']).message
                try:
                    self._scope(conn, message.actor_id, message.project_id)
                except TeamForbidden as exc:
                    conn.execute("UPDATE team_message_state SET state='cancelled',error_code=? WHERE id=?", (str(exc), message.id))
                    continue
                until = self._now() + timedelta(seconds=lease_seconds)
                conn.execute("UPDATE team_message_state SET state='sending',fence=fence+1,worker_id=?,lease_until=? WHERE id=?",
                             (worker_id, until.timestamp(), message.id))
                return self._message(conn, message.id)
        return None

    def finish_message(self, claim: MessageState, *, state, error_code=None):
        if state not in {'sent', 'uncertain', 'failed', 'cancelled'}:
            raise ValueError('invalid_message_result')
        with self.db.transaction() as conn:
            self._validate_message_claim(conn, claim)
            conn.execute('UPDATE team_message_state SET state=?,worker_id=NULL,lease_until=NULL,error_code=? WHERE id=?',
                         (state, error_code, claim.message.id))
            return self._message(conn, claim.message.id)

    def _validate_message_claim(self, conn, claim):
        current = self._message(conn, claim.message.id)
        if (current.state != 'sending' or current.fence != claim.fence or current.worker_id != claim.worker_id
                or current.message != claim.message or current.lease_until is None
                or current.lease_until <= self._now() or current.reconciliation != claim.reconciliation):
            raise TeamConflict('stale_message_claim')
        return current

    def authorize_message(self, claim):
        """Mandatory immediately before T7/T10 send or any paid inbox work."""
        with self.db.transaction() as conn:
            current = self._validate_message_claim(conn, claim)
            if current.reconciliation:
                raise TeamConflict('reconciliation_is_read_only')
            self._scope(conn, current.message.actor_id, current.message.project_id)
            return current.message

    def renew_message(self, claim, *, lease_seconds=60):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('invalid_lease_seconds')
        with self.db.transaction() as conn:
            self._validate_message_claim(conn, claim)
            until = self._now() + timedelta(seconds=lease_seconds)
            conn.execute('UPDATE team_message_state SET lease_until=? WHERE id=?', (until.timestamp(), claim.message.id))
            return self._message(conn, claim.message.id)

    def claim_message_reconciliation(self, identifier, worker_id, *, lease_seconds=60):
        if not worker_id or type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('invalid_message_claim')
        with self.db.transaction() as conn:
            current = self._message(conn, identifier)
            if current.state != 'uncertain':
                raise TeamConflict('message_not_uncertain')
            until = self._now() + timedelta(seconds=lease_seconds)
            conn.execute("UPDATE team_message_state SET state='sending',fence=fence+1,worker_id=?,lease_until=?,reconciliation=1 WHERE id=?",
                         (worker_id, until.timestamp(), identifier))
            return self._message(conn, identifier)
