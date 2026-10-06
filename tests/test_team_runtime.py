"""T12 runtime boundary tests: synthetic files/credentials only, never live services."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest


def config_data(root):
    return {
        'schema_version': 1, 'deployment_id': str(uuid4()), 'project_dir': str(root),
        'secretary_database_path': str(root / 'source' / 'meetings-custom.sqlite'),
        'team_database_path': str(root / 'team' / 'commands-custom.sqlite'),
        'billing_database_path': str(root / 'ledger' / 'shared-custom.sqlite'),
        'vikunja_database_path': str(root / 'native' / 'tasks-custom.sqlite'),
        'control_database_path': str(root / 'control' / 'maintenance-custom.sqlite'),
        'public_origin': 'https://team.example.test', 'max_bot_id': '19',
        'max_bot_token': 'synthetic-max-token', 'max_webhook_secret': 'synthetic-hook-secret',
        'team_auth_secret': 'x' * 32, 'publication_service_secret': 'synthetic-service-secret',
        'polza_api_key': 'synthetic-polza-key', 'vikunja_token': 'tk_synthetic-token',
        'local_owner_id': '10000000-0000-4000-8000-000000000001',
        'project_bindings': [{'project_id': '7', 'manual_view_id': '8', 'bot_user_id': '19',
            'bucket_ids': dict(zip(('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled'),
                                   map(str, range(101, 108)))),
            'important_label_id': '21', 'urgent_label_id': '22', 'cancelled_label_id': '23'}],
        'publication_project_id': '7',
    }


def parse(data):
    from secretary.team_settings import TeamRuntimeSettings
    return TeamRuntimeSettings.model_validate(data)


def test_config_preserves_explicit_independent_absolute_database_paths(tmp_path):
    data = config_data(tmp_path)
    settings = parse(data)
    for name in ('secretary_database_path', 'team_database_path', 'billing_database_path',
                 'vikunja_database_path', 'control_database_path'):
        assert getattr(settings, name) == Path(data[name])
    assert not (tmp_path / 'source').exists()


def test_config_does_not_read_process_env_or_default_dotenv(tmp_path, monkeypatch):
    from secretary.team_settings import secretary_settings
    data = config_data(tmp_path)
    (tmp_path / '.env').write_text('POLZA_API_KEY=must-never-be-read\n', encoding='utf-8')
    monkeypatch.setenv('POLZA_API_KEY', 'must-never-be-read-either')
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'unexpected'))
    settings = parse(data)
    cloud = secretary_settings(settings)
    assert cloud.polza_api_key.get_secret_value() == data['polza_api_key']
    assert cloud.data_dir == Path(data['secretary_database_path']).parent
    assert cloud.project_dir == tmp_path


@pytest.mark.parametrize('change', [
    {'team_database_path': 'relative.sqlite'},
    {'team_database_path': '//server/share/team.sqlite'},
    {'gateway_port': True}, {'gateway_port': '8766'}, {'gateway_port': 0},
    {'local_secretary_port': 65536}, {'schema_version': 2},
    {'publication_project_id': '999'}, {'approved_monthly_external_costs_rub': '3001'},
    {'outbound_enabled': 'true'}, {'surprise_provider_url': 'https://unknown.test'},
    {'schema_version': True}, {'secretary_options': {'cloud_enabled': 'false'}},
])
def test_unsafe_or_ambiguous_runtime_configuration_is_refused(tmp_path, change):
    data = {**config_data(tmp_path), **change}
    with pytest.raises(ValueError):
        parse(data)


def test_duplicate_database_paths_refused_before_opening_sqlite(tmp_path):
    data = config_data(tmp_path)
    data['billing_database_path'] = data['team_database_path']
    with pytest.raises(ValueError):
        parse(data)
    assert list(tmp_path.rglob('*.sqlite')) == []


def test_database_outside_project_is_refused(tmp_path):
    data = config_data(tmp_path)
    data['team_database_path'] = str(tmp_path.parent / 'outside.sqlite')
    with pytest.raises(ValueError):
        parse(data)


def test_config_loader_rejects_duplicate_json_keys_without_secret_output(tmp_path):
    from secretary.team_settings import load_team_settings, RuntimeConfigurationError
    path = tmp_path / 'protected.json'
    path.write_text('{"max_bot_token":"private-not-in-error","max_bot_token":"second"}', encoding='utf-8')
    with pytest.raises(RuntimeConfigurationError) as error:
        load_team_settings(path)
    assert error.value.code == 'team_runtime_config_invalid'
    assert 'private-not-in-error' not in str(error.value)


def test_config_secrets_are_redacted_and_missing_credentials_are_honest(tmp_path):
    data = config_data(tmp_path)
    settings = parse(data)
    assert data['max_bot_token'] not in repr(settings)
    assert data['polza_api_key'] not in settings.model_dump_json()
    assert settings.configuration_code() is None
    data['max_bot_token'] = ''
    assert parse(data).configuration_code() == 'max_configuration_required'


def test_restore_and_default_runtime_are_outbound_disabled(tmp_path):
    assert parse(config_data(tmp_path)).outbound_enabled is False
    data = {**config_data(tmp_path), 'outbound_enabled': True, 'restore_blocked': True}
    assert parse(data).outbound_allowed is False


def test_shared_secretary_provider_is_disabled_when_outbound_is_disabled(tmp_path):
    from secretary.team_settings import secretary_settings
    assert secretary_settings(parse(config_data(tmp_path))).cloud_enabled is False
    configured = {**config_data(tmp_path), 'outbound_enabled': True}
    assert secretary_settings(parse(configured)).cloud_enabled is True


def seeded_runtime(tmp_path, *, outbound=True, max_identity='19', restore=False, subscription_present=True,
                   reconcile=False, on_max_request=None):
    """Real stores and HTTP adapters with a purely in-memory provider contract."""
    from datetime import datetime, timezone
    import httpx
    from importlib import util
    import sys
    fixture_path = Path(__file__).resolve().parents[1] / 'scripts/team/probe_team_ui.py'
    fixture_spec = util.spec_from_file_location('t12_synthetic_native', fixture_path)
    fixture_module = util.module_from_spec(fixture_spec)
    sys.modules[fixture_spec.name] = fixture_module
    fixture_spec.loader.exec_module(fixture_module)
    SimulatedVikunja = fixture_module.SimulatedVikunja
    from secretary.domain.team import TeamMember
    from secretary.infrastructure.team_database import TeamDatabase
    from secretary.infrastructure.team_repository import TeamRepository
    from secretary.infrastructure.vikunja import ProjectBinding
    from secretary.orchestration.team_runtime import TeamRuntime, RuntimeDependencies
    data = config_data(tmp_path)
    data.update(outbound_enabled=outbound, polza_api_key='', vikunja_token='SYNTHETIC_ONLY', subscription_reconcile_enabled=reconcile)
    second = {**data['project_bindings'][0], 'project_id': '9', 'manual_view_id': '18',
        'bucket_ids': dict(zip(('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled'), map(str, range(201, 208)))),
        'important_label_id': '31', 'urgent_label_id': '32', 'cancelled_label_id': '33'}
    data['project_bindings'].append(second)
    settings = parse(data)
    now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    team = TeamRepository(TeamDatabase(settings.team_database_path), clock=lambda: now)
    owner = TeamMember(id=settings.local_owner_id, display_name='Синтетический владелец', role='owner',
        max_user_id='7001', vikunja_user_id='8001', project_ids=('7', '9'))
    team.upsert_member(owner, expected_revision=None)
    if restore:
        with team.db.transaction() as conn:
            conn.execute('CREATE TABLE maintenance_restore_guard(id INTEGER PRIMARY KEY,restore_id TEXT,reconciliation_required INTEGER,manifest_sha256 TEXT)')
            conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,?,1,?)', (str(uuid4()), 'a' * 64))
    bindings = tuple(ProjectBinding(**item.model_dump()) for item in settings.project_bindings)
    native = SimulatedVikunja(bindings)
    for index in range(52):
        binding = bindings[0 if index < 51 else 1]
        identifier = str(500 + index)
        native.tasks[identifier] = dict(id=int(identifier), project_id=int(binding.project_id),
            title='Синтетическая задача ' + identifier, description='', done=False, due_date='0001-01-01T00:00:00Z',
            assignees=[{'id': 8001}], labels=[], buckets=[{'id': int(binding.bucket_ids['accepted']),
            'project_view_id': int(binding.manual_view_id)}], repeat_after=0, repeat_mode=0)
        native.comments[identifier] = []
    max_requests = []
    subscriptions = [subscription_present]
    def max_handler(request):
        max_requests.append(request)
        if on_max_request is not None:
            on_max_request(request)
        if request.method == 'GET' and request.url.path == '/me':
            return httpx.Response(200, json={'user_id': int(max_identity), 'is_bot': True, 'username': 'synthetic_bot'})
        if request.method == 'GET' and request.url.path == '/subscriptions':
            return httpx.Response(200, json={'subscriptions': [{'url': settings.public_origin + '/hooks/max',
                'time': 0, 'update_types': ['message_created', 'message_callback', 'bot_started']}] if subscriptions[0] else []})
        if request.method == 'POST' and request.url.path == '/subscriptions':
            subscriptions[0] = True
            return httpx.Response(200, json={'success': True})
        if request.method == 'POST' and request.url.path == '/messages':
            body = json.loads(request.content)
            return httpx.Response(200, json={'message': {'body': {'mid': 'synthetic-message', 'text': body['text']},
                'recipient': {'user_id': int(request.url.params['user_id']), 'chat_type': 'dialog'},
                'sender': {'user_id': 19}, 'timestamp': int(now.timestamp() * 1000)}})
        raise AssertionError('Unexpected synthetic MAX operation')
    runtime = TeamRuntime(settings, run_id=str(uuid4()), control_dir=tmp_path / 'runs' / 'synthetic', clock=lambda: now,
        dependencies=RuntimeDependencies(vikunja_transport=httpx.MockTransport(native),
            max_transport=httpx.MockTransport(max_handler),
            identity={'pid': os.getpid(), 'creation_time': 'synthetic-only', 'executable_sha256': 'a' * 64, 'argv_sha256': 'b' * 64}))
    return runtime, team, owner, native, max_requests


async def eventually(predicate, timeout=5):
    async def wait():
        while not predicate():
            await asyncio.sleep(.01)
    await asyncio.wait_for(wait(), timeout)


@pytest.mark.asyncio
async def test_explicit_runtime_starts_real_scoped_workers_and_paginates_two_projects(tmp_path):
    from secretary.domain.team import TaskCommand, TaskChange
    runtime, team, owner, native, maximum = seeded_runtime(tmp_path)
    await runtime.start()
    try:
        assert runtime.components['gateway']['state'] == 'healthy', runtime.health()
        assert runtime.components['max_identity']['state'] == 'healthy'
        descriptor_path = runtime.settings.control_database_path.parent / 'gateway-participant.json'
        descriptor = json.loads(descriptor_path.read_text(encoding='utf-8'))
        assert descriptor == runtime.maintenance.participant_descriptor(runtime.participant_id, role='gateway')
        assert descriptor['containment_version'] == 0
        assert runtime.components['native_containment']['state'] == 'not_configured'
        assert descriptor['run_id'] == runtime.run_id and str(runtime.settings.team_database_path) not in json.dumps(descriptor)
        await eventually(lambda: runtime.components.get('sync_7', {}).get('state') == 'healthy'
                         and runtime.components.get('sync_9', {}).get('state') == 'healthy')
        assert len(team.list_projections(owner.id, '7', limit=100)) == 51
        assert len(team.list_projections(owner.id, '9', limit=100)) == 1
        command = TaskCommand(operation_id=str(uuid4()), action='create', project_id='9', expected_assignee_revision=0,
            values=TaskChange(title='Команда во второй проект', assignee_id=owner.id,
                classification_confirmed=True, important=False, urgent=False))
        receipt = team.accept_command(owner.id, command, expected_actor_revision=0)
        assert receipt.execution_state.state == 'queued'
        await eventually(lambda: team.get_receipt(owner.id, command.operation_id).execution_state.state == 'applied')
        current = team.get_receipt(owner.id, command.operation_id)
        assert native.tasks[current.execution_state.task_id]['project_id'] == 9
        assert runtime.budget.repository.path == runtime.settings.billing_database_path
        assert runtime.provider.budget is runtime.budget
        assert all(request.method == 'GET' or request.url.path == '/messages' for request in maximum)
    finally:
        await runtime.close()
    assert runtime.phase == 'stopped'
    assert runtime.maintenance.status()['active_tickets'] == 0


@pytest.mark.asyncio
async def test_wrong_max_identity_does_not_register_send_or_accept_http_intake(tmp_path):
    import httpx
    from secretary.domain.team import TaskCommand, TaskChange
    runtime, team, owner, native, maximum = seeded_runtime(tmp_path, max_identity='20')
    command = TaskCommand(operation_id=str(uuid4()), action='create', project_id='7', expected_assignee_revision=0,
        values=TaskChange(title='Не отправлять при чужом MAX боте', assignee_id=owner.id))
    team.accept_command(owner.id, command)
    await runtime.start()
    try:
        assert runtime.components['max_identity']['state'] == 'not_configured'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.app), base_url='https://team.example.test') as client:
            response = await client.post('/hooks/max', json={}, headers={'X-Max-Bot-Api-Secret': 'synthetic-hook-secret'})
            assert response.status_code == 503
        assert all(request.method == 'GET' and request.url.path == '/me' for request in maximum)
        assert not getattr(runtime, '_max_loops_started', False)
        await asyncio.sleep(.15)
        assert native.requests == []
        assert team.get_receipt(owner.id, command.operation_id).execution_state.state == 'queued'
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_restore_guard_blocks_startup_recovery_intake_and_every_provider_call(tmp_path):
    import httpx
    runtime, team, _, native, maximum = seeded_runtime(tmp_path, restore=True)
    await runtime.start()
    try:
        assert runtime.team is None and runtime.budget is None
        assert runtime.components['gateway']['error_code'] == 'team_restore_reconciliation_required'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.app), base_url='https://team.example.test') as client:
            assert (await client.get('/api/team/v1/me')).status_code == 503
        assert native.requests == [] and maximum == []
        assert not runtime._workers
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_vikunja_restore_guard_blocks_before_new_control_store_or_provider_calls(tmp_path):
    import sqlite3
    runtime, _, _, native, maximum = seeded_runtime(tmp_path)
    path = runtime.settings.vikunja_database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE files(id INTEGER PRIMARY KEY)')
        conn.execute('''CREATE TABLE maintenance_restore_guard(
            id INTEGER PRIMARY KEY CHECK(id=1), restore_id TEXT NOT NULL,
            reconciliation_required INTEGER NOT NULL CHECK(reconciliation_required=1),
            manifest_sha256 TEXT NOT NULL)''')
        conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,?,1,?)',
                     (str(uuid4()), 'a' * 64))

    await runtime.start()
    try:
        assert not runtime.settings.control_database_path.exists()
        assert runtime.components['gateway']['error_code'] == 'team_restore_reconciliation_required'
        assert runtime.components['outbound']['error_code'] == 'team_restore_reconciliation_required'
        assert runtime.team is None and runtime.budget is None
        assert native.requests == [] and maximum == []
        assert not runtime._workers
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_default_outbound_disabled_never_reads_profile_or_starts_recovering_workers(tmp_path):
    runtime, _, _, native, maximum = seeded_runtime(tmp_path, outbound=False)
    await runtime.start()
    try:
        assert runtime.components['gateway']['state'] == 'healthy'
        assert runtime.components['outbound']['error_code'] == 'team_outbound_disabled'
        assert native.requests == [] and maximum == []
        assert runtime.budget.snapshot().paused_code == 'monthly_budget_configuration'
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_bound_stop_file_and_health_do_not_leak_secrets(tmp_path):
    runtime, _, _, _, _ = seeded_runtime(tmp_path, outbound=False)
    await runtime.start()
    try:
        record = runtime.health()
        encoded = json.dumps(record)
        assert runtime.settings.max_bot_token.get_secret_value() not in encoded
        assert str(runtime.settings.team_database_path) not in encoded
        request = {'schema_version': 1, 'deployment_id': runtime.settings.deployment_id, 'run_id': str(uuid4()),
            'request_id': str(uuid4()), 'requested_at': runtime._now().isoformat()}
        target = runtime.control_dir / 'stop.request.json'
        target.write_text(json.dumps(request), encoding='utf-8')
        assert runtime._requested_stop() is False
        request['run_id'] = runtime.run_id
        target.write_text(json.dumps(request), encoding='utf-8')
        assert runtime._requested_stop() is True
    finally:
        await runtime.close()


def test_no_implicit_publication_project_from_first_binding(tmp_path):
    data = config_data(tmp_path)
    del data['publication_project_id']
    settings = parse(data)
    assert settings.publication_project_id is None
    assert settings.publication_configured is False


def test_runtime_constructor_is_inert(tmp_path, monkeypatch):
    from secretary.orchestration.team_runtime import TeamRuntime
    settings = parse(config_data(tmp_path))
    runtime = TeamRuntime(settings, run_id=str(uuid4()), control_dir=tmp_path / 'runs' / 'one')
    assert runtime.phase == 'starting'
    assert not (tmp_path / 'control').exists()
    assert not (tmp_path / 'team').exists()
    assert not (tmp_path / 'runs').exists()


class FakeMaintenance:
    def __init__(self):
        self.blocked = False
        self.active = []
        self.left = []

    def enter(self, participant, kind, operation_id=None):
        if self.blocked:
            raise ValueError('maintenance_blocked')
        ticket = object()
        self.active.append(ticket)
        return ticket

    def leave(self, ticket):
        self.active.remove(ticket)
        self.left.append(ticket)

    @asynccontextmanager
    async def async_admission(self, participant, kind, operation_id=None):
        ticket = self.enter(participant, kind, operation_id)
        try:
            yield ticket
        finally:
            self.leave(ticket)


@pytest.mark.asyncio
async def test_asgi_drain_keeps_admission_until_cancelled_request_finishes_owned_work():
    from secretary.orchestration.team_runtime import AdmittedASGI
    gate, entered, finish = FakeMaintenance(), asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        entered.set()
        await finish.wait()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    wrapped = AdmittedASGI(app, gate, 'runtime')
    output = []

    async def receive():
        return {'type': 'http.request', 'body': b''}

    async def send(message):
        output.append(message)

    request = asyncio.create_task(wrapped({'type': 'http', 'path': '/', 'method': 'GET'}, receive, send))
    await entered.wait()
    request.cancel()
    await asyncio.sleep(0)
    assert len(gate.active) == 1 and gate.left == []
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert len(gate.left) == 1 and gate.active == []
    assert output[0]['status'] == 200


@pytest.mark.asyncio
async def test_maintenance_refuses_entire_http_request_before_intake_or_auth_write():
    from secretary.orchestration.team_runtime import AdmittedASGI
    gate = FakeMaintenance()
    gate.blocked = True
    calls, output = [], []

    async def app(scope, receive, send):
        calls.append('mutated')

    async def send(message):
        output.append(message)

    await AdmittedASGI(app, gate, 'runtime')({'type': 'http', 'path': '/hooks/max', 'method': 'POST'}, None, send)
    assert not calls
    assert output[0]['status'] == 503
    assert b'maintenance' in output[1]['body']


@pytest.mark.asyncio
async def test_admitted_async_operation_leaves_ticket_after_receipt_checkpoint():
    from secretary.orchestration.team_runtime import admitted_operation
    gate = FakeMaintenance()
    order = []

    async def run():
        assert len(gate.active) == 1
        order.append('provider-response')
        await asyncio.sleep(0)
        order.append('durable-receipt')
        return 19

    assert await admitted_operation(gate, 'runtime', 'worker', run) == 19
    assert order == ['provider-response', 'durable-receipt'] and len(gate.left) == 1


@pytest.mark.asyncio
async def test_occupied_gateway_port_refuses_before_runtime_factory(tmp_path, monkeypatch):
    import socket
    import secretary.orchestration.team_runtime as runtime_module
    from secretary.interface.team_main import run_gateway
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    occupied.bind(('127.0.0.1', 0))
    occupied.listen(1)
    data = config_data(tmp_path)
    data['gateway_port'] = occupied.getsockname()[1]
    calls = []
    monkeypatch.setattr(runtime_module, 'TeamRuntime', lambda *args, **kwargs: calls.append('constructed'))
    try:
        with pytest.raises(OSError):
            await run_gateway(parse(data), run_id=str(uuid4()), control_dir=tmp_path / 'runs' / 'occupied')
        assert calls == []
        assert list(tmp_path.rglob('*.sqlite')) == []
    finally:
        occupied.close()


def test_due_router_confirms_using_validated_preview_project():
    from types import SimpleNamespace
    from secretary.orchestration.team_runtime import _DueRouter
    calls = []
    stored = SimpleNamespace(preview=SimpleNamespace(candidate=SimpleNamespace(project_id='9')))
    repository = SimpleNamespace(confirmation_state=lambda *args, **kwargs: (stored, None))
    service = SimpleNamespace(confirm=lambda *args, **kwargs: calls.append((args, kwargs)) or 'receipt')
    router = _DueRouter(None, repository, {'9': service})
    assert router.confirm('actor', 'preview', SimpleNamespace(operation_id='operation'), expected_actor_revision=2) == 'receipt'
    assert calls[0][1] == {'expected_actor_revision': 2}


@pytest.mark.asyncio
async def test_live_predecessor_without_tickets_refuses_before_stores_or_clients(tmp_path, monkeypatch):
    from secretary.infrastructure.team_maintenance import MaintenanceRepository
    import secretary.infrastructure.team_process_identity as identity_module
    from secretary.orchestration.team_runtime import TeamRuntime, RuntimeDependencies
    settings = parse(config_data(tmp_path))
    gate = MaintenanceRepository(settings.control_database_path, settings.deployment_id)
    old_run = str(uuid4())
    identity = {'pid': os.getpid(), 'creation_time': 'synthetic-only', 'executable_sha256': 'a' * 64, 'argv_sha256': 'b' * 64}
    gate.register_participant('gateway:' + old_run, run_id=old_run, identity=identity)
    def alive(value):
        assert value == identity
        raise ValueError('process_still_alive')
    monkeypatch.setattr(identity_module, 'verify_dead_identity', alive)
    runtime = TeamRuntime(settings, run_id=str(uuid4()), control_dir=tmp_path / 'runs' / 'second',
        dependencies=RuntimeDependencies(identity=identity))
    await runtime.start()
    try:
        assert runtime.components['gateway'] == {'state': 'not_configured', 'error_code': 'team_runtime_predecessor_unverified'}
        assert runtime.team is None and runtime._clients == [] and not runtime._workers
        assert gate.status()['active_tickets'] == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_cancelled_runtime_close_drains_operation_and_closes_clients(tmp_path):
    from types import SimpleNamespace
    from secretary.orchestration.team_runtime import TeamRuntime
    runtime = TeamRuntime(parse(config_data(tmp_path)), run_id=str(uuid4()), control_dir=tmp_path / 'runs' / 'drain')
    runtime.maintenance = FakeMaintenance()
    entered, finish = asyncio.Event(), asyncio.Event()
    closed = []
    runtime._clients = [SimpleNamespace(close=lambda: closed.append('client'))]
    async def operation():
        entered.set()
        await finish.wait()
    runtime._tasks = [asyncio.create_task(runtime._loop('test', 'outbound_task', operation, 1))]
    await entered.wait()
    close = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    close.cancel()
    await asyncio.sleep(0)
    assert runtime.maintenance.active and closed == []
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert runtime.phase == 'stopped' and closed == ['client']
    assert runtime.maintenance.active == []


def test_stop_request_duplicate_fields_do_not_choose_a_last_value(tmp_path):
    from secretary.orchestration.team_runtime import TeamRuntime
    runtime = TeamRuntime(parse(config_data(tmp_path)), run_id=str(uuid4()), control_dir=tmp_path / 'runs' / 'stop')
    runtime.control_dir.mkdir(parents=True)
    payload = {'schema_version': 1, 'deployment_id': runtime.settings.deployment_id, 'run_id': runtime.run_id,
        'request_id': str(uuid4()), 'requested_at': runtime._now().isoformat()}
    encoded = json.dumps(payload)
    (runtime.control_dir / 'stop.request.json').write_text('{"run_id":"other",' + encoded[1:], encoding='utf-8')
    assert runtime._requested_stop() is False


@pytest.mark.asyncio
async def test_missing_subscription_is_a_health_issue_without_automatic_registration(tmp_path):
    from secretary.team_settings import RuntimeConfigurationError
    runtime, _, _, _, maximum = seeded_runtime(tmp_path, outbound=False, subscription_present=False)
    await runtime.start()
    try:
        with pytest.raises(RuntimeConfigurationError, match='max_subscription_missing'):
            await runtime._subscription()
        assert runtime.health_repository.read_status()['subscription_state'] == 'missing'
        assert all(request.method == 'GET' for request in maximum)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_trusted_intake_records_only_durable_accepted_event_identity(tmp_path):
    from secretary.domain.bot import BotEvent
    from secretary.orchestration.team_runtime import _TrustedIntake
    from dataclasses import replace
    runtime, _, owner, _, _ = seeded_runtime(tmp_path, outbound=False)
    await runtime.start()
    try:
        adapter = _TrustedIntake(runtime.bot, runtime.health_repository)
        event = BotEvent(event_id=str(uuid4()), dedup_key='synthetic-accepted', bot_id='19', kind='message_created',
            user_id=owner.max_user_id, message_id='synthetic-mid', timestamp_ms=int(runtime._now().timestamp() * 1000), text='статус')
        receipt = adapter.intake(event)
        assert receipt.state == 'queued'
        assert adapter.intake(replace(event, event_id=str(uuid4()))).state == 'duplicate'
        quarantine = adapter.intake(replace(event, event_id=str(uuid4()), dedup_key='synthetic-unbound', user_id='999'))
        assert quarantine.state == 'quarantined'
        with runtime.maintenance._transaction() as conn:
            ids = [row[0] for row in conn.execute('SELECT event_id FROM runtime_health_callbacks')]
        assert ids == [receipt.event_id]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_subscription_post_ticket_matches_durable_registration_operation(tmp_path):
    seen = []
    def check(request):
        if request.method == 'POST':
            tickets = [ticket for ticket in runtime.maintenance.active_operations(runtime.participant_id)
                if ticket.kind == 'outbound_subscription']
            assert len(tickets) == 1
            with runtime.maintenance._transaction() as conn:
                row = conn.execute('SELECT operation_id,state FROM runtime_health_attempts').fetchone()
            assert row['state'] == 'submitted' and row['operation_id'] == tickets[0].operation_id
            seen.append(tickets[0].operation_id)
    runtime, _, _, _, _ = seeded_runtime(tmp_path, reconcile=True, subscription_present=False, on_max_request=check)
    runtime._start_loops = lambda verified: None
    await runtime.start()
    try:
        await runtime._subscription()
        assert len(seen) == 1
        assert runtime.health_repository.read_status()['subscription_state'] == 'verified'
        assert runtime.maintenance.active_operations(runtime.participant_id) == ()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_explicit_startup_checkpoints_proven_dead_predecessor_before_intake(tmp_path, monkeypatch):
    from secretary.domain.team import TaskCommand, TaskChange
    from secretary.infrastructure.team_maintenance import MaintenanceRepository
    from secretary.infrastructure.team_process_identity import DeadProcessProof
    import secretary.infrastructure.team_process_identity as identities
    import secretary.infrastructure.team_runtime_recovery as recovery_module
    runtime, team, owner, native, maximum = seeded_runtime(tmp_path, outbound=False)
    gate = MaintenanceRepository(runtime.settings.control_database_path, runtime.settings.deployment_id, clock=runtime.clock)
    gate.bind_sources({role: getattr(runtime.settings, role + '_database_path') for role in ('secretary', 'team', 'billing', 'vikunja')})
    old_run, old_identity = str(uuid4()), {**runtime.dependencies.identity, 'creation_time': '987654321'}
    old = 'gateway:' + old_run
    gate.register_participant(old, run_id=old_run, identity=old_identity)
    ticket = gate.enter(old, 'outbound_task')
    command = TaskCommand(operation_id=str(uuid4()), action='create', project_id='7', expected_assignee_revision=0,
        values=TaskChange(title='Неизвестный результат старой попытки', assignee_id=owner.id))
    team.accept_command(owner.id, command)
    team.claim_command(old + ':task')
    proof = DeadProcessProof(**old_identity, state='exited', observed_at=runtime._now().isoformat())
    def proven(identity):
        assert identity == old_identity
        return proof
    monkeypatch.setattr(identities, 'verify_dead_identity', proven)
    monkeypatch.setattr(recovery_module, 'verify_dead_identity', proven)
    await runtime.start()
    try:
        assert runtime.components['gateway']['state'] == 'healthy', runtime.health()
        assert runtime.components['recovery']['state'] == 'healthy'
        assert gate.active_operations(old) == ()
        assert team.get_receipt(owner.id, command.operation_id).execution_state.state == 'uncertain'
        with gate._transaction() as conn:
            saved = json.loads(conn.execute('SELECT payload FROM maintenance_recovery_checkpoints').fetchone()[0])
        assert saved['ticket_ids'] == [ticket.id]
        assert native.requests == [] and maximum == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_wrong_source_role_rolls_back_binding_before_registration_or_factory(tmp_path):
    import sqlite3
    from secretary.orchestration.team_runtime import TeamRuntime
    from secretary.infrastructure.team_backup import BackupError
    settings = parse(config_data(tmp_path))
    settings.team_database_path.parent.mkdir(parents=True)
    with sqlite3.connect(settings.team_database_path) as conn:
        conn.execute('CREATE TABLE unrelated_foreign_owner(value TEXT)')
    runtime = TeamRuntime(settings, run_id=str(uuid4()), control_dir=tmp_path / 'runs' / 'foreign')
    with pytest.raises(BackupError, match='backup_source_role_invalid'):
        await runtime.start()
    assert runtime.maintenance.bound_sources() == {}
    assert runtime.maintenance.status()['participants'] == ()
    assert runtime.team is None and runtime._clients == []
    assert not settings.secretary_database_path.exists() and not settings.billing_database_path.exists()
    with sqlite3.connect(settings.team_database_path) as conn:
        assert [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ['unrelated_foreign_owner']


@pytest.mark.asyncio
async def test_late_definite_identity_mismatch_halts_future_attempts(tmp_path):
    from types import SimpleNamespace
    from secretary.orchestration.team_runtime import TeamRuntime
    runtime = TeamRuntime(parse(config_data(tmp_path)), run_id=str(uuid4()), control_dir=tmp_path / 'runs' / 'identity')
    calls, stopped = [], []
    class ImmediateStop:
        def is_set(self):
            return bool(calls)
        async def wait(self):
            raise TimeoutError
    runtime._stop = ImmediateStop()
    runtime.maintenance = FakeMaintenance()
    runtime._workers = [SimpleNamespace(stop=lambda: stopped.append('worker'))]
    async def mismatch():
        runtime._component('max_identity', 'not_configured', 'max_bot_identity_mismatch')
        return False
    runtime._verify_max_identity = mismatch
    await runtime._retry_identity()
    async def attempted():
        calls.append('native-post')
    await runtime._loop('task_worker', 'outbound_task', attempted, 1)
    assert calls == [] and stopped == ['worker']
    assert runtime.components['gateway']['state'] == 'not_configured'


async def _await_event(event):
    await asyncio.wait_for(event.wait(), timeout=3)


@pytest.mark.asyncio
async def test_main_second_cancel_cannot_skip_runtime_drain_or_release_owned_socket(tmp_path, monkeypatch):
    """The stop helper is deliberately slow, as any awaited cancellation may be."""
    import socket
    import uvicorn
    from types import SimpleNamespace
    import secretary.infrastructure.team_process_job as process_job
    monkeypatch.setattr(process_job, "install_process_job", lambda: None)
    import secretary.orchestration.team_runtime as runtime_module
    import secretary.interface.team_main as main_module
    run_gateway = main_module.run_gateway

    order = []
    serving, stop_waiting, stop_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_stop, close_entered, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Socket:
        def setsockopt(self, *args):
            order.append('exclusive')
        def bind(self, address):
            assert address == ('127.0.0.1', 8766)
            order.append('bind')
        def listen(self, count):
            order.append('listen')
        def setblocking(self, value):
            order.append('nonblocking')
        def close(self):
            order.append('socket_closed')

    class Stop:
        async def wait(self):
            stop_waiting.set()
            try:
                await release_stop.wait()
            except asyncio.CancelledError:
                stop_cancelled.set()
                await release_stop.wait()

    class Runtime:
        def __init__(self, *args, **kwargs):
            assert order[-1] == 'nonblocking'
            order.append('runtime_created')
            self.stop_requested = Stop()
            self.app = None
        async def close(self):
            order.append('runtime_drain_entered')
            close_entered.set()
            await release_close.wait()
            order.append('runtime_drained')

    class Server:
        def __init__(self, config):
            self.started = True
            self.should_exit = False
        async def serve(self, *, sockets):
            assert len(sockets) == 1
            serving.set()
            await asyncio.Event().wait()

    fake_socket_module = SimpleNamespace(socket=lambda *args, **kwargs: Socket(),
        AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM, SOL_SOCKET=socket.SOL_SOCKET)
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        fake_socket_module.SO_EXCLUSIVEADDRUSE = socket.SO_EXCLUSIVEADDRUSE
    monkeypatch.setattr(main_module, 'socket', fake_socket_module)
    monkeypatch.setattr(uvicorn, 'Server', Server)
    monkeypatch.setattr(runtime_module, 'TeamRuntime', Runtime)
    task = asyncio.create_task(run_gateway(SimpleNamespace(gateway_port=8766),
        run_id=str(uuid4()), control_dir=tmp_path / 'synthetic-run'))
    try:
        await _await_event(serving)
        await _await_event(stop_waiting)
        task.cancel()
        await _await_event(stop_cancelled)
        task.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        assert 'socket_closed' not in order, order
        release_stop.set()
        await _await_event(close_entered)
        task.cancel()
        await asyncio.sleep(0)
        assert 'socket_closed' not in order and 'runtime_drained' not in order, order
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert order.index('runtime_drained') < order.index('socket_closed'), order
        assert order.count('runtime_drain_entered') == order.count('socket_closed') == 1
    finally:
        release_stop.set()
        release_close.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_full_project_scan_paginates_again_after_30_second_schedule(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from secretary.orchestration import team_runtime as runtime_module
    runtime, team, owner, native, maximum = seeded_runtime(tmp_path)
    runtime._start_loops = lambda verified: None
    async def no_monitor():
        return
    runtime._monitor = no_monitor
    await runtime.start()
    scans, delays, starts = [], [], []
    logical = [0.0]
    original = asyncio.wait_for

    async def wait(coroutine, *, timeout):
        if timeout == 25:
            coroutine.close()
            delays.append(timeout)
            logical[0] += timeout
            raise TimeoutError
        return await original(coroutine, timeout=timeout)

    async def scan():
        starts.append(logical[0])
        value = await asyncio.to_thread(runtime.sync_services['7'].sync_once)
        logical[0] += 5
        assert value.state == 'ready'
        current = team.get_projection(owner.id, '500')
        scans.append(current.title)
        if len(scans) == 1:
            native.tasks['500']['title'] = 'Synthetic changed between complete scans'
        else:
            runtime._stop.set()

    monkeypatch.setattr(asyncio, 'wait_for', wait)
    monkeypatch.setattr(runtime_module, 'time', SimpleNamespace(monotonic=lambda: logical[0]))
    try:
        await runtime._loop('sync_7', 'sync', scan, 30, maximum=300)
        assert len(team.list_projections(owner.id, '7', limit=100)) == 51
        pages = [str(request.url.params['page']) for request in native.requests
            if request.url.path == '/api/v2/projects/7/tasks']
        assert pages == ['1', '2', '1', '2'], pages
        assert len(scans) == 2 and scans[0] != scans[1]
        assert scans[1] == 'Synthetic changed between complete scans'
        assert starts == [0, 30]
        assert delays == [25]
        assert native.mutations == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_unknown_subscription_post_preserves_operation_ticket_and_never_replays(tmp_path):
    import httpx
    from secretary.team_settings import RuntimeConfigurationError
    checked = []

    def failure(request):
        if request.method != 'POST':
            return
        active = runtime.maintenance.active_operations(runtime.participant_id)
        outbound = [ticket for ticket in active if ticket.kind == 'outbound_subscription']
        assert len(outbound) == 1
        with runtime.maintenance._transaction() as conn:
            row = conn.execute('SELECT operation_id,state FROM runtime_health_attempts').fetchone()
        assert row['state'] == 'submitted' and row['operation_id'] == outbound[0].operation_id
        checked.append(row['operation_id'])
        raise httpx.ReadTimeout('synthetic response lost', request=request)

    runtime, _, _, native, maximum = seeded_runtime(tmp_path, reconcile=True,
        subscription_present=False, on_max_request=failure)
    runtime._start_loops = lambda verified: None
    await runtime.start()
    try:
        with pytest.raises(RuntimeConfigurationError, match='max_subscription_uncertain'):
            await runtime._subscription()
        for _ in range(2):
            with pytest.raises(RuntimeConfigurationError, match='max_subscription_uncertain'):
                await runtime._subscription()
        assert len(checked) == 1
        assert sum(request.method == 'POST' for request in maximum) == 1
        assert native.mutations == []
        health = runtime.health_repository.read_status()
        assert health['registration_state'] == 'uncertain' and health['phone_delivery'] == 'unknown'
        assert runtime.maintenance.active_operations(runtime.participant_id) == ()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_job_containment_refusal_prevents_factory_and_releases_owned_listener(tmp_path, monkeypatch):
    import socket
    from types import SimpleNamespace
    import secretary.interface.team_main as main_module
    import secretary.orchestration.team_runtime as runtime_module
    import secretary.infrastructure.team_process_job as process_job
    order = []
    class Listener:
        def setsockopt(self, *args):
            pass
        def bind(self, address):
            assert address == ('127.0.0.1', 8766)
            order.append('bind')
        def listen(self, count):
            pass
        def setblocking(self, value):
            pass
        def close(self):
            order.append('closed')
    def refused_job():
        order.append('job_refused')
        raise process_job.ProcessJobError('job_object_unavailable')
    def forbidden_factory(*args, **kwargs):
        raise AssertionError('No factory/database/provider after containment refusal')
    fake = SimpleNamespace(socket=lambda *args: Listener(), AF_INET=socket.AF_INET,
        SOCK_STREAM=socket.SOCK_STREAM, SOL_SOCKET=socket.SOL_SOCKET)
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        fake.SO_EXCLUSIVEADDRUSE = socket.SO_EXCLUSIVEADDRUSE
    monkeypatch.setattr(main_module, 'socket', fake)
    monkeypatch.setattr(process_job, 'install_process_job', refused_job)
    monkeypatch.setattr(runtime_module, 'TeamRuntime', forbidden_factory)
    with pytest.raises(process_job.ProcessJobError, match='job_object_unavailable'):
        await main_module.run_gateway(SimpleNamespace(gateway_port=8766), run_id=str(uuid4()), control_dir=tmp_path)
    assert order == ['bind', 'job_refused', 'closed']
    assert not list(tmp_path.rglob('*.sqlite'))


@pytest.mark.asyncio
async def test_explicit_owned_job_receipt_precedes_stores_and_qualifies_descriptor(tmp_path, monkeypatch):
    from secretary.infrastructure.team_process_job import WindowsJob, OwnedProcessJobProof
    runtime, _, _, _, _ = seeded_runtime(tmp_path, outbound=False)
    job = object.__new__(WindowsJob)
    job.pid = os.getpid()
    runtime.native_job = job
    verified = []
    def proof(self):
        assert self is job and runtime.team is None and runtime._clients == []
        assert not runtime.settings.billing_database_path.exists()
        verified.append('verified-before-stores')
        return OwnedProcessJobProof(**runtime.dependencies.identity)
    monkeypatch.setattr(WindowsJob, 'verify_owned_identity', proof)
    await runtime.start()
    try:
        assert verified == ['verified-before-stores']
        assert runtime.maintenance.has_native_containment(runtime.participant_id, runtime.run_id) is True
        assert runtime.components['native_containment']['state'] == 'healthy'
        descriptor = json.loads((runtime.settings.control_database_path.parent / 'gateway-participant.json').read_text(encoding='utf-8'))
        assert descriptor['containment_version'] == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('active_ticket', [False, True])
async def test_uncontained_dead_native_run_refuses_before_schema_migration_or_new_ledger(tmp_path, monkeypatch, active_ticket):
    from contextlib import closing
    import hashlib
    import sqlite3
    from secretary.infrastructure.team_maintenance import MaintenanceRepository
    from secretary.infrastructure.team_process_identity import DeadProcessProof
    import secretary.infrastructure.team_process_identity as identities
    import secretary.infrastructure.team_runtime_recovery as recovery_module
    runtime, _, _, native, maximum = seeded_runtime(tmp_path, outbound=False)
    gate = MaintenanceRepository(runtime.settings.control_database_path, runtime.settings.deployment_id, clock=runtime.clock)
    gate.bind_sources({role: getattr(runtime.settings, role + '_database_path')
        for role in ('secretary', 'team', 'billing', 'vikunja')})
    old_run = str(uuid4())
    old = 'gateway:' + old_run
    old_identity = {**runtime.dependencies.identity, 'creation_time': '987654321'}
    gate.register_participant(old, run_id=old_run, identity=old_identity)
    if active_ticket:
        gate.enter(old, 'job_voice')
    # A genuine schema-7 fixture: remove the otherwise empty migration-8/9 objects.
    path = runtime.settings.team_database_path
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('DROP TRIGGER response_abort_guard')
        conn.execute('DROP TRIGGER abort_response_guard')
        conn.execute('DROP TABLE voice_request_aborts')
        conn.execute('DROP TABLE team_due_resolution_consumptions')
        conn.execute('DROP TABLE team_due_resolution_previews')
        conn.execute('DELETE FROM team_schema WHERE version>7')
        identifier = str(uuid4())
        conn.execute('INSERT INTO bot_events VALUES(?,?,?,?,?,?,?,?)',
            (identifier, '19', 'message_created', identifier, 'e' * 64, 'f' * 64, '{}', runtime._now().timestamp()))
        conn.execute('INSERT INTO voice_jobs VALUES(?,?,?,?,?,?,?)',
            (identifier, identifier, '19', '7001', 'e' * 64, '{}', runtime._now().timestamp()))
        conn.execute('INSERT INTO voice_job_state VALUES(?,?,?,?,?,?,?)',
            (identifier, 'processing', 'stt', old + ':voice', 1, runtime._now().timestamp() + 3600, None))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    proof = DeadProcessProof(**old_identity, state='exited', observed_at=runtime._now().isoformat())
    def proven(identity):
        assert identity == old_identity
        return proof
    monkeypatch.setattr(identities, 'verify_dead_identity', proven)
    monkeypatch.setattr(recovery_module, 'verify_dead_identity', proven)
    await runtime.start()
    try:
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 7
            assert conn.execute('SELECT state FROM voice_job_state WHERE id=?', (identifier,)).fetchone()[0] == 'processing'
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before, 'Containment preflight must precede migrations'
        assert not runtime.settings.billing_database_path.exists()
        assert runtime.components['gateway'] == {'state': 'not_configured',
            'error_code': 'maintenance_recovery_native_containment_required'}
        assert runtime.team is None and not runtime._workers and runtime._clients == []
        assert len(gate.active_operations(old)) == int(active_ticket)
        assert native.requests == [] and maximum == []
    finally:
        await runtime.close()
