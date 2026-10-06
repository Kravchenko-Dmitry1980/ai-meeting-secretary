"""Durable notification scheduling and fenced MAX outbox; no runtime or HTTP imports."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import re

from secretary.domain.bot import BotSend, SendReceipt
from secretary.domain.cloud_budget import MOSCOW, budget_period
from secretary.domain.notifications import (NotificationAuthorization, NotificationClaim, NotificationDeferred,
    NotificationError, NotificationIntent, ReminderSnapshot, ReminderTask)
from secretary.domain.team import TaskSnapshot, TeamForbidden, TeamMember, canonical, content_hash


NOTIFICATION_MIGRATION = (
    '''CREATE TABLE notification_due_state(task_id TEXT PRIMARY KEY NOT NULL REFERENCES team_projections(task_id),
        project_id TEXT NOT NULL,due_revision INTEGER NOT NULL CHECK(due_revision>=0),due_at TEXT,
        due_confirmed INTEGER NOT NULL CHECK(due_confirmed IN(0,1)),assignee_id TEXT)''',
    '''CREATE TABLE notification_due_generations(task_id TEXT NOT NULL,due_revision INTEGER NOT NULL,
        project_id TEXT NOT NULL,due_at TEXT,due_confirmed INTEGER NOT NULL,assignee_id TEXT,
        projection_revision INTEGER NOT NULL,PRIMARY KEY(task_id,due_revision))''',
    '''INSERT INTO notification_due_state SELECT task_id,project_id,0,json_extract(payload,'$.due_at'),
        json_extract(payload,'$.due_confirmed'),json_extract(payload,'$.assignee_id') FROM team_projections''',
    '''INSERT INTO notification_due_generations SELECT task_id,0,project_id,json_extract(payload,'$.due_at'),
        json_extract(payload,'$.due_confirmed'),json_extract(payload,'$.assignee_id'),revision FROM team_projections''',
    '''CREATE TABLE notification_intents(notification_id TEXT PRIMARY KEY NOT NULL,dedup_key TEXT UNIQUE NOT NULL,
        recipient_id TEXT NOT NULL,rule TEXT NOT NULL,scheduled_at TEXT NOT NULL,group_id TEXT NOT NULL,
        part_index INTEGER NOT NULL,payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)),created_at TEXT NOT NULL)''',
    '''CREATE TABLE notification_task_refs(notification_id TEXT NOT NULL REFERENCES notification_intents(notification_id),
        task_id TEXT NOT NULL,project_id TEXT NOT NULL,due_revision INTEGER NOT NULL,
        PRIMARY KEY(notification_id,task_id),FOREIGN KEY(task_id,due_revision)
        REFERENCES notification_due_generations(task_id,due_revision))''',
    '''CREATE TABLE notification_state(notification_id TEXT PRIMARY KEY NOT NULL REFERENCES notification_intents(notification_id),
        state TEXT NOT NULL CHECK(state IN('pending','sending','sent','uncertain','failed','cancelled')),
        worker_id TEXT,fence INTEGER NOT NULL DEFAULT 0 CHECK(fence>=0),lease_until REAL,
        attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3),not_before REAL NOT NULL,error_code TEXT)''',
    '''CREATE TABLE notification_attempts(notification_id TEXT NOT NULL REFERENCES notification_intents(notification_id),
        attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 3),fence INTEGER NOT NULL,send_hash TEXT NOT NULL,
        send_payload TEXT NOT NULL CHECK(json_valid(send_payload)),started_at TEXT NOT NULL,
        PRIMARY KEY(notification_id,attempt))''',
    '''CREATE TABLE notification_receipts(notification_id TEXT NOT NULL,attempt INTEGER NOT NULL,fence INTEGER NOT NULL,
        receipt_hash TEXT NOT NULL,receipt TEXT NOT NULL CHECK(json_valid(receipt)),received_at TEXT NOT NULL,
        accepted_at TEXT,PRIMARY KEY(notification_id,attempt),FOREIGN KEY(notification_id,attempt)
        REFERENCES notification_attempts(notification_id,attempt))''',
    '''CREATE TABLE notification_plan_runs(plan_hash TEXT PRIMARY KEY NOT NULL,scope_key TEXT NOT NULL,
        planned_at TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE notification_planner_state(scope_key TEXT PRIMARY KEY NOT NULL,last_planned_at TEXT NOT NULL)''',
    'CREATE INDEX notification_pending ON notification_state(state,not_before)',
    'CREATE INDEX notification_ref_generation ON notification_task_refs(task_id,due_revision)',
    *tuple(f'''CREATE TRIGGER {table}_immutable_{verb.lower()} BEFORE {verb} ON {table}
        BEGIN SELECT RAISE(ABORT,'Immutable notification evidence'); END'''
        for table in ('notification_due_generations','notification_intents','notification_task_refs',
                      'notification_attempts','notification_receipts','notification_plan_runs') for verb in ('UPDATE','DELETE')),
    *tuple(f'''CREATE TRIGGER {table}_replace_guard BEFORE INSERT ON {table}
        WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition})
        BEGIN SELECT RAISE(ABORT,'Immutable notification evidence'); END'''
        for table, condition in (
            ('notification_due_generations','task_id=NEW.task_id AND due_revision=NEW.due_revision'),
            ('notification_intents','notification_id=NEW.notification_id OR dedup_key=NEW.dedup_key'),
            ('notification_task_refs','notification_id=NEW.notification_id AND task_id=NEW.task_id'),
            ('notification_attempts','notification_id=NEW.notification_id AND attempt=NEW.attempt'),
            ('notification_receipts','notification_id=NEW.notification_id AND attempt=NEW.attempt'),
            ('notification_plan_runs','plan_hash=NEW.plan_hash'))),
    '''CREATE TRIGGER notification_due_state_binding BEFORE UPDATE ON notification_due_state
        WHEN NEW.task_id IS NOT OLD.task_id OR NEW.project_id IS NOT OLD.project_id
        OR NEW.due_revision!=OLD.due_revision+1
        OR (NEW.due_at IS OLD.due_at AND NEW.due_confirmed IS OLD.due_confirmed AND NEW.assignee_id IS OLD.assignee_id)
        OR NOT EXISTS(SELECT 1 FROM team_projections p WHERE p.task_id=NEW.task_id AND p.project_id=NEW.project_id
            AND json_extract(p.payload,'$.due_at') IS NEW.due_at
            AND json_extract(p.payload,'$.due_confirmed') IS NEW.due_confirmed
            AND json_extract(p.payload,'$.assignee_id') IS NEW.assignee_id)
        BEGIN SELECT RAISE(ABORT,'Notification due binding invalid'); END''',
    '''CREATE TRIGGER notification_due_state_no_delete BEFORE DELETE ON notification_due_state
        BEGIN SELECT RAISE(ABORT,'Notification due state cannot be deleted'); END''',
    '''CREATE TRIGGER notification_due_state_no_replace BEFORE INSERT ON notification_due_state
        WHEN EXISTS(SELECT 1 FROM notification_due_state WHERE task_id=NEW.task_id)
        BEGIN SELECT RAISE(ABORT,'Notification due state cannot be replaced'); END''',
    '''CREATE TRIGGER notification_state_guard BEFORE UPDATE ON notification_state
        WHEN NEW.notification_id IS NOT OLD.notification_id OR NEW.fence<OLD.fence OR NEW.attempt<OLD.attempt
        OR (OLD.state IN('sent','uncertain','failed','cancelled') AND NEW.state IS NOT OLD.state)
        BEGIN SELECT RAISE(ABORT,'Notification state cannot regress'); END''',
    '''CREATE TRIGGER notification_state_no_replace BEFORE INSERT ON notification_state
        WHEN EXISTS(SELECT 1 FROM notification_state WHERE notification_id=NEW.notification_id)
        BEGIN SELECT RAISE(ABORT,'Notification state cannot be replaced'); END''',
    '''CREATE TRIGGER notification_state_no_delete BEFORE DELETE ON notification_state
        BEGIN SELECT RAISE(ABORT,'Notification state cannot be deleted'); END''',
    '''CREATE TRIGGER notification_projection_insert AFTER INSERT ON team_projections BEGIN
        INSERT INTO notification_due_state VALUES(NEW.task_id,NEW.project_id,0,json_extract(NEW.payload,'$.due_at'),
            json_extract(NEW.payload,'$.due_confirmed'),json_extract(NEW.payload,'$.assignee_id'));
        INSERT INTO notification_due_generations VALUES(NEW.task_id,0,NEW.project_id,json_extract(NEW.payload,'$.due_at'),
            json_extract(NEW.payload,'$.due_confirmed'),json_extract(NEW.payload,'$.assignee_id'),NEW.revision);
        END''',
    '''CREATE TRIGGER notification_projection_due_update AFTER UPDATE OF payload ON team_projections
        WHEN json_extract(NEW.payload,'$.due_at') IS NOT json_extract(OLD.payload,'$.due_at')
          OR json_extract(NEW.payload,'$.due_confirmed') IS NOT json_extract(OLD.payload,'$.due_confirmed')
          OR json_extract(NEW.payload,'$.assignee_id') IS NOT json_extract(OLD.payload,'$.assignee_id') BEGIN
        UPDATE notification_due_state SET due_revision=due_revision+1,due_at=json_extract(NEW.payload,'$.due_at'),
            due_confirmed=json_extract(NEW.payload,'$.due_confirmed'),assignee_id=json_extract(NEW.payload,'$.assignee_id')
            WHERE task_id=NEW.task_id;
        INSERT INTO notification_due_generations SELECT task_id,due_revision,project_id,due_at,due_confirmed,assignee_id,
            NEW.revision FROM notification_due_state WHERE task_id=NEW.task_id;
        UPDATE notification_state SET state='cancelled',error_code='notification_due_changed',
            worker_id=NULL,lease_until=NULL,fence=fence+1 WHERE state='pending' AND notification_id IN(
            SELECT r.notification_id FROM notification_task_refs r JOIN notification_intents n USING(notification_id)
            WHERE r.task_id=NEW.task_id AND n.rule!='daily_digest' AND r.due_revision!=(
                SELECT due_revision FROM notification_due_state WHERE task_id=NEW.task_id));
        END''',
)

_STATES = frozenset({'pending','sending','sent','uncertain','failed','cancelled'})
_CODES = frozenset({'notification_due_changed','notification_cancelled','notification_stale',
    'notification_forbidden','notification_empty','notification_coalesced','notification_deferred',
    'notification_send_uncertain','notification_send_failed','notification_lease_expired',
    'notification_quiet_hours','notification_snapshot_stale','notification_remote_unavailable',
    'notification_remote_changed','notification_task_changed','notification_recipient_changed',
    'max_rate_limited','max_send_rejected','max_send_uncertain','max_send_unavailable'})


def _code(value):
    return value if value in _CODES else 'notification_send_failed'


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise NotificationError('notification_time_invalid')
    return value.astimezone(timezone.utc)


def _encoded(model):
    return canonical(model.model_dump(mode='json'))


def _receipt_data(receipt):
    return {key: _aware(value).isoformat() if isinstance(value, datetime) else value
            for key, value in asdict(receipt).items()}


class NotificationRepository:
    def __init__(self, team, *, clock=None):
        self.team, self.db = team, team.db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        return _aware(self.clock())

    @staticmethod
    def _projects(values):
        if (not isinstance(values, tuple) or not 1 <= len(values) <= 10 or len(set(values)) != len(values)
                or any(not isinstance(v, str) or not re.fullmatch(r'[1-9][0-9]{0,127}', v) for v in values)):
            raise NotificationError('notification_scope_invalid')
        return tuple(sorted(values))

    def due_revision(self, task_id):
        with self.db.connection() as conn:
            row = conn.execute('SELECT due_revision FROM notification_due_state WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise NotificationError('notification_task_changed')
            return row[0]

    def _snapshot(self, conn, projects, now, resumed=False, budget=None):
        scope_key = content_hash(projects)
        watermark = conn.execute('SELECT last_planned_at FROM notification_planner_state WHERE scope_key=?', (scope_key,)).fetchone()
        verified = []
        tasks = []
        for project in projects:
            sync = conn.execute('SELECT state,last_successful_sync_at FROM team_sync_state WHERE project_id=?', (project,)).fetchone()
            if not sync or sync['state'] != 'ready' or not sync['last_successful_sync_at']:
                raise NotificationDeferred('notification_snapshot_stale')
            at = _aware(datetime.fromisoformat(sync['last_successful_sync_at']))
            if not timedelta() <= now-at <= timedelta(seconds=90):
                raise NotificationDeferred('notification_snapshot_stale')
            verified.append(at)
            for row in conn.execute('''SELECT p.payload,d.due_revision FROM team_projections p
                JOIN notification_due_state d USING(task_id) WHERE p.project_id=? ORDER BY length(p.task_id),p.task_id''', (project,)):
                tasks.append(ReminderTask(snapshot=TaskSnapshot.model_validate_json(row['payload']), due_revision=row['due_revision']))
        members = tuple(member for row in conn.execute('SELECT payload FROM team_members ORDER BY id')
            if (member := TeamMember.model_validate_json(row[0])).enabled and set(member.project_ids) & set(projects))
        return ReminderSnapshot(tasks=tuple(tasks), members=members, verified_at=min(verified), project_ids=projects,
            last_planned_at=watermark[0] if watermark else None, resumed=resumed, budget=budget)

    def planning_snapshot(self, project_ids, *, now=None, resumed=False, budget=None):
        projects = self._projects(project_ids)
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            return self._snapshot(conn, projects, _aware(now) if now is not None else self._now(), resumed, budget)

    def commit_plan(self, snapshot, intents, *, planned_at):
        snapshot = ReminderSnapshot.model_validate(snapshot.model_dump())
        intents = tuple(NotificationIntent.model_validate(item.model_dump()) for item in intents)
        planned_at = _aware(planned_at)
        projects = self._projects(snapshot.project_ids)
        if len(intents) > 2000 or len({i.notification_id for i in intents}) != len(intents):
            raise NotificationError('notification_plan_invalid')
        request = {'snapshot': snapshot.model_dump(mode='json'), 'intents': [i.model_dump(mode='json') for i in intents],
                   'planned_at': planned_at.isoformat()}
        digest, scope_key = content_hash(request), content_hash(projects)
        with self.db.transaction() as conn:
            old = conn.execute('SELECT payload FROM notification_plan_runs WHERE plan_hash=?', (digest,)).fetchone()
            if old:
                return tuple(self._intent(conn, identifier) for identifier in json.loads(old[0])['notification_ids'])
            fresh = self._snapshot(conn, projects, self._now(), snapshot.resumed, snapshot.budget)
            if (fresh.tasks != snapshot.tasks or fresh.members != snapshot.members
                    or fresh.last_planned_at != snapshot.last_planned_at
                    or planned_at > self._now() or (fresh.last_planned_at and planned_at < fresh.last_planned_at)):
                raise NotificationError('notification_plan_conflict')
            refs = {(t.snapshot.project_id,t.snapshot.task_id,t.due_revision): t.snapshot for t in fresh.tasks}
            members = {m.id:m for m in fresh.members}
            stored = []
            for intent in intents:
                member = members.get(intent.recipient_id)
                if (not member or member.revision != intent.recipient_revision
                        or not set(intent.project_ids) <= set(projects) & set(member.project_ids)
                        or intent.canonical_verified_at != snapshot.verified_at):
                    raise NotificationError('notification_plan_conflict')
                if intent.rule.startswith('budget_') and member.role != 'owner':
                    raise NotificationError('notification_forbidden')
                for ref in intent.task_refs:
                    task = refs.get((ref.project_id,ref.task_id,ref.due_revision))
                    if not task or task.assignee_id != member.id or task.bucket in {'done','cancelled'}:
                        raise NotificationError('notification_plan_conflict')
                existing = conn.execute('''SELECT notification_id,dedup_key FROM notification_intents
                    WHERE notification_id=? OR dedup_key=?''', (intent.notification_id,intent.dedup_key)).fetchall()
                if existing:
                    if len(existing)!=1 or existing[0]['notification_id']!=intent.notification_id or existing[0]['dedup_key']!=intent.dedup_key:
                        raise NotificationError('notification_identity_conflict')
                    prior = self._intent(conn, intent.notification_id)
                    identity = ('recipient_id','rule','scheduled_at','group_id','part_index','budget_period','budget_threshold')
                    if any(getattr(prior,key)!=getattr(intent,key) for key in identity):
                        raise NotificationError('notification_identity_conflict')
                    stored.append(prior)  # Stable event; never replace an earlier plan/body.
                    continue
                payload = intent.model_dump(mode='json')
                conn.execute('INSERT INTO notification_intents VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (intent.notification_id,intent.dedup_key,intent.recipient_id,intent.rule,intent.scheduled_at.isoformat(),
                     intent.group_id,intent.part_index,content_hash(payload),canonical(payload),self._now().isoformat()))
                conn.execute("INSERT INTO notification_state(notification_id,state,not_before) VALUES(?,'pending',?)",
                             (intent.notification_id,intent.scheduled_at.timestamp()))
                conn.executemany('INSERT INTO notification_task_refs VALUES(?,?,?,?)',
                    ((intent.notification_id,r.task_id,r.project_id,r.due_revision) for r in intent.task_refs))
                stored.append(intent)
            coalesce = (snapshot.resumed or snapshot.last_planned_at is None
                or planned_at-snapshot.last_planned_at>timedelta(seconds=90)
                or any(item.rule == 'daily_digest' for item in stored))
            if coalesce:
                for row in conn.execute("SELECT i.notification_id,i.payload FROM notification_intents i JOIN notification_state s USING(notification_id) WHERE s.state='pending'").fetchall():
                    prior = NotificationIntent.model_validate_json(row['payload'])
                    if set(prior.project_ids) <= set(projects) and not prior.rule.startswith('budget_'):
                        if prior.rule != 'daily_digest' or prior.scheduled_at.astimezone(MOSCOW).date() < planned_at.astimezone(MOSCOW).date():
                            self._cancel(conn, prior.notification_id, 'notification_coalesced')
            conn.execute('INSERT INTO notification_plan_runs VALUES(?,?,?,?)',
                (digest,scope_key,planned_at.isoformat(),canonical({'notification_ids':[i.notification_id for i in stored]})))
            conn.execute('''INSERT INTO notification_planner_state VALUES(?,?) ON CONFLICT(scope_key)
                DO UPDATE SET last_planned_at=excluded.last_planned_at''', (scope_key,planned_at.isoformat()))
            return tuple(stored)

    @staticmethod
    def _intent(conn, identifier):
        row = conn.execute('SELECT payload,payload_hash FROM notification_intents WHERE notification_id=?', (identifier,)).fetchone()
        if not row or content_hash(json.loads(row['payload'])) != row['payload_hash']:
            raise NotificationError('notification_storage_corrupt')
        return NotificationIntent.model_validate_json(row['payload'])

    def _claim_value(self, conn, row):
        return NotificationClaim(id=row['notification_id'],intent=self._intent(conn,row['notification_id']),
            worker_id=row['worker_id'],fence=row['fence'],lease_until=datetime.fromtimestamp(row['lease_until'],timezone.utc),
            attempt=row['attempt'],state='sending' if row['state']=='sending' else 'preparing')

    @staticmethod
    def _lease(worker_id, seconds):
        if not isinstance(worker_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',worker_id) or type(seconds) is not int or not 1<=seconds<=3600:
            raise NotificationError('notification_claim_invalid')

    def _recover(self, conn):
        conn.execute('''UPDATE notification_state SET state=CASE WHEN state='sending' THEN 'uncertain' ELSE 'pending' END,
            error_code=CASE WHEN state='sending' THEN 'notification_lease_expired' ELSE error_code END,
            worker_id=NULL,lease_until=NULL,fence=fence+1 WHERE state IN('pending','sending') AND lease_until<=?''',
            (self._now().timestamp(),))

    def recover_expired(self):
        with self.db.transaction() as conn:
            self._recover(conn)

    def claim(self, worker_id, lease_seconds=60):
        self._lease(worker_id,lease_seconds)
        with self.db.transaction() as conn:
            self._recover(conn)
            row=conn.execute('''SELECT s.* FROM notification_state s JOIN notification_intents i USING(notification_id)
                WHERE s.state='pending' AND s.worker_id IS NULL AND s.not_before<=?
                ORDER BY s.not_before,i.created_at,i.notification_id LIMIT 1''',(self._now().timestamp(),)).fetchone()
            if not row:
                return None
            conn.execute('UPDATE notification_state SET worker_id=?,fence=fence+1,lease_until=? WHERE notification_id=?',
                (worker_id,(self._now()+timedelta(seconds=lease_seconds)).timestamp(),row['notification_id']))
            return self._claim_value(conn,conn.execute('SELECT * FROM notification_state WHERE notification_id=?',(row['notification_id'],)).fetchone())

    def _claimed(self, conn, claim, *, sending=False):
        if not isinstance(claim,NotificationClaim):
            raise NotificationError('notification_claim_invalid')
        row=conn.execute('SELECT * FROM notification_state WHERE notification_id=?',(claim.id,)).fetchone()
        if (not row or row['state'] not in {'pending','sending'} or row['worker_id']!=claim.worker_id
                or row['fence']!=claim.fence or row['lease_until'] is None or row['lease_until']<=self._now().timestamp()):
            raise NotificationError('notification_claim_lost')
        actual=self._claim_value(conn,row)
        if actual.intent!=claim.intent or (sending and (actual.state!='sending' or actual.attempt!=claim.attempt)):
            raise NotificationError('notification_claim_lost')
        return actual

    def _authorize(self, conn, claim):
        actual=self._claimed(conn,claim)
        intent=actual.intent
        try:
            member=self.team._member(conn,intent.recipient_id)
        except TeamForbidden:
            raise NotificationError('notification_forbidden') from None
        if member.revision!=intent.recipient_revision or not set(intent.project_ids)<=set(member.project_ids):
            raise NotificationError('notification_recipient_changed')
        if intent.rule.startswith('budget_'):
            if member.role!='owner' or intent.budget_period!=budget_period(self._now()):
                raise NotificationError('notification_stale')
            return NotificationAuthorization(claim=actual,recipient=member,tasks=())
        tasks=[]
        for ref in intent.task_refs:
            task=self.team._projection(conn,ref.task_id)
            generation=conn.execute('SELECT due_revision FROM notification_due_state WHERE task_id=?',(ref.task_id,)).fetchone()
            valid=(task is not None and task.project_id==ref.project_id and ref.project_id in member.project_ids
                and task.assignee_id==member.id and task.bucket not in {'done','cancelled'} and generation is not None
                and generation[0]==ref.due_revision)
            if valid and intent.rule!='daily_digest':
                valid=task.due_confirmed and task.due_at is not None
                if valid and intent.rule in {'due_24h','due_1h'}:
                    valid=task.due_at>self._now()
                if valid and intent.rule=='overdue':
                    valid=task.due_at<=self._now()
            if not valid:
                if intent.rule!='daily_digest':
                    raise NotificationError('notification_task_changed')
                continue
            if conn.execute('SELECT 1 FROM team_resources WHERE resource_key=? AND operation_id IS NOT NULL',('task:'+task.task_id,)).fetchone():
                raise NotificationDeferred('notification_snapshot_stale')
            tasks.append(task)
        if not tasks:
            raise NotificationError('notification_empty')
        return NotificationAuthorization(claim=actual,recipient=member,tasks=tuple(tasks))

    def authorize(self, claim):
        with self.db.transaction() as conn:
            return self._authorize(conn,claim)

    def renew(self, claim, lease_seconds=60):
        self._lease(claim.worker_id,lease_seconds)
        with self.db.transaction() as conn:
            actual=self._claimed(conn,claim)
            until=max(actual.lease_until,self._now()+timedelta(seconds=lease_seconds))
            conn.execute('UPDATE notification_state SET lease_until=? WHERE notification_id=?',(until.timestamp(),claim.id))
            return actual.model_copy(update={'lease_until':until})

    def mark_sending(self, claim, send, *, observed_fingerprints):
        if not isinstance(send,BotSend) or len(send.text)>3500 or send.sensitive or send.operation_id!=claim.id:
            raise NotificationError('notification_send_invalid')
        send=BotSend(**asdict(send)) if not send.buttons else send
        from secretary.application.reminders import quiet,next_morning
        with self.db.transaction() as conn:
            auth=self._authorize(conn,claim)
            actual=auth.claim
            if actual.state!='preparing' or actual.attempt>=3:
                raise NotificationError('notification_claim_lost')
            if quiet(self._now()):
                raise NotificationDeferred('notification_quiet_hours',next_morning(self._now()))
            expected=tuple((task.task_id,task.remote_fingerprint) for task in auth.tasks)
            if not isinstance(observed_fingerprints,tuple) or sorted(observed_fingerprints)!=sorted(expected) or send.user_id!=auth.recipient.max_user_id:
                raise NotificationError('notification_task_changed')
            payload=asdict(send)
            attempt=actual.attempt+1
            conn.execute('INSERT INTO notification_attempts VALUES(?,?,?,?,?,?)',
                (claim.id,attempt,claim.fence,content_hash(payload),canonical(payload),self._now().isoformat()))
            conn.execute("UPDATE notification_state SET state='sending',attempt=?,error_code=NULL WHERE notification_id=?",(attempt,claim.id))
            return actual.model_copy(update={'state':'sending','attempt':attempt})

    def finish(self, claim, receipt):
        if not isinstance(receipt,SendReceipt) or receipt.operation_id!=claim.id:
            raise NotificationError('notification_receipt_invalid')
        receipt=SendReceipt(**asdict(receipt))
        data=_receipt_data(receipt)
        if data.get('error_code') is not None:
            data['error_code']=_code(data['error_code'])
        digest=content_hash(data)
        with self.db.transaction() as conn:
            actual=self._claimed(conn,claim,sending=True)
            state={'sent':'sent','rejected':'failed','uncertain':'uncertain','retryable':'uncertain'}[receipt.state]
            after=self._now().timestamp()
            if receipt.state=='retryable' and receipt.error_code=='max_rate_limited':
                state='pending' if actual.attempt<3 else 'failed'
                after+=receipt.retry_after if receipt.retry_after is not None else 1
            conn.execute('INSERT INTO notification_receipts VALUES(?,?,?,?,?,?,?)',
                (claim.id,actual.attempt,actual.fence,digest,canonical(data),self._now().isoformat(),data.get('accepted_at')))
            conn.execute('''UPDATE notification_state SET state=?,not_before=?,error_code=?,worker_id=NULL,lease_until=NULL
                WHERE notification_id=?''',(state,after,data.get('error_code'),claim.id))
            return self._read(conn,claim.id)

    @staticmethod
    def _cancel(conn, identifier, error_code):
        conn.execute('''UPDATE notification_state SET state='cancelled',error_code=?,worker_id=NULL,
            lease_until=NULL,fence=fence+1 WHERE notification_id=? AND state='pending' ''',(_code(error_code),identifier))

    def cancel(self, claim, error_code='notification_cancelled'):
        with self.db.transaction() as conn:
            actual=self._claimed(conn,claim)
            if actual.state!='preparing':
                raise NotificationError('notification_claim_lost')
            self._cancel(conn,claim.id,error_code)

    def defer(self, claim, *, available_at, error_code='notification_deferred'):
        available_at=_aware(available_at)
        with self.db.transaction() as conn:
            actual=self._claimed(conn,claim)
            if actual.state!='preparing' or available_at<self._now():
                raise NotificationError('notification_claim_lost')
            conn.execute('''UPDATE notification_state SET worker_id=NULL,lease_until=NULL,fence=fence+1,
                not_before=?,error_code=? WHERE notification_id=?''',(available_at.timestamp(),_code(error_code),claim.id))

    def _read(self, conn, identifier):
        intent=self._intent(conn,identifier)
        row=conn.execute('SELECT * FROM notification_state WHERE notification_id=?',(identifier,)).fetchone()
        receipt=conn.execute('SELECT receipt FROM notification_receipts WHERE notification_id=? ORDER BY attempt DESC LIMIT 1',(identifier,)).fetchone()
        return {'intent':intent,'state':row['state'],'attempt':row['attempt'],'error_code':row['error_code'],
                'receipt':json.loads(receipt[0]) if receipt else None}

    def read(self, notification_id):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            return self._read(conn,notification_id)

    def summary(self, actor, project_id, *, expected_actor_revision=None):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            owner=self.team._scope(conn,actor,project_id)
            self.team._actor_revision(owner,expected_actor_revision)
            if owner.role!='owner':
                raise TeamForbidden('owner_required')
            scope = '''(EXISTS(SELECT 1 FROM notification_task_refs ref
                WHERE ref.notification_id=i.notification_id AND ref.project_id=?) OR
                (i.rule LIKE 'budget_%' AND EXISTS(SELECT 1 FROM json_each(i.payload,'$.project_ids') WHERE value=?)))'''
            rows=conn.execute(f'''SELECT s.state,COUNT(*) AS count FROM notification_state s
                JOIN notification_intents i USING(notification_id) WHERE {scope} GROUP BY s.state''',(project_id,project_id)).fetchall()
            counts={row['state']:row['count'] for row in rows}
            receipt=conn.execute(f'''SELECT r.accepted_at
                FROM notification_receipts r JOIN notification_intents i USING(notification_id)
                WHERE json_extract(r.receipt,'$.state')='sent' AND r.accepted_at IS NOT NULL AND {scope}''',(project_id,project_id))
            accepted = None
            while rows := receipt.fetchmany(256):
                for row in rows:
                    instant = _aware(datetime.fromisoformat(row['accepted_at']))
                    accepted = instant if accepted is None else max(accepted, instant)
            return {'state':'issues' if counts.get('uncertain',0) or counts.get('failed',0) else 'observed',
                **{key+'_count':counts.get(key,0) for key in ('pending','sending','uncertain','failed','cancelled')},
                'last_max_api_accepted_at':accepted,
                'human_read_confirmed':None}
