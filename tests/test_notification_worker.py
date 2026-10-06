"""Thread ownership/fencing probes. No provider or runtime database is used."""
import asyncio
from datetime import datetime, timedelta, timezone
from importlib import import_module, util
from threading import Event
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from secretary.domain.bot import BotSend, SendReceipt
from secretary.domain.notifications import (NotificationAuthorization, NotificationClaim,
    NotificationDeferred, NotificationError, NotificationIntent, NotificationTaskRef)
from secretary.domain.team import TaskSnapshot, TeamMember


def worker_type():
    assert util.find_spec('secretary.orchestration.notification_worker'), 'T10 worker absent'
    return import_module('secretary.orchestration.notification_worker').NotificationWorker


class Repository:
    def __init__(self):
        self.now = datetime(2026, 10, 5, 9, tzinfo=timezone.utc)
        self.member = TeamMember(id=str(uuid5(NAMESPACE_URL, 'synthetic-member')),
            display_name='Synthetic', max_user_id='39', vikunja_user_id='29', project_ids=('7',))
        self.task = TaskSnapshot(task_id='9', project_id='7', revision=0, remote_fingerprint='a'*64,
            title='Synthetic', assignee_id=self.member.id)
        self.intent = NotificationIntent(notification_id=str(uuid5(NAMESPACE_URL, 'synthetic-notification')),
            group_id=str(uuid5(NAMESPACE_URL, 'synthetic-group')), dedup_key='synthetic',
            recipient_id=self.member.id, recipient_revision=0, rule='daily_digest', scheduled_at=self.now,
            task_refs=(NotificationTaskRef(project_id='7',task_id='9',due_revision=0),),
            project_ids=('7',), canonical_verified_at=self.now, text='PREVIEW_ONLY')
        self.claim_value = NotificationClaim(id=self.intent.notification_id, intent=self.intent,
            worker_id='worker', fence=1, lease_until=self.now+timedelta(seconds=60))
        self.state, self.calls, self.receipts = 'pending', [], []
        self.claimed, self.lost = False, False

    def check(self, claim):
        if self.lost or claim.fence != self.claim_value.fence:
            raise NotificationError('notification_claim_lost')

    def recover_expired(self):
        self.calls.append('recover')
        if self.lost and self.state == 'sending':
            self.state = 'uncertain'

    def claim(self, worker_id, *, lease_seconds):
        self.calls.append('claim')
        if self.state != 'pending' or self.claimed:
            return None
        self.claimed = True
        return self.claim_value

    def authorize(self, claim):
        self.calls.append('authorize')
        self.check(claim)
        return NotificationAuthorization(claim=claim, recipient=self.member, tasks=(self.task,))

    def renew(self, claim, *, lease_seconds):
        self.calls.append('renew')
        self.check(claim)
        return claim

    def mark_sending(self, claim, send, *, observed_fingerprints):
        self.check(claim)
        assert self.state == 'pending' and send.operation_id == claim.id
        assert observed_fingerprints == ((self.task.task_id,self.task.remote_fingerprint),)
        self.calls.append('mark')
        self.body, self.state = send, 'sending'
        return claim.model_copy(update={'state':'sending', 'attempt':1})

    def finish(self, claim, receipt):
        self.check(claim)
        assert claim.state == 'sending' and claim.attempt == 1 and self.state == 'sending'
        self.calls.append('finish')
        self.receipts.append(receipt)
        self.state = receipt.state

    def defer(self, claim, *, available_at, error_code):
        self.check(claim)
        assert self.state == 'pending'
        self.calls.append('defer')
        self.claimed = False
        self.error = error_code

    def cancel(self, claim, error_code):
        self.check(claim)
        assert self.state == 'pending'
        self.calls.append('cancel')
        self.state = 'cancelled'


class Sender:
    def __init__(self):
        self.posts = 0
        self.before_get, self.after_get, self.after_post = None, None, None
        self.receipt = None

    def dispatch(self, intent, *, authorize, before_send, cancelled):
        if self.before_get:
            self.before_get()
        current = authorize()
        if self.after_get:
            self.after_get()
        if cancelled():
            raise NotificationDeferred('notification_not_ready')
        send = BotSend(intent.notification_id, current.recipient.max_user_id, 'Synthetic current body')
        proof = tuple((task.task_id,task.remote_fingerprint) for task in current.tasks)
        before_send(send, proof)
        self.posts += 1
        if self.after_post:
            self.after_post()
        return self.receipt or SendReceipt(intent.notification_id, 'sent', message_id='synthetic-mid')


def setup():
    repository, sender = Repository(), Sender()
    worker = worker_type()(repository, sender, worker_id='worker', lease_seconds=3, poll_seconds=.01)
    return SimpleNamespace(repo=repository,sender=sender,worker=worker)


def test_mark_sending_precedes_exactly_one_post_and_finish_uses_new_attempt():
    c = setup()
    receipt = asyncio.run(c.worker.run_once())
    assert receipt.state == 'sent' and c.repo.state == 'sent' and c.sender.posts == 1
    assert c.repo.calls.index('mark') < c.repo.calls.index('finish')
    assert asyncio.run(c.worker.run_once()) is None and c.sender.posts == 1


@pytest.mark.parametrize('phase', ['recover','claim','get'])
def test_stop_during_preparation_never_dispatches(phase):
    c = setup()
    if phase == 'get':
        c.sender.after_get = c.worker.stop
    else:
        original = getattr(c.repo, phase if phase == 'claim' else 'recover_expired')
        def stop(*args, **kwargs):
            value = original(*args, **kwargs)
            c.worker.stop()
            return value
        setattr(c.repo, phase if phase == 'claim' else 'recover_expired', stop)
    asyncio.run(c.worker.run_once())
    assert c.sender.posts == 0 and 'mark' not in c.repo.calls
    assert c.repo.state == 'pending'


def test_error_before_commit_defers_or_cancels_without_outbound():
    for error, state in [(NotificationDeferred('notification_sync_stale'),'pending'),
                         (NotificationError('notification_stale'),'cancelled'),
                         (RuntimeError('PRIVATE_PAYLOAD'),'pending')]:
        c = setup()
        def fail():
            raise error
        c.sender.before_get = fail
        asyncio.run(c.worker.run_once())
        assert c.repo.state == state and c.sender.posts == 0 and c.repo.receipts == []
        assert 'PRIVATE_PAYLOAD' not in str(getattr(c.repo, 'error', ''))


def test_exception_after_commit_is_uncertain_never_retry_sent_claim():
    c = setup()
    def fail():
        raise RuntimeError('PRIVATE_RESPONSE')
    c.sender.after_post = fail
    receipt = asyncio.run(c.worker.run_once())
    assert receipt.state == c.repo.state == 'uncertain' and c.sender.posts == 1
    assert 'PRIVATE_RESPONSE' not in repr(receipt)
    asyncio.run(c.worker.run_once())
    assert c.sender.posts == 1


def test_mismatched_success_receipt_becomes_uncertain():
    c = setup()
    c.sender.receipt = SendReceipt(str(uuid5(NAMESPACE_URL,'foreign')), 'sent', message_id='foreign')
    receipt = asyncio.run(c.worker.run_once())
    assert receipt.state == c.repo.state == 'uncertain' and receipt.operation_id == c.repo.intent.notification_id


def test_lost_fence_after_post_does_not_write_false_sent():
    c = setup()
    c.sender.after_post = lambda: setattr(c.repo,'lost',True)
    asyncio.run(c.worker.run_once())
    assert c.repo.state == 'uncertain' and c.repo.receipts == [] and c.sender.posts == 1


def test_heartbeat_loss_during_slow_get_stops_post():
    c = setup()
    entered, release, renewed = Event(), Event(), Event()
    def slow():
        entered.set()
        assert release.wait(5)
    c.sender.after_get = slow
    def lost(*args, **kwargs):
        c.repo.lost = True
        renewed.set()
        raise NotificationError('notification_claim_lost')
    c.repo.renew = lost
    async def scenario():
        running = asyncio.create_task(c.worker.run_once())
        assert await asyncio.to_thread(entered.wait,3)
        assert await asyncio.to_thread(renewed.wait,3)
        release.set()
        await running
    asyncio.run(scenario())
    assert c.sender.posts == 0 and 'mark' not in c.repo.calls


@pytest.mark.parametrize('phase', ['get','post'])
def test_repeated_cancellation_drains_owned_thread_and_blocks_replacement(phase):
    c = setup()
    entered, release = Event(), Event()
    def slow():
        entered.set()
        assert release.wait(5)
    setattr(c.sender, 'after_'+phase, slow)
    async def scenario():
        first = asyncio.create_task(c.worker.run_once())
        assert await asyncio.to_thread(entered.wait,3)
        first.cancel()
        await asyncio.sleep(.02)
        first.cancel()
        await asyncio.sleep(.02)
        assert not first.done()
        replacement = asyncio.create_task(c.worker.run_once())
        await asyncio.sleep(.02)
        assert not replacement.done()
        c.worker.stop()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await replacement
    asyncio.run(scenario())
    assert c.sender.posts == (1 if phase == 'post' else 0)
    assert c.repo.state == ('sent' if phase == 'post' else 'pending')
