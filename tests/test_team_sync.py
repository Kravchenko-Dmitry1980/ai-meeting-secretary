"""T9 polling against real SQLite and the real read-only Vikunja mapper."""
from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from secretary.domain.team import TaskCommand, TaskSnapshot, TeamConflict, TeamForbidden, TeamMember
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository
from secretary.infrastructure.vikunja import MappingMemberDirectory, ProjectBinding, VikunjaClient


def api():
    names = ('secretary.infrastructure.team_sync_repository', 'secretary.application.team_sync')
    for name in names:
        assert importlib.util.find_spec(name) is not None, f'Missing sync implementation: {name}'
    return tuple(importlib.import_module(name) for name in names)


def uid():
    return str(uuid4())


@pytest.fixture
def case(tmp_path):
    storage, application = api()
    now = [datetime(2026, 10, 4, 12, tzinfo=timezone.utc)]
    team = TeamRepository(TeamDatabase(tmp_path / 'synthetic-sync.sqlite3'), clock=lambda: now[0])
    member = TeamMember(id=uid(), display_name='Synthetic member', max_user_id='11',
                        vikunja_user_id='12', project_ids=('7',))
    team.upsert_member(member, expected_revision=None)
    binding = ProjectBinding('7', '17', '18',
        dict(zip(('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled'), map(str, range(31, 38)))),
        '41', '42', '43')
    remote = {str(n): dict(id=n, project_id=7, title=f'Original {n}', description='<p>Literal</p>',
        done=False, due_date='0001-01-01T00:00:00Z', assignees=[{'id': 12}], labels=[],
        buckets=[{'id': 31, 'project_view_id': 17}], repeat_after=0, repeat_mode=0) for n in (55, 56)}
    control = SimpleNamespace(hook=None, fail_page=None, duplicate=False, reads={}, requests=[], invalid_binding=False)

    def transport(request):
        assert request.method == 'GET', 'A sync must never mutate the provider'
        control.requests.append(request.url.path)
        path = request.url.path.removeprefix('/api/v2')
        view = dict(id=17, project_id=7, view_kind='kanban', bucket_configuration_mode='manual',
                    done_bucket_id=99 if control.invalid_binding else 0, default_bucket_id=31)
        if path == '/user':
            return httpx.Response(200, json={'id': 18, 'bot_owner_id': 19})
        if path == '/projects/7/views/17':
            return httpx.Response(200, json=view)
        if path == '/projects/7/views':
            return httpx.Response(200, json=dict(items=[view], page=1, per_page=50, total=1, total_pages=1))
        if path == '/projects/7/views/17/buckets':
            return httpx.Response(200, json={'items': [{'id': n, 'project_view_id': 17} for n in range(31, 38)]})
        if path.startswith('/labels/'):
            identifier = int(path.rsplit('/', 1)[1])
            return httpx.Response(200, json={'id': identifier, 'created_by': {'id': 18},
                'title': {41: 'secretary:important', 42: 'secretary:urgent', 43: 'secretary:cancelled'}[identifier]})
        if path == '/projects/7/tasks':
            page = int(request.url.params['page'])
            if page == control.fail_page:
                return httpx.Response(503, json={'secret': 'DO_NOT_EXPOSE_PROVIDER_BODY'})
            items = list(remote.values())
            # Two bounded provider pages independently exercise full traversal.
            items = items[page - 1:page]
            if control.duplicate and page == 2:
                items = [remote['55']]
            return httpx.Response(200, json=dict(items=items, page=page, per_page=1,
                total=len(remote), total_pages=len(remote)))
        identifier = path.split('/')[2]
        if path.endswith('/comments'):
            return httpx.Response(200, json=dict(items=[], page=1, per_page=50, total=0, total_pages=0))
        control.reads[identifier] = control.reads.get(identifier, 0) + 1
        if control.hook:
            hook, control.hook = control.hook, None
            hook()
        return httpx.Response(200, json=remote[identifier])

    http = httpx.Client(base_url='http://127.0.0.1:34891/api/v2', headers={'Authorization': 'Bearer SYNTHETIC'},
        trust_env=False, follow_redirects=False, timeout=5, transport=httpx.MockTransport(transport))
    client = VikunjaClient(http, binding=binding, members=MappingMemberDirectory((member,)))
    snapshots = {}
    for identifier in remote:
        observed = client.observe_task(identifier)
        snapshots[identifier] = TaskSnapshot(task_id=identifier, project_id='7', revision=0,
            remote_fingerprint=observed.remote_fingerprint, title=observed.title, description=observed.description,
            assignee_id=member.id, important=False, urgent=False, classification_confirmed=True)
        team.save_projection(snapshots[identifier], expected_revision=None)
    control.reads.clear()
    control.requests.clear()
    repository = storage.TeamSyncRepository(team, clock=lambda: now[0])
    yield SimpleNamespace(storage=storage, application=application, now=now, team=team, db=team.db,
        member=member, remote=remote, client=client, control=control, snapshots=snapshots,
        repository=repository, service=application.TeamSyncService(repository, client))
    http.close()


def rows(c, table):
    assert table in {'team_sync_runs', 'team_sync_results', 'team_sync_observations'}
    with c.db.connection() as conn:
        return tuple(dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY rowid'))


def test_complete_poll_persists_real_success_without_advancing_unchanged_projection(case):
    c = case
    assert c.repository.status(c.member.id, '7').state == 'never_synced'
    first = c.service.sync_once()
    assert first.state == 'ready' and first.last_successful_sync_at == c.now[0]
    assert c.control.reads == {'55': 1, '56': 1}
    assert c.team.get_projection(c.member.id, '55').revision == 0
    c.now[0] += timedelta(seconds=45)
    second = c.service.sync_once()
    assert second.last_successful_sync_at == c.now[0]
    assert c.team.get_projection(c.member.id, '55').revision == 0
    assert len(rows(c, 'team_sync_runs')) == len(rows(c, 'team_sync_results')) == 2
    assert rows(c, 'team_sync_observations') == ()


def test_invalid_remote_binding_cannot_claim_a_successful_sync(case):
    c = case
    c.control.invalid_binding = True
    c.remote['55']['title'] = 'Must not adopt under invalid manual view'
    result = c.service.sync_once()
    assert result.state == 'degraded' and result.last_successful_sync_at is None
    assert c.team.get_projection(c.member.id, '55') == c.snapshots['55']
    assert not c.control.reads


def test_remote_change_is_atomically_observed_without_inventing_an_author(case):
    c = case
    c.remote['55']['title'] = 'External change'
    result = c.service.sync_once()
    assert result.state == 'ready'
    current = c.team.get_projection(c.member.id, '55')
    assert current.title == 'External change' and current.revision == 1
    evidence = next(row for row in rows(c, 'team_sync_observations') if row['task_id'] == '55')
    assert evidence['event'] == 'remote_change_observed'
    assert json.loads(evidence['changed_fields']) == ['title']
    assert evidence['before_fingerprint'] == c.snapshots['55'].remote_fingerprint
    assert evidence['after_fingerprint'] == current.remote_fingerprint
    assert 'actor_id' not in evidence and 'remote_updated_at' not in evidence


def test_changed_external_classification_loses_confirmation_without_fabricating_one(case):
    c = case
    c.remote['55']['labels'] = [{'id': 41}]
    result = c.service.sync_once()
    current = c.team.get_projection(c.member.id, '55')
    assert result.state == 'ready'
    assert current.important is True and current.urgent is False and current.classification_confirmed is False


def test_unchanged_unclassified_task_is_success_and_keeps_revision(case):
    c = case
    baseline = c.snapshots['55'].model_copy(update={'revision': 1, 'classification_confirmed': False})
    c.team.save_projection(baseline, expected_revision=0)
    assert c.service.sync_once().state == 'ready'
    assert c.team.get_projection(c.member.id, '55') == baseline


def test_same_remote_fingerprint_cannot_hide_a_pre_poll_local_identity_remap(case):
    c = case
    c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'vikunja_user_id': '99'}), expected_revision=0)
    replacement = TeamMember(id=uid(), display_name='Replacement synthetic member', max_user_id='22',
        vikunja_user_id='12', project_ids=('7',))
    c.team.upsert_member(replacement, expected_revision=None)
    result = c.service.sync_once()
    assert result.state == 'degraded' and result.error_code == 'sync_mapping_changed'
    assert result.last_successful_sync_at is None
    assert c.team.get_projection(c.member.id, '55') == c.snapshots['55']


@pytest.mark.parametrize('failure', ['last_page', 'duplicate', 'missing'])
def test_incomplete_poll_retains_previous_success_and_never_partially_applies(case, failure):
    c = case
    c.service.sync_once()
    previous = c.now[0]
    c.now[0] += timedelta(minutes=1)
    c.remote['55']['title'] = 'Must not apply'
    if failure == 'last_page':
        c.control.fail_page = 2
    elif failure == 'duplicate':
        c.control.duplicate = True
    else:
        del c.remote['56']
    result = c.service.sync_once()
    assert result.state == 'degraded' and result.issue_count >= 1
    assert result.last_successful_sync_at == previous
    assert c.team.get_projection(c.member.id, '55') == c.snapshots['55']
    assert 'DO_NOT_EXPOSE' not in result.model_dump_json()
    if failure == 'missing':
        assert any(row['task_id'] == '56' and row['event'] == 'remote_task_missing'
                   for row in rows(c, 'team_sync_observations'))


@pytest.mark.parametrize('remote_change', [
    {'due_date': '2026-10-05T10:00:00Z'},
    {'assignees': [{'id': 999}]}, {'repeat_after': 1},
    {'assignees': [{'id': 12}, {'id': 13}]}, {'done': True},
])
def test_unconfirmed_or_unsupported_remote_state_is_degraded_evidence_not_projection(case, remote_change):
    c = case
    c.remote['55'].update(remote_change)
    result = c.service.sync_once()
    assert result.state == 'degraded' and result.last_successful_sync_at is None
    assert c.team.get_projection(c.member.id, '55') == c.snapshots['55']
    evidence = next(row for row in rows(c, 'team_sync_observations') if row['task_id'] == '55')
    assert evidence['event'] == 'remote_observation_rejected' and evidence['error_code']


@pytest.mark.parametrize('state', ['running', 'uncertain'])
def test_held_command_resource_even_uncertain_preserves_baseline(case, state):
    c = case
    initial = c.snapshots['55']
    command = TaskCommand(operation_id=uid(), project_id='7', task_id='55', expected_revision=0,
        expected_fingerprint=initial.remote_fingerprint, action='set_state', values={'bucket': 'doing'})
    c.team.accept_command(c.member.id, command)
    claim = c.team.claim_command('synthetic-command')
    if state == 'uncertain':
        c.team.record_remote_result(claim, state='uncertain', error_code='synthetic_lost_ack')
    c.remote['55']['title'] = 'Intermediate provider state'
    result = c.service.sync_once()
    assert result.state == 'degraded' and result.error_code == 'sync_task_busy'
    assert c.team.get_projection(c.member.id, '55') == initial
    assert any(row['task_id'] == '55' and row['error_code'] == 'sync_task_busy'
               for row in rows(c, 'team_sync_observations'))


def test_held_create_prevents_adopting_a_partial_project(case):
    c = case
    owner = c.member.model_copy(update={'revision': 1, 'role': 'owner'})
    c.team.upsert_member(owner, expected_revision=0)
    command = TaskCommand(operation_id=uid(), project_id='7', action='create',
        values={'title': 'Creating', 'assignee_id': owner.id})
    c.team.accept_command(owner.id, command)
    assert c.team.claim_command('creator')
    c.remote['55']['title'] = 'Must remain observed only'
    result = c.service.sync_once()
    assert result.state == 'degraded' and result.error_code == 'sync_task_busy'
    assert c.team.get_projection(owner.id, '55') == c.snapshots['55']


def test_resource_claimed_during_http_also_blocks_commit(case):
    c = case
    command = TaskCommand(operation_id=uid(), project_id='7', task_id='55', expected_revision=0,
        expected_fingerprint=c.snapshots['55'].remote_fingerprint, action='set_state', values={'bucket': 'doing'})
    c.team.accept_command(c.member.id, command)
    c.control.hook = lambda: c.team.claim_command('late-resource-owner')
    c.remote['55']['title'] = 'Must not replace command baseline'
    assert c.service.sync_once().error_code == 'sync_task_busy'
    assert c.team.get_projection(c.member.id, '55') == c.snapshots['55']


@pytest.mark.parametrize('race', ['projection', 'member', 'new_task'])
def test_commit_rechecks_entire_frozen_scope_before_any_projection_write(case, race):
    c = case
    c.remote['55']['title'] = 'Remote new title'
    def during_read():
        if race == 'member':
            c.team.upsert_member(c.member.model_copy(update={'revision': 1}), expected_revision=0)
        elif race == 'projection':
            c.team.save_projection(c.snapshots['56'].model_copy(update={'revision': 1, 'title': 'Local concurrent edit'}), expected_revision=0)
        else:
            c.team.save_projection(c.snapshots['56'].model_copy(update={'task_id': '57'}), expected_revision=None)
    c.control.hook = during_read
    result = c.service.sync_once()
    assert result.state == 'degraded' and result.last_successful_sync_at is None
    assert c.team.get_projection(c.member.id, '55') == c.snapshots['55']


def test_expired_fence_cannot_finish_over_a_new_run(case):
    c = case
    first = c.repository.begin('7', worker_id='first', lease_seconds=5)
    c.now[0] += timedelta(seconds=5)
    second = c.repository.begin('7', worker_id='second')
    assert second.fence > first.fence
    with pytest.raises(TeamConflict, match='sync_claim_lost'):
        c.repository.fail(first, 'sync_read_failed')
    assert c.repository.status(c.member.id, '7').state == 'syncing'
    assert c.repository.fail(second, 'sync_read_failed').state == 'degraded'


def test_concurrent_begin_has_one_live_claim(case):
    c = case
    def begin(worker):
        try:
            return c.repository.begin('7', worker_id=worker)
        except TeamConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(pool.map(begin, ('first', 'second')))
    assert sum(claim is not None for claim in claims) == 1


def test_same_completion_replays_without_duplicate_evidence_and_changed_completion_conflicts(case):
    c = case
    claim = c.repository.begin('7')
    c.remote['55']['title'] = 'External title'
    captured = c.client.observe_task('55')
    changed = c.snapshots['55'].model_copy(update={'title': 'External title', 'revision': 1,
        'remote_fingerprint': captured.remote_fingerprint})
    projections = (changed, c.snapshots['56'])
    evidence = (c.storage.SyncObservation('55', captured.remote_fingerprint, ('title',)),
                c.storage.SyncObservation('56', c.snapshots['56'].remote_fingerprint))
    first = c.repository.commit(claim, evidence, projections, ('55', '56'))
    c.now[0] += timedelta(hours=1)
    replay = c.repository.commit(claim, evidence, projections, ('55', '56'))
    assert replay == first
    assert len(rows(c, 'team_sync_observations')) == 1 and len(rows(c, 'team_sync_results')) == 1
    with pytest.raises(TeamConflict, match='sync_result_conflict'):
        c.repository.fail(claim, 'sync_read_failed')


def test_renewed_claim_and_its_immutable_input_are_checked(case):
    c = case
    claim = c.repository.begin('7', lease_seconds=5)
    c.now[0] += timedelta(seconds=4)
    claim = c.repository.renew(claim, lease_seconds=20)
    c.now[0] += timedelta(seconds=5)
    with pytest.raises(TeamConflict, match='sync_claim_lost'):
        c.repository.fail(replace(claim, members=()), 'sync_read_failed')
    assert c.repository.fail(claim, 'sync_read_failed').state == 'degraded'


def test_status_requires_current_actor_revision_and_project_access(case):
    c = case
    c.service.sync_once()
    with pytest.raises(TeamForbidden):
        c.repository.status(c.member.id, '8')
    with pytest.raises(TeamForbidden):
        c.repository.status(c.member.id, '7', expected_actor_revision=1)
    c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'enabled': False}), expected_revision=0)
    with pytest.raises(TeamForbidden):
        c.repository.status(c.member.id, '7')


@pytest.mark.parametrize('table', ['team_sync_runs', 'team_sync_results', 'team_sync_observations'])
def test_sync_evidence_rejects_update_delete_and_replace_even_for_raw_sqlite(case, table):
    c = case
    c.remote['55']['title'] = 'Observed delta'
    c.service.sync_once()
    with sqlite3.connect(c.db.path) as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        row = conn.execute(f'SELECT * FROM {table} LIMIT 1').fetchone()
        assert row
        for sql, parameters in ((f'DELETE FROM {table}', ()),
            (f'UPDATE {table} SET rowid=rowid', ()),
            (f'INSERT OR REPLACE INTO {table} VALUES({",".join("?" for _ in row)})', row)):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, parameters)
            conn.rollback()


def test_v5_database_migrates_once_without_changing_existing_projection(tmp_path):
    api()
    from secretary.infrastructure.team_database import SCHEMA, INSERT_GUARDS
    from secretary.infrastructure.team_auth_repository import AUTH_MIGRATION
    from secretary.infrastructure.bot_repository import BOT_MIGRATION
    from secretary.infrastructure.voice_repository import VOICE_MIGRATION
    path = tmp_path / 'synthetic-v5.sqlite3'
    with sqlite3.connect(path) as conn:
        for statement in (*SCHEMA, *INSERT_GUARDS, *AUTH_MIGRATION, *BOT_MIGRATION, *VOICE_MIGRATION):
            conn.execute(statement)
        conn.execute('INSERT INTO team_schema VALUES(5)')
        frozen = TaskSnapshot(task_id='55', project_id='7', revision=8, remote_fingerprint='a' * 64,
                              title='Preserved synthetic projection').model_dump_json()
        conn.execute('INSERT INTO team_projections VALUES(?,?,?,?)', ('55', '7', 8, frozen))
    database = TeamDatabase(path)
    TeamDatabase(path)
    with database.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 9
        assert conn.execute('SELECT COUNT(*) FROM team_schema WHERE version=6').fetchone()[0] == 1
        assert conn.execute('PRAGMA foreign_key_check').fetchone() is None
        assert conn.execute('SELECT payload FROM team_projections WHERE task_id=?', ('55',)).fetchone()[0] == frozen


def test_migration_failure_rolls_back_version_and_new_tables(tmp_path, monkeypatch):
    storage, _ = api()
    from secretary.infrastructure.team_database import SCHEMA, INSERT_GUARDS
    from secretary.infrastructure.team_auth_repository import AUTH_MIGRATION
    from secretary.infrastructure.bot_repository import BOT_MIGRATION
    from secretary.infrastructure.voice_repository import VOICE_MIGRATION
    path = tmp_path / 'synthetic-migration-rollback.sqlite3'
    with sqlite3.connect(path) as conn:
        for statement in (*SCHEMA, *INSERT_GUARDS, *AUTH_MIGRATION, *BOT_MIGRATION, *VOICE_MIGRATION):
            conn.execute(statement)
        conn.execute('INSERT INTO team_schema VALUES(5)')
    monkeypatch.setattr(storage, 'SYNC_MIGRATION', storage.SYNC_MIGRATION + ('SELECT * FROM missing_synthetic_table',))
    with pytest.raises(sqlite3.OperationalError):
        TeamDatabase(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'team_sync_%'").fetchone()[0] == 0
