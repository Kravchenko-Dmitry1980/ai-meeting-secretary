"""Voice ownership remains on one event loop and survives repeated cancellation."""
import asyncio
from threading import Event
from types import SimpleNamespace

import pytest


class Repository:
    def __init__(self):
        self.trace = []
        self.next = 0

    def recover_expired(self):
        self.trace.append('recover')

    def claim_job(self, worker, **options):
        self.next += 1
        self.trace.append('claim')
        return SimpleNamespace(job_id=str(self.next))

    def authorize_job(self, claim):
        self.trace.append('authorize')
        return claim

    def renew_job(self, claim, **options):
        self.trace.append('renew')


def worker_type():
    from secretary.orchestration.voice_worker import VoiceWorker
    return VoiceWorker


def test_async_voice_calls_share_owner_loop_and_stop_prevents_new_intake():
    async def scenario():
        repo, calls = Repository(), []
        owner_loop = asyncio.get_running_loop()
        async def execute(claim, *, cancelled):
            assert asyncio.get_running_loop() is owner_loop
            assert not cancelled()
            calls.append(claim.job_id)
            return claim.job_id
        worker = worker_type()(repo, SimpleNamespace(execute=execute), worker_id='synthetic-worker')
        assert await worker.run_once() == '1'
        assert await worker.run_once() == '2'
        worker.stop()
        assert await worker.run_once() is None and calls == ['1', '2']
    asyncio.run(scenario())


def test_repeated_cancellation_drains_async_request_and_its_owned_native_thread():
    async def scenario():
        repo = Repository()
        started, release, ended = Event(), Event(), Event()
        observed = []
        def native():
            started.set()
            assert release.wait(3), 'bounded synthetic release missing'
            ended.set()
        async def execute(claim, *, cancelled):
            await asyncio.to_thread(native)
            observed.append(cancelled())
        worker = worker_type()(repo, SimpleNamespace(execute=execute), worker_id='synthetic-worker')
        task = asyncio.create_task(worker.run_once())
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and not ended.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set() and observed == [True]
    asyncio.run(scenario())


def test_stop_during_recovery_prevents_paid_work_claim():
    async def scenario():
        repo = Repository()
        started, release = Event(), Event()
        def recovery():
            started.set()
            assert release.wait(3)
        repo.recover_expired = recovery
        async def execute(*args, **kwargs):
            pytest.fail('work dispatched after intake stop')
        worker = worker_type()(repo, SimpleNamespace(execute=execute), worker_id='synthetic-worker')
        task = asyncio.create_task(worker.run_once())
        assert await asyncio.to_thread(started.wait, 2)
        worker.stop()
        release.set()
        assert await task is None and repo.next == 0
    asyncio.run(scenario())


def test_unexpected_failure_preserves_recoverable_claim_instead_of_false_success():
    async def scenario():
        repo = Repository()
        async def execute(claim, **options):
            raise RuntimeError('synthetic local failure')
        worker = worker_type()(repo, SimpleNamespace(execute=execute), worker_id='synthetic-worker')
        with pytest.raises(RuntimeError):
            await worker.run_once()
        assert repo.next == 1
    asyncio.run(scenario())


def test_lease_loss_is_reported_before_a_second_cloud_stage():
    async def scenario():
        from secretary.domain.voice_commands import VoiceError
        repo = Repository()
        def denied(claim, **options):
            raise VoiceError('voice_lease_lost')
        repo.renew_job = denied
        async def execute(claim, *, cancelled):
            await asyncio.sleep(1.05)
            assert cancelled(), 'lost lease cannot authorize a new cloud stage'
        worker = worker_type()(repo, SimpleNamespace(execute=execute), worker_id='synthetic-worker',
                               lease_seconds=3)
        await worker.run_once()
    asyncio.run(scenario())


def test_concurrent_runs_do_not_overlap_owned_voice_jobs():
    async def scenario():
        repo = Repository()
        active, maximum = 0, 0
        async def execute(claim, *, cancelled):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
        worker = worker_type()(repo, SimpleNamespace(execute=execute), worker_id='synthetic-worker')
        await asyncio.gather(worker.run_once(), worker.run_once())
        assert repo.next == 2 and maximum == 1
    asyncio.run(scenario())


def test_known_revocation_does_not_stop_next_actor_job():
    async def scenario():
        from secretary.domain.voice_commands import VoiceError
        repo = Repository()
        async def execute(claim, *, cancelled):
            if claim.job_id == '1':
                raise VoiceError('voice_actor_changed')
            return 'next actor proceeds'
        worker = worker_type()(repo, SimpleNamespace(execute=execute), worker_id='synthetic-worker')
        assert await worker.run_once() is None
        assert await worker.run_once() == 'next actor proceeds'
    asyncio.run(scenario())
