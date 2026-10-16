"""Fenced, read-only-provider polling evidence in the independent Team database."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import uuid4

from secretary.domain.team import TaskSnapshot, TeamConflict, TeamMember, canonical, content_hash


SYNC_MIGRATION = (
    '''CREATE TABLE team_sync_runs(id TEXT PRIMARY KEY NOT NULL,project_id TEXT NOT NULL,
        started_at TEXT NOT NULL,worker_id TEXT NOT NULL,fence INTEGER NOT NULL CHECK(fence>0),
        payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE team_sync_results(run_id TEXT PRIMARY KEY NOT NULL REFERENCES team_sync_runs(id),
        finished_at TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN('ready','degraded')),
        request_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE team_sync_observations(id TEXT PRIMARY KEY NOT NULL,
        run_id TEXT NOT NULL REFERENCES team_sync_runs(id),task_id TEXT NOT NULL,project_id TEXT NOT NULL,
        recorded_at TEXT NOT NULL,event TEXT NOT NULL,changed_fields TEXT NOT NULL CHECK(json_valid(changed_fields)),
        before_fingerprint TEXT,after_fingerprint TEXT,error_code TEXT,UNIQUE(run_id,task_id))''',
    '''CREATE TABLE team_sync_state(project_id TEXT PRIMARY KEY NOT NULL,run_id TEXT NOT NULL REFERENCES team_sync_runs(id),
        state TEXT NOT NULL CHECK(state IN('syncing','ready','degraded')),worker_id TEXT,
        fence INTEGER NOT NULL CHECK(fence>0),lease_until REAL,last_attempt_at TEXT NOT NULL,
        last_successful_sync_at TEXT,error_code TEXT,issue_count INTEGER NOT NULL DEFAULT 0 CHECK(issue_count>=0))''',
    'CREATE INDEX team_sync_history ON team_sync_observations(project_id,task_id,recorded_at,id)',
    *tuple(f'''CREATE TRIGGER {table}_immutable_{verb.lower()} BEFORE {verb} ON {table}
        BEGIN SELECT RAISE(ABORT,'Immutable sync evidence'); END'''
        for table in ('team_sync_runs', 'team_sync_results', 'team_sync_observations') for verb in ('UPDATE', 'DELETE')),
    *tuple(f'''CREATE TRIGGER {table}_replace_guard BEFORE INSERT ON {table}
        WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition})
        BEGIN SELECT RAISE(ABORT,'Immutable sync evidence'); END'''
        for table, condition in (('team_sync_runs', 'id=NEW.id'), ('team_sync_results', 'run_id=NEW.run_id'),
            ('team_sync_observations', 'id=NEW.id OR (run_id=NEW.run_id AND task_id=NEW.task_id)'))),
    '''CREATE TRIGGER team_sync_state_binding BEFORE UPDATE ON team_sync_state
        WHEN NEW.project_id IS NOT OLD.project_id OR NEW.fence<OLD.fence
        BEGIN SELECT RAISE(ABORT,'Sync fence cannot regress'); END''',
    '''CREATE TRIGGER team_sync_state_replace_guard BEFORE INSERT ON team_sync_state
        WHEN EXISTS(SELECT 1 FROM team_sync_state WHERE project_id=NEW.project_id)
        BEGIN SELECT RAISE(ABORT,'Sync state cannot be replaced'); END''',
    '''CREATE TRIGGER team_sync_state_no_delete BEFORE DELETE ON team_sync_state
        BEGIN SELECT RAISE(ABORT,'Sync state cannot be deleted'); END''',
)

_DECIMAL = re.compile(r'[1-9][0-9]{0,127}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_FIELDS = frozenset({'title', 'description', 'assignee_id', 'bucket', 'important', 'urgent',
                    'classification_confirmed', 'due_at', 'due_confirmed', 'remote_fingerprint'})
_ERRORS = frozenset({'sync_read_failed', 'sync_scan_incomplete', 'sync_task_busy', 'sync_task_missing',
    'sync_projection_changed', 'sync_mapping_changed', 'sync_lease_expired', 'sync_scope_invalid',
    'remote_due_unconfirmed', 'unknown_remote_assignee', 'multiple_assignees_not_supported',
    'repeating_task_not_supported', 'inconsistent_remote_task_state', 'task_bucket_mapping_mismatch',
    'sync_remote_unsupported'})


def _identifier(value):
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise TeamConflict('sync_scope_invalid')
    return value


def _error(value):
    return value if value in _ERRORS else 'sync_read_failed'


@dataclass(frozen=True)
class SyncObservation:
    task_id: str
    after_fingerprint: str | None
    changed_fields: tuple[str, ...] = ()
    error_code: str | None = None

    def __post_init__(self):
        _identifier(self.task_id)
        if self.after_fingerprint is not None and (not isinstance(self.after_fingerprint, str)
                or not _DIGEST.fullmatch(self.after_fingerprint)):
            raise TeamConflict('sync_observation_invalid')
        if (not isinstance(self.changed_fields, tuple) or len(set(self.changed_fields)) != len(self.changed_fields)
                or not set(self.changed_fields) <= _FIELDS):
            raise TeamConflict('sync_observation_invalid')
        if self.error_code is not None and self.error_code not in _ERRORS:
            raise TeamConflict('sync_observation_invalid')


@dataclass(frozen=True)
class SyncClaim:
    run_id: str
    project_id: str
    worker_id: str
    fence: int
    lease_until: datetime
    projections: tuple[TaskSnapshot, ...]
    members: tuple[TeamMember, ...]
    held_tasks: tuple[str | None, ...] = ()


def _capture(projections, members, held_tasks):
    return {'projections': [item.model_dump(mode='json') for item in projections],
            'members': [item.model_dump(mode='json') for item in members], 'held_tasks': list(held_tasks)}


class TeamSyncRepository:
    def __init__(self, team, clock=None):
        self.team, self.db = team, team.db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('sync_clock_invalid')
        return value.astimezone(timezone.utc)

    @staticmethod
    def _scope(conn, project):
        projections = tuple(TaskSnapshot.model_validate_json(row['payload']) for row in conn.execute(
            'SELECT payload FROM team_projections WHERE project_id=? ORDER BY length(task_id),task_id', (project,)))
        members = tuple(member for row in conn.execute('SELECT payload FROM team_members ORDER BY id')
            if (member := TeamMember.model_validate_json(row['payload'])).enabled and project in member.project_ids)
        held = tuple(row['task_id'] for row in conn.execute('''SELECT c.task_id FROM team_resources r
            JOIN team_commands c ON c.operation_id=r.operation_id WHERE c.project_id=? ORDER BY r.resource_key''', (project,)))
        return projections, members, held

    def _status(self, conn, project):
        from secretary.domain.team_reads import SyncStatus
        row = conn.execute('SELECT * FROM team_sync_state WHERE project_id=?', (project,)).fetchone()
        verified = conn.execute('''SELECT MAX(json_extract(e.payload,'$.verified_at')) FROM team_execution e
            JOIN team_commands c USING(operation_id) WHERE c.project_id=? AND e.state='applied' ''', (project,)).fetchone()[0]
        if row is None:
            return SyncStatus(state='never_synced', last_command_verified_at=verified)
        expired = row['state'] == 'syncing' and row['lease_until'] <= self._now().timestamp()
        return SyncStatus(state='degraded' if expired else row['state'], last_attempt_at=row['last_attempt_at'],
            last_successful_sync_at=row['last_successful_sync_at'], last_command_verified_at=verified,
            error_code='sync_lease_expired' if expired else row['error_code'], issue_count=1 if expired else row['issue_count'])

    def status(self, actor, project_id, *, expected_actor_revision=None):
        project_id = _identifier(project_id)
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            member = self.team._scope(conn, actor, project_id)
            self.team._actor_revision(member, expected_actor_revision)
            return self._status(conn, project_id)

    def begin(self, project_id, worker_id='team-sync', lease_seconds=120):
        project_id = _identifier(project_id)
        if not isinstance(worker_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', worker_id):
            raise ValueError('sync_worker_invalid')
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('sync_lease_invalid')
        with self.db.transaction() as conn:
            now = self._now()
            state = conn.execute('SELECT * FROM team_sync_state WHERE project_id=?', (project_id,)).fetchone()
            if state and state['state'] == 'syncing':
                if state['lease_until'] > now.timestamp():
                    raise TeamConflict('sync_busy')
                self._finish(conn, state['run_id'], project_id, 'sync_lease_expired', 1, 'expired')
            projections, members, held = self._scope(conn, project_id)
            payload = canonical(_capture(projections, members, held))
            if len(projections) > 50_000 or len(payload.encode('utf-8')) > 32 * 1024 * 1024:
                raise TeamConflict('sync_scope_too_large')
            identifier, fence = str(uuid4()), (state['fence'] + 1 if state else 1)
            until = now + timedelta(seconds=lease_seconds)
            conn.execute('INSERT INTO team_sync_runs VALUES(?,?,?,?,?,?,?)',
                (identifier, project_id, now.isoformat(), worker_id, fence, content_hash(json.loads(payload)), payload))
            if state:
                conn.execute('''UPDATE team_sync_state SET run_id=?,state='syncing',worker_id=?,fence=?,lease_until=?,
                    last_attempt_at=?,error_code=NULL,issue_count=0 WHERE project_id=?''',
                    (identifier, worker_id, fence, until.timestamp(), now.isoformat(), project_id))
            else:
                conn.execute('''INSERT INTO team_sync_state(project_id,run_id,state,worker_id,fence,lease_until,last_attempt_at)
                    VALUES(?,?,'syncing',?,?,?,?)''', (project_id, identifier, worker_id, fence, until.timestamp(), now.isoformat()))
            return SyncClaim(identifier, project_id, worker_id, fence, until, projections, members, held)

    def _claim(self, conn, claim, *, replay=False):
        if not isinstance(claim, SyncClaim) or type(claim.fence) is not int:
            raise TeamConflict('sync_claim_lost')
        run = conn.execute('SELECT * FROM team_sync_runs WHERE id=?', (claim.run_id,)).fetchone()
        if (not run or run['project_id'] != claim.project_id or run['worker_id'] != claim.worker_id
                or run['fence'] != claim.fence or content_hash(json.loads(run['payload'])) != run['payload_hash']
                or canonical(_capture(claim.projections, claim.members, claim.held_tasks)) != run['payload']):
            raise TeamConflict('sync_claim_lost')
        finished = conn.execute('SELECT * FROM team_sync_results WHERE run_id=?', (claim.run_id,)).fetchone()
        if finished and finished['request_hash'] == 'expired':
            raise TeamConflict('sync_claim_lost')
        if finished and replay:
            return finished
        state = conn.execute('SELECT * FROM team_sync_state WHERE project_id=?', (claim.project_id,)).fetchone()
        if (finished or not state or state['run_id'] != claim.run_id or state['state'] != 'syncing'
                or state['worker_id'] != claim.worker_id or state['fence'] != claim.fence
                or state['lease_until'] <= self._now().timestamp()):
            raise TeamConflict('sync_claim_lost')
        return None

    def renew(self, claim, lease_seconds=120):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('sync_lease_invalid')
        with self.db.transaction() as conn:
            self._claim(conn, claim)
            row = conn.execute('SELECT lease_until FROM team_sync_state WHERE project_id=?', (claim.project_id,)).fetchone()
            until = max(datetime.fromtimestamp(row['lease_until'], timezone.utc), self._now() + timedelta(seconds=lease_seconds))
            conn.execute('UPDATE team_sync_state SET lease_until=? WHERE project_id=?', (until.timestamp(), claim.project_id))
            return replace(claim, lease_until=until)

    def _finish(self, conn, run_id, project, error, issue_count, request_hash):
        now = self._now().isoformat()
        state = 'degraded' if error or issue_count else 'ready'
        conn.execute('''UPDATE team_sync_state SET state=?,worker_id=NULL,lease_until=NULL,error_code=?,issue_count=?,
            last_successful_sync_at=CASE WHEN ?='ready' THEN ? ELSE last_successful_sync_at END WHERE project_id=?''',
            (state, error, issue_count, state, now, project))
        result = self._status(conn, project)
        conn.execute('INSERT INTO team_sync_results VALUES(?,?,?,?,?)',
            (run_id, now, state, request_hash, result.model_dump_json()))
        return result

    def fail(self, claim, error_code):
        return self.commit(claim, (), (), (), error_code=_error(error_code))

    def commit(self, claim, observations, snapshots, seen_ids, error_code=None):
        from secretary.domain.team_reads import SyncStatus
        if not all(isinstance(value, tuple) for value in (observations, snapshots, seen_ids)):
            raise TeamConflict('sync_commit_invalid')
        observations = tuple(SyncObservation(**asdict(item)) for item in observations)
        snapshots = tuple(TaskSnapshot.model_validate(item.model_dump()) for item in snapshots)
        seen_ids = tuple(_identifier(item) for item in seen_ids)
        if (len(seen_ids) > 50_000 or len(set(seen_ids)) != len(seen_ids)
                or len({item.task_id for item in observations}) != len(observations)
                or len({item.task_id for item in snapshots}) != len(snapshots)
                or any(item.task_id not in seen_ids for item in observations)
                or any(item.project_id != claim.project_id or item.task_id not in seen_ids for item in snapshots)):
            raise TeamConflict('sync_commit_invalid')
        error_code = _error(error_code) if error_code is not None else None
        request_hash = content_hash({'observations': [asdict(item) for item in observations],
            'snapshots': [item.model_dump(mode='json') for item in snapshots], 'seen_ids': seen_ids, 'error_code': error_code})
        with self.db.transaction() as conn:
            replay = self._claim(conn, claim, replay=True)
            if replay:
                if replay['request_hash'] != request_hash:
                    raise TeamConflict('sync_result_conflict')
                return SyncStatus.model_validate_json(replay['payload'])
            current, members, held = self._scope(conn, claim.project_id)
            old = {item.task_id: item for item in claim.projections}
            evidence = {item.task_id: item for item in observations}
            observed = set(evidence)
            projected = {item.task_id: item for item in snapshots}
            issues = [error_code] if error_code else []
            if canonical(_capture(current, (), ())) != canonical(_capture(claim.projections, (), ())):
                issues.append('sync_projection_changed')
            if members != claim.members:
                issues.append('sync_mapping_changed')
            for identifier, snapshot in projected.items():
                previous = old.get(identifier)
                if (previous and previous.remote_fingerprint == snapshot.remote_fingerprint
                        and previous.assignee_id != snapshot.assignee_id):
                    # A directory remap completed before begin can change local
                    # identity without changing any provider bytes. Never mark
                    # the stale own-task assignment as successfully synced.
                    evidence[identifier] = SyncObservation(identifier, snapshot.remote_fingerprint,
                        ('assignee_id',), 'sync_mapping_changed')
            busy = set(held) | set(claim.held_tasks)
            if busy:
                issues.insert(0, 'sync_task_busy')
                for identifier in busy - {None}:
                    prior = evidence.get(identifier)
                    evidence[identifier] = SyncObservation(identifier, prior.after_fingerprint if prior else None,
                        prior.changed_fields if prior else (), 'sync_task_busy')
            if not error_code:
                for identifier in set(old) - set(seen_ids):
                    evidence[identifier] = SyncObservation(identifier, None, (), 'sync_task_missing')
                if observed != set(seen_ids):
                    issues.append('sync_scan_incomplete')
                for identifier in seen_ids:
                    item = evidence.get(identifier)
                    snapshot = projected.get(identifier)
                    if item and not item.error_code and (snapshot is None or snapshot.remote_fingerprint != item.after_fingerprint):
                        raise TeamConflict('sync_commit_invalid')
            issues.extend(item.error_code for item in evidence.values() if item.error_code)
            # The entire scope is compared before any projection write. Polls
            # never replace the baseline of a live or uncertain remote command.
            if not issues:
                for identifier, snapshot in projected.items():
                    previous = old.get(identifier)
                    if previous and previous.remote_fingerprint == snapshot.remote_fingerprint:
                        continue
                    self.team._save_projection(conn, snapshot, previous.revision if previous else None)
            recorded = self._now().isoformat()
            for identifier, item in evidence.items():
                previous = old.get(identifier)
                before = previous.remote_fingerprint if previous else None
                if previous and before == item.after_fingerprint and not item.error_code:
                    continue  # The immutable run/result proves unchanged polling.
                event = ('remote_task_missing' if item.error_code == 'sync_task_missing'
                    else 'remote_observation_rejected' if item.error_code
                    else 'remote_change_observed' if before is not None and before != item.after_fingerprint
                    else 'remote_task_observed')
                conn.execute('INSERT INTO team_sync_observations VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (str(uuid4()), claim.run_id, identifier, claim.project_id, recorded, event,
                     canonical(item.changed_fields), before, item.after_fingerprint, item.error_code))
            unique_issues = tuple(dict.fromkeys(issues))
            return self._finish(conn, claim.run_id, claim.project_id,
                unique_issues[0] if unique_issues else None, max(len(unique_issues), sum(bool(item.error_code) for item in evidence.values())), request_hash)
