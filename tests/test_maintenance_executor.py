"""An owned background thread remains inside the backup barrier until completion."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
from threading import Event
from uuid import uuid4

import pytest

from secretary.infrastructure.database import Database
from secretary.infrastructure.team_maintenance import MaintenanceRepository, MaintenanceBlocked, MaintenanceError


@pytest.fixture
def owned(tmp_path):
    gate = MaintenanceRepository(tmp_path / 'control.sqlite3', str(uuid4()))
    gate.register_participant('secretary', run_id=str(uuid4()), identity={
        'pid': 123, 'creation_time': '1', 'executable_sha256': 'a' * 64, 'argv_sha256': 'b' * 64})
    db = Database(tmp_path / 'source.sqlite3', maintenance=gate, participant_id='secretary')
    return gate, db


def executor(gate):
    from secretary.infrastructure.maintenance_executor import MaintenanceExecutor
    return MaintenanceExecutor(ThreadPoolExecutor(max_workers=1), maintenance=gate, participant_id='secretary')


def test_background_file_and_sql_completion_is_owned_during_barrier(tmp_path, owned):
    gate, db = owned
    pool = executor(gate)
    started, release = Event(), Event()
    def finish():
        (tmp_path / 'asset.txt').write_text('complete synthetic asset')
        started.set()
        assert release.wait(3)
        db.create_meeting('durable completion after admission')
        return 'completed'
    future = pool.submit(finish)
    try:
        assert started.wait(3)
        claim = gate.begin_barrier(owner_id='backup')
        assert [ticket.kind for ticket in gate.active_tickets(claim)] == ['background_job']
        with pytest.raises(MaintenanceBlocked):
            pool.submit(lambda: pytest.fail('new background work was admitted'))
        release.set()
        assert future.result(timeout=3) == 'completed'
        assert gate.active_tickets(claim) == ()
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_cancelled_queued_job_leaves_ticket_but_running_job_keeps_ownership(owned):
    gate, _ = owned
    pool = executor(gate)
    started, release = Event(), Event()
    first = pool.submit(lambda: (started.set(), release.wait(3)))
    try:
        assert started.wait(3)
        queued = pool.submit(lambda: pytest.fail('cancelled job ran'))
        assert len(gate.active_operations()) == 2
        assert queued.cancel()
        assert len(gate.active_operations()) == 1
        assert not first.cancel()
        release.set()
        first.result(timeout=3)
        assert gate.active_operations() == ()
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_job_exception_and_submit_failure_do_not_leak_tickets(owned):
    gate, _ = owned
    pool = executor(gate)
    def fail():
        raise ValueError('synthetic processing failure')
    with pytest.raises(ValueError):
        pool.submit(fail).result(timeout=3)
    assert gate.active_operations() == ()
    pool.shutdown(wait=True, cancel_futures=True)
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)
    assert gate.active_operations() == ()


def test_enrollment_service_uses_owned_executor(owned, tmp_path):
    from secretary.application.enrollments import EnrollmentService
    from secretary.infrastructure.maintenance_executor import MaintenanceExecutor
    from secretary.infrastructure.voice_resources import InferenceCoordinator
    from secretary.settings import Settings
    gate, db = owned
    service = EnrollmentService(Settings(_env_file=None, data_dir=tmp_path / 'private'),
        db, None, InferenceCoordinator(), store=type('Store', (), {})())
    assert isinstance(service._executor, MaintenanceExecutor)
    service._executor.shutdown(wait=True, cancel_futures=True)


def test_capture_completion_keeps_admission_until_receipt_and_background_submission(owned):
    from secretary.infrastructure.voice_resources import CaptureLease, LeasedCapture
    gate, db = owned
    pool = executor(gate)
    finished = Event()
    class Native:
        def start(self, identifier, microphone, system, on_chunk, on_error, on_finish=None):
            self.finished = on_finish
            return {'recording': True, 'native_closed': False}
    native = Native()
    capture = LeasedCapture(native, CaptureLease(), 'enrollment', maintenance=gate, participant_id='secretary')
    def receipt(snapshot):
        assert capture.lease.owner is None  # device is closed, file cleanup is now safe
        with pytest.raises(MaintenanceError, match='maintenance_capture_active'):
            gate.begin_barrier(owner_id='backup')
        # A simultaneous stop/state completion must not drop callback ownership.
        capture._release(identifier, snapshot)
        assert gate.status()['active_capture'] == 1
        db.create_meeting('native closure callback receipt')
        pool.submit(finished.set).result(timeout=3)
    identifier = str(uuid4())
    try:
        capture.start(identifier, on_finish=receipt)
        native.finished({'recording': False, 'native_closed': True})
        assert finished.is_set() and gate.active_operations() == ()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def test_cancelled_http_waits_for_its_native_completion_before_leaving(owned, tmp_path):
    from secretary.interface.maintenance_middleware import MaintenanceMiddleware
    gate, db = owned
    started, release, finished = Event(), Event(), Event()
    def native():
        started.set()
        assert release.wait(3)
        (tmp_path / 'final-asset.bin').write_bytes(b'synthetic owned asset')
        db.create_meeting('final receipt after disconnected request')
        finished.set()
    async def app(scope, receive, send):
        await asyncio.to_thread(native)
    async def run():
        request = asyncio.create_task(MaintenanceMiddleware(app, maintenance=gate,
            participant_id='secretary')({'type':'http'}, None, None))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            request.cancel()
            await asyncio.sleep(.02)
            request.cancel()
            await asyncio.sleep(.02)
            claim = gate.begin_barrier(owner_id='backup')
            assert len(gate.active_tickets(claim)) == 1 and not request.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert finished.is_set() and gate.active_tickets(claim) == ()
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
    asyncio.run(run())
