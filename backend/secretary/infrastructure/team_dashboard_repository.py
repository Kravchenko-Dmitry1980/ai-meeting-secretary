"""Read-only owner dashboard from explicit local ports, never provider refreshes.

An absent port is not configured, even when its tables happen to exist. Generic
multi-project replies cannot be attributed; only a frozen single-project event
or a canonical command reference provides project attribution. Reading intake
does not prove authenticated webhook acceptance or current webhook health.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from datetime import datetime, timezone

from secretary.domain.bot import SendReceipt
from secretary.domain.team import AcceptanceReceipt, ExecutionState, TeamForbidden, canonical, uuid_string
from secretary.domain.team_dashboard import (DashboardDeliverySummary, DashboardNotificationSummary,
    DashboardWebhookStatus, OwnerCommandItem, OwnerCommandPage, OwnerDashboardView)
from secretary.infrastructure.team_read_repository import TeamReadRepository, _SAFE_ERRORS


_ACTIVE = ('queued', 'running', 'reconciling', 'uncertain')
_DELIVERY_STATES = ('pending', 'sending', 'retryable', 'uncertain', 'rejected', 'sent')


def _cursor(project, at, operation):
    data = canonical({'project': project, 'at': at, 'operation': operation}).encode('utf-8')
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def _decode(value, project):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', value):
            raise ValueError
        data = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
        if not isinstance(data, dict) or set(data) != {'project', 'at', 'operation'} or data['project'] != project:
            raise ValueError
        at = datetime.fromisoformat(data['at'])
        if at.tzinfo is None or uuid_string(data['operation']) != data['operation']:
            raise ValueError
        if _cursor(project, data['at'], data['operation']) != value:
            raise ValueError
        return data['at'], data['operation']
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        raise ValueError('team_dashboard_invalid') from None


class TeamDashboardRepository:
    def __init__(self, team, *, reads=None, bot=None, voices=None, notifications=None, webhook_reader=None):
        self.team, self.db = team, team.db
        self.reads = reads if reads is not None else TeamReadRepository(team)
        self.bot, self.voices = bot, voices
        self.notifications, self.webhook_reader = notifications, webhook_reader

    def _owner(self, conn, actor, project, revision):
        member = self.team._scope(conn, actor, project)
        self.team._actor_revision(member, revision)
        if member.role != 'owner':
            raise TeamForbidden('owner_required')
        return member

    def _commands(self, conn, project, limit, cursor):
        where, parameters = '', [project]
        if cursor is not None:
            where = ' AND (c.created_at>? OR (c.created_at=? AND c.operation_id>?))'
            parameters.extend((cursor[0], cursor[0], cursor[1]))
        rows = conn.execute('''SELECT c.operation_id,c.task_id,c.created_at,c.acceptance,
            json_extract(c.payload,'$.action') AS action,e.state,e.payload,e.lease_until,
            (SELECT MAX(j.created_at) FROM team_journal j WHERE j.operation_id=c.operation_id
             AND j.event IN('accepted','queued','running','reconciling','uncertain')) AS last_state_at
            FROM team_commands c JOIN team_execution e USING(operation_id)
            WHERE c.project_id=? AND e.state IN('queued','running','reconciling','uncertain')'''
            + where + ' ORDER BY c.created_at,c.operation_id LIMIT ?', (*parameters, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            acceptance = AcceptanceReceipt.model_validate_json(row['acceptance'])
            execution = ExecutionState.model_validate_json(row['payload'])
            if acceptance.operation_id != row['operation_id'] or execution.state != row['state'] or acceptance.decision != 'accepted':
                raise sqlite3.DataError('team_dashboard_storage_invalid')
            code = execution.error_code
            items.append(OwnerCommandItem(operation_id=row['operation_id'], task_id=execution.task_id or row['task_id'],
                action=row['action'], state=execution.state, accepted_at=acceptance.decided_at,
                last_state_at=datetime.fromisoformat(row['last_state_at']) if row['last_state_at'] else None,
                lease_until=datetime.fromtimestamp(row['lease_until'], timezone.utc) if row['lease_until'] is not None else None,
                error_code=code if code in _SAFE_ERRORS else 'task_operation_failed' if code else None))
        after = _cursor(project, rows[limit - 1]['created_at'], rows[limit - 1]['operation_id']) if len(rows) > limit else None
        return OwnerCommandPage(items=tuple(items), next_cursor=after)

    def _deliveries(self, conn, project):
        sources = tuple(name for name, port in (('bot', self.bot), ('voice', self.voices)) if port is not None)
        if not sources:
            return DashboardDeliverySummary(state='not_configured')
        counts, accepted_at = dict.fromkeys(_DELIVERY_STATES, 0), None
        try:
            for name, port, table, states in (('bot', self.bot, 'bot_replies', 'bot_reply_state'),
                                             ('voice', self.voices, 'voice_notices', 'voice_notice_state')):
                if port is None:
                    continue
                if getattr(getattr(port, 'db', None), 'path', None) != self.db.path:
                    raise ValueError('dashboard_source_database_invalid')
                # SQL projects only state/receipt, never outbound text or raw event.
                rows = conn.execute(f'''SELECT n.id,s.state,s.receipt FROM {table} n
                    JOIN {states} s ON s.id=n.id JOIN bot_events event ON event.id=n.event_id
                    LEFT JOIN team_commands command ON command.operation_id=json_extract(n.payload,'$.receipt.acceptance_receipt.operation_id')
                    WHERE (command.project_id=? AND command.actor_id=json_extract(event.payload,'$.actor_id'))
                    OR (json_extract(n.payload,'$.receipt') IS NULL
                        AND json_array_length(event.payload,'$.project_ids')=1
                        AND json_extract(event.payload,'$.project_ids[0]')=?)''', (project, project))
                while batch := rows.fetchmany(256):
                    for row in batch:
                        state = row['state']
                        if state not in counts:
                            raise ValueError('dashboard_delivery_state_invalid')
                        if state == 'sent':
                            receipt = SendReceipt(**json.loads(row['receipt']))
                            if receipt.operation_id != row['id'] or receipt.state != 'sent':
                                raise ValueError('dashboard_delivery_receipt_invalid')
                            timestamp = getattr(receipt, 'accepted_at', None)
                            if timestamp is not None:
                                instant = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                                accepted_at = max(accepted_at, instant) if accepted_at is not None else instant
                        counts[state] += 1
            return DashboardDeliverySummary(state='issues' if any(counts[x] for x in ('uncertain', 'retryable', 'rejected')) else 'observed',
                sources=sources, pending_count=counts['pending'], sending_count=counts['sending'],
                retryable_count=counts['retryable'], uncertain_count=counts['uncertain'], rejected_count=counts['rejected'],
                max_api_accepted_count=counts['sent'], last_max_api_accepted_at=accepted_at)
        except TeamForbidden:
            raise
        except Exception:
            return DashboardDeliverySummary(state='unavailable', sources=sources)

    def _notifications(self, actor, project, revision):
        if self.notifications is None:
            return DashboardNotificationSummary(state='not_configured')
        try:
            result = self.notifications.summary(actor, project, expected_actor_revision=revision)
            return DashboardNotificationSummary.model_validate(result)
        except TeamForbidden:
            raise
        except Exception:
            return DashboardNotificationSummary(state='unavailable')

    def _webhook(self, actor, project, revision):
        if self.webhook_reader is None:
            return DashboardWebhookStatus(state='not_configured')
        try:
            value = DashboardWebhookStatus.model_validate(self.webhook_reader(actor, project, expected_actor_revision=revision))
            if value.state not in {'unknown', 'observed', 'issues', 'not_configured'}:
                raise ValueError('dashboard_webhook_evidence_invalid')
            return value
        except TeamForbidden:
            raise
        except Exception:
            return DashboardWebhookStatus(state='unavailable')

    def dashboard(self, actor, project_id, *, limit=50, after=None, expected_actor_revision=None):
        actor = uuid_string(actor)
        if not isinstance(project_id, str) or not re.fullmatch(r'[1-9][0-9]{0,127}', project_id) or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('team_dashboard_invalid')
        cursor = _decode(after, project_id)
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            member = self._owner(conn, actor, project_id, expected_actor_revision)
            commands = self._commands(conn, project_id, limit, cursor)
            deliveries = self._deliveries(conn, project_id)
        status = self.reads.status(actor, project_id, expected_actor_revision=member.revision)
        notifications = self._notifications(actor, project_id, member.revision)
        webhook = self._webhook(actor, project_id, member.revision)
        # Every injected reader may run while permissions change; never release
        # even partial aggregates under the earlier session revision.
        with self.db.connection() as conn:
            self._owner(conn, actor, project_id, member.revision)
        if status.project_id != project_id:
            raise sqlite3.DataError('team_dashboard_storage_invalid')
        return OwnerDashboardView(project_id=project_id, status=status, commands=commands,
            deliveries=deliveries, notifications=notifications, webhook=webhook)
