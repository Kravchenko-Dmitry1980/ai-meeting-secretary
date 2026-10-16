"""T12 owner resolution uses real Team SQLite and the real Vikunja transport."""
from __future__ import annotations

import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from secretary.application.team_sync import TeamSyncService
from secretary.application.team_tasks import TeamTaskService
from secretary.domain.team import TaskCommand, TaskSnapshot, TeamConflict, TeamForbidden, TeamMember
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import RepositoryMemberDirectory, TeamRepository
from secretary.infrastructure.team_sync_repository import TeamSyncRepository
from secretary.infrastructure.vikunja import ProjectBinding, VikunjaClient, ZERO_DATE


def uid():
    return str(uuid4())


@pytest.fixture
def case(tmp_path):
    domain = importlib.import_module('secretary.domain.team_due_resolution')
    storage = importlib.import_module('secretary.infrastructure.team_due_resolution_repository')
    application = importlib.import_module('secretary.application.team_due_resolution')
    now = [datetime(2026, 10, 4, 12, tzinfo=timezone.utc)]
    team = TeamRepository(TeamDatabase(tmp_path / 'due-resolution.sqlite3'), clock=lambda: now[0])
    owner = TeamMember(id=uid(), display_name='Synthetic owner', role='owner', max_user_id='11',
                       vikunja_user_id='12', project_ids=('7',))
    member = TeamMember(id=uid(), display_name='Synthetic member', max_user_id='13',
                        vikunja_user_id='14', project_ids=('7',))
    for person in (owner, member):
        team.upsert_member(person, expected_revision=None)
    binding = ProjectBinding('7', '17', '18',
        dict(zip(('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled'), map(str, range(31, 38)))),
        '41', '42', '43')
    remote = dict(id=55, project_id=7, title='Synthetic task', description='<p>Unchanged</p>', done=False,
                  due_date=ZERO_DATE, assignees=[{'id': 14}], labels=[],
                  buckets=[{'id': 31, 'project_view_id': 17}], repeat_after=0, repeat_mode=0)
    comments = []
    control = SimpleNamespace(requests=[], after_read=None, after_write=None, lost_ack=None, final_change=False)

    def transport(request):
        path = request.url.path.removeprefix('/api/v2')
        control.requests.append((request.method, path))
        view = dict(id=17, project_id=7, view_kind='kanban', bucket_configuration_mode='manual',
                    done_bucket_id=0, default_bucket_id=31)
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
            return httpx.Response(200, json=dict(items=[dict(remote)], page=1, per_page=50, total=1, total_pages=1))
        assert path in {'/tasks/55', '/tasks/55/comments'}, path
        if request.method == 'GET':
            payload = (dict(items=[dict(item) for item in comments], page=1, per_page=50,
                            total=len(comments), total_pages=1 if comments else 0)
                       if path.endswith('/comments') else dict(remote))
            if not path.endswith('/comments') and control.after_read:
                hook, control.after_read = control.after_read, None
                hook()
            return httpx.Response(200, json=payload)
        assert request.method in {'POST', 'PATCH'}, request.method
        payload = json.loads(request.content)
        if path.endswith('/comments'):
            value = dict(id=901 + len(comments), comment=payload['comment'], author={'id': 18})
            comments.append(value)
            response = value
        else:
            assert set(payload) == {'due_date'}
            remote.update(payload)
            response = dict(remote)
        if control.after_write:
            hook, control.after_write = control.after_write, None
            hook()
        if control.lost_ack == path:
            control.lost_ack = None
            raise httpx.ReadTimeout('Synthetic lost acknowledgement')
        return httpx.Response(201 if path.endswith('/comments') else 200, json=response)

    http = httpx.Client(base_url='http://127.0.0.1:34891/api/v2', headers={'Authorization': 'Bearer SYNTHETIC'},
        trust_env=False, follow_redirects=False, timeout=5, transport=httpx.MockTransport(transport))
    client = VikunjaClient(http, binding=binding, members=RepositoryMemberDirectory(team))
    observed = client.observe_task('55')
    baseline = TaskSnapshot(task_id='55', project_id='7', revision=0, remote_fingerprint=observed.remote_fingerprint,
        title=observed.title, description=observed.description, assignee_id=member.id,
        important=False, urgent=False, classification_confirmed=True)
    team.save_projection(baseline, expected_revision=None)
    sync = TeamSyncService(TeamSyncRepository(team, clock=lambda: now[0]), client)
    repo = storage.TeamDueResolutionRepository(team, clock=lambda: now[0])
    service = application.TeamDueResolutionService(repo, client)
    remote['due_date'] = '2026-10-05T10:00:00Z'
    assert sync.sync_once().error_code == 'remote_due_unconfirmed'
    control.requests.clear()
    yield SimpleNamespace(domain=domain, storage=storage, application=application, now=now, team=team,
        owner=owner, member=member, client=client, baseline=baseline, remote=remote, comments=comments,
        control=control, repo=repo, service=service, sync=sync, tasks=TeamTaskService(team, client))
    http.close()


def preview(c, *, due_at='2026-10-06T10:00:00Z', actor=None):
    actor = actor or c.owner
    candidate = c.service.candidate(actor.id, '55', expected_actor_revision=actor.revision)
    request = c.domain.DueResolutionRequest(observation_id=candidate.observation_id,
        expected_revision=candidate.baseline_revision, expected_fingerprint=candidate.observed_fingerprint,
        due_at=due_at, reason='Confirmed synthetic decision')
    return c.service.preview(actor.id, '55', request, expected_actor_revision=actor.revision)


def confirm(c, p, *, operation_id=None, actor=None):
    actor = actor or c.owner
    return c.service.confirm(actor.id, p.preview_id,
        c.domain.DueResolutionConfirmation(operation_id=operation_id or uid()), expected_actor_revision=actor.revision)


def writes(c):
    return [item for item in c.control.requests if item[0] != 'GET']


@pytest.mark.parametrize('chosen', ['2026-10-06T10:00:00Z', None, '2026-10-05T10:00:00Z'])
def test_explicit_owner_resolution_applies_only_after_final_get_and_keeps_evidence(case, chosen):
    c = case
    p = preview(c, due_at=chosen)
    assert not writes(c)
    receipt = confirm(c, p)
    assert receipt.execution_state.state == 'queued' and receipt.current == c.baseline
    claim = c.team.claim_command('resolver')
    assert claim.command.action == 'resolve_due'
    assert claim.command.expected_fingerprint == c.baseline.remote_fingerprint
    result = c.tasks.execute(claim)
    assert result.execution_state.state == 'applied', result.execution_state
    expected = datetime.fromisoformat(chosen.replace('Z', '+00:00')) if chosen else None
    assert result.current.due_at == expected and result.current.due_confirmed
    assert result.current.revision == 1 and result.current.assignee_id == c.member.id
    assert result.current.description == c.baseline.description
    assert writes(c)[-1] == ('POST', '/tasks/55/comments')
    assert c.control.requests[-1] == ('GET', '/tasks/55/comments')
    with c.team.db.connection() as conn:
        progress = c.team._remote_progress(conn, claim.command.operation_id)
        assert progress['steps'][0]['before_fingerprint'] == p.candidate.observed_fingerprint
        assert progress['steps'][0]['before_image']['due_at'] == '2026-10-05T10:00:00+00:00'
    assert c.sync.sync_once().state == 'ready'


def test_member_cannot_read_preview_or_confirm_owner_resolution(case):
    c = case
    with pytest.raises(TeamForbidden):
        c.service.candidate(c.member.id, '55', expected_actor_revision=0)
    p = preview(c)
    with pytest.raises(TeamForbidden):
        confirm(c, p, actor=c.member)
    assert not writes(c)


@pytest.mark.parametrize('change', ['role', 'project', 'disable', 'revision'])
def test_revoked_owner_or_acl_revision_blocks_confirmation_without_consumption(case, change):
    c = case
    p = preview(c)
    changes = {'role': {'role': 'member'}, 'project': {'project_ids': ('8',)},
               'disable': {'enabled': False}, 'revision': {'display_name': 'Changed owner'}}[change]
    c.team.upsert_member(c.owner.model_copy(update={**changes, 'revision': 1}), expected_revision=0)
    with pytest.raises((TeamForbidden, TeamConflict)):
        confirm(c, p)
    with c.team.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_due_resolution_consumptions').fetchone()[0] == 0
    assert not writes(c)


def test_late_remote_change_invalidates_preview_and_worker(case):
    c = case
    p = preview(c)
    c.remote['title'] = 'Changed after preview'
    with pytest.raises(TeamConflict, match='due_resolution_remote_changed'):
        confirm(c, p)
    assert not writes(c)
    c.sync.sync_once()
    p = preview(c)
    receipt = confirm(c, p)
    claim = c.team.claim_command('resolver')
    c.remote['title'] = 'Changed after acceptance'
    result = c.tasks.execute(claim)
    assert result.execution_state.state == 'conflict'
    assert c.team.get_receipt(c.owner.id, receipt.acceptance_receipt.operation_id).current == c.baseline
    assert not writes(c)


def test_double_confirmation_is_idempotent_and_other_operation_conflicts(case):
    c = case
    p = preview(c)
    operation = uid()
    receipts = [confirm(c, p, operation_id=operation) for _ in range(2)]
    assert receipts[0] == receipts[1]
    with pytest.raises(TeamConflict, match='due_resolution_already_confirmed'):
        confirm(c, p)
    with c.team.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM team_due_resolution_consumptions').fetchone()[0] == 1


def test_simultaneous_confirmations_queue_exactly_once(case):
    c = case
    p = preview(c)
    def action(_):
        try:
            return confirm(c, p)
        except TeamConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(action, range(2)))
    assert sum(item is not None for item in results) == 1


def test_expired_preview_is_not_consumed(case):
    c = case
    p = preview(c)
    c.now[0] = p.expires_at
    with pytest.raises(TeamConflict, match='due_resolution_preview_expired'):
        confirm(c, p)
    assert not writes(c)


def test_generic_command_cannot_forge_resolution_or_bypass_ordinary_fresh(case):
    c = case
    p = preview(c)
    forged = TaskCommand(operation_id=uid(), project_id='7', task_id='55', expected_revision=0,
        expected_fingerprint=c.baseline.remote_fingerprint, action='resolve_due', resolution_id=p.preview_id,
        values={'due_at': p.due_at, 'due_confirmed': True, 'reason': p.reason})
    assert c.team.accept_command(c.owner.id, forged).acceptance_receipt.decision == 'rejected'
    ordinary = TaskCommand(operation_id=uid(), project_id='7', task_id='55', expected_revision=0,
        expected_fingerprint=p.candidate.observed_fingerprint, action='set_due',
        values={'due_at': p.due_at, 'due_confirmed': True, 'reason': p.reason})
    assert c.team.accept_command(c.owner.id, ordinary).execution_state.state == 'conflict'
    assert not writes(c)


@pytest.mark.parametrize('path', ['/tasks/55', '/tasks/55/comments'])
def test_unknown_write_never_replays_on_recovery(case, path):
    c = case
    p = preview(c)
    accepted = confirm(c, p)
    operation = accepted.acceptance_receipt.operation_id
    claim = c.team.claim_command('resolver')
    c.control.lost_ack = path
    result = c.tasks.execute(claim)
    assert result.execution_state.state == 'uncertain'
    original = list(writes(c))
    c.now[0] += timedelta(hours=1)
    recovery = c.team.claim_reconciliation(operation, 'recovery')
    recovered = c.tasks.execute(recovery)
    assert writes(c) == original
    assert recovered.execution_state.state == ('applied' if path.endswith('/comments') else 'uncertain')


def test_polling_busy_evidence_does_not_invalidate_already_confirmed_resolution(case):
    c = case
    p = preview(c)
    confirm(c, p)
    claim = c.team.claim_command('resolver')
    assert c.sync.sync_once().error_code == 'sync_task_busy'
    assert c.tasks.execute(claim).execution_state.state == 'applied'


def test_external_assignee_change_rejected_before_any_write(case):
    c = case
    c.remote['assignees'] = [{'id': 12}]
    c.sync.sync_once()
    with pytest.raises(TeamConflict, match='due_resolution_assignee_changed'):
        preview(c)
    assert not writes(c)


def test_external_clear_also_requires_owner_confirmation(case):
    c = case
    p = preview(c)
    confirm(c, p)
    assert c.tasks.execute(c.team.claim_command('resolver')).execution_state.state == 'applied'
    c.remote['due_date'] = ZERO_DATE
    assert c.sync.sync_once().error_code == 'remote_due_unconfirmed'
    candidate = c.service.candidate(c.owner.id, '55', expected_actor_revision=0)
    assert candidate.observed_due_at is None
    p = preview(c, due_at=None)
    confirm(c, p)
    assert c.tasks.execute(c.team.claim_command('resolver')).execution_state.state == 'applied'


def test_preview_evidence_is_immutable_even_for_replace(case):
    c = case
    p = preview(c)
    confirm(c, p)
    with sqlite3.connect(c.team.db.path) as conn:
        for table in ('team_due_resolution_previews', 'team_due_resolution_consumptions'):
            row = conn.execute(f'SELECT * FROM {table}').fetchone()
            for sql, arguments in ((f'DELETE FROM {table}', ()), (f'UPDATE {table} SET rowid=rowid', ()),
                (f'INSERT OR REPLACE INTO {table} VALUES({",".join("?" for _ in row)})', row)):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(sql, arguments)
                conn.rollback()


@pytest.mark.parametrize('values', [{'reason': 'Chosen'}, {'due_at': None, 'reason': ' '},
    {'due_at': '2026-10-05T10:00:00', 'reason': 'Chosen'}])
def test_explicit_date_or_clear_and_reason_are_required(case, values):
    c = case
    candidate = c.service.candidate(c.owner.id, '55', expected_actor_revision=0)
    with pytest.raises(ValidationError):
        c.domain.DueResolutionRequest(observation_id=candidate.observation_id,
            expected_revision=0, expected_fingerprint=candidate.observed_fingerprint, **values)


@pytest.mark.parametrize('stage', ['before_claim', 'after_claim', 'between_steps', 'final_read'])
def test_owner_revocation_fences_every_write_and_final_commit(case, stage):
    c = case
    p = preview(c)
    receipt = confirm(c, p)
    def revoke():
        c.team.upsert_member(c.owner.model_copy(update={'role': 'member', 'revision': 1}), expected_revision=0)
    if stage == 'before_claim':
        revoke()
        assert c.team.claim_command('resolver') is None
        assert not writes(c)
        return
    claim = c.team.claim_command('resolver')
    if stage == 'after_claim':
        revoke()
    elif stage == 'between_steps':
        c.control.after_write = revoke
    else:
        original = c.client.get_task
        def final_read(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            revoke()
            return snapshot
        c.client.get_task = final_read
    result = c.tasks.execute(claim)
    assert result.execution_state.state in {'rejected', 'uncertain'}
    assert result.current == c.baseline
    assert len(writes(c)) == {'after_claim': 0, 'between_steps': 1, 'final_read': 2}[stage]


def test_demoted_owner_cannot_finish_readonly_reconciliation(case):
    c = case
    p = preview(c)
    operation = confirm(c, p).acceptance_receipt.operation_id
    c.control.lost_ack = '/tasks/55/comments'
    assert c.tasks.execute(c.team.claim_command('resolver')).execution_state.state == 'uncertain'
    original = list(writes(c))
    c.team.upsert_member(c.owner.model_copy(update={'role': 'member', 'revision': 1}), expected_revision=0)
    recovery = c.team.claim_reconciliation(operation, 'recovery')
    result = c.tasks.execute(recovery)
    assert result.execution_state.state == 'uncertain' and result.current == c.baseline
    assert writes(c) == original


def test_baseline_revision_aba_invalidates_preview_even_with_same_remote_fingerprint(case):
    c = case
    p = preview(c)
    c.team.save_projection(c.baseline.model_copy(update={'revision': 1}), expected_revision=0)
    with pytest.raises(TeamConflict):
        confirm(c, p)
    assert not writes(c)
    with c.team.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_due_resolution_consumptions').fetchone()[0] == 0


def test_repeated_poll_same_facts_preserves_exact_bound_observation(case):
    c = case
    p = preview(c)
    c.sync.sync_once()
    latest = c.service.candidate(c.owner.id, '55', expected_actor_revision=0)
    assert latest.observation_id != p.candidate.observation_id
    assert latest.observed_fingerprint == p.candidate.observed_fingerprint
    assert confirm(c, p).execution_state.state == 'queued'


def test_observed_remote_aba_invalidates_old_preview(case):
    c = case
    p = preview(c)
    c.remote['due_date'] = '2026-10-07T10:00:00Z'
    c.sync.sync_once()
    c.remote['due_date'] = '2026-10-05T10:00:00Z'
    c.sync.sync_once()
    with pytest.raises(TeamConflict, match='due_resolution_candidate_changed'):
        confirm(c, p)
    assert not writes(c)
    assert confirm(c, preview(c)).execution_state.state == 'queued'


def test_rejected_acceptance_rolls_back_consumption_and_command(case, monkeypatch):
    c = case
    p = preview(c)
    original = c.team._permission
    def deny(conn, actor, command):
        if command.action == 'resolve_due':
            raise TeamForbidden('synthetic_acceptance_denial')
        return original(conn, actor, command)
    monkeypatch.setattr(c.team, '_permission', deny)
    with pytest.raises(TeamConflict, match='synthetic_acceptance_denial'):
        confirm(c, p)
    with c.team.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_due_resolution_consumptions').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 0
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_late_change_during_final_get_never_reports_applied(case):
    c = case
    p = preview(c)
    confirm(c, p)
    original = c.client.get_task
    def changed_final(*args, **kwargs):
        c.remote['title'] = 'Outside write before final GET'
        return original(*args, **kwargs)
    c.client.get_task = changed_final
    result = c.tasks.execute(c.team.claim_command('resolver'))
    assert result.execution_state.state == 'uncertain' and result.current == c.baseline
    assert result.execution_state.error_code == 'external_change_during_final_read'


def _downgrade_to_seven(c):
    with c.team.db.connection() as conn:
        conn.execute('DROP TRIGGER response_abort_guard')
        conn.execute('DROP TRIGGER abort_response_guard')
        conn.execute('DROP TABLE voice_request_aborts')
        conn.execute('DROP TABLE team_due_resolution_consumptions')
        conn.execute('DROP TABLE team_due_resolution_previews')
        conn.execute('DELETE FROM team_schema WHERE version>7')


def test_real_schema_seven_upgrade_preserves_old_receipts_and_payloads(case):
    c = case
    command = TaskCommand(operation_id=uid(), project_id='7', task_id='55', expected_revision=0,
        expected_fingerprint=c.baseline.remote_fingerprint, action='comment', values={'comment': 'Old evidence'})
    receipt = c.team.accept_command(c.owner.id, command)
    _downgrade_to_seven(c)
    database = TeamDatabase(c.team.db.path)
    TeamDatabase(c.team.db.path)
    reopened = TeamRepository(database, clock=lambda: c.now[0])
    assert reopened.get_receipt(c.owner.id, command.operation_id) == receipt
    with database.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 9
        assert conn.execute('SELECT COUNT(*) FROM team_due_resolution_previews').fetchone()[0] == 0
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_failed_schema_eight_upgrade_rolls_back_all_new_evidence(case, monkeypatch):
    c = case
    _downgrade_to_seven(c)
    with c.team.db.connection() as conn:
        original_projection = conn.execute('SELECT payload FROM team_projections WHERE task_id=?', ('55',)).fetchone()[0]
    monkeypatch.setattr(c.storage, 'DUE_RESOLUTION_MIGRATION', c.storage.DUE_RESOLUTION_MIGRATION + ('INVALID SQL',))
    with pytest.raises(sqlite3.OperationalError):
        TeamDatabase(c.team.db.path)
    with c.team.db.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 7
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'team_due_resolution_%'").fetchall() == []
        assert conn.execute('SELECT payload FROM team_projections WHERE task_id=?', ('55',)).fetchone()[0] == original_projection
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.fixture
def gateway(case, monkeypatch):
    import http.cookiejar
    import time as stdlib_time
    from fastapi.testclient import TestClient
    from secretary.infrastructure.team_auth_repository import AuthRepository
    from secretary.interface.team_gateway import create_team_app, TeamGatewayClients, TeamGatewaySettings
    c = case
    monkeypatch.setattr(http.cookiejar, 'time', SimpleNamespace(
        time=lambda: c.now[0].timestamp(), localtime=stdlib_time.localtime))
    auth = AuthRepository(c.team.db, secret=b'SYNTHETIC_DUE_RESOLUTION_SECRET_32', clock=lambda: c.now[0])
    settings = TeamGatewaySettings(public_origin='https://team.example', bot_id='42', bot_token='SYNTHETIC')
    app = create_team_app(settings, c.team, TeamGatewayClients(auth=auth, due_resolution=c.service))
    with TestClient(app, base_url=settings.public_origin, client=('203.0.113.9', 53123)) as client:
        def login(member=None):
            code = auth.issue_code((member or c.owner).max_user_id)
            response = client.post('/api/team/v1/session/code', json={'value': code.value},
                                   headers={'Origin': settings.public_origin})
            assert response.status_code == 200, response.text
            return {'Origin': settings.public_origin, 'X-CSRF-Token': response.json()['csrf']}
        yield SimpleNamespace(client=client, login=login, case=c)


def test_public_contract_creates_immutable_preview_then_queues_confirmation(gateway):
    g, c = gateway, gateway.case
    headers = g.login()
    candidate = g.client.get('/api/team/v1/tasks/55/due-resolution')
    assert candidate.status_code == 200, candidate.text
    body = candidate.json()
    assert set(body) == {'observation_id', 'project_id', 'task_id', 'baseline_revision', 'baseline_fingerprint',
                         'observed_fingerprint', 'baseline_due_at', 'observed_due_at', 'title'}
    response = g.client.post('/api/team/v1/tasks/55/due-resolution/previews', headers=headers,
        json={'observation_id': body['observation_id'], 'expected_revision': body['baseline_revision'],
              'expected_fingerprint': body['observed_fingerprint'], 'due_at': None, 'reason': 'Explicit no deadline'})
    assert response.status_code == 201, response.text
    assert 'before_image' not in response.text and 'max_user_id' not in response.text
    preview = response.json()
    operation = uid()
    accepted = g.client.post(f'/api/team/v1/due-resolutions/{preview["preview_id"]}/confirm', headers=headers,
                            json={'operation_id': operation})
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()['execution_state']['state'] == 'queued'
    assert not writes(c)
    assert c.tasks.execute(c.team.claim_command('resolver')).execution_state.state == 'applied'
    result = g.client.get(f'/api/team/v1/commands/{operation}')
    assert result.json()['execution_state']['state'] == 'applied'


def test_public_resolution_requires_owner_csrf_exact_fields_and_dedicated_confirm(gateway):
    g, c = gateway, gateway.case
    member_headers = g.login(c.member)
    assert g.client.get('/api/team/v1/tasks/55/due-resolution').status_code == 403
    assert g.client.post('/api/team/v1/tasks/55/due-resolution/previews', headers=member_headers,
        json={'observation_id': uid(), 'expected_revision': 0, 'expected_fingerprint': 'a' * 64,
              'due_at': None, 'reason': 'Chosen'}).status_code == 403
    headers = g.login()
    candidate = g.client.get('/api/team/v1/tasks/55/due-resolution').json()
    body = {'observation_id': candidate['observation_id'], 'expected_revision': 0,
            'expected_fingerprint': candidate['observed_fingerprint'], 'due_at': None, 'reason': 'Chosen'}
    assert g.client.post('/api/team/v1/tasks/55/due-resolution/previews', json=body).status_code == 403
    assert g.client.post('/api/team/v1/tasks/55/due-resolution/previews', headers=headers,
                         json={**body, 'unexpected': 'SENSITIVE_DO_NOT_ECHO'}).status_code == 422
    forged = TaskCommand(operation_id=uid(), project_id='7', task_id='55', expected_revision=0,
        expected_fingerprint=c.baseline.remote_fingerprint, action='resolve_due', resolution_id=uid(),
        values={'due_at': None, 'due_confirmed': True, 'reason': 'Forged'}).model_dump(mode='json', exclude_unset=True)
    rejected = g.client.post('/api/team/v1/commands', headers=headers, json=forged)
    assert rejected.status_code == 422 and rejected.json()['error_code'] == 'team_due_resolution_confirmation_required'
    assert not writes(c)


@pytest.mark.parametrize('method,path', [('GET', '/api/team/v1/tasks/55/due-resolution/previews'),
    ('POST', '/api/team/v1/tasks/55/due-resolution'), ('GET', '/api/team/v1/due-resolutions/00000000-0000-0000-0000-000000000000/confirm'),
    ('GET', '/api/team/v1/tasks/55/%64ue-resolution')])
def test_resolution_route_methods_and_encoded_paths_are_denied(gateway, method, path):
    g = gateway
    headers = g.login()
    assert g.client.request(method, path, headers=headers).status_code == 404


def test_candidate_and_confirmation_recheck_owner_acl_after_remote_get(case):
    c = case
    def revoke():
        c.team.upsert_member(c.owner.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    c.control.after_read = revoke
    with pytest.raises(TeamForbidden):
        c.service.candidate(c.owner.id, '55', expected_actor_revision=0)
    assert not writes(c)


def test_bound_first_before_image_cannot_be_forged_by_worker(case):
    c = case
    p = preview(c)
    confirm(c, p)
    claim = c.team.claim_command('resolver')
    c.team.save_remote_plan(claim, [{'kind': 'patch', 'fields': {'due_date': ZERO_DATE}}])
    with pytest.raises(TeamConflict, match='due_resolution_before_image_invalid'):
        c.team.begin_remote_step(claim, 0, 'a' * 64, p.candidate.observed_fingerprint,
                                 before_image={'forged': 'No original remote evidence'})
    assert not writes(c)


def test_foreign_task_observation_and_another_owner_preview_are_not_capabilities(case):
    from secretary.infrastructure.team_sync_repository import SyncObservation
    c = case
    other = TeamMember(id=uid(), role='owner', display_name='Other owner', max_user_id='15',
                       vikunja_user_id='16', project_ids=('7', '8'))
    c.team.upsert_member(other, expected_revision=None)
    foreign = TaskSnapshot(task_id='77', project_id='8', revision=0, remote_fingerprint='a' * 64,
                           title='SENSITIVE_FOREIGN_TASK')
    c.team.save_projection(foreign, expected_revision=None)
    sync = TeamSyncRepository(c.team, clock=lambda: c.now[0])
    run = sync.begin('8')
    sync.commit(run, (SyncObservation('77', 'b' * 64, (), 'remote_due_unconfirmed'),), (), ('77',))
    with c.team.db.connection() as conn:
        observation = conn.execute('SELECT id FROM team_sync_observations WHERE task_id=?', ('77',)).fetchone()[0]
    with pytest.raises(TeamForbidden):
        c.service.candidate(c.owner.id, '77', expected_actor_revision=0)
    request = c.domain.DueResolutionRequest(observation_id=observation, expected_revision=0,
        expected_fingerprint='b' * 64, due_at=None, reason='Forged cross project')
    with pytest.raises(TeamConflict):
        c.service.preview(c.owner.id, '55', request, expected_actor_revision=0)
    p = preview(c)
    with pytest.raises(TeamForbidden):
        confirm(c, p, actor=other)
    assert not writes(c)


def test_owner_dashboard_reads_queued_and_uncertain_resolution_without_false_delivery(case):
    from secretary.infrastructure.team_dashboard_repository import TeamDashboardRepository
    c = case
    p = preview(c)
    confirm(c, p)
    dashboard = TeamDashboardRepository(c.team)
    queued = dashboard.dashboard(c.owner.id, '7', expected_actor_revision=0)
    assert len(queued.commands.items) == 1 and queued.commands.items[0].action == 'resolve_due'
    assert queued.commands.items[0].state == 'queued'
    c.control.lost_ack = '/tasks/55'
    c.tasks.execute(c.team.claim_command('resolver'))
    uncertain = dashboard.dashboard(c.owner.id, '7', expected_actor_revision=0)
    assert uncertain.commands.items[0].state == 'uncertain'


def test_public_unconfigured_port_and_remote_error_are_explicit_sanitized(gateway):
    from fastapi.testclient import TestClient
    from secretary.interface.team_gateway import create_team_app, TeamGatewayClients, TeamGatewaySettings
    from secretary.infrastructure.team_auth_repository import AuthRepository
    from secretary.infrastructure.vikunja import VikunjaError
    g, c = gateway, gateway.case
    auth = AuthRepository(c.team.db, secret=b'SYNTHETIC_DUE_RESOLUTION_SECRET_32', clock=lambda: c.now[0])
    settings = TeamGatewaySettings(public_origin='https://team.example', bot_id='42', bot_token='SYNTHETIC')
    app = create_team_app(settings, c.team, TeamGatewayClients(auth=auth))
    with TestClient(app, base_url=settings.public_origin, client=('203.0.113.10', 53124)) as client:
        code = auth.issue_code(c.owner.max_user_id)
        assert client.post('/api/team/v1/session/code', json={'value': code.value},
                           headers={'Origin': settings.public_origin}).status_code == 200
        response = client.get('/api/team/v1/tasks/55/due-resolution')
        assert response.status_code == 503 and response.json()['error_code'] == 'team_due_resolution_unavailable'
    g.login()
    def fail(*args, **kwargs):
        raise VikunjaError('SENSITIVE_REMOTE_ERROR_BODY')
    c.client.validate_binding = fail
    response = g.client.get('/api/team/v1/tasks/55/due-resolution')
    assert response.status_code == 503
    assert response.json()['error_code'] == 'team_due_resolution_remote_unavailable'
    assert 'SENSITIVE' not in response.text
