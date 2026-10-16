"""Cross-boundary T12 checks; no owner files, devices or cloud calls."""
import asyncio
import sqlite3
from threading import Event
from uuid import uuid4

import pytest

from secretary.infrastructure.database import Database
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_maintenance import MaintenanceRepository, MaintenanceError, MaintenanceBlocked
from secretary.infrastructure.voice_resources import CaptureLease, LeasedCapture
from secretary.interface.maintenance_middleware import MaintenanceMiddleware
from secretary.orchestration.publication_worker import PublicationWorker


@pytest.fixture
def gate(tmp_path):
    value = MaintenanceRepository(tmp_path / 'control.sqlite3', str(uuid4()))
    value.register_participant('secretary', run_id=str(uuid4()), identity={
        'pid': 123, 'creation_time': '2026-10-04T00:00:00Z',
        'executable_sha256': 'a' * 64, 'argv_sha256': 'b' * 64})
    return value


def test_secretary_autocommit_connections_cannot_write_after_barrier(tmp_path, gate):
    db = Database(tmp_path / 'source.sqlite3', maintenance=gate, participant_id='secretary')
    gate.begin_barrier(owner_id='backup')
    with pytest.raises(MaintenanceBlocked):
        db.execute("INSERT INTO configuration VALUES('unsafe','true')")
    with pytest.raises(MaintenanceBlocked):
        with db.connection():
            pytest.fail('unadmitted raw connection')
    with sqlite3.connect(db.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM configuration').fetchone()[0] == 0


def test_admitted_secretary_operation_commits_then_leaves_before_snapshot(tmp_path, gate):
    db = Database(tmp_path / 'source.sqlite3', maintenance=gate, participant_id='secretary')
    with gate.admission('secretary', 'job'):
        claim = gate.begin_barrier(owner_id='backup')
        db.create_meeting('synthetic durable result')
        assert len(gate.active_tickets(claim)) == 1
        with pytest.raises(MaintenanceBlocked):
            with gate.admission('secretary', 'outbound_polza'):
                pytest.fail('new paid attempt admitted')
    assert gate.active_tickets(claim) == ()
    with sqlite3.connect(db.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM meetings').fetchone()[0] == 1


def test_secretary_database_path_survives_different_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = Database('source.sqlite3')
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    db.create_meeting('same file')
    assert db.path == tmp_path / 'source.sqlite3'
    assert not (elsewhere / 'source.sqlite3').exists()


class Capture:
    def __init__(self):
        self.recording = False
        self.stops = 0

    def start(self, identifier, microphone, system, on_chunk, on_error, on_finish=None):
        self.recording = True
        return {'recording': True, 'native_closed': False}

    def state(self, identifier):
        return {'recording': self.recording, 'native_closed': not self.recording}

    def stop(self, identifier):
        self.stops += 1
        self.recording = False
        return self.state(identifier)


def test_active_capture_defers_barrier_and_never_interrupts_audio(gate):
    native = Capture()
    capture = LeasedCapture(native, CaptureLease(), 'meeting', maintenance=gate, participant_id='secretary')
    capture.start('synthetic')
    with pytest.raises(MaintenanceError, match='maintenance_capture_active'):
        gate.begin_barrier(owner_id='backup')
    assert native.recording and native.stops == 0
    capture.stop('synthetic')
    assert gate.status()['active_capture'] == 0
    gate.begin_barrier(owner_id='backup')
    with pytest.raises(MaintenanceBlocked):
        capture.start('later')
    assert not native.recording and native.stops == 1


def test_uncertain_native_capture_start_keeps_durable_admission(gate):
    class Uncertain(Capture):
        def start(self, *args, **kwargs):
            self.recording = True
            raise RuntimeError('synthetic lost acknowledgement')
    native = Uncertain()
    capture = LeasedCapture(native, CaptureLease(), 'enrollment', maintenance=gate, participant_id='secretary')
    with pytest.raises(RuntimeError):
        capture.start('synthetic')
    assert gate.status()['active_capture'] == 1
    with pytest.raises(MaintenanceError, match='maintenance_capture_active'):
        gate.begin_barrier(owner_id='backup')
    capture.stop('synthetic')
    assert gate.status()['active_capture'] == 0


def test_restored_team_blocks_raw_autocommit_and_outbox_claim_even_without_optional_gate(tmp_path):
    original = TeamDatabase(tmp_path / 'restored.sqlite3')
    with original.connection() as conn:
        conn.execute('CREATE TABLE maintenance_restore_guard(id INTEGER PRIMARY KEY,restore_id TEXT,reconciliation_required INTEGER,manifest_sha256 TEXT)')
        conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,?,1,?)', (str(uuid4()), 'a' * 64))
    reopened = TeamDatabase(original.path)
    with reopened.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] >= 8
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute('DELETE FROM team_execution')
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute('DROP TABLE maintenance_restore_guard')
    with pytest.raises(sqlite3.DatabaseError):
        with reopened.transaction() as conn:
            conn.execute('INSERT INTO team_resources VALUES(?,?,?)', ('task:1', None, 0))


def test_http_admission_lasts_through_final_response_body_and_denies_new_intake(gate):
    async def run():
        messages = []
        async def handler(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            claim = gate.begin_barrier(owner_id='backup')
            assert len(gate.active_tickets(claim)) == 1
            await send({'type': 'http.response.body', 'body': b'durable result'})
            assert len(gate.active_tickets(claim)) == 1
        async def send(message):
            messages.append(message)
        app = MaintenanceMiddleware(handler, maintenance=gate, participant_id='secretary')
        await app({'type': 'http'}, None, send)
        assert gate.status()['active_tickets'] == 0
        await app({'type': 'http'}, None, send)
        assert [item['status'] for item in messages if item['type'] == 'http.response.start'] == [200, 503]
    asyncio.run(run())


def runtime_config(tmp_path, **values):
    from secretary.team_settings import TeamRuntimeSettings
    fields = {'deployment_id': str(uuid4()), 'project_dir': tmp_path,
        'secretary_database_path': tmp_path / 'source.sqlite3', 'team_database_path': tmp_path / 'team.sqlite3',
        'billing_database_path': tmp_path / 'shared-billing.sqlite3', 'vikunja_database_path': tmp_path / 'native.sqlite3',
        'control_database_path': tmp_path / 'control.sqlite3'}
    return TeamRuntimeSettings.model_validate({**fields, **values})


def identity():
    return {'pid': 123, 'creation_time': '134040000000000000',
        'executable_sha256': 'a' * 64, 'argv_sha256': 'b' * 64}


def test_explicit_secretary_factory_uses_shared_paths_without_owner_setup(tmp_path):
    from secretary.orchestration.secretary_team_runtime import create_secretary_team_app
    config = runtime_config(tmp_path)
    app = create_secretary_team_app(config, identity_reader=identity, run_worker=False)
    assert app.state.db.path == config.secretary_database_path
    assert app.state.cloud_budget.repository.path == config.billing_database_path
    assert app.state.team_directory is None and app.state.team_auth is None
    assert app.state.task_publications is None
    assert not config.team_database_path.exists()
    assert app.state.capture._claimed is None
    assert app.state.enrollment_capture._claimed is None
    assert app.state.settings.cloud_enabled is False
    assert app.state.maintenance.status()['active_tickets'] == 0
    import json
    descriptor = json.loads((config.control_database_path.parent / 'secretary-participant.json').read_text())
    assert descriptor == app.state.maintenance.participant_descriptor('secretary', role='secretary')
    assert descriptor['run_id'] == app.state.team_runtime_run_id
    assert not list(config.control_database_path.parent.glob('*.tmp'))


def test_main_factory_does_not_replace_live_idle_predecessor(tmp_path, monkeypatch):
    from secretary.orchestration.secretary_team_runtime import create_secretary_team_app
    from secretary.infrastructure import team_process_identity
    config = runtime_config(tmp_path)
    original = create_secretary_team_app(config, identity_reader=identity, run_worker=False,
                                         enforce_single_instance=False)
    before = config.secretary_database_path.read_bytes()
    descriptor_path = config.control_database_path.parent / 'secretary-participant.json'
    descriptor = descriptor_path.read_bytes()
    def alive(value):
        assert value == identity()
        raise ValueError('process_still_alive')
    monkeypatch.setattr(team_process_identity, 'verify_dead_identity', alive)
    with pytest.raises(ValueError, match='process_still_alive'):
        create_secretary_team_app(config, identity_reader=identity, run_worker=False)
    assert original.state.maintenance.status()['active_tickets'] == 0
    assert config.secretary_database_path.read_bytes() == before
    assert descriptor_path.read_bytes() == descriptor


def test_main_factory_refuses_invalid_native_job_before_source_stores(tmp_path):
    from secretary.orchestration.secretary_team_runtime import create_secretary_team_app
    config = runtime_config(tmp_path)
    with pytest.raises(MaintenanceError, match='containment'):
        create_secretary_team_app(config, identity_reader=identity, native_job=object(), run_worker=False)
    assert not config.secretary_database_path.exists()
    assert not config.billing_database_path.exists()
    assert not (config.control_database_path.parent / 'secretary-participant.json').exists()


def test_main_factory_recovers_dead_background_receipt_before_new_registration(tmp_path, monkeypatch):
    from secretary.orchestration.secretary_team_runtime import create_secretary_team_app
    from secretary.infrastructure import team_process_identity, team_runtime_recovery
    from secretary.infrastructure.team_process_identity import DeadProcessProof
    config = runtime_config(tmp_path)
    original = create_secretary_team_app(config, identity_reader=identity, run_worker=False)
    gate = original.state.maintenance
    old = gate.enter('secretary', 'background_job', str(uuid4()))
    meeting = original.state.db.create_meeting('synthetic interrupted local operation')
    job = original.state.db.enqueue(meeting['id'], 'prepare')
    original.state.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
    proof = DeadProcessProof(**identity(), state='exited', observed_at='2026-10-04T12:00:00+00:00')
    new_identity = {**identity(), 'pid':456, 'creation_time':'134040000000000001'}
    def dead(value):
        assert value == identity()
        return proof
    monkeypatch.setattr(team_process_identity, 'verify_dead_identity', dead)
    monkeypatch.setattr(team_runtime_recovery, 'verify_dead_identity', dead)
    monkeypatch.setattr(team_runtime_recovery, 'process_identity', lambda: new_identity)
    # This fixture simulates the trusted old-launch containment receipt; it
    # does not assign this pytest process to a native Windows Job Object.
    monkeypatch.setattr(MaintenanceRepository, 'has_native_containment',
        lambda self, participant_id, run_id: True, raising=False)
    replacement = create_secretary_team_app(config, identity_reader=lambda:new_identity, run_worker=False,
                                            enforce_single_instance=False)
    assert replacement.state.db.job(job['id'])['status'] == 'queued'
    assert gate.active_operations() == ()
    assert replacement.state.team_runtime_run_id != old.run_id
    assert next(row for row in gate.status()['participants'] if row['participant_id']=='secretary')['identity'] == new_identity
    with sqlite3.connect(gate.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM maintenance_recovery_checkpoints').fetchone()[0] == 1


def test_main_recovery_refuses_native_work_from_unqualified_old_launch(tmp_path, monkeypatch):
    from secretary.orchestration.secretary_team_runtime import create_secretary_team_app
    from secretary.infrastructure import team_process_identity, team_runtime_recovery
    from secretary.infrastructure.team_process_identity import DeadProcessProof
    config = runtime_config(tmp_path)
    original = create_secretary_team_app(config, identity_reader=identity, run_worker=False)
    gate = original.state.maintenance
    ticket = gate.enter('secretary', 'background_job', str(uuid4()))
    meeting = original.state.db.create_meeting('synthetic unqualified native preparation')
    job = original.state.db.enqueue(meeting['id'], 'prepare')
    original.state.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
    proof = DeadProcessProof(**identity(), state='exited', observed_at='2026-10-04T12:00:00+00:00')
    monkeypatch.setattr(team_process_identity, 'verify_dead_identity', lambda value: proof)
    monkeypatch.setattr(team_runtime_recovery, 'verify_dead_identity', lambda value: proof)
    monkeypatch.setattr(team_runtime_recovery, 'process_identity',
        lambda: pytest.fail('new recovery actor must not start before native qualification'))
    with pytest.raises(MaintenanceError, match='containment'):
        create_secretary_team_app(config, identity_reader=lambda:{**identity(), 'pid':456}, run_worker=False)
    assert original.state.db.job(job['id'])['status'] == 'running'
    assert gate.active_operations('secretary') == (ticket,)
    assert next(row for row in gate.status()['participants'] if row['participant_id']=='secretary')['run_id'] == ticket.run_id


def test_participant_descriptor_does_not_replace_foreign_deployment(tmp_path, gate):
    from secretary.infrastructure.maintenance_binding import write_participant_descriptor
    gate.bind_sources({role: tmp_path / (role + '.sqlite3') for role in ('secretary','team','billing','vikunja')})
    target = gate.path.parent / 'secretary-participant.json'
    original = '{"deployment_id":"foreign", "role":"secretary"}'
    target.write_text(original)
    with pytest.raises(ValueError, match='maintenance_descriptor_ownership_mismatch'):
        write_participant_descriptor(gate, 'secretary', role='secretary')
    assert target.read_text() == original


@pytest.mark.parametrize('role', ['secretary', 'team', 'billing'])
def test_owner_factory_refuses_restored_store_before_other_store_or_registration(tmp_path, role):
    from secretary.orchestration.secretary_team_runtime import create_secretary_team_app
    from secretary.team_settings import RuntimeConfigurationError
    config = runtime_config(tmp_path)
    guarded = getattr(config, role + '_database_path')
    with sqlite3.connect(guarded) as conn:
        conn.execute('CREATE TABLE maintenance_restore_guard(id INTEGER PRIMARY KEY,reconciliation_required INTEGER)')
        conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,1)')
    before = guarded.read_bytes()
    with pytest.raises(RuntimeConfigurationError, match='team_restore_reconciliation_required'):
        create_secretary_team_app(config, identity_reader=lambda: pytest.fail('registered restored runtime'), run_worker=False)
    assert guarded.read_bytes() == before
    assert not config.control_database_path.exists()
    assert not any(getattr(config, other + '_database_path').exists()
        for other in ('secretary', 'team', 'billing', 'vikunja') if other != role)


def test_owner_local_invitation_factory_uses_real_auth_repository(tmp_path):
    from secretary.orchestration.secretary_team_runtime import create_secretary_team_app
    from secretary.infrastructure.team_repository import TeamRepository
    from secretary.domain.team import TeamMember
    from starlette.testclient import TestClient
    owner_id = str(uuid4())
    config = runtime_config(tmp_path, local_owner_id=owner_id, team_auth_secret='synthetic-key-32-bytes-for-local-auth')
    team = TeamRepository(TeamDatabase(config.team_database_path))
    team.upsert_member(TeamMember(id=owner_id, display_name='Synthetic owner', role='owner',
        max_user_id='11', vikunja_user_id='22', project_ids=('1',)), expected_revision=None)
    app = create_secretary_team_app(config, identity_reader=identity, run_worker=False)
    # ASGI only: no actual loopback listener or client creation side effects.
    client = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 40001))
    token = client.get('/api/v1/session').json()['csrf_token']
    response = client.post('/api/v1/team/invitations', json={'project_ids': ['1']}, headers={'x-secretary-token': token})
    assert response.status_code == 201
    invitation = client.get('/api/v1/team/invitations/' + response.json()['id'])
    assert invitation.status_code == 200 and invitation.json()['project_ids'] == ['1']
    assert app.state.maintenance.status()['active_tickets'] == 0


def test_main_asgi_returns_503_during_shared_maintenance(tmp_path):
    from secretary.orchestration.secretary_team_runtime import create_secretary_team_app
    from starlette.testclient import TestClient
    app = create_secretary_team_app(runtime_config(tmp_path), identity_reader=identity, run_worker=False)
    app.state.maintenance.begin_barrier(owner_id='backup')
    response = TestClient(app, base_url='http://127.0.0.1').get('/api/v1/session')
    assert response.status_code == 503 and response.json() == {'detail': 'maintenance_blocked'}


def test_distinct_secretary_launch_ids_cannot_share_stop_marker(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('t12_capture_launcher', Path(__file__).parents[1] / 'scripts/run_server.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    first, second = str(uuid4()), str(uuid4())
    assert launcher.stop_marker(tmp_path, first) != launcher.stop_marker(tmp_path, second)
    assert launcher.stop_marker(tmp_path, first).parent == tmp_path / '.runtime'
    with pytest.raises(ValueError):
        launcher.stop_marker(tmp_path, '../other')


@pytest.mark.asyncio
async def test_foreign_secretary_port_is_refused_before_settings_or_runtime_factory(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('t12_capture_port_launcher', Path(__file__).parents[1] / 'scripts/run_server.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    calls = []
    class Foreign:
        def setsockopt(self, *args):
            pass
        def bind(self, address):
            calls.append(address)
            raise OSError('synthetic foreign occupied port')
        def close(self):
            calls.append('closed')
    monkeypatch.setattr(launcher.socket, 'socket', lambda *args: Foreign())
    from secretary import team_settings
    monkeypatch.setattr(team_settings, 'load_team_settings', lambda *args: pytest.fail('config touched before bind'))
    with pytest.raises(OSError):
        await launcher.run(18765, tmp_path / 'not-read.json', str(uuid4()))
    assert calls == [('127.0.0.1', 18765), 'closed']


@pytest.mark.parametrize('code', ['maintenance_blocked', 'restore_reconciliation_required', 'runtime_outbound_disabled'])
def test_unsubmitted_processing_attempt_waits_configuration_without_unknown_charge(tmp_path, gate, code):
    from secretary.application.worker import Worker
    from secretary.infrastructure.polza import ProviderError
    from secretary.settings import Settings
    from test_backend import prepared, FakeProvider
    settings = Settings(_env_file=None, data_dir=tmp_path / 'data', polza_api_key='synthetic',
        stt_price_rub_per_minute=0.05)
    db = Database(tmp_path / 'source.sqlite3', maintenance=gate, participant_id='secretary')
    meeting, chunk = prepared(db, settings)
    job = db.enqueue(meeting['id'], 'transcribe', chunk_id=chunk['id'])
    class Rejected(FakeProvider):
        async def transcribe(self, *args, **kwargs):
            gate.begin_barrier(owner_id='backup')
            raise ProviderError(code, 'Request not submitted')
    worker = Worker(db, settings, Rejected, recovery=False)
    assert asyncio.run(worker.run_once()) is True
    assert gate.status()['active_tickets'] == 0
    with sqlite3.connect(db.path) as conn:
        assert conn.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'waiting_config'
        assert conn.execute('SELECT status FROM usage WHERE job_id=?', (job['id'],)).fetchone()[0] == 'released'


def test_worker_close_waits_for_real_operation_without_three_second_cancellation(tmp_path):
    from secretary.application.worker import Worker
    from secretary.settings import Settings
    from test_backend import FakeProvider
    async def run():
        worker = Worker(Database(tmp_path / 'source.sqlite3'), Settings(_env_file=None), FakeProvider, recovery=False)
        release = asyncio.Event()
        worker.task = asyncio.create_task(release.wait())
        closing = asyncio.create_task(worker.close())
        await asyncio.sleep(3.1)
        assert not closing.done() and not worker.task.cancelled()
        release.set()
        await closing
    asyncio.run(run())


def test_repeated_processing_cancel_keeps_job_admission_until_native_final_receipt(tmp_path, gate):
    from secretary.application.worker import Worker
    from secretary.settings import Settings
    from test_backend import FakeProvider
    started, release = Event(), Event()
    db = Database(tmp_path / 'source.sqlite3', maintenance=gate, participant_id='secretary')
    worker = Worker(db, Settings(_env_file=None), FakeProvider, recovery=False)
    def completion():
        started.set()
        assert release.wait(3)
        db.create_meeting('native job committed after repeated cancellation')
    async def owned():
        await asyncio.to_thread(completion)
        return True
    worker._run_once = owned
    async def run():
        running = asyncio.create_task(worker.run_once())
        try:
            assert await asyncio.to_thread(started.wait, 3)
            running.cancel()
            await asyncio.sleep(.02)
            running.cancel()
            await asyncio.sleep(.02)
            claim = gate.begin_barrier(owner_id='backup')
            assert not running.done() and len(gate.active_tickets(claim)) == 1
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert gate.active_tickets(claim) == ()
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)
    asyncio.run(run())


def test_repeated_worker_close_cancel_still_drains_local_and_processing_tasks(tmp_path):
    from secretary.application.worker import Worker
    from secretary.settings import Settings
    from test_backend import FakeProvider
    async def run():
        worker = Worker(Database(tmp_path / 'source.sqlite3'), Settings(_env_file=None), FakeProvider, recovery=False)
        local_release, main_release = asyncio.Event(), asyncio.Event()
        worker.local_task = asyncio.create_task(local_release.wait())
        worker.task = asyncio.create_task(main_release.wait())
        closing = asyncio.create_task(worker.close())
        try:
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(.02)
            closing.cancel()
            await asyncio.sleep(.02)
            assert not closing.done() and not worker.local_task.cancelled()
            local_release.set()
            await asyncio.sleep(.02)
            assert not closing.done() and not worker.task.cancelled()
            main_release.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert worker.local_task.done() and worker.task.done()
        finally:
            local_release.set()
            main_release.set()
            await asyncio.gather(closing, worker.task, worker.local_task, return_exceptions=True)
    asyncio.run(run())


def test_worker_close_preserves_local_error_after_main_receipt_is_committed(tmp_path):
    from secretary.application.worker import Worker
    from secretary.settings import Settings
    from test_backend import FakeProvider
    async def run():
        db = Database(tmp_path / 'source.sqlite3')
        worker = Worker(db, Settings(_env_file=None), FakeProvider, recovery=False)
        release = asyncio.Event()
        async def local():
            raise RuntimeError('synthetic local cleanup failure')
        async def main():
            await release.wait()
            db.create_meeting('durable final worker receipt')
        worker.local_task = asyncio.create_task(local())
        worker.task = asyncio.create_task(main())
        closing = asyncio.create_task(worker.close())
        try:
            await asyncio.sleep(.03)
            assert not closing.done()
            release.set()
            with pytest.raises(RuntimeError, match='synthetic local cleanup failure'):
                await closing
            assert worker.task.done() and db.one('SELECT COUNT(*) AS n FROM meetings')['n'] == 1
        finally:
            release.set()
            await asyncio.gather(closing, worker.task, worker.local_task, return_exceptions=True)
    asyncio.run(run())


def test_sse_client_releases_admission_when_backup_begins(tmp_path):
    from secretary.orchestration.secretary_team_runtime import create_secretary_team_app
    import httpx
    async def run():
        app = create_secretary_team_app(runtime_config(tmp_path), identity_reader=identity, run_worker=False)
        meeting = app.state.db.create_meeting('synthetic')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            request = asyncio.create_task(client.get('/api/v1/meetings/' + meeting['id'] + '/events'))
            for _ in range(100):
                if app.state.maintenance.status()['active_tickets']:
                    break
                await asyncio.sleep(.01)
            assert app.state.maintenance.status()['active_tickets'] == 1
            claim = app.state.maintenance.begin_barrier(owner_id='backup')
            response = await asyncio.wait_for(request, 3)
            assert response.status_code == 200
            assert app.state.maintenance.active_tickets(claim) == ()
    asyncio.run(run())


def test_publication_cancellation_retains_ticket_until_owned_thread_finishes(gate):
    async def run():
        entered, release = Event(), Event()
        class Dispatcher:
            def run_once(self, worker_id):
                entered.set()
                assert release.wait(5)
                with gate.admission('secretary', 'sql'):
                    assert gate.status()['active_tickets'] == 1
                return 'durable receipt'
        worker = PublicationWorker(Dispatcher(), worker_id='synthetic', maintenance=gate, participant_id='secretary')
        task = asyncio.create_task(worker.run_once())
        assert await asyncio.to_thread(entered.wait, 2)
        claim = gate.begin_barrier(owner_id='backup')
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done() and len(gate.active_tickets(claim)) == 1
        waiting = asyncio.create_task(worker.run_once())
        await asyncio.sleep(0)
        assert not waiting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await waiting is None
        assert gate.active_tickets(claim) == ()
    asyncio.run(run())
