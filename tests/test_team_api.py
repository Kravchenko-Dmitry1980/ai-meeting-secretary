"""T6 isolated public Gateway contract; no owner state or providers."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import importlib
import importlib.util
import hashlib
import hmac
import json
import http.cookiejar
import time as stdlib_time
from urllib.parse import quote
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from secretary.domain.team import TaskCommand, TaskSnapshot, TeamMember
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository


def ident():
    return str(uuid4())


@pytest.fixture
def case(tmp_path, monkeypatch):
    name = 'secretary.interface.team_gateway'
    assert importlib.util.find_spec(name), 'T6 isolated public Gateway missing'
    gateway = importlib.import_module(name)
    auth_module = importlib.import_module('secretary.infrastructure.team_auth_repository')
    clock = [datetime(2026, 10, 3, 12, tzinfo=timezone.utc)]
    # The browser's cookie clock follows the same mutable server clock. Keep
    # expiration enabled; never change global time or asynchronous durations.
    monkeypatch.setattr(http.cookiejar, 'time', SimpleNamespace(
        time=lambda: clock[0].timestamp(), localtime=stdlib_time.localtime))
    db = TeamDatabase(tmp_path / 'team.sqlite3')
    repo = TeamRepository(db, clock=lambda: clock[0])
    owner = TeamMember(id=ident(), display_name='Owner', role='owner', max_user_id='11',
                       vikunja_user_id='21', project_ids=('7',))
    member = TeamMember(id=ident(), display_name='Member', max_user_id='12', vikunja_user_id='22', project_ids=('7',))
    foreign = TeamMember(id=ident(), display_name='Other', max_user_id='13', vikunja_user_id='23', project_ids=('8',))
    for person in (owner, member, foreign):
        repo.upsert_member(person, expected_revision=None)
    snapshot = TaskSnapshot(task_id='9007199254740997', project_id='7', revision=0,
        remote_fingerprint='a' * 64, title='Synthetic', assignee_id=member.id,
        important=False, urgent=False, classification_confirmed=True)
    repo.save_projection(snapshot, expected_revision=None)
    auth = auth_module.AuthRepository(db, secret=b'synthetic-secret-for-tests-32bytes', clock=lambda: clock[0])
    settings = gateway.TeamGatewaySettings(public_origin='https://team.example', bot_id='42', bot_token='synthetic-token')
    app = gateway.create_team_app(settings, repo, gateway.TeamGatewayClients(auth=auth))
    with TestClient(app, base_url='https://team.example', client=('203.0.113.8', 43123)) as client:
        yield SimpleNamespace(g=gateway, auth=auth, repo=repo, db=db, owner=owner, member=member,
            foreign=foreign, task=snapshot, client=client, clock=clock, settings=settings)


def login(c, member=None):
    code = c.auth.issue_code((member or c.member).max_user_id)
    response = c.client.post('/api/team/v1/session/code', json={'value': code.value}, headers={'Origin': c.settings.public_origin})
    assert response.status_code == 200, response.text
    return {'Origin': c.settings.public_origin, 'X-CSRF-Token': response.json()['csrf']}


def command(c, **edits):
    payload = TaskCommand(operation_id=ident(), project_id='7', task_id=c.task.task_id,
        expected_revision=0, expected_fingerprint='a' * 64, action='set_state', values={'bucket': 'doing'}).model_dump(mode='json', exclude_unset=True)
    payload.update(edits)
    return payload


def test_opaque_cookie_and_exact_actor_contract(case):
    c = case
    code = c.auth.issue_code(c.member.max_user_id)
    response = c.client.post('/api/team/v1/session/code', json={'value': code.value}, headers={'Origin': c.settings.public_origin})
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    for attribute in ('__Host-secretary-team=', 'HttpOnly', 'Secure', 'SameSite=lax', 'Path=/'):
        assert attribute in cookie
    assert 'Domain=' not in cookie
    assert 'token' not in response.json()
    me = c.client.get('/api/team/v1/me')
    assert me.status_code == 200
    assert me.json()['actor']['id'] == c.member.id
    assert set(me.json()['actor']) == {'id', 'display_name', 'role', 'project_ids', 'revision'}
    assert me.json()['csrf'] == response.json()['csrf']
    assert me.headers['cache-control'] == 'no-store'


def test_browser_cookie_and_server_session_expire_at_the_same_twelve_hour_boundary(case):
    from secretary.domain.team_auth import AuthError
    c = case
    code = c.auth.issue_code(c.member.max_user_id)
    response = c.client.post('/api/team/v1/session/code', json={'value': code.value},
        headers={'Origin': c.settings.public_origin})
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    for attribute in ('Secure', 'HttpOnly', 'SameSite=lax', 'Path=/', 'expires='):
        assert attribute.lower() in cookie.lower()
    token = response.cookies.get('__Host-secretary-team')
    started = c.clock[0]
    # Keep the documented 30-minute idle window active while exercising the
    # independent absolute 12-hour expiry. These are genuine authenticated reads.
    for step in range(1, 25):
        c.clock[0] = started + timedelta(minutes=29 * step)
        assert c.client.get('/api/team/v1/me').status_code == 200
    c.clock[0] = started + timedelta(hours=12, seconds=-1)
    before = c.client.get('/api/team/v1/me')
    assert before.status_code == 200 and 'cookie' in before.request.headers
    assert c.auth.authenticate(token).id == c.member.id
    c.clock[0] = started + timedelta(hours=12)
    expired = c.client.get('/api/team/v1/me')
    assert expired.status_code == 401 and 'cookie' not in expired.request.headers
    assert expired.json() == {'error_code': 'team_authentication_required'}
    with pytest.raises(AuthError) as caught:
        c.auth.authenticate(token)
    assert caught.value.code == 'auth_invalid_credentials'


def test_read_requires_session_and_preserves_large_string_ids(case):
    c = case
    assert c.client.get('/api/team/v1/tasks', params={'project_id': '7'}).status_code == 401
    login(c)
    data = c.client.get('/api/team/v1/tasks', params={'project_id': '7'}).json()
    assert data['items'][0]['task_id'] == '9007199254740997'
    assert c.client.get('/api/team/v1/tasks/' + c.task.task_id).json()['task_id'] == c.task.task_id


def test_member_cannot_assign_others_or_raise_budget(case):
    c = case
    headers = login(c)
    response = c.client.post('/api/team/v1/commands', json=command(c, action='assign',
        expected_assignee_revision=0, values={'assignee_id': c.owner.id}), headers=headers)
    assert response.status_code == 202
    assert response.json()['acceptance_receipt']['decision'] == 'rejected'
    assert response.json()['current'] is None
    assert c.client.post('/api/team/v1/cloud-budget', json={'limit': 100000}, headers=headers).status_code == 404


def test_every_write_requires_scope_and_operation_id(case):
    c = case
    headers = login(c)
    for omitted in ('operation_id', 'project_id', 'expected_revision', 'expected_fingerprint'):
        payload = command(c)
        del payload[omitted]
        assert c.client.post('/api/team/v1/commands', json=payload, headers=headers).status_code == 422
    payload = command(c)
    queued = c.client.post('/api/team/v1/commands', json=payload, headers=headers)
    assert queued.status_code == 202
    assert queued.json()['execution_state']['state'] == 'queued'
    assert c.client.post('/api/team/v1/commands', json=payload, headers=headers).json() == queued.json()
    changed = {**payload, 'values': {'bucket': 'blocked'}}
    assert c.client.post('/api/team/v1/commands', json=changed, headers=headers).status_code == 409
    assert c.client.get('/api/team/v1/commands/' + payload['operation_id']).json() == queued.json()
    assert c.repo.claim_command('worker') is not None
    assert c.repo.claim_command('other') is None


@pytest.mark.parametrize('extra', [{'actor_id': '00000000-0000-4000-8000-000000000001'}, {'budget': 99999},
    {'origin': {'source_kind': 'max'}}, {'action': 'link', 'values': {}}])
def test_client_cannot_forge_identity_or_trusted_origin(case, extra):
    c = case
    headers = login(c, c.owner)
    payload = command(c, **extra)
    result = c.client.post('/api/team/v1/commands', json=payload, headers=headers)
    assert result.status_code == 422


def test_cross_project_scope_and_receipt_do_not_leak(case):
    c = case
    headers = login(c, c.owner)
    payload = command(c)
    c.client.post('/api/team/v1/commands', json=payload, headers=headers)
    login(c, c.foreign)
    for path in ('/api/team/v1/tasks/' + c.task.task_id, '/api/team/v1/commands/' + payload['operation_id']):
        response = c.client.get(path)
        assert response.status_code == 403
        assert c.task.title not in response.text and c.owner.id not in response.text
    assert c.client.get('/api/team/v1/tasks', params={'project_id': '7'}).status_code == 403


@pytest.mark.parametrize('headers', [{}, {'Origin': 'https://evil.example'}, {'Origin': 'null'}, {'Origin': 'https://team.example'}])
def test_command_requires_matching_origin_and_csrf(case, headers):
    c = case
    login(c)
    assert c.client.post('/api/team/v1/commands', json=command(c), headers=headers).status_code == 403
    assert c.repo.claim_command('worker') is None


def test_login_origin_and_json_duplicates_and_size_fail_before_code_consumption(case):
    c = case
    code = c.auth.issue_code(c.member.max_user_id)
    assert c.client.post('/api/team/v1/session/code', json={'value': code.value}).status_code == 403
    bad = '{"value":"' + code.value + '","value":"different"}'
    assert c.client.post('/api/team/v1/session/code', content=bad,
        headers={'Origin': c.settings.public_origin, 'Content-Type': 'application/json'}).status_code == 400
    assert c.client.post('/api/team/v1/session/code', content=b'x' * (256 * 1024 + 1),
        headers={'Origin': c.settings.public_origin}).status_code == 413
    assert c.client.post('/api/team/v1/session/code', json={'value': code.value},
        headers={'Origin': c.settings.public_origin}).status_code == 200


def test_logout_revokes_server_session_and_requires_csrf(case):
    c = case
    headers = login(c)
    assert c.client.delete('/api/team/v1/session', headers={'Origin': c.settings.public_origin}).status_code == 403
    assert c.client.get('/api/team/v1/me').status_code == 200
    token = c.client.cookies.get('__Host-secretary-team')
    assert c.client.delete('/api/team/v1/session', headers=headers).status_code == 204
    c.client.cookies.set('__Host-secretary-team', token)
    assert c.client.get('/api/team/v1/me').status_code == 401


def test_revoked_binding_denies_every_request(case):
    c = case
    headers = login(c)
    c.auth.revoke_member(c.owner.id, c.member.id, expected_revision=0)
    assert c.client.get('/api/team/v1/me').status_code == 401
    assert c.client.post('/api/team/v1/commands', json=command(c), headers=headers).status_code == 401
    assert c.repo.claim_command('worker') is None


def test_auth_revision_is_rechecked_in_accept_transaction(case):
    c = case
    headers = login(c, c.owner)
    authenticate = c.auth.authenticate
    changed = [False]
    def raced(*args, **kwargs):
        actor = authenticate(*args, **kwargs)
        if kwargs.get('require_csrf') and not changed[0]:
            c.repo.upsert_member(c.owner.model_copy(update={'revision': 1}), expected_revision=0)
            changed[0] = True
        return actor
    c.auth.authenticate = raced
    response = c.client.post('/api/team/v1/commands', json=command(c), headers=headers)
    assert response.status_code == 403
    assert c.repo.claim_command('worker') is None


@pytest.mark.parametrize('resource', ['list', 'task', 'receipt'])
def test_auth_revision_is_rechecked_before_task_or_receipt_read(case, resource):
    c = case
    headers = login(c, c.owner)
    payload = command(c)
    assert c.client.post('/api/team/v1/commands', json=payload, headers=headers).status_code == 202
    authenticate = c.auth.authenticate
    changed = [False]
    def raced(*args, **kwargs):
        actor = authenticate(*args, **kwargs)
        if not changed[0]:
            c.repo.upsert_member(c.owner.model_copy(update={'revision': 1}), expected_revision=0)
            changed[0] = True
        return actor
    c.auth.authenticate = raced
    paths = {'list': '/api/team/v1/tasks?project_id=7', 'task': '/api/team/v1/tasks/' + c.task.task_id,
        'receipt': '/api/team/v1/commands/' + payload['operation_id']}
    response = c.client.get(paths[resource])
    assert response.status_code == 403
    assert c.task.title not in response.text


def test_public_assignment_requires_member_revision_and_owner_scope(case):
    c = case
    headers = login(c, c.owner)
    missing = command(c, action='assign', values={'assignee_id': c.member.id})
    assert c.client.post('/api/team/v1/commands', json=missing, headers=headers).status_code == 422
    stale = {**missing, 'expected_assignee_revision': 1}
    response = c.client.post('/api/team/v1/commands', json=stale, headers=headers)
    assert response.status_code == 202 and response.json()['execution_state']['state'] == 'conflict'
    assert c.repo.claim_command('worker') is None


@pytest.mark.parametrize('path', ['/config', '/session', '/api/v1/config', '/api/v1/session',
    '/api/v1/meetings/x/audio', '/internal/v1/task-publications', '/%69nternal/v1/task-publications',
    '/api/team/v1/../../internal/v1/task-publications', '/docs', '/openapi.json'])
@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_public_gateway_has_no_local_admin_routes(case, path, method):
    c = case
    response = c.client.request(method, path, headers={'Origin': c.settings.public_origin,
        'X-Secretary-Service-Secret': 'synthetic', 'X-Forwarded-For': '127.0.0.1'})
    assert response.status_code == 404
    assert 'sqlite' not in response.text


def test_public_host_and_cookie_ambiguity_rejected(case):
    c = case
    headers = login(c)
    assert c.client.get('/api/team/v1/me', headers={'Host': 'evil.example', 'X-Forwarded-Host': 'team.example'}).status_code == 403
    token = c.client.cookies.get('__Host-secretary-team')
    assert c.client.get('/api/team/v1/me', headers={'Cookie': f'__Host-secretary-team={token}; __Host-secretary-team={token}'}).status_code == 401
    assert c.client.post('/api/team/v1/commands', json=command(c), headers=list(headers.items()) + [('X-CSRF-Token', headers['X-CSRF-Token'])]).status_code == 403


def test_pagination_exact_ids_stable_order_and_mine_filter(case):
    c = case
    login(c)
    for value in ('1', '2', '10', '100'):
        task = c.task.model_copy(update={'task_id': value, 'assignee_id': c.owner.id if value == '10' else c.member.id})
        c.repo.save_projection(task, expected_revision=None)
    cursor = None
    ids = []
    while True:
        params = {'project_id': '7', 'limit': 2}
        if cursor:
            params['after'] = cursor
        response = c.client.get('/api/team/v1/tasks', params=params)
        assert response.status_code == 200
        page = response.json()
        ids.extend(task['task_id'] for task in page['items'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert ids == ['1', '2', '10', '100', c.task.task_id]
    mine = c.client.get('/api/team/v1/tasks', params={'project_id': '7', 'mine': True}).json()
    assert '10' not in [task['task_id'] for task in mine['items']]
    assert c.client.get('/api/team/v1/tasks', params={'project_id': '7', 'limit': 101}).status_code == 422


def test_gateway_launch_config_ignores_forwarding_headers(case):
    config = case.g.gateway_server_config(case.client.app)
    assert config.proxy_headers is False and config.host == '127.0.0.1'
    with pytest.raises(ValueError):
        case.g.gateway_server_config(case.client.app, host='0.0.0.0')


def test_real_max_login_uses_pinned_vector_and_canonical_replay(case):
    c = case
    vector = json.loads((Path(__file__).resolve().parents[1] / 'docs/contracts/max-contract.json').read_text(encoding='utf-8'))['fixtures']['init_data_vector']
    stamp = int(next(value[10:] for value in vector['launch_params'].split('\n') if value.startswith('auth_date=')))
    c.clock[0] = datetime.fromtimestamp(stamp, timezone.utc)
    c.repo.upsert_member(c.owner.model_copy(update={'revision': 1, 'max_user_id': '9007199254740993'}), expected_revision=0)
    # The pinned vector's synthetic token is the configuration, not a credential.
    app = c.g.create_team_app(c.g.TeamGatewaySettings(public_origin=c.settings.public_origin,
        bot_id='42', bot_token=vector['synthetic_token']), c.repo, c.g.TeamGatewayClients(auth=c.auth))
    with TestClient(app, base_url=c.settings.public_origin, client=('203.0.113.8', 43123)) as client:
        headers = {'Origin': c.settings.public_origin}
        response = client.post('/api/team/v1/session/max', json={'init_data': vector['init_data']}, headers=headers)
        assert response.status_code == 200
        assert response.json()['actor']['id'] == c.owner.id
        for raw in ('&'.join(reversed(vector['init_data'].split('&'))), vector['init_data'].replace('%7B', '%7b')):
            replay = client.post('/api/team/v1/session/max', json={'init_data': raw}, headers=headers)
            assert replay.status_code == 401
            assert '9007199254740993' not in replay.text


def test_invalid_max_signature_unknown_user_and_actor_field_do_not_login(case):
    c = case
    headers = {'Origin': c.settings.public_origin}
    values = {'auth_date': str(int(c.clock[0].timestamp())), 'user': '{"id":99}'}
    params = '\n'.join(key + '=' + values[key] for key in sorted(values))
    secret = hmac.new(b'WebAppData', b'synthetic-token', hashlib.sha256).digest()
    values['hash'] = hmac.new(secret, params.encode(), hashlib.sha256).hexdigest()
    raw = '&'.join(key + '=' + quote(value) for key, value in values.items())
    for body in ({'init_data': raw}, {'init_data': raw[:-1] + ('0' if raw[-1] != '0' else '1')},
                 {'init_data': raw, 'actor_id': c.owner.id}):
        response = c.client.post('/api/team/v1/session/max', json=body, headers=headers)
        assert response.status_code in {401, 422}
        assert raw not in response.text and c.owner.id not in response.text
        assert 'set-cookie' not in response.headers


def test_login_rate_uses_actual_peer_and_not_spoofed_forwarding_header(case):
    c = case
    for count in range(20):
        response = c.client.post('/api/team/v1/session/code', json={'value': 'invalid'},
            headers={'Origin': c.settings.public_origin, 'X-Forwarded-For': f'198.51.100.{count + 1}'})
        assert response.status_code == 401
    response = c.client.post('/api/team/v1/session/code', json={'value': 'invalid'},
        headers={'Origin': c.settings.public_origin, 'X-Forwarded-For': '198.51.100.99'})
    assert response.status_code == 429


def test_public_openapi_has_only_team_contracts_and_authenticated_max_hook(case):
    c = case
    schema = c.client.app.openapi()
    assert set(schema['paths']) == {'/api/team/v1/session/max', '/api/team/v1/session/code',
        '/api/team/v1/session', '/api/team/v1/me', '/api/team/v1/tasks', '/api/team/v1/tasks/{task_id}',
        '/api/team/v1/commands', '/api/team/v1/commands/{operation_id}', '/hooks/max',
        '/api/team/v1/members', '/api/team/v1/status', '/api/team/v1/tasks/{task_id}/history', '/api/team/v1/dashboard',
        '/api/team/v1/tasks/{task_id}/due-resolution', '/api/team/v1/tasks/{task_id}/due-resolution/previews',
        '/api/team/v1/due-resolutions/{preview_id}/confirm'}
    for path in ('/api/team/v1/members', '/api/team/v1/status', '/api/team/v1/tasks/{task_id}/history', '/api/team/v1/dashboard'):
        assert set(schema['paths'][path]) == {'get'}
        assert schema['paths'][path]['get']['security'] == [{'APIKeyCookie': []}]
    assert set(schema['paths']['/hooks/max']) == {'post'}
    assert schema['paths']['/hooks/max']['post']['security'] == [{'APIKeyHeader': []}]
    assert 'Polza' not in json.dumps(schema) and 'enrollment' not in json.dumps(schema)


def test_unavailable_auth_is_not_an_expired_login(case):
    from secretary.domain.team_auth import AuthError
    c = case
    login(c)
    def unavailable(*args, **kwargs):
        raise AuthError('auth_unavailable')
    c.auth.authenticate = unavailable
    response = c.client.get('/api/team/v1/me')
    assert response.status_code == 503
    assert response.json() == {'error_code': 'team_auth_unavailable'}


def test_storage_failure_is_sanitized_and_not_fabricated_acceptance(case):
    import sqlite3
    c = case
    headers = login(c)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic private path and payload')
    c.repo.accept_command = unavailable
    response = c.client.post('/api/team/v1/commands', json=command(c), headers=headers)
    assert response.status_code == 503
    assert response.json() == {'error_code': 'team_storage_unavailable'}
    assert c.repo.claim_command('worker') is None


def test_corrupt_projection_is_unavailable_and_does_not_echo_data(case):
    c = case
    login(c)
    payload = {**c.task.model_dump(mode='json'), 'revision': 'private invalid revision'}
    with c.db.transaction() as conn:
        conn.execute('UPDATE team_projections SET payload=? WHERE task_id=?', (json.dumps(payload), c.task.task_id))
    response = c.client.get('/api/team/v1/tasks/' + c.task.task_id)
    assert response.status_code == 503
    assert response.json() == {'error_code': 'team_storage_unavailable'}


def test_public_origin_requires_exact_https_origin():
    from secretary.interface.team_gateway import TeamGatewaySettings
    for origin in ('http://team.example', 'https://team.example/path', 'https://user:pass@team.example', 'https://team.example/?x=1'):
        with pytest.raises(ValueError):
            TeamGatewaySettings(public_origin=origin, bot_id='42', bot_token='synthetic')


def test_https_default_port_and_host_case_use_browser_canonical_origin(case):
    c = case
    settings = c.g.TeamGatewaySettings(public_origin='https://TEAM.EXAMPLE:443', bot_id='42', bot_token='synthetic')
    app = c.g.create_team_app(settings, c.repo, c.g.TeamGatewayClients(auth=c.auth))
    code = c.auth.issue_code(c.member.max_user_id)
    with TestClient(app, base_url='https://team.example', client=('203.0.113.8', 43123)) as client:
        response = client.post('/api/team/v1/session/code', json={'value': code.value}, headers={'Origin': 'https://team.example'})
        assert response.status_code == 200
        assert settings.public_origin == 'https://team.example'


@pytest.mark.parametrize('origin', ['https://team.example:', 'https://team.example\\other', 'https://team.%65xample'])
def test_malformed_https_authority_is_configuration_error(origin):
    from secretary.interface.team_gateway import TeamGatewaySettings
    with pytest.raises(ValueError):
        TeamGatewaySettings(public_origin=origin, bot_id='42', bot_token='synthetic')
