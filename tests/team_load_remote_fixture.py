"""Owned, offline T13 children sharing one durable simulated provider.

This fixture intentionally supplies no provider idempotency or compare-and-set.
Every attempted request and actual side effect is recorded independently.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from threading import Barrier
import time
from uuid import uuid4

import httpx

from secretary.application.team_sync import TeamSyncService
from secretary.application.team_tasks import TeamTaskService
from secretary.domain.team import TaskCommand, TeamForbidden, TeamMember
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository
from secretary.infrastructure.team_sync_repository import TeamSyncRepository
from secretary.infrastructure.vikunja import MappingMemberDirectory, ProjectBinding, VikunjaClient
from secretary.orchestration.team_worker import TeamWorker


NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
STATES = ('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled')


def binding(project):
    base = 100 if project == '7' else 200
    return ProjectBinding(project, '8' if project == '7' else '18', '19',
        dict(zip(STATES, map(str, range(base + 1, base + 8)))),
        '21' if project == '7' else '31', '22' if project == '7' else '32',
        '23' if project == '7' else '33')


@contextmanager
def connect(directory):
    conn = sqlite3.connect(Path(directory) / 'remote.sqlite3', timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=15000')
    try:
        yield conn
    finally:
        conn.close()


def initialize(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    repo = TeamRepository(TeamDatabase(directory / 'team.sqlite3'), clock=lambda: NOW)
    members = []
    for project, offset in (('7', 0), ('9', 5)):
        for number in range(5):
            member = TeamMember(id=str(uuid4()), display_name=f'Synthetic user {offset + number}',
                role='owner' if number == 0 else 'member', max_user_id=str(1001 + offset + number),
                vikunja_user_id=str(200 + offset + number), project_ids=(project,))
            repo.upsert_member(member, expected_revision=None)
            members.append(member)
    (directory / 'members.json').write_text(json.dumps([x.model_dump(mode='json') for x in members]), encoding='utf-8')
    with connect(directory) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
          CREATE TABLE tasks(id INTEGER PRIMARY KEY,project INTEGER NOT NULL,payload TEXT NOT NULL);
          CREATE TABLE comments(id INTEGER PRIMARY KEY AUTOINCREMENT,task INTEGER NOT NULL,payload TEXT NOT NULL);
          CREATE TABLE attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,pid INTEGER NOT NULL,
            method TEXT NOT NULL,path TEXT NOT NULL,started_ns INTEGER NOT NULL,finished_ns INTEGER,
            outcome TEXT,task INTEGER,logical TEXT,page INTEGER);
          CREATE TABLE effects(id INTEGER PRIMARY KEY AUTOINCREMENT,attempt INTEGER NOT NULL,
            task INTEGER NOT NULL,kind TEXT NOT NULL,logical TEXT NOT NULL);
          CREATE TABLE faults(task INTEGER PRIMARY KEY,mode TEXT NOT NULL);
        ''')
        rows = []
        for project, start, offset in (('7', 1001, 0), ('9', 1501, 5)):
            b = binding(project)
            for n in range(500):
                value = dict(id=start + n, project_id=int(project), title=f'Synthetic task {start+n}',
                    description='<p>Preserved synthetic HTML</p>', done=False,
                    due_date='0001-01-01T00:00:00Z', assignees=[{'id': int(members[offset+n%5].vikunja_user_id)}],
                    labels=[], buckets=[{'id': int(b.bucket_ids['accepted']), 'project_view_id': int(b.manual_view_id)}],
                    repeat_after=0, repeat_mode=0)
                rows.append((value['id'], int(project), json.dumps(value)))
        conn.executemany('INSERT INTO tasks VALUES(?,?,?)', rows)
        conn.executemany('INSERT INTO faults VALUES(?,?)', ((1001, '429'), (1002, '503'), (1003, 'timeout')))
    return repo, tuple(members)


class DurableRemote:
    def __init__(self, directory, *, rendezvous_index=None, rendezvous_timeout=30):
        self.directory = Path(directory)
        if rendezvous_index is not None and (type(rendezvous_index) is not int or rendezvous_index not in (0, 1)):
            raise ValueError('rendezvous_index_invalid')
        if (type(rendezvous_timeout) not in (int, float)
                or not 0 < rendezvous_timeout <= 30):
            raise ValueError('rendezvous_timeout_invalid')
        self.rendezvous_index, self.rendezvous_timeout = rendezvous_index, rendezvous_timeout
        self._first_mutation_joined = False

    def _join_first_mutation(self, attempt, started):
        if self.rendezvous_index is None or self._first_mutation_joined:
            return
        config = json.loads((self.directory / 'mutation-rendezvous.json').read_text('utf-8'))
        pids = config.get('pids')
        if (set(config) != {'schema', 'run_id', 'pids'} or type(config['schema']) is not int
                or config['schema'] != 1 or not isinstance(pids, list) or len(pids) != 2
                or any(type(pid) is not int or pid <= 0 for pid in pids) or len(set(pids)) != 2
                or pids[self.rendezvous_index] != os.getpid()):
            raise ValueError('rendezvous_participants_invalid')
        run_id = config['run_id']
        if (not isinstance(run_id, str) or len(run_id) != 32
                or any(character not in '0123456789abcdef' for character in run_id)):
            raise ValueError('rendezvous_run_invalid')
        own = self.directory / f'first-mutation-{run_id}-{os.getpid()}.json'
        value = dict(run_id=run_id, pid=os.getpid(), attempt_id=attempt, started_ns=started)
        # Unique run/PID names prevent old or unrelated markers from releasing us.
        temporary = own.with_suffix('.tmp')
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream)
        try:
            # Publish a complete file atomically; link refuses an existing marker.
            os.link(temporary, own)
        finally:
            temporary.unlink()
        peer_pid = pids[1 - self.rendezvous_index]
        peer = self.directory / f'first-mutation-{run_id}-{peer_pid}.json'
        wait_file(peer, timeout=self.rendezvous_timeout)
        # Marker creation follows a committed actual attempt and precedes effect.
        # The peer may have finished by now; don't require it to remain unfinished.
        observation = json.loads(peer.read_text('utf-8'))
        if (set(observation) != set(value) or observation['run_id'] != run_id
                or type(observation['pid']) is not int or observation['pid'] != peer_pid
                or type(observation['attempt_id']) is not int
                or type(observation['started_ns']) is not int):
            raise AssertionError('rendezvous_peer_marker_invalid')
        with connect(self.directory) as conn:
            row = conn.execute('SELECT pid,method,started_ns FROM attempts WHERE id=?',
                               (observation['attempt_id'],)).fetchone()
        if (row is None or row['pid'] != peer_pid or row['method'] == 'GET'
                or row['started_ns'] != observation['started_ns']):
            raise AssertionError('rendezvous_peer_attempt_invalid')
        self._first_mutation_joined = True

    @staticmethod
    def page(items, request):
        page = int(request.url.params.get('page', 1))
        per_page = int(request.url.params.get('per_page', 50))
        return dict(items=items[(page-1)*per_page:page*per_page], page=page, per_page=per_page,
                    total=len(items), total_pages=(len(items)+per_page-1)//per_page)

    def __call__(self, request):
        path = request.url.path.removeprefix('/api/v2')
        parts = path.strip('/').split('/')
        method = request.method
        body = json.loads(request.content) if request.content else {}
        identifier = int(parts[1]) if parts[0] == 'tasks' else None
        logical = body.get('title') or body.get('comment') or ''
        started = time.perf_counter_ns()
        with connect(self.directory) as conn:
            attempt = conn.execute('INSERT INTO attempts(pid,method,path,started_ns,task,logical,page) VALUES(?,?,?,?,?,?,?)',
                (os.getpid(), method, path, started, identifier, logical, int(request.url.params['page']) if 'page' in request.url.params else None)).lastrowid
        # Provider delay/barriers must never hold a shared DB transaction.
        if method != 'GET':
            self._join_first_mutation(attempt, started)
            time.sleep(.035)
        with connect(self.directory) as conn:
            conn.execute('BEGIN IMMEDIATE')
            status, outcome = 200, 'ok'
            if path == '/user' and method == 'GET':
                value = {'id': 19, 'bot_owner_id': 1}
            elif parts[0] == 'labels' and method == 'GET':
                value = next(dict(id=int(parts[1]), title='secretary:' + kind, created_by={'id': 19})
                    for project in ('7', '9') for kind in ('important', 'urgent', 'cancelled')
                    if parts[1] == getattr(binding(project), kind + '_label_id'))
            elif parts[0] == 'projects':
                b = binding(parts[1])
                view = dict(id=int(b.manual_view_id), project_id=int(b.project_id), view_kind='kanban',
                    bucket_configuration_mode='manual', done_bucket_id=0, default_bucket_id=int(b.bucket_ids['inbox']))
                if parts[2:] == ['views']:
                    value = self.page([view], request)
                elif parts[2:] == ['views', b.manual_view_id]:
                    value = view
                elif parts[2:] == ['views', b.manual_view_id, 'buckets']:
                    value = self.page([dict(id=int(v), project_view_id=int(b.manual_view_id)) for v in b.bucket_ids.values()], request)
                elif parts[2:] == ['tasks'] and method == 'GET':
                    value = self.page([json.loads(row[0]) for row in conn.execute('SELECT payload FROM tasks WHERE project=? ORDER BY id', (int(b.project_id),))], request)
                else:
                    raise AssertionError('Unexpected synthetic project request')
            elif parts[0] == 'tasks':
                task = json.loads(conn.execute('SELECT payload FROM tasks WHERE id=?', (identifier,)).fetchone()[0])
                if len(parts) == 2 and method == 'GET':
                    value = task
                elif len(parts) == 2 and method == 'PATCH':
                    assert set(body) == {'title'}, 'Load scenario must preserve outside fields'
                    fault = conn.execute('SELECT mode FROM faults WHERE task=?', (identifier,)).fetchone()
                    mode = fault[0] if fault else None
                    if mode == '429':
                        status, value, outcome = 429, {'code': 'synthetic_rate_limit'}, 'rejected_429'
                    else:
                        task.update(body)
                        conn.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(task), identifier))
                        conn.execute('INSERT INTO effects(attempt,task,kind,logical) VALUES(?,?,?,?)', (attempt, identifier, 'rename', logical))
                        value = task
                        if mode in ('503', 'timeout'):
                            outcome = 'unknown_' + mode
                            status = 503
                            value = {'code': 'synthetic_unknown'}
                elif parts[2:] == ['comments'] and method == 'GET':
                    value = self.page([json.loads(row[0]) for row in conn.execute('SELECT payload FROM comments WHERE task=? ORDER BY id', (identifier,))], request)
                elif parts[2:] == ['comments'] and method == 'POST':
                    comment_id = conn.execute('INSERT INTO comments(task,payload) VALUES(?,?)', (identifier, '{}')).lastrowid
                    value = dict(id=comment_id, comment=body['comment'], author={'id': 19})
                    conn.execute('UPDATE comments SET payload=? WHERE id=?', (json.dumps(value), comment_id))
                    conn.execute('INSERT INTO effects(attempt,task,kind,logical) VALUES(?,?,?,?)', (attempt, identifier, 'comment', logical))
                    status = 201
                else:
                    raise AssertionError('Unexpected synthetic task request')
            else:
                raise AssertionError('Unexpected synthetic HTTP request')
            conn.execute('UPDATE attempts SET finished_ns=?,outcome=? WHERE id=?', (time.perf_counter_ns(), outcome, attempt))
            conn.commit()
        if outcome == 'unknown_timeout':
            raise httpx.ReadTimeout('Synthetic acknowledgement lost', request=request)
        headers = {'ETag': '"fixture-' + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() + '"'}
        return httpx.Response(status, json=value, headers=headers)


def clients(directory, members, *, rendezvous_index=None):
    remote = DurableRemote(directory, rendezvous_index=rendezvous_index)
    result = {}
    for project in ('7', '9'):
        http = httpx.Client(base_url='http://127.0.0.1:34891/api/v2',
            headers={'Authorization': 'Bearer SYNTHETIC_LOAD_ONLY'}, trust_env=False,
            follow_redirects=False, timeout=5, transport=httpx.MockTransport(remote))
        result[project] = VikunjaClient(http, binding=binding(project), members=MappingMemberDirectory(members))
    return result


def wait_file(path, timeout=90):
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() > deadline:
            raise TimeoutError('Synthetic phase barrier timed out')
        time.sleep(.01)


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.replace(path)


def command_inputs(repo, members, index):
    project, start, offset = ('7', 1001, 0) if index == 0 else ('9', 1501, 5)
    rows = []
    for number in range(20):
        actor = members[offset] if number < 4 else members[offset + 1 + (number-4)%4]
        snapshot = repo.get_projection(actor.id, str(start+number))
        action = 'rename' if number < 4 else 'comment'
        values = {'title': f'Synthetic rename {index}:{number}'} if action == 'rename' else {'comment': f'Synthetic comment {index}:{number}'}
        cmd = TaskCommand(operation_id=str(uuid4()), project_id=project, task_id=snapshot.task_id,
            expected_revision=snapshot.revision, expected_fingerprint=snapshot.remote_fingerprint,
            action=action, values=values)
        rows.append((actor, cmd))
    for number, task in enumerate((1041, 1541, 1042, 1542, 1043)):
        actor = members[0 if task < 1500 else 5]
        snapshot = repo.get_projection(actor.id, str(task))
        cmd = TaskCommand(operation_id=str(uuid4()), project_id=snapshot.project_id, task_id=snapshot.task_id,
            expected_revision=snapshot.revision, expected_fingerprint=snapshot.remote_fingerprint,
            action='rename', values={'title': f'Synthetic race {index}:{number}'})
        rows.append((actor, cmd))
    return rows


def child(directory, index):
    directory = Path(directory)
    members = tuple(TeamMember.model_validate(x) for x in json.loads((directory / 'members.json').read_text(encoding='utf-8')))
    repo = TeamRepository(TeamDatabase(directory / 'team.sqlite3'), clock=lambda: NOW)
    services = clients(directory, members, rendezvous_index=index)
    write_json(directory / f'ready-{index}.json', {'pid': os.getpid()})
    wait_file(directory / 'go-sync')
    scan_started = time.perf_counter()
    sync = TeamSyncService(TeamSyncRepository(repo, clock=lambda: NOW), services['7' if index == 0 else '9'], worker_id=f'load-sync-{index}')
    status = sync.sync_once()
    assert status.state == 'ready', status
    scan_seconds = time.perf_counter() - scan_started
    write_json(directory / f'synced-{index}.json', {'state': status.state, 'scan_seconds': scan_seconds})
    wait_file(directory / 'go-accept')
    inputs = command_inputs(repo, members, index)
    start = time.perf_counter_ns()
    barrier = Barrier(25, action=lambda: write_json(directory / f'intake-ready-{index}.json', {'callers': 25}), timeout=30)
    def accept(item):
        actor, cmd = item
        barrier.wait()
        # All fifty callers are queued at the boundary before any acceptance.
        wait_file(directory / f'intake-ready-{1-index}.json', timeout=30)
        started = time.perf_counter_ns()
        receipt = repo.accept_command(actor.id, cmd, expected_actor_revision=actor.revision)
        assert receipt.acceptance_receipt.decision == 'accepted', receipt
        duplicate = repo.accept_command(actor.id, cmd, expected_actor_revision=actor.revision)
        assert duplicate.acceptance_receipt == receipt.acceptance_receipt
        finished = time.perf_counter_ns()
        return dict(actor_id=actor.id, command=cmd.model_dump(mode='json'),
                    started_ns=started, finished_ns=finished, acceptance_ns=finished-started)
    with ThreadPoolExecutor(max_workers=25) as pool:
        accepted = list(pool.map(accept, inputs))
    write_json(directory / f'accepted-{index}.json', {'pid': os.getpid(), 'started_ns': start,
        'finished_ns': time.perf_counter_ns(), 'operations': accepted})
    wait_file(directory / 'go-execute')
    class Router:
        def execute(self, claim):
            return TeamTaskService(repo, services[claim.command.project_id]).execute(claim)
    worker = TeamWorker(repo, Router(), worker_id=f'load-worker-{index}')
    executed, execution_ns = [], []
    async def drain():
        while True:
            before = time.perf_counter_ns()
            result = await worker.run_once()
            if result is None:
                break
            executed.append(result.acceptance_receipt.operation_id)
            execution_ns.append(time.perf_counter_ns()-before)
        # Another idle pass must never replay a formerly ambiguous request.
        assert await worker.run_once() is None
    asyncio.run(drain())
    acl_checks = 0
    for member in members[index*5:index*5+5]:
        foreign = '9' if member.project_ids == ('7',) else '7'
        try:
            repo.list_projections(member.id, foreign)
        except TeamForbidden:
            acl_checks += 1
        else:
            raise AssertionError('Cross-project read leaked')
        try:
            repo.get_projection(member.id, '1501' if foreign == '9' else '1001')
        except TeamForbidden:
            acl_checks += 1
        else:
            raise AssertionError('Cross-project task leaked')
    from team_load_cloud_fixture import run_cloud_phase
    cloud = run_cloud_phase(directory, index, None)
    write_json(directory / f'result-{index}.json', dict(pid=os.getpid(), scan_seconds=scan_seconds,
        accepted=len(accepted), executed=executed, execution_ns=execution_ns, acl_checks=acl_checks, cloud=cloud))
    for client in services.values():
        client._http.close()


def mutation_child(directory, index):
    """Small owned-process proof of the same load rendezvous, without Team work."""
    remote = DurableRemote(directory, rendezvous_index=index)
    write_json(directory / f'ready-{index}.json', {'pid': os.getpid()})
    wait_file(directory / f'go-mutation-{index}')
    for number in range(2):
        task = 1004 + index
        response = remote(httpx.Request('PATCH', f'http://127.0.0.1:34891/api/v2/tasks/{task}',
                                      json={'title': f'Synthetic rendezvous {index}:{number}'}))
        assert response.status_code == 200
    write_json(directory / f'result-{index}.json', {'pid': os.getpid()})


if __name__ == '__main__':
    if sys.argv[2] == '--mutation-only':
        mutation_child(Path(sys.argv[1]), int(sys.argv[3]))
    else:
        child(Path(sys.argv[1]), int(sys.argv[2]))
