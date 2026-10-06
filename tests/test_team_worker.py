"""T4 worker ownership and captured-plan regressions; isolated DB, no network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from importlib import import_module
from threading import Event
from uuid import uuid4

import pytest

from test_team_repository import api, case, command
from test_team_tasks import Remote

TeamWorker = import_module('secretary.orchestration.team_worker').TeamWorker


class BlockingWork:
    def __init__(self, c):
        self.c, self.calls = c, 0
        self.entered, self.release, self.finished = Event(), Event(), Event()

    def execute(self, claim):
        self.calls += 1
        self.entered.set()
        try:
            assert self.release.wait(5), 'Synthetic work must always be released'
            snapshot = self.c.snapshot.model_copy(update={'revision': 1, 'bucket': 'doing',
                                                          'remote_fingerprint': 'b' * 64})
            return self.c.repo.record_remote_result(claim, state='applied', snapshot=snapshot)
        finally:
            self.finished.set()


async def wait_event(event):
    assert await asyncio.to_thread(event.wait, 3)


async def release_and_drain(work, running):
    work.release.set()
    await wait_event(work.finished)
    await asyncio.gather(running, return_exceptions=True)


def test_repeated_asyncio_cancellation_keeps_owner_until_sync_work_finishes(case):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    service = BlockingWork(c)
    worker = TeamWorker(c.repo, service, worker_id='review-worker', lease_seconds=3)

    async def scenario():
        running = asyncio.create_task(worker.run_once())
        try:
            await wait_event(service.entered)
            running.cancel()
            await asyncio.sleep(.02)
            assert not running.done(), 'First cancellation must retain ownership'
            running.cancel()
            await asyncio.sleep(.02)
            assert not running.done(), 'Repeated cancellation detached running synchronous mutation'
        finally:
            await release_and_drain(service, running)

    asyncio.run(scenario())
    assert service.calls == 1
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).execution_state.state == 'applied'


def test_single_cancellation_waits_for_owned_work_and_preserves_result(case):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    service = BlockingWork(c)
    worker = TeamWorker(c.repo, service, worker_id='review-worker', lease_seconds=3)

    async def scenario():
        running = asyncio.create_task(worker.run_once())
        try:
            await wait_event(service.entered)
            running.cancel()
            await asyncio.sleep(.02)
            assert not running.done() and not service.finished.is_set()
        finally:
            await release_and_drain(service, running)
        assert running.cancelled()

    asyncio.run(scenario())
    assert service.calls == 1
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).execution_state.state == 'applied'


def test_cancelled_claim_thread_cannot_abandon_already_committed_ownership(case, monkeypatch):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    committed, release_claim, claim_returned = Event(), Event(), Event()
    original = c.repo.claim_command

    def delayed_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        committed.set()
        try:
            assert release_claim.wait(5)
            return result
        finally:
            claim_returned.set()

    monkeypatch.setattr(c.repo, 'claim_command', delayed_claim)
    service = BlockingWork(c)
    service.release.set()
    worker = TeamWorker(c.repo, service, worker_id='review-worker', lease_seconds=3)

    async def scenario():
        running = asyncio.create_task(worker.run_once())
        try:
            await wait_event(committed)
            running.cancel()
            await asyncio.sleep(.02)
            assert not running.done(), 'Cancelled to_thread dropped ownership of a committed claim'
        finally:
            release_claim.set()
            await wait_event(claim_returned)
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).execution_state.state != 'running'
    assert service.calls <= 1


def test_stop_during_recovery_prevents_new_claim_and_dispatch(case, monkeypatch):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    recovering, release_recovery = Event(), Event()
    original = c.repo.recover_expired

    def recover():
        recovering.set()
        assert release_recovery.wait(5)
        return original()

    monkeypatch.setattr(c.repo, 'recover_expired', recover)
    service = BlockingWork(c)
    service.release.set()
    worker = TeamWorker(c.repo, service, worker_id='review-worker', lease_seconds=3)

    async def scenario():
        running = asyncio.create_task(worker.run_once())
        try:
            await wait_event(recovering)
            worker.stop()
        finally:
            release_recovery.set()
            await asyncio.gather(running, return_exceptions=False)

    asyncio.run(scenario())
    assert service.calls == 0, 'stop requested before claiming must prevent new intake'
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).execution_state.state == 'queued'


def test_heartbeat_renews_while_sync_work_blocks_and_event_loop_remains_responsive(case, monkeypatch):
    c = case
    c.repo.clock = lambda: datetime.now(timezone.utc)
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    service = BlockingWork(c)
    heartbeat_seen = Event()
    original = c.repo.renew_claim

    def renew(claim, **kwargs):
        result = original(claim, **kwargs)
        heartbeat_seen.set()
        return result

    monkeypatch.setattr(c.repo, 'renew_claim', renew)
    worker = TeamWorker(c.repo, service, worker_id='review-worker', lease_seconds=3)

    async def scenario():
        running = asyncio.create_task(worker.run_once())
        try:
            await wait_event(service.entered)
            await asyncio.wait_for(asyncio.sleep(.02), timeout=.2)
            await wait_event(heartbeat_seen)
            assert not running.done() and not service.finished.is_set()
            assert c.repo.get_receipt(c.owner.id, cmd.operation_id).execution_state.state == 'running'
        finally:
            await release_and_drain(service, running)

    asyncio.run(scenario())
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).execution_state.state == 'applied'


def test_unexpected_failure_is_uncertain_and_next_intake_never_replays_it(case):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)

    class BrokenWork:
        calls = 0

        def execute(self, claim):
            self.calls += 1
            raise OSError('SYNTHETIC PRIVATE RESPONSE')

    service = BrokenWork()
    worker = TeamWorker(c.repo, service, worker_id='review-worker', lease_seconds=3)

    async def scenario():
        result = await worker.run_once()
        assert result.execution_state.state == 'uncertain'
        assert result.execution_state.error_code == 'team_worker_failure'
        assert await worker.run_once() is None

    asyncio.run(scenario())
    assert service.calls == 1


def test_expired_owned_service_cannot_dispatch_next_remote_step(case):
    c = case
    remote = Remote(c)
    cmd = c.api.TaskCommand(operation_id=str(uuid4()), project_id='7', task_id=c.snapshot.task_id,
        expected_revision=0, expected_fingerprint=c.snapshot.remote_fingerprint, action='classify',
        values={'important': True, 'urgent': True, 'classification_confirmed': True})
    c.repo.accept_command(c.owner.id, cmd)

    def expire_after_first_mutation(kind):
        if kind == 'add_label':
            c.time[0] += timedelta(seconds=4)
            c.repo.recover_expired()

    remote.hook = expire_after_first_mutation
    service = import_module('secretary.application.team_tasks').TeamTaskService(c.repo, remote, lease_seconds=3)
    worker = TeamWorker(c.repo, service, worker_id='review-worker', lease_seconds=3)
    assert asyncio.run(worker.run_once()) is None
    assert [call[0] for call in remote.calls].count('add_label') == 1
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).execution_state.state == 'uncertain'
    assert asyncio.run(worker.run_once()) is None


def claim_for(c):
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    return c.repo.claim_command('review-worker')


def journal_count(c, operation_id):
    with c.db.connection() as conn:
        return conn.execute('SELECT COUNT(*) FROM team_journal WHERE operation_id=?',
                            (operation_id,)).fetchone()[0]


def test_saved_plan_remains_bound_to_its_hash_during_caller_mutation(case, monkeypatch):
    c = case
    claim = claim_for(c)
    steps = [{'kind': 'move_bucket', 'bucket': 'doing'}]
    encoded, release = Event(), Event()
    original_transaction = c.db.transaction

    @contextmanager
    def pause_after_encoding():
        encoded.set()
        assert release.wait(5)
        with original_transaction() as conn:
            yield conn

    monkeypatch.setattr(c.db, 'transaction', pause_after_encoding)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(c.repo.save_remote_plan, claim, steps)
        assert encoded.wait(5)
        steps[0]['bucket'] = 'blocked'
        release.set()
        saved = result.result(timeout=5)
    assert saved['plan_hash'] == c.api.content_hash(saved['plan'])
    assert saved['plan'] == [{'kind': 'move_bucket', 'bucket': 'doing'}]


def test_captured_plan_still_enforces_step_limit_after_input_changes(case, monkeypatch):
    c = case
    claim = claim_for(c)
    steps = [{'kind': 'move_bucket', 'bucket': 'doing'} for _ in range(32)]
    module = import_module(type(c.repo).__module__)
    original_canonical = module.canonical
    checked, release = Event(), Event()

    def pause_at_capture(value):
        if value is steps:
            checked.set()
            assert release.wait(5)
        return original_canonical(value)

    monkeypatch.setattr(module, 'canonical', pause_at_capture)
    before = journal_count(c, claim.command.operation_id)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(c.repo.save_remote_plan, claim, steps)
        assert checked.wait(5)
        steps.append({'kind': 'patch_done', 'done': False})
        release.set()
        with pytest.raises(ValueError, match='invalid_remote_plan'):
            result.result(timeout=5)
    assert journal_count(c, claim.command.operation_id) == before
