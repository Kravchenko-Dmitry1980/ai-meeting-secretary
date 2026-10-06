"""Scoped read-only projections of durable Team evidence and optional local budget."""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime

from secretary.domain.cloud_budget import BudgetSnapshot
from secretary.domain.team import (ExecutionState, TaskCommand, TeamForbidden, TeamMember,
                                   canonical, uuid_string)
from secretary.domain.team_reads import (CloudStatus, MemberPage, MemberView, SyncStatus,
                                        TaskHistoryEntry, TaskHistoryPage, TeamStatusView)


_SAFE_ERRORS = frozenset({'actor_unknown', 'actor_disabled', 'actor_binding_changed',
    'project_forbidden', 'owner_required', 'task_unavailable', 'task_not_assigned_to_actor',
    'owner_review_required', 'completion_result_required', 'member_mapping_changed',
    'task_revision_conflict', 'external_change_detected', 'external_change_between_steps',
    'remote_result_mismatch', 'remote_response_uncertain', 'remote_request_rejected',
    'lease_expired', 'worker_interrupted', 'operation_uncertain'})
_BUDGET_CODES = frozenset({'monthly_budget_configuration', 'monthly_budget_stale',
    'monthly_budget_exhausted', 'monthly_budget_unavailable', 'monthly_budget_uncertain',
    'monthly_budget_reconciliation_required'})


def _project(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,127}', value):
        raise ValueError('team_read_invalid')
    return value


def _limit(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError('team_read_invalid')


def _cursor(task_id, recorded_at, identifier):
    value = canonical({'task_id': task_id, 'at': recorded_at, 'id': identifier}).encode('utf-8')
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _decode_cursor(value, task_id):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', value):
            raise ValueError
        data = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
        if not isinstance(data, dict) or set(data) != {'task_id', 'at', 'id'} or data['task_id'] != task_id:
            raise ValueError
        at = datetime.fromisoformat(data['at'])
        if at.tzinfo is None or not re.fullmatch(r'(?:command:[0-9]{20}|external:[0-9a-f-]{36})', data['id']):
            raise ValueError
        if _cursor(task_id, data['at'], data['id']) != value:
            raise ValueError
        return data['at'], data['id']
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        raise ValueError('team_read_invalid') from None


class TeamReadRepository:
    def __init__(self, team, *, sync=None, budget_reader=None):
        self.team, self.db = team, team.db
        self.sync, self.budget_reader = sync, budget_reader

    def _actor(self, conn, actor, project_id, expected_revision):
        member = self.team._scope(conn, uuid_string(actor), _project(project_id))
        self.team._actor_revision(member, expected_revision)
        return member

    def members(self, actor, project_id, *, limit=50, after=None, expected_actor_revision=None):
        _limit(limit)
        after = uuid_string(after) if after is not None else None
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            owner = self._actor(conn, actor, project_id, expected_actor_revision)
            if owner.role != 'owner':
                raise TeamForbidden('owner_required')
            rows = conn.execute('''SELECT payload FROM team_members WHERE
                json_extract(payload,'$.enabled')=1
                AND EXISTS(SELECT 1 FROM json_each(team_members.payload,'$.project_ids') WHERE value=?)
                AND (? IS NULL OR id>?) ORDER BY id LIMIT ?''', (project_id, after, after, limit + 1))
            members = tuple(TeamMember.model_validate_json(row['payload']) for row in rows)
            values = tuple(MemberView(id=m.id, display_name=m.display_name, revision=m.revision) for m in members[:limit])
            return MemberPage(items=values, next_cursor=values[-1].id if len(members) > limit else None)

    def history(self, actor, task_id, *, limit=50, after=None, expected_actor_revision=None):
        _limit(limit)
        task_id = _project(task_id)
        marker = _decode_cursor(after, task_id)
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            task = self.team._projection(conn, task_id)
            if task is None:
                raise TeamForbidden('task_unavailable')
            member = self._actor(conn, actor, task.project_id, expected_actor_revision)
            # Only terminal verified effects are shared across project members.
            # Private attempts retain the command-read ACL, including meeting scope.
            sql = '''SELECT * FROM (
                SELECT 'command:'||printf('%020d',j.id) AS id,'command' AS kind,j.event,j.created_at AS recorded_at,
                    c.operation_id,c.actor_id,c.payload AS command,e.payload AS execution,
                    m.payload AS actor_payload,j.payload AS event_payload,
                    NULL AS changed_fields,NULL AS before_fingerprint,NULL AS after_fingerprint,NULL AS observation_error
                FROM team_journal j JOIN team_commands c USING(operation_id)
                JOIN team_execution e USING(operation_id) LEFT JOIN team_members m ON m.id=c.actor_id
                WHERE c.project_id=? AND COALESCE(json_extract(e.payload,'$.task_id'),c.task_id)=?
                    AND j.event IN ('accepted','running','reconciling','applied','conflict','uncertain','rejected')
                    AND (?=1 OR j.event='applied' OR (c.actor_id=? AND
                        COALESCE(json_extract(c.payload,'$.origin.source_kind'),'manual')!='meeting'))
                UNION ALL
                SELECT 'external:'||id,'external_change',event,recorded_at,
                    NULL,NULL,NULL,NULL,NULL,NULL,changed_fields,before_fingerprint,after_fingerprint,error_code
                FROM team_sync_observations WHERE project_id=? AND task_id=?
                    AND before_fingerprint IS NOT NULL
                    AND ((after_fingerprint IS NOT NULL AND before_fingerprint!=after_fingerprint)
                        OR (event='remote_task_missing' AND error_code='sync_task_missing'
                            AND after_fingerprint IS NULL))
            )'''
            args = [task.project_id, task_id, int(member.role == 'owner'), member.id, task.project_id, task_id]
            if marker:
                sql += ' WHERE recorded_at<? OR (recorded_at=? AND id<?)'
                args.extend((marker[0], marker[0], marker[1]))
            sql += ' ORDER BY recorded_at DESC,id DESC LIMIT ?'
            args.append(limit + 1)
            rows = conn.execute(sql, args).fetchall()
            items = tuple(self._history_entry(row) for row in rows[:limit])
            last = rows[limit - 1] if len(rows) > limit else None
            return TaskHistoryPage(items=items,
                next_cursor=_cursor(task_id, last['recorded_at'], last['id']) if last else None)

    @staticmethod
    def _history_entry(row):
        shared = dict(id=row['id'], kind=row['kind'], event=row['event'], recorded_at=row['recorded_at'])
        if row['kind'] == 'external_change':
            return TaskHistoryEntry(**shared, changed_fields=tuple(json.loads(row['changed_fields'])),
                before_fingerprint=row['before_fingerprint'], after_fingerprint=row['after_fingerprint'],
                error_code=row['observation_error'])
        command = TaskCommand.model_validate_json(row['command'])
        execution = ExecutionState.model_validate_json(row['execution'])
        author = TeamMember.model_validate_json(row['actor_payload']) if row['actor_payload'] else None
        error = json.loads(row['event_payload']).get('error_code')
        return TaskHistoryEntry(**shared, operation_id=command.operation_id, actor_id=row['actor_id'],
            actor_display_name=author.display_name if author else None, action=command.action, values=command.values,
            execution_state='queued' if row['event'] == 'accepted' else row['event'],
            verified_at=execution.verified_at if row['event'] == 'applied' else None,
            error_code=error if error in _SAFE_ERRORS else 'task_operation_failed' if error else None)

    def _cloud(self, owner):
        if self.budget_reader is None:
            return CloudStatus(state='not_configured')
        try:
            snapshot = self.budget_reader()
            if not isinstance(snapshot, BudgetSnapshot):
                raise ValueError('budget_snapshot_required')
            amounts = (snapshot.confirmed_micro, snapshot.reserved_micro,
                       snapshot.remaining_micro, snapshot.effective_limit_micro)
            if any(type(value) is not int or value < 0 for value in amounts):
                raise ValueError('budget_snapshot_invalid')
            paused = snapshot.paused_code or ('monthly_budget_exhausted' if snapshot.remaining_micro == 0 else None)
            if paused is not None and paused not in _BUDGET_CODES:
                paused = 'monthly_budget_unavailable'
            fields = {}
            if owner:
                fields = {key: f'{value // 1000000}.{value % 1000000:06d}' for key, value in zip(
                    ('spend_rub', 'reserved_rub', 'remaining_rub', 'effective_budget_rub'), amounts)}
            return CloudStatus(state='paused' if paused else 'ready', paused_reason=paused, **fields)
        except Exception:
            # The injected port is a local snapshot reader, never a provider refresh.
            return CloudStatus(state='unavailable', paused_reason='monthly_budget_unavailable')

    def status(self, actor, project_id, *, expected_actor_revision=None):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            member = self._actor(conn, actor, project_id, expected_actor_revision)
            row = conn.execute('''SELECT MAX(json_extract(e.payload,'$.verified_at')) AS verified
                FROM team_commands c JOIN team_execution e USING(operation_id)
                WHERE c.project_id=? AND e.state='applied' ''', (project_id,)).fetchone()
            verified = row['verified']
        sync = (self.sync.status(actor, project_id, expected_actor_revision=expected_actor_revision)
                if self.sync is not None else SyncStatus(state='not_configured'))
        cloud = self._cloud(member.role == 'owner')
        # Budget reading can take time; a concurrent revocation must not reveal aggregates.
        with self.db.connection() as conn:
            self._actor(conn, actor, project_id, member.revision)
        return TeamStatusView(project_id=project_id,
            sync=sync.model_copy(update={'last_command_verified_at': datetime.fromisoformat(verified) if verified else None}),
            cloud=cloud)
