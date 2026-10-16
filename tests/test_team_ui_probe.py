"""T9: real disposable Gateway/auth/repository, synthetic provider only."""
from dataclasses import replace
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import asyncio
import json
import sys

from fastapi.testclient import TestClient
import pytest


def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/team/probe_team_ui.py'
    assert path.is_file(), 'T9 browser fixture is absent'
    spec = util.spec_from_file_location('t9_ui_probe', path)
    value = util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def case(tmp_path):
    m = module()
    run = tmp_path / ('t9-ui-' + uuid4().hex)
    run.mkdir()
    dist = tmp_path / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'team.html').write_text('<html>synthetic Team <script src="./assets/team.js"></script></html>', encoding='utf-8')
    (dist / 'index.html').write_text('SECRETARY_ENTRY_MUST_NOT_BE_SERVED', encoding='utf-8')
    (dist / 'assets/team.js').write_text('import "./shared.js"; /* synthetic built team */', encoding='utf-8')
    (dist / 'assets/shared.js').write_text('/* required shared chunk */', encoding='utf-8')
    (dist / 'assets/secretary.js').write_text('/* unrelated Secretary entry */', encoding='utf-8')
    fixture = m.build_fixture(run, frontend_dist=dist, origin='https://secretary-t9.localhost:44443')
    with TestClient(fixture.app, base_url=fixture.origin, client=('127.0.0.1', 45001)) as client:
        yield m, fixture, client, dist
    fixture.close()


def login(fixture, client, index=0):
    response = client.post('/api/team/v1/session/code', json={'value': fixture.codes[index]},
                           headers={'Origin': fixture.origin})
    assert response.status_code == 200, response.text
    return {'Origin': fixture.origin, 'X-CSRF-Token': response.json()['csrf']}


def test_real_gateway_cookie_scope_csrf_and_synthetic_factory(case):
    m, f, client, _ = case
    assert len(f.owners) == 3 and len(f.project_ids) == 2
    assert all(person.role == 'owner' for person in f.owners)
    assert f.db.path.parent == f.run_root
    assert client.get('/api/team/v1/tasks', params={'project_id': f.project_ids[0]}).status_code == 401
    code = f.codes[0]
    response = client.post('/api/team/v1/session/code', json={'value': code}, headers={'Origin': f.origin})
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert '__Host-secretary-team=' in cookie and 'Secure' in cookie and 'HttpOnly' in cookie
    assert client.post('/api/team/v1/session/code', json={'value': code}, headers={'Origin': f.origin}).status_code == 401
    assert client.get('/api/team/v1/me').json()['actor']['id'] == f.owners[0].id
    assert client.delete('/api/team/v1/session', headers={'Origin': f.origin}).status_code == 403
    assert client.get('/api/team/v1/me', headers={'Host': 'localhost:44443'}).status_code == 403


def test_scenarios_and_numeric_ids_survive_real_pagination(case):
    _, f, client, _ = case
    login(f, client)
    rows, cursor = [], None
    while True:
        params = {'project_id': f.project_ids[0], 'limit': 10}
        if cursor:
            params['after'] = cursor
        page = client.get('/api/team/v1/tasks', params=params).json()
        rows.extend(page['items'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert len(rows) > 50 and len({row['task_id'] for row in rows}) == len(rows)
    assert all(isinstance(row['task_id'], str) and int(row['task_id']) > 2**53 for row in rows)
    assert any(row['due_at'] for row in rows) and any(row['due_at'] is None for row in rows)
    assert any(row['important'] for row in rows) and any(not row['classification_confirmed'] for row in rows)
    assert any(len(row['title']) > 300 for row in rows)
    assert {row['bucket'] for row in rows} == set(f.states)
    login(f, client, 2)
    assert client.get('/api/team/v1/tasks', params={'project_id': f.project_ids[0]}).status_code == 403


def test_only_fixture_team_entry_and_assets_are_served(case):
    _, f, client, _ = case
    assert 'СИНТЕТИЧЕСКИЙ' in client.get('/fixture').text
    assert f.codes[0] in client.get('/fixture').text
    assert client.get('/team/').status_code == 200
    assert client.get('/team/assets/team.js').status_code == 200
    assert client.get('/team/assets/shared.js').status_code == 200
    for path in ('/', '/index.html', '/team/index.html', '/assets/team.js', '/team/assets/',
                 '/.env', '/team/../index.html', '/api/v1/status', '/openapi.json', '/team/%2e%2e/index.html',
                 '/team/assets/secretary.js'):
        response = client.get(path)
        assert response.status_code == 404, path
        assert 'SECRETARY_ENTRY_MUST_NOT_BE_SERVED' not in response.text
    assert client.get('/fixture', headers={'Host': 'evil.example'}).status_code == 403


@pytest.mark.parametrize('action,values', [('rename', {'title': 'Проверено через HTTP MockTransport'}),
    ('set_state', {'bucket': 'doing'}), ('comment', {'comment': 'Синтетический комментарий'}),
    ('classify', {'important': False, 'urgent': True, 'classification_confirmed': True})])
def test_queued_acceptance_then_real_service_verifies_simulated_remote(case, action, values):
    _, f, client, _ = case
    headers = login(f, client)
    old = f.repository.get_projection(f.owners[0].id, f.task_ids[0])
    payload = dict(operation_id=str(uuid4()), project_id=old.project_id, task_id=old.task_id,
                   expected_revision=old.revision, expected_fingerprint=old.remote_fingerprint,
                   action=action, values=values)
    response = client.post('/api/team/v1/commands', json=payload, headers=headers)
    assert response.status_code == 202 and response.json()['execution_state']['state'] == 'queued'
    assert f.repository.get_projection(f.owners[0].id, old.task_id) == old
    receipt = asyncio.run(f.worker.run_once())
    assert receipt.execution_state.state == 'applied', receipt
    assert receipt.current.revision == old.revision + 1
    assert receipt.current.remote_fingerprint != old.remote_fingerprint
    replay = client.post('/api/team/v1/commands', json=payload, headers=headers).json()
    assert replay['execution_state']['state'] == 'applied'
    assert len(f.provider.mutations) >= 1
    assert all(request.url.host == '127.0.0.1' for request in f.provider.requests)


def test_snapshot_conflict_does_not_mutate_simulated_remote(case):
    _, f, client, _ = case
    headers = login(f, client)
    old = f.repository.get_projection(f.owners[0].id, f.task_ids[0])
    payload = dict(operation_id=str(uuid4()), project_id=old.project_id, task_id=old.task_id,
                   expected_revision=old.revision + 1, expected_fingerprint=old.remote_fingerprint,
                   action='rename', values={'title': 'stale'})
    result = client.post('/api/team/v1/commands', json=payload, headers=headers)
    assert result.status_code == 202 and result.json()['execution_state']['state'] == 'conflict'
    assert asyncio.run(f.worker.run_once()) is None and f.provider.mutations == []


def test_fixture_code_refresh_requires_own_origin(case):
    _, f, client, _ = case
    old = f.codes[0]
    assert client.post('/fixture', data={'owner': '0'}).status_code == 403
    assert client.post('/fixture', data={'owner': '0'}, headers={'Origin': f.origin}).status_code == 200
    assert f.codes[0] != old
    assert client.post('/api/team/v1/session/code', json={'value': old}, headers={'Origin': f.origin}).status_code == 401


def test_probe_path_guard_refuses_foreign_existing_and_reparse_roots(tmp_path):
    m = module()
    with pytest.raises(m.ProbeFailure):
        m.validate_run_root(tmp_path / 'foreign', parent=tmp_path)
    path = tmp_path / ('t9-ui-' + uuid4().hex)
    assert m.validate_run_root(path, parent=tmp_path) == path
    other = tmp_path / 'outside'
    other.mkdir()
    path.symlink_to(other, target_is_directory=True)
    with pytest.raises(m.ProbeFailure):
        m.validate_run_root(path, parent=tmp_path)


def test_status_and_stop_refuse_pid_reuse_without_any_process_kill(tmp_path, monkeypatch):
    m = module()
    path = tmp_path / ('t9-ui-' + uuid4().hex)
    path.mkdir()
    identity = dict(version=1, run_id=path.name, pid=12345, create_time=100., state='ready',
                    origin='https://secretary-t9.localhost:44443', simulation=True)
    (path / 'manifest.json').write_text(json.dumps(identity), encoding='utf-8')
    class OtherProcess:
        def create_time(self): return 200.
        def terminate(self): pytest.fail('probe must never terminate a PID')
        def kill(self): pytest.fail('probe must never kill a PID')
    monkeypatch.setattr(m.psutil, 'Process', lambda _: OtherProcess())
    with pytest.raises(m.ProbeFailure):
        m.request_stop(path, parent=tmp_path)
    assert not (path / 'stop.request').exists()


def test_stop_marker_is_bounded_scoped_and_never_follows_link(tmp_path):
    m = module()
    path = tmp_path / ('t9-ui-' + uuid4().hex)
    path.mkdir()
    marker = path / 'stop.request'
    assert m.stop_requested(path) is False
    marker.write_text('t9-ui-' + uuid4().hex, encoding='ascii')
    assert m.stop_requested(path) is False
    marker.write_text(path.name, encoding='ascii')
    assert m.stop_requested(path) is True
    marker.unlink()
    foreign = tmp_path / 'untouched.txt'
    foreign.write_text(path.name, encoding='ascii')
    marker.symlink_to(foreign)
    with pytest.raises(m.ProbeFailure):
        m.stop_requested(path)
    assert foreign.read_text(encoding='ascii') == path.name


def test_manifest_writes_do_not_follow_existing_pending_symlink(tmp_path):
    m = module()
    path = tmp_path / ('t9-ui-' + uuid4().hex)
    path.mkdir()
    foreign = tmp_path / 'untouched.txt'
    foreign.write_text('unchanged', encoding='ascii')
    (path / 'manifest.pending').symlink_to(foreign)
    with pytest.raises(m.ProbeFailure):
        m._write_manifest(path, {'simulation': True})
    assert foreign.read_text(encoding='ascii') == 'unchanged'


def test_create_assign_and_due_use_http_steps_and_exact_member_revision(case):
    _, f, client, _ = case
    headers = login(f, client)
    payload = dict(operation_id=str(uuid4()), project_id=f.project_ids[0], action='create',
                   expected_assignee_revision=0,
                   values={'title': 'Синтетическая новая задача', 'description': '<script>literal</script>',
                           'assignee_id': f.owners[0].id})
    response = client.post('/api/team/v1/commands', json=payload, headers=headers)
    assert response.json()['execution_state']['state'] == 'queued'
    created = asyncio.run(f.worker.run_once())
    assert created.execution_state.state == 'applied', created
    task = created.current
    for action, values, extra in [
        ('assign', {'assignee_id': f.owners[1].id}, {'expected_assignee_revision': 0}),
        ('set_due', {'due_at': '2026-10-10T09:00:00Z', 'due_confirmed': True,
                     'due_phrase': '10 октября в 12:00', 'reason': 'Синтетическое согласование'}, {}),
    ]:
        payload = dict(operation_id=str(uuid4()), project_id=task.project_id, task_id=task.task_id,
                       expected_revision=task.revision, expected_fingerprint=task.remote_fingerprint,
                       action=action, values=values, **extra)
        assert client.post('/api/team/v1/commands', json=payload, headers=headers).status_code == 202
        receipt = asyncio.run(f.worker.run_once())
        assert receipt.execution_state.state == 'applied', receipt
        task = receipt.current
    assert task.assignee_id == f.owners[1].id and task.due_confirmed


def test_static_asset_symlink_never_reads_outside_built_directory(case):
    _, f, client, dist = case
    outside = f.run_root / 'not-an-asset.txt'
    outside.write_text('SENSITIVE_SYNTHETIC_SENTINEL', encoding='ascii')
    (dist / 'assets/unsafe.js').symlink_to(outside)
    response = client.get('/team/assets/unsafe.js')
    assert response.status_code == 404 and 'SENSITIVE_SYNTHETIC_SENTINEL' not in response.text


def test_simulated_provider_rejects_unknown_methods_instead_of_fake_success(case):
    _, f, _, _ = case
    import httpx
    for method, path in [('DELETE', '/tasks/' + f.task_ids[0]), ('POST', '/projects/7/views')]:
        with pytest.raises(ValueError, match='simulator_unexpected_request'):
            f.provider(httpx.Request(method, 'http://127.0.0.1:3456/api/v2' + path))


def test_start_refuses_ancestor_link_before_making_any_foreign_directory(tmp_path, monkeypatch):
    m = module()
    outside = tmp_path / 'outside'
    outside.mkdir()
    anchor = tmp_path / 'runtime-link'
    anchor.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(m, 'SCRATCH', anchor / 'team-rollout')
    dist = tmp_path / 'dist'
    dist.mkdir()
    (dist / 'team.html').write_text('<html>synthetic</html>', encoding='ascii')
    with pytest.raises(m.ProbeFailure):
        m.start(dist, tmp_path / 'openssl.exe')
    assert list(outside.iterdir()) == []


def test_real_read_status_directory_and_unclassified_full_sync(case):
    _, f, client, _ = case
    login(f, client)
    status = client.get('/api/team/v1/status', params={'project_id': f.project_ids[0]})
    assert status.status_code == 200
    body = status.json()
    assert body['sync']['state'] == 'ready' and body['sync']['last_successful_sync_at']
    assert body['cloud']['state'] == 'not_configured'
    directory = client.get('/api/team/v1/members', params={'project_id': f.project_ids[0]}).json()
    assert {row['id'] for row in directory['items']} == {member.id for member in f.owners[:2]}
    assert all(set(row) == {'id', 'display_name', 'revision'} for row in directory['items'])
    assert any(not f.repository.get_projection(f.owners[0].id, task_id).classification_confirmed for task_id in f.task_ids[:62])
    assert f.sync_services[f.project_ids[0]].sync_once().state == 'ready'


def test_external_change_is_observed_by_real_sync_without_invented_author(case):
    _, f, client, _ = case
    login(f, client)
    old = f.repository.get_projection(f.owners[0].id, f.task_ids[0])
    response = client.post('/fixture', data={'scenario': 'external_title'}, headers={'Origin': f.origin})
    assert response.status_code == 200
    current = f.repository.get_projection(f.owners[0].id, old.task_id)
    assert current.title != old.title and current.revision == old.revision + 1
    history = client.get('/api/team/v1/tasks/' + old.task_id + '/history').json()['items']
    observation = next(row for row in history if row['kind'] == 'external_change')
    assert observation['changed_fields'] == ['title']
    assert observation['actor_id'] is None and observation['remote_occurred_at'] is None
    assert observation['recorded_at'] and observation['before_fingerprint'] == old.remote_fingerprint


def test_foreign_due_is_degraded_and_preserves_confirmed_projection_and_last_success(case):
    _, f, client, _ = case
    login(f, client)
    before = client.get('/api/team/v1/status', params={'project_id': f.project_ids[0]}).json()['sync']
    old = f.repository.get_projection(f.owners[0].id, f.task_ids[0])
    response = client.post('/fixture', data={'scenario': 'external_due'}, headers={'Origin': f.origin})
    assert response.status_code == 200
    after = client.get('/api/team/v1/status', params={'project_id': f.project_ids[0]}).json()['sync']
    assert after['state'] == 'degraded' and after['issue_count'] >= 1
    assert after['last_successful_sync_at'] == before['last_successful_sync_at']
    assert f.repository.get_projection(f.owners[0].id, old.task_id) == old
    assert client.post('/fixture', data={'scenario': 'restore_due'}, headers={'Origin': f.origin}).status_code == 200
    assert client.get('/api/team/v1/status', params={'project_id': f.project_ids[0]}).json()['sync']['state'] == 'ready'


@pytest.mark.parametrize('blocked', ['connection', 'request'])
def test_owned_serve_bounds_actual_uvicorn_shutdown_drain(tmp_path, monkeypatch, blocked):
    """A stuck TLS connection/request must not keep the owned fixture alive."""
    import uvicorn

    m = module()
    run = tmp_path / ('t9-ui-' + uuid4().hex)
    run.mkdir()
    original_validate = m.validate_run_root
    monkeypatch.setattr(m, 'validate_run_root', lambda value: original_validate(value, parent=tmp_path))

    def certificate(root, executable):
        cert, key = root / 'localhost.crt', root / 'localhost.key'
        cert.write_text('-----BEGIN CERTIFICATE-----\nZml4dHVyZQ==\n-----END CERTIFICATE-----\n', encoding='ascii')
        key.write_text('synthetic; never used for TLS', encoding='ascii')
        return cert, key

    class Socket:
        def bind(self, address):
            assert address == ('127.0.0.1', 0)
        def getsockname(self):
            return ('127.0.0.1', 44443)
        def close(self):
            pass

    class Worker:
        def __init__(self):
            self.stopping = asyncio.Event()
            self.stopped = self.finished = False
        async def run(self):
            await self.stopping.wait()
            self.finished = True
        def stop(self):
            self.stopped = True
            self.stopping.set()

    class Fixture:
        def __init__(self):
            self.worker, self.app = Worker(), object()
            self.closed = False
        def close(self):
            self.closed = True

    fixtures = []
    def build_fixture(*args, **kwargs):
        fixture = Fixture()
        fixtures.append(fixture)
        return fixture

    class StuckConnection:
        def shutdown(self):
            pass  # Model a disconnected transport whose protocol never left the set.

    async def lifespan_shutdown():
        return None

    request_tasks = []
    class ControlledServer(uvicorn.Server):
        async def serve(self, sockets=None):
            self.servers = []
            self.lifespan = SimpleNamespace(shutdown=lifespan_shutdown)
            if blocked == 'connection':
                self.server_state.connections.add(StuckConnection())
            else:
                task = asyncio.create_task(asyncio.Event().wait())
                request_tasks.append(task)
                self.server_state.tasks.add(task)
                task.add_done_callback(self.server_state.tasks.discard)
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(.01)
            # Execute real Uvicorn shutdown using the actual Config built by _serve.
            await self.shutdown(sockets=sockets)
            await asyncio.sleep(0)

    monkeypatch.setattr(m, '_certificate', certificate)
    monkeypatch.setattr(m, 'socket', SimpleNamespace(socket=lambda *args: Socket(), AF_INET=2, SOCK_STREAM=1))
    monkeypatch.setattr(m, 'build_fixture', build_fixture)
    monkeypatch.setattr(m, 'stop_requested', lambda root: True)
    monkeypatch.setattr(uvicorn, 'Server', ControlledServer)

    async def scenario():
        try:
            await asyncio.wait_for(m._serve(run, tmp_path / 'dist', tmp_path / 'openssl.exe'), timeout=6.5)
            assert fixtures[0].worker.stopped and fixtures[0].worker.finished
            assert fixtures[0].closed
            if blocked == 'request':
                assert len(request_tasks) == 1 and request_tasks[0].cancelled()
            assert json.loads((run / 'manifest.json').read_text(encoding='utf-8'))['state'] == 'stopped'
        except TimeoutError:
            pytest.fail('Owned fixture shutdown exceeded 6.5 seconds with a blocked ' + blocked, pytrace=False)
        finally:
            for task in request_tasks:
                task.cancel()
            if request_tasks:
                await asyncio.gather(*request_tasks, return_exceptions=True)
    asyncio.run(scenario())
